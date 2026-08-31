#!/usr/bin/env python3
"""Request exact per-player team damage through the game's own RPC thread.

The hook is intentionally narrow and build-specific.  It intercepts the
confirmed ScriptEntity::call_server overload, rate-limits an additional
zero-argument ReqCommonCombatStatisticsByTeam call, then continues the
original request untouched.  All entry-point bytes are restored on close.
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import time

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


CALL_SERVER_RVA = 0x09A5AD40
CALL_SERVER_PROLOGUE = bytes.fromhex(
    "40 53 48 83 ec 40 48 8b 81 e8 00 00 00 48 8b d9"
)
REQUEST_METHOD = b"ReqCommonCombatStatisticsByTeam"
STATE_MAGIC = b"GMZZSTAT"
STATE_SIZE = 0x300
CODE_SIZE = 0x1000
STATE_ENABLED_OFFSET = 0x08
STATE_LAST_REQUEST_OFFSET = 0x10
STATE_REQUEST_COUNT_OFFSET = 0x18
STATE_LAST_RESULT_OFFSET = 0x20
STATE_ARGUMENTS_OFFSET = 0x100
STATE_ARGUMENTS_SIZE = 0x78
STATE_VARIADIC_OFFSET = 0x180
STATE_EMPTY_DESCRIPTOR_OFFSET = 0x1C0
STATE_NULL_CELL_OFFSET = 0x200
STATE_TRAILING_CELL_OFFSET = 0x208
STATE_METHOD_OFFSET = 0x240
STATE_METHOD_TEXT_OFFSET = 0x280
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


def build_request_stub(
    state: int,
    trampoline: int,
    target: int,
    interval_100ns: int,
) -> bytes:
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
    code += b"\x48\x89\xcb"  # rbx = original ScriptEntity
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
    code += b"\x48\x89\xd9"
    code += b"\x49\xba" + struct.pack("<Q", state + STATE_ARGUMENTS_OFFSET)
    code += b"\x4c\x89\xd2"
    code += b"\x49\xb8" + struct.pack("<Q", state + STATE_METHOD_OFFSET)
    code += b"\x49\xb9" + struct.pack(
        "<Q", state + STATE_VARIADIC_OFFSET
    )
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
    code += CALL_SERVER_PROLOGUE
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack(
        "<Q", target + len(CALL_SERVER_PROLOGUE)
    )
    if len(code) >= TRAMPOLINE_OFFSET:
        raise AssertionError(f"request stub is too large: {len(code)}")
    return bytes(code)


def build_trampoline(target: int) -> bytes:
    return (
        CALL_SERVER_PROLOGUE
        + b"\xff\x25\x00\x00\x00\x00"
        + struct.pack("<Q", target + len(CALL_SERVER_PROLOGUE))
    )


class TeamStatsRequestHook:
    def __init__(
        self,
        *,
        pid: int | None = None,
        interval: float = 1.0,
        enabled: bool = True,
    ):
        self.requested_pid = pid
        self.interval = max(0.25, float(interval))
        self.initially_enabled = bool(enabled)
        self.pid = 0
        self.process = 0
        self.base = 0
        self.target = 0
        self.state = 0
        self.code = 0
        self.installed = False

    @property
    def alive(self) -> bool:
        return bool(self.process and process_alive(self.process))

    def install(self) -> "TeamStatsRequestHook":
        if self.installed:
            return self
        self.pid = self.requested_pid or find_pid("C7-Win64-Shipping.exe")
        self.base, module_size, _path = find_module(
            self.pid, "C7-Win64-Shipping.exe"
        )
        if CALL_SERVER_RVA + len(CALL_SERVER_PROLOGUE) > module_size:
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
        self.target = self.base + CALL_SERVER_RVA
        actual = read_region(self.process, self.target, len(CALL_SERVER_PROLOGUE))
        if actual != CALL_SERVER_PROLOGUE:
            self.close()
            got = actual.hex(" ") if actual else "unreadable"
            raise RuntimeError(
                "team-stat request signature mismatch at "
                f"RVA 0x{CALL_SERVER_RVA:x}: {got}"
            )
        try:
            self.state = int(
                kernel32.VirtualAllocEx(
                    self.process,
                    None,
                    STATE_SIZE,
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

            method_text = self.state + STATE_METHOD_TEXT_OFFSET
            state_data = bytearray(STATE_SIZE)
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
                "<QQQQ",
                state_data,
                STATE_METHOD_OFFSET,
                method_text,
                0,
                len(REQUEST_METHOD),
                0x2F,
            )
            struct.pack_into(
                "<QQQQ",
                state_data,
                STATE_VARIADIC_OFFSET,
                0,
                self.state + STATE_EMPTY_DESCRIPTOR_OFFSET,
                0xFFFF_FFFF_FFFF_FFFF,
                0,
            )
            struct.pack_into(
                "<Q", state_data, STATE_TRAILING_CELL_OFFSET, 1
            )
            state_data[
                STATE_METHOD_TEXT_OFFSET : STATE_METHOD_TEXT_OFFSET
                + len(REQUEST_METHOD)
            ] = REQUEST_METHOD
            write_memory(self.process, self.state, bytes(state_data))

            trampoline = self.code + TRAMPOLINE_OFFSET
            stub = build_request_stub(
                self.state,
                trampoline,
                self.target,
                int(self.interval * 10_000_000),
            )
            trampoline_code = build_trampoline(self.target)
            write_memory(self.process, self.code, stub)
            write_memory(self.process, trampoline, trampoline_code)
            kernel32.FlushInstructionCache(
                self.process, ctypes.c_void_p(self.code), CODE_SIZE
            )
            patch = build_absolute_patch(self.code, len(CALL_SERVER_PROLOGUE))
            suspended = suspend_game_threads(self.pid)
            try:
                write_code(self.process, self.target, patch)
                self.installed = True
            finally:
                resume_threads(suspended)
            if read_region(self.process, self.target, len(patch)) != patch:
                raise RuntimeError("team-stat request hook verification failed")
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

    def status(self) -> dict[str, int | bool]:
        if not self.installed or not self.alive:
            return {"enabled": False, "request_count": 0, "last_result": 0}
        data = read_region(self.process, self.state, 0x28)
        if not data or data[:8] != STATE_MAGIC:
            raise RuntimeError("team-stat request state became unreadable")
        return {
            "enabled": bool(struct.unpack_from("<Q", data, 0x08)[0]),
            "last_request_filetime": struct.unpack_from("<Q", data, 0x10)[0],
            "request_count": struct.unpack_from("<Q", data, 0x18)[0],
            "last_result": struct.unpack_from("<Q", data, 0x20)[0],
        }

    def close(self) -> None:
        owned = bool(self.installed)
        if self.process and owned and self.alive:
            suspended: list[int] = []
            try:
                suspended = suspend_game_threads(self.pid)
                write_code(self.process, self.target, CALL_SERVER_PROLOGUE)
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
            if self.code:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.code), 0, MEM_RELEASE
                )
            if self.state:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.state), 0, MEM_RELEASE
                )
        if self.process:
            kernel32.CloseHandle(self.process)
        self.process = 0
        self.state = 0
        self.code = 0

    def __enter__(self) -> "TeamStatsRequestHook":
        return self.install()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    deadline = time.monotonic() + max(0.25, args.seconds)
    with TeamStatsRequestHook(pid=args.pid, interval=args.interval) as hook:
        print(
            f"READY pid={hook.pid} target=0x{hook.target:x} "
            f"state=0x{hook.state:x}",
            flush=True,
        )
        while time.monotonic() < deadline and hook.alive:
            time.sleep(0.05)
        print(f"STATUS {hook.status()}", flush=True)
    print("DONE restored", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
