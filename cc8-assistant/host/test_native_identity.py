"""Compile actual native privacy floor/epoch code; fake store, no Android resources."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]


class NativeIdentityTests(unittest.TestCase):
    def test_actual_native_mode_floor_persistence_old_callbacks_and_failure_paths(self):
        source = (
            ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/IdentityState.java'
        )
        json = """package org.json;import java.util.*;public class JSONObject {
 public final Map<String,Object> data=new HashMap<>();
 public JSONObject(Object...pairs){for(int i=0;i<pairs.length;i+=2)data.put((String)pairs[i],pairs[i+1]);}
 public Object opt(String key){return data.get(key);}public int length(){return data.size();}}"""
        harness = """package com.happyhome.cc8assistant;import org.json.*;import java.io.*;
public class IdentityCheck {
 static void yes(boolean v,String m){if(!v)throw new AssertionError(m);}
 static class Disk implements IdentityState.Store {
  IdentityState.Saved saved=new IdentityState.Saved(false,"owner",-1);boolean fail;int reads,writes;
  public IdentityState.Saved read(){reads++;return saved;}
  public boolean write(IdentityState.Saved s){writes++;if(fail)return false;saved=s;return true;}
 }
 static JSONObject state(String mode,Object revision,String authority,String token){
  return new JSONObject("event","identity_state","token",token,"authority",authority,
   "identity_mode",mode,"identity_revision",revision,"state",new JSONObject("schema",1,
   "mode",mode,"revision",revision,"ready",true,"owner_restore","desktop_only","device_applied",false));
 }
 static JSONObject event(String mode,Object revision){return new JSONObject("identity_mode",mode,"identity_revision",revision);}
 public static void main(String[] args)throws Exception {
  String a="a".repeat(32),b="b".repeat(32);Disk disk=new Disk();IdentityState s=new IdentityState(disk);
  yes(disk.reads==0&&disk.writes==0&&!s.ready(),"constructor performed I/O or granted role");
  s.load();s.route(1);yes(!s.ready()&&!s.owner(),"route implicitly granted owner");
  yes(s.accept(state("owner",0,"reconcile",a),1)&&s.owner(),"default owner receipt rejected");
  long ownerEpoch=s.epoch();yes(s.accepts(event("owner",0),1),"owner packet rejected");
  yes(s.requestGuest()==0&&!s.ready()&&disk.saved.guestRequested,"guest intent was not persisted before request");
  yes(!s.current(ownerEpoch)&&!s.accepts(event("owner",0),1),"queued owner callback survived guest intent");
  yes(!s.acceptLegacy(1)&&!s.accept(state("owner",0,"owner_desktop",a),1),"old owner/legacy reopened guest pending");
  yes(!s.accept(state("guest",0,"reconcile",a),1),"same revision changed role");
  yes(s.accept(state("guest",1,"reconcile",b),1)&&!s.owner()&&s.ready(),"guest not applied");
  yes(!s.accepts(event("owner",0),1)&&s.accepts(event("guest",1),1),"stale owner packet escaped role gate");
  long guestEpoch=s.epoch();int writes=disk.writes;
  yes(s.accept(state("guest",1,"reconcile",b),1)&&s.current(guestEpoch)&&disk.writes==writes,"duplicate receipt reset scope or rewrote state");
  IdentityState restart=new IdentityState(disk);restart.load();restart.route(2);
  yes(restart.guestRequested()&&!restart.acceptLegacy(2),"restart lost restrictive choice");
  yes(!restart.accept(state("owner",2,"reconcile",a),2),"unauthorized owner source accepted");
  disk.fail=true;boolean failed=false;try{restart.accept(state("owner",2,"owner_desktop",a),2);}catch(IOException e){failed=true;}
  yes(failed&&!restart.ready()&&restart.guestRequested(),"failed owner persistence granted access");
  disk.fail=false;yes(restart.accept(state("owner",2,"owner_desktop",a),2)&&restart.owner(),"desktop restore failed");
  yes(!restart.accept(state("guest",1,"reconcile",b),2),"older guest revision rolled state backward");
  restart.route(3);yes(!restart.ready()&&!restart.accept(state("owner",2,"owner_desktop",a),2),"old route applied state");
  yes(restart.accept(state("owner",2,"owner_desktop",b),3),"same saved owner could not reconcile on new route");
  for(Object bad:new Object[]{true,-1,1.0,"3",Integer.MAX_VALUE+1L}){
   yes(!restart.accept(state("guest",bad,"reconcile",a),3),"malformed revision accepted");
  }
  JSONObject mismatch=state("guest",3,"reconcile",a);mismatch.data.put("identity_revision",2);
  yes(!restart.accept(mismatch,3),"conflicting outer role metadata accepted");
  yes(!restart.accept(state("guest",3,"reconcile","x"),3),"malformed nonce accepted");
  disk.fail=true;failed=false;try{restart.requestGuest();}catch(IOException e){failed=true;}
  yes(failed&&!restart.ready()&&restart.guestRequested()&&!restart.acceptLegacy(3),"failed guest intent fell back to owner");
  yes(!restart.accept(state("owner",2,"owner_desktop",a),3),"failed guest intent reopened with old receipt");
 }
}"""
        java = java_bin()
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            files = []
            for name, contents in [('JSONObject.java', json), ('IdentityCheck.java', harness)]:
                path = folder / name
                path.write_text(contents)
                files.append(str(path))
            result = subprocess.run(
                [str(java / 'javac'), '-encoding', 'UTF-8', '-d', str(folder), str(source), *files],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [
                    str(java / 'java'),
                    '-cp',
                    str(folder),
                    'com.happyhome.cc8assistant.IdentityCheck',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
