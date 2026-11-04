package io.github.qiuyuxc.agdns;

import android.content.Context;
import android.content.ContextWrapper;
import android.content.res.ColorStateList;
import android.content.res.Configuration;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.animation.DecelerateInterpolator;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Switch;
import android.widget.TextView;

/** Platform Views styled after Miuix's rounded surfaces and blue accent. */
public final class GmUi {
    public static final int BLUE = 0xff3482ff;
    public static final float FONT_SP = 11;
    public interface ThemeOwner { boolean darkTheme(); }
    private GmUi() { }
    public static int dp(Context context, float value) {
        return Math.round(value * context.getResources().getDisplayMetrics().density);
    }
    public static boolean dark(Context context) {
        Context current = context;
        while (current instanceof ContextWrapper && !(current instanceof ThemeOwner)) {
            Context next = ((ContextWrapper) current).getBaseContext();
            if (next == current) break;
            current = next;
        }
        if (current instanceof ThemeOwner) return ((ThemeOwner) current).darkTheme();
        return systemDark(context);
    }
    public static boolean systemDark(Context context) {
        return (context.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
    }
    public static int surface(Context c) { return dark(c) ? 0xff232325 : 0xfffafafa; }
    public static int fill(Context c) { return dark(c) ? 0xff323235 : 0xffefeff1; }
    public static int text(Context c) { return dark(c) ? 0xfff4f4f6 : 0xff19191b; }
    public static int muted(Context c) { return dark(c) ? 0xffaaaab0 : 0xff85858b; }
    public static int accent(Context c) { return dark(c) ? 0xff91b9ff : BLUE; }
    public static int selected(Context c) { return dark(c) ? 0xff263b57 : 0xffe9f1ff; }
    public static GradientDrawable round(Context c, int color, float radius) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(color); shape.setCornerRadius(dp(c, radius));
        return shape;
    }
    public static Button button(Context c, String label, boolean primary) {
        Button button = new PressButton(c);
        button.setText(label); button.setTextSize(FONT_SP);
        button.setTag(primary ? "gm:primary" : "gm:secondary");
        style(button, primary ? BLUE : fill(c), primary ? 0xffffffff : text(c), 11);
        button.setPadding(dp(c, 10), 0, dp(c, 10), 0);
        return button;
    }
    public static Button handle(Context c) {
        Button button = new PressButton(c);
        button.setText("GM"); button.setTextSize(FONT_SP); button.setLetterSpacing(.015f);
        button.setTag("gm:handle");
        style(button, dark(c) ? 0xf22e3342 : 0xf2f4f7ff, dark(c) ? 0xff91b9ff : 0xff286ce0, 18);
        button.setElevation(dp(c, 5));
        return button;
    }
    public static Button icon(Context c, boolean close) {
        Button button = new IconButton(c, close);
        button.setTag("gm:icon");
        style(button, fill(c), text(c), 10);
        button.setContentDescription(close ? "关闭辅助面板" : "刷新辅助面板");
        return button;
    }
    public static Button navigation(Context c, String label, boolean active) {
        Button button = new PressButton(c); button.setText(label); button.setTextSize(FONT_SP);
        button.setTag(active ? "gm:nav-active" : "gm:nav");
        style(button, active ? selected(c) : 0x00000000, active ? accent(c) : muted(c), 10);
        return button;
    }
    public static Button theme(Context c) {
        Button button = new ThemeButton(c); button.setTag("gm:theme");
        style(button, fill(c), accent(c), 10);
        button.setContentDescription(dark(c) ? "切换到日间" : "切换到夜间");
        return button;
    }
    public static void repaint(View view) {
        Context c = view.getContext(); Object tag = view.getTag();
        if (view instanceof Button) {
            Button button = (Button) view;
            if ("gm:primary".equals(tag)) style(button, BLUE, 0xffffffff, 11);
            else if ("gm:nav-active".equals(tag)) style(button, selected(c), accent(c), 10);
            else if ("gm:nav".equals(tag)) style(button, 0x00000000, muted(c), 10);
            else if ("gm:handle".equals(tag)) style(button, dark(c) ? 0xf22e3342 : 0xf2f4f7ff, accent(c), 18);
            else style(button, fill(c), "gm:theme".equals(tag) ? accent(c) : text(c), 10);
            if ("gm:quick".equals(tag)) button.setMaxLines(2);
            if ("gm:theme".equals(tag)) button.setContentDescription(dark(c) ? "切换到日间" : "切换到夜间");
            button.invalidate();
        } else if (view instanceof EditText) {
            EditText field = (EditText) view; field.setTextColor(text(c)); field.setHintTextColor(muted(c));
            field.setBackground(round(c, fill(c), 10));
        } else if (view instanceof Switch) {
            Switch toggle = (Switch) view; toggle.setTextColor(text(c));
            toggle.setThumbTintList(new ColorStateList(new int[][]{{android.R.attr.state_checked},{}}, new int[]{accent(c),muted(c)}));
            toggle.setTrackTintList(new ColorStateList(new int[][]{{android.R.attr.state_checked},{}}, new int[]{selected(c),fill(c)}));
        } else if (view instanceof TextView) {
            ((TextView) view).setTextColor("gm:online".equals(tag) ? (dark(c) ? 0xff77cdb5 : 0xff309782)
                    : "gm:muted".equals(tag) ? muted(c) : text(c));
        }
        if ("gm:surface".equals(tag)) view.setBackground(round(c, surface(c), 20));
        if ("gm:sidebar".equals(tag)) view.setBackground(round(c, fill(c), 12));
        if (view instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) view;
            for (int i = 0; i < group.getChildCount(); i++) repaint(group.getChildAt(i));
        }
    }
    private static void style(Button button, int background, int foreground, int radius) {
        Context c = button.getContext();
        button.setAllCaps(false); button.setIncludeFontPadding(false);
        button.setMaxLines(1); button.setEllipsize(android.text.TextUtils.TruncateAt.END);
        button.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        button.setGravity(Gravity.CENTER); button.setPadding(0, 0, 0, 0);
        button.setMinWidth(0); button.setMinimumWidth(0); button.setMinHeight(0); button.setMinimumHeight(0);
        button.setTextColor(foreground); button.setStateListAnimator(null);
        button.setBackground(new RippleDrawable(ColorStateList.valueOf(0x203482ff),
                round(c, background, radius), round(c, 0xffffffff, radius)));
    }
    private static class PressButton extends Button {
        PressButton(Context c) { super(c); }
        @Override public void setPressed(boolean pressed) {
            super.setPressed(pressed);
            if (isAttachedToWindow()) animate().scaleX(pressed ? .94f : 1f).scaleY(pressed ? .94f : 1f)
                    .setDuration(160).setInterpolator(new DecelerateInterpolator()).start();
        }
    }
    private static final class IconButton extends PressButton {
        final boolean close;
        final Paint pen = new Paint(Paint.ANTI_ALIAS_FLAG);
        IconButton(Context c, boolean close) { super(c); this.close = close; }
        @Override protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            canvas.save();
            float scale = dp(getContext(), 16) / 24f;
            canvas.translate(getWidth() / 2f - 12 * scale, getHeight() / 2f - 12 * scale);
            canvas.scale(scale, scale);
            pen.setColor(text(getContext())); pen.setStrokeWidth(1.85f);
            pen.setStyle(Paint.Style.STROKE); pen.setStrokeCap(Paint.Cap.ROUND); pen.setStrokeJoin(Paint.Join.ROUND);
            if (close) {
                canvas.drawLine(6.5f, 6.5f, 17.5f, 17.5f, pen);
                canvas.drawLine(17.5f, 6.5f, 6.5f, 17.5f, pen);
            } else {
                Path refresh = new Path();
                refresh.moveTo(3, 12);
                refresh.cubicTo(3, 7.03f, 7.03f, 3, 12, 3);
                refresh.cubicTo(14.88f, 3, 17.18f, 4.32f, 19.2f, 6.3f);
                refresh.lineTo(21, 8);
                refresh.moveTo(21, 3); refresh.lineTo(21, 8); refresh.lineTo(16, 8);
                refresh.moveTo(21, 12);
                refresh.cubicTo(21, 16.97f, 16.97f, 21, 12, 21);
                refresh.cubicTo(9.12f, 21, 6.82f, 19.68f, 4.8f, 17.7f);
                refresh.lineTo(3, 16);
                refresh.moveTo(8, 16); refresh.lineTo(3, 16); refresh.lineTo(3, 21);
                canvas.drawPath(refresh, pen);
            }
            canvas.restore();
        }
    }
    private static final class ThemeButton extends PressButton {
        final Paint pen = new Paint(Paint.ANTI_ALIAS_FLAG);
        ThemeButton(Context c) { super(c); }
        @Override protected void onDraw(Canvas canvas) {
            super.onDraw(canvas); canvas.save();
            float scale = dp(getContext(), 16) / 24f;
            canvas.translate(getWidth() / 2f - 12 * scale, getHeight() / 2f - 12 * scale); canvas.scale(scale, scale);
            pen.setColor(accent(getContext())); pen.setStyle(Paint.Style.STROKE); pen.setStrokeWidth(1.7f); pen.setStrokeCap(Paint.Cap.ROUND);
            if (dark(getContext())) {
                canvas.drawCircle(12, 12, 4, pen);
                for (int i = 0; i < 8; i++) {
                    double angle = i * Math.PI / 4;
                    canvas.drawLine(12 + (float)Math.cos(angle)*7, 12 + (float)Math.sin(angle)*7,
                            12 + (float)Math.cos(angle)*9, 12 + (float)Math.sin(angle)*9, pen);
                }
            } else {
                Path moon = new Path(); moon.moveTo(19.5f, 13.2f);
                moon.cubicTo(12, 15, 9, 9, 10.5f, 4);
                moon.cubicTo(2, 6, 2, 18, 10, 20);
                moon.cubicTo(15, 21, 19, 18, 19.5f, 13.2f); canvas.drawPath(moon, pen);
            }
            canvas.restore();
        }
    }
}
