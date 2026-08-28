#!/usr/bin/env python3
"""Export C7 skill IDs and their loaded Chinese names.

The game's KSBC2 cache contains every skill row, but localized fields are
stored as ``LangStrSplit`` references.  The live Lua localization tables map
those numeric references to UTF-8 strings.  This tool decodes the KSBC2 cache,
walks ``SkillDataNew.data``, and reads that already-loaded localization map
without installing a hook or writing to the game process.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


KSBC2_XOR_KEY = bytes.fromhex(
    "093f3756a60bcf0f1ce16f5a62424517"
    "f4482d713b9abc5c578fbbe395247a14"
    "234bd080d18b441689538c6b98df30c3"
    "74dbf3b4541b0355320a0275b713996c"
)

DEFAULT_CACHE = Path(
    r"E:\GMZZLauncher\Game\C7\Saved\kscache\14\4efadcdd4c7bb254c65f6f07_2068316"
)
DEFAULT_ROOT_HANDLE = 0x02D209A8

# Exact values recovered from the current client's localization table.  They
# let metadata-only exports remain reproducible when the game has temporarily
# unloaded the corresponding Lua hash nodes.
PROFESSION_NAME_SNAPSHOT = {
    765_264_387_900_928: "太阳途径",
    765_264_387_901_440: "空想家途径",
    765_264_387_901_952: "愚者途径",
    1_222_658_003_831_040: "审判者途径",
    765_264_387_902_976: "门途径",
    765_264_387_903_488: "黄昏巨人途径",
    765_264_387_904_000: "隐者途径",
}

NUMERIC_FORMATS: tuple[str | None, ...] = (
    "<d",  # 0: double
    "<B",  # 1: uint8
    "<b",  # 2: int8
    "<H",  # 3: uint16
    "<h",  # 4: int16
    "<I",  # 5: uint32
    "<i",  # 6: int32
    None,
    None,
    "<B",  # 9: bool storage
)
STORAGE_WIDTHS = (8, 1, 1, 2, 2, 4, 4, 4, 4, 1)


@dataclass(frozen=True)
class TableRef:
    handle: int


@dataclass(frozen=True)
class LangRef:
    localization_id: int
    namespace: str


@dataclass(frozen=True)
class SkillSource:
    skill_id: int
    lang_ref: LangRef | None
    direct_name: str | None
    icon_path: str | None


@dataclass(frozen=True)
class ProfessionSource:
    class_id: int
    localization_id: int | None
    icon_path: str | None


class KSBC2Error(RuntimeError):
    pass


class KSBC2Reader:
    """Minimal reader for the table/value subset used by C7's KSBC2 data."""

    def __init__(self, encoded: bytes):
        key = KSBC2_XOR_KEY
        self.data = bytes(value ^ key[index & 63] for index, value in enumerate(encoded))

    @classmethod
    def from_path(cls, path: Path) -> "KSBC2Reader":
        return cls(path.read_bytes())

    def _check(self, offset: int, size: int = 1) -> None:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise KSBC2Error(
                f"KSBC2 offset outside file: 0x{offset:x}+0x{size:x} "
                f"(size 0x{len(self.data):x})"
            )

    def u32(self, offset: int) -> int:
        self._check(offset, 4)
        return struct.unpack_from("<I", self.data, offset)[0]

    def scalar(self, offset: int, kind: int) -> int | float:
        if not 0 <= kind < len(NUMERIC_FORMATS) or NUMERIC_FORMATS[kind] is None:
            raise KSBC2Error(f"unsupported numeric storage kind {kind} at 0x{offset:x}")
        fmt = NUMERIC_FORMATS[kind]
        assert fmt is not None
        self._check(offset, struct.calcsize(fmt))
        return struct.unpack_from(fmt, self.data, offset)[0]

    def string(self, offset: int) -> str:
        self._check(offset)
        end = self.data.find(b"\0", offset, min(len(self.data), offset + 1_000_000))
        if end < 0:
            raise KSBC2Error(f"unterminated KSBC2 string at 0x{offset:x}")
        return self.data[offset:end].decode("utf-8", "strict")

    def table_entries(self, handle: int) -> Iterator[tuple[object, object]]:
        """Yield a KSBC2 table in the same order as ``ksbc_next``."""

        table_header = self.u32(handle)
        key_block = self.u32(table_header)
        descriptor_block = self.u32(table_header + 4)
        value_block = self.u32(table_header + 8)

        key_storage = self.data[key_block + 4]
        value_storage = self.data[value_block]
        if key_storage >= len(STORAGE_WIDTHS) or value_storage >= len(STORAGE_WIDTHS):
            raise KSBC2Error(
                f"invalid table storage at handle 0x{handle:x}: "
                f"key={key_storage}, value={value_storage}"
            )
        key_width = STORAGE_WIDTHS[key_storage]
        value_width = STORAGE_WIDTHS[value_storage]
        count = self.u32(key_block + 5)
        numeric_key_count = self.u32(key_block + 9)
        if numeric_key_count > count:
            raise KSBC2Error(
                f"invalid key counts at handle 0x{handle:x}: "
                f"{numeric_key_count}>{count}"
            )

        for one_based_index in range(1, count + 1):
            if one_based_index <= numeric_key_count:
                key_offset = key_block + 13 + (one_based_index - 1) * key_width
                key: object = self.scalar(key_offset, key_storage)
            else:
                string_key_offset = (
                    key_block
                    + 9
                    + numeric_key_count * key_width
                    + (one_based_index - numeric_key_count) * 4
                )
                key = self.string(self.u32(string_key_offset))

            descriptor = self.data[descriptor_block + one_based_index - 1]
            value_offset_entry = value_block + 1 + (one_based_index - 1) * value_width
            encoded_offset = int(self.scalar(value_offset_entry, value_storage))

            relative_backwards = descriptor >= 50
            descriptor_kind = descriptor - 50 if relative_backwards else descriptor
            if descriptor_kind >= 10:
                descriptor_kind -= 10

            payload_offset = (
                value_offset_entry - encoded_offset
                if relative_backwards
                else handle + encoded_offset
            )
            if 0 <= descriptor_kind <= 6:
                value: object = self.scalar(payload_offset, descriptor_kind)
            elif descriptor_kind == 7:
                value = self.string(self.u32(payload_offset))
            elif descriptor_kind == 8:
                value = TableRef(self.u32(payload_offset))
            elif descriptor_kind == 9:
                self._check(payload_offset)
                value = self.data[payload_offset] == 1
            else:
                raise KSBC2Error(
                    f"unknown value descriptor {descriptor} at table 0x{handle:x}"
                )
            yield key, value

    def table_dict(self, handle: int) -> dict[object, object]:
        return dict(self.table_entries(handle))


def table_ref(value: object, path: str) -> int:
    if not isinstance(value, TableRef):
        raise KSBC2Error(f"expected table at {path}, got {value!r}")
    return value.handle


def normalize_localization_id(value: object) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
    result = int(value)
    return result if result > 0 else None


def extract_lang_ref(reader: KSBC2Reader, value: object) -> LangRef | None:
    if not isinstance(value, TableRef):
        return None
    container = reader.table_dict(value.handle)
    if container.get("__cname") != "LangStrSplit":
        return None
    split = container.get("value")
    if not isinstance(split, TableRef):
        return None
    parts = reader.table_dict(split.handle)
    localization_id = normalize_localization_id(parts.get(1))
    namespace = parts.get(2)
    if localization_id is None or not isinstance(namespace, str) or not namespace:
        return None
    return LangRef(localization_id, namespace)


def extract_skill_sources(reader: KSBC2Reader, root_handle: int) -> list[SkillSource]:
    root = reader.table_dict(root_handle)
    skill_data_new = reader.table_dict(
        table_ref(root.get("SkillDataNew"), "root.SkillDataNew")
    )
    rows = reader.table_entries(
        table_ref(skill_data_new.get("data"), "root.SkillDataNew.data")
    )

    result: list[SkillSource] = []
    for raw_skill_id, raw_row in rows:
        if not isinstance(raw_skill_id, (int, float)) or isinstance(raw_skill_id, bool):
            continue
        skill_id = int(raw_skill_id)
        if skill_id <= 0 or not isinstance(raw_row, TableRef):
            continue
        row = reader.table_dict(raw_row.handle)
        raw_name = row.get("Name")
        direct_name = raw_name.strip() if isinstance(raw_name, str) else None
        raw_icon = row.get("SkillIcon") or row.get("IconTexture")
        icon_path = raw_icon.strip() if isinstance(raw_icon, str) else None
        result.append(
            SkillSource(
                skill_id=skill_id,
                lang_ref=extract_lang_ref(reader, raw_name),
                direct_name=direct_name or None,
                icon_path=icon_path or None,
            )
        )
    return result


def extract_profession_skill_ids(
    reader: KSBC2Reader, root_handle: int
) -> dict[int, list[int]]:
    """Return each playable profession's own skills from the unlock table."""
    root = reader.table_dict(root_handle)
    unlock_data = reader.table_dict(
        table_ref(root.get("RoleSkillUnlockData"), "root.RoleSkillUnlockData")
    )
    class_map = reader.table_entries(
        table_ref(
            unlock_data.get("roleClassSkillListMap"),
            "root.RoleSkillUnlockData.roleClassSkillListMap",
        )
    )

    result: dict[int, list[int]] = {}
    for raw_class_id, raw_skills in class_map:
        if (
            not isinstance(raw_class_id, (int, float))
            or isinstance(raw_class_id, bool)
            or not isinstance(raw_skills, TableRef)
        ):
            continue
        class_id = int(raw_class_id)
        if not 1_200_001 <= class_id <= 1_200_007:
            continue
        profession_index = class_id - 1_200_000
        first_skill_id = 86_000_000 + profession_index * 10_000
        next_skill_id = first_skill_id + 10_000
        skill_ids = {
            int(raw_skill_id)
            for raw_skill_id, _value in reader.table_entries(raw_skills.handle)
            if isinstance(raw_skill_id, (int, float))
            and not isinstance(raw_skill_id, bool)
            and first_skill_id <= int(raw_skill_id) < next_skill_id
        }
        result[class_id] = sorted(skill_ids)
    return dict(sorted(result.items()))


def extract_profession_sources(
    reader: KSBC2Reader, root_handle: int
) -> dict[int, ProfessionSource]:
    root = reader.table_dict(root_handle)
    transfer = reader.table_dict(
        table_ref(root.get("TransferPathProfessionData"), "root.TransferPathProfessionData")
    )
    transfer_rows = reader.table_dict(
        table_ref(transfer.get("data"), "root.TransferPathProfessionData.data")
    )

    start = reader.table_dict(
        table_ref(
            root.get("ProfessionSequenceStartData"),
            "root.ProfessionSequenceStartData",
        )
    )
    start_rows = reader.table_dict(
        table_ref(start.get("data"), "root.ProfessionSequenceStartData.data")
    )
    icon_paths: dict[int, str] = {}
    for raw_class_id, raw_row in start_rows.items():
        if not isinstance(raw_row, TableRef):
            continue
        row = reader.table_dict(raw_row.handle)
        raw_icon = row.get("PathwayIcon")
        if isinstance(raw_icon, str) and raw_icon.strip():
            icon_paths[int(raw_class_id)] = raw_icon.strip()

    # The current client omits profession 1200004 from the start table, but
    # still ships its exact occupation icon path in the same decoded cache.
    arbitrator_icon = (
        "/Game/Arts/UI_2/Resource/Character_2/NotAtlas/Occupation/"
        "UI_Character_Icon_HeadArbitrator.UI_Character_Icon_HeadArbitrator"
    )
    if arbitrator_icon.encode() in reader.data:
        icon_paths[1_200_004] = arbitrator_icon

    result: dict[int, ProfessionSource] = {}
    for raw_class_id, raw_row in transfer_rows.items():
        try:
            class_id = int(raw_class_id)
        except (TypeError, ValueError):
            continue
        if not 1_200_001 <= class_id <= 1_200_007 or not isinstance(raw_row, TableRef):
            continue
        row = reader.table_dict(raw_row.handle)
        result[class_id] = ProfessionSource(
            class_id=class_id,
            localization_id=normalize_localization_id(row.get("Name")),
            icon_path=icon_paths.get(class_id),
        )
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class LocalizedCandidate:
    text: str
    node_address: int
    string_object: int
    key_index: int


class LiveLocalizationScanner:
    """Read Lua hash nodes that pair LangStrSplit numbers with UTF-8 strings."""

    MEM_PRIVATE = 0x20000
    MEM_COMMIT = 0x1000
    PAGE_GUARD = 0x100
    PAGE_NOACCESS = 0x01
    LUA_STRING_TAG_TAIL = b"\x80\xfd\xff"

    def __init__(self, pid: int):
        if sys.platform != "win32":
            raise RuntimeError("live localization scanning is Windows-only")
        from proc_inspect import (
            MEMORY_BASIC_INFORMATION,
            PROCESS_QUERY_INFORMATION,
            PROCESS_VM_READ,
            kernel32,
            read_region,
            winerror,
        )

        self._mbi_type = MEMORY_BASIC_INFORMATION
        self._kernel32 = kernel32
        self._read_region = read_region
        self._winerror = winerror
        self.pid = pid
        self.process = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
        )
        if not self.process:
            raise winerror("OpenProcess(localization scan)")

    def close(self) -> None:
        if self.process:
            self._kernel32.CloseHandle(self.process)
            self.process = None

    def __enter__(self) -> "LiveLocalizationScanner":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _decode_string_object(self, pointer: int) -> tuple[str, int] | None:
        # LuaJIT's tagged GC reference points eight bytes into this string
        # object.  The following fields are flags, key index, source hash,
        # byte length, then the UTF-8 payload.
        header = self._read_region(self.process, pointer + 8, 16)
        if not header or len(header) != 16:
            return None
        flags, key_index, _source_hash, byte_length = struct.unpack("<IIII", header)
        if flags not in (0x400, 0x401) or not 0 < byte_length <= 16_384:
            return None
        payload = self._read_region(self.process, pointer + 24, byte_length)
        if not payload or len(payload) != byte_length or b"\0" in payload:
            return None
        try:
            text = payload.decode("utf-8", "strict").strip()
        except UnicodeDecodeError:
            return None
        if not text or any(ord(char) < 0x20 for char in text):
            return None
        return text, key_index

    def scan(
        self,
        localization_ids: set[int],
        *,
        address_limit: int = 0x0000_8000_0000_0000,
    ) -> dict[int, list[LocalizedCandidate]]:
        import ctypes

        wanted_bits = {
            struct.pack("<d", float(localization_id)): localization_id
            for localization_id in localization_ids
        }
        candidates: dict[int, set[LocalizedCandidate]] = defaultdict(set)
        if not wanted_bits:
            return {}

        mbi = self._mbi_type()
        address = 0
        seen_nodes: set[int] = set()
        pointer_pairs: set[tuple[int, int, int]] = set()
        while address < address_limit:
            queried = self._kernel32.VirtualQueryEx(
                self.process,
                ctypes.c_void_p(address),
                ctypes.byref(mbi),
                ctypes.sizeof(mbi),
            )
            if not queried:
                break
            region_base = int(mbi.BaseAddress or 0)
            region_size = int(mbi.RegionSize)
            region_end = min(address_limit, region_base + region_size)
            readable_private = (
                mbi.State == self.MEM_COMMIT
                and mbi.Type == self.MEM_PRIVATE
                and not (mbi.Protect & self.PAGE_GUARD)
                and (mbi.Protect & 0xFF) not in (0, self.PAGE_NOACCESS)
            )
            if readable_private:
                chunk_address = region_base
                tail = b""
                while chunk_address < region_end:
                    chunk_size = min(16 * 1024 * 1024, region_end - chunk_address)
                    block = self._read_region(self.process, chunk_address, chunk_size)
                    if block:
                        blob = tail + block
                        origin = chunk_address - len(tail)
                        found = blob.find(self.LUA_STRING_TAG_TAIL)
                        while found >= 0:
                            # LuaJIT GC64 stores bits 32..39 of the GC pointer
                            # in the byte immediately before the stable tag
                            # tail.  Older low-address heaps used zero here,
                            # which made the original 32-bit-only decoder look
                            # correct until the game allocated Lua objects
                            # above 4 GiB.
                            if found >= 5:
                                node_offset = found - 5
                                node_address = origin + node_offset
                                if (
                                    node_address not in seen_nodes
                                    and node_offset + 16 <= len(blob)
                                ):
                                    seen_nodes.add(node_address)
                                    localization_id = wanted_bits.get(
                                        bytes(blob[node_offset + 8 : node_offset + 16])
                                    )
                                    if localization_id is not None:
                                        pointer_low = struct.unpack_from(
                                            "<I", blob, node_offset
                                        )[0]
                                        pointer_high = blob[node_offset + 4]
                                        string_pointer = pointer_low | (
                                            pointer_high << 32
                                        )
                                        pointer_pairs.add(
                                            (localization_id, string_pointer, node_address)
                                        )
                            found = blob.find(self.LUA_STRING_TAG_TAIL, found + 1)
                        tail = blob[-32:]
                    else:
                        tail = b""
                    chunk_address += chunk_size
            address = max(address + 0x1000, region_base + region_size)

        decoded_objects: dict[int, tuple[str, int] | None] = {}
        for localization_id, string_pointer, node_address in pointer_pairs:
            if string_pointer not in decoded_objects:
                decoded_objects[string_pointer] = self._decode_string_object(string_pointer)
            decoded = decoded_objects[string_pointer]
            if decoded is None:
                continue
            text, key_index = decoded
            candidates[localization_id].add(
                LocalizedCandidate(text, node_address, string_pointer, key_index)
            )

        return {
            localization_id: sorted(
                values,
                key=lambda item: (item.text, item.string_object, item.node_address),
            )
            for localization_id, values in candidates.items()
        }


def choose_names(
    sources: list[SkillSource],
    candidates: dict[int, list[LocalizedCandidate]],
) -> tuple[dict[str, str], dict[int, list[str]], list[int]]:
    names: dict[str, str] = {}
    ambiguous: dict[int, list[str]] = {}
    unresolved: set[int] = set()

    for source in sources:
        if source.direct_name:
            names[str(source.skill_id)] = source.direct_name
            continue
        if source.lang_ref is None:
            continue
        texts = sorted(
            {
                candidate.text
                for candidate in candidates.get(source.lang_ref.localization_id, [])
            }
        )
        if len(texts) == 1:
            names[str(source.skill_id)] = texts[0]
        elif len(texts) > 1:
            ambiguous[source.lang_ref.localization_id] = texts
        else:
            unresolved.add(source.lang_ref.localization_id)
    return names, ambiguous, sorted(unresolved)


def choose_profession_names(
    sources: dict[int, ProfessionSource],
    candidates: dict[int, list[LocalizedCandidate]],
) -> tuple[dict[str, str], list[int]]:
    names: dict[str, str] = {}
    unresolved: list[int] = []
    for class_id, source in sources.items():
        localization_id = source.localization_id
        if localization_id is None:
            unresolved.append(class_id)
            continue
        texts = sorted(
            {candidate.text for candidate in candidates.get(localization_id, [])}
        )
        if len(texts) == 1:
            names[str(class_id)] = texts[0]
        elif localization_id in PROFESSION_NAME_SNAPSHOT:
            names[str(class_id)] = PROFESSION_NAME_SNAPSHOT[localization_id]
        else:
            unresolved.append(class_id)
    return names, unresolved


def build_skill_metadata(
    sources: list[SkillSource],
    profession_sources: dict[int, ProfessionSource],
    profession_skills: dict[int, list[int]],
    profession_names: dict[str, str],
) -> dict[str, object]:
    skill_to_professions: dict[int, list[int]] = defaultdict(list)
    for class_id, skill_ids in profession_skills.items():
        for skill_id in skill_ids:
            skill_to_professions[skill_id].append(class_id)
    skill_metadata: dict[str, dict[str, object]] = {}
    for source in sources:
        value: dict[str, object] = {}
        if source.icon_path:
            value["icon_path"] = source.icon_path
        class_ids = skill_to_professions.get(source.skill_id, [])
        if class_ids:
            value["profession_ids"] = class_ids
        if value:
            skill_metadata[str(source.skill_id)] = value
    return {
        "professions": {
            str(class_id): {
                "name": profession_names.get(str(class_id), ""),
                "icon_path": source.icon_path or "",
                "skill_ids": profession_skills.get(class_id, []),
            }
            for class_id, source in profession_sources.items()
        },
        "skills": skill_metadata,
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def integer(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--root", type=integer, default=DEFAULT_ROOT_HANDLE)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--output", type=Path, default=Path("skill_names.json"))
    parser.add_argument("--metadata", type=Path, default=Path("skill_metadata.json"))
    parser.add_argument(
        "--diagnostics", type=Path, default=Path("skill_names_diagnostics.json")
    )
    parser.add_argument(
        "--no-live-scan",
        action="store_true",
        help="only parse KSBC2 references; do not resolve loaded Chinese strings",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="reuse the existing name catalog and only rebuild client metadata",
    )
    args = parser.parse_args()

    if not args.cache.is_file():
        raise SystemExit(f"KSBC2 cache not found: {args.cache}")

    reader = KSBC2Reader.from_path(args.cache)
    sources = extract_skill_sources(reader, args.root)
    profession_skills = extract_profession_skill_ids(reader, args.root)
    profession_sources = extract_profession_sources(reader, args.root)
    if args.metadata_only:
        try:
            existing_names = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise SystemExit(f"could not reuse {args.output}: {exc}") from exc
        if not isinstance(existing_names, dict) or len(existing_names) < 2:
            raise SystemExit(f"existing skill catalog is not valid: {args.output}")
        profession_names, unresolved_professions = choose_profession_names(
            profession_sources, {}
        )
        metadata = build_skill_metadata(
            sources, profession_sources, profession_skills, profession_names
        )
        write_json(args.metadata, metadata)
        print(
            f"reused={len(existing_names)} metadata_skills="
            f"{len(metadata['skills'])} professions={len(profession_names)} "
            f"unresolved_professions={len(unresolved_professions)}"
        )
        print(f"wrote {args.metadata}")
        return 0
    skill_localization_ids = {
        source.lang_ref.localization_id
        for source in sources
        if source.lang_ref is not None
    }
    profession_localization_ids = {
        source.localization_id
        for source in profession_sources.values()
        if source.localization_id is not None
    }
    localization_ids = skill_localization_ids | profession_localization_ids
    candidates: dict[int, list[LocalizedCandidate]] = {}
    pid = args.pid
    if not args.no_live_scan:
        if pid is None:
            from proc_inspect import find_pid

            pid = find_pid(args.process)
        with LiveLocalizationScanner(pid) as scanner:
            candidates = scanner.scan(localization_ids)
        if localization_ids and not candidates:
            raise SystemExit(
                "live localization scan returned no matches; "
                "existing output files were left unchanged"
            )

    names, ambiguous, unresolved = choose_names(sources, candidates)
    profession_names, unresolved_professions = choose_profession_names(
        profession_sources, candidates
    )
    write_json(args.output, names)

    metadata = build_skill_metadata(
        sources, profession_sources, profession_skills, profession_names
    )
    write_json(args.metadata, metadata)

    resolved_skill_ids = {int(skill_id) for skill_id in names}
    profession_coverage: dict[str, dict[str, object]] = {}
    profession_total = 0
    profession_resolved = 0
    profession_missing: list[int] = []
    for class_id, skill_ids in profession_skills.items():
        missing = [skill_id for skill_id in skill_ids if skill_id not in resolved_skill_ids]
        resolved = len(skill_ids) - len(missing)
        profession_coverage[str(class_id)] = {
            "total": len(skill_ids),
            "resolved": resolved,
            "missing_skill_ids": missing,
        }
        profession_total += len(skill_ids)
        profession_resolved += resolved
        profession_missing.extend(missing)

    namespaces: dict[str, int] = defaultdict(int)
    for source in sources:
        if source.lang_ref:
            namespaces[source.lang_ref.namespace] += 1
    diagnostics = {
        "cache": str(args.cache),
        "root_handle": f"0x{args.root:08x}",
        "pid": pid,
        "skill_rows": len(sources),
        "skill_rows_with_lang_ref": sum(source.lang_ref is not None for source in sources),
        "unique_localization_ids": len(localization_ids),
        "unique_skill_localization_ids": len(skill_localization_ids),
        "unique_profession_localization_ids": len(profession_localization_ids),
        "resolved_localization_ids": sum(
            len({candidate.text for candidate in values}) == 1
            for values in candidates.values()
        ),
        "exported_skill_names": len(names),
        "profession_skill_summary": {
            "professions": len(profession_skills),
            "total": profession_total,
            "resolved": profession_resolved,
            "missing_skill_ids": sorted(profession_missing),
        },
        "profession_skill_coverage": profession_coverage,
        "profession_names": profession_names,
        "unresolved_profession_ids": unresolved_professions,
        "namespaces": dict(sorted(namespaces.items())),
        "ambiguous": {str(key): value for key, value in sorted(ambiguous.items())},
        "unresolved_localization_ids": unresolved,
    }
    write_json(args.diagnostics, diagnostics)

    print(
        f"skills={len(sources)} lang_refs={diagnostics['skill_rows_with_lang_ref']} "
        f"unique_lang_ids={len(localization_ids)} resolved_ids="
        f"{diagnostics['resolved_localization_ids']} exported={len(names)} "
        f"ambiguous={len(ambiguous)} unresolved={len(unresolved)}"
    )
    print(f"wrote {args.output}, {args.metadata}, and {args.diagnostics}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
