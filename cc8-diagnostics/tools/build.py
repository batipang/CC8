from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys

project = Path(__file__).resolve().parents[1]
workspace = project.parent
sys.path.insert(0, str(workspace / 'cc8-assistant/host'))
from configuration import load_config  # noqa: E402 - standalone build entry
from release_info import RELEASE, source_stamp, write_build_receipt  # noqa: E402

config = load_config(root=workspace)
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
key = (config.signing_keystore or signing / 'cc8-diagnostics-debug.jks').resolve()
if config.signing_keystore is not None and not key.is_file():
    raise SystemExit('Configured signing keystore is missing; no replacement key will be generated')
env['CC8_SIGNING_KEYSTORE'] = str(key)
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
stamp = source_stamp(workspace)
subprocess.run(
    [paths['gradle'], '--console=plain', '--no-daemon'] + tasks, cwd=project, env=env, check=True
)
apk = project / 'app/build/outputs/apk/release/app-release.apk'
if apk.exists() and '--prepare' not in sys.argv:
    output = workspace / 'artifacts'
    output.mkdir(exist_ok=True)
    version = RELEASE.diagnostics.version
    final = output / ('cc8-diagnostics-' + version + '.apk')
    shutil.copy2(apk, final)
    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    final.with_suffix(final.suffix + '.sha256').write_text(digest + '  ' + final.name + '\n')
    write_build_receipt(final, 'diagnostics', stamp, workspace)
    print('APK: ' + str(final))
    print('SHA-256: ' + digest)
