package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.view.View;
import java.util.Arrays;
import java.util.Random;

/** Voice-driven particle field, separate from the approved portrait and subtitles. */
final class WaveformView extends View {
  // 独立于头像布局；实际播放 RMS 控制能量，暂停冻结运动，停止时回落为低亮度。
  private static final int COUNT = 3200, BINS = 4;
  private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
  private final Typeface mono = Typeface.create("monospace", Typeface.NORMAL);
  private final float[] u = new float[COUNT], depth = new float[COUNT], jitter = new float[COUNT];
  private final float[] sinA = new float[COUNT],
      cosA = new float[COUNT],
      sinB = new float[COUNT],
      cosB = new float[COUNT];
  private final float[] ridgePoints = new float[320 * 2],
      ridgeSinA = new float[320],
      ridgeCosA = new float[320],
      ridgeSinB = new float[320],
      ridgeCosB = new float[320];
  private final float[][] points = new float[BINS][COUNT * 2];
  private final int[] counts = new int[BINS];
  private final int[] colors = {
    Color.rgb(25, 108, 171),
    Color.rgb(37, 166, 220),
    Color.rgb(70, 222, 255),
    Color.rgb(190, 249, 255)
  };
  private float energy, phase;
  private boolean playing, paused;

  WaveformView(Context context) {
    super(context);
    setContentDescription("青蓝粒子声场，起伏随播报音量变化");
    for (int i = 0; i < 320; i++) {
      double x = i / 319.0, a = x * Math.PI * 4.3, b = x * Math.PI * 8.1;
      ridgeSinA[i] = (float) Math.sin(a);
      ridgeCosA[i] = (float) Math.cos(a);
      ridgeSinB[i] = (float) Math.sin(b);
      ridgeCosB[i] = (float) Math.cos(b);
    }
    Random random = new Random(8192);
    for (int i = 0; i < COUNT; i++) {
      u[i] = (i % 112 + random.nextFloat() * .18f) / 112;
      depth[i] =
          (float) Math.max(-3.2, Math.min(3.2, random.nextGaussian() * (i % 5 == 0 ? 1.8 : 1)));
      jitter[i] = random.nextFloat() * 2 - 1;
      double a = u[i] * Math.PI * 4.3, b = u[i] * Math.PI * 8.1;
      sinA[i] = (float) Math.sin(a);
      cosA[i] = (float) Math.cos(a);
      sinB[i] = (float) Math.sin(b);
      cosB[i] = (float) Math.cos(b);
    }
  }

  void update(float level, boolean playing, boolean paused) {
    // Freeze the cloud during a tentative pause; silence never fabricates energy.
    if (!paused) {
      float target = playing ? (float) Math.pow(Math.min(1, Math.max(0, level) * 40), .45) : 0;
      energy += (target - energy) * (target > energy ? .6f : .055f);
      if (!playing) energy = 0;
      if (playing && target > .015f) phase += .018f + .035f * target;
    }
    this.playing = playing;
    this.paused = paused;
    invalidate();
  }

  @Override
  protected void onDraw(Canvas canvas) {
    float w = getWidth(), h = getHeight(), d = getResources().getDisplayMetrics().density;
    // The product background remains visible: no card, graph grid or framing.
    Arrays.fill(counts, 0);
    float sine = (float) Math.sin(phase),
        cosine = (float) Math.cos(phase),
        sineB = (float) Math.sin(phase * .67f),
        cosineB = (float) Math.cos(phase * .67f);
    float center = h * .5f, available = h * .42f;
    for (int i = 0; i < COUNT; i++) {
      float x = u[i];
      float edge = (float) Math.sin(Math.PI * x);
      edge = (float) Math.sqrt(Math.max(0, edge));
      float ridge =
          (sinA[i] * cosine - cosA[i] * sine) * .62f + (sinB[i] * cosineB + cosB[i] * sineB) * .16f;
      float height = available * (ridge * energy * .88f + depth[i] * (.15f + .35f * energy)) * edge;
      float y = center + height + jitter[i] * (.7f + energy) * d;
      float distance = Math.abs(depth[i]);
      int bin = distance > 1.55f ? 0 : distance > .95f ? 1 : distance > .28f ? 2 : 3;
      if (i % 37 == 0) bin = 3;
      // Dim outer particles and sparse edge specks create depth; the core
      // stays sharp. Fixed seeds avoid random flashing when audio pauses.
      if (!playing && !paused && (i % 5 != 0)) continue;
      if (y < 3 * d || y > h - 3 * d) continue;
      int index = counts[bin];
      points[bin][index] = (4 * d + x * (w - 8 * d));
      points[bin][index + 1] = y;
      counts[bin] += 2;
    }
    canvas.save();
    canvas.clipRect(0, 0, w, h);
    p.setStrokeCap(Paint.Cap.ROUND);
    p.setStyle(Paint.Style.STROKE);
    // Batched points avoid per-frame allocations and thousands of draw calls.
    if (playing || paused) {
      p.setColor(Color.rgb(42, 191, 255));
      p.setAlpha(12 + Math.round(12 * energy));
      p.setStrokeWidth(3.4f * d);
      canvas.drawPoints(points[3], 0, counts[3], p);
    }
    for (int bin = 0; bin < BINS; bin++) {
      p.setColor(colors[bin]);
      p.setAlpha(
          playing || paused
              ? (int)
                  ((bin == 0 ? 105 : bin == 1 ? 160 : bin == 2 ? 215 : 240) * (.8f + .2f * energy))
              : 32);
      p.setStrokeWidth((bin == 0 ? .85f : bin == 1 ? 1.15f : bin == 2 ? 1.4f : 1.65f) * d);
      canvas.drawPoints(points[bin], 0, counts[bin], p);
    }
    if ((playing || paused) && energy > .015f) {
      for (int i = 0; i < 320; i++) {
        float x = i / 319f, edge = (float) Math.sqrt(Math.max(0, Math.sin(Math.PI * x)));
        float ridge =
            (ridgeSinA[i] * cosine - ridgeCosA[i] * sine) * .62f
                + (ridgeSinB[i] * cosineB + ridgeCosB[i] * sineB) * .16f;
        ridgePoints[i * 2] = 4 * d + x * (w - 8 * d);
        ridgePoints[i * 2 + 1] = center + available * ridge * energy * .88f * edge;
      }
      p.setColor(Color.rgb(67, 224, 252));
      p.setAlpha(paused ? 14 : 35);
      p.setStrokeWidth(6 * d);
      canvas.drawPoints(ridgePoints, p);
      p.setColor(Color.rgb(172, 248, 255));
      p.setAlpha(paused ? 110 : 245);
      p.setStrokeWidth(2.1f * d);
      canvas.drawPoints(ridgePoints, p);
    }
    canvas.restore();
    p.setAlpha(255);
  }
}
