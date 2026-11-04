"""Check component versions and record content hashes; never install or deploy."""

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
from release_info import RELEASE, load_release  # noqa: E402

if __package__:
    from .apk_signing import signer_fingerprints
    from .java_toolchain import require_java
else:
    from apk_signing import signer_fingerprints
    from java_toolchain import require_java


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def git_state(root=ROOT):
    def git(*args):
        return subprocess.check_output(
            ['git', '-C', str(root), *args], text=True, timeout=10
        ).strip()

    commit = git('rev-parse', 'HEAD')
    dirty = bool(git('status', '--porcelain', '--untracked-files=normal'))
    return commit, dirty


def check_apk(component, apk, root=ROOT):
    release = load_release(root)
    info = getattr(release, component)
    paths = json.loads((root / '.toolchains/cc8/paths.json').read_text())
    sdk = Path(paths['android_sdk']) / 'build-tools/35.0.0'
    if not 0 < apk.stat().st_size <= 150 * 1024 * 1024:
        raise ValueError('APK size exceeds release-check budget')
    before = digest(apk)
    metadata = subprocess.check_output(
        [str(sdk / 'aapt'), 'dump', 'badging', str(apk)], text=True, timeout=20
    )
    package = re.search(
        r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'",
        metadata,
        re.MULTILINE,
    )
    if not package or (package[1], int(package[2]), package[3]) != (
        info.package,
        info.version_code,
        info.version,
    ):
        raise ValueError('Built APK package/version does not match release.json')
    signers = signer_fingerprints(apk, require_java(root=root) / 'java', sdk / 'lib/apksigner.jar')
    if digest(apk) != before:
        raise ValueError('APK changed during release verification')
    return {
        'component': component,
        'filename': f'cc8-{component}-{info.version}.apk',
        'package': info.package,
        'version': info.version,
        'version_code': info.version_code,
        'sha256': before,
        'signer_sha256': sorted(signers),
    }


def check_build_receipt(component, apk, artifact, root, commit, dirty):
    with apk.with_suffix('.apk.build.json').open('rb') as source:
        receipt_raw = source.read(16385)
    if len(receipt_raw) > 16384:
        raise ValueError('Oversized build receipt')
    receipt = json.loads(receipt_raw)
    expected = {
        'schema_version': 1,
        'component': component,
        'version': artifact['version'],
        'version_code': artifact['version_code'],
        'source_commit': commit,
        'source_dirty': dirty,
        'manifest_sha256': digest(root / 'release.json'),
        'apk_sha256': artifact['sha256'],
    }
    if json.dumps(receipt, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError('APK build receipt does not match this source revision; rebuild the APK')


def create_record(root=ROOT, *, allow_dirty=False, tag=None):
    release = load_release(root)
    commit, dirty = git_state(root)
    if dirty and not allow_dirty:
        raise ValueError(
            'Commit source changes before recording a release; --allow-dirty is candidate-only'
        )
    if tag:
        # Resolve only a bounded, explicit tag ref, never arbitrary revision options.
        if (
            not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/-]{0,100}', tag)
            or '..' in tag
            or tag.endswith('/')
        ):
            raise ValueError('Invalid release tag')
        ref = 'refs/tags/' + tag
        kind = subprocess.check_output(
            ['git', '-C', str(root), 'cat-file', '-t', ref], text=True, timeout=10
        ).strip()
        target = subprocess.check_output(
            ['git', '-C', str(root), 'rev-parse', ref + '^{commit}'], text=True, timeout=10
        ).strip()
        if kind != 'tag' or target != commit or dirty:
            raise ValueError(
                'Release tag must be annotated and point to the current clean source commit'
            )
    components = []
    for name in ('assistant', 'diagnostics'):
        info = getattr(release, name)
        apk = root / 'artifacts' / f'cc8-{name}-{info.version}.apk'
        item = check_apk(name, apk, root)
        check_build_receipt(name, apk, item, root, commit, dirty)
        components.append(item)
    if git_state(root) != (commit, dirty):
        raise ValueError('Source changed while recording the release')
    return {
        'schema_version': 1,
        'release_id': release.release_id,
        'source_commit': commit,
        'source_dirty': dirty,
        'tag': tag,
        'manifest_sha256': digest(root / 'release.json'),
        'backend_version': release.backend_version,
        'protocol_version': release.protocol_version,
        'device_protocol_min': release.device_protocol_min,
        'device_protocol_max': release.device_protocol_max,
        'artifacts': components,
        'device_acceptance': 'not_asserted',
    }


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(record, out, indent=2, allow_nan=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_record(path, root=ROOT):
    with path.open('rb') as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('Release record exceeds size limit')

    def pairs(items):
        value = {}
        for name, item in items:
            if name in value:
                raise ValueError('Duplicate release record field')
            value[name] = item
        return value

    supplied = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
    if not isinstance(supplied, dict) or supplied.get('source_dirty') is not False:
        raise ValueError('Candidate/invalid records are not a verified release')
    tag = supplied.get('tag')
    if tag is not None and not isinstance(tag, str):
        raise ValueError('Invalid record tag')
    # Regenerate only expected local artifact names; never follow a path supplied
    # by the record. Strict equality also rejects missing/extra or altered fields.
    actual = create_record(root, tag=tag)
    if json.dumps(supplied, sort_keys=True, allow_nan=False) != json.dumps(
        actual, sort_keys=True, allow_nan=False
    ):
        raise ValueError('Release record does not match current clean source and artifacts')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('show')
    check = commands.add_parser('check-apk')
    check.add_argument('component', choices=('assistant', 'diagnostics'))
    check.add_argument('apk', type=Path)
    record = commands.add_parser('record')
    record.add_argument('--allow-dirty', action='store_true')
    record.add_argument('--tag')
    record.add_argument(
        '--output', type=Path, default=ROOT / 'artifacts/releases' / (RELEASE.release_id + '.json')
    )
    verify = commands.add_parser('verify')
    verify.add_argument('record', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'show':
            print(json.dumps(asdict(RELEASE), indent=2))
        elif args.action == 'check-apk':
            print(json.dumps(check_apk(args.component, args.apk), indent=2))
        elif args.action == 'record':
            value = create_record(allow_dirty=args.allow_dirty, tag=args.tag)
            write_record(args.output, value)
            print('Release record saved; device acceptance is not asserted.')
        else:
            verify_record(args.record)
            print(
                'Release source, versions, APK hashes and signer sets match; device acceptance is not asserted.'
            )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError):
        parser.exit(
            2,
            'Release check failed; verify clean source, tag, manifest and both built APKs. No device action performed.\n',
        )


if __name__ == '__main__':
    main()
