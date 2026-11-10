package com.happyhome.cc8assistant;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.text.InputFilter;
import android.widget.*;
import org.json.JSONArray;
import org.json.JSONObject;

/** Visible explicit meeting mode. Closing/Home pauses native capture; reconnect never resumes it. */
final class MeetingDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog dialog, child, confirmation;
  private TextView info;
  private Button start,
      pause,
      end,
      transcript,
      delete,
      export,
      summarize,
      translate,
      analysisResults,
      cancelAnalysis;
  private long route = -1, epoch;
  private JSONObject state = new JSONObject();
  private String pending = "", action = "";
  private Runnable timeout;
  private String readingRow = "", readingId = "";
  private long readingRevision;
  private int readingOffset;
  private StringBuilder text;
  private final MeetingAnalysisDialog analysisDialog;

  MeetingDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
    analysisDialog =
        new MeetingAnalysisDialog(
            activity,
            new MeetingAnalysisDialog.Port() {
              public long epoch() {
                return MeetingDialog.this.epoch;
              }

              public boolean current(long owner) {
                return live(owner);
              }

              public JSONObject state() {
                return MeetingDialog.this.state;
              }

              public void request(JSONObject args) {
                MeetingDialog.this.request(args);
              }

              public void toast(String message) {
                MeetingDialog.this.toast(message);
              }
            });
  }

  private boolean live() {
    return dialog != null
        && session.currentRoute(route)
        && session.isConnected()
        && session.supportsMeetings();
  }

  private boolean live(long owner) {
    return owner == epoch && live();
  }

  private void toast(String value) {
    Toast.makeText(activity, value, Toast.LENGTH_LONG).show();
  }

  void close() {
    epoch++;
    long oldRoute = route;
    route = -1;
    handler.removeCallbacks(poll);
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    pending = action = readingRow = readingId = "";
    text = null;
    analysisDialog.close();
    AlertDialog old = dialog;
    dialog = null;
    if (child != null) child.dismiss();
    if (confirmation != null) confirmation.dismiss();
    child = confirmation = null;
    if (old != null) old.dismiss();
    if (oldRoute >= 0) session.leaveMeeting(oldRoute);
  }

  void open() {
    close();
    final long viewOwner = epoch;
    route = session.connectedRoute();
    if (route < 0 || !session.supportsMeetings()) {
      toast("请先连接支持会议模式的 Mac 后台。");
      return;
    }
    LinearLayout panel = new LinearLayout(activity);
    panel.setOrientation(LinearLayout.VERTICAL);
    panel.setPadding(24, 14, 24, 14);
    info = new TextView(activity);
    info.setTextSize(26);
    panel.addView(info);
    LinearLayout controls = new LinearLayout(activity);
    start = button(controls, "开始会议", this::prepare);
    pause = button(controls, "暂停录音", () -> request(forMeeting("pause")));
    end = button(controls, "结束会议", () -> request(forMeeting("end")));
    panel.addView(controls);
    LinearLayout results = new LinearLayout(activity);
    transcript =
        button(
            results,
            "查看／校对文字",
            () -> {
              JSONObject args = forMeeting("index");
              Wire.put(args, "revision", state.optLong("revision"));
              Wire.put(args, "offset", 0);
              request(args);
            });
    delete =
        button(
            results,
            "删除会议",
            () -> {
              final String meeting = state.optString("id");
              final long revision = state.optLong("revision");
              confirmation =
                  new LargeDialogBuilder(activity)
                      .setTitle("删除这场会议？")
                      .setMessage("将删除本机内存中的会议转写和校对内容，不能撤回。已明确导出的文件需在‘已导出文件’中单独删除。")
                      .setNegativeButton("保留", null)
                      .setPositiveButton(
                          "删除",
                          (d, n) -> {
                            if (!live(viewOwner) || confirmation != d) return;
                            JSONObject args = args("delete");
                            Wire.put(args, "id", meeting);
                            Wire.put(args, "revision", revision);
                            Wire.put(args, "confirmed", true);
                            request(args);
                          })
                      .show();
              AlertDialog owner = confirmation;
              owner.setOnDismissListener(
                  d -> {
                    if (confirmation == owner) confirmation = null;
                  });
            });
    panel.addView(results);
    LinearLayout documents = new LinearLayout(activity);
    export = button(documents, "导出文字", this::exportText);
    button(documents, "已导出文件", () -> request(args("exports")));
    panel.addView(documents);
    LinearLayout analysis = new LinearLayout(activity);
    summarize = button(analysis, "Dot 总结", () -> requestAnalysis("summarize", "zh"));
    translate = button(analysis, "翻译文字", analysisDialog::chooseTranslation);
    analysisResults = button(analysis, "查看分析", analysisDialog::openResults);
    panel.addView(analysis);
    LinearLayout processing = new LinearLayout(activity);
    cancelAnalysis =
        button(
            processing,
            "取消文字处理",
            () -> {
              JSONObject job = state.optJSONObject("analysis_job");
              if (job == null) return;
              JSONObject args = forMeeting("cancel_analysis");
              Wire.put(args, "analysis_id", job.optString("id"));
              request(args);
            });
    panel.addView(processing);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(panel);
    dialog =
        new LargeDialogBuilder(activity)
            .setTitle("会议转写")
            .setView(scroll)
            .setNegativeButton("关闭并暂停", (d, n) -> close())
            .create();
    AlertDialog owner = dialog;
    dialog.setOnDismissListener(
        d -> {
          if (dialog == owner) close();
        });
    dialog.show();
    state = new JSONObject();
    render();
    request(args("status"));
    handler.postDelayed(poll, 1000);
  }

  private Button button(LinearLayout parent, String title, Runnable click) {
    final long owner = epoch;
    Button value = new Button(activity);
    value.setAllCaps(false);
    value.setText(title);
    value.setTextSize(24);
    value.setOnClickListener(
        v -> {
          if (live(owner)) click.run();
        });
    parent.addView(
        value, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
    return value;
  }

  private final Runnable poll =
      new Runnable() {
        public void run() {
          if (!live()) {
            close();
            return;
          }
          if (pending.isEmpty()
              && child == null
              && confirmation == null
              && !analysisDialog.isOpen()) request(args("status"));
          handler.postDelayed(this, 1000);
        }
      };

  private JSONObject args(String action) {
    JSONObject value = new JSONObject();
    Wire.put(value, "action", action);
    return value;
  }

  private JSONObject forMeeting(String action) {
    JSONObject value = args(action);
    Wire.put(value, "id", state.optString("id"));
    return value;
  }

  private void prepare() {
    if (Build.VERSION.SDK_INT >= 23
        && activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
      activity.requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, 35);
      return;
    }
    JSONObject args =
        "paused".equals(state.optString("state")) ? forMeeting("prepare_resume") : args("prepare");
    if ("prepare_resume".equals(args.optString("action")))
      Wire.put(args, "revision", state.optLong("revision"));
    request(args);
  }

  private void confirm(JSONObject offer) {
    if (!offer.optString("id").matches("[a-f0-9]{32}")) return;
    final long owner = epoch;
    final String id = state.optString("id");
    final boolean resume = "resume".equals(offer.optString("mode"));
    CheckBox consent = new CheckBox(activity);
    consent.setTextSize(26);
    consent.setText("已获得所有参与者同意，将录音片段发送 MiniMax 转写。\n默认不保存原音；文字只保留在本机内存。关闭页面即暂停录音。");
    consent.setPadding(24, 18, 24, 18);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(consent);
    AlertDialog value =
        new LargeDialogBuilder(activity)
            .setTitle(resume ? "继续会议录音" : "开始会议录音")
            .setView(scroll)
            .setNegativeButton("取消", null)
            .setPositiveButton("确认录音", null)
            .create();
    confirmation = value;
    value.setOnDismissListener(
        d -> {
          if (confirmation == value) confirmation = null;
        });
    value.setOnShowListener(
        d ->
            value
                .getButton(AlertDialog.BUTTON_POSITIVE)
                .setOnClickListener(
                    v -> {
                      if (!live(owner) || confirmation != value || !value.isShowing()) {
                        value.dismiss();
                        return;
                      }
                      if (!consent.isChecked()) {
                        toast("请先获得参与者同意并勾选确认。");
                        return;
                      }
                      JSONObject args = args(resume ? "resume" : "start");
                      Wire.put(args, "offer_id", offer.optString("id"));
                      Wire.put(args, "confirmed", true);
                      if (resume) Wire.put(args, "id", id);
                      else Wire.put(args, "save_audio", false);
                      value.dismiss();
                      request(args);
                    }));
    value.show();
  }

  private void request(JSONObject args) {
    if (!live()) return;
    if (timeout != null) handler.removeCallbacks(timeout);
    pending = WorkflowModel.id();
    action = args.optString("action");
    final String id = pending;
    timeout =
        () -> {
          if (!id.equals(pending) || !live()) return;
          pending = action = "";
          analysisDialog.failed();
          session.leaveMeeting(route);
          info.setText("会议请求未确认，已停止本机会议录音。请刷新查看，不会自动重试。");
        };
    handler.postDelayed(timeout, 12000);
    if (!session.meetingRequest(id, args, route)) {
      if ("analysis_text".equals(action)) analysisDialog.failed();
      handler.removeCallbacks(timeout);
      pending = action = "";
      toast("请求未发送，请等待音频释放、检查权限和连接后重新确认。");
    }
  }

  void accept(JSONObject value, long owner) {
    if (!live() || owner != route) return;
    boolean reply = !pending.isEmpty() && pending.equals(value.optString("request_id"));
    if (!reply && !"meeting_update".equals(value.optString("event"))) return;
    if (reply && !action.equals(value.optString("action"))) return;
    JSONObject current = value.optJSONObject("state");
    if (current != null) {
      if (!state.optString("id").equals(current.optString("id"))
          || current.optLong("revision", -1) >= state.optLong("revision", -1)) {
        state = current;
        render();
      }
    }
    if (!pending.equals(value.optString("request_id")) || pending.isEmpty()) return;
    String completed = action;
    pending = action = "";
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    if (!Boolean.TRUE.equals(value.opt("ok"))) {
      if ("analysis_text".equals(completed)) analysisDialog.failed();
      toast(value.optString("message", "会议状态已变化，请刷新查看。"));
      return;
    }
    if ("prepare".equals(completed) || "prepare_resume".equals(completed)) {
      JSONObject offer = value.optJSONObject("offer");
      if (offer != null) confirm(offer);
    } else if ("index".equals(completed)) showRows(value.optJSONObject("page"));
    else if ("text".equals(completed)) readPage(value.optJSONObject("page"));
    else if ("edit".equals(completed) && child != null) {
      child.dismiss();
      child = null;
    } else if ("exports".equals(completed)) showExports(value.optJSONObject("exports"));
    else if ("export".equals(completed))
      showExport(value.optJSONObject("export"), value.optString("local_path"));
    else if ("delete_export".equals(completed)) request(args("exports"));
    else if ("analysis_text".equals(completed))
      analysisDialog.acceptPage(value.optJSONObject("page"));
  }

  private void render() {
    boolean exists = state.optBoolean("available");
    String mode = state.optString("state");
    long seconds = state.optLong("position_samples") / 16000;
    String label =
        "capturing".equals(mode)
            ? (session.isMeetingCapturing() ? "会议录音中" : "正在准备会议录音")
            : "paused".equals(mode) ? "已暂停录音" : "ended".equals(mode) ? "会议已结束" : "尚未开始会议";
    String message =
        label
            + " · "
            + seconds / 60
            + " 分 "
            + seconds % 60
            + " 秒\n"
            + "待转写 "
            + state.optInt("pending_count")
            + " 段 · 缺口／未确认 "
            + state.optInt("gap_count")
            + " 段\n"
            + (state.optString("reason").isEmpty() ? "原音不保存，转写文字请核对。" : "存在中断或未确认内容，请核对文字。");
    if (session.meetingMediaBlocked()) message = "麦克风释放未确认。请关闭并重启助手后再录音；已有会议文字仍可查看。 ";
    showText(start, "paused".equals(mode) ? "继续录音" : "开始会议");
    start.setEnabled(!session.meetingMediaBlocked() && (!exists || "paused".equals(mode)));
    pause.setEnabled("capturing".equals(mode));
    end.setEnabled("capturing".equals(mode) || "paused".equals(mode));
    transcript.setEnabled(state.optInt("row_count") > 0);
    delete.setEnabled(exists && !"capturing".equals(mode) && state.optInt("pending_count") == 0);
    export.setEnabled("ended".equals(mode) && state.optInt("pending_count") == 0);
    JSONObject job = state.optJSONObject("analysis_job");
    String progress = job == null ? "" : job.optString("state");
    boolean busy = "queued".equals(progress) || "running".equals(progress);
    boolean ready =
        "ended".equals(mode)
            && state.optInt("pending_count") == 0
            && state.optInt("row_count") > 0
            && !busy;
    summarize.setEnabled(ready);
    translate.setEnabled(ready);
    cancelAnalysis.setEnabled(busy);
    JSONArray results = state.optJSONArray("analyses");
    analysisResults.setEnabled(results != null && results.length() > 0);
    if (job != null)
      message +=
          "\nDot 文字处理："
              + (busy
                  ? "正在处理，可取消"
                  : "completed".equals(progress)
                      ? "结果已生成，请核对"
                      : "failed".equals(progress)
                          ? job.optString("message", "未取得完整结果，未保存部分生成内容")
                          : "已取消")
              + "\n仅处理文字，不自动执行会议待办。";
    showText(info, message);
  }

  private static void showText(TextView view, String value) {
    // Repeated identical status polls must not keep accessibility and layout busy.
    if (!value.contentEquals(view.getText())) view.setText(value);
  }

  private void requestAnalysis(String kind, String language) {
    JSONObject args = forMeeting(kind);
    Wire.put(args, "revision", state.optLong("revision"));
    Wire.put(args, "confirmed", true);
    if ("translate".equals(kind)) Wire.put(args, "language", language);
    request(args);
  }

  private void showRows(JSONObject page) {
    if (page == null) return;
    JSONArray rows = page.optJSONArray("rows");
    if (rows == null) return;
    final long viewOwner = epoch;
    String[] labels = new String[rows.length() + (page.isNull("next_offset") ? 0 : 1)];
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      long seconds = row.optLong("start_sample") / 16000;
      labels[i] =
          seconds / 60
              + ":"
              + String.format(java.util.Locale.ROOT, "%02d", seconds % 60)
              + " · "
              + rowLabel(row.optString("state"))
              + (row.optBoolean("edited") ? " · 已校对" : "");
    }
    if (labels.length > rows.length()) labels[labels.length - 1] = "下一页";
    child =
        new LargeDialogBuilder(activity)
            .setTitle("会议片段")
            .setItems(
                labels,
                (d, index) -> {
                  if (!live(viewOwner) || child != d) return;
                  child = null;
                  if (index == rows.length()) {
                    JSONObject args = args("index");
                    Wire.put(args, "id", page.optString("id"));
                    Wire.put(args, "revision", page.optLong("revision"));
                    Wire.put(args, "offset", page.optInt("next_offset"));
                    request(args);
                  } else {
                    JSONObject row = rows.optJSONObject(index);
                    readingRow = row.optString("id");
                    readingId = page.optString("id");
                    readingRevision = page.optLong("revision");
                    text = new StringBuilder();
                    requestText(0);
                  }
                })
            .setNegativeButton("关闭", null)
            .show();
    AlertDialog owner = child;
    owner.setOnDismissListener(
        d -> {
          if (child == owner) child = null;
        });
  }

  private void requestText(int offset) {
    readingOffset = offset;
    JSONObject args = args("text");
    Wire.put(args, "id", readingId);
    Wire.put(args, "revision", readingRevision);
    Wire.put(args, "row_id", readingRow);
    Wire.put(args, "offset", offset);
    Wire.put(args, "original", false);
    request(args);
  }

  private void readPage(JSONObject page) {
    if (page == null
        || text == null
        || !readingId.equals(page.optString("id"))
        || readingRevision != page.optLong("revision", -1)
        || readingOffset != page.optInt("offset", -1)
        || page.optJSONObject("row") == null
        || !readingRow.equals(page.optJSONObject("row").optString("id"))) return;
    String part = page.optString("text");
    text.append(part);
    if (text.length() > 24000) {
      text = null;
      toast("文字超过显示上限，请重新查看。");
      return;
    }
    if (!page.isNull("next_offset")) {
      int next = page.optInt("next_offset", -1);
      if (part.isEmpty() || next != readingOffset + part.codePointCount(0, part.length())) {
        text = null;
        toast("文字分页状态已变化，请重新查看。");
        return;
      }
      requestText(next);
      return;
    }
    EditText edit = new EditText(activity);
    edit.setTextSize(26);
    edit.setMinLines(3);
    edit.setMaxLines(8);
    edit.setFilters(new InputFilter[] {new InputFilter.LengthFilter(24000)});
    edit.setText(text.toString());
    JSONObject row = page.optJSONObject("row");
    boolean correctable =
        ("recognized".equals(row.optString("state")) || "empty".equals(row.optString("state")))
            && !"capturing".equals(state.optString("state"))
            && state.optInt("pending_count") == 0;
    edit.setEnabled(correctable);
    final String meeting = readingId, item = readingRow;
    final long revision = readingRevision;
    final long viewOwner = epoch;
    AlertDialog.Builder builder =
        new LargeDialogBuilder(activity)
            .setTitle("会议转写 · 请核对")
            .setMessage("文字是识别结果，不保证逐字准确；未确认片段不代表没有人说话。")
            .setView(edit)
            .setNegativeButton("关闭", null)
            .setNeutralButton(
                "复制",
                (d, n) -> {
                  if (!live(viewOwner) || child != d) return;
                  ((ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE))
                      .setPrimaryClip(ClipData.newPlainText("会议转写", edit.getText().toString()));
                });
    if (correctable) builder.setPositiveButton("保存校对", null);
    child = builder.create();
    AlertDialog owner = child;
    owner.setOnDismissListener(
        d -> {
          if (child == owner) child = null;
        });
    owner.setOnShowListener(
        d -> {
          if (!correctable) return;
          owner
              .getButton(AlertDialog.BUTTON_POSITIVE)
              .setOnClickListener(
                  v -> {
                    if (!live(viewOwner)
                        || child != owner
                        || !owner.isShowing()
                        || !pending.isEmpty()) return;
                    String value = edit.getText().toString();
                    if (value.codePointCount(0, value.length()) > 12000) {
                      toast("每段文字最多 12000 字符。");
                      return;
                    }
                    JSONObject args = args("edit");
                    Wire.put(args, "id", meeting);
                    Wire.put(args, "revision", revision);
                    Wire.put(args, "row_id", item);
                    Wire.put(args, "text", value);
                    Wire.put(args, "confirmed", true);
                    request(args);
                  });
        });
    owner.show();
    text = null;
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

  private void exportText() {
    final long owner = epoch, revision = state.optLong("revision");
    final String meeting = state.optString("id");
    showChild(
        new LargeDialogBuilder(activity)
            .setTitle("导出完整转写到当前 Mac")
            .setItems(
                new String[] {"保存为 TXT（不含录音）", "保存为 JSON（不含录音）"},
                (d, index) -> {
                  if (!live(owner) || child != d || index < 0 || index > 1) return;
                  child = null;
                  JSONObject args = args("export");
                  Wire.put(args, "id", meeting);
                  Wire.put(args, "revision", revision);
                  Wire.put(args, "format", index == 0 ? "txt" : "json");
                  Wire.put(args, "confirmed", true);
                  request(args);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private static JSONObject exportReceipt(JSONObject value) {
    if (value == null) return null;
    String id = value.optString("id"), checksum = value.optString("sha256");
    long bytes = value.optLong("bytes", -1);
    String format = value.optString("format");
    if (!id.matches("(?:[a-f0-9]{32}_r(?:0|[1-9][0-9]{0,18})\\.(?:txt|json)|pending-[a-f0-9]{32})")
        || !checksum.matches("[a-f0-9]{64}")
        || bytes < 0
        || bytes > 2101248
        || !(value.opt("integrity_confirmed") instanceof Boolean)) return null;
    boolean complete = value.optBoolean("integrity_confirmed");
    if (complete
        ? !("txt".equals(format) || "json".equals(format)) || !id.endsWith("." + format)
        : !"incomplete".equals(format)) return null;
    JSONObject frozen = new JSONObject();
    Wire.put(frozen, "id", id);
    Wire.put(frozen, "sha256", checksum);
    Wire.put(frozen, "bytes", bytes);
    Wire.put(frozen, "format", format);
    Wire.put(frozen, "integrity_confirmed", complete);
    return frozen;
  }

  private void showExports(JSONObject value) {
    JSONArray items = value == null ? null : value.optJSONArray("items");
    if (items == null || items.length() > 16) {
      toast("导出文件列表未确认，请重新查看。");
      return;
    }
    final long owner = epoch;
    final JSONObject[] frozen = new JSONObject[items.length()];
    String[] labels = new String[items.length()];
    for (int i = 0; i < items.length(); i++) {
      frozen[i] = exportReceipt(items.optJSONObject(i));
      if (frozen[i] == null) {
        toast("导出文件回执未确认，请重新查看。");
        return;
      }
      labels[i] =
          (frozen[i].optBoolean("integrity_confirmed") ? "已保存 · " : "不完整／待核对 · ")
              + frozen[i].optString("id");
    }
    LargeDialogBuilder builder = new LargeDialogBuilder(activity);
    builder.setTitle("已导出文件 · 当前 Mac").setNegativeButton("关闭", null);
    if (items.length() == 0)
      builder.setMessage(
          value.optBoolean("unknown_files")
              ? "没有可确认的会议导出；目录含未知文件，未删除或覆盖。请在 Mac 核对。"
              : "还没有已导出的会议文字。会议不会自动保存到磁盘。");
    else
      builder.setItems(
          labels,
          (d, index) -> {
            if (!live(owner) || child != d || index < 0 || index >= frozen.length) return;
            child = null;
            showExport(frozen[index], "");
          });
    showChild(builder.create());
  }

  private void showExport(JSONObject received, String localPath) {
    final JSONObject file = exportReceipt(received);
    if (file == null) {
      toast("导出回执未确认，请查看已导出文件。");
      return;
    }
    final long owner = epoch;
    final String path =
        localPath.length() <= 4096 && localPath.indexOf('\n') < 0 && localPath.indexOf('\r') < 0
            ? localPath
            : "";
    AlertDialog.Builder builder =
        new LargeDialogBuilder(activity)
            .setTitle(file.optBoolean("integrity_confirmed") ? "已保存会议文字" : "导出结果未确认")
            .setMessage(
                file.optString("id")
                    + "\n"
                    + file.optLong("bytes")
                    + " 字节\n"
                    + (file.optBoolean("integrity_confirmed")
                        ? "文件及校验回执完整；转写准确度仍需核对。"
                        : "文件不完整或校验失败，不当作成功导出。")
                    + (path.isEmpty() ? "" : "\n保存位置：\n" + path)
                    + "\n不包含录音；删除内存会议不会删除这份文件。")
            .setNegativeButton("关闭", null)
            .setPositiveButton(
                "删除这份导出",
                (d, n) -> {
                  if (!live(owner) || child != d) return;
                  child = null;
                  confirmDeleteExport(file, owner);
                });
    if (!path.isEmpty())
      builder.setNeutralButton(
          "复制路径",
          (d, n) -> {
            if (!live(owner) || child != d) return;
            ((ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE))
                .setPrimaryClip(ClipData.newPlainText("会议导出路径", path));
          });
    showChild(builder.create());
  }

  private void confirmDeleteExport(JSONObject file, long owner) {
    confirmation =
        new LargeDialogBuilder(activity)
            .setTitle("删除这份会议导出？")
            .setMessage(file.optString("id") + "\n只删除当前 Mac 中这份导出文件和校验回执，不删除内存中的转写或其他修订。")
            .setNegativeButton("保留", null)
            .setPositiveButton(
                "确认删除",
                (d, n) -> {
                  if (!live(owner) || confirmation != d) return;
                  JSONObject args = args("delete_export");
                  Wire.put(args, "export_id", file.optString("id"));
                  Wire.put(args, "sha256", file.optString("sha256"));
                  Wire.put(args, "confirmed", true);
                  request(args);
                })
            .create();
    final AlertDialog value = confirmation;
    value.setOnDismissListener(
        d -> {
          if (confirmation == value) confirmation = null;
        });
    value.show();
  }

  private static String rowLabel(String state) {
    if ("recognized".equals(state)) return "识别文字";
    if ("empty".equals(state)) return "未识别出文字";
    if ("pending".equals(state)) return "正在转写";
    if ("gap".equals(state)) return "传输缺口";
    if ("too_short".equals(state)) return "片段过短，未转写";
    return "结果未确认";
  }
}
