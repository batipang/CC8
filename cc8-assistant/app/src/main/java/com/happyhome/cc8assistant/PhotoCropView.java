package com.happyhome.cc8assistant;

import android.content.Context;
import android.graphics.*;
import android.view.MotionEvent;
import android.view.View;

/** UI references only; the panel owns and recycles its bounded bitmap. */
final class PhotoCropView extends View {
  private Bitmap image;
  private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
  private boolean cropping;
  private float startX, startY;
  private CropRegion region;

  PhotoCropView(Context context) {
    super(context);
    setContentDescription("照片预览与裁剪区域");
    setClickable(true);
  }

  void image(Bitmap value) {
    image = value;
    cropping = false;
    region = null;
    invalidate();
  }

  void cropping(boolean value) {
    cropping = value;
    region = value && image != null ? CropRegion.whole(image.getWidth(), image.getHeight()) : null;
    invalidate();
  }

  boolean cropping() {
    return cropping;
  }

  CropRegion region() {
    return region;
  }

  @Override
  protected void onDraw(Canvas canvas) {
    super.onDraw(canvas);
    if (image == null || image.isRecycled() || getWidth() <= 0 || getHeight() <= 0) return;
    float scale =
        Math.min((float) getWidth() / image.getWidth(), (float) getHeight() / image.getHeight());
    float x = (getWidth() - image.getWidth() * scale) / 2,
        y = (getHeight() - image.getHeight() * scale) / 2;
    RectF full = new RectF(x, y, x + image.getWidth() * scale, y + image.getHeight() * scale);
    paint.setStyle(Paint.Style.FILL);
    paint.setAlpha(255);
    canvas.drawBitmap(image, null, full, paint);
    if (cropping && region != null) {
      RectF selected =
          new RectF(
              x + region.left * scale,
              y + region.top * scale,
              x + region.right * scale,
              y + region.bottom * scale);
      paint.setColor(0x99000000);
      canvas.drawRect(full.left, full.top, full.right, selected.top, paint);
      canvas.drawRect(full.left, selected.bottom, full.right, full.bottom, paint);
      canvas.drawRect(full.left, selected.top, selected.left, selected.bottom, paint);
      canvas.drawRect(selected.right, selected.top, full.right, selected.bottom, paint);
      paint.setStyle(Paint.Style.STROKE);
      paint.setStrokeWidth(3 * getResources().getDisplayMetrics().density);
      paint.setColor(0xffffc36a);
      canvas.drawRect(selected, paint);
      paint.setStyle(Paint.Style.FILL);
    }
  }

  @Override
  public boolean onTouchEvent(MotionEvent event) {
    if (!cropping || image == null) return super.onTouchEvent(event);
    if (event.getActionMasked() == MotionEvent.ACTION_DOWN) {
      startX = event.getX();
      startY = event.getY();
      region = null;
      invalidate();
      return true;
    }
    if (event.getActionMasked() == MotionEvent.ACTION_MOVE
        || event.getActionMasked() == MotionEvent.ACTION_UP) {
      region =
          CropRegion.fromDrag(
              getWidth(),
              getHeight(),
              image.getWidth(),
              image.getHeight(),
              startX,
              startY,
              event.getX(),
              event.getY());
      invalidate();
      if (event.getActionMasked() == MotionEvent.ACTION_UP) performClick();
      return true;
    }
    return true;
  }

  @Override
  public boolean performClick() {
    super.performClick();
    return true;
  }
}
