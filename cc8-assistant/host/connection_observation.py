"""Content-free, device-local connection receipts; no clocks are subtracted across hosts."""

STATES = frozenset(('waiting', 'connected', 'unavailable', 'not_observing'))
COUNTERS = (
    'waiting_ms',
    'unavailable_ms',
    'last_recovered_ms',
    'recovered_count',
    'recovered_total_ms',
    'interrupted_count',
)


def availability(value):
    """Copy only known state/counters; never forward device text or extensions."""
    if (
        not isinstance(value, dict)
        or type(value.get('version')) is not int
        or value['version'] != 1
    ):
        return None
    if type(value.get('state')) is not str or value['state'] not in STATES:
        return None
    result = {'version': 1, 'state': value['state']}
    for key in COUNTERS:
        number = value.get(key)
        minimum = -1 if key == 'last_recovered_ms' else 0
        if type(number) is not int or not minimum <= number <= 2**63 - 1:
            return None
        result[key] = number
    if (result['state'] != 'waiting' and result['waiting_ms'] != 0) or (
        result['state'] != 'unavailable' and result['unavailable_ms'] != 0
    ):
        return None
    count, last, total = (
        result[k] for k in ('recovered_count', 'last_recovered_ms', 'recovered_total_ms')
    )
    if (count == 0 and (last != -1 or total != 0)) or (count > 0 and (last < 0 or total < last)):
        return None
    return result


def connection_report(control):
    """Explicit fresh status query. An absent/old peer is unknown, never healthy."""
    result = {'ok': True, 'scope': 'device_activity_foreground_intervals'}
    try:
        reply = control('status', timeout=3)
    except (OSError, EOFError, RuntimeError):
        return {**result, 'observation': 'unavailable', 'connection_availability': None}
    sample = availability(reply.get('connection_availability')) if reply.get('ok') is True else None
    return {
        **result,
        'observation': 'device_reply' if sample is not None else 'missing_or_unsupported',
        'connection_availability': sample,
    }
