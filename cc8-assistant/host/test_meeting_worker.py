"""Real worker threads, synthetic PCM and controlled per-request clients only."""

import threading
import time
import unittest

from meeting_asr import Transcript
from meeting_session import CaptureOwner, MeetingSession
from meeting_worker import MeetingWorker
from minimax_client import MiniMaxError

PCM = b'\x00\x20' * 1600


class Client:
    def __init__(self, *, blocked=False, error=None, bad_cancel=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.calls = 0
        self.error, self.bad_cancel = error, bad_cancel
        if not blocked:
            self.release.set()

    def transcribe_chunk(self, pcm, is_current):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError('Test client was not released')
        if self.error:
            raise self.error
        # Deliberately disregard cancellation to exercise the worker's commit
        # guard against an uncooperative provider returning late.
        return Transcript('真实回执', len(pcm) * 1000 // 32000)

    def cancel(self):
        self.cancelled.set()
        self.release.set()
        if self.bad_cancel:
            raise RuntimeError('Private transport failure must not escape')


class MeetingWorkerTests(unittest.TestCase):
    def setup_worker(self, clients, *, changed=lambda: None):
        model = MeetingSession('a' * 32)
        lock = threading.Lock()  # No undocumented reentrant-lock requirement.
        creations = []

        def factory():
            client = clients[len(creations)]
            creations.append(client)
            return client

        worker = MeetingWorker(lock, factory, changed=changed)
        worker.bind(model)
        model.start(CaptureOwner(1, 1, format(1, '032x')))
        worker.start()

        def cleanup():
            worker.close()
            for client in clients:
                client.release.set()
            worker.thread.join(2)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(worker.queue.unfinished_tasks, 0)

        self.addCleanup(cleanup)
        return worker, model, creations

    def chunk(self, model, *, pcm=PCM):
        who = model.owner
        accepted, chunks = model.receive(who, 0, 0, pcm)
        self.assertEqual(accepted, len(pcm) // 2)
        self.assertFalse(chunks)
        chunk = model.pause()[0]
        index = len(model.used_nonces) + 1
        model.start(CaptureOwner(1, index, format(index, '032x')))
        return chunk

    def idle(self, worker):
        deadline = time.monotonic() + 2
        while worker.busy() and time.monotonic() < deadline:
            # Waiting is bounded, and only on this confirmed live worker.
            threading.Event().wait(0.005)
        self.assertFalse(worker.busy())

    def test_constructor_bind_and_status_are_passive_and_start_requires_consent(self):
        worker = MeetingWorker(threading.Lock(), lambda: self.fail('Unexpected client'))
        self.assertIsNone(worker.thread)
        self.assertFalse(worker.busy())
        worker.bind(MeetingSession('a' * 32))
        with self.assertRaises(ValueError):
            worker.start()
        with self.assertRaises(ValueError):
            worker.submit(())
        worker.close()
        self.assertIsNone(worker.thread)

    def test_pause_and_end_can_finish_pending_asr_without_restarting_capture(self):
        client = Client(blocked=True)
        worker, model, creations = self.setup_worker([client])
        chunk = self.chunk(model)
        self.assertTrue(worker.submit((chunk,)))
        self.assertTrue(client.entered.wait(1))
        model.end()
        self.assertEqual(model.state, 'ended')
        client.release.set()
        self.idle(worker)
        self.assertEqual(model.rows[0]['text'], '真实回执')
        self.assertTrue(model.snapshot()['processing_complete'])
        self.assertEqual(creations, [client])

    def test_duplicate_batch_and_inflight_submission_are_rejected_before_any_admission(self):
        client = Client(blocked=True)
        worker, model, creations = self.setup_worker([client])
        first, second = self.chunk(model), self.chunk(model)
        with self.assertRaises(ValueError):
            worker.submit((first, first))
        self.assertFalse(worker.busy())
        worker.submit((first,))
        self.assertTrue(client.entered.wait(1))
        with self.assertRaises(ValueError):
            worker.submit((second, first))
        self.assertNotIn(second.ident, worker.inflight)
        client.release.set()
        self.idle(worker)
        self.assertEqual(len(creations), 1)

    def test_full_queue_pauses_and_marks_all_pending_unknown_without_blocking_reader(self):
        client = Client(blocked=True)
        worker, model, creations = self.setup_worker([client])
        chunks = [self.chunk(model) for _ in range(4)]
        worker.submit(chunks[:1])
        self.assertTrue(client.entered.wait(1))
        self.assertTrue(worker.submit(chunks[1:3]))
        self.assertFalse(worker.submit(chunks[3:]))
        self.assertTrue(client.cancelled.is_set())
        self.idle(worker)
        self.assertEqual((model.state, model.reason), ('paused', 'backpressure'))
        self.assertTrue(all(row['state'] == 'unconfirmed' for row in model.rows))
        self.assertEqual(len(creations), 1)

    def test_old_cancel_and_late_result_cannot_change_new_capture_or_cancel_next_client(self):
        old, new = Client(blocked=True), Client()
        worker, model, _ = self.setup_worker([old, new])
        chunk = self.chunk(model)
        worker.submit((chunk,))
        self.assertTrue(old.entered.wait(1))
        worker.interrupt('route_lost')
        self.idle(worker)
        self.assertEqual(model.rows[0]['state'], 'unconfirmed')
        model.start(CaptureOwner(2, 9, format(9, '032x')))
        next_chunk = self.chunk(model)
        worker.submit((next_chunk,))
        self.idle(worker)
        self.assertEqual(model.rows[1]['text'], '真实回执')
        self.assertFalse(new.cancelled.is_set())
        self.assertEqual(new.calls, 1)

    def test_zero_signal_is_unconfirmed_not_silence_and_never_uploaded(self):
        worker, model, creations = self.setup_worker([])
        chunk = self.chunk(model, pcm=bytes(3200))
        worker.submit((chunk,))
        self.idle(worker)
        self.assertFalse(creations)
        self.assertEqual(
            (model.rows[0]['state'], model.rows[0]['reason']), ('unconfirmed', 'no_signal')
        )

    def test_callback_exceptions_are_counted_and_do_not_kill_worker_or_hold_owner_lock(self):
        holder = []
        acquisitions = []

        def changed():
            lock = holder[0].lock
            acquired = lock.acquire(blocking=False)
            acquisitions.append(acquired)
            if acquired:
                lock.release()
            raise RuntimeError('Private callback content')

        worker, model, _ = self.setup_worker([Client(), Client()], changed=changed)
        holder.append(worker)
        for _ in range(2):
            worker.submit((self.chunk(model),))
            self.idle(worker)
        self.assertTrue(worker.thread.is_alive())
        self.assertGreaterEqual(worker.notification_failures, 2)
        self.assertTrue(acquisitions)
        self.assertTrue(all(acquisitions))
        self.assertTrue(all(row['state'] == 'recognized' for row in model.rows))

    def test_cancel_cleanup_failure_cannot_accept_late_result_or_prevent_next_job(self):
        old, new = Client(blocked=True, bad_cancel=True), Client()
        worker, model, _ = self.setup_worker([old, new])
        worker.submit((self.chunk(model),))
        self.assertTrue(old.entered.wait(1))
        worker.interrupt('background')
        self.idle(worker)
        self.assertEqual(worker.cleanup_failures, 1)
        self.assertEqual(model.rows[0]['state'], 'unconfirmed')
        model.start(CaptureOwner(1, 9, format(9, '032x')))
        worker.submit((self.chunk(model),))
        self.idle(worker)
        self.assertEqual(model.rows[1]['state'], 'recognized')

    def test_provider_failure_is_not_retried_and_next_explicit_chunk_can_succeed(self):
        bad, good = Client(error=MiniMaxError('private', http_status=402)), Client()
        worker, model, _ = self.setup_worker([bad, good])
        worker.submit((self.chunk(model),))
        self.idle(worker)
        self.assertEqual(model.rows[0]['reason'], 'quota')
        self.assertEqual((model.state, model.reason), ('paused', 'quota'))
        model.start(CaptureOwner(1, 9, format(9, '032x')))
        worker.submit((self.chunk(model),))
        self.idle(worker)
        self.assertEqual((bad.calls, good.calls), (1, 1))
        self.assertEqual(model.rows[1]['state'], 'recognized')

    def test_closed_worker_never_restarts_or_reopens_deleted_meeting(self):
        client = Client(blocked=True)
        worker, model, _ = self.setup_worker([client])
        worker.submit((self.chunk(model),))
        self.assertTrue(client.entered.wait(1))
        worker.close()
        worker.thread.join(2)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(model.rows[0]['state'], 'unconfirmed')
        with self.assertRaises(ValueError):
            worker.start()
        with self.assertRaises(ValueError):
            worker.bind(MeetingSession('b' * 32))

    def test_cancellation_during_client_construction_disposes_unused_client_without_upload(self):
        client = Client()
        worker, model, creations = self.setup_worker([client])
        factory_entered, factory_release = threading.Event(), threading.Event()

        def factory():
            factory_entered.set()
            if not factory_release.wait(2):
                raise AssertionError('Test factory was not released')
            return client

        worker.factory = factory
        worker.submit((self.chunk(model),))
        self.assertTrue(factory_entered.wait(1))
        worker.interrupt('background')
        factory_release.set()
        self.idle(worker)
        self.assertTrue(client.cancelled.is_set())
        self.assertEqual((client.calls, creations), (0, []))
        self.assertEqual(model.rows[0]['state'], 'unconfirmed')

    def test_quota_failure_cancels_queued_uploads_and_requires_fresh_capture_consent(self):
        client = Client(blocked=True, error=MiniMaxError('private', http_status=402))
        worker, model, creations = self.setup_worker([client])
        chunks = [self.chunk(model) for _ in range(3)]
        worker.submit(chunks[:1])
        self.assertTrue(client.entered.wait(1))
        worker.submit(chunks[1:])
        client.release.set()
        self.idle(worker)
        self.assertEqual((model.state, model.reason), ('paused', 'quota'))
        self.assertTrue(all(row['reason'] == 'quota' for row in model.rows))
        self.assertEqual((len(creations), client.calls), (1, 1))
        with self.assertRaises(ValueError):
            model.start(CaptureOwner(1, 1, format(1, '032x')))

    def test_model_text_budget_failure_pauses_instead_of_continuing_to_upload(self):
        from unittest.mock import patch

        worker, model, _ = self.setup_worker([Client()])
        with patch('meeting_session.MAX_TEXT_BYTES', 1):
            worker.submit((self.chunk(model),))
            self.idle(worker)
        self.assertEqual((model.state, model.reason), ('paused', 'text_limit'))
        self.assertEqual(model.rows[0]['reason'], 'text_limit')

    def test_thread_start_failure_cannot_leave_capture_or_admit_unprocessed_chunks(self):
        from unittest.mock import patch

        worker = MeetingWorker(threading.Lock(), lambda: self.fail('Unexpected client'))
        model = MeetingSession('a' * 32)
        worker.bind(model)
        model.start(CaptureOwner(1, 1, 'b' * 32))
        with patch('meeting_worker.threading.Thread.start', side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):
                worker.start()
        self.assertIsNone(worker.thread)
        self.assertEqual((model.state, model.reason), ('paused', 'asr_failed'))
        with self.assertRaises(ValueError):
            worker.submit(())
        worker.close()


if __name__ == '__main__':
    unittest.main()
