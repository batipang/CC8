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

/** Shared private pairing and pinned TLS for USB and Wi-Fi. */
final class LanConnector {
  // Both links prove possession of the stored pairing before joining the session.
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

  synchronized void enroll(String source) throws Exception {
    if (paired() || source == null || source.length() > 2048)
      throw new IOException("Pairing import unavailable");
    JSONObject value = new JSONObject(source);
    JSONArray hosts = value.optJSONArray("hosts");
    if (value.length() != 5 || hosts == null || hosts.length() > 8)
      throw new IOException("Invalid pairing import");
    for (int i = 0; i < hosts.length(); i++) {
      Object host = hosts.get(i);
      if (!(host instanceof String) || !((String) host).matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}"))
        throw new IOException("Invalid pairing host");
      String[] components = ((String) host).split("\\.");
      byte[] bytes = new byte[4];
      for (int j = 0; j < bytes.length; j++) {
        int octet = Integer.parseInt(components[j]);
        if (octet > 255) throw new IOException("Invalid IPv4 octet");
        bytes[j] = (byte) octet;
      }
      if (!InetAddress.getByAddress(bytes).isSiteLocalAddress())
        throw new IOException("Private IPv4 required");
    }
    configure(value);
  }

  synchronized void configure(JSONObject value) throws Exception {
    Object sidValue = value.opt("server_id");
    Object tokenValue = value.opt("token");
    Object pinValue = value.opt("certificate_sha256");
    Object portValue = value.opt("port");
    if (!(sidValue instanceof String)
        || !(tokenValue instanceof String)
        || !(pinValue instanceof String)
        || !(portValue instanceof Integer)) throw new IOException("Invalid LAN pairing fields");
    String sid = (String) sidValue, token = (String) tokenValue, pin = (String) pinValue;
    int port = (Integer) portValue;
    if (!sid.matches("[0-9a-f]{32}")
        || !token.matches("[0-9a-f]{64}")
        || !pin.matches("[0-9a-f]{64}")
        || port < 1024
        || port > 65535) throw new IOException("Invalid LAN pairing");
    String old = prefs.getString("config", "");
    if (!old.isEmpty()) {
      JSONObject previous = new JSONObject(old);
      if (!previous.optString("server_id").equals(sid)
          || !TransportAuth.matches(previous.optString("token"), token)
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
    Socket raw = new Socket();
    pendingSocket = raw;
    try {
      check(current);
      int port = config.getInt("port");
      raw.connect(new InetSocketAddress(address, port), 900);
      Socket socket = authenticate(raw, host, port, config, current, false);
      prefs.edit().putString("last_host", host).apply();
      return socket;
    } catch (Exception error) {
      TransportClose.asynchronously(raw);
      throw error;
    } finally {
      if (pendingSocket == raw) pendingSocket = null;
    }
  }

  Socket authenticateUsb(Socket raw, Current current) throws Exception {
    JSONObject config = new JSONObject(prefs.getString("config", "{}"));
    return authenticate(raw, "localhost", raw.getPort(), config, current, true);
  }

  private static SSLContext pinnedContext(String pin) throws GeneralSecurityException {
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
    return ssl;
  }

  private static Socket authenticate(
      Socket raw, String host, int port, JSONObject config, Current current, boolean usb)
      throws Exception {
    check(current);
    String pin = config.getString("certificate_sha256"), token = config.getString("token");
    if (!pin.matches("[0-9a-f]{64}") || !token.matches("[0-9a-f]{64}"))
      throw new IOException("Missing valid pairing");
    SSLContext ssl = pinnedContext(pin);
    raw.setTcpNoDelay(true);
    raw.setSoTimeout(3500);
    SSLSocket socket = (SSLSocket) ssl.getSocketFactory().createSocket(raw, host, port, true);
    try {
      // On USB Android accepts the TCP connection, but it remains the TLS client.
      socket.setUseClientMode(true);
      socket.setEnabledProtocols(new String[] {"TLSv1.2"});
      socket.setSoTimeout(3500);
      socket.startHandshake();
      check(current);
      JSONObject auth = Wire.event("transport_auth");
      Wire.put(auth, "token", token);
      if (usb) Wire.put(auth, "transport", "usb");
      Wire.write(
          new DataOutputStream(socket.getOutputStream()),
          Wire.EVENT,
          0,
          0,
          0,
          System.nanoTime(),
          Wire.jsonBytes(auth));
      Wire.Packet reply = Wire.read(new DataInputStream(socket.getInputStream()));
      if (reply.type != Wire.EVENT
          || !"transport_welcome".equals(Wire.json(reply).optString("event")))
        throw new IOException("Mac authentication incomplete");
      check(current);
      return socket;
    } catch (Exception error) {
      TransportClose.asynchronously(socket);
      throw error;
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
