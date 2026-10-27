package io.github.qiuyuxc.agdns;

import android.util.Log;
import java.lang.reflect.Method;
import java.net.InetAddress;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;
import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XSharedPreferences;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

public final class MainHook implements IXposedHookLoadPackage {
    private static final String TAG = "AGDnsRedirect";
    private static final AtomicInteger LOG_COUNT = new AtomicInteger();
    private static boolean installed;
    private static native int nativeStatus();
    private static native boolean nativeConfigure(String address);

    @Override public void handleLoadPackage(XC_LoadPackage.LoadPackageParam param) {
        if (!DnsPolicy.GAME.equals(param.packageName)) return;
        synchronized (MainHook.class) {
            if (installed) return;
            installed = true;
        }
        ServerAddress selected = ServerAddress.parse(ServerAddress.DEFAULT);
        try {
            XSharedPreferences preferences = new XSharedPreferences(ServerPreferences.PACKAGE, ServerPreferences.FILE);
            preferences.reload();
            if (preferences.getFile().canRead())
                selected = ServerAddress.stored(preferences.getString(ServerPreferences.KEY, ServerAddress.DEFAULT));
        } catch (Throwable error) {
            XposedBridge.log(TAG + ": using default server; preferences unavailable");
        }
        final ServerAddress server = selected;
        final byte[] addressBytes = server.bytes();
        int nativeHooks = 0;
        try {
            System.loadLibrary("agredirect");
            if (!nativeConfigure(server.host)) throw new IllegalStateException("Invalid native server address");
            nativeHooks = nativeStatus();
            Log.i(TAG, "Native hook status=" + nativeHooks);
            if ((nativeHooks & 1) == 0) XposedBridge.log(TAG + ": native getaddrinfo hook unavailable; check LSPosed native API support");
        } catch (Throwable error) {
            Log.e(TAG, "Native DNS hook unavailable", error);
            XposedBridge.log(TAG + ": native load failed: " + error);
        }
        if (DnsPolicy.GAME.equals(param.processName)) {
            try { GameGmEntry.install(); }
            catch (Throwable error) { XposedBridge.log(TAG + ": GM entry setup failed: " + error.getClass().getSimpleName()); }
        }
        int hooked = 0;
        for (Method method : InetAddress.class.getDeclaredMethods()) {
            String name = method.getName();
            if (!(name.equals("getByName") || name.equals("getAllByName")
                    || name.equals("getByNameOnNet") || name.equals("getAllByNameOnNet"))) continue;
            Class<?>[] parameters = method.getParameterTypes();
            final boolean array = method.getReturnType() == InetAddress[].class;
            if (parameters.length == 0 || parameters[0] != String.class
                    || (!array && method.getReturnType() != InetAddress.class)) continue;
            try {
                XposedBridge.hookMethod(method, new XC_MethodHook() {
                    @Override protected void beforeHookedMethod(MethodHookParam hook) throws Throwable {
                        String host = (String) hook.args[0];
                        if (!DnsPolicy.matches(host)) return;
                        InetAddress address = InetAddress.getByAddress(host, addressBytes);
                        hook.setResult(array ? new InetAddress[]{address} : address);
                        if (LOG_COUNT.getAndIncrement() < 24) Log.i(TAG, "Java " + host + " -> " + server.host);
                    }
                });
                hooked++;
            } catch (Throwable error) {
                XposedBridge.log(TAG + ": cannot hook " + method + ": " + error);
            }
        }
        Log.i(TAG, "Java hooks=" + hooked + ", process=" + param.processName);
        XposedBridge.log(TAG + ": active for " + param.processName + " -> " + server.host);
        // Some devices suppress application logcat. Record one small status
        // file in this game's cache, without URLs, account data, or tokens.
        try {
            File cache = new File(param.appInfo.dataDir, "cache");
            if (cache.isDirectory() || cache.mkdirs()) {
                String state = "policy=api-only\npid=" + android.os.Process.myPid()
                        + "\ntime_ms=" + System.currentTimeMillis() + "\njava_hooks=" + hooked
                        + "\nnative_hooks=" + nativeHooks + "\nserver=" + server.host + "\n";
                try (FileOutputStream output = new FileOutputStream(new File(cache, "ag_dns_redirect.status"))) {
                    output.write(state.getBytes(StandardCharsets.UTF_8));
                }
            }
        } catch (Throwable error) {
            Log.w(TAG, "Could not write diagnostic status", error);
        }
    }
}
