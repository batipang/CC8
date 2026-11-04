"""Native-free progress helper plus real packet/receipt correlation; never plays sound."""

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
from bridge import Bridge
from diagnostic_run import DiagnosticRun
from diagnostics import TraceBook
from playout_receipt import PlayoutReceipt
from protocol import Packet, EVENT, SPEAKER, decode_event, validate_device_event
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]


def event(generation=100, playback_id=1, **values):
    result = dict(
        event='playout_progress',
        generation=generation,
        playback_id=playback_id,
        first_signal_frame=2,
        played_frames=3,
        accepted_frames=4,
        nanos=900000000000,
    )
    result.update(values)
    return result


def acknowledge(probe, value):
    return probe.accept(
        *(
            value[key]
            for key in (
                'generation',
                'playback_id',
                'first_signal_frame',
                'played_frames',
                'accepted_frames',
            )
        )
    )


class PlayoutReceiptTests(unittest.TestCase):
    def test_exact_signal_range_once_per_clause_and_clear_are_required(self):
        probe = PlayoutReceipt()
        self.assertFalse(acknowledge(probe, event()))
        probe.begin(100, 1)
        probe.reserve(100, 1, 0, np.array([0, 0, 1, -2], dtype=np.int16))
        for update in [
            dict(played_frames=2),
            dict(played_frames=5, accepted_frames=5),
            dict(accepted_frames=5),
            dict(first_signal_frame=1),
            dict(playback_id=2),
            dict(generation=101),
        ]:
            self.assertFalse(acknowledge(probe, event(**update)), update)
        self.assertTrue(acknowledge(probe, event()))
        self.assertFalse(acknowledge(probe, event()))
        probe.clear()
        self.assertFalse(acknowledge(probe, event()))

    def test_silence_old_sender_and_noncontiguous_reservation_cannot_create_ack(self):
        probe = PlayoutReceipt()
        probe.begin(100, 1)
        probe.reserve(100, 1, 0, np.zeros(4, dtype=np.int16))
        self.assertFalse(acknowledge(probe, event(first_signal_frame=0)))
        probe.begin(101, 2)
        probe.reserve(100, 1, 4, np.ones(4, dtype=np.int16))
        self.assertEqual(probe.maximum, 0)
        with self.assertRaises(ValueError):
            probe.reserve(101, 2, 4, np.ones(4, dtype=np.int16))
        probe.reserve(101, 2, 0, np.ones(4, dtype=np.int16))
        self.assertFalse(acknowledge(probe, event()))
        self.assertTrue(acknowledge(probe, event(101, 2, first_signal_frame=0)))

    def test_event_rejects_missing_boolean_negative_and_unbounded_scalars(self):
        self.assertEqual(decode_event(json.dumps(event()).encode())['played_frames'], 3)
        for key in (
            'generation',
            'playback_id',
            'first_signal_frame',
            'played_frames',
            'accepted_frames',
            'nanos',
        ):
            for value in (True, -1, 1.5, '3', None, 2**64):
                invalid = event(**{key: value})
                with (
                    self.subTest(key=key, value=type(value).__name__),
                    self.assertRaises(ValueError),
                ):
                    validate_device_event(invalid)
            invalid = event()
            invalid.pop(key)
            with self.assertRaises(ValueError):
                validate_device_event(invalid)
        for value in (True, -1, '1', 1.5):
            with self.assertRaises(ValueError):
                validate_device_event({'event': 'device_ready', 'playout_receipt_version': value})

    def session(self):
        with patch('threading.Thread.start'):
            b = Bridge(None, None)
        b.session.connection = object()
        b.session.active = True
        b.session.awake = True
        b.save_status = lambda: None
        b.log = lambda *a, **k: None
        b.event = lambda *a, **k: None
        return b

    def test_ack_from_real_reader_before_send_returns_is_admitted_using_host_clock(self):
        b = self.session()
        b.playback.set_playout_capability(True)
        generation = b.generation
        key = b.diagnostics.begin('text', generation)
        now = [100.0]
        b.diagnostics.clock = lambda: now[0]
        # Rebase synthetic start before generating stages; no device/host clock subtraction.
        b.diagnostics.rows[-1]['started'] = 100.0
        b.diagnostics.mark(key, 'utterance_end')
        b.diagnostics.mark(key, 'first_pcm')
        sent = []
        errors = []

        def output(name, **fields):
            if name == 'play_begin':
                sent.append(fields)
            if name == 'play_end':
                b.on_event({'event': 'play_done', 'generation': generation})

        b.event = output

        def send(packet):
            self.assertEqual(packet.kind, SPEAKER)
            now[0] = 100.25

            def receive():
                try:
                    # Android control packet header is the capture epoch;
                    # its body binds playback generation and per-clause ID.
                    payload = event(generation, sent[-1]['playback_id'])
                    b.on_packet(Packet(EVENT, b.session.epoch, data=json.dumps(payload).encode()))
                except Exception as error:
                    errors.append(error)

            reader = threading.Thread(target=receive)
            reader.start()
            reader.join(1)
            self.assertFalse(reader.is_alive())

        b.send = send
        self.assertTrue(b.play(np.array([0, 0, 1, -2], dtype=np.int16), 'synthetic', generation))
        self.assertEqual(errors, [])
        self.assertEqual(sent[0]['playout_receipt_version'], 1)
        self.assertEqual(b.metrics['playout_receipts'], 1)
        report = b.diagnostics.report()
        self.assertEqual(report['requests'][0]['stages']['playout_ack'], 250)
        self.assertEqual(
            report['groups']['text']['latencies']['utterance_to_playout_ack_ms']['p50'], 250
        )
        self.assertFalse(report['playout_ack_is_audible_onset'])
        # A duplicate after completion never updates counters or fabricates another stage.
        b.on_event(event(generation, played_frames=3))
        self.assertEqual(b.metrics['playout_receipts'], 1)

    def test_legacy_peer_unsolicited_cancelled_and_prior_clause_ack_are_ignored(self):
        b = self.session()
        b.playback.playing = True
        b.playback.playout_probe.begin(b.generation, 1)
        b.playback.playout_probe.reserve(b.generation, 1, 0, np.array([0, 0, 1, 1], dtype=np.int16))
        b.diagnostics.begin('text', b.generation)
        b.on_event(event(b.generation))
        self.assertNotIn('playout_receipts', b.metrics)
        b.playback.set_playout_capability(True)
        b.playback.playout_probe.begin(b.generation, 2)
        b.on_event(event(b.generation))
        self.assertNotIn('playout_receipts', b.metrics)
        b.cancel('fixture')
        b.on_event(event(b.generation - 1, 2))
        self.assertNotIn('playout_receipts', b.metrics)
        b.playback.set_playout_capability(True)
        b.playback.clear_transport_pause()
        self.assertFalse(b.playback.device_can_report_playout)

    def test_handshake_old_peer_clears_capability_without_starting_capture(self):
        b = self.session()
        b.session.active = False
        b.event = lambda *a, **k: None
        b.on_event({'event': 'device_ready', 'playout_receipt_version': 1})
        self.assertTrue(b.playback.device_can_report_playout)
        b.on_event({'event': 'device_ready'})
        self.assertFalse(b.playback.device_can_report_playout)
        self.assertFalse(b.session.active)

    def test_diagnostic_runs_accept_progress_but_never_accept_acoustic_claim(self):
        book = TraceBook()
        run = DiagnosticRun()
        run.ingest(book.report())
        book.begin('text', 10)
        book.mark(10, 'playout_ack')
        run.ingest(book.report())
        self.assertIn('playout_ack', run.report()['requests'][0]['stages'])
        self.assertFalse(run.report()['playout_ack_is_audible_onset'])
        snapshot = book.report()
        snapshot['playout_ack_is_audible_onset'] = True
        with self.assertRaises(ValueError):
            run.ingest(snapshot)

    def test_confirmed_clauses_accumulate_then_stay_fixed_at_terminal_outcome(self):
        book = TraceBook()
        run = DiagnosticRun()
        run.ingest(book.report())
        book.begin('announcement', 1)
        ack = book.mark(1, 'playout_ack')
        book.playback_done(1, confirmed_ack_ms=ack)
        run.ingest(book.report())
        book.playback_done(1, confirmed_ack_ms=book.mark(1, 'playout_ack'))
        book.finish(1, 'completed')
        run.ingest(book.report())
        for invalid in (None, True, 1, 3, 65536):
            with self.subTest(invalid=invalid):
                snapshot = book.report()
                row = snapshot['requests'][0]
                if invalid is None:
                    del row['confirmed_playback_clauses']
                else:
                    row['confirmed_playback_clauses'] = invalid
                with self.assertRaises(ValueError):
                    run.ingest(snapshot)
        book.playback_done(1, confirmed_ack_ms=ack)
        self.assertEqual(book.report()['requests'][0]['confirmed_playback_clauses'], 2)
        self.assertEqual(run.report()['requests'][0]['confirmed_playback_clauses'], 2)

    def test_cancel_during_probe_publication_cannot_leave_old_identity(self):
        b = self.session()
        b.playback.set_playout_capability(True)
        b.send = lambda packet: None
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        original = b.playback.playout_probe.begin
        errors = []
        generation = b.generation

        def begin(*args):
            entered.set()
            if not release.wait(1):
                raise RuntimeError('Probe publication barrier timed out')
            original(*args)

        def play():
            try:
                b.play(np.ones(16, dtype=np.int16), 'synthetic', generation)
            except Exception as error:
                errors.append(error)

        def cancel():
            try:
                b.cancel('publication_test')
            except Exception as error:
                errors.append(error)
            finally:
                cancelled.set()

        with patch.object(b.playback.playout_probe, 'begin', side_effect=begin):
            output = threading.Thread(target=play)
            owner = threading.Thread(target=cancel)
            output.start()
            self.assertTrue(entered.wait(1))
            owner.start()
            cancelled.wait(0.05)
            release.set()
            output.join(2)
            owner.join(2)
        self.assertFalse(output.is_alive())
        self.assertFalse(owner.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(b.playback.playout_probe.identity)
        self.assertFalse(b.playback.playing)

    def test_actual_java_probe_handles_short_writes_silence_pause_and_drain(self):
        java = java_bin()
        source = (
            ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/PlayoutProbe.java'
        )
        code = """package com.happyhome.cc8assistant;
public class ProbeCheck {
 static void no(PlayoutProbe.Observation o){if(o!=null)throw new AssertionError("unexpected observation");}
 public static void main(String[] args){
  if(!PlayoutProbe.requested(1)||!PlayoutProbe.requested(1L)||PlayoutProbe.requested(true)
    ||PlayoutProbe.requested("1")||PlayoutProbe.requested(1.0)||PlayoutProbe.requested(2))throw new AssertionError("capability type");
  PlayoutProbe p=new PlayoutProbe();
  no(p.observe(1,10,true,false,true));
  p.accepted(new short[]{9,0,0,9},1,2);
  no(p.observe(2,10,true,false,true));
  p.accepted(new short[]{0,7,8},0,2);
  no(p.observe(3,10,true,false,true));
  no(p.observe(4,10,false,false,true));
  no(p.observe(4,10,true,true,true));
  no(p.observe(4,10,true,false,false));
  no(p.observe(5,10,true,false,true));
  PlayoutProbe.Observation seen=p.observe(4,123,true,false,true);
  if(seen==null||seen.firstSignal!=3||seen.played!=4||seen.accepted!=4||seen.nanos!=123||p.pending())throw new AssertionError();
  no(p.observe(4,124,true,false,true));
  PlayoutProbe silent=new PlayoutProbe();silent.accepted(new short[160],0,160);no(silent.observe(160,1,true,false,true));
  PlayoutProbe drained=new PlayoutProbe();drained.accepted(new short[]{1},0,1);
  if(drained.observe(1,1,true,false,true)==null)throw new AssertionError("short final buffer missed");
  try{drained.accepted(new short[1],1,1);throw new AssertionError();}catch(IllegalArgumentException expected){}
 }
}"""
        with tempfile.TemporaryDirectory() as folder:
            test = Path(folder) / 'ProbeCheck.java'
            test.write_text(code)
            compiled = subprocess.run(
                [str(java / 'javac'), '-encoding', 'UTF-8', '-d', folder, str(source), str(test)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run(
                [str(java / 'java'), '-cp', folder, 'com.happyhome.cc8assistant.ProbeCheck'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
