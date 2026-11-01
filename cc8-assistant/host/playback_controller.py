"""Own speaker output, completion acknowledgements and tentative pause state.

The session owns cancellation generations; DSP owns its render timeline.
Only explicit callbacks cross those boundaries. No microphone is opened here.
"""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import itertools
import threading
import time
from typing import Callable, ContextManager
from caption_timing import CaptionTiming
from playout_receipt import PlayoutReceipt
from diagnostics import TraceBook, error_kind
from failure_feedback import FailureSink
from operation_failure import OperationFailure
from dsp import RATE
from minimax_client import MiniMaxError, Cancelled
from protocol import Packet, SPEAKER, JsonValue
from reply_types import mood
from speech_providers import DirectSpeech
from session_ports import EventSink, MetricSink


@dataclass(frozen=True)
class PlaybackHooks:
    lock: ContextManager[object]
    generation: Callable[[], int]  # Read-only, non-locking session generation accessor.
    active: Callable[[], bool]
    announcement_generation: Callable[[], int | None]
    valid: Callable[[int], bool]
    cancel_session: Callable[[str], int]
    event: EventSink
    send: Callable[[Packet], None]
    status: Callable[[str, str], None]
    log: EventSink
    metric: MetricSink
    diagnostics: Callable[[], TraceBook]
    failure: FailureSink
    trace: Callable[[], str | None]
    echo_metrics: Callable[[], dict[str, JsonValue]]
    utterance_paused: Callable[[int], bool]
    speech: Callable[[], object]
    cancel_timeline_pause: Callable[[], None]
    pause_timeline: Callable[[int], None]
    resume_timeline: Callable[[int], None]


class PlaybackController:
    def __init__(self, hooks: PlaybackHooks):
        self.hooks = hooks
        self.playing = False
        self.completed = set()
        self.done = threading.Condition()
        self.last_spoken = ''
        self.last_audio_end = 0
        self.spoken = deque(maxlen=32)
        self.pause_state = None
        self.pause_serial = 0
        self.pause_token = 0
        self.publication_lock = threading.RLock()
        self.device_can_pause = False
        self.device_can_report_playout = False
        self.playout_probe = PlayoutReceipt()
        self.pause_cooldown = 0
        self.playback_serial = 0
        self.previous_spoken = ''
        self.previous_spoken_until = 0

    def stop_output(self):
        # The caller holds the session guard through the entire cancel transaction.
        with self.publication_lock:
            was_playing = self.playing
            self.playing = False
            self.playout_probe.clear()
            return was_playing

    def clear_cancelled_pause(self, was_playing):
        self.pause_state = None
        self.pause_token = 0
        self.hooks.cancel_timeline_pause()
        if was_playing:
            self.last_audio_end = time.monotonic()

    def clear_transport_pause(self):
        self.pause_state = None
        self.set_playout_capability(False)

    def notify(self):
        with self.done:
            self.done.notify_all()

    def set_pause_capability(self, enabled):
        self.device_can_pause = enabled

    def set_playout_capability(self, enabled):
        with self.publication_lock:
            self.device_can_report_playout = enabled
            if not enabled:
                self.playout_probe.clear()

    def reset_session(self):
        self.playout_probe.clear()
        self.spoken.clear()
        self.pause_state = None
        self.pause_token = 0
        self.last_spoken = ''
        self.previous_spoken = ''
        self.previous_spoken_until = 0
        self.last_audio_end = 0

    def device_event(self, event):
        name = event.get('event')
        if name == 'playout_progress':
            generation = event['generation']
            with self.publication_lock:
                if (
                    self.device_can_report_playout
                    and self.playing
                    and generation == self.hooks.generation()
                    and self.playout_probe.accept(
                        generation,
                        event['playback_id'],
                        event['first_signal_frame'],
                        event['played_frames'],
                        event['accepted_frames'],
                    )
                ):
                    # The admission point precedes local stop/publication; its
                    # clock and trace key are always the Mac and this generation.
                    self.hooks.diagnostics().mark(generation, 'playout_ack')
                    self.hooks.metric('playout_receipts', increment=True)
            return
        if name == 'play_done':
            if int(event['generation']) == self.hooks.generation():
                self.hooks.diagnostics().mark(self.hooks.generation(), 'playback_done')
            with self.done:
                if int(event['generation']) == self.hooks.generation():
                    self.completed = {self.hooks.generation()}
                self.done.notify_all()
            if int(event['generation']) == self.hooks.generation():
                self.playing = False
                self.last_audio_end = time.monotonic()
        elif (
            name in ('playback_paused', 'playback_resumed')
            and int(event.get('token', -1)) == self.pause_token
            and int(event.get('generation', -1)) == self.hooks.generation()
        ):
            if name == 'playback_paused':
                self.hooks.diagnostics().mark(self.hooks.trace(), 'pause_ack')
                self.hooks.pause_timeline(int(event['nanos']))
            else:
                self.hooks.diagnostics().mark(self.hooks.trace(), 'resume_ack')
                self.hooks.resume_timeline(int(event['nanos']))
        elif (
            name == 'playback_error'
            and int(event.get('generation', self.hooks.generation())) == self.hooks.generation()
        ):
            self.hooks.diagnostics().failure(self.hooks.generation(), 'playback', RuntimeError())
            self.hooks.log(
                'device_playback_error', error_type=str(event.get('error_type', 'Unknown'))[:50]
            )
            generation = self.hooks.generation()
            self.hooks.cancel_session('playback_error')
            self.hooks.failure(
                '设备播放失败',
                OperationFailure(
                    'Device playback failed', stage='playback', kind='provider_or_runtime'
                ),
                trace_key=generation,
            )

    def pause_for_voice(self):
        # A tentative pause retains generation; confirmed speech cancels later.
        with self.hooks.lock:
            if (
                not self.device_can_pause
                or not self.hooks.active()
                or (not self.playing)
                or self.pause_state
                or (time.monotonic() < self.pause_cooldown)
            ):
                return None
            self.pause_serial += 1
            token = self.pause_serial
            self.pause_token = token
            self.pause_state = {
                'token': token,
                'generation': self.hooks.generation(),
                'started': time.monotonic(),
            }
        self.hooks.diagnostics().mark(self.hooks.trace(), 'pause_request')
        self.hooks.event('playback_pause', token=token)
        self.hooks.metric('barge_in_pauses', increment=True)
        self.hooks.log('barge_in_tentative_pause', token=token, **self.hooks.echo_metrics())
        return token

    def resume_after_rejection(self, token, generation):
        with self.hooks.lock:
            state = self.pause_state
            if (
                token is None
                or not state
                or state['token'] != token
                or (state['generation'] != generation)
                or (not self.hooks.valid(generation))
            ):
                return
            if self.hooks.utterance_paused(token):
                return
            self.pause_state = None
            self.pause_cooldown = time.monotonic() + 0.6
        self.hooks.event('playback_resume', generation=generation, token=token)
        self.hooks.metric('barge_in_resumed', increment=True)
        self.hooks.log('barge_in_rejected_resumed', token=token)
        with self.done:
            self.done.notify_all()

    def wait_playback(self, generation):
        if not self.pause_state:
            return 0.0
        start = time.monotonic()
        while self.hooks.valid(generation):
            state = self.pause_state
            if not state:
                break
            if time.monotonic() - state['started'] > 20:
                # A newer owner request may have cancelled this generation while
                # the timeout was being checked. Never cancel that new request.
                with self.hooks.lock:
                    if not self.hooks.valid(generation):
                        break
                    self.hooks.cancel_session('barge_in_timeout')
                self.hooks.status('listening', '插话识别超时，已停止旧播报')
                break
            with self.done:
                self.done.wait(0.02)
        return time.monotonic() - start

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
            self.previous_spoken = self.last_spoken
            self.previous_spoken_until = time.monotonic() + 1
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
                self.last_spoken = text
                self.spoken.append((time.monotonic(), text))
                self.playback_serial += 1
                playback_id = self.playback_serial
                request_playout = self.device_can_report_playout
                if request_playout:
                    self.playout_probe.begin(generation, playback_id)
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
                timings=timing.rows() if timing else [],
            )
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
            if not self.hooks.valid(generation):
                return False
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
