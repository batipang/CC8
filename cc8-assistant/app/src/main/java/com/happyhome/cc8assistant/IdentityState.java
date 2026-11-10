package com.happyhome.cc8assistant;

import java.io.IOException;
import org.json.JSONObject;

/** Persistent privacy floor and queued-render ownership; never opens device resources. */
final class IdentityState {
  static final long MAX_REVISION = Integer.MAX_VALUE;

  static final class Saved {
    final boolean guestRequested;
    final String mode;
    final long revision;

    Saved(boolean guestRequested, String mode, long revision) {
      if (!("owner".equals(mode) || "guest".equals(mode))
          || revision < -1
          || revision > MAX_REVISION
          || (revision == -1 && (!"owner".equals(mode) || guestRequested))
          || ("guest".equals(mode) && !guestRequested)) {
        throw new IllegalArgumentException("Invalid saved identity");
      }
      this.guestRequested = guestRequested;
      this.mode = mode;
      this.revision = revision;
    }
  }

  interface Store {
    Saved read() throws IOException;

    boolean write(Saved state);
  }

  private final Store store;
  private Saved saved = new Saved(false, "owner", -1);
  private boolean loaded, confirmed, legacy, restrictive;
  private long route = -1, epoch;
  private String mode = "blocked", token = "";

  IdentityState(Store store) {
    this.store = store;
  }

  synchronized void load() throws IOException {
    loaded = confirmed = legacy = false;
    restrictive = true;
    mode = "blocked";
    epoch++;
    Saved value = store.read();
    if (value == null) throw new IOException("Identity storage unavailable");
    saved = value;
    restrictive = value.guestRequested;
    loaded = true;
  }

  synchronized void route(long value) {
    if (route == value) return;
    route = value;
    confirmed = legacy = false;
    mode = "blocked";
    token = "";
    epoch++;
  }

  synchronized long beginGuest() throws IOException {
    if (!loaded || !confirmed || legacy || !"owner".equals(mode))
      throw new IOException("Guest switching is unavailable");
    restrictive = true;
    confirmed = false;
    mode = "blocked";
    epoch++;
    return saved.revision;
  }

  synchronized void persistGuest(long expected) throws IOException {
    if (saved.guestRequested && "guest".equals(saved.mode) && saved.revision > expected) return;
    if (confirmed || !restrictive || saved.revision != expected)
      throw new IOException("Identity changed before guest persistence");
    Saved next = new Saved(true, saved.mode, saved.revision);
    if (!store.write(next)) throw new IOException("Guest choice was not saved");
    saved = next;
  }

  synchronized long requestGuest() throws IOException {
    long revision = beginGuest();
    persistGuest(revision);
    return revision;
  }

  synchronized boolean accept(JSONObject event, long incomingRoute) throws IOException {
    if (!loaded
        || route < 0
        || incomingRoute != route
        || !"identity_state".equals(event.opt("event"))) return false;
    Object raw = event.opt("state"), nonce = event.opt("token"), authority = event.opt("authority");
    if (!(raw instanceof JSONObject)
        || !(nonce instanceof String)
        || !((String) nonce).matches("[a-f0-9]{32}")) return false;
    JSONObject state = (JSONObject) raw;
    Object role = state.opt("mode");
    Long revision = integer(state.opt("revision"));
    Long schema = integer(state.opt("schema"));
    if (state.length() != 6
        || schema == null
        || schema != 1
        || revision == null
        || revision < 0
        || revision > MAX_REVISION
        || !("owner".equals(role) || "guest".equals(role))
        || !Boolean.TRUE.equals(state.opt("ready"))
        || !(state.opt("device_applied") instanceof Boolean)
        || !"desktop_only".equals(state.opt("owner_restore"))
        || !("reconcile".equals(authority) || "owner_desktop".equals(authority))
        || !role.equals(event.opt("identity_mode"))
        || !revision.equals(integer(event.opt("identity_revision")))) return false;
    if (revision < saved.revision || (revision == saved.revision && !role.equals(saved.mode)))
      return false;
    if ("owner".equals(role)
        && (saved.guestRequested || restrictive)
        && (!"owner_desktop".equals(authority) || revision <= saved.revision)) return false;
    Saved next = new Saved("guest".equals(role), (String) role, revision);
    boolean changed = !confirmed || legacy || !mode.equals(next.mode) || revision != saved.revision;
    if (next.guestRequested != saved.guestRequested
        || !next.mode.equals(saved.mode)
        || next.revision != saved.revision) {
      // Close the old scope before persistence; a partial failure cannot keep
      // rendering an owner view or grant a newly requested owner role.
      confirmed = false;
      if (next.guestRequested) restrictive = true;
      mode = "blocked";
      epoch++;
      if (!store.write(next)) throw new IOException("Identity application was not saved");
      saved = next;
    }
    mode = next.mode;
    restrictive = next.guestRequested;
    token = (String) nonce;
    legacy = false;
    confirmed = true;
    if (changed) epoch++;
    return true;
  }

  synchronized boolean acceptLegacy(long incomingRoute) {
    if (!loaded
        || incomingRoute != route
        || route < 0
        || restrictive
        || saved.guestRequested
        || !"owner".equals(saved.mode)) return false;
    confirmed = legacy = true;
    mode = "owner";
    epoch++;
    return true;
  }

  synchronized boolean accepts(JSONObject event, long incomingRoute) {
    if (!confirmed || incomingRoute != route) return false;
    if (legacy) return event.opt("identity_mode") == null && event.opt("identity_revision") == null;
    Long revision = integer(event.opt("identity_revision"));
    return mode.equals(event.opt("identity_mode"))
        && revision != null
        && revision == saved.revision;
  }

  synchronized boolean current(long renderEpoch) {
    return epoch == renderEpoch;
  }

  synchronized long epoch() {
    return epoch;
  }

  synchronized boolean ready() {
    return loaded && confirmed;
  }

  synchronized boolean owner() {
    return ready() && "owner".equals(mode);
  }

  synchronized boolean guestRequested() {
    return !loaded || restrictive || saved.guestRequested;
  }

  synchronized boolean supported() {
    return ready() && !legacy;
  }

  synchronized String mode() {
    return mode;
  }

  synchronized long revision() {
    return saved.revision;
  }

  synchronized String token() {
    return token;
  }

  private static Long integer(Object value) {
    return value instanceof Integer || value instanceof Long ? ((Number) value).longValue() : null;
  }
}
