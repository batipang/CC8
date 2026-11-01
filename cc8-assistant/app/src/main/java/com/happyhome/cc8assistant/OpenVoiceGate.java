package com.happyhome.cc8assistant;

/** One bounded foreground-opening request, consumed before permission/capture. */
final class OpenVoiceGate {
  // 只消费一次明确打开界面的请求，最多等连接 30 秒；普通重连不重新授权开麦。
  private boolean pending, opening;
  private long deadline;

  void opened(long now) {
    opening = true;
    pending = true;
    deadline = now + 30000;
  }

  void resumed(long now) {
    if (opening) {
      opening = false;
      pending = true;
      deadline = now + 30000;
    }
  }

  void stopped() {
    cancel();
  }

  void cancel() {
    opening = false;
    pending = false;
  }

  boolean staged() {
    return opening;
  }

  boolean waiting(long now) {
    return pending && now < deadline;
  }

  boolean expire(long now) {
    if (pending && now >= deadline) {
      pending = false;
      return true;
    }
    return false;
  }

  boolean take(long now, boolean foreground, boolean connected, boolean recording) {
    // 权限/硬件操作前就消费请求，避免失败重试或重复回调意外多次启动收音。
    if (expire(now) || opening || !pending || !foreground) return false;
    if (recording) {
      pending = false;
      return false;
    }
    if (!connected) return false;
    pending = false;
    return true;
  }
}
