#!/usr/bin/env python3
"""Embed the project's DPI manifest into a Windows executable.

Nuitka 4.1 does not expose a custom-manifest option.  Updating RT_MANIFEST
after the onefile build keeps the runtime code and the packaged executable on
the same Per-Monitor-V2 DPI contract without requiring the Windows SDK's
``mt.exe`` to be installed.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path


RT_MANIFEST = 24
MANIFEST_RESOURCE_ID = 1


def _resource_id(value: int) -> ctypes.c_void_p:
    # MAKEINTRESOURCEW(value), represented as a pointer-sized integer for the
    # UpdateResourceW lpType/lpName parameters.
    return ctypes.c_void_p(int(value) & 0xFFFF)


def embed_manifest(executable: Path, manifest: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("manifest embedding is only supported on Windows")
    executable = executable.resolve()
    manifest = manifest.resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    data = manifest.read_bytes()
    if b"PerMonitorV2" not in data or b"dpiAware" not in data:
        raise ValueError(f"manifest does not declare PerMonitorV2: {manifest}")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle_type = wintypes.HANDLE
    begin = kernel32.BeginUpdateResourceW
    begin.argtypes = (wintypes.LPCWSTR, wintypes.BOOL)
    begin.restype = handle_type
    update = kernel32.UpdateResourceW
    update.argtypes = (
        handle_type,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    update.restype = ctypes.wintypes.BOOL
    end = kernel32.EndUpdateResourceW
    end.argtypes = (handle_type, wintypes.BOOL)
    end.restype = wintypes.BOOL

    resource = begin(str(executable), False)
    if not resource:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(data)
    closed = False
    try:
        ok = update(
            resource,
            _resource_id(RT_MANIFEST),
            _resource_id(MANIFEST_RESOURCE_ID),
            0,
            ctypes.cast(buffer, ctypes.c_void_p),
            len(data),
        )
        if not ok:
            error = ctypes.get_last_error()
            end(resource, True)
            closed = True
            raise ctypes.WinError(error)
        if not end(resource, False):
            raise ctypes.WinError(ctypes.get_last_error())
        closed = True
    except BaseException:
        # The successful EndUpdateResource call closes the handle.  If an
        # unexpected exception occurs before it, discard the partial update.
        if not closed:
            try:
                end(resource, True)
            except Exception:
                pass
        raise


def verify_embedded_manifest(executable: Path) -> None:
    """Fail the build if the resulting PE does not contain our DPI marker."""

    try:
        payload = executable.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not read embedded executable: {executable}") from exc
    # UpdateResourceW stores the XML bytes verbatim. This lightweight check is
    # intentionally independent of optional PE parsing packages so it also
    # works in the minimal build environment.
    if b"PerMonitorV2" not in payload or b"dpiAware" not in payload:
        raise RuntimeError(
            f"embedded DPI manifest markers were not found in {executable}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("windows_dpi.manifest"),
    )
    args = parser.parse_args(argv)
    embed_manifest(args.executable, args.manifest)
    verify_embedded_manifest(args.executable.resolve())
    print(f"Embedded PerMonitorV2 manifest: {args.executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
