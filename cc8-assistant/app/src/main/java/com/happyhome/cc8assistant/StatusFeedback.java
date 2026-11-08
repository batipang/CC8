package com.happyhome.cc8assistant;

/** Remembers only status ownership and terminal failure, never request content. */
final class StatusFeedback {
  private int generation = -1;
  private boolean failed;
  private boolean cancelled;
  private boolean begun;
  private boolean transportInterrupted;

  synchronized void reset() {
    generation = -1;
    failed = false;
    cancelled = false;
    begun = false;
    transportInterrupted = false;
  }

  synchronized boolean begin(int incoming) {
    // The host's cancel(N) deliberately precedes begin(N). Admit that first
    // begin, but do not reopen a duplicate or a finished failed request.
    if (incoming < 0 || incoming < generation || (incoming == generation && (begun || failed)))
      return false;
    generation = incoming;
    failed = false;
    cancelled = false;
    begun = true;
    transportInterrupted = false;
    return true;
  }

  synchronized boolean accept(int incoming, String phase) {
    if (incoming < 0
        || incoming < generation
        || (transportInterrupted(incoming) && ("listening".equals(phase) || "idle".equals(phase))))
      return false;
    generation = incoming;
    failed = "error".equals(phase);
    cancelled = false;
    begun = true;
    transportInterrupted = false;
    return true;
  }

  synchronized boolean cancel(int incoming, boolean ownedRequest) {
    if (incoming < 0 || incoming <= generation) return false;
    generation = incoming;
    failed = false;
    cancelled = ownedRequest;
    begun = false;
    transportInterrupted = false;
    return true;
  }

  synchronized boolean transportInterruption(int incoming) {
    if (incoming < 0 || incoming < generation || (incoming == generation && begun)) return false;
    generation = incoming;
    failed = true;
    cancelled = false;
    begun = true;
    transportInterrupted = true;
    return true;
  }

  synchronized boolean transportInterrupted(int incoming) {
    return incoming == generation && transportInterrupted;
  }

  synchronized boolean cancelled(int incoming) {
    return incoming == generation && cancelled;
  }

  synchronized boolean unstarted(int incoming) {
    // The host can reject a request before text/announcement begin exists.
    // Only the exact cancellation successor may report this once.
    return incoming >= 0 && incoming == generation && !begun;
  }

  synchronized boolean current(int incoming) {
    return incoming == generation;
  }

  synchronized boolean failed(int incoming) {
    return incoming == generation && failed;
  }
}
