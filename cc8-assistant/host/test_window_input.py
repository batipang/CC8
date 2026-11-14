"""Synthetic pixels and inherited-pipe fixtures only; never select/capture a real window."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_vision as fixtures
from window_input import (
    WindowPicker,
    WindowSelectionEnded,
    decode_result,
    verified_helper,
    source_hashes,
)
from failure_feedback import feedback
from operation_failure import OperationFailure
from protocol import validate_device_event

IDENT = 'a' * 32


def response(**changes):
    value = dict(
        status='completed',
        request_id=IDENT,
        source_type='mac_window',
        window_id=7,
        process_id=99,
        application='Synthetic',
        title='Fixture',
        width=640,
        height=480,
        image_base64=base64.b64encode(fixtures.jpeg()).decode(),
        question='请概括',
    )
    value.update(changes)
    return (json.dumps(value) + '\n').encode()


class WindowProtocolTests(unittest.TestCase):
    def test_failure_guidance_names_the_mac_scope_and_does_not_suggest_camera_permission(self):
        error = OperationFailure(
            'private error', stage='vision', kind='timeout', input_source='mac_window'
        )
        result = feedback(error.stage, error.kind, input_source=error.input_source)
        self.assertIn('Mac 系统面板', result.message)
        self.assertNotIn('摄像头', result.message)
        self.assertNotIn('private error', result.message)

    def test_verified_frame_keeps_explicit_source_without_other_window_scope(self):
        selected = decode_result(response(), IDENT)
        self.assertEqual(selected.image, fixtures.jpeg())
        prompt = selected.prompt()
        for text in ('mac_window', 'Fixture', '用户问题：请概括'):
            self.assertIn(text, prompt)
        self.assertNotIn('image_base64', repr(selected))
        self.assertNotIn('\\xff', repr(selected))

    def test_wrong_scope_identity_geometry_labels_and_envelopes_are_rejected(self):
        for change in (
            dict(source_type='display'),
            dict(request_id='b' * 32),
            dict(window_id=True),
            dict(process_id=0),
            dict(width=1281),
            dict(width=639),
            dict(title='bad\nlabel'),
            dict(application='x' * 101),
            dict(image_base64='!'),
            dict(unexpected=True),
            dict(question=''),
            dict(question='x' * 1001),
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                decode_result(response(**change), IDENT)
        for data in (response() + response(), response()[:-1], b'x' * (1024 * 1024 + 8193)):
            with self.assertRaises(ValueError):
                decode_result(data, IDENT)

    def test_cancel_and_timeout_never_return_a_previous_image(self):
        for status in ('cancelled', 'expired', 'capture_failed', 'selection_invalid'):
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                decode_result(response(status=status), IDENT)

    def test_device_window_mode_requires_valid_explicit_nonce(self):
        validate_device_event(dict(event='vision_request', mode='mac_window', nonce=IDENT))
        for nonce in ('', '../bad', True):
            with self.assertRaises(ValueError):
                validate_device_event(dict(event='vision_request', mode='mac_window', nonce=nonce))


class WindowProcessTests(unittest.TestCase):
    def helper(self, folder, body):
        path = Path(folder) / 'synthetic-picker'
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o700)
        return path

    def test_actual_pipe_accepts_one_matched_result_and_reaps_only_its_child(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.helper(
                folder,
                'import sys,json\njson.loads(sys.stdin.readline())\nsys.stdout.buffer.write('
                + repr(response())
                + ')\nsys.stdout.flush()\nsys.stdin.read()\n',
            )
            picker = WindowPicker()
            children = []
            real = subprocess.Popen

            def launch(*args, **kwargs):
                child = real(*args, **kwargs)
                children.append(child)
                return child

            with (
                patch('window_input.verified_helper', return_value=path),
                patch('window_input.subprocess.Popen', side_effect=launch),
            ):
                result = picker.choose('概括这个窗口', IDENT, lambda: True)
            self.assertEqual(result.window_id, 7)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].returncode)
            self.assertIsNone(picker.process)

    def test_cancel_before_launch_never_creates_a_process(self):
        picker = WindowPicker()
        picker.cancel()
        with (
            patch('window_input.verified_helper', return_value=Path('/unused')),
            patch('window_input.subprocess.Popen') as launch,
        ):
            with self.assertRaises(RuntimeError):
                picker.choose('概括窗口', IDENT, lambda: True)
            launch.assert_not_called()

    def test_early_helper_exit_is_not_falsely_reported_as_a_ninety_second_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.helper(folder, 'import sys\nsys.stdin.readline()\nsys.exit(2)\n')
            picker = WindowPicker()
            with patch('window_input.verified_helper', return_value=path):
                with self.assertRaises(WindowSelectionEnded) as failure:
                    picker.choose('概括窗口', IDENT, lambda: True)
            self.assertEqual(failure.exception.reason, 'capture_failed')
            self.assertIsNone(picker.process)

    def test_owner_revocation_while_waiting_terminates_and_reaps_the_bounded_child(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.helper(folder, 'import sys\nsys.stdin.readline()\nsys.stdin.read()\n')
            picker = WindowPicker()
            outcome = []

            def choose():
                try:
                    picker.choose('概括窗口', IDENT, lambda: True)
                    outcome.append('unexpected_image')
                except RuntimeError:
                    outcome.append('cancelled')

            with patch('window_input.verified_helper', return_value=path):
                thread = threading.Thread(target=choose)
                thread.start()
                deadline = time.monotonic() + 3
                while picker.process is None and time.monotonic() < deadline:
                    thread.join(0.01)
                child = picker.process
                self.assertIsNotNone(child)
                picker.cancel()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, ['cancelled'])
            self.assertIsNotNone(child.returncode)
            self.assertIsNone(picker.process)


class WindowVisionTests(unittest.TestCase):
    def bridge(self):
        bridge = fixtures.BridgeVisionTests().bridge()
        bridge.visual.followup_capable = True
        bridge.answer = lambda *args, **kwargs: setattr(bridge, 'answered', (args, kwargs))
        return bridge

    def test_reentrant_cancellation_cannot_replace_a_newer_request_owner(self):
        bridge = self.bridge()
        cancel = bridge.cancel
        entered = []

        def replace(reason):
            generation = cancel(reason)
            if not entered:
                entered.append(True)
                bridge.begin_visual('后来明确提交的照片问题', 'button', 'b' * 32)
            return generation

        bridge.cancel = replace
        with patch('vision_controller.window_available', return_value=True):
            bridge.on_event(dict(event='vision_request', nonce=IDENT, mode='mac_window'))
        self.assertEqual(bridge.visual.generation, bridge.generation)
        self.assertEqual(bridge.job.nonce, 'b' * 32)
        self.assertTrue(bridge.valid(bridge.job.generation))

    def test_device_request_uses_window_helper_without_camera_mic_or_photo_retention(self):
        bridge = self.bridge()
        selected = decode_result(response(), IDENT)
        with (
            patch('vision_controller.window_available', return_value=True),
            patch('vision_controller.WindowPicker') as factory,
        ):
            factory.return_value.choose.return_value = selected
            bridge.on_event(dict(event='vision_request', nonce=IDENT, mode='mac_window'))
            self.assertEqual(bridge.job.origin, 'mac_window')
            self.assertEqual(bridge.events[-1][1]['origin'], 'mac_window')
            bridge.answer_visual(bridge.job)
        self.assertFalse(bridge.session.active)
        self.assertFalse(any(name == 'camera_capture' for name, _ in bridge.events))
        self.assertIsNone(bridge.visual.inbox.pending)
        self.assertIsNone(bridge.visual.followup.metadata(bridge.generation))
        self.assertIn('mac_window', bridge.answered[0][0])
        self.assertIn('用户问题：请概括', bridge.answered[0][0])
        self.assertNotIn(bridge.job.text, bridge.answered[0][0])
        self.assertEqual(bridge.answered[1]['image'], fixtures.jpeg())
        self.assertIsNone(bridge.visual.window_picker)
        factory.return_value.cancel.assert_called_once()

    def test_unavailable_does_not_fall_back_to_camera_or_cancel_existing_work(self):
        bridge = self.bridge()
        generation = bridge.generation
        with patch('vision_controller.window_available', return_value=False):
            bridge.on_event(dict(event='vision_request', nonce=IDENT, mode='mac_window'))
        self.assertEqual(bridge.generation, generation)
        self.assertFalse(hasattr(bridge, 'job'))
        self.assertEqual(bridge.events[-1][0], 'vision_unavailable')

    def test_window_metadata_survives_real_response_and_explicit_save_without_pixels(self):
        from bridge import Bridge
        from saved_tasks import SavedTasks

        bridge = self.bridge()
        bridge.answer = Bridge.answer.__get__(bridge)
        selected = decode_result(response(), IDENT)
        bridge.speak = lambda *args: True

        class Brain:
            last_metrics = {'completed': True}

            def vision_clauses(self, text, image, history, current, update):
                if image != selected.image:
                    raise AssertionError('Wrong input image')
                yield '已分析窗口'

        with (
            patch('vision_controller.window_available', return_value=True),
            patch('vision_controller.WindowPicker') as factory,
        ):
            factory.return_value.choose.return_value = selected
            bridge.on_event(dict(event='vision_request', nonce=IDENT, mode='mac_window'))
            bridge.brain = Brain()
            bridge.answer_visual(bridge.job)
        task = bridge.workspace.snapshot()['tasks'][0]
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['window_input'], selected.receipt())
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'results.json'
            store = SavedTasks(archive)
            store.save(task)
            self.assertEqual(SavedTasks(archive).snapshot()[0]['window_input'], selected.receipt())
            saved = json.loads(archive.read_bytes())
            self.assertEqual(saved['schema_version'], 3)
            self.assertNotIn('image_base64', str(saved))
            self.assertNotIn('process_id', str(saved))
            self.assertTrue(store.delete(task['id']))
            self.assertEqual(json.loads(archive.read_bytes())['schema_version'], 2)

    def test_handover_while_choosing_rejects_late_pixels_and_preserves_new_owner(self):
        bridge = self.bridge()
        with (
            patch('vision_controller.window_available', return_value=True),
            patch('vision_controller.WindowPicker') as factory,
        ):
            bridge.on_event(dict(event='vision_request', nonce=IDENT, mode='mac_window'))
            old = bridge.job

            def choose(*args):
                bridge.begin_visual('新的显式照片请求', 'button', 'b' * 32)
                return decode_result(response(), IDENT)

            factory.return_value.choose.side_effect = choose
            bridge.answer_visual(old)
        self.assertFalse(hasattr(bridge, 'answered'))
        self.assertEqual(bridge.visual.generation, bridge.job.generation)
        self.assertNotEqual(bridge.visual.generation, old.generation)
        self.assertFalse(any(name == 'camera_capture' for name, _ in bridge.events))
        self.assertIsNone(bridge.visual.window_picker)

    def test_cancelled_selector_has_no_image_or_camera_fallback(self):
        bridge = self.bridge()
        with (
            patch('vision_controller.window_available', return_value=True),
            patch('vision_controller.WindowPicker') as factory,
        ):
            factory.return_value.choose.side_effect = WindowSelectionEnded('cancelled')
            bridge.on_event(dict(event='vision_request', nonce=IDENT, mode='mac_window'))
            bridge.answer_visual(bridge.job)
        self.assertFalse(hasattr(bridge, 'answered'))
        self.assertFalse(any(name == 'camera_capture' for name, _ in bridge.events))
        self.assertIsNone(bridge.visual.generation)
        self.assertIsNone(bridge.visual.window_picker)
        self.assertFalse(any(name == 'runtime_error' for name, _ in bridge.events))

    def test_expired_selection_reports_timeout_with_mac_scope_without_camera_fallback(self):
        bridge = self.bridge()
        with (
            patch('vision_controller.window_available', return_value=True),
            patch('vision_controller.WindowPicker') as factory,
        ):
            factory.return_value.choose.side_effect = WindowSelectionEnded('expired')
            bridge.on_event(dict(event='vision_request', nonce=IDENT, mode='mac_window'))
            bridge.answer_visual(bridge.job)
        self.assertFalse(hasattr(bridge, 'answered'))
        self.assertEqual(bridge.metrics['vision_last_error'], 'timeout')
        self.assertFalse(any(name == 'camera_capture' for name, _ in bridge.events))


class WindowArtifactTests(unittest.TestCase):
    def test_exact_binary_source_identity_and_owner_modes_before_any_process_launch(self):
        import hashlib

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o700)
            binary = root / 'CC8WindowInput.app/Contents/MacOS/cc8-window-picker'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'inert synthetic fixture; never executed')
            binary.chmod(0o700)
            record = dict(
                protocol=1,
                sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                sources=source_hashes(),
            )
            manifest = root / 'build.json'
            manifest.write_text(json.dumps(record))
            manifest.chmod(0o600)
            with (
                patch('window_input.platform.system', return_value='Darwin'),
                patch('window_input.platform.mac_ver', return_value=('15.2', '', '')),
            ):
                self.assertEqual(verified_helper(root), binary.resolve())
                binary.write_bytes(b'changed binary')
                with self.assertRaises(ValueError):
                    verified_helper(root)
                record['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
                record['sources']['mac-window-input/Package.swift'] = '0' * 64
                manifest.write_text(json.dumps(record))
                with self.assertRaises(ValueError):
                    verified_helper(root)
                record['sources'] = source_hashes()
                manifest.write_text(json.dumps(record))
                binary.chmod(0o755)
                with self.assertRaises(ValueError):
                    verified_helper(root)


if __name__ == '__main__':
    unittest.main()
