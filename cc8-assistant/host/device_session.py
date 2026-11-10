"""Negotiate an authenticated device session using existing component owners."""

from __future__ import annotations
import threading
import time
from typing import TYPE_CHECKING
from wake_controller import WAITING
from route_output import same_route
from window_input import available as window_available
from runtime_versions import public_release

if TYPE_CHECKING:
    from session_controller import SessionController


def complete_handshake(session: SessionController, j):
    hooks = session.hooks
    owner = hooks.owner_access()
    if owner:
        hooks.meeting_peer(j.get('meeting_version'))
        hooks.visual().update_peer(j)
    session.correction_ui_supported = owner and (
        type(j.get('speech_correction_version')) is int and j['speech_correction_version'] == 1
    )
    frontend = session.negotiate_capture_frontend(j)
    hooks.capture().paired_peer_ready = frontend == 'paired'
    session.capture_rejection_supported = (
        type(j.get('capture_reject_version')) is int and j['capture_reject_version'] == 1
    )
    session.capture_start_ack = (
        session.capture_rejection_supported
        and type(j.get('capture_start_ack_version')) is int
        and j['capture_start_ack_version'] == 1
    )
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
        release_info=public_release(),
        message=message,
        transport=session.transport_kind,
        capture_frontend=frontend,
        playback_interrupted_by_transport=interrupted,
        supports_dot_chat_handoff=owner,
        supports_report_sources=owner,
        supports_report_archive=owner,
        supports_thread_queue=owner,
        supports_report_progress=owner,
        supports_saved_task_results=owner,
        supports_preference_memory=owner,
        supports_vocabulary=owner,
        supports_workflows=owner,
        supports_meetings=owner
        and type(j.get('meeting_version')) is int
        and j['meeting_version'] == 1,
        meeting_state=hooks.meeting_state() if owner else {},
        supports_vision_modes=owner,
        supports_photo_followup=owner,
        supports_mac_window=owner and window_available(),
        supports_result_provenance=owner,
        supports_result_reading=owner,
        voice_preferences=session.voice_preferences_state(),
        asr_engine=getattr(getattr(hooks.speech(), 'recognizer', None), 'engine', 'sensevoice'),
        **(
            {'capture_start_token': session.capture_start_token}
            if session.capture_start_ack
            else {}
        ),
    )
    # A failed send leaves the notice for the next authenticated route.
    session.playback_interrupted_by_transport = False
    with session.guard:
        # Resume the pending screen update without changing capture ownership.
        # An old route may have accepted the write before its device-side
        # handler was superseded. Reconcile once on the next authenticated activation.
        if (
            session.active
            and session.pending_screen_event is None
            and not same_route(
                session.screen_synced_owner, session.connection, session.bound_route_token
            )
        ):
            session.pending_screen_event = (
                'screen_sleep' if session.screen_sleeping else 'screen_wake'
            )
        session.flush_screen_event()
    hooks.log('device_handshake_verified', transport=session.transport_kind)
    hooks.event(
        'brain_provider',
        provider=getattr(hooks.brain(), 'selected', 'minimax') if owner else 'guest_minimax',
    )
    hooks.publish_workspace()
    hooks.save_status()


def rebind_capture(session: SessionController, j):
    hooks = session.hooks
    if not hooks.dialogue_access():
        session.reject_capture_start(j.get('capture_id'))
        return
    if not session.accepts_capture_frontend(j):
        session.reject_capture_start(j.get('capture_id'))
        return
    if session.resend_capture_rejection(j.get('capture_id')):
        return
    if session.capture_start_ack or session.capture_id is not None or 'capture_start_token' in j:
        if not session.capture_claim_matches(j):
            session.reject_capture_start(j.get('capture_id'))
            return
    # A rebind can recover existing capture, but cannot undo a stop.
    if session.rebind_seen or session.capture_start_required:
        return
    session.rebind_seen = True
    session.capture_route_pending = False
    if session.active and int(j.get('epoch', -1)) == session.epoch:
        hooks.wake_reset('rebind')
        if hooks.calibration().active:
            hooks.capture().echo.clear_calibration_frames()
            threading.Thread(
                target=hooks.calibrate(), args=(session.generation,), daemon=True
            ).start()
        else:
            hooks.status('listening', '连接已切换，我在听' if session.awake else WAITING)
    else:
        hooks.route_event({**j, 'event': 'session_start'})


def start_capture(session: SessionController, j):
    capture_id = j.get('capture_id')
    if not session.hooks.dialogue_access() or session.hooks.capture_reserved():
        session.reject_capture_start(capture_id)
        return
    if not session.accepts_capture_frontend(j):
        session.reject_capture_start(capture_id)
        return
    negotiated = (
        session.capture_start_ack or session.capture_id is not None or 'capture_start_token' in j
    )
    if negotiated:
        if session.capture_claim_matches(j):
            # The first ACK may have been lost before a surviving route took over.
            session.rebind(j)
            session.confirm_capture_start()
            return
        if (
            not session.capture_start_ack
            or capture_id is None
            or j.get('capture_start_token') != session.capture_start_token
            or capture_id in session.consumed_capture_ids
            or (session.active and int(j['epoch']) == session.epoch)
        ):
            session.reject_capture_start(capture_id)
            return
        session.consumed_capture_ids.append(capture_id)
    if session.resend_capture_rejection(capture_id):
        return
    session.rebind_seen = True
    session.capture_route_pending = False
    # A route rebind can arrive before the capture worker's first start.
    # Both describe the same native recording epoch; never restart it.
    if session.active and int(j['epoch']) == session.epoch:
        return
    if session.hooks.visual().generation is not None or session.text_generation is not None:
        session.hooks.cancel('microphone_start')
    if session.active or session.hooks.announcements().generation is not None:
        session.hooks.stop()
    session.epoch = int(j['epoch'])
    session.capture_start_required = True
    session.generation += 1
    session.hooks.capture().reset_session()
    session.capture_frontend = j.get('capture_frontend', 'mono')
    session.hooks.capture().paired_reference = session.capture_frontend == 'paired'
    session.hooks.metric('capture_frontend', session.capture_frontend)
    session.hooks.metric('capture_fallback', j.get('capture_fallback_reason'))
    session.hooks.playback().reset_session()
    try:
        session.hooks.capture().echo.open()
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        if not session.capture_rejection_supported:
            if isinstance(error, ImportError):
                raise RuntimeError('Local echo processor unavailable') from None
            raise
        session.rejected_capture_id = capture_id
        session.hooks.log('capture_initialization_failed', error_type=type(error).__name__)
        session.hooks.save_status()
        # Keep the authenticated link on a local processor failure. If
        # sending fails, a rebind can resend to this same recording only.
        session.resend_capture_rejection(capture_id)
        return
    session.capture_start_required = False
    session.active = True
    session.capture_id = capture_id if negotiated else None
    session.tool_samples.bind_capture(session)
    session.hooks.calibration().begin()
    session.awake = not session.wake
    session.screen_sleeping = False
    session.last_interaction = time.monotonic()
    session.hooks.wake_reset('start')
    for k in [
        'mic_frames',
        'render_frames',
        'sequence_gaps',
        'interruptions',
        'utterances',
        'fast_stop_count',
        'echo_transcripts_rejected',
        'weak_near_rejections',
    ]:
        session.hooks.metric(k, 0)
    generation = session.generation
    session.hooks.prepare_brain(generation)
    threading.Thread(target=session.hooks.calibrate(), args=(generation,), daemon=True).start()
    session.hooks.log('foreground_capture_started')
    if negotiated:
        session.confirm_capture_start()


def stop_capture(session: SessionController, j):
    capture_id = j.get('capture_id')
    if (
        session.capture_start_ack
        and capture_id is not None
        and capture_id not in session.consumed_capture_ids
    ):
        session.consumed_capture_ids.append(capture_id)
    if session.capture_id is not None and (
        capture_id != session.capture_id or j.get('epoch') != session.epoch
    ):
        return
    if not session.active:
        # A native capture worker can stop before its Start reaches us.
        session.capture_start_required = True
    elif int(j.get('epoch', session.epoch)) == session.epoch:
        session.hooks.stop()
        session.hooks.log('foreground_capture_stopped')


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
