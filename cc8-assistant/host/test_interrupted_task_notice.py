"""Compile the real Activity task/link callbacks; no Android I/O or task replay."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
ACTIVITY = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/MainActivity.java'
SOURCE = ACTIVITY


def method(source, signature):
    if source.count(signature) != 1:
        raise AssertionError('Activity callback must be unique: ' + signature)
    start = source.index(signature)
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


HARNESS = r"""
package com.happyhome.cc8assistant;
import java.util.*;
class ActivityCheck {
 static class JSONObject {
  final Map<String,Object> values=new HashMap<>();
  JSONObject(Object... pairs){for(int i=0;i<pairs.length;i+=2)values.put((String)pairs[i],pairs[i+1]);}
  String optString(String key){Object v=values.get(key);return v==null?"":String.valueOf(v);}
  long optLong(String key,long fallback){Object v=values.get(key);return v instanceof Number?((Number)v).longValue():fallback;}
  JSONArray optJSONArray(String key){Object v=values.get(key);return v instanceof JSONArray?(JSONArray)v:null;}
 }
 static class JSONArray {
  final List<Object> values=new ArrayList<>();
  JSONArray(Object... rows){values.addAll(Arrays.asList(rows));}
  int length(){return values.size();}
  JSONObject optJSONObject(int index){return index>=0&&index<values.size()&&values.get(index) instanceof JSONObject?(JSONObject)values.get(index):null;}
 }
 static class TextView {
  String text="";int color;
  void setText(String value){text=value;} void setTextColor(int value){color=value;}
  void setCompoundDrawables(Object a,Object b,Object c,Object d){}
 }
 interface Click {void click(Object dialog,int index);}
 static class AlertDialog {
  static Builder last;
  static class Builder {
   String title="",message="",positiveLabel="";String[] items;
   Click positive,neutral,negative;
   Builder(Object owner){}
   Builder setTitle(String value){title=value;return this;}
   Builder setMessage(String value){message=value;return this;}
   Builder setPositiveButton(String value,Click action){positiveLabel=value;positive=action;return this;}
   Builder setNeutralButton(String value,Click action){neutral=action;return this;}
   Builder setNegativeButton(String value,Click action){negative=action;return this;}
   Builder setItems(String[] value,Click action){items=value;return this;}
   void show(){last=this;}
  }
 }
 static class Session {int requests;String transportName(){return "usb";}void workspaceGet(){requests++;}}
 static class Dialog {void close(){}void open(){throw new AssertionError("Unrequested saved-results action");}}
 static class Companion {void setTransport(String value){}}
 static class Steps {void task(JSONObject value){}}
 static class R {static class drawable {static int ic_hud_wifi=1,ic_hud_usb=2;}}
 static final int INK=1,MUTED=2,CYAN=3,AMBER=4;
 final TextView dotText=new TextView(),taskAction=new TextView(),reminderText=new TextView(),brainText=new TextView(),linkText=new TextView();
 final Dialog dotChats=new Dialog(),savedTasks=new Dialog();
 final Companion companion=new Companion();final Steps taskSteps=new Steps();
 final Session session=new Session();
 JSONArray tasksData=new JSONArray(),remindersData=new JSONArray();
 long workspaceRevision=-1;boolean linked=true,taskResultUnconfirmed;
 String brainProvider="dot";
 int taskActions;
 void runOnUiThread(Runnable work){work.run();}
 void traceAuto(String value){}void screen(boolean value){}void refresh(){}void maybeAutoVoice(){}
 Object icon(int id,int color,int size){return new Object();}
 String dateLabel(String value){return value;}
 void inputTask(){taskActions++;}void showReminders(){taskActions++;}void showTaskDetail(JSONObject task){taskActions++;}
 static JSONObject task(String status){return new JSONObject("id","same-id","generation",1,"status",status,"title","公开测试任务");}
 void snapshot(long revision,JSONObject... tasks){workspace(new JSONObject("revision",revision,"tasks",new JSONArray((Object[])tasks),"reminders",new JSONArray()));}
 static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
 METHODS
 public static void main(String[] args){
  ActivityCheck c=new ActivityCheck();
  switch(args[0]){
   case "running_loss":
    c.snapshot(1,task("running"));c.link(false);
    yes(c.tasksData.length()==0,"Keep original RAM-only receipt clearing");
    yes(c.taskResultUnconfirmed,"Interrupted work needs a separate uncertainty notice");
    yes(c.dotText.text.contains("结果待核对"),"Card must explain missing in-flight result");
    yes(c.dotText.color==AMBER,"Pending reconciliation uses the existing warning color");
    c.showTasks();
    yes(AlertDialog.last.message.contains("可能已经执行")&&AlertDialog.last.message.contains("电脑端核对"),"Explain partial effects and verification");
    yes(c.taskActions==0,"Opening a notice never reissues work");break;
   case "terminal_receipts":
    c.snapshot(1,task("completed"),task("failed"),task("cancelled"));c.link(false);
    yes(!c.taskResultUnconfirmed,"Known terminal receipts must not become unknown");
    c.showTasks();yes(!AlertDialog.last.message.contains("可能已经执行"),"No fabricated interrupted-work warning");break;
   case "completed_before_loss":
    c.snapshot(5,task("running"));c.snapshot(6,task("completed"));c.snapshot(4,task("running"));c.link(false);
    yes(!c.taskResultUnconfirmed,"Authoritative completion before loss wins over an older snapshot");break;
   case "sticky_reconnect":
    c.snapshot(5,task("running"));c.link(false);c.link(true);c.snapshot(0);
    yes(c.taskResultUnconfirmed,"Empty restarted workspace cannot prove prior completion");
    yes(c.dotText.text.contains("结果待核对"),"Recovery keeps notice visible without fake task records");
    c.snapshot(1,task("completed"));
    yes(c.taskResultUnconfirmed,"Reused IDs/generations cannot confirm an older task");
    c.showTasks();AlertDialog.last.neutral.click(null,0);
    yes(c.taskResultUnconfirmed,"Viewing current tasks does not acknowledge old work");
    yes(AlertDialog.last.items.length==2,"Current authoritative task and saved-results entry remain accessible");
    yes(c.taskActions==0,"No new task submitted during reconnection");break;
   case "acknowledgement":
    c.snapshot(1,task("running"));c.link(false);c.link(false);
    yes(c.taskResultUnconfirmed,"Repeated loss cannot clear the notice");
    c.showTasks();int reads=c.session.requests;
    AlertDialog.last.positive.click(null,0);
    yes(!c.taskResultUnconfirmed,"Owner can dismiss the RAM-only hint");
    yes(c.session.requests==reads&&c.taskActions==0,"Acknowledgement sends no task or capture request");
    yes(c.tasksData.length()==0,"Acknowledgement fabricates no completion receipt");
    yes(!c.dotText.text.contains("结果待核对")&&c.dotText.color==INK,"Normal empty-card presentation restored");
    c.link(false);yes(!c.taskResultUnconfirmed,"An empty loss does not resurrect a dismissed hint");break;
   case "new_interruption":
    c.snapshot(1,task("running"));c.link(false);c.showTasks();AlertDialog.last.positive.click(null,0);
    c.link(true);c.snapshot(0,task("running"));c.link(false);
    yes(c.taskResultUnconfirmed,"A later real interruption creates a new notice");break;
   default:throw new AssertionError("Unknown test scenario");
  }
 }
}
"""


class InterruptedTaskNoticeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = SOURCE.read_text()
        reference = ACTIVITY.read_text()
        # Same candidate helper/UI interfaces support old-callback reproduction;
        # old link/render/showTasks bodies remain the actual old production code.
        callbacks = (
            'public void workspace(',
            'public void link(',
            'private void renderWorkspace(',
            'private String taskStatus(',
            'private void showTasks(',
        )
        helpers = (
            'private boolean hasRunningTask(',
            'private void showUnconfirmedTaskNotice(',
            'private void showTaskList(',
        )
        methods = [method(source, signature) for signature in callbacks]
        methods.extend(
            method(source if signature in source else reference, signature) for signature in helpers
        )
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        folder = Path(cls.temporary.name)
        path = folder / 'ActivityCheck.java'
        path.write_text(HARNESS.replace('METHODS', '\n'.join(methods)))
        cls.java = java_bin()
        cls.folder = folder
        result = subprocess.run(
            [str(cls.java / 'javac'), '-encoding', 'UTF-8', '-d', str(folder), str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise AssertionError('Actual Activity callbacks did not compile: ' + result.stderr)

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

    def test_running_task_loss_keeps_an_explicit_uncertainty_notice(self):
        self.scenario('running_loss')

    def test_known_terminal_receipts_do_not_create_false_warnings(self):
        self.scenario('terminal_receipts')

    def test_completed_receipt_before_loss_and_stale_snapshot_are_respected(self):
        self.scenario('completed_before_loss')

    def test_notice_survives_empty_reconnect_and_reused_ids(self):
        self.scenario('sticky_reconnect')

    def test_acknowledgement_only_dismisses_the_notice_without_replay(self):
        self.scenario('acknowledgement')

    def test_later_interrupted_task_can_raise_a_new_notice(self):
        self.scenario('new_interruption')


if __name__ == '__main__':
    unittest.main()
