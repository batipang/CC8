package com.happyhome.cc8assistant;

/** Device-clock bound for one explicitly requested test capture; zero is normal use. */
final class CaptureWindow {
  static final long MIN_TEST_MS = 1000, MAX_TEST_MS = 1500000;
  final long limitMs;
  private final long deadline;

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
}
