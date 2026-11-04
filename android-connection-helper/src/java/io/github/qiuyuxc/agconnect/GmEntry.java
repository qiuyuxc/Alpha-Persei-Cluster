package io.github.qiuyuxc.agconnect;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.widget.Toast;

public final class GmEntry {
    private GmEntry() { }
    public static void open(Context context) {
        try {
            GmServer.origin(context);
            Intent intent = new Intent(context, SubGmActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            if (!(context instanceof Activity)) intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(intent);
        } catch (IllegalArgumentException error) {
            Toast.makeText(context, "请先在连接助手中填写服务器地址，或选择手机本地模式。", Toast.LENGTH_LONG).show();
        } catch (RuntimeException error) {
            Toast.makeText(context, "无法打开辅助面板，请从连接助手首页打开。", Toast.LENGTH_LONG).show();
        }
    }
}
