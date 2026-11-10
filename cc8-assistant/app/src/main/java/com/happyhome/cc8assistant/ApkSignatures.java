package com.happyhome.cc8assistant;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;
import java.security.MessageDigest;
import java.util.TreeSet;

/** Read only this installed package's public signing certificates, never a signing key. */
final class ApkSignatures {
  static String current(Context context) {
    try {
      PackageInfo info =
          context
              .getPackageManager()
              .getPackageInfo(
                  context.getPackageName(),
                  Build.VERSION.SDK_INT >= 28
                      ? PackageManager.GET_SIGNING_CERTIFICATES
                      : PackageManager.GET_SIGNATURES);
      Signature[] values;
      if (Build.VERSION.SDK_INT >= 28) {
        values = info.signingInfo == null ? null : info.signingInfo.getApkContentsSigners();
      } else values = info.signatures;
      if (values == null || values.length == 0 || values.length > 8) return "未取得，请在 Mac 核对";
      TreeSet<String> hashes = new TreeSet<>();
      for (Signature value : values) {
        if (value == null) return "未取得，请在 Mac 核对";
        byte[] raw = value.toByteArray();
        if (raw.length == 0 || raw.length > 16384) return "未取得，请在 Mac 核对";
        StringBuilder hash = new StringBuilder();
        for (byte b : MessageDigest.getInstance("SHA-256").digest(raw)) {
          hash.append("0123456789abcdef".charAt((b & 255) >>> 4));
          hash.append("0123456789abcdef".charAt(b & 15));
        }
        hashes.add(hash.toString());
      }
      StringBuilder result = new StringBuilder();
      for (String value : hashes) {
        if (result.length() > 0) result.append('\n');
        result.append(value);
      }
      return result.toString();
    } catch (Exception unavailable) {
      return "未取得，请在 Mac 核对";
    }
  }
}
