"""A wider native hit is not authority without its own complete, current interval."""

from dataclasses import replace
import json
import unittest
from unittest.mock import Mock

from english_wake_candidates import (
    EnglishConfirmation,
    EnglishWakeCandidates,
    MAX_CANDIDATES,
    MAX_ISSUED,
    MAX_SPAN_NS,
)
from wake_utterance import WaitingUtterance


class EnglishCandidateTests(unittest.TestCase):
    def fixture(self):
        tokens = iter(f'{index:032x}' for index in range(1, 64))
        store = EnglishWakeCandidates(lambda size: next(tokens))
        base = WaitingUtterance(3, 5, 1_000_000_000, model_generation=7)
        return store, base

    def mark(self, store, stamp=1_500_000_000, model=11, serial=3, generation=5):
        return store.mark(stamp, serial=serial, generation=generation, model_generation=model)

    def test_same_retained_interval_receipt_is_one_use_and_preserves_other_model_generation(self):
        store, base = self.fixture()
        self.assertTrue(self.mark(store))
        ticket = store.issue(base, 2_000_000_000, model_generation=11)
        self.assertIsInstance(ticket, EnglishConfirmation)
        self.assertEqual(ticket.model_generation, 7)
        self.assertEqual(ticket.verifier_generation, 11)
        self.assertEqual(ticket.first_sample_ns, base.first_sample_ns)
        self.assertEqual(ticket.last_sample_ns, 2_000_000_000)
        self.assertTrue(store.consume(ticket))
        self.assertFalse(store.consume(ticket))
        self.assertIsNone(store.issue(base, 2_000_000_000, model_generation=11))
        self.assertEqual(
            store.snapshot(),
            {'pending_candidates': 0, 'issued_receipts': 0, 'clock_blocked': False},
        )

    def test_outside_retained_audio_including_later_endpoint_tail_cannot_issue(self):
        for point in (999_999_999, 2_000_000_001, 2_500_000_000):
            store, base = self.fixture()
            self.mark(store, point)
            self.assertIsNone(store.issue(base, 2_000_000_000, model_generation=11))
        for point in (base.first_sample_ns, 2_000_000_000):
            store, base = self.fixture()
            self.mark(store, point)
            self.assertIsNotNone(store.issue(base, 2_000_000_000, model_generation=11))

    def test_forged_receipt_fields_do_not_consume_the_real_receipt(self):
        store, base = self.fixture()
        self.mark(store)
        ticket = store.issue(base, 2_000_000_000, model_generation=11)
        changes = dict(
            serial=4,
            generation=6,
            first_sample_ns=1_000_000_001,
            last_sample_ns=2_000_000_001,
            model_generation=8,
            verifier_generation=12,
            confirmation_token='f' * 32,
        )
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertFalse(store.consume(replace(ticket, **{field: value})))
        self.assertTrue(store.consume(ticket))
        self.assertFalse(store.consume(base))

    def test_capture_or_model_owner_change_clears_both_pending_and_issued(self):
        for changed in ('model', 'serial', 'generation'):
            store, base = self.fixture()
            self.mark(store)
            ticket = store.issue(base, 2_000_000_000, model_generation=11)
            self.mark(store, 2_500_000_000)
            self.mark(
                store,
                3_000_000_000,
                **{changed: {'model': 12, 'serial': 4, 'generation': 6}[changed]},
            )
            self.assertFalse(store.consume(ticket))
            self.assertIsNone(store.issue(base, 3_500_000_000, model_generation=11))

    def test_reset_invalidates_queued_receipt_and_pending_candidate(self):
        store, base = self.fixture()
        self.mark(store)
        ticket = store.issue(base, 2_000_000_000, model_generation=11)
        self.mark(store, 2_500_000_000)
        store.reset()
        self.assertFalse(store.consume(ticket))
        self.assertIsNone(store.issue(base, 3_000_000_000, model_generation=11))
        self.assertEqual(store.snapshot()['pending_candidates'], 0)

    def test_regressed_clock_blocks_same_owner_until_reset_duplicate_is_only_ignored(self):
        store, base = self.fixture()
        self.assertTrue(self.mark(store))
        self.assertFalse(self.mark(store))
        ticket = store.issue(base, 2_000_000_000, model_generation=11)
        self.assertIsNotNone(ticket)
        self.assertFalse(self.mark(store, 1_400_000_000))
        self.assertFalse(self.mark(store, 2_500_000_000))
        self.assertFalse(store.consume(ticket))
        self.assertTrue(store.snapshot()['clock_blocked'])
        store.reset()
        self.assertTrue(self.mark(store))

    def test_bounded_storage_evicts_old_receipts_and_never_exports_private_tokens(self):
        store, _ = self.fixture()
        for index in range(MAX_CANDIDATES + 5):
            self.mark(store, 1_000_000_000 + index)
        self.assertEqual(store.snapshot()['pending_candidates'], MAX_CANDIDATES)
        tickets = []
        for index in range(MAX_ISSUED + 1):
            first = 2_000_000_000 + index * 1_000_000_000
            self.mark(store, first)
            tickets.append(
                store.issue(WaitingUtterance(3, 5, first), first + 1, model_generation=11)
            )
        self.assertEqual(store.snapshot()['issued_receipts'], MAX_ISSUED)
        self.assertFalse(store.consume(tickets[0]))
        self.assertTrue(store.consume(tickets[-1]))
        snapshot = json.dumps(store.snapshot())
        self.assertNotIn(tickets[1].confirmation_token, snapshot)
        self.assertNotIn(tickets[1].confirmation_token, repr(tickets[1]))

    def test_invalid_values_and_nonce_failure_leave_no_new_authority(self):
        for bad in (True, -1, 2**63, 1.25, float('nan'), 'private'):
            store, base = self.fixture()
            self.assertFalse(self.mark(store, bad))
            self.mark(store)
            self.assertIsNone(store.issue(base, bad, model_generation=11))
        for delta in (-1, MAX_SPAN_NS):
            store, base = self.fixture()
            self.mark(store)
            self.assertIsNone(store.issue(base, base.first_sample_ns + delta, model_generation=11))
        for nonce in ('bad', [], None, 'g' * 32):
            store, base = self.fixture()
            store.token = lambda _size: nonce
            self.mark(store)
            self.assertIsNone(store.issue(base, 2_000_000_000, model_generation=11))
        for error in (OSError('private'), ValueError('private')):
            store, base = self.fixture()
            store.token = Mock(side_effect=error)
            self.mark(store)
            self.assertIsNone(store.issue(base, 2_000_000_000, model_generation=11))

    def test_nonce_collision_does_not_replace_another_queued_receipt(self):
        store, base = self.fixture()
        store.token = lambda _: 'a' * 32
        self.mark(store)
        ticket = store.issue(base, 2_000_000_000, model_generation=11)
        self.mark(store, 2_500_000_000)
        self.assertIsNone(
            store.issue(
                replace(base, first_sample_ns=2_000_000_001), 3_000_000_000, model_generation=11
            )
        )
        self.assertTrue(store.consume(ticket))


if __name__ == '__main__':
    unittest.main()
