"""Bounded resolver occupancy, late results and per-connection deadlines."""

import socket
import subprocess
import sys
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from dns_budget import Resolver, connect
from http_budget import IOBudget


RECORD = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))


class ResolverTests(unittest.TestCase):
    def test_cancelled_lookups_keep_a_bounded_capacity_until_the_os_returns(self):
        pool = Resolver(2)
        release = threading.Event()
        count_lock = threading.Lock()
        entered = [threading.Event(), threading.Event()]
        finished = [threading.Event(), threading.Event()]
        calls = []
        outcomes = []
        budgets = [IOBudget(3), IOBudget(3)]

        def stalled(*args):
            with count_lock:
                index = len(calls)
                calls.append(args)
            entered[index].set()
            try:
                release.wait(4)
                return [RECORD]
            finally:
                finished[index].set()

        def lookup(index):
            try:
                pool.lookup('example.invalid', 443, budgets[index], time.monotonic() + 3)
            except OSError:
                outcomes.append(index)

        workers = [threading.Thread(target=lookup, args=(i,), daemon=True) for i in range(2)]
        with patch('socket.getaddrinfo', side_effect=stalled):
            try:
                for worker in workers:
                    worker.start()
                self.assertTrue(all(event.wait(2) for event in entered))
                for budget in budgets:
                    budget.cancel()
                for worker in workers:
                    worker.join(0.6)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(sorted(outcomes), [0, 1])
                for _ in range(20):
                    with self.assertRaisesRegex(OSError, 'capacity'):
                        pool.lookup('example.invalid', 443, IOBudget(1), time.monotonic() + 1)
                self.assertEqual(len(calls), 2)
            finally:
                release.set()
                self.assertTrue(all(event.wait(2) for event in finished))
                for worker in workers:
                    worker.join(2)
        # Waiting OS calls have relinquished capacity; no poisoned DNS cache.
        with patch('socket.getaddrinfo', return_value=[RECORD]):
            deadline = time.monotonic() + 1
            while True:
                try:
                    result = pool.lookup('example.invalid', 443, IOBudget(1), deadline)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            self.assertEqual(result, [RECORD])

    def test_late_dns_result_cannot_start_a_connection(self):
        release, finished = threading.Event(), threading.Event()
        pool = Resolver(1)

        def stalled(*args):
            try:
                release.wait(2)
                return [RECORD]
            finally:
                finished.set()

        with (
            patch('dns_budget.RESOLVER', pool),
            patch('socket.getaddrinfo', side_effect=stalled),
            patch('socket.socket') as socket_factory,
        ):
            try:
                with self.assertRaises(TimeoutError):
                    connect(('example.invalid', 443), 0.05, IOBudget(1))
                self.assertFalse(release.is_set())
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
            socket_factory.assert_not_called()

    def test_local_bind_hostnames_are_resolved_under_the_same_deadline(self):
        release, finished = threading.Event(), threading.Event()
        peer = Mock()

        def lookup(host, *args):
            if host == 'remote.invalid':
                return [RECORD]
            try:
                release.wait(2)
                return [RECORD]
            finally:
                finished.set()

        with (
            patch('socket.getaddrinfo', side_effect=lookup),
            patch('socket.socket', return_value=peer) as factory,
        ):
            try:
                with self.assertRaises(TimeoutError):
                    connect(
                        ('remote.invalid', 443),
                        0.05,
                        IOBudget(1),
                        source_address=('local.invalid', 0),
                    )
                self.assertFalse(release.is_set())
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
            factory.assert_not_called()

    def test_worker_start_failure_does_not_exhaust_the_pool(self):
        pool = Resolver(1)
        with patch('threading.Thread.start', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                pool.lookup('example.invalid', 443, IOBudget(1), time.monotonic() + 1)
        with patch('socket.getaddrinfo', return_value=[RECORD]):
            self.assertEqual(
                pool.lookup('example.invalid', 443, IOBudget(1), time.monotonic() + 1), [RECORD]
            )

    def test_dns_errors_and_empty_or_oversized_answers_fail_without_a_socket(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror('fixture')):
            with self.assertRaises(socket.gaierror):
                Resolver(1).lookup('example.invalid', 443, IOBudget(1), time.monotonic() + 1)
        for records in ([], [RECORD] * 129):
            with (
                self.subTest(count=len(records)),
                patch('socket.getaddrinfo', return_value=records),
            ):
                with self.assertRaises(OSError):
                    Resolver(1).lookup('example.invalid', 443, IOBudget(1), time.monotonic() + 1)

    def test_unfinished_os_lookup_cannot_hold_process_exit(self):
        program = """
import socket, threading, time
from dns_budget import Resolver
from http_budget import IOBudget
held = threading.Event()
socket.getaddrinfo = lambda *args: held.wait(10)
try:
    Resolver(1).lookup('example.invalid', 443, IOBudget(1), time.monotonic() + 0.05)
except TimeoutError:
    print('bounded')
"""
        result = subprocess.run(
            [sys.executable, '-c', program],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'bounded\n')

    def test_pre_cancelled_lookup_never_invokes_the_os(self):
        budget = IOBudget(1)
        budget.cancel()
        for current, deadline in (
            (budget, time.monotonic() + 1),
            (IOBudget(1), time.monotonic() - 1),
        ):
            with patch('socket.getaddrinfo') as lookup, self.assertRaises(OSError):
                Resolver(1).lookup('example.invalid', 443, current, deadline)
            lookup.assert_not_called()

    def test_all_resolved_addresses_share_one_connection_deadline(self):
        peers = []

        def refused(target):
            time.sleep(0.04)
            raise OSError('fixture refused')

        def make_peer(*args):
            peer = Mock()
            peer.connect.side_effect = refused
            peers.append(peer)
            return peer

        with (
            patch('socket.getaddrinfo', return_value=[RECORD] * 8),
            patch('socket.socket', side_effect=make_peer),
        ):
            began = time.monotonic()
            with self.assertRaises(TimeoutError):
                connect(('example.invalid', 443), 0.07, IOBudget(1))
            self.assertLess(time.monotonic() - began, 0.3)
            self.assertLess(len(peers), 8)
            self.assertGreaterEqual(len(peers), 1)
            for peer in peers:
                peer.close.assert_called_once()

    def test_socket_options_and_failed_address_cleanup_are_preserved(self):
        first, second = Mock(), Mock()
        first.connect.side_effect = OSError('fixture refused')
        with (
            patch('socket.getaddrinfo', return_value=[RECORD, RECORD]),
            patch('socket.socket', side_effect=[first, second]),
        ):
            peer = connect(
                ('[::1]', 443),
                1,
                IOBudget(2),
                socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
            )
        self.assertIs(peer, second)
        first.close.assert_called_once()
        second.close.assert_not_called()
        second.setsockopt.assert_called_once_with(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        second.connect.assert_called_once_with(RECORD[-1])


if __name__ == '__main__':
    unittest.main()
