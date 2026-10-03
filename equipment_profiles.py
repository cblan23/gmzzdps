#!/usr/bin/env python3
"""Live teammate equipment queries with append-only local evidence.

The UI never reads this archive as a cache.  Every displayed profile must come
from a response correlated to the current process, character, party session,
and roster.  A transport/capture session may rotate during a scene change and
is retained only for diagnostics.  The archive intentionally keeps older and rejected
responses so protocol and equipment metadata can be improved without asking a
player to reproduce the same team again.
"""

from __future__ import annotations

import base64
import ctypes
import datetime as dt
from copy import deepcopy
import hashlib
import json
import math
import os
import queue
import re
import struct
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from pathlib import Path

from proc_inspect import (
    MEM_COMMIT,
    MEMORY_BASIC_INFORMATION,
    PAGE_GUARD,
    PAGE_NOACCESS,
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_module,
    kernel32,
    read_region,
    snapshot,
    winerror,
)
from runtime_capability import runtime_profile_hook


REQUEST_METHOD = "ReqOtherRoleShapeData"
RESPONSE_METHOD = "RetOtherRoleShapeData"
REQUEST_METHOD_ID = 313
RESPONSE_METHOD_ID = 352
EXPECTED_ARGUMENT_DESCRIPTORS = (("ListStr", 5), ("int", 1))
ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE = 0x80
ROLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{10,32}$")
MAX_TEAM_QUERY_TOKENS = 12
EQUIPMENT_QUERY_DEDUP_SECONDS = 20.0
EQUIPMENT_QUERY_COALESCE_SECONDS = 0.12

# Current C7 Lua API entry points.  The call_server entry itself continues to
# come from the signed runtime profile and is signature checked before use.
LUA_CREATETABLE_RVA = 0x09604650
LUA_PUSHLSTRING_RVA = 0x09605540
LUA_PUSHNUMBER_RVA = 0x096055E0
LUA_RAWSETI_RVA = 0x09605920
LUA_SETTOP_RVA = 0x09605F50

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
THREAD_SUSPEND_RESUME = 0x0002
THREAD_QUERY_INFORMATION = 0x0040
TH32CS_SNAPTHREAD = 0x00000004
MEM_PRIVATE = 0x00020000
MEM_RESERVE = 0x00002000
MEM_RELEASE = 0x00008000
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
STILL_ACTIVE = 259
MAX_USER_ADDRESS = (1 << 47) - 1

STATE_MAGIC = b"GMZZSHP2"
STATE_SIZE = 0x800
CODE_SIZE = 0x1800
TRAMPOLINE_OFFSET = 0x1000
STATE_ARM = 0x08
STATE_COUNT = 0x10
STATE_RESULT = 0x18
STATE_ORIGINAL_TOP = 0x28
STATE_ORIGINAL_BASE = 0x30
STATE_ACTIVE_LUA = 0x38
STATE_CONTEXT = 0x80
STATE_VARIADIC = 0x100
STATE_TOKEN_DATA = 0x200
ARM_IDLE = 0
ARM_READY = 1
ARM_CLAIMED = 2
ARM_DONE = 3
TRANSIENT_THREAD_ERRORS = frozenset({5, 6, 87})


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


_thread_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_thread32_first = _thread_kernel32.Thread32First
_thread32_first.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(THREADENTRY32),
]
_thread32_first.restype = wintypes.BOOL
_thread32_next = _thread_kernel32.Thread32Next
_thread32_next.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(THREADENTRY32),
]
_thread32_next.restype = wintypes.BOOL


# The request stub always starts by saving flags/registers and then loading the
# address of its private state block into r15.  These bytes let a later process
# instance distinguish our own abandoned hook from an unrelated modification.
REQUEST_STUB_STATE_PREFIX = (
    b"\x9c\x50\x53\x51\x52\x56\x57"
    b"\x41\x50\x41\x51\x41\x52\x41\x53"
    b"\x41\x54\x41\x55\x41\x56\x41\x57"
    b"\x49\xbf"
)

# Stack offsets after pushfq and fourteen register pushes.
SAVED_R9 = 0x30
SAVED_R8 = 0x38
SAVED_RDX = 0x50
SAVED_RCX = 0x58
SAVED_ENTRY_STACK_ARG0 = 0xA0

DESCRIPTOR_PATTERN = bytes.fromhex(
    "03 00 00 00 01 00 00 00 "
    "00 01 00 01 00 00 00 00 "
    "39 01 00 00 00 00 00 00"
)
PREFERRED_DESCRIPTOR_START = 0x0000_0005_0000_0000
PREFERRED_DESCRIPTOR_END = 0x0000_0006_0000_0000
PVP_ITEM_TAG = 2
ACTIVE_WORD_CLASS_TYPE = 1
EQUIPMENT_QUALITY_NAMES = {
    6: "黄色品质",
    7: "红色品质",
}
FIGHT_PROP_NAMES = {
    "AirShield_N": "护盾",
    "AllProHurtPlus_N": "全职业伤害增加",
    "AllProHurtReduce_N": "全职业伤害降低",
    "AllRaceHurtPlus_N": "全种族伤害增加",
    "Atk_N": "攻击力",
    "BeSkilledReduce_N": "受技能伤害降低",
    "Block_N": "格挡",
    "CritAnti_N": "抗暴",
    "Crit_N": "暴击",
    "DefReduce_N": "防御削减",
    "Def_N": "防御",
    "MaxHp_N": "最大生命",
    "Pierce_N": "穿透",
    "ShieldBreak_N": "破盾",
    "SkillPlus_N": "技能增伤",
}

# The wire response uses the game's sparse equipment-body slot IDs, not a
# compact 1..8 list.  These IDs line up with EquipmentGrowBodyConfigData and
# the order shown by the live equipment panel.
EQUIPMENT_SLOT_NAMES = {
    1: "武器",
    2: "胸针",
    5: "指环",
    6: "护符",
    7: "护甲",
    9: "鞋靴",
    10: "帽子",
    12: "披风",
}

# Confirmed against the live client localization IDs carried by these item
# templates. Keep unknown templates unnamed rather than inventing an item name.
EQUIPMENT_ITEM_NAMES = {
    3010455: "知识之乡",
    3060643: "裂金之战刃",
    3200643: "元素秘纹符",
    3210641: "镜像之自我",
    3240557: "不曾遗忘的守护",
    3240643: "贤者石之环",
    3250557: "不再流浪的期许",
    3250644: "熔锻之战冠",
    3270643: "炼药的魔袍",
    3280643: "逐金之行者",
    3300555: "不甘命运的复仇",
    3300643: "秘术之战铠",
}
EQUIPMENT_ITEM_NAME_LOCALIZATION_IDS = {
    3010455: 440_011_883_398_400,
    3060643: 440_011_883_341_056,
    3200643: 440_011_883_351_808,
    3210641: 440_011_883_417_856,
    3240557: 440_011_883_399_424,
    3240643: 440_011_883_363_584,
    3250557: 440_011_883_399_680,
    3250644: 440_011_883_370_240,
    3270643: 440_011_883_376_384,
    3280643: 440_011_883_381_760,
    3300555: 440_011_883_399_936,
    3300643: 440_011_883_388_160,
}
EQUIPMENT_ITEM_NAME_CACHE_SCHEMA_VERSION = 1


def equipment_item_name(
    item_id: object,
    localization_id: object | None = None,
) -> str:
    try:
        parsed_item_id = int(item_id)
    except (TypeError, ValueError, OverflowError):
        return ""
    name = EQUIPMENT_ITEM_NAMES.get(parsed_item_id, "")
    if localization_id is None or not name:
        return name
    try:
        current_localization_id = int(localization_id)
    except (TypeError, ValueError, OverflowError):
        return ""
    return (
        name
        if EQUIPMENT_ITEM_NAME_LOCALIZATION_IDS.get(parsed_item_id)
        == current_localization_id
        else ""
    )


def load_equipment_item_name_cache(
    path: Path,
    item_metadata: Mapping[int, Mapping[str, object]],
) -> dict[int, str]:
    """Load names only when their localization IDs still match ItemNewData."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    raw_items = payload.get("items", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(raw_items, Mapping):
        return {}
    names: dict[int, str] = {}
    for raw_item_id, raw_value in raw_items.items():
        if not isinstance(raw_value, Mapping):
            continue
        item_id = _positive_int(raw_item_id)
        metadata = item_metadata.get(item_id)
        if not item_id or not isinstance(metadata, Mapping):
            continue
        localization_id = _positive_int(raw_value.get("localization_id"))
        if localization_id != _positive_int(metadata.get("item_name_id")):
            continue
        name = str(raw_value.get("name", "") or "").strip()[:128]
        if name:
            names[item_id] = name
    return names


def write_equipment_item_name_cache(
    path: Path,
    item_metadata: Mapping[int, Mapping[str, object]],
    item_names: Mapping[int, str],
) -> None:
    rows: dict[str, dict[str, object]] = {}
    for raw_item_id, raw_name in item_names.items():
        item_id = _positive_int(raw_item_id)
        metadata = item_metadata.get(item_id)
        localization_id = (
            _positive_int(metadata.get("item_name_id"))
            if isinstance(metadata, Mapping)
            else 0
        )
        name = str(raw_name or "").strip()[:128]
        if item_id and localization_id and name:
            rows[str(item_id)] = {
                "localization_id": localization_id,
                "name": name,
            }
    payload = {
        "schema_version": EQUIPMENT_ITEM_NAME_CACHE_SCHEMA_VERSION,
        "items": rows,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def scan_equipment_item_names(
    pid: int,
    item_metadata: Mapping[int, Mapping[str, object]],
    existing_names: Mapping[int, str],
) -> dict[int, str]:
    """Resolve missing current-client names in one background memory scan."""

    from ksbc2_skill_names import LiveLocalizationScanner

    names = {
        int(item_id): str(name).strip()[:128]
        for item_id, name in existing_names.items()
        if _positive_int(item_id) and str(name).strip()
    }
    localization_items: dict[int, list[int]] = {}
    for raw_item_id, metadata in item_metadata.items():
        item_id = _positive_int(raw_item_id)
        localization_id = (
            _positive_int(metadata.get("item_name_id"))
            if isinstance(metadata, Mapping)
            else 0
        )
        if item_id and localization_id and item_id not in names:
            localization_items.setdefault(localization_id, []).append(item_id)
    if not localization_items:
        return names
    with LiveLocalizationScanner(int(pid)) as scanner:
        candidates = scanner.scan(set(localization_items))
    for localization_id, item_ids in localization_items.items():
        texts = {
            candidate.text.strip()
            for candidate in candidates.get(localization_id, [])
            if candidate.text.strip()
        }
        if len(texts) != 1:
            continue
        name = next(iter(texts))[:128]
        for item_id in item_ids:
            names[item_id] = name
    return names


def equipment_slot_name(slot: object) -> str:
    """Return a display label without discarding an unknown wire slot."""

    try:
        parsed = int(slot or 0)
    except (TypeError, ValueError, OverflowError):
        parsed = 0
    return EQUIPMENT_SLOT_NAMES.get(parsed, f"槽位 {parsed}" if parsed else "未知槽位")


def _compact_wire_value(value: object, depth: int = 0) -> object:
    """Keep response details useful for inspection without archiving huge blobs."""

    if depth >= 4:
        return "..."
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 32:
                break
            result[str(key)] = _compact_wire_value(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_compact_wire_value(item, depth + 1) for item in list(value)[:32]]
    return str(value)[:256]


def _word_score_values(value: object) -> list[int]:
    """Read per-word scores only when the wire response actually supplies them."""

    if isinstance(value, Mapping):
        values = [item for _key, item in _indexed_items(value)]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return []
    return [_positive_int(item) for item in values]


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
kernel32.FlushInstructionCache.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_size_t,
]
kernel32.FlushInstructionCache.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, bytes):
        return {
            "$bytes_base64": base64.b64encode(value).decode("ascii"),
            "$length": len(value),
        }
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    code = getattr(value, "code", None)
    data = getattr(value, "data", None)
    if isinstance(code, int) and isinstance(data, bytes):
        return {
            "$msgpack_ext_code": code,
            "$bytes_base64": base64.b64encode(data).decode("ascii"),
            "$length": len(data),
        }
    return {"$type": type(value).__name__, "$repr": repr(value)}


class EquipmentQueryArchive:
    """Append-only JSONL evidence.  This class has no read/delete API."""

    def __init__(self, data_dir: Path):
        self.directory = Path(data_dir) / "equipment_queries"
        self.lock = threading.Lock()

    def append(self, event: str, **fields: object) -> Path:
        now = dt.datetime.now().astimezone()
        record = {
            "schema_version": 1,
            "archived_at": now.isoformat(timespec="microseconds"),
            "event": str(event),
            **fields,
        }
        payload = json.dumps(
            _json_safe(record),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        path = self.directory / f"equipment_queries_{now:%Y%m%d}.jsonl"
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return path


def _wire_key(value: object) -> object:
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+", stripped):
            try:
                return int(stripped)
            except ValueError:
                pass
    return value


def expand_wire_value(value: object) -> object:
    """Expand legacy $map wrappers while retaining every nested value."""

    if isinstance(value, list):
        return [expand_wire_value(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    pairs = value.get("$map")
    if isinstance(pairs, list):
        expanded: dict[object, object] = {}
        valid = True
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                valid = False
                break
            expanded[_wire_key(pair[0])] = expand_wire_value(pair[1])
        if valid:
            extras = {
                str(key): expand_wire_value(item)
                for key, item in value.items()
                if key != "$map"
            }
            if extras:
                expanded["$wire_metadata"] = extras
            return expanded
    return {
        _wire_key(key): expand_wire_value(item)
        for key, item in value.items()
    }


def _field(value: object, key: int, default=None):
    if not isinstance(value, Mapping):
        return default
    for candidate in (key, str(key), float(key)):
        if candidate in value:
            return value[candidate]
    return default


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed > 0 else 0


def _nonnegative_int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _indexed_items(value: object) -> list[tuple[object, object]]:
    if isinstance(value, Mapping):
        def order(pair):
            try:
                return (0, int(pair[0]))
            except (TypeError, ValueError, OverflowError):
                return (1, str(pair[0]))

        return sorted(value.items(), key=order)
    if isinstance(value, list):
        return list(enumerate(value, start=1))
    return []


def _integer_list(value: object) -> list[int]:
    if isinstance(value, Mapping):
        values = [item for _key, item in _indexed_items(value)]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    return [parsed for item in values if (parsed := _positive_int(item))]


def parse_shape_response(record: Mapping[str, object]) -> dict[str, object]:
    arguments = expand_wire_value(record.get("decoded_arguments", []))
    if not isinstance(arguments, list) or not arguments:
        raise ValueError("RetOtherRoleShapeData has no decoded arguments")
    raw_profiles = arguments[0]
    if not isinstance(raw_profiles, Mapping):
        raise ValueError("RetOtherRoleShapeData profile map is missing")
    correlation = _positive_int(arguments[1] if len(arguments) > 1 else 0)
    profiles: list[dict[str, object]] = []
    for raw_token, raw_profile in raw_profiles.items():
        token = str(raw_token or "").strip()
        if not ROLE_TOKEN_RE.fullmatch(token) or not isinstance(raw_profile, Mapping):
            continue
        equipment_rows: list[dict[str, object]] = []
        appearance = _field(raw_profile, 25, {})
        equipment = _field(raw_profile, 11, {})
        for raw_slot, raw_item in _indexed_items(equipment):
            if not isinstance(raw_item, Mapping):
                continue
            try:
                slot = int(raw_slot)
            except (TypeError, ValueError, OverflowError):
                slot = 0
            item_id = _positive_int(_field(raw_item, 2, 0))
            details = _field(raw_item, 12, {})
            base_data = _field(details, 0, {})
            random_data = _field(details, 1, {})
            word_ids = _integer_list(_field(random_data, 0, []))
            raw_word_scores = _field(random_data, 3, _field(random_data, 4, []))
            word_scores = _word_score_values(raw_word_scores)
            equipment_rows.append(
                {
                    "slot": slot,
                    "slot_name": equipment_slot_name(slot),
                    "item_id": item_id,
                    "quality": _positive_int(_field(raw_item, 8, 0)),
                    "base_score": _positive_int(_field(base_data, 1, 0)),
                    "word_ids": word_ids,
                    "auxiliary_id": _positive_int(_field(random_data, 1, 0)),
                    "word_score": _positive_int(_field(random_data, 2, 0)),
                    "word_scores": word_scores,
                    "details": _compact_wire_value(details),
                    "raw_item": raw_item,
                }
            )
        profiles.append(
            {
                "user_token": token,
                "name": str(_field(raw_profile, 0, "") or "").strip()[:64],
                "role_number": _positive_int(_field(raw_profile, 1, 0)),
                "profession_id": _positive_int(_field(raw_profile, 2, 0)),
                "level": _positive_int(_field(raw_profile, 4, 0)),
                "extraordinary_rating": _positive_int(_field(raw_profile, 19, 0)),
                "avatar_id": _positive_int(_field(appearance, 0, 0)),
                "avatar_frame_id": _positive_int(_field(appearance, 1, 0)),
                "combat_attributes": _compact_wire_value(
                    _field(raw_profile, 12, None)
                ),
                "equipment_count": sum(
                    bool(row.get("item_id")) for row in equipment_rows
                ),
                "equipment": equipment_rows,
                "raw_profile": raw_profile,
            }
        )
    return {
        "correlation": correlation,
        "profiles": profiles,
        "decoded_arguments": arguments,
    }


@dataclass(frozen=True)
class EquipmentMetadataCatalog:
    source_path: str
    source_sha256: str
    item_metadata: dict[int, dict[str, object]]
    word_class_types: dict[int, int]
    word_metadata: dict[int, dict[str, object]] = field(default_factory=dict)
    convergence_metadata: dict[int, dict[str, object]] = field(default_factory=dict)
    body_grow_ids: dict[tuple[int, int], int] = field(default_factory=dict)
    enhance_schedules: dict[int, tuple[dict[str, int], ...]] = field(
        default_factory=dict
    )
    item_names: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_game_module(
        cls,
        module_path: str,
        *,
        item_name_cache_path: Path | None = None,
    ) -> "EquipmentMetadataCatalog":
        from ksbc2_skill_names import (
            KSBC2Reader,
            TableRef,
            discover_root_handle,
            table_ref,
        )

        game_root = Path(module_path).resolve().parents[2]
        cache_dir = game_root / "Saved" / "kscache" / "14"
        candidates = list(cache_dir.glob("4efadcdd4c7bb254c65f6f07_*"))
        if not candidates:
            raise FileNotFoundError(f"C7 metadata cache not found under {cache_dir}")
        source = max(
            candidates,
            key=lambda path: (
                int(path.name.rpartition("_")[2])
                if path.name.rpartition("_")[2].isdigit()
                else -1,
                path.stat().st_mtime_ns,
            ),
        )
        encoded = source.read_bytes()
        reader = KSBC2Reader(encoded)
        root = reader.table_dict(discover_root_handle(reader))

        def resolved_table(value: object) -> dict[object, object]:
            if not isinstance(value, TableRef):
                return {}
            return reader.table_dict(value.handle)

        def first_scalar(value: object) -> object | None:
            if isinstance(value, TableRef):
                value = reader.table_dict(value.handle)
            if isinstance(value, Mapping):
                for _key, nested in _indexed_items(value):
                    resolved = first_scalar(nested)
                    if resolved is not None:
                        return resolved
                return None
            if isinstance(value, (list, tuple)):
                for nested in value:
                    resolved = first_scalar(nested)
                    if resolved is not None:
                        return resolved
                return None
            return value

        def fight_properties(value: object) -> list[dict[str, object]]:
            properties = []
            for raw_key, raw_value in resolved_table(value).items():
                key = str(raw_key or "").strip()
                if not key:
                    continue
                properties.append(
                    {
                        "key": key,
                        "name": FIGHT_PROP_NAMES.get(key, key),
                        "value": first_scalar(raw_value),
                    }
                )
            return properties

        def convergence_properties(value: object) -> list[dict[str, object]]:
            properties = []
            for _index, raw_property in _indexed_items(resolved_table(value)):
                row = resolved_table(raw_property)
                key = str(_field(row, 1, "") or "").strip()
                if not key:
                    continue
                properties.append(
                    {
                        "key": key,
                        "name": FIGHT_PROP_NAMES.get(key, key),
                        "value": _field(row, 2, None),
                    }
                )
            return properties

        item_container = reader.table_dict(
            table_ref(root.get("ItemNewData"), "root.ItemNewData")
        )
        item_rows = reader.table_entries(
            table_ref(item_container.get("data"), "root.ItemNewData.data")
        )
        item_metadata: dict[int, dict[str, object]] = {}
        for raw_item_id, raw_row in item_rows:
            if not isinstance(raw_row, TableRef):
                continue
            item_id = _positive_int(raw_item_id)
            if not item_id:
                continue
            row = reader.table_dict(raw_row.handle)
            tag = _positive_int(row.get("Tag"))
            # Only equippable rows carry the stable combat-mode tags needed by
            # the HUD.  Unknown item IDs remain archived and classify as zero.
            if tag not in (1, 2):
                continue
            item_metadata[item_id] = {
                "tag": tag,
                "mode": "pvp" if tag == PVP_ITEM_TAG else "adventure",
                "tag_name_id": _positive_int(row.get("TagName")),
                "item_name_id": _positive_int(row.get("itemName")),
                "item_description_id": _positive_int(row.get("itemDes")),
                "icon": str(row.get("icon", "") or ""),
                "quality": _positive_int(row.get("quality")),
                "base_score": _positive_int(row.get("Mark")),
                "item_level": _positive_int(row.get("TC")),
                "required_level": _positive_int(row.get("lvReq")),
                "sub_type": _positive_int(row.get("subType")),
                "season_id": _positive_int(row.get("SeasonID")),
                "random_group": str(row.get("RandomGroup", "") or ""),
            }

        body_config_container = reader.table_dict(
            table_ref(
                root.get("EquipmentGrowBodyConfigData"),
                "root.EquipmentGrowBodyConfigData",
            )
        )
        body_config_rows = reader.table_entries(
            table_ref(
                body_config_container.get("data"),
                "root.EquipmentGrowBodyConfigData.data",
            )
        )
        body_grow_ids: dict[tuple[int, int], int] = {}
        for raw_grow_id, raw_row in body_config_rows:
            if not isinstance(raw_row, TableRef):
                continue
            grow_id = _positive_int(raw_grow_id)
            row = reader.table_dict(raw_row.handle)
            season_id = _positive_int(row.get("Season"))
            raw_slots = row.get("Slot")
            if not grow_id or not season_id or not isinstance(raw_slots, TableRef):
                continue
            for _index, raw_slot in reader.table_entries(raw_slots.handle):
                slot = _positive_int(raw_slot)
                if slot:
                    body_grow_ids[(season_id, slot)] = grow_id

        enhance_container = reader.table_dict(
            table_ref(
                root.get("EquipmentGrowBodyEnhanceData"),
                "root.EquipmentGrowBodyEnhanceData",
            )
        )
        enhance_rows = reader.table_entries(
            table_ref(
                enhance_container.get("data"),
                "root.EquipmentGrowBodyEnhanceData.data",
            )
        )
        enhance_schedules: dict[int, tuple[dict[str, int], ...]] = {}
        for raw_grow_id, raw_stages in enhance_rows:
            grow_id = _positive_int(raw_grow_id)
            if not grow_id or not isinstance(raw_stages, TableRef):
                continue
            stages: list[tuple[int, int]] = []
            for raw_stage, raw_stage_row in reader.table_entries(
                raw_stages.handle
            ):
                if not isinstance(raw_stage_row, TableRef):
                    continue
                stage_row = reader.table_dict(raw_stage_row.handle)
                level = _positive_int(stage_row.get("StageID")) or _positive_int(
                    raw_stage
                )
                score = _positive_int(stage_row.get("Mark"))
                if level and score:
                    stages.append((level, score))
            cumulative_score = 0
            schedule: list[dict[str, int]] = []
            for level, score in sorted(stages):
                cumulative_score += score
                schedule.append(
                    {
                        "level": level,
                        "score": score,
                        "cumulative_score": cumulative_score,
                    }
                )
            if schedule:
                enhance_schedules[grow_id] = tuple(schedule)

        word_container = reader.table_dict(
            table_ref(
                root.get("EquipmentWordRandomClassData"),
                "root.EquipmentWordRandomClassData",
            )
        )
        raw_word_types = reader.table_dict(
            table_ref(
                word_container.get("EquipRandomWordID2ClassType"),
                "root.EquipmentWordRandomClassData.EquipRandomWordID2ClassType",
            )
        )
        word_types = {
            word_id: word_type
            for raw_word_id, raw_word_type in raw_word_types.items()
            if (word_id := _positive_int(raw_word_id))
            and (word_type := _positive_int(raw_word_type))
        }

        word_data_container = reader.table_dict(
            table_ref(
                root.get("EquipmentWordRandomWordData"),
                "root.EquipmentWordRandomWordData",
            )
        )
        word_data_rows = reader.table_entries(
            table_ref(
                word_data_container.get("data"),
                "root.EquipmentWordRandomWordData.data",
            )
        )
        word_metadata: dict[int, dict[str, object]] = {}
        for raw_word_id, raw_row in word_data_rows:
            if not isinstance(raw_row, TableRef):
                continue
            word_id = _positive_int(raw_word_id)
            if not word_id:
                continue
            row = reader.table_dict(raw_row.handle)
            properties = fight_properties(row.get("FightProp"))
            word_metadata[word_id] = {
                "effect_type": str(row.get("EffectType", "") or ""),
                "score": _positive_int(row.get("Mark")) or None,
                "properties": properties,
            }

        skill_names: dict[int, str] = {}
        try:
            raw_skill_names = json.loads(
                Path(__file__).with_name("skill_names.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, TypeError, ValueError):
            raw_skill_names = {}
        if isinstance(raw_skill_names, Mapping):
            for raw_skill_id, raw_name in raw_skill_names.items():
                skill_id = _positive_int(raw_skill_id)
                name = str(raw_name or "").strip()
                if skill_id and name:
                    skill_names[skill_id] = name

        convergence_container = reader.table_dict(
            table_ref(
                root.get("EquipmentSpiritualityConvergenceData"),
                "root.EquipmentSpiritualityConvergenceData",
            )
        )
        convergence_rows = reader.table_entries(
            table_ref(
                convergence_container.get("data"),
                "root.EquipmentSpiritualityConvergenceData.data",
            )
        )
        convergence_metadata: dict[int, dict[str, object]] = {}
        for raw_auxiliary_id, raw_row in convergence_rows:
            if not isinstance(raw_row, TableRef):
                continue
            auxiliary_id = _positive_int(raw_auxiliary_id)
            if not auxiliary_id:
                continue
            row = reader.table_dict(raw_row.handle)
            passive_skill_ids = _integer_list(
                resolved_table(row.get("PassiveSkill1"))
            )
            display_name = next(
                (
                    skill_names[skill_id]
                    for skill_id in passive_skill_ids
                    if skill_id in skill_names
                ),
                "",
            )
            convergence_metadata[auxiliary_id] = {
                "auxiliary_id": auxiliary_id,
                "name": display_name or f"特殊属性 {auxiliary_id}",
                "name_id": _positive_int(row.get("Name")) or None,
                "score": _positive_int(row.get("Score")) or None,
                "icon": str(row.get("Icon", "") or ""),
                "passive_skill_ids": passive_skill_ids,
                "properties": convergence_properties(row.get("Prop1")),
            }
        item_names = (
            load_equipment_item_name_cache(item_name_cache_path, item_metadata)
            if item_name_cache_path is not None
            else {}
        )
        for item_id, metadata in item_metadata.items():
            static_name = equipment_item_name(
                item_id,
                metadata.get("item_name_id"),
            )
            if static_name:
                item_names.setdefault(item_id, static_name)
        return cls(
            source_path=str(source),
            source_sha256=hashlib.sha256(encoded).hexdigest(),
            item_metadata=item_metadata,
            word_class_types=word_types,
            item_names=item_names,
            word_metadata=word_metadata,
            convergence_metadata=convergence_metadata,
            body_grow_ids=body_grow_ids,
            enhance_schedules=enhance_schedules,
        )

    def enrich(
        self,
        profile: Mapping[str, object],
        exact_scores: Mapping[int, Mapping[str, object]] | None = None,
    ) -> dict[str, object]:
        result = dict(profile)
        enriched_equipment: list[dict[str, object]] = []
        pvp_count = 0
        active_words = 0
        total_words = 0
        raw_equipment = profile.get("equipment", [])
        for raw_item in raw_equipment if isinstance(raw_equipment, list) else []:
            if not isinstance(raw_item, Mapping):
                continue
            item = dict(raw_item)
            item_id = _positive_int(item.get("item_id"))
            metadata = dict(self.item_metadata.get(item_id, {}))
            item_name = str(self.item_names.get(item_id, "") or "").strip()
            if not item_name:
                item_name = equipment_item_name(
                    item_id,
                    metadata.get("item_name_id"),
                )
            if item_name:
                metadata["item_name"] = item_name
                item["item_name"] = item_name
            word_ids = _integer_list(item.get("word_ids"))
            word_classes = [
                int(self.word_class_types.get(word_id, 0) or 0)
                for word_id in word_ids
            ]
            word_scores = _word_score_values(item.get("word_scores"))
            aggregate_word_score = _positive_int(item.get("word_score"))
            base_score = (
                _positive_int(item.get("base_score"))
                or _positive_int(metadata.get("base_score"))
            )
            random_score = aggregate_word_score
            slot = _positive_int(item.get("slot"))
            exact = dict((exact_scores or {}).get(slot, {}))
            if _positive_int(exact.get("item_id")) != item_id:
                exact = {}
            exact_base_score = _positive_int(exact.get("base_score"))
            exact_random_score = _nonnegative_int_or_none(
                exact.get("random_score")
            )
            if exact_base_score:
                base_score = exact_base_score
            if exact_random_score is not None:
                random_score = exact_random_score
            enhance_score = _nonnegative_int_or_none(exact.get("enhance_score"))
            enhance_level = _nonnegative_int_or_none(exact.get("enhance_level"))
            enhance_completed_level = _nonnegative_int_or_none(
                exact.get("enhance_completed_level")
            )
            enhance_level_progress_percent = _nonnegative_int_or_none(
                exact.get("enhance_level_progress_percent")
            )
            enhance_overall_percent = _nonnegative_int_or_none(
                exact.get("enhance_overall_percent")
            )
            enhance_stage_count = _nonnegative_int_or_none(
                exact.get("enhance_stage_count")
            )
            enhance_stages: list[dict[str, int]] = []
            raw_enhance_stages = exact.get("enhance_stages")
            if isinstance(raw_enhance_stages, (list, tuple)):
                for raw_stage in raw_enhance_stages:
                    if not isinstance(raw_stage, Mapping):
                        continue
                    stage = _positive_int(raw_stage.get("stage"))
                    if not stage:
                        continue
                    enhance_stages.append(
                        {
                            "stage": stage,
                            "level": _nonnegative_int_or_none(
                                raw_stage.get("level")
                            )
                            or 0,
                            "percent": min(
                                100,
                                _nonnegative_int_or_none(
                                    raw_stage.get("percent")
                                )
                                or 0,
                            ),
                        }
                    )
            if enhance_stages:
                if enhance_completed_level is None:
                    enhance_completed_level = sum(
                        int(stage.get("percent", 0)) >= 100
                        for stage in enhance_stages
                    )
                if enhance_level is None:
                    enhance_level = max(
                        (
                            int(stage.get("stage", 0) or 0)
                            for stage in enhance_stages
                            if int(stage.get("percent", 0) or 0) > 0
                        ),
                        default=enhance_completed_level or 0,
                    )
                current_stage = next(
                    (
                        stage
                        for stage in enhance_stages
                        if int(stage.get("stage", 0) or 0) == enhance_level
                    ),
                    None,
                )
                if (
                    enhance_level_progress_percent is None
                    and current_stage is not None
                ):
                    enhance_level_progress_percent = int(
                        current_stage.get("percent", 0) or 0
                    )
                if enhance_overall_percent is None:
                    enhance_overall_percent = sum(
                        int(stage.get("percent", 0) or 0)
                        for stage in enhance_stages
                    ) // len(enhance_stages)
            if enhance_completed_level is None and enhance_level is not None:
                enhance_completed_level = enhance_level

            season_id = _positive_int(metadata.get("season_id"))
            grow_body_id = self.body_grow_ids.get((season_id, slot), 0)
            enhance_schedule = [
                dict(stage)
                for stage in self.enhance_schedules.get(grow_body_id, ())
            ]
            if enhance_stage_count is None and enhance_schedule:
                enhance_stage_count = len(enhance_schedule)
            elif enhance_stage_count is None and enhance_stages:
                enhance_stage_count = len(enhance_stages)

            enhance_completed_score = None
            enhance_level_score = None
            next_enhance_level = None
            next_enhance_score = None
            next_enhance_increment = None
            if enhance_level is not None and enhance_schedule:
                enhance_completed_score = 0
                for stage in enhance_schedule:
                    level = int(stage.get("level", 0) or 0)
                    if level <= (enhance_completed_level or 0):
                        enhance_completed_score = int(
                            stage.get("cumulative_score", 0) or 0
                        )
                    if level == enhance_level:
                        enhance_level_score = int(
                            stage.get("cumulative_score", 0) or 0
                        )
                    if level > enhance_level and next_enhance_level is None:
                        next_enhance_level = level
                        next_enhance_score = int(
                            stage.get("cumulative_score", 0) or 0
                        )
                        next_enhance_increment = int(
                            stage.get("score", 0) or 0
                        )
                if enhance_level == 0:
                    enhance_level_score = 0
            enhance_level_remaining = (
                max(0, enhance_level_score - enhance_score)
                if enhance_level_score is not None and enhance_score is not None
                else None
            )
            next_enhance_remaining = (
                max(0, next_enhance_score - enhance_score)
                if next_enhance_score is not None and enhance_score is not None
                else None
            )

            total_score = _positive_int(exact.get("total_score")) or None
            if total_score is None:
                supplied_total = _positive_int(item.get("total_score")) or None
                if supplied_total is not None and bool(item.get("score_complete")):
                    total_score = supplied_total
            if (
                total_score is None
                and enhance_score is not None
                and base_score
            ):
                total_score = base_score + random_score + enhance_score
            known_score = (
                base_score + random_score
                if base_score
                else random_score
                or _positive_int(item.get("item_score"))
            )
            item_score = total_score if total_score is not None else known_score
            quality = (
                _positive_int(item.get("quality"))
                or _positive_int(exact.get("quality"))
                or _positive_int(metadata.get("quality"))
            )
            affixes = []
            resolved_word_scores: list[int | None] = []
            for index, word_id in enumerate(word_ids):
                word_metadata = dict(self.word_metadata.get(word_id, {}))
                metadata_score = _nonnegative_int_or_none(
                    word_metadata.get("score")
                )
                score = (
                    word_scores[index]
                    if index < len(word_scores)
                    else metadata_score
                )
                resolved_word_scores.append(score)
                properties = word_metadata.get("properties")
                properties = (
                    deepcopy(properties) if isinstance(properties, list) else []
                )
                primary_property = next(
                    (row for row in properties if isinstance(row, Mapping)), {}
                )
                class_type = (
                    word_classes[index] if index < len(word_classes) else 0
                )
                affixes.append(
                    {
                        "word_id": word_id,
                        "class_type": class_type,
                        "category": "active" if class_type == ACTIVE_WORD_CLASS_TYPE else "standard",
                        "effect_type": str(
                            word_metadata.get("effect_type") or ""
                        ),
                        "name": str(primary_property.get("name") or ""),
                        "property_key": str(primary_property.get("key") or ""),
                        "property_value": primary_property.get("value"),
                        "properties": properties,
                        "score": score,
                    }
                )
            auxiliary_id = _positive_int(item.get("auxiliary_id"))
            special_affix = None
            if auxiliary_id:
                special_metadata = self.convergence_metadata.get(auxiliary_id)
                if isinstance(special_metadata, Mapping):
                    special_affix = deepcopy(dict(special_metadata))
            special_score = (
                _nonnegative_int_or_none(special_affix.get("score"))
                if isinstance(special_affix, Mapping)
                else None
            )
            score_breakdown_complete = bool(word_ids or auxiliary_id) and all(
                score is not None for score in resolved_word_scores
            ) and (not auxiliary_id or special_score is not None)
            score_breakdown_total = sum(
                int(score or 0) for score in resolved_word_scores
            ) + int(special_score or 0)
            score_breakdown_matches = (
                score_breakdown_complete
                and score_breakdown_total == random_score
            )
            is_pvp = metadata.get("mode") == "pvp"
            pvp_count += int(is_pvp)
            total_words += len(word_ids)
            active_words += sum(
                word_type == ACTIVE_WORD_CLASS_TYPE
                for word_type in word_classes
            )
            item.update(
                {
                    "metadata": metadata,
                    "equipment_mode": metadata.get("mode", "unknown"),
                    "is_pvp": is_pvp,
                    "slot_name": str(
                        item.get("slot_name")
                        or equipment_slot_name(item.get("slot"))
                    ),
                    "item_score": item_score or None,
                    "base_score": base_score or None,
                    "random_score": random_score or None,
                    "enhance_score": enhance_score,
                    "enhance_level": enhance_level,
                    "enhance_completed_level": enhance_completed_level,
                    "enhance_stage_count": enhance_stage_count,
                    "enhance_stages": enhance_stages,
                    "grow_body_id": grow_body_id or None,
                    "enhance_schedule": enhance_schedule,
                    "enhance_completed_score": enhance_completed_score,
                    "enhance_level_score": enhance_level_score,
                    "enhance_level_remaining": enhance_level_remaining,
                    "enhance_level_progress_percent": (
                        enhance_level_progress_percent
                    ),
                    "enhance_overall_percent": enhance_overall_percent,
                    # Backwards-compatible alias used by earlier snapshots.
                    "enhance_progress_percent": enhance_overall_percent,
                    "next_enhance_level": next_enhance_level,
                    "next_enhance_score": next_enhance_score,
                    "next_enhance_increment": next_enhance_increment,
                    "next_enhance_remaining": next_enhance_remaining,
                    "known_score": known_score or None,
                    "total_score": total_score,
                    "score_complete": total_score is not None,
                    "score_source": str(
                        exact.get("score_source")
                        or (
                            "wire_base_random_subtotal"
                            if known_score
                            else "unavailable"
                        )
                    ),
                    "quality": quality or None,
                    "quality_name": EQUIPMENT_QUALITY_NAMES.get(quality, ""),
                    "word_scores": resolved_word_scores,
                    "affixes": affixes,
                    "special_affix": special_affix,
                    "score_breakdown_total": (
                        score_breakdown_total if score_breakdown_complete else None
                    ),
                    "score_breakdown_complete": score_breakdown_complete,
                    "score_breakdown_matches": score_breakdown_matches,
                    "word_class_types": word_classes,
                    "active_word_count": sum(
                        value == ACTIVE_WORD_CLASS_TYPE
                        for value in word_classes
                    ),
                    "total_word_count": len(word_ids),
                }
            )
            enriched_equipment.append(item)
        complete_scores = bool(enriched_equipment) and all(
            bool(item.get("score_complete")) for item in enriched_equipment
        )
        known_equipment_score = sum(
            _positive_int(item.get("known_score"))
            for item in enriched_equipment
        )
        exact_equipment_score = sum(
            _positive_int(item.get("total_score"))
            for item in enriched_equipment
        ) if complete_scores else 0
        result.update(
            {
                "equipment": enriched_equipment,
                "equipment_score": exact_equipment_score or None,
                "equipment_known_score": known_equipment_score or None,
                "equipment_score_complete": complete_scores,
                "equipment_score_source": (
                    "local_equipment_model_sum"
                    if complete_scores
                    else "wire_base_random_subtotal"
                    if known_equipment_score
                    else "unavailable"
                ),
                "pvp_equipment_count": pvp_count,
                "active_word_count": active_words,
                "total_word_count": total_words,
                "metadata_source_path": self.source_path,
                "metadata_source_sha256": self.source_sha256,
            }
        )
        return result


@dataclass(frozen=True)
class EquipmentQuerySession:
    game_pid: int
    capture_session_id: int
    local_user_token: str
    party_session_id: int
    member_count: int
    members: tuple[dict[str, object], ...]
    tokens: tuple[str, ...]
    priority: bool = False

    @classmethod
    def from_value(cls, value: object) -> "EquipmentQuerySession":
        if not isinstance(value, Mapping):
            raise ValueError("equipment query session is not an object")
        game_pid = _positive_int(value.get("game_pid"))
        capture_session_id = _positive_int(value.get("capture_session_id"))
        local_token = str(value.get("local_user_token", "") or "").strip()
        party_session_id = _positive_int(value.get("party_session_id"))
        member_count = _positive_int(value.get("member_count"))
        raw_members = value.get("members", [])
        members: list[dict[str, object]] = []
        tokens: list[str] = []
        for raw_member in raw_members if isinstance(raw_members, Sequence) else []:
            if not isinstance(raw_member, Mapping):
                continue
            token = str(raw_member.get("user_token", "") or "").strip()
            name = str(raw_member.get("name", "") or "").strip()
            if (
                not ROLE_TOKEN_RE.fullmatch(token)
                or bool(raw_member.get("is_ai", False))
                or token in tokens
            ):
                continue
            member = {
                "user_token": token,
                "actor_id": int(raw_member.get("actor_id", 0) or 0),
                "name": name[:64],
                "extraordinary_rating": _positive_int(
                    raw_member.get("extraordinary_rating")
                ),
            }
            members.append(member)
            tokens.append(token)
        if (
            not game_pid
            or not capture_session_id
            or not ROLE_TOKEN_RE.fullmatch(local_token)
            or not party_session_id
            or not tokens
        ):
            raise ValueError("equipment query session is incomplete")
        if len(tokens) > MAX_TEAM_QUERY_TOKENS:
            raise ValueError("equipment query roster exceeds the team limit")
        return cls(
            game_pid=game_pid,
            capture_session_id=capture_session_id,
            local_user_token=local_token,
            party_session_id=party_session_id,
            member_count=member_count,
            members=tuple(members),
            tokens=tuple(sorted(tokens)),
            priority=bool(value.get("priority")),
        )

    @property
    def key(self) -> tuple[object, ...]:
        """Logical team query identity, independent of transport reconnects."""

        ratings = tuple(
            (
                token,
                _positive_int(self.member(token).get("extraordinary_rating")),
            )
            for token in self.tokens
        )
        return (
            self.game_pid,
            self.local_user_token,
            self.party_session_id,
            self.priority,
            ratings,
        )

    def member(self, token: str) -> dict[str, object]:
        return next(
            (dict(item) for item in self.members if item["user_token"] == token),
            {},
        )

    def archive_fields(self) -> dict[str, object]:
        return {
            "game_pid": self.game_pid,
            "capture_session_id": self.capture_session_id,
            "local_user_token": self.local_user_token,
            "party_session_id": self.party_session_id,
            "member_count": self.member_count,
            "members": list(self.members),
            "tokens": list(self.tokens),
        }


def profile_matches_session(
    payload: object,
    *,
    game_pid: int,
    capture_session_id: int,
    local_user_token: str,
    party_session_id: int,
    current_tokens: Sequence[str],
) -> bool:
    """Accept only the current process, role and team lifetime.

    ``capture_session_id`` deliberately is not a validity boundary.  The game
    opens a new transport while entering/leaving a scene, but that does not
    change the character, party, or equipment result.  The value remains in
    archived evidence for diagnostics only.
    """

    if not isinstance(payload, Mapping):
        return False
    token = str(payload.get("user_token", "") or "").strip()
    return bool(
        token
        and token in set(current_tokens)
        and int(payload.get("game_pid", 0) or 0) == int(game_pid or 0)
        and str(payload.get("local_user_token", "") or "").strip()
        == str(local_user_token or "").strip()
        and int(payload.get("party_session_id", 0) or 0)
        == int(party_session_id or 0)
    )


def _argument_descriptor_type_name(snapshot_bytes: bytes) -> str:
    if len(snapshot_bytes) < 0x30:
        return ""
    try:
        return snapshot_bytes[0x10:0x30].split(b"\0", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return ""


def _read_std_string(process: int, address: int) -> str:
    raw = read_region(process, address, 0x20)
    if raw is None or len(raw) != 0x20:
        return ""
    length, capacity = struct.unpack_from("<QQ", raw, 0x10)
    if length > 127 or capacity < length:
        return ""
    data = raw[:length] if capacity < 16 else (
        read_region(process, struct.unpack_from("<Q", raw)[0], length) or b""
    )
    try:
        return data.decode("ascii")
    except UnicodeDecodeError:
        return ""


def _descriptor_schema(process: int, descriptor_address: int) -> tuple[tuple[str, int], ...]:
    descriptor = read_region(process, descriptor_address, 0x70)
    if descriptor is None or len(descriptor) != 0x70:
        return ()
    if struct.unpack_from("<Q", descriptor, 0x20)[0] != REQUEST_METHOD_ID:
        return ()
    if _read_std_string(process, descriptor_address + 0x28) != REQUEST_METHOD:
        return ()
    vector_start, vector_end = struct.unpack_from("<QQ", descriptor, 0x48)
    if vector_end - vector_start != 16:
        return ()
    raw_vector = read_region(process, vector_start, 16)
    if raw_vector is None or len(raw_vector) != 16:
        return ()
    observed: list[tuple[str, int]] = []
    for pointer in struct.unpack("<2Q", raw_vector):
        raw = read_region(process, pointer, ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE)
        if raw is None or len(raw) != ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE:
            return ()
        observed.append((_argument_descriptor_type_name(raw), int(raw[0x50])))
    return tuple(observed)


def _iter_readable_private_regions(process: int, start: int, end: int):
    cursor = max(0x10000, int(start))
    end = min(int(end), MAX_USER_ADDRESS)
    while cursor < end:
        mbi = MEMORY_BASIC_INFORMATION()
        queried = kernel32.VirtualQueryEx(
            process,
            ctypes.c_void_p(cursor),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi),
        )
        if not queried:
            return
        base = int(mbi.BaseAddress or 0)
        region_end = base + int(mbi.RegionSize)
        if region_end <= cursor:
            return
        readable = bool(
            mbi.State == MEM_COMMIT
            and mbi.Type == MEM_PRIVATE
            and not (mbi.Protect & PAGE_GUARD)
            and (mbi.Protect & 0xFF) != PAGE_NOACCESS
        )
        lo = max(cursor, base)
        hi = min(end, region_end)
        if readable and hi > lo and hi - lo <= 256 * 1024 * 1024:
            yield lo, hi - lo
        cursor = max(cursor + 0x1000, region_end)


def _scan_descriptor_range(process: int, start: int, end: int) -> list[int]:
    results: list[int] = []
    overlap = len(DESCRIPTOR_PATTERN) - 1
    for region, size in _iter_readable_private_regions(process, start, end):
        position = region
        region_end = region + size
        tail = b""
        while position < region_end:
            amount = min(4 * 1024 * 1024, region_end - position)
            block = read_region(process, position, amount)
            if not block:
                tail = b""
                position += amount
                continue
            data = tail + block
            origin = position - len(tail)
            found = 0
            while True:
                found = data.find(DESCRIPTOR_PATTERN, found)
                if found < 0:
                    break
                candidate = origin + found - 0x10
                if (
                    candidate > 0
                    and candidate not in results
                    and _descriptor_schema(process, candidate)
                    == EXPECTED_ARGUMENT_DESCRIPTORS
                ):
                    results.append(candidate)
                found += 1
            tail = data[-overlap:]
            position += amount
    return results


def locate_registered_method(
    process: int, descriptor_hint: int = 0
) -> tuple[int, tuple[int, ...]]:
    # Reuse only an in-memory location whose protocol name, ID and full
    # argument schema are readable and still correct in this process.
    # This caches no player equipment or Lua/session values.
    if (
        descriptor_hint > 0
        and _descriptor_schema(process, descriptor_hint)
        == EXPECTED_ARGUMENT_DESCRIPTORS
    ):
        return descriptor_hint, (descriptor_hint,)
    candidates = _scan_descriptor_range(
        process,
        PREFERRED_DESCRIPTOR_START,
        PREFERRED_DESCRIPTOR_END,
    )
    if not candidates:
        candidates = _scan_descriptor_range(process, 0x10000, MAX_USER_ADDRESS)
    if not candidates:
        raise RuntimeError("ReqOtherRoleShapeData descriptor was not found")
    candidates.sort()
    return candidates[0], tuple(candidates)


def _write_memory(process: int, address: int, data: bytes) -> None:
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


def _write_code(process: int, address: int, data: bytes) -> None:
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
        _write_memory(process, address, data)
        if not kernel32.FlushInstructionCache(
            process, ctypes.c_void_p(address), len(data)
        ):
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


def _process_alive(process: int) -> bool:
    code = wintypes.DWORD()
    return bool(
        process
        and kernel32.GetExitCodeProcess(process, ctypes.byref(code))
        and code.value == STILL_ACTIVE
    )


def _thread_ids(pid: int) -> list[int]:
    result: list[int] = []
    threads = snapshot(TH32CS_SNAPTHREAD)
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        available = _thread32_first(threads, ctypes.byref(entry))
        while available:
            if int(entry.th32OwnerProcessID) == int(pid):
                result.append(int(entry.th32ThreadID))
            available = _thread32_next(threads, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(threads)
    return result


def _suspend_threads(pid: int) -> list[int]:
    suspended: list[int] = []
    access = THREAD_SUSPEND_RESUME | THREAD_QUERY_INFORMATION
    try:
        for thread_id in _thread_ids(pid):
            thread = kernel32.OpenThread(access, False, thread_id)
            if not thread:
                # A thread can terminate between Toolhelp enumeration and
                # OpenThread. Windows reports that harmless race as access
                # denied, invalid handle, or invalid parameter depending on
                # the exact exit point.
                if ctypes.get_last_error() in TRANSIENT_THREAD_ERRORS:
                    continue
                raise winerror(f"OpenThread({thread_id})")
            previous = kernel32.SuspendThread(thread)
            if previous == 0xFFFFFFFF:
                error = ctypes.get_last_error()
                kernel32.CloseHandle(thread)
                if error in TRANSIENT_THREAD_ERRORS:
                    continue
                raise winerror(f"SuspendThread({thread_id})")
            suspended.append(int(thread))
        if not suspended:
            raise RuntimeError("no accessible game threads could be suspended")
        return suspended
    except Exception:
        _resume_threads(suspended)
        raise


def _resume_threads(handles: list[int]) -> None:
    for thread in reversed(handles):
        kernel32.ResumeThread(thread)
        kernel32.CloseHandle(thread)
    handles.clear()


def _absolute_patch(stub: int, length: int) -> bytes:
    jump = b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", stub)
    if len(jump) > length:
        raise ValueError("call_server prologue is too short")
    return jump + b"\x90" * (length - len(jump))


def _owned_idle_request_hook(
    process: int,
    target: int,
    prologue: bytes,
    current: bytes | None = None,
) -> tuple[int, int] | None:
    """Return ``(code, state)`` only for our exact recoverable abandoned hook."""

    length = len(prologue)
    if length < 14:
        return None
    if current is None:
        current = read_region(process, target, length)
    if current is None or len(current) != length:
        return None
    if current[:6] != b"\xff\x25\x00\x00\x00\x00":
        return None
    if current[14:] != b"\x90" * (length - 14):
        return None
    code = struct.unpack_from("<Q", current, 6)[0]
    if code <= 0 or code > MAX_USER_ADDRESS:
        return None
    if current != _absolute_patch(code, length):
        return None

    prefix_size = len(REQUEST_STUB_STATE_PREFIX)
    stub_header = read_region(process, code, prefix_size + 8)
    if (
        stub_header is None
        or len(stub_header) != prefix_size + 8
        or stub_header[:prefix_size] != REQUEST_STUB_STATE_PREFIX
    ):
        return None
    state = struct.unpack_from("<Q", stub_header, prefix_size)[0]
    if state <= 0 or state > MAX_USER_ADDRESS:
        return None
    state_header = read_region(process, state, STATE_ARM + 8)
    if state_header is None or len(state_header) != STATE_ARM + 8:
        return None
    if state_header[: len(STATE_MAGIC)] != STATE_MAGIC:
        return None
    arm_state = struct.unpack_from("<Q", state_header, STATE_ARM)[0]
    if arm_state not in (ARM_IDLE, ARM_READY, ARM_DONE):
        return None

    trampoline = code + TRAMPOLINE_OFFSET
    expected_trampoline = (
        bytes(prologue)
        + b"\xff\x25\x00\x00\x00\x00"
        + struct.pack("<Q", target + length)
    )
    if read_region(process, trampoline, len(expected_trampoline)) != expected_trampoline:
        return None
    return code, state


def _restore_owned_idle_request_hook(
    process: int,
    pid: int,
    target: int,
    prologue: bytes,
) -> bool:
    """Restore a fully validated, inactive hook left by an interrupted request."""

    owned = _owned_idle_request_hook(process, target, prologue)
    if owned is None:
        return False
    code, state = owned
    suspended = _suspend_threads(pid)
    try:
        current = read_region(process, target, len(prologue))
        if current != _absolute_patch(code, len(prologue)):
            return False
        if _owned_idle_request_hook(
            process, target, prologue, current
        ) != (code, state):
            return False
        _write_code(process, target, prologue)
        if read_region(process, target, len(prologue)) != prologue:
            raise RuntimeError("abandoned equipment request hook restoration failed")
    finally:
        _resume_threads(suspended)
    return True


def _near_jump(code: bytearray, opcode: bytes) -> int:
    code += opcode
    at = len(code)
    code += b"\0\0\0\0"
    return at


def _patch_jump(code: bytearray, at: int, target: int) -> None:
    struct.pack_into("<i", code, at, target - (at + 4))


def _build_request_stub(
    *,
    state: int,
    trampoline: int,
    target: int,
    prologue: bytes,
    registered_storage: int,
    method_object: int,
    token_cells: Sequence[tuple[int, int]],
    correlation: int,
    lua_createtable: int,
    lua_pushlstring: int,
    lua_pushnumber: int,
    lua_rawseti: int,
    lua_settop: int,
) -> bytes:
    code = bytearray()
    code += b"\x9c\x50\x53\x51\x52\x56\x57"
    code += b"\x41\x50\x41\x51\x41\x52\x41\x53"
    code += b"\x41\x54\x41\x55\x41\x56\x41\x57"
    code += b"\x49\xbf" + struct.pack("<Q", state)

    code += b"\x49\x83\x7f\x08\x01"
    skip_jumps = [_near_jump(code, b"\x0f\x85")]

    # Borrow one natural ReqNTP call so execution stays on the game's Lua/RPC
    # thread and uses its current session serializer.
    code += b"\x4c\x8b\x44\x24" + bytes([SAVED_R8])
    code += b"\x4d\x85\xc0"
    skip_jumps.append(_near_jump(code, b"\x0f\x84"))
    code += b"\x49\x83\x78\x10\x06"
    skip_jumps.append(_near_jump(code, b"\x0f\x85"))
    code += b"\x49\x83\x78\x18\x10"
    inline = _near_jump(code, b"\x0f\x82")
    code += b"\x49\x8b\x00"
    method_ready = _near_jump(code, b"\xe9")
    _patch_jump(code, inline, len(code))
    code += b"\x4c\x89\xc0"
    _patch_jump(code, method_ready, len(code))
    code += b"\x81\x38" + struct.pack("<I", 0x4E716552)
    skip_jumps.append(_near_jump(code, b"\x0f\x85"))
    code += b"\x66\x81\x78\x04" + struct.pack("<H", 0x5054)
    skip_jumps.append(_near_jump(code, b"\x0f\x85"))

    code += b"\x4c\x8b\x74\x24" + bytes([SAVED_R9])
    code += b"\x4d\x85\xf6"
    skip_jumps.append(_near_jump(code, b"\x0f\x84"))
    code += b"\x4d\x8b\x2e"
    code += b"\x4d\x85\xed"
    skip_jumps.append(_near_jump(code, b"\x0f\x84"))

    code += b"\xb8\x01\x00\x00\x00\xba\x02\x00\x00\x00"
    code += b"\xf0\x49\x0f\xb1\x57\x08"
    skip_jumps.append(_near_jump(code, b"\x0f\x85"))
    code += b"\x4d\x89\x6f\x38\xfc"

    code += b"\x48\x8b\x74\x24" + bytes([SAVED_RDX])
    code += b"\x48\x85\xf6"
    claimed_failures = [_near_jump(code, b"\x0f\x84")]
    code += b"\x48\xbf" + struct.pack("<Q", state + STATE_CONTEXT)
    code += b"\xb9\x0f\x00\x00\x00\xf3\x48\xa5"
    code += b"\x4c\x89\xf6"
    code += b"\x48\xbf" + struct.pack("<Q", state + STATE_VARIADIC)
    code += b"\xb9\x08\x00\x00\x00\xf3\x48\xa5"

    code += b"\x49\x8b\x45\x20\x49\x89\x47\x30"
    code += b"\x49\x8b\x5d\x28\x49\x89\x5f\x28"
    code += b"\x48\x8d\x43\x10\x49\x3b\x45\x30"
    claimed_failures.append(_near_jump(code, b"\x0f\x87"))

    code += b"\x48\x83\xec\x40"
    for source, destination in zip(
        (SAVED_ENTRY_STACK_ARG0 + 0x40 + offset for offset in (0, 8, 16, 24)),
        (0x20, 0x28, 0x30, 0x38),
        strict=True,
    ):
        code += b"\x48\x8b\x84\x24" + struct.pack("<I", source)
        code += b"\x48\x89\x44\x24" + bytes([destination])

    code += b"\x4c\x89\xe9\xba" + struct.pack("<I", len(token_cells))
    code += b"\x45\x31\xc0"
    code += b"\x48\xb8" + struct.pack("<Q", lua_createtable) + b"\xff\xd0"
    for index, (token_address, token_length) in enumerate(token_cells, start=1):
        code += b"\x4c\x89\xe9\x48\xba" + struct.pack("<Q", token_address)
        code += b"\x41\xb8" + struct.pack("<I", token_length)
        code += b"\x48\xb8" + struct.pack("<Q", lua_pushlstring) + b"\xff\xd0"
        code += b"\x4c\x89\xe9\xba\xfe\xff\xff\xff\x41\xb8"
        code += struct.pack("<I", index)
        code += b"\x48\xb8" + struct.pack("<Q", lua_rawseti) + b"\xff\xd0"

    correlation_bits = struct.unpack("<Q", struct.pack("<d", float(correlation)))[0]
    code += b"\x4c\x89\xe9\x48\xb8" + struct.pack("<Q", correlation_bits)
    code += b"\x66\x48\x0f\x6e\xc8"
    code += b"\x48\xb8" + struct.pack("<Q", lua_pushnumber) + b"\xff\xd0"

    code += b"\x49\x8b\x47\x28\x49\x2b\x47\x30"
    code += b"\x48\xc1\xe8\x03\xff\xc0\x89\xc2\xff\xc2"
    code += b"\x48\xc1\xe2\x20\x48\x09\xd0"
    code += b"\x48\xbb" + struct.pack("<Q", state + STATE_VARIADIC)
    code += b"\x4c\x89\x2b"
    code += b"\x48\xba" + struct.pack("<Q", registered_storage)
    code += b"\x48\x89\x53\x08\x48\x89\x43\x10"

    code += b"\x48\x8b\x8c\x24" + struct.pack("<I", SAVED_RCX + 0x40)
    code += b"\x48\xba" + struct.pack("<Q", state + STATE_CONTEXT)
    code += b"\x49\xb8" + struct.pack("<Q", method_object)
    code += b"\x49\xb9" + struct.pack("<Q", state + STATE_VARIADIC)
    code += b"\x48\xb8" + struct.pack("<Q", trampoline) + b"\xff\xd0"
    code += b"\x0f\xb6\xc0\x49\x89\x47\x18\x49\xff\x47\x10"

    code += b"\x49\x8b\x57\x28\x49\x2b\x57\x30\x48\xc1\xea\x03"
    code += b"\x4c\x89\xe9"
    code += b"\x48\xb8" + struct.pack("<Q", lua_settop) + b"\xff\xd0"
    code += b"\x49\xc7\x47\x08\x03\x00\x00\x00"
    code += b"\x48\x83\xc4\x40"
    successful = _near_jump(code, b"\xe9")

    claimed_failure_at = len(code)
    for jump in claimed_failures:
        _patch_jump(code, jump, claimed_failure_at)
    code += b"\x49\xc7\x47\x18\xff\xff\xff\xff"
    code += b"\x49\xff\x47\x10\x49\xc7\x47\x08\x03\x00\x00\x00"

    done = len(code)
    _patch_jump(code, successful, done)
    for jump in skip_jumps:
        _patch_jump(code, jump, done)
    code += b"\x41\x5f\x41\x5e\x41\x5d\x41\x5c"
    code += b"\x41\x5b\x41\x5a\x41\x59\x41\x58"
    code += b"\x5f\x5e\x5a\x59\x5b\x58\x9d"
    code += bytes(prologue)
    code += b"\xff\x25\x00\x00\x00\x00" + struct.pack(
        "<Q", target + len(prologue)
    )
    if len(code) >= TRAMPOLINE_OFFSET:
        raise AssertionError(f"shape request stub is too large: {len(code)}")
    return bytes(code)


class OneShotEquipmentRequest:
    def __init__(
        self,
        *,
        pid: int,
        tokens: Sequence[str],
        correlation: int,
        runtime_profile: Mapping[str, object],
        descriptor_hint: int = 0,
    ) -> None:
        hook = runtime_profile_hook(runtime_profile, "team_stats")
        self.pid = int(pid)
        self.tokens = tuple(str(value).encode("ascii") for value in tokens)
        self.correlation = int(correlation)
        self.module_name = str(runtime_profile.get("game_module") or "C7-Win64-Shipping.exe")
        self.rva = int(hook["rva"])
        self.prologue = bytes(hook["prologue"])
        self.signature = bytes(hook["signature"])
        self.process = 0
        self.base = 0
        self.module_path = ""
        self.target = 0
        self.state = 0
        self.code = 0
        self.descriptor = 0
        self.descriptor_candidates: tuple[int, ...] = ()
        self.descriptor_hint = int(descriptor_hint or 0)
        self.installed = False
        self.recovered_abandoned_hook = False

    def install(self) -> None:
        self.base, module_size, self.module_path = find_module(
            self.pid, self.module_name
        )
        if self.rva + len(self.signature) > module_size:
            raise RuntimeError("call_server target is outside the current module")
        access = (
            PROCESS_QUERY_INFORMATION
            | PROCESS_VM_OPERATION
            | PROCESS_VM_READ
            | PROCESS_VM_WRITE
        )
        self.process = int(kernel32.OpenProcess(access, False, self.pid) or 0)
        if not self.process:
            raise winerror("OpenProcess(equipment query)")
        self.target = self.base + self.rva
        entry = read_region(self.process, self.target, len(self.signature))
        if entry != self.signature:
            self.recovered_abandoned_hook = _restore_owned_idle_request_hook(
                self.process,
                self.pid,
                self.target,
                self.prologue,
            )
            entry = read_region(self.process, self.target, len(self.signature))
        if entry != self.signature:
            raise RuntimeError("call_server entry is not clean; equipment request not sent")
        self.descriptor, self.descriptor_candidates = locate_registered_method(
            self.process, self.descriptor_hint
        )
        registered_storage = self.descriptor + 0x18
        method_object = self.descriptor + 0x28
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
            raise winerror("VirtualAllocEx(equipment query)")
        state = bytearray(STATE_SIZE)
        state[:8] = STATE_MAGIC
        token_cells: list[tuple[int, int]] = []
        cursor = STATE_TOKEN_DATA
        for token in self.tokens:
            if cursor + len(token) + 1 > STATE_SIZE:
                raise ValueError("equipment query token buffer is full")
            state[cursor : cursor + len(token)] = token
            token_cells.append((self.state + cursor, len(token)))
            cursor += len(token) + 1
        _write_memory(self.process, self.state, bytes(state))
        trampoline = self.code + TRAMPOLINE_OFFSET
        stub = _build_request_stub(
            state=self.state,
            trampoline=trampoline,
            target=self.target,
            prologue=self.prologue,
            registered_storage=registered_storage,
            method_object=method_object,
            token_cells=token_cells,
            correlation=self.correlation,
            lua_createtable=self.base + LUA_CREATETABLE_RVA,
            lua_pushlstring=self.base + LUA_PUSHLSTRING_RVA,
            lua_pushnumber=self.base + LUA_PUSHNUMBER_RVA,
            lua_rawseti=self.base + LUA_RAWSETI_RVA,
            lua_settop=self.base + LUA_SETTOP_RVA,
        )
        _write_memory(self.process, self.code, stub)
        trampoline_bytes = (
            self.prologue
            + b"\xff\x25\x00\x00\x00\x00"
            + struct.pack("<Q", self.target + len(self.prologue))
        )
        _write_memory(self.process, trampoline, trampoline_bytes)
        kernel32.FlushInstructionCache(
            self.process, ctypes.c_void_p(self.code), CODE_SIZE
        )
        patch = _absolute_patch(self.code, len(self.prologue))
        suspended = _suspend_threads(self.pid)
        try:
            _write_code(self.process, self.target, patch)
            self.installed = True
        finally:
            _resume_threads(suspended)
        if read_region(self.process, self.target, len(patch)) != patch:
            raise RuntimeError("equipment request hook verification failed")

    def arm(self) -> None:
        if not self.installed or not _process_alive(self.process):
            raise RuntimeError("equipment request hook is not installed")
        _write_memory(
            self.process,
            self.state + STATE_ARM,
            struct.pack("<Q", ARM_READY),
        )

    def status(self) -> tuple[int, int, int]:
        raw = read_region(self.process, self.state + STATE_ARM, 0x18)
        if raw is None or len(raw) != 0x18:
            raise RuntimeError("equipment request state is unreadable")
        return struct.unpack("<3Q", raw)

    def close(self) -> None:
        if self.process and self.state:
            try:
                _write_memory(
                    self.process,
                    self.state + STATE_ARM,
                    struct.pack("<Q", ARM_IDLE),
                )
            except OSError:
                pass
        if self.installed and self.process and _process_alive(self.process):
            patch = _absolute_patch(self.code, len(self.prologue))
            suspended = _suspend_threads(self.pid)
            try:
                if read_region(self.process, self.target, len(patch)) == patch:
                    _write_code(self.process, self.target, self.prologue)
            finally:
                _resume_threads(suspended)
            self.installed = False
        if self.process and _process_alive(self.process):
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
        self.process = self.state = self.code = 0


class EquipmentProfileCoordinator:
    """Run bounded batch queries and publish only correlated live profiles."""

    def __init__(
        self,
        *,
        data_dir: Path,
        emit: Callable[[str, object], None],
        stop_event: threading.Event,
        runtime_profile_provider: Callable[[], Mapping[str, object] | None],
        request_hook_pause: Callable[[int], bool] | None = None,
        request_hook_resume: Callable[[int], None] | None = None,
    ) -> None:
        self.archive = EquipmentQueryArchive(data_dir)
        self.item_name_cache_path = Path(data_dir) / "equipment_item_names.json"
        self.emit = emit
        self.stop_event = stop_event
        self.closing = threading.Event()
        self.runtime_profile_provider = runtime_profile_provider
        self.request_hook_pause = request_hook_pause
        self.request_hook_resume = request_hook_resume
        self.commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self.query_commands: queue.Queue[EquipmentQuerySession | None] = queue.Queue()
        self.priority_queries: queue.Queue[EquipmentQuerySession] = queue.Queue()
        self.thread = threading.Thread(
            target=self._run,
            name="C7EquipmentProfiles",
            daemon=False,
        )
        self.query_thread = threading.Thread(
            target=self._run_queries,
            name="C7EquipmentQueries",
            daemon=False,
        )
        self.lock = threading.Lock()
        self.started = False
        self.latest_session: EquipmentQuerySession | None = None
        # None means queued/preparing; the response timeout starts only once
        # the game has sent the request, never while another batch is ahead.
        self.requested_keys: dict[tuple[object, ...], float | None] = {}
        self.priority_keys: set[tuple[object, ...]] = set()
        self.requests: dict[int, dict[str, object]] = {}
        self.pending_profiles: list[tuple[dict[str, object], EquipmentQuerySession, dict[str, object]]] = []
        self.latest_profiles: dict[
            tuple[int, int, str],
            tuple[dict[str, object], EquipmentQuerySession, dict[str, object]],
        ] = {}
        self.catalog: EquipmentMetadataCatalog | None = None
        self.catalog_loading = False
        self.catalog_module_path = ""
        self.item_names_loading = False
        self.local_equipment_scores: dict[
            tuple[int, str, int], dict[int, dict[str, object]]
        ] = {}
        self.local_equipment_loading: set[tuple[int, str, int]] = set()
        self.descriptor_hint: tuple[int, int] = (0, 0)
        self.descriptor_scan_lock = threading.Lock()
        self.request_hook_lock = threading.Lock()
        self.descriptor_preparing_pids: set[int] = set()

    def start(self) -> None:
        with self.lock:
            if self.started:
                return
            self.started = True
            self.thread.start()
            self.query_thread.start()

    def close(self, timeout: float = 8.0) -> None:
        if not self.started:
            return
        self.closing.set()
        self.commands.put(("stop", None))
        self.query_commands.put(None)
        deadline = time.monotonic() + max(0.1, float(timeout))
        for thread in (self.query_thread, self.thread):
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def schedule(self, value: object) -> bool:
        try:
            session = EquipmentQuerySession.from_value(value)
        except (TypeError, ValueError, OverflowError):
            return False
        with self.lock:
            if (
                self.latest_session is not None
                and self._query_identity(self.latest_session)
                != self._query_identity(session)
            ):
                self.latest_profiles.clear()
                self.requested_keys.clear()
                self.priority_keys.clear()
            self.latest_session = session
            now = time.monotonic()
            self.requested_keys = {
                key: requested_at
                for key, requested_at in self.requested_keys.items()
                if requested_at is None
                or now - requested_at < EQUIPMENT_QUERY_DEDUP_SECONDS
            }
            self.priority_keys.intersection_update(self.requested_keys)
            members = tuple(
                member for member in session.members
                if self._member_query_key(session, member) not in self.requested_keys
                or (
                    session.priority
                    and self.requested_keys[self._member_query_key(session, member)] is None
                    and self._member_query_key(session, member) not in self.priority_keys
                )
            )
            if not members:
                return False
            session = replace(
                session, members=members,
                tokens=tuple(sorted(str(member["user_token"]) for member in members)),
            )
            for member in members:
                key = self._member_query_key(session, member)
                self.requested_keys[key] = None
                if session.priority:
                    self.priority_keys.add(key)
        (self.priority_queries if session.priority else self.query_commands).put(session)
        return True

    def prepare(self, game_pid: object) -> bool:
        """Warm the read-only method lookup before a teammate joins."""
        pid = _positive_int(game_pid)
        if not pid or self.stop_event.is_set() or self.closing.is_set():
            return False
        with self.lock:
            if self.descriptor_hint[0] == pid or pid in self.descriptor_preparing_pids:
                return False
            self.descriptor_preparing_pids.add(pid)

        def run() -> None:
            started = time.monotonic()
            process = 0
            hook_paused = False
            self.request_hook_lock.acquire()
            try:
                if self.request_hook_pause is not None:
                    hook_paused = bool(self.request_hook_pause(pid))
                    if not hook_paused:
                        raise RuntimeError("call_server entry is busy")
                profile = self._runtime_profile()
                hook = runtime_profile_hook(profile, "team_stats")
                base, module_size, module_path = find_module(
                    pid, str(profile.get("game_module") or "C7-Win64-Shipping.exe")
                )
                rva = int(hook["rva"])
                signature = bytes(hook["signature"])
                if rva + len(signature) > module_size:
                    raise RuntimeError("call_server target is outside the current module")
                process = int(
                    kernel32.OpenProcess(
                        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
                    )
                    or 0
                )
                if not process:
                    raise winerror("OpenProcess(equipment prepare)")
                if read_region(process, base + rva, len(signature)) != signature:
                    raise RuntimeError("call_server entry is not clean")
                with self.descriptor_scan_lock:
                    descriptor, candidates = locate_registered_method(process)
                with self.lock:
                    self.descriptor_hint = (pid, descriptor)
                self.archive.append(
                    "descriptor_prepared",
                    game_pid=pid,
                    module_path=module_path,
                    descriptor_address=descriptor,
                    descriptor_candidates=list(candidates),
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                )
            except Exception as error:
                self.archive.append(
                    "descriptor_prepare_failed",
                    game_pid=pid,
                    error_type=type(error).__name__,
                    error=str(error),
                )
            finally:
                if process:
                    kernel32.CloseHandle(process)
                try:
                    if hook_paused and self.request_hook_resume is not None:
                        self.request_hook_resume(pid)
                finally:
                    self.request_hook_lock.release()
                with self.lock:
                    self.descriptor_preparing_pids.discard(pid)

        threading.Thread(
            target=run,
            name="C7EquipmentPrepare",
            daemon=True,
        ).start()
        return True

    def invalidate(self, reason: str) -> None:
        with self.lock:
            previous = self.latest_session
            self.latest_session = None
            self.descriptor_hint = (0, 0)
            self.local_equipment_scores.clear()
            self.latest_profiles.clear()
            self.requested_keys.clear()
            self.priority_keys.clear()
        self.archive.append(
            "current_context_invalidated",
            reason=str(reason or "unknown")[:96],
            previous_session=(previous.archive_fields() if previous else None),
        )

    def handle_response(self, record: object) -> None:
        if not isinstance(record, Mapping):
            return
        # Capture/combat dispatch must not serialize or fsync equipment here.
        self.commands.put(("response", dict(record)))

    def _archive_response(self, record: Mapping[str, object]) -> None:
        # Persist the complete response before any parser or current-session
        # decision can reject it.
        self.archive.append(
            "response_received",
            method=RESPONSE_METHOD,
            protocol_id=RESPONSE_METHOD_ID,
            full_record=dict(record),
            raw_payload_base64=record.get("npcap_rpc_payload_base64", ""),
            raw_payload_sha256=record.get("npcap_rpc_payload_sha256", ""),
            raw_payload_length=record.get("npcap_rpc_payload_length", 0),
        )

    def _runtime_profile(self) -> Mapping[str, object]:
        value = self.runtime_profile_provider()
        if not isinstance(value, Mapping):
            raise RuntimeError("runtime profile is unavailable")
        return value

    def _begin_catalog_load(self, module_path: str, pid: int) -> None:
        with self.lock:
            if self.catalog is not None or self.catalog_loading:
                return
            self.catalog_loading = True
            self.catalog_module_path = str(module_path)

        def load() -> None:
            started = time.monotonic()
            try:
                catalog = EquipmentMetadataCatalog.from_game_module(
                    module_path,
                    item_name_cache_path=self.item_name_cache_path,
                )
            except Exception as error:
                self.archive.append(
                    "metadata_load_failed",
                    game_pid=pid,
                    module_path=module_path,
                    error_type=type(error).__name__,
                    error=str(error),
                )
                with self.lock:
                    self.catalog_loading = False
                return
            with self.lock:
                self.catalog = catalog
                self.catalog_loading = False
            self.archive.append(
                "metadata_loaded",
                game_pid=pid,
                source_path=catalog.source_path,
                source_sha256=catalog.source_sha256,
                item_count=len(catalog.item_metadata),
                word_class_count=len(catalog.word_class_types),
                elapsed_ms=round((time.monotonic() - started) * 1000, 3),
            )
            self.commands.put(("catalog_ready", None))
            self._begin_item_name_load(catalog, pid)

        threading.Thread(
            target=load,
            name="C7EquipmentMetadata",
            daemon=True,
        ).start()

    def _begin_item_name_load(
        self,
        catalog: EquipmentMetadataCatalog,
        pid: int,
    ) -> None:
        with self.lock:
            if self.item_names_loading:
                return
            unresolved = {
                item_id
                for item_id, metadata in catalog.item_metadata.items()
                if _positive_int(metadata.get("item_name_id"))
                and item_id not in catalog.item_names
            }
            if not unresolved:
                return
            self.item_names_loading = True

        def load() -> None:
            started = time.monotonic()
            try:
                names = scan_equipment_item_names(
                    pid,
                    catalog.item_metadata,
                    catalog.item_names,
                )
                with self.lock:
                    changed = {
                        item_id: name
                        for item_id, name in names.items()
                        if catalog.item_names.get(item_id) != name
                    }
                    if self.catalog is catalog:
                        catalog.item_names.update(changed)
                write_equipment_item_name_cache(
                    self.item_name_cache_path,
                    catalog.item_metadata,
                    names,
                )
                self.archive.append(
                    "item_names_loaded",
                    game_pid=pid,
                    resolved_count=len(names),
                    newly_resolved_count=len(changed),
                    unresolved_count=max(0, len(catalog.item_metadata) - len(names)),
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                )
                if changed:
                    self.commands.put(("item_names_ready", pid))
            except Exception as error:
                self.archive.append(
                    "item_names_load_failed",
                    game_pid=pid,
                    error_type=type(error).__name__,
                    error=str(error),
                )
            finally:
                with self.lock:
                    self.item_names_loading = False

        threading.Thread(
            target=load,
            name="C7EquipmentItemNames",
            daemon=True,
        ).start()

    @staticmethod
    def _local_equipment_key(
        session: EquipmentQuerySession,
    ) -> tuple[int, str, int]:
        member = session.member(session.local_user_token)
        return (
            session.game_pid,
            session.local_user_token,
            _positive_int(member.get("extraordinary_rating")),
        )

    def _begin_local_equipment_load(self, session: EquipmentQuerySession) -> None:
        """Read the logged-in character's exact item totals in parallel."""

        key = self._local_equipment_key(session)
        with self.lock:
            cached = self.local_equipment_scores.get(key)
            if cached or key in self.local_equipment_loading:
                return
            self.local_equipment_loading.add(key)

        def load() -> None:
            started = time.monotonic()
            scores: dict[int, dict[str, object]] = {}
            error: Exception | None = None
            try:
                from runtime_metadata import LiveTeamProfileReader

                with LiveTeamProfileReader(session.game_pid) as reader:
                    scores = {
                        int(slot): dict(value)
                        for slot, value in reader.local_equipment_scores(
                            session.local_user_token
                        ).items()
                        if int(slot) > 0 and isinstance(value, Mapping)
                    }
            except Exception as caught:
                error = caught
            with self.lock:
                self.local_equipment_loading.discard(key)
                if scores:
                    self.local_equipment_scores[key] = scores
            event = {
                "game_pid": session.game_pid,
                "local_user_token": session.local_user_token,
                "extraordinary_rating": key[2],
                "item_count": len(scores),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }
            if error is None:
                self.archive.append("local_equipment_scores_loaded", **event)
            else:
                self.archive.append(
                    "local_equipment_scores_failed",
                    **event,
                    error_type=type(error).__name__,
                    error=str(error),
                )
            self.commands.put(("local_equipment_ready", key))

        threading.Thread(
            target=load,
            name="C7LocalEquipmentScores",
            daemon=True,
        ).start()

    def _profile_exact_scores(
        self,
        profile: Mapping[str, object],
        session: EquipmentQuerySession,
    ) -> Mapping[int, Mapping[str, object]] | None:
        if str(profile.get("user_token", "") or "") != session.local_user_token:
            return None
        with self.lock:
            return deepcopy(
                self.local_equipment_scores.get(
                    self._local_equipment_key(session), {}
                )
            )

    def _local_scores_pending(
        self,
        profile: Mapping[str, object],
        session: EquipmentQuerySession,
    ) -> bool:
        if str(profile.get("user_token", "") or "") != session.local_user_token:
            return False
        with self.lock:
            return self._local_equipment_key(session) in self.local_equipment_loading

    def _execute_query(self, session: EquipmentQuerySession) -> None:
        self._begin_local_equipment_load(session)
        batch_id = uuid.uuid4().hex
        correlation = int(time.time_ns() // 1_000_000)
        context = {
            "batch_id": batch_id,
            "correlation": correlation,
            **session.archive_fields(),
            "request_method": REQUEST_METHOD,
            "request_protocol_id": REQUEST_METHOD_ID,
            "logical_arguments": [list(session.tokens), correlation],
            "raw_request_payload_available": False,
        }
        with self.lock:
            self.requests[correlation] = {
                "session": session,
                "context": context,
            }
            hint_pid, hint_address = self.descriptor_hint
        self.archive.append("request_started", **context)
        request: OneShotEquipmentRequest | None = None
        sent = False
        hook_paused = False
        self.request_hook_lock.acquire()
        try:
            if self.request_hook_pause is not None:
                hook_paused = bool(self.request_hook_pause(session.game_pid))
                if not hook_paused:
                    raise RuntimeError("call_server entry is busy")
            with self.descriptor_scan_lock:
                with self.lock:
                    hint_pid, hint_address = self.descriptor_hint
                request = OneShotEquipmentRequest(
                    pid=session.game_pid,
                    tokens=session.tokens,
                    correlation=correlation,
                    runtime_profile=self._runtime_profile(),
                    descriptor_hint=(
                        hint_address if hint_pid == session.game_pid else 0
                    ),
                )
                request.install()
                with self.lock:
                    latest = self.latest_session
                    if latest is not None and latest.game_pid == session.game_pid:
                        self.descriptor_hint = (session.game_pid, request.descriptor)
            self._begin_catalog_load(request.module_path, session.game_pid)
            self.archive.append(
                "request_hook_installed",
                **context,
                module_base=request.base,
                module_path=request.module_path,
                call_server_address=request.target,
                descriptor_address=request.descriptor,
                descriptor_candidates=list(request.descriptor_candidates),
                descriptor_schema=list(EXPECTED_ARGUMENT_DESCRIPTORS),
                recovered_abandoned_hook=request.recovered_abandoned_hook,
            )
            request.arm()
            deadline = time.monotonic() + 12.0
            arm_state = count = call_result = 0
            while (
                time.monotonic() < deadline
                and not self.stop_event.is_set()
                and not self.closing.is_set()
            ):
                arm_state, count, call_result = request.status()
                if arm_state == ARM_DONE or count:
                    break
                time.sleep(0.01)
            sent = bool(count)
            if not sent:
                raise TimeoutError("no ReqNTP edge arrived before timeout")
            with self.lock:
                for member in session.members:
                    self.requested_keys[self._member_query_key(session, member)] = time.monotonic()
            self.archive.append(
                "request_call_finished",
                **context,
                arm_state=arm_state,
                call_count=count,
                call_result=call_result,
            )
        except Exception as error:
            self.archive.append(
                "request_failed",
                **context,
                sent=sent,
                error_type=type(error).__name__,
                error=str(error),
            )
            if not sent:
                with self.lock:
                    for member in session.members:
                        key = self._member_query_key(session, member)
                        self.requested_keys.pop(key, None)
                        self.priority_keys.discard(key)
                    self.requests.pop(correlation, None)
        finally:
            if request is not None:
                try:
                    request.close()
                    self.archive.append("request_hook_restored", **context)
                except Exception as error:
                    self.archive.append(
                        "request_hook_restore_failed",
                        **context,
                        error_type=type(error).__name__,
                        error=str(error),
                    )
            try:
                if hook_paused and self.request_hook_resume is not None:
                    self.request_hook_resume(session.game_pid)
            finally:
                self.request_hook_lock.release()

    def _process_response(self, record: Mapping[str, object]) -> None:
        self._archive_response(record)
        try:
            parsed = parse_shape_response(record)
        except Exception as error:
            self.archive.append(
                "response_parse_failed",
                method=RESPONSE_METHOD,
                error_type=type(error).__name__,
                error=str(error),
            )
            return
        correlation = int(parsed.get("correlation", 0) or 0)
        with self.lock:
            request = self.requests.get(correlation)
        session = request.get("session") if isinstance(request, Mapping) else None
        context = request.get("context") if isinstance(request, Mapping) else None
        matched = isinstance(session, EquipmentQuerySession)
        self.archive.append(
            "response_parsed",
            correlation=correlation,
            matched_request=matched,
            request_context=context,
            full_decoded_arguments=parsed.get("decoded_arguments"),
            parsed_profiles=parsed.get("profiles"),
        )
        if not matched:
            return
        assert isinstance(session, EquipmentQuerySession)
        response_context = dict(context or {})
        try:
            response_context["captured_at_ns"] = int(
                record.get("capture_timestamp_ns", 0) or 0
            )
        except (TypeError, ValueError, OverflowError):
            response_context["captured_at_ns"] = 0
        profiles = parsed.get("profiles", [])
        for raw_profile in profiles if isinstance(profiles, list) else []:
            if not isinstance(raw_profile, dict):
                continue
            token = str(raw_profile.get("user_token", "") or "")
            if token not in session.tokens:
                self.archive.append(
                    "profile_rejected",
                    correlation=correlation,
                    reason="TOKEN_NOT_REQUESTED",
                    profile=raw_profile,
                    request_context=context,
                )
                continue
            with self.lock:
                self.latest_profiles[
                    (session.game_pid, session.party_session_id, token)
                ] = (dict(raw_profile), session, dict(response_context))
            with self.lock:
                catalog = self.catalog
            if catalog is None or self._local_scores_pending(raw_profile, session):
                with self.lock:
                    self.pending_profiles.append((raw_profile, session, response_context))
                continue
            self._publish_profile(
                catalog.enrich(
                    raw_profile,
                    exact_scores=self._profile_exact_scores(raw_profile, session),
                ),
                session,
                response_context,
            )

    def _publish_profile(
        self,
        profile: dict[str, object],
        session: EquipmentQuerySession,
        context: dict[str, object],
    ) -> None:
        token = str(profile.get("user_token", "") or "")
        member = session.member(token)
        returned_name = str(profile.get("name", "") or "").strip()
        expected_name = str(member.get("name", "") or "").strip()
        identity_matches = not expected_name or expected_name == returned_name
        archived_profile = {
            **context,
            "identity_matches_requested_member": identity_matches,
            "parsed_profile": profile,
        }
        self.archive.append("profile_enriched", **archived_profile)
        if not identity_matches:
            return
        captured_at_ns = _positive_int(context.get("captured_at_ns")) or time.time_ns()
        captured_at = ""
        if captured_at_ns:
            captured_at = dt.datetime.fromtimestamp(
                captured_at_ns / 1_000_000_000, dt.timezone.utc
            ).isoformat()
        equipment_snapshot = {
            "captured_at_ns": captured_at_ns,
            "captured_at": captured_at,
            "extraordinary_rating": profile.get("extraordinary_rating") or None,
            "equipment_score": profile.get("equipment_score") or None,
            "equipment_known_score": profile.get("equipment_known_score") or None,
            "equipment_score_complete": bool(
                profile.get("equipment_score_complete")
            ),
            "equipment_score_source": str(
                profile.get("equipment_score_source") or "unavailable"
            ),
            "equipment": [],
            "gems": None,
            "attributes": profile.get("combat_attributes"),
            "sets": None,
            "pvp_equipment_count": profile.get("pvp_equipment_count", 0),
            "active_word_count": profile.get("active_word_count", 0),
            "total_word_count": profile.get("total_word_count", 0),
            "source": RESPONSE_METHOD,
            "partial": not bool(profile.get("equipment_score_complete")),
        }
        for raw_item in profile.get("equipment", []):
            if not isinstance(raw_item, Mapping):
                continue
            metadata = raw_item.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            equipment_snapshot["equipment"].append(
                {
                    "slot": int(raw_item.get("slot", 0) or 0),
                    "slot_name": str(
                        raw_item.get("slot_name")
                        or equipment_slot_name(raw_item.get("slot"))
                    ),
                    "item_id": int(raw_item.get("item_id", 0) or 0),
                    "item_name": str(
                        raw_item.get("item_name")
                        or metadata.get("item_name")
                        or equipment_item_name(
                            raw_item.get("item_id"),
                            metadata.get("item_name_id"),
                        )
                    ),
                    "word_ids": list(raw_item.get("word_ids") or ()),
                    "auxiliary_id": int(raw_item.get("auxiliary_id", 0) or 0),
                    "word_score": int(raw_item.get("word_score", 0) or 0),
                    "item_score": int(raw_item.get("item_score", 0) or 0)
                    or None,
                    "base_score": int(raw_item.get("base_score", 0) or 0)
                    or None,
                    "random_score": int(raw_item.get("random_score", 0) or 0)
                    or None,
                    "enhance_score": _nonnegative_int_or_none(
                        raw_item.get("enhance_score")
                    ),
                    "enhance_level": _nonnegative_int_or_none(
                        raw_item.get("enhance_level")
                    ),
                    "enhance_completed_level": _nonnegative_int_or_none(
                        raw_item.get("enhance_completed_level")
                    ),
                    "enhance_stage_count": _nonnegative_int_or_none(
                        raw_item.get("enhance_stage_count")
                    ),
                    "enhance_stages": deepcopy(
                        raw_item.get("enhance_stages") or []
                    ),
                    "grow_body_id": int(raw_item.get("grow_body_id", 0) or 0)
                    or None,
                    "enhance_schedule": deepcopy(
                        raw_item.get("enhance_schedule") or []
                    ),
                    "enhance_completed_score": _nonnegative_int_or_none(
                        raw_item.get("enhance_completed_score")
                    ),
                    "enhance_level_score": _nonnegative_int_or_none(
                        raw_item.get("enhance_level_score")
                    ),
                    "enhance_level_remaining": _nonnegative_int_or_none(
                        raw_item.get("enhance_level_remaining")
                    ),
                    "enhance_level_progress_percent": (
                        _nonnegative_int_or_none(
                            raw_item.get("enhance_level_progress_percent")
                        )
                    ),
                    "enhance_overall_percent": _nonnegative_int_or_none(
                        raw_item.get("enhance_overall_percent")
                    ),
                    "enhance_progress_percent": _nonnegative_int_or_none(
                        raw_item.get("enhance_progress_percent")
                    ),
                    "next_enhance_level": _nonnegative_int_or_none(
                        raw_item.get("next_enhance_level")
                    ),
                    "next_enhance_score": _nonnegative_int_or_none(
                        raw_item.get("next_enhance_score")
                    ),
                    "next_enhance_increment": _nonnegative_int_or_none(
                        raw_item.get("next_enhance_increment")
                    ),
                    "next_enhance_remaining": _nonnegative_int_or_none(
                        raw_item.get("next_enhance_remaining")
                    ),
                    "known_score": int(raw_item.get("known_score", 0) or 0)
                    or None,
                    "total_score": int(raw_item.get("total_score", 0) or 0)
                    or None,
                    "score_complete": bool(raw_item.get("score_complete")),
                    "score_source": str(
                        raw_item.get("score_source") or "unavailable"
                    ),
                    "quality": int(raw_item.get("quality", 0) or 0) or None,
                    "quality_name": str(raw_item.get("quality_name") or ""),
                    "word_scores": list(raw_item.get("word_scores") or ()),
                    "affixes": deepcopy(raw_item.get("affixes") or []),
                    "special_affix": deepcopy(
                        raw_item.get("special_affix")
                        if isinstance(raw_item.get("special_affix"), Mapping)
                        else None
                    ),
                    "score_breakdown_total": _nonnegative_int_or_none(
                        raw_item.get("score_breakdown_total")
                    ),
                    "score_breakdown_complete": bool(
                        raw_item.get("score_breakdown_complete")
                    ),
                    "score_breakdown_matches": bool(
                        raw_item.get("score_breakdown_matches")
                    ),
                    "details": _compact_wire_value(raw_item.get("details")),
                    "equipment_mode": str(
                        raw_item.get("equipment_mode") or "unknown"
                    ),
                    "is_pvp": bool(raw_item.get("is_pvp")),
                    "word_class_types": list(
                        raw_item.get("word_class_types") or ()
                    ),
                    "active_word_count": int(
                        raw_item.get("active_word_count", 0) or 0
                    ),
                    "total_word_count": int(
                        raw_item.get("total_word_count", 0) or 0
                    ),
                    "metadata": {
                        key: metadata[key]
                        for key in (
                            "tag", "mode", "tag_name_id", "item_name_id",
                            "item_name",
                            "item_description_id", "icon", "quality",
                            "base_score", "item_level", "required_level",
                            "sub_type", "season_id", "random_group",
                        )
                        if key in metadata
                    },
                }
            )
        if equipment_snapshot["equipment_known_score"] is None:
            known_score = sum(
                _positive_int(item.get("known_score"))
                for item in equipment_snapshot["equipment"]
            )
            equipment_snapshot["equipment_known_score"] = known_score or None
        payload = {
            **session.archive_fields(),
            "batch_id": context.get("batch_id", ""),
            "correlation": context.get("correlation", 0),
            "user_token": token,
            "actor_id": int(member.get("actor_id", 0) or 0),
            "name": returned_name,
            "role_number": profile.get("role_number", 0),
            "profession_id": profile.get("profession_id", 0),
            "level": profile.get("level", 0),
            "extraordinary_rating": profile.get("extraordinary_rating", 0),
            "avatar_id": profile.get("avatar_id", 0),
            "avatar_frame_id": profile.get("avatar_frame_id", 0),
            "equipment_count": profile.get("equipment_count", 0),
            "pvp_equipment_count": profile.get("pvp_equipment_count", 0),
            "active_word_count": profile.get("active_word_count", 0),
            "total_word_count": profile.get("total_word_count", 0),
            "equipment_snapshot": equipment_snapshot,
            "source_method": RESPONSE_METHOD,
        }
        self.emit("equipment_profile", payload)

    def _publish_pending(self) -> None:
        with self.lock:
            catalog = self.catalog
            pending = list(self.pending_profiles)
            self.pending_profiles.clear()
        if catalog is None:
            with self.lock:
                self.pending_profiles.extend(pending)
            return
        for profile, session, context in pending:
            if self._local_scores_pending(profile, session):
                with self.lock:
                    self.pending_profiles.append((profile, session, context))
                continue
            self._publish_profile(
                catalog.enrich(
                    profile,
                    exact_scores=self._profile_exact_scores(profile, session),
                ),
                session,
                context,
            )

    def _republish_current_profiles_with_names(self, pid: object) -> None:
        game_pid = _positive_int(pid)
        with self.lock:
            catalog = self.catalog
            latest = self.latest_session
            profiles = list(self.latest_profiles.values())
        if catalog is None or latest is None or latest.game_pid != game_pid:
            return
        current_identity = self._query_identity(latest)
        for profile, session, context in profiles:
            if self._query_identity(session) != current_identity:
                continue
            self._publish_profile(
                catalog.enrich(
                    profile,
                    exact_scores=self._profile_exact_scores(profile, session),
                ),
                session,
                context,
            )

    @staticmethod
    def _query_identity(session: EquipmentQuerySession) -> tuple[object, ...]:
        return session.game_pid, session.local_user_token, session.party_session_id

    @classmethod
    def _member_query_key(cls, session, member):
        return (
            *cls._query_identity(session), str(member["user_token"]),
            _positive_int(member.get("extraordinary_rating")),
        )

    def _coalesce_queries(self, session: EquipmentQuerySession) -> EquipmentQuerySession:
        # Roster rows often arrive separately while the first request is being
        # prepared. Combine only unsent newcomers in the same role/team;
        # never query existing equipment again or combine different sessions.
        members = {str(row["user_token"]): row for row in session.members}
        member_count = session.member_count
        deferred = []
        combined = False
        for pending_queue in (self.priority_queries, self.query_commands):
            while not self.closing.is_set():
                try:
                    pending = pending_queue.get_nowait()
                except queue.Empty:
                    break
                if pending is None:
                    pending_queue.put(None)
                    break
                if (
                    self._query_identity(pending) != self._query_identity(session)
                    or len(set(members) | set(pending.tokens)) > MAX_TEAM_QUERY_TOKENS
                ):
                    deferred.append((pending_queue, pending))
                    continue
                members.update({str(row["user_token"]): row for row in pending.members})
                member_count = max(member_count, pending.member_count)
                combined = True
        for pending_queue, pending in deferred:
            pending_queue.put(pending)
        if not combined:
            return session
        tokens = tuple(sorted(members))
        return EquipmentQuerySession(
            game_pid=session.game_pid,
            capture_session_id=session.capture_session_id,
            local_user_token=session.local_user_token,
            party_session_id=session.party_session_id,
            member_count=max(member_count, len(tokens)),
            members=tuple(members[token] for token in tokens),
            tokens=tokens,
            priority=session.priority,
        )

    def _run_queries(self) -> None:
        # Only this worker can install a one-shot request, so hooks/sends stay
        # serial. The response worker remains free during scans/NTP waits.
        while not self.stop_event.is_set() and not self.closing.is_set():
            try:
                session = self.priority_queries.get_nowait()
            except queue.Empty:
                try:
                    session = self.query_commands.get(timeout=0.25)
                except queue.Empty:
                    continue
            if session is None:
                return
            if self.closing.wait(EQUIPMENT_QUERY_COALESCE_SECONDS):
                return
            if not session.priority:
                try:
                    urgent = self.priority_queries.get_nowait()
                except queue.Empty:
                    pass
                else:
                    self.query_commands.put(session)
                    session = urgent
            session = self._coalesce_queries(session)
            if self.stop_event.is_set() or self.closing.is_set():
                return
            with self.lock:
                latest = self.latest_session
                if latest is None or self._query_identity(latest) != self._query_identity(session):
                    continue
                # An urgent batch may have included members still present in
                # an older normal batch. Send each queued member only once.
                members = tuple(
                    member for member in session.members
                    if self._member_query_key(session, member) in self.requested_keys
                    and self.requested_keys[self._member_query_key(session, member)] is None
                )
            if not members:
                continue
            session = replace(
                session, members=members,
                tokens=tuple(sorted(str(member["user_token"]) for member in members)),
            )
            self._execute_query(session)

    def _run(self) -> None:
        # Drain responses queued before close's stop marker so moving archival
        # off capture dispatch does not lose the final match's raw evidence.
        while True:
            try:
                kind, payload = self.commands.get(timeout=0.25)
            except queue.Empty:
                if self.stop_event.is_set() or self.closing.is_set():
                    return
                continue
            if kind == "stop":
                return
            if kind == "response" and isinstance(payload, Mapping):
                self._process_response(payload)
            elif kind in {"catalog_ready", "local_equipment_ready"}:
                self._publish_pending()
            elif kind == "item_names_ready":
                self._republish_current_profiles_with_names(payload)


__all__ = [
    "EquipmentMetadataCatalog",
    "EquipmentProfileCoordinator",
    "EquipmentQueryArchive",
    "EquipmentQuerySession",
    "REQUEST_METHOD",
    "REQUEST_METHOD_ID",
    "RESPONSE_METHOD",
    "RESPONSE_METHOD_ID",
    "expand_wire_value",
    "parse_shape_response",
    "profile_matches_session",
]
