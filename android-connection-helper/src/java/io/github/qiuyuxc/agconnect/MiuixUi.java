package io.github.qiuyuxc.agconnect;

import android.app.Activity;
import android.app.Dialog;
import android.content.res.ColorStateList;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.Set;

/**
 * Native-view styling inspired by https://github.com/compose-miuix-ui/miuix.
 * This app does not embed the Compose library or proprietary MIUI resources.
 */
final class MiuixUi {
    final Activity activity;
    final boolean dark;
    final int background, surface, field, text, secondary, primary, softPrimary, line, error;
    private final Set<Dialog> dialogs = new HashSet<>();
    MiuixUi(Activity activity) {
        this.activity = activity;
        dark = (activity.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)
                == Configuration.UI_MODE_NIGHT_YES;
        background = Color.rgb(dark ? 0 : 247, dark ? 0 : 247, dark ? 0 : 247);
        surface = dark ? 0xff242424 : Color.WHITE;
        field = dark ? 0xff2b2b2b : 0xfff0f0f0;
        text = dark ? 0xfff2f2f2 : 0xff000000;
        secondary = dark ? 0xffa6a6a6 : 0xff737373;
        primary = dark ? 0xff277af7 : 0xff3482ff;
        softPrimary = dark ? 0xff172a47 : 0xffe8f0ff;
        line = dark ? 0xff2a2a2a : 0xffeeeeee;
        error = dark ? 0xffff8b83 : 0xffb3261e;
    }
    int dp(int n) { return Math.round(n * activity.getResources().getDisplayMetrics().density); }
    GradientDrawable shape(int color, int radius) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color); drawable.setCornerRadius(dp(radius)); return drawable;
    }
    RippleDrawable ripple(int color, int radius) {
        return new RippleDrawable(ColorStateList.valueOf(dark ? 0x22ffffff : 0x14000000),
                shape(color, radius), shape(Color.WHITE, radius));
    }
    LinearLayout column() {
        LinearLayout result = new LinearLayout(activity);
        result.setOrientation(LinearLayout.VERTICAL); return result;
    }
    TextView text(String value, int size, int color, boolean medium) {
        TextView result = new TextView(activity);
        result.setText(value); result.setTextSize(size); result.setTextColor(color);
        result.setFontFeatureSettings("kern");
        result.setTypeface(Typeface.create(medium ? "sans-serif-medium" : "sans-serif", Typeface.NORMAL));
        result.setIncludeFontPadding(false);
        result.setLineSpacing(dp(3), 1);
        return result;
    }
    void gap(LinearLayout parent, int height) {
        parent.addView(new View(activity), new LinearLayout.LayoutParams(1, dp(height)));
    }
    void section(LinearLayout parent, String value) {
        TextView label = text(value, 14, secondary, false);
        label.setPadding(dp(16), dp(24), dp(16), dp(10)); parent.addView(label);
    }
    LinearLayout card(LinearLayout parent) {
        LinearLayout result = column();
        result.setBackground(shape(surface, 16));
        result.setClipToOutline(true);
        parent.addView(result, new LinearLayout.LayoutParams(-1, -2));
        return result;
    }
    void divider(LinearLayout parent) {
        View divider = new View(activity); divider.setBackgroundColor(line);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(1));
        params.setMarginStart(dp(20)); params.setMarginEnd(dp(20)); parent.addView(divider, params);
    }
    TextView row(LinearLayout parent, String title, String summary, Runnable action) {
        LinearLayout row = new LinearLayout(activity);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(20), dp(16), dp(20), dp(16)); row.setMinimumHeight(dp(80));
        row.setBackground(ripple(surface, 0));
        row.setFocusable(true); row.setOnClickListener(view -> action.run());
        LinearLayout labels = column();
        labels.addView(text(title, 17, text, false));
        TextView subtitle = text(summary, 14, secondary, false);
        gap(labels, 5); labels.addView(subtitle);
        row.addView(labels, new LinearLayout.LayoutParams(0, -2, 1));
        TextView arrow = text("›", 28, secondary, false);
        arrow.setPadding(dp(12), 0, 0, 0);
        arrow.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        row.addView(arrow); parent.addView(row);
        return subtitle;
    }
    Button button(String label, boolean prominent, Runnable action) {
        Button button = new Button(activity);
        button.setText(label); button.setTextSize(17); button.setAllCaps(false);
        button.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        button.setMinHeight(dp(52)); button.setMinimumHeight(dp(52));
        button.setMinWidth(0); button.setMinimumWidth(0);
        button.setPadding(dp(16), dp(12), dp(16), dp(12));
        button.setStateListAnimator(null); button.setElevation(0);
        styleButton(button, prominent);
        button.setOnClickListener(view -> {
            view.performHapticFeedback(android.view.HapticFeedbackConstants.VIRTUAL_KEY);
            action.run();
        });
        return button;
    }
    void styleButton(Button button, boolean prominent) {
        // A slightly deeper text-button blue retains white-text contrast.
        button.setTextColor(ColorStateList.valueOf(prominent ? Color.WHITE : text));
        button.setBackground(ripple(prominent ? 0xff2674ec : field, 16));
    }
    Dialog dialog(String title, LinearLayout content) {
        Dialog dialog = new Dialog(activity);
        dialogs.add(dialog);
        dialog.setOnDismissListener(ignored -> dialogs.remove(dialog));
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE);
        LinearLayout box = column(); box.setPadding(dp(24), dp(24), dp(24), dp(24));
        box.setBackground(shape(surface, 28));
        box.addView(text(title, 21, text, true)); gap(box, 18);
        box.addView(content);
        ScrollView scroll = new ScrollView(activity);
        scroll.setFillViewport(false); scroll.addView(box);
        dialog.setContentView(scroll);
        Window window = dialog.getWindow();
        if (window != null) {
            window.setBackgroundDrawableResource(android.R.color.transparent);
            window.addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);
            window.setDimAmount(0.35f);
            window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        }
        dialog.setOnShowListener(ignored -> {
            if (window != null) {
                int width = Math.min(activity.getResources().getDisplayMetrics().widthPixels - dp(32), dp(480));
                window.setLayout(width, WindowManager.LayoutParams.WRAP_CONTENT);
            }
        });
        return dialog;
    }
    void actions(LinearLayout content, Dialog dialog, String positive, Runnable action) {
        gap(content, 24);
        LinearLayout buttons = new LinearLayout(activity);
        Button cancel = button("取消", false, dialog::dismiss);
        Button accept = button(positive, true, action);
        LinearLayout.LayoutParams left = new LinearLayout.LayoutParams(0, -2, 1);
        left.setMarginEnd(dp(12)); buttons.addView(cancel, left);
        buttons.addView(accept, new LinearLayout.LayoutParams(0, -2, 1)); content.addView(buttons);
    }
    void message(String title, String message) {
        LinearLayout content = column();
        content.addView(text(message, 16, secondary, false));
        Dialog dialog = dialog(title, content);
        gap(content, 24);
        content.addView(button("知道了", true, dialog::dismiss), new LinearLayout.LayoutParams(-1, -2));
        dialog.show();
    }
    void close() {
        for (Dialog dialog : new ArrayList<>(dialogs)) dialog.dismiss();
        dialogs.clear();
    }
}
