"""Explicit SDK integration check using a built APK and disposable test key.

No device access, installed apps, original signing private keys, or capture.
Run in Android CI after the real build, not in the SDK-free offline suite.
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from apk_signing import signer_fingerprints, require_same_signers, SignatureError
from java_toolchain import require_java, ROOT


def check(apk, java, jar):
    original = signer_fingerprints(apk, java, jar)
    require_same_signers(original, original)
    with tempfile.TemporaryDirectory(prefix='cc8-signature-test-') as folder:
        directory = Path(folder)
        key = directory / 'disposable.jks'
        alternative = directory / 'alternative.apk'
        shutil.copy2(apk, alternative)
        commands = [
            [
                str(java.parent / 'keytool'),
                '-genkeypair',
                '-keystore',
                str(key),
                '-storepass',
                'android',
                '-keypass',
                'android',
                '-alias',
                'fixture',
                '-keyalg',
                'RSA',
                '-keysize',
                '2048',
                '-validity',
                '2',
                '-dname',
                'CN=Disposable CC8 Test',
            ],
            [
                str(java),
                '-jar',
                str(jar),
                'sign',
                '--ks',
                str(key),
                '--ks-key-alias',
                'fixture',
                '--ks-pass',
                'pass:android',
                '--key-pass',
                'pass:android',
                str(alternative),
            ],
        ]
        for command in commands:
            subprocess.run(command, check=True, capture_output=True, timeout=30)
        other = signer_fingerprints(alternative, java, jar)
        try:
            require_same_signers(original, other)
        except SignatureError:
            pass
        else:
            raise RuntimeError('Different test certificates were not rejected')
        tampered = directory / 'tampered.apk'
        data = bytearray(apk.read_bytes())
        data[100] ^= 1
        tampered.write_bytes(data)
        try:
            signer_fingerprints(tampered, java, jar)
        except SignatureError:
            pass
        else:
            raise RuntimeError('Modified signed APK was not rejected')
    print(
        'PASS: real SDK-27 signature, same-certificate match, different-key rejection and tamper rejection; no device accessed'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('apk', type=Path)
    args = parser.parse_args()
    try:
        paths = json.loads((ROOT / '.toolchains/cc8/paths.json').read_text())
        java = require_java() / 'java'
        jar = Path(paths['android_sdk']) / 'build-tools/35.0.0/lib/apksigner.jar'
        check(args.apk, java, jar)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        parser.exit(
            1, 'APK signature integration check failed; no device or original key was used\n'
        )


if __name__ == '__main__':
    main()
