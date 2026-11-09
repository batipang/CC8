"""Typed preference request shape and text bounds, independent of filesystem/OS ports."""

import re
import unicodedata

REVISION_LIMIT = 2**31 - 2


def field(value: object, maximum: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError('Preference text required')
    value = unicodedata.normalize('NFC', value).strip()
    if (
        not value
        or len(value) > maximum
        or any(
            unicodedata.category(c).startswith('C') and not (multiline and c == '\n') for c in value
        )
    ):
        raise ValueError('Invalid preference text')
    value.encode('utf-8')
    return value


def validate_request(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError('Invalid preference request')
    action = value.get('action')
    shapes = {
        'list': set(),
        'put': {'revision', 'key', 'value'},
        'delete': {'revision', 'key'},
        'clear': {'revision'},
        'set_enabled': {'revision', 'enabled'},
    }
    if (
        value.get('event') != 'preference_request'
        or not isinstance(action, str)
        or action not in shapes
        or set(value) != {'event', 'request_id', 'action'} | shapes[action]
        or not isinstance(value.get('request_id'), str)
        or not re.fullmatch('[a-f0-9]{32}', value['request_id'])
    ):
        raise ValueError('Invalid preference request')
    if action != 'list' and (
        type(value['revision']) is not int or not 0 <= value['revision'] < REVISION_LIMIT
    ):
        raise ValueError('Invalid preference revision')
    if action in ('put', 'delete'):
        field(value['key'], 40)
    if action == 'put':
        field(value['value'], 200, multiline=True)
    if action == 'set_enabled' and type(value['enabled']) is not bool:
        raise ValueError('Invalid preference switch')
