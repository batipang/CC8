"""Revision-bound small views; never send an entire meeting in one device frame.

Offsets are opaque Python Unicode-scalar cursors returned by the server. Android
must echo next_offset, not compute it from its UTF-16 String.length(). Callers
hold the meeting owner lock; this module creates no transport, file or worker.
"""

from meeting_session import MeetingSession, integer, token, MAX_ROWS

INDEX_ROWS = 12
TEXT_CHARS = 1024
MAX_REVISION = 2**63 - 1


def checked(model, ident, revision):
    if (
        not isinstance(model, MeetingSession)
        or not token(ident)
        or model.ident != ident
        or model.state == 'deleted'
        or not integer(revision, MAX_REVISION)
        or model.revision != revision
    ):
        raise ValueError('Meeting changed; refresh its current view')


def status(model):
    if not isinstance(model, MeetingSession) or model.state == 'deleted':
        return {'available': False}
    return dict(
        available=True,
        id=model.ident,
        revision=model.revision,
        state=model.state,
        reason=model.reason,
        position_samples=model.position,
        received_samples=model.received_samples,
        row_count=len(model.rows),
        pending_count=sum(row['state'] == 'pending' for row in model.rows),
        gap_count=sum(row['state'] in ('gap', 'unconfirmed', 'too_short') for row in model.rows),
        raw_audio_saved=False,
        processing_complete=model.state == 'ended'
        and not model.reason
        and bool(model.rows)
        and all(row['state'] in ('recognized', 'empty') for row in model.rows),
    )


def row_info(row):
    return dict(
        id=row['id'],
        start_sample=row['start_sample'],
        end_sample=row['end_sample'],
        state=row['state'],
        reason=row['reason'],
        edited=row['edited'],
        text_length=len(row['text']),
        original_length=len(row['original']),
        alignment=row['metadata'],
        span_count=len(row['spans']),
    )


def index_page(model, ident, revision, offset):
    checked(model, ident, revision)
    if not integer(offset, MAX_ROWS) or offset > len(model.rows):
        raise ValueError('Invalid meeting index cursor')
    rows = model.rows[offset : offset + INDEX_ROWS]
    end = offset + len(rows)
    return dict(
        status(model),
        offset=offset,
        rows=[row_info(row) for row in rows],
        next_offset=end if end < len(model.rows) else None,
    )


def text_page(model, ident, revision, row_id, offset, *, original=False):
    checked(model, ident, revision)
    if not token(row_id) or type(original) is not bool or not integer(offset, 12000):
        raise ValueError('Invalid meeting text cursor')
    row = next((row for row in model.rows if row['id'] == row_id), None)
    if row is None:
        raise ValueError('Meeting row unavailable')
    content = row['original'] if original else row['text']
    if offset > len(content):
        raise ValueError('Meeting text cursor exceeds length')
    end = min(offset + TEXT_CHARS, len(content))
    return dict(
        id=model.ident,
        revision=model.revision,
        row=row_info(row),
        original=original,
        offset=offset,
        text=content[offset:end],
        next_offset=end if end < len(content) else None,
    )


def validate_request(value):
    """Validate before controller ownership or side effects; no implicit consent."""
    if type(value) is not dict or value.get('event') != 'meeting_request':
        raise ValueError('Invalid meeting request')
    if not token(value.get('request_id')):
        raise ValueError('Invalid meeting request identity')
    action = value.get('action')
    if type(action) is not str:
        raise ValueError('Invalid meeting action')
    fields = {
        'status': set(),
        'prepare': set(),
        'start': {'offer_id', 'confirmed', 'save_audio'},
        'prepare_resume': {'id', 'revision'},
        'resume': {'id', 'offer_id', 'confirmed'},
        # Safety actions intentionally do not depend on ASR-changing revisions.
        'pause': {'id'},
        'end': {'id'},
        'index': {'id', 'revision', 'offset'},
        'text': {'id', 'revision', 'row_id', 'offset', 'original'},
        'edit': {'id', 'revision', 'row_id', 'text', 'confirmed'},
        'delete': {'id', 'revision', 'confirmed'},
        'export': {'id', 'revision', 'format', 'confirmed'},
        'summarize': {'id', 'revision', 'confirmed'},
        'translate': {'id', 'revision', 'language', 'confirmed'},
    }
    if action not in fields or set(value) != {'event', 'request_id', 'action'} | fields[action]:
        raise ValueError('Unexpected meeting fields')
    for key in ('id', 'offer_id', 'row_id'):
        if key in value and not token(value[key]):
            raise ValueError('Invalid meeting identity')
    if 'revision' in value and not integer(value['revision'], MAX_REVISION):
        raise ValueError('Invalid meeting revision')
    if 'confirmed' in value and value['confirmed'] is not True:
        raise ValueError('Explicit meeting confirmation required')
    for key in ('save_audio', 'original'):
        if key in value and type(value[key]) is not bool:
            raise ValueError('Invalid meeting option')
    if 'offset' in value and not integer(value['offset'], MAX_ROWS if action == 'index' else 12000):
        raise ValueError('Invalid meeting page cursor')
    if action == 'edit':
        from meeting_asr import text_value

        text_value(value['text'])
    if action == 'export' and value['format'] not in ('txt', 'json'):
        raise ValueError('Unsupported meeting export')
    if action == 'translate' and value['language'] not in ('zh', 'en'):
        raise ValueError('Unsupported meeting translation language')
