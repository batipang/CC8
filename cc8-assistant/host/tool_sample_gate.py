"""Own the fixed QA tool reservation and its capture-bound admission state.

Use the session's existing synchronization. The gate owns no capture, transport
or inference resources and cannot dispatch a tool.
"""

from __future__ import annotations
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING
import uuid

from capture_evidence import TOOL_TRANSCRIPT_MISMATCHES
from qa_speech import (
    SCENARIO_QUESTIONS,
    TOOL_COMPARE_ENGINES,
    normalize_question,
    tool_sample_request,
    tool_transcript_rejection,
)

if TYPE_CHECKING:
    from session_controller import SessionController


@dataclass
class ToolSample:
    token: str
    expected: str
    generation: int
    arm_until: float
    comparison: tuple[int, str] | None = None
    rejection_diagnosis: tuple[int, str] | None = None
    capture_id: str | None = None
    state: str = 'pending'
    opened_at: float = 0.0
    closed_reason: str = 'not_open'


class ToolSampleGate:
    def __init__(self):
        self._sample: ToolSample | None = None

    def arm(self, session: SessionController, case, *, compare_engine=None, diagnose_engine=None):
        if not tool_sample_request({'operation': 'tool_sample_arm', 'case': case}):
            raise ValueError('Invalid tool sample case')
        engines = [engine for engine in (compare_engine, diagnose_engine) if engine is not None]
        if len(engines) > 1 or any(engine not in TOOL_COMPARE_ENGINES for engine in engines):
            raise ValueError('Invalid tool comparison engine')
        with session.guard:
            existing = self._sample
            now = time.monotonic()
            if (
                session.active
                or session.connection is None
                or session.transport_kind != 'usb'
                or not session.capture_start_ack
                or not session.wake
                or session.hooks.voice_busy()
                or (existing is not None and now < existing.arm_until)
            ):
                raise RuntimeError('Tool sample requires an idle USB session')
            token = uuid.uuid4().hex
            self._sample = ToolSample(
                token,
                normalize_question(SCENARIO_QUESTIONS['tool'][case - 1]),
                session.generation + 1,
                now + 30,
                comparison=(case, compare_engine) if compare_engine is not None else None,
                rejection_diagnosis=(case, diagnose_engine)
                if diagnose_engine is not None
                else None,
            )
            return {'status': 'armed', 'token': token}

    def begin(self, session: SessionController, token):
        with session.guard:
            sample = self._sample
            if (
                sample is None
                or sample.token != token
                or sample.state != 'bound'
                or not session.active
                or sample.capture_id != session.capture_id
                or sample.generation != session.generation
                or session.transport_kind != 'usb'
                or not session.capture_start_ack
                or not session.rebind_seen
                or time.monotonic() >= sample.arm_until
                or not session.wake
                or session.awake
                or session.hooks.calibration().active
                or session.hooks.voice_busy()
            ):
                return {'status': 'unavailable'}
            sample.opened_at = time.monotonic()
            sample.state = 'open'
            return {'status': 'ready'}

    def release(self, session: SessionController, token):
        with session.guard:
            sample = self._sample
            # Only capture teardown can remove a guard already bound to it.
            if sample is not None and sample.token == token and sample.state == 'pending':
                self._sample = None
            return {'status': 'released' if self._sample is None else 'unavailable'}

    def rejection(self, session: SessionController, text, local_wake, captured_at):
        # Called at the final ASR admission boundary under the session guard.
        sample = self._sample
        if sample is None:
            return None
        if sample.state != 'open':
            return sample.closed_reason
        sample.state = 'closed'
        if not session.active or sample.capture_id != session.capture_id:
            return 'capture_changed'
        if sample.generation != session.generation:
            return 'generation_changed'
        if (
            session.transport_kind != 'usb'
            or not session.capture_start_ack
            or not session.rebind_seen
        ):
            return 'route_changed'
        if not local_wake:
            return 'local_wake_missing'
        if not (
            type(captured_at) in (int, float)
            and sample.opened_at <= captured_at <= sample.opened_at + 20
        ):
            return 'capture_window'
        if sample.comparison is not None:
            return 'comparison_only'
        return tool_transcript_rejection(text, sample.expected)

    def comparison(self, session: SessionController, reason='comparison_only'):
        sample = self._sample
        if sample is None:
            return None
        if reason == 'comparison_only':
            return sample.comparison
        if (
            reason in TOOL_TRANSCRIPT_MISMATCHES
            and sample.state == 'closed'
            and session.active
            and sample.capture_id == session.capture_id
            and sample.generation == session.generation
            and session.transport_kind == 'usb'
            and session.capture_start_ack
            and session.rebind_seen
        ):
            return sample.rejection_diagnosis
        return None

    def advance_generation(self, generation, reason):
        sample = self._sample
        if sample is not None:
            if sample.state == 'pending':
                if reason in ('transport_switch', 'transport_disconnect') and (
                    sample.generation == generation
                ):
                    # An Activity reopen may reconnect both routes before Start.
                    sample.generation = generation + 1
            elif (
                reason == 'transport_switch'
                and sample.state == 'bound'
                and sample.generation == generation - 1
            ):
                # No stimulus is authorized yet. Begin still needs a valid
                # rebind of this exact capture on the authenticated USB route.
                sample.generation = generation
            elif reason == 'voice' and sample.state == 'open':
                # The endpoint may advance a live turn when VAD begins.
                sample.generation = generation
            elif sample.state != 'closed':
                sample.state = 'closed'
                sample.closed_reason = (
                    'route_changed' if reason == 'transport_switch' else 'generation_changed'
                )

    def bind_capture(self, session: SessionController):
        sample = self._sample
        if sample is not None:
            bindable = (
                sample.state == 'pending'
                and time.monotonic() < sample.arm_until
                and sample.generation == session.generation
                and session.capture_id is not None
                and session.transport_kind in ('usb', 'wifi')
            )
            sample.capture_id = session.capture_id
            sample.state = 'bound' if bindable else 'closed'
            if not bindable:
                sample.closed_reason = 'capture_changed'

    def stop_capture(self):
        # An idle stop may race the already requested QA capture start.
        # Keep that reservation so the late start cannot bypass its gate.
        pending_sample = self._sample is not None and self._sample.state == 'pending'
        if not pending_sample:
            self._sample = None
        return pending_sample
