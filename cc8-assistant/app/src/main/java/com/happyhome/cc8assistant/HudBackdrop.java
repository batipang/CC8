package com.happyhome.cc8assistant;
import android.content.Context;
import android.graphics.*;
import android.view.View;
/** Quiet navy base from the selected design; no extra grid or corner chrome. */
final class HudBackdrop extends View {
 private final Paint paint=new Paint();private Shader background;
 HudBackdrop(Context c){super(c);setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_NO);}
 @Override protected void onSizeChanged(int w,int h,int ow,int oh){background=new LinearGradient(0,0,w,h,new int[]{Color.rgb(0,25,40),Color.rgb(0,16,29)},null,Shader.TileMode.CLAMP);}
 @Override protected void onDraw(Canvas c){paint.setShader(background);c.drawRect(0,0,getWidth(),getHeight(),paint);}
}
