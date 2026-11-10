"""Execute real workflow dialogs/model/grants with inert platform widgets, never device media."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_recognition_controls_ui import STUBS as WIDGETS
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'


def stubs():
    result = dict(WIDGETS)
    result[
        'android/app/Activity.java'
    ] = """package android.app;public class Activity extends android.content.Context{
 public int permission,requested;public int checkSelfPermission(String name){return permission;}
 public void requestPermissions(String[] names,int code){requested=code;}}"""
    result['android/Manifest.java'] = (
        'package android;public class Manifest{public static class permission{public static final String CAMERA="camera";}}'
    )
    result['android/content/pm/PackageManager.java'] = (
        'package android.content.pm;public class PackageManager{public static final int PERMISSION_GRANTED=0;}'
    )
    result['android/os/Build.java'] = (
        'package android.os;public class Build{public static class VERSION{public static int SDK_INT=27;}}'
    )
    result['android/content/Context.java'] = """package android.content;public class Context{
 public static final String CLIPBOARD_SERVICE="clipboard";public Object getSystemService(String name){return new ClipboardManager();}}"""
    result['android/content/ClipboardManager.java'] = (
        'package android.content;public class ClipboardManager{public void setPrimaryClip(ClipData d){}}'
    )
    result['android/content/ClipData.java'] = (
        'package android.content;public class ClipData{public static ClipData newPlainText(String a,String b){return new ClipData();}}'
    )
    result['com/happyhome/cc8assistant/R.java'] = (
        'package com.happyhome.cc8assistant;public class R{public static class layout{public static int dialog_spinner_item=1;}}'
    )
    result['android/widget/ArrayAdapter.java'] = (
        'package android.widget;public class ArrayAdapter<T>{public ArrayAdapter(android.content.Context c,int n,T[] values){}}'
    )
    result[
        'android/widget/Spinner.java'
    ] = """package android.widget;public class Spinner extends android.view.View{
 int selected;public Spinner(android.content.Context c){}public void setAdapter(ArrayAdapter<?> a){}public void setSelection(int n){selected=n;}public int getSelectedItemPosition(){return selected;}}"""
    result['android/widget/TimePicker.java'] = 'package android.widget;public class TimePicker{}'
    result[
        'android/app/TimePickerDialog.java'
    ] = """package android.app;public class TimePickerDialog extends AlertDialog{
 public interface OnTimeSetListener{void onTimeSet(android.widget.TimePicker view,int hour,int minute);}final OnTimeSetListener listener;
 public TimePickerDialog(android.content.Context c,OnTimeSetListener l,int h,int m,boolean all){super(new Builder(c));listener=l;}
 public void choose(int h,int m){listener.onTimeSet(new android.widget.TimePicker(),h,m);dismiss();}}"""
    result['android/content/DialogInterface.java'] = result[
        'android/content/DialogInterface.java'
    ].replace(
        'interface OnClickListener',
        'interface OnMultiChoiceClickListener{void onClick(DialogInterface d,int n,boolean checked);}interface OnClickListener',
    )
    result['android/app/AlertDialog.java'] = (
        result['android/app/AlertDialog.java']
        .replace(
            'public Builder setTitle(String s){return this;}public Builder setMessage(String s){return this;}',
            'public String title,message;public boolean[] checked;public OnMultiChoiceClickListener multi;'
            'public Builder setMultiChoiceItems(String[] names,boolean[] selected,OnMultiChoiceClickListener l){labels=names;checked=selected;multi=l;return this;}'
            'public Builder setTitle(String s){title=s;return this;}public Builder setMessage(String s){message=s;return this;}',
        )
        .replace(
            'final java.util.Map<Integer,OnClickListener> actions',
            'public final java.util.Map<Integer,OnClickListener> actions',
        )
    )
    result['org/json/JSONObject.java'] = result['org/json/JSONObject.java'].replace(
        'public final java.util.Map',
        'public static final Object NULL=new Object();'
        'public int length(){return data.size();}public boolean has(String k){return data.containsKey(k);}'
        'public boolean isNull(String k){return opt(k)==null||opt(k)==NULL;}'
        'public java.util.Iterator<String> keys(){return data.keySet().iterator();}public final java.util.Map',
    )
    result[
        'com/happyhome/cc8assistant/VoiceSession.java'
    ] = """package com.happyhome.cc8assistant;import org.json.*;
 class VoiceSession{long route=1;boolean foreground=true,supported=true;String lastId;final java.util.List<JSONObject> requests=new java.util.ArrayList<>();
 long connectedRoute(){return route;}boolean currentRoute(long r){return foreground&&r==route;}boolean isConnected(){return foreground;}
 boolean supportsWorkflows(){return supported;}boolean workflowRequest(String id,JSONObject data,long owner){if(!currentRoute(owner)||!supported)return false;lastId=id;requests.add((JSONObject)WorkflowModel.copy(data));return true;}}"""
    return result


HARNESS = r"""package com.happyhome.cc8assistant;import org.json.*;import android.app.*;import android.os.*;
public class WorkflowCheck {
 static final String FLOW="a".repeat(32),STEP="b".repeat(32),OFFER="c".repeat(32);
 static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
 static JSONObject flow(String kind){JSONObject row=new JSONObject("id",STEP,"kind",kind,"title","实际步骤");
  if(kind.equals("work_report"))row.put("period","today").put("source",JSONObject.NULL);else row.put("text","实际输入");
  return new JSONObject("id",FLOW,"name","实际流程","steps",new JSONArray(row),"schedule",JSONObject.NULL);}
 static JSONObject state(){return new JSONObject("revision",1,"items",new JSONArray(new JSONObject("id",FLOW,"name","实际流程","step_count",1,"scheduled",false)),"attempts",new JSONArray(),"run",JSONObject.NULL,"journal_unconfirmed",false);}
 static JSONObject reply(VoiceSession session){return new JSONObject("request_id",session.lastId,"ok",true,"state",state());}
 static void detail(WorkflowDialog ui,VoiceSession session,String kind){ui.open();ui.accept(reply(session),1);AlertDialog.last.pick(0);ui.accept(reply(session).put("definition",flow(kind)),1);}
 static void confirm(WorkflowDialog ui,VoiceSession session,String kind){detail(ui,session,kind);AlertDialog.last.pick(0);ui.accept(reply(session).put("offer",new JSONObject("id",OFFER,"definition",flow(kind),"expires_in_seconds",90,"camera_required",kind.equals("ocr"))),1);}
 public static void main(String[] args){Activity activity=new Activity();VoiceSession session=new VoiceSession();WorkflowDialog ui=new WorkflowDialog(activity,session);
 switch(args[0]) {
 case "consent":{
  WorkflowConsent grant=new WorkflowConsent();yes(grant.offer(FLOW,1,100),"fresh grant rejected");
  yes(!grant.accepts(FLOW,1,7,101),"unbound camera granted");yes(grant.cancel(7).isEmpty(),"initial cancel revoked unbound grant");
  yes(!grant.bind(FLOW,2,7,101)&&grant.bind(FLOW,1,7,101),"wrong route bound");yes(grant.bind(FLOW,1,7,102),"duplicate receipt not idempotent");
  yes(!grant.accepts(FLOW,1,8,103)&&grant.accepts(FLOW,1,7,103),"generation fence missing");
  yes(grant.consume(FLOW,1,7,104)&&!grant.consume(FLOW,1,7,105),"grant reused");
  grant.offer(FLOW,1,200);grant.bind(FLOW,1,9,201);yes(grant.cancel(10).equals(FLOW)&&!grant.accepts(FLOW,1,9,202),"Stop retained grant");break;
 }
 case "expiry":{
  WorkflowConsent grant=new WorkflowConsent();yes(!grant.offer(FLOW,1,Long.MAX_VALUE),"overflow grant accepted");grant.offer(FLOW,1,100);
  yes(!grant.bind(FLOW,1,1,99)&&!grant.bind(FLOW,1,1,15100),"expired/rollback grant bound");
  grant.offer(FLOW,1,100);grant.bind(FLOW,1,1,101);yes(!grant.accepts(FLOW,1,1,15100),"grant expiry extended");break;
 }
 case "snapshot":{
  JSONObject value=flow("web_search"),saved=WorkflowModel.definition(value);yes(saved!=null,"valid flow rejected");
  value.optJSONArray("steps").optJSONObject(0).put("text","changed");yes(saved.optJSONArray("steps").optJSONObject(0).optString("text").equals("实际输入"),"mutable JSON retargeted flow");
  yes(!WorkflowModel.text(Character.toString((char)0xd800),500),"unpaired UTF16 accepted");
  yes(WorkflowModel.definition(flow("web_search").put("extra",true))==null,"unknown definition field accepted");
  JSONObject ocr=flow("ocr");ocr.put("schedule",new JSONObject("kind","interval","enabled",true,"configured_at",0,"seconds",300));
  yes(WorkflowModel.definition(ocr)==null,"unattended camera scheduled");
  JSONObject report=flow("work_report");report.put("schedule",new JSONObject("kind","interval","enabled",true,"configured_at",0,"seconds",300));
  yes(WorkflowModel.definition(report)==null,"unbound report scheduled");break;
 }
 case "once":{
  confirm(ui,session,"web_search");int count=session.requests.size();AlertDialog confirmation=AlertDialog.last;
  yes(session.requests.stream().noneMatch(x->"run".equals(x.optString("action"))),"prepare executed workflow");
  confirmation.click(-1);yes(session.requests.size()==count+1&&session.requests.get(count).optString("camera_nonce").isEmpty(),"explicit run not sent once");
  confirmation.builder.actions.get(-1).onClick(confirmation,-1);yes(session.requests.size()==count+1,"retired confirmation replayed run");break;
 }
 case "camera":{
  confirm(ui,session,"ocr");yes(AlertDialog.last.builder.message.contains("5 秒"),"visible preview omitted");
  AlertDialog.last.click(-1);JSONObject run=session.requests.get(session.requests.size()-1);
  yes(WorkflowModel.token(run.opt("camera_nonce"))&&Boolean.TRUE.equals(run.opt("confirmed")),"fresh camera confirmation missing");
  ui.accept(reply(session),1);yes(!AlertDialog.last.showing,"camera preview covered by results dialog");break;
 }
 case "permission":{
  activity.permission=-1;confirm(ui,session,"ocr");int count=session.requests.size();AlertDialog.last.click(-1);
  yes(activity.requested==34&&session.requests.size()==count,"permission request executed workflow or used wrong entry");
  activity.permission=0;yes(session.requests.size()==count,"grant auto-replayed camera workflow");break;
 }
 case "stale":{
  confirm(ui,session,"web_search");AlertDialog old=AlertDialog.last;ui.open();AlertDialog waiting=AlertDialog.last;int count=session.requests.size();
  old.builder.actions.get(-1).onClick(old,-1);yes(count==session.requests.size()&&waiting.showing,"old confirmation executed");
  String oldId=session.lastId;ui.open();waiting=AlertDialog.last;ui.accept(new JSONObject("request_id",oldId,"ok",true,"state",state()),1);yes(waiting.showing&&AlertDialog.last==waiting,"old receipt closed new wait");
  Runnable timeout=Handler.queued.get(Handler.queued.size()-1);ui.close();ui.open();waiting=AlertDialog.last;timeout.run();yes(waiting.showing,"old timeout closed fresh wait");break;
 }
 case "handover":{
  confirm(ui,session,"ocr");AlertDialog old=AlertDialog.last;int count=session.requests.size();session.route=2;old.builder.actions.get(-1).onClick(old,-1);
  yes(count==session.requests.size(),"old route opened camera");session.foreground=false;old.builder.actions.get(-1).onClick(old,-1);yes(count==session.requests.size(),"Home retained confirmation");break;
 }
 case "draft":{
  final java.util.List<JSONObject> saved=new java.util.ArrayList<>();WorkflowEditor editor=new WorkflowEditor(activity,session,(value,revision,route)->saved.add(value));
  JSONObject value=flow("web_search");value.put("schedule",new JSONObject("kind","interval","enabled",true,"configured_at",100,"seconds",300));
  editor.open(value,1,1);yes(saved.isEmpty(),"opening editor wrote configuration");AlertDialog.last.click(-1);yes(saved.isEmpty(),"review wrote configuration");AlertDialog.last.click(-1);
  yes(saved.size()==1&&saved.get(0).isNull("schedule")&&Boolean.TRUE.equals(value.optJSONObject("schedule").opt("enabled")),"draft did not disable schedule or mutated original");break;
 }
 case "schedule":{
  final java.util.List<JSONObject> saved=new java.util.ArrayList<>();WorkflowEditor editor=new WorkflowEditor(activity,session,(value,revision,route)->saved.add(value));
  editor.schedules(flow("ocr"),1,1);AlertDialog.last.pick(1);yes(saved.isEmpty(),"OCR timer saved");
  editor.schedules(flow("web_search"),1,1);AlertDialog.last.pick(4);((TimePickerDialog)AlertDialog.last).choose(8,30);AlertDialog.last.click(-1);
  yes(AlertDialog.last.builder.message.contains("08:30")&&AlertDialog.last.builder.message.contains("周一"),"human schedule summary missing");yes(saved.isEmpty(),"schedule saved before confirmation");
  AlertDialog.last.click(-1);yes(saved.size()==1&&saved.get(0).optJSONObject("schedule").optJSONArray("weekdays").length()==7,"daily choices lost");break;
 }
 case "wrong_definition":{
  ui.open();ui.accept(reply(session),1);AlertDialog.last.pick(0);JSONObject other=flow("web_search").put("id","f".repeat(32));
  ui.accept(reply(session).put("definition",other),1);yes(!AlertDialog.last.showing,"different flow substituted under requested ID");break;
 }
 default:throw new AssertionError("Unknown workflow case");
 }
 }
}"""


class WorkflowUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        files = []
        for name, source in {
            **stubs(),
            'com/happyhome/cc8assistant/WorkflowCheck.java': HARNESS,
        }.items():
            path = cls.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
            files.append(str(path))
        files += [
            str(JAVA / name)
            for name in (
                'WorkflowModel.java',
                'WorkflowConsent.java',
                'WorkflowDialog.java',
                'WorkflowEditor.java',
            )
        ]
        cls.java = java_bin()
        result = subprocess.run(
            [str(cls.java / 'javac'), '-encoding', 'UTF-8', '-d', str(cls.folder), *files],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise AssertionError(result.stderr)

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(self.folder),
                'com.happyhome.cc8assistant.WorkflowCheck',
                name,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_generation_bound_single_use_camera_grant(self):
        self.scenario('consent')

    def test_grant_expiry_rollback_and_overflow(self):
        self.scenario('expiry')

    def test_definition_snapshot_and_unsafe_schedule_rejection(self):
        self.scenario('snapshot')

    def test_prepare_is_read_only_and_confirm_is_once_only(self):
        self.scenario('once')

    def test_camera_confirmation_leaves_preview_uncovered(self):
        self.scenario('camera')

    def test_camera_permission_does_not_automatically_run_or_capture(self):
        self.scenario('permission')

    def test_retired_confirmation_ack_and_timeout_do_not_retarget_current_window(self):
        self.scenario('stale')

    def test_route_and_home_revoke_old_confirmation(self):
        self.scenario('handover')

    def test_draft_requires_final_save_and_disables_prior_schedule(self):
        self.scenario('draft')

    def test_daily_schedule_requires_explicit_safe_confirmation(self):
        self.scenario('schedule')

    def test_mismatched_definition_does_not_replace_selected_flow(self):
        self.scenario('wrong_definition')
