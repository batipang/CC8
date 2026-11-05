"""Narrow callback types shared by lifecycle components, without their state."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Protocol, TYPE_CHECKING
from protocol import JsonValue

if TYPE_CHECKING:
    from announcement_controller import AnnouncementController
    from calibration_controller import CalibrationController
    from capture_pipeline import CapturePipeline
    from diagnostics import TraceBook
    from playback_controller import PlaybackController
    from protocol import JsonObject, Packet
    from reply_types import TextReply
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
