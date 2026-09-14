"""Read-only target initialization for entities already observed on the wire.

Damage, healing and timestamps still come from Npcap. This bounded reader
copies exact existing entity/template fields; it never calls game functions.
"""
from __future__ import annotations

import json
import struct
import time
from collections import Counter
from pathlib import Path

import proc_inspect


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
        self.base, self.size, _path = proc_inspect.find_module(pid, profile['game_module'])
        self.table = dict(profile['object_table'])
        self.templates = json.loads(Path(__file__).with_name('monster_metadata.json').read_text(encoding='utf-8'))['templates']
        self.handle = proc_inspect.kernel32.OpenProcess(
            proc_inspect.PROCESS_QUERY_INFORMATION | proc_inspect.PROCESS_VM_READ, False, pid)
        if not self.handle:
            raise OSError('Could not open read-only entity metadata reader')
        self.counters = Counter()
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

    @staticmethod
    def _record(entity, component, signature, timestamp):
        return {'function': 'CommonComponent_TargetIdLookup',
                'entity_id': entity, 'template_id': signature[1],
                'boss_type': signature[2], 'component': component,
                'filetime_100ns': timestamp, 'sequence': -1,
                'target_id_lookup': True, 'boss_source': 'npcap_read_only_initialization'}

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
        # Read small batches rather than walking the entire table in one poll.
        for chunk_index in range((count + 65535)//65536):
            raw = self._read(chunks + chunk_index*8, 8)
            if raw is None:
                continue
            chunk = struct.unpack('<Q', raw)[0]
            if not chunk:
                continue
            slots = min(65536, count-chunk_index*65536)
            for first in range(0, slots, 512):
                block = self._read(chunk+first*24, min(512, slots-first)*24)
                if block is None:
                    yield 0
                    continue
                for offset in range(0, len(block), 24):
                    yield struct.unpack_from('<Q', block, offset+8)[0]

    def poll(self, *, budget_seconds=0.002, max_objects=512):
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
