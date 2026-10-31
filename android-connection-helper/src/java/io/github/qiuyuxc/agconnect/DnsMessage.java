package io.github.qiuyuxc.agconnect;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/** DNS wire parsing and exact-host synthesis, independent of Android. */
public final class DnsMessage {
    public static final int MAX_SIZE = 65535;
    public static final int FORMERR = 1;
    public static final int SERVFAIL = 2;
    private DnsMessage() { }

    public static final class Question {
        public final String name;
        public final int type, clazz, udpLimit;
        private final byte[] encoded;

        private Question(String name, int type, int clazz, byte[] encoded, int udpLimit) {
            this.name = name; this.type = type; this.clazz = clazz;
            this.encoded = encoded; this.udpLimit = udpLimit;
        }

        public boolean redirected() {
            return matchesHost(encoded, "open.ys4fun.com")
                    || matchesHost(encoded, "prod-api-activity.ys4fun.com");
        }
    }

    private static boolean matchesHost(byte[] encoded, String host) {
        int offset = 0;
        for (String label : host.split("\\.")) {
            if (offset >= encoded.length || (encoded[offset++] & 255) != label.length()) return false;
            for (int i = 0; i < label.length(); i++) {
                if (offset >= encoded.length) return false;
                int value = encoded[offset++] & 255;
                if (value >= 'A' && value <= 'Z') value += 'a' - 'A';
                if (value != label.charAt(i)) return false;
            }
        }
        return offset + 5 == encoded.length && encoded[offset] == 0;
    }

    private static final class Name {
        final String value;
        final byte[] encoded;
        final int end;
        Name(String value, byte[] encoded, int end) {
            this.value = value; this.encoded = encoded; this.end = end;
        }
    }

    static int u16(byte[] data, int offset) {
        return ((data[offset] & 255) << 8) | (data[offset + 1] & 255);
    }

    static void put16(byte[] data, int offset, int value) {
        data[offset] = (byte) (value >>> 8); data[offset + 1] = (byte) value;
    }

    private static Name name(byte[] data, int offset) {
        Set<Integer> visited = new HashSet<>();
        StringBuilder text = new StringBuilder();
        ByteArrayOutputStream encoded = new ByteArrayOutputStream();
        int end = -1, size = 1;
        while (true) {
            if (offset >= data.length || !visited.add(offset))
                throw new IllegalArgumentException("truncated or cyclic DNS name");
            int count = data[offset] & 255;
            if ((count & 0xc0) == 0xc0) {
                if (offset + 1 >= data.length)
                    throw new IllegalArgumentException("truncated DNS pointer");
                int target = u16(data, offset) & 0x3fff;
                // RFC 1035 pointers refer to a prior occurrence, never the header.
                if (target < 12 || target >= offset)
                    throw new IllegalArgumentException("invalid DNS pointer");
                if (end < 0) end = offset + 2;
                offset = target;
                continue;
            }
            if ((count & 0xc0) != 0) throw new IllegalArgumentException("invalid DNS label");
            offset++;
            if (count == 0) {
                encoded.write(0);
                return new Name(text.toString().toLowerCase(Locale.ROOT),
                        encoded.toByteArray(), end < 0 ? offset : end);
            }
            size += count + 1;
            if (offset + count > data.length || size > 255)
                throw new IllegalArgumentException("oversized DNS name");
            if (text.length() > 0) text.append('.');
            // Keep the original wire labels when constructing local responses.
            text.append(new String(data, offset, count, StandardCharsets.ISO_8859_1));
            encoded.write(count);
            encoded.write(data, offset, count);
            offset += count;
        }
    }

    private static Question question(byte[] data, boolean response) {
        if (data == null || data.length < 12 || data.length > MAX_SIZE
                || (u16(data, 2) & 0x8000) != (response ? 0x8000 : 0)
                || (u16(data, 2) & 0x7800) != 0 || u16(data, 4) != 1)
            throw new IllegalArgumentException("expected a standard single-question DNS message");
        Name name = name(data, 12);
        if (name.end + 4 > data.length) throw new IllegalArgumentException("short DNS question");
        int type = u16(data, name.end), clazz = u16(data, name.end + 2);
        byte[] encoded = Arrays.copyOf(name.encoded, name.encoded.length + 4);
        put16(encoded, encoded.length - 4, type); put16(encoded, encoded.length - 2, clazz);
        int limit = 512;
        if (!response) {
            if (u16(data, 6) != 0 || u16(data, 8) != 0)
                throw new IllegalArgumentException("unexpected query records");
            int offset = name.end + 4;
            for (int i = 0; i < u16(data, 10); i++) {
                Name record = name(data, offset);
                offset = record.end;
                if (offset + 10 > data.length)
                    throw new IllegalArgumentException("short additional record");
                int recordType = u16(data, offset), length = u16(data, offset + 8);
                if (recordType == 41 && record.value.isEmpty())
                    limit = Math.max(512, Math.min(1232, u16(data, offset + 2)));
                offset += 10 + length;
                if (offset > data.length) throw new IllegalArgumentException("short additional data");
            }
            if (offset != data.length) throw new IllegalArgumentException("trailing query data");
        }
        return new Question(name.value, type, clazz, encoded, limit);
    }

    public static Question parseQuery(byte[] data) { return question(data, false); }

    private static byte[] response(byte[] query, Question q, int code, boolean truncated, byte[] address) {
        boolean answer = address != null && q != null && q.type == 1 && q.clazz == 1 && code == 0;
        int questionLength = q == null ? 0 : q.encoded.length;
        byte[] data = new byte[12 + questionLength + (answer ? 16 : 0)];
        data[0] = query[0]; data[1] = query[1];
        // Preserve RD/CD; never claim DNSSEC authentication for synthetic data.
        put16(data, 2, 0x8080 | (u16(query, 2) & 0x0110) | code | (truncated ? 0x0200 : 0));
        put16(data, 4, q == null ? 0 : 1); put16(data, 6, answer ? 1 : 0);
        if (q != null) System.arraycopy(q.encoded, 0, data, 12, questionLength);
        if (answer) {
            int offset = 12 + questionLength;
            put16(data, offset, 0xc00c); put16(data, offset + 2, 1);
            put16(data, offset + 4, 1); put16(data, offset + 8, 30);
            put16(data, offset + 10, 4);
            System.arraycopy(address, 0, data, offset + 12, 4);
        }
        return data;
    }

    /** Null means forward unchanged; AAAA/HTTPS for redirected hosts return NODATA. */
    public static byte[] localResponse(byte[] query, ServerAddress server) {
        Question q = parseQuery(query);
        return q.redirected() ? response(query, q, 0, false, server.bytes()) : null;
    }

    public static byte[] error(byte[] query, int code) {
        if (query == null || query.length < 12 || (query[2] & 0x80) != 0) return null;
        Question q = null;
        try { q = parseQuery(query); } catch (IllegalArgumentException ignored) { }
        return response(query, q, code, false, null);
    }

    public static byte[] forUdp(byte[] query, byte[] answer) {
        Question q = parseQuery(query);
        return answer.length <= q.udpLimit ? answer : response(query, q, 0, true, null);
    }

    public static boolean matches(byte[] query, byte[] answer) {
        try {
            Question a = parseQuery(query), b = question(answer, true);
            return query[0] == answer[0] && query[1] == answer[1]
                    && a.name.equals(b.name) && a.type == b.type && a.clazz == b.clazz;
        } catch (IllegalArgumentException | IndexOutOfBoundsException ignored) { return false; }
    }

    public static boolean truncated(byte[] response) { return (u16(response, 2) & 0x0200) != 0; }
}
