"""Generate UI-fault transcripts using real controllers and isolated provider I/O.

Default I/O is fake and offline. Explicit loopback TLS uses a verified temporary
certificate and fixed failures, never owner credentials or remote providers.
Explicit local stdio uses a fixed peer, never a Codex installation.
UI recovery never plays audio. Both TLS fixtures check local ASR/TTS recovery
requests in memory.
"""

from dataclasses import dataclass, replace
from contextlib import ExitStack
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DOT_FIXTURES = ('fake', 'local_stdio')
HTTP_FIXTURES = ('fake', 'loopback_tls', 'loopback_business')
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
SEARCH_CASES = tuple(Case('search', kind, repeat) for kind in KINDS for repeat in range(1, 4))
PLAYBACK_CASES = tuple(
    Case(stage, kind, repeat)
    for stage, kind in (
        ('playback', 'provider_or_runtime'),
        ('playback', 'timeout'),
        ('transport', 'transport_or_io'),
    )
    for repeat in range(1, 4)
)
INTERRUPTION_CASES = (Case('interruption', 'timeout', 1),)
CASE_SETS = {
    'standard': CASES,
    'search': SEARCH_CASES,
    'playback': PLAYBACK_CASES,
    'interruption': INTERRUPTION_CASES,
}
OFFLINE_OUTPUT_CASE_SETS = ('playback', 'interruption')


def generate(*, http_fixture='fake', dot_fixture='fake', cases=CASES):
    if http_fixture not in HTTP_FIXTURES:
        raise ValueError('Unknown QA HTTP fixture')
    if dot_fixture not in DOT_FIXTURES:
        raise ValueError('Unknown QA Dot fixture')
    first = cases[0] if isinstance(cases, tuple) and cases else None
    allowed = next((group for group in CASE_SETS.values() if first in group), ())
    search = allowed is SEARCH_CASES
    playback = allowed is PLAYBACK_CASES or allowed is INTERRUPTION_CASES
    if (
        not allowed
        or len(cases) > len(allowed)
        or any(
            not isinstance(case, Case) or type(case.repeat) is not int or case not in allowed
            for case in cases
        )
        or len(set(cases)) != len(cases)
    ):
        raise ValueError('Unknown QA case selection')
    if search and (http_fixture == 'fake' or dot_fixture != 'fake'):
        raise ValueError('Search QA requires loopback TLS and no Dot subprocess')
    if playback and (http_fixture != 'fake' or dot_fixture != 'fake'):
        raise ValueError('Output QA requires offline fixtures')
    import numpy as np
    import requests

    sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
    import bridge
    from brain_router import BrainRouter
    from configuration import load_config
    from dot_client import DotError, DotApprovalRequired
    from failure_feedback import feedback
    from minimax_client import Transport, MiniMaxBrain, MiniMaxVoice
    from protocol import receive, EVENT, SPEAKER, decode_event
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
        pcm_attempts = 0

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

    with ExitStack() as stack:
        temporary = stack.enter_context(
            tempfile.TemporaryDirectory(prefix='cc8-ui-fault-fixtures-')
        )
        folder = Path(temporary)
        defaults = folder / 'cc8-assistant/host/voice-config.json'
        defaults.parent.mkdir(parents=True)
        defaults.write_text('{"schema_version":1,"asr":"minimax"}')
        config = load_config(root=folder, environ={})
        tls = None
        factory = FaultSession
        if http_fixture != 'fake':
            from tools.qa_http import LoopbackHTTP, RECOVERY_TEXT, RECOVERY_PCM, SEARCH_SOURCE

            def requested():
                state.http_calls += 1

            tls = stack.enter_context(
                LoopbackHTTP(
                    lambda: state.case,
                    requested,
                    business=http_fixture == 'loopback_business',
                    recovering=lambda: state.recovery,
                    search=search,
                )
            )
            factory = tls.session_factory
        transport = Transport(
            key='qa-not-a-real-credential', session_factory=factory, config=config
        )
        if tls is not None:
            # Change this isolated test instance only. The real constructor's
            # official-endpoint allowlist and all owner configuration stay intact.
            transport.base = tls.base
            transport.timeout = (0.8, 0.8)
        recognizer = MandarinRecognizer(engine='minimax', recognizer=transport, config=config)
        voice = MiniMaxVoice(transport=transport, config=config)

        class Speech:
            def recognize(self, pcm):
                return recognizer.recognize(pcm)

            def stream(self, text, current, on_subtitle=None):
                if playback:
                    frames = 640 if state.case.stage == 'interruption' else 320
                    return iter((np.zeros(frames, dtype=np.int16),))
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

        fake_dot = FakeDot()
        local_dot = None
        if dot_fixture == 'local_stdio':
            from tools.qa_dot import LocalDot

            local_dot = LocalDot(folder)
            stack.callback(local_dot.close)
        with patch.object(bridge, 'PRIVATE', folder), patch('threading.Thread.start'):
            b = bridge.Bridge(
                Speech(),
                BrainRouter(
                    MiniMaxBrain(transport, config=config) if search else fake_dot,
                    local_dot.brain if local_dot else fake_dot,
                    selected='minimax' if search else 'dot',
                ),
            )
        b.session.connection = Recorder()
        if playback:
            b.playback.set_completion_capability(True)
            send_event = b.send

            def fault_output(packet):
                if packet.kind != SPEAKER:
                    return send_event(packet)
                state.pcm_attempts += 1
                if state.case.stage == 'transport':
                    raise BrokenPipeError('qa-injected-sensitive-body')
                if state.case.stage == 'interruption':
                    if b.pause_for_voice() is None:
                        raise AssertionError('Fixture did not acquire a tentative pause')
                    b.playback.pause_state['started'] -= 21
                    return
                if state.case.kind == 'provider_or_runtime':
                    b.on_event(
                        {
                            'event': 'playback_error',
                            'generation': packet.generation,
                            'playback_id': b.playback.playback_serial,
                            'error_type': 'IOException',
                        }
                    )
                # Withhold PCM and completion in the timeout case. No speaker
                # packet is retained or replayed to the QA device.

            b.send = fault_output
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
        for case in cases:
            state.case = case
            state.recovery = False
            state.http_calls = state.brain_calls = 0
            state.pcm_attempts = 0
            packets.clear()
            dot_start_timeout = (
                local_dot is not None and case.stage == 'brain' and case.kind == 'timeout'
            )
            if local_dot:
                local_dot.withhold_start_ack = dot_start_timeout
            prefix = '联网查询' if search else ''
            b.submit_text(prefix + '自动故障测试 ' + case.ident)
            job = b.response.queue.get_nowait()
            interruption = case.stage == 'interruption'
            if interruption:
                # Simulate the host-side recording flag only. Never send PCM or
                # start the microphone on the isolated Android receiver.
                b.session.active = True
                b.playback.set_pause_capability(True)
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
            if interruption:
                if not b.session.active or b.playback.pause_state is not None:
                    raise AssertionError(
                        'Interruption timeout changed capture or retained the pause'
                    )
                b.session.active = False
            error_packets = list(packets)
            unknown_start = (
                local_dot is not None
                and case.stage == 'brain'
                and case.kind in ('transport_or_io', 'timeout')
            )
            expected_message = (
                '插话识别超时，已停止旧播报'
                if interruption
                else feedback(case.stage, case.kind, unconfirmed=unknown_start).message
            )
            errors = [
                p
                for p in error_packets
                if (d := decode_event(p.data)).get('event') == 'status'
                and d.get('phase') == 'error'
            ]
            if len(errors) != 1 or decode_event(errors[0].data)['message'] != expected_message:
                raise AssertionError('Fixture did not preserve its declared failure')
            report = b.diagnostics.report()['requests'][-1]
            expected_outcome = 'cancelled' if interruption else 'failed'
            expected_error = None if interruption else {'stage': case.stage, 'kind': case.kind}
            if report['outcome'] != expected_outcome or report['error'] != expected_error:
                raise AssertionError('Fixture diagnostic mismatch')
            expected_http = 2 if search else 0 if playback or case.stage == 'brain' else 1
            expected_brain = int(playback or case.stage in ('brain', 'tts'))
            fault_turns = (
                local_dot.counts()[case.ident + ':fault'] if local_dot else state.brain_calls
            )
            if (state.http_calls, fault_turns, state.pcm_attempts) != (
                expected_http,
                expected_brain,
                int(playback),
            ):
                raise AssertionError('Unexpected provider request/retry')
            wire_requests = tls.count(case.ident) if tls is not None else 0
            if tls is not None and wire_requests != expected_http:
                raise AssertionError('QA TLS request did not reach its fixed local endpoint')
            expected_task = (
                'completed'
                if playback or case.stage == 'tts'
                else 'failed'
                if case.stage in ('brain', 'search')
                else None
            )
            if expected_task and b.workspace.tasks[-1]['status'] != expected_task:
                raise AssertionError('Failure changed task completion incorrectly')
            if playback and (b.playback.playing or b.session.text_generation is not None):
                raise AssertionError('Output fault left playback or text ownership active')
            if unknown_start and b.workspace.tasks[-1]['stage'] != '执行结果待核对':
                raise AssertionError('Fixture task lost the unconfirmed start')
            if dot_start_timeout:
                expired_peer = local_dot.processes[-1]
                if expired_peer.poll() is None:
                    raise AssertionError('Timed-out Dot peer survived before explicit recovery')
                local_dot.withhold_start_ack = False
            if 'qa-injected-sensitive-body' in json.dumps(
                [decode_event(p.data) for p in error_packets]
            ):
                raise AssertionError('Provider body leaked into fixture')
            state.recovery = True
            packets.clear()
            recovery_wire_requests = 0
            if tls is not None and case.stage in ('asr', 'tts'):
                if case.stage == 'asr':
                    recovered = recognizer.recognize(audio) == RECOVERY_TEXT
                else:
                    pcm = b''.join(
                        chunk.astype('<i2').tobytes() for chunk in voice.pcm(RECOVERY_TEXT)
                    )
                    recovered = pcm == RECOVERY_PCM
                recovery_wire_requests = tls.count(case.ident, recovery=True)
                if not recovered or recovery_wire_requests != 1 or transport.slots:
                    raise AssertionError('Fixture transport did not recover on a new request')
            b.submit_text(prefix + '自动恢复检查 ' + case.ident)
            run_one(b.response, b.response.queue.get_nowait())
            recovery = list(packets)
            if search:
                recovery_wire_requests = tls.count(case.ident, recovery=True)
                report = b.diagnostics.report()['requests'][-1]
                sources = [
                    e['items']
                    for packet in recovery
                    if (e := decode_event(packet.data)).get('event') == 'web_sources'
                    and e.get('items')
                ]
                if (
                    recovery_wire_requests != 3
                    or state.brain_calls
                    or 'web_route_completed' not in report['stages']
                    or len(sources) != 1
                    or [row['url'] for row in sources[0]] != [SEARCH_SOURCE]
                    or transport.slots
                ):
                    raise AssertionError('Fixture search did not recover on a new request')
            if (
                b.workspace.tasks[-1]['status'] != 'completed'
                or b.session.text_generation is not None
                or state.http_calls != expected_http + recovery_wire_requests
            ):
                raise AssertionError('Recovery did not complete')
            if playback and (state.pcm_attempts != 1 or state.brain_calls != 2):
                raise AssertionError('Output recovery repeated a request or emitted PCM')
            if dot_start_timeout and local_dot.processes[-1] is expired_peer:
                raise AssertionError('Recovery did not start a fresh Dot peer')
            dot_counts = {}
            if local_dot:
                observed = local_dot.counts()
                dot_counts = {
                    'fault_turns': observed[case.ident + ':fault'],
                    'recovery_turns': observed[case.ident + ':recovery'],
                    'declined_approvals': observed[case.ident + ':declined'],
                    'withheld_start_acks': observed[case.ident + ':withheld_start_ack'],
                }
                if dot_counts != {
                    'fault_turns': expected_brain,
                    'recovery_turns': 1,
                    'declined_approvals': int(case.kind == 'approval_declined'),
                    'withheld_start_acks': int(dot_start_timeout),
                }:
                    raise AssertionError('Unexpected Dot stdio request/recovery or approval')
            rows.append(
                {
                    'case': case,
                    'error': error_packets,
                    'stale_error': errors[0],
                    'recovery': recovery,
                    'expected_task': expected_task,
                    'expected_message': expected_message,
                    'provider_requests': expected_http + expected_brain,
                    'http_fixture': http_fixture,
                    'dot_fixture': dot_fixture,
                    'dot_wire_counts': dot_counts,
                    'wire_requests': wire_requests,
                    'recovery_wire_requests': recovery_wire_requests,
                }
            )
        if local_dot and sum(local_dot.counts().values()) != sum(
            sum(row['dot_wire_counts'].values()) for row in rows
        ):
            raise AssertionError('Unexpected extra Dot stdio request')
        transport.cancel()
        return rows
