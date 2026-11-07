"""Attempt every owned cleanup action, then propagate the first real failure."""

from collections.abc import Callable


def cleanup_all(*actions: Callable[[], object]) -> None:
    first: Exception | None = None
    for action in actions:
        try:
            action()
        except Exception as error:
            # A secondary cleanup failure must not hide the original cause or
            # prevent independent resources from receiving their stop request.
            if first is None:
                first = error
    if first is not None:
        raise first
