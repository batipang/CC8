"""Reproduce keyword stream boundaries using public Mac synthesis, never a microphone."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import uuid

if __package__:
    from .private_output import PrivateOutput, private_directory
else:
    from private_output import PrivateOutput, private_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cc8-assistant/host'))
from mac_voice import (
    available_voices as available_voices,
    parse_voices as parse_voices,
    resolve_voice,
    synthesize,
    read_pcm as read_pcm,
    __file__ as voice_source,
)

ROOT = Path(__file__).resolve().parents[1]

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
CASES = (
    tuple(
        (
            f'{voice.lower()}-{rate}',
            voice,
            rate,
            'Bumblebee，你好。' if voice == 'Tingting' else 'Bumblebee',
            True,
        )
        for voice in ('Samantha', 'Karen', 'Daniel', 'Tingting')
        for rate in (140, 170, 200)
    )
    + tuple(
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
    + tuple((f'legacy-default-{rate}', None, rate, 'Bumblebee', True) for rate in (140, 170, 200))
)

CORPUS_VERSION = 'installed-voices-and-explicit-default-v2'


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
            and valid_digest(r.get('fixture_pcm_sha256'))
            and type(r.get('expected_keyword')) is bool
            and r['expected_keyword'] == expected[r['case']]
            and all(
                type(r.get(k)) is bool and r[k] == r['expected_keyword']
                for k in CONDITIONS
                if k.startswith('production_')
            )
            for r in rows
        )
        and not fixture_conflicts(rows)
    )


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def fixture_groups(rows):
    groups = {}
    for row in rows:
        if valid_digest(row.get('fixture_pcm_sha256')):
            groups.setdefault(row['fixture_pcm_sha256'], []).append(row)
    return groups


def fixture_conflicts(rows, *, label=lambda row: row['expected_keyword']):
    return [
        {'pcm_sha256': digest, 'cases': [row['case'] for row in group]}
        for digest, group in fixture_groups(rows).items()
        if len({label(row) for row in group}) > 1
    ]


def evaluate(model_root, output):
    # Explicit execution only. Importing this module or printing a plan never
    # imports a native model, reads credentials, or starts device capture.
    corpus_hash = hashlib.sha256(
        json.dumps([CASES, PREFIX_SAMPLES], ensure_ascii=False).encode()
    ).hexdigest()
    source_hash = None
    automation_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    speech_source_hash = hashlib.sha256(Path(voice_source).read_bytes()).hexdigest()
    native_versions = {}
    native_hashes = {}
    rows, fixtures = [], {}
    model_loaded = False
    integrity = {'state': 'not_checked', 'conflicts': [], 'phonetic_correctness_asserted': False}

    def save(state):
        output.write(
            {
                'schema': 2,
                'state': state,
                'scope': 'public_synthetic_keyword_boundaries',
                'corpus_version': CORPUS_VERSION,
                'automation_source_sha256': automation_hash,
                'speech_probe_source_sha256': speech_source_hash,
                'source_sha256': source_hash,
                'corpus_sha256': corpus_hash,
                'native_package_versions': native_versions,
                'native_binary_sha256': native_hashes,
                'model_loaded': model_loaded,
                'input_integrity': integrity,
                'microphone_used': False,
                'device_used': False,
                'network_requested': False,
                'raw_audio_retained': False,
                'human_accuracy_asserted': False,
                'samples': rows,
                'summary': summary(rows),
            }
        )

    save('preparing_fixtures')
    try:
        # Finish input validation before allocating native models or admitting
        # any score. Opposing labels on identical PCM cannot test a detector.
        for ident, voice, rate, text, positive in CASES:
            row = {'case': ident, 'expected_keyword': positive}
            try:
                if type(positive) is not bool:
                    raise ValueError('Invalid fixture label')
                row['voice_selection'] = 'system_default' if voice is None else 'installed_name'
                row['requested_voice'] = voice
                row['resolved_voice'] = resolve_voice(voice)
                with tempfile.TemporaryDirectory(prefix='cc8-public-wake-fixture-') as temporary:
                    pcm = synthesize(Path(temporary), voice, rate, text)
                if not isinstance(pcm, bytes) or not 0 < len(pcm) <= 320000 or len(pcm) % 2:
                    raise ValueError('Invalid fixture PCM')
                row['fixture_pcm_sha256'] = hashlib.sha256(pcm).hexdigest()
                fixtures[ident] = pcm
                row['state'] = 'prepared'
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                row['state'] = 'fixture_failed'
            rows.append(row)
            save('preparing_fixtures')
        conflicts = fixture_conflicts(rows)
        invalid = len(fixtures) != len(CASES) or bool(conflicts)
        integrity.update(
            state='invalid_fixtures' if invalid else 'verified_exact_pcm_labels',
            conflicts=conflicts,
            same_label_duplicate_groups=sum(
                len(group) > 1 and len({r['expected_keyword'] for r in group}) == 1
                for group in fixture_groups(rows).values()
            ),
        )
        if invalid:
            save('input_invalid')
            return 'input_invalid', summary(rows)
        try:
            import numpy as np
            from sherpa_onnx.lib import _sherpa_onnx

            if __package__:
                from .cc8 import verified_models
            else:
                from cc8 import verified_models
            from local_wake import EnglishWake
            from local_wake import __file__ as wake_source
            from streaming_keywords import StreamingKeywords

            source_hash = hashlib.sha256(Path(wake_source).read_bytes()).hexdigest()
            if not verified_models(model_root, 'minimax'):
                raise ValueError('Unverified model resources')
            detector = EnglishWake.load(
                model_root / '.toolchains/cc8/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01'
            )
            model_loaded = True

            class ColdReference(StreamingKeywords):
                KEYWORDS = EnglishWake.KEYWORDS

            cold = ColdReference(detector.detector, gain=detector.gain, lock=detector.engine_lock)
            for name in ('sherpa-onnx', 'sherpa-onnx-core'):
                try:
                    native_versions[name] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    native_versions[name] = None
            binary = Path(_sherpa_onnx.__file__)
            native_files = [binary, *binary.parent.glob('*.dylib')]
            native_hashes = {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in native_files
            }
        except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError):
            save('model_unavailable')
            raise
        save('running')
        for row in rows:
            try:
                pcm = fixtures[row['case']]
                samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
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
            save('running')
    except KeyboardInterrupt:
        save('cancelled')
        raise
    finally:
        fixtures.clear()
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
                    'corpus_version': CORPUS_VERSION,
                    'sample_count': len(CASES),
                    'conditions': CONDITIONS,
                    'fixture_integrity': 'exact_pcm_labels_before_model_load',
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
