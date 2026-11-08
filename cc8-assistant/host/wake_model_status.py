"""Local model observations and waiting feedback without owning model lifetime.

Snapshots remain read-only. The caller retains the original session/native
locks and owns deduplication; health reporting never starts a worker here.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wake_controller import WakeController

WAITING = '等待唤醒 · 请说“大黄蜂”或“Bumblebee”'
RECOVERING = '本地中文唤醒正在恢复，请稍后再说'
RECOVERY_FAILED = '本地中文唤醒复核暂不可用，请暂停语音后重新开始'
ENGLISH_RECOVERING = '英文唤醒增强正在恢复，基础英文唤醒仍可用'
ENGLISH_RECOVERY_FAILED = '英文唤醒增强暂不可用，基础英文唤醒仍可用；可暂停语音后重新开始'


def report_model_recovery(self: WakeController):
    # Called only under the session guard for a current waiting frame.
    models = []
    chinese = self.complete_recovery_snapshot
    if chinese is not None and chinese['state'] != 'stopped':
        models.append(('chinese', self._model_generation(), chinese['state']))
    english = getattr(self.detector, 'english_recovery_snapshot', None)
    if (
        self.complete_english_enabled
        and isinstance(english, dict)
        and english['state'] != 'stopped'
    ):
        models.append(('english', self._english_generation(), english['state']))
    if not models:
        return
    # Chinese verification is required for its configured main path;
    # optional English recovery must not hide that failure or clear it.
    pending = next((model for model in models if model[2] != 'ready'), None)
    key = pending or ('ready', tuple(models))
    if key == self.last_recovery_status or self.hooks.render_risk() or self.hooks.busy():
        return
    self.last_recovery_status = key
    if pending is None:
        self.hooks.status('listening', WAITING)
        return
    language, _, state = pending
    recovering, failed = (
        (RECOVERING, RECOVERY_FAILED)
        if language == 'chinese'
        else (ENGLISH_RECOVERING, ENGLISH_RECOVERY_FAILED)
    )
    self.hooks.status(
        # The shipped HUD replaces listening detail with a generic label.
        # Use its visible recoverable-error surface until ready again.
        'error',
        failed if state == 'failed' else recovering,
    )


def chinese_verifier_ready(self: WakeController):
    if not self.chinese_verifier_configured:
        return False
    verifier = getattr(self.detector, 'chinese_verifier', None)
    worker = (
        verifier.worker
        if verifier is not None
        else getattr(self.detector, 'chinese_complete', None)
    )
    if worker is None:
        return False
    recovery = self.complete_recovery_snapshot
    if recovery is not None:
        # Capture frames own readiness polling and recovery. Health must
        # not wait for a concurrent process spawn/close state lock.
        return worker.last_ready is True and recovery['state'] == 'ready'
    if not self.native_lock.acquire(blocking=False):
        return worker.last_ready is True
    try:
        return worker.ready is True
    finally:
        self.native_lock.release()


def english_input_samples(self: WakeController):
    # A stalled decoder must not block the service health endpoint.
    if not self.native_lock.acquire(timeout=0.01):
        return None
    try:
        counts = getattr(self.detector, 'english_input_samples', None)
        if type(counts) is not tuple or len(counts) != 2:
            return None
        primary, alternate = counts
        return {'primary': primary, 'alternate': alternate}
    finally:
        self.native_lock.release()
