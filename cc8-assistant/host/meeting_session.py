"""Passive meeting timeline: explicit capture owners, bounded chunks and honest gaps."""

from copy import deepcopy
from dataclasses import dataclass, field
import uuid

from meeting_asr import CHUNK_SAMPLES, RATE, MIN_SAMPLES, Transcript
from meeting_protocol import MAX_ROWS, token, integer, text_value

MAX_SAMPLES = RATE * 3600
MAX_TEXT_BYTES = 256 * 1024
MAX_SPANS = 2048
MAX_ALIGNMENT_BYTES = 256 * 1024
MAX_PACKET_SAMPLES = 1600
REASONS = frozenset(
    (
        'transport_gap',
        'protocol_error',
        'route_lost',
        'background',
        'cancelled',
        'duration_limit',
        'segment_limit',
        'text_limit',
        'backpressure',
        'timeout',
        'quota',
        'credential_or_permission',
        'network',
        'asr_failed',
        'no_signal',
        'capture_failed',
    )
)


@dataclass(frozen=True)
class CaptureOwner:
    route: int
    generation: int
    nonce: str

    def __post_init__(self):
        if (
            not integer(self.route, 2**63 - 1)
            or not integer(self.generation, 2**32 - 1)
            or not token(self.nonce)
        ):
            raise ValueError('Invalid meeting capture owner')


@dataclass(frozen=True)
class Chunk:
    ident: str
    meeting: str
    start_sample: int
    end_sample: int
    pcm: bytes = field(repr=False)

    def __post_init__(self):
        if (
            not token(self.ident)
            or not token(self.meeting)
            or not integer(self.start_sample, MAX_SAMPLES)
            or not integer(self.end_sample, MAX_SAMPLES)
            or not MIN_SAMPLES <= self.end_sample - self.start_sample <= CHUNK_SAMPLES
            or type(self.pcm) is not bytes
            or len(self.pcm) != (self.end_sample - self.start_sample) * 2
        ):
            raise ValueError('Invalid meeting chunk bounds')


class MeetingSession:
    """No microphone, file, model, worker or scheduling operations in this owner."""

    def __init__(self, ident, *, maximum_samples=MAX_SAMPLES):
        if (
            not token(ident)
            or not integer(maximum_samples, MAX_SAMPLES)
            or maximum_samples < MIN_SAMPLES
        ):
            raise ValueError('Invalid meeting bounds')
        self.ident, self.maximum_samples = ident, maximum_samples
        self.state, self.reason = 'ready', ''
        self.owner = None
        self.used_nonces = set()
        self.revision = 0
        self.position = self.received_samples = 0
        self.next_sequence = self.next_frame = 0
        self.buffer = bytearray()
        self.buffer_start = 0
        self.rows = []

    def _change(self):
        self.revision += 1

    def start(self, owner):
        if not isinstance(owner, CaptureOwner) or self.state not in ('ready', 'paused'):
            raise ValueError('Meeting cannot start capture')
        if owner.nonce in self.used_nonces or len(self.used_nonces) >= MAX_ROWS:
            raise ValueError('Meeting capture consent already used or exhausted')
        if self.position >= self.maximum_samples or len(self.rows) >= MAX_ROWS:
            raise ValueError('Meeting limit reached')
        self.used_nonces.add(owner.nonce)
        self.owner = owner
        self.state, self.reason = 'capturing', ''
        self.next_sequence = self.next_frame = 0
        self.buffer_start = self.position
        self._change()

    def _row(self, start, end, state, reason=''):
        if len(self.rows) >= MAX_ROWS:
            self.state, self.reason, self.owner = 'ended', 'segment_limit', None
            self.buffer.clear()
            self._change()
            return None
        row = dict(
            id=uuid.uuid4().hex,
            start_sample=start,
            end_sample=end,
            state=state,
            reason=reason,
            text='',
            original='',
            edited=False,
            metadata='unavailable',
            spans=[],
        )
        self.rows.append(row)
        self._change()
        return row

    def _seal(self):
        if not self.buffer:
            return ()
        pcm = bytes(self.buffer)
        self.buffer.clear()
        row = self._row(
            self.buffer_start,
            self.position,
            'pending' if len(pcm) >= MIN_SAMPLES * 2 else 'too_short',
        )
        self.buffer_start = self.position
        if row is None or row['state'] != 'pending':
            return ()
        return (Chunk(row['id'], self.ident, row['start_sample'], row['end_sample'], pcm),)

    def receive(self, owner, sequence, frame, pcm):
        """Return accepted sample count and sealed chunks; stale owners change nothing."""
        if self.state != 'capturing' or owner != self.owner:
            return 0, ()
        if (
            not integer(sequence, 2**64 - 1)
            or not integer(frame, self.maximum_samples)
            or type(pcm) is not bytes
            or not pcm
            or len(pcm) % 2
            or len(pcm) > MAX_PACKET_SAMPLES * 2
        ):
            self.interrupt('protocol_error')
            return 0, ()
        if sequence < self.next_sequence or frame < self.next_frame:
            return 0, ()  # duplicate/replayed data cannot corrupt current capture
        lost = frame - self.next_frame
        if (sequence > self.next_sequence) != (lost > 0):
            self.interrupt('protocol_error')
            return 0, ()
        count = len(pcm) // 2
        if self.position + lost + count > self.maximum_samples:
            self.interrupt('duration_limit')
            self.state = 'ended'
            return 0, ()
        ready = []
        received_before = self.received_samples
        if lost:
            ready.extend(self._seal())
            row = self._row(self.position, self.position + lost, 'gap', 'transport_gap')
            if row is None:
                return 0, tuple(ready)
            self.position += lost
            self.buffer_start = self.position
        self.next_sequence, self.next_frame = sequence + 1, frame + count
        offset = 0
        while offset < len(pcm):
            if self.state != 'capturing':
                return self.received_samples - received_before, tuple(ready)
            take = min(len(pcm) - offset, CHUNK_SAMPLES * 2 - len(self.buffer))
            self.buffer.extend(pcm[offset : offset + take])
            offset += take
            self.position += take // 2
            self.received_samples += take // 2
            if len(self.buffer) == CHUNK_SAMPLES * 2:
                ready.extend(self._seal())
        if self.position == self.maximum_samples:
            ready.extend(self._seal())
            self.owner = None
            self.state, self.reason = 'ended', 'duration_limit'
            self._change()
        return self.received_samples - received_before, tuple(ready)

    def pause(self):
        if self.state != 'capturing':
            raise ValueError('Meeting is not capturing')
        self.owner = None
        self.state = 'paused'
        result = self._seal()
        self._change()
        return result

    def end(self):
        if self.state not in ('capturing', 'paused'):
            raise ValueError('Meeting cannot end')
        self.owner = None
        self.state = 'ended'
        result = self._seal()
        self._change()
        return result

    def interrupt(self, reason):
        if reason not in REASONS:
            raise ValueError('Invalid meeting interruption')
        if self.state not in ('capturing', 'paused', 'ended'):
            return
        ended = self.state == 'ended'
        if ended and not any(r['state'] == 'pending' for r in self.rows):
            return
        previous_reason = self.reason
        self.owner = None
        self._seal()  # mark, but never submit new ASR work after interruption
        for row in self.rows:
            if row['state'] == 'pending':
                row.update(state='unconfirmed', reason=reason)
        terminal = ended or self.state == 'ended' or reason in ('duration_limit', 'segment_limit')
        self.state = 'ended' if terminal else 'paused'
        self.reason = (previous_reason or reason) if ended else reason
        self._change()

    def _pending(self, chunk):
        if not isinstance(chunk, Chunk) or chunk.meeting != self.ident or self.state == 'deleted':
            return None
        return next(
            (
                r
                for r in self.rows
                if r['id'] == chunk.ident
                and r['state'] == 'pending'
                and r['start_sample'] == chunk.start_sample
                and r['end_sample'] == chunk.end_sample
            ),
            None,
        )

    def _text_bytes(self):
        return sum(
            len(r['text'].encode())
            + (len(r['original'].encode()) if r['original'] != r['text'] else 0)
            for r in self.rows
        )

    def complete(self, chunk, result):
        row = self._pending(chunk)
        if row is None:
            return False
        if (
            not isinstance(result, Transcript)
            or type(result.duration_ms) is not int
            or result.duration_ms != round((chunk.end_sample - chunk.start_sample) * 1000 / RATE)
        ):
            self.fail(chunk, 'asr_failed')
            return False
        try:
            text = text_value(result.text)
        except (ValueError, UnicodeError):
            self.fail(chunk, 'asr_failed')
            return False
        if self._text_bytes() + len(text.encode()) > MAX_TEXT_BYTES:
            self.fail(chunk, 'text_limit')
            return False
        span_count = sum(len(r['spans']) for r in self.rows)
        alignment_bytes = sum(len(s['text'].encode('utf-8')) for r in self.rows for s in r['spans'])
        spans = []
        if (
            result.metadata == 'available'
            and span_count + len(result.spans) <= MAX_SPANS
            and alignment_bytes + sum(len(s.text.encode('utf-8')) for s in result.spans)
            <= MAX_ALIGNMENT_BYTES
        ):
            spans = [
                dict(start_ms=s.start_ms, end_ms=s.end_ms, speaker=s.speaker, text=s.text)
                for s in result.spans
            ]
        row.update(
            state='recognized' if text else 'empty',
            text=text,
            original=text,
            spans=spans,
            metadata=result.metadata if spans or not result.spans else 'resource_limit',
        )
        self._change()
        return True

    def fail(self, chunk, reason):
        if reason not in REASONS:
            raise ValueError('Invalid meeting failure reason')
        row = self._pending(chunk)
        if row is None:
            return False
        row.update(state='unconfirmed', reason=reason)
        self._change()
        return True

    def edit(self, ident, revision, text):
        if (
            self.state not in ('paused', 'ended')
            or revision != self.revision
            or type(revision) is not int
        ):
            raise ValueError('Meeting changed or is still capturing')
        if any(r['state'] == 'pending' for r in self.rows):
            raise ValueError('Meeting recognition still pending')
        row = next(
            (r for r in self.rows if r['id'] == ident and r['state'] in ('recognized', 'empty')),
            None,
        )
        if row is None:
            raise ValueError('Meeting row cannot be corrected')
        text = text_value(text)
        old = row['text']
        if text == old:
            return False
        # Include both original and corrected text in the memory budget.
        new_size = self._text_bytes() - len(old.encode()) + len(text.encode())
        if old == row['original'] and text != old:
            new_size += len(old.encode())
        if text == row['original'] and old != text:
            new_size -= len(text.encode())
        if new_size > MAX_TEXT_BYTES:
            raise ValueError('Meeting text memory budget reached')
        row.update(text=text, edited=text != row['original'], metadata='edited_text', spans=[])
        self._change()
        return True

    def snapshot(self):
        return deepcopy(
            dict(
                id=self.ident,
                state=self.state,
                reason=self.reason,
                revision=self.revision,
                position_samples=self.position,
                received_samples=self.received_samples,
                rows=self.rows,
                raw_audio_saved=False,
                processing_complete=self.state == 'ended'
                and not self.reason
                and bool(self.rows)
                and all(r['state'] in ('recognized', 'empty') for r in self.rows),
            )
        )

    def delete(self):
        self.owner = None
        self.buffer.clear()
        self.rows.clear()
        self.used_nonces.clear()
        self.state, self.reason = 'deleted', ''
        self._change()
