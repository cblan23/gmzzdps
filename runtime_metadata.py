#!/usr/bin/env python3
"""Read-only role metadata discovery for the live C7 client.

The game keeps profile rows in a LuaJIT GC64 heap.  Those rows contain the
persistent role ID and the Chinese role name, while C7.log records the live
combat entity ID assigned to that role.  Joining the two gives the same entity
IDs used by the damage callback without calling or modifying any game code.
"""

from __future__ import annotations

import ctypes
import re
import struct
import time
from pathlib import Path

from proc_inspect import (
    MEM_COMMIT,
    MEMORY_BASIC_INFORMATION,
    PAGE_GUARD,
    PAGE_NOACCESS,
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    kernel32,
    read_region,
    winerror,
)


MEM_PRIVATE = 0x00020000
PAGE_READWRITE = 0x04
MAX_USER_ADDRESS = (1 << 47) - 1

LUA_POINTER_MASK = (1 << 47) - 1
LUA_TAG_MASK = 0xFFFF800000000000
LUA_STRING_TAG = 0xFFFD800000000000

ROLE_FIELD = b"rolename"
ROLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,32}$")
ENTITY_LOG_RE = re.compile(
    rb"create_entity Entity:([A-Za-z0-9_-]{8,32}), Uid:(\d+) "
    rb"cname:(?:MainPlayer|AvatarActor)\b"
)


class RuntimeMetadataReader:
    """Discover live entity names without installing another hook."""

    def __init__(
        self,
        pid: int,
        module_base: int,
        module_path: str,
        *,
        stop_event=None,
    ):
        self.pid = int(pid)
        self.module_base = int(module_base)
        self.module_path = str(module_path)
        self.stop_event = stop_event
        self.process = int(
            kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, self.pid
            )
            or 0
        )
        if not self.process:
            raise winerror("OpenProcess(runtime metadata)")

        game_root = Path(self.module_path).resolve().parents[2]
        self.log_path = game_root / "Saved" / "Logs" / "C7.log"
        self.log_offset = 0
        self.log_remainder = b""
        self.uid_by_role: dict[str, int] = {}
        self.role_names: dict[str, str] = {}
        self.emitted_names: dict[int, str] = {}
        self.role_key_object = 0
        self.profile_scan_due = True
        self.last_profile_scan = 0.0
        self.last_discovery_attempt = 0.0

    def _stopped(self) -> bool:
        return bool(self.stop_event and self.stop_event.is_set())

    @property
    def alive(self) -> bool:
        return bool(self.process and read_region(self.process, self.module_base, 1))

    def close(self) -> None:
        if self.process:
            kernel32.CloseHandle(self.process)
            self.process = 0

    def _read_qword(self, address: int) -> int:
        data = read_region(self.process, address, 8)
        if data is None or len(data) != 8:
            return 0
        return struct.unpack("<Q", data)[0]

    def _read_lua_string(self, value: int, max_length: int = 128) -> str:
        if value & LUA_TAG_MASK != LUA_STRING_TAG:
            return ""
        obj = value & LUA_POINTER_MASK
        raw_length = read_region(self.process, obj + 0x14, 4)
        if raw_length is None or len(raw_length) != 4:
            return ""
        length = struct.unpack("<I", raw_length)[0]
        if length > max_length:
            return ""
        raw = read_region(self.process, obj + 0x18, length)
        if raw is None or len(raw) != length:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    @staticmethod
    def _clean_name(value: str) -> str:
        value = value.strip().replace("\x00", "")
        if not value or value.casefold() == "system" or len(value) > 64:
            return ""
        if any(ord(char) < 0x20 for char in value):
            return ""
        return value

    def _iter_readable_regions(self, start: int, end: int):
        cursor = max(0, start)
        end = min(end, MAX_USER_ADDRESS)
        while cursor < end and not self._stopped():
            mbi = MEMORY_BASIC_INFORMATION()
            queried = kernel32.VirtualQueryEx(
                self.process,
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
            readable = (
                mbi.State == MEM_COMMIT
                and mbi.Type == MEM_PRIVATE
                and not (mbi.Protect & PAGE_GUARD)
                and (mbi.Protect & 0xFF) == PAGE_READWRITE
            )
            lo = max(cursor, base)
            hi = min(end, region_end)
            if readable and hi > lo:
                yield lo, hi - lo
            cursor = max(cursor + 0x1000, region_end)

    def _scan_range(self, start: int, end: int, needle: bytes):
        overlap = max(0, len(needle) - 1)
        for region, size in self._iter_readable_regions(start, end):
            # Large resource heaps cannot contain normal Lua GC allocations and
            # are expensive to copy.  Lua pages in this build are small private
            # read/write allocations.
            if size > 64 * 1024 * 1024:
                continue
            position = region
            region_end = region + size
            tail = b""
            while position < region_end and not self._stopped():
                amount = min(4 * 1024 * 1024, region_end - position)
                block = read_region(self.process, position, amount)
                if block:
                    data = tail + block
                    origin = position - len(tail)
                    found_at = 0
                    while True:
                        found_at = data.find(needle, found_at)
                        if found_at < 0:
                            break
                        yield origin + found_at
                        found_at += 1
                    tail = data[-overlap:] if overlap else b""
                else:
                    tail = b""
                position += amount

    def _valid_gc_string_object(self, text_address: int, text: bytes) -> int:
        obj = text_address - 0x18
        if obj <= 0:
            return 0
        raw_length = read_region(self.process, obj + 0x14, 4)
        if raw_length != struct.pack("<I", len(text)):
            return 0
        tagged = LUA_STRING_TAG | obj
        return obj if self._read_lua_string(tagged, len(text)) == text.decode() else 0

    def _discover_role_key(self) -> bool:
        if self.role_key_object:
            tagged = LUA_STRING_TAG | self.role_key_object
            if self._read_lua_string(tagged, len(ROLE_FIELD)) == ROLE_FIELD.decode():
                return True
            self.role_key_object = 0

        # Lua allocations consistently live in the ordinary low private heaps.
        # Start with the allocator band used by the current client, then fall
        # back to the remaining low address space for a new game build/session.
        ranges = (
            (0x1C0000000, 0x200000000),
            (0x100000000, 0x1C0000000),
            (0x200000000, 0x300000000),
            (0x000000000, 0x100000000),
        )
        for start, end in ranges:
            for text_address in self._scan_range(start, end, ROLE_FIELD):
                obj = self._valid_gc_string_object(text_address, ROLE_FIELD)
                if obj:
                    self.role_key_object = obj
                    return True
        return False

    def _extract_profile(self, role_key_reference: int) -> tuple[str, str] | None:
        name = self._clean_name(
            self._read_lua_string(self._read_qword(role_key_reference - 8), 64)
        )
        if not name:
            return None

        role_id = ""
        for delta in range(-10, 11):
            key_at = role_key_reference + delta * 24
            key = self._read_lua_string(self._read_qword(key_at), 32)
            if key not in ("id", "entityId"):
                continue
            candidate = self._read_lua_string(self._read_qword(key_at - 8), 40)
            if ROLE_ID_RE.fullmatch(candidate):
                role_id = candidate
                if key == "id":
                    break
        return (role_id, name) if role_id else None

    def _scan_role_profiles(self) -> None:
        if not self.role_key_object:
            return
        tagged_key = LUA_STRING_TAG | self.role_key_object
        needle = struct.pack("<Q", tagged_key)
        start = max(0, self.role_key_object - 0x10000000)
        end = min(MAX_USER_ADDRESS, self.role_key_object + 0x10000000)
        discovered: dict[str, str] = {}
        for reference in self._scan_range(start, end, needle):
            profile = self._extract_profile(reference)
            if profile:
                role_id, name = profile
                discovered[role_id] = name
        if discovered:
            self.role_names.update(discovered)

    def _consume_log_bytes(self, data: bytes) -> bool:
        changed = False
        for match in ENTITY_LOG_RE.finditer(data):
            try:
                role_id = match.group(1).decode("ascii")
                entity_id = int(match.group(2))
            except (UnicodeDecodeError, ValueError):
                continue
            if entity_id and self.uid_by_role.get(role_id) != entity_id:
                self.uid_by_role[role_id] = entity_id
                changed = True
        return changed

    def _poll_game_log(self) -> bool:
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return False
        if size < self.log_offset:
            self.log_offset = 0
            self.log_remainder = b""
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(self.log_offset)
                data = handle.read()
                self.log_offset = handle.tell()
        except OSError:
            return False
        if not data:
            return False
        combined = self.log_remainder + data
        self.log_remainder = combined[-512:]
        return self._consume_log_bytes(combined)

    def poll(self) -> list[dict]:
        if not self.process or self._stopped():
            return []
        log_changed = self._poll_game_log()
        if log_changed:
            self.profile_scan_due = True

        now = time.monotonic()
        if not self.role_key_object and now - self.last_discovery_attempt >= 10.0:
            self.last_discovery_attempt = now
            if self._discover_role_key():
                self.profile_scan_due = True

        unresolved = any(
            role_id not in self.role_names for role_id in self.uid_by_role
        )
        should_scan = self.role_key_object and (
            (self.profile_scan_due and now - self.last_profile_scan >= 5.0)
            or (unresolved and now - self.last_profile_scan >= 60.0)
        )
        if should_scan and not self._stopped():
            self._scan_role_profiles()
            self.last_profile_scan = time.monotonic()
            self.profile_scan_due = False

        updates: list[dict] = []
        for role_id, entity_id in self.uid_by_role.items():
            name = self.role_names.get(role_id, "")
            if not name or self.emitted_names.get(entity_id) == name:
                continue
            self.emitted_names[entity_id] = name
            updates.append(
                {
                    "function": "LuaRoleProfile/C7.log",
                    "entity_id": entity_id,
                    "role_id": role_id,
                    "name": name,
                }
            )
        return updates

    def __enter__(self) -> "RuntimeMetadataReader":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
