"""Exercise actual Android-free transport teardown under a UI-like caller."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]


class TransportCloseTests(unittest.TestCase):
    def test_tls_style_close_is_async_and_failure_does_not_reach_caller(self):
        java = java_bin()
        source = (
            ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/TransportClose.java'
        )
        harness = """package com.happyhome.cc8assistant;
import java.net.Socket;import java.util.concurrent.*;
public final class CloseCheck {
 public static void main(String[] args)throws Exception{
  Thread ui=Thread.currentThread();CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1),done=new CountDownLatch(1);
  Socket fake=new Socket(){public void close(){
   if(Thread.currentThread()==ui)throw new AssertionError("network on UI");
   entered.countDown();try{release.await(2,TimeUnit.SECONDS);}catch(InterruptedException e){throw new AssertionError(e);}finally{done.countDown();}
   throw new IllegalStateException("simulated TLS close failure");
  }};
  TransportClose.asynchronously(fake);
  if(!entered.await(1,TimeUnit.SECONDS))throw new AssertionError("close did not run");
  if(done.getCount()!=1)throw new AssertionError("expected blocked close");
  release.countDown();if(!done.await(1,TimeUnit.SECONDS))throw new AssertionError("close did not end");
 }
}"""
        with tempfile.TemporaryDirectory() as folder:
            test = Path(folder) / 'CloseCheck.java'
            test.write_text(harness)
            subprocess.run(
                [str(java / 'javac'), '-d', folder, str(source), str(test)],
                check=True,
                capture_output=True,
                timeout=30,
            )
            result = subprocess.run(
                [str(java / 'java'), '-cp', folder, 'com.happyhome.cc8assistant.CloseCheck'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Exception in thread', result.stderr)
