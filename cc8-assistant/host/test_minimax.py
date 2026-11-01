import json
import threading
import time
import unittest
import numpy as np

from minimax_client import (
    Transport,
    MiniMaxBrain,
    MiniMaxVoice,
    MiniMaxError,
    Cancelled,
    RequestSlot,
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
