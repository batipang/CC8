"""Explicitly install checksum-pinned CI CLIs; never run downloaded archive scripts."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tools/resources/ci-tools.json'


def tool_info(name):
    info = json.loads(MANIFEST.read_text())[name]
    system = platform.system().lower() + '-' + platform.machine().lower()
    if system not in info['platforms']:
        raise RuntimeError('Unsupported CI CLI platform: ' + system)
    asset = info['platforms'][system]
    url = (
        f'https://github.com/{info["repo"]}/releases/download/v{info["version"]}/{asset["archive"]}'
    )
    directory = ROOT / '.toolchains/cc8/quality' / (name + '-' + info['version'])
    return asset, url, directory / asset['archive'], directory / name


def binary(name):
    asset, _, archive, executable = tool_info(name)
    if (
        not archive.is_file()
        or hashlib.sha256(archive.read_bytes()).hexdigest() != asset['sha256']
        or not executable.is_file()
    ):
        raise RuntimeError(f'{name} missing/corrupt; run python tools/ci_tools.py setup {name}')
    # Compare to the authenticated archive, so a changed cached executable is not trusted.
    with tarfile.open(archive, 'r:gz') as source:
        member = source.getmember(name)
        if not member.isfile() or member.size > 100 * 1024 * 1024:
            raise RuntimeError('Unsafe CLI archive member')
        with source.extractfile(member) as payload:
            if (
                hashlib.sha256(payload.read()).digest()
                != hashlib.sha256(executable.read_bytes()).digest()
            ):
                raise RuntimeError(name + ' executable does not match verified archive')
    return executable


def setup(name):
    asset, url, archive, executable = tool_info(name)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ci-tool-', dir=archive.parent) as temporary:
        downloaded = Path(temporary) / 'download.tar.gz'
        with urllib.request.urlopen(url, timeout=30) as response, downloaded.open('wb') as out:
            total = 0
            while block := response.read(65536):
                total += len(block)
                if total > 100 * 1024 * 1024:
                    raise RuntimeError('CLI download exceeds size limit')
                out.write(block)
        if hashlib.sha256(downloaded.read_bytes()).hexdigest() != asset['sha256']:
            raise RuntimeError('CLI download checksum mismatch')
        with tarfile.open(downloaded, 'r:gz') as source:
            member = source.getmember(name)
            if not member.isfile() or member.size > 100 * 1024 * 1024:
                raise RuntimeError('Unsafe CLI archive member')
            target = Path(temporary) / name
            with source.extractfile(member) as payload, target.open('wb') as out:
                shutil.copyfileobj(payload, out)
        target.chmod(0o755)
        downloaded.replace(archive)
        target.replace(executable)
    print(f'{name}: official release archive SHA-256 verified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('setup', 'path'))
    parser.add_argument('tool', choices=('gitleaks', 'actionlint'))
    args = parser.parse_args()
    try:
        if args.action == 'setup':
            setup(args.tool)
        else:
            print(binary(args.tool))
    except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
