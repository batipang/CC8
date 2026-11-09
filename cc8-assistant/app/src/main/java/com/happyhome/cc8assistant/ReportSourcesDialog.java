package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
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
    if ("source_queue".equals(completed)) {
      Object body = data.opt("queue_text");
      if (!(body instanceof String)
          || ((String) body).isEmpty()
          || ((String) body).length() > 24000) {
        toast("未取得有效的原聊天队列状态");
        return;
      }
      final String text = (String) body;
      final long owner = epoch + 1;
      show(
          new AlertDialog.Builder(activity)
              .setTitle("原聊天待办 · 只读")
              .setMessage(text)
              .setNegativeButton("关闭", null)
              .setPositiveButton(
                  "复制",
                  (d, n) -> {
                    if (!live(owner, null)) return;
                    ClipboardManager clipboard =
                        (ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE);
                    if (clipboard != null)
                      clipboard.setPrimaryClip(ClipData.newPlainText("原聊天待办队列", text));
                  })
              .create());
      return;
    }
    if ("report".equals(completed)) {
      String text = data.optString("report_text");
      if (text.isEmpty() || text.length() > 48000) {
        toast("未取得有效汇报");
        return;
      }
      AlertDialog.Builder builder =
          new AlertDialog.Builder(activity)
              .setTitle("工作汇报 · 当前范围只读快照")
              .setMessage(text)
              .setNegativeButton("关闭", null);
      JSONObject report = data.optJSONObject("report");
      String snapshotId = report == null ? "" : report.optString("snapshot_id");
      Object archiveRevision = data.opt("archive_revision");
      final long owner = epoch + 1;
      if (session.supportsReportArchive()
          && snapshotId.matches("[a-f0-9]{32}")
          && archiveRevision instanceof Integer
          && (Integer) archiveRevision >= 0) {
        final int revision = (Integer) archiveRevision;
        builder.setPositiveButton(
            "保存快照",
            (d, n) -> {
              if (live(owner, null)) confirmSave(snapshotId, revision);
            });
      }
      show(builder.create());
      return;
    }
    if ("saved_reports".equals(completed)
        || "saved_report".equals(completed)
        || "save_report".equals(completed)
        || "delete_report".equals(completed)
        || "clear_reports".equals(completed)) {
      showArchive(data);
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
    return "请先暂停语音。登记本身不执行任务；删除当前范围的来源或切换范围会清除对应的内存编号和待播账本，不撤销原任务。未确认时刷新查看，不自动重试。已另行保存的历史快照需在“已保存汇报”中单独删除。";
  }

  private void register(Snapshot value, Source item) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("登记：" + item.title)
            .setMessage(
                "仅保存名称、项目简称和聊天标识，最多8个，保留至手动删除。不会读取正文、启动模型或追加原聊天；只有明确请求汇报或查看待办队列才读取所选来源。\n\n"
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
    AlertDialog.Builder builder =
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
            .setNegativeButton("取消", null);
    if (session.supportsThreadQueue())
      builder.setNeutralButton(
          "查看待办队列",
          (d, n) -> {
            if (!live(owner, value) || !session.supportsThreadQueue()) return;
            JSONObject args = args("source_queue");
            Wire.put(args, "revision", value.revision);
            Wire.put(args, "registration_id", item.id);
            request(args);
          });
    show(builder.create());
  }

  private void options(Snapshot value, String scope) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("工作汇报范围")
            .setItems(
                new String[] {
                  "使用全部登记来源",
                  "使用当前摘要聊天",
                  "读取当前范围：" + (scope.isEmpty() ? "未选择" : scope),
                  "清空来源登记",
                  "已保存汇报"
                },
                (d, n) -> {
                  if (!live(owner, value)) return;
                  if (n == 0) {
                    JSONObject args = args("use_sources");
                    Wire.put(args, "revision", value.revision);
                    request(args);
                  } else if (n == 1) request(args("use_selected_report"));
                  else if (n == 2) periods(value);
                  else if (n == 3) clear(value);
                  else if (session.supportsReportArchive()) request(args("saved_reports"));
                  else toast("当前后台不支持汇报快照");
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

  private void confirmSave(String id, int revision) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("保存本次工作汇报？")
            .setMessage(
                "只保存本次汇报的摘录、来源标识、完成时间和覆盖提示，不保存完整聊天或录音。最多16份、总计256KiB，保留至手动删除，不自动淘汰。重启只供查看，不恢复语音编号、已播账本、自动播报或原任务。请先暂停语音。")
            .setPositiveButton(
                "确认保存",
                (d, n) -> {
                  if (!live(owner, null) || !session.supportsReportArchive()) return;
                  JSONObject value = args("save_report");
                  Wire.put(value, "snapshot_id", id);
                  Wire.put(value, "revision", revision);
                  Wire.put(value, "confirmed", true);
                  request(value);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void showArchive(JSONObject data) {
    JSONObject state = data.optJSONObject("saved_reports");
    if (state == null
        || !(state.opt("revision") instanceof Integer)
        || (Integer) state.opt("revision") < 0) {
      toast("汇报快照格式不兼容");
      return;
    }
    final int revision = (Integer) state.opt("revision");
    JSONArray rows = state.optJSONArray("items");
    if (rows == null || rows.length() > 16) {
      toast("汇报快照格式不兼容");
      return;
    }
    final List<String> ids = new ArrayList<>(), labels = new ArrayList<>();
    HashSet<String> seen = new HashSet<>();
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      if (row == null
          || !(row.opt("id") instanceof String)
          || !(row.opt("source_title") instanceof String)
          || !row.optString("id").matches("[a-f0-9]{32}")
          || !label(row.optString("source_title"))
          || !seen.add(row.optString("id"))
          || !(row.opt("saved_at") instanceof Integer || row.opt("saved_at") instanceof Long)
          || !(row.opt("coverage_complete") instanceof Boolean)) {
        toast("汇报快照格式不兼容");
        return;
      }
      long savedAt = ((Number) row.opt("saved_at")).longValue();
      if (savedAt < 0 || savedAt >= 253402300799L) {
        toast("汇报快照时间无效");
        return;
      }
      java.text.SimpleDateFormat format =
          new java.text.SimpleDateFormat("MM-dd HH:mm", java.util.Locale.CHINA);
      format.setTimeZone(java.util.TimeZone.getTimeZone("Asia/Shanghai"));
      ids.add(row.optString("id"));
      labels.add(
          row.optString("source_title")
              + " · "
              + format.format(new java.util.Date(savedAt * 1000))
              + (Boolean.TRUE.equals(row.opt("coverage_complete")) ? "" : " · 覆盖不完整"));
    }
    // Strings and revision copied now; later JSON mutation cannot retarget a confirmation.
    final String savedId = data.optString("saved_id"), body = data.optString("saved_report_text");
    final long owner = epoch + 1;
    AlertDialog.Builder builder =
        new AlertDialog.Builder(activity)
            .setTitle("已保存汇报 · " + ids.size() + "/16")
            .setNegativeButton("关闭", null);
    if ("saved_report".equals(data.optString("action"))) {
      if (!ids.contains(savedId) || body.isEmpty() || body.length() > 40000) {
        toast("未取得有效快照");
        return;
      }
      builder
          .setTitle("历史快照 · 非实时结果")
          .setMessage(body)
          .setPositiveButton(
              "复制",
              (d, n) -> {
                if (!live(owner, null)) return;
                ClipboardManager clipboard =
                    (ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE);
                if (clipboard != null)
                  clipboard.setPrimaryClip(ClipData.newPlainText("工作汇报历史快照", body));
              })
          .setNeutralButton(
              "删除快照",
              (d, n) -> {
                if (live(owner, null)) confirmArchiveDelete(savedId, revision);
              });
    } else {
      if (ids.isEmpty())
        builder.setMessage("尚未保存汇报。仅在本次汇报中明确确认后保存；保留至手动删除，不自动播报或执行。总计256KiB，不自动淘汰旧记录。");
      else
        builder
            .setItems(
                labels.toArray(new String[0]),
                (d, n) -> {
                  if (!live(owner, null)) return;
                  JSONObject value = args("saved_report");
                  Wire.put(value, "saved_id", ids.get(n));
                  request(value);
                })
            .setNeutralButton(
                "清空快照",
                (d, n) -> {
                  if (live(owner, null)) confirmArchiveDelete("", revision);
                });
    }
    show(builder.create());
  }

  private void confirmArchiveDelete(String id, int revision) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle(id.isEmpty() ? "清空已保存汇报？" : "删除本份汇报快照？")
            .setMessage("只删除本机保存的汇报摘录和索引，不删除来源登记、原聊天或任务。请先暂停语音；未确认时刷新查看，不自动重试。")
            .setPositiveButton(
                "确认删除",
                (d, n) -> {
                  if (!live(owner, null) || !session.supportsReportArchive()) return;
                  JSONObject value = args(id.isEmpty() ? "clear_reports" : "delete_report");
                  Wire.put(value, "revision", revision);
                  Wire.put(value, "confirmed", true);
                  if (!id.isEmpty()) Wire.put(value, "saved_id", id);
                  request(value);
                })
            .setNegativeButton("取消", null)
            .create());
  }
}
