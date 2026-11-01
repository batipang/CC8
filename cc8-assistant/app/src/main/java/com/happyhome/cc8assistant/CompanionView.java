package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import android.os.SystemClock;
import android.view.View;
import java.util.Calendar;
import java.util.Locale;

/** Local-time orbit clock: bold yellow hour/minute markers and a live second sweep around the portrait. */
final class CompanionView extends View {
  // 光环表示系统本地时间：长短黄线为分/时，秒点在 00 秒位于顶部，顺时针一圈一分钟。
  private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
  private final RectF oval = new RectF();
  private final BumblebeeAvatar avatar;
  private final float[] ticks = new float[60 * 4];
  private final DashPathEffect dots = new DashPathEffect(new float[] {1.7f, 7}, 0);
  private final Shader halo =
      new RadialGradient(
          300,
          300,
          320,
          new int[] {Color.rgb(8, 44, 63), Color.TRANSPARENT},
          null,
          Shader.TileMode.CLAMP);
  private final Shader secondsTail =
      new SweepGradient(
          300,
          300,
          new int[] {Color.TRANSPARENT, CYAN, Color.TRANSPARENT},
          new float[] {0, .033333f, .033334f});
  private float playback;
  private boolean linked;
  private String mood = "friendly";
  private long clockSecond = Long.MIN_VALUE;
  private int hour, minute, second;
  private String clockLabel = "", clockZone = "";
  private static final int CYAN = Color.rgb(67, 224, 252),
      BLUE = Color.rgb(61, 116, 153),
      AMBER = Color.rgb(255, 199, 91);

  CompanionView(Context context) {
    super(context);
    avatar = new BumblebeeAvatar(context);
    for (int i = 0; i < 60; i++) {
      double a = (i * 6 - 90) * Math.PI / 180;
      float inner = i % 5 == 0 ? 268 : 279;
      int j = i * 4;
      ticks[j] = 300 + (float) Math.cos(a) * inner;
      ticks[j + 1] = 300 + (float) Math.sin(a) * inner;
      ticks[j + 2] = 300 + (float) Math.cos(a) * 289;
      ticks[j + 3] = 300 + (float) Math.sin(a) * 289;
    }
    refreshClock(System.currentTimeMillis());
  }

  void setTransport(String value) {}

  void update(
      float playback, float input, String mood, String phase, boolean linked, boolean active) {
    this.playback = playback;
    this.mood = mood;
    this.linked = linked;
    invalidate();
  }

  private void refreshClock(long millis) {
    if (clockSecond == millis / 1000) return;
    clockSecond = millis / 1000;
    Calendar local = Calendar.getInstance();
    local.setTimeInMillis(millis);
    hour = local.get(Calendar.HOUR_OF_DAY);
    minute = local.get(Calendar.MINUTE);
    second = local.get(Calendar.SECOND);
    clockZone = local.getTimeZone().getID();
    String label = String.format(Locale.US, "%02d:%02d", hour, minute);
    if (!label.equals(clockLabel)) {
      clockLabel = label;
      setContentDescription("大黄蜂头像，当前时间 " + clockLabel);
    }
  }

  String clockTime() {
    refreshClock(System.currentTimeMillis());
    return clockLabel;
  }

  String clockTimeZone() {
    refreshClock(System.currentTimeMillis());
    return clockZone;
  }

  private void stroke(int color, float width, int alpha) {
    p.setShader(null);
    p.setStyle(Paint.Style.STROKE);
    p.setColor(color);
    p.setAlpha(alpha);
    p.setStrokeWidth(width);
    p.setStrokeCap(Paint.Cap.BUTT);
    p.setPathEffect(null);
  }

  private void ring(
      Canvas c, float radius, float start, float sweep, int color, float width, int alpha) {
    oval.set(300 - radius, 300 - radius, 300 + radius, 300 + radius);
    if (width >= 6) {
      stroke(color, width + 21, 9);
      c.drawArc(oval, start, sweep, false, p);
      stroke(color, width + 10, 25);
      c.drawArc(oval, start, sweep, false, p);
    }
    stroke(color, width, alpha);
    c.drawArc(oval, start, sweep, false, p);
  }

  private void secondPoint(Canvas c, float degrees) {
    double angle = (degrees - 90) * Math.PI / 180;
    float x = 300 + (float) Math.cos(angle) * 296, y = 300 + (float) Math.sin(angle) * 296;
    p.setStyle(Paint.Style.FILL);
    p.setColor(CYAN);
    p.setAlpha(35);
    c.drawCircle(x, y, 7, p);
    p.setAlpha(255);
    c.drawCircle(x, y, 3.3f, p);
    p.setColor(Color.rgb(217, 250, 255));
    c.drawCircle(x, y, 1.6f, p);
  }

  private void clockMarker(Canvas c, float degrees, float inner, float width) {
    double angle = (degrees - 90) * Math.PI / 180;
    float x = (float) Math.cos(angle), y = (float) Math.sin(angle);
    // Radial markers stay in the orbit annulus so neither crosses the portrait.
    stroke(AMBER, width + 10, 24);
    p.setStrokeCap(Paint.Cap.ROUND);
    c.drawLine(300 + x * inner, 300 + y * inner, 300 + x * 287, 300 + y * 287, p);
    stroke(AMBER, width, 255);
    p.setStrokeCap(Paint.Cap.ROUND);
    c.drawLine(300 + x * inner, 300 + y * inner, 300 + x * 287, 300 + y * 287, p);
  }

  @Override
  protected void onDraw(Canvas c) {
    super.onDraw(c);
    float size = Math.min(getWidth(), getHeight());
    c.save();
    c.translate((getWidth() - size) / 2, (getHeight() - size) / 2);
    c.scale(size / 600, size / 600);
    long wall = System.currentTimeMillis();
    refreshClock(wall);
    float seconds = second + (wall % 1000) / 1000f;
    float minuteDegrees = (minute + seconds / 60) * 6,
        hourDegrees = ((hour % 12) + minute / 60f + seconds / 3600) * 30;
    p.setStyle(Paint.Style.FILL);
    p.setAlpha(255);
    p.setShader(halo);
    c.drawCircle(300, 300, 320, p);
    p.setShader(null);
    ring(c, 320, 0, 360, BLUE, 1, 30);
    ring(c, 296, 0, 360, BLUE, 2, 230);
    stroke(CYAN, 1.3f, linked ? 225 : 100);
    c.drawLines(ticks, p);
    // Short, heavier hour marker and long minute marker share the same yellow.
    clockMarker(c, hourDegrees, 263, 12);
    clockMarker(c, minuteDegrees, 239, 8);
    // One continuous short arc replaces the stepped comet tail; its leading point is real seconds.
    c.save();
    c.rotate(seconds * 6 - 102, 300, 300);
    stroke(CYAN, 4, 255);
    p.setStrokeCap(Paint.Cap.ROUND);
    p.setShader(secondsTail);
    oval.set(4, 4, 596, 596);
    c.drawArc(oval, 0, 12, false, p);
    p.setShader(null);
    c.restore();
    secondPoint(c, seconds * 6);
    ring(c, 232, 0, 360, BLUE, 1.2f, 180);
    stroke(CYAN, 1.6f, 170);
    p.setStrokeCap(Paint.Cap.ROUND);
    p.setPathEffect(dots);
    oval.set(56, 56, 544, 544);
    c.drawOval(oval, p);
    p.setPathEffect(null);
    c.save();
    c.translate(70, 89);
    c.scale(430f / 1254, 400f / 1254);
    avatar.draw(c, playback, mood, SystemClock.uptimeMillis());
    c.restore();
    c.restore();
  }
}
