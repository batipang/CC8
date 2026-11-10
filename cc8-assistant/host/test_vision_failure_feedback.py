"""Fixed camera frames and real controllers expose classified vision failures."""

import json
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from brain_router import BrainRouter
from bridge import Bridge
from diagnostic_run import DiagnosticRun
from dot_client import DotError
from failure_feedback import feedback
from operation_failure import OperationFailure
from test_failure_feedback import Brain, SECRET
from test_vision import events
from tools.java_toolchain import java_bin
from vision import CameraInbox, VisionError

ROOT = Path(__file__).resolve().parents[2]


class VisionFailureTests(unittest.TestCase):
    def bridge(self, brain=None):
        with patch('threading.Thread.start'):
            b = Bridge(None, BrainRouter(Brain(), brain or Brain()))
        b.session.connection = object()
        b.events, b.logs = [], []
        b.save_status = lambda: None
        b.event = lambda name, **fields: b.events.append((name, fields))
        b.log = lambda name, **fields: b.logs.append((name, fields))
        return b

    def capture_events(self, b, reason=None):
        output = b.event

        def event(name, **fields):
            output(name, **fields)
            if name == 'camera_capture':
                capture = b.visual.inbox.pending
                if reason:
                    b.on_event(
                        {
                            'event': 'camera_error',
                            'request_id': capture.ident,
                            'vision_generation': capture.generation,
                            'error': reason,
                        }
                    )
                else:
                    for value in events(capture):
                        b.on_event(value)

        b.event = event

    def run_visual(self, b, nonce='a' * 32):
        b.begin_visual('固定公开物品检查', 'button', nonce)
        job = b.response.queue.get_nowait()
        with patch.object(b.response.queue, 'get', side_effect=[job, SystemExit]):
            with self.assertRaises(SystemExit):
                b.response.work()
        return job

    def error(self, b, stage, kind):
        rows = [
            (i, data)
            for i, (event, data) in enumerate(b.events)
            if event == 'status' and data.get('phase') == 'error'
        ]
        self.assertEqual(len(rows), 1, 'Vision failed without classified error UI')
        index, data = rows[0]
        self.assertEqual(data['message'], feedback(stage, kind).message)
        done = [i for i, (event, _) in enumerate(b.events) if event == 'vision_done']
        self.assertEqual(len(done), 1)
        self.assertLess(index, done[0])
        report = b.diagnostics.report()
        self.assertEqual(len(report['requests']), 1)
        self.assertEqual(report['requests'][0]['error'], {'stage': stage, 'kind': kind})
        self.assertEqual(report['requests'][0]['outcome'], 'failed')
        self.assertIsNone(b.visual.inbox.pending)
        self.assertIsNone(b.visual.generation)
        self.assertFalse(b.session.active or b.response.busy)
        self.assertNotIn(SECRET, json.dumps([b.events, b.logs, b.metrics, report]))

    def test_dot_vision_error_matrix_is_not_a_generic_success_or_fake_dot_result(self):
        for kind in (
            'timeout',
            'rate_limit',
            'quota',
            'credential_or_permission',
            'transport_or_io',
            'approval_declined',
        ):
            for _ in range(3):
                with self.subTest(kind=kind):
                    dot = Brain(DotError(SECRET, kind=kind))
                    b = self.bridge(dot)
                    self.capture_events(b)
                    self.run_visual(b)
                    self.error(b, 'brain', kind)
                    self.assertEqual(dot.calls, 1)
                    self.assertFalse(any(n == 'dot_result' for n, _ in b.events))
                    self.assertEqual(b.workspace.tasks[-1]['status'], 'failed')

    def test_tts_failure_keeps_real_completed_task_and_actual_dot_result(self):
        dot = Brain(receipt=True)
        b = self.bridge(dot)
        self.capture_events(b)
        calls = []

        def speak(*args):
            calls.append(1)
            raise OperationFailure(SECRET, stage='tts', kind='timeout')

        b.speak = speak
        self.run_visual(b)
        self.error(b, 'tts', 'timeout')
        self.assertEqual((dot.calls, len(calls)), (1, 1))
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        results = [d['text'] for n, d in b.events if n == 'dot_result']
        self.assertEqual(results, ['真实任务完成'])

    def test_camera_error_codes_are_classified_and_unknown_payload_never_retained(self):
        for reason, kind in [
            ('permission_required', 'credential_or_permission'),
            ('camera_timeout', 'timeout'),
            ('image_transfer_failed', 'transport_or_io'),
            ('camera_unavailable', 'provider_or_runtime'),
            ('private_provider_url_token', 'provider_or_runtime'),
        ]:
            for _ in range(3):
                with self.subTest(reason=reason):
                    dot = Brain()
                    b = self.bridge(dot)
                    collector = DiagnosticRun()
                    collector.ingest(b.diagnostics.report())
                    self.capture_events(b, reason)
                    self.run_visual(b)
                    self.error(b, 'vision', kind)
                    self.assertEqual(dot.calls, 0)
                    self.assertEqual(b.workspace.tasks, [])
                    self.assertFalse(any(n == 'dot_result' for n, _ in b.events))
                    self.assertNotIn(
                        'private_provider_url_token', json.dumps([b.events, b.logs, b.metrics])
                    )
                    collector.ingest(b.diagnostics.report())
                    self.assertEqual(collector.report()['summary']['samples'], 1)

    def test_desktop_photo_failure_is_safe_terminal_and_releases_owner_waiter(self):
        b = self.bridge()
        self.capture_events(b, 'permission_required')
        b.visual.challenge = 'c' * 32
        b.transport_hub = SimpleNamespace(pairing=SimpleNamespace(token='a' * 64))
        with patch('threading.Thread.start'):
            job = b.request_snapshot()
        capture = b.visual.inbox.pending
        b.visual.capture_snapshot(job, capture)
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.result, 'failed')
        self.assertEqual(job.error, 'permission_required')
        self.assertIsNone(job.image)
        self.assertIsNone(b.visual.snapshot_job)
        self.error(b, 'vision', 'credential_or_permission')

    def test_camera_failures_give_device_guidance_and_require_an_explicit_new_request(self):
        cases = (
            ('permission_denied', 'credential_or_permission', 'CC8 的应用权限'),
            ('camera_timeout', 'timeout', '等待画面超时'),
            ('image_transfer_failed', 'transport_or_io', '画面传输中断'),
            ('camera_unavailable', 'provider_or_runtime', '关闭占用摄像头的页面'),
        )
        for reason, kind, instruction in cases:
            for repeat in range(3):
                with self.subTest(reason=reason, repeat=repeat):
                    dot = Brain(receipt=True)
                    b = self.bridge(dot)
                    output = b.event
                    b.speak = lambda *args: True
                    self.capture_events(b, reason)
                    failed = self.run_visual(b)
                    self.error(b, 'vision', kind)
                    messages = [
                        row['message']
                        for name, row in b.events
                        if name == 'status' and row.get('phase') == 'error'
                    ]
                    self.assertIn(instruction, messages[0])
                    self.assertNotIn('凭证', messages[0])
                    self.assertEqual(dot.calls, 0)
                    self.assertEqual(b.workspace.tasks, [])
                    self.assertTrue(b.response.queue.empty())
                    old_capture = next(row for name, row in b.events if name == 'camera_capture')

                    def next_capture(name, **fields):
                        output(name, **fields)
                        if name != 'camera_capture':
                            return
                        b.on_event(
                            {
                                'event': 'camera_error',
                                'request_id': old_capture['request_id'],
                                'vision_generation': failed.generation,
                                'error': reason,
                            }
                        )
                        for event in events(b.visual.inbox.pending):
                            b.on_event(event)

                    b.event = next_capture
                    recovered = self.run_visual(b, 'b' * 32)
                    self.assertGreater(recovered.generation, failed.generation)
                    self.assertEqual(dot.calls, 1)
                    self.assertEqual([task['status'] for task in b.workspace.tasks], ['completed'])
                    done = [row['result'] for name, row in b.events if name == 'vision_done']
                    self.assertEqual(done, ['failed', 'completed'])
                    self.assertEqual(
                        sum(
                            name == 'status' and row.get('phase') == 'error'
                            for name, row in b.events
                        ),
                        1,
                    )
                    reports = b.diagnostics.report()['requests']
                    self.assertEqual([row['outcome'] for row in reports], ['failed', 'completed'])
                    self.assertIsNone(reports[-1]['error'])
                    self.assertIsNone(b.visual.inbox.pending)
                    self.assertIsNone(b.visual.generation)
                    self.assertTrue(b.response.queue.empty())

    def test_capture_timeout_is_distinct_from_owner_cancellation(self):
        for cancelled in (False, True):
            inbox = CameraInbox()
            capture = inbox.begin(1)
            capture.expires = 0
            with self.assertRaises(VisionError) as caught:
                inbox.wait(capture, lambda: not cancelled)
            self.assertEqual(str(caught.exception), 'cancelled' if cancelled else 'capture_timeout')
            self.assertEqual(getattr(caught.exception, 'stage', None), 'vision')
            self.assertEqual(
                getattr(caught.exception, 'kind', None),
                'provider_or_runtime' if cancelled else 'timeout',
            )

    def test_vision_error_never_exposes_arbitrary_exception_text(self):
        for unsafe in (SECRET, 'private_provider_url_token', None, {'error': 'secret'}):
            error = VisionError(unsafe)
            self.assertEqual(str(error), 'camera_failed')
            self.assertEqual(getattr(error, 'code', None), 'camera_failed')

    def test_error_publication_and_finalization_cannot_cross_new_picture_admission(self):
        for checkpoint in ('failure', 'done'):
            with self.subTest(checkpoint=checkpoint):
                dot = Brain(DotError(SECRET, kind='timeout'))
                b = self.bridge(dot)
                self.capture_events(b)
                b.begin_visual('固定公开检查', 'button', 'a' * 32)
                job = b.response.queue.get_nowait()
                entered, release, owner_done = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )
                failures = []
                if checkpoint == 'failure':
                    original = b.visual.hooks.valid
                    armed = [True]

                    def valid(generation):
                        value = original(generation)
                        if value and dot.calls and armed[0]:
                            armed[0] = False
                            entered.set()
                            if not release.wait(2):
                                raise RuntimeError('vision error barrier')
                        return value

                    b.visual.hooks = replace(b.visual.hooks, valid=valid)
                else:
                    original = b.event

                    def output(name, **fields):
                        if name == 'vision_done':
                            entered.set()
                            if not release.wait(2):
                                raise RuntimeError('vision done barrier')
                        original(name, **fields)

                    b.event = output

                def work():
                    try:
                        b.answer_visual(job)
                    except BaseException as error:
                        failures.append(type(error).__name__)

                def owner():
                    try:
                        b.begin_visual('新的固定公开检查', 'button', 'b' * 32)
                        owner_done.set()
                    except BaseException as error:
                        failures.append(type(error).__name__)

                first, second = (
                    threading.Thread(target=work, daemon=True),
                    threading.Thread(target=owner, daemon=True),
                )
                first.start()
                try:
                    self.assertTrue(entered.wait(1))
                    second.start()
                    self.assertFalse(owner_done.wait(0.1))
                finally:
                    release.set()
                    first.join(2)
                    if second.ident is not None:
                        second.join(2)
                self.assertEqual(failures, [])
                self.assertTrue(owner_done.is_set())
                self.assertFalse(first.is_alive() or second.is_alive())
                self.assertEqual(b.visual.generation, b.generation)
                last = max(i for i, (event, _) in enumerate(b.events) if event == 'vision_begin')
                self.assertFalse(
                    any(
                        event == 'status' and d.get('phase') == 'error'
                        for event, d in b.events[last + 1 :]
                    )
                )


class VisionUiTests(unittest.TestCase):
    def test_actual_main_activity_completion_releases_camera_without_hiding_error(self):
        source = (
            ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/MainActivity.java'
        ).read_text()
        legacy = 'public void vision(boolean busy, String message)'
        signature = 'public void vision(boolean busy, String message, boolean preserveError)'
        if signature not in source:
            signature = legacy
        start = source.index(signature)
        end = source.index('{', start) + 1
        depth = 1
        while depth:
            depth += (source[end] == '{') - (source[end] == '}')
            end += 1
        method = source[start:end]
        if signature == legacy:
            # Arity adapter only: the old method body still runs unchanged.
            method += 'void vision(boolean b,String m,boolean preserve){vision(b,m);}'
        harness = (
            """class VisionUi {
 String phase="error",message="fixed recovery guidance";boolean active;
 static class Eyes {int cancelled;void cancel(){cancelled++;}}
 static class Button {String text;void setText(String t){text=t;}void setContentDescription(String t){}}
 Eyes eyes=new Eyes();Button visionButton=new Button();
 static class Workflows {int closed;void close(){closed++;}} Workflows workflows=new Workflows();
 void postIdentityUi(Runnable call){call.run();}
 void state(String p,String m){phase=p;message=m;}
 """
            + method
            + """ public static void main(String[]args){
 VisionUi c=new VisionUi();c.vision(false,"finished",true);
 if(!c.phase.equals("error")||!c.message.equals("fixed recovery guidance"))throw new AssertionError("vision cleanup hid error");
 if(c.eyes.cancelled!=1||!c.visionButton.text.equals("看一看"))throw new AssertionError("camera controls stuck");
 c.vision(false,"new unavailable message",false);
 if(!c.message.equals("new unavailable message"))throw new AssertionError("old error hid new unavailable message");
 c.vision(true,"new picture",false);if(!c.phase.equals("thinking"))throw new AssertionError("new picture kept old error");
 if(c.workflows.closed!=1)throw new AssertionError("workflow modal covered live camera preview");
 c.vision(false,"done",false);if(!c.phase.equals("idle"))throw new AssertionError("normal cleanup failed");}}
"""
        )
        java = java_bin()
        with tempfile.TemporaryDirectory() as temporary:
            test = Path(temporary) / 'VisionUi.java'
            test.write_text(harness)
            subprocess.run(
                [str(java / 'javac'), '-encoding', 'UTF-8', '-d', temporary, str(test)],
                capture_output=True,
                check=True,
                timeout=30,
            )
            result = subprocess.run(
                [str(java / 'java'), '-cp', temporary, 'VisionUi'],
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
