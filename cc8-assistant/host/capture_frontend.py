"""Validate negotiated microphone formats without owning capture/session state.

SessionController remains the owner of requested/actual formats and calls these
predicates within its existing handshake/start/rebind admission boundaries.
"""


def requested_frontend(peer, preference):
    paired = type(peer.get('paired_capture_version')) is int and peer['paired_capture_version'] == 1
    requested = 'paired' if paired and preference == 'paired' else 'mono'
    return requested


def accepts_frontend(event, requested):
    actual = event.get('capture_frontend', 'mono')
    if actual not in ('mono', 'paired'):
        return False
    fallback = event.get('capture_fallback_reason')
    if actual == 'mono' and requested == 'paired' and fallback is None:
        return False
    if fallback not in (None, 'platform_api', 'paired_format_unavailable') or (
        fallback is not None and (actual == 'paired' or requested != 'paired')
    ):
        return False
    if actual == 'paired' and (
        requested != 'paired'
        or any(
            type(event.get(key)) is not int or event[key] != expected
            for key, expected in (
                ('sample_rate', 16000),
                ('capture_sample_rate', 48000),
                ('capture_channels', 4),
                ('source', 7),
            )
        )
    ):
        return False
    return True
