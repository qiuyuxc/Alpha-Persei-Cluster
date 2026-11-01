import io.github.qiuyuxc.agconnect.*;
import java.io.IOException;
import java.net.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** Run with an Android JVM and -Djava.library.path pointing to the built ABI. */
public final class NativeTunnelTest {
    private static byte[] syn(int port) {
        byte[] packet = TestSupport.tcp(port, 700, 0, 2, new byte[0], 512, 8192);
        packet[19] = 3; TestSupport.put16(packet, 22, 443);
        TestSupport.put16(packet, 10, Ipv4Packet.checksum(packet, 0, 20, 0));
        int pseudo = 6 + packet.length - 20;
        for (int i = 12; i < 20; i += 2) pseudo += TestSupport.u16(packet, i);
        TestSupport.put16(packet, 36, Ipv4Packet.checksum(packet, 20, packet.length - 20, pseudo));
        return packet;
    }
    public static void main(String[] args) throws Exception {
        AtomicInteger errors = new AtomicInteger();
        try (ServerSocket backend = new ServerSocket(0, 4, InetAddress.getByName("127.0.0.1"));
             LocalForwarder forwarder = new LocalForwarder(backend.getLocalPort(), backend.getLocalPort(), Socket::new,
                                                          new AtomicLong(), new AtomicLong())) {
            for (int iteration = 0; iteration < 10; iteration++) {
                BlockingQueue<byte[]> replies = new LinkedBlockingQueue<>();
                NativeTunnel tunnel = new NativeTunnel(forwarder.port(), replies::add, error -> errors.incrementAndGet());
                try {
                    boolean rejected = false;
                    try { new NativeTunnel(forwarder.port(), packet -> {}, error -> {}).close(); }
                    catch (IOException expected) { rejected = true; }
                    TestSupport.check(rejected, "a second native stack is rejected");
                    TestSupport.check(tunnel.send(syn(41000 + iteration)), "JNI passes the request packet");
                    byte[] answer = replies.poll(3, TimeUnit.SECONDS);
                    TestSupport.check(answer != null && (answer[33] & 18) == 18, "real stack returns SYN/ACK");
                    TestSupport.check(TestSupport.u32(answer, 28) == 701, "SYN/ACK acknowledges the client sequence");
                    TestSupport.check(answer[15] == 3 && answer[19] == 1, "reply has the original virtual destination as its source");
                    TestSupport.checksums(answer);
                } finally {
                    long started = System.nanoTime(); tunnel.close(); tunnel.close();
                    TestSupport.check(System.nanoTime() - started < TimeUnit.SECONDS.toNanos(2), "native stop joins within two seconds");
                    TestSupport.check(!tunnel.send(syn(42000)), "closed JNI handle cannot be written");
                }
            }
            AtomicReference<NativeTunnel> reference = new AtomicReference<>();
            CountDownLatch stopped = new CountDownLatch(1);
            NativeTunnel callbackClose = new NativeTunnel(forwarder.port(), packet -> {
                reference.get().close(); stopped.countDown();
            }, error -> errors.incrementAndGet());
            reference.set(callbackClose);
            callbackClose.send(syn(43000));
            TestSupport.check(stopped.await(3, TimeUnit.SECONDS), "close from packet callback does not join itself");
            callbackClose.close();
            TestSupport.check(errors.get() == 0, "normal shutdown produces no failure callbacks");
        }
        TestSupport.done("native JNI startup, packets and repeated shutdown");
    }
}
