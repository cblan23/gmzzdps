#!/usr/bin/env python3
"""Restore the inbound Zstd stream from read-only client state.

Npcap starts at the current point of an already connected UDP stream.  The
client's Zstd decoder therefore owns up to one window of history plus entropy
tables that were established before capture began.  This module locates the
decoder paired with the already verified RC4 object, copies that state with
PROCESS_VM_READ, and recreates it in a private local decoder.  It never writes
to the game process or executes code inside it.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import npcap_key_state
import proc_inspect


GAME_MODULE = "C7-Win64-Shipping.exe"
NATIVE_LIBRARY_NAME = "npcap_zstd_restore.dll"

# Verified against the supported client.  The codec is stored immediately
# after its RC4 peer in the owning connection object.
ZSTD_CODEC_VTABLE_RVA = 0x0DB75398
CODEC_DCTX_POINTER_OFFSET = 0x28
CONNECTION_CODEC_POINTER_OFFSET = 0x08

# Zstd 1.5.6 and 1.5.7 use this identical layout in the supported build.
DCTX_CONTEXT_SIZE = 0x176F0
DCTX_PREVIOUS_DST_END_OFFSET = 0x74C0
DCTX_PREFIX_START_OFFSET = 0x74C8
DCTX_VIRTUAL_START_OFFSET = 0x74D0
DCTX_DICT_END_OFFSET = 0x74D8
DCTX_FRAME_WINDOW_SIZE_OFFSET = 0x74F0
DCTX_FRAME_BLOCK_SIZE_OFFSET = 0x74F8
MAX_ZSTD_WINDOW_SIZE = 128 * 1024 * 1024
MAX_ZSTD_BLOCK_SIZE = 128 * 1024
SCAN_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CONNECTION_SCAN_BYTES = 2 * 1024 * 1024 * 1024
SMALL_CONNECTION_REGION_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ZstdLocateDiagnostics:
    regions_read: int
    bytes_read: int
    pointer_hits: int
    elapsed_seconds: float


@dataclass(frozen=True)
class ZstdSnapshot:
    codec_address: int
    context_address: int
    read_before_epoch: float
    read_after_epoch: float
    context: bytes
    history: bytes
    ll_table: bytes
    ml_table: bytes
    of_table: bytes
    huf_table: bytes

    @property
    def fingerprint(self) -> bytes:
        digest = hashlib.sha256()
        for value in (
            self.context,
            self.history,
            self.ll_table,
            self.ml_table,
            self.of_table,
            self.huf_table,
        ):
            digest.update(len(value).to_bytes(8, "little"))
            digest.update(value)
        return digest.digest()


def _read_exact(process, address: int, size: int) -> bytes:
    value = proc_inspect.read_region(process, int(address), int(size))
    if value is None or len(value) != size:
        raise RuntimeError(
            f"read failed at 0x{int(address):x}: expected {int(size)}, "
            f"got {0 if value is None else len(value)}"
        )
    return value


def _pointer(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, int(offset))[0]


def _native_candidates() -> list[Path]:
    roots = []
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        roots.append(Path(bundle))
    roots.extend((Path(__file__).resolve().parent, Path(sys.executable).resolve().parent))
    result = []
    seen = set()
    for root in roots:
        path = root / NATIVE_LIBRARY_NAME
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


class NativeZstdLibrary:
    _shared: "NativeZstdLibrary | None" = None

    def __init__(self, path: Path):
        self.path = Path(path)
        self.dll = ctypes.CDLL(str(self.path))
        for name in (
            "zsh_context_size",
            "zsh_ll_table_capacity",
            "zsh_ml_table_capacity",
            "zsh_of_table_capacity",
            "zsh_huf_table_capacity",
        ):
            getattr(self.dll, name).restype = ctypes.c_size_t
        self.dll.zsh_version_number.restype = ctypes.c_uint32
        self.dll.zsh_create.restype = ctypes.c_void_p
        self.dll.zsh_create.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.dll.zsh_decode_block.restype = ctypes.c_int
        self.dll.zsh_decode_block.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.dll.zsh_free.argtypes = [ctypes.c_void_p]
        if int(self.dll.zsh_context_size()) != DCTX_CONTEXT_SIZE:
            raise RuntimeError("native Zstd context layout is incompatible")

    @classmethod
    def load(cls) -> "NativeZstdLibrary":
        if cls._shared is not None:
            return cls._shared
        failures = []
        for path in _native_candidates():
            try:
                library = cls(path)
            except (OSError, RuntimeError) as exc:
                failures.append(f"{path}: {exc}")
                continue
            cls._shared = library
            return library
        raise RuntimeError(
            "Npcap Zstd recovery component is unavailable"
            + (f": {'; '.join(failures)}" if failures else "")
        )

    @property
    def context_size(self) -> int:
        return int(self.dll.zsh_context_size())

    @property
    def table_capacities(self) -> tuple[int, int, int, int]:
        return (
            int(self.dll.zsh_ll_table_capacity()),
            int(self.dll.zsh_ml_table_capacity()),
            int(self.dll.zsh_of_table_capacity()),
            int(self.dll.zsh_huf_table_capacity()),
        )


def _ctypes_buffer(value: bytes):
    return ctypes.create_string_buffer(value, len(value))


class NativeZstdDecoder:
    def __init__(self, snapshot: ZstdSnapshot):
        self.native = NativeZstdLibrary.load()
        values = [
            _ctypes_buffer(snapshot.context),
            _ctypes_buffer(snapshot.history),
            _ctypes_buffer(snapshot.ll_table),
            _ctypes_buffer(snapshot.ml_table),
            _ctypes_buffer(snapshot.of_table),
            _ctypes_buffer(snapshot.huf_table),
        ]
        error = ctypes.create_string_buffer(256)
        self.state = self.native.dll.zsh_create(
            values[0],
            len(snapshot.context),
            values[1],
            len(snapshot.history),
            values[2],
            len(snapshot.ll_table),
            values[3],
            len(snapshot.ml_table),
            values[4],
            len(snapshot.of_table),
            values[5],
            len(snapshot.huf_table),
            error,
            len(error),
        )
        if not self.state:
            raise RuntimeError(error.value.decode("utf-8", "replace"))

    def close(self) -> None:
        if self.state:
            self.native.dll.zsh_free(self.state)
            self.state = None

    def decompress(self, block: bytes) -> bytes:
        if not self.state:
            raise RuntimeError("Zstd recovery decoder is closed")
        source = _ctypes_buffer(block)
        output = ctypes.create_string_buffer(MAX_ZSTD_BLOCK_SIZE)
        output_size = ctypes.c_size_t()
        error = ctypes.create_string_buffer(256)
        if not self.native.dll.zsh_decode_block(
            self.state,
            source,
            len(block),
            output,
            len(output),
            ctypes.byref(output_size),
            error,
            len(error),
        ):
            raise RuntimeError(error.value.decode("utf-8", "replace"))
        return output.raw[: output_size.value]

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _region_priority(base: int, size: int) -> tuple[bool, int, int, int]:
    """Put connection-sized heap regions ahead of large asset arenas.

    The connection holder is a small allocation, but its virtual address moves
    between allocator bands as a long-running client loads more assets.  Using
    the historical address bands as the primary key can therefore consume the
    scan budget before reaching the live connection.  Exact region size is a
    stable discriminator: visit the smallest private heaps first, then retain
    the observed address bands only as a tie-breaker.  Large arenas keep their
    previous distance ordering.
    """
    distance = min(abs(int(base) - 0x35000000), abs(int(base) - 0x10D000000))
    small = int(size) <= SMALL_CONNECTION_REGION_BYTES
    return (
        not small,
        int(size) if small else 0,
        distance,
        int(base),
    )


def _ordered_regions(process):
    regions = []
    for mbi in npcap_key_state._memory_regions(process):
        base = int(mbi.BaseAddress or 0)
        size = int(mbi.RegionSize or 0)
        protection = int(mbi.Protect) & 0xFF
        if (
            mbi.State != proc_inspect.MEM_COMMIT
            or mbi.Type != npcap_key_state.MEM_PRIVATE
            or protection not in npcap_key_state.WRITABLE_PAGE_TYPES
            or size <= 0
        ):
            continue
        regions.append((_region_priority(base, size), base, size))
    regions.sort()
    return regions


def _valid_context(process, address: int) -> bool:
    try:
        data = _read_exact(process, address, DCTX_FRAME_BLOCK_SIZE_OFFSET + 8)
        window_size = _pointer(data, DCTX_FRAME_WINDOW_SIZE_OFFSET)
        block_size = struct.unpack_from(
            "<I", data, DCTX_FRAME_BLOCK_SIZE_OFFSET
        )[0]
        previous_dst_end = _pointer(data, DCTX_PREVIOUS_DST_END_OFFSET)
        prefix_start = _pointer(data, DCTX_PREFIX_START_OFFSET)
    except (OSError, RuntimeError, struct.error):
        return False
    return bool(
        0 < window_size <= MAX_ZSTD_WINDOW_SIZE
        and 0 < block_size <= MAX_ZSTD_BLOCK_SIZE
        and previous_dst_end >= prefix_start > 0
        and all(_pointer(data, offset) for offset in (0, 8, 16, 24))
    )


def locate_zstd_readers(
    pid: int,
    cryptor_addresses,
    *,
    module_name: str = GAME_MODULE,
    maximum_scan_bytes: int = MAX_CONNECTION_SCAN_BYTES,
) -> tuple[dict[int, "ZstdStateReader"], ZstdLocateDiagnostics]:
    started = time.monotonic()
    addresses = tuple(
        dict.fromkeys(
            int(address) for address in cryptor_addresses if int(address) > 0
        )
    )
    if not addresses:
        return {}, ZstdLocateDiagnostics(0, 0, 0, 0.0)
    module_base, _module_size, _path = proc_inspect.find_module(pid, module_name)
    codec_vtable = module_base + ZSTD_CODEC_VTABLE_RVA
    access = proc_inspect.PROCESS_QUERY_INFORMATION | proc_inspect.PROCESS_VM_READ
    process = proc_inspect.kernel32.OpenProcess(access, False, int(pid))
    if not process:
        raise proc_inspect.winerror("OpenProcess(Zstd locator)")
    needles = {address: struct.pack("<Q", address) for address in addresses}
    unresolved = set(addresses)
    bytes_read = 0
    regions_read = 0
    pointer_hits = 0
    results: dict[int, tuple[int, int]] = {}
    try:
        for _priority, base, size in _ordered_regions(process):
            if bytes_read >= maximum_scan_bytes or not unresolved:
                break
            offset = 0
            overlap = b""
            while offset < size and unresolved:
                length = min(SCAN_CHUNK_BYTES, size - offset)
                length = min(length, maximum_scan_bytes - bytes_read)
                if length <= 0:
                    break
                block = proc_inspect.read_region(process, base + offset, length)
                bytes_read += length
                if block:
                    data = overlap + block
                    origin = base + offset - len(overlap)
                    for cryptor_address in tuple(unresolved):
                        needle = needles[cryptor_address]
                        position = data.find(needle)
                        while position >= 0:
                            field_address = origin + position
                            if field_address % 8 == 0:
                                pointer_hits += 1
                                try:
                                    pair = _read_exact(
                                        process, field_address, 16
                                    )
                                    owner, codec_address = struct.unpack(
                                        "<QQ", pair
                                    )
                                    codec = _read_exact(
                                        process,
                                        codec_address,
                                        CODEC_DCTX_POINTER_OFFSET + 8,
                                    )
                                    context_address = _pointer(
                                        codec, CODEC_DCTX_POINTER_OFFSET
                                    )
                                except (OSError, RuntimeError, struct.error):
                                    pass
                                else:
                                    if (
                                        owner == cryptor_address
                                        and _pointer(codec, 0) == codec_vtable
                                        and _valid_context(
                                            process, context_address
                                        )
                                    ):
                                        results[cryptor_address] = (
                                            codec_address,
                                            context_address,
                                        )
                                        unresolved.remove(cryptor_address)
                                        break
                            position = data.find(needle, position + 1)
                    overlap = data[-15:]
                else:
                    overlap = b""
                offset += length
            regions_read += 1
    finally:
        proc_inspect.kernel32.CloseHandle(process)

    diagnostics = ZstdLocateDiagnostics(
        regions_read=regions_read,
        bytes_read=bytes_read,
        pointer_hits=pointer_hits,
        elapsed_seconds=time.monotonic() - started,
    )
    readers = {}
    for cryptor_address, (codec_address, context_address) in results.items():
        try:
            readers[cryptor_address] = ZstdStateReader(
                pid, codec_address, context_address
            )
        except (OSError, RuntimeError, ValueError, struct.error):
            continue
    return readers, diagnostics


def locate_zstd_reader(
    pid: int,
    cryptor_address: int,
    *,
    module_name: str = GAME_MODULE,
    maximum_scan_bytes: int = MAX_CONNECTION_SCAN_BYTES,
) -> tuple["ZstdStateReader | None", ZstdLocateDiagnostics]:
    readers, diagnostics = locate_zstd_readers(
        pid,
        (cryptor_address,),
        module_name=module_name,
        maximum_scan_bytes=maximum_scan_bytes,
    )
    return readers.get(int(cryptor_address)), diagnostics


class ZstdStateReader:
    def __init__(self, pid: int, codec_address: int, context_address: int):
        self.pid = int(pid)
        self.codec_address = int(codec_address)
        self.context_address = int(context_address)
        self.native = NativeZstdLibrary.load()
        access = proc_inspect.PROCESS_QUERY_INFORMATION | proc_inspect.PROCESS_VM_READ
        self.process = proc_inspect.kernel32.OpenProcess(access, False, self.pid)
        if not self.process:
            raise proc_inspect.winerror("OpenProcess(Zstd reader)")
        try:
            self.snapshot()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.process:
            proc_inspect.kernel32.CloseHandle(self.process)
            self.process = None

    def _sequence_table(self, address: int, capacity: int) -> bytes:
        header = _read_exact(self.process, address, 8)
        table_log = struct.unpack_from("<I", header, 4)[0]
        if table_log > 12:
            raise RuntimeError(f"invalid Zstd sequence table log: {table_log}")
        size = (1 + (1 << table_log)) * 8
        if size > capacity:
            raise RuntimeError("Zstd sequence table exceeds local capacity")
        return _read_exact(self.process, address, size)

    def _snapshot_once(self) -> ZstdSnapshot:
        before = time.time()
        context = _read_exact(
            self.process, self.context_address, self.native.context_size
        )
        previous_dst_end = _pointer(context, DCTX_PREVIOUS_DST_END_OFFSET)
        prefix_start = _pointer(context, DCTX_PREFIX_START_OFFSET)
        virtual_start = _pointer(context, DCTX_VIRTUAL_START_OFFSET)
        dict_end = _pointer(context, DCTX_DICT_END_OFFSET)
        window_size = _pointer(context, DCTX_FRAME_WINDOW_SIZE_OFFSET)
        block_size = struct.unpack_from(
            "<I", context, DCTX_FRAME_BLOCK_SIZE_OFFSET
        )[0]
        if not 0 < window_size <= MAX_ZSTD_WINDOW_SIZE:
            raise RuntimeError(f"invalid Zstd window size: {window_size}")
        if not 0 < block_size <= MAX_ZSTD_BLOCK_SIZE:
            raise RuntimeError(f"invalid Zstd block size: {block_size}")
        current_length = previous_dst_end - prefix_start
        if not 0 <= current_length <= MAX_ZSTD_WINDOW_SIZE * 3:
            raise RuntimeError("invalid Zstd prefix range")
        current_length = min(int(current_length), int(window_size))
        current = (
            _read_exact(
                self.process,
                previous_dst_end - current_length,
                current_length,
            )
            if current_length
            else b""
        )
        available_previous = max(0, prefix_start - virtual_start)
        previous_length = min(
            int(window_size) - len(current), int(available_previous)
        )
        previous = (
            _read_exact(
                self.process,
                dict_end - previous_length,
                previous_length,
            )
            if previous_length
            else b""
        )
        ll_capacity, ml_capacity, of_capacity, huf_capacity = (
            self.native.table_capacities
        )
        ll_table = self._sequence_table(_pointer(context, 0), ll_capacity)
        ml_table = self._sequence_table(_pointer(context, 8), ml_capacity)
        of_table = self._sequence_table(_pointer(context, 16), of_capacity)
        huf_table = _read_exact(
            self.process, _pointer(context, 24), huf_capacity
        )
        after = time.time()
        return ZstdSnapshot(
            codec_address=self.codec_address,
            context_address=self.context_address,
            read_before_epoch=before,
            read_after_epoch=after,
            context=context,
            history=previous + current,
            ll_table=ll_table,
            ml_table=ml_table,
            of_table=of_table,
            huf_table=huf_table,
        )

    def snapshot(self, attempts: int = 12) -> ZstdSnapshot:
        for _attempt in range(max(1, int(attempts))):
            first = self._snapshot_once()
            second = self._snapshot_once()
            if first.fingerprint == second.fingerprint:
                return ZstdSnapshot(
                    codec_address=second.codec_address,
                    context_address=second.context_address,
                    read_before_epoch=first.read_before_epoch,
                    read_after_epoch=second.read_after_epoch,
                    context=second.context,
                    history=second.history,
                    ll_table=second.ll_table,
                    ml_table=second.ml_table,
                    of_table=second.of_table,
                    huf_table=second.huf_table,
                )
        raise RuntimeError("Zstd state changed during every snapshot attempt")


def coherent_session_snapshot(rc4_reader, zstd_reader: ZstdStateReader):
    for _attempt in range(8):
        anchor_before = rc4_reader.snapshot()
        snapshot = zstd_reader.snapshot(attempts=4)
        anchor_after = rc4_reader.snapshot()
        before_decrypt = anchor_before.decrypt
        after_decrypt = anchor_after.decrypt
        if (
            anchor_before.cryptor_address == anchor_after.cryptor_address
            and before_decrypt.x == after_decrypt.x
            and before_decrypt.y == after_decrypt.y
            and before_decrypt.s == after_decrypt.s
        ):
            return anchor_after, snapshot
    raise RuntimeError("RC4 and Zstd state could not be sampled coherently")
