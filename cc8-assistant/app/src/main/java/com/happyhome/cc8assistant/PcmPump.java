package com.happyhome.cc8assistant;

import java.io.IOException;

/** AudioTrack may return a short/zero write when paused during a blocking write. */
final class PcmPump {
    // AudioTrack 暂停时可能短写或零写；只有被硬件接受的采样才能推进参考帧和嘴型。
    interface Sink {
        boolean allowed(); boolean paused();
        void awaitReady() throws InterruptedException;
        int write(short[] samples,int offset,int count) throws IOException;
        void accepted(short[] samples,int offset,int count) throws IOException;
    }
    static boolean writeAll(short[] samples,Sink sink)throws IOException,InterruptedException {
        int offset=0,stalls=0;
        while(offset<samples.length){
            sink.awaitReady();if(!sink.allowed())return false;
            int count=sink.write(samples,offset,samples.length-offset);
            if(!sink.allowed())return false;
            if(count>samples.length-offset)throw new IOException("Invalid PCM write count");
            if(count>0){sink.accepted(samples,offset,count);offset+=count;stalls=0;}
            else if(sink.paused()){sink.awaitReady();}
            else if(count<0)throw new IOException("AudioTrack error "+count);
            else {if(++stalls>600)throw new IOException("AudioTrack stalled");Thread.sleep(5);}
        }
        return true;
    }
}
