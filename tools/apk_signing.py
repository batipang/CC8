"""Verify APK signatures for this SDK-27 device; never read a private key."""

from pathlib import Path
import re
import subprocess


class SignatureError(RuntimeError):
    pass


def signer_fingerprints(apk, java, jar):
    if not Path(java).is_file() or not Path(jar).is_file():
        raise SignatureError('APK verifier missing; run ./cc8 doctor --profile android')
    try:
        result = subprocess.run(
            [
                str(java),
                '-jar',
                str(jar),
                'verify',
                '--verbose',
                '--print-certs',
                '--min-sdk-version',
                '27',
                '--max-sdk-version',
                '27',
                str(apk),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # Tool output can include paths / certificate subjects. Keep diagnostics
        # stable and do not dump arbitrary APK metadata or subprocess arguments.
        raise SignatureError(
            'APK signature verification failed for Android SDK 27; installation was not attempted'
        ) from None
    if len(result.stdout) > 1024 * 1024:
        raise SignatureError('Oversized APK signature report')
    counts = re.findall(r'^Number of signers: (1[0-6]|[1-9])$', result.stdout, re.MULTILINE)
    rows = re.findall(
        r'^Signer #([1-9][0-9]*) certificate SHA-256 digest: ([a-fA-F0-9]{64})$',
        result.stdout,
        re.MULTILINE,
    )
    if len(counts) != 1:
        raise SignatureError('APK signer count could not be verified')
    expected = {str(i) for i in range(1, int(counts[0]) + 1)}
    if len(rows) != len(expected) or {row[0] for row in rows} != expected:
        raise SignatureError('APK signing certificate set could not be verified')
    values = frozenset(row[1].lower() for row in rows)
    if len(values) != len(rows):
        raise SignatureError('Duplicate APK signing certificates')
    return values


def require_same_signers(candidate, installed):
    if not candidate or candidate != installed:
        raise SignatureError(
            'Signing certificate mismatch; installation was not attempted. '
            'Import the original CC8_SIGNING_KEYSTORE and rebuild. '
            'Do not uninstall or clear application data; see docs/SIGNING.md'
        )
