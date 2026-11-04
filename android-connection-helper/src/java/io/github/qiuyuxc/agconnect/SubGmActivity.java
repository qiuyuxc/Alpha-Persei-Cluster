package io.github.qiuyuxc.agconnect;

import android.app.Activity;
import android.content.Intent;
import android.content.res.Configuration;
import android.os.Bundle;

/** Hosts the same native panel when opened from the connection helper's home screen. */
public final class SubGmActivity extends Activity {
    private GmPanel panel;

    @Override public void onCreate(Bundle state) {
        setTheme(GmPanel.themeResource(this));
        super.onCreate(state);
        panel = new GmPanel(this, getWindow(), this::finish);
        setFinishOnTouchOutside(true);
        panel.create();
    }
    @Override protected void onStart() { super.onStart(); GmOverlay.panelStarted(); }
    @Override protected void onStop() { GmOverlay.panelStopped(); super.onStop(); }
    @Override protected void onResume() {
        super.onResume(); checkServer();
    }
    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent); setIntent(intent); checkServer();
    }
    private void checkServer() {
        try { if (panel != null && panel.serverChanged()) recreate(); }
        catch (IllegalArgumentException error) { finish(); }
    }
    @Override public void onConfigurationChanged(Configuration next) {
        super.onConfigurationChanged(next);
        if (panel != null) panel.configurationChanged();
    }
    @Override public void onBackPressed() { finish(); }
    @Override public void finish() {
        if (panel != null) panel.dispose();
        super.finish();
    }
    @Override protected void onDestroy() {
        if (panel != null) panel.dispose();
        super.onDestroy();
    }
}
