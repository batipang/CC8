"""Actual native store, receive boundary, Activity queue and dialog privacy registry."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.java_toolchain import java_bin
from test_interrupted_task_notice import method
import test_identity_runtime as host_fixture

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'


def expression(value):
    if isinstance(value, dict):
        return (
            'new JSONObject('
            + ','.join(expression(x) for pair in value.items() for x in pair)
            + ')'
        )
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if value is None:
        return 'null'
    return str(value)


class IdentityAndroidTests(unittest.TestCase):
    def test_real_android_boundaries_roundtrip_and_old_ui_cannot_reappear(self):
        fixture = host_fixture.IdentityRuntimeTests()
        b, channel = fixture.session(saved_guest=True)
        self.addCleanup(fixture.doCleanups)
        fixture.ready(b)
        packet = channel.events[-1]
        ui_method = method(
            (JAVA / 'MainActivity.java').read_text(), 'private void postIdentityUi(Runnable action)'
        )
        receive = method(
            (JAVA / 'VoiceSession.java').read_text(),
            'private boolean handleIdentityEvent(JSONObject j, long route, Link source)',
        )
        connected = method(
            (JAVA / 'VoiceSession.java').read_text(), 'private boolean identityConnected()'
        )
        sources = {
            'org/json/JSONObject.java': """package org.json;import java.util.*;public class JSONObject {
 public final Map<String,Object> data=new HashMap<>();public JSONObject(Object...p){for(int i=0;i<p.length;i+=2)data.put((String)p[i],p[i+1]);}
 public Object opt(String k){return data.get(k);}public int length(){return data.size();}
 public String optString(String k){Object v=opt(k);return v instanceof String?(String)v:"";}
 public String optString(String k,String fallback){Object v=opt(k);return v instanceof String?(String)v:fallback;}}""",
            'android/content/SharedPreferences.java': """package android.content;import java.util.Map;public interface SharedPreferences {
 Map<String,?> getAll();Editor edit();interface Editor{Editor putInt(String k,int v);Editor putLong(String k,long v);Editor putString(String k,String v);Editor putBoolean(String k,boolean v);boolean commit();}}""",
            'android/content/Context.java': """package android.content;public class Context {
 public static final int MODE_PRIVATE=0;public SharedPreferences prefs;
 public Context getApplicationContext(){return this;}public SharedPreferences getSharedPreferences(String name,int mode){return prefs;}}""",
            'android/view/View.java': """package android.view;import java.util.*;public class View {
 public static final int GONE=8;public int visibility;public interface OnAttachStateChangeListener{void onViewAttachedToWindow(View v);void onViewDetachedFromWindow(View v);}
 final List<OnAttachStateChangeListener> listeners=new ArrayList<>();public void setVisibility(int v){visibility=v;}
 public void addOnAttachStateChangeListener(OnAttachStateChangeListener l){listeners.add(l);}public void removeOnAttachStateChangeListener(OnAttachStateChangeListener l){listeners.remove(l);}
 public void attach(){for(OnAttachStateChangeListener l:new ArrayList<>(listeners))l.onViewAttachedToWindow(this);}}""",
            'android/widget/TextView.java': """package android.widget;public class TextView extends android.view.View{public float size;public void setTextSize(float v){size=v;}}""",
            'android/R.java': """package android;public class R{public static class id{public static int message=1;}}""",
            'android/app/AlertDialog.java': """package android.app;import android.content.Context;import android.view.View;public class AlertDialog {
 public boolean dismissed;public static class Window{final View decor=new View();public View getDecorView(){return decor;}}
 final Window window=new Window();final android.widget.TextView message=new android.widget.TextView();public Window getWindow(){return window;}
 @SuppressWarnings("unchecked") public <T extends View>T findViewById(int id){return(T)message;}public void dismiss(){dismissed=true;}
 public static class Builder{public Builder(Context context){}public AlertDialog create(){return new AlertDialog();}}}""",
        }
        program = """package com.happyhome.cc8assistant;
import org.json.*;import android.content.*;import android.app.*;import java.util.*;import java.io.*;
public class NativeIdentityFlow {
 static void yes(boolean v,String m){if(!v)throw new AssertionError(m);}
 static class Prefs implements SharedPreferences {
  Map<String,Object> values=new HashMap<>();boolean fail;
  public Map<String,?>getAll(){return new HashMap<>(values);}public Editor edit(){return new Edit();}
  class Edit implements Editor{Map<String,Object> next=new HashMap<>(values);
   public Editor putInt(String k,int v){next.put(k,v);return this;}public Editor putLong(String k,long v){next.put(k,v);return this;}
   public Editor putString(String k,String v){next.put(k,v);return this;}public Editor putBoolean(String k,boolean v){next.put(k,v);return this;}
   public boolean commit(){if(fail)return false;values=next;return true;}}
 }
 static class UiSession {final IdentityState state;UiSession(IdentityState s){state=s;}long identityEpoch(){return state.epoch();}boolean currentIdentity(long e){return state.current(e);}}
 static class Ui {UiSession session;final List<Runnable> queue=new ArrayList<>();String shown="";
 void runOnUiThread(Runnable r){queue.add(r);}UI_METHOD
 void drain(){for(Runnable r:new ArrayList<>(queue))r.run();queue.clear();}}
 static class Gate extends Context implements LargeDialogBuilder.OwnerAccess {
 final IdentityState state;Gate(IdentityState s){state=s;}public long dialogEpoch(){return state.epoch();}
 public boolean ownerDialogAllowed(long e){return state.owner()&&state.current(e);}}
 static class Receiver {
 String identityRequestId="";final IdentityState identity;Receiver(IdentityState s){identity=s;}boolean identityHello=true;
 Object sourcesPresentation,speechCorrection;int stops,visions,outputs;
 static class Followup{void clear(){}}final Followup photoFollowup=new Followup();
 static class Listener{int resets;void identityChanged(boolean stop){resets++;}void state(String phase,String message){}}
 final Listener listener=new Listener();
 void stopMicrophone(){stops++;}void cancelVision(){visions++;}void cancelPlayback(){outputs++;}
 class Link{JSONObject ack;void event(JSONObject value){yes(identity.ready(),"ACK before persistence");ack=value;}}
 RECEIVE_METHOD
 CONNECTED_METHOD
 }
 static class Wire {static JSONObject event(String name){return new JSONObject("event",name);}static void put(JSONObject o,String k,Object v){o.data.put(k,v);}}
 public static void main(String[] args)throws Exception {
  Context context=new Context();Prefs disk=new Prefs();context.prefs=disk;
  IdentityPreferences adapter=new IdentityPreferences(context);
  IdentityState s=new IdentityState(adapter);s.load();s.route(9);
  Receiver receiver=new Receiver(s);Receiver.Link link=receiver.new Link();
  yes(receiver.handleIdentityEvent(AUTH_PACKET,9,link),"actual host packet not handled");
  yes(s.ready()&&!s.owner()&&!receiver.identityConnected(),"state ACK auto-enabled dialogue/capture");
  yes(Boolean.TRUE.equals(disk.values.get("guest_requested")),"guest ACK preceded Android commit");
  receiver.identityHello=true;int resets=receiver.listener.resets;
  yes(receiver.handleIdentityEvent(AUTH_PACKET,9,link),"duplicate packet not acknowledged");
  yes(receiver.identityConnected()&&receiver.listener.resets==resets,"duplicate state disconnected the UI or cleared it");
  int stops=receiver.stops;
  yes(receiver.handleIdentityEvent(new JSONObject("event","identity_result","request_id","old","ok",false),9,link),"old result unhandled");
  yes(receiver.stops==stops&&receiver.identityConnected(),"late mode error stopped the new conversation");
  IdentityState reboot=new IdentityState(adapter);reboot.load();reboot.route(10);
  yes(reboot.guestRequested()&&!reboot.acceptLegacy(10),"Android restart leaked owner");
  System.out.println(link.ack.optString("token")+"|"+link.ack.optString("mode")+"|"+link.ack.opt("revision"));
  // Queue the actual Activity callback before a privacy transition.
  Ui ui=new Ui();ui.session=new UiSession(s);ui.postIdentityUi(()->ui.shown="OLD_PRIVATE");
  long guestEpoch=s.epoch();s.route(11);ui.postIdentityUi(()->ui.shown="PUBLIC_PENDING");ui.drain();
  yes(ui.shown.equals("PUBLIC_PENDING")&&!s.current(guestEpoch),"stale Activity callback survived");
  // Restore explicitly, create a private dialog, then invalidate that scope.
  JSONObject owner=AUTH_PACKET;JSONObject os=(JSONObject)owner.opt("state");
  os.data.put("mode","owner");os.data.put("revision",2);owner.data.put("identity_mode","owner");owner.data.put("identity_revision",2);owner.data.put("authority","owner_desktop");
  yes(s.accept(owner,11),"owner restore fixture failed");
  Gate gate=new Gate(s);AlertDialog visible=new LargeDialogBuilder(gate).create();visible.getWindow().getDecorView().attach();
  yes(!visible.dismissed,"owner dialog was blocked");
  s.requestGuest();LargeDialogBuilder.dismissAll(gate);yes(visible.dismissed,"private dialog survived switch");
  AlertDialog stale=new LargeDialogBuilder(gate).create();stale.getWindow().getDecorView().attach();
  yes(stale.dismissed&&stale.getWindow().getDecorView().visibility==8,"late private dialog became visible");
  AlertDialog publicDialog=new LargeDialogBuilder(gate,true).create();publicDialog.getWindow().getDecorView().attach();
  yes(!publicDialog.dismissed,"public identity notice blocked");
  // Failure does not reset a corrupt file to owner or acknowledge a new role.
  disk.values.put("schema",true);boolean failed=false;try{adapter.read();}catch(IOException e){failed=true;}
  yes(failed&&Boolean.TRUE.equals(disk.values.get("schema")),"corrupt Android data silently reset");
 }
}"""
        program = (
            program.replace('UI_METHOD', ui_method)
            .replace('RECEIVE_METHOD', receive)
            .replace('CONNECTED_METHOD', connected)
            .replace('AUTH_PACKET', expression(packet))
        )
        sources['com/happyhome/cc8assistant/NativeIdentityFlow.java'] = program
        java = java_bin()
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            paths = []
            for name, content in sources.items():
                path = folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                paths.append(str(path))
            compile_result = subprocess.run(
                [
                    str(java / 'javac'),
                    '-encoding',
                    'UTF-8',
                    '-d',
                    str(folder),
                    *[
                        str(JAVA / name)
                        for name in (
                            'IdentityState.java',
                            'IdentityPreferences.java',
                            'LargeDialogBuilder.java',
                        )
                    ],
                    *paths,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [
                    str(java / 'java'),
                    '-cp',
                    str(folder),
                    'com.happyhome.cc8assistant.NativeIdentityFlow',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            token, mode, revision = result.stdout.strip().split('|')
            b.on_event(
                {
                    'event': 'identity_applied',
                    'token': token,
                    'mode': mode,
                    'revision': int(revision),
                }
            )
            self.assertTrue(b.identity.device_applied)
            self.assertTrue(b.dialogue_access())
            self.assertFalse(b.owner_access())
