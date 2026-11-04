package io.github.qiuyuxc.agdns;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.res.Configuration;
import android.graphics.Rect;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputFilter;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.*;
import java.text.SimpleDateFormat;
import java.util.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONArray;
import org.json.JSONObject;

/** Native centered controls; the server authorizes every gameplay operation. */
public final class SubGmActivity extends Activity implements GmUi.ThemeOwner {
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService work = Executors.newSingleThreadExecutor();
    private final List<Control> controls = new ArrayList<>();
    private GmApiClient api;
    private JSONObject account, profile, item, hero, player;
    private LinearLayout shell, page, navigation, footer;
    private ScrollView navigationScroll, statusScroll;
    private LinearLayout statusCard;
    private boolean quickRewards = true;
    private TextView footerHint;
    private Button themeButton;
    private String themeMode = "system";
    private boolean twoColumns;
    private FrameLayout content;
    private TextView identity, message;
    private ProgressBar progress;
    private Button refresh;
    private boolean busy, closed, loggingOut;
    private int generation;
    private String section = "rewards", category = "currency", itemSearch = "", playerSearch = "", amount = "100";
    private interface Done { void apply(GmApiClient.Result result) throws Exception; }
    private static final class Control {
        final View view; final boolean allowed;
        Control(View view, boolean allowed) { this.view = view; this.allowed = allowed; }
    }
    private int dp(float n) { return GmUi.dp(this, n); }
    private boolean admin() { return account != null && account.optInt("is_admin") == 1; }
    private JSONObject object(JSONObject parent, String key) {
        JSONObject value = parent == null ? null : parent.optJSONObject(key);
        return value == null ? new JSONObject() : value;
    }
    private JSONObject json(Object... pairs) {
        JSONObject value = new JSONObject();
        try { for (int i = 0; i < pairs.length; i += 2) value.put((String) pairs[i], pairs[i + 1]); }
        catch (Exception e) { throw new IllegalArgumentException(e); }
        return value;
    }
    @Override public void onCreate(Bundle state) {
        themeMode = GmThemePolicy.normalize(getSharedPreferences("agdns_gm_ui", MODE_PRIVATE).getString("theme", "system"));
        quickRewards=getSharedPreferences("agdns_gm_ui",MODE_PRIVATE).getBoolean("quick_rewards",true);
        applyActivityTheme();
        super.onCreate(state); api = new GmApiClient(this);
        getWindow().setGravity(Gravity.CENTER);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);
        getWindow().setDimAmount(.34f);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        getWindow().setBackgroundDrawable(GmUi.round(this, GmUi.surface(this), 20));
        setFinishOnTouchOutside(true); buildShell(); resizePanel();
        if (api.hasSession()) verifySession(); else showLogin("");
    }
    @Override public boolean darkTheme() { return GmThemePolicy.dark(themeMode, GmUi.systemDark(this)); }
    private void applyActivityTheme() {
        int style = getResources().getIdentifier(darkTheme() ? "SubGmDarkTheme" : "SubGmTheme", "style", getPackageName());
        if (style != 0) setTheme(style);
    }
    private void selectTheme(String choice) {
        themeMode = GmThemePolicy.normalize(choice);
        getSharedPreferences("agdns_gm_ui", MODE_PRIVATE).edit().putString("theme", themeMode).apply();
        applyActivityTheme();
        getWindow().setBackgroundDrawable(GmUi.round(this, GmUi.surface(this), 20));
        GmUi.repaint(shell);
    }
    private void resizePanel() {
        Rect area = new Rect(); getWindowManager().getDefaultDisplay().getRectSize(area);
        float density = getResources().getDisplayMetrics().density;
        int width = GmGeometry.panelWidth(area.width(), density);
        int cardWidth=profile!=null && account!=null && !account.optBoolean("password_change_required") ? GmGeometry.statusCardWidth(width,density) : 0;
        float mainWidth=width/density-(width/density<420?60:76)-cardWidth/density-32;
        twoColumns = mainWidth >= 320;
        if(statusScroll!=null){
            statusScroll.setVisibility(cardWidth>0?View.VISIBLE:View.GONE);
            LinearLayout.LayoutParams card=(LinearLayout.LayoutParams)statusScroll.getLayoutParams();card.width=cardWidth;statusScroll.setLayoutParams(card);
        }
        getWindow().setLayout(width, GmGeometry.panelHeight(area.height(), density));
        if (navigationScroll != null) {
            LinearLayout.LayoutParams params = (LinearLayout.LayoutParams) navigationScroll.getLayoutParams();
            params.width = dp(width / density < 420 ? 60 : 76); navigationScroll.setLayoutParams(params);
        }
    }
    @Override public void onConfigurationChanged(Configuration next) {
        boolean previous=twoColumns;
        super.onConfigurationChanged(next); resizePanel();
        applyActivityTheme(); GmUi.repaint(shell);
        if(previous!=twoColumns && profile!=null && "rewards".equals(section) && !busy)showRewards();
    }
    private LinearLayout column() {
        LinearLayout view = new LinearLayout(this); view.setOrientation(LinearLayout.VERTICAL); return view;
    }
    private LinearLayout row() {
        LinearLayout view = new LinearLayout(this); view.setOrientation(LinearLayout.HORIZONTAL);
        view.setGravity(Gravity.CENTER_VERTICAL); return view;
    }
    private TextView text(String value, int size, boolean muted) {
        TextView view = new TextView(this); view.setText(value); view.setTextSize(GmUi.FONT_SP); view.setTag(muted ? "gm:muted" : "gm:text");
        view.setTextColor(muted ? GmUi.muted(this) : GmUi.text(this)); view.setPadding(dp(3), dp(3), dp(3), dp(3)); return view;
    }
    private void buildShell() {
        shell = column(); shell.setPadding(dp(8), dp(6), dp(8), dp(6)); shell.setTag("gm:surface");
        shell.setBackground(GmUi.round(this, GmUi.surface(this), 20)); shell.setClipToOutline(true);
        LinearLayout bar = row();
        TextView title = text("辅助面板",11,false); title.setTypeface(null,android.graphics.Typeface.BOLD);
        bar.addView(title,new LinearLayout.LayoutParams(dp(76),dp(30)));
        identity = text("游戏账号登录",11,true); identity.setSingleLine(true);
        identity.setEllipsize(android.text.TextUtils.TruncateAt.END);
        bar.addView(identity,new LinearLayout.LayoutParams(0,dp(30),1));
        themeButton = GmUi.theme(this);
        themeButton.setOnClickListener(v -> selectTheme(GmThemePolicy.toggle(themeMode,GmUi.systemDark(this))));
        themeButton.setOnLongClickListener(v -> {
            pick("界面主题",new String[]{"日间","夜间","跟随手机"},index ->
                    selectTheme(new String[]{"light","dark","system"}[index])); return true;
        });
        LinearLayout.LayoutParams icon = new LinearLayout.LayoutParams(dp(28),dp(28)); icon.leftMargin=dp(4);
        bar.addView(themeButton,icon);
        refresh=GmUi.icon(this,false); refresh.setContentDescription("刷新账号与面板状态");
        refresh.setOnClickListener(v -> { if(!busy) { if(account==null)verifySession(); else loadProfile(); } });
        LinearLayout.LayoutParams refreshLayout=new LinearLayout.LayoutParams(dp(28),dp(28));refreshLayout.leftMargin=dp(4);
        bar.addView(refresh,refreshLayout);
        Button close=GmUi.icon(this,true);close.setContentDescription("收起辅助面板");close.setOnClickListener(v -> finish());
        LinearLayout.LayoutParams closeLayout=new LinearLayout.LayoutParams(dp(28),dp(28));closeLayout.leftMargin=dp(4);
        bar.addView(close,closeLayout);shell.addView(bar);
        progress=new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);
        progress.setIndeterminate(true);progress.setVisibility(View.GONE);shell.addView(progress,new LinearLayout.LayoutParams(-1,dp(2)));
        LinearLayout body=row();body.setGravity(Gravity.TOP);
        shell.addView(body,new LinearLayout.LayoutParams(-1,0,1));
        navigationScroll=new ScrollView(this);navigationScroll.setVerticalScrollBarEnabled(false);navigationScroll.setTag("gm:sidebar");
        navigationScroll.setBackground(GmUi.round(this,GmUi.fill(this),12));
        navigation=column();navigation.setPadding(dp(4),dp(6),dp(4),dp(6));navigationScroll.addView(navigation);
        LinearLayout.LayoutParams navLayout=new LinearLayout.LayoutParams(dp(76),-1);navLayout.topMargin=dp(5);body.addView(navigationScroll,navLayout);
        LinearLayout workspace=column();LinearLayout.LayoutParams workLayout=new LinearLayout.LayoutParams(0,-1,1);
        workLayout.leftMargin=dp(7);body.addView(workspace,workLayout);
        message=text("",11,true);message.setVisibility(View.GONE);workspace.addView(message);
        content=new FrameLayout(this);workspace.addView(content,new LinearLayout.LayoutParams(-1,0,1));
        footer=row();workspace.addView(footer,new LinearLayout.LayoutParams(-1,dp(34)));
        statusScroll=new ScrollView(this);statusScroll.setVerticalScrollBarEnabled(false);
        statusCard=column();statusCard.setTag("gm:sidebar");statusCard.setPadding(dp(8),dp(7),dp(8),dp(7));
        statusCard.setBackground(GmUi.round(this,GmUi.fill(this),12));statusScroll.addView(statusCard);
        LinearLayout.LayoutParams card=new LinearLayout.LayoutParams(dp(118),-1);card.leftMargin=dp(7);card.topMargin=dp(5);
        body.addView(statusScroll,card);statusScroll.setVisibility(View.GONE);
        setContentView(shell);
    }
    private void startPage(String title) {
        controls.clear(); content.removeAllViews(); footer.removeAllViews();
        footerHint=text("当前账号",11,true); footerHint.setSingleLine(true); footerHint.setEllipsize(android.text.TextUtils.TruncateAt.END);
        footer.addView(footerHint,new LinearLayout.LayoutParams(0,dp(30),1));
        ScrollView scroll = new ScrollView(this); scroll.setFillViewport(true);
        page = column(); page.setPadding(dp(3), dp(5), dp(3), dp(5)); scroll.addView(page);
        content.addView(scroll, new FrameLayout.LayoutParams(-1, -1)); TextView heading=text(title,11,false);heading.setTypeface(null,android.graphics.Typeface.BOLD);page.addView(heading);
    }
    private void note(String value) { page.addView(text(value, 13, true)); }
    private Button button(LinearLayout parent, String title, boolean primary, boolean allowed, Runnable action) {
        return button(parent, title, primary, allowed, "当前账号没有此操作权限，请联系管理员。", action);
    }
    private Button button(LinearLayout parent, String title, boolean primary, boolean allowed, String denial, Runnable action) {
        Button view = GmUi.button(this, title, primary); view.setEnabled(!busy); view.setAlpha(allowed ? 1f : .55f);
        boolean horizontal = parent.getOrientation() == LinearLayout.HORIZONTAL;
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(horizontal ? 0 : -1, dp(28));
        if (horizontal) params.weight = 1;
        params.setMargins(dp(2), dp(3), dp(2), dp(3)); parent.addView(view, params);
        controls.add(new Control(view, allowed));
        view.setOnClickListener(v -> GmPermissionPolicy.activate(busy, allowed, denial, action, this::notifyFailure));
        return view;
    }
    private EditText input(LinearLayout parent, String hint, String value, boolean password, boolean numeric) {
        EditText view = new EditText(this); view.setSingleLine(true); view.setTextSize(GmUi.FONT_SP);
        view.setHint(hint); view.setText(value); view.setTextColor(GmUi.text(this)); view.setHintTextColor(GmUi.muted(this));
        view.setPadding(dp(9), dp(3), dp(9), dp(3)); view.setBackground(GmUi.round(this, GmUi.fill(this), 10));
        view.setInputType(password ? InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
                : numeric ? InputType.TYPE_CLASS_NUMBER : InputType.TYPE_CLASS_TEXT);
        view.setFilters(new InputFilter[]{new InputFilter.LengthFilter(password ? 64 : 100)});
        if (password) view.setSaveEnabled(false);
        boolean horizontal = parent.getOrientation() == LinearLayout.HORIZONTAL;
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(horizontal ? 0 : -1, dp(30));
        if (horizontal) params.weight = 1;
        params.setMargins(dp(2), dp(3), dp(2), dp(3)); parent.addView(view, params); return view;
    }
    private Switch toggle(String label, boolean selected) {
        Switch view = new Switch(this); view.setText(label); view.setTextColor(GmUi.text(this)); view.setTextSize(GmUi.FONT_SP); view.setChecked(selected);
        view.setPadding(dp(3), dp(4), dp(3), dp(4)); page.addView(view); GmUi.repaint(view); return view;
    }
    private Button footerButton(String title,boolean primary,boolean enabled,Runnable action) {
        return footerButton(title, primary, enabled, "当前账号没有此操作权限，请联系管理员。", action);
    }
    private Button footerButton(String title,boolean primary,boolean enabled,String denial,Runnable action) {
        Button view=button(footer,title,primary,enabled,denial,action);
        LinearLayout.LayoutParams params=(LinearLayout.LayoutParams)view.getLayoutParams();
        params.width=dp(92);params.weight=0;view.setLayoutParams(params);return view;
    }
    private interface Pick { void choose(int index); }
    private void styleDialog(AlertDialog dialog) {
        if(dialog.getWindow()!=null)dialog.getWindow().setBackgroundDrawable(GmUi.round(this,GmUi.surface(this),18));
        for(int key:new int[]{AlertDialog.BUTTON_POSITIVE,AlertDialog.BUTTON_NEGATIVE}) {
            Button button=dialog.getButton(key);
            if(button!=null){button.setTextSize(GmUi.FONT_SP);button.setTextColor(GmUi.accent(this));button.setMinimumHeight(dp(28));button.setMinHeight(dp(28));}
        }
    }
    private void pick(String title,String[] labels,Pick selected) {
        ArrayAdapter<String> rows=new ArrayAdapter<String>(this,android.R.layout.simple_list_item_1,labels) {
            @Override public View getView(int position,View reused,android.view.ViewGroup parent) {
                TextView view=(TextView)super.getView(position,reused,parent);view.setTextSize(GmUi.FONT_SP);
                view.setTextColor(GmUi.text(SubGmActivity.this));view.setPadding(dp(12),dp(9),dp(12),dp(9));return view;
            }
        };
        TextView heading=text(title,11,false);heading.setTypeface(null,android.graphics.Typeface.BOLD);heading.setPadding(dp(12),dp(12),dp(12),dp(5));
        AlertDialog dialog=new AlertDialog.Builder(this).setCustomTitle(heading).setAdapter(rows,(d,index)->selected.choose(index))
                .setNegativeButton("取消",null).create();dialog.show();styleDialog(dialog);
    }
    private void setBusy(boolean value) {
        busy = value; progress.setVisibility(value ? View.VISIBLE : View.GONE); refresh.setEnabled(!value);
        for (Control control : controls) {
            control.view.setEnabled(!value);
            control.view.setAlpha(value || !control.allowed ? .55f : 1f);
        }
        for (int i = 0; i < navigation.getChildCount(); i++) navigation.getChildAt(i).setEnabled(!value);
    }
    private void say(String value) { message.setText(value); message.setVisibility(value.isEmpty() ? View.GONE : View.VISIBLE); }
    private void notifyFailure(String value) { say(value); Toast.makeText(this, value, Toast.LENGTH_LONG).show(); }
    private void clearSession(String value) { api.clear(); account = null; profile = null; item = null; hero = null; player = null; showLogin(value); }
    private void fail(Exception error) {
        if (loggingOut) { loggingOut = false; clearSession("已退出本机登录。"); return; }
        if (error instanceof GmApiClient.Failure && ((GmApiClient.Failure) error).status == 401 && (account != null || api.hasSession()))
            clearSession(error.getMessage());
        else notifyFailure(error.getMessage() == null ? "操作未完成，请重试" : error.getMessage());
    }
    private void call(String method, String path, JSONObject body, Done done) {
        if (busy || closed) return;
        setBusy(true); say(""); final int ticket = ++generation;
        work.execute(() -> {
            GmApiClient.Result result = null; Exception error = null;
            try { result = api.request(method, path, body); } catch (Exception failure) { error = failure; }
            final GmApiClient.Result value = result; final Exception failure = error;
            main.post(() -> {
                if (closed || isFinishing() || ticket != generation) return;
                setBusy(false); if (failure != null) { fail(failure); return; }
                try { done.apply(value); } catch (Exception problem) { fail(problem); }
            });
        });
    }
    private void verifySession() {
        if (!api.hasSession()) { showLogin(""); return; }
        startPage("正在验证登录…");
        call("GET", "/api/auth/me", null, result -> {
            account = result.object();
            if (account.optBoolean("password_change_required")) showPassword(true); else loadProfile();
        });
    }
    private void showLogin(String hint) {
        navigation.removeAllViews(); updateStatusCard(); identity.setText("游戏账号登录 · 奖励发给当前账号"); startPage("登录游戏账号");
        EditText username = input(page, "数字账号", "", false, true);
        username.setFilters(new InputFilter[]{new InputFilter.LengthFilter(16)});
        EditText password = input(page, "密码 / 首次注册的 8 位注册码", "", true, false);
        note("验证码注册的新号，首次用注册码登录后设置新密码。");
        footerButton("登录", true, true, () -> {
            String name = username.getText().toString(), secret = password.getText().toString();
            call("POST", "/api/auth/login", json("username", name, "password", secret), result -> {
                JSONObject signed = result.object();
                if (signed.optLong("uid") <= 0 || !name.equals(signed.optString("username")))
                    throw new GmApiClient.Failure(0, "服务器返回的账号信息不一致");
                api.remember(signed.optString("token")); account = signed; password.setText("");
                if (account.optBoolean("password_change_required")) showPassword(true); else loadProfile();
            });
        }); say(hint);
    }
    private void loadProfile() {
        if (account == null) { verifySession(); return; }
        if (account.optBoolean("password_change_required")) { showPassword(true); return; }
        call("GET", "/api/sub-gm/me", null, result -> {
            JSONObject next = result.object();
            if (object(next, "account").optLong("uid") != account.optLong("uid"))
                throw new GmApiClient.Failure(0, "服务器返回的存档不属于当前账号");
            profile = next; account = next.getJSONObject("account");
            JSONObject game = object(profile, "game");
            identity.setText(game.optString("nick", account.optString("username")) + " · Lv." + game.optInt("level", 1)
                    + " · " + (profile.optBoolean("online") ? "游戏在线" : "游戏离线"));
            updateStatusCard(); renderNavigation(); showSection(section);
        });
    }
    private void updateStatusCard() {
        if(statusCard==null)return;
        statusCard.removeAllViews();
        if(profile==null || account==null){resizePanel();return;}
        JSONObject game=object(profile,"game");
        TextView heading=text("当前账号",11,false);heading.setTypeface(null,android.graphics.Typeface.BOLD);statusCard.addView(heading);
        TextView name=text(game.optString("nick",account.optString("username")),11,false);
        name.setMaxLines(2);name.setEllipsize(android.text.TextUtils.TruncateAt.END);statusCard.addView(name);
        statusCard.addView(text("等级  Lv."+game.optInt("level",1),11,true));
        TextView online=text(profile.optBoolean("online")?"游戏在线":"游戏离线",11,true);
        if(profile.optBoolean("online")){online.setTag("gm:online");online.setTextColor(darkTheme()?0xff77cdb5:0xff309782);}
        statusCard.addView(online);
        TextView permissions=text("权限等级",11,false);permissions.setTypeface(null,android.graphics.Typeface.BOLD);
        permissions.setPadding(dp(3),dp(8),dp(3),dp(3));statusCard.addView(permissions);
        JSONObject rules=object(profile,"permissions");
        statusCard.addView(text("货币 / 材料  "+object(rules,"currency").optInt("level")+" / "+object(rules,"materials").optInt("level"),11,true));
        statusCard.addView(text("角色 / 装备  "+object(rules,"heroes").optInt("level")+" / "+object(rules,"equipment").optInt("level"),11,true));
        statusCard.addView(text(admin()?"管理员":"仅操作当前账号",11,true));resizePanel();
    }
    private void rewardMode(boolean quick) {
        quickRewards=quick;getSharedPreferences("agdns_gm_ui",MODE_PRIVATE).edit().putBoolean("quick_rewards",quick).apply();showRewards();
    }
    private void renderNavigation() {
        navigation.removeAllViews();
        String[][] entries = admin() ? new String[][]{{"rewards","奖励"},{"heroes","角色"},{"players","玩家"},{"tools","管理"},{"account","账号"}}
                : new String[][]{{"rewards","奖励"},{"heroes","角色"},{"account","账号"}};
        for (String[] entry : entries) {
            Button tab = GmUi.navigation(this, entry[1], entry[0].equals(section));
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(30)); params.setMargins(0, dp(3), 0, dp(3));
            navigation.addView(tab, params);
            tab.setOnClickListener(v -> { if (!busy) { section = entry[0]; renderNavigation(); showSection(section); } });
        }
    }
    private void showSection(String name) {
        if (profile == null) return; section = name;
        if ("rewards".equals(name)) showRewards();
        else if ("heroes".equals(name)) showHeroes();
        else if ("players".equals(name) && admin()) showPlayers();
        else if ("tools".equals(name) && admin()) showTools();
        else showAccount();
    }
    private boolean allowed(String key) { return object(object(profile, "permissions"), key).optBoolean("allowed"); }
    private String deniedPermission(String key) {
        JSONObject rule = object(object(profile, "permissions"), key);
        return GmPermissionPolicy.reason(profile != null && profile.optBoolean("enabled"),
                rule.optString("label", "此功能"), object(profile, "game").optInt("level"), rule.optInt("level"));
    }
    private String permission(String key) {
        JSONObject value = object(object(profile, "permissions"), key);
        return value.optString("label", "此功能") + " · Lv." + value.optInt("level") + (value.optBoolean("allowed") ? " 已开放" : " 开放");
    }
    private void showRewards() {
        startPage("奖励与养成");LinearLayout modes=row();page.addView(modes);
        Button quick=button(modes,"快捷",false,true,()->rewardMode(true));
        Button custom=button(modes,"自定义",false,true,()->rewardMode(false));
        quick.setTag(quickRewards?"gm:nav-active":"gm:nav");custom.setTag(!quickRewards?"gm:nav-active":"gm:nav");
        GmUi.repaint(quick);GmUi.repaint(custom);
        if(!quickRewards){showCustomRewards();return;}
        LinearLayout top=row();page.addView(top);
        quickTile(top,"金币","10,000 个",allowed("currency"),deniedPermission("currency"),()->quickGrant("currency",10000,GmQuickRewardPolicy.COINS,"金币"));
        quickTile(top,"养成材料","1,000 个",allowed("materials"),deniedPermission("materials"),()->quickGrant("materials",1000,GmQuickRewardPolicy.MATERIALS,"养成材料"));
        LinearLayout middle=row();page.addView(middle);
        quickTile(middle,"解锁角色","选择目标角色",allowed("heroes"),deniedPermission("heroes"),()->jump("heroes"));
        quickTile(middle,"角色培养","等级与养成",allowed("heroes"),deniedPermission("heroes"),()->jump("heroes"));
        LinearLayout bottom=row();page.addView(bottom);
        quickTile(bottom,"自定义奖励","物品与数量",true,()->rewardMode(false));
        quickTile(bottom,"账号设置","密码与权限",true,()->jump("account"));
        footerHint.setText("快捷奖励发给当前账号");footerButton("刷新状态",false,true,this::loadProfile);
    }
    private void quickTile(LinearLayout parent,String title,String hint,boolean enabled,Runnable action) {
        quickTile(parent,title,hint,enabled,"当前账号没有此操作权限，请联系管理员。",action);
    }
    private void quickTile(LinearLayout parent,String title,String hint,boolean enabled,String denial,Runnable action) {
        Button tile=button(parent,title+"\n"+hint,false,enabled,denial,action);tile.setTag("gm:quick");tile.setMaxLines(2);
        LinearLayout.LayoutParams params=(LinearLayout.LayoutParams)tile.getLayoutParams();params.height=dp(46);tile.setLayoutParams(params);
    }
    private void jump(String target){section=target;renderNavigation();showSection(target);}
    private void quickGrant(String kind,long requested,String[] names,String title) {
        call("GET","/api/sub-gm/items?category="+kind,null,result->{
            JSONArray choices=result.object().getJSONArray("items");JSONObject found=null;
            for(String preferred:names){
                for(int i=0;i<choices.length();i++){
                    JSONObject candidate=choices.getJSONObject(i);
                    if(candidate.optLong("id")>0 && GmQuickRewardPolicy.preferred(candidate.optString("name"),new String[]{preferred})){
                        found=candidate;break;
                    }
                }if(found!=null)break;
            }
            if(found!=null){sendQuickReward(kind,found,requested);return;}
            if(choices.length()==0){say("没有可快捷发放的"+title+"，请切换自定义搜索。");return;}
            String[] labels=new String[choices.length()];
            for(int i=0;i<labels.length;i++)labels[i]=choices.getJSONObject(i).optString("name");
            pick("选择快捷发放物品",labels,index->sendQuickReward(kind,choices.optJSONObject(index),requested));
        });
    }
    private void sendQuickReward(String kind,JSONObject selected,long requested) {
        try{
            long number=GmQuickRewardPolicy.amount(requested,object(profile,"limits").optLong(kind,0));
            long id=selected.getLong("id");if(id<=0)throw new IllegalArgumentException("物品编号无效");
            category=kind;item=selected;amount=String.valueOf(number);String label=selected.optString("name");
            call("POST","/api/sub-gm/grant",json("item_id",id,"count",number),result->{
                JSONObject sync=object(result.object(),"sync");
                say("已发放 "+label+" × "+number+(sync.optInt("clients")>0?"，已同步游戏。":"，重进游戏后可查看。"));
            });
        }catch(Exception error){fail(error);}
    }
    private void showCustomRewards() {
        LinearLayout categories=row();page.addView(categories);
        for(String[] value:new String[][]{{"currency","货币"},{"materials","材料"},{"heroes","角色"},{"equipment","装备"}})
            button(categories,value[1],category.equals(value[0]),true,()->{
                category=value[0];item=null;amount=("heroes".equals(category)||"equipment".equals(category))?"1":"100";showRewards();
            });
        LinearLayout grid=twoColumns?row():column();grid.setGravity(Gravity.TOP);page.addView(grid);
        LinearLayout selection=column(),quantities=column();
        if(twoColumns){
            grid.addView(selection,new LinearLayout.LayoutParams(0,-2,1.4f));
            LinearLayout.LayoutParams right=new LinearLayout.LayoutParams(0,-2,1);right.leftMargin=dp(10);grid.addView(quantities,right);
        }else{grid.addView(selection);grid.addView(quantities);}
        selection.addView(text("选择物品",11,true));LinearLayout search=row();selection.addView(search);
        EditText query=input(search,"物品名称或编号",itemSearch,false,false);
        query.addTextChangedListener(new android.text.TextWatcher(){
            public void beforeTextChanged(CharSequence s,int start,int count,int after){}
            public void onTextChanged(CharSequence s,int start,int before,int count){itemSearch=s.toString();}
            public void afterTextChanged(android.text.Editable value){}
        });
        Runnable choose=()->{
            itemSearch=query.getText().toString();
            call("GET","/api/sub-gm/items?category="+category+"&search="+GmApiClient.encode(itemSearch),null,
                    result->chooseItems(result.object().getJSONArray("items")));
        };
        Button searchButton=button(search,"搜索",false,allowed(category),deniedPermission(category),choose);
        LinearLayout.LayoutParams searchLayout=(LinearLayout.LayoutParams)searchButton.getLayoutParams();searchLayout.width=dp(48);searchLayout.weight=0;searchButton.setLayoutParams(searchLayout);
        button(selection,item==null?"选择物品":item.optString("name"),false,allowed(category),deniedPermission(category),choose);
        selection.addView(text(permission(category),11,true));
        quantities.addView(text("发放数量",11,true));EditText count=input(quantities,"数量",amount,false,true);
        count.addTextChangedListener(new android.text.TextWatcher(){
            public void beforeTextChanged(CharSequence s,int start,int c,int after){}
            public void onTextChanged(CharSequence s,int start,int before,int c){amount=s.toString();}
            public void afterTextChanged(android.text.Editable value){}
        });
        LinearLayout presets=row();quantities.addView(presets);
        String[] choices=("currency".equals(category)||"materials".equals(category))?new String[]{"100","1000","10000"}
                :"heroes".equals(category)?new String[]{"1"}:new String[]{"1","10"};
        for(String value:choices)button(presets,value,false,true,()->count.setText(value));
        long limit=object(profile,"limits").optLong(category,1);
        quantities.addView(text("每次最多 "+limit+" 个",11,true));
        footerHint.setText("奖励发给当前账号");
        footerButton("发放奖励",true,allowed(category)&&item!=null,
                !allowed(category)?deniedPermission(category):"请先选择要发放的物品。",()->{
            try{
                amount=count.getText().toString();long number=Long.parseLong(amount);
                if(number<1||number>limit)throw new IllegalArgumentException("数量须在 1–"+limit+" 之间");
                long id=item.getLong("id");String name=item.optString("name");
                call("POST","/api/sub-gm/grant",json("item_id",id,"count",number),result->{
                    JSONObject sync=object(result.object(),"sync");
                    say("已发放 "+name+" × "+number+(sync.optInt("clients")>0?"，已同步游戏。":"，重进游戏后可查看。"));
                });
            }catch(Exception error){fail(error);}
        });
    }
    private void chooseItems(JSONArray choices) throws Exception {
        if (choices.length() == 0) { say("没有匹配的可发放物品，请换一个搜索词。"); return; }
        String[] labels = new String[choices.length()];
        for (int i = 0; i < labels.length; i++) labels[i] = choices.getJSONObject(i).optString("name") + " · " + choices.getJSONObject(i).optLong("id");
        pick("选择物品",labels,index->{item=choices.optJSONObject(index);showRewards();});
    }
    private void showHeroes() {
        startPage("角色解锁与培养"); note(permission("heroes")); JSONArray choices = profile.optJSONArray("heroes");
        if (choices == null || choices.length() == 0) { note("当前没有可操作的角色。"); return; }
        if (hero != null) {
            long id = hero.optLong("id"); hero = null;
            for (int i = 0; i < choices.length(); i++) if (choices.optJSONObject(i).optLong("id") == id) hero = choices.optJSONObject(i);
        }
        if (hero == null) hero = choices.optJSONObject(0);
        button(page, hero.optString("name"), false, allowed("heroes"), deniedPermission("heroes"), () -> {
            String[] labels = new String[choices.length()];
            for (int i = 0; i < labels.length; i++) {
                JSONObject h = choices.optJSONObject(i); labels[i] = h.optString("name") + (h.optBoolean("owned") ? " · 已拥有" : " · 未解锁");
            }
            pick("选择角色",labels,index->{hero=choices.optJSONObject(index);showHeroes();});
        });
        note(hero.optBoolean("owned") ? "已拥有 · Lv." + hero.optInt("level") : "尚未解锁");
        LinearLayout actions = row(); page.addView(actions);
        button(actions,"解锁角色",true,allowed("heroes") && !hero.optBoolean("owned"),
                !allowed("heroes")?deniedPermission("heroes"):"当前账号已经拥有此角色。", () ->
                call("POST","/api/sub-gm/hero/unlock",json("hero_id",hero.optLong("id")),result -> { toast("角色已解锁"); loadProfile(); }));
        button(actions,"一键培养",true,allowed("heroes") && hero.optBoolean("owned"),
                !allowed("heroes")?deniedPermission("heroes"):"请先解锁此角色，再进行培养。", () ->
                call("POST","/api/sub-gm/hero/graduate",json("hero_id",hero.optLong("id")),result -> { toast("角色培养已完成"); loadProfile(); }));
    }
    private void showAccount() {
        startPage("当前游戏账号"); note("数字账号 " + account.optString("username") + " · UID " + account.optLong("uid"));
        for (String key : new String[]{"currency","materials","heroes","equipment"}) note(permission(key));
        button(page,"修改密码",true,true,() -> showPassword(false)); button(page,"退出登录",false,true,this::logout);
    }
    private void showPassword(boolean required) {
        if (required) navigation.removeAllViews();
        identity.setText("游戏账号 " + account.optString("username")); startPage(required ? "设置新密码" : "修改密码");
        EditText previous = input(page,required ? "本地注册码（初始密码）" : "原密码","",true,false);
        EditText next = input(page,"新密码：8–64 个字符","",true,false), confirm = input(page,"确认新密码","",true,false);
        note(required ? "注册已完成，设置新密码后即可登录游戏。" : "修改后网页和游戏需使用新密码重新登录。");
        footerButton("保存密码",true,true,() -> {
            String old = previous.getText().toString(), value = next.getText().toString();
            if (value.length() < 8 || value.length() > 64) { say("新密码须为 8–64 个字符。"); return; }
            if (!value.equals(confirm.getText().toString())) { say("两次输入的新密码不一致。"); return; }
            if (old.equals(value)) { say("新密码须与原密码不同。"); return; }
            call("POST","/api/auth/password",json("old_password",old,"password",value),result -> {
                previous.setText(""); next.setText(""); confirm.setText(""); clearSession("密码已修改，请用新密码重新登录网页和游戏。");
            });
        });
        if (required) button(page,"退出登录",false,true,this::logout); else button(page,"返回账号页",false,true,this::showAccount);
    }
    private void logout() {
        loggingOut = true;
        call("POST","/api/auth/logout",json(),result -> { loggingOut = false; clearSession("已退出登录。"); });
    }
    private String playerLabel(JSONObject value) {
        return value.optString("nick","玩家") + " · " + (value.isNull("username") ? String.valueOf(value.optLong("uid")) : value.optString("username"));
    }
    private void showPlayers() {
        startPage("玩家状态与管理"); LinearLayout search = row(); page.addView(search);
        EditText query = input(search,"昵称、账号或 UID",playerSearch,false,false);
        Runnable choose = () -> { playerSearch = query.getText().toString(); loadPlayers(); };
        button(search,"查询",false,true,choose); button(page,player == null ? "选择玩家" : playerLabel(player),false,true,choose);
        if (player == null) { note("在线状态按实际游戏连接显示。"); return; }
        note("UID " + player.optLong("uid") + " · Lv." + player.optInt("level",1) + " · "
                + (player.optBoolean("online") ? "在线（" + player.optInt("connection_count") + " 个连接）" : "离线")
                + (player.optInt("disabled") != 0 ? " · 已封禁" : ""));
        LinearLayout actions = row(); page.addView(actions);
        button(actions,"踢下线",false,true,() -> confirm("踢下线","将断开 " + playerLabel(player) + " 的游戏连接。",() ->
                call("POST","/api/gm/accounts/kick",json("uid",player.optLong("uid")),result -> { toast("已断开游戏连接"); reloadPlayer(); })));
        boolean banned = player.optInt("disabled") != 0;
        button(actions,banned ? "解封" : "封禁",false,player.optInt("is_admin") != 1 && !player.isNull("username"),
                player.optInt("is_admin")==1?"不能在此处封禁管理员账号。":"该玩家尚未绑定登录账号，无法封禁或解封。",() ->
                confirm(banned ? "解封账号" : "封禁账号",playerLabel(player),() ->
                    call("POST","/api/gm/accounts/disabled",json("uid",player.optLong("uid"),"disabled",!banned),
                            result -> { toast(banned ? "账号已解封" : "账号已封禁"); reloadPlayer(); })));
    }
    private void loadPlayers() {
        call("GET","/api/gm/accounts",null,result -> {
            JSONArray all = result.array(); List<JSONObject> matches = new ArrayList<>(); List<String> labels = new ArrayList<>();
            String filter = playerSearch.toLowerCase(Locale.ROOT);
            for (int i = 0; i < all.length(); i++) {
                JSONObject value = all.getJSONObject(i);
                if ((playerLabel(value) + " " + value.optLong("uid")).toLowerCase(Locale.ROOT).contains(filter)) {
                    matches.add(value); labels.add((value.optBoolean("online") ? "[在线] " : "[离线] ") + playerLabel(value));
                }
            }
            if (matches.isEmpty()) { say("没有匹配的玩家。"); return; }
            pick("选择玩家",labels.toArray(new String[0]),index->{player=matches.get(index);showPlayers();});
        });
    }
    private void reloadPlayer() {
        long selected = player.optLong("uid");
        call("GET","/api/gm/accounts",null,result -> {
            JSONArray all = result.array(); player = null;
            for (int i = 0; i < all.length(); i++) if (all.getJSONObject(i).optLong("uid") == selected) player = all.getJSONObject(i);
            showPlayers();
        });
    }
    private void showTools() {
        startPage("管理工具");
        button(page,"卡池轮换",false,true,this::showRotation);
        button(page,"商店补货",false,true,this::showRestock);
        button(page,"子 GM 权限等级",false,true,this::showPolicy);
        button(page,"在浏览器打开完整管理后台",false,true,() -> {
            try { startActivity(new Intent(Intent.ACTION_VIEW,Uri.parse(api.origin + "/web/gm_console.html"))); }
            catch (RuntimeException e) { say("未找到可用浏览器。"); }
        });
    }
    private void showRotation() {
        call("GET","/api/gm/automation/status",null,result -> {
            JSONObject settings = result.object().getJSONObject("settings").getJSONObject("rotation"); startPage("卡池自动轮换");
            Switch enabled = toggle("启用自动轮换",settings.optBoolean("enabled"));
            Switch random = toggle("随机选择卡池","random".equals(settings.optString("mode")));
            EditText days = input(page,"轮换周期（天）",String.valueOf(settings.optInt("duration_days",7)),false,true);
            EditText groups = input(page,"每次随机主题组数",String.valueOf(settings.optInt("random_group_count",1)),false,true);
            note("常驻池、候选范围和已有顺序轮换表保留当前配置。");
            button(page,"保存轮换设置",true,true,() -> {
                try {
                    call("POST","/api/gm/automation/schedule",json("rotation",json("enabled",enabled.isChecked(),
                            "mode",random.isChecked() ? "random" : "sequence","duration_days",number(days,1,365),"random_group_count",number(groups,1,10))),
                            saved -> { toast("轮换设置已保存"); showTools(); });
                } catch (Exception e) { fail(e); }
            }); button(page,"返回管理工具",false,true,this::showTools);
        });
    }
    private void showRestock() {
        call("GET","/api/gm/shop/restock/status",null,result -> {
            JSONObject data = result.object(), settings = data.getJSONObject("settings"); startPage("商店定时补货");
            Switch enabled = toggle("启用定时补货",settings.optBoolean("enabled"));
            EditText clock = input(page,"服务器时间 HH:mm",settings.optString("time","05:00"),false,false);
            EditText days = input(page,"补货间隔（天）",String.valueOf(settings.optInt("interval_days",1)),false,true);
            JSONArray scope = settings.optJSONArray("shop_ids"); StringBuilder ids = new StringBuilder();
            if (scope != null) for (int i = 0; i < scope.length(); i++) { if (i > 0) ids.append(','); ids.append(scope.optLong(i)); }
            EditText stores = input(page,"商店编号，逗号分隔；留空为全部周期商店",ids.toString(),false,false);
            long next = data.optLong("next_restock_ts");
            SimpleDateFormat date = new SimpleDateFormat("MM-dd HH:mm",Locale.CHINA);
            date.setTimeZone(new SimpleTimeZone(data.optInt("utc_offset_seconds",28800) * 1000,"server"));
            note(next > 0 ? "下次补货：" + date.format(new Date(next * 1000)) + "（服务器时间）" : "当前未启用定时补货");
            button(page,"保存补货设置",true,true,() -> {
                try {
                    JSONArray selected = new JSONArray(); String list = stores.getText().toString().trim();
                    if (!list.isEmpty()) for (String id : list.split(",")) {
                        long value = Long.parseLong(id.trim()); if (value <= 0) throw new IllegalArgumentException("商店编号须为正整数"); selected.put(value);
                    }
                    call("POST","/api/gm/shop/restock/settings",json("enabled",enabled.isChecked(),"time",clock.getText().toString().trim(),
                            "interval_days",number(days,1,365),"shop_ids",selected),saved -> { toast("补货设置已保存"); showTools(); });
                } catch (Exception e) { fail(e); }
            });
            button(page,"立即补货",false,true,() -> confirm("立即补货","按当前范围为周期商店补货，确认执行？",() ->
                    call("POST","/api/gm/shop/restock/run",json(),saved -> { toast("商店已补货"); showTools(); })));
            button(page,"返回管理工具",false,true,this::showTools);
        });
    }
    private void showPolicy() {
        call("GET","/api/gm/sub-gm/policy",null,result -> {
            JSONObject values = result.object(); startPage("子 GM 权限等级");
            Switch enabled = toggle("开放玩家子 GM",values.optBoolean("enabled"));
            String[] keys = {"currency_level","material_level","hero_level","equipment_level"};
            String[] names = {"货币奖励等级","材料与养成等级","角色解锁培养等级","装备与钥从等级"};
            EditText[] fields = new EditText[4];
            for (int i = 0; i < fields.length; i++) { note(names[i]); fields[i] = input(page,names[i],String.valueOf(values.optInt(keys[i])),false,true); }
            button(page,"保存权限等级",true,true,() -> {
                try {
                    JSONObject policy = json("enabled",enabled.isChecked());
                    for (int i = 0; i < fields.length; i++) policy.put(keys[i],number(fields[i],1,200));
                    call("POST","/api/gm/sub-gm/policy",policy,saved -> { toast("权限等级已更新"); loadProfile(); });
                } catch (Exception e) { fail(e); }
            }); button(page,"返回管理工具",false,true,this::showTools);
        });
    }
    private int number(EditText field, int min, int max) {
        int value;
        try { value = Integer.parseInt(field.getText().toString()); }
        catch (NumberFormatException e) { throw new IllegalArgumentException("请输入 " + min + "–" + max + " 之间的整数"); }
        if (value < min || value > max) throw new IllegalArgumentException("请输入 " + min + "–" + max + " 之间的整数");
        return value;
    }
    private void confirm(String title,String description,Runnable action) {
        TextView heading=text(title,11,false);heading.setPadding(dp(12),dp(12),dp(12),dp(5));
        TextView info=text(description,11,true);info.setPadding(dp(12),dp(4),dp(12),dp(8));
        AlertDialog dialog=new AlertDialog.Builder(this).setCustomTitle(heading).setView(info).setNegativeButton("取消",null)
                .setPositiveButton("确认",(d,index)->{if(!busy)action.run();}).create();
        dialog.show();styleDialog(dialog);
    }
    private void toast(String value) { Toast.makeText(this,value,Toast.LENGTH_SHORT).show(); }
    @Override public void onBackPressed() { finish(); }
    @Override protected void onDestroy() { closed = true; generation++; work.shutdownNow(); main.removeCallbacksAndMessages(null); super.onDestroy(); }
}
