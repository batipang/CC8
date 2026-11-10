"""Owner-Mac identity status/restore; no device-selected owner role or automatic retry."""

import argparse
import json
import os
from pathlib import Path
import socket
import stat
import sys

from announce import SOCKET
from protocol import decode_json_object
from identity_controller import revision


def request(payload, path=SOCKET, *, timeout=8):
    path = Path(path)
    parent, info = path.parent.lstat(), path.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o077
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise OSError('Unsafe owner identity socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise OSError('Owner identity socket changed')
        connection.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode())
        data = bytearray()
        while b'\n' not in data:
            chunk = connection.recv(min(1024, 4097 - len(data)))
            if not chunk:
                raise OSError('Identity result was not confirmed')
            data.extend(chunk)
            if len(data) > 4096:
                raise ValueError('Identity result exceeds limit')
    return decode_json_object(bytes(data), max_bytes=4096)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status', help='Read saved mode and current device application receipt')
    owner = commands.add_parser('owner', help='Explicitly restore owner mode from this Mac')
    owner.add_argument('--revision', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        payload = (
            {'operation': 'identity_status'}
            if args.action == 'status'
            else {'operation': 'identity_owner', 'revision': revision(args.revision)}
        )
        result = request(payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ok') is True else 1
    except (OSError, ValueError):
        print('身份结果未确认。请检查本机服务并查询状态；不要自动重试切换。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
