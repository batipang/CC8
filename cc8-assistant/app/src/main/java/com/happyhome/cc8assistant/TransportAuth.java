package com.happyhome.cc8assistant;

import java.security.MessageDigest;
import java.security.GeneralSecurityException;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Android-free signing primitives shared by discovery and certificate pinning. */
final class TransportAuth {
  // UDP 发现使用 HMAC，TLS 使用证书指纹；签名比较按字节完成，不能改成普通字符串相等。
  static byte[] unhex(String value) {
    if (value == null || value.length() % 2 != 0) throw new IllegalArgumentException("Invalid hex");
    byte[] bytes = new byte[value.length() / 2];
    for (int i = 0; i < bytes.length; i++) {
      int a = Character.digit(value.charAt(i * 2), 16),
          b = Character.digit(value.charAt(i * 2 + 1), 16);
      if (a < 0 || b < 0) throw new IllegalArgumentException("Invalid hex");
      bytes[i] = (byte) (a * 16 + b);
    }
    return bytes;
  }

  static String hex(byte[] bytes) {
    char[] out = new char[bytes.length * 2];
    String digits = "0123456789abcdef";
    for (int i = 0; i < bytes.length; i++) {
      out[i * 2] = digits.charAt((bytes[i] >>> 4) & 15);
      out[i * 2 + 1] = digits.charAt(bytes[i] & 15);
    }
    return new String(out);
  }

  static String sign(String token, String value) throws GeneralSecurityException {
    Mac mac = Mac.getInstance("HmacSHA256");
    mac.init(new SecretKeySpec(unhex(token), "HmacSHA256"));
    return hex(mac.doFinal(value.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
  }

  static boolean matches(String expected, String actual) {
    try {
      return MessageDigest.isEqual(unhex(expected), unhex(actual));
    } catch (IllegalArgumentException error) {
      return false;
    }
  }
}
