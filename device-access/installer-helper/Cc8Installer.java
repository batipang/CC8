import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Looper;
import android.os.Process;
import android.os.Handler;
import android.os.HandlerThread;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** Temporary shell-UID client of the stock package installer; never requests root. */
public final class Cc8Installer {
    private static String hex(byte[] data) {
        StringBuilder result = new StringBuilder();
        for (byte item : data) result.append(String.format("%02x", item & 255));
        return result.toString();
    }

    private static Context shellContext() throws Exception {
        if (Looper.myLooper() == null) Looper.prepareMainLooper();
        Class<?> activityThread = Class.forName("android.app.ActivityThread");
        Object thread = activityThread.getMethod("systemMain").invoke(null);
        Context system = (Context) activityThread.getMethod("getSystemContext").invoke(thread);
        return system.createPackageContext("com.android.shell", Context.CONTEXT_IGNORE_SECURITY);
    }

    public static void main(String[] args) {
        try {
            if (Process.myUid() != 2000 || Build.VERSION.SDK_INT != 27 || !"AILABS_S6".equals(Build.MODEL))
                throw new SecurityException("Expected this CC8 at shell UID 2000 / SDK 27");
            Context context = shellContext();
            PackageInstaller installer = context.getPackageManager().getPackageInstaller();
            if (args.length == 1 && "--probe".equals(args[0])) {
                System.out.println("CC8_INSTALLER_API_OK uid=" + Process.myUid() + " package=" + context.getPackageName());
                System.exit(0);
            }
            if (args.length != 4 || !"--install".equals(args[0]))
                throw new IllegalArgumentException("Expected --probe or --install PACKAGE BYTES SHA256");
            String packageName = args[1];
            long expectedSize = Long.parseLong(args[2]);
            String expectedHash = args[3];
            if (!packageName.matches("[A-Za-z][A-Za-z0-9_.]+") || expectedSize <= 0 || !expectedHash.matches("[a-f0-9]{64}"))
                throw new IllegalArgumentException("Invalid package, size or digest");
            PackageInstaller.SessionParams parameters = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
            parameters.setAppPackageName(packageName);
            parameters.setSize(expectedSize);
            java.lang.reflect.Field flags = PackageInstaller.SessionParams.class.getField("installFlags");
            flags.setInt(parameters, flags.getInt(parameters) | 2); // INSTALL_REPLACE_EXISTING; signatures still enforced.
            final int sessionId = installer.createSession(parameters);
            boolean committed = false;
            HandlerThread callbacks = new HandlerThread("cc8-install-result");
            callbacks.start();
            final CountDownLatch finished = new CountDownLatch(1);
            final AtomicBoolean successful = new AtomicBoolean(false);
            PackageInstaller.SessionCallback callbackListener = new PackageInstaller.SessionCallback() {
                public void onCreated(int id) { }
                public void onBadgingChanged(int id) { }
                public void onActiveChanged(int id, boolean active) { }
                public void onProgressChanged(int id, float progress) { }
                public void onFinished(int id, boolean success) {
                    if (id == sessionId) { successful.set(success); finished.countDown(); }
                }
            };
            installer.registerSessionCallback(callbackListener, new Handler(callbacks.getLooper()));
            try {
                PackageInstaller.Session session = installer.openSession(sessionId);
                try {
                    MessageDigest digest = MessageDigest.getInstance("SHA-256");
                    long count = 0;
                    OutputStream output = session.openWrite("base.apk", 0, expectedSize);
                    try {
                        byte[] buffer = new byte[32768];
                        while (count < expectedSize) {
                            int size = System.in.read(buffer, 0, (int) Math.min(buffer.length, expectedSize - count));
                            if (size < 0) throw new java.io.EOFException("Incomplete APK input");
                            output.write(buffer, 0, size);
                            digest.update(buffer, 0, size);
                            count += size;
                        }
                        session.fsync(output);
                    } finally { output.close(); }
                    if (!expectedHash.equals(hex(digest.digest()))) throw new SecurityException("APK stream digest mismatch");
                    System.out.println("CC8_APK_STREAM_VERIFIED bytes=" + count);
                    // A valid status receiver owned by the shell package. No receiver
                    // is installed, no app data is restored, and no permission is granted.
                    Intent status = new Intent("com.happyhome.cc8.INSTALL_STATUS").setPackage("com.android.shell");
                    PendingIntent callback = PendingIntent.getBroadcast(context, sessionId, status, PendingIntent.FLAG_ONE_SHOT);
                    session.commit(callback.getIntentSender());
                    committed = true;
                } finally { session.close(); }
                if (!finished.await(60, TimeUnit.SECONDS)) throw new IllegalStateException("Installer completion callback timed out");
                if (!successful.get()) throw new IllegalStateException("System installer reported failure");
                PackageInfo info = context.getPackageManager().getPackageInfo(packageName, 0);
                System.out.println("CC8_PACKAGE_INSTALLED " + info.packageName + " version=" + info.versionCode);
            } finally {
                installer.unregisterSessionCallback(callbackListener);
                callbacks.quitSafely();
                if (!committed) installer.abandonSession(sessionId);
            }
            System.exit(0);
        } catch (Throwable error) {
            error.printStackTrace(System.err);
            System.exit(1);
        }
    }
}
