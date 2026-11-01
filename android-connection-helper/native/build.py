#!/usr/bin/env python3
"""Build the pinned local TCP stack using Android NDK or ARM64 Termux clang."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
BUILD = ROOT.parent / "build/native"
ABIS = {"arm64-v8a": "aarch64-linux-android24", "armeabi-v7a": "armv7a-linux-androideabi24",
        "x86": "i686-linux-android24", "x86_64": "x86_64-linux-android24"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unpack(archive_path, target):
    with tarfile.open(archive_path) as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe archive path: " + member.name)
            parts = relative.parts[1:]  # codeload's one top-level directory
            if not parts:
                continue
            output = target.joinpath(*parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, output.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
            else:
                raise ValueError("Unsupported archive entry: " + member.name)


def sources(archive_dir):
    packages = json.loads((ROOT / "dependencies.json").read_text())["packages"]
    cache = archive_dir.resolve() if archive_dir else BUILD / "archives"
    cache.mkdir(parents=True, exist_ok=True)
    staging = BUILD / "source"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for package in packages:
        path = cache / (package["name"] + "-" + package["commit"] + ".tar.gz")
        if not path.exists():
            pending = path.with_suffix(".download")
            with urllib.request.urlopen(package["url"], timeout=60) as response, pending.open("wb") as destination:
                shutil.copyfileobj(response, destination)
            if digest(pending) != package["sha256"]:
                pending.unlink()
                raise ValueError("Archive SHA256 mismatch: " + package["name"])
            pending.replace(path)
        if digest(path) != package["sha256"]:
            raise ValueError("Archive SHA256 mismatch: " + str(path))
        unpack(path, staging / package["target"])
        print("Verified source: " + package["repository"] + " @ " + package["commit"], flush=True)
    return staging, packages


def run(command, log):
    result = subprocess.run([str(value) for value in command], capture_output=True, text=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("Command failed; see " + str(log) + "\n" + result.stdout + result.stderr)
    return result.stdout


def build(abi, compiler, readelf, source, jni_includes, jobs, link_flags=()):
    work = BUILD / abi
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    includes = [source / p for p in ("src", "src/misc", "src/core/include", "third-part/yaml/include",
                "third-part/yaml/src", "third-part/lwip/src/include", "third-part/lwip/src/ports/include",
                "third-part/hev-task-system/include", "third-part/hev-task-system/src")]
    common = ["-O2", "-fPIC", "-fvisibility=hidden", "-fno-stack-clash-protection", "-pthread",
              "-DFD_SET_DEFINED", "-DSOCKLEN_T_DEFINED", "-DENABLE_LIBRARY", '-DCOMMIT_ID="1711a443"',
              *("-I" + str(p) for p in [*includes, *jni_includes, ROOT])]
    if abi == "armeabi-v7a":
        common += ["-mfpu=neon"]
    task_flags = ["-DENABLE_STACK_OVERFLOW_DETECTION", "-DCONFIG_STACK_BACKEND=STACK_MMAP",
                  "-DCONFIG_STACK_OVERFLOW_DETECTION=1", "-DCONFIG_MEMALLOC_SLICE_ALIGN=16",
                  "-DCONFIG_MEMALLOC_SLICE_MAX_SIZE=4096", "-DCONFIG_MEMALLOC_SLICE_MAX_COUNT=1000",
                  "-DCONFIG_SCHED_CLOCK=CLOCK_NONE", "-DCONFIG_SCHED_IO_POLL_INTERVAL=50000"]
    yaml_flags = ["-DYAML_VERSION_MAJOR=0", "-DYAML_VERSION_MINOR=2", "-DYAML_VERSION_PATCH=5",
                  '-DYAML_VERSION_STRING="0.2.5"']
    groups = [("engine", source / "src", []), ("lwip", source / "third-part/lwip/src", []),
              ("tasks", source / "third-part/hev-task-system/src", task_flags),
              ("yaml", source / "third-part/yaml/src", yaml_flags)]
    units = [("engine", ROOT / "bridge.c", [])]
    for group, directory, flags in groups:
        units += [(group, p, flags) for p in sorted(directory.rglob("*"))
                  if p.suffix in (".c", ".S") and p.name != "hev-jni.c"]

    def compile_unit(item):
        index, (group, path, flags) = item
        output = work / "objects" / (str(index) + ".o")
        output.parent.mkdir(parents=True, exist_ok=True)
        run([compiler, *common, *flags, "-c", path, "-o", output], work / "logs" / (str(index) + ".log"))
        return group, output

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        compiled = list(pool.map(compile_unit, enumerate(units)))
    # Keep dependencies as archives, as upstream does: unused utility
    # implementations share symbols with the engine and must not be forced in.
    objects = [path for group, path in compiled if group == "engine"]
    for group in ("yaml", "lwip", "tasks"):
        archive = work / (group + ".a")
        run([compiler.parent / "llvm-ar", "rcs", archive,
             *(path for kind, path in compiled if kind == group)], work / (group + "-archive.log"))
        objects.append(archive)
    libraries = BUILD / "lib" / abi
    libraries.mkdir(parents=True, exist_ok=True)
    output = libraries / "libagconnect-tun.so"
    run([compiler, *link_flags, "-shared", "-pthread", "-Wl,-z,defs", "-Wl,-z,max-page-size=16384",
         "-Wl,-z,common-page-size=16384", "-Wl,--wrap=hev_socks5_tunnel_run",
         "-Wl,-soname,libagconnect-tun.so", "-o", output, *objects, "-lm"], work / "link.log")
    dynamic = run([readelf, "-d", output], work / "dynamic.txt")
    headers = run([readelf, "-lW", output], work / "program-headers.txt")
    symbols = run([readelf, "--dyn-syms", "-W", output], work / "symbols.txt")
    if "(RPATH)" in dynamic or "(RUNPATH)" in dynamic:
        raise ValueError("Library contains a host-specific runtime path")
    needed = [line.split("[", 1)[1].split("]", 1)[0] for line in dynamic.splitlines() if "(NEEDED)" in line]
    if set(needed) - {"libc.so", "libm.so", "libdl.so", "liblog.so"}:
        raise ValueError("Library depends on non-Android runtime libraries: " + str(needed))
    if "Java_io_github_qiuyuxc_agconnect_NativeTunnel_nativeOpen" not in symbols:
        raise ValueError("JNI entry point missing")
    loads = [line.split() for line in headers.splitlines() if line.strip().startswith("LOAD ")]
    if not loads or any(int(row[-1], 16) < 16384 for row in loads):
        raise ValueError("Library is not aligned for 16 KiB Android pages")
    report = {"abi": abi, "path": str(output), "bytes": output.stat().st_size, "sha256": digest(output),
              "needed": needed, "page_alignment": 16384, "objects": len(compiled), "api": 24}
    print(json.dumps(report), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    compiler = parser.add_mutually_exclusive_group(required=True)
    compiler.add_argument("--ndk", type=Path, help="Android NDK r27+ root")
    compiler.add_argument("--termux-prefix", type=Path, help="ARM64 Termux prefix containing clang and OpenJDK")
    parser.add_argument("--abis", nargs="+", choices=tuple(ABIS), help="Default: ARM64 for Termux, all four ABIs for NDK")
    parser.add_argument("--archive-dir", type=Path, help="Reuse pinned codeload source archives")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    BUILD.mkdir(parents=True, exist_ok=True)
    if args.termux_prefix:
        prefix = args.termux_prefix.resolve()
        abis = args.abis or ["arm64-v8a"]
        if abis != ["arm64-v8a"]:
            parser.error("Termux builds support ARM64 only; use NDK for other ABIs")
        clang, readelf = prefix / "bin/clang", prefix / "bin/llvm-readelf"
        java = sorted((prefix / "lib/jvm").glob("*/include/jni.h"))
        if not java:
            parser.error("Install Termux openjdk-17 or newer")
        jni = [java[-1].parent, java[-1].parent / "linux"]
        target = run([clang, "-dumpmachine"], BUILD / "compiler-target.txt").strip()
        if not target.startswith("aarch64") or "android" not in target:
            parser.error("Termux compiler must target aarch64 Android")
        toolchains = [("arm64-v8a", clang, readelf, jni, ["-fno-termux-rpath"])]
    else:
        prebuilt = [p for p in (args.ndk.resolve() / "toolchains/llvm/prebuilt").iterdir() if (p / "bin").is_dir()]
        if len(prebuilt) != 1:
            parser.error("Cannot identify NDK host toolchain")
        binaries = prebuilt[0] / "bin"
        abis = args.abis or list(ABIS)
        toolchains = [(abi, binaries / (ABIS[abi] + "-clang"), binaries / "llvm-readelf", [], []) for abi in abis]
    for _, clang, readelf, _, _ in toolchains:
        if not clang.is_file() or not readelf.is_file():
            parser.error("Missing compiler or llvm-readelf")
    source, packages = sources(args.archive_dir)
    libraries = BUILD / "lib"
    if libraries.exists():
        shutil.rmtree(libraries)
    (BUILD / "validation.json").unlink(missing_ok=True)
    reports = [build(abi, clang, readelf, source, jni, args.jobs, flags)
               for abi, clang, readelf, jni, flags in toolchains]
    (BUILD / "validation.json").write_text(json.dumps({"packages": packages, "libraries": reports}, indent=2) + "\n")
    print("Native library directory: " + str(BUILD / "lib"), flush=True)


if __name__ == "__main__":
    main()
