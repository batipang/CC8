"""Start/reuse this workspace's USB/Wi-Fi voice service without duplicate workers."""

from pathlib import Path
import argparse
import fcntl
import os
import signal
import shlex
import subprocess
import sys
import time
from configuration import load_config
from config_schema import ConfigError

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'device-access/private/voice-runtime'
BRIDGE = ROOT / 'cc8-assistant/host/bridge.py'
PYTHON = ROOT / '.toolchains/cc8/voice-venv/bin/python'


def own_process(pid):
    # PID 会复用，必须核对当前用户及脚本绝对路径，避免停止别的工作区服务。
    if type(pid) is not int or pid <= 0:
        return False
    result = subprocess.run(
        ['ps', '-p', str(pid), '-o', 'uid=', '-o', 'command='], capture_output=True, text=True
    )
    try:
        owner, command = result.stdout.strip().split(None, 1)
        owner = int(owner)
        arguments = shlex.split(command)
        script = arguments[2:] if arguments[1:2] == ['-u'] else arguments[1:]
    except ValueError:
        return False
    return (
        result.returncode == 0
        and owner == os.getuid()
        and bool(script)
        and script[0] == str(BRIDGE)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument(
        '--no-open',
        action='store_true',
        help='Start/reuse the backend without opening the device UI',
    )
    parser.add_argument(
        '--no-auto-voice',
        action='store_true',
        help='Open the UI without starting a new microphone session',
    )
    parser.add_argument('--echo-check-after-start', action='store_true')
    args = parser.parse_args()
    managed_stop = False
    if args.stop:
        from service import enabled, disable

        if enabled():
            disable()
            managed_stop = True
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PRIVATE, 0o700)
    with (PRIVATE / 'launcher.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid_file = PRIVATE / 'bridge.pid'
        try:
            pid = int(pid_file.read_text())
        except (OSError, ValueError):
            pid = None
        running = bool(pid and own_process(pid))
        if args.stop:
            if running:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while own_process(pid) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if own_process(pid):
                    raise SystemExit('旧服务仍在退出，请稍后再启动。')
            print('大黄蜂语音服务已停止。' if running or managed_stop else '大黄蜂语音服务未运行。')
            return
        if not PYTHON.is_file():
            raise SystemExit('缺少语音运行环境，请执行 python3.13 scripts/setup-voice.py。')
        if not running:
            try:
                load_config()
            except ConfigError as error:
                raise SystemExit(str(error)) from None
            except OSError:
                raise SystemExit(
                    'CC8 configuration file unavailable; check path and owner access'
                ) from None
            log = PRIVATE / 'service.log'
            with log.open('ab') as output:
                command = [str(PYTHON), '-u', str(BRIDGE)]
                if args.echo_check_after_start:
                    command.append('--echo-check-after-start')
                child = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            pid_file.write_text(str(child.pid) + '\n')
            print('本机语音服务已启动，正在准备识别模型。')
        else:
            print('本机语音服务已运行。')
        if args.no_open:
            print('语音后台已准备，未打开设备界面或麦克风。')
            return
        sys.path.insert(0, str(ROOT / 'device-access'))
        import cc8_install as access

        command = 'input keyevent 224; am start --activity-clear-top -n com.happyhome.cc8assistant/.MainActivity'
        command += ' --ez auto_start_voice ' + ('false' if args.no_auto_voice else 'true')
        try:
            print(access.shell(access.target(), command).strip())
        except (OSError, RuntimeError, subprocess.SubprocessError):
            print('未发现可用 USB；请点 CC8 桌面大黄蜂入口，后台等待已配对的局域网连接。')
        print(
            '本次打开不自动开始语音；会话仍遵守前后台和断线停止规则。'
            if args.no_auto_voice
            else '打开后将自动开始语音；首次授权后，请安静等待 6 秒校准。'
        )


if __name__ == '__main__':
    main()
