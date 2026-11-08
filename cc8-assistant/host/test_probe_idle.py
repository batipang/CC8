"""Unadmitted candidates may settle; owner activity never authorizes a probe."""

from copy import deepcopy
import json
import unittest

from diagnostics import TraceBook
from probe_idle import PendingWorkFailure, SETTLE_SECONDS, settle_pending_voice, valid_evidence


class Observer:
    def __init__(self):
        self.now = 0.0
        self.book = TraceBook(clock=lambda: self.now)
        self.health = dict(
            runtime_instance='instance',
            capture_generation=120,
            local_wake_count=0,
            voice_asr_attempts=0,
            render_frames=0,
            connected=True,
            microphone_active=True,
            waiting_for_wake=True,
            local_wake_ready=True,
            transport='usb',
            playing=False,
            calibrating=False,
        )
        self.evidence = []
        self.after_sleep = lambda: None
        self.reads = 0

    def candidate(self):
        ident = self.book.begin('voice')
        self.book.mark(ident, 'voice_detected')
        return ident

    def reject(self, ident):
        self.book.mark(ident, 'utterance_end')
        self.book.gate(ident, 'wake_closed')
        self.book.finish(ident, 'rejected')

    def rows(self):
        self.reads += 1
        return self.book.report()['requests']

    def sleep(self, seconds):
        self.now += seconds
        self.after_sleep()

    def run(self):
        return settle_pending_voice(
            self.rows(),
            dict(self.health),
            read_rows=self.rows,
            read_health=lambda: dict(self.health),
            clock=lambda: self.now,
            sleep=self.sleep,
            snapshot=self.evidence.append,
        )


class PendingProbeTests(unittest.TestCase):
    def refused(self, observer, state):
        with self.assertRaisesRegex(PendingWorkFailure, '^owner_request_pending$'):
            observer.run()
        evidence = observer.evidence[-1]
        self.assertEqual(evidence['state'], state)
        self.assertTrue(valid_evidence(evidence))
        self.assertFalse(valid_evidence(evidence, passed=True))

    def test_real_tracebook_candidate_settles_only_after_explicit_local_rejection(self):
        o = Observer()
        ident = o.candidate()
        o.after_sleep = lambda: o.reject(ident) if o.now >= 0.5 else None
        rows, health = o.run()
        self.assertEqual(o.now, 0.5)
        self.assertEqual(rows[0]['outcome'], 'rejected')
        self.assertNotIn('asr_start', rows[0]['stages'])
        self.assertIsNone(rows[0]['generation'])
        self.assertEqual(health['capture_generation'], 120)
        self.assertTrue(valid_evidence(o.evidence[-1], passed=True))
        self.assertNotIn(ident, json.dumps(o.evidence))

    def test_idle_legacy_peer_does_not_wait_or_invent_settlement(self):
        o = Observer()
        o.health.pop('capture_generation')
        self.assertEqual(o.run()[0], [])
        self.assertEqual(o.evidence, [])
        self.assertEqual(o.now, 0)

    def test_pending_legacy_peer_keeps_original_refusal(self):
        o = Observer()
        o.candidate()
        o.health.pop('capture_generation')
        self.refused(o, 'unsupported')
        self.assertEqual(o.now, 0)

    def test_asr_can_be_owner_activity_even_before_generation_binding(self):
        for admit in ('asr', 'wake', 'binding', 'other_origin'):
            with self.subTest(admit=admit):
                o = Observer()
                ident = o.candidate()

                def change():
                    if admit == 'asr':
                        o.book.asr_start(ident, 320)
                    elif admit == 'wake':
                        o.book.mark(ident, 'wake_detected')
                    elif admit == 'binding':
                        o.book.bind(ident, 120)
                    else:
                        o.book.begin('announcement', 121)

                o.after_sleep = change
                self.refused(o, 'owner_activity')
                self.assertEqual(o.now, 0.25)

    def test_new_owner_task_cannot_hide_by_finishing_between_polls(self):
        o = Observer()
        ident = o.candidate()

        def change():
            o.reject(ident)
            request = o.book.begin('text', 121)
            o.book.mark(request, 'brain_start')
            o.book.finish(request, 'completed')

        o.after_sleep = change
        self.refused(o, 'owner_activity')

    def test_capture_and_output_changes_abort_without_new_probe(self):
        changes = dict(
            capture_generation=121,
            runtime_instance='replacement',
            local_wake_count=1,
            voice_asr_attempts=1,
            render_frames=160,
            connected=False,
            microphone_active=False,
            waiting_for_wake=False,
            local_wake_ready=False,
            transport='wifi',
            playing=True,
            calibrating=True,
        )
        for key, value in changes.items():
            with self.subTest(key=key):
                o = Observer()
                ident = o.candidate()

                def change():
                    o.reject(ident)
                    o.health[key] = value

                o.after_sleep = change
                self.refused(o, 'capture_changed')

    def test_disappearing_candidate_is_missing_evidence_not_an_idle_result(self):
        o = Observer()
        o.candidate()
        o.after_sleep = lambda: o.book.rows.clear()
        self.refused(o, 'evidence_lost')

    def test_cancelled_or_differently_rejected_candidate_never_becomes_settled(self):
        for outcome, gate, state in (
            ('cancelled', 'stale', 'owner_activity'),
            ('rejected', 'empty', 'owner_activity'),
            ('completed', 'wake_closed', 'owner_activity'),
        ):
            with self.subTest(outcome=outcome, gate=gate):
                o = Observer()
                ident = o.candidate()

                def change():
                    o.book.gate(ident, gate)
                    o.book.finish(ident, outcome)

                o.after_sleep = change
                self.refused(o, state)

    def test_unfinished_candidate_is_bounded_and_unknown_does_not_mean_zero(self):
        o = Observer()
        o.candidate()
        self.refused(o, 'timed_out')
        self.assertEqual(o.now, SETTLE_SECONDS)
        self.assertEqual(o.evidence[-1]['observations'][-1]['candidate'], 1)
        self.assertLessEqual(o.reads, 12)

    def test_slow_rpc_cannot_authorize_late_settlement(self):
        o = Observer()
        ident = o.candidate()

        def slow_read():
            o.now += 3
            o.reject(ident)
            return o.book.report()['requests']

        o.rows = slow_read
        # The initial snapshot is already pending; delay only a subsequent read.
        initial = o.book.report()['requests']
        with self.assertRaises(PendingWorkFailure):
            settle_pending_voice(
                initial,
                o.health,
                read_rows=slow_read,
                read_health=lambda: o.health,
                clock=lambda: o.now,
                sleep=o.sleep,
                snapshot=o.evidence.append,
            )
        self.assertEqual(o.evidence[-1]['state'], 'timed_out')

    def test_unknown_fields_are_never_copied_to_evidence(self):
        o = Observer()
        ident = o.candidate()
        o.book.rows[0]['text'] = 'PRIVATE_BODY'
        o.book.rows[0]['credential'] = 'PRIVATE_TOKEN'
        o.after_sleep = lambda: o.reject(ident)
        o.run()
        serialized = json.dumps(o.evidence)
        self.assertNotIn('PRIVATE', serialized)
        self.assertNotIn(ident, serialized)

    def test_evidence_validator_rejects_forged_content_types_and_settlement(self):
        o = Observer()
        ident = o.candidate()
        o.after_sleep = lambda: o.reject(ident)
        o.run()
        good = o.evidence[-1]
        self.assertTrue(valid_evidence(None, passed=True))
        cases = (
            lambda r: r.update(state=[]),
            lambda r: r.update(state='private'),
            lambda r: r.update(schema=True),
            lambda r: r.update(request_id='PRIVATE'),
            lambda r: r.update(elapsed_seconds=10**500),
            lambda r: r.update(elapsed_seconds=float('nan')),
            lambda r: r['observations'][-1].update(candidate=True),
            lambda r: r['observations'][-1].update(request=1),
            lambda r: r['observations'][-1].update(seconds=-1),
            lambda r: r['observations'][-1].update(seconds=10**500),
            lambda r: r['observations'][-1].update(text='PRIVATE'),
            lambda r: r.update(state='waiting'),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                invalid = deepcopy(good)
                mutate(invalid)
                self.assertFalse(valid_evidence(invalid, passed=True))


class ProbeAdmissionIntegrationTests(unittest.TestCase):
    def test_real_smokerun_waits_for_local_rejection_before_the_only_stimulus(self):
        import test_device_smoke as fixtures

        now = [0]
        book = TraceBook(clock=lambda: now[0])
        ident = book.begin('voice')
        book.mark(ident, 'voice_detected')

        class Before:
            def __iter__(self):
                return iter(book.report()['requests'])

        fixture = fixtures.DeviceAutomationTests()
        run, device, health, now = fixture.acoustic_fixture(
            lambda _: [*book.report()['requests'], fixture.acoustic_row()], before=Before()
        )
        health.update(capture_generation=120)

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 0.5:
                book.mark(ident, 'utterance_end')
                book.gate(ident, 'wake_closed')
                book.finish(ident, 'rejected')

        run.sleep = sleep
        run.acoustic_probe()
        device.speak_probe.assert_called_once()
        self.assertEqual(run.pre_probe_idle_evidence['state'], 'settled')
        self.assertEqual(run.pre_probe_idle_evidence['elapsed_seconds'], 0.5)
        self.assertEqual(run.acoustic_evidence['completed_tasks'], 1)
        self.assertTrue(valid_evidence(run.pre_probe_idle_evidence, passed=True))

    def test_health_generation_is_content_free_and_cleared_when_capture_is_off(self):
        from bridge import Bridge

        bridge = Bridge(None, None)
        self.assertIsNone(bridge.health()['capture_generation'])
        bridge.session.active = True
        bridge.session.generation = 123
        self.assertEqual(bridge.health()['capture_generation'], 123)
        bridge.session.generation += 1
        self.assertEqual(bridge.health()['capture_generation'], 124)
        bridge.session.active = False
        self.assertIsNone(bridge.health()['capture_generation'])

    def test_preparation_cannot_borrow_settlement_after_owner_or_capture_change(self):
        import test_device_smoke as fixtures
        from device_smoke import CheckFailure

        for change in ('capture', 'new_candidate'):
            with self.subTest(change=change):
                now = [0]
                book = TraceBook(clock=lambda: now[0])
                ident = book.begin('voice')
                book.mark(ident, 'voice_detected')

                class Before:
                    def __iter__(self):
                        return iter(book.report()['requests'])

                run, device, health, now = fixtures.DeviceAutomationTests().acoustic_fixture(
                    lambda _: [], before=Before()
                )
                health.update(capture_generation=120)

                def sleep(seconds):
                    now[0] += seconds
                    book.gate(ident, 'wake_closed')
                    book.finish(ident, 'rejected')

                original_status = device.status.side_effect

                def status():
                    if change == 'capture':
                        health['capture_generation'] += 1
                    else:
                        candidate = book.begin('voice')
                        book.mark(candidate, 'voice_detected')
                    return original_status()

                run.sleep = sleep
                device.status.side_effect = status
                with self.assertRaisesRegex(CheckFailure, 'work_changed_before_stimulus'):
                    run.acoustic_probe()
                device.speak_probe.assert_not_called()

    def test_a_previous_wait_receipt_does_not_survive_a_new_acoustic_attempt(self):
        import test_device_smoke as fixtures
        from device_smoke import CheckFailure

        run, device, _, _ = fixtures.DeviceAutomationTests().fixture()
        run.pre_probe_idle_evidence = {'state': 'settled'}
        with self.assertRaises(CheckFailure):
            run.acoustic_probe()
        self.assertIsNone(run.pre_probe_idle_evidence)
        device.speak_probe.assert_not_called()

    def test_offline_suite_audit_rejects_unsettled_or_extended_evidence(self):
        import test_audit_device_suite as fixtures
        from audit_device_suite import content_free_phase

        report = fixtures.saved_english_pair()['phases'][0]['report']
        self.assertTrue(content_free_phase(report))
        o = Observer()
        ident = o.candidate()
        o.after_sleep = lambda: o.reject(ident)
        o.run()
        report['pre_probe_idle_evidence'] = o.evidence[-1]
        self.assertTrue(content_free_phase(report))
        for extra in ({'state': 'waiting'}, {'state': 'owner_activity'}, {'text': 'PRIVATE'}):
            with self.subTest(extra=extra):
                invalid = deepcopy(report)
                invalid['pre_probe_idle_evidence'].update(extra)
                self.assertFalse(content_free_phase(invalid))


if __name__ == '__main__':
    unittest.main()
