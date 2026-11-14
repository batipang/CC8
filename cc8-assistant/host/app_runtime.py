"""Backend process entry: own transport/IPC lifetime, signal wiring and event loop.

Factories are supplied by the public entry so tests and launcher choices retain
one injection boundary; construction does not open device capture or a camera.
"""

from __future__ import annotations
import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import queue
import signal
import socket
import sys
import subprocess
import threading
from typing import Callable
from config_schema import ConfigError
from process_lock import atomic_private_bytes
from lifecycle_cleanup import cleanup_all
from backend_signals import BackendSignals


@dataclass(frozen=True)
class BackendDependencies:
    private: Path
    description: str
    load_config: Callable[..., object]
    direct_speech: Callable[..., object]
    local_speech: Callable[..., object]
    fast_stop: Callable[..., object]
    wake_detector: Callable[..., object]
    brain_router: Callable[..., object]
    minimax_brain: Callable[..., object]
    bridge: Callable[..., object]
    workspace: Callable[..., object]
    transport_hub: Callable[..., object]
    pairing: Callable[..., object]
    connect_device: Callable[..., object]
    usb_present: Callable[..., object]
    announcement_server: Callable[..., object]


def reconcile_transport(bridge, hub):
    preferred = hub.preferred()
    uncertain = getattr(hub, 'role_uncertain', False)
    if preferred is hub.active and preferred is bridge.session.connection and not uncertain:
        return False
    if (preferred is not hub.active or uncertain) and not hub.selected(preferred):
        # Keep the working route and its session if the new route failed.
        return True
    try:
        bridge.change_transport(preferred)
    except (OSError, EOFError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if preferred is not None:
            preferred.close()
        bridge.log('transport_bind_failed', error_type=type(error).__name__)
    else:
        bridge.save_status()
    return True


def run_backend(dependencies: BackendDependencies):
    parser = argparse.ArgumentParser(description=dependencies.description)
    parser.add_argument('--brain', choices=['minimax', 'echo'], default='minimax')
    parser.add_argument('--speech-smoke', action='store_true')
    parser.add_argument(
        '--no-fast-stop',
        action='store_true',
        help='Use the slower utterance-confirmation stop route only',
    )
    parser.add_argument(
        '--echo-check-after-start',
        action='store_true',
        help='Run a one-shot echo diagnostic only after the foreground microphone button',
    )
    args = parser.parse_args()
    try:
        config = dependencies.load_config()
    except ConfigError as error:
        parser.error(str(error))
    except OSError:
        parser.error('CC8 configuration file unavailable; check path and owner access')
    if args.brain == 'echo' and config.brain != 'echo':
        # Keep the legacy CLI entry local even with the default cloud ASR.
        config = replace(
            config, brain='echo', asr='sensevoice' if config.asr == 'minimax' else config.asr
        )
    local = config.brain == 'echo'
    if not args.speech_smoke:
        # A direct backend start also needs an owned PID before any provider
        # or transport starts. The global runtime lease is already held.
        try:
            atomic_private_bytes(dependencies.private / 'bridge.pid', f'{os.getpid()}\n'.encode())
        except OSError:
            parser.error('语音后台未能登记进程。请检查运行目录的权限和磁盘空间。')
    try:
        speech = (
            dependencies.local_speech(config=config)
            if local
            else dependencies.direct_speech(config=config)
        )
    except (OSError, RuntimeError):
        if not local:
            raise
        parser.error('本机语音无法初始化。运行 ./cc8 doctor --profile voice 检查本机模型和播报。')
    print(
        'ASR provider configured ('
        + str(speech.recognizer.engine)
        + '); requests require an accepted utterance.',
        flush=True,
    )
    if args.speech_smoke:
        phrase = '你好，语音链路已准备好。'
        pcm = speech.synthesize(phrase)
        recognized = speech.recognize(pcm.astype(float) / 32768)
        print(json.dumps({'tts_frames': len(pcm), 'recognized': recognized}, ensure_ascii=False))
        return
    fast_stop = None if args.no_fast_stop else dependencies.fast_stop()
    print(
        'Local streaming stop commands ready.'
        if fast_stop
        else 'Streaming stop commands disabled.',
        flush=True,
    )
    wake_options = {}
    if getattr(config, 'english_wake_verifier', 'off') == 'whisper_complete':
        wake_options['verify_english_complete'] = True
    if config.chinese_wake_verifier == 'sensevoice_complete':
        wake_detector = dependencies.wake_detector(
            fast_stop, verify_chinese_complete=True, **wake_options
        )
        print('Complete Chinese wake confirmation configured; loads on capture.', flush=True)
    elif config.chinese_wake_verifier == 'sensevoice':
        wake_detector = dependencies.wake_detector(fast_stop, verify_chinese=True, **wake_options)
        print(
            'Chinese wake verifier configured; loads when capture starts.'
            if wake_detector.chinese_verifier is not None
            else 'Chinese wake verifier unavailable; using standard local wake.',
            flush=True,
        )
    else:
        wake_detector = dependencies.wake_detector(fast_stop, **wake_options)
    bridge = dependencies.bridge(
        speech,
        dependencies.brain_router(dependencies.minimax_brain(config=config), selected=config.brain)
        if not local
        else None,
        fast_stop=fast_stop,
        wake_detector=wake_detector,
        wake_window_seconds=config.wake_window_seconds,
        capture_frontend=getattr(config, 'capture_frontend', 'mono'),
        echo_check=args.echo_check_after_start,
        workspace=dependencies.workspace(dependencies.private / 'reminders.json'),
    )
    pairing = dependencies.pairing(port=config.tls_port)
    hub = dependencies.transport_hub(
        pairing,
        lambda: dependencies.connect_device(pairing),
        dependencies.usb_present,
        bridge.log,
        tls_port=config.tls_port,
    )
    bridge.transport_hub = hub
    announcements = None
    stop = threading.Event()
    controls = BackendSignals()

    def shutdown_resources():
        stop.set()

        def cancel_turn():
            try:
                bridge.cancel('shutdown')
            except (OSError, EOFError):
                pass

        def close_channel():
            c = bridge.session.connection
            if not c:
                return

            def shutdown_channel():
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def close_stream():
                try:
                    c.close()
                except OSError:
                    pass

            cleanup_all(shutdown_channel, close_stream)

        def close_brain():
            if bridge.brain:
                getattr(bridge.brain, 'close', bridge.brain.cancel)()

        def cancel_speech():
            cancel = getattr(bridge.speech, 'cancel', None)
            if cancel:
                cancel()

        cleanup_all(
            bridge.session.deactivate,
            lambda: (
                bridge.meetings.close() if getattr(bridge, 'meetings', None) is not None else None
            ),
            bridge.playback.stop_output,
            bridge.announcements.invalidate,
            cancel_turn,
            lambda: announcements.close() if announcements is not None else None,
            hub.close,
            close_brain,
            cancel_speech,
            close_channel,
        )

    signal.signal(signal.SIGTERM, controls.request_stop)
    signal.signal(signal.SIGINT, controls.request_stop)

    def retry_echo_check(scope):
        # This signal can rerun the diagnostic on an already active session;
        # it deliberately cannot open the microphone or resume a stopped one.
        if (
            controls.stopping
            or not bridge.session.active
            or bridge.calibration.active
            or bridge.calibration.running
            or bridge.playback.playing
        ):
            bridge.log('echo_self_check_retry_ignored')
            return
        generation = bridge.cancel(
            'echo_check_retry', expected_generation=scope[0], expected_epoch=scope[1]
        )
        if generation is None or controls.stopping:
            return
        bridge.calibration.prepare_retry()
        threading.Thread(target=bridge.calibrate, args=(generation,), daemon=True).start()

    def request_echo_retry(*unused):
        # These are plain owner fields; validation is atomic in Session.cancel.
        controls.request_retry(
            bridge.session.generation, bridge.session.epoch, bridge.session.active
        )

    signal.signal(signal.SIGUSR1, request_echo_retry)

    def heartbeat():
        while not stop.wait(5):
            if controls.stopping:
                return
            bridge.heartbeat_tick()

    try:
        if controls.stopping:
            return
        hub.start()
        if controls.stopping:
            return
        bridge.save_status()
        announcements = dependencies.announcement_server(bridge)
        if controls.stopping:
            return
        announcements.start()
        if controls.stopping:
            return
        threading.Thread(target=heartbeat, daemon=True).start()
        print(
            'Waiting for CC8: USB priority, paired TLS Wi-Fi standby. Capture requires foreground Start.',
            flush=True,
        )
        while not controls.stopping:
            if (scope := controls.take_retry()) is not None:
                retry_echo_check(scope)
            if controls.stopping:
                break
            channel = None
            try:
                try:
                    kind, channel, packet = hub.events.get(timeout=0.25)
                except queue.Empty:
                    kind = packet = None
                # A signal may have arrived inside a completed transport write.
                # Do not bind/dispatch another packet after that stop request.
                if controls.stopping:
                    break
                if (
                    not reconcile_transport(bridge, hub)
                    and kind == 'up'
                    and channel is not hub.active
                ):
                    channel.event('transport_standby')
                if controls.stopping:
                    break
                if kind in ('up', 'down'):
                    bridge.save_status()
                if (
                    not controls.stopping
                    and kind == 'packet'
                    and channel is hub.active
                    and not channel.closed
                ):
                    bridge.on_packet(packet)
            except (OSError, EOFError, ValueError, RuntimeError, subprocess.SubprocessError):
                if channel is not None:
                    channel.close()
    finally:
        # Run after transport frames have unwound, including startup/loop errors.
        # A secondary cleanup failure must not replace the initiating exception.
        primary = sys.exc_info()[1]
        try:
            cleanup_all(
                shutdown_resources,
                bridge.session.unbind,
                lambda: bridge.stop() if bridge.session.active else None,
                bridge.save_status,
            )
        except Exception as error:
            if primary is None:
                raise
            try:
                bridge.log('backend_cleanup_failed', error_type=type(error).__name__)
            except Exception:
                pass
