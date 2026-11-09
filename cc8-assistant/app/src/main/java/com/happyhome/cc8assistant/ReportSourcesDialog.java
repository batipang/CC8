package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONObject;

/** Explicit persistent metadata consent and read-only multi-chat reports; never arms capture. */
final class ReportSourcesDialog {
  private static final class Source {
    final String id, thread, title, project;

    Source(String id, String thread, String title, String project) {
      this.id = id;
      this.thread = thread;
      this.title = title;
      this.project = project;
    }
  }

  private static final class Snapshot {
    final int revision;
    final List<Source> sources;

    Snapshot(int revision, List<Source> sources) {
      this.revision = revision;
      this.sources = Collections.unmodifiableList(new ArrayList<>(sources));
    }

    static Snapshot parse(JSONObject value) {
      if (value == null || !Integer.valueOf(1).equals(value.opt("schema"))) return null;
      Object revision = value.opt("revision");
      if (!(revision instanceof Integer) || (Integer) revision < 0) return null;
      JSONArray rows = value.optJSONArray("items");
      if (rows == null || rows.length() > 8) return null;
      List<Source> result = new ArrayList<>();
      HashSet<String> ids = new HashSet<>(), threads = new HashSet<>();
      for (int i = 0; i < rows.length(); i++) {
        JSONObject row = rows.optJSONObject(i);
        if (row == null || !"codex".equals(row.opt("provider")) || !"local".equals(row.opt("host")))
          return null;
        if (!(row.opt("id") instanceof String)
            || !(row.opt("thread_id") instanceof String)
            || !(row.opt("title") instanceof String)
            || !(row.opt("project") instanceof String)) return null;
        String id = row.optString("id"), thread = row.optString("thread_id");
        String title = row.optString("title"), project = row.optString("project");
        if (!id.matches("[a-f0-9]{16}")
            || !thread.matches("[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
            || !label(title)
            || !label(project)
            || !ids.add(id)
            || !threads.add(thread)) return null;
        result.add(new Source(id, thread, title, project));
      }
      return new Snapshot((Integer) revision, result);
    }
  }

  private final Activity activity;
  private final VoiceSession session;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog dialog;
  private Snapshot snapshot;
  private String pending = "", action = "";
  private long route = -1, epoch;
  private Runnable timeout;

  ReportSourcesDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  private static boolean label(String value) {
    return !value.isEmpty()
        && value.equals(value.trim())
        && value.codePointCount(0, value.length()) <= 80
        && !value.matches("(?s).*[\\x00-\\x1f].*");
  }

  void open() {
    request(args("sources"));
  }

  void close() {
    if (!pending.isEmpty() && session.currentRoute(route)) session.dotChatCancel(pending);
    pending = action = "";
    snapshot = null;
    epoch++;
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    AlertDialog previous = dialog;
    dialog = null;
    if (previous != null) previous.dismiss();
  }

  private JSONObject args(String action) {
    JSONObject result = new JSONObject();
    Wire.put(result, "action", action);
    return result;
  }

  private boolean live(long owner, Snapshot value) {
    return owner == epoch
        && dialog != null
        && snapshot == value
        && session.currentRoute(route)
        && session.supportsReportSources();
  }

  private long show(AlertDialog next) {
    AlertDialog previous = dialog;
    dialog = next;
    final long owner = ++epoch;
    if (previous != null) previous.dismiss();
    next.setOnDismissListener(
        d -> {
          if (dialog == next) close();
        });
    next.show();
    return owner;
  }

  private void request(JSONObject arguments) {
    close();
    route = session.connectedRoute();
    if (!session.currentRoute(route) || !session.supportsReportSources()) {
      toast("请连接支持工作汇报来源的后台");
      return;
    }
    pending = UUID.randomUUID().toString().replace("-", "");
    action = arguments.optString("action");
    final String ident = pending;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("工作汇报来源")
            .setMessage("正在核对。未确认的修改请刷新查看，不会自动重试。")
            .setNegativeButton("关闭等待", (d, n) -> close())
            .create());
    timeout =
        () -> {
          if (ident.equals(pending)) {
            close();
            toast("未确认来源状态，请重新查看；不自动重试修改");
          }
        };
    handler.postDelayed(timeout, "report".equals(action) ? 75000 : 35000);
    if (!session.reportSourcesRequest(ident, arguments, route)) {
      close();
      toast("连接已变化，来源请求未发送");
    }
  }

  void accept(JSONObject data) {
    if (pending.isEmpty()
        || !pending.equals(data.optString("request_id"))
        || !session.currentRoute(route)) return;
    String completed = action;
    pending = "";
    close();
    if (!Boolean.TRUE.equals(data.opt("ok"))) {
      toast(data.optString("message", "未确认来源状态，请刷新查看"));
      return;
    }
    if ("report".equals(completed)) {
      String text = data.optString("report_text");
      if (text.isEmpty() || text.length() > 48000) {
        toast("未取得有效汇报");
        return;
      }
      show(
          new AlertDialog.Builder(activity)
              .setTitle("工作汇报 · 当前范围只读快照")
              .setMessage(text)
              .setNegativeButton("关闭", null)
              .create());
      return;
    }
    Snapshot value = Snapshot.parse(data.optJSONObject("registry"));
    if (value == null) {
      toast("来源格式不兼容，请刷新查看");
      return;
    }
    snapshot = value;
    if ("source_candidates".equals(completed)) candidates(data, value);
    else menu(value, data.optString("report_source_title"));
  }

  private void menu(Snapshot value, String scope) {
    final long owner = epoch + 1;
    String[] labels = new String[value.sources.size()];
    for (int i = 0; i < labels.length; i++)
      labels[i] = value.sources.get(i).title + " · " + value.sources.get(i).project;
    AlertDialog.Builder builder =
        new AlertDialog.Builder(activity)
            .setTitle("已登记来源 · " + labels.length + "/8")
            .setPositiveButton(
                "添加来源",
                (d, n) -> {
                  if (live(owner, value)) request(args("source_candidates"));
                })
            .setNeutralButton(
                "汇报选项",
                (d, n) -> {
                  if (live(owner, value)) options(value, scope);
                })
            .setNegativeButton("关闭", null);
    if (labels.length == 0)
      builder.setMessage(
          "仅在明确添加时保存本机 Codex 聊天的名称、项目简称和标识，最多8个，保留至手动删除。原始聊天按需读取，不保存正文，不自动汇报，不向原聊天追加。重启不自动启用登记范围。\n当前汇报范围："
              + (scope.isEmpty() ? "未选择" : scope));
    else
      builder.setItems(
          labels,
          (d, n) -> {
            if (live(owner, value)) remove(value, value.sources.get(n));
          });
    show(builder.create());
  }

  private void candidates(JSONObject data, Snapshot value) {
    JSONArray rows = data.optJSONArray("items");
    if (rows == null || rows.length() > 10) {
      close();
      toast("没有可用来源列表");
      return;
    }
    List<Source> offered = new ArrayList<>();
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      if (row == null) {
        close();
        return;
      }
      if (!(row.opt("id") instanceof String)
          || !(row.opt("title") instanceof String)
          || !(row.opt("project") instanceof String)) {
        close();
        return;
      }
      String id = row.optString("id"),
          title = row.optString("title"),
          project = row.optString("project");
      if (!id.matches("[A-Za-z0-9][A-Za-z0-9_-]{0,95}") || !label(title) || !label(project)) {
        close();
        return;
      }
      offered.add(new Source("", id, title, project));
    }
    final List<Source> frozen = Collections.unmodifiableList(new ArrayList<>(offered));
    String[] labels = new String[frozen.size()];
    for (int i = 0; i < labels.length; i++)
      labels[i] = frozen.get(i).title + " · " + frozen.get(i).project;
    final long owner = epoch + 1;
    AlertDialog.Builder builder =
        new AlertDialog.Builder(activity)
            .setTitle("选择登记来源 · 只读元数据")
            .setItems(
                labels,
                (d, n) -> {
                  if (live(owner, value)) register(value, frozen.get(n));
                })
            .setNegativeButton("关闭", null);
    String cursor = data.isNull("next_cursor") ? "" : data.optString("next_cursor");
    if (!cursor.isEmpty() && cursor.length() <= 2048)
      builder.setNeutralButton(
          "下一页",
          (d, n) -> {
            if (!live(owner, value)) return;
            JSONObject args = args("source_candidates");
            Wire.put(args, "cursor", cursor);
            request(args);
          });
    show(builder.create());
  }

  private JSONObject mutation(String action, Snapshot value) {
    JSONObject arguments = args(action);
    Wire.put(arguments, "revision", value.revision);
    Wire.put(arguments, "confirmed", true);
    return arguments;
  }

  private String changeNotice() {
    return "请先暂停语音。登记本身不执行任务；删除当前范围的来源或切换范围会清除对应的内存编号和待播账本，不撤销原任务。未确认时刷新查看，不自动重试。";
  }

  private void register(Snapshot value, Source item) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("登记：" + item.title)
            .setMessage(
                "仅保存名称、项目简称和聊天标识，最多8个，保留至手动删除。不会读取正文、启动模型或追加原聊天；只有明确请求汇报才读取已登记聊天。\n\n"
                    + changeNotice())
            .setPositiveButton(
                "确认登记",
                (d, n) -> {
                  if (!live(owner, value)) return;
                  JSONObject args = mutation("register_source", value);
                  Wire.put(args, "source_id", item.thread);
                  request(args);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void remove(Snapshot value, Source item) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle(item.title + " · 本机 Codex")
            .setMessage("项目：" + item.project + "\n只删除本机来源登记，不删除原聊天。\n\n" + changeNotice())
            .setPositiveButton(
                "删除登记",
                (d, n) -> {
                  if (!live(owner, value)) return;
                  JSONObject args = mutation("delete_source", value);
                  Wire.put(args, "registration_id", item.id);
                  request(args);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void options(Snapshot value, String scope) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("工作汇报范围")
            .setItems(
                new String[] {
                  "使用全部登记来源", "使用当前摘要聊天", "读取当前范围：" + (scope.isEmpty() ? "未选择" : scope), "清空来源登记"
                },
                (d, n) -> {
                  if (!live(owner, value)) return;
                  if (n == 0) {
                    JSONObject args = args("use_sources");
                    Wire.put(args, "revision", value.revision);
                    request(args);
                  } else if (n == 1) request(args("use_selected_report"));
                  else if (n == 2) periods(value);
                  else clear(value);
                })
            .setNegativeButton("关闭", null)
            .create());
  }

  private void periods(Snapshot value) {
    final long owner = epoch + 1;
    String[] periods = {"half_hour", "two_hours", "today"};
    show(
        new AlertDialog.Builder(activity)
            .setTitle("读取当前汇报范围 · 北京时间")
            .setItems(
                new String[] {"过去半小时", "过去两小时", "今天"},
                (d, n) -> {
                  if (!live(owner, value)) return;
                  JSONObject args = args("report");
                  Wire.put(args, "period", periods[n]);
                  request(args);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void clear(Snapshot value) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("清空本机来源登记？")
            .setMessage("只清除来源元数据，原聊天和任务保持。\n\n" + changeNotice())
            .setPositiveButton(
                "确认清空",
                (d, n) -> {
                  if (live(owner, value)) request(mutation("clear_sources", value));
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void toast(String message) {
    Toast.makeText(activity, message, Toast.LENGTH_LONG).show();
  }
}
