"""Build a private native window-picker bundle; never launch it or request capture permission."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
from window_input import source_hashes  # noqa: E402 - standalone build entry


def build(output):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = output.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError('Window helper output must be an owner-private directory')
    sources = source_hashes()
    subprocess.run(
        ['/usr/bin/xcrun', 'swift', 'build', '--package-path', str(ROOT / 'mac-window-input')],
        check=True,
    )
    path = subprocess.check_output(
        [
            '/usr/bin/xcrun',
            'swift',
            'build',
            '--package-path',
            str(ROOT / 'mac-window-input'),
            '--show-bin-path',
        ],
        text=True,
    ).strip()
    # Reject replacement of an existing helper. The explicit maintenance path
    # owns upgrades; building this feature cannot race a live selection.
    destination = output / 'CC8WindowInput.app'
    if destination.exists() or (output / 'build.json').exists():
        raise ValueError('Choose a fresh private output directory for this build')
    with tempfile.TemporaryDirectory(prefix='.window-build-', dir=output) as temporary:
        bundle = Path(temporary) / 'CC8WindowInput.app'
        executable = bundle / 'Contents/MacOS/cc8-window-picker'
        executable.parent.mkdir(parents=True, mode=0o700)
        shutil.copyfile(Path(path) / 'cc8-window-picker', executable)
        executable.chmod(0o700)
        plist = dict(
            CFBundleIdentifier='com.happyhome.cc8windowinput',
            CFBundleName='大黄蜂窗口输入',
            CFBundleExecutable='cc8-window-picker',
            CFBundlePackageType='APPL',
            CFBundleShortVersionString='1.0',
            CFBundleVersion='1',
            LSMinimumSystemVersion='15.2',
            NSHighResolutionCapable=True,
            NSScreenCaptureUsageDescription='仅获取你在系统面板选定、预览并明确发送的单个窗口画面。',
        )
        (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(plist))
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(bundle)], check=True)
        for folder, dirs, files in os.walk(bundle):
            Path(folder).chmod(0o700)
            for name in files:
                if Path(folder) / name != executable:
                    (Path(folder) / name).chmod(0o600)
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(bundle)], check=True)
        if source_hashes() != sources:
            raise RuntimeError('Window helper source changed during build')
        record = dict(
            protocol=1, sha256=hashlib.sha256(executable.read_bytes()).hexdigest(), sources=sources
        )
        bundle.rename(destination)
        manifest = output / 'build.json'
        fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream)
            stream.write('\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.toolchains/cc8/window-input')
    args = parser.parse_args()
    print(json.dumps(build(args.output)))


if __name__ == '__main__':
    main()
