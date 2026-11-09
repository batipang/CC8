"""Actual private files, ownership/revisions, local commands and model inputs; no capture."""

from datetime import datetime
import json
import os
import stat
import threading
import unittest
from unittest.mock import patch

from brain_router import BrainRouter
from dot_client import DotBrain
from minimax_client import MiniMaxBrain, Cancelled
from operation_failure import OperationFailure
from preference_controller import PreferenceController, PreferenceHooks
from preference_memory import PreferenceMemory, command, LIMIT, validate
from protocol import validate_device_event
from reminder_store import ReminderCommitUncertain
from test_dot_client import FakeRpc
from test_memory_fixture import isolated_memory
import test_failure_feedback as fixtures
from reply_types import Reply
from vision import visual_request


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.memory = isolated_memory()
        self.path = self.memory.path

    def put(self, key='称呼', value='固定公开名字', revision=None, source='device'):
        revision = self.memory.snapshot()['revision'] if revision is None else revision
        return self.memory.apply(
            dict(action='put', key=key, value=value, revision=revision), source
        )

    def test_empty_reads_never_create_files_or_locks(self):
        self.assertEqual(self.memory.context(), '')
        self.assertEqual(self.memory.snapshot()['items'], [])
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_atomic_restart_update_provenance_and_canonical_key(self):
        self.memory.clock = lambda: datetime.fromisoformat('2026-11-09T12:00:00+08:00')
        first = self.put(key='Name')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.memory.clock = lambda: datetime.fromisoformat('2026-11-09T12:01:00+08:00')
        last = self.put(key='NAME', value='新名字', source='voice')
        self.assertEqual(len(last['items']), 1)
        item = last['items'][0]
        self.assertEqual((item['key'], item['value'], item['source']), ('Name', '新名字', 'voice'))
        self.assertEqual(item['created_at'], first['items'][0]['created_at'])
        self.assertNotEqual(item['updated_at'], item['created_at'])
        self.assertEqual(PreferenceMemory(self.path).snapshot(), last)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_delete_disable_reenable_and_clear_remove_injected_data(self):
        self.put(value='唯一旧偏好')
        self.assertIn('唯一旧偏好', self.memory.context())
        self.memory.apply(dict(action='set_enabled', enabled=False, revision=1), 'device')
        self.assertEqual(self.memory.context(), '')
        self.assertEqual(len(self.memory.snapshot()['items']), 1)
        self.memory.apply(dict(action='set_enabled', enabled=True, revision=2), 'device')
        self.assertIn('唯一旧偏好', self.memory.context())
        self.memory.apply(dict(action='delete', key='称呼', revision=3), 'voice')
        self.assertEqual(PreferenceMemory(self.path).context(), '')
        self.put(value='第二个公开偏好')
        self.memory.apply(dict(action='clear', revision=5), 'voice')
        self.assertEqual(self.memory.snapshot()['items'], [])

    def test_stale_revision_never_replays_or_overwrites(self):
        self.put()
        before = self.path.read_bytes()
        other = PreferenceMemory(self.path)
        with self.assertRaisesRegex(ValueError, 'revision'):
            other.apply(dict(action='clear', revision=0), 'device')
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_writers_same_revision_only_one_commits(self):
        gate = threading.Barrier(2)
        results = []
        errors = []

        def write(name):
            store = PreferenceMemory(self.path)
            gate.wait(2)
            try:
                store.apply(dict(action='put', key=name, value='公开值', revision=0), 'device')
                results.append(True)
            except ValueError:
                results.append(False)
            except Exception as error:
                errors.append(type(error).__name__)

        workers = [threading.Thread(target=write, args=(str(i),)) for i in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.memory.snapshot()['items']), 1)

    def test_capacity_never_evicts_old_preferences(self):
        for n in range(LIMIT):
            self.put(key=str(n))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'capacity'):
            self.put(key='extra')
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_and_duplicate_schema_never_reset_or_overwrite(self):
        for raw in ('{"schema":1,"schema":1}', '{bad', '{"schema":2}', '[]'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                self.path.chmod(0o600)
                with self.assertRaises(ValueError):
                    self.put()
                self.assertEqual(self.path.read_text(), raw)

    def test_links_and_public_store_are_rejected(self):
        target = self.path.parent / 'fixed.txt'
        target.write_text('fixed owner data')
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.memory.snapshot()
        self.assertEqual(target.read_text(), 'fixed owner data')
        self.path.unlink()
        self.put()
        self.path.chmod(0o644)
        with self.assertRaises((OSError, ValueError)):
            self.memory.snapshot()

    def test_excessively_nested_corrupt_file_is_preserved_and_reported_as_invalid(self):
        raw = '[' * 2000 + '0' + ']' * 2000
        self.path.write_text(raw)
        self.path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.memory.snapshot()
        self.assertEqual(self.path.read_text(), raw)

    def test_unsafe_lock_is_not_followed(self):
        target = self.path.parent / 'fixed.txt'
        target.write_text('fixed owner data')
        (self.path.parent / '.preference-memory.lock').symlink_to(target)
        with self.assertRaises(OSError):
            self.put()
        self.assertFalse(self.path.exists())
        self.assertEqual(target.read_text(), 'fixed owner data')

    def test_post_rename_uncertainty_keeps_real_state_and_old_revision_rejected(self):
        actual = os.fsync
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('fixed directory-sync failure')
            return actual(fd)

        with patch('reminder_store.os.fsync', side_effect=fsync):
            with self.assertRaises(ReminderCommitUncertain):
                self.put()
        self.assertEqual(self.memory.snapshot()['revision'], 1)
        with self.assertRaises(ValueError):
            self.put(revision=0)

    def test_invalid_mutations_and_unicode_do_not_write(self):
        requests = [
            [],
            {},
            dict(action=[], revision=0),
            dict(action='clear', revision=True),
            dict(action='clear', revision=0, extra=True),
            dict(action='set_enabled', revision=0, enabled=1),
        ]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.memory.apply(request, 'device')
        for key, value in (
            ('', 'v'),
            ('x' * 41, 'v'),
            ('a\n', ''),
            ('k', 'v' * 201),
            ('k', 'a\x00b'),
            ('k', '\ud800'),
            ('k', 'a\u202eb'),
        ):
            with self.subTest(key=repr(key), value=repr(value)), self.assertRaises(ValueError):
                self.put(key, value)
        self.assertFalse(self.path.exists())

    def test_multiline_value_is_data_and_snapshot_is_detached(self):
        state = self.put(value='固定内容\n不是工具授权')
        state['items'].clear()
        self.assertEqual(len(self.memory.snapshot()['items']), 1)
        self.assertIn('\\n', self.memory.context())

    def test_context_orders_latest_explicit_update_after_older_different_names(self):
        self.memory.clock = lambda: datetime.fromisoformat('2026-11-09T12:00:00+08:00')
        self.put(key='公开偏好甲', value='旧值')
        self.memory.clock = lambda: datetime.fromisoformat('2026-11-09T12:01:00+08:00')
        self.put(key='公开偏好乙', value='中间值')
        self.memory.clock = lambda: datetime.fromisoformat('2026-11-09T12:02:00+08:00')
        self.put(key='公开偏好甲', value='最新明确值')
        value = json.loads(self.memory.context().split('\n', 1)[1])
        self.assertEqual([i['value'] for i in value['preferences']], ['中间值', '最新明确值'])
        self.assertEqual(value['subject'], 'user')

    def test_store_dates_and_duplicate_keys_are_validated(self):
        state = self.put()
        for invalid in (
            dict(state, revision=True),
            dict(state, enabled=1),
            dict(state, items=state['items'] * 2),
        ):
            with self.assertRaises(ValueError):
                validate(invalid)
        state['items'][0]['updated_at'] = '2020-01-01T00:00:00'
        with self.assertRaises(ValueError):
            validate(state)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.memory = isolated_memory()
        self.current = True
        self.connected = object()
        self.busy = False
        self.events = []
        self.controller = PreferenceController(
            PreferenceHooks(
                threading.RLock(),
                lambda: self.connected,
                lambda: self.busy,
                lambda name, **data: self.events.append((name, data)),
            ),
            self.memory,
        )

    def request(self, action='list', token='a', **fields):
        self.controller.request(
            dict(event='preference_request', request_id=token * 32, action=action, **fields)
        )

    def voice(self, text):
        return self.controller.voice(text, lambda: self.current)

    def test_device_mutation_and_duplicate_or_busy_requests(self):
        self.request('put', revision=0, key='称呼', value='公开姓名')
        self.assertTrue(self.events[-1][1]['ok'])
        self.request('clear', revision=1)
        self.assertFalse(self.events[-1][1]['ok'])
        self.assertEqual(self.memory.snapshot()['revision'], 1)
        self.busy = True
        self.request('clear', token='b', revision=1)
        self.assertIn('暂停语音', self.events[-1][1]['message'])
        self.assertEqual(self.memory.snapshot()['revision'], 1)

    def test_disconnected_and_cancelled_voice_have_no_effect(self):
        self.connected = None
        self.request('put', revision=0, key='k', value='v')
        self.assertEqual(self.events, [])
        with self.assertRaises(Cancelled):
            self.voice('记住，称呼是公开姓名')
        self.connected = object()
        self.current = False
        with self.assertRaises(Cancelled):
            self.voice('记住，称呼是公开姓名')
        self.assertFalse(self.memory.path.exists())

    def test_voice_put_replace_forget_and_clear_require_explicit_structure(self):
        self.assertIsNone(self.voice('普通聊天，我喜欢蓝色'))
        self.assertIn('请明确', self.voice('记住刚才那件事'))
        self.assertFalse(self.memory.path.exists())
        self.assertIn('没有找到', self.voice('把记忆中的称呼改成另一姓名'))
        self.assertIn('没有找到', self.voice('忘记称呼'))
        self.assertFalse(self.memory.path.exists())
        self.assertIn('内容是：称呼，公开姓名', self.voice('请记住，我的称呼是公开姓名'))
        self.voice('把记忆中的称呼改成另一姓名')
        self.assertEqual(self.memory.snapshot()['items'][0]['value'], '另一姓名')
        self.assertEqual(self.memory.snapshot()['items'][0]['source'], 'voice')
        self.voice('忘记称呼')
        self.assertEqual(self.memory.snapshot()['items'], [])
        self.voice('记住，颜色是蓝色')
        self.assertIn('确认清空', self.voice('清空记忆'))
        self.assertEqual(len(self.memory.snapshot()['items']), 1)
        self.voice('确认清空偏好记忆')
        self.assertEqual(self.memory.snapshot()['items'], [])

    def test_disable_does_not_delete_and_saved_while_disabled_is_honest(self):
        self.voice('关闭记忆')
        self.assertIn('目前已关闭', self.voice('记住，称呼是公开姓名'))
        self.assertEqual(self.controller.context(), '')
        self.assertIn('已关闭', self.voice('查看记忆'))
        self.voice('开启记忆')
        self.assertIn('公开姓名', self.controller.context())

    def test_asking_meaning_or_confirmation_never_persists_a_preference(self):
        for phrase in ('记住我的称呼是什么意思', '记住称呼是公开姓名吗', '请问记住是什么意思'):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.voice(phrase))
                self.assertFalse(self.memory.path.exists())

    def test_schema_rejects_unknown_fields_types_and_explicit_model_write_has_no_tool(self):
        from voice_tools import TOOLS

        self.assertNotIn('cc8_memory', [t['name'] for t in TOOLS])
        for fields in (
            {'action': []},
            {'revision': True},
            {'revision': '0'},
            {'key': 'a\x00b'},
            {'value': None},
            {'extra': 'not allowed'},
        ):
            request = (
                dict(
                    event='preference_request',
                    request_id='a' * 32,
                    action='put',
                    revision=0,
                    key='k',
                    value='v',
                )
                | fields
            )
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_device_event(request)
        with self.assertRaises(ValueError):
            validate_device_event(
                dict(event='preference_request', request_id='x' * 32, action='list')
            )

    def test_write_uncertainty_never_claims_success_or_returns_content(self):
        with patch.object(self.memory, 'apply', side_effect=OSError('PRIVATE_FIXED')):
            self.request('put', revision=0, key='k', value='v')
            with self.assertRaises(OperationFailure):
                self.voice('记住，称呼是公开姓名')
        self.assertFalse(self.events[-1][1]['ok'])
        self.assertNotIn('PRIVATE_FIXED', str(self.events))

    def test_memory_phrases_do_not_open_camera_or_delegate_ordinary_actions(self):
        for phrase in (
            '记住，我的爱好是拍照',
            '记住，测试内容是打开摄像头',
            '把记忆中的备注改成看看镜头',
            '关闭记忆',
        ):
            self.assertIsNotNone(command(phrase))
            self.assertFalse(visual_request(phrase))
        for phrase in (
            '帮我看看镜头',
            '联网查一下新闻',
            '删除桌面文件',
            '不要记住我的名字',
            '你能记住什么',
        ):
            self.assertIsNone(command(phrase))

    def test_current_wake_names_and_polite_prefixes_keep_explicit_local_intent(self):
        for phrase in (
            '大黄蜂，记住，称呼是公开姓名',
            'Bumblebee，请记住称呼是公开姓名',
            '请大黄蜂记住称呼是公开姓名',
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    command(phrase), {'action': 'put', 'key': '称呼', 'value': '公开姓名'}
                )
        self.assertIsNone(command('我刚说过大黄蜂记住这句话'))


class ProviderTests(unittest.TestCase):
    def test_dot_context_change_and_delete_replace_owned_ephemeral_context_only(self):
        memory = isolated_memory()
        first = FakeRpc()
        dot = DotBrain(first)
        peers = []

        def factory():
            peer = FakeRpc()
            peers.append(peer)
            return peer

        dot.rpc_factory = factory
        list(dot.clauses('初始公开问题', []))
        memory.apply(dict(action='put', key='称呼', value='唯一公开名字', revision=0), 'device')
        dot.preference_context = memory.context()
        list(dot.clauses('下一问题', []))
        self.assertTrue(first.closed)
        self.assertEqual(len(peers), 1)
        thread = peers[0].calls[0][1]
        self.assertTrue(thread['ephemeral'])
        self.assertNotIn('approvalPolicy', thread)
        self.assertIn('唯一公开名字', json.dumps(peers[0].calls, ensure_ascii=False))
        memory.apply(dict(action='delete', key='称呼', revision=1), 'device')
        dot.preference_context = memory.context()
        list(dot.clauses('删除后问题', []))
        self.assertEqual(len(peers), 2)
        self.assertNotIn('唯一公开名字', json.dumps(peers[1].calls, ensure_ascii=False))
        self.assertTrue(peers[0].closed)
        self.assertFalse(
            any(
                name in ('thread/resume', 'thread/read') for peer in peers for name, _ in peer.calls
            )
        )

    def test_real_router_local_memory_never_starts_model_or_records_audio(self):
        dot = DotBrain(FakeRpc())
        mini = fixtures.Brain()
        case = fixtures.FailureFeedbackTests()
        bridge = case.session(brain=BrainRouter(mini, dot))
        before = bridge.generation
        self.assertIn('已明确保存', ''.join(bridge.brain.clauses('记住，称呼是公开姓名', [])))
        self.assertEqual(dot.rpc.calls, [])
        self.assertEqual(mini.calls, 0)
        self.assertEqual(bridge.generation, before)
        self.assertEqual(bridge.metrics['mic_frames'], 0)
        self.assertTrue(bridge.response.queue.empty())
        bridge.session.active = False
        bridge.on_event(dict(event='preference_request', request_id='d' * 32, action='list'))
        self.assertEqual(bridge.events[-1][0], 'preference_result')
        self.assertEqual(bridge.events[-1][1]['state']['items'][0]['key'], '称呼')

    def test_actual_response_owner_does_not_retain_memory_command_in_model_history(self):
        dot = DotBrain(FakeRpc())
        case = fixtures.FailureFeedbackTests()
        bridge = case.session(brain=BrainRouter(fixtures.Brain(), dot))
        bridge.speak = lambda *args: True
        case.run_one(bridge.response, Reply(bridge.generation, '记住，称呼是固定公开姓名'))
        self.assertEqual(bridge.workspace.tasks[-1]['status'], 'completed')
        self.assertEqual(list(bridge.response.history), [])
        self.assertEqual(dot.rpc.calls, [])
        self.assertEqual(bridge.preferences.store.snapshot()['items'][0]['value'], '固定公开姓名')

    def test_minimax_stateless_requests_drop_disabled_preferences(self):
        class Transport:
            def __init__(self):
                self.bodies = []

            def events(self, endpoint, body, is_current):
                self.bodies.append(body)
                yield {
                    'choices': [{'delta': {'content': '固定公开回答。'}, 'finish_reason': 'stop'}]
                }

            def cancel(self):
                pass

        transport = Transport()
        mini = MiniMaxBrain(transport, model='public-fixture')
        dot = DotBrain(FakeRpc())
        router = BrainRouter(mini, dot, selected='minimax')
        memory = isolated_memory()
        memory.apply(dict(action='put', key='称呼', value='唯一公开名字', revision=0), 'device')
        router.preferences = PreferenceController(
            PreferenceHooks(
                threading.RLock(), lambda: object(), lambda: False, lambda *a, **k: None
            ),
            memory,
        )
        list(router.clauses('你好', []))
        self.assertIn('唯一公开名字', json.dumps(transport.bodies[-1], ensure_ascii=False))
        memory.apply(dict(action='set_enabled', enabled=False, revision=1), 'device')
        history = [{'role': 'assistant', 'content': '含有唯一公开名字的旧缓存回答'}]
        list(router.clauses('再见', history))
        self.assertEqual(history, [])
        self.assertNotIn('唯一公开名字', json.dumps(transport.bodies[-1], ensure_ascii=False))
        self.assertEqual(dot.rpc.calls, [])
