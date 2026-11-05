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
    if value.get('operation') == 'known_speech_result':
        return (
            set(value) == {'operation', 'token'}
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
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1:
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
    return dict(value)
