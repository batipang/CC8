"""Offline upgrade planning and explicit non-secret settings backup; never deploy."""

import argparse
from dataclasses import asdict
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


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False
        ).encode()
    ).hexdigest()


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
        ],
    }
    if assistant_apk is not None:
        # Reuse actual AAPT/API-27 signature checks; no adb or installation here.
        from tools.release import check_apk

        value['assistant_apk'] = check_apk('assistant', Path(assistant_apk), root=Path(after_root))
        value['apk_checked'] = True
    return value


def backup(profile, source_root=ROOT):
    # Existing schema rejects secrets, duplicate/unknown fields and public files.
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
        elif args.action == 'stage-restore':
            value = verify_backup(args.backup)
        else:
            value = plan(args.from_root, args.to_root, args.assistant_apk)
        print(output(value, args.output))
        return 2 if args.action == 'plan' and not value['declared_protocol_compatible'] else 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
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
