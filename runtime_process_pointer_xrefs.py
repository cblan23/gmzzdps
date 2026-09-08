#!/usr/bin/env python3
"""Find exact pointer values across readable memory in a live process.

The scanner is deliberately read-only and reports only region metadata and
addresses containing the requested pointer.  It is intended for locating
runtime callback/registration tables that live outside the main PE image.
"""

from __future__ import annotations

import argparse
import ctypes
import struct

from proc_inspect import (
    MEM_COMMIT,
    MEMORY_BASIC_INFORMATION,
    PAGE_GUARD,
    PAGE_NOACCESS,
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_module,
    find_pid,
    kernel32,
    occurrences,
    read_region,
    winerror,
)


MAX_USER_ADDRESS = 0x00007FFFFFFFFFFF
READ_CHUNK_SIZE = 16 * 1024 * 1024


def integer(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("values", nargs="+", type=integer)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--module", default="C7-Win64-Shipping.exe")
    parser.add_argument(
        "--absolute",
        action="store_true",
        help="treat input values as absolute addresses instead of module RVAs",
    )
    parser.add_argument(
        "--max-address",
        type=integer,
        default=MAX_USER_ADDRESS,
        help="exclusive upper bound for scanned virtual addresses",
    )
    parser.add_argument(
        "--private-only",
        action="store_true",
        help="scan only MEM_PRIVATE regions (useful for managed runtime heaps)",
    )
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    base, module_size, path = find_module(pid, args.module)
    targets = args.values if args.absolute else [base + value for value in args.values]
    needles = {target: struct.pack("<Q", target) for target in targets}

    process = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not process:
        raise winerror("OpenProcess(runtime process pointer xrefs)")

    matches: list[tuple[int, int, int, int, int]] = []
    readable_bytes = 0
    region_count = 0
    cursor = 0
    try:
        scan_end = min(MAX_USER_ADDRESS, max(0, args.max_address))
        while cursor < scan_end:
            mbi = MEMORY_BASIC_INFORMATION()
            queried = kernel32.VirtualQueryEx(
                process,
                ctypes.c_void_p(cursor),
                ctypes.byref(mbi),
                ctypes.sizeof(mbi),
            )
            if not queried:
                break
            region_base = int(mbi.BaseAddress or 0)
            region_size = int(mbi.RegionSize)
            region_end = region_base + region_size
            if region_end <= cursor:
                break
            readable = (
                mbi.State == MEM_COMMIT
                and not (mbi.Protect & PAGE_GUARD)
                and (mbi.Protect & 0xFF) != PAGE_NOACCESS
                and (not args.private_only or int(mbi.Type) == 0x20000)
            )
            if readable:
                region_count += 1
                offset = 0
                region_size = min(region_size, scan_end - region_base)
                overlap = 7
                while offset < region_size:
                    size = min(READ_CHUNK_SIZE, region_size - offset)
                    read_at = region_base + max(0, offset - overlap)
                    read_size = size + (overlap if offset else 0)
                    block = read_region(process, read_at, read_size)
                    if block:
                        readable_bytes += len(block) - (overlap if offset else 0)
                        minimum = region_base + offset
                        for target, needle in needles.items():
                            for found in occurrences(block, needle):
                                address = read_at + found
                                if address >= minimum:
                                    matches.append(
                                        (
                                            target,
                                            address,
                                            region_base,
                                            region_size,
                                            int(mbi.Type),
                                        )
                                    )
                    offset += size
            cursor = region_end
    finally:
        kernel32.CloseHandle(process)

    print(
        f"PID={pid} module={path} base=0x{base:x} size=0x{module_size:x} "
        f"regions={region_count} readable={readable_bytes}"
    )
    for target, address, region_base, region_size, region_type in matches:
        location = (
            f"module+0x{address - base:x}"
            if base <= address < base + module_size
            else "external"
        )
        print(
            f"TARGET=0x{target:x} POINTER=0x{address:x} {location} "
            f"REGION=0x{region_base:x}+0x{region_size:x} TYPE=0x{region_type:x}"
        )
    print(f"MATCHES={len(matches)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
