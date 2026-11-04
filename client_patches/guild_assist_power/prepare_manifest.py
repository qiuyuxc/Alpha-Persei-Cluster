"""Prepare the Android script update while retaining all other manifest entries."""
import argparse
import hashlib
import json
from pathlib import Path

ASSETS = {'GuildData.lua', 'GuildBossAssistantHeadItemView.lua', 'GuildBossSelectHeroProxy.lua'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--bundle32', type=Path, required=True)
    parser.add_argument('--bundle64', type=Path, required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    source = args.manifest.read_bytes()
    original = json.loads(source)
    if original['appVersion'] != 307 or original['buildCode'] != 321:
        raise ValueError('inspect this client version before preparing a script update')
    output, expected, bundles = source, json.loads(source), []
    for script, path in (('scripts32', args.bundle32), ('scripts64', args.bundle64)):
        build = json.loads(path.with_suffix('.manifest.json').read_text())
        if set(build['assets']) != ASSETS:
            raise ValueError('unexpected guild asset changes')
        old = script + '|{md5}|{bytes}'.format(**build['source'])
        new = script + '|{md5}|{bytes}'.format(**build['patched'])
        if original['assetHashList'].count(old) != 1 or source.count(json.dumps(old).encode()) != 1:
            raise ValueError('script source does not match the current manifest')
        raw = path.read_bytes()
        if (len(raw) != build['patched']['bytes']
                or hashlib.md5(raw).hexdigest() != build['patched']['md5']
                or hashlib.sha256(raw).hexdigest() != build['patched']['sha256']):
            raise ValueError('script bundle does not match its build result')
        output = output.replace(json.dumps(old).encode(), json.dumps(new).encode(), 1)
        expected['assetHashList'][expected['assetHashList'].index(old)] = new
        bundles.append({'script_name': script, 'filename': build['patched']['md5'] + '.ys',
                        'bytes': len(raw), 'sha256': build['patched']['sha256'], 'raw': raw})
    if json.loads(output) != expected or args.manifest.read_bytes() != source:
        raise ValueError('unrelated manifest entries or original file changed')
    name = 'assethash_307_321_' + hashlib.md5(output).hexdigest() + '.bytes'
    directory = args.output_directory
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open('xb') as handle:
        handle.write(output)
    for bundle in bundles:
        with (directory / bundle['filename']).open('xb') as handle:
            handle.write(bundle.pop('raw'))
    result = {'platform': 'android', 'app_version': 307, 'resource_version': 321,
              'assethash_filename': name, 'assethash_sha256': hashlib.sha256(output).hexdigest(),
              'source_assethash_filename': args.manifest.name,
              'source_assethash_sha256': hashlib.sha256(source).hexdigest(),
              'bundles': bundles, 'unchanged_resource_entries': len(original['assetHashList']) - 2,
              'other_manifest_fields_unchanged': True, 'source_manifest_unchanged': True,
              'deployed': False}
    (directory / 'prepared-files.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
