package io.github.qiuyuxc.agconnect;

import java.util.function.Consumer;

/** Denied controls explain their state without sending a gameplay request. */
public final class GmPermissionPolicy {
    private GmPermissionPolicy() { }

    public static String reason(boolean enabled, String feature, int currentLevel, int requiredLevel) {
        if (!enabled) return "子 GM 功能已停用，请联系管理员。";
        if (requiredLevel > 0 && currentLevel < requiredLevel)
            return feature + "需要账号 Lv." + requiredLevel + "，当前 Lv." + Math.max(0, currentLevel) + "。";
        return "当前账号没有" + feature + "权限，请联系管理员。";
    }

    public static void activate(boolean busy, boolean allowed, String denial,
                                Runnable action, Consumer<String> feedback) {
        if (busy) return;
        if (allowed) action.run();
        else feedback.accept(denial == null || denial.trim().isEmpty()
                ? "当前账号没有此操作权限，请联系管理员。" : denial);
    }
}
