"""In-memory work items and presentation mood; no worker/session ownership."""

from dataclasses import dataclass


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
class VisualReply(Reply):
    origin: str = 'voice'
    nonce: str = ''


@dataclass
class TextReply(Reply):
    pass
