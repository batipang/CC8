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
from unicodedata import category
from typing import Callable, ContextManager, Mapping, Pattern, Protocol
import numpy as np
from diagnostics import ASR_STOP_STAGE, TraceBook
from dsp import RATE, STOP_COMMANDS, resembles_playback
from qa_speech import SCENARIO_QUESTIONS, normalize_question, tool_asr_result
from reply_types import Reply
from session_ports import EventSink, MetricSink
from tool_asr_compare import compare_local
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
    bind_response_probe: Callable[[str, int, int | None], None] = lambda *_: None
    tool_sample_rejection: Callable[[str, bool, float | None], str | None] = lambda *_: None
    tool_sample_comparison: Callable[[str], tuple[int, str] | None] = lambda _: None


MIN_ECHO_RISK_CREDIBLE_FRAMES = 5
WAKE_PREFIX_SEPARATORS = ' \t\r\n，。,！!：:'
WAKE_QUOTES = {'“': '”', '「': '」', '『': '』', '"': '"'}
SLEEP_COMMAND = re.compile(r'(?:请)?(?:你)?(?:先)?(?:去)?休息(?:一下|一会儿?)?吧?')


def wake_payload(text, wake_pattern):
    candidate = text.lstrip(WAKE_PREFIX_SEPARATORS)
    closing_quote = WAKE_QUOTES.get(candidate[:1])
    if closing_quote:
        candidate = candidate[1:].lstrip(WAKE_PREFIX_SEPARATORS)
    prefix = wake_pattern.match(candidate)
    if prefix is None:
        return None
    tail = candidate[prefix.end() :]
    if closing_quote:
        tail = tail.removeprefix(closing_quote)
    return tail.lstrip(WAKE_PREFIX_SEPARATORS)


def stop_command(text, wake_pattern):
    command = re.sub(r'\W', '', text)
    if command in STOP_COMMANDS:
        return command
    payload = wake_payload(text, wake_pattern)
    command = re.sub(r'\W', '', payload) if payload is not None else ''
    return command if command in STOP_COMMANDS else ''


def stop_matches_reference(command, references):
    for reference in references:
        clean = re.sub(r'\W', '', reference)
        if clean == command or (len(command) > 1 and command in clean):
            return True
    return False


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
                old = self.queue.get_nowait()
                if len(old) > 6:
                    self.hooks.diagnostics().gate(old[6], 'stale')
                    self.hooks.diagnostics().finish(old[6], 'cancelled')
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
            tool_sample_rejection = None
            compare_target = None
            try:
                # The dequeued job can lose ownership before announcing ASR.
                with self.hooks.lock:
                    if not self.hooks.valid(generation):
                        continue
                    if not self.hooks.wake_allowed():
                        decision = 'wake_closed'
                        rejected = True
                        self.hooks.status('listening', WAITING)
                        continue
                    if not self.hooks.playing():
                        self.hooks.status('thinking', '正在识别语音…')
                    self.hooks.diagnostics().asr_start(trace_id, len(audio))
                    self.hooks.metric('voice_asr_attempts', increment=True)
                text = self.hooks.speech().recognize(audio)
                self.hooks.diagnostics().mark(trace_id, 'asr_end')
                asr = getattr(self.hooks.speech(), 'recognizer', None)
                if asr:
                    self.hooks.metric('last_asr', dict(asr.last_metrics))
                if not self.hooks.valid(generation) or not self.hooks.wake_allowed():
                    continue
                clean = re.sub('\\W', '', text)
                if not clean or clean in ['谢谢观看', '感谢观看', '字幕由Amaraorg社区提供']:
                    captured = (
                        self.hooks.diagnostics().capture_record(trace_id) if not clean else None
                    )
                    with self.hooks.lock:
                        if not self.hooks.valid(generation) or not self.hooks.wake_allowed():
                            continue
                        # A matched local cue can still produce an empty cloud
                        # transcript. Pair it only inside the explicit QA guard.
                        if (
                            not clean
                            and evidence is not None
                            and evidence.get('local_wake') is True
                            and evidence.get('echo_risk') is False
                            and not self.hooks.playing()
                            and not (self.hooks.echo_running() or self.hooks.echo_failed())
                            and (target := self.hooks.tool_sample_comparison('comparison_only'))
                            is not None
                        ):
                            tool_sample_rejection = self.hooks.tool_sample_rejection(
                                text, True, evidence.get('captured_at')
                            )
                            if tool_sample_rejection is not None:
                                decision = 'tool_sample_guard'
                                rejected = True
                                if tool_sample_rejection == 'comparison_only':
                                    compare_target = target
                                self.hooks.status(
                                    'listening',
                                    '识别对照采样完成'
                                    if compare_target is not None
                                    else '工具测试语音未通过核对',
                                )
                                continue
                        decision = 'empty'
                        rejected = True
                        if not self.hooks.playing():
                            ask_again = (
                                captured is not None
                                and not captured['echo_risk']
                                and captured['credible_frames'] >= MIN_ECHO_RISK_CREDIBLE_FRAMES
                                and self.hooks.awake()
                                and not (self.hooks.echo_running() or self.hooks.echo_failed())
                            )
                            self.hooks.status(
                                'listening', '刚才没听清，请再说一遍' if ask_again else '我在听'
                            )
                    continue
                command = stop_command(text, self.hooks.wake_pattern)
                risky = (
                    evidence['echo_risk']
                    if evidence is not None
                    else during_playback or time.monotonic() - self.hooks.last_audio_end() < 8
                )
                available = (
                    references if evidence is not None else references + self.hooks.recent_spoken()
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
                    if not self.hooks.valid(generation) or not self.hooks.wake_allowed():
                        continue
                    # Only the captured render interval is compared, sequentially
                    # using the selected ASR provider; never decode the full answer.
                    self.hooks.metric('render_asr_checks', increment=True)
                    self.hooks.metric('voice_asr_attempts', increment=True)
                    render_text = self.hooks.speech().recognize(render_audio)
                    if not self.hooks.valid(generation):
                        continue
                    echo = bool(render_text) and resembles_playback(text, [render_text])
                    if echo:
                        echo_decision = 'echo_render'
                if risky and ('明镜与点点' in clean or '明镜与点栏目' in clean):
                    echo = True
                captured = self.hooks.diagnostics().capture_record(trace_id)
                # Recheck after reading evidence, then linearize admission,
                # echo feedback, wake/stop actions and routing with the session guard.
                # The capture-record lock was released before entering here.
                with self.hooks.lock:
                    if not self.hooks.valid(generation) or not self.hooks.wake_allowed():
                        continue
                    capture_age_ms = (
                        round((time.monotonic() - evidence['captured_at']) * 1000)
                        if evidence
                        else None
                    )
                    self.hooks.metric(
                        'last_voice_gate',
                        {
                            'echo_risk': risky,
                            'references': len(available),
                            'characters': len(clean),
                            'capture_age_ms': capture_age_ms,
                            'rejected': False,
                        },
                    )
                    if self.hooks.echo_running() and render_text:
                        self.hooks.record_echo_pair(text, render_text, echo)
                    if echo:
                        decision = echo_decision
                        rejected = True
                        if echo_decision == 'echo_render':
                            self.hooks.metric('render_asr_rejections', increment=True)
                        self.hooks.set_metric_field('last_voice_gate', 'rejected', True)
                        self.hooks.metric('echo_transcripts_rejected', increment=True)
                        self.hooks.log(
                            'reference_like_transcript_rejected',
                            characters=len(clean),
                            references=len(available),
                            capture_age_ms=capture_age_ms,
                        )
                        if not self.hooks.playing():
                            self.hooks.status('listening', '我在听')
                        continue
                    if (
                        self.hooks.echo_running() or self.hooks.echo_failed()
                    ) and clean not in STOP_COMMANDS:
                        decision = 'calibration'
                        if self.hooks.echo_running():
                            self.hooks.add_metric_field(
                                'echo_self_check', 'unmatched_candidates', 1
                            )
                        self.hooks.log(
                            'echo_self_check_candidate_withheld',
                            characters=len(clean),
                            echo_risk=risky,
                            references=len(available),
                        )
                        continue
                    weak_capture = (
                        risky
                        and captured is not None
                        and captured['echo_risk']
                        and captured['credible_frames'] < MIN_ECHO_RISK_CREDIBLE_FRAMES
                    )
                    if weak_capture and command and stop_matches_reference(command, available):
                        # A decoded stop normally bypasses short-text echo checks.
                        # A matching playback quote needs confirmed near speech.
                        decision = 'echo_text'
                        rejected = True
                        self.hooks.set_metric_field('last_voice_gate', 'rejected', True)
                        self.hooks.metric('echo_transcripts_rejected', increment=True)
                        if not self.hooks.playing():
                            self.hooks.status('listening', '我在听')
                        continue
                    if weak_capture and not captured['local_wake'] and not command:
                        # ASR can turn a brief residual/noise burst into unrelated
                        # fluent text. Require 50 ms of the existing near detector's
                        # confirmed output before committing a new risky request.
                        # This does not change VAD energy, tentative pause or ASR.
                        decision = 'weak_near'
                        rejected = True
                        self.hooks.metric('weak_near_rejections', increment=True)
                        if not self.hooks.playing():
                            self.hooks.status('listening', '我在听')
                        continue
                    wake_mode = self.hooks.wake()
                    local_wake = evidence is not None and evidence.get('local_wake') is True
                    payload = (
                        wake_payload(text, self.hooks.wake_pattern)
                        if local_wake or wake_mode
                        else None
                    )
                    if local_wake and payload is not None:
                        text = payload
                        # ASR can add punctuation to the name alone. Detect
                        # that without stripping signs or decimal points from a query.
                        if all(ch.isspace() or category(ch).startswith('P') for ch in text):
                            decision = 'wake_only'
                            rejected = True
                            self.hooks.cancel_session('wake_only')
                            self.hooks.status('listening', '我在听，请说出你的问题')
                            continue
                    tool_sample_rejection = self.hooks.tool_sample_rejection(
                        text, local_wake, evidence.get('captured_at') if evidence else None
                    )
                    if tool_sample_rejection is not None:
                        decision = 'tool_sample_guard'
                        rejected = True
                        compare_target = self.hooks.tool_sample_comparison(tool_sample_rejection)
                        if not self.hooks.playing():
                            self.hooks.status(
                                'listening',
                                '正在核对工具测试语音'
                                if compare_target is not None
                                else '工具测试语音未通过核对',
                            )
                        continue
                    sleep_text = payload if payload is not None else text
                    if wake_mode and SLEEP_COMMAND.fullmatch(sleep_text.strip().rstrip('。.!！')):
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
                    if command:
                        self.hooks.diagnostics().mark(trace_id, ASR_STOP_STAGE)
                        self.hooks.diagnostics().finish(trace_id, 'completed')
                        self.hooks.status('listening', '已停止播报，我在听')
                        continue
                    if visual_request(text):
                        self.hooks.begin_visual(text, 'voice')
                    else:
                        if local_wake:
                            self.hooks.bind_response_probe(
                                text, generation, evidence.get('input_end_ns')
                            )
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
                reason = tool_sample_rejection if decision == 'tool_sample_guard' else None
                if reason is not None and compare_target is not None:
                    case, engine = compare_target
                    expected = normalize_question(SCENARIO_QUESTIONS['tool'][case - 1])
                    primary = tool_asr_result(text, expected)

                    def comparison_current():
                        return (
                            self.hooks.valid(generation)
                            and self.hooks.tool_sample_comparison(reason) is compare_target
                        )

                    try:
                        local = (
                            compare_local(case, audio, comparison_current, engine=engine)
                            if comparison_current()
                            else None
                        )
                    except Exception:
                        # An optional local diagnostic cannot stop the recognition worker.
                        local = 'error'
                    with self.hooks.lock:
                        comparison = (
                            {'primary': primary, 'local': local}
                            if local is not None and comparison_current()
                            else None
                        )
                        if comparison is not None and engine != 'whisper':
                            comparison['local_engine'] = engine
                        self.hooks.diagnostics().gate(trace_id, decision, reason, comparison)
                else:
                    self.hooks.diagnostics().gate(trace_id, decision, reason)
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
