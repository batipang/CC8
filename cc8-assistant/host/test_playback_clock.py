"""Run the actual Android-free Java presentation-clock helper with the JDK."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class PlaybackClockTests(unittest.TestCase):
    def test_live_stale_and_future_timestamp_paths(self):
        config=ROOT/'.toolchains/cc8/paths.json'
        if not config.exists():self.skipTest('Local JDK is absent')
        java=Path(json.loads(config.read_text())['java_home'])/'bin'
        source=ROOT/'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/PlaybackClock.java'
        harness='''package com.happyhome.cc8assistant;
public class ClockCheck {
 static void eq(long a,long b){if(a!=b)throw new AssertionError(a+" != "+b);}
 public static void main(String[] args){
  eq(PlaybackClock.estimate(1000000000L,3200,1600,true,800,950000000L),1100000000L);
  eq(PlaybackClock.estimate(5000000000L,16000,16000,true,16000,1000000000L),5000000000L);
  eq(PlaybackClock.estimate(1000000000L,0,0,false,0,0),1000000000L);
  eq(PlaybackClock.estimate(1000000000L,0,0,true,0,4000000000L),1000000000L);
  eq(PlaybackClock.estimate(1000000000L,0,0,true,0,990000000L),1000000000L);
 }
}'''
        with tempfile.TemporaryDirectory() as folder:
            test=Path(folder)/'ClockCheck.java';test.write_text(harness)
            compiled=subprocess.run([str(java/'javac'),'-d',folder,str(source),str(test)],capture_output=True,text=True,timeout=30)
            self.assertEqual(compiled.returncode,0,compiled.stderr)
            result=subprocess.run([str(java/'java'),'-cp',folder,'com.happyhome.cc8assistant.ClockCheck'],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
