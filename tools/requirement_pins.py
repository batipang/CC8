"""Read the flat pinned dependency manifests shared by doctor and CI."""

import re


_PIN = re.compile(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+\-]+)((?:[ \t]+--hash=sha256:[a-f0-9]{64})*)')


def read_pins(path, *, require_hashes=False):
    pins = {}
    continued = []
    for raw in path.read_text().splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.endswith('\\'):
            continued.append(line[:-1].rstrip())
            continue
        match = _PIN.fullmatch(' '.join((*continued, line)))
        if not match:
            raise ValueError('Expected a flat pinned manifest: ' + path.name)
        continued.clear()
        name, version, hashes = match.groups()
        key = re.sub(r'[-_.]+', '-', name).lower()
        if key in pins or (require_hashes and not hashes):
            raise ValueError('Expected one pin with wheel hashes: ' + name)
        pins[key] = version
    if continued or not pins:
        raise ValueError('Incomplete or empty pinned manifest: ' + path.name)
    return pins
