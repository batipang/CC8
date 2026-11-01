from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

project = Path(__file__).resolve().parents[1]
workspace = project.parent
tools = workspace / '.toolchains/cc8'
paths = json.loads((tools / 'paths.json').read_text())
env = os.environ.copy()
env['JAVA_HOME'] = paths['java_home']
env['ANDROID_HOME'] = paths['android_sdk']
env['ANDROID_SDK_ROOT'] = paths['android_sdk']
env['GRADLE_USER_HOME'] = str(tools / 'gradle-cache')
env['PATH'] = str(Path(paths['java_home']) / 'bin') + os.pathsep + env.get('PATH', '')
(project / 'local.properties').write_text('sdk.dir=' + paths['android_sdk'] + '\n')
signing = project / 'signing'
signing.mkdir(exist_ok=True)
key = signing / 'cc8-diagnostics-debug.jks'
if not key.exists():
    subprocess.run(
        [
            str(Path(paths['java_home']) / 'bin/keytool'),
            '-genkeypair',
            '-keystore',
            str(key),
            '-storepass',
            'android',
            '-keypass',
            'android',
            '-alias',
            'cc8-diagnostics',
            '-keyalg',
            'RSA',
            '-keysize',
            '2048',
            '-validity',
            '10000',
            '-dname',
            'CN=CC8 Diagnostics Development, O=Personal Project, C=CN',
        ],
        env=env,
        check=True,
    )
if '--prepare' in sys.argv:
    tasks = ['wrapper', '--gradle-version', '8.11.1', '--distribution-type', 'bin']
else:
    tasks = sys.argv[1:] or ['assembleRelease', 'lintRelease']
subprocess.run(
    [paths['gradle'], '--console=plain', '--no-daemon'] + tasks, cwd=project, env=env, check=True
)
apk = project / 'app/build/outputs/apk/release/app-release.apk'
if apk.exists() and '--prepare' not in sys.argv:
    output = workspace / 'artifacts'
    output.mkdir(exist_ok=True)
    version = re.search(
        r"versionName\s+['\"]([^'\"]+)['\"]", (project / 'app/build.gradle').read_text()
    ).group(1)
    final = output / ('cc8-diagnostics-' + version + '.apk')
    shutil.copy2(apk, final)
    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    final.with_suffix(final.suffix + '.sha256').write_text(digest + '  ' + final.name + '\n')
    print('APK: ' + str(final))
    print('SHA-256: ' + digest)
