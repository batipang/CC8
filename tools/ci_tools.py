"""Explicitly install checksum-pinned CI CLIs; never run downloaded archive scripts."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tools/resources/ci-tools.json'
REPOSITORIES = {'actionlint': 'rhysd/actionlint', 'gitleaks': 'gitleaks/gitleaks'}


def tool_info(name):
    info = json.loads(MANIFEST.read_text())[name]
    if info['repo'] != REPOSITORIES[name]:
        raise RuntimeError('CLI must use its pinned official repository')
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


def _download_archive(asset, request, downloaded):
    try:
        with urllib.request.urlopen(request, timeout=30) as response, downloaded.open('wb') as out:
            total = 0
            while block := response.read(65536):
                total += len(block)
                if total > asset.get('size', 100 * 1024 * 1024):
                    raise RuntimeError('CLI download exceeds size limit')
                out.write(block)
        if 'size' in asset and total != asset['size']:
            raise RuntimeError('CLI download size mismatch')
        if hashlib.sha256(downloaded.read_bytes()).hexdigest() != asset['sha256']:
            raise RuntimeError('CLI download checksum mismatch')
    except BaseException:
        downloaded.unlink(missing_ok=True)
        raise


def setup(name):
    asset, url, archive, executable = tool_info(name)
    identity = asset.get('github_asset_id')
    if identity is not None and (type(identity) is not int or not 0 < identity < 2**63):
        raise RuntimeError('Invalid pinned CLI asset identity')
    if 'size' in asset and (
        type(asset['size']) is not int or not 0 < asset['size'] <= 100 * 1024 * 1024
    ):
        raise RuntimeError('Invalid pinned CLI asset size')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ci-tool-', dir=archive.parent) as temporary:
        downloaded = Path(temporary) / 'download.tar.gz'
        try:
            _download_archive(asset, url, downloaded)
        except urllib.error.HTTPError as error:
            if not 500 <= error.code <= 599 or identity is None:
                raise
            error.close()
            print('CLI release unavailable; trying the pinned official asset API once.')
            request = urllib.request.Request(
                f'https://api.github.com/repos/{REPOSITORIES[name]}/releases/assets/{identity}',
                headers={
                    'Accept': 'application/octet-stream',
                    'X-GitHub-Api-Version': '2022-11-28',
                },
            )
            _download_archive(asset, request, downloaded)
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
