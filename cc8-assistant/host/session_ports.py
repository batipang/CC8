"""Narrow callback types shared by lifecycle components, without their state."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, ContextManager, Mapping, Pattern, Protocol, TYPE_CHECKING
from protocol import JsonValue

if TYPE_CHECKING:
    from wake_utterance import WaitingUtterance, Samples
    from announcement_controller import AnnouncementController
    from calibration_controller import CalibrationController
    from capture_pipeline import CapturePipeline
    from diagnostics import TraceBook
    from playback_controller import PlaybackController
    from protocol import JsonObject, Packet
    from reply_types import Reply, TextReply
    from vision_controller import VisionController
    from workspace import Workspace


class EventSink(Protocol):
    def __call__(self, event: str, **fields: JsonValue) -> None: ...


class AnswerSink(Protocol):
    def __call__(self, text: str, generation: int, *, image: bytes) -> None: ...


class MetricSink(Protocol):
    def __call__(self, name: str, value: object = None, *, increment: bool = False) -> None: ...


@dataclass(frozen=True)
class SessionView:
    connected: bool
    active: bool
    generation: int
    transport_kind: str
    wake: bool
    screen_sleeping: bool
    awake: bool
    capture_frontend: str = 'mono'
    text_request_active: bool = False


@dataclass(frozen=True)
class SessionHooks:
    diagnostics: Callable[[], TraceBook]
    workspace: Callable[[], Workspace]
    playback: Callable[[], PlaybackController]
    capture: Callable[[], CapturePipeline]
    visual: Callable[[], VisionController]
    calibration: Callable[[], CalibrationController]
    announcements: Callable[[], AnnouncementController]
    brain: Callable[[], object]
    speech: Callable[[], object]
    voice_busy: Callable[[], bool]
    send: Callable[[Packet], None]
    event: EventSink
    status: Callable[[str, str], None]
    valid: Callable[[int], bool]
    cancel: Callable[[str], int]
    stop: Callable[[], None]
    touch_activity: Callable[[], None]
    reminder_tick: Callable[[], None]
    save_status: Callable[[], None]
    publish_workspace: Callable[[], None]
    enqueue_response: Callable[[TextReply], None]
    route_event: Callable[[JsonObject], None]
    calibrate: Callable[[], Callable[[int], None]]
    log: EventSink
    metric: MetricSink
    metric_value: Callable[[str], int]
    wake_reset: Callable[[str], None]
    wake_touch: Callable[[], None]
    awake_changed: Callable[[bool], None]
    wake_tick: Callable[[], None]
    prepare_brain: Callable[[int], None]
    cancel_observation: Callable[[], None] = lambda: None


class FailureSink(Protocol):
    def __call__(
        self, message: str, error: BaseException, *, trace_key: str | int | None = None
    ) -> None: ...


class MetricFieldSink(Protocol):
    def __call__(self, name: str, field: str, value: object) -> None: ...


class PairSink(Protocol):
    def __call__(self, text: str, render_text: str, echo: bool) -> None: ...


@dataclass(frozen=True)
class RecognitionHooks:
    lock: ContextManager[object]
    valid: Callable[[int], bool]
    diagnostics: Callable[[], TraceBook]
    playing: Callable[[], bool]
    last_audio_end: Callable[[], float]
    recent_spoken: Callable[[], list[str]]
    pause_state: Callable[[], Mapping[str, object] | None]
    speech: Callable[[], object]
    metric: MetricSink
    set_metric_field: MetricFieldSink
    add_metric_field: MetricFieldSink
    record_echo_pair: PairSink
    echo_running: Callable[[], bool]
    echo_failed: Callable[[], bool]
    awake: Callable[[], bool]
    wake_allowed: Callable[[], bool]
    wake: Callable[[], bool]
    set_awake: Callable[[bool], None]
    wake_pattern: Pattern[str]
    touch: Callable[[], None]
    event: EventSink
    status: Callable[[str, str], None]
    log: EventSink
    cancel_session: Callable[[str], int]
    begin_visual: Callable[[str, str], None]
    enqueue_response: Callable[[Reply], None]
    failure: FailureSink
    resume_rejected: Callable[[int | None, int], None]
    bind_response_probe: Callable[[str, int, int | None], None] = lambda *_: None
    tool_sample_rejection: Callable[[str, bool, float | None], str | None] = lambda *_: None
    tool_sample_comparison: Callable[[str], tuple[int, str] | None] = lambda _: None
    recheck_wake: Callable[[WaitingUtterance, Samples, int], bool | None] = lambda *_: None
    tool_sample_asr_rejection: Callable[[bool, float | None], str | None] = lambda *_: None
    natural_barge_enabled: Callable[[], bool] = lambda: True
