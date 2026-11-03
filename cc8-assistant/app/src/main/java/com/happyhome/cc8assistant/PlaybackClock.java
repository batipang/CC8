package com.happyhome.cc8assistant;

/** Presentation estimate for a not-yet-written block, never a stale past time. */
final class PlaybackClock {
  // 估计待写片段何时发声；HAL 时间戳冻结时回退到当前播放头，防止 AEC 参考错位。
  private static final long FRAME_NANOS = 1000000000L / 16000;
  static final int HARDWARE = 0, CLAMPED = 1, MISSING = 2, INVALID = 3, OUTSIDE = 4;

  static int source(long now, long written, boolean valid, long position, long timestamp) {
    if (!valid) return MISSING;
    if (timestamp <= 0 || position < 0 || position > written) return INVALID;
    long predicted = timestamp + (written - position) * FRAME_NANOS;
    if (predicted < now - 20000000L || predicted > now + 1000000000L) return OUTSIDE;
    return predicted < now ? CLAMPED : HARDWARE;
  }

  static long estimate(
      long now, long written, long head, boolean valid, long position, long timestamp) {
    long fallback = now + Math.max(0, written - head) * FRAME_NANOS;
    int source = source(now, written, valid, position, timestamp);
    if (source != HARDWARE && source != CLAMPED) return fallback;
    long predicted = timestamp + (written - position) * FRAME_NANOS;
    // Legacy HAL timestamps can freeze when the track underruns. Newly
    // supplied samples cannot have played hundreds of milliseconds ago.
    return Math.max(now, predicted);
  }
}
