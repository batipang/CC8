import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
import numpy as np

from minimax_client import (
    Transport,
    MiniMaxBrain,
    MiniMaxVoice,
    MiniMaxError,
    Cancelled,
    RequestSlot,
    SEARCH_ROUTE,
    VisibleText,
    sse_events,
)


class Response:
    status_code = 200
    headers = {'Content-Type': 'text/event-stream; charset=utf-8'}

    def __init__(self, events):
        self.events = events
        self.closed = False

    def iter_lines(self, **kwargs):
        for e in self.events:
            yield 'data: ' + json.dumps(e, ensure_ascii=False)
            yield ''
        yield 'data: [DONE]'
        yield ''

    def iter_content(self, **kwargs):
        yield ('\n'.join(self.iter_lines()) + '\n').encode('utf-8')

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


def transport(events):
    response = Response(events)
    session = Session(response)
    return (
        Transport(key='sk-cp-test-not-a-secret', session_factory=lambda: session),
        session,
        response,
    )


class StreamTests(unittest.TestCase):
    def test_multiline_sse_and_done(self):
        lines = [': ping', 'event: chunk', 'data: {"a":', 'data: 1}', '', 'data: [DONE]', '']
        self.assertEqual(list(sse_events(lines)), [{'a': 1}])

    def test_split_thinking_tags_never_become_visible(self):
        f = VisibleText()
        out = ''.join(
            f.feed(x) for x in ['<thi', 'nk>private', ' reasoning</th', 'ink>你好', '，我在。']
        )
        self.assertEqual(out, '你好，我在。')

    def test_unclosed_thinking_is_suppressed(self):
        f = VisibleText()
        self.assertEqual(f.feed('<think>private'), '')
        self.assertEqual(f.feed('more private'), '')

    def test_cancel_does_not_wait_for_response_read_lock(self):
        entered = threading.Event()
        release = threading.Event()

        class Blocking:
            def close(self):
                entered.set()
                release.wait(2)

        slot = RequestSlot(response=Blocking())
        start = time.monotonic()
        slot.cancel()
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertTrue(slot.stopped.is_set())
        self.assertTrue(entered.wait(1))
        release.set()

    def test_cancel_closes_session_even_when_response_close_fails(self):
        closed = threading.Event()
        response = Mock()
        response.close.side_effect = OSError('private cleanup detail')
        session = Mock()
        session.close.side_effect = closed.set
        slot = RequestSlot(response=response, session=session)
        slot.cancel()
        self.assertTrue(closed.wait(1))
        response.close.assert_called_once()
        session.close.assert_called_once()


class TransportCleanupTests(unittest.TestCase):
    def test_explicit_bearer_keeps_netrc_out_of_prepared_requests(self):
        import requests

        transport = Transport(key='fixture-bearer')
        slot = transport.request_slot()
        self.addCleanup(transport.release_slot, slot)
        with patch(
            'requests.sessions.get_netrc_auth', return_value=('fixture', 'unrelated')
        ) as netrc:
            prepared = slot.session.prepare_request(requests.Request('POST', transport.base))
        netrc.assert_not_called()
        self.assertEqual(prepared.headers['Authorization'], 'Bearer fixture-bearer')
        self.assertTrue(slot.session.trust_env)

    def test_terminal_cleanup_preserves_success_failure_and_cancellation(self):
        for entry in ('asr', 'json', 'sse'):
            for outcome in ('success', 'quota', 'cancelled'):
                with self.subTest(entry=entry, outcome=outcome):
                    value = {'text': '固定测试'}
                    if outcome == 'quota':
                        value = {'base_resp': {'status_code': 1008}}
                    t, session, response = transport([value])
                    if entry != 'sse':
                        response.iter_content = Mock(
                            return_value=iter([json.dumps(value).encode()])
                        )
                    response.close = Mock(side_effect=OSError('private cleanup detail'))
                    close_session = Mock(side_effect=OSError('private session cleanup detail'))
                    session.close = close_session

                    def current():
                        return outcome != 'cancelled' or not session.calls

                    if entry == 'asr':
                        call = lambda: t.transcribe(b'RIFFtest', current)
                        expected = '固定测试'
                    elif entry == 'json':
                        call = lambda: t.post_json('/v1/chat/completions', {}, current)
                        expected = value
                    else:
                        call = lambda: list(t.events('/v1/chat/completions', {}, current))
                        expected = [value]

                    if outcome == 'success':
                        self.assertEqual(call(), expected)
                    elif outcome == 'quota':
                        with self.assertRaises(MiniMaxError) as error:
                            call()
                        self.assertEqual(error.exception.kind, 'quota')
                    else:
                        with self.assertRaises(Cancelled):
                            call()
                    self.assertEqual(len(session.calls), 1)
                    self.assertEqual(t.slots, set())
                    response.close.assert_called_once()
                    close_session.assert_called_once()

    def test_read_only_retry_stops_if_the_first_response_cannot_close(self):
        t, session, response = transport([])
        response.status_code = 503
        response.close = Mock(side_effect=OSError('private cleanup detail'))
        with self.assertRaises(MiniMaxError) as error:
            t.post_json(SEARCH_ROUTE, {'q': '固定公开搜索'})
        self.assertEqual(error.exception.kind, 'transport_or_io')
        self.assertNotIn('private cleanup detail', str(error.exception))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(t.slots, set())
        self.assertTrue(session.closed)


class TextTests(unittest.TestCase):
    def test_direct_request_streams_only_visible_clauses(self):
        events = [
            {'choices': [{'delta': {'reasoning_content': 'never speak this'}}]},
            {'choices': [{'delta': {'content': '你好。第二'}}]},
            {'choices': [{'delta': {'content': '句话。'}, 'finish_reason': 'stop'}]},
        ]
        t, s, r = transport(events)
        b = MiniMaxBrain(t)
        self.assertEqual(list(b.clauses('你好', [])), ['你好。', '第二句话。'])
        url, kwargs = s.calls[0]
        self.assertEqual(url, 'https://api.minimax.cn/v1/chat/completions')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('tools', kwargs['json'])
        self.assertEqual(kwargs['json']['thinking'], {'type': 'disabled'})
        self.assertTrue(r.closed)

    def test_rejects_other_credential_hosts(self):
        with self.assertRaises(ValueError):
            Transport(key='test', base='https://example.com')

    def test_cancelled_generation_sends_no_request(self):
        t, s, r = transport([])
        with self.assertRaises(Cancelled):
            list(t.events('/v1/chat/completions', {}, lambda: False))
        self.assertEqual(s.calls, [])
        self.assertTrue(s.closed)

    def test_redirect_not_followed_or_exposed_with_credentials(self):
        t, s, r = transport([])
        r.status_code = 302
        with self.assertRaises(MiniMaxError) as e:
            list(t.events('/v1/chat/completions', {}))
        self.assertEqual(e.exception.http_status, 302)
        self.assertNotIn('sk-cp-test', str(e.exception))

    def test_truncated_response_is_not_marked_complete(self):
        t, _, _ = transport([{'choices': [{'delta': {'content': 'partial'}}]}])
        with self.assertRaises(MiniMaxError):
            MiniMaxBrain(t).reply('question', [])

    def test_api_status_failure_is_not_a_scripted_reply(self):
        t, _, _ = transport([{'base_resp': {'status_code': 1008}}])
        with self.assertRaises(MiniMaxError) as e:
            MiniMaxBrain(t).reply('question', [])
        self.assertEqual(e.exception.code, 1008)

    def test_midstream_cancel_discards_late_text(self):
        t, _, r = transport(
            [
                {'choices': [{'delta': {'content': '第一句。'}}]},
                {'choices': [{'delta': {'content': '旧的第二句。'}, 'finish_reason': 'stop'}]},
            ]
        )
        current = [True]
        clauses = MiniMaxBrain(t).clauses('q', [], lambda: current[0])
        self.assertEqual(next(clauses), '第一句。')
        current[0] = False
        with self.assertRaises(Cancelled):
            next(clauses)
        self.assertTrue(r.closed)


class VoiceTests(unittest.TestCase):
    def test_final_aggregate_cannot_duplicate_audio(self):
        a = np.arange(320, dtype='<i2').tobytes()
        b = (np.arange(160, dtype='<i2') + 500).tobytes()
        t, s, r = transport(
            [
                {'data': {'audio': a.hex(), 'status': 1}},
                {'data': {'audio': (a + b).hex(), 'status': 2}},
            ]
        )
        pcm = np.concatenate(list(MiniMaxVoice(t).pcm('test')))
        self.assertEqual(pcm.tobytes(), a + b)
        body = s.calls[0][1]['json']
        self.assertTrue(body['stream_options']['exclude_aggregated_audio'])
        self.assertEqual(
            body['audio_setting'], {'sample_rate': 16000, 'format': 'pcm', 'channel': 1}
        )

    def test_pcm_split_on_odd_network_boundary_is_reassembled(self):
        raw = np.arange(400, dtype='<i2').tobytes()
        t, _, _ = transport(
            [
                {'data': {'audio': raw[:101].hex(), 'status': 1}},
                {'data': {'audio': raw[101:].hex(), 'status': 2}},
            ]
        )
        self.assertEqual(np.concatenate(list(MiniMaxVoice(t).pcm('test'))).tobytes(), raw)

    def test_missing_final_event_is_an_error(self):
        t, _, _ = transport([{'data': {'audio': '0000' * 320, 'status': 1}}])
        with self.assertRaises(MiniMaxError):
            list(MiniMaxVoice(t).pcm('test'))

    def test_incomplete_pcm_sample_is_an_error(self):
        t, _, _ = transport([{'data': {'audio': '01', 'status': 2}}])
        with self.assertRaises(MiniMaxError):
            list(MiniMaxVoice(t).pcm('test'))

    def test_stale_audio_is_not_yielded(self):
        t, _, r = transport(
            [
                {'data': {'audio': '0000' * 320, 'status': 1}},
                {'data': {'audio': '0000' * 320, 'status': 2}},
            ]
        )
        current = [True]
        chunks = MiniMaxVoice(t).pcm('test', lambda: current[0])
        next(chunks)
        current[0] = False
        with self.assertRaises(Cancelled):
            next(chunks)
        self.assertTrue(r.closed)


if __name__ == '__main__':
    unittest.main()
