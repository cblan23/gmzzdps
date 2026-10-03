"""Verify a locally prepared protected release without publishing it."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--directory', type=Path, required=True)
    cli.add_argument('--source', type=Path, required=True)
    cli.add_argument('--version', default='0.3.1')
    cli.add_argument('--build-id', required=True)
    cli.add_argument('--backend', choices=('windows_raw', 'npcap', 'legacy'), default='windows_raw')
    args = cli.parse_args()
    directory = args.directory.resolve()
    manifest = read_json(directory / f'release-manifest-{args.build_id}.json')
    update = read_json(directory / 'update.json')
    allowed = read_json(directory / f'build-allowlist-{args.build_id}.json')['builds']
    executable = directory / manifest['filename']
    assert executable.parent == directory and executable.suffix.lower() == '.exe'
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    assert digest == manifest['sha256'] == update['sha256']
    assert executable.stat().st_size == manifest['size'] == update['size']
    assert manifest['build_id'] == update['build_id'] == args.build_id
    assert manifest['version'] == update['display_version'] == args.version
    assert update['latest_version'] == re.fullmatch(r'(\d+(?:\.\d+){2,3})[a-z]?', args.version).group(1)
    assert manifest['protected'] and update['protected']
    assert manifest['capture_backend'] == args.backend
    assert len(allowed) == 1 and allowed[0]['build_id'] == args.build_id
    assert allowed[0]['enabled'] and allowed[0]['protected']
    assert not manifest['source_included'] and not manifest['tests_included']
    resources = manifest['resource_hashes']
    for name in ('assets/main_hud_reference.png', 'assets/main_boss_icon.png', 'assets/app_logo.png', 'assets/app_icon.ico'):
        assert resources[name] == hashlib.sha256((args.source / name).read_bytes()).hexdigest()
    portrait_manifest = args.source / 'assets/bosses/hud/manifest.json'
    if portrait_manifest.is_file():
        mappings = read_json(portrait_manifest)['templates']
        for name in ('manifest.json', *set(mappings.values())):
            relative = f'assets/bosses/hud/{name}'
            assert resources[relative] == hashlib.sha256((args.source / relative).read_bytes()).hexdigest()
    assert not any('runtime-profile' in name or 'dps_config' in name for name in resources)
    if args.backend == 'npcap':
        assert '_capture_variant.json' in resources
        assert resources['npcap_zstd_restore.dll'] == hashlib.sha256((args.source / 'npcap_zstd_restore.dll').read_bytes()).hexdigest()
    if args.backend == 'windows_raw':
        assert manifest.get('requires_npcap') is False
        assert '_capture_variant.json' in resources
        expected = {
            'windivert/WinDivert.dll': args.source / 'third_party/windivert/x64/WinDivert.dll',
            'windivert/WinDivert64.sys': args.source / 'third_party/windivert/x64/WinDivert64.sys',
            'third_party_licenses/WinDivert-LGPL-GPL.txt': args.source / 'third_party/windivert/LICENSE',
        }
        for target, source in expected.items():
            assert resources[target] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert not any(name.startswith('_npcap/') for name in resources)
    notes = (args.source / f'release-notes-v{args.version}.txt').read_text(encoding='utf-8-sig').strip()
    assert notes == update['notes']
    assert notes == (directory / f'更新日志-v{args.version}.txt').read_text(encoding='utf-8-sig').strip()
    embedded = None
    tree = ast.parse((args.source / 'dps_meter.pyw').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'releases' for target in node.targets):
            for label, text in ast.literal_eval(node.value):
                if label == f'v{args.version}':
                    embedded = text
    assert embedded == notes, 'Embedded and updater release notes differ'
    print(json.dumps(dict(version=args.version, build_id=args.build_id, size=manifest['size'],
                          sha256=digest, protected=True, capture_backend=args.backend,
                          notes_lines=len(notes.splitlines()), status='verified'), ensure_ascii=False))


if __name__ == '__main__':
    main()
