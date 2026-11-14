"""Bounded GPT-Live transport and capture-owned trial session."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass
import json
import queue
import re
import ssl
import threading
import time
import uuid

import numpy as np

MODEL = 'gpt-live-1'
ENDPOINT = 'wss://api.openai.com/v1/live/sessions'
INSTRUCTIONS = (
    '你是大黄蜂。默认说简洁自然的普通话，听到用户插话就让出话筒。'
    '问候、停止和唤醒词可简短回应。所有实质问题和操作都必须使用客户端委托，'
    '委托给后台 Dot，等待它返回实际结果；不可自行声称操作成功。'
    '“大黄蜂”或“Bumblebee”单独出现时，只简短回应。'
)
MAX_SESSION_SECONDS = 180
IDLE_SECONDS = 30
OUTPUT_GAP = 0.45


def connect(key):
    from websockets.asyncio.client import connect as open_websocket

    return open_websocket(
        ENDPOINT,
        # A fixed TCP destination also makes websockets reject cross-origin redirects.
        host='api.openai.com',
        additional_headers={'Authorization': 'Bearer ' + key},
        ssl=ssl.create_default_context(),
        proxy=None,
        open_timeout=5,
        close_timeout=1,
        max_size=256 * 1024,
        max_queue=8,
    )


def start_message(voice):
    return {
        'type': 'session.start',
        'event_id': uuid.uuid4().hex,
        'session': {
            'model': MODEL,
            'store': False,
            'instructions': INSTRUCTIONS,
            'audio': {'format': {'type': 'audio/pcm', 'rate': 16000}, 'output': {'voice': voice}},
            'delegation': {'type': 'client'},
        },
    }


def pcm16(samples):
    return np.rint(np.clip(samples, -1, 32767 / 32768) * 32768).astype('<i2').tobytes()


@dataclass(frozen=True)
class Fragment:
    text: str
    start: int
    end: int

    @classmethod
    def parse(cls, event):
        text, start, end = (event.get(key) for key in ('delta', 'start_ms', 'end_ms'))
        if (
            not isinstance(text, str)
            or not 0 < len(text) <= 512
            or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end <= (MAX_SESSION_SECONDS + 10) * 1000
        ):
            raise ValueError('Invalid Live transcript interval')
        if start == end:
            # Appended context has no corresponding microphone or playback audio.
            return None
        return cls(text, start, end)


class LivePlayback:
    """One inferred speech segment. The primary socket omits output PCM timestamps."""

    def __init__(self, generation, clock=time.monotonic):
        self.generation = generation
        self.clock = clock
        self.chunks = queue.Queue(maxsize=48)
        self.interrupted = threading.Event()
        self.closed = threading.Event()
        self.last_audio = clock()
        self.samples = 0
        self.fragments = []
        self.caption_lock = threading.Lock()
        self.publish = None

    def add_audio(self, data):
        if not 0 < len(data) <= 32000 or len(data) % 2:
            raise ValueError('Invalid Live PCM chunk')
        self.chunks.put_nowait(np.frombuffer(data, dtype='<i2'))
        self.samples += len(data) // 2
        if self.samples > 30 * 16000:
            raise ValueError('Live playback segment exceeded limit')
        self.last_audio = self.clock()

    def subscribe(self, publish):
        with self.caption_lock:
            self.publish = publish
            text, rows = self._caption()
        if publish and text and not self.interrupted.is_set():
            publish(text, rows)

    def _caption(self):
        if not self.fragments:
            return '', []
        origin = self.fragments[0].start
        text, rows, offset = [], [], 0
        for part in self.fragments:
            length = len(part.text.encode('utf-16-le')) // 2
            rows.append(
                [offset, offset + length, (part.start - origin) * 16, (part.end - origin) * 16]
            )
            text.append(part.text)
            offset += length
        return ''.join(text), rows

    def caption(self):
        with self.caption_lock:
            return self._caption()

    def add_caption(self, part):
        with self.caption_lock:
            if self.fragments and part.start < self.fragments[-1].end:
                return
            if (
                len(self.fragments) >= 128
                or sum(len(p.text) for p in self.fragments) + len(part.text) > 4096
            ):
                return
            self.fragments.append(part)
            text, rows = self._caption()
            publish = self.publish
        if publish and not self.interrupted.is_set():
            publish(text, rows)

    def stream(self):
        while not self.interrupted.is_set():
            try:
                yield self.chunks.get(timeout=0.05)
            except queue.Empty:
                if self.closed.is_set():
                    return


class LiveSession:
    """One primary WebSocket, owned by one local wake window and route."""

    def __init__(self, key, voice, callbacks, *, connector=connect, clock=time.monotonic):
        self.key, self.voice, self.callbacks = key, voice, callbacks
        self.connector, self.clock = connector, clock
        self.audio = queue.Queue(maxsize=100)
        self.results = queue.Queue(maxsize=8)
        self.initial_audio = deque()
        self.stop = threading.Event()
        self.started = threading.Event()
        self.input_overflow = threading.Event()
        self.finished = threading.Event()
        self.created = self.last_activity = clock()
        self.playback = None
        self.interrupt = threading.Event()
        self.interrupt_instruction = False
        self.transcripts = deque(maxlen=80)
        self.pending = {}
        self.interrupted_delegations = set()
        self.seen = set()
        self.input_offset = 0
        self.last_input_end = 0
        self.last_output_caption_end = 0
        self.early_captions = deque(maxlen=8)
        self.drop_output = False
        self.waiting_for_input = False
        self.last_output = 0.0

    def feed(self, pcm, voiced):
        if self.stop.is_set():
            return
        if voiced:
            self.last_activity = self.clock()
        try:
            self.audio.put_nowait(pcm)
        except queue.Full:
            self.input_overflow.set()
            self.stop.set()

    def interrupt_playback(self):
        self.interrupt.set()

    @staticmethod
    async def send(socket, message):
        async with asyncio.timeout(1):
            await socket.send(json.dumps(message, ensure_ascii=False))

    async def command(self, socket, kind, **fields):
        await self.send(socket, {'type': kind, 'event_id': uuid.uuid4().hex, **fields})

    @staticmethod
    async def receive(socket, timeout):
        async with asyncio.timeout(timeout):
            return json.loads(await socket.recv())

    def run(self):
        asyncio.run(self.exchange())

    async def exchange(self):
        failed = False
        try:
            async with self.connector(self.key) as socket:
                if not self.callbacks.current(self):
                    return
                await self.send(socket, start_message(self.voice))
                deadline = self.clock() + 5
                while not self.stop.is_set() and not self.started.is_set():
                    try:
                        event = await self.receive(socket, 0.05)
                    except TimeoutError:
                        if not self.callbacks.current(self):
                            return
                        if self.clock() >= deadline:
                            raise
                        continue
                    if (
                        event.get('type') != 'session.started'
                        or event.get('session', {}).get('model') != MODEL
                    ):
                        raise ValueError('Live startup was not confirmed')
                    self.started.set()
                while (
                    not self.stop.is_set()
                    and self.callbacks.current(self)
                    and self.clock() - self.created < MAX_SESSION_SECONDS
                ):
                    self.tick()
                    if self.interrupt_instruction and self.callbacks.current(self):
                        self.interrupt_instruction = False
                        await self.command(
                            socket,
                            'session.instructions.append',
                            delegation_id=None,
                            content='用户刚刚插话。停止上一段播报，先听完新的话，再处理新的请求。',
                        )
                    if (
                        self.clock() - self.last_activity >= IDLE_SECONDS
                        and not self.pending
                        and self.playback is None
                    ):
                        break
                    try:
                        while self.callbacks.current(self):
                            ident, content = self.results.get_nowait()
                            if ident in self.interrupted_delegations:
                                self.interrupted_delegations.discard(ident)
                                continue
                            await self.command(
                                socket,
                                'session.commentary.append',
                                delegation_id=ident,
                                content=content,
                            )
                            self.pending.pop(ident, None)
                    except queue.Empty:
                        pass
                    chunks = []
                    for _ in range(5):
                        try:
                            chunks.append(
                                self.initial_audio.popleft()
                                if self.initial_audio
                                else self.audio.get_nowait()
                            )
                        except queue.Empty:
                            break
                    if chunks and self.callbacks.current(self):
                        await self.command(
                            socket,
                            'session.input_audio.append',
                            audio=base64.b64encode(b''.join(chunks)).decode('ascii'),
                        )
                    try:
                        event = await self.receive(socket, 0.015)
                    except TimeoutError:
                        continue
                    if self.callbacks.current(self) and self.event(event):
                        return
                if self.started.is_set():
                    await self.command(socket, 'session.close')
                    deadline = self.clock() + 1
                    while self.clock() < deadline:
                        if (await self.receive(socket, max(0, deadline - self.clock()))).get(
                            'type'
                        ) == 'session.closed':
                            break
        except Exception:
            failed = bool(self.callbacks.current(self))
        finally:
            self.stop.set()
            if self.playback is not None:
                self.playback.interrupted.set()
            try:
                self.callbacks.closed(self, failed or self.input_overflow.is_set())
            finally:
                self.finished.set()

    def tick(self):
        now = self.clock()
        if self.interrupt.is_set():
            self.interrupt.clear()
            self.interrupt_instruction = True
            if self.playback is not None:
                self.playback.interrupted.set()
                self.playback = None
            self.early_captions.clear()
            # Dot can finish an already-started task and publish its receipt,
            # but its old narration must not interrupt the new utterance.
            self.interrupted_delegations.update(
                ident for ident, (_, _, dispatched) in self.pending.items() if dispatched
            )
            self.pending.clear()
            self.input_offset = max(self.input_offset, self.last_input_end)
            self.drop_output = True
            self.waiting_for_input = True
            self.last_output = now
        if self.playback is not None and now - self.playback.last_audio >= OUTPUT_GAP:
            self.playback.closed.set()
            self.playback = None
        if self.drop_output and not self.waiting_for_input and now - self.last_output >= OUTPUT_GAP:
            self.drop_output = False
        for ident, (offset, deadline, dispatched) in tuple(self.pending.items()):
            if not dispatched and now >= deadline:
                self.dispatch(ident, offset)

    def user_caption(self):
        utterance = []
        end = self.last_input_end
        for part in reversed(self.transcripts):
            if end - part.end > 700:
                break
            utterance.append(part.text)
            end = part.start
        return ''.join(reversed(utterance))[-1000:]

    def dispatch(self, ident, offset):
        selected = [
            part
            for part in self.transcripts
            if self.input_offset <= part.start and part.end <= offset + 150 and part.start < offset
        ]
        self.pending[ident] = (offset, 0, True)
        # Retire this part of the timeline even when the transcript was
        # incomplete. A later delegation must never act on a delayed old turn.
        self.input_offset = max(self.input_offset, offset, *(part.end for part in selected))
        # A delegation identifies the current request, never a replay of earlier
        # unrelated speech. Keep only the latest contiguous spoken group.
        for index in range(len(selected) - 1, 0, -1):
            if selected[index].start - selected[index - 1].end > 1000:
                selected = selected[index:]
                break
        text = ''.join(part.text for part in selected).strip()[:1500]
        if not text or not self.callbacks.delegate(self, ident, text):
            self.result(ident, '这次没有确认到新的请求，请用户把要交给 Dot 的任务再说一遍。')

    def result(self, ident, text):
        # Dot may complete after cancellation. The session is the only owner
        # allowed to consume this queue and speak a result.
        if self.stop.is_set():
            return
        try:
            self.results.put_nowait((ident, text[:200] or 'Dot 没有返回可播报的结果。'))
        except queue.Full:
            self.stop.set()

    def event(self, event):
        kind = event.get('type')
        if kind == 'session.closed':
            return True
        if kind == 'error':
            raise ValueError('GPT-Live reported a session error')
        if kind == 'session.input_transcript.delta':
            part = Fragment.parse(event)
            if part is None or part.start < self.last_input_end:
                return False
            self.last_input_end = part.end
            self.transcripts.append(part)
            if self.callbacks.transcript(self):
                self.waiting_for_input = False
        elif kind == 'session.delegation.created':
            details = event.get('delegation', {})
            ident, offset = details.get('id'), event.get('offset_ms')
            if (
                details.get('type') == 'delegation'
                and details.get('target') == 'client'
                and isinstance(ident, str)
                and re.fullmatch(r'[\w-]{1,128}', ident, re.ASCII)
                and ident not in self.seen
                and len(self.seen) < 64
                and type(offset) is int
                and self.input_offset <= offset <= (MAX_SESSION_SECONDS + 10) * 1000
            ):
                self.seen.add(ident)
                self.pending[ident] = (offset, self.clock() + 0.35, False)
        elif kind == 'session.output_audio.delta':
            self.last_output = self.last_activity = self.clock()
            if self.drop_output:
                return False
            data = base64.b64decode(event['delta'], validate=True)
            if self.playback is None:
                self.playback = LivePlayback(self.callbacks.generation(), self.clock)
                while self.early_captions:
                    self.playback.add_caption(self.early_captions.popleft())
                self.playback.add_audio(data)
                if not self.callbacks.play(self, self.playback):
                    self.playback.interrupted.set()
                    self.playback = None
                    self.drop_output = True
            else:
                self.playback.add_audio(data)
        elif kind == 'session.output_transcript.delta':
            part = Fragment.parse(event)
            if part is not None and part.start >= self.last_output_caption_end:
                self.last_output_caption_end = part.end
                if not self.drop_output:
                    if self.playback is None:
                        self.early_captions.append(part)
                    else:
                        self.playback.add_caption(part)
        return False
