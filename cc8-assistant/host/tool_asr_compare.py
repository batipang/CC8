"""One local decode of a bounded fixed QA utterance over an anonymous pipe."""

from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

from capture_evidence import TOOL_ASR_MATCH_RESULTS
from qa_speech import (
    SCENARIO_QUESTIONS,
    normalize_question,
    tool_sample_request,
    tool_transcript_rejection,
)


MAX_SAMPLES = 16000 * 13
TIMEOUT_SECONDS = 20


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

        result = MandarinRecognizer(engine='whisper').recognize(np.frombuffer(pcm, dtype='<f4'))
    expected = normalize_question(SCENARIO_QUESTIONS['tool'][case - 1])
    print(tool_transcript_rejection(result, expected) or 'matched')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
