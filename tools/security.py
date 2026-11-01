"""Online dependency audits and redacted Git-history secret scan; no device/model access."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

if __package__:
    from .ci_tools import binary
else:
    from ci_tools import binary

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = (
    'requirements-dev.txt',
    'requirements-security.txt',
    'cc8-assistant/host/requirements-runtime.lock',
)


def audit(run=subprocess.run):
    directory = ROOT / 'artifacts/security'
    directory.mkdir(parents=True, exist_ok=True)
    failed = False
    for index, requirements in enumerate(REQUIREMENTS):
        report = directory / f'audit-{index}.json'
        report.unlink(missing_ok=True)
        # Each environment has its own lock; do not merge conflicting versions.
        result = run(
            [
                sys.executable,
                '-m',
                'pip_audit',
                '--no-deps',
                '--disable-pip',
                '--progress-spinner',
                'off',
                '--format',
                'json',
                '--output',
                str(report),
                '-r',
                requirements,
            ],
            cwd=ROOT,
            timeout=300,
        )
        if result.returncode:
            failed = True
        if not report.is_file():
            print(f'{requirements}: audit report missing', file=sys.stderr)
            failed = True
            continue
        dependencies = json.loads(report.read_text())['dependencies']
        skipped = [d['name'] for d in dependencies if d.get('skip_reason')]
        vulnerabilities = sum(len(d.get('vulns', [])) for d in dependencies)
        if vulnerabilities:
            failed = True
        print(
            f'{requirements}: {len(dependencies)} packages, {vulnerabilities} advisories, '
            f'{len(skipped)} skipped',
            flush=True,
        )
        if skipped:
            print('Unscannable packages: ' + ', '.join(skipped), file=sys.stderr)
            failed = True
    return 1 if failed else 0


def secrets(run=subprocess.run):
    return run(
        [
            str(binary('gitleaks')),
            'git',
            str(ROOT),
            '--redact=100',
            '--no-banner',
            '--log-level',
            'warn',
            '--log-opts=--all',
            '--timeout=180',
        ],
        cwd=ROOT,
        timeout=200,
    ).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('audit', 'secrets'))
    args = parser.parse_args()
    try:
        return audit() if args.action == 'audit' else secrets()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
