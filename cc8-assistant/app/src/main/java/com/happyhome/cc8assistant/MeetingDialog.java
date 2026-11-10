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
  private Button start, pause, end, transcript, delete;
  private long route = -1;
  private JSONObject state = new JSONObject();
  private String pending = "", action = "";
  private Runnable timeout;
  private String readingRow = "", readingId = "";
  private long readingRevision;
  private int readingOffset;
  private StringBuilder text;

  MeetingDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  private boolean live() {
    return dialog != null
        && session.currentRoute(route)
        && session.isConnected()
        && session.supportsMeetings();
  }

  private void toast(String value) {
    Toast.makeText(activity, value, Toast.LENGTH_LONG).show();
  }

  void close() {
    long oldRoute = route;
    route = -1;
    handler.removeCallbacks(poll);
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    pending = action = readingRow = readingId = "";
    text = null;
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
    route = session.connectedRoute();
    if (route < 0 || !session.supportsMeetings()) {
      toast("请先连接支持会议模式的 Mac 后台。");
      return;
    }
    LinearLayout panel = new LinearLayout(activity);
    panel.setOrientation(LinearLayout.VERTICAL);
    panel.setPadding(24, 14, 24, 14);
    info = new TextView(activity);
    info.setTextSize(23);
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
              confirmation =
                  new AlertDialog.Builder(activity)
                      .setTitle("删除这场会议？")
                      .setMessage("将删除本机内存中的会议转写和校对内容，不能撤回。")
                      .setNegativeButton("保留", null)
                      .setPositiveButton(
                          "删除",
                          (d, n) -> {
                            JSONObject args = forMeeting("delete");
                            Wire.put(args, "revision", state.optLong("revision"));
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
    dialog =
        new AlertDialog.Builder(activity)
            .setTitle("会议转写")
            .setView(panel)
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
    Button value = new Button(activity);
    value.setText(title);
    value.setTextSize(20);
    value.setOnClickListener(
        v -> {
          if (live()) click.run();
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
          if (pending.isEmpty() && child == null && confirmation == null) request(args("status"));
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
    final long owner = route;
    final String id = state.optString("id");
    final boolean resume = "resume".equals(offer.optString("mode"));
    CheckBox consent = new CheckBox(activity);
    consent.setTextSize(22);
    consent.setText("已获得所有参与者同意，将录音片段发送 MiniMax 转写。\n默认不保存原音；文字只保留在本机内存。关闭页面即暂停录音。");
    consent.setPadding(24, 18, 24, 18);
    AlertDialog value =
        new AlertDialog.Builder(activity)
            .setTitle(resume ? "继续会议录音" : "开始会议录音")
            .setView(consent)
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
                      if (!live() || owner != route) {
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
          session.leaveMeeting(route);
          info.setText("会议请求未确认，已停止本机会议录音。请刷新查看，不会自动重试。");
        };
    handler.postDelayed(timeout, 12000);
    if (!session.meetingRequest(id, args, route)) {
      handler.removeCallbacks(timeout);
      pending = action = "";
      toast("请求未发送，请等待音频释放、检查权限和连接后重新确认。");
    }
  }

  void accept(JSONObject value, long owner) {
    if (!live() || owner != route) return;
    JSONObject current = value.optJSONObject("state");
    if (current != null) {
      state = current;
      render();
    }
    if (!pending.equals(value.optString("request_id")) || pending.isEmpty()) return;
    String completed = action;
    pending = action = "";
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    if (!Boolean.TRUE.equals(value.opt("ok"))) {
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
    }
  }

  private void render() {
    boolean exists = state.optBoolean("available");
    String mode = state.optString("state");
    long seconds = state.optLong("position_samples") / 16000;
    String label =
        "capturing".equals(mode)
            ? (session.isMeetingCapturing() ? "会议录音中" : "正在准备会议录音")
            : "paused".equals(mode) ? "已暂停录音" : "ended".equals(mode) ? "会议已结束" : "尚未开始会议";
    info.setText(
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
            + (state.optString("reason").isEmpty() ? "原音不保存，转写文字请核对。" : "存在中断或未确认内容，请核对文字。"));
    if (session.meetingMediaBlocked()) info.setText("麦克风释放未确认。请关闭并重启助手后再录音；已有会议文字仍可查看。 ");
    start.setText("paused".equals(mode) ? "继续录音" : "开始会议");
    start.setEnabled(!session.meetingMediaBlocked() && (!exists || "paused".equals(mode)));
    pause.setEnabled("capturing".equals(mode));
    end.setEnabled("capturing".equals(mode) || "paused".equals(mode));
    transcript.setEnabled(state.optInt("row_count") > 0);
    delete.setEnabled(exists && !"capturing".equals(mode) && state.optInt("pending_count") == 0);
  }

  private void showRows(JSONObject page) {
    if (page == null) return;
    JSONArray rows = page.optJSONArray("rows");
    if (rows == null) return;
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
        new AlertDialog.Builder(activity)
            .setTitle("会议片段")
            .setItems(
                labels,
                (d, index) -> {
                  child = null;
                  if (!live()) return;
                  if (index == rows.length()) {
                    JSONObject args = forMeeting("index");
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
    edit.setTextSize(22);
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
    AlertDialog.Builder builder =
        new AlertDialog.Builder(activity)
            .setTitle("会议转写 · 请核对")
            .setMessage("文字是识别结果，不保证逐字准确；未确认片段不代表没有人说话。")
            .setView(edit)
            .setNegativeButton("关闭", null)
            .setNeutralButton(
                "复制",
                (d, n) ->
                    ((ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE))
                        .setPrimaryClip(ClipData.newPlainText("会议转写", edit.getText().toString())));
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
                    if (!live() || !pending.isEmpty()) return;
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

  private static String rowLabel(String state) {
    if ("recognized".equals(state)) return "识别文字";
    if ("empty".equals(state)) return "未识别出文字";
    if ("pending".equals(state)) return "正在转写";
    if ("gap".equals(state)) return "传输缺口";
    if ("too_short".equals(state)) return "片段过短，未转写";
    return "结果未确认";
  }
}
