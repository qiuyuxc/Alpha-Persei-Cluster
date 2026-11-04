"""Wrap native ActivityTools while preserving every other bundle object."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import UnityPy

ASSET = 'ActivityTools.lua'
EXPECTED_ASSET_SHA256 = {
    'bc2fde6ef2eacbc059458ef6bcb8aaed06b5a8e6975422183ccaf0f799763575',
    '6d3d871f855afcdb24427bd9bd1d4f11a138676adf32cdb20d34dfc15b6b83a1',
}
PATCH = Path(__file__).with_name('patch.lua')


def digest(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'md5': hashlib.md5(raw).hexdigest()}


def lua_string(raw):
    return '"' + ''.join('\\%03d' % byte for byte in raw) + '"'


def script_bytes(asset):
    return asset.m_Script.encode('utf8', 'surrogateescape')


def object_hashes(env):
    return {(obj.assets_file.name, obj.path_id): hashlib.sha256(obj.get_raw_data()).hexdigest()
            for obj in env.objects}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--luajit', default='luajit')
    parser.add_argument('--compiled-wrapper', type=Path)
    parser.add_argument('--wrapper-source-output', type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() == args.bundle.resolve():
        parser.error('output must be a new path, separate from the source bundle')
    source = args.bundle.read_bytes()
    start = source.index(b'UnityFS')
    env = UnityPy.load(source[start:])
    targets = [obj for obj in env.objects if obj.type.name == 'TextAsset'
               and obj.read().m_Name == ASSET]
    if len(targets) != 1:
        raise ValueError('expected exactly one ' + ASSET)
    target = targets[0]
    data = target.read()
    original = script_bytes(data)
    if hashlib.sha256(original).hexdigest() not in EXPECTED_ASSET_SHA256:
        raise ValueError('unsupported ActivityTools; inspect and test this client version first')
    patch_source = PATCH.read_bytes()
    wrapper = ('local Tools = assert(loadstring(' + lua_string(original)
               + ', "@ActivityTools.original"))()\n'
               + 'local install = assert(loadstring(' + lua_string(patch_source)
               + ', "@home_banner.patch"))()\nreturn install(Tools)\n')
    if args.wrapper_source_output:
        args.wrapper_source_output.parent.mkdir(parents=True, exist_ok=True)
        args.wrapper_source_output.write_text(wrapper)
        if not args.compiled_wrapper:
            print('Wrapper source exported; compile with matching LuaJIT frame format.')
            return
    if args.compiled_wrapper:
        patched = args.compiled_wrapper.read_bytes()
    else:
        with tempfile.TemporaryDirectory(prefix='ag-home-banner-build-') as folder:
            input_path, output_path = Path(folder) / 'wrapper.lua', Path(folder) / 'wrapper.luac'
            input_path.write_text(wrapper)
            subprocess.run([args.luajit, '-b', str(input_path), str(output_path)], check=True)
            patched = output_path.read_bytes()
    if patched[:5] != original[:5]:
        raise ValueError('LuaJIT version or frame format does not match the client')
    if original not in patched or patch_source not in patched:
        raise ValueError('wrapper does not contain this original script and patch')
    before = object_hashes(env)
    data.m_Script = patched.decode('utf8', 'surrogateescape')
    data.save()
    bundle, = env.files.values()
    output = source[:start] + bundle.save(packer='original')
    verified = UnityPy.load(output[start:])
    after = object_hashes(verified)
    expected_key = (target.assets_file.name, target.path_id)
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if changed != {expected_key}:
        raise ValueError('unexpected bundle objects changed: ' + repr(changed))
    saved = next(obj.read() for obj in verified.objects
                 if (obj.assets_file.name, obj.path_id) == expected_key)
    if script_bytes(saved) != patched:
        raise ValueError('patched bytecode did not survive the bundle round trip')
    if args.bundle.read_bytes() != source:
        raise ValueError('source bundle changed during build')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as handle:
        handle.write(output)
    args.output.with_suffix('.lua.luac').write_bytes(patched)
    manifest = {'asset': ASSET, 'path_id': target.path_id, 'source': digest(source),
                'patched': digest(output), 'original_bytecode': digest(original),
                'patched_bytecode': digest(patched), 'patch_source': digest(patch_source),
                'bytecode_header': patched[:5].hex(), 'unchanged_objects': len(before) - 1,
                'unitypy_version': UnityPy.__version__, 'deployed': False}
    args.output.with_suffix('.manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    main()
