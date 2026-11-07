"""Actual workspace dialog callbacks and send boundaries; no Android/device or user writes."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_interrupted_task_notice import method
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
ACTIVITY = JAVA / 'MainActivity.java'
SESSION = JAVA / 'VoiceSession.java'
SAVED = JAVA / 'SavedTasksDialog.java'

# These interfaces only hold widget values/callbacks and record transport offers.
# Task/reminder/save behavior comes from compiled production methods/classes.
FIXTURES = {
    'org/json/JSONObject.java': r"""
package org.json;
public class JSONObject {
 public final java.util.Map<String,Object> values=new java.util.HashMap<>();
 public JSONObject(Object... pairs){for(int i=0;i<pairs.length;i+=2)values.put((String)pairs[i],pairs[i+1]);}
 public String optString(String k){return optString(k,"");}
 public String optString(String k,String fallback){Object v=values.get(k);return v==null?fallback:String.valueOf(v);}
 public boolean optBoolean(String k){return Boolean.TRUE.equals(values.get(k));}
 public JSONArray optJSONArray(String k){Object v=values.get(k);return v instanceof JSONArray?(JSONArray)v:null;}
 public JSONObject optJSONObject(String k){Object v=values.get(k);return v instanceof JSONObject?(JSONObject)v:null;}
 public Object remove(String k){return values.remove(k);}
}
""",
    'org/json/JSONArray.java': r"""
package org.json;
public class JSONArray {
 final java.util.List<Object> values;
 public JSONArray(Object... rows){values=java.util.Arrays.asList(rows);}
 public int length(){return values.size();}
 public JSONObject optJSONObject(int i){Object v=values.get(i);return v instanceof JSONObject?(JSONObject)v:null;}
}
""",
    'android/app/Activity.java': r"""
package android.app;
public class Activity {
 public static final String CLIPBOARD_SERVICE="clipboard";
 public Object getSystemService(String key){return new android.content.ClipboardManager();}
}
""",
    'android/widget/TextView.java': r"""
package android.widget;
public class TextView {
 protected String text="";
 public TextView(Object owner){}
 public void setText(String value){text=value;}
 public CharSequence getText(){return text;}
 public void setTextColor(int value){}public void setTextSize(int value){}
 public void setTextIsSelectable(boolean value){}public void setPadding(int a,int b,int c,int d){}
}
""",
    'android/widget/Button.java': r"""
package android.widget;
public class Button extends TextView {
 public interface Click{void click(Object view);}
 private Click click;
 public Button(Object owner){super(owner);}
 public void setOnClickListener(Click value){click=value;}
 public void performClick(){if(click!=null)click.click(this);}
}
""",
    'android/widget/EditText.java': r"""
package android.widget;
public class EditText extends TextView {
 public static EditText last;
 public EditText(Object owner){super(owner);last=this;}
 public void setHintTextColor(int value){}public void setHint(String value){}
 public void setSingleLine(boolean value){}public void setFilters(android.text.InputFilter[] values){}
 public void setError(String value){}
}
""",
    'android/widget/ScrollView.java': r"""
package android.widget;
public class ScrollView {public ScrollView(Object owner){}public void addView(Object child){}}
""",
    'android/widget/LinearLayout.java': r"""
package android.widget;
public class LinearLayout {
 public static final int VERTICAL=1;
 public LinearLayout(Object owner){}public void setOrientation(int value){}
 public void setPadding(int a,int b,int c,int d){}public void addView(Object child){}
 public void addView(Object child,LayoutParams params){}
 public static class LayoutParams{public LayoutParams(int width,int height){}}
}
""",
    'android/widget/Spinner.java': r"""
package android.widget;
public class Spinner {
 int selected;
 public Spinner(Object owner){}public void setAdapter(Object adapter){}
 public void setSelection(int value){selected=value;}public int getSelectedItemPosition(){return selected;}
}
""",
    'android/widget/ArrayAdapter.java': r"""
package android.widget;
public class ArrayAdapter<T>{public ArrayAdapter(Object owner,int layout,T[] values){}}
""",
    'android/widget/Toast.java': r"""
package android.widget;
public class Toast {
 public static final int LENGTH_SHORT=0,LENGTH_LONG=1;public static int shows;
 public static Toast makeText(Object owner,String text,int length){return new Toast();}
 public void show(){shows++;}
}
""",
    'android/app/AlertDialog.java': r"""
package android.app;
import android.widget.Button;
public class AlertDialog {
 public static final int BUTTON_POSITIVE=-1,BUTTON_NEGATIVE=-2,BUTTON_NEUTRAL=-3;
 public interface Click{void click(AlertDialog dialog,int index);}
 public interface Event{void event(AlertDialog dialog);}
 public static AlertDialog last;
 public final Builder builder;public boolean dismissed;
 private Event onShow,onCancel;
 private final java.util.Map<Integer,Button> buttons=new java.util.HashMap<>();
 public AlertDialog(Builder value){builder=value;}
 public void setOnShowListener(Event value){onShow=value;}
 public void setOnCancelListener(Event value){onCancel=value;}
 public void show(){last=this;if(onShow!=null)onShow.event(this);}
 public void dismiss(){dismissed=true;}
 public Button getButton(int which){
  return buttons.computeIfAbsent(which,k->{Button b=new Button(this);Click c=k==BUTTON_POSITIVE?builder.positive:k==BUTTON_NEGATIVE?builder.negative:builder.neutral;
   b.setOnClickListener(v->{if(c!=null)c.click(this,k);dismiss();});return b;});
 }
 public void item(int index){builder.itemsClick.click(this,index);}
 public void positive(){getButton(BUTTON_POSITIVE).performClick();}
 public void negative(){getButton(BUTTON_NEGATIVE).performClick();}
 public static class Builder {
  public String title="",message="";public String[] labels;public Click positive,negative,neutral,itemsClick;
  public Builder(Object owner){}public Builder setTitle(String value){title=value;return this;}
  public Builder setMessage(String value){message=value;return this;}public Builder setView(Object value){return this;}
  public Builder setPositiveButton(String label,Click click){positive=click;return this;}
  public Builder setNegativeButton(String label,Click click){negative=click;return this;}
  public Builder setNeutralButton(String label,Click click){neutral=click;return this;}
  public Builder setItems(String[] values,Click click){labels=values;itemsClick=click;return this;}
  public AlertDialog create(){return new AlertDialog(this);}
  public AlertDialog show(){AlertDialog d=create();d.show();return d;}
 }
}
""",
    'android/app/DatePickerDialog.java': r"""
package android.app;
public class DatePickerDialog {
 public interface Pick{void pick(Object view,int year,int month,int day);}
 public DatePickerDialog(Object owner,Pick listener,int year,int month,int day){}public void show(){}
}
""",
    'android/app/TimePickerDialog.java': r"""
package android.app;
public class TimePickerDialog {
 public interface Pick{void pick(Object view,int hour,int minute);}
 public TimePickerDialog(Object owner,Pick listener,int hour,int minute,boolean full){}public void show(){}
}
""",
    'android/text/InputFilter.java': r"""
package android.text;
public interface InputFilter {class LengthFilter implements InputFilter{public LengthFilter(int value){}}}
""",
    'android/R.java': r"""
package android;
public class R {public static class drawable{public static int ic_menu_today=1;}public static class layout{public static int simple_spinner_dropdown_item=1;}}
""",
    'android/os/Looper.java': r"""
package android.os;
public class Looper{public static Looper getMainLooper(){return new Looper();}}
""",
    'android/os/Handler.java': r"""
package android.os;
public class Handler {
 public Handler(Looper looper){}public void postDelayed(Runnable work,long delay){}
 public void removeCallbacks(Runnable work){}
}
""",
    'android/content/ClipData.java': r"""
package android.content;
public class ClipData{public static ClipData newPlainText(String title,String text){return new ClipData();}}
""",
    'android/content/ClipboardManager.java': r"""
package android.content;
public class ClipboardManager{public void setPrimaryClip(ClipData data){}}
""",
}

HARNESS = r"""
package com.happyhome.cc8assistant;
import android.app.*;import android.widget.*;import org.json.*;import java.util.*;import java.io.*;
class VoiceSession {
 final Object routeLock=new Object();boolean foreground=true,savedTasksSupported=true;
 long routeEpoch=1;int epoch,eventCalls;Link activeLink=new Link("original"),usbLink,wifiLink,backup;
 final List<JSONObject> sent=new ArrayList<>();
 boolean failNext;
 class Link {
  final String name;boolean closed,ready=true;
  Link(String value){name=value;}
  void event(JSONObject j)throws IOException{Wire.Packet p=new Wire.Packet();p.data=Wire.jsonBytes(j);offer(p);}
  void offer(Wire.Packet p)throws IOException{
   eventCalls++;
   if(failNext){failNext=false;close();throw new IOException("Fixture queue full");}
   if(closed)throw new IOException("Fixture link closed");
   sent.add(Wire.decode(p.data));
  }
  void close(){if(closed)return;closed=true;if(activeLink==this){activeLink=backup;backup=null;routeEpoch++;}}
 }
 void change(){synchronized(routeLock){routeEpoch++;activeLink=new Link("new");}}
 void select(Link link)throws IOException{activeLink=link;routeEpoch++;}
 void workspaceGet(){}
 boolean captureRegistered(int epoch){throw new AssertionError("Workspace action sent microphone data");}
 SESSION_METHODS
 LEGACY_ADAPTERS
}
class Wire {
 static final int MIC=1,EVENT=3,MIC_PAIRED=5;
 static class Packet{int type,generation;long sequence,frame,nanos;byte[] data;}
 static String pauseOn="";
 static final java.util.concurrent.CountDownLatch entered=new java.util.concurrent.CountDownLatch(1),release=new java.util.concurrent.CountDownLatch(1);
 static final java.util.concurrent.atomic.AtomicInteger sequence=new java.util.concurrent.atomic.AtomicInteger();
 static final Map<Integer,JSONObject> values=new java.util.concurrent.ConcurrentHashMap<>();
 static JSONObject event(String name){
  if(name.equals(pauseOn)){entered.countDown();try{if(!release.await(2,java.util.concurrent.TimeUnit.SECONDS))throw new AssertionError("fixture barrier timeout");}catch(InterruptedException e){throw new AssertionError(e);}}
  return new JSONObject("event",name);
 }
 static void put(JSONObject j,String name,Object value){j.values.put(name,value);}
 static byte[] jsonBytes(JSONObject j){int key=sequence.incrementAndGet();values.put(key,j);return java.nio.ByteBuffer.allocate(4).putInt(key).array();}
 static JSONObject decode(byte[] raw){return values.get(java.nio.ByteBuffer.wrap(raw).getInt());}
}
class ActionCheck extends Activity {
 final VoiceSession session=new VoiceSession();final SavedTasksDialog savedTasks=new SavedTasksDialog(this,session);
 boolean linked=true;long workspaceRoute=1;
 JSONArray tasksData=new JSONArray(),remindersData=new JSONArray();
 static final int INK=1,MUTED=2;
 int dp(int value){return value;}TextView text(String value,int size,int color){TextView v=new TextView(this);v.setText(value);return v;}
 Button action(String value,int icon){Button b=new Button(this);b.setText(value);return b;}
 String dateLabel(String value){return value;}void inputTask(){throw new AssertionError("Unrequested text task");}
 ACTIVITY_METHODS
 static JSONObject task(String status){return new JSONObject("id","public-task","status",status,"title","公开测试任务","stage","固定阶段","steps",new JSONArray(),"result","公开测试结果");}
 static JSONObject reminder(){return new JSONObject("id","public-reminder","title","公开测试提醒","due","2099-01-01T10:00:00+08:00","repeat","none");}
 static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
 void detail(String status){tasksData=new JSONArray(task(status));showTaskList();AlertDialog.last.item(1);}
 void reminderOptions(){remindersData=new JSONArray(reminder());showReminders();AlertDialog.last.item(0);}
 void answer(Object... data){JSONObject j=new JSONObject(data);j.values.put("ok",true);j.values.put("request_id",session.sent.get(session.sent.size()-1).optString("request_id"));savedTasks.accept(j);}
 public static void main(String[] args)throws Exception{
  ActionCheck c=new ActionCheck();VoiceSession s=c.session;
  switch(args[0]){
   case "task_list":{
    c.tasksData=new JSONArray(task("running"));c.showTaskList();AlertDialog list=AlertDialog.last;s.change();list.item(1);
    if(AlertDialog.last!=list)AlertDialog.last.positive();
    yes(s.sent.isEmpty(),"Old task list must not open an actionable detail on the new route");break;
   }
   case "task_cancel":
    c.detail("running");s.change();AlertDialog.last.positive();yes(s.sent.isEmpty(),"Old task stop must not target a new route");break;
   case "task_save_entry":{
    c.detail("completed");AlertDialog detail=AlertDialog.last;s.change();detail.positive();
    if(AlertDialog.last!=detail)AlertDialog.last.positive();
    yes(s.sent.isEmpty(),"Old task save entry must not target a new route");break;
   }
   case "task_save_confirm":
    c.detail("completed");AlertDialog.last.positive();s.change();AlertDialog.last.positive();
    yes(s.sent.isEmpty(),"Old save confirmation must not target a new route");break;
   case "reminder_list":{
    c.remindersData=new JSONArray(reminder());c.showReminders();AlertDialog list=AlertDialog.last;s.change();list.item(0);
    if(AlertDialog.last!=list)AlertDialog.last.item(2);
    yes(s.sent.isEmpty(),"Old reminder list must not target a new route");break;
   }
   case "reminder_snooze":case "reminder_cancel":
    c.reminderOptions();s.change();AlertDialog.last.item("reminder_snooze".equals(args[0])?1:2);
    yes(s.sent.isEmpty(),"Old reminder action must not target a new route");break;
   case "reminder_form":{
    c.reminderOptions();AlertDialog.last.item(0);AlertDialog form=AlertDialog.last;s.change();form.positive();
    yes(s.sent.isEmpty()&&!form.dismissed,"Stale edit must remain unsubmitted rather than silently saving elsewhere");break;
   }
   case "saved_list":{
    c.savedTasks.open();c.answer("items",new JSONArray(task("completed")));AlertDialog list=AlertDialog.last;s.change();list.item(0);
    yes(s.sent.size()==1,"Old saved-result list cannot request an item from a new route");break;
   }
   case "saved_delete":{
    c.savedTasks.open();c.answer("items",new JSONArray(task("completed")));AlertDialog.last.item(0);c.answer("item",task("completed"));
    AlertDialog.last.negative();AlertDialog confirm=AlertDialog.last;s.change();confirm.positive();
    yes(s.sent.size()==2,"Old deletion confirmation cannot target a new route");break;
   }
   case "saved_response":{
    c.savedTasks.open();AlertDialog waiting=AlertDialog.last;s.change();c.answer("items",new JSONArray(task("completed")));
    yes(AlertDialog.last==waiting&&waiting.dismissed&&s.sent.size()==1,"Old saved-result response must not open a new view or request");break;
   }
   case "saved_pending":{
    c.savedTasks.open();c.answer("items",new JSONArray(task("completed")));AlertDialog old=AlertDialog.last;
    s.change();c.savedTasks.open();AlertDialog current=AlertDialog.last;old.item(0);
    yes(s.sent.size()==2&&!current.dismissed,"Old saved-list action must not cancel the current pending request");
    c.answer("items",new JSONArray(task("completed")));yes(AlertDialog.last!=current,"Current pending request must still accept its own response");break;
   }
   case "boundary_task":case "boundary_reminder":case "boundary_saved":{
    String kind=args[0].substring("boundary_".length());
    Wire.pauseOn="task".equals(kind)?"task_cancel":"reminder".equals(kind)?"reminder_action":"saved_task_request";
    long route=s.routeEpoch;List<Throwable> errors=new ArrayList<>();
    Thread sender=new Thread(()->{try{
     if("task".equals(kind))s.cancelTask("public-task",route);
     else if("reminder".equals(kind))s.reminderAction(new JSONObject("action","cancel","id","public-reminder"),route);
     else s.savedTaskRequest("public-request","delete","public-task",route);
    }catch(Throwable e){errors.add(e);}});sender.start();
    try{yes(Wire.entered.await(1,java.util.concurrent.TimeUnit.SECONDS),"fixture did not reach send preparation");s.change();}
    finally{Wire.release.countDown();sender.join(3000);}
    yes(!sender.isAlive()&&errors.isEmpty(),"fixture sender did not finish cleanly");
    yes(s.sent.isEmpty(),"Send boundary must recheck ownership after UI/preparation checks");break;
   }
   case "queue_failure":{
    s.backup=s.new Link("backup");s.failNext=true;boolean queued=s.cancelTask("public-task",s.routeEpoch);
    yes(!queued&&s.sent.isEmpty()&&s.eventCalls==1,"A failed owned write must not retry on a new route or claim it was queued");break;
   }
   case "current_controls":{
    c.detail("running");AlertDialog.last.positive();
    c.reminderOptions();AlertDialog.last.item(1);
    c.reminderOptions();AlertDialog.last.item(0);AlertDialog.last.positive();
    c.detail("completed");AlertDialog.last.positive();AlertDialog.last.positive();
    yes(s.sent.size()==4,"Current explicit operations must remain usable exactly once");
    yes("task_cancel".equals(s.sent.get(0).optString("event")),"Current stop event preserved");
    yes("update".equals(s.sent.get(1).optJSONObject("arguments").optString("action")),"Current snooze preserved");
    yes("save".equals(s.sent.get(3).optString("action")),"Current explicit result save preserved");break;
   }
   case "current_saved_delete":{
    c.savedTasks.open();c.answer("items",new JSONArray(task("completed")));AlertDialog.last.item(0);c.answer("item",task("completed"));
    AlertDialog.last.negative();AlertDialog.last.positive();yes(s.sent.size()==3&&"delete".equals(s.sent.get(2).optString("action")),"Current confirmed delete remains exactly once");break;
   }
   default:throw new AssertionError("Unknown fixture scenario");
  }
 }
}
"""


class WorkspaceActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activity, session = ACTIVITY.read_text(), SESSION.read_text()
        callbacks = [
            method(activity, signature)
            for signature in (
                'private void showTaskList(',
                'private void showTaskDetail(',
                'private void showReminders(',
                'private void reminderOptions(',
                'private void editReminder(',
                'private String formatDue(',
                'private String repeatLabel(',
                'private String taskStatus(',
            )
        ]
        for signature in (
            'private boolean requireWorkspaceRoute(',
            'private void workspaceNotSent(',
        ):
            if signature in activity:
                callbacks.append(method(activity, signature))
        methods = [
            method(session, signature)
            for signature in (
                'boolean isConnected(',
                'boolean currentRoute(',
                'boolean supportsSavedTaskResults(',
                'private void send(',
                'private void sendPacket(',
            )
        ]
        bound = 'boolean cancelTask(String id, long route)' in session
        for name in ('cancelTask', 'reminderAction', 'savedTaskRequest'):
            methods.append(method(session, ('boolean ' if bound else 'void ') + name + '('))
        for signature in ('long connectedRoute(', 'private boolean sendWorkspace('):
            if signature in session:
                methods.append(method(session, signature))
        adapters = ''
        if not bound:
            # Expose the candidate test call shape, but execute the actual old
            # unbound body. Do not add any guard to the old production callbacks.
            adapters = """boolean cancelTask(String id,long ignored){cancelTask(id);return true;}
boolean reminderAction(JSONObject args,long ignored){reminderAction(args);return true;}
boolean savedTaskRequest(String requestId,String action,String id,long ignored){savedTaskRequest(requestId,action,id);return true;}"""
        harness = (
            HARNESS.replace('SESSION_METHODS', '\n'.join(methods))
            .replace('ACTIVITY_METHODS', '\n'.join(callbacks))
            .replace('LEGACY_ADAPTERS', adapters)
        )
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.folder = Path(cls.temporary.name)
        sources = []
        for name, content in {
            **FIXTURES,
            'ActionCheck.java': harness,
            'SavedTasksDialog.java': SAVED.read_text(),
        }.items():
            path = cls.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            sources.append(str(path))
        cls.java = java_bin()
        compiled = subprocess.run(
            [str(cls.java / 'javac'), '-encoding', 'UTF-8', '-d', str(cls.folder), *sources],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if compiled.returncode:
            raise AssertionError('Actual workspace actions did not compile: ' + compiled.stderr)

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(self.folder),
                'com.happyhome.cc8assistant.ActionCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_task_list_cannot_supply_new_actions(self):
        self.scenario('task_list')

    def test_old_task_stop_is_rejected(self):
        self.scenario('task_cancel')

    def test_old_task_save_entry_is_rejected(self):
        self.scenario('task_save_entry')

    def test_old_task_save_confirmation_is_rejected(self):
        self.scenario('task_save_confirm')

    def test_old_reminder_list_is_rejected(self):
        self.scenario('reminder_list')

    def test_old_reminder_snooze_is_rejected(self):
        self.scenario('reminder_snooze')

    def test_old_reminder_cancel_is_rejected(self):
        self.scenario('reminder_cancel')

    def test_old_reminder_form_stays_unsubmitted(self):
        self.scenario('reminder_form')

    def test_old_saved_result_list_is_rejected(self):
        self.scenario('saved_list')

    def test_old_saved_delete_confirmation_is_rejected(self):
        self.scenario('saved_delete')

    def test_task_send_boundary_checks_after_preparation(self):
        self.scenario('boundary_task')

    def test_reminder_send_boundary_checks_after_preparation(self):
        self.scenario('boundary_reminder')

    def test_saved_send_boundary_checks_after_preparation(self):
        self.scenario('boundary_saved')

    def test_old_saved_response_cannot_open_a_new_view(self):
        self.scenario('saved_response')

    def test_old_saved_action_does_not_cancel_current_pending_request(self):
        self.scenario('saved_pending')

    def test_queue_failure_does_not_retry_on_backup(self):
        self.scenario('queue_failure')

    def test_current_controls_remain_usable(self):
        self.scenario('current_controls')

    def test_current_saved_delete_remains_usable(self):
        self.scenario('current_saved_delete')
