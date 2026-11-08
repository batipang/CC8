"""Bounded, content-free proof for an expanded same-device response interval."""

LEGACY_INTERVAL_MS = 20000
MAX_INTERVAL_MS = 120000
CLOCK_TOLERANCE_MS = 2


def interval_clock(value, elapsed):
    if (
        type(elapsed) not in (int, float)
        or not LEGACY_INTERVAL_MS < elapsed <= MAX_INTERVAL_MS
        or not isinstance(value, dict)
        or set(value) != {'version', 'coverage_ms', 'discontinuities'}
        or type(value['version']) is not int
        or value['version'] != 1
        or type(value['discontinuities']) is not int
        or value['discontinuities'] != 0
        or type(value['coverage_ms']) not in (int, float)
        or not elapsed - CLOCK_TOLERANCE_MS
        <= value['coverage_ms']
        <= MAX_INTERVAL_MS + CLOCK_TOLERANCE_MS
    ):
        return None
    return dict(value)
