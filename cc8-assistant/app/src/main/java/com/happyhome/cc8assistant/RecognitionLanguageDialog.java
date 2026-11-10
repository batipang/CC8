package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import android.view.View;
import org.json.JSONObject;

/** An explicit hint for future MiniMax utterances, never an ASR text rewrite. */
final class RecognitionLanguageDialog {
  private final Activity activity;
  private final VoiceSession session;
  private AlertDialog dialog;

  RecognitionLanguageDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  void close() {
    AlertDialog previous = dialog;
    dialog = null;
    if (previous != null) previous.dismiss();
  }

  void open() {
    final long route = session.connectedRoute();
    JSONObject current = session.voicePreferences(route);
    if (current == null || !Boolean.TRUE.equals(current.opt("recognition_language_supported"))) {
      Toast.makeText(activity, "当前后台或识别提供方不支持语言提示", Toast.LENGTH_LONG).show();
      return;
    }
    close();
    final int revision = current.optInt("revision");
    LinearLayout content = new LinearLayout(activity);
    content.setOrientation(LinearLayout.VERTICAL);
    content.setPadding(24, 12, 24, 12);
    TextView help = new TextView(activity);
    help.setTextSize(26);
    help.setText(
        "先暂停语音并等待任务结束，再应用。自动适合中英混合；中文或英文向 MiniMax 提供语言提示，不能保证听写更准确。\n"
            + "不改写识别原文，不改变本地唤醒、回声处理或音色。仅本次 Mac 服务有效，重启恢复自动；应用不会开启麦克风。");
    content.addView(help);
    RadioGroup group = new RadioGroup(activity);
    String[] values = {"auto", "zh", "en"};
    String[] labels = {"自动／中英混合（默认）", "中文提示", "英文提示"};
    int[] ids = {View.generateViewId(), View.generateViewId(), View.generateViewId()};
    for (int i = 0; i < values.length; i++) {
      RadioButton button = new RadioButton(activity);
      button.setId(ids[i]);
      button.setTextSize(26);
      button.setText(labels[i]);
      group.addView(button);
      if (values[i].equals(current.optString("recognition_language"))) group.check(ids[i]);
    }
    content.addView(group);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(content);
    AlertDialog editor =
        new LargeDialogBuilder(activity)
            .setTitle("识别语言")
            .setView(scroll)
            .setNegativeButton("取消", null)
            .setNeutralButton("恢复自动", null)
            .setPositiveButton("应用", null)
            .create();
    dialog = editor;
    editor.setOnShowListener(
        d -> {
          editor.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(v -> group.check(ids[0]));
          editor
              .getButton(AlertDialog.BUTTON_POSITIVE)
              .setOnClickListener(
                  v -> {
                    if (dialog != editor || session.voicePreferences(route) != current) return;
                    for (int i = 0; i < ids.length; i++) {
                      if (group.getCheckedRadioButtonId() != ids[i]) continue;
                      if (!session.setRecognitionLanguage(values[i], revision, route))
                        Toast.makeText(activity, "连接或能力已变化，请重新打开识别语言", Toast.LENGTH_LONG).show();
                      editor.dismiss();
                      break;
                    }
                  });
        });
    editor.setOnDismissListener(
        d -> {
          if (dialog == editor) dialog = null;
        });
    editor.show();
  }
}
