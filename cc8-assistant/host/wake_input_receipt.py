"""Owner IPC contract for fixed public wake fixtures."""

from math import isfinite
import re

from mac_voice import validate_prepared_speech

FIXTURE_FRAMES = 39419
FIXTURE_PCM_SHA256 = '56089a76c91d384a60fe8a011f70df7eebd6464603930671a6525f97f0ec71fe'
FIXTURE_WAV_SHA256 = '9510d2d219f1bb277578db5add07052691d92cf9a0ab8798e7490fa28e5bcb4c'
MIN_MATCH_FRAMES = FIXTURE_FRAMES + 1280
MAX_FRAMES = 64000
CHINESE_FIXTURE_FRAMES = 90432
CHINESE_PCM_SHA256 = '8e7318bb9667ef38e81d49168e2bd8cc0b0878ba3369d24735636c010696d80e'
CHINESE_WAV_SHA256 = '0ea74fc0f9241dbf78a4d0d554c251ca5d3b55d9a49638ab8f737dddba870454'
CHINESE_MAX_FRAMES = 128000


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
    return (meta['frames'], meta['pcm_sha256'], meta['wav_sha256']) in (
        (FIXTURE_FRAMES, FIXTURE_PCM_SHA256, FIXTURE_WAV_SHA256),
        (CHINESE_FIXTURE_FRAMES, CHINESE_PCM_SHA256, CHINESE_WAV_SHA256),
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


def _valid_replay(value, matched, hit_fields):
    if not matched:
        return value == {'status': 'not_run'}
    return value == {'status': 'unavailable'} or (
        isinstance(value, dict)
        and set(value) == {'status'} | hit_fields
        and value['status'] == 'measured'
        and all(type(value[key]) is bool for key in hit_fields)
    )


def _valid_live_decoder(value, primary_frames, alternate_frames):
    if not isinstance(value, dict) or set(value) != {
        'primary_samples_before',
        'alternate_samples_before',
        'render_rejections',
    }:
        return False
    # Each render rejection removes one 160-sample frame from the alternate.
    return (
        primary_frames >= 160
        and all(
            type(value[key]) is int and 0 <= value[key] < 2**53
            for key in ('primary_samples_before', 'alternate_samples_before')
        )
        and type(value['render_rejections']) is int
        and 0 <= value['render_rejections'] <= (primary_frames - alternate_frames) // 160
    )


def summarize(value):
    if value == {'status': 'unavailable'}:
        return value
    if not isinstance(value, dict):
        return {'status': 'invalid'}
    chinese = value.get('language') == 'zh'
    streams = ('primary', 'alternate', 'raw') if chinese else ('primary', 'alternate')
    frame_counts = tuple(stream + '_frames' for stream in streams)
    limit = CHINESE_MAX_FRAMES if chinese else MAX_FRAMES
    minimum = CHINESE_FIXTURE_FRAMES + 1280 if chinese else MIN_MATCH_FRAMES
    fields = {'status', 'raw_audio_saved', *streams, *frame_counts}
    if chinese:
        fields.add('language')
    if (
        set(value) - {'fresh_replay', 'live_decoder'} != fields
        or chinese
        and 'fresh_replay' not in value
        or value['status'] != 'measured'
        or value['raw_audio_saved'] is not False
        or any(type(value[key]) is not int for key in frame_counts)
        or not 0 <= value['primary_frames'] <= limit
        or any(not 0 <= value[key] <= value['primary_frames'] for key in frame_counts)
        or not all(_valid_stream(value[key]) for key in streams)
        or (
            'live_decoder' in value
            and (
                chinese
                or not _valid_live_decoder(
                    value['live_decoder'], value['primary_frames'], value['alternate_frames']
                )
            )
        )
        or (
            'fresh_replay' in value
            and not _valid_replay(
                value['fresh_replay'],
                all(value[key]['status'] == 'matched' for key in streams)
                and minimum <= value['primary_frames']
                and all(value[key] == value['primary_frames'] for key in frame_counts),
                {stream + '_hit' for stream in streams},
            )
        )
    ):
        return {'status': 'invalid'}
    return value
