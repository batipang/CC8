"""Observe real USB-bus edges and the idle CC8 transport using read-only probes."""

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time

from diagnose import query
from private_output import PrivateOutput

CC8_MODEL = 'AILABS_S6'
CC8_USB_IDS = (0x0E8D, 0x201C)
INTERVAL = 0.5
MAX_POLL_GAP = 5
ROUTE_WAIT = 30


class CheckFailure(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise CheckFailure(reason)


def positive_id(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class USBDevice:
    serial: str
    location: int
    entry: int
    ancestors: tuple[int, ...]


def topology(data):
    """Project only device identity and topology in memory, never in reports."""
    require(0 < len(data) <= 2 * 1024**2, 'usb_registry_invalid')
    root = plistlib.loads(data)
    nodes = [(node, ()) for node in (root if isinstance(root, list) else [root])]
    entries = set()
    devices = []
    locations = {}
    while nodes:
        node, ancestors = nodes.pop()
        require(isinstance(node, dict), 'usb_registry_invalid')
        entry = node.get('IORegistryEntryID')
        require(positive_id(entry) and entry not in entries, 'usb_registry_invalid')
        entries.add(entry)
        children = node.get('IORegistryEntryChildren', [])
        require(isinstance(children, list), 'usb_registry_invalid')
        nodes.extend((child, (*ancestors, entry)) for child in children)
        location = node.get('locationID')
        if 'idVendor' in node and positive_id(location):
            locations.setdefault(location, set()).add(entry)
        if node.get('USB Product Name') == CC8_MODEL:
            require(
                node.get('IOObjectClass') == 'IOUSBHostDevice'
                and (node.get('idVendor'), node.get('idProduct')) == CC8_USB_IDS
                and type(node.get('USB Serial Number')) is str
                and 0 < len(node['USB Serial Number']) <= 256
                and positive_id(location)
                and ancestors,
                'usb_device_identity_unconfirmed',
            )
            devices.append(USBDevice(node['USB Serial Number'], location, entry, ancestors))
    require(entries and len(devices) <= 1, 'usb_device_identity_ambiguous')
    return (devices[0] if devices else None), entries, locations


def read_usb(timeout):
    try:
        result = subprocess.run(
            ['/usr/sbin/ioreg', '-a', '-p', 'IOUSB', '-l'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
        return topology(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, OverflowError):
        raise CheckFailure('usb_registry_unavailable') from None


class Handover:
    def __init__(self):
        self.identity = None
        self.last_entry = None
        self.seen_entries = set()
        self.runtime = None
        self.frames = None
        self.rows = []
        self.current = None

    def validate_runtime(self, health, transport):
        runtime = (health.get('runtime_instance'), health.get('bridge_version'))
        require(
            all(type(value) is str and 0 < len(value) <= 128 for value in runtime)
            and re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', runtime[1]),
            'runtime_identity_unconfirmed',
        )
        require(health.get('model_ready') is True, 'runtime_not_ready')
        require(
            all(
                health.get(key) is False for key in ('microphone_active', 'calibrating', 'playing')
            ),
            'runtime_audio_not_idle',
        )
        require(transport.get('usb_test_suppressed') is False, 'usb_software_suppression')
        mic_frames = health.get('mic_frames')
        require(type(mic_frames) is int and mic_frames >= 0, 'mic_counter_unavailable')
        if self.runtime is not None:
            require(runtime == self.runtime, 'runtime_changed')
            require(mic_frames == self.frames, 'microphone_frames_changed')
        return runtime, mic_frames

    def sample(self, usb, health, transport, elapsed):
        device, entries, locations = usb
        runtime, mic_frames = self.validate_runtime(health, transport)
        route = None
        selected = health.get('transport')
        if (
            health.get('connected') is True
            and selected in ('usb', 'wifi')
            and selected == transport.get('transport')
            and transport.get(selected + '_ready') is True
        ):
            route = selected

        if self.identity is None:
            require(
                device is not None
                and locations.get(device.location) == {device.entry}
                and route == 'usb'
                and transport.get('wifi_ready') is True,
                'baseline_requires_cc8_on_usb_with_paired_wifi',
            )
            self.identity = device
            self.last_entry = device.entry
            self.seen_entries.add(device.entry)
            self.runtime, self.frames = runtime, mic_frames
            return False

        identity = self.identity
        require(set(identity.ancestors) <= entries, 'usb_upstream_topology_changed')
        if device is None:
            require(not locations.get(identity.location), 'usb_port_has_other_device')
        else:
            require(
                (device.serial, device.location, device.ancestors)
                == (identity.serial, identity.location, identity.ancestors)
                and locations.get(identity.location) == {device.entry},
                'usb_device_or_port_changed',
            )

        row = self.current
        if row is None:
            if device is not None:
                require(device.entry == self.last_entry, 'usb_detach_not_observed')
                return False
            self.current = row = {'usb_absent_s': round(elapsed, 3)}
        if 'usb_returned_s' in row:
            require(device is not None and device.entry == self.last_entry, 'usb_return_unstable')
            require(elapsed - row['usb_returned_s'] <= ROUTE_WAIT, 'usb_priority_not_restored')
        elif device is None:
            if 'wifi_selected_s' not in row:
                require(elapsed - row['usb_absent_s'] <= ROUTE_WAIT, 'wifi_takeover_not_observed')
                if route == 'wifi' and transport.get('usb_ready') is False:
                    row['wifi_selected_s'] = round(elapsed, 3)
            else:
                require(
                    route == 'wifi' and transport.get('usb_ready') is False,
                    'wifi_route_lost_while_usb_absent',
                )
        else:
            require('wifi_selected_s' in row, 'usb_returned_before_wifi_observed')
            require(device.entry not in self.seen_entries, 'usb_new_registration_not_observed')
            self.last_entry = device.entry
            self.seen_entries.add(device.entry)
            row['usb_returned_s'] = round(elapsed, 3)
        if route == 'usb' and 'usb_returned_s' in row:
            row['usb_selected_s'] = round(elapsed, 3)
            self.rows.append(row)
            self.current = None
            return True
        return False


def run(
    args,
    *,
    read_bus=read_usb,
    request=query,
    clock=time.monotonic,
    wall=time.time,
    sleep=time.sleep,
):
    started = clock()
    deadline = started + args.seconds
    state = Handover()
    socket = args.runtime_root / 'device-access/private/voice-runtime/announce.sock'
    previous = (started, wall())
    polls, unstable_polls = 0, 0
    outcome, reason = 'running', None
    stamp = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with PrivateOutput(args.output) as output:

        def save():
            output.write(
                {
                    'schema': 1,
                    'source_sha256': stamp,
                    'scope': 'idle_usb_bus_reenumeration_and_route',
                    'state': outcome,
                    'reason': reason,
                    'requested_cycles': args.cycles,
                    'requested_seconds': args.seconds,
                    'elapsed_seconds': round(clock() - started, 3),
                    'polls': polls,
                    'unstable_bus_polls': unstable_polls,
                    'completed_cycles': state.rows,
                    'in_progress_cycle': state.current,
                    'baseline_confirmed': state.identity is not None,
                    'backend_version': state.runtime[1] if state.runtime else None,
                    'device_capture_started': False,
                    'manual_cable_action_asserted': False,
                    'full_p0_acceptance': False,
                }
            )

        def budget():
            remaining = deadline - clock()
            require(remaining >= 0.05, 'observation_deadline')
            return min(2, remaining)

        save()
        try:
            checkpoint = started + 10
            while True:
                bus = read_bus(budget())
                h = request(socket, operation='health', timeout=budget())
                t = request(socket, operation='transport_status', timeout=budget())
                bus_after = read_bus(budget())
                now, wall_now = clock(), wall()
                require(
                    0 <= now - previous[0] <= MAX_POLL_GAP
                    and 0 <= wall_now - previous[1] <= MAX_POLL_GAP
                    and now <= deadline,
                    'observation_poll_gap',
                )
                completed = False
                stable_bus = bus == bus_after
                if stable_bus:
                    previous = (now, wall_now)
                    polls += 1
                    completed = state.sample(bus, h, t, now - started)
                else:
                    # An edge crossed the probe window. Keep polling, but do
                    # not attach either host reading to an uncertain bus state.
                    unstable_polls += 1
                    state.validate_runtime(h, t)
                if len(state.rows) == args.cycles:
                    outcome = 'passed'
                    break
                if (polls == 1 and stable_bus) or completed or now >= checkpoint:
                    save()
                    checkpoint = now + 10
                sleep(min(INTERVAL, max(0, deadline - clock())))
        except CheckFailure as error:
            outcome, reason = 'incomplete', str(error)
        except (OSError, ValueError, TypeError, RecursionError):
            outcome, reason = 'incomplete', 'read_only_probe_unavailable'
        except KeyboardInterrupt:
            outcome, reason = 'cancelled', 'interrupted'
        finally:
            save()
    return {'passed': 0, 'cancelled': 130}.get(outcome, 2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cycles', type=int, default=20)
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.cycles <= 20 or not 2 <= args.seconds <= 3600:
        parser.error('Expected 1–20 cycles and 2–3600 seconds')
    if not args.execute:
        print(
            json.dumps(
                {
                    'execute': False,
                    'scope': 'idle_usb_bus_reenumeration_and_route',
                    'cycles': args.cycles,
                    'seconds': args.seconds,
                }
            )
        )
        return 0
    if sys.platform != 'darwin' or args.output is None:
        parser.error('Executing requires macOS and an output path in an owner-private directory')
    if not args.runtime_root.is_absolute() or args.runtime_root.resolve() != args.runtime_root:
        parser.error('Executing requires a real absolute runtime root')
    try:
        result = run(args)
        print(args.output)
        return result
    except (OSError, ValueError):
        print('Unable to write a private USB handover report.', file=sys.stderr)
        return 2
