"""Exercise isolation gates and real-controller fixtures without hardware/providers."""

import json
from contextlib import contextmanager, ExitStack
import os
import queue
from pathlib import Path
import socket
import subprocess
import sys
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

from tools import check_failure_ui as check, qa_faults, qa_ui, qa_variant


class VariantTests(unittest.TestCase):
    def test_real_manifest_and_exact_port_package_only_in_separate_snapshot(self):
        names = qa_variant.snapshot_files()
        self.assertTrue(names)
        self.assertFalse(any('/private/' in n or '/signing/' in n for n in names))
        originals = {n: qa_variant.safe_source(qa_variant.ROOT, n) for n in names}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'variant'
            before, after = qa_variant.snapshot(target)
            self.assertEqual(set(before), set(names))
            self.assertEqual(
                {n for n in before if before[n] != after[n]},
                {qa_variant.GRADLE, qa_variant.MANIFEST, qa_variant.SESSION},
            )
            manifest = (target / qa_variant.MANIFEST).read_text()
            for permission in qa_variant.FORBIDDEN:
                self.assertNotIn('android.permission.' + permission, manifest)
            self.assertNotIn('sharedUserId', manifest)
            self.assertIn(qa_variant.PACKAGE, (target / qa_variant.GRADLE).read_text())
            self.assertIn('28765)', (target / qa_variant.SESSION).read_text())
            with self.assertRaises(FileExistsError):
                qa_variant.snapshot(target)
            self.assertEqual(
                originals, {n: qa_variant.safe_source(qa_variant.ROOT, n) for n in names}
            )

    def test_changed_duplicate_and_shared_uid_context_are_rejected(self):
        for name in (qa_variant.MANIFEST, qa_variant.SESSION, qa_variant.GRADLE):
            raw = qa_variant.safe_source(qa_variant.ROOT, name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                qa_variant.variant(name, raw + raw)
            with self.subTest(name=name), self.assertRaises(ValueError):
                qa_variant.variant(name, b'missing context')
        raw = qa_variant.safe_source(qa_variant.ROOT, qa_variant.MANIFEST)
        with self.assertRaises(ValueError):
            qa_variant.variant(
                qa_variant.MANIFEST, raw.replace(b'<manifest ', b'<manifest sharedUserId="x" ')
            )

    def test_links_fifo_and_path_escape_never_enter_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'real').write_bytes(b'source')
            (root / 'link').symlink_to(root / 'real')
            os.link(root / 'real', root / 'hard')
            os.mkfifo(root / 'fifo')
            (root / 'dirlink').symlink_to(root, target_is_directory=True)
            for name in ('link', 'hard', 'fifo', 'dirlink/real', '../escape', '/absolute'):
                with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                    qa_variant.safe_source(root, name)

    def test_aapt_requires_qa_identity_and_manifest_absence_of_all_capture_permissions(self):
        good = "package: name='com.happyhome.cc8assistant.qa' versionCode='38' versionName='0.9.3'"
        for badging, manifest in (
            (good.replace('.qa', ''), ''),
            (good + "\nuses-permission: name='android.permission.CAMERA'", ''),
            (good, 'android.permission.RECORD_AUDIO'),
            (good, 'sharedUserId'),
            (good, 'android.permission.SYSTEM_ALERT_WINDOW'),
        ):
            with (
                self.subTest(manifest=manifest),
                patch.object(subprocess, 'check_output', side_effect=[badging, manifest]),
                self.assertRaises(ValueError),
            ):
                qa_variant.apk_metadata(Path('fixed.apk'), Path('aapt'))
        with patch.object(subprocess, 'check_output', side_effect=[good, 'safe manifest']):
            self.assertEqual(
                qa_variant.apk_metadata(Path('fixed.apk'), Path('aapt'))['package'],
                qa_variant.PACKAGE,
            )


class HierarchyTests(unittest.TestCase):
    message = '识别服务连接失败。请检查连接后再试。'

    def test_stdout_wrapper_accepts_only_known_android_footer(self):
        for label in ('hierarchy', 'hierchary'):
            for proc in ('self', '12345'):
                root = qa_ui.stdout_hierarchy(
                    '<hierarchy/>\nUI ' + label + ' dumped to: /proc/' + proc + '/fd/1'
                )
                self.assertEqual(root.tag, 'hierarchy')
        with self.assertRaises(ValueError):
            qa_ui.stdout_hierarchy('<hierarchy/>ERROR: could not get idle state')

    def root(self, content):
        return qa_ui.hierarchy('<hierarchy>' + content + '</hierarchy>')

    def node(self, text, **attributes):
        attrs = {
            'package': qa_variant.PACKAGE,
            'text': text,
            'bounds': '[10,20][90,60]',
            **attributes,
        }
        return (
            '<node '
            + ' '.join(k + '="' + escape(v, {'"': '&quot;'}) + '"' for k, v in attrs.items())
            + '/>'
        )

    def test_unique_rendered_summary_click_and_full_dialog(self):
        summary = self.message.split('。')[0] + ' · 查看详情 ›'
        root = self.root(self.node(summary, clickable='true'))
        self.assertEqual(qa_ui.status_target(root, self.message), (50, 40))
        dialog = self.root(
            self.node(self.message, **{'resource-id': 'android:id/message'})
            + self.node('关闭', clickable='true', **{'resource-id': 'android:id/button1'})
        )
        self.assertEqual(qa_ui.dialog_close(dialog, self.message), (50, 40))

    def test_missing_duplicate_wrong_package_nonclickable_and_clipped_details_fail(self):
        summary = self.message.split('。')[0] + ' · 查看详情 ›'
        node = self.node(summary, clickable='true')
        for content in (
            '',
            node + node,
            node.replace(qa_variant.PACKAGE, 'other'),
            self.node(summary),
        ):
            with self.subTest(content=content), self.assertRaises(ValueError):
                qa_ui.status_target(self.root(content), self.message)
        with self.assertRaises(ValueError):
            qa_ui.dialog_close(self.root(self.node(self.message.split('。')[0])), self.message)

    def test_unsafe_xml_and_invalid_bounds_fail_without_clicks(self):
        for raw in (
            '<!DOCTYPE hierarchy><hierarchy/>',
            '<!ENTITY x "x"><hierarchy/>',
            '<wrong/>',
            '<hierarchy>' + 'x' * 262145 + '</hierarchy>',
        ):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                qa_ui.hierarchy(raw)
        for bounds in ('[1,1][1,9]', '[-1,0][20,10]', '[0,0][9000,10]', 'garbage'):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                qa_ui.point(self.root(self.node('', bounds=bounds))[0])


class ControllerFixtureTests(unittest.TestCase):
    def test_all_48_real_controller_failures_and_owned_recoveries_are_offline(self):
        # Fail immediately if fake I/O accidentally falls through to a network.
        with (
            patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')),
            patch.object(subprocess, 'Popen', side_effect=AssertionError('process forbidden')),
        ):
            fixtures = qa_faults.generate()
        from protocol import decode_event, EVENT

        self.assertEqual(len(fixtures), 48)
        self.assertEqual([f['case'] for f in fixtures], list(qa_faults.CASES))
        self.assertEqual(len({f['case'].ident for f in fixtures}), 48)
        for fixture in fixtures:
            packets = fixture['error'] + fixture['recovery']
            self.assertTrue(all(p.kind == EVENT for p in packets))
            events = [decode_event(p.data) for p in fixture['error']]
            errors = [i for i, e in enumerate(events) if e.get('phase') == 'error']
            self.assertEqual(len(errors), 1)
            self.assertEqual(events[errors[0]]['message'], fixture['expected_message'])
            finished = [i for i, e in enumerate(events) if e.get('event') == 'text_done']
            self.assertTrue(finished and errors[0] < finished[0])
            recover = [decode_event(p.data) for p in fixture['recovery']]
            generations = [
                p.generation
                for p in fixture['recovery']
                if decode_event(p.data).get('event') == 'text_begin'
            ]
            self.assertTrue(generations and generations[0] > fixture['error'][errors[0]].generation)
            self.assertNotIn('qa-injected-sensitive-body', json.dumps(events + recover))
            self.assertEqual(
                fixture['provider_requests'], 2 if fixture['case'].stage == 'tts' else 1
            )
        self.assertEqual(len(check.corpus_identity(fixtures)), 64)
        changed = fixtures[0]['error'].pop()
        self.assertNotEqual(
            check.corpus_identity(fixtures),
            check.corpus_identity([{**fixtures[0], 'error': [changed]}]),
        )

    def test_local_stdio_matrix_has_real_start_timeouts_declines_and_recoveries(self):
        # The test child talks only over stdio. A remote provider in the
        # controller would violate this independent network guard.
        with patch.object(
            socket.socket, 'connect', side_effect=AssertionError('network forbidden')
        ):
            fixtures = qa_faults.generate(dot_fixture='local_stdio')
        self.assertEqual(len(fixtures), 48)
        self.assertIn('qa_dot.py', check.source_identity(dot_fixture='local_stdio'))
        from protocol import decode_event

        for row in fixtures:
            case = row['case']
            with self.subTest(case=case.ident):
                timeout = case.stage == 'brain' and case.kind == 'timeout'
                self.assertEqual(row['dot_fixture'], 'local_stdio')
                self.assertEqual(
                    row['dot_wire_counts'],
                    {
                        'fault_turns': int(case.stage != 'asr'),
                        'recovery_turns': 1,
                        'declined_approvals': int(case.kind == 'approval_declined'),
                        'withheld_start_acks': int(timeout),
                    },
                )
                events = [decode_event(p.data) for p in row['error'] + row['recovery']]
                self.assertNotIn('qa-injected-sensitive-body', json.dumps(events))
                if timeout:
                    self.assertIn('结果待核对', row['expected_message'])
        self.assertNotEqual(
            check.corpus_identity(fixtures),
            check.corpus_identity([{**row, 'dot_fixture': 'fake'} for row in fixtures]),
        )

    def test_invalid_dot_fixture_is_rejected_without_launching_a_process(self):
        with patch.object(subprocess, 'Popen') as process, self.assertRaises(ValueError):
            qa_faults.generate(dot_fixture='unknown')
        process.assert_not_called()


class DriverGateTests(unittest.TestCase):
    def test_listener_start_retry_is_bounded_and_never_replays_requests(self):
        device = Mock()
        link = Mock()
        with (
            patch.object(check, 'Link', side_effect=[queue.Empty, link]) as constructor,
            patch.object(check.time, 'sleep'),
        ):
            self.assertIs(check.connect(device), link)
            self.assertEqual(constructor.call_count, 2)
            self.assertEqual(device.safe.call_count, 2)
            link.send.assert_not_called()
        with (
            patch.object(check, 'Link', side_effect=queue.Empty) as constructor,
            patch.object(check.time, 'sleep'),
            self.assertRaises(RuntimeError),
        ):
            check.connect(device)
        self.assertEqual(constructor.call_count, 3)
        with (
            patch.object(
                check, 'Link', side_effect=RuntimeError('qa_device_not_idle')
            ) as constructor,
            self.assertRaises(RuntimeError),
        ):
            check.connect(device)
        self.assertEqual(constructor.call_count, 1)

    def test_failed_link_handshake_closes_its_pipe_and_reader(self):
        from protocol import Packet

        closed = threading.Event()
        pipe = Mock()
        pipe.close.side_effect = closed.set
        packets = [Packet.event('transport_hello', protocol=99)]

        def receive(unused):
            if packets:
                return packets.pop()
            closed.wait(3)
            raise EOFError()

        device = Mock()
        device.access.PREFIX = ''
        device.transport = '7'
        with (
            patch('usb_transport.USBPipe', return_value=pipe),
            patch('protocol.receive', side_effect=receive),
            self.assertRaises(RuntimeError),
        ):
            check.Link(device)
        pipe.close.assert_called_once()
        self.assertTrue(closed.is_set())

    def test_capture_or_background_changes_abort_before_ui_input(self):
        device = check.Device.__new__(check.Device)
        good = {'foreground': True, 'microphone_active': False, 'mic_frames': 0}
        ui = {'camera_open': False, 'camera_captures': 0, 'auto_voice_start_attempts': 0}
        device.status = Mock(return_value=(good, ui))
        self.assertEqual(device.safe(), (good, ui))
        for d, u in (
            ({**good, 'foreground': False}, ui),
            ({**good, 'microphone_active': True}, ui),
            (good, {**ui, 'camera_open': True}),
            (good, {**ui, 'auto_voice_start_attempts': 1}),
        ):
            device.status.return_value = d, u
            with self.assertRaises(RuntimeError):
                device.safe()

    def test_device_hierarchy_uses_stdout_and_never_device_or_shared_storage(self):
        device = check.Device.__new__(check.Device)
        device.safe = Mock()
        device.shell = Mock(return_value='<invalid')
        with self.assertRaises(Exception):
            device.xml()
        self.assertEqual(
            device.shell.call_args.args[0],
            'uiautomator dump --compressed /proc/self/fd/1 | toybox head -c 262145',
        )

    def test_real_default_plans_work_without_native_or_device_imports_minimal_path(self):
        root = qa_variant.ROOT
        for argv in (
            [str(root / 'cc8'), 'verify-failure-ui', '--runtime-root', '/not/used'],
            [str(root / 'cc8'), 'verify-failure-ui', '--host-only'],
            [str(root / 'tools/qa_variant.py'), '--output', '/not/created'],
        ):
            code = (
                'import runpy,sys;sys.argv='
                + repr(argv)
                + ';sys.path.insert(0,'
                + repr(str(Path(argv[0]).parent))
                + ')'
                + ';\ntry:runpy.run_path(sys.argv[0],run_name="__main__")\nexcept SystemExit as e:assert e.code==0\n'
                + 'assert not any(n in sys.modules for n in ("numpy","bridge","cc8_install","device_smoke","requests"))'
            )
            result = subprocess.run(
                [sys.executable, '-c', code],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['execute'])

    def test_host_only_runs_real_loopback_recovery_without_device_or_runtime(self):
        with (
            patch.object(check, 'Device', side_effect=AssertionError('unexpected device')),
            patch.object(check, 'private_directory', side_effect=AssertionError('private writes')),
            patch('builtins.print') as output,
        ):
            self.assertEqual(check.main(['--host-only', '--execute', '--limit', '1']), 0)
        report = json.loads(output.call_args.args[0])
        self.assertEqual(report['state'], 'passed')
        self.assertEqual(report['scope'], 'host_fault_recovery')
        self.assertFalse(report['physical_ui_tested'])
        self.assertEqual(report['selected_cases'], 1)
        self.assertEqual(report['generated_wire_requests'], 1)
        self.assertEqual(report['generated_recovery_wire_requests'], 1)
        self.assertEqual(report['generated_dot_wire_counts']['recovery_turns'], 1)
        self.assertRegex(report['fixture_sha256'], r'^[a-f0-9]{64}$')

    def test_host_only_rejects_device_options_and_redacts_fixture_failure(self):
        with (
            patch.object(
                check, 'generate', side_effect=RuntimeError('sensitive test value')
            ) as run,
            patch('builtins.print') as output,
        ):
            for extra in (['--runtime-root', '/x'], ['--qa-receipt', '/x'], ['--shared-staging']):
                with self.subTest(extra=extra), self.assertRaises(SystemExit):
                    check.main(['--host-only', '--execute', *extra])
            run.assert_not_called()
            self.assertEqual(check.main(['--host-only', '--execute']), 2)
        report = json.loads(output.call_args.args[0])
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['error_type'], 'RuntimeError')
        self.assertNotIn('sensitive test value', output.call_args.args[0])

    def test_private_receipt_regular_owner_only_and_no_links_or_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / 'receipt.json'
            target.write_text('{}')
            target.chmod(0o600)
            self.assertEqual(check.private_json(target), {})
            for kind in ('wide', 'link', 'fifo', 'hard'):
                bad = root / kind
                if kind == 'wide':
                    bad.write_text('{}')
                    bad.chmod(0o644)
                elif kind == 'link':
                    bad.symlink_to(target)
                elif kind == 'fifo':
                    os.mkfifo(bad)
                else:
                    os.link(target, bad)
                with self.subTest(kind=kind), self.assertRaises((RuntimeError, OSError)):
                    check.private_json(bad)
                bad.unlink()

    def test_partial_install_is_removable_only_when_exact_owned_candidate_matches(self):
        device = Mock()
        device.access.package_paths.return_value = ['/data/app/fixed/base.apk']
        device.shell.return_value = 'a' * 64 + '  /data/app/fixed/base.apk'
        self.assertTrue(check.owned_package(device, 'a' * 64))
        device.shell.return_value = 'b' * 64
        with self.assertRaises(RuntimeError):
            check.owned_package(device, 'a' * 64)
        device.access.package_paths.return_value = []
        device.shell.return_value = ''
        self.assertFalse(check.owned_package(device, 'a' * 64))
        device.shell.return_value = 'error'
        with self.assertRaises(RuntimeError):
            check.owned_package(device, 'a' * 64)

    def test_restore_waits_for_transient_reconnect_but_rejects_changed_instance(self):
        baseline = {'pid': 7, 'instance': 'fixed'}
        with patch('service_smoke.LiveService') as service, patch.object(check.time, 'sleep'):
            service.return_value.sample.side_effect = [RuntimeError('not ready'), None, baseline]
            device = Mock()
            check.restore(Path('/unused'), device, baseline)
            self.assertIn('auto_start_voice false', device.shell.call_args.args[0])
            service.return_value.sample.side_effect = [{'pid': 8, 'instance': 'changed'}]
            with self.assertRaises(RuntimeError):
                check.restore(Path('/unused'), device, baseline)


class DriverExecutionTests(unittest.TestCase):
    @contextmanager
    def driver(self):
        from protocol import Packet

        digest = __import__('hashlib').sha256(b'fixed').hexdigest()
        receipt = dict(
            schema=1,
            kind='cc8_isolated_qa_apk',
            component=check.COMPONENT,
            device_port=check.PORT,
            package=check.PACKAGE,
            capture_permissions_absent=True,
            shared_uid_absent=True,
            input_sha256={'fixed': digest},
            apk_sha256=digest,
            version='0.9.3',
            version_code=38,
            source_commit='f' * 40,
            source_dirty=True,
        )
        fixture = dict(
            case=qa_faults.CASES[0],
            error=[Packet.event('status', 1, phase='error')],
            recovery=[
                Packet.event('status', 2, phase='thinking'),
                Packet.event('status', 2, phase='idle'),
            ],
            stale_error=Packet.event('status', 1, phase='error'),
            expected_task='failed',
            expected_message='固定错误。固定建议。',
        )
        with ExitStack() as stack:
            patches = {
                'private_json': receipt,
                'snapshot_files': ['fixed'],
                'safe_source': b'fixed',
                'private_bytes': b'fixed',
                'apk_metadata': {'version': '0.9.3', 'version_code': 38},
                'generate': [fixture],
                'source_identity': {},
                'owned_package': True,
                'status_target': (1, 1),
                'dialog_close': (2, 2),
            }
            for name, value in patches.items():
                stack.enter_context(patch.object(check, name, return_value=value))
            stack.enter_context(patch.object(check.time, 'sleep'))
            device = stack.enter_context(patch.object(check, 'Device')).return_value
            device.hierarchy_diagnostic = None
            device.access.package_paths.side_effect = [[], []]
            device.access.install.return_value = {'installed': True, 'apk_sha256': digest}
            device.shell.side_effect = lambda cmd: (
                'Success' if cmd.startswith('cmd package uninstall ') else ''
            )
            device.safe.return_value = {}, {'ui_phase': 'thinking'}
            service = stack.enter_context(patch('service_smoke.LiveService')).return_value
            service.sample.return_value = {'pid': 7, 'instance': 'fixed'}
            stack.enter_context(
                patch.object(
                    check,
                    'wait_phase',
                    side_effect=[
                        ({}, {'latest_task_status': 'failed'}),
                        ({}, {}),
                        ({}, {'latest_task_status': 'completed'}),
                    ],
                )
            )
            restore = stack.enter_context(patch.object(check, 'restore'))
            link = stack.enter_context(patch.object(check, 'Link')).return_value
            output = Mock()
            yield device, link, restore, output

    def test_report_binds_the_local_stdio_counts_and_passes_mode_to_generator(self):
        with self.driver() as (_, _, _, output):
            counts = {
                'fault_turns': 0,
                'recovery_turns': 1,
                'declined_approvals': 0,
                'withheld_start_acks': 0,
            }
            check.generate.return_value[0].update(dot_fixture='local_stdio', dot_wire_counts=counts)
            self.assertEqual(
                check.run(
                    Path('/runtime'),
                    Path('/private/qa-build.json'),
                    1,
                    output,
                    dot_fixture='local_stdio',
                ),
                'passed',
            )
            check.generate.assert_called_once_with(
                http_fixture='fake', dot_fixture='local_stdio', cases=qa_faults.CASES[:1]
            )
            report = output.write.call_args.args[0]
            self.assertEqual(report['provider_dot_fixture'], 'local_stdio')
            self.assertEqual(report['generated_dot_wire_counts'], counts)
            check.source_identity.assert_called_once_with('fake', 'local_stdio')
            self.assertFalse(report['real_provider_requested'])

    def test_success_replays_ui_then_removes_only_qa_and_restores_capture_off(self):
        with self.driver() as (device, link, restore, output):
            state = check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            self.assertEqual(state, 'passed')
            link.close.assert_called_once()
            restore.assert_called_once()
            commands = [c.args[0] for c in device.shell.call_args_list]
            self.assertIn('cmd package uninstall ' + check.PACKAGE, commands)
            self.assertFalse(
                any(
                    c == 'cmd package uninstall com.happyhome.cc8assistant'
                    or c.startswith('pm clear ')
                    for c in commands
                )
            )
            report = output.write.call_args.args[0]
            self.assertTrue(report['cleanup_verified'])
            self.assertTrue(report['checks'][0]['stale_error_ignored'])
            self.assertFalse(report['real_provider_requested'])
            self.assertEqual(
                report['owner_rechecks'],
                ['owner_before_signer', 'owner_before_install', 'owner_before_ui'],
            )

    def test_install_commit_then_exception_still_removes_owned_qa_without_opening_ui(self):
        with self.driver() as (device, link, restore, output):
            device.access.install.side_effect = RuntimeError('verification incomplete')
            with self.assertRaises(RuntimeError):
                check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            self.assertIn(
                'cmd package uninstall ' + check.PACKAGE,
                [c.args[0] for c in device.shell.call_args_list],
            )
            restore.assert_not_called()
            self.assertTrue(output.write.call_args.args[0]['cleanup_verified'])

    def test_existing_qa_is_never_replaced_or_removed(self):
        with self.driver() as (device, link, restore, output):
            device.access.package_paths.side_effect = None
            device.access.package_paths.return_value = ['/already/owned.apk']
            with self.assertRaises(RuntimeError):
                check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            device.access.install.assert_not_called()
            self.assertFalse(any('uninstall' in c.args[0] for c in device.shell.call_args_list))

    def test_missing_local_installer_bundle_prevents_any_install_or_ui_takeover(self):
        with self.driver() as (device, link, restore, output):
            device.access.helper_artifact.side_effect = FileNotFoundError('missing local helper')
            with self.assertRaises(FileNotFoundError):
                check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            device.access.install.assert_not_called()
            device.original.home.assert_not_called()
            restore.assert_not_called()
            link.close.assert_not_called()
            check.generate.assert_not_called()

    def test_owner_starting_capture_during_fixture_generation_prevents_install_or_ui(self):
        with self.driver() as (device, link, restore, output):
            with patch('service_smoke.LiveService') as service:
                service.return_value.sample.side_effect = [
                    {'pid': 7, 'instance': 'fixed'},
                    RuntimeError('owner_capture_active'),
                ]
                with self.assertRaisesRegex(RuntimeError, 'owner_capture_active'):
                    check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            device.access.install.assert_not_called()
            device.original.home.assert_not_called()
            restore.assert_not_called()

    def test_owner_camera_activity_during_signer_check_prevents_install_or_ui(self):
        with self.driver() as (device, link, restore, output):
            with patch('service_smoke.LiveService') as service:
                baseline = {'pid': 7, 'instance': 'fixed'}
                service.return_value.sample.side_effect = [
                    baseline,
                    baseline,
                    RuntimeError('owner_camera_active'),
                ]
                with self.assertRaisesRegex(RuntimeError, 'owner_camera_active'):
                    check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            device.access.signature_preflight.assert_called_once()
            device.access.install.assert_not_called()
            device.original.home.assert_not_called()
            restore.assert_not_called()

    def test_owner_using_device_after_install_only_removes_new_qa_without_taking_ui(self):
        with self.driver() as (device, link, restore, output):
            with patch('service_smoke.LiveService') as service:
                baseline = {'pid': 7, 'instance': 'fixed'}
                service.return_value.sample.side_effect = [
                    baseline,
                    baseline,
                    baseline,
                    RuntimeError('owner_left_foreground'),
                ]
                with self.assertRaisesRegex(RuntimeError, 'owner_left_foreground'):
                    check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            device.access.install.assert_called_once()
            device.original.home.assert_not_called()
            restore.assert_not_called()
            commands = [call.args[0] for call in device.shell.call_args_list]
            self.assertIn('cmd package uninstall ' + check.PACKAGE, commands)
            self.assertFalse(any(command.startswith('am start ') for command in commands))
            self.assertTrue(output.write.call_args.args[0]['cleanup_verified'])

    def test_replaced_but_idle_backend_is_not_adopted_as_a_new_qa_target(self):
        with self.driver() as (device, link, restore, output):
            with patch('service_smoke.LiveService') as service:
                service.return_value.sample.side_effect = [
                    {'pid': 7, 'instance': 'fixed'},
                    {'pid': 8, 'instance': 'new'},
                ]
                with self.assertRaisesRegex(RuntimeError, 'production_state_changed'):
                    check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            device.access.install.assert_not_called()
            device.original.home.assert_not_called()
            restore.assert_not_called()

    def test_failed_qa_removal_cannot_be_reported_as_pass(self):
        with self.driver() as (device, link, restore, output):
            device.shell.side_effect = lambda cmd: (
                'Failure' if cmd.startswith('cmd package uninstall ') else ''
            )
            with self.assertRaises(RuntimeError):
                check.run(Path('/runtime'), Path('/private/qa-build.json'), 1, output)
            self.assertEqual(output.write.call_args.args[0]['state'], 'cleanup_unconfirmed')
            self.assertFalse(output.write.call_args.args[0]['cleanup_verified'])
            restore.assert_called_once()


if __name__ == '__main__':
    unittest.main()
