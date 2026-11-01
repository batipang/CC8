package com.happyhome.cc8assistant;

/** Hold a live visible preview for five full seconds before taking a frame. */
final class PreviewDelay {
    static final long HOLD_MS=5000;
    private volatile long visibleAt=-1;
    void reset(){visibleAt=-1;}
    void visible(long now){if(visibleAt<0)visibleAt=now;}
    boolean started(){return visibleAt>=0;}
    long elapsed(long now){return visibleAt<0?0:Math.max(0,now-visibleAt);}
    boolean ready(long now){return started()&&elapsed(now)>=HOLD_MS;}
    int secondsLeft(long now){return (int)Math.max(0,(HOLD_MS-elapsed(now)+999)/1000);}
}
