"""One source/route-bound ten-minute RAM snapshot of a scheduled read, never voice context."""

from copy import deepcopy
import time


class WatchResults:
    def __init__(self, context, *, clock=time.monotonic):
        self.context, self.clock = context, clock
        self.value = None

    def clear(self):
        self.value = None

    def expire(self):
        if self.value is not None and self.clock() >= self.value[1]:
            self.clear()

    def remaining_ms(self):
        self.expire()
        if self.value is None:
            raise ValueError('Scheduled report results expired')
        remaining = int((self.value[1] - self.clock()) * 1000)
        if remaining < 1:
            raise ValueError('Scheduled report results expired')
        return min(600000, remaining)

    def remember(self, report):
        owner = self.context()
        if owner is not None:
            # Retain the actual connection object, never deepcopy it or rely on
            # a recyclable id(). Context metadata comes from a store snapshot.
            self.value = (owner, self.clock() + 600, deepcopy(report))

    def read(self, ident=None):
        if self.value is not None:
            owner, deadline, report = self.value
            current = self.context()
            if (
                self.clock() >= deadline
                or current is None
                or owner[0] is not current[0]
                or owner[1:] != current[1:]
            ):
                self.clear()
            elif ident is None or ident == report['snapshot_id']:
                return deepcopy(report)
        raise ValueError('Scheduled report results expired or changed')

    def available(self):
        try:
            self.read()
            return True
        except ValueError:
            return False
