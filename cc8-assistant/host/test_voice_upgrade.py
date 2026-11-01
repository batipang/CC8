from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from caption_timing import CaptionTiming
from voice_tools import VoiceTools
from bridge import Bridge
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]


class TimingTests(unittest.TestCase):
    def test_cumulative_final_duplicates_never_regress_and_utf16(self):
        timing = CaptionTiming('你😀好')

        def segment(end, words):
            return {'text_begin': 0, 'text_end': 3, 'time_end': end, 'timestamped_words': words}

        first = {'word_begin': 0, 'word_end': 1, 'time_begin': 0, 'time_end': 100}
        emoji = {'word_begin': 1, 'word_end': 2, 'time_begin': 100, 'time_end': 300}
        timing.feed(segment(100, [first]))
        timing.feed(segment(300, [first, emoji]))
        timing.feed(segment(100, [first]))
        self.assertEqual(timing.rows(), [[0, 1, 0, 1600], [1, 3, 1600, 4800]])

    def test_invalid_provider_offsets_and_nonfinite_times_are_ignored(self):
        timing = CaptionTiming('你好')
        for value in [
            None,
            {},
            {'text_begin': 0, 'text_end': 2, 'time_end': float('nan'), 'timestamped_words': []},
        ]:
            timing.feed(value)
        self.assertEqual(timing.rows(), [])


class IdleTests(unittest.TestCase):
    def bridge(self):
        b = Bridge(None, None)
        b.active = True
        b.connection = object()
        b.last_interaction = time.monotonic() - 901
        b.events = []
        b.event = lambda kind, **kw: b.events.append(kind)
        return b

    def test_idle_goes_to_wake_only_and_touch_restores_without_starting_capture(self):
        b = self.bridge()
        b.wake = False
        b.awake = True
        b.idle_tick()
        self.assertTrue(b.screen_sleeping)
        self.assertFalse(b.awake)
        self.assertEqual(b.events, ['screen_sleep'])
        b.idle_tick()
        self.assertEqual(len(b.events), 1)
        b.touch_activity()
        self.assertFalse(b.screen_sleeping)
        self.assertFalse(b.awake)
        self.assertEqual(b.events[-1], 'screen_wake')

    def test_never_sleep_during_answer_calibration_or_without_user_microphone(self):
        for field in ('playing', 'calibrating', 'response_busy', 'recognition_busy'):
            b = self.bridge()
            owner = (
                b.playback
                if field == 'playing'
                else b.recognition
                if field == 'recognition_busy'
                else b
            )
            setattr(owner, 'busy' if field == 'recognition_busy' else field, True)
            b.idle_tick()
            self.assertFalse(b.screen_sleeping)
        b = self.bridge()
        b.active = False
        b.idle_tick()
        self.assertFalse(b.screen_sleeping)

    def test_device_wake_receipt_resets_standby_state_and_timer(self):
        b = self.bridge()
        b.screen_sleeping = True

        def event(kind, **kw):
            b.events.append(kind)
            if kind == 'device_control':
                b.on_event(
                    {'event': 'device_control_result', 'request_id': kw['request_id'], 'ok': True}
                )

        b.event = event
        result = b.control_device('wake_screen')
        self.assertTrue(result['ok'])
        self.assertFalse(b.screen_sleeping)
        self.assertLess(time.monotonic() - b.last_interaction, 1)
        self.assertEqual(b.events, ['device_control', 'screen_wake'])


class ToolTests(unittest.TestCase):
    def test_device_result_is_real_and_mic_start_is_not_exposed(self):
        calls = []
        tools = VoiceTools(lambda a, p: calls.append((a, p)) or {'ok': True, 'volume_percent': p})
        self.assertTrue(tools.call('cc8_control', {'action': 'volume', 'percent': 40})['ok'])
        self.assertEqual(calls, [('volume', 40)])
        for args in [
            {'action': 'start_microphone'},
            {'action': 'volume', 'percent': 101},
            {'action': 'volume', 'percent': True},
        ]:
            with self.assertRaises(ValueError):
                tools.call('cc8_control', args)

    def test_cancelled_call_does_nothing_and_url_has_no_shell(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        tools = VoiceTools(run=run)
        self.assertFalse(
            tools.call('mac_open', {'kind': 'url', 'target': 'https://example.com'}, lambda: False)[
                'ok'
            ]
        )
        self.assertFalse(calls)
        for target in [
            'file:///etc/passwd',
            'mailto:user@example.com',
            'https://u:p@example.com',
            'javascript:alert(1)',
        ]:
            with self.assertRaises(ValueError):
                tools.call('mac_open', {'kind': 'url', 'target': target})
        tools.call('mac_open', {'kind': 'url', 'target': 'https://example.com/?q=$(hello)'})
        self.assertEqual(calls, [['/usr/bin/open', 'https://example.com/?q=$(hello)']])

    def test_file_traversal_and_private_paths_rejected(self):
        for target in ['../../etc/passwd', '.toolchains', 'device-access/private']:
            with self.assertRaises(ValueError):
                VoiceTools().call('mac_open', {'kind': 'project_file', 'target': target})


class NativeAudioTests(unittest.TestCase):
    def test_short_writes_pause_resume_cancel_and_actual_frame_caption(self):
        java = java_bin()
        source = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
        harness = """package com.happyhome.cc8assistant;
import java.io.*;import java.util.*;
public class NativeCheck {
 static class Sink implements PcmPump.Sink {
  boolean active=true,paused=false;int calls=0;List<Short> values=new ArrayList<>();
  public boolean allowed(){return active;}public boolean paused(){return paused;}
  public void awaitReady(){paused=false;}
  public int write(short[] s,int o,int n){calls++;if(calls==1)return 2;if(calls==2){paused=true;return 0;}return Math.min(2,n);}
  public void accepted(short[] s,int o,int n){for(int i=o;i<o+n;i++)values.add(s[i]);}
 }
 static void eq(float a,float b){if(Math.abs(a-b)>.001)throw new AssertionError(a+" != "+b);}
 public static void main(String[] args)throws Exception{
  Sink sink=new Sink();if(!PcmPump.writeAll(new short[]{1,2,3,4,5},sink))throw new AssertionError();
  if(!sink.values.equals(Arrays.asList((short)1,(short)2,(short)3,(short)4,(short)5)))throw new AssertionError(sink.values);
  Sink cancelled=new Sink(){public int write(short[] s,int o,int n){active=false;return 1;}};
  if(PcmPump.writeAll(new short[]{1,2},cancelled)||!cancelled.values.isEmpty())throw new AssertionError();
  try{PcmPump.writeAll(new short[]{1},new Sink(){public int write(short[] s,int o,int n){return -3;}});throw new AssertionError();}catch(IOException expected){}
  CaptionTimeline line=new CaptionTimeline(new int[][]{{0,1,0,1600},{1,3,3200,6400}},3);
  eq(line.position(0),0);eq(line.position(800),.5f);eq(line.position(2400),1);eq(line.position(4800),2);eq(line.position(6400),3);
  eq(line.position(4800),2); // paused playback head must not advance from wall time
  eq(new CaptionTimeline(new int[0][],20).position(90000),0);
 }
}"""
        with tempfile.TemporaryDirectory() as folder:
            test = Path(folder) / 'NativeCheck.java'
            test.write_text(harness)
            build = subprocess.run(
                [
                    str(java / 'javac'),
                    '-d',
                    folder,
                    str(source / 'PcmPump.java'),
                    str(source / 'CaptionTimeline.java'),
                    str(test),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run(
                [str(java / 'java'), '-cp', folder, 'com.happyhome.cc8assistant.NativeCheck'],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
