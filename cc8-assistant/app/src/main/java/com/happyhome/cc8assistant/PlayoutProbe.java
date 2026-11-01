package com.happyhome.cc8assistant;

/** One track's first nonzero PCM crossed by its playback head, not acoustic onset. */
final class PlayoutProbe {
  private long accepted;
  private long firstSignal = -1;
  private boolean reported;

  static boolean requested(Object value) {
    return (value instanceof Integer || value instanceof Long) && ((Number) value).longValue() == 1;
  }

  boolean pending() {
    return !reported;
  }

  void accepted(short[] samples, int offset, int count) {
    if (offset < 0
        || count <= 0
        || offset > samples.length - count
        || accepted > Long.MAX_VALUE - count)
      throw new IllegalArgumentException("Invalid accepted PCM range");
    if (firstSignal < 0) {
      for (int i = 0; i < count; i++) {
        if (samples[offset + i] != 0) {
          firstSignal = accepted + i;
          break;
        }
      }
    }
    accepted += count;
  }

  Observation observe(long head, long nanos, boolean playing, boolean paused, boolean allowed) {
    if (reported
        || !playing
        || paused
        || !allowed
        || nanos < 0
        || firstSignal < 0
        || head <= firstSignal
        || head > accepted) return null;
    reported = true;
    return new Observation(firstSignal, head, accepted, nanos);
  }

  static final class Observation {
    final long firstSignal, played, accepted, nanos;

    Observation(long signal, long head, long written, long time) {
      firstSignal = signal;
      played = head;
      accepted = written;
      nanos = time;
    }
  }
}
