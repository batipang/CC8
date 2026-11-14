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
    'com/happyhome/cc8assistant/LargeDialogBuilder.java': 'package com.happyhome.cc8assistant;public class LargeDialogBuilder extends android.app.AlertDialog.Builder{public LargeDialogBuilder(android.app.Activity a){super(a);}}',
    'com/happyhome/cc8assistant/R.java': 'package com.happyhome.cc8assistant;public class R{public static class layout{public static int dialog_spinner_item=1;}}',
    'org/json/JSONObject.java': r"""
package org.json;
public class JSONObject {
 public final java.util.Map<String,Object> values=new java.util.HashMap<>();
 public JSONObject(Object... pairs){for(int i=0;i<pairs.length;i+=2)values.put((String)pairs[i],pairs[i+1]);}
 public Object opt(String k){return values.get(k);}
 public String optString(String k){return optString(k,"");}
 public String optString(String k,String fallback){Object v=values.get(k);return v==null?fallback:String.valueOf(v);}
 public boolean optBoolean(String k){return Boolean.TRUE.equals(values.get(k));}
 public int optInt(String k,int fallback){Object v=values.get(k);return v instanceof Integer?(Integer)v:fallback;}
 public JSONArray optJSONArray(String k){Object v=values.get(k);return v instanceof JSONArray?(JSONArray)v:null;}
 public JSONObject optJSONObject(String k){Object v=values.get(k);return v instanceof JSONObject?(JSONObject)v:null;}
 public Object remove(String k){return values.remove(k);}
 public boolean isNull(String k){return values.get(k)==null;}
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
 private Click click;boolean enabled=true;
 public void setEnabled(boolean value){enabled=value;}
 public Button(Object owner){super(owner);}
 public void setMinHeight(int value){}
 public void setOnClickListener(Click value){click=value;}
 public void performClick(){if(enabled&&click!=null)click.click(this);}
}
""",
    'android/widget/EditText.java': r"""
package android.widget;
public class EditText extends TextView {
 public static EditText last;
 public EditText(Object owner){super(owner);last=this;}
 public void setHintTextColor(int value){}public void setHint(String value){}
 public void setSingleLine(boolean value){}public void setFilters(android.text.InputFilter[] values){}
 public void setInputType(int value){}
 public void setError(String value){}
}
""",
    'android/widget/Switch.java': r"""
package android.widget;
public class Switch extends TextView {
 boolean checked;
 public Switch(Object owner){super(owner);}
 public void setChecked(boolean value){checked=value;}public boolean isChecked(){return checked;}
}
""",
    'android/text/InputType.java': r"""
package android.text;
public class InputType {public static final int TYPE_CLASS_NUMBER=2;}
""",
    'android/widget/ScrollView.java': r"""
package android.widget;
public class ScrollView {public Object child;public ScrollView(Object owner){}public void addView(Object value){child=value;}}
""",
    'android/widget/LinearLayout.java': r"""
package android.widget;
public class LinearLayout {
 public static final int VERTICAL=1;
 public boolean focusableInTouchMode,focused;
 public void setFocusableInTouchMode(boolean value){focusableInTouchMode=value;}
 public boolean requestFocus(){focused=true;return true;}
 public final java.util.List<Object> children=new java.util.ArrayList<>();
 public LinearLayout(Object owner){}public void setOrientation(int value){}
 public void setPadding(int a,int b,int c,int d){}public void addView(Object child){children.add(child);}
 public void addView(Object child,LayoutParams params){}
 public static class LayoutParams{public LayoutParams(int width,int height){}}
}
""",
    'android/widget/Spinner.java': r"""
package android.widget;
public class Spinner {
 public static final java.util.List<Spinner> all=new java.util.ArrayList<>();
 int selected;
 public Spinner(Object owner){all.add(this);}public void setAdapter(Object adapter){}
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
 private Event onShow,onCancel,onDismiss;
 public final android.view.Window window=new android.view.Window();
 public android.view.Window getWindow(){return window;}
 public void setOnDismissListener(Event value){onDismiss=value;}
 private final java.util.Map<Integer,Button> buttons=new java.util.HashMap<>();
 public AlertDialog(Builder value){builder=value;}
 public void setOnShowListener(Event value){onShow=value;}
 public void setOnCancelListener(Event value){onCancel=value;}
 public void show(){last=this;if(onShow!=null)onShow.event(this);}
 public void dismiss(){if(dismissed)return;dismissed=true;if(onDismiss!=null)onDismiss.event(this);}
 public Button getButton(int which){
  return buttons.computeIfAbsent(which,k->{Button b=new Button(this);Click c=k==BUTTON_POSITIVE?builder.positive:k==BUTTON_NEGATIVE?builder.negative:builder.neutral;
   b.setOnClickListener(v->{if(c!=null)c.click(this,k);dismiss();});return b;});
 }
 public void item(int index){builder.itemsClick.click(this,index);}
 public void positive(){getButton(BUTTON_POSITIVE).performClick();}
 public void negative(){getButton(BUTTON_NEGATIVE).performClick();}
 public static class Builder {
  public String title="",message="";public Object view;public String[] labels;public Click positive,negative,neutral,itemsClick;
  public Builder(Object owner){}public Builder setTitle(String value){title=value;return this;}
  public Builder setMessage(String value){message=value;return this;}public Builder setView(Object value){view=value;return this;}
  public Builder setPositiveButton(String label,Click click){positive=click;return this;}
  public Builder setNegativeButton(String label,Click click){negative=click;return this;}
  public Builder setNeutralButton(String label,Click click){neutral=click;return this;}
  public Builder setItems(String[] values,Click click){labels=values;itemsClick=click;return this;}
  public AlertDialog create(){return new AlertDialog(this);}
  public AlertDialog show(){AlertDialog d=create();d.show();return d;}
 }
}
""",
    'android/view/Window.java': r"""
package android.view;
public class Window {
 public int cleared,soft;
 public void clearFlags(int value){cleared|=value;}
 public void setSoftInputMode(int value){soft=value;}
}
""",
    'android/view/WindowManager.java': r"""
package android.view;
public class WindowManager {public static class LayoutParams {
 public static final int FLAG_FULLSCREEN=1024,SOFT_INPUT_STATE_HIDDEN=2,SOFT_INPUT_ADJUST_RESIZE=16;
}}
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
 public static Runnable last;
 public Handler(Looper looper){}public void postDelayed(Runnable work,long delay){last=work;}
 public void removeCallbacks(Runnable work){if(last==work)last=null;}
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
import android.app.*;import android.widget.*;import android.view.*;import org.json.*;import java.util.*;import java.io.*;
class VoiceSession {
 static class IdentityFixture { boolean ready(){return true;} }
 final IdentityFixture identity=new IdentityFixture();boolean identityHello=true;
 boolean identityConnected(){return true;}
 final Object routeLock=new Object();boolean foreground=true,savedTasksSupported=true,resultProvenanceSupported=true,resultReadingSupported=true;
 long routeEpoch=1;int epoch,eventCalls;Link activeLink=new Link("original"),usbLink,wifiLink,backup;
 final List<JSONObject> sent=new ArrayList<>();
 boolean failNext;
 class Link {
  final String name;String routeToken;boolean closed,ready=true;
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
 static class Packet{int type,generation;long sequence,frame,nanos,outgoingRoute=-1;byte[] data;}
 static void bindRoute(Packet p,String token){throw new AssertionError("Unexpected negotiated route in legacy fixture");}
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
 final VoiceSession session=new VoiceSession();
 final ResultDetailsDialog resultDetails=new ResultDetailsDialog(this,session);
 final SavedTasksDialog savedTasks=new SavedTasksDialog(this,session,resultDetails);
 boolean linked=true;long workspaceRoute=1;
 JSONArray tasksData=new JSONArray(),remindersData=new JSONArray();
 JSONObject reminderPolicyData;
 JSONArray reminderHistoryData=new JSONArray();
 boolean reminderChannelsSupported=true;
 AlertDialog reminderPolicyDialog,reminderEditorDialog,reminderHistoryDialog;
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
 static JSONObject sourceTask(String kind,String status){
  JSONObject value=task("completed");
  JSONObject file=new JSONObject("item_id","file-item","path","/tmp/public.txt","kind",kind,"status",status,"move_to",null);
  value.values.put("finished_at","2026-11-09T08:00:00+08:00");
  value.values.put("provenance",new JSONObject("provider","dot","host","local","thread_id","voice-thread","turn_id","voice-turn","files",new JSONArray(file),"files_truncated",false));
  return value;
 }
 static Button locateButton(){
  LinearLayout content=(LinearLayout)((ScrollView)AlertDialog.last.builder.view).child;
  return (Button)content.children.get(1);
 }
 public static void main(String[] args)throws Exception{
  ActionCheck c=new ActionCheck();VoiceSession s=c.session;
  switch(args[0]){
   case "reminder_old_backend":{
    c.reminderChannelsSupported=false;c.editReminder(null,s.routeEpoch);EditText.last.setText("公开固定旧后台提醒");
    AlertDialog.last.positive();yes(s.sent.size()==1&&!s.sent.get(0).optJSONObject("arguments").values.containsKey("channel"),"Old backend must receive its original CC8 shape");
    int before=Spinner.all.size();c.editReminder(new JSONObject("channel","desktop"),s.routeEpoch);
    yes(Spinner.all.size()==before&&s.sent.size()==1,"Unsupported desktop edit must not silently convert to speech");break;
   }
   case "reminder_channel_save":{
    JSONObject item=new JSONObject("id","public-reminder","title","公开固定提醒","due",c.formatDue(System.currentTimeMillis()+3600000),"repeat","none","channel","desktop");
    c.editReminder(item,s.routeEpoch);yes(Spinner.all.get(1).getSelectedItemPosition()==1,"Editing lost the original desktop choice");
    AlertDialog.last.positive();yes("desktop".equals(s.sent.get(0).optJSONObject("arguments").optString("channel")),"Native save must preserve explicit desktop channel");
    c.editReminder(null,s.routeEpoch);yes(Spinner.all.get(3).getSelectedItemPosition()==0,"New reminders must default to CC8");
    Spinner.all.get(3).setSelection(1);EditText.last.setText("另一个公开固定提醒");AlertDialog.last.positive();
    yes(s.sent.size()==2&&"desktop".equals(s.sent.get(1).optJSONObject("arguments").optString("channel")),"An explicit new channel selection was not sent");break;
   }
   case "reminder_channel_labels":{
    JSONObject item=new JSONObject("status","delivering","channel","desktop");
    yes(c.reminderDeliveryLabel(item).contains("等待通知回执")&&!c.reminderDeliveryLabel(item).contains("播完"),"Desktop pending was mislabeled playback");
    item.values.put("status","delivered");item.values.put("delivery_attempt",new JSONObject("result","notified","channel","desktop"));
    yes(c.reminderDeliveryLabel(item).contains("已进入通知中心，未确认阅读"),"Delivery does not prove read");
    c.reminderHistoryData=new JSONArray(item);c.showReminderHistory();
    yes(AlertDialog.last.builder.labels.length==1&&AlertDialog.last.builder.labels[0].contains("未确认阅读")&&s.sent.isEmpty(),"History is actual and read-only");
    item.values.put("status","pending");item.values.put("channel","cc8");
    yes(c.reminderDeliveryLabel(item).equals("CC8 · 等待到期"),"Future occurrence must use its chosen channel");
    yes(c.reminderHistoryLabel(item).contains("Mac 桌面")&&c.reminderHistoryLabel(item).contains("通知中心"),"Editing channel must not rewrite the prior notification receipt");
    JSONObject legacy=new JSONObject("status","delivered");
    yes(c.reminderHistoryLabel(legacy).contains("历史已送达标记，未保留尝试回执"),"Legacy metadata must not fabricate a completed playback attempt");
    item.values.put("channel","desktop");
    item.values.put("status","unconfirmed");item.values.put("delivery_attempt",new JSONObject("detail","permission_denied"));
    yes(c.reminderDeliveryLabel(item).contains("Mac 通知授权"),"Missing actionable permission reason");
    item.values.remove("channel");item.values.put("status","delivering");yes(c.reminderDeliveryLabel(item).contains("等待播完回执"),"Legacy reminders remain CC8");break;
   }
   case "reading_controls":{
    c.detail("completed");LinearLayout content=(LinearLayout)((ScrollView)AlertDialog.last.builder.view).child;
    ((Button)content.children.get(1)).performClick();((Button)content.children.get(1)).performClick();
    yes(s.sent.size()==1,"Repeated taps must not resend");JSONObject request=s.sent.get(0);
    yes("result_read_request".equals(request.optString("event"))&&request.values.size()==4&&!request.optBoolean("restart")&&!request.values.containsKey("text"),"Continue sends only receipt identity and mode");
    ((Button)content.children.get(2)).performClick();yes(s.sent.size()==2&&s.sent.get(1).optBoolean("restart"),"From-start mode remains explicit");break;
   }
   case "reading_stale":{
    c.detail("completed");LinearLayout content=(LinearLayout)((ScrollView)AlertDialog.last.builder.view).child;
    s.change();((Button)content.children.get(1)).performClick();yes(s.sent.isEmpty(),"Old task read cannot cross a route");break;
   }
   case "reading_capability":{
    s.resultReadingSupported=false;c.detail("completed");LinearLayout content=(LinearLayout)((ScrollView)AlertDialog.last.builder.view).child;
    yes(content.children.size()==1&&!s.readResult("id",false,s.routeEpoch)&&s.sent.isEmpty(),"Old backend has no unsupported read control");break;
   }
   case "reading_queue_failure":{
    c.detail("completed");LinearLayout content=(LinearLayout)((ScrollView)AlertDialog.last.builder.view).child;
    s.backup=s.new Link("backup");s.failNext=true;((Button)content.children.get(1)).performClick();
    yes(s.sent.isEmpty()&&s.eventCalls==1,"Uncertain read cannot retry on backup");break;
   }
   case "result_source":{
    c.resultDetails.open(sourceTask("update","completed"),false,s.routeEpoch);
    yes(s.sent.isEmpty(),"Viewing provenance must not send a task or locate anything");
    TextView body=(TextView)((LinearLayout)((ScrollView)AlertDialog.last.builder.view).child).children.get(0);
    yes(body.getText().toString().contains("voice-thread")&&body.getText().toString().contains("/tmp/public.txt"),"Actual receipt provenance must be visible");
    locateButton().performClick();AlertDialog.last.item(0);
    yes(s.sent.size()==1,"An explicit file choice sends exactly one request");
    JSONObject request=s.sent.get(0);
    yes("reveal_live".equals(request.optString("action"))&&request.values.size()==5&&!request.values.containsKey("path"),"Wire may carry only receipt ID and index, never a client path");break;
   }
   case "result_stale_choice":{
    c.resultDetails.open(sourceTask("update","completed"),false,s.routeEpoch);locateButton().performClick();
    AlertDialog choice=AlertDialog.last;s.change();choice.item(0);
    yes(s.sent.isEmpty(),"Old result choice must not reveal on a new route");break;
   }
   case "result_bad_receipt":{
    c.resultDetails.open(sourceTask("delete","completed"),false,s.routeEpoch);locateButton().performClick();AlertDialog.last.item(0);
    c.resultDetails.open(sourceTask("update","failed"),false,s.routeEpoch);locateButton().performClick();AlertDialog.last.item(0);
    yes(s.sent.isEmpty(),"Deleted and failed file receipts cannot launch file location");break;
   }
   case "result_capability":{
    s.resultProvenanceSupported=false;c.resultDetails.open(sourceTask("update","completed"),false,s.routeEpoch);
    LinearLayout view=(LinearLayout)((ScrollView)AlertDialog.last.builder.view).child;
    yes(view.children.size()==1&&!s.revealResultFile("id","task",0,false,s.routeEpoch)&&s.sent.isEmpty(),"Old backend must not show or accept the new action");break;
   }
   case "result_saved":{
    c.savedTasks.open();c.answer("items",new JSONArray(sourceTask("update","completed")));AlertDialog.last.item(0);
    c.answer("item",sourceTask("update","completed"));locateButton().performClick();
    locateButton().performClick();AlertDialog.last.item(0);
    yes(s.sent.size()==3&&"reveal_saved".equals(s.sent.get(2).optString("action")),"Saved provenance must use the saved-receipt lookup");break;
   }
   case "result_timeout":{
    c.resultDetails.open(sourceTask("update","completed"),false,s.routeEpoch);locateButton().performClick();AlertDialog.last.item(0);
    AlertDialog waiting=AlertDialog.last;android.os.Handler.last.run();
    c.resultDetails.accept(new JSONObject("request_id",s.sent.get(0).optString("request_id"),"ok",true,"revealed",true));
    yes(s.sent.size()==1&&waiting.dismissed,"Timeout/late ACK must not retry or reopen a stale result");break;
   }
   case "result_queue_failure":{
    c.resultDetails.open(sourceTask("update","completed"),false,s.routeEpoch);locateButton().performClick();s.backup=s.new Link("backup");s.failNext=true;AlertDialog.last.item(0);
    yes(s.sent.isEmpty()&&s.eventCalls==1,"An uncertain reveal write cannot retry on a fallback route");break;
   }
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
   case "saved_handoff":{
    JSONObject pending=task("completed");pending.values.put("desktop_handoff_pending",true);
    JSONObject malformed=task("completed");malformed.values.put("desktop_handoff_pending","true");
    JSONObject uncertain=task("cancelled");uncertain.values.put("execution_unconfirmed",true);
    JSONObject malformedUncertain=task("failed");malformedUncertain.values.put("execution_unconfirmed","true");
    c.savedTasks.open();c.answer("items",new JSONArray(pending,task("completed"),malformed,uncertain,malformedUncertain));
    AlertDialog list=AlertDialog.last;
    yes(list.builder.labels[0].startsWith("待桌面接续 · "),"Handoff list must identify pending desktop work");
    for(int i=1;i<3;i++)yes(list.builder.labels[i].startsWith("已完成 · "),"Normal or malformed metadata must use the ordinary receipt label");
    yes(list.builder.labels[3].startsWith("执行结果待核对 · "),"Unconfirmed execution must survive the saved list");
    yes(list.builder.labels[4].startsWith("未完成 · "),"String flags cannot become execution evidence");
    list.item(0);c.answer("item",pending);
    ScrollView pendingScroll=(ScrollView)AlertDialog.last.builder.view;
    LinearLayout pendingBody=(LinearLayout)pendingScroll.child;
    yes(((TextView)pendingBody.children.get(0)).getText().toString().contains("状态：待桌面接续"),"Saved detail must keep the pending desktop status");
    c.savedTasks.open();c.answer("items",new JSONArray(uncertain));AlertDialog.last.item(0);c.answer("item",uncertain);
    ScrollView scroll=(ScrollView)AlertDialog.last.builder.view;
    LinearLayout body=(LinearLayout)scroll.child;
    String detail=((TextView)body.children.get(0)).getText().toString();
    yes(detail.contains("状态：执行结果待核对")&&detail.contains("请先在 Mac 核对原任务"),"Saved detail must tell the user to verify execution");break;
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
   case "policy_apply":case "policy_reset":case "policy_stale":case "policy_invalid":case "policy_cancel":{
    c.reminderPolicyData=new JSONObject("revision",7,"quiet_enabled",true,"quiet_start","21:00","quiet_end","08:00","late_policy","skip","max_late_minutes",30);
    c.reminderDeliverySettings(1);AlertDialog form=AlertDialog.last;
    LinearLayout content=(LinearLayout)((ScrollView)form.builder.view).child;
    yes(content.focusableInTouchMode&&content.focused,"Settings opened with an automatically focused editor");
    yes(form.window.soft==(WindowManager.LayoutParams.SOFT_INPUT_STATE_HIDDEN|WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE),"IME resize/initial visibility policy missing");
    yes((form.window.cleared&WindowManager.LayoutParams.FLAG_FULLSCREEN)!=0,"Fullscreen flag still blocks IME resize");
    if(args[0].equals("policy_stale"))s.change();
    if(args[0].equals("policy_invalid"))((EditText)content.children.get(2)).setText("24:00");
    if(args[0].equals("policy_reset"))form.getButton(AlertDialog.BUTTON_NEUTRAL).performClick();
    else if(args[0].equals("policy_cancel"))form.negative();else {form.positive();form.positive();}
    boolean submit=args[0].equals("policy_apply")||args[0].equals("policy_reset");
    if(args[0].equals("policy_cancel"))yes(form.dismissed&&c.reminderPolicyDialog==null,"Cancelled policy dialog retained stale window ownership");
    yes(s.sent.size()==(submit?1:0),"Policy cancelled, invalid, old-route or repeated tap crossed send ownership");
    if(submit){
     JSONObject policy=s.sent.get(0).optJSONObject("arguments").optJSONObject("policy");
     yes(policy.optInt("revision",-1)==7,"Policy lost original CAS revision");
     yes(policy.optBoolean("quiet_enabled")==args[0].equals("policy_apply"),"Wrong explicit quiet mode");
     yes(policy.optString("quiet_start").equals(args[0].equals("policy_apply")?"21:00":"22:00"),"Defaults or original time wrong");
    }break;
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
                'private void reminderDeliverySettings(',
                'private void sendReminderPolicy(',
                'private static String reminderDeliveryLabel(',
                'private static String reminderHistoryLabel(',
                'private void showReminderHistory(',
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
                'boolean supportsResultProvenance(',
                'boolean supportsResultReading(',
                'boolean readResult(',
                'boolean revealResultFile(',
                'private void send(',
                'private void sendPacket(',
                'private void ownPacket(',
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
            'ResultDetailsDialog.java': (JAVA / 'ResultDetailsDialog.java').read_text(),
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

    def test_new_device_keeps_old_backend_cc8_reminder_shape(self):
        self.scenario('reminder_old_backend')

    def test_actual_native_create_and_edit_preserve_explicit_channel(self):
        self.scenario('reminder_channel_save')

    def test_actual_native_channel_and_history_labels_do_not_claim_playback_or_reading(self):
        self.scenario('reminder_channel_labels')

    def test_old_reminder_cancel_is_rejected(self):
        self.scenario('reminder_cancel')

    def test_old_reminder_form_stays_unsubmitted(self):
        self.scenario('reminder_form')

    def test_old_saved_result_list_is_rejected(self):
        self.scenario('saved_list')

    def test_old_saved_delete_confirmation_is_rejected(self):
        self.scenario('saved_delete')

    def test_saved_handoff_list_and_detail_keep_pending_status(self):
        self.scenario('saved_handoff')

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

    def test_actual_reminder_policy_apply_reset_and_ownership(self):
        for case in (
            'policy_apply',
            'policy_reset',
            'policy_stale',
            'policy_invalid',
            'policy_cancel',
        ):
            with self.subTest(case=case):
                self.scenario(case)

    def test_reading_controls_send_identity_once_and_explicit_restart(self):
        self.scenario('reading_controls')

    def test_reading_old_page_cannot_cross_route(self):
        self.scenario('reading_stale')

    def test_reading_requires_advertised_backend_capability(self):
        self.scenario('reading_capability')

    def test_reading_uncertain_write_never_uses_backup(self):
        self.scenario('reading_queue_failure')

    def test_result_source_view_and_explicit_receipt_file_choice(self):
        self.scenario('result_source')

    def test_old_result_file_choice_cannot_cross_a_route(self):
        self.scenario('result_stale_choice')

    def test_deleted_and_failed_files_do_not_request_reveal(self):
        self.scenario('result_bad_receipt')

    def test_old_backend_does_not_offer_result_reveal(self):
        self.scenario('result_capability')

    def test_saved_result_uses_saved_source_lookup(self):
        self.scenario('result_saved')

    def test_result_reveal_timeout_and_late_receipt_do_not_retry(self):
        self.scenario('result_timeout')

    def test_result_reveal_write_failure_does_not_retry_on_fallback(self):
        self.scenario('result_queue_failure')
