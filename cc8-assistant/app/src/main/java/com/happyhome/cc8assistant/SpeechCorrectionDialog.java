package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.widget.TextView;
import android.widget.ScrollView;
import android.widget.Toast;
import org.json.JSONObject;

/** An explicit input draft, separate from completed work and model/tool approvals. */
final class SpeechCorrectionDialog {
  private final Activity activity;
  private final VoiceSession session;
  private AlertDialog dialog;

  SpeechCorrectionDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  void close() {
    if (dialog != null) dialog.dismiss();
    dialog = null;
  }

  void accept(JSONObject state, long route) {
    if (!session.currentSpeechCorrection(state, route)) return;
    close();
    if (!Boolean.TRUE.equals(state.opt("pending"))) return;
    TextView content = new TextView(activity);
    content.setTextSize(30);
    content.setPadding(24, 16, 24, 16);
    content.setText(
        "原问题：\n"
            + state.optString("original")
            + "\n\n更正后的完整问题：\n"
            + state.optString("proposed")
            + "\n\n确认后会提交一个新请求。原来已经执行的操作不会撤销。"
            + "\n可说“确认更正”或“撤回更正”；草稿两分钟后失效。"
            + "\n只说一个新词时，这个词会作为完整新问题；需要保留上下文时，请明确说“新词，不是旧词”。");
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(content);
    AlertDialog created =
        new LargeDialogBuilder(activity)
            .setTitle("更正草稿 · 尚未提交")
            .setView(scroll)
            .setNegativeButton("撤回更正", null)
            .setPositiveButton("确认更正", null)
            .create();
    dialog = created;
    // Back/outside taps must not leave a hidden, still-confirmable draft.
    created.setCancelable(false);
    created.setOnShowListener(
        unused -> {
          created
              .getButton(AlertDialog.BUTTON_POSITIVE)
              .setOnClickListener(v -> send(created, state, route, "confirm"));
          created
              .getButton(AlertDialog.BUTTON_NEGATIVE)
              .setOnClickListener(v -> send(created, state, route, "cancel"));
        });
    created.show();
  }

  private void send(AlertDialog current, JSONObject state, long route, String action) {
    if (dialog != current) return;
    if (!session.currentSpeechCorrection(state, route)) {
      close();
      return;
    }
    current.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
    current.getButton(AlertDialog.BUTTON_NEGATIVE).setEnabled(false);
    if (!session.speechCorrectionAction(state.optString("draft_id"), action, route)) {
      Toast.makeText(activity, "连接已变化，提交未确认；不会自动重试。请重新提出更正。", Toast.LENGTH_LONG).show();
      close();
    }
  }
}
