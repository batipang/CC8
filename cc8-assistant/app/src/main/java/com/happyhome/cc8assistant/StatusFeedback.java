package com.happyhome.cc8assistant;

/** Remembers only status ownership and terminal failure, never request content. */
final class StatusFeedback {
  private int generation = -1;
  private boolean failed;
  private boolean cancelled;
  private boolean begun;

  synchronized void reset() {
    generation = -1;
    failed = false;
    cancelled = false;
    begun = false;
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
    return true;
  }

  synchronized boolean accept(int incoming, String phase) {
    if (incoming < 0 || incoming < generation) return false;
    generation = incoming;
    failed = "error".equals(phase);
    cancelled = false;
    begun = true;
    return true;
  }

  synchronized boolean cancel(int incoming, boolean ownedRequest) {
    if (incoming < 0 || incoming <= generation) return false;
    generation = incoming;
    failed = false;
    cancelled = ownedRequest;
    begun = false;
    return true;
  }

  synchronized boolean cancelled(int incoming) {
    return incoming == generation && cancelled;
  }

  synchronized boolean current(int incoming) {
    return incoming == generation;
  }

  synchronized boolean failed(int incoming) {
    return incoming == generation && failed;
  }
}
