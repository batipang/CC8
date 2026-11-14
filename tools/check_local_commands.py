"""Public synthetic Chinese wake/stop regression; no capture, playback or cloud."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import uuid

if __package__:
    from .private_output import PrivateOutput, private_directory
    from . import check_wake
else:
    from private_output import PrivateOutput, private_directory
    import check_wake

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cc8-assistant/host'))
from mac_voice import resolve_voice, synthesize

ROOT = Path(__file__).resolve().parents[1]
CORPUS_VERSION = 'chinese-wake-and-stop-v1'
PREFIX_SAMPLES = {f'start_{ms}ms': ms * 16 for ms in (0, 60, 140, 160, 480, 1000)}
CONDITIONS = tuple(PREFIX_SAMPLES)
# Predeclare the full matrix, including near words and mixed wake/stop inputs.
# Do not remove a case or reinterpret a label after looking at model results.
CASES = tuple(
    (f'{voice.lower()}-{rate}-{index}', voice, rate, text, wake, stop)
    for voice in ('Tingting', 'Meijia')
    for rate in (140, 170, 200)
    for index, (text, wake, stop) in enumerate(
        (
            ('大黄蜂', True, ''),
            ('大黄蜂，现在几点了？', True, ''),
            ('停一下', False, '停一下'),
            ('停止', False, '停止'),
            ('暂停', False, '暂停'),
            ('大黄蜂，停一下', True, '停一下'),
        )
    )
) + tuple(
    (f'negative-{index:02}', 'Tingting', 170, text, False, '')
    for index, text in enumerate(
        (
            '大黄瓜',
            '大黄粉',
            '打黄蜂',
            '挺一下',
            '通知',
            '餐厅',
            '今天的天气很好',
            '帮我把灯打开',
        )
    )
)


def expected(case):
    return {'wake': ['大黄蜂'] if case[4] else [], 'stop': [case[5]] if case[5] else []}


def fixture_conflicts(rows):
    def label(row):
        return tuple(row['expected']['wake']), tuple(row['expected']['stop'])

    return check_wake.fixture_conflicts(rows, label=label)


def passed(rows):
    labels = {c[0]: expected(c) for c in CASES}
    return (
        len(rows) == len(CASES)
        and {r.get('case') for r in rows} == set(labels)
        and all(
            row.get('state') == 'measured'
            and check_wake.valid_digest(row.get('fixture_pcm_sha256'))
            and row.get('expected') == labels[row['case']]
            and all(row.get(condition) == labels[row['case']] for condition in CONDITIONS)
            for row in rows
        )
        and not fixture_conflicts(rows)
    )


def summary(rows):
    result = {}
    for condition in CONDITIONS:
        complete = [row for row in rows if row.get('state') == 'measured' and condition in row]
        result[condition] = {
            'measured': len(complete),
            'exact': sum(row[condition] == row['expected'] for row in complete),
            'wake_misses': sum(
                bool(row['expected']['wake']) and not row[condition]['wake'] for row in complete
            ),
            'wake_false_hits': sum(
                not row['expected']['wake'] and bool(row[condition]['wake']) for row in complete
            ),
            'stop_misses': sum(
                bool(row['expected']['stop']) and row[condition]['stop'] != row['expected']['stop']
                for row in complete
            ),
            'stop_false_hits': sum(
                not row['expected']['stop'] and bool(row[condition]['stop']) for row in complete
            ),
        }
    return result


def load_detectors(model_root):
    from fast_stop import FastStopDetector
    from local_wake import ChineseWake

    model = model_root / '.toolchains/cc8/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
    stop = FastStopDetector(model=model)
    wake = ChineseWake(stop.detector, lock=stop.engine_lock)
    return {'wake': wake, 'stop': stop}


def model_identity(model_root):
    """Bind the supplied cache's manifests to this reviewed source checkout."""
    result = {}
    for name in ('fast-stop-model.json', 'english-wake-model.json'):
        relative = Path('cc8-assistant/host/resources') / name
        expected_data = (ROOT / relative).read_bytes()
        if (model_root / relative).read_bytes() != expected_data:
            raise ValueError('Model manifest differs from reviewed source')
        result[name] = hashlib.sha256(expected_data).hexdigest()
    return result


def fingerprint():
    from sherpa_onnx.lib import _sherpa_onnx

    binary = Path(_sherpa_onnx.__file__)
    versions = {}
    for name in ('sherpa-onnx', 'sherpa-onnx-core'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        'native_package_versions': versions,
        'native_binary_sha256': {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (binary, *sorted(binary.parent.glob('*.dylib')))
        },
    }


def measure(detectors, samples, prefix):
    import numpy as np

    for detector in detectors.values():
        detector.reset()
    # Include the same two-second flush used by the English digital probe.
    audio = np.concatenate(
        [np.zeros(prefix, dtype=np.float32), samples, np.zeros(32000, dtype=np.float32)]
    )
    hits = {name: set() for name in detectors}
    for index in range(0, len(audio), 320):
        frame = audio[index : index + 320]
        for name, detector in detectors.items():
            keyword = detector.feed(frame)
            if keyword:
                hits[name].add(keyword)
    return {name: sorted(values) for name, values in hits.items()}


def evaluate(model_root, output):
    if __package__:
        from .cc8 import verified_models
    else:
        from cc8 import verified_models
    model_hashes = model_identity(model_root)
    if not verified_models(model_root, 'minimax'):
        raise ValueError('Unverified model resources')
    # Resolve every named voice before running any fixture. Missing voices fail,
    # rather than letting say silently substitute the system default.
    voices = {voice: resolve_voice(voice) for voice in {c[1] for c in CASES}}
    provenance = {'model_manifest_sha256': model_hashes}
    source_files = (
        Path(__file__),
        Path(check_wake.__file__),
        *(
            ROOT / 'cc8-assistant/host' / name
            for name in (
                'fast_stop.py',
                'local_wake.py',
                'streaming_keywords.py',
                'mac_voice.py',
                'resources/stop-keywords.txt',
                'resources/wake-chinese-keywords.txt',
            )
        ),
    )
    provenance['source_sha256'] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files
    }
    provenance['corpus_sha256'] = hashlib.sha256(
        json.dumps([CASES, PREFIX_SAMPLES], ensure_ascii=False).encode()
    ).hexdigest()
    rows, fixtures = [], {}
    integrity = {'state': 'not_checked', 'conflicts': []}
    model_loaded = False

    def save(state):
        output.write(
            {
                'schema': 2,
                'state': state,
                'scope': 'public_synthetic_chinese_keyword_boundaries',
                'corpus_version': CORPUS_VERSION,
                **provenance,
                'model_loaded': model_loaded,
                'input_integrity': integrity,
                'microphone_used': False,
                'camera_used': False,
                'device_used': False,
                'network_requested': False,
                'audio_played': False,
                'raw_audio_retained': False,
                'runtime_changed': False,
                'human_accuracy_asserted': False,
                'full_p0_acceptance': False,
                'samples': rows,
                'summary': summary(rows),
            }
        )

    save('preparing_fixtures')
    try:
        for case in CASES:
            ident, voice, rate, text, _, _ = case
            row = {'case': ident, 'expected': expected(case), 'resolved_voice': voices[voice]}
            try:
                with tempfile.TemporaryDirectory(prefix='cc8-public-chinese-') as temporary:
                    pcm = synthesize(Path(temporary), voices[voice], rate, text)
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
        )
        if invalid:
            save('input_invalid')
            return 'input_invalid', summary(rows)

        import numpy as np

        detectors = load_detectors(model_root)
        model_loaded = True
        provenance.update(fingerprint())
        for row in rows:
            samples = np.frombuffer(fixtures[row['case']], dtype='<i2').astype(np.float32) / 32768
            try:
                for condition in CONDITIONS:
                    row[condition] = measure(detectors, samples, PREFIX_SAMPLES[condition])
                row['state'] = 'measured'
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                row['state'] = 'fixture_or_model_failed'
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
                    'corpus_version': CORPUS_VERSION,
                    'sample_count': len(CASES),
                    'conditions': CONDITIONS,
                    'microphone_used': False,
                    'network_requested': False,
                    'audio_played': False,
                }
            )
        )
        return 0
    check_wake.require_evaluation_python(parser)
    try:
        if platform.system() != 'Darwin':
            raise ValueError('Mac synthesizer required')
        if not args.model_root.is_absolute() or args.model_root.resolve() != args.model_root:
            raise ValueError('Real absolute model root required')
        folder = private_directory(
            private_directory(ROOT / 'device-access/private') / 'wake-checks'
        )
        name = (
            'chinese-'
            + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
            + uuid.uuid4().hex[:8]
            + '.json'
        )
        with PrivateOutput(folder / name) as output:
            try:
                state, counts = evaluate(args.model_root, output)
            except (
                OSError,
                ValueError,
                RuntimeError,
                ImportError,
                subprocess.SubprocessError,
                tarfile.TarError,
            ):
                output.write(
                    {
                        'schema': 1,
                        'state': 'aborted',
                        'scope': 'public_synthetic_chinese_keyword_boundaries',
                        'microphone_used': False,
                        'camera_used': False,
                        'device_used': False,
                        'network_requested': False,
                        'audio_played': False,
                        'runtime_changed': False,
                        'full_p0_acceptance': False,
                    }
                )
                raise
            print(json.dumps({'state': state, 'summary': counts}, ensure_ascii=False))
            print(output.path)
            return 0 if state == 'passed' else 2
    except KeyboardInterrupt:
        print('中文本地语音检查已取消。', file=sys.stderr)
        return 130
    except (
        OSError,
        ValueError,
        RuntimeError,
        ImportError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ):
        print(
            '中文本地语音检查未完成；请核对隔离评测 Python、固定模型和已安装音色。', file=sys.stderr
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
