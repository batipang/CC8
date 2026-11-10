package com.happyhome.cc8assistant;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.IOException;
import java.util.Map;

/** Private, synchronous commit before a mode ACK; corrupt values are never reset to owner. */
final class IdentityPreferences implements IdentityState.Store {
  private final Context context;

  IdentityPreferences(Context context) {
    this.context = context.getApplicationContext();
  }

  private SharedPreferences preferences() {
    return context.getSharedPreferences("identity-v1", Context.MODE_PRIVATE);
  }

  public IdentityState.Saved read() throws IOException {
    try {
      Map<String, ?> values = preferences().getAll();
      if (values.isEmpty()) return new IdentityState.Saved(false, "owner", -1);
      Object schema = values.get("schema"),
          guest = values.get("guest_requested"),
          mode = values.get("mode"),
          revision = values.get("revision");
      if (values.size() != 4
          || !Integer.valueOf(1).equals(schema)
          || !(guest instanceof Boolean)
          || !(mode instanceof String)
          || !(revision instanceof Long)) {
        throw new IOException("Invalid identity storage");
      }
      return new IdentityState.Saved((Boolean) guest, (String) mode, (Long) revision);
    } catch (RuntimeException error) {
      throw new IOException("Identity storage unavailable");
    }
  }

  public boolean write(IdentityState.Saved state) {
    try {
      return preferences()
          .edit()
          .putInt("schema", 1)
          .putBoolean("guest_requested", state.guestRequested)
          .putString("mode", state.mode)
          .putLong("revision", state.revision)
          .commit();
    } catch (RuntimeException error) {
      return false;
    }
  }
}
