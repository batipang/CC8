package com.happyhome.cc8diagnostics;

import java.io.*;
import java.nio.charset.StandardCharsets;

/** Bounded RIFF reader and mono 16-bit PCM writer. */
public final class WavCodec {
    public static final class Pcm {
        public final int sampleRate;
        public final short[] samples;
        Pcm(int rate,short[] pcm) { sampleRate=rate; samples=pcm; }
    }
    private static int le16(DataInputStream in) throws IOException { return in.readUnsignedByte() | in.readUnsignedByte()<<8; }
    private static long le32(DataInputStream in) throws IOException { return ((long)le16(in) | (long)le16(in)<<16)&0xffffffffL; }
    private static String four(DataInputStream in) throws IOException { byte[] b=new byte[4];in.readFully(b);return new String(b,StandardCharsets.US_ASCII); }
    private static void skip(DataInputStream in,long n) throws IOException { while(n>0) { long done=in.skip(n);if(done<=0) {if(in.read()<0)throw new EOFException();done=1;}n-=done;} }
    public static Pcm read(InputStream input) throws IOException {
        DataInputStream in=new DataInputStream(new BufferedInputStream(input));
        if(!"RIFF".equals(four(in)))throw new IOException("Not RIFF");
        le32(in);if(!"WAVE".equals(four(in)))throw new IOException("Not WAVE");
        int rate=0,channels=0,bits=0,format=0;
        for(int chunk=0;chunk<64;chunk++) {
            String id=four(in);long length=le32(in);
            if(length>8*1024*1024)throw new IOException("WAV chunk too large");
            if("fmt ".equals(id)) {
                if(length<16)throw new IOException("Invalid fmt chunk");
                format=le16(in);channels=le16(in);rate=(int)le32(in);le32(in);le16(in);bits=le16(in);skip(in,length-16);
            } else if("data".equals(id)) {
                if(format!=1||channels!=1||bits!=16||rate<8000||rate>192000||length%2!=0)throw new IOException("Expected mono 16-bit PCM");
                short[] data=new short[(int)length/2];for(int i=0;i<data.length;i++)data[i]=(short)le16(in);
                return new Pcm(rate,data);
            } else skip(in,length);
            if((length&1)!=0)skip(in,1);
        }
        throw new IOException("Missing PCM data");
    }
    private static void write16(OutputStream out,int v) throws IOException {out.write(v&255);out.write((v>>>8)&255);}
    private static void write32(OutputStream out,int v) throws IOException {write16(out,v);write16(out,v>>>16);}
    public static void write(File file,short[] samples,int length,int rate) throws IOException {
        if(length<0||length>samples.length||rate<8000||rate>192000)throw new IllegalArgumentException("Invalid PCM");
        try(OutputStream out=new BufferedOutputStream(new FileOutputStream(file))) {
            out.write("RIFF".getBytes(StandardCharsets.US_ASCII));write32(out,36+length*2);out.write("WAVEfmt ".getBytes(StandardCharsets.US_ASCII));write32(out,16);write16(out,1);write16(out,1);write32(out,rate);write32(out,rate*2);write16(out,2);write16(out,16);out.write("data".getBytes(StandardCharsets.US_ASCII));write32(out,length*2);for(int i=0;i<length;i++)write16(out,samples[i]);
        }
    }
}
