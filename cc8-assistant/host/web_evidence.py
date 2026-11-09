"""Bounded per-turn web-operation receipts, distinct from model-written links.

App Server webSearch items expose an action, not fetched page contents or
publication timestamps. A completed page action must never be presented as
independent proof that a cited claim or the page body was verified.
"""

from web_search import now, public_url

MAX_ACTIONS = 64


class WebEvidence:
    def __init__(self, clock=now):
        self.clock = clock
        self.actions = {}

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
        for link in links[:30]:
            if not isinstance(link, dict):
                continue
            url = public_url(link.get('url'))
            if url is None or url in seen:
                continue
            title = link.get('title')
            if not isinstance(title, str) or not 1 <= len(title) <= 160:
                continue
            seen.add(url)
            # A later failed access supersedes an earlier completed action for
            # this URL. Neither state establishes article or event dates.
            matching = [row for row in self.actions.values() if row['url'] == url]
            receipt = matching[-1] if matching else None
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
            if len(result) == 6:
                break
        return result
