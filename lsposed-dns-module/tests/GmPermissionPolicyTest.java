import io.github.qiuyuxc.agdns.GmPermissionPolicy;

public final class GmPermissionPolicyTest {
    public static void main(String[] args) {
        int[] actions = {0}, notices = {0};
        String[] message = {""};
        java.util.function.Consumer<String> feedback = value -> { notices[0]++; message[0] = value; };
        Runnable action = () -> actions[0]++;
        GmPermissionPolicy.activate(false, false, "需要 Lv.40", action, feedback);
        check(actions[0] == 0 && notices[0] == 1 && message[0].equals("需要 Lv.40"));
        GmPermissionPolicy.activate(false, true, "禁止", action, feedback);
        check(actions[0] == 1 && notices[0] == 1);
        GmPermissionPolicy.activate(true, true, "禁止", action, feedback);
        GmPermissionPolicy.activate(true, false, "禁止", action, feedback);
        check(actions[0] == 1 && notices[0] == 1);
        GmPermissionPolicy.activate(false, false, "", action, feedback);
        check(actions[0] == 1 && message[0].contains("没有此操作权限"));
        check(GmPermissionPolicy.reason(true, "角色培养", 11, 40).equals("角色培养需要账号 Lv.40，当前 Lv.11。"));
        check(GmPermissionPolicy.reason(true, "装备", 60, 80).contains("Lv.80"));
        check(GmPermissionPolicy.reason(false, "货币", 100, 10).contains("已停用"));
        check(GmPermissionPolicy.reason(true, "货币", 100, 10).contains("没有货币权限"));
        check(GmPermissionPolicy.reason(true, "奖励", 0, 0).contains("没有奖励权限"));
        System.out.println("GM permission feedback: 9 cases passed");
    }
    private static void check(boolean condition) { if (!condition) throw new AssertionError(); }
}
