"""Native echo-cancellation adapters for fixed-size host PCM frames."""

import ctypes
import numpy as np

from audio_signal import FRAME, RATE


class EchoCanceller:
    def __init__(self, library, residual=True):
        self.lib = ctypes.CDLL(str(library))
        self.ptr = ctypes.POINTER(ctypes.c_int16)
        self.lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        self.lib.speex_echo_state_init.restype = ctypes.c_void_p
        self.lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.lib.speex_echo_ctl.restype = ctypes.c_int
        self.lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p, self.ptr, self.ptr, self.ptr]
        self.lib.speex_echo_cancellation.restype = None
        self.lib.speex_echo_state_destroy.argtypes = [ctypes.c_void_p]
        self.lib.speex_echo_state_destroy.restype = None
        self.lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        self.lib.speex_preprocess_state_init.restype = ctypes.c_void_p
        self.lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.lib.speex_preprocess_ctl.restype = ctypes.c_int
        self.lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, self.ptr]
        self.lib.speex_preprocess_run.restype = ctypes.c_int
        self.lib.speex_preprocess_state_destroy.argtypes = [ctypes.c_void_p]
        self.lib.speex_preprocess_state_destroy.restype = None
        self.preprocessor = None
        self.state = self.lib.speex_echo_state_init(FRAME, RATE // 4)
        if not self.state:
            raise RuntimeError('Cannot allocate software AEC')
        rate = ctypes.c_int(RATE)
        if self.lib.speex_echo_ctl(self.state, 24, ctypes.byref(rate)):
            self.close()
            raise RuntimeError('Cannot configure software AEC')
        if residual:
            # SpeexDSP's own testecho.c chains this after linear AEC and
            # passes the echo-state pointer directly (not pointer-to-pointer).
            # Without it, intelligible residual echo still reaches ASR/VAD.
            self.preprocessor = self.lib.speex_preprocess_state_init(FRAME, RATE)
            try:
                if not self.preprocessor:
                    raise RuntimeError('Cannot allocate residual echo suppression')
                if self.lib.speex_preprocess_ctl(self.preprocessor, 24, self.state):
                    raise RuntimeError('Cannot link residual echo suppression')
                for request, value in [(0, 1), (2, 0), (18, -12), (20, -45), (22, -10)]:
                    setting = ctypes.c_int(value)
                    if self.lib.speex_preprocess_ctl(
                        self.preprocessor, request, ctypes.byref(setting)
                    ):
                        raise RuntimeError('Cannot configure residual echo suppression')
            except Exception:
                self.close()
                raise

    def process(self, mic, reference):
        if len(mic) != FRAME or len(reference) != FRAME:
            raise ValueError('AEC needs exactly 10 ms in both directions')
        rec = np.rint(np.clip(mic, -1, 32767 / 32768) * 32768).astype(np.int16)
        ref = np.rint(np.clip(reference, -1, 32767 / 32768) * 32768).astype(np.int16)
        out = np.empty(FRAME, dtype=np.int16)
        self.lib.speex_echo_cancellation(
            self.state,
            rec.ctypes.data_as(self.ptr),
            ref.ctypes.data_as(self.ptr),
            out.ctypes.data_as(self.ptr),
        )
        if self.preprocessor:
            self.lib.speex_preprocess_run(self.preprocessor, out.ctypes.data_as(self.ptr))
        return out.astype(np.float64) / 32768

    def close(self):
        if self.preprocessor:
            self.lib.speex_preprocess_state_destroy(self.preprocessor)
            self.preprocessor = None
        if self.state:
            self.lib.speex_echo_state_destroy(self.state)
            self.state = None


class WebRtcEchoCanceller:
    # 输入/输出为归一化单声道，每块固定 160 个采样，即 16 kHz 下的 10 ms。
    """AEC3 + moderate NS; normalized PCM in/out, no device access or AGC."""

    def __init__(self, noise_suppression=True):
        from pywebrtc_audio import AudioProcessor

        self.processor = AudioProcessor(
            sample_rate=RATE,
            echo_cancellation=True,
            noise_suppression=noise_suppression,
            auto_gain_control=False,
            ns_level=1,
            stream_delay_ms=20,
        )
        self.speech_probability = 0.0

    def process(self, mic, reference):
        if len(mic) != FRAME or len(reference) != FRAME:
            raise ValueError('AEC3 needs exactly 10 ms in both directions')
        if self.processor is None:
            raise RuntimeError('AEC3 already closed')
        rec = np.rint(np.clip(mic, -1, 32767 / 32768) * 32768).astype(np.int16)
        ref = np.rint(np.clip(reference, -1, 32767 / 32768) * 32768).astype(np.int16)
        out = self.processor.process(rec, ref)
        self.speech_probability = float(self.processor.speech_probability)
        return out.astype(np.float64) / 32768

    def close(self):
        self.processor = None
