"""In-memory work items and presentation mood; no worker/session ownership."""

from dataclasses import dataclass, field
from typing import Callable


def mood(text):
    if any(w in text for w in ['难过', '担心', '不开心', '烦恼', '辛苦', '害怕', '抱歉']):
        return 'empathetic'
    if any(w in text for w in ['放松', '呼吸', '慢慢', '平静', '休息']):
        return 'calm'
    if any(w in text for w in ['开心', '高兴', '太好了', '成功', '喜欢', '谢谢']):
        return 'happy'
    return 'friendly'


@dataclass
class Reply:
    generation: int
    text: str


@dataclass
class LiveReply(Reply):
    current: Callable[[], bool] = field(repr=False)
    deliver: Callable[[str], None] = field(repr=False)


@dataclass
class VisualReply(Reply):
    origin: str = 'voice'
    nonce: str = ''


@dataclass
class PhotoReply(VisualReply):
    """An explicit, already-consumed photo question; no camera acquisition."""

    image: bytes | None = field(default=None, repr=False)
    policy: object = None
    expires: float = 0


@dataclass
class TextReply(Reply):
    pass


@dataclass
class CorrectedReply(TextReply):
    """An explicitly confirmed replacement, never a replay of the original task."""


@dataclass
class WorkflowReply(TextReply):
    """A frozen, explicitly confirmed workflow in the existing serial queue."""

    run: object = field(default=None, repr=False)


@dataclass
class MeetingReply(TextReply):
    """A complete frozen document, never a clipped voice utterance."""

    run: object = field(default=None, repr=False)


@dataclass
class ResultRead(TextReply):
    result_id: str = ''
    restart: bool = False


@dataclass(frozen=True)
class BrainPreparation:
    """Connection-only work in the existing serial response queue."""

    generation: int
