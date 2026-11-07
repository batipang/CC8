"""Own bounded device-control request IDs, waiters and filtered acknowledgements."""

from __future__ import annotations
from dataclasses import dataclass
import queue
from typing import Callable, ContextManager
from session_ports import EventSink
from connection_observation import availability


@dataclass(frozen=True)
class DeviceControlHooks:
    lock: ContextManager[object]
    connected: Callable[[], bool]
    cancel_session: Callable[[str], int]
    event: EventSink
    touch: Callable[[], None]


class DeviceController:
    def __init__(self, hooks: DeviceControlHooks):
        self.hooks = hooks
        self.serial = 0
        self.waiters = {}

    def control(self, action, percent=None, *, timeout=5):
        if type(timeout) not in (int, float) or not 0 < timeout <= 5:
            raise ValueError('Invalid device control timeout')
        if action not in ('status', 'volume', 'stop', 'wake_screen'):
            raise ValueError('Unsupported device action')
        if not self.hooks.connected():
            raise RuntimeError('CC8 disconnected')
        if action == 'stop':
            self.hooks.cancel_session('dot_device_stop')
        with self.hooks.lock:
            self.serial += 1
            ident = str(self.serial)
            waiter = queue.Queue(maxsize=1)
            self.waiters[ident] = waiter
        try:
            fields = {'request_id': ident, 'action': action}
            if percent is not None:
                fields['percent'] = percent
            self.hooks.event('device_control', **fields)
            try:
                result = waiter.get(timeout=timeout)
                if result.get('ok') and action == 'wake_screen':
                    self.hooks.touch()
                return result
            except queue.Empty:
                raise RuntimeError('CC8 control was not acknowledged') from None
        finally:
            with self.hooks.lock:
                self.waiters.pop(ident, None)

    def receive(self, j):
        with self.hooks.lock:
            waiter = self.waiters.get(str(j.get('request_id')))
        if waiter:
            try:
                result = {
                    k: v
                    for k, v in j.items()
                    if k
                    in (
                        'ok',
                        'volume_percent',
                        'microphone_active',
                        'foreground',
                        'error',
                        'transport',
                        'wifi_ready',
                    )
                }
                sample = availability(j.get('connection_availability'))
                if sample is not None:
                    result['connection_availability'] = sample
                waiter.put_nowait(result)
            except queue.Full:
                pass
