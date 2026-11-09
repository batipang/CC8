"""Lossless report text pages and bounded legacy previews; never change server snapshots."""

from copy import deepcopy
from protocol import Packet, HEADER, MAX_PACKET

PAGE_BYTES = 8000
MAX_PAGES = 16
ROUTE_BYTES = 17


def text_pages(text):
    """Account for actual JSON escaping while preserving every original character."""
    if not isinstance(text, str):
        raise ValueError('Invalid report display text')
    parts, begin, used = [], 0, 0
    for index, char in enumerate(text):
        if char in ('"', '\\', '\b', '\f', '\n', '\r', '\t'):
            size = 2
        elif ord(char) < 32:
            size = 6
        else:
            size = len(char.encode('utf-8'))
        if used + size > PAGE_BYTES:
            parts.append(text[begin:index])
            begin, used = index, 0
        used += size
    parts.append(text[begin:])
    if len(parts) > MAX_PAGES:
        raise ValueError('Report display page capacity exceeded')
    return parts


def fits(fields, envelope):
    return (
        len(Packet.event('dot_chat_state', **envelope, **fields).data) + HEADER.size + ROUTE_BYTES
        <= MAX_PACKET
    )


def paged_text(text, envelope, *, page=0, field='report_text', fields=None):
    if field not in ('report_text', 'saved_report_text') or type(page) is not int:
        raise ValueError('Invalid report display page')
    result = dict(
        fields or {}, **{field: text}, report_page=0, report_pages=1, report_page_marker=''
    )
    pages = text_pages(text)
    if len(pages) == 1 and fits(result, envelope):
        if page != 0:
            raise ValueError('Report has no such page')
        return result
    if not 0 <= page < len(pages):
        raise ValueError('Report has no such page')
    marker = f'【第{page + 1}/{len(pages)}页；请翻页继续查看】\n\n'
    result.update(
        {field: marker + pages[page]},
        report_page=page,
        report_pages=len(pages),
        report_page_marker=marker,
    )
    if not fits(result, envelope):
        raise ValueError('Report metadata exceeds control-frame budget')
    return result


def display_fields(report, text, envelope, *, page=0):
    original = dict(
        report=deepcopy(report),
        report_text=text,
        report_page=0,
        report_pages=1,
        report_page_marker='',
    )
    if fits(original, envelope):
        return paged_text(text, envelope, page=page, fields=dict(report=deepcopy(report)))
    projected = deepcopy(report)
    # Legacy readers still receive all result identities and evidence, with
    # explicitly shortened previews. The modern reader receives all text pages.
    for item in projected['items']:
        if len(item['excerpt']) > 80:
            item['excerpt'] = item['excerpt'][:79] + '…'
            item['excerpt_truncated'] = True
            projected['display_preview_truncated'] = True
    return paged_text(text, envelope, page=page, fields=dict(report=projected))
