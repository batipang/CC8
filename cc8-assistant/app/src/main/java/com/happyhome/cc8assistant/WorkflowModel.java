package com.happyhome.cc8assistant;

import java.util.HashSet;
import java.util.Iterator;
import org.json.JSONArray;
import org.json.JSONObject;

/** Bounded display snapshots; editor mutation never retargets a received offer. */
final class WorkflowModel {
  static String id() {
    return java.util.UUID.randomUUID().toString().replace("-", "");
  }

  static boolean token(Object value) {
    return value instanceof String && ((String) value).matches("[a-f0-9]{32}");
  }

  static boolean text(Object raw, int limit) {
    if (!(raw instanceof String)) return false;
    String value = (String) raw;
    if (value.isEmpty() || value.codePointCount(0, value.length()) > limit) return false;
    for (int i = 0; i < value.length(); i++) {
      char c = value.charAt(i);
      if (c < 32 && c != '\n' && c != '\t') return false;
      if (Character.isHighSurrogate(c)) {
        if (++i >= value.length() || !Character.isLowSurrogate(value.charAt(i))) return false;
      } else if (Character.isLowSurrogate(c)) return false;
    }
    return true;
  }

  static boolean label(Object raw, int limit) {
    if (!text(raw, limit)) return false;
    String value = (String) raw;
    int first = value.codePointAt(0), last = value.codePointBefore(value.length());
    return value.equals(value.trim())
        && !Character.isSpaceChar(first)
        && !Character.isSpaceChar(last)
        && !Character.isWhitespace(first)
        && !Character.isWhitespace(last)
        && first != 0x85
        && last != 0x85;
  }

  static Object copy(Object value) {
    if (value instanceof JSONObject) {
      JSONObject result = new JSONObject();
      Iterator<String> keys = ((JSONObject) value).keys();
      while (keys.hasNext()) {
        String key = keys.next();
        Wire.put(result, key, copy(((JSONObject) value).opt(key)));
      }
      return result;
    }
    if (value instanceof JSONArray) {
      JSONArray result = new JSONArray();
      JSONArray rows = (JSONArray) value;
      for (int i = 0; i < rows.length(); i++) result.put(copy(rows.opt(i)));
      return result;
    }
    return value;
  }

  static boolean source(JSONObject value) {
    if (value == null
        || value.length() != 4
        || !(value.opt("source_id") instanceof String)
        || !value.optString("source_id").matches("[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
        || !label(value.opt("source_title"), 80)) return false;
    JSONArray rows = value.optJSONArray("members");
    if (rows == null || rows.length() > 8) return false;
    if ("selected".equals(value.opt("kind"))) return rows.length() == 0;
    if (!"registered".equals(value.opt("kind")) || rows.length() == 0) return false;
    HashSet<String> ids = new HashSet<>(), threads = new HashSet<>();
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      if (row == null
          || row.length() != 4
          || !(row.opt("registration_id") instanceof String)
          || !row.optString("registration_id").matches("[a-f0-9]{16}")
          || !(row.opt("thread_id") instanceof String)
          || !row.optString("thread_id").matches("[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
          || !label(row.opt("title"), 80)
          || !label(row.opt("project"), 80)
          || !ids.add(row.optString("registration_id"))
          || !threads.add(row.optString("thread_id"))) return false;
    }
    return true;
  }

  static boolean kind(String value) {
    return "assistant".equals(value)
        || "web_search".equals(value)
        || "work_report".equals(value)
        || "ocr".equals(value);
  }

  static boolean stamp(Object raw) {
    return (raw instanceof Integer || raw instanceof Long)
        && ((Number) raw).longValue() >= 0
        && ((Number) raw).longValue() <= 253402300799L;
  }

  static boolean schedule(JSONObject value) {
    if (value == null
        || !(value.opt("enabled") instanceof Boolean)
        || !stamp(value.opt("configured_at"))) return false;
    if ("interval".equals(value.opt("kind")))
      return value.length() == 4
          && value.opt("seconds") instanceof Integer
          && (value.optInt("seconds") == 300
              || value.optInt("seconds") == 900
              || value.optInt("seconds") == 1800);
    if (!"daily".equals(value.opt("kind"))
        || value.length() != 5
        || !(value.opt("minute_of_day") instanceof Integer)
        || value.optInt("minute_of_day") < 0
        || value.optInt("minute_of_day") > 1439) return false;
    JSONArray days = value.optJSONArray("weekdays");
    if (days == null || days.length() < 1 || days.length() > 7) return false;
    HashSet<Integer> seen = new HashSet<>();
    for (int i = 0; i < days.length(); i++) {
      Object day = days.opt(i);
      if (!(day instanceof Integer)
          || (Integer) day < 0
          || (Integer) day > 6
          || !seen.add((Integer) day)) return false;
    }
    return true;
  }

  static JSONObject definition(JSONObject raw) {
    if (raw == null || raw.length() != 4 || !token(raw.opt("id")) || !label(raw.opt("name"), 40))
      return null;
    JSONArray steps = raw.optJSONArray("steps");
    if (steps == null || steps.length() < 1 || steps.length() > 4) return null;
    HashSet<String> ids = new HashSet<>();
    boolean safe = true;
    for (int i = 0; i < steps.length(); i++) {
      JSONObject row = steps.optJSONObject(i);
      if (row == null
          || !token(row.opt("id"))
          || !ids.add(row.optString("id"))
          || !label(row.opt("title"), 40)
          || !kind(row.optString("kind"))) return null;
      String kind = row.optString("kind");
      if ("work_report".equals(kind)) {
        if (row.length() != 5
            || !("half_hour".equals(row.opt("period"))
                || "two_hours".equals(row.opt("period"))
                || "today".equals(row.opt("period")))) return null;
        if (!row.has("source")) return null;
        if (row.isNull("source")) safe = false;
        else if (!source(row.optJSONObject("source"))) return null;
      } else {
        if (row.length() != 4 || !label(row.opt("text"), 500) || "ocr".equals(kind) && i != 0)
          return null;
        if (!"web_search".equals(kind)) safe = false;
      }
    }
    if (!raw.has("schedule")) return null;
    if (!raw.isNull("schedule")) {
      JSONObject scheduled = raw.optJSONObject("schedule");
      if (!schedule(scheduled) || Boolean.TRUE.equals(scheduled.opt("enabled")) && !safe)
        return null;
    }
    return (JSONObject) copy(raw);
  }

  static String kindName(String kind) {
    return "assistant".equals(kind)
        ? "助理交办"
        : "web_search".equals(kind) ? "只读联网检索" : "work_report".equals(kind) ? "只读工作汇报" : "本次取景识字";
  }

  static boolean run(JSONObject value) {
    if (value == null
        || !token(value.opt("id"))
        || !token(value.opt("workflow_id"))
        || !label(value.opt("name"), 40)
        || !Boolean.FALSE.equals(value.opt("independently_verified"))
        || !("manual".equals(value.opt("origin")) || "scheduled".equals(value.opt("origin"))))
      return false;
    String state = value.optString("state");
    if (!("prepared".equals(state)
        || "running".equals(state)
        || "completed".equals(state)
        || "failed".equals(state)
        || "cancelled".equals(state))) return false;
    JSONArray rows = value.optJSONArray("steps");
    if (rows == null || rows.length() < 1 || rows.length() > 4) return false;
    HashSet<String> ids = new HashSet<>();
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      if (row == null
          || !token(row.opt("id"))
          || !ids.add(row.optString("id"))
          || !label(row.opt("title"), 40)
          || !kind(row.optString("kind"))) return false;
      String status = row.optString("state");
      if (!("pending".equals(status)
              || "running".equals(status)
              || "completed".equals(status)
              || "unconfirmed".equals(status))
          || "completed".equals(state) && !"completed".equals(status)) return false;
    }
    return true;
  }

  static String stateName(String value) {
    return "completed".equals(value)
        ? "已完成"
        : "running".equals(value)
            ? "执行中"
            : "prepared".equals(value)
                ? "已受理，等待执行"
                : "pending".equals(value)
                    ? "未执行"
                    : "cancelled".equals(value)
                        ? "已停止等待"
                        : "failed".equals(value)
                            ? "未全部完成"
                            : "claimed".equals(value)
                                ? "已领取，结果待核对"
                                : "not_started".equals(value)
                                    ? "未开始播报"
                                    : "unconfirmed".equals(value) ? "回执未确认" : "状态未知";
  }

  static String describe(JSONObject flow) {
    StringBuilder text = new StringBuilder(flow.optString("name"));
    JSONArray rows = flow.optJSONArray("steps");
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      text.append("\n\n")
          .append(i + 1)
          .append(". ")
          .append(row.optString("title"))
          .append(" · ")
          .append(kindName(row.optString("kind")));
      if ("work_report".equals(row.optString("kind"))) {
        JSONObject source = row.optJSONObject("source");
        text.append("\n来源：")
            .append(source == null ? "运行前使用本次选定的汇报来源" : source.optString("source_title"));
        String period = row.optString("period");
        text.append("\n范围：")
            .append("today".equals(period) ? "今天" : "half_hour".equals(period) ? "过去半小时" : "过去两小时");
      } else text.append("\n").append(row.optString("text"));
    }
    return text.toString();
  }
}
