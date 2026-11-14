package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import android.os.Looper;
import android.text.InputFilter;
import android.view.Window;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Toast;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONObject;

/** Explicit wordlist editing and literal correction drafts, scoped to one route and window. */
final class VocabularyDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog dialog;
  private String pending = "";
  private long pendingRoute = -1, epoch;
  private Runnable timeout;

  VocabularyDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  void close() {
    epoch++;
    pending = "";
    pendingRoute = -1;
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    AlertDialog previous = dialog;
    dialog = null;
    if (previous != null) previous.dismiss();
  }

  private void display(AlertDialog shown) {
    dialog = shown;
    shown.setOnDismissListener(
        d -> {
          if (dialog == shown) close();
        });
    shown.show();
  }

  void open() {
    request(args("list", -1), session.connectedRoute());
  }

  private boolean available(long route) {
    if (route >= 0
        && session.currentRoute(route)
        && session.isConnected()
        && session.supportsVocabulary()) return true;
    toast("请连接支持用户词表的后台，再重新打开。");
    return false;
  }

  private JSONObject args(String action, int revision) {
    JSONObject data = new JSONObject();
    Wire.put(data, "action", action);
    if (revision >= 0) Wire.put(data, "revision", revision);
    return data;
  }

  private void request(JSONObject data, long route) {
    if (!available(route)) return;
    close();
    final String id = UUID.randomUUID().toString().replace("-", "");
    pending = id;
    pendingRoute = route;
    dialog =
        new LargeDialogBuilder(activity)
            .setTitle("用户词表")
            .setMessage("正在核对 Mac 词表。关闭等待不会撤销已提交的修改；未确认时请重新查看，不会自动重试。")
            .setNegativeButton("关闭等待", (d, n) -> close())
            .create();
    dialog.setOnCancelListener(d -> close());
    display(dialog);
    timeout =
        () -> {
          if (id.equals(pending)) {
            close();
            toast("词表状态未确认，请重新打开核对。");
          }
        };
    handler.postDelayed(timeout, 10000);
    if (!session.vocabularyRequest(id, data, route)) {
      close();
      toast("请求未提交，请重新打开词表。");
    }
  }

  void accept(JSONObject data, long route) {
    if (pending.isEmpty() || !pending.equals(data.optString("request_id")) || pendingRoute != route)
      return;
    if (!session.currentRoute(route)) {
      close();
      return;
    }
    close();
    if (!Boolean.TRUE.equals(data.opt("ok"))) {
      toast(data.optString("message", "词表状态未确认。"));
      return;
    }
    if (Boolean.TRUE.equals(data.opt("prepared"))) {
      toast("请核对更正草稿，确认后才提交新请求。原任务已经执行的操作不会撤回。");
      return;
    }
    if (!(data.opt("source_id") instanceof String)) {
      toast("词表来源格式不兼容，请重新打开。");
      return;
    }
    VocabularySnapshot state =
        VocabularySnapshot.parse(
            data.optJSONObject("state"),
            data.optString("source_id"),
            data.optJSONArray("suggestions"));
    if (state == null) {
      toast("词表格式不兼容，请核对后台版本。");
      return;
    }
    showList(state, route);
  }

  private void showList(VocabularySnapshot state, long route) {
    if (!available(route)) return;
    final long owner = epoch;
    int suggestions = state.matches.size();
    String[] labels = new String[suggestions + state.items.size()];
    for (int i = 0; i < suggestions; i++) {
      VocabularySnapshot.Match match = state.matches.get(i);
      labels[i] = "本次可更正：" + match.alias + " → " + match.term;
    }
    for (int i = 0; i < state.items.size(); i++) labels[suggestions + i] = state.items.get(i).term;
    AlertDialog.Builder builder =
        new LargeDialogBuilder(activity)
            .setTitle("用户词表 · " + state.items.size() + "/64 · 手动纠错参考")
            .setPositiveButton(
                "新增词语",
                (d, n) -> {
                  if (owner == epoch) edit(null, state.revision, route);
                })
            .setNeutralButton(
                "说明／清空",
                (d, n) -> {
                  if (owner == epoch) explain(state.revision, route);
                })
            .setNegativeButton("关闭", null);
    if (labels.length == 0)
      builder.setMessage(
          "保存正确名称及常见误听写法，之后可以明确选词生成更正草稿。没有向 MiniMax 发送热词，也不会自动改写识别原文。修改前先暂停语音并等任务结束。");
    else
      builder.setItems(
          labels,
          (d, n) -> {
            if (owner != epoch) return;
            if (n < suggestions) {
              VocabularySnapshot.Match match = state.matches.get(n);
              prepare(match.term, match.alias, state.revision, state.sourceId, route);
            } else {
              detail(state.items.get(n - suggestions), state.revision, state.sourceId, route);
            }
          });
    dialog = builder.create();
    display(dialog);
  }

  private void detail(VocabularySnapshot.Entry item, int revision, String sourceId, long route) {
    if (!available(route)) return;
    close();
    final long owner = epoch;
    String[] labels = new String[item.aliases.size() + 2];
    for (int i = 0; i < item.aliases.size(); i++)
      labels[i] = "准备更正：" + item.aliases.get(i) + " → " + item.term;
    labels[item.aliases.size()] = "编辑误听写法";
    labels[item.aliases.size() + 1] = "删除这个词语";
    dialog =
        new LargeDialogBuilder(activity)
            .setTitle(item.term + " · " + item.updatedAt)
            .setItems(
                labels,
                (d, n) -> {
                  if (owner != epoch || !available(route)) return;
                  if (n < item.aliases.size()) {
                    prepare(item.term, item.aliases.get(n), revision, sourceId, route);
                  } else if (n == item.aliases.size()) edit(item, revision, route);
                  else {
                    JSONObject data = args("delete", revision);
                    Wire.put(data, "term", item.term);
                    request(data, route);
                  }
                })
            .setNegativeButton("关闭", null)
            .create();
    display(dialog);
  }

  private void prepare(String term, String alias, int revision, String sourceId, long route) {
    if (sourceId.isEmpty()) {
      toast("没有可更正的本次语音。请开启语音说完整问题，再重新打开词表。");
      return;
    }
    JSONObject data = args("prepare", revision);
    Wire.put(data, "term", term);
    Wire.put(data, "alias", alias);
    Wire.put(data, "source_id", sourceId);
    request(data, route);
  }

  private void explain(int revision, long route) {
    if (!available(route)) return;
    close();
    final long owner = epoch;
    dialog =
        new LargeDialogBuilder(activity)
            .setTitle("用户词表说明")
            .setMessage(
                "正确词与误听写法只保存在这台 Mac，最多64条，每条最多4个误听词，各40字。不自动采集聊天或录音，不作为模型记忆或工具授权。当前 MiniMax 没有已确认的热词参数，词表不会改善云端听写本身。\n\n使用：先开启语音说一个问题，在同一语音会话的两分钟内选一个误听词。只有原文中能唯一定位该完整词时才生成更正草稿；核对后明确确认才提交。关闭语音／Home／换线后请重新提问。")
            .setNeutralButton(
                "清空全部词语",
                (d, n) -> {
                  if (owner != epoch || !available(route)) return;
                  close();
                  final long confirmation = epoch;
                  dialog =
                      new LargeDialogBuilder(activity)
                          .setTitle("清空本机用户词表？")
                          .setMessage("只删除本机词表，不修改原来的识别记录或已执行任务。")
                          .setPositiveButton(
                              "确认清空",
                              (yes, which) -> {
                                if (confirmation == epoch) request(args("clear", revision), route);
                              })
                          .setNegativeButton("取消", null)
                          .create();
                  display(dialog);
                })
            .setPositiveButton("知道了", null)
            .create();
    display(dialog);
  }

  private void edit(VocabularySnapshot.Entry item, int revision, long route) {
    if (!available(route)) return;
    close();
    final long owner = epoch;
    LinearLayout form = new LinearLayout(activity);
    form.setOrientation(LinearLayout.VERTICAL);
    form.setPadding(24, 16, 24, 16);
    form.setFocusableInTouchMode(true);
    EditText term = new EditText(activity);
    term.setTextSize(30);
    term.setHint("正确词语，例如常州");
    term.setSingleLine(true);
    term.setFilters(new InputFilter[] {new InputFilter.LengthFilter(80)});
    EditText aliases = new EditText(activity);
    aliases.setTextSize(30);
    aliases.setHint("常见误听写法，一行一个，最多4个；可留空");
    aliases.setMinLines(4);
    aliases.setFilters(new InputFilter[] {new InputFilter.LengthFilter(324)});
    if (item != null) {
      term.setText(item.term);
      term.setEnabled(false);
      aliases.setText(android.text.TextUtils.join("\n", item.aliases));
    }
    form.addView(term);
    form.addView(aliases);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(form);
    AlertDialog editor =
        new LargeDialogBuilder(activity)
            .setTitle("明确保存词语")
            .setView(scroll)
            .setPositiveButton("保存", null)
            .setNegativeButton("取消", null)
            .create();
    dialog = editor;
    Window window = editor.getWindow();
    if (window != null) {
      window.clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
      window.setSoftInputMode(
          WindowManager.LayoutParams.SOFT_INPUT_STATE_HIDDEN
              | WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
    }
    editor.setOnShowListener(
        d -> {
          form.requestFocus();
          editor
              .getButton(AlertDialog.BUTTON_POSITIVE)
              .setOnClickListener(
                  v -> {
                    if (owner != epoch || !available(route)) return;
                    String correct = term.getText().toString().trim();
                    JSONArray values = new JSONArray();
                    String lines = aliases.getText().toString().trim();
                    if (!VocabularySnapshot.word(correct)) {
                      toast("请填入1–40字正确词语，不含逗号或‘不是’。");
                      return;
                    }
                    if (!lines.isEmpty())
                      for (String line : lines.split("\n", -1)) {
                        String value = line.trim();
                        if (!VocabularySnapshot.word(value)) {
                          toast("每个误听词为1–40字，不含逗号或‘不是’。");
                          return;
                        }
                        values.put(value);
                      }
                    if (values.length() > 4) {
                      toast("每个正确词最多填写4个误听写法。");
                      return;
                    }
                    JSONObject data = args("put", revision);
                    Wire.put(data, "term", correct);
                    Wire.put(data, "aliases", values);
                    request(data, route);
                  });
        });
    display(editor);
  }

  private void toast(String message) {
    Toast.makeText(activity, message, Toast.LENGTH_LONG).show();
  }
}
