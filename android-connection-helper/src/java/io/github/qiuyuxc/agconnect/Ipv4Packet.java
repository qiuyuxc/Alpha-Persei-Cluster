package io.github.qiuyuxc.agconnect;

import java.util.Arrays;
import java.util.concurrent.atomic.AtomicInteger;

/** Only IPv4 UDP/TCP packets addressed to the private DNS /32 enter the engine. */
public final class Ipv4Packet {
    public static final String INTERFACE_ADDRESS = "10.111.0.1";
    public static final String DNS_ADDRESS = "10.111.0.2";
    private static final byte[] DNS_BYTES = {10, 111, 0, 2};
    private static final AtomicInteger IDS = new AtomicInteger();
    public final byte[] source, destination, payload;
    public final int sourcePort, destinationPort, protocol, flags, mss, window;
    public final long sequence, acknowledgement;

    private Ipv4Packet(byte[] source, byte[] destination, byte[] payload, int sourcePort,
                       int destinationPort, int protocol, int flags, int mss, int window,
                       long sequence, long acknowledgement) {
        this.source = source; this.destination = destination; this.payload = payload;
        this.sourcePort = sourcePort; this.destinationPort = destinationPort;
        this.protocol = protocol; this.flags = flags; this.mss = mss;
        this.window = window;
        this.sequence = sequence; this.acknowledgement = acknowledgement;
    }

    private static long u32(byte[] data, int offset) {
        return ((long) DnsMessage.u16(data, offset) << 16) | DnsMessage.u16(data, offset + 2);
    }

    private static void put32(byte[] data, int offset, long value) {
        DnsMessage.put16(data, offset, (int) (value >>> 16));
        DnsMessage.put16(data, offset + 2, (int) value);
    }

    public static Ipv4Packet parse(byte[] data) {
        if (data == null || data.length < 20 || (data[0] & 0xf0) != 0x40) return null;
        int header = (data[0] & 15) * 4;
        if (header < 20 || header > data.length || DnsMessage.u16(data, 2) != data.length
                || (DnsMessage.u16(data, 6) & 0x3fff) != 0
                || !Arrays.equals(Arrays.copyOfRange(data, 16, 20), DNS_BYTES)) return null;
        int protocol = data[9] & 255;
        if (protocol == 17) {
            if (header + 8 > data.length || DnsMessage.u16(data, header + 4) != data.length - header)
                return null;
            return flow(data, header, header + 8, protocol, 0, 1400, 0, 0, 0);
        }
        if (protocol != 6 || header + 20 > data.length) return null;
        int tcpHeader = (data[header + 12] >>> 4 & 15) * 4, mss = 1400;
        if (tcpHeader < 20 || header + tcpHeader > data.length) return null;
        int offset = header + 20;
        while (offset < header + tcpHeader) {
            int kind = data[offset] & 255;
            if (kind == 0) break;
            if (kind == 1) { offset++; continue; }
            if (offset + 2 > header + tcpHeader) return null;
            int size = data[offset + 1] & 255;
            if (size < 2 || offset + size > header + tcpHeader) return null;
            if (kind == 2 && size == 4) mss = Math.max(64, Math.min(1400, DnsMessage.u16(data, offset + 2)));
            offset += size;
        }
        return flow(data, header, header + tcpHeader, protocol, data[header + 13] & 255, mss,
                DnsMessage.u16(data, header + 14),
                u32(data, header + 4), u32(data, header + 8));
    }

    private static Ipv4Packet flow(byte[] data, int transport, int payload, int protocol,
                                   int flags, int mss, int window, long sequence, long acknowledgement) {
        if (DnsMessage.u16(data, transport + 2) != 53) return null;
        return new Ipv4Packet(Arrays.copyOfRange(data, 12, 16), Arrays.copyOfRange(data, 16, 20),
                Arrays.copyOfRange(data, payload, data.length), DnsMessage.u16(data, transport),
                53, protocol, flags, mss, window, sequence, acknowledgement);
    }

    public String key() {
        return (source[0] & 255) + "." + (source[1] & 255) + "." + (source[2] & 255)
                + "." + (source[3] & 255) + ":" + sourcePort;
    }

    private byte[] reply(int transportLength, int protocol) {
        byte[] data = new byte[20 + transportLength];
        data[0] = 0x45;
        DnsMessage.put16(data, 2, data.length); DnsMessage.put16(data, 4, IDS.incrementAndGet());
        data[8] = 64; data[9] = (byte) protocol;
        System.arraycopy(destination, 0, data, 12, 4);
        System.arraycopy(source, 0, data, 16, 4);
        DnsMessage.put16(data, 20, destinationPort); DnsMessage.put16(data, 22, sourcePort);
        return data;
    }

    public byte[] udpReply(byte[] dns) {
        if (dns.length > 65507) throw new IllegalArgumentException("oversized UDP DNS response");
        byte[] data = reply(8 + dns.length, 17);
        DnsMessage.put16(data, 24, 8 + dns.length);
        System.arraycopy(dns, 0, data, 28, dns.length);
        finish(data, 26);
        return data;
    }

    public byte[] tcpReply(long sequence, long acknowledgement, int flags, byte[] payload) {
        int header = (flags & 2) != 0 ? 24 : 20;
        byte[] data = reply(header + payload.length, 6);
        put32(data, 24, sequence); put32(data, 28, acknowledgement);
        data[32] = (byte) ((header / 4) << 4); data[33] = (byte) flags;
        DnsMessage.put16(data, 34, 65535);
        if (header == 24) {
            data[40] = 2; data[41] = 4; DnsMessage.put16(data, 42, 1400);
        }
        System.arraycopy(payload, 0, data, 20 + header, payload.length);
        finish(data, 36);
        return data;
    }

    private static void finish(byte[] data, int checksumOffset) {
        DnsMessage.put16(data, 10, checksum(data, 0, 20, 0));
        int sum = sum(data, 12, 8, 0) + (data[9] & 255) + data.length - 20;
        int transportChecksum = checksum(data, 20, data.length - 20, sum);
        DnsMessage.put16(data, checksumOffset, transportChecksum == 0 ? 65535 : transportChecksum);
    }

    private static int sum(byte[] data, int offset, int count, int initial) {
        int sum = initial, end = offset + count;
        while (offset + 1 < end) { sum += DnsMessage.u16(data, offset); offset += 2; }
        if (offset < end) sum += (data[offset] & 255) << 8;
        return sum;
    }

    public static int checksum(byte[] data, int offset, int count, int initial) {
        int sum = sum(data, offset, count, initial);
        while ((sum >>> 16) != 0) sum = (sum & 65535) + (sum >>> 16);
        return ~sum & 65535;
    }
}
