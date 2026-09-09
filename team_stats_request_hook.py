#!/usr/bin/env python3
"""Request server combat snapshots through the game's own RPC thread.

The hook is intentionally narrow and build-specific.  It intercepts the
confirmed ScriptEntity::call_server overload, round-robins a bounded set of
server-authorized statistics requests, then continues the original request
untouched. Numeric research requests borrow one live Lua argument cell only
for the duration of the nested call and restore its exact bits before the
original request continues. Production can select the legacy primary-only
path, which retains the stable zero-argument team request without installing
the outbound recorder or any argument-bearing request machinery. All
entry-point bytes are restored on close.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import struct
import time
from collections.abc import Mapping
from pathlib import Path

from inline_capture import (
    MEM_COMMIT,
    MEM_RELEASE,
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
from runtime_capability import (
    load_runtime_profile_file,
    normalize_runtime_profile,
    runtime_profile_hook,
)


STATE_MAGIC = b"GMZZSTAT"
PRIMARY_TEAM_STATE_SIZE = 0x300
CONTROL_STATE_SIZE = 0x600
CODE_SIZE = 0x1000
MAX_REQUEST_METHODS = 4
STATE_ENABLED_OFFSET = 0x08
STATE_LAST_REQUEST_OFFSET = 0x10
STATE_REQUEST_COUNT_OFFSET = 0x18
STATE_LAST_RESULT_OFFSET = 0x20
STATE_METHOD_COUNT_OFFSET = 0x28
STATE_NEXT_METHOD_INDEX_OFFSET = 0x30
STATE_LAST_METHOD_INDEX_OFFSET = 0x38
STATE_ARGUMENTS_OFFSET = 0x100
STATE_ARGUMENTS_SIZE = 0x78
STATE_VARIADIC_OFFSET = 0x180
STATE_EMPTY_DESCRIPTOR_OFFSET = 0x1C0
STATE_NULL_CELL_OFFSET = 0x200
STATE_TRAILING_CELL_OFFSET = 0x208
STATE_METHOD_OFFSET = 0x240
STATE_METHOD_TEXT_OFFSET = 0x280
STATE_METHOD_STRIDE = 0xC0
STATE_METHOD_ARGUMENT_KIND_OFFSET = 0x20
STATE_METHOD_ARGUMENT_VALUE_OFFSET = 0x28
STATE_NUMERIC_SLOT_OFFSET = 0x550
STATE_NUMERIC_ORIGINAL_CELL_OFFSET = 0x558
STATE_NUMERIC_VARIADIC_OFFSET = 0x580
STATE_NUMERIC_VARIADIC_SIZE = 0x40
STATE_STRING_DESCRIPTOR_OFFSET = 0x5C0
REQUEST_ARGUMENT_NONE = 0
REQUEST_ARGUMENT_NUMBER = 1
REQUEST_ARGUMENT_RAW_LUA_VALUE = 2
LUA_GC64_POINTER_MASK = (1 << 47) - 1
LUA_GC64_STRING_TAG = 0xFFFD_8000_0000_0000
LUA_STATE_GLOBAL_STATE_OFFSET = 0x10
LUA_GLOBAL_STRING_TABLE_OFFSET = 0x98
LUA_STRING_TABLE_SEED_OFFSET = 0x18
LUA_GC_STRING_HEADER_SIZE = 0x18
LUA_STRING_CHAIN_LIMIT = 64
REQUEST_RING_OFFSET = CONTROL_STATE_SIZE
REQUEST_RING_MAGIC = b"GMZZREQ2"
REQUEST_RECORD_SIZE = 0x200
REQUEST_RECORD_COUNT = 2048
REQUEST_RECORD_MASK = REQUEST_RECORD_COUNT - 1
REQUEST_RECORDS_OFFSET = 0x100
REQUEST_CONTEXT_SNAPSHOT_OFFSET = 0xC0
REQUEST_CONTEXT_SNAPSHOT_SIZE = 0x78
REQUEST_VARIADIC_SNAPSHOT_OFFSET = 0x140
REQUEST_VARIADIC_SNAPSHOT_SIZE = 0x40
REQUEST_VARIADIC_STORAGE_SNAPSHOT_SIZE = 0x200
LUA_STATE_STACK_BASE_OFFSET = 0x20
LUA_STATE_STACK_TOP_OFFSET = 0x28
LUA_STATE_SNAPSHOT_SIZE = 0x30
MAX_CAPTURED_LUA_ARGUMENTS = 64
REQUEST_STACK_CELLS_OFFSET = 0x180
REQUEST_STACK_CELL_COUNT = 4
REQUEST_ARGUMENT_SYNC_STATE_OFFSET = 0x1A0
REQUEST_ARGUMENT_ACK_OFFSET = 0x1A8
REQUEST_COMMIT_OFFSET = 0x1F8
REQUEST_ARGUMENT_SYNC_TIMEOUT_100NS = 2_500_000  # 250 ms hard fail-open
REQUEST_RING_SIZE = (
    REQUEST_RECORDS_OFFSET + REQUEST_RECORD_COUNT * REQUEST_RECORD_SIZE
)
STATE_STRING_STORAGE_OFFSET = REQUEST_RING_OFFSET + REQUEST_RING_SIZE
STATE_STRING_STORAGE_SIZE = 0x48
STATE_STRING_VECTOR_OFFSET = STATE_STRING_STORAGE_OFFSET + 0x50
STATE_STRING_VECTOR_SIZE = 0x08
STATE_SIZE = STATE_STRING_VECTOR_OFFSET + STATE_STRING_VECTOR_SIZE
ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE = 0x80
MAX_ARGUMENT_DESCRIPTOR_COUNT = 64
STRING_DESCRIPTOR_SCAN_RADIUS = 0x400
DUMMY_LUA_STRING_TVALUE = LUA_GC64_STRING_TAG | 0x1_0000

# The entry-point stub first saves flags, RAX/RBX/RCX/RDX/RSI/RDI, then
# R8-R13.  These offsets are therefore the original Windows x64 call values.
SAVED_R9_STACK_OFFSET = 0x20
SAVED_R8_STACK_OFFSET = 0x28
SAVED_RDX_STACK_OFFSET = 0x40
SAVED_RCX_STACK_OFFSET = 0x48
SAVED_RETURN_ADDRESS_STACK_OFFSET = 0x68
# Capture entry-RSP +0x20 through +0x38 verbatim.  This deliberately includes
# the final home-space cell as well as the following stack-argument cells; no
# ABI meaning is assigned until a concrete call site proves it.
REQUEST_ENTRY_STACK_CAPTURE_OFFSET = 0x88
TRAMPOLINE_OFFSET = 0x800


def _emit_near_jump(code: bytearray, opcode: bytes) -> int:
    code += opcode
    displacement_at = len(code)
    code += b"\x00\x00\x00\x00"
    return displacement_at


def _patch_near_jump(code: bytearray, displacement_at: int, target: int) -> None:
    struct.pack_into(
        "<i", code, displacement_at, target - (displacement_at + 4)
    )


def _emit_request_method_candidate(code: bytearray, method: bytes) -> int:
    """Branch to a shared match target when record R13 has this method."""

    if not method or len(method) > 0x7F:
        raise ValueError("synchronized request method length is unsupported")
    mismatches: list[int] = []
    code += b"\x49\x83\x7d\x38" + bytes([len(method)])
    mismatches.append(_emit_near_jump(code, b"\x0f\x85"))
    offset = 0
    while len(method) - offset >= 8:
        code += b"\x48\xb8" + method[offset : offset + 8]
        code += b"\x49\x39\x85" + struct.pack("<I", 0x40 + offset)
        mismatches.append(_emit_near_jump(code, b"\x0f\x85"))
        offset += 8
    if len(method) - offset >= 4:
        code += b"\xb8" + method[offset : offset + 4]
        code += b"\x41\x39\x85" + struct.pack("<I", 0x40 + offset)
        mismatches.append(_emit_near_jump(code, b"\x0f\x85"))
        offset += 4
    if len(method) - offset >= 2:
        code += b"\x66\xb8" + method[offset : offset + 2]
        code += b"\x66\x41\x39\x85" + struct.pack(
            "<I", 0x40 + offset
        )
        mismatches.append(_emit_near_jump(code, b"\x0f\x85"))
        offset += 2
    if len(method) - offset:
        code += b"\xb0" + method[offset : offset + 1]
        code += b"\x41\x38\x85" + struct.pack("<I", 0x40 + offset)
        mismatches.append(_emit_near_jump(code, b"\x0f\x85"))
    matched = _emit_near_jump(code, b"\xe9")
    next_candidate = len(code)
    for displacement_at in mismatches:
        _patch_near_jump(code, displacement_at, next_candidate)
    return matched


def _emit_request_capture(
    code: bytearray,
    state: int,
    synchronized_methods: tuple[bytes, ...],
) -> None:
    """Append a fail-passive raw outbound-RPC recorder to ``code``.

    The recorder copies only memory that belongs to the live call and commits
    the slot last.  It never interprets the context, variadic object, or stack
    cells inside the game process.
    """

    ring = state + REQUEST_RING_OFFSET
    code += b"\xfc"  # cld; the caller's flags are restored before resuming
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"  # lock xadd [r10+8], rax
    code += b"\x48\x89\xc3"  # rbx = sequence
    code += b"\x25" + struct.pack("<I", REQUEST_RECORD_MASK)
    code += b"\x48\xc1\xe0\x09"  # record size 0x200
    code += b"\x4d\x8d\xac\x02" + struct.pack(
        "<I", REQUEST_RECORDS_OFFSET
    )  # r13 = record
    code += b"\x49\x89\x5d\x00"  # sequence
    code += b"\x49\xc7\x85" + struct.pack(
        "<I", REQUEST_COMMIT_OFFSET
    ) + b"\x00\x00\x00\x00"
    code += b"\x49\xc7\x85" + struct.pack(
        "<I", REQUEST_ARGUMENT_SYNC_STATE_OFFSET
    ) + b"\x00\x00\x00\x00"
    code += b"\x49\xc7\x85" + struct.pack(
        "<I", REQUEST_ARGUMENT_ACK_OFFSET
    ) + b"\x00\x00\x00\x00"

    # Copy the four register arguments and return address from their saved,
    # immutable entry values.  Keeping each load explicit also makes the ABI
    # offsets mechanically verifiable in the generated stub.
    code += b"\x48\x8b\x44\x24" + bytes([SAVED_RCX_STACK_OFFSET])
    code += b"\x49\x89\x45\x10"
    code += b"\x48\x8b\x44\x24" + bytes([SAVED_RDX_STACK_OFFSET])
    code += b"\x49\x89\x45\x18"
    code += b"\x48\x8b\x44\x24" + bytes([SAVED_R8_STACK_OFFSET])
    code += b"\x49\x89\x45\x20"
    code += b"\x48\x8b\x44\x24" + bytes([SAVED_R9_STACK_OFFSET])
    code += b"\x49\x89\x45\x28"
    code += b"\x48\x8b\x44\x24" + bytes(
        [SAVED_RETURN_ADDRESS_STACK_OFFSET]
    )
    code += b"\x49\x89\x45\x30"
    code += b"\x49\xc7\x45\x38\x00\x00\x00\x00"

    # Read a coherent KUSER_SHARED_DATA.SystemTime value into R12.
    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x45\x8b\x62\x18"
    code += b"\x41\x8b\x52\x14"
    code += b"\x45\x3b\x62\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x49\xc1\xe4\x20"
    code += b"\x49\x09\xd4"
    code += b"\x4d\x89\x65\x08"

    # Preserve the call-owned structures before the original function can
    # reuse them. Null is retained as an all-zero snapshot.
    code += b"\x31\xc0"
    code += b"\x49\x8d\xbd" + struct.pack(
        "<I", REQUEST_CONTEXT_SNAPSHOT_OFFSET
    )
    code += b"\xb9\x18\x00\x00\x00"
    code += b"\xf3\x48\xab"  # context, padding, and variadic snapshot

    code += b"\x48\x8b\x74\x24" + bytes([SAVED_RDX_STACK_OFFSET])
    code += b"\x48\x85\xf6"
    context_done = _emit_near_jump(code, b"\x0f\x84")
    code += b"\x49\x8d\xbd" + struct.pack(
        "<I", REQUEST_CONTEXT_SNAPSHOT_OFFSET
    )
    code += b"\xb9" + struct.pack("<I", REQUEST_CONTEXT_SNAPSHOT_SIZE // 8)
    code += b"\xf3\x48\xa5"
    _patch_near_jump(code, context_done, len(code))

    code += b"\x48\x8b\x74\x24" + bytes([SAVED_R9_STACK_OFFSET])
    code += b"\x48\x85\xf6"
    variadic_done = _emit_near_jump(code, b"\x0f\x84")
    code += b"\x49\x8d\xbd" + struct.pack(
        "<I", REQUEST_VARIADIC_SNAPSHOT_OFFSET
    )
    code += b"\xb9" + struct.pack("<I", REQUEST_VARIADIC_SNAPSHOT_SIZE // 8)
    code += b"\xf3\x48\xa5"
    _patch_near_jump(code, variadic_done, len(code))

    code += b"\x48\x8d\xb4\x24" + struct.pack(
        "<I", REQUEST_ENTRY_STACK_CAPTURE_OFFSET
    )
    code += b"\x49\x8d\xbd" + struct.pack("<I", REQUEST_STACK_CELLS_OFFSET)
    code += b"\xb9" + struct.pack("<I", REQUEST_STACK_CELL_COUNT)
    code += b"\xf3\x48\xa5"

    # Copy a bounded MSVC std::string method name.  The full declared length
    # remains in the fixed header even when the text is longer than 127 bytes.
    code += b"\x41\xc6\x45\x40\x00"
    code += b"\x48\x8b\x74\x24" + bytes([SAVED_R8_STACK_OFFSET])
    code += b"\x48\x85\xf6"
    method_done_jumps = [_emit_near_jump(code, b"\x0f\x84")]
    code += b"\x48\x8b\x4e\x10"
    code += b"\x49\x89\x4d\x38"
    code += b"\x48\x85\xc9"
    method_done_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    code += b"\x48\x83\x7e\x18\x10"
    inline_jump = len(code)
    code += b"\x72\x00"
    code += b"\x48\x8b\x16"
    data_ready_jump = len(code)
    code += b"\xeb\x00"
    inline_data = len(code)
    code += b"\x48\x89\xf2"
    data_ready = len(code)
    code[inline_jump + 1] = (inline_data - (inline_jump + 2)) & 0xFF
    code[data_ready_jump + 1] = (data_ready - (data_ready_jump + 2)) & 0xFF
    code += b"\x48\x83\xf9\x7f"
    bounded_jump = len(code)
    code += b"\x76\x00"
    code += b"\xb9\x7f\x00\x00\x00"
    bounded = len(code)
    code[bounded_jump + 1] = (bounded - (bounded_jump + 2)) & 0xFF
    code += b"\x48\x89\xd6"
    code += b"\x49\x8d\x7d\x40"
    code += b"\xf3\xa4"
    code += b"\xc6\x07\x00"
    method_done = len(code)
    for displacement_at in method_done_jumps:
        _patch_near_jump(code, displacement_at, method_done)

    if synchronized_methods:
        matched_jumps = [
            _emit_request_method_candidate(code, method)
            for method in synchronized_methods
        ]
        skip_sync = _emit_near_jump(code, b"\xe9")
        matched = len(code)
        for displacement_at in matched_jumps:
            _patch_near_jump(code, displacement_at, matched)
        code += b"\x49\xc7\x85" + struct.pack(
            "<I", REQUEST_ARGUMENT_SYNC_STATE_OFFSET
        ) + b"\x01\x00\x00\x00"
        _patch_near_jump(code, skip_sync, len(code))

    code += b"\x48\x8d\x43\x01"
    code += b"\x49\x89\x85" + struct.pack("<I", REQUEST_COMMIT_OFFSET)

    if synchronized_methods:
        # A synchronized call stays at the hook entry while the controller
        # reads its live variadic storage.  It resumes immediately after the
        # acknowledgement and always fails open after 250 ms.
        code += b"\x49\x83\xbd" + struct.pack(
            "<I", REQUEST_ARGUMENT_SYNC_STATE_OFFSET
        ) + b"\x01"
        no_wait = _emit_near_jump(code, b"\x0f\x85")
        code += b"\x4c\x8d\x4b\x01"  # r9 = sequence + 1
        wait_loop = len(code)
        code += b"\x4d\x39\x8d" + struct.pack(
            "<I", REQUEST_ARGUMENT_ACK_OFFSET
        )
        acknowledged = _emit_near_jump(code, b"\x0f\x84")
        code += b"\xf3\x90"
        code += b"\x41\xba\x00\x00\xfe\x7f"
        clock_retry = len(code)
        code += b"\x41\x8b\x42\x18"
        code += b"\x41\x8b\x52\x14"
        code += b"\x41\x3b\x42\x1c"
        changed = _emit_near_jump(code, b"\x0f\x85")
        _patch_near_jump(code, changed, clock_retry)
        code += b"\x48\xc1\xe0\x20\x48\x09\xd0"
        code += b"\x49\x2b\x45\x08"
        code += b"\x48\x3d" + struct.pack(
            "<I", REQUEST_ARGUMENT_SYNC_TIMEOUT_100NS
        )
        keep_waiting = _emit_near_jump(code, b"\x0f\x82")
        _patch_near_jump(code, keep_waiting, wait_loop)
        code += b"\x49\xc7\x85" + struct.pack(
            "<I", REQUEST_ARGUMENT_SYNC_STATE_OFFSET
        ) + b"\x03\x00\x00\x00"
        wait_done = _emit_near_jump(code, b"\xe9")
        acknowledged_at = len(code)
        _patch_near_jump(code, acknowledged, acknowledged_at)
        code += b"\x49\xc7\x85" + struct.pack(
            "<I", REQUEST_ARGUMENT_SYNC_STATE_OFFSET
        ) + b"\x02\x00\x00\x00"
        done_waiting = len(code)
        _patch_near_jump(code, wait_done, done_waiting)
        _patch_near_jump(code, no_wait, done_waiting)


def build_primary_team_request_stub(
    state: int,
    trampoline: int,
    target: int,
    interval_100ns: int,
    *,
    prologue: bytes,
) -> bytes:
    """Build the stable primary-only request path used by v0.1.6.

    This path deliberately has no outbound recorder, no request round robin,
    and no Lua argument handling.  It only issues the configured primary
    zero-argument team snapshot before continuing the intercepted request.
    """

    code = bytearray()
    code += b"\x9c"  # pushfq
    code += b"\x50\x53\x51\x52\x56\x57"
    code += b"\x41\x50\x41\x51\x41\x52\x41\x53\x41\x54\x41\x55"

    code += b"\x49\xba" + struct.pack("<Q", state)
    code += b"\x49\x83\x7a\x08\x00"
    skip_jumps = [_emit_near_jump(code, b"\x0f\x84")]  # disabled

    # Read a coherent KUSER_SHARED_DATA.SystemTime value into R12.
    code += b"\x41\xbb\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x45\x8b\x63\x18"
    code += b"\x41\x8b\x53\x14"
    code += b"\x45\x3b\x63\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x49\xc1\xe4\x20"
    code += b"\x49\x09\xd4"

    code += b"\x4d\x8b\x6a\x10"  # previous request FILETIME
    code += b"\x4d\x85\xed"
    due_jump = _emit_near_jump(code, b"\x0f\x84")
    code += b"\x4c\x89\xe0"
    code += b"\x4c\x29\xe8"
    code += b"\x48\x3d" + struct.pack("<I", interval_100ns)
    skip_jumps.append(_emit_near_jump(code, b"\x0f\x82"))  # too soon
    due = len(code)
    _patch_near_jump(code, due_jump, due)

    # Claim this interval across all game threads before making the extra call.
    code += b"\x4c\x89\xe8"
    code += b"\xf0\x4d\x0f\xb1\x62\x10"
    skip_jumps.append(_emit_near_jump(code, b"\x0f\x85"))

    # Copy only the confirmed stable RPC context and construct the game's
    # zero-argument representation. Original RDX is at [rsp+0x40].
    code += b"\x48\x89\xcb"  # rbx = original ScriptEntity
    code += b"\x48\x8b\x74\x24\x40"
    code += b"\x48\xbf" + struct.pack("<Q", state + STATE_ARGUMENTS_OFFSET)
    code += b"\xb9" + struct.pack("<I", STATE_ARGUMENTS_SIZE // 8)
    code += b"\xf3\x48\xa5"
    code += b"\x4d\x89\xd5"  # r13 = state (callee-saved)
    code += b"\x48\x83\xec\x40"
    code += b"\x48\xb8" + struct.pack("<Q", state + STATE_NULL_CELL_OFFSET)
    code += b"\x48\x89\x44\x24\x20"
    code += b"\x48\xb8" + struct.pack("<Q", state + STATE_TRAILING_CELL_OFFSET)
    code += b"\x48\x89\x44\x24\x28"
    code += b"\x48\x89\xd9"
    code += b"\x49\xba" + struct.pack("<Q", state + STATE_ARGUMENTS_OFFSET)
    code += b"\x4c\x89\xd2"
    code += b"\x49\xb8" + struct.pack("<Q", state + STATE_METHOD_OFFSET)
    code += b"\x49\xb9" + struct.pack("<Q", state + STATE_VARIADIC_OFFSET)
    code += b"\x48\xb8" + struct.pack("<Q", trampoline)
    code += b"\xff\xd0"
    code += b"\x0f\xb6\xc0"
    code += b"\x49\x89\x45\x20"
    code += b"\x49\xff\x45\x18"
    code += b"\x48\x83\xc4\x40"

    skip = len(code)
    for displacement_at in skip_jumps:
        _patch_near_jump(code, displacement_at, skip)

    code += b"\x41\x5d\x41\x5c\x41\x5b\x41\x5a"
    code += b"\x41\x59\x41\x58\x5f\x5e\x5a\x59\x5b\x58\x9d"
    code += bytes(prologue)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack(
        "<Q", target + len(prologue)
    )
    if len(code) >= TRAMPOLINE_OFFSET:
        raise AssertionError(f"primary team request stub is too large: {len(code)}")
    return bytes(code)


def build_request_stub(
    state: int,
    trampoline: int,
    target: int,
    interval_100ns: int,
    *,
    prologue: bytes,
    request_method_count: int = 1,
    synchronized_methods: tuple[bytes, ...] = (),
) -> bytes:
    if not 1 <= int(request_method_count) <= MAX_REQUEST_METHODS:
        raise ValueError("request method count is unsupported")
    code = bytearray()
    code += b"\x9c"  # pushfq
    code += b"\x50\x53\x51\x52\x56\x57"
    code += b"\x41\x50\x41\x51\x41\x52\x41\x53\x41\x54\x41\x55"

    _emit_request_capture(code, state, synchronized_methods)

    code += b"\x49\xba" + struct.pack("<Q", state)
    code += b"\x49\x83\x7a\x08\x00"
    skip_jumps = [_emit_near_jump(code, b"\x0f\x84")]  # disabled

    # Read a coherent KUSER_SHARED_DATA.SystemTime value into R12.
    code += b"\x41\xbb\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x45\x8b\x63\x18"
    code += b"\x41\x8b\x53\x14"
    code += b"\x45\x3b\x63\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x49\xc1\xe4\x20"
    code += b"\x49\x09\xd4"

    code += b"\x4d\x8b\x6a\x10"  # previous request FILETIME
    code += b"\x4d\x85\xed"
    due_jump = _emit_near_jump(code, b"\x0f\x84")
    code += b"\x4c\x89\xe0"
    code += b"\x4c\x29\xe8"
    code += b"\x48\x3d" + struct.pack("<I", interval_100ns)
    skip_jumps.append(_emit_near_jump(code, b"\x0f\x82"))  # too soon
    due = len(code)
    _patch_near_jump(code, due_jump, due)

    # Claim this interval across all threads before making the extra call.
    code += b"\x4c\x89\xe8"
    code += b"\xf0\x4d\x0f\xb1\x62\x10"
    skip_jumps.append(_emit_near_jump(code, b"\x0f\x85"))

    # Preserve the live ScriptEntity and copy its stable 0x78-byte RPC context.
    # The separate variadic-argument object, stack cells, and method are fixed
    # below to the game's confirmed zero-argument representation.  Reusing an
    # arbitrary request's variadic object leaks that request's argument count
    # (for example 10 for ReqCastSkillNew) and the server silently drops the
    # otherwise valid Common request. Original RDX is at [rsp+0x40].
    code += b"\x48\x8b\x5c\x24\x48"  # rbx = original ScriptEntity
    code += b"\x48\x8b\x74\x24\x40"
    code += b"\x48\xbf" + struct.pack(
        "<Q", state + STATE_ARGUMENTS_OFFSET
    )
    code += b"\xb9" + struct.pack("<I", STATE_ARGUMENTS_SIZE // 8)
    code += b"\xf3\x48\xa5"
    code += b"\x4d\x89\xd5"  # r13 = state (callee-saved)
    code += b"\x48\x83\xec\x40"
    code += b"\x48\xb8" + struct.pack(
        "<Q", state + STATE_NULL_CELL_OFFSET
    )
    code += b"\x48\x89\x44\x24\x20"
    code += b"\x48\xb8" + struct.pack(
        "<Q", state + STATE_TRAILING_CELL_OFFSET
    )
    code += b"\x48\x89\x44\x24\x28"
    # Select one authorized request per dispatch.  The controller chooses a
    # sub-second dispatch interval so every method retains the requested
    # per-method cadence without issuing a burst of calls on one game tick.
    code += b"\x49\x8b\x45" + bytes([STATE_NEXT_METHOD_INDEX_OFFSET])
    code += b"\x49\x89\x45" + bytes([STATE_LAST_METHOD_INDEX_OFFSET])
    code += b"\x48\x69\xc0" + struct.pack("<I", STATE_METHOD_STRIDE)
    code += b"\x49\xb8" + struct.pack("<Q", state + STATE_METHOD_OFFSET)
    code += b"\x49\x01\xc0"  # r8 += method index * stride
    code += b"\x49\xff\x45" + bytes([STATE_NEXT_METHOD_INDEX_OFFSET])
    code += (
        b"\x49\x83\x7d"
        + bytes([STATE_NEXT_METHOD_INDEX_OFFSET, int(request_method_count)])
    )
    keep_index = _emit_near_jump(code, b"\x0f\x82")
    code += (
        b"\x49\xc7\x45"
        + bytes([STATE_NEXT_METHOD_INDEX_OFFSET])
        + b"\x00\x00\x00\x00"
    )
    _patch_near_jump(code, keep_index, len(code))

    # Most authorized statistics requests have no arguments. A controlled
    # numeric research request temporarily borrows the first argument cell of
    # the live Lua call. The nested call serializes the replacement number
    # synchronously, after which the original TValue bits are restored before
    # the intercepted request is allowed to continue.
    code += b"\x49\xc7\x85" + struct.pack(
        "<I", STATE_NUMERIC_SLOT_OFFSET
    ) + b"\x00\x00\x00\x00"
    code += b"\x49\x83\x78" + bytes(
        [STATE_METHOD_ARGUMENT_KIND_OFFSET, REQUEST_ARGUMENT_NONE]
    )
    zero_argument = _emit_near_jump(code, b"\x0f\x84")
    code += b"\x49\x83\x78" + bytes(
        [STATE_METHOD_ARGUMENT_KIND_OFFSET, REQUEST_ARGUMENT_NUMBER]
    )
    cancel_call_jumps = [_emit_near_jump(code, b"\x0f\x82")]
    code += b"\x49\x83\x78" + bytes(
        [STATE_METHOD_ARGUMENT_KIND_OFFSET, REQUEST_ARGUMENT_RAW_LUA_VALUE]
    )
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x87"))

    # RSP moved by 0x40 above, so the saved original R9 is now at +0x60.
    code += b"\x48\x8b\x44\x24\x60"
    code += b"\x48\x85\xc0"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    # Raw string requests are disabled until the controller has identified
    # the game's real string descriptor. The live storage pointer is also
    # required because its stable 0x48-byte header is cloned below.
    code += b"\x49\x83\x78" + bytes(
        [STATE_METHOD_ARGUMENT_KIND_OFFSET, REQUEST_ARGUMENT_RAW_LUA_VALUE]
    )
    non_string_argument = _emit_near_jump(code, b"\x0f\x85")
    code += b"\x49\x83\xbd" + struct.pack(
        "<I", STATE_STRING_DESCRIPTOR_OFFSET
    ) + b"\x00"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    code += b"\x48\x83\x78\x08\x00"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    _patch_near_jump(code, non_string_argument, len(code))
    code += b"\x48\x8b\x08"  # rcx = lua_State
    code += b"\x48\x85\xc9"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    code += b"\x48\x8b\x50\x10"  # packed first/last Lua stack indices
    code += b"\x48\x83\xfa\xff"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    code += b"\x41\x89\xd1"  # r9d = first argument index
    code += b"\x45\x85\xc9"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    code += b"\x41\x81\xf9\x00\x10\x00\x00"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x87"))
    code += b"\x49\x89\xd2\x49\xc1\xea\x20"
    code += b"\x45\x39\xca"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x82"))
    code += b"\x48\x8b\x71\x20"  # lua_State.base
    code += b"\x48\x8b\x79\x28"  # lua_State.top
    code += b"\x48\x85\xf6"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    code += b"\x48\x85\xff"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x84"))
    code += b"\x4a\x8d\x54\xce\xf8"  # base + (first - 1) * 8
    code += b"\x48\x39\xf2"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x82"))
    code += b"\x48\x8d\x42\x08\x48\x39\xf8"
    cancel_call_jumps.append(_emit_near_jump(code, b"\x0f\x87"))

    # Argument-bearing calls also carry four call-site cells after the x64
    # home space. The first real stack argument is at entry RSP+0x28; entry
    # RSP+0x20 is the final home-space cell and must not be shifted into it.
    for source_offset, target_offset in zip(
        (
            REQUEST_ENTRY_STACK_CAPTURE_OFFSET + 0x48,
            REQUEST_ENTRY_STACK_CAPTURE_OFFSET + 0x50,
            REQUEST_ENTRY_STACK_CAPTURE_OFFSET + 0x58,
            REQUEST_ENTRY_STACK_CAPTURE_OFFSET + 0x60,
        ),
        (0x20, 0x28, 0x30, 0x38),
    ):
        code += b"\x48\x8b\x84\x24" + struct.pack("<I", source_offset)
        code += b"\x48\x89\x44\x24" + bytes([target_offset])

    code += b"\x49\x89\x95" + struct.pack(
        "<I", STATE_NUMERIC_SLOT_OFFSET
    )
    code += b"\x48\x8b\x02"
    code += b"\x49\x89\x85" + struct.pack(
        "<I", STATE_NUMERIC_ORIGINAL_CELL_OFFSET
    )

    # Preserve the game-owned VariadicArguments object, changing only its
    # packed argument range to the borrowed single stack cell.
    code += b"\x48\x8b\x74\x24\x60"
    code += b"\x48\xbf" + struct.pack(
        "<Q", state + STATE_NUMERIC_VARIADIC_OFFSET
    )
    code += b"\xb9" + struct.pack("<I", STATE_NUMERIC_VARIADIC_SIZE // 8)
    code += b"\xf3\x48\xa5"
    code += b"\x44\x89\xc8\x44\x89\xca\x48\xc1\xe2\x20\x48\x09\xd0"
    code += b"\x49\xba" + struct.pack(
        "<Q", state + STATE_NUMERIC_VARIADIC_OFFSET
    )
    code += b"\x49\x89\x42\x10"
    code += b"\x49\x8b\x95" + struct.pack(
        "<I", STATE_NUMERIC_SLOT_OFFSET
    )
    code += b"\x49\x8b\x40" + bytes([STATE_METHOD_ARGUMENT_VALUE_OFFSET])
    code += b"\x48\x89\x02"

    # A raw Lua string needs the matching string Argument descriptor. Clone
    # the live storage header into hook-owned memory and replace only its
    # one-element descriptor vector. Numeric research calls retain the
    # original shallow VariadicArguments copy unchanged.
    code += b"\x49\x83\x78" + bytes(
        [STATE_METHOD_ARGUMENT_KIND_OFFSET, REQUEST_ARGUMENT_RAW_LUA_VALUE]
    )
    descriptor_ready = _emit_near_jump(code, b"\x0f\x85")
    code += b"\x49\x8b\xb5" + struct.pack(
        "<I", STATE_NUMERIC_VARIADIC_OFFSET + 0x08
    )
    code += b"\x48\xbf" + struct.pack(
        "<Q", state + STATE_STRING_STORAGE_OFFSET
    )
    code += b"\xb9\x09\x00\x00\x00"
    code += b"\xf3\x48\xa5"
    code += b"\x48\xbf" + struct.pack(
        "<Q", state + STATE_STRING_STORAGE_OFFSET
    )
    code += b"\x48\xb8" + struct.pack(
        "<Q", state + STATE_STRING_VECTOR_OFFSET
    )
    code += b"\x48\x89\x47\x30"
    code += b"\x48\x83\xc0\x08"
    code += b"\x48\x89\x47\x38"
    code += b"\x48\x89\x47\x40"
    code += b"\x49\x8b\x95" + struct.pack(
        "<I", STATE_STRING_DESCRIPTOR_OFFSET
    )
    code += b"\x48\xb8" + struct.pack(
        "<Q", state + STATE_STRING_VECTOR_OFFSET
    )
    code += b"\x48\x89\x10"
    code += b"\x48\xb8" + struct.pack(
        "<Q", state + STATE_STRING_STORAGE_OFFSET
    )
    code += b"\x49\x89\x85" + struct.pack(
        "<I", STATE_NUMERIC_VARIADIC_OFFSET + 0x08
    )
    _patch_near_jump(code, descriptor_ready, len(code))
    code += b"\x49\xb9" + struct.pack(
        "<Q", state + STATE_NUMERIC_VARIADIC_OFFSET
    )
    call_ready = _emit_near_jump(code, b"\xe9")

    zero_argument_at = len(code)
    _patch_near_jump(code, zero_argument, zero_argument_at)
    code += b"\x49\xb9" + struct.pack("<Q", state + STATE_VARIADIC_OFFSET)
    call_ready_at = len(code)
    _patch_near_jump(code, call_ready, call_ready_at)

    code += b"\x48\x89\xd9"
    code += b"\x49\xba" + struct.pack("<Q", state + STATE_ARGUMENTS_OFFSET)
    code += b"\x4c\x89\xd2"
    code += b"\x48\xb8" + struct.pack("<Q", trampoline)
    code += b"\xff\xd0"
    code += b"\x0f\xb6\xc0"
    code += b"\x49\x89\x45\x20"
    code += b"\x49\xff\x45\x18"

    code += b"\x49\x8b\x95" + struct.pack("<I", STATE_NUMERIC_SLOT_OFFSET)
    code += b"\x48\x85\xd2"
    restored = _emit_near_jump(code, b"\x0f\x84")
    code += b"\x49\x8b\x85" + struct.pack(
        "<I", STATE_NUMERIC_ORIGINAL_CELL_OFFSET
    )
    code += b"\x48\x89\x02"
    _patch_near_jump(code, restored, len(code))

    cancel_call_at = len(code)
    for displacement_at in cancel_call_jumps:
        _patch_near_jump(code, displacement_at, cancel_call_at)
    code += b"\x48\x83\xc4\x40"

    skip = len(code)
    for displacement_at in skip_jumps:
        _patch_near_jump(code, displacement_at, skip)

    code += b"\x41\x5d\x41\x5c\x41\x5b\x41\x5a"
    code += b"\x41\x59\x41\x58\x5f\x5e\x5a\x59\x5b\x58\x9d"
    code += bytes(prologue)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack(
        "<Q", target + len(prologue)
    )
    if len(code) >= TRAMPOLINE_OFFSET:
        raise AssertionError(f"request stub is too large: {len(code)}")
    return bytes(code)


def build_trampoline(target: int, *, prologue: bytes) -> bytes:
    return (
        bytes(prologue)
        + b"\xff\x25\x00\x00\x00\x00"
        + struct.pack("<Q", target + len(prologue))
    )


def parse_request_record(data: bytes, expected_sequence: int) -> dict | None:
    """Parse one committed raw outbound call without interpreting arguments."""

    if len(data) != REQUEST_RECORD_SIZE:
        return None
    (
        sequence,
        filetime_100ns,
        script_entity,
        context,
        method_object,
        variadic_arguments,
        return_address,
        method_length,
    ) = struct.unpack_from("<8Q", data)
    commit = struct.unpack_from("<Q", data, REQUEST_COMMIT_OFFSET)[0]
    if sequence != expected_sequence or commit != expected_sequence + 1:
        return None
    raw_method = data[0x40:REQUEST_CONTEXT_SNAPSHOT_OFFSET].split(b"\x00", 1)[0]
    method = raw_method.decode("utf-8", "replace")
    try:
        event_time = dt.datetime.fromtimestamp(
            (filetime_100ns - 116_444_736_000_000_000) / 10_000_000,
            tz=dt.timezone.utc,
        ).astimezone().isoformat(timespec="microseconds")
    except (OverflowError, OSError, ValueError):
        event_time = ""
    stack_cells = list(
        struct.unpack_from(
            f"<{REQUEST_STACK_CELL_COUNT}Q", data, REQUEST_STACK_CELLS_OFFSET
        )
    )
    argument_sync_state = struct.unpack_from(
        "<Q", data, REQUEST_ARGUMENT_SYNC_STATE_OFFSET
    )[0]
    variadic_snapshot = data[
        REQUEST_VARIADIC_SNAPSHOT_OFFSET : REQUEST_VARIADIC_SNAPSHOT_OFFSET
        + REQUEST_VARIADIC_SNAPSHOT_SIZE
    ]
    variadic_qwords = list(struct.unpack("<8Q", variadic_snapshot))
    raw_shape = variadic_qwords[2]
    argument_start = None
    argument_end = None
    argument_count = None
    if raw_shape == 0xFFFF_FFFF_FFFF_FFFF:
        argument_start = -1
        argument_end = -1
        argument_count = 0
    else:
        argument_start, argument_end = struct.unpack(
            "<2i", struct.pack("<Q", raw_shape)
        )
        candidate_count = argument_end - argument_start + 1
        if (
            argument_start >= 1
            and argument_end >= argument_start
            and candidate_count <= 0x1000
        ):
            argument_count = candidate_count
    return {
        "event_time": event_time,
        "filetime_100ns": filetime_100ns,
        "sequence": sequence,
        "function": "doraemon::script::ScriptEntity::call_server",
        "direction": "outbound",
        "capture_source": "raw_outbound_rpc",
        "script_entity": script_entity,
        "context": context,
        "method_object": method_object,
        "variadic_arguments": variadic_arguments,
        "return_address": f"0x{return_address:016x}",
        "method_length": method_length,
        "method": method,
        "context_snapshot": data[
            REQUEST_CONTEXT_SNAPSHOT_OFFSET : REQUEST_CONTEXT_SNAPSHOT_OFFSET
            + REQUEST_CONTEXT_SNAPSHOT_SIZE
        ].hex(),
        "variadic_snapshot": variadic_snapshot.hex(),
        "variadic_qwords": variadic_qwords,
        "lua_state_address": variadic_qwords[0],
        "variadic_storage_address": variadic_qwords[1],
        "argument_descriptor_address": variadic_qwords[1],
        "variadic_argument_start": argument_start,
        "variadic_argument_end": argument_end,
        "variadic_argument_count": argument_count,
        "argument_sync_state": argument_sync_state,
        "entry_stack_offsets": ["0x20", "0x28", "0x30", "0x38"],
        "entry_stack_cells": stack_cells,
    }


def capture_lua_argument_cells(process: int, record: dict) -> None:
    """Capture the exact Lua stack cells while a synchronized call is paused."""

    try:
        lua_state = int(record.get("lua_state_address", 0) or 0)
        argument_start = int(record.get("variadic_argument_start", 0) or 0)
        argument_count = int(record.get("variadic_argument_count", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return
    if not (
        0x1_0000 <= lua_state <= 0x0000_7FFF_FFFF_FFFF
        and argument_start >= 1
        and 1 <= argument_count <= MAX_CAPTURED_LUA_ARGUMENTS
    ):
        return

    state = read_region(process, lua_state, LUA_STATE_SNAPSHOT_SIZE)
    if not state or len(state) != LUA_STATE_SNAPSHOT_SIZE:
        return
    stack_base = struct.unpack_from("<Q", state, LUA_STATE_STACK_BASE_OFFSET)[0]
    stack_top = struct.unpack_from("<Q", state, LUA_STATE_STACK_TOP_OFFSET)[0]
    slot_address = stack_base + (argument_start - 1) * 8
    byte_count = argument_count * 8
    if not (
        0x1_0000 <= stack_base <= 0x0000_7FFF_FFFF_FFFF
        and stack_base <= stack_top <= 0x0000_7FFF_FFFF_FFFF
        and slot_address >= stack_base
        and slot_address + byte_count <= stack_top
    ):
        return
    cells = read_region(process, slot_address, byte_count)
    if not cells or len(cells) != byte_count:
        return

    raw_cells = list(struct.unpack(f"<{argument_count}Q", cells))
    numeric_values: list[int | float | None] = []
    for raw_cell in raw_cells:
        signed_tag = struct.unpack("<q", struct.pack("<Q", raw_cell))[0] >> 47
        if -14 <= signed_tag <= -1:
            numeric_values.append(None)
            continue
        value = struct.unpack("<d", struct.pack("<Q", raw_cell))[0]
        if not (value == value and abs(value) != float("inf")):
            numeric_values.append(None)
        elif value.is_integer() and abs(value) <= 9_007_199_254_740_992:
            numeric_values.append(int(value))
        else:
            numeric_values.append(value)

    record["lua_state_snapshot"] = state.hex()
    record["lua_stack_base"] = stack_base
    record["lua_stack_top"] = stack_top
    record["lua_argument_slot_address"] = slot_address
    record["lua_argument_indices"] = list(
        range(argument_start, argument_start + argument_count)
    )
    record["lua_argument_cells"] = raw_cells
    record["lua_argument_numbers"] = numeric_values
    record["lua_argument_snapshot"] = cells.hex()
    record["lua_argument_capture"] = "hook_entry_synchronized"


def _valid_user_pointer(value: object) -> bool:
    try:
        address = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False
    return 0x1_0000 <= address <= 0x0000_7FFF_FFFF_FFFF


def argument_descriptor_type_name(snapshot: bytes) -> str:
    """Read the observed inline type name from an argument descriptor."""

    if len(snapshot) < 0x30:
        return ""
    raw_name = snapshot[0x10:0x30].split(b"\0", 1)[0]
    try:
        name = raw_name.decode("ascii")
    except UnicodeDecodeError:
        return ""
    return name if name.isidentifier() else ""


def capture_lua_argument_descriptors(process: int, record: dict) -> None:
    """Snapshot a paused call's descriptor vector before acknowledging it."""

    try:
        storage_address = int(record.get("variadic_storage_address", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return
    if not _valid_user_pointer(storage_address):
        return
    storage = read_region(process, storage_address, STATE_STRING_STORAGE_SIZE)
    if storage is None or len(storage) != STATE_STRING_STORAGE_SIZE:
        return
    descriptor_start, descriptor_end = struct.unpack_from("<QQ", storage, 0x30)
    if (
        not _valid_user_pointer(descriptor_start)
        or descriptor_end < descriptor_start
        or descriptor_end > 0x0000_7FFF_FFFF_FFFF
        or (descriptor_end - descriptor_start) % 8
    ):
        return
    descriptor_count = (descriptor_end - descriptor_start) // 8
    if not 1 <= descriptor_count <= MAX_ARGUMENT_DESCRIPTOR_COUNT:
        return
    raw_pointers = read_region(process, descriptor_start, descriptor_count * 8)
    if raw_pointers is None or len(raw_pointers) != descriptor_count * 8:
        return

    items: list[dict[str, object]] = []
    for (address,) in struct.iter_unpack("<Q", raw_pointers):
        item: dict[str, object] = {"address": address}
        if _valid_user_pointer(address):
            snapshot = read_region(
                process, address, ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE
            )
            if snapshot is not None and len(snapshot) == ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE:
                item["snapshot"] = snapshot.hex()
                item["type_name"] = argument_descriptor_type_name(snapshot)
                item["lua_type"] = snapshot[0x50]
        items.append(item)
    record["argument_descriptor_vector"] = {
        "start": descriptor_start,
        "end": descriptor_end,
        "items": items,
        "capture": "hook_entry_synchronized",
    }


def is_lua_string_argument_descriptor(process: int, address: int) -> bool:
    """Validate both the descriptor name and its observed Lua type code."""

    if not _valid_user_pointer(address):
        return False
    snapshot = read_region(process, int(address), ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE)
    return bool(
        snapshot is not None
        and len(snapshot) == ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE
        and argument_descriptor_type_name(snapshot) == "string"
        and snapshot[0x50] == 3
    )


def find_lua_string_argument_descriptor(
    process: int,
    descriptor_addresses: list[int] | tuple[int, ...],
) -> int:
    """Find the registered string descriptor near a captured descriptor."""

    checked: set[int] = set()
    offsets = [0]
    for distance in range(0x10, STRING_DESCRIPTOR_SCAN_RADIUS + 0x10, 0x10):
        offsets.extend((-distance, distance))
    for raw_address in descriptor_addresses:
        try:
            base_address = int(raw_address)
        except (TypeError, ValueError, OverflowError):
            continue
        for offset in offsets:
            candidate = base_address + offset
            if candidate in checked or not _valid_user_pointer(candidate):
                continue
            checked.add(candidate)
            if is_lua_string_argument_descriptor(process, candidate):
                return candidate
    return 0


def _rotate_left_32(value: int, count: int) -> int:
    value &= 0xFFFF_FFFF
    return ((value << count) | (value >> (32 - count))) & 0xFFFF_FFFF


def lua_sparse_string_hash(seed: int, value: bytes) -> int:
    """Return LuaJIT 2.1's primary keyed sparse string hash."""

    if not value:
        raise ValueError("Lua string lookup requires a non-empty value")

    def u32(offset: int) -> int:
        return struct.unpack_from("<I", value, offset)[0]

    length = len(value)
    hash_value = (length ^ int(seed)) & 0xFFFF_FFFF
    if length >= 4:
        accumulator_a = u32(0)
        hash_value ^= u32(length - 4)
        accumulator_b = u32((length >> 1) - 2)
        hash_value ^= accumulator_b
        hash_value = (hash_value - _rotate_left_32(accumulator_b, 14)) & 0xFFFF_FFFF
        accumulator_b = (accumulator_b + u32((length >> 2) - 1)) & 0xFFFF_FFFF
    else:
        accumulator_a = value[0]
        hash_value ^= value[length - 1]
        accumulator_b = value[length >> 1]
        hash_value ^= accumulator_b
        hash_value = (hash_value - _rotate_left_32(accumulator_b, 14)) & 0xFFFF_FFFF
    accumulator_a ^= hash_value
    accumulator_a = (accumulator_a - _rotate_left_32(hash_value, 11)) & 0xFFFF_FFFF
    accumulator_b ^= accumulator_a
    accumulator_b = (accumulator_b - _rotate_left_32(accumulator_a, 25)) & 0xFFFF_FFFF
    hash_value ^= accumulator_b
    return (hash_value - _rotate_left_32(accumulator_b, 16)) & 0xFFFF_FFFF


def lua_dense_string_hash(seed: int, sparse_hash: int, value: bytes) -> int:
    """Return LuaJIT 2.1's secondary keyed dense string hash."""

    def u32(offset: int) -> int:
        return struct.unpack_from("<I", value, offset)[0]

    def byte_swap_32(number: int) -> int:
        return int.from_bytes(int(number & 0xFFFF_FFFF).to_bytes(4, "little"), "big")

    hash_value = int(sparse_hash) & 0xFFFF_FFFF
    accumulator_b = byte_swap_32(_rotate_left_32(hash_value ^ (int(seed) >> 32), 4))
    if len(value) > 12:
        accumulator_a = int(seed) & 0xFFFF_FFFF
        end = len(value) - 12
        pointer = end
        front = 0
        while True:
            accumulator_a = (accumulator_a + u32(pointer)) & 0xFFFF_FFFF
            accumulator_b = (accumulator_b + u32(pointer + 4)) & 0xFFFF_FFFF
            hash_value = (hash_value + u32(pointer + 8)) & 0xFFFF_FFFF
            pointer = front
            front += 12
            hash_value ^= accumulator_b
            hash_value = (hash_value - _rotate_left_32(accumulator_b, 14)) & 0xFFFF_FFFF
            accumulator_a ^= hash_value
            accumulator_a = (
                accumulator_a - _rotate_left_32(hash_value, 11)
            ) & 0xFFFF_FFFF
            accumulator_b ^= accumulator_a
            accumulator_b = (
                accumulator_b - _rotate_left_32(accumulator_a, 25)
            ) & 0xFFFF_FFFF
            if pointer >= end:
                break
        hash_value ^= accumulator_b
        hash_value = (hash_value - _rotate_left_32(accumulator_b, 16)) & 0xFFFF_FFFF
        accumulator_a ^= hash_value
        accumulator_a = (accumulator_a - _rotate_left_32(hash_value, 4)) & 0xFFFF_FFFF
        accumulator_b ^= accumulator_a
        accumulator_b = (
            accumulator_b - _rotate_left_32(accumulator_a, 14)
        ) & 0xFFFF_FFFF
    return accumulator_b


def resolve_lua_string_tvalue(
    process: int,
    lua_state: int,
    value: str,
) -> int:
    """Resolve an already-interned string to its tagged GC64 TValue."""

    encoded = str(value).encode("utf-8")
    if not encoded or len(encoded) > 0x1000:
        raise ValueError("Lua string lookup value has an invalid length")
    state = read_region(process, int(lua_state), 0x18)
    if not state or len(state) != 0x18 or state[9] != 6:
        raise RuntimeError("captured Lua state is no longer readable")
    global_state = (
        struct.unpack_from("<Q", state, LUA_STATE_GLOBAL_STATE_OFFSET)[0]
        & LUA_GC64_POINTER_MASK
    )
    string_state = read_region(
        process,
        global_state + LUA_GLOBAL_STRING_TABLE_OFFSET,
        LUA_STRING_TABLE_SEED_OFFSET + 8,
    )
    if not string_state or len(string_state) != LUA_STRING_TABLE_SEED_OFFSET + 8:
        raise RuntimeError("Lua string table is unreadable")
    buckets, mask, _count = struct.unpack_from("<QII", string_state)
    seed = struct.unpack_from("<Q", string_state, LUA_STRING_TABLE_SEED_OFFSET)[0]
    buckets &= LUA_GC64_POINTER_MASK
    if not buckets or mask > 0x00FF_FFFF:
        raise RuntimeError("Lua string table metadata is invalid")

    sparse_hash = lua_sparse_string_hash(seed, encoded)
    bucket_index = sparse_hash & mask
    raw_head = read_region(process, buckets + bucket_index * 8, 8)
    if not raw_head or len(raw_head) != 8:
        raise RuntimeError("Lua string bucket is unreadable")
    raw_pointer = struct.unpack("<Q", raw_head)[0]
    expected_hash = sparse_hash
    if raw_pointer & 1:
        expected_hash = lua_dense_string_hash(seed, sparse_hash, encoded)
        bucket_index = expected_hash & mask
        raw_head = read_region(process, buckets + bucket_index * 8, 8)
        if not raw_head or len(raw_head) != 8:
            raise RuntimeError("secondary Lua string bucket is unreadable")
        raw_pointer = struct.unpack("<Q", raw_head)[0]

    address = raw_pointer & LUA_GC64_POINTER_MASK & ~1
    seen: set[int] = set()
    while address and address not in seen and len(seen) < LUA_STRING_CHAIN_LIMIT:
        seen.add(address)
        header = read_region(process, address, LUA_GC_STRING_HEADER_SIZE)
        if not header or len(header) != LUA_GC_STRING_HEADER_SIZE:
            break
        next_address = struct.unpack_from("<Q", header)[0] & LUA_GC64_POINTER_MASK & ~1
        string_hash = struct.unpack_from("<I", header, 0x10)[0]
        string_length = struct.unpack_from("<I", header, 0x14)[0]
        if (
            header[9] == 4
            and string_hash == expected_hash
            and string_length == len(encoded)
            and read_region(process, address + LUA_GC_STRING_HEADER_SIZE, string_length)
            == encoded
        ):
            return LUA_GC64_STRING_TAG | address
        address = next_address
    raise LookupError(f"Lua string is not currently interned: {value!r}")


class TeamStatsRequestHook:
    def __init__(
        self,
        *,
        profile: Mapping[str, object],
        pid: int | None = None,
        interval: float = 1.0,
        enabled: bool = True,
        additional_request_methods: tuple[str, ...] = (),
        numeric_request_arguments: Mapping[str, int] | None = None,
        raw_lua_request_arguments: Mapping[str, int] | None = None,
        synchronized_methods: tuple[str, ...] = (),
        takeover_existing: bool = False,
        stable_primary_only: bool = False,
    ):
        self.profile = normalize_runtime_profile(profile)
        hook = runtime_profile_hook(self.profile, "team_stats")
        protocol = self.profile["protocol"]
        if not isinstance(protocol, dict):
            raise ValueError("runtime protocol profile is invalid")
        self.rva = int(hook["rva"])
        self.prologue = bytes(hook["prologue"])
        self.signature = bytes(hook["signature"])
        primary_request_method = str(protocol["team_stats_request_method"])
        try:
            self.request_methods = tuple(
                dict.fromkeys(
                    method.strip().encode("ascii")
                    for method in (
                        primary_request_method,
                        *(str(value) for value in additional_request_methods),
                    )
                    if method.strip()
                )
            )
        except UnicodeEncodeError as exc:
            raise ValueError("team-stat request method must be ASCII") from exc
        if (
            not self.request_methods
            or len(self.request_methods) > MAX_REQUEST_METHODS
            or any(len(method) > 0x7F for method in self.request_methods)
        ):
            raise ValueError("team-stat request method set is invalid")
        # Retain the singular attribute for callers and diagnostics written by
        # earlier builds.  It is always the primary Common snapshot request.
        self.request_method = self.request_methods[0]
        raw_numeric_arguments = dict(numeric_request_arguments or {})
        raw_lua_arguments = dict(raw_lua_request_arguments or {})
        try:
            encoded_numeric_arguments = {
                str(method).strip().encode("ascii"): value
                for method, value in raw_numeric_arguments.items()
            }
            encoded_lua_arguments = {
                str(method).strip().encode("ascii"): value
                for method, value in raw_lua_arguments.items()
            }
        except UnicodeEncodeError as exc:
            raise ValueError("argument-bearing request method must be ASCII") from exc
        duplicate_argument_methods = set(encoded_numeric_arguments).intersection(
            encoded_lua_arguments
        )
        if duplicate_argument_methods:
            duplicate = sorted(
                method.decode("ascii", "replace")
                for method in duplicate_argument_methods
            )
            raise ValueError(
                f"request methods have multiple argument kinds: {duplicate}"
            )
        unknown_argument_methods = (
            set(encoded_numeric_arguments) | set(encoded_lua_arguments)
        ).difference(self.request_methods)
        if unknown_argument_methods:
            unknown = sorted(
                method.decode("ascii", "replace") for method in unknown_argument_methods
            )
            raise ValueError(
                "request arguments are not present in the request "
                f"round robin: {unknown}"
            )
        self.request_argument_kinds: tuple[int, ...] = tuple(
            (
                REQUEST_ARGUMENT_NUMBER
                if method in encoded_numeric_arguments
                else (
                    REQUEST_ARGUMENT_RAW_LUA_VALUE
                    if method in encoded_lua_arguments
                    else REQUEST_ARGUMENT_NONE
                )
            )
            for method in self.request_methods
        )
        self.request_argument_bits: tuple[int | None, ...] = tuple(
            (
                self._numeric_argument_bits(encoded_numeric_arguments.get(method))
                if method in encoded_numeric_arguments
                else (
                    self._raw_lua_argument_bits(encoded_lua_arguments.get(method))
                    if method in encoded_lua_arguments
                    else None
                )
            )
            for method in self.request_methods
        )
        self.synchronized_methods = tuple(
            dict.fromkeys(
                str(method).strip().encode("ascii")
                for method in synchronized_methods
                if str(method).strip()
            )
        )
        if any(
            not method or len(method) > 0x7F for method in self.synchronized_methods
        ):
            raise ValueError("synchronized request method is invalid")
        self.stable_primary_only = bool(stable_primary_only)
        if self.stable_primary_only and (
            len(self.request_methods) != 1
            or self.request_argument_kinds != (REQUEST_ARGUMENT_NONE,)
            or self.synchronized_methods
        ):
            raise ValueError(
                "stable primary-only mode accepts exactly one zero-argument "
                "request and no synchronized outbound methods"
            )
        self.requested_pid = pid
        self.request_interval = max(0.5, float(interval))
        self.interval = max(0.25, self.request_interval / len(self.request_methods))
        self.initially_enabled = bool(enabled)
        self.takeover_existing = bool(takeover_existing)
        self.pid = 0
        self.process = 0
        self.base = 0
        self.target = 0
        self.state = 0
        self.code = 0
        self.installed = False
        self.adopted = False
        self.request_next_sequence = 0
        self.request_dropped_count = 0
        self.lua_state_address = 0
        self.string_descriptor_address = 0
        self.active_lua_string_requests: dict[str, str] = {}

    @staticmethod
    def _numeric_argument_bits(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("numeric request argument must be an integer")
        if not 0 < value <= 9_007_199_254_740_992:
            raise ValueError("numeric request argument is outside exact Lua range")
        return struct.unpack("<Q", struct.pack("<d", float(value)))[0]

    @staticmethod
    def _raw_lua_argument_bits(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("raw Lua request argument must be an integer cell")
        if not 0 <= value <= 0xFFFF_FFFF_FFFF_FFFF:
            raise ValueError("raw Lua request argument must fit one TValue cell")
        pointer = value & LUA_GC64_POINTER_MASK
        tag = value & ~LUA_GC64_POINTER_MASK
        if pointer < 0x1_0000 or tag != LUA_GC64_STRING_TAG:
            raise ValueError("raw Lua request argument must be a tagged GC64 string")
        return value

    @property
    def alive(self) -> bool:
        return bool(self.process and process_alive(self.process))

    def _build_installed_stub(self, state: int, code: int) -> bytes:
        trampoline = code + TRAMPOLINE_OFFSET
        if self.stable_primary_only:
            return build_primary_team_request_stub(
                state,
                trampoline,
                self.target,
                int(self.interval * 10_000_000),
                prologue=self.prologue,
            )
        return build_request_stub(
            state,
            trampoline,
            self.target,
            int(self.interval * 10_000_000),
            prologue=self.prologue,
            request_method_count=len(self.request_methods),
            synchronized_methods=self.synchronized_methods,
        )

    def _adopt_existing_primary(self, patch: bytes) -> bool:
        """Adopt an exact stable primary-only hook left by this build."""

        if (
            len(patch) != len(self.prologue)
            or patch[:6] != b"\xff\x25\0\0\0\0"
        ):
            return False
        code = struct.unpack_from("<Q", patch, 6)[0]
        prefix = (
            b"\x9c\x50\x53\x51\x52\x56\x57"
            b"\x41\x50\x41\x51\x41\x52\x41\x53\x41\x54\x41\x55"
            b"\x49\xba"
        )
        stub_head = read_region(self.process, code, len(prefix) + 8)
        if not stub_head or not stub_head.startswith(prefix):
            return False
        state = struct.unpack_from("<Q", stub_head, len(prefix))[0]
        state_head = read_region(self.process, state, 0x40)
        method_head = read_region(
            self.process, state + STATE_METHOD_OFFSET, 0x30
        )
        if (
            not state_head
            or state_head[:8] != STATE_MAGIC
            or struct.unpack_from(
                "<Q", state_head, STATE_METHOD_COUNT_OFFSET
            )[0]
            != 1
            or not method_head
            or len(method_head) != 0x30
        ):
            return False
        (
            method_pointer,
            method_size,
            method_length,
            method_capacity,
            argument_kind,
            argument_value,
        ) = struct.unpack("<QQQQQQ", method_head)
        request_method = self.request_methods[0]
        if (
            method_pointer != state + STATE_METHOD_TEXT_OFFSET
            or method_size != 0
            or method_length != len(request_method)
            or method_capacity != 0x2F
            or argument_kind != REQUEST_ARGUMENT_NONE
            or argument_value != 0
            or read_region(self.process, method_pointer, len(request_method))
            != request_method
            or read_region(
                self.process,
                code,
                len(self._build_installed_stub(state, code)),
            )
            != self._build_installed_stub(state, code)
        ):
            return False
        expected_trampoline = build_trampoline(
            self.target, prologue=self.prologue
        )
        if (
            read_region(
                self.process,
                code + TRAMPOLINE_OFFSET,
                len(expected_trampoline),
            )
            != expected_trampoline
        ):
            return False
        self.state = state
        self.code = code
        self.request_next_sequence = 0
        self.request_dropped_count = 0
        self.installed = True
        self.adopted = True
        self.set_enabled(self.initially_enabled)
        return True

    def _adopt_existing(self, patch: bytes) -> bool:
        """Adopt only an exact Dps-Logs team hook for this runtime profile."""

        if self.stable_primary_only:
            return self._adopt_existing_primary(patch)

        if (
            len(patch) != len(self.prologue)
            or patch[:6] != b"\xff\x25\0\0\0\0"
        ):
            return False
        code = struct.unpack_from("<Q", patch, 6)[0]
        prefix = (
            b"\x9c\x50\x53\x51\x52\x56\x57"
            b"\x41\x50\x41\x51\x41\x52\x41\x53\x41\x54\x41\x55"
            b"\xfc\x49\xba"
        )
        stub_head = read_region(self.process, code, len(prefix) + 8)
        if not stub_head or not stub_head.startswith(prefix):
            return False
        ring = struct.unpack_from("<Q", stub_head, len(prefix))[0]
        if ring < REQUEST_RING_OFFSET:
            return False
        state = ring - REQUEST_RING_OFFSET
        state_head = read_region(self.process, state, 0x28)
        ring_head = read_region(self.process, ring, 0x28)
        if (
            not state_head
            or state_head[:8] != STATE_MAGIC
            or not ring_head
            or ring_head[:8] != REQUEST_RING_MAGIC
        ):
            return False
        _, write_index, capacity, record_size, target = struct.unpack(
            "<8sQQQQ", ring_head
        )
        if (
            capacity != REQUEST_RECORD_COUNT
            or record_size != REQUEST_RECORD_SIZE
            or target != self.target
        ):
            return False
        state_method_count = read_region(
            self.process, state + STATE_METHOD_COUNT_OFFSET, 0x08
        )
        if (
            not state_method_count
            or struct.unpack("<Q", state_method_count)[0]
            != len(self.request_methods)
        ):
            return False
        adopted_argument_bits = list(self.request_argument_bits)
        for index, request_method in enumerate(self.request_methods):
            method_offset = STATE_METHOD_OFFSET + index * STATE_METHOD_STRIDE
            method_text_offset = (
                STATE_METHOD_TEXT_OFFSET + index * STATE_METHOD_STRIDE
            )
            method_head = read_region(
                self.process, state + method_offset, 0x30
            )
            if not method_head or len(method_head) != 0x30:
                return False
            (
                method_pointer,
                method_size,
                method_length,
                method_capacity,
                argument_kind,
                argument_value,
            ) = struct.unpack("<QQQQQQ", method_head)
            expected_argument = self.request_argument_bits[index]
            argument_matches = argument_value == int(expected_argument or 0)
            if (
                argument_kind == REQUEST_ARGUMENT_RAW_LUA_VALUE
                and self.request_argument_kinds[index]
                == REQUEST_ARGUMENT_RAW_LUA_VALUE
            ):
                try:
                    adopted_argument_bits[index] = self._raw_lua_argument_bits(
                        argument_value
                    )
                    argument_matches = True
                except ValueError:
                    argument_matches = False
            if (
                method_pointer != state + method_text_offset
                or method_size != 0
                or method_length != len(request_method)
                or method_capacity != 0x7F
                or argument_kind != self.request_argument_kinds[index]
                or not argument_matches
                or read_region(
                    self.process, method_pointer, len(request_method)
                )
                != request_method
            ):
                return False
        expected_stub = self._build_installed_stub(state, code)
        expected_trampoline = build_trampoline(
            self.target, prologue=self.prologue
        )
        if (
            read_region(self.process, code, len(expected_stub)) != expected_stub
            or read_region(
                self.process,
                code + TRAMPOLINE_OFFSET,
                len(expected_trampoline),
            )
            != expected_trampoline
        ):
            return False
        self.state = state
        self.code = code
        self.request_argument_bits = tuple(adopted_argument_bits)
        self.request_next_sequence = write_index
        self.request_dropped_count = 0
        self.installed = True
        self.adopted = True
        if REQUEST_ARGUMENT_RAW_LUA_VALUE in self.request_argument_kinds:
            self.suspend_lua_string_requests()
        self.set_enabled(self.initially_enabled)
        return True

    def install(self) -> "TeamStatsRequestHook":
        if self.installed:
            return self
        self.pid = self.requested_pid or find_pid("C7-Win64-Shipping.exe")
        self.base, module_size, _path = find_module(
            self.pid, "C7-Win64-Shipping.exe"
        )
        if self.rva + len(self.signature) > module_size:
            raise RuntimeError("team-stat request target is outside the module")
        access = (
            PROCESS_QUERY_INFORMATION
            | PROCESS_VM_OPERATION
            | PROCESS_VM_READ
            | PROCESS_VM_WRITE
        )
        self.process = int(kernel32.OpenProcess(access, False, self.pid) or 0)
        if not self.process:
            raise winerror("OpenProcess(team-stat request)")
        self.target = self.base + self.rva
        actual = read_region(self.process, self.target, len(self.signature))
        if actual != self.signature:
            if actual and self._adopt_existing(
                actual[: len(self.prologue)]
            ):
                return self
            self.close()
            raise RuntimeError(
                "team-stat request signature does not match the authorized "
                f"runtime profile {self.profile['profile_id']}"
            )
        try:
            state_size = (
                PRIMARY_TEAM_STATE_SIZE
                if self.stable_primary_only
                else STATE_SIZE
            )
            self.state = int(
                kernel32.VirtualAllocEx(
                    self.process,
                    None,
                    state_size,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_READWRITE,
                )
                or 0
            )
            self.code = int(
                kernel32.VirtualAllocEx(
                    self.process,
                    None,
                    CODE_SIZE,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_EXECUTE_READWRITE,
                )
                or 0
            )
            if not self.state or not self.code:
                raise winerror("VirtualAllocEx(team-stat request)")

            state_data = bytearray(state_size)
            struct.pack_into(
                "<8sQ",
                state_data,
                0,
                STATE_MAGIC,
                int(self.initially_enabled),
            )
            struct.pack_into(
                "<Q",
                state_data,
                STATE_LAST_REQUEST_OFFSET,
                time.time_ns() // 100 + 116_444_736_000_000_000,
            )
            struct.pack_into(
                "<Q",
                state_data,
                STATE_METHOD_COUNT_OFFSET,
                len(self.request_methods),
            )
            for index, request_method in enumerate(self.request_methods):
                method_offset = STATE_METHOD_OFFSET + index * STATE_METHOD_STRIDE
                method_text_offset = (
                    STATE_METHOD_TEXT_OFFSET + index * STATE_METHOD_STRIDE
                )
                method_text = self.state + method_text_offset
                struct.pack_into(
                    "<QQQQQQ",
                    state_data,
                    method_offset,
                    method_text,
                    0,
                    len(request_method),
                    0x2F if self.stable_primary_only else 0x7F,
                    self.request_argument_kinds[index],
                    int(self.request_argument_bits[index] or 0),
                )
                state_data[
                    method_text_offset : method_text_offset + len(request_method)
                ] = request_method
            struct.pack_into(
                "<QQQQ",
                state_data,
                STATE_VARIADIC_OFFSET,
                0,
                self.state + STATE_EMPTY_DESCRIPTOR_OFFSET,
                0xFFFF_FFFF_FFFF_FFFF,
                0,
            )
            struct.pack_into("<Q", state_data, STATE_TRAILING_CELL_OFFSET, 1)
            if not self.stable_primary_only:
                struct.pack_into(
                    "<8sQQQQ",
                    state_data,
                    REQUEST_RING_OFFSET,
                    REQUEST_RING_MAGIC,
                    0,
                    REQUEST_RECORD_COUNT,
                    REQUEST_RECORD_SIZE,
                    self.target,
                )
            write_memory(self.process, self.state, bytes(state_data))

            trampoline = self.code + TRAMPOLINE_OFFSET
            stub = self._build_installed_stub(self.state, self.code)
            trampoline_code = build_trampoline(self.target, prologue=self.prologue)
            write_memory(self.process, self.code, stub)
            write_memory(self.process, trampoline, trampoline_code)
            kernel32.FlushInstructionCache(
                self.process, ctypes.c_void_p(self.code), CODE_SIZE
            )
            patch = build_absolute_patch(self.code, len(self.prologue))
            suspended = suspend_game_threads(self.pid)
            try:
                write_code(self.process, self.target, patch)
                self.installed = True
            finally:
                resume_threads(suspended)
            if read_region(self.process, self.target, len(patch)) != patch:
                raise RuntimeError("team-stat request hook verification failed")
            self.request_next_sequence = 0
            self.request_dropped_count = 0
            return self
        except Exception:
            self.close()
            raise

    def set_enabled(self, enabled: bool) -> None:
        if not self.installed or not self.alive:
            return
        write_memory(
            self.process,
            self.state + STATE_ENABLED_OFFSET,
            struct.pack("<Q", int(bool(enabled))),
        )

    def rearm_request_schedule(
        self,
        *,
        filetime_100ns: int | None = None,
        settle_seconds: float = 0.35,
    ) -> bool:
        """Shift the existing request cadence without issuing an extra RPC."""

        if not self.installed or not self.alive:
            return False
        self.set_enabled(False)
        try:
            # Let any entry that observed the previous enabled flag finish its
            # timestamp claim, and deliberately move off the old one-second
            # phase. Original game RPCs and the network reader keep running.
            time.sleep(min(1.0, max(0.01, float(settle_seconds))))
            timestamp = (
                int(filetime_100ns)
                if filetime_100ns is not None
                else time.time_ns() // 100 + 116_444_736_000_000_000
            )
            write_memory(
                self.process,
                self.state + STATE_LAST_REQUEST_OFFSET,
                struct.pack("<Q", max(1, timestamp)),
            )
        finally:
            self.set_enabled(True)
        return True

    def set_raw_lua_request_argument(self, method: str, value: int) -> None:
        """Atomically retarget one configured request to a live string TValue."""

        try:
            encoded_method = str(method).strip().encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("request method must be ASCII") from exc
        try:
            index = self.request_methods.index(encoded_method)
        except ValueError as exc:
            raise ValueError(
                f"request method is not configured: {method!r}"
            ) from exc
        argument_bits = self._raw_lua_argument_bits(value)
        if not self.installed or not self.alive:
            raise RuntimeError("team-stat request hook is not active")
        method_offset = STATE_METHOD_OFFSET + index * STATE_METHOD_STRIDE
        # Publish the valid pointer before enabling its argument kind.
        write_memory(
            self.process,
            self.state + method_offset + STATE_METHOD_ARGUMENT_VALUE_OFFSET,
            struct.pack("<Q", argument_bits),
        )
        write_memory(
            self.process,
            self.state + method_offset + STATE_METHOD_ARGUMENT_KIND_OFFSET,
            struct.pack("<Q", REQUEST_ARGUMENT_RAW_LUA_VALUE),
        )
        kinds = list(self.request_argument_kinds)
        arguments = list(self.request_argument_bits)
        kinds[index] = REQUEST_ARGUMENT_RAW_LUA_VALUE
        arguments[index] = argument_bits
        self.request_argument_kinds = tuple(kinds)
        self.request_argument_bits = tuple(arguments)

    def _learn_argument_metadata(self, record: dict) -> None:
        """Remember a live Lua state and discover its registered string type."""

        try:
            lua_state = int(record.get("lua_state_address", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            lua_state = 0
        if record.get("lua_state_snapshot") and _valid_user_pointer(lua_state):
            self.lua_state_address = lua_state
        if int(getattr(self, "string_descriptor_address", 0) or 0):
            return
        vector = record.get("argument_descriptor_vector")
        if not isinstance(vector, dict):
            return
        items = vector.get("items")
        if not isinstance(items, list):
            return
        addresses: list[int] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                address = int(item.get("address", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if _valid_user_pointer(address):
                addresses.append(address)
        descriptor = find_lua_string_argument_descriptor(
            self.process, tuple(addresses)
        )
        if descriptor:
            self.string_descriptor_address = descriptor
            record["discovered_string_descriptor"] = f"0x{descriptor:016x}"

    def suspend_lua_string_requests(self) -> None:
        """Fail-close all raw-string calls while their target is changing."""

        if self.installed and self.alive:
            write_memory(
                self.process,
                self.state + STATE_STRING_DESCRIPTOR_OFFSET,
                struct.pack("<Q", 0),
            )
        self.active_lua_string_requests = {}

    def set_lua_string_request_argument(self, method: str, value: str) -> int:
        """Resolve and publish one interned string for an active RPC method."""

        token = str(value).strip().replace("\x00", "")
        if not token:
            raise ValueError("Lua string request argument must not be empty")
        if not self.installed or not self.alive:
            raise RuntimeError("team-stat request hook is not active")
        self.suspend_lua_string_requests()
        lua_state = int(getattr(self, "lua_state_address", 0) or 0)
        descriptor = int(getattr(self, "string_descriptor_address", 0) or 0)
        if not lua_state:
            raise RuntimeError("no synchronized Lua state has been captured")
        if not descriptor or not is_lua_string_argument_descriptor(
            self.process, descriptor
        ):
            self.string_descriptor_address = 0
            raise RuntimeError("the Lua string argument descriptor is unavailable")
        raw_value = resolve_lua_string_tvalue(self.process, lua_state, token)
        self.set_raw_lua_request_argument(method, raw_value)
        # Publish the validated descriptor last. Until this write, the native
        # stub cancels raw-string calls before touching the borrowed TValue.
        write_memory(
            self.process,
            self.state + STATE_STRING_DESCRIPTOR_OFFSET,
            struct.pack("<Q", descriptor),
        )
        self.active_lua_string_requests = {str(method): token}
        return raw_value

    def poll_requests(self) -> list[dict]:
        """Return newly committed original outbound calls in exact sequence."""

        if (
            getattr(self, "stable_primary_only", False)
            or not self.installed
            or not self.alive
        ):
            return []
        ring = self.state + REQUEST_RING_OFFSET
        header = read_region(self.process, ring, 0x28)
        if not header or header[:8] != REQUEST_RING_MAGIC:
            raise RuntimeError("outbound request ring became unreadable or corrupt")
        _, write_index, capacity, record_size, target = struct.unpack(
            "<8sQQQQ", header
        )
        if (
            capacity != REQUEST_RECORD_COUNT
            or record_size != REQUEST_RECORD_SIZE
            or target != self.target
        ):
            raise RuntimeError("outbound request ring header changed unexpectedly")
        if write_index - self.request_next_sequence > REQUEST_RECORD_COUNT:
            dropped = write_index - self.request_next_sequence - REQUEST_RECORD_COUNT
            self.request_dropped_count += dropped
            self.request_next_sequence = write_index - REQUEST_RECORD_COUNT
        records: list[dict] = []
        while self.request_next_sequence < write_index:
            slot = self.request_next_sequence & REQUEST_RECORD_MASK
            raw = read_region(
                self.process,
                ring + REQUEST_RECORDS_OFFSET + slot * REQUEST_RECORD_SIZE,
                REQUEST_RECORD_SIZE,
            )
            record = parse_request_record(raw or b"", self.request_next_sequence)
            if record is None:
                break
            sync_state = int(record.get("argument_sync_state", 0) or 0)
            try:
                if sync_state == 1:
                    capture_lua_argument_cells(self.process, record)
                    capture_lua_argument_descriptors(self.process, record)
                try:
                    storage_address = int(
                        record.get("variadic_storage_address", 0) or 0
                    )
                except (TypeError, ValueError, OverflowError):
                    storage_address = 0
                if 0x1_0000 <= storage_address <= 0x0000_7FFF_FFFF_FFFF:
                    storage = read_region(
                        self.process,
                        storage_address,
                        REQUEST_VARIADIC_STORAGE_SNAPSHOT_SIZE,
                    )
                    if storage:
                        # When sync_state is 1 the original call still owns
                        # this storage. Otherwise retain the old evidence-only
                        # post-call behavior and label its weaker timing.
                        record["variadic_storage_snapshot"] = storage.hex()
                        record["variadic_storage_snapshot_size"] = len(storage)
                        record["variadic_storage_capture"] = (
                            "hook_entry_synchronized"
                            if sync_state == 1
                            else "post_call_readprocessmemory"
                        )
            finally:
                if sync_state == 1:
                    write_memory(
                        self.process,
                        ring
                        + REQUEST_RECORDS_OFFSET
                        + slot * REQUEST_RECORD_SIZE
                        + REQUEST_ARGUMENT_ACK_OFFSET,
                        struct.pack(
                            "<Q", int(record["sequence"]) + 1
                        ),
                    )
            self._learn_argument_metadata(record)
            records.append(record)
            self.request_next_sequence += 1
        return records

    def status(self) -> dict[str, object]:
        if not self.installed or not self.alive:
            return {
                "enabled": False,
                "request_count": 0,
                "last_result": 0,
                "request_methods": [
                    method.decode("ascii") for method in self.request_methods
                ],
                "last_request_method": "",
                "captured_request_count": self.request_next_sequence,
                "dropped_request_count": self.request_dropped_count,
                "lua_state_address": "",
                "string_descriptor_address": "",
                "lua_string_request_arguments": {},
            }
        data = read_region(self.process, self.state, 0x40)
        if not data or data[:8] != STATE_MAGIC:
            raise RuntimeError("team-stat request state became unreadable")
        if self.stable_primary_only:
            return {
                "enabled": bool(struct.unpack_from("<Q", data, 0x08)[0]),
                "adopted": bool(self.adopted),
                "last_request_filetime": struct.unpack_from(
                    "<Q", data, 0x10
                )[0],
                "request_count": struct.unpack_from("<Q", data, 0x18)[0],
                "last_result": struct.unpack_from("<Q", data, 0x20)[0],
                "request_methods": [self.request_method.decode("ascii")],
                "numeric_request_arguments": {},
                "raw_lua_request_arguments": {},
                "lua_state_address": "",
                "string_descriptor_address": "",
                "lua_string_request_arguments": {},
                "last_request_method": self.request_method.decode("ascii"),
                "captured_request_count": 0,
                "dropped_request_count": 0,
            }
        ring_header = read_region(
            self.process, self.state + REQUEST_RING_OFFSET, 0x10
        )
        if not ring_header or ring_header[:8] != REQUEST_RING_MAGIC:
            raise RuntimeError("outbound request ring became unreadable")
        last_method_index = struct.unpack_from(
            "<Q", data, STATE_LAST_METHOD_INDEX_OFFSET
        )[0]
        last_request_method = (
            self.request_methods[last_method_index].decode("ascii")
            if last_method_index < len(self.request_methods)
            else ""
        )
        return {
            "enabled": bool(struct.unpack_from("<Q", data, 0x08)[0]),
            "adopted": bool(self.adopted),
            "last_request_filetime": struct.unpack_from("<Q", data, 0x10)[0],
            "request_count": struct.unpack_from("<Q", data, 0x18)[0],
            "last_result": struct.unpack_from("<Q", data, 0x20)[0],
            "request_methods": [
                method.decode("ascii") for method in self.request_methods
            ],
            "numeric_request_arguments": {
                method.decode("ascii"): int(
                    struct.unpack(
                        "<d", struct.pack("<Q", argument_bits)
                    )[0]
                )
                for method, kind, argument_bits in zip(
                    self.request_methods,
                    self.request_argument_kinds,
                    self.request_argument_bits,
                )
                if argument_bits is not None
                and kind == REQUEST_ARGUMENT_NUMBER
            },
            "raw_lua_request_arguments": {
                method.decode("ascii"): f"0x{argument_bits:016x}"
                for method, kind, argument_bits in zip(
                    self.request_methods,
                    self.request_argument_kinds,
                    self.request_argument_bits,
                )
                if argument_bits is not None
                and kind == REQUEST_ARGUMENT_RAW_LUA_VALUE
            },
            "lua_state_address": (
                f"0x{int(getattr(self, 'lua_state_address', 0) or 0):016x}"
                if int(getattr(self, "lua_state_address", 0) or 0)
                else ""
            ),
            "string_descriptor_address": (
                f"0x{int(getattr(self, 'string_descriptor_address', 0) or 0):016x}"
                if int(getattr(self, "string_descriptor_address", 0) or 0)
                else ""
            ),
            "lua_string_request_arguments": dict(
                getattr(self, "active_lua_string_requests", {})
            ),
            "last_request_method": last_request_method,
            "captured_request_count": struct.unpack_from(
                "<Q", ring_header, 0x08
            )[0],
            "dropped_request_count": self.request_dropped_count,
        }

    def close(self) -> None:
        owned = bool(
            self.installed
            and (not self.adopted or self.takeover_existing)
        )
        if self.process and owned and self.alive:
            suspended: list[int] = []
            try:
                suspended = suspend_game_threads(self.pid)
                write_code(self.process, self.target, self.prologue)
                if (
                    read_region(
                        self.process, self.target, len(self.prologue)
                    )
                    != self.prologue
                ):
                    raise RuntimeError(
                        "team-stat request hook restoration verification failed"
                    )
                self.installed = False
            finally:
                if suspended:
                    resume_threads(suspended)
        self.installed = False
        # Do not free code/state pages after detaching an active hook.  A
        # thread suspended inside the trampoline can still return through
        # them after the entry point is restored.  Failed pre-install
        # allocations are safe to release normally.
        if self.process and self.alive and not owned:
            if self.code and not self.adopted:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.code), 0, MEM_RELEASE
                )
            if self.state and not self.adopted:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.state), 0, MEM_RELEASE
                )
        if self.process:
            kernel32.CloseHandle(self.process)
        self.process = 0
        self.state = 0
        self.code = 0
        self.adopted = False
        self.lua_state_address = 0
        self.string_descriptor_address = 0
        self.active_lua_string_requests = {}

    def __enter__(self) -> "TeamStatsRequestHook":
        return self.install()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def parse_numeric_request(value: str) -> tuple[str, int]:
    method, separator, raw_argument = str(value).partition("=")
    method = method.strip()
    raw_argument = raw_argument.strip()
    if not separator or not method or not raw_argument:
        raise argparse.ArgumentTypeError("numeric request must use METHOD=INTEGER")
    try:
        argument = int(raw_argument, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "numeric request argument must be an integer"
        ) from exc
    if not 0 < argument <= 9_007_199_254_740_992:
        raise argparse.ArgumentTypeError(
            "numeric request argument is outside exact Lua range"
        )
    return method, argument


def parse_raw_lua_request(value: str) -> tuple[str, int]:
    method, separator, raw_argument = str(value).partition("=")
    method = method.strip()
    raw_argument = raw_argument.strip()
    if not separator or not method or not raw_argument:
        raise argparse.ArgumentTypeError(
            "raw Lua request must use METHOD=TAGGED_TVALUE"
        )
    try:
        argument = int(raw_argument, 0)
        TeamStatsRequestHook._raw_lua_argument_bits(argument)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return method, argument


def parse_lua_string_request(value: str) -> tuple[str, str]:
    method, separator, string_value = str(value).partition("=")
    method = method.strip()
    if not separator or not method or not string_value:
        raise argparse.ArgumentTypeError("Lua string request must use METHOD=STRING")
    try:
        method.encode("ascii")
        encoded_value = string_value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise argparse.ArgumentTypeError(
            "Lua request method must be ASCII and its value valid UTF-8"
        ) from exc
    if len(encoded_value) > 0x1000:
        raise argparse.ArgumentTypeError("Lua request string is too long")
    return method, string_value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(__file__).resolve().with_name("runtime-profile.dev.json"),
    )
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument(
        "--capture-only",
        action="store_true",
        help="capture original outbound RPCs without issuing team-stat requests",
    )
    parser.add_argument("--sync-method", action="append", default=[])
    parser.add_argument(
        "--additional-request-method",
        action="append",
        default=[],
        help="append an authorized zero-argument request to the round robin",
    )
    parser.add_argument(
        "--numeric-request",
        action="append",
        default=[],
        type=parse_numeric_request,
        metavar="METHOD=INTEGER",
        help=("append a controlled one-number research request to the round " "robin"),
    )
    parser.add_argument(
        "--raw-lua-request",
        action="append",
        default=[],
        type=parse_raw_lua_request,
        metavar="METHOD=TAGGED_TVALUE",
        help=("append a controlled request carrying one tagged Lua string " "TValue"),
    )
    parser.add_argument(
        "--lua-string-request",
        action="append",
        default=[],
        type=parse_lua_string_request,
        metavar="METHOD=STRING",
        help=(
            "resolve one already-interned Lua string and carry it as the "
            "request argument"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    profile = load_runtime_profile_file(args.profile)
    numeric_request_arguments = dict(args.numeric_request)
    raw_lua_request_arguments = dict(args.raw_lua_request)
    lua_string_request_arguments = dict(args.lua_string_request)
    conflicting_methods = (
        set(numeric_request_arguments) & set(raw_lua_request_arguments)
        | set(numeric_request_arguments) & set(lua_string_request_arguments)
        | set(raw_lua_request_arguments) & set(lua_string_request_arguments)
    )
    if conflicting_methods:
        parser.error(
            "request methods have multiple argument kinds: "
            + ", ".join(sorted(conflicting_methods))
        )
    unresolved_lua_strings = dict(lua_string_request_arguments)
    raw_lua_request_arguments.update(
        {method: LUA_GC64_STRING_TAG | 0x1_0000 for method in unresolved_lua_strings}
    )
    additional_request_methods = tuple(
        (
            *args.additional_request_method,
            *numeric_request_arguments,
            *raw_lua_request_arguments,
        )
    )
    deadline = time.monotonic() + max(0.25, args.seconds)
    with TeamStatsRequestHook(
        profile=profile,
        pid=args.pid,
        interval=args.interval,
        enabled=not args.capture_only and not unresolved_lua_strings,
        additional_request_methods=additional_request_methods,
        numeric_request_arguments=numeric_request_arguments,
        raw_lua_request_arguments=raw_lua_request_arguments,
        synchronized_methods=tuple(args.sync_method),
    ) as hook:
        print(
            f"READY pid={hook.pid} target=0x{hook.target:x} " f"state=0x{hook.state:x}",
            flush=True,
        )
        output = (
            args.output.open("a", encoding="utf-8", buffering=1)
            if args.output is not None
            else None
        )
        try:
            while time.monotonic() < deadline and hook.alive:
                progressed = False
                records = hook.poll_requests()
                if unresolved_lua_strings:
                    lua_states = tuple(
                        dict.fromkeys(
                            int(record.get("lua_state_address", 0) or 0)
                            for record in records
                            if int(record.get("lua_state_address", 0) or 0)
                        )
                    )
                    for lua_state in lua_states:
                        for method, string_value in tuple(
                            unresolved_lua_strings.items()
                        ):
                            try:
                                raw_value = resolve_lua_string_tvalue(
                                    hook.process, lua_state, string_value
                                )
                            except (LookupError, RuntimeError, ValueError):
                                continue
                            hook.set_raw_lua_request_argument(method, raw_value)
                            del unresolved_lua_strings[method]
                            print(
                                f"RESOLVED method={method} "
                                f"value={string_value!r} "
                                f"tvalue=0x{raw_value:016x}",
                                flush=True,
                            )
                    if not unresolved_lua_strings and not args.capture_only:
                        hook.set_enabled(True)
                for record in records:
                    line = json.dumps(record, ensure_ascii=False)
                    if output is not None:
                        output.write(line + "\n")
                    else:
                        print(line, flush=True)
                    progressed = True
                if not progressed:
                    time.sleep(0.01)
        finally:
            if output is not None:
                output.close()
        print(f"STATUS {hook.status()}", flush=True)
    print("DONE restored", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
