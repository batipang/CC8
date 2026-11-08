"""Fixed startup fault/recovery events from actual controllers, without providers/audio."""

from contextlib import ExitStack
import io
import json
from pathlib import Path
import queue
import sys
from unittest.mock import patch


def generate_startup(cases):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cc8-assistant/host'))
    from bridge import Bridge
    from failure_feedback import feedback
    from protocol import EVENT, receive, decode_event
    from tools.qa_faults import STARTUP_CASES

    if (
        not isinstance(cases, tuple)
        or not cases
        or len(set(cases)) != len(cases)
        or any(case not in STARTUP_CASES for case in cases)
    ):
        raise ValueError('Unknown startup case selection')

    class EndWorker(BaseException):
        pass

    class Brain:
        calls = 0
        last_metrics = {}

        def cancel(self):
            pass

        def clauses(self, text, history, current, on_web):
            self.calls += 1
            self.last_metrics = {'completed': True}
            on_web('dot_result', {'text': '自动恢复检查已完成。', 'truncated': False})
            yield '自动恢复检查已完成。'

    class NoAudio:
        def cancel(self):
            pass

        def stream(self, *args, **kwargs):
            raise AssertionError('Startup QA must not synthesize audio')

        def recognize(self, *args, **kwargs):
            raise AssertionError('Startup QA must not recognize audio')

    class Reader:
        def __init__(self, data):
            self.data = io.BytesIO(data)

        def recv(self, count):
            return self.data.read(count)

    packets = []

    class Recorder:
        kind = 'usb'

        def sendall(self, raw):
            packet = receive(Reader(raw))
            if packet.kind != EVENT:
                raise AssertionError('Startup QA emitted unexpected audio')
            packets.append(packet)

    # One receiver replays the entire corpus. Preserve controller-owned
    # generations and completed receipts across cases, as normal requests do.
    with patch('threading.Thread.start'):
        host = Bridge(NoAudio(), Brain())
    host.session.connection = Recorder()
    host.log = lambda *args, **kwargs: None
    host.save_status = lambda: None
    host.speak = lambda *args: True
    rows = []
    for case in cases:
        packets.clear()
        before = host.generation
        calls_before = host.brain.calls
        tasks_before = host.workspace.snapshot()['tasks']
        original_event, original_enqueue = host.event, host.enqueue_response

        def event(name, **fields):
            if name == 'text_begin':
                raise OSError('qa-injected-sensitive-body')
            original_event(name, **fields)

        def uncertain(job):
            original_enqueue(job)
            raise OSError('qa-injected-sensitive-body')

        def prepare(_):
            raise OSError('qa-injected-sensitive-body')

        with ExitStack() as stack:
            if case.stage == 'text_begin':
                stack.enter_context(patch.object(host, 'event', side_effect=event))
            elif case.stage == 'text_queue':
                stack.enter_context(
                    patch.object(host, 'enqueue_response', side_effect=queue.Full())
                )
            elif case.stage == 'text_queue_uncertain':
                stack.enter_context(patch.object(host, 'enqueue_response', side_effect=uncertain))
            elif case.stage == 'text_cancel':
                stack.enter_context(patch.object(host.brain, 'cancel', side_effect=RuntimeError()))
            elif case.stage == 'announcement_queue':
                stack.enter_context(patch.object(host.response, 'offer', side_effect=queue.Full()))
            elif case.stage == 'announcement_cancel':
                stack.enter_context(patch.object(host.brain, 'cancel', side_effect=RuntimeError()))
            if case.stage.startswith('text_'):
                host.on_event({'event': 'text_request', 'text': '自动启动故障检查'})
            else:
                try:
                    host.announcements.announce(
                        '自动启动故障检查',
                        prepare=prepare if case.stage == 'announcement_prepare' else None,
                    )
                except (OSError, RuntimeError):
                    pass
                else:
                    raise AssertionError('Startup fault was not raised')

        # Drain the ambiguous admission through the actual serial worker. A
        # cleared text flag alone is insufficient if recording kept it valid.
        while not host.response.queue.empty():
            host.response.respond_job(host.response.queue.get_nowait())
        if (
            host.session.active
            or host.session.text_generation is not None
            or host.announcements.generation is not None
            or host.brain.calls != calls_before
            or host.workspace.snapshot()['tasks'] != tasks_before
        ):
            raise AssertionError('Startup failure retained work or capture')
        errors = [
            p
            for p in packets
            if (e := decode_event(p.data)).get('event') == 'status' and e.get('phase') == 'error'
        ]
        stage = 'transport' if case.stage == 'text_begin' else 'runtime'
        prefix = '本次文字指令未启动。' if case.stage.startswith('text_') else '本次播报未启动。'
        expected = prefix + feedback(stage, case.kind).message
        if (
            len(errors) != 1
            or decode_event(errors[0].data)['message'] != expected
            or errors[0].generation != host.generation
            or host.generation <= before
        ):
            raise AssertionError('Startup failure guidance or ownership mismatch')
        diagnostic = host.diagnostics.report()['requests'][-1]
        if diagnostic['outcome'] != 'failed' or diagnostic['error'] != {
            'stage': stage,
            'kind': case.kind,
        }:
            raise AssertionError('Startup diagnostic mismatch')
        failed_packets = list(packets)
        packets.clear()
        host.on_event({'event': 'text_request', 'text': '自动恢复检查'})
        job = host.response.queue.get_nowait()
        with patch.object(host.response.queue, 'get', side_effect=[job, EndWorker]):
            try:
                host.response.work()
            except EndWorker:
                pass
        if (
            host.brain.calls != calls_before + 1
            or host.workspace.tasks[-1]['status'] != 'completed'
            or host.session.text_generation is not None
            or host.session.active
            or not host.response.queue.empty()
        ):
            raise AssertionError('Explicit recovery did not complete once')
        if 'qa-injected-sensitive-body' in json.dumps(
            [decode_event(p.data) for p in failed_packets + packets]
        ):
            raise AssertionError('Private fixture error leaked into UI')
        rows.append(
            {
                'case': case,
                'error': failed_packets,
                'stale_error': errors[0],
                'recovery': list(packets),
                'expected_task': None,
                'expected_message': expected,
                'provider_requests': 0,
                'http_fixture': 'fake',
                'dot_fixture': 'fake',
                'dot_wire_counts': {},
                'wire_requests': 0,
                'recovery_wire_requests': 0,
                'startup_task_created': False,
                'explicit_recovery_dispatches': host.brain.calls - calls_before,
            }
        )
    return rows
