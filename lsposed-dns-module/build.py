#!/usr/bin/env python3
"""Build the ARM64 Android module with an Android SDK and NDK (or Termux)."""
import argparse
import hashlib
import json
import os
import urllib.request
from pathlib import Path
import shutil
import struct
import subprocess
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--sdk', type=Path, default=os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT'))
parser.add_argument('--ndk', type=Path, default=os.environ.get('ANDROID_NDK_HOME'))
parser.add_argument('--termux-prefix', type=Path, help='Use an existing ARM64 Termux toolchain instead of the NDK')
parser.add_argument('--keystore', type=Path, help='Optional PKCS12 signing key; omitted builds an unsigned APK')
parser.add_argument('--key-alias', default='module')
parser.add_argument('--password-file', type=Path, help='File containing the keystore password')
args = parser.parse_args()
if not args.sdk:
    parser.error('Set ANDROID_HOME or pass --sdk')
if bool(args.keystore) != bool(args.password_file):
    parser.error('--keystore and --password-file must be provided together')
if not args.termux_prefix and not args.ndk:
    parser.error('Pass --ndk /path/to/ndk or --termux-prefix /path/to/termux/usr')
BUILD = ROOT / 'build'
DIST = ROOT / 'dist'
for path in (BUILD, DIST, BUILD / 'classes', BUILD / 'tests', BUILD / 'dex'):
    path.mkdir(parents=True, exist_ok=True)
# Avoid shipping stale classes when a Java source is renamed or removed.
for name in ('classes', 'tests', 'dex'):
    shutil.rmtree(BUILD / name)
    (BUILD / name).mkdir()
ANDROID = args.sdk / 'platforms/android-35/android.jar'
API = ROOT / 'deps/xposed-api-82.jar'
TERMUX = args.termux_prefix
if TERMUX:
    SIGNER = TERMUX / 'share/java/apksigner.jar'
    D8 = TERMUX / 'share/java/d8.jar'
    AAPT2 = TERMUX / 'bin/aapt2'
else:
    versions = [path for path in (args.sdk / 'build-tools').iterdir() if (path / 'lib/d8.jar').is_file()]
    if not versions:
        parser.error('Install Android SDK Build-Tools')
    sdk_tools = max(versions, key=lambda path: tuple(int(x) if x.isdigit() else 0 for x in path.name.split('.')))
    SIGNER = sdk_tools / 'lib/apksigner.jar'
    D8 = sdk_tools / 'lib/d8.jar'
    AAPT2 = sdk_tools / 'aapt2'
EXPECTED_API_SHA = 'f48c635f1c7469fdec0e00ad2ea0b7a6b2f5b55065784a35b7ca3a84615e8e25'
if not API.exists():
    API.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen('https://api.xposed.info/de/robv/android/xposed/api/82/api-82.jar', timeout=30) as response:
        api_bytes = response.read()
    if hashlib.sha256(api_bytes).hexdigest() != EXPECTED_API_SHA:
        raise RuntimeError('Xposed API checksum mismatch')
    API.write_bytes(api_bytes)
assert hashlib.sha256(API.read_bytes()).hexdigest() == EXPECTED_API_SHA


def run(command, log=None):
    command = [str(value) for value in command]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if log:
        (BUILD / log).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('Command failed: ' + command[0] + '\n' + result.stdout + result.stderr)
    return result.stdout


java = sorted((ROOT / 'src/java').rglob('*.java'))
run(['javac', '--release', '8', '-encoding', 'UTF-8', '-classpath', f'{ANDROID}:{API}',
     '-d', BUILD / 'classes', *java], 'javac.log')
run(['javac', '--release', '8', '-classpath', BUILD / 'classes', '-d', BUILD / 'tests',
     *sorted((ROOT / 'tests').glob('*.java'))], 'test-javac.log')
java_test = run(['java', '-cp', f'{BUILD / "classes"}:{BUILD / "tests"}',
                 'PolicyTest', ROOT / 'tests/domains.tsv'])
gm_test = run(['java', '-cp', f'{BUILD / "classes"}:{BUILD / "tests"}', 'GmPagePolicyTest'])
address_test = run(['java', '-cp', f'{BUILD / "classes"}:{BUILD / "tests"}', 'ServerAddressTest'])
run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
     ROOT / 'tests/policy_test.c', '-o', BUILD / 'policy-test'])
native_test = run([BUILD / 'policy-test', ROOT / 'tests/domains.tsv'])
print(java_test + native_test + gm_test + address_test, end='', flush=True)

library = BUILD / 'libagredirect.so'
if TERMUX:
    run([TERMUX / 'bin/clang', '-std=c11', '-c', '-fPIC', '-O2', '-fvisibility=hidden',
         '-Wall', '-Wextra', '-Werror', '-mno-outline-atomics', ROOT / 'src/native/redirect.c',
         '-o', BUILD / 'redirect.o'], 'native-compile.log')
    # The Termux compiler driver injects a private RUNPATH when linking. Invoke
    # the linker directly so the module depends only on Android's public libraries.
    run([TERMUX / 'bin/ld.lld', '-shared', '--no-undefined', '-soname', 'libagredirect.so',
         '-z', 'max-page-size=16384', '-z', 'common-page-size=16384',
         '-z', 'relro', '-z', 'now', '-z', 'noexecstack', BUILD / 'redirect.o',
         '/system/lib64/libc.so', '/system/lib64/libdl.so', '/system/lib64/liblog.so',
         '-o', library], 'native-build.log')
else:
    compilers = list((args.ndk / 'toolchains/llvm/prebuilt').glob('*/bin/aarch64-linux-android24-clang'))
    if len(compilers) != 1:
        parser.error('Cannot locate the Android NDK ARM64 compiler')
    run([compilers[0], '-std=c11', '-shared', '-fPIC', '-O2', '-fvisibility=hidden',
         '-Wall', '-Wextra', '-Werror', '-Wl,-soname,libagredirect.so',
         '-Wl,-z,max-page-size=16384,-z,common-page-size=16384,-z,relro,-z,now,-z,noexecstack',
         ROOT / 'src/native/redirect.c', '-llog', '-ldl', '-o', library], 'native-build.log')
dynamic = run(['readelf', '-dW', library], 'native-dynamic.txt')
assert 'RPATH' not in dynamic and 'RUNPATH' not in dynamic
assert all(any(name in line for name in ('[libc.so]', '[libdl.so]', '[liblog.so]', '[libm.so]'))
           for line in dynamic.splitlines() if '(NEEDED)' in line)
symbols = run(['readelf', '-Ws', library], 'native-symbols.txt')
assert 'native_init' in symbols and 'Java_io_github_qiuyuxc_agdns_MainHook_nativeStatus' in symbols
elf = library.read_bytes()
assert elf[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H', elf, 18)[0] == 183
phoff = struct.unpack_from('<Q', elf, 32)[0]
phentsize, phnum = struct.unpack_from('<HH', elf, 54)
for index in range(phnum):
    kind, _, _, _, _, _, _, alignment = struct.unpack_from('<II6Q', elf, phoff + phentsize * index)
    if kind == 1:
        assert alignment >= 16384

run(['java', '-cp', D8, 'com.android.tools.r8.D8',
     '--min-api', '24', '--lib', ANDROID, '--lib', API, '--output', BUILD / 'dex',
     *sorted((BUILD / 'classes').rglob('*.class'))], 'd8.log')
run([AAPT2, 'compile', '--dir', ROOT / 'res', '-o', BUILD / 'resources.zip'], 'aapt-compile.log')
run([AAPT2, 'link', '-I', ANDROID, '--manifest', ROOT / 'AndroidManifest.xml',
     '-A', ROOT / 'assets', '-o', BUILD / 'resources.apk', BUILD / 'resources.zip'], 'aapt-link.log')

unsigned = BUILD / 'unsigned.apk'
with zipfile.ZipFile(BUILD / 'resources.apk') as resources, zipfile.ZipFile(unsigned, 'w') as output:
    entries = [(info.filename, resources.read(info.filename), info.compress_type) for info in resources.infolist()]
    entries += [('classes.dex', (BUILD / 'dex/classes.dex').read_bytes(), zipfile.ZIP_DEFLATED),
                ('lib/arm64-v8a/libagredirect.so', elf, zipfile.ZIP_STORED)]
    for name, data, compression in entries:
        info = zipfile.ZipInfo(name, date_time=(2026, 10, 25, 0, 0, 0))
        info.compress_type = compression
        info.external_attr = 0o644 << 16
        if compression == zipfile.ZIP_STORED:
            alignment = 16384 if name.endswith('.so') else 4
            padding = (-(output.fp.tell() + 30 + len(name.encode()) + 4)) % alignment
            info.extra = struct.pack('<HH', 0xd935, padding) + b'\0' * padding
        output.writestr(info, data)

manifest = ET.parse(ROOT / 'AndroidManifest.xml').getroot()
android_ns = '{http://schemas.android.com/apk/res/android}'
version_code = int(manifest.get(android_ns + 'versionCode'))
version_name = manifest.get(android_ns + 'versionName')
apk = DIST / f'AlphaPersei-DNS-LSPosed-v{version_code}.apk'
if args.keystore:
    run(['java', '-Xmx256m', '-jar', SIGNER, 'sign', '--ks', args.keystore,
         '--ks-type', 'PKCS12', '--ks-key-alias', args.key_alias, '--ks-pass', 'file:' + str(args.password_file),
         '--v1-signer-name', 'AG_DNS', '--min-sdk-version', '24', '--alignment-preserved', 'true',
         '--v1-signing-enabled', 'true', '--v2-signing-enabled', 'true', '--v3-signing-enabled', 'true',
         '--v4-signing-enabled', 'false', '--out', apk, unsigned], 'sign.log')
    # Force the verifier to inspect the included v1 signature as well; the APK's
    # actual minimum SDK remains 24. API 24+ verification otherwise skips v1.
    verify = run(['java', '-jar', SIGNER, 'verify', '--verbose', '--print-certs', '--min-sdk-version', '23', apk],
                 'verify-signature.log')
    assert verify.startswith('Verifies\n')
    for scheme in ('v1', 'v2', 'v3'):
        assert any(line.startswith('Verified using ' + scheme + ' scheme') and line.endswith('true')
                   for line in verify.splitlines())
else:
    shutil.copyfile(unsigned, apk)
run([AAPT2, 'dump', 'badging', apk], 'badging.txt')
run([AAPT2, 'dump', 'xmltree', apk, '--file', 'AndroidManifest.xml'], 'manifest-tree.txt')
with zipfile.ZipFile(apk) as archive, apk.open('rb') as source:
    assert archive.testzip() is None
    assert archive.read('assets/xposed_init').strip() == b'io.github.qiuyuxc.agdns.MainHook'
    assert archive.read('assets/native_init').strip() == b'libagredirect.so'
    assert archive.read('lib/arm64-v8a/libagredirect.so') == elf
    assert not any('xposed/api' in name or name.endswith(('.p12', '.pass', '.jks')) for name in archive.namelist())
    for info in archive.infolist():
        if info.compress_type != zipfile.ZIP_STORED:
            continue
        source.seek(info.header_offset)
        header = source.read(30)
        name_len, extra_len = struct.unpack_from('<HH', header, 26)
        data_offset = info.header_offset + 30 + name_len + extra_len
        assert data_offset % (16384 if info.filename.endswith('.so') else 4) == 0

report = {'filename': apk.name, 'package': 'io.github.qiuyuxc.agdns', 'version': version_name, 'version_code': version_code,
          'bytes': apk.stat().st_size, 'sha256': hashlib.sha256(apk.read_bytes()).hexdigest(),
          'target_package': 'com.yongshi.tenojo.ys', 'default_server': '127.0.0.1', 'abi': 'arm64-v8a',
          'redirected_hosts': ['open.ys4fun.com', 'prod-api-activity.ys4fun.com'],
          'official_downloads_passthrough': True,
          'default_sub_gm_url': 'https://127.0.0.1/web/sub_gm.html',
          'server_configurable': True,
          'web_page_scale': {'landscape': 0.8, 'portrait': 1.0, 'drawer_size_changed': False},
          'gm_navigation_test': gm_test.strip(), 'server_address_test': address_test.strip(),
          'gm_handle': {'default_side': 'left', 'drag_axes': ['x', 'y'],
                        'edge_snapping': False, 'position_persistence': 'per_orientation',
                        'bounds': 'visible_window_excluding_system_bars_and_display_cutout'},
          'system_overlay_permission': False,
          'java_policy_test': java_test.strip(), 'native_policy_test': native_test.strip(),
          'native_sha256': hashlib.sha256(elf).hexdigest(), 'native_elf_and_zip_alignment': 16384,
          'signature_schemes_verified': ['v1', 'v2', 'v3'] if args.keystore else [],
          'signed': bool(args.keystore)}
(DIST / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')
(DIST / 'SHA256SUMS.txt').write_text(report['sha256'] + '  ' + apk.name + '\n')
print(json.dumps(report, indent=2), flush=True)
