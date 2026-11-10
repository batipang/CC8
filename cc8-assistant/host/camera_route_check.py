"""Explicit camera-preview interruption checks; logical outages, no image export or model calls."""

from copy import deepcopy
import re
import time

CASES = ('wifi_handover', 'all_routes_lost')
COUNTERS = ('voice_asr_attempts', 'mic_frames', 'render_frames', 'desktop_snapshots_captured')


class CameraCheckFailure(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise CameraCheckFailure(reason)


class CameraRouteRun:
    def __init__(self, device, request, probe_factory, *, clock=time.monotonic, sleep=time.sleep):
        self.device, self.request, self.probe_factory = device, request, probe_factory
        self.clock, self.sleep = clock, sleep
        self.owner = self.counters = None
        self.probe = None
        self.cases = []
        self.deadline = float('inf')
        self.started = False

    def health(self):
        value = self.request({'operation': 'health'})
        require(
            value.get('camera_metadata_only_supported') is True, 'metadata_only_camera_unsupported'
        )
        identity = (value.get('runtime_instance'), value.get('runtime_process_id'))
        require(
            isinstance(identity[0], str)
            and re.fullmatch('[a-f0-9]{32}', identity[0])
            and type(identity[1]) is int
            and identity[1] > 1
            and type(value.get('health_owner_version')) is int
            and value['health_owner_version'] == 1,
            'runtime_identity_unconfirmed',
        )
        if self.owner is not None:
            require(identity == self.owner, 'runtime_owner_changed')
        current = []
        for key in COUNTERS:
            count = value.get(key)
            require(type(count) is int and count >= 0, 'camera_counter_unavailable')
            current.append(count)
        if self.counters is not None:
            require(tuple(current) == self.counters, 'media_or_model_counter_changed')
        require(
            all(
                value.get(key) is False
                for key in ('microphone_active', 'playing', 'calibrating', 'text_request_active')
            ),
            'owner_audio_or_task_active',
        )
        require(
            type(value.get('session_generation')) is int and value['session_generation'] >= 0,
            'generation_unconfirmed',
        )
        return value

    def device_state(self):
        state, ui = self.device.status()
        require(state.get('foreground') is True, 'assistant_left_foreground')
        require(state.get('microphone_active') is False, 'device_microphone_active')
        require(
            type(state.get('vision_active')) is bool and type(ui.get('camera_open')) is bool,
            'camera_state_unconfirmed',
        )
        captures = ui.get('camera_captures')
        require(type(captures) is int and captures >= 0, 'device_capture_counter_unavailable')
        return state, ui

    def wait(self, condition, seconds, reason):
        deadline = min(self.deadline, self.clock() + seconds)
        while self.clock() < deadline:
            value = condition()
            require(self.clock() < deadline, reason)
            if value:
                return value
            self.sleep(0.1)
        raise CameraCheckFailure(reason)

    def preflight(self):
        health = self.health()
        require(
            health.get('desktop_snapshot_active') is False and health.get('vision_active') is False,
            'owner_camera_busy',
        )
        require(
            health.get('connected') is True and health.get('transport') == 'usb', 'usb_not_ready'
        )
        state, ui = self.device_state()
        require(
            state['vision_active'] is False and ui['camera_open'] is False, 'device_camera_busy'
        )
        routes = self.request({'operation': 'transport_status'})
        require(
            routes.get('usb_ready') is True
            and routes.get('wifi_ready') is True
            and routes.get('usb_test_suppressed') is False
            and routes.get('all_routes_test_suppressed') is False,
            'paired_routes_not_ready',
        )
        if self.owner is None:
            self.owner = (health['runtime_instance'], health['runtime_process_id'])
            self.counters = tuple(health[key] for key in COUNTERS)
        return health

    def inactive(self, captures=None, route=None):
        health = self.health()
        state, ui = self.device_state()
        if captures is not None:
            require(ui['camera_captures'] == captures, 'device_captured_after_preview_cancel')
        return (
            health.get('vision_active') is False
            and health.get('desktop_snapshot_active') is False
            and state['vision_active'] is False
            and ui['camera_open'] is False
            and (
                route is None
                or health.get('transport') == route
                and health.get('connected') is (route != 'none')
                and state.get('transport') == route
                and state.get('connected') is (route != 'none')
            )
        )

    def interrupted_case(self, kind, row):
        before = self.preflight()
        generation = before['session_generation'] + 1
        probe = self.probe = self.probe_factory(
            before['runtime_instance'], before['session_generation']
        )
        row['camera_request_attempted'] = True
        probe.start()

        def preview():
            require(not probe.done.is_set(), 'camera_request_ended_before_outage')
            health = self.health()
            state, ui = self.device_state()
            if health.get('desktop_snapshot_active') is not True:
                return None
            require(
                health.get('desktop_snapshot_generation') == generation
                and health['session_generation'] == generation,
                'camera_preview_owner_changed',
            )
            elapsed, hold = ui.get('camera_preview_elapsed_ms'), ui.get('camera_preview_hold_ms')
            if state['vision_active'] is not True or ui['camera_open'] is not True:
                return None
            require(
                type(elapsed) is int and type(hold) is int and hold >= 5000,
                'preview_clock_unconfirmed',
            )
            require(elapsed < 3500, 'preview_interruption_window_missed')
            return ui if elapsed > 0 else None

        ui = self.wait(preview, 7, 'visible_preview_not_observed')
        row.update(
            request_id=probe.ident,
            generation=generation,
            preview_elapsed_ms=ui['camera_preview_elapsed_ms'],
        )
        captures = ui['camera_captures']
        health = self.health()
        require(
            not probe.done.is_set() and health.get('desktop_snapshot_generation') == generation,
            'camera_ended_before_outage',
        )
        operation = dict(
            operation='camera_route_interrupt',
            route=kind,
            expected_instance=before['runtime_instance'],
            expected_generation=generation,
        )
        row['outage_requested'] = True
        reply = self.request(operation)
        require(reply.get('ok') is True, 'camera_route_outage_rejected')
        row['outage_started'] = True
        suppressed = (
            'all_routes_test_suppressed' if kind == 'all_routes_lost' else 'usb_test_suppressed'
        )
        expected_route = 'none' if kind == 'all_routes_lost' else 'wifi'

        def cancelled():
            health = self.health()
            routes = self.request({'operation': 'transport_status'})
            require(routes.get(suppressed) is True, 'camera_outage_window_ended')
            return (
                health.get('transport') == expected_route
                and health.get('connected') is (kind == 'wifi_handover')
                and self.inactive(captures, expected_route)
            )

        self.wait(cancelled, 6, 'camera_not_released_on_route_change')
        self.wait(lambda: probe.done.is_set(), 3, 'camera_cancel_receipt_missing')
        require(
            probe.error is None
            and probe.result
            == dict(status='interrupted', request_id=probe.ident, metadata_only=True),
            'camera_cancel_receipt_unconfirmed',
        )
        row.update(camera_released=True, matched_interruption_receipt=True, images_returned=0)

        def restored():
            health = self.health()
            routes = self.request({'operation': 'transport_status'})
            require(self.inactive(captures), 'camera_reopened_during_reconnect')
            return (
                health.get('transport') == 'usb'
                and health.get('connected') is True
                and routes.get('usb_ready') is True
                and routes.get('wifi_ready') is True
                and routes.get('usb_test_suppressed') is False
                and routes.get('all_routes_test_suppressed') is False
                and self.inactive(captures, 'usb')
            )

        self.wait(restored, 25, 'usb_priority_not_restored')
        began = self.clock()
        while self.clock() - began < 3:
            require(self.inactive(captures, 'usb'), 'camera_or_route_changed_after_reconnect')
            require(self.clock() < self.deadline, 'camera_case_deadline')
            self.sleep(0.1)
        row.update(
            usb_priority_restored=True,
            reconnect_observed_seconds=round(self.clock() - began, 3),
            counters_unchanged=True,
        )

    def run(self, selected=CASES, checkpoint=lambda report: None):
        require(
            isinstance(selected, tuple)
            and 0 < len(selected) <= 2
            and len(set(selected)) == len(selected)
            and all(case in CASES for case in selected),
            'invalid_camera_cases',
        )
        require(not self.started, 'camera_route_run_already_started')
        self.started = True
        result = dict(
            schema=1,
            state='running',
            scope='explicit_metadata_only_camera_preview_logical_routes',
            cases=self.cases,
            images_saved=False,
            images_exported=False,
            microphone_started=False,
            model_requests_started=False,
            physical_usb_unplug_tested=False,
            real_camera_test_performed=False,
            cleanup_scope='owned_camera_request_only',
            full_p0_acceptance=False,
        )
        for kind in selected:
            began = self.clock()
            self.deadline = began + 60
            row = dict(case=kind, state='running', cleanup_verified=False)
            self.cases.append(row)
            checkpoint(deepcopy(result))
            self.probe = None
            try:
                self.interrupted_case(kind, row)
                row['state'] = 'passed'
            except KeyboardInterrupt:
                row.update(state='interrupted', reason='operator_interrupted')
            except Exception as error:
                row.update(
                    state='failed',
                    reason=str(error)
                    if isinstance(error, CameraCheckFailure)
                    else 'camera_check_operation_failed',
                )
            finally:
                try:
                    if self.probe is not None:
                        self.probe.close()
                        self.deadline = self.clock() + 30
                        self.wait(self.inactive, 5, 'camera_cleanup_not_confirmed')

                        def route_cleanup():
                            self.health()
                            routes = self.request({'operation': 'transport_status'})
                            return (
                                routes.get('usb_test_suppressed') is False
                                and routes.get('all_routes_test_suppressed') is False
                            )

                        self.wait(route_cleanup, 25, 'camera_route_cleanup_unconfirmed')
                    row['cleanup_verified'] = True
                except Exception:
                    row.update(state='failed', cleanup_reason='owned_camera_cleanup_unconfirmed')
                row['elapsed_seconds'] = round(self.clock() - began, 3)
                result['real_camera_test_performed'] = any(
                    'preview_elapsed_ms' in case for case in self.cases
                )
            if row['state'] != 'passed':
                result['state'] = 'interrupted' if row['state'] == 'interrupted' else 'failed'
                checkpoint(deepcopy(result))
                return result
            checkpoint(deepcopy(result))
        result['state'] = 'passed'
        checkpoint(deepcopy(result))
        return result
