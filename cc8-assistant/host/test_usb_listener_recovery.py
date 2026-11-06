"""Exercise the actual Android USB accept loop with real loopback sockets."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_interrupted_task_notice import method
from tools.java_toolchain import require_java

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/VoiceSession.java'

HARNESS = r"""
import java.io.IOException;
import java.net.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

public final class UsbListenerCheck {
    final Object routeLock = new Object();
    final CountDownLatch firstFailure = new CountDownLatch(1);
    final CountDownLatch connected = new CountDownLatch(1);
    volatile boolean foreground = true;
    volatile int lifecycle = 1;
    volatile ServerSocket server;
    volatile Link usbLink;
    volatile Link activeLink;
    final int port;
    final Listener listener = new Listener();

    UsbListenerCheck(int port) { this.port = port; }

    final class Listener {
        void state(String phase, String message) { firstFailure.countDown(); }
    }

    final class Link implements Runnable {
        final Socket socket;
        volatile boolean closed;
        Link(Socket socket, String route, int ticket) { this.socket = socket; }
        public void run() {
            try (Socket owned = socket) {
                connected.countDown();
            } catch (IOException error) {
                throw new AssertionError(error);
            } finally {
                closed = true;
            }
        }
    }

    /*ACTUAL_ACCEPT*/

    void finish() throws IOException {
        synchronized (routeLock) {
            foreground = false;
            lifecycle++;
            if (server != null) server.close();
            server = null;
        }
    }

    public static void main(String[] args) throws Exception {
        InetAddress loopback = InetAddress.getByName("127.0.0.1");
        try (ServerSocket conflict = new ServerSocket(0, 1, loopback)) {
            UsbListenerCheck check = new UsbListenerCheck(conflict.getLocalPort());
            Thread worker = new Thread(() -> check.accept(1));
            worker.start();
            try {
                if (!check.firstFailure.await(2, TimeUnit.SECONDS)) {
                    throw new AssertionError("occupied port did not trigger the bind failure");
                }
                if (args[0].equals("pause")) check.finish();
                conflict.close();
                if (args[0].equals("recover")) {
                    long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(4);
                    while (System.nanoTime() < deadline) {
                        try (Socket client = new Socket(loopback, check.port)) {
                            if (!check.connected.await(2, TimeUnit.SECONDS)) {
                                throw new AssertionError("listener did not hand off the accepted socket");
                            }
                            return;
                        } catch (ConnectException unavailable) {
                            Thread.sleep(20);
                        }
                    }
                    throw new AssertionError("listener never recovered after the port became free");
                }
                worker.join(2500);
                if (worker.isAlive() || check.server != null) {
                    throw new AssertionError("obsolete foreground ticket kept a listener");
                }
            } finally {
                check.finish();
                worker.join(2500);
                if (worker.isAlive()) throw new AssertionError("listener did not exit");
            }
        }
    }
}
"""


class UsbListenerRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = require_java()
        cls.temp = tempfile.TemporaryDirectory(prefix='cc8-usb-listener-')
        cls.addClassCleanup(cls.temp.cleanup)
        accept = method(SOURCE.read_text(), 'private void accept(int ticket)')
        assert accept.count('18765') == 1
        generated = Path(cls.temp.name) / 'UsbListenerCheck.java'
        generated.write_text(HARNESS.replace('/*ACTUAL_ACCEPT*/', accept.replace('18765', 'port')))
        subprocess.run([cls.java / 'javac', generated], check=True, timeout=15)

    def scenario(self, name):
        result = subprocess.run(
            [self.java / 'java', '-cp', self.temp.name, 'UsbListenerCheck', name],
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recovers_after_a_real_bind_conflict_clears(self):
        self.scenario('recover')

    def test_paused_ticket_cannot_reopen_a_listener(self):
        self.scenario('pause')
