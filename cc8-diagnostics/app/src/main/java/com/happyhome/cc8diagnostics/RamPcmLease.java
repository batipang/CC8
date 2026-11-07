package com.happyhome.cc8diagnostics;

import java.util.Arrays;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/** One bounded PCM window. The foreground owner supplies a monotonic clock; nothing is persisted. */
public final class RamPcmLease {
  public static final int RATE = 48000, CHANNELS = 4, FRAME_BYTES = CHANNELS * 2;
  public static final int MAX_BYTES = RATE * FRAME_BYTES * 6, CHUNK_BYTES = 192000;
  public static final long LIFETIME_MS = 45000;

  public enum Profile {
    MONO(16000, 1),
    QUAD(48000, 4);
    public final int rate, channels, frameBytes, maxBytes;

    Profile(int rate, int channels) {
      this.rate = rate;
      this.channels = channels;
      frameBytes = channels * 2;
      maxBytes = rate * frameBytes * 6;
    }

    public static Profile forLayout(int rate, int channels) {
      for (Profile profile : values())
        if (profile.rate == rate && profile.channels == channels) return profile;
      throw new IllegalArgumentException("Unsupported RAM analysis layout");
    }
  }

  private byte[] pcm;
  private int length, next, source;
  private long generation, started, expires;
  private String token, state = "idle";
  private Profile profile;

  public static boolean validToken(String token) {
    return token != null && token.length() == 32 && token.matches("[a-f0-9]{32}");
  }

  public synchronized long begin(String token, int source, long now) {
    return begin(token, source, Profile.QUAD, now);
  }

  public synchronized long begin(String token, int source, Profile profile, long now) {
    if (!validToken(token)
        || profile == null
        || (source != 1 && source != 6 && source != 7)
        || now < 0
        || now > Long.MAX_VALUE - LIFETIME_MS)
      throw new IllegalArgumentException("Invalid RAM analysis request");
    clear("pending");
    this.token = token;
    this.source = source;
    this.profile = profile;
    started = now;
    expires = now + LIFETIME_MS;
    return generation;
  }

  /** Transfers buffer ownership on success; failed or stale producers must not reuse their buffer. */
  public synchronized boolean publish(long owner, byte[] data, int used, long now) {
    if (data == null) {
      if (owner == generation) clear("invalid_extent");
      throw new IllegalArgumentException("Missing PCM");
    }
    expire(now);
    if (owner != generation || !"pending".equals(state)) {
      if (owner == generation) clear("invalid_publish");
      if (data != pcm) Arrays.fill(data, (byte) 0);
      return false;
    }
    Profile layout = profile;
    if (used < layout.rate * layout.frameBytes
        || used > layout.maxBytes
        || used > data.length
        || data.length > layout.maxBytes
        || used % layout.frameBytes != 0) {
      if (owner == generation) clear("invalid_extent");
      if (data != pcm) Arrays.fill(data, (byte) 0);
      throw new IllegalArgumentException("Invalid PCM extent");
    }
    pcm = data;
    length = used;
    state = "ready";
    return true;
  }

  public static final class Chunk {
    public final byte[] bytes;
    public final int offset, totalBytes, source, sampleRate, channels;

    private Chunk(byte[] bytes, int offset, int totalBytes, int source, Profile profile) {
      this.bytes = bytes;
      this.offset = offset;
      this.totalBytes = totalBytes;
      this.source = source;
      sampleRate = profile.rate;
      channels = profile.channels;
    }

    public String sha256() throws NoSuchAlgorithmException {
      byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
      StringBuilder hex = new StringBuilder(64);
      for (byte value : digest) {
        hex.append(Character.forDigit((value >>> 4) & 15, 16));
        hex.append(Character.forDigit(value & 15, 16));
      }
      return hex.toString();
    }
  }

  public synchronized Chunk read(String token, int offset, long now) {
    expire(now);
    if (!"ready".equals(state) || !this.token.equals(token)) return null;
    // An unconfirmed transfer cannot replay an earlier chunk or skip audio.
    if (offset != next) {
      clear("invalid_cursor");
      return null;
    }
    int count = Math.min(CHUNK_BYTES, length - next);
    Chunk result =
        new Chunk(Arrays.copyOfRange(pcm, next, next + count), next, length, source, profile);
    Arrays.fill(pcm, next, next + count, (byte) 0);
    next += count;
    if (next == length) clear("consumed");
    return result;
  }

  public static final class Status {
    public final String state;
    public final int remainingBytes;

    private Status(String state, int remainingBytes) {
      this.state = state;
      this.remainingBytes = remainingBytes;
    }
  }

  public synchronized Status status(long now) {
    expire(now);
    return new Status(state, "ready".equals(state) ? length - next : 0);
  }

  public synchronized void clear() {
    clear("cleared");
  }

  public synchronized void discard(long owner) {
    if (owner == generation) clear("cleared");
  }

  public synchronized boolean discardToken(String token) {
    if (this.token == null || !this.token.equals(token)) return false;
    clear("cleared");
    return true;
  }

  private void expire(long now) {
    if (token != null && (now < started || now >= expires)) clear("expired");
  }

  private void clear(String reason) {
    if (pcm != null) Arrays.fill(pcm, (byte) 0);
    pcm = null;
    token = null;
    profile = null;
    length = next = 0;
    state = reason;
    generation++;
  }
}
