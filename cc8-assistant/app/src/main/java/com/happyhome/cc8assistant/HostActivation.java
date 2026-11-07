package com.happyhome.cc8assistant;

import java.util.ArrayDeque;

/** Orders role changes across both sockets; never stores application content. */
final class HostActivation {
  private String owner;
  private long serial;
  private final ArrayDeque<String> retired = new ArrayDeque<>();

  boolean accept(String nextOwner, long nextSerial) {
    if (nextOwner == null || !nextOwner.matches("[a-f0-9]{32}") || nextSerial <= 0)
      throw new IllegalArgumentException("Invalid host activation identity");
    if (nextOwner.equals(owner)) {
      if (nextSerial <= serial) return false;
    } else {
      if (retired.contains(nextOwner)) return false;
      if (owner != null) {
        retired.addLast(owner);
        if (retired.size() > 16) retired.removeFirst();
      }
      owner = nextOwner;
    }
    serial = nextSerial;
    return true;
  }
}
