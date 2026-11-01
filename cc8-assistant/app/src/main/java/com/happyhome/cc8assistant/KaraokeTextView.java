package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import android.text.Layout;
import android.widget.TextView;

/** Paint a clipped highlight without relaying out the text on every audio frame. */
final class KaraokeTextView extends TextView {
  // 复用同一份文字布局，仅裁切高亮绘制范围；逐字推进不应引发每帧重新排版。
  private final Path path = new Path();
  private final RectF bounds = new RectF();
  private float position;
  private boolean timed;

  KaraokeTextView(Context context) {
    super(context);
  }

  void progress(float value, boolean enabled) {
    if (position != value || timed != enabled) {
      position = value;
      if (timed != enabled)
        setTextColor(enabled ? Color.rgb(104, 143, 173) : Color.rgb(217, 240, 249));
      timed = enabled;
      invalidate();
    }
  }

  @Override
  protected void onDraw(Canvas canvas) {
    int original = getCurrentTextColor();
    getPaint().setColor(timed ? Color.rgb(104, 143, 173) : original);
    super.onDraw(canvas);
    Layout layout = getLayout();
    if (!timed || layout == null || position <= 0) return;
    String text = getText().toString();
    int end = Math.min(text.length(), (int) position);
    if (end > 0 && end < text.length() && Character.isLowSurrogate(text.charAt(end))) end--;
    canvas.save();
    canvas.translate(getTotalPaddingLeft(), getTotalPaddingTop());
    path.reset();
    if (end > 0) layout.getSelectionPath(0, end, path);
    if (end < text.length()) {
      int next = end + Character.charCount(text.codePointAt(end));
      Path glyph = new Path();
      layout.getSelectionPath(end, next, glyph);
      glyph.computeBounds(bounds, true);
      float fraction = Math.min(1, (position - end) / (next - end));
      bounds.right = bounds.left + bounds.width() * fraction;
      Path crop = new Path();
      crop.addRect(bounds, Path.Direction.CW);
      glyph.op(crop, Path.Op.INTERSECT);
      path.addPath(glyph);
    }
    canvas.clipPath(path);
    getPaint().setColor(Color.rgb(255, 199, 91));
    layout.draw(canvas);
    canvas.restore();
    getPaint().setColor(original);
  }
}
