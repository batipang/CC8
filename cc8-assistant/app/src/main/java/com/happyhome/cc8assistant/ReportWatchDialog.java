package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import java.util.UUID;
import org.json.JSONObject;

/** Explicit source/interval/channel consent; owns no media, timers or report bodies. */
final class ReportWatchDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog dialog;
  private String pending = "";
  private long route = -1, epoch;
  private Runnable timeout;

  ReportWatchDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
  }

  void open() {
    request(args("watch_state"));
  }

  void close() {
    if (!pending.isEmpty() && session.currentRoute(route)) session.dotChatCancel(pending);
    pending = "";
    epoch++;
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    AlertDialog old = dialog;
    dialog = null;
    if (old != null) old.dismiss();
  }

  private JSONObject args(String action) {
    JSONObject value = new JSONObject();
    Wire.put(value, "action", action);
    return value;
  }

  private boolean live(long owner) {
    return owner == epoch
        && dialog != null
        && session.currentRoute(route)
        && session.supportsReportSources();
  }

  private void show(AlertDialog value) {
    AlertDialog old = dialog;
    dialog = value;
    epoch++;
    if (old != null) old.dismiss();
    value.setOnDismissListener(
        d -> {
          if (dialog == value) close();
        });
    value.show();
  }

  private void toast(String text) {
    Toast.makeText(activity, text, Toast.LENGTH_LONG).show();
  }

  private void request(JSONObject value) {
    close();
    route = session.connectedRoute();
    if (!session.currentRoute(route) || !session.supportsReportSources()) {
      toast("请连接工作汇报后台");
      return;
    }
    pending = UUID.randomUUID().toString().replace("-", "");
    final String id = pending;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("定时工作进展提示")
            .setMessage("正在核对；未确认的修改不会自动重试。")
            .setNegativeButton("关闭等待", (d, n) -> close())
            .create());
    timeout =
        () -> {
          if (id.equals(pending)) {
            close();
            toast("状态未确认，请重新查看，不自动重试");
          }
        };
    handler.postDelayed(timeout, 35000);
    if (!session.reportSourcesRequest(id, value, route)) {
      close();
      toast("连接已变化，请重新查看");
    }
  }

  boolean accept(JSONObject data) {
    if (pending.isEmpty()
        || !pending.equals(data.optString("request_id"))
        || !session.currentRoute(route)) return false;
    pending = "";
    close();
    if (!Boolean.TRUE.equals(data.opt("ok"))) {
      toast(data.optString("message", "状态未确认，请重新查看"));
      return true;
    }
    JSONObject state = data.optJSONObject("report_watch");
    Object body = data.opt("watch_text");
    if (state == null
        || !Integer.valueOf(1).equals(state.opt("schema"))
        || !(state.opt("revision") instanceof Integer)
        || state.optInt("revision") < 0
        || !(state.opt("configured") instanceof Boolean)
        || !(state.opt("enabled") instanceof Boolean)
        || Boolean.TRUE.equals(state.opt("enabled"))
            && !Boolean.TRUE.equals(state.opt("configured"))
        || !(body instanceof String)
        || ((String) body).isEmpty()
        || ((String) body).length() > 10000) {
      toast("定时提示格式不兼容");
      return true;
    }
    final int revision = state.optInt("revision");
    Object rawOffer = data.opt("watch_offer_id");
    final String offer = rawOffer instanceof String ? (String) rawOffer : "";
    Object raw = data.opt("watch_offer_text");
    final String source = raw instanceof String ? (String) raw : "";
    final long owner = epoch + 1;
    AlertDialog.Builder builder =
        new AlertDialog.Builder(activity)
            .setTitle("定时工作进展提示")
            .setMessage((String) body)
            .setNegativeButton("关闭", null);
    if (offer.matches("[a-f0-9]{32}") && !source.isEmpty() && source.length() <= 6000)
      builder.setPositiveButton(
          "设置／修改",
          (d, n) -> {
            if (live(owner)) periods(offer, source, revision);
          });
    if (Boolean.TRUE.equals(state.opt("configured")))
      builder.setNeutralButton(
          "暂停／清除",
          (d, n) -> {
            if (!live(owner)) return;
            final long menu = epoch + 1;
            show(
                new AlertDialog.Builder(activity)
                    .setTitle("管理定时提示")
                    .setItems(
                        new String[] {"暂停提示", "清除配置和提示索引"},
                        (w, choice) -> {
                          if (live(menu))
                            confirm(
                                choice == 0 ? "pause_watch" : "clear_watch",
                                "",
                                "",
                                revision,
                                0,
                                "");
                        })
                    .setNegativeButton("取消", null)
                    .create());
          });
    show(builder.create());
    return true;
  }

  private void periods(String offer, String source, int revision) {
    final long owner = epoch + 1;
    final int[] periods = {300, 900, 1800};
    show(
        new AlertDialog.Builder(activity)
            .setTitle("检查周期")
            .setItems(
                new String[] {"每5分钟", "每15分钟", "每30分钟"},
                (d, n) -> {
                  if (live(owner)) channels(offer, source, revision, periods[n]);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void channels(String offer, String source, int revision, int period) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("提示渠道")
            .setItems(
                new String[] {"CC8短播报", "Mac桌面通知"},
                (d, n) -> {
                  if (live(owner))
                    confirm(
                        "configure_watch",
                        offer,
                        source,
                        revision,
                        period,
                        n == 0 ? "cc8" : "desktop");
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void confirm(
      String action, String offer, String source, int revision, int period, String channel) {
    final long owner = epoch + 1;
    String text =
        action.equals("configure_watch")
            ? source
                + "\n\n从确认时开始，每"
                + period / 60
                + "分钟检查。渠道："
                + (channel.equals("cc8") ? "CC8短播报" : "Mac桌面通知（须先有系统授权）")
                + "。仅保存来源、启用起点、最多256个已提示版本标识及送达认领，不保存结果正文或录音，保留至手动清除。每次只提示最多3项数量和来源，不代表工作已验证或摘要已播完。覆盖不完整会说明；超31天或容量用尽暂停。只在前台、空闲且非安静时段运行；不自动开麦、拍照或启动任务。送达不明不重发。重新设置会清除旧提示索引，从现在重新开始。"
            : action.equals("pause_watch")
                ? "暂停后不再检查或发送新提示；保留配置和提示索引，不取消原工作。"
                : "只清除定时提示的配置和提示索引，不删除来源登记、历史快照、手动补播账本或原工作。";
    show(
        new AlertDialog.Builder(activity)
            .setTitle("确认修改定时提示？")
            .setMessage(text + "\n\n请先暂停语音。未确认时重新查看，不自动重试。")
            .setPositiveButton(
                "确认",
                (d, n) -> {
                  if (!live(owner)) return;
                  JSONObject value = args(action);
                  Wire.put(value, "revision", revision);
                  Wire.put(value, "confirmed", true);
                  if (action.equals("configure_watch")) {
                    Wire.put(value, "offer_id", offer);
                    Wire.put(value, "period_seconds", period);
                    Wire.put(value, "channel", channel);
                  }
                  request(value);
                })
            .setNegativeButton("取消", null)
            .create());
  }
}
