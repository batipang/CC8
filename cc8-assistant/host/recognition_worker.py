"""Own the bounded ASR queue, worker busy state and transcript/echo decisions.

No device capture or provider is started by constructing this component.
Session/wake, output and diagnostic mutations cross explicit callback ports.
"""

from __future__ import annotations
from dataclasses import dataclass
import queue
import re
import threading
import time
from typing import Callable, ContextManager, Mapping, Pattern, Protocol
import numpy as np
from diagnostics import TraceBook
from dsp import RATE, resembles_playback
from reply_types import Reply
from session_ports import EventSink, MetricSink
from vision import visual_request
from wake_controller import WAITING


class FailureSink(Protocol):
    def __call__(
        self, message: str, error: BaseException, *, trace_key: str | int | None = None
    ) -> None: ...


class MetricFieldSink(Protocol):
    def __call__(self, name: str, field: str, value: object) -> None: ...


class PairSink(Protocol):
    def __call__(self, text: str, render_text: str, echo: bool) -> None: ...


@dataclass(frozen=True)
class RecognitionHooks:
    lock: ContextManager[object]
    valid: Callable[[int], bool]
    diagnostics: Callable[[], TraceBook]
    playing: Callable[[], bool]
    last_audio_end: Callable[[], float]
    recent_spoken: Callable[[], list[str]]
    pause_state: Callable[[], Mapping[str, object] | None]
    speech: Callable[[], object]
    metric: MetricSink
    set_metric_field: MetricFieldSink
    add_metric_field: MetricFieldSink
    record_echo_pair: PairSink
    echo_running: Callable[[], bool]
    echo_failed: Callable[[], bool]
    awake: Callable[[], bool]
    wake_allowed: Callable[[], bool]
    wake: Callable[[], bool]
    set_awake: Callable[[bool], None]
    wake_pattern: Pattern[str]
    touch: Callable[[], None]
    event: EventSink
    status: Callable[[str, str], None]
    log: EventSink
    cancel_session: Callable[[str], int]
    begin_visual: Callable[[str, str], None]
    enqueue_response: Callable[[Reply], None]
    failure: FailureSink
    resume_rejected: Callable[[int | None, int], None]


class RecognitionWorker:
    def __init__(self, hooks: RecognitionHooks):
        self.hooks = hooks
        self.queue = queue.Queue(maxsize=2)
        self.busy = False
        self.worker = None
        self.start_lock = threading.Lock()

    def start(self):
        with self.start_lock:
            if self.worker is not None:
                raise RuntimeError('Recognition worker already started')
            self.worker = threading.Thread(target=self.work, daemon=True)
            self.worker.start()

    def enqueue(self, job):
        try:
            self.queue.put_nowait(job)
        except queue.Full:
            try:
                old = self.queue.get_nowait()
                if len(old) > 6:
                    self.hooks.diagnostics().finish(old[6], 'cancelled')
            except queue.Empty:
                pass
            self.queue.put_nowait(job)

    def discard(self):
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def work(self):
        while True:
            job = self.queue.get()
            generation, audio = job[:2]
            during_playback = job[2] if len(job) > 2 else False
            references = job[3] if len(job) > 3 else []
            evidence = job[4] if len(job) > 4 else None
            render_audio = job[5] if len(job) > 5 else None
            if not self.hooks.valid(generation):
                if len(job) > 6:
                    self.hooks.diagnostics().gate(job[6], 'stale')
                    self.hooks.diagnostics().finish(job[6], 'cancelled')
                continue
            trace_id = job[6] if len(job) > 6 else self.hooks.diagnostics().begin('voice')
            accepted = False
            self.busy = True
            rejected = False
            decision = 'stale'
            try:
                if not self.hooks.wake_allowed():
                    decision = 'wake_closed'
                    rejected = True
                    self.hooks.status('listening', WAITING)
                    continue
                if not self.hooks.playing():
                    self.hooks.status('thinking', '正在识别语音…')
                self.hooks.diagnostics().mark(trace_id, 'asr_start')
                text = self.hooks.speech().recognize(audio)
                self.hooks.diagnostics().mark(trace_id, 'asr_end')
                asr = getattr(self.hooks.speech(), 'recognizer', None)
                if asr:
                    self.hooks.metric('last_asr', dict(asr.last_metrics))
                if not self.hooks.valid(generation) or not self.hooks.wake_allowed():
                    continue
                clean = re.sub('\\W', '', text)
                if not clean or clean in ['谢谢观看', '感谢观看', '字幕由Amaraorg社区提供']:
                    decision = 'empty'
                    rejected = True
                    self.hooks.status('listening', '我在听')
                    continue
                risky = (
                    evidence['echo_risk']
                    if evidence is not None
                    else during_playback or time.monotonic() - self.hooks.last_audio_end() < 8
                )
                available = (
                    references if evidence is not None else references + self.hooks.recent_spoken()
                )
                self.hooks.metric(
                    'last_voice_gate',
                    {
                        'echo_risk': risky,
                        'references': len(available),
                        'characters': len(clean),
                        'capture_age_ms': round((time.monotonic() - evidence['captured_at']) * 1000)
                        if evidence
                        else None,
                        'rejected': False,
                    },
                )
                echo = (
                    risky
                    and (len(clean) > 2 or during_playback)
                    and resembles_playback(text, available)
                )
                echo_decision = 'echo_text'
                render_text = ''
                if (
                    risky
                    and (not echo)
                    and (render_audio is not None)
                    and (len(render_audio) >= RATE // 3)
                    and (np.count_nonzero(np.abs(render_audio) > 0.001) >= RATE // 5)
                ):
                    # Only the captured render interval is compared, sequentially
                    # using the selected ASR provider; never decode the full answer.
                    self.hooks.metric('render_asr_checks', increment=True)
                    render_text = self.hooks.speech().recognize(render_audio)
                    if not self.hooks.valid(generation):
                        continue
                    echo = bool(render_text) and resembles_playback(text, [render_text])
                    if echo:
                        echo_decision = 'echo_render'
                        self.hooks.metric('render_asr_rejections', increment=True)
                if risky and ('明镜与点点' in clean or '明镜与点栏目' in clean):
                    echo = True
                if self.hooks.echo_running() and render_text:
                    self.hooks.record_echo_pair(text, render_text, echo)
                if echo:
                    decision = echo_decision
                    rejected = True
                    self.hooks.set_metric_field('last_voice_gate', 'rejected', True)
                    self.hooks.metric('echo_transcripts_rejected', increment=True)
                    self.hooks.log(
                        'reference_like_transcript_rejected',
                        characters=len(clean),
                        references=len(available),
                        capture_age_ms=round((time.monotonic() - evidence['captured_at']) * 1000)
                        if evidence
                        else None,
                    )
                    if not self.hooks.playing():
                        self.hooks.status('listening', '我在听')
                    continue
                if (self.hooks.echo_running() or self.hooks.echo_failed()) and clean not in {
                    '停',
                    '停下',
                    '停一下',
                    '停止',
                    '暂停',
                    '打住',
                    '等等',
                    '别说了',
                    '听我说',
                }:
                    decision = 'calibration'
                    if self.hooks.echo_running():
                        self.hooks.add_metric_field('echo_self_check', 'unmatched_candidates', 1)
                    self.hooks.log(
                        'echo_self_check_candidate_withheld',
                        characters=len(clean),
                        echo_risk=risky,
                        references=len(available),
                    )
                    continue
                if evidence and evidence.get('local_wake') is True:
                    stripped = text.lstrip('，。 ,！!')
                    found = self.hooks.wake_pattern.match(stripped)
                    if found:
                        text = stripped[found.end() :].lstrip('，。 ,！!')
                    if not text:
                        decision = 'wake_only'
                        rejected = True
                        self.hooks.cancel_session('wake_only')
                        self.hooks.status('listening', '我在听，请说出你的问题')
                        continue
                if (
                    self.hooks.wake()
                    and '休息' in text
                    and (self.hooks.wake_pattern.search(text) or len(text) < 8)
                ):
                    decision = 'wake_sleep'
                    self.hooks.set_awake(False)
                    self.hooks.cancel_session('wake_sleep')
                    self.hooks.status('listening', '请说“大黄蜂”或“Bumblebee”开始聊天')
                    continue
                if risky:
                    generation = self.hooks.cancel_session('verified_voice')
                self.hooks.diagnostics().bind(trace_id, generation)
                accepted = True
                decision = 'accepted'
                self.hooks.touch()
                self.hooks.event('transcript', text=text)
                self.hooks.log(
                    'user_turn_recognized',
                    characters=len(text),
                    echo_risk=risky,
                    references=len(available),
                )
                if re.sub('\\W', '', text) in {
                    '停',
                    '停下',
                    '停一下',
                    '停止',
                    '暂停',
                    '打住',
                    '等等',
                    '别说了',
                    '听我说',
                }:
                    self.hooks.diagnostics().finish(trace_id, 'completed')
                    self.hooks.status('listening', '已停止播报，我在听')
                    continue
                if visual_request(text):
                    self.hooks.begin_visual(text, 'voice')
                else:
                    self.hooks.enqueue_response(Reply(generation, text))
            except Exception as e:
                decision = 'error'
                self.hooks.diagnostics().failure(trace_id, 'asr', e)
                with self.hooks.lock:
                    if self.hooks.valid(generation):
                        try:
                            if (
                                self.hooks.pause_state()
                                and evidence
                                and (
                                    evidence.get('pause_token') == self.hooks.pause_state()['token']
                                )
                            ):
                                self.hooks.cancel_session('barge_asr_error')
                        except (OSError, EOFError):
                            pass
                        self.hooks.failure('这次识别没有完成，请再说一次', e, trace_key=trace_id)
            finally:
                self.hooks.diagnostics().gate(trace_id, decision)
                self.busy = False
                if not accepted:
                    self.hooks.diagnostics().finish(
                        trace_id, 'rejected' if rejected else 'cancelled'
                    )
                if evidence and rejected:
                    try:
                        self.hooks.resume_rejected(evidence.get('pause_token'), generation)
                    except (OSError, EOFError) as error:
                        self.hooks.log(
                            'playback_resume_disconnected', error_type=type(error).__name__
                        )
