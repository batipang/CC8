"""Bounded source metadata without the captured image or user question."""

from typing import TypedDict, cast


class WindowReceipt(TypedDict):
    window_id: int
    application: str
    title: str
    width: int
    height: int


def validate_window_receipt(value: object) -> WindowReceipt:
    if not isinstance(value, dict) or set(value) != set(WindowReceipt.__annotations__):
        raise ValueError('Invalid window receipt fields')
    for key, maximum in (('window_id', 2**32 - 1), ('width', 1280), ('height', 1280)):
        if type(value[key]) is not int or not 0 < value[key] <= maximum:
            raise ValueError('Invalid window identity or geometry')
    if value['width'] * value['height'] > 1280 * 960:
        raise ValueError('Invalid window receipt area')
    for key, maximum in (('application', 100), ('title', 240)):
        label = value[key]
        if (
            not isinstance(label, str)
            or len(label) > maximum
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in label)
        ):
            raise ValueError('Invalid window label')
    return cast(WindowReceipt, dict(value))
