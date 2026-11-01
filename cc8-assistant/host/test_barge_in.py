import json
import threading
import time
import unittest
import numpy as np

from bridge import Bridge,Reply
from dsp import FRAME,RATE,NearSpeechDetector,RenderTimeline,VoiceDetector
from protocol import EVENT,SPEAKER,receive
from test_runtime import Fragmented


class Sink:
    def __init__(self):self.packets=[]
    def sendall(self,data):self.packets.append(receive(Fragmented(data)))


class BargeInTests(unittest.TestCase):
    def bridge(self,speech=None):
        b=Bridge(speech,None);b.connection=Sink();b.active=True;b.device_can_pause=True;b.playing=True;b.save_status=lambda:None
        return b
    def events(self,b):return [json.loads(p.data) for p in b.connection.packets if p.kind==EVENT]
    def test_tentative_pause_keeps_generation_and_can_resume(self):
        b=self.bridge();generation=b.generation;token=b.pause_for_voice()
        self.assertIsNotNone(token);self.assertEqual(b.generation,generation);self.assertTrue(b.playing)
        b.resume_after_rejection(token,generation)
        self.assertIsNone(b.pause_state);self.assertEqual(b.generation,generation)
        self.assertEqual([e['event'] for e in self.events(b)],['playback_pause','playback_resume'])
    def test_cancel_prevents_late_rejection_from_resuming(self):
        b=self.bridge();generation=b.generation;token=b.pause_for_voice();b.cancel('new_question')
        b.resume_after_rejection(token,generation)
        self.assertFalse(any(e['event']=='playback_resume' for e in self.events(b)))
        self.assertFalse(b.valid(generation))
    def test_earlier_rejection_cannot_resume_over_a_new_speech_segment(self):
        b=self.bridge();token=b.pause_for_voice();b.vad.talking=True;b.turn_pause_token=token
        b.resume_after_rejection(token,b.generation);self.assertIsNotNone(b.pause_state)
        b.vad.talking=False;b.resume_after_rejection(token,b.generation);self.assertIsNone(b.pause_state)
    def test_unsupported_device_and_inactive_mic_do_not_pause(self):
        b=self.bridge();b.device_can_pause=False;self.assertIsNone(b.pause_for_voice())
        b.device_can_pause=True;b.active=False;self.assertIsNone(b.pause_for_voice())
    def test_old_pause_ack_does_not_retime_new_audio(self):
        b=self.bridge();token=b.pause_for_voice();old=b.generation;b.cancel('next')
        b.on_event({'event':'playback_paused','token':token,'generation':old,'nanos':1000})
        self.assertIsNone(b.timeline.paused_at)
    def test_false_recognition_resumes_without_transcript_or_reply(self):
        class Speech:
            def recognize(self,pcm):return '这是刚刚播报的参考句'
        b=self.bridge(Speech());token=b.pause_for_voice();generation=b.generation
        b.queue.put((generation,np.ones(RATE),True,['这是刚刚播报的参考句。'],{'echo_risk':True,'captured_at':time.monotonic(),'pause_token':token}))
        deadline=time.monotonic()+1
        while b.pause_state and time.monotonic()<deadline:time.sleep(.01)
        self.assertIsNone(b.pause_state);self.assertEqual(b.generation,generation)
        self.assertFalse(any(e['event']=='transcript' for e in self.events(b)))
    def test_empty_asr_resumes_and_new_question_cancels_old_turn(self):
        class Speech:
            def recognize(self,pcm):return '给我讲个笑话'
        b=self.bridge(Speech());token=b.pause_for_voice();old=b.generation;answered=threading.Event()
        b.speak=lambda text,generation:(answered.set() or True)
        b.queue.put((old,np.ones(RATE),True,['今天讲一段故事'],{'echo_risk':True,'captured_at':time.monotonic(),'pause_token':token}))
        self.assertTrue(answered.wait(1));self.assertGreater(b.generation,old)
        self.assertFalse(any(e['event']=='playback_resume' for e in self.events(b)))
    def test_asr_can_run_while_old_speech_waits_for_pause_resolution(self):
        entered=threading.Event();recognized=threading.Event();release=threading.Event()
        class Speech:
            def recognize(self,pcm):recognized.set();return ''
        b=self.bridge(Speech());old=b.generation;token=b.pause_for_voice()
        def waiting_response(text,generation):entered.set();b.wait_playback(generation);release.set()
        b.answer=waiting_response;b.enqueue_response(Reply(old,'旧回答'));self.assertTrue(entered.wait(1))
        b.queue.put((old,np.ones(RATE),True,[],{'echo_risk':True,'captured_at':time.monotonic(),'pause_token':token}))
        self.assertTrue(recognized.wait(1));self.assertTrue(release.wait(1))
    def test_timeout_stops_instead_of_resuming_unconfirmed_audio(self):
        b=self.bridge();b.pause_for_voice();b.pause_state['started']-=21;old=b.generation;b.wait_playback(old)
        self.assertGreater(b.generation,old);self.assertIsNone(b.pause_state)
    def test_asr_failure_stops_old_audio_instead_of_resuming(self):
        class Speech:
            def recognize(self,pcm):raise RuntimeError('recognizer unavailable')
        b=self.bridge(Speech());token=b.pause_for_voice();old=b.generation
        b.queue.put((old,np.ones(RATE),True,[],{'echo_risk':True,'captured_at':time.monotonic(),'pause_token':token}))
        deadline=time.monotonic()+1
        while b.generation==old and time.monotonic()<deadline:time.sleep(.01)
        self.assertGreater(b.generation,old);self.assertFalse(b.playing)
        self.assertFalse(any(e['event']=='playback_resume' for e in self.events(b)))
    def test_no_old_pcm_is_sent_while_paused(self):
        b=self.bridge();b.pause_for_voice();done=threading.Event()
        def play():b.play_stream(iter([np.ones(320,dtype=np.int16)]),'test',b.generation);done.set()
        thread=threading.Thread(target=play,daemon=True);thread.start();time.sleep(.05)
        self.assertFalse(any(p.kind==SPEAKER for p in b.connection.packets))
        b.cancel('stop');self.assertTrue(done.wait(1))


class AcousticGateTests(unittest.TestCase):
    class SpeechVad:
        def __init__(self,value):self.value=value
        def is_speech(self,*args):return self.value
    def test_energy_without_speech_and_coherent_echo_do_not_qualify(self):
        wave=np.sin(np.arange(FRAME)*.4)*.1
        for detector in [NearSpeechDetector(self.SpeechVad(False)),NearSpeechDetector(self.SpeechVad(True))]:
            for _ in range(40):detector.feed(wave*.1,wave,wave)
            self.assertFalse(detector.credible)
    def test_sustained_independent_speech_qualifies_but_click_does_not(self):
        detector=NearSpeechDetector(self.SpeechVad(True));rng=np.random.default_rng(0)
        detector.feed(rng.normal(0,.04,FRAME),rng.normal(0,.04,FRAME),np.zeros(FRAME));self.assertFalse(detector.credible)
        for _ in range(20):
            frame=rng.normal(0,.04,FRAME);detector.feed(frame,frame,np.zeros(FRAME))
        self.assertTrue(detector.credible)
    def test_delayed_echo_rejected_without_suppressing_double_talk(self):
        rng=np.random.default_rng(21);render=rng.normal(0,.06,FRAME*60);echo=np.r_[np.zeros(320),render[:-320]]*.5
        detector=NearSpeechDetector(self.SpeechVad(True))
        for i in range(0,len(render),FRAME):detector.feed(echo[i:i+FRAME]*.2,echo[i:i+FRAME],render[i:i+FRAME])
        self.assertFalse(detector.credible);self.assertGreater(detector.residual_correlation,.95)
        # Raw mic remains dominated by echo, but AEC output contains the user's
        # independent ordinary voice. It must still qualify for interruption.
        for i in range(0,len(render),FRAME):
            speech=rng.normal(0,.008,FRAME);ref=render[i:i+FRAME];mic=ref+speech
            detector.feed(speech,mic,ref)
        self.assertTrue(detector.credible)
    def test_soft_speech_uses_vad_for_end_of_utterance(self):
        detector=VoiceDetector();onsets=0;ends=0
        for _ in range(100):
            detector.external_voice=True;began,done,_=detector.feed(np.ones(FRAME)*.005);onsets+=began;ends+=done is not None
        self.assertEqual(onsets,1);self.assertEqual(ends,0)
        for _ in range(85):
            detector.external_voice=False;_,done,_=detector.feed(np.zeros(FRAME));ends+=done is not None
        self.assertEqual(ends,1)
    @staticmethod
    def acoustic_frame(db,seed=81):
        frame=np.random.default_rng(seed).normal(0,1,FRAME)
        return frame/np.sqrt(np.mean(frame*frame))*10**(db/20)
    def test_weak_background_is_rejected_even_if_vad_misclassifies_it(self):
        detector=NearSpeechDetector(self.SpeechVad(True));utterance=VoiceDetector();begins=0
        for i in range(100):
            frame=self.acoustic_frame(-44,i);voiced,credible=detector.feed(frame,frame,np.zeros(FRAME))
            utterance.external_voice=voiced;began,_,_=utterance.feed(frame);begins+=began
            self.assertFalse(voiced);self.assertFalse(credible)
        self.assertEqual(begins,0)
    def test_short_speech_like_transient_cannot_start_a_turn_or_pause(self):
        detector=NearSpeechDetector(self.SpeechVad(True));utterance=VoiceDetector();begins=0
        for i in range(40):
            frame=self.acoustic_frame(-32,i) if 5<=i<18 else np.zeros(FRAME)
            voiced,credible=detector.feed(frame,frame,np.zeros(FRAME))
            utterance.external_voice=voiced;began,_,_=utterance.feed(frame);begins+=began
            self.assertFalse(credible)
        self.assertEqual(begins,0)
    def test_ordinary_and_soft_clear_speech_still_start_and_confirm(self):
        for db in (-38,-42):
            with self.subTest(db=db):
                detector=NearSpeechDetector(self.SpeechVad(True));utterance=VoiceDetector();begins=0;confirmed=False
                for i in range(40):
                    frame=self.acoustic_frame(db,i);voiced,credible=detector.feed(frame,frame,np.zeros(FRAME))
                    utterance.external_voice=voiced;began,_,_=utterance.feed(frame);begins+=began;confirmed|=credible
                self.assertEqual(begins,1);self.assertTrue(confirmed)
    def test_moderate_background_requires_clearer_energy_without_losing_near_speech(self):
        detector=NearSpeechDetector(self.SpeechVad(True));detector.noise=-48
        for i in range(40):
            frame=self.acoustic_frame(-38,i);voiced,credible=detector.feed(frame,frame,np.zeros(FRAME))
            self.assertFalse(voiced);self.assertFalse(credible)
        for i in range(40):
            frame=self.acoustic_frame(-32,i);detector.feed(frame,frame,np.zeros(FRAME))
        self.assertTrue(detector.credible)
    def test_reference_moves_with_pause_without_replaying_future_echo_early(self):
        timeline=RenderTimeline();pcm=np.ones(320);stamp=1_000_000_000
        timeline.add(100,0,stamp,pcm);timeline.pause_at(stamp+10_000_000)
        self.assertGreater(np.max(timeline.at(stamp,160)),0)
        self.assertEqual(np.max(timeline.at(stamp+10_000_000,160)),0)
        timeline.resume_at(stamp+510_000_000)
        self.assertEqual(np.max(timeline.at(stamp+10_000_000,160)),0)
        self.assertGreater(np.max(timeline.at(stamp+510_000_000,160)),0)


if __name__=='__main__':unittest.main()
