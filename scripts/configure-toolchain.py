"""Point this checkout at an existing JDK 21 / Android SDK / Gradle 8.11.1."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java-home', required=True, type=Path)
    parser.add_argument('--android-sdk', required=True, type=Path)
    parser.add_argument('--gradle', required=True, type=Path)
    args = parser.parse_args()
    paths = {name: str(getattr(args, name).expanduser().resolve())
             for name in ('java_home', 'android_sdk', 'gradle')}
    java, sdk = Path(paths['java_home']), Path(paths['android_sdk'])
    required = [java/'bin/java', java/'bin/javac', java/'bin/keytool',
                sdk/'platform-tools/adb', sdk/'build-tools/35.0.0/aapt',
                sdk/'build-tools/35.0.0/d8', sdk/'platforms/android-35/android.jar',
                Path(paths['gradle'])]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        parser.error('Missing required tool files: ' + ', '.join(missing))
    destination = ROOT/'.toolchains/cc8/paths.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(paths, indent=2)+'\n')
    print('Configured local toolchain; paths.json is ignored by Git.')


if __name__ == '__main__':
    main()
