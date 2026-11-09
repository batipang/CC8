"""Compile the actual native metadata/clock gate without Android media or sockets."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'


class NativePhotoWindowTests(unittest.TestCase):
    def test_actual_route_generation_expiry_no_renewal_and_question_validation(self):
        harness = r"""package com.happyhome.cc8assistant;
public class PhotoWindowCheck {
 static void yes(boolean value){if(!value)throw new AssertionError();}
 public static void main(String[] args){
 PhotoFollowupWindow window=new PhotoFollowupWindow();
 String a="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",b="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
 yes(window.current(4,7,1000)==null);
 yes(window.offer(a,7,1280,960,120000,4,1000));
 PhotoFollowupWindow.Picture picture=window.current(4,7,1000);
 yes(picture!=null&&picture.id.equals(a)&&picture.until==121000);
 yes(window.current(5,7,1001)==null&&window.current(4,8,1001)==null);
 yes(window.current(4,7,120999)==picture&&window.current(4,7,121000)==null);
 yes(!window.offer(a,7,1280,960,120000,4,121001));
 window.clear();yes(!window.offer(b,7,1280,960,120000,4,121002));
 yes(window.offer(b,8,401,320,50000,4,130000));
 yes(!window.expire(a,7,4)&&!window.expire(b,8,5));
 yes(window.current(4,8,130001)!=null);
 for(int ttl:new int[]{0,-1,120001})yes(!window.offer(a,9,640,480,ttl,4,130001));
 yes(!window.offer(a,9,1280,962,5000,4,130001));
 yes(!window.offer(a,9,640,480,5000,4,Long.MAX_VALUE-4999));
 yes(!window.offer("bad",9,640,480,5000,4,130001));
 yes(window.current(4,8,130002)!=null);
 yes(window.expire(b,8,4)&&window.current(4,8,130003)==null);
 yes(window.offer(a,0,640,480,5000,5,140000));
 yes(!window.expire(a,7,4)&&window.current(5,0,140001)!=null);
 yes(PhotoFollowupWindow.validQuestion("这张照片写了什么？"));
 yes(PhotoFollowupWindow.validQuestion("emoji "+new String(Character.toChars(0x1f680))+"\n第二行"));
 for(String value:new String[]{null,""," \t\n","　　", Character.toString((char)0x85),
    Character.toString((char)0xd800),Character.toString((char)0xdc00),"a"+(char)1+"b"})
  yes(!PhotoFollowupWindow.validQuestion(value));
 yes(PhotoFollowupWindow.validQuestion(new String(new char[1000]).replace((char)0,'x')));
 yes(!PhotoFollowupWindow.validQuestion(new String(new char[1001]).replace((char)0,'x')));
 }
}"""
        java = java_bin()
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            source = work / 'PhotoWindowCheck.java'
            source.write_text(harness)
            result = subprocess.run(
                [
                    str(java / 'javac'),
                    '-d',
                    str(work),
                    str(JAVA / 'PhotoFollowupWindow.java'),
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [
                    str(java / 'java'),
                    '-cp',
                    str(work),
                    'com.happyhome.cc8assistant.PhotoWindowCheck',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
