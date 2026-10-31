package com.happyhome.cc8diagnostics;

import java.io.*;
import java.nio.file.Files;
import java.util.Arrays;

public final class HostTests {
    private static int count;
    private static void check(boolean value,String message){count++;if(!value)throw new AssertionError(message);}
    private static void failRead(byte[] data,String label) throws Exception {boolean failed=false;try{WavCodec.read(new ByteArrayInputStream(data));}catch(IOException e){failed=true;}check(failed,label);}
    public static void main(String[] args) throws Exception {
        AudioMath silence=new AudioMath();silence.add(new short[16000],0,16000);
        check(silence.dbfs()==-120,"Silence must have a finite floor");check(silence.zeroRatio()==1,"Silence must be identified as zero samples");
        AudioMath loud=new AudioMath();loud.add(new short[]{-32768,32767,0,0},0,4);
        check(loud.peak==32768,"Negative full scale must not overflow");check(loud.clipRatio()==.5,"Clipped samples must be counted");check(loud.zeroRatio()==.5,"Zero ratio must preserve mixed data");
        AudioMath offset=new AudioMath();offset.add(new short[]{100,0,0,100},1,2);check(offset.samples==2&&offset.squares==0,"PCM offsets must be respected");
        check(Arrays.equals(AudioMath.repeat(new short[]{1,2,3},7),new short[]{1,2,3,1,2,3,1}),"Reference must repeat to exact duration");
        boolean empty=false;try{AudioMath.repeat(new short[0],10);}catch(IllegalArgumentException e){empty=true;}check(empty,"Empty reference must be rejected");
        File file=File.createTempFile("cc8-wav-test-",".wav");
        try{
            short[] samples={0,-32768,-1,1,32767,1024,-1024};WavCodec.write(file,samples,samples.length,16000);
            byte[] bytes=Files.readAllBytes(file.toPath());WavCodec.Pcm pcm=WavCodec.read(new ByteArrayInputStream(bytes));
            check(pcm.sampleRate==16000,"Sample rate must round trip");check(Arrays.equals(samples,pcm.samples),"Signed PCM must round trip");check(bytes.length==44+samples.length*2,"WAV length must match sample count");
            byte[] stereo=bytes.clone();stereo[22]=2;failRead(stereo,"Stereo reference must not be misread as mono");
            failRead(Arrays.copyOf(bytes,bytes.length-1),"Truncated PCM must be rejected");
            byte[] invalid=bytes.clone();invalid[0]='X';failRead(invalid,"Non-RIFF files must be rejected");
            ByteArrayOutputStream withMetadata=new ByteArrayOutputStream();withMetadata.write(bytes,0,12);withMetadata.write(new byte[]{'J','U','N','K',1,0,0,0,99,0});withMetadata.write(bytes,12,bytes.length-12);
            byte[] metadata=withMetadata.toByteArray();int riffSize=metadata.length-8;for(int i=0;i<4;i++)metadata[4+i]=(byte)(riffSize>>>(i*8));
            check(Arrays.equals(samples,WavCodec.read(new ByteArrayInputStream(metadata)).samples),"Odd metadata chunks and padding must be skipped correctly");
        }finally{file.delete();}
        System.out.println("Host audio tests passed: "+count+" checks");
    }
}
