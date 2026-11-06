"""Local Mandarin ASR with bounded gain and no microphone file/network access."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import wave
import numpy as np
from configuration import load_config

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / '.toolchains/cc8/sensevoice-int8-2025-09-09'
MANIFEST = Path(__file__).parent / 'resources/sensevoice.json'


def selected_engine():
    return load_config().asr


def prepare_audio(samples):
    # 有界 16 kHz 单句先去直流、限制增益并补首尾静音，减少轻声和辅音被截断。
    pcm = np.asarray(samples, dtype=np.float32)
    if pcm.ndim != 1 or len(pcm) > 16000 * 13:
        raise ValueError('ASR expects bounded 16 kHz mono audio')
    if not len(pcm):
        return np.zeros(0, dtype=np.float32), 1.0
    pcm = np.nan_to_num(pcm, nan=0, posinf=0, neginf=0)
    pcm = pcm - float(np.mean(pcm))
    peak = float(np.max(np.abs(pcm)))
    rms = float(np.sqrt(np.mean(pcm**2)))
    if peak < 0.001 or rms < 0.0001:
        return np.zeros(0, dtype=np.float32), 1.0
    gain = min(6.0, max(1.0, 0.08 / rms), 0.92 / max(peak, 1e-9))
    pcm = np.clip(pcm * gain, -0.98, 0.98)
    # Keep the first/last consonants away from the decoder window boundary.
    return np.pad(pcm, (1920, 2560)), gain


def verify_model():
    manifest = json.loads(MANIFEST.read_text())
    for name, digest in manifest['sha256'].items():
        path = MODEL / name
        if not path.is_file():
            raise RuntimeError('Missing local SenseVoice model; see host/resources/sensevoice.json')
        h = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                h.update(chunk)
        if h.hexdigest() != digest:
            raise RuntimeError('Local SenseVoice checksum mismatch')


class MandarinRecognizer:
    # 按显式配置选择引擎，失败不偷偷换服务或上传更多录音。
    def __init__(self, engine=None, recognizer=None, config=None, *, whisper_model=None):
        self.engine = engine or (
            'sensevoice'
            if recognizer is not None
            else config.asr
            if config is not None
            else selected_engine()
        )
        self.last_metrics = {}
        if self.engine == 'sensevoice':
            if recognizer is None:
                verify_model()
                import sherpa_onnx

                recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(MODEL / 'model.int8.onnx'),
                    tokens=str(MODEL / 'tokens.txt'),
                    language='',
                    use_itn=True,
                    num_threads=4,
                    provider='cpu',
                    debug=False,
                )
            self.recognizer = recognizer
        elif self.engine == 'minimax':
            from minimax_client import Transport

            self.recognizer = recognizer or Transport(config=config)
        elif self.engine == 'whisper':
            os.environ['HF_HUB_OFFLINE'] = '1'
            os.environ['TRANSFORMERS_OFFLINE'] = '1'
            import mlx_whisper

            self.whisper = mlx_whisper
            if whisper_model is None:
                cache = (
                    Path.home()
                    / '.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots'
                )
                paths = sorted(p for p in cache.glob('*') if (p / 'weights.safetensors').exists())
                if not paths:
                    raise RuntimeError('Local MLX speech model is missing')
                whisper_model = paths[-1]
            self.model = str(whisper_model)
        else:
            raise ValueError('Unknown local ASR engine')

    def cancel(self):
        if self.engine == 'minimax':
            self.recognizer.cancel()

    def recognize(self, samples, *, is_current=None):
        # 云端仅接收当前单句的内存 WAV；持续音频、AEC 和即时插话仍在 Mac 本地处理。
        start = time.monotonic()
        pcm, gain = prepare_audio(samples)
        if not len(pcm):
            text = ''
        elif self.engine == 'minimax':
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(np.rint(pcm * 32768).astype('<i2').tobytes())
            wav = buffer.getvalue()
            text = (
                self.recognizer.transcribe(wav)
                if is_current is None
                else self.recognizer.transcribe(wav, is_current)
            )
        elif self.engine == 'sensevoice':
            stream = self.recognizer.create_stream()
            stream.accept_waveform(16000, pcm)
            self.recognizer.decode_stream(stream)
            text = stream.result.text
            text = re.sub(r'<\|[^|]*\|>', '', text).strip()
            text = re.sub(r'帮不[币庇比]|邦布比|班布比', 'Bumblebee', text)
        else:
            result = self.whisper.transcribe(
                pcm,
                path_or_hf_repo=self.model,
                language='zh',
                temperature=0,
                condition_on_previous_text=False,
                verbose=None,
                no_speech_threshold=0.6,
                logprob_threshold=-1.0,
                initial_prompt='普通话简体中文对话。常用名称：大黄蜂，Bumblebee，天猫精灵，MiniMax，Dot。',
            )
            text = ''.join(
                s.get('text', '')
                for s in result.get('segments', [])
                if not (s.get('no_speech_prob', 0) > 0.6 and s.get('avg_logprob', 0) < -0.8)
            ).strip()
        self.last_metrics = {
            'engine': self.engine,
            'input_seconds': round(len(samples) / 16000, 3),
            'gain_db': round(20 * float(np.log10(max(gain, 1e-9))), 1),
            'characters': len(text),
            'elapsed_ms': round((time.monotonic() - start) * 1000, 1),
        }
        return text
