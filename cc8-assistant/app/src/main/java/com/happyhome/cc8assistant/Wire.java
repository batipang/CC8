package com.happyhome.cc8assistant;

import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

final class Wire {
  static final int MIC = 1,
      RENDER = 2,
      EVENT = 3,
      SPEAKER = 4,
      MIC_PAIRED = 5,
      ROUTE_BOUND = 6,
      HEADER = 29,
      MAX = 65536;

  static final class Packet {
    int type, generation;
    long sequence, frame, nanos;
    byte[] data;
    // A queue may outlive activation of this same socket. Never retag old work.
    long outgoingRoute = -1;
    // Queue-only ownership for camera events. These fields never cross the wire.
    String cameraRequestId;
    long cameraRoute;
    int cameraGeneration;
  }

  static Packet read(DataInputStream in) throws IOException {
    int size = in.readInt();
    if (size < HEADER || size > MAX) throw new IOException("Invalid packet length");
    Packet p = new Packet();
    p.type = in.readUnsignedByte();
    p.generation = in.readInt();
    p.sequence = in.readLong();
    p.frame = in.readLong();
    p.nanos = in.readLong();
    p.data = new byte[size - HEADER];
    in.readFully(p.data);
    validate(p.type, p.data);
    return p;
  }

  static void write(
      DataOutputStream out,
      int type,
      int generation,
      long sequence,
      long frame,
      long nanos,
      byte[] data)
      throws IOException {
    validate(type, data);
    out.writeInt(HEADER + data.length);
    out.writeByte(type);
    out.writeInt(generation);
    out.writeLong(sequence);
    out.writeLong(frame);
    out.writeLong(nanos);
    out.write(data);
    out.flush();
  }

  private static void validate(int type, byte[] data) throws IOException {
    if (data == null) throw new IOException("Missing packet payload");
    if (type < 1 || type > ROUTE_BOUND) throw new IOException("Invalid packet type");
    if (data.length > MAX - HEADER) throw new IOException("Packet too large");
    int offset = 0;
    if (type == ROUTE_BOUND) {
      if (data.length < 17) throw new IOException("Invalid route envelope");
      type = data[16] & 255;
      if (type < 1 || type > MIC_PAIRED) throw new IOException("Invalid inner packet type");
      offset = 17;
    }
    int length = data.length - offset;
    if (type == MIC_PAIRED && (length == 0 || length % 4 != 0))
      throw new IOException("Invalid paired PCM frame");
    if (type != EVENT && (length % 2 != 0 || length > 6400))
      throw new IOException("Invalid PCM frame");
  }

  static void bindRoute(Packet packet, String token) throws IOException {
    validate(packet.type, packet.data);
    if (packet.type == ROUTE_BOUND || token == null || !token.matches("[a-f0-9]{32}"))
      throw new IOException("Invalid route token");
    byte[] bound = new byte[17 + packet.data.length];
    for (int i = 0; i < 16; i++)
      bound[i] = (byte) Integer.parseInt(token.substring(i * 2, i * 2 + 2), 16);
    bound[16] = (byte) packet.type;
    System.arraycopy(packet.data, 0, bound, 17, packet.data.length);
    validate(ROUTE_BOUND, bound);
    packet.type = ROUTE_BOUND;
    packet.data = bound;
  }

  static Packet unbindRoute(Packet packet, String token) throws IOException {
    validate(packet.type, packet.data);
    if (packet.type != ROUTE_BOUND || token == null) return null;
    if (!token.matches("[a-f0-9]{32}")) throw new IOException("Invalid route token");
    for (int i = 0; i < 16; i++)
      if (packet.data[i] != (byte) Integer.parseInt(token.substring(i * 2, i * 2 + 2), 16))
        return null;
    Packet inner = new Packet();
    inner.type = packet.data[16] & 255;
    inner.generation = packet.generation;
    inner.sequence = packet.sequence;
    inner.frame = packet.frame;
    inner.nanos = packet.nanos;
    inner.data = java.util.Arrays.copyOfRange(packet.data, 17, packet.data.length);
    return inner;
  }

  static JSONObject json(Packet p) throws Exception {
    return new JSONObject(new String(p.data, StandardCharsets.UTF_8));
  }

  static byte[] pcm(short[] samples, int length) {
    byte[] b = new byte[length * 2];
    for (int i = 0; i < length; i++) {
      b[i * 2] = (byte) samples[i];
      b[i * 2 + 1] = (byte) (samples[i] >> 8);
    }
    return b;
  }

  static short[] samples(byte[] b) {
    short[] s = new short[b.length / 2];
    for (int i = 0; i < s.length; i++) s[i] = (short) ((b[i * 2] & 255) | (b[i * 2 + 1] << 8));
    return s;
  }

  static byte[] jsonBytes(JSONObject value) {
    return value.toString().getBytes(StandardCharsets.UTF_8);
  }

  static JSONObject event(String name) {
    JSONObject j = new JSONObject();
    put(j, "event", name);
    return j;
  }

  static void put(JSONObject j, String k, Object v) {
    try {
      j.put(k, v);
    } catch (Exception ignored) {
    }
  }
}
