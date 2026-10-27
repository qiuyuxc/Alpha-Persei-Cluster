package io.github.qiuyuxc.agdns;

import java.util.Locale;

public final class DnsPolicy {
    public static final String GAME = "com.yongshi.tenojo.ys";
    private static final String[] DOMAINS = {"open.ys4fun.com", "prod-api-activity.ys4fun.com"};

    private DnsPolicy() {}

    public static boolean matches(String host) {
        if (host == null || host.isEmpty() || host.length() > 254) return false;
        String name = host.endsWith(".") ? host.substring(0, host.length() - 1) : host;
        if (name.isEmpty() || name.charAt(0) == '.' || name.endsWith(".") || name.contains("..")) return false;
        for (int i = 0; i < name.length(); i++) {
            char c = name.charAt(i);
            if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
                    || (c >= '0' && c <= '9') || c == '-' || c == '.')) return false;
        }
        name = name.toLowerCase(Locale.ROOT);
        for (String domain : DOMAINS) {
            if (name.equals(domain)) return true;
        }
        return false;
    }
}
