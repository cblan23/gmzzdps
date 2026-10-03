"""Read-only target initialization for entities already observed on the wire.

Damage, healing and timestamps still come from Npcap. This bounded reader
copies exact existing entity/template fields; it never calls game functions.
"""
from __future__ import annotations

import json
import struct
import threading
import time
from collections import Counter
from pathlib import Path

import proc_inspect
from monster_metadata import load_validated_reserved_damage_targets
from runtime_metadata import LiveTeamProfileReader, ROLE_ID_RE


def decode_component(raw, entity_id, module_base, module_size, templates):
    if raw is None or len(raw) != 0x19c:
        return None
    vtable = struct.unpack_from('<Q', raw)[0]
    class_pointer = struct.unpack_from('<Q', raw, 0x10)[0]
    observed_id = struct.unpack_from('<Q', raw, 0x58)[0]
    template_id = struct.unpack_from('<I', raw, 0x198)[0]
    boss_type = raw[0x137]
    if (observed_id != entity_id or not class_pointer or
            not module_base <= vtable < module_base+module_size or
            str(template_id) not in templates or boss_type not in (0, 1, 2, 3)):
        return None
    return entity_id, template_id, boss_type, class_pointer, vtable


class PassiveEntityMetadataReader:
    def __init__(self, pid, profile):
        self.base, self.size, module_path = proc_inspect.find_module(
            pid, profile['game_module']
        )
        self.table = dict(profile['object_table'])
        self.templates = json.loads(Path(__file__).with_name('monster_metadata.json').read_text(encoding='utf-8'))['templates']
        try:
            validated_damage_targets = load_validated_reserved_damage_targets(
                module_path
            )
        except Exception:
            validated_damage_targets = {}
        self.templates.update(validated_damage_targets)
        self.handle = proc_inspect.kernel32.OpenProcess(
            proc_inspect.PROCESS_QUERY_INFORMATION | proc_inspect.PROCESS_VM_READ, False, pid)
        if not self.handle:
            raise OSError('Could not open read-only entity metadata reader')
        self.counters = Counter()
        self.counters['validated_reserved_damage_targets'] = len(
            validated_damage_targets
        )
        self.pending = {}
        self.resolved = set()
        self.components = {}
        self.signatures = {}
        self.checked_at = {}
        self.ready = []
        self.iterator = None
        self.retry_at = 0.0
        self.next_poll_at = 0.0
        self.scan_targets = set()
        self.unresolved_retry_at = {}

    def validated_damage_target_metadata(self, template_id):
        try:
            metadata = self.templates.get(str(int(template_id or 0)), {})
        except (TypeError, ValueError, OverflowError):
            return {}
        if not isinstance(metadata, dict) or metadata.get(
            'damage_target_validated'
        ) is not True:
            return {}
        return {
            key: metadata[key]
            for key in (
                'damage_target_validated', 'localization_id', 'level', 'name'
            )
            if key in metadata
        }

    def request(self, entity_id, timestamp):
        if (isinstance(entity_id, bool) or not isinstance(entity_id, int) or
                not 0 < entity_id < 2**64):
            return
        if entity_id in self.resolved:
            now = time.monotonic()
            if now-self.checked_at.get(entity_id, 0) < 1:
                return
            self.checked_at[entity_id] = now
            component = self.components[entity_id]
            first = decode_component(self._read(component, 0x19c), entity_id, self.base, self.size, self.templates)
            second = decode_component(self._read(component, 0x19c), entity_id, self.base, self.size, self.templates) if first else None
            if first is not None and first == second:
                if first != self.signatures[entity_id]:
                    self.signatures[entity_id] = first
                    self.ready.append(self._record(entity_id, component, first, int(timestamp)))
                    self.counters['metadata_template_refreshes'] += 1
                return
            self.resolved.discard(entity_id)
            self.components.pop(entity_id, None)
            self.signatures.pop(entity_id, None)
        if entity_id not in self.pending and len(self.pending) >= 256:
            self.counters['metadata_pending_limit'] += 1
            return
        if time.monotonic() < getattr(self, 'unresolved_retry_at', {}).get(entity_id, 0):
            return
        self.pending.setdefault(entity_id, (int(timestamp), time.monotonic()))

    def _record(self, entity, component, signature, timestamp):
        record = {'function': 'CommonComponent_TargetIdLookup',
                  'entity_id': entity, 'template_id': signature[1],
                  'boss_type': signature[2], 'component': component,
                  'filetime_100ns': timestamp, 'sequence': -1,
                  'target_id_lookup': True, 'boss_source': 'npcap_read_only_initialization'}
        record.update(self.validated_damage_target_metadata(signature[1]))
        return record

    def _read(self, address, length):
        value = proc_inspect.read_region(self.handle, address, length)
        return value if value is not None and len(value) == length else None

    def _objects(self):
        count_raw = self._read(self.base + int(self.table['count_rva']), 4)
        chunks_raw = self._read(self.base + int(self.table['chunks_rva']), 8)
        if count_raw is None or chunks_raw is None:
            return
        count = struct.unpack('<I', count_raw)[0]
        chunks = struct.unpack('<Q', chunks_raw)[0]
        if not chunks or not 0 < count <= 5_000_000:
            return
        # Newly spawned targets are near the table tail; still cover every slot.
        for chunk_index in range((count + 65535)//65536 - 1, -1, -1):
            raw = self._read(chunks + chunk_index*8, 8)
            if raw is None:
                continue
            chunk = struct.unpack('<Q', raw)[0]
            if not chunk:
                continue
            slots = min(65536, count-chunk_index*65536)
            for first in range(((slots - 1)//512)*512, -1, -512):
                block = self._read(chunk+first*24, min(512, slots-first)*24)
                if block is None:
                    yield 0
                    continue
                for offset in range(len(block) - 24, -1, -24):
                    yield struct.unpack_from('<Q', block, offset+8)[0]

    def poll(self, *, budget_seconds=0.005, max_objects=2048):
        result, self.ready = self.ready, []
        now = time.monotonic()
        for entity, (_timestamp, requested) in list(self.pending.items()):
            if now-requested > 30:
                self.pending.pop(entity, None)
                self.counters['metadata_timeouts'] += 1
        if not self.pending or now < self.retry_at or now < getattr(self, 'next_poll_at', 0):
            return result
        # Do not pay one metadata slice for every captured packet. Leave the
        # receive loop time to drain bursts before buffers overflow.
        self.next_poll_at = now + 0.01
        if self.iterator is None:
            self.iterator = self._objects()
            self.scan_targets = set(self.pending)
            self.counters['metadata_scans'] += 1
        deadline = time.perf_counter() + budget_seconds
        for _ in range(max_objects):
            try:
                component = next(self.iterator)
            except StopIteration:
                self.iterator = None
                for entity in self.scan_targets.intersection(self.pending):
                    self.pending.pop(entity, None)
                    self.unresolved_retry_at[entity] = time.monotonic() + 30
                self.unresolved_retry_at = {key: value for key, value in self.unresolved_retry_at.items()
                                            if value > time.monotonic()}
                self.retry_at = time.monotonic()+2
                break
            if component:
                raw = self._read(component, 0x19c)
                self.counters['metadata_objects_read'] += 1
                if raw is not None:
                    entity = struct.unpack_from('<Q', raw, 0x58)[0]
                    if entity in self.pending:
                        first = decode_component(raw, entity, self.base, self.size, self.templates)
                        second = decode_component(self._read(component, 0x19c), entity, self.base, self.size, self.templates) if first else None
                        if first is not None and first == second:
                            timestamp, _requested = self.pending.pop(entity)
                            self.resolved.add(entity)
                            self.components[entity] = component
                            self.signatures[entity] = first
                            self.checked_at[entity] = time.monotonic()
                            result.append(self._record(entity, component, first, timestamp))
                            self.counters['metadata_resolved'] += 1
            if time.perf_counter() >= deadline:
                break
        return result

    def reset(self):
        self.pending.clear()
        self.resolved.clear()
        self.components.clear()
        self.signatures.clear()
        self.checked_at.clear()
        self.ready.clear()
        self.iterator = None
        self.retry_at = 0.0
        self.next_poll_at = 0.0
        self.scan_targets.clear()
        self.unresolved_retry_at.clear()

    def close(self):
        if self.handle:
            proc_inspect.kernel32.CloseHandle(self.handle)
            self.handle = None


class PassiveTeamProfilePoller:
    """Poll token-bound live ``power`` values off the packet receive loop."""

    def __init__(
        self,
        pid,
        *,
        module_name="C7-Win64-Shipping.exe",
        interval_seconds=1.0,
        token_ttl_seconds=90.0,
        max_tokens=64,
        startup_profile_callback=None,
    ):
        self.pid = int(pid)
        self.module_name = str(module_name)
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.token_ttl_seconds = max(
            self.interval_seconds * 2, float(token_ttl_seconds)
        )
        self.max_tokens = max(1, int(max_tokens))
        self.startup_profile_callback = startup_profile_callback
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._tokens = {}
        self._ready = []
        self._emitted = {}
        self._refresh_requested = False
        self._counters = Counter()
        self._error = ""
        self._startup_token = ""
        self._startup_actor_id = 0
        self._startup_profile_published = False
        self._local_role_generation = 0
        self._roster_signature = None
        self._thread = threading.Thread(
            target=self._run,
            name="NpcapLiveTeamProfiles",
            daemon=True,
        )

    def _refresh_local_role(self, reader):
        """Notice an in-process account/character switch.

        The game can replace ``MainPlayer`` while retaining both its process
        and the passive capture connection.  Treating the local role as a
        startup-only value lets the previous token be republished forever.
        Re-read the log-proven local role on the profile cadence and arm one
        fresh authoritative callback for every distinct token/actor pair.
        """

        local_role_reader = getattr(reader, "current_local_role", None)
        local_role = (
            local_role_reader() if callable(local_role_reader) else None
        )
        if not isinstance(local_role, (tuple, list)) or len(local_role) < 2:
            return False
        token = str(local_role[0]).strip()
        try:
            actor_id = int(local_role[1])
        except (TypeError, ValueError, OverflowError):
            return False
        if not ROLE_ID_RE.fullmatch(token) or actor_id <= 0:
            return False
        if token == self._startup_token and actor_id == self._startup_actor_id:
            return False

        previous_role = (self._startup_token, self._startup_actor_id)
        self._startup_token = token
        self._startup_actor_id = actor_id
        self._startup_profile_published = False
        self._local_role_generation += 1
        with self._lock:
            self._counters["local_role_changes"] += int(
                bool(previous_role[0] or previous_role[1])
            )
        self.request((token,))
        return True

    def start(self):
        self._thread.start()
        return self

    def request(self, tokens):
        now = time.monotonic()
        cleaned = {
            str(token).strip()
            for token in tokens
            if ROLE_ID_RE.fullmatch(str(token).strip())
        }
        if not cleaned:
            return
        with self._lock:
            added = cleaned - set(self._tokens)
            for token in cleaned:
                self._tokens[token] = now
            if len(self._tokens) > self.max_tokens:
                oldest = sorted(self._tokens, key=self._tokens.get)
                for token in oldest[: len(self._tokens) - self.max_tokens]:
                    self._tokens.pop(token, None)
                    self._emitted.pop(token, None)
            added.intersection_update(self._tokens)
            if added:
                self._refresh_requested = True
        if added:
            self._wake.set()

    def replace(self, tokens):
        """Replace the requested set with the parser's current live roster."""

        now = time.monotonic()
        cleaned = {
            str(token).strip()
            for token in tokens
            if ROLE_ID_RE.fullmatch(str(token).strip())
        }
        if len(cleaned) > self.max_tokens:
            cleaned = set(sorted(cleaned)[: self.max_tokens])
        with self._lock:
            previous = set(self._tokens)
            removed = previous - cleaned
            added = cleaned - previous
            self._tokens = {token: now for token in cleaned}
            for token in removed:
                self._emitted.pop(token, None)
            if added:
                self._refresh_requested = True
        if added:
            self._wake.set()

    def _take_refresh_request(self):
        with self._lock:
            requested = self._refresh_requested
            self._refresh_requested = False
        return requested

    def _active_tokens(self):
        now = time.monotonic()
        with self._lock:
            expired = [
                token
                for token, observed_at in self._tokens.items()
                if now - observed_at > self.token_ttl_seconds
            ]
            for token in expired:
                self._tokens.pop(token, None)
                self._emitted.pop(token, None)
            return set(self._tokens)

    @staticmethod
    def _record(profile):
        unix_ns = time.time_ns()
        observed_at = int(profile.get('client_rating_observed_filetime', 0) or 0)
        if observed_at > 116444736000000000:
            unix_ns = (observed_at - 116444736000000000) * 100
        return {
            "function": "LuaTeamProfileSnapshot",
            "method": "NpcapLiveTeamProfile",
            "capture_source": "npcap_read_only_team_profile",
            "profile_source": (
                "live_lua_bound_rating" if profile.get('client_rating_binding')
                else "live_lua_power" if 'extraordinary_rating' in profile
                else 'live_lua_rating_unverified'
            ),
            "filetime_100ns": unix_ns // 100 + 116444736000000000,
            "capture_timestamp_ns": unix_ns,
            "sequence": -1,
            **profile,
        }

    def _publish(self, profiles):
        records = []
        startup_record = None
        for profile in profiles:
            token = str(profile.get("user_token", "")).strip()
            if not token:
                continue
            signature = tuple(
                profile.get(key)
                for key in (
                    "name",
                    "profession_id",
                    "level",
                    "role_number",
                    "extraordinary_rating",
                )
            )
            unchanged = self._emitted.get(token) == signature
            needs_startup_callback = bool(
                not self._startup_profile_published
                and token == self._startup_token
                and self._startup_actor_id > 0
            )
            if unchanged and not needs_startup_callback:
                continue
            self._emitted[token] = signature
            record = self._record(profile)
            records.append(record)
            if (
                not self._startup_profile_published
                and token == self._startup_token
                and self._startup_actor_id > 0
            ):
                startup_record = {
                    **record,
                    "entity_id": self._startup_actor_id,
                    "local_role_confirmed": True,
                    "local_role_generation": self._local_role_generation,
                }
                self._startup_profile_published = True
        if not records:
            return
        with self._lock:
            self._ready.extend(records)
            self._counters["live_team_profile_updates"] += len(records)
        if startup_record is not None and callable(self.startup_profile_callback):
            try:
                self.startup_profile_callback(startup_record)
            except Exception:
                with self._lock:
                    self._counters["live_team_profile_callback_errors"] += 1

    def _run(self):
        reader = None
        try:
            reader = LiveTeamProfileReader(
                self.pid,
                module_name=self.module_name,
                stop_event=self._stop,
            )
            self._refresh_local_role(reader)
            next_poll = 0.0
            next_roster_poll = 0.0
            next_local_role_check = 0.0
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_local_role_check:
                    self._refresh_local_role(reader)
                    next_local_role_check = (
                        time.monotonic() + self.interval_seconds
                    )
                if now >= next_roster_poll:
                    roster_reader = getattr(reader, "current_dungeon_roster", None)
                    roster = roster_reader() if callable(roster_reader) else None
                    if roster is not None and roster == roster_reader():
                        signature = (
                            roster["dungeon_id"], roster["group_id"],
                            roster["local_user_token"],
                            tuple(sorted(
                                (member["user_token"], member["name"],
                                 member.get("extraordinary_rating"))
                                for member in roster["members"]
                            )),
                        )
                        if signature != self._roster_signature:
                            self._roster_signature = signature
                            self.request(
                                member["user_token"]
                                for member in roster["members"]
                                if not member["is_ai"]
                            )
                            unix_ns = time.time_ns()
                            with self._lock:
                                self._ready.append({
                                    "method": "ReadOnlyCurrentDungeonRoster",
                                    "function": "LuaGroupSystemCurrentRoster",
                                    "filetime_100ns": (
                                        unix_ns // 100 + 116444736000000000
                                    ),
                                    "capture_timestamp_ns": unix_ns,
                                    **roster,
                                })
                                self._counters["live_dungeon_roster_updates"] += 1
                    next_roster_poll = time.monotonic() + max(2.0, self.interval_seconds)
                refresh_requested = self._take_refresh_request()
                tokens = self._active_tokens()
                if tokens and (refresh_requested or now >= next_poll):
                    with self._lock:
                        self._counters["live_team_profile_polls"] += 1
                        if refresh_requested:
                            self._counters["live_team_profile_immediate_polls"] += 1
                    profiles = reader.snapshot(tokens)
                    self._publish(profiles)
                    next_poll = time.monotonic() + self.interval_seconds
                    continue
                wait_for = 1.0
                if tokens:
                    wait_for = max(0.05, min(1.0, next_poll - now))
                self._wake.wait(wait_for)
                self._wake.clear()
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"[:500]
                self._counters["live_team_profile_errors"] += 1
        finally:
            if reader is not None:
                reader.close()

    def poll(self):
        with self._lock:
            records, self._ready = self._ready, []
        return records

    def take_counters(self):
        with self._lock:
            values = Counter(self._counters)
            self._counters.clear()
        return values

    def take_error(self):
        with self._lock:
            value, self._error = self._error, ""
        return value

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(3.0)
