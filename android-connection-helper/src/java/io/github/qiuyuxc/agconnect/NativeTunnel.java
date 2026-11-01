package io.github.qiuyuxc.agconnect;

import java.io.Closeable;
import java.io.IOException;
import java.util.function.Consumer;

/** A userspace TCP stack connected by a packet socketpair, never by /dev/net/tun. */
public final class NativeTunnel implements Closeable {
    static { System.loadLibrary("agconnect-tun"); }
    private final long handle;
    private final Thread responses;
    private final Object writes = new Object();
    private volatile boolean closed;

    public NativeTunnel(int socksPort, Consumer<byte[]> sink, Consumer<IOException> failure) throws IOException {
        handle = nativeOpen(LocalServerConfig.tunnelConfig(socksPort));
        if (handle == 0) throw new IOException("Cannot start local TCP stack");
        responses = new Thread(() -> {
            try {
                while (!closed) {
                    byte[] packet = nativeRead(handle);
                    if (closed) break;
                    if (packet == null || !nativeAlive(handle)) throw new IOException("Local TCP stack stopped");
                    if (packet.length != 0) sink.accept(packet);
                }
            } catch (IOException | RuntimeException error) {
                if (!closed) failure.accept(new IOException("Local TCP forwarding failed", error));
            }
        }, "local-tcp-packets");
        responses.setDaemon(true); responses.start();
    }

    public boolean send(byte[] packet) throws IOException {
        synchronized (writes) { return !closed && nativeWrite(handle, packet); }
    }

    @Override public void close() {
        synchronized (writes) {
            if (closed) return;
            closed = true; nativeStop(handle);
        }
        boolean interrupted = false;
        while (Thread.currentThread() != responses && responses.isAlive()) {
            try { responses.join(); } catch (InterruptedException error) { interrupted = true; }
        }
        nativeDestroy(handle);
        if (interrupted) Thread.currentThread().interrupt();
    }

    private static native long nativeOpen(String configuration) throws IOException;
    private static native byte[] nativeRead(long handle) throws IOException;
    private static native boolean nativeWrite(long handle, byte[] packet) throws IOException;
    private static native boolean nativeAlive(long handle);
    private static native void nativeStop(long handle);
    private static native void nativeDestroy(long handle);
}
