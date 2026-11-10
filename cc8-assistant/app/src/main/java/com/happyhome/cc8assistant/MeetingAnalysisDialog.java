package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.os.SystemClock;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Button;
import android.widget.LinearLayout;
import org.json.JSONArray;
import org.json.JSONObject;

/** Passive complete-document viewer; no capture, provider, disk write or task execution. */
final class MeetingAnalysisDialog {
  interface Port {
    long epoch();

    boolean current(long owner);

    JSONObject state();

    void request(JSONObject args);

    void toast(String message);
  }

  private final Activity activity;
  private final Port port;
  private AlertDialog child;
  private String readingAnalysis = "", readingMeeting = "", analysisHash = "";
  private int analysisOffset, analysisLength;
  private long analysisDeadline;
  private StringBuilder analysisText;

  MeetingAnalysisDialog(Activity activity, Port port) {
    this.activity = activity;
    this.port = port;
  }

  void close() {
    failed();
    if (child != null) child.dismiss();
    child = null;
    readingAnalysis = readingMeeting = analysisHash = "";
  }

  void failed() {
    analysisText = null;
  }

  boolean isOpen() {
    return child != null || analysisText != null;
  }

  private JSONObject args(String action) {
    JSONObject result = new JSONObject();
    Wire.put(result, "action", action);
    return result;
  }

  private void showChild(AlertDialog value) {
    if (child != null) child.dismiss();
    child = value;
    value.setOnDismissListener(
        d -> {
          if (child == value) child = null;
        });
    value.show();
  }

  void chooseTranslation() {
    final long owner = port.epoch(), revision = port.state().optLong("revision");
    final String meeting = port.state().optString("id");
    showChild(
        new LargeDialogBuilder(activity)
            .setTitle("交给 Dot 翻译会议文字")
            .setItems(
                new String[] {"翻译为中文", "翻译为英文"},
                (d, n) -> {
                  if (!port.current(owner) || child != d || n < 0 || n > 1) return;
                  child = null;
                  JSONObject args = args("translate");
                  Wire.put(args, "id", meeting);
                  Wire.put(args, "revision", revision);
                  Wire.put(args, "language", n == 0 ? "zh" : "en");
                  Wire.put(args, "confirmed", true);
                  port.request(args);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  void openResults() {
    JSONArray values = port.state().optJSONArray("analyses");
    if (values == null || values.length() > 3) return;
    failed();
    final String meeting = port.state().optString("id");
    final String[] ids = new String[values.length()];
    String[] labels = new String[values.length()];
    final long owner = port.epoch();
    for (int i = 0; i < values.length(); i++) {
      JSONObject value = values.optJSONObject(i);
      if (value == null || !value.optString("id").matches("[a-f0-9]{32}")) return;
      ids[i] = value.optString("id");
      labels[i] =
          ("summarize".equals(value.optString("kind"))
                  ? "会议总结"
                  : "zh".equals(value.optString("language")) ? "中文翻译" : "英文翻译")
              + (value.optBoolean("stale") ? " · 依据旧文字，请重新生成" : " · 请核对")
              + " · 源修订 "
              + value.optLong("source_revision");
    }
    showChild(
        new LargeDialogBuilder(activity)
            .setTitle("Dot 会议文字分析")
            .setItems(
                labels,
                (d, n) -> {
                  if (!port.current(owner) || child != d || n < 0 || n >= ids.length) return;
                  child = null;
                  readingMeeting = meeting;
                  readingAnalysis = ids[n];
                  analysisText = new StringBuilder();
                  analysisDeadline = SystemClock.elapsedRealtime() + 30000;
                  analysisHash = "";
                  requestAnalysisPage(0);
                })
            .setNegativeButton("关闭", null)
            .create());
  }

  private void requestAnalysisPage(int offset) {
    analysisOffset = offset;
    JSONObject args = args("analysis_text");
    Wire.put(args, "id", readingMeeting);
    Wire.put(args, "analysis_id", readingAnalysis);
    Wire.put(args, "offset", offset);
    port.request(args);
  }

  void acceptPage(JSONObject page) {
    if (analysisText == null) return;
    if (page == null
        || SystemClock.elapsedRealtime() > analysisDeadline
        || !readingAnalysis.equals(page.optString("id"))
        || page.optInt("offset", -1) != analysisOffset) {
      analysisText = null;
      port.toast("完整结果未确认，请重新查看；未显示部分结果。");
      return;
    }
    String hash = page.optString("result_sha256"), part = page.optString("text");
    int length = page.optInt("characters", -1);
    if (!hash.matches("[a-f0-9]{64}")
        || length < 1
        || length > 524288
        || (!analysisHash.isEmpty() && (!hash.equals(analysisHash) || length != analysisLength))) {
      analysisText = null;
      port.toast("结果回执已变化，请重新查看。");
      return;
    }
    analysisHash = hash;
    analysisLength = length;
    analysisText.append(part);
    if (analysisText.length() > 1048576) {
      analysisText = null;
      port.toast("结果超出显示上限，请导出查看。");
      return;
    }
    int next = analysisOffset + part.codePointCount(0, part.length());
    if (!page.isNull("next_offset")) {
      if (part.isEmpty() || page.optInt("next_offset", -1) != next || next >= analysisLength) {
        analysisText = null;
        port.toast("结果分页未确认，请重新查看。");
        return;
      }
      requestAnalysisPage(next);
      return;
    }
    final String complete = analysisText.toString();
    analysisText = null;
    if (next != analysisLength || !sha256(complete).equals(analysisHash)) {
      port.toast("完整结果校验失败，请重新查看；未显示部分结果。");
      return;
    }
    showComplete(complete, page.optBoolean("stale"));
  }

  private void showComplete(String complete, boolean stale) {
    final long owner = port.epoch();
    final java.util.ArrayList<Integer> cuts = new java.util.ArrayList<>();
    cuts.add(0);
    for (int position = 0; position < complete.length(); ) {
      int end = Math.min(position + 4096, complete.length());
      if (end < complete.length() && Character.isHighSurrogate(complete.charAt(end - 1))) end--;
      cuts.add(end);
      position = end;
    }
    final int[] page = {0};
    LinearLayout panel = new LinearLayout(activity);
    panel.setOrientation(LinearLayout.VERTICAL);
    LinearLayout controls = new LinearLayout(activity);
    Button previous = new Button(activity), next = new Button(activity);
    previous.setText("上一页");
    next.setText("下一页");
    previous.setTextSize(24);
    next.setTextSize(24);
    TextView position = new TextView(activity);
    position.setTextSize(24);
    controls.addView(previous);
    controls.addView(position);
    controls.addView(next);
    panel.addView(controls);
    TextView body = new TextView(activity);
    body.setTextSize(26);
    body.setTextIsSelectable(true);
    body.setPadding(24, 16, 24, 16);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(body);
    panel.addView(scroll);
    AlertDialog value =
        new LargeDialogBuilder(activity)
            .setTitle(stale ? "依据旧文字 · 请重新生成" : "Dot 结果 · 请核对")
            .setView(panel)
            .setNegativeButton("关闭", null)
            .setNeutralButton(
                "复制完整结果",
                (d, n) -> {
                  if (!port.current(owner) || child != d) return;
                  ((ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE))
                      .setPrimaryClip(ClipData.newPlainText("会议分析", complete));
                })
            .create();
    Runnable render =
        () -> {
          body.setText(complete.substring(cuts.get(page[0]), cuts.get(page[0] + 1)));
          position.setText("第 " + (page[0] + 1) + " / " + (cuts.size() - 1) + " 页");
          previous.setEnabled(page[0] > 0);
          next.setEnabled(page[0] + 1 < cuts.size() - 1);
          scroll.scrollTo(0, 0);
        };
    previous.setOnClickListener(
        v -> {
          if (port.current(owner) && child == value && page[0] > 0) {
            page[0]--;
            render.run();
          }
        });
    next.setOnClickListener(
        v -> {
          if (port.current(owner) && child == value && page[0] + 1 < cuts.size() - 1) {
            page[0]++;
            render.run();
          }
        });
    render.run();
    showChild(value);
  }

  private static String sha256(String text) {
    try {
      byte[] bytes =
          java.security.MessageDigest.getInstance("SHA-256")
              .digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
      StringBuilder out = new StringBuilder();
      for (byte value : bytes)
        out.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
      return out.toString();
    } catch (java.security.NoSuchAlgorithmException e) {
      return "";
    }
  }
}
