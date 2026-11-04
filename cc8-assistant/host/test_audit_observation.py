"""Validate saved sampler reports and independently catch false acceptance."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from audit_observation import DURATION, audit, main
from observation_run import ObservationRun
from test_observation import health


BACKEND = '0.14.20'


def observation(*, update=lambda *_: None):
    book = ObservationRun(60)
    for elapsed in range(0, DURATION, 60):
        sample = health(uptime=elapsed + 1)
        sample.update(connected=True, transport='usb')
        update(elapsed, sample)
        book.add(sample, elapsed, elapsed)
    # Match the pre-boundary-fix writer that started the live observation.
    report = book.report()
    report['observation'] = {
        'state': 'completed',
        'requested_seconds': DURATION,
        'interval_seconds': 60,
        'elapsed_seconds': DURATION,
        'wall_elapsed_seconds': DURATION,
        'expected_backend_version': BACKEND,
    }
    return report


class AuditObservationTests(unittest.TestCase):
    def test_complete_window_recalculates_trends_without_claiming_full_acceptance(self):
        report = observation()
        report['segments'][0]['gauges']['rss_bytes']['slope_per_hour'] = 123
        result = audit(report, BACKEND)
        self.assertTrue(result['window_verified'])
        self.assertFalse(result['full_p0_acceptance'])
        self.assertEqual(result['polls'], 4320)
        self.assertEqual(result['gauges']['rss_bytes']['slope_per_hour'], 0)
        self.assertNotIn('samples', result)

    def test_short_running_wrong_version_and_coarse_schedule_each_block_acceptance(self):
        for key, value, reason in (
            ('elapsed_seconds', DURATION - 1, 'window_shorter_than_72_hours'),
            ('wall_elapsed_seconds', DURATION - 1, 'window_shorter_than_72_hours'),
            ('state', 'running', 'window_not_completed'),
            ('expected_backend_version', None, 'backend_version_not_bound'),
            ('interval_seconds', 61, 'poll_interval_over_one_minute'),
        ):
            with self.subTest(key=key):
                report = observation()
                report['observation'][key] = value
                result = audit(report, BACKEND)
                self.assertFalse(result['window_verified'])
                self.assertIn(reason, result['reasons'])

    def test_old_writer_terminal_gap_and_clock_jump_are_found_from_raw_times(self):
        report = observation()
        report['observation']['elapsed_seconds'] += 180
        report['observation']['wall_elapsed_seconds'] += 480
        self.assertEqual(report['poll_gaps'], 0)
        result = audit(report, BACKEND)
        self.assertFalse(result['window_verified'])
        self.assertEqual(result['counts']['poll_gaps'], 1)
        self.assertEqual(result['counts']['wall_clock_discontinuities'], 1)

    def test_missing_start_and_interior_samples_are_recomputed(self):
        report = observation()
        del report['samples'][100:105]
        del report['samples'][:4]
        result = audit(report, BACKEND)
        self.assertFalse(result['window_verified'])
        self.assertEqual(result['counts']['poll_gaps'], 2)
        self.assertIn('poll_totals_inconsistent', result['reasons'])

    def test_completed_collection_can_contain_disconnect_or_new_instance(self):
        def update(elapsed, sample):
            if elapsed == 60:
                sample.update(connected=False, transport='none')
            if elapsed >= 120:
                sample['runtime_instance'] = 'e' * 32

        report = observation(update=update)
        self.assertEqual(report['observation']['state'], 'completed')
        result = audit(report, BACKEND)
        self.assertFalse(result['window_verified'])
        self.assertEqual(result['counts']['disconnected_polls'], 1)
        self.assertIn('single_runtime_not_confirmed', result['reasons'])
        self.assertEqual(result['gauges'], {})

    def test_missing_resources_and_recorded_poll_errors_block_acceptance(self):
        report = observation()
        report['samples'][1]['rss_bytes'] = None
        report['samples'][2]['error'] = 'ipc_unavailable'
        result = audit(report, BACKEND)
        self.assertFalse(result['window_verified'])
        self.assertEqual(result['counts']['resource_coverage_incomplete_polls'], 1)
        self.assertEqual(result['errors'], {'ipc_unavailable': 1})
        self.assertEqual(result['gauges'], {})

    def test_cli_refuses_unsafe_inputs_and_never_prints_paths_or_report_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private-name.json'
            path.write_text('{"schema":1,"schema":1,"private":"do-not-print"}')
            path.chmod(0o600)
            link = path.with_name('link.json')
            link.symlink_to(path)
            fifo = path.with_name('fifo.json')
            os.mkfifo(fifo, 0o600)
            for target in (path, link, fifo):
                with self.subTest(target=target.name), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main([str(target), '--expect-backend', BACKEND]), 2)
                    result = json.loads(output.getvalue())
                    self.assertEqual(result['reasons'], ['invalid_private_report'])
                    self.assertNotIn(str(target), output.getvalue())
                    self.assertNotIn('do-not-print', output.getvalue())

    def test_cli_reads_saved_private_report_without_changing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            original = json.dumps(observation()).encode()
            path.write_bytes(original)
            path.chmod(0o600)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([str(path), '--expect-backend', BACKEND]), 0)
            self.assertTrue(json.loads(output.getvalue())['window_verified'])
            self.assertEqual(path.read_bytes(), original)
