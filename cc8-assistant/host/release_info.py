"""Validated independent component versions shared with the Android builds."""

from __future__ import annotations
from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ApkVersion:
    version: str
    version_code: int
    package: str


@dataclass(frozen=True)
class Release:
    release_id: str
    backend_version: str
    assistant: ApkVersion
    diagnostics: ApkVersion
    protocol_version: int
    device_protocol_min: int
    device_protocol_max: int

    def compatible(self, device_protocol: object) -> bool:
        return (
            type(device_protocol) is int
            and self.device_protocol_min <= device_protocol <= self.device_protocol_max
        )


def _object(value: object, names: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError('Invalid release manifest fields; see docs/RELEASES.md')
    return {str(key): item for key, item in value.items()}


def _integer(value: object, maximum: int = 2**31 - 1) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('Invalid release integer')
    return value


def _text(value: object, pattern: str) -> str:
    if not isinstance(value, str) or len(value) > 100 or not re.fullmatch(pattern, value):
        raise ValueError('Invalid release text')
    return value


def _version(value: object) -> str:
    return _text(value, r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


def _apk(value: object, package: str) -> ApkVersion:
    item = _object(value, {'version', 'version_code', 'package'})
    if item['package'] != package:
        raise ValueError('Unexpected release package')
    return ApkVersion(_version(item['version']), _integer(item['version_code']), package)


def load_release(root: Path = ROOT) -> Release:
    with (root / 'release.json').open('rb') as source:
        raw = source.read(16385)
    if len(raw) > 16384:
        raise ValueError('Release manifest exceeds size limit')

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate release field')
            result[key] = value
        return result

    try:
        data: object = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
    except (UnicodeError, RecursionError):
        raise ValueError('Invalid release encoding') from None
    values = _object(
        data,
        {
            'schema_version',
            'release_id',
            'backend',
            'assistant',
            'diagnostics',
            'protocol',
            'known_deployed_pair',
        },
    )
    if type(values['schema_version']) is not int or values['schema_version'] != 1:
        raise ValueError('Unsupported release schema')
    protocol = _object(values['protocol'], {'version', 'device_min', 'device_max'})
    low = _integer(protocol['device_min'], 255)
    high = _integer(protocol['device_max'], 255)
    current = _integer(protocol['version'], 255)
    if not low <= current <= high:
        raise ValueError('Inconsistent protocol compatibility range')
    backend = _object(values['backend'], {'version'})
    known = _object(
        values['known_deployed_pair'],
        {'assistant_version', 'assistant_version_code', 'backend_version', 'protocol'},
    )
    _version(known['assistant_version'])
    _version(known['backend_version'])
    _integer(known['assistant_version_code'])
    _integer(known['protocol'], 255)
    return Release(
        _text(values['release_id'], r'[a-z][a-z0-9.-]{0,63}'),
        _version(backend['version']),
        _apk(values['assistant'], 'com.happyhome.cc8assistant'),
        _apk(values['diagnostics'], 'com.happyhome.cc8diagnostics'),
        current,
        low,
        high,
    )


RELEASE = load_release()


@dataclass(frozen=True)
class SourceStamp:
    commit: str | None
    dirty: bool
    manifest_sha256: str


def source_stamp(root: Path = ROOT) -> SourceStamp:
    manifest_hash = hashlib.sha256((root / 'release.json').read_bytes()).hexdigest()
    try:
        commit = subprocess.check_output(
            ['git', '-C', str(root), 'rev-parse', 'HEAD'],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
        status = subprocess.check_output(
            ['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=normal'],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('Invalid source revision')
        return SourceStamp(commit, bool(status.strip()), manifest_hash)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Source archives can still build candidates; a release record requires
        # a clean Git revision and therefore cannot mistake this for provenance.
        return SourceStamp(None, True, manifest_hash)


def write_build_receipt(
    apk: Path, component: Literal['assistant', 'diagnostics'], stamp: SourceStamp, root: Path = ROOT
) -> None:
    if source_stamp(root) != stamp:
        raise ValueError('Source changed during build; rebuild before recording a release')
    release = load_release(root)
    version = release.assistant if component == 'assistant' else release.diagnostics
    digest = hashlib.sha256()
    with apk.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    value = {
        'schema_version': 1,
        'component': component,
        'version': version.version,
        'version_code': version.version_code,
        'source_commit': stamp.commit,
        'source_dirty': stamp.dirty,
        'manifest_sha256': stamp.manifest_sha256,
        'apk_sha256': digest.hexdigest(),
    }
    fd, temporary = tempfile.mkstemp(prefix='.build-receipt-', dir=apk.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, apk.with_suffix('.apk.build.json'))
    finally:
        Path(temporary).unlink(missing_ok=True)


def check_artifact_replacement(apk: Path, built: Path, stamp: SourceStamp) -> bool:
    """Protect existing clean APK bytes; return whether to write a fresh receipt."""
    if not apk.exists():
        return True

    def digest(path: Path) -> str:
        value = hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda: source.read(65536), b''):
                value.update(block)
        return value.hexdigest()

    original, proposed = digest(apk), digest(built)
    receipt = apk.with_suffix('.apk.build.json')
    if receipt.exists():
        if receipt.stat().st_size > 16384:
            raise ValueError('Existing APK build receipt is oversized; preserve it for inspection')
        data = json.loads(receipt.read_text())
        if (
            not isinstance(data, dict)
            or type(data.get('source_dirty')) is not bool
            or data.get('apk_sha256') != original
        ):
            raise ValueError('Existing APK does not match its receipt; refusing to overwrite')
        if data['source_dirty']:
            return True
    # Missing provenance cannot authorize overwriting an unknown baseline.
    if proposed != original:
        raise ValueError(
            'A protected APK already uses this version; bump the version before publishing new bytes'
        )
    # A dirty rebuild of identical bytes must not replace clean provenance.
    return not stamp.dirty
