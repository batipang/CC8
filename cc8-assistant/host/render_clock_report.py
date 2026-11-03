"""Bound and validate optional Android estimator provenance; never infer audible onset."""

from copy import deepcopy
import threading

SOURCES = ('hardware', 'clamped', 'missing', 'invalid', 'outside')
NUMBERS = ('observations', 'frames', 'source_changes', 'clock_steps', 'max_step_nanos')


def summarize(value):
    bad = {'status': 'invalid', 'first_audible_onset_measured': False}
    if not isinstance(value, dict) or set(value) != {
        'version',
        'complete',
        'expired',
        'invalid',
        'source_counts',
        *NUMBERS,
    }:
        return bad
    if type(value['version']) is not int or value['version'] != 1:
        return bad
    if any(type(value[k]) is not bool for k in ('complete', 'expired', 'invalid')):
        return bad
    counts = value['source_counts']
    if not isinstance(counts, dict) or set(counts) != set(SOURCES):
        return bad
    for n in (*counts.values(), *(value[k] for k in NUMBERS)):
        if type(n) is not int or not 0 <= n < 2**63:
            return bad
    n = value['observations']
    if (
        n >= 2**32
        or sum(counts.values()) != n
        or not n <= value['frames'] <= n * 32768
        or value['frames'] >= 2**32
        or value['source_changes'] > max(0, n - 1)
        or value['source_changes'] < max(0, sum(c > 0 for c in counts.values()) - 1)
        or value['source_changes'] > min(max(0, n - 1), 2 * (n - max(counts.values())))
        or value['clock_steps'] > max(0, n - 1)
        or (n <= 1 and value['max_step_nanos'] != 0)
        or (value['clock_steps'] == 0 and value['max_step_nanos'] > 40_000_000)
        or (value['clock_steps'] > 0 and value['max_step_nanos'] <= 40_000_000)
        or value['complete'] != (n > 0 and not value['expired'] and not value['invalid'])
    ):
        return bad
    return {
        'status': 'observed' if value['complete'] else 'incomplete',
        'version': 1,
        'observations': n,
        'frames': value['frames'],
        'source_counts': dict(counts),
        'source_changes': value['source_changes'],
        'clock_steps': value['clock_steps'],
        'max_clock_step_ms': value['max_step_nanos'] / 1e6,
        'expired': value['expired'],
        'invalid': value['invalid'],
        'hardware_timestamp_accepted': counts['hardware'] + counts['clamped'] > 0,
        'first_audible_onset_measured': False,
    }


class RenderClockReceipt:
    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None
        self.result = {'status': 'not_requested', 'first_audible_onset_measured': False}

    def begin(self, generation, playback_id):
        if (
            type(generation) is not int
            or type(playback_id) is not int
            or not 0 < playback_id < 2**32
            or not 0 <= generation < 2**32
        ):
            return False
        with self.lock:
            if self.owner is not None or self.result['status'] != 'not_requested':
                return False
            self.owner = (generation, playback_id)
            self.result = {'status': 'waiting', 'first_audible_onset_measured': False}
            return True

    def accept(self, generation, playback_id, value, expected_frames):
        if type(generation) is not int or type(playback_id) is not int:
            return
        result = (
            {'status': 'missing', 'first_audible_onset_measured': False}
            if value is None
            else summarize(value)
        )
        if result['status'] == 'observed' and (
            type(expected_frames) is not int or result['frames'] != expected_frames
        ):
            result = {'status': 'invalid', 'first_audible_onset_measured': False}
        with self.lock:
            if self.owner != (generation, playback_id) or self.result['status'] != 'waiting':
                return
            self.result = result

    def cancel(self):
        with self.lock:
            if self.result['status'] == 'waiting':
                self.result = {'status': 'cancelled', 'first_audible_onset_measured': False}

    def snapshot(self, *, final=False):
        with self.lock:
            if final and self.result['status'] == 'waiting':
                self.result = {'status': 'missing', 'first_audible_onset_measured': False}
            return deepcopy(self.result)
