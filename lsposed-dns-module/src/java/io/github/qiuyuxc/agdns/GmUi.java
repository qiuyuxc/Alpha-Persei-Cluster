package io.github.qiuyuxc.agdns;

import android.content.Context;
import android.content.res.ColorStateList;
import android.content.res.Configuration;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.animation.DecelerateInterpolator;
import android.widget.Button;

/** Platform Views styled after Miuix's rounded surfaces and blue accent. */
public final class GmUi {
    public static final int BLUE = 0xff3482ff;
    private GmUi() { }
    public static int dp(Context context, float value) {
        return Math.round(value * context.getResources().getDisplayMetrics().density);
    }
    public static boolean dark(Context context) {
        return (context.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
    }
    public static int surface(Context c) { return dark(c) ? 0xff232325 : 0xfffafafa; }
    public static int fill(Context c) { return dark(c) ? 0xff323235 : 0xffefeff1; }
    public static int text(Context c) { return dark(c) ? 0xfff4f4f6 : 0xff19191b; }
    public static int muted(Context c) { return dark(c) ? 0xffaaaab0 : 0xff85858b; }
    public static GradientDrawable round(Context c, int color, float radius) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(color); shape.setCornerRadius(dp(c, radius));
        return shape;
    }
    public static Button button(Context c, String label, boolean primary) {
        Button button = new PressButton(c);
        button.setText(label); button.setTextSize(14);
        style(button, primary ? BLUE : fill(c), primary ? 0xffffffff : text(c), 16);
        button.setPadding(dp(c, 18), 0, dp(c, 18), 0);
        return button;
    }
    public static Button handle(Context c) {
        Button button = new PressButton(c);
        button.setText("GM"); button.setTextSize(13); button.setLetterSpacing(.015f);
        style(button, dark(c) ? 0xf22e3342 : 0xf2f4f7ff, dark(c) ? 0xff91b9ff : 0xff286ce0, 18);
        button.setElevation(dp(c, 5));
        return button;
    }
    public static Button icon(Context c, boolean close) {
        Button button = new IconButton(c, close);
        style(button, fill(c), text(c), 16);
        button.setContentDescription(close ? "关闭子 GM" : "重新加载子 GM 页面");
        return button;
    }
    private static void style(Button button, int background, int foreground, int radius) {
        Context c = button.getContext();
        button.setAllCaps(false); button.setIncludeFontPadding(false);
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
            float scale = dp(getContext(), 22) / 24f;
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
}
