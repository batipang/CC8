package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import android.os.SystemClock;
import android.view.View;
import java.util.Calendar;
import java.util.Locale;

/** Local-time orbit clock: hour/minute beacons and a live second sweep around the portrait. */
final class CompanionView extends View {
    private final Paint p=new Paint(Paint.ANTI_ALIAS_FLAG),timePaint=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF oval=new RectF();
    private final BumblebeeAvatar avatar;private final float[] ticks=new float[60*4];
    private final DashPathEffect dots=new DashPathEffect(new float[]{1.7f,7},0);
    private final Shader halo=new RadialGradient(300,300,320,new int[]{Color.rgb(8,44,63),Color.TRANSPARENT},null,Shader.TileMode.CLAMP);
    private float playback,input;private boolean linked,active;private String mood="friendly",phase="idle";
    private long clockSecond=Long.MIN_VALUE;private int hour,minute,second;private String clockLabel="",clockZone="";
    private static final int CYAN=Color.rgb(67,224,252),BLUE=Color.rgb(61,116,153),AMBER=Color.rgb(255,199,91);
    CompanionView(Context context){super(context);avatar=new BumblebeeAvatar(context);
        timePaint.setTypeface(Typeface.create("monospace",Typeface.NORMAL));timePaint.setTextAlign(Paint.Align.CENTER);timePaint.setTextSize(34);
        for(int i=0;i<60;i++){double a=(i*6-90)*Math.PI/180;float inner=i%5==0?268:279;int j=i*4;ticks[j]=300+(float)Math.cos(a)*inner;ticks[j+1]=300+(float)Math.sin(a)*inner;ticks[j+2]=300+(float)Math.cos(a)*289;ticks[j+3]=300+(float)Math.sin(a)*289;}
        refreshClock(System.currentTimeMillis());
    }
    void setTransport(String value){}
    void update(float playback,float input,String mood,String phase,boolean linked,boolean active){this.playback=playback;this.input=input;this.mood=mood;this.phase=phase;this.linked=linked;this.active=active;invalidate();}
    private void refreshClock(long millis){
        if(clockSecond==millis/1000)return;clockSecond=millis/1000;
        Calendar local=Calendar.getInstance();local.setTimeInMillis(millis);hour=local.get(Calendar.HOUR_OF_DAY);minute=local.get(Calendar.MINUTE);second=local.get(Calendar.SECOND);clockZone=local.getTimeZone().getID();
        String label=String.format(Locale.US,"%02d:%02d",hour,minute);
        if(!label.equals(clockLabel)){clockLabel=label;setContentDescription("大黄蜂头像，当前时间 "+clockLabel);}
    }
    String clockTime(){refreshClock(System.currentTimeMillis());return clockLabel;}
    String clockTimeZone(){refreshClock(System.currentTimeMillis());return clockZone;}
    private void stroke(int color,float width,int alpha){p.setShader(null);p.setStyle(Paint.Style.STROKE);p.setColor(color);p.setAlpha(alpha);p.setStrokeWidth(width);p.setStrokeCap(Paint.Cap.BUTT);p.setPathEffect(null);}
    private void ring(Canvas c,float radius,float start,float sweep,int color,float width,int alpha){oval.set(300-radius,300-radius,300+radius,300+radius);if(width>=6){stroke(color,width+21,9);c.drawArc(oval,start,sweep,false,p);stroke(color,width+10,25);c.drawArc(oval,start,sweep,false,p);}stroke(color,width,alpha);c.drawArc(oval,start,sweep,false,p);}
    private void secondPoint(Canvas c,float degrees){
        double angle=(degrees-90)*Math.PI/180;float x=300+(float)Math.cos(angle)*296,y=300+(float)Math.sin(angle)*296;
        p.setStyle(Paint.Style.FILL);p.setColor(CYAN);p.setAlpha(24);c.drawCircle(x,y,12,p);p.setAlpha(70);c.drawCircle(x,y,7,p);p.setAlpha(255);c.drawCircle(x,y,3.3f,p);
    }
    @Override protected void onDraw(Canvas c){super.onDraw(c);float size=Math.min(getWidth(),getHeight());c.save();c.translate((getWidth()-size)/2,(getHeight()-size)/2);c.scale(size/600,size/600);
        long wall=System.currentTimeMillis();refreshClock(wall);float seconds=second+(wall%1000)/1000f;
        float minuteDegrees=(minute+seconds/60)*6,hourDegrees=((hour%12)+minute/60f+seconds/3600)*30;
        int tint="error".equals(phase)?AMBER:CYAN;
        p.setStyle(Paint.Style.FILL);p.setAlpha(255);p.setShader(halo);c.drawCircle(300,300,320,p);p.setShader(null);
        ring(c,320,0,360,BLUE,1,30);ring(c,296,0,360,BLUE,2,230);stroke(CYAN,1.3f,linked?225:100);c.drawLines(ticks,p);
        // Twelve o'clock stays fixed; every luminous marker represents wall-clock time.
        stroke(AMBER,4,245);c.drawLine(300,10,300,32,p);
        ring(c,273,-90,hourDegrees,AMBER,1.6f,90);ring(c,273,hourDegrees-97,14,AMBER,10,250);
        float energy=Math.min(1,Math.max(playback,input)*24);int alpha=linked?Math.round(220+35*("speaking".equals(phase)?energy:active?.8f:.2f)):150;
        ring(c,258,-90,minuteDegrees,tint,1.4f,80);ring(c,258,minuteDegrees-101,22,tint,8,alpha);
        ring(c,296,seconds*6-101,11,CYAN,3,170);secondPoint(c,seconds*6);
        ring(c,232,0,360,BLUE,1.2f,180);stroke(CYAN,1.6f,170);p.setStrokeCap(Paint.Cap.ROUND);p.setPathEffect(dots);oval.set(56,56,544,544);c.drawOval(oval,p);p.setPathEffect(null);
        c.save();c.translate(70,89);c.scale(430f/1254,400f/1254);avatar.draw(c,playback,mood,SystemClock.uptimeMillis());c.restore();
        // Readout sits below the face and inside the orbit, clear of the independent waveform.
        oval.set(216,508,384,558);p.setStyle(Paint.Style.FILL);p.setColor(Color.rgb(3,24,39));p.setAlpha(245);c.drawRoundRect(oval,10,10,p);stroke(BLUE,1,180);c.drawRoundRect(oval,10,10,p);
        timePaint.setColor(Color.rgb(217,240,249));c.drawText(clockLabel,300,545,timePaint);c.restore();
    }
}
