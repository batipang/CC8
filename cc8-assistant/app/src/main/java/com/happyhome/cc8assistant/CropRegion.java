package com.happyhome.cc8assistant;

/** Upright JPEG pixels mapped from a fit-center view; letterboxing is never image content. */
final class CropRegion {
  final int left, top, right, bottom;

  private CropRegion(int l, int t, int r, int b) {
    left = l;
    top = t;
    right = r;
    bottom = b;
  }

  int width() {
    return right - left;
  }

  int height() {
    return bottom - top;
  }

  static CropRegion whole(int width, int height) {
    if (width < 1
        || height < 1
        || Math.max(width, height) > 1280
        || (long) width * height > 1280 * 960) return null;
    return new CropRegion(0, 0, width, height);
  }

  static CropRegion fromDrag(
      int viewW, int viewH, int imageW, int imageH, float x0, float y0, float x1, float y1) {
    if (viewW <= 0 || viewH <= 0 || whole(imageW, imageH) == null) return null;
    for (float coordinate : new float[] {x0, y0, x1, y1})
      if (Float.isNaN(coordinate) || Float.isInfinite(coordinate)) return null;
    double scale = Math.min((double) viewW / imageW, (double) viewH / imageH);
    double offsetX = (viewW - imageW * scale) / 2, offsetY = (viewH - imageH * scale) / 2;
    int left = clamp((int) Math.floor((Math.min(x0, x1) - offsetX) / scale), imageW);
    int top = clamp((int) Math.floor((Math.min(y0, y1) - offsetY) / scale), imageH);
    int right = clamp((int) Math.ceil((Math.max(x0, x1) - offsetX) / scale), imageW);
    int bottom = clamp((int) Math.ceil((Math.max(y0, y1) - offsetY) / scale), imageH);
    return right - left >= 8 && bottom - top >= 8 ? new CropRegion(left, top, right, bottom) : null;
  }

  private static int clamp(int value, int bound) {
    return Math.max(0, Math.min(bound, value));
  }
}
