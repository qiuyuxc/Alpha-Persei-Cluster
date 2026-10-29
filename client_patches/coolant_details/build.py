"""Build a timed-coolant UI patch from a compatible Android script bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import UnityPy

ASSET = 'ItemUseView.lua'
EXPECTED_ASSET_SHA256 = {
    'd29a423840e206daecc27caac2352de05c224f9b6440e695ce28cbf5b7127d1e',
    '5a11b3a6565ad2664572affb26f9fbb1cc3974c32e6df9aa414b43a83ff56523',
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
    parser.add_argument('--test-inputs', type=Path)
    parser.add_argument('--luajit', default='luajit')
    parser.add_argument('--compiled-wrapper', type=Path)
    parser.add_argument('--wrapper-source-output', type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() == args.bundle.resolve():
        parser.error('output must be a new path, separate from the original bundle')
    if args.test_inputs and args.test_inputs.exists():
        parser.error('test-inputs must be a new directory')
    source = args.bundle.read_bytes()
    start = source.index(b'UnityFS')
    env = UnityPy.load(source[start:])
    before = object_hashes(env)
    matches = [obj for obj in env.objects if obj.type.name == 'TextAsset'
               and obj.read().m_Name == ASSET]
    if len(matches) != 1:
        raise ValueError('expected exactly one ' + ASSET)
    target = matches[0]
    data = target.read()
    original = script_bytes(data)
    if hashlib.sha256(original).hexdigest() not in EXPECTED_ASSET_SHA256:
        raise ValueError('unsupported original script; inspect this client version first')
    patch_source = PATCH.read_bytes()
    wrapper = ('local View = assert(loadstring(' + lua_string(original)
               + ', "@ItemUseView.original"))()\n'
               + 'local install = assert(loadstring(' + lua_string(patch_source)
               + ', "@coolant_details.patch"))()\nreturn install(View)\n')
    if args.wrapper_source_output:
        args.wrapper_source_output.parent.mkdir(parents=True, exist_ok=True)
        args.wrapper_source_output.write_text(wrapper)
        if not args.compiled_wrapper:
            print('Wrapper source exported; compile with matching LuaJIT frame format.')
            return
    if args.compiled_wrapper:
        patched = args.compiled_wrapper.read_bytes()
    else:
        with tempfile.TemporaryDirectory(prefix='ag-coolant-ui-build-') as folder:
            source_path = Path(folder) / 'wrapper.lua'
            output_path = Path(folder) / 'wrapper.luac'
            source_path.write_text(wrapper)
            subprocess.run([args.luajit, '-b', str(source_path), str(output_path)], check=True)
            patched = output_path.read_bytes()
    if patched[:5] != original[:5]:
        raise ValueError('LuaJIT bytecode version or architecture flags do not match the client')
    if original not in patched or patch_source not in patched:
        raise ValueError('compiled wrapper does not contain this original script and patch')
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
        raise ValueError('patched LuaJIT bytes did not survive the bundle round trip')
    if args.bundle.read_bytes() != source:
        raise ValueError('original bundle changed during build')
    if args.test_inputs:
        names = {'StoreItemInfo.lua', ASSET, 'ItemCfg.lua',
                 'ItemConst.lua', 'ItemTools.lua', 'CommonAction.lua'}
        fixtures = {obj.read().m_Name: script_bytes(obj.read()) for obj in env.objects
                    if obj.type.name == 'TextAsset' and obj.read().m_Name in names}
        fixtures[ASSET] = original
        if fixtures.keys() != names:
            raise ValueError('missing client test assets')
        args.test_inputs.mkdir(parents=True)
        for name, raw in fixtures.items():
            (args.test_inputs / (name + '.luac')).write_bytes(raw)
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
