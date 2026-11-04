"""Explicit managed-backend crash and sleep checks; default invocation only plans."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import uuid

from device_smoke import CheckFailure, Device, ROOT, require, rpc
from private_output import PrivateOutput, private_directory
from release_info import load_release
from usb_handover import read_usb


def process_rows(text, root, uid):
    """Match full script arguments and UID, never a process-name substring."""
    scripts = {
        'bridge': ['-u', str(root / 'cc8-assistant/host/bridge.py')],
        'supervisor': [str(root / 'cc8-assistant/host/service.py'), 'run'],
    }
    result = {'bridge': [], 'supervisor': [], 'unexpected': []}
    for line in text.splitlines():
        try:
            raw_pid, raw_uid, raw_parent, command = line.split(None, 3)
            pid, owner, parent = int(raw_pid), int(raw_uid), int(raw_parent)
            args = shlex.split(command)
        except ValueError:
            continue
        for kind, expected in scripts.items():
            if owner == uid and pid > 1 and args[1:] == expected:
                result[kind].append((pid, parent))
            elif owner == uid and pid > 1 and expected[-1 if kind == 'bridge' else 0] in args[1:3]:
                result['unexpected'].append((pid, parent))
    return result


class LiveService:
    def __init__(self, root, device):
        self.root, self.device = root, device
        self.private = root / 'device-access/private/voice-runtime'
        self.socket = self.private / 'announce.sock'
        self.release = load_release(root)
        label = 'com.happyhome.cc8.' + hashlib.sha256(str(root).encode()).hexdigest()[:12]
        self.label = label
        self.plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')

    def read_private(self, path):
        info = path.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and not info.st_mode & 0o077
            and info.st_size <= 16384,
            'unsafe_service_metadata',
        )
        return path.read_bytes()

    def processes(self):
        result = subprocess.run(
            ['/bin/ps', '-ax', '-o', 'pid=', '-o', 'uid=', '-o', 'ppid=', '-o', 'command='],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return process_rows(result.stdout, self.root, os.getuid())

    def launchd_pid(self):
        result = subprocess.run(
            ['/bin/launchctl', 'print', f'gui/{os.getuid()}/{self.label}'],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        require(len(result.stdout) <= 65536, 'invalid_launchd_receipt')
        pids = re.findall(r'^\s*pid = ([1-9][0-9]*)\s*$', result.stdout, re.MULTILINE)
        require(len(pids) == 1, 'launchd_pid_missing_or_ambiguous')
        return int(pids[0])

    def sample(self, *, include_activity=False):
        enabled = json.loads(self.read_private(self.private / 'service-enabled.json'))
        require(enabled.get('enabled') is True, 'owner_service_stopped')
        agent = plistlib.loads(self.read_private(self.plist))
        require(
            agent.get('WorkingDirectory') == str(self.root)
            and agent.get('ProgramArguments')
            == [
                str(self.root / '.toolchains/cc8/voice-venv/bin/python'),
                str(self.root / 'cc8-assistant/host/service.py'),
                'run',
            ],
            'service_agent_source_mismatch',
        )
        processes = self.processes()
        require(not processes['unexpected'], 'unexpected_service_process')
        require(len(processes['bridge']) <= 1, 'duplicate_backend')
        require(len(processes['supervisor']) == 1, 'supervisor_missing_or_ambiguous')
        d, ui = self.device.status()
        require(d.get('foreground') is True, 'assistant_left_foreground')
        require(d.get('microphone_active') is False, 'device_capture_active')
        require(
            d.get('vision_active') is False and ui.get('camera_open') is False,
            'device_camera_active',
        )
        try:
            health = rpc(self.socket, {'operation': 'health'})
        except OSError:
            return None
        require(health.get('ok') is True, 'invalid_health')
        require(
            health.get('bridge_version') == self.release.backend_version
            and type(health.get('protocol_version')) is int
            and health['protocol_version'] == self.release.protocol_version,
            'runtime_source_version_mismatch',
        )
        require(health.get('microphone_active') is False, 'backend_capture_active')
        require(
            health.get('playing') is False and health.get('calibrating') is False,
            'owner_audio_busy',
        )
        # Diagnostics are bounded, content-free receipts. Reject pending work;
        # a stopped microphone alone does not mean Dot has finished a task.
        diagnostics = rpc(self.socket, {'operation': 'diagnostics'})
        requests = diagnostics.get('requests')
        require(isinstance(requests, list), 'diagnostics_unavailable')
        require(all(r.get('outcome') is not None for r in requests), 'owner_request_pending')
        if len(processes['bridge']) != 1:
            return None
        pid, parent = processes['bridge'][0]
        supervisor_pid = processes['supervisor'][0][0]
        supervisor = json.loads(self.read_private(self.private / 'supervisor.json'))
        if parent != supervisor_pid:
            # service.py deliberately adopts a surviving backend after launchd
            # replaces a crashed supervisor. Never accept an arbitrary parent,
            # stale ready file or a supervisor outside this exact LaunchAgent.
            updated = supervisor.get('updated_at')
            require(
                parent == 1
                and type(updated) in (int, float)
                and math.isfinite(updated)
                and 0 <= time.time() - updated <= 6
                and self.launchd_pid() == supervisor_pid,
                'backend_not_supervisor_child',
            )
        ready = (
            all(
                health.get(k) is True
                for k in ('model_ready', 'recognition_worker', 'response_worker')
            )
            and health.get('connected') is True
            and health.get('transport') == 'usb'
            and supervisor.get('phase') == 'ready'
            and supervisor.get('pid') == pid
        )
        if not ready:
            return None
        instance = health.get('runtime_instance')
        require(isinstance(instance, str) and 0 < len(instance) <= 128, 'runtime_instance_missing')
        require(
            self.device.version()
            == (
                self.release.assistant.version,
                self.release.assistant.version_code,
            ),
            'device_source_version_mismatch',
        )
        snapshot = {'pid': pid, 'instance': instance, 'mic_frames': health.get('mic_frames')}
        if include_activity:
            require(
                health.get('release_id') == self.release.release_id,
                'resume_release_identity_changed',
            )
            if d.get('connected') is not True or d.get('transport') != 'usb':
                return None
            starts = ui.get('auto_voice_start_attempts')
            require(
                ui.get('auto_voice_pending') is False and type(starts) is int and starts >= 0,
                'resume_device_auto_voice_not_idle',
            )
            counters = ('mic_frames', 'render_frames', 'voice_asr_attempts', 'local_wake_count')
            require(
                all(type(health.get(key)) is int and health[key] >= 0 for key in counters),
                'resume_audio_counters_unavailable',
            )
            ids = [row.get('request_id') for row in requests]
            require(
                len(ids) <= 128
                and all(isinstance(ident, str) and ident for ident in ids)
                and len(set(ids)) == len(ids),
                'resume_request_window_unavailable',
            )
            require(
                type(d.get('mic_frames')) is int and d['mic_frames'] >= 0,
                'resume_device_mic_counter_unavailable',
            )
            snapshot.update(
                **{key: health[key] for key in counters if key != 'mic_frames'},
                device_mic_frames=d['mic_frames'],
                device_auto_voice_starts=starts,
                requests=frozenset(ids),
            )
        return snapshot

    def crash(self, previous):
        # Re-sample all idle/ownership gates immediately before signalling. A
        # changed instance or PID is an abort, not a new target to follow.
        current = self.sample()
        require(current == previous, 'runtime_changed_before_fault')
        processes = self.processes()
        require(
            not processes['unexpected']
            and len(processes['supervisor']) == 1
            and len(processes['bridge']) == 1
            and processes['bridge'][0][0] == previous['pid'],
            'process_changed_before_fault',
        )
        os.kill(previous['pid'], signal.SIGKILL)

    def supervisor_sample(self, *, ready_after=None, previous=None, include_activity=False):
        try:
            snapshot = self.sample(include_activity=True) if include_activity else self.sample()
        except CheckFailure as error:
            if (
                str(error) != 'backend_not_supervisor_child'
                or ready_after is None
                or previous is None
            ):
                raise
            # A throttled launchd replacement can exist before its first ready
            # write. Wait only for the exact surviving backend from our fault;
            # this is not a successful sample and never authorizes a signal.
            processes = self.processes()
            require(
                not processes['unexpected']
                and len(processes['supervisor']) == 1
                and processes['bridge'] == [(previous['pid'], 1)]
                and rpc(self.socket, {'operation': 'health'}).get('runtime_instance')
                == previous['instance'],
                'unexpected_adoption_target',
            )
            return None
        if snapshot is None:
            return None
        processes = self.processes()
        require(
            not processes['unexpected']
            and len(processes['supervisor']) == 1
            and len(processes['bridge']) == 1
            and processes['bridge'][0][0] == snapshot['pid'],
            'process_changed_during_sample',
        )
        supervisor_pid = processes['supervisor'][0][0]
        require(self.launchd_pid() == supervisor_pid, 'launchd_supervisor_mismatch')
        if ready_after is not None:
            state = json.loads(self.read_private(self.private / 'supervisor.json'))
            updated = state.get('updated_at')
            if (
                type(updated) not in (int, float)
                or not math.isfinite(updated)
                or updated <= ready_after
                or state.get('phase') != 'ready'
                or state.get('pid') != snapshot['pid']
            ):
                return None
        return {
            **snapshot,
            'supervisor_pid': supervisor_pid,
            'adopted_bridge': processes['bridge'][0][1] == 1,
        }

    def crash_supervisor(self, previous):
        require(self.supervisor_sample() == previous, 'runtime_changed_before_fault')
        os.kill(previous['supervisor_pid'], signal.SIGKILL)

    def resume_sample(self, expected_usb):
        before, _, _ = read_usb(3)
        if before is None:
            return None
        require(
            (before.serial, before.location) == expected_usb,
            'resume_usb_identity_changed',
        )
        try:
            device = Device()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # The same USB device can appear before its engineering shell.
            return None
        after, _, _ = read_usb(3)
        if before != after:
            return None
        self.device = device
        try:
            return self.supervisor_sample(include_activity=True)
        except CheckFailure as error:
            if str(error) != 'usb_target_changed':
                raise
            return None
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None


class RecoveryRun:
    fault = 'owned_backend_sigkill'

    def sample(self):
        return self.service.sample()

    def crash(self, previous):
        self.service.crash(previous)

    def recovered(self, current, previous):
        return current['instance'] != previous['instance'] and current['pid'] != previous['pid']

    def __init__(
        self,
        service,
        cycles,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
        checkpoint=lambda value: None,
    ):
        require(type(cycles) is int and 1 <= cycles <= 10, 'invalid_cycle_count')
        self.service, self.cycles = service, cycles
        self.clock, self.sleep, self.checkpoint = clock, sleep, checkpoint
        self.started = clock()
        self.rows = []
        self.fault_sent = False
        self.recovery_verified = False
        self.reason = None
        self.source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def report(self, state):
        return {
            'schema': 1,
            'state': state,
            'requested_cycles': self.cycles,
            'fault': self.fault,
            'automation_source_sha256': self.source_hash,
            'elapsed_seconds': round(self.clock() - self.started, 3),
            'checks': list(self.rows),
            'reason': self.reason,
            'recovery_verified': self.recovery_verified,
            'starts_capture': False,
            'opens_ui': False,
            'raw_audio_saved': False,
            'contains_content': False,
            'full_p0_acceptance': False,
        }

    def wait_ready(self, previous=None):
        deadline = self.clock() + 90
        while True:
            current = self.sample()
            if current and (previous is None or self.recovered(current, previous)):
                return current
            require(self.clock() < deadline, 'service_recovery_timeout')
            self.sleep(min(1, max(0, deadline - self.clock())))

    def run(self):
        state = 'failed'
        previous = None
        try:
            # Preflight must be ready now; never wait for or start an idle service.
            previous = self.sample()
            require(previous is not None, 'preflight_service_not_ready')
            for index in range(self.cycles):
                row = {'cycle': index + 1, 'state': 'running'}
                self.rows.append(row)
                self.checkpoint(self.report('running'))
                began = self.clock()
                self.crash(previous)
                self.fault_sent = True
                self.recovery_verified = False
                current = self.wait_ready(previous)
                self.sleep(3)
                stable = self.sample()
                require(stable == current, 'recovered_service_not_stable')
                row.update(state='passed', recovery_seconds=round(self.clock() - began, 3))
                if self.fault == 'owned_supervisor_sigkill':
                    row['backend_reused'] = current['pid'] == previous['pid']
                previous = current
                self.recovery_verified = True
                self.checkpoint(self.report('running'))
            state = 'passed'
        except KeyboardInterrupt:
            state = 'interrupted'
        except CheckFailure as error:
            self.reason = str(error)
            if self.rows and self.rows[-1]['state'] == 'running':
                self.rows[-1].update(state='failed', reason=str(error))
        except Exception:
            self.reason = 'service_test_operation_failed'
            if self.rows and self.rows[-1]['state'] == 'running':
                self.rows[-1].update(state='failed', reason='service_test_operation_failed')
        finally:
            if self.fault_sent and not self.recovery_verified:
                # Observe the existing supervisor only. Do not override a user
                # stop, install a service, replay work or remotely arm capture.
                try:
                    self.wait_ready(previous)
                    self.recovery_verified = True
                except Exception:
                    state = 'recovery_unconfirmed'
            self.checkpoint(self.report(state))
        return self.report(state)


class SupervisorRecoveryRun(RecoveryRun):
    fault = 'owned_supervisor_sigkill'

    def sample(self):
        try:
            if self.fault_sent:
                return self.service.supervisor_sample(
                    ready_after=self.fault_at, previous=self.fault_previous
                )
            return self.service.supervisor_sample()
        except CheckFailure as error:
            if self.fault_sent and str(error) == 'supervisor_missing_or_ambiguous':
                processes = self.service.processes()
                require(
                    not processes['unexpected']
                    and len(processes['supervisor']) <= 1
                    and len(processes['bridge']) <= 1,
                    'unexpected_recovery_processes',
                )
                # launchd may already have spawned the replacement between the
                # two ps snapshots. Re-sample every gate on the next poll; do
                # not mistake that allowed transition for a duplicate service.
                return None
            raise

    def crash(self, previous):
        self.fault_previous = dict(previous)
        self.service.crash_supervisor(previous)
        # Revalidation itself can cross a ready-write tick. Timestamp after the
        # signal so an old supervisor's last write cannot satisfy recovery.
        self.fault_at = time.time()

    def recovered(self, current, previous):
        return current['supervisor_pid'] != previous['supervisor_pid'] and (
            (current['pid'] == previous['pid'] and current['instance'] == previous['instance'])
            or super().recovered(current, previous)
        )


def mac_sleep_clocks():
    require(
        sys.platform == 'darwin' and hasattr(time, 'CLOCK_UPTIME_RAW'),
        'resume_mac_clocks_unavailable',
    )
    for _ in range(3):
        awake_before = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
        continuous = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
        awake_after = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
        if 0 <= awake_after - awake_before <= 10_000_000:
            # Bracket the offset with two awake-clock reads. Scheduler delays
            # cannot inflate the lower bound of a genuine sleep interval.
            return (
                continuous / 1e9,
                (continuous - awake_after) / 1e9,
                (continuous - awake_before) / 1e9,
            )
    raise CheckFailure('resume_mac_clock_sample_unstable')


class SleepRecoveryRun:
    def __init__(
        self,
        service,
        seconds,
        *,
        clock=mac_sleep_clocks,
        sleep=time.sleep,
        checkpoint=lambda value: None,
        usb_reader=read_usb,
    ):
        self.service, self.seconds = service, seconds
        self.clock, self.sleep, self.checkpoint = clock, sleep, checkpoint
        self.usb_reader = usb_reader

    def run(self):
        report = {
            'schema': 1,
            'scope': 'observed_mac_system_sleep',
            'state': 'running',
            'wait_seconds': self.seconds,
            'minimum_sleep_seconds': None,
            'post_resume_ready_observed_seconds': None,
            'unready_samples_after_resume': 0,
            'same_runtime_and_idle_audio_verified': False,
            'recovery_verified': False,
            'starts_capture': False,
            'opens_ui': False,
            'full_p0_acceptance': False,
            'automation_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        self.checkpoint(report)
        try:
            _, old_low, old_high = self.clock()
            usb, _, _ = self.usb_reader(3)
            require(usb is not None, 'resume_usb_baseline_missing')
            identity = (usb.serial, usb.location)
            baseline = self.service.supervisor_sample(include_activity=True)
            require(baseline is not None, 'preflight_service_not_ready')
            require(self.usb_reader(3)[0] == usb, 'resume_usb_baseline_changed')
            now, low, high = self.clock()
            require(
                low <= old_high + 0.01 and high >= old_low - 0.01, 'resume_sleep_crossed_baseline'
            )
            deadline = now + self.seconds
            print('正在只读等待 Mac 睡眠和唤醒。', flush=True)
            while True:
                now, low, high = self.clock()
                require(now < deadline, 'resume_sleep_not_observed')
                if low - old_high >= 5:
                    report['minimum_sleep_seconds'] = round(low - old_high, 3)
                    self.checkpoint(report)
                    break
                self.sleep(min(1, deadline - now))

            resumed, sleep_high = now, high
            ready_deadline = resumed + 90
            while True:
                ready = self.service.resume_sample(identity)
                now, low, _ = self.clock()
                require(low <= sleep_high + 0.01, 'resume_additional_sleep_observed')
                require(now <= ready_deadline, 'resume_service_not_ready')
                if ready is not None:
                    require(ready == baseline, 'resume_runtime_or_activity_changed')
                    break
                report['unready_samples_after_resume'] += 1
                self.checkpoint(report)
                self.sleep(min(1, ready_deadline - now))
            report['post_resume_ready_observed_seconds'] = round(now - resumed, 3)
            self.sleep(3)
            require(self.service.resume_sample(identity) == baseline, 'resume_service_not_stable')
            now, low, _ = self.clock()
            require(low <= sleep_high + 0.01, 'resume_additional_sleep_observed')
            require(now <= ready_deadline, 'resume_service_not_ready')
            report.update(
                state='passed', same_runtime_and_idle_audio_verified=True, recovery_verified=True
            )
        except KeyboardInterrupt:
            report['state'] = 'interrupted'
        except CheckFailure as error:
            report.update(state='failed', reason=str(error))
        except Exception:
            report.update(state='failed', reason='resume_test_operation_failed')
        self.checkpoint(report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--cycles', type=int, choices=range(1, 11))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fault', choices=('backend', 'supervisor'), default='backend')
    mode.add_argument('--observe-sleep', action='store_true')
    parser.add_argument('--sleep-wait', type=int, metavar='SECONDS')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if args.observe_sleep and args.cycles is not None:
        parser.error('--observe-sleep waits for one transition and does not use --cycles')
    if args.sleep_wait is not None and (
        not args.observe_sleep or not 10 <= args.sleep_wait <= 3600
    ):
        parser.error('--sleep-wait requires --observe-sleep and accepts 10–3600 seconds')
    cycles = args.cycles if args.cycles is not None else 10
    sleep_wait = args.sleep_wait if args.sleep_wait is not None else 900
    if not args.execute:
        print(
            json.dumps(
                {
                    'execute': False,
                    **(
                        {
                            'scope': 'observed_mac_system_sleep',
                            'wait_seconds': sleep_wait,
                            'forces_sleep': False,
                        }
                        if args.observe_sleep
                        else {'cycles': cycles, 'fault': 'owned_' + args.fault + '_sigkill'}
                    ),
                    'starts_capture': False,
                    'opens_ui': False,
                    'full_p0_acceptance': False,
                }
            )
        )
        return 0
    try:
        if args.observe_sleep:
            mac_sleep_clocks()
        require(
            args.runtime_root.is_absolute() and args.runtime_root.resolve() == args.runtime_root,
            'runtime_root_must_be_real_absolute',
        )
        private = private_directory(args.runtime_root / 'device-access/private/voice-runtime')
        folder = private_directory(
            private_directory(ROOT / 'device-access/private') / 'service-checks'
        )
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
                and not info.st_mode & 0o077
                and info.st_nlink == 1,
                'unsafe_test_lock',
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            name = (
                'service-'
                + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
                + uuid.uuid4().hex[:8]
                + '.json'
            )
            with PrivateOutput(folder / name) as output:
                service = LiveService(args.runtime_root, Device())

                def save(value):
                    output.write(
                        {
                            **value,
                            'backend_version': service.release.backend_version,
                            'expected_apk_version': service.release.assistant.version,
                            'expected_apk_code': service.release.assistant.version_code,
                        }
                    )

                if args.observe_sleep:
                    result = SleepRecoveryRun(service, sleep_wait, checkpoint=save).run()
                    summary = {
                        key: result[key]
                        for key in ('state', 'minimum_sleep_seconds', 'recovery_verified')
                    }
                else:
                    runner = SupervisorRecoveryRun if args.fault == 'supervisor' else RecoveryRun
                    result = runner(service, cycles, checkpoint=save).run()
                    summary = {
                        'state': result['state'],
                        'cycles': len(result['checks']),
                        'recovery_verified': result['recovery_verified'],
                    }
                print(json.dumps(summary))
                print(output.path)
                return 0 if result['state'] == 'passed' else 2
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print('后台恢复检查未完成，未开启麦克风或摄像头。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
