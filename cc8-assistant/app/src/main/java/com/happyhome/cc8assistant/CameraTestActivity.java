package com.happyhome.cc8assistant;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.graphics.SurfaceTexture;
import android.hardware.Camera;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.io.*;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.JSONArray;
import org.json.JSONObject;

/** Foreground-only, bounded local Camera1 test for the CC8's legacy HAL. */
@SuppressWarnings("deprecation")
public final class CameraTestActivity extends Activity
    implements TextureView.SurfaceTextureListener {
  private final Handler ui = new Handler(Looper.getMainLooper());
  private HandlerThread thread;
  private Handler worker;
  private TextureView preview;
  private FrameLayout previewBox;
  private TextView cover, status, details;
  private Button start, stop;
  private volatile boolean foreground, wanted, running;
  private volatile int generation;
  private volatile SurfaceTexture surface;
  private Camera camera;
  private volatile String report = "{}";
  private JSONObject capabilities = new JSONObject();
  private final AtomicInteger textureUpdates = new AtomicInteger();
  private long frames, badFrames, started, firstFrame, lastFrame, lastPublish, changingFrames;
  private double luminanceSum, changeSum;
  private int width, height, format, expectedBytes, minY = 255, maxY = 0, errorCode;
  private String errorType = "";
  private final int[] previousSamples = new int[64];
  private boolean previousValid;

  private int dp(float n) {
    return Math.round(n * getResources().getDisplayMetrics().density);
  }

  private TextView text(String value, int size, int color) {
    TextView t = new TextView(this);
    t.setText(value);
    t.setTextSize(size);
    t.setTextColor(color);
    return t;
  }

  @Override
  public void onCreate(Bundle state) {
    super.onCreate(state);
    getWindow()
        .addFlags(
            WindowManager.LayoutParams.FLAG_FULLSCREEN
                | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    thread = new HandlerThread("cc8-local-camera-test");
    thread.start();
    worker = new Handler(thread.getLooper());
    LinearLayout root = new LinearLayout(this);
    root.setOrientation(LinearLayout.VERTICAL);
    root.setPadding(dp(28), dp(20), dp(28), dp(20));
    root.setBackgroundColor(0xff061521);
    setContentView(root);
    LinearLayout header = new LinearLayout(this);
    header.setGravity(Gravity.CENTER_VERTICAL);
    header.addView(
        text("摄像头检测  /  CAMERA TEST", 27, 0xffd9f0f9), new LinearLayout.LayoutParams(0, -2, 1));
    Button back = new Button(this);
    back.setText("返回助手");
    back.setOnClickListener(v -> finish());
    header.addView(back);
    root.addView(header);
    TextView notice = text("仅在本机预览 · 不拍照、不录音、不保存图像、不上传 · 20 秒自动关闭", 16, 0xfff3b862);
    notice.setPadding(0, dp(8), 0, dp(16));
    root.addView(notice);
    LinearLayout content = new LinearLayout(this);
    root.addView(content, new LinearLayout.LayoutParams(-1, 0, 1));
    previewBox = new FrameLayout(this);
    previewBox.setBackgroundColor(0xff000000);
    LinearLayout.LayoutParams viewLp = new LinearLayout.LayoutParams(0, -1, 2.6f);
    viewLp.rightMargin = dp(22);
    content.addView(previewBox, viewLp);
    preview = new TextureView(this);
    preview.setSurfaceTextureListener(this);
    previewBox.addView(preview, new FrameLayout.LayoutParams(-1, -1, Gravity.CENTER));
    cover = text("摄像头尚未开启\n点击「开始 20 秒预览」", 23, 0xff789caf);
    cover.setGravity(Gravity.CENTER);
    cover.setBackgroundColor(0xff000000);
    previewBox.addView(cover, new FrameLayout.LayoutParams(-1, -1));
    LinearLayout controls = new LinearLayout(this);
    controls.setOrientation(LinearLayout.VERTICAL);
    content.addView(controls, new LinearLayout.LayoutParams(0, -1, 1));
    status = text("等待测试", 22, 0xff59e0f4);
    status.setPadding(0, 0, 0, dp(14));
    controls.addView(status);
    ScrollView scroll = new ScrollView(this);
    details = text("系统已发现前置摄像头。\n本页测试开流、取帧和预览显示。\n测试会暂停语音会话。", 16, 0xffb9d1dd);
    scroll.addView(details);
    controls.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
    start = new Button(this);
    start.setText("开始 20 秒预览");
    start.setContentDescription("开始摄像头本地测试");
    start.setOnClickListener(v -> begin());
    controls.addView(start, new LinearLayout.LayoutParams(-1, dp(58)));
    stop = new Button(this);
    stop.setText("立即关闭摄像头");
    stop.setEnabled(false);
    stop.setOnClickListener(v -> stopTest("stopped"));
    controls.addView(stop, new LinearLayout.LayoutParams(-1, dp(58)));
    publish("ready");
  }

  private boolean permitted() {
    return Build.VERSION.SDK_INT < 23
        || checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
  }

  private void begin() {
    if (!foreground || wanted || running) return;
    if (!permitted()) {
      requestPermissions(new String[] {Manifest.permission.CAMERA}, 32);
      return;
    }
    wanted = true;
    start.setEnabled(false);
    stop.setEnabled(true);
    cover.setVisibility(View.VISIBLE);
    cover.setText("正在打开摄像头…");
    status.setText("正在连接本机摄像头…");
    openIfReady();
  }

  @Override
  public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
    super.onRequestPermissionsResult(request, permissions, results);
    if (request == 32) {
      if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
        if (foreground) begin();
        else status.setText("已授权，请点击开始预览");
      } else status.setText("摄像头权限未获允许，未采集图像");
    }
  }

  private void openIfReady() {
    SurfaceTexture target = surface;
    if (!wanted || !foreground || target == null || running) return;
    running = true;
    int token = ++generation;
    worker.post(() -> openCamera(token, target));
  }

  private boolean current(int token) {
    return foreground && wanted && generation == token;
  }

  private static JSONArray sizes(List<Camera.Size> values) {
    JSONArray out = new JSONArray();
    if (values != null)
      for (Camera.Size size : values)
        if (out.length() < 128) out.put(size.width + "x" + size.height);
    return out;
  }

  @SuppressLint(
      "MissingPermission") // Visible Start action + runtime permission; rechecked on camera thread.
  private void openCamera(int token, SurfaceTexture target) {
    Camera local = null;
    try {
      if (!current(token) || !permitted()) return;
      frames = badFrames = changingFrames = 0;
      started = firstFrame = lastFrame = lastPublish = 0;
      minY = 255;
      maxY = 0;
      luminanceSum = changeSum = 0;
      previousValid = false;
      textureUpdates.set(0);
      errorCode = 0;
      errorType = "";
      capabilities = new JSONObject();
      int count = Camera.getNumberOfCameras();
      Wire.put(capabilities, "camera_count", count);
      if (count == 0) throw new IOException("系统未暴露摄像头");
      int id = 0;
      Camera.CameraInfo info = new Camera.CameraInfo();
      for (int i = 0; i < count; i++) {
        Camera.getCameraInfo(i, info);
        if (info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT) {
          id = i;
          break;
        }
      }
      Camera.getCameraInfo(id, info);
      Wire.put(capabilities, "camera_id", id);
      Wire.put(
          capabilities,
          "facing",
          info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT ? "front" : "back");
      Wire.put(capabilities, "sensor_orientation", info.orientation);
      local = Camera.open(id);
      if (!current(token)) {
        local.release();
        return;
      }
      camera = local;
      Camera.Parameters params = local.getParameters();
      Wire.put(capabilities, "preview_sizes", sizes(params.getSupportedPreviewSizes()));
      Wire.put(capabilities, "picture_sizes", sizes(params.getSupportedPictureSizes()));
      JSONArray formats = new JSONArray();
      List<Integer> supported = params.getSupportedPreviewFormats();
      if (supported != null) for (int value : supported) formats.put(value);
      Wire.put(capabilities, "preview_formats", formats);
      JSONArray rates = new JSONArray();
      List<int[]> ranges = params.getSupportedPreviewFpsRange();
      if (ranges != null)
        for (int[] range : ranges) {
          JSONArray value = new JSONArray();
          value.put(range[0]);
          value.put(range[1]);
          rates.put(value);
        }
      Wire.put(capabilities, "fps_ranges", rates);
      Camera.Size best = params.getPreviewSize();
      double score = Double.MAX_VALUE;
      List<Camera.Size> choices = params.getSupportedPreviewSizes();
      if (choices != null)
        for (Camera.Size size : choices) {
          double next =
              Math.abs(Math.log((double) size.width * size.height / (640 * 480)))
                  + 4 * Math.abs((double) size.width / size.height - 4.0 / 3);
          if (next < score) {
            score = next;
            best = size;
          }
        }
      params.setPreviewSize(best.width, best.height);
      if (supported != null && supported.contains(ImageFormat.NV21))
        params.setPreviewFormat(ImageFormat.NV21);
      local.setParameters(params);
      params = local.getParameters();
      width = params.getPreviewSize().width;
      height = params.getPreviewSize().height;
      format = params.getPreviewFormat();
      expectedBytes = (width * height * ImageFormat.getBitsPerPixel(format) + 7) / 8;
      if (expectedBytes <= 0 || expectedBytes > 8 * 1024 * 1024)
        throw new IOException("不支持的预览缓冲大小");
      int[] fps = new int[2];
      params.getPreviewFpsRange(fps);
      Wire.put(capabilities, "configured_fps_min", fps[0]);
      Wire.put(capabilities, "configured_fps_max", fps[1]);
      int rotation = getWindowManager().getDefaultDisplay().getRotation();
      int degrees =
          rotation == Surface.ROTATION_90
              ? 90
              : rotation == Surface.ROTATION_180 ? 180 : rotation == Surface.ROTATION_270 ? 270 : 0;
      int display =
          info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT
              ? (360 - (info.orientation + degrees) % 360) % 360
              : (info.orientation - degrees + 360) % 360;
      Wire.put(capabilities, "display_orientation", display);
      local.setDisplayOrientation(display);
      target.setDefaultBufferSize(width, height);
      local.setPreviewTexture(target);
      final Camera owned = local;
      local.setErrorCallback(
          (code, ignored) -> {
            if (current(token)) {
              errorCode = code;
              errorType = "CameraError";
              stopTest("error");
            }
          });
      local.setPreviewCallbackWithBuffer(
          (data, owner) -> {
            if (!current(token) || owner != camera) return;
            long now = SystemClock.elapsedRealtime();
            if (firstFrame == 0) firstFrame = now;
            lastFrame = now;
            frames++;
            if (data == null || data.length < expectedBytes) badFrames++;
            else sampleLuma(data);
            if (now - lastPublish >= 500) {
              lastPublish = now;
              publish("previewing");
            }
            if (current(token))
              try {
                owned.addCallbackBuffer(data);
              } catch (RuntimeException ignored) {
              }
          });
      for (int i = 0; i < 3; i++) local.addCallbackBuffer(new byte[expectedBytes]);
      if (!current(token)) {
        release();
        return;
      }
      local.startPreview();
      started = SystemClock.elapsedRealtime();
      publish("previewing");
      ui.post(
          () -> {
            if (current(token)) {
              fitPreview(display);
              cover.setVisibility(View.GONE);
            }
          });
      worker.postDelayed(
          () -> {
            if (current(token)) stopTest("completed");
          },
          20000);
    } catch (Exception error) {
      if (current(token)) {
        errorType = error.getClass().getSimpleName();
        wanted = false;
        release();
        publish("error");
      } else if (local != null && local != camera)
        try {
          local.release();
        } catch (RuntimeException ignored) {
        }
    } finally {
      if (!current(token) && camera != null) release();
      if (!wanted) running = false;
    }
  }

  private void sampleLuma(byte[] data) {
    int step = format == ImageFormat.YUY2 ? 2 : 1;
    if (format != ImageFormat.NV21 && format != ImageFormat.YV12 && format != ImageFormat.YUY2)
      return;
    double sum = 0, change = 0;
    int pixels = width * height;
    for (int i = 0; i < 64; i++) {
      int index = Math.min(data.length - 1, ((i * 7919 + pixels / 11) % pixels) * step);
      int y = data[index] & 255;
      sum += y;
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
      if (previousValid) change += Math.abs(y - previousSamples[i]);
      previousSamples[i] = y;
    }
    luminanceSum += sum / 64;
    change /= 64;
    if (previousValid) {
      changeSum += change;
      if (change > .1) changingFrames++;
    }
    previousValid = true;
  }

  private void fitPreview(int rotation) {
    if (width == 0 || height == 0) return;
    float ratio =
        (rotation == 90 || rotation == 270) ? (float) height / width : (float) width / height;
    int w = previewBox.getWidth(), h = previewBox.getHeight();
    if (w <= 0 || h <= 0) return;
    int targetW = Math.min(w, Math.round(h * ratio)), targetH = Math.round(targetW / ratio);
    preview.setLayoutParams(new FrameLayout.LayoutParams(targetW, targetH, Gravity.CENTER));
  }

  private void release() {
    Camera owned = camera;
    camera = null;
    if (owned != null) {
      try {
        owned.setPreviewCallbackWithBuffer(null);
        owned.stopPreview();
      } catch (RuntimeException ignored) {
      }
      try {
        owned.release();
      } catch (RuntimeException ignored) {
      }
    }
    running = false;
  }

  private void stopTest(String reason) {
    boolean hadRun = wanted || running;
    wanted = false;
    generation++;
    ui.post(
        () -> {
          cover.setVisibility(View.VISIBLE);
          cover.setText("摄像头已关闭\n图像未保存或上传");
          start.setEnabled(true);
          stop.setEnabled(false);
        });
    if (hadRun && worker != null)
      worker.post(
          () -> {
            release();
            publish(reason);
          });
  }

  private void publish(String phase) {
    JSONObject value;
    try {
      value = new JSONObject(capabilities.toString());
    } catch (Exception ignored) {
      value = new JSONObject();
    }
    long duration = firstFrame > 0 && lastFrame > firstFrame ? lastFrame - firstFrame : 0;
    Wire.put(value, "phase", phase);
    Wire.put(value, "api", "Camera1 / legacy HAL");
    Wire.put(value, "permission_granted", permitted());
    Wire.put(value, "preview_width", width);
    Wire.put(value, "preview_height", height);
    Wire.put(value, "preview_format", format);
    Wire.put(value, "expected_frame_bytes", expectedBytes);
    Wire.put(value, "frames", frames);
    Wire.put(value, "bad_frames", badFrames);
    Wire.put(value, "texture_updates", textureUpdates.get());
    Wire.put(
        value,
        "first_frame_latency_ms",
        firstFrame > 0 && started > 0 ? Math.max(0, firstFrame - started) : -1);
    Wire.put(value, "frame_span_ms", duration);
    Wire.put(
        value,
        "observed_fps",
        duration > 0 ? Math.round((frames - 1) * 10000.0 / duration) / 10.0 : 0);
    Wire.put(value, "luma_min", frames > 0 ? minY : 0);
    Wire.put(value, "luma_max", maxY);
    Wire.put(value, "mean_luma", frames > 0 ? Math.round(luminanceSum / frames * 100) / 100.0 : 0);
    Wire.put(value, "changing_frames", changingFrames);
    Wire.put(
        value,
        "mean_frame_luma_change",
        frames > 1 ? Math.round(changeSum / (frames - 1) * 100) / 100.0 : 0);
    Wire.put(value, "camera_open", camera != null);
    Wire.put(value, "preview_verified", frames >= 5 && badFrames == 0 && textureUpdates.get() > 0);
    Wire.put(value, "non_uniform_image", maxY - minY > 16);
    Wire.put(value, "error_type", errorType);
    Wire.put(value, "error_code", errorCode);
    Wire.put(value, "images_saved", false);
    Wire.put(value, "images_uploaded", false);
    Wire.put(value, "audio_recorded", false);
    final JSONObject captured = value;
    report = value.toString();
    if (!"previewing".equals(phase) && !"ready".equals(phase))
      try (FileOutputStream out = openFileOutput("camera-test-report.json", MODE_PRIVATE)) {
        out.write(report.getBytes("UTF-8"));
      } catch (IOException ignored) {
      }
    ui.post(
        () -> {
          if (isFinishing()) return;
          status.setText(
              "previewing".equals(phase)
                  ? "摄像头使用中 · 20 秒后自动关闭"
                  : "completed".equals(phase)
                      ? "测试结束 · 摄像头已关闭"
                      : "error".equals(phase)
                          ? "调用失败 · " + errorType
                          : "ready".equals(phase) ? "等待测试" : "摄像头已关闭");
          details.setText(
              "前置摄像头 · Camera1\n预览："
                  + width
                  + " × "
                  + height
                  + "\n收到帧："
                  + frames
                  + "\n显示更新："
                  + textureUpdates.get()
                  + "\n实际帧率："
                  + captured.optDouble("observed_fps")
                  + " fps\n帧长度错误："
                  + badFrames
                  + "\n图像亮度范围："
                  + (frames > 0 ? minY : 0)
                  + "–"
                  + maxY
                  + "\n\n"
                  + (captured.optBoolean("preview_verified") ? "已验证：开流、取帧及显示更新。" : "等待收到连续画面…")
                  + "\n不保存或上传图像，仅保存检测参数。");
          if (!wanted) {
            cover.setVisibility(View.VISIBLE);
            if (!"ready".equals(phase)) cover.setText("摄像头已关闭\n图像未保存或上传");
            start.setEnabled(true);
            stop.setEnabled(false);
          }
        });
  }

  @Override
  protected void onResume() {
    super.onResume();
    foreground = true;
    DesktopEntryService.update(this, true);
  }

  @Override
  protected void onPause() {
    foreground = false;
    stopTest("background");
    DesktopEntryService.update(this, false);
    super.onPause();
  }

  @Override
  protected void onDestroy() {
    stopTest("destroyed");
    if (thread != null) thread.quitSafely();
    super.onDestroy();
  }

  @Override
  public void onSurfaceTextureAvailable(SurfaceTexture texture, int w, int h) {
    surface = texture;
    openIfReady();
  }

  @Override
  public void onSurfaceTextureSizeChanged(SurfaceTexture texture, int w, int h) {}

  @Override
  public boolean onSurfaceTextureDestroyed(SurfaceTexture texture) {
    surface = null;
    stopTest("surface_destroyed");
    return true;
  }

  @Override
  public void onSurfaceTextureUpdated(SurfaceTexture texture) {
    if (wanted) textureUpdates.incrementAndGet();
  }

  @Override
  public void dump(String prefix, FileDescriptor fd, PrintWriter writer, String[] args) {
    writer.println("CC8_CAMERA_TEST_STATUS " + report);
  }
}
