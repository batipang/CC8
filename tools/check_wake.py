"""Reproduce keyword stream boundaries using public Mac synthesis, never a microphone."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import uuid
import wave

if __package__:
    from .private_output import PrivateOutput, private_directory
else:
    from private_output import PrivateOutput, private_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
CONDITIONS = (
    'cold_reference',
    'production_immediate',
    'production_offset_60ms',
    'production_offset_140ms',
    'production_offset_160ms',
    'production_offset_480ms',
    'production_continuous',
    'continuous_reference',
)
PREFIX_SAMPLES = {
    'cold_reference': 0,
    'production_immediate': 0,
    'production_offset_60ms': 960,
    'production_offset_140ms': 2240,
    'production_offset_160ms': 2560,
    'production_offset_480ms': 7680,
    'production_continuous': 16000,
    'continuous_reference': 16000,
}
CASES = tuple(
    (
        f'{voice.lower()}-{rate}',
        voice,
        rate,
        'Bumblebee，你好。' if voice == 'Tingting' else 'Bumblebee',
        True,
    )
    for voice in ('Samantha', 'Alex', 'Daniel', 'Tingting')
    for rate in (140, 170, 200)
) + tuple(
    (f'negative-{index:02}', 'Tingting' if index >= 7 else 'Samantha', 170, text, False)
    for index, text in enumerate(
        (
            'Bumble',
            'Bubble bee',
            'Bee',
            'Bumble by',
            'Hello everybody',
            'Turn on the light',
            'The bee is near a bubble',
            '今天的天气很好',
            '帮我把灯打开',
            '把这个放到旁边',
        )
    )
)


def synthesize(folder, voice, rate, text):
    source, output = folder / 'source.aiff', folder / 'source.wav'
    subprocess.run(
        ['/usr/bin/say', '-v', voice, '-r', str(rate), '-o', str(source), text],
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


def summary(rows):
    result = {}
    for condition in CONDITIONS:
        complete = [r for r in rows if r.get('state') == 'measured' and condition in r]
        result[condition] = {
            'positive_samples': sum(r['expected_keyword'] for r in complete),
            'positive_hits': sum(r['expected_keyword'] and r[condition] for r in complete),
            'negative_samples': sum(not r['expected_keyword'] for r in complete),
            'false_hits': sum(not r['expected_keyword'] and r[condition] for r in complete),
        }
    return result


def passed(rows):
    # Old references are observations, never the pass criterion. Every fixed
    # production start offset must match the corpus labels, including negatives.
    expected = {case[0]: case[4] for case in CASES}
    return (
        len(rows) == len(CASES)
        and {r.get('case') for r in rows} == {c[0] for c in CASES}
        and all(
            r.get('state') == 'measured'
            and type(r.get('expected_keyword')) is bool
            and r['expected_keyword'] == expected[r['case']]
            and all(
                type(r.get(k)) is bool and r[k] == r['expected_keyword']
                for k in CONDITIONS
                if k.startswith('production_')
            )
            for r in rows
        )
    )


def evaluate(model_root, output):
    # Explicit execution only. Importing this module or printing a plan never
    # imports a native model, reads credentials, or starts device capture.
    import numpy as np
    from sherpa_onnx.lib import _sherpa_onnx

    if __package__:
        from .cc8 import verified_models
    else:
        from cc8 import verified_models
    from local_wake import EnglishWake
    from streaming_keywords import StreamingKeywords

    if not verified_models(model_root, 'minimax'):
        raise ValueError('Unverified model resources')
    detector = EnglishWake.load(
        model_root / '.toolchains/cc8/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01'
    )

    class ColdReference(StreamingKeywords):
        KEYWORDS = EnglishWake.KEYWORDS

    cold = ColdReference(detector.detector, gain=detector.gain, lock=detector.engine_lock)
    corpus_hash = hashlib.sha256(
        json.dumps([CASES, PREFIX_SAMPLES], ensure_ascii=False).encode()
    ).hexdigest()
    from local_wake import __file__ as wake_source

    source_hash = hashlib.sha256(Path(wake_source).read_bytes()).hexdigest()
    native_versions = {}
    for name in ('sherpa-onnx', 'sherpa-onnx-core'):
        try:
            native_versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            native_versions[name] = None
    binary = Path(_sherpa_onnx.__file__)
    native_files = [binary, *binary.parent.glob('*.dylib')]
    native_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in native_files}
    rows = []

    def save(state):
        output.write(
            {
                'schema': 1,
                'state': state,
                'scope': 'public_synthetic_keyword_boundaries',
                'source_sha256': source_hash,
                'corpus_sha256': corpus_hash,
                'native_package_versions': native_versions,
                'native_binary_sha256': native_hashes,
                'microphone_used': False,
                'device_used': False,
                'network_requested': False,
                'raw_audio_retained': False,
                'human_accuracy_asserted': False,
                'samples': rows,
                'summary': summary(rows),
            }
        )

    save('running')
    try:
        for ident, voice, rate, text, positive in CASES:
            row = {'case': ident, 'expected_keyword': positive}
            try:
                with tempfile.TemporaryDirectory(prefix='cc8-public-wake-fixture-') as temporary:
                    pcm = synthesize(Path(temporary), voice, rate, text)
                samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
                row['fixture_pcm_sha256'] = hashlib.sha256(pcm).hexdigest()
                for condition in CONDITIONS:
                    current = detector if condition.startswith('production_') else cold
                    current.reset()
                    audio = np.concatenate(
                        [
                            np.zeros(PREFIX_SAMPLES[condition], dtype=np.float32),
                            samples,
                            np.zeros(32000, dtype=np.float32),
                        ]
                    )
                    hit = False
                    for index in range(0, len(audio), 320):
                        hit = bool(current.feed(audio[index : index + 320])) or hit
                    row[condition] = hit
                row['state'] = 'measured'
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                row['state'] = 'fixture_or_model_failed'
            rows.append(row)
            save('running')
    except KeyboardInterrupt:
        save('cancelled')
        raise
    state = 'passed' if passed(rows) else 'failed'
    save(state)
    return state, summary(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-root', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    'execute': False,
                    'sample_count': len(CASES),
                    'conditions': CONDITIONS,
                    'microphone_used': False,
                    'network_requested': False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    try:
        if platform.system() != 'Darwin':
            raise ValueError('Mac synthesizer required')
        if not args.model_root.is_absolute() or args.model_root.resolve() != args.model_root:
            raise ValueError('Real absolute model root required')
        folder = private_directory(
            private_directory(ROOT / 'device-access/private') / 'wake-checks'
        )
        name = (
            'synthetic-'
            + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
            + uuid.uuid4().hex[:8]
            + '.json'
        )
        with PrivateOutput(folder / name) as output:
            state, counts = evaluate(args.model_root, output)
            print(json.dumps({'state': state, 'summary': counts}, ensure_ascii=False))
            print(output.path)
            return 0 if state == 'passed' else 2
    except KeyboardInterrupt:
        print('合成唤醒检查已取消。', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError):
        print(
            '合成唤醒检查未完成；请核对隔离评测 Python、固定模型和 Mac 合成音色。', file=sys.stderr
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
