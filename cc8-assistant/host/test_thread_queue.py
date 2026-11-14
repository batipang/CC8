"""Actual bounded reader and registered-source controller; no original queue mutation."""

from copy import deepcopy
import json
import tempfile
import unittest

from dot_client import Cancelled, DotError
from thread_queue import ThreadQueueReader, queue_text, PAGE_SIZE, MAX_PAGES
import test_report_sources_controller as controllers
from test_report_sources import entry
from protocol import validate_device_event
from protocol import Packet, MAX_PACKET


def row(ident='queued-1', text='继续检查，暂未验证'):
    return dict(
        id=ident,
        clientUserMessageId='message-1',
        input=[dict(type='text', text=text)],
        raw='PRIVATE_EXTRA',
    )


class QueueFixture:
    def __init__(self):
        self.pages = [dict(data=[row()], nextCursor=None)]
        self.calls, self.clients = [], []
        self.after = lambda: None
        self.failed = False
        self.now = 0
        self.advance = 0
        self.failed_start = False
        self.start_timeout = None
        self.start_delay = 0
        self.after_close = lambda: None

    def factory(self):
        parent = self

        class Rpc:
            closed = False

            def start(self, *, timeout):
                parent.start_timeout = timeout
                parent.now += parent.start_delay
                if parent.failed_start:
                    raise DotError('PRIVATE_PROVIDER')

            def request(self, method, params, **kwargs):
                assert method == 'thread/queue/list', (
                    'Queue inspection must not start/resume/write a thread'
                )
                parent.calls.append((method, deepcopy(params), kwargs))
                assert params['threadId'] == 'chat-a'
                assert params['limit'] == PAGE_SIZE
                assert 0 < kwargs['timeout'] <= 8
                if parent.failed:
                    raise DotError('PRIVATE_PROVIDER')
                parent.now += parent.advance
                result = parent.pages[int(params.get('cursor', '0'))]
                parent.after()
                return result

            def close(self):
                self.closed = True
                parent.after_close()

        rpc = Rpc()
        self.clients.append(rpc)
        return rpc

    def read(self, **kwargs):
        return ThreadQueueReader(self.factory, clock=lambda: self.now, wall_clock=lambda: 500).read(
            entry(), **kwargs
        )


class QueueReaderTests(unittest.TestCase):
    def test_maximum_unicode_queue_and_metadata_fit_the_actual_control_frame(self):
        pages = [
            dict(
                data=[
                    row(f'{i * PAGE_SIZE + n:02d}' + 'q' * 94, '😀' * 1000)
                    for n in range(PAGE_SIZE)
                ],
                nextCursor=str(i + 1) if i + 1 < MAX_PAGES else None,
            )
            for i in range(MAX_PAGES)
        ]
        for page in pages:
            for item in page['data']:
                item['clientUserMessageId'] = 'c' * 96
                item['input'].append(dict(type='image', url='PRIVATE_ATTACHMENT'))

        class Rpc:
            def start(self, *, timeout):
                pass

            def request(self, method, params, **kwargs):
                assert method == 'thread/queue/list'
                return pages[int(params.get('cursor', '0'))]

            def close(self):
                pass

        source = dict(
            provider='codex', host='local', thread_id='s' * 96, title='😀' * 80, project='😀' * 80
        )
        result = ThreadQueueReader(Rpc).read(source)
        self.assertEqual(len(result['items']), PAGE_SIZE * MAX_PAGES)
        packet = Packet.event(
            'dot_chat_state',
            request_id='a' * 32,
            ok=True,
            action='source_queue',
            source_title='😀' * 80,
            report_source_title='😀' * 80,
            mode='summary_handoff',
            shared_chat=False,
            history_persisted=False,
            work_reports_supported=True,
            registered_sources_supported=True,
            report_source_available=True,
            report_scope='selected_chat',
            local_project_available=True,
            source_queue=result,
            queue_text=queue_text(result),
        )
        self.assertLessEqual(len(packet.data), MAX_PACKET)
        self.assertNotIn(b'PRIVATE_ATTACHMENT', packet.data)

    def test_projection_uses_only_selected_queue_and_skips_attachments_and_extra_fields(self):
        f = QueueFixture()
        value = f.pages[0]['data'][0]
        value['input'] += [
            dict(type='localImage', path='PRIVATE_FILE'),
            dict(type='image', url='PRIVATE_URL'),
        ]
        result = f.read()
        self.assertTrue(result['coverage_complete'])
        self.assertTrue(result['items'][0]['other_input_not_read'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertIn('不发送', queue_text(result))
        self.assertIn('没有读取附件', queue_text(result))
        self.assertEqual(len(f.calls), 1)
        self.assertTrue(all(c.closed for c in f.clients))

    def test_confirmed_empty_queue_never_implies_idle_or_completed(self):
        f = QueueFixture()
        f.pages[0]['data'] = []
        result = f.read()
        self.assertEqual(result['state'], 'read')
        self.assertTrue(result['coverage_complete'])
        self.assertIn('不代表原任务已经结束', queue_text(result))

    def test_unavailable_and_malformed_first_page_never_become_zero_work(self):
        for page in (
            None,
            {},
            dict(data='wrong'),
            dict(data=[row(), row()]),
            dict(data=[dict(row(), id='../bad')]),
            dict(data=[dict(row(), input='PRIVATE')]),
        ):
            f = QueueFixture()
            f.pages = [page]
            result = f.read()
            self.assertEqual(result['state'], 'unavailable')
            self.assertFalse(result['coverage_complete'])
            self.assertIn('不能当作零条', queue_text(result))
        for start in (True, False):
            f = QueueFixture()
            f.failed_start = start
            f.failed = not start
            self.assertEqual(f.read()['state'], 'unavailable')
            self.assertTrue(all(c.closed for c in f.clients))

    def test_page_cap_cursor_cycle_and_mid_read_failures_keep_partial_coverage_visible(self):
        f = QueueFixture()
        f.pages = [dict(data=[row(f'q-{i}')], nextCursor=str(i + 1)) for i in range(MAX_PAGES)]
        result = f.read()
        self.assertEqual(len(f.calls), MAX_PAGES)
        self.assertEqual(result['reason'], 'page_limit')
        self.assertEqual(result['state'], 'partial')
        self.assertIn('至少读到4条', queue_text(result))
        f = QueueFixture()
        f.pages = [dict(data=[row('q-1')], nextCursor='1'), dict(data=[row('q-2')], nextCursor='1')]
        self.assertEqual(f.read()['reason'], 'cursor_cycle')
        self.assertEqual(len(f.calls), 2)
        f = QueueFixture()
        f.pages = [
            dict(data=[row('q-1')], nextCursor='1'),
            dict(data=[row('q-1')], nextCursor=None),
        ]
        result = f.read()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(len(result['items']), 1)

    def test_deadline_and_cancellation_never_read_later_pages_or_leak_the_client(self):
        f = QueueFixture()
        f.advance = 13
        f.pages = [
            dict(data=[row('q-1')], nextCursor='1'),
            dict(data=[row('q-2')], nextCursor=None),
        ]
        result = f.read()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['reason'], 'deadline')
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(all(c.closed for c in f.clients))
        f = QueueFixture()
        current = [True]
        f.after = lambda: current.__setitem__(0, False)
        with self.assertRaises(Cancelled):
            f.read(is_current=lambda: current[0])
        self.assertEqual(len(f.calls), 1)
        self.assertTrue(all(c.closed for c in f.clients))
        f = QueueFixture()
        with self.assertRaises(Cancelled):
            f.read(is_current=lambda: False)
        self.assertEqual(f.clients, [])

    def test_initialization_consumes_queue_read_budget(self):
        f = QueueFixture()
        f.start_delay = 19
        f.advance = 6
        result = f.read()
        self.assertEqual(f.start_timeout, 20)
        self.assertEqual(f.calls[0][2]['timeout'], 6)
        self.assertEqual(result['reason'], 'deadline')
        self.assertFalse(result['coverage_complete'])
        self.assertTrue(f.clients[0].closed)

    def test_unicode_controls_truncation_and_source_mutation_cannot_poison_output(self):
        f = QueueFixture()
        f.pages[0]['data'] = [row(text='\ud800\x00\n' + '汉' * 500)]
        source = entry()
        f.after = lambda: source.update(thread_id='other-chat')
        result = ThreadQueueReader(f.factory).read(source)
        self.assertEqual(result['source']['thread_id'], 'chat-a')
        self.assertLessEqual(len(result['items'][0]['preview']), 160)
        self.assertTrue(result['items'][0]['preview_truncated'])
        self.assertNotIn('\x00', queue_text(result))
        queue_text(result).encode('utf-8')

    def test_cancellation_during_owned_client_close_cannot_return_a_current_snapshot(self):
        f = QueueFixture()
        current = [True]
        f.after_close = lambda: current.__setitem__(0, False)
        with self.assertRaises(Cancelled):
            f.read(is_current=lambda: current[0])
        self.assertTrue(all(c.closed for c in f.clients))


class QueueControllerTests(unittest.TestCase):
    def prepare(self, folder):
        helper = controllers.SourcesControllerTests()
        b = helper.session(folder)
        state = b.chats.sources.apply('register', 0, entry=entry())
        self.registration = state['items'][0]['id']
        self.fixture = QueueFixture()
        b.chats.catalog.rpc_factory = self.fixture.factory
        return helper, b

    def test_real_dispatch_reads_only_registered_member_without_handoff_or_store_change(self):
        with tempfile.TemporaryDirectory() as folder:
            helper, b = self.prepare(folder)
            import test_work_reports as reports

            selected = reports.ReportTests.source
            b.chats.selection = selected
            b.brain.dot.handoff = selected
            self.assertTrue(
                b.chats.voice.remember(selected, reports.ReportTests().report(reports.Fixture()))
            )
            original_snapshot, original_progress = b.chats.voice.snapshot, b.chats.voice.progress
            raw = b.chats.sources.path.read_bytes()
            helper.request(b, 'source_queue', revision=1, registration_id=self.registration)
            reply = b.events[-1][1]
            self.assertTrue(reply['ok'])
            self.assertEqual(reply['source_queue']['source']['thread_id'], 'chat-a')
            self.assertEqual(b.chats.sources.path.read_bytes(), raw)
            self.assertIs(b.chats.selection, selected)
            self.assertIs(b.brain.dot.handoff, selected)
            self.assertIs(b.chats.voice.snapshot, original_snapshot)
            self.assertIs(b.chats.voice.progress, original_progress)
            self.assertFalse(b.session.active)
            self.assertEqual(b.workspace.tasks, [])

    def test_stale_or_removed_registration_never_initializes_rpc(self):
        with tempfile.TemporaryDirectory() as folder:
            helper, b = self.prepare(folder)
            helper.request(b, 'source_queue', revision=0, registration_id=self.registration)
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual(self.fixture.clients, [])
            b.chats.sources.apply('clear', 1)
            helper.request(b, 'source_queue', revision=2, registration_id=self.registration)
            self.assertFalse(b.events[-1][1]['ok'])
            self.assertEqual(self.fixture.clients, [])

    def test_external_removal_or_route_change_stops_paging_and_rejects_old_result(self):
        for changed in ('remove', 'route'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder:
                helper, b = self.prepare(folder)
                self.fixture.pages = [dict(data=[row()], nextCursor='1')]

                def change():
                    if changed == 'remove':
                        b.chats.sources.apply('clear', 1)
                    else:
                        b.session.connection = object()

                self.fixture.after = change
                helper.request(b, 'source_queue', revision=1, registration_id=self.registration)
                self.assertEqual(len(self.fixture.calls), 1)
                self.assertTrue(all(c.closed for c in self.fixture.clients))
                self.assertFalse(any(fields.get('source_queue') for _, fields in b.events))

    def test_wire_cannot_supply_thread_path_body_or_implicit_target(self):
        base = dict(
            event='dot_chat_request',
            request_id='a' * 32,
            action='source_queue',
            revision=1,
            registration_id='b' * 16,
        )
        validate_device_event(base)
        for change in (
            dict(thread_id='other-chat'),
            dict(text='PRIVATE'),
            dict(revision=True),
            dict(registration_id='../x'),
            dict(confirmed=True),
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_device_event(dict(base, **change))
