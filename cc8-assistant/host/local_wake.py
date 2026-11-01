"""Bilingual local wake streams; Chinese reuses stop weights, English uses BPE KWS."""

from pathlib import Path
from fast_stop import MODEL, ROOT, keyword_spotter
from streaming_keywords import StreamingKeywords

RESOURCES = Path(__file__).parent / 'resources'
ENGLISH_MODEL = ROOT / '.toolchains/cc8/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01'


class ChineseWake(StreamingKeywords):
    KEYWORDS = frozenset(('大黄蜂',))

    def __init__(self, detector, gain=2.0, *, lock=None):
        phrases = (RESOURCES / 'wake-chinese-keywords.txt').read_text().strip()
        if not phrases.endswith('@大黄蜂') or '\n' in phrases:
            raise ValueError('Invalid Chinese wake resource')
        super().__init__(detector, gain, lock=lock, keywords=phrases)


class EnglishWake(StreamingKeywords):
    KEYWORDS = frozenset(('Bumblebee',))

    @classmethod
    def load(cls, model=ENGLISH_MODEL):
        import sherpa_onnx

        def file(name):
            path = model / name
            if not path.is_file():
                raise RuntimeError('Local English wake model missing: ' + name)
            return str(path)

        detector = sherpa_onnx.KeywordSpotter(
            tokens=file('tokens.txt'),
            encoder=file('encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),
            decoder=file('decoder-epoch-12-avg-2-chunk-16-left-64.onnx'),
            joiner=file('joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),
            keywords_file=str(RESOURCES / 'wake-english-keywords.txt'),
            num_threads=1,
            provider='cpu',
            keywords_score=1.0,
            keywords_threshold=0.25,
            num_trailing_blanks=1,
        )
        return cls(detector)


class WakeDetector:
    KEYWORDS = ChineseWake.KEYWORDS | EnglishWake.KEYWORDS

    def __init__(self, chinese, english):
        self.chinese = chinese
        self.english = english

    @classmethod
    def load(cls, stop=None, *, english_model=ENGLISH_MODEL):
        chinese = (
            ChineseWake(stop.detector, lock=stop.engine_lock)
            if stop is not None
            else ChineseWake(keyword_spotter(MODEL))
        )
        return cls(chinese, EnglishWake.load(english_model))

    def reset(self, *, clear_metrics=True):
        self.chinese.reset(clear_metrics=clear_metrics)
        self.english.reset(clear_metrics=clear_metrics)

    def feed(self, pcm):
        # Each native lock is released before entering the next; no callbacks.
        chinese = self.chinese.feed(pcm)
        english = self.english.feed(pcm)
        return chinese or english

    @property
    def samples(self):
        return min(self.chinese.samples, self.english.samples)

    @property
    def compute_seconds(self):
        return self.chinese.compute_seconds + self.english.compute_seconds
