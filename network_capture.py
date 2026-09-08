#!/usr/bin/env python3
"""Capture C7 messages after transport decrypt/decompress and RPC decoding."""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import json
import math
import struct
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from inline_capture import (
    MEM_COMMIT,
    MEM_RELEASE,
    MEM_RESERVE,
    PAGE_EXECUTE_READWRITE,
    PAGE_READWRITE,
    THREAD_QUERY_INFORMATION,
    THREAD_SUSPEND_RESUME,
    build_absolute_patch,
    process_alive,
    resume_threads,
    thread_ids,
    write_code,
    write_memory,
)
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


PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020

MESSAGE_MAGIC = b"GMZZNET2"
MESSAGE_RECORD_SIZE = 0x100
MESSAGE_RECORD_COUNT = 4096
MESSAGE_RECORD_MASK = MESSAGE_RECORD_COUNT - 1
MESSAGE_RECORDS_OFFSET = 0x100
MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET = 0x28
MESSAGE_ARGUMENT_SYNC_STATE_OFFSET = 0xE8
MESSAGE_ARGUMENT_ACK_OFFSET = 0xF0
MESSAGE_COMMIT_OFFSET = 0xF8
# These messages own authoritative damage or low-frequency encounter-boundary
# values. The decoded msgpack graph is stack/arena backed and can be reused as
# soon as do_message continues, so the hook waits for the reader to finish.
MESSAGE_ARGUMENT_SYNC_TIMEOUT_100NS = 2_500_000  # 250 ms hard fail-open
MESSAGE_RING_SIZE = (
    MESSAGE_RECORDS_OFFSET + MESSAGE_RECORD_COUNT * MESSAGE_RECORD_SIZE
)


def _emit_near_branch(code: bytearray, opcode: bytes) -> int:
    code += opcode
    displacement_at = len(code)
    code += b"\x00\x00\x00\x00"
    return displacement_at


def _patch_near_branch(
    code: bytearray, displacement_at: int, target: int
) -> None:
    struct.pack_into(
        "<i", code, displacement_at, target - (displacement_at + 4)
    )


def _emit_record_method_candidate(code: bytearray, method: bytes) -> int:
    """Branch to a shared match target when the captured method is exact."""
    if not method or len(method) > 0x7F:
        raise ValueError("synchronous RPC method length is unsupported")
    mismatches: list[int] = []
    code += b"\x49\x83\xbb\x38\x00\x00\x00" + bytes([len(method)])
    mismatches.append(_emit_near_branch(code, b"\x0f\x85"))
    offset = 0
    while len(method) - offset >= 8:
        code += b"\x48\xb8" + method[offset : offset + 8]
        code += b"\x49\x39\x83" + struct.pack("<I", 0x40 + offset)
        mismatches.append(_emit_near_branch(code, b"\x0f\x85"))
        offset += 8
    if len(method) - offset >= 4:
        code += b"\xb8" + method[offset : offset + 4]
        code += b"\x41\x39\x83" + struct.pack("<I", 0x40 + offset)
        mismatches.append(_emit_near_branch(code, b"\x0f\x85"))
        offset += 4
    if len(method) - offset >= 2:
        code += b"\x66\xb8" + method[offset : offset + 2]
        code += b"\x66\x41\x39\x83" + struct.pack(
            "<I", 0x40 + offset
        )
        mismatches.append(_emit_near_branch(code, b"\x0f\x85"))
        offset += 2
    if len(method) - offset:
        code += b"\xb0" + method[offset : offset + 1]
        code += b"\x41\x38\x83" + struct.pack("<I", 0x40 + offset)
        mismatches.append(_emit_near_branch(code, b"\x0f\x85"))
    matched = _emit_near_branch(code, b"\xe9")
    next_candidate = len(code)
    for displacement_at in mismatches:
        _patch_near_branch(code, displacement_at, next_candidate)
    return matched


def suspend_game_threads(pid: int) -> list[int]:
    """Suspend accessible game threads while ignoring ACE-owned protected ones."""
    suspended: list[int] = []
    access = THREAD_SUSPEND_RESUME | THREAD_QUERY_INFORMATION
    try:
        for thread_id in thread_ids(pid):
            thread = kernel32.OpenThread(access, False, thread_id)
            if not thread:
                if ctypes.get_last_error() == 5:
                    continue
                raise winerror(f"OpenThread({thread_id})")
            previous = kernel32.SuspendThread(thread)
            if previous == 0xFFFFFFFF:
                error = ctypes.get_last_error()
                kernel32.CloseHandle(thread)
                if error == 5:
                    continue
                raise winerror(f"SuspendThread({thread_id})")
            suspended.append(int(thread))
        if not suspended:
            raise RuntimeError("no accessible game threads could be suspended")
        return suspended
    except Exception:
        resume_threads(suspended)
        raise


def build_message_stub(
    ring: int,
    resume: int,
    *,
    prologue: bytes,
    synchronized_methods: tuple[bytes, ...],
    return_address_stack_offset: int = 0x48,
) -> bytes:
    """Capture do_message registers and copy its decoded RPC method name."""
    if not 0 <= return_address_stack_offset <= 0x7F:
        raise ValueError("return-address stack offset must fit disp8")
    code = bytearray()
    code += b"\x9c"  # pushfq
    code += b"\x50\x53\x51\x52"  # rax, rbx, rcx, rdx
    code += b"\x41\x50\x41\x51\x41\x52\x41\x53"  # r8-r11
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"  # lock xadd [r10+8], rax
    code += b"\x48\x89\xc3"  # sequence
    code += b"\x25" + struct.pack("<I", MESSAGE_RECORD_MASK)
    code += b"\x48\xc1\xe0\x08"  # record size 0x100
    code += b"\x4d\x8d\x9c\x02" + struct.pack("<I", MESSAGE_RECORDS_OFFSET)
    code += b"\x49\x89\x1b"
    code += b"\x49\xc7\x83" + struct.pack(
        "<I", MESSAGE_ARGUMENT_SYNC_STATE_OFFSET
    ) + b"\x00\x00\x00\x00"
    code += b"\x49\xc7\x83" + struct.pack(
        "<I", MESSAGE_ARGUMENT_ACK_OFFSET
    ) + b"\x00\x00\x00\x00"
    code += b"\x49\x89\x4b\x10"  # ScriptEntity
    code += b"\x49\x89\x53\x18"  # decoded message context
    code += b"\x4d\x89\x43\x20"  # std::string method name
    code += b"\x4d\x89\x4b\x28"  # decoded arguments
    code += b"\x48\x8b\x44\x24" + bytes([return_address_stack_offset])
    code += b"\x49\x89\x43\x30"  # return address

    # KUSER_SHARED_DATA.SystemTime -> FILETIME.
    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x41\x8b\x42\x18\x41\x8b\x52\x14\x41\x3b\x42\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0\x49\x89\x43\x08"

    # MSVC std::string uses a 16-byte inline buffer and stores size/capacity at
    # +0x10/+0x18. Copy a bounded method name while the object is alive.
    code += b"\x41\xc6\x43\x40\x00"
    code += b"\x49\x8b\x48\x10\x49\x89\x4b\x38"
    done_jumps: list[int] = []
    code += b"\x48\x85\xc9"
    done_jumps.append(len(code))
    code += b"\x74\x00"
    code += b"\x49\x83\x78\x18\x10"
    inline_jump = len(code)
    code += b"\x72\x00"
    code += b"\x49\x8b\x10"
    data_ready_jump = len(code)
    code += b"\xeb\x00"
    inline_data = len(code)
    code += b"\x4c\x89\xc2"
    data_ready = len(code)
    code[inline_jump + 1] = (inline_data - (inline_jump + 2)) & 0xFF
    code[data_ready_jump + 1] = (data_ready - (data_ready_jump + 2)) & 0xFF
    code += b"\x48\x83\xf9\x7f"
    bounded_jump = len(code)
    code += b"\x76\x00"
    code += b"\xb9\x7f\x00\x00\x00"
    bounded = len(code)
    code[bounded_jump + 1] = (bounded - (bounded_jump + 2)) & 0xFF
    code += b"\x33\xc0"
    copy_loop = len(code)
    code += b"\x44\x8a\x14\x02"  # r10b = data[index]
    code += b"\x45\x88\x94\x03\x40\x00\x00\x00"
    code += b"\x48\xff\xc0\x48\x3b\xc1"
    code += b"\x72" + bytes([(copy_loop - (len(code) + 2)) & 0xFF])
    code += b"\x41\xc6\x84\x03\x40\x00\x00\x00\x00"
    done = len(code)
    for jump in done_jumps:
        code[jump + 1] = (done - (jump + 2)) & 0xFF

    matched_jumps = [
        _emit_record_method_candidate(code, method)
        for method in synchronized_methods
    ]
    skip_sync_state = _emit_near_branch(code, b"\xe9")
    matched = len(code)
    for displacement_at in matched_jumps:
        _patch_near_branch(code, displacement_at, matched)
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\x49\x83\x7a" + bytes(
        [MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET, 1]
    )
    sync_disabled = _emit_near_branch(code, b"\x0f\x85")
    code += b"\x49\xc7\x83" + struct.pack(
        "<I", MESSAGE_ARGUMENT_SYNC_STATE_OFFSET
    ) + b"\x01\x00\x00\x00"
    sync_enabled = _emit_near_branch(code, b"\xe9")
    disabled = len(code)
    _patch_near_branch(code, sync_disabled, disabled)
    code += b"\x49\xc7\x83" + struct.pack(
        "<I", MESSAGE_ARGUMENT_SYNC_STATE_OFFSET
    ) + b"\x03\x00\x00\x00"
    commit = len(code)
    _patch_near_branch(code, skip_sync_state, commit)
    _patch_near_branch(code, sync_enabled, commit)

    code += b"\x48\x8d\x43\x01"
    code += b"\x49\x89\x83" + struct.pack("<I", MESSAGE_COMMIT_OFFSET)

    # Selected authoritative/encounter-boundary messages wait. The game thread
    # is released as soon as Python acknowledges the decoded graph, or after
    # 250 ms if the reader has stopped/crashed. This prevents arena reuse from
    # changing arguments halfway through a read without a permanent hang.
    code += b"\x49\x83\xbb" + struct.pack(
        "<I", MESSAGE_ARGUMENT_SYNC_STATE_OFFSET
    ) + b"\x01"
    no_wait = _emit_near_branch(code, b"\x0f\x85")
    code += b"\x4c\x8d\x4b\x01"  # r9 = sequence + 1 acknowledgement
    wait_loop = len(code)
    code += b"\x4d\x39\x8b" + struct.pack(
        "<I", MESSAGE_ARGUMENT_ACK_OFFSET
    )
    acknowledged = _emit_near_branch(code, b"\x0f\x84")
    code += b"\xf3\x90"  # pause
    code += b"\x41\xba\x00\x00\xfe\x7f"
    clock_retry = len(code)
    code += b"\x41\x8b\x42\x18"
    code += b"\x41\x8b\x52\x14"
    code += b"\x41\x3b\x42\x1c"
    clock_changed = _emit_near_branch(code, b"\x0f\x85")
    _patch_near_branch(code, clock_changed, clock_retry)
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0"
    code += b"\x49\x2b\x43\x08"
    code += b"\x48\x3d" + struct.pack(
        "<I", MESSAGE_ARGUMENT_SYNC_TIMEOUT_100NS
    )
    keep_waiting = _emit_near_branch(code, b"\x0f\x82")
    _patch_near_branch(code, keep_waiting, wait_loop)
    code += b"\x49\xc7\x83" + struct.pack(
        "<I", MESSAGE_ARGUMENT_SYNC_STATE_OFFSET
    ) + b"\x03\x00\x00\x00"
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\x49\xc7\x42" + bytes(
        [MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET]
    ) + b"\x00\x00\x00\x00"
    wait_done = _emit_near_branch(code, b"\xe9")
    acknowledged_at = len(code)
    _patch_near_branch(code, acknowledged, acknowledged_at)
    code += b"\x49\xc7\x83" + struct.pack(
        "<I", MESSAGE_ARGUMENT_SYNC_STATE_OFFSET
    ) + b"\x02\x00\x00\x00"
    done_waiting = len(code)
    _patch_near_branch(code, wait_done, done_waiting)
    _patch_near_branch(code, no_wait, done_waiting)

    code += b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59\x5b\x58\x9d"
    code += prologue
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
    return bytes(code)


def parse_message_record(data: bytes, expected_sequence: int) -> dict | None:
    if len(data) != MESSAGE_RECORD_SIZE:
        return None
    values = struct.unpack_from("<8Q", data)
    (
        sequence,
        filetime_100ns,
        script_entity,
        context,
        method_object,
        arguments,
        return_address,
        method_length,
    ) = values
    argument_sync_state = struct.unpack_from(
        "<Q", data, MESSAGE_ARGUMENT_SYNC_STATE_OFFSET
    )[0]
    commit = struct.unpack_from("<Q", data, MESSAGE_COMMIT_OFFSET)[0]
    if sequence != expected_sequence or commit != expected_sequence + 1:
        return None
    raw_method = data[0x40:0xC0].split(b"\x00", 1)[0]
    method = raw_method.decode("utf-8", "replace")
    event_time = dt.datetime.fromtimestamp(
        (filetime_100ns - 116_444_736_000_000_000) / 10_000_000,
        tz=dt.timezone.utc,
    ).astimezone()
    return {
        "event_time": event_time.isoformat(timespec="microseconds"),
        "filetime_100ns": filetime_100ns,
        "sequence": sequence,
        "function": "doraemon::script::ScriptEntity::do_message",
        "script_entity": script_entity,
        "context": context,
        "method_object": method_object,
        "arguments": arguments,
        "return_address": f"0x{return_address:016x}",
        "method_length": method_length,
        "method": method,
        "argument_sync_state": argument_sync_state,
    }


class MessageDecodeError(RuntimeError):
    pass


class RemoteMsgpackReader:
    """Decode a msgpack::object graph from the live, post-decrypt RPC call."""

    OBJECT_SIZE = 24
    MAX_DEPTH = 32
    MAX_ITEMS = 20_000
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, process: int):
        self.process = process
        self.items_read = 0
        self.bytes_read = 0

    @staticmethod
    def _valid_pointer(address: int) -> bool:
        return 0x10000 <= address < (1 << 47)

    def _read(self, address: int, size: int) -> bytes:
        if size < 0 or self.bytes_read + size > self.MAX_BYTES:
            raise MessageDecodeError("decoded message exceeds byte limit")
        if size and not self._valid_pointer(address):
            raise MessageDecodeError(f"invalid message pointer 0x{address:x}")
        data = read_region(self.process, address, size)
        if data is None or len(data) != size:
            raise MessageDecodeError(
                f"message memory became unavailable at 0x{address:x}"
            )
        self.bytes_read += size
        return data

    def decode(self, address: int):
        self.items_read = 0
        self.bytes_read = 0
        return self._decode_object(address, 0)

    def _decode_object(self, address: int, depth: int):
        return self._decode_raw_object(self._read(address, self.OBJECT_SIZE), depth)

    def _decode_raw_object(self, raw: bytes, depth: int):
        if depth > self.MAX_DEPTH:
            raise MessageDecodeError("decoded message exceeds nesting limit")
        self.items_read += 1
        if self.items_read > self.MAX_ITEMS:
            raise MessageDecodeError("decoded message exceeds item limit")
        if len(raw) != self.OBJECT_SIZE:
            raise MessageDecodeError("decoded message object is truncated")
        object_type = struct.unpack_from("<I", raw)[0]
        scalar = struct.unpack_from("<Q", raw, 8)[0]
        pointer = struct.unpack_from("<Q", raw, 16)[0]

        if object_type == 0:  # NIL
            return None
        if object_type == 1:  # BOOLEAN
            return bool(scalar & 0xFF)
        if object_type == 2:  # POSITIVE_INTEGER
            return scalar
        if object_type == 3:  # NEGATIVE_INTEGER
            return struct.unpack_from("<q", raw, 8)[0]
        if object_type in (4, 10):  # FLOAT64/FLOAT32 are normalized to double
            value = struct.unpack_from("<d", raw, 8)[0]
            return value if math.isfinite(value) else str(value)
        if object_type in (5, 6):  # STR/BIN
            size = scalar & 0xFFFFFFFF
            if size > self.MAX_BYTES:
                raise MessageDecodeError("decoded string exceeds byte limit")
            value = self._read(pointer, size) if size else b""
            if object_type == 6:
                return {"$binary": base64.b64encode(value).decode("ascii")}
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    "$text": value.decode("utf-8", "replace"),
                    "$raw": base64.b64encode(value).decode("ascii"),
                }
        if object_type == 7:  # ARRAY
            size = scalar & 0xFFFFFFFF
            if size > self.MAX_ITEMS - self.items_read:
                raise MessageDecodeError("decoded array exceeds item limit")
            children = self._read(pointer, size * self.OBJECT_SIZE) if size else b""
            return [
                self._decode_raw_object(
                    children[
                        index * self.OBJECT_SIZE : (index + 1) * self.OBJECT_SIZE
                    ],
                    depth + 1,
                )
                for index in range(size)
            ]
        if object_type == 8:  # MAP
            size = scalar & 0xFFFFFFFF
            if size * 2 > self.MAX_ITEMS - self.items_read:
                raise MessageDecodeError("decoded map exceeds item limit")
            entries = (
                self._read(pointer, size * self.OBJECT_SIZE * 2) if size else b""
            )
            pairs = []
            for index in range(size):
                pair = index * self.OBJECT_SIZE * 2
                key = self._decode_raw_object(
                    entries[pair : pair + self.OBJECT_SIZE], depth + 1
                )
                value = self._decode_raw_object(
                    entries[
                        pair + self.OBJECT_SIZE : pair + self.OBJECT_SIZE * 2
                    ],
                    depth + 1,
                )
                pairs.append((key, value))
            if all(isinstance(key, str) for key, _value in pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        return {"$map": [[item_key, item] for item_key, item in pairs]}
                    result[key] = value
                return result
            return {"$map": [[key, value] for key, value in pairs]}
        if object_type == 9:  # EXT
            extension_type = struct.unpack_from("<b", raw, 8)[0]
            size = struct.unpack_from("<I", raw, 12)[0]
            value = self._read(pointer, size) if size else b""
            return {
                "$extension": {
                    "type": extension_type,
                    "data": base64.b64encode(value).decode("ascii"),
                }
            }
        raise MessageDecodeError(f"unknown msgpack object type {object_type}")


class NetworkMessageHook:
    def __init__(
        self,
        *,
        profile: Mapping[str, object],
        pid: int | None = None,
        takeover_existing: bool = False,
    ):
        self.profile = normalize_runtime_profile(profile)
        hook = runtime_profile_hook(self.profile, "network_message")
        protocol = self.profile["protocol"]
        if not isinstance(protocol, dict):
            raise ValueError("runtime protocol profile is invalid")
        self.rva = int(hook["rva"])
        self.prologue = bytes(hook["prologue"])
        self.signature = bytes(hook["signature"])
        self.synchronized_methods = tuple(
            str(method).encode("ascii")
            for method in protocol["synchronized_methods"]
        )
        self.synchronized_method_names = frozenset(
            method.decode("ascii") for method in self.synchronized_methods
        )
        self.requested_pid = pid
        self.takeover_existing = bool(takeover_existing)
        self.pid = 0
        self.base = 0
        self.process = 0
        self.target = 0
        self.ring = 0
        self.stub = 0
        self.installed = False
        self.adopted = False
        self.next_sequence = 0

    @property
    def alive(self) -> bool:
        return bool(self.process and process_alive(self.process))

    def _adopt_existing(self, patch: bytes) -> bool:
        if (
            len(patch) != len(self.prologue)
            or patch[:6] != b"\xff\x25\0\0\0\0"
        ):
            return False
        stub = struct.unpack_from("<Q", patch, 6)[0]
        prefix = b"\x9c\x50\x53\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53\x49\xba"
        stub_head = read_region(self.process, stub, len(prefix) + 8)
        if not stub_head or not stub_head.startswith(prefix):
            return False
        ring = struct.unpack_from("<Q", stub_head, len(prefix))[0]
        header = read_region(self.process, ring, 48)
        if not header or header[:8] != MESSAGE_MAGIC:
            return False
        _, write_index, capacity, record_size, target, _sync_enabled = struct.unpack(
            "<8sQQQQQ", header
        )
        if (
            capacity != MESSAGE_RECORD_COUNT
            or record_size != MESSAGE_RECORD_SIZE
            or target != self.target
        ):
            return False
        expected_stub = build_message_stub(
            ring,
            self.target + len(self.prologue),
            prologue=self.prologue,
            synchronized_methods=self.synchronized_methods,
        )
        if read_region(self.process, stub, len(expected_stub)) != expected_stub:
            return False
        self.stub = stub
        self.ring = ring
        self.next_sequence = write_index
        self.installed = True
        self.adopted = True
        return True

    def install(self) -> "NetworkMessageHook":
        if self.installed:
            return self
        self.pid = self.requested_pid or find_pid("C7-Win64-Shipping.exe")
        self.base, module_size, _path = find_module(
            self.pid, "C7-Win64-Shipping.exe"
        )
        if self.rva + len(self.signature) > module_size:
            raise RuntimeError("network message target is outside the live module")
        self.target = self.base + self.rva
        access = (
            PROCESS_QUERY_INFORMATION
            | PROCESS_VM_OPERATION
            | PROCESS_VM_READ
            | PROCESS_VM_WRITE
        )
        self.process = int(kernel32.OpenProcess(access, False, self.pid) or 0)
        if not self.process:
            raise winerror("OpenProcess(network message)")
        actual = read_region(
            self.process, self.target, len(self.signature)
        )
        if actual != self.signature:
            if actual and self._adopt_existing(
                actual[: len(self.prologue)]
            ):
                return self
            self.close()
            raise RuntimeError(
                "network message version/signature does not match the "
                f"authorized runtime profile {self.profile['profile_id']}"
            )
        try:
            self.ring = int(
                kernel32.VirtualAllocEx(
                    self.process,
                    None,
                    MESSAGE_RING_SIZE,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_READWRITE,
                )
                or 0
            )
            if not self.ring:
                raise winerror("VirtualAllocEx(network ring)")
            self.stub = int(
                kernel32.VirtualAllocEx(
                    self.process,
                    None,
                    0x1000,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_EXECUTE_READWRITE,
                )
                or 0
            )
            if not self.stub:
                raise winerror("VirtualAllocEx(network stub)")
            header = struct.pack(
                "<8sQQQQQ",
                MESSAGE_MAGIC,
                0,
                MESSAGE_RECORD_COUNT,
                MESSAGE_RECORD_SIZE,
                self.target,
                1,
            )
            write_memory(self.process, self.ring, header)
            stub = build_message_stub(
                self.ring,
                self.target + len(self.prologue),
                prologue=self.prologue,
                synchronized_methods=self.synchronized_methods,
            )
            write_memory(self.process, self.stub, stub)
            kernel32.FlushInstructionCache(
                self.process, ctypes.c_void_p(self.stub), len(stub)
            )
            patch = build_absolute_patch(self.stub, len(self.prologue))
            suspended = suspend_game_threads(self.pid)
            try:
                write_code(self.process, self.target, patch)
                self.installed = True
            finally:
                resume_threads(suspended)
            if read_region(self.process, self.target, len(patch)) != patch:
                raise RuntimeError("network message hook verification failed")
            return self
        except Exception:
            self.close()
            raise

    def poll(
        self,
        *,
        decode_arguments: bool = False,
        decode_method_filter: Callable[[str], bool] | None = None,
        include_snapshots: bool = False,
    ) -> list[dict]:
        if not self.installed or not self.alive:
            return []
        header = read_region(self.process, self.ring, 0x30)
        if not header or header[:8] != MESSAGE_MAGIC:
            raise RuntimeError("network message ring became unreadable or corrupt")
        write_index = struct.unpack_from("<Q", header, 8)[0]
        sync_enabled = struct.unpack_from(
            "<Q", header, MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET
        )[0]
        if not sync_enabled:
            write_memory(
                self.process,
                self.ring + MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET,
                struct.pack("<Q", 1),
            )
        if write_index - self.next_sequence > MESSAGE_RECORD_COUNT:
            self.next_sequence = write_index - MESSAGE_RECORD_COUNT
        records: list[dict] = []
        while self.next_sequence < write_index:
            slot = self.next_sequence & MESSAGE_RECORD_MASK
            record_address = (
                self.ring
                + MESSAGE_RECORDS_OFFSET
                + slot * MESSAGE_RECORD_SIZE
            )
            raw = read_region(
                self.process,
                record_address,
                MESSAGE_RECORD_SIZE,
            )
            record = parse_message_record(raw or b"", self.next_sequence)
            if record is None:
                break
            method = str(record.get("method", ""))
            synchronized = method in self.synchronized_method_names
            sync_state = int(record.get("argument_sync_state", 0) or 0)
            should_decode = decode_arguments and (
                decode_method_filter is None or decode_method_filter(method)
            )
            try:
                if should_decode and synchronized and sync_state != 1:
                    record["decode_error"] = (
                        "synchronized argument read timed out"
                    )
                elif should_decode:
                    record["decoded_arguments"] = RemoteMsgpackReader(
                        self.process
                    ).decode(int(record["arguments"]))
                    decoded_at_100ns = (
                        time.time_ns() // 100 + 116_444_736_000_000_000
                    )
                    record["decode_delay_ms"] = max(
                        0.0,
                        (decoded_at_100ns - int(record["filetime_100ns"])) / 10_000,
                    )
                    if synchronized:
                        record["arguments_synchronized"] = True
            except MessageDecodeError as exc:
                record["decode_error"] = str(exc)
            finally:
                if synchronized and sync_state == 1:
                    write_memory(
                        self.process,
                        record_address + MESSAGE_ARGUMENT_ACK_OFFSET,
                        struct.pack("<Q", int(record["sequence"]) + 1),
                    )
            if include_snapshots:
                for field in ("context", "arguments"):
                    address = int(record[field])
                    snapshot = read_region(self.process, address, 0x100) if address else None
                    record[f"{field}_snapshot"] = (snapshot or b"").hex()
            records.append(record)
            self.next_sequence += 1
        return records

    def close(self) -> None:
        owned = self.installed and (
            not self.adopted or self.takeover_existing
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
                        "network message hook restoration verification failed"
                    )
                self.installed = False
            finally:
                if suspended:
                    resume_threads(suspended)
        self.installed = False
        # A game thread can already be executing inside the trampoline when
        # the entry point is restored.  Freeing its pages here races that
        # thread after it is resumed and can crash the game.  Owned pages are
        # therefore intentionally left unreachable; Windows reclaims them
        # when the game exits.  Allocations from a failed pre-install attempt
        # were never executable and remain safe to release.
        if self.process and self.alive and not owned:
            if self.stub and not self.adopted:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.stub), 0, MEM_RELEASE
                )
            if self.ring and not self.adopted:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.ring), 0, MEM_RELEASE
                )
        if self.process:
            kernel32.CloseHandle(self.process)
        self.process = 0
        self.ring = 0
        self.stub = 0
        self.installed = False
        self.adopted = False

    def __enter__(self) -> "NetworkMessageHook":
        return self.install()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(__file__).resolve().with_name("runtime-profile.dev.json"),
    )
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=Path("network_messages.jsonl"))
    parser.add_argument("--decode", action="store_true")
    parser.add_argument("--snapshots", action="store_true")
    args = parser.parse_args()
    profile = load_runtime_profile_file(args.profile)
    deadline = time.monotonic() + max(0.1, args.seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with NetworkMessageHook(profile=profile, pid=args.pid) as hook, args.output.open(
        "a", encoding="utf-8", buffering=1
    ) as output:
        print(
            f"READY pid={hook.pid} target=0x{hook.target:x} output={args.output}",
            flush=True,
        )
        while time.monotonic() < deadline and hook.alive:
            records = hook.poll(
                decode_arguments=args.decode,
                include_snapshots=args.snapshots,
            )
            for record in records:
                line = json.dumps(record, ensure_ascii=False)
                output.write(line + "\n")
                print(line, flush=True)
            time.sleep(0.002 if records else 0.01)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
