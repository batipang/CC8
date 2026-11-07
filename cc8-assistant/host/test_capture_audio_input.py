"""Actual recorder factory/converter against bounded native interfaces; no hardware access."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from tools.java_toolchain import java_bin

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
FAKES = {
    'android/annotation/SuppressLint.java': r"""package android.annotation; public @interface SuppressLint {String[] value();}""",
    'android/os/Build.java': r"""package android.os; public class Build {public static class VERSION {public static int SDK_INT=27;}}""",
    'android/media/MediaRecorder.java': r"""package android.media; public class MediaRecorder {public static class AudioSource {public static int VOICE_COMMUNICATION=7;}}""",
    'android/media/AudioFormat.java': r"""package android.media;
public class AudioFormat {
 public static int CHANNEL_IN_MONO=1,ENCODING_PCM_16BIT=2;public int rate,channels;
 public static class Builder {
  final AudioFormat format=new AudioFormat();
  public Builder setEncoding(int value){return this;}public Builder setSampleRate(int value){format.rate=value;return this;}
  public Builder setChannelIndexMask(int mask){format.channels=Integer.bitCount(mask);return this;}
  public AudioFormat build(){return format;}
 }
}""",
    'android/media/AudioRecord.java': r"""package android.media;
import java.util.*;
public class AudioRecord {
 public static final int STATE_INITIALIZED=1;
 public static final List<AudioRecord> created=new ArrayList<>();
 public static String mode="ok";public static int builders;
 public static short[] source=new short[0];public static int[] sizes={3840};
 public int rate,channels,encoding=2,state=STATE_INITIALIZED,releases,starts,position,reads;
 public AudioRecord(int source,int rate,int mask,int encoding,int buffer){this(rate,1);if(mode.equals("bad_mono"))state=0;}
 private AudioRecord(int rate,int channels){this.rate=rate;this.channels=channels;created.add(this);}
 public static int getMinBufferSize(int rate,int channels,int encoding){return mode.equals("bad_min")?-1:640;}
 public int getState(){return state;}public int getSampleRate(){return rate;}public int getChannelCount(){return channels;}public int getAudioFormat(){return encoding;}
 public void startRecording(){starts++;}
 public void release(){releases++;if(mode.equals("release_fail"))throw new IllegalStateException();}
 public int read(short[] out,int offset,int count){
  if(mode.equals("negative"))return -3;if(mode.equals("oversize"))return count+1;
  int planned=sizes[reads++%sizes.length];if(planned==0)return 0;
  int n=Math.min(Math.min(count,planned),source.length-position);
  System.arraycopy(source,position,out,offset,n);position+=n;return n;
 }
 public static class Builder {
  AudioFormat format;
  public Builder setAudioSource(int source){return this;}public Builder setAudioFormat(AudioFormat format){this.format=format;return this;}
  public Builder setBufferSizeInBytes(int size){return this;}
  public AudioRecord build(){
   builders++;if(mode.equals("throw_paired"))throw new IllegalArgumentException();
   AudioRecord r=new AudioRecord(format.rate,format.channels);
   if(mode.equals("bad_rate"))r.rate=44100;if(mode.equals("bad_encoding"))r.encoding=3;
   if(mode.equals("bad_channels")||mode.equals("release_fail"))r.channels=2;
   if(mode.equals("bad_state"))r.state=0;return r;
  }
 }
}""",
    'com/happyhome/cc8assistant/CaptureInputCheck.java': r"""package com.happyhome.cc8assistant;
import android.media.*;import android.os.Build;import java.io.*;import java.util.*;
public class CaptureInputCheck {
 static void need(boolean ok,String why){if(!ok)throw new AssertionError(why);}
 static void close(CaptureAudioInput input){input.clear();input.recorder.release();}
 static void factory(boolean paired)throws Exception{
  CaptureAudioInput input=CaptureAudioInput.open(paired);
  need(input.paired==paired,"Wrong selected frontend");need(input.recorder.starts==0,"Factory started recording");
  need(input.recorder.getSampleRate()==(paired?48000:16000),"Wrong rate");
  need(input.recorder.getChannelCount()==(paired?4:1),"Wrong channels");close(input);
  need(input.recorder.releases==1,"Recorder release ownership changed");
 }
 static void fallback(String mode)throws Exception{
  AudioRecord.mode=mode;CaptureAudioInput input=CaptureAudioInput.open(true);
  need(!input.paired&&input.fallbackReason!=null,"Unsupported paired format did not declare fallback");
  if(!mode.equals("throw_paired"))need(AudioRecord.created.get(0).releases==1,"Failed paired recorder leaked");
  need(input.recorder.starts==0,"Fallback acquired an active recorder");close(input);
 }
 static void oldApi()throws Exception{
  Build.VERSION.SDK_INT=22;CaptureAudioInput input=CaptureAudioInput.open(true);
  need(!input.paired&&AudioRecord.builders==0,"Pre-23 path invoked new API");close(input);
 }
 static short[] signal(){
  short[] raw=new short[9600*4];for(int f=0;f<9600;f++){
   raw[f*4]=(short)Math.rint(12000*Math.sin(2*Math.PI*1000*f/48000));
   raw[f*4+1]=30000;raw[f*4+2]=(short)(-raw[f*4]);raw[f*4+3]=-30000;
  }return raw;
 }
 static void split()throws Exception{
  short[] raw=signal();AudioRecord.source=raw;AudioRecord.sizes=new int[]{1,7,0,19,3840,29,3,511};
  short[] expected=new PairedCapturePcm().push(raw,0,raw.length);
  CaptureAudioInput input=CaptureAudioInput.open(true);ArrayList<Short> values=new ArrayList<>();
  for(int attempts=0;attempts<5000&&input.recorder.position<raw.length;attempts++){
   CaptureAudioInput.Chunk c=input.read();if(c==null)continue;
   need(c.paired&&c.frames==320&&c.samples.length==640,"Short read emitted partial logical packet");
   for(short v:c.samples)values.add(v);c.clear();for(short v:c.samples)need(v==0,"Returned chunk did not clear");
  }
  need(input.recorder.position==raw.length,"Input was not consumed");
  need(values.size()==expected.length,"Converted frames dropped or duplicated");
  for(int i=0;i<expected.length;i++)need(values.get(i)==expected[i],"Packet splits changed selected audio");
  close(input);
 }
 static void mono()throws Exception{
  AudioRecord.source=new short[]{1000,-2000,3000,-4000};AudioRecord.sizes=new int[]{4};
  CaptureAudioInput input=CaptureAudioInput.open(false);CaptureAudioInput.Chunk c=input.read();
  need(!c.paired&&c.frames==4&&Arrays.equals(c.samples,AudioRecord.source),"Legacy mono read changed");
  c.clear();need(input.read()==null,"EOF emitted data");close(input);
 }
 static void level(){
  short[] data=new short[640];for(int i=0;i<data.length;i+=2){data[i]=1000;data[i+1]=30000;}
  CaptureAudioInput.Chunk c=new CaptureAudioInput.Chunk(data,true);
  need(Math.abs(c.level()-1000.0/32768)<1e-7,"Candidate reference polluted microphone level");c.clear();
 }
 static void failure(String mode,boolean paired)throws Exception{
  AudioRecord.mode=mode;boolean failed=false;
  try{CaptureAudioInput input=CaptureAudioInput.open(paired);try{input.read();}finally{close(input);}}
  catch(IOException e){failed=true;}
  need(failed,"Invalid native state/read accepted");
  for(AudioRecord r:AudioRecord.created)need(r.releases==1,"Failure leaked or double-released recorder");
  if(mode.equals("release_fail"))need(AudioRecord.created.size()==1,"Unconfirmed release still opened fallback");
 }
 public static void main(String[] args)throws Exception{
  switch(args[0]){
   case "paired":factory(true);break;case "mono":factory(false);mono();break;
   case "fallback":fallback(args[1]);break;case "old_api":oldApi();break;case "split":split();break;
   case "level":level();break;case "negative":failure("negative",true);break;
   case "oversize":failure("oversize",true);break;case "bad_mono":failure("bad_mono",false);break;
   case "release_fail":failure("release_fail",true);break;default:throw new AssertionError("Unknown case");
  }System.out.println("PASS "+args[0]);
 }
}""",
}


class CaptureAudioInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = java_bin()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-capture-input-')
        root = Path(cls.folder.name)
        sources = []
        try:
            for name, body in FAKES.items():
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body)
                sources.append(str(p))
            cls.classes = root / 'classes'
            cls.classes.mkdir()
            result = subprocess.run(
                [
                    str(cls.java / 'javac'),
                    '-d',
                    str(cls.classes),
                    str(JAVA / 'PairedCapturePcm.java'),
                    str(JAVA / 'CaptureAudioInput.java'),
                    *sources,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                raise AssertionError(result.stderr[:5000])
        except BaseException:
            cls.folder.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def check(self, case, *args):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                str(self.classes),
                'com.happyhome.cc8assistant.CaptureInputCheck',
                case,
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'PASS ' + case)

    def test_requested_format_is_initialized_but_never_started(self):
        self.check('paired')
        self.check('mono')

    def test_unsupported_api_uses_legacy_constructor(self):
        self.check('old_api')

    def test_failed_paired_format_releases_before_mono_fallback(self):
        for mode in ('throw_paired', 'bad_rate', 'bad_channels', 'bad_state', 'bad_encoding'):
            with self.subTest(mode=mode):
                self.check('fallback', mode)

    def test_short_reads_preserve_pairs_until_complete_twenty_ms_packet(self):
        self.check('split')

    def test_reference_does_not_affect_microphone_meter(self):
        self.check('level')

    def test_bad_native_read_is_bounded_and_releases_recorder(self):
        self.check('negative')
        self.check('oversize')

    def test_failed_mono_initialization_releases_recorder(self):
        self.check('bad_mono')

    def test_unconfirmed_release_prevents_new_fallback_recorder(self):
        self.check('release_fail')


if __name__ == '__main__':
    unittest.main()
