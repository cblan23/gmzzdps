#!/usr/bin/env python3
"""Read-only runtime inspector for the C7 Win64 module.

The tool never writes to or injects into the target.  It reads committed pages
with ReadProcessMemory, then locates strings, absolute pointers and common
x64 RIP-relative references in the live (possibly unpacked) image.
"""

from __future__ import annotations

import argparse
import ctypes
import re
import struct
import sys
from ctypes import wintypes


if sys.platform != "win32":
    raise SystemExit("Windows only")


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * MAX_PATH),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32FirstW.restype = wintypes.BOOL
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def winerror(label: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{label}: {ctypes.FormatError(code).strip()}")


def snapshot(flags: int, pid: int = 0):
    handle = kernel32.CreateToolhelp32Snapshot(flags, pid)
    if handle == INVALID_HANDLE_VALUE:
        raise winerror("CreateToolhelp32Snapshot")
    return handle


def find_pid(name: str) -> int:
    wanted = name.casefold()
    wanted_stem = wanted.removesuffix(".exe")
    snap = snapshot(TH32CS_SNAPPROCESS)
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            actual = entry.szExeFile.casefold()
            if actual == wanted or actual.removesuffix(".exe") == wanted_stem:
                return entry.th32ProcessID
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    raise RuntimeError(f"process not found: {name}")


def find_module(pid: int, name: str) -> tuple[int, int, str]:
    wanted = name.casefold()
    snap = snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szModule.casefold() == wanted:
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value
                return int(base), int(entry.modBaseSize), entry.szExePath
            ok = kernel32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    raise RuntimeError(f"module not found in PID {pid}: {name}")


def read_region(handle, address: int, size: int) -> bytes | None:
    buffer = ctypes.create_string_buffer(size)
    done = ctypes.c_size_t()
    ok = kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(done)
    )
    if not ok and not done.value:
        return None
    return buffer.raw[: done.value]


def read_live_image(handle, base: int, size: int) -> tuple[bytearray, list[tuple[int, int, int]]]:
    image = bytearray(size)
    regions: list[tuple[int, int, int]] = []
    cursor = base
    end = base + size
    while cursor < end:
        mbi = MEMORY_BASIC_INFORMATION()
        queried = kernel32.VirtualQueryEx(
            handle, ctypes.c_void_p(cursor), ctypes.byref(mbi), ctypes.sizeof(mbi)
        )
        if not queried:
            break
        region_base = int(mbi.BaseAddress or 0)
        region_end = region_base + int(mbi.RegionSize)
        lo = max(cursor, region_base)
        hi = min(end, region_end)
        readable = (
            mbi.State == MEM_COMMIT
            and not (mbi.Protect & PAGE_GUARD)
            and (mbi.Protect & 0xFF) != PAGE_NOACCESS
        )
        if readable and hi > lo:
            # Keep individual reads modest; protected processes sometimes reject
            # a large request even when all constituent pages are readable.
            chunk_at = lo
            while chunk_at < hi:
                chunk_size = min(1024 * 1024, hi - chunk_at)
                block = read_region(handle, chunk_at, chunk_size)
                if block:
                    offset = chunk_at - base
                    image[offset : offset + len(block)] = block
                    regions.append((offset, len(block), int(mbi.Protect)))
                chunk_at += chunk_size
        cursor = max(cursor + 0x1000, region_end)
    return image, regions


def occurrences(data: bytes | bytearray, needle: bytes):
    at = 0
    while True:
        at = data.find(needle, at)
        if at < 0:
            return
        yield at
        at += 1


RIP_RE = re.compile(
    rb"(?:\x66)?(?:[\x40-\x4f])?[\x03\x0b\x23\x2b\x33\x3b\x63\x8b\x89\x8d]"
    rb"[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}",
    re.DOTALL,
)


def rip_xrefs(image: bytes | bytearray, target_rva: int):
    for match in RIP_RE.finditer(image):
        disp = struct.unpack_from("<i", image, match.end() - 4)[0]
        if match.end() + disp == target_rva:
            yield match.start(), match.end()


def hex_context(data: bytes | bytearray, offset: int, radius: int = 32) -> str:
    lo = max(0, offset - radius)
    hi = min(len(data), offset + radius)
    return bytes(data[lo:hi]).hex(" ")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--module", default="C7-Win64-Shipping.exe")
    parser.add_argument("strings", nargs="+", default=["KAPI_HandleDamageSyncV2"])
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    base, size, path = find_module(pid, args.module)
    print(f"PID {pid}, module={path}")
    print(f"base=0x{base:x}, size=0x{size:x}")
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise winerror("OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ)")
    try:
        image, regions = read_live_image(handle, base, size)
    finally:
        kernel32.CloseHandle(handle)
    readable_bytes = sum(length for _, length, _ in regions)
    print(f"read {readable_bytes:,}/{size:,} bytes across {len(regions)} chunks")
    if not readable_bytes:
        raise RuntimeError("no module pages could be read")

    for text in args.strings:
        found_any = False
        for encoding, needle in (("ascii", text.encode()), ("utf16", text.encode("utf-16le"))):
            for rva in occurrences(image, needle):
                found_any = True
                print(f"string {text!r} [{encoding}] RVA=0x{rva:x}, VA=0x{base+rva:x}")
                print(f"  bytes: {hex_context(image, rva)}")
                absolute = struct.pack("<Q", base + rva)
                pointers = list(occurrences(image, absolute))
                print(
                    "  absolute pointers: "
                    + (", ".join(f"RVA 0x{x:x}" for x in pointers[:32]) or "none")
                )
                xrefs = list(rip_xrefs(image, rva))
                print(
                    "  RIP references: "
                    + (", ".join(f"RVA 0x{x:x}" for x, _ in xrefs[:32]) or "none")
                )
        if not found_any:
            print(f"string not found in readable module pages: {text!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
