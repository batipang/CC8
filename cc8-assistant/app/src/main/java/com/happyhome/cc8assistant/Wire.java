package com.happyhome.cc8assistant;

import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

final class Wire {
    static final int MIC=1, RENDER=2, EVENT=3, SPEAKER=4, HEADER=29, MAX=65536;
    static final class Packet {
        int type,generation;long sequence,frame,nanos;byte[] data;
    }
    static Packet read(DataInputStream in)throws IOException {
        int size=in.readInt();if(size<HEADER||size>MAX)throw new IOException("Invalid packet length");
        Packet p=new Packet();p.type=in.readUnsignedByte();p.generation=in.readInt();p.sequence=in.readLong();p.frame=in.readLong();p.nanos=in.readLong();p.data=new byte[size-HEADER];in.readFully(p.data);
        if(p.type<1||p.type>4)throw new IOException("Invalid packet type");
        if(p.type!=EVENT&&(p.data.length%2!=0||p.data.length>6400))throw new IOException("Invalid PCM frame");return p;
    }
    static void write(DataOutputStream out,int type,int generation,long sequence,long frame,long nanos,byte[] data)throws IOException {
        if(data.length+HEADER>MAX)throw new IOException("Packet too large");out.writeInt(HEADER+data.length);out.writeByte(type);out.writeInt(generation);out.writeLong(sequence);out.writeLong(frame);out.writeLong(nanos);out.write(data);out.flush();
    }
    static JSONObject json(Packet p)throws Exception{return new JSONObject(new String(p.data,StandardCharsets.UTF_8));}
    static byte[] pcm(short[] samples,int length){byte[] b=new byte[length*2];for(int i=0;i<length;i++){b[i*2]=(byte)samples[i];b[i*2+1]=(byte)(samples[i]>>8);}return b;}
    static short[] samples(byte[] b){short[] s=new short[b.length/2];for(int i=0;i<s.length;i++)s[i]=(short)((b[i*2]&255)|(b[i*2+1]<<8));return s;}
    static byte[] jsonBytes(JSONObject value){return value.toString().getBytes(StandardCharsets.UTF_8);}
    static JSONObject event(String name){JSONObject j=new JSONObject();put(j,"event",name);return j;}
    static void put(JSONObject j,String k,Object v){try{j.put(k,v);}catch(Exception ignored){}}
}
