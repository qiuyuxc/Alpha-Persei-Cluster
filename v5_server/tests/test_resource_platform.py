"""Request-level platform selection and cache isolation; no player seed required."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import client_platform


class PlatformDetectionTests(unittest.TestCase):
    def test_explicit_platform_wins_over_conflicting_user_agent(self):
        for raw, expected in ((" Android ", "android"), ("iPad", "ios"), ("WindowsPlayer", "pc")):
            for ua in ("iPhone iOS", "Windows NT 10.0", "Dalvik/2.1.0 (Linux; Android 15)"):
                with self.subTest(raw=raw, ua=ua):
                    self.assertEqual(client_platform.detect_platform({"platformType": raw}, headers={"User-Agent": ua}), expected)

    def test_headers_and_user_agents(self):
        for headers, expected in (({"User-Agent": "Dalvik/2.1.0 (Linux; Android 15)"}, "android"),
                                  ({"user-agent": "App/307 CFNetwork/1496 Darwin/23.0"}, "ios"),
                                  ({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, "pc"),
                                  ({"Sec-CH-UA-Platform": '"Android"'}, "android"),
                                  ({"User-Agent": "Mozilla/5.0 (Macintosh) Mobile/15E148 Safari/604.1"}, "ios")):
            self.assertEqual(client_platform.detect_platform(headers=headers), expected)

    def test_unknown_is_not_inferred_from_channel_or_substrings(self):
        for headers in ({}, {"User-Agent": "UnityPlayer/2021.3.0f1"}, {"User-Agent": "curios-client rpc/1.0"}):
            self.assertIsNone(client_platform.detect_platform({"channelId": "7"}, headers=headers))
        for value in (123, [], {}, "unknown"):
            with self.assertRaises(ValueError):
                client_platform.detect_platform({"platformType": value})
        self.assertIsNone(client_platform.detect_platform(["invalid JSON body"]))

    def test_cdn_path_overrides_user_agent(self):
        self.assertEqual(client_platform.detect_platform(path="/android/resources/file.bytes",
                         headers={"User-Agent": "iPhone"}), "android")


class ResourcePlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Guard import-time defaults too when this test is run on its own.
        cls.environment = tempfile.TemporaryDirectory(prefix="ag-platform-tests-")
        cls.env_patch = patch.dict(os.environ, {"ACCOUNT_DB_PATH": str(Path(cls.environment.name) / "default.db")})
        cls.env_patch.start()
        import account_db
        import cdn_proxy
        import res_version_manager
        import server_net
        cls.account_db, cls.cdn, cls.versions, cls.net = account_db, cdn_proxy, res_version_manager, server_net

    @classmethod
    def tearDownClass(cls):
        cls.env_patch.stop()
        cls.environment.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=self.environment.name)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("_CURRENT_VERSION", "321"), ("VERSION_CONFIGS", deepcopy(self.versions.BUILTIN_VERSION_CONFIGS)),
                            ("SUPPORTED_VERSIONS", list(self.versions.BUILTIN_VERSION_CONFIGS)),
                            ("DYNAMIC_VERSIONS_PATH", str(self.root / "versions.json")),
                            ("_DETECTED_CLIENT_INFO", {})):
            self.stack.enter_context(patch.object(self.versions, name, value))
        self.stack.enter_context(patch.object(self.cdn, "resolve_doh", side_effect=AssertionError("Unexpected network")))
        self.stack.enter_context(patch.object(self.net, "_LATEST_CLIENT_PLATFORM", ["pc"]))
        self.stack.enter_context(patch("gm_api._context", {}))
        self.db = self.account_db.AccountDB(str(self.root / "empty.db"))
        self.addCleanup(self.db.close)
        self.app = self.net.build_https_app(SimpleNamespace(db=self.db, log=lambda *a, **k: None), host_ip="127.0.0.1")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def update(self, client=None, **kwargs):
        return (client or self.client).open("/sdk-api/pass/game/updateversion/getLatest", **kwargs).get_json()

    def assert_platform(self, response, platform):
        self.assertEqual(response["errorCode"], "0", response)
        patch_info = next(item for item in response["data"] if item["type"] == "noInstall")
        self.assertEqual(patch_info["platformType"], platform)
        self.assertEqual(patch_info["assethash"], self.versions.BUILTIN_VERSION_CONFIGS["321"]["assethash"][platform])
        self.assertTrue(all("/" + platform + "/resources/" in url for url in patch_info["downloadUrl"].split(";")))

    def test_query_form_and_json_platforms_reach_real_sdk_endpoint(self):
        for platform in ("android", "ios", "pc"):
            for values in ({"query_string": {"platformType": platform}},
                           {"method": "POST", "data": {"platformType": platform}},
                           {"method": "POST", "json": {"platformType": platform}}):
                self.assert_platform(self.update(headers={"User-Agent": "iPhone"}, **values), platform)

    def test_config_uses_same_platform_policy(self):
        response = self.client.post("/config", json={"platformType": "ANDROID"}, headers={"User-Agent": "iPhone"}).get_json()
        self.assertEqual(response["data"]["platformType"], "android")
        self.assert_platform(self.update(query_string={"platformType": "android", "channelId": 7}), "android")

    def test_unknown_client_is_not_assigned_previous_platform(self):
        self.assert_platform(self.update(query_string={"platformType": "ios"}), "ios")
        result = self.update(headers={"User-Agent": "UnityPlayer/2021.3.0f1"})
        self.assertEqual(result["errorCode"], "400")
        self.assertEqual(result["data"], [])
        self.assertEqual(self.client.get("/config").get_json()["errorCode"], "400")

    def test_malformed_platform_and_json_do_not_raise_500(self):
        for payload in (["android"], {"platformType": 11}, {"platformType": {"os": "android"}}):
            self.assertEqual(self.update(method="POST", json=payload)["errorCode"], "400")

    def test_mixed_platform_requests_do_not_share_resource_selection(self):
        platforms = ["android", "ios", "pc"] * 5
        def fetch(platform):
            with self.app.test_client() as client:
                return platform, self.update(client, query_string={"platformType": platform})
        with ThreadPoolExecutor(max_workers=3) as executor:
            for platform, result in executor.map(fetch, platforms):
                self.assert_platform(result, platform)

    def test_missing_platform_manifest_never_falls_back(self):
        for version, platform in (("229", "android"), ("311", "android"), ("311", "ios")):
            with patch.object(self.versions, "_CURRENT_VERSION", version):
                response = self.update(query_string={"platformType": platform})
                self.assertEqual(response["errorCode"], "400")
                self.assertEqual(response["data"], [])
        self.versions.VERSION_CONFIGS["321"]["assethash"]["ios"] = self.versions.BUILTIN_VERSION_CONFIGS["229"]["assethash"]["ios"]
        with self.assertRaises(ValueError):
            self.versions.get_assethash("ios", "321")

    def test_pc_client_import_does_not_invent_mobile_manifests(self):
        manifest = self.root / "AssetHash_Info.bytes"
        manifest.write_text(json.dumps({"buildCode": 400, "appVersion": 307, "versionName": "v5.4.0"}))
        self.assertTrue(self.versions.detect_client_resource_version(str(self.root))["detected"])
        self.assertEqual(set(self.versions.VERSION_CONFIGS["400"]["assethash"]), {"pc"})
        with self.assertRaises(ValueError):
            self.versions.get_assethash("android", "400")

    def test_caches_and_initial_copy_keep_platform_assets_separate(self):
        static = self.root / "static"
        static.mkdir()
        pc_hash = self.versions.BUILTIN_VERSION_CONFIGS["321"]["assethash"]["pc"]
        (static / pc_hash).write_bytes(b"pc-manifest")
        (static / "AssetHash_Info.bytes").write_bytes(b"unknown-platform")
        (static / "asset.bundle").write_bytes(b"unknown-bundle")
        for platform in ("android", "ios", "pc"):
            (static / platform).mkdir()
            (static / platform / "asset.bundle").write_bytes(platform.encode())
        targets = {platform: str(self.root / "cache" / platform) for platform in ("android", "ios", "pc")}
        with patch.object(self.cdn, "DEFAULT_STATIC_DIR", str(static)), patch.object(self.cdn, "PLATFORM_DIRS", targets), \
                patch.object(self.cdn, "_SERVER_DIR", str(self.root)):
            self.cdn.sync_initial_manifests()
            for platform in targets:
                self.assertEqual((Path(targets[platform]) / "asset.bundle").read_bytes(), platform.encode())
                self.assertEqual((Path(targets[platform]) / pc_hash).exists(), platform == "pc")
                self.assertFalse((Path(targets[platform]) / "AssetHash_Info.bytes").exists())
            self.assertIsNone(self.cdn.find_cached_file(pc_hash, "android"))
            self.assertIsNone(self.cdn.find_cached_file("AssetHash_Info.bytes", "ios"))

    def test_ambiguous_resource_request_is_rejected_before_network(self):
        response = self.cdn.handle_resource_request("/resources/asset.bundle", headers={"User-Agent": "UnityPlayer/1"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
