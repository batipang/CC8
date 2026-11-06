"""Recheck a saved fixed device suite or synthetic latency batch without device access."""

import argparse
import json
from pathlib import Path
import re
from types import SimpleNamespace

from device_smoke import (
    ECHO_SOAK_MIN_BACKEND,
    CheckFailure,
    ECHO_SOAK_SCOPE,
    ECHO_SOAK_SECONDS,
    LEGACY_ECHO_SOAK_SECONDS,
    POST_DIALOGUE_SECONDS,
    ROUTED_LATENCY_MIN_BACKEND,
    RENDER_OVERLAP_MIN_BACKEND,
    STANDBY_SUITE_SCOPE,
    TOOL_SAMPLE_MIN_BACKEND,
    device_suite_phases,
    matched_playout,
    interruption_input,
    STOP_VISUAL_MIN_APK_CODE,
    EXTENDED_VOICE_MIN_BACKEND,
    planned_checks,
    run_device_suite,
    standby_failure,
)
from private_output import read_private_json
from qa_speech import SCENARIO_DIGESTS
from soak_background import valid_background

VERSION = r'[0-9]{1,8}(?:\.[0-9]{1,8}){2}'
SOURCE_DIGEST = r'[a-f0-9]{64}'


def bounded_number(value, low, high):
    return type(value) in (int, float) and low <= value <= high


def audit(data, *, expect_backend=None, expect_apk_code=None, require_standby_quiet=False):
    if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
        raise ValueError('Unknown report schema')
    scope = data.get('scope')
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
    if scenario == 'tool':
        result['tool_sample_guard_required'] = tool_guard_required
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

    reports = []
    artifact = None
    failure = None
    for phase, (name, options) in zip(phases, plan):
        if (
            not isinstance(phase, dict)
            or phase.get('name') != name
            or phase.get('expected_checks') != planned_checks(set(options))
        ):
            failure = {'phase': name, 'reason': 'phase_plan_mismatch'}
            break
        report = phase.get('report')
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
            break
        version, code = report.get('expected_apk_version'), report.get('expected_apk_code')
        if (
            not isinstance(version, str)
            or not re.fullmatch(VERSION, version)
            or type(code) is not int
            or not 0 < code < 2**31
            or (interruption and code < STOP_VISUAL_MIN_APK_CODE)
            or (expect_apk_code is not None and code != expect_apk_code)
            or (artifact is not None and artifact != (version, code))
        ):
            failure = {'phase': name, 'reason': 'apk_identity_mismatch'}
            break
        artifact = (version, code)
        if (
            type(report.get('schema')) is not int
            or report['schema'] != 1
            or report.get('capture_tests_enabled') is not True
            or report.get('raw_audio_saved') is not False
            or report.get('contains_conversation_content') is not False
            or not all(
                isinstance(report.get(key), str) and re.fullmatch(SOURCE_DIGEST, report[key])
                for key in ('automation_source_sha256', 'speech_probe_source_sha256')
            )
        ):
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
    if room_coverage:
        result['response_acoustic_unconfirmed'] = reviewed['response_acoustic_unconfirmed']
    if (qa_latency or interruption) and result['completed_phases']:
        result['latencies_ms'] = reviewed['latencies_ms']
        if 'math_answers' in reviewed:
            result['math_answers'] = reviewed['math_answers']
        if scenario != 'qa':
            result['latency_scenario'] = scenario
    if reviewed['state'] == 'acoustic_incomplete':
        return {
            **result,
            'reason': 'suite_qa_acoustic_coverage_incomplete'
            if data.get('state') == 'acoustic_incomplete'
            else 'report_not_completed',
        }
    if reviewed['state'] != 'passed':
        if failure is not None and result['completed_phases'] == len(reports):
            return {**result, **failure}
        failed = next(p for p in reviewed['phases'] if p['state'] != 'passed')
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
    return {**result, 'verified': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--expect-backend')
    parser.add_argument('--expect-apk-code', type=int)
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
            require_standby_quiet=args.require_standby_quiet,
        )
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(2, '无法复核所选私有设备批次报告。\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['verified'] else 2
