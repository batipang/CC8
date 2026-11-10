"""Read-only local OS/service evidence for an explicitly armed login check."""

import ast
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

from diagnose import query
from process_arguments import process_arguments
from release_info import load_release, source_stamp
from configuration import profile_path
from login_receipt import require, LoginCheckFailure


def command(args, limit=1024 * 1024):
    try:
        value = subprocess.run(args, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise LoginCheckFailure('login_os_receipt_unavailable') from None
    require(len(value.encode()) <= limit, 'login_os_receipt_oversized')
    return value


def gui_session(raw, uid):
    # launchctl print is a diagnostic interface: reject an unfamiliar format,
    # rather than interpreting a process PID/Unix SID as a GUI login event.
    require(
        raw.startswith(f'gui/{uid} = {{\n') and raw.rstrip().endswith('}'),
        'login_domain_unconfirmed',
    )
    for key, expected in (('type', 'login'), ('session', 'Aqua')):
        require(
            re.findall(r'^\t' + key + r' = (.+)$', raw, re.M) == [expected],
            'login_domain_unconfirmed',
        )
    contexts = re.findall(
        r'^\tsecurity context = \{\n\t\tuid = ([0-9]+)\n\t\tasid = ([0-9]+)\n\t\}', raw, re.M
    )
    require(
        len(contexts) == 1 and int(contexts[0][0]) == uid and int(contexts[0][1]) > 0,
        'login_domain_unconfirmed',
    )
    return contexts[0][1]


def fingerprint(nonce, value):
    return hashlib.sha256((nonce + '\0' + value).encode()).hexdigest()


def observer_supported(root):
    """Inspect the actual runtime source, not the CLI caller's newer files."""
    helper = root / 'cc8-assistant/host/check_login.py'
    source = root / 'cc8-assistant/host/service.py'
    if not helper.is_file():
        return False
    with source.open('rb') as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        return False
    try:
        tree = ast.parse(raw)
    except (SyntaxError, UnicodeError):
        return False
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == 'check_login'
        and any(alias.name == 'start_login_check' and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )
    return imported and any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == 'start_login_check'
        for node in ast.walk(tree)
    )


def file_identity(
    path, *, private=True, optional=False, metadata=False, limit=1024 * 1024, include_data=False
):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        require(optional and not path.is_symlink(), 'login_owner_file_missing')
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & (0o077 if private else 0o022)
            and 0 < info.st_size <= limit,
            'login_owner_file_unsafe',
        )
        raw = stream.read(limit + 1)
        require(len(raw) == info.st_size, 'login_owner_file_changed')
    result = dict(sha256=hashlib.sha256(raw).hexdigest())
    if metadata:
        result.update(
            device=info.st_dev,
            inode=info.st_ino,
            mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
        )
    return (result, raw) if include_data else result


class LoginProbe:
    def __init__(self, root, *, supervisor_pid=None):
        require(sys.platform == 'darwin', 'login_checks_require_macos')
        require(root.is_absolute() and root.resolve() == root, 'login_root_not_real_absolute')
        self.root = root
        self.private = root / 'device-access/private/voice-runtime'
        self.release = load_release(root)
        self.label = 'com.happyhome.cc8.' + hashlib.sha256(str(root).encode()).hexdigest()[:12]
        self.plist = Path.home() / 'Library/LaunchAgents' / (self.label + '.plist')
        self.supervisor_pid = supervisor_pid

    def session(self, nonce):
        uid = os.getuid()
        asid = gui_session(command(['/bin/launchctl', 'print', f'gui/{uid}']), uid)
        boot = command(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], 128).strip()
        require(
            re.fullmatch('[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', boot),
            'login_boot_identity_unconfirmed',
        )
        return fingerprint(nonce, f'{uid}:{boot.lower()}:{asid}')

    def identity(self):
        marker = self.private / 'service-enabled.json'
        # Check identity before reading structured metadata; no contents are
        # copied to the diagnostic file, including the full original ENV.
        agent_id, agent_raw = file_identity(
            self.plist, metadata=True, limit=16384, include_data=True
        )
        marker_id, marker_raw = file_identity(marker, metadata=True, limit=1024, include_data=True)
        agent = plistlib.loads(agent_raw)
        require(json.loads(marker_raw) == {'enabled': True}, 'login_service_disabled')
        require(
            agent.get('RunAtLoad') is True
            and agent.get('Label') == self.label
            and agent.get('WorkingDirectory') == str(self.root)
            and agent.get('ProgramArguments')
            == [
                str(self.root / '.toolchains/cc8/voice-venv/bin/python'),
                str(self.root / 'cc8-assistant/host/service.py'),
                'run',
            ],
            'login_agent_source_mismatch',
        )
        stamp = source_stamp(self.root)
        require(stamp.commit is not None and stamp.dirty is False, 'login_source_not_clean')
        environment = agent.get('EnvironmentVariables')
        require(
            isinstance(environment, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()),
            'login_agent_environment_unconfirmed',
        )
        paths = [
            self.private / name
            for name in (
                'reminders.json',
                'preference-memory.json',
                'recognition-vocabulary.json',
                'work-report-sources.json',
                'work-report-archive.json',
                'work-report-progress.json',
                'work-report-watch.json',
            )
        ]
        return dict(
            agent=agent_id,
            enabled=marker_id,
            source_commit=stamp.commit,
            manifest_sha256=stamp.manifest_sha256,
            owner_config=file_identity(Path.home() / '.codex/config.toml', private=False),
            profile=file_identity(profile_path(self.root, environment), optional=True),
            stores=[file_identity(path, optional=True) for path in paths],
        )

    def processes(self):
        from service_smoke import process_rows

        raw = command(
            ['/bin/ps', '-axww', '-o', 'pid=', '-o', 'uid=', '-o', 'ppid=', '-o', 'command=']
        )
        return process_rows(raw, self.root, os.getuid())

    def sample(self, nonce):
        rows = self.processes()
        require(
            not rows['unexpected'] and len(rows['supervisor']) == 1 and len(rows['bridge']) <= 1,
            'login_service_not_unique',
        )
        supervisor, parent = rows['supervisor'][0]
        require(
            parent == 1 and (self.supervisor_pid is None or supervisor == self.supervisor_pid),
            'login_supervisor_owner_changed',
        )
        target = f'gui/{os.getuid()}/{self.label}'
        raw = command(['/bin/launchctl', 'print', target], 65536)
        require(
            re.findall(r'^\s*pid = ([1-9][0-9]*)\s*$', raw, re.M) == [str(supervisor)],
            'login_launchd_owner_changed',
        )
        if not rows['bridge']:
            return None
        pid, parent = rows['bridge'][0]
        adopted = parent == 1
        if adopted:
            from private_output import read_private_json
            import time

            try:
                receipt = read_private_json(self.private / 'supervisor.json', 4096)
            except FileNotFoundError:
                return None
            updated = receipt.get('updated_at')
            if not (
                receipt.get('phase') == 'ready'
                and receipt.get('pid') == pid
                and type(updated) in (int, float)
                and math.isfinite(updated)
                and 0 <= time.time() - updated <= 6
            ):
                return None  # The supervisor may not have published first ready yet.
        else:
            require(parent == supervisor, 'login_worker_not_supervisor_child')
        require(
            process_arguments(pid)[1:] == ['-u', str(self.root / 'cc8-assistant/host/bridge.py')],
            'login_worker_arguments_changed',
        )
        try:
            health = query(self.private / 'announce.sock', 'health', timeout=1)
        except (OSError, ValueError):
            return None
        require(
            health.get('bridge_version') == self.release.backend_version
            and health.get('release_id') == self.release.release_id
            and type(health.get('protocol_version')) is int
            and health['protocol_version'] == self.release.protocol_version,
            'login_backend_source_changed',
        )
        require(
            type(health.get('runtime_process_id')) is int
            and health['runtime_process_id'] == pid
            and type(health.get('health_owner_version')) is int
            and health['health_owner_version'] == 1,
            'login_worker_health_owner_unconfirmed',
        )
        if not all(
            health.get(key) is True
            for key in ('model_ready', 'recognition_worker', 'response_worker')
        ):
            return None
        instance = health.get('runtime_instance')
        require(
            isinstance(instance, str) and re.fullmatch('[a-f0-9]{32}', instance),
            'login_worker_instance_invalid',
        )
        idle = all(
            health.get(key) is False
            for key in (
                'microphone_active',
                'playing',
                'calibrating',
                'text_request_active',
                'vision_active',
                'desktop_snapshot_active',
            )
        )
        counters = [
            health.get(key)
            for key in ('voice_asr_attempts', 'mic_frames', 'render_frames', 'local_wake_count')
        ]
        require(
            all(type(n) is int and n >= 0 for n in counters), 'login_activity_counters_unconfirmed'
        )
        try:
            diagnostics = query(self.private / 'announce.sock', 'diagnostics', timeout=1)
        except (OSError, ValueError):
            return None
        requests = diagnostics.get('requests')
        require(
            isinstance(requests, list) and all(isinstance(r, dict) for r in requests),
            'login_requests_unconfirmed',
        )
        ids = [row.get('request_id') for row in requests]
        require(
            len(ids) <= 128
            and all(isinstance(i, str) and 0 < len(i) <= 128 for i in ids)
            and len(set(ids)) == len(ids),
            'login_requests_unconfirmed',
        )
        connected, route = health.get('connected'), health.get('transport')
        require(
            type(connected) is bool
            and route in ('none', 'usb', 'wifi')
            and connected == (route != 'none'),
            'login_route_unconfirmed',
        )
        if self.processes() != rows:
            return None  # A buffered healthy reply cannot outlive its worker.
        return dict(
            pid=pid,
            process=fingerprint(nonce, str(pid)),
            instance=fingerprint(nonce, instance),
            adopted=adopted,
            activity=dict(counters=counters, requests=fingerprint(nonce, json.dumps(sorted(ids)))),
            idle=idle,
            activity_zero=all(n == 0 for n in counters) and not requests,
            requests_idle=all(r.get('outcome') is not None for r in requests),
            connected=connected,
            transport=route,
        )
