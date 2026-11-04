"""Wrap only DrawBasePool in an already patched native client bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import UnityPy

ROOT = Path(__file__).resolve().parent
ASSET = 'DrawBasePool.lua'


def digest(data):
    return {'bytes': len(data), 'md5': hashlib.md5(data).hexdigest(),
            'sha256': hashlib.sha256(data).hexdigest()}


def lua_string(data):
    return '"' + ''.join('\\%03d' % byte for byte in data) + '"'


def script_bytes(asset):
    return asset.m_Script.encode('utf8', 'surrogateescape')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiled-wrapper', type=Path)
    parser.add_argument('--export-only', action='store_true')
    parser.add_argument('--luajit', default='luajit')
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() == args.bundle.resolve():
        parser.error('keep output separate from the original bundle')
    raw = args.bundle.read_bytes()
    start = raw.index(b'UnityFS')
    env = UnityPy.load(raw[start:])
    env.path = ''
    originals = {}
    target = None
    for obj in env.objects:
        if obj.type.name != 'TextAsset':
            continue
        data = obj.read()
        if data.m_Name in (ASSET, 'DrawRolePool.lua', 'DrawPoolCfg.lua'):
            originals[data.m_Name] = script_bytes(data)
            if data.m_Name == ASSET:
                if target is not None: raise ValueError('duplicate target')
                target = obj
    if target is None or len(originals) != 3:
        raise ValueError('required native scripts absent')
    original = originals[ASSET]
    patch = (ROOT / 'patch.lua').read_bytes()
    catalog = (ROOT / 'catalog.lua').read_bytes()
    wrapper = ('local Base = assert(loadstring(' + lua_string(original) + ', "@DrawBasePool.original"))()\n'
               + 'local catalog = assert(loadstring(' + lua_string(catalog) + ', "@gacha_background.catalog"))()\n'
               + 'local install = assert(loadstring(' + lua_string(patch) + ', "@gacha_background.patch"))()\n'
               + 'return install(Base, catalog)\n')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source_path = args.output.with_suffix('.wrapper.lua')
    source_path.write_text(wrapper)
    original_dir = args.output.parent / (args.output.stem + '-original')
    original_dir.mkdir(exist_ok=True)
    for name, code in originals.items():
        (original_dir / (name + '.luac')).write_bytes(code)
    if args.export_only:
        print(json.dumps({'wrapper': str(source_path), 'originals': str(original_dir),
                          'header': original[:5].hex(), 'source': digest(raw)}))
        return
    bytecode_path = args.output.with_suffix('.lua.luac')
    if args.compiled_wrapper:
        patched = args.compiled_wrapper.read_bytes()
    else:
        subprocess.run([args.luajit, '-b', str(source_path), str(bytecode_path)], check=True)
        patched = bytecode_path.read_bytes()
    if patched[:5] != original[:5]:
        raise ValueError('compiler frame format does not match the client')
    if not all(code in patched for code in (original, patch, catalog)):
        raise ValueError('wrapper lost native or patch source')
    before = {obj.path_id: hashlib.sha256(obj.get_raw_data()).hexdigest() for obj in env.objects}
    data = target.read()
    data.m_Script = patched.decode('utf8', 'surrogateescape')
    data.save()
    bundle, = env.files.values()
    output = raw[:start] + bundle.save(packer='original')
    verified = UnityPy.load(output[start:])
    after = {obj.path_id: hashlib.sha256(obj.get_raw_data()).hexdigest() for obj in verified.objects}
    if {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)} != {target.path_id}:
        raise ValueError('unexpected bundle objects changed')
    saved = next(obj.read() for obj in verified.objects if obj.path_id == target.path_id)
    if script_bytes(saved) != patched or args.bundle.read_bytes() != raw:
        raise ValueError('bundle round trip or source preservation failed')
    with args.output.open('xb') as handle:
        handle.write(output)
    bytecode_path.write_bytes(patched)
    report = {'asset': ASSET, 'path_id': target.path_id, 'source': digest(raw), 'patched': digest(output),
              'original_bytecode': digest(original), 'patched_bytecode': digest(patched),
              'patch_source': digest(patch), 'catalog_source': digest(catalog),
              'bytecode_header': patched[:5].hex(), 'unchanged_objects': len(before) - 1,
              'deployed': False}
    args.output.with_suffix('.manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
