"""Actual public-reader parsing, pinned DNS and bounded child I/O; no internet."""

import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from source_page import (
    MAX_BYTES,
    MAX_METADATA_CHARS,
    MAX_TEXT,
    PageError,
    addresses,
    fetch,
    headers,
    read_page,
)
from web_evidence import WebEvidence


def response(body=b'<title>Article</title><p>Actual page text.</p>', status=200, **fields):
    values = {'Content-Type': 'text/html; charset=utf-8', **fields}
    raw = (
        f'HTTP/1.1 {status} Test\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in values.items()) + '\r\n'
    )
    return body, raw.encode('ascii')


class PageReaderTests(unittest.TestCase):
    def setUp(self):
        self.resolves, self.requests = [], []
        self.rows = [response()]

    def resolve(self, host, deadline, current):
        self.resolves.append(host)
        return '8.8.8.8'

    def request(self, url, address, deadline, current):
        self.requests.append((url, address, deadline))
        return self.rows.pop(0)

    def read(self, url='https://example.com/page', current=lambda: True):
        return read_page(
            url, current, resolve=self.resolve, request=self.request, clock=lambda: 'now'
        )

    def test_real_html_and_publication_labels_are_data_not_fact_verification(self):
        self.rows = [
            response(
                b'<title> Page &amp; title </title><script>ignore()</script>'
                b'<style>secret style</style><meta property="article:published_time" '
                b'content="2026-11-10"><p>First <b>actual</b> text.</p>'
            )
        ]
        value = self.read()
        self.assertTrue(value['ok'])
        self.assertEqual(value['title'], 'Page & title')
        self.assertEqual(value['publication_labels'], ['2026-11-10'])
        self.assertIn('First actual text.', value['text'])
        self.assertNotIn('ignore', value['text'])
        self.assertNotIn('secret', value['text'])
        self.assertIs(value['facts_verified'], False)
        self.assertIs(value['untrusted_page'], True)
        self.assertEqual(self.requests[0][1], '8.8.8.8')

    def test_url_and_http_last_modified_do_not_invent_a_publication_date(self):
        self.rows = [response(**{'Last-Modified': 'Tue, 10 Nov 2026 01:00:00 GMT'})]
        self.assertEqual(self.read('https://example.com/2026/11/10/page')['publication_labels'], [])

    def test_parsely_publication_label_is_distinct_from_updates_and_events(self):
        self.rows = [
            response(
                b'<meta property="og:updated_time" content="2026-05-07T11:39:50-04:00">'
                b'<meta name="parsely-pub-date" content="2026-04-11T02:31:50Z">'
                b'<meta name="parsely-pub-date" content="2026-02-30T02:31:50Z">'
                b'<time datetime="2026-04-10">Event date</time><p>Story</p>'
            )
        ]
        result = self.read()
        self.assertEqual(result['publication_labels'], ['2026-04-11T02:31:50Z'])
        evidence = WebEvidence(clock=lambda: 'now')
        evidence.page(result)
        row = evidence.sources([{'url': result['url'], 'title': 'Story'}])[0]
        self.assertEqual(row['page_dates'], '2026-04-11T02:31:50Z')
        self.assertEqual(row['event_at'], '')

    def test_page_control_characters_are_normalized_before_native_display(self):
        self.rows = [response('<p>first\x00 second\x85 third</p>'.encode())]
        self.assertEqual(self.read()['text'], 'first second third')

    def test_conflicting_publisher_labels_remain_separate(self):
        self.rows = [
            response(
                b'<meta property="article:published_time" content="A">'
                b'<meta name="datePublished" content="B"><p>text</p>'
            )
        ]
        self.assertEqual(self.read()['publication_labels'], ['A', 'B'])

    def test_duplicate_html_attributes_cannot_silently_choose_a_date(self):
        self.rows = [
            response(b'<meta property="article:published_time" content="A" content="B"><p>text</p>')
        ]
        self.assertEqual(self.read()['publication_labels'], [])

    def test_json_ld_publication_label_from_article_graph_keeps_conflicting_meta(self):
        self.rows = [
            response(
                b'<meta name="datePublished" content="2026-11-10">'
                b'<script type="application/ld+json">{"@context":"https://schema.org",'
                b'"@graph":[{"@type":"WebSite","datePublished":"2000-01-01"},'
                b'{"@type":["Article","NewsArticle"],"datePublished":"2026-11-11T07:00:00Z",'
                b'"dateModified":"2026-11-13","description":"private fixture text"}]}</script>'
                b'<p>Visible story</p>'
            )
        ]
        result = self.read()
        self.assertEqual(result['publication_labels'], ['2026-11-10', '2026-11-11T07:00:00Z'])
        self.assertEqual(result['text'], 'Visible story')
        self.assertFalse(result['facts_verified'])
        evidence = WebEvidence(clock=lambda: 'now')
        evidence.page(result)
        row = evidence.sources([{'url': result['url'], 'title': 'Story'}])[0]
        self.assertEqual(row['page_dates'], '2026-11-10；2026-11-11T07:00:00Z')
        self.assertEqual(row['event_at'], '')
        self.assertFalse(row['body_verified'])

    def test_json_ld_ignores_non_article_bad_dates_duplicate_keys_and_ambiguous_graph(self):
        base = {'@context': 'https://schema.org', '@type': 'NewsArticle'}
        for raw in (
            json.dumps({**base, '@type': 'Event', 'datePublished': '2026-11-10'}),
            json.dumps({**base, 'dateModified': '2026-11-10', 'startDate': '2026-11-12'}),
            json.dumps({**base, 'datePublished': '2026-02-30'}),
            json.dumps(
                {
                    **base,
                    '@context': {'datePublished': 'https://example.com'},
                    'datePublished': '2026-11-10',
                }
            ),
            '{"@context":"https://schema.org","@type":"Article","datePublished":"2026-11-10","datePublished":"2026-11-11"}',
            json.dumps(
                {
                    '@context': base['@context'],
                    '@graph': [
                        {'@type': 'NewsArticle', 'datePublished': '2026-11-10'},
                        {'@type': 'BlogPosting', 'datePublished': '2026-11-11'},
                    ],
                }
            ),
        ):
            with self.subTest(raw=raw):
                self.rows = [
                    response(
                        f'<script type="application/ld+json">{raw}</script><p>Story</p>'.encode()
                    )
                ]
                self.assertEqual(self.read()['publication_labels'], [])

    def test_publication_uses_only_the_article_identifying_the_final_page(self):
        original = 'https://example.com/page'
        destination = 'https://other.example/story?edition=local'
        latest = '2026-11-14T10:30:00+08:00'
        base = {'@context': 'https://schema.org', '@type': 'NewsArticle'}

        def block(**values):
            return (
                '<script type="application/ld+json">' + json.dumps({**base, **values}) + '</script>'
            ).encode()

        related = block(url=original, datePublished='2026-11-01')
        unbound = block(datePublished='2026-11-02')
        current = block(
            mainEntityOfPage={'@id': 'https://OTHER.example:443/story?edition=local#article'},
            datePublished=latest,
        )
        self.rows = [
            response(status=302, Location=destination),
            response(related + unbound + current + b'<p>Story</p>'),
        ]
        result = self.read(original)
        self.assertEqual(result['publication_labels'], [latest])
        self.assertEqual(len(self.requests), 2)
        evidence = WebEvidence(clock=lambda: 'now')
        evidence.page(result)
        for url in (original, destination):
            row = evidence.sources([{'url': url, 'title': 'Story'}])[0]
            self.assertEqual(row['page_dates'], latest)
            self.assertEqual(row['event_at'], '')

        for metadata, expected in (
            (block(url=destination, datePublished=latest) + related, [latest]),
            (current + block(url=destination, datePublished='2026-11-13'), []),
            (related, []),
            (unbound + related, []),
            (block(url=destination.replace('local', 'global'), datePublished=latest), []),
        ):
            with self.subTest(metadata=metadata):
                self.rows = [response(metadata + b'<p>Story</p>')]
                result = self.read(destination)
                self.assertEqual(result['publication_labels'], expected)

    def test_json_ld_processing_is_bounded_and_does_not_hide_page_text(self):
        block = b'<script type="application/ld+json">%s</script>'
        valid = b'{"@context":"https://schema.org","@type":"Article","datePublished":"2026-11-10"}'
        ignored = block % (b' ' * MAX_METADATA_CHARS + valid)
        self.rows = [response(ignored + block % b'{}' * 3 + block % valid + b'<p>Story</p>')]
        result = self.read()
        self.assertEqual(result['publication_labels'], [])
        self.assertEqual(result['text'], 'Story')

    def test_only_inline_json_ld_with_an_unambiguous_media_type_is_read(self):
        valid = b'{"@context":"https://schema.org","@type":"Article","datePublished":"2026-11-10"}'
        self.rows = [
            response(
                b'<script src="https://other.example/data" type="application/ld+json">'
                + valid
                + b'</script><script type="text/javascript" type="application/ld+json">'
                + valid
                + b'</script><p>Story</p>'
            )
        ]
        result = self.read()
        self.assertEqual(result['publication_labels'], [])
        self.assertEqual(result['text'], 'Story')
        self.assertEqual(self.resolves, ['example.com'])

    def test_every_redirect_is_resolved_and_pinned_with_one_shared_deadline(self):
        self.rows = [response(status=302, Location='https://other.example/final'), response()]
        value = self.read()
        self.assertTrue(value['ok'])
        self.assertEqual(self.resolves, ['example.com', 'other.example'])
        self.assertEqual(value['url'], 'https://example.com/page')
        self.assertEqual(value['final_url'], 'https://other.example/final')
        self.assertEqual(self.requests[0][2], self.requests[1][2])

    def test_unsafe_redirect_downgrade_loop_and_redirect_budget_fail(self):
        for target, expected in [
            ('http://127.0.0.1/', 'invalid_redirect'),
            ('file:///private/file', 'invalid_redirect'),
            ('http://example.com/', 'insecure_redirect'),
            ('https://example.com/page', 'redirect_loop'),
        ]:
            with self.subTest(target=target):
                self.rows = [response(status=302, Location=target)]
                self.assertEqual(self.read()['error'], expected)
        self.rows = [response(status=302, Location=f'/page{i}') for i in range(4)]
        self.assertEqual(self.read()['error'], 'redirect_limit')

    def test_redirect_dns_rebinding_to_lan_is_rejected_before_second_connection(self):
        self.rows = [response(status=302, Location='https://same.example/second')]

        def resolve(host, deadline, current):
            if host == 'same.example':
                raise PageError('non_public_address')
            return '8.8.8.8'

        value = read_page('https://example.com/page', resolve=resolve, request=self.request)
        self.assertEqual(value['error'], 'non_public_address')
        self.assertEqual(len(self.requests), 1)

    def test_http_to_https_then_back_to_http_cannot_downgrade_the_encrypted_hop(self):
        self.rows = [
            response(status=301, Location='https://example.com/secure'),
            response(status=302, Location='http://example.com/plain'),
        ]
        value = self.read('http://example.com/start')
        self.assertEqual(value['error'], 'insecure_redirect')
        self.assertEqual(len(self.requests), 2)

    def test_private_credential_file_and_invalid_links_never_start_a_request(self):
        for url in (
            'http://127.0.0.1/',
            'http://127.1/',
            'https://user:pw@example.com/',
            'file:///private/x',
            'https://example.com:22/',
            'https://machine.local/',
            None,
        ):
            with self.subTest(url=url):
                self.assertFalse(self.read(url)['ok'])
        self.assertEqual(self.requests, [])

    def test_http_failure_binary_encoding_and_empty_text_are_explicit_failures(self):
        for row, code in [
            (response(status=404), 'http_error'),
            (response(**{'Content-Type': 'application/pdf'}), 'unsupported_content_type'),
            (response(**{'Content-Encoding': 'gzip'}), 'unsupported_content_encoding'),
            (response(b'\xff'), 'unsupported_encoding'),
            (response(b'<script>hidden</script>'), 'no_readable_text'),
        ]:
            self.rows = [row]
            result = self.read()
            self.assertEqual(result['error'], code)
            self.assertEqual(result['http_status'], 404 if code == 'http_error' else 200)
            self.assertNotIn('text', result)

    def test_invalid_status_and_redirect_failures_cannot_claim_a_terminal_response(self):
        for code in ('0200', '999', '99', '20.0'):
            self.rows = [(b'ignored', ('HTTP/1.1 ' + code + ' Error\r\n\r\n').encode())]
            result = self.read()
            self.assertEqual(result['error'], 'invalid_headers')
            self.assertNotIn('http_status', result)
        self.rows = [response(status=302, Location='/next')]

        def next_or_timeout(*args):
            if self.rows:
                return self.rows.pop(0)
            raise PageError('timeout')

        self.request = next_or_timeout
        result = self.read()
        self.assertEqual(result['error'], 'timeout')
        self.assertNotIn('http_status', result)

    def test_failed_redirect_uses_only_the_attempted_public_destination(self):
        self.rows = [
            response(status=302, Location='https://other.example/final'),
            response(status=404),
        ]
        result = self.read()
        self.assertEqual(result['url'], 'https://example.com/page')
        self.assertEqual(result['final_url'], 'https://other.example/final')
        self.assertEqual(result['http_status'], 404)
        self.assertNotIn('text', result)

        self.rows = [response(status=302, Location='http://127.0.0.1/secret')]
        result = self.read()
        self.assertEqual(result['error'], 'invalid_redirect')
        self.assertEqual(result['final_url'], result['url'])
        self.assertNotIn('127.0.0.1', str(result))

    def test_nonpublic_internal_error_text_is_not_returned(self):
        def fail(*args):
            raise PageError('/private/owner/credential-secret')

        result = read_page('https://example.com', resolve=fail)
        self.assertEqual(result['error'], 'read_failed')
        self.assertNotIn('credential-secret', str(result))

    def test_oversized_body_is_not_promoted_to_a_partial_success(self):
        self.rows = [response(b'a' * (MAX_BYTES + 1))]
        value = self.read()
        self.assertEqual(value['error'], 'page_too_large')
        self.assertNotIn('text', value)

    def test_text_excerpt_truncation_is_explicit_and_received_bytes_stay_exact(self):
        body = b'X' * (MAX_TEXT + 100)
        self.rows = [response(body, **{'Content-Type': 'text/plain'})]
        value = self.read()
        self.assertEqual(len(value['text']), MAX_TEXT)
        self.assertTrue(value['text_truncated'])
        self.assertEqual(value['received_bytes'], len(body))

    def test_cancelled_read_starts_no_dns_and_discards_late_data(self):
        self.assertEqual(self.read(current=lambda: False)['error'], 'cancelled')
        self.assertEqual(self.resolves, [])
        active = [True]

        def request(*args):
            active[0] = False
            return response()

        value = read_page(
            'https://example.com/', lambda: active[0], resolve=self.resolve, request=request
        )
        self.assertEqual(value['error'], 'cancelled')
        self.assertNotIn('text', value)

    def test_ambiguous_headers_and_fake_extra_responses_fail(self):
        for raw in (
            b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Type: text/plain\r\n\r\n',
            b'HTTP/1.1 200 OK\r\n\r\nHTTP/1.1 200 OK\r\n\r\n',
            b'not headers',
            b'\x00',
        ):
            with self.assertRaises(PageError):
                headers(raw)
        self.assertEqual(headers(b'HTTP/1.1 103 Hints\r\n\r\nHTTP/2 200\r\n\r\n')[0], 200)

    def test_dns_all_addresses_must_be_global_and_mapped_or_multicast_are_rejected(self):
        def row(ip):
            return (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 0))

        for ips in [('8.8.8.8',), ('8.8.8.8', '10.0.0.1'), ('::ffff:8.8.8.8',), ('224.0.0.1',)]:
            with patch('source_page.socket.getaddrinfo', return_value=[row(ip) for ip in ips]):
                if len(ips) == 1 and ips[0] == '8.8.8.8':
                    self.assertEqual(
                        addresses('example.com', time.monotonic() + 1, lambda: True), '8.8.8.8'
                    )
                else:
                    with self.assertRaises(PageError):
                        addresses('example.com', time.monotonic() + 1, lambda: True)

    def test_stalled_dns_is_bounded_and_releases_its_slot_when_it_finishes(self):
        release = threading.Event()

        def stalled(*args, **kwargs):
            release.wait(1)
            return []

        with patch('source_page.socket.getaddrinfo', side_effect=stalled):
            try:
                start = time.monotonic()
                with self.assertRaisesRegex(PageError, 'timeout'):
                    addresses('example.com', start + 0.03, lambda: True)
                self.assertLess(time.monotonic() - start, 0.5)
            finally:
                release.set()

    def test_thread_start_failure_does_not_leak_the_dns_capacity(self):
        with patch('source_page.threading.Thread.start', side_effect=RuntimeError):
            for _ in range(3):
                with self.assertRaisesRegex(PageError, 'dns_busy'):
                    addresses('example.com', time.monotonic() + 1, lambda: True)

    def test_no_persistent_output_or_credential_options_are_sent_to_curl(self):
        commands = []
        fetch(
            'https://example.com/',
            '8.8.8.8',
            time.monotonic() + 2,
            lambda: True,
            popen=self.child('pass', commands),
        )
        cmd = commands[0]
        for flag in (
            '--cookie',
            '--cookie-jar',
            '--netrc',
            '--output',
            '--config',
            '--proxy',
            '-L',
        ):
            self.assertNotIn(flag, cmd)

    def child(self, code, commands=None):
        def popen(command, **kwargs):
            if commands is not None:
                commands.append(command)
            return subprocess.Popen([sys.executable, '-c', code], **kwargs)

        return popen

    def test_real_pipe_io_is_bounded_and_curl_cannot_use_proxy_or_follow_redirects(self):
        commands = []
        body, raw = fetch(
            'https://example.com/',
            '8.8.8.8',
            time.monotonic() + 2,
            lambda: True,
            popen=self.child(
                'import sys;sys.stdout.buffer.write(b"body");sys.stderr.buffer.write(b"headers")',
                commands,
            ),
        )
        self.assertEqual((body, raw), (b'body', b'headers'))
        cmd = commands[0]
        self.assertEqual(cmd[:2], ['/usr/bin/curl', '-q'])
        self.assertEqual(cmd[cmd.index('--noproxy') + 1], '*')
        self.assertEqual(cmd[cmd.index('--connect-to') + 1], '::8.8.8.8:443')
        self.assertNotIn('--location', cmd)
        self.assertNotIn('--insecure', cmd)
        self.assertNotIn('--netrc', cmd)

    def test_final_network_pin_rejects_private_addresses_before_starting_a_child(self):
        for address in ('127.0.0.1', '10.0.0.1', '::ffff:8.8.8.8'):
            with self.assertRaisesRegex(PageError, 'non_public_address'):
                fetch(
                    'https://example.com/',
                    address,
                    time.monotonic() + 1,
                    lambda: True,
                    popen=lambda *a, **k: self.fail('Unsafe connection started'),
                )

    def test_real_child_overflow_timeout_and_cancellation_are_killed_and_reaped(self):
        for code, current, budget, expected in [
            (
                'import sys;sys.stdout.buffer.write(b"X"*1100000);sys.stdout.flush()',
                lambda: True,
                2,
                'page_too_large',
            ),
            ('import time;time.sleep(10)', lambda: True, 0.08, 'timeout'),
        ]:
            start = time.monotonic()
            with self.assertRaisesRegex(PageError, expected):
                fetch(
                    'https://example.com/',
                    '8.8.8.8',
                    start + budget,
                    current,
                    popen=self.child(code),
                )
            self.assertLess(time.monotonic() - start, 3)
        active = [True]

        def popen(command, **kwargs):
            child = self.child('import time;time.sleep(10)')(command, **kwargs)
            active[0] = False
            return child

        with self.assertRaisesRegex(PageError, 'cancelled'):
            fetch(
                'https://example.com/',
                '8.8.8.8',
                time.monotonic() + 2,
                lambda: active[0],
                popen=popen,
            )


if __name__ == '__main__':
    unittest.main()
