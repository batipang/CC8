package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import android.widget.Button;
import java.util.UUID;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;
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
    request(nextAction, source, cursor, null);
  }

  private void request(String nextAction, String source, String cursor, String period) {
    close();
    if (!session.isConnected()) {
      toast("请先连接 Mac");
      return;
    }
    if (!session.supportsDotChatHandoff()) {
      toast("当前后台尚未确认支持聊天接续，请完成连接或更新后台");
      return;
    }
    pending = UUID.randomUUID().toString().replace("-", "");
    action = nextAction;
    String ident = pending;
    progress =
        new LargeDialogBuilder(activity)
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
    session.dotChatRequest(ident, nextAction, source, cursor, period);
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
    else if ("report".equals(completedAction)) showReport(data.optJSONObject("report"));
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
    boolean reports = selected && state.optBoolean("work_reports_supported");
    AlertDialog.Builder builder =
        new LargeDialogBuilder(activity)
            .setTitle("Dot 聊天接续")
            .setMessage(
                (selected ? "当前来源：" + state.optString("source_title") : "当前为独立语音会话")
                    + "\n\n摘要接续会读取所选聊天最近三个已完成回合的文字摘录。结果显示在大黄蜂任务页，可复制交回桌面；不会自动追加原聊天。")
            .setPositiveButton("选择聊天", (d, n) -> request("list", null, null))
            .setNeutralButton("独立语音", (d, n) -> request("clear", null, null))
            .setNegativeButton("关闭", null);
    Button reportButton = new Button(activity);
    reportButton.setText("工作汇报");
    reportButton.setTextSize(28);
    if (reports) builder.setView(reportButton);
    AlertDialog dialog = builder.create();
    reportButton.setOnClickListener(
        v -> {
          dialog.dismiss();
          showReportPeriods();
        });
    dialog.show();
  }

  private void showReportPeriods() {
    String[] periods = {"half_hour", "two_hours", "today"};
    new LargeDialogBuilder(activity)
        .setTitle("所选聊天的工作汇报")
        .setItems(
            new String[] {"过去半小时", "过去两小时", "今天 · 北京时间"},
            (d, n) -> request("report", null, null, periods[n]))
        .setNegativeButton("取消", null)
        .show();
  }

  private void showReport(JSONObject report) {
    if (report == null) {
      toast("未取得有效汇报，请重试");
      return;
    }
    SimpleDateFormat dates = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA);
    dates.setTimeZone(TimeZone.getTimeZone("Asia/Shanghai"));
    StringBuilder text = new StringBuilder();
    text.append("来源：").append(report.optString("source_title"));
    text.append("\n北京时间：")
        .append(dates.format(new Date(report.optLong("window_start") * 1000)))
        .append(" 至 ")
        .append(dates.format(new Date(report.optLong("window_end") * 1000)))
        .append("（含起点，不含终点）\n");
    if (!report.optBoolean("coverage_complete")) text.append("\n覆盖不完整，不能据此断言没有其他成果。\n");
    text.append("缺完成时间：")
        .append(report.optInt("unknown_completion_time"))
        .append("；缺最终答复：")
        .append(report.optInt("missing_final_answer"))
        .append("\n窗口内失败：")
        .append(report.optInt("failed_turns"))
        .append("；中断：")
        .append(report.optInt("interrupted_turns"))
        .append("；读到仍进行中：")
        .append(report.optInt("active_turns_seen"))
        .append("\n");
    if (report.optBoolean("display_preview_truncated"))
      text.append("\n本窗口为简短预览；可在工作汇报来源页面翻页查看完整汇报。\n");
    JSONArray items = report.optJSONArray("items");
    if (report.optBoolean("empty_confirmed")) text.append("\n已读取范围内没有本窗口的已完成答复。\n");
    if (items != null) {
      for (int i = 0; i < items.length(); i++) {
        JSONObject item = items.optJSONObject(i);
        if (item == null) continue;
        text.append("\n")
            .append(i + 1)
            .append(". ")
            .append(dates.format(new Date(item.optLong("completed_at") * 1000)))
            .append("\n原答复摘录：\n")
            .append(item.optString("excerpt"));
        if (item.optBoolean("excerpt_truncated")) text.append("\n（摘录已截短）");
        JSONObject evidence = item.optJSONObject("evidence");
        if (evidence != null) {
          text.append("\n终端成功／失败回执：")
              .append(evidence.optInt("command_successes"))
              .append("／")
              .append(evidence.optInt("command_failures"))
              .append("；文件变更成功／失败回执：")
              .append(evidence.optInt("file_change_successes"))
              .append("／")
              .append(evidence.optInt("file_change_failures"))
              .append("；待确认：")
              .append(evidence.optInt("unconfirmed_tool_items"));
        }
        if (!item.optBoolean("evidence_complete")) text.append("\n执行回执未读取完整。");
        text.append("\n回合完成不代表任务已验证；以上答复尚未独立核验。\n");
      }
    }
    new LargeDialogBuilder(activity)
        .setTitle("工作汇报 · 只读快照")
        .setMessage(text.toString())
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
        new LargeDialogBuilder(activity)
            .setTitle("选择摘要来源 · 不追加原聊天")
            .setItems(
                names,
                (d, n) -> {
                  JSONObject item = items.optJSONObject(n);
                  if (item == null) return;
                  new LargeDialogBuilder(activity)
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
