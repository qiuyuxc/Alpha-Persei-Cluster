import io.github.qiuyuxc.agconnect.*;
import java.io.*;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

public final class DnsForwarderTest {
    private static final byte[] IP = {93, (byte) 184, (byte) 216, 34};

    private static final class LocalDns implements AutoCloseable {
        final DatagramSocket udp;
        final ServerSocket tcp;
        final ExecutorService threads = Executors.newFixedThreadPool(3);
        final boolean truncate, wrongFirst, fail;
        final List<byte[]> seen = Collections.synchronizedList(new ArrayList<>());
        volatile boolean running = true;
        volatile Throwable error;
        AtomicInteger tcpQueries = new AtomicInteger();
        LocalDns(boolean truncate, boolean wrongFirst, boolean fail) throws IOException {
            this.truncate = truncate; this.wrongFirst = wrongFirst; this.fail = fail;
            tcp = new ServerSocket(0, 5, InetAddress.getLoopbackAddress());
            udp = new DatagramSocket(new InetSocketAddress(InetAddress.getLoopbackAddress(), tcp.getLocalPort()));
            threads.execute(this::udpLoop); threads.execute(this::tcpLoop);
        }
        InetSocketAddress address() { return new InetSocketAddress(InetAddress.getLoopbackAddress(), tcp.getLocalPort()); }
        void udpLoop() {
            try {
                while (running) {
                    byte[] buffer = new byte[65535]; DatagramPacket request = new DatagramPacket(buffer, buffer.length);
                    udp.receive(request);
                    byte[] query = Arrays.copyOf(buffer, request.getLength()); seen.add(query);
                    if (fail) continue;
                    byte[] answer = TestSupport.answer(query, IP);
                    if (wrongFirst) {
                        byte[] wrong = answer.clone(); wrong[0] ^= 1;
                        udp.send(new DatagramPacket(wrong, wrong.length, request.getSocketAddress()));
                    }
                    if (truncate) { answer = Arrays.copyOf(query, query.length); TestSupport.put16(answer, 2, 0x8380); }
                    udp.send(new DatagramPacket(answer, answer.length, request.getSocketAddress()));
                }
            } catch (Throwable failure) { if (running) error = failure; }
        }
        void tcpLoop() {
            try {
                while (running) {
                    Socket socket = tcp.accept();
                    threads.execute(() -> {
                        try (Socket peer = socket) {
                            peer.setSoTimeout(1000);
                            DataInputStream input = new DataInputStream(peer.getInputStream());
                            byte[] query = new byte[input.readUnsignedShort()]; input.readFully(query);
                            seen.add(query); tcpQueries.incrementAndGet();
                            if (fail) return;
                            byte[] answer = truncate ? TestSupport.largeAnswer(query, 1800) : TestSupport.answer(query, IP);
                            DataOutputStream output = new DataOutputStream(peer.getOutputStream());
                            output.writeShort(answer.length); output.flush();
                            // Exercise partial TCP reads rather than a single write.
                            output.write(answer, 0, 7); output.flush();
                            output.write(answer, 7, answer.length - 7); output.flush();
                        } catch (Throwable failure) { if (running) error = failure; }
                    });
                }
            } catch (Throwable failure) { if (running) error = failure; }
        }
        @Override public void close() throws Exception {
            running = false; udp.close(); tcp.close(); threads.shutdownNow();
            TestSupport.check(threads.awaitTermination(2, TimeUnit.SECONDS), "local DNS threads stopped");
            TestSupport.check(error == null, "local DNS server error: " + error);
        }
    }

    public static void main(String[] args) throws Exception {
        AtomicInteger opened = new AtomicInteger(), released = new AtomicInteger();
        DnsForwarder.Transport transport = new DnsForwarder.Transport() {
            public DatagramSocket udp() throws IOException { opened.incrementAndGet(); return new DatagramSocket(); }
            public Socket tcp() { opened.incrementAndGet(); return new Socket(); }
            public void release(Closeable socket) {
                DnsForwarder.Transport.super.release(socket); released.incrementAndGet();
            }
        };
        byte[] query = TestSupport.query("download.ys4fun.com", 1);
        TestSupport.put16(query, 2, 0x0110);
        try (LocalDns normal = new LocalDns(false, true, false)) {
            byte[] answer = new DnsForwarder(Collections.singletonList(normal.address()), transport, 300).resolve(query);
            TestSupport.check(Arrays.equals(answer, TestSupport.answer(query, IP)), "UDP pass-through exact response");
            TestSupport.check(normal.tcpQueries.get() == 0, "small UDP does not require TCP");
            TestSupport.check(Arrays.equals(query, normal.seen.get(0)), "official query bytes unchanged");
        }
        try (LocalDns truncated = new LocalDns(true, false, false)) {
            byte[] answer = new DnsForwarder(Collections.singletonList(truncated.address()), transport, 300).resolve(query);
            TestSupport.check(answer.length == 1800 && DnsMessage.matches(query, answer), "UDP TC falls back to TCP");
            TestSupport.check(truncated.tcpQueries.get() == 1, "one TCP fallback");
            TestSupport.check(truncated.seen.stream().allMatch(q -> Arrays.equals(q, query)),
                    "TCP fallback keeps original wire query");
        }
        try (LocalDns primary = new LocalDns(false, false, true);
             LocalDns backup = new LocalDns(false, false, false)) {
            byte[] answer = new DnsForwarder(Arrays.asList(primary.address(), backup.address()), transport, 100).resolve(query);
            TestSupport.check(Arrays.equals(answer, TestSupport.answer(query, IP)), "failed primary uses second physical DNS");
            TestSupport.check(primary.tcpQueries.get() > 0, "UDP failure also tries TCP");
        }
        try (LocalDns unavailable = new LocalDns(false, false, true)) {
            boolean failed = false;
            try { new DnsForwarder(Collections.singletonList(unavailable.address()), transport, 100).resolve(query); }
            catch (IOException expected) { failed = true; }
            TestSupport.check(failed, "unavailable DNS fails within configured timeout");
        }
        TestSupport.check(opened.get() == released.get(), "all forwarding sockets released");
        TestSupport.done("physical-network DNS forwarding");
    }
}
