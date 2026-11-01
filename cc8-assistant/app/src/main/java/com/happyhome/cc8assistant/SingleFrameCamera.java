package com.happyhome.cc8assistant;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.*;
import android.hardware.Camera;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.io.ByteArrayOutputStream;

/** Visible, one-shot Camera1 capture. No files, no continuous streaming. */
@SuppressWarnings("deprecation")
final class SingleFrameCamera implements TextureView.SurfaceTextureListener {
    interface Result {void image(byte[] jpeg,int width,int height);void error(String reason);}
    private final Activity activity;private final FrameLayout panel;private final TextureView preview;
    private final Handler ui=new Handler(Looper.getMainLooper());private final HandlerThread thread=new HandlerThread("cc8-one-frame-camera");private final Handler worker;
    private volatile SurfaceTexture surface;private volatile boolean wanted,opening;private volatile int serial;private Camera camera;private Result result;
    private volatile boolean cameraOpen;private long captures;private String lastError="";
    SingleFrameCamera(Activity activity,FrameLayout stage,Runnable cancel){
        this.activity=activity;thread.start();worker=new Handler(thread.getLooper());
        panel=new FrameLayout(activity);panel.setBackgroundColor(0xff061521);panel.setVisibility(View.GONE);
        preview=new TextureView(activity);preview.setSurfaceTextureListener(this);panel.addView(preview,new FrameLayout.LayoutParams(-1,-1));
        TextView label=new TextView(activity);label.setText("● 摄像头使用中 · 仅拍一张给 Dot");label.setTextColor(0xffffc36a);label.setTextSize(15);label.setPadding(10,8,10,8);label.setBackgroundColor(0xdd061521);panel.addView(label,new FrameLayout.LayoutParams(-1,-2,Gravity.TOP));
        Button stop=new Button(activity);stop.setText("取消拍摄");stop.setOnClickListener(v->cancel.run());panel.addView(stop,new FrameLayout.LayoutParams(-1,48,Gravity.BOTTOM));
        FrameLayout.LayoutParams lp=new FrameLayout.LayoutParams(360,320,Gravity.TOP|Gravity.LEFT);lp.leftMargin=40;lp.topMargin=155;stage.addView(panel,lp);
    }
    void start(Result callback){
        cancel();result=callback;wanted=true;lastError="";serial++;panel.setVisibility(View.VISIBLE);openIfReady();
        final int token=serial;ui.postDelayed(()->{if(current(token))fail(token,"camera_timeout");},6000);
    }
    boolean busy(){return wanted;}
    boolean cameraOpen(){return cameraOpen;}
    long captures(){return captures;}
    String lastError(){return lastError;}
    private boolean current(int token){return wanted&&serial==token;}
    private void openIfReady(){SurfaceTexture target=surface;if(!wanted||opening||target==null)return;opening=true;final int token=serial;worker.post(()->open(token,target));}
    @SuppressLint("MissingPermission") // Explicit visible button/voice request; runtime permission checked in MainActivity and here.
    private void open(int token,SurfaceTexture target){
        try{
            if(!current(token))return;
            if(activity.checkCallingOrSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED){fail(token,"permission_denied");return;}
            int count=Camera.getNumberOfCameras();if(count==0){fail(token,"camera_unavailable");return;}
            int id=0;Camera.CameraInfo info=new Camera.CameraInfo();for(int i=0;i<count;i++){Camera.getCameraInfo(i,info);if(info.facing==Camera.CameraInfo.CAMERA_FACING_FRONT){id=i;break;}}
            Camera.getCameraInfo(id,info);Camera owned=Camera.open(id);camera=owned;cameraOpen=true;if(!current(token)){release();return;}
            Camera.Parameters p=owned.getParameters();boolean supports=false;for(Camera.Size s:p.getSupportedPreviewSizes())if(s.width==640&&s.height==480)supports=true;
            if(!supports||!p.getSupportedPreviewFormats().contains(ImageFormat.NV21)){fail(token,"unsupported_preview");return;}
            p.setPreviewSize(640,480);p.setPreviewFormat(ImageFormat.NV21);owned.setParameters(p);
            int rotation=activity.getWindowManager().getDefaultDisplay().getRotation();int degrees=rotation==Surface.ROTATION_90?90:rotation==Surface.ROTATION_180?180:rotation==Surface.ROTATION_270?270:0;
            final int upright=info.facing==Camera.CameraInfo.CAMERA_FACING_FRONT?(info.orientation+degrees)%360:(info.orientation-degrees+360)%360;
            owned.setDisplayOrientation(info.facing==Camera.CameraInfo.CAMERA_FACING_FRONT?(360-upright)%360:upright);target.setDefaultBufferSize(640,480);owned.setPreviewTexture(target);
            final long began=SystemClock.elapsedRealtime();final int[] frames={0};
            owned.setErrorCallback((code,ignored)->fail(token,"camera_error_"+code));
            owned.setPreviewCallbackWithBuffer((data,owner)->{
                if(!current(token)||owner!=camera)return;
                if(data==null||data.length<640*480*3/2){fail(token,"invalid_frame");return;}
                frames[0]++;
                if(frames[0]<8||SystemClock.elapsedRealtime()-began<700){try{owned.addCallbackBuffer(data);}catch(RuntimeException error){fail(token,"preview_failed");}return;}
                try{
                    ByteArrayOutputStream out=new ByteArrayOutputStream();boolean ok=new YuvImage(data,ImageFormat.NV21,640,480,null).compressToJpeg(new Rect(0,0,640,480),82,out);if(!ok)throw new IllegalStateException();
                    byte[] jpeg=out.toByteArray();int width=640,height=480;
                    if(upright!=0){Bitmap b=BitmapFactory.decodeByteArray(jpeg,0,jpeg.length);if(b==null)throw new IllegalStateException();Matrix m=new Matrix();m.postRotate(upright);Bitmap rotated=Bitmap.createBitmap(b,0,0,b.getWidth(),b.getHeight(),m,true);out.reset();if(!rotated.compress(Bitmap.CompressFormat.JPEG,82,out))throw new IllegalStateException();width=rotated.getWidth();height=rotated.getHeight();jpeg=out.toByteArray();if(rotated!=b)rotated.recycle();b.recycle();}
                    if(jpeg.length>384*1024)throw new IllegalStateException();
                    release();final byte[] image=jpeg;final int w=width,h=height;
                    ui.post(()->{if(!current(token))return;wanted=false;opening=false;captures++;panel.setVisibility(View.GONE);Result done=result;result=null;if(done!=null)done.image(image,w,h);});
                }catch(Exception error){fail(token,"image_encode_failed");}
            });
            for(int i=0;i<3;i++)owned.addCallbackBuffer(new byte[640*480*3/2]);if(!current(token)){release();return;}owned.startPreview();
        }catch(Exception error){fail(token,"camera_open_failed");}
    }
    private void fail(int token,String reason){
        worker.post(()->{if(!current(token))return;release();ui.post(()->{if(!current(token))return;wanted=false;opening=false;lastError=reason;panel.setVisibility(View.GONE);Result done=result;result=null;if(done!=null)done.error(reason);});});
    }
    private void release(){Camera owned=camera;camera=null;if(owned!=null){try{owned.setPreviewCallbackWithBuffer(null);owned.stopPreview();}catch(RuntimeException ignored){}try{owned.release();}catch(RuntimeException ignored){}}cameraOpen=false;}
    void cancel(){wanted=false;opening=false;serial++;result=null;panel.setVisibility(View.GONE);worker.post(this::release);}
    void destroy(){cancel();thread.quitSafely();}
    @Override public void onSurfaceTextureAvailable(SurfaceTexture texture,int w,int h){surface=texture;openIfReady();}
    @Override public void onSurfaceTextureSizeChanged(SurfaceTexture texture,int w,int h){}
    @Override public boolean onSurfaceTextureDestroyed(SurfaceTexture texture){surface=null;if(wanted)fail(serial,"preview_surface_lost");else worker.post(this::release);return true;}
    @Override public void onSurfaceTextureUpdated(SurfaceTexture texture){}
}
