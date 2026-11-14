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

 static JSONObject watchPayload(VoiceSession s){return new JSONObject("request_id",s.lastId,"ok",true,"report_watch",new JSONObject("schema",1,"revision",4,"configured",true,"enabled",true),"watch_text","原来源：等待允许检查","watch_offer_id","a".repeat(32),"watch_offer_text","原来源与成员");}
 static void watch(ReportSourcesDialog ui,VoiceSession s,JSONObject payload){ui.open();ui.accept(result(s,state(row())).put("report_watch_supported",true));AlertDialog.last.click(-3);AlertDialog.last.pick(6);ui.accept(payload.put("request_id",s.lastId));}
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
   case "calendar_report":{
    for(Object supported:new Object[]{true,"true"}){
     ui.open();ui.accept(result(s,state(row())).put("work_report_calendar_supported",supported));
     AlertDialog.last.click(-3);AlertDialog.last.pick(2);
     AlertDialog periods=AlertDialog.last;
     yes(periods.builder.labels.length==(Boolean.TRUE.equals(supported)?5:3),"calendar negotiation ignored");
     if(Boolean.TRUE.equals(supported)){
      periods.pick(4);JSONObject sent=s.requests.get(s.requests.size()-1);
      yes("report".equals(sent.optString("action"))&&"last_week".equals(sent.optString("period")),"last week not submitted");
     }
     ui.close();
    }break;
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
    for(Object supported:new Object[]{true,null,"true"}){
     JSONObject data=reportPayload().put("archive_receipts_supported",supported);report(ui,s,data);
     int count=s.requests.size();AlertDialog.last.click(-1);
     data.put("archive_revision",9);data.optJSONObject("report").put("snapshot_id","f".repeat(32));
     data.put("archive_receipts_supported",!Boolean.TRUE.equals(supported));
     yes(s.requests.size()==count,"save did not require confirmation");AlertDialog.last.click(-1);
     JSONObject sent=s.requests.get(s.requests.size()-1);
     yes("save_report".equals(sent.optString("action"))&&"c".repeat(32).equals(sent.optString("snapshot_id"))&&sent.optInt("revision")==2&&Boolean.TRUE.equals(sent.opt("confirmed")),"save confirmation retargeted immutable report");
     yes(Boolean.TRUE.equals(sent.opt("include_receipts"))==Boolean.TRUE.equals(supported),"saved evidence consent was added for an older host or changed during confirmation");
     ui.close();
    }break;
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

   case "live_pages":{
    JSONObject data=reportPayload().put("report_page",0).put("report_pages",2).put("report_page_marker","【第1/2页；请翻页继续查看】\n\n").put("report_text","【第1/2页；请翻页继续查看】\n\n第一页");
    report(ui,s,data);data.optJSONObject("report").put("snapshot_id","e".repeat(32));AlertDialog retired=AlertDialog.last;AlertDialog.last.click(-1);
    JSONObject sent=s.requests.get(s.requests.size()-1);yes("report_page".equals(sent.optString("action"))&&"c".repeat(32).equals(sent.optString("snapshot_id"))&&sent.optInt("page")==1,"paging retargeted mutable report");
    JSONObject reply=reportPayload().put("request_id",s.lastId).put("action","report_page").put("report_page",1).put("report_pages",2).put("report_page_marker","【第2/2页；请翻页继续查看】\n\n").put("report_text","【第2/2页；请翻页继续查看】\n\n第二页");ui.accept(reply);
    int before=s.requests.size();retired.builder.actions.get(-1).onClick(retired,-1);yes(s.requests.size()==before,"retired page sent request");
    AlertDialog.last.click(-3);AlertDialog.last.pick(0);sent=s.requests.get(s.requests.size()-1);yes(sent.optInt("page")==0,"previous page unavailable");break;
   }
   case "archive_pages_copy":{
    JSONObject state=archiveState(archiveRow());archives(ui,s,state);AlertDialog.last.pick(0);
    JSONObject data=new JSONObject("request_id",s.lastId,"ok",true,"action","saved_report","saved_reports",state,"saved_id","d".repeat(32),"saved_report_text","【第1/2页；请翻页继续查看】\n\n第一页😀","report_page",0,"report_pages",2,"report_page_marker","【第1/2页；请翻页继续查看】\n\n");
    ui.accept(data);AlertDialog.last.click(-3);AlertDialog.last.pick(2);JSONObject sent=s.requests.get(s.requests.size()-1);
    yes(sent.optInt("page")==0&&sent.optInt("revision")==3&&"d".repeat(32).equals(sent.optString("saved_id")),"copy did not freeze record/revision");
    android.content.ClipboardManager.text="原剪贴板";ui.accept(data.put("request_id",s.lastId));yes("原剪贴板".equals(android.content.ClipboardManager.text),"partial page copied");
    sent=s.requests.get(s.requests.size()-1);yes(sent.optInt("page")==1&&sent.optInt("revision")==3,"copy did not request next revision-bound page");
    ui.accept(new JSONObject("request_id",s.lastId,"ok",true,"action","saved_report","saved_reports",state,"saved_id","d".repeat(32),"saved_report_text","【第2/2页；请翻页继续查看】\n\n第二页","report_page",1,"report_pages",2,"report_page_marker","【第2/2页；请翻页继续查看】\n\n"));
    yes("第一页😀第二页".equals(android.content.ClipboardManager.text),"full clipboard not lossless or includes page markers");break;
   }
   case "copy_cancel_revision":{
    for(int mode=0;mode<4;mode++){
     JSONObject state=archiveState(archiveRow());archives(ui,s,state);AlertDialog.last.pick(0);
     JSONObject data=new JSONObject("request_id",s.lastId,"ok",true,"action","saved_report","saved_reports",state,"saved_id","d".repeat(32),"saved_report_text","【第1/2页；请翻页继续查看】\n\n第一页","report_page",0,"report_pages",2,"report_page_marker","【第1/2页；请翻页继续查看】\n\n");
     ui.accept(data);AlertDialog.last.click(-3);AlertDialog.last.pick(2);android.content.ClipboardManager.text="保留";
     if(mode==0)ui.close();if(mode==1)s.route++;if(mode==2)state.put("revision",4);if(mode==3)android.os.Handler.queued.get(android.os.Handler.queued.size()-1).run();
     int before=s.requests.size();ui.accept(data.put("request_id",s.lastId));yes("保留".equals(android.content.ClipboardManager.text)&&before==s.requests.size(),"canceled/revised copy continued or modified clipboard");
    }break;
   }
   case "bad_page":{
    JSONObject[] bad={reportPayload().put("report_page",true).put("report_pages",2).put("report_page_marker",""),reportPayload().put("report_page",0).put("report_pages",17).put("report_page_marker",""),reportPayload().put("report_page",0).put("report_pages",2).put("report_page_marker","wrong")};
    for(JSONObject data:bad){report(ui,s,data);yes(!AlertDialog.last.showing,"malformed pages offered actions");}break;
   }

   case "copy_bounds":{
    JSONObject state=archiveState(archiveRow());
    ReportTextPage.Copy job=new ReportTextPage.Copy("d".repeat(32),3,16,s.route);
    for(int i=0;i<16;i++){
     String marker="【第"+(i+1)+"/16页；请翻页继续查看】\n\n";
     JSONObject data=new JSONObject("saved_reports",state,"saved_id","d".repeat(32),"saved_report_text",marker+"a".repeat(5000),"report_page",i,"report_pages",16,"report_page_marker",marker);
     if(i==13){yes(!job.append(data)&&job.completed()==null,"oversized copy escaped capacity bound");break;}yes(job.append(data),"valid page rejected");
    }
    job=new ReportTextPage.Copy("d".repeat(32),3,2,s.route);
    try{java.lang.reflect.Field deadline=ReportTextPage.Copy.class.getDeclaredField("deadline");deadline.setAccessible(true);deadline.setLong(job,0);}catch(Exception e){throw new AssertionError(e);}
    yes(!job.alive()&&job.completed()==null,"expired copy remained usable");break;
   }

   case "watch_configure":{
    JSONObject data=watchPayload(s);watch(ui,s,data);int count=s.requests.size();AlertDialog.last.click(-1);AlertDialog.last.pick(1);AlertDialog.last.pick(1);
    data.put("watch_offer_id","b".repeat(32));data.optJSONObject("report_watch").put("revision",99);
    yes(s.requests.size()==count,"watch configured without final consent");AlertDialog.last.click(-1);JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("configure_watch".equals(sent.optString("action"))&&"a".repeat(32).equals(sent.optString("offer_id"))&&sent.optInt("revision")==4&&sent.optInt("period_seconds")==900&&"desktop".equals(sent.optString("channel"))&&Boolean.TRUE.equals(sent.opt("confirmed")),"watch consent/source/period/channel retargeted");
    yes(sent.opt("source_id")==null&&sent.opt("text")==null&&sent.opt("path")==null,"watch supplied target/body");break;
   }
   case "watch_management":{
    for(int n=0;n<2;n++){
     JSONObject data=watchPayload(s);watch(ui,s,data);AlertDialog.last.click(-3);AlertDialog.last.pick(n);int count=s.requests.size();yes(s.requests.size()==count,"watch management changed before consent");
     data.optJSONObject("report_watch").put("revision",9);AlertDialog.last.click(-1);JSONObject sent=s.requests.get(s.requests.size()-1);
     yes((n==0?"pause_watch":"clear_watch").equals(sent.optString("action"))&&sent.optInt("revision")==4&&Boolean.TRUE.equals(sent.opt("confirmed")),"watch management retargeted");
    }break;
   }
   case "watch_stale":{
    watch(ui,s,watchPayload(s));AlertDialog.last.click(-1);AlertDialog.last.pick(0);AlertDialog.last.pick(0);AlertDialog old=AlertDialog.last;int count=s.requests.size();
    ui.close();s.route++;old.builder.actions.get(-1).onClick(old,-1);yes(s.requests.size()==count,"old watch confirmation survived Home/route");
    watch(ui,s,watchPayload(s));AlertDialog.last.click(-1);AlertDialog.last.pick(0);AlertDialog.last.pick(0);AlertDialog.last.click(-1);Runnable timer=android.os.Handler.queued.get(android.os.Handler.queued.size()-1);count=s.requests.size();timer.run();
    ui.accept(watchPayload(s));yes(!AlertDialog.last.showing&&s.requests.size()==count,"watch timeout retried or accepted late receipt");break;
   }
   case "watch_summary":{
    JSONObject data=watchPayload(s).put("watch_summary_supported",true);watch(ui,s,data);int before=s.requests.size();AlertDialog.last.click(-1);AlertDialog.last.pick(0);AlertDialog.last.pick(0);AlertDialog.last.pick(1);
    data.put("watch_summary_supported",false).put("watch_offer_id","b".repeat(32));data.optJSONObject("report_watch").put("revision",99);
    yes(s.requests.size()==before,"summary enabled before final consent");AlertDialog.last.click(-1);JSONObject sent=s.requests.get(s.requests.size()-1);
    yes("summary".equals(sent.opt("mode"))&&"cc8".equals(sent.opt("channel"))&&sent.optInt("revision")==4&&"a".repeat(32).equals(sent.opt("offer_id"))&&Boolean.TRUE.equals(sent.opt("confirmed")),"summary consent/source/mode changed");break;
   }
   case "watch_results":{
    JSONObject data=watchPayload(s).put("watch_has_results",true);watch(ui,s,data);AlertDialog.last.click(-3);AlertDialog.last.pick(2);
    JSONObject sent=s.requests.get(s.requests.size()-1);yes("watch_result".equals(sent.optString("action"))&&sent.opt("confirmed")==null,"recent read sent mutation");
    JSONObject page=new JSONObject("request_id",s.lastId,"ok",true,"action","watch_result","watch_result",true,"watch_result_ttl_ms",1000,"snapshot_id","c".repeat(32),"report_text","【第1/2页；请翻页继续查看】\n\n第一页","report_page",0,"report_pages",2,"report_page_marker","【第1/2页；请翻页继续查看】\n\n");ui.accept(page);
    AlertDialog retired=AlertDialog.last;page.put("snapshot_id","e".repeat(32));AlertDialog.last.click(-1);sent=s.requests.get(s.requests.size()-1);
    yes("watch_result_page".equals(sent.optString("action"))&&"c".repeat(32).equals(sent.optString("snapshot_id"))&&sent.optInt("page")==1,"recent paging retargeted mutable response");
    ui.accept(new JSONObject("request_id",s.lastId,"ok",true,"action","watch_result_page","watch_result",true,"watch_result_ttl_ms",1000,"snapshot_id","c".repeat(32),"report_text","【第2/2页；请翻页继续查看】\n\n第二页","report_page",1,"report_pages",2,"report_page_marker","【第2/2页；请翻页继续查看】\n\n"));
    int count=s.requests.size();retired.builder.actions.get(-1).onClick(retired,-1);yes(s.requests.size()==count,"retired recent page sent request");
    AlertDialog.last.click(-3);yes(s.requests.get(s.requests.size()-1).optInt("page")==0,"recent previous page missing");ui.close();ui.accept(page.put("request_id",s.lastId));yes(!AlertDialog.last.showing,"Home retained recent body");break;
   }
   case "watch_result_expiry":{
    watch(ui,s,watchPayload(s).put("watch_has_results",true));AlertDialog.last.click(-3);AlertDialog.last.pick(2);
    ui.accept(new JSONObject("request_id",s.lastId,"ok",true,"action","watch_result","watch_result",true,"watch_result_ttl_ms",1,"snapshot_id","c".repeat(32),"report_text","只读正文","report_page",0,"report_pages",1,"report_page_marker",""));
    yes(AlertDialog.last.showing,"valid expiring result not visible");Runnable expiry=android.os.Handler.queued.get(android.os.Handler.queued.size()-1);int count=s.requests.size();expiry.run();yes(!AlertDialog.last.showing&&s.requests.size()==count,"result expiry retained body or queried history");
    watch(ui,s,watchPayload(s));AlertDialog newer=AlertDialog.last;expiry.run();yes(newer.showing,"old result expiry closed a new owner");break;
   }
   case "watch_bad_result":{
    for(String broken:new String[]{"snapshot","page","action","marker","ttl"}){
     watch(ui,s,watchPayload(s).put("watch_has_results",true));AlertDialog.last.click(-3);AlertDialog.last.pick(2);
     JSONObject page=new JSONObject("request_id",s.lastId,"ok",true,"action","watch_result","watch_result",true,"watch_result_ttl_ms",1000,"snapshot_id","c".repeat(32),"report_text","正文","report_page",0,"report_pages",1,"report_page_marker","");
     if(broken.equals("ttl"))page.put("watch_result_ttl_ms",true);if(broken.equals("snapshot"))page.put("snapshot_id",42);if(broken.equals("page"))page.put("report_page",true);if(broken.equals("action"))page.put("action","report");if(broken.equals("marker"))page.put("report_page_marker","bad");
     ui.accept(page);yes(!AlertDialog.last.showing,"malformed recent result accepted: "+broken);
    }break;
   }
   case "watch_invalid_unsupported":{
    master(ui,s);AlertDialog.last.click(-3);yes(AlertDialog.last.builder.labels.length==6,"old host offered subscription");
    JSONObject data=watchPayload(s);data.optJSONObject("report_watch").put("revision",true);watch(ui,s,data);yes(!AlertDialog.last.showing,"Boolean watch revision accepted");
    data=watchPayload(s);data.optJSONObject("report_watch").put("configured",false);watch(ui,s,data);yes(!AlertDialog.last.showing,"enabled without configuration accepted");break;
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
            'boolean supportsReportSources(){return true;}boolean archive=true,threadQueue=true,reportProgress=true;boolean supportsReportProgress(){return reportProgress;}boolean supportsThreadQueue(){return threadQueue;}boolean supportsReportArchive(){return archive;}void dotChatCancel(String id,long route){}'
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
        files.append(str(widgets.JAVA / 'ReportTextPage.java'))
        files.append(str(widgets.JAVA / 'ReportWatchDialog.java'))
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

    def test_calendar_periods_are_offered_only_by_a_compatible_host(self):
        self.scenario('calendar_report')

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

    def test_live_pages_use_frozen_snapshot_and_retire_old_navigation(self):
        self.scenario('live_pages')

    def test_full_copy_assembles_every_revision_bound_page_before_clipboard_write(self):
        self.scenario('archive_pages_copy')

    def test_home_route_revision_and_timeout_cancel_copy_without_partial_clipboard(self):
        self.scenario('copy_cancel_revision')

    def test_malformed_page_metadata_has_no_actions(self):
        self.scenario('bad_page')

    def test_complete_copy_capacity_and_whole_job_deadline_are_enforced(self):
        self.scenario('copy_bounds')

    def test_watch_freezes_source_revision_period_channel_and_waits_for_consent(self):
        self.scenario('watch_configure')

    def test_watch_pause_and_clear_use_only_confirmed_frozen_revision(self):
        self.scenario('watch_management')

    def test_watch_child_closes_with_parent_home_route_and_timeout(self):
        self.scenario('watch_stale')

    def test_watch_malformed_state_and_older_host_do_not_offer_actions(self):
        self.scenario('watch_invalid_unsupported')

    def test_recent_watch_pages_are_frozen_readonly_and_retired_on_home(self):
        self.scenario('watch_results')

    def test_recent_watch_results_require_typed_pages_and_expected_action(self):
        self.scenario('watch_bad_result')

    def test_watch_summary_needs_supported_channel_and_frozen_explicit_consent(self):
        self.scenario('watch_summary')

    def test_recent_body_expires_on_device_without_querying_or_closing_new_owner(self):
        self.scenario('watch_result_expiry')
