package com.happyhome.cc8diagnostics;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.*;
import android.util.Base64;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.*;
import java.util.concurrent.*;

public final class MainActivity extends Activity implements AudioProbe.Listener {
  private static final int MIC_PERMISSION = 41, EXPORT_DOCUMENT = 42;
  private static final int INK = 0xff243b32,
      MUTED = 0xff5d6f65,
      PAPER = 0xfff8faf5,
      MINT = 0xffd9efe4;
  private final Handler ui = new Handler(Looper.getMainLooper());
  private final ExecutorService reader = Executors.newSingleThreadExecutor();
  private final ArrayList<JSONObject> tests = new ArrayList<>();
  private final ArrayList<Button> audioButtons = new ArrayList<>();
  private final ArrayList<View> audioSettings = new ArrayList<>();
  private AudioProbe probe;
  private JSONObject report;
  private LinearLayout pageHost;
  private TextView globalStatus, levelText, resultText;
  private ProgressBar levelBar, timeBar;
  private Button stopButton, replayButton, analysisButton;
  private String analysisToken;
  private int analysisSourceIndex;
  private boolean analysisPlayback;
  private RamPcmLease.Profile analysisProfile;
  private boolean analysisUsbConnected, analysisUsbConfigured, usbReceiverRegistered;
  private int analysisUsbLosses;
  private final BroadcastReceiver usbReceiver =
      new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
          if (destroyed) return;
          boolean wasReady = analysisUsbReady();
          analysisUsbConnected = intent.getBooleanExtra("connected", false);
          analysisUsbConfigured = intent.getBooleanExtra("configured", false);
          if (wasReady && !analysisUsbReady() && analysisUsbLosses < Integer.MAX_VALUE)
            analysisUsbLosses++;
          String state = probe.analysisStatus().optString("state");
          if (!analysisUsbReady()
              && (analysisToken != null || "pending".equals(state) || "ready".equals(state))) {
            analysisToken = null;
            probe.cancel();
            status = "USB 已断开，临时分析已清除";
          }
          updateStatus();
        }
      };
  private int page = 0, sourceIndex = 2, rateIndex = 0;
  private boolean requestAec = true,
      communicationMode = false,
      channelReference = false,
      foreground = false,
      destroyed = false,
      permissionApprovedPending = false;
  private Runnable permissionAction;
  private File pendingExport;
  private String status = "正在扫描设备信息…";
  private double lastLevel = -120;
  private int elapsed = 0, total = 1, scanGeneration = 0;
  private static final int[] SOURCES =
      Build.VERSION.SDK_INT >= 24
          ? new int[] {
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            MediaRecorder.AudioSource.UNPROCESSED
          }
          : new int[] {
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            MediaRecorder.AudioSource.VOICE_COMMUNICATION
          };
  private static final String[] SOURCE_NAMES =
      Build.VERSION.SDK_INT >= 24
          ? new String[] {
            "MIC（普通麦克风）", "VOICE_RECOGNITION（语音识别输入）",
            "VOICE_COMMUNICATION（通信输入，建议对比）", "UNPROCESSED（原始输入，需实测支持）"
          }
          : new String[] {
            "MIC（普通麦克风）", "VOICE_RECOGNITION（语音识别输入）", "VOICE_COMMUNICATION（通信输入，建议对比）"
          };
  private static final int[] RATES = {16000, 48000, 44100};

  @Override
  public void onCreate(Bundle saved) {
    super.onCreate(saved);
    probe = new AudioProbe(this, this);
    buildShell();
    try {
      IntentFilter filter = new IntentFilter("android.hardware.usb.action.USB_STATE");
      Intent sticky =
          Build.VERSION.SDK_INT >= 33
              ? registerReceiver(usbReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
              : registerReceiver(usbReceiver, filter);
      usbReceiverRegistered = true;
      analysisUsbConnected = sticky != null && sticky.getBooleanExtra("connected", false);
      analysisUsbConfigured = sticky != null && sticky.getBooleanExtra("configured", false);
    } catch (RuntimeException unavailable) {
      // Offline diagnostics still work; USB-bound analysis stays unavailable.
      analysisUsbConnected = analysisUsbConfigured = false;
    }
    scan();
  }

  // A cable can remain connected while the host drops the configured USB data link.
  private boolean analysisUsbReady() {
    return analysisUsbConnected && analysisUsbConfigured;
  }

  private int dp(float value) {
    return Math.round(value * getResources().getDisplayMetrics().density);
  }

  private GradientDrawable surface(int color, int stroke) {
    GradientDrawable d = new GradientDrawable();
    d.setColor(color);
    d.setCornerRadius(dp(12));
    if (stroke != 0) d.setStroke(dp(1), stroke);
    return d;
  }

  private TextView text(String value, int size, int color) {
    TextView v = new TextView(this);
    v.setText(value);
    v.setTextSize(size);
    v.setTextColor(color);
    v.setLineSpacing(dp(4), 1);
    return v;
  }

  private LinearLayout column() {
    LinearLayout v = new LinearLayout(this);
    v.setOrientation(LinearLayout.VERTICAL);
    return v;
  }

  private void space(LinearLayout parent, int height) {
    View v = new View(this);
    parent.addView(v, new LinearLayout.LayoutParams(1, dp(height)));
  }

  private void heading(LinearLayout parent, String value) {
    TextView v = text(value, 21, INK);
    v.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
    v.setPadding(0, dp(15), 0, dp(10));
    parent.addView(v);
  }

  private void note(LinearLayout parent, String value) {
    TextView v = text(value, 14, MUTED);
    v.setPadding(0, dp(4), 0, dp(10));
    parent.addView(v);
  }

  private Button button(String label, Runnable action) {
    Button b = new Button(this);
    b.setText(label);
    b.setTextSize(15);
    b.setAllCaps(false);
    b.setTextColor(INK);
    b.setMinHeight(dp(50));
    b.setPadding(dp(12), dp(9), dp(12), dp(9));
    b.setBackground(surface(MINT, 0));
    b.setOnClickListener(v -> action.run());
    return b;
  }

  private void fullButton(LinearLayout parent, Button b) {
    LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1, -2);
    p.setMargins(0, dp(5), 0, dp(5));
    parent.addView(b, p);
  }

  private void buildShell() {
    LinearLayout root = column();
    root.setBackgroundColor(PAPER);
    root.setPadding(dp(20), dp(12), dp(20), dp(8));
    LinearLayout top = new LinearLayout(this);
    top.setGravity(Gravity.CENTER_VERTICAL);
    TextView title = text("CC8 能力检测", 25, INK);
    title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
    top.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
    stopButton =
        button(
            "停止测试",
            () -> {
              analysisToken = null;
              boolean wasRunning = probe.isRunning();
              probe.cancel();
              status = wasRunning ? "正在停止并释放音频设备…" : "测试已停止，临时分析已清除";
              updateStatus();
            });
    stopButton.setEnabled(false);
    top.addView(stopButton, new LinearLayout.LayoutParams(dp(112), -2));
    root.addView(top);
    Object version =
        DeviceReport.safe(
            () -> getPackageManager().getPackageInfo(getPackageName(), 0).versionName);
    note(root, "离线检测 · 不修改固件 · " + version + " · 兼容 Android 5.0 及以上");
    LinearLayout navigation = new LinearLayout(this);
    String[] names = {"设备信息", "音频测试", "导出报告"};
    for (int i = 0; i < names.length; i++) {
      final int index = i;
      Button tab =
          button(
              names[i],
              () -> {
                page = index;
                renderPage();
              });
      LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, -2, 1);
      lp.setMargins(dp(i == 0 ? 0 : 5), 0, 0, 0);
      navigation.addView(tab, lp);
    }
    root.addView(navigation);
    globalStatus = text(status, 14, MUTED);
    globalStatus.setPadding(0, dp(10), 0, dp(8));
    root.addView(globalStatus);
    ScrollView scroll = new ScrollView(this);
    scroll.setFillViewport(true);
    pageHost = column();
    scroll.addView(pageHost, new ScrollView.LayoutParams(-1, -2));
    root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
    setContentView(root);
    renderPage();
  }

  private void renderPage() {
    pageHost.removeAllViews();
    audioButtons.clear();
    audioSettings.clear();
    levelText = null;
    levelBar = null;
    timeBar = null;
    resultText = null;
    replayButton = null;
    analysisButton = null;
    if (page == 0) renderDevice();
    else if (page == 1) renderAudio();
    else renderExport();
    updateStatus();
  }

  private void scan() {
    final int generation = ++scanGeneration;
    status = "正在读取公开属性与系统接口…";
    updateStatus();
    reader.execute(
        () -> {
          JSONObject snapshot;
          try {
            snapshot = DeviceReport.collect(this);
          } catch (Exception | LinkageError e) {
            snapshot =
                DeviceReport.object(
                    "schema_version",
                    "1.0",
                    "captured_at",
                    DeviceReport.timestamp(),
                    "scan_error",
                    e.getClass().getSimpleName() + ": " + e.getMessage());
          }
          final JSONObject ready = snapshot;
          ui.post(
              () -> {
                if (destroyed || generation != scanGeneration) return;
                report = ready;
                status = ready.has("scan_error") ? "部分检测失败，可导出错误信息" : "设备信息已更新；音频能力需要点击实测";
                if (page == 0) renderPage();
                else updateStatus();
              });
        });
  }

  private String display(Object value) {
    if (value == null || value == JSONObject.NULL) return "未知 / 系统未提供";
    if (value instanceof Boolean) return (Boolean) value ? "是" : "否";
    if (value instanceof JSONObject || value instanceof JSONArray) {
      try {
        return value instanceof JSONObject
            ? ((JSONObject) value).toString(2)
            : ((JSONArray) value).toString(2);
      } catch (JSONException ignored) {
      }
    }
    return String.valueOf(value);
  }

  private void row(LinearLayout parent, String key, Object value) {
    LinearLayout group = column();
    group.setPadding(dp(12), dp(9), dp(12), dp(9));
    TextView label = text(key, 13, MUTED);
    group.addView(label);
    TextView content = text(display(value), 16, INK);
    content.setTextIsSelectable(true);
    group.addView(content);
    if (key.contains("RAM") || key.contains("分区"))
      note(group, "数值单位 bytes；1 GiB = 1,073,741,824 bytes。数据分区容量不是整颗存储芯片容量。");
    parent.addView(group);
    View divider = new View(this);
    divider.setBackgroundColor(0xffe3e9df);
    parent.addView(divider, new LinearLayout.LayoutParams(-1, dp(1)));
  }

  private void renderDevice() {
    fullButton(pageHost, button("重新扫描设备信息", this::scan));
    if (report == null) {
      note(pageHost, "正在扫描。无需麦克风权限即可读取基础信息。");
      return;
    }
    if (report.has("scan_error")) {
      row(pageHost, "扫描错误", report.opt("scan_error"));
      return;
    }
    JSONObject sections = report.optJSONObject("device");
    if (sections != null) {
      Iterator<String> names = sections.keys();
      while (names.hasNext()) {
        String name = names.next();
        heading(pageHost, name);
        Object data = sections.opt(name);
        if (data instanceof JSONObject) {
          JSONObject section = (JSONObject) data;
          Iterator<String> keys = section.keys();
          while (keys.hasNext()) {
            String key = keys.next();
            row(pageHost, key, section.opt(key));
          }
        } else row(pageHost, "检测结果", data);
      }
    }
    heading(pageHost, "如何理解结果");
    note(
        pageHost,
        "未知/null/-1（接口约定的未知值）不等于不支持。构建时间、安全补丁和 OTA 推送时间不同。型号与 ABI 以本机实际读取为准。麦克风数量不能由音频端口数量推断。");
  }

  private interface Selection {
    void changed(int index);
  }

  private Spinner spinner(
      LinearLayout parent, String label, String[] options, int selected, Selection onChange) {
    TextView title = text(label, 15, INK);
    parent.addView(title);
    Spinner spinner = new Spinner(this);
    ArrayAdapter<String> adapter =
        new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, options);
    adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
    spinner.setAdapter(adapter);
    spinner.setSelection(selected);
    parent.addView(spinner, new LinearLayout.LayoutParams(-1, dp(52)));
    spinner.setOnItemSelectedListener(
        new AdapterView.OnItemSelectedListener() {
          public void onItemSelected(AdapterView<?> p, View v, int position, long id) {
            onChange.changed(position);
          }

          public void onNothingSelected(AdapterView<?> p) {}
        });
    audioSettings.add(spinner);
    return spinner;
  }

  private void pair(LinearLayout parent, Button first, Button second) {
    LinearLayout line = new LinearLayout(this);
    LinearLayout.LayoutParams a = new LinearLayout.LayoutParams(0, dp(58), 1);
    a.setMargins(0, dp(5), dp(5), dp(5));
    line.addView(first, a);
    LinearLayout.LayoutParams b = new LinearLayout.LayoutParams(0, dp(58), 1);
    b.setMargins(dp(5), dp(5), 0, dp(5));
    line.addView(second, b);
    parent.addView(line);
    audioButtons.add(first);
    audioButtons.add(second);
  }

  private void renderAudio() {
    heading(pageHost, "先听，再录，再同时录放");
    note(pageHost, "测试仅在前台、由你点击后执行。播放使用当前媒体音量，应用内参考音增益为 35%。请先调低设备音量。录音保存在本应用本地缓存，未自动上传。");
    spinner(pageHost, "录音输入源", SOURCE_NAMES, sourceIndex, index -> sourceIndex = index);
    spinner(
        pageHost,
        "请求采样率 / PCM16（录音回放为单声道）",
        new String[] {"16000 Hz（建议先测）", "48000 Hz", "44100 Hz"},
        rateIndex,
        index -> rateIndex = index);
    CheckBox channelPlayback = new CheckBox(this);
    channelPlayback.setText("多声道统计时播放本机参考音");
    channelPlayback.setTextColor(INK);
    channelPlayback.setChecked(channelReference);
    channelPlayback.setOnCheckedChangeListener((button, checked) -> channelReference = checked);
    pageHost.addView(channelPlayback);
    audioSettings.add(channelPlayback);
    pair(
        pageHost,
        button("双声道统计 6 秒", () -> ensureMicrophone(() -> startChannels(2))),
        button("四声道统计 6 秒", () -> ensureMicrophone(() -> startChannels(4))));
    note(pageHost, "多声道检测沿用上方输入源和采样率，仅保存各通道电平、相关性与重复比例。");
    analysisButton = button("参考通道分析 6 秒", () -> ensureMicrophone(this::startAnalysis));
    fullButton(pageHost, analysisButton);
    note(pageHost, "Mac 发起分析后可用：16 kHz 单声道或 48 kHz 四路，采样仅临时保留在内存中，停止或离开前台即清除。");
    CheckBox aec = new CheckBox(this);
    aec.setText(R.string.aec_option);
    aec.setTextColor(INK);
    aec.setChecked(requestAec);
    aec.setOnCheckedChangeListener((b, c) -> requestAec = c);
    pageHost.addView(aec);
    audioSettings.add(aec);
    CheckBox comm = new CheckBox(this);
    comm.setText("临时使用通信音频模式（结束后恢复）");
    comm.setTextColor(INK);
    comm.setChecked(communicationMode);
    comm.setOnCheckedChangeListener((b, c) -> communicationMode = c);
    pageHost.addView(comm);
    audioSettings.add(comm);
    note(
        pageHost, "同时录放共 10 秒：前 3 秒保持安静，之后一边听参考音，一边说“一二三，测试”。输入源、AEC 开关和通信模式请分别对比；本工具不把录音直接回送扬声器。");
    pair(
        pageHost,
        button("1. 播放参考音 6 秒", () -> startTest("playback")),
        button("2. 麦克风录音 6 秒", () -> ensureMicrophone(() -> startTest("capture"))));
    replayButton =
        button(
            "回放最近录音",
            () -> {
              analysisToken = null;
              if (probe.replay()) {
                status = "准备回放最近录音…";
                updateStatus();
              } else toast("请先完成一次录音测试");
            });
    pair(
        pageHost,
        button("3. 同时录放 10 秒", () -> ensureMicrophone(() -> startTest("duplex"))),
        replayButton);
    heading(pageHost, "实时状态");
    levelText = text("尚未开始", 16, INK);
    pageHost.addView(levelText);
    levelBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
    levelBar.setMax(100);
    pageHost.addView(levelBar, new LinearLayout.LayoutParams(-1, dp(16)));
    timeBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
    timeBar.setMax(1000);
    pageHost.addView(timeBar, new LinearLayout.LayoutParams(-1, dp(10)));
    note(pageHost, "音量条只表示采样电平，不是人声识别。AEC 接口存在、创建或启用成功，都不代表实际消回声通过。");
    resultText = text(testSummary(), 14, INK);
    resultText.setTypeface(Typeface.MONOSPACE);
    resultText.setTextIsSelectable(true);
    pageHost.addView(resultText);
  }

  private void startTest(String mode) {
    analysisToken = null;
    if (!foreground) {
      toast("请在应用前台开始测试");
      return;
    }
    if (probe.start(mode, SOURCES[sourceIndex], RATES[rateIndex], requestAec, communicationMode)) {
      status = "正在准备音频设备…";
      elapsed = 0;
      lastLevel = -120;
      updateStatus();
    } else toast("请先停止当前测试");
  }

  private void startChannels(int channels) {
    analysisToken = null;
    if (!foreground) {
      toast("请在应用前台开始测试");
      return;
    }
    if (probe.startChannels(SOURCES[sourceIndex], RATES[rateIndex], channels, channelReference)) {
      status = "正在准备多声道检测…";
      elapsed = 0;
      lastLevel = -120;
      updateStatus();
    } else toast("请先停止当前测试");
  }

  private void startAnalysis() {
    String token = analysisToken;
    analysisToken = null;
    if (!foreground
        || !analysisUsbReady()
        || token == null
        || sourceIndex > 2
        || analysisProfile == null
        || RATES[rateIndex] != analysisProfile.rate
        || sourceIndex != analysisSourceIndex
        || channelReference != analysisPlayback) {
      toast("请先在 Mac 准备参考通道分析");
      updateStatus();
      return;
    }
    if (probe.startReferenceAnalysis(
        SOURCES[sourceIndex], channelReference, token, analysisProfile)) {
      status = "正在准备参考通道分析，采样不写文件…";
      elapsed = 0;
      lastLevel = -120;
      ui.postDelayed(
          () -> {
            probe.expireAnalysis();
            if ("expired".equals(probe.analysisStatus().optString("state"))) status = "临时分析已过期并清除";
            updateStatus();
          },
          RamPcmLease.LIFETIME_MS);
      updateStatus();
    } else toast("请先停止当前测试");
  }

  private void ensureMicrophone(Runnable action) {
    if (probe.isRunning()) {
      toast("请先停止当前测试");
      return;
    }
    if (Build.VERSION.SDK_INT < 23
        || checkSelfPermission(Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED) {
      action.run();
      return;
    }
    permissionAction = action;
    requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, MIC_PERMISSION);
  }

  @Override
  public void onRequestPermissionsResult(int code, String[] permissions, int[] grants) {
    if (Build.VERSION.SDK_INT >= 23) super.onRequestPermissionsResult(code, permissions, grants);
    if (code != MIC_PERMISSION) return;
    if (grants.length > 0 && grants[0] == PackageManager.PERMISSION_GRANTED) {
      updatePermission();
      if (foreground) {
        Runnable action = permissionAction;
        permissionAction = null;
        if (action != null) action.run();
      } else permissionApprovedPending = true;
    } else {
      permissionAction = null;
      permissionApprovedPending = false;
      status = "麦克风未授权；设备信息与播放测试仍可用";
      updateStatus();
      toast("未执行录音。可在系统应用权限中授权后重试。");
    }
  }

  private void updatePermission() {
    if (report == null) return;
    JSONObject sections = report.optJSONObject("device");
    if (sections == null) return;
    JSONObject p = sections.optJSONObject("权限与调试状态");
    if (p != null)
      DeviceReport.put(
          p,
          "麦克风权限",
          checkCallingOrSelfPermission(Manifest.permission.RECORD_AUDIO)
              == PackageManager.PERMISSION_GRANTED);
  }

  @Override
  public void progress(String message, double dbfs, int elapsedMs, int totalMs) {
    ui.post(
        () -> {
          if (destroyed) return;
          status = message;
          lastLevel = dbfs;
          elapsed = elapsedMs;
          total = Math.max(1, totalMs);
          updateStatus();
        });
  }

  @Override
  public void finished(JSONObject result, File capture) {
    ui.post(
        () -> {
          if (destroyed) return;
          tests.add(result);
          while (tests.size() > 20) tests.remove(0);
          String outcome = result.optString("status");
          // Keep the current test's UI if this completion was delivered after a new start.
          if (!probe.isRunning())
            status =
                "completed".equals(outcome)
                    ? (result.optBoolean("ram_analysis_requested")
                        ? (result.optBoolean("ram_analysis_available")
                                && "ready".equals(probe.analysisStatus().optString("state"))
                            ? "采样已关闭，临时分析就绪，等待 Mac 读取"
                            : "采样已关闭，临时分析已清除或过期")
                        : "channel_capture".equals(result.optString("type"))
                            ? "多声道统计完成，可导出数字报告"
                            : "测试完成，请回放并导出报告")
                    : "cancelled".equals(outcome)
                        ? "测试已停止，音频设备已释放"
                        : "测试失败：" + result.optString("error", "未知错误");
          updatePermission();
          saveSessionSnapshot();
          if (resultText != null) resultText.setText(testSummary());
          updateStatus();
        });
  }

  private void updateStatus() {
    if (globalStatus == null) return;
    boolean busy = probe != null && probe.isRunning();
    globalStatus.setText(status);
    stopButton.setEnabled(
        busy || analysisToken != null || "ready".equals(probe.analysisStatus().optString("state")));
    if (foreground && (busy || page == 1 || page == 2))
      getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    for (Button button : audioButtons) button.setEnabled(!busy);
    if (analysisButton != null)
      analysisButton.setEnabled(!busy && foreground && analysisUsbReady() && analysisToken != null);
    for (View view : audioSettings) view.setEnabled(!busy);
    if (replayButton != null) replayButton.setEnabled(!busy && probe.getLastCapture() != null);
    if (levelText != null)
      levelText.setText(
          getString(R.string.audio_progress, status, lastLevel, elapsed / 1000.0, total / 1000.0));
    if (levelBar != null)
      levelBar.setProgress((int) Math.max(0, Math.min(100, (lastLevel + 90) / 90 * 100)));
    if (timeBar != null)
      timeBar.setProgress((int) Math.min(1000, elapsed * 1000L / Math.max(1, total)));
  }

  private String testSummary() {
    if (tests.isEmpty()) return "还没有音频实测记录。\n基础设备信息中的“可用”不是实测结论。";
    JSONObject last = tests.get(tests.size() - 1);
    StringBuilder s =
        new StringBuilder(
            "已记录 "
                + tests.size()
                + " 次测试\n最近："
                + last.optString("type")
                + " · "
                + last.optString("status")
                + "\n");
    if (last.has("error")) s.append("错误：").append(last.optString("error")).append('\n');
    JSONObject capture = last.optJSONObject("capture");
    if (capture != null)
      s.append(
          String.format(
              Locale.CHINA,
              "采样 %d 帧 · RMS %.1f dBFS\n零值 %.2f%% · 削波 %.2f%%\n",
              capture.optLong("frames"),
              capture.optDouble("rms_dbfs"),
              capture.optDouble("zero_ratio") * 100,
              capture.optDouble("clipped_ratio") * 100));
    JSONObject aec = last.optJSONObject("aec");
    if (aec != null)
      s.append("AEC 创建：")
          .append(display(aec.opt("created")))
          .append("；实际启用：")
          .append(display(aec.opt("actual_enabled")))
          .append('\n');
    s.append(last.optString("signal_note", "请确认能否听到声音，或查看导出报告中的详细计数。"));
    return s.toString();
  }

  private JSONObject snapshot() {
    JSONObject copy;
    try {
      copy = report == null ? new JSONObject() : new JSONObject(report.toString());
    } catch (JSONException e) {
      copy = DeviceReport.object("snapshot_error", e.getMessage());
    }
    DeviceReport.put(copy, "exported_at", DeviceReport.timestamp());
    JSONArray results = new JSONArray();
    for (JSONObject result : tests) results.put(result);
    DeviceReport.put(copy, "audio_tests", results);
    if (probe != null) DeviceReport.put(copy, "ram_analysis", probe.analysisStatus());
    if (probe != null) DeviceReport.put(copy, "audio_probe_running", probe.isRunning());
    DeviceReport.put(copy, "ram_analysis_usb_connected", analysisUsbConnected);
    DeviceReport.put(copy, "ram_analysis_usb_configured", analysisUsbConfigured);
    DeviceReport.put(copy, "ram_analysis_usb_ready", analysisUsbReady());
    DeviceReport.put(copy, "ram_analysis_usb_losses", analysisUsbLosses);
    return copy;
  }

  private String reportString() {
    try {
      return snapshot().toString(2);
    } catch (JSONException e) {
      return snapshot().toString();
    }
  }

  private File reportDirectory() throws IOException {
    File base = getExternalFilesDir(Environment.DIRECTORY_DOCUMENTS);
    if (base == null) base = getFilesDir();
    File folder = new File(base, "reports");
    if (!folder.exists() && !folder.mkdirs()) throw new IOException("无法创建报告目录");
    return folder;
  }

  private File saveReportFile(boolean latest) throws IOException {
    File file =
        new File(
            reportDirectory(),
            latest
                ? "cc8-session-latest.json"
                : "cc8-report-"
                    + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date())
                    + ".json");
    try (FileOutputStream out = new FileOutputStream(file)) {
      out.write(reportString().getBytes(StandardCharsets.UTF_8));
    }
    return file;
  }

  private File saveCaptureFile() throws IOException {
    File source = probe.getLastCapture();
    if (source == null || !source.exists()) throw new IOException("没有已完成的录音");
    File file = new File(reportDirectory(), source.getName());
    try (InputStream in = new FileInputStream(source);
        OutputStream out = new FileOutputStream(file)) {
      byte[] buffer = new byte[8192];
      int count;
      while ((count = in.read(buffer)) != -1) out.write(buffer, 0, count);
    }
    return file;
  }

  private void saveSessionSnapshot() {
    try {
      saveReportFile(true);
    } catch (IOException | RuntimeException error) {
      android.util.Log.w("CC8Diagnostics", "Cannot save local test metrics", error);
    }
  }

  @Override
  public void dump(String prefix, FileDescriptor descriptor, PrintWriter writer, String[] args) {
    // Activity dumps are gated by Android's DUMP permission. Expose existing
    // Prepare visible controls or read bounded results; never a recording command.
    String operation = args != null && args.length > 0 ? args[0] : "snapshot";
    try {
      if ("snapshot".equals(operation)) {
        writer.println("CC8_REPORT_BEGIN");
        writer.println(reportString());
        writer.println("CC8_REPORT_END");
      } else if ("export-json".equals(operation) || "export-wav".equals(operation)) {
        if (probe.isRunning()) {
          writer.println("ERROR: wait for the foreground test to finish");
          return;
        }
        writer.println(
            "CC8_EXPORTED_FILE "
                + ("export-json".equals(operation) ? saveReportFile(false) : saveCaptureFile())
                    .getAbsolutePath());
      } else if ("playback-reference".equals(operation)) {
        if (!foreground || probe.isRunning()) {
          writer.println("ERROR: foreground idle activity required");
          return;
        }
        startTest("playback");
        writer.println("CC8_REFERENCE_PLAYBACK_STARTED");
      } else if ("prepare-audio-page".equals(operation)) {
        if (!foreground || probe.isRunning()) {
          writer.println("ERROR: foreground idle activity required");
          return;
        }
        if (args.length != 5 && args.length != 6)
          throw new IllegalArgumentException(
              "Expected source, rate, AEC, communication and optional channel reference flag");
        if (args.length == 6 && !"true".equals(args[5]) && !"false".equals(args[5]))
          throw new IllegalArgumentException("Expected a boolean channel reference flag");
        int source = Integer.parseInt(args[1]), rate = Integer.parseInt(args[2]);
        if (source < 0 || source >= SOURCES.length || rate < 0 || rate >= RATES.length)
          throw new IllegalArgumentException("Unsupported source/rate index");
        sourceIndex = source;
        rateIndex = rate;
        requestAec = Boolean.parseBoolean(args[3]);
        communicationMode = Boolean.parseBoolean(args[4]);
        channelReference = args.length == 6 && Boolean.parseBoolean(args[5]);
        analysisToken = null;
        probe.clearAnalysis();
        page = 1;
        renderPage();
        writer.println("CC8_AUDIO_PAGE_READY; no recording started");
      } else if ("prepare-reference-analysis".equals(operation)) {
        if (!foreground || !analysisUsbReady() || probe.isRunning()) {
          writer.println("ERROR: foreground idle activity with USB required");
          return;
        }
        if (args.length != 4
            || (!"mono".equals(args[3]) && !"quad".equals(args[3]))
            || (!"true".equals(args[2]) && !"false".equals(args[2])))
          throw new IllegalArgumentException(
              "Expected source index, playback flag and mono/quad profile");
        int source = Integer.parseInt(args[1]);
        if (source < 0 || source > 2)
          throw new IllegalArgumentException("Unsupported analysis source");
        sourceIndex = source;
        analysisProfile =
            "mono".equals(args[3]) ? RamPcmLease.Profile.MONO : RamPcmLease.Profile.QUAD;
        rateIndex = analysisProfile == RamPcmLease.Profile.MONO ? 0 : 1;
        requestAec = communicationMode = false;
        channelReference = Boolean.parseBoolean(args[2]);
        probe.clearAnalysis();
        analysisToken = UUID.randomUUID().toString().replace("-", "");
        analysisSourceIndex = source;
        analysisPlayback = channelReference;
        final String prepared = analysisToken;
        ui.postDelayed(
            () -> {
              if (prepared.equals(analysisToken)) {
                analysisToken = null;
                status = "分析准备已过期，请在 Mac 重新发起";
                updateStatus();
              }
            },
            30000);
        page = 1;
        renderPage();
        status = "分析已准备，请点“参考通道分析 6 秒”";
        updateStatus();
        writer.println("CC8_ANALYSIS_PAGE_READY " + prepared + "; no recording started");
      } else if ("clear-reference-analysis".equals(operation)) {
        if (args.length != 2 || !RamPcmLease.validToken(args[1]))
          throw new IllegalArgumentException("Expected analysis token");
        boolean discarded = probe.discardAnalysis(args[1]);
        if (args[1].equals(analysisToken)) {
          analysisToken = null;
          discarded = true;
        }
        if (discarded) status = "临时分析已清除";
        updateStatus();
        writer.println("CC8_ANALYSIS_CLEARED");
      } else if ("read-reference-analysis".equals(operation)) {
        if (!foreground
            || !analysisUsbReady()
            || probe.isRunning()
            || args.length != 3
            || !RamPcmLease.validToken(args[1])
            || !args[2].matches("[0-9]{1,7}")) {
          writer.println("ERROR: foreground completed analysis required");
          return;
        }
        RamPcmLease.Chunk chunk = probe.readAnalysis(args[1], Integer.parseInt(args[2]));
        if (chunk == null) {
          String state = probe.analysisStatus().optString("state");
          if ("invalid_cursor".equals(state)
              || "expired".equals(state)
              || "consumed".equals(state)) {
            status = "临时分析不可读取，采样已清除";
            updateStatus();
          }
          writer.println("CC8_ANALYSIS_UNAVAILABLE");
          return;
        }
        try {
          writer.println(
              "CC8_ANALYSIS_CHUNK "
                  + DeviceReport.object(
                      "offset_bytes",
                      chunk.offset,
                      "total_bytes",
                      chunk.totalBytes,
                      "chunk_bytes",
                      chunk.bytes.length,
                      "sample_rate",
                      chunk.sampleRate,
                      "channels",
                      chunk.channels,
                      "source",
                      chunk.source,
                      "sha256",
                      chunk.sha256()));
          writer.println("CC8_ANALYSIS_PCM " + Base64.encodeToString(chunk.bytes, Base64.NO_WRAP));
          if (writer.checkError()) {
            probe.clearAnalysis();
            status = "分析传输未确认，临时采样已清除";
          } else if (chunk.offset + chunk.bytes.length == chunk.totalBytes) status = "临时采样已读出并清除";
          updateStatus();
        } catch (Exception error) {
          probe.clearAnalysis();
          throw error;
        } finally {
          Arrays.fill(chunk.bytes, (byte) 0);
        }
      } else writer.println("ERROR: unknown diagnostic operation");
    } catch (Exception error) {
      writer.println("ERROR: " + error.getClass().getSimpleName() + ": " + error.getMessage());
    }
  }

  private void renderExport() {
    heading(pageHost, "把实机结果带回分析");
    note(pageHost, "导出包含型号、固件、ABI、内存、屏幕、音频接口和最多 20 条测试记录。JSON 不包含录音原始内容；录音仅在你单独选择导出 WAV 时写入目标位置。");
    fullButton(pageHost, button("保存 JSON 检测报告", () -> exportReport(false)));
    fullButton(pageHost, button("复制 JSON 报告", this::copyReport));
    fullButton(pageHost, button("导出最近录音 WAV", () -> exportReport(true)));
    fullButton(
        pageHost,
        button(
            "删除本地录音缓存",
            () -> {
              if (probe.isRunning()) {
                toast("请先停止音频测试");
                return;
              }
              analysisToken = null;
              probe.clearAnalysis();
              File[] files =
                  getCacheDir()
                      .listFiles(
                          (dir, name) -> name.startsWith("cc8-capture-") && name.endsWith(".wav"));
              int removed = 0;
              if (files != null) for (File f : files) if (f.delete()) removed++;
              toast("已删除 " + removed + " 个本地录音缓存");
            }));
    note(pageHost, "若 CC8 没有系统文件选择器，报告仍会保存到应用专用目录，并显示路径。安装检测器不等于解锁系统；本工具不会申请 Root、刷机或停用原厂服务。");
    heading(pageHost, "最近测试摘要");
    TextView summary = text(testSummary(), 15, INK);
    summary.setTextIsSelectable(true);
    pageHost.addView(summary);
    heading(pageHost, "JSON 预览");
    TextView json = text(reportString(), 12, INK);
    json.setTypeface(Typeface.MONOSPACE);
    json.setTextIsSelectable(true);
    pageHost.addView(json);
  }

  private void copyReport() {
    if (report == null) {
      toast("请等待设备扫描完成");
      return;
    }
    android.content.ClipboardManager clipboard =
        (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
    clipboard.setPrimaryClip(ClipData.newPlainText("CC8 检测报告", reportString()));
    toast("JSON 报告已复制");
  }

  private void exportReport(boolean audio) {
    if (probe.isRunning()) {
      toast("请先停止测试，再导出完整结果");
      return;
    }
    File file;
    String type;
    try {
      if (audio) {
        file = saveCaptureFile();
        type = "audio/wav";
      } else {
        if (report == null) {
          toast("请等待设备扫描完成");
          return;
        }
        file = saveReportFile(false);
        type = "application/json";
      }
      pendingExport = file;
      Intent intent =
          new Intent(Intent.ACTION_CREATE_DOCUMENT)
              .setType(type)
              .addCategory(Intent.CATEGORY_OPENABLE)
              .putExtra(Intent.EXTRA_TITLE, file.getName());
      try {
        startActivityForResult(intent, EXPORT_DOCUMENT);
      } catch (ActivityNotFoundException e) {
        pendingExport = null;
        showSaved(file);
      }
    } catch (Exception e) {
      toast("导出失败：" + e.getMessage());
    }
  }

  private void showSaved(File file) {
    new AlertDialog.Builder(this)
        .setTitle("文件已保存在本机")
        .setMessage(file.getAbsolutePath() + "\n\n可用文件管理器复制，或使用“复制 JSON 报告”。")
        .setPositiveButton("知道了", null)
        .setNeutralButton(
            "复制路径",
            (d, w) -> {
              android.content.ClipboardManager c =
                  (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
              c.setPrimaryClip(ClipData.newPlainText("文件路径", file.getAbsolutePath()));
            })
        .show();
  }

  @Override
  protected void onActivityResult(int request, int result, Intent data) {
    super.onActivityResult(request, result, data);
    if (request != EXPORT_DOCUMENT) return;
    File file = pendingExport;
    pendingExport = null;
    if (result != RESULT_OK || data == null || data.getData() == null || file == null) {
      toast("未选择目标位置；本机文件仍保留");
      return;
    }
    Uri destination = data.getData();
    try (InputStream in = new FileInputStream(file);
        OutputStream out = getContentResolver().openOutputStream(destination)) {
      if (out == null) throw new IOException("无法写入目标文件");
      byte[] buffer = new byte[8192];
      int n;
      while ((n = in.read(buffer)) != -1) out.write(buffer, 0, n);
      toast("文件已导出");
    } catch (IOException | SecurityException e) {
      toast("写入失败：" + e.getMessage());
    }
  }

  private void toast(String value) {
    Toast.makeText(this, value, Toast.LENGTH_LONG).show();
  }

  @Override
  protected void onResume() {
    super.onResume();
    foreground = true;
    getWindow().addFlags(WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
    if (permissionApprovedPending) {
      permissionApprovedPending = false;
      Runnable action = permissionAction;
      permissionAction = null;
      if (action != null) action.run();
    }
    updateStatus();
  }

  @Override
  protected void onPause() {
    foreground = false;
    analysisToken = null;
    if (probe != null) probe.clearAnalysis();
    getWindow()
        .clearFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
    if (probe != null && probe.isRunning()) {
      probe.cancel();
      status = "离开前台，正在停止音频测试…";
    }
    super.onPause();
  }

  @Override
  public void onConfigurationChanged(Configuration configuration) {
    super.onConfigurationChanged(configuration);
    renderPage();
  }

  @Override
  protected void onDestroy() {
    destroyed = true;
    ++scanGeneration;
    if (probe != null) probe.close();
    if (usbReceiverRegistered) {
      try {
        unregisterReceiver(usbReceiver);
      } catch (IllegalArgumentException ignored) {
        // A failed registration never leaves recording or a readable lease behind.
      }
      usbReceiverRegistered = false;
    }
    reader.shutdownNow();
    ui.removeCallbacksAndMessages(null);
    super.onDestroy();
  }
}
