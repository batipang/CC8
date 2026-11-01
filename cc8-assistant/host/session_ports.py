"""Narrow callback types shared by lifecycle components, without their state."""

from typing import Protocol
from protocol import JsonValue


class EventSink(Protocol):
    def __call__(self, event: str, **fields: JsonValue) -> None: ...


class AnswerSink(Protocol):
    def __call__(self, text: str, generation: int, *, image: bytes) -> None: ...


class MetricSink(Protocol):
    def __call__(self, name: str, value: object = None, *, increment: bool = False) -> None: ...
