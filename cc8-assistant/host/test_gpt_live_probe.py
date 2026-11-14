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
from gpt_live import LiveSession
from gpt_live_probe import VOICE_LINE, main, run_live, save_voice, score
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

        with patch('gpt_live_probe.SILENCE_SECONDS', 1):
            result = await run_live('fixture', 'cedar', spoken + b'\x02\x00', factory=factory)
        self.assertEqual(result, ('请介绍大黄蜂', spoken, VOICE_LINE, 1))
        self.assertEqual(messages[0]['session']['audio']['output']['voice'], 'cedar')
        uploaded = b''.join(
            base64.b64decode(event['audio'])
            for event in messages
            if event['type'] == 'session.input_audio.append'
        )
        self.assertTrue(uploaded.startswith(spoken + b'\x02\x00'))
        self.assertEqual(set(uploaded[FRAME * 2 + 2 :]), {0})
        self.assertEqual(messages[-1]['type'], 'session.close')

    def test_dry_run_does_not_load_credential_or_start_a_session(self):
        with patch('gpt_live_probe.run') as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--runtime-root', '/unused']), 0)
        run.assert_not_called()
        self.assertFalse(json.loads(out.getvalue())['execute'])

    def test_scores_missing_transcript_separately_from_recognized_errors(self):
        self.assertEqual(score('南京', ''), {'outcome': 'missing_transcript'})
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
