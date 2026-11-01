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
    private final java.util.ArrayDeque<String> autoVoiceTrace=new java.util.ArrayDeque<>();
    private final OpenVoiceGate autoVoice=new OpenVoiceGate();
    private boolean permissionGrantedAwaitingResume;private int autoVoiceOpens,autoVoiceStarts;private boolean lastOpenWantedVoice;
    private final Runnable autoVoiceTick=()->maybeAutoVoice();
    private SingleFrameCamera eyes;private Button visionButton,wakeButton,freeButton;
    private TaskStepsView taskSteps;private TextView taskAction,reminderText;private ImageView stateIcon;private long workspaceRevision=-1;private JSONArray tasksData=new JSONArray(),remindersData=new JSONArray();private String lastStateMessage="系统待命";
    private VoiceSession session;private CompanionView companion;private WaveformView waveform;private KaraokeTextView captionText;private View blackScreen;private boolean screenSleeping;private float previousBrightness=-1;private TextView progressText;private ProgressBar captionProgress;private int captionId,lastCaptionLine=-1;private TextView linkText,stateText,privacyText,heardText,sourceText,micText,brainText,dotText;private Button start,interrupt;private Switch wake;
    private static final int INK=Color.rgb(217,240,249),CYAN=Color.rgb(67,224,252),MUTED=Color.rgb(120,156,175),AMBER=Color.rgb(255,199,91);
    private CaptionViewport captionScroll;
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
            captionText.progress(cp.position,cp.timed);captionProgress.setVisibility(cp.timed?View.VISIBLE:View.INVISIBLE);progressText.setVisibility(cp.timed?View.VISIBLE:View.GONE);
            int progress=Math.round(1000*cp.position/Math.max(1,cp.text.length()));captionProgress.setProgress(progress);String clockText=playTime(cp.elapsedMs)+(cp.totalMs>0?" / "+playTime(cp.totalMs):"");if(!clockText.contentEquals(progressText.getText()))progressText.setText(clockText);
            android.text.Layout layout=captionText.getLayout();if(layout!=null){int line=layout.getLineForOffset(Math.min(Math.max(0,cp.text.length()-1),(int)cp.position));if(line!=lastCaptionLine){lastCaptionLine=line;captionScroll.revealLine(captionText,line);}}
        }else{captionText.progress(0,false);captionProgress.setVisibility(View.INVISIBLE);progressText.setVisibility(View.INVISIBLE);}
        handler.postDelayed(this,33);
    }};
    @Override public void onCreate(Bundle state){super.onCreate(state);getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,WindowManager.LayoutParams.FLAG_FULLSCREEN);getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN|View.SYSTEM_UI_FLAG_HIDE_NAVIGATION|View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);session=new VoiceSession(this,this);requestOpenVoice(getIntent());build();}
    private GradientDrawable surface(int fill,int stroke){GradientDrawable d=new GradientDrawable();d.setColor(fill);d.setCornerRadius(dp(14));d.setStroke(dp(1),stroke);return d;}
    private TextView mono(String value,int size,int color){TextView t=text(value,size,color);t.setTypeface(Typeface.create("monospace",Typeface.NORMAL));t.setLetterSpacing(.08f);return t;}
    private void buttonStyle(Button button,boolean primary){
        StateListDrawable bg=new StateListDrawable();bg.addState(new int[]{-android.R.attr.state_enabled},rounded(Color.rgb(8,25,39),Color.rgb(24,48,65),18));bg.addState(new int[]{android.R.attr.state_pressed},rounded(Color.rgb(30,72,94),CYAN,18));GradientDrawable normal=new GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,new int[]{Color.rgb(19,48,69),Color.rgb(12,35,53)});normal.setCornerRadius(dp(18));normal.setStroke(dp(1),Color.rgb(52,78,104));bg.addState(new int[]{},normal);button.setBackground(bg);button.setTextColor(new ColorStateList(new int[][]{new int[]{-android.R.attr.state_enabled},new int[]{}},new int[]{Color.rgb(83,111,126),INK}));
    }
    private StateListDrawable switchShape(boolean thumb){
        StateListDrawable states=new StateListDrawable();
        for(boolean checked:new boolean[]{true,false}){GradientDrawable d=new GradientDrawable();d.setShape(thumb?GradientDrawable.OVAL:GradientDrawable.RECTANGLE);d.setSize(dp(thumb?20:48),dp(thumb?20:12));d.setCornerRadius(dp(10));d.setColor(thumb?(checked?CYAN:Color.rgb(106,146,166)):(checked?Color.rgb(23,88,111):Color.rgb(24,49,65)));d.setStroke(dp(1),checked?CYAN:Color.rgb(58,105,128));states.addState(checked?new int[]{android.R.attr.state_checked}:new int[]{},d);}return states;
    }
    private String playTime(long milliseconds){long seconds=milliseconds/1000;return String.format(java.util.Locale.US,"%02d:%02d",seconds/60,seconds%60);}
    private GradientDrawable rounded(int fill,int border,int radius){GradientDrawable d=surface(fill,border);d.setCornerRadius(dp(radius));return d;}
    private android.graphics.drawable.Drawable icon(int resource,int tint,int size){android.graphics.drawable.Drawable d=getDrawable(resource).mutate();d.setTint(tint);d.setBounds(0,0,dp(size),dp(size));return d;}
    private Button action(String title,int resource){
        Button b=new Button(this);b.setText(title);b.setAllCaps(false);b.setTextSize(22);b.setMinHeight(0);b.setMinWidth(0);b.setStateListAnimator(null);b.setPadding(dp(12),0,dp(12),0);b.setGravity(Gravity.CENTER);
        if(resource!=0){b.setCompoundDrawables(icon(resource,INK,28),null,null,null);b.setCompoundDrawablePadding(dp(11));}
        b.addOnLayoutChangeListener((v,l,t,r,bot,ol,ot,orr,ob)->{int group=(int)Math.ceil(b.getPaint().measureText(b.getText().toString()))+(resource==0?0:dp(39));int pad=Math.max(dp(6),(r-l-group)/2);if(b.getPaddingLeft()!=pad)b.setPadding(pad,0,pad,0);});buttonStyle(b,false);return b;
    }
    private void place(FrameLayout parent,View view,float x,float y,float w,float h){FrameLayout.LayoutParams lp=new FrameLayout.LayoutParams(dp(w),dp(h));lp.leftMargin=dp(x);lp.topMargin=dp(y);parent.addView(view,lp);}
    private void rule(FrameLayout parent,float x,float y,float w,float h){View line=new View(this);line.setBackgroundColor(Color.rgb(36,75,96));place(parent,line,x,y,w,h);}
    private TextView label(String value,int size,int color){TextView t=text(value,size,color);t.setGravity(Gravity.CENTER_VERTICAL);t.setIncludeFontPadding(false);return t;}
    private void build(){
        FrameLayout stage=new FrameLayout(this);stage.addView(new HudBackdrop(this),new FrameLayout.LayoutParams(-1,-1));setContentView(stage);
        FrameLayout root=new FrameLayout(this);root.setClipChildren(false);stage.addView(root,new FrameLayout.LayoutParams(dp(1280),dp(800)));
        stage.addOnLayoutChangeListener((v,l,t,r,b,ol,ot,orr,ob)->{float scale=Math.min(stage.getWidth()/(float)dp(1280),stage.getHeight()/(float)dp(800));root.setPivotX(0);root.setPivotY(0);root.setScaleX(scale);root.setScaleY(scale);root.setTranslationX((stage.getWidth()-dp(1280)*scale)/2);root.setTranslationY((stage.getHeight()-dp(800)*scale)/2);});
        TextView title=label("BUMBLEBEE",36,AMBER);title.setLetterSpacing(.16f);place(root,title,35,15,268,47);rule(root,308,25,1,28);
        place(root,label("大黄蜂 · 个人助理",21,INK),331,20,260,39);
        brainText=label("Dot 已连接",20,INK);brainText.setCompoundDrawables(icon(R.drawable.ic_hud_connection,CYAN,24),null,null,null);brainText.setCompoundDrawablePadding(dp(9));brainText.setOnClickListener(v->showBrainSelector());place(root,brainText,838,15,146,48);rule(root,985,26,1,26);
        linkText=label("未连接",20,INK);linkText.setCompoundDrawables(icon(R.drawable.ic_hud_usb,INK,25),null,null,null);linkText.setCompoundDrawablePadding(dp(9));linkText.setOnClickListener(v->showConnectionInfo());linkText.setSingleLine(true);place(root,linkText,991,15,121,48);rule(root,1114,26,1,26);
        micText=label("麦克风关闭",20,AMBER);micText.setCompoundDrawables(icon(R.drawable.ic_hud_mic,AMBER,29),null,null,null);micText.setCompoundDrawablePadding(dp(7));micText.setSingleLine(true);place(root,micText,1125,14,140,50);rule(root,25,69,1230,1);
        companion=new CompanionView(this);companion.setOnLongClickListener(v->{startActivity(new Intent(this,CameraTestActivity.class));return true;});place(root,companion,25,75,550,456);
        waveform=new WaveformView(this);place(root,waveform,24,520,528,112);
        LinearLayout status=new LinearLayout(this);status.setGravity(Gravity.CENTER);stateIcon=new ImageView(this);stateIcon.setImageDrawable(icon(R.drawable.ic_hud_voice,AMBER,24));status.addView(stateIcon,new LinearLayout.LayoutParams(dp(24),dp(28)));
        stateText=label("系统待命",20,CYAN);stateText.setSingleLine(true);stateText.setEllipsize(TextUtils.TruncateAt.END);LinearLayout.LayoutParams statusTextLp=new LinearLayout.LayoutParams(-2,-1);statusTextLp.leftMargin=dp(9);status.addView(stateText,statusTextLp);status.setOnClickListener(v->Toast.makeText(this,lastStateMessage,Toast.LENGTH_LONG).show());place(root,status,24,629,528,38);
        TextView eyebrow=label("对话",23,INK);eyebrow.setCompoundDrawables(icon(R.drawable.ic_hud_chat,INK,27),null,null,null);eyebrow.setCompoundDrawablePadding(dp(14));place(root,eyebrow,584,114,220,37);
        heardText=label("等待你的指令",24,MUTED);heardText.setMaxLines(2);heardText.setEllipsize(TextUtils.TruncateAt.END);place(root,heardText,587,168,666,54);
        captionScroll=new CaptionViewport(this);captionScroll.setFillViewport(false);captionScroll.setVerticalScrollBarEnabled(false);captionText=new KaraokeTextView(this);captionText.setText(autoVoice.waiting(SystemClock.elapsedRealtime())?"连接后自动开始语音。\n请安静等待校准。":"麦克风已关闭。\n点击开始语音即可对话。");captionText.setTextSize(42);captionText.setTypeface(Typeface.DEFAULT_BOLD);captionText.setTextColor(INK);captionText.setLineSpacing(dp(13),1.05f);captionText.setIncludeFontPadding(false);captionText.setPadding(0,dp(6),0,dp(16));captionScroll.addView(captionText,new ScrollView.LayoutParams(-1,-2));place(root,captionScroll,587,229,665,153);
        captionProgress=new PlaybackProgressView(this);captionProgress.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);captionProgress.setMax(1000);captionProgress.setVisibility(View.INVISIBLE);place(root,captionProgress,588,390,524,24);
        progressText=label("",18,MUTED);progressText.setGravity(Gravity.CENTER_VERTICAL|Gravity.END);progressText.setSingleLine(true);place(root,progressText,1125,387,126,31);
        sourceText=label("",18,CYAN);sourceText.setGravity(Gravity.CENTER_VERTICAL|Gravity.END);sourceText.setSingleLine(true);sourceText.setEllipsize(TextUtils.TruncateAt.END);sourceText.setVisibility(View.GONE);sourceText.setOnClickListener(v->showSources());place(root,sourceText,991,114,261,37);
        rule(root,584,442,668,1);rule(root,1095,468,1,96);
        ImageView taskIcon=new ImageView(this);taskIcon.setImageDrawable(icon(R.drawable.ic_hud_task,CYAN,31));place(root,taskIcon,585,465,31,35);
        dotText=label("当前任务 · 暂无任务",24,INK);dotText.setTypeface(Typeface.DEFAULT_BOLD);dotText.setSingleLine(true);dotText.setEllipsize(TextUtils.TruncateAt.END);dotText.setOnClickListener(v->showTasks());place(root,dotText,632,463,446,43);
        taskSteps=new TaskStepsView(this);taskSteps.setOnClickListener(v->showTasks());place(root,taskSteps,584,513,540,43);
        taskAction=label("查看任务  ›",22,CYAN);taskAction.setGravity(Gravity.CENTER);taskAction.setOnClickListener(v->showTasks());place(root,taskAction,1116,477,137,78);
        rule(root,584,583,668,1);rule(root,1095,600,1,47);
        ImageView reminderIcon=new ImageView(this);reminderIcon.setImageDrawable(icon(R.drawable.ic_hud_bell,INK,32));reminderIcon.setOnClickListener(v->showReminders());place(root,reminderIcon,590,605,32,36);
        reminderText=label("暂无提醒 · 点击添加",23,MUTED);reminderText.setSingleLine(true);reminderText.setEllipsize(TextUtils.TruncateAt.END);reminderText.setOnClickListener(v->showReminders());place(root,reminderText,642,600,438,48);
        TextView reminderAction=label("查看提醒  ›",22,CYAN);reminderAction.setGravity(Gravity.CENTER);reminderAction.setOnClickListener(v->showReminders());place(root,reminderAction,1117,600,136,48);
        privacyText=text("",20,MUTED);
        FrameLayout dock=new FrameLayout(this);dock.setBackground(rounded(Color.rgb(3,24,39),Color.rgb(29,70,95),22));place(root,dock,24,674,1232,92);
        FrameLayout modes=new FrameLayout(this);modes.setBackground(rounded(Color.rgb(15,42,61),Color.rgb(50,78,104),35));place(root,modes,36,686,440,68);
        wake=new Switch(this);wake.setChecked(getPreferences(0).getBoolean("wake_mode",true));wake.setOnCheckedChangeListener((button,checked)->{getPreferences(0).edit().putBoolean("wake_mode",checked).apply();session.settings(checked);refreshModes();updatePrivacy();});
        wakeButton=action("唤醒模式",R.drawable.ic_hud_chat);freeButton=action("自由对话",R.drawable.ic_hud_chat);wakeButton.setOnClickListener(v->wake.setChecked(true));freeButton.setOnClickListener(v->wake.setChecked(false));place(modes,wakeButton,0,0,213,68);place(modes,freeButton,213,0,227,68);rule(root,476,688,1,64);
        visionButton=action("看一看",R.drawable.ic_hud_camera);visionButton.setContentDescription("拍一张画面交给 Dot 分析");visionButton.setOnClickListener(v->{if(session.isVisual()){eyes.cancel();session.cancelVision();}else if(Build.VERSION.SDK_INT>=23&&checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED){requestPermissions(new String[]{Manifest.permission.CAMERA},34);}else session.requestVision();});place(root,visionButton,499,686,177,68);
        Button tasks=action("任务",R.drawable.ic_hud_task);tasks.setOnClickListener(v->showTasks());place(root,tasks,694,686,151,68);
        start=action("开始语音",R.drawable.ic_hud_pause);start.setOnClickListener(v->toggleVoice());place(root,start,863,686,198,68);
        Button settings=action("设置",R.drawable.ic_hud_settings);settings.setOnClickListener(v->showSettings());place(root,settings,1079,686,164,68);
        interrupt=new Button(this);interrupt.setOnClickListener(v->session.interrupt());
        blackScreen=new View(this);blackScreen.setBackgroundColor(Color.BLACK);blackScreen.setVisibility(View.GONE);blackScreen.setContentDescription("待机黑屏，触摸或说大黄蜂唤醒");blackScreen.setOnClickListener(v->session.screenTouched());stage.addView(blackScreen,new FrameLayout.LayoutParams(-1,-1));eyes=new SingleFrameCamera(this,stage,()->session.cancelVision());refresh();renderWorkspace();
    }
    private void statusLabel(){if(stateText==null)return;String message="speaking".equals(phase)?"正在播报 · "+(active?"开口即可打断":"点击停止播报"):"thinking".equals(phase)?"正在处理 · "+(active?"可开口打断":"点击停止任务"):"listening".equals(phase)?"正在倾听 · 大黄蜂 / Bumblebee":"calibrating".equals(phase)?"正在校准 · 请保持安静":lastStateMessage;
        android.text.SpannableString value=new android.text.SpannableString(message);int stop=message.indexOf('·');value.setSpan(new android.text.style.ForegroundColorSpan("speaking".equals(phase)?AMBER:"error".equals(phase)?AMBER:CYAN),0,stop<0?message.length():stop,android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);stateText.setTextColor(INK);stateText.setText(value);stateIcon.setImageDrawable(icon(R.drawable.ic_hud_voice,"speaking".equals(phase)?AMBER:CYAN,24));
    }
    private void toggleVoice(){if(!active&&playbackActive){session.interrupt();return;}traceAuto("button");boolean waiting=autoVoice.waiting(SystemClock.elapsedRealtime());autoVoice.cancel();handler.removeCallbacks(autoVoiceTick);if(active){session.stopMicrophone();state("idle","麦克风已关闭");caption("语音会话已暂停。\n点击开始语音继续。","calm");}else if(waiting&&!linked){state("idle","已取消自动开始，请连接后手动开启语音");}else begin();}
    private void refreshModes(){if(wakeButton==null)return;for(Button b:new Button[]{wakeButton,freeButton}){boolean chosen=b==wakeButton?wake.isChecked():!wake.isChecked();if(chosen){GradientDrawable fill=new GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,new int[]{Color.rgb(255,208,104),Color.rgb(255,191,76)});fill.setCornerRadius(dp(35));b.setBackground(fill);}else b.setBackgroundColor(Color.TRANSPARENT);b.setTextColor(chosen?Color.rgb(3,22,35):INK);b.setTypeface(chosen?Typeface.DEFAULT_BOLD:Typeface.DEFAULT);b.setCompoundDrawables(icon(chosen?R.drawable.ic_hud_chat_fill:R.drawable.ic_hud_chat,chosen?Color.rgb(3,22,35):INK,28),null,null,null);}}
    @Override public void announcement(){runOnUiThread(()->{heardText.setText("指定播报 · 麦克风状态保持不变");webSources=new JSONArray();sourceText.setVisibility(View.GONE);});}
    @Override public void workspace(JSONObject data){runOnUiThread(()->{
        long revision=data.optLong("revision",-1);if(revision<workspaceRevision)return;
        JSONArray tasks=data.optJSONArray("tasks"),reminders=data.optJSONArray("reminders");
        if(tasks==null||reminders==null||tasks.length()>12||reminders.length()>32)return;
        workspaceRevision=revision;tasksData=tasks;remindersData=reminders;renderWorkspace();
    });}
    @Override public void workspaceResult(boolean ok,String message){runOnUiThread(()->Toast.makeText(this,message,Toast.LENGTH_LONG).show());}
    private String taskStatus(String status){return "completed".equals(status)?"已完成":"failed".equals(status)?"未完成":"cancelled".equals(status)?"已停止":"处理中";}
    private void renderWorkspace(){
        if(dotText==null)return;JSONObject latest=tasksData.optJSONObject(0);taskSteps.task(latest);
        dotText.setText(latest==null?(linked?"当前任务 · 暂无任务":"当前任务 · 等待连接"):"当前任务 · "+latest.optString("title"));taskAction.setText("查看任务  ›");
        JSONObject reminder=remindersData.optJSONObject(0);reminderText.setText(reminder==null?"暂无提醒 · 点击添加":dateLabel(reminder.optString("due"))+"  "+reminder.optString("title"));reminderText.setTextColor(reminder==null?MUTED:INK);
    }
    private String dateLabel(String due){
        if(due.length()<16)return due;java.util.Calendar day=java.util.Calendar.getInstance(java.util.TimeZone.getTimeZone("Asia/Shanghai"));java.text.SimpleDateFormat f=new java.text.SimpleDateFormat("yyyy-MM-dd",java.util.Locale.US);f.setTimeZone(day.getTimeZone());String today=f.format(day.getTime());day.add(java.util.Calendar.DAY_OF_MONTH,1);String tomorrow=f.format(day.getTime());String prefix=due.startsWith(today)?"今天":due.startsWith(tomorrow)?"明天":due.substring(5,10).replace('-','/');return prefix+" "+due.substring(11,16);
    }
    private void inputTask(){EditText input=new EditText(this);input.setTextSize(22);input.setHint("输入要交给大黄蜂的指令");input.setTextColor(INK);input.setHintTextColor(MUTED);input.setPadding(dp(24),dp(18),dp(24),dp(18));input.setFilters(new android.text.InputFilter[]{new android.text.InputFilter.LengthFilter(6000)});AlertDialog dialog=new AlertDialog.Builder(this).setTitle("文字指令 · 不开启麦克风").setView(input).setPositiveButton("发送",null).setNegativeButton("取消",null).create();dialog.setOnShowListener(d->dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{String value=input.getText().toString().trim();if(value.isEmpty()){input.setError("请输入指令");return;}if(!linked){input.setError("Mac 尚未连接");return;}session.requestText(value);dialog.dismiss();}));dialog.show();}
    private void showTasks(){
        if(linked)session.workspaceGet();
        if(tasksData.length()==0){new AlertDialog.Builder(this).setTitle("任务").setMessage("还没有任务记录。你可以让大黄蜂查询资料、分析照片或操作当前 Mac。任务记录仅在本次 Mac 服务运行期间保留。").setPositiveButton("文字指令",(d,n)->inputTask()).setNegativeButton("关闭",null).show();return;}
        final JSONArray tasks=tasksData;String[] names=new String[tasks.length()];for(int i=0;i<names.length;i++){JSONObject t=tasks.optJSONObject(i);names[i]=taskStatus(t.optString("status"))+" · "+t.optString("title");}
        new AlertDialog.Builder(this).setTitle("最近任务 · 本次服务").setItems(names,(d,n)->showTaskDetail(tasks.optJSONObject(n))).setNeutralButton("提醒",(d,n)->showReminders()).setPositiveButton("文字指令",(d,n)->inputTask()).setNegativeButton("关闭",null).show();
    }
    private void showTaskDetail(JSONObject task){
        StringBuilder body=new StringBuilder(task.optString("stage"));JSONArray steps=task.optJSONArray("steps");
        if(steps!=null)for(int i=0;i<steps.length();i++){JSONObject step=steps.optJSONObject(i);body.append("\n\n").append(step.optString("title")).append(" · ").append(taskStatus(step.optString("status")));}
        String result=task.optString("result");if(!result.isEmpty())body.append("\n\n结果摘录：\n").append(result);
        TextView detail=text(body.toString(),22,INK);detail.setTextIsSelectable(true);detail.setPadding(dp(24),dp(18),dp(24),dp(18));ScrollView scroll=new ScrollView(this);scroll.addView(detail);
        AlertDialog.Builder dialog=new AlertDialog.Builder(this).setTitle(task.optString("title")).setView(scroll).setNegativeButton("关闭",null);
        if("running".equals(task.optString("status"))&&linked)dialog.setPositiveButton("停止任务",(d,n)->session.cancelTask(task.optString("id")));
        dialog.show();
    }
    private void showReminders(){
        if(linked)session.workspaceGet();final JSONArray items=remindersData;String[] names=new String[items.length()];
        for(int i=0;i<names.length;i++){JSONObject item=items.optJSONObject(i);names[i]=dateLabel(item.optString("due"))+" · "+item.optString("title")+"\n"+repeatLabel(item.optString("repeat"));}
        AlertDialog.Builder dialog=new AlertDialog.Builder(this).setTitle("提醒 · 北京时间").setNegativeButton("关闭",null);
        if(names.length>0)dialog.setItems(names,(d,n)->reminderOptions(items.optJSONObject(n)));
        else dialog.setMessage(linked?"暂无提醒。Mac 服务运行、CC8 前台连接时可到时播报；离线期间不会开启设备或麦克风。":"连接 Mac 后可管理提醒。");
        if(linked)dialog.setPositiveButton("添加提醒",(d,n)->editReminder(null));dialog.show();
    }
    private String repeatLabel(String value){return "daily".equals(value)?"每天":"weekdays".equals(value)?"工作日":"weekly".equals(value)?"每周":"仅一次";}
    private void reminderOptions(JSONObject item){new AlertDialog.Builder(this).setTitle(item.optString("title")).setItems(new String[]{"修改提醒","稍后 10 分钟","取消提醒"},(d,n)->{
        if(!linked){Toast.makeText(this,"请先连接 Mac",Toast.LENGTH_SHORT).show();return;}
        if(n==0){editReminder(item);return;}JSONObject args=Wire.event("unused");args.remove("event");Wire.put(args,"action",n==1?"update":"cancel");Wire.put(args,"id",item.optString("id"));
        if(n==1)Wire.put(args,"due",formatDue(System.currentTimeMillis()+600000));session.reminderAction(args);
    }).setNegativeButton("关闭",null).show();}
    private String formatDue(long millis){java.text.SimpleDateFormat f=new java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss",java.util.Locale.US);f.setTimeZone(java.util.TimeZone.getTimeZone("Asia/Shanghai"));return f.format(new java.util.Date(millis))+"+08:00";}
    private void editReminder(JSONObject item){
        LinearLayout form=new LinearLayout(this);form.setOrientation(LinearLayout.VERTICAL);form.setPadding(dp(24),dp(16),dp(24),dp(8));
        EditText title=new EditText(this);title.setTextColor(INK);title.setTextSize(22);title.setHintTextColor(MUTED);title.setHint("提醒内容");title.setSingleLine(true);title.setFilters(new android.text.InputFilter[]{new android.text.InputFilter.LengthFilter(120)});if(item!=null)title.setText(item.optString("title"));form.addView(title);
        java.util.Calendar date=java.util.Calendar.getInstance(java.util.TimeZone.getTimeZone("Asia/Shanghai"));date.add(java.util.Calendar.HOUR_OF_DAY,1);date.set(java.util.Calendar.SECOND,0);date.set(java.util.Calendar.MILLISECOND,0);
        if(item!=null)try{java.text.SimpleDateFormat f=new java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss",java.util.Locale.US);f.setTimeZone(date.getTimeZone());date.setTime(f.parse(item.optString("due").substring(0,19)));}catch(java.text.ParseException|IndexOutOfBoundsException ignored){}
        Button time=action(dateLabel(formatDue(date.getTimeInMillis())),android.R.drawable.ic_menu_today);form.addView(time,new LinearLayout.LayoutParams(-1,dp(58)));time.setOnClickListener(v->new android.app.DatePickerDialog(this,(picker,y,m,d)->{
            date.set(y,m,d);new android.app.TimePickerDialog(this,(clock,h,min)->{date.set(java.util.Calendar.HOUR_OF_DAY,h);date.set(java.util.Calendar.MINUTE,min);time.setText(dateLabel(formatDue(date.getTimeInMillis())));},date.get(java.util.Calendar.HOUR_OF_DAY),date.get(java.util.Calendar.MINUTE),true).show();
        },date.get(java.util.Calendar.YEAR),date.get(java.util.Calendar.MONTH),date.get(java.util.Calendar.DAY_OF_MONTH)).show());
        Spinner repeat=new Spinner(this);String[] keys={"none","daily","weekdays","weekly"};repeat.setAdapter(new ArrayAdapter<>(this,android.R.layout.simple_spinner_dropdown_item,new String[]{"仅一次","每天","工作日","每周"}));if(item!=null)for(int i=0;i<keys.length;i++)if(keys[i].equals(item.optString("repeat")))repeat.setSelection(i);form.addView(repeat,new LinearLayout.LayoutParams(-1,dp(54)));
        TextView note=text("Mac 服务运行、CC8 前台连接时播报。修改仅在收到 Mac 保存回执后生效。",19,INK);form.addView(note);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle(item==null?"添加提醒":"修改提醒").setView(form).setPositiveButton("保存",null).setNegativeButton("取消",null).create();dialog.setOnShowListener(d->dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{
            String value=title.getText().toString().trim();if(value.isEmpty()){title.setError("请输入提醒内容");return;}if(date.getTimeInMillis()<=System.currentTimeMillis()){Toast.makeText(this,"请选择将来的时间",Toast.LENGTH_LONG).show();return;}if(!linked){Toast.makeText(this,"Mac 尚未连接，未保存",Toast.LENGTH_LONG).show();return;}
            JSONObject args=new JSONObject();Wire.put(args,"action",item==null?"create":"update");if(item!=null)Wire.put(args,"id",item.optString("id"));Wire.put(args,"title",value);Wire.put(args,"due",formatDue(date.getTimeInMillis()));Wire.put(args,"repeat",keys[repeat.getSelectedItemPosition()]);session.reminderAction(args);dialog.dismiss();
        }));dialog.show();
    }
    private void showSettings(){new AlertDialog.Builder(this).setTitle("大黄蜂设置").setItems(new String[]{"媒体音量","助理大脑","提醒管理","检索来源","最近 Dot 完整结果","连接与桌面入口","隐私与语音说明","立即打断播报"},(d,n)->{
        if(n==0){android.media.AudioManager audio=(android.media.AudioManager)getSystemService(AUDIO_SERVICE);SeekBar volume=new SeekBar(this);volume.setMax(audio.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC));volume.setProgress(audio.getStreamVolume(android.media.AudioManager.STREAM_MUSIC));volume.setPadding(dp(24),dp(18),dp(24),dp(18));volume.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener(){public void onProgressChanged(SeekBar bar,int p,boolean user){if(user)audio.setStreamVolume(android.media.AudioManager.STREAM_MUSIC,p,0);}public void onStartTrackingTouch(SeekBar b){}public void onStopTrackingTouch(SeekBar b){}});new AlertDialog.Builder(this).setTitle("媒体音量").setView(volume).setPositiveButton("完成",null).show();}
        else if(n==1)showBrainSelector();else if(n==2)showReminders();else if(n==3){if(webSources.length()>0)showSources();else Toast.makeText(this,"本轮暂无检索来源",Toast.LENGTH_SHORT).show();}else if(n==4){if(dotResult.isEmpty())Toast.makeText(this,"暂无 Dot 结果",Toast.LENGTH_SHORT).show();else showDotResult();}else if(n==5)showConnectionInfo();else if(n==6){updatePrivacy();new AlertDialog.Builder(this).setTitle("隐私与语音说明").setMessage(privacyText.getText()).setPositiveButton("知道了",null).show();}else session.interrupt();
    }).setNegativeButton("关闭",null).show();}
    @Override public void vision(boolean busy,String message){runOnUiThread(()->{if(!busy&&eyes!=null)eyes.cancel();if(visionButton!=null){visionButton.setText(busy?"停止看图":"看一看");visionButton.setContentDescription(busy?"停止拍摄和 Dot 看图分析":"拍一张画面交给 Dot 分析");}state(busy?"thinking":active?"listening":"idle",message);});}
    @Override public void cameraCapture(JSONObject request){runOnUiThread(()->{
        if(!foreground||!session.isVisionRequestCurrent(request)){session.cameraFailed(request,"not_foreground");return;}
        if(Build.VERSION.SDK_INT>=23&&checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED){session.cameraFailed(request,"permission_required");state("idle","请点看一看允许摄像头权限，然后重试");return;}
        eyes.start(new SingleFrameCamera.Result(){public void image(byte[] jpeg,int width,int height){if(foreground&&session.isVisionRequestCurrent(request)){state("thinking","摄像头已关闭 · 正在把本次画面交给 Dot");session.imageReady(request,jpeg,width,height);}}public void error(String reason){session.cameraFailed(request,reason);}});
    });}
    private void showConnectionInfo(){
        JSONObject info=session.snapshot();String current="usb".equals(session.transportName())?"USB":"wifi".equals(session.transportName())?"Wi-Fi":"等待连接";
        String message="当前通道："+current+"\n连接策略：USB 优先，断线自动改用 Wi-Fi\n\n"+(info.optBoolean("wifi_ready")?"Wi-Fi 备用连接已就绪":info.optBoolean("wifi_paired")?"Wi-Fi 已配对，正在等待局域网连接":"首次 Wi-Fi 配对请先通过 USB 连接 Mac")+"\n\nWi-Fi 需要两台设备处于同一局域网，且 Mac 语音服务正在运行。";
        new AlertDialog.Builder(this).setTitle("连接状态").setMessage(message).setNeutralButton("桌面入口",(dialog,which)->DesktopEntryService.settings(this)).setPositiveButton("知道了",null).show();
    }
    private void showBrainSelector(){new AlertDialog.Builder(this).setTitle("选择助理大脑").setSingleChoiceItems(new String[]{"MiniMax · 对话与联网查询","Dot / Codex · 思考与执行"},"dot".equals(brainProvider)?1:0,(dialog,which)->{session.brain(which==1?"dot":"minimax");dialog.dismiss();}).setNegativeButton("关闭",null).show();}
    private void showDotResult(){if(dotResult.isEmpty())return;TextView detail=text(dotResult,22,INK);detail.setTextIsSelectable(true);detail.setPadding(dp(22),dp(16),dp(22),dp(16));ScrollView scroll=new ScrollView(this);scroll.addView(detail);new AlertDialog.Builder(this).setTitle("Dot / Codex · 任务结果").setView(scroll).setPositiveButton("关闭",null).show();}
    @Override public void brain(String provider){runOnUiThread(()->{brainProvider="dot".equals(provider)?"dot":"minimax";brainText.setText("dot".equals(brainProvider)?(linked?"Dot 已连接":"Dot 未连接"):"MiniMax");updatePrivacy();});}
    @Override public void dotResult(String value,boolean truncated){runOnUiThread(()->{dotResult=value;});}
    private void traceAuto(String event){
        long now=SystemClock.elapsedRealtime();String value=event+" fg="+foreground+" link="+linked+" mic="+(session!=null&&session.isRecording())+" wait="+autoVoice.waiting(now)+" staged="+autoVoice.staged();
        synchronized(autoVoiceTrace){if(autoVoiceTrace.size()>=32)autoVoiceTrace.removeFirst();autoVoiceTrace.addLast(value);}
    }
    private void requestOpenVoice(Intent intent){
        pending=false;permissionGrantedAwaitingResume=false;
        lastOpenWantedVoice=intent==null||intent.getBooleanExtra("auto_start_voice",true);autoVoiceOpens++;
        if(lastOpenWantedVoice)autoVoice.opened(SystemClock.elapsedRealtime());else autoVoice.cancel();traceAuto("open:"+lastOpenWantedVoice);
    }
    private void maybeAutoVoice(){
        handler.removeCallbacks(autoVoiceTick);if(!foreground)return;
        long now=SystemClock.elapsedRealtime();
        if(autoVoice.expire(now)){state("idle","连接超时，请连接后点击开始语音");return;}
        if(autoVoice.take(now,true,linked&&session.isConnected(),session.isRecording())){autoVoiceStarts++;traceAuto("auto_take");begin();return;}
        if(autoVoice.waiting(now)){traceAuto("await");state("idle","正在连接 Mac，连接后将自动开始语音");handler.postDelayed(autoVoiceTick,500);}
    }
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);requestOpenVoice(intent);}
    private void begin(){traceAuto("begin");if(!foreground)return;if(!linked){state("idle","请连接 USB 或与 Mac 处于同一局域网");return;}autoVoice.cancel();handler.removeCallbacks(autoVoiceTick);if(Build.VERSION.SDK_INT>=23&&checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){pending=true;requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},21);return;}session.settings(wake.isChecked());session.startMicrophone();}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] results){super.onRequestPermissionsResult(request,permissions,results);if(request==34){state("idle",results.length>0&&results[0]==PackageManager.PERMISSION_GRANTED?"摄像头已授权，请再点看一看":"未获得摄像头权限，未采集图片");}if(request==21){boolean allowed=results.length>0&&results[0]==PackageManager.PERMISSION_GRANTED;boolean requested=pending;pending=false;if(requested&&allowed){autoVoice.opened(SystemClock.elapsedRealtime());if(foreground){autoVoice.resumed(SystemClock.elapsedRealtime());traceAuto("resumed_gate");maybeAutoVoice();}else permissionGrantedAwaitingResume=true;}else if(!allowed)state("idle","未获得麦克风权限，尚未开始采集");}}
    private void updatePrivacy(){if(privacyText==null)return;String ears="minimax".equals(recognitionEngine)?"语音片段发送 MiniMax 云端识别":"语音在当前 Mac 本机识别";privacyText.setText(ears+"\n"+("dot".equals(brainProvider)?"Dot 思考":"MiniMax 思考")+" · MiniMax 播报\n"+(!active?"首次开启请安静等待 6 秒校准":wake.isChecked()?"15 分钟黑屏待机 · 大黄蜂 / Bumblebee 唤醒":"自由对话 · 闲置 15 分钟后等待唤醒")+"\n看一看：仅拍一张交给 Dot，不保存照片");}
    @Override public void recognition(String engine){runOnUiThread(()->{recognitionEngine=engine;updatePrivacy();});}
    private void refresh(){if(start==null)return;updatePrivacy();boolean waiting=foreground&&autoVoice.waiting(SystemClock.elapsedRealtime());start.setText(active?"暂停语音":playbackActive?("thinking".equals(phase)?"停止任务":"停止播报"):waiting&&!linked?"取消等待":"开始语音");start.setEnabled(linked||active||waiting);if(visionButton!=null){visionButton.setEnabled(linked&&!"calibrating".equals(phase));buttonStyle(visionButton,false);}interrupt.setEnabled((active||playbackActive||session.isVisual())&&!"calibrating".equals(phase));buttonStyle(start,false);buttonStyle(interrupt,false);refreshModes();brainText.setEnabled(!"calibrating".equals(phase));micText.setText(active?"麦克风开启":"麦克风关闭");micText.setCompoundDrawables(icon(R.drawable.ic_hud_mic,active?CYAN:AMBER,29),null,null,null);micText.setTextColor(active?CYAN:AMBER);statusLabel();if(foreground)getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);}
    @Override public void link(boolean connected){runOnUiThread(()->{linked=connected;workspaceRevision=-1;if(!connected){tasksData=new JSONArray();remindersData=new JSONArray();renderWorkspace();}brainText.setText("dot".equals(brainProvider)?(connected?"Dot 已连接":"Dot 未连接"):"MiniMax");traceAuto("link:"+connected);if(!connected)screen(false);String transport="wifi".equals(session.transportName())?"Wi-Fi":"USB";linkText.setText(connected?("USB".equals(transport)?"USB 优先":"Wi-Fi"):"未连接");companion.setTransport(transport);linkText.setCompoundDrawables(icon("Wi-Fi".equals(transport)?R.drawable.ic_hud_wifi:R.drawable.ic_hud_usb,connected?INK:MUTED,25),null,null,null);linkText.setTextColor(connected?CYAN:MUTED);refresh();maybeAutoVoice();});}
    @Override public void state(String next,String message){runOnUiThread(()->{phase=next;lastStateMessage=message;refresh();});}
    @Override public void caption(String text,String emotion){runOnUiThread(()->{final String value=text.isEmpty()?(active?"说大黄蜂唤醒我。\n也可以点击看一看。":"麦克风已关闭。\n点击开始语音即可对话。"):text;if(!value.equals(captionText.getText().toString())){captionText.setText(value);captionText.progress(0,false);captionScroll.scrollTo(0,0);captionId=0;}mood=emotion;});}
    @Override public void heard(String text){runOnUiThread(()->heardText.setText("你："+text));}
    @Override public void sources(JSONArray items,String time){runOnUiThread(()->{
        webSources=new JSONArray();retrievedAt=time;
        if(items!=null)for(int i=0;i<Math.min(6,items.length());i++){JSONObject item=items.optJSONObject(i);if(item!=null&&safeSource(item.optString("url")))webSources.put(item);}
        sourceText.setVisibility(webSources.length()==0?View.GONE:View.VISIBLE);
        if(webSources.length()>0)sourceText.setText("来源 "+webSources.length()+" 条 · 点击查看");
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
            catch(ActivityNotFoundException|SecurityException e){TextView link=text(url,22,INK);link.setTextIsSelectable(true);link.setPadding(dp(24),dp(20),dp(24),dp(20));new AlertDialog.Builder(this).setTitle("设备无法打开浏览器").setView(link).setPositiveButton("知道了",null).show();}
        }).setNegativeButton("关闭",null).show();
    }
    @Override public void playback(boolean value){runOnUiThread(()->{playbackActive=value;refresh();});}
    @Override public void microphone(boolean value){runOnUiThread(()->{active=session.isRecording();if(!active)screen(false);if(active)heardText.setText("等待你的指令");refresh();});}
    @Override public void screen(boolean sleeping){runOnUiThread(()->{
        if(sleeping&&(!foreground||!active))return;if(screenSleeping==sleeping)return;screenSleeping=sleeping;
        WindowManager.LayoutParams attrs=getWindow().getAttributes();if(sleeping){previousBrightness=attrs.screenBrightness;attrs.screenBrightness=0;blackScreen.setVisibility(View.VISIBLE);handler.removeCallbacks(animate);}
        else{attrs.screenBrightness=previousBrightness;blackScreen.setVisibility(View.GONE);if(foreground){handler.removeCallbacks(animate);handler.post(animate);}}getWindow().setAttributes(attrs);
    });}
    @Override protected void onResume(){super.onResume();foreground=true;traceAuto("resume");DesktopEntryService.update(this,true);getWindow().addFlags(WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);session.settings(wake.isChecked());session.resume();handler.post(animate);refresh();if(permissionGrantedAwaitingResume){permissionGrantedAwaitingResume=false;autoVoice.opened(SystemClock.elapsedRealtime());}autoVoice.resumed(SystemClock.elapsedRealtime());traceAuto("resumed_gate");maybeAutoVoice();}
    @Override protected void onPause(){screen(false);foreground=false;if(eyes!=null)eyes.cancel();traceAuto("pause");handler.removeCallbacks(autoVoiceTick);handler.removeCallbacks(animate);session.pause();DesktopEntryService.update(this,false);getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON|WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);phase="idle";companion.update(0,0,"calm","idle",false,false);super.onPause();}
    @Override protected void onStop(){pending=false;permissionGrantedAwaitingResume=false;autoVoice.stopped();traceAuto("stop");handler.removeCallbacks(autoVoiceTick);super.onStop();}
    @Override protected void onDestroy(){if(eyes!=null)eyes.destroy();super.onDestroy();}
    private boolean overlaps(View a,View b){android.graphics.Rect first=new android.graphics.Rect(),second=new android.graphics.Rect();return a.getGlobalVisibleRect(first)&&b.getGlobalVisibleRect(second)&&android.graphics.Rect.intersects(first,second);}
    @Override public void dump(String prefix,FileDescriptor fd,PrintWriter writer,String[] args){writer.println("CC8_ASSISTANT_STATUS "+session.snapshot());JSONObject ui=new JSONObject();Wire.put(ui,"theme","bumblebee_orbit_reference");Wire.put(ui,"camera_open",eyes!=null&&eyes.cameraOpen());Wire.put(ui,"camera_captures",eyes==null?0:eyes.captures());Wire.put(ui,"camera_preview_hold_ms",eyes==null?0:eyes.previewHoldMs());Wire.put(ui,"camera_preview_elapsed_ms",eyes==null?0:eyes.previewElapsedMs());Wire.put(ui,"camera_captured_after_preview_ms",eyes==null?0:eyes.capturedAfterPreviewMs());Wire.put(ui,"camera_last_error",eyes==null?"":eyes.lastError());Wire.put(ui,"screen_sleeping",screenSleeping);Wire.put(ui,"orbit_clock_time",companion.clockTime());Wire.put(ui,"orbit_clock_timezone",companion.clockTimeZone());Wire.put(ui,"progress_style","segmented_telemetry");Wire.put(ui,"waveform_separate",true);Wire.put(ui,"waveform_style","particle_field");Wire.put(ui,"karaoke_progress",captionProgress.getProgress());Wire.put(ui,"brain_provider",brainProvider);Wire.put(ui,"asr_engine",recognitionEngine);Wire.put(ui,"dot_result_available",!dotResult.isEmpty());Wire.put(ui,"wake_width",wakeButton.getWidth());Wire.put(ui,"wake_height",wakeButton.getHeight());Wire.put(ui,"wake_checked",wake.isChecked());Wire.put(ui,"auto_voice_open_requests",autoVoiceOpens);Wire.put(ui,"auto_voice_start_attempts",autoVoiceStarts);Wire.put(ui,"auto_voice_requested",lastOpenWantedVoice);Wire.put(ui,"auto_voice_pending",autoVoice.waiting(SystemClock.elapsedRealtime()));synchronized(autoVoiceTrace){Wire.put(ui,"auto_voice_trace",new JSONArray(autoVoiceTrace));}Wire.put(ui,"caption_view_height",captionScroll.getHeight());Wire.put(ui,"caption_scroll_y",captionScroll.getScrollY());Wire.put(ui,"caption_current_line",lastCaptionLine);Wire.put(ui,"caption_line_visible",lastCaptionLine<0||captionScroll.lineVisible(captionText,lastCaptionLine));Wire.put(ui,"caption_progress_overlap",overlaps(captionScroll,captionProgress));Wire.put(ui,"caption_clip_bounds",captionScroll.getClipBounds()!=null);Wire.put(ui,"source_count",webSources.length());Wire.put(ui,"workspace_revision",workspaceRevision);Wire.put(ui,"task_count",tasksData.length());Wire.put(ui,"reminder_count",remindersData.length());Wire.put(ui,"halo_status_overlap",overlaps(companion,stateText));Wire.put(ui,"caption_text_size_sp",captionText.getTextSize()/getResources().getDisplayMetrics().scaledDensity);Wire.put(ui,"control_text_size_sp",start.getTextSize()/getResources().getDisplayMetrics().scaledDensity);Wire.put(ui,"task_text_size_sp",dotText.getTextSize()/getResources().getDisplayMetrics().scaledDensity);writer.println("HUD_UI_STATUS "+ui);}
}
