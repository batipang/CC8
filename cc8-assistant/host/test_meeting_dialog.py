"""Execute actual meeting UI ownership and consent with inert platform widgets."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_workflow_ui import stubs as workflow_stubs
from tools.java_toolchain import java_bin

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'


def stubs():
    value = workflow_stubs()
    value['android/os/SystemClock.java'] = (
        'package android.os;public class SystemClock{public static long now=1;public static long elapsedRealtime(){return now;}}'
    )
    value['android/widget/TextView.java'] = value['android/widget/TextView.java'].replace(
        'public void setTextSize',
        'public void setTextIsSelectable(boolean b){}public void setTextSize',
    )
    value['android/widget/TextView.java'] = value['android/widget/TextView.java'].replace(
        'public void setText(String s){',
        'public static int writes;public void setText(String s){writes++;',
    )
    value['android/widget/ScrollView.java'] = value['android/widget/ScrollView.java'].replace(
        'public void addView', 'public void scrollTo(int x,int y){}public void addView'
    )
    value['android/Manifest.java'] = value['android/Manifest.java'].replace(
        'CAMERA="camera"', 'CAMERA="camera",RECORD_AUDIO="microphone"'
    )
    value['org/json/JSONObject.java'] = value['org/json/JSONObject.java'].replace(
        'public int length()',
        'public boolean optBoolean(String k){return Boolean.TRUE.equals(opt(k));}'
        'public long optLong(String k){return optLong(k,0);}'
        'public long optLong(String k,long fallback){return opt(k) instanceof Number?((Number)opt(k)).longValue():fallback;}'
        'public int length()',
    )
    value['android/widget/EditText.java'] = value['android/widget/EditText.java'].replace(
        'public void setFilters', 'public void setMaxLines(int n){}public void setFilters'
    )
    value['android/widget/Button.java'] = value['android/widget/Button.java'].replace(
        'public Button(){super(null);}',
        'public boolean enabled=true;public Button(){super(null);}'
        'public Button(android.content.Context c){super(c);}public void setEnabled(boolean b){enabled=b;}'
        'public void performClick(){if(enabled&&click!=null)click.onClick(this);}',
    )
    value['android/widget/CheckBox.java'] = (
        'package android.widget;public class CheckBox extends Button{'
        'public boolean checked;public CheckBox(android.content.Context c){super(c);}'
        'public void setChecked(boolean b){checked=b;}public boolean isChecked(){return checked;}}'
    )
    value['android/widget/LinearLayout.java'] = value['android/widget/LinearLayout.java'].replace(
        'public static final int VERTICAL=1;',
        'public static final int VERTICAL=1;'
        'public static class LayoutParams{public static final int WRAP_CONTENT=-2;'
        'public LayoutParams(int w,int h,int weight){}}'
        'public void addView(android.view.View v,LayoutParams p){children.add(v);}',
    )
    value['android/app/AlertDialog.java'] = (
        value['android/app/AlertDialog.java']
        .replace(
            'public AlertDialog create()',
            'public AlertDialog show(){AlertDialog d=create();d.show();return d;}'
            'public AlertDialog create()',
        )
        .replace(
            'public void show(){', 'public boolean isShowing(){return showing;}public void show(){'
        )
    )
    value['android/content/ClipboardManager.java'] = (
        'package android.content;public class ClipboardManager{'
        'public static String last;public void setPrimaryClip(ClipData d){last=d.value;}}'
    )
    value['android/content/ClipData.java'] = (
        'package android.content;public class ClipData{public String value;'
        'public static ClipData newPlainText(String a,String b){ClipData d=new ClipData();d.value=b;return d;}}'
    )
    value[
        'com/happyhome/cc8assistant/VoiceSession.java'
    ] = r"""package com.happyhome.cc8assistant;import org.json.*;
class VoiceSession{long route=1;boolean foreground=true,supported=true,blocked,capturing;int leaves;
 String lastId;final java.util.List<JSONObject> requests=new java.util.ArrayList<>();
 long connectedRoute(){return route;}boolean currentRoute(long r){return foreground&&r==route;}
 boolean isConnected(){return foreground;}boolean supportsMeetings(){return supported;}
 boolean isMeetingCapturing(){return capturing;}boolean meetingMediaBlocked(){return blocked;}
 void leaveMeeting(long r){if(currentRoute(r))leaves++;}
 boolean meetingRequest(String id,JSONObject data,long r){if(!currentRoute(r))return false;lastId=id;
 requests.add((JSONObject)WorkflowModel.copy(data));return true;}}"""
    return value


HARNESS = r"""package com.happyhome.cc8assistant;
import android.app.*;import android.widget.*;import android.view.*;import android.content.*;import org.json.*;
public class MeetingDialogCheck{
 static final String ID="a".repeat(32),ROW="b".repeat(32),FILE=ID+"_r7.txt",HASH="c".repeat(64);
 static void need(boolean b,String why){if(!b)throw new AssertionError(why);}
 static JSONObject state(){return new JSONObject("available",true,"id",ID,"revision",7L,"state","ended","row_count",1,"pending_count",0,"position_samples",1600L);}
 static JSONObject last(VoiceSession s){return s.requests.get(s.requests.size()-1);}
 static JSONObject reply(VoiceSession s){return new JSONObject("event","meeting_result","request_id",s.lastId,"action",last(s).optString("action"),"ok",true,"state",state());}
 static JSONObject file(){return new JSONObject("id",FILE,"sha256",HASH,"format","txt","bytes",123L,"integrity_confirmed",true);}
 static String hash(String text){try{StringBuilder b=new StringBuilder();for(byte x:java.security.MessageDigest.getInstance("SHA-256").digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)))b.append(String.format("%02x",x&255));return b.toString();}catch(Exception e){throw new RuntimeException(e);}}
 static JSONObject analysis(String body){return new JSONObject("id","d".repeat(32),"kind","summarize","language","zh","source_revision",7L,"stale",false,"characters",body.codePointCount(0,body.length()),"result_sha256",hash(body));}
 static Button button(View v,String name){if(v instanceof Button&&((Button)v).getText().toString().equals(name))return (Button)v;
  if(v instanceof ScrollView)return button(((ScrollView)v).child,name);
  if(v instanceof LinearLayout)for(View child:((LinearLayout)v).children){Button b=button(child,name);if(b!=null)return b;}return null;}
 static AlertDialog open(MeetingDialog ui,VoiceSession s){ui.open();AlertDialog root=AlertDialog.last;ui.accept(reply(s),1);return root;}
 static void press(AlertDialog root,String label){Button b=button(root.builder.view,label);need(b!=null,"Missing control "+label);b.performClick();}
 static TextView body(View view){if(view instanceof ScrollView&&((ScrollView)view).child instanceof TextView)return (TextView)((ScrollView)view).child;
  if(view instanceof LinearLayout)for(View child:((LinearLayout)view).children){TextView found=body(child);if(found!=null)return found;}return null;}
 public static void main(String[] args){Activity a=new Activity();VoiceSession s=new VoiceSession();MeetingDialog ui=new MeetingDialog(a,s);
  AlertDialog root=open(ui,s);
  switch(args[0]){
   case "export_revision":{
    press(root,"导出文字");AlertDialog format=AlertDialog.last;int n=s.requests.size();
    need(n==1,"Opening format wrote a file");ui.accept(new JSONObject("event","meeting_update","state",state().put("revision",8L)),1);
    format.pick(0);JSONObject sent=last(s);need(sent.optString("action").equals("export")&&sent.optString("id").equals(ID)&&sent.optLong("revision")==7L,"Mutable status retargeted export");
    need(Boolean.TRUE.equals(sent.opt("confirmed"))&&sent.optString("format").equals("txt"),"Explicit format not preserved");
    ui.accept(reply(s).put("export",file()).put("local_path","/private/exports/meeting.txt"),1);
    AlertDialog saved=AlertDialog.last;need(saved.builder.message.contains("/private/exports/meeting.txt"),"Verified path absent");
    saved.click(-3);need(ClipboardManager.last.equals("/private/exports/meeting.txt"),"Copy did not use displayed path");break;
   }
   case "retired_format":{
    press(root,"导出文字");AlertDialog old=AlertDialog.last;ui.close();open(ui,s);int count=s.requests.size();
    old.builder.item.onClick(old,0);need(s.requests.size()==count,"Retired format exported another meeting");break;
   }
   case "delete_file":{
    press(root,"已导出文件");JSONObject original=file();ui.accept(reply(s).put("exports",new JSONObject("items",new JSONArray(original))),1);
    original.put("id","d".repeat(32)+"_r9.json").put("sha256","e".repeat(64));
    AlertDialog.last.pick(0);AlertDialog detail=AlertDialog.last;need(detail.builder.message.contains(FILE),"Mutable list retargeted selected file");
    detail.click(-1);AlertDialog confirm=AlertDialog.last;need(!last(s).optString("action").equals("delete_export"),"Opening confirmation deleted file");
    confirm.click(-1);JSONObject sent=last(s);need(sent.optString("export_id").equals(FILE)&&sent.optString("sha256").equals(HASH)&&Boolean.TRUE.equals(sent.opt("confirmed")),"Deletion not bound to selected receipt");
    int count=s.requests.size();confirm.builder.actions.get(-1).onClick(confirm,-1);need(s.requests.size()==count,"Retired delete repeated write");
    ui.accept(reply(s),1);need(last(s).optString("action").equals("exports"),"Successful delete did not refresh read-only list");break;
   }
   case "home_delete":{
    press(root,"已导出文件");ui.accept(reply(s).put("exports",new JSONObject("items",new JSONArray(file()))),1);
    AlertDialog.last.pick(0);AlertDialog.last.click(-1);AlertDialog confirm=AlertDialog.last;
    ui.close();open(ui,s);int count=s.requests.size();confirm.builder.actions.get(-1).onClick(confirm,-1);
    need(count==s.requests.size(),"Home/reopen allowed old file deletion");break;
   }
   case "ram_delete":{
    press(root,"删除会议");AlertDialog confirm=AlertDialog.last;ui.accept(new JSONObject("event","meeting_update","state",state().put("revision",8L)),1);
    confirm.click(-1);need(last(s).optString("action").equals("delete")&&last(s).optLong("revision")==7L,"RAM delete retargeted newer revision");break;
   }
   case "permission_once":{
    ui.close();root=open(ui,s);ui.accept(new JSONObject("event","meeting_update","state",new JSONObject("available",false)),1);
    a.permission=-1;int count=s.requests.size();press(root,"开始会议");need(a.requested==35&&count==s.requests.size(),"Permission grant started meeting");
    a.permission=0;need(count==s.requests.size(),"Grant auto-submitted");press(root,"开始会议");need(last(s).optString("action").equals("prepare"),"Start bypassed offer");
    ui.accept(reply(s).put("offer",new JSONObject("id","f".repeat(32),"mode","start")),1);
    AlertDialog confirm=AlertDialog.last;CheckBox consent=(CheckBox)((ScrollView)confirm.builder.view).child;
    count=s.requests.size();confirm.click(-1);need(count==s.requests.size(),"Unchecked consent authorized capture");
    consent.setChecked(true);confirm.click(-1);need(last(s).optString("action").equals("start")&&!last(s).optBoolean("save_audio"),"Confirmed capture altered audio retention");
    count=s.requests.size();confirm.getButton(-1).click.onClick(confirm.getButton(-1));need(count==s.requests.size(),"Confirmation reused");break;
   }
   case "stale_reply":{
    press(root,"已导出文件");String old=s.lastId;String oldAction=last(s).optString("action");ui.close();root=open(ui,s);
    int count=s.requests.size();AlertDialog before=AlertDialog.last;
    ui.accept(new JSONObject("event","meeting_result","request_id",old,"action",oldAction,"ok",true,"state",state().put("revision",999L),"exports",new JSONObject("items",new JSONArray(file()))),1);
    need(AlertDialog.last==before&&count==s.requests.size(),"Stale receipt opened another page");
    press(root,"导出文字");AlertDialog.last.pick(0);need(last(s).optLong("revision")==7L,"Stale receipt changed current state");break;
   }
   case "invalid_list":{
    press(root,"已导出文件");int count=s.requests.size();ui.accept(reply(s).put("exports",new JSONObject("items",new JSONArray(file().put("id","../outside")))),1);
    need(count==s.requests.size()&&Toast.last.contains("回执未确认"),"Malformed file became actionable");break;
   }
   case "stable_idle_render":{
    int writes=TextView.writes;
    for(int i=0;i<40;i++)ui.accept(new JSONObject("event","meeting_update","state",state()),1);
    need(TextView.writes==writes,"Unchanged polling repeatedly repainted accessible text");
    JSONObject running=state().put("analysis_job",new JSONObject("id","e".repeat(32),"state","running"));
    ui.accept(new JSONObject("event","meeting_update","state",running),1);
    need(TextView.writes>writes,"Changed document status was not rendered");writes=TextView.writes;
    for(int i=0;i<40;i++)ui.accept(new JSONObject("event","meeting_update","state",running),1);
    need(TextView.writes==writes,"Unchanged analysis status repeatedly repainted text");
    ui.accept(new JSONObject("event","meeting_update","state",running.put("position_samples",32000L)),1);
    need(TextView.writes>writes,"Changed meeting time was not rendered");break;
   }
   case "analysis_complete":case "analysis_tampered":case "analysis_deadline":{
    String full="😀完整结果";JSONObject meta=analysis(full);
    ui.accept(new JSONObject("event","meeting_update","state",state().put("analyses",new JSONArray(meta))),1);
    press(root,"查看分析");AlertDialog.last.pick(0);
    need(last(s).optString("action").equals("analysis_text"),"Analysis list did not request complete text");
    ui.accept(reply(s).put("page",analysis(full).put("offset",0).put("text","😀完").put("next_offset",2)),1);
    need(last(s).optInt("offset")==2,"Analysis cursor used UTF16 length");
    if(args[0].equals("analysis_deadline"))android.os.SystemClock.now=40000;
    String tail=args[0].equals("analysis_tampered")?"错结果":"整结果";
    ClipboardManager.last="keep";
    ui.accept(reply(s).put("page",analysis(full).put("offset",2).put("text",tail).put("next_offset",JSONObject.NULL)),1);
    if(!args[0].equals("analysis_complete")){
      need(ClipboardManager.last.equals("keep")&&Toast.last.contains(args[0].equals("analysis_deadline")?"完整结果未确认":"校验失败"),"Partial or tampered analysis shown as complete");break;
    }
    AlertDialog display=AlertDialog.last;need(body(display.builder.view).getText().toString().equals(full),"Analysis text lost Unicode");
    display.click(-3);need(ClipboardManager.last.equals(full),"Complete result copy changed content");
    ui.close();open(ui,s);ClipboardManager.last="keep";display.builder.actions.get(-3).onClick(display,-3);
    need(ClipboardManager.last.equals("keep"),"Retired analysis copied into reopened UI");break;
   }
   case "analysis_paging":{
    String full="中".repeat(4095)+"😀"+"后".repeat(4095);int length=full.codePointCount(0,full.length());
    ui.accept(new JSONObject("event","meeting_update","state",state().put("analyses",new JSONArray(analysis(full)))),1);
    press(root,"查看分析");AlertDialog.last.pick(0);
    for(int offset=0;offset<length;){
      int next=Math.min(offset+1024,length);String part=full.substring(full.offsetByCodePoints(0,offset),full.offsetByCodePoints(0,next));
      ui.accept(reply(s).put("page",analysis(full).put("offset",offset).put("text",part).put("next_offset",next<length?next:JSONObject.NULL)),1);offset=next;
    }
    AlertDialog display=AlertDialog.last;int count=s.requests.size();StringBuilder joined=new StringBuilder();
    for(int i=0;i<3;i++){
      String part=body(display.builder.view).getText().toString();need(!Character.isLowSurrogate(part.charAt(0))&&!Character.isHighSurrogate(part.charAt(part.length()-1)),"Visible page split surrogate pair");joined.append(part);press(display,"下一页");
    }
    need(joined.toString().equals(full)&&count==s.requests.size(),"Local paging lost text or submitted new work");
    display.click(-3);need(ClipboardManager.last.equals(full),"Paged copy copied only visible page");break;
   }
   case "translation_revision":{
    press(root,"翻译文字");AlertDialog choose=AlertDialog.last;
    ui.accept(new JSONObject("event","meeting_update","state",state().put("revision",8L)),1);
    choose.pick(1);need(last(s).optString("action").equals("translate")&&last(s).optString("language").equals("en")&&last(s).optLong("revision")==7L,"Translation retargeted mutable revision");break;
   }
   case "cancel_analysis":{
    ui.accept(new JSONObject("event","meeting_update","state",state().put("analysis_job",new JSONObject("id","e".repeat(32),"state","running"))),1);
    press(root,"取消文字处理");need(last(s).optString("action").equals("cancel_analysis")&&last(s).optString("analysis_id").equals("e".repeat(32)),"Cancel retargeted another job");break;
   }
   case "failed_edit":{
    press(root,"查看／校对文字");ui.accept(reply(s).put("page",new JSONObject("id",ID,"revision",7L,"rows",new JSONArray(new JSONObject("id",ROW,"start_sample",0L,"state","recognized")),"next_offset",JSONObject.NULL)),1);
    AlertDialog.last.pick(0);ui.accept(reply(s).put("page",new JSONObject("id",ID,"revision",7L,"offset",0,"row",new JSONObject("id",ROW,"state","recognized"),"text","original 😀","next_offset",JSONObject.NULL)),1);
    AlertDialog edit=AlertDialog.last;((EditText)edit.builder.view).setText("corrected 😀");edit.click(-1);
    ui.accept(reply(s).put("ok",false).put("message","revision changed"),1);
    need(edit.showing&&((EditText)edit.builder.view).getText().toString().equals("corrected 😀"),"Failed correction lost draft");
    ui.close();open(ui,s);int count=s.requests.size();edit.getButton(-1).click.onClick(edit.getButton(-1));need(count==s.requests.size(),"Retired editor submitted into new meeting");break;
   }
  }
  System.out.println("PASS "+args[0]);
 }
}"""


class MeetingDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        files = []
        for name, text in {
            **stubs(),
            'com/happyhome/cc8assistant/MeetingDialogCheck.java': HARNESS,
        }.items():
            path = cls.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            files.append(str(path))
        cls.java = java_bin()
        result = subprocess.run(
            [
                str(cls.java / 'javac'),
                '-encoding',
                'UTF-8',
                '-d',
                str(cls.folder),
                *files,
                str(JAVA / 'MeetingDialog.java'),
                str(JAVA / 'MeetingAnalysisDialog.java'),
                str(JAVA / 'WorkflowModel.java'),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr)

    def test_actual_dialog_ownership_receipts_and_consent(self):
        for name in (
            'export_revision',
            'retired_format',
            'delete_file',
            'home_delete',
            'ram_delete',
            'permission_once',
            'stale_reply',
            'invalid_list',
            'failed_edit',
            'stable_idle_render',
            'analysis_complete',
            'analysis_tampered',
            'analysis_deadline',
            'analysis_paging',
            'translation_revision',
            'cancel_analysis',
        ):
            with self.subTest(scenario=name):
                result = subprocess.run(
                    [
                        str(self.java / 'java'),
                        '-cp',
                        str(self.folder),
                        'com.happyhome.cc8assistant.MeetingDialogCheck',
                        name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
