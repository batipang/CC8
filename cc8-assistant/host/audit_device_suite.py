"""Recheck a saved fixed device suite or synthetic latency batch without device access."""

import argparse
import json
from pathlib import Path
import re
from types import SimpleNamespace

from device_smoke import (
    AWAKE_DIALOGUE_CHECK,
    ENGLISH_WAKE_PHASES,
    ECHO_SOAK_MIN_BACKEND,
    ECHO_SOAK_CHECK,
    ECHO_SOAK_CAPTURE_MS,
    CheckFailure,
    ECHO_SOAK_SCOPE,
    ECHO_SOAK_SECONDS,
    LEGACY_ECHO_SOAK_SECONDS,
    POST_DIALOGUE_SECONDS,
    ROUTED_LATENCY_MIN_BACKEND,
    RENDER_OVERLAP_MIN_BACKEND,
    STANDBY_SUITE_SCOPE,
    TOOL_REJECTION_MIN_BACKEND,
    TOOL_SAMPLE_MIN_BACKEND,
    device_suite_phases,
    matched_playout,
    interruption_input,
    STOP_VISUAL_MIN_APK_CODE,
    EXTENDED_VOICE_MIN_BACKEND,
    planned_checks,
    qa_fixture_matches,
    run_device_suite,
    safe_tool_transcript_miss,
    standby_failure,
    tool_dialogue_failure_reason,
    valid_tv_prime,
)
from mac_voice import validate_prepared_speech
from chinese_wake_evidence import valid_counter_delta
from private_output import read_private_json
from probe_idle import valid_evidence as valid_probe_idle_evidence
from qa_speech import SCENARIO_DIGESTS
from soak_background import valid_background
from capture_frontend_evidence import (
    FRONTENDS,
    matches_expected as matches_capture_frontend,
    valid_report as valid_capture_frontend_report,
)
from wake_input_receipt import FIXTURE_FRAMES, FIXTURE_PCM_SHA256, FIXTURE_WAV_SHA256
from wake_input_receipt import summarize as summarize_wake_input

VERSION = r'[0-9]{1,8}(?:\.[0-9]{1,8}){2}'
SOURCE_DIGEST = r'[a-f0-9]{64}'
SOURCE_FIELDS = ('automation_source_sha256', 'speech_probe_source_sha256')
TOOL_DIALOGUE_FAILURES = (
    'tool_sample_extra_voice_candidate',
    'tool_sample_guard_not_confirmed',
    'tool_sample_transcript_not_confirmed',
)


def bounded_number(value, low, high):
    return type(value) in (int, float) and low <= value <= high


def content_free_phase(report):
    return (
        type(report.get('schema')) is int
        and report['schema'] == 1
        and report.get('capture_tests_enabled') is True
        and report.get('raw_audio_saved') is False
        and report.get('contains_conversation_content') is False
        and valid_capture_frontend_report(report)
        and valid_probe_idle_evidence(
            report.get('pre_probe_idle_evidence'), passed=report.get('state') == 'passed'
        )
        and all(
            isinstance(report.get(key), str) and re.fullmatch(SOURCE_DIGEST, report[key])
            for key in SOURCE_FIELDS
        )
    )


def phase_apk_identity(report, expect_apk_code, previous):
    if not isinstance(report, dict):
        return None
    version, code = report.get('expected_apk_version'), report.get('expected_apk_code')
    if (
        not isinstance(version, str)
        or not re.fullmatch(VERSION, version)
        or type(code) is not int
        or not 0 < code < 2**31
        or (expect_apk_code is not None and code != expect_apk_code)
        or (previous is not None and previous != (version, code))
    ):
        return None
    return version, code


def recorded_tool_failure(report, expected, previous):
    if not (
        isinstance(report, dict)
        and content_free_phase(report)
        and (previous is None or all(report[key] == previous[key] for key in SOURCE_FIELDS))
    ):
        return None
    reason = tool_dialogue_failure_reason(report, expected)
    return reason if reason in TOOL_DIALOGUE_FAILURES else None


def recorded_tv_failure(report, expected, background):
    if not (
        isinstance(report, dict)
        and content_free_phase(report)
        and report.get('state') == 'failed'
        and report.get('cleanup_microphone_off_verified') is True
        and valid_tv_prime(report.get('acoustic_evidence'))
    ):
        return None
    checks = report.get('checks')
    stop = expected.index(ECHO_SOAK_CHECK)
    if not (
        isinstance(checks, list)
        and len(checks) == stop + 1
        and all(isinstance(row, dict) for row in checks)
        and [(row.get('name'), row.get('state')) for row in checks]
        == [(name, 'passed') for name in expected[:stop]] + [(ECHO_SOAK_CHECK, 'failed')]
        and checks[-1].get('reason') == 'tv_background_voice_blocked'
    ):
        return None
    evidence = report.get('echo_soak_evidence')
    if not (
        isinstance(evidence, dict)
        and evidence.get('background') == background
        and evidence.get('background_dispatch_guarded') is True
        and type(evidence.get('background_guard_rejections')) is int
        and evidence['background_guard_rejections'] > 0
        and counters_equal(evidence, ('admitted_voice_requests',), 0)
        and bounded_number(evidence.get('elapsed_seconds'), 0, ECHO_SOAK_CAPTURE_MS / 1000)
        and bounded_number(report.get('elapsed_seconds'), evidence['elapsed_seconds'], 86400)
        and evidence.get('background_observed_seconds') == evidence['elapsed_seconds']
    ):
        return None
    return {
        'elapsed_seconds': evidence['elapsed_seconds'],
        'background_guard_rejections': evidence['background_guard_rejections'],
        'admitted_voice_requests': 0,
    }


ENGLISH_WAKE_SCOPES = ('english_wake_awake', 'english_wake_pair')
ENGLISH_TURN_COUNTS = (
    'admitted_voice_requests',
    'completed_requests',
    'completed_tasks',
    'local_wake_detections',
    'english_wake_detections',
)
ENGLISH_TURN_STAGES = (
    'wake_detected',
    'asr_start',
    'asr_end',
    'brain_start',
    'playout_ack',
    'playback_done',
)


def counters_equal(row, keys, count):
    return isinstance(row, dict) and all(
        type(row.get(key)) is int and row[key] == count for key in keys
    )


def audit_english_wake(data, expect_backend, expect_apk_code, expect_capture_frontend=None):
    scope, backend = data['scope'], data.get('backend_version')
    plan = ENGLISH_WAKE_PHASES if scope == 'english_wake_pair' else ENGLISH_WAKE_PHASES[:1]
    phases = data.get('phases')
    result = {
        'scope': scope,
        'verified': False,
        'full_p0_acceptance': False,
        'completed_phases': 0,
        'expected_phases': len(plan),
        'phases': [],
    }
    if not (
        isinstance(backend, str)
        and re.fullmatch(VERSION, backend)
        and type(data.get('protocol_version')) is int
        and data['protocol_version'] > 0
        and isinstance(phases, list)
        and len(phases) == len(plan)
        and all(
            data.get(key) is False
            for key in ('raw_audio_saved', 'contains_conversation_content', 'full_p0_acceptance')
        )
    ):
        raise ValueError('Invalid English wake report')
    if expect_backend is not None and backend != expect_backend:
        return {**result, 'reason': 'backend_version_mismatch'}
    fixture = validate_prepared_speech(data.get('prepared_english_input'))
    if data.get('prepared_english_input_cleaned') is not True or (
        fixture['frames'],
        fixture['pcm_sha256'],
        fixture['wav_sha256'],
    ) != (FIXTURE_FRAMES, FIXTURE_PCM_SHA256, FIXTURE_WAV_SHA256):
        return {**result, 'reason': 'english_fixture_unconfirmed'}

    identity = None
    sources = None
    for phase, (name, options) in zip(phases, plan, strict=True):
        if not isinstance(phase, dict) or phase.get('name') != name:
            return {**result, 'reason': 'phase_plan_mismatch'}
        row = {'name': name}
        result['phases'].append(row)
        report = phase.get('report')
        if not matches_capture_frontend(report, expect_capture_frontend):
            return {**result, 'reason': 'capture_frontend_expectation_mismatch'}
        state = phase.get('state')
        identity = phase_apk_identity(report, expect_apk_code, identity)
        if not (
            identity
            and content_free_phase(report)
            and state in ('passed', 'failed')
            and report.get('state') == state
            and report.get('cleanup_microphone_off_verified') is True
            and report.get('english_fixture_played') is True
            and report.get('awake_dialogue_enabled') is (name == 'awake')
            and report.get('standby_enabled') is (name == 'standby')
            and report.get('synthetic_acoustic_probe_enabled') is True
        ):
            return {**result, 'reason': 'english_phase_provenance_unconfirmed'}
        current_sources = tuple(report[key] for key in SOURCE_FIELDS)
        if sources is not None and sources != current_sources:
            return {**result, 'reason': 'phase_source_mismatch'}
        sources = current_sources
        if name == 'standby':
            reason = standby_failure(
                report.get('standby_evidence'), report.get('elapsed_seconds'), require_quiet=True
            )
            if reason:
                return {**result, 'reason': reason}
        checks = report.get('checks')
        expected = planned_checks(options)
        if state == 'failed' and name == 'awake':
            expected = expected[: expected.index(AWAKE_DIALOGUE_CHECK) + 1]
        states = ['passed'] * len(expected)
        if state == 'failed':
            states[-1] = 'failed'
        if (
            not isinstance(checks, list)
            or not all(isinstance(check, dict) for check in checks)
            or [(check.get('name'), check.get('state')) for check in checks]
            != list(zip(expected, states, strict=True))
        ):
            return {**result, 'reason': 'english_phase_checks_unconfirmed'}

        ev = report.get('acoustic_evidence')
        if isinstance(ev, dict) and 'english_wake_input_shape' in ev:
            probe = summarize_wake_input(ev['english_wake_input_shape'])
            if probe['status'] == 'invalid' or 'language' in probe:
                return {**result, 'reason': 'english_probe_invalid'}
            row['input_shape'] = probe['status']
            if probe['status'] == 'measured':
                row['stream_shape'] = {
                    name: probe[name]['status'] for name in ('primary', 'alternate')
                }
            for field in ('live_decoder', 'fresh_replay'):
                if field in probe:
                    row[field] = probe[field]
        count = int(state == 'passed')
        if not (
            isinstance(ev, dict)
            and ev.get('wake_language') == 'en'
            and counters_equal(ev, ('new_voice_requests',), 1)
            and counters_equal(ev, ('rejected_voice_candidates',), 1 - count)
            and counters_equal(
                ev,
                (
                    'pending_voice_candidates',
                    'unclassified_terminal_candidates',
                    'unresumed_rejected_pauses',
                    'cancelled_requests',
                ),
                0,
            )
            and counters_equal(ev, ENGLISH_TURN_COUNTS, count)
            and counters_equal(ev.get('stage_counts'), ENGLISH_TURN_STAGES, count)
        ):
            return {**result, 'reason': 'english_turn_unconfirmed'}
        if count:
            if not (
                ev.get('recognized_stimulus_matches') is True
                and matched_playout(ev)
                and bounded_number(
                    ev.get('post_playback_quiet_seconds'), POST_DIALOGUE_SECONDS, 86400
                )
            ):
                return {**result, 'reason': 'english_playout_unconfirmed'}
        elif not (
            checks[-1].get('reason') == 'synthetic_acoustic_chain_not_confirmed'
            and counters_equal(ev.get('rejected_voice_gates'), ('wake_closed',), 1)
        ):
            return {**result, 'reason': 'english_wake_failure_unconfirmed'}

        row['outcome'] = 'passed' if count else 'wake_closed'
        result['completed_phases'] += count

    passed = result['completed_phases'] == len(plan)
    if data.get('state') != ('passed' if passed else 'failed'):
        return {**result, 'reason': 'english_report_outcome_mismatch'}
    return {**result, 'verified': True} if passed else {**result, 'reason': 'english_wake_miss'}


def audit(
    data,
    *,
    expect_backend=None,
    expect_apk_code=None,
    expect_capture_frontend=None,
    require_standby_quiet=False,
    require_qa_fixture_binding=False,
):
    if expect_capture_frontend not in (None, *FRONTENDS):
        raise ValueError('Invalid expected capture frontend')
    if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
        raise ValueError('Unknown report schema')
    scope = data.get('scope')
    if scope in ENGLISH_WAKE_SCOPES:
        if require_qa_fixture_binding:
            raise ValueError('QA fixture binding requires a QA latency suite')
        if require_standby_quiet and scope == 'english_wake_awake':
            raise ValueError('Standby quiet evidence requires a standby suite')
        return audit_english_wake(data, expect_backend, expect_apk_code, expect_capture_frontend)
    backend = data.get('backend_version')
    if (
        scope
        not in (
            'fixed_device_suite',
            STANDBY_SUITE_SCOPE,
            'synthetic_qa_latency',
            'synthetic_interruption_latency',
            ECHO_SOAK_SCOPE,
        )
        or not isinstance(backend, str)
        or not re.fullmatch(VERSION, backend)
        or type(data.get('protocol_version')) is not int
        or data['protocol_version'] <= 0
        or any(
            data.get(key) is not False
            for key in (
                'full_p0_acceptance',
                'physical_usb_unplug_tested',
                'human_accuracy_asserted',
            )
        )
    ):
        raise ValueError('Invalid report scope')
    qa_latency = scope == 'synthetic_qa_latency'
    qa_fixture_bound = 'qa_fixture_bound' in data
    if qa_fixture_bound and (not qa_latency or data['qa_fixture_bound'] is not True):
        raise ValueError('Invalid QA fixture binding')
    if require_qa_fixture_binding and not qa_latency:
        raise ValueError('QA fixture binding requires a QA latency suite')
    interruption = scope == 'synthetic_interruption_latency'
    echo_soak = scope == ECHO_SOAK_SCOPE
    standby_only = scope == STANDBY_SUITE_SCOPE
    require_standby_quiet = require_standby_quiet or standby_only
    has_soak_duration = 'self_playback_seconds_per_phase' in data
    soak_duration = data.get('self_playback_seconds_per_phase')
    legacy_echo_soak = echo_soak and not has_soak_duration
    background = data.get('background')
    extended = data.get('extended_voice_evidence', False)
    room_latency = data.get('response_acoustic_evidence', False)
    room_coverage = data.get('response_acoustic_coverage', False)
    render_overlap = data.get('require_render_overlap', False)
    scenario = data.get('latency_scenario', 'qa')
    tool_rejection_coverage = 'tool_rejection_coverage' in data
    if (
        type(extended) is not bool
        or type(room_latency) is not bool
        or type(room_coverage) is not bool
        or type(render_overlap) is not bool
        or (
            render_overlap
            and (
                not interruption
                or tuple(map(int, backend.split('.')))
                < tuple(map(int, RENDER_OVERLAP_MIN_BACKEND.split('.')))
            )
        )
        or (room_coverage and not room_latency)
        or ((qa_latency or interruption or echo_soak or standby_only) and extended)
        or (
            interruption
            and tuple(map(int, backend.split('.')))
            < tuple(map(int, EXTENDED_VOICE_MIN_BACKEND.split('.')))
        )
        or (
            echo_soak
            and tuple(map(int, backend.split('.')))
            < tuple(map(int, ECHO_SOAK_MIN_BACKEND.split('.')))
        )
        or (background is not None and (not echo_soak or not valid_background(background)))
        or (
            background is not None
            and background['kind'] == 'tv'
            and tuple(map(int, backend.split('.')))
            < tuple(map(int, TOOL_SAMPLE_MIN_BACKEND.split('.')))
        )
        or (
            has_soak_duration
            and (
                not echo_soak
                or type(soak_duration) is not int
                or soak_duration != ECHO_SOAK_SECONDS
            )
        )
        or (room_latency and not qa_latency)
        or not isinstance(scenario, str)
        or scenario not in SCENARIO_DIGESTS
        or (
            tool_rejection_coverage
            and not (
                data['tool_rejection_coverage'] is True and qa_fixture_bound and scenario == 'tool'
            )
        )
        or (
            scenario != 'qa'
            and (
                not qa_latency
                or tuple(map(int, backend.split('.')))
                < tuple(map(int, ROUTED_LATENCY_MIN_BACKEND.split('.')))
            )
        )
        or (
            qa_latency
            and (
                data.get('qa_corpus_sha256') != SCENARIO_DIGESTS[scenario]
                or data.get('audible_onset_measured') is not False
            )
        )
    ):
        raise ValueError('Invalid suite mode')
    plan = device_suite_phases(
        qa_latency=qa_latency,
        extended_voice=extended,
        interruption=interruption,
        echo_soak=echo_soak,
        legacy_echo_soak=legacy_echo_soak,
        standby_only=standby_only,
    )
    if require_standby_quiet and not any('standby' in options for _, options in plan):
        raise ValueError('Standby quiet evidence requires a standby suite')
    phases = data.get('phases')
    if not isinstance(phases, list) or len(phases) != len(plan):
        raise ValueError('Invalid suite length')
    result = {
        'scope': scope,
        'verified': False,
        'full_p0_acceptance': False,
        'completed_phases': 0,
        'expected_phases': len(plan),
    }
    tool_guard_required = scenario == 'tool' and tuple(map(int, backend.split('.'))) >= tuple(
        map(int, TOOL_SAMPLE_MIN_BACKEND.split('.'))
    )
    if tool_rejection_coverage:
        if tuple(map(int, backend.split('.'))) < tuple(
            map(int, TOOL_REJECTION_MIN_BACKEND.split('.'))
        ):
            raise ValueError('Tool rejection coverage requires bounded input evidence')
        result['tool_transcript_misses'] = 0
    if scenario == 'tool':
        result['tool_sample_guard_required'] = tool_guard_required
    if qa_latency:
        result['qa_fixture_binding_required'] = qa_fixture_bound
    if render_overlap:
        result['require_render_overlap'] = True
    if require_standby_quiet:
        result['require_standby_quiet'] = True
    if echo_soak:
        result['self_playback_seconds_per_phase'] = (
            LEGACY_ECHO_SOAK_SECONDS if legacy_echo_soak else ECHO_SOAK_SECONDS
        )
    if expect_backend is not None and backend != expect_backend:
        return {**result, 'reason': 'backend_version_mismatch'}
    if require_qa_fixture_binding and not qa_fixture_bound:
        return {**result, 'reason': 'suite_qa_fixture_binding_missing'}

    fixed_chinese = 'prepared_chinese_input' in data or 'prepared_chinese_input_cleaned' in data
    modern_standby = standby_only and tuple(map(int, backend.split('.'))) >= (0, 15, 60)
    result['same_chinese_stimulus_verified'] = False
    chinese_metadata = None
    if fixed_chinese or modern_standby:
        try:
            chinese_metadata = validate_prepared_speech(data.get('prepared_chinese_input'))
        except ValueError:
            return {**result, 'reason': 'chinese_stimulus_identity_unconfirmed'}
        if not standby_only or data.get('prepared_chinese_input_cleaned') is not True:
            return {**result, 'reason': 'chinese_stimulus_cleanup_unconfirmed'}

    reports = []
    artifact = None
    failure = None
    for case, (phase, (name, options)) in enumerate(zip(phases, plan), start=1):
        if (
            not isinstance(phase, dict)
            or phase.get('name') != name
            or phase.get('expected_checks') != planned_checks(set(options))
        ):
            failure = {'phase': name, 'reason': 'phase_plan_mismatch'}
            break
        report = phase.get('report')
        if (
            chinese_metadata is not None
            and phase.get('state') == 'passed'
            and (
                not isinstance(report, dict)
                or report.get('prepared_chinese_input') != chinese_metadata
                or report.get('chinese_fixture_played') is not True
                or not isinstance(report.get('acoustic_evidence'), dict)
                or not valid_counter_delta(report['acoustic_evidence'].get('chinese_wake_counters'))
            )
        ):
            failure = {'phase': name, 'reason': 'phase_chinese_stimulus_unconfirmed'}
            break
        if not matches_capture_frontend(report, expect_capture_frontend):
            failure = {'phase': name, 'reason': 'capture_frontend_expectation_mismatch'}
            break
        if isinstance(report, dict) and (
            (qa_fixture_bound and not qa_fixture_matches(report, scenario, case))
            or (not qa_fixture_bound and 'qa_fixture' in report)
        ):
            failure = {'phase': name, 'reason': 'suite_qa_fixture_mismatch'}
            break
        identity = phase_apk_identity(report, expect_apk_code, artifact)
        if phase.get('state') == 'guard_rejected':
            if not (
                tool_rejection_coverage
                and identity is not None
                and content_free_phase(report)
                and safe_tool_transcript_miss(report, phase['expected_checks'])
            ):
                failure = {'phase': name, 'reason': 'phase_tool_rejection_unconfirmed'}
                break
            artifact = identity
            reports.append(report)
            continue
        if phase.get('state') != 'passed' or not isinstance(report, dict):
            failure = {'phase': name, 'reason': 'phase_not_completed'}
            if (
                data.get('state') == 'failed'
                and phase.get('state') == 'failed'
                and 'report' not in phase
                and phase.get('reason') == 'suite_source_changed'
            ):
                # The batch rejected this phase before collecting evidence.
                failure['recorded_failure'] = 'suite_source_changed'
            elif (
                background is not None
                and background['kind'] == 'tv'
                and identity is not None
                and data.get('state') == phase.get('state') == 'failed'
                and (
                    not reports
                    or all(report.get(key) == reports[-1].get(key) for key in SOURCE_FIELDS)
                )
            ):
                observed = recorded_tv_failure(report, phase['expected_checks'], background)
                if observed is not None:
                    failure.update(
                        recorded_failure='tv_background_voice_blocked',
                        partial_background_observation=observed,
                    )
            elif (
                tool_guard_required
                and identity is not None
                and data.get('state') == phase.get('state') == 'failed'
            ):
                recorded = recorded_tool_failure(
                    report, phase['expected_checks'], reports[-1] if reports else None
                )
                if recorded is not None:
                    failure['recorded_failure'] = recorded
            break
        if identity is None or (interruption and identity[1] < STOP_VISUAL_MIN_APK_CODE):
            failure = {'phase': name, 'reason': 'apk_identity_mismatch'}
            break
        artifact = identity
        if not content_free_phase(report):
            failure = {'phase': name, 'reason': 'phase_provenance_incomplete'}
            break
        if 'standby' in options:
            reason = standby_failure(
                report.get('standby_evidence'),
                report.get('elapsed_seconds'),
                require_quiet=require_standby_quiet,
            )
            if reason:
                failure = {'phase': name, 'reason': reason}
                break
        prepared = report.get('prepared_interruption_input')
        # Known-speech corroboration is currently an explicit individual run;
        # no fixed batch may silently claim its additional evidence scope.
        if (
            report.get('known_speech_evidence_required', False) is not False
            or report.get('known_speech_room_model_enabled', False) is not False
            or report.get('known_speech_evidence') is not None
        ):
            failure = {'phase': name, 'reason': 'unexpected_known_speech_scope'}
            break
        if prepared is not None:
            try:
                if not {'synthetic_barge', 'synthetic_stop'} & set(options):
                    raise ValueError('Prepared input outside interruption scope')
                interruption_input(prepared, stop='synthetic_stop' in options)
                if report.get('prepared_interruption_input_cleaned') is not True:
                    raise ValueError('Prepared input cleanup unconfirmed')
            except (ValueError, CheckFailure):
                failure = {'phase': name, 'reason': 'prepared_input_evidence_invalid'}
                break
        if {'awake_dialogue', 'acoustic'} & set(options):
            evidence = report.get('acoustic_evidence')
            quiet = (
                evidence.get('post_playback_quiet_seconds') if isinstance(evidence, dict) else None
            )
            if not bounded_number(quiet, POST_DIALOGUE_SECONDS, 86400) or not matched_playout(
                evidence
            ):
                failure = {'phase': name, 'reason': 'dialogue_timing_unconfirmed'}
                break
            if tool_guard_required:
                gates = evidence.get('rejected_voice_gates')
                rejected = gates.get('tool_sample_guard') if isinstance(gates, dict) else None
                admitted = evidence.get('admitted_voice_requests')
                if not (
                    evidence.get('tool_sample_guarded') is True
                    and type(admitted) is int
                    and admitted == 1
                    and type(rejected) is int
                    and rejected == 0
                ):
                    failure = {'phase': name, 'reason': 'phase_tool_sample_guard_unconfirmed'}
                    break
        reports.append(report)

    if failure is not None and not reports:
        return {**result, **failure}
    rows = iter(reports)

    def replay(_options, _checkpoint, **_kwargs):
        row = next(rows, None)
        # Stop the in-memory replay at the first phase without usable evidence.
        source = reports[0] if row is None else row
        return SimpleNamespace(
            source_sha256=source['automation_source_sha256'],
            speech_source_sha256=source['speech_probe_source_sha256'],
            run=lambda: {'state': 'failed'} if row is None else row,
        )

    reviewed = run_device_suite(
        replay,
        lambda _value: None,
        qa_latency=qa_latency,
        qa_fixture_bound=qa_fixture_bound,
        tool_rejection_coverage=tool_rejection_coverage,
        response_acoustic=room_latency,
        response_acoustic_coverage=room_coverage,
        require_render_overlap=render_overlap,
        extended_voice=extended,
        latency_scenario=scenario,
        interruption=interruption,
        echo_soak=echo_soak,
        background=background,
        legacy_echo_soak=legacy_echo_soak,
        standby_only=standby_only,
    )
    result['completed_phases'] = sum(p['state'] == 'passed' for p in reviewed['phases'])
    if tool_rejection_coverage:
        result['tool_transcript_misses'] = reviewed['tool_transcript_misses']
    if room_coverage:
        result['response_acoustic_unconfirmed'] = reviewed['response_acoustic_unconfirmed']
    if (qa_latency or interruption) and result['completed_phases']:
        result['latencies_ms'] = reviewed['latencies_ms']
        if 'math_answers' in reviewed:
            result['math_answers'] = reviewed['math_answers']
        if scenario != 'qa':
            result['latency_scenario'] = scenario
    if reviewed['state'] == 'tool_transcript_incomplete':
        return {
            **result,
            'reason': 'suite_tool_transcript_coverage_incomplete'
            if data.get('state') == 'tool_transcript_incomplete'
            and type(data.get('tool_transcript_misses')) is int
            and data['tool_transcript_misses'] == reviewed['tool_transcript_misses']
            else 'report_not_completed',
        }
    if reviewed['state'] == 'acoustic_incomplete':
        return {
            **result,
            'reason': 'suite_qa_acoustic_coverage_incomplete'
            if data.get('state') == 'acoustic_incomplete'
            else 'report_not_completed',
        }
    if reviewed['state'] != 'passed':
        if failure is not None and (
            result['completed_phases'] + result.get('tool_transcript_misses', 0) == len(reports)
        ):
            return {**result, **failure}
        failed = next(
            p for p in reviewed['phases'] if p['state'] not in ('passed', 'guard_rejected')
        )
        return {
            **result,
            'phase': failed['name'],
            'reason': failed.get('reason', 'phase_not_completed'),
        }
    if data.get('state') != 'passed':
        return {**result, 'reason': 'report_not_completed'}
    if echo_soak:
        result['echo_soak_totals'] = reviewed['echo_soak_totals']
        if background is not None:
            result['background'] = background
            if background['kind'] == 'tv':
                result['background_dispatch_guarded'] = True
    result['same_chinese_stimulus_verified'] = chinese_metadata is not None
    return {**result, 'verified': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--expect-backend')
    parser.add_argument('--expect-apk-code', type=int)
    parser.add_argument('--expect-capture-frontend', choices=FRONTENDS)
    parser.add_argument(
        '--require-qa-fixture-binding',
        action='store_true',
        help='Require each fixed QA latency phase to name its corpus scenario and case number',
    )
    parser.add_argument(
        '--require-standby-quiet',
        action='store_true',
        help='Require zero wake, ASR and render counter deltas in every standby phase',
    )
    args = parser.parse_args(argv)
    try:
        result = audit(
            read_private_json(args.report, 4 * 1024**2),
            expect_backend=args.expect_backend,
            expect_apk_code=args.expect_apk_code,
            expect_capture_frontend=args.expect_capture_frontend,
            require_standby_quiet=args.require_standby_quiet,
            require_qa_fixture_binding=args.require_qa_fixture_binding,
        )
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(2, '无法复核所选私有设备批次报告。\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['verified'] else 2
