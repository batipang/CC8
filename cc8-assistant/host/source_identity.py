"""Pure bounded source identifiers and display titles shared by read/result paths."""

import re


def identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value):
        raise ValueError('Invalid chat identifier')
    return value


def title(value: object) -> str:
    if not isinstance(value, str):
        return '未命名聊天'
    return ''.join(c for c in value[:160] if ord(c) >= 32)[:80].strip() or '未命名聊天'
