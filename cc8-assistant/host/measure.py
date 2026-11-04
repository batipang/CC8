"""Collect bounded content-free diagnostic receipts; never opens device capture."""

import argparse
from datetime import datetime, timezone
import math
import re
import sys
from pathlib import Path
import time
import uuid
from diagnose import PRIVATE, ROOT, query
from diagnostic_run import DiagnosticRun, RunCapacityError
from private_output import PrivateOutput, private_directory

CONDITIONS = ('unspecified', 'quiet', 'tv')
DISTANCES = ('unspecified', '1m', '3m')
PARTITIONS = ('unspecified', 'calibration', 'acceptance')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


def valid_version(value):
    return isinstance(value, str) and len(value) <= 100 and VERSION.fullmatch(value) is not None


def diagnostic_source(payload):
    if not isinstance(payload, dict):
        raise ValueError('Invalid diagnostic snapshot')
    health = payload.get('health', {})
    if not isinstance(health, dict):
        raise ValueError('Invalid diagnostic health metadata')
    version = health.get('bridge_version')
    instance = health.get('runtime_instance')
    if version is not None and not valid_version(version):
        raise ValueError('Invalid diagnostic backend version')
    if instance is not None and (
        not isinstance(instance, str) or not re.fullmatch('[a-f0-9]{32}', instance)
    ):
        raise ValueError('Invalid diagnostic runtime identity')
    return version, instance


def validate(args):
    for value, minimum, maximum in ((args.seconds, 0.05, 3600), (args.interval, 0.05, 60)):
        if (
            type(value) not in (int, float)
            or not minimum <= value <= maximum
            or not math.isfinite(value)
        ):
            raise ValueError('Invalid measurement duration or interval')
    if (
        args.condition not in CONDITIONS
        or args.distance not in DISTANCES
        or args.partition not in PARTITIONS
    ):
        raise ValueError('Invalid measurement labels')


def default_output():
    private = private_directory(ROOT / 'device-access/private')
    folder = private_directory(private / 'diagnostic-runs')
    return folder / (
        'run-'
        + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
        + uuid.uuid4().hex[:8]
        + '.json'
    )


def run(args, *, query_fn=query, clock=time.monotonic, sleep=time.sleep):
    validate(args)
    expected_backend = getattr(args, 'expect_backend', None)
    if expected_backend is not None and not valid_version(expected_backend):
        raise ValueError('Expected backend must be a bounded canonical version')
    source = None
    path = args.output if args.output is not None else default_output()
    book = DiagnosticRun()
    started = clock()
    deadline = started + args.seconds
    next_checkpoint = started + 30
    state = 'running'
    failures = consecutive = 0
    with PrivateOutput(path) as output:

        def save():
            result = book.report()
            result['capture'] = {
                'state': state,
                'elapsed_seconds': round(max(0, clock() - started), 3),
                'requested_seconds': args.seconds,
                'query_failures': failures,
                'condition': args.condition,
                'distance': args.distance,
                'partition': args.partition,
                'labels_are_operator_supplied': True,
                'device_capture_started': False,
                'expected_backend_version': expected_backend,
                'backend_version': source[0] if source else None,
                'runtime_bound': bool(source and all(source)),
            }
            output.write(result)

        save()
        try:
            while deadline - clock() >= 0.01:
                try:
                    payload = query_fn(
                        args.socket,
                        operation='diagnostics',
                        timeout=min(5, max(0.001, deadline - clock())),
                    )
                    current = diagnostic_source(payload)
                    if source is not None and current != source:
                        state = 'runtime_changed'
                        break
                    if expected_backend is not None:
                        if not all(current):
                            state = 'runtime_unconfirmed'
                            break
                        if current[0] != expected_backend:
                            state = 'backend_version_mismatch'
                            break
                    book.ingest(payload)
                    source = current
                    if book.polls == 1:
                        print(
                            '诊断采集已就绪，请开始本轮测试；不会开启设备采集。',
                            file=sys.stderr,
                            flush=True,
                        )
                    consecutive = 0
                except RunCapacityError:
                    state = 'capacity_reached'
                    break
                except OSError:
                    failures += 1
                    consecutive += 1
                    if consecutive >= 3:
                        state = 'ipc_unavailable'
                        break
                except (ValueError, TypeError, RecursionError):
                    state = 'invalid_snapshot'
                    break
                if clock() >= next_checkpoint:
                    save()
                    next_checkpoint = clock() + 30
                sleep(min(args.interval, max(0, deadline - clock())))
            else:
                state = (
                    'completed'
                    if book.polls and not failures
                    else 'partial'
                    if book.polls
                    else 'ipc_unavailable'
                )
        except KeyboardInterrupt:
            state = 'cancelled'
        finally:
            save()
        print(output.path)
    return 0 if state == 'completed' else 130 if state == 'cancelled' else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--seconds',
        type=float,
        default=60,
        help='Read-only collection window, at most 3600 seconds',
    )
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--socket', type=Path, default=PRIVATE / 'announce.sock')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--expect-backend', help='Require this version and one runtime instance')
    parser.add_argument('--condition', choices=CONDITIONS, default='unspecified')
    parser.add_argument('--distance', choices=DISTANCES, default='unspecified')
    parser.add_argument('--partition', choices=PARTITIONS, default='unspecified')
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError):
        parser.exit(2, '诊断采集未完成，请检查后台、参数及本机私有输出目录；未打开设备采集。\n')


if __name__ == '__main__':
    raise SystemExit(main())
