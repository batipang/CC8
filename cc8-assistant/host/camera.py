"""Request one visible CC8 snapshot and save it privately for inline chat display."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import base64
import hashlib
import json
import os
import secrets
import socket
import stat
import sys

from announce import SOCKET
from vision import MAX_IMAGE,jpeg_dimensions

PHOTOS=Path(__file__).resolve().parents[2]/'device-access/private/photos'
MAX_RESPONSE=MAX_IMAGE*4//3+4096


def receive_photo(path=SOCKET):
    ident=secrets.token_hex(16)
    info=path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
        raise ValueError('Unsafe camera service socket')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(30);connection.connect(str(path))
        connection.sendall((json.dumps({'operation':'camera_snapshot','request_id':ident})+'\n').encode())
        data=bytearray()
        while b'\n' not in data:
            chunk=connection.recv(min(65536,MAX_RESPONSE+1-len(data)))
            if not chunk:raise ValueError('Camera response ended early')
            data.extend(chunk)
            if len(data)>MAX_RESPONSE:raise ValueError('Camera response exceeds size limit')
    value=json.loads(data)
    if not isinstance(value,dict):raise ValueError('Invalid camera response')
    if value.get('status')!='completed':return value,None
    if value.get('request_id')!=ident or value.get('media_type')!='image/jpeg':raise ValueError('Camera response does not match request')
    raw=value.get('image_base64')
    if not isinstance(raw,str) or len(raw)>MAX_IMAGE*4//3:raise ValueError('Invalid camera image size')
    image=base64.b64decode(raw,validate=True);width,height=jpeg_dimensions(image)
    if len(image)!=value.get('bytes') or hashlib.sha256(image).hexdigest()!=value.get('sha256') or (width,height)!=(value.get('width'),value.get('height')):
        raise ValueError('Camera image verification failed')
    return {k:value[k] for k in ('status','request_id','media_type','width','height','bytes','sha256')},image


def save_photo(image,folder=PHOTOS):
    jpeg_dimensions(image)
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('Photo directory must be owner-private and not a symlink')
    stamp=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    target=folder/('cc8-'+stamp+'-'+secrets.token_hex(8)+'.jpg')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as output:output.write(image)
    except BaseException:
        target.unlink(missing_ok=True);raise
    return target.resolve()


def main():
    # 桌面显式拍照入口；校验回执后仅把本次新图片保存到本机私有目录。
    try:
        result,image=receive_photo()
        if image is None:
            print(json.dumps({k:v for k,v in result.items() if k in ('status','reason')},ensure_ascii=False));return 1
        target=save_photo(image);result['path']=str(target)
        print(json.dumps(result,ensure_ascii=False));return 0
    except (OSError,ValueError):
        print(json.dumps({'status':'unavailable','reason':'No verified photo was saved; keep CC8 foreground and connected, then explicitly request another capture.'}));return 1


if __name__=='__main__':sys.exit(main())
