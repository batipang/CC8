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

/** Explicit local sensitivity controls; no capture or transcript rewriting. */
final class RecognitionEnvironmentDialog {
  private final Activity activity;
  private final VoiceSession session;
  private AlertDialog dialog;

  RecognitionEnvironmentDialog(Activity activity, VoiceSession session) {
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
    if (current == null || !Boolean.TRUE.equals(current.opt("environment_profiles_supported"))) {
      Toast.makeText(activity, "当前后台不支持听音环境预设", Toast.LENGTH_LONG).show();
      return;
    }
    close();
    final int revision = current.optInt("revision");
    LinearLayout content = new LinearLayout(activity);
    content.setOrientation(LinearLayout.VERTICAL);
    content.setPadding(24, 12, 24, 12);
    TextView help = new TextView(activity);
    help.setTextSize(22);
    help.setText(
        "先暂停语音，等任务和播报结束后应用。普通保持原来的灵敏度；安静更容易接收轻声，也更容易被微小声音触发。电视背景更严格，轻声或远处讲话可能漏掉。\n"
            + "这不是说话人识别，不能保证排除电视人声。保留回声与停止指令门控，识别原文不改写。仅本次 Mac 服务有效，重启恢复普通；应用不会开启麦克风。");
    content.addView(help);
    RadioGroup group = new RadioGroup(activity);
    String[] values = {"normal", "quiet", "television"};
    String[] labels = {"普通（默认）", "安静／轻声", "电视背景／更严格"};
    int[] ids = {View.generateViewId(), View.generateViewId(), View.generateViewId()};
    for (int i = 0; i < values.length; i++) {
      RadioButton button = new RadioButton(activity);
      button.setId(ids[i]);
      button.setTextSize(24);
      button.setText(labels[i]);
      group.addView(button);
      if (values[i].equals(current.optString("environment_profile"))) group.check(ids[i]);
    }
    content.addView(group);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(content);
    AlertDialog editor =
        new AlertDialog.Builder(activity)
            .setTitle("听音环境")
            .setView(scroll)
            .setNegativeButton("取消", null)
            .setNeutralButton("恢复普通", null)
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
                      if (!session.setEnvironmentProfile(values[i], revision, route))
                        Toast.makeText(activity, "连接或能力已变化，请重新打开听音环境", Toast.LENGTH_LONG).show();
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
