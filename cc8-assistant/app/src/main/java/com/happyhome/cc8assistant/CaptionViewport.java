package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.Rect;
import android.text.Layout;
import android.util.TypedValue;
import android.view.View;
import android.widget.ScrollView;
import android.widget.TextView;

/** Hard three-line viewport for both base text and the karaoke highlight pass. */
final class CaptionViewport extends ScrollView {
  // 按 Layout 实测行高裁出三行，不用固定字号猜高度，避免第四行残片及进度条遮挡。
  private static final int LINES = 3;
  private final Rect clip = new Rect();

  CaptionViewport(Context context) {
    super(context);
    setClipChildren(true);
    setClipToPadding(true);
    setOverScrollMode(OVER_SCROLL_NEVER);
    setVerticalFadingEdgeEnabled(false);
  }

  @Override
  protected void onSizeChanged(int w, int h, int oldw, int oldh) {
    super.onSizeChanged(w, h, oldw, oldh);
    clip.set(0, 0, w, h);
    setClipBounds(clip);
  }

  @Override
  protected void onLayout(boolean changed, int l, int t, int r, int b) {
    super.onLayout(changed, l, t, r, b);
    View child = getChildAt(0);
    if (!(child instanceof TextView)) return;
    TextView text = (TextView) child;
    Layout layout = text.getLayout();
    if (layout == null || getHeight() <= 0) return;
    int count = layout.getLineCount();
    int window = text.getTotalPaddingTop() + layout.getLineTop(Math.min(LINES, count));
    // Account for the real fallback-font metrics rather than assuming a fixed CJK line height.
    if (count >= LINES && window > getHeight()) {
      text.setTextSize(
          TypedValue.COMPLEX_UNIT_PX, Math.max(1, text.getTextSize() * getHeight() / window - .5f));
      return;
    }
    if (count <= LINES) window += text.getTotalPaddingBottom();
    window = Math.min(getHeight(), window);
    int bottom = getHeight() - window;
    if (getPaddingBottom() != bottom) setPadding(0, 0, 0, bottom);
    clip.set(0, 0, getWidth(), window);
    setClipBounds(clip);
  }

  void revealLine(TextView text, int line) {
    Layout layout = text.getLayout();
    if (layout == null || getHeight() <= 0 || line < 0 || line >= layout.getLineCount()) return;
    if (!lineVisible(text, line)) {
      int first = Math.min(line, Math.max(0, layout.getLineCount() - LINES));
      // Align complete lines; retain a full final page instead of clipping earlier context.
      scrollTo(0, layout.getLineTop(first));
    }
  }

  boolean lineVisible(TextView text, int line) {
    Layout layout = text.getLayout();
    if (layout == null || getHeight() == 0 || line < 0 || line >= layout.getLineCount())
      return false;
    int top = text.getTop() + text.getTotalPaddingTop() + layout.getLineTop(line) - getScrollY();
    int bottom =
        text.getTop() + text.getTotalPaddingTop() + layout.getLineBottom(line) - getScrollY();
    return top >= getPaddingTop() && bottom <= getHeight() - getPaddingBottom();
  }

  int fullVisibleLines(TextView text) {
    Layout layout = text.getLayout();
    if (layout == null) return 0;
    int count = 0;
    for (int i = 0; i < layout.getLineCount(); i++) if (lineVisible(text, i)) count++;
    return count;
  }

  int lineLimit() {
    return LINES;
  }

  int layoutLines(TextView text) {
    Layout layout = text.getLayout();
    return layout == null ? 0 : layout.getLineCount();
  }
}
