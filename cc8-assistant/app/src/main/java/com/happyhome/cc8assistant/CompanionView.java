package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import android.os.SystemClock;
import android.view.View;

/** Reference-layout orbit: short luminous arcs, precise ticks and an unobstructed portrait. */
final class CompanionView extends View {
    private final Paint p=new Paint(Paint.ANTI_ALIAS_FLAG);private final RectF oval=new RectF();
    private final BumblebeeAvatar avatar;private final float[] ticks=new float[96*4];
    private final DashPathEffect dots=new DashPathEffect(new float[]{1.7f,7},0);
    private final Shader halo=new RadialGradient(300,300,320,new int[]{Color.rgb(8,44,63),Color.TRANSPARENT},null,Shader.TileMode.CLAMP);
    private long lastFrame;private float orbitAngle;private float playback,input;private boolean linked,active;private String mood="friendly",phase="idle";
    private static final int[] BEACONS={-90,90,180,45};
    private static final int CYAN=Color.rgb(67,224,252),BLUE=Color.rgb(61,116,153),AMBER=Color.rgb(255,199,91);
    CompanionView(Context context){super(context);avatar=new BumblebeeAvatar(context);setContentDescription("大黄蜂头像与完整动态光环");
        for(int i=0;i<96;i++){double a=i*Math.PI*2/96;float inner=i%4==0?268:276;int j=i*4;ticks[j]=300+(float)Math.cos(a)*inner;ticks[j+1]=300+(float)Math.sin(a)*inner;ticks[j+2]=300+(float)Math.cos(a)*285;ticks[j+3]=300+(float)Math.sin(a)*285;}}
    void setTransport(String value){}
    void update(float playback,float input,String mood,String phase,boolean linked,boolean active){this.playback=playback;this.input=input;this.mood=mood;this.phase=phase;this.linked=linked;this.active=active;invalidate();}
    private void stroke(int color,float width,int alpha){p.setShader(null);p.setStyle(Paint.Style.STROKE);p.setColor(color);p.setAlpha(alpha);p.setStrokeWidth(width);p.setStrokeCap(Paint.Cap.BUTT);p.setPathEffect(null);}
    private void ring(Canvas c,float radius,float start,float sweep,int color,float width,int alpha){oval.set(300-radius,300-radius,300+radius,300+radius);if(width>=6){stroke(color,width+21,9);c.drawArc(oval,start,sweep,false,p);stroke(color,width+10,25);c.drawArc(oval,start,sweep,false,p);}stroke(color,width,alpha);c.drawArc(oval,start,sweep,false,p);}
    @Override protected void onDraw(Canvas c){super.onDraw(c);float size=Math.min(getWidth(),getHeight());c.save();c.translate((getWidth()-size)/2,(getHeight()-size)/2);c.scale(size/600,size/600);
        long now=SystemClock.uptimeMillis();float seconds=now/1000f,dt=lastFrame==0?0:Math.min(.1f,(now-lastFrame)/1000f);lastFrame=now;orbitAngle=(orbitAngle+dt*("thinking".equals(phase)?14:active?7:3))%360;float angle=orbitAngle;int tint="error".equals(phase)?AMBER:CYAN;
        p.setStyle(Paint.Style.FILL);p.setAlpha(255);p.setShader(halo);c.drawCircle(300,300,320,p);p.setShader(null);
        ring(c,320,0,360,BLUE,1,30);ring(c,296,0,360,BLUE,2,230);stroke(CYAN,1.3f,linked?225:100);c.drawLines(ticks,p);
        float energy=Math.min(1,Math.max(playback,input)*24);int alpha=linked?Math.round(220+35*("speaking".equals(phase)?energy:active?.6f+.4f*(float)Math.sin(seconds*1.5f):.2f)):100;for(int i=0;i<4;i++)ring(c,273,angle-150+i*90,23,tint,11,alpha);
        ring(c,273,angle*.6f-42,17,AMBER,8,245);ring(c,252,0,360,CYAN,1.3f,220);ring(c,232,0,360,BLUE,1.2f,180);
        stroke(CYAN,1.6f,220);p.setStrokeCap(Paint.Cap.ROUND);p.setPathEffect(dots);oval.set(300-244,300-244,300+244,300+244);c.drawOval(oval,p);p.setPathEffect(null);
        for(int index:BEACONS){double a=index*Math.PI/180;stroke(AMBER,5,245);c.drawLine(300+(float)Math.cos(a)*268,300+(float)Math.sin(a)*268,300+(float)Math.cos(a)*289,300+(float)Math.sin(a)*289,p);}
        c.save();c.translate(70,89);c.scale(430f/1254,400f/1254);avatar.draw(c,playback,mood,SystemClock.uptimeMillis());c.restore();c.restore();
    }
}
