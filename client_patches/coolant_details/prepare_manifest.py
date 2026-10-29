"""Prepare Android CDN files with the patched scripts64 hash and size."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--build-manifest', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--script-name', choices=('scripts32', 'scripts64'), default='scripts64')
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    build = json.loads(args.build_manifest.read_text())
    source = args.manifest.read_bytes()
    manifest = json.loads(source)
    if manifest['appVersion'] != 307 or manifest['buildCode'] != 321:
        raise ValueError('inspect the resource manifest for this client version first')
    old_entry = args.script_name + '|{md5}|{bytes}'.format(**build['source'])
    new_entry = args.script_name + '|{md5}|{bytes}'.format(**build['patched'])
    if manifest['assetHashList'].count(old_entry) != 1:
        raise ValueError('original script hash or size does not match the resource manifest')
    old_token = json.dumps(old_entry).encode()
    if source.count(old_token) != 1:
        raise ValueError('ambiguous original script entry')
    output = source.replace(old_token, json.dumps(new_entry).encode(), 1)
    expected = json.loads(source)
    index = expected['assetHashList'].index(old_entry)
    expected['assetHashList'][index] = new_entry
    if json.loads(output) != expected:
        raise ValueError('unexpected resource manifest changes')
    bundle = args.bundle.read_bytes()
    if len(bundle) != build['patched']['bytes'] or hashlib.md5(bundle).hexdigest() != build['patched']['md5']:
        raise ValueError('bundle bytes do not match the build manifest')
    if hashlib.sha256(bundle).hexdigest() != build['patched']['sha256']:
        raise ValueError('bundle SHA256 does not match the build manifest')
    name = 'assethash_{appVersion}_{buildCode}_{hash}.bytes'.format(
        **manifest, hash=hashlib.md5(output).hexdigest())
    bundle_name = build['patched']['md5'] + '.ys'
    directory = args.output_directory
    if directory.resolve() == args.manifest.parent.resolve():
        raise ValueError('keep generated CDN files separate from the original manifest')
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open('xb') as handle:
        handle.write(output)
    shutil.copyfile(args.bundle, directory / bundle_name)
    if args.manifest.read_bytes() != source:
        raise ValueError('original resource manifest changed')
    result = {'platform': 'android', 'script_name': args.script_name, 'app_version': manifest['appVersion'],
              'resource_version': manifest['buildCode'], 'assethash_filename': name,
              'bundle_filename': bundle_name, 'bundle_bytes': len(bundle),
              'unchanged_resource_entries': len(manifest['assetHashList']) - 1,
              'other_manifest_fields_unchanged': True, 'source_manifest_unchanged': True,
              'version_metadata_update_required': True, 'deployed': False}
    (directory / 'prepared-files.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
