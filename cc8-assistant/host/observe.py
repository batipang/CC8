"""Read-only health/resource observation up to 72 hours; never starts capture."""

import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import sys
import time
import uuid
from diagnose import PRIVATE, ROOT, query
from observation_run import ObservationRun
from private_output import PrivateOutput, private_directory


def validate(seconds, interval):
    if (
        type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0.05 <= seconds <= 72 * 3600
        or type(interval) not in (int, float)
        or not math.isfinite(interval)
        or not 0.05 <= interval <= 300
        or math.ceil(seconds / interval) + 1 > 6000
    ):
        raise ValueError('Invalid observation duration/interval or sample capacity')


def default_output():
    folder = private_directory(private_directory(ROOT / 'device-access/private') / 'observations')
    return folder / (
        'observe-'
        + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
        + uuid.uuid4().hex[:8]
        + '.json'
    )


def run(args, *, query_fn=query, clock=time.monotonic, wall_clock=time.time, sleep=time.sleep):
    validate(args.seconds, args.interval)
    expected_backend = getattr(args, 'expect_backend', None)
    if expected_backend is not None and (
        not isinstance(expected_backend, str)
        or len(expected_backend) > 100
        or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', expected_backend)
    ):
        raise ValueError('Expected backend must be a bounded canonical version')
    book = ObservationRun(args.interval)
    started, wall_started = clock(), wall_clock()
    deadline = started + args.seconds
    checkpoint = started + 300
    state = 'running'
    with PrivateOutput(args.output if args.output is not None else default_output()) as output:

        def save():
            result = book.report()
            result['observation'] = {
                'state': state,
                'requested_seconds': args.seconds,
                'interval_seconds': args.interval,
                'elapsed_seconds': round(max(0, clock() - started), 3),
                'wall_elapsed_seconds': round(wall_clock() - wall_started, 3),
                'checkpoint_seconds': 300,
                'expected_backend_version': expected_backend,
            }
            output.write(result)

        save()
        try:
            while deadline - clock() >= 0.01:
                payload, error = None, None
                try:
                    payload = query_fn(
                        args.socket,
                        operation='health',
                        timeout=min(5, max(0.001, deadline - clock())),
                    )
                    if expected_backend is not None and (
                        not isinstance(payload, dict)
                        or payload.get('bridge_version') != expected_backend
                    ):
                        payload, error = None, 'backend_version_mismatch'
                except OSError:
                    error = 'ipc_unavailable'
                except (ValueError, TypeError, RecursionError):
                    error = 'invalid_health'
                elapsed, wall_elapsed = clock() - started, wall_clock() - wall_started
                try:
                    book.add(payload, elapsed, wall_elapsed, error=error)
                except (ValueError, TypeError, RecursionError):
                    # Malformed health data is a recorded gap, not successful uptime.
                    book.add(None, elapsed, wall_elapsed, error='invalid_health')
                if clock() >= checkpoint:
                    save()
                    checkpoint = clock() + 300
                sleep(min(args.interval, max(0, deadline - clock())))
            report = book.report()
            state = (
                'completed'
                if report['successful_polls']
                and not any(
                    report[key]
                    for key in (
                        'errors',
                        'resource_coverage_incomplete_polls',
                        'poll_gaps',
                        'wall_clock_discontinuities',
                        'uptime_discontinuities',
                        'unhealthy_polls',
                    )
                )
                else 'partial'
            )
        except KeyboardInterrupt:
            state = 'cancelled'
        except Exception:
            state = 'failed'
            raise
        finally:
            save()
        print(output.path)
    return 0 if state == 'completed' else 130 if state == 'cancelled' else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=72 * 3600)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--socket', type=Path, default=PRIVATE / 'announce.sock')
    parser.add_argument('--output', type=Path)
    parser.add_argument(
        '--expect-backend', help='Require this backend version on every health poll'
    )
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, TypeError, RecursionError):
        print(
            'Observation failed; check bounded options, IPC and private output permissions. No provider data shown.',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
