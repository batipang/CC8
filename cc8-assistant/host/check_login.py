"""Explicitly arm one passive check after the next real GUI login; default only plans."""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from login_receipt import report, require


def arm(root):
    from login_probe import LoginProbe, observer_supported
    from login_check_store import LoginCheckStore

    probe = LoginProbe(root)
    require(observer_supported(root), 'login_runtime_observer_unsupported')
    ident = uuid.uuid4().hex
    session = probe.session(ident)
    identity = probe.identity()
    sample = probe.sample(ident)
    require(
        sample is not None and sample['idle'] is True and sample['requests_idle'] is True,
        'login_baseline_not_idle_ready',
    )
    require(
        probe.session(ident) == session and probe.identity() == identity, 'login_baseline_changed'
    )
    now = time.time()
    value = dict(
        schema=1,
        id=ident,
        state='armed',
        armed_at=now,
        expires_at=now + 7 * 86400,
        scope='new_gui_login_backend_readiness',
        baseline=dict(
            session=session,
            identity=identity,
            instance=sample['instance'],
            process=sample['process'],
            activity=sample['activity'],
        ),
        new_gui_login_observed=False,
        backend_ready=False,
        full_p0_acceptance=False,
        automatic_launch_cause_verified=False,
        media_started=False,
        tasks_replayed=False,
    )
    LoginCheckStore(probe.private).arm(value)
    return report(value)


def execute(root, action):
    if action == 'arm':
        return arm(root)
    from login_check_store import LoginCheckStore

    require(root.is_absolute() and root.resolve() == root, 'login_root_not_real_absolute')
    store = LoginCheckStore(root / 'device-access/private/voice-runtime')
    value = store.load()
    if value is None:
        return dict(state='not_armed', full_p0_acceptance=False)
    if action == 'cancel' and value['state'] in ('armed', 'observing'):
        previous = value['state']
        value.update(state='interrupted', reason='owner_cancelled_login_check')
        require(store.replace(value['id'], value, expected=previous), 'login_check_replaced')
    if (
        action == 'status'
        and value['state'] in ('armed', 'observing')
        and not value['armed_at'] <= time.time() < value['expires_at']
    ):
        previous = value['state']
        value.update(state='expired', reason='login_check_expired')
        require(store.replace(value['id'], value, expected=previous), 'login_check_replaced')
    return report(value)


def start_login_check(root, private, stop, logger):
    try:
        return _start_login_check(root, private, stop, logger)
    except Exception:
        # Creating optional diagnostic infrastructure must not stop recovery.
        try:
            logger.warning('login_check_observer_unconfirmed')
        except Exception:
            pass
        return None


def _start_login_check(root, private, stop, logger):
    # Default off: no new directory, OS command, socket, worker or report.
    if not (private / 'login-check.json').exists():
        return None
    import os
    import threading
    from login_check_store import LoginCheckStore
    from login_probe import LoginProbe
    from login_receipt import LoginRecoveryRun

    pending = LoginCheckStore(private).load()
    if pending is None or pending['state'] != 'armed':
        return None

    class Observer:
        def __init__(self):
            self.closed = threading.Event()
            self.thread = threading.Thread(target=self.run, daemon=True, name='cc8-login-check')

        def is_set(self):
            return stop.is_set() or self.closed.is_set()

        def run(self):
            try:
                LoginRecoveryRun(
                    LoginCheckStore(private),
                    LoginProbe(root, supervisor_pid=os.getpid()),
                    self,
                    wait=self.closed.wait,
                ).run()
            except Exception:
                # Optional diagnostics cannot impair supervision or log raw
                # launchd output, owner configuration or socket replies.
                try:
                    logger.warning('login_check_observer_unconfirmed')
                except Exception:
                    pass

        def close(self):
            self.closed.set()
            self.thread.join(0.25)

    observer = Observer()
    observer.thread.start()
    return observer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    for action in ('arm', 'status', 'cancel'):
        group.add_argument('--' + action, action='store_true')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    action = next((a for a in ('arm', 'status', 'cancel') if getattr(args, a)), 'status')
    if not args.execute:
        print(
            json.dumps(
                dict(
                    execute=False,
                    action=action,
                    scope='new_gui_login_backend_readiness',
                    expires_days=7,
                    readiness_budget_seconds=180,
                    stable_seconds=3,
                    opens_device=False,
                    starts_media=False,
                    starts_models=False,
                    restarts_service=False,
                    forces_logout_or_sleep=False,
                    full_p0_acceptance=False,
                )
            )
        )
        return 0
    if sys.version_info < (3, 11):
        print(json.dumps(dict(state='unconfirmed', reason='login_execution_requires_python_311')))
        return 2
    try:
        result = execute(args.runtime_root, action)
    except KeyboardInterrupt:
        print(json.dumps(dict(state='interrupted', reason='check_login_status_before_rearming')))
        return 130
    except (OSError, ValueError, RuntimeError):
        print(
            json.dumps(
                dict(state='unconfirmed', reason='login_check_not_confirmed_no_automatic_retry')
            )
        )
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
