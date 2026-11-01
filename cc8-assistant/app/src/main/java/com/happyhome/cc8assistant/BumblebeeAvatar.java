package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import java.io.IOException;
import java.io.InputStream;

/** Approved transparent head asset, with lightweight eyes and speech overlays. */
final class BumblebeeAvatar {
    // 保留已确认的透明头像原图，仅覆盖眼睑/嘴部；嘴型是播放包络动画，非音素级对齐。
    private final Bitmap head;
    private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG|Paint.FILTER_BITMAP_FLAG);
    private final RectF bounds=new RectF(0,0,1254,1254);
    private final Path leftEye=new Path(),rightEye=new Path(),mouth=new Path();
    BumblebeeAvatar(Context context){
        // Decode once at half resolution: about 1.5 MB, ample for the 330 px HUD.
        BitmapFactory.Options options=new BitmapFactory.Options();options.inSampleSize=2;
        try(InputStream source=context.getAssets().open("jarvis-g1-bumblebee-head.png")){
            head=BitmapFactory.decodeStream(source,null,options);
        }catch(IOException error){throw new IllegalStateException("Missing bundled avatar",error);}
        if(head==null)throw new IllegalStateException("Invalid bundled avatar");
        leftEye.moveTo(405,678);leftEye.lineTo(454,657);leftEye.lineTo(573,685);leftEye.lineTo(592,741);leftEye.lineTo(455,734);leftEye.close();
        rightEye.moveTo(759,771);rightEye.lineTo(797,723);rightEye.lineTo(885,723);rightEye.lineTo(916,769);rightEye.lineTo(879,798);rightEye.close();
    }
    void draw(Canvas canvas,float playback,String mood,long milliseconds){
        paint.setShader(null);paint.setStyle(Paint.Style.FILL);paint.setAlpha(255);
        canvas.drawBitmap(head,null,bounds,paint);
        // Overlays stay within the existing blue eye shapes; helmet and face stay intact.
        float phase=milliseconds%6200;float blink=phase>5970?(float)Math.sin((phase-5970)/230*Math.PI):0;
        float upper="calm".equals(mood)?.22f:"empathetic".equals(mood)?.32f:0;
        float lower="happy".equals(mood)?.22f:0;
        eyelid(canvas,leftEye,650,745,Math.max(blink,upper),lower);
        eyelid(canvas,rightEye,718,800,Math.max(blink,upper),lower);
        // Only actual rendered PCM opens the mouth; idle reproduces the approved smile.
        float opening=Math.min(1,Math.max(0,(playback-.003f)*18))*66;
        if(opening<2)return;
        mouth.reset();mouth.moveTo(520,923);
        mouth.quadTo(624,974-opening*.28f,766,967);
        mouth.quadTo(641,974+opening,520,923);mouth.close();
        paint.setColor(Color.rgb(27,34,46));canvas.drawPath(mouth,paint);
        paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(3);paint.setColor(Color.rgb(95,105,118));canvas.drawPath(mouth,paint);
        paint.setStyle(Paint.Style.FILL);
    }
    private void eyelid(Canvas canvas,Path shape,float top,float bottom,float upper,float lower){
        if(upper<=0&&lower<=0)return;
        canvas.save();canvas.clipPath(shape);paint.setColor(Color.rgb(40,49,64));
        canvas.drawRect(380,top-2,940,top+(bottom-top)*upper,paint);
        if(lower>0)canvas.drawRect(380,bottom-(bottom-top)*lower,940,bottom+2,paint);
        canvas.restore();
    }
}
