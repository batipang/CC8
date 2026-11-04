"""A pinned background fixture never opens the live audio output in offline tests."""

import hashlib
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

import soak_background
from rpc_process import _group_live
from soak_background import Background, RATE


class BackgroundTests(unittest.TestCase):
    def track(self, path, *, frames=RATE, sample=b'\x01\x00', rate=RATE):
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, rate, frames, 'NONE', ''))
            wav.writeframes(sample * frames)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_real_guardian_stops_player_and_removes_snapshot_when_owner_dies(self):
        source = Path(soak_background.__file__).resolve()
        for kill_owner in (False, True):
            with self.subTest(kill_owner=kill_owner), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                track = root / 'fixture.wav'
                pin = self.track(track)
                player = root / 'player'
                player.write_text(
                    '#!/bin/sh\n'
                    'printf "%s\n%s\n" "$$" "$3" > "$CC8_PLAYER_RECEIPT.new"\n'
                    '/bin/mv "$CC8_PLAYER_RECEIPT.new" "$CC8_PLAYER_RECEIPT"\n'
                    'exec /bin/sleep 60\n'
                )
                player.chmod(0o700)
                replacement = source.read_text().replace(
                    "PLAYER = '/usr/bin/afplay'", f'PLAYER = {str(player)!r}'
                )
                (root / 'soak_background.py').write_text(replacement)
                receipt = root / 'player.txt'
                command = f"""
import hashlib, sys
from pathlib import Path
sys.path[:0] = [{str(root)!r}, {str(source.parent)!r}]
from soak_background import Background
track = Path({str(track)!r})
data = track.read_bytes()
background = Background('tv', hashlib.sha256(data).hexdigest(), data)
track.unlink()
with background.play() as active:
    print(active(), flush=True)
    sys.stdin.buffer.readline()
"""
                group = None
                with subprocess.Popen(
                    [sys.executable, '-I', '-S', '-c', command],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, 'TMPDIR': folder, 'CC8_PLAYER_RECEIPT': str(receipt)},
                ) as owner:
                    try:
                        self.assertTrue(select.select([owner.stdout], [], [], 8)[0])
                        self.assertEqual(owner.stdout.readline(), b'True\n')
                        deadline = time.monotonic() + 5
                        while not receipt.exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        ident, path = receipt.read_text().splitlines()
                        staged = Path(path)
                        group = os.getpgid(int(ident))
                        self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest(), pin)
                        self.assertFalse(track.exists())
                        if kill_owner:
                            owner.kill()
                        else:
                            owner.stdin.write(b'\n')
                            owner.stdin.flush()
                        self.assertEqual(owner.wait(timeout=8), -9 if kill_owner else 0)
                        deadline = time.monotonic() + 6
                        while (
                            staged.exists() or _group_live(group, 0.2)
                        ) and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertFalse(staged.parent.exists())
                        self.assertFalse(_group_live(group, 0.2))
                    finally:
                        if owner.poll() is None:
                            owner.kill()
                            owner.wait(timeout=2)
                        if group is not None and _group_live(group, 0.2):
                            os.killpg(group, 9)

    @patch('soak_background.PLAYBACK_SECONDS', 1)
    def test_rejects_wrong_pin_invalid_wav_short_silent_or_linked_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            track = Path(folder) / 'fixture.wav'
            pin = self.track(track)
            with self.assertRaisesRegex(ValueError, 'pinned digest'):
                Background.load(track, 'low-noise', '0' * 64)
            alias = track.with_name('alias.wav')
            alias.symlink_to(track)
            with self.assertRaises(OSError):
                Background.load(alias, 'low-noise', pin)
            for arguments, message in (
                ({'frames': RATE - 1}, 'long'),
                ({'sample': b'\x00\x00'}, 'silent'),
                ({'rate': 8000}, '16 kHz'),
            ):
                with self.subTest(arguments=arguments):
                    pin = self.track(track, **arguments)
                    with self.assertRaisesRegex(ValueError, message):
                        Background.load(track, 'low-noise', pin)
            track.write_bytes(b'bad wave')
            with self.assertRaisesRegex(ValueError, 'Invalid background WAV'):
                Background.load(track, 'low-noise', hashlib.sha256(track.read_bytes()).hexdigest())
