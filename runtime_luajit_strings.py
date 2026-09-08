#!/usr/bin/env python3
"""Search LuaJIT 2.1 GC64's interned-string table in a live process."""

from __future__ import annotations

import argparse
import struct

from proc_inspect import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_pid,
    kernel32,
    winerror,
)
from runtime_luajit_gc import CachedReader, GCSTR_SIZE, POINTER_MASK, read_string


GLOBAL_STRING_STATE_OFFSET = 0x98


def integer(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("strings", nargs="+")
    parser.add_argument("--lua-state", type=integer, required=True)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--substring", action="store_true")
    args = parser.parse_args()
    wanted = {value.casefold() for value in args.strings}

    pid = args.pid or find_pid(args.process)
    process = int(
        kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        or 0
    )
    if not process:
        raise winerror("OpenProcess(runtime LuaJIT string search)")
    reader = CachedReader(process, max_chunks=512)
    try:
        state = reader.read(args.lua_state, 0x18)
        if state is None or len(state) != 0x18 or state[9] != 6:
            raise RuntimeError(
                f"0x{args.lua_state:x} does not look like a live lua_State"
            )
        global_state = struct.unpack_from("<Q", state, 0x10)[0]
        string_state = reader.read(global_state + GLOBAL_STRING_STATE_OFFSET, 0x10)
        if string_state is None or len(string_state) != 0x10:
            raise RuntimeError("could not read LuaJIT string-intern state")
        buckets_address, mask, declared_count = struct.unpack("<QII", string_state)
        if mask > 16_777_215:
            raise RuntimeError(f"implausible LuaJIT string-table mask: 0x{mask:x}")
        bucket_count = mask + 1
        buckets = reader.read(buckets_address, bucket_count * 8)
        if buckets is None or len(buckets) != bucket_count * 8:
            raise RuntimeError("could not read LuaJIT string-table buckets")

        seen: set[int] = set()
        matches: list[tuple[int, str]] = []
        unreadable = 0
        for raw_address in struct.unpack(f"<{bucket_count}Q", buckets):
            address = raw_address & POINTER_MASK
            bucket_seen: set[int] = set()
            while address and address not in bucket_seen:
                bucket_seen.add(address)
                if address in seen:
                    break
                seen.add(address)
                head = reader.read(address, GCSTR_SIZE)
                if head is None or len(head) != GCSTR_SIZE or head[9] != 4:
                    unreadable += 1
                    break
                text = read_string(reader, address)
                if text is not None:
                    folded = text.casefold()
                    matched = (
                        any(term in folded for term in wanted)
                        if args.substring
                        else folded in wanted
                    )
                    if matched:
                        matches.append((address, text))
                address = struct.unpack_from("<Q", head)[0] & POINTER_MASK

        print(
            f"STRINGS pid={pid} lua_state=0x{args.lua_state:x} "
            f"global=0x{global_state:x} buckets={bucket_count} "
            f"declared={declared_count} visited={len(seen)} unreadable={unreadable}"
        )
        for address, text in sorted(matches, key=lambda item: (item[1], item[0])):
            tagged = (0xFFFD_8000_0000_0000 | address) & 0xFFFF_FFFF_FFFF_FFFF
            print(f"STRING 0x{address:x} tagged=0x{tagged:016x} {text!r}")
        print(f"MATCHES={len(matches)}")
    finally:
        kernel32.CloseHandle(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
