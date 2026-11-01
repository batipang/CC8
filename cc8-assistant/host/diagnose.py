"""Export private, content-free diagnostics without opening device audio/video."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import stat

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT/'device-access/private/voice-runtime'


def query(path=PRIVATE/'announce.sock', operation='diagnostics'):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError('Unsafe diagnostics socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5); conn.connect(str(path))
        conn.sendall((json.dumps({'operation': operation})+'\n').encode())
        data = bytearray()
        while b'\n' not in data:
            block = conn.recv(4096)
            if not block: raise OSError('Incomplete diagnostics response')
            data.extend(block)
            if len(data) > 1024*1024: raise ValueError('Oversized diagnostics response')
    result = json.loads(data)
    if not isinstance(result,dict) or result.get('ok') is not True: raise ValueError('Diagnostics unavailable')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--health', action='store_true'); args = parser.parse_args()
    try: result = query(operation='health' if args.health else 'diagnostics')
    except (OSError, ValueError): raise SystemExit('后台不可用，未打开设备、麦克风或摄像头。')
    if args.health: print(json.dumps(result, ensure_ascii=False)); return
    folder = ROOT/'device-access/private/diagnostics'; folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    name = 'diagnostics-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json'
    path = folder/name
    with open(path, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(path)


if __name__ == '__main__': main()
