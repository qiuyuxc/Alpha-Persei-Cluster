import io.github.qiuyuxc.agconnect.GmApiPolicy;

public final class GmApiPolicyTest {
    public static void main(String[] args) {
        String root = "https://192.0.2.8";
        String[][] allowed = {
            {"GET","/api/auth/me"},{"POST","/api/auth/login"},{"POST","/api/auth/password"},
            {"POST","/api/auth/logout"},{"GET","/api/sub-gm/me"},{"POST","/api/sub-gm/grant"},
            {"POST","/api/sub-gm/hero/unlock"},{"POST","/api/sub-gm/hero/graduate"},
            {"GET","/api/sub-gm/items?category=materials&search=%E8%8A%AF%E7%89%87"},
            {"GET","/api/gm/accounts"},{"POST","/api/gm/accounts/kick"},{"POST","/api/gm/accounts/disabled"},
            {"GET","/api/gm/automation/status"},{"POST","/api/gm/automation/schedule"},
            {"GET","/api/gm/shop/restock/status"},{"POST","/api/gm/shop/restock/settings"},{"POST","/api/gm/shop/restock/run"},
            {"GET","/api/gm/sub-gm/policy"},{"POST","/api/gm/sub-gm/policy"}
        };
        for (String[] value : allowed) if (!GmApiPolicy.allows(root,value[0],root+value[1])) throw new AssertionError("Allowed API rejected: " + value[1]);
        String[][] denied = {
            {"GET","http://192.0.2.8/api/auth/me"},{"GET","https://192.0.2.9/api/auth/me"},
            {"GET","https://192.0.2.8.evil.test/api/auth/me"},{"GET","https://192.0.2.8@evil.test/api/auth/me"},
            {"GET","https://admin@192.0.2.8/api/auth/me"},{"GET","https://192.0.2.8:444/api/auth/me"},
            {"GET",root+"/api/auth/me#token"},{"GET",root+"/api/auth/me?token=secret"},
            {"GET",root+"/api/sub-gm/items?category=materials&uid=2174928301"},
            {"GET",root+"/api/sub-gm/items?category=unknown"},{"GET",root+"/api/sub-gm/items?category=materials&category=currency"},
            {"GET",root+"/api/gm/accounts/kick"},{"POST",root+"/api/auth/me"},{"DELETE",root+"/api/auth/me"},
            {"GET",root+"/api/auth/%6de"},{"GET",root+"/api/sub-gm/../auth/me"},
            {"GET",root+"/web/sub_gm.html"},{"GET","file:///api/auth/me"},{"GET","javascript:alert(1)"}
        };
        for (String[] value : denied) if (GmApiPolicy.allows(root,value[0],value[1])) throw new AssertionError("Untrusted API accepted: " + value[1]);
        if (GmApiPolicy.allows("http://192.0.2.8","GET",root+"/api/auth/me")) throw new AssertionError("Insecure origin");
        System.out.println("GM native API policy: " + (allowed.length + denied.length + 1) + " cases passed");
    }
}
