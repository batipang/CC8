"""Owner-level login service; never opens CC8 UI, microphone or camera."""

import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import threading
import time

from diagnose import query
from launch import (
    ROOT,
    PRIVATE,
    BRIDGE,
    PYTHON,
    WorkerPidWriteError,
    own_process,
    publish_worker_pid,
)
from process_lock import ProcessLease, atomic_private_bytes, atomic_private_json
from runtime_env import command_path
from configuration import load_config
from config_schema import ConfigError
from runtime_guard import _private_file, ensure_available, RuntimeConflict

LABEL = 'com.happyhome.cc8.' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
PLIST = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
ENABLED = PRIVATE / 'service-enabled.json'


def enabled(path=ENABLED):
    try:
        return json.loads(path.read_text()).get('enabled') is True
    except (OSError, ValueError, AttributeError):
        return False


def agent_payload():
    # 子进程退避由本进程管理；监督进程自身崩溃才由 launchd 拉起。
    # 明确停止先落盘 enabled=false，即使遇到拉起竞态也不会再启动语音后台。
    config = load_config()
    return {
        'Label': LABEL,
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 10,
        'ProgramArguments': [str(PYTHON), str(Path(__file__).resolve()), 'run'],
        'WorkingDirectory': str(ROOT),
        # This process serves explicit, deadline-bound voice interactions.
        # Background throttling is inherited by its local model children and
        # can make their unchanged startup deadlines fail before readiness.
        # The socket-based service cannot use XPC Adaptive transactions.
        'ProcessType': 'Interactive',
        'Umask': 0o077,
        'EnvironmentVariables': {'PATH': command_path(), **config.service_environment(os.environ)},
    }


class ServiceRegistrationError(RuntimeError):
    pass


def _launchctl(*args, timeout=15, check=False, capture=False):
    return subprocess.run(
        ['/bin/launchctl', *args],
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=check,
    )


def _registered(domain, target):
    result = _launchctl('print', target, timeout=10, capture=True)
    if result.returncode == 0:
        return result.stdout
    # A failed lookup also occurs when the login domain itself is unavailable.
    _launchctl('print', domain, timeout=10, check=True)
    return None


def _snapshot(path, limit):
    try:
        return _private_file(path, limit)
    except FileNotFoundError:
        return None


def _restore(path, previous, *, private_parent=True):
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_private_bytes(path, previous, private_parent=private_parent)


def _rollback_install(domain, target, marker, plist, bootstrap_attempted):
    try:
        _restore(ENABLED, marker)
    finally:
        try:
            if bootstrap_attempted:
                current = _registered(domain, target)
                if current is not None:
                    path = next(
                        (
                            line.strip().removeprefix('path = ')
                            for line in current.splitlines()
                            if line.strip().startswith('path = ')
                        ),
                        None,
                    )
                    if path is None or Path(path).resolve() != PLIST.resolve():
                        raise ServiceRegistrationError('无法核实新注册的登录任务。')
                    _launchctl('bootout', target, check=True)
        finally:
            _restore(PLIST, plist, private_parent=False)


def _disable():
    atomic_private_json(ENABLED, {'enabled': False})
    if PLIST.exists():
        _launchctl('bootout', f'gui/{os.getuid()}', str(PLIST))


def disable():
    with ProcessLease(PRIVATE / 'service-change.lock'):
        _disable()


def install():
    with ProcessLease(PRIVATE / 'service-change.lock'):
        ensure_available(ROOT)
        if not PYTHON.is_file() or not BRIDGE.is_file():
            raise ServiceRegistrationError('缺少语音运行环境。')
        payload = plistlib.dumps(agent_payload())
        marker = _snapshot(ENABLED, 1024)
        plist = _snapshot(PLIST, 16384)
        domain = f'gui/{os.getuid()}'
        target = f'{domain}/{LABEL}'
        try:
            loaded = _registered(domain, target) is not None
        except (OSError, subprocess.SubprocessError):
            raise ServiceRegistrationError('无法访问当前用户的 launchd 登录域。') from None
        bootstrap_attempted = False
        try:
            atomic_private_bytes(PLIST, payload, private_parent=False)
            atomic_private_json(ENABLED, {'enabled': True})
            if not loaded:
                bootstrap_attempted = True
                _launchctl('bootstrap', domain, str(PLIST), check=True)
            _launchctl('kickstart', target, check=True)
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            try:
                _rollback_install(domain, target, marker, plist, bootstrap_attempted)
            except (OSError, subprocess.SubprocessError, ServiceRegistrationError):
                raise ServiceRegistrationError('登录任务注册失败，无法确认已完全回退。') from None
            if isinstance(error, KeyboardInterrupt):
                raise
            raise ServiceRegistrationError('登录任务注册失败，已恢复先前设置。') from None


def read_pid():
    try:
        pid = int((PRIVATE / 'bridge.pid').read_text())
        return pid if own_process(pid) else None
    except (OSError, ValueError):
        return None


def retry_delay(failures):
    return min(30, 2 ** min(max(failures - 1, 0), 5))


def worker_health(health: object, pid: object) -> str:
    """Classify one current reply; PID metadata is not peer authentication."""
    if pid is None:
        return 'unavailable'
    if not isinstance(health, dict) or type(pid) is not int or not 1 < pid < 2**31:
        return 'invalid_owner'
    ready = all(
        health.get(k) is True
        for k in ('ok', 'model_ready', 'recognition_worker', 'response_worker')
    )
    # A healthy old backend may still be adopted without replacing it solely
    # because its schema lacks attribution. Do not claim verified readiness.
    if 'health_owner_version' not in health and 'runtime_process_id' not in health:
        return 'legacy_unverified' if ready else 'unhealthy'
    if (
        type(health.get('health_owner_version')) is not int
        or health['health_owner_version'] != 1
        or type(health.get('runtime_process_id')) is not int
        or not 1 < health['runtime_process_id'] < 2**31
    ):
        return 'invalid_owner'
    if health['runtime_process_id'] != pid:
        return 'wrong_owner'
    return 'ready' if ready else 'unhealthy'


class Supervisor:
    def __init__(self):
        self.stop = threading.Event()
        self.child = None
        self.pid = None
        self.log = logging.getLogger('cc8.service')
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        self.log.handlers.clear()
        handler = RotatingFileHandler(
            PRIVATE / 'service.log', maxBytes=2 * 1024 * 1024, backupCount=3
        )
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.log.addHandler(handler)
        os.chmod(PRIVATE / 'service.log', 0o600)

    def state(self, phase, failures):
        receipt = {
            'schema': 1,
            'phase': phase,
            'failures': failures,
            'retry_seconds': retry_delay(failures) if phase == 'backoff' else 0,
            'pid': self.pid,
            'updated_at': time.time(),
            'opens_device': False,
        }
        try:
            atomic_private_json(PRIVATE / 'supervisor.json', receipt)
        except OSError as error:
            # A diagnostic receipt is not a reason to terminate the backend.
            self.log.warning('supervisor_state_write_failed error_type=%s', type(error).__name__)

    def _drain(self, stream):
        # 即使子进程不换行也限定每次读取大小，日志总量以 2 MiB × 4 份封顶。
        try:
            while True:
                line = stream.readline(65536)
                if not line:
                    break
                self.log.info(line.decode('utf-8', errors='replace').rstrip())
        finally:
            stream.close()

    def start_child(self):
        self.child = subprocess.Popen(
            [str(PYTHON), '-u', str(BRIDGE)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.pid = self.child.pid
        # Model loading happens in the child. Publish its PID before releasing
        # the shared launcher lease so a concurrent manual open can reuse it.
        try:
            publish_worker_pid(PRIVATE / 'bridge.pid', self.child)
        except WorkerPidWriteError:
            self.pid = None
            self.child = None
            raise
        threading.Thread(target=self._drain, args=(self.child.stdout,), daemon=True).start()

    def terminate(self):
        if self.child is not None:
            # An unreaped child remains ours even if the OS cannot read argv.
            # Reap it before the supervisor can start another worker.
            if self.child.poll() is None:
                self.child.terminate()
            try:
                self.child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=3)
        elif self.pid and own_process(self.pid):
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 8
            while own_process(self.pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if own_process(self.pid):
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.pid = None
        self.child = None

    def run(self):
        failures = 0
        try:
            while enabled() and not self.stop.is_set():
                try:
                    with ProcessLease(PRIVATE / 'launcher.lock'):
                        if not enabled() or self.stop.is_set():
                            break
                        # Adoption and launch are serialized with the manual
                        # launcher, including the worker's initial PID receipt.
                        self.pid = read_pid()
                        if self.pid is None:
                            ensure_available(ROOT)
                            if not enabled() or self.stop.is_set():
                                break
                            self.start_child()
                except BlockingIOError:
                    if self.stop.wait(0.5):
                        break
                    continue
                except RuntimeConflict:
                    self.state('other_runtime', failures)
                    if self.stop.wait(30):
                        break
                    continue
                except WorkerPidWriteError as error:
                    self.log.warning(
                        'worker_pid_write_failed error_type=%s', type(error.__cause__).__name__
                    )
                started = time.monotonic()
                bad_health = 0
                was_ready = False
                while self.pid is not None and enabled() and not self.stop.wait(2):
                    if self.child is not None:
                        if self.child.poll() is not None:
                            break
                    elif not own_process(self.pid):
                        break
                    try:
                        health = query(operation='health')
                        state = worker_health(health, self.pid)
                    except (OSError, ValueError):
                        state = 'unhealthy'
                    # The reply may have been buffered before its worker exited.
                    # Recheck the owned process before publishing its readiness.
                    if self.child is not None:
                        if self.child.poll() is not None:
                            break
                    elif not own_process(self.pid):
                        break
                    if state in ('ready', 'legacy_unverified'):
                        # Legacy responsiveness retains the normal recovery
                        # budget, but remains explicitly unverified externally.
                        was_ready = True
                        bad_health = 0
                        self.state(state, failures)
                        if time.monotonic() - started >= 60:
                            failures = 0
                    # Loading grace applies only until this worker first becomes
                    # ready. A later outage must use the normal failure budget.
                    elif (
                        state in ('wrong_owner', 'invalid_owner')
                        or was_ready
                        or time.monotonic() - started > 180
                    ):
                        bad_health += 1
                        if bad_health >= 3:
                            break
                        self.state(state, failures)
                    else:
                        self.state('starting', failures)
                self.terminate()
                if self.stop.is_set() or not enabled():
                    break
                failures += 1
                self.state('backoff', failures)
                # 只恢复后台进程，不恢复/重放排队请求；重连不能重新开启设备采集。
                if self.stop.wait(retry_delay(failures)):
                    break
        finally:
            self.terminate()
            self.state('stopped', failures)
            for handler in self.log.handlers:
                handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'action', choices=('install', 'start', 'stop', 'uninstall', 'status', 'run')
    )
    args = parser.parse_args()
    if args.action in ('install', 'start'):
        try:
            install()
        except (ConfigError, RuntimeConflict, ServiceRegistrationError) as error:
            parser.error(str(error))
        except OSError:
            parser.error('无法读取登录服务设置，或另一个管理操作仍在进行。')
        print('登录后台已启用；不打开设备界面或麦克风。')
    elif args.action in ('stop', 'uninstall'):
        try:
            with ProcessLease(PRIVATE / 'service-change.lock'):
                _disable()
                subprocess.run(
                    [str(PYTHON), str(ROOT / 'cc8-assistant/host/launch.py'), '--stop'], check=True
                )
                if args.action == 'uninstall':
                    PLIST.unlink(missing_ok=True)
        except BlockingIOError:
            parser.error('登录服务正在变更，请稍后重试。')
        print('后台已明确停止，崩溃恢复不会将其重新拉起。')
    elif args.action == 'status':
        try:
            health = query(operation='health')
        except (OSError, ValueError):
            health = {'ok': False}
        print(
            json.dumps(
                {
                    'enabled': enabled(),
                    'installed': PLIST.exists(),
                    'health': health,
                    'worker_health': worker_health(health, read_pid()),
                },
                ensure_ascii=False,
            )
        )
    elif enabled():
        os.umask(0o077)
        try:
            with ProcessLease(PRIVATE / 'supervisor.lock'):
                supervisor = Supervisor()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: supervisor.stop.set())
                supervisor.run()
        except BlockingIOError:
            raise SystemExit('当前目录已有后台监督进程。')


if __name__ == '__main__':
    main()
