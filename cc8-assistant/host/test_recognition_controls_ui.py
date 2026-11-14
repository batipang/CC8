"""Compile actual dialogs/snapshots against inert widgets; no Android I/O or permission."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'

STUBS = {
    'com/happyhome/cc8assistant/LargeDialogBuilder.java': 'package com.happyhome.cc8assistant;public class LargeDialogBuilder extends android.app.AlertDialog.Builder{public LargeDialogBuilder(android.app.Activity a){super(a);}}',
    'android/content/Context.java': 'package android.content;public class Context{}',
    'android/app/Activity.java': 'package android.app;public class Activity extends android.content.Context{}',
    'android/content/DialogInterface.java': """package android.content;public interface DialogInterface{
 interface OnClickListener{void onClick(DialogInterface d,int n);}interface OnDismissListener{void onDismiss(DialogInterface d);}
 interface OnCancelListener{void onCancel(DialogInterface d);}interface OnShowListener{void onShow(DialogInterface d);}}""",
    'android/view/View.java': """package android.view;public class View{
 static int next=1;int id;public static int generateViewId(){return next++;}public void setId(int i){id=i;}
 public int getId(){return id;}public interface OnClickListener{void onClick(View v);}public void setPadding(int a,int b,int c,int d){}
 public void setFocusableInTouchMode(boolean b){}public boolean requestFocus(){return true;}}""",
    'android/view/Window.java': 'package android.view;public class Window{public void clearFlags(int i){}public void setSoftInputMode(int i){}}',
    'android/view/WindowManager.java': """package android.view;public class WindowManager{public static class LayoutParams{
 public static final int FLAG_FULLSCREEN=1,SOFT_INPUT_STATE_HIDDEN=2,SOFT_INPUT_ADJUST_RESIZE=4;}}""",
    'android/widget/TextView.java': """package android.widget;public class TextView extends android.view.View{
 public String value="";public TextView(android.content.Context c){}public void setText(String s){value=s;}
 public CharSequence getText(){return value;}public void setTextSize(int i){}public void setHint(String s){}
 public void setSingleLine(boolean b){}public void setMinLines(int i){}public void setEnabled(boolean b){}}""",
    'android/widget/EditText.java': """package android.widget;public class EditText extends TextView{
 public EditText(android.content.Context c){super(c);}public void setFilters(android.text.InputFilter[] f){}}""",
    'android/widget/Button.java': """package android.widget;public class Button extends TextView{
 public android.view.View.OnClickListener click;public Button(){super(null);}public void setOnClickListener(android.view.View.OnClickListener l){click=l;}}""",
    'android/widget/RadioButton.java': 'package android.widget;public class RadioButton extends Button{public RadioButton(android.content.Context c){}}',
    'android/widget/LinearLayout.java': """package android.widget;public class LinearLayout extends android.view.View{
 public static final int VERTICAL=1;public final java.util.List<android.view.View> children=new java.util.ArrayList<>();
 public LinearLayout(android.content.Context c){}public void setOrientation(int i){}public void addView(android.view.View v){children.add(v);}}""",
    'android/widget/RadioGroup.java': """package android.widget;public class RadioGroup extends LinearLayout{
 int selected;public RadioGroup(android.content.Context c){super(c);}public void check(int i){selected=i;}public int getCheckedRadioButtonId(){return selected;}}""",
    'android/widget/ScrollView.java': """package android.widget;public class ScrollView extends android.view.View{
 public android.view.View child;public ScrollView(android.content.Context c){}public void addView(android.view.View v){child=v;}}""",
    'android/widget/Toast.java': """package android.widget;public class Toast{public static final int LENGTH_LONG=1;
 public static String last;public static Toast makeText(android.content.Context c,String s,int i){last=s;return new Toast();}public void show(){}}""",
    'android/text/InputFilter.java': 'package android.text;public interface InputFilter{class LengthFilter implements InputFilter{public LengthFilter(int i){}}}',
    'android/text/TextUtils.java': """package android.text;public class TextUtils{
 public static String join(CharSequence d,Iterable<?> values){java.util.StringJoiner j=new java.util.StringJoiner(d.toString());for(Object v:values)j.add(v.toString());return j.toString();}}""",
    'android/os/Looper.java': 'package android.os;public class Looper{public static Looper getMainLooper(){return new Looper();}}',
    'android/os/Handler.java': """package android.os;public class Handler{
 public static final java.util.List<Runnable> queued=new java.util.ArrayList<>();public Handler(Looper l){}
 public void postDelayed(Runnable r,long n){queued.add(r);}public void removeCallbacks(Runnable r){queued.remove(r);}}""",
    'android/app/AlertDialog.java': """package android.app;import android.content.*;import android.widget.*;
 public class AlertDialog implements DialogInterface{
 public static final int BUTTON_POSITIVE=-1,BUTTON_NEGATIVE=-2,BUTTON_NEUTRAL=-3;public static AlertDialog last;
 public Builder builder;public boolean showing;OnDismissListener dismissed;OnShowListener shown;
 final java.util.Map<Integer,Button> buttons=new java.util.HashMap<>();
 AlertDialog(Builder b){builder=b;}public void setOnCancelListener(OnCancelListener l){}
 public void setOnDismissListener(OnDismissListener l){dismissed=l;}public void setOnShowListener(OnShowListener l){shown=l;}
 public void show(){showing=true;last=this;if(shown!=null)shown.onShow(this);}public void dismiss(){if(!showing)return;showing=false;if(dismissed!=null)dismissed.onDismiss(this);}
 public android.view.Window getWindow(){return new android.view.Window();}
 public Button getButton(int n){return buttons.computeIfAbsent(n,k->new Button());}
 public void click(int n){Button b=getButton(n);if(b.click!=null){b.click.onClick(b);return;}OnClickListener l=builder.actions.get(n);if(l!=null)l.onClick(this,n);dismiss();}
 public void pick(int n){builder.item.onClick(this,n);dismiss();}
 public static class Builder{public android.view.View view;public String[] labels;public OnClickListener item;
 final java.util.Map<Integer,OnClickListener> actions=new java.util.HashMap<>();public Builder(Context c){}
 public Builder setTitle(String s){return this;}public Builder setMessage(String s){return this;}public Builder setView(android.view.View v){view=v;return this;}
 public Builder setItems(String[] values,OnClickListener l){labels=values;item=l;return this;}
 public Builder setPositiveButton(String s,OnClickListener l){actions.put(-1,l);return this;}
 public Builder setNegativeButton(String s,OnClickListener l){actions.put(-2,l);return this;}
 public Builder setNeutralButton(String s,OnClickListener l){actions.put(-3,l);return this;}
 public AlertDialog create(){return new AlertDialog(this);}}
 }""",
    'org/json/JSONObject.java': """package org.json;public class JSONObject{
 public final java.util.Map<String,Object> data=new java.util.LinkedHashMap<>();public JSONObject(Object...args){for(int i=0;i<args.length;i+=2)data.put((String)args[i],args[i+1]);}
 public JSONObject put(String k,Object v){data.put(k,v);return this;}public Object opt(String k){return data.get(k);}
 public String optString(String k){Object v=opt(k);return v==null?"":v.toString();}public String optString(String k,String d){return opt(k)==null?d:optString(k);}
 public int optInt(String k){return optInt(k,0);}public int optInt(String k,int d){return opt(k) instanceof Integer?(Integer)opt(k):d;}
 public JSONObject optJSONObject(String k){return opt(k) instanceof JSONObject?(JSONObject)opt(k):null;}
 public JSONArray optJSONArray(String k){return opt(k) instanceof JSONArray?(JSONArray)opt(k):null;}
 }""",
    'org/json/JSONArray.java': """package org.json;public class JSONArray{
 final java.util.List<Object> rows=new java.util.ArrayList<>();public JSONArray(Object...args){java.util.Collections.addAll(rows,args);}
 public JSONArray put(Object v){rows.add(v);return this;}public int length(){return rows.size();}public Object opt(int i){return i>=0&&i<rows.size()?rows.get(i):null;}
 public JSONObject optJSONObject(int i){return opt(i) instanceof JSONObject?(JSONObject)opt(i):null;}}""",
    'com/happyhome/cc8assistant/Wire.java': """package com.happyhome.cc8assistant;import org.json.*;
 class Wire{static void put(JSONObject o,String k,Object v){o.put(k,v);}}""",
    'com/happyhome/cc8assistant/VoiceSession.java': """package com.happyhome.cc8assistant;import org.json.*;
 class VoiceSession{long route=1;boolean foreground=true;JSONObject state=new JSONObject("revision",0,"environment_profiles_supported",true,"environment_profile","normal","recognition_language_supported",true,"recognition_language","auto");
 final java.util.List<JSONObject> requests=new java.util.ArrayList<>();String lastId;int settings;
 long connectedRoute(){return route;}boolean currentRoute(long r){return foreground&&route==r;}boolean isConnected(){return foreground;}
 boolean supportsVocabulary(){return true;}JSONObject voicePreferences(long r){return currentRoute(r)?state:null;}
 boolean setEnvironmentProfile(String p,int rev,long r){settings++;return currentRoute(r);}
 boolean setRecognitionLanguage(String p,int rev,long r){settings++;return currentRoute(r);}
 boolean vocabularyRequest(String id,JSONObject d,long r){if(!currentRoute(r))return false;lastId=id;requests.add(d);return true;}}
 """,
}

HARNESS = r"""package com.happyhome.cc8assistant;import org.json.*;import android.app.*;import android.widget.*;
public class RecognitionControlsCheck{
 static void yes(boolean v,String why){if(!v)throw new AssertionError(why);}
 static JSONObject row(){return new JSONObject("term","常州","aliases",new JSONArray("沧州"),"source","device","updated_at","2026-11-10T01:00:00+08:00");}
 static JSONObject result(String id){return new JSONObject("request_id",id,"source_id","d".repeat(32),"ok",true,"prepared",false,"state",new JSONObject("schema",1,"revision",1,"items",new JSONArray(row())));}
 public static void main(String[] args){VoiceSession s=new VoiceSession();Activity a=new Activity();
 switch(args[0]){
 case "snapshot":{
  JSONObject r=row(),state=new JSONObject("schema",1,"revision",1,"items",new JSONArray(r));
  VocabularySnapshot snap=VocabularySnapshot.parse(state);yes(snap!=null&&snap.items.get(0).term.equals("常州"),"valid entry rejected");
  r.put("term","另一个");yes(snap.items.get(0).term.equals("常州"),"mutable JSON retargeted word");
  try{snap.items.get(0).aliases.add("external");throw new AssertionError("mutable aliases");}catch(UnsupportedOperationException expected){}
  state.put("revision",true);yes(VocabularySnapshot.parse(state)==null,"Boolean revision accepted");
  for(String value:new String[]{"", "不是这个","a,b","a，b","a\nb","a\tb","e\u0301","a\u0085b","\u3000name",Character.toString((char)0xd800)})yes(!VocabularySnapshot.word(value),"invalid word accepted");
  yes(VocabularySnapshot.word("C++")&&VocabularySnapshot.word("Bumblebee"),"proper spelling rejected");break;
 }
 case "stale_list":{
  VocabularyDialog ui=new VocabularyDialog(a,s);ui.open();String id=s.lastId;ui.accept(result(id),1);AlertDialog old=AlertDialog.last;
  ui.open();AlertDialog waiting=AlertDialog.last;int count=s.requests.size();old.builder.item.onClick(old,0);
  yes(AlertDialog.last==waiting&&waiting.showing&&s.requests.size()==count,"retired list retargeted new wait");
  ui.accept(result(id),1);yes(AlertDialog.last==waiting&&waiting.showing,"old receipt closed new wait");
  Runnable late=android.os.Handler.queued.get(android.os.Handler.queued.size()-1);ui.close();ui.open();AlertDialog next=AlertDialog.last;late.run();
  yes(next.showing&&AlertDialog.last==next,"old timeout closed new window");break;
 }
 case "dismiss":{
  VocabularyDialog ui=new VocabularyDialog(a,s);ui.open();ui.accept(result(s.lastId),1);AlertDialog old=AlertDialog.last;old.click(-2);
  int count=s.requests.size();old.builder.item.onClick(old,0);yes(s.requests.size()==count&&!old.showing,"dismissed list remained actionable");break;
 }
 case "prepare":{
  VocabularyDialog ui=new VocabularyDialog(a,s);ui.open();ui.accept(result(s.lastId),1);AlertDialog.last.pick(0);AlertDialog.last.pick(0);
  JSONObject sent=s.requests.get(s.requests.size()-1);yes("prepare".equals(sent.optString("action"))&&"常州".equals(sent.optString("term"))&&"沧州".equals(sent.optString("alias"))&&"d".repeat(32).equals(sent.optString("source_id")),"literal pair/source not preserved");
  int count=s.requests.size();ui.accept(new JSONObject("request_id",s.lastId,"ok",true,"prepared",true),1);
  yes(s.requests.size()==count&&!AlertDialog.last.showing,"preparation automatically submitted or left wait open");break;
 }
 case "suggestions":{
  VocabularyDialog ui=new VocabularyDialog(a,s);ui.open();
  JSONObject good=new JSONObject("term","常州","alias","沧州");
  JSONObject invalid=new JSONObject("term","常州","alias","其他");
  JSONObject receipt=result(s.lastId).put("suggestions",new JSONArray(good,good,invalid));
  ui.accept(receipt,1);AlertDialog list=AlertDialog.last;good.put("term","被修改");list.pick(0);
  JSONObject sent=s.requests.get(s.requests.size()-1);
  yes(s.requests.size()==2&&"prepare".equals(sent.optString("action"))&&"常州".equals(sent.optString("term"))&&"沧州".equals(sent.optString("alias"))&&"d".repeat(32).equals(sent.optString("source_id")),"suggestion retargeted or submitted instead of prepared");
  list.builder.item.onClick(list,0);yes(s.requests.size()==2,"retired suggestion prepared again");break;
 }
 case "handover":{
  VocabularyDialog ui=new VocabularyDialog(a,s);ui.open();String old=s.lastId;s.route=2;ui.close();ui.open();AlertDialog waiting=AlertDialog.last;
  ui.accept(result(old),1);yes(AlertDialog.last==waiting&&waiting.showing,"old route receipt replaced current UI");break;
 }
 case "setting_owners":{
  RecognitionEnvironmentDialog ui=new RecognitionEnvironmentDialog(a,s);ui.open();AlertDialog old=AlertDialog.last;ui.open();AlertDialog newer=AlertDialog.last;
  old.getButton(-1).click.onClick(null);yes(s.settings==0&&newer.showing,"old environment editor applied");
  s.state=new JSONObject("revision",1,"environment_profiles_supported",true,"environment_profile","quiet");
  newer.getButton(-1).click.onClick(null);yes(s.settings==0,"stale preference snapshot applied");
  ui.open();AlertDialog current=AlertDialog.last;current.click(-1);yes(s.settings==1,"current environment editor could not apply");
  RecognitionLanguageDialog language=new RecognitionLanguageDialog(a,s);s.state.put("recognition_language_supported",true).put("recognition_language","auto");language.open();old=AlertDialog.last;old.click(-2);old.getButton(-1).click.onClick(null);yes(s.settings==1,"dismissed language editor applied");break;
 }
 default:throw new AssertionError("unknown case");
 }
 }
}"""


class RecognitionControlsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        files = []
        for name, source in {
            **STUBS,
            'com/happyhome/cc8assistant/RecognitionControlsCheck.java': HARNESS,
        }.items():
            path = cls.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
            files.append(str(path))
        files.extend(
            str(JAVA / name)
            for name in (
                'PhotoFollowupWindow.java',
                'VocabularySnapshot.java',
                'VocabularyDialog.java',
                'RecognitionEnvironmentDialog.java',
                'RecognitionLanguageDialog.java',
            )
        )
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
                'com.happyhome.cc8assistant.RecognitionControlsCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshot_is_bounded_typed_and_immutable(self):
        self.scenario('snapshot')

    def test_stale_list_receipt_and_timeout_cannot_replace_current_window(self):
        self.scenario('stale_list')

    def test_dismissed_list_cannot_prepare_or_open_an_editor(self):
        self.scenario('dismiss')

    def test_selected_pair_only_prepares_without_confirming(self):
        self.scenario('prepare')

    def test_current_suggestion_only_prepares_the_bound_pair(self):
        self.scenario('suggestions')

    def test_old_route_receipt_cannot_close_new_route_wait(self):
        self.scenario('handover')

    def test_environment_and_language_editors_reject_retired_owners(self):
        self.scenario('setting_owners')
