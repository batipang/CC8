package com.happyhome.cc8assistant;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.wifi.WifiManager;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.security.cert.*;
import javax.net.ssl.*;
import org.json.JSONObject;
import org.json.JSONArray;

/** USB-provisioned private pairing; no transport secret is compiled into the APK. */
final class LanConnector {
  // USB 写入的私有配对信息是信任来源，Wi-Fi 连接还须校验证书指纹和共享密钥。
  interface Current {
    boolean active();
  }

  private final Object connectLock = new Object();
  private volatile Socket pendingSocket;

  void cancelPending() {
    Socket pending = pendingSocket;
    if (pending != null)
      try {
        pending.close();
      } catch (IOException ignored) {
      }
  }

  private static void check(Current current) throws IOException {
    if (!current.active()) throw new IOException("Connection lifecycle ended");
  }

  private final SharedPreferences prefs;
  private final WifiManager manager;
  private final ConnectivityManager connectivity;
  private WifiManager.WifiLock lock;

  LanConnector(Context context) {
    prefs = context.getSharedPreferences("cc8_lan_pairing", Context.MODE_PRIVATE);
    manager = (WifiManager) context.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
    connectivity = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
  }

  boolean networkAvailable() {
    NetworkInfo network = connectivity == null ? null : connectivity.getActiveNetworkInfo();
    return network != null
        && network.isConnected()
        && (network.getType() == ConnectivityManager.TYPE_WIFI
            || network.getType() == ConnectivityManager.TYPE_ETHERNET);
  }

  boolean authorizeDesktopCamera(
      DesktopCameraAuth authorization, String nonce, int generation, String signature) {
    try {
      return authorization.accept(
          new JSONObject(prefs.getString("config", "{}")).optString("token"),
          nonce,
          generation,
          signature);
    } catch (Exception error) {
      return false;
    }
  }

  boolean paired() {
    return !prefs.getString("config", "").isEmpty();
  }

  synchronized void acquire() {
    if (manager != null && lock == null) {
      lock =
          manager.createWifiLock(
              WifiManager.WIFI_MODE_FULL_HIGH_PERF, "Bumblebee foreground voice");
      lock.setReferenceCounted(false);
      lock.acquire();
    }
  }

  synchronized void release() {
    if (lock != null) {
      if (lock.isHeld()) lock.release();
      lock = null;
    }
  }

  void configure(JSONObject value) throws Exception {
    String sid = value.optString("server_id"),
        token = value.optString("token"),
        pin = value.optString("certificate_sha256");
    int port = value.optInt("port");
    if (!sid.matches("[0-9a-f]{32}")
        || !token.matches("[0-9a-f]{64}")
        || !pin.matches("[0-9a-f]{64}")
        || port < 1024
        || port > 65535) throw new IOException("Invalid LAN pairing");
    String old = prefs.getString("config", "");
    if (!old.isEmpty()) {
      JSONObject previous = new JSONObject(old);
      if (!previous.optString("server_id").equals(sid)
          || !previous.optString("certificate_sha256").equals(pin))
        throw new IOException("Different paired Mac");
    }
    if (!prefs.edit().putString("config", value.toString()).commit())
      throw new IOException("Could not persist LAN pairing");
  }

  Socket connect(Current current) throws Exception {
    synchronized (connectLock) {
      check(current);
      JSONObject config = new JSONObject(prefs.getString("config", "{}"));
      String last = prefs.getString("last_host", "");
      if (!last.isEmpty())
        try {
          return connectTo(last, config, current);
        } catch (IOException | GeneralSecurityException ignored) {
        }
      JSONArray hosts = config.optJSONArray("hosts");
      if (hosts != null)
        for (int i = 0; i < Math.min(8, hosts.length()); i++) {
          String host = hosts.optString(i);
          if (host.equals(last)) continue;
          try {
            return connectTo(host, config, current);
          } catch (IOException | GeneralSecurityException ignored) {
          }
        }
      check(current);
      return connectTo(discover(config), config, current);
    }
  }

  private Socket connectTo(String host, JSONObject config, Current current) throws Exception {
    // 只连局域网 IPv4，TLS 握手前后均检查前台票据，拒绝已经过期的连接任务。
    check(current);
    InetAddress address = InetAddress.getByName(host);
    if (!(address instanceof Inet4Address) || !address.isSiteLocalAddress())
      throw new IOException("LAN address required");
    final String pin = config.getString("certificate_sha256");
    SSLContext ssl = SSLContext.getInstance("TLSv1.2");
    ssl.init(
        null,
        new TrustManager[] {
          new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() {
              return new X509Certificate[0];
            }

            public void checkClientTrusted(X509Certificate[] chain, String auth)
                throws CertificateException {
              throw new CertificateException("Client certificate unsupported");
            }

            public void checkServerTrusted(X509Certificate[] chain, String auth)
                throws CertificateException {
              try {
                if (chain == null
                    || chain.length == 0
                    || !TransportAuth.matches(
                        pin,
                        TransportAuth.hex(
                            MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded()))))
                  throw new CertificateException("Mac certificate mismatch");
              } catch (GeneralSecurityException error) {
                throw new CertificateException("Mac certificate validation failed", error);
              }
            }
          }
        },
        new SecureRandom());
    Socket raw = new Socket();
    SSLSocket socket = null;
    pendingSocket = raw;
    try {
      check(current);
      raw.connect(new InetSocketAddress(address, config.getInt("port")), 900);
      raw.setTcpNoDelay(true);
      raw.setSoTimeout(3500);
      socket =
          (SSLSocket) ssl.getSocketFactory().createSocket(raw, host, config.getInt("port"), true);
      socket.setEnabledProtocols(new String[] {"TLSv1.2"});
      socket.setSoTimeout(3500);
      socket.startHandshake();
      check(current);
      JSONObject auth = Wire.event("transport_auth");
      Wire.put(auth, "token", config.getString("token"));
      byte[] bytes = Wire.jsonBytes(auth);
      Wire.write(
          new DataOutputStream(socket.getOutputStream()),
          Wire.EVENT,
          0,
          0,
          0,
          System.nanoTime(),
          bytes);
      Wire.Packet reply = Wire.read(new DataInputStream(socket.getInputStream()));
      if (reply.type != Wire.EVENT
          || !"transport_welcome".equals(Wire.json(reply).optString("event")))
        throw new IOException("Mac authentication incomplete");
      check(current);
      prefs.edit().putString("last_host", host).apply();
      return socket;
    } catch (Exception error) {
      if (socket != null)
        try {
          socket.close();
        } catch (IOException ignored) {
        }
      else
        try {
          raw.close();
        } catch (IOException ignored) {
        }
      throw error;
    } finally {
      if (pendingSocket == raw) pendingSocket = null;
    }
  }

  private String discover(JSONObject config) throws Exception {
    byte[] nonceBytes = new byte[16];
    new SecureRandom().nextBytes(nonceBytes);
    String nonce = TransportAuth.hex(nonceBytes), token = config.getString("token");
    JSONObject request = new JSONObject();
    request.put("type", "cc8_discover");
    request.put("nonce", nonce);
    request.put("mac", TransportAuth.sign(token, "discover:" + nonce));
    byte[] data = request.toString().getBytes(StandardCharsets.UTF_8);
    try (DatagramSocket socket = new DatagramSocket()) {
      socket.setBroadcast(true);
      socket.setSoTimeout(1600);
      socket.send(
          new DatagramPacket(data, data.length, InetAddress.getByName("255.255.255.255"), 18767));
      long deadline = System.nanoTime() + 1600000000L;
      while (System.nanoTime() < deadline) {
        byte[] bytes = new byte[2048];
        DatagramPacket response = new DatagramPacket(bytes, bytes.length);
        socket.receive(response);
        if (!response.getAddress().isSiteLocalAddress()) continue;
        JSONObject offer;
        try {
          offer =
              new JSONObject(new String(bytes, 0, response.getLength(), StandardCharsets.UTF_8));
        } catch (org.json.JSONException malformed) {
          continue;
        }
        String sid = offer.optString("server_id");
        int port = offer.optInt("port");
        String expected = TransportAuth.sign(token, "offer:" + nonce + ":" + sid + ":" + port);
        if ("cc8_offer".equals(offer.optString("type"))
            && nonce.equals(offer.optString("nonce"))
            && sid.equals(config.getString("server_id"))
            && port == config.getInt("port")
            && TransportAuth.matches(expected, offer.optString("mac")))
          return response.getAddress().getHostAddress();
      }
    }
    throw new IOException("Paired Mac not found on LAN");
  }
}
