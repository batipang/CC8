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

/** Display receipt-owned provenance; locating a file is a separate explicit Mac action. */
final class ResultDetailsDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private String pending = "";
  private long pendingRoute = -1;
  private AlertDialog progress;
  private Runnable timeout;

  ResultDetailsDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  void close() {
    pending = "";
    pendingRoute = -1;
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    if (progress != null) progress.dismiss();
    progress = null;
  }

  void open(JSONObject record, boolean saved, long route) {
    JSONObject source = record.optJSONObject("provenance");
    if (source == null) {
      toast("这条旧记录未保存结构化来源，不能从答复文字推测文件或会话。");
      return;
    }
    StringBuilder body = new StringBuilder();
    body.append("任务记录：").append(record.optString("id"));
    body.append("\n结束时间：").append(record.optString("finished_at", "旧记录未保存"));
    boolean report = "work_report".equals(source.optString("provider"));
    body.append(report ? "\n只读汇报来源 · 未追加原聊天" : "\n独立 Dot 语音执行 · 临时会话");
    body.append("\n主机：").append(source.optString("host"));
    body.append("\n会话：").append(source.optString("thread_id"));
    if (!source.isNull("turn_id")) body.append("\n回合：").append(source.optString("turn_id"));
    JSONObject original = source.optJSONObject("original_source");
    if (original != null) {
      body.append("\n\n原聊天来源：").append(original.optString("title"));
      body.append("\n来源会话：").append(original.optString("thread_id"));
      if (!report) body.append("\n仅摘要交接，执行结果未追加原聊天。");
    }
    body.append("\n\n文件仅来自结构化文件变更工具回执，不从模型答复推测；命令生成的文件可能未列出。回执成功不代表独立验证，也不保证文件现在仍存在。");
    JSONArray files = source.optJSONArray("files");
    if (files == null || files.length() == 0) body.append("\n\n本记录没有可定位的结构化文件回执。");
    if (source.optBoolean("files_truncated")) body.append("\n文件回执覆盖不完整或已截短，请在 Mac 核对完整结果。");
    if (files != null)
      for (int i = 0; i < files.length(); i++) {
        JSONObject file = files.optJSONObject(i);
        if (file == null) continue;
        body.append("\n\n").append(i + 1).append(". ").append(file.optString("path"));
        if (!file.isNull("move_to")) body.append("\n移至：").append(file.optString("move_to"));
        body.append("\n操作：")
            .append(
                "add".equals(file.optString("kind"))
                    ? "新增"
                    : "delete".equals(file.optString("kind")) ? "删除" : "修改");
        body.append(" · 工具回执：")
            .append(
                "completed".equals(file.optString("status"))
                    ? "成功"
                    : "declined".equals(file.optString("status")) ? "已拒绝" : "失败");
        body.append("\n回执项目：").append(file.optString("item_id"));
      }
    TextView text = new TextView(activity);
    text.setText(body.toString());
    text.setTextSize(26);
    text.setTextIsSelectable(true);
    text.setPadding(24, 18, 24, 18);
    LinearLayout content = new LinearLayout(activity);
    content.setOrientation(LinearLayout.VERTICAL);
    content.addView(text);
    if (files != null && files.length() > 0 && session.supportsResultProvenance()) {
      Button locate = new Button(activity);
      locate.setText("在 Mac 定位文件");
      locate.setTextSize(24);
      locate.setOnClickListener(v -> chooseFile(record.optString("id"), files, saved, route));
      content.addView(locate);
    }
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(content);
    new LargeDialogBuilder(activity)
        .setTitle("来源与文件 · 实际回执")
        .setView(scroll)
        .setPositiveButton("关闭", null)
        .setNeutralButton(
            "复制来源与回执",
            (d, n) -> {
              ClipboardManager clipboard =
                  (ClipboardManager) activity.getSystemService(Activity.CLIPBOARD_SERVICE);
              clipboard.setPrimaryClip(ClipData.newPlainText("结果来源与文件回执", body.toString()));
              toast("已复制；不会自动发送到其他聊天。");
            })
        .show();
  }

  private void chooseFile(String taskId, JSONArray files, boolean saved, long route) {
    String[] names = new String[Math.min(files.length(), 8)];
    for (int i = 0; i < names.length; i++) {
      JSONObject file = files.optJSONObject(i);
      names[i] = file == null ? "未确认文件" : file.optString("path");
    }
    new LargeDialogBuilder(activity)
        .setTitle("选择文件 · 仅在 Finder 定位")
        .setItems(
            names,
            (d, n) -> {
              JSONObject file = files.optJSONObject(n);
              if (file == null
                  || !"completed".equals(file.optString("status"))
                  || "delete".equals(file.optString("kind"))) {
                toast("该回执不是成功写入的现存文件，请在 Mac 核对。");
                return;
              }
              reveal(taskId, n, saved, route);
            })
        .setNegativeButton("取消", null)
        .show();
  }

  private void reveal(String taskId, int index, boolean saved, long route) {
    if (!session.isConnected()
        || !session.currentRoute(route)
        || !session.supportsResultProvenance()) {
      toast("连接或后台能力已变化，请重新打开结果。");
      return;
    }
    close();
    pending = UUID.randomUUID().toString().replace("-", "");
    pendingRoute = route;
    String requestId = pending;
    progress =
        new LargeDialogBuilder(activity)
            .setTitle("正在 Mac 定位文件")
            .setMessage("仅定位，不打开文件内容。关闭等待不会撤销已经提交的定位；未确认时不会自动重试。")
            .setNegativeButton("关闭等待", (d, n) -> close())
            .create();
    progress.setOnCancelListener(d -> close());
    progress.show();
    timeout =
        () -> {
          if (!requestId.equals(pending)) return;
          close();
          toast("定位结果未确认，请在 Mac 查看；不会自动重试。");
        };
    handler.postDelayed(timeout, 15000);
    if (!session.revealResultFile(requestId, taskId, index, saved, route)) {
      close();
      toast("请求未能提交，请重新打开结果并核对连接。");
    }
  }

  void accept(JSONObject data) {
    if (pending.isEmpty() || !pending.equals(data.optString("request_id"))) return;
    boolean current = session.currentRoute(pendingRoute);
    close();
    if (!current) return;
    toast(
        data.optBoolean("ok") && data.optBoolean("revealed")
            ? "已提交 Finder 定位请求，请在 Mac 查看。"
            : data.optString("message", "定位结果未确认，请在 Mac 核对。"));
  }

  private void toast(String message) {
    Toast.makeText(activity, message, Toast.LENGTH_LONG).show();
  }
}
