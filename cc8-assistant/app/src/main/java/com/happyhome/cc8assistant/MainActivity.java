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
  // 主界面只编排前台操作与显示；录放和通道生命周期由 VoiceSession 管理。
  private final java.util.ArrayDeque<String> autoVoiceTrace = new java.util.ArrayDeque<>();
  private final OpenVoiceGate autoVoice = new OpenVoiceGate();
  private boolean permissionGrantedAwaitingResume;
  private int autoVoiceOpens, autoVoiceStarts;
  private boolean lastOpenWantedVoice;
  private long pendingTestCaptureLimitMs;
  private final Runnable autoVoiceTick = () -> maybeAutoVoice();
  private SingleFrameCamera eyes;
  private Button visionButton, wakeButton, freeButton;
  private TaskStepsView taskSteps;
  private TextView taskAction, reminderText;
  private ImageView stateIcon;
  private long workspaceRevision = -1;
  private long workspaceRoute = -1;
  private JSONArray tasksData = new JSONArray(), remindersData = new JSONArray();
  private JSONObject reminderPolicyData;
  private AlertDialog reminderPolicyDialog, reminderEditorDialog, reminderHistoryDialog;
  private JSONArray reminderHistoryData = new JSONArray();
  private boolean reminderChannelsSupported;
  // RAM-only notice: a lost connection cannot undo tool effects or prove failure.
  // Keep this separate from authoritative task receipts and never replay work.
  private boolean taskResultUnconfirmed;
  private String lastStateMessage = "系统待命";
  private VoiceSession session;
  private DotChatsDialog dotChats;
  private PreferenceMemoryDialog preferenceMemory;
  private RecognitionLanguageDialog recognitionLanguage;
  private RecognitionEnvironmentDialog recognitionEnvironment;
  private VocabularyDialog vocabulary;
  private AlertDialog imageQualityDialog;
  private boolean highDefinitionImage;
  private boolean reviewCapturedImage;
  private boolean retainCapturedPhoto;
  private SpeechCorrectionDialog correctionDialog;
  private SavedTasksDialog savedTasks;
  private ResultDetailsDialog resultDetails;
  private String dotResultSource = "";
  private CompanionView companion;
  private WaveformView waveform;
  private KaraokeTextView captionText;
  private View blackScreen;
  private boolean screenSleeping;
  private float previousBrightness = -1;
  private TextView progressText;
  private ProgressBar captionProgress;
  private int captionId, lastCaptionLine = -1;
  private boolean captionPlaceholder;
  private TextView linkText,
      stateText,
      privacyText,
      heardText,
      sourceText,
      micText,
      brainText,
      dotText;
  private Button start, interrupt;
  private Switch wake;
  private static final int INK = Color.rgb(217, 240, 249),
      CYAN = Color.rgb(67, 224, 252),
      MUTED = Color.rgb(120, 156, 175),
      AMBER = Color.rgb(255, 199, 91);
  private CaptionViewport captionScroll;
  private String brainProvider = "dot", recognitionEngine = "minimax", dotResult = "";
  private SourcesPresentation displayedSources;
  private AlertDialog sourcesDialog;
  private boolean foreground,
      active = false,
      playbackActive = false,
      linked = false,
      pending = false;
  private String phase = "idle", mood = "friendly";
  private final ListeningState listeningState = new ListeningState();
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog connectionInfoDialog;
  private JSONObject connectionInfoAvailability;
  private String connectionInfoRenderedMessage = "";

  private int dp(float value) {
    return Math.round(value * getResources().getDisplayMetrics().density);
  }

  private TextView text(String value, int size, int color) {
    TextView t = new TextView(this);
    t.setText(value);
    t.setTextSize(size);
    t.setTextColor(color);
    return t;
  }

  private final Runnable animate =
      new Runnable() {
        public void run() {
          if (!foreground || screenSleeping) return;
          VoiceSession.CaptionProgress cp = session.playbackFrame();
          float level = cp.level;
          companion.update(
              level, session.microphoneLevel(), level > 0 ? cp.mood : mood, phase, linked, active);
          waveform.update(level, cp.playing, cp.paused);
          if (cp.id > 0 && cp.text.equals(captionText.getText().toString())) {
            if (captionId != cp.id) {
              captionId = cp.id;
              lastCaptionLine = -1;
              captionScroll.scrollTo(0, 0);
            }
            captionText.progress(cp.position, cp.timed);
            captionProgress.setVisibility(cp.timed ? View.VISIBLE : View.INVISIBLE);
            progressText.setVisibility(cp.timed ? View.VISIBLE : View.GONE);
            int progress = Math.round(1000 * cp.position / Math.max(1, cp.text.length()));
            captionProgress.setProgress(progress);
            String clockText =
                playTime(cp.elapsedMs) + (cp.totalMs > 0 ? " / " + playTime(cp.totalMs) : "");
            if (!clockText.contentEquals(progressText.getText())) progressText.setText(clockText);
            android.text.Layout layout = captionText.getLayout();
            if (layout != null) {
              int line =
                  layout.getLineForOffset(
                      Math.min(Math.max(0, cp.text.length() - 1), (int) cp.position));
              if (line != lastCaptionLine) {
                lastCaptionLine = line;
                captionScroll.revealLine(captionText, line);
              }
            }
          } else {
            captionText.progress(0, false);
            captionProgress.setVisibility(View.INVISIBLE);
            progressText.setVisibility(View.INVISIBLE);
            if (!playbackActive && captionId == 0 && captionPlaceholder)
              session.noteTestStopVisuals(cp);
          }
          handler.postDelayed(this, 33);
        }
      };

  @Override
  public void onCreate(Bundle state) {
    super.onCreate(state);
    getWindow()
        .setFlags(
            WindowManager.LayoutParams.FLAG_FULLSCREEN, WindowManager.LayoutParams.FLAG_FULLSCREEN);
    getWindow()
        .getDecorView()
        .setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
    session = new VoiceSession(this, this);
    correctionDialog = new SpeechCorrectionDialog(this, session);
    dotChats = new DotChatsDialog(this, session);
    preferenceMemory = new PreferenceMemoryDialog(this, session);
    recognitionLanguage = new RecognitionLanguageDialog(this, session);
    recognitionEnvironment = new RecognitionEnvironmentDialog(this, session);
    vocabulary = new VocabularyDialog(this, session);
    resultDetails = new ResultDetailsDialog(this, session);
    savedTasks = new SavedTasksDialog(this, session, resultDetails);
    requestOpenVoice(getIntent());
    build();
  }

  private GradientDrawable surface(int fill, int stroke) {
    GradientDrawable d = new GradientDrawable();
    d.setColor(fill);
    d.setCornerRadius(dp(14));
    d.setStroke(dp(1), stroke);
    return d;
  }

  private TextView mono(String value, int size, int color) {
    TextView t = text(value, size, color);
    t.setTypeface(Typeface.create("monospace", Typeface.NORMAL));
    t.setLetterSpacing(.08f);
    return t;
  }

  private void buttonStyle(Button button, boolean primary) {
    StateListDrawable bg = new StateListDrawable();
    bg.addState(
        new int[] {-android.R.attr.state_enabled},
        rounded(Color.rgb(8, 25, 39), Color.rgb(24, 48, 65), 18));
    bg.addState(new int[] {android.R.attr.state_pressed}, rounded(Color.rgb(30, 72, 94), CYAN, 18));
    GradientDrawable normal =
        new GradientDrawable(
            GradientDrawable.Orientation.TOP_BOTTOM,
            new int[] {Color.rgb(19, 48, 69), Color.rgb(12, 35, 53)});
    normal.setCornerRadius(dp(18));
    normal.setStroke(dp(1), Color.rgb(52, 78, 104));
    bg.addState(new int[] {}, normal);
    button.setBackground(bg);
    button.setTextColor(
        new ColorStateList(
            new int[][] {new int[] {-android.R.attr.state_enabled}, new int[] {}},
            new int[] {Color.rgb(83, 111, 126), INK}));
  }

  private StateListDrawable switchShape(boolean thumb) {
    StateListDrawable states = new StateListDrawable();
    for (boolean checked : new boolean[] {true, false}) {
      GradientDrawable d = new GradientDrawable();
      d.setShape(thumb ? GradientDrawable.OVAL : GradientDrawable.RECTANGLE);
      d.setSize(dp(thumb ? 20 : 48), dp(thumb ? 20 : 12));
      d.setCornerRadius(dp(10));
      d.setColor(
          thumb
              ? (checked ? CYAN : Color.rgb(106, 146, 166))
              : (checked ? Color.rgb(23, 88, 111) : Color.rgb(24, 49, 65)));
      d.setStroke(dp(1), checked ? CYAN : Color.rgb(58, 105, 128));
      states.addState(checked ? new int[] {android.R.attr.state_checked} : new int[] {}, d);
    }
    return states;
  }

  private String playTime(long milliseconds) {
    long seconds = milliseconds / 1000;
    return String.format(java.util.Locale.US, "%02d:%02d", seconds / 60, seconds % 60);
  }

  private GradientDrawable rounded(int fill, int border, int radius) {
    GradientDrawable d = surface(fill, border);
    d.setCornerRadius(dp(radius));
    return d;
  }

  private android.graphics.drawable.Drawable icon(int resource, int tint, int size) {
    android.graphics.drawable.Drawable d = getDrawable(resource).mutate();
    d.setTint(tint);
    d.setBounds(0, 0, dp(size), dp(size));
    return d;
  }

  private Button action(String title, int resource) {
    Button b = new Button(this);
    b.setText(title);
    b.setAllCaps(false);
    b.setTextSize(22);
    b.setMinHeight(0);
    b.setMinWidth(0);
    b.setStateListAnimator(null);
    b.setPadding(dp(12), 0, dp(12), 0);
    b.setGravity(Gravity.CENTER);
    if (resource != 0) {
      b.setCompoundDrawables(icon(resource, INK, 28), null, null, null);
      b.setCompoundDrawablePadding(dp(11));
    }
    b.addOnLayoutChangeListener(
        (v, l, t, r, bot, ol, ot, orr, ob) -> {
          int group =
              (int) Math.ceil(b.getPaint().measureText(b.getText().toString()))
                  + (resource == 0 ? 0 : dp(39));
          int pad = Math.max(dp(6), (r - l - group) / 2);
          if (b.getPaddingLeft() != pad) b.setPadding(pad, 0, pad, 0);
        });
    buttonStyle(b, false);
    return b;
  }

  private void place(FrameLayout parent, View view, float x, float y, float w, float h) {
    FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(dp(w), dp(h));
    lp.leftMargin = dp(x);
    lp.topMargin = dp(y);
    parent.addView(view, lp);
  }

  private void rule(FrameLayout parent, float x, float y, float w, float h) {
    View line = new View(this);
    line.setBackgroundColor(Color.rgb(36, 75, 96));
    place(parent, line, x, y, w, h);
  }

  private TextView label(String value, int size, int color) {
    TextView t = text(value, size, color);
    t.setGravity(Gravity.CENTER_VERTICAL);
    t.setIncludeFontPadding(false);
    return t;
  }

  private void build() {
    // 固定 1280×800 设计坐标统一缩放，保留光环、独立声波、字幕和进度条的间距。
    FrameLayout stage = new FrameLayout(this);
    stage.addView(new HudBackdrop(this), new FrameLayout.LayoutParams(-1, -1));
    setContentView(stage);
    FrameLayout root = new FrameLayout(this);
    root.setClipChildren(false);
    stage.addView(root, new FrameLayout.LayoutParams(dp(1280), dp(800)));
    stage.addOnLayoutChangeListener(
        (v, l, t, r, b, ol, ot, orr, ob) -> {
          float scale =
              Math.min(stage.getWidth() / (float) dp(1280), stage.getHeight() / (float) dp(800));
          root.setPivotX(0);
          root.setPivotY(0);
          root.setScaleX(scale);
          root.setScaleY(scale);
          root.setTranslationX((stage.getWidth() - dp(1280) * scale) / 2);
          root.setTranslationY((stage.getHeight() - dp(800) * scale) / 2);
        });
    TextView title = label("BUMBLEBEE", 36, AMBER);
    title.setLetterSpacing(.16f);
    place(root, title, 35, 15, 268, 47);
    rule(root, 308, 25, 1, 28);
    place(root, label("大黄蜂 · 个人助理", 21, INK), 331, 20, 260, 39);
    brainText = label("Dot 已连接", 20, INK);
    brainText.setCompoundDrawables(icon(R.drawable.ic_hud_connection, CYAN, 24), null, null, null);
    brainText.setCompoundDrawablePadding(dp(9));
    brainText.setOnClickListener(v -> showBrainSelector());
    place(root, brainText, 838, 15, 146, 48);
    rule(root, 985, 26, 1, 26);
    linkText = label("未连接", 20, INK);
    linkText.setCompoundDrawables(icon(R.drawable.ic_hud_usb, INK, 25), null, null, null);
    linkText.setCompoundDrawablePadding(dp(9));
    linkText.setOnClickListener(v -> showConnectionInfo());
    linkText.setSingleLine(true);
    place(root, linkText, 991, 15, 121, 48);
    rule(root, 1114, 26, 1, 26);
    micText = label("麦克风关闭", 20, AMBER);
    micText.setCompoundDrawables(icon(R.drawable.ic_hud_mic, AMBER, 29), null, null, null);
    micText.setCompoundDrawablePadding(dp(7));
    micText.setSingleLine(true);
    place(root, micText, 1125, 14, 140, 50);
    rule(root, 25, 69, 1230, 1);
    companion = new CompanionView(this);
    companion.setOnLongClickListener(
        v -> {
          startActivity(new Intent(this, CameraTestActivity.class));
          return true;
        });
    place(root, companion, 25, 75, 550, 456);
    waveform = new WaveformView(this);
    place(root, waveform, 24, 520, 528, 112);
    LinearLayout status = new LinearLayout(this);
    status.setGravity(Gravity.CENTER);
    stateIcon = new ImageView(this);
    stateIcon.setImageDrawable(icon(R.drawable.ic_hud_voice, AMBER, 24));
    status.addView(stateIcon, new LinearLayout.LayoutParams(dp(24), dp(28)));
    stateText = label("系统待命", 20, CYAN);
    stateText.setSingleLine(true);
    stateText.setEllipsize(TextUtils.TruncateAt.END);
    LinearLayout.LayoutParams statusTextLp = new LinearLayout.LayoutParams(-2, -1);
    statusTextLp.leftMargin = dp(9);
    status.addView(stateText, statusTextLp);
    status.setOnClickListener(v -> showStatusDetails());
    place(root, status, 24, 629, 528, 38);
    TextView eyebrow = label("对话", 23, INK);
    eyebrow.setCompoundDrawables(icon(R.drawable.ic_hud_chat, INK, 27), null, null, null);
    eyebrow.setCompoundDrawablePadding(dp(14));
    place(root, eyebrow, 584, 114, 220, 37);
    heardText = label("等待你的指令", 24, MUTED);
    heardText.setMaxLines(2);
    heardText.setEllipsize(TextUtils.TruncateAt.END);
    place(root, heardText, 587, 168, 666, 54);
    captionScroll = new CaptionViewport(this);
    captionScroll.setFillViewport(false);
    captionScroll.setVerticalScrollBarEnabled(false);
    captionText = new KaraokeTextView(this);
    captionText.setText(
        autoVoice.waiting(SystemClock.elapsedRealtime())
            ? "连接后自动开始语音。\n请安静等待校准。"
            : "麦克风已关闭。\n点击开始语音即可对话。");
    captionText.setTextSize(36);
    captionText.setTypeface(Typeface.DEFAULT_BOLD);
    captionText.setTextColor(INK);
    captionText.setLineSpacing(dp(6), 1f);
    captionText.setIncludeFontPadding(false);
    captionText.setPadding(0, dp(3), 0, dp(16));
    captionScroll.addView(captionText, new ScrollView.LayoutParams(-1, -2));
    place(root, captionScroll, 587, 229, 665, 153);
    captionProgress = new PlaybackProgressView(this);
    captionProgress.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
    captionProgress.setMax(1000);
    captionProgress.setVisibility(View.INVISIBLE);
    place(root, captionProgress, 588, 390, 524, 40);
    progressText = label("", 18, MUTED);
    progressText.setGravity(Gravity.CENTER_VERTICAL | Gravity.END);
    progressText.setSingleLine(true);
    place(root, progressText, 1125, 387, 126, 31);
    sourceText = label("", 18, CYAN);
    sourceText.setGravity(Gravity.CENTER_VERTICAL | Gravity.END);
    sourceText.setSingleLine(true);
    sourceText.setEllipsize(TextUtils.TruncateAt.END);
    sourceText.setVisibility(View.GONE);
    sourceText.setOnClickListener(v -> showSources());
    place(root, sourceText, 991, 114, 261, 37);
    rule(root, 584, 442, 668, 1);
    rule(root, 1095, 468, 1, 96);
    ImageView taskIcon = new ImageView(this);
    taskIcon.setImageDrawable(icon(R.drawable.ic_hud_task, CYAN, 31));
    place(root, taskIcon, 585, 465, 31, 35);
    dotText = label("当前任务 · 暂无任务", 24, INK);
    dotText.setTypeface(Typeface.DEFAULT_BOLD);
    dotText.setSingleLine(true);
    dotText.setEllipsize(TextUtils.TruncateAt.END);
    dotText.setOnClickListener(v -> showTasks());
    place(root, dotText, 632, 463, 446, 43);
    taskSteps = new TaskStepsView(this);
    taskSteps.setOnClickListener(v -> showTasks());
    place(root, taskSteps, 584, 513, 540, 43);
    taskAction = label("查看任务  ›", 22, CYAN);
    taskAction.setGravity(Gravity.CENTER);
    taskAction.setOnClickListener(v -> showTasks());
    place(root, taskAction, 1116, 477, 137, 78);
    rule(root, 584, 583, 668, 1);
    rule(root, 1095, 600, 1, 47);
    ImageView reminderIcon = new ImageView(this);
    reminderIcon.setImageDrawable(icon(R.drawable.ic_hud_bell, INK, 32));
    reminderIcon.setOnClickListener(v -> showReminders());
    place(root, reminderIcon, 590, 605, 32, 36);
    reminderText = label("暂无提醒 · 点击添加", 23, MUTED);
    reminderText.setSingleLine(true);
    reminderText.setEllipsize(TextUtils.TruncateAt.END);
    reminderText.setOnClickListener(v -> showReminders());
    place(root, reminderText, 642, 600, 438, 48);
    TextView reminderAction = label("查看提醒  ›", 22, CYAN);
    reminderAction.setGravity(Gravity.CENTER);
    reminderAction.setOnClickListener(v -> showReminders());
    place(root, reminderAction, 1117, 600, 136, 48);
    privacyText = text("", 20, MUTED);
    FrameLayout dock = new FrameLayout(this);
    dock.setBackground(rounded(Color.rgb(3, 24, 39), Color.rgb(29, 70, 95), 22));
    place(root, dock, 24, 674, 1232, 92);
    FrameLayout modes = new FrameLayout(this);
    modes.setBackground(rounded(Color.rgb(15, 42, 61), Color.rgb(50, 78, 104), 35));
    place(root, modes, 36, 686, 440, 68);
    wake = new Switch(this);
    wake.setChecked(getPreferences(0).getBoolean("wake_mode", true));
    wake.setOnCheckedChangeListener(
        (button, checked) -> {
          getPreferences(0).edit().putBoolean("wake_mode", checked).apply();
          session.settings(checked);
          refreshModes();
          updatePrivacy();
        });
    wakeButton = action("唤醒模式", R.drawable.ic_hud_chat);
    freeButton = action("自由对话", R.drawable.ic_hud_chat);
    wakeButton.setOnClickListener(v -> wake.setChecked(true));
    freeButton.setOnClickListener(v -> wake.setChecked(false));
    place(modes, wakeButton, 0, 0, 213, 68);
    place(modes, freeButton, 213, 0, 227, 68);
    rule(root, 476, 688, 1, 64);
    visionButton = action("看一看", R.drawable.ic_hud_camera);
    visionButton.setContentDescription("点按看图，长按选择识字和看图设置");
    visionButton.setOnClickListener(v -> requestImage("observe"));
    visionButton.setOnLongClickListener(
        v -> {
          if (!foreground || session.isVisual()) return false;
          if (imageQualityDialog != null) imageQualityDialog.dismiss();
          AlertDialog dialog =
              new AlertDialog.Builder(this)
                  .setTitle("看图与识字")
                  .setItems(
                      new String[] {"普通看图", "识别图片文字", "看图设置", "追问最近照片", "选择 Mac 窗口"},
                      (d, n) -> {
                        if (n == 2) showImageQuality();
                        else if (n == 3) showPhotoQuestion();
                        else if (n == 4) requestImage("mac_window");
                        else requestImage(n == 1 ? "ocr" : "observe");
                      })
                  .setNegativeButton("取消", null)
                  .create();
          imageQualityDialog = dialog;
          dialog.setOnDismissListener(
              d -> {
                if (imageQualityDialog == dialog) imageQualityDialog = null;
              });
          dialog.show();
          return true;
        });
    place(root, visionButton, 499, 686, 177, 68);
    Button tasks = action("任务", R.drawable.ic_hud_task);
    tasks.setOnClickListener(v -> showTasks());
    place(root, tasks, 694, 686, 151, 68);
    start = action("开始语音", R.drawable.ic_hud_pause);
    start.setOnClickListener(v -> toggleVoice());
    place(root, start, 863, 686, 198, 68);
    Button settings = action("设置", R.drawable.ic_hud_settings);
    settings.setOnClickListener(v -> showSettings());
    place(root, settings, 1079, 686, 164, 68);
    interrupt = new Button(this);
    interrupt.setOnClickListener(v -> session.interrupt());
    blackScreen = new View(this);
    blackScreen.setBackgroundColor(Color.BLACK);
    blackScreen.setVisibility(View.GONE);
    blackScreen.setContentDescription("待机黑屏，触摸或说大黄蜂唤醒");
    blackScreen.setOnClickListener(v -> session.screenTouched());
    stage.addView(blackScreen, new FrameLayout.LayoutParams(-1, -1));
    eyes = new SingleFrameCamera(this, stage, () -> session.cancelVision());
    refresh();
    renderWorkspace();
  }

  private void showStatusDetails() {
    if ("error".equals(phase)) {
      new AlertDialog.Builder(this)
          .setTitle("错误详情与处理方法")
          .setMessage(lastStateMessage)
          .setPositiveButton("关闭", null)
          .show();
    } else Toast.makeText(this, lastStateMessage, Toast.LENGTH_LONG).show();
  }

  private void statusLabel() {
    if (stateText == null) return;
    String message =
        "speaking".equals(phase)
            ? "正在播报 · " + (active ? "开口即可打断" : "点击停止播报")
            : "thinking".equals(phase)
                ? "正在处理 · " + (active ? "可开口打断" : "点击停止任务")
                : "listening".equals(phase)
                    ? listeningState.label()
                    : "calibrating".equals(phase) ? "正在校准 · 请保持安静" : lastStateMessage;
    if ("error".equals(phase)) {
      int end = message.indexOf('。');
      message = (end < 0 ? message : message.substring(0, end)) + " · 查看详情 ›";
    }
    android.text.SpannableString value = new android.text.SpannableString(message);
    int stop = message.indexOf('·');
    value.setSpan(
        new android.text.style.ForegroundColorSpan(
            "speaking".equals(phase) ? AMBER : "error".equals(phase) ? AMBER : CYAN),
        0,
        stop < 0 ? message.length() : stop,
        android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
    stateText.setTextColor(INK);
    stateText.setText(value);
    stateText.setContentDescription("error".equals(phase) ? message + "。点按查看完整错误与处理方法" : message);
    stateIcon.setImageDrawable(
        icon(R.drawable.ic_hud_voice, "speaking".equals(phase) ? AMBER : CYAN, 24));
  }

  private void toggleVoice() {
    pendingTestCaptureLimitMs = 0;
    if (!active && playbackActive) {
      session.interrupt();
      return;
    }
    traceAuto("button");
    boolean waiting = autoVoice.waiting(SystemClock.elapsedRealtime());
    autoVoice.cancel();
    handler.removeCallbacks(autoVoiceTick);
    if (active) {
      session.stopMicrophone();
      state("idle", "麦克风已关闭");
      caption("语音会话已暂停。\n点击开始语音继续。", "calm");
    } else if (waiting && !linked) {
      state("idle", "已取消自动开始，请连接后手动开启语音");
    } else begin();
  }

  private void refreshModes() {
    if (wakeButton == null) return;
    for (Button b : new Button[] {wakeButton, freeButton}) {
      boolean chosen = b == wakeButton ? wake.isChecked() : !wake.isChecked();
      if (chosen) {
        GradientDrawable fill =
            new GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM,
                new int[] {Color.rgb(255, 208, 104), Color.rgb(255, 191, 76)});
        fill.setCornerRadius(dp(35));
        b.setBackground(fill);
      } else b.setBackgroundColor(Color.TRANSPARENT);
      b.setTextColor(chosen ? Color.rgb(3, 22, 35) : INK);
      b.setTypeface(chosen ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
      b.setCompoundDrawables(
          icon(
              chosen ? R.drawable.ic_hud_chat_fill : R.drawable.ic_hud_chat,
              chosen ? Color.rgb(3, 22, 35) : INK,
              28),
          null,
          null,
          null);
    }
  }

  @Override
  public void announcement() {
    runOnUiThread(
        () -> {
          heardText.setText("指定播报 · 麦克风状态保持不变");
          clearSources();
        });
  }

  @Override
  public void workspace(JSONObject data, long route) {
    runOnUiThread(
        () -> {
          if (!linked || workspaceRoute != route || !session.currentRoute(route)) return;
          long revision = data.optLong("revision", -1);
          if (revision < workspaceRevision) return;
          JSONArray tasks = data.optJSONArray("tasks"), reminders = data.optJSONArray("reminders");
          if (tasks == null || reminders == null || tasks.length() > 12 || reminders.length() > 32)
            return;
          workspaceRevision = revision;
          tasksData = tasks;
          remindersData = reminders;
          reminderPolicyData = data.optJSONObject("reminder_policy");
          reminderChannelsSupported = data.optInt("reminder_channels_version", 0) == 1;
          JSONArray history = data.optJSONArray("reminder_history");
          reminderHistoryData =
              history != null && history.length() <= 12 ? history : new JSONArray();
          renderWorkspace();
        });
  }

  @Override
  public void workspaceResult(boolean ok, String message, long route) {
    runOnUiThread(
        () -> {
          if (!linked || workspaceRoute != route || !session.currentRoute(route)) return;
          Toast.makeText(this, message, Toast.LENGTH_LONG).show();
        });
  }

  private String taskStatus(String status) {
    return "completed".equals(status)
        ? "已完成"
        : "failed".equals(status) ? "未完成" : "cancelled".equals(status) ? "已停止" : "处理中";
  }

  private void renderWorkspace() {
    if (dotText == null) return;
    JSONObject latest = tasksData.optJSONObject(0);
    taskSteps.task(latest);
    dotText.setText(
        latest == null
            ? (taskResultUnconfirmed ? "上次执行 · 结果待核对" : linked ? "当前任务 · 暂无任务" : "当前任务 · 等待连接")
            : "当前任务 · " + latest.optString("title"));
    dotText.setTextColor(latest == null && taskResultUnconfirmed ? AMBER : INK);
    taskAction.setText("查看任务  ›");
    JSONObject reminder = remindersData.optJSONObject(0);
    reminderText.setText(
        reminder == null
            ? "暂无提醒 · 点击添加"
            : dateLabel(reminder.optString("due")) + "  " + reminder.optString("title"));
    reminderText.setTextColor(reminder == null ? MUTED : INK);
  }

  private String dateLabel(String due) {
    if (due.length() < 16) return due;
    java.util.Calendar day =
        java.util.Calendar.getInstance(java.util.TimeZone.getTimeZone("Asia/Shanghai"));
    java.text.SimpleDateFormat f =
        new java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US);
    f.setTimeZone(day.getTimeZone());
    String today = f.format(day.getTime());
    day.add(java.util.Calendar.DAY_OF_MONTH, 1);
    String tomorrow = f.format(day.getTime());
    String prefix =
        due.startsWith(today)
            ? "今天"
            : due.startsWith(tomorrow) ? "明天" : due.substring(5, 10).replace('-', '/');
    return prefix + " " + due.substring(11, 16);
  }

  private void inputTask() {
    EditText input = new EditText(this);
    input.setTextSize(22);
    input.setHint("输入要交给大黄蜂的指令");
    input.setTextColor(INK);
    input.setHintTextColor(MUTED);
    input.setPadding(dp(24), dp(18), dp(24), dp(18));
    input.setFilters(
        new android.text.InputFilter[] {new android.text.InputFilter.LengthFilter(6000)});
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("文字指令 · 不开启麦克风")
            .setView(input)
            .setPositiveButton("发送", null)
            .setNegativeButton("取消", null)
            .create();
    dialog.setOnShowListener(
        d ->
            dialog
                .getButton(AlertDialog.BUTTON_POSITIVE)
                .setOnClickListener(
                    v -> {
                      String value = input.getText().toString().trim();
                      if (value.isEmpty()) {
                        input.setError("请输入指令");
                        return;
                      }
                      if (!linked) {
                        input.setError("Mac 尚未连接");
                        return;
                      }
                      session.requestText(value);
                      dialog.dismiss();
                    }));
    dialog.show();
  }

  private void showTasks() {
    if (taskResultUnconfirmed) showUnconfirmedTaskNotice();
    else showTaskList();
  }

  private boolean hasRunningTask(JSONArray tasks) {
    for (int i = 0; i < tasks.length(); i++) {
      JSONObject task = tasks.optJSONObject(i);
      if (task != null && "running".equals(task.optString("status"))) return true;
    }
    return false;
  }

  private void showUnconfirmedTaskNotice() {
    new AlertDialog.Builder(this)
        .setTitle("上次执行结果未确认")
        .setMessage("连接中断前有未结束的任务，部分步骤可能已经执行。请先在电脑端核对结果，再决定是否重新交办。重连不会自动重做任务；关闭提示也不会撤销已执行的操作。")
        .setPositiveButton(
            "知道了",
            (d, n) -> {
              taskResultUnconfirmed = false;
              renderWorkspace();
            })
        .setNeutralButton("查看当前任务", (d, n) -> showTaskList())
        .setNegativeButton("关闭", null)
        .show();
  }

  private void showTaskList() {
    final long route = workspaceRoute;
    if (linked) session.workspaceGet();
    if (tasksData.length() == 0) {
      new AlertDialog.Builder(this)
          .setTitle("任务")
          .setMessage("还没有任务记录。你可以让大黄蜂查询资料、分析照片或操作当前 Mac。任务记录仅在本次 Mac 服务运行期间保留。")
          .setPositiveButton("文字指令", (d, n) -> inputTask())
          .setNeutralButton("已保存结果", (d, n) -> savedTasks.open())
          .setNegativeButton("关闭", null)
          .show();
      return;
    }
    final JSONArray tasks = tasksData;
    String[] names = new String[tasks.length() + 1];
    names[0] = "已保存结果 · 重启后可查";
    for (int i = 0; i < tasks.length(); i++) {
      JSONObject t = tasks.optJSONObject(i);
      names[i + 1] = taskStatus(t.optString("status")) + " · " + t.optString("title");
    }
    new AlertDialog.Builder(this)
        .setTitle("最近任务 · 本次服务")
        .setItems(
            names,
            (d, n) -> {
              if (n == 0) savedTasks.open();
              else showTaskDetail(tasks.optJSONObject(n - 1), route);
            })
        .setNeutralButton("提醒", (d, n) -> showReminders())
        .setPositiveButton("文字指令", (d, n) -> inputTask())
        .setNegativeButton("关闭", null)
        .show();
  }

  private boolean requireWorkspaceRoute(long route) {
    if (linked && workspaceRoute == route && session.currentRoute(route)) return true;
    workspaceNotSent();
    return false;
  }

  private void workspaceNotSent() {
    Toast.makeText(this, "连接已变化或请求未能提交，请重新打开任务或提醒并核对结果", Toast.LENGTH_LONG).show();
  }

  private void showTaskDetail(JSONObject task, long route) {
    if (!requireWorkspaceRoute(route)) return;
    StringBuilder body = new StringBuilder(task.optString("stage"));
    JSONArray steps = task.optJSONArray("steps");
    if (steps != null)
      for (int i = 0; i < steps.length(); i++) {
        JSONObject step = steps.optJSONObject(i);
        body.append("\n\n")
            .append(step.optString("title"))
            .append(" · ")
            .append(taskStatus(step.optString("status")));
      }
    String result = task.optString("result");
    if (!result.isEmpty()) body.append("\n\n结果摘录：\n").append(result);
    TextView detail = text(body.toString(), 22, INK);
    detail.setTextIsSelectable(true);
    detail.setPadding(dp(24), dp(18), dp(24), dp(18));
    LinearLayout content = new LinearLayout(this);
    content.setOrientation(LinearLayout.VERTICAL);
    content.addView(detail);
    if ("completed".equals(task.optString("status")) && session.supportsResultReading()) {
      for (boolean restart : new boolean[] {false, true}) {
        Button read = new Button(this);
        read.setText(restart ? "从头朗读结果" : "继续朗读结果");
        read.setTextSize(22);
        read.setOnClickListener(
            v -> {
              if (!session.readResult(task.optString("id"), restart, route)) workspaceNotSent();
              read.setEnabled(false);
            });
        content.addView(read);
      }
    }
    ScrollView scroll = new ScrollView(this);
    scroll.addView(content);
    AlertDialog.Builder dialog =
        new AlertDialog.Builder(this)
            .setTitle(task.optString("title"))
            .setView(scroll)
            .setNegativeButton("关闭", null);
    if ("running".equals(task.optString("status")) && linked)
      dialog.setPositiveButton(
          "停止任务",
          (d, n) -> {
            if (!session.cancelTask(task.optString("id"), route)) workspaceNotSent();
          });
    else if (linked)
      dialog.setPositiveButton("保存结果", (d, n) -> savedTasks.save(task.optString("id"), route));
    if (!"running".equals(task.optString("status")) && task.optJSONObject("provenance") != null)
      dialog.setNeutralButton("来源与文件", (d, n) -> resultDetails.open(task, false, route));
    dialog.show();
  }

  private void showReminders() {
    final long route = workspaceRoute;
    if (linked) session.workspaceGet();
    final JSONArray items = remindersData;
    String[] names = new String[items.length()];
    for (int i = 0; i < names.length; i++) {
      JSONObject item = items.optJSONObject(i);
      names[i] =
          dateLabel(item.optString("due"))
              + " · "
              + item.optString("title")
              + "\n"
              + repeatLabel(item.optString("repeat"))
              + " · "
              + reminderDeliveryLabel(item);
    }
    AlertDialog.Builder dialog =
        new AlertDialog.Builder(this).setTitle("提醒 · 北京时间").setNegativeButton("关闭", null);
    if (names.length > 0)
      dialog.setItems(names, (d, n) -> reminderOptions(items.optJSONObject(n), route));
    else
      dialog.setMessage(linked ? "暂无提醒。Mac 服务运行、CC8 前台连接时可到时播报；离线期间不会开启设备或麦克风。" : "连接 Mac 后可管理提醒。");
    if (linked) dialog.setPositiveButton("添加提醒", (d, n) -> editReminder(null, route));
    if (linked && reminderPolicyData != null)
      dialog.setNeutralButton("送达设置", (d, n) -> reminderDeliverySettings(route));
    dialog.show();
  }

  private static String reminderDeliveryLabel(JSONObject item) {
    boolean desktop = "desktop".equals(item.optString("channel"));
    String prefix = desktop ? "Mac 桌面 · " : "CC8 · ";
    String status = item.optString("status");
    JSONObject attempt = item.optJSONObject("delivery_attempt");
    if ("cancelled".equals(status)) return prefix + "已取消";
    if ("missed".equals(status)) return prefix + "已错过";
    if ("delivering".equals(status)) return prefix + (desktop ? "等待通知回执" : "等待播完回执");
    if ("unconfirmed".equals(status)) {
      String detail = attempt == null ? "" : attempt.optString("detail");
      String reason =
          "permission_denied".equals(detail)
              ? "，请检查 Mac 通知授权"
              : "not_installed".equals(detail)
                  ? "，请先安装 Mac 通知辅助程序"
                  : "unsupported".equals(detail)
                      ? "，当前主机不支持"
                      : "submitted_unconfirmed".equals(detail) ? "，已提交系统但未确认显示" : "，需重新安排";
      return prefix + "送达未确认" + reason;
    }
    if ("pending".equals(status)) return prefix + "等待到期";
    if ("delivered".equals(status) && attempt == null) return prefix + "历史已送达标记，未保留尝试回执";
    if (attempt != null && "notified".equals(attempt.optString("result")))
      return "Mac 桌面 · 已进入通知中心，未确认阅读";
    if (attempt != null && "played".equals(attempt.optString("result"))) return "CC8 · 已播完";
    return prefix + "等待到期";
  }

  private static String reminderHistoryLabel(JSONObject item) {
    JSONObject attempt = item.optJSONObject("delivery_attempt");
    if (attempt == null) return reminderDeliveryLabel(item);
    String result = attempt.optString("result");
    String status =
        "notified".equals(result) || "played".equals(result)
            ? "delivered"
            : "unconfirmed".equals(result)
                ? "unconfirmed"
                : "missed".equals(result)
                    ? "missed"
                    : "pending_ack".equals(result) ? "delivering" : "";
    if (status.isEmpty()) return "尝试状态不可用";
    JSONObject receipt = new JSONObject();
    Wire.put(
        receipt, "channel", "desktop".equals(attempt.optString("channel")) ? "desktop" : "cc8");
    Wire.put(receipt, "status", status);
    Wire.put(receipt, "delivery_attempt", attempt);
    return reminderDeliveryLabel(receipt);
  }

  private void showReminderHistory() {
    AlertDialog.Builder builder = new AlertDialog.Builder(this).setTitle("提醒送达记录 · 北京时间");
    if (reminderHistoryData.length() == 0) {
      builder.setMessage("暂无记录。这里只显示最近保存的送达状态，不代表用户已听到或已阅读。");
    } else {
      String[] rows = new String[reminderHistoryData.length()];
      for (int i = 0; i < rows.length; i++) {
        JSONObject item = reminderHistoryData.optJSONObject(i);
        if (item == null) {
          rows[i] = "记录不可用";
          continue;
        }
        JSONObject attempt = item.optJSONObject("delivery_attempt");
        String due = attempt == null ? item.optString("due") : attempt.optString("due");
        rows[i] =
            "当前事项："
                + item.optString("title")
                + "\n原定："
                + dateLabel(due)
                + "\n"
                + reminderHistoryLabel(item)
                + ("pending".equals(item.optString("status"))
                    ? "\n下一次："
                        + dateLabel(item.optString("due"))
                        + " · "
                        + reminderDeliveryLabel(item)
                    : "cancelled".equals(item.optString("status")) ? " · 后续已取消" : "");
      }
      builder.setItems(rows, null);
    }
    if (reminderHistoryDialog != null) reminderHistoryDialog.dismiss();
    AlertDialog dialog = builder.setNegativeButton("关闭", null).create();
    reminderHistoryDialog = dialog;
    dialog.setOnDismissListener(
        unused -> {
          if (reminderHistoryDialog == dialog) reminderHistoryDialog = null;
        });
    dialog.show();
  }

  private void reminderDeliverySettings(long route) {
    if (!requireWorkspaceRoute(route) || reminderPolicyData == null) return;
    final JSONObject current = reminderPolicyData;
    final int revision = current.optInt("revision", -1);
    if (revision < 0) return;
    LinearLayout form = new LinearLayout(this);
    form.setOrientation(LinearLayout.VERTICAL);
    form.setPadding(dp(24), dp(12), dp(24), dp(12));
    TextView help = new TextView(this);
    help.setText(
        "设置保存在 Mac。安静时段限制新提醒，不中断已经开始的播报。\n"
            + "迟到从原定时间计算；选择跳过后，安静时段中的提醒也可能错过。送达未确认不会自动重播，可重新安排或取消。");
    help.setTextSize(22);
    form.addView(help);
    Switch quiet = new Switch(this);
    quiet.setText("启用安静时段（北京时间）");
    quiet.setTextSize(22);
    quiet.setChecked(current.optBoolean("quiet_enabled"));
    form.addView(quiet);
    EditText start = new EditText(this), end = new EditText(this);
    start.setHint("开始 HH:MM");
    end.setHint("结束 HH:MM");
    start.setSingleLine(true);
    end.setSingleLine(true);
    start.setText(current.optString("quiet_start"));
    end.setText(current.optString("quiet_end"));
    start.setTextSize(24);
    end.setTextSize(24);
    form.addView(start);
    form.addView(end);
    Switch skip = new Switch(this);
    skip.setText("超过迟到时限则跳过（关闭为补报）");
    skip.setTextSize(22);
    skip.setChecked("skip".equals(current.optString("late_policy")));
    form.addView(skip);
    EditText minutes = new EditText(this);
    minutes.setHint("迟到时限：1～10080 分钟");
    minutes.setSingleLine(true);
    minutes.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
    minutes.setText(String.valueOf(current.optInt("max_late_minutes", 60)));
    minutes.setTextSize(24);
    form.addView(minutes);
    ScrollView scroll = new ScrollView(this);
    scroll.addView(form);
    // Opening switches/settings must not focus an editor and summon the IME.
    form.setFocusableInTouchMode(true);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("提醒送达设置")
            .setView(scroll)
            .setNegativeButton("取消", null)
            .setNeutralButton(
                "恢复默认",
                (d, n) -> sendReminderPolicy(route, revision, false, "22:00", "07:00", false, 60))
            .setPositiveButton("应用", null)
            .create();
    reminderPolicyDialog = dialog;
    dialog.setOnDismissListener(
        unused -> {
          if (reminderPolicyDialog == dialog) reminderPolicyDialog = null;
        });
    Window policyWindow = dialog.getWindow();
    if (policyWindow != null) {
      policyWindow.clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
      policyWindow.setSoftInputMode(
          WindowManager.LayoutParams.SOFT_INPUT_STATE_HIDDEN
              | WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
    }
    dialog.setOnShowListener(
        unused -> {
          form.requestFocus();
          dialog
              .getButton(AlertDialog.BUTTON_POSITIVE)
              .setOnClickListener(
                  v -> {
                    String from = start.getText().toString().trim(),
                        to = end.getText().toString().trim();
                    String pattern = "(?:[01][0-9]|2[0-3]):[0-5][0-9]";
                    int late;
                    try {
                      late = Integer.parseInt(minutes.getText().toString().trim());
                    } catch (NumberFormatException error) {
                      late = 0;
                    }
                    if (!from.matches(pattern)
                        || !to.matches(pattern)
                        || from.equals(to)
                        || late < 1
                        || late > 10080) {
                      Toast.makeText(this, "请填写不同的 HH:MM 起止时间及有效分钟数", Toast.LENGTH_LONG).show();
                      return;
                    }
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
                    sendReminderPolicy(
                        route, revision, quiet.isChecked(), from, to, skip.isChecked(), late);
                    dialog.dismiss();
                  });
        });
    dialog.show();
  }

  private void sendReminderPolicy(
      long route,
      int revision,
      boolean quiet,
      String start,
      String end,
      boolean skip,
      int minutes) {
    if (!requireWorkspaceRoute(route)) return;
    JSONObject policy = new JSONObject();
    Wire.put(policy, "revision", revision);
    Wire.put(policy, "quiet_enabled", quiet);
    Wire.put(policy, "quiet_start", start);
    Wire.put(policy, "quiet_end", end);
    Wire.put(policy, "late_policy", skip ? "skip" : "deliver");
    Wire.put(policy, "max_late_minutes", minutes);
    JSONObject args = new JSONObject();
    Wire.put(args, "action", "set_policy");
    Wire.put(args, "policy", policy);
    if (!session.reminderAction(args, route)) workspaceNotSent();
  }

  private String repeatLabel(String value) {
    return "daily".equals(value)
        ? "每天"
        : "weekdays".equals(value) ? "工作日" : "weekly".equals(value) ? "每周" : "仅一次";
  }

  private void reminderOptions(JSONObject item, long route) {
    if (!requireWorkspaceRoute(route)) return;
    new AlertDialog.Builder(this)
        .setTitle(item.optString("title"))
        .setItems(
            new String[] {"修改提醒", "稍后 10 分钟", "取消提醒"},
            (d, n) -> {
              if (!requireWorkspaceRoute(route)) return;
              if (n == 0) {
                editReminder(item, route);
                return;
              }
              JSONObject args = Wire.event("unused");
              args.remove("event");
              Wire.put(args, "action", n == 1 ? "update" : "cancel");
              Wire.put(args, "id", item.optString("id"));
              if (n == 1) Wire.put(args, "due", formatDue(System.currentTimeMillis() + 600000));
              if (!session.reminderAction(args, route)) workspaceNotSent();
            })
        .setNegativeButton("关闭", null)
        .show();
  }

  private String formatDue(long millis) {
    java.text.SimpleDateFormat f =
        new java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", java.util.Locale.US);
    f.setTimeZone(java.util.TimeZone.getTimeZone("Asia/Shanghai"));
    return f.format(new java.util.Date(millis)) + "+08:00";
  }

  private void editReminder(JSONObject item, long route) {
    if (!requireWorkspaceRoute(route)) return;
    final boolean channelSupported = reminderChannelsSupported;
    if (!channelSupported && item != null && "desktop".equals(item.optString("channel"))) {
      Toast.makeText(this, "请连接支持桌面通知的后台后修改", Toast.LENGTH_LONG).show();
      return;
    }
    LinearLayout form = new LinearLayout(this);
    form.setOrientation(LinearLayout.VERTICAL);
    form.setPadding(dp(24), dp(16), dp(24), dp(8));
    EditText title = new EditText(this);
    title.setTextColor(INK);
    title.setTextSize(22);
    title.setHintTextColor(MUTED);
    title.setHint("提醒内容");
    title.setSingleLine(true);
    title.setFilters(
        new android.text.InputFilter[] {new android.text.InputFilter.LengthFilter(120)});
    if (item != null) title.setText(item.optString("title"));
    form.addView(title);
    java.util.Calendar date =
        java.util.Calendar.getInstance(java.util.TimeZone.getTimeZone("Asia/Shanghai"));
    date.add(java.util.Calendar.HOUR_OF_DAY, 1);
    date.set(java.util.Calendar.SECOND, 0);
    date.set(java.util.Calendar.MILLISECOND, 0);
    if (item != null)
      try {
        java.text.SimpleDateFormat f =
            new java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", java.util.Locale.US);
        f.setTimeZone(date.getTimeZone());
        date.setTime(f.parse(item.optString("due").substring(0, 19)));
      } catch (java.text.ParseException | IndexOutOfBoundsException ignored) {
      }
    Button time =
        action(dateLabel(formatDue(date.getTimeInMillis())), android.R.drawable.ic_menu_today);
    form.addView(time, new LinearLayout.LayoutParams(-1, dp(58)));
    time.setOnClickListener(
        v ->
            new android.app.DatePickerDialog(
                    this,
                    (picker, y, m, d) -> {
                      date.set(y, m, d);
                      new android.app.TimePickerDialog(
                              this,
                              (clock, h, min) -> {
                                date.set(java.util.Calendar.HOUR_OF_DAY, h);
                                date.set(java.util.Calendar.MINUTE, min);
                                time.setText(dateLabel(formatDue(date.getTimeInMillis())));
                              },
                              date.get(java.util.Calendar.HOUR_OF_DAY),
                              date.get(java.util.Calendar.MINUTE),
                              true)
                          .show();
                    },
                    date.get(java.util.Calendar.YEAR),
                    date.get(java.util.Calendar.MONTH),
                    date.get(java.util.Calendar.DAY_OF_MONTH))
                .show());
    Spinner repeat = new Spinner(this);
    String[] keys = {"none", "daily", "weekdays", "weekly"};
    repeat.setAdapter(
        new ArrayAdapter<>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            new String[] {"仅一次", "每天", "工作日", "每周"}));
    if (item != null)
      for (int i = 0; i < keys.length; i++)
        if (keys[i].equals(item.optString("repeat"))) repeat.setSelection(i);
    form.addView(repeat, new LinearLayout.LayoutParams(-1, dp(54)));
    Spinner channel = new Spinner(this);
    String[] channels = {"cc8", "desktop"};
    channel.setAdapter(
        new ArrayAdapter<>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            channelSupported
                ? new String[] {"CC8 语音播报", "Mac 桌面静默通知"}
                : new String[] {"CC8 语音播报"}));
    if (item != null && "desktop".equals(item.optString("channel"))) channel.setSelection(1);
    form.addView(channel, new LinearLayout.LayoutParams(-1, dp(54)));
    TextView note =
        text(
            "两种渠道都需 Mac 运行、CC8 前台连接且空闲。桌面通知需先在 Mac 完成安装和授权；未授权时显示未确认，不自动改成播报。修改收到 Mac 保存回执后生效。",
            19,
            INK);
    form.addView(note);
    ScrollView scroll = new ScrollView(this);
    scroll.addView(form);
    form.setFocusableInTouchMode(true);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle(item == null ? "添加提醒" : "修改提醒")
            .setView(scroll)
            .setPositiveButton("保存", null)
            .setNegativeButton("取消", null)
            .create();
    reminderEditorDialog = dialog;
    dialog.setOnDismissListener(
        unused -> {
          if (reminderEditorDialog == dialog) reminderEditorDialog = null;
        });
    Window editorWindow = dialog.getWindow();
    if (editorWindow != null) {
      editorWindow.clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
      editorWindow.setSoftInputMode(
          WindowManager.LayoutParams.SOFT_INPUT_STATE_HIDDEN
              | WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
    }
    dialog.setOnShowListener(
        d -> {
          form.requestFocus();
          dialog
              .getButton(AlertDialog.BUTTON_POSITIVE)
              .setOnClickListener(
                  v -> {
                    String value = title.getText().toString().trim();
                    if (value.isEmpty()) {
                      title.setError("请输入提醒内容");
                      return;
                    }
                    if (date.getTimeInMillis() <= System.currentTimeMillis()) {
                      Toast.makeText(this, "请选择将来的时间", Toast.LENGTH_LONG).show();
                      return;
                    }
                    if (!requireWorkspaceRoute(route)) return;
                    JSONObject args = new JSONObject();
                    Wire.put(args, "action", item == null ? "create" : "update");
                    if (item != null) Wire.put(args, "id", item.optString("id"));
                    Wire.put(args, "title", value);
                    Wire.put(args, "due", formatDue(date.getTimeInMillis()));
                    Wire.put(args, "repeat", keys[repeat.getSelectedItemPosition()]);
                    if (channelSupported)
                      Wire.put(args, "channel", channels[channel.getSelectedItemPosition()]);
                    if (!session.reminderAction(args, route)) {
                      workspaceNotSent();
                      return;
                    }
                    dialog.dismiss();
                  });
        });
    dialog.show();
  }

  private void showSettings() {
    new AlertDialog.Builder(this)
        .setTitle("大黄蜂设置")
        .setItems(
            new String[] {
              "媒体音量",
              "助理大脑",
              "提醒管理",
              "检索来源",
              "最近 Dot 完整结果",
              "连接与桌面入口",
              "隐私与语音说明",
              "立即打断播报",
              "Dot 聊天接续",
              "声音与自然插话",
              "提醒送达记录",
              "偏好记忆",
              "识别语言",
              "看图设置",
              "识别图片文字",
              "追问最近照片",
              "听音环境",
              "用户词表／选词纠错"
            },
            (d, n) -> {
              if (n == 0) {
                android.media.AudioManager audio =
                    (android.media.AudioManager) getSystemService(AUDIO_SERVICE);
                SeekBar volume = new SeekBar(this);
                volume.setMax(audio.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC));
                volume.setProgress(audio.getStreamVolume(android.media.AudioManager.STREAM_MUSIC));
                volume.setPadding(dp(24), dp(18), dp(24), dp(18));
                volume.setOnSeekBarChangeListener(
                    new SeekBar.OnSeekBarChangeListener() {
                      public void onProgressChanged(SeekBar bar, int p, boolean user) {
                        if (user)
                          audio.setStreamVolume(android.media.AudioManager.STREAM_MUSIC, p, 0);
                      }

                      public void onStartTrackingTouch(SeekBar b) {}

                      public void onStopTrackingTouch(SeekBar b) {}
                    });
                new AlertDialog.Builder(this)
                    .setTitle("媒体音量")
                    .setView(volume)
                    .setPositiveButton("完成", null)
                    .show();
              } else if (n == 1) showBrainSelector();
              else if (n == 2) showReminders();
              else if (n == 3) {
                if (sourceCount() > 0) showSources();
                else Toast.makeText(this, "本轮暂无检索来源", Toast.LENGTH_SHORT).show();
              } else if (n == 4) {
                if (dotResult.isEmpty())
                  Toast.makeText(this, "暂无 Dot 结果", Toast.LENGTH_SHORT).show();
                else showDotResult();
              } else if (n == 5) showConnectionInfo();
              else if (n == 6) {
                updatePrivacy();
                new AlertDialog.Builder(this)
                    .setTitle("隐私与语音说明")
                    .setMessage(privacyText.getText())
                    .setPositiveButton("知道了", null)
                    .show();
              } else if (n == 7) session.interrupt();
              else if (n == 8) dotChats.open();
              else if (n == 9) showVoicePreferences();
              else if (n == 11) preferenceMemory.open();
              else if (n == 12) recognitionLanguage.open();
              else if (n == 13) showImageQuality();
              else if (n == 14) requestImage("ocr");
              else if (n == 15) showPhotoQuestion();
              else if (n == 16) recognitionEnvironment.open();
              else if (n == 17) vocabulary.open();
              else showReminderHistory();
            })
        .setNegativeButton("关闭", null)
        .show();
  }

  private void showVoicePreferences() {
    final long route = workspaceRoute;
    JSONObject current = session.voicePreferences(route);
    if (current == null) {
      Toast.makeText(this, "请连接支持声音设置的 Mac 后台", Toast.LENGTH_LONG).show();
      return;
    }
    final int revision = current.optInt("revision");
    LinearLayout content = new LinearLayout(this);
    content.setOrientation(LinearLayout.VERTICAL);
    content.setPadding(dp(24), dp(12), dp(24), dp(12));
    TextView help = new TextView(this);
    help.setText(
        "请先暂停语音并等待任务结束，再应用设置。\n"
            + "语速相对当前音色调整，标准可恢复原语速。关闭自然插话后，播报时的新问题不会打断；仍可说“停一下”或点击停止。\n"
            + "设置在本次 Mac 服务内有效；更改不会开启麦克风。");
    help.setTextSize(22);
    content.addView(help);
    RadioGroup rates = new RadioGroup(this);
    rates.setOrientation(RadioGroup.HORIZONTAL);
    String[] values = {"slow", "normal", "fast"};
    String[] labels = {"舒缓", "标准", "稍快"};
    int[] ids = {View.generateViewId(), View.generateViewId(), View.generateViewId()};
    for (int i = 0; i < values.length; i++) {
      RadioButton option = new RadioButton(this);
      option.setId(ids[i]);
      option.setText(labels[i]);
      option.setTextSize(24);
      rates.addView(option);
      if (values[i].equals(current.optString("speech_rate"))) rates.check(ids[i]);
    }
    content.addView(rates);
    CheckBox barge = new CheckBox(this);
    barge.setText("允许说新问题打断播报");
    barge.setTextSize(24);
    barge.setChecked(current.optBoolean("natural_barge"));
    content.addView(barge);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("声音与自然插话")
            .setView(content)
            .setNegativeButton("取消", null)
            .setNeutralButton("恢复默认", null)
            .setPositiveButton("应用", null)
            .create();
    dialog.setOnShowListener(
        unused -> {
          dialog
              .getButton(AlertDialog.BUTTON_NEUTRAL)
              .setOnClickListener(
                  v -> {
                    rates.check(ids[1]);
                    barge.setChecked(true);
                  });
          dialog
              .getButton(AlertDialog.BUTTON_POSITIVE)
              .setOnClickListener(
                  v -> {
                    int selected = rates.getCheckedRadioButtonId();
                    for (int i = 0; i < ids.length; i++) {
                      if (selected != ids[i]) continue;
                      if (!session.setVoicePreferences(
                          values[i], barge.isChecked(), revision, route))
                        Toast.makeText(this, "连接已变化，请重新打开声音设置", Toast.LENGTH_LONG).show();
                      dialog.dismiss();
                      break;
                    }
                  });
        });
    dialog.show();
  }

  private void showImageQuality() {
    if (!foreground) return;
    if (session.isVisual()) {
      Toast.makeText(this, "请先结束本次看图，再修改设置", Toast.LENGTH_LONG).show();
      return;
    }
    if (imageQualityDialog != null) imageQualityDialog.dismiss();
    LinearLayout content = new LinearLayout(this);
    content.setOrientation(LinearLayout.VERTICAL);
    content.setPadding(dp(24), dp(12), dp(24), dp(12));
    TextView help =
        text("默认预览 5 秒后直接分析。开启拍后核对后，可发送、重拍和裁剪；最多三张，等待最长 70 秒，拍完即关闭相机。设置仅本次界面有效，不会打开相机。", 22, INK);
    content.addView(help);
    RadioGroup quality = new RadioGroup(this);
    int standardId = View.generateViewId(), hdId = View.generateViewId();
    RadioButton standard = new RadioButton(this);
    standard.setId(standardId);
    standard.setText("标准（默认）");
    standard.setTextSize(24);
    quality.addView(standard);
    RadioButton hd = new RadioButton(this);
    hd.setId(hdId);
    hd.setText("高清（需相机和 Mac 后台支持）");
    hd.setTextSize(24);
    quality.addView(hd);
    quality.check(highDefinitionImage ? hdId : standardId);
    content.addView(quality);
    CheckBox review = new CheckBox(this);
    review.setText("拍后核对，可重拍／裁剪");
    review.setTextSize(24);
    review.setChecked(reviewCapturedImage);
    content.addView(review);
    CheckBox keep = new CheckBox(this);
    keep.setText("允许追问：首次回答后在 Mac 内存保留这张照片 2 分钟");
    keep.setTextSize(24);
    keep.setChecked(retainCapturedPhoto);
    content.addView(keep);
    ScrollView scroll = new ScrollView(this);
    scroll.addView(content);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("看图设置")
            .setView(scroll)
            .setPositiveButton(
                "应用",
                (d, n) -> {
                  if (foreground && imageQualityDialog == d) {
                    if (session.isVisual()) {
                      Toast.makeText(this, "正在处理照片，请结束后再修改设置", Toast.LENGTH_LONG).show();
                      return;
                    }
                    highDefinitionImage = quality.getCheckedRadioButtonId() == hdId;
                    reviewCapturedImage = review.isChecked();
                    if (retainCapturedPhoto
                        && !keep.isChecked()
                        && !session.forgetPhotoForFollowup())
                      Toast.makeText(this, "清除照片未确认，请检查连接", Toast.LENGTH_LONG).show();
                    retainCapturedPhoto = keep.isChecked();
                  }
                })
            .setNegativeButton("取消", null)
            .create();
    imageQualityDialog = dialog;
    dialog.setOnDismissListener(
        d -> {
          if (imageQualityDialog == dialog) imageQualityDialog = null;
        });
    dialog.show();
  }

  private void showPhotoQuestion() {
    if (!foreground) return;
    final PhotoFollowupWindow.Picture picture = session.photoForFollowup();
    if (picture == null) {
      Toast.makeText(this, "没有可追问的照片。请在看图设置明确允许保留，再拍一张。", Toast.LENGTH_LONG).show();
      return;
    }
    if (imageQualityDialog != null) imageQualityDialog.dismiss();
    LinearLayout content = new LinearLayout(this);
    content.setOrientation(LinearLayout.VERTICAL);
    content.setPadding(dp(24), dp(12), dp(24), dp(12));
    content.addView(
        text(
            "照片 #"
                + picture.id.substring(0, 6)
                + " · "
                + picture.width
                + "×"
                + picture.height
                + "。本次会把同一张照片交给 Dot，不重新打开相机；追问不会延长原保留窗口。语音已开启时，也可说‘追问这张照片，……’。",
            22,
            INK));
    EditText question = new EditText(this);
    question.setTextSize(24);
    question.setHint("例如：这张照片上的文字是什么意思？");
    question.setInputType(
        android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE);
    question.setMaxLines(4);
    question.setFilters(
        new android.text.InputFilter[] {new android.text.InputFilter.LengthFilter(1000)});
    content.addView(question, new LinearLayout.LayoutParams(-1, -2));
    ScrollView scroll = new ScrollView(this);
    scroll.addView(content);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("追问这张照片")
            .setView(scroll)
            .setPositiveButton("发送追问", null)
            .setNegativeButton("取消", null)
            .setNeutralButton(
                "清除照片",
                (d, n) -> {
                  if (foreground && imageQualityDialog == d && !session.forgetPhotoForFollowup())
                    Toast.makeText(this, "清除照片未确认，请检查连接", Toast.LENGTH_LONG).show();
                })
            .create();
    imageQualityDialog = dialog;
    dialog.setOnDismissListener(
        d -> {
          if (imageQualityDialog == dialog) imageQualityDialog = null;
        });
    dialog.setOnShowListener(
        d -> {
          dialog
              .getWindow()
              .setSoftInputMode(
                  android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
                      | android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN);
          dialog
              .getButton(AlertDialog.BUTTON_POSITIVE)
              .setOnClickListener(
                  v -> {
                    if (!foreground || imageQualityDialog != dialog) return;
                    String value = question.getText().toString();
                    if (!PhotoFollowupWindow.validQuestion(value)) {
                      Toast.makeText(this, "请输入有效问题（最多 1000 字符）", Toast.LENGTH_LONG).show();
                      return;
                    }
                    if (!session.requestPhotoFollowup(picture, value)) {
                      Toast.makeText(this, "照片或连接已变化，请重新打开入口", Toast.LENGTH_LONG).show();
                      return;
                    }
                    dialog.dismiss();
                  });
          new android.os.Handler(android.os.Looper.getMainLooper())
              .postDelayed(
                  new Runnable() {
                    public void run() {
                      if (imageQualityDialog != dialog) return;
                      if (!foreground || session.photoForFollowup() != picture) dialog.dismiss();
                      else
                        new android.os.Handler(android.os.Looper.getMainLooper())
                            .postDelayed(this, 500);
                    }
                  },
                  500);
        });
    dialog.show();
  }

  private void requestImage(String mode) {
    if (!foreground) return;
    if (session.isVisual()) {
      eyes.cancel();
      session.cancelVision();
      return;
    }
    if ("ocr".equals(mode) && !session.supportsVisionModes()) {
      Toast.makeText(this, "请连接支持识字入口的 Mac 后台", Toast.LENGTH_LONG).show();
      return;
    }
    if ("mac_window".equals(mode)) {
      // This path must not request Android camera permission or start Eyes.
      if (!session.supportsMacWindow() || !session.requestVision(mode))
        Toast.makeText(this, "Mac 窗口入口尚未就绪，请检查 Mac 的安装和连接", Toast.LENGTH_LONG).show();
      return;
    }
    if (Build.VERSION.SDK_INT >= 23
        && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
      requestPermissions(new String[] {Manifest.permission.CAMERA}, 34);
      return;
    }
    if (!session.requestVision(mode))
      Toast.makeText(this, "未提交拍摄，请确认连接后重试", Toast.LENGTH_LONG).show();
  }

  @Override
  public void vision(boolean busy, String message, boolean preserveError) {
    runOnUiThread(
        () -> {
          if (!busy && eyes != null) eyes.cancel();
          if (visionButton != null) {
            visionButton.setText(busy ? "停止看图" : "看一看");
            visionButton.setContentDescription(busy ? "停止拍摄和 Dot 看图分析" : "拍一张画面交给 Dot 分析");
          }
          if (busy || !preserveError)
            state(busy ? "thinking" : active ? "listening" : "idle", message);
        });
  }

  @Override
  public void cameraCapture(JSONObject request) {
    runOnUiThread(
        () -> {
          if (!foreground || !session.isVisionRequestCurrent(request)) {
            session.cameraFailed(request, "not_foreground");
            return;
          }
          if (Build.VERSION.SDK_INT >= 23
              && checkSelfPermission(Manifest.permission.CAMERA)
                  != PackageManager.PERMISSION_GRANTED) {
            session.cameraFailed(request, "permission_required");
            state("idle", "请点看一看允许摄像头权限，然后重试");
            return;
          }
          final CaptureProfile profile;
          try {
            profile = CaptureProfile.fromRequest(request);
          } catch (IllegalArgumentException error) {
            session.cameraFailed(request, "unsupported_preview");
            return;
          }
          final boolean keepPhoto = retainCapturedPhoto && profile.followupAllowed;
          eyes.start(
              new SingleFrameCamera.Result() {
                public void image(byte[] jpeg, int width, int height) {
                  if (foreground && session.isVisionRequestCurrent(request)) {
                    state("thinking", "摄像头已关闭 · 正在把本次画面交给 Dot");
                    session.imageReady(request, jpeg, width, height, keepPhoto);
                  }
                }

                public void error(String reason) {
                  session.cameraFailed(request, reason);
                }
              },
              highDefinitionImage,
              profile,
              reviewCapturedImage,
              () -> foreground && session.isVisionRequestCurrent(request));
        });
  }

  private String connectionInfoMessage(JSONObject info) {
    JSONObject availability = info.optJSONObject("connection_availability");
    String transport = info.optString("transport");
    String current = "usb".equals(transport) ? "USB" : "wifi".equals(transport) ? "Wi-Fi" : "等待连接";
    return "当前通道："
        + current
        + "\n连接策略：USB 优先，断线自动改用 Wi-Fi\n\n"
        + (info.optBoolean("wifi_ready")
            ? "Wi-Fi 备用连接已就绪"
            : info.optBoolean("wifi_paired") ? "Wi-Fi 已配对，正在等待局域网连接" : "首次 Wi-Fi 配对请先通过 USB 连接 Mac")
        + "\n\n"
        + (availability == null ? "连接观测尚未就绪" : availability.optString("summary"))
        + "\n\n仅统计本次界面会话的前台观测，退回桌面期间不计时。断线时间从设备确认连接丢失时开始，不代表故障实际发生时刻。"
        + "\nMac 休眠、服务退出或网络中断时助手均可能不可用；仅凭断线不能确定原因。"
        + "\n\nWi-Fi 需要两台设备处于同一局域网，且 Mac 语音服务正在运行。";
  }

  private void showConnectionInfo() {
    if (!foreground) return;
    if (connectionInfoDialog != null) connectionInfoDialog.dismiss();
    JSONObject initial = session.snapshot();
    final String[] displayed = {connectionInfoMessage(initial)};
    final AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("连接状态")
            .setMessage(displayed[0])
            .setNeutralButton("桌面入口", (d, which) -> DesktopEntryService.settings(this))
            .setPositiveButton("知道了", null)
            .create();
    connectionInfoDialog = dialog;
    connectionInfoAvailability = initial.optJSONObject("connection_availability");
    connectionInfoRenderedMessage = displayed[0];
    final Runnable update =
        new Runnable() {
          public void run() {
            if (!foreground || connectionInfoDialog != dialog || !dialog.isShowing()) return;
            JSONObject info = session.snapshot();
            String next = connectionInfoMessage(info);
            if (!next.equals(displayed[0])) {
              displayed[0] = next;
              dialog.setMessage(next);
              connectionInfoAvailability = info.optJSONObject("connection_availability");
              connectionInfoRenderedMessage = next;
            }
            handler.postDelayed(this, 1000);
          }
        };
    dialog.setOnDismissListener(
        d -> {
          handler.removeCallbacks(update);
          if (connectionInfoDialog == dialog) {
            connectionInfoDialog = null;
            connectionInfoAvailability = null;
            connectionInfoRenderedMessage = "";
          }
        });
    dialog.show();
    handler.postDelayed(update, 1000);
  }

  private JSONObject connectionDialogObservation() {
    JSONObject result = new JSONObject();
    AlertDialog dialog = connectionInfoDialog;
    boolean visible = foreground && dialog != null && dialog.isShowing();
    Wire.put(result, "visible", visible);
    if (!visible) return result;
    TextView message = (TextView) dialog.findViewById(android.R.id.message);
    boolean matched =
        message != null && connectionInfoRenderedMessage.contentEquals(message.getText());
    Wire.put(result, "rendered_message_matches", matched);
    // Read the actual TextView, but export only the associated fixed state and
    // counters. Never dump arbitrary dialog/user text or a newly computed tick.
    if (matched && connectionInfoAvailability != null)
      Wire.put(result, "connection_availability", connectionInfoAvailability);
    return result;
  }

  private void showBrainSelector() {
    new AlertDialog.Builder(this)
        .setTitle("选择助理大脑")
        .setSingleChoiceItems(
            new String[] {"MiniMax · 对话与联网查询", "Dot / Codex · 思考与执行"},
            "dot".equals(brainProvider) ? 1 : 0,
            (dialog, which) -> {
              session.brain(which == 1 ? "dot" : "minimax");
              dialog.dismiss();
            })
        .setNegativeButton("关闭", null)
        .show();
  }

  private void showDotResult() {
    if (dotResult.isEmpty()) return;
    String displayed =
        (dotResultSource.isEmpty() ? "" : "摘要接续来源：" + dotResultSource + "\n结果未追加原桌面聊天\n\n")
            + dotResult;
    TextView detail = text(displayed, 22, INK);
    detail.setTextIsSelectable(true);
    detail.setPadding(dp(22), dp(16), dp(22), dp(16));
    ScrollView scroll = new ScrollView(this);
    scroll.addView(detail);
    new AlertDialog.Builder(this)
        .setTitle("Dot / Codex · 任务结果")
        .setView(scroll)
        .setNeutralButton(
            "复制结果",
            (d, n) -> {
              android.content.ClipboardManager clipboard =
                  (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
              clipboard.setPrimaryClip(
                  android.content.ClipData.newPlainText("Dot 任务结果", displayed));
              Toast.makeText(this, "已复制，可在目标桌面聊天中粘贴", Toast.LENGTH_SHORT).show();
            })
        .setPositiveButton("关闭", null)
        .show();
  }

  @Override
  public void brain(String provider) {
    runOnUiThread(
        () -> {
          brainProvider = "dot".equals(provider) ? "dot" : "minimax";
          brainText.setText(
              "dot".equals(brainProvider) ? (linked ? "Dot 已连接" : "Dot 未连接") : "MiniMax");
          updatePrivacy();
        });
  }

  @Override
  public void dotResult(String value, boolean truncated, String sourceTitle) {
    runOnUiThread(
        () -> {
          dotResult = value;
          dotResultSource = sourceTitle;
        });
  }

  @Override
  public void dotChats(JSONObject data) {
    runOnUiThread(() -> dotChats.accept(data));
  }

  @Override
  public void savedTasks(JSONObject data) {
    runOnUiThread(
        () -> {
          savedTasks.accept(data);
          resultDetails.accept(data);
        });
  }

  @Override
  public void preferenceResult(JSONObject data, long route) {
    runOnUiThread(
        () -> {
          if (foreground && preferenceMemory != null) preferenceMemory.accept(data, route);
        });
  }

  @Override
  public void vocabularyResult(JSONObject data, long route) {
    runOnUiThread(
        () -> {
          if (foreground && session.currentRoute(route) && vocabulary != null)
            vocabulary.accept(data, route);
        });
  }

  @Override
  public void speechCorrection(JSONObject data, long route) {
    runOnUiThread(
        () -> {
          if (foreground && correctionDialog != null) correctionDialog.accept(data, route);
        });
  }

  private void traceAuto(String event) {
    long now = SystemClock.elapsedRealtime();
    String value =
        event
            + " fg="
            + foreground
            + " link="
            + linked
            + " mic="
            + (session != null && session.isRecording())
            + " wait="
            + autoVoice.waiting(now)
            + " staged="
            + autoVoice.staged();
    synchronized (autoVoiceTrace) {
      if (autoVoiceTrace.size() >= 32) autoVoiceTrace.removeFirst();
      autoVoiceTrace.addLast(value);
    }
  }

  private void requestOpenVoice(Intent intent) {
    pending = false;
    permissionGrantedAwaitingResume = false;
    lastOpenWantedVoice = intent == null || intent.getBooleanExtra("auto_start_voice", true);
    pendingTestCaptureLimitMs =
        CaptureWindow.requested(
            intent != null && intent.hasExtra("test_capture_limit_ms"),
            intent != null && intent.hasExtra("test_capture_limit_ms")
                ? intent.getExtras().get("test_capture_limit_ms")
                : null);
    if (pendingTestCaptureLimitMs < 0) {
      // A malformed test limit must not silently become unlimited capture.
      lastOpenWantedVoice = false;
      pendingTestCaptureLimitMs = 0;
    }
    if (session != null && session.isRecording()) pendingTestCaptureLimitMs = 0;
    autoVoiceOpens++;
    if (lastOpenWantedVoice) autoVoice.opened(SystemClock.elapsedRealtime());
    else autoVoice.cancel();
    traceAuto("open:" + lastOpenWantedVoice);
  }

  private void maybeAutoVoice() {
    handler.removeCallbacks(autoVoiceTick);
    if (!foreground) return;
    long now = SystemClock.elapsedRealtime();
    if (autoVoice.expire(now)) {
      state("idle", "连接超时，请连接后点击开始语音");
      return;
    }
    if (autoVoice.take(now, true, linked && session.isConnected(), session.isRecording())) {
      autoVoiceStarts++;
      traceAuto("auto_take");
      begin();
      return;
    }
    if (autoVoice.waiting(now)) {
      traceAuto("await");
      state("idle", "正在连接 Mac，连接后将自动开始语音");
      handler.postDelayed(autoVoiceTick, 500);
    }
  }

  @Override
  protected void onNewIntent(Intent intent) {
    super.onNewIntent(intent);
    setIntent(intent);
    requestOpenVoice(intent);
  }

  private void begin() {
    traceAuto("begin");
    if (!foreground) return;
    if (!linked) {
      state("idle", "请连接 USB 或与 Mac 处于同一局域网");
      return;
    }
    autoVoice.cancel();
    handler.removeCallbacks(autoVoiceTick);
    if (Build.VERSION.SDK_INT >= 23
        && checkSelfPermission(Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
      pending = true;
      requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, 21);
      return;
    }
    session.settings(wake.isChecked());
    long testLimit = pendingTestCaptureLimitMs;
    pendingTestCaptureLimitMs = 0;
    session.startMicrophone(testLimit);
  }

  @Override
  public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
    super.onRequestPermissionsResult(request, permissions, results);
    if (request == 34) {
      state(
          "idle",
          results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED
              ? "摄像头已授权，请重新选择看图或识字"
              : "未获得摄像头权限，未采集图片");
    }
    if (request == 21) {
      boolean allowed = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
      boolean requested = pending;
      pending = false;
      if (requested && allowed) {
        autoVoice.opened(SystemClock.elapsedRealtime());
        if (foreground) {
          autoVoice.resumed(SystemClock.elapsedRealtime());
          traceAuto("resumed_gate");
          maybeAutoVoice();
        } else permissionGrantedAwaitingResume = true;
      } else if (!allowed) state("idle", "未获得麦克风权限，尚未开始采集");
    }
  }

  private void updatePrivacy() {
    if (privacyText == null) return;
    String ears = "minimax".equals(recognitionEngine) ? "语音片段发送 MiniMax 云端识别" : "语音在当前 Mac 本机识别";
    privacyText.setText(
        ears
            + "\n"
            + ("dot".equals(brainProvider) ? "Dot 思考" : "MiniMax 思考")
            + " · MiniMax 播报\n"
            + (!active
                ? "首次开启请安静等待 6 秒校准"
                : wake.isChecked() ? "15 分钟黑屏待机 · 大黄蜂 / Bumblebee 唤醒" : "自由对话 · 闲置 15 分钟后等待唤醒")
            + "\n看一看：仅拍一张交给 Dot，不保存照片");
  }

  @Override
  public void recognition(String engine) {
    runOnUiThread(
        () -> {
          recognitionEngine = engine;
          updatePrivacy();
        });
  }

  private void refresh() {
    if (start == null) return;
    updatePrivacy();
    boolean waiting = foreground && autoVoice.waiting(SystemClock.elapsedRealtime());
    start.setText(
        active
            ? "暂停语音"
            : playbackActive
                ? ("thinking".equals(phase) ? "停止任务" : "停止播报")
                : waiting && !linked ? "取消等待" : "开始语音");
    start.setEnabled(linked || active || waiting);
    if (visionButton != null) {
      visionButton.setEnabled(linked && !"calibrating".equals(phase));
      buttonStyle(visionButton, false);
    }
    interrupt.setEnabled(
        (active || playbackActive || session.isVisual()) && !"calibrating".equals(phase));
    buttonStyle(start, false);
    buttonStyle(interrupt, false);
    refreshModes();
    brainText.setEnabled(!"calibrating".equals(phase));
    micText.setText(active ? "麦克风开启" : "麦克风关闭");
    micText.setCompoundDrawables(
        icon(R.drawable.ic_hud_mic, active ? CYAN : AMBER, 29), null, null, null);
    micText.setTextColor(active ? CYAN : AMBER);
    statusLabel();
    if (foreground) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
  }

  @Override
  public void link(boolean connected, long route) {
    runOnUiThread(
        () -> {
          if (!session.currentRoute(route)) return;
          linked = connected;
          if (workspaceRoute != route || !connected) {
            clearSources();
            workspaceRoute = route;
            workspaceRevision = -1;
            taskResultUnconfirmed |= hasRunningTask(tasksData);
            dotChats.close();
            preferenceMemory.close();
            recognitionLanguage.close();
            recognitionEnvironment.close();
            vocabulary.close();
            if (imageQualityDialog != null) imageQualityDialog.dismiss();
            savedTasks.close();
            resultDetails.close();
            if (correctionDialog != null) correctionDialog.close();
            if (reminderPolicyDialog != null) reminderPolicyDialog.dismiss();
            if (reminderEditorDialog != null) reminderEditorDialog.dismiss();
            if (reminderHistoryDialog != null) reminderHistoryDialog.dismiss();
            tasksData = new JSONArray();
            remindersData = new JSONArray();
            reminderPolicyData = null;
            reminderHistoryData = new JSONArray();
            reminderChannelsSupported = false;
            renderWorkspace();
          }
          brainText.setText(
              "dot".equals(brainProvider) ? (connected ? "Dot 已连接" : "Dot 未连接") : "MiniMax");
          traceAuto("link:" + connected);
          if (!connected) screen(false);
          String transport = "wifi".equals(session.transportName()) ? "Wi-Fi" : "USB";
          linkText.setText(connected ? ("USB".equals(transport) ? "USB 优先" : "Wi-Fi") : "未连接");
          companion.setTransport(transport);
          linkText.setCompoundDrawables(
              icon(
                  "Wi-Fi".equals(transport) ? R.drawable.ic_hud_wifi : R.drawable.ic_hud_usb,
                  connected ? INK : MUTED,
                  25),
              null,
              null,
              null);
          linkText.setTextColor(connected ? CYAN : MUTED);
          refresh();
          maybeAutoVoice();
        });
  }

  @Override
  public void state(String next, String message) {
    runOnUiThread(() -> applyState(next, message));
  }

  @Override
  public void status(StatusPresentation update) {
    runOnUiThread(
        () -> {
          if (!session.currentStatusPresentation(update)) return;
          if (!listeningState.hostStatus(
              update.phase,
              update.listeningState,
              foreground && session.isRecording(),
              update.generation)) return;
          applyState(update.phase, update.message);
        });
  }

  private void applyState(String next, String message) {
    listeningState.localPhase(next);
    phase = next;
    lastStateMessage = message;
    refresh();
  }

  @Override
  public void caption(String text, String emotion) {
    runOnUiThread(() -> applyCaption(text, emotion));
  }

  @Override
  public void playbackPresentation(VoiceSession.PlaybackPresentation update) {
    runOnUiThread(
        () -> {
          if (!foreground || !session.currentPlaybackPresentation(update)) return;
          if (update.text != null) applyCaption(update.text, update.mood);
          applyState(update.phase, update.message);
        });
  }

  private void applyCaption(String text, String emotion) {
    captionPlaceholder = text.isEmpty();
    final String value =
        text.isEmpty() ? (active ? "说大黄蜂唤醒我。\n也可以点击看一看。" : "麦克风已关闭。\n点击开始语音即可对话。") : text;
    if (!value.equals(captionText.getText().toString())) {
      captionText.setText(value);
      captionText.progress(0, false);
      captionScroll.scrollTo(0, 0);
      captionId = 0;
      lastCaptionLine = -1;
    }
    mood = emotion;
  }

  @Override
  public void heard(String text) {
    runOnUiThread(() -> heardText.setText("你：" + text));
  }

  @Override
  public void sources(SourcesPresentation update) {
    runOnUiThread(
        () -> {
          if (!foreground || !session.currentSourcesPresentation(update)) return;
          clearSources();
          displayedSources = update;
          sourceText.setVisibility(sourceCount() == 0 ? View.GONE : View.VISIBLE);
          if (sourceCount() > 0) sourceText.setText("链接 " + sourceCount() + " 条 · 查看依据");
        });
  }

  private int sourceCount() {
    return displayedSources == null ? 0 : displayedSources.size();
  }

  private void clearSources() {
    displayedSources = null;
    if (sourcesDialog != null) sourcesDialog.dismiss();
    if (sourceText != null) sourceText.setVisibility(View.GONE);
  }

  private void showSources() {
    final SourcesPresentation current = displayedSources;
    if (!foreground || current == null || !session.currentSourcesPresentation(current)) {
      clearSources();
      return;
    }
    if (current.size() == 0) return;
    if (sourcesDialog != null) sourcesDialog.dismiss();
    String[] names = new String[current.size()];
    for (int i = 0; i < names.length; i++) {
      names[i] = current.at(i).label();
    }
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle(current.heading())
            .setItems(
                names,
                (selected, index) -> {
                  if (!foreground
                      || sourcesDialog != selected
                      || displayedSources != current
                      || !session.currentSourcesPresentation(current)) return;
                  String url = current.at(index).url;
                  try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                  } catch (ActivityNotFoundException | SecurityException e) {
                    TextView link = text(url, 22, INK);
                    link.setTextIsSelectable(true);
                    link.setPadding(dp(24), dp(20), dp(24), dp(20));
                    final AlertDialog fallback =
                        new AlertDialog.Builder(this)
                            .setTitle("设备无法打开浏览器")
                            .setView(link)
                            .setPositiveButton("知道了", null)
                            .create();
                    sourcesDialog = fallback;
                    fallback.setOnDismissListener(
                        d -> {
                          if (sourcesDialog == fallback) sourcesDialog = null;
                        });
                    fallback.show();
                  }
                })
            .setNegativeButton("关闭", null)
            .create();
    sourcesDialog = dialog;
    dialog.setOnDismissListener(
        d -> {
          if (sourcesDialog == dialog) sourcesDialog = null;
        });
    dialog.show();
  }

  @Override
  public void playback(boolean value) {
    runOnUiThread(
        () -> {
          playbackActive = value;
          refresh();
        });
  }

  @Override
  public void captureRejected(int stoppedEpoch) {
    runOnUiThread(
        () -> {
          if (!foreground || !session.captureRejectionCurrent(stoppedEpoch)) return;
          applyState("error", "Mac 未能启动语音处理，麦克风已关闭。请点击开始语音重试。");
        });
  }

  @Override
  public void microphone(boolean value) {
    runOnUiThread(
        () -> {
          active = session.isRecording();
          if (!active) {
            listeningState.localPhase("idle");
            screen(false);
          }
          if (active) heardText.setText("等待你的指令");
          refresh();
        });
  }

  @Override
  public void screen(boolean sleeping) {
    runOnUiThread(
        () -> {
          if (sleeping && (!foreground || !active)) return;
          if (screenSleeping == sleeping) return;
          screenSleeping = sleeping;
          WindowManager.LayoutParams attrs = getWindow().getAttributes();
          if (sleeping) {
            previousBrightness = attrs.screenBrightness;
            attrs.screenBrightness = 0;
            blackScreen.setVisibility(View.VISIBLE);
            handler.removeCallbacks(animate);
          } else {
            attrs.screenBrightness = previousBrightness;
            blackScreen.setVisibility(View.GONE);
            if (foreground) {
              handler.removeCallbacks(animate);
              handler.post(animate);
            }
          }
          getWindow().setAttributes(attrs);
        });
  }

  @Override
  protected void onResume() {
    super.onResume();
    foreground = true;
    traceAuto("resume");
    DesktopEntryService.update(this, true);
    getWindow().addFlags(WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
    session.settings(wake.isChecked());
    session.resume();
    handler.post(animate);
    refresh();
    if (permissionGrantedAwaitingResume) {
      permissionGrantedAwaitingResume = false;
      autoVoice.opened(SystemClock.elapsedRealtime());
    }
    autoVoice.resumed(SystemClock.elapsedRealtime());
    traceAuto("resumed_gate");
    maybeAutoVoice();
  }

  @Override
  protected void onPause() {
    clearSources();
    if (correctionDialog != null) correctionDialog.close();
    if (reminderPolicyDialog != null) reminderPolicyDialog.dismiss();
    if (reminderEditorDialog != null) reminderEditorDialog.dismiss();
    if (reminderHistoryDialog != null) reminderHistoryDialog.dismiss();
    if (connectionInfoDialog != null) connectionInfoDialog.dismiss();
    if (dotChats != null) dotChats.close();
    if (preferenceMemory != null) preferenceMemory.close();
    if (recognitionLanguage != null) recognitionLanguage.close();
    if (recognitionEnvironment != null) recognitionEnvironment.close();
    if (vocabulary != null) vocabulary.close();
    if (imageQualityDialog != null) imageQualityDialog.dismiss();
    if (savedTasks != null) savedTasks.close();
    if (resultDetails != null) resultDetails.close();
    screen(false);
    foreground = false;
    if (eyes != null) eyes.cancel();
    traceAuto("pause");
    handler.removeCallbacks(autoVoiceTick);
    handler.removeCallbacks(animate);
    session.pause();
    DesktopEntryService.update(this, false);
    getWindow()
        .clearFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
    phase = "idle";
    listeningState.localPhase("idle");
    companion.update(0, 0, "calm", "idle", false, false);
    super.onPause();
  }

  @Override
  protected void onStop() {
    pendingTestCaptureLimitMs = 0;
    pending = false;
    permissionGrantedAwaitingResume = false;
    autoVoice.stopped();
    traceAuto("stop");
    handler.removeCallbacks(autoVoiceTick);
    super.onStop();
  }

  @Override
  protected void onDestroy() {
    if (eyes != null) eyes.destroy();
    super.onDestroy();
  }

  private boolean overlaps(View a, View b) {
    android.graphics.Rect first = new android.graphics.Rect(), second = new android.graphics.Rect();
    return a.getGlobalVisibleRect(first)
        && b.getGlobalVisibleRect(second)
        && android.graphics.Rect.intersects(first, second);
  }

  @Override
  public void dump(String prefix, FileDescriptor fd, PrintWriter writer, String[] args) {
    writer.println("CC8_ASSISTANT_STATUS " + session.snapshot());
    JSONObject ui = new JSONObject();
    Wire.put(ui, "connection_dialog", connectionDialogObservation());
    Wire.put(ui, "listening_state", listeningState.state());
    Wire.put(
        ui,
        "listening_state_visible",
        foreground
            && !screenSleeping
            && "listening".equals(phase)
            && stateText != null
            && stateText.isShown()
            && listeningState.label().equals(stateText.getText().toString()));
    Wire.put(
        ui,
        "ui_phase",
        java.util.Arrays.asList("idle", "listening", "thinking", "speaking", "calibrating", "error")
                .contains(phase)
            ? phase
            : "unknown");
    JSONObject latestTask = tasksData.optJSONObject(0);
    String taskStatus = latestTask == null ? "none" : latestTask.optString("status");
    Wire.put(
        ui,
        "latest_task_status",
        java.util.Arrays.asList("none", "running", "completed", "failed", "cancelled")
                .contains(taskStatus)
            ? taskStatus
            : "unknown");
    Wire.put(ui, "theme", "bumblebee_orbit_reference");
    Wire.put(ui, "camera_open", eyes != null && eyes.cameraOpen());
    Wire.put(ui, "camera_captures", eyes == null ? 0 : eyes.captures());
    Wire.put(ui, "camera_preview_hold_ms", eyes == null ? 0 : eyes.previewHoldMs());
    Wire.put(ui, "camera_preview_elapsed_ms", eyes == null ? 0 : eyes.previewElapsedMs());
    Wire.put(
        ui, "camera_captured_after_preview_ms", eyes == null ? 0 : eyes.capturedAfterPreviewMs());
    Wire.put(ui, "camera_last_error", eyes == null ? "" : eyes.lastError());
    Wire.put(ui, "camera_preview_width", eyes == null ? 0 : eyes.previewWidth());
    Wire.put(ui, "camera_preview_height", eyes == null ? 0 : eyes.previewHeight());
    Wire.put(ui, "camera_quality_fallback", eyes == null ? "" : eyes.qualityFallback());
    Wire.put(ui, "camera_hd_requested", highDefinitionImage);
    Wire.put(ui, "camera_review_requested", reviewCapturedImage);
    Wire.put(ui, "camera_followup_requested", retainCapturedPhoto);
    Wire.put(ui, "screen_sleeping", screenSleeping);
    Wire.put(ui, "orbit_clock_time", companion.clockTime());
    Wire.put(ui, "orbit_clock_timezone", companion.clockTimeZone());
    Wire.put(ui, "orbit_clock_style", "yellow_radial_markers");
    Wire.put(ui, "progress_style", "slanted_energy_cells");
    Wire.put(ui, "seconds_style", "short_continuous_arc");
    Wire.put(ui, "waveform_separate", true);
    Wire.put(ui, "waveform_style", "particle_field");
    Wire.put(ui, "karaoke_progress", captionProgress.getProgress());
    Wire.put(ui, "brain_provider", brainProvider);
    Wire.put(ui, "asr_engine", recognitionEngine);
    Wire.put(ui, "dot_result_available", !dotResult.isEmpty());
    Wire.put(ui, "wake_width", wakeButton.getWidth());
    Wire.put(ui, "wake_height", wakeButton.getHeight());
    Wire.put(ui, "wake_checked", wake.isChecked());
    Wire.put(ui, "auto_voice_open_requests", autoVoiceOpens);
    Wire.put(ui, "auto_voice_start_attempts", autoVoiceStarts);
    Wire.put(ui, "auto_voice_requested", lastOpenWantedVoice);
    Wire.put(ui, "auto_voice_pending", autoVoice.waiting(SystemClock.elapsedRealtime()));
    synchronized (autoVoiceTrace) {
      Wire.put(ui, "auto_voice_trace", new JSONArray(autoVoiceTrace));
    }
    Wire.put(ui, "caption_view_height", captionScroll.getHeight());
    Wire.put(ui, "caption_scroll_y", captionScroll.getScrollY());
    Wire.put(ui, "caption_full_visible_lines", captionScroll.fullVisibleLines(captionText));
    Wire.put(
        ui, "caption_window_height", captionScroll.getHeight() - captionScroll.getPaddingBottom());
    Wire.put(ui, "caption_current_line", lastCaptionLine);
    Wire.put(
        ui,
        "caption_line_visible",
        lastCaptionLine < 0 || captionScroll.lineVisible(captionText, lastCaptionLine));
    Wire.put(ui, "caption_progress_overlap", overlaps(captionScroll, captionProgress));
    Wire.put(ui, "caption_clip_bounds", captionScroll.getClipBounds() != null);
    Wire.put(ui, "source_count", sourceCount());
    Wire.put(ui, "workspace_revision", workspaceRevision);
    Wire.put(ui, "task_result_unconfirmed", taskResultUnconfirmed);
    Wire.put(ui, "task_count", tasksData.length());
    Wire.put(ui, "reminder_count", remindersData.length());
    AlertDialog policyDialog = reminderPolicyDialog;
    boolean policyVisible = policyDialog != null && policyDialog.isShowing();
    boolean policyButtonsVisible = false, policyEditorFocused = false;
    int policyVisibleBottom = 0;
    if (policyVisible && policyDialog.getWindow() != null) {
      Window window = policyDialog.getWindow();
      android.graphics.Rect visible = new android.graphics.Rect();
      window.getDecorView().getWindowVisibleDisplayFrame(visible);
      policyVisibleBottom = visible.bottom;
      policyEditorFocused = window.getCurrentFocus() instanceof EditText;
      policyButtonsVisible = true;
      for (int which :
          new int[] {
            AlertDialog.BUTTON_POSITIVE, AlertDialog.BUTTON_NEGATIVE, AlertDialog.BUTTON_NEUTRAL
          }) {
        Button button = policyDialog.getButton(which);
        android.graphics.Rect bounds = new android.graphics.Rect();
        policyButtonsVisible &=
            button != null
                && button.isShown()
                && button.getGlobalVisibleRect(bounds)
                && visible.contains(bounds);
      }
    }
    // Geometry/focus only: never expose reminder text or edited field values.
    Wire.put(ui, "reminder_policy_dialog_visible", policyVisible);
    Wire.put(ui, "reminder_policy_buttons_visible", policyButtonsVisible);
    Wire.put(ui, "reminder_policy_editor_focused", policyEditorFocused);
    Wire.put(ui, "reminder_policy_visible_bottom", policyVisibleBottom);
    Wire.put(ui, "halo_status_overlap", overlaps(companion, stateText));
    Wire.put(
        ui,
        "caption_text_size_sp",
        captionText.getTextSize() / getResources().getDisplayMetrics().scaledDensity);
    Wire.put(
        ui,
        "control_text_size_sp",
        start.getTextSize() / getResources().getDisplayMetrics().scaledDensity);
    Wire.put(
        ui,
        "task_text_size_sp",
        dotText.getTextSize() / getResources().getDisplayMetrics().scaledDensity);
    writer.println("HUD_UI_STATUS " + ui);
  }
}
