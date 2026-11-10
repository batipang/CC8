package com.happyhome.cc8assistant;

/** One short foreground camera grant, bound to the acknowledged workflow generation. */
final class WorkflowConsent {
  private String nonce = "";
  private long route = -1, expires;
  private int generation = -1;

  synchronized boolean offer(String value, long owner, long now) {
    if (value == null
        || !value.matches("[a-f0-9]{32}")
        || owner < 0
        || now < 0
        || now > Long.MAX_VALUE - 15000) return false;
    nonce = value;
    route = owner;
    expires = now + 15000;
    generation = -1;
    return true;
  }

  private boolean fresh(String value, long owner, long now) {
    return !nonce.isEmpty()
        && nonce.equals(value)
        && route == owner
        && now >= expires - 15000
        && now < expires;
  }

  synchronized boolean bind(String value, long owner, int gen, long now) {
    if (gen < 0 || !fresh(value, owner, now)) return false;
    if (generation >= 0) return generation == gen;
    generation = gen;
    return true;
  }

  synchronized boolean accepts(String value, long owner, int gen, long now) {
    return generation >= 0 && generation == gen && fresh(value, owner, now);
  }

  synchronized boolean consume(String value, long owner, int gen, long now) {
    if (!accepts(value, owner, gen, now)) return false;
    clear();
    return true;
  }

  synchronized String cancel(int gen) {
    return generation >= 0 && gen > generation ? clear() : "";
  }

  synchronized String clear() {
    String previous = nonce;
    nonce = "";
    route = -1;
    expires = 0;
    generation = -1;
    return previous;
  }
}
