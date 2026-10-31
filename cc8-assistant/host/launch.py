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

ROOT=Path(__file__).resolve().parents[2]
PRIVATE=ROOT/'device-access/private/voice-runtime'
BRIDGE=ROOT/'cc8-assistant/host/bridge.py'
PYTHON=ROOT/'.toolchains/cc8/voice-venv/bin/python'


def own_process(pid):
    if type(pid) is not int or pid<=0:return False
    result=subprocess.run(['ps','-p',str(pid),'-o','uid=','-o','command='],capture_output=True,text=True)
    try:
        owner,command=result.stdout.strip().split(None,1)
        owner=int(owner);arguments=shlex.split(command)
        script=arguments[2:] if arguments[1:2]==['-u'] else arguments[1:]
    except ValueError:return False
    return result.returncode==0 and owner==os.getuid() and bool(script) and script[0]==str(BRIDGE)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop',action='store_true')
    parser.add_argument('--echo-check-after-start',action='store_true')
    args=parser.parse_args()
    PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(PRIVATE,0o700)
    with (PRIVATE/'launcher.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        pid_file=PRIVATE/'bridge.pid'
        try:pid=int(pid_file.read_text())
        except (OSError,ValueError):pid=None
        running=bool(pid and own_process(pid))
        if args.stop:
            if running:
                os.kill(pid,signal.SIGTERM)
                deadline=time.monotonic()+5
                while own_process(pid) and time.monotonic()<deadline:time.sleep(.1)
                if own_process(pid):raise SystemExit('旧服务仍在退出，请稍后再启动。')
            print('大黄蜂语音服务已停止。' if running else '大黄蜂语音服务未运行。')
            return
        if not PYTHON.is_file():raise SystemExit('缺少语音运行环境，请执行 python3.13 scripts/setup-voice.py。')
        if not running:
            log=PRIVATE/'service.log'
            with log.open('ab') as output:
                command=[str(PYTHON),'-u',str(BRIDGE)]
                if args.echo_check_after_start:command.append('--echo-check-after-start')
                child=subprocess.Popen(command,cwd=ROOT,
                    stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            pid_file.write_text(str(child.pid)+'\n')
            print('本机语音服务已启动，正在准备识别模型。')
        else:print('本机语音服务已运行。')
        sys.path.insert(0,str(ROOT/'device-access'))
        import cc8_install as access
        try:print(access.shell(access.target(),'input keyevent 224; am start -n com.happyhome.cc8assistant/.MainActivity').strip())
        except (OSError,RuntimeError,subprocess.SubprocessError):print('未发现可用 USB，后台正等待已配对的局域网连接；请在 CC8 打开大黄蜂。')
        print('请在 CC8 点击“开始语音”；首次授权后，安静等待 6 秒校准。')


if __name__=='__main__':main()
