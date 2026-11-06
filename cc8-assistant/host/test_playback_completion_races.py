"""Real threads and bounded barriers exercise completion/cancel ownership."""

import threading
import time
import unittest
from unittest.mock import patch
import numpy as np
from bridge import Bridge
from operation_failure import OperationFailure


class CompletionRacesTests(unittest.TestCase):
    def session(self):
        with patch('threading.Thread.start'):
            b = Bridge(None, None)
        b.session.active = True
        b.session.connection = object()
        b.save_status = lambda: None
        b.log = lambda *a, **k: None
        b.send = lambda packet: None
        b.event = lambda *a, **k: None
        return b

    def test_done_requires_end_submission_and_complete_frames_and_is_consumed_once(self):
        b = self.session()
        b.playback.device_can_report_completion = True
        generation = b.generation
        playback_id = None
        observations = []

        def done(frames):
            b.on_event(
                {
                    'event': 'play_done',
                    'generation': generation,
                    'playback_id': playback_id,
                    'played_frames': frames,
                }
            )
            return b.playback.playing, generation in b.playback.completed

        def output(name, **fields):
            nonlocal playback_id
            if name == 'play_begin':
                playback_id = fields['playback_id']
                observations.append(done(400))
            if name == 'play_end':
                observations.append(done(fields['frames'] - 1))
                observations.append(done(fields['frames'] + 2))  # Pause recovery can pad the tail.
                end = b.playback.last_audio_end
                with patch('playback_controller.time.monotonic', return_value=end + 1):
                    done(fields['frames'] + 2)
                observations.append(b.playback.last_audio_end == end)

        b.event = output
        self.assertTrue(b.play(np.ones(400, dtype=np.int16), 'fixed', generation))
        self.assertEqual(observations, [(True, False), (True, False), (False, True), True])

    def test_cancelled_unpublished_clause_keeps_the_existing_wake_reference(self):
        b = self.session()
        b.playback.last_spoken = '后一句。'
        b.playback.previous_spoken = 'Bumblebee。'
        before = time.monotonic() + 1
        b.playback.previous_spoken_until = before
        generation = b.generation

        def supersede(_):
            b.cancel('new_request')
            return 0

        with patch.object(b.playback, 'wait_playback', side_effect=supersede):
            self.assertFalse(b.play(np.ones(400, dtype=np.int16), '未播报的下一句。', generation))

        self.assertEqual(b.playback.previous_spoken, 'Bumblebee。')
        self.assertEqual(b.playback.previous_spoken_until, before)
        self.assertEqual(b.playback.last_spoken, '后一句。')

    def test_previous_clause_wake_reference_window_starts_at_publication(self):
        b = self.session()
        b.playback.last_spoken = 'Bumblebee。'
        generation = b.generation
        now = 100.0
        paused = False
        observed = []

        def wait(_):
            nonlocal now, paused
            if not paused:
                now += 2
                paused = True
            return 0

        def output(name, **fields):
            if name == 'play_end':
                b.on_event({'event': 'play_done', 'generation': generation})

        def send(_):
            observed.append((b.playback.previous_spoken, b.playback.previous_spoken_until - now))

        b.event = output
        b.send = send
        with (
            patch('playback_controller.time.monotonic', side_effect=lambda: now),
            patch.object(b.playback, 'wait_playback', side_effect=wait),
        ):
            self.assertTrue(b.play(np.ones(400, dtype=np.int16), '下一句。', generation))
        self.assertEqual(observed[0], ('Bumblebee。', 1.0))

    def test_failed_play_begin_does_not_promote_unsent_words_to_wake_reference(self):
        b = self.session()
        b.playback.last_spoken = '最后播出的一句。'
        b.playback.previous_spoken = '上一句。'
        before = time.monotonic() + 1
        b.playback.previous_spoken_until = before

        def output(name, **fields):
            if name == 'play_begin':
                raise BrokenPipeError('fixture did not send play_begin')

        b.event = output
        with self.assertRaises(OperationFailure) as failed:
            b.play(np.ones(400, dtype=np.int16), 'Bumblebee。', b.generation)
        self.assertEqual(failed.exception.stage, 'transport')
        self.assertEqual(b.playback.last_spoken, '最后播出的一句。')
        self.assertEqual(b.playback.previous_spoken, '上一句。')
        self.assertEqual(b.playback.previous_spoken_until, before)

    def test_previous_clause_done_cannot_clear_the_next_clause_playing_state(self):
        b = self.session()
        generation = b.generation
        first_ack_exit, release_ack = threading.Event(), threading.Event()
        second_started, release_second = threading.Event(), threading.Event()
        errors, results, readers = [], [], []
        original = b.playback.done

        class Completion:
            def __enter__(self):
                return original.__enter__()

            def __exit__(self, *args):
                result = original.__exit__(*args)
                if threading.current_thread().name == 'first-clause-ack':
                    first_ack_exit.set()
                    if not release_ack.wait(2):
                        raise RuntimeError('ACK exit barrier timed out')
                return result

            def wait(self, timeout):
                return original.wait(timeout)

            def notify_all(self):
                return original.notify_all()

        b.playback.done = Completion()
        clause = [0]

        def checked(function):
            try:
                function()
            except BaseException as error:
                errors.append(type(error).__name__)

        def output(name, **fields):
            if name == 'play_begin':
                clause[0] += 1
                if clause[0] == 2:
                    second_started.set()
            elif name == 'play_end' and clause[0] == 1:
                reader = threading.Thread(
                    target=lambda: checked(
                        lambda: b.on_event({'event': 'play_done', 'generation': generation})
                    ),
                    name='first-clause-ack',
                    daemon=True,
                )
                readers.append(reader)
                reader.start()
            elif name == 'play_end':
                if not release_second.wait(2):
                    raise RuntimeError('Second clause barrier timed out')
                b.on_event({'event': 'play_done', 'generation': generation})

        b.event = output

        def render():
            for label in ('first', 'second'):
                results.append(b.play(np.ones(4, dtype=np.int16), label, generation))

        worker = threading.Thread(target=lambda: checked(render), daemon=True)
        worker.start()
        try:
            self.assertTrue(first_ack_exit.wait(1))
            # On the old code the second clause publishes before the first
            # reader clears playing. With the fix it waits for publication.
            second_started.wait(0.1)
            release_ack.set()
            for reader in readers:
                reader.join(1)
                self.assertFalse(reader.is_alive())
            self.assertTrue(second_started.wait(1))
            self.assertTrue(b.playback.playing, 'Previous ACK cleared the next clause')
        finally:
            release_ack.set()
            release_second.set()
            worker.join(2)
            for reader in readers:
                reader.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True, True])
        self.assertFalse(b.playback.playing)

    def test_timeout_cannot_cancel_or_publish_error_over_a_new_owner_request(self):
        b = self.session()
        generation = b.generation
        b.diagnostics.begin('text', generation)
        timeout_seen, release_timeout, submitted = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        errors, results, owner_generations, statuses = [], [], [], []
        now = [0.0]

        def log(name, **fields):
            if name == 'playback_completion_timeout':
                timeout_seen.set()
                if not release_timeout.wait(2):
                    raise RuntimeError('Timeout barrier timed out')

        b.log = log
        b.status = lambda phase, message: statuses.append(phase)

        def checked(function):
            try:
                function()
            except BaseException as error:
                errors.append(type(error).__name__)

        def owner():
            with b.session.guard:
                new = b.cancel('new_owner_request')
                b.session.text_generation = new
                owner_generations.append(new)
                b.status('thinking', 'new request')
                submitted.set()

        def wait(_):
            now[0] += 5

        worker = threading.Thread(
            target=lambda: checked(
                lambda: results.append(b.play(np.ones(4, dtype=np.int16), 'old', generation))
            ),
            daemon=True,
        )
        control = threading.Thread(target=lambda: checked(owner), daemon=True)
        with (
            patch('playback_controller.time.monotonic', side_effect=lambda: now[0]),
            patch.object(b.playback.done, 'wait', side_effect=wait),
        ):
            worker.start()
            try:
                self.assertTrue(timeout_seen.wait(1))
                control.start()
                submitted.wait(0.1)
            finally:
                release_timeout.set()
                worker.join(2)
                if control.ident is not None:
                    control.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(control.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [False])
        self.assertEqual(len(owner_generations), 1)
        self.assertEqual(b.generation, owner_generations[0], 'Old timeout cancelled new request')
        self.assertTrue(b.valid(owner_generations[0]))
        self.assertEqual(statuses[-1], 'thinking', 'Old timeout overwrote the new UI')

    def test_device_and_stream_failure_cannot_cancel_a_new_owner_request(self):
        for source in ('device', 'stream'):
            with self.subTest(source=source):
                b = self.session()
                generation = b.generation
                b.diagnostics.begin('text', generation)
                reached, release, submitted = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )
                errors, statuses, owner_generations = [], [], []
                original_cancel = b.cancel

                def cancel(reason):
                    if reason in ('playback_error', 'audio_stream_error'):
                        reached.set()
                        if not release.wait(2):
                            raise RuntimeError('Failure cancellation barrier timed out')
                    return original_cancel(reason)

                b.cancel = cancel
                b.status = lambda phase, message: statuses.append(phase)

                def checked(function):
                    try:
                        function()
                    except BaseException as error:
                        errors.append(type(error).__name__)

                def owner():
                    with b.session.guard:
                        new = b.cancel('new_owner_request')
                        b.session.text_generation = new
                        owner_generations.append(new)
                        b.status('thinking', 'new request')
                        submitted.set()

                def failing():
                    yield np.ones(3200, dtype=np.int16)
                    raise TimeoutError('Synthetic provider failure')

                def fail():
                    if source == 'device':
                        b.on_event({'event': 'playback_error', 'generation': generation})
                    else:
                        try:
                            b.play_stream(failing(), 'old', generation)
                        except OperationFailure as error:
                            self.assertEqual(error.stage, 'tts')
                        else:
                            raise AssertionError('Provider exception was swallowed')

                worker = threading.Thread(target=lambda: checked(fail), daemon=True)
                control = threading.Thread(target=lambda: checked(owner), daemon=True)
                worker.start()
                try:
                    self.assertTrue(reached.wait(1))
                    control.start()
                    submitted.wait(0.1)
                finally:
                    release.set()
                    worker.join(2)
                    if control.ident is not None:
                        control.join(2)
                self.assertFalse(worker.is_alive())
                self.assertFalse(control.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(owner_generations), 1)
                self.assertEqual(b.generation, owner_generations[0])
                self.assertTrue(b.valid(owner_generations[0]))
                self.assertEqual(statuses[-1], 'thinking')

    def test_expired_pause_status_cannot_overwrite_a_new_owner_request(self):
        b = self.session()
        generation = b.generation
        b.playback.pause.pause_state = {
            'token': 1,
            'generation': generation,
            'started': time.monotonic() - 21,
        }
        reached, release, submitted = threading.Event(), threading.Event(), threading.Event()
        statuses, errors = [], []

        def status(phase, message):
            if message == '插话识别超时，已停止旧播报':
                reached.set()
                if not release.wait(2):
                    raise RuntimeError('Expired-pause status barrier timed out')
            statuses.append(phase)

        b.status = status

        def checked(function):
            try:
                function()
            except BaseException as error:
                errors.append(type(error).__name__)

        def owner():
            with b.session.guard:
                new = b.cancel('new_owner_request')
                b.session.text_generation = new
                b.status('thinking', 'new request')
                submitted.set()

        worker = threading.Thread(
            target=lambda: checked(lambda: b.playback.wait_playback(generation)), daemon=True
        )
        control = threading.Thread(target=lambda: checked(owner), daemon=True)
        worker.start()
        try:
            self.assertTrue(reached.wait(1))
            control.start()
            submitted.wait(0.1)
        finally:
            release.set()
            worker.join(2)
            if control.ident is not None:
                control.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(control.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(submitted.is_set())
        self.assertEqual(statuses, ['error', 'thinking'])


if __name__ == '__main__':
    unittest.main()
