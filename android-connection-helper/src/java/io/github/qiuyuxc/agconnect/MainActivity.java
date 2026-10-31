package io.github.qiuyuxc.agconnect;

import android.Manifest;
import android.app.Activity;
import android.app.Dialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.VpnService;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

public final class MainActivity extends Activity {
    private static final int VPN_REQUEST = 10, NOTIFICATION_REQUEST = 11;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private MiuixUi ui;
    private TextView state, detail, addressSummary, stateMark;
    private Button toggle, launch;
    private String pendingServer;
    private boolean permissionPending;
    private boolean lastRunning;
    private ConnectionService.Status lastStatus;
    private Dialog addressDialog;
    private EditText addressInput;
    private final Runnable refresh = new Runnable() {
        public void run() { render(); handler.postDelayed(this, 1000); }
    };

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        ui = new MiuixUi(this);
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        if (Build.VERSION.SDK_INT >= 29) getWindow().setNavigationBarContrastEnforced(false);
        int flags = View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION;
        if (!ui.dark) {
            flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
            if (Build.VERSION.SDK_INT >= 26) flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        getWindow().getDecorView().setSystemUiVisibility(flags);
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true); scroll.setBackgroundColor(ui.background);
        scroll.setClipToPadding(false);
        FrameLayout frame = new FrameLayout(this);
        LinearLayout body = ui.column(); body.setPadding(ui.dp(16), ui.dp(36), ui.dp(16), ui.dp(28));
        int width = Math.min(getResources().getDisplayMetrics().widthPixels, ui.dp(600));
        FrameLayout.LayoutParams layout = new FrameLayout.LayoutParams(width, -2, Gravity.TOP | Gravity.CENTER_HORIZONTAL);
        frame.addView(body, layout); scroll.addView(frame);
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            int left, right, top, bottom;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                left = bars.left; right = bars.right; top = bars.top; bottom = bars.bottom;
            } else {
                left = insets.getSystemWindowInsetLeft(); right = insets.getSystemWindowInsetRight();
                top = insets.getSystemWindowInsetTop(); bottom = insets.getSystemWindowInsetBottom();
            }
            view.setPadding(left, top, right, bottom);
            layout.width = Math.min(getResources().getDisplayMetrics().widthPixels - left - right, ui.dp(600));
            body.setLayoutParams(layout);
            return insets.consumeSystemWindowInsets();
        });
        LinearLayout heading = ui.column(); heading.setPadding(ui.dp(12), 0, ui.dp(12), 0);
        heading.addView(ui.text("连接助手", 32, ui.text, false)); ui.gap(heading, 10);
        heading.addView(ui.text("深空之眼 · 免 root 进服", 15, ui.secondary, false));
        body.addView(heading); ui.gap(body, 28);

        LinearLayout connection = ui.card(body);
        connection.setPadding(ui.dp(20), ui.dp(24), ui.dp(20), ui.dp(20));
        LinearLayout titleRow = new LinearLayout(this); titleRow.setGravity(Gravity.CENTER_VERTICAL);
        stateMark = ui.text("○", 24, ui.primary, true); stateMark.setGravity(Gravity.CENTER);
        stateMark.setBackground(ui.shape(ui.softPrimary, 16));
        stateMark.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        titleRow.addView(stateMark, new LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)));
        state = ui.text("未连接", 22, ui.text, true);
        state.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(0, -2, 1);
        titleParams.setMarginStart(ui.dp(14)); titleRow.addView(state, titleParams); connection.addView(titleRow);
        ui.gap(connection, 16);
        detail = ui.text("", 15, ui.secondary, false); connection.addView(detail);
        ui.gap(connection, 22);
        toggle = ui.button("开启连接", true, () -> {
            if (ConnectionService.status.running) {
                // Android can keep an active VPN bound after stopService().
                // Send the same explicit shutdown command as the notification.
                try {
                    startService(new Intent(this, ConnectionService.class).setAction(ConnectionService.STOP));
                } catch (RuntimeException error) {
                    ui.message("无法停止", "停止指令未能送达，请从系统 VPN 设置断开连接后重试。");
                }
                handler.postDelayed(this::render, 100);
            } else prepareConnection();
        });
        connection.addView(toggle, new LinearLayout.LayoutParams(-1, -2));
        ui.gap(connection, 10);
        launch = ui.button("打开游戏", false, this::launchGame);
        connection.addView(launch, new LinearLayout.LayoutParams(-1, -2));

        ui.section(body, "服务器");
        LinearLayout server = ui.card(body);
        addressSummary = ui.row(server, "服务器地址", "点击填写 IPv4 地址", () -> editAddress(null));
        ui.section(body, "连接准备");
        LinearLayout preparation = ui.card(body);
        ui.row(preparation, "使用说明", "用户 CA 版游戏、证书与连接步骤", this::showGuide);
        ui.divider(preparation);
        ui.row(preparation, "安装服务器证书", "前往系统安全设置", () -> open(new Intent(Settings.ACTION_SECURITY_SETTINGS)));
        ui.divider(preparation);
        ui.row(preparation, "网络设置", "关闭其他 VPN 与私人 DNS", () -> open(new Intent(Settings.ACTION_WIRELESS_SETTINGS)));
        ui.section(body, "帮助");
        LinearLayout support = ui.card(body);
        ui.row(support, "连接诊断", "查看运行状态与解析计数", this::showDiagnostics);
        ui.gap(body, 24);
        TextView footer = ui.text("1.0", 12, ui.secondary, false);
        footer.setGravity(Gravity.CENTER); body.addView(footer);
        setContentView(scroll);
        if (saved != null) {
            pendingServer = saved.getString("pending");
            permissionPending = saved.getBoolean("permission_pending");
            if (saved.containsKey("address_draft")) handler.post(() -> editAddress(saved.getString("address_draft")));
        }
    }

    private String server() { return getPreferences(MODE_PRIVATE).getString("server", ""); }

    private void editAddress(String draft) {
        if (ConnectionService.status.running) {
            ui.message("服务器正在使用", "请先停止连接，再修改服务器地址。"); return;
        }
        LinearLayout content = ui.column();
        content.addView(ui.text("填写手机能访问的 IPv4 地址，不带协议、端口或路径。", 15, ui.secondary, false));
        ui.gap(content, 18);
        addressInput = new EditText(this);
        addressInput.setSingleLine(true); addressInput.setTextSize(18); addressInput.setTextColor(ui.text);
        addressInput.setHintTextColor(ui.secondary); addressInput.setHint("例如 192.168.1.20");
        addressInput.setInputType(InputType.TYPE_CLASS_PHONE); addressInput.setImeOptions(EditorInfo.IME_ACTION_DONE);
        addressInput.setContentDescription("服务器 IPv4 地址");
        addressInput.setPadding(ui.dp(16), ui.dp(12), ui.dp(16), ui.dp(12));
        addressInput.setBackground(ui.shape(ui.field, 12));
        addressInput.setText(draft == null ? server() : draft);
        addressInput.setSelection(addressInput.getText().length());
        content.addView(addressInput, new LinearLayout.LayoutParams(-1, ui.dp(56)));
        ui.gap(content, 12);
        TextView hint = ui.text("127.0.0.1 指手机自身。服务器在电脑或远程主机上时，请填写它的实际地址。", 14, ui.secondary, false);
        content.addView(hint);
        addressDialog = ui.dialog("服务器地址", content);
        Runnable save = () -> {
            try {
                if (ConnectionService.status.running) throw new IllegalArgumentException("请先停止连接再保存。");
                String value = ServerAddress.parse(addressInput.getText().toString()).host;
                getPreferences(MODE_PRIVATE).edit().putString("server", value).apply();
                addressSummary.setText(value); addressDialog.dismiss();
            } catch (IllegalArgumentException error) {
                hint.setText(error.getMessage()); hint.setTextColor(ui.error);
                hint.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
            }
        };
        ui.actions(content, addressDialog, "保存", save);
        addressInput.setOnEditorActionListener((view, action, event) -> {
            if (action != EditorInfo.IME_ACTION_DONE) return false;
            save.run(); return true;
        });
        addressDialog.show();
    }

    private void prepareConnection() {
        if (permissionPending) return;
        try {
            pendingServer = ServerAddress.parse(server()).host;
            getPackageManager().getApplicationInfo(ConnectionService.GAME, 0);
            permissionPending = true; render();
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_REQUEST);
            } else prepareVpn();
        } catch (IllegalArgumentException error) { pendingServer = null; editAddress(null); }
        catch (PackageManager.NameNotFoundException error) {
            pendingServer = null; ui.message("未找到游戏", "请先安装兼容的用户 CA 版深空之眼。");
        }
    }

    private void prepareVpn() {
        try {
            Intent permission = VpnService.prepare(this);
            if (permission == null) startConnection();
            else startActivityForResult(permission, VPN_REQUEST);
        } catch (RuntimeException error) {
            finishPermission(); ui.message("无法开启", "请检查系统 VPN 设置后重试。");
        }
    }

    private void startConnection() {
        if (pendingServer == null) { finishPermission(); return; }
        Intent service = new Intent(this, ConnectionService.class).setAction(ConnectionService.START)
                .putExtra("server", pendingServer);
        try {
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(service);
            else startService(service);
        } catch (RuntimeException error) { ui.message("无法开启", "系统拒绝启动连接，请检查 VPN 设置后重试。"); }
        finishPermission();
    }

    private void finishPermission() { pendingServer = null; permissionPending = false; render(); }

    @Override public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(code, permissions, results);
        // Notification denial does not revoke permission to run a foreground VPN.
        if (code == NOTIFICATION_REQUEST && pendingServer != null) prepareVpn();
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == VPN_REQUEST) {
            if (result == RESULT_OK) startConnection();
            else { finishPermission(); ui.message("未开启连接", "需要允许系统 VPN 请求才能使用免 root 连接。"); }
        }
    }

    private void launchGame() {
        if (!ConnectionService.status.running) {
            ui.message("请先开启连接", "开启连接后，再完全退出并重新打开游戏。"); return;
        }
        Intent game = getPackageManager().getLaunchIntentForPackage(ConnectionService.GAME);
        if (game == null) { ui.message("未找到游戏", "请先安装兼容的用户 CA 版客户端。"); return; }
        LinearLayout content = ui.column();
        content.addView(ui.text("如果游戏已经在运行，请先完全退出，再重新打开，以清除旧连接。", 16, ui.secondary, false));
        Dialog dialog = ui.dialog("重新打开游戏", content);
        ui.actions(content, dialog, "打开游戏", () -> { dialog.dismiss(); open(game); }); dialog.show();
    }

    private void showGuide() {
        ui.message("连接准备", "1. 安装用户 CA 版深空之眼（v0.308.301）。原版游戏仅安装 CA 仍可能无法登录。\n\n"
                + "2. 在系统安全设置中安装自己服务器的 CA 证书。\n\n"
                + "3. 关闭其他 VPN；将“私人 DNS / 私有 DNS”设为关闭。\n\n"
                + "4. 填写服务器 IPv4，开启连接并允许系统 VPN 请求，再完全退出并重开游戏。\n\n"
                + "助手运行不代表已通过服务器登录。资源从原地址下载；请保留助手后台运行，勿开启“无 VPN 时阻止连接”。");
    }

    private void showDiagnostics() {
        ConnectionService.Status current = ConnectionService.status;
        ui.message("连接诊断", current.title + "\n" + current.detail
                + "\n\n已处理进服解析：" + ConnectionService.redirected.get()
                + "\n其他解析失败：" + ConnectionService.dnsFailures.get()
                + "\n\n计数在每次开启时重置。无法登录时，请检查游戏版本、服务器 CA、服务器地址、端口与账号。");
    }

    private void render() {
        ConnectionService.Status current = ConnectionService.status;
        if (current != lastStatus) {
            state.setText(current.title); detail.setText(current.detail);
            stateMark.setText(current.running ? "✓" : "○");
            if (lastStatus == null || current.running != lastRunning) {
                ui.styleButton(toggle, !current.running);
                launch.setAlpha(current.running ? 1f : 0.55f);
                launch.setEnabled(current.running);
            }
            lastStatus = current; lastRunning = current.running;
        }
        toggle.setText(permissionPending ? "等待系统授权…" : current.running ? "停止连接" : "开启连接");
        toggle.setEnabled(!permissionPending); toggle.setAlpha(permissionPending ? 0.55f : 1f);
        String saved = server(); addressSummary.setText(saved.isEmpty() ? "点击填写 IPv4 地址" : saved);
    }

    @Override protected void onResume() { super.onResume(); handler.post(refresh); }
    @Override protected void onPause() { handler.removeCallbacks(refresh); super.onPause(); }
    @Override protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        ui.close();
        super.onDestroy();
    }
    @Override protected void onSaveInstanceState(Bundle out) {
        out.putString("pending", pendingServer); out.putBoolean("permission_pending", permissionPending);
        if (addressDialog != null && addressDialog.isShowing()) out.putString("address_draft", addressInput.getText().toString());
        super.onSaveInstanceState(out);
    }
    private void open(Intent intent) {
        try { startActivity(intent); }
        catch (ActivityNotFoundException error) { ui.message("无法打开", "请从手机系统设置或桌面手动打开。"); }
    }
}
