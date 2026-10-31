package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import android.os.SystemClock;
import android.view.View;

/** Vector HUD and approved G1 avatar; mouth motion follows the playback head. */
final class CompanionView extends View {
    private final Paint p=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF oval=new RectF();
    private final BumblebeeAvatar avatar;
    private final Shader halo=new RadialGradient(300,286,250,new int[]{Color.rgb(9,42,58),Color.TRANSPARENT},null,Shader.TileMode.CLAMP);
    private final float[] ticks=new float[96*4];
    private final Typeface mono=Typeface.create("monospace",Typeface.NORMAL);
    private String transport="USB";void setTransport(String value){transport=value;}
    private float playback,input;private boolean linked,active;private String mood="friendly",phase="idle";
    private static final int CYAN=Color.rgb(89,224,244),BLUE=Color.rgb(32,111,148),AMBER=Color.rgb(243,184,98);
    CompanionView(Context context){super(context);avatar=new BumblebeeAvatar(context);setContentDescription("大黄蜂，G1 大黄蜂头像，声音与嘴型同步");
        for(int i=0;i<96;i++){double a=i*Math.PI*2/96;float inner=i%4==0?226:233;int j=i*4;ticks[j]=300+(float)Math.cos(a)*inner;ticks[j+1]=286+(float)Math.sin(a)*inner;ticks[j+2]=300+(float)Math.cos(a)*241;ticks[j+3]=286+(float)Math.sin(a)*241;}}
    void update(float playback,float input,String mood,String phase,boolean linked,boolean active){this.playback=playback;this.input=input;this.mood=mood;this.phase=phase;this.linked=linked;this.active=active;invalidate();}
    private void stroke(int color,float width,int alpha){p.setShader(null);p.setStyle(Paint.Style.STROKE);p.setColor(color);p.setAlpha(alpha);p.setStrokeWidth(width);p.setStrokeCap(Paint.Cap.BUTT);}
    private void ring(Canvas c,float radius,float start,float sweep,int color,float width,int alpha){stroke(color,width,alpha);oval.set(300-radius,286-radius,300+radius,286+radius);c.drawArc(oval,start,sweep,false,p);}
    private void label(Canvas c,String value,float x,float y,float size,int color,Paint.Align align){p.setShader(null);p.setStyle(Paint.Style.FILL);p.setColor(color);p.setAlpha(255);p.setTypeface(mono);p.setTextSize(size);p.setTextAlign(align);c.drawText(value,x,y,p);}
    @Override protected void onDraw(Canvas c){super.onDraw(c);float size=Math.min(getWidth(),getHeight());c.save();c.translate((getWidth()-size)/2,(getHeight()-size)/2);c.scale(size/600,size/600);
        float seconds=SystemClock.uptimeMillis()/1000f;float angle=seconds*7%360;int tint="error".equals(phase)?AMBER:CYAN;
        float amplitude=Math.min(1,(playback>0?playback:active?input:0)*20);
        p.setStyle(Paint.Style.FILL);p.setAlpha(255);p.setShader(halo);c.drawCircle(300,286,250,p);p.setShader(null);
        ring(c,253,0,360,BLUE,1,100);stroke(BLUE,1,170);c.drawLines(ticks,p);
        for(int i=0;i<4;i++)ring(c,218,angle+i*90,65,tint,2.3f,linked?150:55);
        ring(c,204,-angle*.7f,104,tint,5,linked?220:65);ring(c,204,180-angle*.7f,104,tint,5,linked?220:65);
        ring(c,191,20+angle*.4f,27,AMBER,3,170);ring(c,181,0,360,BLUE,1,180);
        ring(c,167,0,360,tint,1,50);
        c.save();c.translate(126,106+(float)Math.sin(seconds*1.8f)*2);c.scale(348f/1254,348f/1254);
        avatar.draw(c,playback,mood,SystemClock.uptimeMillis());c.restore();
        String status=!linked?"OFFLINE":!active&&!"speaking".equals(phase)&&!"thinking".equals(phase)?"STANDBY":"thinking".equals(phase)?"PROCESSING":"speaking".equals(phase)?"RESPONDING":"calibrating".equals(phase)?"CALIBRATING":"LISTENING";
        label(c,"VOICE CORE",300,17,13,Color.rgb(89,147,169),Paint.Align.CENTER);label(c,status,300,558,18,linked?tint:BLUE,Paint.Align.CENTER);
        label(c,active?"MIC / ON":"MIC / OFF",67,580,12,active?tint:Color.rgb(95,130,148),Paint.Align.LEFT);label(c,linked?transport+" / LINKED":"LINK / WAIT",533,580,12,linked?tint:AMBER,Paint.Align.RIGHT);
        stroke(BLUE,1,150);c.drawLine(58,276,80,276,p);c.drawLine(520,276,542,276,p);c.drawLine(300,35,300,49,p);c.restore();
    }
}
