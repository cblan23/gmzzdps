#!/usr/bin/env python3
"""Find absolute pointer/RVA references to addresses in a live PE module.

This is a read-only companion to ``runtime_call_xrefs.py``.  It is useful for
callbacks that are registered in tables and therefore reached through an
indirect call rather than a direct ``CALL rel32`` instruction.
"""

from __future__ import annotations

import argparse
import struct

from proc_inspect import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_module,
    find_pid,
    kernel32,
    occurrences,
    read_live_image,
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
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    base, size, path = find_module(pid, args.module)
    for rva in args.rvas:
        if not 0 <= rva < size:
            raise SystemExit(f"target RVA 0x{rva:x} is outside the module")

    process = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not process:
        raise winerror("OpenProcess(runtime pointer xrefs)")
    try:
        image, regions = read_live_image(process, base, size)
    finally:
        kernel32.CloseHandle(process)

    readable = sum(length for _, length, _ in regions)
    print(f"PID={pid} module={path} base=0x{base:x} readable={readable}/{size}")
    total = 0
    for target_rva in args.rvas:
        encodings = (
            ("VA64", struct.pack("<Q", base + target_rva)),
            ("RVA32", struct.pack("<I", target_rva)),
        )
        matches: set[tuple[str, int]] = set()
        for kind, needle in encodings:
            for source_rva in occurrences(image, needle):
                matches.add((kind, source_rva))
        print(f"TARGET_RVA=0x{target_rva:x} TARGET_VA=0x{base + target_rva:x}")
        for kind, source_rva in sorted(matches, key=lambda item: item[1]):
            print(f"  {kind} SOURCE_RVA=0x{source_rva:x} SOURCE_VA=0x{base + source_rva:x}")
        print(f"  MATCHES={len(matches)}")
        total += len(matches)
    print(f"TOTAL_MATCHES={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
