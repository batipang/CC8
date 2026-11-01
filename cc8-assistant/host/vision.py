"""One explicitly requested camera frame, bounded in RAM and tied to a turn."""
from dataclasses import dataclass, field
import base64
import hashlib
import re
import secrets
import threading
import time

MAX_IMAGE=384*1024
CHUNK=12288

class VisionError(RuntimeError):pass


def visual_request(text):
    """Explicit present-camera requests; descriptions/quotes of commands aren't consent."""
    value=re.sub(r'\s+','',text).lower()
    value=re.sub(r'^(?:大黄蜂|大黃蜂|大黄峰|bumble-?bee)[，,。:：！!]*','',value)
    value=re.sub(r'^(?:(?:请你|麻烦你|你能不能|能不能|你可以|你能|帮我|给我|替我|麻烦|能否|可以|重新|请|再|来|你)[，,：:]*)+','',value)
    if re.search(r'不要|别(?:开|拍|看|用)|不用|不需要|不能|不许|别再',value):return False
    if not re.search(r'摄像头|相机|镜头|手里|面前|纸|上面|周围',value) and re.search(r'项目|代码|网页|链接|新闻|天气|文件|电影|视频|仓库|文章',value):return False
    if re.match(r'^(?:打开|调用|使用|用|拍一张|拍张).{0,8}(?:摄像头|相机|照片)',value):return True
    if re.match(r'^(?:看看|看一看|看一下|看下|观察|识别|辨认|分析|读一下|读出|看).{0,12}(?:我手里|我面前|我穿|镜头|眼前|周围|这(?:个|是|里|上面|张|些|件)|摄像头|相机|画面)',value):return True
    return bool(re.fullmatch(r'(?:看看|看一看|看看我|这是什么[？?]?|(?:能)?看到(?:了)?什么[？?]?)',value))


def jpeg_dimensions(data):
    if not isinstance(data,bytes) or not 32<=len(data)<=MAX_IMAGE or data[:2]!=b'\xff\xd8' or data[-2:]!=b'\xff\xd9':
        raise VisionError('invalid_jpeg')
    at=2
    while at+4<=len(data):
        if data[at]!=255:raise VisionError('invalid_jpeg_header')
        while at<len(data) and data[at]==255:at+=1
        if at>=len(data):break
        marker=data[at];at+=1
        if marker in (0xd9,0xda):break
        size=int.from_bytes(data[at:at+2],'big')
        if size<2 or at+size>len(data):raise VisionError('invalid_jpeg_segment')
        if marker in (0xc0,0xc2):
            if size<8 or data[at+2]!=8:raise VisionError('unsupported_jpeg')
            height=int.from_bytes(data[at+3:at+5],'big');width=int.from_bytes(data[at+5:at+7],'big')
            if width<1 or height<1 or max(width,height)>640 or width*height>640*480:raise VisionError('image_dimensions_exceeded')
            return width,height
        at+=size
    raise VisionError('missing_jpeg_dimensions')


@dataclass
class Capture:
    generation:int
    ident:str=field(default_factory=lambda:secrets.token_hex(16))
    expires:float=field(default_factory=lambda:time.monotonic()+12)
    done:threading.Event=field(default_factory=threading.Event)
    data:bytearray=field(default_factory=bytearray)
    image:bytes|None=None
    size:int=0
    sequence:int=0
    width:int=0
    height:int=0
    digest:str=''
    error:str=''


class CameraInbox:
    def __init__(self):self.lock=threading.Lock();self.pending=None
    def begin(self,generation):
        with self.lock:
            self._cancel();self.pending=Capture(generation);return self.pending
    def _cancel(self):
        if self.pending:
            self.pending.error='cancelled';self.pending.data.clear();self.pending.image=None;self.pending.done.set()
        self.pending=None
    def cancel(self):
        with self.lock:self._cancel()
    def feed(self,value):
        with self.lock:
            c=self.pending
            if not c or c.done.is_set() or value.get('request_id')!=c.ident or value.get('vision_generation')!=c.generation:return False
            try:
                if time.monotonic()>c.expires:raise VisionError('capture_timeout')
                event=value.get('event')
                if event=='camera_error':
                    reason=value.get('error')
                    raise VisionError(reason if isinstance(reason,str) and re.fullmatch('[a-z0-9_]{1,60}',reason) else 'camera_failed')
                if event=='camera_image_begin':
                    if c.size:raise VisionError('duplicate_image_begin')
                    size=value.get('bytes');width=value.get('width');height=value.get('height');digest=value.get('sha256')
                    if type(size) is not int or not 32<=size<=MAX_IMAGE:raise VisionError('image_size_exceeded')
                    if type(width) is not int or type(height) is not int or min(width,height)<1 or max(width,height)>640 or width*height>640*480:raise VisionError('image_dimensions_exceeded')
                    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):raise VisionError('invalid_image_hash')
                    c.size=size;c.width=width;c.height=height;c.digest=digest
                elif event=='camera_image_chunk':
                    raw=value.get('data')
                    if not c.size or type(value.get('index')) is not int or value['index']!=c.sequence or not isinstance(raw,str) or len(raw)>CHUNK*4//3:raise VisionError('invalid_image_chunk')
                    try:chunk=base64.b64decode(raw,validate=True)
                    except (ValueError,TypeError):raise VisionError('invalid_base64') from None
                    if not chunk or len(c.data)+len(chunk)>c.size:raise VisionError('image_size_mismatch')
                    c.data.extend(chunk);c.sequence+=1
                elif event=='camera_image_end':
                    if not c.size or len(c.data)!=c.size:raise VisionError('incomplete_image')
                    image=bytes(c.data)
                    if hashlib.sha256(image).hexdigest()!=c.digest:raise VisionError('image_hash_mismatch')
                    if jpeg_dimensions(image)!=(c.width,c.height):raise VisionError('image_dimensions_mismatch')
                    c.image=image;c.data.clear();c.done.set()
                else:return False
            except VisionError as error:
                c.error=str(error);c.image=None;c.data.clear();c.done.set()
            return True
    def wait(self,capture,is_current):
        while not capture.done.wait(.05):
            if not is_current() or time.monotonic()>capture.expires:
                with self.lock:
                    if self.pending is capture:self._cancel()
                raise VisionError('capture_cancelled_or_timeout')
        if not is_current() or capture.error or capture.image is None:raise VisionError(capture.error or 'stale_capture')
        image=capture.image
        with self.lock:
            capture.image=None
            if self.pending is capture:self.pending=None
        return image
