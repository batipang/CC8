"""Business failures traverse the actual clients and workers without cloud calls."""

import json
import unittest
from types import SimpleNamespace

import numpy as np
from brain_router import BrainRouter
from diagnostics import error_kind
from minimax_client import MiniMaxBrain, MiniMaxError, MiniMaxVoice, Transport
from recognition import MandarinRecognizer
from reply_types import Reply
import test_failure_feedback as worker_fixture
import test_web_search as search_fixture
from test_minimax import Session

SECRET = 'PRIVATE-provider-message-key-and-request-body'
CODES = {
    'timeout': (1001, 2066),
    'rate_limit': (1002, 1041, 1045, 2045, 2046, 2047),
    'credential_or_permission': (1004, 2042, 2049, 2057, 2061, 2068),
    'quota': (1008, 2053, 2056, 2067),
}
PUBLIC_PCM = np.ones(3200, dtype='<i2').tobytes().hex()


class Response:
    status_code = 200

    def __init__(self, data, stream=False):
        self.headers = {'Content-Type': 'text/event-stream' if stream else 'application/json'}
        self.closed = False
        self.raw = (
            ''.join('data: ' + json.dumps(row) + '\n\n' for row in data).encode()
            if stream
            else json.dumps(data).encode()
        )

    def iter_content(self, **kwargs):
        # Split even inside UTF-8/JSON tokens, exercising the real bounded parser.
        for i in range(0, len(self.raw), 7):
            yield self.raw[i : i + 7]

    def close(self):
        self.closed = True


def error_data(code):
    return {
        'base_resp': {'status_code': code, 'status_msg': SECRET},
        'text': SECRET,
        'choices': [{'delta': {'content': SECRET}, 'finish_reason': 'stop'}],
        'data': {'audio': PUBLIC_PCM, 'status': 2},
    }


def client_for(*responses):
    pending = iter(responses)
    sessions = []

    def create():
        session = Session(next(pending))
        sessions.append(session)
        return session

    return Transport(key='sk-cp-public-fixture', session_factory=create), sessions


def invoke(client, operation):
    if operation == 'asr':
        return client.transcribe(b'public-fixture')
    if operation == 'json':
        return client.post_json('/fixture', {})
    return list(client.events('/fixture', {}))


class BusinessStatusTests(unittest.TestCase):
    def test_documented_business_errors_classify_all_http_200_response_paths(self):
        for kind, codes in CODES.items():
            for code in codes:
                for operation in ('asr', 'json', 'sse', 'stream_json_error'):
                    with self.subTest(code=code, operation=operation):
                        data = error_data(code)
                        response = Response(
                            [data] if operation == 'sse' else data, operation == 'sse'
                        )
                        client, sessions = client_for(response)
                        with self.assertRaises(MiniMaxError) as caught:
                            invoke(client, operation)
                        self.assertEqual(error_kind(caught.exception), kind)
                        self.assertEqual(caught.exception.code, code)
                        self.assertNotIn(SECRET, str(caught.exception))
                        self.assertEqual(len(sessions), 1)
                        self.assertEqual(len(sessions[0].calls), 1)
                        self.assertTrue(sessions[0].closed and response.closed)
                        self.assertEqual(client.slots, set())

    def test_malformed_status_codes_cannot_release_text_json_or_audio(self):
        for code in (False, True, 0.0, None, '', '0', '1008', [], {}, -1, 2**31):
            for operation in ('asr', 'json', 'sse'):
                with self.subTest(code=repr(code), operation=operation):
                    data = error_data(code)
                    response = Response([data] if operation == 'sse' else data, operation == 'sse')
                    client, _ = client_for(response)
                    with self.assertRaises(MiniMaxError) as caught:
                        invoke(client, operation)
                    self.assertEqual(error_kind(caught.exception), 'provider_or_runtime')
                    self.assertIsNone(caught.exception.code)
                    self.assertNotIn(SECRET, str(caught.exception))
                    self.assertEqual(client.slots, set())

    def test_malformed_stream_envelopes_are_not_optional_success(self):
        for base in (None, False, 0, '', [], SECRET):
            with self.subTest(base=repr(base)):
                client, _ = client_for(Response([{'base_resp': base, 'text': SECRET}], True))
                with self.assertRaises(MiniMaxError):
                    list(client.events('/fixture', {}))

    def test_compatible_success_without_business_status_is_preserved(self):
        for operation in ('asr', 'json', 'sse'):
            with self.subTest(operation=operation):
                data = {'text': '公开恢复问题'}
                response = Response([data] if operation == 'sse' else data, operation == 'sse')
                client, _ = client_for(response)
                result = invoke(client, operation)
                self.assertEqual(
                    result,
                    data['text'] if operation == 'asr' else [data] if operation == 'sse' else data,
                )
                self.assertEqual(client.slots, set())

    def test_unknown_or_context_ambiguous_codes_and_explicit_kinds_are_not_guessed(self):
        for code in (1000, 1039, 2013, 2022, 2052, 2062, 2160, 999999):
            with self.subTest(code=code):
                self.assertEqual(error_kind(MiniMaxError(SECRET, code=code)), 'provider_or_runtime')
        self.assertEqual(error_kind(MiniMaxError(SECRET, code=1008, kind='timeout')), 'timeout')
        self.assertEqual(
            error_kind(MiniMaxError(SECRET, code=1008, http_status=401)), 'credential_or_permission'
        )

    def test_search_requires_explicit_integer_success_before_sources_or_answer(self):
        for base in ({}, {'status_code': False}, {'status_code': 0.0}):
            with self.subTest(base=base):
                transport = search_fixture.Fake(
                    search={'base_resp': base, 'organic': [search_fixture.result()]}
                )
                progress = []
                brain = MiniMaxBrain(transport)
                with self.assertRaises(MiniMaxError):
                    list(
                        brain.clauses(
                            '联网查询公开新闻', [], on_web=lambda name, data: progress.append(name)
                        )
                    )
                self.assertNotIn('sources', progress)
                self.assertEqual(
                    [route for route, _ in transport.calls],
                    ['/v1/chat/completions', '/v1/coding_plan/search'],
                )


class WorkerBusinessStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = worker_fixture.FailureFeedbackTests()

    def assert_failure(self, bridge, stage, kind):
        row = bridge.diagnostics.report()['requests'][-1]
        self.assertEqual(row['error'], {'stage': stage, 'kind': kind})
        self.assertEqual(row['outcome'], 'failed')
        self.assertTrue(self.fixture.guidance(bridge))
        self.assertNotIn(
            SECRET, json.dumps([bridge.events, bridge.logs, row, bridge.workspace.tasks])
        )

    def test_asr_business_failures_never_dispatch_then_next_explicit_request_recovers(self):
        for code, kind in (
            (1001, 'timeout'),
            (1002, 'rate_limit'),
            (1004, 'credential_or_permission'),
            (1008, 'quota'),
        ):
            with self.subTest(code=code):
                client, sessions = client_for(
                    Response(error_data(code)), Response({'text': '公开恢复问题'})
                )
                asr = MandarinRecognizer(engine='minimax', recognizer=client)
                speech = SimpleNamespace(recognize=asr.recognize, cancel=asr.cancel)
                b = self.fixture.session(speech, worker_fixture.Brain())
                pcm = np.sin(np.arange(16000) * 0.1) * 0.02
                key = b.diagnostics.begin('voice')
                self.fixture.run_one(b.recognition, (b.generation, pcm, False, [], None, None, key))
                self.assert_failure(b, 'asr', kind)
                self.assertFalse(b.recognition.busy)
                self.assertTrue(b.response.queue.empty())
                self.assertEqual(b.workspace.tasks, [])
                self.assertEqual(len(sessions), 1)
                key = b.diagnostics.begin('voice')
                self.fixture.run_one(b.recognition, (b.generation, pcm, False, [], None, None, key))
                self.assertEqual(b.response.queue.get_nowait().text, '公开恢复问题')
                self.assertEqual(len(sessions), 2)
                self.assertEqual(client.slots, set())

    def test_brain_business_failures_keep_failed_receipt_and_next_request_recovers(self):
        for code, kind in (
            (1001, 'timeout'),
            (1002, 'rate_limit'),
            (1004, 'credential_or_permission'),
            (2056, 'quota'),
        ):
            with self.subTest(code=code):
                client, sessions = client_for(
                    Response([error_data(code)], True),
                    Response(
                        [
                            {
                                'choices': [
                                    {'delta': {'content': '恢复成功。'}, 'finish_reason': 'stop'}
                                ]
                            }
                        ],
                        True,
                    ),
                )
                brain = MiniMaxBrain(client)
                b = self.fixture.session(brain=brain)
                spoken = []
                b.speak = lambda text, generation: spoken.append(text) or True
                self.fixture.run_one(b.response, Reply(b.generation, '公开测试问题'))
                self.assert_failure(b, 'brain', kind)
                self.assertEqual(b.workspace.tasks[-1]['status'], 'failed')
                self.assertEqual(spoken, [])
                self.assertFalse(b.response.busy)
                self.assertEqual(len(sessions), 1)
                b.session.generation += 1
                self.fixture.run_one(b.response, Reply(b.generation, '另一个公开问题'))
                self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
                self.assertEqual(b.workspace.tasks[-2]['status'], 'failed')
                self.assertEqual(spoken, ['恢复成功。'])
                self.assertEqual(len(sessions), 2)
                self.assertEqual(client.slots, set())

    def test_tts_business_failure_after_pcm_preserves_completed_work_and_recovers(self):
        first = {'data': {'audio': PUBLIC_PCM, 'status': 1}}
        last = {'data': {'audio': PUBLIC_PCM, 'status': 2}}
        client, sessions = client_for(
            Response([first, error_data(1008)], True), Response([last], True)
        )
        voice = MiniMaxVoice(client)
        speech = SimpleNamespace(stream=voice.pcm, cancel=voice.cancel)
        dot = worker_fixture.Brain(receipt=True)
        b = self.fixture.session(speech, BrainRouter(worker_fixture.Brain(), dot))
        b.send = lambda packet: None
        event = b.event

        def device_ack(name, **fields):
            event(name, **fields)
            if name == 'play_end':
                b.playback.device_event({'event': 'play_done', 'generation': fields['generation']})

        b.event = device_ack
        self.fixture.run_one(b.response, Reply(b.generation, '公开测试问题'))
        self.assert_failure(b, 'tts', 'quota')
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        self.assertFalse(b.playback.playing or b.response.busy)
        self.assertEqual(dot.calls, 1)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(any(name == 'play_begin' for name, _ in b.events))
        self.fixture.run_one(b.response, Reply(b.generation, '另一个公开问题'))
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        self.assertEqual(b.workspace.tasks[-2]['status'], 'completed')
        self.assertEqual(dot.calls, 2)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(client.slots, set())


if __name__ == '__main__':
    unittest.main()
