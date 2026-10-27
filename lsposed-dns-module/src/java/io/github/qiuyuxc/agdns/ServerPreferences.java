package io.github.qiuyuxc.agdns;

import android.content.Context;
import android.content.SharedPreferences;

/** Only the non-secret destination is shared with the hooked game process. */
public final class ServerPreferences {
    public static final String PACKAGE = "io.github.qiuyuxc.agdns";
    public static final String FILE = "server_address";
    public static final String KEY = "ipv4";
    private ServerPreferences() { }

    @SuppressWarnings("deprecation")
    private static SharedPreferences shared(Context context) {
        // LSPosed API 93+ supplies its relocated, cross-process preferences.
        return context.getSharedPreferences(FILE, Context.MODE_WORLD_READABLE);
    }

    public static ServerAddress read(Context context) {
        try { return ServerAddress.stored(shared(context).getString(KEY, ServerAddress.DEFAULT)); }
        catch (SecurityException | ClassCastException ignored) { return ServerAddress.parse(ServerAddress.DEFAULT); }
    }

    public static void save(Context context, ServerAddress address) {
        try {
            if (!shared(context).edit().putString(KEY, address.host).commit())
                throw new IllegalStateException("保存失败，请重试");
        } catch (SecurityException unavailable) {
            throw new IllegalStateException("请先在 LSPosed 中启用模块，再完全关闭并重新打开本设置页");
        }
    }
}
