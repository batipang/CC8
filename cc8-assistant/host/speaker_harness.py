"""Compile actual Player/lifetime code against bounded native-output interfaces for offline tests."""

from pathlib import Path
import subprocess
import tempfile

from test_interrupted_task_notice import method
from test_error_completion_feedback import branch
from tools.java_toolchain import require_java

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'

STUBS = {
    'android/media/AudioFormat.java': """package android.media;
public class AudioFormat {public static final int CHANNEL_OUT_MONO=4,ENCODING_PCM_16BIT=2;}""",
    'android/media/AudioManager.java': """package android.media;
public class AudioManager {public static final int STREAM_MUSIC=3;}""",
    'android/media/AudioTimestamp.java': """package android.media;
public class AudioTimestamp {public long framePosition,nanoTime;}""",
    'android/os/Build.java': """package android.os;
public class Build {public static class VERSION {public static int SDK_INT=27;}}""",
    'android/os/SystemClock.java': """package android.os;
public class SystemClock {public static long elapsedRealtime(){return System.nanoTime()/1000000;}}""",
    'android/os/Looper.java': """package android.os;
public class Looper {public static Looper getMainLooper(){return new Looper();}}""",
    'android/os/Handler.java': """package android.os;
public class Handler {public Handler(Looper looper){}public void postDelayed(Runnable work,long delay){}}""",
    'android/media/AudioTrack.java': r"""package android.media;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
public class AudioTrack {
 public static final int STATE_INITIALIZED=1,MODE_STREAM=1,PLAYSTATE_PLAYING=3,WRITE_NON_BLOCKING=1;
 public static final java.util.List<AudioTrack> created=new CopyOnWriteArrayList<>();
 public static volatile boolean invalid,volumeError;
 public static volatile int minimum=320,volumeResult;
 public volatile boolean playing,failPause,failPlay,failFlush,released;
 public volatile long physical,lastStart,manualHead=-1,firstFlushNanos,lastTimestampNanos;
 public volatile int pauses,plays,flushes,headReads,legacyWrites,modernWrites;
 public volatile Runnable duringRead;
 public final AtomicInteger releases=new AtomicInteger();
 public final CountDownLatch resetApplied=new CountDownLatch(1);
 public final int bytes;
 public float volume;
 public AudioTrack(int stream,int rate,int channels,int encoding,int size,int mode){
  if(stream!=3||rate!=16000||channels!=4||encoding!=2||mode!=1)throw new AssertionError("native configuration");
  bytes=size;created.add(this);
 }
 public static int getMinBufferSize(int rate,int channels,int encoding){return minimum;}
 public int getState(){return invalid?0:STATE_INITIALIZED;}
 public int setVolume(float gain){if(volumeError)throw new IllegalStateException("volume");volume=gain;return volumeResult;}
 public int getBufferSizeInFrames(){return bytes/2;}
 public void play(){if(failPlay)throw new IllegalStateException("play");playing=true;plays++;}
 public void pause(){if(failPause)throw new IllegalStateException("pause");playing=false;pauses++;}
 public synchronized void flush(){
  if(failFlush)throw new IllegalStateException("flush");
  physical=0;lastStart=0;manualHead=-1;flushes++;
  if(firstFlushNanos==0)firstFlushNanos=System.nanoTime();
  if(flushes>=2&&Thread.currentThread().getName().equals("cc8-speaker-clock"))resetApplied.countDown();
 }
 public void awaitReset(){
  try{if(!resetApplied.await(3,TimeUnit.SECONDS))throw new AssertionError("worker did not observe stop");}
  catch(InterruptedException error){Thread.currentThread().interrupt();throw new AssertionError(error);}
 }
 public void release(){released=true;playing=false;if(releases.incrementAndGet()!=1)throw new AssertionError("double release");}
 public int getPlayState(){return playing?PLAYSTATE_PLAYING:2;}
 public int getPlaybackHeadPosition(){
  headReads++;long position=manualHead>=0?manualHead:physical;
  Runnable action=duringRead;duringRead=null;if(action!=null)action.run();
  return (int)position;
 }
 public boolean getTimestamp(AudioTimestamp timestamp){
  timestamp.framePosition=lastStart&0xffffffffL;timestamp.nanoTime=System.nanoTime()+1000000;lastTimestampNanos=timestamp.nanoTime;return true;
 }
 public int write(short[] samples,int offset,int count,int mode){
  if(mode!=WRITE_NON_BLOCKING)throw new AssertionError("blocking modern write");
  modernWrites++;return accept(samples,offset,count);
 }
 public int write(short[] samples,int offset,int count){legacyWrites++;return accept(samples,offset,count);}
 private int accept(short[] data,int offset,int count){
  if(released)throw new IllegalStateException("released write");
  if(count>320)throw new AssertionError("unbounded native write");
  if(!playing)return 0;
  try{Thread.sleep(1);}catch(InterruptedException error){Thread.currentThread().interrupt();return 0;}
  int accepted=Math.min(count,97);
  synchronized(this){lastStart=physical;physical+=accepted;}
  return accepted;
 }
}""",
}

HARNESS = r"""
package com.happyhome.cc8assistant;
import android.media.*;
import android.os.SystemClock;
import java.io.*;
import java.nio.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

class JSONObject extends HashMap<String,Object> {
 String optString(String name){return String.valueOf(getOrDefault(name,""));}
}
class Wire {
 static final int RENDER=2;
 static class Packet {int generation;byte[] data;Packet(int gen,byte[] data){generation=gen;this.data=data;}}
 static JSONObject event(String name){JSONObject value=new JSONObject();value.put("event",name);return value;}
 static void put(JSONObject value,String key,Object item){value.put(key,item);}
 static byte[] pcm(short[] samples,int count){ByteBuffer b=ByteBuffer.allocate(count*2).order(ByteOrder.LITTLE_ENDIAN);for(int i=0;i<count;i++)b.putShort(samples[i]);return b.array();}
 static short[] samples(byte[] bytes){short[] out=new short[bytes.length/2];ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(out);return out;}
}
public class VoiceSession implements AutoCloseable {
 boolean identityConnected(){return true;}
 static class IdentityFixture { boolean ready(){return true;} }
 final IdentityFixture identity=new IdentityFixture();boolean identityHello=true;
 final Object routeLock=new Object();
 final SpeakerOwner speakerOwner=new SpeakerOwner(NativeSpeaker::new,()->this.foreground);
 volatile boolean foreground=true,recording=true,textSession,visual,announcement;
 volatile int epoch=1,textGeneration,visualGeneration,announcementGeneration;
 volatile long routeEpoch=1;
 int minimumGeneration;
 AudioRecord recorder;
 CaptureWindow captureWindow;
 float inputLevel;
 static class AudioRecord {void stop(){}}
 String pendingVisionNonce="",desktopVisionNonce="",visionRequestId="",pendingPhotoId="";
 boolean photoFollowupSupported,macWindowSupported,vocabularySupported;
 final PhotoFollowupWindow photoFollowup=new PhotoFollowupWindow();
 final WorkflowConsent workflowConsent=new WorkflowConsent();
 final Object desktopCameraAuth=new Object();
 static class Lan {boolean authorizeDesktopCamera(Object auth,String nonce,int generation,String proof){return true;}}
 final Lan lan=new Lan();
 final StatusFeedback statusFeedback=new StatusFeedback();
 Player player;
 PlaybackPresentation playbackPresentation;
 CaptionProgress lastCaption=CaptionProgress.empty();
 long lastPlaybackSpeechFrames,renderFrames;
 int lastPlaybackPaddingFrames;
 String phase="idle";
 final Listener listener=new Listener();
 final List<JSONObject> events=new CopyOnWriteArrayList<>();
 final List<Long> renderPositions=new CopyOnWriteArrayList<>();
 final List<Integer> renderCounts=new CopyOnWriteArrayList<>();
 final List<Long> projectedTimes=new CopyOnWriteArrayList<>(),nativeTimes=new CopyOnWriteArrayList<>();
 volatile Runnable duringDone;
 static class Listener {
  volatile String lastState="idle";volatile long listeningNanos;
  void state(String phase,String message){lastState=phase;if(phase.equals("listening"))listeningNanos=System.nanoTime();}
  void playbackPresentation(PlaybackPresentation value){state(value.phase,value.message);}
  void playback(boolean active){}void microphone(boolean active){}
  void caption(String text,String mood){}void vision(boolean active,String message,boolean preserve){}
  void dotResult(String text,boolean truncated,String title){}void screen(boolean asleep){}
 }
 VoiceSession(){speakerOwner.beginVoice(epoch,()->recording&&epoch==1);}
 void cancelVision(){visual=false;}
 void send(JSONObject event){
  events.add(event);
  if("play_done".equals(event.get("event"))){Runnable action=duringDone;duringDone=null;if(action!=null)action.run();}
 }
 void sendPacket(int type,int generation,long sequence,long frame,long nanos,byte[] bytes){
  yes(type==Wire.RENDER&&sequence==renderPositions.size(),"render sequence");
  renderPositions.add(frame);renderCounts.add(bytes.length/2);
  projectedTimes.add(nanos);nativeTimes.add(player.track.lastTimestampNanos);
 }
 Player make(int generation,int id,boolean report){
  Player p=new Player(generation,"fixture","neutral","assistant",id,new CaptionTimeline(new int[][]{{0,7,0,700}},7),report,report);player=p;return p;
 }
 AudioTrack attach(Player p)throws Exception{
  SpeakerSession.Lease lease=p.outputOwner.output().acquire(p.generation,p.playbackId,p::allowed);
  yes(p.startTrack(lease),"lease startup");return p.track;
 }
 void play(Player p)throws Exception{
  short[] pcm=new short[700];Arrays.fill(pcm,(short)8192);p.offer(Wire.pcm(pcm,pcm.length));p.ended=true;p.start();p.join(3000);yes(!p.isAlive(),"player leaked");
 }
 JSONObject event(String name){for(JSONObject e:events)if(name.equals(e.get("event")))return e;return null;}
 int eventCount(String name){int count=0;for(JSONObject e:events)if(name.equals(e.get("event")))count++;return count;}
 static void yes(boolean value,String why){if(!value)throw new AssertionError(why);}
 static void waitFor(java.util.function.BooleanSupplier condition)throws Exception{
  long until=System.nanoTime()+3000000000L;while(!condition.getAsBoolean()&&System.nanoTime()<until)Thread.sleep(2);yes(condition.getAsBoolean(),"bounded wait expired");
 }
 public void close()throws Exception{
  recording=false;textSession=visual=announcement=false;foreground=false;
  cancelPlayback();speakerOwner.close();
  waitFor(()->AudioTrack.created.stream().allMatch(t->t.releases.get()==1));
 }
 /*ACTUAL_METHODS*/

 static void controls()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   Player pending=s.make(2,3,false);pending.pauseForVoice(10);
   yes(pending.paused&&pending.frozenCaption==null&&s.events.isEmpty(),"early pause receipt");
   AudioTrack ready=s.attach(pending);
   yes(!ready.playing&&pending.frozenCaption!=null&&s.eventCount("playback_paused")==1,"pending pause not applied");
   JSONObject ack=s.event("playback_paused");
   yes((int)ack.get("generation")==2&&(int)ack.get("token")==10,"wrong pause identity");
   yes((long)ack.get("nanos")<=s.listener.listeningNanos,"late pause time");
   pending.pauseForVoice(10);pending.resumeAfterNoise(9);
   yes(s.events.size()==1&&ready.pauses==1,"duplicate or stale control");
   pending.resumeAfterNoise(10);
   yes(ready.playing&&!pending.paused&&pending.frozenCaption==null&&pending.resumeCount==1,"resume state");
   yes((int)s.event("playback_resumed").get("token")==10
    &&(int)s.event("playback_resumed").get("generation")==2&&s.events.size()==2,"resume identity or count");
  }
  try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);p.pauseForVoice(11);p.resumeAfterNoise(11);AudioTrack t=s.attach(p);
   yes(t.playing&&t.pauses==0&&s.events.isEmpty()&&p.resumeCount==0,"withdrawn pause applied");
  }
  for(boolean resume:new boolean[]{false,true})try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);AudioTrack t=s.attach(p);
   if(resume){p.pauseForVoice(12);t.failPlay=true;p.resumeAfterNoise(12);}
   else {t.failPause=true;p.pauseForVoice(12);}
   yes(p.stopped&&p.resumeCount==0&&p.frozenCaption==null,"failed control did not stop");
   yes(s.eventCount("playback_error")==1&&s.eventCount("playback_resumed")==0
    &&s.events.size()==(resume?2:1),"false control receipt");
   yes(s.eventCount("playback_paused")== (resume?1:0),"failed pause acknowledged");
   p.failPlayback(new IllegalStateException());yes(s.eventCount("playback_error")==1,"duplicate error");
  }
  try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);p.pauseForVoice(14);p.cancel();
   SpeakerSession.Lease unused=p.outputOwner.output().acquire(2,3,()->true);
   AudioTrack t=((NativeSpeaker)unused.device).track;int before=t.plays;
   yes(!p.startTrack(unused)&&t.plays==before&&s.events.isEmpty()&&p.cancelAppliedNanos==0,"cancelled startup");
  }
  try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);AudioTrack t=s.attach(p);t.failFlush=true;p.cancel();
   yes(t.pauses>=1&&p.cancelAppliedNanos==0,"flush failure acknowledged");
  }
 }
 static void visuals()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);AudioTrack t=s.attach(p);
   short[] pcm=new short[700];Arrays.fill(pcm,(short)8192);yes(PcmPump.writeAll(pcm,p),"visual samples");
   long base=p.lease.base;t.manualHead=base+100;int before=t.headReads;
   CaptionProgress live=p.visualFrame();
   yes(t.headReads==before+1&&live.level==.25f&&live.position==1&&!live.paused&&live.mood.equals("neutral"),"one live frame");
   t.manualHead=base+200;t.duringRead=()->p.pauseForVoice(15);
   CaptionProgress frozen=p.visualFrame();
   yes(frozen.paused&&frozen.level==0&&frozen.position==2&&frozen.elapsedMs==12,"racing pause did not freeze local frame");
   int reads=t.headReads;t.manualHead=base+300;p.timings=new CaptionTimeline(new int[][]{{0,7,0,350}},7);
   yes(p.visualFrame()==frozen&&t.headReads==reads,"paused frame changed");
   p.resumeAfterNoise(15);CaptionProgress resumed=p.visualFrame();
   yes(!resumed.paused&&resumed.position==6&&resumed.level==.25f,"resume local frame");
   p.pauseForVoice(16);long stop=System.nanoTime();
   synchronized(s.routeLock){s.cancelPlayback();}
   yes(t.flushes>=1&&p.cancelAppliedNanos>=stop&&p.cancelAppliedNanos>=t.firstFlushNanos,"native stop receipt");
   yes(p.visualFrame().id==0&&p.visualFrame().level==0,"stopped visuals retained");
  }
 }
 static void accounting()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,true);s.play(p);JSONObject done=s.event("play_done"),progress=s.event("playout_progress");
   yes(done!=null&&progress!=null&&s.event("playback_error")==null,"player did not complete");
   yes(p.written==700&&s.renderFrames==700&&(long)done.get("played_frames")==700,"warm zeros leaked into receipt");
   yes((long)progress.get("played_frames")<=700&&(long)progress.get("first_signal_frame")==0,"local playout proof");
   long expected=0;for(int i=0;i<s.renderPositions.size();i++){
    yes(s.renderPositions.get(i)==expected,"local render offset");expected+=s.renderCounts.get(i);
    yes(s.projectedTimes.get(i).equals(s.nativeTimes.get(i)),"reference clock did not project from physical clause origin");
   }
   yes(expected==700,"missing render samples");
   JSONObject report=(JSONObject)done.get("render_clock"),sources=(JSONObject)report.get("source_counts");
   yes(((Number)sources.get("hardware")).longValue()>0,"hardware clock used local instead of physical target");
   yes(((Number)report.get("frames")).longValue()==700&&s.lastCaption.elapsedMs==43&&s.lastCaption.position==7,"caption/frame completion");
   AudioTrack t=AudioTrack.created.get(0);yes(t.bytes==3200&&t.volume==.35f&&t.modernWrites>0&&t.legacyWrites==0,"native output configuration");
   yes(t.releases.get()==0&&t.flushes==0,"normal clause released track");
  }
 }
 static void prefixVisuals()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);AudioTrack track=s.attach(p);
   short[] pcm=new short[700];Arrays.fill(pcm,(short)8192);yes(PcmPump.writeAll(pcm,p),"queued content");
   track.manualHead=p.lease.base-160;
   CaptionProgress waiting=p.visualFrame();
   yes(waiting.level==0&&waiting.elapsedMs==0&&waiting.position==0,"mouth moved for queued warm samples");
   track.manualHead=p.lease.base+100;
   yes(p.visualFrame().level==.25f&&p.visualFrame().position==1,"mouth did not follow actual content");
  }
 }
 static void handover()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   Player first=s.make(2,3,false);AtomicReference<Player> next=new AtomicReference<>();
   // Wire receive/control work runs on a different thread from Player.send().
   s.duringDone=()->CompletableFuture.runAsync(()->{
    s.cancelPlayback();Player second=s.make(2,4,false);next.set(second);
    try{s.attach(second);}catch(Exception error){throw new AssertionError(error);}
   }).join();
   s.play(first);Player second=next.get();AudioTrack track=second.track;
   yes(s.player==second&&second.allowed()&&second.head()==0,"old finally cleared new player");
   yes(AudioTrack.created.size()==1&&track.flushes==0&&track.plays==1&&track.releases.get()==0,"late cleanup reset reused track");
   yes(second.outputOwner.output().write(second.lease,new short[]{1},0,1)==1,"new lease damaged");
  }
 }
 static void stopped()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   s.stopMicrophone();s.textSession=true;s.textGeneration=9;
   waitFor(()->AudioTrack.created.stream().allMatch(t->t.releases.get()==1));int opened=AudioTrack.created.size();
   Player late=s.make(9,4,false);s.play(late);
   yes(s.event("play_done")==null&&s.event("playback_error")==null&&s.renderFrames==0,"late playback published");
   yes(AudioTrack.created.size()==opened&&s.speakerOwner.forPlayback(9)==null,"late playback recreated output");
   s.beginTaskOutput("text",9);Player allowed=s.make(9,5,false);s.play(allowed);
   yes(s.event("play_done")!=null&&AudioTrack.created.size()==opened+1,"fresh explicit task could not play");
   int done=s.eventCount("play_done");s.stopMicrophone();
   yes(s.textSession&&s.speakerOwner.forPlayback(9)==null,"Stop kept output for lingering text state");
   s.play(s.make(9,6,false));
   yes(s.eventCount("play_done")==done&&AudioTrack.created.size()==opened+1,"late text replay after Stop");
   s.text_done(9);waitFor(()->AudioTrack.created.stream().allMatch(t->t.releases.get()==1));
   yes(s.speakerOwner.forPlayback(9)==null,"text completion kept idle output");
  }
 }
 static void tasks()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   s.stopMicrophone();s.announcement=true;s.announcementGeneration=6;s.beginTaskOutput("announcement",6);
   SpeakerOwner.Ticket old=s.speakerOwner.forPlayback(6);yes(old!=null,"announcement not armed");
   s.textSession=true;s.textGeneration=7;s.beginTaskOutput("text",7);
   SpeakerOwner.Ticket current=s.speakerOwner.forPlayback(7);
   s.speakerOwner.endVoice(1);s.speakerOwner.endTask(6);s.speakerOwner.endTasksThrough(6);
   yes(current!=null&&current.allows()&&!old.allows(),"old cleanup closed new task");
   s.text_done(7);yes(!current.allows(),"text task did not close");
   s.visual=true;s.visualGeneration=8;s.beginTaskOutput("vision",8);
   SpeakerOwner.Ticket vision=s.speakerOwner.forPlayback(8);yes(vision!=null,"vision output not armed");
   s.vision_done(8);yes(!vision.allows(),"vision completion did not close");
   s.announcement=true;s.announcementGeneration=9;s.beginTaskOutput("announcement",9);
   SpeakerOwner.Ticket announcement=s.speakerOwner.forPlayback(9);s.announcement_done(9);
   yes(announcement!=null&&!announcement.allows(),"announcement completion did not close");
  }
 }
 static void voiceTask()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   SpeakerOwner.Ticket voice=s.speakerOwner.forPlayback(2);
   s.textSession=true;s.textGeneration=6;s.beginTaskOutput("text",6);s.text_done(6);
   yes(s.speakerOwner.forPlayback(7)==voice&&voice.allows(),"task completion closed active voice output");
   s.interrupt();yes(voice.allows(),"interrupt closed listening output");
   s.stopMicrophone();yes(!voice.allows(),"manual stop kept voice output");
  }
 }
 static void legacy()throws Exception{
  android.os.Build.VERSION.SDK_INT=21;try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);s.play(p);AudioTrack t=AudioTrack.created.get(0);
   yes(s.event("play_done")!=null&&t.legacyWrites>0&&t.modernWrites==0,"legacy API guard");
   yes(s.lastPlaybackSpeechFrames==700,"legacy speech count");
  }
 }
 static void initFailure(String kind)throws Exception{
  if(kind.equals("invalid"))AudioTrack.invalid=true;
  if(kind.equals("volume"))AudioTrack.volumeError=true;
  if(kind.equals("minimum"))AudioTrack.minimum=-2;
  if(kind.equals("volume_result"))AudioTrack.volumeResult=-3;
  try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);s.play(p);
   yes(s.eventCount("playback_error")==1&&s.event("play_done")==null,"failed native initialization acknowledged");
   waitFor(()->AudioTrack.created.stream().allMatch(t->t.releases.get()==1));
  }
 }
 static void route()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   Player old=s.make(2,3,false);AudioTrack track=s.attach(old);
   s.routeEpoch=2;
   // The output worker independently cancels a lease whose route is gone.
   // Observe its complete reset before checking that stale controls add nothing.
   waitFor(()->track.flushes>=2);int pauses=track.pauses;
   old.pauseForVoice(10);old.resumeAfterNoise(10);
   old.failPlayback(new IOException("obsolete route"));
   yes(!old.allowed()&&track.pauses==pauses&&s.events.isEmpty(),"stale route applied controls");
   s.cancelPlayback();Player next=s.make(2,4,false);
   Wire.Packet stale=new Wire.Packet(2,new byte[]{1,0});
   s.speaker(stale,1);yes(next.queue.isEmpty(),"old route PCM entered new same-generation player");
   s.speaker(stale,2);yes(next.queue.size()==1,"current route PCM rejected");
  }
 }
 static void visionOutput()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   s.stopMicrophone();
   s.vision_begin(6,"desktop");
   yes(s.visual&&s.speakerOwner.forPlayback(6)==null,"desktop photo created audio output");
   s.vision_done(6);s.pendingVisionNonce="public-test-nonce";
   s.vision_begin(7,"button");
   yes(s.visual&&s.speakerOwner.forPlayback(7)!=null,"explicit scene analysis has no audio owner");
   s.vision_done(7);yes(s.speakerOwner.forPlayback(7)==null,"scene analysis output leaked");
  }
 }
 static void avatarMotion()throws Exception{
  try(VoiceSession s=new VoiceSession()){
   Player p=s.make(2,3,false);AudioTrack track=s.attach(p);
   short[] pcm=new short[700];
   for(int i=0;i<pcm.length;i++)pcm[i]=(short)(i<300||i%2==0?8192:-8192);
   yes(PcmPump.writeAll(pcm,p),"accepted articulation fixture");
   SpeechMotion motion=new SpeechMotion();
   track.manualHead=p.lease.base-160;
   CaptionProgress warm=p.visualFrame();
   motion.update(warm.speechOwner,warm.id,warm.elapsedMs,warm.level,warm.speechDetail,warm.playing,warm.paused);
   yes(motion.opening==0&&warm.speechDetail==0,"queued PCM animated before physical origin");
   track.manualHead=p.lease.base+100;
   CaptionProgress low=p.visualFrame();
   motion.update(low.speechOwner,low.id,low.elapsedMs,low.level,low.speechDetail,low.playing,low.paused);
   yes(low.level==.25f&&low.speechDetail==0&&motion.opening>0,"accepted low-detail speech missing");
   track.manualHead=p.lease.base+500;
   CaptionProgress high=p.visualFrame();
   yes(high.level==.25f&&high.speechDetail>.9f,"shape did not use the same physical envelope");
   motion.update(high.speechOwner,high.id,high.elapsedMs,high.level,high.speechDetail,high.playing,high.paused);
   yes(motion.spread>0&&motion.spread<1,"shape did not transition smoothly");
   p.pauseForVoice(31);CaptionProgress pause=p.visualFrame();
   motion.update(pause.speechOwner,pause.id,pause.elapsedMs,pause.level,pause.speechDetail,pause.playing,pause.paused);
   yes(pause.level==0&&pause.speechDetail==0&&motion.opening==0&&motion.spread==0,"paused mouth not closed");
   p.resumeAfterNoise(31);CaptionProgress resume=p.visualFrame();
   motion.update(resume.speechOwner,resume.id,resume.elapsedMs,resume.level,resume.speechDetail,resume.playing,resume.paused);
   yes(motion.opening>0,"resume did not follow actual still-owned audio");
   p.cancel();CaptionProgress cancelled=p.visualFrame();
   motion.update(cancelled.speechOwner,cancelled.id,cancelled.elapsedMs,cancelled.level,cancelled.speechDetail,cancelled.playing,cancelled.paused);
   yes(cancelled.id==0&&motion.opening==0&&motion.spread==0,"cancel retained articulation");
   Player replacement=s.make(2,3,false);
   yes(replacement.playbackId==p.playbackId&&replacement.speechOwner!=p.speechOwner,
    "reused server playback ID shared a local speech owner");
  }
 }
 public static void main(String[] args)throws Exception{
  switch(args[0]){
   case "controls":controls();break;
   case "visuals":visuals();break;
   case "accounting":accounting();break;
   case "prefix_visuals":prefixVisuals();break;
   case "avatar_motion":avatarMotion();break;
   case "handover":handover();break;
   case "stopped":stopped();break;
   case "tasks":tasks();break;
   case "voice_task":voiceTask();break;
   case "legacy":legacy();break;
   case "route":route();break;
   case "vision_output":visionOutput();break;
   case "invalid":case "volume":case "minimum":case "volume_result":initFailure(args[0]);break;
   default:throw new AssertionError("unknown case");
  }
  System.out.println(args[0]+" passed");
 }
}
"""


class SpeakerHarness:
    def __init__(self, *, cancel_interleave=False):
        self.java = require_java()
        self.folder = tempfile.TemporaryDirectory(prefix='cc8-speaker-integration-')
        try:
            folder = Path(self.folder.name)
            source = (JAVA / 'VoiceSession.java').read_text()
            signatures = (
                'private final class Player',
                'private static final class Envelope',
                'static final class CaptionProgress',
                'static final class PlaybackPresentation',
                'private void publishPlayback(Player owner, String text, String nextPhase, String message)',
                'private synchronized void cancelPlayback()',
                'private synchronized void beginTaskOutput(String role, int generation)',
                'void stopMicrophone()',
                'private boolean stopMicrophone(CaptureWindow expected)',
                'void interrupt()',
                'boolean currentRoute(long route)',
            )
            actual = '\n'.join(method(source, signature) for signature in signatures)
            if cancel_interleave:
                # A derived scheduling fixture: run the actual Player with one latch checkpoint
                # after it publishes stopped. Native operations and receipt logic stay unchanged.
                marker = '      stopped = true;'
                if actual.count(marker) != 1:
                    raise AssertionError('Cancellation scheduling point must be unique')
                actual = actual.replace(
                    marker,
                    marker + '\nif(lease!=null)((NativeSpeaker)lease.device).track.awaitReset();',
                )
            signature = '} else if (p.type == Wire.SPEAKER) {'
            start = source.index(signature) + len(signature)
            end, depth = start, 1
            while depth:
                depth += (source[end] == '{') - (source[end] == '}')
                end += 1
            actual += (
                '\nvoid speaker(Wire.Packet p,long route)throws Exception{'
                + source[start : end - 1]
                + '}\n'
            )
            for event in ('text_done', 'announcement_done', 'vision_done'):
                actual += (
                    '\nvoid '
                    + event
                    + '(int gen){class Packet{int generation=gen;}Packet p=new Packet();'
                    + 'JSONObject j=new JSONObject();j.put("result","completed");'
                    + branch(source, event)
                    + '}\n'
                )
            actual += (
                '\nvoid vision_begin(int gen,String origin){long route=routeEpoch;class Packet{int generation=gen;}Packet p=new Packet();'
                + 'JSONObject j=new JSONObject();j.put("origin",origin);j.put("nonce","public-test-nonce");'
                + branch(source, 'vision_begin')
                + '}\n'
            )
            generated = folder / 'VoiceSession.java'
            generated.write_text(HARNESS.replace('/*ACTUAL_METHODS*/', actual))
            files = [generated]
            for name, contents in STUBS.items():
                path = folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
                files.append(path)
            files += [
                JAVA / (name + '.java')
                for name in (
                    'SpeakerSession',
                    'SpeakerOwner',
                    'NativeSpeaker',
                    'PcmPump',
                    'PlaybackDrain',
                    'CaptionTimeline',
                    'PlaybackClock',
                    'RenderClockStats',
                    'PlayoutProbe',
                    'StatusFeedback',
                    'CaptureWindow',
                    'CaptureClockStats',
                    'PhotoFollowupWindow',
                    'WorkflowConsent',
                    'SpeechMotion',
                )
            ]
            result = subprocess.run(
                [
                    str(self.java / 'javac'),
                    '-encoding',
                    'UTF-8',
                    '-d',
                    str(folder),
                    *map(str, files),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                raise AssertionError(result.stderr)
        except BaseException:
            self.folder.cleanup()
            raise

    def close(self):
        self.folder.cleanup()

    def run(self, scenario):
        return subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                self.folder.name,
                'com.happyhome.cc8assistant.VoiceSession',
                scenario,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
