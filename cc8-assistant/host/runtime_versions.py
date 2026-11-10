"""Public runtime declarations; never installation authorization or filesystem probes."""

import re
from release_info import RELEASE


def public_release(release=RELEASE):
    # Use the process's loaded manifest, not whatever a checkout now says on disk.
    return {
        'schema_version': 1,
        'release_id': release.release_id,
        'backend_version': release.backend_version,
        'expected_assistant_version': release.assistant.version,
        'expected_assistant_code': release.assistant.version_code,
        'protocol_version': release.protocol_version,
        'device_protocol_min': release.device_protocol_min,
        'device_protocol_max': release.device_protocol_max,
    }


def device_release(value):
    fields = {'schema_version', 'assistant_version', 'assistant_code', 'protocol_version'}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    version = value['assistant_version']
    if (
        type(value['schema_version']) is not int
        or value['schema_version'] != 1
        or not isinstance(version, str)
        or len(version) > 32
        or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version) is None
        or type(value['assistant_code']) is not int
        or not 0 < value['assistant_code'] < 2**31
        or type(value['protocol_version']) is not int
        or not 0 < value['protocol_version'] <= 255
    ):
        return None
    return {key: value[key] for key in fields}


def status_versions(result, release=RELEASE):
    """Enrich only an acknowledged status; device claims never override host fields."""
    host = public_release(release)
    device = device_release(result.get('device_release'))
    return {
        **result,
        'host_release': host,
        'device_release': device,
        'declared_protocol_compatible': release.compatible(device['protocol_version'])
        if device
        else None,
        'declared_version_pair_matches': (
            device['assistant_version'] == release.assistant.version
            and device['assistant_code'] == release.assistant.version_code
        )
        if device
        else None,
        'installation_verified': False,
    }
