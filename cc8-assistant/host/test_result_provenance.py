"""Receipt provenance, restart compatibility and explicit non-executing file location."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from dot_client import DotBrain
from dot_handoff import Handoff
from file_receipts import FileReceipts
from result_provenance import MAX_BYTES, MAX_FILES, validate
from result_files import reveal_file
from protocol import Packet, validate_device_event
from reply_types import Reply
from saved_tasks import SavedTasks
from workspace import Workspace
import test_dot_receipt_delivery as receipt_fixtures
import test_failure_feedback as response_fixtures
import test_saved_tasks as saved_fixtures
import test_report_voice as report_fixtures
from test_saved_tasks import task


def item(path='result.txt', ident='file-item', status='completed', kind='update', move=None):
    return dict(
        type='fileChange',
        id=ident,
        status=status,
        changes=[
            dict(
                path=path,
                kind=dict(type=kind, move_path=move) if kind == 'update' else dict(type=kind),
                diff='PRIVATE_DIFF_NOT_RETAINED',
            )
        ],
    )


def event(value=None, *, thread='thread', turn='turn', completed=False):
    return dict(
        method='turn/completed' if completed else 'item/completed',
        params=dict(threadId=thread, turn=dict(id=turn, status='completed'))
        if completed
        else dict(threadId=thread, turnId=turn, item=value),
    )


def provenance(path='/tmp/result.txt'):
    collector = FileReceipts('thread', '/tmp', None, lambda: True, lambda _: None)
    collector.bind('turn')
    collector.feed(event(item(path)))
    return collector.snapshot()


class CollectorTests(unittest.TestCase):
    def collector(self, **kwargs):
        self.updates = []
        return FileReceipts(
            'thread',
            '/tmp/project',
            kwargs.get('handoff'),
            kwargs.get('valid', lambda: True),
            self.updates.append,
        )

    def test_before_ack_receipts_are_projected_and_bound_to_exact_thread_and_turn(self):
        collector = self.collector()
        collector.feed(event(item('actual.txt')))
        collector.feed(event(item('/private/foreign', ident='other'), turn='another-turn'))
        collector.feed(event(item('/private/foreign', ident='other'), thread='another-thread'))
        collector.bind('turn')
        value = collector.snapshot()
        self.assertEqual(value['files'][0]['path'], '/tmp/project/actual.txt')
        self.assertEqual(len(value['files']), 1)
        self.assertNotIn('PRIVATE_DIFF', str(collector.__dict__))
        self.assertNotIn('another-thread', str(value))
        self.assertIs(validate(value), value)

    def test_foreign_cancelled_and_post_terminal_receipts_do_not_change_snapshot(self):
        current = [True]
        collector = self.collector(valid=lambda: current[0])
        collector.bind('turn')
        collector.feed(event(item('first')))
        initial = collector.snapshot()
        collector.feed(event(item('foreign'), turn='other'))
        current[0] = False
        collector.feed(event(item('cancelled')))
        current[0] = True
        collector.feed(event(completed=True))
        collector.feed(event(item('late')))
        self.assertEqual(collector.snapshot(), initial)

    def test_duplicate_conflict_never_claims_a_confirmed_file_and_is_disclosed(self):
        collector = self.collector()
        collector.bind('turn')
        collector.feed(event(item('one')))
        count = len(self.updates)
        collector.feed(event(item('one')))
        self.assertEqual(len(self.updates), count)
        collector.feed(event(item('changed')))
        self.assertEqual(collector.snapshot()['files'], [])
        self.assertTrue(collector.snapshot()['files_truncated'])

    def test_failure_decline_move_and_deleted_receipts_are_not_rewritten_as_success(self):
        collector = self.collector(handoff=Handoff('original-chat', '原任务', (), False, '/tmp'))
        collector.bind('turn')
        for number, (status, kind, move) in enumerate(
            (
                ('failed', 'add', None),
                ('declined', 'update', None),
                ('completed', 'delete', None),
                ('completed', 'update', 'moved.txt'),
            )
        ):
            collector.feed(event(item('source.txt', str(number), status, kind, move)))
        value = collector.snapshot()
        self.assertEqual(
            [x['status'] for x in value['files']], ['failed', 'declined', 'completed', 'completed']
        )
        self.assertEqual(value['files'][3]['move_to'], '/tmp/project/moved.txt')
        self.assertEqual(value['original_source']['thread_id'], 'original-chat')
        self.assertTrue(value['ephemeral'])

    def test_malformed_or_excessive_metadata_is_bounded_and_disclosed_without_prose_inference(self):
        collector = self.collector()
        collector.bind('turn')
        for number in range(40):
            collector.feed(event(item('/tmp/' + str(number), str(number))))
        value = collector.snapshot()
        self.assertEqual(len(value['files']), MAX_FILES)
        self.assertTrue(value['files_truncated'])
        self.assertLessEqual(len(json.dumps(value, ensure_ascii=False).encode()), MAX_BYTES)
        for path in ('file:///etc/passwd', 'a\nb', '\ud800', 'x' * 513):
            collector.feed(event(item(path, 'malformed')))
        self.assertNotIn('/etc/passwd', str(collector.snapshot()))
        collector.feed(
            event(dict(type='agentMessage', id='answer', text='保存到了 /tmp/hallucinated.txt'))
        )
        self.assertNotIn('hallucinated', str(collector.snapshot()))

    def test_early_budget_does_not_retain_diff_or_unbounded_provider_payload(self):
        collector = self.collector()
        for number in range(40):
            value = item('/tmp/' + str(number), str(number))
            value['changes'][0]['diff'] = 'D' * 100000
            collector.feed(event(value))
        self.assertLess(len(str(collector.__dict__)), 50000)
        collector.bind('turn')
        self.assertTrue(collector.snapshot()['files_truncated'])

    def test_large_escaped_paths_drop_whole_entries_and_workspace_stays_on_wire_budget(self):
        collector = self.collector()
        collector.bind('turn')
        for number in range(8):
            value = item(
                '/tmp/' + str(number) + '"' * 490,
                str(number),
                move='/tmp/moved' + str(number) + '"' * 480,
            )
            collector.feed(event(value))
        data = collector.snapshot()
        self.assertTrue(data['files_truncated'])
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False).encode()), MAX_BYTES)
        self.assertTrue(all(row['path'].endswith('"' * 490) for row in data['files']))
        workspace = Workspace()
        for generation in range(12):
            ident = workspace.task_start(generation, '标题' * 40)
            workspace.task_provenance(ident, data)
            workspace.task_finish(ident, 'completed', '结果' * 800)
        self.assertLess(len(Packet.event('workspace', **workspace.snapshot()).encode()), 65536)

    def test_unsupported_future_identifier_disables_optional_metadata_not_the_rpc(self):
        collector = self.collector()
        collector.bind('unsupported/id')
        self.assertTrue(collector.finished)
        self.assertEqual(self.updates, [])


class ProvenanceIntegrationTests(unittest.TestCase):
    def test_missing_original_project_uses_actual_execution_cwd_for_relative_receipt(self):
        from dot_client import ROOT

        rpc = receipt_fixtures.ReceivingRpc()
        updates = []
        brain = DotBrain(rpc)
        brain.set_handoff(Handoff('original-chat', '原来源', (), False, None))
        stream = brain.clauses('明确的当前请求', [], on_web=lambda k, v: updates.append((k, v)))
        try:
            next(stream)
            rpc.event('item/completed', item=item('actual-result.txt'))
            data = next(value for kind, value in reversed(updates) if kind == 'result_provenance')
            self.assertEqual(data['files'][0]['path'], str(ROOT / 'actual-result.txt'))
            self.assertEqual(data['original_source']['thread_id'], 'original-chat')
            rpc.complete()
            list(stream)
        finally:
            stream.close()

    def test_report_metadata_display_failure_cannot_prevent_the_read_receipt(self):
        _, bridge = report_fixtures.IntegrationTests().session()
        bridge.speak = lambda *args: True
        original = bridge.event

        def event(name, **fields):
            tasks = fields.get('tasks', [])
            if (
                name == 'workspace'
                and tasks
                and tasks[0]['status'] == 'running'
                and 'provenance' in tasks[0]
            ):
                raise OSError('PRIVATE_DISPLAY_ERROR')
            return original(name, **fields)

        bridge.event = event
        response_fixtures.FailureFeedbackTests().run_one(
            bridge.response, Reply(bridge.generation, '汇报今天的工作')
        )
        task = bridge.workspace.snapshot()['tasks'][0]
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['provenance']['provider'], 'work_report')
        self.assertNotIn('PRIVATE_DISPLAY_ERROR', str(bridge.logs))

    def test_source_metadata_and_paths_do_not_enter_saved_status(self):
        helper = response_fixtures.FailureFeedbackTests()
        bridge = helper.session()
        ident = bridge.workspace.task_start(bridge.generation, '固定任务')
        data = provenance('/tmp/PRIVATE_FILE_PATH')
        data['original_source'] = dict(
            thread_id='PRIVATE_ORIGINAL_CHAT', title='PRIVATE_SOURCE_TITLE'
        )
        bridge.workspace.task_provenance(ident, data)
        bridge.workspace.task_finish(ident, 'completed', 'PRIVATE_RESULT_BODY')
        with tempfile.TemporaryDirectory() as folder:
            bridge.reporting.hooks = replace(
                bridge.reporting.hooks,
                private_directory=lambda: Path(folder),
                dot_runtime=lambda: {},
            )
            bridge.reporting.save_status()
            status = (Path(folder) / 'status.json').read_text()
        for value in (
            'PRIVATE_FILE_PATH',
            'PRIVATE_ORIGINAL_CHAT',
            'PRIVATE_SOURCE_TITLE',
            'PRIVATE_RESULT_BODY',
        ):
            self.assertNotIn(value, status)

    def test_completed_file_operation_is_retained_when_the_request_later_fails(self):
        rpc = receipt_fixtures.ReceivingRpc()
        helper = response_fixtures.FailureFeedbackTests()
        bridge = helper.session(brain=DotBrain(rpc))

        def speak(text, generation):
            rpc.event('item/completed', item=item('/tmp/partial-result.txt'))
            rpc.complete(status='failed')
            return False

        bridge.speak = speak
        helper.run_one(bridge.response, Reply(bridge.generation, '固定请求'))
        value = bridge.workspace.snapshot()['tasks'][0]
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['provenance']['files'][0]['status'], 'completed')
        self.assertEqual(value['provenance']['files'][0]['path'], '/tmp/partial-result.txt')

    def test_display_disconnect_in_optional_metadata_cannot_drop_early_completion(self):
        class Early(receipt_fixtures.ReceivingRpc):
            def request(self, method, params, timeout=15):
                result = super().request(method, params, timeout)
                if method == 'turn/start':
                    self.event('item/completed', item=item('/tmp/early.txt'))
                    self.complete()
                return result

        rpc = Early()
        completed = []

        def update(kind, value):
            if kind == 'result_provenance':
                raise OSError('PRIVATE_SEND_FAILURE')
            if kind == 'dot_result':
                completed.append(value)

        result = list(DotBrain(rpc).clauses('fixed', [], on_web=update))
        self.assertTrue(result)
        self.assertEqual(len(completed), 1)
        self.assertEqual(rpc.listeners, [])

    def test_receive_receipt_while_speech_suspended_and_completion_freezes_it(self):
        rpc = receipt_fixtures.ReceivingRpc()
        updates = []
        brain = DotBrain(rpc)
        stream = brain.clauses('固定请求', [], on_web=lambda k, v: updates.append((k, v)))
        try:
            self.assertEqual(next(stream), '这是第一句。')
            rpc.event('item/completed', item=item('/tmp/actual.txt'))
            source = next(v for k, v in reversed(updates) if k == 'result_provenance')
            self.assertEqual(source['thread_id'], 'voice-thread')
            self.assertEqual(source['turn_id'], 'stream-turn')
            self.assertEqual(source['files'][0]['path'], '/tmp/actual.txt')
            rpc.complete()
            rpc.event('item/completed', item=item('/tmp/late.txt', 'late'))
            self.assertEqual(list(stream), ['这是第二句。'])
            source = next(v for k, v in reversed(updates) if k == 'result_provenance')
            self.assertEqual(len(source['files']), 1)
            self.assertLess(
                next(i for i, (k, v) in enumerate(updates) if k == 'result_provenance'),
                next(i for i, (k, v) in enumerate(updates) if k == 'dot_result'),
            )
        finally:
            stream.close()
        self.assertEqual(rpc.listeners, [])

    def test_real_response_task_retains_source_and_finish_time_after_narration_stops(self):
        rpc = receipt_fixtures.ReceivingRpc()
        helper = response_fixtures.FailureFeedbackTests()
        bridge = helper.session(brain=DotBrain(rpc))

        def speak(text, generation):
            rpc.event('item/completed', item=item('/tmp/receipt.txt'))
            rpc.complete()
            bridge.session.generation += 1
            return False

        bridge.speak = speak
        helper.run_one(bridge.response, Reply(bridge.generation, '固定请求'))
        record = bridge.workspace.snapshot()['tasks'][0]
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['provenance']['files'][0]['path'], '/tmp/receipt.txt')
        self.assertIn('+08:00', record['finished_at'])
        previous = deepcopy(record)
        bridge.workspace.task_finish(record['id'], 'cancelled')
        bridge.workspace.task_provenance(record['id'], provenance('/tmp/late'))
        self.assertEqual(bridge.workspace.snapshot()['tasks'][0], previous)

    def test_explicit_save_restart_old_schema_read_and_new_metadata_are_private(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o700)
            path = root / 'results.json'
            legacy = SavedTasks(path).save(task())
            path.write_text(json.dumps({'schema_version': 1, 'items': [legacy]}))
            path.chmod(0o600)
            before = path.read_bytes()
            store = SavedTasks(path)
            self.assertEqual(store.snapshot(), [legacy])
            self.assertEqual(path.read_bytes(), before)
            value = task(2)
            value.update(provenance=provenance(), finished_at='2026-11-09T08:00:00+08:00')
            saved = store.save(value)
            self.assertEqual(json.loads(path.read_text())['schema_version'], 2)
            self.assertEqual(SavedTasks(path).snapshot()[-1], saved)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(store.delete(saved['id']))
            self.assertNotIn('/tmp/result.txt', path.read_text())

    def test_invalid_metadata_cannot_replace_saved_record_or_mutate_completed_task(self):
        value = provenance()
        for invalid in (
            {**value, 'thread_id': '../../bad'},
            {**value, 'provider': 'model_text'},
            {**value, 'files': value['files'] * 9},
        ):
            with self.assertRaises(ValueError):
                validate(invalid)
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                root.chmod(0o700)
                path = root / 'saved.json'
                store = SavedTasks(path)
                store.save(task())
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    store.save({**task(2), 'provenance': invalid})
                self.assertEqual(path.read_bytes(), before)
        workspace = Workspace()
        ident = workspace.task_start(1, 'source')
        workspace.task_provenance(ident, value)
        workspace.task_finish(ident, 'completed', 'result')
        self.assertIsNot(workspace.snapshot()['tasks'][0]['provenance'], value)


class RevealTests(unittest.TestCase):
    def setUp(self):
        darwin = patch('result_files.sys.platform', 'darwin')
        darwin.start()
        self.addCleanup(darwin.stop)

    def test_non_mac_does_not_launch_another_program_named_open(self):
        with (
            patch('result_files.sys.platform', 'linux'),
            patch('result_files.subprocess.Popen') as launch,
            self.assertRaises(OSError),
        ):
            reveal_file(dict(provenance=provenance()), 0, launch=launch)
        launch.assert_not_called()

    def test_argv_is_single_absolute_receipt_path_without_shell_or_content_open(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'name $(touch NEVER) `date` .command'
            path.write_text('fixed')
            calls = []

            def launch(args, **kwargs):
                calls.append((args, kwargs))
                return SimpleNamespace(wait=lambda **_: 0)

            reveal_file(dict(provenance=provenance(str(path))), 0, launch=launch)
            self.assertEqual(calls[0][0], ['/usr/bin/open', '-R', str(path)])
            self.assertNotIn('shell', calls[0][1])
            self.assertFalse((Path(folder) / 'NEVER').exists())

    def test_missing_symlink_directory_deleted_failed_and_invalid_index_never_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file = root / 'file'
            file.write_text('fixed')
            link = root / 'link'
            link.symlink_to(file)
            for path in (root / 'missing', link, root):
                with (
                    self.subTest(path=path),
                    patch('result_files.subprocess.Popen') as launch,
                    self.assertRaises((ValueError, OSError)),
                ):
                    reveal_file(dict(provenance=provenance(str(path))), 0, launch=launch)
                launch.assert_not_called()
            for status, kind, index in (
                ('failed', 'update', 0),
                ('completed', 'delete', 0),
                ('completed', 'update', True),
                ('completed', 'update', 8),
            ):
                value = provenance(str(file))
                value['files'][0].update(status=status, kind=kind)
                with (
                    patch('result_files.subprocess.Popen') as launch,
                    self.assertRaises(ValueError),
                ):
                    reveal_file(dict(provenance=value), index, launch=launch)
                launch.assert_not_called()

    def test_wire_accepts_only_record_id_and_index_and_rejects_client_paths(self):
        value = dict(
            event='saved_task_request',
            request_id='a' * 32,
            action='reveal_live',
            id='1' * 16,
            file_index=0,
        )
        self.assertEqual(validate_device_event(value), value)
        for extra in (
            {'path': '/etc/passwd'},
            {'file_index': True},
            {'file_index': 8},
            {'file_index': -1},
            {'id': '../bad'},
        ):
            with self.assertRaises(ValueError):
                validate_device_event({**value, **extra})

    def test_authenticated_live_and_saved_reveal_no_auto_retry_or_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o700)
            helper = saved_fixtures.ControllerTests()
            helper.store = SavedTasks(root / 'saved.json')
            bridge = helper.session()
            ident = bridge.workspace.task_start(bridge.generation, '固定任务')
            bridge.workspace.task_provenance(ident, provenance())
            bridge.workspace.task_finish(ident, 'completed', 'done')
            calls = []
            bridge.saved_tasks.hooks = replace(
                bridge.saved_tasks.hooks,
                reveal=lambda record, index, **_: calls.append((record, index)),
            )
            helper.request(bridge, 'reveal_live', ident, file_index=0)
            self.assertTrue(bridge.events[-1][1]['revealed'])
            helper.request(bridge, 'reveal_live', ident, file_index=0)
            self.assertFalse(bridge.events[-1][1]['ok'])
            self.assertEqual(len(calls), 1)
            helper.request(bridge, 'save', ident)
            bridge.workspace.tasks.clear()
            bridge.on_event(
                dict(
                    event='saved_task_request',
                    request_id='b' * 32,
                    action='reveal_saved',
                    id=ident,
                    file_index=0,
                )
            )
            bridge.saved_tasks.worker.join(2)
            self.assertTrue(bridge.events[-1][1]['revealed'])
            self.assertEqual(len(calls), 2)
            self.assertFalse(bridge.session.active)
            self.assertTrue(bridge.response.queue.empty())

    def test_route_rebound_during_file_validation_cannot_launch_finder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'result.txt'
            path.write_text('fixed')
            helper = saved_fixtures.ControllerTests()
            helper.store = SavedTasks(root / 'saved.json')
            bridge = helper.session()
            ident = bridge.workspace.task_start(bridge.generation, '固定任务')
            bridge.workspace.task_provenance(ident, provenance(str(path)))
            bridge.workspace.task_finish(ident, 'completed', 'done')

            def rebound(selected):
                bridge.session.bound_route_token = 'new-route'
                return SimpleNamespace(st_mode=0o100600)

            with (
                patch('result_files.Path.lstat', autospec=True, side_effect=rebound),
                patch('result_files.subprocess.Popen') as launch,
            ):
                helper.request(bridge, 'reveal_live', ident, file_index=0)
            launch.assert_not_called()
            self.assertEqual(bridge.events, [])
            self.assertFalse(bridge.saved_tasks.busy)

    def test_finder_wait_does_not_hold_session_lock_and_stale_reply_is_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'result.txt'
            path.write_text('fixed')
            helper = saved_fixtures.ControllerTests()
            helper.store = SavedTasks(root / 'saved.json')
            bridge = helper.session()
            ident = bridge.workspace.task_start(bridge.generation, '固定任务')
            bridge.workspace.task_provenance(ident, provenance(str(path)))
            bridge.workspace.task_finish(ident, 'completed', 'done')
            changed = threading.Event()

            def change_route():
                with bridge.guard:
                    bridge.session.bound_route_token = 'new-route'
                    changed.set()

            def wait(timeout):
                worker = threading.Thread(target=change_route, daemon=True)
                worker.start()
                worker.join(1)
                self.assertTrue(changed.is_set())
                return 0

            process = SimpleNamespace(wait=wait)
            with patch('result_files.subprocess.Popen', return_value=process) as launch:
                helper.request(bridge, 'reveal_live', ident, file_index=0)
            self.assertTrue(changed.is_set())
            launch.assert_called_once()
            self.assertEqual(bridge.events, [])
            self.assertFalse(bridge.saved_tasks.busy)

    def test_disconnect_during_saved_lookup_prevents_side_effect_and_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o700)
            store = SavedTasks(root / 'saved.json')
            value = task()
            value['provenance'] = provenance()
            store.save(value)
            helper = saved_fixtures.ControllerTests()
            helper.store = store
            bridge = helper.session()
            calls = []
            bridge.saved_tasks.hooks = replace(
                bridge.saved_tasks.hooks, reveal=lambda *args, **_: calls.append(args)
            )
            original = store.snapshot

            def snapshot():
                bridge.session.connection = object()
                return original()

            store.snapshot = snapshot
            helper.request(bridge, 'reveal_saved', value['id'], file_index=0)
            self.assertEqual(calls, [])
            self.assertEqual(bridge.events, [])


if __name__ == '__main__':
    unittest.main()
