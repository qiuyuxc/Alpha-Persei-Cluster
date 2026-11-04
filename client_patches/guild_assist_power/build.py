"""Wrap the three guild assets while preserving all other Unity bundle objects."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import UnityPy

ASSETS = ('GuildData.lua', 'GuildBossAssistantHeadItemView.lua', 'GuildBossSelectHeroProxy.lua')
PATCH = Path(__file__).with_name('patch.lua')


def digest(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'md5': hashlib.md5(raw).hexdigest()}


def lua_string(raw):
    return '"' + ''.join('\\%03d' % byte for byte in raw) + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--luajit', default='luajit')
    parser.add_argument('--wrapper-directory', type=Path)
    parser.add_argument('--compiled-directory', type=Path)
    parser.add_argument('--supported-assets', type=Path, required=True)
    parser.add_argument('--native-output', type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() == args.bundle.resolve():
        parser.error('output must be a new path separate from the source bundle')
    source = args.bundle.read_bytes()
    start = source.index(b'UnityFS')
    env = UnityPy.load(source[start:])
    expected = json.loads(args.supported_assets.read_text())
    targets = {obj.read().m_Name: obj for obj in env.objects
               if obj.type.name == 'TextAsset' and obj.read().m_Name in ASSETS}
    if set(targets) != set(ASSETS):
        raise ValueError('guild asset set is incomplete')
    before = {(obj.assets_file.name, obj.path_id): hashlib.sha256(obj.get_raw_data()).hexdigest()
              for obj in env.objects}
    changed_assets, changed_keys = {}, set()
    patch = PATCH.read_bytes()
    for name in ASSETS:
        obj = targets[name]
        data = obj.read()
        original = data.m_Script.encode('utf8', 'surrogateescape')
        if hashlib.sha256(original).hexdigest() not in expected[name]:
            raise ValueError('inspect this guild asset version before patching: ' + name)
        wrapper = ('local Tools = assert(loadstring(' + lua_string(original)
                   + ', "@' + name + '.original"))()\n'
                   + 'local install = assert(loadstring(' + lua_string(patch)
                   + ', "@guild_assist_power.patch"))()\nreturn install('
                   + json.dumps(name) + ', Tools)\n')
        if args.native_output:
            args.native_output.mkdir(parents=True, exist_ok=True)
            (args.native_output / (name + '.luac')).write_bytes(original)
        if args.wrapper_directory:
            args.wrapper_directory.mkdir(parents=True, exist_ok=True)
            (args.wrapper_directory / name).write_text(wrapper)
        if args.wrapper_directory and not args.compiled_directory:
            continue
        if args.compiled_directory:
            patched = (args.compiled_directory / (name + '.luac')).read_bytes()
        else:
            with tempfile.TemporaryDirectory(prefix='ag-guild-power-build-') as folder:
                input_path, output_path = Path(folder) / 'wrapper.lua', Path(folder) / 'wrapper.luac'
                input_path.write_text(wrapper)
                subprocess.run([args.luajit, '-b', str(input_path), str(output_path)], check=True)
                patched = output_path.read_bytes()
        if patched[:5] != original[:5] or original not in patched or patch not in patched:
            raise ValueError('wrapper does not preserve the native asset or LuaJIT frame format')
        data.m_Script = patched.decode('utf8', 'surrogateescape')
        data.save()
        changed_keys.add((obj.assets_file.name, obj.path_id))
        changed_assets[name] = {'original': digest(original), 'patched': digest(patched)}
    if args.wrapper_directory and not args.compiled_directory:
        print('Wrapper sources exported; compile using the matching LuaJIT frame format.')
        return
    bundle, = env.files.values()
    output = source[:start] + bundle.save(packer='original')
    verified = UnityPy.load(output[start:])
    after = {(obj.assets_file.name, obj.path_id): hashlib.sha256(obj.get_raw_data()).hexdigest()
             for obj in verified.objects}
    actual = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if actual != changed_keys or args.bundle.read_bytes() != source:
        raise ValueError('unrelated bundle objects or source bundle changed')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as handle:
        handle.write(output)
    result = {'assets': changed_assets, 'source': digest(source), 'patched': digest(output),
              'unchanged_objects': len(before) - len(ASSETS), 'unitypy_version': UnityPy.__version__,
              'deployed': False}
    args.output.with_suffix('.manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
