"""Reap an isolated app-server only after draining its owned process group."""

import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
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


def start_guardian(popen, command, *, cwd, env):
    owner, guardian = socket.socketpair()
    try:
        process = popen(
            [
                sys.executable,
                '-I',
                '-S',
                str(Path(__file__).resolve()),
                str(guardian.fileno()),
                *command,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            bufsize=0,
            start_new_session=True,
            pass_fds=(guardian.fileno(),),
            env=env,
        )
    except BaseException:
        owner.close()
        raise
    finally:
        guardian.close()
    return process, owner


def close_guardian(process, owner):
    if owner is not None:
        with owner:
            try:
                owner.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    if process is not None:
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            # Killing the guardian would abandon its separate tool group.
            return False
    return True


def _release_rpc_stdio():
    # The app-server has inherited these pipes. The guardian must not keep
    # another reference to them or delay the host's normal RPC EOF.
    null = os.open(os.devnull, os.O_RDWR)
    try:
        for fd in (0, 1, 2):
            os.dup2(null, fd)
    finally:
        os.close(null)


def guard(lifetime_fd, command):
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with socket.socket(fileno=lifetime_fd) as lifetime, selectors.DefaultSelector() as poll:
        poll.register(lifetime, selectors.EVENT_READ)
        child = None
        try:
            if stopping or poll.select(timeout=0):
                return 0
            child = subprocess.Popen(command, stdin=0, stdout=1, stderr=2, start_new_session=True)
            _release_rpc_stdio()
            interval = 0.2 if hasattr(os, 'waitid') else 1
            while not stopping and not child_exited(child):
                if poll.select(timeout=interval):
                    break
        except OSError:
            return 1
        finally:
            # Signal unavailability before cleanup without writing to RPC.
            try:
                lifetime.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            if child is not None:
                close_owned_group(child)
    return 0


if __name__ == '__main__':
    raise SystemExit(guard(int(sys.argv[1]), sys.argv[2:]))
