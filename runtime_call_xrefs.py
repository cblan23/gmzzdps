#!/usr/bin/env python3
"""Find direct rel32 calls to one or more RVAs in a live PE64 process.

The shipping image is decrypted only after it is mapped, so scanning the file
on disk does not find reliable code references.  This helper reads executable
sections from the running process and performs the exact rel32 calculation.
It is read-only and intentionally limits itself to direct CALL instructions.
"""

from __future__ import annotations

import argparse
import ctypes
import mmap
import struct
from pathlib import Path

from pe_xrefs import PE
from proc_inspect import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_module,
    find_pid,
    kernel32,
    read_region,
    winerror,
)


def integer(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("rvas", nargs="+", type=integer)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--module", default="C7-Win64-Shipping.exe")
    parser.add_argument("--chunk-size", type=integer, default=0x400000)
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    base, module_size, path = find_module(pid, args.module)
    targets = set(args.rvas)
    for target in targets:
        if not 0 <= target < module_size:
            raise SystemExit(f"target RVA 0x{target:x} is outside the module")

    module_path = Path(path)
    with module_path.open("rb") as source, mmap.mmap(
        source.fileno(), 0, access=mmap.ACCESS_READ
    ) as mapped:
        image = PE(mapped)
        sections = [section for section in image.sections if section.executable]

    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    process = int(kernel32.OpenProcess(access, False, pid) or 0)
    if not process:
        raise winerror("OpenProcess(runtime call xrefs)")
    matches: list[tuple[int, int]] = []
    try:
        for section in sections:
            size = min(section.virtual_size, module_size - section.virtual_address)
            for offset in range(0, size, max(0x1000, args.chunk_size)):
                requested = min(max(0x1000, args.chunk_size), size - offset)
                data = read_region(
                    process, base + section.virtual_address + offset, requested
                )
                if not data:
                    continue
                cursor = 0
                while True:
                    cursor = data.find(b"\xe8", cursor)
                    if cursor < 0 or cursor + 5 > len(data):
                        break
                    call_rva = section.virtual_address + offset + cursor
                    displacement = struct.unpack_from("<i", data, cursor + 1)[0]
                    target_rva = call_rva + 5 + displacement
                    if target_rva in targets:
                        matches.append((call_rva, target_rva))
                    cursor += 1
    finally:
        kernel32.CloseHandle(process)

    print(f"PID={pid} module={path} base=0x{base:x}")
    for call_rva, target_rva in sorted(set(matches)):
        print(f"CALL_RVA=0x{call_rva:x} TARGET_RVA=0x{target_rva:x}")
    print(f"MATCHES={len(set(matches))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
