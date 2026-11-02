package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.os.Handler;
import android.os.Looper;
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
  private final Handler handler = new Handler(Looper.getMainLooper());
  private String pending = "", action = "";
  private AlertDialog progress;
  private Runnable timeout;

  SavedTasksDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  void open() {
    request("list", null);
  }

  void save(String id) {
    new AlertDialog.Builder(activity)
        .setTitle("保存这条任务结果？")
        .setMessage("在这台 Mac 保存任务标题、结束状态、最近五个步骤及最多 1600 字结果摘录。重启后仍可查看，可在“已保存结果”中删除；不保存录音或全部聊天。")
        .setPositiveButton("保存", (d, n) -> request("save", id))
        .setNegativeButton("取消", null)
        .show();
  }

  void close() {
    pending = "";
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    if (progress != null) progress.dismiss();
    progress = null;
  }

  private void request(String nextAction, String id) {
    close();
    if (!session.isConnected()) {
      toast("请先连接 Mac");
      return;
    }
    if (!session.supportsSavedTaskResults()) {
      toast("当前后台尚未确认支持保存结果，请完成连接或更新后台");
      return;
    }
    pending = UUID.randomUUID().toString().replace("-", "");
    action = nextAction;
    String requestId = pending;
    progress =
        new AlertDialog.Builder(activity)
            .setTitle("已保存结果")
            .setMessage("正在处理…关闭等待不会撤销已经提交的保存或删除。")
            .setNegativeButton("关闭等待", (d, n) -> close())
            .create();
    progress.setOnCancelListener(d -> close());
    progress.show();
    timeout =
        () -> {
          if (!requestId.equals(pending)) return;
          close();
          toast("保存记录状态未确认，请刷新查看");
        };
    handler.postDelayed(timeout, 15000);
    session.savedTaskRequest(requestId, nextAction, id);
  }

  void accept(JSONObject data) {
    if (pending.isEmpty() || !pending.equals(data.optString("request_id"))) return;
    String completedAction = action;
    close();
    if (!data.optBoolean("ok")) {
      toast(data.optString("message", "保存记录状态未确认，请刷新查看"));
      return;
    }
    if ("list".equals(completedAction)) showList(data.optJSONArray("items"));
    else if ("get".equals(completedAction)) showDetail(data.optJSONObject("item"));
    else if ("delete".equals(completedAction)) {
      toast(data.optBoolean("deleted") ? "已删除保存记录，原任务不变" : "这条记录已不在列表中");
      open();
    } else toast("结果已保存在 Mac，可在“已保存结果”中查看");
  }

  private void showList(JSONArray items) {
    if (items == null || items.length() == 0) {
      new AlertDialog.Builder(activity)
          .setTitle("已保存结果")
          .setMessage("还没有保存的结果。请在任务详情中明确选择“保存结果”；日常任务不会自动保存。")
          .setPositiveButton("关闭", null)
          .show();
      return;
    }
    String[] labels = new String[items.length()];
    for (int i = 0; i < labels.length; i++) {
      JSONObject item = items.optJSONObject(i);
      labels[i] =
          item == null ? "保存记录" : label(item.optString("status")) + " · " + item.optString("title");
    }
    new AlertDialog.Builder(activity)
        .setTitle("已保存结果 · Mac 本机")
        .setItems(
            labels,
            (d, n) -> {
              JSONObject item = items.optJSONObject(n);
              if (item != null) request("get", item.optString("id"));
            })
        .setPositiveButton("关闭", null)
        .show();
  }

  private void showDetail(JSONObject item) {
    if (item == null) {
      toast("未找到保存记录，请刷新");
      return;
    }
    StringBuilder body =
        new StringBuilder(
            "历史快照 · 不会自动重跑任务\n保存于："
                + item.optString("saved_at")
                + "\n状态："
                + label(item.optString("status")));
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
    TextView detail = new TextView(activity);
    detail.setText(body.toString());
    detail.setTextSize(22);
    detail.setTextIsSelectable(true);
    detail.setPadding(24, 18, 24, 18);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(detail);
    new AlertDialog.Builder(activity)
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
                new AlertDialog.Builder(activity)
                    .setTitle("删除这条保存记录？")
                    .setMessage("仅删除 Mac 上的这份结果快照，不撤销已执行操作，也不删除结果文件或桌面聊天。")
                    .setPositiveButton(
                        "删除", (confirm, which) -> request("delete", item.optString("id")))
                    .setNegativeButton("取消", null)
                    .show())
        .show();
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
