"""Configured speech providers own their recognizer, TTS cancellation and cache.

No session, transport, playback or device-capture state is stored here.
"""

from pathlib import Path
import subprocess
import tempfile
import wave
import numpy as np
from configuration import load_config
from recognition import MandarinRecognizer
from minimax_client import MiniMaxVoice, MiniMaxError
from dsp import RATE

ROOT = Path(__file__).resolve().parents[2]


def model_path():
    cache = (
        Path.home()
        / '.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots'
    )
    matches = sorted(p for p in cache.glob('*') if (p / 'weights.safetensors').exists())
    if not matches:
        raise RuntimeError('Local MLX speech model is missing')
    return str(matches[-1])


def wav_samples(path):
    with wave.open(str(path), 'rb') as f:
        if f.getframerate() != RATE or f.getnchannels() != 1 or f.getsampwidth() != 2:
            raise ValueError('Expected 16 kHz mono PCM16')
        return np.frombuffer(f.readframes(f.getnframes()), dtype='<i2').astype(float) / 32768


class LocalSpeech:
    def __init__(self, config=None):
        settings = config or load_config()
        self.recognizer = MandarinRecognizer(config=settings)
        self.cache = {}

    def recognize(self, samples):
        return self.recognizer.recognize(samples)

    def recognize_current(self, samples, is_current):
        return self.recognizer.recognize(samples, is_current=is_current)

    def synthesize(self, text):
        if text in self.cache:
            return self.cache[text]
        with tempfile.TemporaryDirectory(prefix='cc8-tts-') as folder:
            source = Path(folder) / 'speech.aiff'
            subprocess.run(
                ['/usr/bin/say', '-v', 'Tingting', '-r', '185', '-o', str(source)],
                input=text.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                timeout=30,
            )
            raw = subprocess.check_output(
                [
                    '/opt/homebrew/bin/ffmpeg',
                    '-v',
                    'error',
                    '-i',
                    str(source),
                    '-ar',
                    str(RATE),
                    '-ac',
                    '1',
                    '-f',
                    's16le',
                    'pipe:1',
                ],
                stderr=subprocess.PIPE,
                timeout=20,
            )
        pcm = np.frombuffer(raw, dtype='<i2').copy()
        if len(pcm) == 0:
            raise RuntimeError('Local TTS returned no samples')
        if len(self.cache) >= 12:
            self.cache.pop(next(iter(self.cache)))
        self.cache[text] = pcm
        return pcm


class DirectSpeech(LocalSpeech):
    def __init__(self, config=None):
        settings = config or load_config()
        super().__init__(config=settings)
        self.voice = MiniMaxVoice(config=settings)

    def cancel(self):
        self.voice.cancel()
        self.recognizer.cancel()

    def stream(self, text, is_current=lambda: True, on_subtitle=None):
        return self.voice.pcm(text, is_current, on_subtitle)

    def synthesize(self, text):
        chunks = list(self.stream(text))
        if not chunks:
            raise MiniMaxError('MiniMax returned no audio')
        return np.concatenate(chunks)
