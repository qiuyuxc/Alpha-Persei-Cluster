package io.github.qiuyuxc.agdns;

/** Manual selection takes precedence over changes to the phone's theme. */
public final class GmThemePolicy {
    private GmThemePolicy() { }
    public static String normalize(String mode) {
        return "light".equals(mode) || "dark".equals(mode) ? mode : "system";
    }
    public static boolean dark(String mode, boolean systemDark) {
        String choice = normalize(mode);
        return "dark".equals(choice) || ("system".equals(choice) && systemDark);
    }
    public static String toggle(String mode, boolean systemDark) {
        return dark(mode, systemDark) ? "light" : "dark";
    }
}
