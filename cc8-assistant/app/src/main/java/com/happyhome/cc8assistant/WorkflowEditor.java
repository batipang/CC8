package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.TimePickerDialog;
import android.text.InputFilter;
import android.view.Window;
import android.view.WindowManager;
import android.widget.ArrayAdapter;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;
import java.util.Calendar;
import java.util.TimeZone;
import org.json.JSONArray;
import org.json.JSONObject;

/** Local draft editor. Only its final explicit Save sends a private configuration write. */
final class WorkflowEditor {
  interface Save {
    void submit(JSONObject value, int revision, long route);
  }

  private final Activity activity;
  private final VoiceSession session;
  private final Save save;
  private AlertDialog dialog;
  private long epoch, route;

  WorkflowEditor(Activity activity, VoiceSession session, Save save) {
    this.activity = activity;
    this.session = session;
    this.save = save;
  }

  void close() {
    epoch++;
    AlertDialog old = dialog;
    dialog = null;
    if (old != null) old.dismiss();
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

  private boolean live(long owner) {
    return epoch == owner
        && dialog != null
        && session.currentRoute(route)
        && session.supportsWorkflows();
  }

  private void toast(String text) {
    Toast.makeText(activity, text, Toast.LENGTH_LONG).show();
  }

  void open(JSONObject value, int revision, long ownerRoute) {
    close();
    route = ownerRoute;
    if (!session.currentRoute(route)) return;
    JSONObject draft = (JSONObject) WorkflowModel.copy(value);
    Wire.put(draft, "schedule", JSONObject.NULL);
    edit(draft, revision);
  }

  private void edit(JSONObject flow, int revision) {
    JSONArray rows = flow.optJSONArray("steps");
    String[] labels = new String[rows.length() + 2];
    for (int i = 0; i < rows.length(); i++)
      labels[i] = (i + 1) + ". " + rows.optJSONObject(i).optString("title");
    labels[rows.length()] = "添加步骤（最多 4 步）";
    labels[rows.length() + 1] = "修改流程名称";
    final long owner = epoch + 1;
    show(
        new LargeDialogBuilder(activity)
            .setTitle("编辑：" + flow.optString("name") + " · 原定时将关闭")
            .setItems(
                labels,
                (d, n) -> {
                  if (!live(owner)) return;
                  if (n < rows.length()) stepMenu(flow, revision, n);
                  else if (n == rows.length()) add(flow, revision);
                  else rename(flow, revision);
                })
            .setPositiveButton(
                "核对并保存",
                (d, n) -> {
                  if (!live(owner)) return;
                  if (WorkflowModel.definition(flow) == null) {
                    toast("请填写名称，并添加至少一个完整步骤。");
                    edit(flow, revision);
                    return;
                  }
                  confirm(flow, revision);
                })
            .setNegativeButton("放弃草稿", null)
            .create());
  }

  private EditText input(String text, String hint, int maximum) {
    EditText field = new EditText(activity);
    field.setText(text);
    field.setHint(hint);
    field.setTextSize(26);
    field.setFilters(new InputFilter[] {new InputFilter.LengthFilter(maximum)});
    return field;
  }

  private void keyboard() {
    Window window = dialog.getWindow();
    if (window != null) {
      window.clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
      window.setSoftInputMode(
          WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
              | WindowManager.LayoutParams.SOFT_INPUT_STATE_HIDDEN);
    }
  }

  private void rename(JSONObject flow, int revision) {
    EditText name = input(flow.optString("name"), "流程名称，最多 40 字", 40);
    final long owner = epoch + 1;
    AlertDialog shown =
        new LargeDialogBuilder(activity)
            .setTitle("流程名称")
            .setView(name)
            .setPositiveButton("应用到草稿", null)
            .setNegativeButton(
                "取消",
                (d, n) -> {
                  if (live(owner)) edit(flow, revision);
                })
            .create();
    show(shown);
    keyboard();
    shown
        .getButton(AlertDialog.BUTTON_POSITIVE)
        .setOnClickListener(
            v -> {
              if (!live(owner)) return;
              String text = name.getText().toString().trim();
              if (!WorkflowModel.label(text, 40)) {
                toast("请填写有效的流程名称。");
                return;
              }
              Wire.put(flow, "name", text);
              edit(flow, revision);
            });
  }

  private void add(JSONObject flow, int revision) {
    JSONArray rows = flow.optJSONArray("steps");
    if (rows.length() >= 4) {
      toast("一个流程最多 4 步。");
      edit(flow, revision);
      return;
    }
    String[] kinds = {"assistant", "web_search", "work_report", "ocr"};
    String[] labels = new String[kinds.length];
    for (int i = 0; i < kinds.length; i++) labels[i] = WorkflowModel.kindName(kinds[i]);
    final long owner = epoch + 1;
    show(
        new LargeDialogBuilder(activity)
            .setTitle("添加步骤")
            .setItems(
                labels,
                (d, n) -> {
                  if (!live(owner)) return;
                  if ("ocr".equals(kinds[n]) && rows.length() != 0) {
                    toast("取景识字只能作为第一步；可先调整已有步骤。");
                    edit(flow, revision);
                    return;
                  }
                  JSONObject step = new JSONObject();
                  Wire.put(step, "id", WorkflowModel.id());
                  Wire.put(step, "kind", kinds[n]);
                  Wire.put(step, "title", labels[n]);
                  if ("work_report".equals(kinds[n])) {
                    Wire.put(step, "period", "two_hours");
                    Wire.put(step, "source", JSONObject.NULL);
                  } else Wire.put(step, "text", "");
                  stepFields(flow, revision, -1, step);
                })
            .setNegativeButton(
                "返回",
                (d, n) -> {
                  if (live(owner)) edit(flow, revision);
                })
            .create());
  }

  private void stepMenu(JSONObject flow, int revision, int index) {
    final long owner = epoch + 1;
    show(
        new LargeDialogBuilder(activity)
            .setTitle(flow.optJSONArray("steps").optJSONObject(index).optString("title"))
            .setItems(
                new String[] {"编辑标题和输入", "上移一步", "下移一步", "删除这一步"},
                (d, n) -> {
                  if (!live(owner)) return;
                  JSONArray rows = flow.optJSONArray("steps");
                  if (n == 0) {
                    stepFields(
                        flow,
                        revision,
                        index,
                        (JSONObject) WorkflowModel.copy(rows.optJSONObject(index)));
                    return;
                  }
                  int target = n == 1 ? index - 1 : index + 1;
                  JSONArray changed = new JSONArray();
                  if (n == 3) {
                    for (int i = 0; i < rows.length(); i++)
                      if (i != index) changed.put(rows.opt(i));
                  } else {
                    if (target < 0 || target >= rows.length()) {
                      edit(flow, revision);
                      return;
                    }
                    for (int i = 0; i < rows.length(); i++)
                      changed.put(rows.opt(i == index ? target : i == target ? index : i));
                  }
                  for (int i = 1; i < changed.length(); i++)
                    if ("ocr".equals(changed.optJSONObject(i).optString("kind"))) {
                      toast("取景识字必须保留在第一步。");
                      edit(flow, revision);
                      return;
                    }
                  Wire.put(flow, "steps", changed);
                  edit(flow, revision);
                })
            .setNegativeButton(
                "返回",
                (d, n) -> {
                  if (live(owner)) edit(flow, revision);
                })
            .create());
  }

  private void stepFields(JSONObject flow, int revision, int index, JSONObject step) {
    LinearLayout box = new LinearLayout(activity);
    box.setOrientation(LinearLayout.VERTICAL);
    box.setPadding(24, 12, 24, 12);
    EditText title = input(step.optString("title"), "步骤标题，最多 40 字", 40);
    box.addView(title);
    boolean report = "work_report".equals(step.optString("kind"));
    EditText prompt = input(step.optString("text"), "这一步要做什么，最多 500 字", 500);
    Spinner period = new Spinner(activity);
    String[] periods = {"half_hour", "two_hours", "today"};
    if (report) {
      period.setAdapter(
          new ArrayAdapter<String>(
              activity, R.layout.dialog_spinner_item, new String[] {"过去半小时", "过去两小时", "今天"}));
      for (int i = 0; i < periods.length; i++)
        if (periods[i].equals(step.optString("period"))) period.setSelection(i);
      box.addView(period);
      TextView source = new TextView(activity);
      source.setTextSize(26);
      JSONObject metadata = step.optJSONObject("source");
      source.setText(
          metadata == null
              ? "尚未绑定来源，手动运行前会核对当前选定来源。"
              : "已绑定：" + metadata.optString("source_title"));
      box.addView(source);
    } else {
      prompt.setMinLines(3);
      box.addView(prompt);
    }
    ScrollView scroll = new ScrollView(activity);
    scroll.addView(box);
    final long owner = epoch + 1;
    AlertDialog shown =
        new LargeDialogBuilder(activity)
            .setTitle(WorkflowModel.kindName(step.optString("kind")))
            .setView(scroll)
            .setPositiveButton("应用到草稿", null)
            .setNegativeButton(
                "取消",
                (d, n) -> {
                  if (live(owner)) edit(flow, revision);
                })
            .create();
    show(shown);
    keyboard();
    shown
        .getButton(AlertDialog.BUTTON_POSITIVE)
        .setOnClickListener(
            v -> {
              if (!live(owner)) return;
              String name = title.getText().toString().trim(),
                  text = prompt.getText().toString().trim();
              if (!WorkflowModel.label(name, 40) || !report && !WorkflowModel.label(text, 500)) {
                toast("请填写完整、有效的标题和输入。");
                return;
              }
              Wire.put(step, "title", name);
              if (report) Wire.put(step, "period", periods[period.getSelectedItemPosition()]);
              else Wire.put(step, "text", text);
              JSONArray rows = flow.optJSONArray("steps"), changed = new JSONArray();
              for (int i = 0; i < rows.length(); i++) changed.put(i == index ? step : rows.opt(i));
              if (index < 0) changed.put(step);
              Wire.put(flow, "steps", changed);
              edit(flow, revision);
            });
  }

  private void confirm(JSONObject flow, int revision) {
    final long owner = epoch + 1;
    show(
        new LargeDialogBuilder(activity)
            .setTitle("确认保存到当前 Mac")
            .setMessage(WorkflowModel.describe(flow) + "\n\n保存配置不运行流程；定时需要另外明确开启。")
            .setPositiveButton(
                "确认保存",
                (d, n) -> {
                  if (live(owner))
                    save.submit((JSONObject) WorkflowModel.copy(flow), revision, route);
                })
            .setNegativeButton(
                "返回编辑",
                (d, n) -> {
                  if (live(owner)) edit(flow, revision);
                })
            .create());
  }

  void schedules(JSONObject value, int revision, long ownerRoute) {
    close();
    route = ownerRoute;
    if (!session.currentRoute(route)) return;
    JSONObject flow = (JSONObject) WorkflowModel.copy(value);
    final long owner = epoch + 1;
    show(
        new LargeDialogBuilder(activity)
            .setTitle("定时运行 · 北京时间")
            .setItems(
                new String[] {"关闭定时", "每 5 分钟", "每 15 分钟", "每 30 分钟", "选择钟点和星期"},
                (d, n) -> {
                  if (!live(owner)) return;
                  Wire.put(flow, "schedule", JSONObject.NULL);
                  if (n == 0) {
                    confirm(flow, revision);
                    return;
                  }
                  JSONObject schedule = new JSONObject();
                  Wire.put(schedule, "kind", "interval");
                  Wire.put(schedule, "enabled", true);
                  Wire.put(schedule, "configured_at", 0);
                  Wire.put(schedule, "seconds", n == 1 ? 300 : n == 2 ? 900 : 1800);
                  Wire.put(flow, "schedule", schedule);
                  if (WorkflowModel.definition(flow) == null) {
                    toast("定时只支持只读检索和已绑定来源的汇报，请先绑定来源。");
                    return;
                  }
                  if (n < 4) scheduledConfirm(flow, revision);
                  else chooseTime(flow, revision);
                })
            .setNegativeButton("取消", null)
            .create());
  }

  private void chooseTime(JSONObject flow, int revision) {
    Calendar now = Calendar.getInstance(TimeZone.getTimeZone("GMT+08:00"));
    final long owner = epoch + 1;
    show(
        new TimePickerDialog(
            activity,
            (view, hour, minute) -> {
              if (!live(owner)) return;
              JSONObject schedule = new JSONObject();
              Wire.put(schedule, "kind", "daily");
              Wire.put(schedule, "enabled", true);
              Wire.put(schedule, "configured_at", 0);
              Wire.put(schedule, "minute_of_day", hour * 60 + minute);
              boolean[] selected = {true, true, true, true, true, true, true};
              final long daysOwner = epoch + 1;
              show(
                  new LargeDialogBuilder(activity)
                      .setTitle("选择星期 · 北京时间")
                      .setMultiChoiceItems(
                          new String[] {"周一", "周二", "周三", "周四", "周五", "周六", "周日"},
                          selected,
                          (d, n, on) -> {
                            if (live(daysOwner)) selected[n] = on;
                          })
                      .setPositiveButton(
                          "核对定时",
                          (d, n) -> {
                            if (!live(daysOwner)) return;
                            JSONArray days = new JSONArray();
                            for (int i = 0; i < 7; i++) if (selected[i]) days.put(i);
                            if (days.length() == 0) {
                              toast("至少选择一个星期。");
                              return;
                            }
                            Wire.put(schedule, "weekdays", days);
                            Wire.put(flow, "schedule", schedule);
                            scheduledConfirm(flow, revision);
                          })
                      .setNegativeButton("取消", null)
                      .create());
            },
            now.get(Calendar.HOUR_OF_DAY),
            now.get(Calendar.MINUTE),
            true));
  }

  private void scheduledConfirm(JSONObject flow, int revision) {
    if (WorkflowModel.definition(flow) == null) {
      toast("流程不符合只读定时要求。");
      return;
    }
    JSONObject schedule = flow.optJSONObject("schedule");
    String[] names = {"周一", "周二", "周三", "周四", "周五", "周六", "周日"};
    StringBuilder days = new StringBuilder();
    JSONArray selected = schedule.optJSONArray("weekdays");
    if (selected != null)
      for (int i = 0; i < selected.length(); i++) {
        if (i > 0) days.append("、");
        days.append(names[(Integer) selected.opt(i)]);
      }
    String when =
        "interval".equals(schedule.optString("kind"))
            ? "每 " + (schedule.optInt("seconds") / 60) + " 分钟"
            : String.format(
                    java.util.Locale.ROOT,
                    "北京时间 %02d:%02d",
                    schedule.optInt("minute_of_day") / 60,
                    schedule.optInt("minute_of_day") % 60)
                + "，"
                + days;
    final long owner = epoch + 1;
    show(
        new LargeDialogBuilder(activity)
            .setTitle("确认开启只读定时")
            .setMessage(
                when
                    + "\n\n"
                    + WorkflowModel.describe(flow)
                    + "\n\n仅前台连接、空闲且非安静时段运行；不补跑历史时段。停止、换线或维护会暂停当次许可，重连不会自行恢复。")
            .setPositiveButton(
                "确认开启",
                (d, n) -> {
                  if (live(owner))
                    save.submit((JSONObject) WorkflowModel.copy(flow), revision, route);
                })
            .setNegativeButton("取消", null)
            .create());
  }
}
