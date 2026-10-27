package io.github.qiuyuxc.agdns;

import java.net.URI;

/** The module never accepts a server URL or credentials from an Intent. */
public final class GmPagePolicy {
    private final ServerAddress server;
    public GmPagePolicy(ServerAddress server) { this.server = server; }
    public String url() { return server.gmUrl(); }

    public boolean allows(String value) {
        if (value == null) return false;
        try {
            URI uri = new URI(value);
            return allowsResource(value)
                    && "/web/sub_gm.html".equals(uri.getRawPath())
                    && uri.getRawQuery() == null;
        } catch (Exception ignored) {
            return false;
        }
    }

    public boolean allowsResource(String value) {
        if (value == null) return false;
        try {
            URI uri = new URI(value);
            return "https".equalsIgnoreCase(uri.getScheme())
                    && server.host.equals(uri.getHost())
                    && (uri.getPort() == -1 || uri.getPort() == 443)
                    && uri.getRawUserInfo() == null;
        } catch (Exception ignored) { return false; }
    }
}
