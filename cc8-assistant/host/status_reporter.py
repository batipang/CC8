"""Read-only runtime reporting and serialized owner-private status output.

Reports query component owners. They never alter capture, request generations,
providers or transport selection; aggregate metric mappings arrive as copies.
"""

from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
import uuid
from runtime_resources import ResourceSampler
from typing import Callable
from brain_router import BrainRouter
from diagnostics import TraceBook, error_kind
from operation_failure import OperationFailure
from dsp import NearSpeechDetector, VoiceDetector
from failure_feedback import feedback
from dot_receipt import UnconfirmedTurnStart
from release_info import RELEASE
from session_controller import SessionView
from playback_controller import PlaybackController
from capture_pipeline import CapturePipeline
from calibration_controller import CalibrationController
from vision_controller import VisionController
from announcement_controller import AnnouncementController
from session_ports import EventSink
from wake_controller import WakeController


@dataclass(frozen=True)
class ReportingHooks:
    session: Callable[[], SessionView]
    wake: Callable[[], WakeController]
    diagnostics: Callable[[], TraceBook]
    playback: Callable[[], PlaybackController]
    capture: Callable[[], CapturePipeline]
    calibration: Callable[[], CalibrationController]
    visual: Callable[[], VisionController]
    announcements: Callable[[], AnnouncementController]
    brain: Callable[[], object]
    speech: Callable[[], object]
    recognition_alive: Callable[[], bool]
    response_alive: Callable[[], bool]
    metrics: Callable[[], dict[str, object]]
    transport_diagnostics: Callable[[], dict[str, object]]
    dot_runtime: Callable[[], dict[str, object]]
    private_directory: Callable[[], Path]
    wake_word: str
    log: EventSink
    status: Callable[[str, str], None]


class StatusReporter:
    def __init__(self, hooks: ReportingHooks, *, started_at: float):
        self.hooks = hooks
        self.started_at = started_at
        self.status_lock = threading.Lock()
        self.instance = uuid.uuid4().hex
        self.resources = ResourceSampler()

    def log(self, event, **fields):
        message = json.dumps(
            {'event': event, 'monotonic_seconds': round(time.monotonic(), 3), **fields},
            ensure_ascii=False,
        )
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            # A replaced supervisor can no longer drain this process's stdout
            # pipe. Optional diagnostics must not close a healthy transport or
            # kill a worker; serialization errors above still reach the caller.
            pass

    def health(self):
        state = self.hooks.session()
        playback = self.hooks.playback()
        endpoint = self.hooks.capture().endpoint
        stop = endpoint.fast_stop_observation
        metrics = self.hooks.metrics()
        # 白名单输出就绪位，不导出 metrics 内可能存在的自检文字和用户内容。
        return {
            'ok': True,
            'bridge_version': RELEASE.backend_version,
            'release_id': RELEASE.release_id,
            'protocol_version': RELEASE.protocol_version,
            'uptime_seconds': round(time.monotonic() - self.started_at, 1),
            'runtime_instance': self.instance,
            'resources': self.resources.sample(),
            'model_ready': True,
            'playout_receipt_supported': playback.device_can_report_playout,
            'clause_receipts_supported': (
                playback.device_can_report_playout and playback.device_can_report_completion
            ),
            'playback_evidence_supported': True,
            'known_speech_evidence_supported': True,
            'causal_speech_model_supported': True,
            'far_reference_preflight_supported': True,
            'far_reference_overlap_supported': True,
            'leading_acoustic_evidence_supported': True,
            'response_acoustic_evidence_supported': True,
            'render_clock_evidence_supported': playback.device_can_report_clock,
            'local_wake_ready': self.hooks.wake().ready,
            'waiting_for_wake': state.active
            and not self.hooks.calibration().active
            and not state.awake,
            'recognition_worker': self.hooks.recognition_alive(),
            'response_worker': self.hooks.response_alive(),
            'connected': state.connected,
            'transport': state.transport_kind,
            'microphone_active': state.active,
            'calibrating': self.hooks.calibration().active,
            'asr_engine': getattr(getattr(self.hooks.speech(), 'recognizer', None), 'engine', None),
            'brain_provider': getattr(self.hooks.brain(), 'selected', 'echo'),
            'dot_runtime': self.hooks.dot_runtime(),
            'playing': self.hooks.playback().playing,
            'mic_frames': metrics.get('mic_frames', 0),
            'render_frames': metrics.get('render_frames', 0),
            'local_wake_count': metrics.get('local_wake_count', 0),
            'local_wake_english_count': metrics.get('local_wake_english_count', 0),
            'voice_asr_attempts': metrics.get('voice_asr_attempts', 0),
            'fast_stop_ready': endpoint.fast_stop is not None,
            'fast_stop_count': metrics.get('fast_stop_count', 0),
            'barge_in_pauses': metrics.get('barge_in_pauses', 0),
            'barge_in_resumed': metrics.get('barge_in_resumed', 0),
            'fast_stop_timing_supported': True,
            'fast_stop_observation': (
                {'cancel_generation': stop[0], 'detecting_frame_nanos': stop[1]}
                if stop is not None and state.active and state.generation == stop[0]
                else None
            ),
        }

    def save_status(self):
        state = self.hooks.session()
        private = self.hooks.private_directory()
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(private, 0o700)
        data = {
            'connected': state.connected,
            'active': state.active,
            'calibrating': self.hooks.calibration().active,
            'playing': self.hooks.playback().playing,
            'generation': state.generation,
            'wake_mode': state.wake,
            'wake_word': self.hooks.wake_word,
            'playout_receipt_supported': self.hooks.playback().device_can_report_playout,
            'local_wake_ready': self.hooks.wake().ready,
            'waiting_for_wake': state.active
            and not self.hooks.calibration().active
            and not state.awake,
            'wake_window_seconds': self.hooks.wake().window_seconds,
            'bridge_version': RELEASE.backend_version,
            'release_id': RELEASE.release_id,
            'protocol_version': RELEASE.protocol_version,
            'desktop_camera_enabled': bool(self.hooks.visual().challenge),
            'desktop_snapshot_active': self.hooks.visual().snapshot_job is not None,
            'noise_profile': 'moderate',
            'speech_energy_floor_dbfs': NearSpeechDetector.MIN_DBFS,
            'speech_noise_margin_db': NearSpeechDetector.NOISE_MARGIN_DB,
            'speech_onset_ms': VoiceDetector.ONSET_FRAMES * 10,
            'barge_confirm_min_ms': NearSpeechDetector.CONFIRM_MIN_FRAMES * 10,
            'vision_active': self.hooks.visual().generation is not None,
            'vision_enabled': isinstance(self.hooks.brain(), BrainRouter),
            'transport': state.transport_kind,
            **self.hooks.transport_diagnostics(),
            'screen_sleeping': state.screen_sleeping,
            'idle_sleep_seconds': 900,
            'asr_engine': getattr(getattr(self.hooks.speech(), 'recognizer', None), 'engine', None),
            'barge_in_enabled': self.hooks.playback().device_can_pause,
            'playback_paused': self.hooks.playback().pause_state is not None,
            'announcement_active': self.hooks.announcements().generation is not None,
            'brain_provider': getattr(self.hooks.brain(), 'selected', 'minimax')
            if self.hooks.brain()
            else 'echo test',
            'dot_enabled': isinstance(self.hooks.brain(), BrainRouter),
            'web_search_enabled': self.hooks.brain() is not None,
            'fast_stop_enabled': self.hooks.capture().endpoint.fast_stop is not None,
            'fast_stop_input': 'AEC3 without additional NS',
            'fast_stop_gain': getattr(self.hooks.capture().endpoint.fast_stop, 'gain', None),
            'aec_engine': 'WebRTC AEC3 + NS; AGC disabled',
            'render_asr_comparison': True,
            'residual_echo_suppression': True,
            'echo_check_pending': self.hooks.calibration().pending,
            'echo_check_failed': self.hooks.calibration().failed,
            'speech_voice': getattr(getattr(self.hooks.speech(), 'voice', None), 'voice', None),
            'speech_speed': getattr(getattr(self.hooks.speech(), 'voice', None), 'speed', None),
            'speech_pitch': getattr(getattr(self.hooks.speech(), 'voice', None), 'pitch', None),
            'speech_emotion': getattr(getattr(self.hooks.speech(), 'voice', None), 'emotion', None),
            **self.hooks.metrics(),
        }
        with self.status_lock:
            temporary = private / 'status.tmp'
            temporary.write_text(json.dumps(data, indent=2) + '\n')
            temporary.replace(private / 'status.json')

    def failure(self, message, error, trace_key=None):
        state = self.hooks.session()
        key = state.generation if trace_key is None else trace_key
        stage = (
            error.stage
            if isinstance(error, OperationFailure)
            else self.hooks.diagnostics().current_stage(key, default='runtime')
        )
        self.hooks.diagnostics().failure(key, stage, error)
        record = self.hooks.diagnostics().failure_record(key)
        # Only stable categories reach UI/logs, never provider text or URLs.
        guidance = feedback(
            record['stage'] if record else stage,
            record['kind'] if record else error_kind(error),
            unconfirmed=isinstance(error, UnconfirmedTurnStart),
        )
        self.hooks.log(
            'runtime_error',
            error_type=type(error).__name__,
            stage=guidance.stage,
            kind=guidance.kind,
        )
        try:
            self.hooks.status('error', guidance.message)
        except (OSError, EOFError):
            pass
