package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import android.view.View;

/** Static backdrop: no offscreen bitmap, animated blur, or synthetic telemetry. */
final class HudBackdrop extends View {
    private final Paint p=new Paint(Paint.ANTI_ALIAS_FLAG);
    private LinearGradient background;
    HudBackdrop(Context context){super(context);setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_NO);}
    @Override protected void onSizeChanged(int w,int h,int oldw,int oldh){background=new LinearGradient(0,0,w,h,new int[]{Color.rgb(5,17,28),Color.rgb(3,10,18)},null,Shader.TileMode.CLAMP);}
    @Override protected void onDraw(Canvas c){super.onDraw(c);int w=getWidth(),h=getHeight();float d=getResources().getDisplayMetrics().density;p.setShader(background);c.drawRect(0,0,w,h,p);p.setShader(null);p.setStrokeWidth(d);p.setColor(Color.argb(20,65,161,190));for(float x=0;x<w;x+=48*d)c.drawLine(x,0,x,h,p);for(float y=0;y<h;y+=48*d)c.drawLine(0,y,w,y,p);
        p.setColor(Color.rgb(22,55,73));c.drawLine(38*d,116*d,w-38*d,116*d,p);c.drawLine(38*d,h-112*d,w-38*d,h-112*d,p);p.setColor(Color.rgb(64,153,178));float edge=18*d,len=25*d;for(int side=0;side<2;side++){float x=side==0?edge:w-edge;float direction=side==0?1:-1;for(int vertical=0;vertical<2;vertical++){float y=vertical==0?edge:h-edge;float dy=vertical==0?1:-1;c.drawLine(x,y,x+direction*len,y,p);c.drawLine(x,y,x,y+dy*len,p);}}}
}
