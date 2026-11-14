"""Compare cloud speech using fixed public audio without starting the assistant."""

import argparse
import asyncio
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import platform
import subprocess
from tempfile import TemporaryDirectory
import time
import uuid
import wave

import numpy as np

from asr_evaluation import CORPUS, corpus_samples, edit_counts, han, normalized, read_json, words
from audio_signal import FRAME, RATE
from configuration import load_config
from gpt_live import LiveSession
from gpt_live_credentials import KEY_FILE, load_key
from mac_voice import synthesize
from private_output import private_directory
from recognition import MandarinRecognizer
from speech_providers import DirectSpeech
from synthetic_asr import fixture_digest


SAMPLE_IDS = ('qa-01', 'names_places-05', 'mixed-04')
VOICE_LINE = '你好，我是大黄蜂。汽车人，准备出发！'
VOICE_REQUEST = '请朗读这句：' + VOICE_LINE
FRAME_SECONDS = FRAME / RATE
REPLY_TIMEOUT_SECONDS = 12
MIN_REPLY_WINDOW_SECONDS = 3
REPLY_QUIET_SECONDS = 1.2


@dataclass(frozen=True)
class ProbeResult:
    settled: bool
    transcript: str
    pcm: bytes
    caption: str
    caption_fragments: int
    audio_segments: int
    uncaptained_segments: int
    caption_overrun_frames: int | None


class Probe:
    """Use the production Live parser with synthetic input and a fixed mock Dot reply."""

    def __init__(self):
        self.failed = False
        self.spoken = ''
        self.output = []
        self.pcm = bytearray()
        self.segments = []
        self.active = True

    def current(self, owner):
        return self.active and not owner.stop.is_set()

    def generation(self):
        return 1

    def transcript(self, owner):
        self.spoken += owner.transcripts[-1].text
        if len(self.spoken) > 1000:
            raise ValueError('Probe transcript exceeded its bound')
        return True

    def delegate(self, owner, ident, _text):
        owner.result(ident, VOICE_LINE)
        return True

    def play(self, _owner, output):
        self.output.append(output)
        return True

    def closed(self, _owner, failed):
        self.failed = failed

    def drain(self):
        # The network exchange and this synthetic sink run on the same event loop.
        for output in self.output:
            while not output.chunks.empty():
                chunk = output.chunks.get_nowait()
                if len(self.pcm) + chunk.nbytes > RATE * 24 * 2:
                    raise ValueError('Probe audio exceeded its bound')
                self.pcm.extend(chunk.tobytes())
            if output.closed.is_set():
                self.segments.append(output)
        self.output[:] = [output for output in self.output if not output.closed.is_set()]

    def result(self, *, settled):
        segments = self.segments + self.output
        captions = [segment.caption() for segment in segments]
        overruns = [
            max(0, timings[-1][3] - segment.samples)
            for segment, (_, timings) in zip(segments, captions, strict=True)
            if timings
        ]
        return ProbeResult(
            settled,
            self.spoken,
            bytes(self.pcm),
            ''.join(text for text, _ in captions),
            sum(len(timings) for _, timings in captions),
            len(segments),
            sum(not timings for _, timings in captions),
            max(overruns, default=None),
        )


async def run_live(key, voice, pcm, *, factory=LiveSession):
    fixture_digest(pcm)
    probe = Probe()
    session = factory(key, voice, probe)
    task = asyncio.create_task(session.exchange())
    try:
        async with asyncio.timeout(12):
            while not session.started.is_set() and not task.done():
                await asyncio.wait({task}, timeout=FRAME_SECONDS)
        if not session.started.is_set() or task.done():
            raise ValueError('Probe session did not start')
        frame_bytes = FRAME * 2
        silence = bytes(frame_bytes)
        frames = (len(pcm) + frame_bytes - 1) // frame_bytes
        started = time.monotonic()
        last_caption_end = 0
        last_caption_at = started
        settled = False
        for index in range(frames + round(REPLY_TIMEOUT_SECONDS / FRAME_SECONDS)):
            target = started + FRAME_SECONDS * index
            await asyncio.sleep(max(0, target - time.monotonic()))
            if task.done() or time.monotonic() - target > 0.25:
                raise ValueError('Probe session ended before the stimulus completed')
            voice_frame = pcm[index * frame_bytes : (index + 1) * frame_bytes]
            session.feed(
                voice_frame.ljust(frame_bytes, b'\0') if voice_frame else silence, bool(voice_frame)
            )
            probe.drain()
            if session.last_output_caption_end != last_caption_end:
                last_caption_end = session.last_output_caption_end
                last_caption_at = time.monotonic()
            # Continue sending silence while the provider decides when to speak.
            # Allow a quiet tail after the last PCM or caption to collect late fragments.
            if (
                index >= frames + round(MIN_REPLY_WINDOW_SECONDS / FRAME_SECONDS)
                and probe.segments
                and not probe.output
                and probe.spoken
                and not session.pending
                and time.monotonic() - max(session.last_output, last_caption_at)
                >= REPLY_QUIET_SECONDS
            ):
                settled = True
                break
        probe.active = False
        await asyncio.wait_for(task, timeout=3)
        probe.drain()
        if probe.failed:
            raise ValueError('Probe session failed')
        return probe.result(settled=settled)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def score(reference, transcript):
    if not any(char.isalnum() for char in transcript):
        return {'outcome': 'missing_transcript'}
    return {
        'outcome': 'recognized',
        'han_cer': edit_counts(han(reference), han(transcript)),
        'english_wer': edit_counts(words(reference), words(transcript)),
    }


def save_voice(directory, label, pcm):
    with io.BytesIO() as buffer:
        with wave.open(buffer, 'wb') as output:
            output.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
            output.writeframes(pcm)
        path = directory / (label + '.wav')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(buffer.getbuffer())
    return path.name


def run(root, *, save_samples):
    if platform.system() != 'Darwin':
        raise ValueError('Probe requires macOS synthesis')
    key = load_key(root / KEY_FILE)
    config = load_config(root)
    if config.asr != 'minimax':
        raise ValueError('Probe baseline requires MiniMax ASR')
    corpus = corpus_samples(read_json(CORPUS))
    recognizer = MandarinRecognizer(config=config)
    rows = []
    try:
        with TemporaryDirectory(prefix='cc8-live-fixtures-') as temporary:
            for ident in SAMPLE_IDS:
                reference = corpus[ident]['reference']
                pcm = synthesize(Path(temporary), 'Tingting', 185, reference)
                digest = fixture_digest(pcm)
                live = asyncio.run(run_live(key, config.gpt_live_voice, pcm))
                baseline = recognizer.recognize(np.frombuffer(pcm, dtype='<i2') / 32768)
                rows.append(
                    {
                        'id': ident,
                        'pcm_sha256': digest,
                        'gpt_live': score(reference, live.transcript),
                        'minimax': score(reference, baseline),
                        'gpt_live_reply_settled': live.settled,
                        'gpt_live_output_frames': len(live.pcm) // 2,
                        'gpt_live_output_caption_fragments': live.caption_fragments,
                        'gpt_live_output_segments': live.audio_segments,
                        'gpt_live_uncaptained_segments': live.uncaptained_segments,
                        'gpt_live_max_caption_overrun_ms': (
                            round(live.caption_overrun_frames * 1000 / RATE, 1)
                            if live.caption_overrun_frames is not None
                            else None
                        ),
                        'gpt_live_has_nonzero_pcm': any(live.pcm),
                    }
                )
            voice_pair = voice_samples(root, config, key, Path(temporary)) if save_samples else None
    finally:
        recognizer.cancel()
    return {'gpt_live_voice': config.gpt_live_voice, 'cases': rows, 'voice_pair': voice_pair}


def voice_samples(root, config, key, folder):
    fixture = synthesize(folder, 'Tingting', 185, VOICE_REQUEST)
    live = asyncio.run(run_live(key, config.gpt_live_voice, fixture))
    if not live.settled:
        return {'state': 'incomplete_live_reply'}
    if not any(live.pcm):
        return {'state': 'missing_audio'}
    speech = DirectSpeech(config=config)
    try:
        minimax = speech.synthesize(VOICE_LINE).astype('<i2').tobytes()
    finally:
        speech.cancel()
    if not any(minimax):
        return {'state': 'missing_audio'}
    private = private_directory(root / 'device-access/private')
    parent = private_directory(private / 'gpt-live-probes')
    with TemporaryDirectory(dir=parent) as temporary:
        scratch = Path(temporary) / 'voices'
        scratch.mkdir(mode=0o700)
        names = (save_voice(scratch, 'gpt-live', live.pcm), save_voice(scratch, 'minimax', minimax))
        destination = parent / uuid.uuid4().hex
        scratch.rename(destination)
    return {
        'state': 'saved',
        'gpt_live_caption_matches_requested_text': normalized(live.caption)
        == normalized(VOICE_LINE),
        'gpt_live': str(destination / names[0]),
        'minimax': str(destination / names[1]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', required=True, type=Path)
    parser.add_argument('--execute', action='store_true', help='Call both cloud providers')
    parser.add_argument('--save-voice', action='store_true', help='Save a synthetic voice pair')
    args = parser.parse_args(argv)
    if args.save_voice and not args.execute:
        parser.error('--save-voice requires --execute')
    if not args.execute:
        print(
            json.dumps({'scope': 'synthetic_cloud_speech', 'execute': False, 'cases': SAMPLE_IDS})
        )
        return 0
    try:
        result = run(args.runtime_root.resolve(), save_samples=args.save_voice)
    except FileNotFoundError as error:
        if error.filename == str(args.runtime_root.resolve() / KEY_FILE):
            parser.exit(2, 'Mac 缺少 OpenAI 项目密钥，未发起云端验证。\n')
        parser.exit(2, '合成语音验证需要可用的 Mac 运行环境。\n')
    except (OSError, ValueError, ImportError, RuntimeError, subprocess.SubprocessError):
        parser.exit(2, '合成语音验证未完成，请检查 Mac 上的 API 密钥、运行环境和网络。\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    passed = all(
        row['gpt_live']['outcome'] == row['minimax']['outcome'] == 'recognized'
        and row['gpt_live_reply_settled']
        and row['gpt_live_has_nonzero_pcm']
        and row['gpt_live_output_caption_fragments'] > 0
        and row['gpt_live_uncaptained_segments'] == 0
        for row in result['cases']
    )
    return 0 if passed and (not args.save_voice or result['voice_pair']['state'] == 'saved') else 2


if __name__ == '__main__':
    raise SystemExit(main())
