package com.happyhome.cc8assistant;

/** Small playback-clock-driven pose, approximating articulation without phoneme labels. */
final class SpeechMotion {
  // UI-thread state only. No timers, allocations, bitmap changes or audio input.
  private int playbackId;
  private Object owner;
  private long playedMs;
  float opening, spread;

  void clear() {
    playbackId = 0;
    owner = null;
    playedMs = 0;
    opening = spread = 0;
  }

  void update(
      Object source,
      int id,
      long positionMs,
      float level,
      float detail,
      boolean playing,
      boolean paused) {
    // A stopped/paused owner or real silence closes on this same visual frame.
    // Never ease an old clause's mouth across a pause, route loss or replacement.
    if (source == null
        || id <= 0
        || positionMs < 0
        || !playing
        || paused
        || !finite(level)
        || level <= .003f
        || !finite(detail)) {
      clear();
      return;
    }
    float target = clamp((level - .003f) * 18);
    float width = clamp(detail);
    if (source != owner || id != playbackId) {
      owner = source;
      playbackId = id;
      playedMs = positionMs;
      opening = target;
      spread = width;
      return;
    }
    if (positionMs < playedMs) {
      // A regressed hardware clock must not animate future samples as speech.
      opening = spread = 0;
      return;
    }
    long delta = positionMs - playedMs;
    if (delta == 0) return; // UI refresh alone cannot advance speech motion.
    playedMs = positionMs;
    float fraction = Math.min(1, delta / (target > opening ? 36f : 64f));
    opening += (target - opening) * fraction;
    spread += (width - spread) * Math.min(1, delta / 80f);
  }

  private static boolean finite(float value) {
    return !Float.isNaN(value) && !Float.isInfinite(value);
  }

  private static float clamp(float value) {
    return Math.max(0, Math.min(1, value));
  }
}
