"""Exercise the real Java preview timing used by both camera entry paths."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class PreviewDelayTests(unittest.TestCase):
    def test_five_seconds_start_at_visible_frame_not_camera_open(self):
        config=ROOT/'.toolchains/cc8/paths.json'
        if not config.is_file():self.skipTest('Local JDK unavailable')
        java=Path(json.loads(config.read_text())['java_home'])/'bin'
        source=ROOT/'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/PreviewDelay.java'
        harness='''package com.happyhome.cc8assistant;
public final class PreviewCheck {
 static void yes(boolean value){if(!value)throw new AssertionError();}
 public static void main(String[] args){
  PreviewDelay gate=new PreviewDelay();
  yes(!gate.ready(20000));yes(gate.secondsLeft(20000)==5);
  gate.visible(20000);yes(!gate.ready(24999));yes(gate.secondsLeft(20000)==5);
  gate.visible(21000);yes(gate.elapsed(22000)==2000); // Later preview frames do not reset the countdown.
  yes(gate.secondsLeft(21000)==4);yes(gate.secondsLeft(24999)==1);
  yes(gate.ready(25000));yes(gate.secondsLeft(25000)==0);
  gate.reset();yes(!gate.ready(100000)); // Reopening must not reuse the last preview clock.
  gate.visible(100000);yes(!gate.ready(104999));yes(gate.ready(105000));
  gate.reset();gate.visible(0);yes(gate.started());yes(!gate.ready(4999));yes(gate.ready(5000));
 }
}'''
        with tempfile.TemporaryDirectory() as folder:
            test=Path(folder)/'PreviewCheck.java';test.write_text(harness)
            subprocess.run([str(java/'javac'),'-d',folder,str(source),str(test)],check=True,capture_output=True,timeout=30)
            result=subprocess.run([str(java/'java'),'-cp',folder,'com.happyhome.cc8assistant.PreviewCheck'],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
