package com.happyhome.cc8assistant;

import org.json.JSONObject;

/** Immutable, route-owned public version declarations. No installer or permission decisions. */
final class VersionPresentation {
  static final class Peer {
    final long route;
    final boolean known;
    final String backend, releaseId, assistant;
    final int code, protocol, minimum, maximum;

    Peer(JSONObject value, long route) {
      this.route = route;
      backend = version(value, "backend_version");
      assistant = version(value, "expected_assistant_version");
      releaseId = text(value, "release_id", "[a-z][a-z0-9.-]{0,63}", 64);
      code = integer(value, "expected_assistant_code", Integer.MAX_VALUE);
      protocol = integer(value, "protocol_version", 255);
      minimum = integer(value, "device_protocol_min", 255);
      maximum = integer(value, "device_protocol_max", 255);
      known =
          integer(value, "schema_version", 1) == 1
              && !backend.isEmpty()
              && !assistant.isEmpty()
              && !releaseId.isEmpty()
              && code > 0
              && minimum > 0
              && minimum <= protocol
              && protocol <= maximum;
    }
  }

  private static int integer(JSONObject value, String key, int max) {
    Object raw = value == null ? null : value.opt(key);
    if (!(raw instanceof Integer) && !(raw instanceof Long)) return -1;
    long number = ((Number) raw).longValue();
    return number > 0 && number <= max ? (int) number : -1;
  }

  private static String text(JSONObject value, String key, String pattern, int max) {
    Object raw = value == null ? null : value.opt(key);
    if (!(raw instanceof String)) return "";
    String result = (String) raw;
    return result.length() <= max && result.matches(pattern) ? result : "";
  }

  private static String version(JSONObject value, String key) {
    return text(value, key, "[0-9]+\\.[0-9]+\\.[0-9]+", 32);
  }

  static String message(
      String version, int code, int protocol, String signatures, long route, Peer peer) {
    String result = "大黄蜂 · 当前运行信息\n\n助手：" + version + "（" + code + "）\n本机协议：" + protocol;
    if (route < 0) {
      result += "\n\nMac 未连接 · 仅显示本机信息\n后台版本、协议范围及版本组合：未确认";
    } else if (peer == null || peer.route != route || !peer.known) {
      result += "\n\nMac 已连接，但本次连接未提供可核验的版本声明\n后台版本、协议范围及版本组合：未确认";
    } else {
      boolean compatible = peer.minimum <= protocol && protocol <= peer.maximum;
      result +=
          "\n\nMac 后台："
              + peer.backend
              + "\n后台协议："
              + peer.protocol
              + "\n支持的设备协议："
              + peer.minimum
              + "～"
              + peer.maximum
              + "\n协议范围声明："
              + (compatible ? "兼容" : "不兼容，请在 Mac 核对组合版本")
              + "\n后台声明的助手："
              + peer.assistant
              + "（"
              + peer.code
              + "）"
              + "\n版本组合："
              + (version.equals(peer.assistant) && code == peer.code
                  ? "与后台发布声明一致"
                  : "与后台发布声明不同，请在 Mac 完成组合检查")
              + "\n发布声明："
              + peer.releaseId;
    }
    return result
        + "\n\n本机安装包签名证书 SHA-256：\n"
        + signatures
        + "\n\n更新准备在 Mac 完成：先备份配置、核对原签名和协议，再检查实际安装及运行结果。"
        + "此页是当前信息，不是安装前检查或完整功能验收。";
  }
}
