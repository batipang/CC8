"""Device cannot grant owner access; role changes preserve in-flight work and stop no microphone implicitly."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from identity_controller import IdentityController, IdentityHooks
from identity_mode import IdentityModeStore
from identity_mode import ModeCommitUncertain, ModeState


class IdentityControllerTests(unittest.TestCase):
    def setup_controller(self, folder):
        hooks = IdentityHooks(
            threading.RLock(),
            Mock(return_value=False),
            Mock(return_value=True),
            Mock(return_value=True),
            Mock(),
            Mock(),
            Mock(),
            Mock(return_value=(object(), 'route-a')),
        )
        controller = IdentityController(
            IdentityModeStore(Path(folder) / 'private' / 'identity-mode.json'), hooks
        )
        return controller, hooks

    def test_constructor_passive_load_and_device_guest_then_desktop_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            self.assertFalse(controller.owner)
            self.assertFalse(controller.store.path.parent.exists())
            controller.load()
            self.assertTrue(controller.owner)
            controller.device_request(
                {
                    'event': 'identity_request',
                    'request_id': 'a' * 32,
                    'arguments': {'action': 'guest', 'revision': 0},
                }
            )
            self.assertTrue(controller.guest)
            self.assertTrue(hooks.event.call_args.kwargs['ok'])
            hooks.stop.assert_called_once()
            hooks.reset_contexts.assert_called_once_with('guest')
            result = controller.owner_from_desktop({'revision': 1}, timeout=0)
            self.assertEqual(result['state']['mode'], 'owner')
            self.assertTrue(controller.owner)

    def test_device_status_and_fake_owner_action_cannot_grant_or_mutate(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            controller.select('guest', 0)
            before = controller.store.path.read_bytes()
            hooks.stop.reset_mock()
            hooks.reset_contexts.reset_mock()
            controller.device_request(
                {
                    'event': 'identity_request',
                    'request_id': 'b' * 32,
                    'arguments': {'action': 'owner', 'revision': 1},
                }
            )
            self.assertFalse(hooks.event.call_args.kwargs['ok'])
            self.assertTrue(controller.guest)
            self.assertEqual(controller.store.path.read_bytes(), before)
            hooks.stop.assert_not_called()
            hooks.reset_contexts.assert_not_called()
            controller.device_request(
                {
                    'event': 'identity_request',
                    'request_id': 'c' * 32,
                    'arguments': {'action': 'status'},
                }
            )
            self.assertTrue(hooks.event.call_args.kwargs['ok'])
            self.assertEqual(
                set(hooks.event.call_args.kwargs['state']),
                {'schema', 'mode', 'revision', 'ready', 'owner_restore', 'device_applied'},
            )

    def test_busy_or_unsupported_peer_does_not_stop_or_change_owner_request(self):
        for blocked in ('busy', 'peer_supported', 'connected'):
            with tempfile.TemporaryDirectory() as folder:
                controller, hooks = self.setup_controller(folder)
                controller.load()
                getattr(hooks, blocked).return_value = blocked == 'busy'
                controller.device_request(
                    {
                        'event': 'identity_request',
                        'request_id': 'd' * 32,
                        'arguments': {'action': 'guest', 'revision': 0},
                    }
                )
                self.assertFalse(hooks.event.call_args.kwargs['ok'])
                hooks.stop.assert_not_called()
                hooks.reset_contexts.assert_not_called()
                self.assertFalse(controller.store.path.exists())
                self.assertTrue(controller.owner)

    def test_invalid_store_fails_before_role_ready_and_keeps_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            path = controller.store.path
            path.parent.mkdir(mode=0o700)
            path.write_text('invalid-mode-data')
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                controller.load()
            self.assertFalse(controller.owner)
            self.assertEqual(path.read_text(), 'invalid-mode-data')
            hooks.stop.assert_not_called()

    def test_context_reset_failure_cannot_grant_owner_after_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            controller.select('guest', 0)
            hooks.reset_contexts.side_effect = RuntimeError('private reset detail')
            with self.assertRaises(RuntimeError):
                controller.owner_from_desktop({'revision': 1}, timeout=0)
            self.assertFalse(controller.owner)
            self.assertFalse(controller.public()['ready'])
            self.assertEqual(controller.store.snapshot(), ModeState('owner', 2))
            with self.assertRaises(RuntimeError):
                controller.owner_from_desktop({'revision': 2}, timeout=0)

    def test_commit_uncertain_retains_actual_role_without_owner_access_or_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            controller.select('guest', 0)
            controller.store.change = Mock(side_effect=ModeCommitUncertain(ModeState('owner', 2)))
            with self.assertRaisesRegex(RuntimeError, '保存未确认'):
                controller.owner_from_desktop({'revision': 1}, timeout=0)
            self.assertEqual(controller.state, ModeState('owner', 2))
            self.assertFalse(controller.owner)
            self.assertFalse(controller.public()['ready'])
            hooks.reset_contexts.assert_called_with('blocked')
            controller.device_request(
                {
                    'event': 'identity_request',
                    'request_id': 'e' * 32,
                    'arguments': {'action': 'status'},
                }
            )
            self.assertTrue(hooks.event.call_args.kwargs['ok'])
            self.assertFalse(hooks.event.call_args.kwargs['state']['ready'])

    def test_uncertain_guest_state_is_not_an_active_guest_permission(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            controller.store.change = Mock(side_effect=ModeCommitUncertain(ModeState('guest', 1)))
            controller.device_request(
                {
                    'event': 'identity_request',
                    'request_id': 'f' * 32,
                    'arguments': {'action': 'guest', 'revision': 0},
                }
            )
            self.assertFalse(controller.owner)
            self.assertFalse(controller.guest)
            self.assertFalse(hooks.event.call_args.kwargs['ok'])
            self.assertEqual(controller.public()['mode'], 'guest')
            self.assertFalse(controller.public()['ready'])
            hooks.reset_contexts.assert_called_once_with('blocked')

    def test_extra_actor_or_wrong_event_cannot_bypass_exact_request_shape(self):
        for extra in ({'actor': 'owner_desktop'}, {'event': 'owner_restore'}):
            with tempfile.TemporaryDirectory() as folder:
                controller, hooks = self.setup_controller(folder)
                controller.load()
                controller.device_request(
                    {
                        'event': 'identity_request',
                        'request_id': 'f' * 32,
                        'arguments': {'action': 'guest', 'revision': 0},
                        **extra,
                    }
                )
                self.assertTrue(controller.owner)
                self.assertFalse(hooks.event.call_args.kwargs['ok'])
                hooks.stop.assert_not_called()
                hooks.reset_contexts.assert_not_called()

    def acknowledgement(self, controller):
        return {
            'event': 'identity_applied',
            'token': controller.application.token,
            'mode': controller.state.mode,
            'revision': controller.state.revision,
        }

    def test_actual_state_requires_exact_role_revision_token_and_live_route_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            self.assertFalse(controller.device_applied)
            self.assertFalse(
                controller.applied(
                    {'event': 'identity_applied', 'token': 'f' * 32, 'mode': 'owner', 'revision': 0}
                )
            )
            controller.select('guest', 0)
            self.assertFalse(controller.public()['device_applied'])
            acknowledgement = self.acknowledgement(controller)
            for extra in (
                {'token': 'f' * 32},
                {'revision': True},
                {'revision': 0},
                {'mode': 'owner'},
                {'actor': 'owner_desktop'},
            ):
                self.assertFalse(controller.applied({**acknowledgement, **extra}))
            self.assertEqual(controller.state, ModeState('guest', 1))
            self.assertTrue(controller.applied(acknowledgement))
            self.assertTrue(controller.device_applied)
            hooks.connected.return_value = False
            self.assertFalse(controller.device_applied)
            self.assertFalse(controller.applied(acknowledgement))

    def test_route_handover_reissues_challenge_without_mode_write_or_old_ack_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            controller.select('guest', 0)
            old = self.acknowledgement(controller)
            self.assertTrue(controller.applied(old))
            original = controller.store.path.read_bytes()
            connection, _ = hooks.route.return_value
            hooks.route.return_value = (connection, 'new-activation')
            self.assertFalse(controller.device_applied)
            self.assertFalse(controller.applied(old))
            controller.synchronize()
            self.assertNotEqual(controller.application.token, old['token'])
            self.assertTrue(controller.applied(self.acknowledgement(controller)))
            self.assertEqual(controller.store.path.read_bytes(), original)
            hooks.stop.assert_called_once()

    def test_owner_timeout_is_unconfirmed_and_late_ack_does_not_repeat_role_write(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            controller.select('guest', 0)
            guest_ack = self.acknowledgement(controller)
            controller.applied(guest_ack)
            change = controller.store.change = Mock(wraps=controller.store.change)
            result = controller.owner_from_desktop({'revision': 1}, timeout=0)
            self.assertFalse(result['ok'])
            self.assertFalse(result['state']['device_applied'])
            self.assertEqual(result['state']['mode'], 'owner')
            self.assertFalse(controller.applied(guest_ack))
            self.assertTrue(controller.applied(self.acknowledgement(controller)))
            self.assertTrue(controller.device_applied)
            change.assert_called_once()
            self.assertEqual(controller.store.snapshot(), ModeState('owner', 2))
            self.assertEqual(hooks.event.call_args.kwargs['authority'], 'owner_desktop')

    def test_owner_wait_releases_session_guard_for_receive_thread_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            notice, results = threading.Event(), []
            hooks.event.side_effect = lambda *args, **kwargs: notice.set()
            worker = threading.Thread(
                target=lambda: results.append(
                    controller.owner_from_desktop({'revision': 0}, timeout=2)
                ),
                daemon=True,
            )
            worker.start()
            self.assertTrue(notice.wait(1))
            self.assertTrue(controller.applied(self.acknowledgement(controller)))
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(results[0]['ok'])

    def test_failed_state_send_preserves_committed_role_without_replaying_change(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            hooks.event.side_effect = [OSError('PRIVATE_PATH'), None, None]
            packet = {
                'event': 'identity_request',
                'request_id': 'a' * 32,
                'arguments': {'action': 'guest', 'revision': 0},
            }
            controller.device_request(packet)
            self.assertFalse(hooks.event.call_args.kwargs['ok'])
            self.assertNotIn('PRIVATE_PATH', str(hooks.event.call_args))
            self.assertFalse(controller.device_applied)
            self.assertEqual(controller.store.snapshot(), ModeState('guest', 1))
            controller.device_request(packet)
            self.assertFalse(hooks.event.call_args.kwargs['ok'])
            hooks.stop.assert_called_once()

    def test_saved_owner_restore_can_resynchronize_without_repeating_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            controller, hooks = self.setup_controller(folder)
            controller.load()
            controller.synchronize()
            self.assertEqual(hooks.event.call_args.kwargs['authority'], 'reconcile')
            controller.owner_from_desktop({'revision': 0}, timeout=0)
            original = controller.store.path.read_bytes()
            change = controller.store.change = Mock(wraps=controller.store.change)
            connection, _ = hooks.route.return_value
            hooks.route.return_value = (connection, 'reconnected')
            controller.synchronize()
            self.assertEqual(hooks.event.call_args.kwargs['authority'], 'owner_desktop')
            self.assertTrue(controller.applied(self.acknowledgement(controller)))
            self.assertEqual(controller.store.path.read_bytes(), original)
            change.assert_not_called()
