from pathlib import Path
import json,os,subprocess
root=Path(__file__).resolve().parents[2]
tools=root/'.toolchains/cc8'
paths=json.loads((tools/'paths.json').read_text())
env=os.environ.copy()
env['JAVA_HOME']=paths['java_home']
env['ANDROID_HOME']=paths['android_sdk']
env['ANDROID_SDK_ROOT']=paths['android_sdk']
sdk=Path(paths['android_sdk'])
manager=sdk/'cmdline-tools/latest/bin/sdkmanager'
subprocess.run([str(manager),'--sdk_root='+str(sdk),'--licenses'],env=env,check=True)
with (tools/'sdk-setup.log').open('w') as log:
    result=subprocess.run([str(manager),'--sdk_root='+str(sdk),'platform-tools','platforms;android-35','build-tools;35.0.0'],input=b'y\n'*100,env=env,stdout=log,stderr=subprocess.STDOUT)
    if result.returncode: raise SystemExit('SDK package setup failed; see '+str(log.name))
print('Android SDK platform 35, build-tools 35.0.0 and platform-tools ready.',flush=True)
