"""Compile the actual draft dialog and session send boundary against inert widgets."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from test_workspace_actions import FIXTURES
from test_interrupted_task_notice import method
from tools.java_toolchain import java_bin

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'

HARNESS = r"""
package com.happyhome.cc8assistant;
import org.json.*;import android.app.*;import android.widget.*;import java.util.*;import java.io.*;
class Wire {
 static JSONObject event(String event){return new JSONObject("event",event);}
 static void put(JSONObject j,String name,Object value){j.values.put(name,value);}
}
class VoiceSession {
 final Object routeLock=new Object();boolean foreground=true;long routeEpoch=1;
 JSONObject speechCorrection;int speechCorrectionRevision=-1;
 Link activeLink=new Link();int sends;boolean fail;
 class Link {boolean ready=true,closed;JSONObject last;
  void event(JSONObject j)throws IOException{if(fail)throw new IOException("fixture");sends++;last=j;}
  void close(){closed=true;}
 }
 boolean incoming(JSONObject state){return acceptSpeechCorrection(state);}
 METHODS
}
public class CorrectionCheck {
 static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
 static JSONObject draft(int revision){return new JSONObject("revision",revision,"pending",true,"draft_id","a".repeat(32),"original","只读查询沧州天气","proposed","只读查询常州天气");}
 public static void main(String[] args){
  Activity owner=new Activity();VoiceSession session=new VoiceSession();
  SpeechCorrectionDialog dialogs=new SpeechCorrectionDialog(owner,session);JSONObject state=draft(1);
  yes(session.incoming(state),"valid receipt rejected");dialogs.accept(state,1);AlertDialog shown=AlertDialog.last;
  yes(shown!=null&&!shown.cancelable,"pending draft silently dismissible");
  String text=((TextView)((ScrollView)shown.builder.view).child).getText().toString();
  yes(text.contains("只读查询常州天气")&&text.contains("只读查询沧州天气")&&text.contains("不会撤销"),"real old/new inputs or effects warning absent");
  switch(args[0]){
   case "confirm":case "cancel":{
    if(args[0].equals("confirm")){shown.positive();shown.positive();}else{shown.negative();shown.negative();}
    yes(session.sends==1,"repeated tap re-enqueued submission");
    yes(session.activeLink.last.optString("draft_id").equals("a".repeat(32)),"sent device replacement text instead of draft identity");
    yes(session.activeLink.last.optString("action").equals(args[0]),"wrong explicit action");
    yes(session.activeLink.last.values.size()==4,"unbounded or additional body fields");break;
   }
   case "route":session.routeEpoch++;shown.positive();yes(session.sends==0&&shown.dismissed,"old dialog crossed route");break;
   case "background":session.foreground=false;shown.positive();yes(session.sends==0&&shown.dismissed,"background draft submitted");break;
   case "replaced":{
    JSONObject next=draft(2);session.incoming(next);dialogs.accept(next,1);AlertDialog newer=AlertDialog.last;
    shown.positive();yes(session.sends==0,"old draft tap submitted newer context");
    newer.positive();yes(session.sends==1,"new current dialog lost ownership");break;
   }
   case "closed":{
    JSONObject clear=new JSONObject("revision",2,"pending",false);session.incoming(clear);dialogs.accept(clear,1);
    yes(shown.dismissed,"withdrawal did not close UI");
    yes(!session.incoming(draft(1)),"old revision reopened draft");shown.positive();yes(session.sends==0,"withdrawn draft submitted");break;
   }
   case "write_failure":session.fail=true;shown.positive();shown.positive();yes(session.sends==0&&session.activeLink.closed&&shown.dismissed,"ambiguous send retried or remained enabled");break;
   case "invalid":{
    for(Object revision:new Object[]{true,-1,1.0,"1"}){
     JSONObject bad=draft(3);bad.values.put("revision",revision);yes(!session.incoming(bad),"invalid revision accepted");
    }
    JSONObject bad=draft(3);bad.values.put("proposed","x".repeat(1025));yes(!session.incoming(bad),"oversized body accepted");break;
   }
   default:throw new AssertionError("unknown case");
  }
 }
}
"""


class CorrectionDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.folder = Path(cls.temporary.name)
        session = (JAVA / 'VoiceSession.java').read_text()
        methods = '\n'.join(
            method(session, signature)
            for signature in (
                'boolean currentRoute(',
                'private boolean acceptSpeechCorrection(',
                'boolean currentSpeechCorrection(',
                'boolean speechCorrectionAction(',
                'private boolean sendWorkspace(',
            )
        )
        fixtures = dict(FIXTURES)
        fixtures['org/json/JSONObject.java'] = fixtures['org/json/JSONObject.java'].replace(
            'public Object remove(String k)',
            'public Object opt(String k){return values.get(k);}public Object remove(String k)',
        )
        fixtures['android/app/AlertDialog.java'] = fixtures['android/app/AlertDialog.java'].replace(
            'public void setOnShowListener',
            'public boolean cancelable=true;public void setCancelable(boolean value){cancelable=value;}public void setOnShowListener',
        )
        sources = []
        for name, content in {
            **fixtures,
            'CorrectionCheck.java': HARNESS.replace('METHODS', methods),
            'SpeechCorrectionDialog.java': (JAVA / 'SpeechCorrectionDialog.java').read_text(),
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
            raise AssertionError(compiled.stderr)

    def test_real_dialog_and_send_ownership(self):
        for case in (
            'confirm',
            'cancel',
            'route',
            'background',
            'replaced',
            'closed',
            'write_failure',
            'invalid',
        ):
            with self.subTest(case=case):
                result = subprocess.run(
                    [
                        str(self.java / 'java'),
                        '-cp',
                        str(self.folder),
                        'com.happyhome.cc8assistant.CorrectionCheck',
                        case,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
