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


WINDOWS_EPOCH_SECONDS = 11_644_473_600
FILETIME_TICKS_PER_SECOND = 10_000_000
ZSTD_FRAME_HEADER = bytes.fromhex("28 b5 2f fd 00 38")
ZSTD_EMPTY_LAST_BLOCK = b"\x01\x00\x00"
RPC_MESSAGE_TYPES = frozenset({18, 22})
MAX_DORAEMON_FRAME_BYTES = 256 * 1024
MAX_APPLICATION_MESSAGE_BYTES = 16 * 1024 * 1024


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
        self.application.clear()
        self.application_origins.clear()

    @staticmethod
    def _standalone(block: bytes) -> bool:
        try:
            zstandard.ZstdDecompressor().decompress(
                ZSTD_FRAME_HEADER + _zstd_block(block, last=True),
                max_output_size=MAX_APPLICATION_MESSAGE_BYTES,
            )
            return True
        except (ValueError, zstandard.ZstdError):
            return False

    def _start_zstd(self, block: bytes) -> bytes:
        decoder = zstandard.ZstdDecompressor().decompressobj()
        plain = decoder.decompress(
            ZSTD_FRAME_HEADER + _zstd_block(block, last=False)
        )
        self.zstd = decoder
        self.application.clear()
        self.application_origins.clear()
        self.diagnostics.zstd_resyncs += 1
        return plain

    def _decompress(self, block: bytes) -> bytes | None:
        if self.zstd is None:
            if not self._standalone(block):
                return None
            try:
                return self._start_zstd(block)
            except (ValueError, zstandard.ZstdError):
                self.diagnostics.zstd_errors += 1
                self.zstd = None
                return None
        try:
            return self.zstd.decompress(_zstd_block(block, last=False))
        except (ValueError, zstandard.ZstdError):
            self.diagnostics.zstd_errors += 1
            self.zstd = None
            self.application.clear()
            self.application_origins.clear()
            if not self._standalone(block):
                return None
            try:
                return self._start_zstd(block)
            except (ValueError, zstandard.ZstdError):
                self.diagnostics.zstd_errors += 1
                self.zstd = None
                return None

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
            try:
                entity_id = int(call[0])
                method_id = int(call[1])
            except (TypeError, ValueError, OverflowError):
                continue
            method = METHOD_ID_NAMES.get(method_id, "")
            if not method:
                self.diagnostics.unknown_method_messages += 1
                continue
            if self.retained_methods is not None and method not in self.retained_methods:
                continue
            arguments = call[2] if len(call) > 2 and isinstance(call[2], list) else []
            self.record_sequence += 1
            timestamp_epoch = float(timestamp_epoch)
            records.append(
                {
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
                    "npcap_method_id": method_id,
                    "npcap_kcp_sequence": int(kcp_sequence),
                    "capture_source": "npcap",
                }
            )
            self.diagnostics.retained_records += 1
        return records

    def feed_push(
        self, plaintext: bytes, sequence: int, timestamp_epoch: float
    ) -> list[dict]:
        self.diagnostics.decrypted_pushes += 1
        records: list[dict] = []
        for frame in self.frames.feed(plaintext, sequence, timestamp_epoch):
            self.diagnostics.doraemon_frames += 1
            plain = self._decompress(frame.data)
            if plain is None:
                continue
            self._append_application(
                plain, frame.sequence, frame.timestamp_epoch
            )
            records.extend(self._rpc_records())
        return records
