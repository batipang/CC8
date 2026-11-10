"""Real native caption viewport; content counts are not configured capacity."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.java_toolchain import require_java

ROOT = Path(__file__).resolve().parents[1] / 'app/src'
STUBS = {
    'android/content/Context.java': 'package android.content; public class Context {}',
    'android/graphics/Rect.java': """package android.graphics;
public class Rect {
 public int left,top,right,bottom;
 public void set(int l,int t,int r,int b){left=l;top=t;right=r;bottom=b;}
}""",
    'android/util/TypedValue.java': """package android.util;
public class TypedValue {public static final int COMPLEX_UNIT_PX=0;}""",
    'android/text/Layout.java': """package android.text;
public final class Layout {
 private final int count,height;
 public Layout(int c,int h){count=c;height=h;}
 public int getLineCount(){return count;}
 public int getLineTop(int i){if(i<0||i>count)throw new AssertionError("line top");return i*height;}
 public int getLineBottom(int i){if(i<0||i>=count)throw new AssertionError("line bottom");return (i+1)*height;}
}""",
    'android/view/View.java': """package android.view;
import android.graphics.Rect;
public class View {
 public static final int OVER_SCROLL_NEVER=2;
 private int w,h,top,bottom,scroll;
 private Rect clip;
 protected void onSizeChanged(int w,int h,int oldw,int oldh){}
 protected void onLayout(boolean changed,int l,int t,int r,int b){}
 public void size(int width,int height){int oldw=w,oldh=h;w=width;h=height;onSizeChanged(w,h,oldw,oldh);}
 public int getHeight(){return h;} public int getWidth(){return w;}
 public int getTop(){return 0;}
 public int getPaddingTop(){return top;} public int getPaddingBottom(){return bottom;}
 public void setPadding(int l,int t,int r,int b){top=t;bottom=b;}
 public void setClipBounds(Rect value){clip=value;} public Rect getClipBounds(){return clip;}
 public void scrollTo(int x,int y){scroll=y;} public int getScrollY(){return scroll;}
}""",
    'android/widget/ScrollView.java': """package android.widget;
import android.content.Context;import android.view.View;
public class ScrollView extends View {
 private View child;
 public ScrollView(Context context){}
 public void setClipChildren(boolean value){} public void setClipToPadding(boolean value){}
 public void setOverScrollMode(int value){} public void setVerticalFadingEdgeEnabled(boolean value){}
 public void addView(View value){child=value;} public View getChildAt(int index){return child;}
}""",
    'android/widget/TextView.java': """package android.widget;
import android.view.View;import android.text.Layout;
public class TextView extends View {
 private final int count,baseHeight;private float size=36;
 public boolean missing;
 public TextView(int lines,int height){count=lines;baseHeight=height;}
 public Layout getLayout(){return missing?null:new Layout(count,Math.max(1,Math.round(baseHeight*size/36)));}
 public int getTotalPaddingTop(){return 3;} public int getTotalPaddingBottom(){return 16;}
 public float getTextSize(){return size;} public void setTextSize(int units,float value){size=value;}
}""",
}


JAVA_CHECK = r"""
package com.happyhome.cc8assistant;

import android.content.Context;
import android.widget.TextView;

/** Real viewport geometry under controlled font/layout metrics, never device capture. */
public final class CaptionViewportCheck {
  private static void yes(boolean value, String reason) {
    if (!value) throw new AssertionError(reason);
  }

  private static CaptionViewport viewport(TextView text, int height) {
    CaptionViewport view = new CaptionViewport(new Context());
    view.addView(text);
    view.size(665, height);
    view.onLayout(true, 0, 0, 665, height);
    return view;
  }

  private static void counts() {
    for (int lines = 1; lines <= 8; lines++) {
      TextView text = new TextView(lines, 38);
      CaptionViewport view = viewport(text, 153);
      yes(view.lineLimit() == 3, "configured limit changed with content length");
      yes(view.layoutLines(text) == lines, "actual text line count was replaced by capacity");
      yes(view.fullVisibleLines(text) == Math.min(lines, 3), "incorrect complete visible line count");
      yes(view.getClipBounds().bottom == view.getHeight() - view.getPaddingBottom(),
          "highlight and text use different clipping windows");
    }
  }

  private static void standby() {
    TextView text = new TextView(2, 45);
    CaptionViewport view = viewport(text, 153);
    yes(view.lineLimit() == 3 && view.layoutLines(text) == 2
        && view.fullVisibleLines(text) == 2, "two-line idle prompt is not a three-line failure");
    yes(text.getTextSize() == 36, "short text unnecessarily shrank");
    yes(view.lineVisible(text, 0) && view.lineVisible(text, 1), "idle text clipped");
  }

  private static void fallback() {
    TextView text = new TextView(8, 60);
    CaptionViewport view = viewport(text, 153);
    // Android schedules a new layout after a real fallback font requires shrinking.
    for (int i = 0; i < 4; i++) view.onLayout(true, 0, 0, 665, 153);
    yes(text.getTextSize() < 36 && text.getTextSize() > 1, "fallback font not fitted");
    yes(view.fullVisibleLines(text) == 3 && view.lineVisible(text, 2)
        && !view.lineVisible(text, 3), "fallback fitting exposed a clipped fourth line");
  }

  private static void pages() {
    TextView text = new TextView(9, 38);
    CaptionViewport view = viewport(text, 153);
    for (int i = 0; i < 9; i++) {
      view.revealLine(text, i);
      yes(view.lineVisible(text, i), "current spoken line not fully visible");
      yes(view.fullVisibleLines(text) == 3, "page lost the three complete lines");
    }
    int scroll = view.getScrollY();
    view.revealLine(text, -1);
    view.revealLine(text, 9);
    yes(view.getScrollY() == scroll, "invalid line moved caption page");
  }

  private static void unavailable() {
    TextView text = new TextView(2, 38);
    CaptionViewport view = viewport(text, 0);
    yes(view.fullVisibleLines(text) == 0 && !view.lineVisible(text, 0),
        "unmeasured viewport reported visible text");
    text.missing = true;
    yes(view.layoutLines(text) == 0 && view.fullVisibleLines(text) == 0 && view.lineLimit() == 3,
        "missing layout conflated configured limit with measured evidence");
  }

  public static void main(String[] args) {
    switch (args[0]) {
      case "counts": counts(); break;
      case "standby": standby(); break;
      case "fallback": fallback(); break;
      case "pages": pages(); break;
      case "unavailable": unavailable(); break;
      default: throw new AssertionError("Unknown case");
    }
    System.out.println(args[0] + " passed");
  }
}

"""


class CaptionViewportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = require_java()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-caption-viewport-')
        cls.addClassCleanup(cls.folder.cleanup)
        folder = Path(cls.folder.name)
        files = [
            ROOT / 'main/java/com/happyhome/cc8assistant/CaptionViewport.java',
        ]
        check = folder / 'CaptionViewportCheck.java'
        check.write_text(JAVA_CHECK)
        files.append(check)
        for name, content in STUBS.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            files.append(path)
        result = subprocess.run(
            [
                str(cls.java / 'javac'),
                '-encoding',
                'UTF-8',
                '-d',
                cls.folder.name,
                *map(str, files),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr)

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                self.folder.name,
                'com.happyhome.cc8assistant.CaptionViewportCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name + ' passed')

    def test_actual_one_to_eight_line_content_is_distinct_from_three_line_limit(self):
        self.scenario('counts')

    def test_two_line_standby_prompt_is_complete_and_keeps_default_font(self):
        self.scenario('standby')

    def test_real_fallback_metrics_fit_three_lines_and_hide_fourth(self):
        self.scenario('fallback')

    def test_spoken_line_reveal_retains_complete_pages_and_rejects_invalid_offsets(self):
        self.scenario('pages')

    def test_missing_layout_or_measurement_does_not_fabricate_visible_lines(self):
        self.scenario('unavailable')


if __name__ == '__main__':
    unittest.main()
