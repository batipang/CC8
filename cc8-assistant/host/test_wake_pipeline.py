"""Real endpoint/recognition integration with synthetic PCM, local cue and fake APIs."""

import threading
import time
import unittest
from unittest.mock import Mock, patch
import numpy as np
from bridge import Bridge
from dsp import FRAME, RATE
from protocol import MIC, Packet
from qa_speech import SCENARIO_QUESTIONS, tool_sample_request


class Channel:
    def __init__(self, kind='usb'):
        self.kind = kind

    def sendall(self, data):
        pass


class LocalCue:
    def __init__(self, word):
        self.word = word
        self.calls = 0
        self.resets = []

    def feed(self, pcm):
        self.calls += 1
        return self.word if self.calls == 3 else ''

    def reset(self, *, clear_metrics=True):
        self.resets.append(clear_metrics)


class UtteranceVAD:
    def __init__(self):
        self.talking = False
        self.frames = []

    def reset(self):
        self.talking = False
        self.frames.clear()

    def feed(self, pcm):
        self.frames.append(pcm.copy())
        count = len(self.frames)
        self.talking = count < 5
        return count == 1, np.concatenate(self.frames) if count == 5 else None, -25


class Echo:
    def process(self, mic, reference):
        return mic.copy()

    def close(self):
        pass


class WakePipelineTests(unittest.TestCase):
    def session(self, cue='', text='大黄蜂，常州属于哪个省'):
        asr, answered = threading.Event(), threading.Event()
        audio, requests, events = [], [], []

        class Speech:
            def recognize(self, pcm):
                audio.append(pcm.copy())
                asr.set()
                return text

        class Brain:
            last_metrics = {}

            def cancel(self):
                pass

            def clauses(self, question, history, current, on_web):
                requests.append(question)
                answered.set()
                yield '江苏省。'

        detector = LocalCue(cue)
        b = Bridge(Speech(), Brain(), wake_detector=detector, wake_window_seconds=15)
        b.session.connection = Channel()
        b.session.active = True
        b.session.wake = True
        b.session.awake = False
        b.wake.reset('mode')
        b.capture.echo.aec = Echo()
        b.capture.endpoint.vad = UtteranceVAD()
        b.event = lambda name, **fields: events.append((name, fields))
        b.save_status = lambda: None
        b.speak = lambda *a, **k: True
        b.log = lambda *a, **k: None
        return b, detector, audio, requests, events, asr, answered

    def frames(self, b, first=1, last=5):
        for index in range(first, last + 1):
            pcm = np.full(FRAME, index * 400, dtype='<i2')
            b.process_microphone(
                Packet(
                    MIC,
                    b.session.epoch,
                    index,
                    0,
                    10_000_000_000 + index * FRAME * 1_000_000_000 // RATE,
                    pcm.tobytes(),
                )
            )

    def queue_frames(self, b, first, last):
        for index in range(first, last + 1):
            pcm = np.full(FRAME, 800, dtype='<i2')
            b.on_packet(
                Packet(
                    MIC,
                    b.session.epoch,
                    index,
                    0,
                    10_000_000_000 + index * 10_000_000,
                    pcm.tobytes(),
                )
            )

    def test_capture_reuses_stop_aec_for_alternate_wake_only_outside_playback(self):
        b, detector, _, _, _, _, _ = self.session()
        detector.feed = Mock(return_value='')
        b.capture.endpoint.fast_stop = Mock(feed=Mock(return_value=''))
        alternate = np.full(FRAME, 0.25)
        b.capture.echo.stop_aec = Mock(process=Mock(return_value=alternate))

        self.frames(b, first=1, last=1)
        self.assertIs(detector.feed.call_args.kwargs['alternate_pcm'], alternate)

        b.playback.playing = True
        self.frames(b, first=2, last=2)
        self.assertNotIn('alternate_pcm', detector.feed.call_args.kwargs)

    def test_buffered_capture_from_before_waiting_cannot_wake_but_new_capture_can(self):
        for transition in ('sleep', 'wake_mode'):
            with self.subTest(transition=transition):
                b, detector, _, requests, _, asr, _ = self.session('Bumblebee')
                b.session.awake = True
                if transition == 'wake_mode':
                    b.session.wake = False
                b.capture.echo.aec.process = Mock(wraps=b.capture.echo.aec.process)
                self.queue_frames(b, 1, 4)
                if transition == 'sleep':
                    b.set_awake(False)
                else:
                    b.on_event({'event': 'settings', 'wake_mode': True})
                self.queue_frames(b, 5, 12)
                self.assertEqual(b.capture.echo.aec.process.call_count, 4)
                self.assertEqual(detector.calls, 0)
                self.assertFalse(b.session.awake)
                self.assertFalse(asr.is_set())
                self.assertEqual(requests, [])
                self.queue_frames(b, 13, 15)
                self.assertEqual(detector.calls, 3)
                self.assertTrue(b.session.awake)

    def test_waiting_cutoff_is_rechecked_when_endpoint_is_already_processing(self):
        b, detector, _, _, _, _, _ = self.session()
        b.session.awake = True
        detector.feed = Mock(return_value='大黄蜂')
        feed = b.wake.feed

        def enter_waiting(pcm, generation, **fields):
            b.set_awake(False)
            return feed(pcm, generation, **fields)

        b.wake.feed = enter_waiting
        self.queue_frames(b, 1, 9)
        detector.feed.assert_not_called()
        self.assertEqual(b.capture.endpoint.vad.frames, [])
        self.assertFalse(b.session.awake)

    def test_asleep_utterance_never_reaches_asr_or_brain(self):
        b, detector, audio, requests, events, asr, _ = self.session()
        self.frames(b)
        self.assertFalse(b.session.awake)
        self.assertEqual(audio, [])
        self.assertEqual(requests, [])
        self.assertFalse(asr.is_set())
        self.assertTrue(b.recognition.queue.empty())
        self.assertEqual(b.metrics['local_wake_utterances_withheld'], 1)
        report = b.diagnostics.report()
        self.assertEqual(report['requests'][-1]['outcome'], 'rejected')
        self.assertNotIn('asr_start', report['requests'][-1]['stages'])
        self.assertFalse(any(name == 'transcript' for name, _ in events))

    def test_same_utterance_keeps_every_frame_and_routes_question_after_local_name(self):
        for cue, text in [
            ('大黄蜂', '大黄蜂，常州属于哪个省'),
            ('Bumblebee', 'Bumblebee，常州属于哪个省'),
        ]:
            with self.subTest(cue=cue):
                b, detector, audio, requests, events, asr, answered = self.session(cue, text)
                self.frames(b, 1, 2)
                self.assertFalse(asr.is_set())
                self.assertFalse(b.session.awake)
                self.frames(b, 3, 5)
                self.assertTrue(answered.wait(1))
                self.assertEqual(requests, ['常州属于哪个省'])
                self.assertEqual(len(audio), 1)
                expected = np.concatenate(
                    [np.full(FRAME, index * 400 / 32768, dtype=np.float32) for index in range(1, 6)]
                )
                np.testing.assert_array_equal(audio[0], expected)
                self.assertTrue(b.session.awake)
                self.assertEqual(b.metrics['local_wake_count'], 1)
                self.assertEqual(
                    b.metrics.get('local_wake_english_count', 0), int(cue == 'Bumblebee')
                )
                self.assertTrue(
                    any(
                        name == 'transcript' and fields['text'] == '常州属于哪个省'
                        for name, fields in events
                    )
                )
                row = b.diagnostics.report()['requests'][-1]
                self.assertIn('wake_detected', row['stages'])
                self.assertIn('asr_start', row['stages'])

    def test_english_name_in_either_recent_clause_cannot_wake_or_reach_asr(self):
        for current, previous in (
            ('Bumblebee', 'This is a new sentence.'),
            ('Welcome', 'Bumblebee'),
            ('bee', 'Bumble'),
        ):
            with self.subTest(current=current, previous=previous):
                b, _, audio, requests, _, asr, _ = self.session('Bumblebee')
                b.playback.playing = True
                b.playback.last_spoken = current
                b.playback.previous_spoken = previous
                b.playback.previous_spoken_until = time.monotonic() + 1
                self.frames(b)
                self.assertFalse(b.session.awake)
                self.assertFalse(asr.is_set())
                self.assertEqual(audio, [])
                self.assertEqual(requests, [])
                self.assertEqual(b.metrics['wake_render_rejections'], 1)
                self.assertEqual(b.metrics.get('local_wake_english_count', 0), 0)

    def test_recent_playback_end_guards_wake_when_render_reference_is_missing(self):
        for cue in ('大黄蜂', 'Bumblebee'):
            with self.subTest(cue=cue):
                b, _, audio, requests, _, asr, _ = self.session(cue)
                b.playback.last_spoken = '我的名字是 ' + cue
                b.playback.last_audio_end = time.monotonic()
                self.assertIsNone(b.capture.endpoint.last_capture_render_ns)
                self.frames(b)
                self.assertIsNone(b.capture.endpoint.last_capture_render_ns)
                self.assertFalse(b.session.awake)
                self.assertFalse(asr.is_set())
                self.assertEqual(audio, [])
                self.assertEqual(requests, [])
                self.assertEqual(b.metrics['wake_render_rejections'], 1)

    def test_unrelated_or_expired_playback_text_does_not_block_a_new_name(self):
        for spoken, age in (('现在是下午三点', 0), ('Bumblebee', 3.5)):
            with self.subTest(spoken=spoken, age=age):
                b, _, _, requests, _, _, answered = self.session(
                    'Bumblebee', 'Bumblebee，常州属于哪个省'
                )
                b.playback.last_spoken = spoken
                b.playback.last_audio_end = time.monotonic() - age
                self.frames(b)
                self.assertTrue(answered.wait(1))
                self.assertEqual(requests, ['常州属于哪个省'])
                self.assertNotIn('wake_render_rejections', b.metrics)

    def test_valid_queued_job_cannot_bypass_sleep_even_with_ready_detector(self):
        b, _, audio, requests, _, asr, _ = self.session('大黄蜂')
        finished = threading.Event()
        original = b.diagnostics.finish

        def finish(key, outcome):
            original(key, outcome)
            if outcome == 'rejected':
                finished.set()

        b.diagnostics.finish = finish
        b.enqueue_recognition(
            (
                b.session.generation,
                np.ones(RATE),
                False,
                [],
                {'echo_risk': False, 'captured_at': time.monotonic()},
            )
        )
        self.assertTrue(finished.wait(1))
        self.assertFalse(asr.is_set())
        self.assertEqual(audio, [])
        self.assertEqual(requests, [])

    def test_black_screen_wake_does_not_rearm_after_stop_and_reconnect(self):
        b, detector, audio, requests, events, _, _ = self.session('大黄蜂')
        b.session.screen_sleeping = True
        detector.calls = 2
        self.assertTrue(b.wake.feed(np.zeros(FRAME), b.session.generation, capture_nanos=0))
        self.assertFalse(b.session.screen_sleeping)
        self.assertTrue(any(name == 'screen_wake' for name, _ in events))
        b.stop()
        self.assertFalse(b.session.active)
        self.assertIsNone(b.wake.deadline)
        count = detector.calls
        self.assertFalse(b.wake.feed(np.zeros(FRAME), b.session.generation, capture_nanos=0))
        b.change_transport(None)
        b.change_transport(Channel('wifi'))
        self.assertFalse(b.session.active)
        self.assertFalse(b.wake.allowed())
        self.assertEqual(detector.calls, count)
        self.assertEqual(audio, [])
        self.assertEqual(requests, [])

    def test_continuous_mode_can_answer_without_a_local_name(self):
        b, _, audio, requests, _, _, answered = self.session('', '常州属于哪个省')
        b.session.wake = False
        b.session.set_awake(True)
        b.wake.detector = None
        self.frames(b)
        self.assertTrue(answered.wait(1))
        self.assertEqual(requests, ['常州属于哪个省'])
        self.assertEqual(len(audio), 1)

    def test_echo_diagnostic_cannot_report_pass_without_authorized_asr(self):
        b, _, audio, requests, _, _, _ = self.session('大黄蜂')
        b.check_echo(b.session.generation)
        self.assertEqual(b.metrics['echo_self_check']['state'], 'blocked')
        self.assertFalse(b.calibration.running)
        self.assertFalse(b.calibration.failed)
        self.assertEqual(audio, [])
        self.assertEqual(requests, [])
        self.assertTrue(b.session.active)

    def test_repeated_mode_setting_does_not_sleep_or_cancel_an_awake_conversation(self):
        b, detector, _, _, _, _, _ = self.session('大黄蜂')
        detector.calls = 2
        self.assertTrue(b.wake.feed(np.zeros(FRAME), b.session.generation, capture_nanos=0))
        generation = b.session.generation
        b.on_event({'event': 'settings', 'wake_mode': True})
        self.assertTrue(b.session.awake)
        self.assertEqual(b.session.generation, generation)
        b.on_event({'event': 'settings', 'wake_mode': False})
        self.assertTrue(b.session.awake)
        self.assertFalse(b.session.wake)
        self.assertTrue(b.wake.allowed())
        self.assertIsNone(b.wake.deadline)

    def test_only_a_standalone_rest_request_returns_to_local_waiting(self):
        for cue, transcript in (
            ('', '休息。'),
            ('大黄蜂', '大黄蜂，请休息一下。'),
            ('', 'Bumblebee，先休息吧！'),
            ('', '大黄蜂，你先休息一会儿吧。'),
        ):
            with self.subTest(transcript=transcript):
                b, _, _, requests, events, asr, _ = self.session(cue, transcript)
                if not cue:
                    b.session.set_awake(True)
                generation = b.session.generation
                self.frames(b)
                self.assertTrue(asr.wait(1))
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    rows = b.diagnostics_report()['requests']
                    if rows and rows[0].get('gate') is not None:
                        break
                    time.sleep(0.005)
                self.assertEqual(rows[0].get('gate'), 'wake_sleep')
                self.assertFalse(b.session.awake)
                self.assertTrue(b.session.active)
                self.assertGreater(b.session.generation, generation)
                self.assertEqual(requests, [])
                self.assertFalse(any(name == 'transcript' for name, _ in events))

    def test_negation_quotes_and_questions_about_rest_continue_to_dot(self):
        for cue, transcript, expected in (
            ('', '别休息', '别休息'),
            ('', '休息？', '休息？'),
            ('', '“休息”呢？', '“休息”呢？'),
            ('', '什么时候休息？', '什么时候休息？'),
            ('', '大黄蜂，提醒我十点休息', '大黄蜂，提醒我十点休息'),
            ('Bumblebee', 'Bumblebee，别休息，继续讲。', '别休息，继续讲。'),
        ):
            with self.subTest(transcript=transcript):
                b, _, _, requests, _, _, answered = self.session(cue, transcript)
                if not cue:
                    b.session.set_awake(True)
                self.frames(b)
                self.assertTrue(answered.wait(1))
                self.assertEqual(requests, [expected])
                self.assertTrue(b.session.awake)

    def test_rest_is_an_ordinary_request_in_continuous_mode(self):
        b, _, _, requests, _, _, answered = self.session('', '休息一下。')
        b.session.wake = False
        b.session.set_awake(True)
        self.frames(b)
        self.assertTrue(answered.wait(1))
        self.assertEqual(requests, ['休息一下。'])
        self.assertTrue(b.session.awake)

    def test_punctuated_wake_only_stays_listening_without_dispatching_a_task(self):
        for keyword, transcript in (
            ('Bumblebee', 'Bumblebee.'),
            ('Bumblebee', '“Bumble bee?”'),
            ('大黄蜂', '大黄蜂？！'),
            ('大黄蜂', '「大黃蜂？」'),
        ):
            with self.subTest(transcript=transcript):
                b, _, audio, requests, events, asr, _ = self.session(keyword, transcript)
                self.frames(b)
                self.assertTrue(asr.wait(1))
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    rows = b.diagnostics_report()['requests']
                    if rows and rows[0]['outcome'] is not None:
                        break
                    time.sleep(0.005)
                self.assertEqual(rows[0].get('gate'), 'wake_only')
                self.assertEqual(rows[0]['outcome'], 'rejected')
                self.assertEqual(requests, [])
                self.assertFalse(any(name == 'transcript' for name, _ in events))
                self.assertTrue(b.session.awake)
                self.assertEqual(len(audio), 1)

    def arm_tool_capture(self, b, before_start=lambda _: None, *, compare=False):
        s = b.session
        s.active = False
        s.transport_kind = 'usb'
        s.capture_start_ack = True
        token = s.arm_tool_sample(1, compare=compare)['token']
        self.assertIsNone(s.capture_id)
        self.assertEqual(s.begin_tool_sample(token), {'status': 'unavailable'})
        before_start(s)
        with patch.object(b.capture.echo, 'open'), patch('threading.Thread.start'):
            s.start_capture(
                {'epoch': 1, 'capture_id': 'f' * 32, 'capture_start_token': s.capture_start_token}
            )
        b.calibration.stop()
        b.capture.echo.aec = Echo()
        b.capture.endpoint.vad = UtteranceVAD()
        return token

    def test_tool_sample_rejects_dropped_qualifier_before_brain_dispatch(self):
        question = SCENARIO_QUESTIONS['tool'][0]
        transcript = '大黄蜂，' + question.replace('只读', '')
        ordinary, _, _, ordinary_requests, _, _, ordinary_answered = self.session(
            '大黄蜂', transcript
        )
        self.frames(ordinary)
        self.assertTrue(ordinary_answered.wait(1))
        self.assertEqual(ordinary_requests, [question.replace('只读', '')])
        b, _, _, requests, events, asr, _ = self.session('大黄蜂', transcript)
        s = b.session
        token = self.arm_tool_capture(b)
        self.assertEqual(s.release_tool_sample(token), {'status': 'unavailable'})
        self.assertEqual(s.begin_tool_sample(token), {'status': 'ready'})
        self.frames(b)
        self.assertTrue(asr.wait(1))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            rows = b.diagnostics_report()['requests']
            if rows and rows[0]['outcome'] is not None:
                break
            time.sleep(0.005)
        self.assertEqual(rows[0]['gate'], 'tool_sample_guard')
        self.assertEqual(rows[0].get('tool_sample_rejection'), 'read_only_missing')
        self.assertEqual(rows[0]['outcome'], 'rejected')
        self.assertNotIn('brain_start', rows[0]['stages'])
        self.assertEqual(requests, [])
        self.assertFalse(any(name == 'transcript' for name, _ in events))
        self.assertEqual(s.begin_tool_sample(token), {'status': 'unavailable'})
        s.stop()
        self.assertEqual(s.release_tool_sample(token), {'status': 'released'})
        self.assertIsNone(s.tool_sample_rejection('正常对话', False, None))

    def test_tool_sample_accepts_exact_phrase_once(self):
        question = SCENARIO_QUESTIONS['tool'][0]
        b, _, _, requests, _, _, answered = self.session('大黄蜂', '大黄蜂，' + question)
        token = self.arm_tool_capture(b)
        s = b.session
        self.assertEqual(s.begin_tool_sample(token), {'status': 'ready'})
        self.frames(b)
        self.assertTrue(answered.wait(1))
        self.assertEqual(requests, [question])
        self.assertEqual(s.tool_sample_rejection(question, True, time.monotonic()), 'not_open')
        s.stop()

    def test_tool_sample_classifies_mismatches_without_relaxing_admission(self):
        expected = SCENARIO_QUESTIONS['tool'][0]
        for actual, reason in (
            (expected.replace('计算', '计算：'), 'formatting_mismatch'),
            (expected.replace('本机', '服务器'), 'tool_instruction_mismatch'),
            (SCENARIO_QUESTIONS['tool'][1], 'arithmetic_mismatch'),
            (expected.replace('等于几', '等于多少'), 'arithmetic_form_mismatch'),
            (expected + '另外做一个任务', 'transcript_mismatch'),
        ):
            with self.subTest(reason=reason):
                b, *_ = self.session('大黄蜂')
                s = b.session
                token = self.arm_tool_capture(b)
                self.assertEqual(s.begin_tool_sample(token), {'status': 'ready'})
                self.assertEqual(s.tool_sample_rejection(actual, True, time.monotonic()), reason)
                self.assertEqual(
                    s.tool_sample_rejection(expected, True, time.monotonic()), 'not_open'
                )
                s.stop()

    def test_tool_comparison_uses_same_audio_and_never_dispatches(self):
        question = SCENARIO_QUESTIONS['tool'][0]
        for omit, cancel in ((False, False), (True, False), (True, True)):
            with self.subTest(omit=omit, cancel=cancel):
                transcript = question.replace('只读', '') if omit else question
                b, _, primary_audio, requests, events, _, _ = self.session(
                    '大黄蜂', '大黄蜂，' + transcript
                )
                s = b.session
                token = self.arm_tool_capture(b, compare=True)
                self.assertEqual(s.begin_tool_sample(token), {'status': 'ready'})

                def compare(case, audio, is_current):
                    self.assertEqual(case, 1)
                    np.testing.assert_array_equal(audio, primary_audio[0])
                    self.assertTrue(is_current())
                    if cancel:
                        s.cancel('button')
                        self.assertFalse(is_current())
                    return 'matched'

                with patch('recognition_worker.compare_local', side_effect=compare) as decode:
                    self.frames(b)
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        rows = b.diagnostics_report()['requests']
                        if rows and rows[0]['outcome'] is not None:
                            break
                        time.sleep(0.005)
                decode.assert_called_once()
                self.assertEqual(rows[0]['tool_sample_rejection'], 'comparison_only')
                self.assertEqual(rows[0]['outcome'], 'rejected')
                self.assertEqual(requests, [])
                self.assertFalse(any(name == 'transcript' for name, _ in events))
                if cancel:
                    self.assertNotIn('tool_asr_comparison', rows[0])
                else:
                    self.assertEqual(
                        rows[0]['tool_asr_comparison'],
                        {'primary': 'read_only_missing' if omit else 'matched', 'local': 'matched'},
                    )
                s.stop()

    def test_tool_sample_cannot_begin_inside_usb_replacement_cancellation(self):
        b, *_ = self.session('大黄蜂')
        s = b.session
        token = self.arm_tool_capture(b)
        begin_during_switch = []
        with patch.object(
            b.brain,
            'cancel',
            side_effect=lambda: begin_during_switch.append(s.begin_tool_sample(token)),
        ):
            s.change_transport(Channel('usb'))
        self.assertEqual(begin_during_switch, [{'status': 'unavailable'}])
        s.stop()

    def test_pending_tool_sample_stays_closed_if_capture_binding_is_invalid(self):
        question = SCENARIO_QUESTIONS['tool'][0]
        for invalid in (
            lambda s: setattr(s.tool_sample, 'arm_until', 0),
            lambda s: s.cancel('button'),
            lambda s: s.stop(),
        ):
            with self.subTest(invalid=invalid):
                b, *_ = self.session('大黄蜂', question)
                token = self.arm_tool_capture(b, invalid)
                s = b.session
                self.assertEqual(s.begin_tool_sample(token), {'status': 'unavailable'})
                self.assertEqual(
                    s.tool_sample_rejection(question, True, time.monotonic()), 'capture_changed'
                )
                s.stop()
                self.assertEqual(s.release_tool_sample(token), {'status': 'released'})
                self.assertIsNone(s.tool_sample_rejection(question, True, time.monotonic()))

    def test_tool_sample_requires_same_capture_to_finish_startup_on_usb(self):
        question = SCENARIO_QUESTIONS['tool'][0]
        b, *_ = self.session('大黄蜂', question)
        s = b.session
        capabilities = {'capture_reject_version': 1, 'capture_start_ack_version': 1}

        def startup_on_wifi(session):
            for route in (Channel('wifi'), None, Channel('wifi')):
                session.change_transport(route)
            session.ready(capabilities)

        token = self.arm_tool_capture(b, startup_on_wifi)
        self.assertEqual(s.begin_tool_sample(token), {'status': 'unavailable'})
        self.assertEqual(s.tool_sample_rejection(question, True, time.monotonic()), 'not_open')
        s.change_transport(Channel('usb'))
        s.ready({**capabilities, 'capture_active': True})
        self.assertEqual(s.begin_tool_sample(token), {'status': 'unavailable'})
        claim = {'epoch': 1, 'capture_start_token': s.capture_start_token}
        s.rebind({**claim, 'capture_id': '0' * 32})
        self.assertEqual(s.begin_tool_sample(token), {'status': 'unavailable'})
        s.rebind({**claim, 'capture_id': 'f' * 32})
        self.assertEqual(s.begin_tool_sample(token), {'status': 'ready'})
        s.change_transport(Channel('wifi'))
        s.change_transport(Channel('usb'))
        s.ready({**capabilities, 'capture_active': True})
        s.rebind({**claim, 'capture_id': 'f' * 32})
        self.assertEqual(s.begin_tool_sample(token), {'status': 'unavailable'})
        self.assertEqual(s.tool_sample_rejection(question, True, time.monotonic()), 'route_changed')
        s.stop()

    def test_tool_sample_scope_rejects_old_audio_route_changes_and_invalid_ipc(self):
        question = SCENARIO_QUESTIONS['tool'][0]
        for case, reason in (
            ('audio_before_playback', 'capture_window'),
            ('new_generation', 'generation_changed'),
            ('missing_local_wake', 'local_wake_missing'),
        ):
            with self.subTest(case=case):
                b, *_ = self.session('大黄蜂', question)
                s = b.session
                token = self.arm_tool_capture(b)
                self.assertEqual(s.begin_tool_sample('0' * 32), {'status': 'unavailable'})
                self.assertEqual(s.begin_tool_sample(token), {'status': 'ready'})
                started = s.tool_sample.opened_at
                if case == 'new_generation':
                    s.generation += 1
                self.assertEqual(
                    s.tool_sample_rejection(
                        question,
                        case != 'missing_local_wake',
                        started - 0.01 if case == 'audio_before_playback' else started,
                    ),
                    reason,
                )
                self.assertEqual(
                    s.tool_sample_rejection(question, True, time.monotonic()), 'not_open'
                )
                s.stop()
        self.assertFalse(tool_sample_request({'operation': 'tool_sample_arm', 'case': True}))
        self.assertFalse(tool_sample_request({'operation': 'tool_sample_arm', 'case': 31}))
        self.assertFalse(tool_sample_request({'operation': 'tool_sample_begin', 'token': 'x' * 32}))
        self.assertFalse(
            tool_sample_request({'operation': 'tool_sample_arm', 'case': 1, 'text': 'private'})
        )

    def test_wake_prefix_preserves_numbers_and_quotes_in_a_real_question(self):
        for value in ('-3', '+3', '.5', '“参数”'):
            with self.subTest(value=value):
                b, _, _, requests, _, _, answered = self.session(
                    'Bumblebee', '“Bumblebee”，' + value + '是多少？'
                )
                self.frames(b)
                self.assertTrue(answered.wait(1))
                self.assertEqual(requests, [value + '是多少？'])

    def test_wake_only_does_not_call_brain_and_does_not_strip_later_questions(self):
        b, _, audio, requests, _, asr, _ = self.session('大黄蜂', '大黄蜂')
        self.frames(b)
        self.assertTrue(asr.wait(1))
        deadline = time.monotonic() + 1
        while b.recognition.busy and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(requests, [])
        self.assertTrue(b.session.awake)
        self.assertEqual(len(audio), 1)
        self.assertFalse(b.wake.claim_prefix())
        b.speech.recognize = lambda pcm: '大黄蜂是什么车型'
        b.capture.endpoint.vad = UtteranceVAD()
        self.frames(b, 6, 10)
        deadline = time.monotonic() + 1
        while not requests and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(requests, ['大黄蜂是什么车型'])


if __name__ == '__main__':
    unittest.main()
