import io.github.qiuyuxc.agconnect.*;
import java.util.Arrays;

public final class DnsMessageTest {
    public static void main(String[] args) {
        ServerAddress server = ServerAddress.parse("192.168.20.8");
        for (String host : new String[]{"open.ys4fun.com", "OPEN.YS4FUN.COM", "prod-api-activity.ys4fun.com"}) {
            byte[] query = TestSupport.query(host, 1);
            byte[] answer = DnsMessage.localResponse(query, server);
            TestSupport.check(DnsMessage.matches(query, answer), "matching local response");
            TestSupport.check(TestSupport.u16(answer, 6) == 1, "one A record");
            TestSupport.check(Arrays.equals(Arrays.copyOfRange(answer, answer.length - 4, answer.length),
                    new byte[]{(byte) 192, (byte) 168, 20, 8}), "selected IPv4");
            TestSupport.check(TestSupport.u16(answer, answer.length - 8) == 30, "short TTL");
        }
        for (int type : new int[]{28, 64, 65, 255}) {
            byte[] answer = DnsMessage.localResponse(TestSupport.query("open.ys4fun.com", type), server);
            TestSupport.check(TestSupport.u16(answer, 6) == 0, "no official IPv6 or alternate endpoints");
            TestSupport.check((TestSupport.u16(answer, 2) & 15) == 0, "NODATA is not NXDOMAIN");
        }
        for (String host : new String[]{"download.ys4fun.com", "download-eo.ys4fun.com",
                "packaging.ys4fun.com", "ys4fun.com", "notopen.ys4fun.com",
                "open.ys4fun.com.example", "sub.open.ys4fun.com", "example.com"}) {
            byte[] query = TestSupport.query(host, 1);
            TestSupport.check(DnsMessage.localResponse(query, server) == null, "pass through " + host);
        }
        // A dot inside a single wire label is not a DNS label separator.
        byte[] dottedLabel = TestSupport.query("open.ys4fun.com", 1);
        byte[] confusing = new byte[dottedLabel.length];
        System.arraycopy(dottedLabel, 0, confusing, 0, 12);
        confusing[12] = 15;
        byte[] label = "open.ys4fun.com".getBytes(java.nio.charset.StandardCharsets.US_ASCII);
        System.arraycopy(label, 0, confusing, 13, label.length);
        System.arraycopy(dottedLabel, dottedLabel.length - 5, confusing, 28, 5);
        TestSupport.check(DnsMessage.localResponse(confusing, server) == null, "wire-label boundary");
        byte[] flags = TestSupport.query("open.ys4fun.com", 1);
        TestSupport.put16(flags, 2, 0x0130);
        TestSupport.check((TestSupport.u16(DnsMessage.localResponse(flags, server), 2) & 0x30) == 0x10,
                "preserve CD and clear AD");
        byte[] query = TestSupport.query("example.com", 1);
        byte[] answer = TestSupport.answer(query, new byte[]{1, 2, 3, 4});
        TestSupport.check(DnsMessage.matches(query, answer), "upstream match");
        byte[] wrongId = answer.clone(); wrongId[0] ^= 1;
        TestSupport.check(!DnsMessage.matches(query, wrongId), "reject wrong transaction ID");
        TestSupport.check(!DnsMessage.matches(query, TestSupport.answer(TestSupport.query("other.com", 1),
                new byte[]{1, 2, 3, 4})), "reject wrong question");
        TestSupport.check(!DnsMessage.matches(query, query), "reject request as response");
        TestSupport.check(DnsMessage.forUdp(query, answer) == answer, "small UDP response unchanged");
        byte[] large = TestSupport.largeAnswer(query, 1600);
        byte[] tc = DnsMessage.forUdp(query, large);
        TestSupport.check(DnsMessage.truncated(tc) && TestSupport.u16(tc, 6) == 0,
                "large UDP response requests TCP fallback");
        TestSupport.check(DnsMessage.parseQuery(TestSupport.edns(query, 4096)).udpLimit == 1232,
                "bounded EDNS response size");
        byte[] broken = query.clone(); broken[12] = (byte) 0xc0; broken[13] = 12;
        TestSupport.rejected(() -> DnsMessage.parseQuery(broken), "cyclic pointer rejected");
        TestSupport.rejected(() -> DnsMessage.parseQuery(new byte[11]), "short header rejected");
        TestSupport.rejected(() -> DnsMessage.parseQuery(Arrays.copyOf(query, query.length - 1)),
                "short question rejected");
        TestSupport.check(DnsMessage.error(new byte[4], 1) == null, "no short-packet reflection");
        TestSupport.check(DnsMessage.error(answer, 1) == null, "no response reflection");
        TestSupport.check(TestSupport.u16(DnsMessage.error(broken, 1), 4) == 0,
                "FORMERR omits invalid question");
        for (String valid : new String[]{"127.0.0.1", "192.168.1.20", "10.0.0.2", " 1.2.3.4 "})
            TestSupport.check(ServerAddress.parse(valid).bytes().length == 4, "valid IPv4");
        for (String invalid : new String[]{"https://1.2.3.4", "1.2.3.4:443", "1.2.3.4/path",
                "01.2.3.4", "256.2.3.4", "1.2.3", "0.0.0.0", "224.0.0.1",
                "255.255.255.255", Ipv4Packet.DNS_ADDRESS, Ipv4Packet.INTERFACE_ADDRESS})
            TestSupport.rejected(() -> ServerAddress.parse(invalid), "invalid destination " + invalid);
        TestSupport.done("DNS policy and wire parsing");
    }
}
