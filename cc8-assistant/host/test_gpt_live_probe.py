import asyncio
import base64
from contextlib import asynccontextmanager, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from audio_signal import FRAME, RATE
from gpt_live import Fragment, LivePlayback, LiveSession
from gpt_live_probe import Probe, ProbeResult, VOICE_LINE, main, run_live, save_voice, score
from test_gpt_live import delegation, started, transcript


class LiveProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_production_exchange_and_counts_public_audio_and_caption(self):
        messages = []
        events = asyncio.Queue()
        spoken = b'\x01\x00' * FRAME

        class Socket:
            async def send(self, raw):
                message = json.loads(raw)
                messages.append(message)
                match message['type']:
                    case 'session.start':
                        events.put_nowait(started('cedar'))
                    case 'session.input_audio.append' if len(messages) == 2:
                        events.put_nowait(transcript('请介绍', 0, 100))
                        events.put_nowait(transcript('大黄蜂', 100, 300))
                        events.put_nowait(delegation('case_1', 300))
                    case 'session.commentary.append':
                        events.put_nowait(transcript(VOICE_LINE, 500, 800, output=True))
                        events.put_nowait(
                            {
                                'type': 'session.output_audio.delta',
                                'delta': base64.b64encode(spoken).decode(),
                            }
                        )
                    case 'session.close':
                        events.put_nowait({'type': 'session.closed'})

            async def recv(self):
                return json.dumps(await events.get())

        @asynccontextmanager
        async def connector(_key):
            yield Socket()

        def factory(key, voice, probe):
            return LiveSession(key, voice, probe, connector=connector)

        with (
            patch('gpt_live_probe.REPLY_TIMEOUT_SECONDS', 2),
            patch('gpt_live_probe.MIN_REPLY_WINDOW_SECONDS', 0.1),
            patch('gpt_live_probe.REPLY_QUIET_SECONDS', 0.05),
        ):
            result = await run_live('fixture', 'cedar', spoken + b'\x02\x00', factory=factory)
        self.assertEqual(
            result,
            ProbeResult(
                True, '请介绍大黄蜂', spoken, VOICE_LINE, 1, 1, 0, 300 * RATE // 1000 - FRAME
            ),
        )
        self.assertEqual(messages[0]['session']['audio']['output']['voice'], 'cedar')
        uploaded = b''.join(
            base64.b64decode(event['audio'])
            for event in messages
            if event['type'] == 'session.input_audio.append'
        )
        self.assertTrue(uploaded.startswith(spoken + b'\x02\x00'))
        self.assertEqual(set(uploaded[FRAME * 2 + 2 :]), {0})
        self.assertLess(len(uploaded), RATE * 2 * 2)
        self.assertEqual(messages[-1]['type'], 'session.close')

    def test_measures_caption_coverage_and_overrun_per_pcm_segment(self):
        probe = Probe()
        first, second = LivePlayback(1), LivePlayback(1)
        first.add_audio(bytes(FRAME * 2))
        first.add_caption(Fragment('你好', 250, 260))
        first.closed.set()
        second.add_audio(bytes(FRAME * 4))
        probe.play(None, first)
        probe.drain()
        probe.play(None, second)
        probe.drain()

        self.assertEqual(
            probe.result(settled=False),
            ProbeResult(False, '', bytes(FRAME * 6), '你好', 1, 2, 1, 0),
        )
        second.add_caption(Fragment('大黄蜂', 900, 1000))
        self.assertEqual(
            probe.result(settled=True),
            ProbeResult(True, '', bytes(FRAME * 6), '你好大黄蜂', 2, 2, 0, RATE // 10 - FRAME * 2),
        )

    def test_dry_run_does_not_load_credential_or_start_a_session(self):
        with patch('gpt_live_probe.run') as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--runtime-root', '/unused']), 0)
        run.assert_not_called()
        self.assertFalse(json.loads(out.getvalue())['execute'])

    def test_scores_missing_transcript_separately_from_recognized_errors(self):
        self.assertEqual(score('南京', ''), {'outcome': 'missing_transcript'})
        self.assertEqual(score('南京', '……'), {'outcome': 'missing_transcript'})
        self.assertEqual(score('南京 API', '南宁 APP')['han_cer']['substitutions'], 1)
        self.assertEqual(score('南京 API', '南宁 APP')['english_wer']['substitutions'], 1)

    def test_saved_sample_is_owner_private_and_valid_pcm16(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            path = parent / save_voice(parent, 'test', b'\x01\x00' * FRAME)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with wave.open(str(path), 'rb') as source:
                self.assertEqual((source.getframerate(), source.getsampwidth()), (RATE, 2))
                self.assertEqual(source.readframes(FRAME), b'\x01\x00' * FRAME)
            with self.assertRaises(FileExistsError):
                save_voice(parent, 'test', b'\x02\x00' * FRAME)
