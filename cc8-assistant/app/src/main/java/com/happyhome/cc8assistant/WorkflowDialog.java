package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import org.json.JSONArray;
import org.json.JSONObject;

/** Route-scoped explicit configuration, run confirmation and RAM result access; no media owner. */
final class WorkflowDialog {
  private final Activity activity;
  private final VoiceSession session;
  private final WorkflowEditor editor;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private AlertDialog dialog;
  private long route = -1, epoch;
  private String pending = "", action = "";
  private String expectedFlow = "", expectedRun = "", expectedStep = "";
  private boolean pendingCamera;
  private Runnable timeout;

  WorkflowDialog(Activity activity, VoiceSession session) {
    this.activity = activity;
    this.session = session;
    editor =
        new WorkflowEditor(
            activity,
            session,
            (flow, revision, ownerRoute) -> {
              JSONObject value = args("put");
              Wire.put(value, "revision", revision);
              Wire.put(value, "value", flow);
              Wire.put(value, "confirmed", true);
              request(value, ownerRoute);
            });
  }

  void close() {
    epoch++;
    pending = action = expectedFlow = expectedRun = expectedStep = "";
    pendingCamera = false;
    if (timeout != null) handler.removeCallbacks(timeout);
    timeout = null;
    editor.close();
    AlertDialog old = dialog;
    dialog = null;
    if (old != null) old.dismiss();
  }

  void open() {
    request(args("list"), session.connectedRoute());
  }

  private JSONObject args(String value) {
    JSONObject data = new JSONObject();
    Wire.put(data, "action", value);
    return data;
  }

  private void toast(String text) {
    Toast.makeText(activity, text, Toast.LENGTH_LONG).show();
  }

  private boolean available(long ownerRoute) {
    return session.currentRoute(ownerRoute) && session.isConnected() && session.supportsWorkflows();
  }

  private boolean live(long owner) {
    return owner == epoch && dialog != null && available(route);
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

  private void request(JSONObject data, long ownerRoute) {
    if (!available(ownerRoute)) {
      toast("请连接支持工作流程的 Mac 后台。");
      return;
    }
    close();
    route = ownerRoute;
    pending = WorkflowModel.id();
    action = data.optString("action");
    expectedFlow = data.optString("id");
    expectedRun = data.optString("run_id");
    expectedStep = data.optString("step_id");
    pendingCamera = !data.optString("camera_nonce").isEmpty();
    final String id = pending;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("工作流程")
            .setMessage("正在核对。关闭等待不会撤回已提交的操作；未确认时请重新查看，不会自动重试。运行中的流程可用主界面的停止按钮打断。")
            .setNegativeButton("关闭等待", (d, n) -> close())
            .create());
    timeout =
        () -> {
          if (id.equals(pending)) {
            close();
            toast("状态未确认，请重新查看；不要重复交办。");
          }
        };
    handler.postDelayed(timeout, 12000);
    if (!session.workflowRequest(id, data, route)) {
      close();
      toast("请求未提交，请先暂停语音、检查连接后重新打开。");
    }
  }

  void accept(JSONObject data, long ownerRoute) {
    if (pending.isEmpty()
        || !pending.equals(data.optString("request_id"))
        || route != ownerRoute
        || !available(ownerRoute)) return;
    String completedAction = action;
    String flowId = expectedFlow, runId = expectedRun, stepId = expectedStep;
    boolean camera = pendingCamera;
    close();
    if (!Boolean.TRUE.equals(data.opt("ok"))) {
      toast(data.optString("message", "状态未确认，请重新查看。"));
      return;
    }
    JSONObject state = data.optJSONObject("state");
    if (!validState(state)) {
      toast("流程格式不兼容，请核对后台版本。");
      return;
    }
    int revision = state.optInt("revision");
    if ("prepare".equals(completedAction)) {
      JSONObject offer = data.optJSONObject("offer");
      if (offer == null
          || offer.optJSONObject("definition") == null
          || !flowId.equals(offer.optJSONObject("definition").opt("id"))) {
        toast("流程确认已变化，请重新核对。");
        return;
      }
      confirmRun(offer);
      return;
    }
    if ("result".equals(completedAction)) {
      JSONObject row = data.optJSONObject("result"), run = state.optJSONObject("run");
      if (row == null
          || run == null
          || !stepId.equals(row.opt("id"))
          || !runId.equals(run.opt("id"))) {
        toast("单步结果已变化，请重新查看。");
        return;
      }
      showResult(row);
      return;
    }
    JSONObject raw = data.optJSONObject("definition");
    if (raw != null) {
      JSONObject flow = WorkflowModel.definition(raw);
      if (flow == null || !flowId.equals(flow.opt("id"))) {
        toast("流程定义格式不兼容或已变化。");
        return;
      }
      detail(flow, revision);
      return;
    }
    if ("run".equals(completedAction) || "cancel".equals(completedAction)) {
      if (camera) {
        toast("流程已受理，请调整接下来的取景画面。结果可稍后在工作流程中查看。");
        return;
      }
      JSONObject run = state.optJSONObject("run");
      if (WorkflowModel.run(run)) {
        runDetails(run);
        return;
      }
    }
    list((JSONObject) WorkflowModel.copy(state));
  }

  private boolean validState(JSONObject state) {
    if (state == null
        || !(state.opt("revision") instanceof Integer)
        || state.optInt("revision") < 0
        || !(state.opt("journal_unconfirmed") instanceof Boolean)) return false;
    JSONArray rows = state.optJSONArray("items"), attempts = state.optJSONArray("attempts");
    if (rows == null || rows.length() > 8 || attempts == null || attempts.length() > 16)
      return false;
    java.util.HashSet<String> ids = new java.util.HashSet<>();
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      if (row == null
          || !WorkflowModel.token(row.opt("id"))
          || !ids.add(row.optString("id"))
          || !WorkflowModel.label(row.opt("name"), 40)
          || !(row.opt("scheduled") instanceof Boolean)
          || !(row.opt("step_count") instanceof Integer)
          || row.optInt("step_count") < 1
          || row.optInt("step_count") > 4) return false;
    }
    return state.has("run")
        && (state.isNull("run") || WorkflowModel.run(state.optJSONObject("run")));
  }

  private void list(JSONObject state) {
    JSONArray rows = state.optJSONArray("items");
    String[] labels = new String[rows.length() + 1];
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      labels[i] =
          row.optString("name")
              + " · "
              + row.optInt("step_count")
              + " 步"
              + (Boolean.TRUE.equals(row.opt("scheduled")) ? " · 已配置定时" : "");
    }
    labels[rows.length()] = "最近一次运行与单步结果";
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("工作流程 · " + rows.length() + "/8")
            .setItems(
                labels,
                (d, n) -> {
                  if (!live(owner)) return;
                  if (n == rows.length()) {
                    JSONObject run = state.optJSONObject("run");
                    if (WorkflowModel.run(run)) runDetails(run);
                    else {
                      toast("本次后台还没有运行记录。");
                      list(state);
                    }
                  } else {
                    JSONObject value = args("get");
                    Wire.put(value, "id", rows.optJSONObject(n).optString("id"));
                    request(value, route);
                  }
                })
            .setPositiveButton(
                "新建流程",
                (d, n) -> {
                  if (!live(owner)) return;
                  JSONObject flow = new JSONObject();
                  Wire.put(flow, "id", WorkflowModel.id());
                  Wire.put(flow, "name", "新流程");
                  Wire.put(flow, "steps", new JSONArray());
                  Wire.put(flow, "schedule", JSONObject.NULL);
                  close();
                  editor.open(flow, state.optInt("revision"), route);
                })
            .setNeutralButton(
                "定时记录／管理",
                (d, n) -> {
                  if (live(owner)) manage(state);
                })
            .setNegativeButton("关闭", null)
            .create());
  }

  private void detail(JSONObject flow, int revision) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle(flow.optString("name"))
            .setItems(
                new String[] {
                  "核对并运行一次", "查看步骤与输入", "编辑步骤／顺序／输入", "定时设置", "绑定所有汇报步骤到当前来源", "解除全部汇报来源绑定", "删除此流程"
                },
                (d, n) -> {
                  if (!live(owner)) return;
                  if (n == 0) {
                    JSONObject value = mutation("prepare", flow.optString("id"), revision, false);
                    request(value, route);
                  } else if (n == 1) {
                    final long viewOwner = epoch + 1;
                    show(
                        new AlertDialog.Builder(activity)
                            .setTitle(flow.optString("name"))
                            .setMessage(WorkflowModel.describe(flow))
                            .setPositiveButton(
                                "返回",
                                (w, b) -> {
                                  if (live(viewOwner)) detail(flow, revision);
                                })
                            .create());
                  } else if (n == 2 || n == 3) {
                    close();
                    if (n == 2) editor.open(flow, revision, route);
                    else editor.schedules(flow, revision, route);
                  } else if (n == 4)
                    confirmMutation(
                        "bind_source",
                        flow.optString("id"),
                        revision,
                        "把本流程全部汇报步骤绑定到你当前明确选定的汇报来源，并关闭原定时。只保存来源元数据，不读取聊天正文。",
                        null);
                  else if (n == 5) {
                    JSONObject changed = (JSONObject) WorkflowModel.copy(flow);
                    Wire.put(changed, "schedule", JSONObject.NULL);
                    JSONArray steps = changed.optJSONArray("steps");
                    for (int i = 0; i < steps.length(); i++)
                      if ("work_report".equals(steps.optJSONObject(i).optString("kind")))
                        Wire.put(steps.optJSONObject(i), "source", JSONObject.NULL);
                    confirmMutation(
                        "put", null, revision, "解除来源绑定，并关闭原定时。之后手动运行前重新核对当前来源。", changed);
                  } else
                    confirmMutation(
                        "delete", flow.optString("id"), revision, "删除流程配置，不撤销已经执行的操作。", null);
                })
            .setNegativeButton("关闭", null)
            .create());
  }

  private JSONObject mutation(String action, String id, int revision, boolean confirmed) {
    JSONObject value = args(action);
    Wire.put(value, "revision", revision);
    if (id != null) Wire.put(value, "id", id);
    if (confirmed) Wire.put(value, "confirmed", true);
    return value;
  }

  private void confirmMutation(
      String action, String id, int revision, String message, JSONObject body) {
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("确认修改流程")
            .setMessage(message + "\n请先暂停语音，等待任务和播报结束。")
            .setPositiveButton(
                "确认修改",
                (d, n) -> {
                  if (!live(owner)) return;
                  JSONObject value = mutation(action, id, revision, true);
                  if (body != null) Wire.put(value, "value", body);
                  request(value, route);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void confirmRun(JSONObject offer) {
    if (offer == null
        || !WorkflowModel.token(offer.opt("id"))
        || !Integer.valueOf(90).equals(offer.opt("expires_in_seconds"))
        || !(offer.opt("camera_required") instanceof Boolean)) {
      toast("运行确认已不可用，请重新核对。");
      return;
    }
    JSONObject flow = WorkflowModel.definition(offer.optJSONObject("definition"));
    if (flow == null) {
      toast("流程定义已不可用。");
      return;
    }
    boolean camera = Boolean.TRUE.equals(offer.opt("camera_required"));
    if (camera != "ocr".equals(flow.optJSONArray("steps").optJSONObject(0).optString("kind"))) {
      toast("取景声明不匹配。");
      return;
    }
    String id = offer.optString("id");
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle(camera ? "确认本次取景并运行" : "确认运行一次")
            .setMessage(
                WorkflowModel.describe(flow)
                    + "\n\n"
                    + (camera ? "本次将打开摄像头，显示 5 秒预览，再将一张照片交给 Dot。图片不导出保存。\n" : "")
                    + "停止等待不撤销已完成操作；失败不自动重跑。请先暂停语音，确认在 90 秒内有效。")
            .setPositiveButton(
                camera ? "确认取景并运行" : "确认运行",
                (d, n) -> {
                  if (!live(owner)) return;
                  if (camera
                      && android.os.Build.VERSION.SDK_INT >= 23
                      && activity.checkSelfPermission(android.Manifest.permission.CAMERA)
                          != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                    activity.requestPermissions(
                        new String[] {android.Manifest.permission.CAMERA}, 34);
                    return; // Permission completion is not a new run/capture authorization.
                  }
                  JSONObject value = args("run");
                  Wire.put(value, "offer_id", id);
                  Wire.put(value, "confirmed", true);
                  Wire.put(value, "camera_nonce", camera ? WorkflowModel.id() : "");
                  request(value, route);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void runDetails(JSONObject raw) {
    JSONObject run = (JSONObject) WorkflowModel.copy(raw);
    JSONArray steps = run.optJSONArray("steps");
    String[] labels = new String[steps.length()];
    for (int i = 0; i < labels.length; i++)
      labels[i] =
          steps.optJSONObject(i).optString("title")
              + " · "
              + WorkflowModel.stateName(steps.optJSONObject(i).optString("state"));
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle(
                run.optString("name") + " · " + WorkflowModel.stateName(run.optString("state")))
            .setItems(
                labels,
                (d, n) -> {
                  if (!live(owner)) return;
                  JSONObject value = args("result");
                  Wire.put(value, "run_id", run.optString("id"));
                  Wire.put(value, "step_id", steps.optJSONObject(n).optString("id"));
                  request(value, route);
                })
            .setPositiveButton(
                "返回并刷新状态",
                (d, n) -> {
                  if (live(owner)) open();
                })
            .setNeutralButton(
                "停止本次流程／朗读",
                (d, n) -> {
                  if (!live(owner)) return;
                  JSONObject value = args("cancel");
                  Wire.put(value, "run_id", run.optString("id"));
                  request(value, route);
                })
            .setNegativeButton("关闭", null)
            .create());
  }

  private void showResult(JSONObject row) {
    if (row == null
        || !WorkflowModel.token(row.opt("id"))
        || !WorkflowModel.label(row.opt("title"), 40)) {
      toast("单步结果已不可用。");
      return;
    }
    JSONObject result = row.optJSONObject("result");
    String text = "这一步" + WorkflowModel.stateName(row.optString("state")) + "。请先核对实际结果，不要重复交办。";
    if (result != null) {
      if (!"completed".equals(row.opt("state"))
          || !WorkflowModel.text(result.opt("text"), 8000)
          || result.optString("text").trim().isEmpty()
          || !(result.opt("truncated") instanceof Boolean)
          || !Boolean.FALSE.equals(result.opt("independently_verified"))
          || !(result.opt("operation_id") instanceof String)
          || !result.optString("operation_id").matches("[A-Za-z0-9._:-]{1,128}")) {
        toast("操作回执格式不兼容。");
        return;
      }
      text =
          result.optString("text")
              + (Boolean.TRUE.equals(result.opt("truncated")) ? "\n\n[收到的结果已截短]" : "")
              + "\n\n已收到操作完成回执；内容和工具结果尚未独立核验。";
    }
    final String body = text;
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle(
                row.optString("title") + " · " + WorkflowModel.stateName(row.optString("state")))
            .setMessage(body)
            .setPositiveButton(
                "复制",
                (d, n) -> {
                  if (!live(owner)) return;
                  ClipboardManager clipboard =
                      (ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE);
                  if (clipboard != null)
                    clipboard.setPrimaryClip(ClipData.newPlainText("流程单步结果", body));
                })
            .setNegativeButton("关闭", null)
            .create());
  }

  private void manage(JSONObject state) {
    StringBuilder text = new StringBuilder("仅保存领取时段及执行／摘录送达状态，不保存结果正文。显示最近 16 条，最多保留 256 条。\n");
    if (Boolean.TRUE.equals(state.opt("journal_unconfirmed")))
      text.append("\n最近一次记录写入未确认，请核对后再配置。\n");
    JSONArray rows = state.optJSONArray("attempts");
    for (int i = 0; i < rows.length(); i++) {
      JSONObject row = rows.optJSONObject(i);
      if (row == null || !WorkflowModel.stamp(row.opt("slot"))) {
        toast("定时记录格式不兼容。");
        return;
      }
      java.text.SimpleDateFormat date =
          new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.CHINA);
      date.setTimeZone(java.util.TimeZone.getTimeZone("GMT+08:00"));
      String name = "已删除的流程";
      JSONArray flows = state.optJSONArray("items");
      for (int n = 0; n < flows.length(); n++) {
        JSONObject flow = flows.optJSONObject(n);
        if (flow.optString("id").equals(row.optString("workflow_id")))
          name = flow.optString("name");
      }
      text.append("\n")
          .append(name)
          .append(" · 北京时间 ")
          .append(date.format(new java.util.Date(((Number) row.opt("slot")).longValue() * 1000)))
          .append("\n执行：")
          .append(WorkflowModel.stateName(row.optString("execution")))
          .append("；摘录送达：")
          .append(
              "pending".equals(row.opt("delivery"))
                  ? "等待播报确认"
                  : WorkflowModel.stateName(row.optString("delivery")))
          .append("\n");
    }
    final long owner = epoch + 1;
    show(
        new AlertDialog.Builder(activity)
            .setTitle("定时记录与管理")
            .setMessage(text.toString())
            .setPositiveButton(
                "恢复内置模板",
                (d, n) -> {
                  if (live(owner))
                    confirmMutation(
                        "reset",
                        null,
                        state.optInt("revision"),
                        "恢复三种内置模板，关闭定时；保留旧领取记录，不重新执行。",
                        null);
                })
            .setNeutralButton(
                "暂停全部定时并清除记录",
                (d, n) -> {
                  if (live(owner))
                    confirmMutation(
                        "clear_history",
                        null,
                        state.optInt("revision"),
                        "先禁用全部定时，再清除领取记录。重新开启从新的时段开始，不补跑旧时段。",
                        null);
                })
            .setNegativeButton("关闭", null)
            .create());
  }
}
