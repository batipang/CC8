"""Real private files and concurrent writes; no replay, capture or provider calls."""

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from bridge import Bridge
from protocol import Packet, validate_device_event
from saved_tasks import SavedTasks, SavedTaskCapacityError, LIMIT


def task(number=1):
    return {
        'id': f'{number:016x}',
        'generation': 100,
        'title': '合成任务',
        'status': 'completed',
        'result': '真实结果摘录',
        'started_at': '2026-11-02T08:00:00+08:00',
        'steps': [{'id': 'actual-item-1', 'title': '执行步骤', 'status': 'completed'}],
        'unrelated_private_data': 'EXCLUDED_DATA',
    }


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'saved.json'
        self.store = SavedTasks(self.path)


class StoreTests(Fixture):
    def test_mac_cli_lists_and_reads_only_selected_saved_handoffs(self):
        from report_voice import ReportCommand
        from test_report_voice import VoiceTests

        voice = VoiceTests().session()
        voice.answer(ReportCommand('report', 'today'), lambda: True)
        reply = voice.answer(voice.command('继续第二项，下一步：检查失败用例'), lambda: True)
        store = self.root / 'device-access/private/voice-runtime/saved-task-results.json'
        store.parent.mkdir(parents=True, mode=0o700)
        archive = SavedTasks(store)
        archive.save(task(1))
        receipt = archive.save(
            {
                **task(2),
                'result': reply.text,
                'provenance': reply.provenance,
                'steps': [{'id': 'desktop_handoff', 'title': '待桌面接续', 'status': 'completed'}],
            }
        )
        archive.save({**receipt, 'id': task(3)['id'], 'result': reply.text.split('\n\n')[0]})
        before = store.read_bytes()
        entry = Path(__file__).resolve().parents[2] / 'cc8'

        def cli(*args):
            return subprocess.run(
                [
                    sys.executable,
                    str(entry),
                    'saved-handoff',
                    *args,
                    '--runtime-root',
                    str(self.root),
                ],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=5,
            )

        rows = cli('list')
        self.assertEqual(rows.returncode, 0, rows.stderr)
        self.assertEqual(
            [item['id'] for item in json.loads(rows.stdout)], [task(3)['id'], receipt['id']]
        )
        self.assertNotIn('检查失败用例', rows.stdout)
        self.assertNotIn(reply.provenance['thread_id'], rows.stdout)
        selected = cli('show', receipt['id'])
        self.assertEqual(selected.returncode, 0, selected.stderr)
        detail = json.loads(selected.stdout)
        self.assertEqual(detail['next_step'], '检查失败用例')
        self.assertEqual(
            (detail['thread_id'], detail['turn_id']),
            (reply.provenance['thread_id'], reply.provenance['turn_id']),
        )
        self.assertIsNone(json.loads(cli('show', task(3)['id']).stdout)['next_step'])
        self.assertEqual(cli('show', task(1)['id']).returncode, 2)
        self.assertEqual(store.read_bytes(), before)
        store.chmod(0o644)
        refused = cli('show', receipt['id'])
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stdout, '')

    def test_open_handoff_targets_only_a_valid_saved_local_chat(self):
        from tools import saved_handoff

        store = self.root / 'device-access/private/voice-runtime/saved-task-results.json'
        store.parent.mkdir(parents=True, mode=0o700)
        archive = SavedTasks(store)
        archive.save(task(1))

        def save(number, thread_id, source_id):
            return archive.save(
                {
                    **task(number),
                    'result': '私有草稿',
                    'provenance': {
                        'version': 1,
                        'provider': 'work_report',
                        'host': 'local',
                        'thread_id': thread_id,
                        'turn_id': 'original-turn',
                        'ephemeral': False,
                        'original_source': {'thread_id': source_id, 'title': '私有来源'},
                        'files': [],
                        'files_truncated': False,
                    },
                    'steps': [
                        {'id': 'desktop_handoff', 'title': '待桌面接续', 'status': 'completed'}
                    ],
                }
            )['id']

        valid = save(2, 'original-chat', 'original-chat')
        reserved = save(3, 'new', 'new')
        mismatch = save(4, 'another-chat', 'original-chat')
        before = store.read_bytes()

        def invoke(saved_id, platform='darwin'):
            stdout, stderr = StringIO(), StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                patch.object(sys, 'platform', platform),
            ):
                code = saved_handoff.main(['open', saved_id, '--runtime-root', str(self.root)])
            return code, stdout.getvalue(), stderr.getvalue()

        with patch.object(saved_handoff.subprocess, 'Popen') as run:
            run.return_value.wait.return_value = 0
            code, output, _ = invoke(valid)
            self.assertEqual(code, 0)
            self.assertIn('已请求桌面打开', output)
            self.assertNotIn('私有', output)
            run.assert_called_once_with(
                ['/usr/bin/open', '-b', 'com.openai.codex', 'codex://threads/original-chat'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            run.reset_mock()
            for saved_id in (task(1)['id'], reserved, mismatch, task(9)['id'], '../new'):
                self.assertEqual(invoke(saved_id)[:2], (2, ''))
            self.assertEqual(invoke(valid, 'linux')[:2], (2, ''))
            run.assert_not_called()
            run.return_value.wait.return_value = 1
            self.assertEqual(invoke(valid)[:2], (2, ''))
            run.return_value.wait.side_effect = [subprocess.TimeoutExpired('PRIVATE_COMMAND', 5), 1]
            code, output, error = invoke(valid)
            self.assertEqual((code, output), (2, ''))
            self.assertNotIn('PRIVATE_COMMAND', error)
            run.return_value.kill.assert_called_once()
        self.assertEqual(store.read_bytes(), before)

    def test_save_restart_read_delete_and_no_automatic_creation(self):
        self.assertEqual(self.store.snapshot(), [])
        self.assertFalse(self.path.exists())
        first = self.store.save(task())
        self.assertNotIn('generation', first)
        self.assertNotIn('EXCLUDED_DATA', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        reopened = SavedTasks(self.path)
        self.assertEqual(reopened.snapshot(), [first])
        self.assertTrue(reopened.delete(first['id']))
        self.assertEqual(SavedTasks(self.path).snapshot(), [])
        self.assertFalse(reopened.delete(first['id']))

    def test_repeat_save_is_idempotent_and_changed_receipt_cannot_overwrite(self):
        first = self.store.save(task())
        self.assertEqual(self.store.save(task()), first)
        with self.assertRaises(ValueError):
            self.store.save({**task(), 'result': '不同结果'})
        self.assertEqual(self.store.snapshot(), [first])

    def test_only_valid_terminal_receipts_are_saved(self):
        for update in (
            {'status': 'running'},
            {'id': '../other'},
            {'result': 'x' * 1601},
            {'title': '\ud800'},
            {'steps': [{}]},
            {'started_at': 'invalid'},
        ):
            with self.subTest(update=update), self.assertRaises((ValueError, KeyError)):
                self.store.save({**task(), **update})
        self.assertFalse(self.path.exists())

    def test_corrupt_public_and_duplicate_field_stores_are_never_overwritten(self):
        for raw in (b'broken', b'{"schema_version":1,"schema_version":1}', b'x' * (512 * 1024 + 1)):
            self.path.write_bytes(raw)
            self.path.chmod(0o600)
            with self.assertRaises(ValueError):
                self.store.save(task())
            self.assertEqual(self.path.read_bytes(), raw)
        self.path.write_text('{"schema_version":1,"items":[]}')
        self.path.chmod(0o644)
        with self.assertRaises(OSError):
            self.store.save(task())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(OSError):
            SavedTasks(public / 'new.json').save(task())
        self.assertFalse((public / 'new.json').exists())
        self.assertEqual(public.stat().st_mode & 0o777, 0o755)

    def test_links_do_not_redirect_writes(self):
        self.store.save(task())
        before = self.path.read_bytes()
        linked = self.root / 'linked.json'
        linked.symlink_to(self.path)
        with self.assertRaises(OSError):
            SavedTasks(linked).save(task(2))
        linked.unlink()
        os.link(self.path, linked)
        with self.assertRaises(OSError):
            SavedTasks(linked).save(task(2))
        self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_replace_failure_keeps_original_and_cleans_temporary(self):
        self.store.save(task())
        before = self.path.read_bytes()
        with (
            patch('saved_tasks.os.replace', side_effect=OSError('PRIVATE_ERROR')),
            self.assertRaises(OSError),
        ):
            self.store.save(task(2))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_competing_store_instances_preserve_all_records(self):
        barrier, errors = threading.Barrier(8), []

        def save(number):
            try:
                barrier.wait(2)
                SavedTasks(self.path).save(task(number))
            except BaseException as error:
                errors.append(type(error).__name__)

        workers = [threading.Thread(target=save, args=(i,), daemon=True) for i in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.snapshot()), 8)

    def test_external_lock_wait_is_bounded(self):
        lock = self.root / '.saved-task-results.lock'
        lock.touch(mode=0o600)
        with lock.open('r+b') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with patch('saved_tasks.LOCK_WAIT_SECONDS', 0.01), self.assertRaises(TimeoutError):
                self.store.save(task())
        self.assertFalse(self.path.exists())

    def test_full_unicode_capacity_fits_wire_and_never_evicts_selected_results(self):
        self.assertEqual(LIMIT, 32)
        for number in range(LIMIT):
            value = {
                **task(number),
                'title': '😀' * 80,
                'result': '😀' * 1600,
                'steps': [
                    {'id': '😀' * 80, 'title': '😀' * 40, 'status': 'completed'} for _ in range(5)
                ],
            }
            self.store.save(value)
        before = self.path.read_bytes()
        self.assertLess(len(before), 512 * 1024)
        with self.assertRaises(SavedTaskCapacityError):
            self.store.save(task(LIMIT))
        self.assertEqual(self.path.read_bytes(), before)
        items = self.store.snapshot()
        metadata = [{k: i[k] for k in ('id', 'title', 'status', 'saved_at')} for i in items]
        self.assertLess(len(Packet.event('saved_task_result', items=metadata).encode()), 65536)
        self.assertLess(len(Packet.event('saved_task_result', item=items[0]).encode()), 65536)


class ControllerTests(Fixture):
    def test_optional_corrupt_store_does_not_block_handshake_or_start_capture(self):
        path = self.root / 'saved-task-results.json'
        path.write_text('PRIVATE_CORRUPT_CONTENT')
        path.chmod(0o600)
        with patch('bridge.PRIVATE', self.root), patch('threading.Thread.start'):
            b = Bridge(None, None)
        b.session.connection = object()
        b.save_status = lambda: None
        events = []
        b.event = lambda name, **fields: events.append((name, fields))
        with patch.object(
            b.saved_tasks.store, 'snapshot', side_effect=AssertionError('Unrequested archive read')
        ):
            b.on_event({'event': 'device_ready'})
        hello = next(fields for name, fields in events if name == 'hello')
        self.assertTrue(hello['supports_saved_task_results'])
        self.assertFalse(hello['supports_dot_chat_handoff'])
        self.assertFalse(b.session.active)
        self.assertEqual(path.read_text(), 'PRIVATE_CORRUPT_CONTENT')
        self.assertNotIn('PRIVATE_CORRUPT_CONTENT', str(events))

    def session(self):
        with patch('threading.Thread.start'):
            b = Bridge(None, None)
        b.session.connection = object()
        b.save_status = lambda: None
        b.events = []
        b.event = lambda name, **fields: b.events.append((name, fields))
        b.saved_tasks.store = self.store
        return b

    def request(self, b, action, task_id=None, **extra):
        fields = {'event': 'saved_task_request', 'request_id': 'a' * 32, 'action': action, **extra}
        if task_id is not None:
            fields['id'] = task_id
        b.on_event(fields)
        if b.saved_tasks.worker:
            b.saved_tasks.worker.join(2)
            self.assertFalse(b.saved_tasks.worker.is_alive())

    def test_actual_task_roundtrip_ignores_client_text_and_does_not_replay(self):
        b = self.session()
        task_id = b.workspace.task_start(b.generation, '真正的任务')
        b.workspace.task_finish(task_id, 'completed', '实际完成结果')
        self.assertFalse(self.path.exists())
        self.request(b, 'save', task_id, result='FORGED', status='failed')
        self.assertTrue(b.events[-1][1]['ok'])
        self.assertNotIn('FORGED', self.path.read_text())
        b = self.session()
        self.request(b, 'list')
        self.assertEqual(len(b.events[-1][1]['items']), 1)
        self.assertNotIn('result', b.events[-1][1]['items'][0])
        self.request(b, 'get', task_id)
        self.assertEqual(b.events[-1][1]['item']['result'], '实际完成结果')
        self.request(b, 'delete', task_id)
        self.assertTrue(b.events[-1][1]['deleted'])
        self.assertEqual(b.workspace.snapshot()['tasks'], [])
        self.assertTrue(b.response.queue.empty())
        self.assertFalse(b.session.active)

    def test_explicit_handoff_save_and_restart_preserve_pending_state_and_source(self):
        from dot_handoff import Handoff
        from report_voice import report_source

        b = self.session()
        task_id = b.workspace.task_start(b.generation, '继续第二项')
        b.workspace.task_provenance(
            task_id, report_source(Handoff('chat-one', '原聊天', (), False), 'turn-two')
        )
        b.workspace.task_finish(task_id, 'completed', '待桌面接续', desktop_handoff=True)
        self.assertFalse(self.path.exists())
        self.request(b, 'save', task_id)
        restored = self.session()
        self.request(restored, 'list')
        metadata = restored.events[-1][1]['items'][0]
        self.assertIs(metadata['desktop_handoff_pending'], True)
        self.assertNotIn('provenance', metadata)
        self.request(restored, 'get', task_id)
        detail = restored.events[-1][1]['item']
        self.assertIs(detail['desktop_handoff_pending'], True)
        self.assertEqual(detail['provenance']['turn_id'], 'turn-two')
        self.assertNotIn('desktop_handoff_pending', self.path.read_text())
        self.assertTrue(restored.response.queue.empty())

    def test_saved_handoff_opens_only_its_original_chat_once(self):
        from dot_handoff import Handoff
        from report_voice import report_source

        b = self.session()
        task_id = b.workspace.task_start(b.generation, '继续第二项')
        b.workspace.task_provenance(
            task_id, report_source(Handoff('chat-one', '原聊天', (), False), 'turn-two')
        )
        b.workspace.task_finish(task_id, 'completed', '私有草稿', desktop_handoff=True)
        self.request(b, 'save', task_id)
        self.store.save(task(1))
        before = self.path.read_bytes()
        b = self.session()
        launch = Mock()
        launch.return_value.wait.return_value = 0
        b.saved_tasks.hooks = replace(b.saved_tasks.hooks, launch_chat=launch)
        with patch.object(sys, 'platform', 'darwin'):
            self.request(b, 'get', task_id)
        self.assertIs(b.events[-1][1]['item']['desktop_handoff_can_open'], True)
        with self.assertRaises(ValueError):
            self.request(b, 'open_handoff', task_id, thread_id='injected')
        self.request(b, 'open_handoff', task_id)
        self.assertEqual(
            b.events[-1][1],
            {'request_id': 'a' * 32, 'ok': True, 'action': 'open_handoff', 'open_requested': True},
        )
        self.request(b, 'open_handoff', task_id)
        self.assertFalse(b.events[-1][1]['ok'])
        b.on_event(
            {
                'event': 'saved_task_request',
                'request_id': 'b' * 32,
                'action': 'open_handoff',
                'id': task(1)['id'],
            }
        )
        b.saved_tasks.worker.join(2)
        self.assertFalse(b.events[-1][1]['ok'])
        launch.assert_called_once_with('chat-one')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(b.response.queue.empty())

    def test_saved_handoff_cannot_launch_on_a_rebound_route(self):
        from dot_handoff import Handoff
        from report_voice import report_source

        b = self.session()
        self.store.save(
            {
                **task(2),
                'provenance': report_source(Handoff('chat-one', '原聊天', (), False), 'turn-two'),
                'steps': [{'id': 'desktop_handoff', 'title': '待桌面接续', 'status': 'completed'}],
            }
        )
        launch = Mock()
        b.saved_tasks.hooks = replace(b.saved_tasks.hooks, launch_chat=launch)
        snapshot = self.store.snapshot

        def rebound():
            with b.guard:
                b.session.bound_route_token = 'new-route'
            return snapshot()

        with patch.object(self.store, 'snapshot', side_effect=rebound):
            self.request(b, 'open_handoff', task(2)['id'])
        launch.assert_not_called()
        self.assertEqual(b.events, [])

    def test_restart_preserves_unconfirmed_execution_for_failed_or_cancelled_task(self):
        live = self.session()
        cases = [('failed', True), ('cancelled', True), ('failed', False)]
        receipts = {}
        for status, unconfirmed in cases:
            task_id = live.workspace.task_start(live.generation, '核对执行回执')
            live.workspace.task_finish(task_id, status, '回执', unconfirmed=unconfirmed)
            self.request(live, 'save', task_id, execution_unconfirmed=not unconfirmed)
            self.assertTrue(live.events[-1][1]['ok'])
            receipts[task_id] = (status, unconfirmed)
        restored = self.session()
        self.request(restored, 'list')
        for row in restored.events[-1][1]['items']:
            status, unconfirmed = receipts[row['id']]
            self.assertEqual(row['status'], status)
            self.assertIs(row['execution_unconfirmed'], unconfirmed)
            self.request(restored, 'get', row['id'])
            self.assertIs(restored.events[-1][1]['item']['execution_unconfirmed'], unconfirmed)
        self.assertTrue(restored.response.queue.empty())

    def test_explicit_save_freezes_confirmed_narration_separately_from_task_completion(self):
        live = self.session()
        task_id = live.workspace.task_start(live.generation, '有播报的请求')
        live.workspace.task_finish(task_id, 'completed', '已经执行')
        self.assertTrue(live.workspace.task_narration(task_id, 'partial'))
        self.request(live, 'save', task_id)
        self.assertTrue(live.events[-1][1]['ok'])
        self.assertTrue(live.workspace.task_narration(task_id, 'complete'))
        self.assertFalse(live.workspace.task_narration(task_id, 'partial'))
        restored = self.session()
        self.request(restored, 'get', task_id)
        item = restored.events[-1][1]['item']
        self.assertEqual(item['status'], 'completed')
        self.assertEqual(item['steps'][-1]['title'], '结果播报部分确认')
        self.assertEqual(item['result'], '已经执行')

    def test_running_unknown_and_corrupt_records_do_not_change_live_task(self):
        b = self.session()
        task_id = b.workspace.task_start(b.generation, '尚未结束')
        for selected in (task_id, 'f' * 16):
            self.request(b, 'save', selected, status='completed')
            self.assertFalse(b.events[-1][1]['ok'])
        self.assertFalse(self.path.exists())
        b.workspace.task_finish(task_id, 'completed', '已执行')
        self.path.write_text('PRIVATE_CORRUPT_CONTENT')
        self.path.chmod(0o600)
        self.request(b, 'save', task_id)
        self.assertFalse(b.events[-1][1]['ok'])
        self.assertNotIn('PRIVATE_CORRUPT_CONTENT', str(b.events))
        self.assertEqual(self.path.read_text(), 'PRIVATE_CORRUPT_CONTENT')
        self.assertEqual(b.workspace.snapshot()['tasks'][0]['status'], 'completed')

    def test_disconnect_discards_response_but_keeps_explicit_confirmed_write(self):
        b = self.session()
        task_id = b.workspace.task_start(b.generation, '保存任务')
        b.workspace.task_finish(task_id, 'completed', '原结果')
        entered, release = threading.Event(), threading.Event()
        original = self.store.save

        def save(value):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('Write barrier timed out')
            return original(value)

        self.store.save = save
        b.on_event(
            {'event': 'saved_task_request', 'request_id': 'a' * 32, 'action': 'save', 'id': task_id}
        )
        try:
            self.assertTrue(entered.wait(1))
            b.session.connection = object()
        finally:
            release.set()
            b.saved_tasks.worker.join(2)
        self.assertFalse(b.saved_tasks.worker.is_alive())
        self.assertEqual(len(self.store.snapshot()), 1)
        self.assertEqual(b.events, [])
        self.assertEqual(b.workspace.snapshot()['tasks'][0]['status'], 'completed')

    def test_protocol_rejects_bad_nonce_id_and_action(self):
        base = {
            'event': 'saved_task_request',
            'request_id': 'a' * 32,
            'action': 'get',
            'id': 'b' * 16,
        }
        for update in ({'request_id': True}, {'id': '../file'}, {'action': 'resume'}, {'id': 1}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_device_event({**base, **update})

    def test_delete_ack_can_refresh_without_old_cleanup_clearing_the_new_worker(self):
        b = self.session()
        selected = self.store.save(task())
        entered, release = threading.Event(), threading.Event()
        original = self.store.snapshot
        old_workers = []

        def snapshot():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('Refresh barrier timed out')
            return original()

        def event(name, **fields):
            b.events.append((name, fields))
            if fields.get('action') == 'delete':
                old_workers.append(threading.current_thread())
                b.on_event(
                    {'event': 'saved_task_request', 'request_id': 'b' * 32, 'action': 'list'}
                )

        self.store.snapshot = snapshot
        b.event = event
        b.on_event(
            {
                'event': 'saved_task_request',
                'request_id': 'a' * 32,
                'action': 'delete',
                'id': selected['id'],
            }
        )
        try:
            self.assertTrue(entered.wait(1))
            old_workers[0].join(1)
            self.assertFalse(old_workers[0].is_alive())
            self.assertTrue(b.saved_tasks.busy)
        finally:
            release.set()
            b.saved_tasks.worker.join(2)
        self.assertFalse(b.saved_tasks.worker.is_alive())
        self.assertFalse(b.saved_tasks.busy)
        self.assertTrue(b.events[-1][1]['ok'])
        self.assertEqual(b.events[-1][1]['items'], [])


if __name__ == '__main__':
    unittest.main()
