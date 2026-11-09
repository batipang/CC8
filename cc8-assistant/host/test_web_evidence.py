"""No model citation, URL date or incomplete web action can invent verification."""

import unittest

from web_evidence import WebEvidence
from dot_client import DotBrain
from test_dot_client import FakeRpc


class WebEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.time = '2026-11-09T20:00:00+08:00'
        self.evidence = WebEvidence(lambda: self.time)
        self.url = 'https://example.com/2026/11/01/news'

    def action(self, ident='one', status=None, url=None, **extra):
        return dict(
            type='webSearch',
            id=ident,
            status=status,
            action=dict(type='openPage', url=url or self.url),
            **extra,
        )

    def sources(self, **fields):
        return self.evidence.sources([dict(title='原始链接', url=self.url, **fields)])

    def test_answer_url_and_claimed_date_are_not_retrieval_or_body_evidence(self):
        row = self.sources(published_at='2026-11-01', body_verified=True)[0]
        self.assertEqual(row['access_evidence'], 'answer_link_only')
        self.assertEqual((row['published_at'], row['event_at'], row['observed_at']), ('', '', ''))
        self.assertIs(row['body_verified'], False)

    def test_completed_page_action_has_receipt_time_without_inventing_body_or_dates(self):
        self.evidence.completed(self.action())
        row = self.sources()[0]
        self.assertEqual(row['access_evidence'], 'page_action_completed')
        self.assertEqual(row['observed_at'], self.time)
        self.assertIs(row['body_verified'], False)
        self.assertEqual(row['published_at'], '')

    def test_duplicate_receipt_does_not_refresh_time_or_rewrite_another_url(self):
        self.evidence.completed(self.action())
        self.time = '2026-11-09T20:10:00+08:00'
        self.evidence.completed(self.action(url='https://example.org/other'))
        self.assertEqual(self.sources()[0]['observed_at'], '2026-11-09T20:00:00+08:00')

    def test_later_failure_cannot_keep_a_successful_access_label(self):
        self.evidence.completed(self.action())
        self.evidence.completed(self.action('two', 'failed'))
        self.assertEqual(self.sources()[0]['access_evidence'], 'page_action_failed')
        self.assertIs(self.sources()[0]['body_verified'], False)

    def test_unsupported_private_and_nonterminal_items_provide_no_receipt(self):
        for row in (
            self.action(status='inProgress'),
            self.action(url='https://127.0.0.1/a'),
            dict(type='webSearch', id='search', action=dict(type='search', query=self.url)),
            dict(type='agentMessage', id='answer', text=self.url),
            self.action(ident=''),
            self.action(ident=1),
        ):
            self.evidence.completed(row)
        self.assertEqual(self.evidence.actions, {})
        self.assertEqual(self.sources()[0]['access_evidence'], 'answer_link_only')

    def test_receipts_and_source_list_are_bounded_and_do_not_implicitly_fetch_pages(self):
        for i in range(100):
            self.evidence.completed(self.action(str(i), url=f'https://example.com/{i}'))
        self.assertEqual(len(self.evidence.actions), 64)
        self.assertEqual(self.evidence.sources(None), [])
        self.assertEqual(
            len(
                self.evidence.sources(
                    [dict(title=f'链接{i}', url=f'https://example.com/{i}') for i in range(100)]
                )
            ),
            6,
        )


class DotWebEvidenceTests(unittest.TestCase):
    def test_only_owned_completed_page_operations_can_annotate_answer_links(self):
        for method, thread, turn, status, expected in (
            ('item/completed', 'voice-thread', 'turn-1', None, 'page_action_completed'),
            ('item/completed', 'voice-thread', 'turn-1', 'failed', 'page_action_failed'),
            ('item/started', 'voice-thread', 'turn-1', None, 'answer_link_only'),
            ('item/completed', 'other-thread', 'turn-1', None, 'answer_link_only'),
            ('item/completed', 'voice-thread', 'other-turn', None, 'answer_link_only'),
        ):
            with self.subTest(method=method, thread=thread, turn=turn, status=status):
                rpc = FakeRpc(text='来源：[原文](https://example.com/news)。')
                rpc.before_ack = lambda: rpc.events.put(
                    dict(
                        method=method,
                        params=dict(
                            threadId=thread,
                            turnId=turn,
                            item=dict(
                                type='webSearch',
                                id='page',
                                status=status,
                                action=dict(type='openPage', url='https://example.com/news'),
                            ),
                        ),
                    )
                )
                updates = []
                list(
                    DotBrain(rpc).clauses(
                        '查询新闻', [], on_web=lambda k, p: updates.append((k, p))
                    )
                )
                sources = [p['items'] for k, p in updates if k == 'sources']
                if expected == 'answer_link_only':
                    self.assertEqual(sources, [])
                else:
                    self.assertEqual(sources[0][0]['access_evidence'], expected)
                    self.assertIs(sources[0][0]['body_verified'], False)

    def test_prior_turn_page_access_does_not_verify_a_later_answer_link(self):
        rpc = FakeRpc(text='来源：[原文](https://example.com/news)。')
        brain = DotBrain(rpc)
        rpc.before_ack = lambda: rpc.events.put(
            dict(
                method='item/completed',
                params=dict(
                    threadId='voice-thread',
                    turnId='turn-1',
                    item=dict(
                        type='webSearch',
                        id='page',
                        action=dict(type='openPage', url='https://example.com/news'),
                    ),
                ),
            )
        )
        list(brain.clauses('第一次查询', []))
        rpc.before_ack = lambda: rpc.events.put(
            dict(
                method='item/completed',
                params=dict(
                    threadId='voice-thread',
                    turnId='turn-2',
                    item=dict(
                        type='webSearch', id='search', action=dict(type='search', query='新闻')
                    ),
                ),
            )
        )
        updates = []
        list(brain.clauses('第二次查询', [], on_web=lambda k, p: updates.append((k, p))))
        row = next(p['items'][0] for k, p in updates if k == 'sources')
        self.assertEqual(row['access_evidence'], 'answer_link_only')
        self.assertEqual(row['observed_at'], '')


if __name__ == '__main__':
    unittest.main()
