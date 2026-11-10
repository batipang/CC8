"""Pure, typed meeting controls and bounds; no model, transport, file or worker imports."""

import re

MAX_ROWS = 256
MAX_TEXT = 12000
MAX_REVISION = 2**63 - 1
EXPORT_NAME = re.compile(r'([a-f0-9]{32})_r(0|[1-9][0-9]{0,18})\.(txt|json)')
PENDING_EXPORT = re.compile(r'pending-[a-f0-9]{32}')


def export_name(value: object) -> bool:
    if type(value) is not str:
        return False
    match = EXPORT_NAME.fullmatch(value)
    return bool(match and int(match[2]) <= MAX_REVISION or PENDING_EXPORT.fullmatch(value))


def sha256(value: object) -> bool:
    return type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def token(value: object) -> bool:
    return type(value) is str and re.fullmatch(r'[a-f0-9]{32}', value) is not None


def integer(value: object, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def text_value(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_TEXT
        or any(ord(c) < 32 and c not in '\n\r\t' for c in value)
    ):
        raise ValueError('Invalid meeting transcript text')
    if len(value.encode('utf-8')) > MAX_TEXT * 4:
        raise ValueError('Meeting transcript text exceeds budget')
    return value.strip()


def validate_request(value: object) -> None:
    """Validate before controller ownership or side effects; no implicit consent."""
    if not isinstance(value, dict) or value.get('event') != 'meeting_request':
        raise ValueError('Invalid meeting request')
    if not token(value.get('request_id')):
        raise ValueError('Invalid meeting request identity')
    action = value.get('action')
    if not isinstance(action, str):
        raise ValueError('Invalid meeting action')
    fields: dict[str, set[str]] = {
        'status': set(),
        'prepare': set(),
        'start': {'offer_id', 'confirmed', 'save_audio'},
        'prepare_resume': {'id', 'revision'},
        'resume': {'id', 'offer_id', 'confirmed'},
        # Safety actions intentionally do not depend on ASR-changing revisions.
        'pause': {'id'},
        'end': {'id'},
        'capture_failed': {'id', 'nonce', 'reason'},
        'abort_start': {'start_request_id'},
        'index': {'id', 'revision', 'offset'},
        'text': {'id', 'revision', 'row_id', 'offset', 'original'},
        'edit': {'id', 'revision', 'row_id', 'text', 'confirmed'},
        'delete': {'id', 'revision', 'confirmed'},
        'export': {'id', 'revision', 'format', 'confirmed'},
        'exports': set(),
        'delete_export': {'export_id', 'sha256', 'confirmed'},
        'summarize': {'id', 'revision', 'confirmed'},
        'translate': {'id', 'revision', 'language', 'confirmed'},
        'cancel_analysis': {'id', 'analysis_id'},
        'analysis_text': {'id', 'analysis_id', 'offset'},
    }
    if action not in fields or set(value) != {'event', 'request_id', 'action'} | fields[action]:
        raise ValueError('Unexpected meeting fields')
    for key in ('id', 'offer_id', 'row_id', 'nonce', 'start_request_id', 'analysis_id'):
        if key in value and not token(value[key]):
            raise ValueError('Invalid meeting identity')
    if 'revision' in value and not integer(value['revision'], MAX_REVISION):
        raise ValueError('Invalid meeting revision')
    if 'confirmed' in value and value['confirmed'] is not True:
        raise ValueError('Explicit meeting confirmation required')
    for key in ('save_audio', 'original'):
        if key in value and type(value[key]) is not bool:
            raise ValueError('Invalid meeting option')
    if 'offset' in value and not integer(
        value['offset'],
        MAX_ROWS if action == 'index' else 512 * 1024 if action == 'analysis_text' else 12000,
    ):
        raise ValueError('Invalid meeting page cursor')
    if action == 'edit':
        text_value(value['text'])
    if action == 'export' and value['format'] not in ('txt', 'json'):
        raise ValueError('Unsupported meeting export')
    if action == 'delete_export' and (
        not export_name(value['export_id']) or not sha256(value['sha256'])
    ):
        raise ValueError('Invalid meeting export receipt')
    if action == 'translate' and value['language'] not in ('zh', 'en'):
        raise ValueError('Unsupported meeting translation language')
    if action == 'capture_failed' and value['reason'] not in (
        'capture_failed',
        'credential_or_permission',
    ):
        raise ValueError('Invalid meeting capture failure')
