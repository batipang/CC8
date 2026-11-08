"""Content-free boundary counters and one owned public standby stimulus."""

from mac_voice import PreparedSpeech, SpeechFixtureCleanupError, validate_prepared_speech
from qa_speech import QUESTIONS, WAKE

COUNTERS = (
    'local_wake_chinese_candidates',
    'local_wake_chinese_rechecks',
    'local_wake_chinese_recheck_hits',
    'local_wake_chinese_unavailable',
)


def snapshot(health):
    """Copy a boundary; missing legacy fields remain unmeasured, never zero."""
    if not isinstance(health, dict):
        return None
    values = {key: health.get(key) for key in COUNTERS}
    if all(type(value) is int and 0 <= value < 2**63 for value in values.values()):
        return values
    return None


def counter_delta(before, health):
    after = snapshot(health)
    if before is None or after is None:
        return {'status': 'unavailable'}
    if snapshot(before) != before or any(after[key] < before[key] for key in COUNTERS):
        return {'status': 'invalid'}
    return {'status': 'observed', **{key: after[key] - before[key] for key in COUNTERS}}


def valid_counter_delta(value):
    return (
        isinstance(value, dict)
        and set(value) == {'status', *COUNTERS}
        and value['status'] == 'observed'
        and snapshot(value) is not None
    )


def run_fixed_standby(make_run, save, run_suite):
    """Share one generated Chinese WAV across all three real standby cycles.

    The existing suite owns recording and its cleanup. This wrapper owns only
    public synthetic audio; it cannot open a device, retry a phase or rearm voice.
    A completed playback command does not prove acoustic arrival at the CC8.
    """
    fixture = None
    metadata = None
    report = {'schema': 1, 'scope': 'fixed_standby_suite', 'state': 'running', 'phases': []}

    def checkpoint(value):
        nonlocal report
        report = {
            **value,
            'prepared_chinese_input': metadata,
            'prepared_chinese_input_cleaned': False,
        }
        save(report)

    def bound_run(options, update, **kwargs):
        if kwargs or options != {'capture', 'standby', 'acoustic'}:
            raise ValueError('Fixed Chinese source requires the standard standby plan')
        return make_run(options, update, chinese_fixture=fixture)

    try:
        fixture = PreparedSpeech('Tingting', WAKE + '，' + QUESTIONS[0])
        metadata = validate_prepared_speech(fixture.metadata)
        checkpoint(report)
        checkpoint(run_suite(bound_run, checkpoint, standby_only=True))
        if (report.get('state') == 'passed' and len(report['phases']) != 3) or any(
            phase.get('state') == 'passed'
            and (
                phase.get('report', {}).get('prepared_chinese_input') != metadata
                or phase['report'].get('chinese_fixture_played') is not True
            )
            for phase in report['phases']
        ):
            report.update(state='failed', reason='chinese_stimulus_identity_unconfirmed')
    except SpeechFixtureCleanupError as error:
        fixture = error.fixture
        report.update(state='failed', reason='fixture_preparation_failed')
    except KeyboardInterrupt:
        report['state'] = 'interrupted'
    except Exception:
        report.update(state='failed', reason='fixed_standby_operation_failed')
    finally:
        cleaned = fixture is None
        if fixture is not None:
            try:
                fixture.close()
                cleaned = True
            except Exception:
                report.update(state='cleanup_unconfirmed', reason='cleanup_fixture_not_removed')
        report.update(prepared_chinese_input=metadata, prepared_chinese_input_cleaned=cleaned)
        save(report)
    return report
