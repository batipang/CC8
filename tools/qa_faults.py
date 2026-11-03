"""Generate fixed UI-fault transcripts using real controllers and fake provider I/O.

No device, microphone, key, model, DNS, or network is accessed. Recovery is UI
only: synthesis/playback is intentionally bypassed and is never scored as audio.
"""

from dataclasses import dataclass, replace
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
KINDS = ('timeout', 'rate_limit', 'quota', 'credential_or_permission', 'transport_or_io')


@dataclass(frozen=True)
class Case:
    stage: str
    kind: str
    repeat: int

    @property
    def ident(self):
        return f'{self.stage}-{self.kind}-{self.repeat}'


CASES = tuple(
    Case(stage, kind, repeat)
    for stage in ('asr', 'brain', 'tts')
    for kind in KINDS + (('approval_declined',) if stage == 'brain' else ())
    for repeat in range(1, 4)
)


def generate():
    import numpy as np
    import requests

    sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
    import bridge
    from brain_router import BrainRouter
    from configuration import load_config
    from dot_client import DotError, DotApprovalRequired
    from failure_feedback import feedback
    from minimax_client import Transport, MiniMaxVoice
    from protocol import receive, EVENT, decode_event
    from recognition import MandarinRecognizer

    class StopFixture(BaseException):
        pass

    def run_one(worker, job):
        with patch.object(worker.queue, 'get', side_effect=[job, StopFixture]):
            try:
                worker.work()
            except StopFixture:
                pass

    class State:
        case = None
        recovery = False
        http_calls = 0
        brain_calls = 0

    state = State()

    class FaultSession:
        def post(self, *args, **kwargs):
            state.http_calls += 1
            if state.case.kind == 'timeout':
                raise requests.Timeout('qa-injected-sensitive-body')
            if state.case.kind == 'transport_or_io':
                raise requests.ConnectionError('qa-injected-sensitive-body')

            class Response:
                status_code = {'rate_limit': 429, 'quota': 402, 'credential_or_permission': 401}[
                    state.case.kind
                ]
                headers = {'Content-Type': 'application/json'}

                def close(self):
                    pass

            return Response()

        def close(self):
            pass

    class FakeDot:
        last_metrics = {}

        def cancel(self):
            pass

        def clauses(self, text, history, current, on_web, **kwargs):
            state.brain_calls += 1
            self.last_metrics = {}
            if not state.recovery and state.case.stage == 'brain':
                if state.case.kind == 'approval_declined':
                    raise DotApprovalRequired('qa-injected-sensitive-body')
                raise DotError('qa-injected-sensitive-body', kind=state.case.kind)
            on_web('dot_result', {'text': '自动测试结果已生成。', 'truncated': False})
            self.last_metrics = {'completed': True}
            yield '自动测试恢复检查已完成。'

    with tempfile.TemporaryDirectory(prefix='cc8-ui-fault-fixtures-') as temporary:
        folder = Path(temporary)
        defaults = folder / 'cc8-assistant/host/voice-config.json'
        defaults.parent.mkdir(parents=True)
        defaults.write_text('{"schema_version":1,"asr":"minimax"}')
        config = load_config(root=folder, environ={})
        transport = Transport(
            key='qa-not-a-real-credential', session_factory=FaultSession, config=config
        )
        recognizer = MandarinRecognizer(engine='minimax', recognizer=transport, config=config)
        voice = MiniMaxVoice(transport=transport, config=config)

        class Speech:
            def recognize(self, pcm):
                return recognizer.recognize(pcm)

            def stream(self, text, current, on_subtitle=None):
                return voice.pcm(text, current, on_subtitle)

            def cancel(self):
                transport.cancel()

        packets = []

        class Reader:
            def __init__(self, data):
                self.data = io.BytesIO(data)

            def recv(self, count):
                return self.data.read(count)

        class Recorder:
            kind = 'usb'

            def sendall(self, data):
                packet = receive(Reader(data))
                if packet.kind != EVENT:
                    raise AssertionError('Fixture emitted unexpected audio')
                packets.append(packet)

        with patch.object(bridge, 'PRIVATE', folder), patch('threading.Thread.start'):
            b = bridge.Bridge(Speech(), BrainRouter(FakeDot(), FakeDot()))
        b.session.connection = Recorder()
        b.log = lambda *args, **kwargs: None
        b.save_status = lambda: None
        b.session.wake = False
        b.session.awake = True
        # These virtual utterances have a text-request owner and no microphone.
        # Wake detection is outside this deliberately narrow UI fault fixture.
        b.recognition.hooks = replace(b.recognition.hooks, wake_allowed=lambda: True)
        original_speak = b.speak
        b.speak = lambda *args: True if state.recovery else original_speak(*args)
        audio = (
            np.sin(np.arange(16000, dtype=np.float32) * 2 * np.pi * 440 / 16000) * 0.08
        ).astype(np.float32)
        rows = []
        for case in CASES:
            state.case = case
            state.recovery = False
            state.http_calls = state.brain_calls = 0
            packets.clear()
            b.submit_text('自动故障测试 ' + case.ident)
            job = b.response.queue.get_nowait()
            if case.stage == 'asr':
                # The UI owner is text-only, but this virtual recognizer job
                # has a voice diagnostic trace. No microphone is opened.
                trace = b.diagnostics.begin('voice')
                run_one(b.recognition, (job.generation, audio, False, [], None, None, trace))
                observed = next(
                    r for r in b.diagnostics.report()['requests'] if r['request_id'] == trace
                )
                if observed.get('gate') != 'error' or observed.get('outcome') != 'failed':
                    raise AssertionError('Fixture recognizer trace was not a classified failure')
                b.finish_text_response(job.generation)
            else:
                run_one(b.response, job)
            error_packets = list(packets)
            errors = [
                p
                for p in error_packets
                if (d := decode_event(p.data)).get('event') == 'status'
                and d.get('phase') == 'error'
            ]
            if (
                len(errors) != 1
                or decode_event(errors[0].data)['message']
                != feedback(case.stage, case.kind).message
            ):
                raise AssertionError('Fixture did not preserve its declared failure')
            report = b.diagnostics.report()['requests'][-1]
            if report['outcome'] != 'failed' or report['error'] != {
                'stage': case.stage,
                'kind': case.kind,
            }:
                raise AssertionError('Fixture diagnostic mismatch')
            expected_http = 0 if case.stage == 'brain' else 1
            expected_brain = 0 if case.stage == 'asr' else 1
            if (state.http_calls, state.brain_calls) != (expected_http, expected_brain):
                raise AssertionError('Unexpected provider request/retry')
            expected_task = (
                'completed' if case.stage == 'tts' else 'failed' if case.stage == 'brain' else None
            )
            if expected_task and b.workspace.tasks[-1]['status'] != expected_task:
                raise AssertionError('Failure changed task completion incorrectly')
            if 'qa-injected-sensitive-body' in json.dumps(
                [decode_event(p.data) for p in error_packets]
            ):
                raise AssertionError('Provider body leaked into fixture')
            state.recovery = True
            packets.clear()
            b.submit_text('自动恢复检查 ' + case.ident)
            run_one(b.response, b.response.queue.get_nowait())
            recovery = list(packets)
            if (
                b.workspace.tasks[-1]['status'] != 'completed'
                or b.session.text_generation is not None
            ):
                raise AssertionError('Recovery did not complete')
            rows.append(
                {
                    'case': case,
                    'error': error_packets,
                    'stale_error': errors[0],
                    'recovery': recovery,
                    'expected_task': expected_task,
                    'expected_message': feedback(case.stage, case.kind).message,
                    'provider_requests': expected_http + expected_brain,
                }
            )
        transport.cancel()
        return rows
