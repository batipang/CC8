"""Connect an explicitly enabled Live session to the current CC8 capture."""

from pathlib import Path
import threading
import time

import numpy as np

from brain_router import BrainRouter
from gpt_live import LiveSession, pcm16
from gpt_live_credentials import KEY_FILE, load_key
from reply_types import LiveReply
from assistant_identity import WAKE_PATTERN
from recognition_commands import SLEEP_COMMAND, stop_command, wake_payload
from vision import visual_request
from recognition_echo import resembles_playback
from capture_evidence import FRAME_NS, FRAME_JITTER_NS
from dsp import RATE, FRAME


MAX_WAKE_PREFIX_SECONDS = 8


class LiveTrial:
    def __init__(self, host, key, voice, factory=LiveSession):
        self.host, self.key, self.voice = host, key, voice
        self.factory = factory
        self.session = None
        self.scope = None
        self.output = None
        self.pending_output = None
        self.engaged = False
        self.near_frames = 0
        self.last_frame_ns = None
        self.near_at = float('-inf')
        self.barge_latched = False

    def generation(self):
        return self.host.generation

    @property
    def active(self):
        return self.session is not None and not self.session.stop.is_set()

    def eligible(self):
        host = self.host
        return (
            host.session.capture_owner() is not None
            and not host.session.capture_route_pending
            and host.session.live_caption_supported
            and host.session.wake
            and host.wake.allowed()
            and host.owner_access()
            and not host.calibration.active
            and not host.calibration.running
            and not host.calibration.failed
            and isinstance(host.brain, BrainRouter)
            and host.brain.selected == 'dot'
            and host.session.text_generation is None
            and host.visual.generation is None
            and host.announcements.generation is None
            and not host.meetings.reserved()
            and not host.capture.measuring
            and not host.session.tool_samples.reserved
            and host.capture.probe is None
            and host.capture.known_speech is None
        )

    def current(self, owner):
        return (
            self.session is owner
            and not owner.stop.is_set()
            and self.eligible()
            and self.scope == (self.host.session.capture_owner(), self.host.generation)
        )

    def cancel(self, *, stop_device=True):
        if self.session is not None:
            self.session.stop.set()
            if stop_device:
                self.stop_playback()

    def admission_busy(self):
        return (
            self.host.playback.playing
            or self.host.response.busy
            or self.host.capture.live_response_probe() is not None
        )

    def admit(self, initial=None):
        if self.session is not None:
            return self.active
        if self.admission_busy():
            return False
        self.scope = self.host.session.capture_owner(), self.host.generation
        self.near_frames = 0
        self.last_frame_ns = None
        self.engaged = initial is not None
        self.near_at = time.monotonic() if initial is not None else float('-inf')
        self.barge_latched = False
        owner = self.factory(self.key, self.voice, self)
        if initial is not None:
            # Send only locally confirmed wake audio ahead of newer frames.
            raw = pcm16(initial[-RATE * MAX_WAKE_PREFIX_SECONDS :])
            owner.initial_audio.extend(raw[i : i + 3200] for i in range(0, len(raw), 3200))
        self.session = owner
        self.host.add_capture_metric('live_sessions', 1)
        threading.Thread(target=owner.run, name='cc8-gpt-live', daemon=True).start()
        self.host.status('listening', 'GPT-Live 试用 · 我在听')
        return True

    def handoff(self, confirmed_audio):
        if not self.eligible():
            return False
        if confirmed_audio is not None:
            self.admit(confirmed_audio)
        return True

    def tick(self):
        if self.session is not None and not self.current(self.session):
            self.cancel()

    def consume(self, frame, voiced, credible, wake_prefix=()):
        # Called after the existing local wake/stop detectors and AEC, under
        # the capture owner guard. No WebSocket I/O on the capture thread.
        with self.host.guard:
            if not self.host.capture.endpoint_audio_current(frame.nanos):
                return True
            return self.consume_current(frame, voiced, credible, wake_prefix)

    def consume_current(self, frame, voiced, credible, wake_prefix):
        if not self.eligible():
            self.cancel()
            return False
        if self.session is None:
            if self.host.recognition.busy:
                return True
            prefix_frames = RATE * MAX_WAKE_PREFIX_SECONDS // FRAME
            initial = np.concatenate(wake_prefix[-prefix_frames:]) if wake_prefix else None
            if not self.admit(initial):
                return False
        if not self.current(self.session):
            self.cancel()
            return True
        if self.host.playback.playing and self.output is None:
            # No overlap with an existing MiniMax announcement or other owner.
            self.cancel()
            return True
        playing = self.output is not None
        echo_tail = time.monotonic() - self.host.playback.last_audio_end < 0.35
        confirmed = bool(voiced and (credible or not (playing or echo_tail)))
        previous = self.last_frame_ns
        self.last_frame_ns = frame.nanos
        if previous is not None and abs(frame.nanos - previous - FRAME_NS) > FRAME_JITTER_NS:
            self.near_frames = 0
        self.near_frames = self.near_frames + 1 if confirmed else 0
        if self.near_frames >= 5 and (
            not playing or self.host.session.voice_preferences.natural_barge
        ):
            self.engaged = True
            self.near_at = time.monotonic()
        if self.near_frames == 0 and not voiced:
            self.barge_latched = False
        if (
            playing
            and self.near_frames >= 5
            and not self.barge_latched
            and self.host.session.voice_preferences.natural_barge
        ):
            self.barge_latched = True
            self.session.interrupt_playback()
            self.stop_playback()
            self.host.add_capture_metric('live_barge_ins', 1)
        suppress = (playing or echo_tail) and (
            not confirmed or (playing and not self.host.session.voice_preferences.natural_barge)
        )
        self.session.feed(
            bytes(frame.cleaned.size * 2) if suppress else pcm16(frame.cleaned),
            confirmed and not suppress,
        )
        return True

    def stop_playback(self):
        if self.pending_output is not None:
            self.pending_output.interrupted.set()
            self.pending_output = None
        output = self.output
        if output is not None:
            self.output = None
            output.interrupted.set()
            playback = self.host.playback
            if playback.live_output is output:
                playback.clear_cancelled_pause(playback.stop_output())
                playback.notify()
                try:
                    self.host.event(
                        'live_playback_stop',
                        generation=output.generation,
                        playback_id=playback.playback_serial,
                    )
                except (OSError, EOFError, ValueError):
                    pass

    def transcript(self, owner):
        with self.host.guard:
            if self.current(owner) and self.recent_near():
                caption = owner.user_caption()
                references = self.references()
                if references and resembles_playback(caption, references):
                    return False
                self.host.touch_activity()
                payload = wake_payload(caption, WAKE_PATTERN)
                command = caption if payload is None else payload
                if SLEEP_COMMAND.fullmatch(command.strip().rstrip('。.!！')):
                    self.host.set_awake(False)
                    self.host.cancel('wake_sleep')
                    self.host.status('listening', '请说“大黄蜂”再次唤醒')
                    return
                self.host.event('transcript', text=caption)
                self.host.add_capture_metric('live_transcript_fragments', 1)
                return True
        return False

    def references(self):
        since = time.monotonic() - 8
        return [text for stamp, text in tuple(self.host.playback.spoken) if stamp >= since]

    def recent_near(self):
        return time.monotonic() - self.near_at <= 5

    def delegate(self, owner, ident, text):
        host = self.host
        with host.guard:
            if (
                not self.current(owner)
                or not self.recent_near()
                or host.response.busy
                or not host.response.queue.empty()
            ):
                return False
            payload = wake_payload(text, WAKE_PATTERN)
            text = text if payload is None else payload
            if not any(c.isalnum() for c in text) or stop_command(text, WAKE_PATTERN):
                return False
            references = self.references()
            if references and resembles_playback(text, references):
                return False
            self.near_at = float('-inf')
            if visual_request(text):
                self.cancel()
                host.begin_visual(text, 'voice')
                return True
            host.enqueue_response(
                LiveReply(
                    host.generation,
                    text,
                    lambda: self.current(owner),
                    lambda result: owner.result(ident, result),
                )
            )
            return True

    def play(self, owner, output):
        with self.host.guard:
            if not self.current(owner) or owner.interrupt.is_set() or not self.engaged:
                return False
            if self.output is not None and self.pending_output is None:
                self.pending_output = output
                return True
            if self.output is not None or self.host.playback.playing:
                return False
            self.start_output(owner, output)
            return True

    def start_output(self, owner, output):
        self.output = output
        # An answer can arrive during the tail of the request that triggered
        # it. Require a new onset before treating that speech as an interruption.
        self.barge_latched = self.near_frames > 0
        threading.Thread(
            target=self.play_worker, args=(owner, output), name='cc8-live-playback', daemon=True
        ).start()

    def play_worker(self, owner, output):
        host = self.host
        finished = False
        try:
            finished = host.playback.play_stream(
                output.stream(),
                '',
                output.generation,
                live=output,
                current=lambda: self.current(owner) and not output.interrupted.is_set(),
            )
        except Exception:
            with host.guard:
                if self.current(owner):
                    self.host.add_capture_metric('live_failures', 1)
                    self.cancel()
        finally:
            with host.guard:
                if self.output is output:
                    if finished and self.current(owner):
                        self.output = None
                        pending, self.pending_output = self.pending_output, None
                        if pending is not None and not pending.interrupted.is_set():
                            self.start_output(owner, pending)
                    else:
                        self.stop_playback()

    def closed(self, owner, failed):
        with self.host.guard:
            if self.session is not owner:
                return
            owned = self.scope == (self.host.session.capture_owner(), self.host.generation)
            self.stop_playback()
            if failed:
                self.host.add_capture_metric('live_failures', 1)
            self.session = None
            self.scope = None
            if owned and self.eligible():
                self.host.set_awake(False)
                self.host.status(
                    'error' if failed else 'listening',
                    'GPT-Live 连接失败，请重新唤醒或切回 MiniMax'
                    if failed
                    else 'GPT-Live 已结束，请说“大黄蜂”再次唤醒',
                )


def create_trial(host, config, root: Path):
    if config.brain != 'dot':
        raise ValueError('GPT-Live requires the Dot brain')
    return LiveTrial(host, load_key(root / KEY_FILE), config.gpt_live_voice)
