"""Synthetic GUI domains/clocks exercise the real one-shot receipt policy, without login."""

from copy import deepcopy
import threading
import unittest

from login_receipt import LoginRecoveryRun


def baseline():
    return dict(
        schema=1,
        id='a' * 32,
        state='armed',
        armed_at=1000,
        expires_at=2000,
        scope='new_gui_login_backend_readiness',
        baseline=dict(
            session='old',
            identity={'config': 'original'},
            instance='old-worker',
            process='old-pid',
            activity={'before': 5},
        ),
        new_gui_login_observed=False,
        backend_ready=False,
        full_p0_acceptance=False,
        automatic_launch_cause_verified=False,
        media_started=False,
        tasks_replayed=False,
    )


class Store:
    def __init__(self, value=None):
        self.value = deepcopy(value or baseline())
        self.writes = []

    def load(self):
        return deepcopy(self.value)

    def replace(self, ident, value, expected='observing'):
        if self.value['id'] != ident or self.value['state'] != expected:
            return False
        self.value = deepcopy(value)
        self.writes.append(deepcopy(value))
        return True


class Scenario:
    def __init__(self, fault=None):
        self.now, self.fault = 0.0, fault
        self.store, self.stop = Store(), threading.Event()
        self.samples = 0
        self.identities = 0

    def wait(self, seconds):
        self.now += seconds

    def session(self, nonce):
        return (
            'old'
            if self.fault == 'same_login'
            else 'changed-again'
            if self.fault == 'domain' and self.now >= 1
            else 'new'
        )

    def identity(self):
        self.identities += 1
        if (
            self.fault == 'configuration'
            or self.fault == 'final_configuration'
            and self.identities >= 8
        ):
            return {'config': 'owner-new-config'}
        return {'config': 'original'}

    def sample(self, nonce):
        self.samples += 1
        if self.fault == 'timeout':
            return None
        if self.fault == 'loading' and self.now < 2:
            return None
        if self.fault == 'blocked_reply':
            self.now += 181
        if self.fault == 'sampling_gap':
            self.now += 3
        if self.fault == 'cancel' and self.samples == 2:
            self.store.value.update(state='interrupted', reason='owner_cancelled_login_check')
            self.stop.set()
        if self.fault == 'replace' and self.samples == 2:
            self.store.value.update(id='b' * 32, state='armed')
        return dict(
            instance='old-worker'
            if self.fault in ('old_worker', 'adopted', 'adopted_activity')
            else str(self.samples)
            if self.fault == 'unstable'
            else 'new-worker',
            pid=100,
            process='old-pid'
            if self.fault in ('old_worker', 'adopted', 'adopted_activity')
            else 'new-pid',
            adopted=self.fault in ('adopted', 'adopted_activity'),
            activity={'before': 6 if self.fault == 'adopted_activity' else 5},
            idle=self.fault != 'owner_work'
            and not (self.fault == 'final_work' and self.samples >= 8),
            activity_zero=self.fault != 'activity',
            connected=True,
            transport='usb',
        )

    def run(self):
        return LoginRecoveryRun(
            self.store,
            self,
            self.stop,
            clock=lambda: self.now,
            wall=lambda: (
                900
                if self.fault == 'clock_back'
                else 2001
                if self.fault == 'expired' or self.fault == 'final_expiry' and self.samples >= 8
                else 1000 + self.now
            ),
            wait=self.wait,
        ).run()


class LoginReceiptTests(unittest.TestCase):
    def test_verified_surviving_worker_adoption_preserves_counters_instead_of_requiring_zero(self):
        scenario = Scenario('adopted')
        value = scenario.run()
        self.assertEqual(value['state'], 'passed')
        self.assertTrue(value['backend_reused'])
        scenario = Scenario('adopted_activity')
        self.assertEqual(scenario.run()['state'], 'failed')

    def test_new_login_requires_new_worker_clean_configuration_and_three_stable_seconds(self):
        scenario = Scenario('loading')
        value = scenario.run()
        self.assertEqual(value['state'], 'passed')
        self.assertGreaterEqual(value['stable_seconds'], 3)
        self.assertGreaterEqual(value['elapsed_seconds'], 5)
        self.assertTrue(
            value['new_gui_login_observed'] and value['original_configuration_preserved']
        )
        self.assertFalse(value['full_p0_acceptance'] or value['automatic_launch_cause_verified'])
        self.assertNotIn('baseline', value)
        self.assertFalse(value['media_started'] or value['tasks_replayed'])
        self.assertEqual([row['state'] for row in scenario.store.writes], ['observing', 'passed'])

    def test_manual_restart_in_same_gui_session_remains_armed_without_health_polling(self):
        scenario = Scenario('same_login')
        self.assertEqual(scenario.run()['state'], 'armed')
        self.assertEqual(scenario.samples, 0)
        self.assertEqual(scenario.store.writes, [])

    def test_failure_or_clock_change_is_not_retried_or_marked_passed(self):
        for fault in (
            'domain',
            'configuration',
            'final_configuration',
            'timeout',
            'old_worker',
            'owner_work',
            'activity',
            'unstable',
            'blocked_reply',
            'sampling_gap',
            'final_work',
            'clock_back',
            'expired',
            'final_expiry',
        ):
            with self.subTest(fault=fault):
                scenario = Scenario(fault)
                value = scenario.run()
                self.assertIn(value['state'], ('failed', 'expired'))
                self.assertEqual(scenario.store.value['state'], value['state'])
                before = scenario.samples
                scenario.run()
                self.assertEqual(scenario.samples, before)

    def test_cancel_and_replaced_attempt_are_never_overwritten_by_old_observer(self):
        for fault in ('cancel', 'replace'):
            with self.subTest(fault=fault):
                scenario = Scenario(fault)
                scenario.run()
                if fault == 'cancel':
                    self.assertEqual(scenario.store.value['reason'], 'owner_cancelled_login_check')
                else:
                    self.assertEqual(scenario.store.value['id'], 'b' * 32)
                    self.assertEqual(scenario.store.value['state'], 'armed')

    def test_prior_observing_attempt_is_unknown_and_never_replayed(self):
        scenario = Scenario()
        scenario.store.value['state'] = 'observing'
        self.assertEqual(scenario.run()['state'], 'observing')
        self.assertEqual(scenario.samples, 0)
