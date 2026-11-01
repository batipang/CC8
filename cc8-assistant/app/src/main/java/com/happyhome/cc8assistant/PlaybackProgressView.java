package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.widget.ProgressBar;

/** Segmented telemetry rail; every illuminated cell follows actual karaoke progress. */
final class PlaybackProgressView extends ProgressBar {
    private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);private final Path marker=new Path();
    private static final int CYAN=Color.rgb(67,224,252),DIM=Color.rgb(31,66,87);
    PlaybackProgressView(Context context){super(context,null,android.R.attr.progressBarStyleHorizontal);setIndeterminate(false);}
    @Override protected synchronized void onDraw(Canvas c){
        float width=getWidth(),height=getHeight();if(width<=0||height<=0)return;
        float unit=height/24f,left=7*unit,right=Math.max(left,width-7*unit),y=height/2;
        float fraction=Math.max(0,Math.min(1,getProgress()/(float)Math.max(1,getMax()))),head=left+(right-left)*fraction;
        paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(unit);paint.setStrokeCap(Paint.Cap.BUTT);paint.setColor(DIM);
        c.drawLine(left,y-7*unit,right,y-7*unit,paint);c.drawLine(left,y+7*unit,right,y+7*unit,paint);
        // Small cut ends and calibration ticks, rather than a filled rounded capsule.
        c.drawLine(left-5*unit,y,left,y-7*unit,paint);c.drawLine(left-5*unit,y,left,y+7*unit,paint);
        c.drawLine(right+5*unit,y,right,y-7*unit,paint);c.drawLine(right+5*unit,y,right,y+7*unit,paint);
        int count=32;float step=(right-left)/count,gap=Math.min(4*unit,step*.3f);
        for(int i=0;i<count;i++){
            float start=left+i*step,end=start+step-gap;paint.setStrokeWidth(4*unit);paint.setColor(DIM);c.drawLine(start,y,end,y,paint);
            if(head>start){paint.setColor(CYAN);c.drawLine(start,y,Math.min(end,head),y,paint);}
            if(i%4==0){paint.setStrokeWidth(unit);paint.setColor(DIM);c.drawLine(start,y+7*unit,start,y+10*unit,paint);}
        }
        if(fraction<=0)return;
        paint.setColor(CYAN);paint.setStrokeWidth(8*unit);paint.setAlpha(20);c.drawLine(left,y,head,y,paint);paint.setAlpha(255);
        marker.reset();marker.moveTo(head,y-6*unit);marker.lineTo(head+4*unit,y);marker.lineTo(head,y+6*unit);marker.lineTo(head-4*unit,y);marker.close();
        paint.setStyle(Paint.Style.FILL);paint.setColor(Color.rgb(217,250,255));c.drawPath(marker,paint);
    }
}
