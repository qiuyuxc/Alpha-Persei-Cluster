package io.github.qiuyuxc.agdns;

import java.net.URI;
import java.net.URLDecoder;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/** Explicit native API routes; authentication stays in POST bodies/headers. */
public final class GmApiPolicy {
    private static final Set<String> GET = new HashSet<>(Arrays.asList(
            "/api/auth/me", "/api/auth/status", "/api/sub-gm/me", "/api/sub-gm/items",
            "/api/gm/accounts", "/api/gm/sub-gm/policy", "/api/gm/automation/status",
            "/api/gm/shop/restock/status"));
    private static final Set<String> POST = new HashSet<>(Arrays.asList(
            "/api/auth/login", "/api/auth/logout", "/api/auth/password", "/api/sub-gm/grant",
            "/api/sub-gm/hero/unlock", "/api/sub-gm/hero/graduate", "/api/gm/accounts/kick",
            "/api/gm/accounts/disabled", "/api/gm/sub-gm/policy", "/api/gm/automation/schedule",
            "/api/gm/shop/restock/settings", "/api/gm/shop/restock/run"));
    private GmApiPolicy() { }

    public static boolean allows(String origin, String method, String url) {
        try {
            URI base = new URI(origin), target = new URI(url);
            if (!"https".equalsIgnoreCase(base.getScheme()) || base.getHost() == null
                    || base.getRawUserInfo() != null || base.getRawQuery() != null || base.getRawFragment() != null
                    || !(base.getRawPath() == null || base.getRawPath().isEmpty())
                    || (base.getPort() != -1 && base.getPort() != 443)) return false;
            if (!"https".equalsIgnoreCase(target.getScheme()) || !base.getHost().equals(target.getHost())
                    || target.getRawUserInfo() != null || target.getRawFragment() != null
                    || (target.getPort() != -1 && target.getPort() != 443)) return false;
            Set<String> routes = "GET".equals(method) ? GET : "POST".equals(method) ? POST : null;
            if (routes == null || !routes.contains(target.getRawPath())) return false;
            if (target.getRawQuery() == null) return true;
            if (!"GET".equals(method) || !"/api/sub-gm/items".equals(target.getRawPath())) return false;
            Set<String> seen = new HashSet<>();
            for (String part : target.getRawQuery().split("&")) {
                String[] pair = part.split("=", 2);
                String key = URLDecoder.decode(pair[0], "UTF-8");
                if (!Arrays.asList("category", "search").contains(key) || !seen.add(key)) return false;
                if ("category".equals(key) && (pair.length != 2 || !Arrays.asList(
                        "currency", "materials", "heroes", "equipment").contains(URLDecoder.decode(pair[1], "UTF-8")))) return false;
            }
            return true;
        } catch (Exception ignored) { return false; }
    }
}
