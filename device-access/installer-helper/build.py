"""Build the small temporary shell installer using the task's local Android SDK."""

from pathlib import Path
import hashlib
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[2]
paths = json.loads((ROOT / '.toolchains/cc8/paths.json').read_text())
build = ROOT / '.toolchains/cc8/installer-helper'
classes = build / 'classes'
classes.mkdir(parents=True, exist_ok=True)
sdk = Path(paths['android_sdk'])
java = Path(paths['java_home']) / 'bin'
android = sdk / 'platforms/android-35/android.jar'
source = Path(__file__).with_name('Cc8Installer.java')
subprocess.run(
    [
        str(java / 'javac'),
        '-source',
        '8',
        '-target',
        '8',
        '-classpath',
        str(android),
        '-d',
        str(classes),
        str(source),
    ],
    check=True,
)
subprocess.run(
    [str(java / 'jar'), 'cf', str(build / 'classes.jar'), '-C', str(classes), '.'], check=True
)
env = os.environ.copy()
env['JAVA_HOME'] = paths['java_home']
artifact = ROOT / 'artifacts/cc8-installer-shell-1.0.jar'
artifact.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(
    [
        str(sdk / 'build-tools/35.0.0/d8'),
        '--min-api',
        '27',
        '--lib',
        str(android),
        '--output',
        str(artifact),
        str(build / 'classes.jar'),
    ],
    check=True,
    env=env,
)
report = {
    'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
    'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
    'bytes': artifact.stat().st_size,
}
artifact.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
