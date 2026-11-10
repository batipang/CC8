"""Execute native consent/recorder and Java/Python framing with inert audio hardware."""

import base64
from pathlib import Path
import subprocess
import tempfile
import unittest

from protocol import MEETING, Packet
from test_capture_audio_input import FAKES
from tools.java_toolchain import java_bin

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'
CHECK = r"""
package com.happyhome.cc8assistant;
import android.media.AudioRecord;import java.util.*;import java.util.concurrent.*;import java.io.*;
public class MeetingNativeCheck {
 static void need(boolean ok,String why){if(!ok)throw new AssertionError(why);}
 static void consent(){
  MeetingConsent c=new MeetingConsent();String id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  need(!c.take(id,1,1),"Unsolicited reply authorized capture");
  need(!c.arm(id,-1,1)&&!c.arm(id,1,Long.MAX_VALUE),"Invalid bound accepted");
  need(c.arm(id,1,100),"Explicit grant rejected");
  need(!c.arm(id,1,101),"Pending grant replaced");
  need(!c.take(id,2,100)&&!c.take(id,1,99),"Wrong route/time authorized capture");
  need(c.take(id,1,101)&&!c.take(id,1,102),"Grant was not once only");
  need(c.arm(id,1,100)&&!c.take(id,1,12100),"Expired reply authorized capture");
  need(c.clear().equals(id)&&!c.take(id,1,110),"Closing did not revoke grant");
  need(c.arm(id,2,100),"Fresh route could not grant");c.reject("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",2);
  need(c.take(id,2,101),"Old rejection revoked new grant");
 }
 static final class Port implements MeetingRecorder.Port {
  final CountDownLatch done=new CountDownLatch(1), sent=new CountDownLatch(1);
  volatile boolean allowed=true,failed,released;boolean startAllowed=true;volatile int packets;int limit=2;
  final List<byte[]> references=new ArrayList<>();final List<byte[]> copies=new ArrayList<>();
  public boolean current(){return allowed;}
  public boolean start(AudioRecord r){if(!startAllowed)return false;r.startRecording();return true;}
  public boolean send(byte[] pcm,long sequence,long frame){
   need(sequence==packets&&frame==packets*320,"Frame continuity changed");
   references.add(pcm);copies.add(pcm.clone());packets++;sent.countDown();
   if(packets>=limit)allowed=false;return true;
  }
  public void finished(boolean failure,boolean release){failed=failure;released=release;done.countDown();}
 }
 static void waitDone(Port p,MeetingRecorder r)throws Exception{
  need(p.done.await(2,TimeUnit.SECONDS),"Recorder did not release");need(!r.busy()&&!r.capturing(),"Completed recorder remained active");
  for(AudioRecord a:AudioRecord.created)need(a.releases==1,"Hardware leaked/double released");
 }
 static void run(String mode)throws Exception{
  Object resource=new Object();Port p=new Port();MeetingRecorder r=new MeetingRecorder(resource,p);
  need(AudioRecord.created.size()==0&&!r.busy()&&!r.capturing(),"Constructor acquired media");
  AudioRecord.source=new short[640];Arrays.fill(AudioRecord.source,(short)8192);AudioRecord.sizes=new int[]{320};
  if(mode.equals("cancel_wait")){
   synchronized(resource){r.start();r.stop();}waitDone(p,r);need(AudioRecord.created.isEmpty(),"Cancelled waiter opened hardware");return;
  }
  if(mode.equals("denied"))p.startAllowed=false;
  if(mode.equals("read_error"))AudioRecord.mode="negative";
  if(mode.equals("release_error"))AudioRecord.mode="release_fail";
  if(mode.equals("factory_release_error"))AudioRecord.mode="factory_release_error";
  if(mode.equals("blocked_read")){AudioRecord.mode="blocked";p.limit=1000;}
  r.start();
  if(mode.equals("blocked_read")){
   need(AudioRecord.readEntered.await(1,TimeUnit.SECONDS),"Read never blocked");r.stop();
  }
  waitDone(p,r);
  if(mode.equals("denied"))need(p.failed&&AudioRecord.created.get(0).starts==0,"Denied physical start was not reported");
  else if(mode.equals("read_error")||mode.equals("release_error")||mode.equals("factory_release_error"))need(p.failed,"Hardware failure was hidden");
  else if(mode.equals("blocked_read"))need(p.packets==0&&!p.failed,"Stopped read leaked data or became a new recording");
  else {
   need(p.packets==2&&!p.failed,"Mono PCM not delivered exactly once");
   need(p.copies.get(0)[0]==0&&p.copies.get(0)[1]==32,"PCM encoding changed");
   for(byte[] bytes:p.references)for(byte b:bytes)need(b==0,"Returned PCM retained after send");
  }
  need(p.released!=mode.contains("release_error"),"Physical release receipt was invented");
  boolean rejected=false;try{r.start();}catch(IllegalStateException e){rejected=true;}
  need(rejected,"Consumed recorder reopened without a fresh leg");
 }
 public static void main(String[] args)throws Exception{
  String mode=args[0];
  if(mode.equals("wire")){
   byte[] bytes=Base64.getDecoder().decode(args[1]);Wire.Packet packet=Wire.read(new DataInputStream(new ByteArrayInputStream(bytes)));
   Wire.Packet inner=Wire.unbindRoute(packet,"cccccccccccccccccccccccccccccccc");
   need(inner!=null&&inner.type==Wire.MEETING&&inner.data.length==336,"Meeting envelope lost nonce/PCM");
   ByteArrayOutputStream output=new ByteArrayOutputStream();Wire.write(new DataOutputStream(output),packet.type,packet.generation,packet.sequence,packet.frame,packet.nanos,packet.data);
   need(Arrays.equals(output.toByteArray(),bytes),"Wire encoding disagreed");
   for(int size:new int[]{0,16,17,3218}){
    boolean rejected=false;try{Wire.write(new DataOutputStream(new ByteArrayOutputStream()),Wire.MEETING,1,0,0,0,new byte[size]);}catch(IOException e){rejected=true;}
    need(rejected,"Malformed meeting payload accepted");
   }
  } else if(mode.equals("consent"))consent();else run(mode);
  System.out.println("PASS "+mode);
 }
}
"""


class MeetingNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = java_bin()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-meeting-native-')
        cls.root = Path(cls.folder.name)
        fixtures = {
            key: value for key, value in FAKES.items() if not key.endswith('CaptureInputCheck.java')
        }
        fixtures['android/os/SystemClock.java'] = (
            'package android.os;public class SystemClock{public static long elapsedRealtime(){return System.nanoTime()/1000000;}}'
        )
        fixtures['org/json/JSONObject.java'] = (
            'package org.json;public class JSONObject{public JSONObject(){}public JSONObject(String s){}public void put(String k,Object v){}}'
        )
        fixtures['android/media/AudioRecord.java'] = (
            fixtures['android/media/AudioRecord.java']
            .replace(
                'public static String mode="ok";',
                'public static final java.util.concurrent.CountDownLatch readEntered=new java.util.concurrent.CountDownLatch(1);'
                'public static String mode="ok";public volatile boolean stopped;',
            )
            .replace(
                'public void startRecording(){starts++;}',
                'public void startRecording(){starts++;}public synchronized void stop(){stopped=true;notifyAll();}',
            )
            .replace(
                'if(mode.equals("negative"))return -3;',
                'if(mode.equals("blocked")){readEntered.countDown();synchronized(this){while(!stopped){try{wait();}catch(InterruptedException e){throw new RuntimeException(e);}}}return -3;}'
                'if(mode.equals("negative"))return -3;',
            )
        )
        fixtures['android/media/AudioRecord.java'] = (
            fixtures['android/media/AudioRecord.java']
            .replace(
                'if(mode.equals("bad_mono"))state=0;',
                'if(mode.equals("bad_mono")||mode.equals("factory_release_error"))state=0;',
            )
            .replace(
                'if(mode.equals("release_fail"))throw',
                'if(mode.equals("release_fail")||mode.equals("factory_release_error"))throw',
            )
        )
        fixtures['com/happyhome/cc8assistant/MeetingNativeCheck.java'] = CHECK
        files = []
        try:
            for name, value in fixtures.items():
                path = cls.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
                files.append(str(path))
            result = subprocess.run(
                [
                    str(cls.java / 'javac'),
                    '-encoding',
                    'UTF-8',
                    '-d',
                    str(cls.root),
                    *[
                        str(JAVA / name)
                        for name in (
                            'MeetingRecorder.java',
                            'MeetingConsent.java',
                            'CaptureAudioInput.java',
                            'PairedCapturePcm.java',
                            'Wire.java',
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
        except BaseException:
            cls.folder.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_native_consent_recorder_release_and_cross_language_framing(self):
        packet = Packet(MEETING, 3, 4, 5, 6, bytes.fromhex('a' * 32) + b'\x00\x20' * 160)
        for case in (
            'consent',
            'mono',
            'cancel_wait',
            'denied',
            'read_error',
            'release_error',
            'factory_release_error',
            'blocked_read',
            'wire',
        ):
            with self.subTest(case=case):
                args = (
                    [base64.b64encode(packet.bind_route('c' * 32).encode()).decode()]
                    if case == 'wire'
                    else []
                )
                result = subprocess.run(
                    [
                        str(self.java / 'java'),
                        '-cp',
                        str(self.root),
                        'com.happyhome.cc8assistant.MeetingNativeCheck',
                        case,
                        *args,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'PASS ' + case)


if __name__ == '__main__':
    unittest.main()
