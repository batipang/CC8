import io
import json
import struct
import threading
import time
import unittest
import numpy as np

from protocol import Packet, receive, SPEAKER, MIC, MAX_PACKET
from dsp import EchoCanceller, WebRtcEchoCanceller, VoiceDetector, RenderTimeline, ReferenceCapture, FRAME, RATE, estimate_reference_shift, resembles_playback
from bridge import Bridge, ROOT, LIBRARY, wav_samples
from fast_stop import can_fast_stop, FastStopDetector


class Fragmented:
    def __init__(self,data):self.data=io.BytesIO(data)
    def recv(self,n):return self.data.read(min(n,3))


class Sink:
    def __init__(self):self.packets=[]
    def sendall(self,data):self.packets.append(receive(Fragmented(data)))


class ProtocolTests(unittest.TestCase):
    def test_fragmented_pcm_roundtrip(self):
        p=Packet(MIC,17,9,320,123456789,b'\x01\x02'*320)
        self.assertEqual(receive(Fragmented(p.encode())),p)

    def test_chinese_control_roundtrip(self):
        p=Packet.event('play_begin',4,text='你好，薄荷')
        self.assertEqual(json.loads(receive(Fragmented(p.encode())).data)['text'],'你好，薄荷')

    def test_reject_unbounded_length_before_reading_payload(self):
        with self.assertRaises(ValueError):receive(Fragmented(struct.pack('!I',MAX_PACKET+1)))

    def test_reject_odd_pcm(self):
        with self.assertRaises(ValueError):Packet(SPEAKER,data=b'x').encode()

    def test_disconnect_mid_packet(self):
        with self.assertRaises(EOFError):receive(Fragmented(Packet.event('ping').encode()[:-2]))


class AudioTests(unittest.TestCase):
    def test_render_capture_preserves_vad_preroll_and_end_frame(self):
        capture=ReferenceCapture();vad=VoiceDetector();last=None
        for index,pcm in enumerate([np.zeros(FRAME)]*40+[np.ones(FRAME)*.04]*50+[np.zeros(FRAME)]*80):
            began,audio,_=vad.feed(pcm)
            rendered=capture.feed(np.full(FRAME,index),began,audio is not None)
            if audio is not None:
                self.assertEqual(len(rendered),len(audio));self.assertEqual(rendered[-1],index)
                self.assertEqual(rendered[0],index-len(audio)//FRAME+1);last=rendered
        self.assertIsNotNone(last)

    def test_alignment_finds_late_microphone_with_filter_lead_in(self):
        rng=np.random.default_rng(4);reference=rng.normal(0,.04,RATE*3)
        mic=np.r_[np.zeros(480),reference[:-480]]
        shift,correlation=estimate_reference_shift(mic,reference)
        self.assertEqual(shift,160);self.assertGreater(correlation,.99)

    def test_alignment_handles_capture_clock_leading_render_clock(self):
        rng=np.random.default_rng(4);reference=rng.normal(0,.04,RATE*3)
        mic=np.r_[reference[200:],np.zeros(200)]
        shift,correlation=estimate_reference_shift(mic,reference)
        self.assertEqual(shift,-520);self.assertGreater(correlation,.99)

    def test_alignment_does_not_invent_delay_for_missing_reference(self):
        self.assertEqual(estimate_reference_shift(np.ones(RATE),np.zeros(RATE)),(0,0.0))

    def test_playback_fragment_is_rejected_but_user_question_is_not(self):
        spoken=['我在。你可以开始说话，也可以在我回答时打断我。']
        self.assertTrue(resembles_playback('你可以开始说话',spoken))
        self.assertFalse(resembles_playback('你好，介绍一下你自己',spoken))

    def test_stop_command_is_not_mistaken_for_quoted_playback(self):
        self.assertFalse(resembles_playback('停一下',['你可以说停一下来打断我']))

    def test_echo_with_chinese_percentage_and_asr_substitutions(self):
        self.assertTrue(resembles_playback('英特尔因业绩展望疲软大跌百分之二十九',['英特尔因业绩展望疲软大跌29%；']))
        self.assertTrue(resembles_playback('国产GPU沐曦第三季度实现单机扭轨',['国产GPU沐曦第三季度实现单季扭亏。']))
        self.assertTrue(resembles_playback('据媒体报到苹果智能将接入阿里千问',['据媒体报道，苹果智能将接入阿里千问。']))
        self.assertTrue(resembles_playback('报告称长三交易形成从底层算力核心算法到中间应用',
            ['报告称，长三角已形成从底层算力、核心算法到终端应用、高端装备的完整产业生态。']))

    def test_new_question_is_not_a_spoken_percentage_or_echo(self):
        refs=['英特尔因业绩展望疲软大跌29%；国产GPU沐曦第三季度实现单季扭亏。']
        for question in ['英特尔为什么会跌这么多','沐曦靠什么实现扭亏','别讲新闻了，给我讲个故事','停一下']:
            self.assertFalse(resembles_playback(question,refs),question)

    def test_related_new_question_is_not_a_fuzzy_echo(self):
        ref=['报告称，长三角已形成从底层算力、核心算法到终端应用、高端装备的完整产业生态。']
        self.assertFalse(resembles_playback('报告说的长三角是否已经形成完整产业生态',ref))
        self.assertFalse(resembles_playback('长三角是否已形成从底层算力核心算法到终端应用高端装备的完整产业生态',ref))

    def test_reference_alignment_uses_device_clock(self):
        timeline=RenderTimeline();timeline.add(4,0,1_000_000_000,np.ones(320))
        np.testing.assert_allclose(timeline.at(1_010_000_000,320),np.r_[np.ones(160)*.35,np.zeros(160)])

    def test_render_underrun_reanchors_at_actual_playback_after_gap(self):
        timeline=RenderTimeline();timeline.add(4,0,1_000_000_000,np.ones(320))
        timeline.add(4,320,1_420_000_000,np.ones(320)*2)
        np.testing.assert_allclose(timeline.at(1_020_000_000,320),np.zeros(320))
        np.testing.assert_allclose(timeline.at(1_420_000_000,320),np.ones(320)*.7)
        self.assertEqual(timeline.discontinuities,1)

    def test_vad_silence_does_not_start_turn(self):
        detector=VoiceDetector()
        for _ in range(1000):
            onset,audio,_=detector.feed(np.zeros(FRAME))
            self.assertFalse(onset);self.assertIsNone(audio)

    def test_vad_requires_sustained_onset_and_finishes_after_silence(self):
        detector=VoiceDetector();onsets=0;utterances=[]
        for frame in [np.zeros(FRAME)]*40+[np.ones(FRAME)*.04]*50+[np.zeros(FRAME)]*80:
            start,done,_=detector.feed(frame);onsets+=start
            if done is not None:utterances.append(done)
        self.assertEqual(onsets,1);self.assertEqual(len(utterances),1)
        self.assertGreater(len(utterances[0]),RATE//2)

    def replay(self,capture_name,calibration_seconds):
        base=ROOT/'device-access/private/audio-assessment-20261031'
        if not (base/capture_name).exists() or not LIBRARY.exists():self.skipTest('Local device evidence is absent')
        mic=wav_samples(base/capture_name);ref=wav_samples(ROOT/'cc8-diagnostics/app/src/main/assets/reference.wav')
        render=np.resize(ref,len(mic))*.35
        aec=EchoCanceller(LIBRARY);detector=VoiceDetector();onsets=[]
        try:
            for n in range(0,len(mic)-FRAME+1,FRAME):
                cleaned=aec.process(mic[n:n+FRAME],render[n:n+FRAME])
                if n<calibration_seconds*RATE:continue
                start,_,_=detector.feed(cleaned)
                if start:onsets.append(n/RATE)
        finally:aec.close()
        return onsets

    def test_saved_far_end_only_does_not_trigger_after_calibration(self):
        self.assertEqual(self.replay('farend-only.wav',6),[])

    def test_saved_double_talk_still_triggers(self):
        onsets=self.replay('duplex-user-confirmed.wav',3)
        self.assertGreaterEqual(len(onsets),1)
        self.assertGreaterEqual(onsets[0],3)

    def test_recorded_residual_echo_suppression_does_not_mute_double_talk(self):
        base=ROOT/'device-access/private/audio-assessment-20261031'
        if not (base/'farend-only.wav').exists() or not LIBRARY.exists():self.skipTest('Local device evidence is absent')
        ref=wav_samples(ROOT/'cc8-diagnostics/app/src/main/assets/reference.wav');levels={}
        for name in ['farend-only','duplex-user-confirmed']:
            mic=wav_samples(base/(name+'.wav'));render=np.resize(ref,len(mic))*.35
            shift=json.loads((base/'offline-aec'/(name+'-speex.json')).read_text())['reference_shift_samples']
            aligned=np.r_[np.zeros(shift),render[:-shift]]
            for residual in [False,True]:
                aec=EchoCanceller(LIBRARY,residual=residual)
                try:out=np.concatenate([aec.process(mic[n:n+FRAME],aligned[n:n+FRAME]) for n in range(0,len(mic)-FRAME+1,FRAME)])
                finally:aec.close()
                levels[(name,residual)]=10*np.log10(max(np.mean(out[RATE*3:]**2),1e-12))
        self.assertGreater(levels[('farend-only',False)]-levels[('farend-only',True)],3)
        self.assertLess(abs(levels[('duplex-user-confirmed',False)]-levels[('duplex-user-confirmed',True)]),3)

    def test_aec3_recorded_far_end_does_not_trigger_but_double_talk_does(self):
        base=ROOT/'device-access/private/audio-assessment-20261031'
        if not (base/'farend-only.wav').exists():self.skipTest('Local device evidence is absent')
        ref=wav_samples(ROOT/'cc8-diagnostics/app/src/main/assets/reference.wav')
        for name in ['farend-only','duplex-user-confirmed']:
            mic=wav_samples(base/(name+'.wav'));render=np.resize(ref,len(mic))*.35
            shift=json.loads((base/'offline-aec'/(name+'-speex.json')).read_text())['reference_shift_samples'];aligned=np.r_[np.zeros(shift),render[:-shift]]
            aec=WebRtcEchoCanceller();vad=VoiceDetector();onsets=[];parts=[]
            try:
                for n in range(0,len(mic)-FRAME+1,FRAME):
                    out=aec.process(mic[n:n+FRAME],aligned[n:n+FRAME]);parts.append(out)
                    if n>=RATE*3:
                        began,_,_=vad.feed(out)
                        if began:onsets.append(n/RATE)
            finally:aec.close()
            output=np.concatenate(parts);power=10*np.log10(max(np.mean(output[RATE*3:]**2),1e-12))
            if name=='farend-only':self.assertEqual(onsets,[]);self.assertLess(power,-60)
            else:self.assertGreaterEqual(len(onsets),1);self.assertGreater(power,-40)


class CancellationTests(unittest.TestCase):
    def bridge(self,speech):
        b=Bridge(speech,None);b.connection=Sink();b.active=True;b.save_status=lambda:None
        return b

    def test_stale_generation_never_queues_audio(self):
        b=self.bridge(None)
        self.assertFalse(b.play(np.ones(320,dtype=np.int16),'old',b.generation-1))
        self.assertEqual(b.connection.packets,[])

    def test_cancel_increments_generation_and_notifies_device(self):
        b=self.bridge(None);b.playing=True;old=b.generation;b.cancel('test')
        self.assertEqual(b.generation,old+1);self.assertFalse(b.playing)
        p=b.connection.packets[-1];self.assertEqual(p.generation,b.generation)
        self.assertEqual(json.loads(p.data)['event'],'cancel')

    def test_late_old_play_done_does_not_stop_new_playback(self):
        b=self.bridge(None);b.playing=True
        b.on_event({'event':'play_done','generation':b.generation-1})
        self.assertTrue(b.playing)

    def test_recognized_playback_fragment_does_not_cancel_or_reply(self):
        completed=threading.Event()
        class Speech:
            def recognize(self,audio):completed.set();return '你可以开始说话'
        b=self.bridge(Speech());b.playing=True
        b.queue.put((b.generation,np.ones(RATE),True,['我在。你可以开始说话。']))
        self.assertTrue(completed.wait(1));time.sleep(.05)
        self.assertTrue(b.playing);self.assertEqual(b.generation,100)
        self.assertEqual(b.connection.packets,[])

    def test_late_playback_tail_is_not_a_new_web_question(self):
        completed=threading.Event()
        class Speech:
            def recognize(self,audio):completed.set();return '这次联网查询没有成功，我暂时无法核实最新信息。'
        b=self.bridge(Speech());b.last_audio_end=time.monotonic()-2
        b.spoken.append((time.monotonic()-7,'这次联网查询没有成功，我暂时无法核实最新信息。请稍后让我再查一次。'))
        b.queue.put((b.generation,np.ones(RATE),False,[]))
        self.assertTrue(completed.wait(1));time.sleep(.05)
        self.assertFalse(any(json.loads(p.data).get('event')=='transcript' for p in b.connection.packets))

    def test_echo_capture_evidence_survives_long_asr_and_queue_delays(self):
        completed=threading.Event()
        class Speech:
            def recognize(self,audio):completed.set();return '国产GPU沐曦第三季度实现单机扭轨'
        b=self.bridge(Speech());b.last_audio_end=time.monotonic()-120
        refs=['国产GPU沐曦第三季度实现单季扭亏。']
        b.queue.put((b.generation,np.ones(RATE),False,refs,{'echo_risk':True,'captured_at':time.monotonic()-90}))
        self.assertTrue(completed.wait(1));time.sleep(.05)
        self.assertFalse(any(json.loads(p.data).get('event')=='transcript' for p in b.connection.packets))
        self.assertEqual(b.metrics.get('echo_transcripts_rejected'),1)

    def test_true_barge_in_with_captured_echo_risk_still_answers(self):
        answered=threading.Event()
        class Speech:
            def recognize(self,audio):return '别讲新闻了，给我讲个故事'
        class Brain:
            last_metrics={}
            def cancel(self):pass
            def clauses(self,text,history,current,on_web):yield '好的，给你讲个故事。'
        b=self.bridge(Speech());b.brain=Brain()
        b.speak=lambda text,generation:(answered.set() or True)
        b.queue.put((b.generation,np.ones(RATE),True,['今天科技新闻可关注几条。'],{'echo_risk':True,'captured_at':time.monotonic()-40}))
        self.assertTrue(answered.wait(1))
        transcripts=[json.loads(p.data).get('text') for p in b.connection.packets if json.loads(p.data).get('event')=='transcript']
        self.assertIn('别讲新闻了，给我讲个故事',transcripts)

    def test_echo_cannot_trigger_wake_word_before_filtering(self):
        done=threading.Event()
        class Speech:
            def recognize(self,audio):done.set();return '请说大黄蜂开始聊天'
        b=self.bridge(Speech());b.awake=False;b.wake=True
        b.queue.put((b.generation,np.ones(RATE),False,['请说“大黄蜂”开始聊天'],{'echo_risk':True,'captured_at':time.monotonic()-70}))
        self.assertTrue(done.wait(1));time.sleep(.05);self.assertFalse(b.awake)

    def test_self_check_counts_a_missed_echo_without_calling_brain(self):
        done=threading.Event()
        class Speech:
            def recognize(self,audio):done.set();return '没有匹配上的声音片段'
        class Brain:
            def clauses(self,*args):raise AssertionError('Diagnostic must not create a reply')
            def cancel(self):pass
        b=self.bridge(Speech());b.brain=Brain();b.echo_check_running=True;b.metrics['echo_self_check']={'unmatched_candidates':0}
        b.queue.put((b.generation,np.ones(RATE),True,['声音测试。'],{'echo_risk':True,'captured_at':time.monotonic()}))
        self.assertTrue(done.wait(1));time.sleep(.05)
        self.assertEqual(b.metrics['echo_self_check']['unmatched_candidates'],1)
        self.assertFalse(any(json.loads(p.data).get('event')=='transcript' for p in b.connection.packets))

    def test_render_asr_rejects_a_name_spelled_differently_from_caption(self):
        done=threading.Event()
        class Speech:
            calls=0
            def recognize(self,audio):
                self.calls+=1
                if self.calls==1:return '米尼麦克斯欧派鸡皮油'
                done.set();return '米尼麦克斯欧派鸡皮油'
        speech=Speech();b=self.bridge(speech)
        b.queue.put((b.generation,np.ones(RATE),True,['MiniMax、OpenAI、GPU。'],{'echo_risk':True,'captured_at':time.monotonic()},np.ones(RATE)*.1))
        self.assertTrue(done.wait(1));time.sleep(.05)
        self.assertEqual(b.metrics.get('render_asr_rejections'),1)
        self.assertFalse(any(json.loads(p.data).get('event')=='transcript' for p in b.connection.packets))

    def test_novel_barge_in_passes_both_caption_and_render_asr_comparison(self):
        answered=threading.Event()
        class Speech:
            calls=0
            def recognize(self,audio):
                self.calls+=1
                return '先别讲新闻了，我想听个故事' if self.calls==1 else '米尼麦克斯欧派鸡皮油'
        class Brain:
            last_metrics={}
            def cancel(self):pass
            def clauses(self,*args):yield '好的。'
        b=self.bridge(Speech());b.brain=Brain();b.speak=lambda *args:(answered.set() or True)
        b.queue.put((b.generation,np.ones(RATE),True,['MiniMax、OpenAI、GPU。'],{'echo_risk':True,'captured_at':time.monotonic()},np.ones(RATE)*.1))
        self.assertTrue(answered.wait(1));self.assertEqual(b.metrics.get('render_asr_checks'),1)
        self.assertNotIn('render_asr_rejections',b.metrics)

    def test_render_asr_result_after_cancel_is_discarded(self):
        started=threading.Event();release=threading.Event()
        class Speech:
            calls=0
            def recognize(self,audio):
                self.calls+=1
                if self.calls==1:return '这是一个新的问题'
                started.set();release.wait(2);return '扬声器的声音'
        b=self.bridge(Speech())
        b.queue.put((b.generation,np.ones(RATE),True,['声音示例。'],{'echo_risk':True,'captured_at':time.monotonic()},np.ones(RATE)*.1))
        self.assertTrue(started.wait(1));b.cancel('new_turn');release.set();time.sleep(.05)
        self.assertFalse(any(json.loads(p.data).get('event')=='transcript' for p in b.connection.packets))


    def test_recognizer_result_after_interrupt_is_discarded(self):
        started=threading.Event();release=threading.Event()
        class SlowSpeech:
            def recognize(self,audio):started.set();release.wait(2);return '旧的问题'
        b=self.bridge(SlowSpeech());b.queue.put((b.generation,np.ones(RATE)))
        self.assertTrue(started.wait(1));b.cancel('new_speech');release.set();time.sleep(.1)
        self.assertFalse(any(json.loads(p.data).get('event')=='transcript' for p in b.connection.packets))


class WakeRoutingTests(unittest.TestCase):
    def run_turn(self,text,awake=False):
        recognized=threading.Event();answered=threading.Event()
        class Speech:
            def recognize(self,audio):recognized.set();return text
        class Brain:
            last_metrics={}
            def __init__(self):self.requests=[]
            def cancel(self):pass
            def clauses(self,value,history,current,on_web):self.requests.append(value);yield '你好。'
        brain=Brain();b=Bridge(Speech(),brain);b.connection=Sink();b.active=True;b.wake=True;b.awake=awake;b.save_status=lambda:None
        b.speak=lambda *args:(answered.set() or True)
        b.queue.put((b.generation,np.ones(RATE),False,[],{'echo_risk':False,'captured_at':time.monotonic()}))
        self.assertTrue(recognized.wait(1));return b,brain,answered

    def test_new_wake_word_strips_prefix_and_routes_same_utterance(self):
        b,brain,done=self.run_turn('大黄蜂，介绍一下你自己')
        self.assertTrue(done.wait(1));self.assertTrue(b.awake);self.assertEqual(brain.requests,['介绍一下你自己'])

    def test_old_wake_word_no_longer_wakes(self):
        for word in ['小薄荷','贾维斯','JARVIS','bumblebees']:
            b,brain,done=self.run_turn(word+'，介绍一下你自己')
            self.assertFalse(done.wait(.1));self.assertFalse(b.awake);self.assertEqual(brain.requests,[])

    def test_english_and_common_asr_spelling_can_wake(self):
        for word in ['Bumblebee','BUMBLEBEE','bumble bee','邦布比','大黃蜂']:
            b,brain,done=self.run_turn(word+'，你好')
            self.assertTrue(done.wait(1));self.assertEqual(brain.requests,['你好'])

    def test_new_sleep_phrase_returns_to_waiting(self):
        b,brain,done=self.run_turn('大黄蜂，你先休息一会儿吧',awake=True)
        self.assertFalse(done.wait(.1));self.assertFalse(b.awake);self.assertEqual(brain.requests,[])


class CaptureEvidenceTests(unittest.TestCase):
    def test_device_reference_marks_risk_and_captures_later_clauses(self):
        class QuietWorker(Bridge):
            def work(self):pass
        class AEC:
            def process(self,mic,reference):return mic
        class VAD:
            talking=False
            def feed(self,pcm):
                if not self.talking:self.talking=True;return True,None,-30
                self.talking=False;return False,np.ones(RATE),-30
        b=QuietWorker(None,None);b.connection=Sink();b.active=True;b.aec=AEC();b.vad=VAD();b.save_status=lambda:None
        b.last_status=time.monotonic();b.last_audio_end=time.monotonic()-500
        b.spoken.append((time.monotonic()-100,'较早开始的一段长播报。'))
        stamp=1_000_000_000;b.timeline.add(b.generation,0,stamp,np.ones(320)*.1)
        b.process_microphone(Packet(MIC,1,0,0,stamp,(np.ones(FRAME)*1200).astype('<i2').tobytes()))
        b.spoken.append((time.monotonic(),'同一段录音开始后播报的下一句。'))
        b.process_microphone(Packet(MIC,1,1,FRAME,stamp+10_000_000,(np.ones(FRAME)*1200).astype('<i2').tobytes()))
        job=b.queue.get_nowait();self.assertTrue(job[4]['echo_risk']);self.assertFalse(job[2])
        self.assertIn('较早开始的一段长播报。',job[3]);self.assertIn('同一段录音开始后播报的下一句。',job[3])
        self.assertEqual(b.generation,100)  # Unverified echo does not cancel a turn.

    def test_quiet_reference_does_not_mark_idle_user_as_playback(self):
        class QuietWorker(Bridge):
            def work(self):pass
        class AEC:
            def process(self,mic,reference):return mic
        class VAD:
            talking=False
            def feed(self,pcm):
                if not self.talking:self.talking=True;return True,None,-30
                self.talking=False;return False,np.ones(RATE),-30
        b=QuietWorker(None,None);b.connection=Sink();b.active=True;b.aec=AEC();b.vad=VAD();b.save_status=lambda:None;b.last_status=time.monotonic()
        b.process_microphone(Packet(MIC,1,0,0,1_000_000_000,np.ones(320,dtype='<i2').tobytes()))
        job=b.queue.get_nowait();self.assertFalse(job[4]['echo_risk']);self.assertEqual(b.generation,101)



class FastStopTests(unittest.TestCase):
    def bridge(self,spoken='我给你讲一个故事。',calibrating=False):
        class Detector:
            calls=0
            def feed(self,audio):
                self.calls+=1
                return '停一下' if self.calls==1 else ''
        class AEC:
            def process(self,mic,ref):return mic
        b=Bridge(None,None,fast_stop=Detector());b.connection=Sink();b.active=True;b.playing=True
        b.last_spoken=spoken;b.aec=AEC();b.calibrating=calibrating;b.save_status=lambda:None
        return b

    def test_keywords_are_strictly_allowlisted(self):
        self.assertFalse(can_fast_stop('播放',''))
        self.assertTrue(can_fast_stop('停一下','我给你讲一个故事'))

    def test_stop_branch_preserves_weak_voice_while_asr_uses_suppression(self):
        class AEC:
            def __init__(self,value):self.value=value
            def process(self,mic,ref):return np.full(FRAME,self.value)
        class Detector:
            calls=[]
            def feed(self,audio):self.calls.append(audio.copy());return '停一下' if len(self.calls)==1 else ''
        class VAD:
            talking=False
            def __init__(self):self.seen=[]
            def reset(self):pass
            def feed(self,audio):self.seen.append(audio.copy());return False,None,-90
        b=self.bridge();b.aec=AEC(0);b.stop_aec=AEC(.01);b.fast_stop=Detector();b.vad=VAD()
        b.process_microphone(Packet(MIC,0,0,0,10_000_000_000,b'\x00\x00'*320))
        np.testing.assert_allclose(b.fast_stop.calls[0],np.full(FRAME,.01))
        self.assertEqual(b.metrics['fast_stop_count'],1);self.assertFalse(b.playing)

    def test_keyword_gain_is_bounded_and_clipped(self):
        for gain in [0,5]:
            with self.assertRaises(ValueError):FastStopDetector(gain=gain)
        class Stream:
            def accept_waveform(self,rate,pcm):self.pcm=pcm
        class Detector:
            def is_ready(self,stream):return False
        detector=FastStopDetector.__new__(FastStopDetector);detector.gain=2;detector.stream=Stream();detector.detector=Detector();detector.samples=0;detector.compute_seconds=0
        detector.feed(np.array([.01,-.01,1,-1]))
        np.testing.assert_allclose(detector.stream.pcm,[.02,-.02,.98,-.98])

    def test_assistant_quoting_command_cannot_trigger_fast_route(self):
        b=self.bridge('你可以说停一下来中断。')
        b.process_microphone(Packet(MIC,0,0,0,10_000_000_000,b'\x00\x00'*320))
        self.assertTrue(b.playing);self.assertEqual(b.generation,100)
        self.assertEqual(b.connection.packets,[])

    def test_streaming_keyword_stops_without_utterance_endpoint(self):
        b=self.bridge()
        b.process_microphone(Packet(MIC,0,0,0,10_000_000_000,b'\x00\x00'*320))
        self.assertFalse(b.playing);self.assertEqual(b.generation,101);self.assertTrue(b.active)
        self.assertEqual(b.metrics['fast_stop_count'],1)
        events=[json.loads(p.data) for p in b.connection.packets]
        self.assertEqual(events[0]['event'],'cancel')
        self.assertTrue(any(e.get('event')=='transcript' and e.get('text')=='停一下' for e in events))

    def test_previous_sentence_acoustic_tail_cannot_trigger_fast_route(self):
        b=self.bridge();b.previous_spoken='你可以说停一下。';b.previous_spoken_until=time.monotonic()+1
        b.process_microphone(Packet(MIC,0,0,0,10_000_000_000,b'\x00\x00'*320))
        self.assertTrue(b.playing);self.assertEqual(b.connection.packets,[])

    def test_calibration_never_activates_fast_stop(self):
        b=self.bridge(calibrating=True)
        b.process_microphone(Packet(MIC,0,0,0,10_000_000_000,b'\x00\x00'*320))
        self.assertEqual(b.fast_stop.calls,0);self.assertTrue(b.playing)

    def test_late_whisper_result_after_fast_stop_cannot_restart_answer(self):
        started=threading.Event();release=threading.Event()
        class SlowSpeech:
            def recognize(self,audio):started.set();release.wait(2);return '迟到的旧问题'
        b=Bridge(SlowSpeech(),None);b.connection=Sink();b.active=True;b.playing=True;b.save_status=lambda:None
        b.queue.put((b.generation,np.ones(RATE)))
        self.assertTrue(started.wait(1));b.apply_fast_stop('停一下',10_000_000_000);release.set();time.sleep(.1)
        transcripts=[json.loads(p.data).get('text') for p in b.connection.packets if json.loads(p.data).get('event')=='transcript']
        self.assertEqual(transcripts,['停一下'])


if __name__=='__main__':unittest.main()
