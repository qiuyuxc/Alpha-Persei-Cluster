package io.github.qiuyuxc.agconnect;

/** Numeric IPv4 only; no implicit DNS resolution or URL parsing. */
public final class ServerAddress {
    public static final String DEFAULT = "127.0.0.1";
    public final String host;

    private ServerAddress(String host) { this.host = host; }

    public static ServerAddress parse(String value) {
        if (value == null) throw new IllegalArgumentException("请输入服务器 IPv4 地址");
        String host = value.trim();
        String[] parts = host.split("\\.", -1);
        if (parts.length != 4)
            throw new IllegalArgumentException("请输入四段 IPv4 地址，不带协议、端口或路径");
        for (String part : parts) {
            if (!part.matches("0|[1-9][0-9]{0,2}") || Integer.parseInt(part) > 255)
                throw new IllegalArgumentException("IPv4 每段须为 0–255，不能带前导零");
        }
        int first = Integer.parseInt(parts[0]);
        if (first == 0 || first >= 224)
            throw new IllegalArgumentException("请填写可以连接的服务器地址");
        if (host.equals(Ipv4Packet.DNS_ADDRESS) || host.equals(Ipv4Packet.INTERFACE_ADDRESS))
            throw new IllegalArgumentException("该地址供连接助手内部使用，请填写服务器实际地址");
        return new ServerAddress(host);
    }

    public static ServerAddress stored(String value) {
        try { return parse(value); }
        catch (IllegalArgumentException ignored) { return parse(DEFAULT); }
    }

    public byte[] bytes() {
        String[] parts = host.split("\\.");
        byte[] address = new byte[4];
        for (int i = 0; i < 4; i++) address[i] = (byte) Integer.parseInt(parts[i]);
        return address;
    }
}
