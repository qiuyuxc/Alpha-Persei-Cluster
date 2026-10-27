"""Generated ZIP/XML fixtures only; no client files or signing keys in tests."""
import contextlib
import importlib.util
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location("patch_apk", Path(__file__).parents[1] / "patch_apk.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def manifest_fixture():
    values = ["label", tool.ANDROID_URI, "manifest", "application", "Demo"]
    offsets, blob = [], bytearray()
    for value in values:
        offsets.append(len(blob))
        encoded = value.encode("utf-16le")
        blob += struct.pack("<H", len(encoded) // 2) + encoded + b"\0\0"
    blob += b"\0" * (-len(blob) % 4)
    start = 28 + len(offsets) * 4
    pool = struct.pack("<HHI5I", 1, 28, start + len(blob), len(values), 0, 0, start, 0)
    pool += struct.pack("<5I", *offsets) + blob
    resource_map = struct.pack("<HHII", 0x180, 8, 12, 0x01010001)

    def node(name, attrs=b""):
        return struct.pack("<HHIII", 0x102, 16, 36 + len(attrs), 1, tool.NO_INDEX) + \
            struct.pack("<II6H", tool.NO_INDEX, name, 20, 20, len(attrs) // 20, 0, 0, 0) + attrs

    def end(name):
        return struct.pack("<HHI4I", 0x103, 16, 24, 1, tool.NO_INDEX, tool.NO_INDEX, name)

    label = struct.pack("<IIIHBBI", 1, 0, 4, 8, 0, 3, 4)
    body = pool + resource_map + node(2) + node(3, label) + end(3) + end(2)
    return struct.pack("<HHI", 3, 8, len(body) + 8) + body


class ManifestTests(unittest.TestCase):
    def test_network_xml_is_exactly_the_verified_self_authored_configuration(self):
        data = tool.network_config()
        self.assertEqual(len(data), 728)
        self.assertEqual(tool.sha256(data), tool.NETWORK_XML_SHA256)
        strings = data.decode("latin1")
        for name in ("system", "user", "sobot_root_path"):
            self.assertIn(name, strings)

    def test_compact_patch_preserves_old_attribute_semantics(self):
        before = manifest_fixture()
        patched = tool.patch_manifest(before)
        parts = tool.chunks(patched)
        values = tool.utf16_pool(parts[0][1])[0]
        app = next(node for kind, node in parts if kind == 0x102 and values[tool.start_element(node)[0]] == "application")
        _, count, _, attrs = tool.start_element(app)
        self.assertEqual(count, 2)
        old_ns, old_name, old_raw, _, _, old_type, old_value = struct.unpack("<IIIHBBI", attrs[0])
        self.assertEqual((values[old_ns], values[old_name], values[old_raw], old_type, values[old_value]),
                         (tool.ANDROID_URI, "label", "Demo", 3, "Demo"))
        ns, name, raw, _, _, kind, value = struct.unpack("<IIIHBBI", attrs[1])
        self.assertEqual((values[ns], values[name], raw, kind, value),
                         (tool.ANDROID_URI, "networkSecurityConfig", tool.NO_INDEX, 1, tool.XML_ID))
        with self.assertRaises(tool.PatchError):
            tool.patch_manifest(patched)

    def test_truncated_chunk_is_rejected(self):
        with self.assertRaises(tool.PatchError):
            tool.chunks(b"12345678" + struct.pack("<HHI", 1, 28, 100))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "input.apk"
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.comment = b"generated fixture"
            archive.writestr("AndroidManifest.xml", manifest_fixture())
            archive.writestr(tool.NETWORK_PATH, b"old provider XML")
            archive.writestr("resources.arsc", b"unchanged generated table")
            archive.writestr("classes.dex", b"generated-payload-" * 2000, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr("META-INF/MANIFEST.MF", b"test signature metadata")
            archive.writestr("META-INF/TEST.SF", b"test signature")
        self.original = self.source.read_bytes()
        self.payloads = {"AndroidManifest.xml": tool.patch_manifest(manifest_fixture()), tool.NETWORK_PATH: tool.network_config()}

    def test_replacements_preserve_raw_prefix_offsets_names_and_payloads(self):
        output = io.BytesIO()
        stream = tool.DigestWriter(output)
        report = tool.write_unsigned(self.source, stream, self.payloads, tool.snapshot(self.source))
        data = output.getvalue()
        self.assertEqual(data[:report["original_prefix_bytes_preserved"]], self.original[:report["original_prefix_bytes_preserved"]])
        self.assertEqual(stream.digest.hexdigest(), tool.sha256(data))
        with zipfile.ZipFile(self.source) as before, zipfile.ZipFile(io.BytesIO(data)) as after:
            self.assertEqual(set(after.namelist()), {n for n in before.namelist() if not tool.signature(n)})
            self.assertEqual(after.comment, before.comment)
            for name in ("resources.arsc", "classes.dex"):
                self.assertEqual(after.read(name), before.read(name))
                self.assertEqual(after.getinfo(name).header_offset, before.getinfo(name).header_offset)
                self.assertEqual(after.getinfo(name).compress_size, before.getinfo(name).compress_size)
            for name, value in self.payloads.items():
                self.assertEqual(after.read(name), value)
                start = after.getinfo(name).header_offset
                lengths = struct.unpack_from("<HH", data, start + 26)
                self.assertEqual((start + 30 + sum(lengths)) % 4, 0)
        self.assertEqual(self.source.read_bytes(), self.original)
        sink = tool.DigestWriter()
        tool.write_unsigned(self.source, sink, self.payloads)
        self.assertEqual(sink.digest.hexdigest(), tool.sha256(data))

    def test_profile_rejects_unsupported_input(self):
        with self.assertRaisesRegex(tool.PatchError, "Unsupported APK"):
            tool.inspect_apk(self.source)

    def test_input_change_is_detected_before_copying(self):
        before = tool.snapshot(self.source)
        self.source.write_bytes(self.original + b"changed")
        target = tool.DigestWriter()
        with self.assertRaisesRegex(tool.PatchError, "Input changed"):
            tool.write_unsigned(self.source, target, self.payloads, before)
        self.assertEqual(target.tell(), 0)

    def test_non_trailing_signature_layout_is_rejected(self):
        with zipfile.ZipFile(self.source, "a") as archive:
            archive.writestr("later.txt", b"later payload")
        with self.assertRaisesRegex(tool.PatchError, "signature record layout"):
            tool.write_unsigned(self.source, tool.DigestWriter(), self.payloads)

    def test_duplicate_names_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), zipfile.ZipFile(self.source, "a") as archive:
            archive.writestr("classes.dex", b"duplicate")
        with self.assertRaisesRegex(tool.PatchError, "Duplicate"):
            tool.write_unsigned(self.source, tool.DigestWriter(), self.payloads)

    def test_prepended_archive_is_rejected_before_writing(self):
        self.source.write_bytes(b"generated-prefix" + self.original)
        stream = tool.DigestWriter()
        with self.assertRaisesRegex(tool.PatchError, "Unsupported central offset"):
            tool.write_unsigned(self.source, stream, self.payloads)
        self.assertEqual(stream.tell(), 0)

    def test_rejects_added_files(self):
        with self.assertRaisesRegex(tool.PatchError, "replace existing"):
            tool.write_unsigned(self.source, tool.DigestWriter(), {"new.txt": b"no"})

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return tool.main([str(self.source), *map(str, args)])

    def test_cli_preserves_existing_output_and_input(self):
        target = self.root / "existing.apk"
        target.write_bytes(b"keep")
        for path in (self.source, target):
            with self.assertRaises(SystemExit):
                self.invoke("--output", path)
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_cli_unsigned_build_and_checksum(self):
        target = self.root / "new.apk"
        with patch.object(tool, "inspect_apk", return_value=(self.payloads, tool.snapshot(self.source))):
            self.assertEqual(self.invoke("--output", target), 0)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read(tool.NETWORK_PATH), tool.network_config())
        self.assertEqual(Path(str(target) + ".sha256").read_text(), tool.file_hash(target) + "  new.apk\n")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_check_and_dry_run_never_create_output(self):
        for mode in ("--check", "--dry-run"):
            target = self.root / "not-created.apk"
            with patch.object(tool, "inspect_apk", return_value=(self.payloads, tool.snapshot(self.source))):
                self.assertEqual(self.invoke(mode, "--output", target), 0)
            self.assertFalse(target.exists())

    def test_signing_failure_cleans_only_new_output(self):
        target = self.root / "failed.apk"
        with patch.object(tool, "inspect_apk", return_value=(self.payloads, tool.snapshot(self.source))), \
                patch.object(tool, "signer_command", return_value=["unused"]), \
                patch.object(tool, "sign_apk", side_effect=tool.PatchError("test signing failure")):
            with self.assertRaises(SystemExit):
                self.invoke("--output", target, "--keystore", self.root / "test.p12")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob(".user-ca-*")))
        self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
