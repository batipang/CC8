"""Install a local APK through this CC8's stock ADB command path.

The stock adbd checks command prefixes. Reuse its engineering diagnostics
prefix to run ordinary shell-UID package-manager commands. No root, partition
writes, bootloader changes, or persistent debug-property changes are used.
--probe runs only identity/capability checks; --check never contacts ADB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.toolchains/cc8/paths.json'
SDK = Path(json.loads(CONFIG.read_text())['android_sdk']) if CONFIG.is_file() else ROOT / '.toolchains/cc8/android-sdk'
ADB = SDK / 'platform-tools/adb'
AAPT = SDK / 'build-tools/35.0.0/aapt'
PREFIX = 'dumpsys activity com.ailabs.emode >/dev/null 2>&1; '


def helper_artifact() -> bytes:
    helper = ROOT / 'artifacts/cc8-installer-shell-1.0.jar'
    metadata = json.loads(helper.with_suffix('.json').read_text())
    payload = helper.read_bytes()
    source = ROOT / 'device-access/installer-helper/Cc8Installer.java'
    if hashlib.sha256(payload).hexdigest() != metadata['sha256'] or hashlib.sha256(source.read_bytes()).hexdigest() != metadata['source_sha256']:
        raise ValueError('Installer helper/source hash mismatch; rebuild with device-access/installer-helper/build.py')
    return payload


def private_staging(transport: str) -> str:
    # Shared /sdcard storage lets other apps replace a verified helper before
    # app_process loads it. Require a directory writable only by shell/root.
    directory = '/data/local/tmp'
    try:
        fields = shell(transport, "stat -c '%u %g %a' " + directory).split()
        if len(fields) != 3:raise ValueError('Invalid stat fields')
        uid, gid = int(fields[0]), int(fields[1])
        mode = int(fields[2], 8)
    except (ValueError, RuntimeError):
        raise RuntimeError('Cannot verify protected shell staging directory') from None
    if uid != 2000 or mode & 0o002 or (mode & 0o020 and gid != 2000) or mode & 0o300 != 0o300:
        raise RuntimeError('Unsafe permissions on shell staging directory')
    return directory


def install_stream(transport: str, package: str, payload: bytes, digest: str, shared_staging: bool = False):
    helper = helper_artifact()
    helper_hash = hashlib.sha256(helper).hexdigest()
    directory = '/sdcard/Download' if shared_staging else private_staging(transport)
    if shared_staging:
        print('Shared staging explicitly selected: other storage-enabled apps may replace the temporary helper.', flush=True)
    remote = directory + '/.cc8-installer-' + uuid.uuid4().hex + '.jar'
    quoted = shlex.quote(remote)
    try:
        try:
            shell(transport, 'umask 077; cat > ' + quoted, helper)
        except RuntimeError as error:
            if not shared_staging and 'Permission denied' in str(error):
                raise RuntimeError('Protected staging is blocked by device policy; APK installation was not attempted. See docs/APK_INSTALL.md for the explicit --shared-staging compatibility mode and its risks.') from None
            raise
        if not shared_staging:shell(transport, 'chmod 400 ' + quoted)
        received = shell(transport, 'sha256sum ' + quoted).split()
        if not received or received[0] != helper_hash:
            raise RuntimeError('Transferred installer helper hash mismatch')
        command = ('CLASSPATH=' + quoted + ' app_process /system/bin Cc8Installer --install ' +
                   shlex.quote(package) + ' ' + str(len(payload)) + ' ' + digest)
        output = shell(transport, command, payload, timeout=90)
        if 'CC8_APK_STREAM_VERIFIED bytes=' + str(len(payload)) not in output or 'CC8_PACKAGE_INSTALLED ' + package + ' ' not in output:
            raise RuntimeError('System installer completion was not verified: ' + output[:500])
        print('APK stream hash and system install callback verified.', flush=True)
    finally:
        try:
            shell(transport, 'rm -f ' + quoted)
        except (RuntimeError, subprocess.TimeoutExpired):
            print('Temporary helper cleanup could not be verified:', remote, flush=True)


def target() -> str:
    result = subprocess.run([str(ADB), 'devices', '-l'], capture_output=True, text=True, check=True, timeout=15)
    matches = []
    for line in result.stdout.splitlines():
        if not re.match(r'^\S*[:.]', line) and re.search(r'^\S+\s+device\s', line) and ' model:AILABS_S6 ' in ' ' + line + ' ':
            match = re.search(r'\btransport_id:(\d+)\b', line)
            if match:
                matches.append(match.group(1))
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one online USB AILABS_S6 in normal Android mode; check USB data cable and engineering mode.')
    return matches[0]


def shell(transport: str, command: str, data: bytes | None = None, timeout=30) -> str:
    result = subprocess.run([str(ADB), '-t', transport, 'shell', '-T', PREFIX + command],
                            input=data if data is not None else b'', capture_output=True, timeout=timeout)
    output = result.stdout.decode(errors='replace')
    if result.returncode:
        raise RuntimeError(f'CC8 shell command failed ({result.returncode}): ' +
                           (result.stderr.decode(errors='replace') + output)[:500])
    return output


def probe(transport: str) -> dict:
    output = shell(transport, "printf 'CC8_SHELL_OK\\n'; id -u; getprop ro.product.model; getprop ro.build.version.sdk")
    lines = output.splitlines()
    if len(lines) != 4 or lines[0] != 'CC8_SHELL_OK' or lines[2] != 'AILABS_S6' or lines[3] != '27' or lines[1] != '2000':
        raise RuntimeError('Engineering command path or device identity is not verified: ' + repr(output[:180]))
    return {'shell_verified': True, 'uid': int(lines[1]), 'model': lines[2], 'sdk': int(lines[3])}


def install(transport: str, apk: Path, launch: bool, shared_staging: bool = False) -> dict:
    apk = apk.resolve()
    payload = apk.read_bytes()
    metadata = subprocess.check_output([str(AAPT), 'dump', 'badging', str(apk)], text=True, timeout=15)
    package = re.search(r"^package: name='([A-Za-z][A-Za-z0-9_.]+)'", metadata, re.M)
    if not package:
        raise ValueError('Cannot read a safe Android package name from APK')
    package = package.group(1)
    activity = re.search(r"^launchable-activity: name='([A-Za-z][A-Za-z0-9_.$]+)'", metadata, re.M)
    if launch and not activity:
        raise ValueError('APK does not declare a launchable activity')
    digest = hashlib.sha256(payload).hexdigest()
    report = {'package': package, 'apk_sha256': digest, 'installed': False, 'launched': False,
              'firmware_written': False, 'bootloader_modified': False,
              'helper_staging': 'shared_compatibility' if shared_staging else 'protected_shell_directory'}
    # The shipped pm command advertises installation but rejects it at runtime.
    # The temporary helper calls the stock PackageInstaller API at shell UID.
    install_stream(transport, package, payload, digest, shared_staging=shared_staging)
    installed_path = shell(transport, 'pm path ' + shlex.quote(package)).strip()
    if not installed_path.startswith('package:'):
        raise RuntimeError('Package manager could not locate the installed package')
    report['installed'] = True
    report['installation_method'] = 'stock_package_installer_API_via_temporary_shell_UID_helper'
    paths = [line[len('package:'):] for line in installed_path.splitlines() if line.startswith('package:')]
    if len(paths)!=1 or not paths[0].startswith('/'):
        raise RuntimeError('APK installed but its single base APK path was not verified')
    try:
        remote_digest = shell(transport, 'sha256sum ' + shlex.quote(paths[0])).split()
    except RuntimeError:
        raise RuntimeError('APK installed but reading its SHA-256 failed; verification incomplete') from None
    if not remote_digest or remote_digest[0] != digest:
        raise RuntimeError('APK installed but its SHA-256 is missing or differs from the local signed APK')
    report['installed_apk_hash_verified'] = True
    if launch:
        component = package + '/' + activity.group(1)
        output = shell(transport, 'input keyevent 224; am start -W -n ' + shlex.quote(component))
        if 'Error:' in output or 'Exception' in output:
            report['launch_error'] = output[:500]
        else:
            report['launch_command_accepted'] = True
            foreground = shell(transport, 'dumpsys activity activities')
            report['launched'] = any(re.search(r'\b(?:mResumedActivity|ResumedActivity)\s*[:=]', line) and package + '/' in line
                                     for line in foreground.splitlines())
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--probe', action='store_true')
    mode.add_argument('--install', type=Path, metavar='APK')
    parser.add_argument('--launch', action='store_true')
    parser.add_argument('--shared-staging', action='store_true',
                        help='Explicit legacy shared-directory mode; other storage-enabled apps may replace the helper. See docs/APK_INSTALL.md')
    args = parser.parse_args()
    if (args.launch or args.shared_staging) and not args.install:
        parser.error('--launch and --shared-staging require --install APK')
    if not ADB.is_file() or not AAPT.is_file():
        raise SystemExit('Local Android tools are missing')
    if args.check:
        helper_artifact()
        print('PASS: local ADB/AAPT and installer helper/source hashes; no device commands.')
        raise SystemExit(0)
    try:
        transport = target()
        report = probe(transport)
        print(json.dumps(report), flush=True)
        if args.install:
            report.update(install(transport, args.install, args.launch, shared_staging=args.shared_staging))
            destination = ROOT / 'device-access/private/apk-install-result.json'
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, indent=2), flush=True)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
