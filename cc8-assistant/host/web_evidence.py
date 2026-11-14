"""Bounded per-turn web-operation receipts, distinct from model-written links.

App Server webSearch items expose an action, not fetched page contents or
publication timestamps. A completed page action must never be presented as
independent proof that a cited claim or the page body was verified.
"""

from web_search import now, public_url, source_key
from source_page import FAILURE_CODES, MAX_TEXT, iso_date_label, visible_text
import threading
import json
import re

MAX_ACTIONS = 64


def publication_labels(values):
    if not isinstance(values, list) or len(values) > 8:
        return ''
    valid = (
        visible_text(value)
        for value in values
        if isinstance(value, str)
        and 1 <= len(value) <= 64
        and not re.search(r'[\ud800-\udfff]', value)
    )
    return '；'.join(dict.fromkeys(label for label in valid if label))


class WebEvidence:
    def __init__(self, clock=now):
        self.clock = clock
        self.actions = {}
        self.pages = []
        self.page_lock = threading.Lock()

    def page(self, receipt):
        """Accept only the local public-reader result, never model-written fields."""
        if not isinstance(receipt, dict) or (url := public_url(receipt.get('url'))) is None:
            return
        with self.page_lock:
            if len(self.pages) >= 4:
                return
            observed = receipt.get('observed_at')
            observed = (
                observed
                if isinstance(observed, str)
                and re.fullmatch(
                    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+08:00', observed
                )
                and iso_date_label(observed)
                else ''
            )
            row = {'url': url, 'observed_at': observed, 'ok': False}
            if receipt.get('ok') is True:
                text = receipt.get('text')
                size = receipt.get('received_bytes')
                final = public_url(receipt.get('final_url'))
                if (
                    not isinstance(text, str)
                    or not 1 <= len(text) <= MAX_TEXT
                    or re.search(r'[\ud800-\udfff]', text)
                    or type(size) is not int
                    or not 1 <= size <= 1048576
                    or final is None
                    or type(receipt.get('http_status')) is not int
                    or receipt.get('http_status') != 200
                    or receipt.get('facts_verified') is not False
                    or receipt.get('untrusted_page') is not True
                ):
                    return
                normalized = visible_text(text)
                if not normalized:
                    return
                truncated = receipt.get('text_truncated')
                if type(truncated) is not bool or (truncated and len(text) != MAX_TEXT):
                    truncated = None
                updated = receipt.get('modification_labels')
                if not isinstance(updated, list) or len(updated) > 8:
                    updated = []
                row.update(
                    ok=True,
                    final_url=final,
                    excerpt=normalized[:2000],
                    text_chars=len(text),
                    text_truncated=truncated,
                    dates=publication_labels(receipt.get('publication_labels')),
                    updated_dates='；'.join(dict.fromkeys(v for v in updated if iso_date_label(v))),
                    size=size,
                )
            elif receipt.get('ok') is not False:
                return
            else:
                error, status = receipt.get('error'), receipt.get('http_status')
                row['final_url'] = public_url(receipt.get('final_url')) or ''
                row['error_code'] = (
                    error if isinstance(error, str) and error in FAILURE_CODES else ''
                )
                row['http_status'] = (
                    status if type(status) is int and 100 <= status <= 599 else None
                )
            self.pages.append(row)

    def completed(self, item):
        """Only call for an owned item/completed notification, never a delta."""
        if not isinstance(item, dict) or item.get('type') != 'webSearch':
            return
        ident = item.get('id')
        action = item.get('action')
        if (
            not isinstance(ident, str)
            or not 1 <= len(ident) <= 160
            or any(ord(c) < 32 for c in ident)
            or ident in self.actions
            or len(self.actions) >= MAX_ACTIONS
            or not isinstance(action, dict)
            or action.get('type') not in ('openPage', 'findInPage')
        ):
            return
        url = public_url(action.get('url'))
        if url is None:
            return
        status = item.get('status')
        if status not in (None, 'completed', 'failed', 'declined', 'interrupted'):
            return
        self.actions[ident] = {
            'url': url,
            'access_evidence': 'page_action_completed'
            if status in (None, 'completed')
            else 'page_action_failed',
            'observed_at': self.clock(),
        }

    def sources(self, links):
        """Keep answer links useful without promoting them to retrieved evidence."""
        result = []
        seen = set()
        if not isinstance(links, list):
            return result
        with self.page_lock:
            pages = tuple(self.pages)
        reads_by_url = {}
        for index, row in enumerate(pages):
            reads_by_url[source_key(row['url'])] = index
            if row.get('final_url'):
                reads_by_url[source_key(row['final_url'])] = index
        shown_reads = set()
        actions_by_url = {source_key(row['url']): row for row in self.actions.values()}
        for link in links[:30]:
            if not isinstance(link, dict):
                continue
            url = public_url(link.get('url'))
            if url is None:
                continue
            key = source_key(url)
            read_index = reads_by_url.get(key)
            if key in seen or read_index in shown_reads:
                continue
            title = link.get('title')
            if (
                not isinstance(title, str)
                or not 1 <= len(title) <= 160
                or re.search(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff]', title)
            ):
                continue
            seen.add(key)
            # A later failed access supersedes an earlier completed action for
            # this URL. Neither state establishes article or event dates.
            receipt = actions_by_url.get(key)
            result.append(
                {
                    'title': title,
                    'url': url,
                    'access_evidence': receipt['access_evidence']
                    if receipt
                    else 'answer_link_only',
                    'observed_at': receipt['observed_at'] if receipt else '',
                    'published_at': '',
                    'event_at': '',
                    'body_verified': False,
                }
            )
            if read_index is not None:
                # Two citations backed by the same attempt share a card.
                # Separate attempts can still report different outcomes.
                shown_reads.add(read_index)
                read = pages[read_index]
                result[-1].update(
                    access_evidence='page_body_received' if read['ok'] else 'page_fetch_failed',
                    observed_at=read['observed_at'],
                    page_excerpt=read.get('excerpt', ''),
                    page_dates=read.get('dates', ''),
                    page_modified_dates=read.get('updated_dates', ''),
                    page_bytes=read.get('size', 0),
                    final_url=read.get('final_url', ''),
                    page_text_chars=read.get('text_chars'),
                    page_text_truncated=read.get('text_truncated'),
                    page_error_code=read.get('error_code', ''),
                    page_http_status=200 if read['ok'] else read.get('http_status'),
                )
            if len(result) == 6:
                break

        # A source event shares the 64KiB authenticated packet budget. Preserve
        # URLs/receipt truth first; omit optional detail explicitly when needed.
        def size():
            return len(json.dumps(result, ensure_ascii=False).encode('utf-8'))

        for key in ('page_excerpt', 'page_modified_dates', 'page_dates', 'final_url'):
            if size() <= 56000:
                break
            for row in reversed(result):
                if row.pop(key, None):
                    row['page_details_limited'] = True
                if size() <= 56000:
                    break
        while size() > 56000 and result:
            result.pop()
            if result:
                result[-1]['list_limited'] = True
        return result
