package com.happyhome.cc8assistant;

import android.app.*;
import android.content.*;
import android.graphics.*;
import android.graphics.drawable.GradientDrawable;
import android.os.*;
import android.provider.Settings;
import android.view.*;
import android.widget.*;
import java.io.*;
import org.json.JSONObject;

/** Local UI entry only. Never owns microphones, cameras, audio or network sockets. */
public final class DesktopEntryService extends Service {
    private static final String CHANNEL="desktop_entry",PREFS="desktop_entry";
    private static final int NOTIFICATION=701;
    private static final String VISIBLE="ui_visible";
    private WindowManager windows;private View bubble;private WindowManager.LayoutParams placement;
    private Bitmap portrait;private boolean uiVisible,shown;private String error="";

    static boolean enabled(Context context){return context.getSharedPreferences(PREFS,MODE_PRIVATE).getBoolean("enabled",true);}
    static boolean permitted(Context context){return Build.VERSION.SDK_INT<23||Settings.canDrawOverlays(context);}
    static void enabled(Context context,boolean value){
        context.getSharedPreferences(PREFS,MODE_PRIVATE).edit().putBoolean("enabled",value).apply();
        if(value)update(context,true);else context.stopService(new Intent(context,DesktopEntryService.class));
    }
    static void update(Context context,boolean visible){
        if(!enabled(context))return;
        Intent intent=new Intent(context,DesktopEntryService.class).putExtra(VISIBLE,visible);
        try{if(Build.VERSION.SDK_INT>=26)context.startForegroundService(intent);else context.startService(intent);}
        catch(IllegalStateException|SecurityException ignored){}
    }
    static void settings(Activity activity){
        String message=enabled(activity)?"已开启桌面入口。返回桌面后点右侧大黄蜂按钮即可打开助手；可拖动位置。打开后会自动开始语音，请安静等待校准。":"桌面入口已关闭。开启后，返回桌面仍可一键打开助手。";
        if(!permitted(activity))message+="\n\n需要允许显示在其他应用上层，才能显示悬浮按钮。通知入口仍可使用。";
        AlertDialog.Builder dialog=new AlertDialog.Builder(activity).setTitle("大黄蜂桌面入口").setMessage(message)
            .setPositiveButton(enabled(activity)?"关闭入口":"开启入口",(d,w)->enabled(activity,!enabled(activity)))
            .setNegativeButton("返回",null);
        if(!permitted(activity))dialog.setNeutralButton("允许悬浮入口",(d,w)->{
            try{activity.startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,android.net.Uri.parse("package:"+activity.getPackageName())));}
            catch(ActivityNotFoundException|SecurityException error){Toast.makeText(activity,"系统未提供授权页面，请通过已连接的 Mac 配置入口权限",Toast.LENGTH_LONG).show();}
        });
        dialog.show();
    }
    private int dp(float value){return Math.round(value*getResources().getDisplayMetrics().density);}
    private PendingIntent openIntent(){
        Intent intent=new Intent(this,MainActivity.class).putExtra("auto_start_voice",true).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_CLEAR_TOP|Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(this,0,intent,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
    }
    @Override public void onCreate(){
        super.onCreate();windows=(WindowManager)getSystemService(WINDOW_SERVICE);
        NotificationManager manager=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
        if(Build.VERSION.SDK_INT>=26){NotificationChannel channel=new NotificationChannel(CHANNEL,"大黄蜂桌面入口",NotificationManager.IMPORTANCE_LOW);channel.setSound(null,null);channel.enableVibration(false);channel.setShowBadge(false);manager.createNotificationChannel(channel);}
        Notification.Builder builder=Build.VERSION.SDK_INT>=26?new Notification.Builder(this,CHANNEL):new Notification.Builder(this);
        startForeground(NOTIFICATION,builder.setSmallIcon(com.happyhome.cc8assistant.R.drawable.ic_assistant).setContentTitle("大黄蜂 · 打开并开始语音")
            .setContentText("桌面快捷入口 · 点击返回助手").setContentIntent(openIntent()).setOngoing(true).setShowWhen(false).setCategory(Notification.CATEGORY_SERVICE).build());
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(!enabled(this)){stopSelf();return START_NOT_STICKY;}
        if(intent!=null&&intent.hasExtra(VISIBLE))uiVisible=intent.getBooleanExtra(VISIBLE,false);
        refresh();return START_STICKY;
    }
    private void refresh(){
        if(uiVisible||!permitted(this)){hide();return;}
        if(shown)return;
        try{
            LinearLayout entry=new LinearLayout(this);entry.setGravity(Gravity.CENTER);entry.setOrientation(LinearLayout.VERTICAL);
            entry.setPadding(dp(4),dp(3),dp(4),dp(4));entry.setContentDescription("打开大黄蜂并开始语音");
            GradientDrawable background=new GradientDrawable();background.setColor(Color.argb(240,7,26,39));background.setCornerRadius(dp(18));background.setStroke(dp(1),Color.rgb(89,224,244));entry.setBackground(background);
            BitmapFactory.Options options=new BitmapFactory.Options();options.inSampleSize=8;
            try(InputStream source=getAssets().open("jarvis-g1-bumblebee-head.png")){portrait=BitmapFactory.decodeStream(source,null,options);}
            ImageView image=new ImageView(this);image.setImageBitmap(portrait);image.setScaleType(ImageView.ScaleType.FIT_CENTER);entry.addView(image,new LinearLayout.LayoutParams(dp(48),dp(45)));
            TextView label=new TextView(this);label.setText("大黄蜂");label.setTextColor(Color.rgb(217,240,249));label.setTextSize(12);label.setGravity(Gravity.CENTER);entry.addView(label);
            int type=Build.VERSION.SDK_INT>=26?WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY:WindowManager.LayoutParams.TYPE_PHONE;
            placement=new WindowManager.LayoutParams(dp(72),dp(76),type,WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE|WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,PixelFormat.TRANSLUCENT);
            placement.gravity=Gravity.TOP|Gravity.END;placement.x=dp(12);placement.y=getSharedPreferences(PREFS,MODE_PRIVATE).getInt("y",dp(110));
            bubble=entry;entry.setOnTouchListener(new View.OnTouchListener(){
                float startX,startY;int initialY;boolean moved;
                public boolean onTouch(View view,android.view.MotionEvent event){
                    if(event.getActionMasked()==MotionEvent.ACTION_DOWN){startX=event.getRawX();startY=event.getRawY();initialY=placement.y;moved=false;return true;}
                    if(event.getActionMasked()==MotionEvent.ACTION_MOVE){
                        if(Math.abs(event.getRawY()-startY)>dp(8)||Math.abs(event.getRawX()-startX)>dp(8))moved=true;
                        if(moved){int max=Math.max(dp(12),getResources().getDisplayMetrics().heightPixels-dp(100));placement.y=Math.max(dp(12),Math.min(max,initialY+Math.round(event.getRawY()-startY)));try{windows.updateViewLayout(view,placement);}catch(IllegalArgumentException ignored){}}
                        return true;
                    }
                    if(event.getActionMasked()==MotionEvent.ACTION_UP){if(moved)getSharedPreferences(PREFS,MODE_PRIVATE).edit().putInt("y",placement.y).apply();else view.performClick();return true;}
                    return event.getActionMasked()==MotionEvent.ACTION_CANCEL;
                }
            });
            entry.setOnClickListener(v->{try{openIntent().send();}catch(PendingIntent.CanceledException ignored){}});
            windows.addView(entry,placement);shown=true;error="";
        }catch(IOException|RuntimeException failure){error=failure.getClass().getSimpleName();hide();}
    }
    private void hide(){
        if(bubble!=null){try{windows.removeView(bubble);}catch(IllegalArgumentException ignored){}bubble=null;}
        shown=false;portrait=null;
    }
    @Override public void onDestroy(){hide();stopForeground(true);super.onDestroy();}
    @Override public IBinder onBind(Intent intent){return null;}
    @Override protected void dump(FileDescriptor fd,PrintWriter writer,String[] args){
        JSONObject status=new JSONObject();Wire.put(status,"enabled",enabled(this));Wire.put(status,"permission",permitted(this));Wire.put(status,"ui_visible",uiVisible);Wire.put(status,"bubble_visible",shown);Wire.put(status,"error",error);Wire.put(status,"microphone_owned",false);Wire.put(status,"camera_owned",false);
        View entry=bubble;if(entry!=null){int[] location=new int[2];entry.getLocationOnScreen(location);Wire.put(status,"x",location[0]);Wire.put(status,"y",location[1]);Wire.put(status,"width",entry.getWidth());Wire.put(status,"height",entry.getHeight());}
        writer.println("CC8_DESKTOP_ENTRY "+status);
    }
}
