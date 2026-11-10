package com.happyhome.cc8assistant;

import java.net.URI;
import java.util.ArrayList;
import org.json.JSONArray;
import org.json.JSONObject;

/** Immutable source metadata; an operation receipt does not verify page contents. */
final class SourcesPresentation {
  static final class Source {
    final String title, url, evidence, observedAt, publishedAt, pageExcerpt, pageDates, finalUrl;
    final boolean pageRead, detailLimited, listLimited;

    Source(JSONObject row) {
      title = text(row.opt("title"), 160);
      url = text(row.opt("url"), 2048);
      evidence = text(row.opt("access_evidence"), 40);
      observedAt = text(row.opt("observed_at"), 64);
      publishedAt = text(row.opt("published_at"), 64);
      // Host limits count Unicode scalars; Java's bounds count UTF-16 units.
      pageExcerpt = text(row.opt("page_excerpt"), 4000);
      pageDates = text(row.opt("page_dates"), 1088);
      String target = text(row.opt("final_url"), 2048);
      finalUrl = safeUrl(target) ? target : "";
      Object bytes = row.opt("page_bytes");
      pageRead =
          "page_body_received".equals(evidence)
              && !observedAt.isEmpty()
              && bytes instanceof Number
              && ((Number) bytes).doubleValue() == ((Number) bytes).intValue()
              && ((Number) bytes).intValue() > 0
              && ((Number) bytes).intValue() <= 1048576;
      detailLimited = Boolean.TRUE.equals(row.opt("page_details_limited"));
      listLimited = Boolean.TRUE.equals(row.opt("list_limited"));
    }

    private String access() {
      return pageRead
          ? "HTTP 页面已读取，事实仍需核对"
          : "page_fetch_failed".equals(evidence)
              ? "页面读取失败，未取得可用正文"
              : "page_action_completed".equals(evidence)
                  ? "有页面操作完成回执，正文未核实"
                  : "page_action_failed".equals(evidence)
                      ? "页面操作失败，正文未核实"
                      : "search_result_returned".equals(evidence)
                          ? "搜索服务返回，正文未核实"
                          : "仅回答中的链接，读取状态未知";
    }

    String summary() {
      return title + "\n" + URI.create(url).getHost() + " · " + access();
    }

    String label() {
      return title
          + "\n"
          + URI.create(url).getHost()
          + " · "
          + access()
          + (observedAt.isEmpty() ? "" : "\n操作记录：" + observedAt.replace('T', ' '))
          + "\n来源标注日期："
          + (publishedAt.isEmpty() ? "未知" : publishedAt)
          + "；事件日期未知"
          + (pageRead
              ? "\n页面日期标注："
                  + (pageDates.isEmpty() ? "未提供或未展示" : pageDates)
                  + "\n这是页面自述，不是事件日期的核实结果。"
                  + (finalUrl.isEmpty() || finalUrl.equals(url) ? "" : "\n读取的最终地址：" + finalUrl)
                  + "\n\n正文摘录（可能不完整）：\n"
                  + (pageExcerpt.isEmpty() ? "未展示" : pageExcerpt)
              : "")
          + (detailLimited ? "\n部分页面详情受传输大小限制未展示。" : "")
          + (listLimited ? "\n来源列表受传输大小限制，仅显示部分条目。" : "");
    }
  }

  final long route;
  final int generation;
  final String recordedAt, timeBasis;
  private final Source[] sources;

  SourcesPresentation(JSONArray rows, String at, String basis, long route, int generation) {
    this.route = route;
    this.generation = generation;
    recordedAt = text(at, 64);
    timeBasis = text(basis, 32);
    ArrayList<Source> accepted = new ArrayList<>();
    if (rows != null)
      for (int i = 0; i < Math.min(6, rows.length()); i++) {
        JSONObject row = rows.optJSONObject(i);
        if (row == null) continue;
        Source source = new Source(row);
        if (!source.title.isEmpty() && safeUrl(source.url)) accepted.add(source);
      }
    sources = accepted.toArray(new Source[0]);
  }

  int size() {
    return sources.length;
  }

  Source at(int index) {
    return sources[index];
  }

  String heading() {
    String label =
        "search_response".equals(timeBasis)
            ? "检索返回时间"
            : "answer_completed".equals(timeBasis) ? "回答来源汇总时间" : "来源记录时间（依据未提供）";
    return label + "：" + (recordedAt.isEmpty() ? "未知" : recordedAt.replace('T', ' '));
  }

  private static String text(Object value, int max) {
    if (!(value instanceof String)) return "";
    String text = (String) value;
    if (text.length() > max) return "";
    for (int i = 0; i < text.length(); i++) if (Character.isISOControl(text.charAt(i))) return "";
    return text;
  }

  static boolean safeUrl(String value) {
    if (value == null || value.isEmpty() || value.length() > 2048) return false;
    try {
      URI uri = new URI(value);
      String host = uri.getHost();
      if (!("https".equals(uri.getScheme()) || "http".equals(uri.getScheme()))
          || uri.getRawUserInfo() != null
          || host == null
          || host.indexOf(':') >= 0
          || !host.contains(".")
          || (uri.getPort() != -1 && uri.getPort() != 80 && uri.getPort() != 443)) return false;
      host = host.toLowerCase(java.util.Locale.ROOT);
      if (host.endsWith(".")) host = host.substring(0, host.length() - 1);
      if (host.endsWith(".local") || host.endsWith(".localhost") || host.endsWith(".internal"))
        return false;
      if (host.matches("(?i)(?:0x[0-9a-f]+|[0-9]+)(?:\\.(?:0x[0-9a-f]+|[0-9]+))*")
          && !host.matches("[0-9.]+")) return false;
      if (host.matches("[0-9.]+")) {
        String[] parts = host.split("\\.", -1);
        if (parts.length != 4) return false;
        int[] ip = new int[4];
        for (int i = 0; i < 4; i++) {
          ip[i] = Integer.parseInt(parts[i]);
          if (ip[i] > 255 || !Integer.toString(ip[i]).equals(parts[i])) return false;
        }
        if (ip[0] == 0
            || ip[0] == 10
            || ip[0] == 127
            || ip[0] >= 224
            || ip[0] == 169 && ip[1] == 254
            || ip[0] == 172 && ip[1] >= 16 && ip[1] <= 31
            || ip[0] == 192 && ip[1] == 168
            || ip[0] == 100 && ip[1] >= 64 && ip[1] <= 127) return false;
      }
      return true;
    } catch (Exception invalid) {
      return false;
    }
  }
}
