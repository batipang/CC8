"""Resolve installed Mac synthesis voices before explicit public audio probes.

`say` may silently choose the system default for a missing name; a zero exit
status is insufficient evidence of the intended synthetic speaker.
"""

from functools import lru_cache
import re
import subprocess
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
            '-r',
            str(rate),
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


def read_pcm(path):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Invalid synthetic fixture format')
        if not 0 < audio.getnframes() <= 16000 * 10:
            raise ValueError('Synthetic fixture exceeds duration bound')
        expected = audio.getnframes() * 2
        pcm = audio.readframes(audio.getnframes())
        if len(pcm) != expected:
            raise ValueError('Truncated synthetic fixture')
        return pcm
