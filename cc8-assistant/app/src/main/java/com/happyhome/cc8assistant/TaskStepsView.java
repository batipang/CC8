package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.drawable.Drawable;
import android.view.View;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import org.json.JSONArray;
import org.json.JSONObject;

/** A compact diagram of actual tool receipts, never fabricated task percentages. */
final class TaskStepsView extends View {
  // 只从后台真实任务回执提取步骤，保持完成/失败/取消语义，不生成示例任务进度。
  private static final int CYAN = Color.rgb(67, 224, 252),
      AMBER = Color.rgb(255, 199, 91),
      MUTED = Color.rgb(120, 156, 175);
  private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
  private final Drawable check, error;
  private final ArrayList<String[]> nodes = new ArrayList<>();

  TaskStepsView(Context context) {
    super(context);
    check = context.getDrawable(R.drawable.ic_hud_check).mutate();
    check.setTint(CYAN);
    error = context.getDrawable(R.drawable.ic_hud_error).mutate();
    error.setTint(AMBER);
    setFocusable(true);
  }

  private String category(String title) {
    if (title.contains("检索")) return "检索";
    if (title.contains("文件")) return "写入";
    if (title.contains("Mac")) return "执行";
    if (title.contains("操作") || title.contains("工具")) return "操作";
    if (title.contains("连接")) return "连接";
    return "分析";
  }

  void task(JSONObject task) {
    nodes.clear();
    LinkedHashMap<String, String> grouped = new LinkedHashMap<>();
    if (task != null) {
      JSONArray steps = task.optJSONArray("steps");
      if (steps != null)
        for (int i = 0; i < steps.length(); i++) {
          JSONObject step = steps.optJSONObject(i);
          if (step == null) continue;
          String key = category(step.optString("title")), status = step.optString("status");
          String old = grouped.get(key);
          if (old == null || !"failed".equals(old) || "running".equals(status))
            grouped.put(key, status);
        }
      for (java.util.Map.Entry<String, String> entry : grouped.entrySet())
        nodes.add(new String[] {entry.getKey(), entry.getValue()});
      String status = task.optString("status");
      if (nodes.isEmpty()) nodes.add(new String[] {"回答", status});
      else if (("failed".equals(status) || "cancelled".equals(status))
          && !status.equals(nodes.get(nodes.size() - 1)[1])) nodes.add(new String[] {"任务", status});
      while (nodes.size() > 3) nodes.remove(0);
    }
    StringBuilder description = new StringBuilder();
    for (String[] node : nodes) description.append(label(node)).append(' ');
    setContentDescription(nodes.isEmpty() ? "交办任务后显示执行进度" : description.toString());
    invalidate();
  }

  private String label(String[] node) {
    String status = node[1];
    return "completed".equals(status)
        ? node[0] + "完成"
        : "failed".equals(status)
            ? node[0] + "失败"
            : "cancelled".equals(status) ? "已停止" : "正在" + node[0];
  }

  @Override
  protected void onDraw(Canvas canvas) {
    super.onDraw(canvas);
    float d = getResources().getDisplayMetrics().density, cy = getHeight() / 2f;
    paint.setTextSize(21 * d);
    paint.setStyle(Paint.Style.FILL);
    if (nodes.isEmpty()) {
      paint.setColor(MUTED);
      canvas.drawText("交办任务后显示执行进度", 0, cy + 7 * d, paint);
      return;
    }
    float cell = getWidth() / 3f;
    for (int i = 0; i < nodes.size(); i++) {
      String[] node = nodes.get(i);
      float x = cell * i;
      boolean done = "completed".equals(node[1]);
      int color = done ? CYAN : AMBER;
      if (done || "failed".equals(node[1])) {
        Drawable icon = done ? check : error;
        icon.setBounds(
            Math.round(x),
            Math.round(cy - 16 * d),
            Math.round(x + 32 * d),
            Math.round(cy + 16 * d));
        icon.draw(canvas);
      } else {
        paint.setColor(color);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(1.4f * d);
        canvas.drawCircle(x + 16 * d, cy, 16 * d, paint);
        paint.setStyle(Paint.Style.FILL);
        canvas.drawCircle(x + 16 * d, cy, 8 * d, paint);
      }
      paint.setColor(color);
      paint.setStyle(Paint.Style.FILL);
      canvas.drawText(label(node), x + 42 * d, cy + 7 * d, paint);
      if (i < nodes.size() - 1) {
        paint.setColor(done ? CYAN : Color.rgb(37, 84, 105));
        paint.setStrokeWidth(1.2f * d);
        canvas.drawLine(x + 137 * d, cy, x + 158 * d, cy, paint);
      }
    }
  }
}
