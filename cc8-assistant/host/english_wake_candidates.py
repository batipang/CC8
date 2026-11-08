"""One-use English confirmation receipts; session guard owns every operation.

Candidate detection is evidence only. This component never wakes a session,
starts a model, or authorizes cloud recognition. The controller must recheck
capture, playback and model ownership before and after local classification.
"""

from collections import deque
from dataclasses import dataclass, field
import re
import secrets
from typing import Callable

from wake_utterance import WaitingUtterance

MAX_SPAN_NS = 12_000_000_000
MAX_COUNTER = 2**63 - 1
MAX_CANDIDATES = 16
MAX_ISSUED = 8
Owner = tuple[int, int, int]


def counter(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNTER


@dataclass(frozen=True)
class EnglishWaitingUtterance(WaitingUtterance):
    verifier_generation: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.keyword != 'Bumblebee' or not counter(self.verifier_generation):
            raise ValueError('Invalid English verifier owner')


@dataclass(frozen=True)
class EnglishConfirmation(EnglishWaitingUtterance):
    confirmation_token: str = field(default='', repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            self.keyword != 'Bumblebee'
            or self.last_sample_ns is None
            or not counter(self.verifier_generation)
            or not isinstance(self.confirmation_token, str)
            or re.fullmatch('[a-f0-9]{32}', self.confirmation_token) is None
        ):
            raise ValueError('Invalid English confirmation receipt')


def identity(ticket: EnglishConfirmation) -> tuple[int, int, int, int | None, int, int]:
    return (
        ticket.serial,
        ticket.generation,
        ticket.first_sample_ns,
        ticket.last_sample_ns,
        ticket.model_generation,
        ticket.verifier_generation,
    )


class EnglishWakeCandidates:
    """Bounded metadata, never audio; do not acquire another lock inside here."""

    def __init__(self, token: Callable[[int], str] = secrets.token_hex):
        self.token = token
        self.owner: Owner | None = None
        self.points: deque[int] = deque(maxlen=MAX_CANDIDATES)
        self.issued: dict[str, tuple[int, int, int, int | None, int, int]] = {}
        self.last_stamp: int | None = None
        self.blocked = False

    def reset(self) -> None:
        self.owner = None
        self.points.clear()
        self.issued.clear()
        self.last_stamp = None
        self.blocked = False

    def mark(self, stamp: int, *, serial: int, generation: int, model_generation: int) -> bool:
        if not all(counter(value) for value in (stamp, serial, generation, model_generation)):
            return False
        owner = (serial, generation, model_generation)
        if owner != self.owner:
            self.reset()
            self.owner = owner
        if self.blocked:
            return False
        if self.last_stamp is not None and stamp <= self.last_stamp:
            if stamp < self.last_stamp:
                # A regressed clock cannot issue another receipt until an
                # explicit reset or a different capture/model owner arrives.
                self.points.clear()
                self.issued.clear()
                self.blocked = True
            return False
        self.last_stamp = stamp
        while self.points and self.points[0] < stamp - MAX_SPAN_NS:
            self.points.popleft()
        self.points.append(stamp)
        return True

    def issue(
        self, base: WaitingUtterance, last_sample_ns: int, *, model_generation: int
    ) -> EnglishConfirmation | None:
        if (
            type(base) not in (WaitingUtterance, EnglishWaitingUtterance)
            or base.keyword != 'Bumblebee'
            or base.last_sample_ns is not None
            or not all(
                counter(value)
                for value in (
                    base.serial,
                    base.generation,
                    base.model_generation,
                    base.first_sample_ns,
                    last_sample_ns,
                    model_generation,
                )
            )
            or not 0 <= last_sample_ns - base.first_sample_ns < MAX_SPAN_NS
            or self.blocked
            or self.owner != (base.serial, base.generation, model_generation)
            or (
                type(base) is EnglishWaitingUtterance
                and base.verifier_generation != model_generation
            )
            or not any(base.first_sample_ns <= point <= last_sample_ns for point in self.points)
        ):
            return None
        try:
            token = self.token(16)
        except (OSError, ValueError):
            return None
        if (
            not isinstance(token, str)
            or re.fullmatch('[a-f0-9]{32}', token) is None
            or token in self.issued
        ):
            return None
        receipt = EnglishConfirmation(
            serial=base.serial,
            generation=base.generation,
            first_sample_ns=base.first_sample_ns,
            model_generation=base.model_generation,
            last_sample_ns=last_sample_ns,
            confirmation_token=token,
            verifier_generation=model_generation,
        )
        # One observed native candidate cannot be reused by overlapping or
        # duplicated endpoint jobs. Future points retain their own interval.
        self.points = deque(
            (point for point in self.points if point > last_sample_ns), maxlen=MAX_CANDIDATES
        )
        if len(self.issued) == MAX_ISSUED:
            del self.issued[next(iter(self.issued))]
        self.issued[token] = identity(receipt)
        return receipt

    def consume(self, ticket: EnglishConfirmation) -> bool:
        if type(ticket) is not EnglishConfirmation or self.blocked:
            return False
        token = ticket.confirmation_token
        if self.issued.get(token) != identity(ticket):
            return False
        del self.issued[token]
        return True

    def snapshot(self) -> dict[str, int | bool]:
        return {
            'pending_candidates': len(self.points),
            'issued_receipts': len(self.issued),
            'clock_blocked': self.blocked,
        }
