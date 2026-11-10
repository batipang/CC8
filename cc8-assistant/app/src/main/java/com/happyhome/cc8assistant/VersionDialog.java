package com.happyhome.cc8assistant;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.os.Handler;
import android.widget.Toast;

/** Foreground read-only version view; a refresh removes declarations from a lost route. */
final class VersionDialog {
  interface Port {
    boolean foreground();

    long route();

    VersionPresentation.Peer peer();
  }

  private final Activity activity;
  private final Port port;
  private final Handler handler = new Handler();
  private AlertDialog dialog;
  private Runnable refresh;
  private String signatures = "", displayed = "";

  VersionDialog(Activity activity, Port port) {
    this.activity = activity;
    this.port = port;
  }

  private String message() {
    return VersionPresentation.message(
        BuildConfig.VERSION_NAME,
        BuildConfig.VERSION_CODE,
        BuildConfig.CC8_PROTOCOL,
        signatures,
        port.route(),
        port.peer());
  }

  void close() {
    if (refresh != null) handler.removeCallbacks(refresh);
    refresh = null;
    AlertDialog old = dialog;
    dialog = null;
    signatures = displayed = "";
    if (old != null) old.dismiss();
  }

  void open() {
    close();
    if (!port.foreground()) return;
    signatures = ApkSignatures.current(activity);
    displayed = message();
    AlertDialog owner =
        new LargeDialogBuilder(activity)
            .setTitle("版本与更新")
            .setMessage(displayed)
            .setNegativeButton("关闭", null)
            .setPositiveButton(
                "复制当前信息",
                (d, n) -> {
                  if (dialog != d || !port.foreground()) return;
                  String current = message();
                  ownerMessage(d, current);
                  Object service = activity.getSystemService(Context.CLIPBOARD_SERVICE);
                  if (service instanceof ClipboardManager) {
                    ((ClipboardManager) service)
                        .setPrimaryClip(ClipData.newPlainText("大黄蜂运行版本", current));
                  } else Toast.makeText(activity, "当前无法复制版本信息", Toast.LENGTH_SHORT).show();
                })
            .create();
    dialog = owner;
    owner.setOnDismissListener(
        d -> {
          if (dialog == owner) close();
        });
    owner.show();
    refresh =
        new Runnable() {
          public void run() {
            if (dialog != owner) return;
            if (!port.foreground()) {
              close();
              return;
            }
            String current = message();
            if (!current.equals(displayed)) {
              owner.setMessage(current);
              displayed = current;
            }
            handler.postDelayed(this, 1000);
          }
        };
    handler.postDelayed(refresh, 1000);
  }

  private void ownerMessage(android.content.DialogInterface owner, String current) {
    if (owner == dialog && !current.equals(displayed)) {
      dialog.setMessage(current);
      displayed = current;
    }
  }
}
