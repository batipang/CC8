"""Service lifecycle boundaries; never calls launchctl or loads real voice models."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from process_lock import ProcessLease, atomic_private_json
import service


class ServiceTests(unittest.TestCase):
    def test_ten_process_crashes_release_lease_without_stale_pid_ownership(self):
        code='import sys,time;from process_lock import ProcessLease\nwith ProcessLease(sys.argv[1]):\n print("locked",flush=True);time.sleep(20)'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bridge.lock'
            for _ in range(10):
                child=subprocess.Popen([sys.executable,'-c',code,str(path)],cwd=Path(__file__).parent,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(),'locked')
                    with self.assertRaises(BlockingIOError):
                        with ProcessLease(path):pass
                    child.send_signal(signal.SIGKILL);child.wait(timeout=3)
                    with ProcessLease(path):pass
                    self.assertEqual(path.stat().st_mode&0o777,0o600)
                finally:
                    if child.poll() is None:child.kill();child.wait()
                    child.stdout.close();child.stderr.close()

    def test_lease_rejects_symlink_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'keep';target.write_text('original')
            path=Path(folder)/'lease';path.symlink_to(target)
            with self.assertRaises(OSError):
                with ProcessLease(path):pass
            self.assertEqual(target.read_text(),'original')

    def test_private_marker_and_corruption_default_to_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'enabled.json';self.assertFalse(service.enabled(path))
            atomic_private_json(path,{'enabled':True});self.assertTrue(service.enabled(path))
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            atomic_private_json(path,{'enabled':False});self.assertFalse(service.enabled(path))
            path.write_text('broken');self.assertFalse(service.enabled(path))
            self.assertEqual(list(Path(folder).glob('.enabled.json-*')),[])

    def test_agent_only_runs_supervisor_and_has_crash_throttle(self):
        value=service.agent_payload()
        self.assertEqual(value['ProgramArguments'][-1],'run')
        self.assertNotIn('launch.py',' '.join(value['ProgramArguments']))
        self.assertEqual(value['KeepAlive'],{'SuccessfulExit':False})
        self.assertGreaterEqual(value['ThrottleInterval'],10)
        self.assertEqual(set(value['EnvironmentVariables']),{'PATH'})
        self.assertIn('/opt/homebrew/bin',value['EnvironmentVariables']['PATH'])

    def test_backoff_is_bounded_and_not_a_tight_restart_loop(self):
        self.assertEqual([service.retry_delay(n) for n in range(1,8)],[1,2,4,8,16,30,30])

    def test_ten_worker_failures_restart_with_backoff_and_no_request_replay(self):
        count=[0];waits=[]
        class Stop:
            def is_set(self):return count[0]>=10
            def wait(self,seconds):waits.append(seconds);return self.is_set()
        class Child:
            def poll(self):return 1
            def wait(self,timeout):return 1
        with tempfile.TemporaryDirectory() as folder,patch.object(service,'PRIVATE',Path(folder)),patch.object(service,'enabled',return_value=True),patch.object(service,'read_pid',return_value=None),patch.object(service,'own_process',return_value=False):
            supervisor=service.Supervisor();supervisor.stop=Stop()
            def spawn():count[0]+=1;supervisor.child=Child();supervisor.pid=100+count[0]
            with patch.object(supervisor,'start_child',side_effect=spawn) as start:supervisor.run()
            self.assertEqual(start.call_count,10)
            self.assertIn(30,waits);self.assertNotIn(0,waits)
            self.assertEqual(json.loads((Path(folder)/'supervisor.json').read_text())['phase'],'stopped')

    def test_disabled_supervisor_never_spawns_worker(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(service,'PRIVATE',Path(folder)),patch.object(service,'enabled',return_value=False):
            supervisor=service.Supervisor()
            with patch.object(supervisor,'start_child') as spawn:supervisor.run()
            spawn.assert_not_called()
            self.assertEqual(json.loads((Path(folder)/'supervisor.json').read_text())['phase'],'stopped')

    def test_log_rotation_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(service,'PRIVATE',Path(folder)):
            supervisor=service.Supervisor();handler=supervisor.log.handlers[0];handler.maxBytes=100
            try:
                for _ in range(20):supervisor.log.info('x'*80)
                self.assertLessEqual(len(list(Path(folder).glob('service.log*'))),4)
            finally:handler.close()

    def test_clear_stop_is_written_before_launchctl_bootout(self):
        with tempfile.TemporaryDirectory() as folder:
            marker=Path(folder)/'enabled.json';plist=Path(folder)/'agent.plist';plist.touch()
            def run(*args,**kwargs):
                self.assertFalse(json.loads(marker.read_text())['enabled'])
                return subprocess.CompletedProcess(args,0)
            with patch.object(service,'ENABLED',marker),patch.object(service,'PLIST',plist),patch.object(service.subprocess,'run',side_effect=run):service.disable()
            self.assertEqual(marker.stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
