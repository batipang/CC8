"""Separately bounded, cancellable meeting chunks; ordinary utterance ASR is unchanged."""

from dataclasses import dataclass, field
import io
import math
import re
import wave

from minimax_client import Transport, MiniMaxError, Cancelled, check_api_result
from protocol import decode_json_object

RATE = 16000
CHUNK_SAMPLES = RATE * 30
MIN_SAMPLES = RATE // 10
MAX_RESPONSE = 512 * 1024
MAX_TEXT = 12000
MAX_SPANS = 256
MAX_ALIGNMENT_BYTES = 64 * 1024


@dataclass(frozen=True)
class Span:
    start_ms: int
    end_ms: int
    speaker: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class Transcript:
    text: str = field(repr=False)
    duration_ms: int
    spans: tuple[Span, ...] = ()
    metadata: str = 'unavailable'


def text_value(value):
    if (
        not isinstance(value, str)
        or len(value) > MAX_TEXT
        or any(ord(c) < 32 and c not in '\n\r\t' for c in value)
    ):
        raise ValueError('Invalid meeting transcript text')
    if len(value.encode('utf-8')) > MAX_TEXT * 4:
        raise ValueError('Meeting transcript text exceeds budget')
    return value.strip()


def numeric(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def timed_spans(data, text, seconds):
    """Optional alignment is evidence, never guessed from text or speaker names."""
    rows = data.get('segments')
    if rows is None:
        return (), 'unavailable'
    if not isinstance(rows, list) or len(rows) > MAX_SPANS:
        return (), 'invalid'
    spans, previous, size = [], 0.0, 0
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid meeting timing row')
            start, end, speaker = row.get('start'), row.get('end'), row.get('speaker')
            if (
                not numeric(start)
                or not numeric(end)
                or not 0 <= start < end <= seconds + 0.05
                or start < previous
                or not isinstance(speaker, str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,32}', speaker) is None
            ):
                raise ValueError('Invalid meeting timing values')
            content = text_value(row.get('text'))
            if not content:
                raise ValueError('Empty meeting timing content')
            size += len(content.encode('utf-8'))
            if size > MAX_ALIGNMENT_BYTES:
                raise ValueError('Meeting timing text exceeds budget')
            previous = start
            first, last = round(start * 1000), min(round(end * 1000), round(seconds * 1000))
            if first >= last:
                raise ValueError('Meeting timing precision unavailable')
            spans.append(Span(first, last, speaker, content))
        # Whitespace differences may reflect segment boundaries; never repair or
        # replace the actual transcript. Other disagreement invalidates alignment.
        if ''.join(''.join(s.text.split()) for s in spans) != ''.join(text.split()):
            raise ValueError('Meeting timing text disagrees')
    except (ValueError, UnicodeError):
        return (), 'invalid'
    return tuple(spans), 'available'


class MeetingAsr(Transport):
    """An independent Transport instance owns meeting requests and cancellation."""

    def transcribe_chunk(self, pcm, is_current=lambda: True, *, language='auto'):
        if (
            type(pcm) is not bytes
            or len(pcm) % 2
            or not MIN_SAMPLES * 2 <= len(pcm) <= CHUNK_SAMPLES * 2
        ):
            raise ValueError('Meeting PCM chunk exceeds bounds')
        if type(language) is not str or language not in ('auto', 'zh', 'en'):
            raise ValueError('Invalid meeting language hint')
        # This module accepts a bounded in-memory PCM chunk, never a path/URL.
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(RATE)
            audio.writeframes(pcm)
        wav = stream.getvalue()
        seconds = len(pcm) / (RATE * 2)
        headers = {'Authorization': 'Bearer ' + self.key}
        if language != 'auto':
            headers['language'] = language
        slot = self.request_slot()
        try:
            slot.check(is_current)
            response = slot.session.post(
                self.base + '/v1/speech_to_text',
                data={
                    'model': self.config.asr_model,
                    'response_format': 'verbose_json',
                    'timestamp_level': 'sentence',
                    'stream': 'false',
                },
                files={'file': ('meeting-chunk.wav', wav, 'audio/wav')},
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=self.timeout,
            )
            slot.response = response
            slot.check(is_current)
            if response.status_code != 200:
                raise MiniMaxError(
                    'MiniMax meeting ASR HTTP request failed', http_status=response.status_code
                )
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=8192):
                slot.check(is_current)
                if type(chunk) is not bytes:
                    raise ValueError('Invalid meeting ASR body')
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError('Meeting ASR response exceeds bounds')
                chunks.append(chunk)
            slot.check(is_current)
            data = decode_json_object(b''.join(chunks), MAX_RESPONSE)
            check_api_result(data)
            text = text_value(data.get('text'))
            duration = data.get('duration')
            # Check approximate provider-reported input duration. This does not
            # prove word coverage or transcription accuracy; retain that limit.
            if not numeric(duration) or duration <= 0 or abs(duration - seconds) > 0.25:
                raise ValueError('Meeting ASR input duration not confirmed')
            spans, metadata = timed_spans(data, text, seconds)
            return Transcript(text, round(seconds * 1000), spans, metadata)
        except OSError as error:
            if slot.stopped.is_set() or not is_current():
                raise Cancelled('Meeting request cancelled') from None
            raise MiniMaxError(
                'MiniMax meeting ASR network request failed', kind=self.network_kind(error, slot)
            ) from None
        except (ValueError, TypeError, AttributeError):
            raise MiniMaxError('Invalid MiniMax meeting ASR response') from None
        finally:
            self.release_slot(slot)
