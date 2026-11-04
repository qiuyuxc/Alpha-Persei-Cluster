package io.github.qiuyuxc.agconnect;

/** Use the same server selection as the connection helper. */
public final class GmEndpoint {
    private GmEndpoint() { }
    public static String origin(String server, boolean local) {
        return local ? "https://127.0.0.1:" + LocalServerConfig.HTTPS_PORT
                : "https://" + ServerAddress.parse(server).host;
    }
}
