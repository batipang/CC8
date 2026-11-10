"""Execute actual VoiceSession meeting admission/cancellation against inert native ports."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_capture_window import method
from tools.java_toolchain import java_bin

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'
HARNESS = r"""
package com.happyhome.cc8assistant;
import java.io.*;import java.util.*;
class JSONObject {
 final Map<String,Object> values=new HashMap<>();
 JSONObject(Object... fields){for(int i=0;i<fields.length;i+=2)values.put((String)fields[i],fields[i+1]);}
 Object opt(String key){return values.get(key);}String optString(String key){return values.containsKey(key)?values.get(key).toString():"";}
 int optInt(String key){return optInt(key,0);}int optInt(String key,int missing){return opt(key) instanceof Number?((Number)opt(key)).intValue():missing;}
 long optLong(String key){return optLong(key,0);}long optLong(String key,long missing){return opt(key) instanceof Number?((Number)opt(key)).longValue():missing;}
 JSONObject optJSONObject(String key){return opt(key) instanceof JSONObject?(JSONObject)opt(key):null;}
 Iterator<String> keys(){return values.keySet().iterator();}
}
class Wire {
 static final int MEETING=7;static class Packet{int type,generation;long sequence,frame,nanos;byte[] data;}
 static JSONObject event(String name){return new JSONObject("event",name);}static void put(JSONObject value,String key,Object data){value.values.put(key,data);}
}
class WorkflowModel{static String id(){return UUID.randomUUID().toString().replace("-","");}}
class SystemClock{static long now=100;static long elapsedRealtime(){return now;}}
class PackageManager{static final int PERMISSION_GRANTED=0;}
class Manifest{static class permission{static final String RECORD_AUDIO="mic";}}
class AudioRecord {static int opens;int starts;void startRecording(){opens++;starts++;}int getRecordingState(){return starts>0?3:0;}static final int RECORDSTATE_RECORDING=3;}
class MeetingRecorder{
 interface Port{boolean current();boolean start(AudioRecord record);boolean send(byte[] pcm,long sequence,long frame);void finished(boolean failed,boolean released);}
 static final List<MeetingRecorder> created=new ArrayList<>();final Port port;boolean active,stopped;
 MeetingRecorder(Object resource,Port port){this.port=port;created.add(this);}
 void start(){if(!port.current())return;active=port.start(new AudioRecord());if(!active)port.finished(true,true);}
 void stop(){active=false;stopped=true;}boolean capturing(){return active;}
 void release(boolean confirmed){port.finished(!confirmed,confirmed);}
}
class Context{int permission;int checkCallingOrSelfPermission(String name){return permission;}}
class Listener{int updates;void meetingResult(JSONObject data,long route){updates++;}}
class Link{boolean closed,ready=true;final List<Wire.Packet> packets=new ArrayList<>();void close(){closed=true;}void offer(Wire.Packet p)throws IOException{if(closed)throw new IOException();packets.add(p);}}
class Session {
 final Object routeLock=new Object(),captureResourceLock=new Object();final Context context=new Context();final Listener listener=new Listener();
 final MeetingConsent meetingConsent=new MeetingConsent();boolean foreground=true,meetingSupported=true,meetingHostReserved,meetingReleaseFailed;
 boolean recording,visual,announcement,textSession;MeetingRecorder meetingRecorder;String meetingId="",meetingNonce="";long meetingRoute=-1,meetingRevision;int meetingGeneration,minimumGeneration;
 long routeEpoch=1;Link activeLink=new Link();final List<JSONObject> sent=new ArrayList<>();boolean writable=true;
 boolean currentRoute(long route){return route==routeEpoch;}boolean isConnected(){return activeLink!=null&&!activeLink.closed;}
 boolean sendWorkspace(JSONObject value,long route){if(!foreground||!currentRoute(route)||!writable)return false;sent.add(value);return true;}
 void ownPacket(Wire.Packet packet,Link link)throws IOException{if(!foreground||link!=activeLink||link.closed)throw new IOException();}
 METHODS
}
public class MeetingEntryCheck {
 static final String REQUEST="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",ID="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",NONCE="cccccccccccccccccccccccccccccccc";
 static void need(boolean condition,String why){if(!condition)throw new AssertionError(why);}
 static JSONObject startArgs(){return new JSONObject("action","start","offer_id",ID,"confirmed",true,"save_audio",false);}
 static JSONObject result(String request){return new JSONObject("event","meeting_result","request_id",request,"ok",true,
  "capture",new JSONObject("id",ID,"nonce",NONCE,"generation",100,"sample_rate",16000,"packet_type",7),
  "state",new JSONObject("id",ID,"revision",1,"state","capturing"));}
 static void arm(Session s){need(s.meetingRequest(REQUEST,startArgs(),1),"Explicit Start could not enqueue");need(AudioRecord.opens==0,"Request opened hardware before receipt");need(s.meetingReserved(),"Pending confirmation failed to reserve ordinary voice admission");}
 public static void main(String[] args)throws Exception{
  Session s=new Session();String mode=args[0];
  switch(mode){
   case "unsolicited":s.acceptMeeting(result(REQUEST),1,100);need(AudioRecord.opens==0,"Unsolicited reply opened mic");break;
   case "closed":arm(s);s.leaveMeeting(1);s.acceptMeeting(result(REQUEST),1,100);need(AudioRecord.opens==0,"Closed waiting page opened mic");need(s.sent.stream().anyMatch(v->"abort_start".equals(v.optString("action"))),"No bound abort sent");break;
   case "expired":arm(s);SystemClock.now+=12000;s.acceptMeeting(result(REQUEST),1,100);need(AudioRecord.opens==0,"Expired reply opened mic");break;
   case "route":arm(s);s.routeEpoch++;s.acceptMeeting(result(REQUEST),2,100);need(AudioRecord.opens==0,"Replacement route inherited consent");break;
   case "permission":arm(s);s.context.permission=1;s.acceptMeeting(result(REQUEST),1,100);need(AudioRecord.opens==0,"Revoked permission opened mic");need(s.sent.stream().anyMatch(v->"capture_failed".equals(v.optString("action"))),"Native failure not sent");break;
   case "generation":arm(s);s.acceptMeeting(result(REQUEST),1,101);need(AudioRecord.opens==0,"Wrong generation opened mic");break;
   case "valid": {
    arm(s);s.acceptMeeting(result(REQUEST),1,100);need(AudioRecord.opens==1&&s.isMeetingCapturing(),"Confirmed receipt failed to start mic");
    MeetingRecorder r=s.meetingRecorder;need(r.port.send(new byte[320],2,640),"Live capture not sent");
    Wire.Packet p=s.activeLink.packets.get(0);need(p.type==7&&p.sequence==2&&p.frame==640&&p.data.length==336,"Captured frame lost provenance");
    s.leaveMeeting(1);need(r.stopped&&!r.port.send(new byte[320],3,800),"Closing leaked new frames");
    need(!s.isMeetingCapturing(),"Logical stop did not update mic truth");r.release(true);need(s.meetingRecorder==null,"Release did not settle ownership");break;
   }
   case "cancel_generation": {
    arm(s);s.acceptMeeting(result(REQUEST),1,100);MeetingRecorder r=s.meetingRecorder;
    need(s.minimumGeneration==100,"Meeting did not fence old status generations");
    s.cancelMeetingForGeneration(99);need(s.isMeetingCapturing(),"Old cancellation stopped new meeting");
    s.cancelMeetingForGeneration(101);need(r.stopped&&!s.isMeetingCapturing(),"Current cancellation left hardware enabled");break;
   }
   case "late_old": {
    arm(s);s.leaveMeeting(1);String next="dddddddddddddddddddddddddddddddd";
    need(s.meetingRequest(next,startArgs(),1),"Fresh grant rejected");
    s.acceptMeeting(result(REQUEST),1,100);need(AudioRecord.opens==0,"Old reply started fresh grant");
    s.acceptMeeting(result(next),1,100);need(AudioRecord.opens==1,"Old rejection revoked newer grant");break;
   }
   case "release_unknown": {
    arm(s);s.acceptMeeting(result(REQUEST),1,100);MeetingRecorder old=s.meetingRecorder;
    s.leaveMeeting(1);old.release(false);need(s.meetingMediaBlocked()&&s.meetingReserved(),"Unconfirmed release allowed new media");
    need(!s.meetingRequest("dddddddddddddddddddddddddddddddd",startArgs(),1),"Unconfirmed hardware release reused recorder");break;
   }
   case "old_close":arm(s);s.routeEpoch=2;String next=s.meetingConsent.clear();need(!next.isEmpty(),"Fixture lost initial grant");
    need(s.meetingConsent.arm("dddddddddddddddddddddddddddddddd",2,100),"Fresh route did not grant");s.leaveMeeting(1);
    need(s.meetingConsent.take("dddddddddddddddddddddddddddddddd",2,101),"Old dialog closed new route grant");break;
   case "hello":s.meetingHello(new JSONObject("supports_meetings",true,"meeting_state",new JSONObject("state","paused")));
    need(s.meetingReserved()&&AudioRecord.opens==0,"Reconnect forgot reservation or resumed recording");break;
   default:throw new AssertionError("Unknown scenario");
  }
  System.out.println("PASS "+mode);
 }
}
"""


class MeetingEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = java_bin()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-meeting-entry-')
        root = Path(cls.folder.name)
        source = (JAVA / 'VoiceSession.java').read_text()
        methods = '\n'.join(
            method(source, signature)
            for signature in (
                'private void meetingHello(',
                'boolean meetingRequest(',
                'private void abortMeetingStart(',
                'void leaveMeeting(',
                'private String revokeMeeting(',
                'private void acceptMeeting(',
                'private void startMeetingRecorder(',
                'private void cancelMeetingForGeneration(',
                'boolean isMeetingCapturing(',
                'boolean meetingMediaBlocked(',
                'private boolean meetingReserved(',
            )
        )
        # Access is package-local only in this isolated native-port harness.
        methods = methods.replace('private ', '')
        check = root / 'MeetingEntryCheck.java'
        check.write_text(HARNESS.replace('METHODS', methods))
        try:
            result = subprocess.run(
                [
                    str(cls.java / 'javac'),
                    '-encoding',
                    'UTF-8',
                    '-d',
                    str(root),
                    str(JAVA / 'MeetingConsent.java'),
                    str(check),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                raise AssertionError(result.stderr)
        except BaseException:
            cls.folder.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_real_native_entry_and_cancellation(self):
        for case in (
            'unsolicited',
            'closed',
            'expired',
            'route',
            'permission',
            'generation',
            'valid',
            'cancel_generation',
            'late_old',
            'release_unknown',
            'old_close',
            'hello',
        ):
            with self.subTest(case=case):
                result = subprocess.run(
                    [
                        str(self.java / 'java'),
                        '-cp',
                        self.folder.name,
                        'com.happyhome.cc8assistant.MeetingEntryCheck',
                        case,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'PASS ' + case)


if __name__ == '__main__':
    unittest.main()
