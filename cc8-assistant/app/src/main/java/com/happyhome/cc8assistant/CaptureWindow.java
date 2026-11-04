package com.happyhome.cc8assistant;

import java.text.Normalizer;
import java.util.regex.Pattern;

/** Device-clock bound for one explicitly requested test capture; zero is normal use. */
final class CaptureWindow {
  static final long MIN_TEST_MS = 1000, MAX_TEST_MS = 1500000;
  final long limitMs;
  private final long deadline;
  final CaptureClockStats clockStats;
  private TranscriptEvidence transcript;
  private static final Pattern INTERRUPTION_QUERY =
      Pattern.compile(
          "(?:换个问题\\s*[,，]?\\s*)?(?:三|3)\\s*(?:加上?|\\+)\\s*(?:五|5)\\s*(?:等于|=)\\s*(?:几|多少)[\\s?。.!！]*");

  static final class TranscriptEvidence {
    final long route;
    final int generation;
    final boolean matched;

    TranscriptEvidence(long route, int generation, boolean matched) {
      this.route = route;
      this.generation = generation;
      this.matched = matched;
    }
  }

  static long requested(boolean supplied, Object value) {
    if (!supplied) return 0;
    if (!(value instanceof Long) && !(value instanceof Integer)) return -1;
    long limit = ((Number) value).longValue();
    return limit >= MIN_TEST_MS && limit <= MAX_TEST_MS ? limit : -1;
  }

  CaptureWindow(long limit, long now) {
    if (now < 0 || (limit != 0 && (limit < MIN_TEST_MS || limit > MAX_TEST_MS)))
      throw new IllegalArgumentException("Invalid capture window");
    limitMs = limit;
    clockStats = limit == 0 ? null : new CaptureClockStats();
    deadline = limit == 0 ? 0 : now + limit;
    if (limit != 0 && deadline < now)
      throw new IllegalArgumentException("Capture deadline overflow");
  }

  boolean allows(long now) {
    return limitMs == 0 || now < deadline;
  }

  long remaining(long now) {
    return limitMs == 0 ? 0 : Math.max(0, deadline - now);
  }

  synchronized void observeTranscript(long route, int generation, String text, long now) {
    if (limitMs == 0 || !allows(now) || route < 0 || generation < 0) return;
    if (transcript != null
        && (route < transcript.route
            || route == transcript.route && generation <= transcript.generation)) return;
    // Check only the fixed public probe. Keep no recognized text or text digest.
    boolean matched =
        text.length() <= 80
            && INTERRUPTION_QUERY
                .matcher(Normalizer.normalize(text, Normalizer.Form.NFKC).trim())
                .matches();
    transcript = new TranscriptEvidence(route, generation, matched);
  }

  synchronized TranscriptEvidence transcriptEvidence(long route, long now) {
    return allows(now) && transcript != null && transcript.route == route ? transcript : null;
  }
}
