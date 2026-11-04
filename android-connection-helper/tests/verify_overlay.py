#!/usr/bin/env python3
"""Exercise the production overlay controller with host-side Android window doubles.

This checks click routing, window flags and lifecycle without a device. Android's
actual window composition, keyboard and game behavior still require device tests.
Use --source-dir to reproduce the same scenarios against a saved source baseline.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

PACKAGE = "io/github/qiuyuxc/agconnect"
SOURCE = Path(__file__).resolve().parents[1] / "src/java" / PACKAGE

DOUBLES = {
    "android/content/Context.java": """
package android.content;
import android.app.KeyguardManager;
import android.os.PowerManager;
import android.view.*;
import android.hardware.display.DisplayManager;
import android.content.res.Resources;
import java.util.*;
public class Context {
    public static final int MODE_PRIVATE=0, RECEIVER_NOT_EXPORTED=4;
    public static final String WINDOW_SERVICE="window", DISPLAY_SERVICE="display",
            KEYGUARD_SERVICE="keyguard", POWER_SERVICE="power";
    public final Resources resources=new Resources();
    public final WindowManager manager=new WindowManager(this);
    public final KeyguardManager lock=new KeyguardManager();
    public final PowerManager power=new PowerManager();
    public final List<BroadcastReceiver> receivers=new ArrayList<>();
    private final Map<String,SharedPreferences> preferences=new HashMap<>();
    public int activities, windowContextType, receiverFlags;
    public Object getSystemService(String name) {
        if (WINDOW_SERVICE.equals(name)) return manager;
        if (DISPLAY_SERVICE.equals(name)) return new DisplayManager(manager.getDefaultDisplay());
        if (KEYGUARD_SERVICE.equals(name)) return lock;
        if (POWER_SERVICE.equals(name)) return power;
        throw new AssertionError("Unexpected service: "+name);
    }
    public Resources getResources() { return resources; }
    public SharedPreferences getSharedPreferences(String name,int mode) {
        if(!preferences.containsKey(name)) preferences.put(name,new SharedPreferences());
        return preferences.get(name);
    }
    public Context createDisplayContext(Display display) { return this; }
    public Context createWindowContext(int type,Object options) { windowContextType=type; return this; }
    public Intent registerReceiver(BroadcastReceiver receiver,IntentFilter filter) {
        receivers.add(receiver); return null;
    }
    public Intent registerReceiver(BroadcastReceiver receiver,IntentFilter filter,int flags) {
        receiverFlags=flags; return registerReceiver(receiver,filter);
    }
    public void unregisterReceiver(BroadcastReceiver receiver) {
        if(!receivers.remove(receiver)) throw new AssertionError("Receiver removed twice");
    }
    public void broadcast(String action) {
        for(BroadcastReceiver receiver:new ArrayList<>(receivers)) receiver.onReceive(this,new Intent(action));
    }
    public void startActivity(Intent intent) { activities++; }
}
""",
    "android/content/SharedPreferences.java": """
package android.content;
import java.util.*;
public class SharedPreferences {
    private final Map<String,Object> values=new HashMap<>();
    public boolean getBoolean(String key,boolean fallback) { return values.containsKey(key)?(Boolean)values.get(key):fallback; }
    public float getFloat(String key,float fallback) { return values.containsKey(key)?(Float)values.get(key):fallback; }
    public Editor edit() { return new Editor(); }
    public class Editor {
        public Editor putBoolean(String key,boolean value) { values.put(key,value); return this; }
        public Editor putFloat(String key,float value) { values.put(key,value); return this; }
        public void apply() { }
    }
}
""",
    "android/content/Intent.java": """
package android.content;
public class Intent {
    public static final String ACTION_SCREEN_OFF="off", ACTION_SCREEN_ON="on", ACTION_USER_PRESENT="present";
    private final String action;
    public Intent(String action) { this.action=action; }
    public String getAction() { return action; }
}
""",
    "android/content/IntentFilter.java": "package android.content; public class IntentFilter { public void addAction(String action) {} }",
    "android/content/BroadcastReceiver.java": "package android.content; public abstract class BroadcastReceiver { public abstract void onReceive(Context context,Intent intent); }",
    "android/content/DialogInterface.java": "package android.content; public interface DialogInterface { interface OnDismissListener { void onDismiss(DialogInterface dialog); } }",
    "android/content/res/Configuration.java": "package android.content.res; public class Configuration { public static final int ORIENTATION_PORTRAIT=1, ORIENTATION_LANDSCAPE=2; public int orientation=1; }",
    "android/content/res/Resources.java": """
package android.content.res;
import android.util.DisplayMetrics;
public class Resources {
    public final Configuration configuration=new Configuration();
    public Configuration getConfiguration() { return configuration; }
    public DisplayMetrics getDisplayMetrics() { return new DisplayMetrics(); }
}
""",
    "android/util/DisplayMetrics.java": "package android.util; public class DisplayMetrics { public float density=1; }",
    "android/util/Log.java": "package android.util; public class Log { public static int w(String tag,String message,Throwable error) { return 0; } }",
    "android/graphics/PixelFormat.java": "package android.graphics; public class PixelFormat { public static final int TRANSLUCENT=-3; }",
    "android/graphics/Point.java": "package android.graphics; public class Point { public int x,y; }",
    "android/graphics/Rect.java": """
package android.graphics;
public class Rect {
    public int left,top,right,bottom;
    public void set(int l,int t,int r,int b) { left=l;top=t;right=r;bottom=b; }
    public void set(Rect other) { set(other.left,other.top,other.right,other.bottom); }
    public int width() { return right-left; } public int height() { return bottom-top; }
}
""",
    "android/graphics/Insets.java": "package android.graphics; public class Insets { public int left=0,top=24,right=0,bottom=16; }",
    "android/hardware/display/DisplayManager.java": "package android.hardware.display; import android.view.Display; public class DisplayManager { private final Display display; public DisplayManager(Display d){display=d;} public Display getDisplay(int id){return display;} }",
    "android/os/Build.java": "package android.os; public class Build { public static class VERSION { public static int SDK_INT=35; } }",
    "android/os/Looper.java": "package android.os; public class Looper { public static Looper getMainLooper() { return new Looper(); } }",
    "android/os/Handler.java": """
package android.os;
import java.util.*;
public class Handler {
    public static final List<Handler> all=new ArrayList<>();
    public final List<Runnable> tasks=new ArrayList<>();
    public Handler(Looper looper) { all.add(this); }
    public boolean post(Runnable task) { tasks.add(task); return true; }
    public boolean postDelayed(Runnable task,long delay) { tasks.add(task); return true; }
    public void removeCallbacksAndMessages(Object token) { tasks.clear(); }
}
""",
    "android/os/PowerManager.java": "package android.os; public class PowerManager { public boolean interactive=true; public boolean isInteractive(){return interactive;} }",
    "android/app/KeyguardManager.java": "package android.app; public class KeyguardManager { public boolean locked; public boolean isKeyguardLocked(){return locked;} }",
    "android/provider/Settings.java": "package android.provider; import android.content.Context; public class Settings { public static boolean granted=true; public static boolean canDrawOverlays(Context c){return granted;} }",
    "android/view/Gravity.java": "package android.view; public class Gravity { public static final int TOP=48,LEFT=3; }",
    "android/view/View.java": """
package android.view;
import android.content.Context;
public class View {
    public interface OnClickListener { void onClick(View view); }
    public interface OnTouchListener { boolean onTouch(View view,MotionEvent event); }
    public interface OnApplyWindowInsetsListener { WindowInsets onApplyWindowInsets(View view,WindowInsets insets); }
    private OnClickListener click; private OnTouchListener touch;
    public int width=48,height=48;
    public View(Context context) {}
    public void setContentDescription(String description) {}
    public void setOnClickListener(OnClickListener listener) { click=listener; }
    public void setOnTouchListener(OnTouchListener listener) { touch=listener; }
    public void setOnApplyWindowInsetsListener(OnApplyWindowInsetsListener listener) {}
    public boolean performClick() { if(click!=null)click.onClick(this); return true; }
    public boolean touch(int action,float x,float y) { return touch.onTouch(this,new MotionEvent(action,x,y)); }
    public void setPressed(boolean pressed) {}
    public int getWidth(){return width;} public int getHeight(){return height;}
    public WindowInsets getRootWindowInsets(){return new WindowInsets();}
}
""",
    "android/view/MotionEvent.java": """
package android.view;
public class MotionEvent {
    public static final int ACTION_DOWN=0,ACTION_UP=1,ACTION_MOVE=2,ACTION_CANCEL=3,ACTION_POINTER_DOWN=5,ACTION_POINTER_UP=6;
    private final int action; private final float x,y;
    public MotionEvent(int a,float x,float y){action=a;this.x=x;this.y=y;}
    public int getActionMasked(){return action;} public int getPointerId(int i){return i;}
    public int findPointerIndex(int id){return id==0?0:-1;} public int getActionIndex(){return 0;}
    public float getX(){return x;} public float getY(){return y;}
    public float getX(int i){return x;} public float getY(int i){return y;}
    public float getRawX(){return x;} public float getRawY(){return y;}
}
""",
    "android/view/ViewConfiguration.java": "package android.view; import android.content.Context; public class ViewConfiguration { public static ViewConfiguration get(Context c){return new ViewConfiguration();} public int getScaledTouchSlop(){return 8;} }",
    "android/view/DisplayCutout.java": "package android.view; public class DisplayCutout { public int getSafeInsetLeft(){return 0;} public int getSafeInsetTop(){return 0;} public int getSafeInsetRight(){return 0;} public int getSafeInsetBottom(){return 0;} }",
    "android/view/WindowInsets.java": """
package android.view;
import android.graphics.Insets;
public class WindowInsets {
    public static class Type { public static int systemBars(){return 1;} public static int displayCutout(){return 2;} }
    public Insets getInsetsIgnoringVisibility(int mask){return new Insets();}
    public int getStableInsetLeft(){return 0;} public int getStableInsetTop(){return 24;}
    public int getStableInsetRight(){return 0;} public int getStableInsetBottom(){return 16;}
    public DisplayCutout getDisplayCutout(){return null;}
}
""",
    "android/view/Display.java": """
package android.view;
import android.content.Context;
import android.graphics.Point;
public class Display {
    public static final int DEFAULT_DISPLAY=0;
    private final Context context;
    public Display(Context c){context=c;}
    public void getRealSize(Point p){
        boolean landscape=context.resources.configuration.orientation==2;
        p.x=landscape?1920:1080; p.y=landscape?1080:1920;
    }
}
""",
    "android/view/WindowMetrics.java": """
package android.view;
import android.graphics.*;
public class WindowMetrics {
    private final Rect bounds=new Rect();
    public WindowMetrics(Display display){Point p=new Point();display.getRealSize(p);bounds.set(0,0,p.x,p.y);}
    public Rect getBounds(){return bounds;} public WindowInsets getWindowInsets(){return new WindowInsets();}
}
""",
    "android/view/WindowManager.java": """
package android.view;
import android.content.Context;
import java.util.*;
public class WindowManager {
    private final Display display;
    public final Map<View,LayoutParams> views=new HashMap<>();
    public WindowManager(Context c){display=new Display(c);}
    public void addView(View view,LayoutParams params){
        if(views.put(view,params)!=null)throw new AssertionError("Duplicate window");
        view.width=params.width;view.height=params.height;
    }
    public void updateViewLayout(View view,LayoutParams params){
        if(!views.containsKey(view))throw new AssertionError("Updating detached window");
        view.width=params.width;view.height=params.height;
    }
    public void removeViewImmediate(View view){
        if(views.remove(view)==null)throw new AssertionError("Removing detached window");
    }
    public Display getDefaultDisplay(){return display;}
    public WindowMetrics getMaximumWindowMetrics(){return new WindowMetrics(display);}
    public static class LayoutParams {
        public static final int TYPE_PHONE=2002,TYPE_APPLICATION_OVERLAY=2038,FLAG_NOT_FOCUSABLE=8,
                FLAG_NOT_TOUCH_MODAL=32,FLAG_LAYOUT_IN_SCREEN=256,FLAG_WATCH_OUTSIDE_TOUCH=262144,
                FLAG_ALT_FOCUSABLE_IM=131072,LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES=1;
        public int width,height,type,flags,gravity,x,y,layoutInDisplayCutoutMode;
        public LayoutParams(int w,int h,int t,int f,int format){width=w;height=h;type=t;flags=f;}
        public void setTitle(String title){}
    }
}
""",
    "android/view/Window.java": """
package android.view;
public class Window {
    private final WindowManager.LayoutParams params=new WindowManager.LayoutParams(0,0,2,0,0);
    public WindowManager.LayoutParams getAttributes(){return params;}
    public void setType(int type){params.type=type;} public void setTitle(String title){}
    public void addFlags(int flags){params.flags|=flags;} public void clearFlags(int flags){params.flags&=~flags;}
}
""",
    "android/widget/Button.java": "package android.widget; import android.content.Context; public class Button extends android.view.View { public Button(Context c){super(c);} }",
    "android/widget/Toast.java": """
package android.widget;
import android.content.Context;
public class Toast {
    public static final int LENGTH_LONG=1;
    public static int shown;
    public static Toast makeText(Context c,String message,int duration){return new Toast();}
    public void show(){shown++;}
}
""",
    "android/app/Dialog.java": """
package android.app;
import android.content.*;
import android.view.Window;
import java.util.*;
public class Dialog implements DialogInterface {
    public static final List<Dialog> all=new ArrayList<>();
    public static boolean failNextShow;
    private final Window window=new Window();
    private OnDismissListener listener;
    public boolean showing,outsideCancelable;
    public Dialog(Context c,int theme){all.add(this);}
    public Window getWindow(){return window;}
    public void setCanceledOnTouchOutside(boolean value){outsideCancelable=value;}
    public void setOnDismissListener(OnDismissListener value){listener=value;}
    public void show(){
        if(failNextShow){failNextShow=false;throw new RuntimeException("Window denied");}
        int type=window.getAttributes().type;
        if(type!=2002 && type!=2038)throw new AssertionError("Overlay requires a system window type");
        showing=true;
    }
    public void dismiss(){if(showing){showing=false;if(listener!=null)listener.onDismiss(this);}}
    public void outside(){if(outsideCancelable)dismiss();}
}
""",
    PACKAGE + "/ConnectionService.java": "package io.github.qiuyuxc.agconnect; public class ConnectionService { public static final Status status=new Status(); public static class Status {public boolean running=true;} }",
    PACKAGE + "/GmUi.java": "package io.github.qiuyuxc.agconnect; import android.content.Context; import android.widget.Button; public class GmUi { public static Button last; public static Button handle(Context c){return last=new Button(c);} public static int dp(Context c,float n){return Math.round(n);} }",
    PACKAGE + "/GmServer.java": "package io.github.qiuyuxc.agconnect; import android.content.Context; public class GmServer { public static String origin=\"https://127.0.0.1:8443\"; public static boolean invalid; public static String origin(Context c){if(invalid)throw new IllegalArgumentException(\"Missing server\");return origin;} }",
    PACKAGE + "/GmEntry.java": "package io.github.qiuyuxc.agconnect; import android.content.*; public class GmEntry { public static void open(Context c){c.startActivity(new Intent(\"gm\"));} }",
    PACKAGE + "/GmPanel.java": """
package io.github.qiuyuxc.agconnect;
import android.content.Context;
import android.view.Window;
import java.util.*;
public class GmPanel {
    public static final List<GmPanel> all=new ArrayList<>();
    public static boolean failNextCreate;
    public final String origin;
    public boolean disposed;
    public int configurations;
    private final Runnable close;
    public static int themeResource(Context c){return 1;}
    public GmPanel(Context c,Window window,Runnable dismiss){origin=GmServer.origin(c);close=dismiss;all.add(this);}
    public void create(){if(failNextCreate){failNextCreate=false;throw new RuntimeException("Construction failed");}}
    public boolean serverChanged(){return !origin.equals(GmServer.origin(null));}
    public void configurationChanged(){configurations++;}
    public void dispose(){disposed=true;}
    public void close(){dispose();close.run();}
}
""",
}

SCENARIOS = """
import android.app.Dialog;
import android.content.*;
import android.os.*;
import android.provider.Settings;
import android.view.*;
import android.widget.*;
import io.github.qiuyuxc.agconnect.*;
public class OverlayLifecycleTest {
    private static int checks;
    private static void check(boolean value,String label) {
        checks++; if(!value)throw new AssertionError("API "+Build.VERSION.SDK_INT+": "+label);
    }
    private static Dialog dialog(){return Dialog.all.get(Dialog.all.size()-1);}
    private static GmPanel panel(){return GmPanel.all.get(GmPanel.all.size()-1);}
    private static void tap(Button button){
        button.touch(MotionEvent.ACTION_DOWN,10,10);button.touch(MotionEvent.ACTION_UP,10,10);
    }
    private static void enabled(Context c,boolean value){
        c.getSharedPreferences(GmOverlay.PREFERENCES,0).edit().putBoolean("enabled",value).apply();
    }
    private static void handles(Context c,int count,String label){check(c.manager.views.size()==count,label);}
    private static void scenario(int api) {
        Build.VERSION.SDK_INT=api;ConnectionService.status.running=true;Settings.granted=true;
        GmServer.invalid=false;GmServer.origin="https://127.0.0.1:8443";
        Context context=new Context();enabled(context,true);
        GmOverlay overlay=new GmOverlay(context);overlay.refresh();
        Button button=GmUi.last;
        handles(context,1,"One connected handle");
        int type=api>=26?2038:2002;
        if(api>=30)check(context.windowContextType==type,"Window context matches overlay type");
        if(api>=33)check(context.receiverFlags==Context.RECEIVER_NOT_EXPORTED,"Private screen receiver");
        tap(button);
        check(context.activities==0,"Click must keep the foreground app and never launch an Activity");
        check(!Dialog.all.isEmpty() && dialog().showing,"Click opens a panel window");
        check(dialog().getWindow().getAttributes().type==type,"Panel has permission-bearing system type");
        check((dialog().getWindow().getAttributes().flags & (8|131072))==0,"Panel permits keyboard focus");
        check((dialog().getWindow().getAttributes().flags & 32)!=0,"Touches outside remain nonmodal");
        handles(context,0,"Handle hidden while panel open");
        Dialog first=dialog();GmPanel firstPanel=panel();
        overlay.refresh();overlay.refresh();
        check(dialog()==first && first.showing,"Polling preserves one open panel");
        overlay.configurationChanged();
        check(firstPanel.configurations==1 && first.showing,"Rotation updates panel in place");
        first.outside();
        check(!first.showing && firstPanel.disposed,"Outside touch disposes panel");
        handles(context,1,"Outside touch restores handle");
        tap(button);Dialog back=dialog();GmPanel backPanel=panel();back.dismiss();
        check(backPanel.disposed,"Back/dismiss disposes panel");
        handles(context,1,"Back/dismiss restores handle");
        tap(button);panel().close();
        handles(context,1,"Close button restores handle");
        tap(button);Dialog locked=dialog();GmPanel lockedPanel=panel();
        context.lock.locked=true;overlay.refresh();
        check(!locked.showing && lockedPanel.disposed,"Lock removes panel");
        handles(context,0,"Lock removes handle");
        context.lock.locked=false;overlay.refresh();handles(context,1,"Unlock restores handle");
        tap(button);Dialog screen=dialog();GmPanel screenPanel=panel();
        context.power.interactive=false;context.broadcast(Intent.ACTION_SCREEN_OFF);
        check(!screen.showing && screenPanel.disposed,"Screen-off broadcast removes panel immediately");
        handles(context,0,"Screen off removes handle");
        context.power.interactive=true;context.broadcast(Intent.ACTION_USER_PRESENT);
        handles(context,1,"User present restores handle");
        tap(button);Dialog denied=dialog();GmPanel deniedPanel=panel();
        Settings.granted=false;overlay.refresh();
        check(!denied.showing && deniedPanel.disposed,"Revoking permission closes panel");
        handles(context,0,"Revoking permission removes handle");
        Settings.granted=true;overlay.refresh();handles(context,1,"Regrant restores handle");
        tap(button);Dialog disabled=dialog();enabled(context,false);overlay.refresh();
        check(!disabled.showing,"Turning overlay off closes panel");
        handles(context,0,"Turning overlay off removes handle");
        enabled(context,true);overlay.refresh();
        tap(button);Dialog disconnected=dialog();ConnectionService.status.running=false;overlay.refresh();
        check(!disconnected.showing,"Disconnect closes panel");
        handles(context,0,"Disconnect removes handle");
        ConnectionService.status.running=true;overlay.refresh();
        tap(button);Dialog external=dialog();GmOverlay.panelStarted();
        check(!external.showing,"Home-screen panel replaces the overlay panel");
        handles(context,0,"Home-screen panel hides handle");
        GmOverlay.panelStopped();handles(context,1,"Home-screen panel dismissal restores handle");
        int before=Dialog.all.size(),toasts=Toast.shown;
        GmServer.invalid=true;tap(button);
        check(Dialog.all.size()==before && Toast.shown>toasts,"Missing server reports failure without a window");
        handles(context,1,"Missing server leaves handle usable");GmServer.invalid=false;
        Dialog.failNextShow=true;tap(button);
        check(!dialog().showing && panel().disposed,"Window-attach failure cleans up the panel");
        handles(context,1,"Attach failure leaves handle usable");
        GmPanel.failNextCreate=true;tap(button);
        check(!dialog().showing && panel().disposed,"Panel-construction failure cleans up");
        handles(context,1,"Construction failure leaves handle usable");
        before=Dialog.all.size();
        button.touch(MotionEvent.ACTION_DOWN,10,10);button.touch(MotionEvent.ACTION_MOVE,100,150);
        button.touch(MotionEvent.ACTION_UP,100,150);
        check(Dialog.all.size()==before,"Dragging never opens a panel");
        float saved=context.getSharedPreferences(GmOverlay.PREFERENCES,0).getFloat("portrait_x",-1);
        check(saved>=0 && saved<=1,"Drag stores normalized portrait position");
        context.resources.configuration.orientation=2;overlay.configurationChanged();
        handles(context,1,"Rotation keeps a single handle");
        check(context.getSharedPreferences(GmOverlay.PREFERENCES,0).getFloat("portrait_x",-1)==saved,
                "Rotation preserves portrait position");
        button.touch(MotionEvent.ACTION_DOWN,10,10);button.touch(MotionEvent.ACTION_CANCEL,10,10);
        check(Dialog.all.size()==before,"Cancelled touch never opens a panel");
        tap(button);Dialog oldOrigin=dialog();GmPanel oldPanel=panel();
        GmServer.origin="https://47.116.188.72";overlay.refresh();
        check(!oldOrigin.showing && oldPanel.disposed,"Server change closes the previous session panel");
        handles(context,1,"Server change restores handle");
        tap(button);check(panel().origin.equals(GmServer.origin),"Next panel uses the selected server");
        Dialog finalWindow=dialog();GmPanel finalPanel=panel();
        overlay.dispose();overlay.dispose();overlay.refresh();
        check(!finalWindow.showing && finalPanel.disposed,"Service disposal removes panel");
        handles(context,0,"Service disposal removes handle");
        check(context.receivers.isEmpty(),"Service disposal unregisters the screen receiver");
        check(Handler.all.get(Handler.all.size()-1).tasks.isEmpty(),"Service disposal cancels polling");
        before=Dialog.all.size();tap(button);
        check(Dialog.all.size()==before && context.activities==0,"Disposed handle cannot reopen UI");
    }
    public static void main(String[] args) {
        for(int api:new int[]{24,26,30,35,36})scenario(api);
        System.out.println("PASS GM overlay click/window lifecycle: "+checks+" checks across API 24/26/30/35/36 (host doubles)");
    }
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=SOURCE)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ag-overlay-tests-") as directory:
        root = Path(directory)
        paths = []
        for relative, code in {**DOUBLES, "OverlayLifecycleTest.java": SCENARIOS}.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(code, encoding="utf-8")
            paths.append(path)
        production = [args.source_dir / (name + ".java")
                      for name in ("GmOverlay", "GmGeometry", "GmOverlayPolicy")]
        try:
            subprocess.run(["javac", "--release", "8", "-encoding", "UTF-8", "-d", str(root / "classes"),
                            *map(str, paths + production)], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(error.stdout + error.stderr) from error
        subprocess.run(["java", "-cp", str(root / "classes"), "OverlayLifecycleTest"], check=True)


if __name__ == "__main__":
    main()
