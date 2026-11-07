"""Compile actual availability state and connection-dialog callbacks; no Android I/O."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_interrupted_task_notice import method
from tools.java_toolchain import java_bin

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'

HARNESS = r"""
package com.happyhome.cc8assistant;
import java.util.*;
public class AvailabilityCheck {
 static void yes(boolean b,String m){if(!b)throw new AssertionError(m);}
 static class JSONObject {
  final Map<String,Object> values;
  JSONObject(Map<String,Object> values){this.values=values;}
  boolean optBoolean(String key){return Boolean.TRUE.equals(values.get(key));}
  String optString(String key){Object v=values.get(key);return v instanceof String?(String)v:"";}
  JSONObject optJSONObject(String key){return (JSONObject)values.get(key);}
 }
 static class Session {
  final ConnectionAvailability availability=new ConnectionAvailability();
  long now;int reads;
  JSONObject snapshot(){reads++;Map<String,Object> v=new HashMap<>();v.put("wifi_paired",true);v.put("transport","usb");v.put("connection_availability",new JSONObject(availability.snapshot(now).values()));return new JSONObject(v);}
 }
 static class DesktopEntryService {static int calls;static void settings(Object context){calls++;}}
 interface Click {void click(AlertDialog d,int which);}
 interface Dismiss {void dismiss(AlertDialog d);}
 static class AlertDialog {
  String message;boolean showing;Dismiss dismiss;int writes;
  void setMessage(String value){message=value;writes++;}
  void setOnDismissListener(Dismiss d){dismiss=d;}
  void show(){showing=true;}boolean isShowing(){return showing;}
  void dismiss(){showing=false;if(dismiss!=null)dismiss.dismiss(this);}
  static class Builder {
   AlertDialog value=new AlertDialog();Builder(Object context){}
   Builder setTitle(String title){return this;}
   Builder setMessage(String text){value.message=text;return this;}
   Builder setNeutralButton(String text,Click click){return this;}
   Builder setPositiveButton(String text,Click click){return this;}
   AlertDialog create(){return value;}
  }
 }
 static class Handler {
  final ArrayDeque<Runnable> queued=new ArrayDeque<>();
  void postDelayed(Runnable r,long ms){yes(ms==1000,"Unexpected UI refresh cadence");queued.add(r);}
  void removeCallbacks(Runnable r){queued.removeIf(v->v==r);}
  void tick(){Runnable r=queued.poll();if(r!=null)r.run();}
 }
 boolean foreground=true;
 final Handler handler=new Handler();final Session session=new Session();
 AlertDialog connectionInfoDialog;
 METHODS
 public static void main(String[] args) {
  ConnectionAvailability a=new ConnectionAvailability();
  switch(args[0]) {
   case "initial_wait":
    yes("not_observing".equals(a.snapshot(0).state),"Fresh helper pretends foreground");
    a.begin(10);a.offline(100);a.offline(200);
    yes(a.snapshot(2010).waitingMs==2000,"Initial wait was reset by reconnect attempts");
    yes(a.snapshot(2010).unavailableMs==0&&a.snapshot(2010).recoveredCount==0,"Startup was counted as an outage");
    a.online(3010);yes(a.snapshot(4000).lastRecoveredMs==-1,"First connection fabricated a recovered outage");break;
   case "current_outage":
    a.begin(0);a.online(100);a.offline(1000);a.offline(3000);
    yes(a.snapshot(61000).unavailableMs==60000,"Repeated false event reset the outage start");
    yes(a.snapshot(61000).summary().contains("1 分 0 秒"),"Current duration is not displayed");
    a.online(62000);yes(a.snapshot(63000).lastRecoveredMs==61000,"Recovered interval changed after connection");
    yes(a.snapshot(63000).recoveredCount==1&&a.snapshot(63000).recoveredTotalMs==61000,"Recovered receipt missing");break;
   case "route_change":
    a.begin(0);a.online(100);a.online(1000);a.online(2000);
    yes(a.snapshot(4000).recoveredCount==0&&"connected".equals(a.snapshot(4000).state),"USB/WiFi reselection invented downtime");break;
   case "foreground_boundary":
    a.begin(0);a.online(100);a.offline(1000);a.end(2000);
    a.end(3000);a.online(5000);a.offline(6000);
    yes("not_observing".equals(a.snapshot(1000000).state),"Background callback restarted observation");
    a.begin(1000000);yes(a.snapshot(1001000).waitingMs==1000,"Background time became current downtime");
    a.online(1002000);yes(a.snapshot(1003000).recoveredCount==0&&a.snapshot(1003000).interruptedCount==1,"Unknown interval became confirmed recovery");break;
   case "completed_gaps":
    a.begin(0);a.online(0);a.offline(100);a.online(200);a.offline(300);a.online(800);
    a.end(900);a.begin(100000);a.online(100100);
    yes(a.snapshot(100200).recoveredCount==2&&a.snapshot(100200).recoveredTotalMs==600&&a.snapshot(100200).lastRecoveredMs==500,"Closed gap receipts mixed with background wait");break;
   case "clock_bounds":
    a.begin(1000);a.online(2000);a.offline(3000);
    yes(a.snapshot(-1).unavailableMs==0,"Negative clock produced a negative interval");
    yes(a.snapshot(6000).unavailableMs==3000&&a.snapshot(4000).unavailableMs==3000,"Delayed sample erased observed duration");
    yes(ConnectionAvailability.duration(Long.MAX_VALUE).contains("小时"),"Large interval overflowed display");break;
   case "summary_and_export":
    a.begin(0);yes(a.snapshot(1000).summary().contains("尚未连上"),"Waiting is presented as an established outage");
    a.online(1000);a.offline(2000);
    yes(a.snapshot(3000).summary().contains("暂不可用"),"Unavailable assistant is not explained");
    yes(a.snapshot(3000).values().keySet().equals(new HashSet<>(Arrays.asList("version","state","summary","waiting_ms","unavailable_ms","last_recovered_ms","recovered_count","recovered_total_ms","interrupted_count"))),"Export contains unrelated/private state");break;
   case "dialog_updates": {
    AvailabilityCheck c=new AvailabilityCheck();c.session.availability.begin(0);c.showConnectionInfo();
    yes(c.connectionInfoDialog.message.contains("尚未连上"),"Dialog did not show initial wait");
    c.session.availability.online(1000);c.session.availability.offline(2000);c.session.now=7000;c.handler.tick();
    yes(c.connectionInfoDialog.message.contains("5 秒"),"Open dialog duration did not advance");
    c.session.availability.online(9000);c.session.now=10000;c.handler.tick();
    yes(c.connectionInfoDialog.message.contains("最近一次已恢复的中断：7 秒"),"Open dialog did not observe recovery");
    int writes=c.connectionInfoDialog.writes;c.session.now=11000;c.handler.tick();
    yes(c.connectionInfoDialog.writes==writes,"Unchanged connection details rewrote the dialog");
    yes(c.connectionInfoDialog.message.contains("不能确定原因"),"Dialog falsely implies a sleep diagnosis");
    yes(DesktopEntryService.calls==0,"Read-only dialog invoked an action");break;
   }
   case "dialog_lifetime": {
    AvailabilityCheck c=new AvailabilityCheck();c.showConnectionInfo();AlertDialog old=c.connectionInfoDialog;
    Runnable stale=c.handler.queued.peek();c.showConnectionInfo();AlertDialog next=c.connectionInfoDialog;
    yes(!old.isShowing()&&next!=old&&c.handler.queued.size()==1,"Reopening leaked updater/dialog");
    old.dismiss();yes(c.connectionInfoDialog==next,"Old dismiss cleared newer dialog");
    int reads=c.session.reads;stale.run();yes(c.session.reads==reads,"Stale updater queried the new dialog");
    next.dismiss();yes(c.connectionInfoDialog==null&&c.handler.queued.isEmpty(),"Dismiss kept polling");
    c.foreground=false;c.showConnectionInfo();yes(c.connectionInfoDialog==null,"Background opened a dialog");break;
   }
   case "background_tick": {
    AvailabilityCheck c=new AvailabilityCheck();c.showConnectionInfo();c.foreground=false;int reads=c.session.reads;c.handler.tick();
    yes(c.session.reads==reads&&c.handler.queued.isEmpty(),"Background retained periodic snapshot reads");break;
   }
   default:throw new AssertionError("Unknown test");
  }
 }
}
"""


class AvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        source = (JAVA / 'MainActivity.java').read_text()
        methods = '\n'.join(
            method(source, s)
            for s in (
                'private String connectionInfoMessage(',
                'private void showConnectionInfo(',
            )
        )
        file = cls.folder / 'AvailabilityCheck.java'
        file.write_text(HARNESS.replace('METHODS', methods))
        cls.java = java_bin()
        result = subprocess.run(
            [
                str(cls.java / 'javac'),
                '-encoding',
                'UTF-8',
                '-d',
                str(cls.folder),
                str(JAVA / 'ConnectionAvailability.java'),
                str(file),
            ],
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
                'com.happyhome.cc8assistant.AvailabilityCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


for _name in (
    'initial_wait',
    'current_outage',
    'route_change',
    'foreground_boundary',
    'completed_gaps',
    'clock_bounds',
    'summary_and_export',
    'dialog_updates',
    'dialog_lifetime',
    'background_tick',
):
    setattr(AvailabilityTests, 'test_' + _name, lambda self, name=_name: self.scenario(name))
