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

/** Visible, one-shot Camera1 capture. No files, no continuous streaming. */
@SuppressWarnings("deprecation")
final class SingleFrameCamera implements TextureView.SurfaceTextureListener {
  // serial 隔离每次前台拍摄；取消、超时或 Surface 销毁后，旧回调不能再提交图片。
  interface Result {
    void image(byte[] jpeg, int width, int height);

    void error(String reason);
  }

  interface Authorization {
    boolean current();
  }

  private final Activity activity;
  private final FrameLayout panel;
  private final TextureView preview;
  private final TextView label;
  private final PreviewDelay delay = new PreviewDelay();
  private volatile long capturedAfterPreviewMs;
  private final Handler ui = new Handler(Looper.getMainLooper());
  private final HandlerThread thread = new HandlerThread("cc8-one-frame-camera");
  private final Handler worker;
  private final PhotoReviewPanel reviewPanel;
  private final PhotoReviewClock reviewClock = new PhotoReviewClock();
  private final Runnable cancelAction;
  private volatile Authorization authorized = () -> true;
  private volatile long reviewTask;
  private volatile boolean reviewEnabled;
  private volatile long retakeVisibleBaseline;
  private volatile SurfaceTexture surface;
  private volatile boolean wanted, opening;
  private volatile int serial;
  private Camera camera;
  private Result result;
  private volatile boolean cameraOpen, frameReceived;
  private long captures;
  private String lastError = "";
  private volatile CaptureProfile profile = CaptureProfile.legacy();
  private volatile boolean preferHD;
  private volatile int previewWidth, previewHeight;
  private volatile int previewRotation;
  private volatile String qualityLabel = "", qualityFallback = "";

  SingleFrameCamera(Activity activity, FrameLayout stage, Runnable cancel) {
    this.activity = activity;
    cancelAction = cancel;
    thread.start();
    worker = new Handler(thread.getLooper());
    panel = new FrameLayout(activity);
    panel.setBackgroundColor(0xff061521);
    panel.setVisibility(View.GONE);
    preview = new TextureView(activity);
    preview.setSurfaceTextureListener(this);
    panel.addView(preview, new FrameLayout.LayoutParams(-1, -1));
    label = new TextView(activity);
    label.setText("● 摄像头打开中 · 请准备调整画面");
    label.setTextColor(0xffffc36a);
    label.setTextSize(15);
    label.setPadding(10, 8, 10, 8);
    label.setBackgroundColor(0xdd061521);
    panel.addView(label, new FrameLayout.LayoutParams(-1, -2, Gravity.TOP));
    Button stop = new Button(activity);
    stop.setText("取消拍摄");
    stop.setOnClickListener(v -> cancel.run());
    panel.addView(stop, new FrameLayout.LayoutParams(-1, 48, Gravity.BOTTOM));
    FrameLayout.LayoutParams lp =
        new FrameLayout.LayoutParams(360, 320, Gravity.TOP | Gravity.LEFT);
    lp.leftMargin = 40;
    lp.topMargin = 155;
    stage.addView(panel, lp);
    reviewPanel = new PhotoReviewPanel(activity, stage, worker);
  }

  void start(Result callback) {
    start(callback, false, CaptureProfile.legacy());
  }

  void start(Result callback, boolean highDefinition, CaptureProfile limits) {
    start(callback, highDefinition, limits, false, () -> true);
  }

  void start(
      Result callback,
      boolean highDefinition,
      CaptureProfile limits,
      boolean reviewRequested,
      Authorization authorizer) {
    // Review never extends the original task deadline on retake. Each shot
    // still needs its own visible five-second framing window.
    cancel();
    result = callback;
    wanted = true;
    authorized = authorizer;
    profile = limits;
    preferHD = highDefinition;
    reviewEnabled = reviewRequested && limits.reviewAllowed;
    final long task = reviewClock.begin(SystemClock.elapsedRealtime(), reviewEnabled);
    reviewTask = task;
    serial++;
    prepareAttempt();
    ui.postDelayed(
        () -> {
          if (wanted && reviewClock.sameTask(task)) fail(serial, "camera_timeout");
        },
        reviewEnabled ? PhotoReviewClock.REVIEW_MS : PhotoReviewClock.ATTEMPT_MS);
  }

  private void prepareAttempt() {
    retakeVisibleBaseline = surface == null ? Long.MIN_VALUE : surface.getTimestamp();
    previewWidth = previewHeight = 0;
    qualityLabel = qualityFallback = "";
    lastError = "";
    opening = false;
    delay.reset();
    frameReceived = false;
    capturedAfterPreviewMs = 0;
    label.setText("● 摄像头打开中 · 请准备调整画面");
    panel.setVisibility(View.VISIBLE);
    openIfReady();
    final int token = serial;
    ui.postDelayed(
        () -> {
          if (alive(token) && !reviewClock.reviewing()) fail(token, "camera_timeout");
        },
        PhotoReviewClock.ATTEMPT_MS);
  }

  boolean busy() {
    return wanted;
  }

  boolean cameraOpen() {
    return cameraOpen;
  }

  long captures() {
    return captures;
  }

  long previewHoldMs() {
    return PreviewDelay.HOLD_MS;
  }

  long capturedAfterPreviewMs() {
    return capturedAfterPreviewMs;
  }

  long previewElapsedMs() {
    return wanted ? delay.elapsed(SystemClock.elapsedRealtime()) : capturedAfterPreviewMs;
  }

  String lastError() {
    return lastError;
  }

  int previewWidth() {
    return previewWidth;
  }

  int previewHeight() {
    return previewHeight;
  }

  String qualityFallback() {
    return qualityFallback;
  }

  private boolean current(int token) {
    return alive(token)
        && reviewClock.current(reviewTask, SystemClock.elapsedRealtime())
        && authorized.current();
  }

  private boolean alive(int token) {
    return wanted && serial == token;
  }

  private void openIfReady() {
    SurfaceTexture target = surface;
    if (!wanted || opening || target == null) return;
    opening = true;
    final int token = serial;
    worker.post(() -> open(token, target));
  }

  @SuppressLint(
      "MissingPermission") // Explicit visible button/voice request; runtime permission checked in
  // MainActivity and here.
  private void open(int token, SurfaceTexture target) {
    try {
      if (!current(token)) {
        retire(token);
        return;
      }
      if (activity.checkCallingOrSelfPermission(Manifest.permission.CAMERA)
          != PackageManager.PERMISSION_GRANTED) {
        fail(token, "permission_denied");
        return;
      }
      int count = Camera.getNumberOfCameras();
      if (count == 0) {
        fail(token, "camera_unavailable");
        return;
      }
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
      Camera owned = Camera.open(id);
      camera = owned;
      cameraOpen = true;
      if (!current(token)) {
        release();
        retire(token);
        return;
      }
      Camera.Parameters p = owned.getParameters();
      java.util.List<Camera.Size> sizes = p.getSupportedPreviewSizes();
      if (sizes == null
          || sizes.size() > 256
          || p.getSupportedPreviewFormats() == null
          || !p.getSupportedPreviewFormats().contains(ImageFormat.NV21)) {
        fail(token, "unsupported_preview");
        return;
      }
      int[][] candidates = new int[sizes.size()][2];
      for (int i = 0; i < sizes.size(); i++) {
        candidates[i][0] = sizes.get(i).width;
        candidates[i][1] = sizes.get(i).height;
      }
      final CaptureProfile limits = profile;
      final long task = reviewTask;
      final int shot = reviewClock.attempt();
      int[] selected = PreviewSizes.select(candidates, preferHD, limits.maxEdge, limits.maxPixels);
      if (selected == null) {
        fail(token, "unsupported_preview");
        return;
      }
      final int frameWidth = selected[0], frameHeight = selected[1];
      p.setPreviewSize(frameWidth, frameHeight);
      p.setPreviewFormat(ImageFormat.NV21);
      owned.setParameters(p);
      Camera.Size applied = owned.getParameters().getPreviewSize();
      if (applied == null || applied.width != frameWidth || applied.height != frameHeight) {
        fail(token, "unsupported_preview");
        return;
      }
      previewWidth = frameWidth;
      previewHeight = frameHeight;
      qualityFallback =
          preferHD && limits.version < 2
              ? "host_legacy"
              : preferHD && frameWidth * frameHeight <= 640 * 480 ? "hd_unavailable" : "";
      qualityLabel =
          " · "
              + frameWidth
              + "×"
              + frameHeight
              + (qualityFallback.isEmpty() ? "" : " · 高清不可用，使用标准画面");
      int rotation = activity.getWindowManager().getDefaultDisplay().getRotation();
      int degrees =
          rotation == Surface.ROTATION_90
              ? 90
              : rotation == Surface.ROTATION_180 ? 180 : rotation == Surface.ROTATION_270 ? 270 : 0;
      final int upright =
          info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT
              ? (info.orientation + degrees) % 360
              : (info.orientation - degrees + 360) % 360;
      previewRotation = upright;
      ui.post(
          () -> {
            if (current(token)) fitPreview();
          });
      owned.setDisplayOrientation(
          info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT ? (360 - upright) % 360 : upright);
      target.setDefaultBufferSize(frameWidth, frameHeight);
      owned.setPreviewTexture(target);
      final int[] frames = {0};
      owned.setErrorCallback((code, ignored) -> fail(token, "camera_error_" + code));
      owned.setPreviewCallbackWithBuffer(
          (data, owner) -> {
            if (owner != camera) return;
            if (!current(token)) {
              release();
              retire(token);
              return;
            }
            if (data == null || data.length < frameWidth * frameHeight * 3 / 2) {
              fail(token, "invalid_frame");
              return;
            }
            frameReceived = true;
            frames[0]++;
            if (frames[0] < 8 || !delay.ready(SystemClock.elapsedRealtime())) {
              try {
                owned.addCallbackBuffer(data);
              } catch (RuntimeException error) {
                fail(token, "preview_failed");
              }
              return;
            }
            try {
              capturedAfterPreviewMs = delay.elapsed(SystemClock.elapsedRealtime());
              CameraJpeg.Frame frame =
                  CameraJpeg.encode(data, frameWidth, frameHeight, upright, limits);
              release();
              if (!reviewClock.captured(task, shot, SystemClock.elapsedRealtime())) {
                fail(token, "camera_timeout");
                return;
              }
              ui.post(
                  () -> {
                    if (!current(token)) {
                      retire(token);
                      return;
                    }
                    opening = false;
                    if (reviewEnabled) showReview(token, frame);
                    else complete(token, frame.bytes, frame.width, frame.height);
                  });
            } catch (Exception | OutOfMemoryError error) {
              fail(token, "image_encode_failed");
            }
          });
      for (int i = 0; i < 3; i++)
        owned.addCallbackBuffer(new byte[frameWidth * frameHeight * 3 / 2]);
      if (!current(token)) {
        release();
        retire(token);
        return;
      }
      owned.startPreview();
    } catch (Exception | OutOfMemoryError error) {
      fail(token, "camera_open_failed");
    }
  }

  private void fail(int token, String reason) {
    worker.post(
        () -> {
          if (!alive(token)) return;
          release();
          ui.post(
              () -> {
                if (!alive(token)) return;
                boolean report = authorized.current();
                reviewClock.cancel();
                wanted = false;
                opening = false;
                lastError = reason;
                panel.setVisibility(View.GONE);
                reviewPanel.clear();
                Result done = result;
                result = null;
                if (report && done != null) done.error(reason);
              });
        });
  }

  private void retire(int token) {
    ui.post(
        () -> {
          if (alive(token)) cancel();
        });
  }

  private void showReview(int token, CameraJpeg.Frame frame) {
    reviewPanel.show(
        frame.bytes,
        frame.width,
        frame.height,
        profile.maxBytes,
        new PhotoReviewPanel.Actions() {
          public boolean current() {
            return SingleFrameCamera.this.current(token) && reviewClock.reviewing();
          }

          public int attempt() {
            return reviewClock.attempt();
          }

          public long remaining() {
            return reviewClock.remaining(reviewTask, SystemClock.elapsedRealtime());
          }

          public boolean retake() {
            if (!current() || !reviewClock.retake(reviewTask, SystemClock.elapsedRealtime()))
              return false;
            reviewPanel.clear();
            serial++;
            prepareAttempt();
            return true;
          }

          public void send(byte[] jpeg, int width, int height) {
            complete(token, jpeg, width, height);
          }

          public void cancel() {
            cancelAction.run();
          }

          public void error(String reason) {
            fail(token, reason);
          }
        });
  }

  private void complete(int token, byte[] jpeg, int width, int height) {
    if (!current(token)) {
      retire(token);
      return;
    }
    if (!reviewClock.finish(reviewTask, SystemClock.elapsedRealtime())) {
      fail(token, "camera_timeout");
      return;
    }
    wanted = false;
    opening = false;
    captures++;
    reviewPanel.clear();
    panel.setVisibility(View.GONE);
    Result done = result;
    result = null;
    if (done != null) done.image(jpeg, width, height);
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
    cameraOpen = false;
  }

  void cancel() {
    reviewClock.cancel();
    wanted = false;
    opening = false;
    serial++;
    result = null;
    reviewPanel.clear();
    panel.setVisibility(View.GONE);
    worker.post(this::release);
  }

  void destroy() {
    cancel();
    thread.quitSafely();
  }

  @Override
  public void onSurfaceTextureAvailable(SurfaceTexture texture, int w, int h) {
    surface = texture;
    openIfReady();
  }

  @Override
  public void onSurfaceTextureSizeChanged(SurfaceTexture texture, int w, int h) {
    if (texture != surface) return;
    fitPreview();
  }

  private void fitPreview() {
    int w = preview.getWidth(), h = preview.getHeight();
    if (w <= 0 || h <= 0 || previewWidth <= 0 || previewHeight <= 0) return;
    boolean turned = previewRotation == 90 || previewRotation == 270;
    float sourceW = turned ? previewHeight : previewWidth;
    float sourceH = turned ? previewWidth : previewHeight;
    float scale = Math.min(w / sourceW, h / sourceH);
    Matrix transform = new Matrix();
    transform.setScale(sourceW * scale / w, sourceH * scale / h, w / 2f, h / 2f);
    preview.setTransform(transform);
  }

  @Override
  public boolean onSurfaceTextureDestroyed(SurfaceTexture texture) {
    if (texture != surface) return true;
    surface = null;
    if (wanted) fail(serial, "preview_surface_lost");
    else worker.post(this::release);
    return true;
  }

  private void countdown(int token) {
    if (!current(token) || !delay.started()) return;
    int seconds = delay.secondsLeft(SystemClock.elapsedRealtime());
    label.setText((seconds > 0 ? "● 调整画面 · " + seconds + " 秒后拍摄" : "● 正在采集当前画面…") + qualityLabel);
    if (seconds > 0) ui.postDelayed(() -> countdown(token), 100);
  }

  @Override
  public void onSurfaceTextureUpdated(SurfaceTexture texture) {
    if (texture != surface) return;
    if (wanted
        && frameReceived
        && !delay.started()
        && (reviewClock.attempt() == 1 || texture.getTimestamp() != retakeVisibleBaseline)) {
      delay.visible(SystemClock.elapsedRealtime());
      countdown(serial);
    }
  }
}
