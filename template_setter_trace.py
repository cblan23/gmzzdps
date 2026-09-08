#!/usr/bin/env python3
"""Trace CommonComponent template assignments in the current live build.

This read-mostly research hook records the individual TemplateId setter and
the generated bulk CommonComponent application path.  It exists to establish
whether BossType is observed before or after TemplateId is assigned; it does
not alter arguments or return values.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import struct
import sys
import time
from pathlib import Path

from inline_capture import (
    MEM_COMMIT,
    MEM_RESERVE,
    PAGE_EXECUTE_READWRITE,
    PAGE_READWRITE,
    PROCESS_VM_OPERATION,
    PROCESS_VM_WRITE,
    build_absolute_patch,
    process_alive,
    resume_threads,
    write_code,
    write_memory,
)
from network_capture import suspend_game_threads
from proc_inspect import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_module,
    find_pid,
    kernel32,
    read_region,
    winerror,
)


SETTER_RVA = 0x68D78F0
BULK_RVA = 0x68D70A0
SETTER_PROLOGUE = bytes.fromhex(
    "48 89 5c 24 20 57 48 83 ec 50 48 8b 05 17 33 eb 07"
)
BULK_PROLOGUE = bytes.fromhex(
    "40 55 53 56 57 41 55 41 56 41 57 48 8d 6c 24 f9"
)
SETTER_SIGNATURE = SETTER_PROLOGUE + bytes.fromhex("8b da 48 8b f9")
BULK_SIGNATURE = BULK_PROLOGUE + bytes.fromhex("48 81 ec c0 00 00 00")

MAGIC = b"GMZZTPL1"
RECORD_SIZE = 0x80
RECORD_COUNT = 4096
RECORD_MASK = RECORD_COUNT - 1
RECORDS_OFFSET = 0x100
RING_SIZE = RECORDS_OFFSET + RECORD_COUNT * RECORD_SIZE
COMMIT_OFFSET = 0x78
ENTITY_ID_OFFSET = 0x58
BOSS_TYPE_OFFSET = 0x137
TEMPLATE_ID_OFFSET = 0x198


def _timestamp(code: bytearray) -> None:
    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x41\x8b\x42\x18"
    code += b"\x41\x8b\x52\x14"
    code += b"\x41\x3b\x42\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0"
    code += b"\x49\x89\x43\x08"


def build_trace_stub(
    ring: int,
    target: int,
    *,
    source: int,
    prologue: bytes,
    bulk: bool,
) -> bytes:
    code = bytearray()
    code += b"\x9c\x50\x53\x52\x41\x52\x41\x53"
    # Six saved 8-byte cells put the entry return address at [rsp+0x30].
    # The bulk function's ninth argument was at entry [rsp+0x48], hence
    # [rsp+0x78] after the saves.
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"
    code += b"\x48\x89\xc3"
    code += b"\x25" + struct.pack("<I", RECORD_MASK)
    code += b"\x48\xc1\xe0\x07"
    code += b"\x4d\x8d\x9c\x02" + struct.pack("<I", RECORDS_OFFSET)
    code += b"\x49\x89\x1b"
    code += b"\x49\xc7\x43\x10" + struct.pack("<I", source)
    code += b"\x49\x89\x4b\x18"
    code += b"\x48\x8b\x81" + struct.pack("<I", ENTITY_ID_OFFSET)
    code += b"\x49\x89\x43\x20"
    if bulk:
        code += b"\x48\x8b\x44\x24\x78"
    else:
        code += b"\x8b\xc2"
    code += b"\x49\x89\x43\x28"
    code += b"\x8b\x81" + struct.pack("<I", TEMPLATE_ID_OFFSET)
    code += b"\x49\x89\x43\x30"
    code += b"\x0f\xb6\x81" + struct.pack("<I", BOSS_TYPE_OFFSET)
    code += b"\x49\x89\x43\x38"
    code += b"\x48\x8b\x44\x24\x30\x49\x89\x43\x40"
    _timestamp(code)
    code += b"\x48\xff\xc3\x49\x89\x5b\x78"
    code += b"\x41\x5b\x41\x5a\x5a\x5b\x58\x9d"
    if bulk:
        code += prologue
    else:
        displacement = struct.unpack_from("<i", prologue, 13)[0]
        global_address = target + len(prologue) + displacement
        code += prologue[:10]
        code += b"\x48\xa1" + struct.pack("<Q", global_address)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack(
        "<Q", target + len(prologue)
    )
    return bytes(code)


def parse_record(raw: bytes, expected: int) -> dict | None:
    if len(raw) != RECORD_SIZE:
        return None
    values = struct.unpack_from("<9Q", raw)
    sequence, filetime, source, component, entity_id, value, old, boss_type, ret = values
    commit = struct.unpack_from("<Q", raw, COMMIT_OFFSET)[0]
    if sequence != expected or commit != expected + 1:
        return None
    event_time = dt.datetime.fromtimestamp(
        (filetime - 116_444_736_000_000_000) / 10_000_000,
        tz=dt.timezone.utc,
    ).astimezone()
    return {
        "event_time": event_time.isoformat(timespec="microseconds"),
        "sequence": sequence,
        "source": "template_setter" if source == 1 else "common_bulk_apply",
        "component": component,
        "entity_id": entity_id,
        "new_template_id": value & 0xFFFF_FFFF,
        "old_template_id": old & 0xFFFF_FFFF,
        "boss_type_before": boss_type & 0xFF,
        "return_address": f"0x{ret:016x}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument(
        "--output", type=Path, default=Path("research-v0.1.2/template-setter.jsonl")
    )
    args = parser.parse_args()
    pid = args.pid or find_pid("C7-Win64-Shipping.exe")
    base, module_size, module_path = find_module(pid, "C7-Win64-Shipping.exe")
    targets = (
        (SETTER_RVA, SETTER_PROLOGUE, SETTER_SIGNATURE, 1, False),
        (BULK_RVA, BULK_PROLOGUE, BULK_SIGNATURE, 2, True),
    )
    if any(rva + len(signature) > module_size for rva, _, signature, _, _ in targets):
        raise SystemExit("trace target is outside the module")
    access = (
        PROCESS_QUERY_INFORMATION
        | PROCESS_VM_OPERATION
        | PROCESS_VM_READ
        | PROCESS_VM_WRITE
    )
    process = int(kernel32.OpenProcess(access, False, pid) or 0)
    if not process:
        raise winerror("OpenProcess(template setter trace)")
    ring = 0
    pages: list[int] = []
    installed: list[tuple[int, bytes]] = []
    suspended: list[int] = []
    next_sequence = 0
    try:
        for rva, _, signature, _, _ in targets:
            actual = read_region(process, base + rva, len(signature))
            if actual != signature:
                raise RuntimeError(
                    f"signature mismatch at RVA 0x{rva:x}: "
                    f"expected {signature.hex()} got {(actual or b'').hex()}"
                )
        ring = int(
            kernel32.VirtualAllocEx(
                process,
                None,
                RING_SIZE,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_READWRITE,
            )
            or 0
        )
        if not ring:
            raise winerror("VirtualAllocEx(template trace ring)")
        write_memory(
            process,
            ring,
            struct.pack("<8sQQQ", MAGIC, 0, RECORD_COUNT, RECORD_SIZE),
        )
        patches: list[tuple[int, bytes, bytes]] = []
        for rva, prologue, _signature, source, bulk in targets:
            page = int(
                kernel32.VirtualAllocEx(
                    process,
                    None,
                    0x1000,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_EXECUTE_READWRITE,
                )
                or 0
            )
            if not page:
                raise winerror("VirtualAllocEx(template trace stub)")
            pages.append(page)
            target = base + rva
            stub = build_trace_stub(
                ring,
                target,
                source=source,
                prologue=prologue,
                bulk=bulk,
            )
            write_memory(process, page, stub)
            kernel32.FlushInstructionCache(process, ctypes.c_void_p(page), len(stub))
            patches.append(
                (target, prologue, build_absolute_patch(page, len(prologue)))
            )
        suspended = suspend_game_threads(pid)
        try:
            for target, prologue, patch in patches:
                write_code(process, target, patch)
                installed.append((target, prologue))
        finally:
            resume_threads(suspended)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        print(
            f"READY pid={pid} module={module_path} ring=0x{ring:x} "
            f"output={args.output}",
            flush=True,
        )
        deadline = time.monotonic() + max(0.25, args.seconds)
        with args.output.open("a", encoding="utf-8", buffering=1) as output:
            while time.monotonic() < deadline and process_alive(process):
                header = read_region(process, ring, 0x20)
                if not header or header[:8] != MAGIC:
                    raise RuntimeError("template trace ring became unreadable")
                write_index = struct.unpack_from("<Q", header, 8)[0]
                if write_index - next_sequence > RECORD_COUNT:
                    next_sequence = write_index - RECORD_COUNT
                progressed = False
                while next_sequence < write_index:
                    slot = next_sequence & RECORD_MASK
                    raw = read_region(
                        process,
                        ring + RECORDS_OFFSET + slot * RECORD_SIZE,
                        RECORD_SIZE,
                    )
                    record = parse_record(raw or b"", next_sequence)
                    if record is None:
                        break
                    line = json.dumps(record, ensure_ascii=False)
                    output.write(line + "\n")
                    print(line, flush=True)
                    next_sequence += 1
                    progressed = True
                if not progressed:
                    time.sleep(0.02)
    finally:
        if suspended:
            resume_threads(suspended)
        if installed and process_alive(process):
            try:
                suspended = suspend_game_threads(pid)
                try:
                    for target, prologue in reversed(installed):
                        write_code(process, target, prologue)
                    installed.clear()
                finally:
                    resume_threads(suspended)
            except Exception as exc:
                print(f"warning: failed to restore trace: {exc}", file=sys.stderr)
        # Once a hook has been active, retain its remote pages until the game
        # exits. A thread already inside a restored stub may still return there.
        kernel32.CloseHandle(process)
    print(f"DONE records={next_sequence}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
