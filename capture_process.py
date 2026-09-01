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
    should_decode_network_arguments,
    should_retain_network_record,
)
from team_stats_request_hook import TeamStatsRequestHook


CAPTURE_IDLE_WAIT_SECONDS = 0.001
CAPTURE_RETRY_WAIT_SECONDS = 0.5
GAME_SEARCH_WAIT_SECONDS = 1.0
TEAM_STATUS_INTERVAL_SECONDS = 1.0

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


def _should_stop(stop_event, watchdog: ParentProcessWatchdog) -> bool:
    return bool(stop_event.is_set() or not watchdog.is_alive())


def _interruptible_wait(
    stop_event,
    watchdog: ParentProcessWatchdog,
    seconds: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not _should_stop(stop_event, watchdog):
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
        "boss_ring_header_reads",
        "boss_ring_header_failures",
        "boss_ring_records_polled",
        "boss_ring_parse_failures",
        "boss_ring_overruns",
        "target_lookup_candidates",
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


def _close_hook(output_queue, component: str, hook) -> None:
    if hook is None:
        return
    try:
        hook.close()
    except Exception:
        _put(
            output_queue,
            "cleanup_error",
            {"component": component, "details": traceback.format_exc()},
        )


def _emit_batch(
    output_queue,
    *,
    session_id: int,
    batch_id: int,
    game_pid: int,
    network_records: list[dict],
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
    team_stats_mode=None,
) -> None:
    session_id = 0
    while not _should_stop(stop_event, watchdog):
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
        network_hook = NetworkMessageHook()
        try:
            network_hook.install()
        except RuntimeError as exc:
            _close_hook(output_queue, "network", network_hook)
            if "process not found" in str(exc):
                _put(output_queue, "state", {"stage": "game_not_found"})
                _interruptible_wait(
                    stop_event, watchdog, GAME_SEARCH_WAIT_SECONDS
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
            _interruptible_wait(stop_event, watchdog, 3.0)
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
            _interruptible_wait(stop_event, watchdog, 2.0)
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
        current_team_stats_mode = read_team_stats_mode(team_stats_mode)
        network_poller.start()
        try:
            def try_install_team_hook() -> None:
                """Install only after the parent has classified a team scene."""
                nonlocal team_hook, next_team_install_at
                if team_hook is not None:
                    return
                now = time.monotonic()
                if now < next_team_install_at:
                    return
                next_team_install_at = now + 2.0
                try:
                    team_hook = TeamStatsRequestHook(
                        pid=game_pid,
                        interval=1.0,
                    ).install()
                except Exception:
                    team_hook = None
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
                    pid=game_pid,
                    capture_names=True,
                    capture_boss_types=True,
                    target_boss_lookup_enabled=(
                        target_boss_lookup_event.is_set()
                    ),
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
                        pid=game_pid,
                        capture_names=False,
                        capture_boss_types=True,
                        target_boss_lookup_enabled=(
                            target_boss_lookup_event.is_set()
                        ),
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
                    "team_stats_hook_installed": team_hook is not None,
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
                requested_team_stats_mode = read_team_stats_mode(
                    team_stats_mode
                )
                if requested_team_stats_mode != current_team_stats_mode:
                    current_team_stats_mode = requested_team_stats_mode
                    if current_team_stats_mode == TEAM_STATS_MODE_TEAM:
                        if team_hook is not None:
                            try:
                                team_hook.set_enabled(True)
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
                (
                    native_records,
                    native_error,
                    native_diagnostic,
                ) = collect_native_records_with_diagnostic(native_hook)
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

                now = time.monotonic()
                team_status = None
                if team_hook is not None and now >= next_team_status_at:
                    try:
                        team_status = team_hook.status()
                    except Exception:
                        raise RuntimeError(
                            "team-stat request state became unreadable"
                        ) from None
                    next_team_status_at = now + TEAM_STATUS_INTERVAL_SECONDS

                if _emit_batch(
                    output_queue,
                    session_id=session_id,
                    batch_id=batch_id,
                    game_pid=game_pid,
                    network_records=network_records,
                    native_records=native_records,
                    native_damage_hook_installed=native_installed,
                    team_status=team_status,
                    team_stats_mode=current_team_stats_mode,
                    sequence_gaps=sequence_gaps,
                    native_diagnostic=(
                        native_diagnostic
                        if (
                            network_records
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
                if not network_records and not any(native_records.values()):
                    stop_event.wait(CAPTURE_IDLE_WAIT_SECONDS)

            if _should_stop(stop_event, watchdog):
                session_reason = (
                    "stopped" if stop_event.is_set() else "parent_exited"
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
            if team_hook is not None:
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
            _close_hook(output_queue, "team", team_hook)
            _close_hook(output_queue, "native", native_hook)
            network_poller.stop_and_join()
            trailing_network_records = network_poller.drain_records()
            trailing_gaps = network_poller.drain_sequence_gaps()
            if connected and _emit_batch(
                output_queue,
                session_id=session_id,
                batch_id=batch_id,
                game_pid=game_pid,
                network_records=trailing_network_records,
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
            _close_hook(output_queue, "network", network_hook)
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
                    "team_stats_mode": team_stats_mode_name(
                        current_team_stats_mode
                    ),
                    "damage_source": "none",
                },
            )

        if session_reason == "game_exited":
            _put(output_queue, "state", {"stage": "game_exited"})
        if not _should_stop(stop_event, watchdog):
            _interruptible_wait(
                stop_event, watchdog, CAPTURE_RETRY_WAIT_SECONDS
            )


def capture_process_main(
    parent_pid: int,
    stop_event,
    output_queue,
    target_boss_lookup_event,
    team_stats_mode=None,
) -> None:
    """Multiprocessing spawn target. This module deliberately imports no UI."""
    watchdog = ParentProcessWatchdog(parent_pid)
    parent_alive = watchdog.is_alive()
    try:
        priority = configure_capture_priority()
        _put(
            output_queue,
            "process_started",
            {"pid": os.getpid(), "parent_pid": parent_pid, **priority},
        )
        _capture_forever(
            stop_event,
            output_queue,
            watchdog,
            target_boss_lookup_event,
            team_stats_mode,
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
        parent_pid: int | None = None,
        target_boss_lookup_enabled: bool = False,
        team_stats_mode: object = TEAM_STATS_MODE_TEAM,
    ):
        self.context = multiprocessing.get_context("spawn")
        self.stop_event = self.context.Event()
        self.target_boss_lookup_event = self.context.Event()
        if target_boss_lookup_enabled:
            self.target_boss_lookup_event.set()
        self.team_stats_mode_value = self.context.Value(
            "b", normalize_team_stats_mode(team_stats_mode)
        )
        self.output_queue = self.context.Queue(maxsize=0)
        self.process = self.context.Process(
            name="GMZZCapture",
            target=capture_process_main,
            args=(
                int(parent_pid or os.getpid()),
                self.stop_event,
                self.output_queue,
                self.target_boss_lookup_event,
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
        self.process.start()
        self._started = True

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
