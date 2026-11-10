"""Plan/default-off and the real supervisor hook; never alter a login service."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import check_login as cli
from login_check_store import LoginCheckStore
from test_login_receipt import baseline

ROOT = Path(__file__).resolve().parents[2]


class LoginCommandTests(unittest.TestCase):
    def test_explicit_arm_and_actual_background_hook_complete_one_fresh_attempt(self):
        from login_receipt import LoginRecoveryRun

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            phase = ['before']
            clock = [0.0]

            def advance(seconds):
                clock[0] += seconds

            def sample(nonce):
                return dict(
                    instance='old' if phase[0] == 'before' else 'new',
                    process='old-pid' if phase[0] == 'before' else 'new-pid',
                    pid=100,
                    adopted=False,
                    idle=True,
                    activity_zero=True,
                    requests_idle=True,
                    activity={'counters': [0, 0, 0, 0], 'requests': 'digest'},
                    connected=False,
                    transport='none',
                )

            probe = SimpleNamespace(
                private=root / 'private',
                session=lambda nonce: phase[0],
                identity=lambda: {'config': 'original'},
                sample=sample,
            )
            with (
                patch('login_probe.LoginProbe', return_value=probe) as factory,
                patch('login_probe.observer_supported', return_value=True),
            ):
                result = cli.arm(root)
                self.assertEqual(result['state'], 'armed')
                self.assertNotIn('baseline', result)
                phase[0] = 'after'

                def runner(store, probe, stop, wait):
                    return LoginRecoveryRun(
                        store, probe, stop, clock=lambda: clock[0], wait=advance
                    )

                with patch('login_receipt.LoginRecoveryRun', side_effect=runner):
                    observer = cli.start_login_check(root, probe.private, threading.Event(), Mock())
                    observer.thread.join(2)
                    observer.close()
                value = LoginCheckStore(probe.private).load()
                self.assertEqual(value['state'], 'passed')
                self.assertTrue(value['new_gui_login_observed'])
                self.assertFalse(
                    value['connected']
                    or value['full_p0_acceptance']
                    or value['automatic_launch_cause_verified']
                )
                self.assertFalse(observer.thread.is_alive())
                self.assertEqual(factory.call_count, 2)

    def test_old_runtime_cannot_be_armed_by_a_newer_cli_or_touch_os_and_health(self):
        with (
            patch('login_probe.LoginProbe') as probe,
            patch('login_probe.observer_supported', return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, 'login_runtime_observer_unsupported'):
                cli.arm(Path('/old-runtime'))
            probe.return_value.session.assert_not_called()
            probe.return_value.identity.assert_not_called()
            probe.return_value.sample.assert_not_called()

    def test_root_cli_system_environment_plan_never_creates_runtime_or_calls_execution(self):
        for action in ('arm', 'status', 'cancel'):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / 'missing'
                with (
                    patch.object(cli, 'execute', side_effect=AssertionError('no reads')),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    self.assertEqual(cli.main(['--runtime-root', str(root), '--' + action]), 0)
                self.assertFalse(json.loads(out.getvalue())['execute'])
                self.assertFalse(root.exists())
        with tempfile.TemporaryDirectory() as folder:
            value = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'cc8'),
                    'verify-login',
                    '--runtime-root',
                    str(Path(folder) / 'missing'),
                    '--arm',
                ],
                cwd=folder,
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(value.returncode, 0, value.stderr)
            self.assertFalse(json.loads(value.stdout)['starts_media'])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_status_and_cancel_use_only_the_owned_diagnostic_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            private = root / 'device-access/private/voice-runtime'
            private.mkdir(parents=True, mode=0o700)
            store = LoginCheckStore(private)
            value = baseline()
            value['expires_at'] = value['armed_at'] + 500
            with patch('check_login.time.time', return_value=1100):
                self.assertEqual(cli.execute(root, 'status')['state'], 'not_armed')
                store.arm(value)
                self.assertEqual(cli.execute(root, 'status')['state'], 'armed')
                self.assertEqual(cli.execute(root, 'cancel')['state'], 'interrupted')
            self.assertEqual(store.load()['reason'], 'owner_cancelled_login_check')
            self.assertNotIn('baseline', cli.execute(root, 'status'))

    def test_missing_state_starts_no_thread_or_model_and_hook_setup_failure_is_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('threading.Thread', side_effect=AssertionError('no worker')):
                self.assertIsNone(
                    cli.start_login_check(root, root / 'missing', threading.Event(), Mock())
                )
            self.assertEqual(list(root.iterdir()), [])
            store = LoginCheckStore(root)
            value = baseline()
            store.arm(value)
            value['state'] = 'passed'
            store.replace(value['id'], value, expected='armed')
            with patch('threading.Thread', side_effect=AssertionError('no worker')):
                self.assertIsNone(cli.start_login_check(root, root, threading.Event(), Mock()))
        logger = Mock()
        logger.warning.side_effect = OSError('private-message')
        with patch.object(cli, '_start_login_check', side_effect=RuntimeError('private-message')):
            self.assertIsNone(
                cli.start_login_check(Path('/no-read'), Path('/no-read'), threading.Event(), logger)
            )

    def test_actual_background_hook_does_not_poll_or_claim_a_same_domain_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = LoginCheckStore(root)
            value = baseline()
            value['armed_at'] = 1
            value['expires_at'] = 604801
            store.arm(value)
            with patch('login_probe.LoginProbe') as probe:
                # The runner's bound default clock is real, so use a future
                # valid wall window in the stored record below.
                import time

                value['armed_at'] = time.time()
                value['expires_at'] = value['armed_at'] + 3600
                store.replace(value['id'], value, expected='armed')
                probe.return_value.session.return_value = 'old'
                observer = cli.start_login_check(root, root, threading.Event(), Mock())
                self.assertIsNotNone(observer)
                observer.thread.join(2)
                observer.close()
                self.assertFalse(observer.thread.is_alive())
                probe.return_value.sample.assert_not_called()
                probe.return_value.identity.assert_not_called()
            self.assertEqual(store.load()['state'], 'armed')

    def test_optional_observer_close_failure_cannot_skip_real_supervisor_cleanup(self):
        import service

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(service, 'PRIVATE', Path(folder)),
            patch.object(service, 'enabled', return_value=False),
        ):
            supervisor = service.Supervisor()
            observer = Mock()
            observer.close.side_effect = OSError('private-message')
            with (
                patch.object(service, 'start_login_check', return_value=observer),
                patch.object(supervisor, 'terminate') as terminate,
            ):
                supervisor.run()
                terminate.assert_called_once()

    def test_operator_interrupt_has_a_bounded_non_traceback_exit(self):
        with (
            patch.object(cli, 'execute', side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(
                cli.main(['--runtime-root', '/not-started', '--execute', '--arm']), 130
            )
            self.assertEqual(json.loads(out.getvalue())['state'], 'interrupted')
