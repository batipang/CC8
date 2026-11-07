"""Production host-epoch ordering, strictly bounded and free of Android/audio I/O."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.java_toolchain import java_bin

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'
CHECK = r"""
package com.happyhome.cc8assistant;
import java.lang.reflect.Field;import java.util.ArrayDeque;
public class HostActivationCheck {
 static void need(boolean ok,String why){if(!ok)throw new AssertionError(why);}
 public static void main(String[] args)throws Exception{
  HostActivation gate=new HostActivation();String owner="e".repeat(32);
  switch(args[0]){
   case "ordered":
    need(gate.accept(owner,1),"first activation rejected");
    need(!gate.accept(owner,1),"duplicate accepted");
    need(gate.accept(owner,3),"newer activation rejected");
    need(!gate.accept(owner,2),"out-of-order activation accepted");break;
   case "restart":
    need(gate.accept(owner,200),"first owner rejected");
    need(gate.accept("f".repeat(32),1),"new process could not restart serial");
    need(!gate.accept(owner,201),"retired process accepted");
    need(gate.accept("f".repeat(32),2),"retired rejection poisoned active process");break;
   case "bounds":
    need(gate.accept(owner,Long.MAX_VALUE),"largest valid serial rejected");
    need(!gate.accept(owner,Long.MAX_VALUE-1),"overflow allowed old serial");
    for(String bad:new String[]{null,"","e".repeat(31),"E".repeat(32)}){
     try{gate.accept(bad,1);throw new AssertionError("bad identity accepted");}catch(IllegalArgumentException expected){}
    }
    for(long bad:new long[]{0,-1,Long.MIN_VALUE}){
     try{gate.accept("f".repeat(32),bad);throw new AssertionError("bad serial accepted");}catch(IllegalArgumentException expected){}
    }
    need(gate.accept("f".repeat(32),1),"invalid request consumed new owner");break;
   case "budget":
    for(int i=1;i<=32;i++)need(gate.accept(String.format("%032x",i),1),"valid restart rejected");
    Field retired=HostActivation.class.getDeclaredField("retired");retired.setAccessible(true);
    need(((ArrayDeque<?>)retired.get(gate)).size()<=16,"retired metadata grew without bound");
    need(!gate.accept(String.format("%032x",31),2),"recent retired owner accepted");break;
   default:throw new AssertionError("unknown case");
  }
 }
}
"""


class HostActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = java_bin()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-host-activation-')
        cls.addClassCleanup(cls.folder.cleanup)
        cls.path = Path(cls.folder.name)
        check = cls.path / 'HostActivationCheck.java'
        check.write_text(CHECK)
        subprocess.run(
            [
                str(cls.java / 'javac'),
                '-d',
                str(cls.path),
                str(JAVA / 'HostActivation.java'),
                str(check),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(self.path),
                'com.happyhome.cc8assistant.HostActivationCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_late_and_duplicate_activation_controls_are_rejected(self):
        self.scenario('ordered')

    def test_new_host_can_restart_and_retired_host_cannot_return(self):
        self.scenario('restart')

    def test_identity_and_serial_bounds_do_not_consume_a_valid_successor(self):
        self.scenario('bounds')

    def test_retired_process_metadata_is_bounded(self):
        self.scenario('budget')
