package io.github.qiuyuxc.agdns;

import android.app.Activity;
import android.app.Application;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.graphics.Rect;
import android.graphics.drawable.ColorDrawable;
import android.os.Bundle;
import android.os.Build;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.ViewTreeObserver;
import android.widget.Button;
import android.widget.PopupWindow;
import android.widget.Toast;
import java.util.WeakHashMap;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;

/** Installs one freely movable button only in the game's main activity/process. */
public final class GameGmEntry implements Application.ActivityLifecycleCallbacks {
    private final WeakHashMap<Activity, Handle> handles = new WeakHashMap<>();
    private static boolean registered;

    public static void install() {
        XposedHelpers.findAndHookMethod(Application.class, "attach", Context.class, new XC_MethodHook() {
            @Override protected void afterHookedMethod(MethodHookParam param) {
                synchronized (GameGmEntry.class) {
                    if (registered) return;
                    registered = true;
                }
                ((Application) param.thisObject).registerActivityLifecycleCallbacks(new GameGmEntry());
            }
        });
    }

    private static int dp(Activity activity, float value) {
        return Math.round(value * activity.getResources().getDisplayMetrics().density);
    }

    public static void open(Activity activity) {
        try {
            Intent intent = new Intent();
            intent.setClassName("io.github.qiuyuxc.agdns", "io.github.qiuyuxc.agdns.SubGmActivity");
            intent.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
            activity.startActivity(intent);
        } catch (RuntimeException error) {
            Toast.makeText(activity, "无法打开子 GM，请确认模块已更新", Toast.LENGTH_SHORT).show();
        }
    }

    @Override public void onActivityResumed(Activity activity) {
        if (!"com.mimir.sdk.UAMain".equals(activity.getClass().getName())) return;
        try {
            Handle existing = handles.get(activity);
            if (existing != null) { existing.show(); return; }
            View decor = activity.getWindow().getDecorView();
            Button button = GmUi.handle(activity);
            button.setContentDescription("打开辅助面板；可自由拖动，自动记住位置");
            button.setOnClickListener(v -> open(activity));
            Handle handle = new Handle(activity, decor, button);
            handles.put(activity, handle);
            handle.show();
        } catch (Throwable error) {
            XposedBridge.log("AGDnsRedirect: GM entry unavailable: " + error.getClass().getSimpleName());
        }
    }

    private static final class Handle implements View.OnTouchListener, View.OnLayoutChangeListener,
            ViewTreeObserver.OnGlobalLayoutListener {
        final Activity activity;
        final View root;
        final Button button;
        final PopupWindow window;
        final SharedPreferences preferences;
        final int touchSlop;
        int pointer = -1;
        float startX, startY, originalX, originalY;
        float left, top, right, bottom;
        float x, y;
        int originX, originY;
        String scope;
        boolean dragged, multiplePointers, visible;

        Handle(Activity activity, View root, Button button) {
            this.activity = activity; this.root = root; this.button = button;
            window = new PopupWindow(button, dp(activity, 48), dp(activity, 48), false);
            window.setWindowLayoutType(WindowManager.LayoutParams.TYPE_APPLICATION_PANEL);
            window.setBackgroundDrawable(new ColorDrawable(android.graphics.Color.TRANSPARENT));
            window.setTouchable(true); window.setOutsideTouchable(false);
            window.setInputMethodMode(PopupWindow.INPUT_METHOD_NOT_NEEDED);
            window.setClippingEnabled(true); window.setAnimationStyle(0);
            window.setElevation(dp(activity, 5));
            if (Build.VERSION.SDK_INT >= 29) {
                window.setIsLaidOutInScreen(true); window.setIsClippedToScreen(true);
            }
            preferences = activity.getSharedPreferences("agdns_gm_position", Context.MODE_PRIVATE);
            touchSlop = ViewConfiguration.get(activity).getScaledTouchSlop();
            button.setOnTouchListener(this);
            root.addOnLayoutChangeListener(this);
            root.getViewTreeObserver().addOnGlobalLayoutListener(this);
        }

        private String key() {
            return activity.getResources().getConfiguration().orientation == Configuration.ORIENTATION_LANDSCAPE
                    ? "landscape_" : "portrait_";
        }

        private float fraction(String name, float fallback) {
            try {
                float value = preferences.getFloat(key() + name, fallback);
                return GmGeometry.fraction(value, fallback);
            } catch (ClassCastException ignored) { return fallback; }
        }

        private void bounds() {
            int insetLeft = 0, insetTop = 0, insetRight = 0, insetBottom = 0;
            WindowInsets insets = root.getRootWindowInsets();
            if (insets != null) {
                if (Build.VERSION.SDK_INT >= 30) {
                    android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                    insetLeft = safe.left; insetTop = safe.top; insetRight = safe.right; insetBottom = safe.bottom;
                } else {
                    insetLeft = insets.getSystemWindowInsetLeft(); insetTop = insets.getSystemWindowInsetTop();
                    insetRight = insets.getSystemWindowInsetRight(); insetBottom = insets.getSystemWindowInsetBottom();
                    if (Build.VERSION.SDK_INT >= 28 && insets.getDisplayCutout() != null) {
                        insetLeft = Math.max(insetLeft, insets.getDisplayCutout().getSafeInsetLeft());
                        insetTop = Math.max(insetTop, insets.getDisplayCutout().getSafeInsetTop());
                        insetRight = Math.max(insetRight, insets.getDisplayCutout().getSafeInsetRight());
                        insetBottom = Math.max(insetBottom, insets.getDisplayCutout().getSafeInsetBottom());
                    }
                }
            }
            int[] origin = new int[2]; root.getLocationOnScreen(origin);
            originX = origin[0]; originY = origin[1];
            Rect visibleFrame = new Rect(); root.getWindowVisibleDisplayFrame(visibleFrame);
            int size = Math.max(1, Math.min(dp(activity, 48), Math.min(
                    root.getWidth() - insetLeft - insetRight, root.getHeight() - insetTop - insetBottom)));
            window.setWidth(size); window.setHeight(size);
            left = origin[0] + Math.max(root.getPaddingLeft(), insetLeft);
            top = origin[1] + Math.max(root.getPaddingTop(), insetTop);
            right = origin[0] + root.getWidth() - Math.max(root.getPaddingRight(), insetRight) - size;
            bottom = origin[1] + root.getHeight() - Math.max(root.getPaddingBottom(), insetBottom) - size;
            if (!visibleFrame.isEmpty()) {
                left = Math.max(left, visibleFrame.left); top = Math.max(top, visibleFrame.top);
                right = Math.min(right, visibleFrame.right - size);
                bottom = Math.min(bottom, visibleFrame.bottom - size);
            }
            right = Math.max(left, right); bottom = Math.max(top, bottom);
        }

        private void move(float x, float y) {
            this.x = GmGeometry.clamp(x, left, right); this.y = GmGeometry.clamp(y, top, bottom);
            // Move the separate window itself so Unity cannot clip/repaint this View.
            if (window.isShowing()) window.update(windowX(), windowY(), window.getWidth(), window.getHeight());
        }
        private int windowX() { return GmGeometry.windowOffset(x, originX, Build.VERSION.SDK_INT >= 29); }
        private int windowY() { return GmGeometry.windowOffset(y, originY, Build.VERSION.SDK_INT >= 29); }

        private void restore() {
            if (!visible || root.getWidth() <= 0 || root.getHeight() <= 0 || !root.isAttachedToWindow()
                    || root.getWindowToken() == null || activity.isFinishing() || activity.isDestroyed()) return;
            bounds();
            scope = key();
            float defaultX = right > left ? Math.min(1, dp(activity, 12) / (right - left)) : 0;
            move(left + fraction("x", defaultX) * (right - left), top + fraction("y", 1f / 3f) * (bottom - top));
            button.setVisibility(View.VISIBLE);
            if (!window.isShowing()) {
                button.animate().cancel(); button.setScaleX(1); button.setScaleY(1); button.setAlpha(1);
                try { window.showAtLocation(root, Gravity.TOP | Gravity.LEFT, windowX(), windowY()); }
                catch (RuntimeException error) {
                    XposedBridge.log("AGDnsRedirect: GM window unavailable: " + error.getClass().getSimpleName());
                }
            }
        }

        private void save() {
            preferences.edit()
                    .putFloat((scope == null ? key() : scope) + "x", GmGeometry.save(x, left, right))
                    .putFloat((scope == null ? key() : scope) + "y", GmGeometry.save(y, top, bottom))
                    .apply();
        }

        void show() { visible = true; root.post(this::restore); }
        void hide() {
            finishDrag(); visible = false;
            try { window.dismiss(); } catch (RuntimeException ignored) { }
        }
        void dispose() {
            hide(); root.removeOnLayoutChangeListener(this); button.setOnTouchListener(null);
            if (root.getViewTreeObserver().isAlive()) root.getViewTreeObserver().removeOnGlobalLayoutListener(this);
        }

        private void finishDrag() {
            if (pointer != -1 && dragged) save();
            pointer = -1; dragged = false; button.setPressed(false);
        }

        private void track(MotionEvent event) {
            int index = event.findPointerIndex(pointer);
            if (index < 0) return;
            // Indexed raw coordinates are only available from API 29. Derive
            // the same coordinates on Android 7+ while the button is moving.
            float dx = event.getX(index) + event.getRawX() - event.getX() - startX;
            float dy = event.getY(index) + event.getRawY() - event.getY() - startY;
            if (dx * dx + dy * dy > touchSlop * touchSlop) dragged = true;
            if (dragged) { button.setPressed(false); move(originalX + dx, originalY + dy); }
        }

        @Override public boolean onTouch(View view, MotionEvent event) {
            switch (event.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    bounds(); pointer = event.getPointerId(0); dragged = false; multiplePointers = false;
                    startX = event.getRawX(); startY = event.getRawY(); originalX = x; originalY = y;
                    button.setPressed(true); return true;
                case MotionEvent.ACTION_MOVE:
                    if (pointer != -1) track(event);
                    return true;
                case MotionEvent.ACTION_POINTER_DOWN:
                    multiplePointers = true; return true;
                case MotionEvent.ACTION_POINTER_UP:
                    if (event.getPointerId(event.getActionIndex()) == pointer) finishDrag();
                    return true;
                case MotionEvent.ACTION_UP:
                    if (pointer != -1) {
                        track(event);
                        boolean clicked = !dragged && !multiplePointers;
                        finishDrag();
                        if (clicked) view.performClick();
                    }
                    return true;
                case MotionEvent.ACTION_CANCEL:
                    finishDrag(); return true;
                default: return false;
            }
        }

        @Override public void onLayoutChange(View view, int l, int t, int r, int b, int ol, int ot, int or, int ob) {
            // Restore proportional positions independently for portrait and
            // landscape so rotation/resize cannot leave the handle offscreen.
            finishDrag();
            restore();
        }
        @Override public void onGlobalLayout() { if (visible && pointer == -1) restore(); }
    }

    @Override public void onActivityPaused(Activity activity) {
        Handle handle = handles.get(activity); if (handle != null) handle.hide();
    }
    @Override public void onActivityDestroyed(Activity activity) {
        Handle handle = handles.remove(activity); if (handle != null) handle.dispose();
    }
    @Override public void onActivityCreated(Activity a, Bundle b) { }
    @Override public void onActivityStarted(Activity a) { }
    @Override public void onActivityStopped(Activity a) { }
    @Override public void onActivitySaveInstanceState(Activity a, Bundle b) { }
}
