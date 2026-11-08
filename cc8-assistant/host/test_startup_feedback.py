"""Startup rejection reports only its own failure and leaves the device route usable."""

import json
import queue
import unittest
from unittest.mock import patch

from diagnostics import error_kind
from failure_feedback import feedback
import test_text_admission as fixture
import test_error_completion_feedback as java_fixture


class StartupFeedbackTests(unittest.TestCase):
    def bridge(self, active=False):
        return fixture.TextAdmissionTests(methodName='runTest').bridge(active=active)

    def latest(self, bridge):
        bridge.on_event({'event': 'text_request', 'text': '最新指令'})
        fixture.TextAdmissionTests(methodName='runTest').work_latest(bridge, '最新指令')

    def errors(self, bridge):
        return [f for n, f in bridge.events if n == 'status' and f.get('phase') == 'error']

    def one_error(self, bridge):
        errors = self.errors(bridge)
        self.assertEqual(len(errors), 1)
        return errors[0]

    def test_device_queue_failures_are_classified_and_next_request_runs(self):
        for active in (False, True):
            for error in (queue.Full(fixture.SECRET), OSError(fixture.SECRET)):
                with self.subTest(active=active, error=type(error).__name__):
                    bridge = self.bridge(active)
                    generation = bridge.generation
                    with patch.object(bridge, 'enqueue_response', side_effect=error):
                        try:
                            bridge.on_event({'event': 'text_request', 'text': '固定测试指令'})
                        except Exception:
                            self.fail('Local queue failure escaped into the device connection loop')
                    observed = self.one_error(bridge)
                    self.assertEqual(observed['generation'], generation + 2)
                    self.assertEqual(
                        observed['message'],
                        '本次文字指令未启动。' + feedback('runtime', error_kind(error)).message,
                    )
                    self.assertFalse(any(n == 'workspace_result' for n, _ in bridge.events))
                    self.assertEqual(bridge.brain.calls, 0)
                    self.assertIsNone(bridge.session.text_generation)
                    self.assertEqual(bridge.session.active, active)
                    self.assertNotIn(fixture.SECRET, json.dumps((bridge.events, bridge.logs)))
                    self.latest(bridge)

    def test_device_text_begin_delivery_failure_uses_transport_guidance(self):
        bridge = self.bridge()
        original = bridge.event

        def event(name, **fields):
            if name == 'text_begin':
                raise OSError(fixture.SECRET)
            original(name, **fields)

        with patch.object(bridge, 'event', side_effect=event):
            try:
                bridge.on_event({'event': 'text_request', 'text': '固定测试指令'})
            except OSError:
                self.fail('Handled startup delivery error escaped the device router')
        observed = self.one_error(bridge)
        self.assertEqual(
            observed['message'],
            '本次文字指令未启动。' + feedback('transport', 'transport_or_io').message,
        )
        self.latest(bridge)

    def test_initial_provider_cancel_failure_has_guidance_without_starting_work(self):
        bridge = self.bridge()
        with patch.object(bridge.brain, 'cancel', side_effect=RuntimeError(fixture.SECRET)):
            bridge.on_event({'event': 'text_request', 'text': '固定测试指令'})
        observed = self.one_error(bridge)
        self.assertEqual(observed['generation'], bridge.generation)
        self.assertEqual(
            observed['message'],
            '本次文字指令未启动。' + feedback('runtime', 'provider_or_runtime').message,
        )
        self.assertTrue(bridge.response.queue.empty())
        self.assertEqual(bridge.brain.calls, 0)
        self.assertEqual(bridge.diagnostics_report()['requests'][-1]['outcome'], 'failed')
        self.latest(bridge)

    def test_cleanup_replacement_does_not_receive_old_error_or_rejection(self):
        bridge = self.bridge()
        original = bridge.diagnostics.failure

        def failure(generation, stage, error):
            original(generation, stage, error)
            bridge.submit_text('最新指令')

        enqueue = bridge.enqueue_response

        def offer(job):
            if job.text == '旧指令':
                raise OSError(fixture.SECRET)
            enqueue(job)

        with (
            patch.object(bridge, 'enqueue_response', side_effect=offer),
            patch.object(bridge.diagnostics, 'failure', side_effect=failure),
        ):
            try:
                bridge.on_event({'event': 'text_request', 'text': '旧指令'})
            except OSError:
                self.fail('Old submission failure escaped after admitting newer work')
        self.assertEqual(self.errors(bridge), [])
        self.assertFalse(any(n == 'workspace_result' for n, _ in bridge.events))
        fixture.TextAdmissionTests(methodName='runTest').work_latest(bridge, '最新指令')

    def test_feedback_delivery_failure_retains_original_exception(self):
        bridge = self.bridge()
        primary = queue.Full(fixture.SECRET)
        original = bridge.event

        def event(name, **fields):
            if name == 'status' and fields.get('phase') == 'error':
                raise RuntimeError('secondary-' + fixture.SECRET)
            original(name, **fields)

        with (
            patch.object(bridge, 'enqueue_response', side_effect=primary),
            patch.object(bridge, 'event', side_effect=event),
        ):
            with self.assertRaises(queue.Full) as caught:
                bridge.submit_text('固定测试指令')
        self.assertIs(caught.exception, primary)
        self.assertIsNone(bridge.session.text_generation)
        self.assertNotIn(fixture.SECRET, json.dumps(bridge.logs))

    def test_initial_cancel_diagnostic_callback_cannot_mark_new_request_failed(self):
        bridge = self.bridge()
        begin = bridge.diagnostics.begin
        replaced = []

        def diagnostic(origin, generation=None):
            result = begin(origin, generation)
            if not replaced:
                replaced.append(True)
                bridge.submit_text('最新指令')
            return result

        with (
            patch.object(bridge.brain, 'cancel', side_effect=[RuntimeError(fixture.SECRET), None]),
            patch.object(bridge.diagnostics, 'begin', side_effect=diagnostic),
        ):
            bridge.on_event({'event': 'text_request', 'text': '旧指令'})
        self.assertEqual(self.errors(bridge), [])
        self.assertEqual(bridge.session.text_generation, bridge.generation)
        row = bridge.diagnostics_report()['requests'][-1]
        self.assertIsNone(row['outcome'])
        self.assertIsNone(row['error'])
        fixture.TextAdmissionTests(methodName='runTest').work_latest(bridge, '最新指令')

    def test_announcement_prepare_failure_is_visible_and_next_text_runs(self):
        bridge = self.bridge()

        def prepare(_):
            raise OSError(fixture.SECRET)

        with self.assertRaises(OSError):
            bridge.announcements.announce('固定测试播报', prepare=prepare)
        observed = self.one_error(bridge)
        self.assertEqual(observed['generation'], bridge.generation)
        self.assertEqual(
            observed['message'],
            '本次播报未启动。' + feedback('runtime', 'transport_or_io').message,
        )
        self.assertEqual(bridge.workspace.tasks, [])
        self.latest(bridge)

    def test_announcement_initial_cancel_failure_has_owned_diagnostic_and_guidance(self):
        bridge = self.bridge()
        with patch.object(bridge.brain, 'cancel', side_effect=RuntimeError(fixture.SECRET)):
            with self.assertRaises(RuntimeError):
                bridge.announce('旧播报')
        observed = self.one_error(bridge)
        self.assertEqual(observed['generation'], bridge.generation)
        self.assertEqual(
            observed['message'],
            '本次播报未启动。' + feedback('runtime', 'provider_or_runtime').message,
        )
        self.assertEqual(bridge.diagnostics_report()['requests'][-1]['outcome'], 'failed')
        self.assertIsNone(bridge.announcements.generation)
        self.assertEqual(bridge.brain.calls, 0)
        self.latest(bridge)

    def test_readiness_rejection_keeps_existing_input_notice_and_owner(self):
        bridge = self.bridge()
        generation = bridge.generation
        bridge.session.text_generation = generation
        bridge.calibration.active = True
        bridge.on_event({'event': 'text_request', 'text': '固定测试指令'})
        self.assertEqual(bridge.generation, generation)
        self.assertEqual(bridge.session.text_generation, generation)
        self.assertEqual(self.errors(bridge), [])
        self.assertEqual([n for n, _ in bridge.events], ['workspace_result'])

    def test_real_device_branches_admit_only_current_typed_startup_failure(self):
        java_fixture.DeviceFeedbackTests(methodName='runTest').compile_and_run(
            """
            c.cancel(10,"","");
            c.suppliedStartFailure="true";
            c.status(10,"error","bad marker");
            yes(!"error".equals(c.listener.phase),"string marker admitted");
            c.suppliedStartFailure=Boolean.TRUE;
            c.status(11,"error","future error");
            yes(!"error".equals(c.listener.phase),"future unbound error admitted");
            c.status(10,"error","startup failed");
            yes("startup failed".equals(c.listener.message),"startup failure invisible");
            yes(!c.listener.playing&&!c.recording,"startup error opened resources");
            c.status(10,"error","duplicate");
            yes("startup failed".equals(c.listener.message),"duplicate error reopened state");
            c.cancel(12,"","");c.text_begin(12,"","");
            c.status(10,"error","stale");
            yes(!"stale".equals(c.listener.message),"old error replaced new request");
            c.cancel(13,"","");c.foreground=false;
            c.status(13,"error","background");
            yes(!"background".equals(c.listener.message),"background error published");
            """
        )
