"""Build/install the opt-in native notifier, or inspect/request its permission."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
from desktop_notifications import (  # noqa: E402 - standalone CLI after host path bootstrap
    APP_NAME,
    BUNDLE_ID,
    DesktopNotifier,
    directory_lock,
    install_directory,
    source_hashes,
    verify_bundle,
)


def build(root=ROOT):
    if platform.system() != 'Darwin':
        raise ValueError('Native desktop notifications require macOS')
    stage = root / '.toolchains/cc8/desktop-notifier'
    scratch = root / '.toolchains/cc8/notifier-build'
    with directory_lock(stage):
        expected_sources = source_hashes(root)
        subprocess.run(
            [
                '/usr/bin/swift',
                'build',
                '--package-path',
                str(root / 'mac-notifier'),
                '--scratch-path',
                str(scratch),
            ],
            check=True,
            timeout=180,
        )
        binary_root = subprocess.check_output(
            [
                '/usr/bin/swift',
                'build',
                '--package-path',
                str(root / 'mac-notifier'),
                '--scratch-path',
                str(scratch),
                '--show-bin-path',
            ],
            text=True,
            timeout=20,
        ).strip()
        with tempfile.TemporaryDirectory(prefix='.build-', dir=stage) as temporary:
            bundle = Path(temporary) / APP_NAME
            executable = bundle / 'Contents/MacOS/CC8Notifier'
            executable.parent.mkdir(parents=True, mode=0o700)
            shutil.copyfile(Path(binary_root) / 'CC8Notifier', executable)
            executable.chmod(0o700)
            metadata = {
                'CFBundlePackageType': 'APPL',
                'CFBundleExecutable': 'CC8Notifier',
                'CFBundleIdentifier': BUNDLE_ID,
                'CFBundleName': '大黄蜂提醒',
                'CFBundleDisplayName': '大黄蜂提醒',
                'CFBundleVersion': '1',
                'CFBundleShortVersionString': '1.0',
                'LSMinimumSystemVersion': '12.0',
                'NSPrincipalClass': 'NSApplication',
                'LSUIElement': True,
            }
            (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(metadata))
            (bundle / 'Contents/Info.plist').chmod(0o600)
            subprocess.run(
                [
                    '/usr/bin/codesign',
                    '--force',
                    '--sign',
                    '-',
                    '--identifier',
                    BUNDLE_ID,
                    str(bundle),
                ],
                check=True,
                timeout=20,
            )
            # Include the actual signature envelope in the bounded artifact inventory.
            files = {
                str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in bundle.rglob('*')
                if p.is_file()
            }
            receipt = {'protocol': 1, 'sources': expected_sources, 'files': files}
            receipt_path = Path(temporary) / 'build.json'
            receipt_path.write_text(json.dumps(receipt) + '\n')
            receipt_path.chmod(0o600)
            verify_bundle(Path(temporary), root)
            if (stage / APP_NAME).exists():
                verify_bundle(stage, root, current_sources=False)
                shutil.rmtree(stage / APP_NAME)
            os.replace(bundle, stage / APP_NAME)
            os.replace(receipt_path, stage / 'build.json')
    return stage


def install(stage, root=ROOT):
    verify_bundle(stage, root)
    target = install_directory()
    with directory_lock(target):
        if (target / APP_NAME).exists() or (target / 'build.json').exists():
            verify_bundle(target, root, current_sources=False)
        with tempfile.TemporaryDirectory(prefix='.install-', dir=target) as temporary:
            bundle = Path(temporary) / APP_NAME
            shutil.copytree(stage / APP_NAME, bundle)
            shutil.copyfile(stage / 'build.json', Path(temporary) / 'build.json')
            (Path(temporary) / 'build.json').chmod(0o600)
            verify_bundle(Path(temporary), root)
            if (target / APP_NAME).exists():
                shutil.rmtree(target / APP_NAME)
            os.replace(bundle, target / APP_NAME)
            os.replace(Path(temporary) / 'build.json', target / 'build.json')
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'setup', 'status', 'authorize'))
    parser.add_argument(
        '--development',
        action='store_true',
        help='Status of the local built app only; never grants permission',
    )
    args = parser.parse_args(argv)
    if args.development and args.action != 'status':
        parser.error('--development only supports status')
    try:
        if args.action in ('build', 'setup'):
            stage = build()
            if args.action == 'setup':
                install(stage)
            print(
                json.dumps(
                    {
                        'result': 'installed' if args.action == 'setup' else 'built',
                        'permission_requested': False,
                        'notification_posted': False,
                    }
                )
            )
            return 0
        directory = ROOT / '.toolchains/cc8/desktop-notifier' if args.development else None
        result = DesktopNotifier(directory=directory).request({'action': args.action})
        print(json.dumps(result))
        return 0 if result.get('result') == 'status' else 2
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(
            2,
            'Notifier command failed; no notification content is logged. Check setup and macOS permission.\n',
        )


if __name__ == '__main__':
    raise SystemExit(main())
