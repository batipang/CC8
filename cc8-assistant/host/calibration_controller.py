"""Own startup calibration and explicit one-shot echo self-check lifecycle.

Construction is passive. Only the foreground session can begin calibration;
retries cannot enable a stopped microphone and keep the original time budget.
"""

from __future__ import annotations
from dataclasses import dataclass
import threading
import time
from typing import Callable, Protocol
import numpy as np
from dsp import RATE
from session_ports import EventSink, MetricSink
from wake_controller import WAITING

STARTUP_GREETING = '汽车人，变形！'


class PlayReference(Protocol):
    def __call__(self, pcm: np.ndarray, text: str, generation: int, role: str) -> bool: ...


@dataclass(frozen=True)
class CalibrationHooks:
    lock: threading.RLock
    generation: Callable[[], int]
    valid: Callable[[int], bool]
    wake: Callable[[], bool]
    speech: Callable[[], object]
    clear_frames: Callable[[], None]
    reset_activity: Callable[[], None]
    startup_reference: Callable[[], np.ndarray]
    play: PlayReference
    speak: Callable[[str, int], bool]
    align_reference: Callable[[int], bool]
    check_echo: Callable[[int], None]
    recognition_pending: Callable[[], bool]
    talking: Callable[[], bool]
    wake_allowed: Callable[[], bool]
    status: Callable[[str, str], None]
    save_status: Callable[[], None]
    log: EventSink
    failure: Callable[[str, BaseException], None]
    metric: MetricSink
    metric_value: Callable[[str], float]


class CalibrationController:
    def __init__(self, hooks: CalibrationHooks, *, echo_check=False):
        self.hooks = hooks
        self.active = False
        self.pending = echo_check
        self.running = False
        self.failed = False

    def begin(self):
        self.active = True

    def stop(self):
        self.active = False

    def prepare_retry(self):
        self.active = True
        self.pending = True
        self.failed = False
        self.hooks.clear_frames()
        self.hooks.reset_activity()

    def startup_reference(self):
        # 开场口号同时提供扬声器参考，不再使用检测器的固定女声素材。
        pcm = np.asarray(self.hooks.speech().synthesize(STARTUP_GREETING))
        if (
            pcm.ndim != 1
            or pcm.dtype != np.dtype('int16')
            or not RATE <= len(pcm) <= RATE * 6
            or not np.any(pcm)
        ):
            raise ValueError('Invalid startup reference PCM')
        return pcm

    def calibrate(self, generation):
        try:
            self.hooks.status('calibrating', '正在准备大黄蜂开场音…')
            pcm = self.hooks.startup_reference()
            with self.hooks.lock:
                if not self.hooks.valid(generation):
                    return
                # 云端合成可能耗时，时钟对齐只使用真正播放开始后的收音。
                self.hooks.clear_frames()
            started = time.monotonic()
            self.hooks.status('calibrating', '声音校准中，请稍候…')
            if not self.hooks.play(pcm, STARTUP_GREETING, generation, 'calibration'):
                return
            if not self.hooks.valid(generation):
                return
            self.hooks.status('calibrating', '声音校准中，请稍候…')
            # 保留原 6 秒采样窗口和房间尾音时间；后半段安静等待，不再播放额外提示。
            time.sleep(max(0.4, 6.4 - (time.monotonic() - started)))
            if not self.hooks.valid(generation):
                return
            if not self.hooks.align_reference(generation):
                return
            with self.hooks.lock:
                if not self.hooks.valid(generation):
                    return
                self.active = False
                self.hooks.reset_activity()
            if self.pending:
                self.pending = False
                self.hooks.check_echo(generation)
            elif self.hooks.wake():
                self.hooks.status('listening', WAITING)
            else:
                self.hooks.status('listening', '我在听，可以开始说话')
            self.hooks.save_status()
            self.hooks.log('calibration_finished')
        except Exception as e:
            if self.hooks.valid(generation):
                self.hooks.failure('校准未完成，请停止后重试', e)
        finally:
            if generation == self.hooks.generation():
                self.active = False

    def check_echo(self, generation):
        """One-shot diagnostic, ONLY after the device's foreground Start.

        ASR and the normal echo gate stay active. A would-be reply is counted
        as a failure and withheld, so the diagnostic cannot start a web loop.
        No audio is saved and no microphone-start command exists.
        """
        self.running = True
        before = self.hooks.metric_value('echo_transcripts_rejected')
        start = time.monotonic()
        report = {
            'state': 'running',
            'unmatched_candidates': 0,
            'echo_rejections': 0,
            'seconds': 0,
            'diagnostic_text_pairs': [],
        }
        self.hooks.metric('echo_self_check', report)
        lines = [
            '下面是回声自检，内容只是声音示例，不是实时新闻。请保持安静。',
            '报告称，长三角已形成从底层算力、核心算法到终端应用、高端装备的完整产业生态。',
            '现在测试英文和数字，MiniMax、OpenAI、GPU，百分之二点八六和百分之二十九。',
            '金额示例为十五亿元，日期示例为十月三十一日。',
            '声音示例已经播完，请继续保持安静二十秒，等屏幕显示自检结果。',
        ]
        try:
            if not self.hooks.wake_allowed():
                report['state'] = 'blocked'
                self.hooks.status('error', '自检未运行：请先唤醒或切换为无唤醒词模式')
                return
            self.hooks.log('echo_self_check_started')
            self.hooks.save_status()
            for line in lines:
                if not self.hooks.valid(generation) or not self.hooks.speak(line, generation):
                    report['state'] = 'interrupted'
                    return
            self.hooks.status('listening', '回声自检收尾 · 请保持安静20秒')
            deadline = time.monotonic() + 20
            while self.hooks.valid(generation) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not self.hooks.valid(generation):
                report['state'] = 'interrupted'
                return
            drain = time.monotonic() + 20
            while (
                self.hooks.valid(generation)
                and (self.hooks.recognition_pending() or self.hooks.talking())
                and time.monotonic() < drain
            ):
                time.sleep(0.1)
            if not self.hooks.valid(generation):
                report['state'] = 'interrupted'
                return
            complete = not self.hooks.recognition_pending() and not self.hooks.talking()
            passed = complete and report['unmatched_candidates'] == 0
            report['state'] = 'passed' if passed else ('failed' if complete else 'incomplete')
            self.failed = not passed
            self.hooks.status(
                'listening' if passed else 'error',
                '回声自检通过 · 你现在可以说话'
                if passed
                else '自检仍发现疑似回声 · 已暂停自动回答，等待调试',
            )
        except Exception:
            report['state'] = 'error'
            self.failed = True
            raise
        finally:
            report['echo_rejections'] = (
                self.hooks.metric_value('echo_transcripts_rejected') - before
            )
            report['seconds'] = round(time.monotonic() - start, 1)
            self.running = False
            self.hooks.log('echo_self_check_finished', **report)
            self.hooks.save_status()
