"""Backend process entry: own transport/IPC lifetime, signal wiring and event loop.

Factories are supplied by the public entry so tests and launcher choices retain
one injection boundary; construction does not open device capture or a camera.
"""

from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import threading
from typing import Callable
from config_schema import ConfigError
from process_lock import atomic_private_bytes


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
    if not args.speech_smoke:
        # A direct backend start also needs an owned PID before any provider
        # or transport starts. The global runtime lease is already held.
        try:
            atomic_private_bytes(dependencies.private / 'bridge.pid', f'{os.getpid()}\n'.encode())
        except OSError:
            parser.error('语音后台未能登记进程。请检查运行目录的权限和磁盘空间。')
    speech = (
        dependencies.direct_speech(config=config)
        if args.brain == 'minimax'
        else dependencies.local_speech(config=config)
    )
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
    if config.chinese_wake_verifier == 'sensevoice':
        wake_detector = dependencies.wake_detector(fast_stop, verify_chinese=True)
        print(
            'Chinese wake verifier configured; loads when capture starts.'
            if wake_detector.chinese_verifier is not None
            else 'Chinese wake verifier unavailable; using standard local wake.',
            flush=True,
        )
    else:
        wake_detector = dependencies.wake_detector(fast_stop)
    bridge = dependencies.bridge(
        speech,
        dependencies.brain_router(dependencies.minimax_brain(config=config), selected=config.brain)
        if args.brain == 'minimax'
        else None,
        fast_stop=fast_stop,
        wake_detector=wake_detector,
        wake_window_seconds=config.wake_window_seconds,
        capture_frontend=getattr(config, 'capture_frontend', 'mono'),
        echo_check=args.echo_check_after_start,
        workspace=dependencies.workspace(dependencies.private / 'reminders.json'),
    )
    hub = dependencies.transport_hub(
        dependencies.pairing(port=config.tls_port),
        dependencies.connect_device,
        dependencies.usb_present,
        bridge.log,
        tls_port=config.tls_port,
    )
    bridge.transport_hub = hub
    hub.start()
    bridge.save_status()
    announcements = dependencies.announcement_server(bridge)
    announcements.start()
    stop = threading.Event()

    def shutdown(*unused):
        stop.set()
        bridge.session.deactivate()
        bridge.playback.stop_output()
        bridge.announcements.invalidate()
        try:
            bridge.cancel('shutdown')
        except (OSError, EOFError):
            pass
        announcements.close()
        hub.close()
        if bridge.brain:
            close = getattr(bridge.brain, 'close', bridge.brain.cancel)
            close()
        cancel_speech = getattr(bridge.speech, 'cancel', None)
        if cancel_speech:
            cancel_speech()
        c = bridge.session.connection
        if c:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def retry_echo_check(*unused):
        # This signal can rerun the diagnostic on an already active session;
        # it deliberately cannot open the microphone or resume a stopped one.
        if (
            not bridge.session.active
            or bridge.calibration.active
            or bridge.calibration.running
            or bridge.playback.playing
        ):
            bridge.log('echo_self_check_retry_ignored')
            return
        generation = bridge.cancel('echo_check_retry')
        bridge.calibration.prepare_retry()
        threading.Thread(target=bridge.calibrate, args=(generation,), daemon=True).start()

    signal.signal(signal.SIGUSR1, retry_echo_check)

    def heartbeat():
        while not stop.wait(5):
            bridge.heartbeat_tick()

    threading.Thread(target=heartbeat, daemon=True).start()
    print(
        'Waiting for CC8: USB priority, paired TLS Wi-Fi standby. Capture requires foreground Start.',
        flush=True,
    )
    while not stop.is_set():
        try:
            try:
                kind, channel, packet = hub.events.get(timeout=0.25)
            except queue.Empty:
                kind = channel = packet = None
            if not reconcile_transport(bridge, hub) and kind == 'up' and channel is not hub.active:
                channel.event('transport_standby')
            if kind in ('up', 'down'):
                bridge.save_status()
            if kind == 'packet' and channel is hub.active and not channel.closed:
                bridge.on_packet(packet)
        except (OSError, EOFError, ValueError, RuntimeError, subprocess.SubprocessError):
            if channel is not None:
                channel.close()
        if stop.is_set():
            break
    hub.close()
    bridge.session.unbind()
    if bridge.session.active:
        bridge.stop()
    bridge.save_status()
