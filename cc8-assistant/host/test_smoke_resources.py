"""Resource receipts are bounded measurements, never invented acceptance."""

from copy import deepcopy
import json
import unittest

from audit_device_suite import audit as audit_suite
from device_smoke import RuntimeChanged
from smoke_resources import CAPACITY, GAUGES, ResourceEvidence, audit
import test_device_smoke as smoke_tests
from test_audit_device_suite import saved_english_pair, saved_suite


def payload(**changes):
    gauges = dict(
        rss_bytes=2048, peak_rss_bytes=4096, python_threads=4, open_fds=8, cpu_seconds=1.5
    )
    gauges.update(changes)
    return {
        'resources': gauges,
        'runtime_instance': 'private-instance',
        'transcript': 'private-text',
    }


class ResourceEvidenceTests(unittest.TestCase):
    def test_existing_reads_are_downsampled_and_final_actual_read_is_kept(self):
        evidence = ResourceEvidence()
        for second in range(126):
            evidence.observe(payload(cpu_seconds=second / 2), second)
        report = evidence.report()
        self.assertEqual([row['elapsed_seconds'] for row in report['samples']], [0, 60, 120, 125])
        result = audit(report, 130)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['sample_span_seconds'], 125)
        self.assertEqual(result['largest_gap_seconds'], 60)
        self.assertEqual(result['gauges']['cpu_seconds']['delta'], 62.5)
        self.assertFalse(result['long_term_stability_asserted'])
        self.assertNotIn('private', json.dumps(report))
        self.assertNotIn('private', json.dumps(result))
        # A caller mutating a saved report cannot overwrite future receipts.
        report['samples'][0]['rss_bytes'] = 99
        self.assertEqual(evidence.report()['samples'][0]['rss_bytes'], 2048)

    def test_report_does_not_fabricate_end_measurements_or_duplicate_timestamps(self):
        evidence = ResourceEvidence()
        self.assertEqual(audit(evidence.report(), 900)['status'], 'unavailable')
        self.assertEqual(audit(evidence.report(), 900)['largest_gap_seconds'], 900)
        evidence.observe(payload(), 3)
        evidence.observe(payload(rss_bytes=3072), 3)
        first = evidence.report()
        self.assertEqual(first['samples'][0]['rss_bytes'], 3072)
        self.assertEqual(len(first['samples']), 1)
        result = audit(first, 900)
        self.assertEqual(result['sample_span_seconds'], 0)
        self.assertEqual(result['largest_gap_seconds'], 897)
        self.assertEqual(evidence.report(), first)

    def test_capacity_preserves_first_and_last_and_marks_gap_instead_of_growing(self):
        evidence = ResourceEvidence()
        for index in range(CAPACITY + 10):
            evidence.observe(payload(), index * 60)
        report = evidence.report()
        self.assertEqual(len(report['samples']), CAPACITY)
        self.assertEqual(report['samples'][0]['elapsed_seconds'], 0)
        self.assertEqual(report['samples'][-1]['elapsed_seconds'], (CAPACITY + 9) * 60)
        self.assertEqual(report['dropped_interval_samples'], 11)
        result = audit(report, (CAPACITY + 9) * 60)
        self.assertEqual(result['status'], 'partial')
        self.assertGreater(result['largest_gap_seconds'], 60)

    def test_missing_and_malformed_gauges_remain_distinct_and_never_escape(self):
        for resources in (None, {}, 'private-invalid-resource-object'):
            evidence = ResourceEvidence()
            evidence.observe({'resources': resources}, 0)
            report = evidence.report()
            result = audit(report, 1)
            self.assertEqual(result['status'], 'unavailable')
            row = report['samples'][0]
            self.assertTrue(all(row[name] is None for name in GAUGES))
            self.assertEqual(
                row['invalid_gauges'] if isinstance(resources, str) else row['missing_gauges'],
                list(GAUGES),
            )
            self.assertNotIn('private', json.dumps(report))
        for value in (True, 'private-number', -1, float('nan'), float('inf'), 10**1000, 1.25):
            evidence = ResourceEvidence()
            evidence.observe(payload(open_fds=value), 0)
            result = audit(evidence.report(), 1)
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(result['invalid_samples'], 1)
            self.assertEqual(result['gauges']['open_fds']['samples'], 0)

    def test_cumulative_regression_is_reported_without_cross_reset_delta(self):
        evidence = ResourceEvidence()
        evidence.observe(payload(cpu_seconds=9, peak_rss_bytes=9000), 0)
        evidence.observe(payload(cpu_seconds=1, peak_rss_bytes=4000), 60)
        result = audit(evidence.report(), 60)
        self.assertEqual(
            result['cumulative_discontinuities'], {'cpu_seconds': 1, 'peak_rss_bytes': 1}
        )
        for name in ('cpu_seconds', 'peak_rss_bytes'):
            self.assertIsNone(result['gauges'][name]['delta'])
            self.assertIsNone(result['gauges'][name]['slope_per_hour'])
        self.assertEqual(result['status'], 'partial')

    def test_audit_rejects_tampering_instead_of_trusting_saved_summary(self):
        evidence = ResourceEvidence()
        for second in (1, 61, 121, 125):
            evidence.observe(payload(), second)
        valid = evidence.report()
        mutations = [
            lambda d: d.update(schema=True),
            lambda d: d.update(scope='whole_mac'),
            lambda d: d.update(capacity=True),
            lambda d: d.update(interval_seconds=1),
            lambda d: d.update(dropped_interval_samples=-1),
            lambda d: d.update(dropped_interval_samples=1),
            lambda d: d.update(accepted=True),
            lambda d: d.update(samples=d['samples'] * CAPACITY),
            lambda d: d['samples'][0].update(transcript='private-text'),
            lambda d: d['samples'][0].update(elapsed_seconds=float('nan')),
            lambda d: d['samples'][0].update(elapsed_seconds=True),
            lambda d: d['samples'][0].update(elapsed_seconds=-1),
            lambda d: d['samples'][1].update(elapsed_seconds=1),
            lambda d: d['samples'][1].update(elapsed_seconds=2),
            lambda d: d['samples'][-1].update(elapsed_seconds=131),
            lambda d: d['samples'][0].update(open_fds=2.5),
            lambda d: d['samples'][0].update(open_fds=True),
            lambda d: d['samples'][0].update(cpu_seconds=float('inf')),
            lambda d: d['samples'][0].update(rss_bytes=None),
            lambda d: d['samples'][0].update(missing_gauges=['rss_bytes']),
            lambda d: d['samples'][0].update(invalid_gauges=['rss_bytes']),
            lambda d: d['samples'][0].update(missing_gauges=['unknown']),
            lambda d: d['samples'][0].update(missing_gauges=['rss_bytes', 'rss_bytes']),
            lambda d: d['samples'][0].update(
                missing_gauges=['rss_bytes'], invalid_gauges=['rss_bytes'], rss_bytes=None
            ),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                changed = deepcopy(valid)
                mutate(changed)
                with self.assertRaises(ValueError):
                    audit(changed, 130)
        for elapsed in (None, True, -1, float('nan'), 86401):
            with self.assertRaises(ValueError):
                audit(valid, elapsed)


class ResourceIntegrationTests(unittest.TestCase):
    def test_failed_standby_keeps_passed_prefix_and_measured_failed_phase_separate(self):
        suite = saved_suite(standby_only=True, missing_standby_quiet=2)
        evidence = ResourceEvidence()
        for second in range(0, 901, 60):
            evidence.observe(payload(cpu_seconds=second), second)
        for phase in suite['phases'][:2]:
            phase['report']['runtime_resource_evidence'] = evidence.report()
        result = audit_suite(suite)
        self.assertFalse(result['verified'])
        self.assertEqual(result['completed_phases'], 1)
        self.assertEqual(result['reason'], 'phase_not_completed')
        self.assertEqual(
            [row.get('samples') for row in result['runtime_resources']], [16, 16, None]
        )
        self.assertEqual(result['runtime_resources'][2]['status'], 'unavailable')
        self.assertFalse(result['full_p0_acceptance'])

    def test_smoke_health_uses_existing_rpc_only_and_rejects_new_runtime_before_sampling(self):
        run, _, health, now = smoke_tests.DeviceAutomationTests().fixture()
        health['resources'] = payload()['resources']
        calls = []
        original = run.request

        def request(value):
            calls.append(value)
            return original(value)

        run.request = request
        run.initial_instance = health['runtime_instance']
        run.health()
        now[0] += 60
        run.health()
        self.assertEqual(calls, [{'operation': 'health'}] * 2)
        evidence = run.report('running')['runtime_resource_evidence']
        self.assertEqual(len(evidence['samples']), 2)
        health['runtime_instance'] = 'replacement'
        now[0] += 60
        with self.assertRaises(RuntimeChanged):
            run.health()
        self.assertEqual(run.report('failed')['runtime_resource_evidence'], evidence)

    def test_failed_cleanup_preserves_last_measurement_without_other_operations(self):
        run, device, health, now = smoke_tests.DeviceAutomationTests().fixture()
        health['resources'] = payload()['resources']
        run.preflight()
        device.open.side_effect = OSError('private-error')
        result = run.run()
        self.assertEqual(result['state'], 'cleanup_unconfirmed')
        resources = audit(result['runtime_resource_evidence'], result['elapsed_seconds'])
        self.assertGreaterEqual(resources['samples'], 1)
        self.assertNotIn('private', json.dumps(result))
        device.speak_probe.assert_not_called()

    def test_suite_recomputes_resources_for_new_reports_and_preserves_old_coverage_limits(self):
        old = saved_english_pair()
        self.assertTrue(audit_suite(old)['verified'])
        self.assertNotIn('runtime_resources', audit_suite(old))
        evidence = ResourceEvidence()
        evidence.observe(payload(), 1)
        evidence.observe(payload(cpu_seconds=5), 31)
        old['phases'][0]['report']['runtime_resource_evidence'] = evidence.report()
        old['runtime_resources'] = 'fake saved summary'
        result = audit_suite(old)
        self.assertTrue(result['verified'])
        self.assertEqual(result['runtime_resources'][0]['samples'], 2)
        self.assertEqual(result['runtime_resources'][0]['gauges']['cpu_seconds']['delta'], 3.5)
        self.assertEqual(result['runtime_resources'][1]['status'], 'unavailable')
        old['phases'][0]['report']['runtime_resource_evidence']['samples'][0]['secret'] = 'private'
        with self.assertRaises(ValueError):
            audit_suite(old)


if __name__ == '__main__':
    unittest.main()
