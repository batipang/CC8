"""No model citation, URL date or incomplete web action can invent verification."""

import unittest
import json

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

    def page(self, **extra):
        return dict(
            ok=True,
            url=self.url,
            final_url=self.url,
            text='Actual source text',
            publication_labels=['2026-11-10'],
            received_bytes=123,
            http_status=200,
            facts_verified=False,
            untrusted_page=True,
            **extra,
        )

    def test_actual_reader_receipt_exposes_text_and_labels_without_verifying_facts(self):
        receipt = self.page()
        self.evidence.page(receipt)
        receipt['text'] = 'Mutated after receipt'
        receipt['publication_labels'].append('mutated')
        row = self.sources()[0]
        self.assertEqual(row['access_evidence'], 'page_body_received')
        self.assertEqual(row['page_excerpt'], 'Actual source text')
        self.assertEqual(row['page_dates'], '2026-11-10')
        self.assertEqual(row['published_at'], '')
        self.assertIs(row['body_verified'], False)
        self.assertEqual(row['event_at'], '')

    def test_redirect_final_citation_is_bound_to_the_original_real_read(self):
        receipt = self.page()
        receipt['final_url'] = 'https://other.example/final'
        self.evidence.page(receipt)
        row = self.evidence.sources([dict(title='Final', url=receipt['final_url'])])[0]
        self.assertEqual(row['access_evidence'], 'page_body_received')

    def test_model_links_and_malformed_reader_data_cannot_manufacture_body_evidence(self):
        for field, value in [
            ('facts_verified', True),
            ('received_bytes', True),
            ('received_bytes', 1048577),
            ('http_status', 302),
            ('http_status', 200.0),
            ('final_url', 'http://127.0.0.1'),
            ('text', ''),
            ('publication_labels', [5]),
        ]:
            receipt = self.page()
            receipt[field] = value
            self.evidence.page(receipt)
        self.assertEqual(self.sources()[0]['access_evidence'], 'answer_link_only')

    def test_later_failed_read_does_not_keep_the_old_success_label(self):
        self.evidence.page(self.page())
        self.evidence.page(dict(ok=False, url=self.url, error='http_error'))
        row = self.sources()[0]
        self.assertEqual(row['access_evidence'], 'page_fetch_failed')
        self.assertEqual(row['page_excerpt'], '')
        self.assertIsNone(row['page_text_chars'])
        self.assertIsNone(row['page_text_truncated'])

    def test_received_text_coverage_and_excerpt_limits_remain_distinct(self):
        text = '\U0001f600' * 20000
        receipt = self.page()
        receipt.update(text=text, text_truncated=True)
        self.evidence.page(receipt)
        receipt['text_truncated'] = False
        row = self.sources()[0]
        self.assertEqual(row['page_text_chars'], 20000)
        self.assertEqual(len(row['page_excerpt']), 2000)
        self.assertIs(row['page_text_truncated'], True)
        self.assertEqual(row['page_http_status'], 200)
        self.assertIs(row['body_verified'], False)

    def test_legacy_or_malformed_truncation_is_unknown_without_losing_real_text(self):
        for value in (None, 'false', 0, True):
            evidence = WebEvidence()
            receipt = self.page()
            receipt.update(text=' small\x00 text ', text_truncated=value)
            evidence.page(receipt)
            row = evidence.sources([{'title': 'source', 'url': self.url}])[0]
            self.assertEqual(row['page_excerpt'], 'small text')
            self.assertEqual(row['page_text_chars'], 13)
            self.assertEqual(row['access_evidence'], 'page_body_received')
            self.assertIsNone(row['page_text_truncated'])
        evidence = WebEvidence()
        receipt['text_truncated'] = False
        evidence.page(receipt)
        self.assertIs(
            evidence.sources([{'title': 'source', 'url': self.url}])[0]['page_text_truncated'],
            False,
        )

    def test_failed_reads_keep_only_fixed_error_codes_and_valid_terminal_status(self):
        for error, status, code, expected_status in (
            ('http_error', 404, 'http_error', 404),
            ('unsupported_encoding', 200, 'unsupported_encoding', 200),
            ('timeout', None, 'timeout', None),
            ('/private/secret-key', 999, '', None),
            ('network_failed', True, 'network_failed', None),
            ('dns_failed', '404', 'dns_failed', None),
        ):
            evidence = WebEvidence()
            receipt = dict(ok=False, url=self.url, error=error, http_status=status)
            evidence.page(receipt)
            receipt['error'] = 'changed'
            row = evidence.sources([{'title': 'source', 'url': self.url}])[0]
            self.assertEqual(row['access_evidence'], 'page_fetch_failed')
            self.assertEqual(row['page_error_code'], code)
            self.assertEqual(row['page_http_status'], expected_status)
            self.assertEqual(row['page_excerpt'], '')
            self.assertNotIn('secret-key', str(row))

    def test_unicode_large_source_metadata_cannot_overflow_authenticated_packets(self):
        links = []
        for i in range(6):
            url = 'https://example.com/' + str(i) + '\U0001f600' * 2000
            links.append(dict(title='\U0001f600' * 160, url=url))
            receipt = self.page()
            receipt.update(
                url=url,
                final_url=url,
                text='\U0001f600' * 20000,
                publication_labels=['\U0001f600' * 64] * 8,
            )
            self.evidence.page(receipt)
        rows = self.evidence.sources(links)
        self.assertLessEqual(len(json.dumps(rows, ensure_ascii=False).encode()), 56000)
        self.assertTrue(any(row.get('page_details_limited') for row in rows))

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
