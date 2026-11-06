"""Automation fails closed and restores capture; fixtures never contact hardware."""

from contextlib import nullcontext, redirect_stdout, redirect_stderr
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
    ASR_STOP_CHECK,
    AWAKE_DIALOGUE_CHECK,
    BARGE_ANNOUNCEMENT,
    BARGE_CHECK,
    BARGE_PRIME_CHECK,
    ENGLISH_QUESTION,
    FAST_STOP_DEVICE_METRIC,
    STOP_CHECK,
    STOP_VISUAL_METRIC,
    WAKE_SELF_CHECK,
    WAKE_SELF_TEXT,
    CheckFailure,
    Device,
    MULTI_CLAUSE_TEXT,
    P0_DEVICE_PHASES,
    POST_DIALOGUE_SECONDS,
    QA_ENDPOINT_METRIC,
    QA_LATENCY_PHASES,
    QA_LATENCY_QUESTIONS,
    QA_PLAYOUT_METRIC,
    QA_STAGE_METRICS,
    RuntimeChanged,
    SmokeRun,
    english_wake_input_coverage,
    main,
    objects,
    planned_checks,
    rpc,
    run_device_suite,
    run_english_wake_pair,
    summarize_capture_clock,
    summarize_response_acoustic,
    valid_leading_evidence,
)
from diagnostics import ASR_STOP_STAGE, VAD_ONSET_PAUSE
from qa_speech import CORPUS_SHA256 as QA_CORPUS_SHA256
from qa_speech import ALL_QUESTIONS, SCENARIO_DIGESTS, SCENARIO_QUESTIONS


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
            for flags in ({'interruption': True}, {'stop': True}, {'stop': True, 'asr_stop': True}):
                with self.assertRaisesRegex(CheckFailure, 'synthetic_interruption_not_prepared'):
                    device.speak_probe(**flags)
            device.speak_probe(question=QA_LATENCY_QUESTIONS[-1])
            self.assertEqual(command.call_args.args[0][-1], '大黄蜂，六加五等于几？')
            with self.assertRaisesRegex(CheckFailure, 'unknown_synthetic_probe'):
                device.speak_probe(question='任意内容')
            self.assertEqual(command.call_count, 2)

    def test_english_stimulus_has_fixed_voice_rate_and_question(self):
        device = Device.__new__(Device)
        with (
            patch('device_smoke.resolve_voice', return_value='Daniel (English UK)') as resolve,
            patch('device_smoke.subprocess.run') as command,
        ):
            device.speak_probe(question=ENGLISH_QUESTION, english=True)
        resolve.assert_called_once_with('Daniel')
        self.assertEqual(
            command.call_args.args[0],
            [
                '/usr/bin/say',
                '-v',
                'Daniel (English UK)',
                '-r',
                '155',
                'Bumblebee, what is three plus five?',
            ],
        )
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

        def prepare_interruption(*, stop=False, asr_stop=False):
            from test_prepared_speech import metadata

            return {
                **metadata(),
                'kind': 'asr_stop' if asr_stop else 'local_stop' if stop else 'barge',
                'voice_family': 'Tingting',
            }

        device.prepare_interruption_probe.side_effect = prepare_interruption
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
        if options.get('standby'):
            health.update(render_frames=0, local_wake_count=0, voice_asr_attempts=0)

        def request(p):
            if p == {'operation': 'health'}:
                return dict(health)
            if p == {'operation': 'transport_status'}:
                return {'ok': True, 'usb_ready': True, 'wifi_ready': True}
            if p == {'operation': 'diagnostics'}:
                return {'requests': []}
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
            ['--capture', '--connection-ui-evidence'],
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

    def test_standby_batch_plan_only_runs_three_standby_rounds(self):
        output = io.StringIO()
        args = ['--runtime-root', '/unused', '--standby-suite']
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as query,
            redirect_stdout(output),
        ):
            self.assertEqual(main([*args, '--capture']), 0)
            for conflict in ([], ['--capture', '--p0-device-suite'], ['--capture', '--standby']):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main([*args, *conflict])
            device.assert_not_called()
            query.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertFalse(plan['execute'])
        self.assertFalse(plan['full_p0_acceptance'])
        self.assertEqual(plan['standby_seconds_per_phase'], 900)
        self.assertEqual(
            plan['phases'],
            [
                dict(
                    name=f'standby_{index}',
                    checks=planned_checks({'capture', 'standby', 'acoustic'}),
                )
                for index in range(1, 4)
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
        self.assertEqual(plan['minimum_backend_version'], '0.14.60')
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
        for case in ('ok', 'cloud_attempt', 'missing_room', 'missing_visuals'):
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
                        'expected_apk_code': 71,
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
                            FAST_STOP_DEVICE_METRIC: 120,
                            'admitted_voice_requests': 0,
                            'post_playback_quiet_seconds': 20,
                        }
                        if case != 'missing_visuals':
                            report['barge_evidence'][STOP_VISUAL_METRIC] = 32
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
                    failed = 3 if case == 'missing_room' else 2
                    self.assertEqual(len(runs), failed + 1)
                    self.assertEqual(result['state'], 'failed')
                    self.assertEqual(
                        result['phases'][failed]['reason'],
                        'suite_awake_response_acoustic_incomplete'
                        if case == 'missing_room'
                        else 'suite_synthetic_stop_evidence_incomplete',
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

    def test_self_playback_soak_plan_is_read_only_and_keeps_thirty_total_minutes(self):
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as request,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--self-playback-soak']), 0
            )
        device.assert_not_called()
        request.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['self_playback_seconds_per_phase'], 600)
        self.assertEqual(plan['minimum_backend_version'], '0.14.63')
        self.assertEqual(
            [p['name'] for p in plan['phases']],
            ['self_playback_1', 'self_playback_2', 'self_playback_3'],
        )

    def test_background_soak_dry_plan_keeps_path_private_and_never_opens_it(self):
        with (
            patch('device_smoke.Background.load') as load,
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as request,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(
                    [
                        '--runtime-root',
                        '/unused',
                        '--capture',
                        '--self-playback-soak',
                        '--background-scenario',
                        'tv',
                        '--background-wav',
                        '/unused/private-fixture.wav',
                        '--background-sha256',
                        'c' * 64,
                    ]
                ),
                0,
            )
        load.assert_not_called()
        device.assert_not_called()
        request.assert_not_called()
        self.assertNotIn('private-fixture.wav', output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue())['background'], {'kind': 'tv', 'sha256': 'c' * 64}
        )

    def test_self_playback_soak_checks_new_receipts_and_never_retries(self):
        for case, expected in (
            ('quiet', None),
            ('recovered', None),
            ('missing_resume', 'echo_soak_false_pause_limit_or_resume_missing'),
            ('extra_voice', 'echo_soak_unexpected_voice'),
            ('fast_stop', 'echo_soak_unexpected_voice'),
            ('wrong_receipt', 'echo_soak_playback_unconfirmed'),
            ('lost_window', 'echo_soak_receipt_window_lost'),
            ('background_ended', 'echo_soak_background_ended'),
            ('background_played', None),
        ):
            with self.subTest(case=case), patch('device_smoke.ECHO_SOAK_SECONDS', 8):
                run, device, health, now = self.fixture(capture=True, echo_soak=True)
                health.update(
                    microphone_active=True,
                    waiting_for_wake=False,
                    local_wake_count=0,
                    voice_asr_attempts=0,
                    render_frames=0,
                    fast_stop_count=0,
                    barge_in_pauses=0,
                    barge_in_resumed=0,
                )
                d, _ = device.status.return_value
                d.update(
                    microphone_active=True,
                    test_capture_limit_ms=run.capture_limit_ms,
                    test_capture_remaining_ms=run.capture_limit_ms,
                )
                rows = [self.acoustic_row(request_id='old')]
                cycles = []

                def request(payload):
                    if payload == {'operation': 'health'}:
                        return dict(health)
                    if payload == {'operation': 'diagnostics'}:
                        return {'requests': list(rows)}
                    self.assertEqual(payload, {'text': BARGE_ANNOUNCEMENT})
                    index = len(cycles)
                    cycles.append(index)
                    health['mic_frames'] += 10
                    health['render_frames'] += 8
                    now[0] += 2
                    if index == 0:
                        if case in ('recovered', 'missing_resume'):
                            stages = {'pause_request': 10, 'pause_ack': 20}
                            health['barge_in_pauses'] += 1
                            if case == 'recovered':
                                stages['resume_ack'] = 40
                                health['barge_in_resumed'] += 1
                            rows.append(self.rejected_candidate(stages=stages))
                        if case == 'extra_voice':
                            rows.append(self.acoustic_row(request_id='external'))
                        if case == 'fast_stop':
                            health['fast_stop_count'] += 1
                        if case == 'lost_window':
                            rows.clear()
                    rows.append(
                        {
                            'request_id': 'announcement' + str(index),
                            'origin': 'announcement',
                            'outcome': 'completed',
                            'confirmed_playback_clauses': 1,
                            'stages': {}
                            if case == 'wrong_receipt'
                            else {
                                'confirmed_playout_ack': 50,
                                'confirmed_playback_done': 100,
                            },
                        }
                    )
                    return {'status': 'completed', 'characters': len(BARGE_ANNOUNCEMENT)}

                run.request = request
                if case.startswith('background_'):
                    run.background = Mock(receipt={'kind': 'low-noise', 'sha256': 'b' * 64})
                    active = Mock(return_value=True)
                    if case == 'background_ended':
                        active.side_effect = [True, True, False]
                    run.background.play.return_value = nullcontext(active)
                if expected:
                    with self.assertRaisesRegex(CheckFailure, expected):
                        run.self_playback_soak()
                    self.assertEqual(cycles, [0])
                else:
                    run.self_playback_soak()
                    self.assertEqual(cycles, [0, 1])
                    self.assertEqual(run.echo_soak_evidence['elapsed_seconds'], 8)
                    self.assertEqual(run.echo_soak_evidence['render_samples'], 16)
                    self.assertEqual(
                        run.echo_soak_evidence['device_resumes'], int(case == 'recovered')
                    )
                    if case == 'background_played':
                        self.assertEqual(run.echo_soak_evidence['background_observed_seconds'], 8)
                        self.assertEqual(
                            run.echo_soak_evidence['background'], run.background.receipt
                        )
                device.speak_probe.assert_not_called()

    def test_interruption_batch_dry_plan_never_contacts_device_or_backend(self):
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as request,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--interruption-suite']), 0
            )
        device.assert_not_called()
        request.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['minimum_backend_version'], '0.14.60')
        self.assertEqual(plan['minimum_apk_code'], 71)
        self.assertEqual(len(plan['phases']), 30)
        self.assertEqual(
            [p['name'] for p in plan['phases'][:4]],
            [
                'barge_01',
                'stop_01',
                'barge_02',
                'stop_02',
            ],
        )

    def test_routed_latency_plans_bind_distinct_fixed_corpora_without_device_access(self):
        self.assertEqual(len(set(ALL_QUESTIONS)), 90)
        self.assertEqual(len(set(SCENARIO_DIGESTS.values())), 3)
        for scenario in ('web', 'tool'):
            with (
                self.subTest(scenario=scenario),
                patch('device_smoke.Device') as device,
                patch('device_smoke.rpc') as request,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    main(
                        [
                            '--runtime-root',
                            '/unused',
                            '--capture',
                            '--qa-latency-suite',
                            '--latency-scenario',
                            scenario,
                            '--response-acoustic-evidence',
                        ]
                    ),
                    0,
                )
                device.assert_not_called()
                request.assert_not_called()
                plan = json.loads(output.getvalue())
                self.assertTrue(plan['response_acoustic_evidence'])
                self.assertEqual(plan['latency_scenario'], scenario)
                self.assertEqual(
                    plan['minimum_backend_version'], '0.15.4' if scenario == 'tool' else '0.14.62'
                )
                self.assertEqual(plan['qa_corpus_sha256'], SCENARIO_DIGESTS[scenario])
                self.assertEqual(len(plan['phases']), 30)

    def test_qa_latency_batch_counts_only_complete_samples_and_never_retries(self):
        for scenario, fail_at, acoustic, bad_interval in (
            ('qa', None, False, None),
            ('qa', 4, False, None),
            ('qa', None, True, None),
            ('qa', 4, True, True),
            ('qa', 4, True, 20001),
            ('qa', 4, True, float('nan')),
            ('web', None, False, None),
            ('web', 4, False, None),
            ('tool', None, True, None),
            ('tool', 4, True, None),
        ):
            runs, questions = [], []

            def make_run(options, checkpoint, *, qa_question):
                self.assertEqual(options, set(QA_LATENCY_PHASES[0][1]))
                questions.append(qa_question)
                index = len(runs) + 1
                stages = dict.fromkeys(QA_STAGE_METRICS, 100)
                if index % 2:
                    stages[QA_ENDPOINT_METRIC] = 800
                    stages['dot_first_delta_ms'] = 100 * index
                if index == fail_at:
                    stages['dot_first_delta_ms'] = 99999
                if index == fail_at and not acoustic and scenario == 'qa':
                    stages.pop(QA_STAGE_METRICS[0])
                report = {
                    'state': 'passed',
                    'checks': [dict(name=name, state='passed') for name in planned_checks(options)],
                    'cleanup_microphone_off_verified': True,
                    'acoustic_evidence': {
                        'post_playback_quiet_seconds': 20,
                        'recognized_stimulus_matches': True,
                        'qa_stage_latencies_ms': stages,
                        'latency_scenario': 'qa'
                        if index == fail_at and scenario == 'web'
                        else scenario,
                        'latency_route_completed': index != fail_at,
                        'response_playout_timing': {
                            'status': 'observed',
                            QA_PLAYOUT_METRIC: 100 * index,
                        },
                    },
                }
                if acoustic:
                    report['acoustic_evidence']['response_acoustic_timing'] = {
                        'status': 'observed',
                        ACOUSTIC_RESPONSE_METRIC: bad_interval
                        if index == fail_at and scenario == 'qa'
                        else 150 * index,
                    }
                run = Mock(source_sha256='source', speech_source_sha256='voice')
                run.run.side_effect = lambda: checkpoint(report) or report
                runs.append(run)
                return run

            result = run_device_suite(
                make_run,
                lambda _: None,
                qa_latency=True,
                response_acoustic=acoustic,
                latency_scenario=scenario,
            )
            self.assertFalse(result['audible_onset_measured'])
            self.assertFalse(result['full_p0_acceptance'])
            self.assertEqual(result['scope'], 'synthetic_qa_latency')
            self.assertEqual(len(runs), fail_at or 30)
            self.assertEqual(questions, list(SCENARIO_QUESTIONS[scenario][: fail_at or 30]))
            self.assertEqual(result['qa_corpus_sha256'], SCENARIO_DIGESTS[scenario])
            self.assertEqual(result.get('latency_scenario', 'qa'), scenario)
            stats = result['latencies_ms'][QA_PLAYOUT_METRIC]
            self.assertEqual(
                result['latencies_ms'][QA_ENDPOINT_METRIC],
                {'samples': 2 if fail_at else 15, 'p50': 800, 'p95': 800},
            )
            self.assertEqual(
                result['latencies_ms']['dot_first_delta_ms'],
                {'samples': 2, 'p50': 200, 'p95': 290}
                if fail_at
                else {'samples': 15, 'p50': 1500, 'p95': 2760},
            )
            if fail_at:
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(
                    result['phases'][3]['reason'],
                    'suite_latency_scenario_unconfirmed'
                    if scenario == 'web'
                    else 'suite_latency_route_incomplete'
                    if scenario == 'tool'
                    else 'suite_qa_' + ('acoustic' if acoustic else 'stage') + '_timing_incomplete',
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
        for mode in ('--p0-device-suite', '--standby-suite'):
            with self.subTest(mode=mode):
                self.assert_suite_runtime_binding(mode)

    def assert_suite_runtime_binding(self, mode):
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
                    report = dict(
                        state='passed',
                        cleanup_microphone_off_verified=True,
                        checks=[
                            dict(name=name, state='passed') for name in planned_checks(set(options))
                        ],
                    )
                    if mode == '--standby-suite':
                        report.update(
                            elapsed_seconds=925,
                            standby_evidence={
                                'last_awake_seconds': 895,
                                'black_screen_poll_started_seconds': 900,
                                'first_black_screen_seconds': 900,
                                'quiet_counter_deltas': {
                                    'local_wake_count': 0,
                                    'voice_asr_attempts': 0,
                                    'render_frames': 0,
                                },
                            },
                            acoustic_evidence={
                                'post_playback_quiet_seconds': 20,
                                'response_playout_timing': {
                                    'status': 'observed',
                                    'utterance_end_ms': 10,
                                    'confirmed_playout_ack_ms': 30,
                                    'confirmed_playback_done_ms': 100,
                                    QA_PLAYOUT_METRIC: 20,
                                },
                                'admitted_voice_requests': 1,
                                'pending_voice_candidates': 0,
                                'unresumed_rejected_pauses': 0,
                                'recognized_stimulus_matches': True,
                            },
                        )
                    return report

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
                            mode,
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

    def test_capture_clock_reason_counts_are_optional_complete_and_conserved(self):
        reasons = {
            'invalid_' + name: 0
            for name in (
                'frame_range',
                'capture_range',
                'frame_distance',
                'origin',
                'timestamp',
                'future_timestamp',
                'stale_timestamp',
                'offset',
            )
        }
        base = dict(
            samples=3,
            available=2,
            unavailable=0,
            invalid=1,
            duplicate=0,
            discontinuities=0,
            min_offset_ns=0,
            max_offset_ns=1,
        )
        counts = {**reasons, 'invalid_timestamp': 1}
        valid = {**base, **counts}
        for value, expected in (
            (base, {'status': 'unconfirmed', **base}),
            (valid, {'status': 'unconfirmed', **valid}),
            ({**valid, 'invalid_timestamp': True}, {'status': 'malformed'}),
            ({**valid, 'invalid_timestamp': -1}, {'status': 'malformed'}),
            ({**valid, 'invalid_timestamp': 6101}, {'status': 'malformed'}),
            ({**valid, 'invalid_timestamp': 0}, {'status': 'malformed'}),
            ({**base, 'invalid_timestamp': 1}, {'status': 'malformed'}),
            ({**valid, 'raw_timestamp': 123}, {'status': 'malformed'}),
            (
                {**base, **reasons, 'invalid': 0, 'unavailable': 1},
                {'status': 'observed', **base, **reasons, 'invalid': 0, 'unavailable': 1},
            ),
        ):
            with self.subTest(value=value):
                self.assertEqual(summarize_capture_clock(value), expected)

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
        self.assertEqual(result['cleanup_reason'], 'cleanup_home_failed')
        self.assertNotIn('PRIVATE_BODY', json.dumps(result))

    def test_cleanup_failures_keep_distinct_safe_phase_reasons(self):
        for case, reason in (
            ('stop', 'cleanup_capture_stop_unconfirmed'),
            ('reopen', 'cleanup_maintenance_open_failed'),
            ('connection', 'cleanup_connection_unconfirmed'),
            ('foreground', 'cleanup_foreground_unconfirmed'),
            ('fixture', 'cleanup_fixture_not_removed'),
        ):
            with self.subTest(case=case):
                run, device, health, _ = self.fixture()
                run.playback = Mock(side_effect=CheckFailure('original_check_failed'))
                if case == 'stop':
                    device.home.side_effect = lambda: health.update(microphone_active=True)
                elif case == 'reopen':
                    device.open.side_effect = [None, OSError('PRIVATE_REOPEN_FAILURE')]
                elif case in ('connection', 'foreground'):

                    def open_activity(capture):
                        self.assertFalse(capture)
                        if device.open.call_count == 2:
                            if case == 'connection':
                                health['connected'] = False
                            else:
                                device.status.return_value[0]['foreground'] = False

                    device.open.side_effect = open_activity
                else:
                    run.preparation_attempted = True
                    device.close_speech_probe.side_effect = OSError('PRIVATE_FIXTURE_FAILURE')
                result = run.run()
                self.assertEqual(result['state'], 'cleanup_unconfirmed')
                self.assertEqual(result['cleanup_reason'], reason)
                self.assertEqual(result['cleanup_microphone_off_verified'], case == 'fixture')
                self.assertEqual(result['checks'][-1]['reason'], 'original_check_failed')
                self.assertNotIn('PRIVATE_', json.dumps(result))
                device.home.assert_called_once()
                self.assertEqual(
                    [call.args for call in device.open.call_args_list],
                    [(False,)] * (1 if case == 'stop' else 2),
                )

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

    def test_connection_ui_plan_is_offline(self):
        output = io.StringIO()
        with patch('device_smoke.Device') as device, redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        '--runtime-root',
                        '/unused',
                        '--capture',
                        '--all-route-loss',
                        '--connection-ui-evidence',
                    ]
                ),
                0,
            )
            device.assert_not_called()
        self.assertIs(json.loads(output.getvalue())['connection_ui_evidence'], True)

    def test_connection_ui_requires_fixed_labels_from_production_package(self):
        device = Device.__new__(Device)
        device.shell = Mock()
        notice = '连接已断开，正在自动重连 · 麦克风已关闭'
        mic_off = '麦克风关闭'

        def node(text, *, package='com.happyhome.cc8assistant', bounds='[0,1][42,29]'):
            return f'<node package="{package}" text="{text}" bounds="{bounds}" />'

        for connected, nodes, expected in (
            (False, [node('未连接'), node(mic_off), node(notice)], True),
            (False, [node('未连接'), node(mic_off), node(notice, package='other')], False),
            (True, [node('USB 优先'), node(mic_off), node(notice, package='other')], True),
            (True, [node('USB 优先'), node(mic_off), node(notice)], False),
            (True, [node('USB 优先'), node(mic_off), node('Wi-Fi')], False),
            (True, [node('USB 优先'), node(mic_off), node(mic_off)], False),
            (True, [node('USB 优先'), node(mic_off), node('麦克风开启')], False),
        ):
            with self.subTest(connected=connected, nodes=nodes):
                device.shell.return_value = '<hierarchy>' + ''.join(nodes) + '</hierarchy>'
                self.assertIs(device.connection_ui_matches(connected=connected), expected)
        device.shell.return_value = '<hierarchy>' + node(mic_off, bounds='') + '</hierarchy>'
        with self.assertRaisesRegex(ValueError, 'Invalid UI bounds'):
            device.connection_ui_matches(connected=True)

    def test_all_route_loss_requires_stop_and_idle_reconnect_on_both_ends(self):
        for case, expected in (
            ('valid', None),
            ('device_still_recording', 'all_route_loss_stop_unconfirmed'),
            ('host_still_recording', 'all_route_loss_stop_unconfirmed'),
            ('reconnect_active', 'all_route_loss_reconnect_started_audio'),
            ('native_frames_changed', 'all_route_loss_device_reopened_capture'),
            ('native_restart', 'all_route_loss_device_reopened_capture'),
            ('new_request', 'all_route_loss_unexpected_request'),
            ('ui_valid', None),
            ('ui_stale', 'all_route_loss_restored_ui_mismatch'),
            ('ui_late', 'all_route_loss_window_ended'),
        ):
            with self.subTest(case=case):
                ui_evidence = case.startswith('ui_')
                run, device, health, now = self.fixture(
                    capture=True, all_route_loss=True, connection_ui_evidence=ui_evidence
                )
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

                def connection_ui_matches(*, connected):
                    self.assertEqual(connected, not down())
                    if case == 'ui_late':
                        now[0] = 10
                    return not (connected and case == 'ui_stale')

                run.request = request
                device.status.side_effect = status
                device.connection_ui_matches.side_effect = connection_ui_matches
                if expected:
                    with self.assertRaisesRegex(CheckFailure, expected):
                        run.all_routes_loss_probe()
                else:
                    run.all_routes_loss_probe()
                    report = run.report('passed')
                    evidence = dict(
                        both_routes_down=True,
                        capture_stopped=True,
                        usb_priority_restored=True,
                        post_reconnect_mic_off_seconds=5,
                    )
                    if ui_evidence:
                        evidence['connection_ui'] = dict(disconnected=True, usb_restored=True)
                        self.assertEqual(device.connection_ui_matches.call_count, 2)
                    else:
                        device.connection_ui_matches.assert_not_called()
                    self.assertEqual(report['all_route_loss_evidence'], evidence)
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
                'quiet_counter_deltas': {
                    'local_wake_count': 0,
                    'voice_asr_attempts': 0,
                    'render_frames': 0,
                },
            },
        )

    def test_standby_stops_on_activity_without_waiting_for_a_black_screen(self):
        for counter, during_start in (
            ('local_wake_count', True),
            ('local_wake_count', False),
            ('voice_asr_attempts', False),
            ('render_frames', False),
        ):
            with self.subTest(counter=counter, during_start=during_start):
                run, device, health, now = self.fixture(capture=True, standby=True)
                self.arm_fixture_capture(run, device, health)
                d, ui = device.status.return_value
                device.status.side_effect = lambda: (d, {**ui, 'screen_sleeping': now[0] >= 900})

                def open_capture():
                    health['render_frames'] += 160  # Startup calibration reference.
                    if during_start:
                        health[counter] += 1

                def wait(seconds):
                    now[0] += seconds
                    if not during_start:
                        health[counter] += 1

                run.start_capture, run.sleep = open_capture, wait
                with self.assertRaisesRegex(CheckFailure, 'idle_unexpected_audio_activity'):
                    run.idle()
                self.assertLess(now[0], 900)
                self.assertEqual(run.standby_evidence['quiet_counter_deltas'][counter], 1)
                device.speak_probe.assert_not_called()

    def test_standby_rechecks_audio_activity_after_observing_a_black_screen(self):
        run, device, health, now = self.fixture(capture=True, standby=True)
        self.arm_fixture_capture(run, device, health)
        run.start_capture = Mock()
        d, ui = device.status.return_value

        def status():
            sleeping = now[0] >= 900
            if sleeping:
                health['voice_asr_attempts'] += 1
            return d, {**ui, 'screen_sleeping': sleeping}

        device.status.side_effect = status
        with self.assertRaisesRegex(CheckFailure, 'idle_unexpected_audio_activity'):
            run.idle()
        self.assertEqual(run.standby_evidence['first_black_screen_seconds'], 900)
        self.assertEqual(run.standby_evidence['quiet_counter_deltas']['voice_asr_attempts'], 1)
        device.speak_probe.assert_not_called()

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

    def acoustic_fixture(self, rows, *, awake=False, before=(), tasks=None, english_wake=False):
        run, device, health, now = self.fixture(
            capture=True,
            standby=not awake,
            acoustic=not awake,
            awake_dialogue=awake,
            english_wake=english_wake,
        )
        health['microphone_active'] = True
        health['waiting_for_wake'] = True
        health['local_wake_ready'] = True
        spoken = [False]

        def speak(*, question, english=False):
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
            if p == {'operation': 'tool_sample_begin', 'token': 'a' * 32}:
                return {'status': 'ready'}
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

    def test_english_plan_is_explicit_and_never_contacts_device(self):
        for mode in (['--awake-dialogue'], ['--standby', '--acoustic']):
            output = io.StringIO()
            with patch('device_smoke.Device') as device, redirect_stdout(output):
                self.assertEqual(
                    main(['--runtime-root', '/unused', '--capture', '--english-wake', *mode]),
                    0,
                )
                device.assert_not_called()
            plan = json.loads(output.getvalue())
            self.assertFalse(plan['execute'])
            self.assertEqual(plan['wake_language'], 'en')
            self.assertEqual(plan['minimum_backend_version'], '0.14.67')

    def test_wake_shape_plan_requires_a_fixed_english_phase_and_no_device(self):
        for flags, count in (
            (['--english-wake', '--awake-dialogue'], 1),
            (['--english-wake-pair'], 2),
        ):
            output = io.StringIO()
            with patch('device_smoke.Device') as device, redirect_stdout(output):
                self.assertEqual(
                    main(['--runtime-root', '/unused', '--capture', '--wake-input-shape', *flags]),
                    0,
                )
                device.assert_not_called()
            plan = json.loads(output.getvalue())
            self.assertTrue(plan['wake_input_shape'])
            self.assertEqual(len(plan['phases']), count)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(['--runtime-root', '/unused', '--capture', '--wake-input-shape'])
        self.assertEqual(error.exception.code, 2)

    def test_awake_wake_shape_execution_uses_single_prepared_phase(self):
        import device_smoke
        from release_info import RELEASE
        from test_wake_input_probe import metadata

        snapshot = dict(
            bridge_version=RELEASE.backend_version,
            protocol_version=RELEASE.protocol_version,
            release_id=RELEASE.release_id,
            runtime_instance='wake-input-test',
            wake_input_probe_supported=True,
            local_wake_count=0,
            local_wake_english_count=0,
        )
        terminal = dict(
            state='passed', cleanup_microphone_off_verified=True, english_fixture_played=True
        )
        with tempfile.TemporaryDirectory() as path:
            directory = Path(path)
            output = io.StringIO()
            fixture = Mock(directory_name='cc8-public-speech-abcdefgh', metadata=metadata())
            with (
                patch('device_smoke.rpc', return_value=snapshot),
                patch('device_smoke.private_directory', return_value=directory),
                patch('device_smoke.Device'),
                patch('device_smoke.PreparedSpeech', return_value=fixture) as prepare,
                patch(
                    'device_smoke.SmokeRun', return_value=Mock(run=Mock(return_value=terminal))
                ) as run,
                redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            '--runtime-root',
                            str(device_smoke.ROOT),
                            '--capture',
                            '--awake-dialogue',
                            '--english-wake',
                            '--wake-input-shape',
                            '--execute',
                        ]
                    ),
                    0,
                )
            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.kwargs['directory'], directory)
            run.assert_called_once()
            self.assertTrue(run.call_args.kwargs['wake_input_shape'])
            self.assertIs(run.call_args.kwargs['english_fixture'], fixture)
            fixture.close.assert_called_once()
            report = json.loads(next(directory.glob('device-*.json')).read_text())
            self.assertEqual(report['scope'], 'english_wake_awake')
            self.assertEqual(
                json.loads(output.getvalue().splitlines()[1])['phases'],
                [
                    {'name': 'awake', 'state': 'passed'},
                ],
            )

    def test_wrong_wake_shape_fixture_is_closed_before_any_device_phase(self):
        from test_wake_input_probe import metadata

        fixture = Mock(
            directory_name='cc8-public-speech-abcdefgh',
            metadata={**metadata(), 'pcm_sha256': 'a' * 64},
        )
        make_run = Mock()
        with patch('device_smoke.PreparedSpeech', return_value=fixture):
            result = run_english_wake_pair(
                make_run, lambda _: None, fixture_directory=Path('/private'), awake_only=True
            )
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['reason'], 'wake_fixture_identity_unconfirmed')
        self.assertTrue(result['prepared_english_input_cleaned'])
        make_run.assert_not_called()
        fixture.close.assert_called_once()

    def test_fixed_fixture_arms_and_reads_wake_shape_without_exporting_unknown_fields(self):
        from test_wake_input_probe import metadata

        run, device, health, _ = self.acoustic_fixture(
            lambda _: [self.acoustic_row()],
            awake=True,
            english_wake=True,
            tasks=[{'generation': 8, 'status': 'completed', 'title': ENGLISH_QUESTION}],
        )
        health.update(local_wake_count=20, local_wake_english_count=12)
        run.wake_input_shape = True
        original_request = run.request
        operations = []

        def request(payload):
            operation = payload['operation']
            if operation == 'wake_input_arm':
                self.assertEqual(payload['metadata'], metadata())
                operations.append('arm')
                return {'status': 'armed', 'token': 'a' * 32}
            if operation == 'wake_input_result':
                self.assertEqual(payload, {'operation': operation, 'token': 'a' * 32})
                operations.append('read')
                return {'status': 'measured', 'audio': 'never in report'}
            return original_request(payload)

        def play():
            operations.append('play')
            device.speak_probe.side_effect(question=ENGLISH_QUESTION, english=True)
            health['local_wake_count'] += 1
            health['local_wake_english_count'] += 1

        run.request = request
        run.english_fixture = Mock(
            directory_name='cc8-public-speech-abcdefgh',
            metadata=metadata(),
            play=Mock(side_effect=play),
        )
        run.acoustic_probe(awake=True)
        self.assertEqual(operations, ['arm', 'play', 'read'])
        self.assertEqual(run.acoustic_evidence['english_wake_input_shape'], {'status': 'invalid'})

    def test_english_pair_plan_does_not_prepare_audio_or_contact_device(self):
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as query,
            patch('device_smoke.PreparedSpeech') as prepare,
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(['--runtime-root', '/unused', '--capture', '--english-wake-pair']), 0
            )
        device.assert_not_called()
        query.assert_not_called()
        prepare.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['shared_prepared_input'])
        self.assertEqual([phase['name'] for phase in plan['phases']], ['awake', 'standby'])
        self.assertIn(AWAKE_DIALOGUE_CHECK, plan['phases'][0]['checks'])
        self.assertIn('real_idle_black_screen', plan['phases'][1]['checks'])
        for flags in ([], ['--capture', '--awake-dialogue'], ['--capture', '--p0-device-suite']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                main(['--runtime-root', '/unused', '--english-wake-pair', *flags])
            self.assertEqual(stopped.exception.code, 2)

    def test_english_pair_reuses_fixture_and_stops_after_unsafe_control(self):
        from test_prepared_speech import metadata

        passed = {
            'state': 'passed',
            'cleanup_microphone_off_verified': True,
            'english_fixture_played': True,
        }
        rejected = {
            **passed,
            'state': 'failed',
            'checks': [
                {'name': AWAKE_DIALOGUE_CHECK, 'reason': 'synthetic_acoustic_chain_not_confirmed'}
            ],
            'acoustic_evidence': {
                'new_voice_requests': 1,
                'rejected_voice_gates': {'wake_closed': 1},
                'english_wake_detections': 0,
                'admitted_voice_requests': 0,
                'stage_counts': {'asr_start': 0},
            },
        }
        for first, close_fails, second_runs, state in (
            (passed, False, True, 'passed'),
            (rejected, False, True, 'failed'),
            ({**rejected, 'cleanup_microphone_off_verified': False}, False, False, 'failed'),
            (
                {
                    **rejected,
                    'acoustic_evidence': {
                        **rejected['acoustic_evidence'],
                        'stage_counts': {'asr_start': 1},
                    },
                },
                False,
                False,
                'failed',
            ),
            ({**passed, 'english_fixture_played': False}, False, False, 'failed'),
            (passed, True, True, 'cleanup_unconfirmed'),
        ):
            with self.subTest(first=first, close_fails=close_fails):
                fixture = Mock(metadata=metadata())
                if close_fails:
                    fixture.close.side_effect = OSError
                make_run = Mock(
                    side_effect=(
                        Mock(run=Mock(return_value=first)),
                        Mock(run=Mock(return_value=passed)),
                    )
                )
                with patch('device_smoke.PreparedSpeech', return_value=fixture) as prepare:
                    report = run_english_wake_pair(make_run, lambda _: None)
                prepare.assert_called_once_with(
                    'Daniel', 'Bumblebee, what is three plus five?', rate=155
                )
                fixture.close.assert_called_once_with()
                self.assertEqual(report['state'], state)
                self.assertEqual(report['prepared_english_input'], fixture.metadata)
                self.assertEqual(report['prepared_english_input_cleaned'], not close_fails)
                self.assertEqual(make_run.call_count, 1 + int(second_runs))
                self.assertTrue(
                    all(
                        call.kwargs['english_fixture'] is fixture
                        for call in make_run.call_args_list
                    )
                )
                self.assertEqual(
                    report['phases'][1]['state'], 'passed' if second_runs else 'not_run'
                )

    def test_english_wake_old_backend_and_missing_counter_stop_before_device_access(self):
        from dataclasses import replace
        from release_info import RELEASE

        for manifest in (replace(RELEASE, backend_version='0.14.66'), RELEASE):
            health = dict(
                bridge_version=manifest.backend_version,
                protocol_version=manifest.protocol_version,
                release_id=manifest.release_id,
                runtime_instance='test',
                local_wake_count=0,
            )
            with (
                self.subTest(version=manifest.backend_version),
                patch('device_smoke.load_release', return_value=manifest),
                patch('device_smoke.rpc', return_value=health),
                patch('device_smoke.Device') as device,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                main(
                    [
                        '--runtime-root',
                        str(Path.cwd()),
                        '--capture',
                        '--awake-dialogue',
                        '--english-wake',
                        '--execute',
                    ]
                )
            self.assertEqual(stopped.exception.code, 2)
            device.assert_not_called()

    def test_english_wake_requires_new_english_hit_and_matching_spoken_question(self):
        for mode, title, delta, error in (
            ('awake', 'What is three plus five?', (1, 1), None),
            ('standby', "Bumble-bee, what's 3 + 5?", (1, 1), None),
            ('awake', 'What is three plus five?', (1, 0), 'synthetic_english_wake_not_confirmed'),
            ('awake', 'What is three plus five?', (0, 0), 'synthetic_english_wake_not_confirmed'),
            ('awake', 'What is three plus five?', (2, 2), 'english_wake_counter_delta_invalid'),
            ('awake', 'What is three plus six?', (1, 1), 'synthetic_stimulus_not_confirmed'),
            ('awake', 'What is -3 + 5?', (1, 1), 'synthetic_stimulus_not_confirmed'),
        ):
            with self.subTest(mode=mode, title=title, delta=delta):
                awake = mode == 'awake'
                run, device, health, now = self.acoustic_fixture(
                    lambda _: [self.acoustic_row()],
                    awake=awake,
                    english_wake=True,
                    tasks=[{'generation': 8, 'status': 'completed', 'title': title}],
                )
                health.update(local_wake_count=20, local_wake_english_count=12)
                original_speak = device.speak_probe.side_effect

                def speak(**options):
                    original_speak(**options)
                    health['local_wake_count'] += delta[0]
                    health['local_wake_english_count'] += delta[1]

                device.speak_probe.side_effect = speak
                if error:
                    with self.assertRaisesRegex(CheckFailure, error):
                        run.acoustic_probe(awake=awake)
                else:
                    run.acoustic_probe(awake=awake)
                    self.assertEqual(now[0], POST_DIALOGUE_SECONDS)
                    evidence = run.acoustic_evidence
                    self.assertEqual(evidence['english_wake_detections'], 1)
                    self.assertEqual(evidence['local_wake_detections'], 1)
                    self.assertTrue(evidence['recognized_stimulus_matches'])
                    self.assertEqual(
                        evidence['english_wake_input_coverage'], {'status': 'unavailable'}
                    )
                    self.assertNotIn(title, json.dumps(evidence))
                device.speak_probe.assert_called_once_with(question=ENGLISH_QUESTION, english=True)

    def test_english_acoustic_probe_plays_injected_fixture_and_reports_receipt(self):
        run, device, health, _ = self.acoustic_fixture(
            lambda _: [self.acoustic_row()],
            awake=True,
            english_wake=True,
            tasks=[{'generation': 8, 'status': 'completed', 'title': ENGLISH_QUESTION}],
        )
        health.update(local_wake_count=20, local_wake_english_count=12)
        health['english_wake_input_samples'] = {'primary': 320, 'alternate': 160}
        mark_spoken = device.speak_probe.side_effect

        def play():
            mark_spoken(question=ENGLISH_QUESTION, english=True)
            health['local_wake_count'] += 1
            health['local_wake_english_count'] += 1
            health['english_wake_input_samples'] = {'primary': 960, 'alternate': 480}

        run.english_fixture = Mock(play=Mock(side_effect=play))
        run.acoustic_probe(awake=True)
        run.english_fixture.play.assert_called_once_with()
        device.speak_probe.assert_not_called()
        self.assertTrue(run.report('passed')['english_fixture_played'])
        self.assertEqual(
            run.acoustic_evidence['english_wake_input_coverage'],
            {'status': 'observed', 'primary_samples': 640, 'alternate_samples': 320},
        )

    def test_english_input_coverage_rejects_changed_or_invalid_counters(self):
        baseline = {'primary': 960, 'alternate': 320}
        for after in (
            None,
            {'primary': 800, 'alternate': 320},
            {'primary': 1120, 'alternate': 0},
            {'primary': 1120, 'alternate': 640},
            {'primary': 1120, 'alternate': None},
            {'primary': 1120, 'alternate': True},
            {'primary': 1120, 'alternate': 480, 'payload': 'private'},
        ):
            with self.subTest(after=after):
                self.assertEqual(
                    english_wake_input_coverage(baseline, after), {'status': 'invalid'}
                )
        self.assertEqual(
            english_wake_input_coverage(
                {'primary': 320, 'alternate': None}, {'primary': 640, 'alternate': None}
            ),
            {'status': 'observed', 'primary_samples': 320, 'alternate_samples': None},
        )

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
        for case, generation, probe_reason, report_reason in (
            (1, 8, None, None),
            (30, 8, None, None),
            (31, 8, None, None),
            (32, 8, None, None),
            (90, 8, None, None),
            (90, 9, None, 'generation_mismatch'),
            (1, 8, 'render_clock_discontinuous', 'render_clock_discontinuous'),
            (1, 8, 'private-backend-error-body', 'unknown'),
        ):
            with self.subTest(case=case, generation=generation, probe_reason=probe_reason):
                question = ALL_QUESTIONS[case - 1]
                alignment = {
                    30: 'matched_render_frames',
                    31: 'matched_render_frames_room_fit',
                    32: 'matched_render_frames_room_alignment',
                }.get(case)
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
                        if probe_reason is not None:
                            return {
                                'status': 'unconfirmed',
                                'generation': generation,
                                'reason': probe_reason,
                            }
                        return {
                            'status': 'observed',
                            'generation': generation,
                            ACOUSTIC_RESPONSE_METRIC: 720,
                            **({'alignment': alignment} if alignment else {}),
                        }
                    return fixture_request(payload)

                run.request = request
                if report_reason is None:
                    run.acoustic_probe(awake=True)
                    expected = {'status': 'observed', ACOUSTIC_RESPONSE_METRIC: 720}
                    if alignment:
                        expected['alignment'] = alignment
                else:
                    with self.assertRaisesRegex(
                        CheckFailure, 'response_acoustic_timing_unconfirmed'
                    ):
                        run.acoustic_probe(awake=True)
                    expected = {'status': 'unconfirmed', 'reason': report_reason}
                report = run.report('passed' if report_reason is None else 'failed')
                self.assertEqual(report['acoustic_evidence']['response_acoustic_timing'], expected)
                self.assertFalse(report['acoustic_latency_asserted'])
                self.assertNotIn(token, json.dumps(report))
                self.assertNotIn('private-backend-error-body', json.dumps(report))

    def test_response_anchor_diagnostic_is_bounded_and_owned(self):
        diagnostic = {
            'prefix_minus_projected_ms': -60,
            'reference_anchor_spacing_ms': 240,
            'prefix_correlation': 0.99,
            'prefix_competing_correlation': 0.2,
            'prefix_block_status': 'observed',
        }
        reason = 'response_prefix_alignment_mismatch'
        receipt = {'status': 'unconfirmed', 'generation': 8, 'reason': reason}
        expected = {'status': 'unconfirmed', 'reason': reason}
        self.assertEqual(summarize_response_acoustic(receipt, 8), expected)
        self.assertEqual(
            summarize_response_acoustic({**receipt, 'anchor_diagnostic': diagnostic}, 8),
            {**expected, 'anchor_diagnostic': diagnostic},
        )
        self.assertEqual(
            summarize_response_acoustic({**receipt, 'anchor_diagnostic': diagnostic}, 9),
            {'status': 'unconfirmed', 'reason': 'generation_mismatch'},
        )
        for patch_values in (
            {'prefix_minus_projected_ms': True},
            {'prefix_minus_projected_ms': 81},
            {'prefix_minus_projected_ms': -2},
            {'prefix_correlation': float('nan')},
            {'prefix_block_status': 'private-backend-error'},
            {'capture_timestamp': 1700000000},
        ):
            with self.subTest(patch_values=patch_values):
                value = {**receipt, 'anchor_diagnostic': {**diagnostic, **patch_values}}
                self.assertEqual(summarize_response_acoustic(value, 8), expected)

    def test_acoustic_coverage_accepts_only_measurement_misses_for_the_current_response(self):
        for generation, reason, expected_error in (
            (8, 'response_prefix_alignment_mismatch', None),
            (9, 'response_prefix_alignment_mismatch', 'response_acoustic_timing_unconfirmed'),
            (8, 'capture_clock_gap', 'response_acoustic_timing_unconfirmed'),
        ):
            with self.subTest(generation=generation, reason=reason):
                run, _, _, now = self.acoustic_fixture(lambda _: [self.acoustic_row()], awake=True)
                run.response_acoustic_evidence = True
                run.response_acoustic_coverage = True
                run.latency_sample = True
                original_request = run.request

                def request(payload):
                    if payload == {'operation': 'response_probe_arm', 'case': 1}:
                        return {'status': 'armed', 'token': 'a' * 32}
                    if payload == {'operation': 'response_probe_result', 'token': 'a' * 32}:
                        return {'status': 'unconfirmed', 'generation': generation, 'reason': reason}
                    return original_request(payload)

                run.request = request
                if expected_error:
                    with self.assertRaisesRegex(CheckFailure, expected_error):
                        run.acoustic_probe(awake=True)
                else:
                    run.acoustic_probe(awake=True)
                    self.assertEqual(
                        run.acoustic_evidence['response_acoustic_timing'],
                        {'status': 'unconfirmed', 'reason': reason},
                    )
                self.assertEqual(now[0], POST_DIALOGUE_SECONDS)

    def test_acoustic_coverage_plan_is_explicit_and_requires_the_acoustic_batch(self):
        base = ['--runtime-root', '/unused', '--capture', '--collect-acoustic-coverage']
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            for mode in (
                ['--qa-latency-suite'],
                ['--awake-dialogue', '--response-acoustic-evidence'],
            ):
                with self.subTest(mode=mode), self.assertRaises(SystemExit) as rejected:
                    main(base + mode)
                self.assertEqual(rejected.exception.code, 2)
            self.assertEqual(main(base + ['--qa-latency-suite', '--response-acoustic-evidence']), 0)
            device.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertFalse(plan['execute'])
        self.assertTrue(plan['response_acoustic_evidence'])
        self.assertTrue(plan['response_acoustic_coverage'])
        self.assertEqual(len(plan['phases']), 30)

    def test_response_room_fit_status_is_allowlisted_and_owned(self):
        receipt = {
            'status': 'unconfirmed',
            'generation': 8,
            'reason': 'response_prefix_discontinuous',
        }
        expected = {'status': 'unconfirmed', 'reason': 'response_prefix_discontinuous'}
        for status in (
            'reference_coverage_missing',
            'capture_coverage_missing',
            'model_unconfirmed',
            'prefix_capture_below_floor',
            'prefix_gain_out_of_range',
            'prefix_shape_mismatch',
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    summarize_response_acoustic({**receipt, 'room_fit_status': status}, 8),
                    {**expected, 'room_fit_status': status},
                )
        for invalid in (None, True, 'matched', 'private-provider-message', {'pcm': [1]}):
            self.assertEqual(
                summarize_response_acoustic({**receipt, 'room_fit_status': invalid}, 8), expected
            )
        value = {**receipt, 'room_fit_status': 'prefix_shape_mismatch'}
        self.assertEqual(
            summarize_response_acoustic(value, 9),
            {'status': 'unconfirmed', 'reason': 'generation_mismatch'},
        )
        self.assertEqual(
            summarize_response_acoustic({**value, 'reason': 'expired'}, 8),
            {'status': 'unconfirmed', 'reason': 'expired'},
        )

    def test_response_reference_prefix_diagnosis_is_allowlisted_and_owned(self):
        expected = {'status': 'unconfirmed', 'reason': 'reference_not_found'}
        receipt = {**expected, 'generation': 8}
        for prefix in ('nonzero_below_floor', 'digital_silence'):
            value = {**receipt, 'reference_prefix': prefix}
            self.assertEqual(
                summarize_response_acoustic(value, 8), {**expected, 'reference_prefix': prefix}
            )
            self.assertEqual(
                summarize_response_acoustic(value, 9),
                {'status': 'unconfirmed', 'reason': 'generation_mismatch'},
            )
        for invalid in ('private-provider-message', {'pcm': [1]}):
            self.assertEqual(
                summarize_response_acoustic({**receipt, 'reference_prefix': invalid}, 8), expected
            )
        observed = {
            'status': 'observed',
            ACOUSTIC_RESPONSE_METRIC: 1040,
            'alignment': 'matched_after_digital_silence',
        }
        self.assertEqual(summarize_response_acoustic({**observed, 'generation': 8}, 8), observed)

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

    def test_render_overlap_plan_is_explicit_and_old_backend_stops_before_device_access(self):
        from dataclasses import replace
        from release_info import RELEASE

        for mode in ('--synthetic-barge', '--interruption-suite'):
            output = io.StringIO()
            with patch('device_smoke.Device') as device, redirect_stdout(output):
                self.assertEqual(
                    main(
                        ['--runtime-root', '/unused', '--capture', mode, '--require-render-overlap']
                    ),
                    0,
                )
                device.assert_not_called()
            plan = json.loads(output.getvalue())
            self.assertTrue(plan['require_render_overlap'])
            self.assertEqual(plan['minimum_backend_version'], '0.14.83')
        older = replace(RELEASE, backend_version='0.14.82')
        health = dict(bridge_version='0.14.82', protocol_version=older.protocol_version)
        with (
            patch('device_smoke.load_release', return_value=older),
            patch('device_smoke.rpc', return_value=health),
            patch('device_smoke.Device') as device,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as stopped,
        ):
            main(
                [
                    '--runtime-root',
                    '/unused',
                    '--execute',
                    '--capture',
                    '--synthetic-barge',
                    '--require-render-overlap',
                ]
            )
        self.assertEqual(stopped.exception.code, 2)
        device.assert_not_called()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(
                [
                    '--runtime-root',
                    '/unused',
                    '--capture',
                    '--awake-dialogue',
                    '--require-render-overlap',
                ]
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
            ('ok_visual', None),
            ('no_hit', 'fast_stop_not_confirmed'),
            ('preexisting_hit', 'fast_stop_count_unexpected'),
            ('double_hit', 'fast_stop_count_unexpected'),
            ('cloud', 'fast_stop_reached_cloud_asr'),
            ('new_turn', 'fast_stop_unexpected_voice_turn'),
            ('wrong_cancel', 'interruption_device_stop_not_confirmed'),
            ('wrong_clock_generation', 'fast_stop_device_timing_unconfirmed'),
            ('inverted_clock', 'fast_stop_device_timing_unconfirmed'),
            ('missing_clock', 'fast_stop_device_timing_unconfirmed'),
            ('long_interval', 'fast_stop_device_timing_out_of_range'),
            ('wrong_visual', 'fast_stop_visual_timing_unconfirmed'),
            ('missing_visual', 'fast_stop_visual_timing_unconfirmed'),
        ):
            with self.subTest(case=case), patch('device_smoke.ThreadPoolExecutor') as executor:
                run, device, health, now = self.fixture(capture=True, synthetic_stop=True)
                health.update(
                    microphone_active=True,
                    waiting_for_wake=False,
                    fast_stop_ready=True,
                    fast_stop_timing_supported=True,
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
                if case.endswith('_visual'):
                    device_status['test_stop_visual_timing_supported'] = True
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
                    if case != 'missing_clock':
                        health['fast_stop_observation'] = {
                            'cancel_generation': 20 if case == 'wrong_clock_generation' else 21,
                            'detecting_frame_nanos': (
                                1829374651 if case == 'inverted_clock' else 1709374650
                            ),
                        }
                    device_status.update(
                        announcement_active=False,
                        test_playback_stop={
                            'generation': 20,
                            'playback_id': 938471,
                            'cancel_generation': 22 if case == 'wrong_cancel' else 21,
                            'nanos': 22829374650 if case == 'long_interval' else 1829374650,
                        },
                    )
                    if case in ('ok_visual', 'wrong_visual'):
                        device_status['test_stop_visual_timing'] = {
                            'cancel_generation': 20 if case == 'wrong_visual' else 21,
                            'nanos': 1861374650,
                        }
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
                    self.assertEqual(evidence[FAST_STOP_DEVICE_METRIC], 120)
                    self.assertEqual(evidence['admitted_voice_requests'], 0)
                    self.assertEqual(now[0], 20)
                    self.assertNotIn('1829374650', json.dumps(evidence))
                    self.assertNotIn('1709374650', json.dumps(evidence))
                    if case == 'ok_visual':
                        self.assertEqual(evidence[STOP_VISUAL_METRIC], 32)
                        self.assertNotIn('1861374650', json.dumps(evidence))
                    else:
                        self.assertNotIn(STOP_VISUAL_METRIC, evidence)
                if case == 'preexisting_hit':
                    device.speak_probe.assert_not_called()
                else:
                    device.speak_probe.assert_called_once_with(stop=True)

    def test_asr_stop_plan_is_separate_and_needs_no_device(self):
        output = io.StringIO()
        with patch('device_smoke.Device') as device, redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        '--runtime-root',
                        '/unused',
                        '--capture',
                        '--synthetic-stop',
                        '--stop-route',
                        'asr',
                    ]
                ),
                0,
            )
            device.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['stop_route'], 'asr')
        self.assertEqual(plan['checks'][-4:-2], [BARGE_PRIME_CHECK, ASR_STOP_CHECK])
        self.assertEqual(plan['minimum_apk_code'], 71)
        self.assertFalse(plan['execute'])

    def test_asr_stop_rejects_old_backend_or_wrong_provider_before_device_access(self):
        from dataclasses import replace
        from release_info import RELEASE

        for release, engine in (
            (replace(RELEASE, backend_version='0.14.65'), 'minimax'),
            (RELEASE, 'sensevoice'),
        ):
            health = dict(
                bridge_version=release.backend_version,
                protocol_version=release.protocol_version,
                release_id=release.release_id,
                runtime_instance='test',
                asr_engine=engine,
            )
            with (
                self.subTest(version=release.backend_version, engine=engine),
                patch('device_smoke.load_release', return_value=release),
                patch('device_smoke.rpc', return_value=health),
                patch('device_smoke.Device') as device,
                patch('device_smoke.private_directory') as output,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                main(
                    [
                        '--runtime-root',
                        str(Path.cwd()),
                        '--capture',
                        '--synthetic-stop',
                        '--stop-route',
                        'asr',
                        '--execute',
                    ]
                )
            self.assertEqual(stopped.exception.code, 2)
            device.assert_not_called()
            output.assert_not_called()

    def test_asr_stop_needs_its_trace_and_device_receipts_without_dot_or_local_hit(self):
        for case, error in (
            ('valid', None),
            ('render_asr', None),
            ('local', 'fast_stop_count_unexpected'),
            ('dot', 'asr_stop_reached_dot'),
            ('extra_voice', 'asr_stop_unexpected_voice'),
            ('missing_marker', 'asr_stop_not_confirmed'),
            ('reversed_marker', 'asr_stop_not_confirmed'),
            ('wrong_generation', 'asr_stop_not_confirmed'),
            ('wrong_receipt', 'interruption_device_stop_not_confirmed'),
            ('missing_visual', 'asr_stop_visual_timing_unconfirmed'),
        ):
            with self.subTest(case=case), patch('device_smoke.ThreadPoolExecutor') as executor:
                run, device, health, now = self.fixture(
                    capture=True, synthetic_stop=True, stop_route='asr'
                )
                health.update(
                    microphone_active=True,
                    waiting_for_wake=False,
                    fast_stop_ready=True,
                    fast_stop_timing_supported=True,
                    fast_stop_count=12,
                    voice_asr_attempts=5,
                    asr_engine='minimax',
                )
                status, _ = device.status.return_value
                status.update(
                    microphone_active=True,
                    test_capture_limit_ms=run.capture_limit_ms,
                    test_capture_remaining_ms=210000,
                    test_playback_stop_supported=True,
                    playback_paused=False,
                )
                announcement = dict(
                    request_id='old',
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
                    status.update(announcement_active=True, phase='speaking')
                    reply = Mock()
                    reply.result.return_value = {'status': 'interrupted'}
                    return reply

                def stimulus(*, stop, asr_stop):
                    self.assertTrue(stop and asr_stop)
                    announcement['outcome'] = 'cancelled'
                    health.update(
                        playing=False,
                        fast_stop_count=12 + int(case == 'local'),
                        voice_asr_attempts=6 + int(case == 'render_asr'),
                    )
                    stages = {'asr_start': 100, 'asr_end': 400}
                    if case != 'missing_marker':
                        stages[ASR_STOP_STAGE] = 399 if case == 'reversed_marker' else 401
                    if case == 'dot':
                        stages['brain_start'] = 405
                    rows.append(
                        dict(
                            request_id='stop',
                            origin='voice',
                            generation=22 if case == 'wrong_generation' else 21,
                            outcome='completed',
                            gate='accepted',
                            stages=stages,
                        )
                    )
                    if case == 'extra_voice':
                        rows.append(
                            dict(
                                request_id='extra',
                                origin='voice',
                                gate='echo_text',
                                outcome='rejected',
                                stages={'asr_start': 402},
                            )
                        )
                    status.update(
                        announcement_active=False,
                        test_playback_stop=dict(
                            generation=20,
                            playback_id=42,
                            cancel_generation=22 if case == 'wrong_receipt' else 21,
                            nanos=1000000000,
                        ),
                    )
                    if case != 'missing_visual':
                        status['test_stop_visual_timing'] = dict(
                            cancel_generation=21, nanos=1010000000
                        )

                def request(payload):
                    if payload == {'operation': 'health'}:
                        return dict(health)
                    self.assertEqual(payload, {'operation': 'diagnostics'})
                    return {'requests': rows}

                run.request = request
                device.speak_probe.side_effect = stimulus
                executor.return_value.__enter__.return_value.submit.side_effect = submit
                if error:
                    with self.assertRaisesRegex(CheckFailure, error):
                        run.interruption_probe()
                else:
                    run.interruption_probe()
                    evidence = run.report('passed')['barge_evidence']
                    self.assertTrue(evidence['asr_stop_completed'])
                    self.assertEqual(evidence['fast_stop_detections'], 0)
                    self.assertEqual(
                        evidence['stop_cloud_asr_attempts'], 1 + int(case == 'render_asr')
                    )
                    self.assertEqual(evidence[STOP_VISUAL_METRIC], 10)
                    self.assertNotIn(FAST_STOP_DEVICE_METRIC, evidence)
                    self.assertEqual(now[0], 20)
                device.speak_probe.assert_called_once_with(stop=True, asr_stop=True)

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
            ('known', None),
            ('known_room', None),
            ('known_room_raw_miss', 'known_speech_window_unconfirmed'),
            ('known_room_far_miss', 'known_speech_far_reference_unconfirmed'),
            ('known_room_playback_changed', 'known_speech_far_playback_changed'),
            ('known_room_boundary_invalid', 'known_speech_boundary_unconfirmed'),
            ('known_room_overlap_late', None),
            ('known_room_overlap_timeout', 'known_speech_common_overlap_unconfirmed'),
            ('known_room_boundary_mismatch', 'known_speech_boundary_mismatch'),
            ('known_miss', 'known_speech_window_unconfirmed'),
            ('known_bad_receipt', 'known_speech_evidence_invalid'),
            ('known_wrong_input', 'known_speech_input_mismatch'),
            ('known_bad_template', 'known_speech_input_mismatch'),
            ('known_bad_arm', 'known_speech_arm_unconfirmed'),
            ('valid_stop', None),
            ('render_overlap', None),
            ('no_render_overlap', 'interruption_render_overlap_unconfirmed'),
            ('prefix_and_render', None),
            ('prefix_only', 'interruption_render_overlap_unconfirmed'),
            ('missing_capture', 'interruption_render_overlap_unconfirmed'),
            ('legacy_capture', 'interruption_render_overlap_unconfirmed'),
            ('invalid_capture', 'interruption_replacement_playout_unconfirmed'),
            ('missing_stop', 'interruption_device_stop_not_confirmed'),
            ('wrong_stop_generation', 'interruption_device_stop_not_confirmed'),
            ('wrong_stop_cancel_generation', 'interruption_device_stop_not_confirmed'),
            ('invalid_stop_id', 'interruption_device_stop_not_confirmed'),
            ('early_voice', 'interruption_voice_before_stimulus'),
            ('no_playout', 'interruption_playout_not_observed'),
            ('completed_announcement', 'interruption_announcement_not_cancelled'),
            ('wrong_generation', 'interruption_replacement_not_confirmed'),
            ('no_timing', 'interruption_pause_timing_missing'),
            ('no_pair', 'interruption_replacement_playout_unconfirmed'),
            ('wrong_pair', 'interruption_replacement_playout_unconfirmed'),
            ('rejected_pause', 'interruption_rejected_voice_paused'),
            ('extra_voice', 'interruption_unexpected_voice'),
            ('lost_capture', 'interruption_capture_or_usb_lost'),
            ('wrong_transcript', 'interruption_transcript_mismatch'),
            ('wrong_transcript_bad_stop', 'interruption_transcript_mismatch'),
            ('stale_transcript', 'interruption_transcript_receipt_missing'),
            ('transcript_content', 'interruption_transcript_receipt_missing'),
            ('missing_transcript', 'interruption_transcript_receipt_missing'),
        ):
            with self.subTest(case=case), patch('device_smoke.ThreadPoolExecutor') as executor:
                strict = case in (
                    'render_overlap',
                    'no_render_overlap',
                    'prefix_and_render',
                    'prefix_only',
                    'missing_capture',
                    'legacy_capture',
                    'invalid_capture',
                )
                run, device, health, now = self.fixture(
                    capture=True,
                    synthetic_barge=True,
                    require_render_overlap=strict,
                    known_speech_evidence=case.startswith('known'),
                    known_speech_room_model=case.startswith('known_room'),
                )
                known = case.startswith('known')
                if known:
                    from test_prepared_speech import metadata
                    from known_speech_receipt import METHOD

                    reference_metadata = {**metadata(), 'frames': 12800}
                    run.prepared_input = {
                        **reference_metadata,
                        'kind': 'barge',
                        'voice_family': 'Tingting',
                    }
                    device.interruption_fixture.return_value = {
                        'directory': 'cc8-public-speech-abcdefgh',
                        'metadata': reference_metadata,
                    }
                    near_result = dict(
                        schema=1,
                        method=METHOD,
                        status='observed',
                        captured_frames=10000,
                        reference_frames=10000,
                        template_offset_frames=0,
                        template_frames=3200,
                        input_pcm_sha256='a' * 64,
                        raw_audio_saved=False,
                        first_audible_onset_measured=False,
                        physical_double_talk_confirmed=False,
                        confidence=0.9,
                        rival_confidence=0.1,
                        render_similarity=0.1,
                        match_start_offset_frames=3200,
                        matched_window_digital_render_ms=100,
                    )
                    if case.startswith('known_room'):
                        from test_causal_speech_model import signals, CUTOFF
                        from causal_speech_model import analyse

                        mic, far, template = signals()
                        near_result.update(captured_frames=len(mic), reference_frames=len(far))
                        near_result['far_transfer'] = analyse(
                            mic, far, template[:3200], CUTOFF, CUTOFF
                        )
                    if case in ('known_miss', 'known_room_raw_miss'):
                        near_result['status'] = 'not_observed'
                        near_result.pop('match_start_offset_frames')
                        near_result.pop('matched_window_digital_render_ms')
                    elif case == 'known_bad_receipt':
                        near_result['text'] = 'private-content'
                    elif case == 'known_wrong_input':
                        near_result['input_pcm_sha256'] = 'd' * 64
                    elif case == 'known_bad_template':
                        near_result['template_offset_frames'] = 12800
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
                if case.endswith('_stop') or '_stop_' in case or case == 'wrong_transcript':
                    d['test_playback_stop_supported'] = True
                old = self.acoustic_row(request_id='old')
                announcement = {
                    'request_id': 'announcement-id',
                    'origin': 'announcement',
                    'generation': 20,
                    'outcome': None,
                    'stages': {} if case == 'no_playout' else {'playout_ack': 0},
                }
                voice = self.acoustic_row(
                    request_id='c' * 32, scenario='qa', error=None, confirmed_playback_clauses=1
                )
                del voice['text']
                voice.update(gate='accepted', generation=20 if case == 'wrong_generation' else 21)
                if (strict and case != 'missing_capture') or case.startswith('wrong_transcript'):
                    from test_capture_evidence import evidence as capture_evidence

                    captured = capture_evidence()
                    if case in ('prefix_and_render', 'prefix_only'):
                        from capture_evidence import ONSET_PREFIX_COUNTS

                        captured.update(dict.fromkeys(ONSET_PREFIX_COUNTS, 13))
                    if case in ('no_render_overlap', 'prefix_only'):
                        captured.update(
                            credible_with_render_frames=0, credible_without_render_frames=1
                        )
                    elif case == 'legacy_capture':
                        from capture_evidence import TEMPORAL_COUNTS

                        for field in TEMPORAL_COUNTS:
                            captured.pop(field)
                    elif case == 'invalid_capture':
                        captured['credible_with_render_frames'] = True
                    voice['capture'] = captured
                if case == 'wrong_transcript':
                    voice['asr_input_samples'] = 8160
                    voice['stages']['asr_start'] = 1
                voice['stages'].update(
                    pause_request=20,
                    pause_ack=40,
                    utterance_end=80,
                    playout_ack=100,
                    playback_done=130,
                )
                if case != 'no_pair':
                    voice['stages'].update(
                        confirmed_playout_ack=105,
                        confirmed_playback_done=110 if case == 'wrong_pair' else 130,
                    )
                if case != 'no_timing':
                    voice['device_pause_from_trigger_ms'] = 35.5
                if case == 'valid_stop':
                    voice[VAD_ONSET_PAUSE] = 165.5
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
                    self.assertEqual(
                        kwargs,
                        {
                            'interruption': True,
                            **({'expected_input': run.prepared_input} if known else {}),
                        },
                    )
                    health['playing'] = False
                    announcement['outcome'] = (
                        'completed' if case == 'completed_announcement' else 'cancelled'
                    )
                    rows.append(voice)
                    if case != 'missing_transcript':
                        transcript = {
                            'generation': 20 if case == 'stale_transcript' else 21,
                            'matched': not case.startswith('wrong_transcript'),
                        }
                        if case == 'transcript_content':
                            transcript['text'] = 'content must never enter a report'
                        d['test_interruption_transcript'] = transcript
                    if d.get('test_playback_stop_supported') is True and case != 'missing_stop':
                        d['test_playback_stop'] = {
                            'generation': 19 if case == 'wrong_stop_generation' else 20,
                            'cancel_generation': 22
                            if case in ('wrong_stop_cancel_generation', 'wrong_transcript_bad_stop')
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
                    if known and payload.get('operation') == 'known_speech_arm':
                        self.assertEqual(
                            payload,
                            {
                                'operation': 'known_speech_arm',
                                'generation': 20,
                                **device.interruption_fixture.return_value,
                            },
                        )
                        return {
                            'status': 'armed',
                            'token': 'a' * 32,
                            **({'path': 'private'} if case == 'known_bad_arm' else {}),
                        }
                    if known and payload.get('operation') == 'known_speech_result':
                        self.assertEqual(
                            payload, {'operation': 'known_speech_result', 'token': 'a' * 32}
                        )
                        return near_result
                    if (
                        case.startswith('known_room')
                        and payload.get('operation') == 'known_speech_stimulus'
                    ):
                        self.assertEqual(
                            payload,
                            {
                                'operation': 'known_speech_stimulus',
                                'token': 'a' * 32,
                                'verify_far': True,
                            },
                        )
                        if case == 'known_room_boundary_invalid':
                            return {'status': 'unavailable'}
                        if case == 'known_room_overlap_timeout' or (
                            case == 'known_room_overlap_late' and now[0] == 1.0
                        ):
                            return {
                                'status': 'awaiting_overlap',
                                'boundary': {
                                    'status': 'pending',
                                    'pre_stimulus_mic_frames': 16000,
                                    'pre_stimulus_render_frames': 16384,
                                },
                                'coverage': {
                                    'estimated_mic_start_relative_to_render_frames': -7209,
                                    'estimated_overlap_frames': 8791,
                                    'overlap_reference_rms': None,
                                },
                            }
                        self.assertEqual(now[0], 1.5 if case == 'known_room_overlap_late' else 1.0)
                        cutoff = CUTOFF + (160 if case == 'known_room_boundary_mismatch' else 0)
                        transfer = analyse(
                            mic if case != 'known_room_far_miss' else mic * 0,
                            far,
                            None,
                            cutoff,
                            CUTOFF,
                        )
                        if case == 'known_room_playback_changed':
                            health['playing'] = False
                        return {
                            'status': 'checked',
                            'boundary': {
                                'status': 'recorded',
                                'pre_stimulus_mic_frames': cutoff,
                                'pre_stimulus_render_frames': CUTOFF,
                            },
                            'coverage': {
                                'estimated_mic_start_relative_to_render_frames': 0,
                                'estimated_overlap_frames': CUTOFF,
                                'overlap_reference_rms': 0.3,
                            },
                            'transfer': transfer,
                        }
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
                    expected_time = 21 if case.startswith('known_room') else 20.25 if known else 20
                    self.assertEqual(
                        now[0], expected_time + (0.5 if case == 'known_room_overlap_late' else 0)
                    )
                    evidence = run.report('passed')['barge_evidence']
                    self.assertEqual(evidence['device_pause_from_trigger_ms'], 35.5)
                    self.assertEqual(
                        evidence[VAD_ONSET_PAUSE], 165.5 if case == 'valid_stop' else None
                    )
                    self.assertEqual(evidence['post_playback_quiet_seconds'], 20)
                    self.assertTrue(evidence['replacement_completed'])
                    self.assertEqual(evidence['response_playout_timing'][QA_PLAYOUT_METRIC], 25)
                    self.assertIs(evidence['transcript_matches_stimulus'], True)
                    if strict:
                        self.assertEqual(evidence['credible_render_overlap_frames'], 1)
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
                    self.assertNotIn('private-content', json.dumps(run.report('failed')))
                    if case == 'known_miss':
                        self.assertEqual(now[0], 20.25)
                    if case.startswith('wrong_transcript'):
                        self.assertIs(evidence['transcript_matches_stimulus'], False)
                        self.assertIs(evidence['device_stop_applied'], case == 'wrong_transcript')
                        self.assertEqual(evidence['replacement_capture']['frames'], 2)
                        self.assertEqual(
                            evidence['asr_input_samples'],
                            8160 if case == 'wrong_transcript' else None,
                        )
                        self.assertNotIn('9876543210', json.dumps(evidence))
                if case in ('prefix_and_render', 'prefix_only'):
                    self.assertEqual(
                        evidence['onset_prefix'], dict.fromkeys(ONSET_PREFIX_COUNTS, 13)
                    )
                    self.assertEqual(
                        evidence['credible_render_overlap_frames'], int(case == 'prefix_and_render')
                    )
                elif strict:
                    self.assertIsNone(evidence.get('onset_prefix'))
                if case in (
                    'early_voice',
                    'no_playout',
                    'known_bad_arm',
                    'known_room_boundary_invalid',
                    'known_room_overlap_timeout',
                    'known_room_far_miss',
                    'known_room_playback_changed',
                ):
                    device.speak_probe.assert_not_called()
                else:
                    device.speak_probe.assert_called_once_with(
                        interruption=True,
                        **({'expected_input': run.prepared_input} if known else {}),
                    )

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
        for expected, scenario in (('qa', 'qa'), ('qa', 'tool'), ('web', 'web'), ('tool', 'tool')):
            with self.subTest(expected=expected, scenario=scenario):
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
                    dot_turn_start=13,
                    dot_turn_ack=15,
                    dot_first_delta=24,
                    first_clause=30,
                    tts_start=32,
                    first_pcm=35,
                    playout_ack=50,
                    playback_done=60,
                    confirmed_playout_ack=50,
                    confirmed_playback_done=60,
                )
                if scenario == 'web':
                    row['stages']['web_route_completed'] = 27
                if scenario == 'tool':
                    row['stages']['tool_route_completed'] = 27
                run, device, _, _ = self.acoustic_fixture(lambda _: [row], awake=True)
                run.latency_sample = True
                run.latency_scenario = expected
                if expected == 'tool':
                    run.tool_sample_token = 'a' * 32
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
                    {
                        **dict(zip(QA_STAGE_METRICS, (5, 20, 3))),
                        'dot_preparation_ms': 3,
                        'dot_turn_ack_ms': 2,
                        'dot_first_delta_ms': 11,
                        'dot_delta_to_clause_ms': 6,
                    }
                    if scenario == expected
                    else {},
                )
                self.assertEqual(result['acoustic_evidence']['latency_scenario'], scenario)
                self.assertEqual(
                    result['acoustic_evidence']['latency_route_completed'],
                    expected in ('web', 'tool'),
                )
                run.handover.assert_not_called()
                run.playback.assert_not_called()
                device.speak_probe.assert_called_once_with(question=ACOUSTIC_QUESTION)

    def test_fixed_math_reports_only_current_completed_answer_category(self):
        for question, scenario, answer, expected in (
            (ACOUSTIC_QUESTION, 'qa', '一加一等于 **二**。', 'matched'),
            (ACOUSTIC_QUESTION, 'qa', '答案是 12。', 'mismatch'),
            (ACOUSTIC_QUESTION, 'qa', '二或三 private body', 'unconfirmed'),
            (ACOUSTIC_QUESTION, 'qa', None, 'unconfirmed'),
            (SCENARIO_QUESTIONS['tool'][-1], 'tool', '５＋６＝十一。', 'matched'),
            (SCENARIO_QUESTIONS['tool'][-1], 'tool', '1 1', 'unconfirmed'),
            (SCENARIO_QUESTIONS['tool'][-1], 'tool', '5 + 5 = 11', 'mismatch'),
            (
                SCENARIO_QUESTIONS['tool'][0],
                'tool',
                '本机只读终端命令计算结果：一加一等于二。',
                'matched',
            ),
            (
                SCENARIO_QUESTIONS['tool'][0],
                'tool',
                '本机只读终端命令计算结果：一加一等于三。',
                'mismatch',
            ),
            (
                SCENARIO_QUESTIONS['tool'][0],
                'tool',
                '本机只读终端命令计算结果：一加一等于二，或者等于三。',
                'unconfirmed',
            ),
            (SCENARIO_QUESTIONS['tool'][0], 'tool', '本机终端计算结果：一加一等于 2。', 'matched'),
            (SCENARIO_QUESTIONS['tool'][0], 'tool', '本机终端计算结果：一加一等于 3。', 'mismatch'),
            (
                SCENARIO_QUESTIONS['tool'][0],
                'tool',
                '本机终端计算结果：一加一等于二，或者等于三。',
                'unconfirmed',
            ),
            (ACOUSTIC_QUESTION, 'qa', '本机终端计算结果：一加一等于 2。', 'unconfirmed'),
            (ACOUSTIC_QUESTION, 'qa', '本机只读终端命令计算结果：一加一等于二。', 'unconfirmed'),
            (
                SCENARIO_QUESTIONS['tool'][0],
                'tool',
                '本机只读终端计算结果：一加一等于 2。',
                'matched',
            ),
            (
                SCENARIO_QUESTIONS['tool'][0],
                'tool',
                '本机只读终端计算结果：一加一等于 3。',
                'mismatch',
            ),
            (
                SCENARIO_QUESTIONS['tool'][0],
                'tool',
                '本机只读终端计算结果：一加一等于二，或者等于三。',
                'unconfirmed',
            ),
            (ACOUSTIC_QUESTION, 'qa', '本机只读终端计算结果：一加一等于 2。', 'unconfirmed'),
            (SCENARIO_QUESTIONS['web'][0], 'web', '答案是二', None),
        ):
            with self.subTest(scenario=scenario, answer=answer):
                row = self.acoustic_row(scenario=scenario)
                run, _, _, now = self.acoustic_fixture(
                    lambda _: [row],
                    awake=True,
                    tasks=[
                        {'generation': 7, 'status': 'completed', 'title': question, 'result': '二'},
                        {
                            'generation': 8,
                            'status': 'completed',
                            'title': question,
                            'result': answer,
                        },
                    ],
                )
                run.latency_sample = True
                run.qa_question = question
                run.latency_scenario = scenario
                if scenario == 'tool':
                    run.tool_sample_token = 'a' * 32
                run.acoustic_probe(awake=True)
                self.assertEqual(now[0], POST_DIALOGUE_SECONDS)
                evidence = run.report('passed')['acoustic_evidence']
                self.assertEqual(evidence.get('math_answer_status'), expected)
                if scenario == 'web':
                    self.assertNotIn('math_answer_status', evidence)
                if answer:
                    self.assertNotIn(answer, json.dumps(evidence, ensure_ascii=False))

    def test_tool_latency_refuses_unarmed_stimulus_and_stops_on_pre_dispatch_rejection(self):
        row = {
            'request_id': 'PRIVATE_REJECTED',
            'origin': 'voice',
            'generation': None,
            'outcome': 'rejected',
            'gate': 'tool_sample_guard',
            'tool_sample_rejection': 'transcript_mismatch',
            'stages': {'asr_end': 1},
        }
        run, device, _, now = self.acoustic_fixture(lambda _: [row], awake=True)
        run.latency_sample = True
        run.latency_scenario = 'tool'
        run.qa_question = SCENARIO_QUESTIONS['tool'][0]
        with self.assertRaisesRegex(CheckFailure, 'tool_sample_guard_not_ready'):
            run.acoustic_probe(awake=True)
        device.speak_probe.assert_not_called()
        run.tool_sample_token = 'a' * 32
        with self.assertRaisesRegex(CheckFailure, 'tool_sample_transcript_not_confirmed'):
            run.acoustic_probe(awake=True)
        device.speak_probe.assert_called_once_with(question=run.qa_question)
        self.assertLess(now[0], 1)
        evidence = run.report('failed')['acoustic_evidence']
        self.assertEqual(evidence['rejected_voice_gates']['tool_sample_guard'], 1)
        self.assertEqual(evidence.get('tool_sample_rejections'), {'transcript_mismatch': 1})
        self.assertTrue(evidence['tool_sample_guarded'])
        self.assertEqual(evidence['admitted_voice_requests'], 0)
        self.assertNotIn('PRIVATE', json.dumps(evidence))

    def test_tool_latency_keeps_only_bounded_input_from_the_sole_transcript_rejection(self):
        row = self.rejected_candidate(
            gate='tool_sample_guard',
            tool_sample_rejection='read_only_missing',
            stages={'wake_detected': 1, 'asr_start': 2, 'asr_end': 3},
            asr_input_samples=98720,
        )
        for changes, extra, expected in (
            ({}, [], {'asr_input_samples': 98720, 'capture': {'status': 'missing'}}),
            (
                {'asr_input_samples': True, 'capture': {'raw_audio': 'PRIVATE_AUDIO'}},
                [],
                {'asr_input_samples': None, 'capture': {'status': 'malformed'}},
            ),
            ({'tool_sample_rejection': 'route_changed'}, [], None),
            ({'turn_id': 'PRIVATE_TURN'}, [], None),
            (None, [], None),
            ({'stages': {'asr_start': 3, 'asr_end': 2}}, [], None),
            ({'stages': {'asr_start': 2, 'asr_end': 'PRIVATE_TIME'}}, [], None),
            ({'stages': {'asr_start': 2, 'asr_end': 3, 'dot_turn_start': 4}}, [], None),
            ({}, [self.rejected_candidate(request_id='PRIVATE_EXTRA')], None),
        ):
            with self.subTest(changes=changes, extra=len(extra)):
                candidate = {**row, **(changes or {})}
                if changes is None:
                    del candidate['stages']
                run, _, _, _ = self.acoustic_fixture(lambda _: [candidate, *extra], awake=True)
                run.latency_sample = True
                run.latency_scenario = 'tool'
                run.qa_question = SCENARIO_QUESTIONS['tool'][1]
                run.tool_sample_token = 'a' * 32
                reason = (
                    'tool_sample_guard_not_confirmed'
                    if (changes or {}).get('tool_sample_rejection') == 'route_changed'
                    else 'tool_sample_transcript_not_confirmed'
                )
                with self.assertRaisesRegex(CheckFailure, reason):
                    run.acoustic_probe(awake=True)
                evidence = run.report('failed')['acoustic_evidence']
                self.assertEqual(evidence.get('sole_tool_rejection_input'), expected)
                self.assertNotIn('PRIVATE', json.dumps(evidence))

    def test_tool_rejection_diagnosis_is_retained_only_for_the_requested_engine(self):
        comparison = {
            'primary': 'read_only_missing',
            'local': 'matched',
            'local_engine': 'sensevoice',
        }
        for engine, value, retained in (
            ('sensevoice', comparison, True),
            (None, comparison, False),
            ('whisper', comparison, False),
            ('sensevoice', {**comparison, 'primary': 'arithmetic_mismatch'}, False),
            ('sensevoice', {**comparison, 'local': 'PRIVATE_TEXT'}, False),
        ):
            with self.subTest(engine=engine, value=value):
                row = self.rejected_candidate(
                    gate='tool_sample_guard',
                    tool_sample_rejection='read_only_missing',
                    stages={'wake_detected': 1, 'asr_start': 2, 'asr_end': 3},
                    asr_input_samples=98720,
                    tool_asr_comparison=value,
                )
                run, device, _, _ = self.acoustic_fixture(lambda _: [row], awake=True)
                run.latency_sample = True
                run.latency_scenario = 'tool'
                run.qa_question = SCENARIO_QUESTIONS['tool'][1]
                run.tool_sample_token = 'a' * 32
                run.tool_diagnose_engine = engine
                with self.assertRaisesRegex(CheckFailure, 'tool_sample_transcript_not_confirmed'):
                    run.acoustic_probe(awake=True)
                evidence = run.report('failed')['acoustic_evidence']
                self.assertEqual(evidence['admitted_voice_requests'], 0)
                self.assertEqual(
                    evidence['sole_tool_rejection_input'].get('tool_asr_comparison'),
                    comparison if retained else None,
                )
                self.assertNotIn('PRIVATE', json.dumps(evidence))
                device.speak_probe.assert_called_once_with(question=run.qa_question)

    def test_tool_rejection_diagnosis_plan_requires_tool_execution(self):
        base = ['--runtime-root', '/unused', '--capture', '--tool-diagnose-rejection', 'sensevoice']
        for mode in (['--awake-dialogue', '--tool-case', '2'], ['--qa-latency-suite']):
            with (
                patch('device_smoke.Device') as device,
                patch('device_smoke.rpc') as rpc,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(main(base + mode + ['--latency-scenario', 'tool']), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan['minimum_backend_version'], '0.15.20')
            self.assertEqual(plan['tool_rejection_diagnose_engine'], 'sensevoice')
            device.assert_not_called()
            rpc.assert_not_called()
        for mode in (
            ['--awake-dialogue'],
            ['--awake-dialogue', '--tool-case', '2', '--tool-asr-comparison', 'whisper'],
            ['--qa-latency-suite', '--latency-scenario', 'web'],
        ):
            with self.subTest(mode=mode), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(base + mode + ['--execute'])
                self.assertEqual(error.exception.code, 2)

    def test_tool_rejection_diagnosis_requires_supported_distinct_engine_before_device_access(self):
        from release_info import RELEASE

        args = [
            '--runtime-root',
            '/unused',
            '--capture',
            '--awake-dialogue',
            '--latency-scenario',
            'tool',
            '--tool-case',
            '2',
            '--tool-diagnose-rejection',
            'sensevoice',
            '--execute',
        ]
        for supported, primary in ((False, 'minimax'), (True, 'sensevoice')):
            health = dict(
                bridge_version=RELEASE.backend_version,
                protocol_version=RELEASE.protocol_version,
                release_id=RELEASE.release_id,
                runtime_instance='private',
                tool_sample_guard_supported=True,
                tool_rejection_diagnosis_supported=supported,
                asr_engine=primary,
            )
            with (
                self.subTest(supported=supported, primary=primary),
                patch('device_smoke.rpc', return_value=health),
                patch('device_smoke.load_release', return_value=RELEASE),
                patch('device_smoke.Device') as device,
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
                device.assert_not_called()

    def test_tool_latency_distinguishes_an_extra_candidate_after_a_matched_answer(self):
        from test_capture_evidence import evidence as capture_evidence

        question = SCENARIO_QUESTIONS['tool'][0]
        answer = self.acoustic_row(scenario='tool')
        extra = self.rejected_candidate(
            gate='tool_sample_guard',
            tool_sample_rejection='not_open',
            stages={'voice_detected': 0, 'asr_start': 2, 'asr_end': 3},
            asr_input_samples=22880,
        )
        valid = capture_evidence()
        for arrives_after_answer, capture, summary in (
            (True, valid, {'status': 'observed', **valid}),
            (True, {'raw_audio': 'PRIVATE_AUDIO'}, {'status': 'malformed'}),
            (False, valid, None),
        ):
            with self.subTest(arrives_after_answer=arrives_after_answer, summary=summary):
                run, device, _, _ = self.acoustic_fixture(
                    lambda elapsed: (
                        [answer, {**extra, 'capture': capture}]
                        if not arrives_after_answer or elapsed >= 19.5
                        else [answer]
                    ),
                    awake=True,
                    tasks=[dict(generation=8, status='completed', title=question, result='二')],
                )
                run.latency_sample = True
                run.latency_scenario = 'tool'
                run.qa_question = question
                run.tool_sample_token = 'a' * 32
                with self.assertRaisesRegex(CheckFailure, 'tool_sample_extra_voice_candidate'):
                    run.acoustic_probe(awake=True)
                evidence = run.report('failed')['acoustic_evidence']
                self.assertEqual(evidence['completed_tasks'], 1)
                self.assertEqual(evidence['tool_sample_rejections'], {'not_open': 1})
                self.assertNotIn('sole_tool_rejection_input', evidence)
                self.assertEqual(
                    evidence.get('post_answer_tool_rejection_input'),
                    {'asr_input_samples': 22880, 'capture': summary} if summary else None,
                )
                self.assertNotIn('PRIVATE', json.dumps(evidence))
                device.speak_probe.assert_called_once_with(question=question)

    def test_tool_suite_requires_backend_guard_before_constructing_device(self):
        from release_info import RELEASE

        health = dict(
            bridge_version=RELEASE.backend_version,
            protocol_version=RELEASE.protocol_version,
            release_id=RELEASE.release_id,
            runtime_instance='private-unready',
        )
        base = ['--runtime-root', '/unused', '--capture', '--latency-scenario', 'tool', '--execute']
        for flags in (['--qa-latency-suite'], ['--awake-dialogue', '--tool-case', '4']):
            with (
                self.subTest(flags=flags),
                patch('device_smoke.rpc', return_value=health),
                patch('device_smoke.load_release', return_value=RELEASE),
                patch('device_smoke.Device') as device,
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as error:
                    main(base + flags)
                self.assertEqual(error.exception.code, 2)
                device.assert_not_called()

    def test_fixed_tool_comparison_plan_validates_case_and_individual_capture(self):
        base = ['--runtime-root', '/unused']
        flags = ['--tool-asr-comparison', 'sensevoice', '--tool-case', '5']
        with patch('device_smoke.Device') as device, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(base + flags + ['--capture', '--awake-dialogue']), 0)
        device.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['scope'], 'fixed_tool_asr_comparison')
        self.assertEqual((plan['tool_case'], plan['tool_compare_engine']), (5, 'sensevoice'))
        self.assertFalse(plan['dot_dispatch_expected'])
        self.assertIn('fixed_tool_asr_comparison', plan['checks'])
        self.assertNotIn('synthetic_awake_dialogue', plan['checks'])
        for invalid in (
            flags,
            flags[:-1] + ['31', '--capture', '--awake-dialogue'],
            ['--tool-case', '5'],
        ):
            with (
                self.subTest(invalid=invalid),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(base + invalid)

    def test_fixed_tool_execution_plan_binds_one_public_case_without_accessing_hardware(self):
        base = ['--runtime-root', '/unused']
        flags = ['--capture', '--awake-dialogue', '--latency-scenario', 'tool']
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as rpc,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(base + flags + ['--tool-case', '4', '--response-acoustic-evidence']), 0
            )
        device.assert_not_called()
        rpc.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertFalse(plan['execute'])
        self.assertEqual((plan['scope'], plan['tool_case']), ('fixed_tool_case', 4))
        self.assertTrue(plan['dot_dispatch_expected'])
        self.assertTrue(plan['response_acoustic_evidence'])
        self.assertEqual(plan['checks'], planned_checks(set(QA_LATENCY_PHASES[0][1])))
        for invalid in (
            flags,
            flags + ['--tool-case', '31'],
            flags + ['--tool-case', '0'],
            flags + ['--tool-case', '4', '--qa-latency-suite'],
            flags + ['--tool-case', '4', '--english-wake'],
            flags + ['--tool-case', '4', '--tool-asr-comparison', 'sensevoice'],
            flags + ['--tool-case', '4', '--collect-acoustic-coverage'],
            ['--capture', '--latency-scenario', 'tool', '--tool-case', '4'],
        ):
            with self.subTest(invalid=invalid), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(base + invalid + ['--execute'])
                self.assertEqual(error.exception.code, 2)

    def test_fixed_tool_execution_requires_route_playout_timing_and_correct_answer(self):
        question = SCENARIO_QUESTIONS['tool'][0]
        for missing, answer, error in (
            (None, '二', None),
            ('tool_route_completed', '二', 'route_not_confirmed'),
            ('confirmed_playback_done', '二', 'playout_not_confirmed'),
            ('tts_start', '二', 'stage_timing_incomplete'),
            (None, '三', 'answer_not_confirmed'),
        ):
            with self.subTest(missing=missing, answer=answer):
                row = self.acoustic_row(
                    request_id='a' * 32,
                    scenario='tool',
                    error=None,
                    gate='accepted',
                    confirmed_playback_clauses=1,
                )
                del row['text']
                row['stages'].update(
                    utterance_end=0,
                    asr_end=5,
                    brain_start=10,
                    tool_route_completed=27,
                    first_clause=30,
                    tts_start=32,
                    first_pcm=35,
                    playout_ack=50,
                    playback_done=60,
                    confirmed_playout_ack=50,
                    confirmed_playback_done=60,
                )
                if missing is not None:
                    row['stages'].pop(missing)
                run, device, _, now = self.acoustic_fixture(
                    lambda _: [row],
                    awake=True,
                    tasks=[
                        {
                            'generation': 8,
                            'status': 'completed',
                            'title': question,
                            'result': answer,
                        }
                    ],
                )
                run.latency_sample = run.fixed_tool_case = True
                run.latency_scenario = 'tool'
                run.qa_question = question
                run.tool_sample_token = 'a' * 32
                if error is None:
                    run.acoustic_probe(awake=True)
                else:
                    with self.assertRaisesRegex(CheckFailure, f'fixed_tool_{error}'):
                        run.acoustic_probe(awake=True)
                report = run.report('failed' if error else 'passed')
                self.assertEqual((report['scope'], report['tool_case']), ('fixed_tool_case', 1))
                self.assertEqual(now[0], POST_DIALOGUE_SECONDS)
                self.assertNotIn(question, json.dumps(report, ensure_ascii=False))
                device.speak_probe.assert_called_once_with(question=question)

    def test_fixed_tool_execution_binds_case_runtime_and_lock(self):
        from release_info import RELEASE

        health = dict(
            bridge_version=RELEASE.backend_version,
            protocol_version=RELEASE.protocol_version,
            release_id=RELEASE.release_id,
            runtime_instance='owner-private-instance',
            tool_sample_guard_supported=True,
            response_acoustic_evidence_supported=True,
        )
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve()

            def run(device, request, *, checkpoint, **options):
                self.assertTrue(options['fixed_tool_case'])
                self.assertTrue(options['latency_sample'])
                self.assertTrue(options['response_acoustic_evidence'])
                self.assertFalse(options['tool_local_compare'])
                self.assertEqual(options['qa_question'], SCENARIO_QUESTIONS['tool'][3])
                with (directory / 'automation-test.lock').open('rb') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(request({'operation': 'health'}), health)
                result = dict(state='passed', checks=[], cleanup_microphone_off_verified=True)
                checkpoint(result)
                return Mock(run=Mock(return_value=result))

            with (
                patch('device_smoke.rpc', return_value=health) as rpc,
                patch('device_smoke.load_release', return_value=RELEASE),
                patch('device_smoke.private_directory', return_value=directory),
                patch('device_smoke.Device') as device,
                patch('device_smoke.SmokeRun', side_effect=run) as smoke,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            '--runtime-root',
                            str(directory),
                            '--capture',
                            '--awake-dialogue',
                            '--latency-scenario',
                            'tool',
                            '--tool-case',
                            '4',
                            '--response-acoustic-evidence',
                            '--execute',
                        ]
                    ),
                    0,
                )
            device.assert_called_once()
            smoke.assert_called_once()
            self.assertEqual(rpc.call_count, 3)
            report = next(directory.glob('device-*.json')).read_text()
            self.assertNotIn(health['runtime_instance'], report)
            self.assertEqual(json.loads(report)['release_id'], RELEASE.release_id)

    def test_tool_comparison_measures_rejected_pair_without_any_task_dispatch(self):
        result = {'primary': 'read_only_missing', 'local': 'matched', 'local_engine': 'sensevoice'}
        row = {
            'request_id': 'private',
            'origin': 'voice',
            'outcome': 'rejected',
            'gate': 'tool_sample_guard',
            'tool_sample_rejection': 'comparison_only',
            'stages': {'wake_detected': 1, 'asr_start': 2, 'asr_end': 3},
            'tool_asr_comparison': result,
            'asr_input_samples': 99040,
        }
        for overrides, error in (
            ({}, None),
            (
                {'tool_asr_comparison': {**result, 'local_engine': 'owner-private'}},
                'receipt_invalid',
            ),
            ({'asr_input_samples': True}, 'receipt_invalid'),
            ({'stages': {**row['stages'], 'brain_start': 4}}, 'unexpected_voice_activity'),
            ({'gate': 'wake_closed', 'stages': {}}, 'local_wake_not_confirmed'),
            ({'tool_asr_comparison': {**result, 'local': 'timeout'}}, 'decode_failed'),
        ):
            with self.subTest(error=error):
                run, device, _, _ = self.acoustic_fixture(
                    lambda _: [{**row, **overrides}], awake=True
                )
                run.latency_sample = True
                run.latency_scenario = 'tool'
                run.qa_question = SCENARIO_QUESTIONS['tool'][4]
                run.tool_local_compare = True
                run.tool_compare_engine = 'sensevoice'
                run.tool_sample_token = 'a' * 32
                if error is None:
                    run.acoustic_probe(awake=True)
                    report = run.report('passed')
                    self.assertEqual(report['tool_case'], 5)
                    evidence = report['acoustic_evidence']
                    self.assertEqual(evidence['tool_asr_comparison'], result)
                    self.assertEqual(evidence['same_input_samples'], 99040)
                    self.assertNotIn('private', json.dumps(evidence))
                else:
                    with self.assertRaisesRegex(CheckFailure, f'tool_comparison_{error}'):
                        run.acoustic_probe(awake=True)
                device.speak_probe.assert_called_once_with(question=run.qa_question)

    def test_tool_comparison_rejects_using_the_same_engine_before_device_access(self):
        from release_info import RELEASE

        health = {
            'bridge_version': RELEASE.backend_version,
            'protocol_version': RELEASE.protocol_version,
            'release_id': RELEASE.release_id,
            'runtime_instance': 'private',
            'tool_sample_guard_supported': True,
            'asr_engine': 'sensevoice',
        }
        with (
            patch('device_smoke.rpc', return_value=health),
            patch('device_smoke.load_release', return_value=RELEASE),
            patch('device_smoke.Device') as device,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(
                [
                    '--runtime-root',
                    '/unused',
                    '--capture',
                    '--awake-dialogue',
                    '--tool-asr-comparison',
                    'sensevoice',
                    '--tool-case',
                    '5',
                    '--execute',
                ]
            )
        self.assertEqual(error.exception.code, 2)
        device.assert_not_called()

    def test_tool_guard_arms_before_a_capture_is_allowed_to_start(self):
        run, device, _, _ = self.acoustic_fixture(lambda _: [], awake=True)
        run.latency_sample = True
        run.latency_scenario = 'tool'
        run.qa_question = SCENARIO_QUESTIONS['tool'][0]
        run.request = Mock(return_value={'status': 'unavailable'})
        with self.assertRaisesRegex(CheckFailure, 'tool_sample_guard_not_armed'):
            run.start_capture()
        device.open.assert_not_called()
        run.request.assert_called_once_with({'operation': 'tool_sample_arm', 'case': 1})
        original = run.request
        run.request = Mock(return_value={'status': 'armed', 'token': 'a' * 32})
        run.health = Mock(
            return_value={'microphone_active': True, 'calibrating': False, 'local_wake_ready': True}
        )
        device.open.side_effect = lambda *a, **k: self.assertEqual(run.tool_sample_token, 'a' * 32)
        run.start_capture()
        run.request.assert_called_once_with(original.call_args.args[0])
        device.open.assert_called_once_with(True, capture_limit_ms=run.capture_limit_ms)

        run.tool_local_compare = True
        run.tool_compare_engine = 'sensevoice'
        run.request.reset_mock()
        run.start_capture()
        run.request.assert_called_once_with(
            {'operation': 'tool_sample_compare_arm', 'case': 1, 'engine': 'sensevoice'}
        )

        run.tool_local_compare = False
        run.tool_compare_engine = 'whisper'
        run.tool_diagnose_engine = 'sensevoice'
        run.request.reset_mock()
        run.start_capture()
        run.request.assert_called_once_with(
            {'operation': 'tool_sample_diagnose_arm', 'case': 1, 'engine': 'sensevoice'}
        )

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

    def test_acoustic_timeout_distinguishes_capture_from_local_wake_rejection(self):
        capture = {
            'frames': 290,
            'voiced_frames': 177,
            'credible_frames': 160,
            'render_frames': 0,
            'pause_requested_frames': 0,
            'voiced_after_pause_request_frames': 0,
            'raw_correlated_frames': 0,
            'residual_correlated_frames': 0,
            'raw_peak_dbfs': -19.1,
            'cleaned_peak_dbfs': -18.1,
            'render_peak_dbfs': -120.0,
            'max_raw_correlation': 0,
            'max_residual_correlation': 0,
            'echo_risk': False,
            'during_playback': False,
            'local_wake': False,
        }
        for value, expected in (
            (None, {'status': 'missing'}),
            ({**capture, 'raw_audio': 'PRIVATE_AUDIO'}, {'status': 'malformed'}),
            (capture, {'status': 'observed', **capture}),
        ):
            with self.subTest(expected=expected['status']):
                candidate = self.rejected_candidate(
                    gate='wake_closed', stages={'voice_detected': 0}, text='PRIVATE_TEXT'
                )
                if value is not None:
                    candidate['capture'] = value
                run, device, health, _ = self.acoustic_fixture(
                    lambda _: [candidate],
                    awake=True,
                    english_wake=True,
                    before=[self.rejected_candidate(request_id='PRIVATE_HISTORICAL')],
                )
                health.update(local_wake_count=0, local_wake_english_count=0, mic_frames=320)
                speak = device.speak_probe.side_effect

                def speak_and_advance(**kwargs):
                    speak(**kwargs)
                    health['mic_frames'] = 640

                device.speak_probe.side_effect = speak_and_advance
                with self.assertRaisesRegex(CheckFailure, 'synthetic_acoustic_chain_not_confirmed'):
                    run.acoustic_probe(awake=True)
                evidence = run.report('failed')['acoustic_evidence']
                self.assertEqual(evidence['new_mic_frames'], 320)
                self.assertEqual(evidence['rejected_voice_gates']['wake_closed'], 1)
                self.assertEqual(evidence['rejected_voice_gates']['echo_text'], 0)
                self.assertEqual(evidence['stage_counts']['asr_start'], 0)
                self.assertEqual(evidence['sole_wake_rejection_capture'], expected)
                self.assertNotIn('PRIVATE', json.dumps(evidence))

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
        self.assertEqual(evidence['rejected_voice_gates']['echo_text'], 1)
        self.assertIsNone(evidence['sole_wake_rejection_capture'])
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
            {'operation': 'response_probe_arm', 'case': len(ALL_QUESTIONS) + 1},
            {'operation': 'response_probe_arm', 'case': 30, 'text': 'PRIVATE'},
        ):
            with patch('device_smoke.socket.socket') as connection:
                with self.assertRaises(CheckFailure):
                    rpc(Path('/nonexistent'), request)
                connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
