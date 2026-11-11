"""Offline upgrade planning and explicit non-secret settings backup; never deploy."""

import argparse
import ast
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo
from configuration import _read as read_profile, ConfigError
from config_schema import FIELDS, validate_document
from private_output import PrivateOutput, private_directory
from release_info import ROOT, load_release
from identity_compatibility import storage_gate as identity_storage_gate

EXCLUDED = [
    'credentials',
    'signing_keys',
    'pairing',
    'models',
    'recordings',
    'reminders',
    'task_results',
    'environment_overrides',
    'service_definition',
]

# Recovery may carry these reviewed CI-only download fixes, never runtime changes.
RECOVERY_BUILD_SUPPORT = frozenset(
    ('.github/workflows/ci.yml', 'tools/tests/test_gradle_provision.py')
)


def _recovery_source_gate(roots, commits):
    """Compare pinned Git objects; reject missing, linked or divergent support files."""
    changed = (
        subprocess.check_output(
            [
                'git',
                '-C',
                str(roots[2]),
                'diff',
                '--no-ext-diff',
                '--no-renames',
                '--name-only',
                '-z',
                commits[0],
                commits[2],
                '--',
            ],
            timeout=15,
        )
        .decode()
        .split('\0')[:-1]
    )
    extra = set(changed) - {'release.json'}
    if 'release.json' not in changed or extra - RECOVERY_BUILD_SUPPORT:
        raise ValueError('Recovery source may change only release.json and matching CI support')
    for path in sorted(extra):
        entries = []
        for root, commit in zip(roots[1:], commits[1:]):
            entry = subprocess.check_output(
                ['git', '-C', str(root), 'ls-tree', '-z', commit, '--', path],
                timeout=10,
            )
            # Require one regular non-executable blob at this exact path in both trees.
            if not re.fullmatch(
                rb'100644 blob [a-f0-9]{40}\t' + re.escape(path.encode()) + rb'\x00', entry
            ):
                raise ValueError('Recovery CI support must be a regular candidate-matching file')
            entries.append(entry)
        if entries[0] != entries[1]:
            raise ValueError('Recovery CI support differs from the candidate source')
    return {'changed_paths': changed, 'candidate_matching_build_support': sorted(extra)}


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False
        ).encode()
    ).hexdigest()


def reminder_storage_gate(before_root, after_root):
    """Read schema numbers only; never export reminders or overwrite their store."""
    from reminder_store import ReminderStore

    source = Path(after_root) / 'cc8-assistant/host/workspace.py'
    maximum = 1
    if source.is_file():
        for node in ast.parse(source.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'REMINDER_STORE_VERSION' for t in node.targets
            ):
                if (
                    not isinstance(node.value, ast.Constant)
                    or type(node.value.value) is not int
                    or node.value.value not in (1, 2, 3)
                ):
                    raise ValueError('Invalid reminder reader declaration')
                maximum = node.value.value
    store = ReminderStore(Path(before_root) / 'device-access/private/voice-runtime/reminders.json')
    value = store.load()
    present = store.expected is not None
    version = 1
    if present:
        if (
            not isinstance(value, dict)
            or type(value.get('version')) is not int
            or value['version'] not in (1, 2, 3)
        ):
            raise ValueError('Invalid existing reminder store schema')
        version = value['version']
    return {
        'present': present,
        'store_version': version,
        'reader_max_version': maximum,
        'compatible': version <= maximum,
        'reminder_content_exported': False,
    }


def report_watch_storage_gate(before_root, after_root):
    """Optional summary format may outgrow recovery; inspect metadata without exporting it."""
    from reminder_store import ReminderStore

    source = Path(after_root) / 'cc8-assistant/host/report_watch_store.py'
    maximum = 1
    if source.is_file():
        for node in ast.parse(source.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'READER_VERSION' for t in node.targets
            ):
                if (
                    not isinstance(node.value, ast.Constant)
                    or type(node.value.value) is not int
                    or node.value.value not in (1, 2)
                ):
                    raise ValueError('Invalid scheduled report reader declaration')
                maximum = node.value.value
    disk = ReminderStore(
        Path(before_root) / 'device-access/private/voice-runtime/work-report-watch.json'
    )
    value = disk.load()
    present = disk.expected is not None
    version = 1
    if present:
        # This stdlib CLI also runs on macOS Python 3.9. Do not import the
        # voice runtime's report/provider graph just to inspect a format number.
        if (
            not isinstance(value, dict)
            or set(value) != {'schema', 'revision', 'watch'}
            or type(value.get('schema')) is not int
            or value['schema'] not in (1, 2)
        ):
            raise ValueError('Invalid existing scheduled report store schema')
        version = value['schema']
    return dict(
        present=present,
        store_version=version,
        reader_max_version=maximum,
        compatible=version <= maximum,
        report_content_exported=False,
    )


def plan(before_root, after_root, assistant_apk=None):
    before, after = load_release(Path(before_root)), load_release(Path(after_root))
    old_device_new_host = after.compatible(before.protocol_version)
    new_device_old_host = before.compatible(after.protocol_version)
    value = {
        'schema_version': 1,
        'kind': 'cc8_source_upgrade_plan',
        'before': asdict(before),
        'after': asdict(after),
        'before_versions_sha256': fingerprint(asdict(before)),
        'after_versions_sha256': fingerprint(asdict(after)),
        'old_device_new_host_protocol': old_device_new_host,
        'new_device_old_host_protocol': new_device_old_host,
        'declared_protocol_compatible': old_device_new_host and new_device_old_host,
        'assistant_downgrade': after.assistant.version_code < before.assistant.version_code,
        'diagnostics_downgrade': after.diagnostics.version_code < before.diagnostics.version_code,
        'apk_checked': False,
        'installed_signer_checked': False,
        'ready_for_installation': False,
        'device_acceptance': 'not_asserted',
        'required_before_deployment': [
            'actual_installed_versions',
            'installed_signer_match',
            'service_ownership',
            'configuration_backup',
            'physical_handover_validation',
            'reminder_store_compatibility',
            'report_watch_store_compatibility',
        ],
        'reminder_storage': reminder_storage_gate(before_root, after_root),
        'report_watch_storage': report_watch_storage_gate(before_root, after_root),
        'identity_storage': identity_storage_gate(before_root, after_root),
    }
    if assistant_apk is not None:
        # Reuse actual AAPT/API-27 signature checks; no adb or installation here.
        from tools.release import check_apk

        value['assistant_apk'] = check_apk('assistant', Path(assistant_apk), root=Path(after_root))
        value['apk_checked'] = True
    return value


def verify_assistant_transition(
    baseline_root, candidate_root, recovery_root, baseline_apk, pin, *, observation_path=None
):
    """Bind the two source builds to a previously verified baseline APK."""
    from tools.apk_signing import require_same_signers
    from tools.release import check_apk, check_build_receipt, digest, git_state

    if not re.fullmatch('[a-f0-9]{64}', pin):
        raise ValueError('Expected a pinned baseline APK SHA-256')
    roots = tuple(Path(root).resolve() for root in (baseline_root, candidate_root, recovery_root))
    if len(set(roots)) != 3:
        raise ValueError('Expected separate baseline, candidate and recovery workspaces')
    for root in roots:
        top = subprocess.check_output(
            ['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
            text=True,
            timeout=10,
        ).strip()
        if Path(top).resolve() != root:
            raise ValueError('Expected a Git workspace root')
    states = [git_state(root) for root in roots]
    if any(dirty or not re.fullmatch('[a-f0-9]{40}', commit) for commit, dirty in states):
        raise ValueError('Expected three clean, pinned source workspaces')
    versions = [load_release(root) for root in roots]
    before, candidate, recovery = versions
    if (
        not before.assistant.version_code
        < candidate.assistant.version_code
        < recovery.assistant.version_code
    ):
        raise ValueError('Upgrade and recovery APKs must increase versionCode')
    if not all(
        host.compatible(device.protocol_version) for host in versions for device in versions
    ):
        raise ValueError('A transition crosses an incompatible host/device protocol')
    if replace(recovery, release_id=before.release_id, assistant=before.assistant) != before:
        raise ValueError('Recovery must retain baseline runtime and protocol metadata')
    recovery_source = _recovery_source_gate(roots, [state[0] for state in states])
    reminder_gates = {
        name: reminder_storage_gate(roots[0], target)
        for name, target in (('candidate', roots[1]), ('recovery', roots[2]))
    }
    if not all(gate['compatible'] for gate in reminder_gates.values()):
        raise ValueError(
            'Reminder store is not readable by candidate/recovery; do not restore old data'
        )
    watch_gates = {
        name: report_watch_storage_gate(roots[0], target)
        for name, target in (('candidate', roots[1]), ('recovery', roots[2]))
    }
    if not all(gate['compatible'] for gate in watch_gates.values()):
        raise ValueError(
            'Scheduled report store is not readable by candidate/recovery; keep it intact'
        )
    identity_gates = {
        name: identity_storage_gate(roots[0], target)
        for name, target in (('candidate', roots[1]), ('recovery', roots[2]))
    }
    if not all(gate['compatible'] for gate in identity_gates.values()):
        raise ValueError(
            'Guest privacy state requires an identity-aware backend; restore owner explicitly before legacy recovery'
        )

    apks = [Path(baseline_apk)]
    baseline = check_apk('assistant', apks[0], root=roots[0])
    if not hmac.compare_digest(baseline['sha256'], pin):
        raise ValueError('Baseline APK does not match the recorded installed artifact')
    signers = frozenset(baseline['signer_sha256'])
    artifacts = [baseline]
    for root, release, (commit, dirty) in zip(roots[1:], versions[1:], states[1:]):
        apk = root / 'artifacts' / f'cc8-assistant-{release.assistant.version}.apk'
        apks.append(apk)
        artifact = check_apk('assistant', apk, root=root)
        require_same_signers(frozenset(artifact['signer_sha256']), signers)
        check_build_receipt('assistant', apk, artifact, root, commit, dirty)
        artifacts.append(artifact)
    if [git_state(root) for root in roots] != states:
        raise ValueError('A source workspace changed during verification')

    if any(digest(apk) != item['sha256'] for apk, item in zip(apks, artifacts)):
        raise ValueError('An APK changed during transition verification')

    observation = {}
    if observation_path is not None:
        from audit_observation import audit, load_report

        recorded = load_report(observation_path)
        checked = audit(recorded, before.backend_version)
        observation['observation_gate'] = {
            'baseline_backend_version': before.backend_version,
            'report_sha256': fingerprint(recorded),
            'window_verified': checked['window_verified'],
            'successful_polls': checked['successful_polls'],
            'elapsed_seconds': checked['elapsed_seconds'],
            'reasons': checked['reasons'],
        }

    return {
        'schema_version': 1,
        'kind': 'cc8_assistant_transition_artifacts',
        'verified_against_pinned_baseline': True,
        'stages': {
            name: {
                'source_commit': state[0],
                'backend_version': version.backend_version,
                'protocol': version.protocol_version,
                'apk': artifact,
            }
            for name, state, version, artifact in zip(
                ('baseline', 'candidate', 'recovery'), states, versions, artifacts
            )
        },
        'installed_signer_checked': False,
        'ready_for_installation': False,
        'device_acceptance': 'not_asserted',
        'reminder_storage': reminder_gates,
        'report_watch_storage': watch_gates,
        'recovery_source': recovery_source,
        **observation,
    }


def backup(profile, source_root=ROOT):
    # Reuse bounded configuration validation before constructing the snapshot.
    values, legacy = read_profile(Path(profile), private=True)
    settings = {
        'schema_version': 1,
        **{name: field.default for name, field in FIELDS.items()},
        **values,
    }
    source_root = Path(source_root).absolute()
    # Pin references to their original workspace, without opening key contents.
    for name in ('credential_file', 'signing_keystore'):
        raw = settings[name]
        if raw is None:
            if name != 'signing_keystore':
                raise ValueError('Credential file is required')
            raw = 'cc8-diagnostics/signing/cc8-diagnostics-debug.jks'
        path = Path(raw).expanduser()
        settings[name] = str(path if path.is_absolute() else source_root / path)
    validate_document(settings)
    return {
        'schema_version': 1,
        'kind': 'cc8_nonsecret_settings_backup',
        'created_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'legacy_source': legacy,
        'settings': settings,
        'settings_sha256': fingerprint(settings),
        'excluded': EXCLUDED[:],
    }


def read_private_json(path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Owner-private regular file required')
        raw = source.read(32769)
    if len(raw) > 32768:
        raise ValueError('Backup exceeds size limit')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate backup field')
            result[key] = value
        return result

    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)


def verify_backup(path):
    value = read_private_json(path)
    if not isinstance(value, dict) or set(value) != {
        'schema_version',
        'kind',
        'created_at',
        'legacy_source',
        'settings',
        'settings_sha256',
        'excluded',
    }:
        raise ValueError('Invalid backup fields')
    if (
        type(value['schema_version']) is not int
        or value['schema_version'] != 1
        or value['kind'] != 'cc8_nonsecret_settings_backup'
        or type(value['legacy_source']) is not bool
        or value['excluded'] != EXCLUDED
        or not isinstance(value['settings_sha256'], str)
        or not re.fullmatch('[a-f0-9]{64}', value['settings_sha256'])
    ):
        raise ValueError('Invalid backup contract')
    created = value['created_at']
    if (
        not isinstance(created, str)
        or len(created) > 40
        or datetime.fromisoformat(created).tzinfo is None
    ):
        raise ValueError('Invalid backup date')
    settings = value['settings']
    validate_document(settings)
    if type(settings.get('schema_version')) is not int or settings['schema_version'] != 1:
        raise ValueError('Normalized settings version required')
    if not hmac.compare_digest(value['settings_sha256'], fingerprint(settings)):
        raise ValueError('Backup settings checksum mismatch')
    return settings


def output(value, path=None):
    if path is None:
        private = private_directory(ROOT / 'device-access/private')
        folder = private_directory(private / 'upgrade-preparation')
        path = folder / ('upgrade-' + uuid.uuid4().hex + '.json')
    with PrivateOutput(path) as report:
        report.write(value)
    return Path(path).absolute()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    planned = commands.add_parser(
        'plan', help='Compare declared source versions; never proves deployment readiness'
    )
    planned.add_argument('--from-root', type=Path, required=True)
    planned.add_argument('--to-root', type=Path, default=ROOT)
    planned.add_argument('--assistant-apk', type=Path)
    planned.add_argument('--output', type=Path)
    transition = commands.add_parser(
        'verify-transition',
        help='Check signed upgrade and higher-version recovery artifacts without device access',
    )
    transition.add_argument('--from-root', type=Path, required=True)
    transition.add_argument('--to-root', type=Path, required=True)
    transition.add_argument('--recovery-root', type=Path, required=True)
    transition.add_argument('--baseline-apk', type=Path, required=True)
    transition.add_argument('--baseline-sha256', required=True)
    transition.add_argument(
        '--observation-report',
        type=Path,
        help='Also require a complete 72-hour private report for the baseline backend version',
    )
    transition.add_argument('--output', type=Path)
    saved = commands.add_parser(
        'backup', help='Explicitly save only one non-secret settings profile'
    )
    saved.add_argument('--profile', type=Path, required=True)
    saved.add_argument(
        '--source-root',
        type=Path,
        default=ROOT,
        help='Original workspace for relative credential/key references; contents are not copied',
    )
    saved.add_argument('--output', type=Path)
    checked = commands.add_parser('verify', help='Verify a private settings backup without writing')
    checked.add_argument('backup', type=Path)
    staged = commands.add_parser(
        'stage-restore', help='Generate a NEW settings file; never replaces a profile'
    )
    staged.add_argument('backup', type=Path)
    staged.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'verify':
            verify_backup(args.backup)
            print('Private settings backup verified; no profile changed.')
            return 0
        if args.action == 'backup':
            value = backup(args.profile, args.source_root)
        elif args.action == 'verify-transition':
            value = verify_assistant_transition(
                args.from_root,
                args.to_root,
                args.recovery_root,
                args.baseline_apk,
                args.baseline_sha256,
                observation_path=args.observation_report,
            )
        elif args.action == 'stage-restore':
            value = verify_backup(args.backup)
        else:
            value = plan(args.from_root, args.to_root, args.assistant_apk)
        print(output(value, args.output))
        if args.action == 'plan' and (
            not value['declared_protocol_compatible']
            or not value['reminder_storage']['compatible']
            or not value['report_watch_storage']['compatible']
            or not value['identity_storage']['compatible']
        ):
            return 2
        if args.action == 'verify-transition' and args.observation_report is not None:
            return 0 if value['observation_gate']['window_verified'] else 2
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
        RuntimeError,
        ConfigError,
        subprocess.SubprocessError,
    ):
        print(
            'Upgrade preparation failed; verify manifests, owner-private paths and backup integrity. No deployment or profile replacement was performed.',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
