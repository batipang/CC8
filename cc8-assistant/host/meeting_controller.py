"""Explicit foreground meeting offers and exclusive capture on one authenticated route."""

from collections import deque
from dataclasses import dataclass
import threading
import time
from typing import Callable, ContextManager
import uuid

from meeting_asr import MeetingAsr
from meeting_session import CaptureOwner, MeetingSession, token
from meeting_view import checked, index_page, status, text_page
from meeting_protocol import validate_request
from meeting_worker import MeetingWorker
from protocol import MEETING


class Rejected(ValueError):
    """Only fixed local guidance may be sent to the device."""


def same_owner(first, second):
    return (
        first is not None
        and second is not None
        and first[0] is second[0]
        and first[1:] == second[1:]
    )


@dataclass(frozen=True)
class MeetingHooks:
    lock: ContextManager
    owner: Callable[[], tuple | None]  # authenticated connection, route token, generation
    idle: Callable[[], bool]  # other pipelines only; meeting reservation is separate
    event: Callable
    touch: Callable[[], None]
    claim: Callable[[], int]


class MeetingController:
    """Use the shared reentrant session guard, as other device controllers do."""

    def __init__(self, hooks, *, client_factory=MeetingAsr, clock=time.monotonic):
        self.hooks, self.clock = hooks, clock
        self.model = None
        self.peer_owner = self.run_owner = self.offer = None
        self.capture_request = ''
        self.route_serial = 0
        self.seen = deque(maxlen=64)
        self.changed = threading.Event()
        self.worker = MeetingWorker(hooks.lock, client_factory, changed=self.changed.set)
        # No recording, thread, request, credential read or persistence here.

    def peer(self, version):
        with self.hooks.lock:
            owner = self.hooks.owner()
            next_owner = owner[:2] if owner and type(version) is int and version == 1 else None
            if not same_owner(self.peer_owner, next_owner):
                self.invalidate('transport_switch')
            self.peer_owner = next_owner
            self.route_serial += 1

    def available(self):
        owner = self.hooks.owner()
        return (
            not self.worker.closed.is_set()
            and owner is not None
            and token(owner[1])
            and self.peer_owner is not None
            and same_owner(owner[:2], self.peer_owner)
        )

    def reserved(self):
        with self.hooks.lock:
            return (
                self.model is not None
                and self.model.state in ('capturing', 'paused')
                or self.worker.busy()
            )

    def invalidate(self, reason='cancelled'):
        with self.hooks.lock:
            self.offer = None
            self.run_owner = None
        mapped = (
            'route_lost'
            if reason in ('transport_switch', 'transport_disconnect')
            else 'background'
            if reason == 'session_stop'
            else 'cancelled'
        )
        self.worker.interrupt(mapped)

    def view(self):
        with self.hooks.lock:
            return status(self.model)

    def _prepare(self, request, owner):
        action = request['action']
        if not self.hooks.idle() or self.worker.busy():
            raise Rejected('请先暂停日常语音，等待当前任务、播报与转写结束。')
        if action == 'prepare':
            if self.model is not None and self.model.state != 'deleted':
                raise Rejected('已有会议，请先查看、结束并明确删除，再开始新会议。')
        else:
            checked(self.model, request['id'], request['revision'])
            if self.model.state != 'paused':
                raise Rejected('只有已暂停的会议可以继续录音。')
        self.offer = dict(
            id=uuid.uuid4().hex,
            owner=owner,
            expires=self.clock() + 30,
            action=action,
            revision=self.model.revision if self.model else None,
        )
        return dict(
            offer=dict(
                id=self.offer['id'],
                expires_in_seconds=30,
                mode='resume' if action == 'prepare_resume' else 'start',
                audio_destination='MiniMax',
                raw_audio_supported=False,
            )
        )

    def _start(self, request, owner):
        offer = self.offer
        if offer is None or request['offer_id'] != offer['id']:
            raise Rejected('录音确认已失效，请重新核对并确认，不会自动开始。')
        self.offer = None  # Consume this offer, never revoke an unrelated newer one.
        if (
            not same_owner(owner, offer['owner'])
            or self.clock() >= offer['expires']
            or not self.hooks.idle()
            or self.worker.busy()
            or offer['action'] != ('prepare' if request['action'] == 'start' else 'prepare_resume')
        ):
            raise Rejected('录音确认已失效，请重新核对并确认，不会自动开始。')
        if request['action'] == 'start':
            if request['save_audio']:
                raise Rejected('当前开发版本尚未支持保存原音；未开始录音。')
            if self.model is not None and self.model.state != 'deleted':
                raise Rejected('已有会议，未开始新的录音。')
        else:
            checked(self.model, request['id'], offer['revision'])
            if self.model.state != 'paused':
                raise Rejected('会议状态已改变，请重新查看。')
        generation = self.hooks.claim()
        current = self.hooks.owner()
        if (
            not same_owner(owner[:2], current[:2] if current else None)
            or current[2] != generation
            or generation <= owner[2]
        ):
            raise Rejected('会议连接或任务已变化，未开始录音。')
        owner = current
        if request['action'] == 'start':
            model = MeetingSession(uuid.uuid4().hex)
            self.worker.bind(model)
            self.model = model
        capture = CaptureOwner(self.route_serial, owner[2], uuid.uuid4().hex)
        self.model.start(capture)
        self.run_owner = owner
        self.capture_request = request['request_id']
        self.worker.start()
        self.hooks.touch()
        return dict(
            capture=dict(
                id=self.model.ident,
                nonce=capture.nonce,
                generation=capture.generation,
                sample_rate=16000,
                packet_type=MEETING,
                maximum_seconds=3600,
            )
        )

    def _action(self, request, owner):
        action = request['action']
        if action == 'status':
            return {}
        if action in ('prepare', 'prepare_resume'):
            return self._prepare(request, owner)
        if action in ('start', 'resume'):
            return self._start(request, owner)
        if action == 'abort_start':
            if request['start_request_id'] != self.capture_request or not same_owner(
                self.run_owner, owner
            ):
                raise Rejected('该启动请求已失效，不会影响新的录音。')
            if self.model is None or self.model.state != 'capturing':
                return {}
            self.offer = None
            self.worker.interrupt('cancelled')
            return {}
        if self.model is None or request['id'] != self.model.ident or self.model.state == 'deleted':
            raise Rejected('会议已不可用，请刷新当前状态。')
        if action == 'capture_failed':
            if (
                self.model.owner is None
                or self.model.owner.nonce != request['nonce']
                or not same_owner(self.run_owner, owner)
            ):
                raise Rejected('该录音已结束，旧失败通知不能影响新的录音。')
            self.offer = None
            self.worker.interrupt(request['reason'])
            return {}
        if action in ('pause', 'end'):
            self.offer = None
            # Safety operations remain usable while ASR updates the revision.
            if action == 'pause' and self.model.state == 'paused':
                return {}
            if action == 'end' and self.model.state == 'ended':
                return {}
            chunks = self.model.pause() if action == 'pause' else self.model.end()
            if chunks:
                self.worker.submit(chunks)
            return {}
        checked(self.model, request['id'], request['revision'])
        if action == 'index':
            return dict(
                page=index_page(self.model, request['id'], request['revision'], request['offset'])
            )
        if action == 'text':
            return dict(
                page=text_page(
                    self.model,
                    request['id'],
                    request['revision'],
                    request['row_id'],
                    request['offset'],
                    original=request['original'],
                )
            )
        if action == 'edit':
            self.offer = None
            self.model.edit(request['row_id'], request['revision'], request['text'])
            return {}
        if action == 'delete':
            if self.model.state == 'capturing' or self.worker.busy():
                raise Rejected('请先暂停或结束会议，并等待转写停止，再明确删除。')
            self.offer = self.run_owner = None
            self.model.delete()
            return {}
        raise Rejected('该会议功能仍在开发，未执行请求。')

    def request(self, request):
        validate_request(request)
        with self.hooks.lock:
            owner = self.hooks.owner()
            if owner is None or not self.available() or not same_owner(owner, self.hooks.owner()):
                return
            result = dict(request_id=request['request_id'], action=request['action'], ok=False)
            try:
                key = (owner[0], owner[1], request['request_id'])
                if key in self.seen:
                    raise Rejected('该请求已处理，请刷新状态，不会重复执行。')
                self.seen.append(key)
                result.update(self._action(request, owner), ok=True)
            except Rejected as error:
                result['message'] = str(error)
            except (OSError, ValueError, RuntimeError):
                result['message'] = '会议状态未确认，请刷新查看；不会自动重试或开始录音。'
            target = self.hooks.owner() if request['action'] in ('start', 'resume') else owner
            if same_owner(owner[:2], target[:2] if target else None) and same_owner(
                target, self.hooks.owner()
            ):
                self.hooks.event(
                    'meeting_result', generation=target[2], state=self.view(), **result
                )

    def on_packet(self, packet):
        with self.hooks.lock:
            if (
                packet.kind != MEETING
                or not self.available()
                or not same_owner(self.run_owner, self.hooks.owner())
                or self.model is None
                or self.model.state != 'capturing'
                or self.model.owner is None
                or packet.generation != self.model.owner.generation
                or packet.data[:16].hex() != self.model.owner.nonce
            ):
                return
            _, chunks = self.model.receive(
                self.model.owner, packet.sequence, packet.frame, packet.data[16:]
            )
            if chunks:
                self.worker.submit(chunks)
            if self.model.state != 'capturing':
                self.changed.set()
                if self.model.reason and not any(
                    row['state'] == 'pending' for row in self.model.rows
                ):
                    self.worker.interrupt(self.model.reason)
            self.hooks.touch()

    def tick(self):
        if not self.changed.is_set():
            return
        self.changed.clear()
        with self.hooks.lock:
            if self.available():
                self.hooks.event('meeting_update', state=self.view())

    def close(self):
        self.worker.close()
