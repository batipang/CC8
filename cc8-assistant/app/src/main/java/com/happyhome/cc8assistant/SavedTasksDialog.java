package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.os.Handler;
import android.os.Looper;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONObject;

/** Owner-selected historical result snapshots; opening the page never starts capture. */
final class SavedTasksDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final ResultDetailsDialog resultDetails;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private String pending = "", action = "";
  private long pendingRoute = -1;
  private AlertDialog progress;
  private Runnable timeout;

  SavedTasksDialog(Activity activity, VoiceSession session, ResultDetailsDialog resultDetails) {
    this.activity = activity;
    this.session = session;
    this.resultDetails = resultDetails;
  }

  void open() {
    request("list", null, session.connectedRoute());
  }

  void save(String id, long route) {
    if (!available(route)) return;
    new LargeDialogBuilder(activity)
        .setTitle("保存这条任务结果？")
        .setMessage(
            "在这台 Mac 保存任务标题、结束状态与时间、最近五个步骤、最多 1600 字结果摘录，以及可用的会话来源和最多 8"
                + " 条文件变更回执（含本机路径）。分析 Mac 窗口时也保存应用名、窗口标题、编号和截图尺寸。重启后可查看或删除记录；不保存截图、录音或全部聊天。")
        .setPositiveButton("保存", (d, n) -> request("save", id, route))
        .setNegativeButton("取消", null)
        .show();
  }

  void close() {
    pending = "";
    pendingRoute = -1;
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    if (progress != null) progress.dismiss();
    progress = null;
  }

  private boolean available(long route) {
    if (route >= 0 && session.currentRoute(route) && session.isConnected()) return true;
    toast("连接已变化，请重新打开已保存结果或任务详情并核对结果");
    return false;
  }

  private void request(String nextAction, String id, long route) {
    if (!available(route)) return;
    close();
    if (!session.supportsSavedTaskResults()) {
      toast("当前后台尚未确认支持保存结果，请完成连接或更新后台");
      return;
    }
    pending = UUID.randomUUID().toString().replace("-", "");
    pendingRoute = route;
    action = nextAction;
    String requestId = pending;
    progress =
        new LargeDialogBuilder(activity)
            .setTitle("已保存结果")
            .setMessage(
                "open_handoff".equals(nextAction) ? "正在请求 Mac 打开原聊天…" : "正在处理…关闭等待不会撤销已经提交的保存或删除。")
            .setNegativeButton("关闭等待", (d, n) -> close())
            .create();
    progress.setOnCancelListener(d -> close());
    progress.show();
    timeout =
        () -> {
          if (!requestId.equals(pending)) return;
          close();
          toast("open_handoff".equals(nextAction) ? "打开请求未确认，请在 Mac 核对" : "保存记录状态未确认，请刷新查看");
        };
    handler.postDelayed(timeout, 15000);
    if (!session.savedTaskRequest(requestId, nextAction, id, route)) {
      close();
      toast("连接已变化或请求未能提交，请重新打开并核对结果");
    }
  }

  void accept(JSONObject data) {
    if (pending.isEmpty() || !pending.equals(data.optString("request_id"))) return;
    if (!session.currentRoute(pendingRoute)) {
      close();
      return;
    }
    final long route = pendingRoute;
    String completedAction = action;
    close();
    if (!data.optBoolean("ok")) {
      toast(data.optString("message", "保存记录状态未确认，请刷新查看"));
      return;
    }
    if ("list".equals(completedAction)) showList(data.optJSONArray("items"), route);
    else if ("get".equals(completedAction)) showDetail(data.optJSONObject("item"), route);
    else if ("delete".equals(completedAction)) {
      toast(data.optBoolean("deleted") ? "已删除保存记录，原任务不变" : "这条记录已不在列表中");
      request("list", null, route);
    } else if ("open_handoff".equals(completedAction)) {
      toast(
          "open_handoff".equals(data.optString("action"))
                  && Boolean.TRUE.equals(data.opt("open_requested"))
              ? "已请求 Mac 打开原聊天，请在桌面核对后提交草稿"
              : "打开请求未确认，请在 Mac 核对");
    } else toast("结果已保存在 Mac，可在“已保存结果”中查看");
  }

  private void showList(JSONArray items, long route) {
    if (items == null || items.length() == 0) {
      new LargeDialogBuilder(activity)
          .setTitle("已保存结果")
          .setMessage("还没有保存的结果。请在任务详情中明确选择“保存结果”；日常任务不会自动保存。")
          .setPositiveButton("关闭", null)
          .show();
      return;
    }
    String[] labels = new String[items.length()];
    for (int i = 0; i < labels.length; i++) {
      JSONObject item = items.optJSONObject(i);
      labels[i] = item == null ? "保存记录" : receiptLabel(item) + " · " + item.optString("title");
    }
    new LargeDialogBuilder(activity)
        .setTitle("已保存结果 · Mac 本机")
        .setItems(
            labels,
            (d, n) -> {
              JSONObject item = items.optJSONObject(n);
              if (item != null) request("get", item.optString("id"), route);
            })
        .setPositiveButton("关闭", null)
        .show();
  }

  private void showDetail(JSONObject item, long route) {
    if (item == null) {
      toast("未找到保存记录，请刷新");
      return;
    }
    StringBuilder body =
        new StringBuilder(
            "历史快照 · 不会自动重跑任务\n保存于：" + item.optString("saved_at") + "\n状态：" + receiptLabel(item));
    JSONArray steps = item.optJSONArray("steps");
    if (steps != null)
      for (int i = 0; i < steps.length(); i++) {
        JSONObject step = steps.optJSONObject(i);
        if (step != null)
          body.append("\n\n")
              .append(step.optString("title"))
              .append(" · ")
              .append(label(step.optString("status")));
      }
    body.append("\n\n结果摘录：\n").append(item.optString("result"));
    if (needsExecutionCheck(item)) body.append("\n\n请先在 Mac 核对原任务是否执行，再决定是否重新交办。");
    TextView detail = new TextView(activity);
    detail.setText(body.toString());
    detail.setTextSize(30);
    detail.setTextIsSelectable(true);
    detail.setPadding(24, 18, 24, 18);
    LinearLayout content = new LinearLayout(activity);
    content.setOrientation(LinearLayout.VERTICAL);
    content.addView(detail);
    if (Boolean.TRUE.equals(item.opt("desktop_handoff_can_open"))) {
      Button openChat = new Button(activity);
      openChat.setText("在 Mac 打开原聊天");
      openChat.setTextSize(28);
      openChat.setOnClickListener(
          v -> {
            if (!available(route)) return;
            new LargeDialogBuilder(activity)
                .setTitle("在 Mac 打开原聊天？")
                .setMessage("打开这份回执对应的 Codex 聊天。请在 Mac 核对聊天和草稿，再手动提交。")
                .setPositiveButton(
                    "打开原聊天", (d, n) -> request("open_handoff", item.optString("id"), route))
                .setNegativeButton("取消", null)
                .show();
          });
      content.addView(openChat);
    }
    if (item.optJSONObject("provenance") != null || item.optJSONObject("window_input") != null) {
      Button source = new Button(activity);
      source.setText("来源与文件");
      source.setTextSize(28);
      source.setOnClickListener(v -> resultDetails.open(item, true, route));
      content.addView(source);
    }
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(content);
    new LargeDialogBuilder(activity)
        .setTitle(item.optString("title"))
        .setView(scroll)
        .setPositiveButton("关闭", null)
        .setNeutralButton(
            "复制摘录",
            (d, n) -> {
              ClipboardManager clipboard =
                  (ClipboardManager) activity.getSystemService(Activity.CLIPBOARD_SERVICE);
              clipboard.setPrimaryClip(
                  ClipData.newPlainText(item.optString("title"), body.toString()));
              toast("已复制结果摘录");
            })
        .setNegativeButton(
            "删除保存记录",
            (d, n) ->
                new LargeDialogBuilder(activity)
                    .setTitle("删除这条保存记录？")
                    .setMessage("仅删除 Mac 上的这份结果快照，不撤销已执行操作，也不删除结果文件或桌面聊天。")
                    .setPositiveButton(
                        "删除", (confirm, which) -> request("delete", item.optString("id"), route))
                    .setNegativeButton("取消", null)
                    .show())
        .show();
  }

  private String receiptLabel(JSONObject item) {
    String status = item.optString("status");
    if ("completed".equals(status) && Boolean.TRUE.equals(item.opt("desktop_handoff_pending")))
      return "待桌面接续";
    if (needsExecutionCheck(item)) return "执行结果待核对";
    return label(status);
  }

  private boolean needsExecutionCheck(JSONObject item) {
    String status = item.optString("status");
    return ("failed".equals(status) || "cancelled".equals(status))
        && Boolean.TRUE.equals(item.opt("execution_unconfirmed"));
  }

  private String label(String status) {
    if ("completed".equals(status)) return "已完成";
    if ("failed".equals(status)) return "未完成";
    if ("cancelled".equals(status)) return "已停止等待";
    return "保存时未确认";
  }

  private void toast(String text) {
    Toast.makeText(activity, text, Toast.LENGTH_LONG).show();
  }
}
