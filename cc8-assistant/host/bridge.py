"""CC8 USB voice: local AEC/ASR, direct MiniMax dialogue and speech APIs.

Microphone samples stay in RAM. Only explicitly enabled smoke/replay tools
read audio files. The companion APK never starts capture from a host command.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from collections import deque
import json
import itertools
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
import numpy as np

from protocol import Packet, MIC, RENDER, EVENT, SPEAKER
from transport import Pairing, TransportHub
from dsp import RATE, FRAME, WebRtcEchoCanceller, RenderTimeline, ReferenceCapture, VoiceDetector, NearSpeechDetector, level, estimate_reference_shift, resembles_playback
from fast_stop import FastStopDetector, can_fast_stop
from brain_router import BrainRouter
from announce import Announcement, AnnouncementServer, validate_text
from caption_timing import CaptionTiming
from recognition import MandarinRecognizer
from vision import CameraInbox, VisionError, visual_request
from minimax_client import MiniMaxBrain, MiniMaxVoice, MiniMaxError

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT/'device-access/private/voice-runtime'
LIBRARY = ROOT/'.toolchains/cc8/speexdsp-1.2.1/libspeexdsp/.libs/libspeexdsp.dylib'
WAKE_WORD = '大黄蜂'
WAKE_PATTERN = re.compile(r'大黄蜂|大黃蜂|大黄峰|邦布比|班布比|(?<![A-Za-z])bumble[\s-]*bee(?![A-Za-z])',re.I)


def model_path():
    cache = Path.home()/'.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots'
    matches = sorted(p for p in cache.glob('*') if (p/'weights.safetensors').exists())
    if not matches:
        raise RuntimeError('Local MLX speech model is missing')
    return str(matches[-1])


def wav_samples(path):
    with wave.open(str(path), 'rb') as f:
        if f.getframerate()!=RATE or f.getnchannels()!=1 or f.getsampwidth()!=2:
            raise ValueError('Expected 16 kHz mono PCM16')
        return np.frombuffer(f.readframes(f.getnframes()),dtype='<i2').astype(float)/32768


class LocalSpeech:
    def __init__(self):
        self.recognizer=MandarinRecognizer()
        self.cache={}

    def recognize(self, samples):return self.recognizer.recognize(samples)

    def synthesize(self,text):
        if text in self.cache:return self.cache[text]
        with tempfile.TemporaryDirectory(prefix='cc8-tts-') as folder:
            source=Path(folder)/'speech.aiff'
            subprocess.run(['/usr/bin/say','-v','Tingting','-r','185','-o',str(source)],
                           input=text.encode(),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True,timeout=30)
            raw=subprocess.check_output(['/opt/homebrew/bin/ffmpeg','-v','error','-i',str(source),
                '-ar',str(RATE),'-ac','1','-f','s16le','pipe:1'],stderr=subprocess.PIPE,timeout=20)
        pcm=np.frombuffer(raw,dtype='<i2').copy()
        if len(pcm)==0:raise RuntimeError('Local TTS returned no samples')
        if len(self.cache)>=12:self.cache.pop(next(iter(self.cache)))
        self.cache[text]=pcm
        return pcm


class DirectSpeech(LocalSpeech):
    def __init__(self):
        super().__init__()
        self.voice=MiniMaxVoice()
    def cancel(self):self.voice.cancel();self.recognizer.cancel()
    def stream(self,text,is_current=lambda:True,on_subtitle=None):return self.voice.pcm(text,is_current,on_subtitle)
    def synthesize(self,text):
        chunks=list(self.stream(text))
        if not chunks:raise MiniMaxError('MiniMax returned no audio')
        return np.concatenate(chunks)


def mood(text):
    if any(w in text for w in ['难过','担心','不开心','烦恼','辛苦','害怕','抱歉']):return 'empathetic'
    if any(w in text for w in ['放松','呼吸','慢慢','平静','休息']):return 'calm'
    if any(w in text for w in ['开心','高兴','太好了','成功','喜欢','谢谢']):return 'happy'
    return 'friendly'


@dataclass
class Reply:
    generation:int
    text:str


@dataclass
class VisualReply(Reply):
    origin:str="voice"
    nonce:str=""


class Bridge:
    def __init__(self,speech,brain,library=LIBRARY,fast_stop=None,echo_check=False):
        self.speech=speech;self.brain=brain;self.library=library
        self.connection=None;self.transport_hub=None;self.transport_kind="none";self.rebind_seen=False;self.capture_lookahead_ns=80_000_000;self.write_lock=threading.Lock();self.guard=threading.RLock()
        self.camera_inbox=CameraInbox();self.vision_generation=None;self.vision_nonce=""
        self.active=False;self.epoch=0;self.generation=100;self.playing=False;self.calibrating=False;self.announcement_generation=None
        self.wake=False;self.awake=True;self.aec=None;self.stop_aec=None;self.timeline=RenderTimeline();self.vad=VoiceDetector()
        self.history=deque(maxlen=8);self.queue=queue.Queue(maxsize=2);self.completed=set();self.done=threading.Condition()
        self.metrics={'mic_frames':0,'render_frames':0,'sequence_gaps':0,'interruptions':0,'utterances':0,'raw_audio_saved':False}
        self.last_sequence=None;self.last_status=0;self.last_spoken='';self.last_audio_end=0;self.live_turn=0
        self.spoken=deque(maxlen=32);self.pending_mic=deque();self.calibration_frames=[];self.reference_shift_ns=0
        self.dsp_lock=threading.RLock();self.turn_reference=[];self.turn_during_playback=False
        self.turn_echo_risk=False;self.turn_capture_started=0;self.last_capture_render_ns=None
        self.reference_capture=ReferenceCapture();self.near_speech=NearSpeechDetector()
        self.pause_state=None;self.pause_serial=0;self.pause_token=0;self.device_can_pause=False;self.turn_pause_token=None;self.pause_cooldown=0
        self.response_queue=queue.Queue(maxsize=2);self.status_lock=threading.Lock()
        self.playback_serial=0;self.last_interaction=time.monotonic();self.screen_sleeping=False;self.response_busy=False
        self.device_requests={};self.device_request_serial=0
        if isinstance(self.brain,BrainRouter):self.brain.dot.device_control=self.control_device
        self.fast_stop=fast_stop;self.last_fast_stop_ns=0;self.ignore_vad_until_ns=0
        self.previous_spoken='';self.previous_spoken_until=0
        self.echo_check_pending=echo_check;self.echo_check_running=False;self.echo_check_failed=False;self.recognition_busy=False
        self.worker=threading.Thread(target=self.work,daemon=True);self.worker.start()
        self.responder=threading.Thread(target=self.respond_work,daemon=True);self.responder.start()

    def log(self,event,**fields):
        print(json.dumps({'event':event,'monotonic_seconds':round(time.monotonic(),3),**fields},ensure_ascii=False),flush=True)

    def send(self,p):
        with self.write_lock:
            conn=self.connection
            if conn is None:raise EOFError('No device connection')
            if p.kind==SPEAKER and p.generation!=self.generation:return
            conn.sendall(p.encode())

    def event(self,event,generation=None,**fields):
        if generation is not None and generation<self.generation:return
        self.send(Packet.event(event,self.generation if generation is None else generation,**fields))

    def change_transport(self,channel):
        old=self.connection
        if old is channel:return
        was_active=self.active
        try:self.cancel('transport_switch')
        except (OSError,EOFError):pass
        self.connection=channel;self.rebind_seen=False;self.transport_kind=channel.kind if channel else 'none'
        self.capture_lookahead_ns=120_000_000 if self.transport_kind=='wifi' else 80_000_000
        self.timeline=RenderTimeline();self.pending_mic.clear();self.vad.reset();self.reference_capture.reset();self.near_speech.reset();self.last_sequence=None
        self.pause_state=None;self.turn_pause_token=None
        if channel is None:
            self.stop()
        elif was_active:
            # AudioRecord was already enabled locally. The new channel must
            # explicitly confirm the same capture epoch; it cannot start a mic.
            self.metrics['capture_preserved_switches']=self.metrics.get('capture_preserved_switches',0)+1
        self.metrics['transport_changes']=self.metrics.get('transport_changes',0)+1

    def status(self,phase,message):
        if self.connection is not None:self.event('status',phase=phase,message=message)

    def valid(self,generation):return (self.active or generation==self.announcement_generation or generation==self.vision_generation) and self.connection is not None and generation==self.generation

    def touch_activity(self):
        self.last_interaction=time.monotonic()
        if self.screen_sleeping:
            self.screen_sleeping=False;self.event('screen_wake')

    def idle_tick(self):
        if self.active and not self.calibrating and not self.playing and not self.response_busy and not self.recognition_busy and not self.screen_sleeping and time.monotonic()-self.last_interaction>=900:
            self.awake=False;self.screen_sleeping=True;self.event('screen_sleep');self.log('screen_idle_sleep')

    def control_device(self,action,percent=None):
        if action not in ('status','volume','stop','wake_screen'):raise ValueError('Unsupported device action')
        if self.connection is None:raise RuntimeError('CC8 disconnected')
        if action=='stop':self.cancel('dot_device_stop')
        with self.guard:
            self.device_request_serial+=1;ident=str(self.device_request_serial);waiter=queue.Queue(maxsize=1);self.device_requests[ident]=waiter
        try:
            fields={'request_id':ident,'action':action}
            if percent is not None:fields['percent']=percent
            self.event('device_control',**fields)
            try:
                result=waiter.get(timeout=5)
                if result.get('ok') and action=='wake_screen':self.touch_activity()
                return result
            except queue.Empty:raise RuntimeError('CC8 control was not acknowledged') from None
        finally:
            with self.guard:self.device_requests.pop(ident,None)

    def pause_for_voice(self):
        with self.guard:
            if not self.device_can_pause or not self.active or not self.playing or self.pause_state or time.monotonic()<self.pause_cooldown:return None
            self.pause_serial+=1;token=self.pause_serial;self.pause_token=token
            self.pause_state={'token':token,'generation':self.generation,'started':time.monotonic()}
        self.event('playback_pause',token=token)
        self.metrics['barge_in_pauses']=self.metrics.get('barge_in_pauses',0)+1
        self.log('barge_in_tentative_pause',token=token,input_dbfs=self.metrics.get('input_dbfs'),residual_dbfs=self.metrics.get('output_dbfs'),raw_coherence=round(self.near_speech.correlation,3),residual_coherence=round(self.near_speech.residual_correlation,3))
        return token

    def resume_after_rejection(self,token,generation):
        with self.guard:
            state=self.pause_state
            if token is None or not state or state['token']!=token or state['generation']!=generation or not self.valid(generation):return
            if self.vad.talking and self.turn_pause_token==token:return
            self.pause_state=None;self.pause_cooldown=time.monotonic()+.6
        self.event('playback_resume',generation=generation,token=token)
        self.metrics['barge_in_resumed']=self.metrics.get('barge_in_resumed',0)+1
        self.log('barge_in_rejected_resumed',token=token)
        with self.done:self.done.notify_all()

    def wait_playback(self,generation):
        if not self.pause_state:return 0.
        start=time.monotonic()
        while self.valid(generation):
            state=self.pause_state
            if not state:break
            if time.monotonic()-state['started']>20:
                self.cancel('barge_in_timeout');self.status('listening','插话识别超时，已停止旧播报');break
            with self.done:self.done.wait(.02)
        return time.monotonic()-start

    def announce(self,text):
        text=validate_text(text)
        with self.guard:
            if self.connection is None or self.calibrating or self.echo_check_running or self.announcement_generation is not None:
                raise RuntimeError('Announcement unavailable')
            generation=self.cancel('announcement');self.announcement_generation=generation
            job=Announcement(generation,text)
            try:self.response_queue.put_nowait(job)
            except queue.Full:
                self.announcement_generation=None;raise RuntimeError('Announcement queue busy') from None
            return job

    def read_announcement(self,job):
        generation=job.generation
        try:
            if not self.valid(generation):return
            self.event('announcement_begin',generation=generation)
            self.status('thinking','正在准备播报 · 麦克风状态保持不变')
            # Read user-provided text literally, without an LLM rewrite or lookup.
            pieces=[job.text[i:i+120] for i in range(0,len(job.text),120)]
            for text in pieces:
                if not self.valid(generation) or not self.speak(text,generation):return
            if self.valid(generation):job.result='completed'
        except Exception as error:
            job.result='failed'
            if self.valid(generation):self.failure('这次播报没有完成',error)
        finally:
            if self.connection is not None:
                try:self.event('announcement_done',generation=generation,result=job.result)
                except (OSError,EOFError):pass
            if self.announcement_generation==generation:self.announcement_generation=None
            self.metrics['last_announcement']={'status':job.result,'characters':len(job.text)}
            self.save_status();job.done.set()

    def cancel(self,reason='speech'):
        with self.guard:
            self.generation+=1;generation=self.generation;was_playing=self.playing;self.playing=False;self.announcement_generation=None;self.vision_generation=None;self.vision_nonce="";self.camera_inbox.cancel()
            self.pause_state=None;self.pause_token=0;self.timeline.cancel_pause()
            if was_playing:self.last_audio_end=time.monotonic()
        if self.brain:self.brain.cancel()
        cancel_speech=getattr(self.speech,'cancel',None)
        if cancel_speech:cancel_speech()
        if self.connection is not None:self.event('cancel',generation=generation)
        with self.done:self.done.notify_all()
        if was_playing:
            self.metrics['interruptions']+=1;self.log('playback_cancelled',reason=reason,generation=generation)
        return generation

    def stop(self):
        self.active=False;self.calibrating=False;self.screen_sleeping=False;self.cancel('session_stop');self.vad.reset()
        self.reference_capture.reset()
        with self.dsp_lock:
            if self.aec:self.aec.close();self.aec=None
            if self.stop_aec:self.stop_aec.close();self.stop_aec=None
        self.pending_mic.clear();self.calibration_frames.clear();self.save_status()

    def on_event(self,j):
        if not isinstance(j,dict):raise ValueError('Invalid device event')
        event=j.get('event')
        if event in ('camera_image_begin','camera_image_chunk','camera_image_end','camera_error'):
            self.camera_inbox.feed(j)
        elif event=='vision_request':
            nonce=j.get('nonce')
            if isinstance(nonce,str) and re.fullmatch('[a-f0-9]{32}',nonce):
                self.begin_visual('请描述当前摄像头画面中的主要事物，说明你能确定的内容；看不清时请说明。','button',nonce)
        elif event=='vision_cancel':
            if self.vision_generation is not None and (j.get('vision_generation')==self.vision_generation or (j.get('nonce') and j.get('nonce')==self.vision_nonce)):self.cancel('vision_cancel');self.status('listening' if self.active else 'idle','已停止看图')
        elif event=='device_ready':
            self.device_can_pause=bool(j.get('supports_playback_pause'))
            if self.active and not j.get('capture_active',False):self.stop()
            if 'wake_mode' in j:self.wake=bool(j['wake_mode'])
            self.event('hello',message='我在听' if self.active else '回声自检已准备 · 点击开始语音后保持安静' if self.echo_check_pending else '大黄蜂已就绪 · 点击开始语音',transport=self.transport_kind,asr_engine=getattr(getattr(self.speech,'recognizer',None),'engine','sensevoice'));self.log('device_handshake_verified',transport=self.transport_kind)
            self.event('brain_provider',provider=getattr(self.brain,'selected','minimax'));self.save_status()
        elif event=='session_rebind' and j.get('capture_active'):
            if self.rebind_seen:return
            self.rebind_seen=True
            if self.active and int(j.get('epoch',-1))==self.epoch:
                if self.calibrating:
                    self.calibration_frames=[]
                    threading.Thread(target=self.calibrate,args=(self.generation,),daemon=True).start()
                else:self.status('listening','连接已切换，我在听')
            else:self.on_event({**j,'event':'session_start'})
        elif event=='device_control_result':
            with self.guard:waiter=self.device_requests.get(str(j.get('request_id')))
            if waiter:
                try:waiter.put_nowait({k:v for k,v in j.items() if k in ('ok','volume_percent','microphone_active','foreground','error','transport','wifi_ready')})
                except queue.Full:pass
        elif event=='screen_touch':self.touch_activity()
        elif event=='settings':
            if 'wake_mode' in j:self.wake=bool(j['wake_mode']);self.awake=not self.wake
            if 'brain_provider' in j and hasattr(self.brain,'select'):
                provider=j['brain_provider']
                if provider in ('minimax','dot') and not self.calibrating:
                    self.cancel('brain_change');self.brain.select(provider)
                    self.event('brain_provider',provider=provider)
                    self.status('listening' if self.active else 'idle','已选择 Dot / Codex · 思考与执行' if provider=='dot' else '已选择 MiniMax · 日常对话与联网')
            self.save_status()
        elif event=='session_start':
            self.rebind_seen=True
            # A route rebind can arrive before the capture worker's first start.
            # Both describe the same native recording epoch; never restart it.
            if self.active and int(j['epoch'])==self.epoch:return
            if self.vision_generation is not None:self.cancel('microphone_start')
            if self.active or self.announcement_generation is not None:self.stop()
            self.epoch=int(j['epoch']);self.active=True;self.generation+=1;self.timeline=RenderTimeline();self.vad=VoiceDetector();self.last_sequence=None
            self.pending_mic.clear();self.calibration_frames=[];self.reference_shift_ns=0;self.spoken.clear()
            self.turn_reference=[];self.turn_echo_risk=False;self.last_capture_render_ns=None
            self.reference_capture.reset();self.near_speech.reset();self.pause_state=None;self.pause_token=0;self.turn_pause_token=None
            self.last_spoken='';self.previous_spoken='';self.previous_spoken_until=0;self.last_audio_end=0
            self.last_fast_stop_ns=0;self.ignore_vad_until_ns=0
            if self.fast_stop:self.fast_stop.reset()
            self.aec=WebRtcEchoCanceller()
            self.stop_aec=WebRtcEchoCanceller(noise_suppression=False) if self.fast_stop else None
            self.calibrating=True;self.awake=not self.wake;self.screen_sleeping=False;self.last_interaction=time.monotonic()
            for k in ['mic_frames','render_frames','sequence_gaps','interruptions','utterances','fast_stop_count','echo_transcripts_rejected']:self.metrics[k]=0
            generation=self.generation
            threading.Thread(target=self.calibrate,args=(generation,),daemon=True).start();self.log('foreground_capture_started')
        elif event=='session_stop':
            # A stopped worker can finish after a new foreground capture starts.
            if self.active and int(j.get('epoch',self.epoch))==self.epoch:
                self.stop();self.log('foreground_capture_stopped')
        elif event=='interrupt':
            if self.active or self.announcement_generation is not None or self.vision_generation is not None:self.cancel('button');self.status('listening' if self.active else 'idle','播报已停止')
        elif event=='play_done':
            with self.done:
                if int(j['generation'])==self.generation:self.completed={self.generation}
                self.done.notify_all()
            if int(j['generation'])==self.generation:self.playing=False;self.last_audio_end=time.monotonic()
        elif event=='playback_paused' and int(j.get('token',-1))==self.pause_token and int(j.get('generation',-1))==self.generation:
            self.timeline.pause_at(int(j['nanos']))
        elif event=='playback_resumed' and int(j.get('token',-1))==self.pause_token and int(j.get('generation',-1))==self.generation:
            self.timeline.resume_at(int(j['nanos']))
        elif event=='playback_error' and int(j.get('generation',self.generation))==self.generation:
            self.log('device_playback_error',error_type=str(j.get('error_type','Unknown'))[:50])
            self.cancel('playback_error')
        elif event=='capture_gap':self.log('capture_read_gap',milliseconds=j.get('milliseconds'))

    def on_packet(self,p):
        if p.kind==EVENT:
            try:self.on_event(json.loads(p.data))
            except (KeyError,TypeError,OverflowError):
                raise ValueError('Invalid device event fields') from None
            return
        if p.kind==RENDER:
            if p.generation!=self.generation:return
            pcm=np.frombuffer(p.data,dtype='<i2').astype(float)/32768
            self.timeline.add(p.generation,p.frame,p.nanos,pcm);self.metrics['render_frames']+=len(pcm)
            self.metrics['render_clock_discontinuities']=self.timeline.discontinuities;return
        if p.kind!=MIC or not self.active or p.generation!=self.epoch:return
        if self.pending_mic and p.nanos<=self.pending_mic[-1].nanos:
            raise ValueError('Non-monotonic microphone timestamp')
        if len(self.pending_mic)>=64:raise ValueError('Microphone lookahead exceeded')
        self.pending_mic.append(p)
        # A short fixed lookahead lets playback reference arrive before AEC,
        # even when estimated capture timestamps lead the render clock.
        while self.pending_mic and p.nanos-self.pending_mic[0].nanos>=self.capture_lookahead_ns:
            self.process_microphone(self.pending_mic.popleft())

    def process_microphone(self,p):
        if self.last_sequence is not None and p.sequence!=self.last_sequence+1:
            self.metrics['sequence_gaps']+=1;self.log('capture_packet_gap')
        self.last_sequence=p.sequence
        mic=np.frombuffer(p.data,dtype='<i2').astype(float)/32768
        if len(mic)%FRAME:
            self.log('unaligned_capture_frame',frames=len(mic));return
        self.metrics['mic_frames']+=len(mic)
        for offset in range(0,len(mic),FRAME):
            stamp=p.nanos+round(offset*1e9/RATE);reference=self.timeline.at(stamp-self.reference_shift_ns,FRAME)
            with self.dsp_lock:
                if not self.aec:return
                cleaned=self.aec.process(mic[offset:offset+FRAME],reference)
                # Stop keywords need a weak near voice to survive double-talk.
                # Their narrow, text-guarded detector uses AEC3 without the
                # additional noise suppressor used for conversational ASR.
                stop_cleaned=self.stop_aec.process(mic[offset:offset+FRAME],reference) if self.stop_aec else cleaned
            self.metrics['input_dbfs']=round(level(mic[offset:offset+FRAME]),1);self.metrics['output_dbfs']=round(level(cleaned),1)
            if self.calibrating:
                self.calibration_frames.append((stamp,mic[offset:offset+FRAME].copy()))
                continue
            # Capture-clock evidence survives ASR/network queue delays. A
            # wall-clock check made after recognition can miss the same echo.
            if level(reference)>-65:self.last_capture_render_ns=stamp
            echo_risk=self.playing or (self.last_capture_render_ns is not None and 0<=stamp-self.last_capture_render_ns<3_000_000_000)
            if self.fast_stop:
                self.metrics['stop_input_dbfs']=round(level(stop_cleaned),1)
                keyword=self.fast_stop.feed(stop_cleaned)
                if keyword and stamp-self.last_fast_stop_ns>1_200_000_000:
                    recent=self.playing or time.monotonic()-self.last_audio_end<1
                    render_text=self.last_spoken if recent else ''
                    if time.monotonic()<self.previous_spoken_until:render_text+=self.previous_spoken
                    if can_fast_stop(keyword,render_text):
                        self.apply_fast_stop(keyword,stamp)
                    else:self.log('fast_stop_reference_suppressed')
            if stamp<self.ignore_vad_until_ns:continue
            voiced,credible=self.near_speech.feed(cleaned,mic[offset:offset+FRAME],reference)
            self.vad.external_voice=voiced
            began,utterance,_=self.vad.feed(cleaned)
            render_audio=self.reference_capture.feed(reference,began,utterance is not None)
            if began:
                # Wake mode waits for textual keyword recognition until the
                # conversation is awake. This is not offline keyword spotting.
                now=time.monotonic()
                self.turn_during_playback=self.playing
                self.turn_echo_risk=echo_risk;self.turn_capture_started=now
                self.turn_pause_token=self.pause_state['token'] if self.pause_state else None
                # Bounded by count, not clause-start age: a 30-second-old
                # clause can still be audible or queued for recognition.
                self.turn_reference=[text for _,text in tuple(self.spoken)]
                # Only the acoustic speech gate can pause. Decode and reject
                # reference fragments before permanently cancelling a turn.
                self.live_turn=self.generation if self.turn_echo_risk or not self.awake else self.cancel('voice')
                if not self.turn_echo_risk:self.status('listening','我在听，你继续说')
            if self.vad.talking and credible and self.playing and self.awake and self.pause_state is None:
                self.turn_pause_token=self.pause_for_voice()
            if self.vad.talking or utterance is not None:self.turn_echo_risk|=echo_risk
            if utterance is not None:
                self.metrics['utterances']+=1
                # Include clauses that started after this utterance began.
                references=list(dict.fromkeys([*self.turn_reference,*[text for _,text in tuple(self.spoken)]]))[-64:]
                evidence={'echo_risk':self.turn_echo_risk,'captured_at':self.turn_capture_started,'pause_token':self.turn_pause_token}
                job=(self.live_turn,utterance,self.turn_during_playback,references,evidence,render_audio)
                try:self.queue.put_nowait(job)
                except queue.Full:
                    try:self.queue.get_nowait()
                    except queue.Empty:pass
                    self.queue.put_nowait(job)
        if time.monotonic()-self.last_status>1:
            self.last_status=time.monotonic();self.save_status()

    def apply_fast_stop(self,keyword,stamp):
        start=time.perf_counter()
        self.last_fast_stop_ns=stamp;self.ignore_vad_until_ns=stamp+160_000_000
        self.cancel('streaming_stop_keyword');self.vad.reset();self.live_turn=self.generation
        self.reference_capture.reset()
        # A stop command invalidates all queued, not-yet-recognized turns.
        while True:
            try:self.queue.get_nowait()
            except queue.Empty:break
        self.event('transcript',text=keyword)
        self.status('listening','已停止播报，我在听')
        self.metrics['fast_stop_count']=self.metrics.get('fast_stop_count',0)+1
        self.metrics['fast_stop_control_dispatch_ms']=round((time.perf_counter()-start)*1000,2)
        self.log('streaming_stop_keyword',command=keyword,
                 control_dispatch_ms=self.metrics['fast_stop_control_dispatch_ms'])
        self.save_status()

    def save_status(self):
        PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(PRIVATE,0o700)
        data={'connected':self.connection is not None,'active':self.active,'calibrating':self.calibrating,
              'playing':self.playing,'generation':self.generation,'wake_mode':self.wake,'wake_word':WAKE_WORD,
              'bridge_version':'0.9.2','noise_profile':'moderate','speech_energy_floor_dbfs':NearSpeechDetector.MIN_DBFS,'speech_noise_margin_db':NearSpeechDetector.NOISE_MARGIN_DB,'speech_onset_ms':VoiceDetector.ONSET_FRAMES*10,'barge_confirm_min_ms':NearSpeechDetector.CONFIRM_MIN_FRAMES*10,'vision_active':self.vision_generation is not None,'vision_enabled':isinstance(self.brain,BrainRouter),'transport':self.transport_kind,**(self.transport_hub.diagnostics() if self.transport_hub else {}),'screen_sleeping':self.screen_sleeping,'idle_sleep_seconds':900,'asr_engine':getattr(getattr(self.speech,'recognizer',None),'engine',None),'barge_in_enabled':self.device_can_pause,'playback_paused':self.pause_state is not None,'announcement_active':self.announcement_generation is not None,'brain_provider':getattr(self.brain,'selected','minimax') if self.brain else 'echo test','dot_enabled':isinstance(self.brain,BrainRouter),'web_search_enabled':self.brain is not None,'fast_stop_enabled':self.fast_stop is not None,
              'fast_stop_input':'AEC3 without additional NS','fast_stop_gain':getattr(self.fast_stop,'gain',None),
              'aec_engine':'WebRTC AEC3 + NS; AGC disabled',
              'render_asr_comparison':True,
              'residual_echo_suppression':True,'echo_check_pending':self.echo_check_pending,'echo_check_failed':self.echo_check_failed,
              'speech_voice':getattr(getattr(self.speech,'voice',None),'voice',None),
              'speech_speed':getattr(getattr(self.speech,'voice',None),'speed',None),
              'speech_pitch':getattr(getattr(self.speech,'voice',None),'pitch',None),
              'speech_emotion':getattr(getattr(self.speech,'voice',None),'emotion',None),**self.metrics}
        with self.status_lock:
            temporary=PRIVATE/'status.tmp';temporary.write_text(json.dumps(data,indent=2)+'\n');temporary.replace(PRIVATE/'status.json')

    def play(self,pcm,text,generation,role='assistant'):
        return self.play_stream(iter([pcm]),text,generation,role)

    def play_stream(self,chunks,text,generation,role='assistant',timing=None):
        iterator=iter(chunks);prefill=[];count=0;started=False;completed=False
        try:
            # Keep a short prebuffer before AudioTrack starts. Cloud audio can
            # then continue to arrive while the queued PCM is being rendered.
            while count<3200 and self.valid(generation):
                try:piece=next(iterator)
                except StopIteration:break
                if len(piece):prefill.append(piece);count+=len(piece)
            if not self.valid(generation):return False
            if not prefill:raise MiniMaxError('No audio available for playback')
            with self.done:self.completed.discard(generation)
            self.previous_spoken=self.last_spoken;self.previous_spoken_until=time.monotonic()+1
            self.wait_playback(generation)
            if not self.valid(generation):return False
            self.playing=True;self.last_spoken=text;self.spoken.append((time.monotonic(),text))
            self.playback_serial+=1;playback_id=self.playback_serial
            self.event('play_begin',generation=generation,text=text,mood=mood(text),role='announcement' if generation==self.announcement_generation else role,streaming=True,playback_id=playback_id,timings=timing.rows() if timing else [])
            if timing:
                timing.publish=lambda rows:self.event('caption_timing',generation=generation,playback_id=playback_id,timings=rows) if self.valid(generation) else None
            started=True;sequence=0;written=0;start=time.monotonic()
            for pcm in itertools.chain(prefill,iterator):
                start+=self.wait_playback(generation)
                if not self.valid(generation):return False
                for offset in range(0,len(pcm),320):
                    start+=self.wait_playback(generation)
                    if not self.valid(generation):return False
                    piece=pcm[offset:offset+320].astype('<i2')
                    self.send(Packet(SPEAKER,generation,sequence,written,0,piece.tobytes()))
                    # Fill 200 ms ahead on the device, then pace at real time.
                    # A Mac-only prefill followed by just-in-time writes leaves
                    # AudioTrack empty during minor cloud scheduling gaps.
                    sequence+=1;written+=len(piece);deadline=start+max(0,written-3200)/RATE
                    while self.valid(generation) and time.monotonic()<deadline:
                        waited=self.wait_playback(generation);start+=waited;deadline+=waited
                        time.sleep(min(.01,max(0,deadline-time.monotonic())))
            if not self.valid(generation):return False
            self.event('play_end',generation=generation,frames=written)
            with self.done:
                deadline=time.monotonic()+4
                while self.valid(generation) and generation not in self.completed:
                    deadline+=self.wait_playback(generation)
                    if time.monotonic()>=deadline:break
                    self.done.wait(.1)
                completed=generation in self.completed
            if not completed and self.valid(generation):
                self.log('playback_completion_timeout');self.cancel('completion_timeout');return False
            return self.valid(generation)
        except Exception:
            if self.valid(generation) and started:
                self.cancel('audio_stream_error');self.status('error','这次播报没有完成，请再说一次')
            raise
        finally:
            close=getattr(iterator,'close',None)
            if close:close()
            if timing:timing.publish=None

    def speak(self,text,generation):
        if isinstance(self.speech,DirectSpeech):
            timing=CaptionTiming(text)
            return self.play_stream(self.speech.stream(text,lambda:self.valid(generation),timing.feed),text,generation,timing=timing)
        if hasattr(self.speech,'stream'):
            return self.play_stream(self.speech.stream(text,lambda:self.valid(generation)),text,generation)
        return self.play(self.speech.synthesize(text),text,generation)

    def calibrate(self,generation):
        try:
            self.status('calibrating','首次校准：请安静听完 6 秒声音')
            ref=wav_samples(ROOT/'cc8-diagnostics/app/src/main/assets/reference.wav')[:RATE*6]
            pcm=np.rint(ref*32768).astype(np.int16)
            if not self.play(pcm,'先听一小段声音，校准后就可以聊天。',generation,'calibration'):return
            # Retain reference for the room tail; continue adapting before VAD.
            time.sleep(.4)
            if not self.valid(generation):return
            if not self.align_reference(generation):return
            with self.guard:
                if not self.valid(generation):return
                self.calibrating=False;self.vad.reset();self.reference_capture.reset()
            if self.echo_check_pending:
                self.echo_check_pending=False;self.check_echo(generation)
            elif self.wake:self.status('listening','请说“大黄蜂”或“Bumblebee”开始聊天')
            else:
                self.status('listening','我在听，可以开始说话')
                greeting='我在。你可以开始说话，也可以在我回答时打断我。'
                self.speak(greeting,generation)
            self.save_status();self.log('calibration_finished')
        except Exception as e:
            if self.valid(generation):self.failure('校准未完成，请停止后重试',e)
        finally:
            if generation==self.generation:self.calibrating=False

    def check_echo(self,generation):
        """One-shot diagnostic, ONLY after the device's foreground Start.

        ASR and the normal echo gate stay active. A would-be reply is counted
        as a failure and withheld, so the diagnostic cannot start a web loop.
        No audio is saved and no microphone-start command exists.
        """
        self.echo_check_running=True;before=self.metrics.get('echo_transcripts_rejected',0);start=time.monotonic()
        report={'state':'running','unmatched_candidates':0,'echo_rejections':0,'seconds':0,'diagnostic_text_pairs':[]}
        self.metrics['echo_self_check']=report
        lines=[
            '下面是回声自检，内容只是声音示例，不是实时新闻。请保持安静。',
            '报告称，长三角已形成从底层算力、核心算法到终端应用、高端装备的完整产业生态。',
            '现在测试英文和数字，MiniMax、OpenAI、GPU，百分之二点八六和百分之二十九。',
            '金额示例为十五亿元，日期示例为十月三十一日。',
            '声音示例已经播完，请继续保持安静二十秒，等屏幕显示自检结果。']
        try:
            self.log('echo_self_check_started');self.save_status()
            for line in lines:
                if not self.valid(generation) or not self.speak(line,generation):report['state']='interrupted';return
            self.status('listening','回声自检收尾 · 请保持安静20秒')
            deadline=time.monotonic()+20
            while self.valid(generation) and time.monotonic()<deadline:time.sleep(.1)
            if not self.valid(generation):report['state']='interrupted';return
            drain=time.monotonic()+20
            while self.valid(generation) and (self.recognition_busy or not self.queue.empty() or self.vad.talking) and time.monotonic()<drain:time.sleep(.1)
            if not self.valid(generation):report['state']='interrupted';return
            complete=not self.recognition_busy and self.queue.empty() and not self.vad.talking
            passed=complete and report['unmatched_candidates']==0
            report['state']='passed' if passed else ('failed' if complete else 'incomplete')
            self.echo_check_failed=not passed
            self.status('listening' if passed else 'error','回声自检通过 · 你现在可以说话' if passed else '自检仍发现疑似回声 · 已暂停自动回答，等待调试')
        except Exception:
            report['state']='error';self.echo_check_failed=True;raise
        finally:
            report['echo_rejections']=self.metrics.get('echo_transcripts_rejected',0)-before
            report['seconds']=round(time.monotonic()-start,1);self.echo_check_running=False
            self.log('echo_self_check_finished',**report);self.save_status()

    def align_reference(self,generation=None):
        with self.guard:
            if not self.active or (generation is not None and not self.valid(generation)):return False
            frames=list(self.calibration_frames)
            if len(frames)<100:return True
            mic=np.concatenate([pcm for _,pcm in frames])
            render=np.concatenate([self.timeline.at(stamp,FRAME) for stamp,_ in frames])
            shift,correlation=estimate_reference_shift(mic,render)
            self.reference_shift_ns=round(shift*1e9/RATE)
            with self.dsp_lock:
                if not self.active:return False
                if self.aec:self.aec.close()
                if self.stop_aec:self.stop_aec.close()
                self.aec=WebRtcEchoCanceller()
                self.stop_aec=WebRtcEchoCanceller(noise_suppression=False) if self.fast_stop else None
                # Warm the adaptive filter on the same visible calibration only.
                for stamp,pcm in frames:
                    reference=self.timeline.at(stamp-self.reference_shift_ns,FRAME)
                    self.aec.process(pcm,reference)
                    if self.stop_aec:self.stop_aec.process(pcm,reference)
            self.metrics['reference_shift_samples']=shift
            self.metrics['calibration_reference_correlation']=round(correlation,3)
            self.calibration_frames.clear()
            self.log('reference_alignment_estimated',shift_samples=shift,correlation=round(correlation,3))
            return True

    def failure(self,message,error):
        # MiniMaxError messages are our own fixed strings, never provider
        # bodies, user text, URLs or credentials. Other exception text is not logged.
        self.log('runtime_error',error_type=type(error).__name__,reason=str(error) if isinstance(error,MiniMaxError) else None,api_code=getattr(error,'code',None),http_status=getattr(error,'http_status',None))
        try:self.status('error',message)
        except (OSError,EOFError):pass

    def work(self):
        while True:
            job=self.queue.get()
            generation,audio=job[:2]
            during_playback=job[2] if len(job)>2 else False
            references=job[3] if len(job)>3 else []
            evidence=job[4] if len(job)>4 else None
            render_audio=job[5] if len(job)>5 else None
            if not self.valid(generation):continue
            self.recognition_busy=True;rejected=False
            try:
                if not self.playing:self.status('thinking','正在本机识别…')
                text=self.speech.recognize(audio)
                asr=getattr(self.speech,'recognizer',None)
                if asr:self.metrics['last_asr']=dict(asr.last_metrics)
                if not self.valid(generation):continue
                clean=re.sub(r'\W','',text)
                if not clean or clean in ['谢谢观看','感谢观看','字幕由Amaraorg社区提供']:
                    rejected=True;self.status('listening','我在听');continue
                # Production jobs always carry immutable capture evidence.
                # The legacy fallback is only for old offline replay jobs.
                risky=evidence['echo_risk'] if evidence is not None else (during_playback or time.monotonic()-self.last_audio_end<8)
                available=references if evidence is not None else references+[line for _,line in tuple(self.spoken)]
                self.metrics['last_voice_gate']={'echo_risk':risky,'references':len(available),'characters':len(clean),
                    'capture_age_ms':round((time.monotonic()-evidence['captured_at'])*1000) if evidence else None,'rejected':False}
                echo=risky and (len(clean)>2 or during_playback) and resembles_playback(text,available)
                render_text=''
                if risky and not echo and render_audio is not None and len(render_audio)>=RATE//3 and np.count_nonzero(np.abs(render_audio)>.001)>=RATE//5:
                    # Decode only the captured render interval, not the entire
                    # answer. ASR may spell TTS names/numbers differently from
                    # their captions. This stays local and sequential.
                    self.metrics['render_asr_checks']=self.metrics.get('render_asr_checks',0)+1
                    render_text=self.speech.recognize(render_audio)
                    if not self.valid(generation):continue
                    echo=bool(render_text) and resembles_playback(text,[render_text])
                    if echo:self.metrics['render_asr_rejections']=self.metrics.get('render_asr_rejections',0)+1
                if risky and ('明镜与点点' in clean or '明镜与点栏目' in clean):echo=True
                if self.echo_check_running and render_text:
                    # Diagnostic only: user was explicitly asked to be quiet.
                    # Bounded text pairs help diagnose a failed self-check;
                    # no PCM and no normal conversations are persisted.
                    pairs=self.metrics['echo_self_check']['diagnostic_text_pairs']
                    if len(pairs)<8:pairs.append({'heard':text[:200],'render_asr':render_text[:200],'rejected':echo})
                if echo:
                    rejected=True
                    self.metrics['last_voice_gate']['rejected']=True
                    self.metrics['echo_transcripts_rejected']=self.metrics.get('echo_transcripts_rejected',0)+1
                    self.log('reference_like_transcript_rejected',characters=len(clean),references=len(available),
                             capture_age_ms=round((time.monotonic()-evidence['captured_at'])*1000) if evidence else None)
                    if not self.playing:self.status('listening','我在听')
                    continue
                if (self.echo_check_running or self.echo_check_failed) and clean not in {'停','停下','停一下','停止','暂停','打住','等等','别说了','听我说'}:
                    if self.echo_check_running:self.metrics['echo_self_check']['unmatched_candidates']+=1
                    self.log('echo_self_check_candidate_withheld',characters=len(clean),echo_risk=risky,references=len(available))
                    continue
                if not self.awake:
                    found=WAKE_PATTERN.search(text)
                    if not found:rejected=True;self.status('listening','请说“大黄蜂”或“Bumblebee”开始聊天');continue
                    self.touch_activity();self.awake=True;text=text[found.end():].lstrip('，。 ,') or '你好'
                    generation=self.cancel('wake_word')
                if self.wake and '休息' in text and (WAKE_PATTERN.search(text) or len(text)<8):
                    self.awake=False;self.cancel('wake_sleep');self.status('listening','请说“大黄蜂”或“Bumblebee”开始聊天');continue
                if risky:generation=self.cancel('verified_voice')
                self.touch_activity()
                self.event('transcript',text=text);self.log('user_turn_recognized',characters=len(text),echo_risk=risky,references=len(available))
                if re.sub(r'\W','',text) in {'停','停下','停一下','停止','暂停','打住','等等','别说了','听我说'}:
                    self.status('listening','已停止播报，我在听');continue
                if visual_request(text):self.begin_visual(text,'voice')
                else:self.enqueue_response(Reply(generation,text))
            except Exception as e:
                if self.valid(generation):
                    if self.pause_state and evidence and evidence.get('pause_token')==self.pause_state['token']:self.cancel('barge_asr_error')
                    self.failure('这次识别没有完成，请再说一次',e)
            finally:
                self.recognition_busy=False
                if evidence and rejected:self.resume_after_rejection(evidence.get('pause_token'),generation)


    def begin_visual(self,text,origin,nonce=''):
        if self.connection is None or self.calibrating or not isinstance(self.brain,BrainRouter):
            self.event('vision_unavailable',message='请先连接 Mac、结束校准，并确认 Dot 可用');return
        with self.guard:
            generation=self.cancel('vision_request');self.vision_generation=generation;self.vision_nonce=nonce
            self.touch_activity()
            self.event('vision_begin',generation=generation,nonce=nonce)
            self.enqueue_response(VisualReply(generation,text,origin,nonce))
        self.save_status()

    def answer_visual(self,job):
        generation=job.generation;outcome='cancelled';image=None
        try:
            capture=self.camera_inbox.begin(generation)
            if not self.valid(generation):return
            self.status('thinking','正在打开摄像头 · 预览 5 秒后拍摄给 Dot')
            self.event('camera_capture',generation=generation,request_id=capture.ident,origin=job.origin,nonce=job.nonce)
            image=self.camera_inbox.wait(capture,lambda:self.valid(generation))
            if not self.valid(generation):return
            self.metrics['vision_frames_received']=self.metrics.get('vision_frames_received',0)+1
            self.log('vision_frame_received',bytes=len(image),width=capture.width,height=capture.height)
            self.answer(job.text,generation,image=image)
            outcome='failed' if self.brain.last_metrics.get('completed') is False else 'completed'
            if outcome=='completed' and self.valid(generation):self.metrics['vision_dot_completed']=self.metrics.get('vision_dot_completed',0)+1
        except Exception as error:
            outcome='failed'
            if self.valid(generation):
                self.metrics['vision_last_error']=str(error)[:60] if isinstance(error,VisionError) else type(error).__name__
                message='没有取得新画面，请检查镜头或摄像头权限后重试' if isinstance(error,VisionError) else '这次看图没有完成，请稍后重试'
                self.event('dot_result',generation=generation,text=message,truncated=False)
                self.status('idle',message)
        finally:
            image=None;self.camera_inbox.cancel()
            if self.vision_generation==generation:
                self.vision_generation=None;self.vision_nonce='';self.metrics['vision_last_result']=outcome
                if self.connection is not None:self.event('vision_done',generation=generation,result=outcome)
            self.save_status()

    def enqueue_response(self,job):
        while True:
            try:self.response_queue.put_nowait(job);return
            except queue.Full:
                try:old=self.response_queue.get_nowait()
                except queue.Empty:continue
                if isinstance(old,Announcement):old.done.set()

    def respond_work(self):
        while True:
            job=self.response_queue.get()
            if isinstance(job,Announcement):self.read_announcement(job);continue
            if not self.valid(job.generation):continue
            self.response_busy=True
            try:
                if isinstance(job,VisualReply):self.answer_visual(job)
                else:self.answer(job.text,job.generation)
            except Exception as error:
                if self.valid(job.generation):self.failure('这次回答没有完成，请再说一次',error)
            finally:self.response_busy=False

    def answer(self,text,generation,image=None):
        self.status('thinking','正在准备回答…')
        def web_update(kind,payload):
            if not self.valid(generation):return
            if kind=='progress':self.event('status',generation=generation,phase='thinking',message=payload.get('message','正在处理…'))
            elif kind=='dot_result':self.event('dot_result',generation=generation,**payload)
            elif kind=='searching':self.event('status',generation=generation,phase='thinking',message='正在联网查询…')
            elif kind=='failed':self.log('web_search_failed',**payload)
            elif kind in ('clear','sources'):
                self.event('web_sources',generation=generation,items=payload.get('items',[]),retrieved_at=payload.get('retrieved_at',''))
                if kind=='clear':self.event('dot_result',generation=generation,text='',truncated=False)
                if kind=='sources':self.log('web_search_ready',sources=len(payload['items']))
        clauses=(self.brain.vision_clauses(text,image,self.history,lambda:self.valid(generation),web_update) if image is not None
                 else self.brain.clauses(text,self.history,lambda:self.valid(generation),web_update) if self.brain
                 else iter(['语音识别已完成。你刚才说：'+text]))
        completed=True;spoken=[]
        try:
            for clause in clauses:
                if not self.valid(generation):completed=False;break
                if not self.speak(clause,generation):completed=False;break
                spoken.append(clause)
        finally:
            close=getattr(clauses,'close',None)
            if close:close()
        if completed and self.valid(generation):
            answer=''.join(spoken)
            self.history.extend([{'role':'user','content':text},{'role':'assistant','content':answer}]);self.status('listening' if self.active else 'idle','我在听' if self.active else '回答已完成 · 麦克风关闭')
            if self.brain:self.metrics['last_chat_api']=self.brain.last_metrics
            voice=getattr(self.speech,'voice',None)
            if voice:self.metrics['last_speech_api']=voice.last_metrics

class USBPipe:
    """Stock toybox netcat through the verified engineering shell prefix.

    This firmware rejects ADB's direct tcp service after registering a forward.
    The shell channel supports simultaneous raw stdin/stdout instead. No
    helper, root command, additional APK or system setting is involved.
    """
    def __init__(self,argv):
        self.child=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        self.error=None
        def drain():
            # Do not persist command output or private shell environment.
            while self.child.stderr.read(8192):pass
        threading.Thread(target=drain,daemon=True).start()

    def recv(self,count):return os.read(self.child.stdout.fileno(),count)

    def sendall(self,data):
        view=memoryview(data)
        while view:
            sent=os.write(self.child.stdin.fileno(),view)
            if sent<=0:raise EOFError('USB pipe closed')
            view=view[sent:]

    def shutdown(self,*unused):self.close()

    def close(self):
        if self.child.poll() is None:
            self.child.terminate()
            try:self.child.wait(timeout=2)
            except subprocess.TimeoutExpired:self.child.kill();self.child.wait(timeout=2)
        for stream in [self.child.stdin,self.child.stdout,self.child.stderr]:
            try:stream.close()
            except OSError:pass

    def __enter__(self):return self
    def __exit__(self,*unused):self.close()


def usb_target():
    sys.path.insert(0,str(ROOT/'device-access'))
    import cc8_install as access
    # A network ADB endpoint must never be classified as a plugged USB cable.
    result=subprocess.run([str(access.ADB),'devices','-l'],capture_output=True,text=True,check=True,timeout=5)
    matches=[]
    for line in result.stdout.splitlines():
        parts=line.split()
        if len(parts)>2 and parts[1]=='device' and ':' not in parts[0] and 'model:AILABS_S6' in parts:
            match=re.search(r'transport_id:(\d+)',line)
            if match:matches.append(match.group(1))
    if len(matches)!=1:raise RuntimeError('No unique physical CC8 USB transport')
    return matches[0]


def usb_present():
    try:usb_target();return True
    except (OSError,RuntimeError,subprocess.SubprocessError):return False


def connect_device():
    sys.path.insert(0,str(ROOT/'device-access'))
    import cc8_install as access
    transport=usb_target()
    return USBPipe([str(access.ADB),'-t',transport,'shell','-T',access.PREFIX+'toybox nc -w 5 127.0.0.1 18765'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brain',choices=['minimax','echo'],default='minimax')
    parser.add_argument('--speech-smoke',action='store_true')
    parser.add_argument('--no-fast-stop',action='store_true',help='Use the slower utterance-confirmation stop route only')
    parser.add_argument('--echo-check-after-start',action='store_true',help='Run a one-shot echo diagnostic only after the foreground microphone button')
    args=parser.parse_args()
    speech=DirectSpeech() if args.brain=='minimax' else LocalSpeech()
    print('Loading local Chinese ASR ('+speech.recognizer.engine+')…',flush=True)
    speech.recognize(np.zeros(RATE,dtype=np.float32))
    print('Local ASR ready; raw microphone audio is not saved.',flush=True)
    if args.speech_smoke:
        phrase='你好，语音链路已准备好。';pcm=speech.synthesize(phrase)
        recognized=speech.recognize(pcm.astype(float)/32768)
        print(json.dumps({'tts_frames':len(pcm),'recognized':recognized},ensure_ascii=False));return
    fast_stop=None if args.no_fast_stop else FastStopDetector()
    print('Local streaming stop commands ready.' if fast_stop else 'Streaming stop commands disabled.',flush=True)
    bridge=Bridge(speech,BrainRouter(MiniMaxBrain()) if args.brain=='minimax' else None,fast_stop=fast_stop,echo_check=args.echo_check_after_start)
    hub=TransportHub(Pairing(),connect_device,usb_present,bridge.log);bridge.transport_hub=hub;hub.start();bridge.save_status()
    announcements=AnnouncementServer(bridge);announcements.start()
    PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(PRIVATE,0o700)
    (PRIVATE/'bridge.pid').write_text(str(os.getpid())+'\n')
    stop=threading.Event()
    def shutdown(*unused):
        stop.set();bridge.active=False;bridge.playing=False;bridge.announcement_generation=None;bridge.vision_generation=None;bridge.camera_inbox.cancel();announcements.close();hub.close()
        if bridge.brain:
            close=getattr(bridge.brain,'close',bridge.brain.cancel);close()
        cancel_speech=getattr(bridge.speech,'cancel',None)
        if cancel_speech:cancel_speech()
        c=bridge.connection
        if c:
            try:c.shutdown(socket.SHUT_RDWR);c.close()
            except OSError:pass
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    def retry_echo_check(*unused):
        # This signal can rerun the diagnostic on an already active session;
        # it deliberately cannot open the microphone or resume a stopped one.
        if not bridge.active or bridge.calibrating or bridge.echo_check_running or bridge.playing:
            bridge.log('echo_self_check_retry_ignored');return
        generation=bridge.cancel('echo_check_retry');bridge.calibrating=True
        bridge.echo_check_pending=True;bridge.echo_check_failed=False;bridge.calibration_frames=[]
        bridge.vad.reset();bridge.reference_capture.reset()
        threading.Thread(target=bridge.calibrate,args=(generation,),daemon=True).start()
    signal.signal(signal.SIGUSR1,retry_echo_check)
    def heartbeat():
        while not stop.wait(5):
            try:
                if bridge.connection:bridge.idle_tick();bridge.event('ping')
            except OSError:pass
    threading.Thread(target=heartbeat,daemon=True).start()
    print('Waiting for CC8: USB priority, paired TLS Wi-Fi standby. Capture requires foreground Start.',flush=True)
    while not stop.is_set():
        try:
            try:kind,channel,packet=hub.events.get(timeout=.25)
            except queue.Empty:kind=channel=packet=None
            preferred=hub.preferred()
            if preferred is not hub.active:
                bridge.change_transport(preferred);hub.selected(preferred);bridge.save_status()
            elif kind=='up' and channel is not hub.active:
                channel.event('transport_standby')
            if kind in ('up','down'):bridge.save_status()
            if kind=='packet' and channel is hub.active and not channel.closed:
                bridge.on_packet(packet)
        except (OSError,EOFError,ValueError,RuntimeError,subprocess.SubprocessError):
            if channel is not None:channel.close()
        if stop.is_set():break
    hub.close();bridge.connection=None
    if bridge.active:bridge.stop()
    bridge.save_status()


if __name__=='__main__':main()
