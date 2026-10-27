import io.github.qiuyuxc.agdns.GmPagePolicy;
import io.github.qiuyuxc.agdns.ServerAddress;

public final class GmPagePolicyTest {
    public static void main(String[] args) {
        int count = 0;
        for (String host : new String[]{ServerAddress.DEFAULT, "203.0.113.42"}) {
        GmPagePolicy policy = new GmPagePolicy(ServerAddress.parse(host));
        String[] allowed = { policy.url(), policy.url() + "#mail",
                "https://" + host + ":443/web/sub_gm.html#accounts" };
        String[] denied = { null, "", "http://" + host + "/web/sub_gm.html", "javascript:alert(1)",
                "file:///web/sub_gm.html", "https://" + host + ".evil.test/web/sub_gm.html",
                "https://" + host + "@evil.test/web/sub_gm.html", "https://admin@" + host + "/web/sub_gm.html",
                "https://" + host + ":444/web/sub_gm.html", "https://" + host + "/web/gm_console.html",
                policy.url() + "?url=https://evil.test", "https://" + host + "/web/%73ub_gm.html",
                "https://" + host + "/web/../sub_gm.html", "https://" + host + "/web/sub_gm.html/extra" };
        for (String value : allowed) if (!policy.allows(value)) throw new AssertionError("Rejected allowed URL");
        for (String value : denied) if (policy.allows(value)) throw new AssertionError("Accepted untrusted URL");
        if (!policy.allowsResource("https://" + host + "/api/auth/me")) throw new AssertionError("API origin rejected");
        if (policy.allowsResource("https://evil.test/api/auth/me")
                || policy.allowsResource("http://" + host + "/web/sub-gm.js")
                || policy.allowsResource("https://user@" + host + "/api/auth/me")
                || policy.allowsResource("https://" + host + ":444/web/sub-gm.js"))
            throw new AssertionError("External resource accepted");
        String other = host.equals(ServerAddress.DEFAULT) ? "203.0.113.42" : ServerAddress.DEFAULT;
        if (policy.allows("https://" + other + "/web/sub_gm.html")
                || policy.allowsResource("https://" + other + "/api/auth/me"))
            throw new AssertionError("Previous server origin accepted");
        count += allowed.length + denied.length + 7;
        }
        System.out.println("GM navigation and resource origin: " + count + " cases passed");
    }
}
