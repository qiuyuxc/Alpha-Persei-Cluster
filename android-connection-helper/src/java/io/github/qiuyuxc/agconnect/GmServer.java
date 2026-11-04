package io.github.qiuyuxc.agconnect;

import android.content.Context;
import android.content.SharedPreferences;

public final class GmServer {
    private GmServer() { }
    public static String origin(Context context) {
        SharedPreferences connection = context.getSharedPreferences("MainActivity", Context.MODE_PRIVATE);
        return GmEndpoint.origin(connection.getString("server", ""), connection.getBoolean("local", false));
    }
}
