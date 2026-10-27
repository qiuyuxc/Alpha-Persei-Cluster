import io.github.qiuyuxc.agdns.ServerAddress;
import java.net.InetAddress;

public final class ServerAddressTest {
    public static void main(String[] args) throws Exception {
        String[] valid = {"127.0.0.1", "203.0.113.42", "192.168.1.100", "0.0.0.0", "255.255.255.255"};
        String[] invalid = {null, "", "localhost", "1.2.3", "1.2.3.4.5", "1..2.3", "256.1.2.3",
                "01.2.3.4", "-1.2.3.4", "1.2.3.4:443", "https://1.2.3.4", "1.2.3.4/path",
                "1.2.3.4@evil.test", "1.2.3.4?x=y", "1.2.3.4#x", "::1", "１２７.0.0.1", "9999999999.2.3.4"};
        for (String value : valid) {
            ServerAddress address = ServerAddress.parse(value);
            if (!InetAddress.getByAddress(address.bytes()).getHostAddress().equals(value))
                throw new AssertionError("Wrong resolver bytes: " + value);
            if (!address.gmUrl().equals("https://" + value + "/web/sub_gm.html"))
                throw new AssertionError("GM destination diverged from DNS");
            byte[] altered = address.bytes(); altered[0]++;
            if (!InetAddress.getByAddress(address.bytes()).getHostAddress().equals(value))
                throw new AssertionError("Mutable address escaped");
        }
        for (String value : invalid) {
            try { ServerAddress.parse(value); throw new AssertionError("Accepted invalid address: " + value); }
            catch (IllegalArgumentException expected) { }
            if (!ServerAddress.stored(value).host.equals(ServerAddress.DEFAULT))
                throw new AssertionError("Corrupt settings did not fall back to localhost");
        }
        if (!ServerAddress.parse(" 127.0.0.1 ").host.equals(ServerAddress.DEFAULT)) throw new AssertionError("Whitespace");
        System.out.println("Server address: " + (valid.length + invalid.length + 1) + " cases passed");
    }
}
