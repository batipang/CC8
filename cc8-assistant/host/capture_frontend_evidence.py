"""Content-free evidence of the active native capture format, never a preference."""

FRONTENDS = ('mono', 'paired')
FALLBACKS = ('platform_api', 'paired_format_unavailable')


def summarize(device, host):
    if 'capture_frontend' not in device or 'capture_frontend' not in host:
        return {'status': 'unavailable', 'reason': 'legacy_metadata_missing'}
    if device.get('microphone_active') is not True or host.get('microphone_active') is not True:
        return {'status': 'unconfirmed', 'reason': 'capture_not_active'}
    mode = device.get('capture_frontend')
    if mode not in FRONTENDS or host.get('capture_frontend') != mode:
        return {'status': 'unconfirmed', 'reason': 'frontend_not_confirmed'}
    rate, channels = (48000, 4) if mode == 'paired' else (16000, 1)
    if (
        type(device.get('paired_capture_version')) is not int
        or device['paired_capture_version'] != 1
        or type(device.get('capture_sample_rate')) is not int
        or device['capture_sample_rate'] != rate
        or type(device.get('capture_channels')) is not int
        or device['capture_channels'] != channels
    ):
        return {'status': 'unconfirmed', 'reason': 'native_format_not_confirmed'}
    fallback = device.get('capture_fallback_reason')
    if fallback not in (None, *FALLBACKS) or (mode == 'paired' and fallback is not None):
        return {'status': 'unconfirmed', 'reason': 'fallback_not_confirmed'}
    return {
        'status': 'observed',
        'frontend': mode,
        'capture_sample_rate': rate,
        'capture_channels': channels,
        'fallback_reason': fallback,
        # This is only the reported capability, not verified timestamp evidence.
        'capture_clock_supported': device.get('capture_clock_evidence_supported')
        if type(device.get('capture_clock_evidence_supported')) is bool
        else None,
    }


def valid_report(report):
    """Legacy reports remain unmeasured; an explicit frontend claim needs evidence."""
    expected = report.get('expected_capture_frontend')
    if expected not in (None, *FRONTENDS):
        return False
    value = report.get('capture_frontend_evidence')
    if value is None:
        return expected is None
    if type(value) is not dict:
        return False
    if value.get('status') != 'observed':
        return expected is None and value == {
            'status': 'unavailable',
            'reason': 'legacy_metadata_missing',
        }
    mode = value.get('frontend')
    if mode not in FRONTENDS or (expected is not None and mode != expected):
        return False
    rate, channels = (48000, 4) if mode == 'paired' else (16000, 1)
    return (
        set(value)
        == {
            'status',
            'frontend',
            'capture_sample_rate',
            'capture_channels',
            'fallback_reason',
            'capture_clock_supported',
        }
        and type(value.get('capture_sample_rate')) is int
        and value['capture_sample_rate'] == rate
        and type(value.get('capture_channels')) is int
        and value['capture_channels'] == channels
        and value.get('fallback_reason') in ((None,) if mode == 'paired' else (None, *FALLBACKS))
        and (
            value.get('capture_clock_supported') is None
            or type(value['capture_clock_supported']) is bool
        )
    )


def matches_expected(report, expected):
    return expected is None or (
        type(report) is dict
        and report.get('expected_capture_frontend') == expected
        and valid_report(report)
        and type(report.get('capture_frontend_evidence')) is dict
        and report['capture_frontend_evidence'].get('status') == 'observed'
        and report['capture_frontend_evidence'].get('frontend') == expected
    )
