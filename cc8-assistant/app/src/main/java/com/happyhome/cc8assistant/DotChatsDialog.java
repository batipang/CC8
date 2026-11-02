package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONObject;

/** Explicit metadata discovery and selected-chat consent; no automatic capture or history load. */
final class DotChatsDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private JSONObject state = new JSONObject();
  private String pending = "", action = "";
  private AlertDialog progress;
  private Runnable timeout;

  DotChatsDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  void open() {
    request("state", null, null);
  }

  void close() {
    if (!pending.isEmpty()) session.dotChatCancel(pending);
    pending = "";
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    if (progress != null) progress.dismiss();
    progress = null;
  }

  private void request(String nextAction, String source, String cursor) {
    close();
    if (!session.isConnected()) {
      toast("请先连接 Mac");
      return;
    }
    pending = UUID.randomUUID().toString().replace("-", "");
    action = nextAction;
    String ident = pending;
    progress =
        new AlertDialog.Builder(activity)
            .setTitle("Dot 聊天")
            .setMessage("正在读取，请稍候…")
            .setNegativeButton("取消", (d, n) -> close())
            .create();
    progress.setOnCancelListener(d -> close());
    progress.show();
    timeout =
        () -> {
          if (!ident.equals(pending)) return;
          close();
          toast("未确认聊天状态，请连接后刷新查看");
        };
    handler.postDelayed(timeout, 50000);
    session.dotChatRequest(ident, nextAction, source, cursor);
  }

  void accept(JSONObject data) {
    String ident = data.optString("request_id");
    if (!ident.isEmpty() && !pending.equals(ident)) return;
    state = data;
    if (pending.isEmpty() || !pending.equals(data.optString("request_id"))) return;
    String completedAction = action;
    pending = "";
    close();
    if (!data.optBoolean("ok")) {
      toast(data.optString("message", "聊天入口暂不可用"));
      return;
    }
    if ("list".equals(completedAction)) showList(data);
    else if ("state".equals(completedAction)) showMode();
    else
      toast(
          "summary_handoff".equals(state.optString("mode"))
              ? "已选择摘要接续："
                  + state.optString("source_title")
                  + (state.optBoolean("local_project_available") ? "" : "；原项目环境未接入，文件操作需明确目标")
              : "已切回独立 Dot 语音会话");
  }

  private void showMode() {
    boolean selected = "summary_handoff".equals(state.optString("mode"));
    new AlertDialog.Builder(activity)
        .setTitle("Dot 聊天接续")
        .setMessage(
            (selected ? "当前来源：" + state.optString("source_title") : "当前为独立语音会话")
                + "\n\n摘要接续会读取所选聊天最近三个已完成回合的文字摘录。结果显示在大黄蜂任务页，可复制交回桌面；不会自动追加原聊天。")
        .setPositiveButton("选择聊天", (d, n) -> request("list", null, null))
        .setNeutralButton("独立语音", (d, n) -> request("clear", null, null))
        .setNegativeButton("关闭", null)
        .show();
  }

  private void showList(JSONObject data) {
    JSONArray items = data.optJSONArray("items");
    if (items == null || items.length() == 0) {
      toast("这一页没有可选聊天");
      return;
    }
    String cursor = data.isNull("next_cursor") ? "" : data.optString("next_cursor");
    String[] names = new String[items.length()];
    for (int i = 0; i < names.length; i++) {
      JSONObject item = items.optJSONObject(i);
      names[i] =
          item == null
              ? "未命名聊天"
              : item.optString("title", "未命名聊天") + " · " + item.optString("project", "未标明项目");
    }
    AlertDialog.Builder builder =
        new AlertDialog.Builder(activity)
            .setTitle("选择摘要来源 · 不追加原聊天")
            .setItems(
                names,
                (d, n) -> {
                  JSONObject item = items.optJSONObject(n);
                  if (item == null) return;
                  new AlertDialog.Builder(activity)
                      .setTitle(item.optString("title", "未命名聊天"))
                      .setMessage(
                          "将所选聊天最近三个已完成回合的文字摘录交给独立 Dot 会话，可能截断。可用的本地项目目录用于新会话，远端或失效环境不会自动接入。\n\n历史请求不会自动重新执行；结果不会自动追加原聊天。摘录只保留在本次 Mac 服务内存中，随之后的请求使用已有模型服务。")
                      .setPositiveButton(
                          "摘要接续",
                          (confirm, button) -> request("select", item.optString("id"), null))
                      .setNegativeButton("取消", null)
                      .show();
                })
            .setNegativeButton("关闭", null);
    if (!cursor.isEmpty()) builder.setNeutralButton("下一页", (d, n) -> request("list", null, cursor));
    builder.show();
  }

  private void toast(String message) {
    Toast.makeText(activity, message, Toast.LENGTH_LONG).show();
  }
}
