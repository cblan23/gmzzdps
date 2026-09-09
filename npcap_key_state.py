#!/usr/bin/env python3
"""Locate and read the game's inbound RC4 state without modifying the game.

Npcap exposes the encrypted UDP stream.  The session key schedule exists only
inside the connected client, so this module opens the process with query/read
rights and copies the current RC4 state.  It never requests write, operation,
thread, or injection permissions.
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import time
from dataclasses import dataclass
from typing import Iterable

import proc_inspect
from npcap_rc4_decode import Rc4State


GAME_MODULE = "C7-Win64-Shipping.exe"
# Verified vtable for the connection cipher object in the supported 2026-09-09
# client.  ASLR is handled by adding this RVA to the live module base.
RC4_CRYPTOR_OWNER_VTABLE_RVA = 0x0DB589F8
CRYPTOR_POINTERS = struct.Struct("<QQQ")
RC4_STATE_STRUCT = struct.Struct("<II256I")
EVP_ENCRYPT_OFFSET = 0x10
EVP_CIPHER_DATA_OFFSET = 0x70
MEM_PRIVATE = 0x20000
WRITABLE_PAGE_TYPES = frozenset({0x04, 0x08, 0x40, 0x80})
MAX_USER_ADDRESS = 0x00007FFFFFFF0000
SCAN_CHUNK_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Rc4Anchor:
    cryptor_address: int
    read_before_epoch: float
    read_after_epoch: float
    encrypt: Rc4State
    decrypt: Rc4State

    @property
    def midpoint_epoch(self) -> float:
        return (self.read_before_epoch + self.read_after_epoch) / 2.0


@dataclass(frozen=True)
class LocateDiagnostics:
    regions_read: int
    bytes_read: int
    pointer_hits: int
    elapsed_seconds: float


def _read_exact(process, address: int, size: int) -> bytes:
    value = proc_inspect.read_region(process, int(address), int(size))
    if value is None or len(value) != size:
        raise RuntimeError(
            f"read failed at 0x{int(address):x}: expected {size}, "
            f"got {0 if value is None else len(value)}"
        )
    return value


def _read_pointer(process, address: int) -> int:
    return struct.unpack("<Q", _read_exact(process, address, 8))[0]


def _read_rc4(process, address: int) -> Rc4State:
    values = RC4_STATE_STRUCT.unpack(
        _read_exact(process, address, RC4_STATE_STRUCT.size)
    )
    return Rc4State(values[0], values[1], list(values[2:]))


class Rc4StateReader:
    def __init__(self, pid: int, cryptor_address: int, owner_pointer: int):
        self.pid = int(pid)
        self.cryptor_address = int(cryptor_address)
        self.owner_pointer = int(owner_pointer)
        access = (
            proc_inspect.PROCESS_QUERY_INFORMATION
            | proc_inspect.PROCESS_VM_READ
        )
        self.process = proc_inspect.kernel32.OpenProcess(access, False, self.pid)
        if not self.process:
            raise proc_inspect.winerror(
                "OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ)"
            )
        try:
            self.snapshot()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.process:
            proc_inspect.kernel32.CloseHandle(self.process)
            self.process = None

    def snapshot(self) -> Rc4Anchor:
        before = time.time()
        owner, encrypt_ctx, decrypt_ctx = CRYPTOR_POINTERS.unpack(
            _read_exact(
                self.process, self.cryptor_address, CRYPTOR_POINTERS.size
            )
        )
        if owner != self.owner_pointer or not encrypt_ctx or not decrypt_ctx:
            raise RuntimeError("connection cryptor identity changed")
        encrypt_flag = struct.unpack(
            "<I", _read_exact(self.process, encrypt_ctx + EVP_ENCRYPT_OFFSET, 4)
        )[0]
        decrypt_flag = struct.unpack(
            "<I", _read_exact(self.process, decrypt_ctx + EVP_ENCRYPT_OFFSET, 4)
        )[0]
        if encrypt_flag != 1 or decrypt_flag != 0:
            raise RuntimeError(
                f"unexpected EVP directions: {encrypt_flag}/{decrypt_flag}"
            )
        encrypt_state_address = _read_pointer(
            self.process, encrypt_ctx + EVP_CIPHER_DATA_OFFSET
        )
        decrypt_state_address = _read_pointer(
            self.process, decrypt_ctx + EVP_CIPHER_DATA_OFFSET
        )
        encrypt = _read_rc4(self.process, encrypt_state_address)
        decrypt = _read_rc4(self.process, decrypt_state_address)
        after = time.time()
        return Rc4Anchor(
            cryptor_address=self.cryptor_address,
            read_before_epoch=before,
            read_after_epoch=after,
            encrypt=encrypt,
            decrypt=decrypt,
        )


def _memory_regions(process) -> Iterable[proc_inspect.MEMORY_BASIC_INFORMATION]:
    cursor = 0x10000
    while cursor < MAX_USER_ADDRESS:
        mbi = proc_inspect.MEMORY_BASIC_INFORMATION()
        result = proc_inspect.kernel32.VirtualQueryEx(
            process,
            ctypes.c_void_p(cursor),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi),
        )
        if not result:
            break
        base = int(mbi.BaseAddress or cursor)
        size = max(0x1000, int(mbi.RegionSize or 0x1000))
        yield mbi
        cursor = max(cursor + 0x1000, base + size)


def _candidate_addresses(
    process,
    owner_pointer: int,
    *,
    module_base: int,
    maximum_scan_bytes: int,
) -> tuple[list[int], LocateDiagnostics]:
    started = time.monotonic()
    needle = struct.pack("<Q", owner_pointer)
    regions = []
    for mbi in _memory_regions(process):
        base = int(mbi.BaseAddress or 0)
        size = int(mbi.RegionSize or 0)
        protection = int(mbi.Protect) & 0xFF
        if (
            mbi.State != proc_inspect.MEM_COMMIT
            or mbi.Type != MEM_PRIVATE
            or protection not in WRITABLE_PAGE_TYPES
            or size <= 0
        ):
            continue
        # The observed connection allocations live immediately below the main
        # image.  Searching this band first avoids walking multi-gigabyte asset
        # heaps while retaining a complete fallback ordering.
        # Connection objects live in ordinary small heap regions.  Large
        # texture/asset arenas account for most of the process working set and
        # are both unlikely locations and expensive to copy, so visit small
        # regions first.  The two distance anchors cover both allocation bands
        # observed across clean game launches without making either address a
        # correctness requirement.
        distance = min(abs(base - 0x35000000), abs(base - 0x10D000000))
        regions.append((size > 16 * 1024 * 1024, distance, base, size))
    regions.sort()

    candidates: list[int] = []
    seen_candidates: set[int] = set()
    bytes_read = 0
    regions_read = 0
    stop = False
    for _not_preferred, _distance, base, size in regions:
        if stop:
            break
        offset = 0
        overlap = b""
        while offset < size:
            length = min(SCAN_CHUNK_BYTES, size - offset)
            if bytes_read + length > maximum_scan_bytes:
                stop = True
                break
            block = proc_inspect.read_region(process, base + offset, length)
            bytes_read += length
            if block:
                haystack = overlap + block
                origin = base + offset - len(overlap)
                position = haystack.find(needle)
                while position >= 0:
                    address = origin + position
                    if address % 8 == 0 and address not in seen_candidates:
                        seen_candidates.add(address)
                        try:
                            owner, encrypt_ctx, decrypt_ctx = CRYPTOR_POINTERS.unpack(
                                _read_exact(process, address, CRYPTOR_POINTERS.size)
                            )
                            if owner != owner_pointer or not encrypt_ctx or not decrypt_ctx:
                                raise ValueError("not a live cryptor")
                            encrypt_flag = struct.unpack(
                                "<I",
                                _read_exact(
                                    process,
                                    encrypt_ctx + EVP_ENCRYPT_OFFSET,
                                    4,
                                ),
                            )[0]
                            decrypt_flag = struct.unpack(
                                "<I",
                                _read_exact(
                                    process,
                                    decrypt_ctx + EVP_ENCRYPT_OFFSET,
                                    4,
                                ),
                            )[0]
                            if encrypt_flag != 1 or decrypt_flag != 0:
                                raise ValueError("not an RC4 EVP pair")
                            _read_rc4(
                                process,
                                _read_pointer(
                                    process,
                                    encrypt_ctx + EVP_CIPHER_DATA_OFFSET,
                                ),
                            )
                            _read_rc4(
                                process,
                                _read_pointer(
                                    process,
                                    decrypt_ctx + EVP_CIPHER_DATA_OFFSET,
                                ),
                            )
                        except (OSError, RuntimeError, ValueError, struct.error):
                            pass
                        else:
                            candidates.append(address)
                            stop = True
                            break
                    position = haystack.find(needle, position + 1)
                overlap = haystack[-(CRYPTOR_POINTERS.size - 1) :]
            else:
                overlap = b""
            offset += length
        regions_read += 1
    diagnostics = LocateDiagnostics(
        regions_read=regions_read,
        bytes_read=bytes_read,
        pointer_hits=len(candidates),
        elapsed_seconds=time.monotonic() - started,
    )
    return candidates, diagnostics


def locate_readers(
    pid: int,
    *,
    module_name: str = GAME_MODULE,
    owner_vtable_rva: int = RC4_CRYPTOR_OWNER_VTABLE_RVA,
    maximum_scan_bytes: int = 2 * 1024 * 1024 * 1024,
) -> tuple[list[Rc4StateReader], LocateDiagnostics]:
    module_base, _module_size, _path = proc_inspect.find_module(
        int(pid), module_name
    )
    owner_pointer = module_base + int(owner_vtable_rva)
    access = proc_inspect.PROCESS_QUERY_INFORMATION | proc_inspect.PROCESS_VM_READ
    process = proc_inspect.kernel32.OpenProcess(access, False, int(pid))
    if not process:
        raise proc_inspect.winerror("OpenProcess(RC4 locator)")
    try:
        candidates, diagnostics = _candidate_addresses(
            process,
            owner_pointer,
            module_base=module_base,
            maximum_scan_bytes=max(64 * 1024 * 1024, int(maximum_scan_bytes)),
        )
    finally:
        proc_inspect.kernel32.CloseHandle(process)

    readers: list[Rc4StateReader] = []
    for address in candidates:
        try:
            readers.append(Rc4StateReader(pid, address, owner_pointer))
        except (OSError, RuntimeError, ValueError, struct.error):
            continue
    return readers, diagnostics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default=GAME_MODULE)
    args = parser.parse_args()
    pid = args.pid or proc_inspect.find_pid(args.process)
    readers, diagnostics = locate_readers(pid, module_name=args.process)
    try:
        print(
            f"pid={pid} readers={len(readers)} regions={diagnostics.regions_read} "
            f"bytes={diagnostics.bytes_read} elapsed={diagnostics.elapsed_seconds:.3f}s"
        )
        for reader in readers:
            anchor = reader.snapshot()
            print(
                f"cryptor=0x{reader.cryptor_address:x} "
                f"decrypt={anchor.decrypt.x},{anchor.decrypt.y}"
            )
    finally:
        for reader in readers:
            reader.close()
    return 0 if readers else 2


if __name__ == "__main__":
    raise SystemExit(main())
