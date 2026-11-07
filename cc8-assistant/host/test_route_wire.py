"""Actual Java Wire framing interoperates with Python activation envelopes."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from protocol import Packet, MIC, MIC_PAIRED, EVENT, RENDER, SPEAKER
from tools.java_toolchain import java_bin

JAVA = Path(__file__).resolve().parents[1] / 'app/src/main/java/com/happyhome/cc8assistant'
CHECK = r"""
package com.happyhome.cc8assistant;
import java.io.*;import java.util.HexFormat;
public class RouteWireCheck {
 public static void main(String[] args)throws Exception{
  Wire.Packet packet;
  if(args[0].equals("bind")){
   packet=new Wire.Packet();packet.type=Integer.parseInt(args[1]);packet.generation=7;
   packet.sequence=18;packet.frame=320;packet.nanos=1234567890L;
   packet.data=HexFormat.of().parseHex(args[2]);Wire.bindRoute(packet,args[3]);
  }else packet=Wire.read(new DataInputStream(new ByteArrayInputStream(HexFormat.of().parseHex(args[1]))));
  ByteArrayOutputStream buffer=new ByteArrayOutputStream();
  Wire.write(new DataOutputStream(buffer),packet.type,packet.generation,packet.sequence,packet.frame,packet.nanos,packet.data);
  System.out.println(HexFormat.of().formatHex(buffer.toByteArray()));
 }
}
"""


class RouteWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = java_bin()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-route-wire-')
        cls.addClassCleanup(cls.folder.cleanup)
        cls.path = Path(cls.folder.name)
        stub = cls.path / 'org/json/JSONObject.java'
        stub.parent.mkdir(parents=True)
        stub.write_text(
            'package org.json; public class JSONObject { public JSONObject(){} public JSONObject(String raw){} public void put(String key,Object value){} }'
        )
        check = cls.path / 'RouteWireCheck.java'
        check.write_text(CHECK)
        subprocess.run(
            [
                str(cls.java / 'javac'),
                '-d',
                str(cls.path),
                str(stub),
                str(JAVA / 'Wire.java'),
                str(check),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

    def run_java(self, *args):
        return subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(self.path),
                'com.happyhome.cc8assistant.RouteWireCheck',
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_both_directions_preserve_original_packet_fields_and_every_kind(self):
        for kind in (MIC, MIC_PAIRED, RENDER, SPEAKER, EVENT):
            with self.subTest(kind=kind):
                data = b'{"event":"device_ready"}' if kind == EVENT else bytes(range(128)) * 4
                expected = (
                    Packet(kind, 7, 18, 320, 1_234_567_890, data).bind_route('a1' * 16).encode()
                )
                made = self.run_java('bind', str(kind), data.hex(), 'a1' * 16)
                self.assertEqual(made.returncode, 0, made.stderr)
                self.assertEqual(bytes.fromhex(made.stdout.strip()), expected)
                echoed = self.run_java('echo', expected.hex())
                self.assertEqual(echoed.returncode, 0, echoed.stderr)
                self.assertEqual(bytes.fromhex(echoed.stdout.strip()), expected)

    def test_java_rejects_bad_inner_frames_nested_tokens_and_oversized_payloads(self):
        for kind, data, token in (
            (MIC, b'x', 'a' * 32),
            (MIC_PAIRED, b'xx', 'a' * 32),
            (6, bytes(17), 'a' * 32),
            (MIC, bytes(6402), 'a' * 32),
            (MIC, b'xx', 'A' * 32),
            (EVENT, bytes(65536 - 29), 'a' * 32),
        ):
            with self.subTest(kind=kind, size=len(data), token=token):
                self.assertNotEqual(
                    self.run_java('bind', str(kind), data.hex(), token).returncode, 0
                )
