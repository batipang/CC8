package com.happyhome.cc8assistant;

import java.util.LinkedHashMap;
import java.util.Map;

/** In-memory clock evidence from a single bounded 16 kHz test recording. */
final class CaptureClockStats {
  private static final long FRAME_NS = 62500;
  private static final long INTERVAL_NS = 250000000;
  private static final long MAX_SKEW_NS = 2000000000;
  private static final long MAX_FRAME_SKEW = MAX_SKEW_NS / FRAME_NS;
  private static final long MAX_FRAMES = CaptureWindow.MAX_TEST_MS * 16 + MAX_FRAME_SKEW;
  private static final int MAX_SAMPLES = 6100;

  private int samples, available, unavailable, invalid, duplicate, discontinuities;
  private long lastSampleNs, lastFrame, lastStampNs, minOffsetNs, maxOffsetNs;

  // Called by the sole recorder worker. A clock read is never made on the UI thread.
  synchronized boolean due(long now) {
    if (samples == MAX_SAMPLES || (samples > 0 && now - lastSampleNs < INTERVAL_NS)) return false;
    lastSampleNs = now;
    return true;
  }

  synchronized void observe(
      boolean success, long frame, long stampNs, long observedNs, long endFrame, long originNs) {
    samples++;
    if (!success) {
      unavailable++;
      return;
    }
    if (frame < 0
        || frame > MAX_FRAMES
        || endFrame <= 0
        || endFrame > MAX_FRAMES
        || Math.abs(frame - endFrame) > MAX_FRAME_SKEW
        || originNs <= 0
        || stampNs <= 0
        || stampNs > observedNs
        || observedNs - stampNs > MAX_SKEW_NS) {
      invalid++;
      return;
    }
    long elapsedNs = stampNs - originNs;
    long frameNs = frame * FRAME_NS;
    if (elapsedNs < frameNs - MAX_SKEW_NS || elapsedNs > frameNs + MAX_SKEW_NS) {
      invalid++;
      return;
    }
    if (available > 0) {
      if (frame == lastFrame && stampNs == lastStampNs) {
        duplicate++;
        return;
      }
      if (frame <= lastFrame || stampNs <= lastStampNs) {
        discontinuities++;
        return;
      }
    }
    long offsetNs = elapsedNs - frameNs;
    minOffsetNs = available == 0 ? offsetNs : Math.min(minOffsetNs, offsetNs);
    maxOffsetNs = available == 0 ? offsetNs : Math.max(maxOffsetNs, offsetNs);
    available++;
    lastFrame = frame;
    lastStampNs = stampNs;
  }

  synchronized Map<String, Number> snapshot() {
    Map<String, Number> values = new LinkedHashMap<>();
    values.put("samples", samples);
    values.put("available", available);
    values.put("unavailable", unavailable);
    values.put("invalid", invalid);
    values.put("duplicate", duplicate);
    values.put("discontinuities", discontinuities);
    if (available > 0) {
      values.put("min_offset_ns", minOffsetNs);
      values.put("max_offset_ns", maxOffsetNs);
    }
    return values;
  }
}
