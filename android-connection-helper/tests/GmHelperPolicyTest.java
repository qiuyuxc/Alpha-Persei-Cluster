import io.github.qiuyuxc.agconnect.*;

public final class GmHelperPolicyTest {
    public static void main(String[] args) {
        TestSupport.check(GmEndpoint.origin("47.116.188.72", false).equals("https://47.116.188.72"), "Selected remote server");
        TestSupport.check(GmEndpoint.origin("", true).equals("https://127.0.0.1:8443"), "Local server port");
        TestSupport.rejected(() -> GmEndpoint.origin("", false), "Missing server is not replaced silently");
        TestSupport.rejected(() -> GmEndpoint.origin("https://evil.example/", false), "Only configured IPv4 allowed");
        String local = GmEndpoint.origin("", true);
        TestSupport.check(GmApiPolicy.allows(local, "POST", local + "/api/auth/login"), "Local authenticated API");
        TestSupport.check(GmApiPolicy.allows(local, "GET", local + "/api/sub-gm/me"), "Local own account state");
        for (String url : new String[]{"https://127.0.0.1/api/auth/me", "https://127.0.0.1:8444/api/auth/me",
                "https://47.116.188.72:8443/api/auth/me", "http://127.0.0.1:8443/api/auth/me",
                "https://admin@127.0.0.1:8443/api/auth/me", local + "/api/auth/me?token=secret"})
            TestSupport.check(!GmApiPolicy.allows(local, "GET", url), "Local token stays at exact HTTPS origin");
        TestSupport.check(!GmApiPolicy.allows("https://47.116.188.72:8443", "GET",
                "https://47.116.188.72:8443/api/auth/me"), "Custom port restricted to local mode");
        for (int bits = 0; bits < 32; bits++) {
            boolean connected = (bits & 1) != 0, enabled = (bits & 2) != 0, granted = (bits & 4) != 0;
            boolean panel = (bits & 8) != 0, locked = (bits & 16) != 0;
            TestSupport.check(GmOverlayPolicy.available(connected, enabled, granted, locked) == ((bits & 23) == 7),
                    "Open panel remains eligible while handle is hidden; remove it on connection/permission/lock changes " + bits);
            TestSupport.check(GmOverlayPolicy.visible(connected, enabled, granted, panel, locked) == (bits == 7),
                    "Connection/permission/panel/lock lifecycle " + bits);
        }
        TestSupport.done("GM helper policy");
    }
}
