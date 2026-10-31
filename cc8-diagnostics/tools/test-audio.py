from pathlib import Path
import json,subprocess,tempfile
root=Path(__file__).resolve().parents[1]
paths=json.loads((root.parent/'.toolchains/cc8/paths.json').read_text())
java=Path(paths['java_home'])/'bin'
source=root/'app/src/main/java/com/happyhome/cc8diagnostics'
with tempfile.TemporaryDirectory(prefix='cc8-host-tests-') as temporary:
 subprocess.run([str(java/'javac'),'-encoding','UTF-8','-d',temporary,str(source/'AudioMath.java'),str(source/'WavCodec.java'),str(root/'tests/HostTests.java')],check=True)
 subprocess.run([str(java/'java'),'-cp',temporary,'com.happyhome.cc8diagnostics.HostTests'],check=True)
