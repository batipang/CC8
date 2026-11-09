"""Compile actual route lifecycle/dispatch and queued Activity callbacks without Android I/O."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_interrupted_task_notice import HARNESS, method
from test_error_completion_feedback import branch
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
ACTIVITY = JAVA / 'MainActivity.java'
SESSION = JAVA / 'VoiceSession.java'

SESSION_INTERFACES = r"""
 static class Session {
  final Object routeLock=new Object();
  boolean foreground=true,recording,visual,announcement,textSession,receiverRegistered;
  boolean dotChatSupported,savedTasksSupported,preferenceMemorySupported,resultProvenanceSupported,resultReadingSupported;
  JSONObject voicePreferences,speechCorrection;
  int speechCorrectionRevision=-1;
  int epoch,minimumGeneration,lifecycle,requests,announcementGeneration,textGeneration;
  CaptureWindow captureWindow;
  int visualGeneration;String visionRequestId="",pendingVisionNonce="",desktopVisionNonce="";
  volatile long routeEpoch;
  Link activeLink,usbLink,wifiLink;
  Socket socket;DataOutputStream output;ServerSocket server;
  String phase="idle",wakeMode="wake_word",wifiError="";
  ActivityCheck listener;
  final Object usbState=new Object();
  final Context context=new Context();final Lan lan=new Lan();
  final Auth desktopCameraAuth=new Auth();final StatusFeedback statusFeedback=new StatusFeedback();
  final HostActivation hostActivation=new HostActivation();
  final ConnectionAvailability availability=new ConnectionAvailability();
  final java.util.concurrent.atomic.AtomicInteger nativeOpens=new java.util.concurrent.atomic.AtomicInteger();
  final SpeakerOwner speakerOwner=new SpeakerOwner(()->{
   nativeOpens.incrementAndGet();return new SpeakerSession.Device(){
    public void play(){}public void pause(){}public void flush(){}public void release(){}
    public int write(short[] data,int offset,int count){try{Thread.sleep(1);}catch(InterruptedException e){Thread.currentThread().interrupt();return 0;}return count;}
   };
  },()->foreground);
  void cancelVision(){visual=false;}void stopMicrophone(){recording=false;captureWindow=null;}
  boolean stopMicrophone(CaptureWindow expected){
   if(captureWindow!=expected)return false;stopMicrophone();return true;
  }
  int cleanupDelayMs;
  void cancelPlayback(){SystemClock.now+=cleanupDelayMs;}
  boolean receiveHello(Link link,JSONObject hello)throws IOException{return captureHello(link,hello);}
  void receiveCaptureStart(JSONObject receipt,long route){confirmCaptureStart(receipt,route);}
  void microphonePairFrame(int captureEpoch)throws IOException{sendPacket(Wire.MIC_PAIRED,captureEpoch,0,0,10,new byte[640]);}
  void microphoneFrame(int captureEpoch)throws IOException{
   sendPacket(Wire.MIC,captureEpoch,0,0,10,new byte[320]);
  }
  void send(JSONObject j){try{sendPacket(Wire.EVENT,epoch,0,0,10,Wire.jsonBytes(j));}catch(IOException ignored){}}
  String transportName(){return activeLink==null?"none":activeLink.kind;}
  void workspaceGet(){requests++;}
  ROUTE_METHODS
 }
 static class Context{void unregisterReceiver(Object receiver){}}
 static class Lan{int configurations;void cancelPending(){}void release(){}void acquire(){}void configure(JSONObject data){configurations++;}}
 static class Auth{void reset(){}String challenge(){return "public-test-challenge";}}
 static class BuildConfig{static final int CC8_PROTOCOL=2;}
 static class SystemClock{static long now;static long elapsedRealtime(){return now;}}
 static class Link{
  boolean closed,ready,pairedCaptureRequested,outputBound;int ticket;String kind,captureStartToken,routeToken;
  Socket connection;DataOutputStream out;
  Link(String name){kind=name;}
  final List<JSONObject> events=new ArrayList<>();
  final List<Wire.Packet> packets=new ArrayList<>(),queuedCamera=new ArrayList<>();
  Runnable failOffer;
  void failIfRequested()throws IOException{
   if(failOffer!=null){failOffer.run();throw new IOException("fixture write failure");}
  }
  void event(JSONObject data)throws IOException{failIfRequested();events.add(data);}
  void cameraEvent(JSONObject data,int gen,String ident,long route)throws IOException{
   event(data);Wire.Packet packet=new Wire.Packet(data);packet.cameraGeneration=gen;
   packet.cameraRequestId=ident;packet.cameraRoute=route;queuedCamera.add(packet);
  }
  void close(){closed=true;}
  void offer(Wire.Packet packet)throws IOException{failIfRequested();packets.add(packet);}
 }
 static class Wire{
  static final int EVENT=1,MIC=2,MIC_PAIRED=5,ROUTE_BOUND=6;
  static boolean block,blockWrite;static int parses,blockAt=2,writes;
  static void write(DataOutputStream out,int type,int generation,long sequence,long frame,long nanos,Object data)throws IOException{
   writes++;if(blockWrite){entered.countDown();try{if(!release.await(2,java.util.concurrent.TimeUnit.SECONDS))throw new AssertionError("write barrier timeout");}catch(InterruptedException e){throw new IOException(e);}}
  }
  static final java.util.concurrent.CountDownLatch entered=new java.util.concurrent.CountDownLatch(1),release=new java.util.concurrent.CountDownLatch(1);
  static class Packet{
   int type=EVENT,generation=7,cameraGeneration,innerType;Object data;long sequence,frame,nanos,cameraRoute,outgoingRoute=-1;
   String cameraRequestId,routeToken;
   Packet(){}Packet(JSONObject value){data=value;}
  }
  static void bindRoute(Packet packet,String token){packet.routeToken=token;packet.innerType=packet.type;packet.type=ROUTE_BOUND;}
  static Packet unbindRoute(Packet packet,String token){
   if(packet.type!=ROUTE_BOUND||token==null||!token.equals(packet.routeToken))return null;
   Packet result=new Packet();result.type=packet.innerType;result.generation=packet.generation;result.data=packet.data;
   result.sequence=packet.sequence;result.frame=packet.frame;result.nanos=packet.nanos;return result;
  }
  static JSONObject json(Packet p)throws Exception{
   parses++;
   if(block&&parses==blockAt){entered.countDown();if(!release.await(2,java.util.concurrent.TimeUnit.SECONDS))throw new AssertionError("fixture barrier timeout");}
   return (JSONObject)p.data;
  }
  static JSONObject event(String name){return new JSONObject("event",name);}
  static byte[] jsonBytes(JSONObject value){return new byte[]{1};}
  static void put(JSONObject data,String key,Object value){data.values.put(key,value);}
 }
 JSONObject cameraRequest;
 void cameraCapture(JSONObject value){cameraRequest=value;}
 void vision(boolean active,String message,boolean preserve){}
 void speechCorrection(JSONObject state,long route){}
 int preferenceReceipts;
 void preferenceResult(JSONObject data,long route){preferenceReceipts++;}
 void announcement(){}
 void captureRejected(int stoppedEpoch){}
 void playback(boolean active){}void caption(String value,String mood){}void state(String value,String message){}
 final Queue<Runnable> ui=new java.util.concurrent.ConcurrentLinkedQueue<>();
 void drain(){Runnable r;while((r=ui.poll())!=null)r.run();}
 static Wire.Packet oldWorkspace(){return new Wire.Packet(new JSONObject("event","workspace","revision",99,"tasks",new JSONArray(task("completed")),"reminders",new JSONArray()));}
 static void duplex(ActivityCheck c,Link link,String token,long serial)throws Exception{
  if("usb".equals(link.kind))c.session.usbLink=link;else c.session.wifiLink=link;
  c.session.deliver(new Wire.Packet(new JSONObject("event","transport_activate","route_token",token,
   "route_output_version",1,"route_owner","e".repeat(32),"route_serial",serial)),link);
 }
 static void activate(ActivityCheck c,Link link)throws Exception{
  if("usb".equals(link.kind))c.session.usbLink=link;else c.session.wifiLink=link;
  c.session.select(link);
 }
"""

SCENARIOS = r"""
 public static void main(String[] args)throws Exception{
  ActivityCheck c=new ActivityCheck();Session s=c.session;s.listener=c;
  Link old=new Link("usb");activate(c,old);c.drain();
  c.snapshot(1,task("running"));c.drain();
  try{switch(args[0]){
   case "preference_requests":{
    long route=s.routeEpoch;int sent=old.events.size();
    JSONObject argsValue=new JSONObject("action","put","revision",0,"key","称呼","value","固定公开值");
    yes(!s.preferenceRequest("a".repeat(32),argsValue,route)&&old.events.size()==sent,"unsupported peer accepted memory write");
    s.preferenceMemorySupported=true;
    yes(s.preferenceRequest("a".repeat(32),argsValue,route),"supported current request not sent");
    JSONObject offered=old.events.get(old.events.size()-1);
    yes("preference_request".equals(offered.optString("event"))&&"a".repeat(32).equals(offered.optString("request_id"))&&"put".equals(offered.optString("action")),"memory request envelope changed");
    int count=old.events.size();argsValue.values.put("event","session_start");
    yes(!s.preferenceRequest("b".repeat(32),argsValue,route)&&old.events.size()==count,"reserved event override was sent");
    argsValue.values.remove("event");Link wifi=new Link("wifi");activate(c,wifi);s.preferenceMemorySupported=true;int nextCount=wifi.events.size();
    yes(!s.preferenceRequest("c".repeat(32),argsValue,route)&&wifi.events.size()==nextCount,"old editor wrote through new route");
    yes(!s.recording&&!s.visual&&!s.announcement,"memory request started resources");break;
   }
   case "preference_receipts":{
    s.deliver(new Wire.Packet(new JSONObject("event","preference_result","request_id","a".repeat(32))),old);
    yes(c.preferenceReceipts==1,"current preference receipt lost");
    Link wifi=new Link("wifi");activate(c,wifi);
    s.deliver(new Wire.Packet(new JSONObject("event","preference_result","request_id","a".repeat(32))),old);
    yes(c.preferenceReceipts==1,"inactive link retagged old memory receipt");
    s.deliver(new Wire.Packet(new JSONObject("event","preference_result","request_id","b".repeat(32))),wifi);
    yes(c.preferenceReceipts==2,"new route cannot receive own memory receipt");
    s.pause();s.deliver(new Wire.Packet(new JSONObject("event","preference_result","request_id","c".repeat(32))),wifi);
    yes(c.preferenceReceipts==2&&!s.preferenceMemorySupported,"Home retained preference capability/receipt");break;
   }
   case "availability_handover":{
    s.availability.begin(0);s.select(old);Link wifi=new Link("wifi");wifi.ready=true;s.wifiLink=wifi;
    SystemClock.now=1000;s.lost(old);
    yes(s.activeLink==wifi&&"connected".equals(s.availability.snapshot(2000).state),"Ready fallback invented unavailability");
    s.lost(old);yes(s.availability.snapshot(3000).recoveredCount==0,"Inactive loss created an outage receipt");break;
   }
   case "availability_loss":{
    s.availability.begin(0);s.select(old);s.cleanupDelayMs=2500;SystemClock.now=1000;s.lost(old);
    s.cleanupDelayMs=0;SystemClock.now=4000;s.lost(old);
    yes(s.availability.snapshot(4500).unavailableMs==3500,"Cleanup/repeated loss shifted the first observed outage boundary");
    SystemClock.now=5000;Link next=new Link("usb");activate(c,next);s.select(next);
    yes(s.availability.snapshot(6000).lastRecoveredMs==4000&&s.availability.snapshot(6000).recoveredCount==1,"Activation did not finish exactly one observed gap");break;
   }
   case "availability_failed_activation":{
    s.availability.begin(0);s.activeLink=null;s.usbLink=null;
    Link next=new Link("wifi");next.failOffer=()->{};
    try{s.select(next);throw new AssertionError("Fixture activation did not fail");}catch(IOException expected){}
    yes("waiting".equals(s.availability.snapshot(1000).state)&&s.availability.snapshot(1000).recoveredCount==0,"Unconfirmed activation was counted as available");break;
   }
   case "availability_pause":{
    s.availability.begin(0);s.select(old);SystemClock.now=1000;s.lost(old);SystemClock.now=2000;s.pause();
    s.select(new Link("usb"));
    yes("not_observing".equals(s.availability.snapshot(1000000).state),"Background route callback resumed observation");
    yes(s.availability.snapshot(1000000).interruptedCount==1&&s.availability.snapshot(1000000).recoveredCount==0,"Home converted an unknown interval into confirmed recovery");break;
   }
   case "old_activation_control":{
    duplex(c,old,"a".repeat(32),1);Link wifi=new Link("wifi");duplex(c,wifi,"b".repeat(32),2);
    long route=s.routeEpoch;duplex(c,old,"a".repeat(32),1);
    yes(s.activeLink==wifi&&s.routeEpoch==route&&"b".repeat(32).equals(wifi.routeToken),"late activation replaced newer route");
    duplex(c,wifi,"c".repeat(32),2);
    yes(s.routeEpoch==route&&"b".repeat(32).equals(wifi.routeToken),"duplicate activation changed live token");
    s.deliver(new Wire.Packet(new JSONObject("event","transport_activate","route_token","d".repeat(32))),wifi);
    yes(s.routeEpoch==route&&wifi.outputBound,"unversioned activation downgraded duplex socket");break;
   }
   case "host_parse_race":{
    duplex(c,old,"a".repeat(32),1);
    Wire.Packet stale=new Wire.Packet(new JSONObject("event","announcement_begin"));Wire.bindRoute(stale,"a".repeat(32));
    Wire.blockAt=Wire.parses+1;Wire.block=true;List<Throwable> errors=Collections.synchronizedList(new ArrayList<>());
    Thread reader=new Thread(()->{try{s.deliver(stale,old);}catch(Throwable e){errors.add(e);}});reader.start();
    try{
     yes(Wire.entered.await(2,java.util.concurrent.TimeUnit.SECONDS),"bound control did not reach parse barrier");
     duplex(c,new Link("wifi"),"b".repeat(32),2);duplex(c,old,"c".repeat(32),3);
    }finally{Wire.release.countDown();reader.join(3000);}
    yes(!reader.isAlive()&&errors.isEmpty(),"bound control reader failed");
    yes(!s.announcement&&s.speakerOwner.forPlayback(7)==null&&s.nativeOpens.get()==0,"bound control was retagged during parse");break;
   }
   case "host_restart":{
    duplex(c,old,"a".repeat(32),10);Link next=new Link("wifi");
    s.deliver(new Wire.Packet(new JSONObject("event","transport_activate","route_token","d".repeat(32),"route_output_version",1,"route_owner","f".repeat(32),"route_serial",1)),next);
    long route=s.routeEpoch;yes(s.activeLink==next&&!s.recording,"new host did not activate without enabling microphone");
    duplex(c,old,"b".repeat(32),11);
    yes(s.activeLink==next&&s.routeEpoch==route,"retired host reactivated old socket");
    Wire.Packet fresh=new Wire.Packet(new JSONObject("event","announcement_begin"));fresh.generation=1;Wire.bindRoute(fresh,"d".repeat(32));s.deliver(fresh,next);
    yes(s.announcement&&s.speakerOwner.forPlayback(1)!=null,"new host's reset generation was rejected");break;
   }
   case "invalid_activation":{
    duplex(c,old,"a".repeat(32),1);long route=s.routeEpoch;
    for(Object serial:new Object[]{true,0,-1,1.5,"2"}){
     boolean rejected=false;
     try{s.deliver(new Wire.Packet(new JSONObject("event","transport_activate","route_token","b".repeat(32),"route_output_version",1,"route_owner","e".repeat(32),"route_serial",serial)),old);}
     catch(IOException|IllegalArgumentException expected){rejected=true;}
     yes(rejected&&s.routeEpoch==route&&"a".repeat(32).equals(old.routeToken),"invalid activation changed route before validation");
    }
    duplex(c,old,"c".repeat(32),2);yes(s.routeEpoch>route&&old.outputBound,"invalid control consumed valid successor");break;
   }
   case "bound_lan_config":{
    duplex(c,old,"a".repeat(32),1);
    Wire.Packet config=new Wire.Packet(new JSONObject("event","lan_config","config",new JSONObject()));
    s.deliver(config,old);yes(s.lan.configurations==0,"unwrapped stale config was accepted");
    Wire.bindRoute(config,"b".repeat(32));s.deliver(config,old);yes(s.lan.configurations==0,"old token config was accepted");
    Wire.Packet fresh=new Wire.Packet(new JSONObject("event","lan_config","config",new JSONObject()));Wire.bindRoute(fresh,"a".repeat(32));
    s.deliver(fresh,old);yes(s.lan.configurations==1,"fresh bound config did not apply");
    Wire.Packet spoof=new Wire.Packet(new JSONObject("event","transport_activate","route_token","b".repeat(32)));Wire.bindRoute(spoof,"a".repeat(32));
    long route=s.routeEpoch;s.deliver(spoof,old);yes(s.routeEpoch==route&&old.outputBound,"application envelope changed transport role");break;
   }
   case "old_host_after_reuse":{
    duplex(c,old,"a".repeat(32),1);
    Wire.Packet stale=new Wire.Packet(new JSONObject("event","announcement_begin"));stale.generation=7;
    duplex(c,new Link("wifi"),"b".repeat(32),2);duplex(c,old,"c".repeat(32),3);
    s.deliver(stale,old);
    yes(!s.announcement&&s.speakerOwner.forPlayback(7)==null&&s.nativeOpens.get()==0,"old unwrapped host begin armed actual output owner");
    Wire.bindRoute(stale,"a".repeat(32));s.deliver(stale,old);
    yes(!s.announcement&&s.speakerOwner.forPlayback(7)==null&&s.nativeOpens.get()==0,"old bound host begin armed actual output owner");
    Wire.Packet fresh=new Wire.Packet(new JSONObject("event","announcement_begin"));fresh.generation=8;Wire.bindRoute(fresh,"c".repeat(32));
    s.deliver(fresh,old);
    SpeakerOwner.Ticket ticket=s.speakerOwner.forPlayback(8);
    yes(s.announcement&&ticket!=null,"fresh bound begin did not reach actual output owner");
    SpeakerSession.Lease lease=ticket.output().acquire(8,1,()->true);
    yes(ticket.output().write(lease,new short[]{1000},0,1)==1,"fresh output lease could not write synthetic sample");
    yes(s.nativeOpens.get()==1,"output owner acquired more than once");break;
   }

   case "route_queue":{
    s.activateRoute(old,"a".repeat(32),false);
    Wire.Packet mic=new Wire.Packet();mic.type=Wire.MIC;s.ownPacket(mic,old);
    Wire.Packet control=new Wire.Packet();s.ownPacket(control,old);
    yes(s.queuedPacketCurrent(mic,old)&&"a".repeat(32).equals(mic.routeToken),"fresh packet not bound");
    Link wifi=new Link("wifi");s.activateRoute(wifi,"b".repeat(32),false);
    yes(!s.queuedPacketCurrent(mic,old)&&!s.queuedPacketCurrent(control,old),"old queued data survived switch");
    s.activateRoute(old,"c".repeat(32),false);
    yes(!s.queuedPacketCurrent(mic,old)&&!s.queuedPacketCurrent(control,old),"same socket reused old queue");
    s.writeQueuedPacket(mic,old);yes(Wire.writes==0,"stale queue reached actual write helper");
    Wire.Packet fresh=new Wire.Packet();fresh.type=Wire.MIC;s.ownPacket(fresh,old);
    yes(s.queuedPacketCurrent(fresh,old)&&"c".repeat(32).equals(fresh.routeToken),"new data rejected");
    s.writeQueuedPacket(fresh,old);yes(Wire.writes==1,"fresh queue did not write once");
    long route=s.routeEpoch;s.activateRoute(old,"d".repeat(32),false);
    yes(s.routeEpoch>route&&!s.queuedPacketCurrent(fresh,old),"new token on same socket did not fence queue");break;
   }
   case "write_after_admission":{
    s.activateRoute(old,"a".repeat(32),false);
    Wire.Packet mic=new Wire.Packet();mic.type=Wire.MIC;s.ownPacket(mic,old);
    Wire.blockWrite=true;List<Throwable> errors=Collections.synchronizedList(new ArrayList<>());
    Thread writer=new Thread(()->{try{s.writeQueuedPacket(mic,old);}catch(Throwable e){errors.add(e);}});
    writer.start();try{
     yes(Wire.entered.await(2,java.util.concurrent.TimeUnit.SECONDS),"writer did not enter real write helper");
     Thread handover=new Thread(()->{try{s.activateRoute(new Link("wifi"),"b".repeat(32),false);}catch(Throwable e){errors.add(e);}});
     handover.start();handover.join(1000);
     yes(!handover.isAlive(),"socket writer held route lock across blocking I/O");
     yes("a".repeat(32).equals(mic.routeToken),"already admitted packet was retagged");
     yes(!s.queuedPacketCurrent(mic,old),"old packet remained current after handover");
    }finally{Wire.release.countDown();writer.join(2000);}
    yes(!writer.isAlive()&&errors.isEmpty(),"writer/route fixture failed");break;
   }
   case "late_audio_begin_parse":{
    Wire.block=true;
    List<Throwable> errors=Collections.synchronizedList(new ArrayList<>());
    Thread reader=new Thread(()->{try{s.deliver(new Wire.Packet(new JSONObject("event","announcement_begin")),old);}catch(Throwable e){errors.add(e);}});
    reader.start();
    try{
     yes(Wire.entered.await(1,java.util.concurrent.TimeUnit.SECONDS),"audio control did not reach parse barrier");
     activate(c,new Link("wifi"));
    }finally{Wire.release.countDown();reader.join(3000);}
    yes(!reader.isAlive()&&errors.isEmpty(),"audio control reader failed");
    yes(!s.announcement&&s.speakerOwner.forPlayback(7)==null,"old route enabled announcement after control parse");
    break;
   }
   case "late_loss":case "late_reconnect":case "same_link_reactivated":case "same_link_control_parse":{
    Wire.block=true;
    if("same_link_control_parse".equals(args[0]))Wire.blockAt=1;
    List<Throwable> errors=Collections.synchronizedList(new ArrayList<>());
    Thread reader=new Thread(()->{try{s.deliver(oldWorkspace(),old);}catch(Throwable e){errors.add(e);}});
    reader.start();
    try{
     yes(Wire.entered.await(1,java.util.concurrent.TimeUnit.SECONDS),"fixture did not reach post-guard parse");
     if(args[0].startsWith("same_link")){activate(c,new Link("wifi"));activate(c,old);}
     else{s.lost(old);if("late_reconnect".equals(args[0]))activate(c,new Link("usb"));}
     c.drain();
     if(!"late_loss".equals(args[0])){c.snapshot(0);c.drain();}
    }finally{Wire.release.countDown();reader.join(3000);}
    yes(!reader.isAlive()&&errors.isEmpty(),"fixture reader did not finish cleanly");
    c.drain();
    if(!"late_loss".equals(args[0])){c.snapshot(1);c.drain();}
    yes(c.tasksData.length()==0,"Late workspace must not repopulate or poison a new route revision");
    yes(c.taskResultUnconfirmed,"Unknown running work remains visible despite a late old completion");
    break;
   }
   case "queued_workspace_loss":
    s.deliver(oldWorkspace(),old);s.lost(old);c.drain();
    yes(c.tasksData.length()==0&&!c.linked,"Loss must clear the old workspace");
    yes(c.taskResultUnconfirmed,"Queued old completion must not hide interrupted running work");break;
   case "queued_link_after_pause":{
    activate(c,new Link("wifi"));Runnable pending=c.ui.poll();
    yes(pending!=null,"fixture needs a queued link callback");
    s.pause();c.drain();pending.run();
    yes(!c.linked,"Queued old link callback must not reconnect the background UI");break;
   }
   case "backup_without_false_callback":{
    Link wifi=new Link("wifi");wifi.ready=true;s.wifiLink=wifi;
    s.lost(old);c.drain();
    yes(c.linked&&s.activeLink==wifi,"Authenticated backup should remain selected");
    yes(c.tasksData.length()==0&&c.taskResultUnconfirmed,"New activation must clear old task state even without a false callback");
    c.snapshot(0,task("completed"));c.drain();
    yes(c.workspaceRevision==0&&"completed".equals(c.tasksData.optJSONObject(0).optString("status")),"New route accepts its own low revision and true completion");break;
   }
   case "paired_capture_registration":{
    s.recording=true;s.epoch=7;s.captureWindow=new CaptureWindow(0,0);
    s.captureWindow.nativeReady=true;s.captureWindow.paired=true;s.captureWindow.startConfirmed=true;
    s.captureWindow.startToken="c".repeat(32);s.captureWindow.registeredRoute=s.routeEpoch;
    old.captureStartToken="c".repeat(32);old.pairedCaptureRequested=true;
    Link backup=new Link("wifi");
    old.failOffer=()->{try{activate(c,backup);}catch(Exception e){throw new RuntimeException(e);}};
    s.microphonePairFrame(7);
    yes(backup.packets.isEmpty(),"paired fallback bypassed current route registration");
    s.microphonePairFrame(7);
    yes(backup.packets.isEmpty(),"paired input reached route before its format hello");
    JSONObject hello=new JSONObject("capture_frontend","paired");hello.values.put("capture_start_token","c".repeat(32));
    s.receiveHello(backup,hello);s.microphonePairFrame(7);
    yes(backup.packets.size()==1&&backup.packets.get(0).type==Wire.MIC_PAIRED,"confirmed paired route did not resume same capture");
    break;
   }
   case "capture_rebind_id":{
    s.recording=true;s.epoch=7;s.captureWindow=new CaptureWindow(0,0);
    s.captureWindow.nativeReady=true;s.captureWindow.startConfirmed=true;s.captureWindow.startToken="";
    Link backup=new Link("wifi");backup.pairedCaptureRequested=true;
    activate(c,backup);
    yes(!backup.pairedCaptureRequested,"old format capability survived new activation");
    yes(backup.events.size()==1,"capture registered before its hello");
    s.receiveHello(backup,new JSONObject());
    yes(backup.events.size()==2,"new active link did not receive ready and rebind");
    JSONObject ready=backup.events.get(0),rebind=backup.events.get(1);
    yes(Integer.valueOf(1).equals(ready.values.get("capture_reject_version")),
        "capture rejection capability missing");
    yes("session_rebind".equals(rebind.optString("event"))
        &&s.captureWindow.id.equals(rebind.optString("capture_id"))
        &&Integer.valueOf(7).equals(rebind.values.get("epoch")),
        "new active link lost capture identity");
    break;
   }
   case "capture_pending_start":{
    String token="a".repeat(32);String another="b".repeat(32);
    JSONObject hello=new JSONObject("capture_start_token",token);
    s.recording=true;s.epoch=7;s.captureWindow=new CaptureWindow(0,0);
    s.captureWindow.nativeReady=true;
    s.receiveHello(old,hello);
    JSONObject first=old.events.get(old.events.size()-1);
    yes("session_start".equals(first.optString("event")),"fresh capture did not enter old queue");
    String id=first.optString("capture_id");long firstRoute=s.routeEpoch;
    Link backup=new Link("wifi");activate(c,backup);
    s.microphoneFrame(7);
    yes(backup.packets.isEmpty(),"capture audio preceded its registration on the backup");
    s.receiveCaptureStart(new JSONObject("capture_id",id,"epoch",7),firstRoute);
    yes(!s.captureWindow.startConfirmed,"old route confirmed a pending capture");
    yes(backup.events.size()==1,"audio registration was attempted before backup hello");
    s.receiveHello(backup,hello);s.receiveHello(backup,hello);
    yes(backup.events.size()==2,"backup must receive one replay of the fresh start");
    JSONObject retry=backup.events.get(1);
    yes("session_start".equals(retry.optString("event"))&&id.equals(retry.optString("capture_id"))
        &&token.equals(retry.optString("capture_start_token")),"backup changed capture identity");
    s.microphoneFrame(6);s.microphoneFrame(7);
    yes(backup.packets.size()==1&&backup.packets.get(0).generation==7,
        "backup received a stale microphone epoch");
    s.receiveCaptureStart(new JSONObject("capture_id",id,"epoch",6),s.routeEpoch);
    yes(!s.captureWindow.startConfirmed,"an old epoch confirmed the recording");
    SystemClock.now=8000;
    s.receiveCaptureStart(new JSONObject("capture_id",id,"epoch",7),s.routeEpoch);
    yes(!s.captureWindow.startConfirmed,"an expired start accepted a late confirmation");
    SystemClock.now=1;
    s.receiveCaptureStart(new JSONObject("capture_id",id,"epoch",7),s.routeEpoch);
    yes(s.captureWindow.startConfirmed,"current route did not confirm the recording");
    activate(c,old);s.receiveHello(old,hello);
    JSONObject rebind=old.events.get(old.events.size()-1);
    yes("session_rebind".equals(rebind.optString("event"))&&id.equals(rebind.optString("capture_id")),
        "confirmed capture sent another fresh start");
    activate(c,backup);s.receiveHello(backup,new JSONObject("capture_start_token",another));
    yes(!s.recording&&s.captureWindow==null,"new host instance inherited previous capture");
    break;
   }
   case "camera_stale_id":case "camera_stale_activation":case "camera_link_failure":case "camera_current":{
    long originalRoute=s.routeEpoch;String id="a".repeat(32);
    Link backup=new Link("wifi");
    if("camera_stale_activation".equals(args[0])){activate(c,backup);activate(c,old);}
    s.visual=true;s.visualGeneration=7;s.visionRequestId=id;
    if("camera_stale_id".equals(args[0]))id="b".repeat(32);
    if("camera_link_failure".equals(args[0])){
     backup.ready=true;s.wifiLink=backup;old.failOffer=()->s.lost(old);
    }
    JSONObject chunk=new JSONObject("event","camera_image_chunk","data","synthetic");
    int originalEvents=old.events.size();boolean refused=false;
    try{s.cameraSendForTest(chunk,7,id,originalRoute);}catch(IOException expected){refused=true;}
    if("camera_current".equals(args[0])){
     yes(!refused&&old.events.size()+old.packets.size()==originalEvents+1,"current picture was not enqueued once");
     yes(id.equals(chunk.optString("request_id")),"current picture lost its request binding");
    }else{
     yes(refused,"stale or failed picture send succeeded");
     yes(old.events.size()==originalEvents&&old.packets.isEmpty(),"stale picture reached old route");
     yes(backup.packets.isEmpty()&&backup.events.stream().noneMatch(e->"camera_image_chunk".equals(e.optString("event"))),"picture crossed to backup");
    }
    break;
   }
   case "camera_queue":{
    long route=s.routeEpoch;String id="a".repeat(32);
    s.visual=true;s.visualGeneration=7;s.visionRequestId=id;
    s.cameraSendForTest(new JSONObject("event","camera_image_chunk"),7,id,route);
    yes(old.queuedCamera.size()==1,"camera event was not marked in the queue");
    Wire.Packet queued=old.queuedCamera.get(0);
    yes(s.queuedCameraCurrent(queued,old),"live picture was dropped before write");
    yes(s.queuedCameraCurrent(new Wire.Packet(),old),"ordinary packets must keep their existing path");
    old.closed=true;
    yes(!s.queuedCameraCurrent(queued,old),"closed original link could still write a picture");
    old.closed=false;
    s.visual=false;
    yes(!s.queuedCameraCurrent(queued,old),"cancelled picture could still be written");
    activate(c,new Link("wifi"));activate(c,old);
    s.visual=true;s.visualGeneration=7;s.visionRequestId=id;
    yes(!s.queuedCameraCurrent(queued,old),"old queue survived reuse of the same Link");
    break;
   }
   case "camera_dispatch":{
    s.visual=true;s.visualGeneration=7;s.recording=true;
    String id="a".repeat(32);long route=s.routeEpoch;
    JSONObject request=new JSONObject("event","camera_capture","origin","voice","request_id",id,"vision_route",-1);
    s.deliver(new Wire.Packet(request),old);
    yes(c.cameraRequest==request&&request.optLong("vision_route",-1)==route,
        "camera callback did not receive a locally owned activation");
    yes(s.isVisionRequestCurrent(request),"current request could not open preview");
    int oldEvents=old.events.size();s.cameraFailed(request,"permission_required");
    yes(old.events.size()==oldEvents+1&&"camera_error".equals(old.events.get(oldEvents).optString("event")),
        "current camera error was not sent on its own route");
    Link next=new Link("wifi");activate(c,next);
    s.visual=true;s.visualGeneration=7;s.visionRequestId=id;
    yes(!s.isVisionRequestCurrent(request),"queued camera callback opened preview on a new activation");
    int newEvents=next.events.size();s.cameraFailed(request,"not_foreground");
    yes(next.events.size()==newEvents&&next.packets.isEmpty(),"old camera error reached the new route");
    break;
   }
   case "inactive_source":{
    activate(c,new Link("wifi"));c.drain();c.snapshot(0);c.drain();
    int before=Wire.parses;s.deliver(oldWorkspace(),old);c.drain();
    yes(Wire.parses==before+1&&c.tasksData.length()==0,"Inactive workspace source cannot pass the normal-event dispatch guard");break;
   }
   case "workspace_result":{
    Wire.Packet result=new Wire.Packet(new JSONObject("event","workspace_result","ok",true,"message","公开测试结果"));
    s.deliver(result,old);s.lost(old);Link next=new Link("usb");activate(c,next);c.drain();
    yes(Toast.shows==0,"Old operation result cannot show in the new activation");
    s.deliver(result,next);c.drain();yes(Toast.shows==1,"Current operation result remains visible");break;
   }
   case "voice_controls":{
    long route=s.routeEpoch;
    JSONObject state=new JSONObject("version",1,"revision",0,"speech_rate","normal","natural_barge",true,"supported",true);
    Wire.Packet receipt=new Wire.Packet(new JSONObject("event","voice_preferences","voice_preferences",state,"ok",true,"message","已应用"));
    s.minimumGeneration=8;s.deliver(receipt,old);
    yes(s.voicePreferences(route)==null,"stale generation published settings");
    s.minimumGeneration=7;s.deliver(receipt,old);c.drain();
    yes(s.voicePreferences(route)==state&&!s.recording,"current state missing or capture opened");
    int count=old.events.size();
    yes(s.setVoicePreferences("slow",false,0,route)&&old.events.size()==count+1,"current explicit setting not queued once");
    JSONObject sent=old.events.get(count);
    yes("voice_preferences_set".equals(sent.optString("event"))&&"slow".equals(sent.optString("speech_rate")),"setting request changed");
    Link next=new Link("wifi");activate(c,next);
    yes(s.voicePreferences(s.routeEpoch)==null&&!s.setVoicePreferences("fast",true,0,route),"old dialog crossed handover");
    s.deliver(receipt,old);yes(s.voicePreferences(s.routeEpoch)==null,"old source restored settings");
    s.deliver(receipt,next);yes(s.voicePreferences(s.routeEpoch)==state,"new route did not reconcile state");
    activate(c,old);yes(s.voicePreferences(s.routeEpoch)==null,"link reuse retained old capability");
    s.deliver(receipt,old);s.pause();yes(s.voicePreferences(s.routeEpoch)==null&&!s.recording,"Home retained controls or capture");
    break;
   }
   case "voice_controls_invalid":{
    for(Object version:new Object[]{true,2,"1"}){
     s.acceptVoicePreferences(new JSONObject("version",version,"revision",0,"speech_rate","normal","natural_barge",true,"supported",true));
     yes(s.voicePreferences(s.routeEpoch)==null,"bad version enabled settings");
    }
    for(Object revision:new Object[]{true,-1,1.0,"1"}){
     s.acceptVoicePreferences(new JSONObject("version",1,"revision",revision,"speech_rate","normal","natural_barge",true,"supported",true));
     yes(s.voicePreferences(s.routeEpoch)==null,"bad revision enabled settings");
    }
    for(String rate:new String[]{"fast","slow","normal"}){
     s.acceptVoicePreferences(new JSONObject("version",1,"revision",0,"speech_rate",rate,"natural_barge",false,"supported",true));
     yes(s.voicePreferences(s.routeEpoch)!=null,"supported mode unavailable");
    }
    s.acceptVoicePreferences(null);yes(s.voicePreferences(s.routeEpoch)==null,"missing capability retained state");
    break;
   }
   case "correction_receipts":{
    JSONObject draft=new JSONObject("event","speech_correction","revision",1,"pending",true,
      "draft_id","a".repeat(32),"original","旧问题","proposed","新问题");
    Wire.Packet packet=new Wire.Packet(draft);s.minimumGeneration=8;s.deliver(packet,old);
    yes(s.speechCorrection==null,"old generation exposed correction draft");
    s.minimumGeneration=7;s.deliver(packet,old);
    yes(s.currentSpeechCorrection(draft,s.routeEpoch),"current correction receipt missing");
    JSONObject clear=new JSONObject("event","speech_correction","revision",2,"pending",false);
    s.deliver(new Wire.Packet(clear),old);s.deliver(packet,old);
    yes(s.speechCorrection==clear,"old revision restored cleared draft");
    Link next=new Link("wifi");activate(c,next);s.deliver(packet,old);
    yes(s.speechCorrection==null&&s.speechCorrectionRevision==-1,"old source restored route capability");
    s.deliver(packet,next);s.pause();
    yes(s.speechCorrection==null,"Home retained correction data");break;
   }
   case "current_order":{
    c.snapshot(5,task("completed"));c.drain();long route=s.routeEpoch;
    s.select(old);c.drain();c.snapshot(4,task("running"));c.drain();
    yes(s.routeEpoch==route&&c.workspaceRevision==5,"Repeated selection preserves the current route and revision");
    yes("completed".equals(c.tasksData.optJSONObject(0).optString("status"))&&!c.taskResultUnconfirmed,"Current terminal receipt stays authoritative");break;
   }
   default:throw new AssertionError("Unknown scenario");
  }}finally{s.speakerOwner.close();}
 }
}
"""


class WorkspaceRouteTests(unittest.TestCase):
    def test_availability_preserves_continuous_ready_fallback(self):
        self.scenario('availability_handover')

    def test_availability_finishes_one_confirmed_gap(self):
        self.scenario('availability_loss')

    def test_availability_rejects_failed_activation(self):
        self.scenario('availability_failed_activation')

    def test_availability_stops_at_actual_pause_boundary(self):
        self.scenario('availability_pause')

    @classmethod
    def setUpClass(cls):
        activity, session = ACTIVITY.read_text(), SESSION.read_text()
        methods = [
            method(activity, signature)
            for signature in (
                'public void workspace(',
                'public void workspaceResult(',
                'public void link(',
                'private void renderWorkspace(',
                'private boolean hasRunningTask(',
                'private String taskStatus(',
            )
        ]
        route_methods = [
            method(session, signature)
            for signature in (
                'private void select(',
                'private void activateRoute(',
                'private void ownPacket(',
                'private boolean queuedPacketCurrent(',
                'private void writeQueuedPacket(',
                'private String recordingPhase(',
                'private synchronized void beginTaskOutput(',
                'private void registerCapture(',
                'private boolean captureHello(',
                'private void confirmCaptureStart(',
                'private void sendPacket(',
                'private boolean captureRegistered(',
                'boolean isVisionRequestCurrent(',
                'void cameraFailed(',
                'private void lost(',
                'void pause(',
                'private void closeSocket(',
                'private void acceptVoicePreferences(',
                'JSONObject voicePreferences(',
                'boolean setVoicePreferences(',
                'boolean preferenceRequest(',
                'private boolean sendWorkspace(',
                'private boolean acceptSpeechCorrection(',
                'boolean currentSpeechCorrection(',
            )
        ]
        camera_route_bound = 'private void sendCameraEvent(' in session
        if camera_route_bound:
            route_methods.extend(
                method(session, signature)
                for signature in (
                    'private boolean ownsVisionRequest(',
                    'private void sendCameraEvent(',
                )
            )
        else:
            route_methods.append(method(session, 'private void sendImageEvent('))
        route_methods.append(
            'void cameraSendForTest(JSONObject e,int gen,String id,long route)throws IOException{'
            + (
                'sendCameraEvent(e,gen,id,route);'
                if camera_route_bound
                else 'sendImageEvent(e,gen,id);'
            )
            + '}'
        )
        queued_camera_guard = 'private boolean queuedCameraCurrent(' in session
        if queued_camera_guard:
            route_methods.append(method(session, 'private boolean queuedCameraCurrent('))
        else:
            route_methods.append(
                'boolean queuedCameraCurrent(Wire.Packet p,Link link){return true;}'
            )
        bound = 'private long routeFor(' in session
        start = session.index('{', session.index('private void handle(')) + 1
        end = session.index('      if ("hello".equals(event)) {', start)
        # Keep the actual source/activation guard and workspace dispatch branch;
        # omit unrelated event branches and Android audio/camera dependencies.
        route_methods.append(
            (
                'void deliver(Wire.Packet received,Link source)throws Exception{'
                if 'handle(Wire.Packet received,' in session
                else 'void deliver(Wire.Packet p,Link source)throws Exception{'
            )
            + session[start:end]
            + 'if("workspace".equals(event)){'
            + branch(session, 'workspace')
            + '}else if("workspace_result".equals(event)){'
            + branch(session, 'workspace_result')
            + '}else if("voice_preferences".equals(event)){'
            + branch(session, 'voice_preferences')
            + '}else if("preference_result".equals(event)){'
            + branch(session, 'preference_result')
            + '}else if("speech_correction".equals(event)){'
            + branch(session, 'speech_correction')
            + '}else if("announcement_begin".equals(event)){'
            + branch(session, 'announcement_begin')
            + '}else if("camera_capture".equals(event)){'
            + branch(session, 'camera_capture')
            + '}' * (session[start:end].count('{') - session[start:end].count('}') + 2)
        )
        if bound:
            route_methods.extend(
                method(session, signature)
                for signature in ('boolean currentRoute(', 'private long routeFor(')
            )
        else:
            # Only an unused fixture interface on old source; old callbacks and
            # old lifecycle/dispatch bodies remain unmodified for reproduction.
            route_methods.append('boolean currentRoute(long route){return route==routeEpoch;}')
        harness = HARNESS[: HARNESS.index(' public static void main(String[] args)')]
        harness = harness.replace(
            'import java.util.*;', 'import java.util.*;import java.io.*;import java.net.*;'
        )
        harness = harness.replace(
            'String optString(String key)',
            'java.util.Iterator<String> keys(){return values.keySet().iterator();}Object opt(String key){return values.get(key);}JSONObject getJSONObject(String key){return (JSONObject)values.get(key);}boolean optBoolean(String key){return Boolean.TRUE.equals(values.get(key));}String optString(String key)',
        )
        harness = harness.replace(method(harness, 'static class Session'), SESSION_INTERFACES)
        harness = harness.replace(
            'void runOnUiThread(Runnable work){work.run();}',
            'void runOnUiThread(Runnable work){ui.add(work);}',
        )
        adapters = (
            'void workspace(JSONObject data){workspace(data,session.routeEpoch);}' if bound else ''
        )
        harness = (
            harness.replace('ROUTE_METHODS', '\n'.join(route_methods))
            .replace('METHODS', '\n'.join(methods))
            .replace('ROUTE_ADAPTERS', adapters)
            + SCENARIOS
        )
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.folder = Path(cls.temporary.name)
        test = cls.folder / 'ActivityCheck.java'
        test.write_text(harness)
        cls.java = java_bin()
        compiled = subprocess.run(
            [
                str(cls.java / 'javac'),
                '-encoding',
                'UTF-8',
                '-d',
                str(cls.folder),
                str(JAVA / 'CaptureWindow.java'),
                str(JAVA / 'CaptureClockStats.java'),
                str(JAVA / 'StatusFeedback.java'),
                str(JAVA / 'HostActivation.java'),
                str(JAVA / 'ConnectionAvailability.java'),
                str(JAVA / 'SpeakerOwner.java'),
                str(JAVA / 'SpeakerSession.java'),
                str(test),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if compiled.returncode:
            raise AssertionError('Actual route callbacks did not compile: ' + compiled.stderr)

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(self.folder),
                'com.happyhome.cc8assistant.ActivityCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_voice_controls_keep_receipts_dialogs_and_queued_writes_on_their_route(self):
        self.scenario('voice_controls')

    def test_preference_requests_require_current_capability_and_preserve_reserved_fields(self):
        self.scenario('preference_requests')

    def test_preference_receipts_are_retired_on_handover_and_home(self):
        self.scenario('preference_receipts')

    def test_voice_controls_require_typed_supported_state(self):
        self.scenario('voice_controls_invalid')

    def test_correction_receipts_keep_generation_revision_route_and_home_ownership(self):
        self.scenario('correction_receipts')

    def test_camera_transfer_is_bound_to_the_current_request_and_activation(self):
        for scenario in ('camera_stale_id', 'camera_stale_activation', 'camera_current'):
            with self.subTest(scenario=scenario):
                self.scenario(scenario)

    def test_queued_camera_chunk_is_rechecked_after_cancel_and_link_reuse(self):
        self.scenario('camera_queue')

    def test_camera_dispatch_binds_preview_and_error_to_its_own_activation(self):
        self.scenario('camera_dispatch')

    def test_all_queued_packets_keep_their_original_activation_on_link_reuse(self):
        self.scenario('route_queue')

    def test_already_admitted_write_keeps_old_token_without_blocking_handover(self):
        self.scenario('write_after_admission')

    def test_late_activation_or_downgrade_cannot_override_newer_route(self):
        self.scenario('old_activation_control')

    def test_bound_control_parsing_keeps_original_activation(self):
        self.scenario('host_parse_race')

    def test_host_restart_allows_new_generation_but_rejects_retired_host(self):
        self.scenario('host_restart')

    def test_malformed_management_fields_cannot_partially_activate_route(self):
        self.scenario('invalid_activation')

    def test_lan_configuration_uses_same_output_activation_boundary(self):
        self.scenario('bound_lan_config')

    def test_host_old_frames_cannot_arm_real_output_owner_after_link_reuse(self):
        self.scenario('old_host_after_reuse')

    def test_camera_transfer_never_falls_back_after_enqueue_failure(self):
        self.scenario('camera_link_failure')

    def test_paired_pcm_waits_for_registration_on_initial_and_failed_enqueue_routes(self):
        self.scenario('paired_capture_registration')

    def test_backup_activation_rebinds_the_exact_current_capture_id(self):
        self.scenario('capture_rebind_id')

    def test_backup_replays_only_the_unconfirmed_start_and_rejects_new_backend(self):
        self.scenario('capture_pending_start')

    def test_packet_parsed_after_loss_is_rejected(self):
        self.scenario('late_loss')

    def test_packet_parsed_after_reconnect_cannot_poison_revision(self):
        self.scenario('late_reconnect')

    def test_reactivated_same_link_rejects_previous_activation(self):
        self.scenario('same_link_reactivated')

    def test_same_link_reactivation_during_control_parse_keeps_original_binding(self):
        self.scenario('same_link_control_parse')

    def test_old_route_announcement_cannot_arm_output_after_control_parse(self):
        self.scenario('late_audio_begin_parse')

    def test_queued_workspace_cannot_hide_interrupted_work(self):
        self.scenario('queued_workspace_loss')

    def test_queued_link_cannot_reconnect_after_pause(self):
        self.scenario('queued_link_after_pause')

    def test_backup_activation_clears_state_without_a_loss_callback(self):
        self.scenario('backup_without_false_callback')

    def test_inactive_source_is_rejected(self):
        self.scenario('inactive_source')

    def test_current_route_keeps_revision_and_terminal_receipt(self):
        self.scenario('current_order')

    def test_old_operation_result_is_rejected_but_current_result_is_visible(self):
        self.scenario('workspace_result')
