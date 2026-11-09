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

/** Explicit local preferences. Every window/request belongs to one authenticated route. */
final class PreferenceMemoryDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog dialog;
  private String pending = "";
  private long pendingRoute = -1;
  private Runnable timeout;

  PreferenceMemoryDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  void close() {
    pending = "";
    pendingRoute = -1;
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    if (dialog != null) dialog.dismiss();
    dialog = null;
  }

  void open() {
    request(args("list", -1), session.connectedRoute());
  }

  private JSONObject args(String action, int revision) {
    JSONObject value = new JSONObject();
    Wire.put(value, "action", action);
    if (revision >= 0) Wire.put(value, "revision", revision);
    return value;
  }

  private boolean available(long route) {
    if (route >= 0
        && session.currentRoute(route)
        && session.isConnected()
        && session.supportsPreferenceMemory()) return true;
    toast("请连接支持偏好记忆的后台，再重新打开此页。");
    return false;
  }

  private void request(JSONObject args, long route) {
    if (!available(route)) return;
    close();
    final String id = UUID.randomUUID().toString().replace("-", "");
    pending = id;
    pendingRoute = route;
    dialog =
        new AlertDialog.Builder(activity)
            .setTitle("偏好记忆")
            .setMessage("正在核对 Mac 本机记录。关闭等待不会撤销已提交的修改；状态未确认时请刷新，不会自动重试。")
            .setNegativeButton("关闭等待", (d, n) -> close())
            .create();
    dialog.setOnCancelListener(d -> close());
    dialog.show();
    timeout =
        () -> {
          if (!id.equals(pending)) return;
          close();
          toast("记忆状态未确认，请重新打开核对。");
        };
    handler.postDelayed(timeout, 10000);
    if (!session.preferenceRequest(id, args, route)) {
      close();
      toast("请求未提交，请重新连接并核对记录。");
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
    JSONObject state = data.optJSONObject("state");
    if (!Boolean.TRUE.equals(data.opt("ok")) || state == null) {
      toast(data.optString("message", "记忆状态未确认，请重新打开核对。"));
      return;
    }
    showList(state, route);
  }

  private void showList(JSONObject state, long route) {
    if (!available(route)) return;
    JSONArray items = state.optJSONArray("items");
    Object rawRevision = state.opt("revision");
    if (!(state.opt("schema") instanceof Integer)
        || state.optInt("schema", 0) != 1
        || !(rawRevision instanceof Integer)
        || ((Integer) rawRevision) < 0
        || ((Integer) rawRevision) >= Integer.MAX_VALUE
        || !(state.opt("enabled") instanceof Boolean)
        || items == null
        || items.length() > 24) {
      toast("记录格式不兼容，请更新后台。");
      return;
    }
    String[] labels = new String[items.length()];
    for (int i = 0; i < labels.length; i++) {
      JSONObject item = items.optJSONObject(i);
      labels[i] = item == null ? "无效记录" : item.optString("key") + "：" + item.optString("value");
    }
    int revision = state.optInt("revision", -1);
    AlertDialog.Builder builder =
        new AlertDialog.Builder(activity)
            .setTitle(
                "偏好记忆 · "
                    + (state.optBoolean("enabled") ? "已开启" : "已关闭")
                    + " · "
                    + labels.length
                    + "/24")
            .setPositiveButton("新增偏好", (d, n) -> edit(null, revision, route))
            .setNeutralButton("记忆设置", (d, n) -> settings(state, route))
            .setNegativeButton("关闭", null);
    if (labels.length == 0)
      builder.setMessage("只保存你明确要求记住的偏好，不自动提取聊天。启用后会随问题交给所选模型；不存录音。修改前请暂停语音并等任务结束。");
    else builder.setItems(labels, (d, n) -> detail(items.optJSONObject(n), revision, route));
    dialog = builder.create();
    dialog.show();
  }

  private void detail(JSONObject item, int revision, long route) {
    if (item == null || !available(route)) return;
    close();
    dialog =
        new AlertDialog.Builder(activity)
            .setTitle(item.optString("key"))
            .setMessage(
                item.optString("value")
                    + "\n\n来源："
                    + ("voice".equals(item.optString("source")) ? "明确语音指令" : "设备上明确保存")
                    + "\n更新时间："
                    + item.optString("updated_at")
                    + "\n适用：本机大黄蜂后续回答。启用时会交给所选模型，不是工具授权。删除不抹除原聊天中的历史文字。")
            .setPositiveButton("修改", (d, n) -> edit(item, revision, route))
            .setNeutralButton(
                "删除",
                (d, n) -> {
                  JSONObject args = args("delete", revision);
                  Wire.put(args, "key", item.optString("key"));
                  request(args, route);
                })
            .setNegativeButton("关闭", null)
            .create();
    dialog.show();
  }

  private void settings(JSONObject state, long route) {
    if (!available(route)) return;
    close();
    int revision = state.optInt("revision", -1);
    boolean enabled = state.optBoolean("enabled");
    dialog =
        new AlertDialog.Builder(activity)
            .setTitle("记忆设置")
            .setMessage(
                "本机独立保存最多24条明确偏好。开启后会随新问题交给所选模型；关闭保留记录，但不再注入。修改会更新独立语音上下文，原桌面聊天和权限不变。删除无法同步清除第三方聊天历史。")
            .setPositiveButton(
                enabled ? "关闭记忆" : "开启记忆",
                (d, n) -> {
                  JSONObject args = args("set_enabled", revision);
                  Wire.put(args, "enabled", !enabled);
                  request(args, route);
                })
            .setNeutralButton(
                "清空全部",
                (d, n) -> {
                  close();
                  dialog =
                      new AlertDialog.Builder(activity)
                          .setTitle("清空全部偏好？")
                          .setMessage("仅清空大黄蜂在这台 Mac 的偏好库，不能撤回已发送给模型的文字。")
                          .setPositiveButton(
                              "确认清空", (yes, which) -> request(args("clear", revision), route))
                          .setNegativeButton("取消", null)
                          .create();
                  dialog.show();
                })
            .setNegativeButton("关闭", null)
            .create();
    dialog.show();
  }

  private void edit(JSONObject item, int revision, long route) {
    if (!available(route)) return;
    close();
    LinearLayout form = new LinearLayout(activity);
    form.setOrientation(LinearLayout.VERTICAL);
    form.setPadding(24, 16, 24, 16);
    EditText key = new EditText(activity);
    key.setTextSize(22);
    key.setHint("偏好名称，例如称呼");
    key.setSingleLine(true);
    key.setFilters(new InputFilter[] {new InputFilter.LengthFilter(40)});
    EditText value = new EditText(activity);
    value.setTextSize(22);
    value.setHint("内容，例如老王；开启后会提供给模型");
    value.setMinLines(2);
    value.setFilters(new InputFilter[] {new InputFilter.LengthFilter(200)});
    if (item != null) {
      key.setText(item.optString("key"));
      key.setEnabled(false);
      value.setText(item.optString("value"));
    }
    form.addView(key);
    form.addView(value);
    form.setFocusableInTouchMode(true);
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(form);
    AlertDialog editor =
        new AlertDialog.Builder(activity)
            .setTitle("明确保存偏好")
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
                    String name = key.getText().toString().trim(),
                        content = value.getText().toString().trim();
                    if (name.isEmpty() || content.isEmpty()) {
                      toast("请填写偏好名称和内容。");
                      return;
                    }
                    JSONObject args = args("put", revision);
                    Wire.put(args, "key", name);
                    Wire.put(args, "value", content);
                    request(args, route);
                  });
        });
    editor.show();
  }

  private void toast(String value) {
    Toast.makeText(activity, value, Toast.LENGTH_LONG).show();
  }
}
