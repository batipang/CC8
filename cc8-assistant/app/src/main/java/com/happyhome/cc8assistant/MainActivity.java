package com.happyhome.cc8assistant;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.ActivityNotFoundException;
import android.net.Uri;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.content.res.ColorStateList;
import android.text.TextUtils;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.io.*;
import org.json.JSONArray;
import org.json.JSONObject;

public final class MainActivity extends Activity implements VoiceSession.Listener {
    private VoiceSession session;private CompanionView companion;private WaveformView waveform;private KaraokeTextView captionText;private View blackScreen;private boolean screenSleeping;private float previousBrightness=-1;private TextView progressText;private ProgressBar captionProgress;private int captionId,lastCaptionLine=-1;private TextView linkText,stateText,privacyText,heardText,sourceText,micText,brainText,dotText;private Button start,interrupt;private Switch wake;
    private static final int INK=Color.rgb(217,240,249),CYAN=Color.rgb(89,224,244),MUTED=Color.rgb(120,156,175),AMBER=Color.rgb(243,184,98);
    private ScrollView captionScroll;
    private String brainProvider="dot",recognitionEngine="minimax",dotResult="";
    private JSONArray webSources=new JSONArray();private String retrievedAt="";
    private boolean foreground,active=false,playbackActive=false,linked=false,pending=false;private String phase="idle",mood="friendly";private final Handler handler=new Handler(Looper.getMainLooper());
    private int dp(float value){return Math.round(value*getResources().getDisplayMetrics().density);}
    private TextView text(String value,int size,int color){TextView t=new TextView(this);t.setText(value);t.setTextSize(size);t.setTextColor(color);return t;}
    private final Runnable animate=new Runnable(){public void run(){
        if(!foreground||screenSleeping)return;float level=session.mouthLevel();VoiceSession.CaptionProgress cp=session.captionProgress();
        companion.update(level,session.microphoneLevel(),level>0?session.mood():mood,phase,linked,active);waveform.update(level,cp.playing,cp.paused);
        if(cp.id>0&&cp.text.equals(captionText.getText().toString())){
            if(captionId!=cp.id){captionId=cp.id;lastCaptionLine=-1;captionScroll.scrollTo(0,0);}
            captionText.progress(cp.position,cp.timed);captionProgress.setVisibility(cp.timed?View.VISIBLE:View.GONE);progressText.setVisibility(cp.timed?View.VISIBLE:View.GONE);
            int progress=Math.round(1000*cp.position/Math.max(1,cp.text.length()));captionProgress.setProgress(progress);progressText.setText((cp.paused?"已暂停  ·  ":"播放进度  ·  ")+progress/10+"%");
            android.text.Layout layout=captionText.getLayout();if(layout!=null){int line=layout.getLineForOffset(Math.min(cp.text.length(),(int)cp.position));if(line!=lastCaptionLine){lastCaptionLine=line;int y=layout.getLineTop(line);if(y>captionScroll.getScrollY()+captionScroll.getHeight()-dp(80)||y<captionScroll.getScrollY())captionScroll.smoothScrollTo(0,Math.max(0,y-dp(45)));}}
        }else{captionText.progress(0,false);captionProgress.setVisibility(View.GONE);progressText.setVisibility(View.GONE);}
        handler.postDelayed(this,33);
    }};
    @Override public void onCreate(Bundle state){super.onCreate(state);getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,WindowManager.LayoutParams.FLAG_FULLSCREEN);getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN|View.SYSTEM_UI_FLAG_HIDE_NAVIGATION|View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);session=new VoiceSession(this,this);build();}
    private GradientDrawable surface(int fill,int stroke){GradientDrawable d=new GradientDrawable();d.setColor(fill);d.setCornerRadius(dp(5));d.setStroke(dp(1),stroke);return d;}
    private TextView mono(String value,int size,int color){TextView t=text(value,size,color);t.setTypeface(Typeface.create("monospace",Typeface.NORMAL));t.setLetterSpacing(.08f);return t;}
    private void buttonStyle(Button button,boolean primary){
        int fill=primary?(active?AMBER:CYAN):Color.rgb(9,28,41);int border=primary?fill:Color.rgb(48,112,137);
        StateListDrawable bg=new StateListDrawable();bg.addState(new int[]{-android.R.attr.state_enabled},surface(Color.rgb(8,21,31),Color.rgb(28,49,63)));bg.addState(new int[]{android.R.attr.state_pressed},surface(Color.rgb(36,92,112),CYAN));bg.addState(new int[]{},surface(fill,border));button.setBackground(bg);
        button.setTextColor(new ColorStateList(new int[][]{new int[]{-android.R.attr.state_enabled},new int[]{}},new int[]{Color.rgb(83,111,126),primary?Color.rgb(3,18,27):INK}));
    }
    private StateListDrawable switchShape(boolean thumb){
        StateListDrawable states=new StateListDrawable();
        for(boolean checked:new boolean[]{true,false}){GradientDrawable d=new GradientDrawable();d.setShape(thumb?GradientDrawable.OVAL:GradientDrawable.RECTANGLE);d.setSize(dp(thumb?20:48),dp(thumb?20:12));d.setCornerRadius(dp(10));d.setColor(thumb?(checked?CYAN:Color.rgb(106,146,166)):(checked?Color.rgb(23,88,111):Color.rgb(24,49,65)));d.setStroke(dp(1),checked?CYAN:Color.rgb(58,105,128));states.addState(checked?new int[]{android.R.attr.state_checked}:new int[]{},d);}return states;
    }
    private void build(){
        FrameLayout stage=new FrameLayout(this);stage.addView(new HudBackdrop(this),new FrameLayout.LayoutParams(-1,-1));setContentView(stage);
        LinearLayout root=new LinearLayout(this);root.setOrientation(LinearLayout.VERTICAL);root.setPadding(dp(40),dp(25),dp(40),dp(25));stage.addView(root,new FrameLayout.LayoutParams(-1,-1));
        LinearLayout top=new LinearLayout(this);top.setGravity(Gravity.CENTER_VERTICAL);LinearLayout brand=new LinearLayout(this);brand.setOrientation(LinearLayout.VERTICAL);
        TextView title=text("BUMBLEBEE",37,INK);title.setTypeface(Typeface.create("sans-serif-light",Typeface.NORMAL));title.setLetterSpacing(.16f);brand.addView(title);TextView sub=mono("大黄蜂  /  PERSONAL ASSISTANT",12,MUTED);sub.setPadding(dp(2),dp(4),0,0);brand.addView(sub);top.addView(brand,new LinearLayout.LayoutParams(0,-2,1));
        LinearLayout badges=new LinearLayout(this);badges.setGravity(Gravity.CENTER_VERTICAL);brainText=mono("模式 · Dot / Codex ▾",13,CYAN);brainText.setPadding(dp(13),dp(12),dp(13),dp(12));brainText.setBackground(surface(Color.rgb(7,30,43),Color.rgb(37,85,105)));brainText.setOnClickListener(v->showBrainSelector());LinearLayout.LayoutParams brainLp=new LinearLayout.LayoutParams(-2,-2);brainLp.rightMargin=dp(12);badges.addView(brainText,brainLp);linkText=mono("自动 · 等待连接",14,MUTED);linkText.setPadding(dp(16),dp(12),dp(16),dp(12));linkText.setBackground(surface(Color.rgb(7,24,36),Color.rgb(30,68,88)));linkText.setOnClickListener(v->showConnectionInfo());badges.addView(linkText);
        micText=text("● 麦克风关闭",15,AMBER);micText.setPadding(dp(18),dp(12),0,dp(12));badges.addView(micText);top.addView(badges);root.addView(top,new LinearLayout.LayoutParams(-1,dp(77)));
        LinearLayout main=new LinearLayout(this);main.setGravity(Gravity.CENTER_VERTICAL);LinearLayout.LayoutParams mainLp=new LinearLayout.LayoutParams(-1,0,1);mainLp.topMargin=dp(29);mainLp.bottomMargin=dp(27);root.addView(main,mainLp);
        LinearLayout core=new LinearLayout(this);core.setOrientation(LinearLayout.VERTICAL);companion=new CompanionView(this);companion.setOnLongClickListener(v->{startActivity(new Intent(this,CameraTestActivity.class));return true;});companion.setContentDescription("大黄蜂头像，长按打开摄像头测试");core.addView(companion,new LinearLayout.LayoutParams(-1,0,1));waveform=new WaveformView(this);LinearLayout.LayoutParams waveLp=new LinearLayout.LayoutParams(-1,dp(110));waveLp.topMargin=dp(-10);core.addView(waveform,waveLp);LinearLayout.LayoutParams coreLp=new LinearLayout.LayoutParams(0,-1,.95f);coreLp.rightMargin=dp(24);main.addView(core,coreLp);
        LinearLayout words=new LinearLayout(this);words.setOrientation(LinearLayout.VERTICAL);words.setPadding(dp(27),dp(25),dp(27),dp(20));words.setBackground(surface(Color.argb(220,6,21,33),Color.rgb(25,61,81)));main.addView(words,new LinearLayout.LayoutParams(0,-1,1.05f));
        TextView eyebrow=mono("DIALOGUE  /  语音交互",12,Color.rgb(86,150,173));words.addView(eyebrow);
        stateText=text("系统待命",22,CYAN);stateText.setPadding(0,dp(16),0,dp(16));words.addView(stateText);
        View rule=new View(this);rule.setBackgroundColor(Color.rgb(22,54,72));words.addView(rule,new LinearLayout.LayoutParams(-1,dp(1)));
        heardText=text("等待你的指令",17,MUTED);heardText.setMaxLines(3);heardText.setEllipsize(TextUtils.TruncateAt.END);heardText.setPadding(0,dp(18),0,dp(10));words.addView(heardText);
        captionScroll=new ScrollView(this);captionScroll.setFillViewport(false);captionScroll.setVerticalScrollBarEnabled(false);captionText=new KaraokeTextView(this);captionText.setText("语音系统已就绪。\n点击「开始语音」即可对话。");captionText.setTextSize(29);captionText.setTextColor(INK);captionText.setLineSpacing(dp(5),1);captionText.setPadding(0,dp(8),0,dp(18));captionScroll.addView(captionText,new ScrollView.LayoutParams(-1,-2));words.addView(captionScroll,new LinearLayout.LayoutParams(-1,0,1));
        captionProgress=new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);captionProgress.setMax(1000);captionProgress.setProgressTintList(ColorStateList.valueOf(CYAN));captionProgress.setProgressBackgroundTintList(ColorStateList.valueOf(Color.rgb(22,54,72)));captionProgress.setVisibility(View.GONE);words.addView(captionProgress,new LinearLayout.LayoutParams(-1,dp(3)));
        progressText=mono("",10,MUTED);progressText.setPadding(0,dp(5),0,dp(9));progressText.setVisibility(View.GONE);words.addView(progressText);
        sourceText=text("",15,CYAN);sourceText.setMaxLines(2);sourceText.setEllipsize(TextUtils.TruncateAt.END);sourceText.setPadding(dp(12),dp(10),dp(12),dp(10));sourceText.setBackground(surface(Color.rgb(8,35,48),Color.rgb(29,86,105)));sourceText.setVisibility(View.GONE);sourceText.setOnClickListener(v->showSources());LinearLayout.LayoutParams sourceLp=new LinearLayout.LayoutParams(-1,-2);sourceLp.bottomMargin=dp(13);words.addView(sourceText,sourceLp);
        dotText=text("Dot 结果 · 点击查看",15,CYAN);dotText.setPadding(dp(12),dp(10),dp(12),dp(10));dotText.setBackground(surface(Color.rgb(8,35,48),Color.rgb(29,86,105)));dotText.setVisibility(View.GONE);dotText.setOnClickListener(v->showDotResult());LinearLayout.LayoutParams dotLp=new LinearLayout.LayoutParams(-1,-2);dotLp.bottomMargin=dp(12);words.addView(dotText,dotLp);
        privacyText=text("录音在本机处理 · 文字与播报使用联网服务\n首次开启请安静等待 6 秒校准",13,Color.rgb(101,140,160));words.addView(privacyText);
        LinearLayout bottom=new LinearLayout(this);bottom.setGravity(Gravity.CENTER_VERTICAL);
        wake=new Switch(this);wake.setText("唤醒 · 大黄蜂 / Bumblebee  ");wake.setTextColor(MUTED);wake.setTextSize(16);wake.setThumbDrawable(switchShape(true));wake.setTrackDrawable(switchShape(false));wake.setSwitchMinWidth(dp(52));wake.setSwitchPadding(dp(14));wake.setMinWidth(dp(350));wake.setMinHeight(dp(48));wake.setChecked(getPreferences(0).getBoolean("wake_mode",true));wake.setOnCheckedChangeListener((button,checked)->{getPreferences(0).edit().putBoolean("wake_mode",checked).apply();session.settings(checked);updatePrivacy();});FrameLayout wakeBox=new FrameLayout(this);FrameLayout.LayoutParams switchLp=new FrameLayout.LayoutParams(-2,-2,Gravity.CENTER_VERTICAL);wakeBox.addView(wake,switchLp);bottom.addView(wakeBox,new LinearLayout.LayoutParams(0,dp(58),1));
        interrupt=new Button(this);interrupt.setAllCaps(false);interrupt.setText("打断播报");interrupt.setTextSize(18);interrupt.setOnClickListener(v->session.interrupt());bottom.addView(interrupt,new LinearLayout.LayoutParams(dp(160),dp(58)));
        start=new Button(this);start.setAllCaps(false);start.setText("开始语音");start.setTextSize(18);LinearLayout.LayoutParams lp=new LinearLayout.LayoutParams(dp(190),dp(58));lp.leftMargin=dp(16);bottom.addView(start,lp);start.setOnClickListener(v->{if(active){session.stopMicrophone();state("idle","麦克风已关闭");caption("语音会话已暂停。\n点击「开始语音」继续。","calm");}else begin();});root.addView(bottom,new LinearLayout.LayoutParams(-1,dp(59)));blackScreen=new View(this);blackScreen.setBackgroundColor(Color.BLACK);blackScreen.setVisibility(View.GONE);blackScreen.setContentDescription("待机黑屏，触摸或说大黄蜂唤醒");blackScreen.setOnClickListener(v->session.screenTouched());stage.addView(blackScreen,new FrameLayout.LayoutParams(-1,-1));refresh();
    }
    private void showConnectionInfo(){
        JSONObject info=session.snapshot();String current="usb".equals(session.transportName())?"USB":"wifi".equals(session.transportName())?"Wi-Fi":"等待连接";
        String message="当前通道："+current+"\n连接策略：USB 优先，断线自动改用 Wi-Fi\n\n"+(info.optBoolean("wifi_ready")?"Wi-Fi 备用连接已就绪":info.optBoolean("wifi_paired")?"Wi-Fi 已配对，正在等待局域网连接":"首次 Wi-Fi 配对请先通过 USB 连接 Mac")+"\n\nWi-Fi 需要两台设备处于同一局域网，且 Mac 语音服务正在运行。";
        new AlertDialog.Builder(this).setTitle("连接状态").setMessage(message).setPositiveButton("知道了",null).show();
    }
    private void showBrainSelector(){new AlertDialog.Builder(this).setTitle("选择助理大脑").setSingleChoiceItems(new String[]{"MiniMax · 对话与联网查询","Dot / Codex · 思考与执行"},"dot".equals(brainProvider)?1:0,(dialog,which)->{session.brain(which==1?"dot":"minimax");dialog.dismiss();}).setNegativeButton("关闭",null).show();}
    private void showDotResult(){if(dotResult.isEmpty())return;TextView detail=text(dotResult,18,INK);detail.setTextIsSelectable(true);detail.setPadding(dp(22),dp(16),dp(22),dp(16));ScrollView scroll=new ScrollView(this);scroll.addView(detail);new AlertDialog.Builder(this).setTitle("Dot / Codex · 任务结果").setView(scroll).setPositiveButton("关闭",null).show();}
    @Override public void brain(String provider){runOnUiThread(()->{brainProvider="dot".equals(provider)?"dot":"minimax";brainText.setText("模式 · "+("dot".equals(brainProvider)?"Dot / Codex":"MiniMax")+" ▾");updatePrivacy();});}
    @Override public void dotResult(String value,boolean truncated){runOnUiThread(()->{dotResult=value;dotText.setVisibility(value.isEmpty()?View.GONE:View.VISIBLE);dotText.setText(truncated?"Dot 结果 · 内容较长，点击查看摘录":"Dot 结果 · 点击查看");});}
    private void begin(){if(!foreground)return;if(!linked){state("idle","请连接 USB 或与 Mac 处于同一局域网");return;}if(Build.VERSION.SDK_INT>=23&&checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){pending=true;requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},21);return;}session.settings(wake.isChecked());session.startMicrophone();}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] results){super.onRequestPermissionsResult(request,permissions,results);if(request==21){boolean allowed=results.length>0&&results[0]==PackageManager.PERMISSION_GRANTED;if(pending&&allowed&&foreground)begin();else if(!allowed)state("idle","未获得麦克风权限，尚未开始采集");pending=false;}}
    private void updatePrivacy(){if(privacyText==null)return;String ears="minimax".equals(recognitionEngine)?"语音片段发送 MiniMax 云端识别":"语音在当前 Mac 本机识别";privacyText.setText(ears+"\n"+("dot".equals(brainProvider)?"Dot 思考":"MiniMax 思考")+" · MiniMax 播报\n"+(!active?"首次开启请安静等待 6 秒校准":wake.isChecked()?"15 分钟黑屏待机 · 大黄蜂 / Bumblebee 唤醒":"自由对话 · 闲置 15 分钟后等待唤醒"));}
    @Override public void recognition(String engine){runOnUiThread(()->{recognitionEngine=engine;updatePrivacy();});}
    private void refresh(){if(start==null)return;updatePrivacy();start.setText(active?"停止语音":"开始语音");start.setEnabled(linked||active);interrupt.setEnabled((active||playbackActive)&&!"calibrating".equals(phase));buttonStyle(start,true);buttonStyle(interrupt,false);brainText.setEnabled(!"calibrating".equals(phase));micText.setText(active?"● 麦克风开启":"● 麦克风关闭");micText.setTextColor(active?CYAN:AMBER);if(foreground)getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);}
    @Override public void link(boolean connected){runOnUiThread(()->{linked=connected;if(!connected)screen(false);String transport="wifi".equals(session.transportName())?"Wi-Fi":"USB";linkText.setText(connected?transport+" · 已连接":"自动 · 等待连接");companion.setTransport(transport);linkText.setTextColor(connected?CYAN:MUTED);refresh();});}
    @Override public void state(String next,String message){runOnUiThread(()->{phase=next;stateText.setText(message);stateText.setTextColor("error".equals(next)?AMBER:CYAN);refresh();});}
    @Override public void caption(String text,String emotion){runOnUiThread(()->{if(!text.equals(captionText.getText().toString())){captionText.setText(text);captionText.progress(0,false);captionScroll.scrollTo(0,0);captionId=0;}mood=emotion;});}
    @Override public void heard(String text){runOnUiThread(()->heardText.setText("你："+text));}
    @Override public void sources(JSONArray items,String time){runOnUiThread(()->{
        webSources=new JSONArray();retrievedAt=time;
        if(items!=null)for(int i=0;i<Math.min(6,items.length());i++){JSONObject item=items.optJSONObject(i);if(item!=null&&safeSource(item.optString("url")))webSources.put(item);}
        sourceText.setVisibility(webSources.length()==0?View.GONE:View.VISIBLE);
        if(webSources.length()>0)sourceText.setText("检索来源 "+webSources.length()+" 条 · 点此查看\n"+webSources.optJSONObject(0).optString("title"));
    });}
    private boolean safeSource(String url){
        if(url.length()>2048||url.matches(".*[\\s\\p{Cntrl}].*"))return false;
        Uri uri=Uri.parse(url);String scheme=uri.getScheme(),host=uri.getHost();
        return ("https".equals(scheme)||"http".equals(scheme))&&host!=null&&host.contains(".")&&uri.getUserInfo()==null;
    }
    private void showSources(){
        final JSONArray current=webSources;if(current.length()==0)return;
        String[] names=new String[current.length()];
        for(int i=0;i<names.length;i++){JSONObject item=current.optJSONObject(i);String published=item.optString("published_at");names[i]=item.optString("title")+"\n"+Uri.parse(item.optString("url")).getHost()+(published.isEmpty()?" · 发布时间未知":" · "+published);}
        new AlertDialog.Builder(this).setTitle("检索时间："+retrievedAt.replace('T',' ')).setItems(names,(dialog,index)->{
            JSONObject item=current.optJSONObject(index);String url=item.optString("url");if(!safeSource(url))return;
            try{startActivity(new Intent(Intent.ACTION_VIEW,Uri.parse(url)));}
            catch(ActivityNotFoundException|SecurityException e){TextView link=text(url,18,INK);link.setTextIsSelectable(true);link.setPadding(dp(24),dp(20),dp(24),dp(20));new AlertDialog.Builder(this).setTitle("设备无法打开浏览器").setView(link).setPositiveButton("知道了",null).show();}
        }).setNegativeButton("关闭",null).show();
    }
    @Override public void playback(boolean value){runOnUiThread(()->{playbackActive=value;refresh();});}
    @Override public void microphone(boolean value){runOnUiThread(()->{active=value;if(!value)screen(false);if(value)heardText.setText("等待你的指令");refresh();});}
    @Override public void screen(boolean sleeping){runOnUiThread(()->{
        if(sleeping&&(!foreground||!active))return;if(screenSleeping==sleeping)return;screenSleeping=sleeping;
        WindowManager.LayoutParams attrs=getWindow().getAttributes();if(sleeping){previousBrightness=attrs.screenBrightness;attrs.screenBrightness=0;blackScreen.setVisibility(View.VISIBLE);handler.removeCallbacks(animate);}
        else{attrs.screenBrightness=previousBrightness;blackScreen.setVisibility(View.GONE);if(foreground){handler.removeCallbacks(animate);handler.post(animate);}}getWindow().setAttributes(attrs);
    });}
    @Override protected void onResume(){super.onResume();foreground=true;getWindow().addFlags(WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);session.settings(wake.isChecked());session.resume();handler.post(animate);refresh();}
    @Override protected void onPause(){screen(false);foreground=false;pending=false;handler.removeCallbacks(animate);session.pause();getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON|WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);phase="idle";companion.update(0,0,"calm","idle",false,false);super.onPause();}
    @Override public void dump(String prefix,FileDescriptor fd,PrintWriter writer,String[] args){writer.println("CC8_ASSISTANT_STATUS "+session.snapshot());JSONObject ui=new JSONObject();Wire.put(ui,"theme","bumblebee_g1_hud");Wire.put(ui,"screen_sleeping",screenSleeping);Wire.put(ui,"waveform_separate",true);Wire.put(ui,"waveform_style","particle_field");Wire.put(ui,"karaoke_progress",captionProgress.getProgress());Wire.put(ui,"brain_provider",brainProvider);Wire.put(ui,"asr_engine",recognitionEngine);Wire.put(ui,"dot_result_available",!dotResult.isEmpty());Wire.put(ui,"wake_width",wake.getWidth());Wire.put(ui,"wake_height",wake.getHeight());Wire.put(ui,"wake_checked",wake.isChecked());Wire.put(ui,"caption_view_height",captionScroll.getHeight());Wire.put(ui,"source_count",webSources.length());writer.println("HUD_UI_STATUS "+ui);}
}
