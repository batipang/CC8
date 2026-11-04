"""Reap an isolated app-server only after draining its owned process group."""

import os
import signal
import subprocess
import time


def child_exited(process):
    """Check the group leader without releasing its reserved process ID."""
    if process.returncode is not None:
        return True
    if hasattr(os, 'waitid'):
        try:
            return (
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            )
        except ChildProcessError:
            return True
        except OSError:
            pass
    try:
        result = subprocess.run(
            ['/bin/ps', '-p', str(process.pid), '-o', 'stat='],
            capture_output=True,
            text=True,
            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
            timeout=0.2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return not result.stdout.strip() or result.stdout.lstrip().startswith('Z')


def _group_live(group, timeout):
    try:
        result = subprocess.run(
            ['/bin/ps', '-axo', 'pgid=,stat='],
            capture_output=True,
            text=True,
            check=True,
            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(group) and not fields[1].startswith('Z'):
            return True
    return False


def _signal_group(group, sig):
    try:
        os.killpg(group, sig)
    except OSError:
        pass


def _wait_group(group, seconds):
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        if not _group_live(group, min(remaining, 0.2)):
            return True
        time.sleep(min(max(0, deadline - time.monotonic()), 0.05))
    return False


def close_owned_group(process):
    # The app-server was launched in a new session. Keep its PID unreaped
    # until the last group signal, including when the leader is already a zombie.
    if process.returncode is not None:
        return
    group = process.pid
    _signal_group(group, signal.SIGTERM)
    if not _wait_group(group, 2):
        _signal_group(group, signal.SIGKILL)
        _wait_group(group, 0.5)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
