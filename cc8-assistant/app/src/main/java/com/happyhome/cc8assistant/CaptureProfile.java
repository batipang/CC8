package com.happyhome.cc8assistant;

import org.json.JSONObject;

/** A fixed host grant for this exact capture. Never widen limits from a UI preference. */
final class CaptureProfile {
  final int version, maxEdge, maxPixels, maxBytes;
  final boolean reviewAllowed, followupAllowed;

  private CaptureProfile(int version, int edge, int pixels, int bytes) {
    this(version, edge, pixels, bytes, false);
  }

  private CaptureProfile(int version, int edge, int pixels, int bytes, boolean review) {
    this(version, edge, pixels, bytes, review, false);
  }

  private CaptureProfile(
      int version, int edge, int pixels, int bytes, boolean review, boolean followup) {
    this.version = version;
    maxEdge = edge;
    maxPixels = pixels;
    maxBytes = bytes;
    reviewAllowed = review;
    followupAllowed = followup;
  }

  boolean acceptsFrame(int width, int height, int bytes) {
    return width > 0
        && height > 0
        && width % 2 == 0
        && height % 2 == 0
        && Math.max(width, height) <= maxEdge
        && (long) width * height <= maxPixels
        && bytes == (long) width * height * 3 / 2;
  }

  static CaptureProfile legacy() {
    return new CaptureProfile(1, 640, 640 * 480, 384 * 1024);
  }

  static CaptureProfile fromRequest(JSONObject request) {
    Object raw = request.opt("capture_version");
    if (raw == null) return legacy();
    if (!(raw instanceof Integer) || ((Integer) raw != 1 && (Integer) raw != 2))
      throw new IllegalArgumentException("Unsupported capture policy");
    CaptureProfile profile =
        (Integer) raw == 2 ? new CaptureProfile(2, 1280, 1280 * 960, 768 * 1024) : legacy();
    if (!Integer.valueOf(profile.maxEdge).equals(request.opt("max_image_edge"))
        || !Integer.valueOf(profile.maxPixels).equals(request.opt("max_image_pixels"))
        || !Integer.valueOf(profile.maxBytes).equals(request.opt("max_image_bytes")))
      throw new IllegalArgumentException("Invalid capture limits");
    Object review = request.opt("review_allowed"), window = request.opt("review_window_ms");
    if ((review != null || window != null)
        && (!(review instanceof Boolean)
            || !Integer.valueOf(Boolean.TRUE.equals(review) ? 70000 : 0).equals(window)))
      throw new IllegalArgumentException("Invalid review grant");
    Object followup = request.opt("followup_allowed");
    if (followup != null && !(followup instanceof Boolean))
      throw new IllegalArgumentException("Invalid photo retention grant");
    return new CaptureProfile(
        profile.version,
        profile.maxEdge,
        profile.maxPixels,
        profile.maxBytes,
        Boolean.TRUE.equals(review),
        Boolean.TRUE.equals(followup));
  }
}
