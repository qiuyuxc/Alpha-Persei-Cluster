import io.github.qiuyuxc.agconnect.*;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

final class TestSupport {
    static int checks;
    static void check(boolean condition, String message) {
        checks++;
        if (!condition) throw new AssertionError(message);
    }
    static int u16(byte[] b, int p) { return (b[p] & 255) * 256 + (b[p + 1] & 255); }
    static long u32(byte[] b, int p) { return ((long) u16(b, p) << 16) | u16(b, p + 2); }
    static void put16(byte[] b, int p, int value) {
        b[p] = (byte) (value >>> 8); b[p + 1] = (byte) value;
    }
    static void put32(byte[] b, int p, long value) {
        put16(b, p, (int) (value >>> 16)); put16(b, p + 2, (int) value);
    }
    static byte[] query(String host, int type) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] header = new byte[12]; put16(header, 0, 0x7152);
        put16(header, 2, 0x0100); put16(header, 4, 1);
        out.write(header, 0, header.length);
        for (String label : host.split("\\.")) {
            byte[] data = label.getBytes(StandardCharsets.ISO_8859_1);
            out.write(data.length); out.write(data, 0, data.length);
        }
        out.write(0); out.write(type >>> 8); out.write(type); out.write(0); out.write(1);
        return out.toByteArray();
    }
    static byte[] edns(byte[] query, int limit) {
        byte[] result = Arrays.copyOf(query, query.length + 11);
        put16(result, 10, 1);
        put16(result, query.length + 1, 41); put16(result, query.length + 3, limit);
        return result;
    }
    static byte[] answer(byte[] query, byte[] ip) {
        // RFC 1035 A answer with a pointer to the original question.
        byte[] data = Arrays.copyOf(query, query.length + 16);
        put16(data, 2, 0x8180); put16(data, 6, 1);
        int p = query.length;
        put16(data, p, 0xc00c); put16(data, p + 2, 1); put16(data, p + 4, 1);
        put16(data, p + 8, 60); put16(data, p + 10, 4);
        System.arraycopy(ip, 0, data, p + 12, 4);
        return data;
    }
    static byte[] largeAnswer(byte[] query, int size) {
        byte[] result = Arrays.copyOf(query, size);
        put16(result, 2, 0x8180); put16(result, 6, 1);
        int p = query.length;
        put16(result, p, 0xc00c); put16(result, p + 2, 16); put16(result, p + 4, 1);
        put16(result, p + 8, 60); put16(result, p + 10, size - p - 12);
        for (int i = p + 12; i < size;) {
            int length = Math.min(255, size - i - 1);
            result[i++] = (byte) length;
            Arrays.fill(result, i, i + length, (byte) 'x'); i += length;
        }
        return result;
    }
    static byte[] udp(byte[] dns) {
        byte[] data = new byte[28 + dns.length]; data[0] = 0x45;
        put16(data, 2, data.length); data[8] = 64; data[9] = 17;
        byte[] source = {10, 111, 0, 1}, destination = {10, 111, 0, 2};
        System.arraycopy(source, 0, data, 12, 4); System.arraycopy(destination, 0, data, 16, 4);
        put16(data, 20, 40500); put16(data, 22, 53); put16(data, 24, 8 + dns.length);
        System.arraycopy(dns, 0, data, 28, dns.length);
        return data;
    }
    static byte[] tcp(int port, long seq, long ack, int flags, byte[] body, int mss, int window) {
        int header = (flags & 2) != 0 ? 24 : 20;
        byte[] data = new byte[20 + header + body.length]; data[0] = 0x45;
        put16(data, 2, data.length); data[8] = 64; data[9] = 6;
        byte[] source = {10, 111, 0, 1}, destination = {10, 111, 0, 2};
        System.arraycopy(source, 0, data, 12, 4); System.arraycopy(destination, 0, data, 16, 4);
        put16(data, 20, port); put16(data, 22, 53); put32(data, 24, seq); put32(data, 28, ack);
        data[32] = (byte) ((header / 4) << 4); data[33] = (byte) flags; put16(data, 34, window);
        if (header == 24) { data[40] = 2; data[41] = 4; put16(data, 42, mss); }
        System.arraycopy(body, 0, data, 20 + header, body.length);
        return data;
    }
    static byte[] frame(byte[] dns) {
        byte[] out = new byte[dns.length + 2]; put16(out, 0, dns.length);
        System.arraycopy(dns, 0, out, 2, dns.length); return out;
    }
    static byte[] tcpBody(byte[] packet) {
        return Arrays.copyOfRange(packet, 20 + ((packet[32] >>> 4 & 15) * 4), packet.length);
    }
    static void checksums(byte[] data) {
        check(Ipv4Packet.checksum(data, 0, 20, 0) == 0, "IPv4 checksum");
        int pseudo = (data[9] & 255) + data.length - 20;
        for (int i = 12; i < 20; i += 2) pseudo += u16(data, i);
        check(Ipv4Packet.checksum(data, 20, data.length - 20, pseudo) == 0, "transport checksum");
    }
    static void rejected(Runnable action, String message) {
        try { action.run(); }
        catch (IllegalArgumentException expected) { check(true, message); return; }
        throw new AssertionError(message);
    }
    static void done(String name) { System.out.println("PASS " + name + ": " + checks + " checks"); }
}
