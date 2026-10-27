package io.github.qiuyuxc.agdns;

import android.app.Activity;
import android.app.Application;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.os.Bundle;
import android.os.Build;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.FrameLayout;
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
            if (!(decor instanceof FrameLayout)) return;
            FrameLayout root = (FrameLayout) decor;
            Button button = GmUi.handle(activity);
            button.setContentDescription("打开子 GM；可自由拖动，自动记住位置");
            FrameLayout.LayoutParams layout = new FrameLayout.LayoutParams(dp(activity, 48), dp(activity, 48), Gravity.TOP | Gravity.LEFT);
            button.setVisibility(View.INVISIBLE);
            root.addView(button, layout);
            button.setOnClickListener(v -> open(activity));
            Handle handle = new Handle(activity, root, button);
            handles.put(activity, handle);
            handle.show();
        } catch (Throwable error) {
            XposedBridge.log("AGDnsRedirect: GM entry unavailable: " + error.getClass().getSimpleName());
        }
    }

    private static final class Handle implements View.OnTouchListener, View.OnLayoutChangeListener {
        final Activity activity;
        final FrameLayout root;
        final Button button;
        final SharedPreferences preferences;
        final int touchSlop;
        int pointer = -1;
        float startX, startY, originalX, originalY;
        float left, top, right, bottom;
        boolean dragged, multiplePointers, visible;

        Handle(Activity activity, FrameLayout root, Button button) {
            this.activity = activity; this.root = root; this.button = button;
            preferences = activity.getSharedPreferences("agdns_gm_position", Context.MODE_PRIVATE);
            touchSlop = ViewConfiguration.get(activity).getScaledTouchSlop();
            button.setOnTouchListener(this);
            root.addOnLayoutChangeListener(this);
        }

        private String key() {
            return activity.getResources().getConfiguration().orientation == Configuration.ORIENTATION_LANDSCAPE
                    ? "landscape_" : "portrait_";
        }

        private static float clamp(float value, float min, float max) {
            return Math.max(min, Math.min(max, value));
        }

        private float fraction(String name, float fallback) {
            try {
                float value = preferences.getFloat(key() + name, fallback);
                return Float.isNaN(value) || Float.isInfinite(value) ? fallback : clamp(value, 0, 1);
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
            int width = button.getWidth() > 0 ? button.getWidth() : button.getLayoutParams().width;
            int height = button.getHeight() > 0 ? button.getHeight() : button.getLayoutParams().height;
            left = Math.max(root.getPaddingLeft(), insetLeft);
            top = Math.max(root.getPaddingTop(), insetTop);
            right = Math.max(left, root.getWidth() - Math.max(root.getPaddingRight(), insetRight) - width);
            bottom = Math.max(top, root.getHeight() - Math.max(root.getPaddingBottom(), insetBottom) - height);
        }

        private void move(float x, float y) {
            // Absolute screen-side placement, with no edge snapping or axis lock.
            button.setX(clamp(x, left, right));
            button.setY(clamp(y, top, bottom));
        }

        private void restore() {
            if (!visible || root.getWidth() <= 0 || root.getHeight() <= 0) return;
            bounds();
            float defaultX = right > left ? Math.min(1, dp(activity, 12) / (right - left)) : 0;
            move(left + fraction("x", defaultX) * (right - left), top + fraction("y", 1f / 3f) * (bottom - top));
            button.setVisibility(View.VISIBLE);
            button.bringToFront();
        }

        private void save() {
            preferences.edit()
                    .putFloat(key() + "x", right > left ? (button.getX() - left) / (right - left) : 0)
                    .putFloat(key() + "y", bottom > top ? (button.getY() - top) / (bottom - top) : 0)
                    .apply();
        }

        void show() { visible = true; root.post(this::restore); }
        void hide() { finishDrag(); visible = false; button.setVisibility(View.GONE); }
        void dispose() {
            hide(); root.removeOnLayoutChangeListener(this); button.setOnTouchListener(null); root.removeView(button);
        }

        private void finishDrag() {
            if (pointer != -1 && dragged) save();
            pointer = -1; dragged = false; button.setPressed(false);
            root.requestDisallowInterceptTouchEvent(false);
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
                    startX = event.getRawX(); startY = event.getRawY(); originalX = button.getX(); originalY = button.getY();
                    button.setPressed(true); root.requestDisallowInterceptTouchEvent(true); return true;
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
            pointer = -1; dragged = false; button.setPressed(false);
            root.requestDisallowInterceptTouchEvent(false);
            restore();
        }
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
