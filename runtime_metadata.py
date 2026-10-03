#!/usr/bin/env python3
"""Read-only role metadata discovery for the live C7 client.

The game keeps profile rows in a LuaJIT GC64 heap.  Those rows contain the
persistent role ID and the Chinese role name, while C7.log records the live
combat entity ID assigned to that role.  Joining the two gives the same entity
IDs used by the damage callback without calling or modifying any game code.
"""

from __future__ import annotations

import ctypes
import math
import re
import struct
import time
from collections import OrderedDict
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
    winerror,
)


MEM_PRIVATE = 0x00020000
PAGE_READWRITE = 0x04
MAX_USER_ADDRESS = (1 << 47) - 1

LUA_POINTER_MASK = (1 << 47) - 1
LUA_TAG_MASK = 0xFFFF800000000000
LUA_STRING_TAG = 0xFFFD800000000000
LUA_TABLE_TAG = 0xFFFA000000000000

ROLE_FIELD = b"rolename"
LOCAL_SCORE_FIELD = b"CEScore"
ROLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,32}$")
ENTITY_LOG_RE = re.compile(
    rb"create_entity Entity:([A-Za-z0-9_-]{8,32}), Uid:(\d+) "
    rb"cname:(?:MainPlayer|AvatarActor)\b"
)
MAIN_PLAYER_ENTITY_LOG_RE = re.compile(
    rb"create_entity Entity:([A-Za-z0-9_-]{8,32}), Uid:(\d+) "
    rb"cname:MainPlayer\b"
)


def latest_main_player_role(
    log_path: Path, *, tail_bytes: int = 8 * 1024 * 1024
) -> tuple[str, int] | None:
    """Return the newest exact local-role token/actor pair from C7.log.

    A client that has been open for several hours can append enough chat and
    combat output for the most recent ``MainPlayer`` creation line to fall
    outside the usual tail window.  Starting the meter inside an instance then
    has no later role packet to recover from, so search older log chunks until
    the newest matching line is found instead of treating the first tail miss
    as an unknown character.
    """

    try:
        path = Path(log_path)
        size = path.stat().st_size
        chunk_size = max(4096, int(tail_bytes))
        with path.open("rb") as handle:
            end = size
            right_overlap = b""
            while end > 0:
                start = max(0, end - chunk_size)
                handle.seek(start)
                chunk = handle.read(end - start)
                data = chunk + right_overlap
                matches = list(MAIN_PLAYER_ENTITY_LOG_RE.finditer(data))
                if matches:
                    match = matches[-1]
                    try:
                        token = match.group(1).decode("ascii")
                        actor_id = int(match.group(2))
                    except (UnicodeDecodeError, ValueError, OverflowError):
                        return None
                    if not ROLE_ID_RE.fullmatch(token) or actor_id <= 0:
                        return None
                    return token, actor_id
                if start == 0:
                    break
                # The complete match is short, but retain a larger prefix of
                # the newer chunk so a line split exactly at a chunk boundary
                # is still recognized on the next backwards read.
                right_overlap = chunk[:512]
                end = start
    except (OSError, TypeError, ValueError, OverflowError):
        return None
    return None


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

    @staticmethod
    def _decode_lua_text(raw: bytes) -> str:
        """Decode client role strings stored as UTF-8 or legacy GBK."""

        utf8 = ""
        try:
            utf8 = raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
        if utf8 and (
            utf8.isascii()
            or any("\u3400" <= char <= "\u9fff" for char in utf8)
        ):
            return utf8
        try:
            gbk = raw.decode("gb18030")
        except UnicodeDecodeError:
            return utf8
        if any("\u3400" <= char <= "\u9fff" for char in gbk):
            return gbk
        return utf8 or gbk

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
        return self._decode_lua_text(raw)

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

        # Client updates move the Lua allocator between distinct private heap
        # bands. Check the current observed 0x3... band first, then retain the
        # older ranges so one build remains compatible across a rolling update.
        ranges = (
            (0x30000000, 0x40000000),
            (0x500000000, 0x600000000),
            (0x1C0000000, 0x200000000),
            (0x100000000, 0x1C0000000),
            (0x200000000, 0x300000000),
            (0x000000000, 0x30000000),
            (0x40000000, 0x100000000),
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

    def current_local_role(self) -> tuple[str, int] | None:
        return latest_main_player_role(self.log_path)

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


class LiveTeamProfileReader(RuntimeMetadataReader):
    """Read the client's current token-bound team profile without game calls.

    A late-start passive capture sees HP heartbeats, but unchanged field 11
    (the extraordinary rating) is not repeated. Locate the live actor/party
    model in the existing GC chain and validate its complete Lua table, token
    and layout. The two-second refresh reads only the cached bounded tables.
    """

    PROFILE_NODE_RADIUS = 32
    # The current client can keep the interned ``rolename`` key and the live
    # party/profile tables in Lua heap bands more than 256 MiB apart.  The old
    # radius found only stale character-select copies and therefore missed the
    # live ZhanLi/ceScore table after starting inside an instance.
    PROFILE_SCAN_RADIUS = 0x20000000
    FULL_RESCAN_SECONDS = 30.0
    MISSING_RESCAN_SECONDS = 10.0
    MAX_RATING = 1_000_000
    _RATING_KEYS = ("power", "ZhanLi", "ceScore", "CEScore")
    # ZhanLi is a legacy client power field. The current live sample reads
    # 78750 from it, but the user reports that it is not their extraordinary
    # rating. Keep aliases as diagnostics, NOT interchangeable display data.
    _VERIFIED_RATING_KEYS = frozenset({"power"})
    _BOUND_RATING_LAYOUTS = {
        "actor_CEScore": ("CEScore", "eid", "Name", "Profession", "Level"),
        "team_member_ceScore": ("ceScore", "id", "name", "profession", "level"),
        "team_roster_power": ("power", "id", "rolename", "school", "lv"),
    }
    LOCAL_SCORE_KEY_RADIUS = 0x04000000
    LOCAL_SCORE_NODE_RADIUS = 128
    MAX_LUA_TABLE_NODES = 8192
    MAX_LUA_GC_OBJECTS = 250_000
    LUA_GC_SCAN_SECONDS = 6.0
    _NUMBER_KEYS = frozenset(
        {
            *_RATING_KEYS,
            "school",
            "lv",
            "shortUid",
            "offlineTime",
            "enterTime",
        }
    )

    def __init__(
        self,
        pid: int,
        *,
        module_name: str = "C7-Win64-Shipping.exe",
        stop_event=None,
    ):
        module_base, _module_size, module_path = find_module(pid, module_name)
        super().__init__(
            pid,
            module_base,
            module_path,
            stop_event=stop_event,
        )
        self.profile_locators: dict[str, dict[str, object]] = {}
        self.last_live_profile_scan = 0.0
        self.live_profile_scan_tokens: set[str] = set()
        self.profile_lua_state = 0
        self.local_score_key_object = 0

    def current_dungeon_roster(self) -> dict[str, object] | None:
        """Read the exact current group, including projection names.

        A party can be split across the open world and an instance.  The local
        player's dungeon context is therefore metadata only; membership comes
        from ``GroupSystem`` and remains valid on either side of that boundary.
        """

        local_role = self.current_local_role()
        if not local_role or not self.process or self._stopped():
            return None
        local_token = str(local_role[0])
        state = self._discover_profile_lua_state()
        state_data = read_region(self.process, state, 0x50) if state else None
        if state_data is None or len(state_data) != 0x50:
            return None
        environment = struct.unpack_from("<Q", state_data, 0x48)[0] & LUA_POINTER_MASK
        root = LUA_TABLE_TAG | environment
        game = self._lua_table_value(root, "Game")
        group_system = self._lua_table_value(game, "GroupSystem")
        dungeon_system = self._lua_table_value(game, "DungeonSystem")
        model = self._lua_table_value(group_system, "model")
        if not model:
            return None

        dungeon_id = 0
        context = self._lua_table_value(
            dungeon_system, "currentDungeonContext"
        )
        context_table = (
            self._lua_table_nodes(context & LUA_POINTER_MASK)
            if context else None
        )
        if context_table is not None:
            context_fields = self._lua_table_fields(context_table, {})
            in_dungeon = context_fields.get("InDungeon")
            dungeon_field = context_fields.get("DungeonTemplateID")
            if (
                in_dungeon
                and self._lua_value_tag(in_dungeon[1]) == -3
                and dungeon_field
            ):
                dungeon_id = int(
                    self._lua_nonnegative_integer(dungeon_field[1]) or 0
                )

        index_map = self._lua_table_value(model, "MemberIndexMap")
        index_table = self._lua_table_nodes(index_map & LUA_POINTER_MASK) if index_map else None
        if index_table is None:
            return None
        current_tokens = set(self._lua_table_fields(index_table, {}))
        if local_token not in current_tokens or not 1 <= len(current_tokens) <= 150:
            return None

        group_all = self._lua_table_value(model, "GroupAllInfo")
        groups = self._lua_table_value(group_all, "groupInfoMap")
        if not groups:
            return None
        for group_id, group in self._lua_numeric_table_items(groups):
            teams = self._lua_table_value(group, "teamInfoMap")
            if not teams:
                continue
            members = []
            for team_id, team in self._lua_numeric_table_items(teams):
                member_table = self._lua_table_value(team, "members")
                for _index, raw_member in self._lua_numeric_table_items(member_table):
                    if self._lua_value_tag(raw_member) != -12:
                        continue
                    table = self._lua_table_nodes(raw_member & LUA_POINTER_MASK)
                    if table is None:
                        continue
                    fields = self._lua_table_fields(table, {})
                    identity = fields.get("id")
                    name_field = fields.get("name")
                    if not identity or not name_field:
                        continue
                    token = self._read_lua_string(identity[1], 40)
                    name = self._clean_name(self._read_lua_string(name_field[1], 64))
                    if token not in current_tokens or not name:
                        continue
                    member = {
                        "user_token": token,
                        "name": name,
                        "team_id": int(team_id),
                        "member_index": int(_index),
                        "is_ai": "botID" in fields,
                    }
                    for source, target, maximum in (
                        ("profession", "profession_id", 10_000_000),
                        ("level", "level", 10_000),
                        ("shortUid", "role_number", 10**15),
                        ("ceScore", "extraordinary_rating", self.MAX_RATING),
                    ):
                        node = fields.get(source)
                        value = (
                            self._lua_nonnegative_integer(node[1], maximum=maximum)
                            if node else None
                        )
                        if value is not None and value > 0:
                            member[target] = value
                    members.append(member)
            tokens = {member["user_token"] for member in members}
            if (
                local_token in tokens
                and len(members) == len(tokens) == len(current_tokens)
                and tokens == current_tokens
            ):
                return {
                    "local_user_token": local_token,
                    "dungeon_id": dungeon_id,
                    "group_id": int(group_id),
                    "members": members,
                }
        return None

    def _discover_local_score_key(self) -> bool:
        if self.local_score_key_object:
            tagged = LUA_STRING_TAG | self.local_score_key_object
            if self._read_lua_string(tagged, len(LOCAL_SCORE_FIELD)) == LOCAL_SCORE_FIELD.decode():
                return True
            self.local_score_key_object = 0
        if not self._discover_role_key():
            return False
        start = max(0, self.role_key_object - self.LOCAL_SCORE_KEY_RADIUS)
        end = min(MAX_USER_ADDRESS, self.role_key_object + self.LOCAL_SCORE_KEY_RADIUS)
        for text_address in self._scan_range(start, end, LOCAL_SCORE_FIELD):
            obj = self._valid_gc_string_object(text_address, LOCAL_SCORE_FIELD)
            if obj:
                self.local_score_key_object = obj
                return True
        return False

    def _local_actor_score_candidate(self, reference: int):
        rating = self._positive_integer(
            self._lua_number(self._read_qword(reference - 8)),
            maximum=self.MAX_RATING,
        )
        if rating is None:
            return None
        maximum_node = 0
        maximum_rating = None
        for delta in range(-self.LOCAL_SCORE_NODE_RADIUS,
                           self.LOCAL_SCORE_NODE_RADIUS + 1):
            key_at = reference + delta * 24
            key = self._read_lua_string(self._read_qword(key_at), 32)
            if key != "MaxCEScore":
                continue
            parsed = self._positive_integer(
                self._lua_number(self._read_qword(key_at - 8)),
                maximum=self.MAX_RATING,
            )
            if parsed is not None:
                maximum_node = key_at
                maximum_rating = parsed
                break
        if maximum_rating is None or maximum_rating < rating:
            return None
        return {
            "kind": "local_actor_CEScore_pair",
            "rating_key_at": reference,
            "maximum_key_at": maximum_node,
        }

    def _scan_local_actor_score(self, tokens: set[str]) -> set[str]:
        local_role = self.current_local_role()
        if not local_role or local_role[0] not in tokens:
            return set()
        token = str(local_role[0])
        locator = self.profile_locators.get(token, {})
        if (
            locator.get("kind") == "local_actor_CEScore_pair"
            and self._cached_profile(token) is not None
        ):
            return {token}
        if not self._discover_local_score_key():
            return set()
        tagged_key = LUA_STRING_TAG | self.local_score_key_object
        needle = struct.pack("<Q", tagged_key)
        candidates = []
        for start, end in (
            (0x100000000, 0x1C0000000),
            (0x1C0000000, 0x200000000),
            (0x200000000, 0x300000000),
            (0x000000000, 0x100000000),
            (0x500000000, 0x600000000),
        ):
            for reference in self._scan_range(start, end, needle):
                candidate = self._local_actor_score_candidate(reference)
                if candidate is not None:
                    candidates.append(candidate)
        # A unique live CEScore/MaxCEScore pair is the only safe self-only
        # fallback when the actor table has not materialized its eid field.
        if len(candidates) != 1:
            return set()
        self.profile_locators[token] = {
            **candidates[0],
            "user_token": token,
            "actor_id": int(local_role[1]),
        }
        return {token}

    @staticmethod
    def _lua_number(raw: int) -> float | None:
        signed_tag = ctypes.c_int64(raw).value >> 47
        if signed_tag == -14:
            return float(ctypes.c_int32(raw & 0xFFFFFFFF).value)
        value = struct.unpack("<d", struct.pack("<Q", raw))[0]
        if not math.isfinite(value) or abs(value) > 1e15:
            return None
        return value

    @classmethod
    def _positive_integer(
        cls, value: float | None, *, maximum: int
    ) -> int | None:
        if value is None or value <= 0 or value > maximum:
            return None
        rounded = int(value)
        if abs(value - rounded) > 0.001:
            return None
        return rounded

    @classmethod
    def _rating_fields(cls, key: str, value: int) -> dict[str, object]:
        fields = {'client_rating_key': key, 'client_rating_candidate': value}
        if key in cls._VERIFIED_RATING_KEYS:
            fields['extraordinary_rating'] = value
        return fields

    def _lua_global(self, state: int) -> int:
        """Validate a GC64 thread and its existing string/GC state, read-only."""
        raw = read_region(self.process, state, 0x38)
        if raw is None or len(raw) != 0x38 or raw[9] != 6:
            return 0
        global_address = struct.unpack_from("<Q", raw, 0x10)[0]
        base, top, maximum = struct.unpack_from("<QQQ", raw, 0x20)
        if not (0x10000 <= base <= top <= maximum < MAX_USER_ADDRESS
                and maximum - base < 64 * 1024 * 1024):
            return 0
        head = read_region(self.process, global_address, 0xB0)
        if head is None or len(head) != 0xB0:
            return 0
        buckets, mask, count = struct.unpack_from("<QII", head, 0x98)
        if not (0x10000 <= buckets < MAX_USER_ADDRESS and 0 < mask < 2**24
                and mask & (mask + 1) == 0 and 0 < count <= 4 * mask):
            return 0
        root = struct.unpack_from("<Q", head, 0x28)[0] & LUA_POINTER_MASK
        root_head = read_region(self.process, root, 0x18)
        if root_head is None or len(root_head) != 0x18 or not 4 <= root_head[9] <= 12:
            return 0
        return global_address

    def _discover_profile_lua_state(self) -> int:
        state = getattr(self, "profile_lua_state", 0)
        if state and self._lua_global(state):
            return state
        self.profile_lua_state = 0
        if not self._discover_role_key():
            return 0
        # The current client allocates the live Lua state near the top of the
        # 32-bit address band. Check that small observed band before walking the
        # much larger fallback ranges below.
        for signature in (b"\x06\x01\x00\x00\x00\x00\x00",
                          b"\x06\x00\x00\x00\x00\x00\x00"):
            for reference in self._scan_range(
                0xF0000000, 0x100000000, signature
            ):
                state = reference - 9
                if self._lua_global(state):
                    self.profile_lua_state = state
                    return state
        # Locate a live thread rather than assuming that profile tables live
        # beside an interned key. The current heap spans several GiB.
        start = max(0, self.role_key_object - self.PROFILE_SCAN_RADIUS)
        end = min(MAX_USER_ADDRESS, self.role_key_object + self.PROFILE_SCAN_RADIUS)
        for signature in (b"\x06\x01\x00\x00\x00\x00\x00",
                          b"\x06\x00\x00\x00\x00\x00\x00"):
            for reference in self._scan_range(start, end, signature):
                state = reference - 9
                if self._lua_global(state):
                    self.profile_lua_state = state
                    return state

        # The September client keeps the interned profile keys in the 0x5...
        # Lua heap while the owning Lua state lives in a separate low-address
        # allocation.  Search that state band only after the fast local lookup
        # fails; a valid state is cached and revalidated on later polls.
        for start, end in (
            (0x000000000, 0xF0000000),
            (0x100000000, 0x300000000),
            (0x500000000, 0x600000000),
        ):
            for signature in (b"\x06\x01\x00\x00\x00\x00\x00",
                              b"\x06\x00\x00\x00\x00\x00\x00"):
                for reference in self._scan_range(start, end, signature):
                    state = reference - 9
                    if self._lua_global(state):
                        self.profile_lua_state = state
                        return state
        return 0

    def _lua_table_nodes(self, address: int):
        head = read_region(self.process, address, 0x40)
        if head is None or len(head) != 0x40 or head[9] != 11:
            return None
        nodes = struct.unpack_from("<Q", head, 0x28)[0]
        mask = struct.unpack_from("<I", head, 0x34)[0]
        if (not 0x10000 <= nodes < MAX_USER_ADDRESS or mask & (mask + 1)
                or mask >= self.MAX_LUA_TABLE_NODES):
            return None
        data = read_region(self.process, nodes, (mask + 1) * 24)
        if data is None or len(data) != (mask + 1) * 24:
            return None
        # Never join nodes read across a table resize.
        current = read_region(self.process, address, 0x40)
        if (current is None or len(current) != 0x40 or current[9] != 11
                or current[0x28:0x38] != head[0x28:0x38]):
            return None
        return head, nodes, mask, data

    def _lua_table_fields(self, table, strings: dict[int, str]):
        _head, nodes, _mask, data = table
        fields = {}
        for offset in range(0, len(data), 24):
            value, key = struct.unpack_from("<QQ", data, offset)
            if key & LUA_TAG_MASK != LUA_STRING_TAG:
                continue
            if key not in strings:
                strings[key] = self._read_lua_string(key, 80)
            name = strings[key]
            if name:
                fields[name] = (nodes + offset + 8, value)
        return fields

    @staticmethod
    def _lua_value_tag(raw: int) -> int | None:
        """Return a LuaJIT GC64 tag, or ``None`` for a numeric value."""

        tag = ctypes.c_int64(int(raw)).value >> 47
        return tag if -14 <= tag <= -1 else None

    def _lua_table_value(self, raw_table: int, name: str) -> int:
        if self._lua_value_tag(raw_table) != -12:
            return 0
        table = self._lua_table_nodes(raw_table & LUA_POINTER_MASK)
        if table is None:
            return 0
        node = self._lua_table_fields(table, {}).get(name)
        return int(node[1]) if node is not None else 0

    def _lua_numeric_table_items(self, raw_table: int) -> list[tuple[int, int]]:
        """Read integer-keyed Lua table entries without mutating the client."""

        if self._lua_value_tag(raw_table) != -12:
            return []
        table = self._lua_table_nodes(raw_table & LUA_POINTER_MASK)
        if table is None:
            return []
        head, _nodes, _mask, data = table
        result: dict[int, int] = {}

        array_address = struct.unpack_from("<Q", head, 0x10)[0]
        array_size = struct.unpack_from("<I", head, 0x30)[0]
        if 0 < array_size <= self.MAX_LUA_TABLE_NODES and array_address:
            array = read_region(self.process, array_address, array_size * 8)
            if array is not None and len(array) == array_size * 8:
                for index in range(array_size):
                    value = struct.unpack_from("<Q", array, index * 8)[0]
                    if self._lua_value_tag(value) != -1:
                        result[index] = value

        for offset in range(0, len(data), 24):
            value, key = struct.unpack_from("<QQ", data, offset)
            if self._lua_value_tag(value) == -1:
                continue
            key_value = self._lua_number(key)
            if key_value is None or not math.isfinite(key_value):
                continue
            integer_key = int(key_value)
            if abs(key_value - integer_key) <= 0.001:
                result[integer_key] = value
        return sorted(result.items())

    def _lua_nonnegative_integer(self, raw: int, maximum: int = 100_000_000) -> int | None:
        value = self._lua_number(raw)
        if value is None or not math.isfinite(value):
            return None
        parsed = int(value)
        if abs(value - parsed) > 0.001 or parsed < 0 or parsed > maximum:
            return None
        return parsed

    def local_equipment_scores(
        self, expected_token: str = ""
    ) -> dict[int, dict[str, object]]:
        """Return exact scores for the currently logged-in character's equipment.

        ``RetOtherRoleShapeData`` supplies base and random/special marks but not
        the character's body-enhancement mark.  The already-loaded local Lua
        equipment model contains the base/random marks, exact enhancement mark,
        and per-stage progress, so correlate it by slot and item ID and compose
        the final score from those verified parts.  This remains a read-only
        lookup; it must never be used for another character.
        """

        expected = str(expected_token or "").strip()
        local_role = self.current_local_role()
        if expected and (local_role is None or str(local_role[0]) != expected):
            return {}
        state = self._discover_profile_lua_state()
        if not state:
            return {}
        state_data = read_region(self.process, state, 0x50)
        if state_data is None or len(state_data) != 0x50:
            return {}
        environment = struct.unpack_from("<Q", state_data, 0x48)[0] & LUA_POINTER_MASK
        current = LUA_TABLE_TAG | environment
        for name in ("Game", "EquipmentSystem", "model"):
            current = self._lua_table_value(current, name)
            if not current:
                return {}

        growth_info: dict[int, dict[str, object]] = {}
        body = self._lua_table_value(current, "equipmentBodyInfo")
        enhance = self._lua_table_value(body, "enhanceInfo")
        growth_slots = self._lua_table_value(enhance, "slots")
        for slot, raw_growth in self._lua_numeric_table_items(growth_slots):
            mark = self._lua_nonnegative_integer(
                self._lua_table_value(raw_growth, "mark")
            )
            if slot > 0 and mark is not None:
                stages = self._lua_table_value(raw_growth, "stages")
                stage_levels: list[dict[str, int]] = []
                completed = 0
                active_stage = 0
                active_stage_percent = 0
                total_stage_percent = 0
                for stage, raw_stage in self._lua_numeric_table_items(stages):
                    level = self._lua_nonnegative_integer(
                        self._lua_table_value(raw_stage, "level"), maximum=10_000
                    )
                    percent = self._lua_nonnegative_integer(
                        self._lua_table_value(raw_stage, "percent"), maximum=100
                    )
                    if stage <= 0 or level is None:
                        continue
                    stage_levels.append(
                        {
                            "stage": stage,
                            "level": level,
                            "percent": percent or 0,
                        }
                    )
                    total_stage_percent += percent or 0
                    if percent == 100:
                        completed += 1
                    if percent and stage >= active_stage:
                        active_stage = stage
                        active_stage_percent = percent
                stage_count = len(stage_levels)
                growth_info[slot] = {
                    "enhance_score": mark,
                    # The UI labels an in-progress fifth stage as ``+5`` even
                    # before it reaches 100%.  Keep the fully completed level
                    # separately so a partial +5 is never reported as +4.
                    "enhance_level": active_stage,
                    "enhance_completed_level": completed,
                    "enhance_level_progress_percent": active_stage_percent,
                    "enhance_overall_percent": (
                        total_stage_percent // stage_count if stage_count else 0
                    ),
                    "enhance_stage_count": stage_count,
                    "enhance_stages": stage_levels,
                }

        slot_info = self._lua_table_value(current, "equipmentSlotInfo")
        slots = self._lua_table_value(slot_info, "slots")
        result: dict[int, dict[str, object]] = {}
        for table_slot, raw_item in self._lua_numeric_table_items(slots):
            slot = self._lua_nonnegative_integer(
                self._lua_table_value(raw_item, "slotIdx"), maximum=255
            )
            slot = slot or table_slot
            item_id = self._lua_nonnegative_integer(
                self._lua_table_value(raw_item, "itemId")
            )
            raw_total_score = self._lua_table_value(raw_item, "score")
            total_score = (
                self._lua_nonnegative_integer(raw_total_score)
                if raw_total_score
                else None
            )
            quality = self._lua_nonnegative_integer(
                self._lua_table_value(raw_item, "quality"), maximum=255
            )
            prop_info = self._lua_table_value(raw_item, "equipmentPropInfo")
            base_info = self._lua_table_value(prop_info, "basePropInfo")
            random_info = self._lua_table_value(prop_info, "randomPropInfo")
            base_score = self._lua_nonnegative_integer(
                self._lua_table_value(base_info, "mark")
            )
            random_score = self._lua_nonnegative_integer(
                self._lua_table_value(random_info, "mark")
            )
            if not slot or not item_id:
                continue
            growth = growth_info.get(slot, {})
            raw_enhance_score = growth.get("enhance_score")
            enhance_score = (
                int(raw_enhance_score)
                if isinstance(raw_enhance_score, int)
                and not isinstance(raw_enhance_score, bool)
                and raw_enhance_score >= 0
                else None
            )
            if (
                base_score is not None
                and random_score is not None
                and enhance_score is not None
            ):
                # The current local item table has no final ``score`` field.
                # Its body-growth ``mark`` is the exact earned enhancement
                # score (including partial progress), so compose the same
                # total shown by the client from the three verified parts.
                total_score = base_score + random_score + enhance_score
            result[slot] = {
                "item_id": item_id,
                "base_score": base_score,
                "random_score": random_score,
                "enhance_score": enhance_score,
                "enhance_level": growth.get("enhance_level"),
                "enhance_completed_level": growth.get(
                    "enhance_completed_level"
                ),
                "enhance_level_progress_percent": growth.get(
                    "enhance_level_progress_percent"
                ),
                "enhance_overall_percent": growth.get(
                    "enhance_overall_percent"
                ),
                "enhance_stage_count": growth.get("enhance_stage_count"),
                "enhance_stages": growth.get("enhance_stages", []),
                "total_score": total_score,
                "quality": quality,
                "score_source": "local_equipment_model",
            }
        return result

    def _actor_profile_class(self, head: bytes, strings: dict[int, str]) -> str:
        metatable = self._lua_table_nodes(struct.unpack_from("<Q", head, 0x20)[0])
        if metatable is None:
            return ""
        meta_fields = self._lua_table_fields(metatable, strings)
        index = meta_fields.get("__index")
        if index is None or ctypes.c_int64(index[1]).value >> 47 != -12:
            return ""
        class_table = self._lua_table_nodes(index[1] & LUA_POINTER_MASK)
        if class_table is None:
            return ""
        node = self._lua_table_fields(class_table, strings).get("__cname")
        return self._read_lua_string(node[1], 80) if node else ""

    def _bound_profile_candidate(self, address: int, table, tokens: set[str], strings):
        fields = self._lua_table_fields(table, strings)
        for binding, (rating_key, id_key, name_key, school_key, level_key) in self._BOUND_RATING_LAYOUTS.items():
            identity, rating_node, name_node = (fields.get(key) for key in (id_key, rating_key, name_key))
            if identity is None or rating_node is None or name_node is None:
                continue
            token = self._read_lua_string(identity[1], 40)
            if token not in tokens:
                continue
            if binding == "actor_CEScore":
                if self._actor_profile_class(table[0], strings) not in {"MainPlayer", "AvatarActor"}:
                    continue
            elif binding == "team_member_ceScore" and not all(
                key in fields for key in ("hp", "maxHp", "memberIndex", "profession")
            ):
                # A generic friend/guild record is not the current party model.
                continue
            elif binding == "team_roster_power" and not all(
                key in fields
                for key in (
                    "groupID", "teamLeaderName", "roles", "offlineTime", "enterTime"
                )
            ):
                continue
            name = self._clean_name(self._read_lua_string(name_node[1], 64))
            rating = self._positive_integer(self._lua_number(rating_node[1]), maximum=self.MAX_RATING)
            if not name or rating is None:
                continue
            profile = {
                "user_token": token, "name": name, "extraordinary_rating": rating,
                "client_rating_key": rating_key, "client_rating_candidate": rating,
                "client_rating_binding": binding,
            }
            locator = {
                "kind": "bound_lua_table", "table_address": address,
                "table_nodes": table[1], "table_mask": table[2],
                "reference": name_node[0], "name_key": name_key,
                "id_key": id_key, "id_key_at": identity[0],
                "rating_key": rating_key, "rating_key_at": rating_node[0],
                "rating_binding": binding,
            }
            for key, output, maximum in ((school_key, "profession_id", 10_000_000),
                                          (level_key, "level", 10_000),
                                          ("shortUid", "role_number", 10**15)):
                node = fields.get(key)
                value = self._positive_integer(self._lua_number(node[1]), maximum=maximum) if node else None
                if value is not None:
                    profile[output] = value
                    locator[f"{key}_key_at"] = node[0]
            return profile, locator
        return None

    def _scan_bound_lua_profiles(self, tokens: set[str]) -> set[str]:
        state = self._discover_profile_lua_state()
        global_address = self._lua_global(state) if state else 0
        if not global_address:
            return set()
        address = self._read_qword(global_address + 0x28) & LUA_POINTER_MASK
        seen, found, strings = set(), set(), {}
        local_role = self.current_local_role()
        local_token = local_role[0] if local_role else ""
        local_actor_found = local_token not in tokens
        pages = OrderedDict()
        deadline = time.monotonic() + self.LUA_GC_SCAN_SECONDS
        while (address and address not in seen and len(seen) < self.MAX_LUA_GC_OBJECTS
               and time.monotonic() < deadline and not self._stopped()):
            page = address & ~0xFFFF
            if page not in pages:
                pages[page] = read_region(self.process, page, 0x10000) or b""
                if len(pages) > 256:
                    pages.popitem(last=False)
            else:
                pages.move_to_end(page)
            offset = address - page
            head = pages[page][offset:offset + 0x40]
            if len(head) != 0x40:
                head = read_region(self.process, address, 0x40)
            if head is None or len(head) != 0x40:
                break
            seen.add(address)
            if head[9] == 11:
                table = self._lua_table_nodes(address)
                candidate = self._bound_profile_candidate(address, table, tokens - found, strings) if table else None
                if candidate is None and local_token in found and not local_actor_found and table:
                    candidate = self._bound_profile_candidate(address, table, {local_token}, strings)
                if candidate is not None:
                    profile, locator = candidate
                    if profile['user_token'] == local_token:
                        local_actor_found = locator['rating_binding'] == 'actor_CEScore'
                    self.profile_locators[profile["user_token"]] = locator
                    found.add(profile["user_token"])
                    if found == tokens and local_actor_found:
                        break
            address = struct.unpack_from("<Q", head)[0] & LUA_POINTER_MASK
        return found

    def _profile_candidate(
        self, reference: int, wanted_token: str
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        parsed = self._extract_profile(reference)
        if parsed is None or parsed[0] != wanted_token:
            return None
        _token, name = parsed

        nodes: dict[str, tuple[int, int]] = {}
        for delta in range(-self.PROFILE_NODE_RADIUS, self.PROFILE_NODE_RADIUS + 1):
            key_at = reference + delta * 24
            key = self._read_lua_string(self._read_qword(key_at), 64)
            if key not in self._NUMBER_KEYS and key not in {"id", "entityId"}:
                continue
            value = self._read_qword(key_at - 8)
            if key in {"id", "entityId"}:
                if self._read_lua_string(value, 40) != wanted_token:
                    continue
            nodes.setdefault(key, (key_at, value))

        id_node = nodes.get("id") or nodes.get("entityId")
        rating_key = next(
            (key for key in self._RATING_KEYS if key in nodes), ""
        )
        rating_node = nodes.get(rating_key)
        if id_node is None or rating_node is None:
            return None
        # The identifying key must be in the same compact table neighborhood
        # as ``rolename``.  This prevents a nearby allocator object from being
        # joined to the score merely because two tables are adjacent.
        if abs(int(id_node[0]) - reference) > 10 * 24:
            return None

        rating = self._positive_integer(
            self._lua_number(rating_node[1]), maximum=self.MAX_RATING
        )
        if rating is None:
            return None
        profile: dict[str, object] = {
            "user_token": wanted_token,
            "name": name,
            **self._rating_fields(rating_key, rating),
        }
        # A compact neighborhood is useful for diagnostics/names, but cannot
        # prove that a score belongs to the same Lua table, even for ``power``.
        profile.pop('extraordinary_rating', None)
        locator: dict[str, object] = {
            "reference": reference,
            "id_key_at": id_node[0],
            "rating_key": rating_key,
            "rating_key_at": rating_node[0],
        }
        for source_key, output_key, maximum in (
            ("school", "profession_id", 10_000_000),
            ("lv", "level", 10_000),
            ("shortUid", "role_number", 10**15),
        ):
            node = nodes.get(source_key)
            if node is None:
                continue
            parsed_value = self._positive_integer(
                self._lua_number(node[1]), maximum=maximum
            )
            if parsed_value is not None:
                profile[output_key] = parsed_value
                locator[f"{source_key}_key_at"] = node[0]

        offline = nodes.get("offlineTime")
        enter = nodes.get("enterTime")
        locator["selection_score"] = (
            1 if offline and self._lua_number(offline[1]) == 0 else 0,
            self._positive_integer(
                self._lua_number(enter[1]) if enter else None,
                maximum=10**12,
            )
            or 0,
            len(profile),
            reference,
        )
        return profile, locator

    def _cached_profile(self, token: str) -> dict[str, object] | None:
        locator = self.profile_locators.get(token)
        if not locator:
            return None
        if locator.get("kind") == "local_actor_CEScore_pair":
            local_role = self.current_local_role()
            if (
                not local_role
                or str(local_role[0]) != token
                or int(local_role[1]) != int(locator.get("actor_id", 0) or 0)
            ):
                return None
            rating_key_at = int(locator.get("rating_key_at", 0) or 0)
            maximum_key_at = int(locator.get("maximum_key_at", 0) or 0)
            if (
                self._read_lua_string(self._read_qword(rating_key_at), 32)
                != "CEScore"
                or self._read_lua_string(self._read_qword(maximum_key_at), 32)
                != "MaxCEScore"
            ):
                return None
            rating = self._positive_integer(
                self._lua_number(self._read_qword(rating_key_at - 8)),
                maximum=self.MAX_RATING,
            )
            maximum_rating = self._positive_integer(
                self._lua_number(self._read_qword(maximum_key_at - 8)),
                maximum=self.MAX_RATING,
            )
            if rating is None or maximum_rating is None or maximum_rating < rating:
                return None
            return {
                "user_token": token,
                "extraordinary_rating": rating,
                "client_rating_key": "CEScore",
                "client_rating_candidate": rating,
                "client_rating_binding": "local_actor_CEScore_pair",
                "client_rating_observed_filetime": (
                    time.time_ns() // 100 + 116444736000000000
                ),
            }
        if locator.get("kind") == "bound_lua_table":
            observed_at = time.time_ns() // 100 + 116444736000000000
            table = self._lua_table_nodes(int(locator["table_address"]))
            if table is None or table[1:3] != (locator["table_nodes"], locator["table_mask"]):
                return None
            candidate = self._bound_profile_candidate(int(locator["table_address"]), table, {token}, {})
            if candidate is None:
                return None
            return {**candidate[0], 'client_rating_observed_filetime': observed_at}
        try:
            reference = int(locator["reference"])
            id_key_at = int(locator["id_key_at"])
            rating_key = str(locator["rating_key"])
            rating_key_at = int(locator["rating_key_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if (
            self._read_lua_string(self._read_qword(reference), 64) != "rolename"
            or self._read_lua_string(self._read_qword(id_key_at), 64)
            not in {"id", "entityId"}
            or self._read_lua_string(self._read_qword(id_key_at - 8), 40)
            != token
            or rating_key not in self._RATING_KEYS
            or self._read_lua_string(self._read_qword(rating_key_at), 64)
            != rating_key
        ):
            return None
        name = self._clean_name(
            self._read_lua_string(self._read_qword(reference - 8), 64)
        )
        rating = self._positive_integer(
            self._lua_number(self._read_qword(rating_key_at - 8)),
            maximum=self.MAX_RATING,
        )
        if not name or rating is None:
            return None
        profile: dict[str, object] = {
            "user_token": token,
            "name": name,
            **self._rating_fields(rating_key, rating),
        }
        profile.pop('extraordinary_rating', None)
        for source_key, output_key, maximum in (
            ("school", "profession_id", 10_000_000),
            ("lv", "level", 10_000),
            ("shortUid", "role_number", 10**15),
        ):
            raw_address = locator.get(f"{source_key}_key_at")
            try:
                key_at = int(raw_address or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not key_at or self._read_lua_string(
                self._read_qword(key_at), 64
            ) != source_key:
                continue
            value = self._positive_integer(
                self._lua_number(self._read_qword(key_at - 8)),
                maximum=maximum,
            )
            if value is not None:
                profile[output_key] = value
        return profile

    def _scan_live_profiles(self, tokens: set[str]) -> None:
        tokens = tokens - self._scan_bound_lua_profiles(tokens)
        if not tokens:
            return
        tokens = tokens - self._scan_local_actor_score(tokens)
        if not tokens or not self._discover_role_key():
            return
        tagged_key = LUA_STRING_TAG | self.role_key_object
        needle = struct.pack("<Q", tagged_key)
        start = max(0, self.role_key_object - self.PROFILE_SCAN_RADIUS)
        end = min(
            MAX_USER_ADDRESS, self.role_key_object + self.PROFILE_SCAN_RADIUS
        )
        candidates: dict[
            str, list[tuple[dict[str, object], dict[str, object]]]
        ] = {token: [] for token in tokens}
        for reference in self._scan_range(start, end, needle):
            parsed = self._extract_profile(reference)
            if parsed is None or parsed[0] not in tokens:
                continue
            candidate = self._profile_candidate(reference, parsed[0])
            if candidate is not None:
                candidates[parsed[0]].append(candidate)
        for token, values in candidates.items():
            if not values:
                self.profile_locators.pop(token, None)
                continue
            _profile, locator = max(
                values,
                key=lambda value: tuple(value[1].get("selection_score", ())),
            )
            self.profile_locators[token] = locator

    def snapshot(self, tokens) -> list[dict[str, object]]:
        wanted = {
            str(token).strip()
            for token in tokens
            if ROLE_ID_RE.fullmatch(str(token).strip())
        }
        if not wanted or not self.process or self._stopped():
            return []
        now = time.monotonic()
        missing = wanted - set(self.profile_locators)
        elapsed = now - self.last_live_profile_scan
        new_missing = missing - self.live_profile_scan_tokens
        should_scan = bool(
            not self.last_live_profile_scan
            or new_missing
            or (missing and elapsed >= self.MISSING_RESCAN_SECONDS)
            or elapsed >= self.FULL_RESCAN_SECONDS
        )
        if should_scan:
            self._scan_live_profiles(wanted)
            self.last_live_profile_scan = time.monotonic()
            self.live_profile_scan_tokens.update(wanted)

        result: list[dict[str, object]] = []
        invalid: set[str] = set()
        for token in sorted(wanted):
            profile = self._cached_profile(token)
            if profile is None:
                invalid.add(token)
            else:
                result.append(profile)
        if invalid:
            for token in invalid:
                self.profile_locators.pop(token, None)
            elapsed = time.monotonic() - self.last_live_profile_scan
            newly_invalid = invalid - self.live_profile_scan_tokens
            if newly_invalid or elapsed >= self.MISSING_RESCAN_SECONDS:
                self._scan_live_profiles(invalid)
                self.last_live_profile_scan = time.monotonic()
                self.live_profile_scan_tokens.update(invalid)
                for token in sorted(invalid):
                    profile = self._cached_profile(token)
                    if profile is not None:
                        result.append(profile)
        return result
