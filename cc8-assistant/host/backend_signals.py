"""Signal callbacks record intent only; the main loop owns resource mutation."""


class BackendSignals:
    def __init__(self) -> None:
        self.stopping = False
        self.retry_scope: tuple[int, int] | None = None

    def request_stop(self, *unused: object) -> None:
        # No locks, Event.set(), I/O or provider work in a Python signal frame.
        self.stopping = True

    def request_retry(self, generation: int, epoch: int, active: bool) -> None:
        if not self.stopping and active:
            self.retry_scope = (generation, epoch)

    def take_retry(self) -> tuple[int, int] | None:
        pending = None if self.stopping else self.retry_scope
        self.retry_scope = None
        return pending
