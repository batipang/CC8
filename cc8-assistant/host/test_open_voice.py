"""Run the real Android-free opening gate against capture lifecycle scenarios."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]


class OpenVoiceTests(unittest.TestCase):
    def test_open_stop_background_timeout_and_reconnect(self):
        java = java_bin()
        source = (
            ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/OpenVoiceGate.java'
        )
        harness = """package com.happyhome.cc8assistant;
public final class OpenCheck {
 static void yes(boolean value){if(!value)throw new AssertionError();}
 public static void main(String[] args){
  OpenVoiceGate gate=new OpenVoiceGate();
  yes(!gate.take(0,true,true,false)); // Reconnect is not an opening.
  gate.opened(100);yes(!gate.take(100,true,true,false));gate.resumed(100);yes(gate.waiting(100));
  yes(!gate.take(200,true,false,false)); // First connection is still pending.
  yes(!gate.take(300,false,true,false)); // No foreground, no microphone.
  yes(gate.take(400,true,true,false)); // Opening is consumed before permission/capture.
  yes(!gate.take(500,true,true,false)); // Even failed/denied capture cannot loop.
  yes(!gate.take(600,true,false,false));yes(!gate.take(700,true,true,false));
  gate.opened(1000);gate.resumed(1000);gate.cancel(); // Manual stop / background cancels a pending opening.
  yes(!gate.waiting(1100));yes(!gate.take(1200,true,true,false));
  gate.opened(2000);gate.resumed(2000);yes(!gate.take(2100,true,true,true)); // Existing session stays unchanged.
  yes(!gate.take(2200,true,true,false)); // Later stopping it cannot resurrect the opening.
  gate.opened(3000);gate.resumed(3000);yes(gate.waiting(32999));yes(gate.expire(33000));
  yes(!gate.expire(34000));yes(!gate.take(35000,true,true,false));
  gate.opened(40000);gate.resumed(40000);yes(gate.take(40001,true,true,false)); // Intent-before-pause also starts on the next resume.
  gate.opened(50000);gate.cancel();gate.resumed(50001);yes(!gate.take(50002,true,true,false)); // Explicit cancellation clears even a staged open.
  gate.opened(60000);gate.resumed(60000);gate.stopped();gate.resumed(60001);yes(!gate.take(60002,true,true,false)); // Actual background/resume does not rearm.
  gate.opened(70000);gate.resumed(70000);yes(!gate.take(70001,false,true,false));gate.resumed(71000);yes(gate.take(71001,true,true,false)); // Transient pause preserves the unconsumed opening, never captures while paused.
 }
}"""
        with tempfile.TemporaryDirectory() as folder:
            test = Path(folder) / 'OpenCheck.java'
            test.write_text(harness)
            subprocess.run(
                [str(java / 'javac'), '-d', folder, str(source), str(test)],
                check=True,
                capture_output=True,
                timeout=30,
            )
            result = subprocess.run(
                [str(java / 'java'), '-cp', folder, 'com.happyhome.cc8assistant.OpenCheck'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
