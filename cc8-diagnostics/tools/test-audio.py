"""Compile real platform-free audio helpers using a normal JDK, no Android SDK."""

from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.java_toolchain import require_java  # noqa: E402 - standalone repository script


def main():
    java = require_java()
    project = ROOT / 'cc8-diagnostics'
    source = project / 'app/src/main/java/com/happyhome/cc8diagnostics'
    with tempfile.TemporaryDirectory(prefix='cc8-host-tests-') as temporary:
        subprocess.run(
            [
                str(java / 'javac'),
                '-encoding',
                'UTF-8',
                '-d',
                temporary,
                str(source / 'AudioMath.java'),
                str(source / 'ChannelMath.java'),
                str(source / 'ReferenceMatch.java'),
                str(source / 'RamPcmLease.java'),
                str(source / 'WavCodec.java'),
                str(project / 'tests/HostTests.java'),
            ],
            check=True,
            timeout=30,
        )
        subprocess.run(
            [str(java / 'java'), '-cp', temporary, 'com.happyhome.cc8diagnostics.HostTests'],
            check=True,
            timeout=15,
        )


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
