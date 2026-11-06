"""Admit device playback receipts against the current playback owner."""

from __future__ import annotations
import time
from typing import TYPE_CHECKING
from operation_failure import OperationFailure

if TYPE_CHECKING:
    from playback_controller import PlaybackController


def accept_device_receipt(playback: PlaybackController, event):
    name = event.get('event')
    if name == 'playout_progress':
        generation = event['generation']
        with playback.publication_lock:
            if (
                playback.device_can_report_playout
                and playback.playing
                and generation == playback.hooks.generation()
                and playback.playout_probe.accept(
                    generation,
                    event['playback_id'],
                    event['first_signal_frame'],
                    event['played_frames'],
                    event['accepted_frames'],
                )
            ):
                # The admission point precedes local stop/publication; its
                # clock and trace key are always the Mac and this generation.
                playback.current_clause_ack_ms = playback.hooks.diagnostics().mark(
                    generation, 'playout_ack'
                )
                playback.hooks.metric('playout_receipts', increment=True)
        return
    if name == 'play_done':
        generation = int(event['generation'])
        # Publish the old clause's state before waking its renderer. That
        # renderer may immediately publish the next clause of this turn.
        # Order is publication -> completion, never completion -> session.
        with playback.publication_lock:
            played = event.get('played_frames')
            if (
                generation != playback.hooks.generation()
                or playback.end_frames is None
                or not playback.matches_completion(event)
                or (played is not None and played < playback.end_frames)
            ):
                return
            with playback.done:
                clock_id = event.get('playback_id')
                clock_frames = (
                    playback.playout_probe.expected_frames(generation, clock_id)
                    if type(clock_id) is int
                    else None
                )
                playback.hooks.complete_clock_report(
                    generation, clock_id, event.get('render_clock'), clock_frames
                )
                confirmed = (
                    type(clock_id) is int
                    and type(played) is int
                    and playback.playout_probe.observed(generation, clock_id)
                )
                playback.hooks.diagnostics().playback_done(
                    generation,
                    confirmed_ack_ms=playback.current_clause_ack_ms if confirmed else None,
                )
                playback.completed = {generation}
                playback.playing = False
                playback.begin_pending = False
                playback.end_frames = None
                playback.last_audio_end = time.monotonic()
                playback.done.notify_all()
    elif name in ('playback_paused', 'playback_resumed'):
        playback.pause.accept_receipt(name, event)
    elif name == 'playback_error':
        generation = int(event.get('generation', playback.hooks.generation()))
        with playback.hooks.lock:
            if generation != playback.hooks.generation():
                return
            with playback.publication_lock:
                if not playback.matches_completion(event) or (
                    playback.device_can_report_completion and not playback.playing
                ):
                    return
            playback.hooks.diagnostics().failure(generation, 'playback', RuntimeError())
            playback.hooks.log(
                'device_playback_error', error_type=str(event.get('error_type', 'Unknown'))[:50]
            )
            playback.hooks.cancel_session('playback_error')
            playback.hooks.failure(
                '设备播放失败',
                OperationFailure(
                    'Device playback failed', stage='playback', kind='provider_or_runtime'
                ),
                trace_key=generation,
            )
