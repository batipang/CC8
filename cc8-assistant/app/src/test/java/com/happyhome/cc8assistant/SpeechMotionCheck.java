package com.happyhome.cc8assistant;

/** Deterministic presentation checks without a device, audio output or cloud. */
public final class SpeechMotionCheck {
  private static final Object OWNER = new Object();

  private static void yes(boolean value, String reason) {
    if (!value) throw new AssertionError(reason);
  }

  private static void closed(SpeechMotion motion, String reason) {
    yes(motion.opening == 0 && motion.spread == 0, reason);
  }

  private static void frame(SpeechMotion motion, int id, long ms, float rms, float detail) {
    motion.update(OWNER, id, ms, rms, detail, true, false);
  }

  private static void smoothing() {
    SpeechMotion motion = new SpeechMotion();
    frame(motion, 1, 10, .01f, .1f);
    float initial = motion.opening;
    frame(motion, 1, 26, .05f, .9f);
    yes(
        motion.opening > initial && motion.opening < (.05f - .003f) * 18,
        "attack did not follow played time");
    yes(motion.spread > .1f && motion.spread < .9f, "shape jumped");
    float peak = motion.opening;
    frame(motion, 1, 42, .005f, .1f);
    yes(
        motion.opening > (.005f - .003f) * 18 && motion.opening < peak,
        "release did not smooth active speech");
    frame(motion, 1, 58, 0, 1);
    closed(motion, "silence must close immediately");
  }

  private static void stopped() {
    for (int kind = 0; kind < 4; kind++) {
      SpeechMotion motion = new SpeechMotion();
      frame(motion, 1, 100, .08f, .7f);
      motion.update(OWNER, kind == 0 ? 0 : 1, 120, .08f, .7f, kind != 1, kind == 2);
      if (kind == 3) motion.clear();
      closed(motion, "stop/pause/empty/Home kept mouth open");
      frame(motion, 2, 5, .01f, 0);
      yes(
          motion.opening == (.01f - .003f) * 18 && motion.spread == 0,
          "new owner inherited old pose");
    }
  }

  private static void frozen() {
    SpeechMotion motion = new SpeechMotion();
    frame(motion, 1, 100, .01f, .1f);
    float open = motion.opening, wide = motion.spread;
    for (int i = 0; i < 100; i++) frame(motion, 1, 100, .1f, 1);
    yes(
        motion.opening == open && motion.spread == wide,
        "UI refresh animated samples that have not advanced");
    frame(motion, 1, 90, .1f, 1);
    closed(motion, "regressed clock opened future speech");
    frame(motion, 1, 95, .1f, 1);
    closed(motion, "repeated regression reopened pose");
    frame(motion, 1, 101, .03f, .4f);
    yes(motion.opening > 0, "recovered physical clock stayed closed");
    motion.update(new Object(), 1, 1, .01f, 0, true, false);
    yes(
        motion.opening == (.01f - .003f) * 18 && motion.spread == 0,
        "reused server ID inherited an obsolete local player");
  }

  private static void invalid() {
    for (float invalid :
        new float[] {Float.NaN, Float.POSITIVE_INFINITY, Float.NEGATIVE_INFINITY}) {
      SpeechMotion motion = new SpeechMotion();
      frame(motion, 1, 1, .05f, .5f);
      frame(motion, 1, 10, invalid, .5f);
      closed(motion, "invalid energy admitted");
      frame(motion, 1, 1, .05f, .5f);
      frame(motion, 1, 10, .05f, invalid);
      closed(motion, "invalid shape admitted");
    }
    SpeechMotion motion = new SpeechMotion();
    for (int id : new int[] {0, -1, Integer.MIN_VALUE}) {
      frame(motion, id, 10, .2f, .2f);
      closed(motion, "invalid owner admitted");
    }
    frame(motion, 1, -1, .2f, .2f);
    closed(motion, "negative hardware position admitted");
    motion.update(null, 1, 1, .2f, .2f, true, false);
    closed(motion, "missing local playback owner admitted");
  }

  private static void bounded() {
    SpeechMotion motion = new SpeechMotion();
    long state = 731;
    for (int i = 1; i <= 200000; i++) {
      state = (state * 1664525 + 1013904223) & 0xffffffffL;
      float signal = (state % 32769) / 32768f;
      frame(motion, 1 + i / 1000, i * 33L, signal, (state % 400) / 100f - 1);
      yes(
          Float.isFinite(motion.opening)
              && Float.isFinite(motion.spread)
              && motion.opening >= 0
              && motion.opening <= 1
              && motion.spread >= 0
              && motion.spread <= 1,
          "pose escaped bounded native geometry");
    }
    frame(motion, 8, Long.MAX_VALUE - 2, Float.MAX_VALUE, -10);
    frame(motion, 8, Long.MAX_VALUE, Float.MAX_VALUE, Float.MAX_VALUE);
    yes(motion.opening == 1 && motion.spread >= 0 && motion.spread <= 1, "long session overflow");
    motion.clear();
    closed(motion, "bounded run did not clear");
  }

  public static void main(String[] args) {
    switch (args[0]) {
      case "smoothing":
        smoothing();
        break;
      case "stopped":
        stopped();
        break;
      case "frozen":
        frozen();
        break;
      case "invalid":
        invalid();
        break;
      case "bounded":
        bounded();
        break;
      default:
        throw new AssertionError("Unknown case");
    }
    System.out.println(args[0] + " passed");
  }
}
