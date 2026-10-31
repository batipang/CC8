"""Explicit local text-to-CC8 announcements; never starts microphone capture."""
from dataclasses import dataclass,field
from pathlib import Path
import argparse
import json
import os
import socket
import stat
import sys
import threading

SOCKET=Path(__file__).resolve().parents[2]/'device-access/private/voice-runtime/announce.sock'


@dataclass
class Announcement:
    generation:int
    text:str
    done:threading.Event=field(default_factory=threading.Event)
    result:str='interrupted'


def validate_text(text):
    if not isinstance(text,str) or not text.strip() or len(text)>500 or any(ord(c)<32 and c not in '\n\t' for c in text):
        raise ValueError('Text must contain 1–500 characters')
    return text.strip()


class AnnouncementServer:
    def __init__(self,bridge,path=SOCKET):self.bridge=bridge;self.path=path;self.stop=threading.Event();self.socket=None;self.slots=threading.BoundedSemaphore(4)
    def start(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.exists():
            info=self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():raise RuntimeError('Unsafe announcement socket path')
            self.path.unlink()
        self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.socket.bind(str(self.path));os.chmod(self.path,0o600)
        self.socket.listen(2);self.socket.settimeout(.5)
        threading.Thread(target=self.run,daemon=True).start()
    def run(self):
        while not self.stop.is_set():
            try:connection,_=self.socket.accept()
            except socket.timeout:continue
            except OSError:break
            if not self.slots.acquire(blocking=False):
                connection.close();continue
            threading.Thread(target=self.handle,args=(connection,),daemon=True).start()
    def handle(self,connection):
        try:
            with connection:
                try:
                    connection.settimeout(5);data=b''
                    while b'\n' not in data and len(data)<=4096:
                        chunk=connection.recv(1024)
                        if not chunk:break
                        data+=chunk
                    if len(data)>4096:raise ValueError('Oversized request')
                    request=json.loads(data)
                    if request.get('operation')=='transport_status':
                        result={'ok':True,**self.bridge.transport_hub.diagnostics()}
                    elif request.get('operation')=='transport_test':
                        result=self.bridge.transport_hub.suppress_usb_for_test(request.get('seconds',15))
                    elif request.get('operation')=='device_control':
                        from voice_tools import VoiceTools
                        result=VoiceTools(self.bridge.control_device).call('cc8_control',request.get('arguments'))
                    else:
                        text=validate_text(request.get('text'));job=self.bridge.announce(text)
                        finished=job.done.wait(120)
                        if not finished and self.bridge.generation==job.generation:self.bridge.cancel('announcement_timeout')
                        result={'status':job.result if finished else 'timeout','characters':len(text)}
                except (ValueError,TypeError,AttributeError):result={'status':'invalid_request'}
                except RuntimeError:result={'status':'unavailable','reason':'Keep the assistant foreground and an authenticated USB or Wi-Fi connection ready; wait until calibration ends.'}
                except OSError:return
                try:connection.sendall((json.dumps(result)+'\n').encode())
                except OSError:pass
        finally:self.slots.release()

    def close(self):
        self.stop.set()
        if self.socket:self.socket.close()
        try:self.path.unlink()
        except FileNotFoundError:pass


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--text');parser.add_argument('--file',type=Path);args=parser.parse_args()
    if args.text is not None and args.file:parser.error('Use --text, --file, or stdin')
    text=validate_text(args.text if args.text is not None else args.file.read_text() if args.file else sys.stdin.read(2000))
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(125);connection.connect(str(SOCKET));connection.sendall((json.dumps({'text':text},ensure_ascii=False)+'\n').encode())
            data=b''
            while b'\n' not in data and len(data)<4096:
                chunk=connection.recv(1024)
                if not chunk:break
                data+=chunk
        result=json.loads(data);print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('status')=='completed' else 1
    except (OSError,ValueError):print('Announcement service unavailable; start the CC8 voice service first.',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
