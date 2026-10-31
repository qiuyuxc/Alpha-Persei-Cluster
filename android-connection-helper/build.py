#!/usr/bin/env python3
"""Build/test the no-root Android connection helper (Java + Android SDK, no NDK)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
CORE = ("ServerAddress", "DnsMessage", "Ipv4Packet", "DnsTunnel", "DnsForwarder")
TESTS = ("DnsMessageTest", "DnsTunnelTest", "DnsForwarderTest")


def run(command, log):
    result = subprocess.run([str(arg) for arg in command], cwd=ROOT, capture_output=True, text=True)
    (BUILD / log).write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed; see {BUILD / log}\n{result.stdout}{result.stderr}")
    return result.stdout


def clean_directory(path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-only", action="store_true", help="Run pure Java DNS tests without Android SDK")
    parser.add_argument("--sdk", type=Path, default=os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT"))
    parser.add_argument("--termux-prefix", type=Path, help="Use Termux aapt2/d8/apksigner with SDK android.jar")
    parser.add_argument("--keystore", type=Path, help="PKCS12 key; omit for an unsigned APK")
    parser.add_argument("--key-alias", default="connection")
    parser.add_argument("--password-file", type=Path)
    args = parser.parse_args()
    if bool(args.keystore) != bool(args.password_file):
        parser.error("--keystore and --password-file must be supplied together")
    if not args.test_only and not args.sdk:
        parser.error("Set ANDROID_HOME or supply --sdk")

    BUILD.mkdir(exist_ok=True)
    classes = BUILD / "core"
    clean_directory(classes)
    source = ROOT / "src/java/io/github/qiuyuxc/agconnect"
    run(["javac", "--release", "8", "-encoding", "UTF-8", "-d", classes,
         *(source / f"{name}.java" for name in CORE), *sorted((ROOT / "tests").glob("*.java"))], "test-javac.log")
    results = {}
    for name in TESTS:
        results[name] = run(["java", "-cp", classes, name], f"{name}.log").strip()
        print(results[name], flush=True)
    (BUILD / "test-result.json").write_text(json.dumps({"tests": results, "passed": True}, indent=2) + "\n")
    if args.test_only:
        return

    android = args.sdk.resolve() / "platforms/android-35/android.jar"
    if args.termux_prefix:
        prefix = args.termux_prefix.resolve()
        aapt = prefix / "bin/aapt2"
        d8 = prefix / "share/java/d8.jar"
        signer = prefix / "share/java/apksigner.jar"
    else:
        versions = [p for p in (args.sdk / "build-tools").iterdir() if (p / "lib/d8.jar").is_file()]
        if not versions:
            parser.error("Install Android SDK Build-Tools")
        sdk_tools = max(versions, key=lambda p: tuple(int(v) if v.isdigit() else 0 for v in p.name.split(".")))
        aapt, d8, signer = sdk_tools / "aapt2", sdk_tools / "lib/d8.jar", sdk_tools / "lib/apksigner.jar"
    for path in (android, aapt, d8, *([signer] if args.keystore else [])):
        if not path.is_file():
            parser.error(f"Missing tool: {path}")
    for name in ("classes", "dex", "generated"):
        clean_directory(BUILD / name)
    run([aapt, "compile", "--dir", ROOT / "res", "-o", BUILD / "resources.zip"], "aapt-compile.log")
    run([aapt, "link", "-I", android, "--manifest", ROOT / "AndroidManifest.xml",
         "--java", BUILD / "generated", "-o", BUILD / "resources.apk", BUILD / "resources.zip"], "aapt-link.log")
    run(["javac", "--release", "8", "-encoding", "UTF-8", "-classpath", android,
         "-d", BUILD / "classes", *sorted((ROOT / "src/java").rglob("*.java")),
         *sorted((BUILD / "generated").rglob("*.java"))], "javac.log")
    run(["java", "-cp", d8, "com.android.tools.r8.D8", "--min-api", "24", "--lib", android,
         "--output", BUILD / "dex", *sorted((BUILD / "classes").rglob("*.class"))], "d8.log")
    unsigned = BUILD / "unsigned.apk"
    with zipfile.ZipFile(BUILD / "resources.apk") as resources, zipfile.ZipFile(unsigned, "w") as output:
        entries = [(i.filename, resources.read(i.filename), i.compress_type) for i in resources.infolist()]
        entries += [(p.name, p.read_bytes(), zipfile.ZIP_DEFLATED) for p in sorted((BUILD / "dex").glob("*.dex"))]
        for name, data, compression in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 31, 0, 0, 0))
            info.compress_type = compression
            info.external_attr = 0o644 << 16
            if compression == zipfile.ZIP_STORED:
                padding = (-(output.fp.tell() + 30 + len(name.encode()) + 4)) % 4
                info.extra = struct.pack("<HH", 0xd935, padding) + b"\0" * padding
            output.writestr(info, data)
    ns = "{http://schemas.android.com/apk/res/android}"
    manifest = ET.parse(ROOT / "AndroidManifest.xml").getroot()
    version = manifest.get(ns + "versionName")
    DIST.mkdir(exist_ok=True)
    suffix = "" if args.keystore else "-unsigned"
    apk = DIST / f"AlphaPersei-Connect-v{version}{suffix}.apk"
    if args.keystore:
        run(["java", "-jar", signer, "sign", "--ks", args.keystore.resolve(), "--ks-type", "PKCS12",
             "--ks-key-alias", args.key_alias, "--ks-pass", "file:" + str(args.password_file.resolve()),
             "--v1-signer-name", "AG_CONNECT", "--min-sdk-version", "24", "--alignment-preserved", "true",
             "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true",
             "--v4-signing-enabled", "false", "--out", apk, unsigned], "sign.log")
        verification = run(["java", "-jar", signer, "verify", "--verbose", "--print-certs",
                            "--min-sdk-version", "23", apk], "verify-signature.log")
        assert verification.startswith("Verifies\n")
        for scheme in ("v1", "v2", "v3"):
            assert any(line.startswith("Verified using " + scheme + " scheme") and line.endswith("true")
                       for line in verification.splitlines())
    else:
        shutil.copyfile(unsigned, apk)
    badging = run([aapt, "dump", "badging", apk], "badging.txt")
    run([aapt, "dump", "xmltree", apk, "--file", "AndroidManifest.xml"], "manifest-tree.txt")
    assert any(line in ("sdkVersion:'24'", "minSdkVersion:'24'") for line in badging.splitlines())
    assert "targetSdkVersion:'35'" in badging
    with zipfile.ZipFile(apk) as archive, apk.open("rb") as file:
        assert archive.testzip() is None
        assert "classes.dex" in archive.namelist()
        assert not any(name.endswith((".p12", ".pass", ".jks", ".class")) or name.startswith("lib/")
                       for name in archive.namelist())
        for info in archive.infolist():
            if info.compress_type == zipfile.ZIP_STORED:
                file.seek(info.header_offset)
                header = file.read(30)
                name_len, extra_len = struct.unpack_from("<HH", header, 26)
                assert (info.header_offset + 30 + name_len + extra_len) % 4 == 0
    report = {
        "filename": apk.name, "package": manifest.get("package"), "version": version,
        "version_code": int(manifest.get(ns + "versionCode")),
        "bytes": apk.stat().st_size, "sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
        "signed": bool(args.keystore), "signature_schemes_verified": ["v1", "v2", "v3"] if args.keystore else [],
        "min_sdk": 24, "target_sdk": 35, "native_libraries": False, "root_required": False,
        "game_package": "com.yongshi.tenojo.ys",
        "redirected_hosts": ["open.ys4fun.com", "prod-api-activity.ys4fun.com"],
        "tests": results, "device_validation": {"performed_by_build": False},
    }
    (DIST / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    (DIST / "SHA256SUMS.txt").write_text(report["sha256"] + "  " + apk.name + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
