package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.widget.ProgressBar;

/** Rounded playback progress driven by the same real timeline as karaoke text. */
final class PlaybackProgressView extends ProgressBar {
    private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);private final RectF rect=new RectF();
    PlaybackProgressView(Context context){super(context,null,android.R.attr.progressBarStyleHorizontal);setIndeterminate(false);}
    @Override protected synchronized void onDraw(Canvas c){float radius=getHeight()/2f;rect.set(0,0,getWidth(),getHeight());paint.setColor(Color.rgb(24,55,77));c.drawRoundRect(rect,radius,radius,paint);float width=getWidth()*getProgress()/(float)Math.max(1,getMax());if(width>0){rect.right=Math.max(getHeight(),width);paint.setColor(Color.rgb(67,224,252));c.drawRoundRect(rect,radius,radius,paint);}}
}
