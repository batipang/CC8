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
  boolean dotChatSupported,savedTasksSupported;
  int epoch,minimumGeneration,lifecycle,requests;
  volatile long routeEpoch;
  Link activeLink,usbLink,wifiLink;
  Socket socket;DataOutputStream output;ServerSocket server;
  String phase="idle",wakeMode="wake_word",wifiError="";
  ActivityCheck listener;
  final Object usbState=new Object();
  final Context context=new Context();final Lan lan=new Lan();
  final Auth desktopCameraAuth=new Auth();final Feedback statusFeedback=new Feedback();
  void cancelVision(){visual=false;}void stopMicrophone(){recording=false;}void cancelPlayback(){}
  String transportName(){return activeLink==null?"none":activeLink.kind;}
  void workspaceGet(){requests++;}
  ROUTE_METHODS
 }
 static class Context{void unregisterReceiver(Object receiver){}}
 static class Lan{void cancelPending(){}void release(){}void acquire(){}void configure(JSONObject data){}}
 static class Auth{void reset(){}String challenge(){return "public-test-challenge";}}
 static class Feedback{void reset(){}}
 static class BuildConfig{static final int CC8_PROTOCOL=2;}
 static class Link{
  boolean closed,ready;int ticket;String kind;
  Socket connection;DataOutputStream out;
  Link(String name){kind=name;}
  void event(JSONObject data){}void close(){closed=true;}
 }
 static class Wire{
  static final int EVENT=1;
  static boolean block;static int parses,blockAt=2;
  static final java.util.concurrent.CountDownLatch entered=new java.util.concurrent.CountDownLatch(1),release=new java.util.concurrent.CountDownLatch(1);
  static class Packet{int type=EVENT;JSONObject data;Packet(JSONObject value){data=value;}}
  static JSONObject json(Packet p)throws Exception{
   parses++;
   if(block&&parses==blockAt){entered.countDown();if(!release.await(2,java.util.concurrent.TimeUnit.SECONDS))throw new AssertionError("fixture barrier timeout");}
   return p.data;
  }
  static JSONObject event(String name){return new JSONObject("event",name);}
  static void put(JSONObject data,String key,Object value){data.values.put(key,value);}
 }
 void vision(boolean active,String message,boolean preserve){}
 void playback(boolean active){}void caption(String value,String mood){}void state(String value,String message){}
 final Queue<Runnable> ui=new java.util.concurrent.ConcurrentLinkedQueue<>();
 void drain(){Runnable r;while((r=ui.poll())!=null)r.run();}
 static Wire.Packet oldWorkspace(){return new Wire.Packet(new JSONObject("event","workspace","revision",99,"tasks",new JSONArray(task("completed")),"reminders",new JSONArray()));}
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
  switch(args[0]){
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
   case "current_order":{
    c.snapshot(5,task("completed"));c.drain();long route=s.routeEpoch;
    s.select(old);c.drain();c.snapshot(4,task("running"));c.drain();
    yes(s.routeEpoch==route&&c.workspaceRevision==5,"Repeated selection preserves the current route and revision");
    yes("completed".equals(c.tasksData.optJSONObject(0).optString("status"))&&!c.taskResultUnconfirmed,"Current terminal receipt stays authoritative");break;
   }
   default:throw new AssertionError("Unknown scenario");
  }
 }
}
"""


class WorkspaceRouteTests(unittest.TestCase):
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
                'private void lost(',
                'void pause(',
                'private void closeSocket(',
            )
        ]
        bound = 'final long route = routeFor(source);' in session
        start = session.index('{', session.index('private void handle(')) + 1
        end = session.index('      if ("hello".equals(event)) {', start)
        # Keep the actual source/activation guard and workspace dispatch branch;
        # omit unrelated event branches and Android audio/camera dependencies.
        route_methods.append(
            'void deliver(Wire.Packet p,Link source)throws Exception{'
            + session[start:end]
            + 'if("workspace".equals(event)){'
            + branch(session, 'workspace')
            + '}else if("workspace_result".equals(event)){'
            + branch(session, 'workspace_result')
            + '}}}'
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
            'JSONObject getJSONObject(String key){return (JSONObject)values.get(key);}boolean optBoolean(String key){return Boolean.TRUE.equals(values.get(key));}String optString(String key)',
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
            [str(cls.java / 'javac'), '-encoding', 'UTF-8', '-d', str(cls.folder), str(test)],
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

    def test_packet_parsed_after_loss_is_rejected(self):
        self.scenario('late_loss')

    def test_packet_parsed_after_reconnect_cannot_poison_revision(self):
        self.scenario('late_reconnect')

    def test_reactivated_same_link_rejects_previous_activation(self):
        self.scenario('same_link_reactivated')

    def test_same_link_reactivation_during_control_parse_keeps_original_binding(self):
        self.scenario('same_link_control_parse')

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
