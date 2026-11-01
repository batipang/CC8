package com.happyhome.cc8assistant;

import java.io.IOException;
import java.net.Socket;

/** TLS close can send close_notify and must never run on the UI thread. */
final class TransportClose {
  // TLS close_notify 可能阻塞，放到独立线程关闭，避免主线程卡顿及 StrictMode 网络异常。
  static void asynchronously(Socket connection) {
    Thread closer =
        new Thread(
            () -> {
              try {
                connection.close();
              } catch (IOException | RuntimeException ignored) {
              }
            },
            "cc8-transport-close");
    closer.setDaemon(true);
    closer.start();
  }
}
