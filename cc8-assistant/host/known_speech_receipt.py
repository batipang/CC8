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
NEAR_TRANSFER_STATUSES = {
    'coverage_incomplete',
    'training_below_floor',
    'alignment_unconfirmed',
    'fit_unavailable',
    'held_out_unconfirmed',
    'validated',
}
NEAR_BLOCK_STATUSES = {'matched', 'capture_below_floor', 'gain_out_of_range', 'shape_mismatch'}
NEAR_MAX_DELAY = 17600  # 1.1 s at 16 kHz
NEAR_FILTER_HALF = 96
NEAR_HOLDOUT_START = 36000  # 2.25 s at 16 kHz
NEAR_HOLDOUT_BLOCK = 3200
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
    'unexpected_render',
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
    fields = {'operation', 'directory', 'metadata'}
    if value.get('operation') != 'known_speech_arm' or set(value) not in (
        fields | {'generation'},
        fields | {'near_only'},
    ):
        return False
    if 'near_only' in value:
        if value['near_only'] is not True:
            return False
    elif type(value['generation']) is not int or not 0 < value['generation'] < 2**31:
        return False
    if (
        not isinstance(value['directory'], str)
        or re.fullmatch(r'cc8-public-speech-[a-z0-9_-]{6,64}', value['directory']) is None
    ):
        return False
    try:
        validate_prepared_speech(value['metadata'])
    except ValueError:
        return False
    return True


def validate(value, *, near_only=False):
    if not isinstance(value, dict) or not isinstance(value.get('status'), str):
        raise ValueError('Invalid known speech receipt')
    status = value['status']
    fields = (
        BASE
        | ({'near_only'} if near_only else set())
        | (
            {'near_transfer'}
            if near_only
            and 'near_transfer' in value
            and status in ('observed', 'ambiguous', 'not_observed', 'capture_incomplete')
            else set()
        )
        | (
            MATCH
            if status in ('observed', 'ambiguous', 'not_observed', 'reference_not_distinct')
            else set()
        )
        | (OBSERVED if status == 'observed' else set())
        | (
            {'far_transfer'}
            if not near_only
            and 'far_transfer' in value
            and status in ('observed', 'ambiguous', 'not_observed', 'reference_not_distinct')
            else set()
        )
    )
    if status not in STATUSES or set(value) != fields:
        raise ValueError('Invalid known speech receipt fields')
    if near_only:
        if (
            value['near_only'] is not True
            or value['reference_frames'] != 0
            or value.get('render_similarity', 0) != 0
            or value.get('matched_window_digital_render_ms', 0) != 0
            or status in ('reference_not_distinct', 'render_discontinuous', 'render_limit_exceeded')
        ):
            raise ValueError('Invalid near-only speech receipt')
    elif status == 'unexpected_render':
        raise ValueError('Unexpected render is specific to near-only observation')
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
            or (not near_only and value['reference_frames'] < 3520)
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
    if 'near_transfer' in value:
        return {**value, 'near_transfer': validate_near_transfer(value['near_transfer'], value)}
    return dict(value)


def validate_near_transfer(transfer, receipt):
    if not isinstance(transfer, dict) or not isinstance(transfer.get('status'), str):
        raise ValueError('Invalid near transfer receipt')
    status = transfer['status']
    fields = set(transfer)
    scores = {'envelope_confidence', 'envelope_rival'}
    fit = {'delay_frames'}
    holdout = {'held_out_blocks'}
    before_fit = {'status'} | scores
    after_fit = before_fit | fit
    allowed = {
        'coverage_incomplete': ({'status'}, after_fit),
        'training_below_floor': ({'status'}, after_fit),
        'alignment_unconfirmed': (before_fit,),
        'fit_unavailable': (after_fit,),
        'held_out_unconfirmed': (after_fit | holdout,),
        'validated': (after_fit | holdout,),
    }
    if status not in NEAR_TRANSFER_STATUSES or fields not in allowed[status]:
        raise ValueError('Invalid near transfer fields')
    for key in scores & fields:
        item = transfer[key]
        if type(item) not in (int, float) or not 0 <= item <= 1:
            raise ValueError('Invalid near transfer score')
    if fit <= fields:
        delay = transfer['delay_frames']
        if (
            type(delay) is not int
            or not -NEAR_FILTER_HALF <= delay <= NEAR_MAX_DELAY
            or delay % 2
            or transfer['envelope_confidence'] < 0.60
            or transfer['envelope_confidence'] - transfer['envelope_rival'] < 0.08 - 0.0001
        ):
            raise ValueError('Invalid near transfer alignment')
    if holdout <= fields:
        blocks = transfer['held_out_blocks']
        if (
            not isinstance(blocks, list)
            or len(blocks) != 2
            or any(
                not isinstance(block, str) or block not in NEAR_BLOCK_STATUSES for block in blocks
            )
            or (status == 'validated') != (blocks == ['matched', 'matched'])
            or receipt['captured_frames'] < NEAR_HOLDOUT_START + 2 * NEAR_HOLDOUT_BLOCK + delay
        ):
            raise ValueError('Invalid near transfer holdout')
        return {**transfer, 'held_out_blocks': list(blocks)}
    return dict(transfer)


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


# Allow 200 ms to the match start, then 600 ms for training, gap and holdout.
# Two extra frames cover rounding to the even decimation grid.
MIN_COMMON_FRAMES = 12802


def estimated_overlap(mic, render, delta):
    return max(0, min(render, delta + mic) - max(0, delta))


def validate_far_preflight(value):
    if not isinstance(value, dict) or value.get('status') not in ('checked', 'awaiting_overlap'):
        raise ValueError('Invalid far preflight receipt')
    pending = value['status'] == 'awaiting_overlap'
    fields = {'status', 'boundary', 'coverage'}
    if not pending:
        fields.add('transfer')
    if set(value) != fields:
        raise ValueError('Invalid far preflight fields')
    boundary = value['boundary']
    if not isinstance(boundary, dict) or boundary.get('status') != (
        'pending' if pending else 'recorded'
    ):
        raise ValueError('Invalid far preflight boundary')
    validate_boundary({**boundary, 'status': 'recorded'})
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
        or overlap != estimated_overlap(mic, render, delta)
    ):
        raise ValueError('Invalid far preflight coverage values')
    result = {'status': value['status'], 'boundary': dict(boundary), 'coverage': dict(coverage)}
    if pending:
        if rms is not None or overlap >= MIN_COMMON_FRAMES:
            raise ValueError('Invalid pending overlap')
        return result
    if type(rms) not in (int, float) or not 0 <= rms <= 1 or (overlap == 0 and rms != 0):
        raise ValueError('Invalid far reference RMS')
    transfer = validate_transfer(value['transfer'], mic, render, preflight=True)
    if any(transfer[k] != boundary[k] for k in boundary if k != 'status'):
        raise ValueError('Invalid far preflight cutoff')
    return {**result, 'transfer': transfer}
