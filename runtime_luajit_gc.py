#!/usr/bin/env python3
"""Search a live LuaJIT 2.1 GC64 heap without modifying the process."""

from __future__ import annotations

import argparse
import collections
import ctypes
import struct

from proc_inspect import (
    MEM_COMMIT,
    MEMORY_BASIC_INFORMATION,
    PAGE_GUARD,
    PAGE_NOACCESS,
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_pid,
    kernel32,
    read_region,
    winerror,
)
from inline_capture import resume_threads
from network_capture import suspend_game_threads


POINTER_MASK = (1 << 47) - 1
GCSTR_SIZE = 0x18
GCPROTO_SIZE = 0x68
GLOBAL_GC_ROOT_OFFSET = 0x28


def integer(value: str) -> int:
    return int(value, 0)


class CachedReader:
    """Small LRU cache for allocator pages visited by the GC chain."""

    def __init__(self, process: int, *, max_chunks: int = 256) -> None:
        self.process = process
        self.max_chunks = max_chunks
        self.cache: collections.OrderedDict[tuple[int, int], bytes] = (
            collections.OrderedDict()
        )

    def read(self, address: int, size: int) -> bytes | None:
        if address < 0x10000 or size < 0:
            return None
        for key, data in tuple(self.cache.items()):
            base, length = key
            if base <= address and address + size <= base + length:
                self.cache.move_to_end(key)
                offset = address - base
                return data[offset : offset + size]

        mbi = MEMORY_BASIC_INFORMATION()
        queried = kernel32.VirtualQueryEx(
            self.process,
            ctypes.c_void_p(address),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi),
        )
        if not queried:
            return None
        region_base = int(mbi.BaseAddress or 0)
        region_size = int(mbi.RegionSize)
        if (
            mbi.State != MEM_COMMIT
            or mbi.Protect & PAGE_GUARD
            or (mbi.Protect & 0xFF) == PAGE_NOACCESS
        ):
            return None
        if address + size > region_base + region_size:
            return read_region(self.process, address, size)
        chunk_size = 0x40000
        relative = address - region_base
        chunk_base = region_base + relative // chunk_size * chunk_size
        length = min(chunk_size, region_base + region_size - chunk_base)
        data = read_region(self.process, chunk_base, length)
        if data is None or address + size > chunk_base + len(data):
            return read_region(self.process, address, size)
        key = (chunk_base, len(data))
        self.cache[key] = data
        self.cache.move_to_end(key)
        while len(self.cache) > self.max_chunks:
            self.cache.popitem(last=False)
        offset = address - chunk_base
        return data[offset : offset + size]


def gc_pointer(raw: int) -> int:
    return raw & POINTER_MASK


def read_string(reader: CachedReader, address: int) -> str | None:
    address = gc_pointer(address)
    head = reader.read(address, GCSTR_SIZE)
    if head is None or len(head) != GCSTR_SIZE or head[9] != 4:
        return None
    length = struct.unpack_from("<I", head, 0x14)[0]
    if length > 1024 * 1024:
        return None
    raw = reader.read(address + GCSTR_SIZE, length)
    if raw is None or len(raw) != length:
        return None
    return raw.decode("utf-8", errors="replace")


def proto_details(
    reader: CachedReader,
    address: int,
    string_cache: dict[int, str | None],
) -> tuple[str | None, list[tuple[int, str]]]:
    raw = reader.read(address, GCPROTO_SIZE)
    if raw is None or len(raw) != GCPROTO_SIZE or raw[9] != 7:
        return None, []
    k = struct.unpack_from("<Q", raw, 0x20)[0]
    sizekgc = struct.unpack_from("<I", raw, 0x30)[0]
    chunk_address = gc_pointer(struct.unpack_from("<Q", raw, 0x40)[0])
    if sizekgc > 100_000:
        return None, []
    if chunk_address not in string_cache:
        string_cache[chunk_address] = read_string(reader, chunk_address)
    constants: list[tuple[int, str]] = []
    cells = reader.read(k - sizekgc * 8, sizekgc * 8) if sizekgc else b""
    if cells is None or len(cells) != sizekgc * 8:
        return string_cache[chunk_address], []
    for logical_index in range(sizekgc):
        offset = (sizekgc - logical_index - 1) * 8
        constant_address = gc_pointer(struct.unpack_from("<Q", cells, offset)[0])
        if constant_address not in string_cache:
            string_cache[constant_address] = read_string(reader, constant_address)
        text = string_cache[constant_address]
        if text is not None:
            constants.append((logical_index, text))
    return string_cache[chunk_address], constants


def matches(text: str | None, needles: tuple[str, ...]) -> bool:
    if text is None:
        return False
    folded = text.casefold()
    return any(needle in folded for needle in needles)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lua-state", type=integer, required=True)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--contains", action="append", default=[])
    parser.add_argument(
        "--chunk",
        action="append",
        default=[],
        help="only inspect prototypes whose chunk path contains this text",
    )
    parser.add_argument(
        "--constants-only",
        action="store_true",
        help="do not count a matching chunk path as a prototype match",
    )
    parser.add_argument("--max-objects", type=int, default=2_000_000)
    parser.add_argument(
        "--suspend",
        action="store_true",
        help="briefly suspend game threads for a coherent GC-chain snapshot",
    )
    args = parser.parse_args()
    needles = tuple(
        value.casefold()
        for value in (args.contains or ["statistic"])
        if value.strip()
    )
    chunk_needles = tuple(value.casefold() for value in args.chunk if value.strip())

    pid = args.pid or find_pid(args.process)
    process = int(
        kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        or 0
    )
    if not process:
        raise winerror("OpenProcess(runtime LuaJIT GC search)")
    reader = CachedReader(process)
    suspended: list[int] = []
    try:
        if args.suspend:
            suspended = suspend_game_threads(pid)
        state = reader.read(args.lua_state, 0x18)
        if state is None or len(state) != 0x18 or state[9] != 6:
            raise RuntimeError(
                f"0x{args.lua_state:x} does not look like a live lua_State"
            )
        global_state = struct.unpack_from("<Q", state, 0x10)[0]
        root_data = reader.read(global_state + GLOBAL_GC_ROOT_OFFSET, 8)
        if root_data is None or len(root_data) != 8:
            raise RuntimeError("could not read LuaJIT GC root")
        address = gc_pointer(struct.unpack("<Q", root_data)[0])

        seen: set[int] = set()
        type_counts: collections.Counter[int] = collections.Counter()
        function_ffids: collections.Counter[int] = collections.Counter()
        relevant_strings: dict[int, str] = {}
        protos: set[int] = set()
        string_cache: dict[int, str | None] = {}
        stopped = "end"
        while address and address not in seen:
            if len(seen) >= max(1, args.max_objects):
                stopped = "object_limit"
                break
            head = reader.read(address, GCSTR_SIZE)
            if head is None or len(head) != GCSTR_SIZE:
                stopped = f"unreadable_0x{address:x}"
                break
            seen.add(address)
            gct = head[9]
            type_counts[gct] += 1
            if gct == 4:
                text = read_string(reader, address)
                string_cache[address] = text
                if matches(text, needles):
                    relevant_strings[address] = text or ""
            elif gct == 7:
                protos.add(address)
            elif gct == 8:
                function = reader.read(address, 0x28)
                if function is not None and len(function) == 0x28:
                    ffid = function[0x0A]
                    function_ffids[ffid] += 1
                    pc = struct.unpack_from("<Q", function, 0x20)[0]
                    if ffid == 0 and pc >= GCPROTO_SIZE + 0x10000:
                        protos.add(pc - GCPROTO_SIZE)
            address = gc_pointer(struct.unpack_from("<Q", head)[0])

        print(
            f"GC pid={pid} lua_state=0x{args.lua_state:x} "
            f"global=0x{global_state:x} objects={len(seen)} stopped={stopped} "
            f"types={dict(sorted(type_counts.items()))} "
            f"function_ffids={dict(sorted(function_ffids.items()))}"
        )
        for string_address, text in sorted(relevant_strings.items()):
            print(f"STRING 0x{string_address:x} {text!r}")

        matched_protos = 0
        for proto_address in sorted(protos):
            chunk, constants = proto_details(reader, proto_address, string_cache)
            if chunk_needles and not matches(chunk, chunk_needles):
                continue
            matching_constants = [
                (index, text)
                for index, text in constants
                if matches(text, needles)
            ]
            if not matching_constants and (
                args.constants_only or not matches(chunk, needles)
            ):
                continue
            matched_protos += 1
            raw = reader.read(proto_address, GCPROTO_SIZE) or b""
            firstline = struct.unpack_from("<i", raw, 0x48)[0]
            numline = struct.unpack_from("<i", raw, 0x4C)[0]
            sizebc = struct.unpack_from("<I", raw, 0x0C)[0]
            print(
                f"PROTO 0x{proto_address:x} lines={firstline}+{numline} "
                f"bc={sizebc} chunk={chunk!r}"
            )
            for index, text in constants:
                print(f"  KGC[{index}] {text!r}")
        print(
            f"SUMMARY strings={len(relevant_strings)} "
            f"protos={matched_protos}/{len(protos)}"
        )
    finally:
        if suspended:
            resume_threads(suspended)
        kernel32.CloseHandle(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
