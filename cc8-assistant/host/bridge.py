"""CC8 voice terminal: local AEC, MiniMax ASR/TTS and owner-configured Dot.

Microphone samples stay in RAM. Only explicitly enabled smoke/replay tools
read audio files. The companion APK never starts capture from a host command.
"""

from __future__ import annotations
from pathlib import Path
import signal as signal
import subprocess as subprocess
import threading as threading
import time

from protocol import EVENT, MIC, MIC_PAIRED, ROUTE_BOUND, MEETING, decode_event
from transport import Pairing, TransportHub
from echo_processors import WebRtcEchoCanceller
from fast_stop import FastStopDetector
from local_wake import WakeDetector
from brain_router import BrainRouter
from announce import AnnouncementServer
from acoustic_probes import AcousticProbes
from calibration_controller import STARTUP_GREETING as STARTUP_GREETING
from composition import build_components, build_device_events, build_session
from assistant_identity import WAKE_WORD as WAKE_WORD, WAKE_PATTERN as WAKE_PATTERN
from app_runtime import BackendDependencies, run_backend as run_app
from minimax_client import MiniMaxBrain
from workspace import Workspace
from diagnostics import TraceBook
from connection_observation import connection_report
from runtime_guard import RuntimeLease, RuntimeConflict
from configuration import load_config
from report_watch_store import ReportWatchStore
from speech_providers import (
    LocalSpeech as LocalSpeech,
    DirectSpeech as DirectSpeech,
    model_path as model_path,
    wav_samples as wav_samples,
)
from reply_types import (
    Reply as Reply,
    VisualReply as VisualReply,
    TextReply as TextReply,
    mood as mood,
)
from usb_transport import (
    USBPipe as USBPipe,
    usb_target as usb_target,
    usb_present as usb_present,
    connect_device as connect_device,
)
from dot_client import runtime_status as dot_runtime_status

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'device-access/private/voice-runtime'
LIBRARY = ROOT / '.toolchains/cc8/speexdsp-1.2.1/libspeexdsp/.libs/libspeexdsp.dylib'


class Bridge:
    # Mac 端协调收音、消回声/断句、ASR、大脑及 TTS。
    # epoch 标识设备录音会话；generation 标识可被打断的回答，两者不可互换。
    def __init__(
        self,
        speech,
        brain,
        library=LIBRARY,
        fast_stop=None,
        echo_check=False,
        workspace=None,
        wake_detector=None,
        wake_window_seconds=60,
        capture_frontend='mono',
        preference_memory=None,
        vocabulary_store=None,
        report_sources_store=None,
        report_archive_store=None,
        report_checkpoint_store=None,
        report_watch_store=None,
    ):
        self.diagnostics = TraceBook()
        started_at = time.monotonic()
        self.speech = speech
        self.brain = brain
        self.library = library
        self.workspace = workspace or Workspace()
        self.transport_hub = None
        self.metrics = {
            'mic_frames': 0,
            'render_frames': 0,
            'sequence_gaps': 0,
            'interruptions': 0,
            'utterances': 0,
            'raw_audio_saved': False,
        }
        self.session = build_session(self, capture_frontend=capture_frontend)
        if isinstance(self.brain, BrainRouter):
            self.brain.dot.device_control = self.control_device
            self.brain.dot.reminders = self.reminder_action
        components = build_components(
            self,
            echo_factory=lambda **kwargs: WebRtcEchoCanceller(**kwargs),
            wake_pattern=WAKE_PATTERN,
            fast_stop=fast_stop,
            echo_check=echo_check,
            wake_detector=wake_detector,
            wake_window_seconds=wake_window_seconds,
            started_at=started_at,
            private_directory=lambda: PRIVATE,
            wake_word=WAKE_WORD,
            dot_runtime=lambda: dot_runtime_status(),
            preference_memory=preference_memory,
            vocabulary_store=vocabulary_store,
            report_sources_store=report_sources_store,
            report_archive_store=report_archive_store,
            report_checkpoint_store=report_checkpoint_store,
            report_watch_store=report_watch_store,
        )
        self.calibration = components.calibration
        self.visual = components.visual
        self.playback = components.playback
        self.capture = components.capture
        self.recognition = components.recognition
        self.response = components.response
        self.announcements = components.announcements
        self.reminders = components.reminders
        self.device = components.device
        self.reporting = components.reporting
        self.wake = components.wake
        self.chats = components.chats
        self.saved_tasks = components.saved_tasks
        self.preferences = components.preferences
        self.vocabulary = components.vocabulary
        self.workflows = components.workflows
        self.meetings = components.meetings
        if isinstance(self.brain, BrainRouter):
            self.brain.preferences = self.preferences
            self.brain.work_reports = self.chats.voice
        self.probes = AcousticProbes(
            session=self.session,
            wake=self.wake,
            capture=self.capture,
            playback=self.playback,
            calibration=self.calibration,
            recognition=self.recognition,
            response=self.response,
            announcements=self.announcements,
            cancel=lambda reason: self.cancel(reason),
        )
        self.device_events = build_device_events(self)
        self.recognition.start()
        self.response.start()

    @property
    def generation(self):
        return self.session.generation

    @property
    def guard(self):
        return self.session.guard

    def log(self, event, **fields):
        return self.reporting.log(event, **fields)

    def send(self, p):
        return self.session.send(p)

    def event(self, event, generation=None, **fields):
        return self.session.event(event, generation, **fields)

    def change_transport(self, channel):
        return self.session.change_transport(channel)

    def status(self, phase, message):
        return self.session.status(phase, message)

    def valid(self, generation):
        return self.session.valid(generation)

    def touch_activity(self):
        return self.session.touch_activity()

    def idle_tick(self):
        return self.session.idle_tick()

    def heartbeat_tick(self):
        try:
            self.response.expire_corrections()
            self.meetings.tick()
            if self.session.connection is not None:
                self.idle_tick()
                self.event('ping')
        except (OSError, EOFError, ValueError, RuntimeError) as error:
            # A failed send or reminder-store write must not end the timer thread.
            self.log('heartbeat_retry', error_type=type(error).__name__)

    def publish_workspace(self):
        if self.session.connection is not None:
            self.event('workspace', **self.workspace.snapshot())

    def health(self):
        return self.reporting.health()

    def diagnostics_report(self):
        return {'ok': True, 'health': self.health(), **self.diagnostics.report()}

    def connection_diagnostics(self):
        return connection_report(self.device.control)

    def reminder_action(self, args, is_current=lambda: True):
        return self.reminders.action(args, is_current)

    def submit_text(self, text):
        return self.session.submit_text(text)

    def reminder_tick(self):
        result = self.reminders.tick()
        scheduler = self.chats.watch_ui.scheduler
        if scheduler is not None:
            scheduler.tick()
        self.workflows.scheduler.tick()
        return result

    def control_device(self, action, percent=None):
        return self.device.control(action, percent)

    def pause_for_voice(self, trigger_nanos=None):
        return self.playback.pause_for_voice(trigger_nanos)

    def resume_after_rejection(self, token, generation):
        return self.playback.resume_after_rejection(token, generation)

    def wait_playback(self, generation):
        return self.playback.wait_playback(generation)

    def announce(self, text):
        return self.announcements.announce(text)

    def playback_probe(self):
        return self.probes.playback_probe()

    def cancel_playback_probe(self, job, probe):
        self.probes.cancel_playback_probe(job, probe)

    def finish_playback_probe(self, probe):
        self.probes.finish_playback_probe(probe)

    def arm_response_probe(self, case):
        return self.probes.arm_response_probe(case)

    def read_response_probe(self, token):
        return self.probes.read_response_probe(token)

    def arm_known_speech(self, root, directory, metadata, generation, *, near_only=False):
        return self.probes.arm_known_speech(
            root, directory, metadata, generation, near_only=near_only
        )

    def mark_known_speech_stimulus(self, token, *, verify_far=False):
        return self.probes.mark_known_speech_stimulus(token, verify_far=verify_far)

    def read_known_speech(self, token):
        return self.probes.read_known_speech(token)

    def cancel_playback_observation(self):
        self.probes.cancel_playback_observation()

    def read_announcement(self, job):
        return self.announcements.read_announcement(job)

    def cancel(self, reason='speech', *, expected_generation=None, expected_epoch=None):
        if expected_generation is None and expected_epoch is None:
            return self.session.cancel(reason)
        return self.session.cancel(
            reason, expected_generation=expected_generation, expected_epoch=expected_epoch
        )

    def stop(self):
        return self.session.stop()

    def on_event(self, j):
        return self.device_events.on_event(j)

    def on_packet(self, p):
        token = getattr(self.session.connection, 'route_token', None)
        if token is not None:
            p = p.unbind_route(token)
            if p is None:
                return
        elif p.kind == ROUTE_BOUND:
            return
        # A preserved native recorder is not registered on a freshly selected
        # route until its current Start/Rebind is accepted. Never buffer early PCM.
        if p.kind in (MIC, MIC_PAIRED) and (
            self.session.capture_route_pending
            or getattr(self.session.connection, 'application_ready', True) is not True
        ):
            return
        if p.kind == EVENT:
            try:
                self.on_event(decode_event(p.data))
            except (KeyError, TypeError, OverflowError):
                raise ValueError('Invalid device event fields') from None
            return
        if p.kind == MEETING:
            self.meetings.on_packet(p)
            return
        self.capture.on_packet(p)

    def process_microphone(self, p):
        return self.capture.process_microphone(p)

    def add_capture_metric(self, name, amount):
        self.metrics[name] += amount

    def discard_recognition(self):
        return self.recognition.discard()

    def enqueue_recognition(self, job):
        return self.recognition.enqueue(job)

    def apply_fast_stop(self, keyword, stamp):
        return self.capture.endpoint.apply_fast_stop(keyword, stamp)

    def save_status(self):
        return self.reporting.save_status()

    def play(self, pcm, text, generation, role='assistant'):
        return self.playback.play(pcm, text, generation, role)

    def play_stream(self, chunks, text, generation, role='assistant', timing=None):
        return self.playback.play_stream(chunks, text, generation, role, timing)

    def speak(self, text, generation):
        return self.playback.speak(text, generation)

    def startup_reference(self):
        return self.calibration.startup_reference()

    def calibrate(self, generation):
        return self.calibration.calibrate(generation)

    def check_echo(self, generation):
        return self.calibration.check_echo(generation)

    def align_reference(self, generation=None):
        return self.capture.echo.align_reference(generation)

    def failure(self, message, error, trace_key=None):
        return self.reporting.failure(message, error, trace_key)

    def work(self):
        return self.recognition.work()

    def set_awake(self, value):
        return self.session.set_awake(value)

    def set_metric_field(self, name, field, value):
        self.metrics[name][field] = value

    def add_metric_field(self, name, field, value):
        self.metrics[name][field] += value

    def record_echo_pair(self, text, render_text, echo):
        # Only the explicitly armed echo diagnostic records these bounded pairs.
        pairs = self.metrics['echo_self_check']['diagnostic_text_pairs']
        if len(pairs) < 8:
            pairs.append({'heard': text[:200], 'render_asr': render_text[:200], 'rejected': echo})

    def record_metric(self, name, value=None, *, increment=False):
        self.metrics[name] = self.metrics.get(name, 0) + 1 if increment else value

    def request_snapshot(self, *, metadata_only=False):
        return self.visual.request_snapshot(metadata_only=metadata_only)

    def cancel_snapshot(self, job):
        return self.visual.cancel_snapshot(job)

    def capture_snapshot(self, job, capture):
        return self.visual.capture_snapshot(job, capture)

    def begin_visual(self, text, origin, nonce=''):
        return self.visual.begin_visual(text, origin, nonce)

    def answer_visual(self, job):
        return self.visual.answer_visual(job)

    def finish_text_response(self, generation):
        return self.session.finish_text_response(generation)

    def enqueue_response(self, job):
        return self.response.enqueue(job)

    def respond_work(self):
        return self.response.work()

    def respond_job(self, job):
        return self.response.respond_job(job)

    def answer(self, text, generation, image=None):
        return self.response.answer(text, generation, image=image)


def main():
    # 锁覆盖模型加载到退出全程；直接执行 bridge.py 也不能绕过单实例检查。
    try:
        with RuntimeLease(ROOT):
            run_backend()
    except BlockingIOError:
        raise SystemExit('已有大黄蜂后台或环境准备任务占用运行锁，未启动第二份模型。')
    except RuntimeConflict as error:
        raise SystemExit(str(error)) from None


def run_backend():
    return run_app(
        BackendDependencies(
            private=PRIVATE,
            description=__doc__,
            load_config=load_config,
            direct_speech=DirectSpeech,
            local_speech=LocalSpeech,
            fast_stop=FastStopDetector,
            wake_detector=WakeDetector.load,
            brain_router=BrainRouter,
            minimax_brain=MiniMaxBrain,
            bridge=lambda *args, **kwargs: Bridge(
                *args,
                **kwargs,
                report_watch_store=ReportWatchStore(PRIVATE / 'work-report-watch.json'),
            ),
            workspace=Workspace,
            transport_hub=TransportHub,
            pairing=Pairing,
            connect_device=connect_device,
            usb_present=usb_present,
            announcement_server=AnnouncementServer,
        )
    )


if __name__ == '__main__':
    main()
