#!/usr/bin/env python3
"""Extract original profession/skill icons from the local C7 client.

The launcher KMF manifest stores one 48-byte record per chunk.  Each record
points at a source file (or a local.cache override) and at one or more compact
12-byte compression block descriptors.  Package chunks are decrypted and
decompressed locally; no texture bytes are read from the running game.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from Crypto.Cipher import AES
from PIL import Image


DEFAULT_CLIENT = Path(r"E:\GMZZLauncher\Game\C7")
DEFAULT_MANIFEST = DEFAULT_CLIENT / "Saved" / "kscache" / "package_2018737.manifest"
DEFAULT_LOCAL_CACHE = DEFAULT_CLIENT / "Saved" / "kscache" / "local.cache"
DEFAULT_AES_KEY = "A0AF3B78F3C87AC5E2454051FCECE197F664CBE049D6D453CC641F002BC517D3"
TEXTURE_MARKER = bytes.fromhex("040409021003")


# The current SkillDataNew table omits SkillIcon for the unreleased Arbiter
# rows even though the client ships the complete original art.  The paths and
# sprite names below come from the local Arbitrator atlas/package index.  Keep
# every combo/effect ID routed to its root art because any of them may appear in
# KAPI_HandleDamageSyncV2.
ARBITRATOR_SKILL_ICON_FALLBACKS = {
    "86040001": "/Game/Arts/UI_2/Resource/Skill/Profession/Arbiter/Arbiter_Attack_01.Arbiter_Attack_01",
    "86040002": "/Game/Arts/UI_2/Resource/Skill/Profession/Arbiter/Arbiter_Attack_01.Arbiter_Attack_01",
    "86040003": "/Game/Arts/UI_2/Resource/Skill/Profession/Arbiter/Arbiter_Attack_01.Arbiter_Attack_01",
    "86040004": "/Game/Arts/UI_2/Resource/Skill/Profession/Arbiter/Arbiter_Attack_01.Arbiter_Attack_01",
    "86040005": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator00_ys_Sprite.UI_Skill_Icon_Arbitrator00_ys_Sprite",
    "86040006": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator02_cj_Sprite.UI_Skill_Icon_Arbitrator02_cj_Sprite",
    "86040007": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator04_jscc_Sprite.UI_Skill_Icon_Arbitrator04_jscc_Sprite",
    "86040008": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator06_zxsh_Sprite.UI_Skill_Icon_Arbitrator06_zxsh_Sprite",
    "86040009": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator07_sply_Sprite.UI_Skill_Icon_Arbitrator07_sply_Sprite",
    "86040010": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator10_cjzj_Sprite.UI_Skill_Icon_Arbitrator10_cjzj_Sprite",
    "86040011": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator10_cjzj_Sprite.UI_Skill_Icon_Arbitrator10_cjzj_Sprite",
    "86040012": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator10_cjzj_Sprite.UI_Skill_Icon_Arbitrator10_cjzj_Sprite",
    "86040013": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator03_tkzb_Sprite.UI_Skill_Icon_Arbitrator03_tkzb_Sprite",
    "86040014": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator11_zxspj_Sprite.UI_Skill_Icon_Arbitrator11_zxspj_Sprite",
    "86040015": "/Game/Arts/UI_2/Resource/Skill/Atlas/Arbitrator/Sprites/UI_Skill_Icon_Arbitrator05_qj_Sprite.UI_Skill_Icon_Arbitrator05_qj_Sprite",
}


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & -boundary


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise ValueError(f"manifest truncated at 0x{self.offset:x} (need {size} bytes)")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def i32(self) -> int:
        value = struct.unpack_from("<i", self.data, self.offset)[0]
        self.offset += 4
        return value

    def u32(self) -> int:
        value = struct.unpack_from("<I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def fstring(self) -> str:
        count = self.i32()
        if count == 0:
            return ""
        if count > 0:
            raw = self.take(count)
            if raw[-1:] != b"\0":
                raise ValueError("unterminated UTF-8 FString")
            return raw[:-1].decode("utf-8")
        raw = self.take(-count * 2)
        if raw[-2:] != b"\0\0":
            raise ValueError("unterminated UTF-16 FString")
        return raw[:-2].decode("utf-16-le")


@dataclass(frozen=True)
class Block:
    offset: int
    compressed_size: int
    raw_size: int
    method: int


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: bytes
    checksum: int
    container: int
    version: int
    block_start: int
    block_count: int
    offset: int
    stored_size: int
    raw_size: int
    flags: int

    @classmethod
    def parse(cls, raw: bytes) -> "ChunkRecord":
        if len(raw) != 48:
            raise ValueError("chunk record must be 48 bytes")
        return cls(
            chunk_id=raw[16:28],
            checksum=struct.unpack_from("<I", raw, 28)[0],
            container=struct.unpack_from("<I", raw, 32)[0],
            version=struct.unpack_from("<I", raw, 36)[0],
            block_start=struct.unpack_from("<I", raw, 40)[0],
            block_count=struct.unpack_from("<I", raw, 44)[0],
            offset=int.from_bytes(raw[0:5], "little"),
            stored_size=int.from_bytes(raw[5:10], "little"),
            raw_size=int.from_bytes(raw[10:15], "little"),
            flags=raw[15],
        )


@dataclass(frozen=True)
class CacheLocation:
    checksum: int
    offset: int
    relative_path: str


class KmfManifest:
    def __init__(self, path: Path):
        self.path = path
        raw = path.read_bytes()
        if len(raw) < 24 or raw[:4] != b"KMF\0":
            raise ValueError(f"not a KMF manifest: {path}")
        if struct.unpack_from("<I", raw, 4)[0] != 6:
            raise ValueError(f"unsupported KMF version in {path}")
        self.data = zlib.decompress(raw[0x18:])
        reader = Reader(self.data)
        self.version = reader.u32()
        self.files = [reader.fstring() for _ in range(reader.i32())]
        self.methods = [reader.fstring() for _ in range(reader.i32())]

        record_count = reader.i32()
        record_bytes = reader.take(record_count * 48)
        self.records: dict[bytes, ChunkRecord] = {}
        for offset in range(0, len(record_bytes), 48):
            record = ChunkRecord.parse(record_bytes[offset : offset + 48])
            self.records[record.chunk_id] = record

        self.block_count = reader.i32()
        self.block_bytes = reader.take(self.block_count * 12)

        # The remaining fields are not required for extraction, but consume and
        # validate them so a format drift cannot silently produce wrong icons.
        set_count = reader.i32()
        reader.take(set_count * 12)
        u32_count = reader.i32()
        reader.take(u32_count * 4)
        reader.take(16)
        reader.u32()
        if reader.offset != len(self.data):
            raise ValueError(
                f"unparsed KMF bytes: 0x{reader.offset:x}/0x{len(self.data):x}"
            )

    def blocks_for(self, start: int, count: int) -> list[Block]:
        if start < 0 or count < 0 or start + count > self.block_count:
            raise ValueError(f"invalid compression block range {start}+{count}")
        result: list[Block] = []
        for index in range(start, start + count):
            offset = index * 12
            raw_block = self.block_bytes[offset : offset + 12]
            result.append(
                Block(
                    offset=int.from_bytes(raw_block[0:5], "little"),
                    compressed_size=int.from_bytes(raw_block[5:8], "little"),
                    raw_size=int.from_bytes(raw_block[8:11], "little"),
                    method=raw_block[11],
                )
            )
        return result


def parse_local_cache(path: Path) -> dict[bytes, CacheLocation]:
    data = path.read_bytes()
    reader = Reader(data)
    result: dict[bytes, CacheLocation] = {}
    while reader.offset < len(data):
        chunk_id = reader.take(12)
        checksum = reader.u32()
        offset = int.from_bytes(reader.take(8), "little")
        relative_path = reader.fstring()
        result[chunk_id] = CacheLocation(checksum, offset, relative_path)
    return result


class Oodle:
    def __init__(self, dll_path: Path):
        self.dll = ctypes.WinDLL(str(dll_path))
        self.decompress_fn = self.dll.OodleLZ_Decompress
        self.decompress_fn.restype = ctypes.c_ssize_t
        self.decompress_fn.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint64,
            ctypes.c_size_t,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
        ]

    def decompress(self, data: bytes, raw_size: int) -> bytes:
        source = ctypes.create_string_buffer(data)
        target = ctypes.create_string_buffer(raw_size)
        result = self.decompress_fn(
            source,
            len(data),
            target,
            raw_size,
            1,
            1,
            0,
            0,
            0,
            0,
            0,
            None,
            0,
            3,
        )
        if result != raw_size:
            raise ValueError(f"Oodle returned {result}; expected {raw_size}")
        return target.raw[:raw_size]


class ChunkExtractor:
    def __init__(
        self,
        client: Path,
        manifest: KmfManifest,
        local_cache: dict[bytes, CacheLocation],
        aes_key: bytes,
        oodle: Oodle,
    ):
        self.client = client
        self.manifest = manifest
        self.local_cache = local_cache
        self.aes = AES.new(aes_key, AES.MODE_ECB)
        self.oodle = oodle
        self.resolved_routes: dict[str, dict[str, object]] = {}

    def _source(self, record: ChunkRecord) -> tuple[Path, int]:
        cached = self.local_cache.get(record.chunk_id)
        if cached is not None:
            if cached.checksum != record.checksum:
                raise ValueError(
                    f"local.cache checksum mismatch for {record.chunk_id.hex()}"
                )
            return self.local_cache_root / cached.relative_path, cached.offset
        if record.container >= len(self.manifest.files):
            raise ValueError(f"invalid container index {record.container}")
        return self.client / "Content" / self.manifest.files[record.container], record.offset

    @property
    def local_cache_root(self) -> Path:
        return self.client / "Saved" / "kscache"

    def _crc32_at(self, path: Path, offset: int, size: int) -> tuple[int, bytes]:
        with path.open("rb") as source:
            source.seek(offset)
            data = source.read(size)
        if len(data) != size:
            raise ValueError(f"short read from {path} at 0x{offset:x}")
        return zlib.crc32(data) & 0xFFFFFFFF, data

    def _previous_partition(self, path: Path) -> Path | None:
        match = re.match(r"^(.*)_s(\d+)(\.[^.]+)$", path.name, flags=re.IGNORECASE)
        if not match:
            return None
        prefix, number_text, suffix = match.groups()
        number = int(number_text)
        previous_name = (
            f"{prefix}{suffix}" if number == 1 else f"{prefix}_s{number - 1}{suffix}"
        )
        previous = path.with_name(previous_name)
        return previous if previous.is_file() else None

    def _scan_encrypted_oodle(
        self, path: Path, record: ChunkRecord, tail_bytes: int = 128 * 1024 * 1024
    ) -> tuple[int, bytes] | None:
        if not (record.flags & 1) or record.block_count <= 0:
            return None
        first_block = self.manifest.blocks_for(record.block_start, 1)[0]
        if (
            first_block.method >= len(self.manifest.methods)
            or self.manifest.methods[first_block.method].casefold() != "oodle"
        ):
            return None

        file_size = path.stat().st_size
        start = max(0, file_size - tail_bytes) & -16
        with path.open("rb") as source:
            source.seek(start)
            ciphertext = source.read()
        usable = len(ciphertext) // 16 * 16
        plaintext = self.aes.decrypt(ciphertext[:usable])
        marker = b"\x8c\x06\x00"
        at = plaintext.find(marker)
        while at >= 0:
            if at % 16 == 0 and at + record.stored_size <= len(ciphertext):
                candidate = ciphertext[at : at + record.stored_size]
                if zlib.crc32(candidate) & 0xFFFFFFFF == record.checksum:
                    return start + at, candidate
            at = plaintext.find(marker, at + 1)
        return None

    def _verified_stored(self, record: ChunkRecord) -> tuple[Path, int, bytes]:
        source_path, source_offset = self._source(record)
        actual_crc, stored = self._crc32_at(
            source_path, source_offset, record.stored_size
        )
        if actual_crc == record.checksum:
            return source_path, source_offset, stored

        # Older KMF versions can leave the first chunks of a logical partition
        # in the tail of the preceding physical .ucas partition.  Resolve that
        # route by the manifest's exact encrypted CRC instead of hard-coding an
        # asset or offset.
        if record.chunk_id not in self.local_cache:
            previous = self._previous_partition(source_path)
            if previous is not None:
                resolved = self._scan_encrypted_oodle(previous, record)
                if resolved is not None:
                    resolved_offset, stored = resolved
                    self.resolved_routes[record.chunk_id.hex()] = {
                        "declared_file": str(source_path),
                        "declared_offset": source_offset,
                        "resolved_file": str(previous),
                        "resolved_offset": resolved_offset,
                        "checksum": f"{record.checksum:08x}",
                    }
                    return previous, resolved_offset, stored
        raise ValueError(
            f"stored CRC mismatch for {record.chunk_id.hex()}: "
            f"{actual_crc:08x} != {record.checksum:08x}"
        )

    def extract(self, chunk_id: bytes) -> bytes:
        record = self.manifest.records.get(chunk_id)
        if record is None:
            raise KeyError(f"chunk is absent from manifest: {chunk_id.hex()}")
        source_path, source_offset, stored_chunk = self._verified_stored(record)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        # Some route entries are stored verbatim and therefore have no block
        # descriptors (large patch blobs are a common example).
        if record.block_count == 0:
            return stored_chunk[: record.raw_size]

        blocks = self.manifest.blocks_for(record.block_start, record.block_count)

        encrypted = bool(record.flags & 1)
        output = bytearray()
        for block in blocks:
            read_size = align(block.compressed_size, 16) if encrypted else block.compressed_size
            stored = stored_chunk[block.offset : block.offset + read_size]
            if len(stored) != read_size:
                raise ValueError(f"short block read from {source_path}")
            if encrypted:
                stored = self.aes.decrypt(stored)
            compressed = stored[: block.compressed_size]
            if block.method == 0:
                decoded = compressed[: block.raw_size]
            elif block.method < len(self.manifest.methods) and self.manifest.methods[block.method].casefold() == "oodle":
                decoded = self.oodle.decompress(compressed, block.raw_size)
            else:
                raise ValueError(
                    f"unsupported compression method {block.method} for {chunk_id.hex()}"
                )
            if len(decoded) != block.raw_size:
                raise ValueError(f"decoded block length mismatch for {chunk_id.hex()}")
            output.extend(decoded)

        if len(output) != record.raw_size:
            raise ValueError(
                f"chunk length mismatch for {chunk_id.hex()}: {len(output)} != {record.raw_size}"
            )
        return bytes(output)


def package_ids(package_id_exe: Path, package_names: Iterable[str]) -> dict[str, int]:
    names = list(dict.fromkeys(package_names))
    result: dict[str, int] = {}
    for start in range(0, len(names), 64):
        batch = names[start : start + 64]
        completed = subprocess.run(
            [str(package_id_exe), *batch],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        lines = completed.stdout.splitlines()
        if len(lines) != len(batch):
            raise ValueError("package_id.exe returned an unexpected number of rows")
        for expected, line in zip(batch, lines):
            value, actual = line.split("\t", 1)
            if actual != expected:
                raise ValueError(f"package ID output mismatch: {actual!r} != {expected!r}")
            result[expected] = int(value, 16)
    return result


def package_chunk_id(package_id: int) -> bytes:
    return package_id.to_bytes(8, "little") + b"\0\0\0\1"


def split_object_path(object_path: str) -> tuple[str, str]:
    package, dot, obj = object_path.rpartition(".")
    if not dot or not package.startswith("/Game/"):
        raise ValueError(f"unsupported Unreal object path: {object_path}")
    return package, obj


def inspect_zen(inspector: Path, raw: bytes) -> dict:
    completed = subprocess.run(
        [str(inspector), "-"],
        input=raw,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return json.loads(completed.stdout)


def _decode_bcn(
    width: int, height: int, pixel_format: str, pixels: bytes
) -> Image.Image:
    block_size = 8 if pixel_format == "PF_DXT1" else 16
    expected = ((width + 3) // 4) * ((height + 3) // 4) * block_size
    if len(pixels) != expected:
        raise ValueError(
            f"{pixel_format} payload mismatch: {len(pixels)} != {expected}"
        )
    if pixel_format == "PF_DXT1":
        return Image.frombytes("RGBA", (width, height), pixels, "bcn", 1, "BC1")
    if pixel_format == "PF_DXT5":
        return Image.frombytes("RGBA", (width, height), pixels, "bcn", 3, "BC3")
    if pixel_format == "PF_BC7":
        return Image.frombytes("RGBA", (width, height), pixels, "bcn", 7, "BC7")
    raise ValueError(f"unsupported BCN pixel format: {pixel_format}")


def _pixel_size(width: int, height: int, pixel_format: str) -> int | None:
    if pixel_format == "PF_DXT1":
        return ((width + 3) // 4) * ((height + 3) // 4) * 8
    if pixel_format in {"PF_DXT5", "PF_BC7"}:
        return ((width + 3) // 4) * ((height + 3) // 4) * 16
    if pixel_format == "PF_B8G8R8A8":
        return width * height * 4
    return None


def _decode_pixels(
    width: int, height: int, pixel_format: str, pixels: bytes
) -> Image.Image:
    if pixel_format in {"PF_DXT1", "PF_DXT5", "PF_BC7"}:
        return _decode_bcn(width, height, pixel_format, pixels)
    if pixel_format == "PF_B8G8R8A8":
        expected = width * height * 4
        if len(pixels) != expected:
            raise ValueError(
                f"{pixel_format} payload mismatch: {len(pixels)} != {expected}"
            )
        return Image.frombytes("RGBA", (width, height), pixels, "raw", "BGRA")
    raise ValueError(f"unsupported Texture2D pixel format: {pixel_format}")


def decode_texture(raw: bytes, info: dict | None = None) -> Image.Image:
    candidates: list[tuple[int, int, int, str, bytes]] = []
    offset = 0
    while True:
        marker = raw.find(TEXTURE_MARKER, offset)
        if marker < 0:
            break
        offset = marker + 1
        if marker + 0x74 > len(raw):
            continue
        width, height = struct.unpack_from("<II", raw, marker + 6)
        format_end = raw.find(b"\0", marker + 0x60, marker + 0x74)
        if format_end < 0:
            continue
        pixel_format = raw[marker + 0x60 : format_end].decode("ascii", errors="ignore")
        if width <= 0 or height <= 0 or width > 16384 or height > 16384:
            continue
        pixel_size = _pixel_size(width, height, pixel_format)
        if pixel_size is None:
            continue
        pixel_start = marker + 0x74
        pixel_end = pixel_start + pixel_size
        if pixel_end + 28 > len(raw):
            continue
        candidates.append((marker, width, height, pixel_format, raw[pixel_start:pixel_end]))
    if candidates:
        _, width, height, pixel_format, pixels = candidates[-1]
        return _decode_pixels(width, height, pixel_format, pixels)

    # Some atlases use a newer Texture2D header marker.  The Zen bulk table is
    # authoritative and gives the exact pixel offset/length, so locate the DXT
    # format and infer the adjacent dimensions from that validated byte count.
    if info is not None and len(info.get("exports", [])) == 1 and len(info.get("bulk", [])) == 1:
        export = info["exports"][0]
        bulk = info["bulk"][0]
        export_start = int(info["header_size"]) + int(export["serial_offset"])
        pixel_start = export_start + int(bulk["serial_offset"])
        pixel_size = int(bulk["serial_size"])
        pixel_end = pixel_start + pixel_size
        if 0 <= export_start <= pixel_start and pixel_end <= len(raw):
            header = raw[export_start:pixel_start]
            for pixel_format in ("PF_B8G8R8A8", "PF_BC7", "PF_DXT5", "PF_DXT1"):
                format_offset = header.find(pixel_format.encode("ascii"))
                if format_offset < 0:
                    continue
                dimension_candidates: list[tuple[int, int, int]] = []
                for offset in range(0, max(0, format_offset - 7)):
                    width, height = struct.unpack_from("<II", header, offset)
                    if width <= 0 or height <= 0 or width > 16384 or height > 16384:
                        continue
                    expected = _pixel_size(width, height, pixel_format)
                    if expected == pixel_size:
                        dimension_candidates.append((offset, width, height))
                if dimension_candidates:
                    # The cooked top-level dimensions precede derived mip data;
                    # later byte-aligned matches can be accidental divisors of
                    # the same BC payload size.
                    _, width, height = dimension_candidates[0]
                    return _decode_pixels(
                        width,
                        height,
                        pixel_format,
                        raw[pixel_start:pixel_end],
                    )
    raise ValueError("no supported cooked Texture2D payload found")


def atlas_slots(
    raw: bytes, info: dict, texture_size: tuple[int, int]
) -> dict[str, tuple[int, int, int, int]]:
    exports = info.get("exports", [])
    if len(exports) != 1:
        raise ValueError(f"expected one atlas export, found {len(exports)}")
    export = exports[0]
    start = int(info["header_size"]) + int(export["serial_offset"])
    end = start + int(export["serial_size"])
    payload = raw[start:end]
    if len(payload) < 48 or payload[:4] != b"\0\x04\x01\x05":
        raise ValueError("unsupported PaperSprite atlas serialization")
    count = struct.unpack_from("<I", payload, 8)[0]
    names = info.get("names", [])
    if count <= 0 or count > len(names):
        raise ValueError(f"invalid atlas slot count {count}")

    # Each slot has four UE5/LWC doubles.  The compact object reference before
    # it is variable at the start of large name maps, but subsequent slots are
    # 50 bytes apart and the property terminator is always the final 16 bytes.
    coordinate_start = len(payload) - (count - 1) * 50 - 32 - 16
    if coordinate_start < 0:
        raise ValueError("atlas slot table is truncated")
    texture_width, texture_height = texture_size
    result: dict[str, tuple[int, int, int, int]] = {}
    for index in range(count):
        offset = coordinate_start + index * 50
        values = list(struct.unpack_from("<4d", payload, offset))
        values = [0.0 if abs(value) < 1e-200 else value for value in values]
        if not all(value.is_integer() for value in values):
            raise ValueError(f"non-integral atlas coordinates at slot {index}: {values}")
        x, y, width, height = (int(value) for value in values)
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > texture_width
            or y + height > texture_height
        ):
            raise ValueError(
                f"atlas slot {index} is outside {texture_size}: {(x, y, width, height)}"
            )
        result[str(names[index])] = (x, y, width, height)
    return result


def metadata_icon_paths(
    metadata_path: Path,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, object],
    dict[str, str],
]:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    professions = {
        str(class_id): row["icon_path"]
        for class_id, row in metadata.get("professions", {}).items()
        if isinstance(row.get("icon_path"), str)
        and row["icon_path"].startswith("/Game/")
    }
    metadata_skills = {
        str(skill_id): row["icon_path"]
        for skill_id, row in metadata.get("skills", {}).items()
        if isinstance(row.get("icon_path"), str)
        and row["icon_path"].startswith("/Game/")
    }
    fallback_skills = {
        skill_id: object_path
        for skill_id, object_path in ARBITRATOR_SKILL_ICON_FALLBACKS.items()
        if skill_id not in metadata_skills
    }
    skills = {**metadata_skills, **fallback_skills}
    missing = {
        str(skill_id): row.get("icon_path")
        for skill_id, row in metadata.get("skills", {}).items()
        if str(skill_id) not in metadata_skills
    }
    return professions, skills, missing, fallback_skills


def validate_package(info: dict, expected_package: str) -> None:
    actual = info.get("package")
    if actual != expected_package:
        raise ValueError(f"package mismatch: {actual!r} != {expected_package!r}")


def retain_raw(raw_dir: Path | None, package_id: int, raw: bytes) -> None:
    if raw_dir is None:
        return
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{package_id:016x}.zen").write_bytes(raw)


def write_icons(
    *,
    workspace: Path,
    assets_dir: Path,
    raw_dir: Path | None,
    metadata_path: Path,
    skill_names_path: Path,
    package_id_exe: Path,
    inspector: Path,
    extractor: ChunkExtractor,
) -> dict:
    professions, skills, missing, fallback_skills = metadata_icon_paths(metadata_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata_skill_rows = metadata.get("skills", {})

    def profession_ids_for(skill_id: str) -> list[int]:
        row = metadata_skill_rows.get(str(skill_id), {})
        raw_ids = row.get("profession_ids", []) if isinstance(row, dict) else []
        class_ids = sorted(
            {
                int(value)
                for value in raw_ids
                if isinstance(value, (int, float, str))
                and str(value).strip().isdigit()
                and 1_200_001 <= int(value) <= 1_200_007
            }
        )
        if not class_ids:
            parsed_id = int(skill_id)
            if 86_010_000 <= parsed_id < 86_080_000:
                index = (parsed_id - 86_000_000) // 10_000
                if 1 <= index <= 7:
                    class_ids = [1_200_000 + index]
        return class_ids
    source_paths = list(dict.fromkeys([*professions.values(), *skills.values()]))
    split_paths = {path: split_object_path(path) for path in source_paths}
    packages = list(dict.fromkeys(package for package, _ in split_paths.values()))
    ids = package_ids(package_id_exe, packages)

    images: dict[str, Image.Image] = {}
    image_sources: dict[str, dict] = {}
    sprite_atlases: dict[str, str] = {}
    failures: list[dict[str, str]] = []

    for index, object_path in enumerate(source_paths, 1):
        package, obj = split_paths[object_path]
        package_id = ids[package]
        try:
            raw = extractor.extract(package_chunk_id(package_id))
            retain_raw(raw_dir, package_id, raw)
            info = inspect_zen(inspector, raw)
            validate_package(info, package)
            if "/Sprites/" in package or obj.endswith("_Sprite"):
                imports = info.get("imports", [])
                if len(imports) != 1 or not isinstance(imports[0], str):
                    raise ValueError(f"sprite has unexpected atlas imports: {imports!r}")
                sprite_atlases[object_path] = imports[0]
            else:
                image = decode_texture(raw, info)
                images[object_path] = image
                image_sources[object_path] = {
                    "kind": "texture",
                    "package": package,
                    "package_id": f"{package_id:016x}",
                    "size": [image.width, image.height],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
        except Exception as error:
            failures.append({"path": object_path, "error": str(error)})
        if index % 50 == 0 or index == len(source_paths):
            print(f"源资源 {index}/{len(source_paths)}")

    atlas_packages = list(dict.fromkeys(sprite_atlases.values()))
    atlas_ids = package_ids(package_id_exe, atlas_packages)
    atlas_cache: dict[str, tuple[Image.Image, dict[str, tuple[int, int, int, int]], str, str]] = {}
    for atlas_package in atlas_packages:
        try:
            atlas_id = atlas_ids[atlas_package]
            atlas_raw = extractor.extract(package_chunk_id(atlas_id))
            retain_raw(raw_dir, atlas_id, atlas_raw)
            atlas_info = inspect_zen(inspector, atlas_raw)
            validate_package(atlas_info, atlas_package)
            imports = atlas_info.get("imports", [])
            if len(imports) != 1 or not isinstance(imports[0], str):
                raise ValueError(f"atlas has unexpected texture imports: {imports!r}")
            texture_package = imports[0]
            texture_id = package_ids(package_id_exe, [texture_package])[texture_package]
            texture_raw = extractor.extract(package_chunk_id(texture_id))
            retain_raw(raw_dir, texture_id, texture_raw)
            texture_info = inspect_zen(inspector, texture_raw)
            validate_package(texture_info, texture_package)
            texture = decode_texture(texture_raw, texture_info)
            slots = atlas_slots(atlas_raw, atlas_info, texture.size)
            atlas_cache[atlas_package] = (
                texture,
                slots,
                texture_package,
                f"{texture_id:016x}",
            )
        except Exception as error:
            failures.append({"path": atlas_package, "error": str(error)})

    for object_path, atlas_package in sprite_atlases.items():
        try:
            _, obj = split_paths[object_path]
            texture, slots, texture_package, texture_id = atlas_cache[atlas_package]
            box = slots.get(obj)
            if box is None:
                raise ValueError(f"sprite {obj!r} is absent from {atlas_package}")
            x, y, width, height = box
            image = texture.crop((x, y, x + width, y + height))
            images[object_path] = image
            image_sources[object_path] = {
                "kind": "sprite",
                "atlas": atlas_package,
                "texture": texture_package,
                "texture_package_id": texture_id,
                "box": [x, y, width, height],
                "size": [width, height],
            }
        except Exception as error:
            failures.append({"path": object_path, "error": str(error)})

    profession_dir = assets_dir / "professions"
    skill_dir = assets_dir / "skills"
    profession_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir(parents=True, exist_ok=True)

    written_professions = 0
    written_exact_skills = 0
    written_alias_skills = 0
    id_sources: dict[str, dict[str, dict]] = {"professions": {}, "skills": {}}
    for class_id, object_path in professions.items():
        image = images.get(object_path)
        if image is None:
            continue
        image.save(profession_dir / f"{class_id}.png", optimize=True)
        id_sources["professions"][class_id] = {
            "icon_path": object_path,
            **image_sources[object_path],
        }
        written_professions += 1
    for skill_id, object_path in skills.items():
        image = images.get(object_path)
        if image is None:
            continue
        image.save(skill_dir / f"{skill_id}.png", optimize=True)
        source = {
            "icon_path": object_path,
            **image_sources[object_path],
        }
        class_ids = profession_ids_for(skill_id)
        if class_ids:
            source["profession_ids"] = class_ids
        if skill_id in fallback_skills:
            source["route"] = "client_asset_fallback"
            source["reason"] = "客户端技能表缺少 SkillIcon，按本地职业图集与封包技能 ID 对齐"
        id_sources["skills"][skill_id] = source
        written_exact_skills += 1

    # Combat packets may report an effect/sub-skill ID instead of the toolbar
    # skill ID.  Only alias when the client's exact Chinese name resolves to a
    # single original icon path; ambiguous monster/common names stay on the UI
    # fallback so an unrelated image is never shown.
    skill_names = json.loads(skill_names_path.read_text(encoding="utf-8"))
    candidates_by_name: dict[str, set[str]] = {}
    source_ids_by_name_path: dict[tuple[str, str], list[str]] = {}
    for skill_id, object_path in skills.items():
        name = str(skill_names.get(skill_id, "")).strip()
        if name and object_path in images:
            candidates_by_name.setdefault(name, set()).add(object_path)
            source_ids_by_name_path.setdefault((name, object_path), []).append(skill_id)
    icon_aliases: dict[str, dict[str, object]] = {}
    alias_profession_routes = 0
    for raw_skill_id, raw_name in skill_names.items():
        skill_id = str(raw_skill_id)
        if skill_id in skills:
            continue
        name = str(raw_name).strip()
        candidates = candidates_by_name.get(name, set())
        if len(candidates) != 1:
            continue
        object_path = next(iter(candidates))
        images[object_path].save(skill_dir / f"{skill_id}.png", optimize=True)
        source_skill_ids = sorted(
            source_ids_by_name_path.get((name, object_path), []), key=int
        )
        alias_source: dict[str, object] = {
            "name": name,
            "icon_path": object_path,
            "source_skill_ids": source_skill_ids,
            "reason": "客户端同名技能的唯一原图",
        }
        class_ids = sorted(
            {
                class_id
                for source_skill_id in source_skill_ids
                for class_id in profession_ids_for(source_skill_id)
            }
        )
        if len(class_ids) == 1:
            alias_source["profession_ids"] = class_ids
            alias_profession_routes += 1
        icon_aliases[skill_id] = alias_source
        written_alias_skills += 1

    report = {
        "schema_version": 2,
        "source": "C7 local KMF/local.cache unpack",
        "professions": id_sources["professions"],
        "skills": id_sources["skills"],
        "skill_icon_aliases": icon_aliases,
        "client_fallback_skill_icons": fallback_skills,
        "missing_metadata_icons": missing,
        "failures": failures,
        "resolved_routes": extractor.resolved_routes,
        "summary": {
            "profession_metadata": len(professions),
            "profession_pngs": written_professions,
            "skill_metadata": len(skills) - len(fallback_skills),
            "client_fallback_skill_routes": len(fallback_skills),
            "exact_skill_pngs": written_exact_skills,
            "alias_skill_pngs": written_alias_skills,
            "alias_profession_routes": alias_profession_routes,
            "skill_pngs": written_exact_skills + written_alias_skills,
            "unique_source_paths": len(source_paths),
            "decoded_source_paths": len(images),
            "atlas_packages": len(atlas_packages),
            "failures": len(failures),
        },
    }
    (assets_dir / "icon_sources.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    workspace = script_dir.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", type=Path, default=DEFAULT_CLIENT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--local-cache", type=Path, default=DEFAULT_LOCAL_CACHE)
    parser.add_argument("--metadata", type=Path, default=workspace / "skill_metadata.json")
    parser.add_argument("--skill-names", type=Path, default=workspace / "skill_names.json")
    parser.add_argument("--package-id", type=Path, default=script_dir / "package_id.exe")
    parser.add_argument("--inspect-zen", type=Path, default=script_dir / "retoc-src" / "target" / "release" / "inspect_zen.exe")
    parser.add_argument("--oodle", type=Path, default=script_dir / "retoc-src" / "target" / "release" / "oo2core_9_win64.dll")
    parser.add_argument("--aes-key", default=DEFAULT_AES_KEY)
    parser.add_argument("--raw-dir", type=Path, help="also retain extracted Zen packages")
    parser.add_argument("--assets-dir", type=Path, default=workspace / "assets")
    parser.add_argument("--dry-run", action="store_true", help="only validate index coverage")
    parser.add_argument("--only", help="extract one Unreal object/package path for diagnostics")
    args = parser.parse_args()

    manifest = KmfManifest(args.manifest)
    local_cache = parse_local_cache(args.local_cache)
    extractor = ChunkExtractor(
        args.client,
        manifest,
        local_cache,
        bytes.fromhex(args.aes_key),
        Oodle(args.oodle),
    )

    if args.only:
        package = args.only.rpartition(".")[0] if "." in args.only else args.only
        package_id = package_ids(args.package_id, [package])[package]
        raw = extractor.extract(package_chunk_id(package_id))
        info = inspect_zen(args.inspect_zen, raw)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        if args.raw_dir:
            args.raw_dir.mkdir(parents=True, exist_ok=True)
            path = args.raw_dir / f"{package_id:016x}.zen"
            path.write_bytes(raw)
            print(f"raw={path}")
        try:
            image = decode_texture(raw, info)
        except ValueError:
            pass
        else:
            output = (args.raw_dir or workspace / "assets") / f"{package_id:016x}.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            image.save(output)
            print(f"texture={output} {image.width}x{image.height}")
        return 0

    professions, skills, missing, fallback_skills = metadata_icon_paths(args.metadata)
    icon_paths = list(dict.fromkeys([*professions.values(), *skills.values()]))
    packages = [split_object_path(path)[0] for path in icon_paths]
    ids = package_ids(args.package_id, packages)
    coverage = {
        "manifest_records": len(manifest.records),
        "compression_blocks": manifest.block_count,
        "cache_overrides": len(local_cache),
        "profession_icons": len(professions),
        "skill_icons": len(skills),
        "client_fallback_skill_icons": len(fallback_skills),
        "missing_metadata_icons": len(missing),
        "unique_packages": len(set(packages)),
        "packages_present": sum(
            package_chunk_id(ids[package]) in manifest.records
            for package in set(packages)
        ),
    }
    print(json.dumps(coverage, ensure_ascii=False, indent=2))
    if not args.dry_run:
        report = write_icons(
            workspace=workspace,
            assets_dir=args.assets_dir,
            raw_dir=args.raw_dir,
            metadata_path=args.metadata,
            skill_names_path=args.skill_names,
            package_id_exe=args.package_id,
            inspector=args.inspect_zen,
            extractor=extractor,
        )
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        if report["failures"]:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
