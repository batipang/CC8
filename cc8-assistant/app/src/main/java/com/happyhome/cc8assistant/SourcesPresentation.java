package com.happyhome.cc8assistant;

import java.net.URI;
import java.util.ArrayList;
import org.json.JSONArray;
import org.json.JSONObject;

/** Immutable source metadata; an operation receipt does not verify page contents. */
final class SourcesPresentation {
  static final class Source {
    final String title, url, evidence, observedAt, publishedAt, pageExcerpt, pageDates, finalUrl;
    final String pageModifiedDates;
    final boolean pageRead, detailLimited, listLimited, uncitedRead;
    final int pageBytes, textChars, shownChars, httpStatus;
    final Boolean textTruncated;
    final String errorCode;

    Source(JSONObject row) {
      title = text(row.opt("title"), 160);
      url = text(row.opt("url"), 2048);
      evidence = text(row.opt("access_evidence"), 40);
      observedAt = text(row.opt("observed_at"), 64);
      publishedAt = text(row.opt("published_at"), 64);
      pageExcerpt = text(row.opt("page_excerpt"), 2000);
      pageDates = text(row.opt("page_dates"), 519); // Eight 64-scalar labels and seven separators.
      pageModifiedDates = text(row.opt("page_modified_dates"), 519);
      String target = text(row.opt("final_url"), 2048);
      finalUrl = safeUrl(target) ? target : "";
      int bytes = integer(row.opt("page_bytes"), 1048576);
      pageRead = "page_body_received".equals(evidence) && bytes > 0;
      pageBytes = pageRead ? bytes : 0;
      textChars = integer(row.opt("page_text_chars"), 20000);
      shownChars = pageExcerpt.codePointCount(0, pageExcerpt.length());
      Object truncated = row.opt("page_text_truncated");
      textTruncated =
          pageRead && textChars >= shownChars && textChars > 0 && truncated instanceof Boolean
              ? (Boolean) truncated
              : null;
      int status = integer(row.opt("page_http_status"), 599);
      httpStatus = status >= 100 ? status : 0;
      errorCode = text(row.opt("page_error_code"), 40);
      uncitedRead =
          Boolean.FALSE.equals(row.opt("answer_cited"))
              && (pageRead || "page_fetch_failed".equals(evidence));
      detailLimited = Boolean.TRUE.equals(row.opt("page_details_limited"));
      listLimited = Boolean.TRUE.equals(row.opt("list_limited"));
    }

    private String observationDetails() {
      if (pageRead || "page_fetch_failed".equals(evidence)) {
        String time = beijingTime(observedAt);
        return "\n页面读取结束的北京时间 " + (time.isEmpty() ? "未知" : time);
      }
      return observedAt.isEmpty() ? "" : "\n操作记录：" + observedAt.replace('T', ' ');
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
      return title
          + "\n"
          + URI.create(url).getHost()
          + " · "
          + access()
          + (uncitedRead ? "\n回答未引用" : "");
    }

    private String failureDetails() {
      if (!"page_fetch_failed".equals(evidence)) return "";
      return "\n读取原因："
          + failureReason(errorCode)
          + "\nHTTP 响应："
          + (httpStatus == 0 ? "未取得可核对状态" : Integer.toString(httpStatus))
          + "；未取得可用正文。"
          + (finalUrl.isEmpty() || finalUrl.equals(url) ? "" : "\n最后尝试的地址：" + finalUrl);
    }

    private String coverageDetails() {
      String size = "\n接收页面数据：" + pageBytes + " 字节";
      if (textChars > 0 && shownChars <= textChars) {
        size += "\n本次保留文字：" + textChars + " 字符；当前摘录：" + shownChars + " 字符";
        if (shownChars < textChars) size += "\n其余已保留文字未在本页展示。";
      } else size += "\n文字数量：未提供可核对记录";
      if (Boolean.TRUE.equals(textTruncated)) {
        size += "\n页面文字超过单次读取上限，仅保留前段；后文未核对。";
      } else if (Boolean.FALSE.equals(textTruncated)) {
        size += "\n读取器未因文字上限截断；仍需核对页面解析和事实。";
      } else size += "\n读取器截断状态：未提供。";
      return size;
    }

    String label() {
      return summary()
          + observationDetails()
          + failureDetails()
          + "\n来源标注日期："
          + (publishedAt.isEmpty() ? "未知" : publishedAt)
          + "；事件日期未知"
          + (pageRead
              ? coverageDetails()
                  + "\n页面发布日期标注："
                  + (pageDates.isEmpty() ? "未提供或未展示" : pageDates)
                  + "\n页面更新日期标注："
                  + (pageModifiedDates.isEmpty() ? "未提供或未展示" : pageModifiedDates)
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
  private final String questionTime;
  private final Source[] sources;

  SourcesPresentation(
      JSONArray rows, String at, String basis, Object requestedAt, long route, int generation) {
    this.route = route;
    this.generation = generation;
    recordedAt = text(at, 64);
    timeBasis = text(basis, 32);
    questionTime = beijingTime(requestedAt);
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

  String details(int index) {
    String label = at(index).label();
    if (questionTime.isEmpty()) return label;
    return "查询参考的北京时间 " + questionTime + "\n\n" + label;
  }

  private static String beijingTime(Object raw) {
    String value = text(raw, 25);
    if (!value.matches("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\+08:00")) return "";
    java.text.SimpleDateFormat format =
        new java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'+08:00'", java.util.Locale.US);
    format.setLenient(false);
    format.setTimeZone(java.util.TimeZone.getTimeZone("GMT+08:00"));
    if (format.parse(value, new java.text.ParsePosition(0)) == null) return "";
    return value.substring(0, 19).replace('T', ' ');
  }

  private static String text(Object value, int max) {
    if (!(value instanceof String)) return "";
    String text = (String) value;
    // Match Python's scalar limits without accepting broken surrogate pairs.
    if (text.length() > max * 2) return "";
    int scalars = 0;
    for (int i = 0; i < text.length(); i++) {
      if (++scalars > max) return "";
      char c = text.charAt(i);
      if (Character.isHighSurrogate(c)) {
        if (i + 1 >= text.length() || !Character.isLowSurrogate(text.charAt(++i))) return "";
      } else if (Character.isLowSurrogate(c) || Character.isISOControl(c)) return "";
    }
    return text;
  }

  private static int integer(Object raw, int max) {
    if (!(raw instanceof Integer) && !(raw instanceof Long)) return 0;
    long value = ((Number) raw).longValue();
    return value > 0 && value <= max ? (int) value : 0;
  }

  private static String failureReason(String code) {
    switch (code) {
      case "read_failed":
        return "本次页面读取未完成";
      case "timeout":
        return "读取超过本次等待时间";
      case "network_failed":
        return "网络连接或传输未完成";
      case "dns_busy":
        return "本机域名解析任务繁忙，本次未读取";
      case "dns_failed":
        return "来源域名解析失败";
      case "non_public_address":
        return "来源解析到不允许读取的地址";
      case "page_too_large":
        return "页面数据超过单次读取上限";
      case "invalid_headers":
      case "ambiguous_headers":
        return "来源响应头无法可靠解析";
      case "http_error":
        return "来源服务器未返回可读取页面";
      case "unsupported_content_type":
        return "当前不支持此页面格式";
      case "unsupported_content_encoding":
        return "当前不支持此响应压缩方式";
      case "unsupported_encoding":
        return "页面不是可解码的 UTF-8 文字";
      case "no_readable_text":
        return "页面中未提取到可读文字";
      case "redirect_loop":
      case "redirect_limit":
        return "来源跳转重复或超过本次上限";
      case "invalid_url":
      case "invalid_redirect":
      case "insecure_redirect":
        return "来源地址或跳转不符合读取条件";
      case "cancelled":
        return "本次读取已取消";
      default:
        return "未提供可核对的原因";
    }
  }

  static boolean safeUrl(String value) {
    if (text(value, 2048).isEmpty()) return false;
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
