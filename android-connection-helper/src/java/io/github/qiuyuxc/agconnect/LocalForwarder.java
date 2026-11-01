package io.github.qiuyuxc.agconnect;

import java.io.Closeable;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.Collections;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.SynchronousQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;

/** Bounded loopback SOCKS5 CONNECT endpoint for the two local SDK ports. */
public final class LocalForwarder implements Closeable {
    public interface SocketFactory { Socket open() throws IOException; }
    private final int httpsPort, httpPort;
    private final SocketFactory factory;
    private final AtomicLong connections, failures;
    private final Consumer<IOException> failureListener;
    private final ServerSocket listener;
    private final Set<Socket> sockets = Collections.newSetFromMap(new ConcurrentHashMap<Socket, Boolean>());
    private final ThreadPoolExecutor workers = new ThreadPoolExecutor(0, 32, 30, TimeUnit.SECONDS,
            new SynchronousQueue<>(), task -> {
                Thread thread = new Thread(task, "local-sdk-connect"); thread.setDaemon(true); return thread;
            }, new ThreadPoolExecutor.AbortPolicy());
    private volatile boolean closed;

    public LocalForwarder(int httpsPort, int httpPort, SocketFactory factory,
                          AtomicLong connections, AtomicLong failures) throws IOException {
        this(httpsPort, httpPort, factory, connections, failures, error -> {});
    }

    public LocalForwarder(int httpsPort, int httpPort, SocketFactory factory,
                          AtomicLong connections, AtomicLong failures, Consumer<IOException> failureListener) throws IOException {
        if (httpsPort < 1024 || httpsPort > 65535 || httpPort < 1024 || httpPort > 65535)
            throw new IllegalArgumentException("Local listeners must use high ports");
        this.httpsPort = httpsPort; this.httpPort = httpPort; this.factory = factory;
        this.connections = connections; this.failures = failures;
        this.failureListener = failureListener;
        listener = new ServerSocket();
        listener.bind(new InetSocketAddress("127.0.0.1", 0), 32);
        Thread accept = new Thread(this::accept, "local-sdk-listener"); accept.setDaemon(true); accept.start();
    }

    public int port() { return listener.getLocalPort(); }

    private void track(Socket socket) throws IOException {
        sockets.add(socket);
        if (closed) { release(socket); throw new IOException("Local forwarder closed"); }
    }

    private void accept() {
        while (!closed) {
            Socket client = null;
            try {
                client = listener.accept(); track(client);
                final Socket accepted = client;
                workers.execute(() -> serve(accepted));
            } catch (RejectedExecutionException error) {
                failed("本机连接排队", new IOException("连接并发已满", error)); release(client);
            } catch (IOException error) {
                release(client);
                if (!closed) failed("本机转发监听", error);
                break;
            }
        }
    }

    private static int octet(InputStream input) throws IOException {
        int value = input.read();
        if (value < 0) throw new EOFException("Short SOCKS request");
        return value;
    }

    private static void reply(OutputStream output, int code) throws IOException {
        output.write(new byte[]{5, (byte) code, 0, 1, 127, 0, 0, 1, 0, 0}); output.flush();
    }

    private void serve(Socket client) {
        Socket upstream = null;
        Thread responses = null;
        boolean connected = false;
        String phase = "转发协商";
        try {
            client.setSoTimeout(5000); client.setTcpNoDelay(true);
            InputStream input = client.getInputStream(); OutputStream output = client.getOutputStream();
            if (octet(input) != 5) return;
            int count = octet(input); boolean anonymous = false;
            for (int i = 0; i < count; i++) anonymous |= octet(input) == 0;
            output.write(new byte[]{5, anonymous ? (byte) 0 : (byte) 255}); output.flush();
            if (!anonymous) return;
            int version = octet(input), command = octet(input), reserved = octet(input), type = octet(input);
            if (version != 5 || reserved != 0) { reply(output, 1); return; }
            if (command != 1) { reply(output, 7); return; }
            if (type != 1) { reply(output, 8); return; }
            String host = octet(input) + "." + octet(input) + "." + octet(input) + "." + octet(input);
            int port = (octet(input) << 8) | octet(input);
            InetSocketAddress target = LocalServerConfig.target(host, port, httpsPort, httpPort);
            if (target == null) { reply(output, 2); return; }
            phase = "本机连接准备";
            upstream = factory.open(); track(upstream);
            phase = "连接 127.0.0.1:" + target.getPort();
            upstream.setTcpNoDelay(true); upstream.connect(target, 1500);
            upstream.setSoTimeout(120000); client.setSoTimeout(120000);
            reply(output, 0); connected = true; connections.incrementAndGet();
            final Socket peer = upstream;
            responses = new Thread(() -> {
                try { copy(peer.getInputStream(), client.getOutputStream()); client.shutdownOutput(); }
                catch (IOException error) { closeSocket(peer); closeSocket(client); }
            }, "local-sdk-response");
            responses.setDaemon(true); responses.start();
            copy(input, upstream.getOutputStream());
            upstream.shutdownOutput();
            responses.join();
        } catch (IOException error) {
            if (!connected && !closed) {
                failed(phase, error);
                try { reply(client.getOutputStream(), 5); } catch (IOException ignored) { }
            }
        } catch (InterruptedException error) { Thread.currentThread().interrupt(); }
        finally {
            release(client); release(upstream);
            if (responses != null) responses.interrupt();
        }
    }

    private void failed(String phase, IOException error) {
        failures.incrementAndGet();
        failureListener.accept(new IOException(phase + "失败：" + error.getClass().getSimpleName()
                + ": " + error.getMessage(), error));
    }

    private static void copy(InputStream input, OutputStream output) throws IOException {
        byte[] buffer = new byte[16384];
        int size;
        while ((size = input.read(buffer)) != -1) output.write(buffer, 0, size);
        output.flush();
    }

    private static void closeSocket(Socket socket) {
        if (socket != null) try { socket.close(); } catch (IOException ignored) { }
    }

    private void release(Socket socket) { if (socket != null) { sockets.remove(socket); closeSocket(socket); } }

    @Override public void close() {
        closed = true;
        try { listener.close(); } catch (IOException ignored) { }
        for (Socket socket : sockets) release(socket);
        workers.shutdownNow();
    }
}
