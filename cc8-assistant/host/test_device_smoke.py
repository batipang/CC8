"""Automation fails closed and restores capture; fixtures never contact hardware."""

from contextlib import redirect_stdout, redirect_stderr
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from device_smoke import (
    ACOUSTIC_QUESTION,
    ACOUSTIC_RESPONSE_METRIC,
    ACTIVE_HANDOVER_CHECK,
    ALL_ROUTE_LOSS_CHECK,
    ALL_ROUTE_LOSS_REQUEST,
    AWAKE_DIALOGUE_CHECK,
    BARGE_ANNOUNCEMENT,
    BARGE_CHECK,
    BARGE_PRIME_CHECK,
    BARGE_QUERY,
    STOP_CHECK,
    STOP_QUERY,
    WAKE_SELF_CHECK,
    WAKE_SELF_TEXT,
    CheckFailure,
    Device,
    MULTI_CLAUSE_TEXT,
    P0_DEVICE_PHASES,
    POST_DIALOGUE_SECONDS,
    QA_ENDPOINT_METRIC,
    QA_CORPUS_SHA256,
    QA_LATENCY_PHASES,
    QA_LATENCY_QUESTIONS,
    QA_PLAYOUT_METRIC,
    QA_STAGE_METRICS,
    RuntimeChanged,
    SmokeRun,
    main,
    objects,
    planned_checks,
    rpc,
    run_device_suite,
    summarize_capture_clock,
    valid_leading_evidence,
)


class DeviceAutomationTests(unittest.TestCase):
    def test_explicit_open_waits_for_owned_activity_and_keeps_shell_deadline(self):
        for capture in (False, True):
            with self.subTest(capture=capture):
                device = Device.__new__(Device)
                device.transport = 'owned-usb'
                device.access = Mock()
                device.access.target.return_value = device.transport
                device.status = Mock(
                    return_value=(
                        {'test_capture_limit_supported': True, 'microphone_active': False},
                        {},
                    )
                )
                device.open(capture)
                device.access.target.assert_called_once_with()
                args = device.access.shell.call_args
                self.assertEqual(args.kwargs, {'timeout': 15})
                self.assertEqual(args.args[0], device.transport)
                self.assertIn(
                    'am start -W --activity-clear-top -n com.happyhome.cc8assistant/', args.args[1]
                )
                expected = ' --ez auto_start_voice ' + str(capture).lower()
                if capture:
                    expected += ' --el test_capture_limit_ms 180000'
                self.assertTrue(args.args[1].endswith(expected))

    def test_open_refuses_a_changed_usb_target_before_launching_activity(self):
        device = Device.__new__(Device)
        device.transport = 'owned-usb'
        device.access = Mock()
        device.access.target.return_value = 'different-usb'
        with self.assertRaisesRegex(CheckFailure, 'usb_target_changed'):
            device.open(False)
        device.access.shell.assert_not_called()

    def test_missing_speech_probe_voice_aborts_before_ui_or_capture(self):
        run, device, health, now = self.fixture(capture=True, standby=True, acoustic=True)
        device.prepare_speech_probe.side_effect = ValueError('unavailable_voice')
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        device.open.assert_not_called()
        device.speak_probe.assert_not_called()

    def test_speech_probe_resolves_actual_installed_voice_before_say(self):
        device = Device.__new__(Device)
        with (
            patch('device_smoke.resolve_voice', return_value='Tingting (Chinese CN)') as resolve,
            patch('device_smoke.subprocess.run') as command,
        ):
            device.speak_probe()
            resolve.assert_called_once_with('Tingting')
            self.assertEqual(command.call_args.args[0][1:3], ['-v', 'Tingting (Chinese CN)'])
            device.speak_probe(interruption=True)
            self.assertEqual(command.call_args.args[0][-1], BARGE_QUERY)
            device.speak_probe(stop=True)
            self.assertEqual(command.call_args.args[0][-1], STOP_QUERY)
            device.speak_probe(question=QA_LATENCY_QUESTIONS[-1])
            self.assertEqual(command.call_args.args[0][-1], '大黄蜂，六加五等于几？')
            with self.assertRaisesRegex(CheckFailure, 'unknown_synthetic_probe'):
                device.speak_probe(question='任意内容')
            self.assertEqual(command.call_count, 4)
        with (
            patch('device_smoke.resolve_voice', side_effect=ValueError),
            patch('device_smoke.subprocess.run') as command,
        ):
            with self.assertRaises(ValueError):
                device.speak_probe()
            command.assert_not_called()

    def fixture(self, **options):
        now = [0.0]
        device = Mock()
        device.status.return_value = (
            {
                'foreground': True,
                'microphone_active': False,
                'vision_active': False,
                'transport': 'usb',
                'test_capture_limit_supported': True,
                'mic_frames': 100,
            },
            {'camera_open': False, 'wake_checked': True, 'screen_sleeping': False},
        )
        health = {
            'ok': True,
            'runtime_instance': 'same-instance',
            'connected': True,
            'microphone_active': False,
            'calibrating': False,
            'playing': False,
            'transport': 'usb',
            'mic_frames': 100,
        }

        def request(p):
            if p == {'operation': 'health'}:
                return dict(health)
            if p == {'operation': 'transport_status'}:
                return {'ok': True, 'usb_ready': True, 'wifi_ready': True}
            raise AssertionError('Unexpected operation')

        def sleep(seconds):
            now[0] += seconds

        run = SmokeRun(device, request, clock=lambda: now[0], sleep=sleep, **options)
        return run, device, health, now

    def test_dry_plan_in_minimal_path_has_no_device_or_model_side_effect(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'not-created'
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / 'cc8'),
                    'verify-device',
                    '--runtime-root',
                    str(missing),
                    '--capture',
                    '--standby',
                    '--acoustic',
                    '--capture-clock-evidence',
                ],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertFalse(plan['execute'])
            self.assertTrue(plan['capture_clock_evidence'])
            self.assertEqual(len(plan['checks']), 10)
            self.assertIn('recording_clock_provenance', plan['checks'])
            self.assertFalse(missing.exists())
        with patch('device_smoke.Device') as device, patch('device_smoke.rpc') as query:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--runtime-root', '/nonexistent']), 0)
            device.assert_not_called()
            query.assert_not_called()

    def test_capture_options_cannot_be_enabled_implicitly(self):
        for flags in (
            ['--standby'],
            ['--acoustic'],
            ['--capture', '--acoustic'],
            ['--awake-dialogue'],
            ['--capture', '--standby', '--awake-dialogue'],
            ['--synthetic-barge'],
            ['--capture', '--standby', '--synthetic-barge'],
            ['--wake-self-playback'],
            ['--p0-device-suite'],
            ['--capture', '--p0-device-suite', '--active-handover'],
            ['--active-handover'],
            ['--all-route-loss'],
            ['--capture', '--standby', '--all-route-loss'],
            ['--capture', '--standby', '--wake-self-playback'],
            ['--capture', '--synthetic-barge', '--wake-self-playback'],
            ['--capture-clock-evidence'],
            ['--capture', '--leading-acoustic-evidence'],
        ):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit), patch('device_smoke.Device') as device:
                    main(['--runtime-root', '/unused', *flags])
                device.assert_not_called()

    def test_fixed_device_suite_has_three_distinct_standby_cycles(self):
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as query,
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--p0-device-suite']), 0
            )
            device.assert_not_called()
            query.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertFalse(plan['full_p0_acceptance'])
        self.assertEqual(plan['minimum_backend_version'], '0.14.50')
        phases = plan['phases']
        self.assertEqual(len(phases), 6)
        self.assertIn(ACTIVE_HANDOVER_CHECK, phases[0]['checks'])
        self.assertIn(ALL_ROUTE_LOSS_CHECK, phases[0]['checks'])
        self.assertIn(WAKE_SELF_CHECK, phases[0]['checks'])
        self.assertIn(BARGE_CHECK, phases[1]['checks'])
        self.assertIn(BARGE_PRIME_CHECK, phases[1]['checks'])
        self.assertNotIn(AWAKE_DIALOGUE_CHECK, phases[1]['checks'])
        self.assertEqual(phases[2]['name'], 'awake_dialogue')
        self.assertIn(AWAKE_DIALOGUE_CHECK, phases[2]['checks'])
        self.assertNotIn(BARGE_CHECK, phases[2]['checks'])
        for index, phase in enumerate(phases[3:], start=1):
            self.assertEqual(phase['name'], f'standby_{index}')
            self.assertEqual(
                phase['checks'][-2:],
                [
                    'real_idle_black_screen',
                    'synthetic_acoustic_wake_and_response',
                ],
            )

    def test_extended_voice_plan_adds_stop_and_room_evidence_without_device_access(self):
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as query,
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        '--runtime-root',
                        '/unused',
                        '--capture',
                        '--p0-device-suite',
                        '--extended-voice-evidence',
                    ]
                ),
                0,
            )
            device.assert_not_called()
            query.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['extended_voice_evidence'])
        self.assertEqual(plan['minimum_backend_version'], '0.14.59')
        self.assertEqual(plan['minimum_apk_code'], 69)
        self.assertEqual(
            [phase['name'] for phase in plan['phases']],
            [
                'transport_and_self_wake',
                'interruption_and_playback',
                'synthetic_stop',
                'awake_dialogue',
                'standby_1',
                'standby_2',
                'standby_3',
            ],
        )
        self.assertIn(STOP_CHECK, plan['phases'][2]['checks'])

    def test_extended_voice_suite_requires_stop_and_room_results(self):
        for case in ('ok', 'cloud_attempt', 'missing_room'):
            with self.subTest(case=case):
                runs, reports = [], []

                def make_run(options, checkpoint):
                    runs.append(options)
                    report = {
                        'state': 'passed',
                        'checks': [
                            dict(name=name, state='passed') for name in planned_checks(options)
                        ],
                        'cleanup_microphone_off_verified': True,
                        'acoustic_evidence': {
                            'post_playback_quiet_seconds': 20,
                            'recognized_stimulus_matches': True,
                            'response_playout_timing': {'status': 'observed'},
                        },
                    }
                    if 'synthetic_stop' in options:
                        report['barge_evidence'] = {
                            'device_stop_applied': True,
                            'announcement_cancelled': True,
                            'stimulus_played': True,
                            'fast_stop_detections': 1,
                            'stop_cloud_asr_attempts': int(case == 'cloud_attempt'),
                            'admitted_voice_requests': 0,
                            'post_playback_quiet_seconds': 20,
                        }
                    if 'response_acoustic_evidence' in options and case != 'missing_room':
                        report['acoustic_evidence']['response_acoustic_timing'] = {
                            'status': 'observed',
                            ACOUSTIC_RESPONSE_METRIC: 1200,
                        }
                    run = Mock(source_sha256='source', speech_source_sha256='speech')
                    run.run.side_effect = lambda: checkpoint(report) or report
                    return run

                result = run_device_suite(make_run, reports.append, extended_voice=True)
                self.assertTrue(result['extended_voice_evidence'])
                self.assertFalse(result['full_p0_acceptance'])
                if case == 'ok':
                    self.assertEqual(result['state'], 'passed')
                    self.assertEqual(len(runs), 7)
                    self.assertIn('response_acoustic_evidence', runs[3])
                else:
                    failed = 2 if case == 'cloud_attempt' else 3
                    self.assertEqual(len(runs), failed + 1)
                    self.assertEqual(result['state'], 'failed')
                    self.assertEqual(
                        result['phases'][failed]['reason'],
                        'suite_synthetic_stop_evidence_incomplete'
                        if case == 'cloud_attempt'
                        else 'suite_awake_response_acoustic_incomplete',
                    )
                    self.assertTrue(
                        all(p['state'] == 'not_run' for p in result['phases'][failed + 1 :])
                    )

    def test_extended_voice_suite_rejects_missing_stop_detector_before_device_access(self):
        from release_info import RELEASE

        health = dict(
            bridge_version=RELEASE.backend_version,
            protocol_version=RELEASE.protocol_version,
            release_id=RELEASE.release_id,
            runtime_instance='test',
            response_acoustic_evidence_supported=True,
            fast_stop_ready=False,
            fast_stop_count=0,
        )
        errors = io.StringIO()
        with (
            patch('device_smoke.load_release', return_value=RELEASE),
            patch('device_smoke.rpc', return_value=health) as request,
            patch('device_smoke.Device') as device,
            patch('device_smoke.private_directory') as output,
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as stopped,
        ):
            main(
                [
                    '--runtime-root',
                    str(Path.cwd()),
                    '--capture',
                    '--p0-device-suite',
                    '--extended-voice-evidence',
                    '--execute',
                ]
            )
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn('当前版本组合不支持扩展语音验收', errors.getvalue())
        self.assertEqual(request.call_count, 1)
        device.assert_not_called()
        output.assert_not_called()

    def test_device_suite_does_not_retry_or_accept_missing_checks_and_cleanup(self):
        for case in (
            'passed',
            'failed',
            'interrupted',
            'cleanup_unconfirmed',
            'missing_check',
            'missing_cleanup',
            'source_changed',
            'missing_awake_quiet',
            'short_awake_quiet',
            'missing_awake_timing',
            'malformed_standby_timing',
            'missing_awake_stimulus',
        ):
            with self.subTest(case=case):
                runs, saves = [], []

                def make_run(options, checkpoint):
                    index = len(runs)
                    run = Mock(
                        source_sha256='changed'
                        if index == 1 and case == 'source_changed'
                        else 'source',
                        speech_source_sha256='speech',
                    )
                    runs.append(run)
                    expected = planned_checks(options)
                    if case == 'missing_check' and index == 1:
                        expected.pop()
                    report = {
                        'state': case
                        if index == 1 and case in ('failed', 'interrupted', 'cleanup_unconfirmed')
                        else 'passed',
                        'checks': [dict(name=name, state='passed') for name in expected],
                        'cleanup_microphone_off_verified': not (
                            index == 1 and case == 'missing_cleanup'
                        ),
                    }
                    if {'awake_dialogue', 'acoustic'} & options:
                        evidence = report['acoustic_evidence'] = {}
                        if not (case == 'missing_awake_stimulus' and index == 2):
                            evidence['recognized_stimulus_matches'] = True
                        if case != 'missing_awake_quiet':
                            evidence['post_playback_quiet_seconds'] = (
                                2 if case == 'short_awake_quiet' else POST_DIALOGUE_SECONDS
                            )
                        if not (case == 'missing_awake_timing' and index == 2):
                            evidence['response_playout_timing'] = {
                                'status': 'malformed'
                                if case == 'malformed_standby_timing' and index == 3
                                else 'observed',
                            }
                    run.run.side_effect = lambda: checkpoint(report) or report
                    return run

                result = run_device_suite(
                    make_run, lambda value: saves.append(json.loads(json.dumps(value)))
                )
                self.assertEqual(saves[-1], result)
                self.assertFalse(result['full_p0_acceptance'])
                if case == 'passed':
                    self.assertEqual(len(runs), len(P0_DEVICE_PHASES))
                    self.assertEqual(result['state'], 'passed')
                else:
                    attempted = (
                        4
                        if case == 'malformed_standby_timing'
                        else 3
                        if case.endswith(('_awake_quiet', '_awake_timing', '_awake_stimulus'))
                        else 2
                    )
                    self.assertEqual(len(runs), attempted)
                    self.assertTrue(
                        all(row['state'] == 'not_run' for row in result['phases'][attempted:])
                    )
                    self.assertNotEqual(result['state'], 'passed')
                    if case.endswith('_awake_quiet'):
                        self.assertEqual(
                            result['phases'][2]['reason'], 'suite_awake_dialogue_quiet_incomplete'
                        )
                    if case.endswith('_timing'):
                        self.assertEqual(
                            result['phases'][attempted - 1]['reason'],
                            'suite_response_playout_timing_incomplete',
                        )
                    if case == 'missing_awake_stimulus':
                        self.assertEqual(
                            result['phases'][2]['reason'], 'suite_synthetic_stimulus_not_confirmed'
                        )
                if case == 'source_changed':
                    runs[1].run.assert_not_called()
                    self.assertEqual(result['phases'][1]['reason'], 'suite_source_changed')

    def test_qa_latency_dry_plan_has_thirty_samples_and_no_device_operations(self):
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as request,
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--qa-latency-suite']), 0
            )
            device.assert_not_called()
            request.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertFalse(plan['execute'])
        self.assertEqual(len(plan['phases']), 30)
        self.assertEqual(plan['phases'][0]['name'], 'qa_01')
        self.assertEqual(plan['phases'][-1]['name'], 'qa_30')
        self.assertEqual(plan['qa_corpus_sha256'], QA_CORPUS_SHA256)
        self.assertEqual(len(set(QA_LATENCY_QUESTIONS)), 30)
        self.assertNotIn('logical_wifi_handover', plan['phases'][0]['checks'])
        self.assertNotIn('announcement_and_playout', plan['phases'][0]['checks'])

    def test_qa_acoustic_latency_plan_declares_the_new_capability_without_contacting_device(self):
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as request,
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        '--runtime-root',
                        '/unused',
                        '--capture',
                        '--qa-latency-suite',
                        '--response-acoustic-evidence',
                    ]
                ),
                0,
            )
            device.assert_not_called()
            request.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['response_acoustic_evidence'])
        self.assertEqual(plan['minimum_backend_version'], '0.14.57')
        self.assertEqual(plan['qa_corpus_sha256'], QA_CORPUS_SHA256)
        self.assertEqual(len(plan['phases']), 30)

    def test_qa_latency_batch_counts_only_complete_samples_and_never_retries(self):
        for fail_at, acoustic, bad_interval in (
            (None, False, None),
            (4, False, None),
            (None, True, None),
            (4, True, True),
            (4, True, 20001),
            (4, True, float('nan')),
        ):
            runs, questions = [], []

            def make_run(options, checkpoint, *, qa_question):
                self.assertEqual(options, set(QA_LATENCY_PHASES[0][1]))
                questions.append(qa_question)
                index = len(runs) + 1
                stages = dict.fromkeys(QA_STAGE_METRICS, 100)
                if index % 2:
                    stages[QA_ENDPOINT_METRIC] = 800
                if index == fail_at and not acoustic:
                    stages.pop(QA_STAGE_METRICS[0])
                report = {
                    'state': 'passed',
                    'checks': [dict(name=name, state='passed') for name in planned_checks(options)],
                    'cleanup_microphone_off_verified': True,
                    'acoustic_evidence': {
                        'post_playback_quiet_seconds': 20,
                        'recognized_stimulus_matches': True,
                        'qa_stage_latencies_ms': stages,
                        'response_playout_timing': {
                            'status': 'observed',
                            QA_PLAYOUT_METRIC: 100 * index,
                        },
                    },
                }
                if acoustic:
                    report['acoustic_evidence']['response_acoustic_timing'] = {
                        'status': 'observed',
                        ACOUSTIC_RESPONSE_METRIC: bad_interval if index == fail_at else 150 * index,
                    }
                run = Mock(source_sha256='source', speech_source_sha256='voice')
                run.run.side_effect = lambda: checkpoint(report) or report
                runs.append(run)
                return run

            result = run_device_suite(
                make_run, lambda _: None, qa_latency=True, response_acoustic=acoustic
            )
            self.assertFalse(result['audible_onset_measured'])
            self.assertFalse(result['full_p0_acceptance'])
            self.assertEqual(result['scope'], 'synthetic_qa_latency')
            self.assertEqual(len(runs), fail_at or 30)
            self.assertEqual(questions, list(QA_LATENCY_QUESTIONS[: fail_at or 30]))
            self.assertEqual(result['qa_corpus_sha256'], QA_CORPUS_SHA256)
            stats = result['latencies_ms'][QA_PLAYOUT_METRIC]
            self.assertEqual(
                result['latencies_ms'][QA_ENDPOINT_METRIC],
                {'samples': 2 if fail_at else 15, 'p50': 800, 'p95': 800},
            )
            if fail_at:
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(
                    result['phases'][3]['reason'],
                    'suite_qa_acoustic_timing_incomplete'
                    if acoustic
                    else 'suite_qa_stage_timing_incomplete',
                )
                self.assertTrue(all(phase['state'] == 'not_run' for phase in result['phases'][4:]))
                self.assertEqual(stats, {'samples': 3, 'p50': 200, 'p95': 290})
            else:
                self.assertEqual(result['state'], 'passed')
                self.assertEqual(stats, {'samples': 30, 'p50': 1550, 'p95': 2855})
            if acoustic:
                self.assertTrue(result['response_acoustic_evidence'])
                self.assertEqual(
                    result['latencies_ms'][ACOUSTIC_RESPONSE_METRIC],
                    {'samples': 3, 'p50': 300, 'p95': 435}
                    if fail_at
                    else {'samples': 30, 'p50': 2325, 'p95': 4282.5},
                )
            else:
                self.assertNotIn(ACOUSTIC_RESPONSE_METRIC, result['latencies_ms'])

    def test_fixed_suite_rejects_legacy_backend_before_device_access(self):
        from dataclasses import replace
        from release_info import RELEASE

        legacy = replace(RELEASE, backend_version='0.14.49')
        health = dict(
            bridge_version=legacy.backend_version,
            protocol_version=legacy.protocol_version,
            release_id=legacy.release_id,
            runtime_instance='test',
        )
        with (
            patch('device_smoke.load_release', return_value=legacy),
            patch('device_smoke.rpc', return_value=health),
            patch('device_smoke.Device') as device,
            patch('device_smoke.private_directory') as output,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as stopped,
        ):
            main(['--runtime-root', str(Path.cwd()), '--capture', '--p0-device-suite', '--execute'])
        self.assertEqual(stopped.exception.code, 2)
        device.assert_not_called()
        output.assert_not_called()

    def test_acoustic_latency_batch_rejects_missing_support_before_device_access(self):
        from release_info import RELEASE

        health = dict(
            bridge_version=RELEASE.backend_version,
            protocol_version=RELEASE.protocol_version,
            release_id=RELEASE.release_id,
            runtime_instance='test',
        )
        errors = io.StringIO()
        with (
            patch('device_smoke.load_release', return_value=RELEASE),
            patch('device_smoke.rpc', return_value=health) as request,
            patch('device_smoke.Device') as device,
            patch('device_smoke.private_directory') as output,
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as stopped,
        ):
            main(
                [
                    '--runtime-root',
                    str(Path.cwd()),
                    '--capture',
                    '--qa-latency-suite',
                    '--response-acoustic-evidence',
                    '--execute',
                ]
            )
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn('当前后台未声明支持问答声学计时', errors.getvalue())
        self.assertEqual(request.call_count, 1)
        device.assert_not_called()
        output.assert_not_called()

    def test_suite_keeps_one_runtime_lock_and_rejects_instance_change(self):
        import device_smoke
        from release_info import RELEASE

        snapshot = dict(
            ok=True,
            bridge_version=RELEASE.backend_version,
            protocol_version=RELEASE.protocol_version,
            release_id=RELEASE.release_id,
            runtime_instance='first-private-instance',
        )
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            calls = []

            def query(_path, payload, _timeout=5):
                self.assertEqual(payload, {'operation': 'health'})
                return dict(
                    snapshot,
                    runtime_instance='new-instance'
                    if len(calls) == 2
                    else snapshot['runtime_instance'],
                )

            def smoke(device, request, checkpoint, expected_apk, **options):
                calls.append(device)
                with (directory / 'automation-test.lock').open('rb') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                run = Mock(source_sha256='source', speech_source_sha256='speech')

                def run_phase():
                    try:
                        request({'operation': 'health'})
                    except RuntimeChanged as error:
                        return dict(state='failed', checks=[{'reason': str(error)}])
                    return dict(
                        state='passed',
                        cleanup_microphone_off_verified=True,
                        checks=[
                            dict(name=name, state='passed') for name in planned_checks(set(options))
                        ],
                    )

                run.run.side_effect = run_phase
                return run

            with (
                patch('device_smoke.rpc', side_effect=query),
                patch('device_smoke.private_directory', return_value=directory),
                patch('device_smoke.Device') as device,
                patch('device_smoke.SmokeRun', side_effect=smoke),
                patch('device_smoke.sys.platform', 'linux'),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            '--runtime-root',
                            str(device_smoke.ROOT),
                            '--capture',
                            '--p0-device-suite',
                            '--execute',
                        ]
                    ),
                    2,
                )
                device.assert_called_once()
            self.assertEqual(len(calls), 2)
            self.assertIs(calls[0], calls[1])
            report = next(directory.glob('device-suite-*.json')).read_text()
            self.assertNotIn('first-private-instance', report)
            result = json.loads(report)
            self.assertEqual(
                result['phases'][1]['report']['checks'][0]['reason'], 'suite_runtime_changed'
            )
            self.assertEqual(result['phases'][2]['state'], 'not_run')

    def test_awake_dialogue_plan_never_calls_device_and_is_separate_from_standby(self):
        plan_output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as query,
            redirect_stdout(plan_output),
        ):
            self.assertEqual(
                main(['--runtime-root', '/nonexistent', '--capture', '--awake-dialogue']), 0
            )
            device.assert_not_called()
            query.assert_not_called()
        plan = json.loads(plan_output.getvalue())
        self.assertTrue(plan['awake_dialogue'])
        self.assertNotIn('real_idle_black_screen', plan['checks'])
        self.assertEqual(
            plan['checks'][-3:],
            [
                'synthetic_awake_dialogue',
                'home_stops_capture',
                'maintenance_reopen_keeps_microphone_off',
            ],
        )

    def test_maintenance_reports_connection_loss_separately_from_capture(self):
        for connected, capture, reason in (
            (False, False, 'maintenance_connection_lost'),
            (True, True, 'maintenance_open_started_capture'),
        ):
            run, _, health, _ = self.fixture()

            def changed_during_stability(seconds):
                health.update(connected=connected, microphone_active=capture)

            run.sleep = changed_during_stability
            with self.subTest(reason=reason), self.assertRaisesRegex(CheckFailure, reason):
                run.maintenance_open()

    def test_capture_refuses_legacy_device_before_any_open_or_playback(self):
        run, device, _, _ = self.fixture(capture=True)
        device.status.return_value[0].pop('test_capture_limit_supported')
        run.playback = Mock()
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['checks'][0]['reason'], 'device_test_capture_limit_unsupported')
        device.open.assert_not_called()
        run.playback.assert_not_called()

    def test_direct_capture_open_requires_supported_bounded_integer_limit(self):
        for invalid in (0, -1, 999, 1500001, True, 1.5, '1000', None):
            with self.subTest(limit=invalid):
                device = Device.__new__(Device)
                device.shell = Mock()
                device.status = Mock(return_value=({'test_capture_limit_supported': True}, {}))
                with self.assertRaisesRegex(CheckFailure, 'invalid_test_capture_limit'):
                    device.open(True, capture_limit_ms=invalid)
                device.shell.assert_not_called()
        device = Device.__new__(Device)
        device.shell = Mock()
        device.status = Mock(return_value=({}, {}))
        with self.assertRaisesRegex(CheckFailure, 'device_test_capture_limit_unsupported'):
            device.open(True)
        device.shell.assert_not_called()
        device.status.return_value = (
            {'test_capture_limit_supported': True, 'microphone_active': True},
            {},
        )
        with self.assertRaisesRegex(CheckFailure, 'device_microphone_already_active'):
            device.open(True)
        device.shell.assert_not_called()

    def test_capture_limit_is_long_enough_for_actual_standby_but_does_not_extend_on_reconnect(self):
        for standby, limit in ((False, 180000), (True, 1200000)):
            with self.subTest(standby=standby):
                run, device, health, _ = self.fixture(capture=True, standby=standby)
                health.update(microphone_active=True, local_wake_ready=True)
                device.status.return_value[0]['microphone_active'] = True
                device.status.return_value[0].update(
                    test_capture_limit_ms=limit, test_capture_remaining_ms=limit - 7000
                )
                run.start_capture()
                device.open.assert_called_once_with(True, capture_limit_ms=limit)
                # Ordinary health/route observation never requests a new lease.
                run.health()
                self.assertEqual(device.open.call_count, 1)

    def test_failed_start_records_independent_microphone_states_before_cleanup(self):
        for device_active in (True, False):
            with self.subTest(device_active=device_active):
                run, device, _, _ = self.fixture(capture=True)
                device.status.return_value[0].update(
                    microphone_active=device_active,
                    phase='calibrating' if device_active else 'error',
                    mic_frames=0,
                )
                with self.assertRaisesRegex(CheckFailure, 'capture_not_started'):
                    run.start_capture()
                result = run.report('failed')['capture_start_failure']
                self.assertEqual(
                    result,
                    {
                        'host_microphone_active': False,
                        'device_microphone_active': device_active,
                        'device_phase': 'calibrating' if device_active else 'error',
                    },
                )
                device.open.assert_called_once_with(True, capture_limit_ms=180000)
                device.home.assert_not_called()

    def test_failure_evidence_unavailable_does_not_replace_the_original_failure(self):
        run, device, _, _ = self.fixture(capture=True)
        device.status.side_effect = OSError('PRIVATE_DEVICE_OUTPUT')
        with self.assertRaisesRegex(CheckFailure, 'capture_not_started'):
            run.start_capture()
        result = run.report('failed')['capture_start_failure']
        self.assertEqual(result['host_microphone_active'], False)
        self.assertIsNone(result['device_microphone_active'])
        self.assertIsNone(result['device_phase'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_claimed_support_does_not_replace_actual_active_limit_receipt(self):
        for limit, remaining in (
            (None, None),
            (180000.0, 1000),
            (1000, 900),
            (180000, 0),
            (180000, 180001),
            (180000, True),
            (180000, 10.5),
        ):
            with self.subTest(limit=limit, remaining=remaining):
                run, device, health, _ = self.fixture(capture=True)
                health.update(microphone_active=True, local_wake_ready=True)
                device.status.return_value[0].update(
                    microphone_active=True,
                    test_capture_limit_ms=limit,
                    test_capture_remaining_ms=remaining,
                )
                with self.assertRaisesRegex(CheckFailure, 'device_test_capture_limit_not_active'):
                    run.start_capture()

    def test_preflight_transport_errors_are_redacted_by_cli(self):
        import device_smoke
        from release_info import RELEASE

        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                'device_smoke.rpc',
                return_value={
                    'bridge_version': RELEASE.backend_version,
                    'protocol_version': RELEASE.protocol_version,
                },
            ),
            patch('device_smoke.private_directory', return_value=Path(folder)),
            patch('device_smoke.Device', side_effect=RuntimeError('PRIVATE_DEVICE_OUTPUT')),
            redirect_stderr(io.StringIO()) as error,
        ):
            with self.assertRaises(SystemExit) as stopped:
                main(['--runtime-root', str(device_smoke.ROOT), '--execute'])
            self.assertEqual(stopped.exception.code, 2)
            self.assertNotIn('PRIVATE_DEVICE_OUTPUT', error.getvalue())

    def test_preflight_busy_owner_or_camera_never_mutates_device(self):
        for camera in (False, True):
            run, device, health, _ = self.fixture()
            if camera:
                d, ui = device.status.return_value
                ui['camera_open'] = True
            else:
                health['microphone_active'] = True
            result = run.run()
            self.assertEqual(result['state'], 'failed')
            device.open.assert_not_called()
            device.home.assert_not_called()

    def test_replaced_runtime_does_not_pass_checks(self):
        run, _, health, _ = self.fixture()
        run.preflight()
        health['runtime_instance'] = 'different-instance'
        with self.assertRaisesRegex(RuntimeChanged, 'runtime_restarted'):
            run.health()

    def test_cleanup_never_sends_home_to_a_replacement_runtime(self):
        for observed_before_cleanup in (False, True):
            with self.subTest(observed_before_cleanup=observed_before_cleanup):
                run, device, health, _ = self.fixture()
                previous = health['runtime_instance']

                def failure():
                    health['runtime_instance'] = 'PRIVATE_REPLACEMENT'
                    if observed_before_cleanup:
                        try:
                            run.health()
                        finally:
                            # A later matching response cannot restore test ownership.
                            health['runtime_instance'] = previous
                    raise CheckFailure('playback_failure')

                run.playback = failure
                report = run.run()
                device.home.assert_not_called()
                device.open.assert_called_once_with(False)
                self.assertEqual(report['state'], 'cleanup_unconfirmed')
                self.assertFalse(report['cleanup_microphone_off_verified'])
                self.assertEqual(report['cleanup_reason'], 'runtime_restarted_during_test')
                self.assertEqual(
                    report['checks'][-1]['reason'],
                    'runtime_restarted_during_test'
                    if observed_before_cleanup
                    else 'playback_failure',
                )
                self.assertNotIn('PRIVATE_REPLACEMENT', json.dumps(report))

    def test_cleanup_does_not_reopen_when_runtime_changes_after_home(self):
        run, device, health, _ = self.fixture()
        run.playback = Mock(side_effect=CheckFailure('playback_failure'))

        def home():
            health['runtime_instance'] = 'replacement'

        device.home.side_effect = home
        report = run.run()
        device.home.assert_called_once()
        device.open.assert_called_once_with(False)
        self.assertEqual(report['state'], 'cleanup_unconfirmed')
        self.assertEqual(report['cleanup_reason'], 'runtime_restarted_during_test')

    def test_temporary_health_failure_does_not_prevent_emergency_home(self):
        run, device, _, _ = self.fixture()
        request = run.request

        def failure():
            def cleanup_request(payload):
                if not device.home.called and payload == {'operation': 'health'}:
                    raise ConnectionError('PRIVATE_CONNECTION_FAILURE')
                return request(payload)

            run.request = cleanup_request
            raise CheckFailure('playback_failure')

        run.playback = failure
        report = run.run()
        device.home.assert_called_once()
        self.assertEqual(device.open.call_count, 2)
        self.assertEqual(report['state'], 'failed')
        self.assertTrue(report['cleanup_microphone_off_verified'])
        self.assertNotIn('PRIVATE_CONNECTION_FAILURE', json.dumps(report))

    def test_unsupported_acoustic_observer_aborts_before_ui_or_capture(self):
        run, device, health, _ = self.fixture(capture=True, playback_evidence=True)
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['checks'][0]['reason'], 'backend_playback_evidence_unsupported')
        device.open.assert_not_called()
        device.home.assert_not_called()

    def test_capture_clock_requirement_refuses_old_apk_before_capture(self):
        run, device, _, _ = self.fixture(capture=True, capture_clock_evidence=True)
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['checks'][0]['reason'], 'device_capture_clock_evidence_unsupported')
        device.open.assert_not_called()
        device.home.assert_not_called()

    def test_capture_clock_requires_valid_advancing_hardware_receipts(self):
        report = {
            'samples': 4,
            'available': 2,
            'unavailable': 1,
            'invalid': 0,
            'duplicate': 1,
            'discontinuities': 0,
            'min_offset_ns': -10_000_000,
            'max_offset_ns': 10_000_000,
        }
        for value, expected in (
            (None, 'missing'),
            ({**report, 'samples': 5}, 'malformed'),
            ({**report, 'available': True}, 'malformed'),
            ({**report, 'max_offset_ns': -11_000_000}, 'malformed'),
            ({**report, 'min_offset_ns': -2_000_000_001}, 'malformed'),
            ({**report, 'unexpected': 'device data'}, 'malformed'),
            ({**report, 'invalid': 1, 'duplicate': 0}, 'unconfirmed'),
            ({**report, 'discontinuities': 1, 'duplicate': 0}, 'unconfirmed'),
            ({**report, 'available': 1, 'unavailable': 2}, 'unconfirmed'),
            (report, 'observed'),
        ):
            with self.subTest(expected=expected, value=value):
                evidence = summarize_capture_clock(value)
                self.assertEqual(evidence['status'], expected)
                if expected == 'malformed':
                    self.assertEqual(evidence, {'status': 'malformed'})

    def test_bounded_capture_saves_numeric_evidence_even_if_clock_unavailable(self):
        run, device, health, _ = self.fixture(capture=True, capture_clock_evidence=True)
        health.update(microphone_active=True, local_wake_ready=True)
        sample = {
            'samples': 6,
            'available': 0,
            'unavailable': 6,
            'invalid': 0,
            'duplicate': 0,
            'discontinuities': 0,
        }
        device.status.return_value[0].update(
            microphone_active=True,
            test_capture_limit_ms=180000,
            test_capture_remaining_ms=170000,
            capture_clock_evidence=sample,
        )
        status = run.start_capture()
        with self.assertRaisesRegex(CheckFailure, 'capture_clock_provenance_unconfirmed'):
            run.capture_clock_probe(status)
        self.assertEqual(
            run.report('failed')['capture_clock_evidence'],
            {
                'status': 'unconfirmed',
                **sample,
            },
        )
        sample.update(available=2, unavailable=4, min_offset_ns=0, max_offset_ns=10)
        run.capture_clock_probe(status)
        self.assertEqual(run.report('passed')['capture_clock_evidence']['status'], 'observed')

    def test_clock_requirement_refuses_old_apk_before_capture(self):
        run, device, health, _ = self.fixture(
            capture=True, playback_evidence=True, clock_evidence=True
        )
        health['playback_evidence_supported'] = True
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['checks'][0]['reason'], 'device_render_clock_evidence_unsupported')
        device.open.assert_not_called()
        device.home.assert_not_called()

    def arm_fixture_capture(self, run, device, health):
        health.update(microphone_active=True, local_wake_ready=True, waiting_for_wake=True)
        device.status.return_value[0].update(
            microphone_active=True,
            test_capture_limit_ms=run.capture_limit_ms,
            test_capture_remaining_ms=run.capture_limit_ms - 1000,
        )

    def test_clock_requirement_does_not_treat_missing_evidence_as_zero(self):
        run, device, health, _ = self.fixture(clock_evidence=True)
        self.arm_fixture_capture(run, device, health)
        request = run.request
        evidence = {'status': 'observed', 'render_clock_evidence': {'status': 'missing'}}
        run.request = lambda p: (
            {'status': 'completed', 'evidence': evidence}
            if p == {'operation': 'playback_probe'}
            else request(p)
        )
        run.playback_probe()
        with self.assertRaisesRegex(CheckFailure, 'render_clock_provenance_unconfirmed'):
            run.render_clock_probe()
        evidence['render_clock_evidence']['status'] = 'observed'
        run.render_clock_probe()

    def test_leading_acoustic_requirement_refuses_old_backend_before_any_open(self):
        run, device, health, _ = self.fixture(
            capture=True,
            playback_evidence=True,
            leading_acoustic_evidence=True,
        )
        health['playback_evidence_supported'] = True
        result = run.run()
        self.assertEqual(
            result['checks'][0]['reason'], 'backend_leading_acoustic_evidence_unsupported'
        )
        device.open.assert_not_called()
        device.home.assert_not_called()

    def test_leading_acoustic_requires_bounded_numeric_match(self):
        leading = {
            'status': 'observed',
            'block_ms': 20,
            'consecutive_blocks': 2,
            'minimum_correlation': 0.81,
            'reference_block_start_ms': 80,
            'capture_minus_render_origin_ms': 200.0,
        }
        for change in (
            {'minimum_correlation': True},
            {'minimum_correlation': 0.54},
            {'capture_minus_render_origin_ms': float('nan')},
            {'capture_minus_render_origin_ms': 8888},
            {'capture_minus_render_origin_ms': 10**1000},
            {'reference_block_start_ms': 19},
            {'block_ms': 20.0},
            {'consecutive_blocks': 1},
        ):
            with self.subTest(change=change):
                self.assertFalse(valid_leading_evidence({**leading, **change}, 120))
        self.assertTrue(valid_leading_evidence(leading, 120))
        self.assertFalse(valid_leading_evidence(leading, 100))
        self.assertFalse(valid_leading_evidence(leading, None))

        run, device, health, _ = self.fixture(leading_acoustic_evidence=True)
        self.arm_fixture_capture(run, device, health)
        request = run.request
        evidence = {'status': 'observed', 'matched_window_delay_ms': 120, 'render_clock_resets': 0}
        run.request = lambda payload: (
            {'status': 'completed', 'evidence': evidence}
            if payload == {'operation': 'playback_probe'}
            else request(payload)
        )
        run.playback_probe()
        with self.assertRaisesRegex(CheckFailure, 'leading_acoustic_evidence_unconfirmed'):
            run.leading_acoustic_probe()
        evidence['leading_acoustic_evidence'] = leading
        run.leading_acoustic_probe()
        self.assertEqual(
            run.report('passed')['playback_probe_evidence']['leading_acoustic_evidence'], leading
        )

    def test_playback_probe_keeps_failed_numeric_evidence(self):
        run, device, health, _ = self.fixture(capture=True, playback_evidence=True)
        self.arm_fixture_capture(run, device, health)
        request = run.request
        evidence = {'schema': 1, 'status': 'not_observed', 'correlation': 0.02}
        run.request = lambda p: (
            {'status': 'completed', 'evidence': evidence}
            if p == {'operation': 'playback_probe'}
            else request(p)
        )
        with self.assertRaisesRegex(CheckFailure, 'not_acoustically_corroborated'):
            run.playback_probe()
        self.assertEqual(run.report('failed')['playback_probe_evidence'], evidence)
        evidence = {'schema': 1, 'status': 'observed', 'correlation': 0.9}
        run.playback_probe()
        self.assertEqual(run.report('passed')['playback_probe_evidence']['status'], 'observed')
        device.open.assert_not_called()

    def test_device_version_mismatch_is_rejected_before_mutation(self):
        run, device, _, _ = self.fixture(expected_apk=('0.8.8', 33))
        device.version.return_value = ('0.8.6', 31)
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['checks'][0]['reason'], 'device_source_version_mismatch')
        device.open.assert_not_called()
        device.home.assert_not_called()

    def test_maintenance_open_cannot_hide_auto_started_capture(self):
        run, device, health, _ = self.fixture()
        run.preflight()

        def opened(capture):
            self.assertFalse(capture)
            health['microphone_active'] = True

        device.open.side_effect = opened
        with self.assertRaises(CheckFailure):
            run.maintenance_open()

    def test_home_requires_stopped_capture_and_stable_frame_count(self):
        run, device, health, _ = self.fixture()
        run.preflight()
        run.home()
        device.home.assert_called_once()
        original_sleep = run.sleep

        def advancing_frames(seconds):
            original_sleep(seconds)
            health['mic_frames'] += 160

        run.sleep = advancing_frames
        with self.assertRaisesRegex(CheckFailure, 'frames_increasing'):
            run.home()

    def test_home_refuses_to_confirm_if_only_host_stopped_or_device_keeps_producing_frames(self):
        for defect in ('device_still_active', 'device_frames_increasing'):
            with self.subTest(defect=defect):
                run, device, _, now = self.fixture()
                d, _ = device.status.return_value
                d['microphone_active'] = defect == 'device_still_active'
                sleep = run.sleep

                def advance(seconds):
                    sleep(seconds)
                    d['mic_frames'] += 160

                if defect == 'device_frames_increasing':
                    run.sleep = advance
                with self.assertRaisesRegex(
                    CheckFailure,
                    'home_did_not_stop_capture'
                    if defect == 'device_still_active'
                    else 'home_microphone_frames_increasing',
                ):
                    run.home()
                self.assertEqual(now[0], 15 if defect == 'device_still_active' else 2)
                device.home.assert_called_once()
                device.open.assert_not_called()

    def test_disabled_capture_plan_never_opens_recording(self):
        run, device, _, _ = self.fixture()
        run.playback = Mock()
        run.handover = Mock()
        result = run.run()
        self.assertEqual(result['state'], 'passed')
        self.assertTrue(result['cleanup_microphone_off_verified'])
        self.assertTrue(all(call.args == (False,) for call in device.open.call_args_list))
        self.assertEqual(len(result['checks']), 4)
        self.assertFalse(result['physical_usb_unplug_tested'])
        self.assertFalse(result['full_p0_acceptance'])

    def test_failure_and_keyboard_interrupt_cleanup_without_recording_retry(self):
        for error in (RuntimeError('PRIVATE_PROVIDER_BODY'), KeyboardInterrupt()):
            run, device, _, _ = self.fixture(capture=True)

            def failure():
                run.device.open(True)
                raise error

            run.playback = failure
            result = run.run()
            self.assertIn(result['state'], ('failed', 'interrupted'))
            self.assertTrue(result['cleanup_microphone_off_verified'])
            device.home.assert_called_once()
            self.assertEqual(device.open.call_args_list[-1].args, (False,))
            self.assertEqual(sum(call.args == (True,) for call in device.open.call_args_list), 1)
            self.assertNotIn('PRIVATE_PROVIDER_BODY', json.dumps(result))

    def evidence_run_fixture(self, probe_payload, *, lose_capture=False, lose_lease=False):
        run, device, health, now = self.fixture(
            capture=True,
            standby=True,
            acoustic=True,
            playback_evidence=True,
            capture_clock_evidence=True,
            clock_evidence=True,
            leading_acoustic_evidence=True,
        )
        health.update(
            playback_evidence_supported=True,
            leading_acoustic_evidence_supported=True,
            render_clock_evidence_supported=True,
        )
        d, ui = device.status.return_value
        d['capture_clock_evidence_supported'] = True
        run.playback = Mock()
        run.handover = Mock()
        run.acoustic_probe = Mock()
        probe_calls = []
        started = [None]

        def open_app(capture, **_):
            if capture:
                started[0] = now[0]
                self.arm_fixture_capture(run, device, health)
            d['foreground'] = True

        def home():
            d.update(foreground=False, microphone_active=False)
            health['microphone_active'] = False

        def status():
            asleep = bool(
                health['microphone_active']
                and started[0] is not None
                and now[0] - started[0] >= 900
            )
            return d, {**ui, 'screen_sleeping': asleep}

        request = run.request

        def rpc(payload):
            if payload == {'operation': 'playback_probe'}:
                probe_calls.append(payload)
                if lose_capture:
                    d['microphone_active'] = health['microphone_active'] = False
                if lose_lease:
                    d['test_capture_limit_ms'] = 0
                return probe_payload
            return request(payload)

        device.open.side_effect = open_app
        device.home.side_effect = home
        device.status.side_effect = status
        run.request = rpc
        return run, device, probe_calls

    def test_unconfirmed_evidence_is_separate_and_does_not_repeat_stimulus_or_skip_standby(self):
        run, device, probes = self.evidence_run_fixture(
            {
                'status': 'completed',
                'evidence': {
                    'status': 'not_observed',
                    'render_clock_evidence': {'status': 'observed'},
                },
            }
        )
        result = run.run()
        states = {row['name']: row['state'] for row in result['checks']}
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['cleanup_microphone_off_verified'])
        self.assertEqual(states['explicit_open_starts_capture'], 'passed')
        self.assertEqual(states['recording_clock_provenance'], 'failed')
        self.assertEqual(states['speaker_microphone_acoustic_evidence'], 'failed')
        self.assertEqual(states['playback_clock_provenance'], 'passed')
        self.assertEqual(states['leading_playback_acoustic_evidence'], 'failed')
        self.assertEqual(states['home_stops_capture'], 'passed')
        self.assertEqual(states['real_idle_black_screen'], 'passed')
        self.assertEqual(states['synthetic_acoustic_wake_and_response'], 'passed')
        self.assertEqual(len(probes), 1)
        self.assertEqual(sum(c.args == (True,) for c in device.open.call_args_list), 2)
        run.acoustic_probe.assert_called_once()

    def test_lost_capture_or_incomplete_playback_stops_before_followup_and_cleans_up(self):
        for payload, lost, reason in (
            (
                {'status': 'completed', 'evidence': {'status': 'not_observed'}},
                True,
                'playback_probe_requires_idle_capture',
            ),
            (
                {'status': 'timeout', 'evidence': {'status': 'observed'}},
                False,
                'playback_probe_not_completed',
            ),
        ):
            with self.subTest(reason=reason):
                run, device, probes = self.evidence_run_fixture(payload, lose_capture=lost)
                result = run.run()
                self.assertEqual(result['state'], 'failed')
                self.assertTrue(result['cleanup_microphone_off_verified'])
                self.assertEqual(result['checks'][-1]['reason'], reason)
                self.assertEqual(len(probes), 1)
                self.assertEqual(sum(c.args == (True,) for c in device.open.call_args_list), 1)
                run.acoustic_probe.assert_not_called()

    def test_unbounded_recording_after_probe_blocks_standby_followup(self):
        run, _, probes = self.evidence_run_fixture(
            {'status': 'completed', 'evidence': {'status': 'not_observed'}},
            lose_lease=True,
        )
        result = run.run()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['checks'][-1]['reason'], 'device_test_capture_limit_not_active')
        self.assertTrue(result['cleanup_microphone_off_verified'])
        self.assertEqual(len(probes), 1)
        run.acoustic_probe.assert_not_called()

    def test_failed_cleanup_cannot_be_reported_as_pass(self):
        run, device, _, _ = self.fixture()
        run.playback = Mock(side_effect=CheckFailure('injected_failure'))
        device.home.side_effect = OSError('PRIVATE_BODY')
        result = run.run()
        self.assertEqual(result['state'], 'cleanup_unconfirmed')
        self.assertFalse(result['cleanup_microphone_off_verified'])
        self.assertNotIn('PRIVATE_BODY', json.dumps(result))

    def test_playout_requires_fresh_completed_receipt_not_old_success(self):
        for fresh in (False, True):
            run, device, health, _ = self.fixture()
            row = {
                'request_id': 'old',
                'origin': 'announcement',
                'outcome': 'completed',
                'stages': {'playout_ack': 1, 'playback_done': 2},
            }
            count = [0]

            def request(p):
                if p.get('operation') == 'diagnostics':
                    count[0] += 1
                    return {
                        'requests': [row]
                        + ([{**row, 'request_id': 'new'}] if count[0] > 1 and fresh else [])
                    }
                if 'text' in p:
                    return {'status': 'completed'}
                return dict(health)

            run.request = request
            # UI polling must also be observed, so even a fresh receipt alone
            # cannot establish the caption/layout checks.
            with self.assertRaises(CheckFailure):
                run.playback()

    def test_route_switch_cannot_rearm_microphone(self):
        run, _, health, _ = self.fixture()
        run.preflight()

        def request(p):
            if p.get('operation') == 'transport_test':
                health.update(transport='wifi', microphone_active=True)
                return {'ok': True}
            return dict(health)

        run.request = request
        with self.assertRaisesRegex(CheckFailure, 'handover_started_capture'):
            run.handover()

    def test_active_handover_plan_and_legacy_preflight(self):
        output = io.StringIO()
        with patch('device_smoke.Device') as device, redirect_stdout(output):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--active-handover']), 0
            )
            device.assert_not_called()
        self.assertIn(ACTIVE_HANDOVER_CHECK, json.loads(output.getvalue())['checks'])
        run, device, health, _ = self.fixture(capture=True, active_handover=True)
        health.update(local_wake_count=0, voice_asr_attempts=0, render_frames=0)
        report = run.run()
        self.assertEqual(report['checks'][-1]['reason'], 'device_test_capture_epoch_unsupported')
        device.open.assert_not_called()

    def test_all_route_loss_plan_is_offline(self):
        output = io.StringIO()
        with patch('device_smoke.Device') as device, redirect_stdout(output):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--all-route-loss']), 0
            )
            device.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['checks'][-1], ALL_ROUTE_LOSS_CHECK)

    def test_all_route_loss_requires_stop_and_idle_reconnect_on_both_ends(self):
        for case, expected in (
            ('valid', None),
            ('device_still_recording', 'all_route_loss_stop_unconfirmed'),
            ('host_still_recording', 'all_route_loss_stop_unconfirmed'),
            ('reconnect_active', 'all_route_loss_reconnect_started_audio'),
            ('native_frames_changed', 'all_route_loss_device_reopened_capture'),
            ('native_restart', 'all_route_loss_device_reopened_capture'),
            ('new_request', 'all_route_loss_unexpected_request'),
        ):
            with self.subTest(case=case):
                run, device, health, now = self.fixture(capture=True, all_route_loss=True)
                health.update(
                    microphone_active=True,
                    waiting_for_wake=True,
                    local_wake_count=0,
                    voice_asr_attempts=0,
                    render_frames=40,
                )
                run.start_capture = Mock()
                suppressed = False
                rows = []
                d, ui = device.status.return_value

                def down():
                    return suppressed and now[0] < 10

                def status():
                    active = not suppressed or down() and case == 'device_still_recording'
                    values = dict(
                        d,
                        microphone_active=active,
                        connected=not down(),
                        transport='none' if down() else 'usb',
                        test_capture_limit_ms=180000 if active else 0,
                        test_capture_remaining_ms=160000 if active else 0,
                        mic_frames=2000
                        + int(case == 'native_frames_changed' and suppressed and not down()),
                    )
                    if active:
                        values['test_capture_epoch'] = 3
                    restarted = suppressed and not down() and case == 'native_restart'
                    return values, dict(
                        ui, auto_voice_pending=False, auto_voice_start_attempts=2 + int(restarted)
                    )

                def request(payload):
                    nonlocal suppressed
                    if payload == ALL_ROUTE_LOSS_REQUEST:
                        suppressed = True
                        if case == 'new_request':
                            rows.append(
                                dict(request_id='private-id', origin='text', outcome='completed')
                            )
                        return {'ok': True, 'seconds': 10}
                    if payload == {'operation': 'transport_status'}:
                        return dict(
                            transport='none' if down() else 'usb',
                            usb_ready=not down(),
                            wifi_ready=not down(),
                            all_routes_test_suppressed=down(),
                            usb_test_suppressed=down(),
                        )
                    if payload == {'operation': 'diagnostics'}:
                        return {'requests': rows}
                    self.assertEqual(payload, {'operation': 'health'})
                    active = not suppressed or (
                        case == 'host_still_recording' if down() else case == 'reconnect_active'
                    )
                    return dict(
                        health,
                        microphone_active=active,
                        connected=not down(),
                        transport='none' if down() else 'usb',
                    )

                run.request = request
                device.status.side_effect = status
                if expected:
                    with self.assertRaisesRegex(CheckFailure, expected):
                        run.all_routes_loss_probe()
                else:
                    run.all_routes_loss_probe()
                    report = run.report('passed')
                    self.assertEqual(
                        report['all_route_loss_evidence'],
                        dict(
                            both_routes_down=True,
                            capture_stopped=True,
                            usb_priority_restored=True,
                            post_reconnect_mic_off_seconds=5,
                        ),
                    )
                    self.assertFalse(report['physical_usb_unplug_tested'])
                run.start_capture.assert_called_once()
                device.speak_probe.assert_not_called()

    def test_active_handover_requires_fresh_frames_and_the_original_capture(self):
        for case, reason in (
            ('valid', None),
            ('no_frames', 'active_handover_no_new_mic_frames'),
            ('new_epoch', 'active_handover_capture_restarted'),
            ('extended_deadline', 'active_handover_capture_deadline_changed'),
            ('mic_off', 'active_handover_capture_changed'),
            ('recalibrated', 'active_handover_capture_changed'),
            ('extra_switch', 'active_handover_unexpected_switch_count'),
            ('old_playback', 'active_handover_unexpected_audio_activity'),
            ('extra_task', 'active_handover_unexpected_request'),
        ):
            with self.subTest(case=case):
                run, device, health, now = self.fixture(capture=True, active_handover=True)
                health.update(
                    microphone_active=True,
                    local_wake_count=0,
                    voice_asr_attempts=0,
                    render_frames=100,
                    waiting_for_wake=True,
                )
                suppressed = False
                rows = []
                d, ui = device.status.return_value

                def route():
                    return (
                        'wifi'
                        if suppressed
                        and (0.5 <= now[0] < 4 or case == 'no_frames' and now[0] >= 0.5)
                        else 'usb'
                    )

                def status():
                    wifi = route() == 'wifi'
                    return dict(
                        d,
                        transport=route(),
                        microphone_active=True,
                        test_capture_limit_ms=180000,
                        test_capture_remaining_ms=120000
                        - int(now[0] * 1000)
                        + (3000 if wifi and case == 'extended_deadline' else 0),
                        test_capture_epoch=2 if wifi and case == 'new_epoch' else 1,
                    ), ui

                def request(payload):
                    nonlocal suppressed
                    if payload == {'operation': 'health'}:
                        wifi = route() == 'wifi'
                        return dict(
                            health,
                            transport=route(),
                            mic_frames=100 if case == 'no_frames' else 100 + int(now[0] * 1000),
                            microphone_active=not (wifi and case == 'mic_off'),
                            calibrating=wifi and case == 'recalibrated',
                            render_frames=101 if wifi and case == 'old_playback' else 100,
                        )
                    if payload == {'operation': 'transport_status'}:
                        count = 1 if route() == 'wifi' else int(suppressed) * 2
                        return dict(
                            transport=route(),
                            wifi_ready=True,
                            usb_ready=route() == 'usb',
                            usb_test_suppressed=route() == 'wifi',
                            transport_switches=count + int(suppressed and case == 'extra_switch'),
                        )
                    if payload == {'operation': 'diagnostics'}:
                        return {'requests': rows}
                    self.assertEqual(payload, {'operation': 'transport_test', 'seconds': 12})
                    suppressed = True
                    if case == 'extra_task':
                        rows.append(
                            dict(request_id='private-id', origin='text', outcome='completed')
                        )
                    return {'ok': True}

                run.request = request
                device.status.side_effect = status
                if reason:
                    with self.assertRaisesRegex(CheckFailure, reason):
                        run.active_capture_handover()
                else:
                    run.active_capture_handover()
                    evidence = run.report('passed')['active_handover_evidence']
                    self.assertEqual(
                        [row['transport'] for row in evidence['routes']], ['wifi', 'usb']
                    )
                    self.assertTrue(evidence['no_new_requests'])
                    self.assertFalse(run.report('passed')['physical_usb_unplug_tested'])
                    self.assertNotIn('epoch', json.dumps(evidence))
                device.speak_probe.assert_not_called()

    def test_standby_cannot_accept_an_already_black_screen_as_15_minutes(self):
        run, device, health, _ = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        self.arm_fixture_capture(run, device, health)
        _, ui = device.status.return_value
        ui['screen_sleeping'] = True
        with self.assertRaisesRegex(CheckFailure, 'too_early'):
            run.idle()

    def test_playback_checks_ui_samples_and_fresh_receipt(self):
        run, device, health, _ = self.fixture()
        health['playing'] = True
        device.status.return_value[1].update(
            caption_progress_overlap=False, halo_status_overlap=False, caption_line_visible=True
        )
        released = threading.Event()
        count = [0]

        def request(p):
            if 'text' in p:
                self.assertTrue(released.wait(2))
                return {'status': 'completed'}
            if p.get('operation') == 'diagnostics':
                count[0] += 1
                return {
                    'requests': []
                    if count[0] == 1
                    else [
                        {
                            'request_id': 'fresh',
                            'origin': 'announcement',
                            'outcome': 'completed',
                            'stages': {'playout_ack': 10, 'playback_done': 20},
                        }
                    ]
                }
            return dict(health)

        run.request = request

        def sleep(seconds):
            released.set()
            time.sleep(0.001)

        run.sleep = sleep
        run.playback()
        device.status.assert_called()

    def test_multi_clause_plan_has_no_capture_and_uses_separate_check(self):
        output = io.StringIO()
        with redirect_stdout(output), patch('device_smoke.Device') as device:
            self.assertEqual(main(['--runtime-root', '/nonexistent', '--multi-clause']), 0)
            device.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertFalse(plan['capture'])
        self.assertIn('multi_clause_announcement_and_playout', plan['checks'])
        self.assertNotIn('announcement_and_playout', plan['checks'])

    def test_multi_clause_legacy_runtime_refuses_before_any_device_open(self):
        run, device, _, _ = self.fixture(multi_clause=True)
        result = run.run()
        self.assertEqual(result['checks'][0]['reason'], 'clause_receipts_unsupported')
        device.open.assert_not_called()

    def test_multi_clause_requires_exactly_two_confirmed_clauses_and_no_capture(self):
        self.assertTrue(120 < len(MULTI_CLAUSE_TEXT) <= 240)
        for count, extra_frames in ((2, False), (None, False), (1, False), (2, True)):
            with self.subTest(count=count, extra_frames=extra_frames):
                run, device, health, _ = self.fixture(multi_clause=True)
                device.status.return_value[1].update(
                    caption_progress_overlap=False,
                    halo_status_overlap=False,
                    caption_line_visible=True,
                )
                submitted = threading.Event()
                ui_polled = threading.Event()
                sends = []

                def request(payload):
                    if 'text' in payload:
                        sends.append(payload['text'])
                        self.assertTrue(ui_polled.wait(1))
                        if extra_frames:
                            health['mic_frames'] += 1
                        return {'status': 'completed', 'characters': len(payload['text'])}
                    if payload.get('operation') == 'diagnostics':
                        if not sends:
                            return {'requests': []}
                        return {
                            'requests': [
                                {
                                    'request_id': 'PRIVATE_ID',
                                    'origin': 'announcement',
                                    'outcome': 'completed',
                                    'stages': {'playout_ack': 10, 'playback_done': 20},
                                    'confirmed_playback_clauses': count,
                                }
                            ]
                        }
                    # Start idle, then present the playback UI until sampled.
                    playing = submitted.is_set()
                    submitted.set()
                    return {**health, 'playing': playing}

                run.request = request
                run.sleep = lambda _: (ui_polled.set(), time.sleep(0.001))
                if extra_frames:
                    with self.assertRaisesRegex(CheckFailure, 'multi_clause_capture_changed'):
                        run.playback()
                elif count != 2:
                    with self.assertRaisesRegex(
                        CheckFailure, 'multi_clause_receipts_not_confirmed'
                    ):
                        run.playback()
                else:
                    run.playback()
                    report = run.report('passed')
                    self.assertEqual(report['multi_clause_evidence']['confirmed_clauses'], 2)
                    self.assertNotIn('PRIVATE_ID', json.dumps(report))
                self.assertEqual(sends, [MULTI_CLAUSE_TEXT])
                device.status.assert_called()

    def test_real_idle_waits_full_duration(self):
        run, device, health, now = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        self.arm_fixture_capture(run, device, health)
        original = device.status.return_value
        device.status.side_effect = lambda: (
            dict(original[0]),
            {**original[1], 'screen_sleeping': now[0] >= 900},
        )
        run.idle()
        self.assertGreaterEqual(now[0], 900)
        self.assertLess(now[0], 906)
        self.assertFalse(run.report('passed')['full_p0_acceptance'])
        self.assertEqual(
            run.report('passed')['standby_evidence'],
            {
                'last_awake_seconds': 895,
                'black_screen_poll_started_seconds': 900,
                'first_black_screen_seconds': 900,
            },
        )

    def test_standby_rejects_black_screen_seen_at_895_seconds(self):
        run, device, health, now = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        self.arm_fixture_capture(run, device, health)
        d, ui = device.status.return_value
        device.status.side_effect = lambda: (d, {**ui, 'screen_sleeping': now[0] >= 895})
        with self.assertRaisesRegex(CheckFailure, 'idle_black_screen_too_early'):
            run.idle()
        self.assertEqual(
            run.report('failed')['standby_evidence']['first_black_screen_seconds'], 895
        )

    def test_standby_rejects_expired_device_lease_even_if_host_still_reports_capture(self):
        run, device, health, now = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        self.arm_fixture_capture(run, device, health)
        d, ui = device.status.return_value

        def status():
            asleep = now[0] >= 900
            return (
                {**d, 'test_capture_remaining_ms': 0 if asleep else 1000000},
                {**ui, 'screen_sleeping': asleep},
            )

        device.status.side_effect = status
        with self.assertRaisesRegex(CheckFailure, 'device_test_capture_limit_not_active'):
            run.idle()

    def test_standby_requires_observed_awake_state_even_after_wait_budget(self):
        run, device, health, now = self.fixture(capture=True, standby=True)
        self.arm_fixture_capture(run, device, health)
        run.start_capture = lambda: now.__setitem__(0, 901)
        device.status.return_value[1]['screen_sleeping'] = True
        with self.assertRaisesRegex(CheckFailure, 'idle_awake_state_unobserved'):
            run.idle()

    def test_standby_rejects_a_black_screen_poll_that_crosses_900_seconds(self):
        run, device, health, now = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        self.arm_fixture_capture(run, device, health)
        d, ui = device.status.return_value

        def status():
            if now[0] == 895:
                now[0] += 6
                return d, {**ui, 'screen_sleeping': True}
            return d, ui

        device.status.side_effect = status
        with self.assertRaisesRegex(CheckFailure, 'idle_black_screen_too_early'):
            run.idle()
        evidence = run.report('failed')['standby_evidence']
        self.assertEqual(evidence['black_screen_poll_started_seconds'], 895)
        self.assertEqual(evidence['first_black_screen_seconds'], 901)

    def test_idle_timeout_is_bounded(self):
        run, device, health, now = self.fixture(capture=True, standby=True)
        run.start_capture = Mock()
        self.arm_fixture_capture(run, device, health)
        with self.assertRaisesRegex(CheckFailure, 'idle_black_screen_timeout'):
            run.idle()
        self.assertGreaterEqual(now[0], 970)
        self.assertLessEqual(now[0], 975)

    def test_acoustic_probe_needs_idle_capture_before_speaking(self):
        run, device, _, _ = self.fixture(capture=True, standby=True, acoustic=True)
        with self.assertRaises(CheckFailure):
            run.acoustic_probe()
        device.speak_probe.assert_not_called()

    def test_standby_probe_cannot_speak_after_the_device_capture_lease_expires(self):
        run, device, _, _ = self.acoustic_fixture(lambda _: [])
        status = device.status.side_effect

        def expired():
            d, ui = status()
            return {**d, 'test_capture_remaining_ms': 0}, ui

        device.status.side_effect = expired
        with self.assertRaisesRegex(CheckFailure, 'device_test_capture_limit_not_active'):
            run.acoustic_probe()
        device.speak_probe.assert_not_called()

    def acoustic_fixture(self, rows, *, awake=False, before=(), tasks=None):
        run, device, health, now = self.fixture(
            capture=True, standby=not awake, acoustic=not awake, awake_dialogue=awake
        )
        health['microphone_active'] = True
        health['waiting_for_wake'] = True
        health['local_wake_ready'] = True
        spoken = [False]

        def speak(*, question):
            spoken[0] = True

        def status():
            return (
                {
                    'foreground': True,
                    'microphone_active': True,
                    'vision_active': False,
                    'test_capture_limit_ms': run.capture_limit_ms,
                    'test_capture_remaining_ms': 150000 if awake else 220000,
                },
                {'camera_open': False, 'screen_sleeping': not awake and not spoken[0]},
            )

        def request(p):
            if p == {'operation': 'health'}:
                return dict(health)
            if p == {'operation': 'workspace_status'}:
                return {
                    'ok': True,
                    'tasks': tasks
                    if tasks is not None
                    else [{'generation': 8, 'status': 'completed', 'title': '１＋一 等于几？'}],
                }
            self.assertEqual(p, {'operation': 'diagnostics'})
            # Existing success must never count as a fresh spoken response.
            return {'requests': rows(now[0]) if spoken[0] else list(before)}

        device.speak_probe.side_effect = speak
        device.status.side_effect = status
        run.request = request
        return run, device, health, now

    def test_awake_dialogue_runs_before_home_without_waiting_for_black_screen(self):
        old = self.acoustic_row(request_id='old')
        run, device, _, now = self.acoustic_fixture(
            lambda _: [old, self.acoustic_row()], awake=True, before=(old,)
        )
        for operation in ('preflight', 'maintenance_open', 'playback', 'handover', 'start_capture'):
            setattr(run, operation, Mock())
        run.home = Mock()
        run.cleanup = Mock()
        result = run.run()
        self.assertEqual(result['state'], 'passed')
        self.assertTrue(result['awake_dialogue_enabled'])
        self.assertFalse(result['standby_enabled'])
        self.assertEqual(result['acoustic_evidence']['admitted_voice_requests'], 1)
        self.assertEqual(now[0], 20)
        self.assertEqual(
            [row['name'] for row in result['checks'][-3:]],
            [
                'synthetic_awake_dialogue',
                'home_stops_capture',
                'maintenance_reopen_keeps_microphone_off',
            ],
        )
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)
        run.cleanup.assert_called_once()

    def test_awake_response_acoustic_timing_requires_the_completed_generation(self):
        token = 'e' * 32
        for case, generation in ((1, 8), (30, 8), (30, 9)):
            with self.subTest(case=case, generation=generation):
                question = QA_LATENCY_QUESTIONS[case - 1]
                run, device, _, _ = self.acoustic_fixture(
                    lambda _: [self.acoustic_row()],
                    awake=True,
                    tasks=[{'generation': 8, 'status': 'completed', 'title': question}],
                )
                run.qa_question = question
                run.response_acoustic_evidence = True
                fixture_request = run.request

                def request(payload):
                    if payload == {'operation': 'response_probe_arm', 'case': case}:
                        device.speak_probe.assert_not_called()
                        return {'status': 'armed', 'token': token}
                    if payload == {'operation': 'response_probe_result', 'token': token}:
                        device.speak_probe.assert_called_once_with(question=question)
                        return {
                            'status': 'observed',
                            'generation': generation,
                            ACOUSTIC_RESPONSE_METRIC: 720,
                        }
                    return fixture_request(payload)

                run.request = request
                if generation == 8:
                    run.acoustic_probe(awake=True)
                    expected = {'status': 'observed', ACOUSTIC_RESPONSE_METRIC: 720}
                else:
                    with self.assertRaisesRegex(
                        CheckFailure, 'response_acoustic_timing_unconfirmed'
                    ):
                        run.acoustic_probe(awake=True)
                    expected = {'status': 'unconfirmed'}
                report = run.report('passed' if generation == 8 else 'failed')
                self.assertEqual(report['acoustic_evidence']['response_acoustic_timing'], expected)
                self.assertFalse(report['acoustic_latency_asserted'])
                self.assertNotIn(token, json.dumps(report))

    def test_synthetic_barge_dry_plan_has_no_side_effects(self):
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as query,
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--synthetic-barge']), 0
            )
            device.assert_not_called()
            query.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['synthetic_barge'])
        self.assertEqual(
            plan['checks'][-4:],
            [
                BARGE_PRIME_CHECK,
                BARGE_CHECK,
                'home_stops_capture',
                'maintenance_reopen_keeps_microphone_off',
            ],
        )

    def test_synthetic_stop_dry_plan_and_capability_gate(self):
        output = io.StringIO()
        with patch('device_smoke.Device') as mock, redirect_stdout(output):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--synthetic-stop']), 0
            )
            mock.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['synthetic_stop'])
        self.assertEqual(plan['checks'][-4:-2], [BARGE_PRIME_CHECK, STOP_CHECK])
        run, device, _, _ = self.fixture(capture=True, synthetic_stop=True)
        result = run.run()
        self.assertEqual(
            result['checks'][-1]['reason'], 'device_playback_stop_evidence_unsupported'
        )
        device.status.return_value[0]['test_playback_stop_supported'] = True
        result = run.run()
        self.assertEqual(result['checks'][-1]['reason'], 'backend_fast_stop_evidence_unsupported')
        device.open.assert_not_called()
        device.speak_probe.assert_not_called()

    def test_synthetic_stop_requires_one_local_hit_and_no_cloud_or_new_turn(self):
        for case, failure in (
            ('ok', None),
            ('no_hit', 'fast_stop_not_confirmed'),
            ('preexisting_hit', 'fast_stop_count_unexpected'),
            ('double_hit', 'fast_stop_count_unexpected'),
            ('cloud', 'fast_stop_reached_cloud_asr'),
            ('new_turn', 'fast_stop_unexpected_voice_turn'),
            ('wrong_cancel', 'interruption_device_stop_not_confirmed'),
        ):
            with self.subTest(case=case), patch('device_smoke.ThreadPoolExecutor') as executor:
                run, device, health, now = self.fixture(capture=True, synthetic_stop=True)
                health.update(
                    microphone_active=True,
                    waiting_for_wake=False,
                    fast_stop_ready=True,
                    fast_stop_count=12,
                    voice_asr_attempts=5,
                )
                device_status = device.status.return_value[0]
                device_status.update(
                    microphone_active=True,
                    test_capture_limit_ms=run.capture_limit_ms,
                    test_capture_remaining_ms=210000,
                    test_playback_stop_supported=True,
                    playback_paused=False,
                )
                announcement = dict(
                    request_id='announcement-private',
                    origin='announcement',
                    generation=20,
                    outcome=None,
                    stages={'playout_ack': 0},
                )
                rows = []

                def submit(_request, payload):
                    self.assertEqual(payload, {'text': BARGE_ANNOUNCEMENT})
                    rows.append(announcement)
                    health['playing'] = True
                    device_status.update(announcement_active=True, phase='speaking')
                    if case == 'preexisting_hit':
                        health['fast_stop_count'] += 1
                    reply = Mock()
                    reply.result.return_value = {'status': 'interrupted'}
                    return reply

                def stimulus(*, stop):
                    self.assertIs(stop, True)
                    announcement['outcome'] = 'cancelled'
                    health['playing'] = False
                    health['fast_stop_count'] += {'no_hit': 0, 'double_hit': 2}.get(case, 1)
                    health['voice_asr_attempts'] += int(case == 'cloud')
                    device_status.update(
                        announcement_active=False,
                        test_playback_stop={
                            'generation': 20,
                            'playback_id': 938471,
                            'cancel_generation': 22 if case == 'wrong_cancel' else 21,
                            'nanos': 1829374650,
                        },
                    )
                    rows.append(
                        dict(
                            request_id='partial',
                            origin='voice',
                            generation=None,
                            outcome='cancelled',
                            gate='stale',
                            stages={'voice_detected': 0},
                        )
                    )
                    if case == 'new_turn':
                        rows.append(self.acoustic_row(request_id='new-voice'))

                def request(payload):
                    if payload == {'operation': 'health'}:
                        return dict(health)
                    self.assertEqual(payload, {'operation': 'diagnostics'})
                    return {'requests': rows}

                run.request = request
                device.speak_probe.side_effect = stimulus
                executor.return_value.__enter__.return_value.submit.side_effect = submit
                if failure:
                    with self.assertRaisesRegex(CheckFailure, failure):
                        run.interruption_probe()
                else:
                    run.interruption_probe()
                    evidence = run.report('passed')['barge_evidence']
                    self.assertEqual(evidence['fast_stop_detections'], 1)
                    self.assertTrue(evidence['device_stop_applied'])
                    self.assertEqual(evidence['admitted_voice_requests'], 0)
                    self.assertEqual(now[0], 20)
                    self.assertNotIn('1829374650', json.dumps(evidence))
                if case == 'preexisting_hit':
                    device.speak_probe.assert_not_called()
                else:
                    device.speak_probe.assert_called_once_with(stop=True)

    def test_self_playback_plan_and_capability_gate(self):
        output = io.StringIO()
        with patch('device_smoke.Device') as device, redirect_stdout(output):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--wake-self-playback']), 0
            )
            device.assert_not_called()
        self.assertIn(WAKE_SELF_CHECK, json.loads(output.getvalue())['checks'])
        run, device, health, _ = self.fixture(capture=True, wake_self_playback=True)
        health['clause_receipts_supported'] = True
        report = run.run()
        self.assertEqual(report['checks'][-1]['reason'], 'audio_counters_unavailable')
        device.open.assert_not_called()

    def test_self_playback_both_languages_and_quiet_window(self):
        for case, failure in (
            ('valid', None),
            ('woke_and_reclosed', 'wake_self_playback_woke'),
            ('asr_during_tail', 'wake_self_playback_reached_asr'),
            ('missing_render', 'wake_self_playback_audio_not_observed'),
            ('missing_clause', 'wake_self_playback_receipt_missing'),
            ('extra_request', 'wake_self_playback_unexpected_request'),
            ('counter_regressed', 'wake_self_counter_regressed'),
        ):
            with self.subTest(case=case):
                run, device, health, now = self.fixture(capture=True, wake_self_playback=True)
                health.update(
                    microphone_active=True,
                    local_wake_ready=True,
                    waiting_for_wake=True,
                    local_wake_count=2,
                    voice_asr_attempts=4,
                    render_frames=100,
                )
                d, _ = device.status.return_value
                d.update(
                    microphone_active=True,
                    test_capture_limit_ms=180000,
                    test_capture_remaining_ms=150000,
                )
                rows, spoken = [], []

                def request(payload):
                    if payload == {'operation': 'health'}:
                        if case == 'asr_during_tail' and now[0] >= 5:
                            health['voice_asr_attempts'] = 5
                        return dict(health)
                    if payload == {'operation': 'diagnostics'}:
                        return {'requests': rows}
                    text = payload['text']
                    spoken.append(text)
                    health['mic_frames'] += 400
                    if case != 'missing_render':
                        health['render_frames'] += 400
                    if case == 'woke_and_reclosed':
                        health['local_wake_count'] += 1
                    if case == 'counter_regressed':
                        health['voice_asr_attempts'] -= 1
                    rows.append(
                        dict(
                            request_id=f'announcement-{len(spoken)}',
                            origin='announcement',
                            outcome='completed',
                            confirmed_playback_clauses=int(case != 'missing_clause'),
                            stages={'playout_ack': 4, 'playback_done': 8},
                        )
                    )
                    if case == 'extra_request':
                        rows.append(self.acoustic_row(request_id='unrelated-voice'))
                    return {'status': 'completed', 'characters': len(text)}

                run.request = request
                if failure:
                    with self.assertRaisesRegex(CheckFailure, failure):
                        run.wake_self_probe()
                    self.assertEqual(spoken, [WAKE_SELF_TEXT['zh']])
                else:
                    run.wake_self_probe()
                    self.assertEqual(spoken, list(WAKE_SELF_TEXT.values()))
                    self.assertEqual(now[0], 40)
                    evidence = run.report('passed')['wake_self_evidence']
                    self.assertEqual([row['language'] for row in evidence], ['zh', 'en'])
                    for row in evidence:
                        self.assertTrue(row['playback_completed'])
                        self.assertEqual(row['quiet_seconds'], 20)
                        self.assertEqual(row['local_wake_count'], 0)
                        self.assertEqual(row['voice_asr_attempts'], 0)
                device.speak_probe.assert_not_called()

    def test_barge_failure_runs_existing_capture_cleanup(self):
        run, device, _, _ = self.fixture(capture=True, synthetic_barge=True)
        calls = Mock()
        for operation in (
            'preflight',
            'maintenance_open',
            'playback',
            'handover',
            'start_capture',
            'acoustic_probe',
            'interruption_probe',
            'home',
            'cleanup',
        ):
            setattr(run, operation, getattr(calls, operation))
        calls.interruption_probe.side_effect = CheckFailure('interruption_pause_timing_missing')
        report = run.run()
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['checks'][-1]['name'], BARGE_CHECK)
        calls.acoustic_probe.assert_called_once_with(awake=True, quiet_seconds=2)
        calls.home.assert_not_called()
        self.assertEqual(
            [call[0] for call in calls.mock_calls][-3:],
            ['acoustic_probe', 'interruption_probe', 'cleanup'],
        )
        device.speak_probe.assert_not_called()

    def test_barge_refuses_older_apk_before_opening_or_speaking(self):
        run, device, _, _ = self.fixture(capture=True, synthetic_barge=True)
        result = run.run()
        self.assertEqual(
            result['checks'][-1]['reason'], 'device_interruption_transcript_unsupported'
        )
        device.open.assert_not_called()
        device.speak_probe.assert_not_called()

    def test_interruption_probe_requires_new_pause_and_replacement_receipts(self):
        for case, expected in (
            ('valid', None),
            ('valid_stop', None),
            ('missing_stop', 'interruption_device_stop_not_confirmed'),
            ('wrong_stop_generation', 'interruption_device_stop_not_confirmed'),
            ('wrong_stop_cancel_generation', 'interruption_device_stop_not_confirmed'),
            ('invalid_stop_id', 'interruption_device_stop_not_confirmed'),
            ('early_voice', 'interruption_voice_before_stimulus'),
            ('no_playout', 'interruption_playout_not_observed'),
            ('completed_announcement', 'interruption_announcement_not_cancelled'),
            ('wrong_generation', 'interruption_replacement_not_confirmed'),
            ('no_timing', 'interruption_pause_timing_missing'),
            ('rejected_pause', 'interruption_rejected_voice_paused'),
            ('extra_voice', 'interruption_unexpected_voice'),
            ('lost_capture', 'interruption_capture_or_usb_lost'),
            ('wrong_transcript', 'interruption_transcript_mismatch'),
            ('stale_transcript', 'interruption_transcript_receipt_missing'),
            ('transcript_content', 'interruption_transcript_receipt_missing'),
            ('missing_transcript', 'interruption_transcript_receipt_missing'),
        ):
            with self.subTest(case=case), patch('device_smoke.ThreadPoolExecutor') as executor:
                run, device, health, now = self.fixture(capture=True, synthetic_barge=True)
                health.update(microphone_active=True, waiting_for_wake=False)
                d, ui = device.status.return_value
                d.update(
                    microphone_active=True,
                    test_capture_limit_ms=run.capture_limit_ms,
                    test_capture_remaining_ms=210000,
                    announcement_active=True,
                    phase='speaking',
                    playback_paused=False,
                )
                if case.endswith('_stop') or '_stop_' in case:
                    d['test_playback_stop_supported'] = True
                old = self.acoustic_row(request_id='old')
                announcement = {
                    'request_id': 'announcement-id',
                    'origin': 'announcement',
                    'generation': 20,
                    'outcome': None,
                    'stages': {} if case == 'no_playout' else {'playout_ack': 0},
                }
                voice = self.acoustic_row(request_id='new')
                voice.update(gate='accepted', generation=20 if case == 'wrong_generation' else 21)
                voice['stages'].update(pause_request=20, pause_ack=40)
                if case != 'no_timing':
                    voice['device_pause_from_trigger_ms'] = 35.5
                rows = [old]

                def submit(_request, payload):
                    self.assertEqual(payload, {'text': BARGE_ANNOUNCEMENT})
                    health['playing'] = True
                    rows.append(announcement)
                    if case == 'early_voice':
                        rows.append(voice)
                    reply = Mock()
                    reply.result.return_value = {'status': 'interrupted'}
                    return reply

                def stimulus(**kwargs):
                    self.assertEqual(kwargs, {'interruption': True})
                    health['playing'] = False
                    announcement['outcome'] = (
                        'completed' if case == 'completed_announcement' else 'cancelled'
                    )
                    rows.append(voice)
                    if case != 'missing_transcript':
                        transcript = {
                            'generation': 20 if case == 'stale_transcript' else 21,
                            'matched': case != 'wrong_transcript',
                        }
                        if case == 'transcript_content':
                            transcript['text'] = 'content must never enter a report'
                        d['test_interruption_transcript'] = transcript
                    if d.get('test_playback_stop_supported') is True and case != 'missing_stop':
                        d['test_playback_stop'] = {
                            'generation': 19 if case == 'wrong_stop_generation' else 20,
                            'cancel_generation': 22
                            if case == 'wrong_stop_cancel_generation'
                            else 21,
                            'playback_id': True if case == 'invalid_stop_id' else 678901,
                            'nanos': 9876543210,
                        }
                    if case == 'rejected_pause':
                        rows.append(self.rejected_candidate(stages={'pause_request': 25}))
                    if case == 'lost_capture':
                        health['microphone_active'] = False

                def request(payload):
                    if payload == {'operation': 'health'}:
                        return dict(health)
                    self.assertEqual(payload, {'operation': 'diagnostics'})
                    extra = (
                        [self.acoustic_row(request_id='extra')]
                        if (case == 'extra_voice' and now[0] > 2)
                        else []
                    )
                    return {'requests': rows + extra}

                executor.return_value.__enter__.return_value.submit.side_effect = submit
                device.speak_probe.side_effect = stimulus
                run.request = request
                if expected is None:
                    run.interruption_probe()
                    self.assertEqual(now[0], 20)
                    evidence = run.report('passed')['barge_evidence']
                    self.assertEqual(evidence['device_pause_from_trigger_ms'], 35.5)
                    self.assertEqual(evidence['post_playback_quiet_seconds'], 20)
                    self.assertTrue(evidence['replacement_completed'])
                    self.assertIs(evidence['transcript_matches_stimulus'], True)
                    self.assertNotIn('announcement-id', json.dumps(evidence))
                    if case == 'valid_stop':
                        self.assertIs(evidence['device_stop_applied'], True)
                        self.assertNotIn('9876543210', json.dumps(evidence))
                        self.assertNotIn('678901', json.dumps(evidence))
                    else:
                        self.assertNotIn('device_stop_applied', evidence)
                else:
                    with self.assertRaisesRegex(CheckFailure, expected):
                        run.interruption_probe()
                    evidence = run.report('failed')['barge_evidence']
                    self.assertNotIn('content must never enter a report', json.dumps(evidence))
                if case in ('early_voice', 'no_playout'):
                    device.speak_probe.assert_not_called()
                else:
                    device.speak_probe.assert_called_once_with(interruption=True)

    def test_awake_dialogue_rejects_unready_wake_or_capture_before_speaking(self):
        for case in ('wake', 'short_budget', 'pending'):
            with self.subTest(case=case):
                before = (self.acoustic_row(outcome=None),) if case == 'pending' else ()
                run, device, health, _ = self.acoustic_fixture(
                    lambda _: [], awake=True, before=before
                )
                if case == 'wake':
                    health['waiting_for_wake'] = False
                if case == 'short_budget':
                    original_status = device.status.side_effect

                    def short_status():
                        device_status, ui = original_status()
                        return {**device_status, 'test_capture_remaining_ms': 45000}, ui

                    device.status.side_effect = short_status
                with self.assertRaises(CheckFailure):
                    run.acoustic_probe(awake=True)
                device.speak_probe.assert_not_called()

    def test_awake_dialogue_requires_task_and_playout_and_bounds_wait_by_capture(self):
        for missing in ('task', 'playout'):
            with self.subTest(missing=missing):
                row = self.acoustic_row()
                if missing == 'task':
                    row.pop('task_outcome')
                else:
                    row['stages'].pop('playout_ack')
                run, device, _, now = self.acoustic_fixture(lambda _: [row], awake=True)
                run.capture_status = Mock(return_value={'test_capture_remaining_ms': 50000})
                with self.assertRaisesRegex(CheckFailure, 'synthetic_acoustic_chain_not_confirmed'):
                    run.acoustic_probe(awake=True)
                self.assertEqual(now[0], 25)
                device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_same_admitted_request_must_stay_present_through_post_playback(self):
        run, device, _, now = self.acoustic_fixture(
            lambda elapsed: [self.acoustic_row(request_id='before' if elapsed < 5 else 'after')],
            awake=True,
        )
        with self.assertRaisesRegex(CheckFailure, 'unexpected_extra_voice_request'):
            run.acoustic_probe(awake=True)
        self.assertEqual(now[0], 5)
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    @staticmethod
    def acoustic_row(**changes):
        return {
            'request_id': 'PRIVATE_REQUEST_ID',
            'origin': 'voice',
            'generation': 8,
            'outcome': 'completed',
            'task_outcome': 'completed',
            'stages': dict.fromkeys(
                ('wake_detected', 'asr_end', 'brain_start', 'playout_ack', 'playback_done'), 1
            ),
            'text': 'PRIVATE_BODY',
            **changes,
        }

    def test_acoustic_success_observes_post_playback_window_and_only_counts_fixed_stages(self):
        run, device, _, now = self.acoustic_fixture(lambda _: [self.acoustic_row()])
        run.acoustic_probe()
        self.assertGreaterEqual(now[0], 20)
        self.assertLessEqual(now[0], 20.5)
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)
        evidence = run.report('passed').get('acoustic_evidence')
        self.assertIsInstance(evidence, dict)
        self.assertEqual(evidence['post_playback_quiet_seconds'], 20)
        self.assertEqual(evidence['completed_tasks'], 1)
        self.assertTrue(evidence['recognized_stimulus_matches'])
        self.assertEqual(evidence['stage_counts']['playback_done'], 1)
        self.assertNotIn('PRIVATE', json.dumps(evidence))

    def test_acoustic_probe_rejects_unmatched_or_ambiguous_workspace_receipts(self):
        match = {'generation': 8, 'status': 'completed', 'title': '一加一等于几？'}
        for tasks in (
            [dict(match, title='PRIVATE_OTHER_QUESTION')],
            [dict(match, title='1加-1等于几')],
            [dict(match, title='大黄蜂，1加-1等于几')],
            [dict(match, title='大黄蜂帮我查一加一等于几')],
            [dict(match, generation=7)],
            [dict(match, status='running')],
            [match, match],
        ):
            with self.subTest(tasks=tasks):
                run, device, _, _ = self.acoustic_fixture(
                    lambda _: [self.acoustic_row()], awake=True, tasks=tasks
                )
                with self.assertRaisesRegex(CheckFailure, 'synthetic_stimulus_not_confirmed'):
                    run.acoustic_probe(awake=True)
                evidence = run.report('failed')['acoustic_evidence']
                self.assertFalse(evidence['recognized_stimulus_matches'])
                self.assertNotIn('PRIVATE', json.dumps(evidence))
                device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_acoustic_probe_accepts_a_retained_fixed_wake_prefix(self):
        tasks = [{'generation': 8, 'status': 'completed', 'title': '大黄蜂，１＋１等于几？'}]
        run, device, _, _ = self.acoustic_fixture(
            lambda _: [self.acoustic_row()], awake=True, tasks=tasks
        )
        run.acoustic_probe(awake=True)
        self.assertTrue(run.report('passed')['acoustic_evidence']['recognized_stimulus_matches'])
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_latency_phase_rejects_a_correct_transcript_for_another_question(self):
        expected = QA_LATENCY_QUESTIONS[-1]
        for title, matches in (('大黄蜂，６＋５＝几？', True), ('五加六等于几', False)):
            with self.subTest(title=title):
                tasks = [{'generation': 8, 'status': 'completed', 'title': title}]
                run, device, _, _ = self.acoustic_fixture(
                    lambda _: [self.acoustic_row()], awake=True, tasks=tasks
                )
                run.qa_question = expected
                if matches:
                    run.acoustic_probe(awake=True)
                else:
                    with self.assertRaisesRegex(CheckFailure, 'synthetic_stimulus_not_confirmed'):
                        run.acoustic_probe(awake=True)
                self.assertIs(run.acoustic_evidence['recognized_stimulus_matches'], matches)
                device.speak_probe.assert_called_once_with(question=expected)

    def test_acoustic_probe_reports_only_the_fresh_completed_response_pair(self):
        for case in ('observed', 'malformed', 'missing'):
            with self.subTest(case=case):
                row = self.acoustic_row(
                    request_id='a' * 32,
                    generation=8,
                    scenario='qa',
                    error=None,
                    gate='accepted',
                    confirmed_playback_clauses=1,
                )
                del row['text']
                row['stages'].update(utterance_end=100, playout_ack=150, playback_done=200)
                if case != 'missing':
                    row['stages'].update(
                        confirmed_playout_ack=300,
                        confirmed_playback_done=250 if case == 'malformed' else 450,
                    )
                prior = self.acoustic_row(request_id='before')
                run, device, _, _ = self.acoustic_fixture(
                    lambda _: [prior, row], awake=True, before=(prior,)
                )
                run.acoustic_probe(awake=True)
                device.speak_probe.assert_called_once()
                evidence = run.report('passed')['acoustic_evidence']
                timing = evidence['response_playout_timing']
                self.assertEqual(timing['status'], case)
                if case == 'observed':
                    self.assertEqual(
                        timing,
                        dict(
                            status='observed',
                            utterance_end_ms=100,
                            confirmed_playout_ack_ms=300,
                            confirmed_playback_done_ms=450,
                            utterance_to_confirmed_playout_ack_ms=200,
                        ),
                    )
                else:
                    self.assertEqual(timing, {'status': case})
                self.assertNotIn('a' * 32, json.dumps(evidence))

    def test_qa_latency_phase_uses_fresh_voice_stages_and_skips_extra_playback(self):
        for scenario in ('qa', 'tool'):
            with self.subTest(scenario=scenario):
                row = self.acoustic_row(
                    request_id='a' * 32,
                    scenario=scenario,
                    error=None,
                    gate='accepted',
                    confirmed_playback_clauses=1,
                )
                del row['text']
                row['stages'].update(
                    utterance_end=0,
                    asr_end=5,
                    brain_start=10,
                    first_clause=30,
                    tts_start=32,
                    first_pcm=35,
                    playout_ack=50,
                    playback_done=60,
                    confirmed_playout_ack=50,
                    confirmed_playback_done=60,
                )
                run, device, _, _ = self.acoustic_fixture(lambda _: [row], awake=True)
                run.latency_sample = True
                for name in ('preflight', 'maintenance_open', 'start_capture', 'home', 'cleanup'):
                    setattr(run, name, Mock())
                run.playback, run.handover = Mock(), Mock()
                result = run.run()
                self.assertEqual(result['state'], 'passed')
                self.assertEqual(
                    [check['name'] for check in result['checks']],
                    planned_checks(set(QA_LATENCY_PHASES[0][1])),
                )
                self.assertEqual(
                    result['acoustic_evidence']['qa_stage_latencies_ms'],
                    dict(zip(QA_STAGE_METRICS, (5, 20, 3))) if scenario == 'qa' else {},
                )
                run.handover.assert_not_called()
                run.playback.assert_not_called()
                device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_acoustic_cancelled_narration_is_not_a_completed_answer(self):
        row = self.acoustic_row(outcome='cancelled')
        row['stages'].pop('playback_done')
        run, device, _, now = self.acoustic_fixture(lambda _: [row])
        with self.assertRaisesRegex(CheckFailure, 'synthetic_voice_request_not_completed'):
            run.acoustic_probe()
        self.assertLess(now[0], 1)
        evidence = run.report('failed').get('acoustic_evidence')
        self.assertIsInstance(evidence, dict)
        self.assertEqual(evidence['completed_tasks'], 1)
        self.assertEqual(evidence['cancelled_requests'], 1)
        self.assertEqual(evidence['stage_counts']['playback_done'], 0)
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_extra_voice_after_completed_playback_fails_without_replaying_stimulus(self):
        first = self.acoustic_row()
        extra = self.acoustic_row(
            request_id='SECOND_PRIVATE_ID', outcome=None, stages={'pause_request': 1}
        )
        run, device, _, now = self.acoustic_fixture(
            lambda seconds: [first] + ([extra] if seconds >= 5 else [])
        )
        with self.assertRaisesRegex(CheckFailure, 'unexpected_extra_voice_request'):
            run.acoustic_probe()
        self.assertEqual(now[0], 5)
        evidence = run.report('failed').get('acoustic_evidence')
        self.assertIsInstance(evidence, dict)
        self.assertEqual(evidence['new_voice_requests'], 2)
        self.assertEqual(evidence['stage_counts']['pause_request'], 1)
        self.assertGreater(evidence['post_playback_quiet_seconds'], 0)
        self.assertNotIn('PRIVATE', json.dumps(evidence))
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_acoustic_missing_wake_stage_keeps_partial_evidence_on_timeout(self):
        row = self.acoustic_row()
        row['stages'].pop('wake_detected')
        run, device, _, now = self.acoustic_fixture(lambda _: [row])
        with self.assertRaisesRegex(CheckFailure, 'synthetic_acoustic_chain_not_confirmed'):
            run.acoustic_probe()
        self.assertEqual(now[0], 120)
        evidence = run.report('failed').get('acoustic_evidence')
        self.assertIsInstance(evidence, dict)
        self.assertEqual(evidence['stage_counts']['wake_detected'], 0)
        self.assertEqual(evidence['stage_counts']['asr_end'], 1)
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    @staticmethod
    def rejected_candidate(**changes):
        return {
            'request_id': 'PRIVATE_REJECTED_CANDIDATE',
            'origin': 'voice',
            'generation': None,
            'outcome': 'rejected',
            'gate': 'echo_text',
            'stages': {
                'voice_detected': 0,
                'pause_ack': 49.2,
                'asr_end': 1559.3,
                'resume_ack': 1580.8,
            },
            **changes,
        }

    def test_rejected_echo_record_is_reported_without_becoming_a_second_dialogue(self):
        first = self.acoustic_row()
        rejected = self.rejected_candidate()
        run, device, _, now = self.acoustic_fixture(lambda _: [first, rejected])
        try:
            run.acoustic_probe()
        except CheckFailure as error:
            self.fail(f'A rejected, resumed candidate is not an admitted dialogue: {error}')
        evidence = run.report('passed')['acoustic_evidence']
        self.assertEqual(evidence['new_voice_requests'], 2)
        self.assertEqual(evidence['admitted_voice_requests'], 1)
        self.assertEqual(evidence['rejected_voice_candidates'], 1)
        self.assertEqual(evidence['rejected_pause_count'], 1)
        self.assertEqual(evidence['unresumed_rejected_pauses'], 0)
        self.assertGreaterEqual(now[0], 20)
        self.assertNotIn('PRIVATE', json.dumps(evidence))
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_pending_candidate_can_be_rejected_during_observation(self):
        first = self.acoustic_row()
        pending = self.rejected_candidate(outcome=None, gate=None, stages={'voice_detected': 0})
        rejected = self.rejected_candidate()
        run, device, _, now = self.acoustic_fixture(
            lambda seconds: [first, pending if seconds < 1 else rejected]
        )
        try:
            run.acoustic_probe()
        except CheckFailure as error:
            self.fail(
                f'Candidate classification must resolve before being counted as dialogue: {error}'
            )
        self.assertGreaterEqual(now[0], 20)
        self.assertEqual(run.report('passed')['acoustic_evidence']['pending_voice_candidates'], 0)
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_unresolved_candidate_cannot_pass_the_end_of_the_quiet_window(self):
        first = self.acoustic_row()
        pending = self.rejected_candidate(outcome=None, gate=None, stages={'voice_detected': 0})
        run, device, _, now = self.acoustic_fixture(lambda _: [first, pending])
        with self.assertRaisesRegex(CheckFailure, 'unresolved_voice_candidate'):
            run.acoustic_probe()
        self.assertGreaterEqual(now[0], 20)
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_rejected_acknowledged_pause_requires_a_resume_receipt(self):
        first = self.acoustic_row()
        rejected = self.rejected_candidate(stages={'pause_ack': 49.2, 'asr_end': 1559.3})
        run, device, _, now = self.acoustic_fixture(lambda _: [first, rejected])
        with self.assertRaisesRegex(CheckFailure, 'rejected_pause_not_resumed'):
            run.acoustic_probe()
        self.assertGreaterEqual(now[0], 20)
        device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_rejected_label_cannot_hide_an_extra_admitted_generation_or_task(self):
        for marker in (
            {'generation': 7},
            {'task_outcome': 'completed'},
            {'gate': 'accepted'},
            {'stages': {'brain_start': 1}},
        ):
            run, device, _, _ = self.acoustic_fixture(
                lambda _: [self.acoustic_row(), self.rejected_candidate(**marker)]
            )
            with (
                self.subTest(marker=marker),
                self.assertRaisesRegex(CheckFailure, 'unexpected_extra_voice_request'),
            ):
                run.acoustic_probe()
            device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_unknown_or_cancelled_candidate_cannot_be_silently_ignored(self):
        for changes in (
            {'gate': 'stale', 'outcome': 'cancelled'},
            {'gate': 'unknown'},
            {'generation': True},
        ):
            run, device, _, _ = self.acoustic_fixture(
                lambda _: [self.acoustic_row(), self.rejected_candidate(**changes)]
            )
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(CheckFailure, 'synthetic_voice_candidate_not_rejected'),
            ):
                run.acoustic_probe()
            device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_acoustic_observation_rejects_capture_loss_or_runtime_replacement(self):
        for changed in ('microphone_active', 'connected', 'runtime_instance'):
            with self.subTest(changed=changed):
                run, device, health, _ = self.acoustic_fixture(lambda _: [self.acoustic_row()])
                run.initial_instance = health['runtime_instance']

                def lost(seconds):
                    health[changed] = 'replacement' if changed == 'runtime_instance' else False

                run.sleep = lost
                with self.assertRaisesRegex(
                    CheckFailure, 'acoustic_capture_or_connection_lost|runtime_restarted'
                ):
                    run.acoustic_probe()
                device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_dumpsys_missing_or_duplicate_receipt_is_not_success(self):
        for text in ('other output', 'HUD_UI_STATUS {}\nHUD_UI_STATUS {}'):
            with self.assertRaises(CheckFailure):
                objects(text, 'HUD_UI_STATUS ')
        result = objects('PRIVATE_CAPTION\nHUD_UI_STATUS {"camera_open":false}', 'HUD_UI_STATUS ')
        self.assertEqual(result, {'camera_open': False})

    def test_ipc_rejects_camera_and_arbitrary_tool_routes_before_connect(self):
        for request in (
            {'operation': 'camera_snapshot'},
            {'operation': 'text_request', 'text': 'PRIVATE'},
            {'operation': 'device_control', 'arguments': {'action': 'start'}},
            {'operation': 'response_probe_arm', 'case': True},
            {'operation': 'response_probe_arm', 'case': 31},
            {'operation': 'response_probe_arm', 'case': 30, 'text': 'PRIVATE'},
        ):
            with patch('device_smoke.socket.socket') as connection:
                with self.assertRaises(CheckFailure):
                    rpc(Path('/nonexistent'), request)
                connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
