"""Compose identity persistence, route ACKs and private output around the real bridge."""

import re
from identity_controller import IdentityController, IdentityHooks
from composition import voice_busy
from route_output import same_route

# These packets carry lifecycle controls, not owner task/history data. They must
# remain deliverable while identity is unconfirmed so native output can stop.
CONTROL_EVENTS = frozenset(
    (
        'cancel',
        'ping',
        'capture_rejected',
        'identity_state',
        'identity_result',
        'identity_unavailable',
        'screen_wake',
    )
)
GUEST_EVENTS = CONTROL_EVENTS | frozenset(
    (
        'hello',
        'brain_provider',
        'status',
        'transcript',
        'dot_result',
        'text_begin',
        'text_done',
        'play_begin',
        'play_end',
        'caption_timing',
        'playback_pause',
        'playback_resume',
        'capture_started',
        'screen_sleep',
        'workspace',
        'workspace_result',
        'web_sources',
    )
)


class IdentityRuntime:
    def __init__(self, host, store):
        self.host = host
        self.peer_supported = False
        self.pending = True
        self.last_ready = None
        self.hello_token = None
        self.controller = IdentityController(
            store,
            IdentityHooks(
                lock=host.guard,
                busy=self.busy,
                connected=lambda: host.session.connection is not None,
                peer_supported=lambda: self.peer_supported,
                stop=host.stop,
                reset_contexts=host.reset_identity,
                event=host.event,
                route=self.route,
            ),
        )

    def route(self):
        return self.host.session.connection, self.host.session.bound_route_token

    def load(self):
        self.controller.load()
        self.host.reset_identity(self.controller.state.mode)

    def busy(self):
        host = self.host
        watch = host.chats.watch_ui.scheduler
        return bool(
            voice_busy(host)
            or host.session.text_generation is not None
            or host.chats.busy
            or host.workflows.active is not None
            and host.workflows.active.state not in ('completed', 'failed', 'cancelled')
            or host.workflows.narrating
            or host.reminders.job is not None
            or host.reminders.pending_finish is not None
            or watch is not None
            and (
                watch.pending is not None
                or watch.reading is not None
                or watch.worker is not None
                and watch.worker.is_alive()
            )
        )

    def dialogue_access(self):
        return (
            not self.pending
            and self.controller.device_applied
            and (self.controller.owner or self.controller.guest)
        )

    def owner_access(self):
        return self.dialogue_access() and self.controller.owner

    def transport_changed(self):
        with self.host.guard:
            self.pending = True
            self.peer_supported = False
            self.last_ready = None
            self.hello_token = None
            self.controller.application = None

    def ready(self, packet):
        with self.host.guard:
            if (
                self.last_ready is not None
                and type(packet.get('identity_version')) is int
                and same_route(self.last_ready[0], *self.route())
                and self.last_ready[1] == packet
                and self.dialogue_access()
            ):
                return
            self.pending = True
            self.peer_supported = (
                type(packet.get('identity_version')) is int and packet['identity_version'] == 1
            )
            self.last_ready = (self.route(), dict(packet))
            if not self.peer_supported:
                self.host.event('identity_unavailable', message='请使用支持身份隔离的新版助手')
                return
            try:
                if packet.get('identity_guest_requested') is True and self.controller.owner:
                    # A persisted restrictive native choice is never overridden
                    # by an owner hello. A busy rejection stays closed.
                    self.controller.select('guest', self.controller.state.revision)
                else:
                    self.controller.synchronize()
            except (ValueError, RuntimeError, OSError):
                self.host.event('identity_unavailable', message='身份切换尚未完成，请在 Mac 检查')

    def request(self, packet):
        with self.host.guard:
            arguments = packet.get('arguments')
            if (
                set(packet) == {'event', 'request_id', 'arguments'}
                and packet.get('event') == 'identity_request'
                and isinstance(packet.get('request_id'), str)
                and re.fullmatch('[a-f0-9]{32}', packet['request_id']) is not None
                and isinstance(arguments, dict)
                and set(arguments) == {'action', 'revision'}
                and arguments.get('action') == 'guest'
                and type(arguments.get('revision')) is int
                and arguments['revision'] == self.controller.state.revision
                and self.controller.loaded
                and self.controller.durable
                and self.peer_supported
                and self.route()[0] is not None
                and not self.busy()
            ):
                # Close private output before Stop can publish the old owner
                # workspace. Failed persistence stays closed, not auto-owner.
                self.pending = True
        self.controller.device_request(packet)

    def applied(self, packet):
        with self.host.guard:
            if not self.controller.applied(packet) or self.last_ready is None:
                return
            route, ready = self.last_ready
            ticket = self.controller.application
            if (
                ticket is None
                or not same_route(route, *self.route())
                or self.hello_token == ticket.token
            ):
                return
            self.hello_token = ticket.token
            self.pending = False
            # Never start capture here. The existing native start/rebind path
            # still owns the microphone, its token and its recording epoch.
            self.host.session.ready(ready)
            if self.owner_access():
                self.host.chats.publish(ok=True)

    def event_fields(self, name, fields):
        if not self.dialogue_access():
            if name not in CONTROL_EVENTS:
                return None
        elif not self.owner_access():
            if name not in GUEST_EVENTS:
                return None
            if name == 'web_sources' and fields.get('items') != []:
                return None
        return {
            **fields,
            'identity_mode': self.controller.state.mode,
            'identity_revision': self.controller.state.revision,
        }
