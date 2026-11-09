package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import java.util.UUID;
import org.json.JSONObject;

/** Explicit scheduled-report consent and read-only RAM pages; owns no media. */
final class ReportWatchDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog dialog;
  private String pending = "";
  private String pendingAction = "", pendingSnapshot = "";
  private int pendingPage;
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
    pendingAction = pendingSnapshot = "";
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
    pendingAction = value.optString("action");
    pendingSnapshot = value.optString("snapshot_id");
    pendingPage = value.optInt("page");
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
    final String action = pendingAction, snapshot = pendingSnapshot;
    final int requestedPage = pendingPage;
    pending = "";
    close();
    if (!Boolean.TRUE.equals(data.opt("ok"))) {
      toast(data.optString("message", "状态未确认，请重新查看"));
      return true;
    }
    if (action.equals("watch_result") || action.equals("watch_result_page")) {
      ReportTextPage page = ReportTextPage.parse(data, "report_text");
      Object rawId = data.opt("snapshot_id");
      Object ttl = data.opt("watch_result_ttl_ms");
      String id = rawId instanceof String ? (String) rawId : "";
      if (!action.equals(data.opt("action"))
          || !Boolean.TRUE.equals(data.opt("watch_result"))
          || !id.matches("[a-f0-9]{32}")
          || !snapshot.isEmpty() && !snapshot.equals(id)
          || page == null
          || page.index != requestedPage
          || !(ttl instanceof Integer)
          || (Integer) ttl < 1
          || (Integer) ttl > 600000) toast("最近检查页面已变化，请重新查看");
      else showResult(id, page, (Integer) ttl);
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
    final boolean hasResults = Boolean.TRUE.equals(data.opt("watch_has_results"));
    final boolean summarySupported = Boolean.TRUE.equals(data.opt("watch_summary_supported"));
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
            if (live(owner)) periods(offer, source, revision, summarySupported);
          });
    if (Boolean.TRUE.equals(state.opt("configured")))
      builder.setNeutralButton(
          hasResults ? "结果／管理" : "暂停／清除",
          (d, n) -> {
            if (!live(owner)) return;
            final long menu = epoch + 1;
            show(
                new AlertDialog.Builder(activity)
                    .setTitle("管理定时提示")
                    .setItems(
                        hasResults
                            ? new String[] {"暂停提示", "清除配置和提示索引", "最近检查结果（只读）"}
                            : new String[] {"暂停提示", "清除配置和提示索引"},
                        (w, choice) -> {
                          if (!live(menu)) return;
                          if (choice == 2 && hasResults) request(args("watch_result"));
                          else
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

  private void resultPage(String id, int page) {
    JSONObject value = args("watch_result_page");
    Wire.put(value, "snapshot_id", id);
    Wire.put(value, "page", page);
    request(value);
  }

  private void showResult(String id, ReportTextPage page, int ttl) {
    final long owner = epoch + 1;
    AlertDialog.Builder builder =
        new AlertDialog.Builder(activity)
            .setTitle("最近定时检查 · " + (page.index + 1) + "/" + page.count)
            .setMessage(page.text)
            .setNegativeButton("关闭", null);
    if (page.index + 1 < page.count)
      builder.setPositiveButton(
          "下一页",
          (d, n) -> {
            if (live(owner)) resultPage(id, page.index + 1);
          });
    if (page.index > 0)
      builder.setNeutralButton(
          "上一页",
          (d, n) -> {
            if (live(owner)) resultPage(id, page.index - 1);
          });
    show(builder.create());
    timeout =
        () -> {
          if (live(owner)) {
            close();
            toast("最近检查结果已过期");
          }
        };
    handler.postDelayed(timeout, ttl);
  }

  private void periods(String offer, String source, int revision, boolean summarySupported) {
    final long owner = epoch + 1;
    final int[] periods = {300, 900, 1800};
    show(
        new AlertDialog.Builder(activity)
            .setTitle("检查周期")
            .setItems(
                new String[] {"每5分钟", "每15分钟", "每30分钟"},
                (d, n) -> {
                  if (live(owner)) channels(offer, source, revision, periods[n], summarySupported);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void channels(
      String offer, String source, int revision, int period, boolean summarySupported) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("提示渠道")
            .setItems(
                new String[] {"CC8播报", "Mac桌面通知"},
                (d, n) -> {
                  if (!live(owner)) return;
                  if (n == 0 && summarySupported) modes(offer, source, revision, period);
                  else
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

  private void modes(String offer, String source, int revision, int period) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("CC8播报内容")
            .setItems(
                new String[] {"只提示数量和来源", "朗读原答复摘要"},
                (d, n) -> {
                  if (live(owner))
                    confirm(
                        "configure_watch",
                        offer,
                        source,
                        revision,
                        period,
                        "cc8",
                        n == 0 ? "count" : "summary");
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void confirm(
      String action, String offer, String source, int revision, int period, String channel) {
    confirm(action, offer, source, revision, period, channel, "count");
  }

  private void confirm(
      String action,
      String offer,
      String source,
      int revision,
      int period,
      String channel,
      String mode) {
    final long owner = epoch + 1;
    String text =
        action.equals("configure_watch")
            ? source
                + "\n\n从确认时开始，每"
                + period / 60
                + "分钟检查。渠道："
                + (channel.equals("cc8") ? "CC8播报" : "Mac桌面通知（须先有系统授权）")
                + (mode.equals("summary")
                    ? "。每次朗读最多3项原答复摘录，逐项收到设备播完回执才记账；打断保留未确认项，不自动重播。摘录不代表工作已验证。"
                    : "。每次只提示最多3项数量和来源，不代表工作已验证或摘要已播完。")
                + "仅保存来源、启用起点、最多256个已提示版本标识及送达认领，不保存结果正文或录音，保留至手动清除。最近检查正文仅在内存保留10分钟，可只读翻页查看。覆盖不完整会说明；超31天或容量用尽暂停。只在前台、空闲且非安静时段运行；不自动开麦、拍照或启动任务。送达不明不重发。重新设置会清除旧提示索引，从现在重新开始。"
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
                    if (mode.equals("summary")) Wire.put(value, "mode", mode);
                  }
                  request(value);
                })
            .setNegativeButton("取消", null)
            .create());
  }
}
