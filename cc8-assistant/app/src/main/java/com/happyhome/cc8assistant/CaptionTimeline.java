package com.happyhome.cc8assistant;

/** Immutable provider word timings, in UTF-16 offsets and actual playback frames. */
final class CaptionTimeline {
    // 每行 [UTF-16 起点, UTF-16 终点, 采样起点, 采样终点]，frame 来自真实播放头。
    final int[][] rows;final int length;
    CaptionTimeline(int[][] values,int length){this.length=length;this.rows=values;}
    float position(long frame){
        float result=0;
        for(int[] r:rows){
            if(r.length!=4||r[0]<0||r[1]<r[0]||r[1]>length||r[2]<0||r[3]<r[2])continue;
            if(frame<r[2])break;
            if(frame>=r[3])result=Math.max(result,r[1]);
            else {result=Math.max(result,r[0]+(r[1]-r[0])*(float)(frame-r[2])/Math.max(1,r[3]-r[2]));break;}
        }
        return Math.min(length,result);
    }
}
