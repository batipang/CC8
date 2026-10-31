"""Streaming AEC and conservative energy VAD. No files or network access."""
import ctypes
import difflib
import re
from collections import deque
import numpy as np

RATE = 16000
FRAME = 160


def level(samples):
    return float(10 * np.log10(max(float(np.mean(samples * samples)), 1e-12)))


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
                if not self.preprocessor:raise RuntimeError('Cannot allocate residual echo suppression')
                if self.lib.speex_preprocess_ctl(self.preprocessor,24,self.state):raise RuntimeError('Cannot link residual echo suppression')
                for request,value in [(0,1),(2,0),(18,-12),(20,-45),(22,-10)]:
                    setting=ctypes.c_int(value)
                    if self.lib.speex_preprocess_ctl(self.preprocessor,request,ctypes.byref(setting)):raise RuntimeError('Cannot configure residual echo suppression')
            except Exception:
                self.close();raise

    def process(self, mic, reference):
        if len(mic) != FRAME or len(reference) != FRAME:
            raise ValueError('AEC needs exactly 10 ms in both directions')
        rec = np.rint(np.clip(mic, -1, 32767/32768)*32768).astype(np.int16)
        ref = np.rint(np.clip(reference, -1, 32767/32768)*32768).astype(np.int16)
        out = np.empty(FRAME, dtype=np.int16)
        self.lib.speex_echo_cancellation(self.state, rec.ctypes.data_as(self.ptr), ref.ctypes.data_as(self.ptr), out.ctypes.data_as(self.ptr))
        if self.preprocessor:self.lib.speex_preprocess_run(self.preprocessor,out.ctypes.data_as(self.ptr))
        return out.astype(np.float64)/32768

    def close(self):
        if self.preprocessor:
            self.lib.speex_preprocess_state_destroy(self.preprocessor)
            self.preprocessor = None
        if self.state:
            self.lib.speex_echo_state_destroy(self.state)
            self.state = None


class RenderTimeline:
    def __init__(self):
        self.blocks = deque()
        self.anchors = {}
        self.discontinuities = 0
        self.paused_at=None;self.held=[]

    def pause_at(self,nanos):
        if self.paused_at is not None:return
        self.paused_at=nanos;old=self.blocks;self.blocks=deque();self.held=[]
        for stamp,pcm in old:self._append(stamp,pcm)

    def resume_at(self,nanos):
        if self.paused_at is None:return
        gap=max(0,nanos-self.paused_at)
        self.blocks.extend((stamp+gap,pcm) for stamp,pcm in self.held)
        self.anchors={key:value+gap for key,value in self.anchors.items()}
        self.paused_at=None;self.held=[]

    def cancel_pause(self):self.paused_at=None;self.held=[]

    def _append(self,stamp,pcm):
        if self.paused_at is None:self.blocks.append((stamp,pcm));return
        split=max(0,min(len(pcm),round((self.paused_at-stamp)*RATE/1e9)))
        if split:self.blocks.append((stamp,pcm[:split]))
        if split<len(pcm):self.held.append((stamp+round(split*1e9/RATE),pcm[split:]))

    def add(self, generation, frame, nanos, pcm):
        # A single anchor per playback keeps packet scheduling jitter from
        # producing gaps in the playback reference. Timestamp refinements
        # are bounded; old reference remains for the acoustic tail on cancel.
        key = generation
        observed = nanos - frame*1e9/RATE
        anchor = self.anchors.get(key)
        if frame == 0 or anchor is None:
            anchor = observed
        elif abs(observed-anchor) > 40e6:
            # A cloud/USB stall can drain AudioTrack. The next samples then
            # really play later. Ignoring this jump invents continuous audio
            # during the gap and misaligns every subsequent AEC frame.
            anchor=observed;self.discontinuities+=1
        else:
            anchor += .1*(observed-anchor)
        self.anchors[key] = anchor
        self._append(int(anchor+frame*1e9/RATE), pcm.copy()*.35)
        while self.blocks and nanos-self.blocks[0][0] > 12e9:
            self.blocks.popleft()
        if len(self.anchors) > 64:
            self.anchors = {key: anchor}

    def at(self, nanos, count):
        out = np.zeros(count)
        for stamp, block in tuple(self.blocks):
            offset = round((stamp-nanos)*RATE/1e9)
            left = max(0, offset)
            right = min(count, offset+len(block))
            if left < right:
                out[left:right] = block[left-offset:right-offset]
        return out


class WebRtcEchoCanceller:
    """AEC3 + moderate NS; normalized PCM in/out, no device access or AGC."""
    def __init__(self,noise_suppression=True):
        from pywebrtc_audio import AudioProcessor
        self.processor=AudioProcessor(sample_rate=RATE,echo_cancellation=True,
            noise_suppression=noise_suppression,auto_gain_control=False,ns_level=1,stream_delay_ms=20)
        self.speech_probability=0.0
    def process(self,mic,reference):
        if len(mic)!=FRAME or len(reference)!=FRAME:raise ValueError('AEC3 needs exactly 10 ms in both directions')
        if self.processor is None:raise RuntimeError('AEC3 already closed')
        rec=np.rint(np.clip(mic,-1,32767/32768)*32768).astype(np.int16)
        ref=np.rint(np.clip(reference,-1,32767/32768)*32768).astype(np.int16)
        out=self.processor.process(rec,ref)
        self.speech_probability=float(self.processor.speech_probability)
        return out.astype(np.float64)/32768
    def close(self):self.processor=None


class ReferenceCapture:
    """Capture the render window corresponding to VAD's bounded utterance.

    This is playback PCM already held on the Mac, not another microphone or
    recording. Retaining it with the job avoids timeline expiry during ASR.
    """
    def __init__(self):self.reset()
    def reset(self):self.pre=deque(maxlen=50);self.frames=[];self.active=False
    def feed(self,reference,began,finished):
        if not self.active:
            self.pre.append(reference.copy())
            if began:self.frames=list(self.pre);self.pre.clear();self.active=True
        else:self.frames.append(reference.copy())
        if finished:
            output=np.concatenate(self.frames) if self.frames else np.zeros(0)
            self.reset();return output
        if len(self.frames)>1200:self.reset();raise ValueError('Render capture exceeds VAD bound')
        return None


def estimate_reference_shift(mic, reference):
    """Estimate a causal filter lead-in from a user-visible silent calibration.

    Negative shifts need future reference blocks, supplied by the bridge's
    explicit 80 ms microphone lookahead. This is an alignment estimate, not
    a conversation latency measurement.
    """
    count=min(len(mic),len(reference),RATE*5)
    if count<RATE or level(reference[:count]) < -70:return 0,0.0
    y=mic[:count]-np.mean(mic[:count]);x=reference[:count]-np.mean(reference[:count])
    size=1<<(2*count-1).bit_length()
    corr=np.fft.irfft(np.fft.rfft(y,size)*np.conj(np.fft.rfft(x,size)),size)
    yy=np.r_[0,np.cumsum(y*y)];xx=np.r_[0,np.cumsum(x*x)]
    lags=np.arange(-RATE//2,RATE//2+1)
    energy_y=np.where(lags>=0,yy[count]-yy[np.maximum(lags,0)],yy[count+np.minimum(lags,0)])
    energy_x=np.where(lags>=0,xx[count-np.maximum(lags,0)],xx[count]-xx[-np.minimum(lags,0)])
    denominator=np.sqrt(np.maximum(0,energy_y*energy_x))
    coefficients=np.divide(corr[lags%size],denominator,out=np.zeros_like(denominator),where=denominator>0)
    best=int(np.argmax(np.abs(coefficients)));peak=float(abs(coefficients[best]));lag=int(lags[best])
    # Leave 20 ms of causal room response within the 250 ms adaptive filter.
    shift=lag-RATE//50
    shift=max(-RATE*60//1000,min(RATE*400//1000,shift))
    return (shift,peak) if peak>=.15 else (0,peak)


def resembles_playback(text, spoken):
    """Reject decoded playback fragments before creating a conversational turn.

    Explicit stop commands take priority. Short acknowledgements while the
    assistant is speaking are deliberately conservative in this prototype.
    """
    clean=lambda s:re.sub(r'[^\w\u4e00-\u9fff]','',s).lower()
    candidate=clean(text)
    if candidate in {'停','停下','停一下','停止','暂停','打住','等等','别说了','听我说'}:return False
    if len(candidate)<=2:return True
    # Spoken percentages and numbers often use Chinese characters even when
    # the displayed sentence contains Arabic digits. Only use the numeric
    # variant when enough non-numeric words remain to identify the phrase.
    without_numbers=lambda value:re.sub(r'[0-9零〇一二三四五六七八九十百千万亿两点]', '',value.replace('百分之',''))
    for line in [*spoken,''.join(spoken)]:
        reference=clean(line)
        if not reference:continue
        question_markers=('为什么','为何','怎么','什么','多少','哪个','哪些','是否','是不是','吗','呢')
        novel_question=any(marker in candidate and marker not in reference for marker in question_markers)
        if novel_question:continue
        if candidate in reference:return True
        if len(candidate)>=4:
            longest=difflib.SequenceMatcher(None,candidate,reference).find_longest_match()
            if longest.size/len(candidate)>=.8:return True
            if difflib.SequenceMatcher(None,candidate,reference).ratio()>=.78:return True
            blocks=difflib.SequenceMatcher(None,candidate,reference).get_matching_blocks()
            if longest.size>=max(4,len(candidate)*.35) and sum(b.size for b in blocks)/len(candidate)>=.9:return True
            # Actual CC8 echo substituted several separated Chinese words,
            # leaving no long contiguous match. Preserve new question intent
            # before using the more tolerant, ordered-coverage comparison.
            if not novel_question and len(candidate)>=10 and longest.size>=6 and sum(b.size for b in blocks)/len(candidate)>=.78:return True
            a=without_numbers(candidate);b=without_numbers(reference)
            if len(a)>=6 and a in b:return True
    return False


class VoiceDetector:
    """Energy VAD for prototype testing, not a semantic speech detector."""
    def __init__(self):
        self.pre = deque(maxlen=50)
        self.audio = []
        self.hot = self.quiet = 0
        self.talking = False
        self.noise = -55.0
        self.external_voice=None

    def reset(self):
        self.pre.clear(); self.audio.clear()
        self.hot = self.quiet = 0
        self.talking = False

    def feed(self, pcm):
        db = level(pcm)
        threshold = max(-40.0, min(-32.0, self.noise+10))
        voiced = db > threshold if self.external_voice is None else self.external_voice
        started = False
        result = None
        if not self.talking:
            self.pre.append(pcm.copy())
            self.hot = self.hot+1 if voiced else max(0, self.hot-2)
            if not voiced:
                self.noise = .995*self.noise + .005*db
            if self.hot >= 12:  # 120 ms onset, with a 500 ms pre-roll; not end-to-end latency.
                self.talking = True
                self.audio = list(self.pre)
                self.pre.clear()
                self.quiet = 0
                started = True
        else:
            self.audio.append(pcm.copy())
            quiet = db < threshold-3 if self.external_voice is None else not voiced
            self.quiet = self.quiet+1 if quiet else 0
            if self.quiet >= 80 or len(self.audio) >= 1200:
                result = np.concatenate(self.audio)
                self.reset()
        return started, result, db


class NearSpeechDetector:
    """Local WebRTC VAD plus residual energy/correlation; not speaker identity."""
    def __init__(self,vad=None):
        if vad is None:
            import webrtcvad
            vad=webrtcvad.Vad(2)
        self.vad=vad;self.reset()
    def reset(self):
        self.votes=deque(maxlen=20);self.raw=deque(maxlen=20);self.render=deque(maxlen=20);self.residual=deque(maxlen=20)
        self.noise=-58.;self.voiced=False;self.credible=False;self.correlation=0.;self.residual_correlation=0.
    @staticmethod
    def coherence(signal,reference):
        # Search +/-50 ms as the room path and playback clock need not line up
        # at zero lag. Decimation keeps this inexpensive at 100 frames/second.
        a=signal[::4];b=reference[::4];n=len(a)
        if n<80:return 0.
        cross=np.correlate(a,b,'full');lags=np.arange(-min(200,n//3),min(200,n//3)+1)
        sa=np.r_[0.,np.cumsum(a*a)];sb=np.r_[0.,np.cumsum(b*b)]
        start_a=np.maximum(0,lags);end_a=n+np.minimum(0,lags)
        start_b=np.maximum(0,-lags);end_b=n-np.maximum(0,lags)
        energy=np.sqrt((sa[end_a]-sa[start_a])*(sb[end_b]-sb[start_b]))
        return float(np.max(np.abs(cross[lags+n-1])/np.maximum(energy,1e-9)))
    def feed(self,cleaned,mic,reference):
        if any(len(x)!=FRAME for x in (cleaned,mic,reference)):raise ValueError('Near speech requires 10 ms frames')
        db=level(cleaned);threshold=max(-46.,min(-36.,self.noise+10))
        raw=np.rint(np.clip(cleaned*1.5,-.98,.98)*32768).astype('<i2').tobytes()
        speech=bool(self.vad.is_speech(raw,RATE))
        if not speech:self.noise=.995*self.noise+.005*db
        self.raw.append(mic.copy());self.render.append(reference.copy());self.residual.append(cleaned.copy())
        rec=np.concatenate(self.raw);ref=np.concatenate(self.render)
        has_render=level(ref)>-60
        self.correlation=self.coherence(rec,ref) if has_render else 0.
        self.residual_correlation=self.coherence(np.concatenate(self.residual),ref) if has_render else 0.
        # Require the residual itself to match the speaker too: correlated raw
        # mic alone cannot distinguish an ordinary voice over louder playback.
        echo_only=has_render and self.correlation>.78 and self.residual_correlation>.60 and db<level(rec)-6
        self.voiced=speech and db>threshold and not echo_only
        self.votes.append(self.voiced)
        self.credible=len(self.votes)>=16 and sum(self.votes)>=12 and self.voiced
        return self.voiced,self.credible
