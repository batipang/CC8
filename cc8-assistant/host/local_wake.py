"""Bilingual local wake streams; Chinese reuses stop weights, English uses BPE KWS."""

from pathlib import Path
from contextlib import nullcontext

from dsp import FRAME
from fast_stop import MODEL, ROOT, keyword_spotter
from streaming_keywords import StreamingKeywords

RESOURCES = Path(__file__).parent / 'resources'
ENGLISH_MODEL = ROOT / '.toolchains/cc8/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01'
# Match the spoken confusable on the alternate stream and discard its result.
ENGLISH_ALTERNATE_COMPETITOR = '▁BU B B LE ▁BE E @BubbleBeeSpaced'


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
            max_active_paths=8,
            keywords_score=1.0,
            keywords_threshold=0.25,
            num_trailing_blanks=1,
        )
        return cls(detector)

    def alternate(self):
        return EnglishWake(
            self.detector,
            gain=self.gain,
            lock=self.engine_lock,
            keywords=ENGLISH_ALTERNATE_COMPETITOR,
        )


def replay_english_inputs(primary, alternate, cancelled):
    """Compare identical input frames on fresh native streams outside live wake state."""
    if cancelled():
        return {'status': 'unavailable'}
    main = EnglishWake.load()
    secondary = main.alternate()
    primary_hit = alternate_hit = False
    for offset in range(0, len(primary), FRAME):
        if cancelled():
            return {'status': 'unavailable'}
        primary_hit |= main.feed(primary[offset : offset + FRAME]) == 'Bumblebee'
        alternate_hit |= secondary.feed(alternate[offset : offset + FRAME]) == 'Bumblebee'
    if cancelled():
        return {'status': 'unavailable'}
    return {'status': 'measured', 'primary_hit': primary_hit, 'alternate_hit': alternate_hit}


def replay_english_raw(pcm, cancelled):
    """Diagnose a matched raw window without admitting a live wake or cloud ASR."""
    if cancelled():
        return {'status': 'unavailable'}
    stream = EnglishWake.load()
    hit = False
    for offset in range(0, len(pcm), FRAME):
        if cancelled():
            return {'status': 'unavailable'}
        hit |= stream.feed(pcm[offset : offset + FRAME]) == 'Bumblebee'
    return {'status': 'unavailable'} if cancelled() else {'status': 'measured', 'raw_hit': hit}


def replay_chinese_inputs(streams, cancelled):
    if cancelled():
        return {'status': 'unavailable'}
    detector = keyword_spotter(MODEL)
    result = {'status': 'measured'}
    for name, frames in streams.items():
        wake = ChineseWake(detector)
        hit = False
        for offset in range(0, len(frames), FRAME):
            if cancelled():
                return {'status': 'unavailable'}
            if wake.feed(frames[offset : offset + FRAME]) == '大黄蜂':
                hit = True
                break
        result[name + '_hit'] = hit
    return {'status': 'unavailable'} if cancelled() else result


class WakeDetector:
    KEYWORDS = ChineseWake.KEYWORDS | EnglishWake.KEYWORDS
    utterance_recheck_supported = True

    def __init__(
        self,
        chinese,
        english,
        english_alternate=None,
        *,
        chinese_verifier=None,
        chinese_complete=None,
        complete_chinese_enabled=False,
    ):
        self.chinese = chinese
        self.english = english
        self.english_alternate = english_alternate
        self.chinese_verifier = chinese_verifier
        self.chinese_complete = chinese_complete
        if chinese_complete is not None:
            from wake_process_recovery import CaptureWakeRecovery

            self.complete_recovery = CaptureWakeRecovery(chinese_complete)
        else:
            self.complete_recovery = None
        self.complete_chinese_enabled = complete_chinese_enabled
        self.alternate_active = False

    @classmethod
    def load(
        cls,
        stop=None,
        *,
        english_model=ENGLISH_MODEL,
        verify_chinese=False,
        verify_chinese_complete=False,
    ):
        if verify_chinese and verify_chinese_complete:
            raise ValueError('Choose one Chinese wake verifier')
        chinese = (
            ChineseWake(stop.detector, lock=stop.engine_lock)
            if stop is not None
            else ChineseWake(keyword_spotter(MODEL))
        )
        english = EnglishWake.load(english_model)
        verifier = None
        if verify_chinese and stop is not None:
            try:
                from chinese_wake_verifier import ChineseWakeVerifier
                from wake_verifier_process import WakeVerifierProcess

                verifier = ChineseWakeVerifier(
                    ChineseWake(stop.detector, lock=stop.engine_lock), WakeVerifierProcess()
                )
            except (ImportError, OSError, RuntimeError, ValueError):
                # An optional model or stream must never disable standard wake.
                pass
        complete = None
        if verify_chinese_complete:
            from recognition import verify_model
            from complete_wake_process import CompleteWakeVerifierProcess

            verify_model()
            complete = CompleteWakeVerifierProcess()
        return cls(
            chinese,
            english,
            english.alternate(),
            chinese_verifier=verifier,
            chinese_complete=complete,
            complete_chinese_enabled=verify_chinese_complete,
        )

    def start_capture(self):
        if self.chinese_verifier is not None:
            self.chinese_verifier.worker.start()
        if self.chinese_complete is not None:
            self.complete_recovery.start_capture()

    def stop_capture(self):
        if self.chinese_verifier is not None:
            self.chinese_verifier.worker.close()
        if self.chinese_complete is not None:
            self.complete_recovery.stop_capture()

    def poll_capture(self):
        if self.complete_recovery is not None:
            self.complete_recovery.poll_capture()

    @property
    def complete_recovery_generation(self):
        return self.complete_recovery.generation if self.complete_recovery is not None else 0

    @property
    def complete_recovery_snapshot(self):
        return self.complete_recovery.snapshot if self.complete_recovery is not None else None

    def complete_recovery_commit(self):
        return (
            self.complete_recovery.commit_guard()
            if self.complete_recovery is not None
            else nullcontext()
        )

    def recheck_chinese(self, pcm, current):
        if not self.complete_chinese_enabled or self.chinese_complete is None or not current():
            return None
        recovery = getattr(self, 'complete_recovery', None)
        lease = recovery.generation if recovery is not None else 0
        if recovery is not None and not recovery.available:
            return None
        verdict = self.chinese_complete.classify_complete(pcm)
        if (
            verdict is None
            or not current()
            or (recovery is not None and recovery.generation != lease)
        ):
            return None
        target, confusable = verdict
        return target or not confusable

    def recheck_utterance(self, pcm, current):
        from wake_utterance import recheck_utterance

        return recheck_utterance(pcm, self.english.alternate, current)

    def reset(self, *, clear_metrics=True):
        self.chinese.reset(clear_metrics=clear_metrics)
        self.english.reset(clear_metrics=clear_metrics)
        if self.english_alternate is not None:
            self.english_alternate.reset(clear_metrics=clear_metrics)
        if self.chinese_verifier is not None:
            self.chinese_verifier.reset(clear_metrics=clear_metrics)
        self.alternate_active = False

    def feed(self, pcm, *, alternate_pcm=None):
        # Each native lock is released before entering the next; no callbacks.
        chinese = self.chinese.feed(pcm)
        if self.chinese_verifier is not None:
            chinese = self.chinese_verifier.feed(chinese, pcm, alternate_pcm)
        english = self.english.feed(pcm)
        alternate = ''
        if self.english_alternate is not None:
            if alternate_pcm is not None:
                alternate = self.english_alternate.feed(alternate_pcm)
                self.alternate_active = True
            elif self.alternate_active:
                # Do not stitch speech across a playback or source gap.
                self.english_alternate.reset(clear_metrics=False)
                self.alternate_active = False
        keyword = chinese or english or alternate
        if keyword and self.chinese_verifier is not None:
            self.chinese_verifier.reset()
        return keyword

    @property
    def samples(self):
        return min(self.chinese.samples, self.english.samples)

    @property
    def english_input_samples(self):
        alternate = self.english_alternate
        return self.english.samples, alternate.samples if alternate is not None else None

    @property
    def english_stream_samples(self):
        alternate = self.english_alternate
        if alternate is None:
            return None
        return self.english.stream_samples, alternate.stream_samples

    @property
    def compute_seconds(self):
        alternate = self.english_alternate
        verifier = self.chinese_verifier
        return (
            self.chinese.compute_seconds
            + self.english.compute_seconds
            + (alternate.compute_seconds if alternate is not None else 0.0)
            + (
                verifier.alternate.compute_seconds + verifier.decode_seconds
                if verifier is not None
                else 0.0
            )
        )
