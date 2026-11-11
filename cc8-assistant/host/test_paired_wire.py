"""Real Java/Python packet interoperability for negotiated PCM pairs."""

import base64
from pathlib import Path
import subprocess
import tempfile
import unittest
from protocol import Packet, MIC_PAIRED, MIC, receive
from test_runtime import Fragmented
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
CHECK = r"""
package com.happyhome.cc8assistant;
import java.io.*;import java.util.*;
public class PairedWireCheck {
 public static void main(String[] args)throws Exception{
  String mode=args[0];
  if(mode.equals("roundtrip")){
   byte[] raw=Base64.getDecoder().decode(args[1]);DataInputStream in=new DataInputStream(new ByteArrayInputStream(raw));
   Wire.Packet p=Wire.read(in);if(in.available()!=0)throw new AssertionError("trailing wire");
   ByteArrayOutputStream bytes=new ByteArrayOutputStream();
   Wire.write(new DataOutputStream(bytes),p.type,p.generation,p.sequence,p.frame,p.nanos,p.data);
   System.out.println(Base64.getEncoder().encodeToString(bytes.toByteArray()));return;
  }
  int size=Integer.parseInt(args[1]);boolean failed=false;
  try{
   if(mode.equals("write"))Wire.write(new DataOutputStream(new ByteArrayOutputStream()),Wire.MIC_PAIRED,1,2,3,4,new byte[size]);
   else if(mode.equals("null"))Wire.write(new DataOutputStream(new ByteArrayOutputStream()),Wire.MIC_PAIRED,1,2,3,4,null);
   else {
    ByteArrayOutputStream bytes=new ByteArrayOutputStream();DataOutputStream out=new DataOutputStream(bytes);
    out.writeInt(Wire.HEADER+size);out.writeByte(Wire.MIC_PAIRED);out.writeInt(1);out.writeLong(2);out.writeLong(3);out.writeLong(4);out.write(new byte[size]);
    Wire.read(new DataInputStream(new ByteArrayInputStream(bytes.toByteArray())));
   }
  }catch(IOException e){failed=true;}
  if(!failed)throw new AssertionError("malformed pair accepted");System.out.println("REJECTED");
 }
}
"""


class PairedWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = java_bin()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-paired-wire-')
        root = Path(cls.folder.name)
        try:
            stub = root / 'JSONObject.java'
            stub.write_text(
                'package org.json;public class JSONObject {public JSONObject(){} public JSONObject(String raw){} public void put(String key,Object value){} public String toString(){return "{}";}}'
            )
            check = root / 'PairedWireCheck.java'
            check.write_text(CHECK)
            cls.classes = root / 'classes'
            cls.classes.mkdir()
            result = subprocess.run(
                [
                    str(cls.java / 'javac'),
                    '-d',
                    str(cls.classes),
                    str(JAVA / 'Wire.java'),
                    str(stub),
                    str(check),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode:
                raise AssertionError(result.stderr[:4000])
        except BaseException:
            cls.folder.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def run_java(self, *args):
        return subprocess.check_output(
            [
                str(self.java / 'java'),
                '-cp',
                str(self.classes),
                'com.happyhome.cc8assistant.PairedWireCheck',
                *args,
            ],
            text=True,
            stderr=subprocess.PIPE,
            timeout=10,
        ).strip()

    def test_actual_java_codec_preserves_paired_and_legacy_header_bits(self):
        for packet in [
            Packet(MIC_PAIRED, 9, 11, 320, 1_000_000_000, b'\xff\x7f\x00\x80' * 320),
            Packet(MIC_PAIRED, 2**32 - 1, 2**64 - 1, 2**64 - 1, 2**64 - 1, bytes(6400)),
            Packet(MIC, 7, 1, 320, 20_000_000, bytes(640)),
        ]:
            with self.subTest(kind=packet.kind, bytes=len(packet.data)):
                raw = packet.encode()
                result = base64.b64decode(
                    self.run_java('roundtrip', base64.b64encode(raw).decode()), validate=True
                )
                self.assertEqual(result, raw)
                self.assertEqual(receive(Fragmented(result)), packet)

    def test_actual_java_reader_and_writer_reject_half_pairs_and_oversize(self):
        for mode in ('read', 'write'):
            for count in (0, 1, 2, 3, 6, 6404):
                with self.subTest(mode=mode, count=count):
                    self.assertEqual(self.run_java(mode, str(count)), 'REJECTED')
        self.assertEqual(self.run_java('null', '0'), 'REJECTED')


if __name__ == '__main__':
    unittest.main()
