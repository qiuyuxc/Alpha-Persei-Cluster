package io.github.qiuyuxc.agconnect;

import java.net.InetSocketAddress;

/** The SDK keeps its original host and TLS port; only the socket destination changes. */
public final class LocalServerConfig {
    public static final String ADDRESS = "10.111.0.3";
    public static final int HTTPS_PORT = 8443, HTTP_PORT = 8080;
    private LocalServerConfig() { }

    public static InetSocketAddress target(String address, int port, int httpsPort, int httpPort) {
        if (!ADDRESS.equals(address)) return null;
        int mapped = port == 443 ? httpsPort : port == 80 ? httpPort : 0;
        return mapped == 0 ? null : new InetSocketAddress("127.0.0.1", mapped);
    }

    public static boolean acceptsPacket(byte[] packet) {
        if (packet == null || packet.length < 20 || (packet[0] & 0xf0) != 0x40) return false;
        int header = (packet[0] & 15) * 4;
        return header >= 20 && header <= packet.length && DnsMessage.u16(packet, 2) == packet.length
                && packet[9] == 6 && packet[16] == 10 && packet[17] == 111
                && packet[18] == 0 && packet[19] == 3;
    }

    public static String tunnelConfig(int socksPort) {
        if (socksPort < 1 || socksPort > 65535) throw new IllegalArgumentException("Invalid SOCKS port");
        return "tunnel:\n  mtu: 1500\n  ipv4: " + Ipv4Packet.INTERFACE_ADDRESS
                + "\nsocks5:\n  address: 127.0.0.1\n  port: " + socksPort
                + "\nmisc:\n  max-session-count: 32\n  connect-timeout: 3000"
                + "\n  tcp-read-write-timeout: 120000\n  log-level: error\n";
    }
}
