#!/usr/bin/env python3
"""Reusable lifecycle wrapper around the C7 inline damage capture hook."""

from __future__ import annotations

import ctypes
import struct
import time

from inline_capture import (
    BOSS_INIT_MAGIC,
    BOSS_INIT_PROLOGUE,
    BOSS_INIT_PROLOGUE_SIGNATURE,
    BOSS_INIT_RVA,
    BOSS_TEMPLATE_ID_OFFSET,
    BOSS_TYPE_BOSS_VALUE,
    BOSS_TYPE_ENTITY_ID_OFFSET,
    BOSS_TYPE_FIELD_OFFSET,
    BOSS_TYPE_MAGIC,
    BOSS_TYPE_PROLOGUE,
    BOSS_TYPE_PROLOGUE_SIGNATURE,
    BOSS_TYPE_RECORD_COUNT,
    BOSS_TYPE_RECORD_MASK,
    BOSS_TYPE_RECORD_SIZE,
    BOSS_TYPE_RECORDS_OFFSET,
    BOSS_TYPE_RING_SIZE,
    BOSS_TYPE_RVA,
    DAMAGE_RVA,
    MAGIC,
    MEM_COMMIT,
    MEM_RELEASE,
    MEM_RESERVE,
    NAME_CACHE_RVA,
    NAME_MAGIC,
    NAME_PROLOGUE,
    NAME_PROLOGUE_SIGNATURE,
    NAME_RECORD_COUNT,
    NAME_RECORD_MASK,
    NAME_RECORD_SIZE,
    NAME_RECORDS_OFFSET,
    NAME_RING_SIZE,
    PAGE_EXECUTE_READWRITE,
    PAGE_READWRITE,
    PROLOGUE,
    PROLOGUE_SIGNATURE,
    RECORD_COUNT,
    RECORD_MASK,
    RECORD_SIZE,
    RECORDS_OFFSET,
    RING_SIZE,
    build_absolute_patch,
    build_boss_init_stub,
    build_boss_type_stub,
    build_name_stub,
    build_patch,
    build_stub,
    parse_boss_type_record,
    parse_name_record,
    parse_record,
    process_alive,
    resume_threads,
    suspend_process,
    write_code,
    write_memory,
)
from proc_inspect import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_module,
    find_pid,
    kernel32,
    read_region,
    winerror,
)


PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020

# Recovered from FWeakObjectPtr::Get in the current game build.  The damage
# manager keeps weak references at +0xC0 to the data-cache manager and +0xD0
# to the entity/actor manager.  The latter's +0x128 field is the local
# controlled entity ID used by the game's own damage routing comparisons.
GOBJECT_CHUNKS_RVA = 0x0EC0A080
GOBJECT_COUNT_RVA = 0x0EC0A094
DAMAGE_DATA_CACHE_MANAGER_WEAK_OFFSET = 0xC0
DAMAGE_ENTITY_MANAGER_WEAK_OFFSET = 0xD0
LOCAL_ENTITY_ID_OFFSET = 0x128
FILETIME_UNIX_EPOCH_100NS = 116_444_736_000_000_000
LOW_COMBAT_ENTITY_ID_MIN = 1_000_000_000_000
LOW_COMBAT_ENTITY_ID_MAX = 7_999_999_999_999
ENTITY_ID_MIN = 10_000_000_000_000
ENTITY_ID_MAX = 999_999_999_999_999
BOSS_COMPONENT_READ_SIZE = BOSS_TEMPLATE_ID_OFFSET + 4
NAME_CACHE_ENTRY_SIZE = 0x20
NAME_CACHE_READ_BATCH_ENTRIES = 4096
SKILL_CACHE_ENTRY_SIZE = 0x1D8
SKILL_CACHE_READ_BATCH_ENTRIES = 512
UOBJECT_CLASS_PRIVATE_OFFSET = 0x10
TARGET_BOSS_LOOKUP_RETRY_SECONDS = 0.2
TARGET_BOSS_LOOKUP_MAX_WAIT_SECONDS = 10.0
TARGET_BOSS_LOOKUP_MAX_PENDING = 256
TARGET_BOSS_LOOKUP_MAX_OBSERVED_COMPONENTS = 4096


class DamageHook:
    def __init__(
        self,
        *,
        pid: int | None = None,
        process_name: str = "C7-Win64-Shipping.exe",
        module_name: str = "C7-Win64-Shipping.exe",
        rva: int = DAMAGE_RVA,
        capture_names: bool = True,
        capture_boss_types: bool = True,
        target_boss_lookup_enabled: bool = False,
    ):
        self.requested_pid = pid
        self.process_name = process_name
        self.module_name = module_name
        self.rva = rva
        self.capture_names = bool(capture_names)
        self.capture_boss_types = bool(capture_boss_types)
        self.pid = 0
        self.base = 0
        self.target = 0
        self.process = 0
        self.ring = 0
        self.stub = 0
        self.installed = False
        self.adopted = False
        self.next_sequence = 0
        self.module_path = ""
        self.local_player_id = 0
        self._last_local_player_refresh = 0.0
        self.name_target = 0
        self.name_ring = 0
        self.name_stub = 0
        self.name_installed = False
        self.name_adopted = False
        self.name_next_sequence = 0
        self.name_manager = 0
        self.entity_names: dict[int, str] = {}
        self.skill_names: dict[int, str] = {}
        self._last_name_cache_scan = 0.0
        self._last_skill_cache_scan = 0.0
        self.boss_type_target = 0
        self.boss_type_ring = 0
        self.boss_type_stub = 0
        self.boss_type_global_address = 0
        self.boss_type_installed = False
        self.boss_type_adopted = False
        self.boss_type_next_sequence = 0
        self.boss_init_target = 0
        self.boss_init_ring = 0
        self.boss_init_stub = 0
        self.boss_init_installed = False
        self.boss_init_adopted = False
        self.boss_init_next_sequence = 0
        self.pending_boss_components: dict[int, tuple[dict, float]] = {}
        self.pending_existing_boss_targets: set[int] = set()
        self.existing_boss_scan_attempted: set[int] = set()
        self.existing_boss_component_cache: dict[int, dict] = {}
        # The fallback walks the complete Unreal object table and is only for a
        # Boss that already existed when the inline hooks were installed.  A
        # previous implementation repeated that full walk for every newly hit
        # trash entity because an ordinary entity can never enter the Boss
        # cache.  In a dense pull that turns one compatibility fallback into an
        # unbounded stream of millions of cross-process reads.
        self.existing_boss_full_scan_complete = False
        # Optional compatibility route for clients where CommonComponent is
        # observed before its template ID is initialized.  It is fail-closed:
        # this layer only recovers an exact target/component/template tuple;
        # NetworkPacketParser still requires that template in the shipped Boss
        # catalog before any damage is accepted.
        self.target_boss_lookup_enabled = bool(target_boss_lookup_enabled)
        self.pending_target_boss_lookups: dict[int, float] = {}
        self.target_boss_lookup_attempted: set[int] = set()
        self.target_boss_lookup_emitted: set[int] = set()
        self.observed_common_components: dict[int, dict] = {}
        self.common_components_by_entity: dict[int, set[int]] = {}
        self.trusted_common_component_classes: set[int] = set()
        self.target_boss_object_index_complete = False
        self.target_boss_object_scan_count = 0
        self._last_target_boss_lookup_poll = 0.0
        # Export only anonymous stage counters to the diagnostic tool. These
        # values never contain addresses, entity IDs, or raw payloads.
        self.diagnostic_counters: dict[str, int] = {
            "damage_ring_header_reads": 0,
            "damage_ring_header_failures": 0,
            "damage_ring_records_polled": 0,
            "damage_ring_parse_failures": 0,
            "damage_ring_overruns": 0,
            "boss_ring_header_reads": 0,
            "boss_ring_header_failures": 0,
            "boss_ring_records_polled": 0,
            "boss_ring_parse_failures": 0,
            "boss_ring_overruns": 0,
            "target_lookup_candidates": 0,
            "target_lookup_evictions": 0,
            "target_lookup_resolved": 0,
            "target_lookup_timeouts": 0,
            "target_lookup_component_observations": 0,
            "target_lookup_class_reads": 0,
            "target_lookup_class_matches": 0,
            "target_lookup_component_reads": 0,
            "target_lookup_component_matches": 0,
            "target_lookup_component_read_failures": 0,
            "target_lookup_component_class_rejections": 0,
            "target_lookup_component_entity_mismatches": 0,
            "target_lookup_component_template_zero": 0,
            "target_lookup_observed_target_matches": 0,
            "target_lookup_observed_target_template_zero": 0,
            "target_lookup_observed_target_template_nonzero": 0,
            "target_lookup_object_scans": 0,
            "target_lookup_object_candidates": 0,
            "target_lookup_object_table_reads": 0,
            "target_lookup_object_table_failures": 0,
            "target_lookup_object_slots_scanned": 0,
            "target_lookup_object_pointers": 0,
            "target_lookup_object_class_reads": 0,
            "target_lookup_object_class_read_failures": 0,
            "target_lookup_object_class_candidates": 0,
            "target_lookup_object_component_read_failures": 0,
            "target_lookup_object_entity_candidates": 0,
            "target_lookup_object_exact_entity_matches": 0,
            "target_lookup_object_exact_template_zero": 0,
            "target_lookup_object_exact_template_nonzero": 0,
            "existing_boss_scan_attempts": 0,
            "existing_boss_scan_matches": 0,
            "late_boss_component_resolved": 0,
        }

    def _diagnostic_add(self, key: str, amount: int = 1) -> None:
        try:
            value = int(amount)
        except (TypeError, ValueError, OverflowError):
            return
        self.diagnostic_counters[key] = max(
            0, int(self.diagnostic_counters.get(key, 0) or 0) + value
        )

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return anonymous hook/lookup state for troubleshooting reports."""

        return {
            **{
                key: max(0, int(value or 0))
                for key, value in self.diagnostic_counters.items()
            },
            "damage_hook_installed": bool(self.installed),
            "damage_hook_adopted": bool(self.adopted),
            "name_hook_installed": bool(self.name_installed),
            "boss_type_hook_installed": bool(self.boss_type_installed),
            "boss_init_hook_installed": bool(self.boss_init_installed),
            "target_boss_lookup_enabled": bool(self.target_boss_lookup_enabled),
            "target_lookup_pending": len(self.pending_target_boss_lookups),
            "target_lookup_attempted": len(self.target_boss_lookup_attempted),
            "target_lookup_emitted": len(self.target_boss_lookup_emitted),
            "target_lookup_observed_components": len(
                self.observed_common_components
            ),
            "target_lookup_trusted_classes": len(
                self.trusted_common_component_classes
            ),
            "target_lookup_object_index_complete": bool(
                self.target_boss_object_index_complete
            ),
            "target_lookup_object_scan_count": max(
                0, int(self.target_boss_object_scan_count or 0)
            ),
            "existing_boss_scan_attempted": len(
                self.existing_boss_scan_attempted
            ),
            "existing_boss_full_scan_complete": bool(
                self.existing_boss_full_scan_complete
            ),
        }

    def _read_exact(self, address: int, size: int) -> bytes:
        data = read_region(self.process, address, size)
        if data is None or len(data) != size:
            raise RuntimeError(f"could not read target memory at 0x{address:x}")
        return data

    def _u32(self, address: int) -> int:
        return struct.unpack("<I", self._read_exact(address, 4))[0]

    def _u64(self, address: int) -> int:
        return struct.unpack("<Q", self._read_exact(address, 8))[0]

    def _resolve_weak_object(self, owner: int, offset: int) -> int:
        weak = self._read_exact(owner + offset, 8)
        object_index, object_serial = struct.unpack("<ii", weak)
        object_count = self._u32(self.base + GOBJECT_COUNT_RVA)
        if object_serial <= 0 or object_index < 0 or object_index >= object_count:
            return 0
        chunks = self._u64(self.base + GOBJECT_CHUNKS_RVA)
        chunk = self._u64(chunks + (object_index >> 16) * 8)
        if not chunk:
            return 0
        item = chunk + (object_index & 0xFFFF) * 24
        if self._u32(item + 0x10) != object_serial:
            return 0
        manager = self._u64(item + 8)
        return manager

    def resolve_local_player_id(self, damage_manager: int) -> int:
        """Resolve the same local entity ID the native damage handler uses."""
        manager = self._resolve_weak_object(
            damage_manager, DAMAGE_ENTITY_MANAGER_WEAK_OFFSET
        )
        return self._u64(manager + LOCAL_ENTITY_ID_OFFSET) if manager else 0

    def resolve_data_cache_manager(self, damage_manager: int) -> int:
        """Resolve the manager that owns the game's entity-name TMap."""
        return self._resolve_weak_object(
            damage_manager, DAMAGE_DATA_CACHE_MANAGER_WEAK_OFFSET
        )

    def _refresh_local_player_id(
        self, damage_manager: int, *, now: float | None = None
    ) -> int:
        now = time.monotonic() if now is None else float(now)
        if (
            self.local_player_id
            and now - self._last_local_player_refresh < 0.1
        ):
            return self.local_player_id
        try:
            resolved = self.resolve_local_player_id(damage_manager)
            if self._plausible_entity_id(resolved):
                self.local_player_id = resolved
        except (OSError, RuntimeError, struct.error):
            if not self.local_player_id:
                self.local_player_id = 0
        self._last_local_player_refresh = now
        return self.local_player_id

    @property
    def alive(self) -> bool:
        return bool(self.process) and process_alive(self.process)

    def set_target_boss_lookup_enabled(self, enabled: bool) -> None:
        """Enable the exact target-to-template compatibility route at runtime."""
        enabled = bool(enabled)
        if enabled == self.target_boss_lookup_enabled:
            return
        self.target_boss_lookup_enabled = enabled
        self.pending_target_boss_lookups.clear()
        self.target_boss_lookup_attempted.clear()
        self.target_boss_lookup_emitted.clear()
        self.trusted_common_component_classes.clear()
        self.target_boss_object_index_complete = False
        self.target_boss_object_scan_count = 0
        self._last_target_boss_lookup_poll = 0.0

    def _adopt_existing(self, patch: bytes) -> bool:
        if len(patch) != len(PROLOGUE) or patch[:6] != b"\xff\x25\0\0\0\0":
            return False
        stub = struct.unpack_from("<Q", patch, 6)[0]
        stub_head = read_region(self.process, stub, 32)
        if not stub_head or not stub_head.startswith(b"\x9c\x50\x53\x52\x41\x52\x41\x53\x49\xba"):
            return False
        ring = struct.unpack_from("<Q", stub_head, 10)[0]
        header = read_region(self.process, ring, 40)
        if not header or header[:8] != MAGIC:
            return False
        _, write_index, capacity, record_size, target = struct.unpack("<8sQQQQ", header)
        if capacity != RECORD_COUNT or record_size != RECORD_SIZE or target != self.target:
            return False
        self.stub = stub
        self.ring = ring
        self.next_sequence = write_index
        self.installed = True
        self.adopted = True
        return True

    def _adopt_existing_name_hook(self, patch: bytes) -> bool:
        if len(patch) != len(NAME_PROLOGUE) or patch[:6] != b"\xff\x25\0\0\0\0":
            return False
        stub = struct.unpack_from("<Q", patch, 6)[0]
        stub_head = read_region(self.process, stub, 40)
        prefix = b"\x9c\x50\x53\x52\x41\x50\x41\x52\x41\x53\x49\xba"
        if not stub_head or not stub_head.startswith(prefix):
            return False
        ring = struct.unpack_from("<Q", stub_head, len(prefix))[0]
        header = read_region(self.process, ring, 40)
        if not header or header[:8] != NAME_MAGIC:
            return False
        _, write_index, capacity, record_size, target = struct.unpack("<8sQQQQ", header)
        if (
            capacity != NAME_RECORD_COUNT
            or record_size != NAME_RECORD_SIZE
            or target != self.name_target
        ):
            return False
        expected_stub = build_name_stub(
            ring, self.name_target + len(NAME_PROLOGUE)
        )
        if read_region(self.process, stub, len(expected_stub)) != expected_stub:
            return False
        self.name_stub = stub
        self.name_ring = ring
        self.name_next_sequence = write_index
        self.name_installed = True
        self.name_adopted = True
        return True

    def _adopt_existing_boss_type_hook(self, patch: bytes) -> bool:
        if (
            len(patch) != len(BOSS_TYPE_PROLOGUE)
            or patch[:6] != b"\xff\x25\0\0\0\0"
        ):
            return False
        stub = struct.unpack_from("<Q", patch, 6)[0]
        stub_head = read_region(self.process, stub, 40)
        prefix = b"\x9c\x50\x53\x52\x41\x52\x41\x53\x49\xba"
        if not stub_head or not stub_head.startswith(prefix):
            return False
        ring = struct.unpack_from("<Q", stub_head, len(prefix))[0]
        header = read_region(self.process, ring, 40)
        if not header or header[:8] != BOSS_TYPE_MAGIC:
            return False
        _, write_index, capacity, record_size, target = struct.unpack(
            "<8sQQQQ", header
        )
        if (
            capacity != BOSS_TYPE_RECORD_COUNT
            or record_size != BOSS_TYPE_RECORD_SIZE
            or target != self.boss_type_target
        ):
            return False
        expected_stub = build_boss_type_stub(
            ring,
            self.boss_type_target + len(BOSS_TYPE_PROLOGUE),
            self.boss_type_global_address,
        )
        if read_region(self.process, stub, len(expected_stub)) != expected_stub:
            return False
        self.boss_type_stub = stub
        self.boss_type_ring = ring
        self.boss_type_next_sequence = write_index
        self.boss_type_installed = True
        self.boss_type_adopted = True
        return True

    def _install_boss_type_hook(self, module_size: int) -> None:
        self.boss_type_target = self.base + BOSS_TYPE_RVA
        if BOSS_TYPE_RVA + len(BOSS_TYPE_PROLOGUE_SIGNATURE) > module_size:
            raise RuntimeError("boss-type target RVA is outside the live module")
        displacement = struct.unpack_from("<i", BOSS_TYPE_PROLOGUE, 13)[0]
        self.boss_type_global_address = (
            self.boss_type_target + len(BOSS_TYPE_PROLOGUE) + displacement
        )
        actual = read_region(
            self.process,
            self.boss_type_target,
            len(BOSS_TYPE_PROLOGUE_SIGNATURE),
        )
        if actual != BOSS_TYPE_PROLOGUE_SIGNATURE:
            if actual and self._adopt_existing_boss_type_hook(
                actual[: len(BOSS_TYPE_PROLOGUE)]
            ):
                return
            got = actual.hex(" ") if actual else "unreadable"
            raise RuntimeError(
                f"boss-type version/signature mismatch at RVA 0x{BOSS_TYPE_RVA:x}\n"
                f"expected: {BOSS_TYPE_PROLOGUE_SIGNATURE.hex(' ')}\nactual:   {got}"
            )

        self.boss_type_ring = int(
            kernel32.VirtualAllocEx(
                self.process,
                None,
                BOSS_TYPE_RING_SIZE,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_READWRITE,
            )
            or 0
        )
        if not self.boss_type_ring:
            raise winerror("VirtualAllocEx(boss-type ring)")
        self.boss_type_stub = int(
            kernel32.VirtualAllocEx(
                self.process,
                None,
                0x1000,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_EXECUTE_READWRITE,
            )
            or 0
        )
        if not self.boss_type_stub:
            raise winerror("VirtualAllocEx(boss-type stub)")
        header = struct.pack(
            "<8sQQQQ",
            BOSS_TYPE_MAGIC,
            0,
            BOSS_TYPE_RECORD_COUNT,
            BOSS_TYPE_RECORD_SIZE,
            self.boss_type_target,
        )
        write_memory(self.process, self.boss_type_ring, header)
        stub_code = build_boss_type_stub(
            self.boss_type_ring,
            self.boss_type_target + len(BOSS_TYPE_PROLOGUE),
            self.boss_type_global_address,
        )
        write_memory(self.process, self.boss_type_stub, stub_code)
        kernel32.FlushInstructionCache(
            self.process, ctypes.c_void_p(self.boss_type_stub), len(stub_code)
        )
        patch = build_absolute_patch(
            self.boss_type_stub, len(BOSS_TYPE_PROLOGUE)
        )
        suspended = suspend_process(self.pid)
        try:
            write_code(self.process, self.boss_type_target, patch)
            self.boss_type_installed = True
        finally:
            resume_threads(suspended)
        if read_region(self.process, self.boss_type_target, len(patch)) != patch:
            raise RuntimeError("boss-type hook verification failed")
        self.boss_type_next_sequence = 0

    def _adopt_existing_boss_init_hook(self, patch: bytes) -> bool:
        if (
            len(patch) != len(BOSS_INIT_PROLOGUE)
            or patch[:6] != b"\xff\x25\0\0\0\0"
        ):
            return False
        stub = struct.unpack_from("<Q", patch, 6)[0]
        stub_head = read_region(self.process, stub, 40)
        prefix = b"\x9c\x50\x53\x52\x41\x52\x41\x53\x49\xba"
        if not stub_head or not stub_head.startswith(prefix):
            return False
        ring = struct.unpack_from("<Q", stub_head, len(prefix))[0]
        header = read_region(self.process, ring, 40)
        if not header or header[:8] != BOSS_INIT_MAGIC:
            return False
        _, write_index, capacity, record_size, target = struct.unpack(
            "<8sQQQQ", header
        )
        if (
            capacity != BOSS_TYPE_RECORD_COUNT
            or record_size != BOSS_TYPE_RECORD_SIZE
            or target != self.boss_init_target
        ):
            return False
        expected_stub = build_boss_init_stub(
            ring, self.boss_init_target + len(BOSS_INIT_PROLOGUE)
        )
        if read_region(self.process, stub, len(expected_stub)) != expected_stub:
            return False
        self.boss_init_stub = stub
        self.boss_init_ring = ring
        self.boss_init_next_sequence = write_index
        self.boss_init_installed = True
        self.boss_init_adopted = True
        return True

    def _install_boss_init_hook(self, module_size: int) -> None:
        self.boss_init_target = self.base + BOSS_INIT_RVA
        if BOSS_INIT_RVA + len(BOSS_INIT_PROLOGUE_SIGNATURE) > module_size:
            raise RuntimeError("boss-init target RVA is outside the live module")
        actual = read_region(
            self.process,
            self.boss_init_target,
            len(BOSS_INIT_PROLOGUE_SIGNATURE),
        )
        if actual != BOSS_INIT_PROLOGUE_SIGNATURE:
            if actual and self._adopt_existing_boss_init_hook(
                actual[: len(BOSS_INIT_PROLOGUE)]
            ):
                return
            got = actual.hex(" ") if actual else "unreadable"
            raise RuntimeError(
                f"boss-init version/signature mismatch at RVA 0x{BOSS_INIT_RVA:x}\n"
                f"expected: {BOSS_INIT_PROLOGUE_SIGNATURE.hex(' ')}\nactual:   {got}"
            )

        self.boss_init_ring = int(
            kernel32.VirtualAllocEx(
                self.process,
                None,
                BOSS_TYPE_RING_SIZE,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_READWRITE,
            )
            or 0
        )
        if not self.boss_init_ring:
            raise winerror("VirtualAllocEx(boss-init ring)")
        self.boss_init_stub = int(
            kernel32.VirtualAllocEx(
                self.process,
                None,
                0x1000,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_EXECUTE_READWRITE,
            )
            or 0
        )
        if not self.boss_init_stub:
            raise winerror("VirtualAllocEx(boss-init stub)")
        header = struct.pack(
            "<8sQQQQ",
            BOSS_INIT_MAGIC,
            0,
            BOSS_TYPE_RECORD_COUNT,
            BOSS_TYPE_RECORD_SIZE,
            self.boss_init_target,
        )
        write_memory(self.process, self.boss_init_ring, header)
        stub_code = build_boss_init_stub(
            self.boss_init_ring,
            self.boss_init_target + len(BOSS_INIT_PROLOGUE),
        )
        write_memory(self.process, self.boss_init_stub, stub_code)
        kernel32.FlushInstructionCache(
            self.process, ctypes.c_void_p(self.boss_init_stub), len(stub_code)
        )
        patch = build_absolute_patch(
            self.boss_init_stub, len(BOSS_INIT_PROLOGUE)
        )
        suspended = suspend_process(self.pid)
        try:
            write_code(self.process, self.boss_init_target, patch)
            self.boss_init_installed = True
        finally:
            resume_threads(suspended)
        if read_region(self.process, self.boss_init_target, len(patch)) != patch:
            raise RuntimeError("boss-init hook verification failed")
        self.boss_init_next_sequence = 0

    def _install_name_hook(self, module_size: int) -> None:
        self.name_target = self.base + NAME_CACHE_RVA
        if NAME_CACHE_RVA + len(NAME_PROLOGUE_SIGNATURE) > module_size:
            raise RuntimeError("entity-name target RVA is outside the live module")
        actual = read_region(
            self.process, self.name_target, len(NAME_PROLOGUE_SIGNATURE)
        )
        if actual != NAME_PROLOGUE_SIGNATURE:
            if actual and self._adopt_existing_name_hook(
                actual[: len(NAME_PROLOGUE)]
            ):
                return
            got = actual.hex(" ") if actual else "unreadable"
            raise RuntimeError(
                f"entity-name version/signature mismatch at RVA 0x{NAME_CACHE_RVA:x}\n"
                f"expected: {NAME_PROLOGUE_SIGNATURE.hex(' ')}\nactual:   {got}"
            )

        self.name_ring = int(
            kernel32.VirtualAllocEx(
                self.process,
                None,
                NAME_RING_SIZE,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_READWRITE,
            )
            or 0
        )
        if not self.name_ring:
            raise winerror("VirtualAllocEx(entity-name ring)")
        self.name_stub = int(
            kernel32.VirtualAllocEx(
                self.process,
                None,
                0x1000,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_EXECUTE_READWRITE,
            )
            or 0
        )
        if not self.name_stub:
            raise winerror("VirtualAllocEx(entity-name stub)")
        header = struct.pack(
            "<8sQQQQ",
            NAME_MAGIC,
            0,
            NAME_RECORD_COUNT,
            NAME_RECORD_SIZE,
            self.name_target,
        )
        write_memory(self.process, self.name_ring, header)
        stub_code = build_name_stub(
            self.name_ring, self.name_target + len(NAME_PROLOGUE)
        )
        write_memory(self.process, self.name_stub, stub_code)
        kernel32.FlushInstructionCache(
            self.process, ctypes.c_void_p(self.name_stub), len(stub_code)
        )
        patch = build_absolute_patch(self.name_stub, len(NAME_PROLOGUE))
        suspended = suspend_process(self.pid)
        try:
            write_code(self.process, self.name_target, patch)
            self.name_installed = True
        finally:
            resume_threads(suspended)
        if read_region(self.process, self.name_target, len(patch)) != patch:
            raise RuntimeError("entity-name hook verification failed")
        self.name_next_sequence = 0

    def install(self) -> "DamageHook":
        if self.installed:
            return self
        self.pid = self.requested_pid or find_pid(self.process_name)
        self.base, module_size, self.module_path = find_module(
            self.pid, self.module_name
        )
        self.target = self.base + self.rva
        if self.rva + len(PROLOGUE_SIGNATURE) > module_size:
            raise RuntimeError("damage target RVA is outside the live module")
        access = (
            PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE
        )
        self.process = int(kernel32.OpenProcess(access, False, self.pid) or 0)
        if not self.process:
            raise winerror("OpenProcess")

        actual = read_region(self.process, self.target, len(PROLOGUE_SIGNATURE))
        if actual != PROLOGUE_SIGNATURE:
            if actual and self._adopt_existing(actual[: len(PROLOGUE)]):
                try:
                    if self.capture_names:
                        self._install_name_hook(module_size)
                    if self.capture_boss_types:
                        self._install_boss_type_hook(module_size)
                        self._install_boss_init_hook(module_size)
                except Exception:
                    self.close()
                    raise
                return self
            got = actual.hex(" ") if actual else "unreadable"
            self.close()
            raise RuntimeError(
                f"game version/signature mismatch at RVA 0x{self.rva:x}\n"
                f"expected: {PROLOGUE_SIGNATURE.hex(' ')}\nactual:   {got}"
            )

        try:
            self.ring = int(
                kernel32.VirtualAllocEx(
                    self.process,
                    None,
                    RING_SIZE,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_READWRITE,
                )
                or 0
            )
            if not self.ring:
                raise winerror("VirtualAllocEx(ring)")
            self.stub = int(
                kernel32.VirtualAllocEx(
                    self.process,
                    None,
                    0x1000,
                    MEM_COMMIT | MEM_RESERVE,
                    PAGE_EXECUTE_READWRITE,
                )
                or 0
            )
            if not self.stub:
                raise winerror("VirtualAllocEx(stub)")
            header = struct.pack(
                "<8sQQQQ", MAGIC, 0, RECORD_COUNT, RECORD_SIZE, self.target
            )
            write_memory(self.process, self.ring, header)
            stub_code = build_stub(self.ring, self.target + len(PROLOGUE))
            write_memory(self.process, self.stub, stub_code)
            kernel32.FlushInstructionCache(
                self.process, ctypes.c_void_p(self.stub), len(stub_code)
            )

            suspended = suspend_process(self.pid)
            try:
                write_code(self.process, self.target, build_patch(self.stub))
                self.installed = True
            finally:
                resume_threads(suspended)
            patch = read_region(self.process, self.target, len(PROLOGUE))
            if patch != build_patch(self.stub):
                raise RuntimeError("inline hook verification failed")
            self.next_sequence = 0
            if self.capture_names:
                self._install_name_hook(module_size)
            if self.capture_boss_types:
                self._install_boss_type_hook(module_size)
                self._install_boss_init_hook(module_size)
            return self
        except Exception:
            self.close()
            raise

    def poll(self) -> list[dict]:
        if not self.installed or not self.alive:
            return []
        self._diagnostic_add("damage_ring_header_reads")
        header = read_region(self.process, self.ring, 0x20)
        if not header or header[:8] != MAGIC:
            self._diagnostic_add("damage_ring_header_failures")
            raise RuntimeError("damage ring buffer became unreadable or corrupt")
        write_index = struct.unpack_from("<Q", header, 8)[0]
        if write_index - self.next_sequence > RECORD_COUNT:
            self._diagnostic_add(
                "damage_ring_overruns",
                write_index - self.next_sequence - RECORD_COUNT,
            )
            self.next_sequence = write_index - RECORD_COUNT
        records: list[dict] = []
        while self.next_sequence < write_index:
            self._diagnostic_add("damage_ring_records_polled")
            slot = self.next_sequence & RECORD_MASK
            raw = read_region(
                self.process,
                self.ring + RECORDS_OFFSET + slot * RECORD_SIZE,
                RECORD_SIZE,
            )
            record = parse_record(raw or b"", self.next_sequence)
            if record is None:
                self._diagnostic_add("damage_ring_parse_failures")
                break
            damage_manager = int(record["damage_manager"])
            if not self.name_manager:
                try:
                    self.name_manager = self.resolve_data_cache_manager(
                        damage_manager
                    )
                except (OSError, RuntimeError, struct.error):
                    self.name_manager = 0
            self._refresh_local_player_id(damage_manager)
            if self.local_player_id:
                record["local_player_id"] = self.local_player_id
            target_id = int(record.get("target_id", 0) or 0)
            if (
                self._plausible_entity_id(target_id)
                and target_id != self.local_player_id
                and target_id not in self.existing_boss_scan_attempted
                and (
                    not self.existing_boss_full_scan_complete
                    or target_id in self.existing_boss_component_cache
                )
            ):
                self.pending_existing_boss_targets.add(target_id)
            if (
                self.target_boss_lookup_enabled
                and self._plausible_entity_id(target_id)
                and target_id != self.local_player_id
                and target_id not in self.target_boss_lookup_attempted
                and target_id not in self.target_boss_lookup_emitted
            ):
                if target_id not in self.pending_target_boss_lookups:
                    self.pending_target_boss_lookups[target_id] = time.monotonic()
                    self._diagnostic_add("target_lookup_candidates")
                while (
                    len(self.pending_target_boss_lookups)
                    > TARGET_BOSS_LOOKUP_MAX_PENDING
                ):
                    oldest = next(iter(self.pending_target_boss_lookups))
                    self.pending_target_boss_lookups.pop(oldest, None)
                    self.target_boss_lookup_attempted.add(oldest)
                    self._diagnostic_add("target_lookup_evictions")
            records.append(record)
            self.next_sequence += 1
        return records

    @staticmethod
    def _plausible_entity_id(value: int) -> bool:
        return bool(
            LOW_COMBAT_ENTITY_ID_MIN <= value <= LOW_COMBAT_ENTITY_ID_MAX
            or ENTITY_ID_MIN <= value <= ENTITY_ID_MAX
        )

    @staticmethod
    def _plausible_pointer(value: int) -> bool:
        return 0x10000 <= int(value or 0) <= 0x0000_7FFF_FFFF_FFFF

    def _remember_common_component_record(self, record: dict) -> None:
        """Index only pointers proven by a CommonComponent hook callback."""
        component = int(record.get("component", 0) or 0)
        if not self._plausible_pointer(component):
            return
        self._diagnostic_add("target_lookup_component_observations")
        previous = self.observed_common_components.pop(component, None)
        if previous is not None:
            previous_entity = int(previous.get("entity_id", 0) or 0)
            components = self.common_components_by_entity.get(previous_entity)
            if components is not None:
                components.discard(component)
                if not components:
                    self.common_components_by_entity.pop(previous_entity, None)
        saved = dict(record)
        self.observed_common_components[component] = saved
        entity_id = int(saved.get("entity_id", 0) or 0)
        if (
            entity_id in self.pending_target_boss_lookups
            and not saved.get("target_id_index")
        ):
            self._diagnostic_add("target_lookup_observed_target_matches")
            if int(saved.get("template_id", 0) or 0):
                self._diagnostic_add(
                    "target_lookup_observed_target_template_nonzero"
                )
            else:
                self._diagnostic_add(
                    "target_lookup_observed_target_template_zero"
                )
        if self._plausible_entity_id(entity_id):
            self.common_components_by_entity.setdefault(entity_id, set()).add(
                component
            )
        while (
            len(self.observed_common_components)
            > TARGET_BOSS_LOOKUP_MAX_OBSERVED_COMPONENTS
        ):
            stale_component = next(iter(self.observed_common_components))
            stale = self.observed_common_components.pop(stale_component)
            stale_entity = int(stale.get("entity_id", 0) or 0)
            components = self.common_components_by_entity.get(stale_entity)
            if components is not None:
                components.discard(stale_component)
                if not components:
                    self.common_components_by_entity.pop(stale_entity, None)

    def _remember_common_component_class(self, component: int) -> bool:
        self._diagnostic_add("target_lookup_class_reads")
        try:
            class_pointer = self._u64(
                component + UOBJECT_CLASS_PRIVATE_OFFSET
            )
        except (OSError, RuntimeError, struct.error):
            return False
        if not self._plausible_pointer(class_pointer):
            return False
        self.trusted_common_component_classes.add(class_pointer)
        self._diagnostic_add("target_lookup_class_matches")
        return True

    def _prime_common_component_classes(self) -> None:
        if self.trusted_common_component_classes:
            return
        for component in list(self.observed_common_components)[-64:]:
            self._remember_common_component_class(component)

    def _read_target_common_component(
        self, component: int, target_id: int
    ) -> dict | None:
        self._diagnostic_add("target_lookup_component_reads")
        raw = read_region(self.process, component, BOSS_COMPONENT_READ_SIZE)
        if not raw or len(raw) < BOSS_COMPONENT_READ_SIZE:
            self._diagnostic_add("target_lookup_component_read_failures")
            return None
        if component not in self.observed_common_components:
            class_pointer = struct.unpack_from(
                "<Q", raw, UOBJECT_CLASS_PRIVATE_OFFSET
            )[0]
            if class_pointer not in self.trusted_common_component_classes:
                self._diagnostic_add(
                    "target_lookup_component_class_rejections"
                )
                return None
        entity_id = struct.unpack_from(
            "<Q", raw, BOSS_TYPE_ENTITY_ID_OFFSET
        )[0]
        if entity_id != target_id:
            self._diagnostic_add("target_lookup_component_entity_mismatches")
            return None
        template_id = struct.unpack_from(
            "<I", raw, BOSS_TEMPLATE_ID_OFFSET
        )[0]
        if not template_id:
            self._diagnostic_add("target_lookup_component_template_zero")
            return None
        self._diagnostic_add("target_lookup_component_matches")
        return {
            "filetime_100ns": (
                time.time_ns() // 100 + FILETIME_UNIX_EPOCH_100NS
            ),
            "sequence": -1,
            "function": "CommonComponent_TargetIdLookup",
            "component": component,
            "entity_id": entity_id,
            "boss_type": int(raw[BOSS_TYPE_FIELD_OFFSET]),
            "template_id": template_id,
            "target_id_lookup": True,
            "boss_source": "target_id_common_component",
        }

    def _build_target_common_component_index_once(self) -> None:
        """Build one UClass-filtered component index per enable cycle."""
        if self.target_boss_object_index_complete:
            return
        self._prime_common_component_classes()
        if not self.trusted_common_component_classes:
            return
        self.target_boss_object_scan_count += 1
        self._diagnostic_add("target_lookup_object_scans")
        pending_targets = set(self.pending_target_boss_lookups)
        for component in self._iter_live_object_pointers(
            track_target_lookup=True
        ):
            self._diagnostic_add("target_lookup_object_class_reads")
            try:
                class_pointer = self._u64(
                    component + UOBJECT_CLASS_PRIVATE_OFFSET
                )
            except (OSError, RuntimeError, struct.error):
                self._diagnostic_add(
                    "target_lookup_object_class_read_failures"
                )
                continue
            if class_pointer not in self.trusted_common_component_classes:
                continue
            self._diagnostic_add("target_lookup_object_class_candidates")
            raw = read_region(self.process, component, BOSS_COMPONENT_READ_SIZE)
            if not raw or len(raw) < BOSS_COMPONENT_READ_SIZE:
                self._diagnostic_add(
                    "target_lookup_object_component_read_failures"
                )
                continue
            entity_id = struct.unpack_from(
                "<Q", raw, BOSS_TYPE_ENTITY_ID_OFFSET
            )[0]
            if not self._plausible_entity_id(entity_id):
                continue
            self._diagnostic_add("target_lookup_object_entity_candidates")
            self._diagnostic_add("target_lookup_object_candidates")
            template_id = struct.unpack_from(
                "<I", raw, BOSS_TEMPLATE_ID_OFFSET
            )[0]
            if entity_id in pending_targets:
                self._diagnostic_add(
                    "target_lookup_object_exact_entity_matches"
                )
                if template_id:
                    self._diagnostic_add(
                        "target_lookup_object_exact_template_nonzero"
                    )
                else:
                    self._diagnostic_add(
                        "target_lookup_object_exact_template_zero"
                    )
            self._remember_common_component_record(
                {
                    "filetime_100ns": (
                        time.time_ns() // 100 + FILETIME_UNIX_EPOCH_100NS
                    ),
                    "sequence": -1,
                    "function": "CommonComponent_TargetIdIndex",
                    "component": component,
                    "entity_id": entity_id,
                    "boss_type": int(raw[BOSS_TYPE_FIELD_OFFSET]),
                    "template_id": template_id,
                    "target_id_index": True,
                }
            )
        self.target_boss_object_index_complete = True

    def _resolve_target_boss_lookups(self) -> list[dict]:
        if not self.target_boss_lookup_enabled or not self.pending_target_boss_lookups:
            return []
        now = time.monotonic()
        if (
            now - self._last_target_boss_lookup_poll
            < TARGET_BOSS_LOOKUP_RETRY_SECONDS
        ):
            return []
        self._last_target_boss_lookup_poll = now
        unresolved_without_component = any(
            not self.common_components_by_entity.get(target_id)
            for target_id in self.pending_target_boss_lookups
        )
        if unresolved_without_component:
            self._build_target_common_component_index_once()

        resolved: list[dict] = []
        for target_id, first_seen in list(
            self.pending_target_boss_lookups.items()
        ):
            update = None
            for component in tuple(
                self.common_components_by_entity.get(target_id, ())
            ):
                update = self._read_target_common_component(
                    component, target_id
                )
                if update is not None:
                    break
            if update is not None:
                resolved.append(update)
                self._diagnostic_add("target_lookup_resolved")
                self.target_boss_lookup_emitted.add(target_id)
                self.target_boss_lookup_attempted.add(target_id)
                self.pending_target_boss_lookups.pop(target_id, None)
            elif now - first_seen >= TARGET_BOSS_LOOKUP_MAX_WAIT_SECONDS:
                self._diagnostic_add("target_lookup_timeouts")
                self.target_boss_lookup_attempted.add(target_id)
                self.pending_target_boss_lookups.pop(target_id, None)
        return resolved

    def _iter_live_object_pointers(
        self, *, track_target_lookup: bool = False
    ):
        if track_target_lookup:
            self._diagnostic_add("target_lookup_object_table_reads")
        try:
            object_count = self._u32(self.base + GOBJECT_COUNT_RVA)
            chunks = self._u64(self.base + GOBJECT_CHUNKS_RVA)
        except (OSError, RuntimeError, struct.error):
            if track_target_lookup:
                self._diagnostic_add(
                    "target_lookup_object_table_failures"
                )
            return
        if not chunks or not (0 < object_count <= 5_000_000):
            if track_target_lookup:
                self._diagnostic_add(
                    "target_lookup_object_table_failures"
                )
            return
        for chunk_index in range((object_count + 65_535) // 65_536):
            try:
                chunk = self._u64(chunks + chunk_index * 8)
            except (OSError, RuntimeError, struct.error):
                continue
            if not chunk:
                continue
            slot_count = min(65_536, object_count - chunk_index * 65_536)
            raw = read_region(self.process, chunk, slot_count * 24)
            if not raw:
                continue
            if track_target_lookup:
                self._diagnostic_add(
                    "target_lookup_object_slots_scanned", len(raw) // 24
                )
            for offset in range(0, len(raw) - 23, 24):
                object_pointer = struct.unpack_from("<Q", raw, offset + 8)[0]
                if object_pointer:
                    if track_target_lookup:
                        self._diagnostic_add(
                            "target_lookup_object_pointers"
                        )
                    yield object_pointer

    def _scan_existing_boss_components(
        self, target_ids: set[int]
    ) -> list[dict]:
        """Recover Boss metadata for targets created before the hook started."""
        wanted = {
            int(entity_id)
            for entity_id in target_ids
            if self._plausible_entity_id(int(entity_id))
        }
        if not wanted:
            return []
        missing = wanted - self.existing_boss_component_cache.keys()
        if missing:
            self._diagnostic_add("existing_boss_scan_attempts")
            filetime_100ns = (
                time.time_ns() // 100 + FILETIME_UNIX_EPOCH_100NS
            )
            for component in self._iter_live_object_pointers():
                raw = read_region(
                    self.process, component, BOSS_COMPONENT_READ_SIZE
                )
                if not raw or len(raw) < BOSS_COMPONENT_READ_SIZE:
                    continue
                if raw[BOSS_TYPE_FIELD_OFFSET] != BOSS_TYPE_BOSS_VALUE:
                    continue
                entity_id = struct.unpack_from(
                    "<Q", raw, BOSS_TYPE_ENTITY_ID_OFFSET
                )[0]
                template_id = struct.unpack_from(
                    "<I", raw, BOSS_TEMPLATE_ID_OFFSET
                )[0]
                if not self._plausible_entity_id(entity_id) or not template_id:
                    continue
                self.existing_boss_component_cache[entity_id] = {
                    "filetime_100ns": filetime_100ns,
                    "sequence": -1,
                    "function": "CommonComponent_ExistingBossType",
                    "component": component,
                    "entity_id": entity_id,
                    "boss_type": BOSS_TYPE_BOSS_VALUE,
                    "template_id": template_id,
                    "existing_object_scan": True,
                }
                self._diagnostic_add("existing_boss_scan_matches")
        return [
            dict(self.existing_boss_component_cache[entity_id])
            for entity_id in wanted
            if entity_id in self.existing_boss_component_cache
        ]

    def _recover_existing_boss_components_once(
        self, target_ids: set[int]
    ) -> list[dict]:
        """Resolve pre-hook Boss targets with at most one global object walk."""
        wanted = {
            int(entity_id)
            for entity_id in target_ids
            if self._plausible_entity_id(int(entity_id))
        }
        if not wanted:
            return []
        if not self.existing_boss_full_scan_complete:
            updates = self._scan_existing_boss_components(wanted)
            self.existing_boss_full_scan_complete = True
            return updates
        return [
            dict(self.existing_boss_component_cache[entity_id])
            for entity_id in wanted
            if entity_id in self.existing_boss_component_cache
        ]

    def _resolve_pending_boss_components(self) -> list[dict]:
        now = time.monotonic()
        resolved: list[dict] = []
        for component, (record, first_seen) in list(
            self.pending_boss_components.items()
        ):
            if now - first_seen > 300.0:
                self.pending_boss_components.pop(component, None)
                continue
            try:
                entity_id = self._u64(component + BOSS_TYPE_ENTITY_ID_OFFSET)
            except (OSError, RuntimeError, struct.error):
                continue
            if not self._plausible_entity_id(entity_id):
                continue
            update = dict(record)
            update["entity_id"] = entity_id
            update["function"] = f"{record.get('function', 'BossType')}/resolved"
            update["resolved_late"] = True
            resolved.append(update)
            self._diagnostic_add("late_boss_component_resolved")
            self.pending_boss_components.pop(component, None)
        return resolved

    def _poll_boss_ring(
        self,
        ring: int,
        next_sequence: int,
        magic: bytes,
        function: str,
    ) -> tuple[list[dict], int]:
        self._diagnostic_add("boss_ring_header_reads")
        header = read_region(self.process, ring, 0x20)
        if not header or header[:8] != magic:
            self._diagnostic_add("boss_ring_header_failures")
            raise RuntimeError(f"{function} ring buffer became unreadable or corrupt")
        write_index = struct.unpack_from("<Q", header, 8)[0]
        if write_index - next_sequence > BOSS_TYPE_RECORD_COUNT:
            self._diagnostic_add(
                "boss_ring_overruns",
                write_index - next_sequence - BOSS_TYPE_RECORD_COUNT,
            )
            next_sequence = write_index - BOSS_TYPE_RECORD_COUNT
        updates: list[dict] = []
        while next_sequence < write_index:
            self._diagnostic_add("boss_ring_records_polled")
            slot = next_sequence & BOSS_TYPE_RECORD_MASK
            raw = read_region(
                self.process,
                ring
                + BOSS_TYPE_RECORDS_OFFSET
                + slot * BOSS_TYPE_RECORD_SIZE,
                BOSS_TYPE_RECORD_SIZE,
            )
            record = parse_boss_type_record(
                raw or b"", next_sequence, function
            )
            if record is None:
                self._diagnostic_add("boss_ring_parse_failures")
                break
            next_sequence += 1
            updates.append(record)
        return updates, next_sequence

    def poll_boss_types(self) -> list[dict]:
        if not self.alive or not (
            self.boss_type_installed or self.boss_init_installed
        ):
            return []
        updates: list[dict] = []
        if self.boss_type_installed:
            records, self.boss_type_next_sequence = self._poll_boss_ring(
                self.boss_type_ring,
                self.boss_type_next_sequence,
                BOSS_TYPE_MAGIC,
                "KAPI_Common_SetBossType",
            )
            updates.extend(records)
        if self.boss_init_installed:
            records, self.boss_init_next_sequence = self._poll_boss_ring(
                self.boss_init_ring,
                self.boss_init_next_sequence,
                BOSS_INIT_MAGIC,
                "CommonComponent_TemplateBossType",
            )
            updates.extend(records)
        for record in updates:
            self._remember_common_component_record(record)
            component = int(record.get("component", 0) or 0)
            entity_id = int(record.get("entity_id", 0) or 0)
            if (
                int(record.get("boss_type", -1) or 0) == BOSS_TYPE_BOSS_VALUE
                and component
                and not self._plausible_entity_id(entity_id)
            ):
                self.pending_boss_components[component] = (
                    record,
                    time.monotonic(),
                )
        late_updates = self._resolve_pending_boss_components()
        for record in late_updates:
            self._remember_common_component_record(record)
        updates.extend(late_updates)
        if self.target_boss_lookup_enabled:
            for record in updates:
                entity_id = int(record.get("entity_id", 0) or 0)
                template_id = int(record.get("template_id", 0) or 0)
                if self._plausible_entity_id(entity_id) and template_id:
                    # The normal hook path already supplied complete identity;
                    # target lookup must remain a fallback, not emit duplicates.
                    self.target_boss_lookup_attempted.add(entity_id)
                    self.pending_target_boss_lookups.pop(entity_id, None)
        observed_boss_ids = {
            int(record.get("entity_id", 0) or 0)
            for record in updates
            if int(record.get("boss_type", -1) or 0)
            == BOSS_TYPE_BOSS_VALUE
            and self._plausible_entity_id(
                int(record.get("entity_id", 0) or 0)
            )
        }
        self.existing_boss_scan_attempted.update(observed_boss_ids)
        self.pending_existing_boss_targets.difference_update(observed_boss_ids)
        scan_targets = (
            self.pending_existing_boss_targets
            - self.existing_boss_scan_attempted
        )
        if scan_targets:
            updates.extend(
                self._recover_existing_boss_components_once(set(scan_targets))
            )
            self.existing_boss_scan_attempted.update(scan_targets)
            self.pending_existing_boss_targets.difference_update(scan_targets)
        updates.extend(self._resolve_target_boss_lookups())
        return updates

    @staticmethod
    def _clean_entity_name(value: str) -> str:
        value = value.strip().replace("\x00", "")
        if not value or len(value) > 64:
            return ""
        if any(ord(char) < 0x20 for char in value):
            return ""
        return value

    def _read_name_cache(self) -> list[dict]:
        if not self.name_manager:
            return []
        container = self.name_manager + 0x580
        entries = self._u64(container)
        array_num = self._u32(container + 8)
        if not entries or array_num > 100_000:
            return []
        updates: list[dict] = []
        for first_index in range(0, array_num, NAME_CACHE_READ_BATCH_ENTRIES):
            entry_count = min(
                NAME_CACHE_READ_BATCH_ENTRIES, array_num - first_index
            )
            batch = read_region(
                self.process,
                entries + first_index * NAME_CACHE_ENTRY_SIZE,
                entry_count * NAME_CACHE_ENTRY_SIZE,
            )
            if not batch or len(batch) != entry_count * NAME_CACHE_ENTRY_SIZE:
                continue
            for offset in range(0, len(batch), NAME_CACHE_ENTRY_SIZE):
                entity_id, string_data, string_num, string_max = struct.unpack_from(
                    "<QQii", batch, offset
                )
                if (
                    not entity_id
                    or entity_id in self.entity_names
                    or not string_data
                    or not (1 <= string_num <= string_max <= 256)
                ):
                    continue
                raw = read_region(
                    self.process, string_data, min(string_num - 1, 64) * 2
                )
                if raw is None:
                    continue
                name = self._clean_entity_name(raw.decode("utf-16le", "replace"))
                if not name:
                    continue
                self.entity_names[entity_id] = name
                updates.append(
                    {
                        "function": "KAPI_DataCache_CacheEntityName/cache",
                        "manager": self.name_manager,
                        "entity_id": entity_id,
                        "name": name,
                    }
                )
        return updates

    def _read_skill_name_cache(self) -> list[dict]:
        """Read skill names already decoded by the game's own data cache."""
        if not self.name_manager:
            return []
        container = self.name_manager + 0x190
        entries = self._u64(container)
        array_max = self._u32(container + 12)
        if not entries or array_max > 100_000:
            return []
        updates: list[dict] = []
        for first_index in range(0, array_max, SKILL_CACHE_READ_BATCH_ENTRIES):
            entry_count = min(
                SKILL_CACHE_READ_BATCH_ENTRIES, array_max - first_index
            )
            batch = read_region(
                self.process,
                entries + first_index * SKILL_CACHE_ENTRY_SIZE,
                entry_count * SKILL_CACHE_ENTRY_SIZE,
            )
            if not batch or len(batch) != entry_count * SKILL_CACHE_ENTRY_SIZE:
                continue
            for local_index in range(entry_count):
                offset = local_index * SKILL_CACHE_ENTRY_SIZE
                skill_id = struct.unpack_from("<I", batch, offset)[0]
                if skill_id in self.skill_names:
                    continue
                string_data, string_num, string_max = struct.unpack_from(
                    "<Qii", batch, offset + 0x10
                )
                if (
                    not (10_000_000 <= skill_id <= 999_999_999)
                    or not string_data
                    or not (2 <= string_num <= string_max <= 128)
                ):
                    continue
                encoded = read_region(
                    self.process, string_data, min(string_num - 1, 64) * 2
                )
                if encoded is None:
                    continue
                name = self._clean_entity_name(
                    encoded.decode("utf-16le", "replace")
                )
                if not name:
                    continue
                self.skill_names[skill_id] = name
                updates.append(
                    {
                        "function": "KAPI_DataCache_CacheSkillAgentData/cache",
                        "manager": self.name_manager,
                        "skill_id": skill_id,
                        "name": name,
                    }
                )
        return updates

    def poll_names(self) -> list[dict]:
        if not self.alive:
            return []
        updates: list[dict] = []
        if self.name_installed:
            header = read_region(self.process, self.name_ring, 0x20)
            if not header or header[:8] != NAME_MAGIC:
                raise RuntimeError("entity-name ring buffer became unreadable or corrupt")
            write_index = struct.unpack_from("<Q", header, 8)[0]
            if write_index - self.name_next_sequence > NAME_RECORD_COUNT:
                self.name_next_sequence = write_index - NAME_RECORD_COUNT
            while self.name_next_sequence < write_index:
                slot = self.name_next_sequence & NAME_RECORD_MASK
                raw = read_region(
                    self.process,
                    self.name_ring + NAME_RECORDS_OFFSET + slot * NAME_RECORD_SIZE,
                    NAME_RECORD_SIZE,
                )
                record = parse_name_record(raw or b"", self.name_next_sequence)
                if record is None:
                    break
                self.name_next_sequence += 1
                self.name_manager = int(record["manager"])
                name = self._clean_entity_name(str(record.get("name", "")))
                entity_id = int(record["entity_id"])
                if name and self.entity_names.get(entity_id) != name:
                    record["name"] = name
                    self.entity_names[entity_id] = name
                    updates.append(record)
        now = time.monotonic()
        if self.name_manager and now - self._last_name_cache_scan >= 1.0:
            self._last_name_cache_scan = now
            updates.extend(self._read_name_cache())
        return updates

    def poll_skill_names(self) -> list[dict]:
        if not self.alive or not self.name_manager:
            return []
        now = time.monotonic()
        if now - self._last_skill_cache_scan < 1.0:
            return []
        self._last_skill_cache_scan = now
        return self._read_skill_name_cache()

    def close(self) -> None:
        owned_damage = self.installed and not self.adopted
        owned_name = self.name_installed and not self.name_adopted
        owned_boss_type = (
            self.boss_type_installed and not self.boss_type_adopted
        )
        owned_boss_init = (
            self.boss_init_installed and not self.boss_init_adopted
        )
        owned_any = bool(
            owned_damage or owned_name or owned_boss_type or owned_boss_init
        )
        if (
            self.process
            and (
                self.installed
                or self.name_installed
                or self.boss_type_installed
                or self.boss_init_installed
            )
            and self.alive
        ):
            suspended: list[int] = []
            try:
                suspended = suspend_process(self.pid)
                if owned_boss_init:
                    write_code(
                        self.process,
                        self.boss_init_target,
                        BOSS_INIT_PROLOGUE,
                    )
                if owned_boss_type:
                    write_code(
                        self.process,
                        self.boss_type_target,
                        BOSS_TYPE_PROLOGUE,
                    )
                if owned_name:
                    write_code(self.process, self.name_target, NAME_PROLOGUE)
                if owned_damage:
                    write_code(self.process, self.target, PROLOGUE)
            finally:
                if suspended:
                    resume_threads(suspended)
        self.boss_type_installed = False
        self.boss_init_installed = False
        self.name_installed = False
        self.installed = False
        if (
            self.process
            and not self.installed
            and not self.name_installed
            and not self.boss_type_installed
            and not self.boss_init_installed
            and self.alive
            and not owned_any
        ):
            # Only pre-install allocations are released.  After an installed
            # trampoline is detached, a resumed game thread may still be
            # returning through that page.  Leaving those now-unreachable
            # pages allocated avoids a use-after-free; the OS releases them
            # with the game process.
            if self.boss_init_stub and not self.boss_init_adopted:
                kernel32.VirtualFreeEx(
                    self.process,
                    ctypes.c_void_p(self.boss_init_stub),
                    0,
                    MEM_RELEASE,
                )
            if self.boss_init_ring and not self.boss_init_adopted:
                kernel32.VirtualFreeEx(
                    self.process,
                    ctypes.c_void_p(self.boss_init_ring),
                    0,
                    MEM_RELEASE,
                )
            if self.boss_type_stub and not self.boss_type_adopted:
                kernel32.VirtualFreeEx(
                    self.process,
                    ctypes.c_void_p(self.boss_type_stub),
                    0,
                    MEM_RELEASE,
                )
            if self.boss_type_ring and not self.boss_type_adopted:
                kernel32.VirtualFreeEx(
                    self.process,
                    ctypes.c_void_p(self.boss_type_ring),
                    0,
                    MEM_RELEASE,
                )
            if self.name_stub and not self.name_adopted:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.name_stub), 0, MEM_RELEASE
                )
            if self.name_ring and not self.name_adopted:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.name_ring), 0, MEM_RELEASE
                )
            if self.stub and not self.adopted:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.stub), 0, MEM_RELEASE
                )
            if self.ring and not self.adopted:
                kernel32.VirtualFreeEx(
                    self.process, ctypes.c_void_p(self.ring), 0, MEM_RELEASE
                )
        if self.process:
            kernel32.CloseHandle(self.process)
        self.process = 0
        self.ring = 0
        self.stub = 0
        self.installed = False
        self.name_ring = 0
        self.name_stub = 0
        self.name_installed = False
        self.boss_type_ring = 0
        self.boss_type_stub = 0
        self.boss_type_installed = False
        self.boss_init_ring = 0
        self.boss_init_stub = 0
        self.boss_init_installed = False
        self.adopted = False
        self.name_adopted = False
        self.boss_type_adopted = False
        self.boss_init_adopted = False
        self.pending_boss_components.clear()
        self.pending_existing_boss_targets.clear()
        self.existing_boss_scan_attempted.clear()
        self.existing_boss_component_cache.clear()
        self.existing_boss_full_scan_complete = False
        self.pending_target_boss_lookups.clear()
        self.target_boss_lookup_attempted.clear()
        self.target_boss_lookup_emitted.clear()
        self.observed_common_components.clear()
        self.common_components_by_entity.clear()
        self.trusted_common_component_classes.clear()
        self.target_boss_object_index_complete = False
        self.target_boss_object_scan_count = 0
        self._last_target_boss_lookup_poll = 0.0

    def __enter__(self) -> "DamageHook":
        return self.install()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def wait_for_hook(stop, status_callback=None) -> DamageHook | None:
    """Wait for the game, install the hook, and return it unless stopped."""
    last_message = None
    while not stop.is_set():
        try:
            hook = DamageHook().install()
            if status_callback:
                status_callback("connected", hook)
            return hook
        except RuntimeError as exc:
            message = str(exc)
            if "process not found" not in message:
                raise
            if status_callback and message != last_message:
                status_callback("waiting", message)
                last_message = message
        stop.wait(1.0)
    return None
