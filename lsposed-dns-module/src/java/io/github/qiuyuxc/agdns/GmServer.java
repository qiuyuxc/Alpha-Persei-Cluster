package io.github.qiuyuxc.agdns;

import android.content.Context;

/** Public module uses the server selected in its own settings. */
public final class GmServer {
    private GmServer() { }
    public static String origin(Context context) { return "https://" + ServerPreferences.read(context).host; }
}
