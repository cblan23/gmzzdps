#!/usr/bin/env python3
"""Passive Npcap capture backend used by the isolated v0.2.2n build.

The backend receives encrypted UDP/KCP traffic from Npcap and decodes only
inbound server PUSH data.  It never imports a hook module, injects code,
modifies game memory, calls a game function, or transmits a packet.  Npcap
cannot expose the per-session RC4 plaintext state, so the one non-Npcap input
is a query/read-only snapshot of that state from the game process.
"""

from __future__ import annotations

import argparse
import ctypes
import multiprocessing
import os
import queue
import time
import traceback
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Iterable

import npcap_shadow_capture as shadow_capture
from npcap_key_state import Rc4Anchor, Rc4StateReader, locate_readers
from npcap_protocol import NpcapProtocolDecoder, application_frame_valid
from npcap_rc4_decode import Rc4State
from runtime_capability import RuntimeCapability, RuntimeCapabilityError


CAPTURE_RETRY_WAIT_SECONDS = 0.5
GAME_SEARCH_WAIT_SECONDS = 1.0
ENDPOINT_REFRESH_SECONDS = 1.0
PROCESS_EXIT_GRACE_SECONDS = 5.0
PCAP_READ_TIMEOUT_MS = 50
PCAP_BUFFER_BYTES = 32 * 1024 * 1024
BATCH_INTERVAL_SECONDS = 0.02
BATCH_RECORD_LIMIT = 256
ALIGNMENT_VALID_PUSHES = 3
ALIGNMENT_RETRY_SECONDS = 1.0
KCP_GAP_WAIT_SECONDS = 1.25
FLOW_SWITCH_WAIT_SECONDS = 2.0
MAX_FLOW_SEGMENTS = 16_384
MAX_FLOWS = 16

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

SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_int
    _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    _kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.SetPriorityClass.restype = ctypes.c_int
else:
    _kernel32 = None


def normalize_team_stats_mode(
    value: object, *, default: int = TEAM_STATS_MODE_TEAM
) -> int:
    if isinstance(value, str):
        code = TEAM_STATS_MODE_CODES.get(value.strip().casefold())
        return int(default if code is None else code)
    try:
        code = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return int(default)
    return code if code in TEAM_STATS_MODE_NAMES else int(default)


def team_stats_mode_name(value: object) -> str:
    return TEAM_STATS_MODE_NAMES.get(
        normalize_team_stats_mode(value),
        TEAM_STATS_MODE_NAMES[TEAM_STATS_MODE_TEAM],
    )


def _read_team_stats_mode(shared_value: object) -> int:
    try:
        value = shared_value.value
    except (AttributeError, OSError, ValueError):
        value = shared_value
    return normalize_team_stats_mode(value, default=TEAM_STATS_MODE_UNKNOWN)


def _put(output_queue, kind: str, payload=None) -> None:
    output_queue.put((kind, payload))


def configure_capture_priority() -> dict[str, object]:
    if _kernel32 is None:
        return {"priority_class": "default", "priority_applied": False}
    applied = bool(
        _kernel32.SetPriorityClass(
            _kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS
        )
    )
    return {
        "priority_class": "above_normal" if applied else "default",
        "priority_applied": applied,
        "main_thread_priority_applied": False,
    }


class ParentProcessWatchdog:
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
            return bool(
                self.handle
                and _kernel32.WaitForSingleObject(
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


class RuntimeLeaseGate:
    """Validate every parent-provided lease renewal inside the child."""

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


def _runtime_active(runtime_expiry=None) -> bool:
    if runtime_expiry is None:
        return True
    try:
        return float(runtime_expiry.value) > time.time()
    except (AttributeError, TypeError, ValueError, OSError):
        return False


def _should_stop(stop_event, watchdog: ParentProcessWatchdog, runtime_expiry) -> bool:
    return bool(
        stop_event.is_set()
        or not watchdog.is_alive()
        or not _runtime_active(runtime_expiry)
    )


def _interruptible_wait(
    stop_event,
    watchdog: ParentProcessWatchdog,
    seconds: float,
    runtime_expiry,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not _should_stop(stop_event, watchdog, runtime_expiry):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        stop_event.wait(min(0.05, remaining))
    return True


@dataclass(frozen=True)
class PushSegment:
    flow: tuple[int, str, int, int]
    sequence: int
    timestamp_epoch: float
    ciphertext: bytes


@dataclass(frozen=True)
class DecryptedPush:
    flow: tuple[int, str, int, int]
    sequence: int
    timestamp_epoch: float
    plaintext: bytes


class PassiveRc4Reassembler:
    """Align one RC4 snapshot and advance it once per unique KCP PUSH.

    KCP retransmissions are retained only as evidence and never advance RC4.
    A sequence hole pauses output.  The caller may install a fresh read-only
    snapshot after the wait threshold and resume from a later contiguous run.
    """

    def __init__(
        self,
        *,
        required_validation: int = ALIGNMENT_VALID_PUSHES,
        gap_wait_seconds: float = KCP_GAP_WAIT_SECONDS,
        flow_switch_wait_seconds: float = FLOW_SWITCH_WAIT_SECONDS,
        max_flow_segments: int = MAX_FLOW_SEGMENTS,
        max_flows: int = MAX_FLOWS,
    ) -> None:
        self.required_validation = max(2, int(required_validation))
        self.gap_wait_seconds = max(0.0, float(gap_wait_seconds))
        self.flow_switch_wait_seconds = max(
            self.gap_wait_seconds, float(flow_switch_wait_seconds)
        )
        self.max_flow_segments = max(64, int(max_flow_segments))
        self.max_flows = max(1, int(max_flows))
        self.flows: OrderedDict[
            tuple[int, str, int, int], dict[int, PushSegment]
        ] = OrderedDict()
        self.last_seen_monotonic: dict[tuple[int, str, int, int], float] = {}
        self.last_delivered: dict[tuple[int, str, int, int], int] = {}
        self.anchor: Rc4Anchor | None = None
        self.active_flow: tuple[int, str, int, int] | None = None
        self.state: Rc4State | None = None
        self.next_sequence: int | None = None
        self.gap_started_monotonic: float | None = None
        self.alignment_started_monotonic: float | None = None
        self.counters: Counter[str] = Counter()

    @property
    def aligned(self) -> bool:
        return bool(
            self.active_flow is not None
            and self.state is not None
            and self.next_sequence is not None
        )

    def install_anchor(self, anchor: Rc4Anchor, now: float | None = None) -> None:
        self.anchor = anchor
        self.active_flow = None
        self.state = None
        self.next_sequence = None
        self.gap_started_monotonic = None
        self.alignment_started_monotonic = (
            time.monotonic() if now is None else float(now)
        )
        self.counters["rc4_anchors"] += 1

    def invalidate(self) -> None:
        if self.aligned:
            self.counters["stream_invalidations"] += 1
        self.anchor = None
        self.active_flow = None
        self.state = None
        self.next_sequence = None
        self.gap_started_monotonic = None
        self.alignment_started_monotonic = None

    def _flow_segments(
        self, flow: tuple[int, str, int, int]
    ) -> dict[int, PushSegment]:
        values = self.flows.get(flow)
        if values is None:
            values = {}
            self.flows[flow] = values
            while len(self.flows) > self.max_flows:
                stale, _discarded = self.flows.popitem(last=False)
                self.last_seen_monotonic.pop(stale, None)
                self.last_delivered.pop(stale, None)
        else:
            self.flows.move_to_end(flow)
        return values

    def _prune(self, flow: tuple[int, str, int, int]) -> None:
        values = self.flows.get(flow)
        if not values or len(values) <= self.max_flow_segments:
            return
        keep_from = self.last_delivered.get(flow, -1) - 8
        for sequence in sorted(values):
            if len(values) <= self.max_flow_segments:
                break
            if sequence <= keep_from or sequence == min(values):
                values.pop(sequence, None)
        while len(values) > self.max_flow_segments:
            values.pop(min(values), None)
        self.counters["buffer_pruned_pushes"] += 1

    def add(
        self, segment: PushSegment, now: float | None = None
    ) -> list[DecryptedPush]:
        monotonic_now = time.monotonic() if now is None else float(now)
        values = self._flow_segments(segment.flow)
        existing = values.get(int(segment.sequence))
        if existing is not None:
            if existing.ciphertext == segment.ciphertext:
                self.counters["kcp_retransmissions"] += 1
            else:
                self.counters["kcp_sequence_conflicts"] += 1
                if self.active_flow == segment.flow:
                    self.invalidate()
            return []
        values[int(segment.sequence)] = segment
        self.last_seen_monotonic[segment.flow] = monotonic_now
        self.counters["unique_pushes"] += 1
        self._prune(segment.flow)

        if self.aligned and segment.flow == self.active_flow:
            return self._drain()
        if not self.aligned and self.anchor is not None:
            return self._try_align()
        return []

    def _candidate_boundaries(
        self, values: dict[int, PushSegment], anchor_epoch: float
    ) -> list[int]:
        nearby = sorted(
            values,
            key=lambda sequence: abs(
                values[sequence].timestamp_epoch - anchor_epoch
            ),
        )[:32]
        return sorted(set(nearby + [sequence - 1 for sequence in nearby]))

    def _score_boundary(
        self,
        values: dict[int, PushSegment],
        boundary: int,
        state: Rc4State,
        anchor_epoch: float,
    ) -> tuple[int, float] | None:
        trial = state.clone()
        valid = 0
        for sequence in range(boundary + 1, boundary + 9):
            segment = values.get(sequence)
            if segment is None:
                break
            plaintext = trial.forward(segment.ciphertext)
            if not application_frame_valid(plaintext):
                break
            valid += 1
        if valid < self.required_validation:
            return None
        reference = values.get(boundary) or values.get(boundary + 1)
        if reference is None:
            return None
        return valid, -abs(reference.timestamp_epoch - anchor_epoch)

    def _try_align(self) -> list[DecryptedPush]:
        anchor = self.anchor
        if anchor is None:
            return []
        scored: list[
            tuple[int, float, float, tuple[int, str, int, int], int]
        ] = []
        for flow, values in self.flows.items():
            delivered = self.last_delivered.get(flow, -1)
            for boundary in self._candidate_boundaries(
                values, anchor.midpoint_epoch
            ):
                if boundary < delivered:
                    continue
                score = self._score_boundary(
                    values,
                    boundary,
                    anchor.decrypt,
                    anchor.midpoint_epoch,
                )
                if score is None:
                    continue
                valid, negative_distance = score
                recency = self.last_seen_monotonic.get(flow, 0.0)
                scored.append(
                    (valid, negative_distance, recency, flow, boundary)
                )
        if not scored:
            return []

        _valid, _distance, _recency, flow, boundary = max(scored)
        values = self.flows[flow]
        delivered = self.last_delivered.get(flow, -1)
        decoded: dict[int, bytes] = {}

        backward = anchor.decrypt.clone()
        sequence = boundary
        while sequence in values and sequence > delivered:
            plaintext = backward.backward(values[sequence].ciphertext)
            if not application_frame_valid(plaintext):
                break
            decoded[sequence] = plaintext
            sequence -= 1

        forward = anchor.decrypt.clone()
        sequence = boundary + 1
        while sequence in values:
            trial = forward.clone()
            plaintext = trial.forward(values[sequence].ciphertext)
            # Alignment already proved the first run.  Every observed game
            # PUSH in validated sessions is one complete Doraemon frame; an
            # invalid later PUSH means the RC4 object/connection changed.
            if not application_frame_valid(plaintext):
                self.counters["plaintext_validation_failures"] += 1
                break
            forward = trial
            decoded[sequence] = plaintext
            sequence += 1

        if len([item for item in decoded if item > boundary]) < self.required_validation:
            return []

        self.active_flow = flow
        self.state = forward
        self.next_sequence = sequence
        self.gap_started_monotonic = None
        self.counters["rc4_alignments"] += 1
        output = [
            DecryptedPush(
                flow=flow,
                sequence=item,
                timestamp_epoch=values[item].timestamp_epoch,
                plaintext=decoded[item],
            )
            for item in sorted(decoded)
            if item > delivered
        ]
        if output:
            self.last_delivered[flow] = output[-1].sequence
        return output

    def _drain(self) -> list[DecryptedPush]:
        if not self.aligned:
            return []
        assert self.active_flow is not None
        assert self.state is not None
        assert self.next_sequence is not None
        values = self.flows[self.active_flow]
        result: list[DecryptedPush] = []
        while self.next_sequence in values:
            segment = values[self.next_sequence]
            plaintext = self.state.forward(segment.ciphertext)
            if not application_frame_valid(plaintext):
                self.counters["plaintext_validation_failures"] += 1
                self.invalidate()
                break
            result.append(
                DecryptedPush(
                    flow=segment.flow,
                    sequence=segment.sequence,
                    timestamp_epoch=segment.timestamp_epoch,
                    plaintext=plaintext,
                )
            )
            self.last_delivered[segment.flow] = segment.sequence
            self.next_sequence += 1
            self.gap_started_monotonic = None
        return result

    def pending_gap(self, now: float | None = None) -> dict | None:
        if not self.aligned:
            return None
        assert self.active_flow is not None
        assert self.next_sequence is not None
        values = self.flows.get(self.active_flow, {})
        later = [sequence for sequence in values if sequence > self.next_sequence]
        monotonic_now = time.monotonic() if now is None else float(now)
        if not later:
            self.gap_started_monotonic = None
            return None
        if self.gap_started_monotonic is None:
            self.gap_started_monotonic = monotonic_now
        actual = min(later)
        return {
            "expected": self.next_sequence,
            "actual": actual,
            "age_seconds": max(
                0.0, monotonic_now - self.gap_started_monotonic
            ),
            "flow": self.active_flow,
        }

    def needs_resync(self, now: float | None = None) -> bool:
        monotonic_now = time.monotonic() if now is None else float(now)
        gap = self.pending_gap(monotonic_now)
        if gap is not None and gap["age_seconds"] >= self.gap_wait_seconds:
            return True
        if not self.aligned or self.active_flow is None:
            return False
        last_active = self.last_seen_monotonic.get(self.active_flow, 0.0)
        if monotonic_now - last_active < self.flow_switch_wait_seconds:
            return False
        for flow, values in self.flows.items():
            if flow == self.active_flow:
                continue
            if (
                monotonic_now - self.last_seen_monotonic.get(flow, 0.0)
                <= self.flow_switch_wait_seconds
                and len(values) >= self.required_validation
            ):
                return True
        return False

    def alignment_stale(self, now: float | None = None) -> bool:
        if self.aligned or self.anchor is None:
            return False
        monotonic_now = time.monotonic() if now is None else float(now)
        started = self.alignment_started_monotonic or monotonic_now
        return monotonic_now - started >= ALIGNMENT_RETRY_SECONDS


class PcapStat(ctypes.Structure):
    _fields_ = [
        ("received", ctypes.c_uint),
        ("dropped", ctypes.c_uint),
        ("interface_dropped", ctypes.c_uint),
        ("captured", ctypes.c_uint),
    ]


def _configure_pcap(wpcap, handle) -> None:
    try:
        wpcap.pcap_stats.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PcapStat),
        ]
        wpcap.pcap_stats.restype = ctypes.c_int
    except (AttributeError, TypeError):
        pass
    try:
        wpcap.pcap_setbuff.argtypes = [ctypes.c_void_p, ctypes.c_int]
        wpcap.pcap_setbuff.restype = ctypes.c_int
        wpcap.pcap_setbuff(handle, PCAP_BUFFER_BYTES)
    except (AttributeError, TypeError, OSError):
        pass


def _pcap_stats(wpcap, handle) -> dict[str, object]:
    try:
        stats = PcapStat()
        if wpcap.pcap_stats(handle, ctypes.byref(stats)) != 0:
            return {"available": False}
        return {
            "available": True,
            "received": int(stats.received),
            "dropped": int(stats.dropped),
            "interface_dropped": int(stats.interface_dropped),
            "captured": int(stats.captured),
        }
    except (AttributeError, TypeError, OSError):
        return {"available": False}


def _install_bpf(wpcap, handle, expression: str) -> None:
    program = shadow_capture.BpfProgram()
    if wpcap.pcap_compile(
        handle,
        ctypes.byref(program),
        expression.encode("ascii"),
        1,
        0xFFFFFFFF,
    ) != 0:
        raise RuntimeError(
            shadow_capture._decode_native(wpcap.pcap_geterr(handle))
        )
    try:
        if wpcap.pcap_setfilter(handle, ctypes.byref(program)) != 0:
            raise RuntimeError(
                shadow_capture._decode_native(wpcap.pcap_geterr(handle))
            )
    finally:
        wpcap.pcap_freecode(ctypes.byref(program))


def _select_capture_adapter(
    wpcap, endpoints: Iterable[shadow_capture.UdpEndpoint]
) -> tuple[shadow_capture.Adapter, str]:
    adapters = shadow_capture.list_adapters(wpcap)
    addresses = []
    for endpoint in endpoints:
        value = str(endpoint.local_address or "").strip()
        if value and value != "0.0.0.0" and value not in addresses:
            addresses.append(value)
    for address in addresses:
        try:
            return shadow_capture.select_adapter(adapters, "", address)
        except RuntimeError:
            continue
    return shadow_capture.select_adapter(adapters, "", "")


def _team_status(
    team_stats_mode: object,
    counters: Counter[str],
    last_response_filetime: int,
    data_incomplete: bool,
) -> dict[str, object]:
    return {
        "installed": False,
        "enabled": False,
        "adopted": False,
        "request_count": 0,
        "last_result": 0,
        "last_request_filetime": 0,
        "captured_request_count": 0,
        "dropped_request_count": 0,
        "response_health": "passive_npcap",
        "data_incomplete": bool(data_incomplete),
        "response_count": int(counters.get("team_stats_responses", 0)),
        "last_response_filetime": int(last_response_filetime),
        "last_response_age_seconds": None,
        "last_combat_activity_age_seconds": None,
        "requests_since_response": 0,
        "rearm_count": 0,
        "reinstall_count": 0,
        "mode": team_stats_mode_name(_read_team_stats_mode(team_stats_mode)),
    }


def _emit_batch(
    output_queue,
    *,
    session_id: int,
    batch_id: int,
    game_pid: int,
    records: list[dict],
    gaps: list[dict],
    counters: Counter[str],
    pcap_diagnostic: dict[str, object],
    team_stats_mode: object,
    last_response_filetime: int,
    data_incomplete: bool,
) -> bool:
    if not records and not gaps:
        return False
    _put(
        output_queue,
        "batch",
        {
            "session_id": int(session_id),
            "batch_id": int(batch_id),
            "game_pid": int(game_pid),
            "captured_monotonic": time.monotonic(),
            "records": records,
            "request_records": [],
            "native_records": [],
            "native_boss_records": [],
            "native_name_records": [],
            "native_skill_name_records": [],
            "native_damage_hook_installed": False,
            "team_status": _team_status(
                team_stats_mode,
                counters,
                last_response_filetime,
                data_incomplete,
            ),
            "team_stats_mode": team_stats_mode_name(
                _read_team_stats_mode(team_stats_mode)
            ),
            "sequence_gaps": gaps,
            "native_diagnostic": {
                "capture_backend": "npcap",
                "passive_only": True,
                "game_process_access": "query_and_read_only",
                "server_requests_added": 0,
                "counters": dict(counters),
                "pcap": dict(pcap_diagnostic),
            },
            "damage_source": "npcap",
            "capture_backend": "npcap",
        },
    )
    return True


def _locate_reader(pid: int) -> tuple[Rc4StateReader | None, dict[str, object]]:
    readers, diagnostics = locate_readers(pid)
    reader = readers[0] if readers else None
    for extra in readers[1:]:
        extra.close()
    return reader, {
        "regions_read": diagnostics.regions_read,
        "bytes_read": diagnostics.bytes_read,
        "pointer_hits": diagnostics.pointer_hits,
        "elapsed_seconds": diagnostics.elapsed_seconds,
    }


def _session(
    stop_event,
    output_queue,
    watchdog: ParentProcessWatchdog,
    runtime_expiry,
    team_stats_mode,
    wpcap,
    session_id: int,
) -> str:
    discovery = argparse.Namespace(pid=None, process_name="C7-Win64-Shipping.exe")
    try:
        pid, executable, endpoints = shadow_capture.discover_game(discovery)
    except RuntimeError as exc:
        if "No running" in str(exc):
            return "game_not_found"
        raise

    adapter, local_ip = _select_capture_adapter(wpcap, endpoints)
    ports = sorted({int(row.local_port) for row in endpoints if row.local_port})
    bpf = shadow_capture.make_bpf(local_ip, ports)
    errbuf = ctypes.create_string_buffer(shadow_capture.ERRBUF_SIZE)
    handle = wpcap.pcap_open_live(
        adapter.name.encode(), 65535, 0, PCAP_READ_TIMEOUT_MS, errbuf
    )
    if not handle:
        raise RuntimeError(
            f"pcap_open_live failed: {shadow_capture._decode_native(errbuf.value)}"
        )

    reader: Rc4StateReader | None = None
    try:
        _configure_pcap(wpcap, handle)
        _install_bpf(wpcap, handle, bpf)
        datalink = int(wpcap.pcap_datalink(handle))
        _put(
            output_queue,
            "state",
            {
                "stage": "npcap_locating_rc4",
                "process_found": True,
                "game_pid": pid,
                "capture_backend": "npcap",
                "npcap_capture_active": True,
            },
        )
        reader, locate_diagnostic = _locate_reader(pid)
        if reader is None:
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "npcap_rc4_unavailable",
                    "details": "未能读取当前连接的解密状态，将自动重试。",
                    "capture_backend": "npcap",
                    **locate_diagnostic,
                },
            )
            return "retry"

        reassembler = PassiveRc4Reassembler()
        decoder = NpcapProtocolDecoder()
        reassembler.install_anchor(reader.snapshot())
        counters: Counter[str] = Counter()
        counters["server_requests_added"] = 0
        pending_records: list[dict] = []
        pending_gaps: list[dict] = []
        batch_id = 0
        next_flush = time.monotonic() + BATCH_INTERVAL_SECONDS
        next_endpoint_refresh = time.monotonic() + ENDPOINT_REFRESH_SECONDS
        next_anchor_retry = time.monotonic() + ALIGNMENT_RETRY_SECONDS
        process_missing_since: float | None = None
        data_incomplete = False
        last_response_filetime = 0
        last_pcap_dropped = 0
        pcap_diagnostic: dict[str, object] = {"available": False}
        next_pcap_stats = time.monotonic()

        _put(
            output_queue,
            "connected",
            {
                "session_id": session_id,
                "game_pid": pid,
                "game_path": executable,
                "network_hook_adopted": False,
                "native_damage_hook_installed": False,
                "native_damage_hook_adopted": False,
                "team_stats_hook_installed": False,
                "team_stats_hook_adopted": False,
                "team_stats_mode": team_stats_mode_name(
                    _read_team_stats_mode(team_stats_mode)
                ),
                "damage_source": "npcap",
                "capture_backend": "npcap",
                "npcap_capture_active": True,
                "npcap_adapter": adapter.description or adapter.name,
                "npcap_local_ip": local_ip,
                "npcap_game_ports": ports,
                "rc4_read_mode": "query_and_read_only",
                "server_requests_added": 0,
                "rc4_locator": locate_diagnostic,
            },
        )

        while not _should_stop(stop_event, watchdog, runtime_expiry):
            now = time.monotonic()
            header_ptr = ctypes.POINTER(shadow_capture.PcapPacketHeader)()
            data_ptr = ctypes.POINTER(ctypes.c_ubyte)()
            status = wpcap.pcap_next_ex(
                handle, ctypes.byref(header_ptr), ctypes.byref(data_ptr)
            )
            if status == -2:
                return "capture_closed"
            if status < 0:
                raise RuntimeError(
                    shadow_capture._decode_native(wpcap.pcap_geterr(handle))
                )
            if status > 0:
                header = header_ptr.contents
                raw = ctypes.string_at(data_ptr, header.caplen)
                udp = shadow_capture.parse_udp_packet(raw, datalink)
                if udp is None:
                    counters["unparsed_packets"] += 1
                else:
                    (
                        _local_host,
                        local_port,
                        remote_ip,
                        remote_port,
                        direction,
                    ) = shadow_capture_endpoint_parts(udp, local_ip)
                    if direction == "inbound" and local_port in ports:
                        timestamp_epoch = (
                            float(header.ts.tv_sec)
                            + float(header.ts.tv_usec) / 1_000_000.0
                        )
                        segments = shadow_capture.parse_kcp_segments(udp.payload)
                        counters["udp_packets"] += 1
                        counters["kcp_datagrams" if segments else "non_kcp_datagrams"] += 1
                        for item in segments:
                            counters[f"kcp_{str(item['command']).casefold()}"] += 1
                            if item.get("command") != "PUSH":
                                continue
                            offset = int(item["payload_offset"])
                            length = int(item["payload_length"])
                            push = PushSegment(
                                flow=(
                                    int(local_port),
                                    str(remote_ip),
                                    int(remote_port),
                                    int(item["conv"]),
                                ),
                                sequence=int(item["sequence"]),
                                timestamp_epoch=timestamp_epoch,
                                ciphertext=bytes(
                                    udp.payload[offset : offset + length]
                                ),
                            )
                            decrypted = reassembler.add(push, now)
                            for value in decrypted:
                                try:
                                    records = decoder.feed_push(
                                        value.plaintext,
                                        value.sequence,
                                        value.timestamp_epoch,
                                    )
                                except (ValueError, OverflowError):
                                    counters["protocol_stream_errors"] += 1
                                    reassembler.invalidate()
                                    decoder.reset_transport()
                                    data_incomplete = True
                                    next_anchor_retry = now
                                    break
                                for record in records:
                                    if record.get("method") in {
                                        "RetCommonCombatStatisticsByTeam",
                                        "OnMsgSettlementCombatStatistics",
                                    }:
                                        counters["team_stats_responses"] += 1
                                        last_response_filetime = max(
                                            last_response_filetime,
                                            int(record.get("filetime_100ns", 0) or 0),
                                        )
                                    pending_records.append(record)

            now = time.monotonic()
            gap = reassembler.pending_gap(now)
            if reassembler.needs_resync(now):
                if gap is not None:
                    pending_gaps.append(
                        {
                            "expected": int(gap["expected"]),
                            "actual": int(gap["actual"]),
                            "capture_source": "npcap",
                            "reason": "missing_kcp_push",
                        }
                    )
                    counters["sequence_gap_resyncs"] += 1
                else:
                    counters["flow_switch_resyncs"] += 1
                data_incomplete = True
                decoder.reset_transport()
                try:
                    reassembler.install_anchor(reader.snapshot(), now)
                    next_anchor_retry = now + ALIGNMENT_RETRY_SECONDS
                except (OSError, RuntimeError, ValueError):
                    reader.close()
                    reader = None
                    reassembler.invalidate()
                    next_anchor_retry = now

            if (
                (reassembler.anchor is None or reassembler.alignment_stale(now))
                and now >= next_anchor_retry
            ):
                try:
                    if reader is None:
                        reader, locate_diagnostic = _locate_reader(pid)
                        if reader is None:
                            raise RuntimeError("RC4 state is unavailable")
                    reassembler.install_anchor(reader.snapshot(), now)
                    decoder.reset_transport()
                    counters["rc4_reanchors"] += 1
                except (OSError, RuntimeError, ValueError):
                    counters["rc4_reanchor_errors"] += 1
                    if reader is not None:
                        reader.close()
                    reader = None
                next_anchor_retry = now + ALIGNMENT_RETRY_SECONDS

            if now >= next_endpoint_refresh:
                current = [
                    row
                    for row in shadow_capture.list_udp_endpoints()
                    if int(row.pid) == int(pid)
                ]
                current_ports = sorted(
                    {int(row.local_port) for row in current if row.local_port}
                )
                if current_ports:
                    process_missing_since = None
                    if current_ports != ports:
                        old_ports = ports
                        ports = current_ports
                        _install_bpf(
                            wpcap,
                            handle,
                            shadow_capture.make_bpf(local_ip, ports),
                        )
                        counters["capture_filter_updates"] += 1
                        active = reassembler.active_flow
                        if active is not None and active[0] not in ports:
                            reassembler.invalidate()
                            decoder.reset_transport()
                            data_incomplete = True
                            next_anchor_retry = now
                        _put(
                            output_queue,
                            "state",
                            {
                                "stage": "capturing",
                                "process_found": True,
                                "game_pid": pid,
                                "capture_backend": "npcap",
                                "npcap_capture_active": True,
                                "npcap_old_ports": old_ports,
                                "npcap_game_ports": ports,
                            },
                        )
                else:
                    process_missing_since = process_missing_since or now
                    if (
                        now - process_missing_since
                        >= PROCESS_EXIT_GRACE_SECONDS
                    ):
                        return "game_exited"
                next_endpoint_refresh = now + ENDPOINT_REFRESH_SECONDS

            if now >= next_pcap_stats:
                pcap_diagnostic = _pcap_stats(wpcap, handle)
                dropped = int(pcap_diagnostic.get("dropped", 0) or 0)
                if dropped > last_pcap_dropped:
                    counters["pcap_dropped"] += dropped - last_pcap_dropped
                    last_pcap_dropped = dropped
                    data_incomplete = True
                next_pcap_stats = now + 1.0

            if (
                now >= next_flush
                or len(pending_records) >= BATCH_RECORD_LIMIT
                or pending_gaps
            ):
                counters.update(reassembler.counters)
                reassembler.counters.clear()
                counters["protocol_decrypted_pushes"] = (
                    decoder.diagnostics.decrypted_pushes
                )
                counters["protocol_retained_records"] = (
                    decoder.diagnostics.retained_records
                )
                counters["protocol_zstd_resyncs"] = (
                    decoder.diagnostics.zstd_resyncs
                )
                counters["protocol_zstd_errors"] = (
                    decoder.diagnostics.zstd_errors
                )
                counters["protocol_unknown_methods"] = (
                    decoder.diagnostics.unknown_method_messages
                )
                if _emit_batch(
                    output_queue,
                    session_id=session_id,
                    batch_id=batch_id,
                    game_pid=pid,
                    records=pending_records,
                    gaps=pending_gaps,
                    counters=counters,
                    pcap_diagnostic=pcap_diagnostic,
                    team_stats_mode=team_stats_mode,
                    last_response_filetime=last_response_filetime,
                    data_incomplete=data_incomplete,
                ):
                    batch_id += 1
                pending_records = []
                pending_gaps = []
                next_flush = now + BATCH_INTERVAL_SECONDS

        return "stopped"
    finally:
        if reader is not None:
            reader.close()
        wpcap.pcap_close(handle)


def shadow_capture_endpoint_parts(
    udp: shadow_capture.ParsedUdp, local_ip: str
) -> tuple[str, int, str, int, str]:
    if udp.src_ip == local_ip:
        return udp.src_ip, udp.src_port, udp.dst_ip, udp.dst_port, "outbound"
    if udp.dst_ip == local_ip:
        return udp.dst_ip, udp.dst_port, udp.src_ip, udp.src_port, "inbound"
    return local_ip, 0, udp.dst_ip, udp.dst_port, "unknown"


def _capture_forever(
    stop_event,
    output_queue,
    watchdog: ParentProcessWatchdog,
    runtime_expiry,
    team_stats_mode,
) -> None:
    if os.name != "nt":
        raise RuntimeError("Npcap capture is available only on Windows")
    try:
        wpcap = shadow_capture.load_wpcap()
    except (OSError, RuntimeError) as exc:
        _put(
            output_queue,
            "capture_error",
            {
                "stage": "npcap_unavailable",
                "details": f"Npcap 采集组件不可用：{exc}",
                "capture_backend": "npcap",
            },
        )
        return

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
                "npcap_capture_active": True,
                "capture_backend": "npcap",
                "damage_source": "npcap",
            },
        )
        try:
            reason = _session(
                stop_event,
                output_queue,
                watchdog,
                runtime_expiry,
                team_stats_mode,
                wpcap,
                session_id + 1,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "npcap_capture_failed",
                    "details": f"{type(exc).__name__}: {exc}",
                    "capture_backend": "npcap",
                },
            )
            reason = "retry"
        else:
            if reason not in {"game_not_found", "retry"}:
                session_id += 1

        if reason == "game_not_found":
            _put(
                output_queue,
                "state",
                {
                    "stage": "game_not_found",
                    "process_found": False,
                    "capture_backend": "npcap",
                    "npcap_capture_active": True,
                },
            )
        elif reason in {"game_exited", "capture_closed", "retry"}:
            if session_id:
                _put(
                    output_queue,
                    "session_closed",
                    {
                        "session_id": session_id,
                        "team_stats_mode": team_stats_mode_name(
                            _read_team_stats_mode(team_stats_mode)
                        ),
                        "hook_cleanup_verified": True,
                        "hook_cleanup_components": {"npcap": True},
                        "capture_backend": "npcap",
                        "npcap_capture_active": False,
                        "damage_source": "none",
                    },
                )
            if reason == "game_exited":
                _put(output_queue, "state", {"stage": "game_exited"})

        if not _should_stop(stop_event, watchdog, runtime_expiry):
            _interruptible_wait(
                stop_event,
                watchdog,
                GAME_SEARCH_WAIT_SECONDS
                if reason == "game_not_found"
                else CAPTURE_RETRY_WAIT_SECONDS,
                runtime_expiry,
            )

    if not _runtime_active(runtime_expiry) and not stop_event.is_set():
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
    del target_boss_lookup_event  # Npcap passively observes all server records.
    watchdog = ParentProcessWatchdog(parent_pid)
    parent_alive = watchdog.is_alive()
    try:
        capability = RuntimeCapability.from_value(
            runtime_capability,
            trusted_public_keys=trusted_public_keys,
            allow_development=bool(allow_development),
        )
        if not _runtime_active(runtime_expiry):
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
                "capture_backend": "npcap",
                "npcap_capture_active": True,
                **priority,
            },
        )
        _capture_forever(
            stop_event,
            output_queue,
            watchdog,
            runtime_lease,
            team_stats_mode,
        )
    except RuntimeCapabilityError as exc:
        _put(output_queue, "fatal", f"runtime capability rejected: {exc}")
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
    """Parent-side API compatible with the existing UI worker."""

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
            name="GMZZNpcapCapture",
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
        if self._started:
            raise RuntimeError("capture process has already been started")
        if self._closed:
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
        self.runtime_expiry_value.value = 0.0
        self.request_stop()

    def get(self, timeout: float | None = None):
        return self.output_queue.get(timeout=timeout)

    def set_target_boss_lookup_enabled(self, enabled: bool) -> None:
        # UI compatibility only. Passive Npcap observes all matching packets.
        if enabled:
            self.target_boss_lookup_event.set()
        else:
            self.target_boss_lookup_event.clear()

    def set_team_stats_mode(self, mode: object) -> str:
        # Scene mode remains useful to the UI, but never triggers a request.
        normalized = normalize_team_stats_mode(
            mode, default=TEAM_STATS_MODE_UNKNOWN
        )
        self.team_stats_mode_value.value = normalized
        return team_stats_mode_name(normalized)

    def get_team_stats_mode(self) -> str:
        return team_stats_mode_name(self.team_stats_mode_value.value)

    def request_stop(self) -> None:
        self.stop_event.set()

    def is_alive(self) -> bool:
        if not self._started or self._closed:
            return False
        try:
            return self.process.is_alive()
        except (AssertionError, ValueError):
            return False

    def join(self, timeout: float | None = None) -> None:
        if not self._started or self._closed:
            return
        try:
            self.process.join(timeout)
        except (AssertionError, ValueError):
            pass

    def terminate(self) -> None:
        if self.is_alive():
            try:
                self.process.terminate()
            except (AssertionError, ValueError):
                pass

    def close(self, *, wait_for_queue: bool = True) -> None:
        if self._closed:
            return
        try:
            renewal_queue = self.runtime_capability_queue
            cancel_renewal_join = getattr(
                renewal_queue, "cancel_join_thread", None
            )
            if callable(cancel_renewal_join):
                cancel_renewal_join()
            renewal_queue.close()
            if not wait_for_queue:
                cancel_join = getattr(
                    self.output_queue, "cancel_join_thread", None
                )
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
