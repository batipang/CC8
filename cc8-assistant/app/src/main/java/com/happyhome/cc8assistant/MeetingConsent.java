package com.happyhome.cc8assistant;

/** A local foreground confirmation authorizes one matching reply on one route for 12 seconds. */
final class MeetingConsent {
  private String request = "";
  private long route = -1, expires;

  boolean pending() {
    return !request.isEmpty();
  }

  boolean arm(String id, long owner, long now) {
    if (id == null
        || !id.matches("[a-f0-9]{32}")
        || owner < 0
        || now < 0
        || now > Long.MAX_VALUE - 12000
        || !request.isEmpty()) return false;
    request = id;
    route = owner;
    expires = now + 12000;
    return true;
  }

  boolean take(String id, long owner, long now) {
    if (!request.equals(id) || owner != route || now < expires - 12000 || now >= expires)
      return false;
    clear();
    return true;
  }

  void reject(String id, long owner) {
    if (request.equals(id) && route == owner) clear();
  }

  String clear() {
    String previous = request;
    request = "";
    route = -1;
    expires = 0;
    return previous;
  }
}
