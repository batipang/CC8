"""Run actual capture worker/finally and opening callbacks with bounded hardware interfaces."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_interrupted_task_notice import method
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'

HARDWARE = r"""
package com.happyhome.cc8assistant;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

class SystemClock {static long elapsedRealtime(){return CaptureCheck.clock.get();}}
class Manifest {static class permission {static final String RECORD_AUDIO="record";}}
class PackageManager {static final int PERMISSION_GRANTED=0;}
class Context {int checkCallingOrSelfPermission(String permission){return 0;}}
class AudioFormat {static final int CHANNEL_IN_MONO=1,ENCODING_PCM_16BIT=2;}
class MediaRecorder {static class AudioSource {static final int VOICE_COMMUNICATION=7;}}
class JSONObject {
 final Map<String,Object> data=new ConcurrentHashMap<>();
}
class Wire {
 static final int MIC=2;
 static JSONObject event(String name){JSONObject j=new JSONObject();j.data.put("event",name);return j;}
 static void put(JSONObject j,String key,Object value){j.data.put(key,value);}
 static byte[] pcm(short[] samples,int count){return new byte[count*2];}
}
class AudioRecord {
 static final int STATE_INITIALIZED=1,RECORDSTATE_RECORDING=3;
 static final List<AudioRecord> created=new CopyOnWriteArrayList<>();
 static final CountDownLatch entered=new CountDownLatch(1),releaseOld=new CountDownLatch(1);
 final CountDownLatch stopped=new CountDownLatch(1),released=new CountDownLatch(1);
 final int index;int reads;volatile int stops;volatile boolean started;
 AudioRecord(int source,int rate,int channels,int encoding,int size){index=created.size();created.add(this);}
 static int getMinBufferSize(int rate,int channels,int encoding){return 320;}
 int getState(){return STATE_INITIALIZED;}
 void startRecording(){started=true;}
 int getRecordingState(){return started?RECORDSTATE_RECORDING:0;}
 int read(short[] buffer,int offset,int count){
  reads++;
  String test=CaptureCheck.test;
  try {
   if("blocked".equals(test)){
    CaptureCheck.clock.set(1001);entered.countDown();
    if(!stopped.await(3,TimeUnit.SECONDS))throw new AssertionError("native read not unblocked");
    return -3;
   }
   if("replacement".equals(test)&&index==0){
    entered.countDown();
    if(!releaseOld.await(3,TimeUnit.SECONDS))throw new AssertionError("old read fixture timeout");
    return count;
   }
   if("bounded".equals(test)||"crossing".equals(test)){
    CaptureCheck.clock.addAndGet(250);
    // Let an unlimited old worker remain observable, with finite cleanup.
    if(reads>4){entered.countDown();stopped.await(3,TimeUnit.SECONDS);}
   } else {
    CaptureCheck.clock.addAndGet(600000);
    if(reads>=3){entered.countDown();stopped.await(3,TimeUnit.SECONDS);}
   }
  } catch(InterruptedException error){Thread.currentThread().interrupt();return -3;}
  return count;
 }
 void stop(){stops++;stopped.countDown();}
 void release(){released.countDown();}
}
class VoiceSession {
 final Context context=new Context();
 final Object captureResourceLock=new Object(),routeLock=new Object();
 volatile boolean foreground=true,recording,announcement,textSession;
 volatile int epoch,minimumGeneration;
 volatile long micFrames,renderFrames;
 volatile float inputLevel;
 volatile String phase="idle";
 volatile AudioRecord recorder;
 volatile CaptureWindow captureWindow;
 final Listener listener=new Listener();
 final Status statusFeedback=new Status();
 final List<JSONObject> events=new CopyOnWriteArrayList<>();
 final AtomicInteger packets=new AtomicInteger();
 int visionCancels,playbackCancels;
 boolean isConnected(){return true;}
 void cancelVision(){visionCancels++;}
 void cancelPlayback(){playbackCancels++;}
 void send(JSONObject event){events.add(event);}
 void sendPacket(int kind,int epoch,long sequence,long frame,long stamp,byte[] data){packets.incrementAndGet();}
 static class Status{void reset(){}}
 static class Listener {
  final List<Boolean> microphones=new CopyOnWriteArrayList<>();
  final AtomicInteger errors=new AtomicInteger();
  void microphone(boolean active){microphones.add(active);}
  void playback(boolean active){}
  void state(String phase,String value){if("error".equals(phase))errors.incrementAndGet();}
 }
 METHODS
}
public final class CaptureCheck {
 static final AtomicLong clock=new AtomicLong();
 static String test;
 static void yes(boolean value,String why){if(!value)throw new AssertionError(why);}
 interface Condition {boolean done();}
 static void waitFor(Condition condition,String why)throws Exception{
  long until=System.nanoTime()+TimeUnit.SECONDS.toNanos(3);
  while(!condition.done()&&System.nanoTime()<until)Thread.sleep(5);
  yes(condition.done(),why);
 }
 static void clean(VoiceSession s)throws Exception{
  s.stopMicrophone();AudioRecord.releaseOld.countDown();
  for(AudioRecord r:AudioRecord.created)r.stop();
  waitFor(()->Thread.getAllStackTraces().keySet().stream().noneMatch(t->t.isAlive()&&
   ("cc8-microphone".equals(t.getName())||"cc8-test-capture-timeout".equals(t.getName()))),"capture worker leaked");
 }
 public static void main(String[] args)throws Exception {
  test=args[0];VoiceSession s=new VoiceSession();
  try {
   if("invalid".equals(test)){
    for(long limit:new long[]{-1,999,1500001,Long.MAX_VALUE}){
     try{s.startMicrophone(limit);throw new AssertionError("invalid limit accepted");}
     catch(IllegalArgumentException expected){}
    }
    yes(!s.recording&&s.visionCancels==0&&s.playbackCancels==0&&AudioRecord.created.isEmpty(),"invalid limit mutated resources");
   } else if("expired_waiter".equals(test)){
    synchronized(s.captureResourceLock){s.startMicrophone(1000);clock.set(1001);}
    waitFor(()->!s.recording,"expired waiter stayed active");
    yes(AudioRecord.created.isEmpty(),"expired waiter opened hardware");
    yes(s.packets.get()==0&&s.captureWindow==null,"expired waiter sent audio or kept lease");
   } else if("normal".equals(test)){
    s.startMicrophone();yes(AudioRecord.entered.await(2,TimeUnit.SECONDS),"normal read fixture not entered");
    yes(s.recording&&s.captureWindow.limitMs==0&&clock.get()>=1800000,"normal capture gained a test timeout");
    yes(Thread.getAllStackTraces().keySet().stream().noneMatch(t->t.isAlive()&&"cc8-test-capture-timeout".equals(t.getName())),"normal capture spawned watchdog");
   } else if("replacement".equals(test)){
    s.startMicrophone(1000);yes(AudioRecord.entered.await(2,TimeUnit.SECONDS),"old read not entered");
    s.stopMicrophone();s.startMicrophone();clock.set(5000);AudioRecord.releaseOld.countDown();
    waitFor(()->AudioRecord.created.size()==2&&AudioRecord.created.get(1).reads>=3,"new capture did not start");
    yes(s.recording&&s.captureWindow!=null&&s.captureWindow.limitMs==0,"old finally or deadline changed replacement");
    yes(AudioRecord.created.get(1).stops==0,"old watchdog stopped replacement hardware");
   } else {
    s.startMicrophone(1000);
    waitFor(()->!s.recording,"test capture did not expire independently");
    yes(AudioRecord.created.size()==1,"expected one owned hardware session");
    AudioRecord r=AudioRecord.created.get(0);yes(r.released.await(1,TimeUnit.SECONDS),"expired hardware not released");
    yes(s.recorder==null&&s.captureWindow==null,"expired resource ownership retained");
    waitFor(()->s.events.stream().anyMatch(e->"session_stop".equals(e.data.get("event"))),"capture end callback missing");
    yes(s.listener.errors.get()==0,"normal test expiry became a recording error");
    yes(s.listener.microphones.equals(Arrays.asList(true,false)),"missing capture end callback");
    yes(s.events.stream().anyMatch(e->"session_stop".equals(e.data.get("event"))),"backend did not receive capture stop");
    if("crossing".equals(test))yes(s.packets.get()==3,"a read crossing the deadline escaped on the wire");
    if("blocked".equals(test))yes(s.packets.get()==0&&r.stops>=1,"watchdog did not stop the blocked owned record");
   }
  } finally {clean(s);}
 }
}
"""

OPENING = r"""
package com.happyhome.cc8assistant;
import java.util.*;
class SystemClock {static long elapsedRealtime(){return 10;}}
class Intent {
 final Map<String,Object> extras=new HashMap<>();
 boolean hasExtra(String key){return extras.containsKey(key);}
 Map<String,Object> getExtras(){return extras;}
 boolean getBooleanExtra(String key,boolean fallback){return (Boolean)extras.getOrDefault(key,fallback);}
}
public final class OpeningCheck {
 boolean pending,permissionGrantedAwaitingResume,lastOpenWantedVoice;
 long pendingTestCaptureLimitMs;
 int autoVoiceOpens;
 final OpenVoiceGate autoVoice=new OpenVoiceGate();
 Session session;
 static class Session {boolean recording;boolean isRecording(){return recording;}}
 void traceAuto(String value){}
 OPEN_METHOD
 static void yes(boolean value){if(!value)throw new AssertionError();}
 public static void main(String[] args){
  OpeningCheck a=new OpeningCheck();
  for(Object value:new Object[]{0L,-1L,999L,1500001L,true,"1000",1.5,Long.MAX_VALUE}){
   Intent i=new Intent();i.extras.put("test_capture_limit_ms",value);a.requestOpenVoice(i);
   yes(!a.lastOpenWantedVoice&&!a.autoVoice.staged()&&a.pendingTestCaptureLimitMs==0);
  }
  for(Object value:new Object[]{1000,180000L,1200000L,1500000L}){
   Intent i=new Intent();i.extras.put("test_capture_limit_ms",value);a.requestOpenVoice(i);
   yes(a.lastOpenWantedVoice&&a.autoVoice.staged()&&a.pendingTestCaptureLimitMs==((Number)value).longValue());
  }
  a.requestOpenVoice(null);yes(a.lastOpenWantedVoice&&a.pendingTestCaptureLimitMs==0);
  a.session=new Session();a.session.recording=true;
  Intent i=new Intent();i.extras.put("test_capture_limit_ms",1000L);a.requestOpenVoice(i);
  yes(a.pendingTestCaptureLimitMs==0); // Another open cannot retime a current recording.
 }
}
"""


class CaptureWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = java_bin()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-capture-window-')
        path = Path(cls.folder.name)
        source = (JAVA / 'VoiceSession.java').read_text()
        methods = '\n'.join(
            method(source, signature)
            for signature in (
                'void startMicrophone()',
                'void startMicrophone(long testLimitMs)',
                'void stopMicrophone()',
            )
        )
        (path / 'CaptureCheck.java').write_text(HARDWARE.replace('METHODS', methods))
        activity = (JAVA / 'MainActivity.java').read_text()
        (path / 'OpeningCheck.java').write_text(
            OPENING.replace(
                'OPEN_METHOD', method(activity, 'private void requestOpenVoice(Intent intent)')
            )
        )
        # The two isolated harnesses each define SystemClock.
        for source_name, package in (
            ('CaptureCheck.java', 'worker'),
            ('OpeningCheck.java', 'opening'),
        ):
            result = subprocess.run(
                [
                    str(cls.java / 'javac'),
                    '-d',
                    str(path / package),
                    str(JAVA / 'CaptureWindow.java'),
                    str(JAVA / 'OpenVoiceGate.java'),
                    str(path / source_name),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                cls.folder.cleanup()
                raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def check_worker(self, case):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(Path(self.folder.name) / 'worker'),
                'com.happyhome.cc8assistant.CaptureCheck',
                case,
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_capture_expires_and_releases_without_any_host_or_ui_callback(self):
        self.check_worker('bounded')

    def test_blocking_read_is_stopped_by_owned_device_watchdog(self):
        self.check_worker('blocked')

    def test_audio_read_crossing_deadline_is_not_sent(self):
        self.check_worker('crossing')

    def test_default_capture_has_no_limit_or_watchdog(self):
        self.check_worker('normal')

    def test_expired_old_worker_cannot_stop_or_clear_new_normal_capture(self):
        self.check_worker('replacement')

    def test_resource_wait_cannot_open_hardware_after_deadline(self):
        self.check_worker('expired_waiter')

    def test_invalid_direct_limit_does_not_mutate_resources(self):
        self.check_worker('invalid')

    def test_actual_intent_callback_rejects_malformed_or_unbounded_test_limits(self):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(Path(self.folder.name) / 'opening'),
                'com.happyhome.cc8assistant.OpeningCheck',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
