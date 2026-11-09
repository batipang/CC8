"""Compile actual native metadata/report dialogs against inert UI and route objects."""

from pathlib import Path
import subprocess
import tempfile
import unittest
import test_recognition_controls_ui as widgets
from tools.java_toolchain import java_bin

HARNESS = r"""package com.happyhome.cc8assistant;
import org.json.*;import android.app.*;
public class ReportSourcesCheck {
 static void yes(boolean value,String why){if(!value)throw new AssertionError(why);}
 static JSONObject row(){return new JSONObject("id","a".repeat(16),"provider","codex","host","local","thread_id","original-chat","title","原聊天","project","项目");}
 static JSONObject state(JSONObject row){return new JSONObject("schema",1,"revision",1,"items",new JSONArray(row));}
 static JSONObject result(VoiceSession session,JSONObject registry){return new JSONObject("request_id",session.lastId,"ok",true,"registry",registry,"report_source_title","已登记聊天");}
 static void master(ReportSourcesDialog ui,VoiceSession s){ui.open();ui.accept(result(s,state(row())));}
 static JSONObject candidate(){return new JSONObject("id","offered-chat","title","已选择聊天","project","原项目");}
 static void candidates(ReportSourcesDialog ui,VoiceSession s,JSONObject candidate){
  AlertDialog.last.click(-1);
  ui.accept(result(s,state(row())).put("items",new JSONArray(candidate)));
 }
 static void report(ReportSourcesDialog ui,VoiceSession s,JSONObject payload){
  master(ui,s);AlertDialog.last.click(-3);AlertDialog.last.pick(2);AlertDialog.last.pick(0);ui.accept(payload.put("request_id",s.lastId));
 }
 static JSONObject reportPayload(){return new JSONObject("ok",true,"action","report","report_text","只读摘要", "report",new JSONObject("snapshot_id","c".repeat(32)),"archive_revision",2);}
 static JSONObject archiveRow(){return new JSONObject("id","d".repeat(32),"source_title","原来源","saved_at",150,"coverage_complete",false);}
 static JSONObject archiveState(JSONObject row){return new JSONObject("revision",3,"items",new JSONArray(row));}
 static void archives(ReportSourcesDialog ui,VoiceSession s,JSONObject state){
  master(ui,s);AlertDialog.last.click(-3);AlertDialog.last.pick(4);
  ui.accept(new JSONObject("request_id",s.lastId,"ok",true,"action","saved_reports","saved_reports",state));
 }
 static JSONObject progressRow(){return new JSONObject("id","e".repeat(32),"source_title","原来源","saved_at",150,"total",4,"delivered",1);}
 static JSONObject progressState(JSONObject row){return new JSONObject("revision",3,"items",new JSONArray(row));}
 static void checkpoints(ReportSourcesDialog ui,VoiceSession s,JSONObject payload){
  master(ui,s);AlertDialog.last.click(-3);AlertDialog.last.pick(5);ui.accept(payload.put("request_id",s.lastId));
 }
 static JSONObject progressPayload(){return new JSONObject("ok",true,"progress_checkpoints",progressState(progressRow()),"offer_id","f".repeat(32),"current_progress_text","待补播3项，不执行任务");}
 public static void main(String[] args){VoiceSession s=new VoiceSession();ReportSourcesDialog ui=new ReportSourcesDialog(new Activity(),s);
  switch(args[0]){
   case "register":{
    master(ui,s);JSONObject offered=candidate();candidates(ui,s,offered);int before=s.requests.size();
    offered.put("id","replacement-chat").put("title","替换来源");AlertDialog.last.pick(0);
    yes(s.requests.size()==before,"selection persisted without explicit consent");AlertDialog.last.click(-1);
    JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("register_source".equals(sent.optString("action"))&&"offered-chat".equals(sent.optString("source_id"))&&Boolean.TRUE.equals(sent.opt("confirmed"))&&sent.optInt("revision")==1,"frozen source/consent/revision not preserved");
    break;
   }
   case "stale":{
    master(ui,s);AlertDialog retired=AlertDialog.last;ui.open();AlertDialog waiting=AlertDialog.last;int count=s.requests.size();
    retired.builder.item.onClick(retired,0);retired.builder.actions.get(-1).onClick(retired,-1);
    yes(s.requests.size()==count&&AlertDialog.last==waiting&&waiting.showing,"retired metadata window changed current request");
    Runnable old=android.os.Handler.queued.get(android.os.Handler.queued.size()-1);ui.close();ui.open();AlertDialog next=AlertDialog.last;old.run();
    yes(next.showing&&AlertDialog.last==next,"old timeout closed a new wait");break;
   }
   case "cancel_confirm":{
    master(ui,s);candidates(ui,s,candidate());AlertDialog.last.pick(0);AlertDialog confirm=AlertDialog.last;
    int count=s.requests.size();ui.close();s.foreground=false;confirm.builder.actions.get(-1).onClick(confirm,-1);
    yes(s.requests.size()==count&&!confirm.showing,"closed confirmation registered metadata");break;
   }
   case "delete":{
    ui.open();JSONObject mutable=row(),registry=state(mutable);ui.accept(result(s,registry));
    mutable.put("id","b".repeat(16));registry.put("revision",9);AlertDialog.last.pick(0);AlertDialog.last.click(-1);
    JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("delete_source".equals(sent.optString("action"))&&"a".repeat(16).equals(sent.optString("registration_id"))&&sent.optInt("revision")==1,"mutable reply retargeted deletion");break;
   }
   case "handover":{
    ui.open();String old=s.lastId;s.route=2;ui.close();ui.open();AlertDialog current=AlertDialog.last;
    ui.accept(new JSONObject("request_id",old,"ok",true,"registry",state(row())));
    yes(current.showing&&AlertDialog.last==current,"old route response replaced new route UI");break;
   }
   case "scope_report":{
    master(ui,s);AlertDialog.last.click(-3);AlertDialog.last.pick(0);
    JSONObject sent=s.requests.get(s.requests.size()-1);yes("use_sources".equals(sent.optString("action"))&&sent.optInt("revision")==1,"explicit scope not requested");
    ui.accept(result(s,state(row())));AlertDialog.last.click(-3);AlertDialog.last.pick(2);AlertDialog.last.pick(1);
    sent=s.requests.get(s.requests.size()-1);yes("report".equals(sent.optString("action"))&&"two_hours".equals(sent.optString("period")),"explicit range not preserved");
    int count=s.requests.size();ui.accept(new JSONObject("request_id",s.lastId,"ok",true,"report_text","已登记范围：只读结果"));
    yes(s.requests.size()==count&&AlertDialog.last.showing,"report opened a model request");ui.close();yes(!AlertDialog.last.showing,"Home retained report body UI");break;
   }
   case "invalid":{
    ui.open();ui.accept(result(s,state(row()).put("revision",true)));
    yes(!AlertDialog.last.showing,"Boolean revision opened an actionable list");
    ui.open();ui.accept(result(s,state(row().put("host","remote"))));yes(!AlertDialog.last.showing,"unsupported host accepted");
    ui.open();ui.accept(result(s,state(row().put("thread_id",42))));yes(!AlertDialog.last.showing,"numeric chat ID coerced to text");break;
   }
   case "timeout":{
    master(ui,s);candidates(ui,s,candidate());AlertDialog.last.pick(0);AlertDialog.last.click(-1);
    int count=s.requests.size();Runnable timer=android.os.Handler.queued.get(android.os.Handler.queued.size()-1);timer.run();
    yes(s.requests.size()==count&&!AlertDialog.last.showing,"uncertain mutation retried or kept UI alive");break;
   }
   case "archive_save":{
    JSONObject data=reportPayload();report(ui,s,data);int count=s.requests.size();AlertDialog.last.click(-1);
    data.put("archive_revision",9);data.optJSONObject("report").put("snapshot_id","f".repeat(32));
    yes(s.requests.size()==count,"save did not require confirmation");AlertDialog.last.click(-1);
    JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("save_report".equals(sent.optString("action"))&&"c".repeat(32).equals(sent.optString("snapshot_id"))&&sent.optInt("revision")==2&&Boolean.TRUE.equals(sent.opt("confirmed")),"save confirmation retargeted immutable report");break;
   }
   case "archive_stale":{
    report(ui,s,reportPayload());AlertDialog.last.click(-1);AlertDialog confirm=AlertDialog.last;int count=s.requests.size();
    ui.close();s.route++;confirm.builder.actions.get(-1).onClick(confirm,-1);
    yes(s.requests.size()==count,"old confirmation saved after Home/handover");break;
   }
   case "archive_delete":{
    JSONObject row=archiveRow(),state=archiveState(row);archives(ui,s,state);
    yes(AlertDialog.last.builder.labels[0].contains("01-01 08:02")&&AlertDialog.last.builder.labels[0].contains("覆盖不完整"),"archive list lost saved time or coverage");
    AlertDialog.last.pick(0);yes("d".repeat(32).equals(s.requests.get(s.requests.size()-1).optString("saved_id")),"selected wrong snapshot");
    JSONObject response=new JSONObject("request_id",s.lastId,"ok",true,"action","saved_report","saved_reports",state,"saved_id","d".repeat(32),"saved_report_text","历史快照正文");
    ui.accept(response);AlertDialog.last.click(-3);int count=s.requests.size();
    row.put("id","e".repeat(32));state.put("revision",8);response.put("saved_id","e".repeat(32));
    yes(s.requests.size()==count,"delete did not require confirmation");AlertDialog.last.click(-1);
    JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("delete_report".equals(sent.optString("action"))&&"d".repeat(32).equals(sent.optString("saved_id"))&&sent.optInt("revision")==3,"mutable receipt retargeted delete");break;
   }
   case "archive_copy":{
    JSONObject state=archiveState(archiveRow());archives(ui,s,state);AlertDialog.last.pick(0);
    JSONObject data=new JSONObject("request_id",s.lastId,"ok",true,"action","saved_report","saved_reports",state,"saved_id","d".repeat(32),"saved_report_text","历史快照正文");
    ui.accept(data);data.put("saved_report_text","替换正文");int count=s.requests.size();AlertDialog.last.click(-1);
    yes("历史快照正文".equals(android.content.ClipboardManager.text)&&s.requests.size()==count,"copy changed body or sent work");break;
   }
   case "archive_clear":{
    archives(ui,s,archiveState(archiveRow()));int count=s.requests.size();AlertDialog.last.click(-3);yes(s.requests.size()==count,"clear lacked confirmation");
    AlertDialog.last.click(-1);JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("clear_reports".equals(sent.optString("action"))&&sent.optInt("revision")==3&&Boolean.TRUE.equals(sent.opt("confirmed")),"clear lost revision or consent");break;
   }
   case "archive_capability":{
    s.archive=false;report(ui,s,reportPayload());yes(!AlertDialog.last.builder.actions.containsKey(-1),"old host offered save");break;
   }
   case "source_queue":{
    ui.open();JSONObject mutable=row(),registry=state(mutable);ui.accept(result(s,registry));
    mutable.put("id","b".repeat(16));registry.put("revision",8);AlertDialog.last.pick(0);AlertDialog.last.click(-3);
    JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("source_queue".equals(sent.optString("action"))&&"a".repeat(16).equals(sent.optString("registration_id"))&&sent.optInt("revision")==1,"queue read retargeted mutable source");
    yes(sent.opt("thread_id")==null&&sent.opt("confirmed")==null,"queue read supplied arbitrary target or mutation consent");
    int count=s.requests.size();JSONObject reply=new JSONObject("request_id",s.lastId,"ok",true,"queue_text","原聊天待办 · 只读");ui.accept(reply);
    reply.put("queue_text","changed body");AlertDialog.last.click(-1);
    yes("原聊天待办 · 只读".equals(android.content.ClipboardManager.text)&&s.requests.size()==count,"queue copy changed snapshot or sent work");break;
   }
   case "source_queue_stale":{
    master(ui,s);AlertDialog.last.pick(0);AlertDialog retired=AlertDialog.last;int count=s.requests.size();ui.close();s.route++;
    retired.builder.actions.get(-3).onClick(retired,-3);yes(s.requests.size()==count,"old source menu read another queue");break;
   }
   case "source_queue_capability":{
    s.threadQueue=false;master(ui,s);AlertDialog.last.pick(0);yes(!AlertDialog.last.builder.actions.containsKey(-3),"old host offered queue inspection");break;
   }
   case "progress_save":{
    JSONObject payload=progressPayload();checkpoints(ui,s,payload);
    payload.put("offer_id","b".repeat(32));payload.optJSONObject("progress_checkpoints").put("revision",8);
    int before=s.requests.size();AlertDialog.last.click(-1);yes(s.requests.size()==before,"progress saved without consent");AlertDialog.last.click(-1);
    JSONObject sent=s.requests.get(s.requests.size()-1);yes("save_progress".equals(sent.optString("action"))&&sent.optInt("revision")==3&&"f".repeat(32).equals(sent.optString("offer_id"))&&Boolean.TRUE.equals(sent.opt("confirmed")),"progress save consent retargeted");
    yes(sent.opt("entries")==null&&sent.opt("thread_id")==null&&sent.opt("path")==null,"device supplied progress body/target");break;
   }
   case "progress_restore":{
    checkpoints(ui,s,progressPayload());AlertDialog.last.pick(0);
    JSONObject detail=progressPayload().put("request_id",s.lastId).put("checkpoint_id","e".repeat(32)).put("progress_text","原来源：待补播3项").put("can_restore",true);ui.accept(detail);
    detail.put("checkpoint_id","a".repeat(32));int count=s.requests.size();AlertDialog.last.click(-1);yes(s.requests.size()==count,"progress restored without confirmation");AlertDialog.last.click(-1);
    JSONObject sent=s.requests.get(s.requests.size()-1);yes("restore_progress".equals(sent.optString("action"))&&"e".repeat(32).equals(sent.optString("checkpoint_id"))&&sent.optInt("revision")==3&&Boolean.TRUE.equals(sent.opt("confirmed")),"progress restore target/revision changed");break;
   }
   case "progress_delete":{
    checkpoints(ui,s,progressPayload());AlertDialog.last.pick(0);ui.accept(progressPayload().put("request_id",s.lastId).put("checkpoint_id","e".repeat(32)).put("progress_text","原来源").put("can_restore",false));
    AlertDialog.last.click(-3);AlertDialog.last.click(-1);JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("delete_progress".equals(sent.optString("action"))&&"e".repeat(32).equals(sent.optString("checkpoint_id")),"progress delete changed target");break;
   }
   case "progress_clear":{
    checkpoints(ui,s,progressPayload());AlertDialog.last.click(-3);AlertDialog.last.click(-1);JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("clear_progress".equals(sent.optString("action"))&&sent.opt("checkpoint_id")==null&&sent.optInt("revision")==3,"progress clear not explicitly bound");break;
   }
   case "progress_stale":{
    checkpoints(ui,s,progressPayload());AlertDialog.last.click(-1);AlertDialog retired=AlertDialog.last;int count=s.requests.size();ui.close();s.route++;
    retired.builder.actions.get(-1).onClick(retired,-1);yes(s.requests.size()==count,"old progress confirmation survived handover");break;
   }
   case "progress_capability":{
    s.reportProgress=false;master(ui,s);AlertDialog.last.click(-3);int count=s.requests.size();AlertDialog.last.pick(5);yes(s.requests.size()==count,"unsupported host received progress request");break;
   }
   case "progress_invalid":{
    JSONObject payload=progressPayload();payload.optJSONObject("progress_checkpoints").optJSONArray("items").optJSONObject(0).put("delivered",5);
    checkpoints(ui,s,payload);yes(!AlertDialog.last.showing,"malformed progress offered mutations");break;
   }
   default:throw new AssertionError("unknown case");
  }
 }
}"""


class ReportSourcesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        stubs = dict(widgets.STUBS)
        stubs['org/json/JSONObject.java'] = stubs['org/json/JSONObject.java'].replace(
            'public JSONArray optJSONArray',
            'public boolean isNull(String k){return opt(k)==null;}public JSONArray optJSONArray',
        )
        stubs['com/happyhome/cc8assistant/VoiceSession.java'] = stubs[
            'com/happyhome/cc8assistant/VoiceSession.java'
        ].replace(
            'boolean supportsVocabulary()',
            'boolean supportsReportSources(){return true;}boolean archive=true,threadQueue=true,reportProgress=true;boolean supportsReportProgress(){return reportProgress;}boolean supportsThreadQueue(){return threadQueue;}boolean supportsReportArchive(){return archive;}void dotChatCancel(String id){}'
            'boolean reportSourcesRequest(String id,JSONObject args,long r){if(!currentRoute(r))return false;lastId=id;requests.add(args);return true;}'
            'boolean supportsVocabulary()',
        )
        stubs['android/app/AlertDialog.java'] = stubs['android/app/AlertDialog.java'].replace(
            'final java.util.Map<Integer,OnClickListener> actions=',
            'public final java.util.Map<Integer,OnClickListener> actions=',
        )
        stubs['android/content/Context.java'] = (
            'package android.content;public class Context{public static final String CLIPBOARD_SERVICE="clipboard";public Object getSystemService(String s){return new ClipboardManager();}}'
        )
        stubs['android/content/ClipData.java'] = (
            'package android.content;public class ClipData{public String text;public static ClipData newPlainText(String title,String text){ClipData d=new ClipData();d.text=text;return d;}}'
        )
        stubs['android/content/ClipboardManager.java'] = (
            'package android.content;public class ClipboardManager{public static String text;public void setPrimaryClip(ClipData data){text=data.text;}}'
        )
        files = []
        for name, source in {
            **stubs,
            'com/happyhome/cc8assistant/ReportSourcesCheck.java': HARNESS,
        }.items():
            path = cls.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
            files.append(str(path))
        files.append(str(widgets.JAVA / 'ReportSourcesDialog.java'))
        cls.java = java_bin()
        result = subprocess.run(
            [str(cls.java / 'javac'), '-encoding', 'UTF-8', '-d', str(cls.folder), *files],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr)

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(self.folder),
                'com.happyhome.cc8assistant.ReportSourcesCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registration_uses_frozen_offered_source_and_explicit_metadata_consent(self):
        self.scenario('register')

    def test_retired_list_and_timeout_cannot_touch_the_current_owner(self):
        self.scenario('stale')

    def test_closed_confirmation_cannot_register_metadata(self):
        self.scenario('cancel_confirm')

    def test_delete_preserves_original_revision_and_registration_id(self):
        self.scenario('delete')

    def test_previous_route_receipt_cannot_replace_a_new_wait(self):
        self.scenario('handover')

    def test_explicit_scope_and_range_show_readonly_report_then_close_on_home(self):
        self.scenario('scope_report')

    def test_malformed_registry_and_unsupported_host_fail_closed(self):
        self.scenario('invalid')

    def test_unconfirmed_mutation_timeout_never_retries(self):
        self.scenario('timeout')

    def test_save_confirmation_is_bound_to_the_original_report_and_revision(self):
        self.scenario('archive_save')

    def test_old_save_confirmation_cannot_survive_home_or_handover(self):
        self.scenario('archive_stale')

    def test_delete_and_clear_require_current_immutable_snapshot_consent(self):
        self.scenario('archive_delete')
        self.scenario('archive_clear')

    def test_copy_uses_only_the_displayed_historical_body_without_rpc(self):
        self.scenario('archive_copy')

    def test_older_host_cannot_offer_persistent_save(self):
        self.scenario('archive_capability')

    def test_source_queue_uses_frozen_registration_and_only_copies_current_readonly_body(self):
        self.scenario('source_queue')

    def test_source_queue_cannot_use_an_old_window_or_unsupported_host(self):
        self.scenario('source_queue_stale')
        self.scenario('source_queue_capability')

    def test_progress_save_and_restore_require_current_frozen_explicit_consent(self):
        self.scenario('progress_save')
        self.scenario('progress_restore')

    def test_progress_delete_and_clear_do_not_retarget_or_submit_bodies(self):
        self.scenario('progress_delete')
        self.scenario('progress_clear')

    def test_progress_old_confirmation_unsupported_host_and_bad_counts_fail_closed(self):
        self.scenario('progress_stale')
        self.scenario('progress_capability')
        self.scenario('progress_invalid')
