#!/usr/bin/env python3
"""Trace the Lua OnMsgDamageSyncV2 call immediately before lua_pcall.

This is a build-specific research tool.  It hooks the protected-call wrapper,
but records only calls from the decoded-msgpack dispatcher whose function
TValue matches the selected Lua closure.  It never changes Lua arguments.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import math
import struct
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


PCALL_WRAPPER_RVA = 0x98C2A40
DISPATCH_CALL_RETURN_RVA = 0x995464E
ON_DAMAGE_CLOSURE_TVALUE = 0xFFFB8005DD211DB8

PCALL_WRAPPER_PROLOGUE = bytes.fromhex(
    "48 89 5c 24 08 48 89 6c 24 10 48 89 74 24 18"
)
PCALL_WRAPPER_SIGNATURE = PCALL_WRAPPER_PROLOGUE + bytes.fromhex(
    "57 48 83 ec 30 8b f2 48 8b e9 48 8b 15 20 53 68 05"
)

MAGIC = b"GMZZLUA1"
RECORD_SIZE = 0x100
RECORD_COUNT = 4096
RECORD_MASK = RECORD_COUNT - 1
RECORDS_OFFSET = 0x100
RING_SIZE = RECORDS_OFFSET + RECORD_COUNT * RECORD_SIZE
COMMIT_OFFSET = 0xF8
ARGUMENT_COUNT = 10


def _near_branch(code: bytearray, opcode: bytes) -> int:
    code += opcode
    displacement_at = len(code)
    code += b"\x00\x00\x00\x00"
    return displacement_at


def _patch_branch(code: bytearray, displacement_at: int, target: int) -> None:
    struct.pack_into("<i", code, displacement_at, target - (displacement_at + 4))


def _store_rax_at_r11(code: bytearray, displacement: int) -> None:
    if displacement <= 0x7F:
        code += b"\x49\x89\x43" + bytes([displacement])
    else:
        code += b"\x49\x89\x83" + struct.pack("<I", displacement)


def build_trace_stub(
    ring: int,
    resume: int,
    *,
    expected_return: int,
    closure_tvalue: int,
) -> bytes:
    code = bytearray()

    # Preserve every register used by the recorder.  The original return
    # address is at [rsp+0x28] after these five 8-byte pushes.
    code += b"\x9c\x50\x53\x41\x52\x41\x53"
    misses: list[int] = []
    code += b"\x48\x8b\x44\x24\x28"
    code += b"\x49\xba" + struct.pack("<Q", expected_return)
    code += b"\x4c\x39\xd0"
    misses.append(_near_branch(code, b"\x0f\x85"))

    # OnMsgDamageSyncV2 has self plus nine business arguments.
    code += b"\x83\xfa" + bytes([ARGUMENT_COUNT])
    misses.append(_near_branch(code, b"\x0f\x85"))
    code += b"\x4c\x8b\x59\x28"  # r11 = L->top
    code += b"\x48\x8b\xc2\x48\xff\xc0\x48\xc1\xe0\x03"
    code += b"\x49\x29\xc3"  # r11 = function TValue address
    code += b"\x49\x8b\x03"
    code += b"\x49\xba" + struct.pack("<Q", closure_tvalue)
    code += b"\x4c\x39\xd0"
    misses.append(_near_branch(code, b"\x0f\x85"))

    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"  # sequence = atomic fetch_add
    code += b"\x48\x89\xc3"
    code += b"\x25" + struct.pack("<I", RECORD_MASK)
    code += b"\x48\xc1\xe0\x08"
    code += b"\x4d\x8d\x9c\x02" + struct.pack("<I", RECORDS_OFFSET)
    code += b"\x49\x89\x1b"

    # KUSER_SHARED_DATA.SystemTime, read with its documented consistency loop.
    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x41\x8b\x42\x18"
    code += b"\x41\x8b\x5a\x14"
    code += b"\x41\x3b\x42\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x48\xc1\xe0\x20\x48\x09\xd8"
    code += b"\x49\x89\x43\x08"

    code += b"\x65\x48\x8b\x04\x25\x48\x00\x00\x00"
    code += b"\x49\x89\x43\x10"  # thread id
    code += b"\x48\x8b\x44\x24\x28\x49\x89\x43\x18"
    code += b"\x49\x89\x4b\x20"  # lua_State
    code += b"\x49\x89\x53\x28"  # nargs
    code += b"\x4d\x89\x43\x30"  # nresults
    code += b"\x4d\x89\x4b\x38"  # errfunc

    code += b"\x48\x8b\x41\x28"
    code += b"\x49\x89\x43\x40"  # L->top
    code += b"\x49\x89\xc2"
    code += b"\x48\x8b\xc2\x48\xff\xc0\x48\xc1\xe0\x03"
    code += b"\x49\x29\xc2"
    code += b"\x4d\x89\x53\x48"  # function TValue address
    code += b"\x49\x8b\x02"
    code += b"\x49\x89\x43\x50"  # function TValue

    for index in range(ARGUMENT_COUNT):
        code += b"\x49\x8b\x42" + bytes([8 * (index + 1)])
        _store_rax_at_r11(code, 0x58 + 8 * index)

    code += b"\x49\x8b\x03\x48\xff\xc0"
    code += b"\x49\x89\x83" + struct.pack("<I", COMMIT_OFFSET)

    skip = len(code)
    for displacement_at in misses:
        _patch_branch(code, displacement_at, skip)
    code += b"\x41\x5b\x41\x5a\x5b\x58\x9d"
    code += PCALL_WRAPPER_PROLOGUE
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
    return bytes(code)


_TAG_NAMES = {
    -1: "nil",
    -2: "false",
    -3: "true",
    -4: "lightuserdata",
    -5: "string",
    -6: "upvalue",
    -7: "thread",
    -8: "proto",
    -9: "function",
    -10: "trace",
    -11: "cdata",
    -12: "table",
    -13: "userdata",
    -14: "number_sentinel",
}


def decode_tvalue(raw: int):
    signed = ctypes.c_int64(raw).value
    tag = signed >> 47
    if not -14 <= tag <= -1:
        value = struct.unpack("<d", struct.pack("<Q", raw))[0]
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return value
    if tag == -1:
        return None
    if tag == -2:
        return False
    if tag == -3:
        return True
    return {
        "type": _TAG_NAMES.get(tag, f"tag_{tag}"),
        "pointer": f"0x{raw & 0x00007FFFFFFFFFFF:012x}",
    }


def parse_record(raw: bytes, expected_sequence: int) -> dict | None:
    if len(raw) != RECORD_SIZE:
        return None
    header = struct.unpack_from("<11Q", raw)
    sequence = header[0]
    commit = struct.unpack_from("<Q", raw, COMMIT_OFFSET)[0]
    if sequence != expected_sequence or commit != expected_sequence + 1:
        return None
    (
        _,
        filetime,
        tid,
        return_address,
        lua_state,
        nargs,
        nresults,
        errfunc,
        top,
        function_cell,
        function_tvalue,
    ) = header
    raw_args = list(struct.unpack_from(f"<{ARGUMENT_COUNT}Q", raw, 0x58))
    args = [decode_tvalue(value) for value in raw_args]
    event_time = dt.datetime.fromtimestamp(
        (filetime - 116_444_736_000_000_000) / 10_000_000,
        tz=dt.timezone.utc,
    ).astimezone()
    fields = args[1:]
    return {
        "event_time": event_time.isoformat(timespec="microseconds"),
        "filetime_100ns": filetime,
        "sequence": sequence,
        "tid": tid,
        "function": "Lua::TakeDamageComponent.OnMsgDamageSyncV2",
        "return_address": f"0x{return_address:016x}",
        "lua_state": f"0x{lua_state:016x}",
        "nargs": nargs,
        "nresults": ctypes.c_int32(nresults & 0xFFFFFFFF).value,
        "errfunc": ctypes.c_int32(errfunc & 0xFFFFFFFF).value,
        "top": f"0x{top:016x}",
        "function_cell": f"0x{function_cell:016x}",
        "function_tvalue": f"0x{function_tvalue:016x}",
        "self": args[0],
        "attacker_id": fields[0],
        "target_id": fields[1],
        "skill_id_with_type": fields[2],
        "damage_method": fields[3],
        "damage_type": fields[4],
        "total_damage": fields[5],
        "shield_cost": fields[6],
        "real_damage": fields[7],
        "is_dead": fields[8],
        "raw_tvalues": [f"0x{value:016x}" for value in raw_args],
    }


def integer(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument(
        "--closure-tvalue", type=integer, default=ON_DAMAGE_CLOSURE_TVALUE
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research-v0.1.2/lua-damage-trace.jsonl"),
    )
    args = parser.parse_args()

    pid = args.pid or find_pid("C7-Win64-Shipping.exe")
    base, module_size, module_path = find_module(pid, "C7-Win64-Shipping.exe")
    if PCALL_WRAPPER_RVA + len(PCALL_WRAPPER_SIGNATURE) > module_size:
        raise SystemExit("pcall wrapper is outside the live module")
    target = base + PCALL_WRAPPER_RVA
    expected_return = base + DISPATCH_CALL_RETURN_RVA
    access = (
        PROCESS_QUERY_INFORMATION
        | PROCESS_VM_OPERATION
        | PROCESS_VM_READ
        | PROCESS_VM_WRITE
    )
    process = int(kernel32.OpenProcess(access, False, pid) or 0)
    if not process:
        raise winerror("OpenProcess(lua damage trace)")

    ring = 0
    stub = 0
    patch = b""
    installed = False
    suspended: list[int] = []
    next_sequence = 0
    try:
        actual = read_region(process, target, len(PCALL_WRAPPER_SIGNATURE))
        if actual != PCALL_WRAPPER_SIGNATURE:
            raise RuntimeError(
                "pcall wrapper signature mismatch; refusing to overwrite an "
                "unknown build or existing hook"
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
            raise winerror("VirtualAllocEx(lua damage ring)")
        stub = int(
            kernel32.VirtualAllocEx(
                process,
                None,
                0x1000,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_EXECUTE_READWRITE,
            )
            or 0
        )
        if not stub:
            raise winerror("VirtualAllocEx(lua damage stub)")
        write_memory(
            process,
            ring,
            struct.pack(
                "<8sQQQQQQ",
                MAGIC,
                0,
                RECORD_COUNT,
                RECORD_SIZE,
                target,
                expected_return,
                args.closure_tvalue,
            ),
        )
        stub_code = build_trace_stub(
            ring,
            target + len(PCALL_WRAPPER_PROLOGUE),
            expected_return=expected_return,
            closure_tvalue=args.closure_tvalue,
        )
        if len(stub_code) > 0x1000:
            raise AssertionError("lua trace stub exceeds its allocation")
        write_memory(process, stub, stub_code)
        kernel32.FlushInstructionCache(process, ctypes.c_void_p(stub), len(stub_code))
        patch = build_absolute_patch(stub, len(PCALL_WRAPPER_PROLOGUE))
        suspended = suspend_game_threads(pid)
        try:
            current = read_region(process, target, len(PCALL_WRAPPER_SIGNATURE))
            if current != PCALL_WRAPPER_SIGNATURE:
                raise RuntimeError("pcall wrapper changed while installing trace")
            write_code(process, target, patch)
            installed = True
        finally:
            resume_threads(suspended)
        if read_region(process, target, len(patch)) != patch:
            raise RuntimeError("lua damage trace patch verification failed")

        args.output.parent.mkdir(parents=True, exist_ok=True)
        print(
            f"READY pid={pid} module={module_path} target=0x{target:x} "
            f"closure=0x{args.closure_tvalue:016x} output={args.output}",
            flush=True,
        )
        deadline = time.monotonic() + max(0.25, args.seconds)
        with args.output.open("a", encoding="utf-8", buffering=1) as output:
            while time.monotonic() < deadline and process_alive(process):
                header = read_region(process, ring, 0x38)
                if not header or header[:8] != MAGIC:
                    raise RuntimeError("lua damage trace ring became unreadable")
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
    except KeyboardInterrupt:
        pass
    finally:
        if suspended:
            resume_threads(suspended)
        if installed and process_alive(process):
            suspended = suspend_game_threads(pid)
            try:
                current = read_region(process, target, len(patch))
                if current == patch:
                    write_code(process, target, PCALL_WRAPPER_PROLOGUE)
                else:
                    print(
                        "WARNING trace target changed; original bytes were not restored",
                        flush=True,
                    )
            finally:
                resume_threads(suspended)
        if process:
            kernel32.CloseHandle(process)
        print(f"STOP records={next_sequence}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
