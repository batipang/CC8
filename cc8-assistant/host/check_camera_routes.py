"""Plan, or explicitly run two visible metadata-only camera/transport interruption checks."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from camera_route_check import CASES, CameraRouteRun, require

ROOT = Path(__file__).resolve().parents[2]


def plan(selected):
    return dict(
        schema=1,
        execute=False,
        cases=list(selected),
        scope='visible_camera_preview_logical_route_interruption',
        camera_requests_at_most=len(selected),
        preview_seconds=5,
        requires_foreground=True,
        requires_microphone_paused=True,
        requires_paired_usb_and_wifi=True,
        metadata_only=True,
        camera_started=False,
        images_exported=False,
        images_saved=False,
        model_requests_started=False,
        microphone_started=False,
        physical_usb_unplug_tested=False,
        full_p0_acceptance=False,
        minimum_backend_capability='camera_metadata_only_supported',
    )


def store_identity(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        require(not path.is_symlink(), 'private_store_linked')
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o077
            and 0 < info.st_size <= 256 * 1024,
            'private_store_identity_unconfirmed',
        )
        data = stream.read(256 * 1024 + 1)
        require(len(data) == info.st_size, 'private_store_changed_while_reading')
        return hashlib.sha256(data).hexdigest()


def execute(runtime, selected):
    # Plan/help above have no voice dependencies, sockets, file creation or ADB.
    import fcntl
    from camera_route_probe import CameraProbe
    from device_smoke import Device, rpc
    from service_smoke import LiveService
    from private_output import PrivateOutput, private_directory
    from release_info import load_release

    require(sys.platform == 'darwin', 'camera_route_checks_require_macos')
    require(
        runtime.is_absolute() and runtime.resolve() == runtime, 'runtime_root_must_be_real_absolute'
    )
    release = load_release(runtime)
    socket = runtime / 'device-access/private/voice-runtime/announce.sock'

    def source_bound(value):
        require(
            value.get('bridge_version') == release.backend_version
            and value.get('release_id') == release.release_id
            and type(value.get('protocol_version')) is int
            and value['protocol_version'] == release.protocol_version,
            'camera_runtime_source_changed',
        )
        require(
            value.get('camera_metadata_only_supported') is True, 'metadata_only_camera_unsupported'
        )
        return value

    # Reject an old backend before even constructing the Android access client.
    source_bound(rpc(socket, {'operation': 'health'}))
    private = private_directory(socket.parent)
    fd = os.open(
        private / 'automation-test.lock',
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    with os.fdopen(fd, 'r+') as lock:
        info = os.fstat(lock.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o077,
            'unsafe_camera_test_lock',
        )
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        device = Device()  # Existing unique USB target / AILABS_S6 / SDK27 / UID2000 gates.
        service = LiveService(runtime, device)
        initial = service.sample(include_activity=True)
        require(initial is not None, 'camera_owner_service_not_idle')
        paths = [
            private / name
            for name in (
                'reminders.json',
                'preference-memory.json',
                'recognition-vocabulary.json',
                'work-report-sources.json',
                'work-report-archive.json',
                'work-report-progress.json',
                'work-report-watch.json',
                'workflows.json',
            )
        ]
        identities = [store_identity(path) for path in paths]
        owner_config = Path.home() / '.codex/config.toml'
        owner_hash = hashlib.sha256(owner_config.read_bytes()).hexdigest()
        agent_hash = hashlib.sha256(service.read_private(service.plist)).hexdigest()
        folder = private_directory(
            private_directory(ROOT / 'device-access/private') / 'camera-route-checks'
        )
        name = (
            'camera-routes-'
            + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
            + uuid.uuid4().hex[:8]
            + '.json'
        )
        with PrivateOutput(folder / name) as output:
            envelope = dict(
                backend_version=release.backend_version,
                release_id=release.release_id,
                expected_apk_version=release.assistant.version,
                expected_apk_code=release.assistant.version_code,
                runtime_instance=initial['instance'],
                original_stores_and_configuration_preserved=False,
                final_service_ownership_verified=False,
            )

            def request(payload):
                value = rpc(socket, payload)
                if payload == {'operation': 'health'}:
                    source_bound(value)
                    require(
                        value.get('runtime_instance') == initial['instance']
                        and value.get('runtime_process_id') == initial['pid'],
                        'camera_runtime_owner_changed',
                    )
                return value

            runner = CameraRouteRun(
                device,
                request,
                lambda instance, generation: CameraProbe(socket, instance, generation),
            )

            def checkpoint(value):
                # A passing case is not yet a passing delivery: the original
                # owner, private stores and audio counters still need auditing.
                output.write(dict(value, **envelope, checks_state=value['state'], state='running'))

            result = runner.run(selected, checkpoint=checkpoint)
            try:
                final = service.sample(include_activity=True)
                require(
                    final is not None
                    and final['pid'] == initial['pid']
                    and final['instance'] == initial['instance'],
                    'camera_final_service_owner_changed',
                )
                require(
                    all(
                        final[key] == initial[key]
                        for key in (
                            'mic_frames',
                            'render_frames',
                            'voice_asr_attempts',
                            'local_wake_count',
                            'device_mic_frames',
                            'device_auto_voice_starts',
                        )
                    ),
                    'camera_final_audio_state_changed',
                )
                require(
                    identities == [store_identity(path) for path in paths],
                    'camera_owner_private_store_changed',
                )
                require(
                    hashlib.sha256(owner_config.read_bytes()).hexdigest() == owner_hash
                    and hashlib.sha256(service.read_private(service.plist)).hexdigest()
                    == agent_hash,
                    'camera_owner_configuration_changed',
                )
                envelope.update(
                    original_stores_and_configuration_preserved=True,
                    final_service_ownership_verified=True,
                )
            except (Exception, KeyboardInterrupt) as error:
                result.update(
                    state='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                    final_preservation_reason='camera_final_preservation_unconfirmed',
                )
            output.write(dict(result, **envelope))
            return result['state'], output.path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--case', choices=CASES)
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Authorize up to two visible camera previews and logical route outages; no images exported',
    )
    args = parser.parse_args(argv)
    selected = (args.case,) if args.case else CASES
    if not args.execute:
        print(json.dumps(plan(selected), indent=2))
        return 0
    if sys.version_info < (3, 11):
        print(
            'Camera execution requires Python 3.11+ in the configured voice/dev environment.',
            file=sys.stderr,
        )
        return 2
    try:
        state, report = execute(args.runtime_root, selected)
    except KeyboardInterrupt:
        print(json.dumps(dict(state='interrupted', reason='camera_checks_interrupted')))
        return 130
    except (OSError, ValueError, RuntimeError):
        print(
            json.dumps(dict(state='unconfirmed', reason='camera_checks_stopped_no_automatic_retry'))
        )
        return 2
    print(json.dumps(dict(state=state, report=str(report), images_exported=False)))
    return 0 if state == 'passed' else 130 if state == 'interrupted' else 1


if __name__ == '__main__':
    raise SystemExit(main())
