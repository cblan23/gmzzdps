#!/usr/bin/env python3
"""Decode the game's inbound Npcap stream into NetworkPacketParser records.

This module contains no packet transmitter and no game hook.  It accepts
already decrypted, in-order KCP PUSH payloads, restores the Doraemon/Zstd
framing, decodes MessagePack RPC calls, and emits the same small record shape
that the existing combat parser consumes.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import msgpack
import zstandard

from npcap_zstd_state import NativeZstdDecoder, ZstdSnapshot


WINDOWS_EPOCH_SECONDS = 11_644_473_600
FILETIME_TICKS_PER_SECOND = 10_000_000
ZSTD_FRAME_HEADER = bytes.fromhex("28 b5 2f fd 00 38")
ZSTD_EMPTY_LAST_BLOCK = b"\x01\x00\x00"
RPC_NUMERIC_MESSAGE_TYPES = frozenset({18, 22})
# The same application stream also carries name-addressed RPC envelopes.  The
# legacy dispatcher exposes these calls directly (for example team position
# refreshes), so retaining them is required for the Npcap backend to preserve
# the same scene/roster evidence.
RPC_NAMED_MESSAGE_TYPES = frozenset({12, 17})
RPC_MESSAGE_TYPES = RPC_NUMERIC_MESSAGE_TYPES | RPC_NAMED_MESSAGE_TYPES
MAX_DORAEMON_FRAME_BYTES = 256 * 1024
MAX_APPLICATION_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_ZSTD_RECOVERY_CANDIDATES = 32
MAX_ZSTD_RECOVERY_FRAMES = 96
MAX_ZSTD_RECOVERY_PLAIN_BYTES = 2 * 1024 * 1024


# Verified by matching the passive Npcap payloads against decoded records from
# the same 2026-09-09 session.  A method ID can occur on several entity
# classes; every duplicate below resolved to the same method name.
METHOD_ID_NAMES: dict[int, str] = {
    19: "OnMsgSyncFightMode",
    22: "OnMsgSyncDirtyFightAttributes",
    25: "OnMsgSyncCurrentHp",
    26: "OnMsgSyncCurrentMaxHp",
    32: "OnMsgAddBuffNew",
    79: "OnMsgEndureExitHit",
    80: "OnMsgSyncCurrentHp",
    86: "OnMsgEntityRelive",
    87: "OnMsgEntityDead",
    90: "OnMsgDamageSyncV2",
    91: "OnMsgHealSyncV2",
    94: "OnMsgBeatenSyncV2",
    102: "OnMsgSyncFightMode",
    104: "OnMsgDungeonReadinessCheck",
    108: "OnMsgDungeonStageSettlement",
    118: "OnMsgActorBuffStateSync",
    131: "OnMsgCreateLUnitSpellField",
    140: "OnMsgCreateBullet",
    147: "OnMsgCastSkillNew",
    202: "OnCreateTeamSuccess",
    212: "OnUpdateTeamGroupMemberProps",
    213: "OnUpdateTeamGroupSelfProps",
    214: "OnMsgOtherJoinTeamGroup",
    217: "OnMsgBeforeEnterNewSpace",
    220: "RetGetTeamApplyDesc",
    271: "OnMsgSyncTeamGroupMemberFreqProp",
    338: "RetCommonCombatStatisticsByTeam",
    342: "OnMsgSettlementCombatStatistics",
    463: "RetGetBothWayFriends",
    464: "RetGetBothwayFriendFrequentInfo",
    508: "OnMsgActorBuffStateSync",
    1067: "OnMsgAddBuffNew",
    1081: "OnMsgCreateLUnitTrap",
    1085: "OnMsgCreateLUnitAura",
    1088: "OnMsgCreateLUnitSpellField",
    1091: "OnMsgCreateLUnitSpellAgent",
    1097: "OnMsgCreateBullet",
    1119: "OnMsgCastSkillNew",
    1121: "RetCastSkillSuccessNew",
    1127: "OnMsgSkillEnterTeamGCD",
    1381: "RetStopEditTarotTeamSceneCustom",
    1613: "OnMsgRefreshSceneObjects",
    2006: "RetGetServerLevelInfo",
    2239: "RetStopEditTarotTeamSceneCustom",
    2261: "OnMsgPostAkEvent",
}


@dataclass(frozen=True)
class DecodedFrame:
    sequence: int
    timestamp_epoch: float
    data: bytes


@dataclass
class ProtocolDiagnostics:
    decrypted_pushes: int = 0
    doraemon_frames: int = 0
    zstd_resyncs: int = 0
    zstd_errors: int = 0
    application_skipped_bytes: int = 0
    application_messages: int = 0
    retained_records: int = 0
    unknown_method_messages: int = 0
    native_zstd_restores: int = 0
    native_zstd_errors: int = 0
    native_zstd_validations: int = 0
    native_zstd_rejections: int = 0
    zstd_candidate_attempts: int = 0
    zstd_candidate_rejections: int = 0
    zstd_candidate_validations: int = 0


@dataclass
class _ZstdRecoveryCandidate:
    decoder: object
    chunks: list[DecodedFrame]
    probe: bytearray
    frames: int = 0
    plain_bytes: int = 0


def epoch_to_filetime(timestamp_epoch: float) -> int:
    return int(
        (float(timestamp_epoch) + WINDOWS_EPOCH_SECONDS)
        * FILETIME_TICKS_PER_SECOND
    )


def epoch_to_event_time(timestamp_epoch: float) -> str:
    return datetime.fromtimestamp(
        float(timestamp_epoch), timezone.utc
    ).astimezone().isoformat(timespec="microseconds")


def application_frame_valid(plaintext: bytes) -> bool:
    if len(plaintext) < 3:
        return False
    bit_count = int.from_bytes(plaintext[:3], "little") & 0xFFFFF
    return bit_count == len(plaintext) * 8 - 20


def _zstd_block(data: bytes, *, last: bool) -> bytes:
    if len(data) < 3:
        raise ValueError("truncated Zstd block")
    header = int.from_bytes(data[:3], "little")
    size = header >> 3
    if len(data) != size + 3:
        raise ValueError(
            f"invalid Zstd block length: header={size}, actual={len(data) - 3}"
        )
    return ((header & ~1) | int(last)).to_bytes(3, "little") + data[3:]


def _valid_rpc_shape(value: object) -> bool:
    return bool(
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], dict)
        and isinstance(value[1], list)
        and len(value[1]) >= 2
    )


def _valid_named_method(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 96:
        return False
    return bool(
        value.isascii()
        and (value[0].isalpha() or value[0] == "_")
        and all(char.isalnum() or char == "_" for char in value)
    )


def _rpc_payload_prefix_valid(data: bytes | bytearray, offset: int) -> bool:
    """Reject accidental length/type headers found inside arbitrary payloads."""
    payload_offset = int(offset) + 6
    if payload_offset >= len(data):
        return True
    # Every verified Doraemon RPC envelope is a fixed two-element MessagePack
    # array: metadata followed by the call descriptor.
    return data[payload_offset] == 0x92


class DoraemonFrameDecoder:
    """Restore length-delimited Doraemon frames across KCP PUSH boundaries."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.origins: deque[list[float | int]] = deque()

    def reset(self) -> None:
        self.buffer.clear()
        self.origins.clear()

    def _consume(self, length: int) -> None:
        remaining = int(length)
        del self.buffer[:remaining]
        while remaining and self.origins:
            origin = self.origins[0]
            available = int(origin[0])
            if available <= remaining:
                remaining -= available
                self.origins.popleft()
            else:
                origin[0] = available - remaining
                remaining = 0

    def feed(
        self, plaintext: bytes, sequence: int, timestamp_epoch: float
    ) -> list[DecodedFrame]:
        if not plaintext:
            return []
        self.buffer.extend(plaintext)
        self.origins.append(
            [len(plaintext), int(sequence), float(timestamp_epoch)]
        )
        frames: list[DecodedFrame] = []
        while len(self.buffer) >= 3 and self.origins:
            bit_count = int.from_bytes(self.buffer[:3], "little") & 0xFFFFF
            frame_size = (20 + bit_count + 7) // 8
            if not 3 <= frame_size <= MAX_DORAEMON_FRAME_BYTES:
                raise ValueError(f"invalid Doraemon frame size: {frame_size}")
            if len(self.buffer) < frame_size:
                break
            _remaining, frame_sequence, frame_epoch = self.origins[0]
            data = bytes(self.buffer[:frame_size])
            self._consume(frame_size)
            frames.append(
                DecodedFrame(
                    sequence=int(frame_sequence),
                    timestamp_epoch=float(frame_epoch),
                    data=data,
                )
            )
        return frames


class NpcapProtocolDecoder:
    """Stateful Doraemon/Zstd/MessagePack decoder for one inbound RC4 stream."""

    def __init__(self, retained_methods: Iterable[str] | None = None) -> None:
        self.frames = DoraemonFrameDecoder()
        self.zstd = None
        self.native_zstd: NativeZstdDecoder | None = None
        self.native_zstd_validated = False
        self.native_zstd_chunks: list[DecodedFrame] = []
        self.native_zstd_probe = bytearray()
        self.native_zstd_frames = 0
        self.native_zstd_plain_bytes = 0
        self.zstd_candidates: list[_ZstdRecoveryCandidate] = []
        self.state_resync_requested = False
        self.application = bytearray()
        self.application_origins: deque[list[float | int]] = deque()
        self.record_sequence = 0
        self.diagnostics = ProtocolDiagnostics()
        self.retained_methods = (
            frozenset(str(value) for value in retained_methods)
            if retained_methods is not None
            else None
        )

    def reset_transport(self) -> None:
        self.frames.reset()
        self.zstd = None
        if self.native_zstd is not None:
            self.native_zstd.close()
            self.native_zstd = None
        self.native_zstd_validated = False
        self.native_zstd_chunks.clear()
        self.native_zstd_probe.clear()
        self.native_zstd_frames = 0
        self.native_zstd_plain_bytes = 0
        self.zstd_candidates.clear()
        self.application.clear()
        self.application_origins.clear()
        self.state_resync_requested = False

    def install_zstd_snapshot(self, snapshot: ZstdSnapshot) -> None:
        self.reset_transport()
        self.native_zstd = NativeZstdDecoder(snapshot)
        self.diagnostics.native_zstd_restores += 1

    def consume_state_resync_request(self) -> bool:
        requested = self.state_resync_requested
        self.state_resync_requested = False
        return requested

    def _reject_native_zstd(self, *, error: bool) -> None:
        if self.native_zstd is not None:
            self.native_zstd.close()
            self.native_zstd = None
        self.native_zstd_validated = False
        self.native_zstd_chunks.clear()
        self.native_zstd_probe.clear()
        self.native_zstd_frames = 0
        self.native_zstd_plain_bytes = 0
        self.application.clear()
        self.application_origins.clear()
        self.state_resync_requested = True
        if error:
            self.diagnostics.native_zstd_errors += 1
        else:
            self.diagnostics.native_zstd_rejections += 1

    @staticmethod
    def _start_zstd(block: bytes):
        decoder = zstandard.ZstdDecompressor().decompressobj()
        plain = decoder.decompress(
            ZSTD_FRAME_HEADER + _zstd_block(block, last=False)
        )
        return decoder, plain

    @staticmethod
    def _probe_rpc(data: bytearray) -> bool:
        earliest_incomplete: int | None = None
        cursor = 0
        while cursor + 6 <= len(data):
            size, message_type = struct.unpack_from("<IH", data, cursor)
            end = cursor + 4 + int(size)
            if not (
                2 <= size <= MAX_APPLICATION_MESSAGE_BYTES
                and message_type in RPC_MESSAGE_TYPES
                and _rpc_payload_prefix_valid(data, cursor)
            ):
                cursor += 1
                continue
            if end > len(data):
                if earliest_incomplete is None:
                    earliest_incomplete = cursor
                cursor += 1
                continue
            try:
                value = msgpack.unpackb(
                    bytes(data[cursor + 6 : end]),
                    raw=False,
                    strict_map_key=False,
                    unicode_errors="replace",
                )
            except (
                ValueError,
                msgpack.ExtraData,
                msgpack.FormatError,
                UnicodeDecodeError,
            ):
                cursor += 1
                continue
            if _valid_rpc_shape(value):
                call = value[1]
                if (
                    message_type in RPC_NUMERIC_MESSAGE_TYPES
                    and isinstance(call[1], int)
                ) or (
                    message_type in RPC_NAMED_MESSAGE_TYPES
                    and _valid_named_method(call[1])
                ):
                    return True
            cursor += 1
        retain_from = (
            earliest_incomplete
            if earliest_incomplete is not None
            else max(0, len(data) - 5)
        )
        if retain_from:
            del data[:retain_from]
        return False

    def _advance_candidate(
        self,
        candidate: _ZstdRecoveryCandidate,
        block: bytes,
        sequence: int,
        timestamp_epoch: float,
        *,
        first: bool = False,
    ) -> bool:
        try:
            if first:
                decoder, plain = self._start_zstd(block)
                candidate.decoder = decoder
            else:
                plain = candidate.decoder.decompress(
                    _zstd_block(block, last=False)
                )
        except (ValueError, zstandard.ZstdError):
            return False
        candidate.frames += 1
        candidate.plain_bytes += len(plain)
        chunk = DecodedFrame(
            sequence=int(sequence),
            timestamp_epoch=float(timestamp_epoch),
            data=plain,
        )
        candidate.chunks.append(chunk)
        candidate.probe.extend(plain)
        return True

    def _recover_zstd(
        self, block: bytes, sequence: int, timestamp_epoch: float
    ) -> list[DecodedFrame]:
        survivors = []
        validated: _ZstdRecoveryCandidate | None = None
        for candidate in self.zstd_candidates:
            if not self._advance_candidate(
                candidate, block, sequence, timestamp_epoch
            ):
                self.diagnostics.zstd_candidate_rejections += 1
                continue
            if self._probe_rpc(candidate.probe):
                validated = candidate
                break
            if (
                candidate.frames < MAX_ZSTD_RECOVERY_FRAMES
                and candidate.plain_bytes < MAX_ZSTD_RECOVERY_PLAIN_BYTES
            ):
                survivors.append(candidate)
            else:
                self.diagnostics.zstd_candidate_rejections += 1

        if validated is None:
            candidate = _ZstdRecoveryCandidate(
                decoder=None,
                chunks=[],
                probe=bytearray(),
            )
            try:
                started = self._advance_candidate(
                    candidate,
                    block,
                    sequence,
                    timestamp_epoch,
                    first=True,
                )
            except (ValueError, zstandard.ZstdError):
                started = False
            if started:
                self.diagnostics.zstd_candidate_attempts += 1
                if self._probe_rpc(candidate.probe):
                    validated = candidate
                elif len(survivors) < MAX_ZSTD_RECOVERY_CANDIDATES:
                    survivors.append(candidate)

        if validated is None:
            self.zstd_candidates = survivors
            return []

        self.zstd = validated.decoder
        self.zstd_candidates.clear()
        self.application.clear()
        self.application_origins.clear()
        self.diagnostics.zstd_resyncs += 1
        self.diagnostics.zstd_candidate_validations += 1
        return validated.chunks

    def _decompress(
        self, block: bytes, sequence: int, timestamp_epoch: float
    ) -> list[DecodedFrame]:
        if self.native_zstd is not None:
            try:
                plain = self.native_zstd.decompress(block)
            except (OSError, RuntimeError, ValueError):
                self._reject_native_zstd(error=True)
                return []
            else:
                chunk = DecodedFrame(
                    sequence=int(sequence),
                    timestamp_epoch=float(timestamp_epoch),
                    data=plain,
                )
                if self.native_zstd_validated:
                    return [chunk]
                self.native_zstd_frames += 1
                self.native_zstd_plain_bytes += len(plain)
                self.native_zstd_chunks.append(chunk)
                self.native_zstd_probe.extend(plain)
                if self._probe_rpc(self.native_zstd_probe):
                    chunks = self.native_zstd_chunks
                    self.native_zstd_chunks = []
                    self.native_zstd_probe.clear()
                    self.native_zstd_validated = True
                    self.diagnostics.native_zstd_validations += 1
                    return chunks
                if (
                    self.native_zstd_frames >= MAX_ZSTD_RECOVERY_FRAMES
                    or self.native_zstd_plain_bytes
                    >= MAX_ZSTD_RECOVERY_PLAIN_BYTES
                ):
                    self._reject_native_zstd(error=False)
                return []

        if self.zstd is None:
            return self._recover_zstd(block, sequence, timestamp_epoch)
        try:
            plain = self.zstd.decompress(_zstd_block(block, last=False))
        except (ValueError, zstandard.ZstdError):
            self.diagnostics.zstd_errors += 1
            self.zstd = None
            self.application.clear()
            self.application_origins.clear()
            return self._recover_zstd(block, sequence, timestamp_epoch)
        return [
            DecodedFrame(
                sequence=int(sequence),
                timestamp_epoch=float(timestamp_epoch),
                data=plain,
            )
        ]

    def _application_consume(self, length: int) -> None:
        remaining = int(length)
        del self.application[:remaining]
        while remaining and self.application_origins:
            origin = self.application_origins[0]
            available = int(origin[0])
            if available <= remaining:
                remaining -= available
                self.application_origins.popleft()
            else:
                origin[0] = available - remaining
                remaining = 0

    def _append_application(
        self, data: bytes, sequence: int, timestamp_epoch: float
    ) -> None:
        if not data:
            return
        self.application.extend(data)
        self.application_origins.append(
            [len(data), int(sequence), float(timestamp_epoch)]
        )

    def _rpc_records(self) -> list[dict]:
        records: list[dict] = []
        while len(self.application) >= 6 and self.application_origins:
            size, message_type = struct.unpack_from("<IH", self.application, 0)
            end = 4 + int(size)
            plausible = bool(
                2 <= size <= MAX_APPLICATION_MESSAGE_BYTES
                and message_type in RPC_MESSAGE_TYPES
                and _rpc_payload_prefix_valid(self.application, 0)
            )
            if not plausible:
                self._application_consume(1)
                self.diagnostics.application_skipped_bytes += 1
                continue
            if len(self.application) < end:
                break
            try:
                value = msgpack.unpackb(
                    bytes(self.application[6:end]),
                    raw=False,
                    strict_map_key=False,
                    unicode_errors="replace",
                )
            except (
                ValueError,
                msgpack.ExtraData,
                msgpack.FormatError,
                UnicodeDecodeError,
            ):
                self._application_consume(1)
                self.diagnostics.application_skipped_bytes += 1
                continue
            if not _valid_rpc_shape(value):
                self._application_consume(1)
                self.diagnostics.application_skipped_bytes += 1
                continue

            _available, kcp_sequence, timestamp_epoch = self.application_origins[0]
            self._application_consume(end)
            self.diagnostics.application_messages += 1
            call = value[1]
            method_id: int | None = None
            entity_id = 0
            if message_type in RPC_NUMERIC_MESSAGE_TYPES:
                try:
                    entity_id = int(call[0])
                    method_id = int(call[1])
                except (TypeError, ValueError, OverflowError):
                    continue
                method = METHOD_ID_NAMES.get(method_id, "")
                if not method:
                    self.diagnostics.unknown_method_messages += 1
                    continue
            elif (
                message_type in RPC_NAMED_MESSAGE_TYPES
                and _valid_named_method(call[1])
            ):
                method = str(call[1])
                try:
                    entity_id = int(call[0])
                except (TypeError, ValueError, OverflowError):
                    # Name-addressed calls commonly use a player token or an
                    # empty string rather than a numeric ScriptEntity ID.
                    # The method arguments remain useful, while zero keeps the
                    # established parser from inventing a pointer binding.
                    entity_id = 0
            else:
                continue
            if self.retained_methods is not None and method not in self.retained_methods:
                continue
            arguments = call[2] if len(call) > 2 and isinstance(call[2], list) else []
            self.record_sequence += 1
            timestamp_epoch = float(timestamp_epoch)
            record = {
                    "event_time": epoch_to_event_time(timestamp_epoch),
                    "filetime_100ns": epoch_to_filetime(timestamp_epoch),
                    "sequence": self.record_sequence,
                    "function": "npcap::doraemon::rpc",
                    # NetworkPacketParser treats this as an opaque, stable
                    # ScriptEntity key.  The wire entity ID has exactly those
                    # properties and needs no process pointer.
                    "script_entity": entity_id,
                    "network_entity_id": entity_id,
                    "method": method,
                    "decoded_arguments": arguments,
                    "decode_delay_ms": 0.0,
                    "arguments_synchronized": True,
                    "npcap_message_type": int(message_type),
                    "npcap_kcp_sequence": int(kcp_sequence),
                    "capture_source": "npcap",
                }
            if method_id is not None:
                record["npcap_method_id"] = method_id
            else:
                record["npcap_method_name"] = method
            records.append(record)
            self.diagnostics.retained_records += 1
        return records

    def feed_push(
        self, plaintext: bytes, sequence: int, timestamp_epoch: float
    ) -> list[dict]:
        self.diagnostics.decrypted_pushes += 1
        records: list[dict] = []
        for frame in self.frames.feed(plaintext, sequence, timestamp_epoch):
            self.diagnostics.doraemon_frames += 1
            for plain in self._decompress(
                frame.data, frame.sequence, frame.timestamp_epoch
            ):
                self._append_application(
                    plain.data, plain.sequence, plain.timestamp_epoch
                )
                records.extend(self._rpc_records())
        return records
