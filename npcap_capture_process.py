#!/usr/bin/env python3
"""Passive packet capture backend used by the main application and releases.

The backend receives encrypted UDP/KCP or TCP traffic and decodes only
inbound server PUSH data without modifying the game's packet decoder or
transmitting a packet. The only production packet source is the built-in
Windows receive-only transport. The protocol/state reader names are retained
for file-format compatibility with existing captures.
"""

from __future__ import annotations

import argparse
import ctypes
import multiprocessing
import os
import queue
import re
import time
import traceback
from collections import Counter, OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import npcap_shadow_capture as shadow_capture
import proc_inspect
from npcap_key_state import (
    GAME_MODULE as RC4_GAME_MODULE,
    RC4_CRYPTOR_OWNER_VTABLE_RVA,
    Rc4Anchor,
    Rc4StateReader,
    locate_readers,
)
from npcap_protocol import NpcapProtocolDecoder, application_frame_valid
from npcap_rc4_decode import Rc4State
from npcap_zstd_state import (
    ZstdStateReader,
    coherent_session_snapshot,
    locate_zstd_readers,
)
from runtime_capability import RuntimeCapability, RuntimeCapabilityError
from npcap_receiver import BufferedReceiver, MultiAdapterReceiver, endpoint_direction
from npcap_tcp_stream import PassiveTcpRc4Reassembler
from passive_transport import CaptureFrame, PacketReassembler, SequenceUnwrapper
from npcap_bootstrap import FrozenSessionState, PassiveStateError, bootstrap_copies
from network_state import (
    is_player_skill,
    normalize_network_skill_id,
    parse_exact_combat_entity_id,
)
from runtime_metadata import ROLE_ID_RE, latest_main_player_role
from windows_raw_receiver import RawSocketUnavailable
from windows_hybrid_receiver import WindowsHybridReceiver


CAPTURE_RETRY_WAIT_SECONDS = 0.5
GAME_SEARCH_WAIT_SECONDS = 1.0
ENDPOINT_REFRESH_SECONDS = 1.0
PROCESS_EXIT_GRACE_SECONDS = 5.0
# The game process exposes sockets before its per-connection RC4/Zstd objects
# are fully constructed.  A single snapshot attempt at that instant leaves a
# "program first, game second" launch parked forever waiting for a connection
# that is already alive.  Retry only during this initial, not-yet-armed phase.
INITIAL_BOOTSTRAP_MAX_ATTEMPTS = 8
INITIAL_BOOTSTRAP_RETRY_SECONDS = 1.0
# Only before any decoder has been armed: a connection can expose its RC4/Zstd
# objects after the first bounded startup window. Retry one read-only snapshot
# periodically instead of parking the initial launch until the game reconnects.
INITIAL_STATE_RECOVERY_SECONDS = 20.0
# A successful same-connection recovery may later run for a long time before
# another independent packet loss. Allow another bounded read-only snapshot
# only after that recovered decoder has stayed valid for five minutes.
STABLE_GAP_RECOVERY_SECONDS = 300.0
# A coherent snapshot can still lose its first alignment race when the receive
# queue contains a large burst from immediately before the snapshot.  Permit a
# very small number of fresh snapshots only until the first application message
# proves the stream. Runtime failures after ``capture_ready`` remain fail-closed.
PRE_READY_STATE_REFRESH_LIMIT = 2


def _can_recover_stream_gap(
    ready_at: float, now: float, previous_recovery_used: bool
) -> bool:
    return bool(
        ready_at > 0
        and (
            not previous_recovery_used
            or now - ready_at >= STABLE_GAP_RECOVERY_SECONDS
        )
    )
PCAP_READ_TIMEOUT_MS = 50
PCAP_BUFFER_BYTES = 32 * 1024 * 1024
BATCH_INTERVAL_SECONDS = 0.02
BATCH_RECORD_LIMIT = 256
# A long-running game can retain a chat/auction connection while combat moves
# to another UDP flow. Validate gameplay before discarding copied candidates.
BACKGROUND_STREAM_PROBE_SECONDS = 2.0
GAMEPLAY_STREAM_METHODS = frozenset({
    "OnMsgRefreshSceneObjects", "OnMsgSyncFightMode",
    "OnMsgDamageSyncV2", "OnMsgBeatenSyncV2", "OnMsgHealSyncV2",
    "OnMsgCastSkillNew", "RetCastSkillSuccessNew",
    "OnMsgEntityDead", "OnMsgEntityRelive",
    "OnMsgUpdateStageCombatStatistics", "OnMsgSettlementCombatStatistics",
})
# Kept as a read-only legacy label for old diagnostic records. It is never a
# valid runtime source and cannot be selected by the client or build metadata.
CAPTURE_SOURCE_NPCAP = "npcap"
CAPTURE_SOURCE_WINDOWS_RAW = "windows_raw"
CAPTURE_SOURCES = {CAPTURE_SOURCE_WINDOWS_RAW}
ALIGNMENT_VALID_PUSHES = 3
# Sparse scene/heartbeat traffic may need several seconds to provide the three
# consecutive PUSH records used to prove an RC4 boundary.  Refreshing the
# snapshot every second discarded that useful buffer and repeatedly rescanned
# game memory.  Alignment still completes immediately once validation passes;
# this value only controls how long an unproven anchor is retained.
ALIGNMENT_RETRY_SECONDS = 15.0
# A stale RC4 candidate does not need the full sparse-traffic grace period once
# enough *new* KCP PUSH records have arrived to prove it cannot decrypt the
# active flow. A valid candidate aligns after three consecutive PUSH records;
# keeping a larger evidence margin avoids penalising genuinely sparse flows
# while preventing reconnect remnants from freezing capture for 90 seconds.
ALIGNMENT_FAST_REJECT_PUSHES = 12
ALIGNMENT_FAST_REJECT_SECONDS = 0.25
KCP_GAP_WAIT_SECONDS = 1.25
FLOW_SWITCH_WAIT_SECONDS = 2.0
PROTOCOL_STALL_MIN_PUSHES = 64
PROTOCOL_STALL_SECONDS = 8.0
SCENE_TRANSITION_RELOCATE_DELAY_SECONDS = 0.25
SCENE_TRANSITION_METHODS = frozenset(
    {
        "OnMsgBeforeEnterNewSpace",
        "OnMsgLeaveQuestControl",
        "OnMsgLeaveSpace",
    }
)
ENTITY_METADATA_SIGNAL_METHODS = frozenset(
    {
        "OnMsgEndureExitHit",
        "OnMsgSyncCurrentHp",
        "OnMsgSyncCurrentMaxHp",
        "OnMsgSyncFightMode",
        "OnMsgEntityDead",
    }
)
MAX_FLOW_SEGMENTS = 16_384
MAX_FLOWS = 16
LIVE_TEAM_PROFILE_TOKEN_RE = re.compile(r"^AQ[A-Za-z0-9_-]{10,30}$")

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


def normalize_capture_source(value: object) -> str:
    source = str(value or CAPTURE_SOURCE_WINDOWS_RAW).strip().casefold()
    aliases = {
        "raw": CAPTURE_SOURCE_WINDOWS_RAW,
        "raw_socket": CAPTURE_SOURCE_WINDOWS_RAW,
        "windows_raw_socket": CAPTURE_SOURCE_WINDOWS_RAW,
    }
    source = aliases.get(source, source)
    if source not in CAPTURE_SOURCES:
        raise ValueError(
            f"Unsupported passive packet source: {source}; "
            "only the built-in Windows receive-only source is supported"
        )
    return source


def _capture_state_fields(source: object, *, active: bool = True) -> dict[str, object]:
    source = normalize_capture_source(source)
    return {
        "capture_backend": source,
        "capture_source": source,
        "passive_capture_active": bool(active),
        "npcap_capture_active": False,
        "raw_socket_capture_active": bool(
            active and source == CAPTURE_SOURCE_WINDOWS_RAW
        ),
    }


def _open_packet_receiver(source: object, endpoints, *, wpcap=None):
    source = normalize_capture_source(source)
    if source == CAPTURE_SOURCE_WINDOWS_RAW:
        return BufferedReceiver(
            WindowsHybridReceiver(endpoints),
            None,
            backend_name="Windows Native IPv4/IPv6",
        )
    raise ValueError(
        "The legacy packet source is not part of the production capture path"
    )


def _put(output_queue, kind: str, payload=None) -> None:
    # Local diagnostic sidecar: counters/state only, never packet bodies,
    # character identities, connection keys or runtime credentials.
    if kind in {
        'state',
        'fatal',
        'capture_error',
        'runtime_capability_expired',
        'stopped',
    } or (
        kind == 'batch' and isinstance(payload, dict)
        and payload.get('native_diagnostic')
    ):
        import json
        from pathlib import Path
        now = time.monotonic()
        last = getattr(_put, '_diagnostic_at', 0.0)
        if kind != 'batch' or now - last >= 1.0:
            _put._diagnostic_at = now
            if kind == 'batch':
                diagnostic = payload.get('native_diagnostic', {})
                safe = {'counters': diagnostic.get('counters', {}),
                        'pcap': diagnostic.get('pcap', {})}
            elif isinstance(payload, dict):
                safe = {
                    key: payload[key]
                    for key in (
                        'stage',
                        'details',
                        'process_found',
                        'reason',
                        'parent_alive',
                        'stop_event_set',
                        'runtime_expiry',
                        'capture_transport',
                        'capture_protocol_ready',
                        'bootstrap_attempts_for_connection',
                        'same_connection_memory_rescans',
                    )
                    if key in payload
                }
            else:
                safe = {'message': str(payload)[:2000]}
            try:
                folder = Path(__file__).resolve().parent / 'logs'
                folder.mkdir(exist_ok=True)
                with (folder / f'capture_health_{os.getpid()}.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'time': time.time(), 'kind': kind, **safe}, ensure_ascii=False) + '\n')
            except OSError:
                pass
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
    timestamp_ns: int | None = None


@dataclass(frozen=True)
class DecryptedPush:
    flow: tuple[int, str, int, int]
    sequence: int
    timestamp_epoch: float
    plaintext: bytes
    after_anchor: bool
    timestamp_ns: int | None = None


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
        self.serials: dict[tuple, SequenceUnwrapper] = {}
        self.anchor: Rc4Anchor | None = None
        self.active_flow: tuple[int, str, int, int] | None = None
        self.state: Rc4State | None = None
        self.next_sequence: int | None = None
        self.alignment_boundary: int | None = None
        self.gap_started_monotonic: float | None = None
        self.alignment_started_monotonic: float | None = None
        self.alignment_new_unique_pushes = 0
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
        self.alignment_boundary = None
        self.gap_started_monotonic = None
        self.alignment_started_monotonic = (
            time.monotonic() if now is None else float(now)
        )
        self.alignment_new_unique_pushes = 0
        self.counters["rc4_anchors"] += 1

    def invalidate(self, *, clear_history: bool = False) -> None:
        if self.aligned:
            self.counters["stream_invalidations"] += 1
        self.anchor = None
        self.active_flow = None
        self.state = None
        self.next_sequence = None
        self.alignment_boundary = None
        self.gap_started_monotonic = None
        self.alignment_started_monotonic = None
        self.alignment_new_unique_pushes = 0
        if clear_history:
            self.flows.clear()
            self.last_seen_monotonic.clear()
            self.last_delivered.clear()
            self.serials.clear()
            self.counters["connection_history_resets"] += 1

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
                self.serials.pop(stale, None)
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
        serial = self.serials.setdefault(segment.flow, SequenceUnwrapper())
        segment = replace(segment, sequence=serial.unwrap(segment.sequence))
        existing = values.get(int(segment.sequence))
        if existing is not None:
            if existing.ciphertext == segment.ciphertext:
                self.counters["kcp_retransmissions"] += 1
            else:
                self.counters["kcp_sequence_conflicts"] += 1
                if self.active_flow == segment.flow:
                    self.invalidate()
            return []
        if segment.sequence <= self.last_delivered.get(segment.flow, -1):
            self.counters["kcp_old_retransmissions"] += 1
            return []
        values[int(segment.sequence)] = segment
        self.last_seen_monotonic[segment.flow] = monotonic_now
        self.counters["unique_pushes"] += 1
        if (
            self.anchor is not None
            and not self.aligned
            # BufferedReceiver starts before the memory snapshot so startup
            # can never miss the wire bytes around that snapshot.  Those
            # buffered, pre-snapshot PUSHes are useful for backward recovery,
            # but they are not evidence that the candidate is wrong.  Counting
            # them here can reject the live candidate before playback reaches
            # the first packet captured after its RC4 state was copied.
            and float(segment.timestamp_epoch)
            >= float(self.anchor.read_after_epoch)
        ):
            self.alignment_new_unique_pushes += 1
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
            decoded[sequence] = plaintext
            sequence -= 1

        forward = anchor.decrypt.clone()
        sequence = boundary + 1
        while sequence in values:
            plaintext = forward.forward(values[sequence].ciphertext)
            decoded[sequence] = plaintext
            sequence += 1

        if len([item for item in decoded if item > boundary]) < self.required_validation:
            return []

        self.active_flow = flow
        self.state = forward
        self.next_sequence = sequence
        self.alignment_boundary = boundary
        self.gap_started_monotonic = None
        self.counters["rc4_alignments"] += 1
        output = [
            DecryptedPush(
                flow=flow,
                sequence=item,
                timestamp_epoch=values[item].timestamp_epoch,
                plaintext=decoded[item],
                after_anchor=item > boundary,
                timestamp_ns=values[item].timestamp_ns,
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
            result.append(
                DecryptedPush(
                    flow=segment.flow,
                    sequence=segment.sequence,
                    timestamp_epoch=segment.timestamp_epoch,
                    plaintext=plaintext,
                    after_anchor=True,
                    timestamp_ns=segment.timestamp_ns,
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

    def fresh_alternate_flow(self, now: float | None = None):
        """Return a recently active replacement flow, if one is established.

        A replacement flow is a new connection and needs a new detached
        bootstrap snapshot. It is deliberately distinguished from a hole in
        the current flow, which must remain an incomplete/fatal session rather
        than repeatedly rereading memory for the same damaged stream.
        """
        if not self.aligned or self.active_flow is None:
            return None
        monotonic_now = time.monotonic() if now is None else float(now)
        last_active = self.last_seen_monotonic.get(self.active_flow, 0.0)
        if monotonic_now - last_active < self.flow_switch_wait_seconds:
            return None
        candidates = [
            flow
            for flow, values in self.flows.items()
            if flow != self.active_flow
            and monotonic_now - self.last_seen_monotonic.get(flow, 0.0)
            <= self.flow_switch_wait_seconds
            and len(values) >= self.required_validation
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda flow: self.last_seen_monotonic[flow])

    def alignment_stale(self, now: float | None = None) -> bool:
        if self.aligned or self.anchor is None:
            return False
        monotonic_now = time.monotonic() if now is None else float(now)
        started = self.alignment_started_monotonic
        if started is None:
            return False
        elapsed = monotonic_now - started
        # Elapsed time without traffic says nothing about a key candidate.
        if (elapsed >= ALIGNMENT_RETRY_SECONDS
                and self.alignment_new_unique_pushes >= self.required_validation):
            return True
        return bool(
            elapsed >= ALIGNMENT_FAST_REJECT_SECONDS
            and self.alignment_new_unique_pushes
            >= ALIGNMENT_FAST_REJECT_PUSHES
        )


class PassiveGameRc4Reassembler:
    """Select a proved inbound UDP/KCP or TCP stream, never a Hook fallback."""
    def __init__(self):
        self.udp = PassiveRc4Reassembler()
        self.tcp = PassiveTcpRc4Reassembler()
        self.active = None
        self.counters = Counter()

    @property
    def aligned(self):
        return self.active is not None and self.active.aligned

    @property
    def active_flow(self):
        return self.active.active_flow if self.active is not None else None

    @property
    def protocol(self):
        return 'tcp' if self.active is self.tcp else 'udp' if self.active is self.udp else 'unknown'

    @property
    def anchor(self):
        return self.udp.anchor

    @property
    def flows(self):
        return {**self.udp.flows, **self.tcp.flows}

    def install_anchor(self, anchor, now=None):
        self.udp.install_anchor(anchor, now)
        self.tcp.install_anchor(anchor, now)
        self.active = None

    def invalidate(self, *, clear_history=False):
        self.udp.invalidate(clear_history=clear_history)
        self.tcp.invalidate(clear_history=clear_history)
        self.active = None

    def _select(self, source, values):
        self.counters.update(source.counters)
        source.counters.clear()
        if self.active is None and source.aligned:
            self.active = source
        return values if self.active is source else []

    def add(self, push, now=None):
        return self._select(self.udp, self.udp.add(push, now))

    def add_tcp(self, packet, now=None):
        return self._select(self.tcp, self.tcp.add(packet, now))

    def confirm_alignment(self):
        if self.active is self.tcp:
            self.tcp.confirm_alignment()

    def pending_gap(self, now=None):
        return self.active.pending_gap(now) if self.active is not None else None

    def needs_resync(self, now=None):
        return bool(self.active is not None and
                    (self.active.needs_resync(now) or self.fresh_alternate_flow(now) is not None))

    def fresh_alternate_flow(self, now=None):
        if self.active is None:
            return None
        alternate = self.active.fresh_alternate_flow(now)
        if alternate is not None:
            return alternate
        monotonic_now = time.monotonic() if now is None else float(now)
        if monotonic_now-self.active.last_seen_monotonic.get(self.active_flow, 0) < FLOW_SWITCH_WAIT_SECONDS:
            return None
        other = self.tcp if self.active is self.udp else self.udp
        return next((flow for flow in other.flows
                     if monotonic_now-other.last_seen_monotonic.get(flow, 0) < FLOW_SWITCH_WAIT_SECONDS), None)

    def alignment_stale(self, now=None):
        if self.aligned or self.anchor is None:
            return False
        monotonic_now = time.monotonic() if now is None else float(now)
        started = self.udp.alignment_started_monotonic
        if started is None:
            return False
        evidence = self.udp.alignment_new_unique_pushes+self.tcp.alignment_new_unique_pushes
        elapsed = monotonic_now-started
        return bool((elapsed >= ALIGNMENT_RETRY_SECONDS and evidence >= ALIGNMENT_VALID_PUSHES)
                    or (elapsed >= ALIGNMENT_FAST_REJECT_SECONDS and evidence >= ALIGNMENT_FAST_REJECT_PUSHES))


def _protocol_stream_stalled(
    *,
    aligned: bool,
    pushes_without_frame: int,
    last_frame_monotonic: float,
    now: float,
) -> bool:
    return bool(
        aligned
        and int(pushes_without_frame) >= PROTOCOL_STALL_MIN_PUSHES
        and float(now) - float(last_frame_monotonic)
        >= PROTOCOL_STALL_SECONDS
    )


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
    capture_source: str = CAPTURE_SOURCE_WINDOWS_RAW,
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
        "response_health": f"passive_{normalize_capture_source(capture_source)}",
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


def _walk_record_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_record_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_record_values(item)
    else:
        yield value


def live_team_profile_tokens(record: object) -> set[str]:
    """Return live role tokens from team-shaped inbound records only."""

    if not isinstance(record, dict):
        return set()
    method = str(record.get("method", ""))
    folded = method.casefold()
    if not (
        "team" in folded
        or "readiness" in folded
        or "combatstatistics" in folded
        or method == "OnMsgDungeonBotDisplay"
    ):
        return set()
    values = [
        record.get("npcap_recipient"),
        record.get("decoded_arguments", []),
    ]
    return {
        token
        for raw in _walk_record_values(values)
        if LIVE_TEAM_PROFILE_TOKEN_RE.fullmatch(
            token := str(raw or "").strip()
        )
    }


def _request_entity_metadata(metadata_reader, record: object) -> None:
    """Queue only bounded, combat-relevant target discovery work."""

    if metadata_reader is None or not isinstance(record, dict):
        return
    method = str(record.get("method", ""))
    arguments = record.get("decoded_arguments", [])
    if not isinstance(arguments, list):
        arguments = []
    timestamp = int(record.get("filetime_100ns", 0) or 0)

    if method in {"OnMsgCastSkillNew", "OnMsgDamageSyncV2"}:
        skill_index = 0 if method == "OnMsgCastSkillNew" else 2
        if len(arguments) > max(1, skill_index) and is_player_skill(
            normalize_network_skill_id(arguments[skill_index])
        ):
            target_id = parse_exact_combat_entity_id(arguments[1])
            if target_id:
                metadata_reader.request(
                    target_id, timestamp, discovery="preferred"
                )
        return

    if method == "OnMsgEndureExitHit":
        target_id = parse_exact_combat_entity_id(
            record.get("network_entity_id")
        )
        if target_id:
            metadata_reader.request(
                target_id, timestamp, discovery="preferred"
            )
        return

    if method == "OnMsgHealSyncV2" and len(arguments) > 1:
        target_id = parse_exact_combat_entity_id(arguments[1])
        if target_id:
            metadata_reader.request(
                target_id, timestamp, discovery="fallback"
            )
        return

    if method in ENTITY_METADATA_SIGNAL_METHODS:
        target_id = parse_exact_combat_entity_id(
            record.get("network_entity_id")
        )
        if target_id:
            metadata_reader.request(
                target_id, timestamp, discovery="fallback"
            )


def _latest_team_profile_roster(control_queue) -> set[str] | None:
    """Drain roster commands and return the newest exact token set."""

    if control_queue is None:
        return None
    latest = None
    while True:
        try:
            latest = control_queue.get_nowait()
        except queue.Empty:
            break
        except (EOFError, OSError, ValueError):
            return None
    if latest is None:
        return None
    if not isinstance(latest, (list, tuple, set)):
        return set()
    return {
        token
        for raw in latest
        if ROLE_ID_RE.fullmatch(token := str(raw or "").strip())
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
    metadata_records: list[dict] | None = None,
    team_profile_records: list[dict] | None = None,
    heartbeat: bool = False,
    capture_source: str = CAPTURE_SOURCE_WINDOWS_RAW,
) -> bool:
    if (
        not records
        and not gaps
        and not metadata_records
        and not team_profile_records
        and not heartbeat
    ):
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
            "entity_metadata_records": metadata_records or [],
            "team_profile_records": team_profile_records or [],
            "native_name_records": [],
            "native_skill_name_records": [],
            "native_damage_hook_installed": False,
            "team_status": _team_status(
                team_stats_mode,
                counters,
                last_response_filetime,
                data_incomplete,
                capture_source,
            ),
            "team_stats_mode": team_stats_mode_name(
                _read_team_stats_mode(team_stats_mode)
            ),
            "sequence_gaps": gaps,
            "native_diagnostic": {
                **_capture_state_fields(capture_source),
                "passive_only": True,
                "game_process_access": "query_and_read_only",
                "server_requests_added": 0,
                "counters": dict(counters),
                "pcap": dict(pcap_diagnostic),
            },
            "damage_source": normalize_capture_source(capture_source),
            **_capture_state_fields(capture_source),
        },
    )
    return True


@dataclass
class SessionStateReader:
    rc4: Rc4StateReader
    zstd: ZstdStateReader

    @property
    def cryptor_address(self) -> int:
        return self.rc4.cryptor_address

    def close(self) -> None:
        self.rc4.close()
        self.zstd.close()


def _close_state_readers(readers: Iterable[SessionStateReader]) -> None:
    for reader in readers:
        reader.close()


def _rc4_anchor_changed(before: Rc4Anchor, after: Rc4Anchor) -> bool:
    first = before.decrypt
    second = after.decrypt
    return bool(
        first.x != second.x
        or first.y != second.y
        or first.s != second.s
    )


def _prioritize_active_state_readers(
    readers: list[SessionStateReader],
) -> tuple[list[SessionStateReader], int]:
    """Move candidates that advanced during discovery ahead of stale peers."""
    active: list[SessionStateReader] = []
    unchanged: list[SessionStateReader] = []
    for reader in readers:
        initial = getattr(reader.rc4, "initial_anchor", None)
        try:
            current = reader.rc4.snapshot()
        except (OSError, RuntimeError, ValueError):
            unchanged.append(reader)
            continue
        if initial is not None and _rc4_anchor_changed(initial, current):
            active.append(reader)
        else:
            unchanged.append(reader)
    return active + unchanged, len(active)


def _locate_state_readers(
    pid: int,
) -> tuple[list[SessionStateReader], dict[str, object]]:
    rc4_readers, rc4_diagnostic = locate_readers(pid)
    try:
        zstd_readers, zstd_diagnostic = locate_zstd_readers(
            pid, (reader.cryptor_address for reader in rc4_readers)
        )
    except Exception:
        for reader in rc4_readers:
            reader.close()
        raise

    pairs = []
    for rc4_reader in rc4_readers:
        zstd_reader = zstd_readers.pop(rc4_reader.cryptor_address, None)
        if zstd_reader is None:
            rc4_reader.close()
            continue
        pairs.append(SessionStateReader(rc4_reader, zstd_reader))
    for zstd_reader in zstd_readers.values():
        zstd_reader.close()
    pairs, active_candidates = _prioritize_active_state_readers(pairs)
    return pairs, {
        "rc4_regions_read": rc4_diagnostic.regions_read,
        "rc4_bytes_read": rc4_diagnostic.bytes_read,
        "rc4_pointer_hits": rc4_diagnostic.pointer_hits,
        "rc4_elapsed_seconds": rc4_diagnostic.elapsed_seconds,
        "zstd_regions_read": zstd_diagnostic.regions_read,
        "zstd_bytes_read": zstd_diagnostic.bytes_read,
        "zstd_pointer_hits": zstd_diagnostic.pointer_hits,
        "zstd_elapsed_seconds": zstd_diagnostic.elapsed_seconds,
        "rc4_candidates": len(rc4_readers),
        "paired_candidates": len(pairs),
        "active_candidates": active_candidates,
    }


def _locate_hinted_state_readers(
    pid: int,
    address_hints: Iterable[tuple[int, int, int, int]],
) -> tuple[list[SessionStateReader], dict[str, object]]:
    """Reopen previously verified addresses for a new same-process connection.

    The addresses remain only in the capture child and are validated again by
    both reader constructors plus ``coherent_session_snapshot``. A stale or
    reallocated object simply produces no candidate and the caller falls back
    to the ordinary bounded scan.
    """

    matching = [
        (int(cryptor), int(codec), int(context))
        for hint_pid, cryptor, codec, context in address_hints
        if int(hint_pid) == int(pid)
        and int(cryptor) > 0
        and int(codec) > 0
        and int(context) > 0
    ]
    if not matching:
        return [], {
            "address_hint_candidates": 0,
            "address_hint_hits": 0,
        }
    module_base, _module_size, _path = proc_inspect.find_module(
        int(pid), RC4_GAME_MODULE
    )
    owner_pointer = module_base + int(RC4_CRYPTOR_OWNER_VTABLE_RVA)
    readers: list[SessionStateReader] = []
    for cryptor_address, codec_address, context_address in matching:
        rc4_reader = None
        zstd_reader = None
        try:
            rc4_reader = Rc4StateReader(
                int(pid), cryptor_address, owner_pointer
            )
            zstd_reader = ZstdStateReader(
                int(pid), codec_address, context_address
            )
            readers.append(SessionStateReader(rc4_reader, zstd_reader))
        except (OSError, RuntimeError, ValueError):
            if rc4_reader is not None:
                rc4_reader.close()
            if zstd_reader is not None:
                zstd_reader.close()
    readers, active_candidates = _prioritize_active_state_readers(readers)
    return readers, {
        "rc4_regions_read": 0,
        "rc4_bytes_read": 0,
        "rc4_pointer_hits": len(readers),
        "rc4_elapsed_seconds": 0.0,
        "zstd_regions_read": 0,
        "zstd_bytes_read": 0,
        "zstd_pointer_hits": len(readers),
        "zstd_elapsed_seconds": 0.0,
        "rc4_candidates": len(readers),
        "paired_candidates": len(readers),
        "active_candidates": active_candidates,
        "address_hint_candidates": len(matching),
        "address_hint_hits": len(readers),
    }


def _bootstrap_initial_state(
    pid: int,
    receiver,
    stop_event,
    watchdog: ParentProcessWatchdog,
    runtime_expiry,
    output_queue,
    address_hints: Iterable[tuple[int, int, int, int]] = (),
    max_attempts: int = INITIAL_BOOTSTRAP_MAX_ATTEMPTS,
) -> tuple[list[FrozenSessionState], dict[str, object], list[object]]:
    """Take a bounded startup snapshot after early game initialization.

    This is deliberately separate from transport resynchronisation.  It may
    retry while no decoder has ever been armed, but runtime stream failures
    still wait for a real connection change and never rescan the same stream.
    Every failed attempt closes all process handles in ``bootstrap_copies``.
    """
    last_error: Exception | None = None
    current_endpoints = list(receiver.endpoints)
    hints = tuple(address_hints)
    if hints and not _should_stop(stop_event, watchdog, runtime_expiry):
        try:
            snapshots, diagnostic = bootstrap_copies(
                pid,
                lambda hinted_pid: _locate_hinted_state_readers(
                    hinted_pid, hints
                ),
                coherent_session_snapshot,
            )
        except (PassiveStateError, OSError, RuntimeError, ValueError) as exc:
            if "handle cleanup failed" in str(exc).casefold():
                raise
            last_error = exc
        else:
            return snapshots, {
                **diagnostic,
                "bootstrap_attempts_for_connection": 1,
                "same_connection_memory_rescans": 0,
                "startup_retry_only": True,
                "address_hint_fast_path": True,
            }, current_endpoints
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        if _should_stop(stop_event, watchdog, runtime_expiry):
            break
        if attempt > 1:
            refreshed = shadow_capture.list_game_endpoints(pid)
            if refreshed:
                current_endpoints = list(refreshed)
                receiver.refresh(current_endpoints)
        try:
            snapshots, diagnostic = bootstrap_copies(
                pid, _locate_state_readers, coherent_session_snapshot
            )
        except (PassiveStateError, OSError, RuntimeError, ValueError) as exc:
            last_error = exc
            # Handle cleanup is a hard safety boundary, not a startup race.
            if "handle cleanup failed" in str(exc).casefold():
                raise
            if attempt >= attempts:
                break
            _put(
                output_queue,
                "state",
                {
                    "stage": "npcap_waiting_connection_state",
                    "details": str(exc),
                    "process_found": True,
                    "game_pid": pid,
                    "capture_protocol_ready": False,
                    "bootstrap_attempts_for_connection": attempt,
                    "same_connection_memory_rescans": attempt - 1,
                },
            )
            if _interruptible_wait(
                stop_event,
                watchdog,
                INITIAL_BOOTSTRAP_RETRY_SECONDS,
                runtime_expiry,
            ):
                break
            continue
        diagnostic = {
            **diagnostic,
            "bootstrap_attempts_for_connection": attempt,
            "same_connection_memory_rescans": attempt - 1,
            "startup_retry_only": True,
        }
        return snapshots, diagnostic, current_endpoints
    if isinstance(last_error, PassiveStateError):
        raise last_error
    raise PassiveStateError(
        f"Initial connection state scan failed after bounded retries: {last_error}"
        if last_error
        else "Capture stopped before the initial connection state became ready"
    )


def _remember_state_address_hints(
    destination: list[tuple[int, int, int, int]] | None,
    pid: int,
    snapshots: Iterable[FrozenSessionState],
) -> None:
    if destination is None:
        return
    remembered: list[tuple[int, int, int, int]] = []
    for snapshot in snapshots:
        compression = getattr(snapshot, "compression", None)
        try:
            hint = (
                int(pid),
                int(getattr(snapshot, "cryptor_address", 0) or 0),
                int(getattr(compression, "codec_address", 0) or 0),
                int(getattr(compression, "context_address", 0) or 0),
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if all(value > 0 for value in hint):
            remembered.append(hint)
    destination[:] = list(dict.fromkeys(remembered))


def _install_state_snapshot(
    reader: FrozenSessionState,
    reassembler: PassiveRc4Reassembler,
    decoder: NpcapProtocolDecoder,
    now: float | None = None,
) -> None:
    if not isinstance(reader, FrozenSessionState):
        raise PassiveStateError('Runtime state installation requires a detached local snapshot')
    decoder.install_zstd_snapshot(reader.compression)
    reassembler.install_anchor(reader.anchor, now)


def _alternate_udp_state_candidate(reassembler, readers, current_index, now):
    """Find a copied state proven on a recent, different UDP flow."""

    udp = reassembler.udp
    for index in range(current_index + 1, len(readers)):
        anchor = readers[index].anchor
        for flow, values in udp.flows.items():
            if flow == reassembler.active_flow or now - udp.last_seen_monotonic.get(flow, 0) > 10:
                continue
            delivered = udp.last_delivered.get(flow, -1)
            for boundary in udp._candidate_boundaries(values, anchor.midpoint_epoch):
                if boundary >= delivered and udp._score_boundary(
                    values, boundary, anchor.decrypt, anchor.midpoint_epoch
                ) is not None:
                    return index
    return None


def _passive_payloads(reassembler, packet, endpoints, local_addresses, now, counters):
    """Feed owned inbound bytes; TCP segmentation never substitutes for KCP."""
    if packet is None:
        counters['unparsed_packets'] += 1
        return
    local_host, local_port, remote_ip, remote_port, direction = endpoint_direction(
        packet, endpoints, local_addresses)
    if direction != 'inbound':
        return
    counters[f'{packet.protocol}_packets'] += 1
    if packet.protocol == 'tcp':
        counters['tcp_payload_bytes'] += len(packet.payload)
        try:
            yield reassembler.add_tcp(packet, now)
        except ValueError as exc:
            raise PassiveStateError(str(exc)) from None
        return
    if packet.protocol != 'udp':
        return
    segments = shadow_capture.parse_kcp_segments(packet.payload)
    counters['kcp_datagrams' if segments else 'non_kcp_datagrams'] += 1
    for item in segments:
        counters[f"kcp_{str(item['command']).casefold()}"] += 1
        if item.get('command') != 'PUSH':
            continue
        offset, length = int(item['payload_offset']), int(item['payload_length'])
        yield reassembler.add(PushSegment(
            flow=(local_port, remote_ip, remote_port, int(item['conv']), local_host),
            sequence=int(item['sequence']), timestamp_epoch=packet.timestamp_ns/1e9,
            timestamp_ns=packet.timestamp_ns,
            ciphertext=bytes(packet.payload[offset:offset+length]),
        ), now)


def _wait_for_new_connection(stop_event, output_queue, watchdog, runtime_expiry,
                             receiver, packets, reassembler, pid, session_id,
                             capture_source=CAPTURE_SOURCE_WINDOWS_RAW,
                             retry_initial_state=False):
    """Park a failed connection without rereading memory or closing pcap.

    OS endpoint changes and receive-side transport identities are evidence.
    Time passing, more ciphertext on the old flow and unrelated HTTPS
    connections are not evidence that this game stream has been replaced.
    The exception is a decoder that was never armed during initial startup:
    it can periodically retry a bounded snapshot of the current connection.
    """
    active = reassembler.active_flow
    protocol = reassembler.protocol
    active_seen_at = reassembler.udp.last_seen_monotonic.get(active, 0.0)
    known_udp = {
        flow for flow in reassembler.udp.flows
        if flow == active
        or reassembler.udp.last_seen_monotonic.get(flow, 0.0) <= active_seen_at
    }
    known_tcp = {
        (row.remote_address, row.remote_port, row.local_address, row.local_port)
        for row in receiver.endpoints if getattr(row, 'protocol', 'udp') == 'tcp'
    }
    service = (active[1], active[2]) if active is not None else None
    known_syn = {
        (key, value.syn_sequence) for key, value in reassembler.tcp.transport.flows.items()
        if value.syn_sequence is not None
    }
    candidates = {}
    next_refresh = 0.0
    next_heartbeat = 0.0
    next_initial_retry = (
        time.monotonic() + INITIAL_STATE_RECOVERY_SECONDS
        if retry_initial_state else float('inf')
    )
    missing_since = None
    while not _should_stop(stop_event, watchdog, runtime_expiry):
        now = time.monotonic()
        if now >= next_refresh:
            current = shadow_capture.list_game_endpoints(pid)
            if current:
                receiver.refresh(current)
            if shadow_capture.process_path(pid):
                missing_since = None
            else:
                missing_since = now if missing_since is None else missing_since
                if now-missing_since >= PROCESS_EXIT_GRACE_SECONDS:
                    return 'game_exited'
            next_refresh = now+ENDPOINT_REFRESH_SECONDS
        if now >= next_heartbeat:
            _put(output_queue, 'state', {
                'stage': (
                    'npcap_waiting_connection_state'
                    if retry_initial_state else 'npcap_waiting_connection_change'
                ), 'game_pid': pid, 'process_found': True,
                **_capture_state_fields(capture_source),
                'capture_protocol_ready': False, 'capture_transport': protocol,
                'bootstrap_attempts_for_connection': 1,
                'same_connection_memory_rescans': 0,
            })
            next_heartbeat = now+15
        if now >= next_initial_retry and missing_since is None:
            return 'retry'
        frame = receiver.next_frame()
        if frame is None:
            continue
        packet = packets.feed(frame)
        if packet is None:
            continue
        local, port, remote, remote_port, direction = endpoint_direction(
            packet, receiver.endpoints, receiver.local_addresses)
        if direction != 'inbound':
            continue
        if service is not None and remote_port != service[1]:
            # Don't reboot a failed game stream because a game-owned HTTPS
            # connection opens or closes in the background.
            continue
        changed = False
        if packet.protocol == 'udp':
            for item in shadow_capture.parse_kcp_segments(packet.payload):
                if item.get('command') != 'PUSH':
                    continue
                flow = (port, remote, remote_port, int(item['conv']), local)
                if flow in known_udp:
                    continue
                if active is None and not known_udp:
                    # No observed old UDP identity (e.g. bootstrap failed
                    # before the first packet): establish it, don't call an
                    # ordinary packet a new connection.
                    known_udp.add(flow)
                    continue
                evidence = candidates.setdefault(flow, set())
                evidence.add(int(item['sequence']))
                if len(evidence) >= ALIGNMENT_VALID_PUSHES:
                    changed = True
        elif packet.protocol == 'tcp':
            identity = (remote, remote_port, local, port)
            syn = bool(packet.flags & 2)
            if syn and (packet.direction_key, packet.sequence) not in known_syn:
                changed = True
            elif identity not in known_tcp and packet.payload:
                evidence = candidates.setdefault(identity, set())
                evidence.add(packet.sequence)
                changed = len(evidence) >= 2
        if changed:
            _put(output_queue, 'capture_gap', {
                'reason': 'connection_changed', 'capture_source': capture_source,
                'previous_flow': list(active or ()),
                'next_endpoint': [packet.protocol, local, port, remote, remote_port],
            })
            return 'connection_changed'
    return 'stopped'


def _session(
    stop_event,
    output_queue,
    watchdog: ParentProcessWatchdog,
    runtime_expiry,
    team_stats_mode,
    wpcap,
    session_id: int,
    target_profile=None,
    team_profile_roster_queue=None,
    capture_source: str = CAPTURE_SOURCE_WINDOWS_RAW,
    state_address_hints: list[tuple[int, int, int, int]] | None = None,
    startup_retry: bool = False,
    same_connection_recovery_used: bool = False,
) -> str:
    discovery = argparse.Namespace(pid=None, process_name="C7-Win64-Shipping.exe", include_tcp=True)
    try:
        pid, executable, endpoints = shadow_capture.discover_game(discovery)
    except RuntimeError as exc:
        if "No running" in str(exc):
            return "game_not_found"
        raise

    ports = sorted({int(row.local_port) for row in endpoints if row.local_port})
    receiver = None
    state_readers: list[SessionStateReader] = []
    metadata_reader = None
    team_profile_poller = None
    state_reader_index = 0
    reassembler = PassiveGameRc4Reassembler()
    decoder = None
    counters = Counter()
    pending_records, pending_metadata, pending_team_profiles, pending_gaps = [], [], [], []
    batch_id = 0
    last_response_filetime = 0
    data_incomplete = False
    pcap_diagnostic = {'available': False}
    capture_ready_emitted = False
    capture_ready_at = 0.0
    gameplay_stream_seen = False
    next_background_probe = float('inf')
    try:
        if target_profile is not None:
            from npcap_entity_metadata import (
                PassiveEntityMetadataReader,
                PassiveTeamProfilePoller,
            )
            metadata_reader = PassiveEntityMetadataReader(pid, target_profile)
            try:
                game_root = Path(executable).resolve().parents[2]
                startup_local_role = latest_main_player_role(
                    game_root / "Saved" / "Logs" / "C7.log"
                )
            except (IndexError, OSError):
                startup_local_role = None

            def publish_startup_self_profile(record):
                _put(
                    output_queue,
                    "startup_self_profile",
                    {**record, "game_pid": pid},
                )

            team_profile_poller = PassiveTeamProfilePoller(
                pid,
                module_name=str(
                    target_profile.get(
                        "game_module", "C7-Win64-Shipping.exe"
                    )
                ),
                startup_profile_callback=publish_startup_self_profile,
            ).start()
            if startup_local_role is not None:
                team_profile_poller.request((startup_local_role[0],))
        else:
            startup_local_role = None
        # Entity identity/templates now arrive as passive creation records.
        # No persistent game-memory reader is attached to the runtime loop.
        _put(
            output_queue,
            "state",
            {
                "stage": "locating_decryption_state",
                "process_found": True,
                "game_pid": pid,
                **_capture_state_fields(capture_source),
                "startup_self_token": (
                    startup_local_role[0] if startup_local_role is not None else ""
                ),
                "startup_self_actor_id": (
                    startup_local_role[1] if startup_local_role is not None else 0
                ),
            },
        )
        # Opening the receive-only transport and locating the detached RC4/Zstd
        # state can take several seconds. The profile poller is independent of
        # that transport work, so let it identify the local role immediately
        # instead of making the first HUD row wait for capture initialization.
        receiver = _open_packet_receiver(capture_source, endpoints, wpcap=wpcap)
        packets = PacketReassembler()
        state_readers, locate_diagnostic, endpoints = _bootstrap_initial_state(
            pid,
            receiver,
            stop_event,
            watchdog,
            runtime_expiry,
            output_queue,
            tuple(state_address_hints or ()),
            max_attempts=1 if startup_retry else INITIAL_BOOTSTRAP_MAX_ATTEMPTS,
        )
        _remember_state_address_hints(state_address_hints, pid, state_readers)
        ports = sorted({int(row.local_port) for row in endpoints if row.local_port})
        if not state_readers:
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "npcap_stream_state_unavailable",
                    "details": "未能读取当前连接的解密状态，将自动重试。",
                    **_capture_state_fields(capture_source),
                    **locate_diagnostic,
                },
            )
            raise PassiveStateError('No copied state; passive capture stopped')

        # Keep one bounded sample per unknown method ID.  Final combat
        # statistics changed wire IDs during testing; without these samples a
        # complete capture looks identical to a server that sent no result.
        decoder = NpcapProtocolDecoder(
            capture_unknown=os.environ.get('GMZZ_NPCAP_CAPTURE_UNKNOWN', '1') != '0',
            capture_unknown_timeline=(
                os.environ.get('GMZZ_NPCAP_CAPTURE_UNKNOWN_TIMELINE', '0') == '1'
            ),
            # Current arena result/detail callbacks sit next to the verified
            # 799/824 roster methods. Preserve their full low-volume timeline
            # even after the global one-sample budget is exhausted, so a late
            # match result cannot disappear from diagnostics again.
            capture_unknown_method_ids=(796, 797, 798, 802, 803),
        )
        installed = False
        for state_reader_index, state_reader in enumerate(state_readers):
            try:
                _install_state_snapshot(state_reader, reassembler, decoder)
            except (OSError, RuntimeError, ValueError):
                continue
            installed = True
            break
        if not installed:
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "npcap_stream_snapshot_unavailable",
                    "details": "Unable to synchronize the current connection; retrying.",
                    **_capture_state_fields(capture_source),
                    **locate_diagnostic,
                },
            )
            raise PassiveStateError('Copied state could not be installed; passive capture stopped')
        counters: Counter[str] = Counter()
        counters["server_requests_added"] = 0
        counters['bootstrap_attempts_for_connection'] = int(
            locate_diagnostic.get('bootstrap_attempts_for_connection', 1)
        )
        counters['same_connection_memory_rescans'] = int(
            locate_diagnostic.get('same_connection_memory_rescans', 0)
        )
        counters['owned_udp_endpoints'] = sum(getattr(row, 'protocol', 'udp') == 'udp' for row in endpoints)
        counters['owned_tcp_endpoints'] = sum(getattr(row, 'protocol', 'udp') == 'tcp' for row in endpoints)
        counters["stream_state_candidates"] = len(state_readers)
        pending_records: list[dict] = []
        pending_metadata: list[dict] = []
        pending_team_profiles: list[dict] = []
        pending_gaps: list[dict] = []
        batch_id = 0
        next_flush = time.monotonic() + BATCH_INTERVAL_SECONDS
        next_diagnostic_heartbeat = time.monotonic() + 1.0
        next_endpoint_refresh = time.monotonic() + ENDPOINT_REFRESH_SECONDS
        next_anchor_retry = time.monotonic() + ALIGNMENT_RETRY_SECONDS
        process_missing_since: float | None = None
        data_incomplete = False
        last_response_filetime = 0
        last_pcap_dropped = 0
        pcap_diagnostic: dict[str, object] = {"available": False}
        next_pcap_stats = time.monotonic()
        last_protocol_frame = time.monotonic()
        pushes_without_protocol_frame = 0
        traffic_wait_emitted = False
        initialization_started = time.monotonic()
        pre_ready_state_refreshes = 0

        def refresh_pre_ready_state(reason: str) -> bool:
            nonlocal state_readers, state_reader_index, endpoints, ports
            nonlocal next_anchor_retry, pushes_without_protocol_frame
            nonlocal last_protocol_frame, pre_ready_state_refreshes
            if (
                capture_ready_emitted
                or pre_ready_state_refreshes >= PRE_READY_STATE_REFRESH_LIMIT
            ):
                return False
            pre_ready_state_refreshes += 1
            _put(
                output_queue,
                "state",
                {
                    "stage": "npcap_refreshing_initial_state",
                    "details": str(reason),
                    "process_found": True,
                    "game_pid": pid,
                    **_capture_state_fields(capture_source),
                    "capture_protocol_ready": False,
                    "bootstrap_attempts_for_connection": int(
                        counters.get("bootstrap_attempts_for_connection", 1)
                    ),
                    "same_connection_memory_rescans": int(
                        counters.get("same_connection_memory_rescans", 0)
                    ),
                },
            )
            replacements, diagnostic, refreshed_endpoints = _bootstrap_initial_state(
                pid,
                receiver,
                stop_event,
                watchdog,
                runtime_expiry,
                output_queue,
            )
            _remember_state_address_hints(
                state_address_hints, pid, replacements
            )
            decoder.reset_transport()
            reassembler.invalidate()
            replacement_index = -1
            for index, replacement in enumerate(replacements):
                try:
                    _install_state_snapshot(
                        replacement, reassembler, decoder, time.monotonic()
                    )
                except (OSError, RuntimeError, ValueError):
                    continue
                replacement_index = index
                break
            if replacement_index < 0:
                _close_state_readers(replacements)
                raise PassiveStateError(
                    "Fresh initial connection state could not be installed"
                )
            _close_state_readers(state_readers)
            state_readers = replacements
            state_reader_index = replacement_index
            endpoints = list(refreshed_endpoints)
            ports = sorted(
                {int(row.local_port) for row in endpoints if row.local_port}
            )
            attempts = int(
                diagnostic.get("bootstrap_attempts_for_connection", 1) or 1
            )
            counters["bootstrap_attempts_for_connection"] += attempts
            counters["same_connection_memory_rescans"] += attempts
            counters["pre_ready_state_refreshes"] += 1
            counters["stream_state_candidates"] += len(replacements)
            now = time.monotonic()
            next_anchor_retry = now + ALIGNMENT_RETRY_SECONDS
            pushes_without_protocol_frame = 0
            last_protocol_frame = now
            return True

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
                "damage_source": capture_source,
                **_capture_state_fields(capture_source),
                "npcap_adapters": list(receiver.handles),
                "npcap_local_addresses": sorted(receiver.local_addresses),
                "npcap_game_ports": ports,
                "capture_interfaces": list(receiver.handles),
                "capture_local_addresses": sorted(receiver.local_addresses),
                "capture_game_ports": ports,
                "capture_timestamp_resolution_ns": {
                    name: value[0].timestamp_resolution_ns for name, value in receiver.handles.items()
                },
                "rc4_read_mode": "query_and_read_only",
                "zstd_read_mode": "query_and_read_only",
                "server_requests_added": 0,
                "rc4_locator": locate_diagnostic,
            },
        )

        while not _should_stop(stop_event, watchdog, runtime_expiry):
            now = time.monotonic()
            if (not capture_ready_emitted and not traffic_wait_emitted
                    and now-initialization_started >= 10
                    and not reassembler.flows):
                _put(output_queue, 'capture_error', {
                    'stage': 'npcap_waiting_game_traffic',
                    'details': '尚未捕获可用游戏流量；保持被动监听，不重复初始化当前连接。',
                    'game_pid': pid, 'session_id': session_id,
                    **_capture_state_fields(capture_source), 'recoverable': True,
                    'awaiting_game_traffic': True,
                    'requires_new_connection_initialization': False,
                })
                traffic_wait_emitted = True
            protocol_state_resync = False

            frame = receiver.next_frame()
            if frame is not None:
                packet = packets.feed(frame)
                if packet is None:
                    counters["unparsed_packets"] += 1
                else:
                    if packet.protocol in ('udp', 'tcp'):
                        was_aligned = reassembler.aligned
                        for decrypted in _passive_payloads(
                            reassembler, packet, receiver.endpoints,
                            receiver.local_addresses, now, counters,
                        ):
                            if not was_aligned and reassembler.aligned:
                                counters["stream_state_alignments"] += 1
                                counters['capture_transport_tcp'] = int(reassembler.protocol == 'tcp')
                                _put(output_queue, 'state', {
                                    'stage': 'npcap_protocol_validating', 'game_pid': pid,
                                    **_capture_state_fields(capture_source), 'capture_transport': reassembler.protocol,
                                })
                            was_aligned = reassembler.aligned
                            for value in decrypted:
                                if not value.after_anchor:
                                    counters["pre_snapshot_pushes_skipped"] += 1
                                    continue
                                frames_before = decoder.diagnostics.doraemon_frames
                                messages_before = (
                                    decoder.diagnostics.application_messages
                                )
                                try:
                                    records = decoder.feed_push(
                                        value.plaintext,
                                        value.sequence,
                                        value.timestamp_epoch,
                                        timestamp_ns=value.timestamp_ns,
                                    )
                                except (ValueError, OverflowError):
                                    raise PassiveStateError('Invalid passive protocol stream; capture stopped') from None
                                if (
                                    decoder.diagnostics.doraemon_frames
                                    > frames_before
                                ):
                                    last_protocol_frame = now
                                    pushes_without_protocol_frame = 0
                                else:
                                    pushes_without_protocol_frame += 1
                                if decoder.consume_state_resync_request():
                                    if not capture_ready_emitted and state_reader_index+1 < len(state_readers):
                                        # Try only an already-detached candidate. Never
                                        # rescan the same connection after Zstd rejection.
                                        state_reader_index += 1
                                        counters['stream_state_candidate_rotations'] += 1
                                        _install_state_snapshot(state_readers[state_reader_index], reassembler, decoder, now)
                                        was_aligned = False
                                        next_anchor_retry = now+ALIGNMENT_RETRY_SECONDS
                                        pushes_without_protocol_frame = 0
                                        last_protocol_frame = now
                                        break
                                    if refresh_pre_ready_state(
                                        "Initial decompression candidate was rejected"
                                    ):
                                        protocol_state_resync = True
                                        break
                                    raise PassiveStateError('Decompression state rejected; no memory re-read')
                                if not gameplay_stream_seen and any(
                                    record.get('method') in GAMEPLAY_STREAM_METHODS
                                    for record in records
                                ):
                                    gameplay_stream_seen = True
                                    if len(state_readers) > 1:
                                        selected = state_readers[state_reader_index]
                                        _close_state_readers(
                                            reader for index, reader in enumerate(state_readers)
                                            if index != state_reader_index
                                        )
                                        state_readers = [selected]
                                        state_reader_index = 0
                                if (
                                    not capture_ready_emitted
                                    and decoder.diagnostics.application_messages
                                    > messages_before
                                ):
                                    # Installed RC4/Zstd snapshots are only
                                    # candidates until one complete application
                                    # message validates the passive stream and
                                    # the decoder confirms it did not reject the
                                    # copied compression state.
                                    _put(
                                        output_queue,
                                        "capture_ready",
                                        {
                                            "session_id": session_id,
                                            "game_pid": pid,
                                            **_capture_state_fields(capture_source),
                                            "capture_transport": reassembler.protocol,
                                            "protocol_application_messages": (
                                                decoder.diagnostics.application_messages
                                            ),
                                        },
                                    )
                                    capture_ready_emitted = True
                                    capture_ready_at = time.monotonic()
                                    next_background_probe = (
                                        capture_ready_at + BACKGROUND_STREAM_PROBE_SECONDS
                                    )
                                    reassembler.confirm_alignment()
                                for record in records:
                                    record['npcap_transport'] = reassembler.protocol
                                    if reassembler.protocol == 'tcp':
                                        record['npcap_tcp_byte_offset'] = record.pop('npcap_kcp_sequence', value.sequence)
                                    if team_profile_poller is not None:
                                        tokens = live_team_profile_tokens(record)
                                        if tokens:
                                            team_profile_poller.request(tokens)
                                    if metadata_reader is not None:
                                        arguments = record.get('decoded_arguments', [])
                                        if (
                                            record.get('method') == 'NpcapEntityCreated'
                                            and arguments
                                            and isinstance(arguments[0], dict)
                                            and arguments[0].get('entity_class') == 'NpcActor'
                                        ):
                                            properties = arguments[0].get('properties', {})
                                            if isinstance(properties, dict):
                                                arguments[0].update(
                                                    metadata_reader.validated_damage_target_metadata(
                                                        properties.get('TemplateID')
                                                    )
                                                )
                                        _request_entity_metadata(
                                            metadata_reader, record
                                        )
                                    if record.get("method") in SCENE_TRANSITION_METHODS:
                                        # A map transition is still data in the
                                        # current KCP/RC4/Zstd stream. Resetting
                                        # it here discards the arriving roster
                                        # and entity initialization burst.
                                        counters['scene_transitions_preserved'] += 1
                                        if metadata_reader is not None:
                                            metadata_reader.reset()
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
                            if protocol_state_resync:
                                break

            now = time.monotonic()
            if (
                capture_ready_emitted and not gameplay_stream_seen
                and now >= next_background_probe
                and state_reader_index + 1 < len(state_readers)
                and reassembler.protocol == 'udp'
            ):
                alternate = _alternate_udp_state_candidate(
                    reassembler, state_readers, state_reader_index, now
                )
                next_background_probe = now + BACKGROUND_STREAM_PROBE_SECONDS
                if alternate is not None:
                    state_reader_index = alternate
                    _install_state_snapshot(
                        state_readers[alternate], reassembler, decoder, now
                    )
                    counters['stream_background_candidate_rotations'] += 1
                    pushes_without_protocol_frame = 0
                    last_protocol_frame = now
                    next_anchor_retry = now + ALIGNMENT_RETRY_SECONDS
            if metadata_reader is not None:
                pending_metadata.extend(metadata_reader.poll())
            if team_profile_poller is not None:
                roster = _latest_team_profile_roster(team_profile_roster_queue)
                if roster is not None:
                    team_profile_poller.replace(roster)
                pending_team_profiles.extend(team_profile_poller.poll())
                counters.update(team_profile_poller.take_counters())
                profile_error = team_profile_poller.take_error()
                if profile_error:
                    counters["live_team_profile_reader_failures"] += 1
            protocol_stalled = _protocol_stream_stalled(
                aligned=reassembler.aligned,
                pushes_without_frame=pushes_without_protocol_frame,
                last_frame_monotonic=last_protocol_frame,
                now=now,
            )
            if protocol_stalled:
                if refresh_pre_ready_state(
                    "Initial protocol candidate did not produce valid messages"
                ):
                    continue
                raise PassiveStateError('Passive protocol stalled; no automatic memory refresh')

            gap = reassembler.pending_gap(now)
            if reassembler.needs_resync(now):
                replacement = reassembler.fresh_alternate_flow(now)
                if replacement is not None:
                    _put(output_queue, 'capture_gap', {
                        'reason':'connection_changed',
                        'previous_flow':list(reassembler.active_flow or ()),
                        'next_flow':list(replacement),
                        'capture_source':capture_source,
                    })
                    return 'connection_changed'
                if gap is not None:
                    _put(output_queue, 'capture_gap', {'expected':int(gap['expected']),
                         'actual':int(gap['actual']),'reason':'network_sequence_gap',
                         'capture_source':capture_source})
                    raise PassiveStateError('Passive stream gap; capture stopped without rereading the same connection')
                raise PassiveStateError('Passive stream state expired; capture stopped')

            alignment_stale = reassembler.alignment_stale(now)
            if (
                (reassembler.anchor is None and now >= next_anchor_retry)
                or alignment_stale
            ):
                stale_alignment = alignment_stale
                try:
                    if stale_alignment:
                        state_reader_index += 1
                        counters["stream_state_candidate_rotations"] += 1
                    if state_reader_index >= len(state_readers):
                        if refresh_pre_ready_state(
                            "Initial RC4 candidate could not align with live traffic"
                        ):
                            continue
                        raise PassiveStateError('Copied candidates exhausted; no automatic memory rescan')
                    if not state_readers:
                        raise RuntimeError("connection state is unavailable")
                    _install_state_snapshot(
                        state_readers[state_reader_index],
                        reassembler,
                        decoder,
                        now,
                    )
                    pushes_without_protocol_frame = 0
                    last_protocol_frame = now
                    counters["rc4_reanchors"] += 1
                except PassiveStateError:
                    raise
                except (OSError, RuntimeError, ValueError):
                    counters["rc4_reanchor_errors"] += 1
                    reassembler.invalidate()
                    decoder.reset_transport()
                    state_reader_index += 1
                next_anchor_retry = now + ALIGNMENT_RETRY_SECONDS

            if now >= next_endpoint_refresh:
                current = shadow_capture.list_game_endpoints(pid)
                current_ports = sorted(
                    {int(row.local_port) for row in current if row.local_port}
                )
                if current_ports:
                    process_missing_since = None
                    receiver.refresh(current)
                    active = reassembler.active_flow
                    if active is not None and not any(
                        getattr(row, 'protocol', 'udp') == reassembler.protocol
                        and row.local_port == active[0]
                        and row.local_address in (active[4], '0.0.0.0', '::')
                        and (reassembler.protocol != 'tcp'
                             or (row.remote_address, row.remote_port) == (active[1], active[2]))
                        for row in current
                    ):
                        _put(output_queue, 'capture_gap', {
                            'reason': 'connection_changed', 'previous_flow': list(active),
                            'capture_source': capture_source,
                        })
                        return 'connection_changed'
                    if current_ports != ports:
                        old_ports = ports
                        ports = current_ports
                        counters["capture_filter_updates"] += 1
                        active = reassembler.active_flow
                        if active is not None and active[0] not in ports:
                            _put(output_queue, 'capture_gap', {
                                'reason':'connection_changed',
                                'previous_flow':list(active),
                                'npcap_old_ports':old_ports,
                                'npcap_game_ports':ports,
                                'capture_source':capture_source,
                            })
                            return 'connection_changed'
                        _put(
                            output_queue,
                            "state",
                            {
                                "stage": "capturing",
                                "process_found": True,
                                "game_pid": pid,
                                **_capture_state_fields(capture_source),
                                "npcap_old_ports": old_ports,
                                "npcap_game_ports": ports,
                            },
                        )
                else:
                    if shadow_capture.process_path(pid):
                        process_missing_since = None
                    else:
                        process_missing_since = now if process_missing_since is None else process_missing_since
                        if now-process_missing_since >= PROCESS_EXIT_GRACE_SECONDS:
                            return "game_exited"
                next_endpoint_refresh = now + ENDPOINT_REFRESH_SECONDS

            if now >= next_pcap_stats:
                pcap_diagnostic = receiver.statistics(_pcap_stats)
                dropped = int(pcap_diagnostic.get("dropped", 0) or 0)
                if dropped > last_pcap_dropped:
                    counters["pcap_dropped"] += dropped - last_pcap_dropped
                    last_pcap_dropped = dropped
                    data_incomplete = True
                next_pcap_stats = now + 1.0

            if (
                now >= next_flush
                or len(pending_records) >= BATCH_RECORD_LIMIT
                or pending_team_profiles
                or pending_gaps
            ):
                counters.update(reassembler.counters)
                if decoder.unknown_records:
                    unknown = list(decoder.unknown_records)
                    for record in unknown:
                        record['npcap_transport'] = reassembler.protocol
                        if reassembler.protocol == 'tcp':
                            record['npcap_tcp_byte_offset'] = record.pop('npcap_kcp_sequence', 0)
                    _put(output_queue, 'protocol_unknown', unknown)
                    decoder.unknown_records.clear()
                reassembler.counters.clear()
                counters.update(packets.diagnostics)
                packets.diagnostics.clear()
                receive_counters = receiver.take_counters()
                counters.update(receive_counters)
                if (receive_counters.get('capture_adapter_errors', 0)
                        or receive_counters.get('capture_queue_dropped', 0)
                        or receive_counters.get('raw_socket_truncated_packets', 0)):
                    data_incomplete = True
                if metadata_reader is not None:
                    counters.update(metadata_reader.counters)
                    metadata_reader.counters.clear()
                counters["protocol_decrypted_pushes"] = (
                    decoder.diagnostics.decrypted_pushes
                )
                counters["protocol_doraemon_frames"] = (
                    decoder.diagnostics.doraemon_frames
                )
                counters["protocol_application_messages"] = (
                    decoder.diagnostics.application_messages
                )
                counters["protocol_unsupported_application_messages"] = (
                    decoder.diagnostics.unsupported_application_messages
                )
                counters["protocol_application_skipped_bytes"] = (
                    decoder.diagnostics.application_skipped_bytes
                )
                counters["protocol_application_buffer_bytes"] = len(
                    decoder.application
                )
                if len(decoder.application) >= 6:
                    counters["protocol_application_head_size"] = int.from_bytes(
                        decoder.application[:4], "little"
                    )
                    counters["protocol_application_head_type"] = int.from_bytes(
                        decoder.application[4:6], "little"
                    )
                counters["protocol_frame_buffer_bytes"] = len(
                    decoder.frames.buffer
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
                counters["protocol_native_zstd_restores"] = (
                    decoder.diagnostics.native_zstd_restores
                )
                counters["protocol_native_zstd_errors"] = (
                    decoder.diagnostics.native_zstd_errors
                )
                counters["protocol_native_zstd_validations"] = (
                    decoder.diagnostics.native_zstd_validations
                )
                counters["protocol_native_zstd_rejections"] = (
                    decoder.diagnostics.native_zstd_rejections
                )
                counters["protocol_zstd_candidate_attempts"] = (
                    decoder.diagnostics.zstd_candidate_attempts
                )
                counters["protocol_zstd_candidate_rejections"] = (
                    decoder.diagnostics.zstd_candidate_rejections
                )
                counters["protocol_zstd_candidate_validations"] = (
                    decoder.diagnostics.zstd_candidate_validations
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
                    metadata_records=pending_metadata,
                    team_profile_records=pending_team_profiles,
                    heartbeat=now >= next_diagnostic_heartbeat,
                    capture_source=capture_source,
                ):
                    batch_id += 1
                    next_diagnostic_heartbeat = now + 1.0
                pending_records = []
                pending_metadata = []
                pending_team_profiles = []
                pending_gaps = []
                next_flush = now + BATCH_INTERVAL_SECONDS

        return "stopped"
    except PassiveStateError as exc:
        if receiver is None:
            raise
        if "handle cleanup failed" in str(exc).casefold():
            raise
        # Emit collected records before parking so a failure cannot discard
        # the final partial batch or pending settlement evidence.
        counters.update(reassembler.counters)
        _emit_batch(output_queue, session_id=session_id, batch_id=batch_id,
                    game_pid=pid, records=pending_records, gaps=pending_gaps,
                    counters=counters, pcap_diagnostic=pcap_diagnostic,
                    team_stats_mode=team_stats_mode,
                    last_response_filetime=last_response_filetime,
                    data_incomplete=data_incomplete, metadata_records=pending_metadata,
                    team_profile_records=pending_team_profiles, heartbeat=True,
                    capture_source=capture_source)
        pending_records, pending_metadata, pending_team_profiles, pending_gaps = [], [], [], []
        # A snapshot may exist but still fail alignment before the first
        # confirmed protocol frame. That is initial startup, not a previously
        # working session whose failed transport must wait for reconnection.
        initial_state_unavailable = not capture_ready_emitted
        _put(output_queue, 'capture_error', {
            'stage': ('npcap_waiting_connection_state' if initial_state_unavailable
                      else 'npcap_waiting_connection_change'), 'details': str(exc),
            'game_pid': pid, 'session_id': session_id,
            **_capture_state_fields(capture_source), 'capture_transport': reassembler.protocol,
            'recoverable': True,
            'awaiting_initial_state': initial_state_unavailable,
            'awaiting_connection_change': not initial_state_unavailable,
            'requires_new_connection_initialization': False,
            'same_connection_memory_rescans': 0,
        })
        _close_state_readers(state_readers)
        state_readers = []
        if decoder is not None:
            decoder.reset_transport()
        if (
            _can_recover_stream_gap(
                capture_ready_at, time.monotonic(), same_connection_recovery_used
            )
            and str(exc).startswith('Passive stream gap;')
            and shadow_capture.process_path(pid)
        ):
            # A dropped packet is an incomplete session. A bounded read-only
            # snapshot can resume later packets; repeated losses need a stable
            # validated run first. The missing span remains a capture gap.
            return 'recoverable_gap'
        return _wait_for_new_connection(
            stop_event, output_queue, watchdog, runtime_expiry, receiver, packets,
            reassembler, pid, session_id, capture_source,
            retry_initial_state=initial_state_unavailable)
    finally:
        if pending_records or pending_metadata or pending_team_profiles or pending_gaps:
            _emit_batch(output_queue, session_id=session_id, batch_id=batch_id,
                        game_pid=pid, records=pending_records, gaps=pending_gaps,
                        counters=counters, pcap_diagnostic=pcap_diagnostic,
                        team_stats_mode=team_stats_mode,
                        last_response_filetime=last_response_filetime,
                        data_incomplete=data_incomplete, metadata_records=pending_metadata,
                        team_profile_records=pending_team_profiles,
                        capture_source=capture_source)
        if decoder is not None:
            decoder.reset_transport()
        _close_state_readers(state_readers)
        if metadata_reader is not None:
            metadata_reader.close()
        if team_profile_poller is not None:
            team_profile_poller.close()
        if receiver is not None:
            receiver.close()


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
    target_profile=None,
    team_profile_roster_queue=None,
    capture_source: str = CAPTURE_SOURCE_WINDOWS_RAW,
    allow_npcap_fallback: bool = False,
) -> None:
    if os.name != "nt":
        raise RuntimeError("Passive packet capture is available only on Windows")
    capture_source = normalize_capture_source(capture_source)
    # ``allow_npcap_fallback`` remains in the call signature for old worker
    # payloads, but is intentionally ignored: no alternate driver can be
    # loaded by the production process.
    del allow_npcap_fallback
    wpcap = None

    session_id = 0
    # Keep verified object addresses only inside this capture child.  A new
    # transport owned by the same game process can usually reopen these exact
    # objects immediately; _bootstrap_initial_state still validates them and
    # falls back to the ordinary bounded scan when they are stale.
    state_address_hints: list[tuple[int, int, int, int]] = []
    startup_retry = False
    same_connection_recovery_used = False
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
                **_capture_state_fields(capture_source),
                "damage_source": capture_source,
            },
        )
        next_session_id = session_id + 1
        try:
            reason = _session(
                stop_event,
                output_queue,
                watchdog,
                runtime_expiry,
                team_stats_mode,
                wpcap,
                next_session_id,
                target_profile=target_profile,
                team_profile_roster_queue=team_profile_roster_queue,
                capture_source=capture_source,
                state_address_hints=state_address_hints,
                startup_retry=startup_retry,
                same_connection_recovery_used=same_connection_recovery_used,
            )
        except RawSocketUnavailable as exc:
            _put(output_queue, "fatal", {
                "stage": "built_in_capture_unavailable",
                "details": str(exc),
                **_capture_state_fields(capture_source, active=False),
                "fallback_attempted": False,
            })
            return
        except PassiveStateError as exc:
            # _session parks ordinary state failures and returns only after
            # observing a real transport change. An escaping failure has no
            # safe watcher; never turn it into unbounded same-stream scans.
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "passive_capture_failed",
                    "details": str(exc),
                    **_capture_state_fields(capture_source, active=False),
                    "recoverable": False,
                    "requires_new_connection_initialization": False,
                },
            )
            _put(output_queue, 'fatal', {'stage': 'passive_capture_failed',
                 'details': str(exc), **_capture_state_fields(capture_source, active=False),
                 'fallback_attempted': False})
            return
        except (OSError, RuntimeError, ValueError) as exc:
            _put(
                output_queue,
                "capture_error",
                {
                    "stage": "passive_capture_failed",
                    "details": f"{type(exc).__name__}: {exc}",
                    **_capture_state_fields(capture_source, active=False),
                },
            )
            _put(output_queue, 'fatal', {'stage':'passive_capture_failed','details':str(exc),
                  **_capture_state_fields(capture_source, active=False),'fallback_attempted':False})
            return
        else:
            if reason not in {"game_not_found", "retry"}:
                session_id = next_session_id
            startup_retry = reason == 'retry'
            if reason == 'recoverable_gap':
                same_connection_recovery_used = True
            elif reason in {'connection_changed', 'game_exited', 'game_not_found'}:
                same_connection_recovery_used = False

        if reason == "game_not_found":
            _put(
                output_queue,
                "state",
                {
                    "stage": "game_not_found",
                    "process_found": False,
                    **_capture_state_fields(capture_source),
                },
            )
        elif reason in {"game_exited", "capture_closed", "retry", "connection_changed", "recoverable_gap"}:
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
                        "hook_cleanup_components": {capture_source: True},
                        **_capture_state_fields(capture_source, active=False),
                        "damage_source": "none",
                    },
                )
            if reason == "game_exited":
                _put(output_queue, "state", {"stage": "game_exited"})

        if (
            reason != "connection_changed"
            and not _should_stop(stop_event, watchdog, runtime_expiry)
        ):
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
    team_profile_roster_queue=None,
    capture_source: str = CAPTURE_SOURCE_WINDOWS_RAW,
    allow_npcap_fallback: bool = False,
) -> None:
    del target_boss_lookup_event  # Passive capture observes all server records.
    watchdog = ParentProcessWatchdog(parent_pid)
    parent_alive = watchdog.is_alive()
    runtime_lease = None
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
                **_capture_state_fields(capture_source),
                **priority,
            },
        )
        _capture_forever(
            stop_event,
            output_queue,
            watchdog,
            runtime_lease,
            team_stats_mode,
            target_profile=capability.profile,
            team_profile_roster_queue=team_profile_roster_queue,
            capture_source=capture_source,
            allow_npcap_fallback=allow_npcap_fallback,
        )
    except RuntimeCapabilityError as exc:
        _put(output_queue, "fatal", f"runtime capability rejected: {exc}")
    except BaseException:
        _put(output_queue, "fatal", traceback.format_exc())
    finally:
        parent_alive = watchdog.is_alive()
        stop_event_set = bool(stop_event.is_set())
        if not parent_alive:
            stop_reason = "parent_process_exited"
        elif stop_event_set:
            stop_reason = "stop_event"
        elif not _runtime_active(runtime_lease or runtime_expiry):
            stop_reason = "runtime_capability_expired"
        else:
            stop_reason = "capture_loop_returned"
        watchdog.close()
        try:
            _put(
                output_queue,
                "stopped",
                {
                    "reason": stop_reason,
                    "parent_alive": parent_alive,
                    "stop_event_set": stop_event_set,
                    "runtime_expiry": float(
                        (runtime_lease.value if runtime_lease is not None else runtime_expiry)
                    ),
                },
            )
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

    capture_source = CAPTURE_SOURCE_WINDOWS_RAW

    def __init__(
        self,
        *,
        runtime_capability,
        allow_development: bool = False,
        trusted_public_keys=None,
        parent_pid: int | None = None,
        target_boss_lookup_enabled: bool = False,
        team_stats_mode: object = TEAM_STATS_MODE_TEAM,
        capture_source: object = None,
        allow_npcap_fallback: bool = False,
    ):
        # Kept only so older parent payloads remain deserializable. The
        # production client never forwards a fallback request.
        del allow_npcap_fallback
        self.capture_source = normalize_capture_source(
            self.capture_source if capture_source is None else capture_source
        )
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
        self.team_profile_roster_queue = self.context.Queue(maxsize=4)
        self.output_queue = self.context.Queue(maxsize=0)
        self.process = self.context.Process(
            name="GMZZWindowsRawCapture",
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
                self.team_profile_roster_queue,
                self.capture_source,
                False,
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

    def set_live_team_profile_tokens(self, tokens) -> tuple[str, ...]:
        cleaned = tuple(
            sorted(
                {
                    token
                    for raw in tokens
                    if ROLE_ID_RE.fullmatch(token := str(raw or "").strip())
                }
            )
        )
        try:
            self.team_profile_roster_queue.put_nowait(cleaned)
        except queue.Full:
            try:
                self.team_profile_roster_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.team_profile_roster_queue.put_nowait(cleaned)
            except queue.Full:
                pass
        return cleaned

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
            roster_queue = self.team_profile_roster_queue
            cancel_roster_join = getattr(
                roster_queue, "cancel_join_thread", None
            )
            if callable(cancel_roster_join):
                cancel_roster_join()
            roster_queue.close()
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
