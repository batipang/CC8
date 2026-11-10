"""One explicitly armed, metadata-only check across a real GUI login boundary."""

from copy import deepcopy
import math
import re
import time

STATES = ('armed', 'observing', 'passed', 'failed', 'interrupted', 'expired')


class LoginCheckFailure(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise LoginCheckFailure(reason)


def document(value):
    require(
        isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1,
        'login_check_invalid',
    )
    require(value.get('state') in STATES, 'login_check_invalid')
    require(
        isinstance(value.get('id'), str) and re.fullmatch('[a-f0-9]{32}', value['id']),
        'login_check_invalid',
    )
    require(isinstance(value.get('baseline'), dict), 'login_check_invalid')
    require(value.get('scope') == 'new_gui_login_backend_readiness', 'login_check_invalid')
    require(
        all(
            value.get(key) is False
            for key in (
                'full_p0_acceptance',
                'automatic_launch_cause_verified',
                'media_started',
                'tasks_replayed',
            )
        ),
        'login_check_invalid',
    )
    require(
        all(type(value.get(key)) is bool for key in ('new_gui_login_observed', 'backend_ready')),
        'login_check_invalid',
    )
    for key in ('armed_at', 'expires_at'):
        require(
            type(value.get(key)) in (int, float) and math.isfinite(value[key]),
            'login_check_clock_invalid',
        )
    require(0 < value['expires_at'] - value['armed_at'] <= 7 * 86400, 'login_check_clock_invalid')
    for key in ('stable_seconds', 'elapsed_seconds'):
        if key in value:
            require(
                type(value[key]) in (int, float)
                and math.isfinite(value[key])
                and 0 <= value[key] <= 300,
                'login_check_invalid',
            )
    for key in ('connected', 'backend_reused', 'original_configuration_preserved'):
        require(key not in value or type(value[key]) is bool, 'login_check_invalid')
    require(
        'transport' not in value or value['transport'] in ('none', 'usb', 'wifi'),
        'login_check_invalid',
    )
    return value


def report(value):
    # Baseline hashes are for local comparison, not public diagnostics.
    result = {
        key: deepcopy(value[key])
        for key in (
            'schema',
            'id',
            'state',
            'armed_at',
            'expires_at',
            'scope',
            'reason',
            'new_gui_login_observed',
            'backend_ready',
            'backend_reused',
            'stable_seconds',
            'elapsed_seconds',
            'original_configuration_preserved',
            'connected',
            'transport',
            'full_p0_acceptance',
            'automatic_launch_cause_verified',
            'media_started',
            'tasks_replayed',
        )
        if key in value
    }
    # Failures are fixed diagnostic categories, never exception/socket text.
    if 'reason' in result and (
        not isinstance(result['reason'], str)
        or result['reason']
        not in {
            'login_check_expired',
            'login_check_stopped',
            'login_check_replaced',
            'login_domain_changed_during_check',
            'login_original_configuration_changed',
            'login_old_worker_unconfirmed',
            'login_owner_started_work',
            'login_activity_or_replay_observed',
            'login_ready_deadline',
            'owner_cancelled_login_check',
        }
    ):
        result['reason'] = 'login_observation_unconfirmed'
    return result


class LoginRecoveryRun:
    def __init__(self, store, probe, stop, *, clock=time.monotonic, wall=time.time, wait=None):
        self.store, self.probe, self.stop = store, probe, stop
        self.clock, self.wall = clock, wall
        self.wait = wait or stop.wait

    def run(self):
        value = self.store.load()
        if value is None:
            return None
        document(value)
        if value['state'] != 'armed':
            return report(value)  # Never retry an uncertain prior observation.
        ident = value['id']
        claimed = False
        try:
            require(value['armed_at'] <= self.wall() < value['expires_at'], 'login_check_expired')
            session = self.probe.session(ident)
            if session == value['baseline']['session']:
                return report(value)  # Manual restart in this GUI domain is not a login.
            value.update(state='observing', new_gui_login_observed=True)
            require(self.store.replace(ident, value, expected='armed'), 'login_check_replaced')
            claimed = True
            began = self.clock()
            deadline = began + 180
            stable = None
            while self.clock() < deadline:
                require(not self.stop.is_set(), 'login_check_stopped')
                require(
                    value['armed_at'] <= self.wall() < value['expires_at'], 'login_check_expired'
                )
                require(self.probe.session(ident) == session, 'login_domain_changed_during_check')
                require(
                    self.probe.identity() == value['baseline']['identity'],
                    'login_original_configuration_changed',
                )
                sample = self.probe.sample(ident)
                # A timeout or loading worker is not terminal. The observer
                # never starts a worker, restarts a service or alters its grace.
                if sample is None:
                    stable = None
                else:
                    require(sample['idle'] is True, 'login_owner_started_work')
                    reused = sample['instance'] == value['baseline']['instance']
                    if reused:
                        require(
                            sample['adopted'] is True
                            and sample['process'] == value['baseline']['process'],
                            'login_old_worker_unconfirmed',
                        )
                        require(
                            sample['activity'] == value['baseline']['activity'],
                            'login_activity_or_replay_observed',
                        )
                    else:
                        require(
                            sample['activity_zero'] is True, 'login_activity_or_replay_observed'
                        )
                    identity = (sample['instance'], sample['pid'])
                    if stable is None or stable[0] != identity or self.clock() - stable[2] > 2:
                        stable = identity, self.clock(), self.clock()
                    else:
                        stable = stable[0], stable[1], self.clock()
                    if self.clock() - stable[1] >= 3:
                        require(
                            self.probe.session(ident) == session,
                            'login_domain_changed_during_check',
                        )
                        require(
                            self.probe.identity() == value['baseline']['identity'],
                            'login_original_configuration_changed',
                        )
                        final = self.probe.sample(ident)
                        require(
                            final is not None
                            and (final['instance'], final['pid']) == identity
                            and final['idle'] is True
                            and final['activity'] == sample['activity'],
                            'login_final_readiness_changed',
                        )
                        require(self.clock() - stable[2] <= 2, 'login_final_readiness_changed')
                        require(self.clock() < deadline, 'login_ready_deadline')
                        require(not self.stop.is_set(), 'login_check_stopped')
                        require(
                            value['armed_at'] <= self.wall() < value['expires_at'],
                            'login_check_expired',
                        )
                        value.update(
                            state='passed',
                            backend_ready=True,
                            backend_reused=reused,
                            stable_seconds=round(self.clock() - stable[1], 3),
                            elapsed_seconds=round(self.clock() - began, 3),
                            original_configuration_preserved=True,
                            connected=final['connected'],
                            transport=final['transport'],
                        )
                        require(self.store.replace(ident, value), 'login_check_replaced')
                        return report(value)
                self.wait(min(0.5, max(0, deadline - self.clock())))
            raise LoginCheckFailure('login_ready_deadline')
        except Exception as error:
            reason = (
                str(error)
                if isinstance(error, LoginCheckFailure)
                else 'login_observation_unconfirmed'
            )
            value.update(
                state='expired'
                if reason == 'login_check_expired'
                else 'interrupted'
                if reason == 'login_check_stopped'
                else 'failed',
                reason=reason,
            )
            self.store.replace(ident, value, expected='observing' if claimed else 'armed')
            return report(value)
