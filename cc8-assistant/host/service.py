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
from launch import ROOT, PRIVATE, BRIDGE, PYTHON, own_process
from process_lock import ProcessLease, atomic_private_json

LABEL = 'com.happyhome.cc8.'+hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
PLIST = Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
ENABLED = PRIVATE/'service-enabled.json'


def enabled(path=ENABLED):
    try: return json.loads(path.read_text()).get('enabled') is True
    except (OSError, ValueError, AttributeError): return False


def agent_payload():
    # 子进程退避由本进程管理；监督进程自身崩溃才由 launchd 拉起。
    # 明确停止先落盘 enabled=false，即使遇到拉起竞态也不会再启动语音后台。
    return {'Label': LABEL, 'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval':10,
        'ProgramArguments': [str(PYTHON), str(Path(__file__).resolve()), 'run'],
        'WorkingDirectory': str(ROOT), 'ProcessType': 'Background', 'Umask': 0o077}


def disable():
    atomic_private_json(ENABLED, {'enabled': False})
    if PLIST.exists():
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(PLIST)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def install():
    if not PYTHON.is_file() or not BRIDGE.is_file(): raise RuntimeError('Voice environment missing')
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700); os.chmod(PRIVATE, 0o700)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    payload = agent_payload()
    # 本机路径不进入仓库；plist 只包含启动位置，没有模型密钥或配对信息。
    with PLIST.open('wb') as out: plistlib.dump(payload, out)
    os.chmod(PLIST, 0o600)
    atomic_private_json(ENABLED, {'enabled': True})
    result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LABEL}'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode:
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(PLIST)], check=True, timeout=15)
    subprocess.run(['launchctl', 'kickstart', f'gui/{os.getuid()}/{LABEL}'], check=True, timeout=15)


def read_pid():
    try:
        pid = int((PRIVATE/'bridge.pid').read_text())
        return pid if own_process(pid) else None
    except (OSError, ValueError): return None


def retry_delay(failures): return min(30, 2 ** min(max(failures-1, 0), 5))


class Supervisor:
    def __init__(self):
        self.stop = threading.Event(); self.child = None; self.pid = None
        self.log = logging.getLogger('cc8.service'); self.log.setLevel(logging.INFO)
        self.log.propagate = False; self.log.handlers.clear()
        handler = RotatingFileHandler(PRIVATE/'service.log', maxBytes=2*1024*1024, backupCount=3)
        handler.setFormatter(logging.Formatter('%(message)s')); self.log.addHandler(handler)
        os.chmod(PRIVATE/'service.log', 0o600)

    def state(self, phase, failures):
        atomic_private_json(PRIVATE/'supervisor.json', {'schema': 1, 'phase': phase,
            'failures': failures, 'retry_seconds': retry_delay(failures) if phase=='backoff' else 0,
            'pid': self.pid, 'updated_at': time.time(), 'opens_device': False})

    def _drain(self, stream):
        # 即使子进程不换行也限定每次读取大小，日志总量以 2 MiB × 4 份封顶。
        try:
            while True:
                line = stream.readline(65536)
                if not line: break
                self.log.info(line.decode('utf-8', errors='replace').rstrip())
        finally: stream.close()

    def start_child(self):
        self.child = subprocess.Popen([str(PYTHON), '-u', str(BRIDGE)], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.pid = self.child.pid
        threading.Thread(target=self._drain, args=(self.child.stdout,), daemon=True).start()

    def terminate(self):
        if self.pid and own_process(self.pid):
            try:os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:pass
            deadline = time.monotonic()+8
            while own_process(self.pid) and time.monotonic()<deadline: time.sleep(.1)
            if own_process(self.pid):
                try:os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:pass
        if self.child:
            try: self.child.wait(timeout=3)
            except subprocess.TimeoutExpired: pass
        self.pid = None; self.child = None

    def run(self):
        failures = 0
        try:
            while enabled() and not self.stop.is_set():
                # 可接管已在当前目录正常运行的手动服务，避免安装登录服务时重复加载模型。
                self.pid = read_pid()
                if self.pid is None: self.start_child()
                started = time.monotonic(); bad_health = 0
                while enabled() and not self.stop.wait(2):
                    if self.child and self.child.poll() is not None: break
                    if not own_process(self.pid): break
                    try:
                        health = query(operation='health')
                        healthy = all(health.get(k) is True for k in ('model_ready','recognition_worker','response_worker'))
                    except (OSError, ValueError): healthy = False
                    if healthy:
                        bad_health = 0; self.state('ready', failures)
                        if time.monotonic()-started>=60: failures = 0
                    elif time.monotonic()-started>180:
                        bad_health += 1
                        if bad_health>=3: break
                        self.state('unhealthy', failures)
                    else: self.state('starting', failures)
                self.terminate()
                if self.stop.is_set() or not enabled(): break
                failures += 1; self.state('backoff', failures)
                # 只恢复后台进程，不恢复/重放排队请求；重连不能重新开启设备采集。
                if self.stop.wait(retry_delay(failures)): break
        finally:
            self.terminate(); self.state('stopped', failures)
            for handler in self.log.handlers: handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install','start','stop','uninstall','status','run'))
    args = parser.parse_args()
    if args.action in ('install', 'start'): install(); print('登录后台已启用；不打开设备界面或麦克风。')
    elif args.action in ('stop', 'uninstall'):
        disable()
        subprocess.run([str(PYTHON), str(ROOT/'cc8-assistant/host/launch.py'), '--stop'], check=True)
        if args.action=='uninstall': PLIST.unlink(missing_ok=True)
        print('后台已明确停止，崩溃恢复不会将其重新拉起。')
    elif args.action=='status':
        try: health = query(operation='health')
        except (OSError, ValueError): health = {'ok': False}
        print(json.dumps({'enabled': enabled(), 'installed': PLIST.exists(), 'health': health}, ensure_ascii=False))
    elif enabled():
        os.umask(0o077)
        try:
            with ProcessLease(PRIVATE/'supervisor.lock'):
                supervisor = Supervisor()
                for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: supervisor.stop.set())
                supervisor.run()
        except BlockingIOError: raise SystemExit('当前目录已有后台监督进程。')


if __name__=='__main__': main()
