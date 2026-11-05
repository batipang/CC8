"""Negotiate an authenticated device session using existing component owners."""

from __future__ import annotations
import threading
from typing import TYPE_CHECKING
from wake_controller import WAITING

if TYPE_CHECKING:
    from session_controller import SessionController


def complete_handshake(session: SessionController, j):
    hooks = session.hooks
    session.capture_rejection_supported = (
        type(j.get('capture_reject_version')) is int and j['capture_reject_version'] == 1
    )
    hooks.visual().update_peer(j)
    hooks.playback().set_pause_capability(
        bool(j.get('supports_playback_pause')),
        applied_receipt=type(j.get('playback_pause_applied_version')) is int
        and j['playback_pause_applied_version'] == 1,
    )
    hooks.playback().set_playout_capability(
        type(j.get('playout_receipt_version')) is int and j['playout_receipt_version'] == 1
    )
    hooks.playback().set_completion_capability(
        type(j.get('playback_done_id_version')) is int and j['playback_done_id_version'] == 1
    )
    hooks.playback().set_clock_report_capability(
        type(j.get('render_clock_report_version')) is int and j['render_clock_report_version'] == 1
    )
    if session.active and not j.get('capture_active', False):
        hooks.stop()
    session.update_wake_mode(bool(j.get('wake_mode', session.wake)))
    interrupted = session.playback_interrupted_by_transport
    if interrupted:
        message = (
            '播报因连接切换中断。已完成的结果可在任务页查看。'
            '如需继续，可以在语音开启后重新提问；执行结果不明的操作请先核对。'
        )
    elif session.active:
        message = '我在听' if session.awake else WAITING
    elif hooks.calibration().pending:
        message = '回声自检已准备 · 点击开始语音后保持安静'
    else:
        message = '大黄蜂已就绪 · 点击开始语音'
    hooks.event(
        'hello',
        message=message,
        transport=session.transport_kind,
        playback_interrupted_by_transport=interrupted,
        supports_dot_chat_handoff=True,
        supports_saved_task_results=True,
        asr_engine=getattr(getattr(hooks.speech(), 'recognizer', None), 'engine', 'sensevoice'),
    )
    # A failed send leaves the notice for the next authenticated route.
    session.playback_interrupted_by_transport = False
    with session.guard:
        # Resume the pending screen update without changing capture ownership.
        session.flush_screen_event()
    hooks.log('device_handshake_verified', transport=session.transport_kind)
    hooks.event('brain_provider', provider=getattr(hooks.brain(), 'selected', 'minimax'))
    hooks.publish_workspace()
    hooks.save_status()


def rebind_capture(session: SessionController, j):
    hooks = session.hooks
    if session.resend_capture_rejection(j.get('capture_id')):
        return
    # A rebind can recover existing capture, but cannot undo a stop.
    if session.rebind_seen or session.capture_start_required:
        return
    session.rebind_seen = True
    if session.active and int(j.get('epoch', -1)) == session.epoch:
        if hooks.calibration().active:
            hooks.capture().echo.clear_calibration_frames()
            threading.Thread(
                target=hooks.calibrate(), args=(session.generation,), daemon=True
            ).start()
        else:
            hooks.status('listening', '连接已切换，我在听' if session.awake else WAITING)
    else:
        hooks.route_event({**j, 'event': 'session_start'})


def apply_settings(session: SessionController, j):
    hooks = session.hooks
    if session.update_wake_mode(bool(j.get('wake_mode', session.wake))):
        if session.active and not hooks.calibration().active:
            hooks.status('listening', WAITING if session.wake else '我在听，可以开始说话')
    if 'brain_provider' in j and hasattr(hooks.brain(), 'select'):
        provider = j['brain_provider']
        if provider in ('minimax', 'dot') and not hooks.calibration().active:
            hooks.cancel('brain_change')
            hooks.brain().select(provider)
            hooks.event('brain_provider', provider=provider)
            hooks.status(
                'listening' if session.active else 'idle',
                '已选择 Dot / Codex · 思考与执行'
                if provider == 'dot'
                else '已选择 MiniMax · 日常对话与联网',
            )
    hooks.save_status()
