package com.happyhome.cc8assistant;

import org.json.JSONObject;

/** Immutable wire page and bounded all-or-nothing clipboard assembly. Never owns a route. */
final class ReportTextPage {
  final String text, marker;
  final int index, count;

  private ReportTextPage(String text, String marker, int index, int count) {
    this.text = text;
    this.marker = marker;
    this.index = index;
    this.count = count;
  }

  static ReportTextPage parse(JSONObject data, String field) {
    Object raw = data.opt(field);
    if (!(raw instanceof String) || ((String) raw).isEmpty() || ((String) raw).length() > 48000)
      return null;
    Object index = data.opt("report_page"), count = data.opt("report_pages");
    Object marker = data.opt("report_page_marker");
    if (index == null && count == null && marker == null)
      return new ReportTextPage((String) raw, "", 0, 1); // Compatible single-page older host.
    if (!(index instanceof Integer) || !(count instanceof Integer) || !(marker instanceof String))
      return null;
    int page = (Integer) index, pages = (Integer) count;
    String prefix = (String) marker;
    if (pages < 1 || pages > 16 || page < 0 || page >= pages) return null;
    String expected = pages == 1 ? "" : "【第" + (page + 1) + "/" + pages + "页；请翻页继续查看】\n\n";
    if (!expected.equals(prefix) || !((String) raw).startsWith(prefix)) return null;
    return new ReportTextPage((String) raw, prefix, page, pages);
  }

  String body() {
    return text.substring(marker.length());
  }

  static final class Copy {
    final String id;
    final int revision, count;
    final long route;
    private final long deadline = System.nanoTime() + 120000000000L;
    private final StringBuilder text = new StringBuilder();
    int next;

    Copy(String id, int revision, int count, long route) {
      this.id = id;
      this.revision = revision;
      this.count = count;
      this.route = route;
    }

    boolean alive() {
      return System.nanoTime() < deadline;
    }

    boolean append(JSONObject data) {
      ReportTextPage page = parse(data, "saved_report_text");
      JSONObject state = data.optJSONObject("saved_reports");
      if (!alive()
          || page == null
          || page.count != count
          || page.index != next
          || !id.equals(data.opt("saved_id"))
          || state == null
          || !Integer.valueOf(revision).equals(state.opt("revision"))
          || text.length() + page.body().length() > 65536) return false;
      text.append(page.body());
      next++;
      return true;
    }

    String completed() {
      return alive() && next == count ? text.toString() : null;
    }
  }
}
