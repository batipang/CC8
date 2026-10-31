"""Local streaming stop commands; no utterance endpoint or Whisper dependency.

Input is already echo-cancelled microphone PCM. The current render text is
checked separately so an assistant quoting a stop command cannot stop itself
through this fast route. General conversational interruption still uses ASR.
"""
from pathlib import Path
import re
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
MODEL=ROOT/'.toolchains/cc8/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
WORDS=Path(__file__).parent/'resources/stop-keywords.txt'
COMMANDS={'停一下','停止','暂停'}


def can_fast_stop(keyword,render_text):
    normalize=lambda text:re.sub(r'[^\w\u4e00-\u9fff]','',text)
    # Conservatively defer to the existing route while the assistant itself
    # is saying this exact command. A hardware stop button remains available.
    return keyword in COMMANDS and keyword not in normalize(render_text)


class FastStopDetector:
    def __init__(self,gain=2.0):
        if not 1<=gain<=4:raise ValueError('Fast-stop gain must be between 1 and 4')
        self.gain=float(gain)
        import sherpa_onnx
        def file(name):
            p=MODEL/name
            if not p.is_file():raise RuntimeError('Local fast-stop model missing: '+name)
            return str(p)
        self.detector=sherpa_onnx.KeywordSpotter(
            tokens=file('tokens.txt'),
            encoder=file('encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx'),
            decoder=file('decoder-epoch-13-avg-2-chunk-8-left-64.onnx'),
            joiner=file('joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx'),
            keywords_file=str(WORDS),num_threads=1,provider='cpu',
            keywords_score=1.0,keywords_threshold=.25,num_trailing_blanks=1)
        self.stream=self.detector.create_stream()
        self.samples=0;self.compute_seconds=0.0

    def reset(self):
        self.stream=self.detector.create_stream()
        self.samples=0;self.compute_seconds=0.0

    def feed(self,pcm):
        start=time.perf_counter()
        self.stream.accept_waveform(16000,np.clip(np.asarray(pcm,dtype=np.float32)*self.gain,-.98,.98))
        self.samples+=len(pcm)
        keyword=''
        while self.detector.is_ready(self.stream):
            self.detector.decode_stream(self.stream)
            result=self.detector.get_result(self.stream)
            if result:
                self.detector.reset_stream(self.stream)
                if result in COMMANDS:keyword=result
        self.compute_seconds+=time.perf_counter()-start
        return keyword
