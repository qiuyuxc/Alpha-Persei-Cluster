"""Prepare the current Android manifest without changing other resources."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--bundle32', type=Path, required=True)
    parser.add_argument('--bundle64', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    if (manifest['appVersion'], manifest['buildCode']) != (307, 321):
        raise ValueError('inspect this client version before adapting its pools')
    cfg = json.loads(Path(__file__).with_name('compatibility.json').read_text())
    declared = {entry.split('|', 1)[0].lower() for entry in manifest['assetHashList']}
    for row in cfg['catalog'].values():
        if row['start'].lower() not in declared or row['loop'].lower() not in declared:
            raise ValueError('required same-hero animation is absent')
    if not {'widget/system/pool.ys', 'widget/system/poolrole.ys'} <= declared:
        raise ValueError('native display templates are absent')
    output, expected, files = raw, json.loads(raw), []
    for arch, path in [('scripts32', args.bundle32), ('scripts64', args.bundle64)]:
        build = json.loads(path.with_suffix('.manifest.json').read_text())
        if build['asset'] != 'DrawBasePool.lua' or build['unchanged_objects'] != 10267:
            raise ValueError('unexpected client objects changed')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != build['patched']['sha256']:
            raise ValueError('bundle integrity failure')
        old = arch + '|{md5}|{bytes}'.format(**build['source'])
        new = arch + '|{md5}|{bytes}'.format(**build['patched'])
        if manifest['assetHashList'].count(old) != 1:
            raise ValueError('source does not match current script manifest')
        output = output.replace(json.dumps(old).encode(), json.dumps(new).encode(), 1)
        expected['assetHashList'][expected['assetHashList'].index(old)] = new
        files.append({'arch': arch, 'filename': build['patched']['md5'] + '.ys',
                      'sha256': build['patched']['sha256'], 'source': path})
    if json.loads(output) != expected or args.manifest.read_bytes() != raw:
        raise ValueError('unrelated resource or source changed')
    args.output.mkdir(parents=True, exist_ok=True)
    name = 'assethash_307_321_' + hashlib.md5(output).hexdigest() + '.bytes'
    with (args.output / name).open('xb') as handle:
        handle.write(output)
    for row in files:
        shutil.copy2(row.pop('source'), args.output / row['filename'])
    result = {'assethash': name, 'sha256': hashlib.sha256(output).hexdigest(),
              'previous_assethash': args.manifest.name, 'bundles': files,
              'unchanged_resource_entries': len(manifest['assetHashList']) - 2,
              'other_manifest_fields_unchanged': True, 'deployed': False}
    (args.output / 'prepared-files.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
