"""Own speaker output and playback completion acknowledgements.

The session owns cancellation generations; DSP owns its render timeline.
Only explicit callbacks cross those boundaries. No microphone is opened here.
"""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import itertools
import threading
import time
from typing import Callable
from caption_timing import CaptionTiming
from playout_receipt import PlayoutReceipt
from diagnostics import error_kind
from failure_feedback import FailureSink
from operation_failure import OperationFailure
from dsp import RATE
from minimax_client import MiniMaxError, Cancelled
from protocol import Packet, SPEAKER
from reply_types import mood
from speech_providers import DirectSpeech
from playback_pause import PauseHooks, PlaybackPause
from playback_device_receipt import accept_device_receipt


@dataclass(frozen=True)
class PlaybackHooks(PauseHooks):
    announcement_generation: Callable[[], int | None]
    send: Callable[[Packet], None]
    failure: FailureSink
    speech: Callable[[], object]
    begin_clock_report: Callable[[int, int], bool] = lambda generation, playback_id: False
    complete_clock_report: Callable[[int, object, object, int | None], None] = (
        lambda generation, playback_id, value, frames: None
    )


class PlaybackController:
    def __init__(self, hooks: PlaybackHooks):
        self.hooks = hooks
        self.playing = False
        self.begin_pending = False
        self.end_frames = None
        self.completed = set()
        self.done = threading.Condition()
        self.last_spoken = ''
        self.last_audio_end = 0
        self.spoken = deque(maxlen=32)
        self.publication_lock = threading.RLock()
        self.pause = PlaybackPause(
            hooks,
            publication_lock=self.publication_lock,
            done=self.done,
            output_ready=lambda: self.playing and not self.begin_pending,
        )
        self.device_can_report_playout = False
        self.device_can_report_completion = False
        self.device_can_report_clock = False
        self.playout_probe = PlayoutReceipt()
        self.current_clause_ack_ms = None
        self.playback_serial = 0
        self.previous_spoken = ''
        self.previous_spoken_until = 0

    def stop_output(self):
        # The caller holds the session guard through the entire cancel transaction.
        with self.publication_lock:
            was_playing = self.playing
            self.playing = False
            self.begin_pending = False
            self.end_frames = None
            self.playout_probe.clear()
            return was_playing

    @property
    def pause_state(self):
        return self.pause.pause_state

    @property
    def device_can_pause(self):
        return self.pause.device_can_pause

    def clear_cancelled_pause(self, was_playing):
        self.pause.clear_cancelled()
        if was_playing:
            self.last_audio_end = time.monotonic()

    def clear_transport_pause(self):
        self.pause.clear_transport()
        self.set_playout_capability(False)
        self.set_completion_capability(False)

    def notify(self):
        with self.done:
            self.done.notify_all()

    def set_pause_capability(self, enabled, *, applied_receipt=False):
        self.pause.set_capability(enabled, applied_receipt=applied_receipt)

    def set_playout_capability(self, enabled):
        with self.publication_lock:
            self.device_can_report_playout = enabled
            if not enabled:
                self.playout_probe.clear()
                self.device_can_report_clock = False

    def set_clock_report_capability(self, enabled):
        with self.publication_lock:
            self.device_can_report_clock = bool(
                enabled and self.device_can_report_playout and self.device_can_report_completion
            )

    def set_completion_capability(self, enabled):
        with self.publication_lock:
            self.device_can_report_completion = bool(enabled)
            if not enabled:
                self.device_can_report_clock = False

    def matches_completion(self, event):
        ident = event.get('playback_id')
        if ident is None:
            return not self.device_can_report_completion
        return type(ident) is int and ident > 0 and ident == self.playback_serial

    def reset_session(self):
        self.playout_probe.clear()
        self.spoken.clear()
        self.pause.clear()
        self.last_spoken = ''
        self.previous_spoken = ''
        self.previous_spoken_until = 0
        self.last_audio_end = 0

    def device_event(self, event):
        accept_device_receipt(self, event)

    def pause_for_voice(self, trigger_nanos=None):
        return self.pause.pause_for_voice(trigger_nanos)

    def resume_after_rejection(self, token, generation):
        return self.pause.resume_after_rejection(token, generation)

    def wait_playback(self, generation):
        return self.pause.wait_playback(generation)

    def play(self, pcm, text, generation, role='assistant'):
        return self.play_stream(iter([pcm]), text, generation, role)

    def output_event(self, name, **fields):
        try:
            self.hooks.event(name, **fields)
        except (OSError, EOFError, ValueError) as error:
            raise OperationFailure(
                'Device event send failed', stage='transport', kind=error_kind(error)
            ) from None

    def output_packet(self, packet):
        try:
            self.hooks.send(packet)
        except (OSError, EOFError, ValueError) as error:
            raise OperationFailure(
                'Device PCM send failed', stage='transport', kind=error_kind(error)
            ) from None

    def audio_chunks(self, chunks):
        try:
            yield from chunks
        except (Cancelled, OperationFailure):
            raise
        except Exception as error:
            raise OperationFailure(
                'Speech provider failed', stage='tts', kind=error_kind(error)
            ) from None

    def play_stream(self, chunks, text, generation, role='assistant', timing=None):
        # Each clause owns its iterator, caption publisher and playback_id.
        # Sending all PCM is not a device play_done acknowledgement.
        iterator = self.audio_chunks(chunks)
        prefill = []
        count = 0
        started = False
        completed = False
        try:
            # Keep 200 ms buffered while cloud audio continues to arrive.
            while count < 3200 and self.hooks.valid(generation):
                try:
                    piece = next(iterator)
                except StopIteration:
                    break
                if len(piece):
                    self.hooks.diagnostics().mark(generation, 'first_pcm')
                    prefill.append(piece)
                    count += len(piece)
            if not self.hooks.valid(generation):
                return False
            if not prefill:
                raise MiniMaxError('No audio available for playback')
            with self.done:
                self.completed.discard(generation)
            self.wait_playback(generation)
            if not self.hooks.valid(generation):
                return False
            # Owner cancel takes session guard -> publication -> probe. Render
            # only queries validity (no guard acquisition), so it can still
            # drain while an owner holds the session guard. No wire I/O here.
            with self.publication_lock:
                if not self.hooks.valid(generation):
                    return False
                self.playing = True
                self.begin_pending = True
                self.end_frames = None
                self.current_clause_ack_ms = None
                self.playback_serial += 1
                playback_id = self.playback_serial
                request_playout = self.device_can_report_playout
                if request_playout:
                    self.playout_probe.begin(generation, playback_id)
                request_clock = self.device_can_report_clock and self.hooks.begin_clock_report(
                    generation, playback_id
                )
            # Output is locally active before any begin notification can fail.
            started = True
            self.output_event(
                'play_begin',
                generation=generation,
                text=text,
                mood=mood(text),
                role='announcement' if generation == self.hooks.announcement_generation() else role,
                streaming=True,
                playback_id=playback_id,
                playout_receipt_version=1 if request_playout else 0,
                report_audio_clock=request_clock,
                timings=timing.rows() if timing else [],
            )
            with self.publication_lock:
                if not self.hooks.valid(generation) or playback_id != self.playback_serial:
                    return False
                published_at = time.monotonic()
                self.previous_spoken = self.last_spoken
                self.previous_spoken_until = published_at + 1
                self.last_spoken = text
                self.spoken.append((published_at, text))
                # Only now can an ordered pause reach the new device player.
                self.begin_pending = False
            if timing:
                timing.publish = lambda rows: (
                    self.output_event(
                        'caption_timing',
                        generation=generation,
                        playback_id=playback_id,
                        timings=rows,
                    )
                    if self.hooks.valid(generation)
                    else None
                )
            sequence = 0
            written = 0
            start = time.monotonic()
            for pcm in itertools.chain(prefill, iterator):
                start += self.wait_playback(generation)
                if not self.hooks.valid(generation):
                    return False
                for offset in range(0, len(pcm), 320):
                    start += self.wait_playback(generation)
                    if not self.hooks.valid(generation):
                        return False
                    piece = pcm[offset : offset + 320].astype('<i2')
                    if request_playout:
                        self.playout_probe.reserve(generation, playback_id, written, piece)
                    self.output_packet(
                        Packet(SPEAKER, generation, sequence, written, 0, piece.tobytes())
                    )
                    sequence += 1
                    written += len(piece)
                    # Fill the device ahead, then pace; do not starve AudioTrack.
                    deadline = start + max(0, written - 3200) / RATE
                    while self.hooks.valid(generation) and time.monotonic() < deadline:
                        waited = self.wait_playback(generation)
                        start += waited
                        deadline += waited
                        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            with self.publication_lock:
                if not self.hooks.valid(generation) or playback_id != self.playback_serial:
                    return False
                # The device can drain and acknowledge before the send returns.
                self.end_frames = written
            self.output_event('play_end', generation=generation, frames=written)
            with self.done:
                deadline = time.monotonic() + 4
            while self.hooks.valid(generation):
                with self.done:
                    if generation in self.completed:
                        break
                # Pause timeout can acquire the session guard and call cancel.
                # Never retain the completion condition across that callback:
                # owner requests acquire session guard -> completion condition.
                deadline += self.wait_playback(generation)
                if not self.hooks.valid(generation):
                    break
                with self.done:
                    # Recheck after dropping/reacquiring the condition so a
                    # device ACK in the gap cannot become a lost notification.
                    if generation in self.completed:
                        break
                    if time.monotonic() >= deadline:
                        break
                    self.done.wait(0.1)
            with self.done:
                completed = generation in self.completed
            if not completed and self.hooks.valid(generation):
                # Admission, cancellation and error UI belong to one owner
                # transaction. A new request must not enter between them.
                with self.hooks.lock:
                    with self.done:
                        completed = generation in self.completed
                    if not completed and self.hooks.valid(generation):
                        self.hooks.log('playback_completion_timeout')
                        error = OperationFailure(
                            'Playback acknowledgement timed out', stage='playback', kind='timeout'
                        )
                        self.hooks.diagnostics().failure(generation, 'playback', error)
                        self.hooks.cancel_session('completion_timeout')
                        self.hooks.failure('设备播放未确认', error, trace_key=generation)
                        return False
            return self.hooks.valid(generation)
        except Exception as error:
            self.hooks.diagnostics().failure(
                generation, self.hooks.diagnostics().current_stage(generation), error
            )
            with self.hooks.lock:
                if self.hooks.valid(generation) and started:
                    self.hooks.cancel_session('audio_stream_error')
                    self.hooks.failure('这次播报没有完成', error, trace_key=generation)
            raise
        finally:
            close = getattr(iterator, 'close', None)
            if close:
                close()
            if timing:
                timing.publish = None

    def speak(self, text, generation):
        self.hooks.diagnostics().mark(generation, 'tts_start')
        speech = self.hooks.speech()
        timing = CaptionTiming(text) if isinstance(speech, DirectSpeech) else None

        def source():
            # Invoke even eager providers inside the explicit TTS boundary.
            if timing:
                yield from speech.stream(text, lambda: self.hooks.valid(generation), timing.feed)
            elif hasattr(speech, 'stream'):
                yield from speech.stream(text, lambda: self.hooks.valid(generation))
            else:
                yield speech.synthesize(text)

        return self.play_stream(source(), text, generation, timing=timing)
