"""Download verified portable build tools into this workspace only."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / '.toolchains/cc8'
if platform.system() != 'Darwin' or platform.machine() != 'arm64':
    raise SystemExit('Bundled bootstrap requires Apple Silicon macOS; configure existing tools instead.')
TOOLS.mkdir(parents=True, exist_ok=True)

def curl(url, destination):
    subprocess.run(['curl','-fLsS','--retry','2','--connect-timeout','20','--max-time','600','-o',str(destination),url],check=True)

def checksum(url):
    return subprocess.check_output(['curl','-fLsS','--retry','2','--max-time','30',url],text=True).split()[0].strip()

def get(name,url,expected):
    path = TOOLS/name
    if not path.exists():
        print('Downloading '+name,flush=True)
        partial = path.with_suffix(path.suffix+'.partial')
        curl(url,partial)
        partial.rename(path)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual.lower() != expected.lower():
        raise RuntimeError('Checksum mismatch for '+name)
    print('Verified '+name,flush=True)
    return path

def unzip(archive,destination):
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            target = (destination/entry.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError('Unsafe archive path')
        z.extractall(destination)
        for entry in z.infolist():
            mode = entry.external_attr >> 16
            target = destination/entry.filename
            if mode and target.exists() and not target.is_dir(): target.chmod(mode & 0o777)

def jdk():
    sha = 'f4ee9236ffc3ddcd37da877e04a1ab74b3d3376cf9e4287b5426f4749cc869a9'
    archive = get('corretto-21-macos-arm64.tar.gz','https://corretto.aws/downloads/resources/21.0.13.0.11/amazon-corretto-21.0.13.0.11-macosx-aarch64.tar.gz',sha)
    folder = TOOLS/'jdk'
    if not list(folder.glob('*/Contents/Home/bin/java')):
        folder.mkdir(exist_ok=True)
        with tarfile.open(archive) as t:
            for entry in t.getmembers():
                target=(folder/entry.name).resolve()
                if not target.is_relative_to(folder.resolve()): raise RuntimeError('Unsafe archive path')
                if entry.issym() and not (target.parent/entry.linkname).resolve().is_relative_to(folder.resolve()): raise RuntimeError('Unsafe archive link')
                if entry.islnk() and not (folder/entry.linkname).resolve().is_relative_to(folder.resolve()): raise RuntimeError('Unsafe archive link')
            t.extractall(folder)
    homes = list(folder.glob('*/Contents/Home'))
    if len(homes) != 1: raise RuntimeError('Cannot resolve Java home')
    return 'java_home',str(homes[0])

def sdk():
    archive = get('android-commandline-tools.zip','https://dl.google.com/android/repository/commandlinetools-mac_arm64-15859902_latest.zip','835b62a26162b229b441d1f6d4680383815a270809eb33522c0d480fa5002c4e')
    root = TOOLS/'android-sdk'
    target = root/'cmdline-tools/latest'
    if not target.exists():
        staging = TOOLS/'sdk-staging'
        staging.mkdir(exist_ok=True)
        unzip(archive,staging)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.move(str(staging/'cmdline-tools'),str(target))
    return 'android_sdk',str(root)

def gradle():
    url='https://downloads.gradle.org/distributions/gradle-8.11.1-bin.zip'
    archive=get('gradle-8.11.1-bin.zip',url,'f397b287023acdba1e9f6fc5ea72d22dd63669d59ed4a289a29b1a76eee151c6')
    if not (TOOLS/'gradle-8.11.1').exists(): unzip(archive,TOOLS)
    return 'gradle',str(TOOLS/'gradle-8.11.1/bin/gradle')

with ThreadPoolExecutor(max_workers=3) as executor:
    results=dict(executor.map(lambda f:f(),[jdk,sdk,gradle]))
(TOOLS/'paths.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps(results,indent=2),flush=True)
