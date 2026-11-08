package com.happyhome.cc8assistant;

/** Presentation only. This state never enables capture or authorizes a request. */
final class ListeningState {
  private String confirmed = "unknown";
  private int generation = -1;

  static String hostValue(
      Object state,
      Object captureId,
      Object captureEpoch,
      String currentId,
      int epoch,
      boolean ready) {
    if (state == null) return "unknown"; // Older hosts have no explicit state.
    if (!(state instanceof String)
        || !ready
        || currentId == null
        || !currentId.equals(captureId)
        || !Integer.valueOf(epoch).equals(captureEpoch)) return null;
    return "waiting".equals(state) || "awake".equals(state) ? (String) state : "unknown";
  }

  void localPhase(String phase) {
    // Output and recoverable errors can temporarily cover the current capture.
    // Idle or a fresh calibration must not inherit the old capture's state.
    if ("idle".equals(phase) || "calibrating".equals(phase)) confirmed = "unknown";
  }

  boolean hostStatus(String phase, String state, boolean currentCapture, int generation) {
    if (this.generation != generation) {
      confirmed = "unknown";
      this.generation = generation;
    }
    localPhase(phase);
    if ("listening".equals(phase)) {
      // A UI callback can be queued before Stop/Home and run afterwards.
      if (!currentCapture) {
        confirmed = "unknown";
        return false;
      }
      confirmed = "waiting".equals(state) || "awake".equals(state) ? state : "unknown";
    }
    return true;
  }

  String state() {
    return confirmed;
  }

  String label() {
    if ("waiting".equals(confirmed)) return "等待唤醒 · 大黄蜂 / Bumblebee";
    if ("awake".equals(confirmed)) return "正在倾听 · 可以直接说";
    // Older hosts and local-only notifications cannot prove wake state.
    return "正在倾听 · 大黄蜂 / Bumblebee";
  }
}
