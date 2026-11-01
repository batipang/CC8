package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import android.widget.ProgressBar;

/** Selected energy-cartridge design, driven only by the actual karaoke timeline. */
final class PlaybackProgressView extends ProgressBar {
    private static final int CYAN=Color.rgb(67,224,252),DIM=Color.rgb(55,102,130),AMBER=Color.rgb(255,199,91);
    private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path frame=new Path(),played=new Path();private final Path[] cells=new Path[20];
    private Shader energy;private float unit,left,right,slant;
    PlaybackProgressView(Context context){super(context,null,android.R.attr.progressBarStyleHorizontal);setIndeterminate(false);}
    @Override protected void onSizeChanged(int w,int h,int oldw,int oldh){
        super.onSizeChanged(w,h,oldw,oldh);unit=h/40f;left=12*unit;right=w-15*unit;slant=8*unit;
        frame.reset();frame.moveTo(2*unit,14*unit);frame.lineTo(12*unit,3*unit);frame.lineTo(w-12*unit,3*unit);frame.lineTo(w-2*unit,17*unit);frame.lineTo(w-14*unit,h-3*unit);frame.lineTo(10*unit,h-3*unit);frame.lineTo(2*unit,h-11*unit);
        float step=(right-left-slant)/cells.length;
        for(int i=0;i<cells.length;i++){float x=left+i*step,end=x+step-5*unit;Path cell=new Path();cell.moveTo(x+slant,10*unit);cell.lineTo(end+slant,10*unit);cell.lineTo(end,30*unit);cell.lineTo(x,30*unit);cell.close();cells[i]=cell;}
        energy=new LinearGradient(0,10*unit,0,30*unit,new int[]{Color.rgb(105,246,255),Color.rgb(25,201,226)},null,Shader.TileMode.CLAMP);
    }
    private void stroke(int color,float width,int alpha){paint.setShader(null);paint.setColor(color);paint.setAlpha(alpha);paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(width);paint.setStrokeCap(Paint.Cap.BUTT);}
    @Override protected synchronized void onDraw(Canvas c){
        if(unit<=0||right<=left)return;
        stroke(CYAN,unit,130);c.drawPath(frame,paint);
        stroke(CYAN,unit,240);c.drawLine(12*unit,3*unit,54*unit,3*unit,paint);c.drawLine(getWidth()-54*unit,getHeight()-3*unit,getWidth()-14*unit,getHeight()-3*unit,paint);
        stroke(DIM,unit,210);for(Path cell:cells)c.drawPath(cell,paint);
        float fraction=Math.max(0,Math.min(1,getProgress()/(float)Math.max(1,getMax())));
        if(fraction<=0)return;float head=left+(right-left)*fraction;
        played.reset();played.moveTo(0,0);played.lineTo(head+slant*.72f,0);played.lineTo(head-slant*.72f,getHeight());played.lineTo(0,getHeight());played.close();
        c.save();c.clipPath(played);
        stroke(CYAN,3*unit,24);for(Path cell:cells)c.drawPath(cell,paint);
        paint.setStyle(Paint.Style.FILL);paint.setAlpha(255);paint.setShader(energy);for(Path cell:cells)c.drawPath(cell,paint);paint.setShader(null);c.restore();
        stroke(AMBER,8*unit,30);paint.setStrokeCap(Paint.Cap.ROUND);c.drawLine(head+slant*.5f,7*unit,head-slant*.5f,33*unit,paint);
        stroke(AMBER,3.5f*unit,255);paint.setStrokeCap(Paint.Cap.ROUND);c.drawLine(head+slant*.5f,7*unit,head-slant*.5f,33*unit,paint);
    }
}
