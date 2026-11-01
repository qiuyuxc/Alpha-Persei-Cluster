import io.github.qiuyuxc.agconnect.*;
import java.io.*;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public final class LocalForwarderTest {
    private static final class Echo implements AutoCloseable {
        final ServerSocket listener = new ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"));
        final ExecutorService workers = Executors.newFixedThreadPool(4);
        final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
        final AtomicInteger accepted = new AtomicInteger();
        volatile boolean closed;
        volatile Throwable error;
        Echo() throws IOException {
            workers.execute(() -> {
                try {
                    while (!closed) {
                        Socket peer = listener.accept(); sockets.add(peer); accepted.incrementAndGet();
                        workers.execute(() -> {
                            try (Socket socket = peer) {
                                byte[] buffer = new byte[4096]; int size;
                                while ((size = socket.getInputStream().read(buffer)) != -1)
                                    socket.getOutputStream().write(buffer, 0, size);
                                socket.shutdownOutput();
                            } catch (Throwable failure) { if (!closed) error = failure; }
                            finally { sockets.remove(peer); }
                        });
                    }
                } catch (Throwable failure) { if (!closed) error = failure; }
            });
        }
        int port() { return listener.getLocalPort(); }
        public void close() throws Exception {
            closed = true; listener.close();
            for (Socket socket : sockets) socket.close();
            workers.shutdownNow();
            TestSupport.check(workers.awaitTermination(2, TimeUnit.SECONDS), "backend threads stop");
            TestSupport.check(error == null, "backend had no unexpected failure: " + error);
        }
    }

    static Socket greeting(int port, byte[] methods) throws IOException {
        Socket socket = new Socket("127.0.0.1", port); socket.setSoTimeout(3000);
        OutputStream output = socket.getOutputStream();
        // Deliberately split the negotiation across writes.
        output.write(5); output.flush(); output.write(methods.length); output.flush();
        output.write(methods); output.flush();
        byte[] answer = new byte[2]; new DataInputStream(socket.getInputStream()).readFully(answer);
        TestSupport.check(answer[0] == 5, "SOCKS version reply");
        TestSupport.check(answer[1] == (methods[methods.length - 1] == 0 ? 0 : (byte)255), "SOCKS method reply");
        return socket;
    }

    static Socket connect(int listener, int port) throws IOException {
        Socket socket = greeting(listener, new byte[]{2, 0});
        request(socket, new byte[]{5, 1, 0, 1, 10, 111, 0, 3, (byte)(port >>> 8), (byte)port}, 0);
        return socket;
    }

    private static void request(Socket socket, byte[] request, int code) throws IOException {
        for (byte octet : request) { socket.getOutputStream().write(octet); socket.getOutputStream().flush(); }
        byte[] answer = new byte[10]; new DataInputStream(socket.getInputStream()).readFully(answer);
        TestSupport.check(answer[0] == 5 && answer[1] == code && answer[3] == 1, "SOCKS result " + code);
    }

    private static void transfer(int listener, int target, byte[] payload) throws Exception {
        try (Socket socket = connect(listener, target)) {
            ExecutorService writer = Executors.newSingleThreadExecutor();
            try {
                Future<?> pending = writer.submit(() -> {
                    try { socket.getOutputStream().write(payload); socket.shutdownOutput(); }
                    catch (IOException error) { throw new UncheckedIOException(error); }
                });
                byte[] received = new byte[payload.length];
                new DataInputStream(socket.getInputStream()).readFully(received);
                pending.get(3, TimeUnit.SECONDS);
                TestSupport.check(Arrays.equals(payload, received), "all opaque bytes survive both directions");
                TestSupport.check(socket.getInputStream().read() == -1, "half close delivers final response then EOF");
            } finally { writer.shutdownNow(); }
        }
    }

    public static void main(String[] args) throws Exception {
        InetSocketAddress https = LocalServerConfig.target(LocalServerConfig.ADDRESS, 443, 8443, 8080);
        InetSocketAddress http = LocalServerConfig.target(LocalServerConfig.ADDRESS, 80, 8443, 8080);
        TestSupport.check(https.getAddress().isLoopbackAddress() && https.getPort() == 8443, "HTTPS maps to loopback high port");
        TestSupport.check(http.getPort() == 8080, "HTTP maps to 8080");
        for (String host : new String[]{"127.0.0.1", "10.111.0.2", "192.168.1.2", "93.184.216.34"})
            TestSupport.check(LocalServerConfig.target(host, 443, 8443, 8080) == null, "reject arbitrary SOCKS destination");
        TestSupport.check(LocalServerConfig.target(LocalServerConfig.ADDRESS, 8105, 8443, 8080) == null, "reject unrelated port");
        TestSupport.rejected(() -> ServerAddress.parse(LocalServerConfig.ADDRESS), "virtual address cannot be an external server");
        for (String host : new String[]{"open.ys4fun.com", "prod-api-activity.ys4fun.com"}) {
            byte[] answer = DnsMessage.localResponse(TestSupport.query(host, 1), ServerAddress.local());
            TestSupport.check(Arrays.equals(Arrays.copyOfRange(answer, answer.length - 4, answer.length),
                              new byte[]{10, 111, 0, 3}), "local SDK DNS uses the virtual address");
        }
        TestSupport.check(DnsMessage.localResponse(TestSupport.query("download.ys4fun.com", 1), ServerAddress.local()) == null,
                          "resource domains continue to the physical DNS");
        AtomicLong connections = new AtomicLong(), failures = new AtomicLong();
        try (Echo sdk = new Echo(); Echo auxiliary = new Echo()) {
            LocalForwarder forwarder = new LocalForwarder(sdk.port(), auxiliary.port(), Socket::new, connections, failures);
            try {
                byte[] bulk = new byte[512 * 1024]; new Random(7).nextBytes(bulk);
                transfer(forwarder.port(), 443, bulk);
                transfer(forwarder.port(), 80, new byte[]{0, 3, 22, 3, 3, (byte)255, 0});
                TestSupport.check(sdk.accepted.get() == 1 && auxiliary.accepted.get() == 1, "both local target ports reached");
                for (byte[] invalid : new byte[][]{
                    {5, 1, 0, 1, 93, (byte)184, (byte)216, 34, 1, (byte)187},
                    {5, 1, 0, 1, 10, 111, 0, 3, 31, (byte)169},
                    {5, 3, 0, 1}, {5, 1, 0, 3}}) {
                    try (Socket denied = greeting(forwarder.port(), new byte[]{0})) {
                        request(denied, invalid, invalid.length == 10 ? 2 : invalid[1] == 3 ? 7 : 8);
                    }
                }
                try (Socket denied = greeting(forwarder.port(), new byte[]{2})) {
                    TestSupport.check(denied.getInputStream().read() == -1, "unsupported authentication closes the connection");
                }
                TestSupport.check(connections.get() == 2 && failures.get() == 0, "policy rejection never opens a backend");
                Socket active = connect(forwarder.port(), 443);
                Socket incomplete = greeting(forwarder.port(), new byte[]{0});
                long started = System.nanoTime(); forwarder.close();
                try { TestSupport.check(active.getInputStream().read() == -1, "stop closes active streams"); }
                catch (SocketException expected) { TestSupport.check(true, "stop resets active streams"); }
                try { TestSupport.check(incomplete.getInputStream().read() == -1, "stop closes incomplete requests"); }
                catch (SocketException expected) { TestSupport.check(true, "stop resets incomplete requests"); }
                active.close(); incomplete.close();
                TestSupport.check(System.nanoTime() - started < TimeUnit.SECONDS.toNanos(2), "stop is bounded");
            } finally { forwarder.close(); }
            try (LocalForwarder restarted = new LocalForwarder(sdk.port(), auxiliary.port(), Socket::new, connections, failures)) {
                transfer(restarted.port(), 443, "restart".getBytes("UTF-8"));
            }
        }
        int missing;
        try (ServerSocket reserve = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) { missing = reserve.getLocalPort(); }
        AtomicReference<String> diagnostic = new AtomicReference<>();
        try (LocalForwarder unavailable = new LocalForwarder(missing, missing, Socket::new, connections, failures,
                                                           error -> diagnostic.set(error.getMessage()));
             Socket socket = greeting(unavailable.port(), new byte[]{0})) {
            request(socket, new byte[]{5, 1, 0, 1, 10, 111, 0, 3, 1, (byte)187}, 5);
            TestSupport.check(failures.get() == 1, "unavailable local server has a failure reply and diagnostic count");
            TestSupport.check(diagnostic.get().contains("127.0.0.1:" + missing), "diagnostic identifies the failing local port");
        }
        TestSupport.done("local SDK policy, opaque streams and lifecycle");
    }
}
