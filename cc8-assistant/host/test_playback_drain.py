"""Exercise actual Java drain/pump against a streaming tail that needs priming."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.java_toolchain import require_java  # noqa: E402 - repository test entry


class PlaybackDrainTests(unittest.TestCase):
    def test_real_drain_preserves_speech_endpoint_timeout_pause_and_cancel(self):
        java = require_java()
        source = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
        program = r"""
package com.happyhome.cc8assistant;
import java.io.IOException;
public class DrainTest {
 static class Track implements PlaybackDrain.Sink {
  long time=0, head=20992, speech=22849, physical=speech;
  int buffer=4096, pads=0, reports=0, acceptedSpeech=0;
  long resumes=1;
  boolean alive=true, stalled=true, cancelOnWrite=false;
  public long now(){return time;}
  public long head(){return head;}
  public int bufferFrames(){return buffer;}
  public long resumeSerial(){return resumes;}
  public boolean allowed(){return alive;}
  public boolean paused(){return false;}
  public void awaitReady(){}
  public void probe(){reports++;}
  public void waitTick(long n){time+=n;if(!stalled)head=Math.min(physical,head+160);}
  public int write(short[] a,int o,int n){
   for(int i=o;i<o+n;i++)if(a[i]!=0)throw new AssertionError("audible padding");
   int k=Math.min(n,113);physical+=k;pads+=k;
   if(physical-head>=buffer)stalled=false;
   if(cancelOnWrite)alive=false;
   return k;
  }
  public void accepted(short[] a,int o,int n){acceptedSpeech+=n;}
 }
 static void yes(boolean v){if(!v)throw new AssertionError();}
 public static void main(String[] args)throws Exception{
  Track tail=new Track();PlaybackDrain drain=new PlaybackDrain();
  yes(drain.drain(tail.speech,tail));
  yes(tail.head>=tail.speech && tail.head<tail.physical);
  yes(tail.speech==22849 && tail.acceptedSpeech==0);
  yes(drain.paddingFrames()==2239 && tail.pads==2239 && tail.reports>0);
  Track normal=new Track();normal.resumes=0;normal.stalled=false;
  PlaybackDrain noPadding=new PlaybackDrain();yes(noPadding.drain(normal.speech,normal));yes(noPadding.paddingFrames()==0);
  Track underrun=new Track();underrun.resumes=0;
  PlaybackDrain recovered=new PlaybackDrain();yes(recovered.drain(underrun.speech,underrun));
  yes(underrun.time>=100 && underrun.time<3000 && underrun.head>=underrun.speech);
  yes(recovered.paddingFrames()==2239 && underrun.pads==2239 && underrun.acceptedSpeech==0);
  Track stuck=new Track(){public int write(short[] a,int o,int n){int k=super.write(a,o,n);stalled=true;return k;}};stuck.resumes=0;
  try{new PlaybackDrain().drain(stuck.speech,stuck);throw new AssertionError("fake complete");}catch(IOException expected){yes(stuck.head<stuck.speech && stuck.time>=3000);}
  yes(stuck.pads>0 && stuck.pads<=stuck.buffer);
  Track cancelledTail=new Track();cancelledTail.resumes=0;cancelledTail.cancelOnWrite=true;
  yes(!new PlaybackDrain().drain(cancelledTail.speech,cancelledTail));yes(cancelledTail.head<cancelledTail.speech && cancelledTail.time>=100);
  Track cancelled=new Track();cancelled.cancelOnWrite=true;
  yes(!new PlaybackDrain().drain(cancelled.speech,cancelled));yes(cancelled.head<cancelled.speech);
  Track paused=new Track(){boolean first=true;public void awaitReady(){if(first){time+=5000;first=false;}}};
  yes(new PlaybackDrain().drain(paused.speech,paused));yes(paused.time>=5000 && paused.time<8000);
  Track bad=new Track();bad.buffer=32001;
  try{new PlaybackDrain().drain(bad.speech,bad);throw new AssertionError();}catch(IOException expected){yes(bad.pads==0);}
  Track twice=new Track(){boolean again=true;public void waitTick(long n){super.waitTick(n);if(again){again=false;time+=2000;stalled=true;resumes++;}}};
  PlaybackDrain repeated=new PlaybackDrain();yes(repeated.drain(twice.speech,twice));
  yes(repeated.paddingFrames()==2399 && twice.head>=twice.speech);
  Track empty=new Track();empty.speech=empty.head;
  yes(new PlaybackDrain().drain(empty.speech,empty));yes(empty.pads==0);
  System.out.println("drain tail/short-write/timeout/cancel/pause/limit/endpoint passed");
 }
}
"""
        with tempfile.TemporaryDirectory(prefix='cc8-drain-') as folder:
            test = Path(folder) / 'DrainTest.java'
            test.write_text(program)
            subprocess.run(
                [
                    str(java / 'javac'),
                    '-encoding',
                    'UTF-8',
                    '-d',
                    folder,
                    str(source / 'PcmPump.java'),
                    str(source / 'PlaybackDrain.java'),
                    str(test),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            result = subprocess.run(
                [str(java / 'java'), '-cp', folder, 'com.happyhome.cc8assistant.DrainTest'],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('endpoint passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
