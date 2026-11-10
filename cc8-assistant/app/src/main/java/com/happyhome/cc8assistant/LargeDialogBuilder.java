package com.happyhome.cc8assistant;

import android.app.AlertDialog;
import android.content.Context;
import android.view.View;
import android.widget.TextView;

/** Applies body typography after Android has inflated its fixed-size message layout. */
final class LargeDialogBuilder extends AlertDialog.Builder {
  LargeDialogBuilder(Context context) {
    super(context);
  }

  @Override
  public AlertDialog create() {
    AlertDialog dialog = super.create();
    dialog
        .getWindow()
        .getDecorView()
        .addOnAttachStateChangeListener(
            new View.OnAttachStateChangeListener() {
              @Override
              public void onViewAttachedToWindow(View view) {
                TextView message = dialog.findViewById(android.R.id.message);
                if (message != null) message.setTextSize(26);
                view.removeOnAttachStateChangeListener(this);
              }

              @Override
              public void onViewDetachedFromWindow(View view) {}
            });
    return dialog;
  }
}
