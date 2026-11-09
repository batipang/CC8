package com.happyhome.cc8assistant;

import android.app.Activity;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.widget.*;
import java.io.ByteArrayOutputStream;

/** A bounded, RAM-only review. Sensor ownership remains with SingleFrameCamera. */
final class PhotoReviewPanel {
  interface Actions {
    boolean current();

    int attempt();

    long remaining();

    boolean retake();

    void send(byte[] jpeg, int width, int height);

    void cancel();

    void error(String reason);
  }

  private final Activity activity;
  private final FrameLayout root;
  private final PhotoCropView image;
  private final TextView header;
  private final Button send, retake, crop, undo, cancel;
  private final Handler worker, ui = new Handler(Looper.getMainLooper());
  private Bitmap bitmap;
  private byte[] original, jpeg;
  private int width, height, originalWidth, originalHeight, maxBytes, serial;
  private boolean active, busy;
  private Actions actions;

  PhotoReviewPanel(Activity activity, FrameLayout stage, Handler worker) {
    this.activity = activity;
    this.worker = worker;
    root = new FrameLayout(activity);
    root.setBackgroundColor(0xff061521);
    root.setVisibility(View.GONE);
    header = new TextView(activity);
    header.setTextSize(22);
    header.setTextColor(0xffffc36a);
    header.setPadding(dp(20), dp(10), dp(20), dp(8));
    root.addView(header, new FrameLayout.LayoutParams(-1, dp(96), Gravity.TOP));
    image = new PhotoCropView(activity);
    FrameLayout.LayoutParams picture = new FrameLayout.LayoutParams(-1, -1);
    picture.topMargin = dp(96);
    picture.bottomMargin = dp(82);
    root.addView(image, picture);
    LinearLayout buttons = new LinearLayout(activity);
    send = button(buttons, "发送分析");
    retake = button(buttons, "重拍");
    crop = button(buttons, "裁剪");
    undo = button(buttons, "还原整图");
    cancel = button(buttons, "取消");
    root.addView(buttons, new FrameLayout.LayoutParams(-1, dp(78), Gravity.BOTTOM));
    FrameLayout.LayoutParams bounds = new FrameLayout.LayoutParams(-1, -1);
    bounds.setMargins(dp(24), dp(24), dp(24), dp(24));
    stage.addView(root, bounds);
    send.setOnClickListener(
        v -> {
          if (!usable() || busy || image.cropping()) return;
          Actions owner = actions;
          byte[] frame = jpeg;
          int w = width, h = height;
          clear();
          owner.send(frame, w, h);
        });
    retake.setOnClickListener(
        v -> {
          if (!usable() || busy || image.cropping()) return;
          if (!actions.retake()) toast("最多拍三张，且需要留出重新取景时间。");
        });
    crop.setOnClickListener(
        v -> {
          if (!usable() || busy) return;
          if (!image.cropping()) {
            image.cropping(true);
            controls();
            return;
          }
          CropRegion selected = image.region();
          if (selected == null) {
            toast("请在图片里拖出至少 8×8 像素的区域。");
            return;
          }
          crop(selected);
        });
    undo.setOnClickListener(
        v -> {
          if (!usable() || busy) return;
          if (image.cropping()) {
            image.cropping(false);
            controls();
          } else load(original, originalWidth, originalHeight);
        });
    cancel.setOnClickListener(
        v -> {
          if (usable()) actions.cancel();
        });
  }

  private int dp(float n) {
    return Math.round(n * activity.getResources().getDisplayMetrics().density);
  }

  private Button button(LinearLayout row, String text) {
    Button value = new Button(activity);
    value.setText(text);
    value.setTextSize(22);
    row.addView(value, new LinearLayout.LayoutParams(0, -1, 1));
    return value;
  }

  void show(byte[] frame, int w, int h, int limit, Actions owner) {
    clear();
    actions = owner;
    active = true;
    maxBytes = limit;
    original = frame;
    originalWidth = w;
    originalHeight = h;
    root.setVisibility(View.VISIBLE);
    load(frame, w, h);
    final int token = serial;
    tick(token);
  }

  private boolean owns(int token) {
    return active && serial == token && actions != null && actions.current();
  }

  private boolean usable() {
    if (owns(serial)) return true;
    clear();
    return false;
  }

  private void tick(int token) {
    if (!owns(token)) {
      if (active && serial == token) clear();
      return;
    }
    // Updating accessibility text every frame prevents idle interaction and wastes redraws.
    // Round the visible estimate; the independent absolute deadline remains exact.
    String text =
        "摄像头已关闭 · 第 "
            + actions.attempt()
            + " 张 · "
            + width
            + "×"
            + height
            + " · 约 "
            + (((actions.remaining() + 1999) / 2000) * 2)
            + " 秒内取消\n"
            + (busy
                ? "正在处理本张图片…"
                : image.cropping() ? "拖动选择区域，再点应用裁剪。" : "核对后发送给 Dot；未发送的图片只在本机内存中。");
    if (!text.contentEquals(header.getText())) header.setText(text);
    ui.postDelayed(() -> tick(token), 250);
  }

  private void controls() {
    boolean enabled = active && !busy && bitmap != null;
    send.setEnabled(enabled && !image.cropping());
    retake.setEnabled(
        enabled
            && !image.cropping()
            && actions != null
            && actions.attempt() < 3
            && actions.remaining() >= PhotoReviewClock.ATTEMPT_MS);
    crop.setEnabled(enabled);
    crop.setText(image.cropping() ? "应用裁剪" : "裁剪");
    undo.setEnabled(enabled && (image.cropping() || jpeg != original));
    undo.setText(image.cropping() ? "退出裁剪" : "还原整图");
    cancel.setEnabled(active);
  }

  private void load(byte[] frame, int w, int h) {
    if (!usable()) return;
    final int token = serial;
    final int limit = maxBytes;
    busy = true;
    image.image(null);
    if (bitmap != null) bitmap.recycle();
    bitmap = null;
    jpeg = null;
    width = w;
    height = h;
    controls();
    worker.post(
        () -> {
          Bitmap decoded = null;
          try {
            if (frame == null || frame.length > limit || CropRegion.whole(w, h) == null)
              throw new IllegalArgumentException();
            BitmapFactory.Options bounds = new BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(frame, 0, frame.length, bounds);
            if (bounds.outWidth != w || bounds.outHeight != h) throw new IllegalArgumentException();
            BitmapFactory.Options options = new BitmapFactory.Options();
            options.inPreferredConfig = Bitmap.Config.ARGB_8888;
            decoded = BitmapFactory.decodeByteArray(frame, 0, frame.length, options);
            if (decoded == null) throw new IllegalArgumentException();
            deliver(token, frame, decoded);
          } catch (Exception | OutOfMemoryError error) {
            if (decoded != null) decoded.recycle();
            failed(token);
          }
        });
  }

  private void crop(CropRegion region) {
    final int token = serial;
    final Bitmap source = bitmap;
    final int limit = maxBytes;
    // Transfer ownership to the worker before any bitmap operation. Home may
    // now clear the panel without recycling pixels being read by that worker.
    bitmap = null;
    image.image(null);
    jpeg = null;
    busy = true;
    controls();
    worker.post(
        () -> {
          Bitmap cropped = null;
          try {
            cropped =
                Bitmap.createBitmap(
                    source, region.left, region.top, region.width(), region.height());
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] result = null;
            for (int quality : new int[] {82, 72, 62, 52}) {
              out.reset();
              if (!cropped.compress(Bitmap.CompressFormat.JPEG, quality, out))
                throw new IllegalArgumentException();
              if (out.size() <= limit) {
                result = out.toByteArray();
                break;
              }
            }
            if (result == null) throw new IllegalArgumentException();
            if (cropped != source) source.recycle();
            deliver(token, result, cropped);
          } catch (Exception | OutOfMemoryError error) {
            if (cropped != null && cropped != source) cropped.recycle();
            source.recycle();
            failed(token);
          }
        });
  }

  private void deliver(int token, byte[] frame, Bitmap decoded) {
    ui.post(
        () -> {
          if (!owns(token)) {
            decoded.recycle();
            return;
          }
          bitmap = decoded;
          jpeg = frame;
          width = decoded.getWidth();
          height = decoded.getHeight();
          busy = false;
          image.image(decoded);
          controls();
        });
  }

  private void failed(int token) {
    ui.post(
        () -> {
          if (owns(token)) {
            Actions owner = actions;
            clear();
            owner.error("image_encode_failed");
          }
        });
  }

  void clear() {
    active = false;
    serial++;
    image.image(null);
    if (bitmap != null) bitmap.recycle();
    bitmap = null;
    jpeg = original = null;
    actions = null;
    busy = false;
    root.setVisibility(View.GONE);
  }

  private void toast(String text) {
    Toast.makeText(activity, text, Toast.LENGTH_LONG).show();
  }
}
