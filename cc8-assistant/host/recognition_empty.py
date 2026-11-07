"""Handle empty/nonverbal ASR results without owning the recognition worker.

A RecognitionOutcome belongs to one dequeued turn. Its scalar disposition is
shared only by that worker and this synchronous helper, including before a
status callback can fail. Locks and capture/provider resources stay with their
existing owners; this module never recognizes, enqueues or retains audio.
"""

from __future__ import annotations
from dataclasses import dataclass
from session_ports import RecognitionHooks

MIN_ECHO_RISK_CREDIBLE_FRAMES = 5


@dataclass
class RecognitionOutcome:
    rejected: bool = False
    decision: str = 'stale'
    tool_sample_rejection: str | None = None
    compare_target: tuple[int, str] | None = None


def reject_empty(
    hooks: RecognitionHooks,
    outcome: RecognitionOutcome,
    generation,
    trace_id,
    text,
    clean,
    evidence,
):
    captured = hooks.diagnostics().capture_record(trace_id) if not clean else None
    with hooks.lock:
        if not hooks.valid(generation) or not hooks.wake_allowed():
            return
        # An eligible QA utterance consumes its guard even
        # when ASR returned no words. A later utterance must
        # not dispatch in place of this sample.
        if (
            not clean
            and evidence is not None
            and evidence.get('local_wake') is True
            and evidence.get('echo_risk') is False
            and not hooks.playing()
            and not (hooks.echo_running() or hooks.echo_failed())
        ):
            outcome.tool_sample_rejection = hooks.tool_sample_rejection(
                text, True, evidence.get('captured_at')
            )
            if outcome.tool_sample_rejection is not None:
                outcome.decision = 'tool_sample_guard'
                outcome.rejected = True
                outcome.compare_target = hooks.tool_sample_comparison(outcome.tool_sample_rejection)
                hooks.status(
                    'listening',
                    '正在核对工具测试语音'
                    if outcome.compare_target is not None
                    else '工具测试语音未通过核对',
                )
                return
        outcome.decision = 'empty'
        outcome.rejected = True
        if not hooks.playing():
            ask_again = (
                captured is not None
                and not captured['echo_risk']
                and captured['credible_frames'] >= MIN_ECHO_RISK_CREDIBLE_FRAMES
                and hooks.awake()
                and not (hooks.echo_running() or hooks.echo_failed())
            )
            if ask_again:
                hooks.status('error', '刚才没听清，请再说一遍')
            else:
                hooks.status('listening', '我在听')
    return
