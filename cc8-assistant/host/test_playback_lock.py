"""A paused completion and owner cancellation must not invert session/done locks."""

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]

REPRODUCTION = textwrap.dedent("""
    import sys, threading, time
    from pathlib import Path
    sys.path.insert(0, str(Path(sys.argv[1]) / 'cc8-assistant/host'))
    import numpy as np
    from bridge import Bridge

    bridge = Bridge(None, None)
    bridge.session.active = True
    bridge.session.connection = object()
    bridge.send = lambda packet: None
    bridge.log = lambda *args, **fields: None
    owner = getattr(bridge, 'playback', bridge)
    pause_owner = getattr(owner, 'pause', owner)
    generation = bridge.session.generation
    ready = threading.Event()
    deadline_read = threading.Event()
    proceed = threading.Event()
    errors = []
    results = []

    class Paused(dict):
        def __getitem__(self, key):
            value = super().__getitem__(key)
            if key == 'started':
                # The renderer has captured the pause and committed to checking
                # its expiry. Cancel now, while its completion path is active.
                deadline_read.set()
                if not proceed.wait(3):
                    raise RuntimeError('deadline barrier not reached')
            return value

    def event(name, **fields):
        if name == 'play_end':
            pause_owner.pause_state = Paused(token=1, generation=generation,
                                       started=time.monotonic() - 21)
    bridge.event = event

    def control():
        # Owner requests such as a snapshot hold this outer reentrant guard
        # while cancelling the preceding audio generation.
        with bridge.session.guard:
            ready.set()
            if not deadline_read.wait(3):
                raise RuntimeError('renderer did not reach completion deadline')
            proceed.set()
            bridge.cancel('owner_request_cancel')

    def render():
        if not ready.wait(3):
            raise RuntimeError('control did not acquire guard')
        result = bridge.play_stream(iter([np.zeros(3200, dtype=np.int16)]),
                                    'synthetic', generation)
        results.append(result)

    def checked(function):
        try:
            function()
        except BaseException as error:
            errors.append(type(error).__name__)

    workers = [threading.Thread(target=checked, args=(function,), daemon=True)
               for function in (control, render)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
    assert not any(worker.is_alive() for worker in workers), 'session/done lock cycle'
    assert not errors, errors
    assert results == [False], results
    assert bridge.session.generation == generation + 1, 'expired output cancelled the new generation'
    assert owner.playing is False
    print('completed without deadlock or stale cancellation')
""")


class PlaybackLockTests(unittest.TestCase):
    def test_paused_completion_and_owner_cancel_finish_without_stale_cancellation(self):
        # A regression remains bounded even if both threads deadlock. The child
        # imports providers but never instantiates one or accesses a device.
        result = subprocess.run(
            [sys.executable, '-c', REPRODUCTION, str(ROOT)],
            cwd=ROOT,
            env={'PATH': '/usr/bin:/bin'},
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('completed without deadlock or stale cancellation', result.stdout)


if __name__ == '__main__':
    unittest.main()
