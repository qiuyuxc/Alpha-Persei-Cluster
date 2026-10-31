package io.github.qiuyuxc.agconnect;

import java.io.ByteArrayOutputStream;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Consumer;

/**
 * A bounded DNS-only endpoint. Ordinary game TCP/UDP never enters this engine.
 * TCP DNS handles framing, duplicate/out-of-order data, MSS and retransmission.
 */
public final class DnsTunnel {
    public interface Resolver { void resolve(byte[] query, Consumer<byte[]> reply); }
    public interface Sink { void send(byte[] packet); }
    private static final byte[] EMPTY = new byte[0];
    private static final int MAX_SESSIONS = 32, MAX_PENDING = 4;
    private static final long IDLE_MS = 30000, RETRY_MS = 1000;
    private final Resolver resolver;
    private final Sink sink;
    private final Map<String, Session> sessions = new HashMap<>();
    private volatile boolean closed;
    private long now;

    public DnsTunnel(Resolver resolver, Sink sink) { this.resolver = resolver; this.sink = sink; }

    public void accept(byte[] data, long clock) {
        if (closed) return;
        Ipv4Packet packet = Ipv4Packet.parse(data);
        if (packet == null) return;
        if (packet.protocol == 17) {
            resolve(packet.payload, answer -> {
                if (closed || answer == null) return;
                try { sink.send(packet.udpReply(DnsMessage.forUdp(packet.payload, answer))); }
                catch (IllegalArgumentException ignored) {
                    byte[] error = DnsMessage.error(packet.payload, DnsMessage.FORMERR);
                    if (error != null) sink.send(packet.udpReply(error));
                }
            });
            return;
        }
        synchronized (this) {
            now = clock;
            if (closed) return;
            tcp(packet);
        }
    }

    private void resolve(byte[] query, Consumer<byte[]> reply) {
        try {
            DnsMessage.parseQuery(query);
            resolver.resolve(query, answer -> {
                if (closed) return;
                reply.accept(answer != null && DnsMessage.matches(query, answer)
                        ? answer : DnsMessage.error(query, DnsMessage.SERVFAIL));
            });
        } catch (IllegalArgumentException ignored) {
            reply.accept(DnsMessage.error(query, DnsMessage.FORMERR));
        }
    }

    private static long plus(long value, int count) { return (value + count) & 0xffffffffL; }

    private final class Session {
        final Ipv4Packet flow;
        final long initialClient;
        long clientNext, serverNext, touched, sentAt;
        boolean established, peerClosed, finSent;
        int retries, pending, queuedBytes, remoteWindow;
        byte[] outstanding, input = EMPTY, sending = EMPTY;
        int sendOffset;
        final ArrayDeque<byte[]> responses = new ArrayDeque<>();

        Session(Ipv4Packet flow) {
            this.flow = flow; initialClient = flow.sequence;
            clientNext = plus(flow.sequence, 1);
            serverNext = ThreadLocalRandom.current().nextLong(0x100000000L);
            touched = now; remoteWindow = flow.window;
        }

        void acknowledge() { sink.send(flow.tcpReply(serverNext, clientNext, 0x10, EMPTY)); }

        void reliable(int flags, byte[] data) {
            outstanding = flow.tcpReply(serverNext, clientNext, flags, data);
            serverNext = plus(serverNext, data.length + ((flags & 3) != 0 ? 1 : 0));
            sentAt = now; retries = 0;
            sink.send(outstanding);
        }

        void reset() {
            sink.send(flow.tcpReply(serverNext, clientNext, 0x14, EMPTY));
            sessions.remove(flow.key());
        }

        void flush() {
            if (!established || outstanding != null) return;
            if (sendOffset == sending.length) {
                sending = responses.isEmpty() ? EMPTY : responses.removeFirst();
                sendOffset = 0;
            }
            if (sendOffset < sending.length) {
                int length = Math.min(remoteWindow, Math.min(flow.mss, sending.length - sendOffset));
                if (length == 0) return;
                byte[] data = Arrays.copyOfRange(sending, sendOffset, sendOffset + length);
                sendOffset += length; queuedBytes -= length;
                reliable(0x18, data);
            } else if (peerClosed && pending == 0 && !finSent) {
                finSent = true; reliable(0x11, EMPTY);
            }
        }

        void receive(byte[] data) {
            if (input.length + data.length > DnsMessage.MAX_SIZE + 2) { reset(); return; }
            byte[] combined = Arrays.copyOf(input, input.length + data.length);
            System.arraycopy(data, 0, combined, input.length, data.length);
            input = combined;
            while (input.length >= 2 && sessions.get(flow.key()) == this) {
                int length = DnsMessage.u16(input, 0);
                if (length < 12) { reset(); return; }
                if (input.length < length + 2) break;
                if (pending >= MAX_PENDING) { reset(); return; }
                byte[] query = Arrays.copyOfRange(input, 2, length + 2);
                input = Arrays.copyOfRange(input, length + 2, input.length);
                pending++;
                resolve(query, answer -> {
                    synchronized (DnsTunnel.this) {
                        if (closed || sessions.get(flow.key()) != this) return;
                        pending--;
                        if (answer == null || answer.length > DnsMessage.MAX_SIZE
                                || queuedBytes + answer.length + 2 > 4 * (DnsMessage.MAX_SIZE + 2)) {
                            reset(); return;
                        }
                        ByteArrayOutputStream framed = new ByteArrayOutputStream(answer.length + 2);
                        framed.write(answer.length >>> 8); framed.write(answer.length);
                        framed.write(answer, 0, answer.length);
                        responses.addLast(framed.toByteArray());
                        queuedBytes += answer.length + 2;
                        flush();
                    }
                });
            }
        }
    }

    private void tcp(Ipv4Packet packet) {
        Session session = sessions.get(packet.key());
        if ((packet.flags & 4) != 0) { sessions.remove(packet.key()); return; }
        if ((packet.flags & 2) != 0) {
            if ((packet.flags & 0x10) != 0 || packet.payload.length != 0) return;
            if (session != null && !session.established && session.initialClient == packet.sequence) {
                sink.send(session.outstanding); return;
            }
            if (session != null) session.reset();
            if (sessions.size() >= MAX_SESSIONS) {
                sink.send(packet.tcpReply(0, plus(packet.sequence, 1), 0x14, EMPTY)); return;
            }
            session = new Session(packet);
            sessions.put(packet.key(), session);
            session.reliable(0x12, EMPTY);
            return;
        }
        if (session == null) {
            sink.send((packet.flags & 0x10) != 0
                    ? packet.tcpReply(packet.acknowledgement, 0, 4, EMPTY)
                    : packet.tcpReply(0, plus(packet.sequence,
                            packet.payload.length + ((packet.flags & 1) != 0 ? 1 : 0)), 0x14, EMPTY));
            return;
        }
        session.touched = now;
        if ((packet.flags & 0x10) == 0) return;
        session.remoteWindow = packet.window;
        if (session.outstanding != null && packet.acknowledgement == session.serverNext) {
            session.outstanding = null; session.established = true;
            if (session.finSent) { sessions.remove(packet.key()); return; }
        }
        if (!session.established) return;
        if (packet.sequence != session.clientNext) {
            session.acknowledge(); session.flush(); return;
        }
        if (packet.payload.length > 0) {
            if (session.peerClosed) { session.reset(); return; }
            session.clientNext = plus(session.clientNext, packet.payload.length);
            session.acknowledge();
            session.receive(packet.payload);
            if (sessions.get(packet.key()) != session) return;
        }
        if ((packet.flags & 1) != 0 && !session.peerClosed) {
            session.peerClosed = true; session.clientNext = plus(session.clientNext, 1);
            session.acknowledge();
            if (session.input.length != 0) { session.reset(); return; }
        }
        session.flush();
    }

    public synchronized void tick(long clock) {
        now = clock;
        if (closed) return;
        for (Session session : new ArrayList<>(sessions.values())) {
            if (now - session.touched >= IDLE_MS) { session.reset(); continue; }
            if (session.outstanding != null && now - session.sentAt >= RETRY_MS) {
                if (++session.retries > 4) session.reset();
                else { session.sentAt = now; sink.send(session.outstanding); }
            }
        }
    }

    public synchronized int sessionCount() { return sessions.size(); }
    public synchronized void close() { closed = true; sessions.clear(); }
}
