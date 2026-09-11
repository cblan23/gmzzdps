#!/usr/bin/env python3
"""Isolated capture process for the live C7 hooks.

The network hook is polled by a dedicated thread so synchronized game calls
are acknowledged independently from native metadata scans, IPC serialization,
logging, parsing, and UI work.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback
from damage_hook import DamageHook
from network_capture import NetworkMessageHook
from network_state import (
    TEAM_STATISTICS_METHODS,
    should_decode_network_arguments,
    should_retain_network_record,
)
from team_stats_request_hook import TeamStatsRequestHook
from runtime_capability import RuntimeCapability, RuntimeCapabilityError


CAPTURE_IDLE_WAIT_SECONDS = 0.001
CAPTURE_RETRY_WAIT_SECONDS = 0.5
GAME_SEARCH_WAIT_SECONDS = 1.0
TEAM_STATUS_INTERVAL_SECONDS = 1.0
TEAM_STATS_RESPONSE_TIMEOUT_SECONDS = 20.0
TEAM_STATS_RESPONSE_MIN_REQUESTS = 8
TEAM_STATS_MAX_REINSTALLS_PER_SESSION = 1
TEAM_STATS_RECOVERY_ACTIVITY_WINDOW_SECONDS = 10.0
TEAM_STATS_NEW_ACTIVITY_EPOCH_GAP_SECONDS = 60.0

# Team snapshots are the one capture component that actively calls back into
# the game.  Keep its lifecycle behind an explicit parent-controlled mode so
# a training dummy never needs the extra RPC at all.  The numeric values are
# stored in a multiprocessing.Value because Event only gives us two states.
TEAM_STATS_MODE_UNKNOWN = 0
TEAM_STATS_MODE_DUMMY = 1
TEAM_STATS_MODE_TEAM = 2
TEAM_STATS_MODE_NAMES = {
    TEAM_STATS_MODE_UNKNOWN: "unknown",
    TEAM_STATS_MODE_DUMMY: "dummy",
    TEAM_STATS_MODE_TEAM: "team",
}
TEAM_STATS_MODE_CODES = {
    name: code for code, name in TEAM_STATS_MODE_NAMES.items()
}
VERIFIED_ZERO_ARGUMENT_TEAM_DETAIL_REQUEST_METHODS: frozenset[str] = frozenset()
PASSIVE_TEAM_DETAIL_REQUEST_METHODS = frozenset(
    {
        "ReqCommonCombatStatistics",
        "ReqDungeonBattleStatistics",
        "ReqMonsterBattleStatistics",
        "ReqNpcCombatStatisticsByTeam",
        "ReqDirtyNpcCombatStatisticsByTeam",
    }
)
TEAM_STATS_COMBAT_ACTIVITY_METHODS = frozenset(
    {
        "OnMsgDamageSyncV2",
        "OnMsgHealSyncV2",
        "OnMsgSyncFightMode",
        "OnMsgSyncCurrentHp",
        "OnMsgUpdateStageCombatStatistics",
        "OnMsgSettlementCombatStatistics",
    }
)


class TeamStatsResponseHealth:
    """Track real team-stat replies and bound recovery attempts.

    A successful local ``call_server`` result only proves that the game
    accepted the function call.  It does not prove that a team snapshot came
    back from the server, so recovery decisions use captured response methods
    plus the number of requests issued since the last response.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = TEAM_STATS_RESPONSE_TIMEOUT_SECONDS,
        minimum_requests: int = TEAM_STATS_RESPONSE_MIN_REQUESTS,
        maximum_reinstalls: int = TEAM_STATS_MAX_REINSTALLS_PER_SESSION,
        activity_window_seconds: float = (
            TEAM_STATS_RECOVERY_ACTIVITY_WINDOW_SECONDS
        ),
        activity_epoch_gap_seconds: float = (
            TEAM_STATS_NEW_ACTIVITY_EPOCH_GAP_SECONDS
        ),
    ) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.minimum_requests = max(1, int(minimum_requests))
        self.maximum_reinstalls = max(0, int(maximum_reinstalls))
        self.activity_window_seconds = max(
            1.0, float(activity_window_seconds)
        )
        self.activity_epoch_gap_seconds = max(
            self.activity_window_seconds,
            float(activity_epoch_gap_seconds),
        )
        self.mode = TEAM_STATS_MODE_UNKNOWN
        self.hook_installed = False
        self.active_since = 0.0
        self.request_baseline = 0
        self.last_request_count = 0
        self.last_response_at = 0.0
        self.last_response_filetime = 0
        self.response_count = 0
        self.last_combat_activity_at = 0.0
        self.combat_activity_epoch_at = 0.0
        self.response_seen_in_epoch = False
        self.response_baseline_pending = False
        self.recovery_phase = 0
        self.rearm_count = 0
        self.reinstall_count = 0
        self.data_incomplete = False
        self.state = "inactive"

    @staticmethod
    def _request_count(status: object) -> int:
        if not isinstance(status, dict):
            return 0
        try:
            return max(0, int(status.get("request_count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def set_mode(self, mode: object, now: float) -> None:
        normalized = normalize_team_stats_mode(
            mode, default=TEAM_STATS_MODE_UNKNOWN
        )
        if normalized == self.mode:
            return
        self.mode = normalized
        self.active_since = float(now)
        self.request_baseline = self.last_request_count
        self.response_seen_in_epoch = False
        self.response_baseline_pending = False
        self.recovery_phase = 0
        if normalized != TEAM_STATS_MODE_TEAM:
            self.state = "inactive"
        elif self.hook_installed:
            self.state = "waiting_response"
        else:
            self.state = "hook_missing"

    def mark_hook_installed(
        self,
        now: float,
        *,
        request_count: int = 0,
        preserve_recovery: bool = False,
    ) -> None:
        self.hook_installed = True
        self.active_since = float(now)
        self.request_baseline = max(0, int(request_count))
        self.last_request_count = self.request_baseline
        self.response_seen_in_epoch = False
        self.response_baseline_pending = False
        if not preserve_recovery:
            self.recovery_phase = 0
        if preserve_recovery and self.recovery_phase >= 2:
            self.state = "reinstalled_waiting"
        elif self.mode == TEAM_STATS_MODE_TEAM:
            self.state = "waiting_response"
        else:
            self.state = "inactive"

    def mark_hook_missing(self) -> None:
        self.hook_installed = False
        if self.mode == TEAM_STATS_MODE_TEAM:
            self.data_incomplete = True
        self.state = (
            "hook_missing"
            if self.mode == TEAM_STATS_MODE_TEAM
            else "inactive"
        )

    def observe_records(self, records: object, now: float) -> int:
        matched = 0
        latest_filetime = 0
        for record in records if isinstance(records, list) else ():
            if not isinstance(record, dict):
                continue
            method = str(record.get("method", ""))
            if method in TEAM_STATS_COMBAT_ACTIVITY_METHODS:
                self._mark_combat_activity(now)
            if method not in TEAM_STATISTICS_METHODS:
                continue
            matched += 1
            try:
                latest_filetime = max(
                    latest_filetime,
                    int(record.get("filetime_100ns", 0) or 0),
                )
            except (TypeError, ValueError, OverflowError):
                pass
        if matched:
            self.response_count += matched
            self.last_response_at = float(now)
            self.last_response_filetime = max(
                self.last_response_filetime, latest_filetime
            )
            self.response_seen_in_epoch = True
            self.response_baseline_pending = True
            self.recovery_phase = 0
            self.data_incomplete = False
            self.state = "healthy"
        return matched

    def _mark_combat_activity(self, now: float) -> None:
        timestamp = float(now)
        if (
            not self.last_combat_activity_at
            or timestamp - self.last_combat_activity_at
            > self.activity_epoch_gap_seconds
        ):
            self.combat_activity_epoch_at = timestamp
        self.last_combat_activity_at = timestamp

    def observe_native_damage(self, records: object, now: float) -> None:
        for record in records if isinstance(records, list) else ():
            if not isinstance(record, dict):
                continue
            try:
                damage = max(
                    int(record.get("damage", 0) or 0),
                    int(record.get("raw_damage", 0) or 0),
                )
            except (TypeError, ValueError, OverflowError):
                damage = 0
            if damage > 0:
                self._mark_combat_activity(now)
                return

    def assess(self, now: float, status: object) -> str | None:
        request_count = self._request_count(status)
        if request_count < self.last_request_count:
            # A hook replaced outside this monitor starts a fresh request
            # epoch. Do not mistake its lower counter for a stalled stream.
            self.request_baseline = request_count
            self.active_since = float(now)
            self.response_seen_in_epoch = False
            self.response_baseline_pending = False
        self.last_request_count = request_count
        if self.response_baseline_pending:
            self.request_baseline = request_count
            self.response_baseline_pending = False

        if self.mode != TEAM_STATS_MODE_TEAM:
            self.state = "inactive"
            return None
        if not self.hook_installed:
            self.state = "hook_missing"
            return None
        enabled = bool(
            isinstance(status, dict) and status.get("enabled", False)
        )
        if not enabled:
            self.active_since = float(now)
            self.request_baseline = request_count
            self.state = "disabled"
            return None
        activity_recent = bool(
            self.last_combat_activity_at
            and float(now) - self.last_combat_activity_at
            <= self.activity_window_seconds
        )
        if not activity_recent:
            if self.state not in {"rearmed_waiting", "reinstalled_waiting"}:
                self.state = (
                    "healthy"
                    if self.response_seen_in_epoch
                    else "waiting_response"
                )
            return None

        reference = (
            self.last_response_at
            if self.response_seen_in_epoch
            else max(self.active_since, self.combat_activity_epoch_at)
        )
        elapsed = max(0.0, float(now) - float(reference or now))
        requests_since_response = max(
            0, request_count - self.request_baseline
        )
        if (
            elapsed < self.timeout_seconds
            or requests_since_response < self.minimum_requests
        ):
            if self.state not in {"rearmed_waiting", "reinstalled_waiting"}:
                self.state = (
                    "healthy"
                    if self.response_seen_in_epoch
                    else "waiting_response"
                )
            return None
        if self.recovery_phase == 0:
            return "rearm"
        if (
            self.recovery_phase == 1
            and self.reinstall_count < self.maximum_reinstalls
        ):
            return "reinstall"
        self.state = "unhealthy"
        return None

    def mark_rearmed(self, now: float, status: object) -> None:
        self.rearm_count += 1
        self.recovery_phase = 1
        self.data_incomplete = True
        self.active_since = float(now)
        self.request_baseline = self._request_count(status)
        self.response_seen_in_epoch = False
        self.response_baseline_pending = False
        self.state = "rearmed_waiting"

    def mark_reinstalled(self, now: float) -> None:
        self.reinstall_count += 1
        self.recovery_phase = 2
        self.data_incomplete = True
        self.active_since = float(now)
        self.request_baseline = 0
        self.last_request_count = 0
        self.response_seen_in_epoch = False
        self.response_baseline_pending = False
        self.hook_installed = False
        self.state = "reinstalling"

    def mark_recovery_failed(self) -> None:
        self.recovery_phase = 2
        self.data_incomplete = True
        self.state = "recovery_failed"

    def snapshot(self, now: float, status: object) -> dict[str, object]:
        request_count = self._request_count(status)
        response_age = (
            max(0.0, float(now) - self.last_response_at)
            if self.last_response_at
            else None
        )
        activity_age = (
            max(0.0, float(now) - self.last_combat_activity_at)
            if self.last_combat_activity_at
            else None
        )
        return {
            "installed": bool(self.hook_installed),
            "response_health": self.state,
            "data_incomplete": bool(self.data_incomplete),
            "response_count": int(self.response_count),
            "last_response_filetime": int(self.last_response_filetime),
            "last_response_age_seconds": (
                round(response_age, 3) if response_age is not None else None
            ),
            "last_combat_activity_age_seconds": (
                round(activity_age, 3) if activity_age is not None else None
            ),
            "requests_since_response": max(
                0, request_count - self.request_baseline
            ),
            "rearm_count": int(self.rearm_count),
            "reinstall_count": int(self.reinstall_count),
        }


def normalize_team_stats_mode(
    value: object,
    *,
    default: int = TEAM_STATS_MODE_TEAM,
) -> int:
    """Normalize a parent/child team-stat mode without raising in cleanup."""
    if isinstance(value, str):
        code = TEAM_STATS_MODE_CODES.get(value.strip().casefold())
        return int(default if code is None else code)
    try:
        code = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return int(default)
    return code if code in TEAM_STATS_MODE_NAMES else int(default)


def team_stats_mode_name(value: object) -> str:
    """Return the stable wire/diagnostic name for a mode value."""
    return TEAM_STATS_MODE_NAMES.get(
        normalize_team_stats_mode(value),
        TEAM_STATS_MODE_NAMES[TEAM_STATS_MODE_TEAM],
    )


def read_team_stats_mode(
    shared_value: object,
    *,
    default: int = TEAM_STATS_MODE_TEAM,
) -> int:
    """Read a multiprocessing.Value, retaining compatibility with old callers."""
    if shared_value is None:
        return int(default)
    try:
        value = shared_value.value
    except (AttributeError, OSError, ValueError):
        value = shared_value
    return normalize_team_stats_mode(value, default=default)


def authorized_team_detail_request_methods(runtime_profile: object) -> tuple[str, ...]:
    """Select only signed methods whose zero-argument shape is verified."""
    if not isinstance(runtime_profile, dict):
        return ()
    protocol = runtime_profile.get("protocol")
    if not isinstance(protocol, dict):
        return ()
    primary = str(protocol.get("team_stats_request_method", "")).strip()
    methods = protocol.get("synchronized_methods", ())
    if not isinstance(methods, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            method
            for raw_method in methods
            for method in (str(raw_method).strip(),)
            if method
            and method != primary
            and method in VERIFIED_ZERO_ARGUMENT_TEAM_DETAIL_REQUEST_METHODS
        )
    )


def passive_team_detail_request_methods(runtime_profile: object) -> tuple[str, ...]:
    """Select signed detail requests whose natural arguments must be captured."""
    if not isinstance(runtime_profile, dict):
        return ()
    protocol = runtime_profile.get("protocol")
    if not isinstance(protocol, dict):
        return ()
    methods = protocol.get("synchronized_methods", ())
    if not isinstance(methods, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(
            method
            for raw_method in methods
            for method in (str(raw_method).strip(),)
            if method in PASSIVE_TEAM_DETAIL_REQUEST_METHODS
        )
    )

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    _kernel32.GetCurrentThread.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_int
    _kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.SetPriorityClass.restype = ctypes.c_int
    _kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _kernel32.SetThreadPriority.restype = ctypes.c_int
else:
    _kernel32 = None

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
THREAD_PRIORITY_HIGHEST = 2


def _set_current_thread_priority() -> bool:
    if _kernel32 is None:
        return False
    return bool(
        _kernel32.SetThreadPriority(
            _kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST
        )
    )


def configure_capture_priority() -> dict[str, object]:
    """Favor prompt acknowledgements without starving the normal-priority game."""
    if _kernel32 is None:
        return {"priority_class": "default", "priority_applied": False}
    process_applied = bool(
        _kernel32.SetPriorityClass(
            _kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS
        )
    )
    return {
        "priority_class": "above_normal" if process_applied else "default",
        "priority_applied": process_applied,
        "main_thread_priority_applied": False,
    }


class ParentProcessWatchdog:
    """Detect a parent crash without relying on Python's parent-process state."""

    def __init__(self, parent_pid: int):
        self.parent_pid = int(parent_pid or 0)
        self.handle = 0
        if _kernel32 is not None and self.parent_pid > 0:
            self.handle = int(
                _kernel32.OpenProcess(SYNCHRONIZE, False, self.parent_pid) or 0
            )

    def is_alive(self) -> bool:
        if self.parent_pid <= 0:
            return False
        if _kernel32 is not None:
            if not self.handle:
                return False
            return (
                _kernel32.WaitForSingleObject(
                    ctypes.c_void_p(self.handle), 0
                )
                == WAIT_TIMEOUT
            )
        try:
            os.kill(self.parent_pid, 0)
        except OSError:
            return False
        return True

    def close(self) -> None:
        if self.handle and _kernel32 is not None:
            _kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = 0


def _runtime_lease_active(runtime_expiry=None) -> bool:
    if runtime_expiry is None:
        return True
    try:
        expires_at = float(runtime_expiry.value)
    except (AttributeError, TypeError, ValueError, OSError):
        return False
    return bool(expires_at > time.time())


class RuntimeLeaseGate:
    """Child-owned lease state; parent IPC can renew it only with a valid signature."""

    def __init__(
        self,
        capability: RuntimeCapability,
        renewal_queue,
        revocation_value,
        *,
        trusted_public_keys=None,
        allow_development: bool = False,
    ) -> None:
        self.capability = capability
        self.renewal_queue = renewal_queue
        self.revocation_value = revocation_value
        self.trusted_public_keys = dict(trusted_public_keys or {})
        self.allow_development = bool(allow_development)
        self.revoked = False

    def _refresh(self) -> None:
        while not self.revoked:
            try:
                value = self.renewal_queue.get_nowait()
            except queue.Empty:
                return
            try:
                next_capability = RuntimeCapability.from_value(
                    value,
                    expected_session_id=self.capability.session_id,
                    expected_client_id=self.capability.client_id,
                    expected_build_id=self.capability.build_id,
                    expected_client_build=self.capability.client_build,
                    trusted_public_keys=self.trusted_public_keys,
                    allow_development=self.allow_development,
                )
                if (
                    not self.capability.same_binding(next_capability)
                    or next_capability.lease_sequence
                    <= self.capability.lease_sequence
                    or next_capability.issued_at < self.capability.issued_at
                ):
                    raise RuntimeCapabilityError(
                        "stale or mismatched runtime capability renewal"
                    )
            except RuntimeCapabilityError:
                self.revoked = True
                return
            self.capability = next_capability

    @property
    def value(self) -> float:
        try:
            if float(self.revocation_value.value) <= 0:
                return 0.0
        except (AttributeError, TypeError, ValueError, OSError):
            return 0.0
        self._refresh()
        return 0.0 if self.revoked else self.capability.expires_at


def _should_stop(
    stop_event,
    watchdog: ParentProcessWatchdog,
    runtime_expiry=None,
) -> bool:
    return bool(
        stop_event.is_set()
        or not watchdog.is_alive()
        or not _runtime_lease_active(runtime_expiry)
    )


def _interruptible_wait(
    stop_event,
    watchdog: ParentProcessWatchdog,
    seconds: float,
    runtime_expiry=None,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not _should_stop(stop_event, watchdog, runtime_expiry):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        stop_event.wait(min(0.05, remaining))
    return True


def _put(output_queue, kind: str, payload=None) -> None:
    # The multiprocessing queue is intentionally unbounded. Capture must never
    # discard or block on a full application-level queue.
    output_queue.put((kind, payload))


class NetworkPoller(threading.Thread):
    """Continuously copy, decode, and acknowledge network ring records."""

    def __init__(self, hook, stop_event, watchdog: ParentProcessWatchdog):
        super().__init__(name="C7NetworkAck", daemon=False)
        self.hook = hook
        self.stop_event = stop_event
        self.watchdog = watchdog
        self.local_stop = threading.Event()
        self.record_batches: queue.SimpleQueue = queue.SimpleQueue()
        self.errors: queue.SimpleQueue = queue.SimpleQueue()
        self.sequence_gaps: queue.SimpleQueue = queue.SimpleQueue()
        self.expected_sequence: int | None = None

    def run(self) -> None:
        _set_current_thread_priority()
        try:
            while (
                not self.local_stop.is_set()
                and self.hook.alive
            ):
                # The controller uses the shared stop event to enter its
                # cleanup block.  Keep acknowledging synchronized game RPCs
                # until that block has restored every other hook and calls
                # stop_and_join() explicitly.
                records = self.hook.poll(
                    decode_arguments=True,
                    decode_method_filter=should_decode_network_arguments,
                )
                if records:
                    retained_records: list[dict] = []
                    for record in records:
                        sequence = int(record.get("sequence", -1) or 0)
                        if (
                            self.expected_sequence is not None
                            and sequence != self.expected_sequence
                        ):
                            self.sequence_gaps.put(
                                {
                                    "expected": self.expected_sequence,
                                    "actual": sequence,
                                }
                            )
                        self.expected_sequence = sequence + 1
                        method = str(record.get("method", ""))
                        # Keep malformed/test records observable, but stop
                        # gameplay RPCs with no DPS consumer before JSON
                        # serialization, IPC, disk logging and parser work.
                        if not method or should_retain_network_record(method):
                            retained_records.append(record)
                    if retained_records:
                        self.record_batches.put(retained_records)
                    continue
                self.local_stop.wait(CAPTURE_IDLE_WAIT_SECONDS)
        except Exception:
            self.errors.put(traceback.format_exc())

    def drain_records(self) -> list[dict]:
        records: list[dict] = []
        while True:
            try:
                records.extend(self.record_batches.get_nowait())
            except queue.Empty:
                return records

    def drain_sequence_gaps(self) -> list[dict]:
        gaps: list[dict] = []
        while True:
            try:
                gaps.append(self.sequence_gaps.get_nowait())
            except queue.Empty:
                return gaps

    def take_error(self) -> str:
        try:
            return str(self.errors.get_nowait())
        except queue.Empty:
            return ""

    def stop_and_join(self, timeout: float | None = None) -> bool:
        self.local_stop.set()
        if self.is_alive():
            self.join(
                None if timeout is None else max(0.0, float(timeout))
            )
        return not self.is_alive()


_NATIVE_DIAGNOSTIC_KEYS = frozenset(
    {
        "damage_ring_header_reads",
        "damage_ring_header_failures",
        "damage_ring_records_polled",
        "damage_ring_parse_failures",
        "damage_ring_overruns",
        "damage_target_records",
        "damage_target_id_zero",
        "damage_target_id_below_supported",
        "damage_target_id_low_supported",
        "damage_target_id_mid_supported",
        "damage_target_id_gap",
        "damage_target_id_high_supported",
        "damage_target_id_above_supported",
        "local_player_id_available_records",
        "damage_attacker_matches_local_player",
        "damage_target_matches_local_player",
        "boss_ring_header_reads",
        "boss_ring_header_failures",
        "boss_ring_records_polled",
        "boss_ring_parse_failures",
        "boss_ring_overruns",
        "target_lookup_candidates",
        "target_lookup_disabled_candidates",
        "target_lookup_skipped_unplausible",
        "target_lookup_skipped_local_player",
        "target_lookup_skipped_already_attempted",
        "target_lookup_skipped_already_emitted",
        "target_lookup_pending_reobserved",
        "target_lookup_evictions",
        "target_lookup_resolved",
        "target_lookup_timeouts",
        "target_lookup_component_observations",
        "target_lookup_class_reads",
        "target_lookup_class_matches",
        "target_lookup_component_reads",
        "target_lookup_component_matches",
        "target_lookup_component_read_failures",
        "target_lookup_component_class_rejections",
        "target_lookup_component_entity_mismatches",
        "target_lookup_component_template_zero",
        "target_lookup_observed_target_matches",
        "target_lookup_observed_target_template_zero",
        "target_lookup_observed_target_template_nonzero",
        "target_lookup_object_scans",
        "target_lookup_object_candidates",
        "target_lookup_object_table_reads",
        "target_lookup_object_table_failures",
        "target_lookup_object_slots_scanned",
        "target_lookup_object_pointers",
        "target_lookup_object_class_reads",
        "target_lookup_object_class_read_failures",
        "target_lookup_object_class_candidates",
        "target_lookup_object_component_read_failures",
        "target_lookup_object_entity_candidates",
        "target_lookup_object_exact_entity_matches",
        "target_lookup_object_exact_template_zero",
        "target_lookup_object_exact_template_nonzero",
        "existing_boss_scan_attempts",
        "existing_boss_scan_matches",
        "late_boss_component_resolved",
        "damage_hook_installed",
        "damage_hook_adopted",
        "name_hook_installed",
        "boss_type_hook_installed",
        "boss_init_hook_installed",
        "template_id_hook_installed",
        "template_bulk_hook_installed",
        "target_boss_lookup_enabled",
        "target_lookup_pending",
        "target_lookup_attempted",
        "target_lookup_emitted",
        "target_lookup_observed_components",
        "target_lookup_trusted_classes",
        "target_lookup_object_index_complete",
        "target_lookup_object_scan_count",
        "existing_boss_scan_attempted",
        "existing_boss_full_scan_complete",
    }
)


def _sanitize_native_diagnostic(value: object) -> dict[str, object]:
    """Keep native troubleshooting snapshots anonymous and bounded."""

    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if key not in _NATIVE_DIAGNOSTIC_KEYS:
            continue
        if isinstance(raw_value, bool):
            sanitized[key] = bool(raw_value)
            continue
        if isinstance(raw_value, int):
            sanitized[key] = max(0, min(2_000_000_000, int(raw_value)))
            continue
        if isinstance(raw_value, float):
            if raw_value == raw_value and abs(raw_value) != float("inf"):
                sanitized[key] = max(0.0, min(2_000_000_000.0, raw_value))
    return sanitized


def native_diagnostic_snapshot(native_hook) -> dict[str, object]:
    """Read the hook's privacy-safe stage counters when supported."""

    if native_hook is None:
        return {}
    snapshot = getattr(native_hook, "diagnostic_snapshot", None)
    if not callable(snapshot):
        return {}
    try:
        return _sanitize_native_diagnostic(snapshot())
    except Exception:
        # A diagnostics-only helper must never turn a working capture into a
        # failure.  The poll error path remains the source of truth.
        return {}


def collect_native_records(native_hook) -> tuple[dict[str, list[dict]], str]:
    """Poll native rings in exactly the same order as the former worker."""
    captured: dict[str, list[dict]] = {
        "native_records": [],
        "native_boss_records": [],
        "native_name_records": [],
        "native_skill_name_records": [],
    }
    if native_hook is None:
        return captured, ""
    try:
        captured["native_records"] = native_hook.poll()
        captured["native_boss_records"] = native_hook.poll_boss_types()
        captured["native_name_records"] = native_hook.poll_names()
        captured["native_skill_name_records"] = native_hook.poll_skill_names()
    except Exception:
        return captured, traceback.format_exc()
    return captured, ""


def collect_native_records_with_diagnostic(
    native_hook,
) -> tuple[dict[str, list[dict]], str, dict[str, object]]:
    """Compatibility wrapper returning records plus an anonymous snapshot."""

    captured, error = collect_native_records(native_hook)
    return captured, error, native_diagnostic_snapshot(native_hook)


def _close_hook(output_queue, component: str, hook) -> bool:
    if hook is None:
        return True
    details = ""
    for attempt in range(3):
        try:
            hook.close()
            return True
        except Exception:
            details = traceback.format_exc()
            if attempt < 2:
                time.sleep(0.05)
    _put(
        output_queue,
        "cleanup_error",
        {"component": component, "details": details},
    )
    return False


def _emit_batch(
    output_queue,
    *,
    session_id: int,
    batch_id: int,
    game_pid: int,
    network_records: list[dict],
    request_records: list[dict],
    native_records: dict[str, list[dict]],
    native_damage_hook_installed: bool,
    team_status: dict | None = None,
    team_stats_mode: object = TEAM_STATS_MODE_TEAM,
    sequence_gaps: list[dict] | None = None,
    native_diagnostic: dict[str, object] | None = None,
    force_diagnostic: bool = False,
) -> bool:
    has_records = bool(
        network_records
        or request_records
        or any(native_records.get(key) for key in native_records)
    )
    if (
        not has_records
        and team_status is None
        and not sequence_gaps
        and not (force_diagnostic and native_diagnostic)
    ):
        return False
    _put(
        output_queue,
        "batch",
        {
            "session_id": session_id,
            "batch_id": batch_id,
            "game_pid": game_pid,
            "captured_monotonic": time.monotonic(),
            "records": network_records,
            "request_records": request_records,
            **native_records,
            "native_damage_hook_installed": bool(
                native_damage_hook_installed
            ),
            "team_status": team_status,
            "team_stats_mode": team_stats_mode_name(team_stats_mode),
            "sequence_gaps": sequence_gaps or [],
            "native_diagnostic": dict(native_diagnostic or {}),
        },
    )
    return True


def _capture_forever(
    stop_event,
    output_queue,
    watchdog: ParentProcessWatchdog,
    target_boss_lookup_event,
    runtime_profile,
    runtime_expiry,
    team_stats_mode=None,
) -> None:
    session_id = 0
    while not _should_stop(stop_event, watchdog, runtime_expiry):
        _put(
            output_queue,
            "state",
            {
                "stage": "searching_game",
                "process_found": False,
                "network_hook_installed": False,
                "native_damage_hook_installed": False,
                "team_stats_hook_installed": False,
                "team_stats_mode": team_stats_mode_name(
                    read_team_stats_mode(team_stats_mode)
                ),
                "damage_source": "none",
                "game_pid": 0,
            },
        )
        network_hook = NetworkMessageHook(
            profile=runtime_profile,
            takeover_existing=True,
        )
        try:
            network_hook.install()
        except RuntimeError as exc:
            _close_hook(output_queue, "network", network_hook)
            if "process not found" in str(exc):
                _put(output_queue, "state", {"stage": "game_not_found"})
                _interruptible_wait(
                    stop_event,
                    watchdog,
                    GAME_SEARCH_WAIT_SECONDS,
                    runtime_expiry,
                )
                continue
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "network_hook_failed",
                    "component": "network_install",
                    "details": str(exc),
                },
            )
            _interruptible_wait(
                stop_event, watchdog, 3.0, runtime_expiry
            )
            continue
        except Exception:
            _close_hook(output_queue, "network", network_hook)
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "network_hook_failed",
                    "component": "network_install",
                    "details": traceback.format_exc(),
                },
            )
            _interruptible_wait(
                stop_event, watchdog, 2.0, runtime_expiry
            )
            continue

        session_id += 1
        batch_id = 0
        game_pid = int(network_hook.pid or 0)
        network_poller = NetworkPoller(network_hook, stop_event, watchdog)
        native_hook = None
        team_hook = None
        session_reason = "capture_failed"
        connected = False
        next_team_status_at = 0.0
        next_team_install_at = 0.0
        team_reinstall_pending = False
        current_team_stats_mode = read_team_stats_mode(team_stats_mode)
        team_response_health = TeamStatsResponseHealth()
        team_response_health.set_mode(
            current_team_stats_mode, time.monotonic()
        )
        network_poller.start()
        try:
            def try_install_team_hook() -> None:
                """Install the stable zero-argument team snapshot request."""
                nonlocal team_hook, next_team_install_at, team_reinstall_pending
                if team_hook is not None:
                    return
                now = time.monotonic()
                if now < next_team_install_at:
                    return
                next_team_install_at = now + 2.0
                try:
                    team_hook = TeamStatsRequestHook(
                        profile=runtime_profile,
                        pid=game_pid,
                        interval=1.0,
                        takeover_existing=True,
                        stable_primary_only=True,
                    ).install()
                    team_response_health.mark_hook_installed(
                        now,
                        preserve_recovery=team_reinstall_pending,
                    )
                    team_reinstall_pending = False
                except Exception:
                    team_hook = None
                    team_response_health.mark_hook_missing()
                    _put(
                        output_queue,
                        "diagnostic",
                        {
                            "component": "team_install",
                            "details": traceback.format_exc(),
                        },
                    )

            if current_team_stats_mode == TEAM_STATS_MODE_TEAM:
                try_install_team_hook()
            try:
                native_hook = DamageHook(
                    profile=runtime_profile,
                    pid=game_pid,
                    capture_names=True,
                    capture_boss_types=True,
                    target_boss_lookup_enabled=(
                        target_boss_lookup_event.is_set()
                    ),
                    takeover_existing=True,
                ).install()
            except Exception:
                _put(
                    output_queue,
                    "diagnostic",
                    {
                        "component": "native_install",
                        "details": traceback.format_exc(),
                    },
                )
                try:
                    native_hook = DamageHook(
                        profile=runtime_profile,
                        pid=game_pid,
                        capture_names=False,
                        capture_boss_types=True,
                        target_boss_lookup_enabled=(
                            target_boss_lookup_event.is_set()
                        ),
                        takeover_existing=True,
                    ).install()
                except Exception:
                    native_hook = None

            _put(
                output_queue,
                "connected",
                {
                    "session_id": session_id,
                    "game_pid": game_pid,
                    "network_hook_adopted": bool(
                        getattr(network_hook, "adopted", False)
                    ),
                    "native_damage_hook_installed": native_hook is not None,
                    "native_damage_hook_adopted": bool(
                        native_hook is not None
                        and getattr(native_hook, "adopted", False)
                    ),
                    "native_name_hook_installed": bool(
                        native_hook is not None
                        and getattr(native_hook, "name_installed", False)
                    ),
                    "native_boss_type_hook_installed": bool(
                        native_hook is not None
                        and getattr(native_hook, "boss_type_installed", False)
                    ),
                    "native_boss_init_hook_installed": bool(
                        native_hook is not None
                        and getattr(native_hook, "boss_init_installed", False)
                    ),
                    "native_template_id_hook_installed": bool(
                        native_hook is not None
                        and getattr(native_hook, "template_id_installed", False)
                    ),
                    "native_template_bulk_hook_installed": bool(
                        native_hook is not None
                        and getattr(native_hook, "template_bulk_installed", False)
                    ),
                    "team_stats_hook_installed": team_hook is not None,
                    "team_stats_hook_adopted": bool(
                        team_hook is not None
                        and getattr(team_hook, "adopted", False)
                    ),
                    "team_stats_mode": team_stats_mode_name(
                        current_team_stats_mode
                    ),
                    "damage_source": (
                        "native" if native_hook is not None else "script"
                    ),
                },
            )
            connected = True

            while (
                not _should_stop(stop_event, watchdog)
                and _runtime_lease_active(runtime_expiry)
                and network_hook.alive
                and network_poller.is_alive()
            ):
                if team_hook is not None:
                    try:
                        team_hook_alive = bool(team_hook.alive)
                    except Exception:
                        team_hook_alive = False
                    if not team_hook_alive:
                        _close_hook(output_queue, "team", team_hook)
                        team_hook = None
                        team_response_health.mark_hook_missing()
                requested_team_stats_mode = read_team_stats_mode(
                    team_stats_mode
                )
                if requested_team_stats_mode != current_team_stats_mode:
                    current_team_stats_mode = requested_team_stats_mode
                    team_response_health.set_mode(
                        current_team_stats_mode, time.monotonic()
                    )
                    if current_team_stats_mode == TEAM_STATS_MODE_TEAM:
                        if team_hook is not None:
                            try:
                                team_hook.set_enabled(True)
                                rearm = getattr(
                                    team_hook, "rearm_request_schedule", None
                                )
                                if callable(rearm):
                                    rearm(settle_seconds=0.05)
                            except Exception:
                                _put(
                                    output_queue,
                                    "diagnostic",
                                    {
                                        "component": "team_enable",
                                        "details": traceback.format_exc(),
                                    },
                                )
                        else:
                            try_install_team_hook()
                    elif team_hook is not None:
                        # Keep the existing trampoline available for a later
                        # team scene, but stop issuing requests immediately.
                        try:
                            team_hook.set_enabled(False)
                        except Exception:
                            _put(
                                output_queue,
                                "diagnostic",
                                {
                                    "component": "team_disable",
                                    "details": traceback.format_exc(),
                                },
                            )
                if (
                    current_team_stats_mode == TEAM_STATS_MODE_TEAM
                    and team_hook is None
                ):
                    try_install_team_hook()
                if native_hook is not None:
                    native_hook.set_target_boss_lookup_enabled(
                        target_boss_lookup_event.is_set()
                    )
                network_records = network_poller.drain_records()
                sequence_gaps = network_poller.drain_sequence_gaps()
                now = time.monotonic()
                team_response_health.observe_records(network_records, now)
                request_records: list[dict] = []
                if team_hook is not None:
                    try:
                        request_records = team_hook.poll_requests()
                    except Exception:
                        raise RuntimeError(
                            "outbound request capture became unreadable"
                        ) from None
                (
                    native_records,
                    native_error,
                    native_diagnostic,
                ) = collect_native_records_with_diagnostic(native_hook)
                team_response_health.observe_native_damage(
                    native_records.get("native_records", []), now
                )
                native_installed = native_hook is not None
                if native_error:
                    _put(
                        output_queue,
                        "diagnostic",
                        {
                            "component": "native_poll",
                            "details": native_error,
                        },
                    )
                    _close_hook(output_queue, "native", native_hook)
                    native_hook = None
                    native_installed = False

                team_status = None
                if now >= next_team_status_at:
                    if team_hook is not None:
                        attached_check = getattr(
                            team_hook, "is_attached", None
                        )
                        try:
                            team_attached = (
                                bool(attached_check())
                                if callable(attached_check)
                                else True
                            )
                        except Exception:
                            team_attached = False
                        if not team_attached:
                            _put(
                                output_queue,
                                "diagnostic",
                                {
                                    "component": "team_detached",
                                    "details": "request_entry_detached",
                                },
                            )
                            previous_team_hook = team_hook
                            if _close_hook(
                                output_queue, "team", previous_team_hook
                            ):
                                team_hook = None
                                team_response_health.mark_hook_missing()
                                next_team_install_at = 0.0
                                if (
                                    current_team_stats_mode
                                    == TEAM_STATS_MODE_TEAM
                                ):
                                    try_install_team_hook()
                    raw_team_status: dict[str, object] = {
                        "enabled": False,
                        "request_count": team_response_health.last_request_count,
                        "last_result": 0,
                        "last_request_filetime": 0,
                        "captured_request_count": 0,
                        "dropped_request_count": 0,
                    }
                    if team_hook is not None:
                        try:
                            raw_team_status = team_hook.status()
                        except Exception:
                            raise RuntimeError(
                                "team-stat request state became unreadable"
                            ) from None
                    recovery_action = team_response_health.assess(
                        now, raw_team_status
                    )
                    if recovery_action == "rearm" and team_hook is not None:
                        try:
                            if team_hook.rearm_request_schedule():
                                team_response_health.mark_rearmed(
                                    now, raw_team_status
                                )
                                _put(
                                    output_queue,
                                    "diagnostic",
                                    {
                                        "component": "team_rearm",
                                        "details": "response_timeout",
                                    },
                                )
                            else:
                                team_response_health.mark_recovery_failed()
                        except Exception:
                            team_response_health.mark_recovery_failed()
                            _put(
                                output_queue,
                                "diagnostic",
                                {
                                    "component": "team_rearm",
                                    "details": traceback.format_exc(),
                                },
                            )
                    elif (
                        recovery_action == "reinstall"
                        and team_hook is not None
                    ):
                        team_response_health.mark_reinstalled(now)
                        previous_team_hook = team_hook
                        if _close_hook(
                            output_queue, "team", previous_team_hook
                        ):
                            team_hook = None
                            team_reinstall_pending = True
                            next_team_install_at = 0.0
                            try_install_team_hook()
                            _put(
                                output_queue,
                                "diagnostic",
                                {
                                    "component": "team_reinstall",
                                    "details": "response_timeout",
                                },
                            )
                        else:
                            team_response_health.mark_recovery_failed()
                    if team_hook is not None:
                        try:
                            raw_team_status = team_hook.status()
                        except Exception:
                            raise RuntimeError(
                                "team-stat request state became unreadable"
                            ) from None
                    raw_team_status.update(
                        team_response_health.snapshot(now, raw_team_status)
                    )
                    team_status = raw_team_status
                    next_team_status_at = now + TEAM_STATUS_INTERVAL_SECONDS

                if _emit_batch(
                    output_queue,
                    session_id=session_id,
                    batch_id=batch_id,
                    game_pid=game_pid,
                    network_records=network_records,
                    request_records=request_records,
                    native_records=native_records,
                    native_damage_hook_installed=native_installed,
                    team_status=team_status,
                    team_stats_mode=current_team_stats_mode,
                    sequence_gaps=sequence_gaps,
                    native_diagnostic=(
                        native_diagnostic
                        if (
                            network_records
                            or request_records
                            or any(native_records.values())
                            or team_status is not None
                            or sequence_gaps
                        )
                        else None
                    ),
                ):
                    batch_id += 1

                network_error = network_poller.take_error()
                if network_error:
                    raise RuntimeError(network_error)
                if (
                    not network_records
                    and not request_records
                    and not any(native_records.values())
                ):
                    stop_event.wait(CAPTURE_IDLE_WAIT_SECONDS)

            if _should_stop(stop_event, watchdog, runtime_expiry):
                session_reason = (
                    "stopped"
                    if stop_event.is_set()
                    else (
                        "runtime_capability_expired"
                        if not _runtime_lease_active(runtime_expiry)
                        else "parent_exited"
                    )
                )
            elif not network_hook.alive:
                session_reason = "game_exited"
            else:
                network_error = network_poller.take_error()
                if network_error:
                    raise RuntimeError(network_error)
                session_reason = "capture_failed"
        except Exception:
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "capture_failed",
                    "component": "capture",
                    "details": traceback.format_exc(),
                },
            )
            session_reason = "capture_failed"
        finally:
            final_team_status = None
            final_request_records: list[dict] = []
            if team_hook is not None:
                try:
                    final_request_records = team_hook.poll_requests()
                except Exception:
                    _put(
                        output_queue,
                        "diagnostic",
                        {
                            "component": "team_request_poll",
                            "details": traceback.format_exc(),
                        },
                    )
                try:
                    final_team_status = team_hook.status()
                except Exception:
                    _put(
                        output_queue,
                        "diagnostic",
                        {
                            "component": "team_status",
                            "details": traceback.format_exc(),
                        },
                    )

            network_records = network_poller.drain_records()
            sequence_gaps = network_poller.drain_sequence_gaps()
            if native_hook is not None:
                native_hook.set_target_boss_lookup_enabled(
                    target_boss_lookup_event.is_set()
                )
            (
                native_records,
                native_error,
                native_diagnostic,
            ) = collect_native_records_with_diagnostic(native_hook)
            native_installed_during_final_poll = native_hook is not None
            if native_error:
                _put(
                    output_queue,
                    "diagnostic",
                    {
                        "component": "native_poll",
                        "details": native_error,
                    },
                )
            if connected and _emit_batch(
                output_queue,
                session_id=session_id,
                batch_id=batch_id,
                game_pid=game_pid,
                network_records=network_records,
                request_records=final_request_records,
                native_records=native_records,
                native_damage_hook_installed=native_installed_during_final_poll,
                team_status=final_team_status,
                team_stats_mode=current_team_stats_mode,
                sequence_gaps=sequence_gaps,
                native_diagnostic=native_diagnostic,
                force_diagnostic=True,
            ):
                batch_id += 1

            # Keep the network acknowledger alive while the other game hooks
            # are restored. This prevents cleanup from creating a sync timeout.
            team_cleanup_verified = _close_hook(
                output_queue, "team", team_hook
            )
            native_cleanup_verified = _close_hook(
                output_queue, "native", native_hook
            )
            acknowledger_stopped = network_poller.stop_and_join()
            trailing_network_records = network_poller.drain_records()
            trailing_gaps = network_poller.drain_sequence_gaps()
            if connected and _emit_batch(
                output_queue,
                session_id=session_id,
                batch_id=batch_id,
                game_pid=game_pid,
                network_records=trailing_network_records,
                request_records=[],
                native_records={
                    "native_records": [],
                    "native_boss_records": [],
                    "native_name_records": [],
                    "native_skill_name_records": [],
                },
                native_damage_hook_installed=native_installed_during_final_poll,
                team_stats_mode=current_team_stats_mode,
                sequence_gaps=trailing_gaps,
            ):
                batch_id += 1
            network_cleanup_verified = _close_hook(
                output_queue, "network", network_hook
            )
            cleanup_components = {
                "team": bool(team_cleanup_verified),
                "native": bool(native_cleanup_verified),
                "network": bool(network_cleanup_verified),
                "acknowledger": bool(acknowledger_stopped),
            }
            _put(
                output_queue,
                "session_closed",
                {
                    "session_id": session_id,
                    "game_pid": game_pid,
                    "reason": session_reason,
                    "network_hook_installed": False,
                    "native_damage_hook_installed": False,
                    "team_stats_hook_installed": False,
                    "hook_cleanup_verified": all(
                        cleanup_components.values()
                    ),
                    "hook_cleanup_components": cleanup_components,
                    "team_stats_mode": team_stats_mode_name(
                        current_team_stats_mode
                    ),
                    "damage_source": "none",
                },
            )

        if session_reason == "game_exited":
            _put(output_queue, "state", {"stage": "game_exited"})
        if not _should_stop(stop_event, watchdog, runtime_expiry):
            _interruptible_wait(
                stop_event,
                watchdog,
                CAPTURE_RETRY_WAIT_SECONDS,
                runtime_expiry,
            )

    if not _runtime_lease_active(runtime_expiry) and not stop_event.is_set():
        _put(
            output_queue,
            "runtime_capability_expired",
            {"stage": "runtime_capability_expired"},
        )


def capture_process_main(
    parent_pid: int,
    stop_event,
    output_queue,
    target_boss_lookup_event,
    runtime_capability,
    runtime_capability_queue,
    runtime_expiry,
    allow_development: bool = False,
    trusted_public_keys=None,
    team_stats_mode=None,
) -> None:
    """Multiprocessing spawn target. This module deliberately imports no UI."""
    watchdog = ParentProcessWatchdog(parent_pid)
    parent_alive = watchdog.is_alive()
    try:
        capability = RuntimeCapability.from_value(
            runtime_capability,
            trusted_public_keys=trusted_public_keys,
            allow_development=bool(allow_development),
        )
        if not _runtime_lease_active(runtime_expiry):
            raise RuntimeCapabilityError("runtime capability lease has expired")
        runtime_lease = RuntimeLeaseGate(
            capability,
            runtime_capability_queue,
            runtime_expiry,
            trusted_public_keys=trusted_public_keys,
            allow_development=bool(allow_development),
        )
        priority = configure_capture_priority()
        _put(
            output_queue,
            "process_started",
            {
                "pid": os.getpid(),
                "parent_pid": parent_pid,
                "runtime_profile_id": capability.profile_id,
                **priority,
            },
        )
        _capture_forever(
            stop_event,
            output_queue,
            watchdog,
            target_boss_lookup_event,
            capability.profile,
            runtime_lease,
            team_stats_mode,
        )
    except RuntimeCapabilityError as exc:
        _put(
            output_queue,
            "fatal",
            f"runtime capability rejected: {exc}",
        )
    except BaseException:
        _put(output_queue, "fatal", traceback.format_exc())
    finally:
        parent_alive = watchdog.is_alive()
        watchdog.close()
        try:
            _put(output_queue, "stopped", None)
        except Exception:
            parent_alive = False
        close_queue = getattr(output_queue, "close", None)
        if callable(close_queue):
            close_queue()
        if parent_alive:
            join_thread = getattr(output_queue, "join_thread", None)
            if callable(join_thread):
                join_thread()
        else:
            cancel_join = getattr(output_queue, "cancel_join_thread", None)
            if callable(cancel_join):
                cancel_join()


class CaptureProcessClient:
    """Parent-side lifecycle wrapper used by the UI's HookWorker thread."""

    def __init__(
        self,
        *,
        runtime_capability,
        allow_development: bool = False,
        trusted_public_keys=None,
        parent_pid: int | None = None,
        target_boss_lookup_enabled: bool = False,
        team_stats_mode: object = TEAM_STATS_MODE_TEAM,
    ):
        self.allow_development = bool(allow_development)
        self.trusted_public_keys = dict(trusted_public_keys or {})
        self.runtime_capability = RuntimeCapability.from_value(
            runtime_capability,
            trusted_public_keys=self.trusted_public_keys,
            allow_development=self.allow_development,
        )
        self.context = multiprocessing.get_context("spawn")
        self.stop_event = self.context.Event()
        self.target_boss_lookup_event = self.context.Event()
        if target_boss_lookup_enabled:
            self.target_boss_lookup_event.set()
        self.team_stats_mode_value = self.context.Value(
            "b", normalize_team_stats_mode(team_stats_mode)
        )
        self.runtime_expiry_value = self.context.Value(
            "d", self.runtime_capability.expires_at
        )
        self.runtime_capability_queue = self.context.Queue(maxsize=0)
        self.output_queue = self.context.Queue(maxsize=0)
        self.process = self.context.Process(
            name="GMZZCapture",
            target=capture_process_main,
            args=(
                int(parent_pid or os.getpid()),
                self.stop_event,
                self.output_queue,
                self.target_boss_lookup_event,
                self.runtime_capability.to_wire(),
                self.runtime_capability_queue,
                self.runtime_expiry_value,
                self.allow_development,
                self.trusted_public_keys,
                self.team_stats_mode_value,
            ),
            daemon=False,
        )
        self._started = False
        self._closed = False

    @property
    def pid(self) -> int:
        return int(self.process.pid or 0)

    def start(self) -> None:
        if getattr(self, "_started", False):
            raise RuntimeError("capture process has already been started")
        if getattr(self, "_closed", False):
            raise RuntimeError("capture process has already been closed")
        RuntimeCapability.from_value(
            self.runtime_capability,
            expected_session_id=self.runtime_capability.session_id,
            expected_client_id=self.runtime_capability.client_id,
            expected_build_id=self.runtime_capability.build_id,
            expected_client_build=self.runtime_capability.client_build,
            trusted_public_keys=self.trusted_public_keys,
            allow_development=self.allow_development,
        )
        self.process.start()
        self._started = True

    def refresh_runtime_capability(self, value) -> float:
        """Extend the child lease only for the same authorized runtime profile."""
        next_capability = RuntimeCapability.from_value(
            value,
            expected_session_id=self.runtime_capability.session_id,
            expected_client_id=self.runtime_capability.client_id,
            expected_build_id=self.runtime_capability.build_id,
            expected_client_build=self.runtime_capability.client_build,
            trusted_public_keys=self.trusted_public_keys,
            allow_development=self.allow_development,
        )
        if not self.runtime_capability.same_binding(next_capability):
            raise RuntimeCapabilityError(
                "runtime capability binding or profile changed"
            )
        if (
            next_capability.lease_sequence
            <= self.runtime_capability.lease_sequence
            or next_capability.issued_at < self.runtime_capability.issued_at
        ):
            self.revoke_runtime_capability()
            raise RuntimeCapabilityError(
                "stale or replayed runtime capability was rejected"
            )
        try:
            self.runtime_capability_queue.put(next_capability.to_wire())
        except (OSError, ValueError) as exc:
            self.revoke_runtime_capability()
            raise RuntimeCapabilityError(
                "runtime capability renewal could not reach capture process"
            ) from exc
        self.runtime_capability = next_capability
        return next_capability.expires_at

    def revoke_runtime_capability(self) -> None:
        """Close the lease gate immediately and tell the child to clean up."""
        self.runtime_expiry_value.value = 0.0
        self.request_stop()

    def get(self, timeout: float | None = None):
        return self.output_queue.get(timeout=timeout)

    def set_target_boss_lookup_enabled(self, enabled: bool) -> None:
        if enabled:
            self.target_boss_lookup_event.set()
        else:
            self.target_boss_lookup_event.clear()

    def set_team_stats_mode(self, mode: object) -> str:
        """Switch the child-side team request gate without touching game data."""
        normalized = normalize_team_stats_mode(mode, default=TEAM_STATS_MODE_UNKNOWN)
        self.team_stats_mode_value.value = normalized
        return team_stats_mode_name(normalized)

    def get_team_stats_mode(self) -> str:
        return team_stats_mode_name(self.team_stats_mode_value.value)

    def request_stop(self) -> None:
        self.stop_event.set()

    def is_alive(self) -> bool:
        if not getattr(self, "_started", False) or getattr(
            self, "_closed", False
        ):
            return False
        try:
            return self.process.is_alive()
        except (AssertionError, ValueError):
            return False

    def join(self, timeout: float | None = None) -> None:
        if not getattr(self, "_started", False) or getattr(
            self, "_closed", False
        ):
            return
        try:
            self.process.join(timeout)
        except (AssertionError, ValueError):
            return

    def terminate(self) -> None:
        if self.is_alive():
            try:
                self.process.terminate()
            except (AssertionError, ValueError):
                return

    def close(self, *, wait_for_queue: bool = True) -> None:
        """Release parent-side IPC resources after the child has exited.

        Normal application shutdown waits for a local queue feeder, preserving
        the existing lossless behavior. Short-lived consumers such as the
        diagnostic tool can opt out: they only read this queue, and waiting for
        multiprocessing's feeder finalizer can otherwise stall the transition
        from capture cleanup to report upload on some Windows systems.
        """
        if getattr(self, "_closed", False):
            return
        try:
            renewal_queue = getattr(self, "runtime_capability_queue", None)
            if renewal_queue is not None:
                cancel_renewal_join = getattr(
                    renewal_queue, "cancel_join_thread", None
                )
                if callable(cancel_renewal_join):
                    cancel_renewal_join()
                renewal_queue.close()
            if not wait_for_queue:
                cancel_join = getattr(self.output_queue, "cancel_join_thread", None)
                if callable(cancel_join):
                    cancel_join()
            self.output_queue.close()
            if wait_for_queue:
                self.output_queue.join_thread()
        finally:
            close_process = getattr(self.process, "close", None)
            if callable(close_process) and not self.is_alive():
                try:
                    close_process()
                except (AssertionError, ValueError):
                    pass
            self._closed = True
