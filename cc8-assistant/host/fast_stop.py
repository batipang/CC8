"""Local streaming stop commands; no utterance endpoint or Whisper dependency.

Input is already echo-cancelled microphone PCM. The current render text is
checked separately so an assistant quoting a stop command cannot stop itself
through this fast route. General conversational interruption still uses ASR.
"""

from pathlib import Path
import re
from streaming_keywords import StreamingKeywords, validated_gain

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / '.toolchains/cc8/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
WORDS = Path(__file__).parent / 'resources/stop-keywords.txt'
COMMANDS = {'停一下', '停止', '暂停'}


def can_fast_stop(keyword, render_text):
    # 播报本身也可能含“停一下”，先排除参考文本，避免把助理自己的话当作口令。
    normalize = lambda text: re.sub(r'[^\w\u4e00-\u9fff]', '', text)
    # Conservatively defer to the existing route while the assistant itself
    # is saying this exact command. A hardware stop button remains available.
    return keyword in COMMANDS and keyword not in normalize(render_text)


def keyword_spotter(model=MODEL):
    import sherpa_onnx

    def file(name):
        p = model / name
        if not p.is_file():
            raise RuntimeError('Local fast-stop model missing: ' + name)
        return str(p)

    return sherpa_onnx.KeywordSpotter(
        tokens=file('tokens.txt'),
        encoder=file('encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx'),
        decoder=file('decoder-epoch-13-avg-2-chunk-8-left-64.onnx'),
        joiner=file('joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx'),
        keywords_file=str(WORDS),
        num_threads=1,
        provider='cpu',
        keywords_score=1.0,
        keywords_threshold=0.25,
        num_trailing_blanks=1,
    )


class FastStopDetector(StreamingKeywords):
    # 停止与唤醒拥有独立流；只共享不可变模型及串行 native 访问锁。
    KEYWORDS = frozenset(COMMANDS)

    def __init__(self, gain=2.0, *, model=MODEL):
        gain = validated_gain(gain)
        super().__init__(keyword_spotter(model), gain)
