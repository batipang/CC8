package com.happyhome.cc8assistant;

import android.app.AlertDialog;
import android.content.Context;
import android.view.View;
import android.widget.TextView;

/** Tracks dialogs by activity and applies the current identity and body typography. */
final class LargeDialogBuilder extends AlertDialog.Builder {
  interface OwnerAccess {
    long dialogEpoch();

    boolean ownerDialogAllowed(long epoch);
  }

  private static final java.util.WeakHashMap<
          Context, java.util.ArrayList<java.lang.ref.WeakReference<AlertDialog>>>
      dialogs = new java.util.WeakHashMap<>();
  private final Context ownerContext;
  private final boolean publicDialog;
  private final long ownerEpoch;

  LargeDialogBuilder(Context context, boolean publicDialog) {
    super(context);
    this.ownerContext = context;
    this.publicDialog = publicDialog;
    this.ownerEpoch = context instanceof OwnerAccess ? ((OwnerAccess) context).dialogEpoch() : -1;
  }

  private boolean allowed() {
    return publicDialog
        || !(ownerContext instanceof OwnerAccess)
        || ((OwnerAccess) ownerContext).ownerDialogAllowed(ownerEpoch);
  }

  static void dismissAll(Context context) {
    java.util.ArrayList<java.lang.ref.WeakReference<AlertDialog>> values;
    synchronized (dialogs) {
      values = dialogs.remove(context);
    }
    if (values != null)
      for (java.lang.ref.WeakReference<AlertDialog> value : values) {
        AlertDialog dialog = value.get();
        if (dialog != null) dialog.dismiss();
      }
  }

  LargeDialogBuilder(Context context) {
    this(context, false);
  }

  @Override
  public AlertDialog create() {
    AlertDialog dialog = super.create();
    synchronized (dialogs) {
      java.util.ArrayList<java.lang.ref.WeakReference<AlertDialog>> values =
          dialogs.get(ownerContext);
      if (values == null) {
        values = new java.util.ArrayList<>();
        dialogs.put(ownerContext, values);
      }
      java.util.Iterator<java.lang.ref.WeakReference<AlertDialog>> iterator = values.iterator();
      while (iterator.hasNext()) if (iterator.next().get() == null) iterator.remove();
      values.add(new java.lang.ref.WeakReference<>(dialog));
    }
    if (!allowed()) dialog.getWindow().getDecorView().setVisibility(View.GONE);
    dialog
        .getWindow()
        .getDecorView()
        .addOnAttachStateChangeListener(
            new View.OnAttachStateChangeListener() {
              @Override
              public void onViewAttachedToWindow(View view) {
                if (!allowed()) {
                  view.setVisibility(View.GONE);
                  dialog.dismiss();
                  return;
                }
                TextView message = dialog.findViewById(android.R.id.message);
                if (message != null) message.setTextSize(30);
                view.removeOnAttachStateChangeListener(this);
              }

              @Override
              public void onViewDetachedFromWindow(View view) {}
            });
    return dialog;
  }
}
