package com.happyhome.cc8assistant;

/** Metadata only. The Mac owns the pixels and authoritative retention deadline. */
final class PhotoFollowupWindow {
  static final int MAX_TTL_MS = 120000;

  static final class Picture {
    final String id;
    final int generation, width, height;
    final long route, until;

    Picture(String id, int generation, int width, int height, long route, long until) {
      this.id = id;
      this.generation = generation;
      this.width = width;
      this.height = height;
      this.route = route;
      this.until = until;
    }
  }

  private Picture picture;
  private long lastRoute = -1;
  private int lastGeneration = -1;

  static boolean validQuestion(String text) {
    if (text == null || text.length() > 1000) return false;
    boolean content = false;
    for (int i = 0; i < text.length(); i++) {
      char c = text.charAt(i);
      if (c < 32 && c != '\n' && c != '\t') return false;
      if (Character.isHighSurrogate(c)) {
        if (++i >= text.length() || !Character.isLowSurrogate(text.charAt(i))) return false;
        content = true;
      } else if (Character.isLowSurrogate(c)) return false;
      else if (!Character.isWhitespace(c) && !Character.isSpaceChar(c) && c != 0x85) content = true;
    }
    return content;
  }

  synchronized boolean offer(
      String id, int generation, int width, int height, int ttl, long route, long now) {
    if (id == null
        || !id.matches("[a-f0-9]{32}")
        || generation < 0
        || route < 0
        || ttl < 1
        || ttl > MAX_TTL_MS
        || now < 0
        || now > Long.MAX_VALUE - ttl
        || width < 1
        || height < 1
        || Math.max(width, height) > 1280
        || (long) width * height > 1280 * 960
        || (lastRoute == route && generation <= lastGeneration)) return false;
    picture = new Picture(id, generation, width, height, route, now + ttl);
    lastRoute = route;
    lastGeneration = generation;
    return true;
  }

  synchronized Picture current(long route, int generation, long now) {
    if (picture == null
        || picture.route != route
        || picture.generation != generation
        || now < 0
        || now >= picture.until) return null;
    return picture;
  }

  synchronized boolean expire(String id, int generation, long route) {
    if (picture == null
        || !picture.id.equals(id)
        || picture.generation != generation
        || picture.route != route) return false;
    picture = null;
    return true;
  }

  synchronized void clear() {
    // Keep the last accepted generation so a duplicate cannot renew a revoked
    // or expired offer. A new route/generation gets a new explicit offer.
    picture = null;
  }
}
