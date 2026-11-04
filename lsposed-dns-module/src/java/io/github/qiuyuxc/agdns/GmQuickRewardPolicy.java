package io.github.qiuyuxc.agdns;

/** Quick actions resolve IDs from the server catalog rather than hardcoded IDs. */
public final class GmQuickRewardPolicy {
    public static final String[] COINS = {"艾因索菲币", "金币"};
    public static final String[] MATERIALS = {"神力因子", "高级经验芯片", "高级作战记录"};
    private GmQuickRewardPolicy() { }
    public static boolean preferred(String name, String[] candidates) {
        if (name == null || candidates == null) return false;
        for (String candidate : candidates) if (candidate.equals(name.trim())) return true;
        return false;
    }
    public static long amount(long requested, long maximum) {
        if (requested < 1 || maximum < 1) throw new IllegalArgumentException("该奖励当前不可发放");
        return Math.min(requested, maximum);
    }
}
