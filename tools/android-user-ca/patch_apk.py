#!/usr/bin/env python3
"""Patch a user-supplied compatible APK locally; never download or upload APKs."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import zlib

NO_INDEX = 0xffffffff
ANDROID_URI = "http://schemas.android.com/apk/res/android"
ATTRIBUTE_ID = 0x01010527
XML_ID = 0x7f100002
NETWORK_PATH = "res/xml/sobot_provider_paths.xml"
PROFILE = {
    "AndroidManifest.xml": (51108, "c2b945eb5934e291a79e0dda9544728447f0f8b039287730120859a764b42d58"),
    "resources.arsc": (1595324, "83ad917c047d8a3294c1b08864e4da8dc0b752f3b98542ee7dc70b01b29ac4d4"),
    NETWORK_PATH: (288, "afb40a27572c436789d0c866f5f755228c06a9d8f1597c5978c66eadba2631d1"),
}
PATCHED_MANIFEST_SHA256 = "bd5de0fbdfdcdbe90c5a7441a1ecae701bce470779f4d07ef1d8d4fb4d00faa7"
NETWORK_XML_SHA256 = "2f86898bb716ab566a102366803ebc0cf0919a90cee4cf0d59e9938bf242c50e"


class PatchError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PatchError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def chunks(data, start=8):
    result = []
    while start < len(data):
        require(start + 8 <= len(data), "Truncated binary XML chunk")
        kind, header, size = struct.unpack_from("<HHI", data, start)
        require(8 <= header <= size and start + size <= len(data), "Invalid binary XML chunk")
        result.append((kind, data[start:start + size]))
        start += size
    require(start == len(data), "Invalid binary XML size")
    return result


def utf16_pool(data):
    kind, header, size = struct.unpack_from("<HHI", data)
    require(kind == 1 and header == 28 and size == len(data), "Unsupported string pool")
    count, styles, flags, start, style_start = struct.unpack_from("<5I", data, 8)
    require(styles == 0 and style_start == 0 and not flags & 0x101, "Expected an unsorted UTF-16 manifest pool")
    require(header + count * 4 <= start <= size, "Invalid string offsets")
    offsets = data[header:header + count * 4]
    values = []
    for i in range(count):
        pos = start + struct.unpack_from("<I", offsets, i * 4)[0]
        require(pos + 2 <= size, "Invalid string offset")
        units = struct.unpack_from("<H", data, pos)[0]
        require(units < 0x8000 and pos + 4 + units * 2 <= size, "Unsupported string length")
        require(data[pos + 2 + units * 2:pos + 4 + units * 2] == b"\0\0", "Unterminated string")
        values.append(data[pos + 2:pos + 2 + units * 2].decode("utf-16le"))
    return values, offsets, data[header + count * 4:start], data[start:], start


def start_element(node):
    kind, header, size = struct.unpack_from("<HHI", node)
    require(kind == 0x102 and header == 16 and size == len(node), "Invalid start element")
    namespace, name, start, stride, count, *indices = struct.unpack_from("<II6H", node, 16)
    require(start == 20 and stride == 20 and header + start + stride * count <= size, "Unsupported attributes")
    require(all(0 <= i <= count for i in indices), "Invalid attribute indices")
    attrs = [node[header + start + i * stride:header + start + (i + 1) * stride] for i in range(count)]
    return name, count, indices, attrs


def patch_manifest(original):
    require(struct.unpack_from("<HHI", original) == (3, 8, len(original)), "Expected binary AndroidManifest.xml")
    parts = chunks(original)
    require([kind for kind, _ in parts[:2]] == [1, 0x180], "Unsupported manifest structure")
    pool, resource_map = parts[0][1], parts[1][1]
    values, offsets, padding, blob, strings_start = utf16_pool(pool)
    require(struct.unpack_from("<HHI", resource_map) == (0x180, 8, len(resource_map)), "Invalid resource map")
    require((len(resource_map) - 8) % 4 == 0, "Invalid resource IDs")
    ids = list(struct.unpack_from("<" + "I" * ((len(resource_map) - 8) // 4), resource_map, 8))
    insert = len(ids)
    require(insert < len(values) and "networkSecurityConfig" not in values, "Manifest already patched or incompatible")
    require(values.count(ANDROID_URI) == 1, "Missing Android namespace")

    def remap(index):
        return index + 1 if insert <= index < NO_INDEX else index

    name = "networkSecurityConfig"
    new_blob = blob + struct.pack("<H", len(name)) + name.encode("utf-16le") + b"\0\0"
    new_blob += b"\0" * (-len(new_blob) % 4)
    new_offsets = offsets[:insert * 4] + struct.pack("<I", len(blob)) + offsets[insert * 4:]
    header = bytearray(pool[:28])
    struct.pack_into("<I", header, 8, len(values) + 1)
    struct.pack_into("<I", header, 20, strings_start + 4)
    new_pool = header + new_offsets + padding + new_blob
    struct.pack_into("<I", new_pool, 4, len(new_pool))
    require(utf16_pool(new_pool)[0] == values[:insert] + [name] + values[insert:], "String remapping failed")
    new_map = bytearray(resource_map + struct.pack("<I", ATTRIBUTE_ID))
    struct.pack_into("<I", new_map, 4, len(new_map))
    nodes = [bytes(new_pool), bytes(new_map)]
    applications = 0
    for kind, old_node in parts[2:]:
        node = bytearray(old_node)
        positions = [12]  # Optional node comment.
        if kind in (0x100, 0x101, 0x103):
            positions += [16, 20]
        elif kind == 0x102:
            positions += [16, 20]
            _, count, _, _ = start_element(node)
            for i in range(count):
                offset = 36 + i * 20
                positions += [offset, offset + 4, offset + 8]
                if node[offset + 15] == 3:
                    positions.append(offset + 16)
        else:
            raise PatchError(f"Unsupported manifest node: {kind:#x}")
        for pos in positions:
            require(pos + 4 <= len(node), "Truncated manifest node")
            struct.pack_into("<I", node, pos, remap(struct.unpack_from("<I", node, pos)[0]))
        restored = bytearray(node)
        for pos in positions:
            value = struct.unpack_from("<I", restored, pos)[0]
            if insert < value < NO_INDEX:
                struct.pack_into("<I", restored, pos, value - 1)
        require(restored == old_node, "Non-reversible manifest change")
        if kind == 0x102 and values[start_element(old_node)[0]] == "application":
            applications += 1
            _, count, indices, attrs = start_element(old_node)
            attr_ids = [ids[struct.unpack_from("<I", attr, 4)[0]] for attr in attrs]
            require(attr_ids == sorted(attr_ids) and ATTRIBUTE_ID not in attr_ids, "Unsupported application attributes")
            at = next((i for i, value in enumerate(attr_ids) if value > ATTRIBUTE_ID), count)
            attribute = struct.pack("<IIIHBBI", remap(values.index(ANDROID_URI)), insert, NO_INDEX, 8, 0, 1, XML_ID)
            node[36 + at * 20:36 + at * 20] = attribute
            struct.pack_into("<I", node, 4, len(node))
            struct.pack_into("<4H", node, 28, count + 1, *(i + 1 if i > at else i for i in indices))
        nodes.append(bytes(node))
    require(applications == 1, "Expected exactly one application element")
    output = bytearray(original[:8] + b"".join(nodes))
    struct.pack_into("<I", output, 4, len(output))
    return bytes(output)


def network_config():
    """Encode our own trust XML, including the existing FileProvider path node."""
    strings = sorted([".", "base-config", "certificates", "cleartextTrafficPermitted", "name",
                      "network-security-config", "path", "root-path", "sobot_root_path", "src",
                      "system", "trust-anchors", "user"])
    indexes = {value: i for i, value in enumerate(strings)}
    offsets, blob = [], bytearray()
    for value in strings:
        encoded = value.encode("utf-8")
        offsets.append(len(blob))
        blob += bytes([len(value), len(encoded)]) + encoded + b"\0"
    blob += b"\0" * (-len(blob) % 4)
    start = 28 + len(strings) * 4
    pool = struct.pack("<HHI5I", 1, 28, start + len(blob), len(strings), 0, 0x100, start, 0)
    pool += struct.pack("<" + "I" * len(offsets), *offsets) + blob
    nodes = [pool, struct.pack("<HHI", 0x180, 8, 8)]

    def element(name, line, attrs=()):
        node = struct.pack("<HHIII", 0x102, 16, 36 + len(attrs) * 20, line, NO_INDEX)
        node += struct.pack("<II6H", NO_INDEX, indexes[name], 20, 20, len(attrs), 0, 0, 0)
        for key, value in attrs:
            raw, kind, typed = (NO_INDEX, 0x12, NO_INDEX) if value is True else (indexes[value], 3, indexes[value])
            node += struct.pack("<IIIHBBI", NO_INDEX, indexes[key], raw, 8, 0, kind, typed)
        nodes.append(node)

    def end(name, line):
        nodes.append(struct.pack("<HHI4I", 0x103, 16, 24, line, NO_INDEX, NO_INDEX, indexes[name]))

    element("network-security-config", 2)
    element("base-config", 3, [("cleartextTrafficPermitted", True)])
    element("trust-anchors", 4)
    for value, line in (("system", 5), ("user", 6)):
        element("certificates", line, [("src", value)])
        end("certificates", line)
    end("trust-anchors", 4)
    end("base-config", 3)
    element("root-path", 9, [("name", "sobot_root_path"), ("path", ".")])
    end("root-path", 9)
    end("network-security-config", 2)
    body = b"".join(nodes)
    return struct.pack("<HHI", 3, 8, len(body) + 8) + body


def signature(name):
    return bool(re.fullmatch(r"META-INF/(?:MANIFEST\.MF|[^/]+\.(?:SF|RSA|DSA|EC))", name, re.I))


def snapshot(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def inspect_apk(path):
    require(path.is_file(), "Input APK does not exist")
    before = snapshot(path)
    require(before[2] < 2**32, "ZIP64 APKs are not supported")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        require(len(names) == len(set(names)), "Duplicate ZIP entry names")
        ordinary = [i for i in infos if not signature(i.filename)]
        require(len(ordinary) == 53915, "Unsupported APK file set; expected the compatible v0.308.301 client")
        originals = {}
        for name, (size, expected) in PROFILE.items():
            require(name in names and archive.getinfo(name).file_size == size, f"Incompatible {name}")
            data = archive.read(name)
            require(sha256(data) == expected, f"Incompatible {name}: this APK version is unsupported or already patched")
            originals[name] = data
        payloads = {"AndroidManifest.xml": patch_manifest(originals["AndroidManifest.xml"]), NETWORK_PATH: network_config()}
        require(sha256(payloads["AndroidManifest.xml"]) == PATCHED_MANIFEST_SHA256, "Manifest patch differs from the verified format")
        require(sha256(payloads[NETWORK_PATH]) == NETWORK_XML_SHA256, "Network XML differs from the verified format")
    require(snapshot(path) == before, "Input changed while being inspected")
    return payloads, before


def central_records(source, archive):
    source.seek(archive.start_dir)
    records = {}
    for info in archive.infolist():
        header = source.read(46)
        require(len(header) == 46 and header[:4] == b"PK\x01\x02", "Invalid central directory")
        n, e, c = struct.unpack_from("<HHH", header, 28)
        tail = source.read(n + e + c)
        require(len(tail) == n + e + c, "Truncated central directory")
        flags = struct.unpack_from("<H", header, 8)[0]
        name = tail[:n].decode("utf-8" if flags & 0x800 else "cp437")
        require(name == info.filename and name not in records, "Ambiguous ZIP name")
        require(struct.unpack_from("<I", header, 42)[0] == info.header_offset,
                "Unsupported central offset (ZIP64 or prepended archive)")
        records[name] = header + tail
    return records


class DigestWriter:
    def __init__(self, destination=None):
        self.destination = destination
        self.digest = hashlib.sha256()
        self.position = 0

    def write(self, data):
        if self.destination is not None:
            self.destination.write(data)
        self.digest.update(data)
        self.position += len(data)

    def tell(self):
        return self.position


def write_unsigned(path, destination, payloads, expected_snapshot=None):
    """Stream the original local records unchanged, then append two replacements."""
    if expected_snapshot is not None:
        require(snapshot(path) == expected_snapshot, "Input changed before copying")
    with zipfile.ZipFile(path) as archive, path.open("rb") as source:
        infos = archive.infolist()
        require(len(infos) < 65535, "ZIP64 entry counts are not supported")
        require(len({i.filename for i in infos}) == len(infos), "Duplicate ZIP entries")
        require(all(not i.flag_bits & 1 for i in infos), "Encrypted APK entries are not supported")
        signatures = [i for i in infos if signature(i.filename)]
        require(signatures, "Expected a v1-signed APK with trailing signature records")
        cutoff = min(i.header_offset for i in signatures)
        require(all(i.header_offset < cutoff or signature(i.filename) for i in infos), "Unsupported signature record layout")
        require(all(name in archive.namelist() and not signature(name) for name in payloads), "Patches must replace existing ordinary entries")
        records = central_records(source, archive)
        source.seek(0)
        remaining = cutoff
        while remaining:
            block = source.read(min(remaining, 4 * 1024 * 1024))
            require(bool(block), "Input ended during copying")
            destination.write(block)
            remaining -= len(block)
        for name, data in payloads.items():
            encoded = name.encode("utf-8")
            crc, size, offset = zlib.crc32(data) & 0xffffffff, len(data), destination.tell()
            pad = (-(offset + 30 + len(encoded) + 4)) % 4
            extra = struct.pack("<HH", 0xd935, pad) + b"\0" * pad
            destination.write(struct.pack("<I5H3I2H", 0x04034b50, 20, 0, 0, 0, 0x21, crc, size, size, len(encoded), len(extra)))
            destination.write(encoded + extra + data)
            records[name] = struct.pack("<I6H3I5H2I", 0x02014b50, 20, 20, 0, 0, 0, 0x21, crc, size, size,
                                        len(encoded), 0, 0, 0, 0, 0, offset) + encoded
        names = [i.filename for i in infos if not signature(i.filename)]
        start = destination.tell()
        for name in names:
            destination.write(records[name])
        size = destination.tell() - start
        require(start + size + 22 + len(archive.comment) < 2**32, "Output would require ZIP64")
        destination.write(struct.pack("<I4H2IH", 0x06054b50, 0, 0, len(names), len(names), size, start, len(archive.comment)))
        destination.write(archive.comment)
    if expected_snapshot is not None:
        require(snapshot(path) == expected_snapshot, "Input changed during copying")
    return {"original_prefix_bytes_preserved": cutoff, "preserved_member_offsets": len(names) - len(payloads),
            "modified_entries": list(payloads), "unsigned_bytes": destination.tell()}


def signer_command(value=None):
    if value:
        path = Path(value)
    elif shutil.which("apksigner"):
        path = Path(shutil.which("apksigner"))
    else:
        sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        candidates = list((Path(sdk) / "build-tools").glob("*/lib/apksigner.jar")) if sdk else []
        require(candidates, "Set --apksigner or install Android SDK Build-Tools")
        path = max(candidates, key=lambda p: tuple(int(x) if x.isdigit() else 0 for x in p.parent.parent.name.split(".")))
    require(path.is_file(), "apksigner was not found")
    return ["java", "-jar", str(path)] if path.suffix.lower() == ".jar" else [str(path)]


def sign_apk(unsigned, output, signer, keystore, alias, password_file=None):
    require(keystore.is_file(), "Keystore does not exist")
    if password_file:
        require(password_file.is_file(), "Password file does not exist")
    password = None if password_file else getpass.getpass("Keystore password: ") + "\n"
    command = signer + ["sign", "--ks", str(keystore), "--ks-type", "PKCS12", "--ks-key-alias", alias,
                        "--ks-pass", "file:" + str(password_file) if password_file else "stdin",
                        "--min-sdk-version", "23", "--alignment-preserved", "true", "--v1-signing-enabled", "true",
                        "--v2-signing-enabled", "true", "--v3-signing-enabled", "true", "--v4-signing-enabled", "false",
                        "--out", str(output), str(unsigned)]
    result = subprocess.run(command, input=password, text=True, capture_output=True)
    require(result.returncode == 0, "Signing failed:\n" + result.stdout + result.stderr)
    result = subprocess.run(signer + ["verify", "--verbose", "--min-sdk-version", "23", str(output)], text=True, capture_output=True)
    require(result.returncode == 0, "Signature verification failed:\n" + result.stdout + result.stderr)
    for scheme in ("v1", "v2", "v3"):
        require(any(line.startswith("Verified using " + scheme + " scheme") and line.endswith("true")
                    for line in result.stdout.splitlines()), f"Missing verified {scheme} signature")


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Your own compatible original v0.308.301 APK")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Check compatibility without creating an APK")
    modes.add_argument("--dry-run", action="store_true", help="Hash the full unsigned output stream without writing it")
    parser.add_argument("--output", type=Path, help="New output file; existing files are never overwritten")
    parser.add_argument("--keystore", type=Path, help="Your own PKCS12 key; omit to produce an unsigned APK")
    parser.add_argument("--key-alias", default="local", help="Signing key alias (default: local)")
    parser.add_argument("--password-file", type=Path, help="Optional password file; otherwise prompt without echo")
    parser.add_argument("--apksigner", help="Android SDK apksigner executable or JAR")
    args = parser.parse_args(argv)
    try:
        require(not args.password_file or args.keystore, "--password-file requires --keystore")
        require(not (args.check or args.dry_run) or not args.keystore, "Check/dry-run modes do not sign APKs")
        require(args.check or args.dry_run or args.output is not None, "Specify --output, --check or --dry-run")
        source = args.input.resolve()
        if args.output:
            require(args.output.resolve() != source, "Output must not replace the input APK")
            require(not os.path.lexists(args.output), "Output already exists; choose another filename")
            require(not os.path.lexists(str(args.output) + ".sha256"), "Checksum file already exists")
        payloads, before = inspect_apk(source)
        report = {"profile": "com.yongshi.tenojo.ys / v0.308.301 / 2026082719", "compatible": True,
                  "patch_sha256": {name: sha256(data) for name, data in payloads.items()}, "input_modified": False}
        if args.check:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.dry_run:
            stream = DigestWriter()
            report.update(write_unsigned(source, stream, payloads, before))
            report.update(dry_run=True, unsigned_sha256=stream.digest.hexdigest())
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        output = args.output.absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        needed = source.stat().st_size * (2 if args.keystore else 1) + 256 * 1024**2
        require(shutil.disk_usage(output.parent).free >= needed, f"Insufficient free space: need about {needed / 1024**3:.2f} GiB")
        signer = signer_command(args.apksigner) if args.keystore else None
        # Reserve only a new output name. The original APK is always read-only.
        with output.open("xb"):
            pass
        reservation = snapshot(output)
        try:
            with tempfile.TemporaryDirectory(prefix=".user-ca-", dir=output.parent) as temporary:
                unsigned = Path(temporary) / "unsigned.apk"
                with unsigned.open("wb") as target:
                    stream = DigestWriter(target)
                    report.update(write_unsigned(source, stream, payloads, before))
                final = unsigned
                if args.keystore:
                    final = Path(temporary) / "signed.apk"
                    sign_apk(unsigned, final, signer, args.keystore.resolve(), args.key_alias,
                             args.password_file.resolve() if args.password_file else None)
                with zipfile.ZipFile(final) as archive:
                    require(all(archive.read(name) == data for name, data in payloads.items()), "Output patch verification failed")
                digest = file_hash(final) if args.keystore else stream.digest.hexdigest()
                require(snapshot(output) == reservation, "Reserved output changed during the build")
                os.replace(final, output)
                with Path(str(output) + ".sha256").open("x", encoding="utf-8") as checksum:
                    checksum.write(digest + "  " + output.name + "\n")
                report.update(output=str(output), sha256=digest, signed=bool(args.keystore), bytes=output.stat().st_size)
        except BaseException:
            if output.exists() and snapshot(output) == reservation:
                output.unlink()
            raise
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not args.keystore:
            print("Unsigned APK: sign with your own key before installing.")
        return 0
    except (PatchError, OSError, zipfile.BadZipFile, struct.error, UnicodeError, EOFError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
