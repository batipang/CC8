"""Isolated host fault checks and optional CC8 QA error UI replay."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import stat
import sys
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))

if __package__:
    from .qa_variant import PACKAGE, COMPONENT, PORT, apk_metadata, safe_source, snapshot_files
    from .qa_faults import (
        CASE_SETS,
        DOT_FIXTURES,
        HTTP_FIXTURES,
        OFFLINE_OUTPUT_CASE_SETS,
        generate,
    )
    from .qa_ui import stdout_hierarchy, status_target, dialog_close
    from .private_output import private_directory, PrivateOutput
else:
    # Root entry imports this as tools.check_failure_ui to keep one module identity.
    from tools.qa_variant import PACKAGE, COMPONENT, PORT, apk_metadata, safe_source, snapshot_files
    from tools.qa_faults import (
        CASE_SETS,
        DOT_FIXTURES,
        HTTP_FIXTURES,
        OFFLINE_OUTPUT_CASE_SETS,
        generate,
    )
    from tools.qa_ui import stdout_hierarchy, status_target, dialog_close
    from tools.private_output import private_directory, PrivateOutput


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def private_bytes(path, maximum):
    require(path.is_absolute() and path.resolve() == path, 'unsafe_qa_receipt_path')
    private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and not info.st_mode & 0o077
            and info.st_nlink == 1
            and 0 < info.st_size <= maximum,
            'unsafe_qa_receipt',
        )
        data = source.read(maximum + 1)
        require(len(data) == info.st_size, 'qa_receipt_changed')
    return data


def private_json(path):
    data = private_bytes(path, 2 * 1024**2)
    value = json.loads(data)
    require(type(value) is dict, 'invalid_qa_receipt')
    return value


def source_identity(http_fixture='fake', dot_fixture='fake'):
    names = ['check_failure_ui.py', 'qa_faults.py', 'qa_variant.py', 'qa_ui.py']
    if http_fixture != 'fake':
        names.append('qa_http.py')
    if dot_fixture == 'local_stdio':
        names.append('qa_dot.py')
    return {name: hashlib.sha256(safe_source(ROOT, 'tools/' + name)).hexdigest() for name in names}


def corpus_identity(fixtures):
    digest = hashlib.sha256()
    for fixture in fixtures:
        # Include fixed labels, expectations and actual framed protocol bytes.
        digest.update(fixture['case'].ident.encode())
        digest.update(fixture['expected_message'].encode())
        digest.update(fixture.get('http_fixture', 'fake').encode())
        digest.update(str(fixture.get('wire_requests', 0)).encode())
        if fixture.get('http_fixture', 'fake') != 'fake':
            digest.update(str(fixture['recovery_wire_requests']).encode())
        if fixture.get('dot_fixture') == 'local_stdio':
            digest.update(b'local_stdio')
            digest.update(json.dumps(fixture['dot_wire_counts'], sort_keys=True).encode())
        for packet in [*fixture['error'], fixture['stale_error'], *fixture['recovery']]:
            digest.update(packet.encode())
    return digest.hexdigest()


def fixture_summary(fixtures, http_fixture, dot_fixture):
    return {
        'automation_sha256': source_identity(http_fixture, dot_fixture),
        'fixture_sha256': corpus_identity(fixtures),
        'provider_http_fixture': http_fixture,
        'provider_dot_fixture': dot_fixture,
        'generated_wire_requests': sum(f.get('wire_requests', 0) for f in fixtures),
        'generated_recovery_wire_requests': sum(
            f.get('recovery_wire_requests', 0) for f in fixtures
        ),
        'generated_dot_wire_counts': (
            {
                key: sum(f['dot_wire_counts'][key] for f in fixtures)
                for key in (
                    'fault_turns',
                    'recovery_turns',
                    'declined_approvals',
                    'withheld_start_acks',
                )
            }
            if dot_fixture == 'local_stdio'
            else None
        ),
    }


class Device:
    def __init__(self):
        from device_smoke import Device as OriginalDevice

        self.original = OriginalDevice()
        self.access = self.original.access
        self.transport = self.original.transport
        self.hierarchy_diagnostic = None

    def shell(self, command):
        return self.original.shell(command)

    def status(self):
        from device_smoke import objects

        text = self.shell('dumpsys activity ' + COMPONENT)
        return objects(text, 'CC8_ASSISTANT_STATUS '), objects(text, 'HUD_UI_STATUS ')

    def safe(self):
        d, u = self.status()
        require(
            d.get('foreground') is True
            and d.get('microphone_active') is False
            and d.get('mic_frames') == 0
            and u.get('camera_open') is False
            and u.get('camera_captures') == 0
            and u.get('auto_voice_start_attempts') == 0,
            'qa_capture_or_focus_changed',
        )
        return d, u

    def xml(self):
        self.safe()
        # stdout belongs to this bounded engineering-shell call. Avoid both
        # protected-directory denials and persisting a hierarchy on shared storage.
        raw = self.shell('uiautomator dump --compressed /proc/self/fd/1 | toybox head -c 262145')
        self.safe()
        self.hierarchy_diagnostic = {
            'bytes': len(raw.encode()),
            'xml_declaration': '<?xml' in raw,
            'root_begin': '<hierarchy' in raw,
            'root_end': '</hierarchy>' in raw,
            'idle_error': 'could not get idle state' in raw,
            'write_error': 'Error while dumping' in raw,
        }
        return stdout_hierarchy(raw)

    def tap(self, where):
        self.safe()
        x, y = where
        require(
            type(x) is int and type(y) is int and 0 <= x <= 8192 and 0 <= y <= 8192, 'invalid_tap'
        )
        self.shell(f'input tap {x} {y}')


class Link:
    def __init__(self, device):
        from usb_transport import USBPipe
        from protocol import receive, decode_event, EVENT

        self.pipe = USBPipe(
            [
                str(device.access.ADB),
                '-t',
                device.transport,
                'shell',
                '-T',
                device.access.PREFIX + f'toybox nc -w 5 127.0.0.1 {PORT}',
            ]
        )
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ready = queue.Queue(4)
        self.error = None
        self.unexpected = []

        def read():
            try:
                while not self.stop.is_set():
                    packet = receive(self.pipe)
                    require(packet.kind == EVENT, 'qa_unexpected_audio_packet')
                    event = decode_event(packet.data)
                    name = event.get('event')
                    if name in ('transport_hello', 'device_ready'):
                        self.ready.put_nowait(event)
                    elif name not in ('pong', 'session_stop', 'workspace_get'):
                        self.unexpected[:] = [
                            name
                            if name
                            in ('session_start', 'text_request', 'vision_request', 'settings')
                            else 'unknown'
                        ]
            except Exception:
                if not self.stop.is_set():
                    self.error = 'qa_link_receive_failed'

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            hello = self.ready.get(timeout=5)
            require(
                hello.get('event') == 'transport_hello' and hello.get('protocol') == 2,
                'qa_handshake_failed',
            )
            self.event('transport_activate')
            ready = self.ready.get(timeout=5)
            require(
                ready.get('event') == 'device_ready'
                and ready.get('capture_active') is False
                and ready.get('protocol') == 2,
                'qa_device_not_idle',
            )
            self.event('hello', message='隔离自动故障测试 · 不采集音视频', asr_engine='minimax')
            self.event('brain_provider', provider='dot')
        except BaseException:
            self.close()
            raise

        def heartbeat():
            while not self.stop.wait(1):
                try:
                    self.event('ping')
                except Exception:
                    self.error = 'qa_link_send_failed'
                    return

        self.timer = threading.Thread(target=heartbeat, daemon=True)
        self.timer.start()

    def event(self, name, **fields):
        from protocol import Packet

        self.send(Packet.event(name, **fields))

    def send(self, packet):
        require(self.error is None and not self.unexpected, 'qa_link_or_user_action_changed')
        with self.lock:
            self.pipe.sendall(packet.encode())

    def close(self):
        self.stop.set()
        self.pipe.close()
        self.reader.join(3)
        if hasattr(self, 'timer'):
            self.timer.join(3)


def connect(device):
    # A fresh install may not yet have bound its loopback listener. No request
    # packets are sent here; each failed handshake closes its own child/reader.
    for attempt in range(3):
        device.safe()
        try:
            return Link(device)
        except queue.Empty:
            if attempt == 2:
                raise RuntimeError('qa_listener_not_ready') from None
            time.sleep(0.3)


def wait_phase(device, link, phase, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        require(link.error is None and not link.unexpected, 'qa_unexpected_device_action')
        d, u = device.safe()
        if u.get('ui_phase') == phase:
            return d, u
        time.sleep(0.1)
    raise RuntimeError('qa_ui_phase_timeout')


def owned_package(device, digest):
    """Even a failed installer may have committed; only remove our exact new APK."""
    import shlex

    paths = device.access.package_paths(device.transport, PACKAGE)
    if not paths:
        require(not device.shell('pm list packages ' + PACKAGE).strip(), 'qa_absence_unconfirmed')
        return False
    require(len(paths) == 1, 'qa_cleanup_package_changed')
    remote = device.shell('sha256sum ' + shlex.quote(paths[0])).split()
    require(remote and remote[0] == digest, 'qa_cleanup_apk_changed')
    return True


def restore(runtime, device, baseline):
    from service_smoke import LiveService
    from device_smoke import COMPONENT as ORIGINAL_COMPONENT

    device.shell(
        'am start --activity-clear-top -n ' + ORIGINAL_COMPONENT + ' --ez auto_start_voice false'
    )
    end = time.monotonic() + 10
    while time.monotonic() < end:
        try:
            restored = LiveService(runtime, device.original).sample()
        except (RuntimeError, OSError):
            restored = None
        if restored is not None:
            require(
                restored['pid'] == baseline['pid'] and restored['instance'] == baseline['instance'],
                'production_instance_changed',
            )
            return
        time.sleep(0.2)
    raise RuntimeError('production_restore_timeout')


def run(
    runtime,
    receipt_path,
    limit,
    output,
    *,
    shared_staging=False,
    http_fixture='fake',
    dot_fixture='fake',
    case_set='standard',
):
    require(http_fixture in HTTP_FIXTURES, 'unknown_qa_http_fixture')
    require(dot_fixture in DOT_FIXTURES, 'unknown_qa_dot_fixture')
    require(
        case_set in CASE_SETS and type(limit) is int and 1 <= limit <= len(CASE_SETS[case_set]),
        'invalid_qa_selection',
    )
    require(
        case_set != 'search' or http_fixture != 'fake' and dot_fixture == 'fake',
        'search_qa_requires_loopback_without_dot',
    )
    require(
        case_set not in OFFLINE_OUTPUT_CASE_SETS or http_fixture == dot_fixture == 'fake',
        'playback_qa_requires_offline_fixtures',
    )
    cases = CASE_SETS[case_set]
    from service_smoke import LiveService
    from protocol import decode_event

    receipt = private_json(receipt_path)
    require(
        receipt.get('schema') == 1
        and receipt.get('kind') == 'cc8_isolated_qa_apk'
        and receipt.get('component') == COMPONENT
        and receipt.get('device_port') == PORT
        and receipt.get('package') == PACKAGE
        and receipt.get('capture_permissions_absent') is True
        and receipt.get('shared_uid_absent') is True
        and set(receipt.get('input_sha256', {})) == set(snapshot_files()),
        'invalid_qa_build',
    )
    for name, digest in receipt['input_sha256'].items():
        require(hashlib.sha256(safe_source(ROOT, name)).hexdigest() == digest, 'qa_source_changed')
    apk = receipt_path.parent / 'cc8-assistant-qa.apk'
    digest = hashlib.sha256(private_bytes(apk, 128 * 1024**2)).hexdigest()
    require(digest == receipt['apk_sha256'], 'qa_apk_changed')
    device = Device()
    # Verify the ignored local installer bundle before fixture generation or
    # any attempted installation, including its tracked Java source identity.
    device.access.helper_artifact()
    metadata = apk_metadata(apk, device.access.AAPT)
    require(
        metadata['version_code'] == receipt['version_code']
        and metadata['version'] == receipt['version'],
        'qa_version_changed',
    )
    require(not device.access.package_paths(device.transport, PACKAGE), 'qa_package_already_exists')
    require(not device.shell('pm list packages ' + PACKAGE).strip(), 'qa_absence_unconfirmed')
    service = LiveService(runtime, device.original)
    baseline = service.sample()
    require(baseline is not None, 'production_not_idle_ready')
    fixtures = generate(http_fixture=http_fixture, dot_fixture=dot_fixture, cases=cases[:limit])
    summary = fixture_summary(fixtures, http_fixture, dot_fixture)
    rows = []
    link = None
    touched = False
    ui_touched = False
    cleanup = False
    state = 'running'
    phase = 'signer_preflight'
    began = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    failure = None
    owner_rechecks = []

    def save():
        output.write(
            {
                'schema': 1,
                'state': state,
                'scope': 'physical_cc8_qa_ui_replay_from_real_controller_fixtures',
                'source_commit': receipt['source_commit'],
                'source_dirty': receipt['source_dirty'],
                **summary,
                'phase': phase,
                'owner_rechecks': list(owner_rechecks),
                'failure': failure,
                'hierarchy_diagnostic': device.hierarchy_diagnostic,
                'started_at': started_at,
                'elapsed_seconds': round(time.monotonic() - began, 3),
                'planned_production_disconnect': ui_touched,
                'qa_apk_sha256': digest,
                'installer_staging': 'shared_compatibility'
                if shared_staging
                else 'protected_shell_directory',
                'selected_cases': limit,
                'total_fixed_cases': len(cases),
                'case_set': case_set,
                'microphone_used': False,
                'camera_used': False,
                'capture_permissions_absent': True,
                'real_provider_requested': False,
                'dot_provider_is_fixture': True,
                'provider_calls_before_ui_replay': True,
                'audio_playback_asserted': False,
                'physical_usb_handover_tested': False,
                'production_apk_changed': False,
                'production_permissions_changed': False,
                'production_backend_restarted': False,
                'full_p0_acceptance': False,
                'cleanup_verified': cleanup,
                'checks': rows,
            }
        )

    def recheck_owner(stage):
        nonlocal phase
        phase = stage
        save()
        # Fixture generation, signer inspection and installation may each take
        # seconds. Never take the UI from an owner who resumed using the device,
        # or silently retarget a replacement production backend.
        require(service.sample() == baseline, 'production_state_changed')
        owner_rechecks.append(stage)
        save()

    save()
    try:
        recheck_owner('owner_before_signer')
        # Cross-package signer comparison is intentional: the fresh QA package
        # must use the already-installed assistant's verified original signer.
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            device.access.signature_preflight(
                device.transport, 'com.happyhome.cc8assistant', apk, Path(temporary)
            )
        recheck_owner('owner_before_install')
        touched = True
        phase = 'install_qa'
        save()
        report = device.access.install(device.transport, apk, False, shared_staging=shared_staging)
        require(
            report.get('installed') is True and report.get('apk_sha256') == digest,
            'qa_install_unconfirmed',
        )
        require(owned_package(device, digest), 'qa_install_unconfirmed')
        recheck_owner('owner_before_ui')
        phase = 'open_qa'
        ui_touched = True
        save()
        device.original.home()
        device.shell('input keyevent 224')
        device.shell(
            'am start --activity-clear-top -n ' + COMPONENT + ' --ez auto_start_voice false'
        )
        time.sleep(0.7)
        link = connect(device)
        for fixture in fixtures:
            phase = 'error_summary'
            row = {'case': fixture['case'].ident, 'state': 'running'}
            rows.append(row)
            save()
            for packet in fixture['error']:
                link.send(packet)
            _, ui = wait_phase(device, link, 'error')
            if fixture['expected_task'] is not None:
                require(
                    ui.get('latest_task_status') == fixture['expected_task'],
                    'qa_task_outcome_mismatch',
                )
            device.tap(status_target(device.xml(), fixture['expected_message']))
            phase = 'error_dialog'
            save()
            device.tap(dialog_close(device.xml(), fixture['expected_message']))
            row['error_and_full_guidance_seen'] = True
            phase = 'recovery_and_stale_error'
            recovery = fixture['recovery']
            split = next(
                i + 1
                for i, p in enumerate(recovery)
                if (e := decode_event(p.data)).get('event') == 'status'
                and e.get('phase') == 'thinking'
            )
            for packet in recovery[:split]:
                link.send(packet)
            wait_phase(device, link, 'thinking')
            link.send(fixture['stale_error'])
            time.sleep(0.15)
            require(
                device.safe()[1].get('ui_phase') == 'thinking', 'qa_stale_error_overwrote_recovery'
            )
            for packet in recovery[split:]:
                link.send(packet)
            _, ui = wait_phase(device, link, 'idle')
            require(ui.get('latest_task_status') == 'completed', 'qa_recovery_not_completed')
            row.update(state='passed', stale_error_ignored=True, recovery_ui_completed=True)
            save()
        state = 'passed'
    except KeyboardInterrupt:
        state = 'cancelled'
        raise
    except Exception as error:
        state = 'failed'
        site = traceback.extract_tb(error.__traceback__)[-1]
        failure = {
            'type': type(error).__name__,
            'file': Path(site.filename).name,
            'line': site.lineno,
        }
        raise
    finally:
        if link is not None:
            link.close()
        if touched:
            removed = restored = False
            try:
                if owned_package(device, digest):
                    # Delete only the new, still-identical QA application.
                    device.shell('am force-stop ' + PACKAGE)
                    result = device.shell('cmd package uninstall ' + PACKAGE)
                    require(
                        'Success' in result
                        and not device.access.package_paths(device.transport, PACKAGE),
                        'qa_uninstall_unconfirmed',
                    )
                removed = True
            except Exception:
                state = 'cleanup_unconfirmed'
            # A blocked uninstall must not leave the original assistant at Home.
            # This restoration never enables capture or restarts its backend.
            try:
                if ui_touched:
                    restore(runtime, device, baseline)
                restored = True
            except Exception:
                state = 'cleanup_unconfirmed'
            cleanup = removed and restored
        save()
    require(cleanup, 'qa_cleanup_unconfirmed')
    return state


def host_check(args):
    cases = CASE_SETS[args.case_set]
    result = {
        'execute': args.execute,
        'scope': 'host_fault_recovery',
        'selected_cases': args.limit,
        'total_fixed_cases': len(cases),
        'case_set': args.case_set,
        'physical_ui_tested': False,
        'real_provider_requested': False,
        'provider_http_fixture': args.http_fixture,
        'provider_dot_fixture': args.dot_fixture,
    }
    if args.execute:
        try:
            fixtures = generate(
                http_fixture=args.http_fixture,
                dot_fixture=args.dot_fixture,
                cases=cases[: args.limit],
            )
            result.update(fixture_summary(fixtures, args.http_fixture, args.dot_fixture))
            result['state'] = 'passed'
        except KeyboardInterrupt:
            return 130
        except Exception as error:
            result.update(state='failed', error_type=type(error).__name__)
    print(json.dumps(result))
    return 2 if result.get('state') == 'failed' else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path)
    parser.add_argument(
        '--host-only', action='store_true', help='Check fault/recovery fixtures without a device'
    )
    parser.add_argument('--qa-receipt', type=Path)
    parser.add_argument('--case-set', choices=tuple(CASE_SETS), default='standard')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument(
        '--http-fixture',
        choices=HTTP_FIXTURES,
        help='Offline fake or certificate-verified loopback TLS with HTTP or business faults',
    )
    parser.add_argument(
        '--dot-fixture',
        choices=DOT_FIXTURES,
        help='Fixed Dot faults through an offline fake or a local JSON-RPC child',
    )
    parser.add_argument(
        '--shared-staging',
        action='store_true',
        help='Explicit existing shared-directory installer compatibility route; see docs/APK_INSTALL.md',
    )
    args = parser.parse_args(argv)
    total = len(CASE_SETS[args.case_set])
    if args.limit is None:
        args.limit = total
    if not 1 <= args.limit <= total:
        parser.error(f'--limit must be between 1 and {total}')
    if args.case_set == 'search':
        args.http_fixture = args.http_fixture or 'loopback_business'
        args.dot_fixture = args.dot_fixture or 'fake'
        if args.http_fixture == 'fake' or args.dot_fixture != 'fake':
            parser.error('Search QA requires loopback TLS and no Dot subprocess')
    if args.case_set in OFFLINE_OUTPUT_CASE_SETS:
        args.http_fixture = args.http_fixture or 'fake'
        args.dot_fixture = args.dot_fixture or 'fake'
        if args.http_fixture != 'fake' or args.dot_fixture != 'fake':
            parser.error('Output QA requires offline fixtures')
    if args.host_only:
        if args.runtime_root is not None or args.qa_receipt is not None or args.shared_staging:
            parser.error('--host-only cannot be combined with device or installer options')
        args.http_fixture = args.http_fixture or 'loopback_business'
        args.dot_fixture = args.dot_fixture or 'local_stdio'
        return host_check(args)
    if args.runtime_root is None:
        parser.error('--runtime-root is required for device checks')
    args.http_fixture = args.http_fixture or 'fake'
    args.dot_fixture = args.dot_fixture or 'fake'
    if not args.execute:
        print(
            json.dumps(
                {
                    'execute': False,
                    'fixed_cases': total,
                    'case_set': args.case_set,
                    'qa_package': PACKAGE,
                    'microphone_used': False,
                    'camera_used': False,
                    'real_provider_requested': False,
                    'provider_http_fixture': args.http_fixture,
                    'provider_dot_fixture': args.dot_fixture,
                    'production_apk_changed': False,
                }
            )
        )
        return 0
    try:
        require(args.qa_receipt is not None, 'invalid_qa_selection')
        require(
            args.runtime_root.is_absolute() and args.runtime_root.resolve() == args.runtime_root,
            'invalid_runtime_root',
        )
        folder = private_directory(
            private_directory(ROOT / 'device-access/private') / 'ui-failure-checks'
        )
        lock_path = args.runtime_root / 'device-access/private/voice-runtime/automation-test.lock'
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            info = os.fstat(lock.fileno())
            require(
                info.st_uid == os.getuid()
                and not info.st_mode & 0o077
                and info.st_nlink == 1
                and stat.S_ISREG(info.st_mode),
                'unsafe_qa_lock',
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            name = (
                'qa-ui-'
                + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
                + uuid.uuid4().hex[:8]
                + '.json'
            )
            with PrivateOutput(folder / name) as output:
                output.write({'state': 'preparing', 'full_p0_acceptance': False})
                try:
                    state = run(
                        args.runtime_root,
                        args.qa_receipt,
                        args.limit,
                        output,
                        shared_staging=args.shared_staging,
                        http_fixture=args.http_fixture,
                        dot_fixture=args.dot_fixture,
                        case_set=args.case_set,
                    )
                except BaseException:
                    if private_json(output.path).get('state') == 'preparing':
                        output.write({'state': 'preflight_failed', 'full_p0_acceptance': False})
                    print(output.path)
                    raise
                print(json.dumps({'state': state, 'selected_cases': args.limit}))
                print(output.path)
                return 0 if state == 'passed' else 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('隔离实机UI检查未完成；请检查私有报告和恢复状态。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
