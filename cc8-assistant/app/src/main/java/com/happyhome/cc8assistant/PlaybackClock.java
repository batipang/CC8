package com.happyhome.cc8assistant;

/** Presentation estimate for a not-yet-written block, never a stale past time. */
final class PlaybackClock {
  // 估计待写片段何时发声；HAL 时间戳冻结时回退到当前播放头，防止 AEC 参考错位。
  private static final long FRAME_NANOS = 1000000000L / 16000;

  static long estimate(
      long now, long written, long head, boolean valid, long position, long timestamp) {
    long fallback = now + Math.max(0, written - head) * FRAME_NANOS;
    if (!valid || timestamp <= 0 || position < 0 || position > written) return fallback;
    long predicted = timestamp + (written - position) * FRAME_NANOS;
    // Legacy HAL timestamps can freeze when the track underruns. Newly
    // supplied samples cannot have played hundreds of milliseconds ago.
    if (predicted < now - 20000000L || predicted > now + 1000000000L) return fallback;
    return Math.max(now, predicted);
  }
}
