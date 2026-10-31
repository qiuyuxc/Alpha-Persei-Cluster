package io.github.qiuyuxc.agconnect;

import java.io.Closeable;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.util.Arrays;
import java.util.List;

/** Forward exact wire queries to physical-network DNS with UDP/TCP fallback. */
public final class DnsForwarder {
    public interface Transport {
        DatagramSocket udp() throws IOException;
        Socket tcp() throws IOException;
        default void release(Closeable socket) {
            try { socket.close(); } catch (IOException ignored) { }
        }
    }

    private final List<InetSocketAddress> upstreams;
    private final Transport transport;
    private final int timeout;

    public DnsForwarder(List<InetSocketAddress> upstreams, Transport transport, int timeout) {
        if (upstreams.isEmpty() || timeout <= 0)
            throw new IllegalArgumentException("DNS needs upstreams and a positive timeout");
        this.upstreams = upstreams; this.transport = transport; this.timeout = timeout;
    }

    public byte[] resolve(byte[] query) throws IOException {
        DnsMessage.parseQuery(query);
        IOException last = null;
        for (InetSocketAddress upstream : upstreams) {
            try {
                byte[] answer = udp(query, upstream);
                return DnsMessage.truncated(answer) ? tcp(query, upstream) : answer;
            } catch (IOException failure) {
                last = failure;
                // UDP can be filtered independently of TCP.
                try { return tcp(query, upstream); }
                catch (IOException tcpFailure) { last = tcpFailure; }
            }
        }
        throw last == null ? new IOException("DNS unavailable") : last;
    }

    private byte[] udp(byte[] query, InetSocketAddress upstream) throws IOException {
        DatagramSocket socket = transport.udp();
        try {
            socket.connect(upstream);
            socket.send(new DatagramPacket(query, query.length));
            long deadline = System.nanoTime() + timeout * 1000000L;
            byte[] buffer = new byte[DnsMessage.MAX_SIZE];
            while (true) {
                long remaining = (deadline - System.nanoTime() + 999999L) / 1000000L;
                if (remaining <= 0) throw new SocketTimeoutException("DNS response timeout");
                socket.setSoTimeout((int) Math.min(timeout, remaining));
                DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                socket.receive(packet);
                byte[] answer = Arrays.copyOfRange(packet.getData(), packet.getOffset(),
                        packet.getOffset() + packet.getLength());
                if (DnsMessage.matches(query, answer)) return answer;
            }
        } finally { transport.release(socket); }
    }

    private byte[] tcp(byte[] query, InetSocketAddress upstream) throws IOException {
        Socket socket = transport.tcp();
        try {
            socket.connect(upstream, timeout); socket.setSoTimeout(timeout);
            DataOutputStream output = new DataOutputStream(socket.getOutputStream());
            output.writeShort(query.length); output.write(query); output.flush();
            DataInputStream input = new DataInputStream(socket.getInputStream());
            int size = input.readUnsignedShort();
            if (size < 12) throw new IOException("short DNS TCP response");
            byte[] answer = new byte[size]; input.readFully(answer);
            if (!DnsMessage.matches(query, answer) || DnsMessage.truncated(answer))
                throw new IOException("invalid DNS TCP response");
            return answer;
        } finally { transport.release(socket); }
    }
}
