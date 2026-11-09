"""Run the actual Activity entry without Android permissions, media or a screen."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_interrupted_task_notice import method
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]


class NativeWindowEntryTests(unittest.TestCase):
    def test_window_entry_never_requests_cc8_camera_permission(self):
        source = (
            ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/MainActivity.java'
        ).read_text()
        actual = method(source, 'private void requestImage(')
        harness = r"""class WindowEntryCheck {
 boolean foreground=true;int permissionChecks,permissionRequests;
 final Session session=new Session();final Eyes eyes=new Eyes();
 static class Session{boolean supported;int requests;String mode;
  boolean isVisual(){return false;}boolean supportsVisionModes(){return true;}
  boolean supportsMacWindow(){return supported;}
  void cancelVision(){}boolean requestVision(String value){requests++;mode=value;return true;}}
 static class Eyes{void cancel(){}}
 static class Build{static class VERSION{static final int SDK_INT=27;}}
 static class Manifest{static class permission{static final String CAMERA="camera";}}
 static class PackageManager{static final int PERMISSION_GRANTED=0;}
 static class Toast{static final int LENGTH_LONG=1;static Toast makeText(Object c,String t,int d){return new Toast();}void show(){}}
 int checkSelfPermission(String value){permissionChecks++;return -1;}
 void requestPermissions(String[] values,int code){permissionRequests++;}
 static void yes(boolean v){if(!v)throw new AssertionError();}
 ACTUAL
 public static void main(String[] args){
  WindowEntryCheck c=new WindowEntryCheck();c.session.supported=true;
  c.requestImage("mac_window");yes(c.session.requests==1&&"mac_window".equals(c.session.mode));
  yes(c.permissionChecks==0&&c.permissionRequests==0);
  c.session.supported=false;c.requestImage("mac_window");
  yes(c.session.requests==1&&c.permissionChecks==0&&c.permissionRequests==0);
  c.foreground=false;c.session.supported=true;c.requestImage("mac_window");yes(c.session.requests==1);
 }
}""".replace('ACTUAL', actual)
        java = java_bin()
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'WindowEntryCheck.java'
            file.write_text(harness)
            result = subprocess.run(
                [str(java / 'javac'), '-d', folder, str(file)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [str(java / 'java'), '-cp', folder, 'WindowEntryCheck'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
