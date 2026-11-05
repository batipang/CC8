"""Resolve installed Mac synthesis voices before explicit public audio probes.

`say` may silently choose the system default for a missing name; a zero exit
status is insufficient evidence of the intended synthetic speaker.
"""

from functools import lru_cache
import hashlib
import io
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import wave


def parse_voices(text):
    if len(text) > 128000:
        raise ValueError('Voice inventory exceeds bound')
    names = set()
    for line in text.splitlines():
        match = re.search(r'\s+[a-z]{2,3}_[A-Za-z0-9_]+\s+#', line)
        if match:
            name = line[: match.start()].strip()
            if name and not any(ord(c) < 32 for c in name):
                names.add(name)
    if not names:
        raise ValueError('Voice inventory unavailable')
    return frozenset(names)


@lru_cache(maxsize=1)
def available_voices():
    result = subprocess.run(
        ['/usr/bin/say', '-v', '?'],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return parse_voices(result.stdout)


def resolve_voice(requested):
    # None deliberately selects the system default, preserving old fallback
    # inputs without pretending that a missing named speaker was available.
    if requested is None:
        return None
    names = available_voices()
    matches = {
        name
        for name in names
        if name == requested or re.fullmatch(re.escape(requested) + r' \(.+\)', name)
    }
    if len(matches) != 1:
        raise ValueError('Synthetic voice unavailable or ambiguous')
    return matches.pop()


def synthesize(folder, voice, rate, text):
    resolved = resolve_voice(voice)
    source, output = folder / 'source.aiff', folder / 'source.wav'
    subprocess.run(
        [
            '/usr/bin/say',
            *(['-v', resolved] if resolved is not None else []),
            *(['-r', str(rate)] if rate is not None else []),
            '-o',
            str(source),
            text,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=30,
    )
    subprocess.run(
        [
            '/usr/bin/afconvert',
            '-f',
            'WAVE',
            '-d',
            'LEI16@16000',
            '-c',
            '1',
            str(source),
            str(output),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=15,
    )
    return read_pcm(output)


def validate_prepared_speech(value):
    fields = {
        'schema',
        'sample_rate',
        'channels',
        'sample_width_bytes',
        'frames',
        'pcm_sha256',
        'wav_sha256',
        'acoustic_playback_confirmed',
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Invalid prepared speech fields')
    for key, expected in (
        ('schema', 1),
        ('sample_rate', 16000),
        ('channels', 1),
        ('sample_width_bytes', 2),
    ):
        if type(value[key]) is not int or value[key] != expected:
            raise ValueError('Invalid prepared speech format')
    if type(value['frames']) is not int or not 0 < value['frames'] <= 160000:
        raise ValueError('Invalid prepared speech duration')
    if (
        any(
            not isinstance(value[key], str) or not re.fullmatch('[a-f0-9]{64}', value[key])
            for key in ('pcm_sha256', 'wav_sha256')
        )
        or value['acoustic_playback_confirmed'] is not False
    ):
        raise ValueError('Invalid prepared speech evidence')
    return dict(value)


class SpeechFixtureCleanupError(RuntimeError):
    def __init__(self, fixture):
        super().__init__('Synthetic fixture cleanup unconfirmed')
        self.fixture = fixture


class PreparedSpeech:
    """Own one bounded public synthetic fixture until playback and cleanup finish."""

    MAX_WAV_BYTES = 1024 * 1024

    def __init__(self, voice, text, *, rate=None, directory=None):
        if (
            not isinstance(text, str)
            or not 0 < len(text) <= 120
            or text.startswith('-')
            or any(ord(char) < 32 for char in text)
            or resolve_voice(voice) is None
        ):
            raise ValueError('Invalid explicit synthetic speech input')
        self._closed = False
        self._pcm = b''
        self._temporary = TemporaryDirectory(prefix='cc8-public-speech-', dir=directory)
        self._path = Path(self._temporary.name) / 'source.wav'
        try:
            pcm = synthesize(self._path.parent, voice, rate, text)
            artifact = self._read_artifact()
            if not pcm or not any(pcm) or pcm != _read_pcm(io.BytesIO(artifact)):
                raise ValueError('Invalid prepared speech samples')
            self._path.chmod(0o600)
            self._pcm = pcm
            self._metadata = validate_prepared_speech(
                dict(
                    schema=1,
                    sample_rate=16000,
                    channels=1,
                    sample_width_bytes=2,
                    frames=len(pcm) // 2,
                    pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                    wav_sha256=hashlib.sha256(artifact).hexdigest(),
                    acoustic_playback_confirmed=False,
                )
            )
        except BaseException:
            try:
                self.close()
            except BaseException:
                # Preserve cleanup ownership even when construction fails.
                raise SpeechFixtureCleanupError(self) from None
            raise

    def _read_artifact(self):
        if self._path.is_symlink() or not self._path.is_file():
            raise ValueError('Invalid synthetic fixture file')
        with self._path.open('rb') as source:
            data = source.read(self.MAX_WAV_BYTES + 1)
        if not 0 < len(data) <= self.MAX_WAV_BYTES:
            raise ValueError('Synthetic fixture file exceeds bound')
        return data

    @property
    def metadata(self):
        return dict(self._metadata)

    @property
    def directory_name(self):
        if self._closed:
            raise ValueError('Synthetic fixture already closed')
        return self._path.parent.name

    @property
    def pcm(self):
        if self._closed:
            raise ValueError('Synthetic fixture already closed')
        return self._pcm

    def play(self):
        if self._closed:
            raise ValueError('Synthetic fixture already closed')
        if hashlib.sha256(self._read_artifact()).hexdigest() != self._metadata['wav_sha256']:
            raise ValueError('Synthetic fixture changed after preparation')
        subprocess.run(
            ['/usr/bin/afplay', str(self._path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=20,
        )

    def close(self):
        self._closed = True
        self._pcm = b''
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def read_pcm(path):
    return _read_pcm(str(path))


def _read_pcm(source):
    with wave.open(source, 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Invalid synthetic fixture format')
        if not 0 < audio.getnframes() <= 16000 * 10:
            raise ValueError('Synthetic fixture exceeds duration bound')
        expected = audio.getnframes() * 2
        pcm = audio.readframes(audio.getnframes())
        if len(pcm) != expected:
            raise ValueError('Truncated synthetic fixture')
        return pcm
