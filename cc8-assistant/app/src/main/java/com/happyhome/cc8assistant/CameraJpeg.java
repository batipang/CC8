package com.happyhome.cc8assistant;

import android.graphics.*;
import java.io.ByteArrayOutputStream;

/** Bounded camera pixels; temporary rotation bitmaps always have one owner. */
final class CameraJpeg {
  static final class Frame {
    final byte[] bytes;
    final int width, height;

    Frame(byte[] value, int w, int h) {
      bytes = value;
      width = w;
      height = h;
    }
  }

  static Frame encode(byte[] data, int width, int height, int rotation, CaptureProfile limits) {
    if (limits == null
        || data == null
        || !limits.acceptsFrame(width, height, data.length)
        || (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270))
      throw new IllegalArgumentException("Unsupported frame");
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    YuvImage source = new YuvImage(data, ImageFormat.NV21, width, height, null);
    int[] qualities = limits.version == 2 ? new int[] {82, 72, 62, 52} : new int[] {82};
    if (rotation == 0) {
      for (int quality : qualities) {
        out.reset();
        if (!source.compressToJpeg(new Rect(0, 0, width, height), quality, out))
          throw new IllegalArgumentException("JPEG encode failed");
        if (out.size() <= limits.maxBytes) return new Frame(out.toByteArray(), width, height);
      }
      throw new IllegalArgumentException("JPEG exceeds grant");
    }
    if (!source.compressToJpeg(new Rect(0, 0, width, height), 82, out))
      throw new IllegalArgumentException("JPEG encode failed");
    byte[] raw = out.toByteArray();
    Bitmap decoded = null, upright = null;
    try {
      decoded = BitmapFactory.decodeByteArray(raw, 0, raw.length);
      if (decoded == null) throw new IllegalArgumentException("JPEG decode failed");
      Matrix transform = new Matrix();
      transform.postRotate(rotation);
      upright = Bitmap.createBitmap(decoded, 0, 0, width, height, transform, true);
      for (int quality : qualities) {
        out.reset();
        if (!upright.compress(Bitmap.CompressFormat.JPEG, quality, out))
          throw new IllegalArgumentException("JPEG encode failed");
        if (out.size() <= limits.maxBytes)
          return new Frame(out.toByteArray(), upright.getWidth(), upright.getHeight());
      }
      throw new IllegalArgumentException("JPEG exceeds grant");
    } finally {
      if (upright != null && upright != decoded) upright.recycle();
      if (decoded != null) decoded.recycle();
    }
  }
}
