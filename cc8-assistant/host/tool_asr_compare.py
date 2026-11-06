"""One local decode of a bounded fixed QA utterance over an anonymous pipe."""

from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import numpy as np

from capture_evidence import TOOL_ASR_MATCH_RESULTS
from qa_speech import (
    SCENARIO_QUESTIONS,
    normalize_question,
    tool_asr_result,
    tool_sample_request,
)


MAX_SAMPLES = 16000 * 13
TIMEOUT_SECONDS = 20
MODEL_MANIFEST = Path(__file__).with_name('resources') / 'tool-whisper-model.json'


def verified_whisper_snapshot(cache, manifest, *, verify_hashes=True):
    snapshots = (cache / 'snapshots').resolve(strict=True)
    snapshot = (snapshots / manifest['revision']).resolve(strict=True)
    if snapshot.parent != snapshots or snapshot.name != manifest['revision']:
        raise ValueError('Local Whisper snapshot has an invalid path')
    blobs = (cache / 'blobs').resolve()
    shared_blobs = (cache.parent / 'blobs').resolve()
    identities = {}
    for filename, expected in manifest['files'].items():
        path = (snapshot / filename).resolve(strict=True)
        if path.parent not in (snapshot, blobs) and not path.is_relative_to(shared_blobs):
            raise ValueError('Local Whisper artifact is outside its cache')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as artifact:
            info = os.fstat(artifact.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected['size']:
                raise ValueError('Local Whisper artifact has an invalid size')
            if (
                verify_hashes
                and hashlib.file_digest(artifact, 'sha256').hexdigest() != expected['sha256']
            ):
                raise ValueError('Local Whisper artifact checksum mismatch')
            identities[filename] = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
    return snapshot, identities


def compare_local(case, audio, is_current):
    if not is_current():
        return None
    samples = np.asarray(audio, dtype='<f4')
    if samples.ndim != 1 or not 0 < len(samples) <= MAX_SAMPLES:
        return 'error'
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(case)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return 'error'
    try:
        pending = samples.tobytes()
        while is_current():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 'timeout'
            try:
                output, _ = process.communicate(pending, timeout=min(0.25, remaining))
            except subprocess.TimeoutExpired:
                pending = None
                continue
            category = output.decode('ascii', errors='replace').strip()
            return (
                category
                if process.returncode == 0 and category in TOOL_ASR_MATCH_RESULTS
                else 'error'
            )
        return None
    except (OSError, ValueError):
        return 'error'
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()


def main():
    if (
        len(sys.argv) != 2
        or not 1 <= len(sys.argv[1]) <= 2
        or not sys.argv[1].isascii()
        or not sys.argv[1].isdigit()
    ):
        return 2
    case = int(sys.argv[1])
    if not tool_sample_request({'operation': 'tool_sample_compare_arm', 'case': case}):
        return 2
    pcm = sys.stdin.buffer.read(MAX_SAMPLES * 4 + 1)
    if not 0 < len(pcm) <= MAX_SAMPLES * 4 or len(pcm) % 4:
        return 2
    with redirect_stdout(sys.stderr):
        from recognition import MandarinRecognizer

        manifest = json.loads(MODEL_MANIFEST.read_text())
        cache_name = 'models--' + manifest['repo'].replace('/', '--')
        cache = Path.home() / '.cache/huggingface/hub' / cache_name
        snapshot, identities = verified_whisper_snapshot(cache, manifest)
        result = MandarinRecognizer(engine='whisper', whisper_model=snapshot).recognize(
            np.frombuffer(pcm, dtype='<f4')
        )
        if verified_whisper_snapshot(cache, manifest, verify_hashes=False) != (
            snapshot,
            identities,
        ):
            raise ValueError('Local Whisper snapshot changed during comparison')
    expected = normalize_question(SCENARIO_QUESTIONS['tool'][case - 1])
    print(tool_asr_result(result, expected))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
