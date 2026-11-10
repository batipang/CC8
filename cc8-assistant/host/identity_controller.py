"""Actor-specific mode transitions; only owner-only desktop IPC may restore owner mode."""

from dataclasses import dataclass
import re
import threading
import uuid
from typing import Callable, ContextManager, Protocol

from identity_mode import (
    IdentityModeStore,
    ModeState,
    ModeCommitUncertain,
    ModeConflict,
    MAX_REVISION,
)
from protocol import JsonObject, JsonValue
from route_output import RouteOwner, same_route


class IdentityEvent(Protocol):
    def __call__(self, event: str, **fields: JsonValue) -> None: ...


@dataclass(frozen=True)
class IdentityHooks:
    lock: ContextManager[object]
    busy: Callable[[], bool]
    connected: Callable[[], bool]
    peer_supported: Callable[[], bool]
    stop: Callable[[], None]
    reset_contexts: Callable[[str], None]
    event: IdentityEvent
    route: Callable[[], RouteOwner] = lambda: (None, None)


@dataclass
class ModeApplication:
    state: ModeState
    route: RouteOwner
    token: str
    done: threading.Event


def revision(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise ValueError('Invalid identity revision')
    return value


class IdentityController:
    def __init__(self, store: IdentityModeStore, hooks: IdentityHooks) -> None:
        self.store = store
        self.hooks = hooks
        self.state = ModeState()
        self.loaded = False
        self.durable = False
        self.application: ModeApplication | None = None

    def load(self) -> None:
        # Explicit boot phase, before any capture, model preparation or schedulers.
        with self.hooks.lock:
            self.state = self.store.snapshot()
            self.loaded = self.durable = True

    @property
    def owner(self) -> bool:
        return self.loaded and self.durable and self.state.mode == 'owner'

    @property
    def guest(self) -> bool:
        return self.loaded and self.durable and self.state.mode == 'guest'

    def public(self) -> JsonObject:
        return {
            **self.state.public(),
            'ready': self.loaded and self.durable,
            'owner_restore': 'desktop_only',
            'device_applied': self.device_applied,
        }

    @property
    def device_applied(self) -> bool:
        ticket = self.application
        owner = self.hooks.route()
        return bool(
            self.loaded
            and self.durable
            and self.hooks.connected()
            and self.hooks.peer_supported()
            and ticket is not None
            and ticket.state == self.state
            and ticket.done.is_set()
            and owner[0] is not None
            and same_route(ticket.route, *owner)
        )

    def synchronize(self) -> ModeApplication:
        """Issue a fresh route-bound receipt challenge, never a mode selection."""
        with self.hooks.lock:
            route = self.hooks.route()
            if (
                not self.loaded
                or not self.durable
                or not self.hooks.connected()
                or not self.hooks.peer_supported()
                or route[0] is None
            ):
                raise RuntimeError('设备身份状态尚未确认')
            ticket = ModeApplication(self.state, route, uuid.uuid4().hex, threading.Event())
            self.application = ticket
            self.hooks.event(
                'identity_state',
                token=ticket.token,
                # Only owner-only IPC can persist a non-default owner revision.
                # Reconnecting can reissue that same grant without selecting
                # owner again or replaying an unconfirmed mode change.
                authority=(
                    'owner_desktop'
                    if self.state.mode == 'owner' and self.state.revision > 0
                    else 'reconcile'
                ),
                state=self.public(),
            )
            return ticket

    def applied(self, packet: JsonObject) -> bool:
        # A receipt acknowledges this exact state on this exact route. It can
        # never select a role or grant owner access by carrying an owner string.
        with self.hooks.lock:
            if (
                set(packet) != {'event', 'token', 'mode', 'revision'}
                or packet.get('event') != 'identity_applied'
                or type(packet.get('revision')) is not int
            ):
                return False
            ticket = self.application
            route = self.hooks.route()
            if (
                not self.loaded
                or not self.durable
                or not self.hooks.connected()
                or not self.hooks.peer_supported()
                or ticket is None
                or packet.get('token') != ticket.token
                or packet.get('mode') != self.state.mode
                or packet.get('revision') != self.state.revision
                or ticket.state != self.state
                or route[0] is None
                or not same_route(ticket.route, *route)
            ):
                return False
            ticket.done.set()
            return True

    def select(self, mode: str, expected: object) -> JsonObject:
        # This is an internal transition, never exposed as a device-selected role.
        with self.hooks.lock:
            if mode not in ('owner', 'guest'):
                raise ValueError('Invalid identity mode')
            if not self.loaded or not self.durable:
                raise RuntimeError('身份设置尚未确认，请在 Mac 检查')
            if revision(expected) != self.state.revision:
                raise ModeConflict('模式已改变，请刷新后选择')
            if not self.hooks.connected() or not self.hooks.peer_supported():
                raise RuntimeError('请连接支持访客隔离的新版助手')
            if self.hooks.busy():
                raise RuntimeError('当前请求尚未结束，请完成或明确停止后再切换')
            # Switching is explicit and idle-only. It does not auto-start capture.
            self.hooks.stop()
            self.application = None
            try:
                updated = self.store.change(self.state, mode)
            except ModeCommitUncertain as uncertain:
                self.state = uncertain.state
                self.durable = False
                self.hooks.reset_contexts('blocked')
                raise RuntimeError('模式保护已调整，但保存未确认，请在 Mac 检查') from None
            self.state = updated
            # A committed role is not usable until every in-memory owner has
            # invalidated its previous context. A reset failure must not grant
            # owner access merely because the durable state already says owner.
            self.durable = False
            self.hooks.reset_contexts(mode)
            self.durable = True
            self.synchronize()
            return self.public()

    def device_request(self, packet: JsonObject) -> None:
        ident = packet.get('request_id')
        if not isinstance(ident, str) or re.fullmatch('[a-f0-9]{32}', ident) is None:
            return
        try:
            if (
                set(packet) != {'event', 'request_id', 'arguments'}
                or packet.get('event') != 'identity_request'
            ):
                raise ValueError('无效模式请求')
            arguments = packet.get('arguments')
            if not isinstance(arguments, dict) or set(arguments) - {'action', 'revision'}:
                raise ValueError('无效模式请求')
            action = arguments.get('action')
            if action == 'status' and set(arguments) == {'action'}:
                result = self.public()
            elif action == 'guest' and set(arguments) == {'action', 'revision'}:
                result = self.select('guest', arguments['revision'])
            else:
                # No device or speech command can ask this controller to become owner.
                raise ValueError('切回主人模式需要从 Mac 发起')
            self.hooks.event(
                'identity_result',
                request_id=ident,
                ok=True,
                state=result,
                message=(
                    '模式未就绪，请在 Mac 检查'
                    if not self.loaded or not self.durable
                    else '访客模式仅支持独立问答'
                    if self.guest
                    else '主人模式'
                ),
            )
        except (ValueError, RuntimeError, OSError):
            # Do not expose private filesystem errors, paths or unfinished task data.
            self.hooks.event(
                'identity_result',
                request_id=ident,
                ok=False,
                state=self.public(),
                message='模式未完成切换，请等待当前请求结束并在 Mac 检查',
            )

    def owner_from_desktop(self, arguments: object, *, timeout: float = 5) -> JsonObject:
        # Only the owner-UID Unix-socket dispatcher may call this method.
        if not isinstance(arguments, dict) or set(arguments) != {'revision'}:
            raise ValueError('Owner mode requires an exact revision')
        if type(timeout) not in (int, float) or not 0 <= timeout <= 5:
            raise ValueError('Invalid identity acknowledgement timeout')
        with self.hooks.lock:
            self.select('owner', arguments['revision'])
            ticket = self.application
        # Do not hold the session guard while the device receive loop delivers
        # its ACK. A timeout is an unconfirmed outcome, never an action retry.
        if ticket is not None:
            ticket.done.wait(timeout)
        with self.hooks.lock:
            confirmed = self.application is ticket and self.device_applied
            return {
                'ok': confirmed,
                'state': self.public(),
                'message': '主人模式已在设备确认'
                if confirmed
                else '模式已保存，但设备应用未确认；请先查询状态，不要重复切换',
            }
