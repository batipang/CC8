"""Build an isolated, capture-disabled QA APK; never installs or changes production files."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

if __package__:
    from .private_output import PrivateOutput, private_directory
else:
    from private_output import PrivateOutput, private_directory

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'com.happyhome.cc8assistant.qa'
COMPONENT = PACKAGE + '/com.happyhome.cc8assistant.MainActivity'
PORT = 28765
GRADLE = 'cc8-assistant/app/build.gradle'
MANIFEST = 'cc8-assistant/app/src/main/AndroidManifest.xml'
SESSION = 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/VoiceSession.java'
FORBIDDEN = frozenset(('RECORD_AUDIO', 'CAMERA', 'SYSTEM_ALERT_WINDOW'))


def snapshot_files(root=ROOT):
    raw = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root, timeout=10)
    if len(raw) > 1024 * 1024:
        raise ValueError('Source inventory exceeds bound')
    names = raw.decode().rstrip('\0').split('\0')
    selected = [
        name
        for name in names
        if name
        in (
            'release.json',
            GRADLE,
            'cc8-assistant/build.gradle',
            'cc8-assistant/settings.gradle',
            'cc8-assistant/gradle.properties',
        )
        or name.startswith('cc8-assistant/app/src/')
    ]
    if not {GRADLE, SESSION, MANIFEST, 'release.json'}.issubset(selected):
        raise ValueError('Missing required source inputs')
    return selected


def safe_source(root, name):
    path = root / name
    if Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('Invalid source path')
    if any((root / part).is_symlink() for part in Path(name).parents):
        raise ValueError('Linked source directory')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 0 < info.st_size <= 8 * 1024**2
        ):
            raise ValueError('Unsafe source input')
        data = source.read(8 * 1024**2 + 1)
        if len(data) != info.st_size:
            raise ValueError('Source changed during snapshot')
    return data


def variant(name, data):
    if name not in (GRADLE, MANIFEST, SESSION):
        return data
    value = data.decode()
    changes = {
        GRADLE: [("applicationId 'com.happyhome.cc8assistant'", "applicationId '" + PACKAGE + "'")],
        SESSION: [
            (
                'new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 18765)',
                'new InetSocketAddress(InetAddress.getByName("127.0.0.1"), ' + str(PORT) + ')',
            )
        ],
        MANIFEST: [
            ('android:label="大黄蜂 · 个人助理"', 'android:label="大黄蜂 · 自动故障测试"'),
            *[
                ('<uses-permission android:name="android.permission.' + permission + '"/>', '')
                for permission in sorted(FORBIDDEN)
            ],
        ],
    }
    if name == MANIFEST and 'sharedUserId' in value:
        raise ValueError('QA must not share a UID with another package')
    for before, after in changes[name]:
        if value.count(before) != 1:
            raise ValueError('QA patch context changed')
        value = value.replace(before, after)
    return value.encode()


def snapshot(output, root=ROOT):
    # Snapshot only versioned application inputs, never .gradle, build, signing,
    # credentials or private runtime files. A dirty source is explicitly recorded.
    names = snapshot_files(root)
    data, total = {}, 0
    for name in names:
        value = safe_source(root, name)
        total += len(value)
        if total > 16 * 1024**2:
            raise ValueError('QA source snapshot too large')
        data[name] = value
    output.mkdir(mode=0o700)
    original, patched = {}, {}
    for name, value in data.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = variant(name, value)
        path.write_bytes(rendered)
        path.chmod(0o600)
        original[name] = hashlib.sha256(value).hexdigest()
        patched[name] = hashlib.sha256(rendered).hexdigest()
    if any(safe_source(root, n) != value for n, value in data.items()):
        raise ValueError('Source changed during snapshot')
    return original, patched


def apk_metadata(apk, aapt):
    text = subprocess.check_output([str(aapt), 'dump', 'badging', str(apk)], text=True, timeout=20)
    package = re.search(
        r"^package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'", text, re.M
    )
    permissions = set(re.findall(r"^uses-permission: name='([^']+)'", text, re.M))
    if (
        not package
        or package[1] != PACKAGE
        or any('android.permission.' + p in permissions for p in FORBIDDEN)
    ):
        raise ValueError('QA APK package or capture permissions are unsafe')
    tree = subprocess.check_output(
        [str(aapt), 'dump', 'xmltree', str(apk), 'AndroidManifest.xml'], text=True, timeout=20
    )
    if 'sharedUserId' in tree or any('android.permission.' + p in tree for p in FORBIDDEN):
        raise ValueError('QA APK declares shared UID')
    return {
        'package': package[1],
        'version_code': int(package[2]),
        'version': package[3],
        'capture_permissions_absent': True,
        'shared_uid_absent': True,
    }


def build(output, keystore, root=ROOT):
    if not keystore.is_absolute() or not keystore.is_file():
        raise ValueError('An existing signing key reference is required; no key is generated')
    before = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=root, text=True, timeout=10
    ).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, timeout=10))
    original, patched = snapshot(output, root)
    config = json.loads((root / '.toolchains/cc8/paths.json').read_text())
    for name in ('java_home', 'android_sdk', 'gradle'):
        if (
            not isinstance(config.get(name), str)
            or not Path(config[name]).is_absolute()
            or any(c in config[name] for c in '\r\n\0')
        ):
            raise ValueError('Invalid local development tool reference')
    env = dict(os.environ)
    env.update(
        JAVA_HOME=config['java_home'],
        ANDROID_HOME=config['android_sdk'],
        ANDROID_SDK_ROOT=config['android_sdk'],
        CC8_SIGNING_KEYSTORE=str(keystore),
        GRADLE_USER_HOME=str(root / '.toolchains/cc8/gradle-cache'),
    )
    env['PATH'] = str(Path(config['java_home']) / 'bin') + os.pathsep + env.get('PATH', '')
    project = output / 'cc8-assistant'
    (project / 'local.properties').write_text('sdk.dir=' + config['android_sdk'] + '\n')
    with open(output / 'build.log', 'x', opener=lambda p, f: os.open(p, f, 0o600)) as log:
        subprocess.run(
            [
                config['gradle'],
                '--offline',
                '--console=plain',
                '--no-daemon',
                'assembleRelease',
                'lintRelease',
            ],
            cwd=project,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=300,
        )
    apk = output / 'cc8-assistant-qa.apk'
    shutil.copyfile(project / 'app/build/outputs/apk/release/app-release.apk', apk)
    apk.chmod(0o600)
    metadata = apk_metadata(apk, Path(config['android_sdk']) / 'build-tools/35.0.0/aapt')
    if subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=root, text=True, timeout=10
    ).strip() != before or any(
        hashlib.sha256(safe_source(root, name)).hexdigest() != digest
        for name, digest in original.items()
    ):
        raise ValueError('Source changed during QA build')
    receipt = {
        'schema': 1,
        'kind': 'cc8_isolated_qa_apk',
        'source_commit': before,
        'source_dirty': dirty,
        'input_sha256': original,
        'variant_sha256': patched,
        'apk_sha256': hashlib.sha256(apk.read_bytes()).hexdigest(),
        'device_port': PORT,
        'component': COMPONENT,
        **metadata,
        'installed': False,
        'production_files_changed': False,
        'real_provider_tested': False,
        'full_p0_acceptance': False,
    }
    with PrivateOutput(output / 'qa-build.json') as out:
        out.write(receipt)
    return receipt


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--keystore', type=Path)
    p.add_argument('--execute', action='store_true')
    args = p.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    'execute': False,
                    'qa_package': PACKAGE,
                    'device_port': PORT,
                    'capture_permissions_removed': sorted(FORBIDDEN),
                    'production_changed': False,
                }
            )
        )
        return 0
    try:
        if (
            args.keystore is None
            or not args.output.is_absolute()
            or args.output.resolve() != args.output
        ):
            raise ValueError('Absolute new output and existing keystore reference required')
        private_directory(args.output.parent)
        result = build(args.output, args.keystore)
        print(
            json.dumps(
                {
                    k: result[k]
                    for k in ('package', 'version', 'version_code', 'apk_sha256', 'installed')
                }
            )
        )
        print(args.output / 'qa-build.json')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            '隔离QA构建失败；核对固定源码、现有工具/原签名引用和私有构建日志。',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
