package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.Rect;
import android.text.Layout;
import android.widget.ScrollView;
import android.widget.TextView;

/** Hard viewport for both the base text and the custom karaoke highlight pass. */
final class CaptionViewport extends ScrollView {
    private final Rect clip=new Rect();
    CaptionViewport(Context context){super(context);setClipChildren(true);setClipToPadding(true);setOverScrollMode(OVER_SCROLL_NEVER);setVerticalFadingEdgeEnabled(false);}
    @Override protected void onSizeChanged(int w,int h,int oldw,int oldh){super.onSizeChanged(w,h,oldw,oldh);clip.set(0,0,w,h);setClipBounds(clip);}
    void revealLine(TextView text,int line){
        Layout layout=text.getLayout();if(layout==null||getHeight()<=0)return;
        int top=text.getTop()+text.getTotalPaddingTop()+layout.getLineTop(line);
        int bottom=text.getTop()+text.getTotalPaddingTop()+layout.getLineBottom(line);
        int visibleTop=getScrollY()+getPaddingTop(),visibleBottom=getScrollY()+getHeight()-getPaddingBottom();
        // Move before painting the first highlighted glyph of the new line.
        // A smooth transition would leave it partly under the viewport edge.
        if(top<visibleTop||bottom>visibleBottom)scrollTo(0,Math.max(0,top-getPaddingTop()));
    }
    boolean lineVisible(TextView text,int line){
        Layout layout=text.getLayout();if(layout==null||getHeight()==0)return false;
        int top=text.getTop()+text.getTotalPaddingTop()+layout.getLineTop(line)-getScrollY();
        int bottom=text.getTop()+text.getTotalPaddingTop()+layout.getLineBottom(line)-getScrollY();
        return top>=getPaddingTop()&&bottom<=getHeight()-getPaddingBottom();
    }
}
