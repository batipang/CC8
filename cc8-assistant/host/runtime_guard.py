"""Single owner runtime across checkouts, including the older per-checkout launcher.

The OS lease covers model setup/loading and worker lifetime. The bounded process
and LaunchAgent checks recognize older workers that do not yet take that lease.
They never stop another checkout, trust a PID file, or expose process arguments.
"""

from contextlib import ExitStack
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

from process_lock import ProcessLease, read_private_bytes


class RuntimeConflict(RuntimeError):
    pass


def owner_lease_path():
    base = Path.home() / (
        'Library/Application Support' if sys.platform == 'darwin' else '.local/state'
    )
    return base / 'CC8/runtime/owner.lock'


def process_roots(output, uid=None):
    uid = os.getuid() if uid is None else uid
    roots = set()
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or fields[0] != str(uid):
            continue
        # ps prints argv without shell quoting. Anchor at the Python executable
        # and script suffix, preserving spaces inside the absolute checkout path.
        match = re.fullmatch(
            r'.*?python[0-9.]*\s+(?:-u\s+)?(/.+)/cc8-assistant/host/'
            r'(bridge|service)\.py(?:\s+(.*))?',
            fields[2],
            re.IGNORECASE,
        )
        if match and (match[2] == 'bridge' or match[3] == 'run'):
            roots.add(Path(match[1]).resolve())
    return roots


def active_roots():
    try:
        result = subprocess.run(
            ['/bin/ps', '-axo', 'uid=,pid=,command='],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        if len(result.stdout) > 2 * 1024 * 1024:
            raise RuntimeConflict(
                'Cannot verify runtime ownership; retry cc8 doctor --profile voice'
            )
        return process_roots(result.stdout)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise RuntimeConflict(
            'Cannot verify runtime ownership; retry cc8 doctor --profile voice'
        ) from None


def enabled_service_roots(directory=None):
    """Also reserve enabled old services while launchd is between restart attempts."""
    import json

    directory = directory or Path.home() / 'Library/LaunchAgents'
    roots = set()
    for path in directory.glob('com.happyhome.cc8.*.plist'):
        try:
            value = plistlib.loads(read_private_bytes(path, 16384))
            args = value.get('ProgramArguments')
            if not isinstance(args, list) or len(args) != 3 or args[2] != 'run':
                continue
            script = Path(args[1])
            if not script.is_absolute() or script.parts[-3:] != (
                'cc8-assistant',
                'host',
                'service.py',
            ):
                continue
            root = script.parents[2].resolve()
            marker = root / 'device-access/private/voice-runtime/service-enabled.json'
            if json.loads(read_private_bytes(marker, 1024)).get('enabled') is True:
                roots.add(root)
        except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
            continue
    return roots


def other_runtime(root):
    return bool((active_roots() | enabled_service_roots()) - {Path(root).resolve()})


def ensure_available(root):
    if other_runtime(root):
        raise RuntimeConflict(
            'Another checkout owns the CC8 runtime. Stop it from its own directory first; '
            'no service was stopped or replaced.'
        )


class RuntimeLease:
    def __init__(self, root, owner_path=None):
        self.root = Path(root).resolve()
        self.owner_path = owner_path or owner_lease_path()
        self.stack = ExitStack()

    def __enter__(self):
        try:
            self.stack.enter_context(ProcessLease(self.owner_path))
            ensure_available(self.root)
            self.stack.enter_context(
                ProcessLease(self.root / 'device-access/private/voice-runtime/bridge.lock')
            )
        except BaseException:
            self.stack.close()
            raise
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)
