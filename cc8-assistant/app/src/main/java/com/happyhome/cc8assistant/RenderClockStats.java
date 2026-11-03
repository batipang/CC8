package com.happyhome.cc8assistant;

/** Constant-memory, opt-in provenance for the existing presentation estimate. No PCM. */
final class RenderClockStats {
  static final String[] SOURCES = {"hardware", "clamped", "missing", "invalid", "outside"};
  final long[] counts = new long[SOURCES.length];
  final long started;
  long observations, frames, sourceChanges, clockSteps, maxStepNanos;
  boolean expired, invalid;
  private long lastNanos, lastCount;
  private int lastSource = -1;

  RenderClockStats(long started) {
    this.started = started;
  }

  void observe(long now, long frame, int count, long nanos, int source) {
    // A bad optional diagnostic must never change playback or its timestamps.
    if (expired || invalid) return;
    if (now < started || now - started >= 20000) {
      expired = true;
      return;
    }
    if (frame != frames
        || count <= 0
        || count > 32768
        || nanos <= 0
        || source < 0
        || source >= counts.length) {
      invalid = true;
      return;
    }
    if (observations > 0) {
      long expected = lastNanos + lastCount * 62500L;
      if (expected < lastNanos) {
        invalid = true;
        return;
      }
      long step = Math.abs(nanos - expected);
      maxStepNanos = Math.max(maxStepNanos, step);
      if (step > 40000000L) clockSteps++;
      if (lastSource != source) sourceChanges++;
    }
    observations++;
    frames += count;
    counts[source]++;
    lastNanos = nanos;
    lastCount = count;
    lastSource = source;
  }

  boolean complete() {
    return observations > 0 && !expired && !invalid;
  }
}
