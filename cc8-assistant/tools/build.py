from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import re
import sys

project = Path(__file__).resolve().parents[1]
root = project.parent
sys.path.insert(0, str(root / 'cc8-assistant/host'))
from configuration import load_config  # noqa: E402 - standalone build entry

config = load_config(root=root)
tools = root / '.toolchains/cc8'
paths = json.loads((tools / 'paths.json').read_text())
env = os.environ.copy()
env.update(
    JAVA_HOME=paths['java_home'],
    ANDROID_HOME=paths['android_sdk'],
    ANDROID_SDK_ROOT=paths['android_sdk'],
    GRADLE_USER_HOME=str(tools / 'gradle-cache'),
)
env['PATH'] = str(Path(paths['java_home']) / 'bin') + os.pathsep + env.get('PATH', '')
(project / 'local.properties').write_text('sdk.dir=' + paths['android_sdk'] + '\n')
signing = root / 'cc8-diagnostics/signing'
signing.mkdir(parents=True, exist_ok=True)
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
            'CN=CC8 Development, O=Personal Project, C=CN',
        ],
        env=env,
        check=True,
    )
(root / 'artifacts').mkdir(exist_ok=True)
subprocess.run(
    [paths['gradle'], '--console=plain', '--no-daemon', 'assembleRelease', 'lintRelease'],
    cwd=project,
    env=env,
    check=True,
)
version = re.search(
    r"versionName\s+['\"]([^'\"]+)['\"]", (project / 'app/build.gradle').read_text()
).group(1)
apk = root / ('artifacts/cc8-assistant-' + version + '.apk')
shutil.copy2(project / 'app/build/outputs/apk/release/app-release.apk', apk)
digest = hashlib.sha256(apk.read_bytes()).hexdigest()
apk.with_suffix('.apk.sha256').write_text(digest + '  ' + apk.name + '\n')
print('APK:', apk, 'SHA256:', digest)
