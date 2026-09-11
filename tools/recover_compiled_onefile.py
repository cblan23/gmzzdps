"""Finalize an intact Nuitka COFF-payload build after resource-write failure.

Only PE icon/manifest resources are updated. The complete compressed program
payload is verified byte-for-byte before and after; no compilation is skipped.
Uses a working copy and one resource transaction instead of repeated updates.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from embed_windows_manifest import verify_embedded_manifest
from release_security import load_release_identity, verify_release_resources
from nuitka.utils.WindowsResources import getResourcesFromDLL


def update_resources(path, records):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.BeginUpdateResourceW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL)
    api.BeginUpdateResourceW.restype = wintypes.HANDLE
    api.UpdateResourceW.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   wintypes.WORD, ctypes.c_void_p, wintypes.DWORD)
    api.UpdateResourceW.restype = wintypes.BOOL
    api.EndUpdateResourceW.argtypes = (wintypes.HANDLE, wintypes.BOOL)
    api.EndUpdateResourceW.restype = wintypes.BOOL
    handle = api.BeginUpdateResourceW(str(path), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    closed = False
    buffers = []
    try:
        for kind, name, lang, data in records:
            buffer = ctypes.create_string_buffer(data)
            buffers.append(buffer)
            if not api.UpdateResourceW(handle, ctypes.c_void_p(kind), ctypes.c_void_p(name), lang,
                                       ctypes.cast(buffer, ctypes.c_void_p), len(data)):
                raise ctypes.WinError(ctypes.get_last_error())
        closed = True
        if not api.EndUpdateResourceW(handle, False):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if not closed:
            api.EndUpdateResourceW(handle, True)


def verify_payload(path, payload):
    data = path.read_bytes()
    location = data.find(payload[:128])
    if location < 0 or data[location:location + len(payload)] != payload:
        raise ValueError('The complete compiled onefile payload is not intact')


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--directory', type=Path, required=True)
    cli.add_argument('--source', type=Path, required=True)
    args = cli.parse_args()
    directory, source = args.directory.resolve(), args.source.resolve()
    dist = directory / 'dps_meter.dist'
    identity = load_release_identity(dist)
    assert identity.valid_build and identity.valid_protected
    valid, error = verify_release_resources(identity, dist)
    if not valid:
        raise ValueError(error)
    metadata = json.loads((dist / '_release_identity.json').read_text(encoding='utf-8'))
    payload = (directory / 'dps_meter.onefile-build' / 'blobs' / '__payload.bin').read_bytes()
    executable = directory / f'叨叨诡秘-Dps-Logs-v{identity.version}.exe'
    verify_payload(executable, payload)
    records = getResourcesFromDLL(str(dist / 'dps_meter.dll'), (3,14), with_data=True)
    assert sum(kind == 3 for kind, *_ in records) == 9
    manifest = (source / 'windows_dpi.manifest').read_bytes()
    assert b'PerMonitorV2' in manifest
    records.append((24, 1, 0, manifest))
    working_dir = Path(tempfile.mkdtemp(prefix='v023-resource-finalize-', dir=str(ROOT / '.codex-tmp')))
    working = working_dir / 'DpsLogs-v0.2.3.exe'
    shutil.copy2(executable, working)
    update_resources(working, records)
    verify_payload(working, payload)
    verify_embedded_manifest(working)
    written = getResourcesFromDLL(str(working), (3,14,24), with_data=True)
    assert {(kind,name):hashlib.sha256(data).hexdigest() for kind,name,lang,data in written} == {
        (kind,name):hashlib.sha256(data).hexdigest() for kind,name,lang,data in records}
    backup = working_dir / 'resource-incomplete-original.exe'
    shutil.copy2(executable, backup)
    shutil.copy2(working, executable)
    proof = {'build_id':identity.build_id, 'version':identity.version,
             'payload_sha256':hashlib.sha256(payload).hexdigest(),
             'exe_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
             'resource_transaction':'verified', 'backup':str(backup), 'metadata':metadata}
    proof_path = working_dir / 'finalization-proof.json'
    proof_path.write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    print('FINALIZED', executable)
    print('PROOF', proof_path)


if __name__ == '__main__':
    main()
