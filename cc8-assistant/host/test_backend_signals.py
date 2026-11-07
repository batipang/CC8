"""Actual signals inside an owned local USB writer; no device/provider calls."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

HOST = Path(__file__).resolve().parent
CHILD = r"""
import io,json,os,signal,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,sys.argv[1])
from app_runtime import BackendDependencies,run_backend
from transport_channel import Channel
from usb_transport import USBPipe
import usb_transport
pipe=USBPipe([sys.executable,'-I','-S','-c','import sys; sys.stdin.buffer.read()'])
channel=Channel(pipe,'usb',__import__('queue').Queue())
b,hub,server,speech=Mock(),Mock(),Mock(),Mock()
b.session.connection=channel
b.session.active=True
b.calibration.active=b.calibration.running=b.playback.playing=False
b.speech=speech
b.session.deactivate.side_effect=lambda:setattr(b.session,'active',False)
b.session.unbind.side_effect=lambda:setattr(b.session,'connection',None)
b.cancel.side_effect=lambda reason:pipe.sendall(b'cancel')
hub.active=channel
hub.preferred.return_value=channel
hub.role_uncertain=False
hub.close.side_effect=channel.close
raised=False
real_write=os.write
kind=getattr(signal,sys.argv[3])
def interrupt(fd,data):
 global raised
 if not raised:
  raised=True
  os.kill(os.getpid(),kind)
 return real_write(fd,data)
def event(**kwargs):
 with patch.object(usb_transport.os,'write',side_effect=interrupt):
  pipe.sendall(b'first')
 if kind==signal.SIGUSR1:
  os.kill(os.getpid(),signal.SIGTERM)
 return 'packet',channel,object()
hub.events.get.side_effect=event
deps=BackendDependencies(private=Path(sys.argv[2]),description='fixture',
 load_config=Mock(return_value=SimpleNamespace(brain='dot',tls_port=18766,
  wake_window_seconds=60,chinese_wake_verifier='off')),
 direct_speech=Mock(return_value=speech),local_speech=Mock(),fast_stop=Mock(),
 wake_detector=Mock(),brain_router=Mock(),minimax_brain=Mock(),bridge=Mock(return_value=b),
 workspace=Mock(),transport_hub=Mock(return_value=hub),pairing=Mock(),
 connect_device=Mock(),usb_present=Mock(),announcement_server=Mock(return_value=server))
try:
 with patch.object(sys,'argv',['bridge.py','--no-fast-stop']),patch('threading.Thread.start'),patch('sys.stdout',new_callable=io.StringIO):
  run_backend(deps)
 print(json.dumps({'closed':channel.closed and pipe.child.poll() is not None,
  'packet_dispatched':b.on_packet.call_count,'retry_started':b.calibration.prepare_retry.call_count,
  'cancel_calls':b.cancel.call_count,'signal_sent_in_real_write':raised}),flush=True)
finally:
 channel.close()
"""


class BackendSignalTests(unittest.TestCase):
    def check_signal(self, name):
        with tempfile.TemporaryDirectory() as folder:
            process = subprocess.Popen(
                [sys.executable, '-I', '-u', '-c', CHILD, str(HOST), folder, name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env={'PATH': '/usr/bin:/bin'},
            )
            try:
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    self.fail('Registered signal handler reentered the held USB writer')
                self.assertEqual(process.returncode, 0, stderr.decode())
                result = json.loads(stdout)
                self.assertTrue(result['signal_sent_in_real_write'] and result['closed'])
                self.assertEqual(result['packet_dispatched'], 0)
                self.assertEqual(result['retry_started'], 0)
                self.assertEqual(result['cancel_calls'], 1)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=3)

    def test_sigterm_during_usb_write_exits_without_dispatching_the_queued_packet(self):
        self.check_signal('SIGTERM')

    def test_sigint_during_usb_write_uses_the_same_safe_stop_boundary(self):
        self.check_signal('SIGINT')

    def test_stop_dominates_a_selfcheck_requested_during_usb_write(self):
        self.check_signal('SIGUSR1')


if __name__ == '__main__':
    unittest.main()
