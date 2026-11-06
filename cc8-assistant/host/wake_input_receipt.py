"""Owner IPC contract for the fixed public English wake fixture."""

from math import isfinite
import re

from mac_voice import validate_prepared_speech

FIXTURE_FRAMES = 39419
FIXTURE_PCM_SHA256 = '56089a76c91d384a60fe8a011f70df7eebd6464603930671a6525f97f0ec71fe'
FIXTURE_WAV_SHA256 = '9510d2d219f1bb277578db5add07052691d92cf9a0ab8798e7490fa28e5bcb4c'
MIN_MATCH_FRAMES = FIXTURE_FRAMES + 1280
MAX_FRAMES = 64000


def valid_request(value):
    if not isinstance(value, dict):
        return False
    operation = value.get('operation')
    if operation == 'wake_input_result':
        return (
            set(value) == {'operation', 'token'}
            and isinstance(value['token'], str)
            and re.fullmatch('[a-f0-9]{32}', value['token']) is not None
        )
    if (
        operation != 'wake_input_arm'
        or set(value) != {'operation', 'directory', 'metadata'}
        or not isinstance(value['directory'], str)
        or re.fullmatch(r'cc8-public-speech-[a-z0-9_-]{6,64}', value['directory']) is None
    ):
        return False
    try:
        meta = validate_prepared_speech(value['metadata'])
    except ValueError:
        return False
    return (
        meta['frames'] == FIXTURE_FRAMES
        and meta['pcm_sha256'] == FIXTURE_PCM_SHA256
        and meta['wav_sha256'] == FIXTURE_WAV_SHA256
    )


def _unit(value):
    return type(value) in (int, float) and 0 <= value <= 1 and isfinite(value)


def _valid_stream(value):
    if not isinstance(value, dict):
        return False
    status = value.get('status')
    if status in ('capture_incomplete', 'input_gap'):
        return set(value) == {'status'}
    question = {'status', 'question_ncc', 'question_rival_ncc'}
    if not all(_unit(value.get(key)) for key in question - {'status'}):
        return False
    if status in ('question_unconfirmed', 'fixture_boundary_unconfirmed'):
        return set(value) == question
    halves = value.get('question_half_ncc')
    relative = value.get('name_relative_frames')
    return (
        status in ('matched', 'name_or_question_shape_unconfirmed')
        and set(value)
        == question
        | {'question_half_ncc', 'name_ncc', 'name_rms', 'question_rms', 'name_relative_frames'}
        and isinstance(halves, list)
        and len(halves) == 2
        and all(_unit(part) for part in halves)
        and _unit(value['name_ncc'])
        and _unit(value['name_rms'])
        and _unit(value['question_rms'])
        and type(relative) is int
        and -240 <= relative <= 240
    )


def _valid_replay(value, matched):
    if not matched:
        return value == {'status': 'not_run'}
    return value == {'status': 'unavailable'} or (
        isinstance(value, dict)
        and set(value) == {'status', 'primary_hit', 'alternate_hit'}
        and value['status'] == 'measured'
        and type(value['primary_hit']) is bool
        and type(value['alternate_hit']) is bool
    )


def summarize(value):
    if value == {'status': 'unavailable'}:
        return value
    if (
        not isinstance(value, dict)
        or set(value) - {'fresh_replay'}
        != {
            'status',
            'raw_audio_saved',
            'primary_frames',
            'alternate_frames',
            'primary',
            'alternate',
        }
        or value['status'] != 'measured'
        or value['raw_audio_saved'] is not False
        or any(type(value[key]) is not int for key in ('primary_frames', 'alternate_frames'))
        or not 0 <= value['alternate_frames'] <= value['primary_frames'] <= MAX_FRAMES
        or not all(_valid_stream(value[key]) for key in ('primary', 'alternate'))
        or (
            'fresh_replay' in value
            and not _valid_replay(
                value['fresh_replay'],
                value['primary']['status'] == value['alternate']['status'] == 'matched'
                and MIN_MATCH_FRAMES <= value['primary_frames'] == value['alternate_frames'],
            )
        )
    ):
        return {'status': 'invalid'}
    return value
