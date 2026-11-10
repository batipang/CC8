"""Revision-bound small views; never send an entire meeting in one device frame.

Offsets are opaque Python Unicode-scalar cursors returned by the server. Android
must echo next_offset, not compute it from its UTF-16 String.length(). Callers
hold the meeting owner lock; this module creates no transport, file or worker.
"""

from meeting_session import MeetingSession
from meeting_protocol import MAX_REVISION, MAX_ROWS, token, integer

INDEX_ROWS = 12
TEXT_CHARS = 1024


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
