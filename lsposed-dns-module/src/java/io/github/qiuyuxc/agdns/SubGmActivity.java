package io.github.qiuyuxc.agdns;

import android.app.Activity;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Typeface;
import android.content.res.ColorStateList;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

/** A regular translucent Activity: no system overlay or JavaScript bridge. */
public final class SubGmActivity extends Activity {
    private WebView web;
    private LinearLayout drawer;
    private ProgressBar progress;
    private TextView error;
    private boolean loadFailed;
    private GmPagePolicy policy;

    private int dp(float value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        policy = new GmPagePolicy(ServerPreferences.read(this));
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0x48000000);
        root.setOnClickListener(v -> finish());
        drawer = new LinearLayout(this);
        drawer.setOrientation(LinearLayout.VERTICAL);
        drawer.setBackground(GmUi.round(this, GmUi.surface(this), 26));
        drawer.setClipToOutline(true);
        drawer.setElevation(dp(12));
        drawer.setOnClickListener(v -> { });
        FrameLayout.LayoutParams drawerLayout = new FrameLayout.LayoutParams(-1, -1, Gravity.END);
        drawerLayout.setMargins(dp(8), dp(8), dp(8), dp(8));
        root.addView(drawer, drawerLayout);
        setContentView(root);
        if (Build.VERSION.SDK_INT >= 30) {
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                android.graphics.Insets bars = insets.getInsets(android.view.WindowInsets.Type.systemBars()
                        | android.view.WindowInsets.Type.displayCutout() | android.view.WindowInsets.Type.ime());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
                return insets;
            });
        }
        root.addOnLayoutChangeListener((v, left, top, right, bottom, oldLeft, oldTop, oldRight, oldBottom) -> resizeDrawer(root));

        LinearLayout bar = new LinearLayout(this);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(18), dp(8), dp(10), dp(8));
        TextView title = new TextView(this);
        title.setText("子 GM"); title.setTextSize(19); title.setTextColor(GmUi.text(this));
        title.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        title.setIncludeFontPadding(false);
        bar.addView(title, new LinearLayout.LayoutParams(0, dp(44), 1));
        title.setGravity(Gravity.CENTER_VERTICAL);
        Button reload = GmUi.icon(this, false);
        reload.setOnClickListener(v -> { if (web != null) web.loadUrl(policy.url()); });
        LinearLayout.LayoutParams actionLayout = new LinearLayout.LayoutParams(dp(44), dp(44));
        actionLayout.rightMargin = dp(8);
        bar.addView(reload, actionLayout);
        Button close = GmUi.icon(this, true);
        close.setOnClickListener(v -> finish());
        bar.addView(close, new LinearLayout.LayoutParams(dp(44), dp(44)));
        drawer.addView(bar);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setProgressTintList(ColorStateList.valueOf(GmUi.BLUE));
        progress.setProgressBackgroundTintList(ColorStateList.valueOf(GmUi.fill(this)));
        drawer.addView(progress, new LinearLayout.LayoutParams(-1, dp(2)));
        error = new TextView(this);
        error.setTextSize(14); error.setTextColor(GmUi.text(this)); error.setPadding(dp(18), dp(18), dp(18), dp(18));
        error.setLineSpacing(dp(3), 1f);
        error.setBackground(GmUi.round(this, GmUi.fill(this), 18));
        error.setVisibility(View.GONE);
        LinearLayout.LayoutParams errorLayout = new LinearLayout.LayoutParams(-1, -2);
        errorLayout.setMargins(dp(16), dp(14), dp(16), dp(16));
        drawer.addView(error, errorLayout);

        try {
            web = new WebView(this);
            WebSettings settings = web.getSettings();
            settings.setJavaScriptEnabled(true);
            settings.setDomStorageEnabled(true);
            // Honor a viewport sized to the actual drawer at the desired page
            // scale. This scales all web content, not just text or the drawer.
            settings.setUseWideViewPort(true);
            settings.setAllowFileAccess(false);
            settings.setAllowContentAccess(false);
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
            settings.setSupportMultipleWindows(false);
            settings.setJavaScriptCanOpenWindowsAutomatically(false);
            CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
            if (Build.VERSION.SDK_INT >= 26) settings.setSafeBrowsingEnabled(true);
            web.setWebChromeClient(new WebChromeClient() {
                @Override public void onProgressChanged(WebView view, int value) {
                    progress.setProgress(value);
                    progress.setVisibility(value == 100 || loadFailed ? View.GONE : View.VISIBLE);
                }
            });
            web.setWebViewClient(new WebViewClient() {
                @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                    if (policy.allows(request.getUrl().toString())) return false;
                    Toast.makeText(SubGmActivity.this, "请使用子 GM 内的功能入口", Toast.LENGTH_SHORT).show();
                    return true;
                }
                @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                    if (policy.allowsResource(request.getUrl().toString())) return null;
                    return new WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden",
                            java.util.Collections.emptyMap(), new java.io.ByteArrayInputStream(new byte[0]));
                }
                @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                    loadFailed = false; error.setVisibility(View.GONE); web.setVisibility(View.VISIBLE);
                    progress.setVisibility(View.VISIBLE);
                }
                @Override public void onPageFinished(WebView view, String url) {
                    progress.setVisibility(View.GONE);
                    applyPageScale();
                }
                @Override public void onPageCommitVisible(WebView view, String url) {
                    applyPageScale();
                }
                @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError sslError) {
                    handler.cancel();
                    showError("服务器证书验证失败。请确认已安装私服用户 CA 证书，然后点重载。");
                }
                @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError failure) {
                    if (request.isForMainFrame()) showError("暂时无法连接子 GM。请检查网络和服务器状态，然后点重载。");
                }
                @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                    if (request.isForMainFrame()) showError(response.getStatusCode() == 404
                            ? "服务器还没有部署子 GM 页面。部署完成后点重载即可。"
                            : "子 GM 页面暂时不可用（HTTP " + response.getStatusCode() + "），请稍后重载。");
                }
            });
            web.addOnLayoutChangeListener((view, l, t, r, b, ol, ot, or, ob) -> {
                if (r - l != or - ol) view.post(() -> applyPageScale());
            });
            drawer.addView(web, new LinearLayout.LayoutParams(-1, 0, 1));
            // Every opening validates the persisted web session through /api/auth/me.
            web.loadUrl(policy.url());
        } catch (RuntimeException failure) {
            showError("无法启动系统 WebView，请检查 Android System WebView 是否可用。");
        }
    }

    private void resizeDrawer(FrameLayout root) {
        int available = root.getWidth() - root.getPaddingLeft() - root.getPaddingRight() - dp(16);
        if (available <= 0) return;
        int width = Math.min(dp(960), Math.round(available * (available > dp(600) ? .88f : 1f)));
        FrameLayout.LayoutParams params = (FrameLayout.LayoutParams) drawer.getLayoutParams();
        if (params.width != width) { params.width = width; drawer.setLayoutParams(params); }
    }

    private void applyPageScale() {
        if (web == null || loadFailed || web.getWidth() <= 0 || !policy.allows(web.getUrl())) return;
        boolean landscape = getResources().getConfiguration().orientation == Configuration.ORIENTATION_LANDSCAPE;
        float scale = landscape ? .8f : 1f;
        float density = getResources().getDisplayMetrics().density;
        int viewportWidth = Math.max(1, Math.round(web.getWidth() / density / scale));
        String viewport = "width=" + viewportWidth + ", initial-scale=" + scale + ", viewport-fit=cover";
        // React Aria reports a zoom-normalized overlay height. Use CSS viewport
        // units for this page's modal layers while zoomed out so masks fill it.
        String overlay = landscape ? ".modal__backdrop,.modal__container{--visual-viewport-height:100dvh!important}" : "";
        // The numeric width compensates for zooming out, keeping the entire
        // content area filled and fixed-position dialogs in the same viewport.
        web.evaluateJavascript("(function(){var m=document.querySelector('meta[name=viewport]');"
                + "if(m&&m.getAttribute('content')!=='" + viewport + "')m.setAttribute('content','"
                + viewport + "');var s=document.getElementById('ag-module-viewport-style');"
                + "if(!s){s=document.createElement('style');s.id='ag-module-viewport-style';document.head.appendChild(s);}"
                + "s.textContent='" + overlay + "';})()", null);
    }

    private void showError(String text) {
        loadFailed = true;
        progress.setVisibility(View.GONE);
        error.setText(text); error.setVisibility(View.VISIBLE);
        if (web != null) web.setVisibility(View.GONE);
    }

    @Override public void onConfigurationChanged(Configuration config) {
        super.onConfigurationChanged(config);
        drawer.requestLayout();
        if (web != null) web.post(() -> applyPageScale());
    }
    @Override protected void onPause() { if (web != null) web.onPause(); super.onPause(); }
    @Override protected void onResume() {
        super.onResume();
        if (policy != null && !policy.url().equals(ServerPreferences.read(this).gmUrl())) { recreate(); return; }
        if (web != null) web.onResume();
    }
    @Override public void onBackPressed() {
        if (web != null && !loadFailed && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
    @Override protected void onDestroy() {
        if (web != null) {
            drawer.removeView(web); web.stopLoading(); web.setWebChromeClient(null);
            web.setWebViewClient(new WebViewClient()); web.destroy(); web = null;
        }
        super.onDestroy();
    }
}
