package io.github.qiuyuxc.agconnect;

import android.app.Dialog;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.graphics.PixelFormat;
import android.graphics.Point;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Log;
import android.view.Gravity;
import android.view.Display;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.Toast;
import java.lang.ref.WeakReference;

/** A small system overlay owned and removed by the active connection service. */
public final class GmOverlay implements View.OnTouchListener {
    public static final String PREFERENCES = "agconnect_gm_overlay";
    private static WeakReference<GmOverlay> current = new WeakReference<>(null);
    private static int openPanels;
    private final Context context;
    private final WindowManager manager;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final SharedPreferences preferences;
    private final Button button;
    private final WindowManager.LayoutParams params;
    private Dialog panelWindow;
    private GmPanel panel;
    private final int touchSlop;
    private boolean attached, disposed, dragging, multiplePointers;
    private int pointer = -1;
    private float startX, startY, originalX, originalY, left, top, right, bottom;
    private String scope = "";
    private final Runnable check = new Runnable() {
        @Override public void run() { refresh(); if (!disposed) main.postDelayed(this, 2000); }
    };
    private final BroadcastReceiver screen = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (Intent.ACTION_SCREEN_OFF.equals(intent.getAction())) { hide(); dismissPanel(); }
            else refresh();
        }
    };

    public GmOverlay(Context service) {
        int type = Build.VERSION.SDK_INT >= 26 ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
        Display display = ((DisplayManager) service.getSystemService(Context.DISPLAY_SERVICE)).getDisplay(Display.DEFAULT_DISPLAY);
        context = Build.VERSION.SDK_INT >= 30
                ? service.createDisplayContext(display).createWindowContext(type, null) : service;
        manager = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
        preferences = service.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
        touchSlop = ViewConfiguration.get(context).getScaledTouchSlop();
        button = GmUi.handle(context);
        button.setContentDescription("打开辅助面板；可自由拖动，记住横竖屏位置");
        button.setOnClickListener(v -> openPanel());
        button.setOnTouchListener(this);
        button.setOnApplyWindowInsetsListener((view, insets) -> { main.post(this::restore); return insets; });
        params = new WindowManager.LayoutParams(GmUi.dp(context, 48), GmUi.dp(context, 48), type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN, PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.LEFT;
        params.setTitle("深空助手 GM");
        if (Build.VERSION.SDK_INT >= 28)
            params.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        IntentFilter filter = new IntentFilter();
        filter.addAction(Intent.ACTION_SCREEN_OFF); filter.addAction(Intent.ACTION_USER_PRESENT);
        filter.addAction(Intent.ACTION_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 33) service.registerReceiver(screen, filter, Context.RECEIVER_NOT_EXPORTED);
        else service.registerReceiver(screen, filter);
        receiverContext = service;
        current = new WeakReference<>(this);
        main.post(check);
    }
    private final Context receiverContext;

    public static boolean enabled(Context context) {
        return context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE).getBoolean("enabled", false);
    }
    public static void panelStarted() { openPanels++; changed(); }
    public static void panelStopped() { openPanels = Math.max(0, openPanels - 1); changed(); }
    private static void changed() { GmOverlay overlay = current.get(); if (overlay != null) overlay.refresh(); }

    public void refresh() {
        if (disposed) return;
        KeyguardManager lock = (KeyguardManager) context.getSystemService(Context.KEYGUARD_SERVICE);
        PowerManager power = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
        if (!GmOverlayPolicy.available(ConnectionService.status.running, enabled(context),
                Settings.canDrawOverlays(context),
                lock != null && lock.isKeyguardLocked() || power != null && !power.isInteractive())) {
            hide(); dismissPanel(); return;
        }
        if (openPanels > 0) { hide(); dismissPanel(); return; }
        if (panel != null) {
            hide();
            try { if (panel.serverChanged()) { dismissPanel(); refresh(); } }
            catch (IllegalArgumentException error) { dismissPanel(); refresh(); }
            return;
        }
        if (!attached) {
            restore();
            try { manager.addView(button, params); attached = true; }
            catch (RuntimeException error) {
                Toast.makeText(context, "悬浮按钮未能显示，请检查助手的悬浮窗权限。", Toast.LENGTH_LONG).show();
                preferences.edit().putBoolean("enabled", false).apply();
            }
        } else if (pointer == -1) restore();
    }

    private void openPanel() {
        if (disposed || !attached || panelWindow != null) return;
        refresh();
        if (!attached) return;
        try {
            GmServer.origin(context);
            Dialog window = new Dialog(context, GmPanel.themeResource(context));
            window.getWindow().setType(params.type);
            window.getWindow().setTitle("深空助手辅助面板");
            window.getWindow().addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
                    | WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH);
            window.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                    | WindowManager.LayoutParams.FLAG_ALT_FOCUSABLE_IM);
            window.setCanceledOnTouchOutside(true);
            panelWindow = window;
            panel = new GmPanel(context, window.getWindow(), window::dismiss);
            window.setOnDismissListener(ignored -> {
                if (panelWindow != window) return;
                dismissPanel(); refresh();
            });
            panel.create(); window.show(); hide();
        } catch (IllegalArgumentException error) {
            dismissPanel();
            Toast.makeText(context, "请先在连接助手中填写服务器地址，或选择手机本地模式。", Toast.LENGTH_LONG).show();
        } catch (RuntimeException error) {
            dismissPanel();
            Log.w("AgConnect", "GM overlay panel unavailable", error);
            Toast.makeText(context, "悬浮面板未能显示，请检查助手的悬浮窗权限。", Toast.LENGTH_LONG).show();
        }
    }
    private void dismissPanel() {
        GmPanel previous = panel; Dialog window = panelWindow;
        panel = null; panelWindow = null;
        if (previous != null) {
            try { previous.dispose(); } catch (RuntimeException error) {
                Log.w("AgConnect", "GM overlay panel cleanup failed", error);
            }
        }
        if (window != null) {
            try { window.dismiss(); } catch (RuntimeException ignored) { }
        }
    }

    private String key() {
        return context.getResources().getConfiguration().orientation == Configuration.ORIENTATION_LANDSCAPE
                ? "landscape_" : "portrait_";
    }
    private float fraction(String key, float fallback) {
        try { return GmGeometry.fraction(preferences.getFloat(key, fallback), fallback); }
        catch (ClassCastException ignored) { return fallback; }
    }
    private void bounds() {
        Rect area = new Rect(); WindowInsets insets;
        if (Build.VERSION.SDK_INT >= 30) {
            android.view.WindowMetrics metrics = manager.getMaximumWindowMetrics();
            area.set(metrics.getBounds()); insets = metrics.getWindowInsets();
        } else {
            Point size = new Point(); manager.getDefaultDisplay().getRealSize(size);
            area.set(0, 0, size.x, size.y); insets = button.getRootWindowInsets();
        }
        int il = 0, it = 0, ir = 0, ib = 0;
        if (insets != null) {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets safe = insets.getInsetsIgnoringVisibility(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                il = safe.left; it = safe.top; ir = safe.right; ib = safe.bottom;
            } else {
                il = insets.getStableInsetLeft(); it = insets.getStableInsetTop();
                ir = insets.getStableInsetRight(); ib = insets.getStableInsetBottom();
                if (Build.VERSION.SDK_INT >= 28 && insets.getDisplayCutout() != null) {
                    il = Math.max(il, insets.getDisplayCutout().getSafeInsetLeft());
                    it = Math.max(it, insets.getDisplayCutout().getSafeInsetTop());
                    ir = Math.max(ir, insets.getDisplayCutout().getSafeInsetRight());
                    ib = Math.max(ib, insets.getDisplayCutout().getSafeInsetBottom());
                }
            }
        }
        int margin = GmUi.dp(context, 4);
        params.width = params.height = Math.max(1, Math.min(GmUi.dp(context, 48),
                Math.min(area.width() - il - ir - 2 * margin, area.height() - it - ib - 2 * margin)));
        left = area.left + il + margin; top = area.top + it + margin;
        right = Math.max(left, area.right - ir - margin - params.width);
        bottom = Math.max(top, area.bottom - ib - margin - params.height);
    }
    private void restore() {
        if (disposed || pointer != -1) return;
        bounds(); scope = key();
        move(GmGeometry.restore(fraction(scope + "x", .02f), left, right),
                GmGeometry.restore(fraction(scope + "y", .34f), top, bottom));
    }
    private void move(float x, float y) {
        int previousX = params.x, previousY = params.y;
        params.x = Math.round(GmGeometry.clamp(x, left, right));
        params.y = Math.round(GmGeometry.clamp(y, top, bottom));
        if (attached && (previousX != params.x || previousY != params.y
                || button.getWidth() != params.width || button.getHeight() != params.height)) {
            try { manager.updateViewLayout(button, params); }
            catch (RuntimeException error) { hide(); }
        }
    }
    private void finishDrag() {
        if (pointer != -1 && dragging && scope.equals(key()))
            preferences.edit().putFloat(scope + "x", GmGeometry.save(params.x, left, right))
                    .putFloat(scope + "y", GmGeometry.save(params.y, top, bottom)).apply();
        pointer = -1; dragging = false; button.setPressed(false);
    }
    private void track(MotionEvent event) {
        int index = event.findPointerIndex(pointer);
        if (index < 0) return;
        float dx = event.getX(index) + event.getRawX() - event.getX() - startX;
        float dy = event.getY(index) + event.getRawY() - event.getY() - startY;
        if (dx * dx + dy * dy > touchSlop * touchSlop) dragging = true;
        if (dragging) { button.setPressed(false); move(originalX + dx, originalY + dy); }
    }
    @Override public boolean onTouch(View view, MotionEvent event) {
        switch (event.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                bounds(); scope = key(); pointer = event.getPointerId(0); dragging = false; multiplePointers = false;
                startX = event.getRawX(); startY = event.getRawY(); originalX = params.x; originalY = params.y;
                button.setPressed(true); return true;
            case MotionEvent.ACTION_MOVE: if (pointer != -1) track(event); return true;
            case MotionEvent.ACTION_POINTER_DOWN: multiplePointers = true; return true;
            case MotionEvent.ACTION_POINTER_UP:
                if (event.getPointerId(event.getActionIndex()) == pointer) finishDrag(); return true;
            case MotionEvent.ACTION_UP:
                if (pointer != -1) {
                    track(event); boolean click = !dragging && !multiplePointers; finishDrag();
                    if (click) view.performClick();
                }
                return true;
            case MotionEvent.ACTION_CANCEL: finishDrag(); return true;
            default: return false;
        }
    }
    private void hide() {
        finishDrag();
        if (!attached) return;
        attached = false;
        try { manager.removeViewImmediate(button); } catch (RuntimeException ignored) { }
    }
    public void configurationChanged() {
        finishDrag(); restore();
        if (panel != null) panel.configurationChanged();
        refresh();
    }
    public void dispose() {
        if (disposed) return;
        disposed = true; main.removeCallbacksAndMessages(null); hide(); dismissPanel();
        receiverContext.unregisterReceiver(screen);
        if (current.get() == this) current.clear();
    }
}
