import io.github.qiuyuxc.agconnect.*;
import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Consumer;

public final class DnsTunnelTest {
    private static final byte[] EMPTY = new byte[0];
    private static long plus(long value, int n) { return (value + n) & 0xffffffffL; }

    private static final class Client {
        final int port;
        final DnsTunnel tunnel;
        final List<byte[]> out;
        long seq = 0xfffffff0L, ack;
        Client(int port, DnsTunnel tunnel, List<byte[]> out) {
            this.port = port; this.tunnel = tunnel; this.out = out;
        }
        void send(long seq, int flags, byte[] body, int window) {
            tunnel.accept(TestSupport.tcp(port, seq, ack, flags, body, 256, window), 0);
        }
        void open(int window) {
            send(seq, 2, EMPTY, window);
            byte[] synAck = out.remove(0);
            TestSupport.check((synAck[33] & 255) == 0x12, "SYN/ACK");
            TestSupport.check(TestSupport.u32(synAck, 28) == plus(seq, 1), "SYN sequence wrapping");
            TestSupport.check(TestSupport.u16(synAck, 42) == 1400, "MSS option");
            TestSupport.checksums(synAck);
            ack = plus(TestSupport.u32(synAck, 24), 1); seq = plus(seq, 1);
            send(seq, 0x10, EMPTY, window);
        }
        byte[] collect(int window) {
            ByteArrayOutputStream response = new ByteArrayOutputStream();
            while (!out.isEmpty()) {
                byte[] packet = out.remove(0); TestSupport.checksums(packet);
                byte[] body = TestSupport.tcpBody(packet);
                if (body.length == 0) continue;
                TestSupport.check(body.length <= Math.min(256, window), "honor MSS and receive window");
                TestSupport.check(TestSupport.u32(packet, 24) == ack, "in-order response sequence");
                response.write(body, 0, body.length);
                ack = plus(ack, body.length);
                send(seq, 0x10, EMPTY, window);
            }
            return response.toByteArray();
        }
    }

    public static void main(String[] args) {
        byte[] query = TestSupport.query("open.ys4fun.com", 1);
        byte[] answer = DnsMessage.localResponse(query, ServerAddress.parse("192.168.1.4"));
        List<byte[]> out = new ArrayList<>();
        AtomicInteger calls = new AtomicInteger();
        DnsTunnel tunnel = new DnsTunnel((q, reply) -> {
            calls.incrementAndGet(); reply.accept(DnsMessage.localResponse(q, ServerAddress.parse("192.168.1.4")));
        }, out::add);
        tunnel.accept(TestSupport.udp(query), 0);
        TestSupport.check(out.size() == 1 && calls.get() == 1, "UDP query resolved once");
        byte[] udp = out.remove(0); TestSupport.checksums(udp);
        TestSupport.check(TestSupport.u16(udp, 20) == 53 && TestSupport.u16(udp, 22) == 40500,
                "UDP reverse ports");
        TestSupport.check(Arrays.equals(Arrays.copyOfRange(udp, 28, udp.length), answer), "UDP wire response");
        byte[] unrelated = TestSupport.udp(query); unrelated[19] = 8;
        tunnel.accept(unrelated, 0);
        byte[] fragmented = TestSupport.udp(query); fragmented[6] = 0x20;
        tunnel.accept(fragmented, 0);
        byte[] ipv6 = TestSupport.udp(query); ipv6[0] = 0x60;
        tunnel.accept(ipv6, 0);
        byte[] wrongPort = TestSupport.udp(query); TestSupport.put16(wrongPort, 22, 443);
        tunnel.accept(wrongPort, 0);
        TestSupport.check(out.isEmpty() && calls.get() == 1, "ignore other routes, fragments and protocols");

        Client client = new Client(40010, tunnel, out); client.open(65535);
        byte[] framed = TestSupport.frame(query);
        client.send(plus(client.seq, 3), 0x18, Arrays.copyOfRange(framed, 3, framed.length), 65535);
        TestSupport.check(calls.get() == 1, "out-of-order data waits for retransmission"); out.clear();
        client.send(client.seq, 0x18, Arrays.copyOfRange(framed, 0, 3), 65535);
        TestSupport.check(calls.get() == 1, "split DNS frame waits for completion"); out.clear();
        client.seq = plus(client.seq, 3);
        long secondSequence = client.seq;
        byte[] second = Arrays.copyOfRange(framed, 3, framed.length);
        client.send(client.seq, 0x18, second, 65535);
        client.seq = plus(client.seq, second.length);
        TestSupport.check(calls.get() == 2, "complete TCP DNS query resolved once");
        client.send(secondSequence, 0x18, second, 65535);
        TestSupport.check(calls.get() == 2, "duplicate TCP data does not resolve again");
        TestSupport.check(Arrays.equals(client.collect(65535), TestSupport.frame(answer)), "framed TCP response");
        client.send(client.seq, 0x11, EMPTY, 65535); client.seq = plus(client.seq, 1);
        byte[] fin = out.get(out.size() - 1); out.clear();
        TestSupport.check((fin[33] & 255) == 0x11, "close with FIN/ACK"); TestSupport.checksums(fin);
        client.ack = plus(TestSupport.u32(fin, 24), 1); client.send(client.seq, 0x10, EMPTY, 65535);
        TestSupport.check(tunnel.sessionCount() == 0, "closed TCP session released");

        byte[] publicQuery = TestSupport.query("example.com", 16);
        byte[] largeAnswer = TestSupport.largeAnswer(publicQuery, 5000);
        DnsTunnel largeTunnel = new DnsTunnel((q, reply) -> reply.accept(largeAnswer), out::add);
        Client smallWindow = new Client(40011, largeTunnel, out); smallWindow.open(0);
        byte[] largeFrame = TestSupport.frame(publicQuery);
        smallWindow.send(smallWindow.seq, 0x18, largeFrame, 0);
        smallWindow.seq = plus(smallWindow.seq, largeFrame.length);
        TestSupport.check(out.stream().allMatch(p -> TestSupport.tcpBody(p).length == 0),
                "zero TCP window defers response"); out.clear();
        smallWindow.send(smallWindow.seq, 0x10, EMPTY, 100);
        TestSupport.check(Arrays.equals(smallWindow.collect(100), TestSupport.frame(largeAnswer)),
                "large DNS response streamed within client window");
        largeTunnel.close();
        List<Consumer<byte[]>> pending = new ArrayList<>();
        DnsTunnel lateTunnel = new DnsTunnel((q, reply) -> pending.add(reply), out::add);
        lateTunnel.accept(TestSupport.udp(query), 0); lateTunnel.close();
        pending.get(0).accept(answer);
        TestSupport.check(out.isEmpty(), "late upstream response discarded after stop");

        DnsTunnel retries = new DnsTunnel((q, reply) -> reply.accept(answer), out::add);
        retries.accept(TestSupport.tcp(40100, 9, 0, 2, EMPTY, 1400, 65535), 0);
        byte[] firstSyn = out.remove(0); retries.tick(1000);
        TestSupport.check(Arrays.equals(firstSyn, out.remove(0)), "lost SYN/ACK retransmitted verbatim");
        for (int i = 2; i <= 5; i++) retries.tick(i * 1000);
        TestSupport.check(retries.sessionCount() == 0, "retry limit releases session"); out.clear();
        for (int i = 0; i < 33; i++)
            retries.accept(TestSupport.tcp(41000 + i, 1, 0, 2, EMPTY, 1400, 65535), 6000);
        TestSupport.check(retries.sessionCount() == 32, "bounded TCP sessions");
        TestSupport.check((out.get(out.size() - 1)[33] & 4) != 0, "excess TCP session reset");
        retries.tick(36000);
        TestSupport.check(retries.sessionCount() == 0, "idle sessions released");
        tunnel.close(); retries.close();
        TestSupport.done("DNS tunnel UDP/TCP");
    }
}
