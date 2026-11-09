"""One bounded RAM-only UI snapshot; paging never reads history or restores voice context."""

from copy import deepcopy
import time


class DisplaySnapshot:
    def __init__(self, context, clock=time.monotonic):
        self.context, self.clock = context, clock
        self.value = None

    def clear(self):
        self.value = None

    def remember(self, report):
        self.value = (self.context(), self.clock() + 600, deepcopy(report))

    def read(self, ident):
        if self.value is not None:
            owner, deadline, report = self.value
            current = self.context()
            if (
                owner[0] is not current[0]
                or owner[2] is not current[2]
                or (owner[1], *owner[3:]) != (current[1], *current[3:])
                or self.clock() >= deadline
            ):
                self.clear()
            elif report['snapshot_id'] == ident:
                return deepcopy(report)
        raise ValueError('Report display snapshot is no longer current')
