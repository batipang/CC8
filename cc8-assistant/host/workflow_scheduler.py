"""No worker or startup replay: explicit foreground grants submit into the serial queue."""

from dataclasses import dataclass
import time
from typing import Callable, ContextManager

from workflow_definition import fingerprint
from workflow_schedule import due_slot, timestamp


@dataclass(frozen=True)
class ScheduleHooks:
    lock: ContextManager
    owner: Callable
    activation: Callable
    blocked: Callable
    ready: Callable
    submit: Callable
    error: Callable[[str], None] = lambda kind: None


class WorkflowScheduler:
    def __init__(self, store, hooks, *, clock=time.time):
        self.store, self.hooks, self.clock = store, hooks, clock
        self.grant = None
        self.last_activation = None

    def revoke(self):
        self.grant = None

    def authorized(self, definition_hash):
        grant = self.grant
        return (
            grant is not None
            and grant[0] == self.hooks.owner()
            and definition_hash in grant[1].values()
            and not self.hooks.blocked()
        )

    def arm(self):
        with self.hooks.lock:
            owner = self.hooks.owner()
            self.last_activation = self.hooks.activation()
            if owner is None or self.hooks.blocked():
                self.revoke()
                return
            state = self.store.snapshot()
            selected = {
                flow['id']: fingerprint(flow)
                for flow in state['items']
                if flow['schedule'] is not None and flow['schedule']['enabled']
            }
            self.grant = (owner, selected, timestamp(self.clock())) if selected else None

    def tick(self):
        with self.hooks.lock:
            activation = self.hooks.activation()
            if self.hooks.blocked():
                self.last_activation = activation
                self.revoke()
                return
            if activation is not None and activation != self.last_activation:
                try:
                    self.arm()
                except Exception as error:
                    self.revoke()
                    self.error(error)
                    return
            grant = self.grant
            if grant is None:
                return
            if grant[0] != self.hooks.owner():
                self.revoke()
                return
            try:
                now = timestamp(self.clock())
                if now < grant[2]:
                    self.revoke()
                    return
                if not self.hooks.ready():
                    return
                state = self.store.snapshot()
                for flow in state['items']:
                    if flow['id'] not in grant[1]:
                        continue
                    if fingerprint(flow) != grant[1][flow['id']]:
                        self.revoke()
                        return
                    slot = due_slot(flow, now)
                    if (
                        slot is None
                        or slot <= grant[2]
                        or any(
                            row['workflow_id'] == flow['id'] and row['slot'] == slot
                            for row in state['attempts']
                        )
                    ):
                        continue

                    def current():
                        return (
                            self.grant is grant
                            and grant[0] == self.hooks.owner()
                            and not self.hooks.blocked()
                            and self.hooks.ready() is True
                        )

                    claim = self.store.claim(flow['id'], state['revision'], slot, current=current)
                    if claim is None:
                        return
                    try:
                        if not current():
                            raise RuntimeError('Scheduled owner changed after durable claim')
                        result = self.hooks.submit(flow, claim)
                        if not isinstance(result, dict) or result.get('ok') is not True:
                            raise RuntimeError('Scheduled queue admission unconfirmed')
                    except Exception:
                        # A queue failure may be ambiguous. Freeze this slot;
                        # neither tick nor restart can use it as a retry ticket.
                        self.store.finish_execution(claim['id'], 'unconfirmed')
                        raise
                    return  # One admission per tick; the queue owns all work.
            except Exception as error:
                self.revoke()
                self.error(error)

    def error(self, error):
        try:
            self.hooks.error(type(error).__name__)
        except Exception:
            # Optional diagnostic logging cannot end the heartbeat owner.
            pass
