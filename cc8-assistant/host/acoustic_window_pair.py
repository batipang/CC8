"""Content-free corroboration for two fixed contiguous acoustic windows."""

PAIR_ALIGNMENT = 'matched_contiguous_windows'


def pair_evidence(value):
    if not isinstance(value, dict) or set(value) != {
        'version',
        'window_ms',
        'window_correlations',
        'joint_correlation',
        'competing_correlation',
        'prefix_window_ms',
        'prefix_correlation',
        'prefix_competing_correlation',
        'prefix_offset_ms',
    }:
        return None
    if type(value['version']) is not int or value['version'] != 1:
        return None
    if type(value['window_ms']) is not int or value['window_ms'] != 200:
        return None
    if type(value['prefix_window_ms']) is not int or value['prefix_window_ms'] != 40:
        return None
    correlations = value['window_correlations']
    if not isinstance(correlations, list) or len(correlations) != 2:
        return None
    joint, rival = value['joint_correlation'], value['competing_correlation']
    if not all(type(x) in (int, float) and 0.55 <= x <= 1 for x in (*correlations, joint)):
        return None
    if type(rival) not in (int, float) or not 0 <= rival <= 1 or joint - rival < 0.12:
        return None
    prefix, prefix_rival, offset = (
        value['prefix_correlation'],
        value['prefix_competing_correlation'],
        value['prefix_offset_ms'],
    )
    if (
        type(prefix) not in (int, float)
        or not 0.55 <= prefix <= 1
        or type(prefix_rival) not in (int, float)
        or not 0 <= prefix_rival <= 1
        or prefix - prefix_rival < 0.12
        or type(offset) not in (int, float)
        or not -2 <= offset <= 2
    ):
        return None
    return dict(value, window_correlations=list(correlations))
