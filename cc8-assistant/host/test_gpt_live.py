import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np

from brain_router import BrainRouter
from bridge import Bridge
from capture_evidence import FRAME_NS
from configuration import ROOT, load_config, select_voice_mode
from dsp import FRAME, VoiceDetector
from echo_pipeline import CleanFrame
from gpt_live import MAX_INPUT_FRAMES, Fragment, LivePlayback, LiveSession, pcm16
from gpt_live_credentials import load_key
from gpt_live_trial import LiveTrial
from reply_types import LiveReply


def transcript(text, start, end, *, output=False):
    speaker = 'output' if output else 'input'
    return {
        'type': f'session.{speaker}_transcript.delta',
        'delta': text,
        'start_ms': start,
        'end_ms': end,
    }


def delegation(ident, offset):
    return {
        'type': 'session.delegation.created',
        'offset_ms': offset,
        'delegation': {'type': 'delegation', 'target': 'client', 'id': ident},
    }


def started(voice='marin', rate=16000, target='client'):
    return {
        'type': 'session.started',
        'session': {
            'model': 'gpt-live-1',
            'status': 'active',
            'audio': {'format': {'type': 'audio/pcm', 'rate': rate}, 'output': {'voice': voice}},
            'delegation': {'type': target},
        },
    }


class LiveSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_chinese_delegation_audio_and_owned_shutdown(self):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()

        class Socket:
            async def send(self, message):
                outgoing.put_nowait(json.loads(message))

            async def recv(self):
                return json.dumps(await incoming.get())

        @asynccontextmanager
        async def connector(key):
            yield Socket()

        hooks = Mock()
        hooks.generation.return_value = 17
        trial = LiveSession('fixture-only', 'marin', hooks, connector=connector)
        hooks.delegate.side_effect = lambda owner, ident, text: (
            owner.result(ident, '查到了。') or True
        )
        trial.feed(bytes(320), True)
        task = asyncio.create_task(trial.exchange())

        async def get():
            return await asyncio.wait_for(outgoing.get(), 2)

        try:
            start = await get()
            self.assertEqual(start['type'], 'session.start')
            self.assertEqual(start['session']['model'], 'gpt-live-1')
            self.assertEqual(start['session']['delegation'], {'type': 'client'})
            self.assertEqual(
                start['session']['audio']['format'], {'type': 'audio/pcm', 'rate': 16000}
            )
            self.assertEqual(start['session']['audio']['output']['voice'], 'marin')
            self.assertFalse(start['session']['store'])
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(outgoing.get(), 0.08)
            incoming.put_nowait(started())
            audio = await get()
            self.assertEqual(audio['type'], 'session.input_audio.append')
            self.assertEqual(base64.b64decode(audio['audio']), bytes(320))
            incoming.put_nowait(transcript('追加的用户上下文', 100, 100))
            incoming.put_nowait(transcript('请查', 100, 220))
            incoming.put_nowait(delegation('item_example', 800))
            incoming.put_nowait(transcript('常州天气', 220, 760))
            incoming.put_nowait(delegation('item_example', 800))
            reply = await get()
            hooks.delegate.assert_called_once_with(trial, 'item_example', '请查常州天气')
            self.assertEqual(reply['type'], 'session.commentary.append')
            self.assertEqual(reply['delegation_id'], 'item_example')
            self.assertEqual(reply['content'], '查到了。')
            incoming.put_nowait(transcript('追加的助手上下文', 800, 800, output=True))
            incoming.put_nowait(transcript('常州', 1000, 1200, output=True))
            incoming.put_nowait(
                {
                    'type': 'session.output_audio.delta',
                    'delta': base64.b64encode(bytes(320)).decode(),
                }
            )
            for _ in range(100):
                if hooks.play.called:
                    break
                await asyncio.sleep(0.005)
            output = hooks.play.call_args.args[1]
            self.assertEqual(output.generation, 17)
            self.assertEqual(output.caption(), ('常州', [[0, 2, 0, 3200]]))
            self.assertEqual(output.chunks.get_nowait().size, 160)
            hooks.delegate.side_effect = None
            hooks.delegate.return_value = True
            incoming.put_nowait(transcript('再查一次', 1400, 1650))
            incoming.put_nowait(delegation('previous', 1800))
            for _ in range(100):
                if hooks.delegate.call_count == 2:
                    break
                await asyncio.sleep(0.01)
            hooks.delegate.assert_called_with(trial, 'previous', '再查一次')
            trial.interrupt_playback()
            self.assertEqual((await get())['type'], 'session.instructions.append')
            self.assertTrue(output.interrupted.is_set())
            trial.result('previous', '过时的播报')
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(outgoing.get(), 0.08)
            hooks.current.return_value = False
            trial.feed(bytes(320), True)
            self.assertEqual((await get())['type'], 'session.close')
            incoming.put_nowait({'type': 'session.closed'})
            await asyncio.wait_for(task, 2)
            hooks.closed.assert_called_once_with(trial, False)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_delayed_handshake_preserves_order_without_pre_ready_upload(self):
        received, sent = asyncio.Queue(), asyncio.Queue()

        async def send(message):
            sent.put_nowait(json.loads(message))

        @asynccontextmanager
        async def connector(key):
            yield NS(send=send, recv=received.get)

        hooks = Mock()
        trial = LiveSession('fixture-only', 'marin', hooks, connector=connector)
        prefix = b'\x00\x20' * FRAME
        trial.initial_audio.append(prefix)
        task = asyncio.create_task(trial.exchange())
        try:
            start = await asyncio.wait_for(sent.get(), 2)
            self.assertEqual(start['type'], 'session.start')
            # 2.5 seconds of 10 ms capture frames used to overflow the queue
            # while the socket was still within its allowed startup window.
            frames = [index.to_bytes(2, 'little') * FRAME for index in range(250)]
            for frame in frames:
                trial.feed(frame, True)
            self.assertFalse(trial.stop.is_set())
            self.assertTrue(sent.empty())
            received.put_nowait(json.dumps(started()))
            expected = prefix + b''.join(frames)
            actual = bytearray()
            while len(actual) < len(expected):
                event = await asyncio.wait_for(sent.get(), 2)
                self.assertEqual(event['type'], 'session.input_audio.append')
                actual.extend(base64.b64decode(event['audio']))
            self.assertEqual(actual, expected)
            hooks.current.return_value = False
            self.assertEqual((await asyncio.wait_for(sent.get(), 2))['type'], 'session.close')
            received.put_nowait('{"type":"session.closed"}')
            await asyncio.wait_for(task, 2)
            hooks.closed.assert_called_once_with(trial, False)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_capture_invalidated_during_handshake_never_uploads_buffer(self):
        received, sent = asyncio.Queue(), asyncio.Queue()

        async def send(message):
            sent.put_nowait(json.loads(message))

        @asynccontextmanager
        async def connector(key):
            yield NS(send=send, recv=received.get)

        hooks = Mock()
        trial = LiveSession('fixture-only', 'marin', hooks, connector=connector)
        trial.feed(bytes(FRAME * 2), True)
        task = asyncio.create_task(trial.exchange())
        try:
            self.assertEqual((await asyncio.wait_for(sent.get(), 2))['type'], 'session.start')
            hooks.current.return_value = False
            received.put_nowait(json.dumps(started()))
            self.assertEqual((await asyncio.wait_for(sent.get(), 2))['type'], 'session.close')
            received.put_nowait('{"type":"session.closed"}')
            await asyncio.wait_for(task, 2)
            self.assertTrue(sent.empty())
            hooks.closed.assert_called_once_with(trial, False)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_stalled_input_queue_is_bounded(self):
        trial = LiveSession('fixture-only', 'marin', Mock())
        for _ in range(MAX_INPUT_FRAMES + 1):
            trial.feed(bytes(FRAME * 2), True)
        self.assertEqual(trial.audio.qsize(), MAX_INPUT_FRAMES)
        self.assertTrue(trial.stop.is_set())
        self.assertTrue(trial.input_overflow.is_set())

    async def test_failed_or_mismatched_handshake_does_not_upload(self):
        for event in (
            {'type': 'error'},
            started(rate=24000),
            started('cedar'),
            started(target='responses'),
        ):
            with self.subTest(event=event):
                messages = []

                async def send(message):
                    messages.append(json.loads(message)['type'])

                async def recv():
                    return json.dumps(event)

                @asynccontextmanager
                async def connector(key):
                    yield NS(send=send, recv=recv)

                hooks = Mock()
                trial = LiveSession('fixture-only', 'marin', hooks, connector=connector)
                trial.feed(bytes(320), True)
                await trial.exchange()
                self.assertEqual(messages, ['session.start'])
                hooks.closed.assert_called_once_with(trial, True)

    async def test_barge_in_while_receiving_does_not_start_queued_cloud_audio(self):
        messages = []
        events = iter([started(), {'type': 'session.closed'}])

        async def send(message):
            messages.append(json.loads(message)['type'])

        async def recv():
            if messages == ['session.start'] and trial.started.is_set():
                trial.interrupt_playback()
                return json.dumps({'type': 'session.output_audio.delta', 'delta': 'AAA='})
            return json.dumps(next(events))

        @asynccontextmanager
        async def connector(key):
            yield NS(send=send, recv=recv)

        hooks = Mock()
        trial = LiveSession('fixture-only', 'marin', hooks, connector=connector)
        await trial.exchange()
        self.assertEqual(messages, ['session.start', 'session.instructions.append'])
        hooks.play.assert_not_called()
        hooks.closed.assert_called_once_with(trial, False)

    def test_old_and_duplicate_delegations_do_not_replay_old_transcripts(self):
        now = [10.0]
        hooks = Mock()
        trial = LiveSession('fixture-only', 'marin', hooks, clock=lambda: now[0])
        trial.event(delegation('empty', 900))
        now[0] += 1
        trial.tick()
        trial.event(transcript('旧的请求', 100, 700))
        trial.event(delegation('empty', 2000))
        trial.event(delegation('new', 2100))
        now[0] += 1
        trial.tick()
        hooks.delegate.assert_not_called()

    def test_live_caption_uses_utf16_and_sample_positions(self):
        publish = Mock()
        player = LivePlayback(10)
        player.add_caption(Fragment('你好', 200, 400))
        player.subscribe(publish)
        player.add_caption(Fragment('🤖常州', 450, 800))
        player.add_caption(Fragment('迟到的旧字幕', 300, 700))
        publish.assert_called_with('你好🤖常州', [[0, 2, 0, 3200], [2, 6, 4000, 9600]])
        self.assertEqual(publish.call_count, 2)
        player.interrupted.set()
        player.add_caption(Fragment('结束', 900, 1100))
        self.assertEqual(publish.call_count, 2)


class LiveAdmissionTests(unittest.TestCase):
    def test_private_health_distinguishes_trial_readiness_and_live_session(self):
        bridge = Bridge(None, BrainRouter(Mock(), selected='dot'))

        def state(expected):
            self.assertEqual(bridge.health()['gpt_live_state'], expected)

        state('disabled')
        bridge.live_trial_requested = True
        state('configuration_unavailable')
        bridge.live = LiveTrial(bridge, 'fixture-private-key', 'marin')
        state('device_disconnected')
        bridge.session.connection = object()
        bridge.session.capture_route_pending = True
        state('capture_route_pending')
        bridge.session.capture_route_pending = False
        with patch.object(bridge, 'owner_access', return_value=False):
            state('owner_required')
        state('device_incompatible')
        bridge.session.live_caption_supported = True
        bridge.session.wake = False
        state('wake_mode_required')
        bridge.session.wake = True
        state('local_wake_unavailable')
        bridge.wake.detector = NS()
        state('capture_paused')
        bridge.session.active = True
        bridge.session.awake = False
        with patch.object(bridge.calibration, 'failed', True):
            state('calibration_required')
        state('waiting_for_local_wake')
        bridge.session.awake = True
        state('ready_for_speech')
        with patch.object(bridge.response, 'busy', True):
            state('capture_busy')
        bridge.session.text_generation = bridge.generation
        state('capture_busy')
        bridge.session.text_generation = None
        owner = LiveSession('fixture-private-key', 'marin', bridge.live, connector=Mock())
        bridge.live.session = owner
        with patch.object(bridge.live, 'current', return_value=True):
            state('connecting')
            owner.started.set()
            state('active')
        with patch.object(bridge.live, 'current', return_value=False):
            state('session_ending')
        bridge.session.active = False
        bridge.add_capture_metric('live_sessions', 1)
        health = bridge.health()
        self.assertEqual(health['gpt_live_sessions'], 1)
        self.assertEqual(health['gpt_live_failures'], 0)
        self.assertNotIn('fixture-private-key', json.dumps(health))
        owner.connector.assert_not_called()
        self.assertFalse(health['microphone_active'])

    def make_trial(self):
        playback = NS(
            playing=False,
            live_output=None,
            playback_serial=8,
            spoken=[],
            last_audio_end=0,
            stop_output=Mock(return_value=True),
            clear_cancelled_pause=Mock(),
            notify=Mock(),
        )
        host = NS(
            guard=threading.RLock(),
            generation=7,
            owner_access=Mock(return_value=True),
            session=NS(
                capture_owner=Mock(return_value=('usb', 'route1', 1, 'mic')),
                live_caption_supported=True,
                capture_route_pending=False,
                wake=True,
                text_generation=None,
                tool_samples=NS(reserved=False),
                voice_preferences=NS(natural_barge=True),
            ),
            wake=NS(allowed=Mock(return_value=True)),
            calibration=NS(active=False, running=False, failed=False),
            brain=BrainRouter(Mock(), Mock()),
            recognition=NS(busy=False),
            visual=NS(generation=None, voice_followup=Mock(return_value=False)),
            announcements=NS(generation=None),
            meetings=NS(reserved=lambda: False),
            capture=NS(
                measuring=False,
                probe=None,
                known_speech=None,
                live_response_probe=lambda: None,
                endpoint_audio_current=lambda _: True,
            ),
            playback=playback,
            response=NS(busy=False, queue=queue.Queue()),
            event=Mock(),
            status=Mock(),
            add_capture_metric=Mock(),
            enqueue_response=Mock(),
        )
        owner = NS(
            run=Mock(),
            feed=Mock(),
            stop=threading.Event(),
            interrupt=threading.Event(),
            initial_audio=[],
            interrupt_playback=Mock(),
            result=Mock(),
        )
        trial = LiveTrial(host, 'fixture-only', 'marin', Mock(return_value=owner))
        return trial, host, owner

    def test_playback_admission_rechecks_barge_under_capture_guard(self):
        trial, host, owner = self.make_trial()
        self.assertTrue(trial.handoff(np.full(FRAME, 0.1)))
        owner.interrupt.set()
        self.assertFalse(trial.play(owner, LivePlayback(host.generation)))
        self.assertIsNone(trial.output)
        self.assertIsNone(trial.pending_output)

    def test_local_photo_followup_owns_the_request_before_dot_delegation(self):
        trial, host, owner = self.make_trial()
        host.visual.voice_followup.return_value = True
        host.begin_visual = Mock()
        self.assertTrue(trial.handoff(np.full(FRAME, 0.1)))
        self.assertTrue(trial.delegate(owner, 'photo', '大黄蜂，追问这张照片，上面写了什么？'))
        host.visual.voice_followup.assert_called_once_with('追问这张照片，上面写了什么？', 7)
        host.enqueue_response.assert_not_called()
        host.begin_visual.assert_not_called()
        owner.result.assert_called_once_with('photo', '请查看设备上的照片追问提示。')

    def test_live_result_read_cannot_cross_a_capture_route_change_during_cancel(self):
        trial, host, owner = self.make_trial()
        self.assertTrue(trial.handoff(np.full(FRAME, 0.1)))

        def cancel(reason):
            host.generation += 1
            host.session.capture_owner.return_value = ('wifi', 'route2', 2, 'mic')
            return host.generation

        host.cancel = cancel
        self.assertTrue(trial.delegate(owner, 'read', '继续朗读'))
        host.enqueue_response.assert_not_called()

    def test_stale_or_echo_delegation_leaves_photo_state_untouched(self):
        trial, host, owner = self.make_trial()
        self.assertTrue(trial.handoff(np.full(FRAME, 0.1)))
        text = '追问这张照片，上面写了什么？'
        with patch('gpt_live_trial.LiveTrial.references', return_value=[text]):
            self.assertFalse(trial.delegate(owner, 'echo', text))
        host.generation += 1
        self.assertFalse(trial.delegate(owner, 'stale', text))
        host.visual.voice_followup.assert_not_called()

    def test_wake_owner_capability_and_capture_route_gate_upload(self):
        trial, host, owner = self.make_trial()
        frame = NS(nanos=10, cleaned=np.zeros(160))
        host.wake.allowed.return_value = False
        self.assertFalse(trial.consume(frame, True, True))
        host.wake.allowed.return_value = True
        host.session.wake = False
        self.assertFalse(trial.consume(frame, True, True))
        host.session.wake = True
        host.session.live_caption_supported = False
        self.assertFalse(trial.consume(frame, True, True))
        host.session.live_caption_supported = True
        host.owner_access.return_value = False
        self.assertFalse(trial.consume(frame, True, True))
        host.owner_access.return_value = True
        host.session.tool_samples.reserved = True
        self.assertFalse(trial.consume(frame, True, True))
        trial.factory.assert_not_called()
        host.session.tool_samples.reserved = False
        self.assertTrue(trial.handoff(np.full(160, 0.25)))
        self.assertEqual(owner.initial_audio, [b'\x00\x20' * 160])
        host.session.capture_owner.return_value = ('wifi', 'route2', 1, 'mic')
        self.assertTrue(trial.consume(frame, True, True))
        self.assertTrue(owner.stop.is_set())
        owner.feed.assert_not_called()

    def test_streaming_wake_passes_same_utterance_pcm_before_new_frames(self):
        for echo_risk in ('none', 'render', 'pre_roll'):
            with self.subTest(echo_risk=echo_risk):
                trial, host, owner = self.make_trial()
                host.wake.allowed.return_value = False
                endpoint = Bridge(Mock(), host.brain).capture.endpoint
                endpoint.vad = VoiceDetector()
                endpoint.near_speech.feed = Mock(return_value=(True, True))
                playing = Mock(return_value=False)
                endpoint.hooks = replace(
                    endpoint.hooks,
                    capture_current=lambda _: True,
                    awake=host.wake.allowed,
                    wake_allowed=host.wake.allowed,
                    playing=playing,
                    last_audio_end=lambda: float('-inf'),
                    live_audio=trial.consume,
                )
                before = np.full(FRAME, 0.125)
                after = np.full(FRAME, 0.25)
                render = before if echo_risk == 'render' else np.zeros(FRAME)
                for i in range(1, VoiceDetector.ONSET_FRAMES + 1):
                    playing.return_value = echo_risk == 'pre_roll' and i == 1
                    endpoint.consume(CleanFrame(i * FRAME_NS, before, render, before, before))
                self.assertTrue(endpoint.vad.talking)
                trial.factory.assert_not_called()

                def confirm(*args, **kwargs):
                    host.wake.allowed.return_value = True
                    return True

                endpoint.hooks = replace(endpoint.hooks, wake_feed=confirm)
                stamp = (VoiceDetector.ONSET_FRAMES + 1) * FRAME_NS
                endpoint.consume(CleanFrame(stamp, after, render, after, after))
                expected = (
                    pcm16(before) * VoiceDetector.ONSET_FRAMES if echo_risk == 'none' else b''
                )
                self.assertEqual(b''.join(owner.initial_audio), expected)
                owner.feed.assert_called_once_with(pcm16(after), True)
                self.assertFalse(endpoint.vad.talking)

    def test_local_barge_stops_output_without_invalidating_dot(self):
        trial, host, owner = self.make_trial()
        self.assertTrue(trial.handoff(np.full(160, 0.1)))
        player = LivePlayback(host.generation)
        trial.output = player
        host.playback.live_output = player
        host.playback.playing = True
        frame = NS(nanos=10, cleaned=np.full(160, 0.25))
        for _ in range(10):
            frame.nanos += FRAME_NS
            trial.consume(frame, True, False)
        self.assertFalse(player.interrupted.is_set())
        self.assertEqual(owner.feed.call_args.args, (bytes(320), False))
        for _ in range(5):
            frame.nanos += FRAME_NS * 4
            trial.consume(frame, True, True)
        self.assertFalse(player.interrupted.is_set())
        for _ in range(4):
            frame.nanos += FRAME_NS
            trial.consume(frame, True, True)
        self.assertTrue(player.interrupted.is_set())
        self.assertFalse(owner.stop.is_set())
        host.event.assert_called_once_with('live_playback_stop', generation=7, playback_id=8)
        self.assertTrue(trial.delegate(owner, 'new', '大黄蜂，查一下常州天气'))
        job = host.enqueue_response.call_args.args[0]
        self.assertEqual(job.text, '查一下常州天气')
        self.assertTrue(job.current())
        job.deliver('已查询')
        owner.result.assert_called_once_with('new', '已查询')
        host.generation += 1
        self.assertFalse(job.current())

    def test_playback_onset_does_not_treat_the_previous_question_as_an_interruption(self):
        trial, host, owner = self.make_trial()
        self.assertTrue(trial.handoff(np.full(160, 0.1)))
        speech = NS(nanos=10, cleaned=np.full(160, 0.25))
        for _ in range(5):
            speech.nanos += FRAME_NS
            trial.consume(speech, True, True)
        output = LivePlayback(host.generation)
        with patch('gpt_live_trial.threading.Thread'):
            self.assertTrue(trial.play(owner, output))
        for _ in range(5):
            speech.nanos += FRAME_NS
            trial.consume(speech, True, True)
        self.assertFalse(output.interrupted.is_set())
        trial.consume(speech, False, False)
        for _ in range(5):
            speech.nanos += FRAME_NS
            trial.consume(speech, True, True)
        self.assertTrue(output.interrupted.is_set())

    def test_mode_switch_keeps_private_fields_and_minimax_default(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / 'profile.json'
            env = {'CC8_CONFIG_FILE': str(profile)}
            self.assertEqual(load_config(ROOT, {}).voice_mode, 'minimax')
            profile.write_text('{"schema_version":1,"wake_window_seconds":40}')
            profile.chmod(0o600)
            config = select_voice_mode('gpt-live-1', ROOT, env)
            self.assertEqual(config.voice_mode, 'gpt-live-1')
            self.assertEqual(config.wake_window_seconds, 40)
            self.assertEqual(config.gpt_live_voice, 'marin')
            self.assertEqual(select_voice_mode('minimax', ROOT, env).voice_mode, 'minimax')

    def test_key_requires_private_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            path = Path(directory) / 'key'
            path.write_text('offline-fixture-placeholder')
            path.chmod(0o644)
            with self.assertRaises(OSError):
                load_key(path)
            path.chmod(0o600)
            self.assertEqual(load_key(path), 'offline-fixture-placeholder')


class LiveDotIntegrationTests(unittest.TestCase):
    def test_real_response_worker_delivers_dot_result_to_live_only(self):
        class Dot:
            last_metrics = {'completed': True}

            def clauses(self, text, history, current, update):
                self.request = text
                assert current()
                yield '常州在江苏省。'

        dot = Dot()
        minimax = Mock()
        router = BrainRouter(minimax, dot)
        speech = Mock()
        bridge = Bridge(speech, router)
        bridge.session.active = True
        bridge.session.connection = object()

        bridge.publish_workspace = Mock()
        bridge.event = Mock()
        current = Mock(return_value=True)
        delivery = Mock()
        bridge.response.respond_job(
            LiveReply(bridge.generation, '常州属于哪个省？', current, delivery)
        )
        self.assertEqual(dot.request, '常州属于哪个省？')
        delivery.assert_called_once_with('常州在江苏省。')
        minimax.clauses.assert_not_called()
        speech.synthesize.assert_not_called()
        self.assertEqual(bridge.workspace.snapshot()['tasks'][0]['status'], 'completed')

    def test_local_private_memory_is_not_sent_to_live(self):
        router = BrainRouter(Mock(), Mock())
        bridge = Bridge(Mock(), router)
        router.preferences = NS(voice=lambda *_: '本地保存的私人资料')
        bridge.session.active = True
        bridge.session.connection = object()
        bridge.publish_workspace = Mock()
        bridge.event = Mock()
        delivery = Mock()
        bridge.response.respond_job(
            LiveReply(bridge.generation, '读取偏好', lambda: True, delivery)
        )
        self.assertEqual(bridge.workspace.snapshot()['tasks'][0]['status'], 'completed')
        delivery.assert_called_once_with('已处理，请在屏幕上查看结果。')
        self.assertFalse(bridge.response.history)
