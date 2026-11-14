"""Real Java classifier/statistics plus bounded owner binding and packet dispatch."""

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from announce import AnnouncementServer
from bridge import Bridge
from echo_pipeline import CleanFrame
from render_clock_report import RenderClockReceipt, summarize
from protocol import Packet, MIC, RENDER, EVENT, SPEAKER, receive, validate_device_event
from test_runtime import Fragmented
from test_playback_evidence import public_signal, scene, START, RATE
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]


def clock_report():
    return {
        'version': 1,
        'complete': True,
        'expired': False,
        'invalid': False,
        'observations': 3,
        'frames': 960,
        'source_changes': 1,
        'clock_steps': 1,
        'max_step_nanos': 180_000_000,
        'source_counts': {'hardware': 2, 'clamped': 0, 'missing': 1, 'invalid': 0, 'outside': 0},
    }


class ClockReportTests(unittest.TestCase):
    def test_strict_shape_and_numeric_consistency_never_export_unknown_fields(self):
        expected = summarize(clock_report())
        self.assertEqual(expected['status'], 'observed')
        self.assertEqual(expected['max_clock_step_ms'], 180)
        self.assertFalse(expected['first_audible_onset_measured'])
        cases = [None, [], {'api_key': 'must_not_export'}]
        for key, value in [
            ('version', True),
            ('complete', 1),
            ('observations', True),
            ('frames', 2),
            ('frames', 2**32),
            ('source_changes', 3),
            ('clock_steps', 0),
            ('max_step_nanos', float('nan')),
            ('max_step_nanos', -1),
            ('expired', True),
        ]:
            bad = clock_report()
            bad[key] = value
            cases.append(bad)
        bad = clock_report()
        bad['source_counts']['missing'] = 100
        cases.append(bad)
        bad = clock_report()
        bad['source_counts']['api_key'] = 1
        cases.append(bad)
        for value in cases:
            with self.subTest(value=value):
                result = summarize(value)
                self.assertEqual(result['status'], 'invalid')
                self.assertNotIn('api_key', json.dumps(result))
        incomplete = clock_report()
        incomplete.update(complete=False, expired=True)
        self.assertEqual(summarize(incomplete)['status'], 'incomplete')

    def test_bound_generation_playback_id_cancel_and_snapshot_isolation(self):
        receipt = RenderClockReceipt()
        for g, ident in [(True, 1), (1, True), (1, 0), (-1, 1), (1, 2**32)]:
            self.assertFalse(receipt.begin(g, ident))
        self.assertTrue(receipt.begin(10, 2))
        self.assertFalse(receipt.begin(10, 2))
        receipt.accept(9, 2, clock_report(), 960)
        receipt.accept(10, 1, clock_report(), 960)
        self.assertEqual(receipt.snapshot()['status'], 'waiting')
        receipt.accept(10, 2, clock_report(), 960)
        first = receipt.snapshot()
        first['source_counts']['hardware'] = 999
        receipt.cancel()
        self.assertEqual(receipt.snapshot()['source_counts']['hardware'], 2)
        other = RenderClockReceipt()
        other.begin(11, 3)
        other.cancel()
        other.accept(11, 3, clock_report(), 960)
        self.assertEqual(other.snapshot()['status'], 'cancelled')
        missing = RenderClockReceipt()
        missing.begin(12, 4)
        self.assertEqual(missing.snapshot(final=True)['status'], 'missing')
        missing.accept(12, 4, clock_report(), 960)
        self.assertEqual(missing.snapshot()['status'], 'missing')

    def test_unavailable_hardware_clock_is_reported_without_inventing_support(self):
        report = clock_report()
        report['source_counts'].update(hardware=0, missing=3)
        report['source_changes'] = 0
        result = summarize(report)
        self.assertEqual(result['status'], 'observed')
        self.assertFalse(result['hardware_timestamp_accepted'])
        report['source_changes'] = 1
        # Single-source provenance cannot claim an observed source transition.
        self.assertEqual(summarize(report)['status'], 'invalid')

    def test_inconsistent_frame_receipt_is_not_accepted(self):
        for expected in (None, True, 959, 961):
            receipt = RenderClockReceipt()
            receipt.begin(1, 2)
            receipt.accept(1, 2, clock_report(), expected)
            self.assertEqual(receipt.snapshot()['status'], 'invalid')

    def test_cancel_during_validation_cannot_publish_a_late_report(self):
        receipt = RenderClockReceipt()
        receipt.begin(10, 2)
        entered, release = threading.Event(), threading.Event()

        def parse(value):
            entered.set()
            release.wait(1)
            return summarize(value)

        with patch('render_clock_report.summarize', side_effect=parse):
            thread = threading.Thread(target=receipt.accept, args=(10, 2, clock_report(), 960))
            thread.start()
            self.assertTrue(entered.wait(1))
            receipt.cancel()
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(receipt.snapshot()['status'], 'cancelled')

    def test_real_java_sources_and_statistics_roundtrip(self):
        source = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
        harness = r"""package com.happyhome.cc8assistant;
public class ClockStatsCheck {
 static void eq(long a,long b){if(a!=b)throw new AssertionError(a+" != "+b);}
 static long legacy(long now,long written,long head,boolean valid,long position,long timestamp){
  long fallback=now+Math.max(0,written-head)*62500L;
  if(!valid||timestamp<=0||position<0||position>written)return fallback;
  long predicted=timestamp+(written-position)*62500L;
  if(predicted<now-20000000L||predicted>now+1000000000L)return fallback;
  return Math.max(now,predicted);
 }
 public static void main(String[] args) {
  java.util.Random random=new java.util.Random(20261104L);
  for(int i=0;i<10000;i++){
   long now=1000000000L+random.nextInt(1000000)*1000000L;
   long written=random.nextInt(1000000),head=random.nextInt((int)written+3201);
   long position=random.nextInt((int)written+641)-320;
   long timestamp=now+(random.nextInt(10001)-5000)*1000000L;
   boolean valid=random.nextBoolean();
   eq(PlaybackClock.estimate(now,written,head,valid,position,timestamp),
      legacy(now,written,head,valid,position,timestamp));
  }
  eq(PlaybackClock.source(1000000000L,0,false,0,0),PlaybackClock.MISSING);
  eq(PlaybackClock.source(1000000000L,0,true,0,0),PlaybackClock.INVALID);
  eq(PlaybackClock.source(1000000000L,0,true,0,4000000000L),PlaybackClock.OUTSIDE);
  eq(PlaybackClock.source(1000000000L,0,true,0,990000000L),PlaybackClock.CLAMPED);
  eq(PlaybackClock.source(1000000000L,0,true,0,1000000000L),PlaybackClock.HARDWARE);
  RenderClockStats s=new RenderClockStats(10);
  s.observe(10,0,320,1000000000L,PlaybackClock.MISSING);
  s.observe(30,320,320,1200000000L,PlaybackClock.HARDWARE);
  s.observe(50,640,320,1220000000L,PlaybackClock.HARDWARE);
  if(!s.complete())throw new AssertionError("not complete");
  eq(s.frames,960);eq(s.observations,3);eq(s.clockSteps,1);eq(s.sourceChanges,1);
  eq(s.maxStepNanos,180000000L);
  StringBuilder b=new StringBuilder("{\"version\":1,\"complete\":true,\"expired\":false,\"invalid\":false,");
  b.append("\"observations\":").append(s.observations).append(",\"frames\":").append(s.frames);
  b.append(",\"source_changes\":").append(s.sourceChanges).append(",\"clock_steps\":").append(s.clockSteps);
  b.append(",\"max_step_nanos\":").append(s.maxStepNanos).append(",\"source_counts\":{");
  for(int i=0;i<s.counts.length;i++){if(i>0)b.append(',');b.append('"').append(RenderClockStats.SOURCES[i]).append("\":").append(s.counts[i]);}
  System.out.println(b.append("}}").toString());
  s.observe(20010,960,320,1240000000L,PlaybackClock.HARDWARE);
  if(s.complete()||!s.expired)throw new AssertionError("deadline");
  RenderClockStats invalid=new RenderClockStats(0);invalid.observe(0,320,320,100,0);
  if(invalid.complete()||!invalid.invalid)throw new AssertionError("gap");
 }
}"""
        java = java_bin()
        with tempfile.TemporaryDirectory() as folder:
            test = Path(folder) / 'ClockStatsCheck.java'
            test.write_text(harness)
            subprocess.run(
                [
                    str(java / 'javac'),
                    '-d',
                    folder,
                    str(source / 'PlaybackClock.java'),
                    str(source / 'RenderClockStats.java'),
                    str(test),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = subprocess.run(
                [str(java / 'java'), '-cp', folder, 'com.happyhome.cc8assistant.ClockStatsCheck'],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(json.loads(output.stdout), clock_report())
        self.assertEqual(summarize(json.loads(output.stdout))['status'], 'observed')


class ClockWireTests(unittest.TestCase):
    def run_probe(self, *, supported, payload=None, wrong_id=False):
        reference = public_signal()

        class Speech:
            def stream(self, text, current):
                yield (reference * 32768).astype('<i2')

        bridge = Bridge(Speech(), None)
        bridge.session.active = True
        bridge.session.epoch = 7
        bridge.save_status = lambda: None
        bridge.playback.set_playout_capability(True)
        bridge.playback.set_completion_capability(supported)
        bridge.playback.set_clock_report_capability(supported)
        bridge.capture.echo.process = lambda stamp, raw: CleanFrame(stamp, raw, raw, raw, raw)
        bridge.capture.endpoint.consume = lambda frame: None
        begins = []

        class Loopback:
            def sendall(self, data):
                p = receive(Fragmented(data))
                if p.kind == SPEAKER:
                    bridge.on_packet(
                        Packet(
                            RENDER,
                            p.generation,
                            p.sequence,
                            p.frame,
                            START + round(p.frame * 1e9 / RATE),
                            p.data,
                        )
                    )
                if p.kind == EVENT:
                    event = json.loads(p.data)
                    if event['event'] == 'play_begin':
                        begins.append(event)
                    if event['event'] == 'play_end':
                        microphone = scene(reference)
                        for frame in range(0, len(microphone), 320):
                            bridge.on_packet(
                                Packet(
                                    MIC,
                                    7,
                                    frame // 320,
                                    frame,
                                    START - 200_000_000 + round(frame * 1e9 / RATE),
                                    (microphone[frame : frame + 320] * 32768)
                                    .astype('<i2')
                                    .tobytes(),
                                )
                            )
                        done = {'event': 'play_done', 'generation': p.generation}
                        if supported:
                            done.update(
                                playback_id=begins[-1]['playback_id'] + (1 if wrong_id else 0),
                                played_frames=event['frames'],
                                render_clock=payload,
                            )
                        bridge.on_event(done)
                        if supported and wrong_id:
                            if not bridge.playback.playing:
                                raise AssertionError('Stale completion ended the current clause')
                            bridge.on_event(
                                {
                                    'event': 'play_done',
                                    'generation': p.generation,
                                    'playback_id': begins[-1]['playback_id'],
                                    'played_frames': event['frames'],
                                }
                            )

        bridge.session.connection = Loopback()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.sock'
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                import socket

                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect(str(path))
                    client.sendall(b'{"operation":"playback_probe"}\n')
                    result = json.loads(client.recv(8192))
            finally:
                server.close()
                bridge.cancel_playback_observation()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['evidence']['status'], 'observed')
        self.assertEqual(begins[0]['report_audio_clock'], supported)
        return result['evidence']['render_clock_evidence']

    def test_supported_packet_receipt_is_attached_before_announcement_completes(self):
        self.assertEqual(
            self.run_probe(supported=True, payload={**clock_report(), 'frames': 16000})['status'],
            'observed',
        )

    def test_old_apk_does_not_get_a_request_or_fabricated_zero_observations(self):
        self.assertEqual(self.run_probe(supported=False)['status'], 'not_requested')

    def test_bad_metadata_and_old_playback_receipts_do_not_change_audio_completion(self):
        self.assertEqual(self.run_probe(supported=True, payload={})['status'], 'invalid')
        self.assertEqual(
            self.run_probe(supported=True, payload=clock_report(), wrong_id=True)['status'],
            'missing',
        )
        self.assertEqual(
            self.run_probe(supported=True, payload=clock_report())['status'], 'invalid'
        )

    def test_normal_announcement_does_not_enable_clock_probe_and_old_peer_clears_capability(self):
        from test_announce import AnnouncementTests

        bridge = AnnouncementTests().bridge()
        bridge.playback.set_playout_capability(True)
        bridge.playback.set_completion_capability(True)
        bridge.playback.set_clock_report_capability(True)
        sink = bridge.session.connection
        clause = []

        def send(data):
            packet = receive(Fragmented(data))
            sink.packets.append(packet)
            if packet.kind == EVENT:
                event = json.loads(packet.data)
                if event['event'] == 'play_begin':
                    clause.append(event['playback_id'])
                elif event['event'] == 'play_end':
                    bridge.on_event(
                        {
                            'event': 'play_done',
                            'generation': packet.generation,
                            'playback_id': clause[-1],
                            'played_frames': event['frames'],
                        }
                    )

        sink.sendall = send
        job = bridge.announce('公开的普通播报测试')
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'completed')
        self.assertFalse(bridge.session.active)
        events = [json.loads(p.data) for p in bridge.session.connection.packets if p.kind == EVENT]
        self.assertFalse(
            next(j for j in events if j['event'] == 'play_begin')['report_audio_clock']
        )
        bridge.session.ready({'playout_receipt_version': 1, 'capture_active': False})
        self.assertFalse(bridge.playback.device_can_report_clock)

    def test_late_same_generation_completion_and_error_do_not_finish_next_clause(self):
        from test_barge_in import Sink

        bridge = Bridge(None, None)
        bridge.session.connection = Sink()
        bridge.save_status = lambda: None
        generation = bridge.generation
        bridge.playback.device_can_report_completion = True
        bridge.playback.playback_serial = 2
        bridge.playback.playing = True
        bridge.playback.end_frames = 1
        for event in (
            {'event': 'play_done', 'generation': generation, 'playback_id': 1, 'played_frames': 1},
            {'event': 'playback_error', 'generation': generation, 'playback_id': 1},
            {'event': 'play_done', 'generation': generation, 'played_frames': 1},
            {'event': 'play_done', 'generation': generation, 'playback_id': 2},
        ):
            bridge.on_event(event)
            self.assertTrue(bridge.playback.playing)
            self.assertEqual(bridge.generation, generation)
            self.assertNotIn(generation, bridge.playback.completed)
        bridge.on_event(
            {'event': 'play_done', 'generation': generation, 'playback_id': 2, 'played_frames': 1}
        )
        self.assertFalse(bridge.playback.playing)
        self.assertIn(generation, bridge.playback.completed)

    def test_protocol_rejects_boolean_capability_or_playback_id(self):
        for value in (
            {'event': 'device_ready', 'render_clock_report_version': True},
            {'event': 'play_done', 'generation': 2, 'playback_id': True},
            {'event': 'play_done', 'generation': 2, 'played_frames': True},
            {'event': 'play_done', 'generation': 2, 'played_frames': -1},
        ):
            with self.assertRaises(ValueError):
                validate_device_event(value)
