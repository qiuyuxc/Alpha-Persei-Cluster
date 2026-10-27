package io.github.qiuyuxc.agdns;

import android.app.Activity;
import android.os.Bundle;
import android.graphics.Color;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Button;
import android.widget.EditText;
import android.text.InputType;
import android.content.Intent;

public final class MainActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        int pad = (int) (24 * getResources().getDisplayMetrics().density);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(pad, pad * 2, pad, pad);
        content.setBackgroundColor(GmUi.fill(this));
        TextView title = new TextView(this);
        title.setText("深空私服 DNS");
        title.setTextSize(26);
        title.setTextColor(GmUi.text(this));
        content.addView(title);
        TextView label = new TextView(this);
        label.setText("服务器 IPv4 地址"); label.setTextSize(16); label.setTextColor(GmUi.text(this));
        label.setPadding(0, pad, 0, pad / 2);
        content.addView(label);
        EditText address = new EditText(this);
        address.setSingleLine(true);
        address.setInputType(InputType.TYPE_CLASS_PHONE);
        address.setTextColor(GmUi.text(this));
        address.setHintTextColor(GmUi.muted(this));
        address.setText(ServerPreferences.read(this).host);
        address.setHint(ServerAddress.DEFAULT);
        address.setContentDescription("服务器 IPv4 地址");
        address.setPadding(pad, pad / 2, pad, pad / 2);
        address.setBackground(GmUi.round(this, GmUi.surface(this), 16));
        content.addView(address, new LinearLayout.LayoutParams(-1, GmUi.dp(this, 56)));
        TextView status = new TextView(this);
        status.setTextColor(GmUi.muted(this)); status.setTextSize(14);
        status.setPadding(0, pad / 2, 0, pad / 2);
        status.setText("默认 127.0.0.1 指本机。填写自己的服务器 IP，保存后完全关闭并重启游戏。");
        Button save = GmUi.button(this, "保存服务器地址", true);
        save.setOnClickListener(v -> {
            try {
                ServerAddress selected = ServerAddress.parse(address.getText().toString());
                ServerPreferences.save(this, selected);
                address.setText(selected.host);
                address.setError(null);
                status.setText("已保存。请完全关闭并重启游戏。\n子 GM：" + selected.gmUrl());
            } catch (IllegalArgumentException invalid) {
                address.setError(invalid.getMessage());
            } catch (IllegalStateException unavailable) {
                status.setText(unavailable.getMessage());
            }
        });
        LinearLayout.LayoutParams saveLayout = new LinearLayout.LayoutParams(-1, GmUi.dp(this, 52));
        saveLayout.topMargin = pad / 2;
        content.addView(save, saveLayout);
        content.addView(status);
        Button gm = GmUi.button(this, "打开子 GM 后台", true);
        gm.setOnClickListener(v -> startActivity(new Intent(this, SubGmActivity.class)));
        LinearLayout.LayoutParams buttonLayout = new LinearLayout.LayoutParams(-1, GmUi.dp(this, 52));
        buttonLayout.setMargins(0, pad, 0, pad);
        content.addView(gm, buttonLayout);
        TextView text = new TextView(this);
        text.setTextSize(15);
        text.setTextColor(GmUi.text(this));
        text.setLineSpacing(0, 1.25f);
        text.setPadding(pad, pad, pad, pad);
        text.setBackground(GmUi.round(this, GmUi.surface(this), 24));
        text.setText("启用步骤\n1. 在 LSPosed 中启用本模块。\n2. 作用域只勾选「深空之眼」。\n3. 保存服务器地址，完全关闭游戏后重新打开。"
                + "\n\n请使用支持用户 CA 的游戏客户端，并安装自己服务器的 CA 证书。"
                + "\n\n生效范围\n仅重定向登录接口 open.ys4fun.com 和游戏网关 prod-api-activity.ys4fun.com。"
                + "\n\n资源下载\n官方资源、语音、安装包和 PV 视频保持官方直连。"
                + "\n\n子 GM\nGM 按钮默认在左侧，可自由拖动到屏幕内任意位置，横竖屏分别记住位置。点击打开后台，关闭或返回即可回到游戏。使用管理员账号登录。"
                + "\n不需要系统悬浮窗权限。网页尚未部署时会提示等待部署。"
                + "\n\n停用\n在 LSPosed 中停用本模块，再重启游戏。\n\nARM64 · LSPosed");
        content.addView(text);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(content);
        setContentView(scroll);
    }
}
