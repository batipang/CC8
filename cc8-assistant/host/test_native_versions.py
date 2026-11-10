"""Run real version view, certificate lookup and lifecycle with inert Android surfaces."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_recognition_controls_ui import STUBS
from tools.java_toolchain import java_bin

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'


def stubs():
    result = dict(STUBS)
    result['android/os/Handler.java'] = result['android/os/Handler.java'].replace(
        'public Handler(Looper l){}', 'public Handler(){}public Handler(Looper l){}'
    )
    result['android/content/Context.java'] = """package android.content;public class Context{
 public static final String CLIPBOARD_SERVICE="clipboard";public Object clipboard=new ClipboardManager();
 public Object getSystemService(String name){return clipboard;}
 public final android.content.pm.PackageManager packages=new android.content.pm.PackageManager();
 public android.content.pm.PackageManager getPackageManager(){return packages;}
 public String getPackageName(){return "com.happyhome.cc8assistant";}}"""
    result[
        'android/content/ClipData.java'
    ] = """package android.content;public class ClipData{public String value;
 public static ClipData newPlainText(String name,String value){ClipData d=new ClipData();d.value=value;return d;}}"""
    result[
        'android/content/ClipboardManager.java'
    ] = """package android.content;public class ClipboardManager{
 public static String last;public void setPrimaryClip(ClipData d){last=d.value;}}"""
    result['android/widget/Toast.java'] = result['android/widget/Toast.java'].replace(
        'LENGTH_LONG=1', 'LENGTH_LONG=1,LENGTH_SHORT=0'
    )
    result['android/os/Build.java'] = (
        'package android.os;public class Build{public static class VERSION{public static int SDK_INT=27;}}'
    )
    result[
        'android/content/pm/PackageManager.java'
    ] = """package android.content.pm;public class PackageManager{
 public static final int GET_SIGNATURES=1,GET_SIGNING_CERTIFICATES=2;public int calls,flags;public boolean fail;
 public final PackageInfo info=new PackageInfo();public PackageInfo getPackageInfo(String name,int f)throws Exception{
 if(!name.equals("com.happyhome.cc8assistant"))throw new AssertionError("queried another app");calls++;flags=f;
 if(fail)throw new Exception("synthetic unavailable");return info;}}"""
    result[
        'android/content/pm/PackageInfo.java'
    ] = """package android.content.pm;public class PackageInfo{
 public Signature[] signatures={new Signature("a")};public SigningInfo signingInfo=new SigningInfo();}"""
    result[
        'android/content/pm/Signature.java'
    ] = """package android.content.pm;public class Signature{
 final byte[] bytes;public Signature(String value){bytes=value.getBytes(java.nio.charset.StandardCharsets.UTF_8);}public byte[] toByteArray(){return bytes;}}"""
    result[
        'android/content/pm/SigningInfo.java'
    ] = """package android.content.pm;public class SigningInfo{
 public Signature[] values={new Signature("b")};public Signature[] getApkContentsSigners(){return values;}}"""
    result[
        'com/happyhome/cc8assistant/BuildConfig.java'
    ] = """package com.happyhome.cc8assistant;class BuildConfig{
 static final String VERSION_NAME="0.9.123";static final int VERSION_CODE=160,CC8_PROTOCOL=2;}"""
    result['android/app/AlertDialog.java'] = (
        result['android/app/AlertDialog.java']
        .replace(
            'public Builder builder;',
            'public int writes;public String message;public void setMessage(String value){writes++;message=value;}public Builder builder;',
        )
        .replace(
            'AlertDialog(Builder b){builder=b;}',
            'AlertDialog(Builder b){builder=b;message=b.message;}',
        )
        .replace(
            'public Builder setTitle(String s){return this;}public Builder setMessage(String s){return this;}',
            'public String message;public Builder setTitle(String s){return this;}public Builder setMessage(String s){message=s;return this;}',
        )
    )
    result['android/app/AlertDialog.java'] = result['android/app/AlertDialog.java'].replace(
        'final java.util.Map<Integer,OnClickListener> actions',
        'public final java.util.Map<Integer,OnClickListener> actions',
    )
    return result


HARNESS = r"""package com.happyhome.cc8assistant;
import org.json.*;import android.app.*;import android.os.*;import android.content.*;import android.content.pm.*;
public class VersionsCheck {
 static void yes(boolean value,String why){if(!value)throw new AssertionError(why);}
 static JSONObject declaration(){return new JSONObject("schema_version",1,"release_id","version-info-test",
 "backend_version","0.16.33","expected_assistant_version","0.9.123","expected_assistant_code",160,
 "protocol_version",2,"device_protocol_min",2,"device_protocol_max",2);}
 static class Port implements VersionDialog.Port{boolean active=true;long route=4;VersionPresentation.Peer peer=new VersionPresentation.Peer(declaration(),4);
 public boolean foreground(){return active;}public long route(){return route;}public VersionPresentation.Peer peer(){return peer;}}
 static void tick(){Runnable r=Handler.queued.remove(0);r.run();}
 public static void main(String[] args){Activity activity=new Activity();Port port=new Port();VersionDialog ui=new VersionDialog(activity,port);
 switch(args[0]){
 case "declarations":{
  JSONObject raw=declaration();VersionPresentation.Peer peer=new VersionPresentation.Peer(raw,4);
  raw.put("backend_version","9.9.9");String text=VersionPresentation.message("0.9.123",160,2,"cert",4,peer);
  yes(text.contains("后台：0.16.33")&&text.contains("版本组合：与后台发布声明一致")&&text.contains("不是安装前检查"),"public data changed or became installation proof");
  text=VersionPresentation.message("0.0.1",1,3,"cert",4,peer);
  yes(text.contains("协议范围声明：不兼容")&&text.contains("版本组合：与后台发布声明不同"),"protocol and version mismatch conflated");
  for(long route:new long[]{-1,5}){text=VersionPresentation.message("0.9.123",160,2,"cert",route,peer);
   yes(!text.contains("0.16.33")&&text.contains("未确认"),"lost route retained declaration");}
  for(Object bad:new Object[]{null,true,"1",1.0,0,2}){yes(!new VersionPresentation.Peer(declaration().put("schema_version",bad),4).known,"coerced schema");}
  for(String key:new String[]{"expected_assistant_code","protocol_version","device_protocol_min","device_protocol_max"})
   for(Object bad:new Object[]{null,true,"2",2.0,0,-1,Long.MAX_VALUE})yes(!new VersionPresentation.Peer(declaration().put(key,bad),4).known,"coerced/overflow integer");
  for(String key:new String[]{"backend_version","expected_assistant_version","release_id"})
   yes(!new VersionPresentation.Peer(declaration().put(key,"bad\nsecret"),4).known,"unbounded release text");
  yes(!new VersionPresentation.Peer(declaration().put("device_protocol_min",3),4).known,"reversed range accepted");
  yes(!new VersionPresentation.Peer(null,4).known,"legacy release became known");break;
 }
 case "certificates":{
  String a="ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb";
  String b="3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d";
  yes(ApkSignatures.current(activity).equals(a)&&activity.packages.flags==PackageManager.GET_SIGNATURES,"SDK27 signer query wrong");
  Build.VERSION.SDK_INT=28;activity.packages.info.signingInfo.values=new Signature[]{new Signature("a"),new Signature("b"),new Signature("a")};
  yes(ApkSignatures.current(activity).equals(b+"\n"+a)&&activity.packages.flags==PackageManager.GET_SIGNING_CERTIFICATES,"modern signer set unsorted/incomplete");
  activity.packages.info.signingInfo=null;yes(ApkSignatures.current(activity).contains("未取得"),"missing current signer borrowed legacy");
  Build.VERSION.SDK_INT=27;activity.packages.info.signatures=new Signature[]{null};yes(ApkSignatures.current(activity).contains("未取得"),"null signer promoted");
  activity.packages.info.signatures=new Signature[]{new Signature("")};yes(ApkSignatures.current(activity).contains("未取得"),"empty signer promoted");
  activity.packages.fail=true;yes(ApkSignatures.current(activity).contains("未取得"),"lookup exception promoted");break;
 }
 case "lifecycle":{
  yes(activity.packages.calls==0&&Handler.queued.isEmpty(),"constructor active");ui.open();AlertDialog old=AlertDialog.last;
  yes(old.message.contains("0.16.33")&&activity.packages.calls==1&&Handler.queued.size()==1,"explicit open missing actual data");
  tick();yes(old.writes==0&&Handler.queued.size()==1,"unchanged polling repainted/scrolled body");
  Runnable retired=Handler.queued.get(0);ui.open();AlertDialog current=AlertDialog.last;
  old.builder.actions.get(-1).onClick(old,-1);retired.run();old.dismiss();
  yes(current.showing&&ClipboardManager.last==null&&Handler.queued.size()==1,"old window affected replacement");
  port.route=-1;current.click(-1);yes(ClipboardManager.last.contains("未连接")&&!ClipboardManager.last.contains("0.16.33"),"copy cached old route");
  yes(Handler.queued.isEmpty(),"dismiss retained timer");ui.open();port.active=false;tick();
  yes(!AlertDialog.last.showing&&Handler.queued.isEmpty(),"background retained modal/timer");int queries=activity.packages.calls;ui.open();
  yes(activity.packages.calls==queries&&Handler.queued.isEmpty(),"background open did work");break;
 }
 case "refresh":{
  ui.open();port.route=5;tick();yes(AlertDialog.last.message.contains("未确认")&&!AlertDialog.last.message.contains("0.16.33"),"new route borrowed old backend");
  port.peer=new VersionPresentation.Peer(declaration().put("backend_version","0.16.34"),5);tick();
  yes(AlertDialog.last.message.contains("0.16.34"),"fresh current declaration missing");
  activity.clipboard=null;AlertDialog.last.click(-1);yes(android.widget.Toast.last.contains("无法复制")&&ClipboardManager.last==null,"clipboard failure hidden");break;
 }
 default:throw new AssertionError(args[0]);
 }
 }
}"""


class NativeVersionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        directory = Path(cls.folder.name)
        files = []
        for name, contents in {
            **stubs(),
            'com/happyhome/cc8assistant/VersionsCheck.java': HARNESS,
        }.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
            files.append(str(path))
        cls.java = java_bin()
        result = subprocess.run(
            [
                str(cls.java / 'javac'),
                '-encoding',
                'UTF-8',
                '-d',
                str(directory),
                *[
                    str(JAVA / name)
                    for name in (
                        'VersionPresentation.java',
                        'VersionDialog.java',
                        'ApkSignatures.java',
                    )
                ],
                *files,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr)

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                self.folder.name,
                'com.happyhome.cc8assistant.VersionsCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_claims_are_bounded_and_route_owned(self):
        self.scenario('declarations')

    def test_real_signing_certificate_hashes_and_sdk_guards(self):
        self.scenario('certificates')

    def test_passive_constructor_lifecycle_stale_callbacks_and_current_copy(self):
        self.scenario('lifecycle')

    def test_refresh_invalidation_and_missing_clipboard(self):
        self.scenario('refresh')
