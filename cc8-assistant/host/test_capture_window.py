"""Run actual capture worker/finally and opening callbacks with bounded hardware interfaces."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_interrupted_task_notice import method
from tools.java_toolchain import java_bin
from device_smoke import BARGE_QUERY

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
class Build {static class VERSION {static int SDK_INT=27;}}
class AudioTimestamp {
 static final int TIMEBASE_MONOTONIC=0;
 long framePosition,nanoTime;
}
class AudioRecord {
 static final int STATE_INITIALIZED=1,RECORDSTATE_RECORDING=3,SUCCESS=0;
 static final List<AudioRecord> created=new CopyOnWriteArrayList<>();
 static final CountDownLatch entered=new CountDownLatch(1),releaseOld=new CountDownLatch(1);
 final CountDownLatch stopped=new CountDownLatch(1),released=new CountDownLatch(1);
 final int index;int reads;volatile int timestamps;volatile int stops;volatile boolean started;
 AudioRecord(int source,int rate,int channels,int encoding,int size){index=created.size();created.add(this);}
 static int getMinBufferSize(int rate,int channels,int encoding){return 320;}
 int getState(){return STATE_INITIALIZED;}
 void startRecording(){started=true;}
 int getRecordingState(){return started?RECORDSTATE_RECORDING:0;}
 int read(short[] buffer,int offset,int count){
  reads++;
  String test=CaptureCheck.test;
  try {
   if("read_gap".equals(test)){
    if(reads==2)Thread.sleep(450);
    if(reads==3){entered.countDown();stopped.await(3,TimeUnit.SECONDS);}
    return count;
   }
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
   if(!"normal".equals(test)&&!"replacement".equals(test)){
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
 int getTimestamp(AudioTimestamp stamp,int timebase){
  timestamps++;
  if("timestamp_unavailable".equals(CaptureCheck.test))throw new IllegalStateException("fixture");
  return -3;
 }
 void stop(){stops++;stopped.countDown();}
 void release(){released.countDown();}
}
class VoiceSession {
 final Context context=new Context();
 final Object captureResourceLock=new Object(),routeLock=new Object();
 volatile boolean foreground=true,recording,announcement,textSession;
 volatile int epoch,minimumGeneration;
 volatile long routeEpoch=2;
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
 boolean currentRoute(long route){return route==routeEpoch;}
 void rejectForTest(Object id,long route){rejectCapture(id,route);}
 void cancelVision(){visionCancels++;}
 void cancelPlayback(){playbackCancels++;}
 void send(JSONObject event){events.add(event);}
 void sendPacket(int kind,int epoch,long sequence,long frame,long stamp,byte[] data){packets.incrementAndGet();}
 static class Status{void reset(){}}
 static class Listener {
  final List<Boolean> microphones=new CopyOnWriteArrayList<>();
  final AtomicInteger errors=new AtomicInteger();
  final AtomicInteger rejected=new AtomicInteger(-1);
  void captureRejected(int stoppedEpoch){rejected.set(stoppedEpoch);}
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
   if("rejected_capture".equals(test)){
    s.startMicrophone();
    yes(AudioRecord.entered.await(3,TimeUnit.SECONDS),"capture did not start");
    String rejectedId=s.captureWindow.id;
    int rejectedEpoch=s.epoch;
    yes(s.events.stream().anyMatch(j->"session_start".equals(j.data.get("event"))&&
       rejectedId.equals(j.data.get("capture_id"))),"capture ID did not enter the wire");
    s.rejectForTest(rejectedId,1);
    s.rejectForTest(Boolean.TRUE,2);
    yes(s.recording&&s.listener.rejected.get()==-1,"invalid rejection stopped capture");
    s.rejectForTest(rejectedId,2);
    yes(!s.recording&&s.captureWindow==null,"matching rejection did not stop capture");
    yes(s.listener.rejected.get()==rejectedEpoch+1&&s.captureRejectionCurrent(rejectedEpoch+1),
        "current rejection did not publish error ownership");
    yes(AudioRecord.created.get(0).released.await(3,TimeUnit.SECONDS),"hardware was not released");
    yes(s.events.stream().filter(j->"session_stop".equals(j.data.get("event"))&&
        Integer.valueOf(rejectedEpoch).equals(j.data.get("epoch"))).count()==1,
        "missing or duplicate device stop");
    s.startMicrophone();
    yes(s.recording&&!s.captureWindow.id.equals(rejectedId),"fresh capture reused ID");
    s.rejectForTest(rejectedId,2);
    yes(s.recording&&!s.captureRejectionCurrent(rejectedEpoch+1),"old rejection affected new capture");
    return;
   }
   if("interruption_transcript".equals(test)){
    CaptureWindow normal=new CaptureWindow(0,100);
    normal.observeTranscript(1,5,args[1],101);
    yes(normal.transcriptEvidence(1,101)==null,"ordinary capture recorded a test receipt");
    CaptureWindow window=new CaptureWindow(1000,100);
    window.observeTranscript(1,5,args[1],101);
    yes(window.transcriptEvidence(1,102).matched,"fixed synthetic sentence did not match");
    window.observeTranscript(1,5,"三加六等于多少？",102);
    yes(window.transcriptEvidence(1,102).matched,"replayed generation rewrote the receipt");
    window.observeTranscript(1,6,"换个问题，三减五等于几？",103);
    yes(!window.transcriptEvidence(1,103).matched,"wrong operator matched");
    window.observeTranscript(1,7,"三加五等于几？我要继续聊天。",104);
    yes(!window.transcriptEvidence(1,104).matched,"extra sentence matched");
    window.observeTranscript(1,8,"３＋５＝多少？",105);
    yes(window.transcriptEvidence(1,105).matched,"equivalent digit form did not match");
    yes(window.transcriptEvidence(2,105)==null,"previous route receipt remained visible");
    window.observeTranscript(2,1,"三加六等于几？",106);
    window.observeTranscript(1,100,args[1],107);
    CaptureWindow.TranscriptEvidence proof=window.transcriptEvidence(2,107);
    yes(proof.generation==1&&!proof.matched,"old route rewrote new route receipt");
    window.observeTranscript(2,2,args[1],1100);
    yes(window.transcriptEvidence(2,1100)==null,"expired capture reported a receipt");
    yes(new CaptureWindow(1000,1100).transcriptEvidence(2,1100)==null,
        "new capture inherited a transcript receipt");
    return;
   }
   if("clock_math".equals(test)){
    CaptureClockStats stats=new CaptureClockStats();
    long origin=10_000_000_000L;
    yes(stats.due(origin),"first clock poll not due");
    stats.observe(false,0,0,origin,320,origin);
    yes(!stats.due(origin+249_999_999),"clock poll not throttled");
    yes(stats.due(origin+250_000_000),"clock poll suppressed");
    stats.observe(true,320,origin+10_000_000,origin+20_000_000,640,origin);
    stats.observe(true,320,origin+10_000_000,origin+20_000_000,640,origin);
    stats.observe(true,319,origin+9_000_000,origin+20_000_000,640,origin);
    stats.observe(true,640,origin+50_000_000,origin+49_999_999,640,origin);
    stats.observe(true,640,origin+50_000_000,origin+60_000_000,640,origin);
    yes(stats.snapshot().equals(Map.of(
     "samples",6,"available",2,"unavailable",1,"invalid",1,"duplicate",1,
     "discontinuities",1,"min_offset_ns",-10_000_000L,"max_offset_ns",10_000_000L)),
     "incorrect classification, offset or data exposure");
   } else if("invalid".equals(test)){
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
   } else if("read_gap".equals(test)){
    s.startMicrophone();
    yes(AudioRecord.entered.await(2,TimeUnit.SECONDS),"fixture did not send second packet");
    List<JSONObject> gaps=s.events.stream().filter(e->"capture_gap".equals(e.data.get("event"))).toList();
    yes(gaps.size()==1,"read gap event missing or repeated");
    JSONObject gap=gaps.get(0);
    yes(gap.data.get("epoch").equals(s.epoch)&&gap.data.get("sequence").equals(1L),
        "read gap not bound to the affected capture packet");
    yes(s.packets.get()==2,"read gap changed microphone packet count");
   } else if("normal".equals(test)){
    s.startMicrophone();yes(AudioRecord.entered.await(2,TimeUnit.SECONDS),"normal read fixture not entered");
    yes(s.captureWindow.clockStats==null&&AudioRecord.created.get(0).timestamps==0,
        "normal capture queried hardware clock");
    yes(s.recording&&s.captureWindow.limitMs==0&&clock.get()>=1800000,"normal capture gained a test timeout");
    yes(Thread.getAllStackTraces().keySet().stream().noneMatch(t->t.isAlive()&&"cc8-test-capture-timeout".equals(t.getName())),"normal capture spawned watchdog");
   } else if("replacement".equals(test)){
    s.startMicrophone(1000);yes(AudioRecord.entered.await(2,TimeUnit.SECONDS),"old read not entered");
    s.stopMicrophone();s.startMicrophone();clock.set(5000);AudioRecord.releaseOld.countDown();
    waitFor(()->AudioRecord.created.size()==2&&AudioRecord.created.get(1).reads>=3,"new capture did not start");
    yes(s.recording&&s.captureWindow!=null&&s.captureWindow.limitMs==0,"old finally or deadline changed replacement");
    yes(AudioRecord.created.get(1).stops==0,"old watchdog stopped replacement hardware");
   } else {
    if("old_sdk".equals(test))Build.VERSION.SDK_INT=23;
    s.startMicrophone(1000);
    waitFor(()->!s.recording,"test capture did not expire independently");
    yes(AudioRecord.created.size()==1,"expected one owned hardware session");
    AudioRecord r=AudioRecord.created.get(0);yes(r.released.await(1,TimeUnit.SECONDS),"expired hardware not released");
    yes(s.recorder==null&&s.captureWindow==null,"expired resource ownership retained");
    if("timestamp_unavailable".equals(test))yes(r.timestamps>=1&&s.packets.get()==3,
      "timestamp exception stopped bounded audio or was not exercised");
    if("old_sdk".equals(test))yes(r.timestamps==0&&s.packets.get()==3,
      "pre-24 device queried unsupported timestamp");
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
class Manifest {static class permission {static final String RECORD_AUDIO="record";}}
class PackageManager {static final int PERMISSION_GRANTED=0;}
class Build {static class VERSION {static final int SDK_INT=27;}}
public final class OpeningCheck {
 boolean pending,permissionGrantedAwaitingResume,lastOpenWantedVoice;
 boolean foreground=true,linked=true,active,playbackActive;
 long pendingTestCaptureLimitMs;
 int autoVoiceOpens;
 final OpenVoiceGate autoVoice=new OpenVoiceGate();
 Session session;
 static class Session {
  boolean recording;long lastLimit=-1;int starts,stops;
  boolean isRecording(){return recording;}
  void settings(boolean wake){}
  void startMicrophone(long limit){if(!recording){recording=true;lastLimit=limit;starts++;}}
  void stopMicrophone(){recording=false;stops++;}
  void interrupt(){}
 }
 static class Handler {void removeCallbacks(Runnable r){}}
 static class Wake {boolean isChecked(){return true;}}
 final Handler handler=new Handler();final Wake wake=new Wake();
 final Runnable autoVoiceTick=()->{};
 int checkSelfPermission(String value){return 0;}
 void requestPermissions(String[] permissions,int request){throw new AssertionError("unexpected permission request");}
 void state(String phase,String value){}void caption(String text,String mood){}
 void traceAuto(String value){}
 OPEN_METHOD
 BEGIN_METHOD
 TOGGLE_METHOD
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
  a.session.recording=false;a.requestOpenVoice(i);a.begin();
  yes(a.session.starts==1&&a.session.lastLimit==1000&&a.pendingTestCaptureLimitMs==0);
  a.session.recording=false;a.requestOpenVoice(null);a.begin();
  yes(a.session.starts==2&&a.session.lastLimit==0); // Ordinary opening stays unlimited.
  a.session.recording=false;a.requestOpenVoice(i);a.toggleVoice();
  yes(a.session.starts==3&&a.session.lastLimit==0); // Manual start does not inherit a staged test.
  a.active=true;a.toggleVoice();
  yes(a.session.stops==1&&!a.session.recording&&a.pendingTestCaptureLimitMs==0);
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
                'private boolean stopMicrophone(CaptureWindow expected)',
                'private void rejectCapture(Object captureId, long route)',
                'boolean captureRejectionCurrent(int stoppedEpoch)',
            )
        )
        (path / 'CaptureCheck.java').write_text(HARDWARE.replace('METHODS', methods))
        activity = (JAVA / 'MainActivity.java').read_text()
        (path / 'OpeningCheck.java').write_text(
            OPENING.replace(
                'OPEN_METHOD', method(activity, 'private void requestOpenVoice(Intent intent)')
            )
            .replace('BEGIN_METHOD', method(activity, 'private void begin()'))
            .replace('TOGGLE_METHOD', method(activity, 'private void toggleVoice()'))
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
                    str(JAVA / 'CaptureClockStats.java'),
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

    def check_worker(self, case, *args):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(Path(self.folder.name) / 'worker'),
                'com.happyhome.cc8assistant.CaptureCheck',
                case,
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejection_stops_only_the_owned_capture_and_releases_hardware(self):
        self.check_worker('rejected_capture')

    def test_capture_expires_and_releases_without_any_host_or_ui_callback(self):
        self.check_worker('bounded')

    def test_blocking_read_is_stopped_by_owned_device_watchdog(self):
        self.check_worker('blocked')

    def test_audio_read_crossing_deadline_is_not_sent(self):
        self.check_worker('crossing')

    def test_delayed_native_read_reports_its_exact_epoch_and_packet_sequence(self):
        self.check_worker('read_gap')

    def test_default_capture_has_no_limit_or_watchdog(self):
        self.check_worker('normal')

    def test_capture_clock_uses_only_plausible_advancing_anchors(self):
        self.check_worker('clock_math')

    def test_bounded_capture_correlates_the_public_interruption_transcript(self):
        self.check_worker('interruption_transcript', BARGE_QUERY)

    def test_capture_clock_unavailable_does_not_stop_audio(self):
        self.check_worker('timestamp_unavailable')

    def test_capture_clock_guard_keeps_pre_api_24_audio_working(self):
        self.check_worker('old_sdk')

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
