"""Create a Windows portable ZIP using an explicit public file list."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--directory', type=Path, required=True)
    cli.add_argument('--source', type=Path, required=True)
    cli.add_argument('--build-id', required=True)
    args = cli.parse_args()
    directory = args.directory.resolve()
    source = args.source.resolve()
    manifest = json.loads((directory / f'release-manifest-{args.build_id}.json').read_text(encoding='utf-8-sig'))
    executable = directory / manifest['filename']
    if executable.parent != directory or executable.suffix.lower() != '.exe':
        raise ValueError('Invalid executable filename')
    if not manifest['protected']:
        raise ValueError('Public ZIP requires a protected build')
    if sha256(executable) != manifest['sha256'] or executable.stat().st_size != manifest['size']:
        raise ValueError('Executable does not match its build manifest')
    version = manifest['version']
    notes = source / f'release-notes-v{version}.txt'
    guide = source / 'docs/usage.md'
    bundle_name = f'Daodao-GMZZ-v{version}-win-x64'
    standalone_path = directory / f'{bundle_name}.exe'
    if standalone_path != executable:
        shutil.copyfile(executable, standalone_path)
    archive_path = directory / f'{bundle_name}.zip'
    files = {
        manifest['filename']: executable,
        f'更新日志-v{version}.txt': notes,
        'licenses/WinDivert-LGPL-GPL.txt': source / 'third_party/windivert/LICENSE',
        'licenses/zstandard-BSD.txt': source / 'third_party_licenses/zstandard-BSD.txt',
    }
    guide_text = guide.read_text(encoding='utf-8').replace(
        '(../使用问题大全.md)', '(https://github.com/cblan23/gmzzdps/blob/main/%E4%BD%BF%E7%94%A8%E9%97%AE%E9%A2%98%E5%A4%A7%E5%85%A8.md)',
    ).replace('(release-v0.3.5.md)', '(https://github.com/cblan23/gmzzdps/blob/main/docs/release-v0.3.5.md)')
    notices = (
        '第三方组件\n\n'
        'WinDivert 2.2.2: https://reqrypt.org/windivert.html\n'
        'Source: https://github.com/basil00/WinDivert/tree/v2.2.2\n'
        'License: licenses/WinDivert-LGPL-GPL.txt\n\n'
        'Zstandard: https://github.com/facebook/zstd\n'
        'License: licenses/zstandard-BSD.txt\n\n'
        '本包不包含用户配置、卡号、数据库、运行日志或开发运行配置。\n'
    )
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for target, path in files.items():
            archive.write(path, f'{bundle_name}/{target}')
        archive.writestr(f'{bundle_name}/使用说明.md', guide_text)
        archive.writestr(f'{bundle_name}/第三方组件说明.txt', notices)
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP integrity check failed')
        expected = {f'{bundle_name}/{target}' for target in files} | {f'{bundle_name}/第三方组件说明.txt', f'{bundle_name}/使用说明.md'}
        if set(archive.namelist()) != expected:
            raise ValueError('ZIP contains unexpected files')
        embedded = hashlib.sha256(archive.read(f'{bundle_name}/{manifest["filename"]}')).hexdigest()
        if embedded != manifest['sha256']:
            raise ValueError('ZIP executable hash mismatch')
    checksums = directory / 'SHA256SUMS.txt'
    checksums.write_text(f'{sha256(standalone_path)}  {standalone_path.name}\n{sha256(archive_path)}  {archive_path.name}\n', encoding='utf-8')
    print(json.dumps({'executable': str(standalone_path), 'archive': str(archive_path), 'zip_size': archive_path.stat().st_size,
                      'zip_sha256': sha256(archive_path), 'exe_sha256': manifest['sha256'],
                      'checksums': str(checksums), 'files': sorted(expected)}, ensure_ascii=True))


if __name__ == '__main__':
    main()
