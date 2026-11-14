package com.happyhome.cc8assistant;

/** No images: one absolute deadline, distinct task/attempt ownership, at most three shots. */
final class PhotoReviewClock {
  static final long REVIEW_MS = 70000, ATTEMPT_MS = 11000;
  private long task, deadline;
  private int attempt;
  private boolean active, review, ready;

  synchronized long begin(long now, boolean allowReview) {
    long duration = allowReview ? REVIEW_MS : ATTEMPT_MS;
    if (now < 0 || now > Long.MAX_VALUE - duration || task == Long.MAX_VALUE)
      throw new IllegalArgumentException("Invalid review clock");
    task++;
    deadline = now + duration;
    attempt = 1;
    active = true;
    review = allowReview;
    ready = false;
    return task;
  }

  synchronized boolean current(long token, long now) {
    return active && token == task && now >= 0 && now < deadline;
  }

  synchronized boolean captured(long token, int shot, long now) {
    if (!current(token, now) || shot != attempt || ready) return false;
    ready = true;
    return true;
  }

  synchronized boolean retake(long token, long now) {
    if (!current(token, now) || !review || !ready || attempt >= 3 || deadline - now < ATTEMPT_MS)
      return false;
    attempt++;
    ready = false;
    return true;
  }

  synchronized boolean finish(long token, long now) {
    if (!current(token, now) || !ready) return false;
    active = false;
    return true;
  }

  synchronized boolean hasFrame() {
    return active && ready;
  }

  synchronized boolean reviewing() {
    return active && review && ready;
  }

  synchronized int attempt() {
    return attempt;
  }

  synchronized long remaining(long token, long now) {
    return current(token, now) ? deadline - now : 0;
  }

  synchronized boolean sameTask(long token) {
    return task == token;
  }

  synchronized void cancel() {
    active = false;
    ready = false;
  }
}
