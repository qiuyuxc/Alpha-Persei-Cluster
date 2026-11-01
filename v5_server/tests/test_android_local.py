"""Same-phone startup configuration and real loopback SDK listener contracts."""
import argparse
import contextlib
from http.client import HTTPConnection, HTTPSConnection
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import startup_network


class StartupNetworkTests(unittest.TestCase):
    def options(self, *arguments, detect=None):
        parser = argparse.ArgumentParser()
        startup_network.add_network_arguments(parser)
        args = parser.parse_args(arguments)
        return startup_network.resolve_network_options(args, detect or (lambda: "192.168.1.77"))

    def test_local_mode_uses_high_ports_and_separates_egress(self):
        detect = Mock(side_effect=AssertionError("Local startup must not require a physical network"))
        options = self.options("--android-local", detect=detect)
        self.assertEqual((options.host_ip, options.bind_host), ("127.0.0.1", "127.0.0.1"))
        self.assertEqual((options.https_port, options.http_port), (8443, 8080))
        self.assertIsNone(options.cdn_source_ip)
        detect.assert_not_called()

    def test_external_defaults_and_explicit_egress(self):
        options = self.options()
        self.assertEqual((options.host_ip, options.cdn_source_ip, options.bind_host),
                         ("192.168.1.77", "192.168.1.77", "0.0.0.0"))
        self.assertEqual((options.https_port, options.http_port), (443, 80))
        options = self.options("--host-ip", "192.168.1.20", "--cdn-source-ip", "192.168.1.21",
                               "--https-port", "9443", "--http-port", "0")
        self.assertEqual((options.host_ip, options.cdn_source_ip, options.https_port, options.http_port),
                         ("192.168.1.20", "192.168.1.21", 9443, 0))

    def test_local_custom_egress_is_independent_of_advertised_loopback(self):
        options = self.options("--android-local", "--cdn-source-ip", "192.168.1.21")
        self.assertEqual(options.host_ip, "127.0.0.1")
        self.assertEqual(options.cdn_source_ip, "192.168.1.21")

    def test_invalid_local_listeners_are_rejected(self):
        for arguments in (("--dns",), ("--host-ip", "192.168.1.21"), ("--https-port", "443"),
                          ("--http-port", "80"), ("--http-port", "0"), ("--game-port", "443"),
                          ("--gw-port", "0"), ("--game-port", "8080"), ("--battle-port", "53")):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.options("--android-local", *arguments)

    def test_invalid_ip_ports_and_virtual_egress_are_rejected(self):
        for arguments in (("--https-port", "-1"), ("--http-port", "65536"),
                          ("--host-ip", "localhost"), ("--host-ip", "::1")):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.options(*arguments)
        for source in ("127.0.0.1", "198.18.0.1", "10.111.0.1", "10.111.0.3"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.options("--android-local", "--cdn-source-ip", source)

    def test_actual_cli_rejects_low_port_dns_before_opening_database(self):
        server = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "never-opened.db"
            environment = dict(os.environ, ACCOUNT_DB_PATH=str(database))
            result = subprocess.run([sys.executable, str(server / "main.py"), "--android-local", "--dns",
                                     "--db", str(database)], cwd=server, env=environment, capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("不启动本机 53 端口", result.stderr)
            self.assertFalse(database.exists())


class LocalListenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="apc-local-listeners-")
        cls.root = Path(cls.folder.name)
        cls.environment = patch.dict(os.environ, {"ACCOUNT_DB_PATH": str(cls.root / "default.db")})
        cls.environment.start()
        import gen_cert
        import server_net
        cls.certificates, cls.net = gen_cert, server_net
        with patch.object(gen_cert, "get_all_lan_ips", return_value=["127.0.0.1"]):
            if not gen_cert.generate_dual_layer_certs(cls.folder.name, extra_ips=["127.0.0.1"], log=lambda *a, **k: None):
                raise AssertionError("Could not generate isolated test certificates")
        cls.cert, cls.key, cls.ca = (str(cls.root / name) for name in ("sdk_cert.pem", "sdk_key.pem", "sdk_ca.crt"))

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        cls.folder.cleanup()

    def app(self):
        from flask import Flask, jsonify
        app = Flask("local-listener-test")
        @app.route("/config")
        def config():
            return jsonify({"mode": "local"})
        return app

    def free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_real_http_https_listeners_and_hostname_validation(self):
        https_port, http_port = self.free_port(), self.free_port()
        https, http = self.net.make_sdk_servers(self.app(), https_port, self.cert, self.key,
                                                http_port=http_port, bind_host="127.0.0.1")
        servers = (https, http)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(all(server.server_address[0] == "127.0.0.1" for server in servers))
            for cls, port, extra in ((HTTPConnection, http_port, {}),
                                     (HTTPSConnection, https_port, {"context": ssl.create_default_context(cafile=self.ca)})):
                client = cls("127.0.0.1", port, timeout=3, **extra)
                try:
                    client.request("GET", "/config")
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), {"mode": "local"})
                finally:
                    client.close()
            context = ssl.create_default_context(cafile=self.ca)
            with socket.create_connection(("127.0.0.1", https_port), timeout=3) as raw:
                with context.wrap_socket(raw, server_hostname="open.ys4fun.com") as tls:
                    self.assertIsNotNone(tls.getpeercert())
            with socket.create_connection(("127.0.0.1", https_port), timeout=3) as raw:
                with self.assertRaises(ssl.SSLCertVerificationError):
                    context.wrap_socket(raw, server_hostname="unrelated.example")
        finally:
            for server in servers:
                server.shutdown(); server.server_close()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_http_disable_and_tls_binding_failure_release_ports(self):
        https, http = self.net.make_sdk_servers(self.app(), 0, self.cert, self.key, http_port=0, bind_host="127.0.0.1")
        self.assertIsNone(http)
        https.server_close()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0)); occupied.listen(1)
            occupied_port = occupied.getsockname()[1]
            http_port = self.free_port()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(OSError):
                self.net.make_sdk_servers(self.app(), occupied_port, self.cert, self.key,
                                          http_port=http_port, bind_host="127.0.0.1")
            with socket.socket() as released:
                released.bind(("127.0.0.1", http_port))

    def test_invalid_certificate_does_not_bind_either_listener(self):
        invalid = self.root / "invalid.pem"
        invalid.write_text("invalid test certificate")
        http_port = self.free_port()
        with self.assertRaises(ssl.SSLError):
            self.net.make_sdk_servers(self.app(), self.free_port(), str(invalid), self.key,
                                      http_port=http_port, bind_host="127.0.0.1")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", http_port))

    def test_external_mode_can_still_start_https_when_auxiliary_http_is_busy(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0)); occupied.listen(1)
            with contextlib.redirect_stderr(io.StringIO()):
                https, http = self.net.make_sdk_servers(
                    self.app(), 0, self.cert, self.key, http_port=occupied.getsockname()[1],
                    bind_host="127.0.0.1", require_http=False)
            try:
                self.assertIsNone(http)
                self.assertGreater(https.server_address[1], 0)
            finally:
                https.server_close()

    def test_game_and_gateway_bind_only_to_loopback(self):
        with patch("fatigue_service.start_fatigue_ticker"):
            game = self.net.start_game_server(0, None, None, None, None, None, bind_host="127.0.0.1")
        gateway = self.net.start_gateway_server(0, None, None, None, None, None,
                                                host_ip="127.0.0.1", bind_host="127.0.0.1")
        try:
            self.assertEqual(game.server_address[0], "127.0.0.1")
            self.assertEqual(gateway.server_address[0], "127.0.0.1")
            self.assertEqual(gateway.host_ip, "127.0.0.1")
        finally:
            game.server_close(); gateway.server_close()

    def test_cdn_auto_detection_does_not_reuse_client_loopback(self):
        import cdn_proxy
        with patch.object(cdn_proxy, "_SOURCE_IP", "192.168.1.10"), \
                patch.object(cdn_proxy, "_DNS_CACHE", {"old.example": (0, [])}), \
                patch.object(self.certificates, "get_default_lan_ip", return_value="192.168.1.20"):
            cdn_proxy.set_source_ip(None)
            self.assertFalse(cdn_proxy._DNS_CACHE)
            self.assertEqual(cdn_proxy._source_ip(), "192.168.1.20")
            for address in ("127.0.0.1", "10.111.0.3", "198.18.0.1"):
                with self.subTest(address=address), self.assertRaises(ValueError):
                    cdn_proxy.set_source_ip(address)


if __name__ == "__main__":
    unittest.main()
