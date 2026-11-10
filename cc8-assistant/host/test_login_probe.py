"""Real local IPC/private metadata with synthetic OS process and GUI receipts only."""

from copy import deepcopy
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from announce import AnnouncementServer
from login_probe import LoginProbe, gui_session, file_identity, command
from login_receipt import LoginCheckFailure
from process_lock import atomic_private_json


class LoginProbeTests(unittest.TestCase):
    def test_os_command_failure_or_deadline_has_only_a_fixed_diagnostic(self):
        for error in (
            subprocess.CalledProcessError(1, ['private-command'], output='private-output'),
            subprocess.TimeoutExpired(['private-command'], 5),
            OSError('private-message'),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch('login_probe.subprocess.run', side_effect=error),
            ):
                with self.assertRaisesRegex(LoginCheckFailure, '^login_os_receipt_unavailable$'):
                    command(['/not-executed'])

    def test_gui_identity_is_a_login_domain_not_a_pid_unix_sid_or_nested_field(self):
        uid = os.getuid()
        raw = f'gui/{uid} = {{\n\ttype = login\n\tsession = Aqua\n\tsecurity context = {{\n\t\tuid = {uid}\n\t\tasid = 100015\n\t}}\n}}\n'
        self.assertEqual(gui_session(raw, uid), '100015')
        for invalid in (
            raw.replace('Aqua', 'Background'),
            raw.replace('type = login', 'type = user'),
            raw.replace('asid = 100015', 'pid = 100015'),
            raw.replace('\tsession', '\t\tsession'),
            raw.replace(f'uid = {uid}', f'uid = {uid + 1}'),
            raw + '\tsession = Aqua\n',
            raw.replace('100015', '0'),
        ):
            with self.subTest(raw=invalid), self.assertRaises(LoginCheckFailure):
                gui_session(invalid, uid)
        probe = LoginProbe.__new__(LoginProbe)
        with patch(
            'login_probe.command', side_effect=[raw, '00000000-0000-0000-0000-000000000001\n']
        ):
            first = probe.session('a' * 32)
        with patch(
            'login_probe.command', side_effect=[raw, '00000000-0000-0000-0000-000000000002\n']
        ):
            self.assertNotEqual(probe.session('a' * 32), first)
        self.assertNotIn('100015', first)

    def test_configuration_hashes_detect_manual_start_without_exporting_environment_or_profile(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            probe = LoginProbe.__new__(LoginProbe)
            probe.root, probe.private, probe.label = root, root / 'private', 'fixture'
            probe.private.mkdir(mode=0o700)
            home = root / 'home'
            (home / '.codex').mkdir(parents=True)
            (home / '.codex/config.toml').write_text('owner-config-never-export')
            profile = root / 'profile.json'
            profile.write_text('owner-profile-never-export')
            profile.chmod(0o600)
            probe.plist = root / 'agent.plist'
            probe.plist.write_bytes(
                plistlib.dumps(
                    dict(
                        RunAtLoad=True,
                        Label='fixture',
                        WorkingDirectory=str(root),
                        ProgramArguments=[
                            str(root / '.toolchains/cc8/voice-venv/bin/python'),
                            str(root / 'cc8-assistant/host/service.py'),
                            'run',
                        ],
                        EnvironmentVariables={
                            'CC8_CONFIG_FILE': str(profile),
                            'OWNER_VALUE': 'never-export-env',
                        },
                    )
                )
            )
            probe.plist.chmod(0o600)
            marker = probe.private / 'service-enabled.json'
            atomic_private_json(marker, {'enabled': True})
            with (
                patch('pathlib.Path.home', return_value=home),
                patch(
                    'login_probe.source_stamp',
                    return_value=SimpleNamespace(
                        commit='a' * 40, dirty=False, manifest_sha256='b' * 64
                    ),
                ),
            ):
                before = probe.identity()
                self.assertEqual(probe.identity(), before)
                raw = json.dumps(before)
                for private in (
                    'never-export-env',
                    'owner-profile-never-export',
                    'owner-config-never-export',
                ):
                    self.assertNotIn(private, raw)
                atomic_private_json(marker, {'enabled': True})
                self.assertNotEqual(
                    probe.identity(), before
                )  # install/start rewrites identical bytes.
            profile.unlink()
            profile.symlink_to(marker)
            with self.assertRaises(OSError):
                file_identity(profile)

    def test_actual_owner_socket_health_is_attributed_and_rechecked_after_reply(self):
        for fault in (
            None,
            'wrong_pid',
            'wrong_source',
            'microphone',
            'activity',
            'request',
            'exit_after_reply',
            'adopted',
            'stale_adoption',
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                probe = LoginProbe.__new__(LoginProbe)
                probe.root, probe.private, probe.label, probe.supervisor_pid = (
                    root,
                    root,
                    'fixture',
                    100,
                )
                probe.release = SimpleNamespace(
                    backend_version='test', release_id='test', protocol_version=2
                )
                health = dict(
                    ok=True,
                    bridge_version='other' if fault == 'wrong_source' else 'test',
                    release_id='test',
                    protocol_version=2,
                    runtime_process_id=201 if fault == 'wrong_pid' else 200,
                    health_owner_version=1,
                    model_ready=True,
                    recognition_worker=True,
                    response_worker=True,
                    runtime_instance='a' * 32,
                    microphone_active=fault == 'microphone',
                    playing=False,
                    calibrating=False,
                    text_request_active=False,
                    vision_active=False,
                    desktop_snapshot_active=False,
                    voice_asr_attempts=1 if fault == 'activity' else 0,
                    mic_frames=0,
                    render_frames=0,
                    local_wake_count=0,
                    connected=False,
                    transport='none',
                )
                requests = (
                    [dict(request_id='bounded-request', outcome='completed')]
                    if fault == 'request'
                    else []
                )
                bridge = SimpleNamespace(
                    health=lambda: health,
                    diagnostics_report=lambda: dict(ok=True, requests=requests),
                )
                server = AnnouncementServer(bridge, root / 'announce.sock')
                server.start()
                rows = dict(
                    unexpected=[],
                    supervisor=[(100, 1)],
                    bridge=[(200, 1 if fault in ('adopted', 'stale_adoption') else 100)],
                )
                after = dict(rows, bridge=[]) if fault == 'exit_after_reply' else deepcopy(rows)
                if fault in ('adopted', 'stale_adoption'):
                    import time

                    atomic_private_json(
                        root / 'supervisor.json',
                        dict(
                            phase='ready',
                            pid=200,
                            updated_at=time.time() - (60 if fault == 'stale_adoption' else 0),
                        ),
                    )
                try:
                    with (
                        patch.object(probe, 'processes', side_effect=[rows, after]),
                        patch('login_probe.command', return_value='\tpid = 100\n'),
                        patch(
                            'login_probe.process_arguments',
                            return_value=[
                                'python',
                                '-u',
                                str(root / 'cc8-assistant/host/bridge.py'),
                            ],
                        ),
                    ):
                        if fault in ('wrong_pid', 'wrong_source'):
                            with self.assertRaises(LoginCheckFailure):
                                probe.sample('b' * 32)
                        else:
                            value = probe.sample('b' * 32)
                            if fault in ('exit_after_reply', 'stale_adoption'):
                                self.assertIsNone(value)
                            else:
                                self.assertEqual(value['idle'], fault != 'microphone')
                                self.assertEqual(
                                    value['activity_zero'], fault not in ('activity', 'request')
                                )
                                self.assertEqual(value['adopted'], fault == 'adopted')
                                self.assertNotEqual(value['instance'], 'a' * 32)
                finally:
                    server.close()
