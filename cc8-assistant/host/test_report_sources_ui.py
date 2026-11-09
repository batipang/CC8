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
            'boolean supportsReportSources(){return true;}void dotChatCancel(String id){}'
            'boolean reportSourcesRequest(String id,JSONObject args,long r){if(!currentRoute(r))return false;lastId=id;requests.add(args);return true;}'
            'boolean supportsVocabulary()',
        )
        stubs['android/app/AlertDialog.java'] = stubs['android/app/AlertDialog.java'].replace(
            'final java.util.Map<Integer,OnClickListener> actions=',
            'public final java.util.Map<Integer,OnClickListener> actions=',
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
