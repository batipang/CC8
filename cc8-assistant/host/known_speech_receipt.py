"""Content-free known-speech window receipts; safe for the stdlib QA client."""

import math
import re
from mac_voice import validate_prepared_speech

METHOD = 'public_fixture_capture_window_ncc_v1'
BASE = {
    'schema',
    'method',
    'status',
    'captured_frames',
    'reference_frames',
    'template_offset_frames',
    'template_frames',
    'raw_audio_saved',
    'first_audible_onset_measured',
    'physical_double_talk_confirmed',
    'input_pcm_sha256',
}
MATCH = {'confidence', 'rival_confidence', 'render_similarity'}
OBSERVED = {'match_start_offset_frames', 'matched_window_digital_render_ms'}
STATUSES = {
    'observed',
    'ambiguous',
    'not_observed',
    'reference_not_distinct',
    'capture_incomplete',
    'analysing',
    'expired',
    'cancelled',
    'timer_unavailable',
    'invalid_audio',
    'mic_discontinuous',
    'render_discontinuous',
    'mic_limit_exceeded',
    'render_limit_exceeded',
    'capture_gap',
    'transport_changed',
    'session_changed',
    'capture_closed',
}


def valid_request(value):
    if not isinstance(value, dict):
        return False
    if value.get('operation') in ('known_speech_result', 'known_speech_stimulus'):
        return (
            (
                set(value) == {'operation', 'token'}
                or (
                    value['operation'] == 'known_speech_stimulus'
                    and set(value) == {'operation', 'token', 'verify_far'}
                    and value['verify_far'] is True
                )
            )
            and isinstance(value['token'], str)
            and re.fullmatch('[a-f0-9]{32}', value['token']) is not None
        )
    if value.get('operation') != 'known_speech_arm' or set(value) != {
        'operation',
        'directory',
        'metadata',
        'generation',
    }:
        return False
    if (
        not isinstance(value['directory'], str)
        or re.fullmatch(r'cc8-public-speech-[a-z0-9_-]{6,64}', value['directory']) is None
        or type(value['generation']) is not int
        or not 0 < value['generation'] < 2**31
    ):
        return False
    try:
        validate_prepared_speech(value['metadata'])
    except ValueError:
        return False
    return True


def validate(value):
    if not isinstance(value, dict) or not isinstance(value.get('status'), str):
        raise ValueError('Invalid known speech receipt')
    status = value['status']
    fields = (
        BASE
        | (
            MATCH
            if status in ('observed', 'ambiguous', 'not_observed', 'reference_not_distinct')
            else set()
        )
        | (OBSERVED if status == 'observed' else set())
        | (
            {'far_transfer'}
            if 'far_transfer' in value
            and status in ('observed', 'ambiguous', 'not_observed', 'reference_not_distinct')
            else set()
        )
    )
    if status not in STATUSES or set(value) != fields:
        raise ValueError('Invalid known speech receipt fields')
    if type(value['schema']) is not int or value['schema'] != 1 or value['method'] != METHOD:
        raise ValueError('Invalid known speech receipt method')
    if not isinstance(value['input_pcm_sha256'], str) or not re.fullmatch(
        '[a-f0-9]{64}', value['input_pcm_sha256']
    ):
        raise ValueError('Invalid known speech input digest')
    for key, maximum in (
        ('captured_frames', 96000),
        ('reference_frames', 96000),
        ('template_offset_frames', 156800),
        ('template_frames', 3200),
    ):
        if type(value[key]) is not int or not 0 <= value[key] <= maximum:
            raise ValueError('Invalid known speech receipt bounds')
    if value['template_frames'] != 3200 or value['template_offset_frames'] % 320:
        raise ValueError('Invalid known speech template')
    if any(
        value[k] is not False
        for k in (
            'raw_audio_saved',
            'first_audible_onset_measured',
            'physical_double_talk_confirmed',
        )
    ):
        raise ValueError('Invalid known speech physical claim')
    for key in MATCH & fields:
        number = value[key]
        if type(number) not in (int, float) or not 0 <= number <= 1:
            raise ValueError('Invalid known speech score')
    if status == 'observed':
        start, overlap = (
            value['match_start_offset_frames'],
            value['matched_window_digital_render_ms'],
        )
        if (
            type(start) is not int
            or start % 2 != 0
            or value['captured_frames'] < 3520
            or not 0 <= start <= value['captured_frames'] - 3200
            or value['reference_frames'] < 3520
            or type(overlap) not in (int, float)
            or not math.isfinite(overlap)
            or not 0 <= overlap <= 200
            or value['confidence'] < 0.55
            or value['confidence'] - value['rival_confidence'] < 0.12 - 0.0001
            or value['render_similarity'] >= 0.45
        ):
            raise ValueError('Invalid known speech observed evidence')
    if 'far_transfer' in value:
        return {
            **value,
            'far_transfer': validate_transfer(
                value['far_transfer'], value['captured_frames'], value['reference_frames']
            ),
        }
    return dict(value)


def validate_boundary(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'status', 'pre_stimulus_mic_frames', 'pre_stimulus_render_frames'}
        or value['status'] != 'recorded'
    ):
        raise ValueError('Invalid known speech boundary')
    for key in ('pre_stimulus_mic_frames', 'pre_stimulus_render_frames'):
        if type(value[key]) is not int or not 12000 <= value[key] <= 96000:
            raise ValueError('Invalid known speech boundary frames')
    return dict(value)


def validate_transfer(value, mic_frames, render_frames, *, preflight=False):
    """Strict content-free diagnostics; never an alternative raw-match gate."""
    base = {
        'schema',
        'status',
        'pre_stimulus_mic_frames',
        'pre_stimulus_render_frames',
        'training_frames',
        'held_out_frames',
    }
    alignment = {'alignment_confidence', 'alignment_rival'}
    held = {'held_out_confidence', 'held_out_gain_valid', 'held_out_polarity_valid'}
    residual = {
        'residual_confidence',
        'residual_rival',
        'raw_at_residual_confidence',
        'predicted_far_rms',
        'residual_rms',
        'residual_window_offset_frames',
    }
    statuses = {
        'pre_stimulus_incomplete',
        'reference_below_floor',
        'alignment_unconfirmed',
        'fit_unavailable',
        'held_out_unconfirmed',
        'mixed_window_incomplete',
        'residual_observed',
        'residual_unconfirmed',
    }
    if preflight:
        statuses -= {'mixed_window_incomplete', 'residual_observed', 'residual_unconfirmed'}
        statuses.add('validated')
    if (
        not isinstance(value, dict)
        or not isinstance(value.get('status'), str)
        or value['status'] not in statuses
    ):
        raise ValueError('Invalid far transfer status')
    status = value['status']
    # Early exits can occur before or after alignment, but never admit arbitrary
    # keys or partial score groups. Later stages require all preceding evidence.
    fields = base | (alignment if alignment <= value.keys() else set())
    if status in (
        'alignment_unconfirmed',
        'fit_unavailable',
        'held_out_unconfirmed',
        'mixed_window_incomplete',
        'validated',
        'residual_observed',
        'residual_unconfirmed',
    ):
        fields |= alignment
    if status in (
        'held_out_unconfirmed',
        'mixed_window_incomplete',
        'validated',
        'residual_observed',
        'residual_unconfirmed',
    ):
        fields |= held
    if status in ('residual_observed', 'residual_unconfirmed'):
        fields |= residual
    if set(value) != fields or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Invalid far transfer fields')
    validate_boundary(
        {
            'status': 'recorded',
            'pre_stimulus_mic_frames': value['pre_stimulus_mic_frames'],
            'pre_stimulus_render_frames': value['pre_stimulus_render_frames'],
        }
    )
    if (
        value['pre_stimulus_mic_frames'] > mic_frames
        or value['pre_stimulus_render_frames'] > render_frames
    ):
        raise ValueError('Invalid far transfer cutoff')
    if any(
        type(value[k]) is not int or value[k] != 3200
        for k in ('training_frames', 'held_out_frames')
    ):
        raise ValueError('Invalid far transfer sample allocation')
    for key in (
        alignment | {'held_out_confidence'} | residual - {'residual_window_offset_frames'}
    ) & fields:
        number = value[key]
        if type(number) not in (float, int) or not 0 <= number <= 1:
            raise ValueError('Invalid far transfer score')
    if held <= fields and any(
        type(value[k]) is not bool for k in ('held_out_gain_valid', 'held_out_polarity_valid')
    ):
        raise ValueError('Invalid held-out gain')
    if residual <= fields:
        offset = value['residual_window_offset_frames']
        if (
            type(offset) is not int
            or offset % 2
            or not value['pre_stimulus_mic_frames'] <= offset <= mic_frames - 3200
        ):
            raise ValueError('Invalid residual window')
    if status in (
        'validated',
        'mixed_window_incomplete',
        'residual_observed',
        'residual_unconfirmed',
    ) and (
        value['alignment_confidence'] < 0.55
        or value['alignment_confidence'] - value['alignment_rival'] < 0.12 - 0.0001
        or value['held_out_confidence'] < 0.8
        or value['held_out_gain_valid'] is not True
        or value['held_out_polarity_valid'] is not True
    ):
        raise ValueError('Invalid validated transfer')
    if status == 'residual_observed' and (
        value['residual_confidence'] < 0.55
        or value['residual_confidence'] - value['residual_rival'] < 0.12 - 0.0001
        or value['residual_rms'] < 0.002
    ):
        raise ValueError('Invalid residual match')
    return dict(value)


def validate_far_preflight(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'status', 'boundary', 'coverage', 'transfer'}
        or value['status'] != 'checked'
    ):
        raise ValueError('Invalid far preflight receipt')
    boundary = validate_boundary(value['boundary'])
    mic = boundary['pre_stimulus_mic_frames']
    render = boundary['pre_stimulus_render_frames']
    coverage = value['coverage']
    if not isinstance(coverage, dict) or set(coverage) != {
        'estimated_mic_start_relative_to_render_frames',
        'estimated_overlap_frames',
        'overlap_reference_rms',
    }:
        raise ValueError('Invalid far preflight coverage')
    delta = coverage['estimated_mic_start_relative_to_render_frames']
    overlap = coverage['estimated_overlap_frames']
    rms = coverage['overlap_reference_rms']
    if (
        type(delta) is not int
        or abs(delta) > 2**63
        or type(overlap) is not int
        or overlap != max(0, min(render, delta + mic) - max(0, delta))
        or type(rms) not in (int, float)
        or not 0 <= rms <= 1
        or (overlap == 0 and rms != 0)
    ):
        raise ValueError('Invalid far preflight coverage values')
    transfer = validate_transfer(value['transfer'], mic, render, preflight=True)
    if any(transfer[k] != boundary[k] for k in boundary if k != 'status'):
        raise ValueError('Invalid far preflight cutoff')
    return {
        'status': 'checked',
        'boundary': boundary,
        'coverage': dict(coverage),
        'transfer': transfer,
    }
