package com.happyhome.cc8diagnostics;

import android.Manifest;
import android.app.Activity;
import android.app.ActivityManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Point;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkInfo;
import android.os.BatteryManager;
import android.os.Build;
import android.os.StatFs;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.view.Display;
import org.json.*;
import java.io.*;
import java.text.SimpleDateFormat;
import java.util.*;
import java.util.concurrent.*;

public final class DeviceReport {
  public interface Query {
    Object get() throws Exception;
  }

  private static final ScheduledExecutorService WATCHDOG =
      Executors.newSingleThreadScheduledExecutor(
          r -> {
            Thread t = new Thread(r, "getprop-timeout");
            t.setDaemon(true);
            return t;
          });

  public static Object safe(Query query) {
    try {
      Object value = query.get();
      return value == null ? JSONObject.NULL : value;
    } catch (Exception | LinkageError e) {
      return "未知：" + e.getClass().getSimpleName();
    }
  }

  public static JSONObject object(Object... pairs) {
    JSONObject result = new JSONObject();
    for (int i = 0; i + 1 < pairs.length; i += 2)
      put(result, String.valueOf(pairs[i]), pairs[i + 1]);
    return result;
  }

  public static void put(JSONObject object, String key, Object value) {
    try {
      object.put(key, value == null ? JSONObject.NULL : value);
    } catch (JSONException ignored) {
    }
  }

  public static JSONArray array(int[] values) {
    JSONArray a = new JSONArray();
    for (int v : values) a.put(v);
    return a;
  }

  public static String timestamp() {
    return new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSZ", Locale.US).format(new Date());
  }

  private static String smallFile(String path) throws IOException {
    try (BufferedReader reader = new BufferedReader(new FileReader(path))) {
      char[] b = new char[8192];
      int n = reader.read(b);
      return n > 0 ? new String(b, 0, n).trim() : "";
    }
  }

  private static Object property(String key) {
    return safe(
        () -> {
          Process process =
              new ProcessBuilder("/system/bin/getprop", key).redirectErrorStream(true).start();
          ScheduledFuture<?> stop =
              WATCHDOG.schedule(process::destroy, 1500, TimeUnit.MILLISECONDS);
          try (BufferedReader reader =
              new BufferedReader(new InputStreamReader(process.getInputStream()))) {
            String value = reader.readLine();
            int exit = process.waitFor();
            if (exit != 0) throw new IOException("getprop exit " + exit);
            return value == null || value.trim().isEmpty() ? JSONObject.NULL : value.trim();
          } finally {
            stop.cancel(false);
            process.destroy();
          }
        });
  }

  public static JSONObject collect(Activity activity) {
    JSONObject root = new JSONObject();
    put(root, "schema_version", "1.0");
    put(root, "captured_at", timestamp());
    put(
        root,
        "app",
        object(
            "name",
            "CC8 能力检测",
            "version",
            safe(
                () ->
                    activity
                        .getPackageManager()
                        .getPackageInfo(activity.getPackageName(), 0)
                        .versionName),
            "version_code",
            safe(
                () ->
                    activity
                        .getPackageManager()
                        .getPackageInfo(activity.getPackageName(), 0)
                        .versionCode),
            "package",
            activity.getPackageName(),
            "uid",
            android.os.Process.myUid(),
            "offline",
            true));
    JSONObject sections = new JSONObject();
    put(
        sections,
        "机型与固件",
        object(
            "制造商",
            Build.MANUFACTURER,
            "品牌",
            Build.BRAND,
            "型号",
            Build.MODEL,
            "设备代号",
            Build.DEVICE,
            "产品代号",
            Build.PRODUCT,
            "主板",
            Build.BOARD,
            "硬件平台",
            Build.HARDWARE,
            "Android 版本",
            Build.VERSION.RELEASE,
            "API 等级",
            Build.VERSION.SDK_INT,
            "系统显示版本",
            Build.DISPLAY,
            "Build ID",
            Build.ID,
            "增量版本",
            Build.VERSION.INCREMENTAL,
            "构建指纹",
            Build.FINGERPRINT,
            "安全补丁日期",
            Build.VERSION.SDK_INT >= 23 ? Build.VERSION.SECURITY_PATCH : JSONObject.NULL,
            "系统构建时间",
            new SimpleDateFormat("yyyy-MM-dd HH:mm:ss Z", Locale.US).format(new Date(Build.TIME))));
    JSONObject props = new JSONObject();
    for (String key :
        new String[] {
          "ro.board.platform",
          "ro.hardware",
          "ro.product.cpu.abilist",
          "ro.build.display.id",
          "ro.build.version.incremental",
          "ro.build.version.release",
          "ro.build.version.sdk",
          "ro.build.version.security_patch",
          "ro.boot.verifiedbootstate",
          "ro.boot.flash.locked",
          "ro.boot.vbmeta.device_state",
          "ro.debuggable",
          "ro.secure"
        }) put(props, key, property(key));
    put(sections, "只读系统属性", props);
    JSONObject memory =
        object(
            "支持的 APK ABI",
            new JSONArray(Arrays.asList(Build.SUPPORTED_ABIS)),
            "32 位 ABI",
            new JSONArray(Arrays.asList(Build.SUPPORTED_32_BIT_ABIS)),
            "64 位 ABI",
            new JSONArray(Arrays.asList(Build.SUPPORTED_64_BIT_ABIS)),
            "Java 进程架构",
            System.getProperty("os.arch"),
            "当前可用逻辑核心数",
            Runtime.getRuntime().availableProcessors(),
            "CPU0 最高频率 kHz",
            safe(() -> smallFile("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")));
    put(
        memory,
        "CPU 信息摘要",
        safe(
            () -> {
              JSONObject cpu = new JSONObject();
              for (String line : smallFile("/proc/cpuinfo").split("\n")) {
                int colon = line.indexOf(':');
                if (colon < 0) continue;
                String key = line.substring(0, colon).trim();
                if (Arrays.asList(
                        "Hardware",
                        "Processor",
                        "model name",
                        "CPU architecture",
                        "CPU implementer",
                        "CPU part")
                    .contains(key)) put(cpu, key, line.substring(colon + 1).trim());
              }
              return cpu;
            }));
    put(
        memory,
        "RAM",
        safe(
            () -> {
              ActivityManager manager =
                  (ActivityManager) activity.getSystemService(Context.ACTIVITY_SERVICE);
              ActivityManager.MemoryInfo info = new ActivityManager.MemoryInfo();
              manager.getMemoryInfo(info);
              return object(
                  "total_bytes",
                  info.totalMem,
                  "available_bytes",
                  info.availMem,
                  "low_memory",
                  info.lowMemory,
                  "app_heap_limit_mib",
                  manager.getMemoryClass());
            }));
    put(
        memory,
        "应用数据分区",
        safe(
            () -> {
              StatFs fs = new StatFs(activity.getFilesDir().getAbsolutePath());
              return object(
                  "total_bytes",
                  fs.getTotalBytes(),
                  "available_bytes",
                  fs.getAvailableBytes(),
                  "note",
                  "数据分区大小不等于整颗 eMMC 的出厂容量");
            }));
    put(sections, "处理器与内存", memory);
    put(
        sections,
        "屏幕与输入",
        safe(
            () -> {
              Display display = activity.getWindowManager().getDefaultDisplay();
              DisplayMetrics real = new DisplayMetrics();
              display.getRealMetrics(real);
              Point app = new Point();
              display.getSize(app);
              return object(
                  "物理像素",
                  real.widthPixels + " × " + real.heightPixels,
                  "应用可用像素",
                  app.x + " × " + app.y,
                  "density_dpi",
                  real.densityDpi,
                  "density",
                  real.density,
                  "scaled_density",
                  real.scaledDensity,
                  "refresh_rate_hz",
                  display.getRefreshRate(),
                  "touchscreen_feature",
                  activity.getPackageManager().hasSystemFeature(PackageManager.FEATURE_TOUCHSCREEN),
                  "microphone_feature",
                  activity.getPackageManager().hasSystemFeature(PackageManager.FEATURE_MICROPHONE));
            }));
    put(sections, "电池与供电快照", battery(activity));
    AudioManager audio = (AudioManager) activity.getSystemService(Context.AUDIO_SERVICE);
    JSONObject sound =
        object(
            "AEC 接口可用",
            safe(AcousticEchoCanceler::isAvailable),
            "AGC 接口可用",
            safe(AutomaticGainControl::isAvailable),
            "降噪接口可用",
            safe(NoiseSuppressor::isAvailable),
            "硬件麦克风静音标志",
            safe(audio::isMicrophoneMute),
            "当前音频模式",
            safe(audio::getMode),
            "媒体音量",
            safe(() -> audio.getStreamVolume(AudioManager.STREAM_MUSIC)),
            "媒体最大音量",
            safe(() -> audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC)),
            "输出采样率属性",
            safe(() -> audio.getProperty(AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE)),
            "输出帧缓冲属性",
            safe(() -> audio.getProperty(AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER)));
    if (Build.VERSION.SDK_INT >= 24)
      put(
          sound,
          "UNPROCESSED 支持属性",
          safe(() -> audio.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)));
    JSONObject rates = new JSONObject();
    for (int rate : new int[] {16000, 44100, 48000})
      put(
          rates,
          String.valueOf(rate),
          safe(
              () ->
                  AudioRecord.getMinBufferSize(
                      rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)));
    put(sound, "单声道 PCM16 最小录音缓冲字节", rates);
    put(sound, "系统枚举音频端口", safe(() -> ports(audio)));
    put(sound, "解释", "端口数/声道数不等于物理麦克风数量；接口可用、缓冲参数有效不代表实机录音和 AEC 已通过。");
    put(sections, "音频接口", sound);
    JSONArray su = new JSONArray();
    for (String path : new String[] {"/system/bin/su", "/system/xbin/su", "/sbin/su", "/su/bin/su"})
      if (new File(path).exists()) su.put(path);
    put(
        sections,
        "权限与调试状态",
        object(
            "麦克风权限",
            activity.checkCallingOrSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED,
            "已作为 APK 运行",
            true,
            "系统应用",
            (activity.getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_SYSTEM)
                != 0,
            "安装来源",
            safe(
                () ->
                    activity
                        .getPackageManager()
                        .getInstallerPackageName(activity.getPackageName())),
            "ADB 设置标志",
            safe(
                () ->
                    Settings.Global.getInt(
                        activity.getContentResolver(), Settings.Global.ADB_ENABLED, -1)),
            "开发者选项标志",
            safe(
                () ->
                    Settings.Global.getInt(
                        activity.getContentResolver(),
                        Settings.Global.DEVELOPMENT_SETTINGS_ENABLED,
                        -1)),
            "build_tags",
            Build.TAGS,
            "可观察 su 路径",
            su,
            "root_进程",
            android.os.Process.myUid() == 0,
            "说明",
            "ADB 标志不代表已连接/授权；未执行 su。路径和 build_tags 不能独立证明 Root 成功或不存在。"));
    put(sections, "网络连接类型", safe(() -> network(activity)));
    put(root, "device", sections);
    put(
        root,
        "limitations",
        new JSONArray(
            Arrays.asList(
                "系统未提供或权限拒绝的字段为 null/未知，不能当作不支持。",
                "不采集序列号、IMEI、MAC、Wi-Fi 名称、账号信息；APK 不含 INTERNET 权限。",
                "系统构建时间、安全补丁日期与 OTA 推送日期不等价。",
                "音频测试结果用于人工对比，不自动认定有效 AEC 或可靠全双工。",
                "没有执行 Root、解锁、刷机、停用原厂服务或开机配置修改。")));
    return root;
  }

  public static Object battery(Context context) {
    return safe(
        () -> {
          Intent i =
              context.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
          if (i == null) return object("状态", "未提供");
          int scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, -1),
              level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
          Object temperature =
              i.hasExtra(BatteryManager.EXTRA_TEMPERATURE)
                  ? i.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0
                  : JSONObject.NULL;
          return object(
              "level",
              level,
              "scale",
              scale,
              "percent",
              scale > 0 && level >= 0 ? Math.round(level * 100.0 / scale) : JSONObject.NULL,
              "status",
              i.getIntExtra(BatteryManager.EXTRA_STATUS, -1),
              "health",
              i.getIntExtra(BatteryManager.EXTRA_HEALTH, -1),
              "plugged",
              i.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1),
              "voltage_mv",
              i.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1),
              "temperature_c",
              temperature,
              "technology",
              i.getStringExtra(BatteryManager.EXTRA_TECHNOLOGY),
              "解释",
              "仅当前电池快照，不能据此推断某固件已修复或存在充电限制。");
        });
  }

  private static Object ports(AudioManager manager) {
    if (Build.VERSION.SDK_INT < 23) return "API 23 以下未提供此枚举接口";
    JSONArray ports = new JSONArray();
    appendPorts(ports, manager.getDevices(AudioManager.GET_DEVICES_INPUTS), "input");
    appendPorts(ports, manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS), "output");
    return ports;
  }

  @android.annotation.TargetApi(23) // Called only behind the SDK >= 23 guard in ports().
  private static void appendPorts(JSONArray ports, AudioDeviceInfo[] devices, String direction) {
    for (AudioDeviceInfo d : devices)
      ports.put(
          object(
              "direction",
              direction,
              "type",
              d.getType(),
              "type_name",
              portType(d.getType()),
              "source",
              d.isSource(),
              "sink",
              d.isSink(),
              "sample_rates",
              array(d.getSampleRates()),
              "channel_counts",
              array(d.getChannelCounts()),
              "encodings",
              array(d.getEncodings())));
  }

  private static String portType(int type) {
    switch (type) {
      case 1:
        return "听筒";
      case 2:
        return "内置扬声器";
      case 3:
        return "有线耳麦";
      case 4:
        return "有线耳机";
      case 7:
        return "蓝牙 SCO";
      case 8:
        return "蓝牙 A2DP";
      case 9:
        return "HDMI";
      case 11:
        return "USB 音频设备";
      case 12:
        return "USB 音频附件";
      case 15:
        return "内置麦克风";
      case 18:
        return "电视调谐器";
      case 22:
        return "USB 耳麦";
      default:
        return "其他系统端口";
    }
  }

  private static Object network(Context context) {
    ConnectivityManager manager =
        (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
    if (Build.VERSION.SDK_INT >= 23) {
      Network active = manager.getActiveNetwork();
      NetworkCapabilities cap = active == null ? null : manager.getNetworkCapabilities(active);
      return object(
          "存在活动网络",
          active != null,
          "Wi-Fi",
          cap != null && cap.hasTransport(NetworkCapabilities.TRANSPORT_WIFI),
          "Ethernet",
          cap != null && cap.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET),
          "说明",
          "仅查询网络类型，未联网探测或上传数据。");
    }
    NetworkInfo active = manager.getActiveNetworkInfo();
    return object(
        "存在活动网络",
        active != null,
        "connected",
        active != null && active.isConnected(),
        "type",
        active == null ? JSONObject.NULL : active.getTypeName());
  }
}
