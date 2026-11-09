package com.happyhome.cc8assistant;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import org.json.JSONArray;
import org.json.JSONObject;

/** Copy bounded display data before callbacks; JSON mutation cannot retarget a correction. */
final class VocabularySnapshot {
  static final class Entry {
    final String term, updatedAt;
    final List<String> aliases;

    Entry(String term, List<String> aliases, String updatedAt) {
      this.term = term;
      this.aliases = Collections.unmodifiableList(new ArrayList<>(aliases));
      this.updatedAt = updatedAt;
    }
  }

  final int revision;
  final String sourceId;
  final List<Entry> items;

  private VocabularySnapshot(int revision, List<Entry> items, String sourceId) {
    this.revision = revision;
    this.sourceId = sourceId;
    this.items = Collections.unmodifiableList(new ArrayList<>(items));
  }

  static boolean word(String value) {
    if (!(value != null
        && value.codePointCount(0, value.length()) <= 40
        && PhotoFollowupWindow.validQuestion(value)
        && value.equals(value.trim())
        && !value.contains("\n")
        && !value.contains(",")
        && !value.contains("，")
        && !value.contains("不是"))) return false;
    if (!java.text.Normalizer.normalize(value, java.text.Normalizer.Form.NFC).equals(value))
      return false;
    int first = value.codePointAt(0), last = value.codePointBefore(value.length());
    if (Character.isWhitespace(first)
        || Character.isSpaceChar(first)
        || first == 0x85
        || Character.isWhitespace(last)
        || Character.isSpaceChar(last)
        || last == 0x85) return false;
    for (int i = 0; i < value.length(); ) {
      int point = value.codePointAt(i), type = Character.getType(point);
      if (type == Character.CONTROL
          || type == Character.FORMAT
          || type == Character.PRIVATE_USE
          || type == Character.SURROGATE
          || type == Character.UNASSIGNED) return false;
      i += Character.charCount(point);
    }
    return true;
  }

  static VocabularySnapshot parse(JSONObject state) {
    return parse(state, "");
  }

  static VocabularySnapshot parse(JSONObject state, String sourceId) {
    if (sourceId == null || !(sourceId.isEmpty() || sourceId.matches("[a-f0-9]{32}"))) return null;
    if (state == null
        || !Integer.valueOf(1).equals(state.opt("schema"))
        || !(state.opt("revision") instanceof Integer)) return null;
    int revision = state.optInt("revision", -1);
    JSONArray rows = state.optJSONArray("items");
    if (revision < 0 || revision >= Integer.MAX_VALUE || rows == null || rows.length() > 64)
      return null;
    ArrayList<Entry> entries = new ArrayList<>();
    HashSet<String> terms = new HashSet<>();
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      if (row == null
          || !(row.opt("term") instanceof String)
          || !"device".equals(row.optString("source"))) return null;
      if (!(row.opt("updated_at") instanceof String)) return null;
      String term = row.optString("term"), updated = row.optString("updated_at");
      JSONArray values = row.optJSONArray("aliases");
      if (!word(term)
          || !terms.add(term.toLowerCase(Locale.ROOT))
          || values == null
          || values.length() > 4
          || updated.isEmpty()
          || updated.length() > 40) return null;
      ArrayList<String> aliases = new ArrayList<>();
      HashSet<String> seen = new HashSet<>();
      for (int n = 0; n < values.length(); n++) {
        Object raw = values.opt(n);
        if (!(raw instanceof String)
            || !word((String) raw)
            || term.equals(raw)
            || !seen.add(((String) raw).toLowerCase(Locale.ROOT))) return null;
        aliases.add((String) raw);
      }
      entries.add(new Entry(term, aliases, updated));
    }
    return new VocabularySnapshot(revision, entries, sourceId);
  }
}
