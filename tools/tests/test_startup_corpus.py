"""Cross-case replay must retain the same real controller's generation/history."""

import socket
import subprocess
import unittest
from unittest.mock import patch

from tools import qa_faults


class StartupCorpusTests(unittest.TestCase):
    def test_complete_corpus_is_one_monotonic_session_with_preserved_results(self):
        # Load Bridge through the same host import path as the QA generator.
        import sys

        sys.path.insert(0, str(qa_faults.ROOT / 'cc8-assistant/host'))
        import bridge

        original = bridge.Bridge
        instances = []

        def create(*args, **kwargs):
            instance = original(*args, **kwargs)
            instances.append(instance)
            return instance

        with (
            patch.object(bridge, 'Bridge', side_effect=create),
            patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')),
            patch.object(subprocess, 'Popen', side_effect=AssertionError('process forbidden')),
        ):
            rows = qa_faults.generate(cases=qa_faults.STARTUP_CASES)
        self.assertEqual(len(rows), 21)
        previous = -1
        for row in rows:
            with self.subTest(case=row['case'].ident):
                packets = row['error'] + row['recovery']
                self.assertGreater(min(p.generation for p in packets), previous)
                self.assertGreater(
                    min(p.generation for p in row['recovery']),
                    max(p.generation for p in row['error']),
                )
                previous = max(p.generation for p in packets)
                self.assertEqual(row['explicit_recovery_dispatches'], 1)
        self.assertEqual(len(instances), 1)
        host = instances[0]
        self.assertEqual(host.brain.calls, len(rows))
        tasks = host.workspace.snapshot()['tasks']
        self.assertGreater(len(tasks), 1)
        self.assertTrue(all(t['status'] == 'completed' for t in tasks))
        self.assertIsNone(host.session.text_generation)
        self.assertIsNone(host.announcements.generation)
        self.assertFalse(host.session.active)
        self.assertTrue(host.response.queue.empty())
