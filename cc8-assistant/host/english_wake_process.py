"""Optional local English confirmation on an isolated, capture-owned process.

Only the fixed name verdict crosses anonymous IPC. MiniMax remains the selected
utterance recognizer. This model must never become a cloud wake fallback.
"""

import importlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from complete_wake_process import CompleteWakeVerifierProcess, MAX_COMPLETE_SAMPLES
from wake_verifier_process import READY, STARTUP_SECONDS, _receive

REQUEST_SECONDS = 2.5
NAME = re.compile(r'(?<![a-z])bumble\s*bee(?![a-z])', re.I)
OPTIONS = {
    'language': 'en',
    'initial_prompt': None,
    'temperature': 0,
    'condition_on_previous_text': False,
    'no_speech_threshold': 0.6,
    'logprob_threshold': -1.0,
    'sample_len': 64,
    'word_timestamps': False,
}


def confirms_name(text):
    return isinstance(text, str) and bool(NAME.search(text))


class EnglishWakeVerifierProcess(CompleteWakeVerifierProcess):
    VALID_REPLIES = frozenset((1, 2))

    def __init__(self):
        super().__init__(worker_script=Path(__file__).resolve(), request_seconds=REQUEST_SECONDS)

    def confirm(self, samples):
        result = self.classify_complete(samples)
        if result is None:
            return None
        return result == (True, False)


def verify_cached_model():
    """Read pinned local artifacts only; never import or allocate the model."""
    from tool_asr_compare import MODEL_MANIFEST, verified_whisper_snapshot

    manifest = json.loads(MODEL_MANIFEST.read_text())
    cache = (
        Path.home() / '.cache/huggingface/hub' / ('models--' + manifest['repo'].replace('/', '--'))
    )
    snapshot, identities = verified_whisper_snapshot(cache, manifest)
    return cache, manifest, snapshot, identities


def load_local_model():
    from tool_asr_compare import verified_whisper_snapshot
    import mlx.core as mx
    import mlx_whisper

    cache, manifest, snapshot, identities = verify_cached_model()
    holder = importlib.import_module('mlx_whisper.transcribe').ModelHolder
    holder.get_model(str(snapshot), mx.float16)
    if verified_whisper_snapshot(cache, manifest, verify_hashes=False) != (snapshot, identities):
        raise ValueError('Local English confirmation model changed while loading')

    def classify(samples):
        # The exact complete NS interval is passed without gain, padding,
        # language guessing, name prompts, transcript aliases or conversation.
        if verified_whisper_snapshot(cache, manifest, verify_hashes=False) != (
            snapshot,
            identities,
        ):
            raise ValueError('Local English confirmation model changed before decoding')
        result = mlx_whisper.transcribe(
            samples, path_or_hf_repo=str(snapshot), verbose=None, **OPTIONS
        )
        if verified_whisper_snapshot(cache, manifest, verify_hashes=False) != (
            snapshot,
            identities,
        ):
            raise ValueError('Local English confirmation model changed during decoding')
        return confirms_name(result['text'])

    return classify


def _worker(fd):
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, STARTUP_SECONDS)
    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY'):
        os.environ[key] = '1'

    def offline(*_args, **_kwargs):
        raise OSError('Local confirmation cannot use a network connection')

    socket.socket.connect = socket.socket.connect_ex = offline
    socket.create_connection = socket.getaddrinfo = offline
    import numpy as np

    with socket.socket(fileno=fd) as channel:
        classify = load_local_model()
        signal.setitimer(signal.ITIMER_REAL, 0)
        channel.sendall(READY)
        while True:
            (size,) = struct.unpack('>I', _receive(channel, 4))
            if size % 640 or not 0 < size <= MAX_COMPLETE_SAMPLES * 4:
                raise ValueError('Invalid English confirmation payload')
            pcm = _receive(channel, size)
            try:
                samples = np.frombuffer(pcm, dtype='<f4')
                if not np.all(np.isfinite(samples)):
                    raise ValueError('Invalid English confirmation samples')
                signal.setitimer(signal.ITIMER_REAL, REQUEST_SECONDS)
                target = classify(samples)
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                pcm[:] = bytes(size)
            channel.sendall(b'\x01' if target else b'\x02')


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--check-model']:
            verify_cached_model()
        else:
            _worker(int(sys.argv[1]))
    except (EOFError, BrokenPipeError):
        pass
