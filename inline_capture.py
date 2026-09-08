#!/usr/bin/env python3
"""Low-overhead C7 damage argument capture without a debugger or DLL.

The current game build's KAPI_HandleDamageSyncV2 entry is redirected to a tiny
position-independent x64 stub.  The stub copies call arguments into a private
ring buffer allocated inside the target, executes the displaced prologue, and
returns to the original function.  This controller only polls that buffer.

All allocations and the original 14 bytes are restored on normal exit.  This
tool is build-specific and refuses to patch when the expected prologue differs.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

from capstone import Cs, CS_ARCH_X86, CS_MODE_64

from proc_inspect import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_module,
    find_pid,
    kernel32,
    read_region,
    snapshot,
    winerror,
)
from runtime_capability import load_runtime_profile_file, runtime_profile_hook


PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
THREAD_SUSPEND_RESUME = 0x0002
THREAD_QUERY_INFORMATION = 0x0040
TH32CS_SNAPTHREAD = 0x00000004
MEM_COMMIT = 0x00001000
MEM_RESERVE = 0x00002000
MEM_RELEASE = 0x00008000
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
STILL_ACTIVE = 259

RECORD_SIZE = 0x80
RECORD_COUNT = 4096
RECORD_MASK = RECORD_COUNT - 1
RECORDS_OFFSET = 0x100
RING_SIZE = RECORDS_OFFSET + RECORD_COUNT * RECORD_SIZE
MAGIC = b"GMZZDPS1"

NAME_MAGIC = b"GMZZNAM1"
NAME_RECORD_SIZE = 0x100
NAME_RECORD_COUNT = 1024
NAME_RECORD_MASK = NAME_RECORD_COUNT - 1
NAME_RECORDS_OFFSET = 0x100
NAME_RING_SIZE = NAME_RECORDS_OFFSET + NAME_RECORD_COUNT * NAME_RECORD_SIZE

# CommonComponent::SetBossType.  KAPI components inherit the owning network
# EntityId at +0x58.  BossType is an enum byte at +0x137: 3 is Boss while
# 0xff is the uninitialised/invalid sentinel, so it must not be treated as a
# generic truthy flag.
BOSS_TYPE_MAGIC = b"GMZZBOS1"
BOSS_TYPE_RECORD_SIZE = 0x40
BOSS_TYPE_RECORD_COUNT = 1024
BOSS_TYPE_RECORD_MASK = BOSS_TYPE_RECORD_COUNT - 1
BOSS_TYPE_RECORDS_OFFSET = 0x100
BOSS_TYPE_RING_SIZE = (
    BOSS_TYPE_RECORDS_OFFSET + BOSS_TYPE_RECORD_COUNT * BOSS_TYPE_RECORD_SIZE
)
BOSS_TYPE_ENTITY_ID_OFFSET = 0x58
BOSS_TYPE_FIELD_OFFSET = 0x137
BOSS_TEMPLATE_ID_OFFSET = 0x198
BOSS_TYPE_BOSS_VALUE = 3

# CommonComponent template/application path. Unlike SetBossType, this executes
# when an entity's already-serialized component data is applied, which covers
# dungeon bosses whose BossType flag is initialized directly from a template.
BOSS_INIT_MAGIC = b"GMZZBIN1"
TEMPLATE_ID_MAGIC = b"GMZZTID1"
TEMPLATE_BULK_MAGIC = b"GMZZTBK1"


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32First.restype = wintypes.BOOL
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.restype = wintypes.BOOL
kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenThread.restype = wintypes.HANDLE
kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
kernel32.SuspendThread.restype = wintypes.DWORD
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_size_t,
    wintypes.DWORD,
    wintypes.DWORD,
]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualFreeEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_size_t,
    wintypes.DWORD,
]
kernel32.VirtualFreeEx.restype = wintypes.BOOL
kernel32.VirtualProtectEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_size_t,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.VirtualProtectEx.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
kernel32.FlushInstructionCache.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL


def build_stub(ring: int, resume: int, *, prologue: bytes) -> bytes:
    code = bytearray()
    code += b"\x9c"  # pushfq
    code += b"\x50\x53\x52\x41\x52\x41\x53"  # push rax,rbx,rdx,r10,r11
    code += b"\x49\xba" + struct.pack("<Q", ring)  # mov r10, ring
    code += b"\xb8\x01\x00\x00\x00"  # mov eax, 1
    code += b"\xf0\x49\x0f\xc1\x42\x08"  # lock xadd qword [r10+8], rax
    code += b"\x48\x89\xc3"  # mov rbx, rax (full sequence)
    code += b"\x25" + struct.pack("<I", RECORD_MASK)  # and eax, mask
    code += b"\x48\xc1\xe0\x07"  # shl rax, 7 (record size)
    code += b"\x4d\x8d\x9c\x02" + struct.pack("<I", RECORDS_OFFSET)

    # sequence, thread id, return address, this, and register arguments.
    code += b"\x49\x89\x1b"  # [r11+00] = rbx
    code += b"\x65\x48\x8b\x04\x25\x48\x00\x00\x00"  # rax=TEB.UniqueThread
    code += b"\x49\x89\x43\x10"
    code += b"\x48\x8b\x44\x24\x30\x49\x89\x43\x18"  # return address
    code += b"\x49\x89\x4b\x20"  # this / rcx
    code += b"\x49\x89\x53\x28"  # arg2 / rdx
    code += b"\x4d\x89\x43\x30"  # arg3 / r8
    code += b"\x4d\x89\x4b\x38"  # arg4 / r9

    # With six 8-byte pushes, original stack arg5 starts at current rsp+0x58.
    for stack_disp, record_disp in zip(
        (0x58, 0x60, 0x68, 0x70, 0x78, 0x80),
        (0x40, 0x48, 0x50, 0x58, 0x60, 0x68),
    ):
        if stack_disp <= 0x7F:
            code += b"\x48\x8b\x44\x24" + bytes([stack_disp])
        else:
            code += b"\x48\x8b\x84\x24" + struct.pack("<I", stack_disp)
        code += b"\x49\x89\x43" + bytes([record_disp])

    # Exact Windows UTC time from KUSER_SHARED_DATA.SystemTime.  KSYSTEM_TIME
    # uses high1/low/high2; retry if an update raced our read.
    code += b"\x41\xba\x00\x00\xfe\x7f"  # mov r10d, 0x7ffe0000
    code += b"\x41\x8b\x42\x18"  # retry: mov eax, [r10+0x18] (high1)
    code += b"\x41\x8b\x52\x14"  # mov edx, [r10+0x14] (low)
    code += b"\x41\x3b\x42\x1c"  # cmp eax, [r10+0x1c] (high2)
    code += b"\x75\xf2"  # jne retry
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0"
    code += b"\x49\x89\x43\x08"
    code += b"\x48\xff\xc3\x49\x89\x5b\x70"  # commit = sequence + 1

    # Restore scratch state and execute exactly the 14 displaced bytes.
    code += b"\x41\x5b\x41\x5a\x5a\x5b\x58\x9d"
    code += bytes(prologue)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
    return bytes(code)


def build_patch(stub: int) -> bytes:
    return b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", stub)


def build_absolute_patch(stub: int, length: int) -> bytes:
    jump = build_patch(stub)
    if length < len(jump):
        raise ValueError("absolute jump does not fit in requested patch length")
    return jump + b"\x90" * (length - len(jump))


def build_name_stub(ring: int, resume: int, *, prologue: bytes) -> bytes:
    """Capture (manager, EntityId, FString*) at CacheEntityName entry."""
    code = bytearray()
    code += b"\x9c"  # pushfq
    code += b"\x50\x53\x52\x41\x50\x41\x52\x41\x53"  # rax/rbx/rdx/r8/r10/r11
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"  # lock xadd [r10+8], rax
    code += b"\x48\x89\xc3"  # full sequence in rbx
    code += b"\x25" + struct.pack("<I", NAME_RECORD_MASK)
    code += b"\x48\xc1\xe0\x08"  # record size 0x100
    code += b"\x4d\x8d\x9c\x02" + struct.pack("<I", NAME_RECORDS_OFFSET)
    code += b"\x49\x89\x1b"  # sequence
    code += b"\x49\x89\x4b\x10"  # manager
    code += b"\x49\x89\x53\x18"  # EntityId (passed by value in rdx)
    code += b"\x4d\x89\x43\x20"  # FString pointer
    code += b"\x49\x8b\x10\x49\x89\x53\x28"  # FString.Data
    code += b"\x49\x8b\x40\x08\x49\x89\x43\x30"  # Num/Max
    code += b"\x48\x8b\x44\x24\x38\x49\x89\x43\x38"  # return address

    # KUSER_SHARED_DATA.SystemTime -> FILETIME at +0x08.
    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x41\x8b\x42\x18\x41\x8b\x52\x14\x41\x3b\x42\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0\x49\x89\x43\x08"

    # Copy at most min(FString.Num - 1, 63) UTF-16 code units.  Num includes
    # FString's trailing NUL; bounding the loop avoids reading beyond Data.
    code += b"\x66\x41\xc7\x43\x40\x00\x00"  # record.name[0] = NUL
    code += b"\x49\x8b\x53\x28"  # reload FString.Data after timestamp scratch use
    code += b"\x48\x85\xd2"  # test Data, Data
    string_done_jumps = [len(code)]
    code += b"\x74\x00"
    code += b"\x45\x8b\x53\x30"  # r10d = FString.Num
    code += b"\x41\x83\xfa\x01"  # an empty FString only contains its NUL
    string_done_jumps.append(len(code))
    code += b"\x7e\x00"
    code += b"\x41\xff\xca"  # exclude FString's trailing NUL
    code += b"\x41\x83\xfa\x3f"
    length_ready_jump = len(code)
    code += b"\x76\x00"
    code += b"\x41\xba\x3f\x00\x00\x00"
    length_ready = len(code)
    code[length_ready_jump + 1] = (
        length_ready - (length_ready_jump + 2)
    ) & 0xFF
    code += b"\x33\xc0"  # index = 0
    copy_loop = len(code)
    code += b"\x0f\xb7\x1c\x42"  # ebx = Data[index]
    code += b"\x66\x41\x89\x5c\x43\x40"  # record.name[index] = bx
    code += b"\x66\x41\xc7\x44\x43\x42\x00\x00"  # trailing NUL
    code += b"\x66\x85\xdb"
    string_done_jumps.append(len(code))
    code += b"\x74\x00"
    code += b"\xff\xc0\x41\x3b\xc2"  # ++index; compare with bounded length
    code += b"\x72" + bytes([(copy_loop - (len(code) + 2)) & 0xFF])
    string_done = len(code)
    for jump in string_done_jumps:
        code[jump + 1] = (string_done - (jump + 2)) & 0xFF

    code += b"\x49\x8b\x03\x48\xff\xc0"
    code += b"\x49\x89\x83\xf8\x00\x00\x00"  # commit
    code += b"\x41\x5b\x41\x5a\x41\x58\x5a\x5b\x58\x9d"
    code += bytes(prologue)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
    return bytes(code)


def build_boss_type_stub(
    ring: int,
    resume: int,
    boss_type_global_address: int,
    *,
    prologue: bytes,
) -> bytes:
    """Capture CommonComponent, EntityId, and the requested BossType value."""
    code = bytearray()
    code += b"\x9c"  # pushfq
    code += b"\x50\x53\x52\x41\x52\x41\x53"  # rax/rbx/rdx/r10/r11
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"  # lock xadd [r10+8], rax
    code += b"\x48\x89\xc3"  # full sequence in rbx
    code += b"\x25" + struct.pack("<I", BOSS_TYPE_RECORD_MASK)
    code += b"\x48\xc1\xe0\x06"  # record size 0x40
    code += b"\x4d\x8d\x9c\x02" + struct.pack("<I", BOSS_TYPE_RECORDS_OFFSET)
    code += b"\x49\x89\x1b"  # sequence
    code += b"\x49\x89\x4b\x10"  # CommonComponent
    code += b"\x48\x8b\x81" + struct.pack("<I", BOSS_TYPE_ENTITY_ID_OFFSET)
    code += b"\x49\x89\x43\x18"  # network EntityId
    code += b"\x0f\xb6\xc2\x49\x89\x43\x20"  # requested value
    code += b"\x8b\x81" + struct.pack("<I", BOSS_TEMPLATE_ID_OFFSET)
    code += b"\x49\x89\x43\x28"  # MonsterData template ID
    code += b"\x48\x8b\x44\x24\x30\x49\x89\x43\x30"  # return address

    # KUSER_SHARED_DATA.SystemTime -> FILETIME at +0x08.
    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x41\x8b\x42\x18\x41\x8b\x52\x14\x41\x3b\x42\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0\x49\x89\x43\x08"
    code += b"\x48\xff\xc3\x49\x89\x5b\x38"  # commit

    code += b"\x41\x5b\x41\x5a\x5a\x5b\x58\x9d"
    # The displaced prologue ends in a RIP-relative load. Re-encode that load
    # with its absolute address because the stub lives outside the module.
    code += bytes(prologue)[:10]
    code += b"\x48\xa1" + struct.pack("<Q", boss_type_global_address)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
    return bytes(code)


def build_boss_init_stub(
    ring: int, resume: int, *, prologue: bytes
) -> bytes:
    """Capture BossType while serialized CommonComponent data is applied."""
    code = bytearray()
    code += b"\x9c"  # pushfq
    code += b"\x50\x53\x52\x41\x52\x41\x53"  # rax/rbx/rdx/r10/r11
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"  # lock xadd [r10+8], rax
    code += b"\x48\x89\xc3"  # full sequence in rbx
    code += b"\x25" + struct.pack("<I", BOSS_TYPE_RECORD_MASK)
    code += b"\x48\xc1\xe0\x06"  # record size 0x40
    code += b"\x4d\x8d\x9c\x02" + struct.pack("<I", BOSS_TYPE_RECORDS_OFFSET)
    code += b"\x49\x89\x1b"  # sequence
    code += b"\x49\x89\x7b\x10"  # CommonComponent (rdi)
    code += b"\x48\x8b\x87" + struct.pack("<I", BOSS_TYPE_ENTITY_ID_OFFSET)
    code += b"\x49\x89\x43\x18"  # network EntityId
    code += b"\x48\x8b\x44\x24\x20"  # saved original rax
    code += b"\x0f\xb6\xc0\x49\x89\x43\x20"  # requested value from al
    code += b"\x8b\x87" + struct.pack("<I", BOSS_TEMPLATE_ID_OFFSET)
    code += b"\x49\x89\x43\x28"  # MonsterData template ID
    code += b"\x48\x8b\x44\x24\x30\x49\x89\x43\x30"  # return address

    # KUSER_SHARED_DATA.SystemTime -> FILETIME at +0x08.
    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x41\x8b\x42\x18\x41\x8b\x52\x14\x41\x3b\x42\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0\x49\x89\x43\x08"
    code += b"\x48\xff\xc3\x49\x89\x5b\x38"  # commit

    code += b"\x41\x5b\x41\x5a\x5a\x5b\x58\x9d"
    code += bytes(prologue)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
    return bytes(code)


def build_template_id_stub(
    ring: int,
    resume: int,
    template_global_address: int,
    *,
    prologue: bytes,
) -> bytes:
    """Capture a single CommonComponent TemplateId assignment.

    The setter receives ``(CommonComponent*, uint32 TemplateId)`` in RCX/RDX.
    Reading BossType from the same component at this point complements the
    BossType setter: whichever field is assigned second produces a complete
    identity tuple without guessing from names, HP, or nearby entities.
    """

    code = bytearray()
    code += b"\x9c"
    code += b"\x50\x53\x52\x41\x52\x41\x53"
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"
    code += b"\x48\x89\xc3"
    code += b"\x25" + struct.pack("<I", BOSS_TYPE_RECORD_MASK)
    code += b"\x48\xc1\xe0\x06"
    code += b"\x4d\x8d\x9c\x02" + struct.pack(
        "<I", BOSS_TYPE_RECORDS_OFFSET
    )
    code += b"\x49\x89\x1b"
    code += b"\x49\x89\x4b\x10"
    code += b"\x48\x8b\x81" + struct.pack("<I", BOSS_TYPE_ENTITY_ID_OFFSET)
    code += b"\x49\x89\x43\x18"
    code += b"\x0f\xb6\x81" + struct.pack("<I", BOSS_TYPE_FIELD_OFFSET)
    code += b"\x49\x89\x43\x20"
    code += b"\x8b\xc2\x49\x89\x43\x28"
    code += b"\x48\x8b\x44\x24\x30\x49\x89\x43\x30"

    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x41\x8b\x42\x18\x41\x8b\x52\x14\x41\x3b\x42\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0\x49\x89\x43\x08"
    code += b"\x48\xff\xc3\x49\x89\x5b\x38"

    code += b"\x41\x5b\x41\x5a\x5a\x5b\x58\x9d"
    # The displaced setter prologue ends in a RIP-relative global load.
    code += bytes(prologue)[:10]
    code += b"\x48\xa1" + struct.pack("<Q", template_global_address)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
    return bytes(code)


def build_template_bulk_stub(
    ring: int, resume: int, *, prologue: bytes
) -> bytes:
    """Capture the generated bulk CommonComponent assignment arguments.

    The current build passes BossType in R8B and TemplateId as argument nine
    at entry ``[rsp+0x48]``.  Six saved qwords move that stack argument to
    ``[rsp+0x78]`` while the trace runs.
    """

    code = bytearray()
    code += b"\x9c"
    code += b"\x50\x53\x52\x41\x52\x41\x53"
    code += b"\x49\xba" + struct.pack("<Q", ring)
    code += b"\xb8\x01\x00\x00\x00"
    code += b"\xf0\x49\x0f\xc1\x42\x08"
    code += b"\x48\x89\xc3"
    code += b"\x25" + struct.pack("<I", BOSS_TYPE_RECORD_MASK)
    code += b"\x48\xc1\xe0\x06"
    code += b"\x4d\x8d\x9c\x02" + struct.pack(
        "<I", BOSS_TYPE_RECORDS_OFFSET
    )
    code += b"\x49\x89\x1b"
    code += b"\x49\x89\x4b\x10"
    code += b"\x48\x8b\x81" + struct.pack("<I", BOSS_TYPE_ENTITY_ID_OFFSET)
    code += b"\x49\x89\x43\x18"
    code += b"\x41\x0f\xb6\xc0\x49\x89\x43\x20"
    code += b"\x8b\x44\x24\x78\x49\x89\x43\x28"
    code += b"\x48\x8b\x44\x24\x30\x49\x89\x43\x30"

    code += b"\x41\xba\x00\x00\xfe\x7f"
    retry = len(code)
    code += b"\x41\x8b\x42\x18\x41\x8b\x52\x14\x41\x3b\x42\x1c"
    code += b"\x75" + bytes([(retry - (len(code) + 2)) & 0xFF])
    code += b"\x48\xc1\xe0\x20\x48\x09\xd0\x49\x89\x43\x08"
    code += b"\x48\xff\xc3\x49\x89\x5b\x38"

    code += b"\x41\x5b\x41\x5a\x5a\x5b\x58\x9d"
    code += bytes(prologue)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
    return bytes(code)


def parse_name_record(data: bytes, expected_sequence: int) -> dict | None:
    if len(data) != NAME_RECORD_SIZE:
        return None
    sequence, filetime_100ns, manager, entity_id, string_object, string_data, sizes, return_address = struct.unpack_from(
        "<8Q", data
    )
    commit = struct.unpack_from("<Q", data, 0xF8)[0]
    if sequence != expected_sequence or commit != expected_sequence + 1:
        return None
    num = ctypes.c_int32(sizes & 0xFFFFFFFF).value
    maximum = ctypes.c_int32(sizes >> 32).value
    copied = data[0x40:0xC0]
    for terminator in range(0, len(copied), 2):
        if copied[terminator : terminator + 2] == b"\x00\x00":
            copied = copied[:terminator]
            break
    try:
        name = copied.decode("utf-16le", "replace").strip("\x00")
    except UnicodeError:
        name = ""
    event_time = dt.datetime.fromtimestamp(
        (filetime_100ns - 116_444_736_000_000_000) / 10_000_000,
        tz=dt.timezone.utc,
    ).astimezone()
    return {
        "event_time": event_time.isoformat(timespec="microseconds"),
        "filetime_100ns": filetime_100ns,
        "sequence": sequence,
        "function": "KAPI_DataCache_CacheEntityName",
        "manager": manager,
        "entity_id": entity_id,
        "string_object": string_object,
        "string_data": string_data,
        "string_num": num,
        "string_max": maximum,
        "name": name,
        "return_address": f"0x{return_address:016x}",
    }


def parse_boss_type_record(
    data: bytes,
    expected_sequence: int,
    function: str = "KAPI_Common_SetBossType",
) -> dict | None:
    if len(data) != BOSS_TYPE_RECORD_SIZE:
        return None
    (
        sequence,
        filetime_100ns,
        component,
        entity_id,
        requested_value,
        template_id,
        return_address,
        commit,
    ) = struct.unpack_from("<8Q", data)
    if sequence != expected_sequence or commit != expected_sequence + 1:
        return None
    event_time = dt.datetime.fromtimestamp(
        (filetime_100ns - 116_444_736_000_000_000) / 10_000_000,
        tz=dt.timezone.utc,
    ).astimezone()
    return {
        "event_time": event_time.isoformat(timespec="microseconds"),
        "filetime_100ns": filetime_100ns,
        "sequence": sequence,
        "function": function,
        "component": component,
        "entity_id": entity_id,
        "boss_type": requested_value & 0xFF,
        "template_id": template_id & 0xFFFFFFFF,
        "return_address": f"0x{return_address:016x}",
    }


def disassemble(code: bytes, address: int = 0) -> str:
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    return "\n".join(
        f"0x{insn.address:x}: {bytes(insn.bytes).hex(' '):<28} {insn.mnemonic} {insn.op_str}"
        for insn in md.disasm(code, address)
    )


def write_memory(process, address: int, data: bytes) -> None:
    buffer = ctypes.create_string_buffer(data)
    done = ctypes.c_size_t()
    if not kernel32.WriteProcessMemory(
        process,
        ctypes.c_void_p(address),
        buffer,
        len(data),
        ctypes.byref(done),
    ) or done.value != len(data):
        raise winerror(f"WriteProcessMemory(0x{address:x}, {len(data)})")


def write_code(process, address: int, data: bytes) -> None:
    old = wintypes.DWORD()
    if not kernel32.VirtualProtectEx(
        process,
        ctypes.c_void_p(address),
        len(data),
        PAGE_EXECUTE_READWRITE,
        ctypes.byref(old),
    ):
        raise winerror("VirtualProtectEx(PAGE_EXECUTE_READWRITE)")
    try:
        write_memory(process, address, data)
        if not kernel32.FlushInstructionCache(process, ctypes.c_void_p(address), len(data)):
            raise winerror("FlushInstructionCache")
    finally:
        ignored = wintypes.DWORD()
        kernel32.VirtualProtectEx(
            process,
            ctypes.c_void_p(address),
            len(data),
            old.value,
            ctypes.byref(ignored),
        )


def thread_ids(pid: int) -> list[int]:
    result: list[int] = []
    snap = snapshot(TH32CS_SNAPTHREAD)
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Thread32First(snap, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                result.append(int(entry.th32ThreadID))
            ok = kernel32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return result


def suspend_process(pid: int) -> list[int]:
    suspended: list[int] = []
    access = THREAD_SUSPEND_RESUME | THREAD_QUERY_INFORMATION
    try:
        for tid in thread_ids(pid):
            thread = kernel32.OpenThread(access, False, tid)
            if not thread:
                raise winerror(f"OpenThread({tid})")
            previous = kernel32.SuspendThread(thread)
            if previous == 0xFFFFFFFF:
                kernel32.CloseHandle(thread)
                raise winerror(f"SuspendThread({tid})")
            suspended.append(int(thread))
        return suspended
    except Exception:
        resume_threads(suspended)
        raise


def resume_threads(handles: list[int]) -> None:
    for thread in reversed(handles):
        kernel32.ResumeThread(thread)
        kernel32.CloseHandle(thread)
    handles.clear()


def process_alive(process) -> bool:
    code = wintypes.DWORD()
    return bool(kernel32.GetExitCodeProcess(process, ctypes.byref(code))) and code.value == STILL_ACTIVE


def signed32(value: int) -> int:
    return ctypes.c_int32(value & 0xFFFFFFFF).value


def parse_record(data: bytes, expected_sequence: int) -> dict | None:
    if len(data) != RECORD_SIZE:
        return None
    values = struct.unpack_from("<15Q", data)
    sequence, filetime_100ns, tid, return_address, this, *args, commit = values
    if sequence != expected_sequence or commit != expected_sequence + 1:
        return None
    event_time = dt.datetime.fromtimestamp(
        (filetime_100ns - 116_444_736_000_000_000) / 10_000_000,
        tz=dt.timezone.utc,
    ).astimezone()
    return {
        "event_time": event_time.isoformat(timespec="microseconds"),
        "filetime_100ns": filetime_100ns,
        "sequence": sequence,
        "tid": tid,
        "function": "KAPI_HandleDamageSyncV2",
        "return_address": f"0x{return_address:016x}",
        "this": f"0x{this:016x}",
        "damage_manager": this,
        "arg2_u64": args[0],
        "arg3_u64": args[1],
        # HandleDamageSyncV2 receives the damage source first and the victim
        # second.  The live local-player chain confirms arg2 is the attacker:
        # during a player attack local_player_id == arg2, while arg3 is the
        # enemy entity.  Keeping the normalized names here makes every caller,
        # persisted log, team inference, and skill aggregation agree.
        "attacker_id": args[0],
        "target_id": args[1],
        "arg4_u64": args[2],
        "arg5_i32": signed32(args[3]),
        "arg6_i32": signed32(args[4]),
        "arg7_i32": signed32(args[5]),
        "arg8_i32": signed32(args[6]),
        "arg9_i32": signed32(args[7]),
        "arg10_bool": bool(args[8] & 0xFF),
        "raw_damage": signed32(args[5]),
        "damage": signed32(args[7]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--module", default="C7-Win64-Shipping.exe")
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(__file__).resolve().with_name("runtime-profile.dev.json"),
    )
    parser.add_argument("--rva", type=lambda x: int(x, 0))
    parser.add_argument("--output", type=Path, default=Path("damage_inline.jsonl"))
    parser.add_argument("--hits", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--show-stub", action="store_true")
    args = parser.parse_args()

    profile = load_runtime_profile_file(args.profile)
    damage_hook = runtime_profile_hook(profile, "damage")
    damage_rva = int(args.rva or damage_hook["rva"])
    damage_prologue = bytes(damage_hook["prologue"])
    damage_signature = bytes(damage_hook["signature"])

    if args.show_stub:
        stub = build_stub(
            0x123456780000,
            0x140001234,
            prologue=damage_prologue,
        )
        print(f"stub size: {len(stub)} bytes")
        print(disassemble(stub, 0x123456789000))
        return 0

    pid = args.pid or find_pid(args.process)
    base, module_size, module_path = find_module(pid, args.module)
    target = base + damage_rva
    if damage_rva + len(damage_signature) > module_size:
        raise SystemExit("target RVA is outside module")
    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE
    process = kernel32.OpenProcess(access, False, pid)
    if not process:
        raise winerror("OpenProcess")

    ring = 0
    stub_address = 0
    installed = False
    suspended: list[int] = []
    hit_count = 0
    try:
        actual = read_region(process, target, len(damage_signature))
        if actual != damage_signature:
            got = actual.hex(" ") if actual else "unreadable"
            raise RuntimeError(
                f"version/signature mismatch at RVA 0x{damage_rva:x}\n"
                f"expected: {damage_signature.hex(' ')}\nactual:   {got}"
            )

        ring = int(
            kernel32.VirtualAllocEx(
                process, None, RING_SIZE, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
            )
            or 0
        )
        if not ring:
            raise winerror("VirtualAllocEx(ring)")
        stub_address = int(
            kernel32.VirtualAllocEx(
                process, None, 0x1000, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE
            )
            or 0
        )
        if not stub_address:
            raise winerror("VirtualAllocEx(stub)")

        header = struct.pack("<8sQQQQ", MAGIC, 0, RECORD_COUNT, RECORD_SIZE, target)
        write_memory(process, ring, header)
        stub = build_stub(
            ring,
            target + len(damage_prologue),
            prologue=damage_prologue,
        )
        write_memory(process, stub_address, stub)
        kernel32.FlushInstructionCache(process, ctypes.c_void_p(stub_address), len(stub))
        patch = build_patch(stub_address)
        if len(patch) != len(damage_prologue):
            raise AssertionError("absolute jump must exactly cover displaced prologue")

        suspended = suspend_process(pid)
        try:
            write_code(process, target, patch)
            installed = True
        finally:
            resume_threads(suspended)
        if read_region(process, target, len(patch)) != patch:
            raise RuntimeError("hook verification failed")

        print(
            f"READY pid={pid} module={module_path} target=0x{target:x} "
            f"stub=0x{stub_address:x} ring=0x{ring:x} output={args.output}",
            flush=True,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(1.0, args.timeout)
        next_sequence = 0
        with args.output.open("a", encoding="utf-8", buffering=1) as output:
            while time.monotonic() < deadline and process_alive(process):
                header_live = read_region(process, ring, 0x20)
                if not header_live or header_live[:8] != MAGIC:
                    raise RuntimeError("ring buffer became unreadable or corrupt")
                write_index = struct.unpack_from("<Q", header_live, 8)[0]
                if write_index - next_sequence > RECORD_COUNT:
                    next_sequence = write_index - RECORD_COUNT
                progressed = False
                while next_sequence < write_index:
                    slot = next_sequence & RECORD_MASK
                    raw = read_region(
                        process, ring + RECORDS_OFFSET + slot * RECORD_SIZE, RECORD_SIZE
                    )
                    record = parse_record(raw or b"", next_sequence)
                    if record is None:
                        break
                    line = json.dumps(record, ensure_ascii=False)
                    output.write(line + "\n")
                    print(line, flush=True)
                    next_sequence += 1
                    hit_count += 1
                    progressed = True
                    if args.hits and hit_count >= args.hits:
                        break
                if args.hits and hit_count >= args.hits:
                    break
                if not progressed:
                    time.sleep(0.01)
    finally:
        if suspended:
            resume_threads(suspended)
        if installed and process_alive(process):
            try:
                suspended = suspend_process(pid)
                try:
                    write_code(process, target, damage_prologue)
                    installed = False
                finally:
                    resume_threads(suspended)
            except Exception as exc:
                print(f"warning: failed to restore hook: {exc}", file=sys.stderr, flush=True)
        if not installed and process_alive(process):
            if stub_address:
                kernel32.VirtualFreeEx(process, ctypes.c_void_p(stub_address), 0, MEM_RELEASE)
            if ring:
                kernel32.VirtualFreeEx(process, ctypes.c_void_p(ring), 0, MEM_RELEASE)
        kernel32.CloseHandle(process)
    print(f"DONE hits={hit_count}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
