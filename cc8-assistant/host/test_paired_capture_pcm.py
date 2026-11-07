"""Compile the real causal converter and exercise signal, split-read and epoch boundaries."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/PairedCapturePcm.java'
CHECK = r"""
package com.happyhome.cc8assistant;
import java.util.*;
import java.io.*;
public class PairedPcmCheck {
 static void need(boolean value,String reason){if(!value)throw new AssertionError(reason);}
 static short[] input(int frames,double hz){
  short[] x=new short[frames*4];
  for(int i=0;i<frames;i++){
   x[i*4]=(short)Math.rint(16000*Math.sin(2*Math.PI*hz*i/48000));
   x[i*4+1]=(short)(i*37); x[i*4+2]=x[i*4];x[i*4+3]=(short)~x[i*4+1];
  }return x;
 }
 static double rms(short[] data,int channel){
  double power=0;int count=0;
  for(int i=3200+channel;i<data.length;i+=2){power+=(double)data[i]*data[i];count++;}
  return Math.sqrt(power/count);
 }
 static void channel(){
  short[] x=input(4800,1000),a=new PairedCapturePcm().push(x,0,x.length);
  for(int i=0;i<a.length;i+=2)need(a[i]==a[i+1],"Equal selected inputs diverged");
  for(int i=0;i<x.length;i+=4){x[i+1]^=32767;x[i+3]^=32767;}
  need(Arrays.equals(a,new PairedCapturePcm().push(x,0,x.length)),"Unused channel leaked");
  need(a.length==3200,"Wrong logical frame count");
 }
 static void split(){
  short[] x=new short[19201];Random random=new Random(19);
  for(int i=0;i<x.length;i++)x[i]=(short)random.nextInt();
  short[] expected=new PairedCapturePcm().push(x,0,x.length);
  PairedCapturePcm p=new PairedCapturePcm();ArrayList<Short> all=new ArrayList<>();
  int[] sizes={1,7,19,3840,29,2,5};int pos=0,index=0;
  while(pos<x.length){int size=Math.min(sizes[index++%sizes.length],x.length-pos);
   short[] chunk=p.push(x,pos,size);for(short s:chunk)all.add(s);pos+=size;}
  short[] actual=new short[all.size()];for(int i=0;i<actual.length;i++)actual[i]=all.get(i);
  need(Arrays.equals(expected,actual),"Splits changed phase or partial frames");
 }
 static void reset(){
  PairedCapturePcm p=new PairedCapturePcm();short[] loud=new short[1001];Arrays.fill(loud,(short)32767);p.push(loud,0,loud.length);p.clear();
  short[] quiet=new short[1920];short[] out=p.push(quiet,0,quiet.length);
  for(short s:out)need(s==0,"Old recording crossed epoch");
  need(Arrays.equals(out,new PairedCapturePcm().push(quiet,0,quiet.length)),"Reset phase changed");
 }
 static void partial(){
  PairedCapturePcm p=new PairedCapturePcm();short[] x=new short[12];Arrays.fill(x,(short)1234);
  need(p.push(x,0,3).length==0,"Incomplete input frame emitted");
  need(p.push(x,3,8).length==0,"Incomplete third frame emitted");
  need(p.push(x,11,1).length==2,"Completed frame pair missing");
  need(p.push(x,0,0).length==0,"Empty read emitted samples");
 }
 static void invalid(){
  PairedCapturePcm p=new PairedCapturePcm();short[] x=input(300,500);
  int caught=0;
  try{p.push(null,0,0);}catch(IllegalArgumentException e){caught++;}
  try{p.push(x,-1,1);}catch(IllegalArgumentException e){caught++;}
  try{p.push(x,0,-1);}catch(IllegalArgumentException e){caught++;}
  try{p.push(x,x.length,1);}catch(IllegalArgumentException e){caught++;}
  try{p.push(new short[192001],0,192001);}catch(IllegalArgumentException e){caught++;}
  need(caught==5,"Invalid extent accepted");
  need(Arrays.equals(p.push(x,0,x.length),new PairedCapturePcm().push(x,0,x.length)),"Rejected input mutated state");
 }
 static void saturate(){
  short[] x=new short[4800];for(int i=0;i<x.length;i+=4){x[i]=32767;x[i+2]=-32768;}
  short[] out=new PairedCapturePcm().push(x,0,x.length);boolean positive=false,negative=false;
  for(int i=40;i<80;i+=2){need(out[i]>20000&&out[i+1]<-20000,"FIR overshoot wrapped PCM");positive|=out[i]==32767;negative|=out[i+1]==-32768;}
  need(positive&&negative,"Saturation control did not exercise clipping");
  need(out[out.length-2]==32767&&out[out.length-1]==-32768,"DC gain changed");
 }
 static void frequency(){
  double ideal=16000/Math.sqrt(2);
  for(double hz:new double[]{1000,6000,10000,16000}){
   short[] x=input(48000,hz),out=new PairedCapturePcm().push(x,0,x.length);
   double gain=rms(out,0)/ideal;
   if(hz<=6000)need(gain>.97&&gain<1.01,"Speech passband attenuated");
   else need(gain<.003,"High frequency aliased into speech");
  }
 }
 public static void main(String[] args){
  switch(args[0]){case "channel":channel();break;case "split":split();break;case "reset":reset();break;case "partial":partial();break;case "invalid":invalid();break;case "saturate":saturate();break;case "frequency":frequency();break;default:throw new AssertionError("Unknown case");}
  System.out.println("PASS "+args[0]);
 }
}
"""


class PairedCapturePcmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-paired-pcm-')
        root = Path(cls.folder.name)
        fixture = root / 'PairedPcmCheck.java'
        fixture.write_text(CHECK)
        cls.classes = root / 'classes'
        cls.classes.mkdir()
        try:
            result = subprocess.run(
                [str(java_bin() / 'javac'), '-d', str(cls.classes), str(SOURCE), str(fixture)],
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

    def run_case(self, name):
        output = subprocess.check_output(
            [
                str(java_bin() / 'java'),
                '-cp',
                str(self.classes),
                'com.happyhome.cc8assistant.PairedPcmCheck',
                name,
            ],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        self.assertEqual(output.strip(), 'PASS ' + name)

    def test_selected_channels_and_equal_phase(self):
        self.run_case('channel')

    def test_arbitrary_short_read_boundaries(self):
        self.run_case('split')

    def test_stop_clears_partial_frames_and_filter_history(self):
        self.run_case('reset')

    def test_incomplete_frames_and_empty_reads(self):
        self.run_case('partial')

    def test_invalid_extents_are_atomic(self):
        self.run_case('invalid')

    def test_filter_overshoot_saturates_instead_of_wrapping(self):
        self.run_case('saturate')

    def test_speech_passband_and_alias_rejection(self):
        self.run_case('frequency')


if __name__ == '__main__':
    unittest.main()
