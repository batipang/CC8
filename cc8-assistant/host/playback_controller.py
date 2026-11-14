"""Own speaker output and playback completion acknowledgements.

The session owns cancellation generations; DSP owns its render timeline.
Only explicit callbacks cross those boundaries. No microphone is opened here.
"""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from contextlib import nullcontext
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
from minimax_client import Cancelled
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
    begin_response_output: Callable[[int], None] = lambda generation: None
    begin_clock_report: Callable[[int, int], bool] = lambda generation, playback_id: False
    complete_clock_report: Callable[[int, object, object, int | None], None] = (
        lambda generation, playback_id, value, frames: None
    )


class PlaybackController:
    def __init__(self, hooks: PlaybackHooks):
        self.hooks = hooks
        self.live_output = None
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
            if self.live_output is not None:
                self.live_output.interrupted.set()
                self.live_output = None
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
        return self.play_stream(iter([pcm]), text, generation, role, source_stage='playback')

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

    def audio_chunks(self, chunks, stage):
        while True:
            try:
                piece = next(chunks)
            except StopIteration:
                return
            except (Cancelled, OperationFailure):
                raise
            except Exception as error:
                raise OperationFailure(
                    'Audio source failed', stage=stage, kind=error_kind(error)
                ) from None
            yield piece

    def play_stream(
        self,
        chunks,
        text,
        generation,
        role='assistant',
        timing=None,
        *,
        live=None,
        current=None,
        before_playback=None,
        source_stage='tts',
    ):
        def valid():
            return self.hooks.valid(generation) and (current is None or current())

        # Each clause owns its iterator, caption publisher and playback_id.
        # Sending all PCM is not a device play_done acknowledgement.
        stage = 'playback' if live is not None else source_stage
        source = None
        prefill = []
        count = 0
        started = False
        completed = False
        try:
            try:
                source = iter(chunks)
            except (Cancelled, OperationFailure):
                raise
            except Exception as error:
                raise OperationFailure(
                    'Audio source unavailable', stage=stage, kind=error_kind(error)
                ) from None
            iterator = self.audio_chunks(source, stage)
            # Keep 200 ms buffered while cloud audio continues to arrive.
            while count < 3200 and valid():
                try:
                    piece = next(iterator)
                except StopIteration:
                    break
                if len(piece):
                    self.hooks.diagnostics().mark(generation, 'first_pcm')
                    prefill.append(piece)
                    count += len(piece)
            if not valid():
                return False
            if not prefill:
                raise OperationFailure(
                    'Audio source produced no PCM', stage=stage, kind='provider_or_runtime'
                )
            with self.done:
                self.completed.discard(generation)
            self.wait_playback(generation)
            if not valid():
                return False
            # Owner cancel takes session guard -> publication -> probe. Render
            # only queries validity (no guard acquisition), so it can still
            # drain while an owner holds the session guard. No wire I/O here.
            # Check a reminder again after asynchronous synthesis but before
            # committing this player's ownership. Do not hold the session
            # lock during TTS, pacing, or acknowledgement waits.
            with (
                self.hooks.lock if before_playback is not None else nullcontext(),
                self.publication_lock,
            ):
                if not valid():
                    return False
                if before_playback is not None and not before_playback():
                    return False
                self.live_output = live
                self.playing = True
                self.begin_pending = True
                self.end_frames = None
                self.current_clause_ack_ms = None
                self.playback_serial += 1
                playback_id = self.playback_serial
                request_playout = self.device_can_report_playout
                if request_playout:
                    self.playout_probe.begin(generation, playback_id)
                self.hooks.begin_response_output(generation)
                request_clock = self.device_can_report_clock and self.hooks.begin_clock_report(
                    generation, playback_id
                )
            # Output is locally active before any begin notification can fail.
            started = True
            caption, rows = (
                live.caption() if live is not None else (text, timing.rows() if timing else [])
            )
            # Live barge-in stops a player within the same turn generation.
            # Order its begin against that stop with the capture-owner guard.
            with self.hooks.lock if live is not None else nullcontext():
                if not valid():
                    return False
                self.output_event(
                    'play_begin',
                    generation=generation,
                    text=caption,
                    mood=mood(caption),
                    role='announcement'
                    if generation == self.hooks.announcement_generation()
                    else role,
                    streaming=True,
                    playback_id=playback_id,
                    playout_receipt_version=1 if request_playout else 0,
                    report_audio_clock=request_clock,
                    timings=rows,
                    **({'live_caption': True} if live is not None else {}),
                )
                with self.publication_lock:
                    if not valid() or playback_id != self.playback_serial:
                        return False
                    published_at = time.monotonic()
                    self.previous_spoken = self.last_spoken
                    self.previous_spoken_until = published_at + 1
                    self.last_spoken = caption
                    self.spoken.append((published_at, caption))
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
                    if valid()
                    else None
                )
            if live is not None:

                def publish_live(caption, rows):
                    with self.hooks.lock:
                        if (
                            not valid()
                            or self.live_output is not live
                            or not caption.startswith(self.last_spoken)
                        ):
                            return
                        self.output_event(
                            'live_caption',
                            generation=generation,
                            playback_id=playback_id,
                            text=caption,
                            timings=rows,
                        )
                        self.last_spoken = caption
                        if self.spoken:
                            self.spoken[-1] = (time.monotonic(), caption)

                live.subscribe(publish_live)
            sequence = 0
            written = 0
            start = time.monotonic()
            for pcm in itertools.chain(prefill, iterator):
                start += self.wait_playback(generation)
                if not valid():
                    return False
                for offset in range(0, len(pcm), 320):
                    start += self.wait_playback(generation)
                    if not valid():
                        return False
                    piece = pcm[offset : offset + 320].astype('<i2')
                    if request_playout:
                        self.playout_probe.reserve(generation, playback_id, written, piece)
                    self.output_packet(
                        Packet(
                            SPEAKER,
                            generation,
                            sequence,
                            written,
                            playback_id if live is not None else 0,
                            piece.tobytes(),
                        )
                    )
                    sequence += 1
                    written += len(piece)
                    # Fill the device ahead, then pace; do not starve AudioTrack.
                    deadline = start + max(0, written - 3200) / RATE
                    while valid() and time.monotonic() < deadline:
                        waited = self.wait_playback(generation)
                        start += waited
                        deadline += waited
                        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            with self.publication_lock:
                if not valid() or playback_id != self.playback_serial:
                    return False
                # The device can drain and acknowledge before the send returns.
                self.end_frames = written
            self.output_event(
                'play_end',
                generation=generation,
                frames=written,
                **({'playback_id': playback_id} if live is not None else {}),
            )
            with self.done:
                deadline = time.monotonic() + 4
            while valid():
                with self.done:
                    if generation in self.completed:
                        break
                # Pause timeout can acquire the session guard and call cancel.
                # Never retain the completion condition across that callback:
                # owner requests acquire session guard -> completion condition.
                deadline += self.wait_playback(generation)
                if not valid():
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
            if not completed and valid():
                # Admission, cancellation and error UI belong to one owner
                # transaction. A new request must not enter between them.
                with self.hooks.lock:
                    with self.done:
                        completed = generation in self.completed
                    if not completed and valid():
                        self.hooks.log('playback_completion_timeout')
                        error = OperationFailure(
                            'Playback acknowledgement timed out', stage='playback', kind='timeout'
                        )
                        self.hooks.diagnostics().failure(generation, 'playback', error)
                        self.hooks.cancel_session('completion_timeout')
                        self.hooks.failure('设备播放未确认', error, trace_key=generation)
                        return False
            return valid()
        except Exception as error:
            self.hooks.diagnostics().failure(
                generation, self.hooks.diagnostics().current_stage(generation), error
            )
            with self.hooks.lock:
                if valid() and started:
                    self.hooks.cancel_session('audio_stream_error')
                    self.hooks.failure('这次播报没有完成', error, trace_key=generation)
            raise
        finally:
            if live is not None:
                live.subscribe(None)
                with self.publication_lock:
                    if self.live_output is live and not self.playing:
                        self.live_output = None
            if timing:
                timing.publish = None
            # Closing the source after an early stop must not replace a
            # confirmed play_done or the primary TTS/transport failure.
            close = getattr(source, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception as error:
                    self.hooks.log(
                        'playback_iterator_close_failed', error_type=type(error).__name__
                    )

    def speak(self, text, generation, *, before_playback=None):
        if before_playback is not None:
            with self.hooks.lock:
                if not self.hooks.valid(generation) or not before_playback():
                    return False
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

        return self.play_stream(
            source(), text, generation, timing=timing, before_playback=before_playback
        )
