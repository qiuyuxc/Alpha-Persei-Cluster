#!/usr/bin/env python3
"""Exercise real TCP/TLS through the native packet bridge and Java forwarder.

Run with Termux Python for an Android ARM64 library. No TUN device, root,
public network, game client or production certificate is used by this test.
"""
import argparse
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import selectors
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time

CHECKS = 0
SOURCE = socket.inet_aton("10.111.0.1")
DESTINATION = socket.inet_aton("10.111.0.3")
MASK = 0xFFFFFFFF


def check(condition, description):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(description)


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack("!" + "H" * (len(data) // 2), data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return (~total) & 65535


class Library:
    def __init__(self, path):
        self.native = ctypes.CDLL(str(path), use_errno=True)
        declarations = {
            "agconnect_open": ([ctypes.c_char_p], ctypes.c_void_p),
            "agconnect_read": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int], ctypes.c_ssize_t),
            "agconnect_write": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_ssize_t),
            "agconnect_alive": ([ctypes.c_void_p], ctypes.c_int),
            "agconnect_stop": ([ctypes.c_void_p], None),
            "agconnect_destroy": ([ctypes.c_void_p], None),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.native, name)
            function.argtypes, function.restype = arguments, result
        self.handle = None
        self.buffer = ctypes.create_string_buffer(65535)

    def open(self, port):
        config = ("tunnel:\n  mtu: 1500\n  ipv4: 10.111.0.1\nsocks5:\n  address: 127.0.0.1\n  port: "
                  + str(port) + "\nmisc:\n  max-session-count: 32\n  connect-timeout: 3000\n"
                  "  tcp-read-write-timeout: 120000\n  log-level: error\n")
        self.handle = self.native.agconnect_open(config.encode())
        check(bool(self.handle), "native stack initializes before returning")

    def send(self, packet):
        if not self.handle:
            raise IOError("Native stack closed")
        result = self.native.agconnect_write(self.handle, packet, len(packet))
        if result != len(packet):
            raise OSError(ctypes.get_errno(), "Could not queue native packet")

    def receive(self, port, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.handle:
                raise IOError("Native stack closed")
            size = self.native.agconnect_read(self.handle, self.buffer, len(self.buffer),
                                              max(1, min(250, int((deadline - time.monotonic()) * 1000))))
            if size == -2:
                continue
            if size <= 0:
                raise IOError("Native packet reader stopped")
            packet = self.buffer.raw[:size]
            ihl = (packet[0] & 15) * 4
            if len(packet) >= ihl + 20 and struct.unpack_from("!H", packet, ihl + 2)[0] == port:
                check(packet[12:16] == DESTINATION and packet[16:20] == SOURCE, "native reply preserves virtual IPs")
                check(checksum(packet[:ihl]) == 0, "valid reply IPv4 checksum")
                pseudo = packet[12:20] + struct.pack("!BBH", 0, 6, len(packet) - ihl)
                check(checksum(pseudo + packet[ihl:]) == 0, "valid reply TCP checksum")
                return packet, ihl
        raise TimeoutError("Timed out waiting for a native TCP packet")

    def close(self):
        if self.handle:
            started = time.monotonic()
            self.native.agconnect_stop(self.handle)
            check(not self.native.agconnect_alive(self.handle), "native stop disables packet access")
            self.native.agconnect_destroy(self.handle)
            self.handle = None
            check(time.monotonic() - started < 2, "native shutdown joins within two seconds")


class Tcp:
    def __init__(self, tunnel, source_port, destination_port=443, drop_response=False):
        self.tunnel, self.port, self.destination_port = tunnel, source_port, destination_port
        self.tx, self.acked, self.rx = 17000 + source_port, 17000 + source_port, 0
        self.data = bytearray()
        self.pending, self.fin_at, self.remote_fin = {}, None, False
        self.drop_response, self.dropped, self.retransmissions, self.reordered = drop_response, None, 0, False
        self.send_packet(self.tx, 2, options=b"\x02\x04\x02\x00")
        self.tx += 1
        packet, ihl = tunnel.receive(source_port, 5)
        _, _, sequence, acknowledgment, _, flags = struct.unpack_from("!HHIIBB", packet, ihl)
        check(flags & 18 == 18 and acknowledgment == self.tx, "native TCP SYN/ACK handshake")
        self.rx, self.acked = (sequence + 1) & MASK, acknowledgment
        self.send_packet(self.tx, 16)

    def send_packet(self, sequence, flags, body=b"", options=b""):
        tcp = struct.pack("!HHIIBBHHH", self.port, self.destination_port, sequence & MASK, self.rx,
                          ((20 + len(options)) // 4) << 4, flags, 8192, 0, 0) + options + body
        pseudo = SOURCE + DESTINATION + struct.pack("!BBH", 0, 6, len(tcp))
        tcp = tcp[:16] + struct.pack("!H", checksum(pseudo + tcp)) + tcp[18:]
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0, SOURCE, DESTINATION)
        ip = ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:]
        self.tunnel.send(ip + tcp)

    def pump(self, timeout=5):
        packet, ihl = self.tunnel.receive(self.port, timeout)
        _, _, sequence, acknowledgment, offset, flags = struct.unpack_from("!HHIIBB", packet, ihl)
        body = packet[ihl + ((offset >> 4) * 4):]
        if flags & 4:
            raise ConnectionResetError("Native stream reset (destination unavailable or rejected)")
        if body and self.drop_response and self.dropped is None:
            self.dropped = sequence
            return  # Model loss before the client acknowledges the packet.
        if sequence == self.dropped and body:
            self.retransmissions += 1
        if flags & 16 and ((acknowledgment - self.acked) & MASK) < 0x80000000:
            self.acked = acknowledgment
        if body and ((sequence - self.rx) & MASK) < 0x80000000:
            self.pending[sequence] = body
        while self.rx in self.pending:
            chunk = self.pending.pop(self.rx)
            self.data.extend(chunk)
            self.rx = (self.rx + len(chunk)) & MASK
        if flags & 1:
            self.fin_at = (sequence + len(body)) & MASK
        if self.fin_at == self.rx:
            self.rx = (self.rx + 1) & MASK
            self.remote_fin = True
        if body or flags & 1:
            self.send_packet(self.tx, 16)

    def send(self, data, reorder=False):
        chunks = [data[index:index + 512] for index in range(0, len(data), 512)]
        for offset in range(0, len(chunks), 8):
            group = chunks[offset:offset + 8]
            frames, sequence = [], self.tx
            for chunk in group:
                frames.append((sequence, chunk)); sequence = (sequence + len(chunk)) & MASK
            if reorder and not self.reordered and len(frames) >= 2:
                frames[0], frames[1] = frames[1], frames[0]; self.reordered = True
            self.tx = sequence
            for sequence, chunk in frames:
                self.send_packet(sequence, 24, chunk)
            deadline = time.monotonic() + 5
            while ((self.acked - self.tx) & MASK) >= 0x80000000:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Native stack did not acknowledge the transmitted stream")
                self.pump(remaining)

    def receive(self):
        while not self.data and not self.remote_fin:
            self.pump()
        result = bytes(self.data); self.data.clear()
        return result

    def close(self, abort=False):
        if not self.tunnel.handle:
            return
        self.send_packet(self.tx, 20 if abort else 17)
        self.tx = (self.tx + 1) & MASK
        if not abort:
            deadline = time.monotonic() + 2
            while not self.remote_fin and time.monotonic() < deadline:
                self.pump(max(0.01, deadline - time.monotonic()))


class Tls:
    def __init__(self, tcp, context, hostname):
        self.tcp = tcp
        self.input, self.output = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.ssl = context.wrap_bio(self.input, self.output, server_hostname=hostname)
        while True:
            try:
                self.ssl.do_handshake(); self.flush(); break
            except ssl.SSLWantReadError:
                self.flush(); self.feed()
            except ssl.SSLError:
                self.flush(); raise

    def flush(self, reorder=False):
        data = self.output.read()
        if data:
            self.tcp.send(data, reorder=reorder)

    def feed(self):
        data = self.tcp.receive()
        if data:
            self.input.write(data)
        else:
            self.input.write_eof()

    def send(self, data, reorder=False):
        self.ssl.write(data); self.flush(reorder)

    def receive(self):
        while True:
            try:
                return self.ssl.read(65535)
            except ssl.SSLWantReadError:
                self.flush(); self.feed()


def http_response(stream):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = stream.receive()
        if not chunk:
            raise EOFError("Stream closed before the HTTP response header")
        data.extend(chunk)
    header, body = bytes(data).split(b"\r\n\r\n", 1)
    length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:"))
    while len(body) < length:
        chunk = stream.receive()
        if not chunk:
            raise EOFError("Stream closed before the complete HTTP response body")
        body += chunk
    check(header.split(b"\r\n", 1)[0].split()[1] == b"200", "local SDK HTTP response succeeds")
    check(len(body) == length, "response framing preserves the complete body")
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--classes", type=Path, required=True, help="build/core from build.py --test-only")
    parser.add_argument("--java", default="java")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"passed": False, "library_sha256": hashlib.sha256(args.library.read_bytes()).hexdigest(),
              "tests": [], "uses_tun_device": False, "external_network": False}
    if args.output and args.output.exists():
        parser.error("Output already exists")
    native = Library(args.library.resolve())
    check(not native.native.agconnect_open(b"x" * 16385), "invalid configuration cannot allocate a native instance")
    body = bytes(range(256)) * 384
    hostnames = []
    harness, servers, threads = None, [], []
    try:
        with tempfile.TemporaryDirectory(prefix="agconnect-tls-") as folder:
            certificate, key = Path(folder) / "test.crt", Path(folder) / "test.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                            "-nodes", "-days", "1", "-subj", "/CN=open.ys4fun.com",
                            "-addext", "subjectAltName=DNS:open.ys4fun.com,DNS:prod-api-activity.ys4fun.com",
                            "-keyout", str(key), "-out", str(certificate)], check=True, capture_output=True)
            ca_bytes = certificate.read_bytes()
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def respond(self, data):
                    self.send_response(200); self.send_header("Content-Length", str(len(data)))
                    self.end_headers(); self.wfile.write(data)
                def do_GET(self):
                    self.respond(ca_bytes if self.path == "/cert" else body)
                def do_POST(self):
                    received = self.rfile.read(int(self.headers["Content-Length"]))
                    self.respond(hashlib.sha256(received).hexdigest().encode())
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(certificate), str(key))
            context.set_servername_callback(lambda peer, name, ctx: hostnames.append(name))
            https = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            https.socket = context.wrap_socket(https.socket, server_side=True)
            http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            servers = [https, http]
            for server in servers:
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); threads.append(thread)
            harness = subprocess.Popen([args.java, "-cp", str(args.classes.resolve()), "LocalForwarderHarness",
                                        str(https.server_port), str(http.server_port)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            with selectors.DefaultSelector() as selector:
                selector.register(harness.stdout, selectors.EVENT_READ)
                check(bool(selector.select(10)), "Java local forwarder starts within ten seconds")
            ready = harness.stdout.readline().strip()
            check(ready.startswith("READY "), "Java local forwarder publishes its bound loopback port")
            native.open(int(ready.split()[1]))
            plain = Tcp(native, 44000, destination_port=80)
            plain.send(b"GET /cert HTTP/1.0\r\nHost: open.ys4fun.com\r\n\r\n")
            check(http_response(plain) == ca_bytes, "virtual TCP:80 reaches the local HTTP certificate endpoint")
            plain.close(); report["tests"].append("HTTP 80 mapping and original certificate bytes")
            print("PASS native TCP -> Java -> local HTTP", flush=True)

            trusted = ssl.create_default_context(cafile=str(certificate))
            for index, host in enumerate(("open.ys4fun.com", "prod-api-activity.ys4fun.com")):
                tcp = Tcp(native, 44001 + index, drop_response=index == 1)
                tls = Tls(tcp, trusted, host)
                check(tls.ssl.getpeercert() is not None, "TLS verifies the original server certificate")
                tls.send(b"GET /config HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
                check(http_response(tls) == body, "96 KiB TLS response survives native segmentation")
                if index == 1:
                    check(tcp.dropped is not None and tcp.retransmissions > 0, "TLS succeeds after TCP response packet loss")
                tcp.close()
            check(hostnames[:2] == ["open.ys4fun.com", "prod-api-activity.ys4fun.com"], "TLS SNI is preserved")
            report["tests"].append("TLS certificate/SNI, segmentation and retransmission")
            print("PASS native TLS, original hostnames and packet retransmission", flush=True)

            tcp = Tcp(native, 44003)
            tls = Tls(tcp, trusted, "open.ys4fun.com")
            payload = bytes(range(256)) * 256
            tls.send(b"POST /echo HTTP/1.0\r\nHost: open.ys4fun.com\r\nContent-Length: "
                     + str(len(payload)).encode() + b"\r\n\r\n" + payload, reorder=True)
            check(tcp.reordered, "request packets were deliberately delivered out of order")
            check(http_response(tls) == hashlib.sha256(payload).hexdigest().encode(), "64 KiB reordered TLS request is unchanged")
            tcp.close(); report["tests"].append("Out-of-order native TCP carrying a TLS request")
            print("PASS native TLS with reordered request packets", flush=True)

            tcp = Tcp(native, 44004)
            try:
                Tls(tcp, trusted, "unrelated.example")
            except ssl.SSLCertVerificationError:
                check(True, "wrong TLS hostname remains rejected")
            else:
                raise AssertionError("Wrong TLS hostname was accepted")
            finally:
                tcp.close(abort=True)
            report["tests"].append("TLS rejects an unrelated hostname")

            denied = Tcp(native, 44005, destination_port=8105)
            try:
                check(denied.receive() == b"", "unrelated TCP port closes without forwarding data")
            except ConnectionResetError:
                check(True, "unrelated TCP port is not forwarded")
            report["tests"].append("Unrelated destination port rejection")

            blocked = Tcp(native, 44006, destination_port=80)
            blocked.send(b"GET /")  # An open stream awaiting the rest of its HTTP header.
            native.close(); report["tests"].append("Stop with an active local stream")
            native.open(int(ready.split()[1])); native.close()
            report["tests"].append("Restart after native shutdown")
            stdout, stderr = harness.communicate("\n", timeout=5)
            check(harness.returncode == 0 and "STOPPED" in stdout, "Java local forwarder also stops cleanly")
            report["forwarder_output"] = stdout.strip()
            check(not stderr, "Java local forwarder emitted no runtime error")
            report["passed"] = True
    finally:
        native.close()
        if harness is not None and harness.poll() is None:
            harness.kill(); harness.communicate(timeout=5)
        for server in servers:
            server.shutdown(); server.server_close()
        for thread in threads:
            thread.join(2)
        report["checks"] = CHECKS
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
