"""Explicit public-synthetic MiniMax ASR measurements; no microphone or tasks."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from asr_evaluation import (
    CORPUS,
    CATEGORIES,
    MAX_EDIT_CELLS,
    corpus_samples,
    edit_counts,
    han,
    normalized,
    read_json,
    words,
)
from mac_voice import resolve_voice, synthesize, __file__ as voice_source
from diagnostics import percentiles
from private_output import PrivateOutput, private_directory
from release_info import load_release

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_FILES = (
    'recognition.py',
    'minimax_client.py',
    'configuration.py',
    'config_schema.py',
    'http_budget.py',
)


def select_samples(partition):
    samples = list(corpus_samples(read_json(CORPUS)).values())
    if Counter(r['partition'] for r in samples) != {'calibration': 15, 'acceptance': 60}:
        raise ValueError('Incomplete fixed ASR corpus')
    expected = {f'{category}-{index:02}' for category in CATEGORIES for index in range(1, 16)}
    if {r['id'] for r in samples} != expected or any(
        r['category'] != r['id'].rsplit('-', 1)[0]
        or r['partition'] != ('calibration' if int(r['id'][-2:]) <= 3 else 'acceptance')
        for r in samples
    ):
        raise ValueError('Fixed ASR corpus identity or partition changed')
    if partition not in ('calibration', 'acceptance', 'all'):
        raise ValueError('Invalid partition')
    return [r for r in samples if partition == 'all' or r['partition'] == partition]


def pipeline(runtime):
    # Only configured MiniMax recognition is constructed. No bridge, brain,
    # camera, input device, local speech model or TTS provider is started.
    from configuration import load_config
    from recognition import MandarinRecognizer
    import numpy as np

    config = load_config(root=runtime)
    if config.asr != 'minimax':
        raise ValueError('Synthetic API check requires selected MiniMax ASR')
    hashes = {}
    for name in PIPELINE_FILES:
        current = ROOT / 'cc8-assistant/host' / name
        actual = runtime / 'cc8-assistant/host' / name
        digest = hashlib.sha256(current.read_bytes()).hexdigest()
        if hashlib.sha256(actual.read_bytes()).hexdigest() != digest:
            raise ValueError('ASR source differs; run from matching source checkout')
        hashes[name] = digest
    recognizer = MandarinRecognizer(config=config)

    def recognize(pcm):
        return recognizer.recognize(np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768)

    return recognize, recognizer.cancel, {'asr_model': config.asr_model, 'pipeline_sha256': hashes}


def totals(rows):
    complete = [r for r in rows if r.get('outcome') == 'recognized']
    result = {
        'measured_samples': len(complete),
        'normalized_exact_matches': sum(r['normalized_exact'] for r in complete),
        'outcomes': dict(Counter(r['outcome'] for r in rows)),
        # Same linear interpolation as pipeline diagnostics. This excludes
        # synthesis time and is not an acoustic/end-to-end response latency.
        'recognition_ms': percentiles([r['recognition_seconds'] * 1000 for r in complete]),
    }
    for kind in ('han_cer', 'english_wer'):
        counts = {
            key: sum(r[kind][key] for r in complete)
            for key in ('reference_units', 'errors', 'substitutions', 'deletions', 'insertions')
        }
        result[kind] = {
            **counts,
            'rate': counts['errors'] / counts['reference_units']
            if counts['reference_units']
            else None,
        }
    return result


def selected_digest(samples):
    return hashlib.sha256(
        json.dumps(samples, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def fixture_digest(pcm):
    if type(pcm) is not bytes or not pcm or len(pcm) % 2 or len(pcm) > 320000 or not any(pcm):
        raise ValueError('Invalid public synthetic PCM')
    return hashlib.sha256(pcm).hexdigest()


def reference_fixtures(path, samples, synthesize_pcm):
    report = read_json(path, private=True)
    expected = {item['id']: (item['category'], item['partition']) for item in samples}
    if (
        not isinstance(report, dict)
        or type(report.get('schema')) is not int
        or report['schema'] != 1
        or report.get('scope') != 'public_synthetic_asr_api'
        or report.get('corpus_sha256') != hashlib.sha256(CORPUS.read_bytes()).hexdigest()
        or report.get('selected_samples_sha256') != selected_digest(samples)
        or not isinstance(report.get('samples'), list)
        or len(report['samples']) != len(expected)
    ):
        raise ValueError('Reference ASR report does not cover the selected corpus')

    hashes = {}
    for row in report['samples']:
        sample_id = row.get('id') if isinstance(row, dict) else None
        if not isinstance(sample_id, str) or sample_id not in expected or sample_id in hashes:
            raise ValueError('Invalid reference sample ID')
        digest = row.get('fixture_pcm_sha256')
        if (row.get('category'), row.get('partition')) != expected[sample_id] or not (
            isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)
        ):
            raise ValueError('Invalid reference fixture')
        hashes[sample_id] = digest

    # Keep the checked bytes in memory so recognition consumes exactly what
    # was verified. The fixed corpus and per-clip bound cap this at 24 MB.
    verified = {}
    for item in samples:
        started = time.monotonic()
        pcm = synthesize_pcm(item['reference'])
        if fixture_digest(pcm) != hashes[item['id']]:
            raise ValueError('Synthetic PCM differs from reference')
        verified[item['id']] = (pcm, round(time.monotonic() - started, 3))
    return verified


class SyntheticRun:
    def __init__(
        self,
        samples,
        recognize,
        synthesize_pcm,
        *,
        verified_fixtures=None,
        clock=time.monotonic,
        started=None,
        checkpoint=lambda value: None,
    ):
        self.samples, self.recognize, self.synthesize = samples, recognize, synthesize_pcm
        self.clock, self.checkpoint = clock, checkpoint
        self.verified_fixtures = verified_fixtures
        self.started = clock() if started is None else started
        self.rows = []
        self.source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.voice_hash = hashlib.sha256(Path(voice_source).read_bytes()).hexdigest()
        self.corpus_hash = hashlib.sha256(CORPUS.read_bytes()).hexdigest()
        self.selected_hash = selected_digest(samples)

    def report(self, state):
        return {
            'schema': 1,
            'state': state,
            'scope': 'public_synthetic_asr_api',
            'selected_samples': len(self.samples),
            'processed_samples': len(self.rows),
            'remaining_samples': len(self.samples) - len(self.rows),
            'source_sha256': self.source_hash,
            'speech_probe_source_sha256': self.voice_hash,
            'corpus_sha256': self.corpus_hash,
            'selected_samples_sha256': self.selected_hash,
            'reference_fixtures_verified': self.verified_fixtures is not None,
            'elapsed_seconds': round(self.clock() - self.started, 3),
            'delivery_realized': 'normal_mac_synthetic',
            'critical_fields_assessed': False,
            'intent_assessed': False,
            'distance_assessed': False,
            'television_assessed': False,
            'raw_audio_retained': False,
            'contains_transcript': False,
            'microphone_used': False,
            'device_used': False,
            'full_p0_acceptance': False,
            'samples': list(self.rows),
            'summary': totals(self.rows),
            'by_partition': {
                p: totals([r for r in self.rows if r['partition'] == p])
                for p in ('calibration', 'acceptance')
            },
        }

    def run(self):
        consecutive_failures, cells = 0, 0
        state = 'running'
        self.checkpoint(self.report(state))
        try:
            for item in self.samples:
                row = {key: item[key] for key in ('id', 'category', 'partition')}
                row.update(outcome='pending', requested_delivery=item['delivery'])
                self.rows.append(row)
                phase = 'fixture'
                started = self.clock()
                earlier_synthesis_seconds = 0
                try:
                    if self.verified_fixtures is None:
                        pcm = self.synthesize(item['reference'])
                        synthesis_seconds = round(self.clock() - started, 3)
                    else:
                        pcm, synthesis_seconds = self.verified_fixtures[item['id']]
                        earlier_synthesis_seconds = synthesis_seconds
                    row['fixture_pcm_sha256'] = fixture_digest(pcm)
                    row['synthesis_seconds'] = synthesis_seconds
                    phase = 'recognition'
                    began = self.clock()
                    transcript = self.recognize(pcm)
                    row['recognition_seconds'] = round(self.clock() - began, 3)
                    if not isinstance(transcript, str) or len(transcript) > 1000:
                        raise ValueError('Invalid ASR transcript')
                    phase = 'scoring'
                    ref_han, hyp_han = han(item['reference']), han(transcript)
                    ref_words, hyp_words = words(item['reference']), words(transcript)
                    cells += len(ref_han) * len(hyp_han) + len(ref_words) * len(hyp_words)
                    if cells > MAX_EDIT_CELLS:
                        row['outcome'] = 'scoring_budget_exceeded'
                        break
                    row.update(
                        outcome='recognized',
                        normalized_exact=normalized(item['reference']) == normalized(transcript),
                        han_cer=edit_counts(ref_han, hyp_han),
                        english_wer=edit_counts(ref_words, hyp_words),
                    )
                    transcript = None
                    consecutive_failures = 0
                except KeyboardInterrupt:
                    row['outcome'] = 'cancelled'
                    raise
                except Exception:
                    # Provider bodies, URLs and transcript values never enter
                    # reports. A fixture failure is not blamed on the API.
                    row['outcome'] = phase + '_failed'
                    consecutive_failures += 1
                finally:
                    row['total_seconds'] = round(
                        self.clock() - started + earlier_synthesis_seconds, 3
                    )
                self.checkpoint(self.report(state))
                if consecutive_failures >= 3:
                    break
            state = (
                'completed'
                if len(self.rows) == len(self.samples)
                and all(r['outcome'] == 'recognized' for r in self.rows)
                else 'partial'
            )
        except KeyboardInterrupt:
            state = 'cancelled'
        except Exception:
            state = 'failed'
            raise
        finally:
            self.checkpoint(self.report(state))
        return self.report(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument(
        '--partition', choices=('calibration', 'acceptance', 'all'), default='calibration'
    )
    parser.add_argument('--execute', action='store_true')
    parser.add_argument(
        '--reference-report', type=Path, help='Require identical PCM to a private earlier report'
    )
    args = parser.parse_args(argv)
    try:
        samples = select_samples(args.partition)
        if not args.execute:
            print(
                json.dumps(
                    {
                        'execute': False,
                        'sample_count': len(samples),
                        'partition': args.partition,
                        'verify_reference_fixtures_when_executed': args.reference_report
                        is not None,
                        'microphone_used': False,
                        'network_target_when_executed': 'configured_minimax_asr',
                        'full_p0_acceptance': False,
                    }
                )
            )
            return 0
        runtime = args.runtime_root
        if (
            platform.system() != 'Darwin'
            or not runtime.is_absolute()
            or runtime.resolve() != runtime
        ):
            raise ValueError('Real Mac runtime root required')
        resolved = resolve_voice('Tingting')
        release = load_release(runtime)
        private = private_directory(runtime / 'device-access/private/voice-runtime')
        folder = private_directory(
            private_directory(ROOT / 'device-access/private') / 'synthetic-asr'
        )
        fd = os.open(
            private / 'automation-test.lock',
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        with os.fdopen(fd, 'r+') as lock:
            info = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ValueError('Unsafe automation lock')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def fixture(text):
                with tempfile.TemporaryDirectory(prefix='cc8-public-asr-') as temporary:
                    return synthesize(Path(temporary), resolved, 185, text)

            verified = None
            started = None
            if args.reference_report is not None:
                started = time.monotonic()
                try:
                    verified = reference_fixtures(args.reference_report, samples, fixture)
                except (OSError, ValueError, subprocess.SubprocessError):
                    print('无法核对基准报告与全部合成 PCM，未调用识别 API。', file=sys.stderr)
                    return 2
            recognize, cancel, metadata = pipeline(runtime)
            try:
                name = (
                    'api-'
                    + args.partition
                    + '-'
                    + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
                    + uuid.uuid4().hex[:8]
                    + '.json'
                )
                with PrivateOutput(folder / name) as output:

                    def save(value):
                        output.write(
                            {
                                **value,
                                **metadata,
                                'resolved_voice': resolved,
                                'rate': 185,
                                'runtime_source_backend_version': release.backend_version,
                                'network_target': 'configured_official_minimax_asr',
                            }
                        )

                    result = SyntheticRun(
                        samples,
                        recognize,
                        fixture,
                        verified_fixtures=verified,
                        started=started,
                        checkpoint=save,
                    ).run()
                    print(json.dumps({'state': result['state'], 'summary': result['summary']}))
                    print(output.path)
                    return (
                        0
                        if result['state'] == 'completed'
                        else 130
                        if result['state'] == 'cancelled'
                        else 2
                    )
            finally:
                cancel()
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError):
        print('合成ASR评测未完成；请核对已安装音色、匹配的源码和现有MiniMax配置。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
