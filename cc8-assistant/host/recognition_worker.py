"""Own the bounded ASR queue, worker busy state and transcript/echo decisions.

No device capture or provider is started by constructing this component.
Session/wake, output and diagnostic mutations cross explicit callback ports.
"""

from __future__ import annotations
import queue
import re
import threading
import time
from unicodedata import category
import numpy as np
from diagnostics import ASR_STOP_STAGE
from dsp import RATE
from minimax_client import Cancelled
from reply_types import Reply
from recognition_commands import STOP_COMMANDS
from recognition_commands import SLEEP_COMMAND, stop_command, stop_matches_reference, wake_payload
from recognition_echo import resembles_playback
from session_ports import RecognitionHooks
from tool_asr_compare import comparison_evidence
from vision import visual_request
from speech_correction import command as correction_command
from wake_controller import WAITING
from wake_utterance import WaitingUtterance


from recognition_empty import (
    MIN_ECHO_RISK_CREDIBLE_FRAMES as MIN_ECHO_RISK_CREDIBLE_FRAMES,
    RecognitionOutcome,
    reject_empty,
)


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
                self.erase_complete_wake(old)
                if len(old) > 6:
                    self.hooks.diagnostics().finish(old[6], 'cancelled')
            except queue.Empty:
                pass
            self.queue.put_nowait(job)

    def discard(self):
        while True:
            try:
                old = self.queue.get_nowait()
                self.erase_complete_wake(old)
                if len(old) > 6:
                    self.hooks.diagnostics().gate(old[6], 'stale')
                    self.hooks.diagnostics().finish(old[6], 'cancelled')
            except queue.Empty:
                break

    @staticmethod
    def erase_complete_wake(job):
        # This buffer is owned by the new completed-candidate job. Ordinary
        # caller-owned audio and legacy waiting jobs retain their old contract.
        if len(job) > 7 and isinstance(job[7], WaitingUtterance) and job[7].keyword == '大黄蜂':
            job[1].fill(0)

    def _recognize_current(self, audio, is_current):
        if not is_current():
            raise Cancelled('Turn cancelled')
        speech = self.hooks.speech()
        recognize = getattr(speech, 'recognize_current', None)
        if callable(recognize):
            return recognize(audio, is_current)
        return speech.recognize(audio)

    def work(self):
        while True:
            job = self.queue.get()
            generation, audio = job[:2]
            during_playback = job[2] if len(job) > 2 else False
            references = job[3] if len(job) > 3 else []
            evidence = job[4] if len(job) > 4 else None
            render_audio = job[5] if len(job) > 5 else None
            waiting = job[7] if len(job) > 7 else None
            if not self.hooks.valid(generation):
                self.erase_complete_wake(job)
                if len(job) > 6:
                    self.hooks.diagnostics().gate(job[6], 'stale')
                    self.hooks.diagnostics().finish(job[6], 'cancelled')
                continue
            trace_id = job[6] if len(job) > 6 else self.hooks.diagnostics().begin('voice')
            accepted = False
            self.busy = True
            outcome = RecognitionOutcome()
            checking_render = False

            def is_current():
                return self.hooks.valid(generation) and self.hooks.wake_allowed()

            try:
                confirmed = None
                if (
                    waiting is not None
                    and evidence
                    and not during_playback
                    and not evidence['echo_risk']
                ):
                    confirmed = self.hooks.recheck_wake(waiting, audio, generation)
                # The dequeued job can lose ownership before announcing ASR.
                with self.hooks.lock:
                    if not self.hooks.valid(generation):
                        continue
                    allowed = self.hooks.wake_allowed()
                    if waiting is not None and (
                        type(confirmed) is not bool or confirmed != allowed
                    ):
                        continue
                    if not allowed:
                        outcome.decision = 'wake_closed'
                        outcome.rejected = True
                        if waiting is not None:
                            self.hooks.metric('local_wake_utterances_withheld', increment=True)
                        self.hooks.status('listening', WAITING)
                        continue
                    if waiting is not None:
                        evidence = {**evidence, 'local_wake': True}
                        self.hooks.diagnostics().mark(trace_id, 'wake_detected')
                    if self.hooks.live_owns_capture(audio if waiting is not None else None):
                        outcome.decision = 'stale'
                        continue
                    # A fixed comparison authorizes one capture-bound sample,
                    # not every later VAD candidate in this awake window.
                    upload_rejection = self.hooks.tool_sample_asr_rejection(
                        bool(evidence and evidence.get('local_wake')),
                        evidence.get('captured_at') if evidence else None,
                    )
                    if upload_rejection is not None:
                        outcome.tool_sample_rejection = upload_rejection
                        outcome.decision = 'tool_sample_guard'
                        outcome.rejected = True
                        continue
                    if not self.hooks.playing():
                        self.hooks.status('thinking', '正在识别语音…')
                    self.hooks.diagnostics().asr_start(trace_id, len(audio))
                    self.hooks.metric('voice_asr_attempts', increment=True)
                text = self._recognize_current(audio, is_current)
                self.hooks.diagnostics().mark(trace_id, 'asr_end')
                asr = getattr(self.hooks.speech(), 'recognizer', None)
                if asr:
                    self.hooks.metric('last_asr', dict(asr.last_metrics))
                if not self.hooks.valid(generation) or not self.hooks.wake_allowed():
                    continue
                clean = re.sub('\\W', '', text)
                if not clean or clean in ['谢谢观看', '感谢观看', '字幕由Amaraorg社区提供']:
                    reject_empty(self.hooks, outcome, generation, trace_id, text, clean, evidence)
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
                    checking_render = True
                    self.hooks.diagnostics().mark(trace_id, 'render_asr_start')
                    try:
                        render_text = self._recognize_current(render_audio, is_current)
                    finally:
                        self.hooks.diagnostics().mark(trace_id, 'render_asr_end')
                    checking_render = False
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
                        outcome.decision = echo_decision
                        outcome.rejected = True
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
                        outcome.decision = 'calibration'
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
                        outcome.decision = 'echo_text'
                        outcome.rejected = True
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
                        outcome.decision = 'weak_near'
                        outcome.rejected = True
                        self.hooks.metric('weak_near_rejections', increment=True)
                        if not self.hooks.playing():
                            self.hooks.status('listening', '我在听')
                        continue
                    wake_mode = self.hooks.wake()
                    if risky and not command and not self.hooks.natural_barge_enabled():
                        # Keep explicit stop commands and their echo checks.
                        # Ordinary playback-time speech cannot replace work,
                        # or open a camera in this selected mode.
                        outcome.decision = 'barge_disabled'
                        outcome.rejected = True
                        self.hooks.set_metric_field('last_voice_gate', 'rejected', True)
                        continue
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
                            outcome.decision = 'wake_only'
                            outcome.rejected = True
                            self.hooks.cancel_session('wake_only')
                            self.hooks.status('listening', '我在听，请说出你的问题')
                            continue
                    outcome.tool_sample_rejection = self.hooks.tool_sample_rejection(
                        text, local_wake, evidence.get('captured_at') if evidence else None
                    )
                    if outcome.tool_sample_rejection is not None:
                        outcome.decision = 'tool_sample_guard'
                        outcome.rejected = True
                        outcome.compare_target = self.hooks.tool_sample_comparison(
                            outcome.tool_sample_rejection
                        )
                        if not self.hooks.playing():
                            self.hooks.status(
                                'listening',
                                '正在核对工具测试语音'
                                if outcome.compare_target is not None
                                else '工具测试语音未通过核对',
                            )
                        continue
                    sleep_text = payload if payload is not None else text
                    if wake_mode and SLEEP_COMMAND.fullmatch(sleep_text.strip().rstrip('。.!！')):
                        outcome.decision = 'wake_sleep'
                        self.hooks.set_awake(False)
                        self.hooks.cancel_session('wake_sleep')
                        self.hooks.status('listening', '请说“大黄蜂”或“Bumblebee”开始聊天')
                        continue
                    if risky:
                        generation = self.hooks.cancel_session('verified_voice')
                    self.hooks.diagnostics().bind(trace_id, generation)
                    accepted = True
                    outcome.decision = 'accepted'
                    self.hooks.touch()
                    self.hooks.event('transcript', text=text)
                    self.hooks.log(
                        'user_turn_recognized',
                        characters=len(text),
                        echo_risk=risky,
                        references=len(available),
                    )
                    if self.hooks.owner_access() and self.hooks.photo_voice(
                        text, generation, bool(command)
                    ):
                        continue
                    if command:
                        self.hooks.clear_correction_draft()
                        if not self.hooks.valid(generation):
                            continue
                        self.hooks.diagnostics().mark(trace_id, ASR_STOP_STAGE)
                        self.hooks.diagnostics().finish(trace_id, 'completed')
                        self.hooks.status('listening', '已停止播报，我在听')
                        continue
                    if (
                        self.hooks.owner_access()
                        and correction_command(text) is None
                        and visual_request(text)
                    ):
                        self.hooks.begin_visual(text, 'voice')
                    else:
                        if local_wake:
                            self.hooks.bind_response_probe(
                                text, generation, evidence.get('input_end_ns')
                            )
                        self.hooks.enqueue_response(Reply(generation, text))
            except Cancelled:
                outcome.decision = 'stale'
            except Exception as e:
                outcome.decision = 'error'
                with self.hooks.lock:
                    if checking_render and not is_current():
                        outcome.decision = 'stale'
                        continue
                    self.hooks.diagnostics().failure(trace_id, 'asr', e)
                    if checking_render:
                        # The unadmitted candidate does not own the current
                        # response generation, including between spoken clauses.
                        outcome.rejected = True
                        continue
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
                reason = (
                    outcome.tool_sample_rejection
                    if outcome.decision == 'tool_sample_guard'
                    else None
                )
                if reason is not None and outcome.compare_target is not None:

                    def comparison_current():
                        return (
                            self.hooks.valid(generation)
                            and self.hooks.tool_sample_comparison(reason) is outcome.compare_target
                        )

                    paired = comparison_evidence(
                        outcome.compare_target, text, audio, comparison_current
                    )
                    with self.hooks.lock:
                        if paired is not None and not comparison_current():
                            paired = None
                        self.hooks.diagnostics().gate(trace_id, outcome.decision, reason, paired)
                else:
                    self.hooks.diagnostics().gate(trace_id, outcome.decision, reason)
                self.erase_complete_wake(job)
                self.busy = False
                if not accepted:
                    self.hooks.diagnostics().finish(
                        trace_id, 'rejected' if outcome.rejected else 'cancelled'
                    )
                if evidence and outcome.rejected:
                    try:
                        self.hooks.resume_rejected(evidence.get('pause_token'), generation)
                    except (OSError, EOFError) as error:
                        self.hooks.log(
                            'playback_resume_disconnected', error_type=type(error).__name__
                        )
