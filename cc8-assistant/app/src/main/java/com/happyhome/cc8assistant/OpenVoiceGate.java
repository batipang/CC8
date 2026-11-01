package com.happyhome.cc8assistant;

/** One bounded foreground-opening request, consumed before permission/capture. */
final class OpenVoiceGate {
    private boolean pending,opening;private long deadline;
    void opened(long now){opening=true;pending=true;deadline=now+30000;}
    void resumed(long now){if(opening){opening=false;pending=true;deadline=now+30000;}}
    void stopped(){cancel();}
    void cancel(){opening=false;pending=false;}
    boolean staged(){return opening;}
    boolean waiting(long now){return pending&&now<deadline;}
    boolean expire(long now){if(pending&&now>=deadline){pending=false;return true;}return false;}
    boolean take(long now,boolean foreground,boolean connected,boolean recording){
        if(expire(now)||opening||!pending||!foreground)return false;
        if(recording){pending=false;return false;}
        if(!connected)return false;
        pending=false;return true;
    }
}
