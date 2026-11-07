package com.happyhome.cc8assistant;

import java.util.LinkedHashMap;
import java.util.Map;

/** Foreground-only observations of confirmed assistant links, never a sleep detector. */
final class ConnectionAvailability {
  // VoiceSession owns this object under routeLock. Times use elapsedRealtime,
  // including device sleep; wall-clock changes cannot invent or erase an outage.
  private boolean observing, connected, seenConnection;
  private long lastNow, startedAt, lostAt;
  private long recoveredCount, recoveredTotalMs, lastRecoveredMs = -1;
  private long interruptedCount;

  private long time(long now) {
    lastNow = Math.max(lastNow, now);
    return lastNow;
  }

  void begin(long now) {
    now = time(now);
    if (observing) return;
    observing = true;
    connected = seenConnection = false;
    startedAt = now;
  }

  void online(long now) {
    now = time(now);
    if (!observing || connected) return;
    if (seenConnection) {
      lastRecoveredMs = now - lostAt;
      recoveredTotalMs = add(recoveredTotalMs, lastRecoveredMs);
      recoveredCount = add(recoveredCount, 1);
    }
    connected = seenConnection = true;
  }

  void offline(long now) {
    now = time(now);
    if (!observing || !connected) return;
    connected = false;
    lostAt = now;
  }

  void end(long now) {
    time(now);
    if (observing && seenConnection && !connected) interruptedCount = add(interruptedCount, 1);
    observing = connected = seenConnection = false;
  }

  Snapshot snapshot(long now) {
    now = time(now);
    String state =
        !observing
            ? "not_observing"
            : connected ? "connected" : seenConnection ? "unavailable" : "waiting";
    return new Snapshot(
        state,
        observing && !seenConnection ? now - startedAt : 0,
        observing && seenConnection && !connected ? now - lostAt : 0,
        lastRecoveredMs,
        recoveredCount,
        recoveredTotalMs,
        interruptedCount);
  }

  private static long add(long value, long increment) {
    return value > Long.MAX_VALUE - increment ? Long.MAX_VALUE : value + increment;
  }

  static String duration(long ms) {
    long seconds = Math.max(0, ms) / 1000;
    if (seconds == 0) return "不足 1 秒";
    if (seconds < 60) return seconds + " 秒";
    if (seconds < 3600) return seconds / 60 + " 分 " + seconds % 60 + " 秒";
    return seconds / 3600 + " 小时 " + seconds / 60 % 60 + " 分";
  }

  static final class Snapshot {
    final String state;
    final long waitingMs, unavailableMs, lastRecoveredMs, recoveredCount, recoveredTotalMs;
    final long interruptedCount;

    Snapshot(
        String state,
        long waitingMs,
        long unavailableMs,
        long lastRecoveredMs,
        long recoveredCount,
        long recoveredTotalMs,
        long interruptedCount) {
      this.state = state;
      this.waitingMs = waitingMs;
      this.unavailableMs = unavailableMs;
      this.lastRecoveredMs = lastRecoveredMs;
      this.recoveredCount = recoveredCount;
      this.recoveredTotalMs = recoveredTotalMs;
      this.interruptedCount = interruptedCount;
    }

    Map<String, Object> values() {
      Map<String, Object> values = new LinkedHashMap<>();
      values.put("version", 1);
      values.put("state", state);
      values.put("summary", summary());
      values.put("waiting_ms", waitingMs);
      values.put("unavailable_ms", unavailableMs);
      values.put("last_recovered_ms", lastRecoveredMs);
      values.put("recovered_count", recoveredCount);
      values.put("recovered_total_ms", recoveredTotalMs);
      values.put("interrupted_count", interruptedCount);
      return values;
    }

    String summary() {
      String current =
          "waiting".equals(state)
              ? "本次前台尚未连上 Mac，已等待 " + duration(waitingMs)
              : "unavailable".equals(state)
                  ? "连接中断，助手暂不可用；已观察到中断 " + duration(unavailableMs)
                  : "connected".equals(state) ? "当前连接已确认" : "当前不在前台，未观察连接可用性";
      if (lastRecoveredMs >= 0) current += "\n最近一次已恢复的中断：" + duration(lastRecoveredMs);
      return current;
    }
  }
}
