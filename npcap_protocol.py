#!/usr/bin/env python3
"""Decode the game's inbound Npcap stream into NetworkPacketParser records.

This module contains no packet transmitter and no game hook.  It accepts
already decrypted, in-order KCP PUSH payloads, restores the Doraemon/Zstd
framing, decodes MessagePack RPC calls, and emits the same small record shape
that the existing combat parser consumes.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import time
import uuid
from itertools import islice
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import msgpack
import zstandard
from npcap_wire_entities import decode_creation, validate_wrapper

from npcap_zstd_state import NativeZstdDecoder, ZstdSnapshot
from npcap_method_tables import LOCAL_ROLE_RPC_METHODS, NPC_RPC_METHODS


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
    # 86/87 were inferred from a small timing correlation, not a verified
    # entity-class method table. Live captures show repeated full-HP events
    # and a two-argument duration payload at 86, unlike the four-argument
    # legacy revive payload. Keep them unknown until their class is verified;
    # otherwise normal mechanisms become fabricated deaths and end battles.
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
    352: "RetOtherRoleShapeData",
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
    1613: "OnMsgRefreshSceneObjects",
    2006: "RetGetServerLevelInfo",
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
    unsupported_application_messages: int = 0
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


def _bounded_unknown_sample(value, remaining=None, depth=0):
    remaining = [256] if remaining is None else remaining
    if remaining[0] <= 0 or depth > 5:
        return '<truncated>'
    remaining[0] -= 1
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, bytes):
        return {'bytes_hex': value[:64].hex(), 'length': len(value)}
    if isinstance(value, dict):
        return {str(key)[:64]: _bounded_unknown_sample(item, remaining, depth+1)
                for key, item in islice(value.items(), 24)}
    if isinstance(value, (list, tuple)):
        return [_bounded_unknown_sample(item, remaining, depth+1) for item in value[:24]]
    return value


def _valid_named_method(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 96:
        return False
    return bool(
        value.isascii()
        and (value[0].isalpha() or value[0] == "_")
        and all(char.isalnum() or char == "_" for char in value)
    )


def _valid_current_damage_sync_v2(
    entity_id: int, arguments: object
) -> bool:
    """Recognize the current client's method 93 damage callback by shape.

    Numeric RPC IDs are scoped to an entity class, so method 93 cannot be
    added to the global table without also accepting unrelated callbacks.  A
    real DamageSyncV2 carries exactly nine values.  Captures before the
    2026-09-20 update dispatched it on the attacking entity; current team-PVP
    traffic dispatches the same canonical ``attacker, target, ...`` payload on
    the affected entity.  Requiring the RPC owner to be one of those two exact
    endpoints keeps the compatibility alias bounded to the observed combat
    message without dropping the current layout.
    """

    if not isinstance(arguments, list) or len(arguments) != 9:
        return False
    if type(arguments[8]) is not bool:
        return False
    if any(type(value) is not int for value in arguments[:8]):
        return False
    attacker_id, target_id, skill_id = arguments[:3]
    if (
        entity_id not in (attacker_id, target_id)
        or not 0 < attacker_id < 2**64
        or not 0 < target_id < 2**64
        or not 0 < skill_id < 2**64
    ):
        return False
    # Hit/result fields are small non-negative enums. Damage fields are
    # non-negative signed values; zero remains valid for immune/absorbed hits.
    if not all(0 <= arguments[index] <= 0x7FFFFFFF for index in (3, 4)):
        return False
    return all(0 <= arguments[index] <= 0x7FFFFFFFFFFFFFFF for index in (5, 6, 7))


def _team_pvp_field(value: dict, key: int) -> object:
    """Read the integer-keyed MessagePack field after either raw or JSON replay."""

    if key in value:
        return value[key]
    return value.get(str(key))


def _valid_team_pvp_info(arguments: object) -> bool:
    """Recognize the verified team-arena roster payload without ID guessing.

    Method 795 was this callback in the previous client, while the current
    descriptor assigns it to ``OnRecvPVPBattleOrder`` and moves this callback
    to 799.  Numeric RPC IDs are class/version scoped, so both compatibility
    IDs are accepted only when every roster row has the captured wire shape.
    """

    if not isinstance(arguments, list) or not 2 <= len(arguments) <= 61:
        return False
    member_count = arguments[0]
    if (
        isinstance(member_count, bool)
        or not isinstance(member_count, int)
        or member_count != len(arguments) - 1
    ):
        return False
    tokens = set()
    for member in arguments[1:]:
        if not isinstance(member, dict):
            return False
        token = _team_pvp_field(member, 0)
        profession = _team_pvp_field(member, 2)
        level = _team_pvp_field(member, 3)
        name = _team_pvp_field(member, 4)
        avatar_template = _team_pvp_field(member, 6)
        if (
            not isinstance(token, str)
            or not 8 <= len(token) <= 128
            or not token.isascii()
            or any(char.isspace() for char in token)
            or token in tokens
            or not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 64
            or isinstance(profession, bool)
            or not isinstance(profession, int)
            or not 1_000_000 <= profession <= 1_999_999
            or isinstance(level, bool)
            or not isinstance(level, int)
            or not 1 <= level <= 1_000
            or isinstance(avatar_template, bool)
            or not isinstance(avatar_template, int)
            or not 1 <= avatar_template <= 99_999_999
        ):
            return False
        tokens.add(token)
    return True


def _valid_team_pvp_roster(arguments: object) -> bool:
    """Recognize ``RetGetTeamArenaBattleInfo`` observed on method 824.

    The callback contains one map keyed by two numeric team IDs.  Each team
    owns field 0, a list of stable-token/name/profession rows.  AI-controlled
    arena avatars additionally carry a positive template ID in field 2.
    Requiring two equally sized teams and unique tokens keeps the numeric
    compatibility alias isolated from unrelated method-824 callbacks.
    """

    if not isinstance(arguments, list) or len(arguments) != 1:
        return False
    teams = arguments[0]
    if not isinstance(teams, dict) or not 2 <= len(teams) <= 4:
        return False
    team_sizes = []
    tokens = set()
    for raw_team_id, raw_team in teams.items():
        try:
            team_id = int(raw_team_id)
        except (TypeError, ValueError, OverflowError):
            return False
        if isinstance(raw_team_id, bool) or not 0 < team_id < 2**64:
            return False
        if not isinstance(raw_team, dict):
            return False
        members = _team_pvp_field(raw_team, 0)
        if not isinstance(members, list) or not 1 <= len(members) <= 60:
            return False
        team_sizes.append(len(members))
        for member in members:
            if not isinstance(member, dict):
                return False
            token = _team_pvp_field(member, 0)
            name = _team_pvp_field(member, 1)
            profession = _team_pvp_field(member, 3)
            ai_template = _team_pvp_field(member, 2)
            if (
                not isinstance(token, str)
                or not 8 <= len(token) <= 128
                or not token.isascii()
                or any(char.isspace() for char in token)
                or token in tokens
                or not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 64
                or isinstance(profession, bool)
                or not isinstance(profession, int)
                or not 1_000_000 <= profession <= 1_999_999
                or (
                    ai_template is not None
                    and (
                        isinstance(ai_template, bool)
                        or not isinstance(ai_template, int)
                        or not 1 <= ai_template <= 99_999_999
                    )
                )
            ):
                return False
            tokens.add(token)
    return len(teams) == 4 or len(set(team_sizes)) == 1


def _valid_team_pvp_settlement(arguments: object) -> bool:
    """Recognize the complete team-arena result observed on method 798.

    The first argument is a two-team map. Each team contains an equally sized
    member list in field 4; fields 15-19 on every member are authoritative
    damage, healing, damage-taken, kill and death counters. Exactly one team
    carries field 0 == 1. Structural validation is required because numeric
    RPC IDs are scoped to an entity class and can be reused.
    """

    if not isinstance(arguments, list) or len(arguments) != 2:
        return False
    teams, server_stamp = arguments
    if (
        not isinstance(teams, dict)
        or not 2 <= len(teams) <= 4
        or isinstance(server_stamp, bool)
        or not isinstance(server_stamp, int)
        or server_stamp <= 0
    ):
        return False
    team_sizes = []
    tokens = set()
    winner_count = 0
    for raw_team_id, raw_team in teams.items():
        try:
            team_id = int(raw_team_id)
        except (TypeError, ValueError, OverflowError):
            return False
        if isinstance(raw_team_id, bool) or not 0 < team_id < 2**64:
            return False
        if not isinstance(raw_team, dict):
            return False
        winner = _team_pvp_field(raw_team, 0)
        if winner not in (None, 0, 1, False, True):
            return False
        winner_count += int(winner in (1, True))
        captain = _team_pvp_field(raw_team, 1)
        started_at = _team_pvp_field(raw_team, 2)
        ended_at = _team_pvp_field(raw_team, 3)
        members = _team_pvp_field(raw_team, 4)
        if (
            not isinstance(captain, str)
            or not 8 <= len(captain) <= 128
            or not captain.isascii()
            or any(char.isspace() for char in captain)
            or isinstance(started_at, bool)
            or not isinstance(started_at, int)
            or isinstance(ended_at, bool)
            or not isinstance(ended_at, int)
            or started_at <= 0
            or ended_at < started_at
            or not isinstance(members, list)
            or not 1 <= len(members) <= 60
        ):
            return False
        team_sizes.append(len(members))
        for member in members:
            if not isinstance(member, dict):
                return False
            token = _team_pvp_field(member, 0)
            profession = _team_pvp_field(member, 2)
            level = _team_pvp_field(member, 3)
            name = _team_pvp_field(member, 4)
            if (
                not isinstance(token, str)
                or not 8 <= len(token) <= 128
                or not token.isascii()
                or any(char.isspace() for char in token)
                or token in tokens
                or isinstance(profession, bool)
                or not isinstance(profession, int)
                or not 1_000_000 <= profession <= 1_999_999
                or isinstance(level, bool)
                or not isinstance(level, int)
                or not 1 <= level <= 1_000
                or not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 64
            ):
                return False
            for field in (15, 16, 17, 18, 19):
                value = _team_pvp_field(member, field)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or value > 0x7FFFFFFFFFFFFFFF
                ):
                    return False
            tokens.add(token)
    return winner_count == 1 and (len(teams) == 4 or len(set(team_sizes)) == 1)


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

    def __init__(
        self,
        retained_methods: Iterable[str] | None = None,
        *,
        stream_id: str | None = None,
        capture_unknown: bool = False,
        capture_unknown_timeline: bool = False,
        capture_unknown_method_ids: Iterable[int] | None = None,
    ) -> None:
        self.capture_unknown = capture_unknown
        self.capture_unknown_timeline = bool(capture_unknown_timeline)
        self.capture_unknown_method_ids = frozenset(
            int(method_id) for method_id in (capture_unknown_method_ids or ())
        )
        self.unknown_records = deque(maxlen=64)
        self.sampled_unknown_ids = set()
        self.stream_id = stream_id or uuid.uuid4().hex
        self.transport_generation = 0
        self.application_offset = 0
        self.capture_timestamps: OrderedDict[int, int] = OrderedDict()
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
        self.application_framing_validated = False
        self.record_sequence = 0
        self.diagnostics = ProtocolDiagnostics()
        self.retained_methods = (
            frozenset(str(value) for value in retained_methods)
            if retained_methods is not None
            else None
        )

    def reset_transport(self) -> None:
        self.transport_generation += 1
        self.application_offset = 0
        self.capture_timestamps.clear()
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
        self.application_framing_validated = False
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
        self.application_framing_validated = False
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
        self.application_framing_validated = False
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
            self.application_framing_validated = False
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
        self.application_offset += remaining
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
            # Keep complete framing: do not scan through creation payload bytes
            # as if embedded integers were independent RPC headers.
            if 2 <= size <= MAX_APPLICATION_MESSAGE_BYTES:
                if message_type > 255 and message_type & 255 in (3, 15):
                    # Forwarding wrappers can declare a multi-megabyte streamed
                    # body.  Their children retain their own application
                    # framing, so enter the wrapper as soon as its small
                    # metadata header is complete instead of freezing all RPCs
                    # until the entire body arrives.
                    header_size = 6 + self.application[5]
                    if len(self.application) < header_size:
                        break
                    try:
                        header_size = validate_wrapper(self.application, end)
                    except EOFError:
                        break
                    except (ValueError, msgpack.ExtraData, msgpack.FormatError):
                        # Capture may begin mid-application-message. A header-like
                        # byte sequence is not a validated forwarding boundary.
                        self._application_consume(1)
                        self.diagnostics.application_skipped_bytes += 1
                        continue
                    self._application_consume(header_size)
                    self.application_framing_validated = True
                    continue
                if message_type in (7, 11):
                    if not _rpc_payload_prefix_valid(self.application, 0):
                        self.application_framing_validated = False
                        self._application_consume(1)
                        self.diagnostics.application_skipped_bytes += 1
                        continue
                    if len(self.application) < end:
                        break
                    try:
                        value = msgpack.unpackb(bytes(self.application[6:end]), raw=True, strict_map_key=False)
                        entity = decode_creation(message_type, value)
                    except (ValueError, UnicodeError, msgpack.ExtraData, msgpack.FormatError):
                        self._application_consume(1)
                        self.diagnostics.application_skipped_bytes += 1
                        continue
                    _available, kcp_sequence, epoch = self.application_origins[0]
                    offset = self.application_offset
                    self._application_consume(end)
                    self.application_framing_validated = True
                    self.diagnostics.application_messages += 1
                    if entity is not None:
                        ns = self.capture_timestamps.get(int(kcp_sequence), int(float(epoch)*1e9))
                        self.record_sequence += 1
                        records.append({'method':'NpcapEntityCreated', 'decoded_arguments':[entity],
                            'script_entity':entity['entity_id'],'network_entity_id':entity['entity_id'],
                            'sequence':self.record_sequence,'capture_source':'npcap','arguments_synchronized':True,
                            'capture_timestamp_ns':ns,'event_time':epoch_to_event_time(float(epoch)),
                            'filetime_100ns':WINDOWS_EPOCH_SECONDS*FILETIME_TICKS_PER_SECOND+ns//100,
                            'capture_event_id':f'{self.stream_id}:{self.transport_generation}:{kcp_sequence}:{offset}',
                            'npcap_message_type':message_type,'npcap_kcp_sequence':int(kcp_sequence),
                            'npcap_method_scope':'entity_creation','decode_delay_ms':0.0})
                        self.diagnostics.retained_records += 1
                    continue
                if (
                    self.application_framing_validated
                    and message_type not in RPC_MESSAGE_TYPES
                ):
                    # Once a real wrapper/creation/RPC has established the
                    # application boundary, unsupported message kinds are
                    # still complete length-delimited frames.  Preserve that
                    # boundary instead of byte-scanning through their payload,
                    # which can fabricate a huge header and freeze later RPCs.
                    if len(self.application) < end:
                        break
                    self._application_consume(end)
                    self.diagnostics.application_messages += 1
                    self.diagnostics.unsupported_application_messages += 1
                    continue
            plausible = bool(
                2 <= size <= MAX_APPLICATION_MESSAGE_BYTES
                and message_type in RPC_MESSAGE_TYPES
                and _rpc_payload_prefix_valid(self.application, 0)
            )
            if not plausible:
                self.application_framing_validated = False
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
                self.application_framing_validated = False
                self._application_consume(1)
                self.diagnostics.application_skipped_bytes += 1
                continue

            _available, kcp_sequence, timestamp_epoch = self.application_origins[0]
            message_offset = self.application_offset
            raw_message = bytes(self.application[:end])
            self._application_consume(end)
            self.application_framing_validated = True
            self.diagnostics.application_messages += 1
            timestamp_epoch = float(timestamp_epoch)
            timestamp_ns = self.capture_timestamps.get(
                int(kcp_sequence), int(timestamp_epoch * 1_000_000_000)
            )
            capture_event_id = (
                f"{self.stream_id}:{self.transport_generation}:"
                f"{int(kcp_sequence)}:{message_offset}"
            )
            call = value[1]
            method_id: int | None = None
            method_scope = ''
            entity_id = 0
            arguments = call[2] if len(call) > 2 and isinstance(call[2], list) else []
            if message_type in RPC_NUMERIC_MESSAGE_TYPES:
                try:
                    entity_id = int(call[0])
                    method_id = int(call[1])
                except (TypeError, ValueError, OverflowError):
                    continue
                if method_id in LOCAL_ROLE_RPC_METHODS:
                    method = LOCAL_ROLE_RPC_METHODS[method_id]
                    method_scope = 'player_entity' if method_id in (2244, 2245) else 'local_role'
                elif method_id in NPC_RPC_METHODS:
                    method = NPC_RPC_METHODS[method_id]
                    method_scope = 'npc'
                elif method_id == 93 and _valid_current_damage_sync_v2(
                    entity_id, arguments
                ):
                    method = "OnMsgDamageSyncV2"
                    method_scope = "damage_shape_compat"
                elif method_id in (795, 799) and _valid_team_pvp_info(arguments):
                    method = "OnMsgSyncTeamPVPInfo"
                    method_scope = "team_pvp_info_shape_compat"
                elif method_id == 798 and _valid_team_pvp_settlement(arguments):
                    method = "OnMsgTeamPVPSettlement"
                    method_scope = "team_pvp_settlement_shape_compat"
                elif method_id == 824 and _valid_team_pvp_roster(arguments):
                    method = "RetGetTeamArenaBattleInfo"
                    method_scope = "team_pvp_roster_shape_compat"
                elif (
                    method_id == 794
                    and len(arguments) >= 3
                    and isinstance(arguments[2], list)
                    and _valid_team_pvp_info(
                        [len(arguments[2]), *arguments[2]]
                    )
                ):
                    # Current 3V3 captures carry the same verified roster rows
                    # in slot 2 of a battle-order callback. Normalize only this
                    # exact shape; unrelated method-794 payloads stay unknown.
                    arguments = [len(arguments[2]), *arguments[2]]
                    method = "OnMsgSyncTeamPVPInfo"
                    method_scope = "team_pvp_info_nested_shape_compat"
                else:
                    method = METHOD_ID_NAMES.get(method_id, "")
                if not method:
                    self.diagnostics.unknown_method_messages += 1
                    first_sample = (
                        method_id not in self.sampled_unknown_ids
                        and len(self.sampled_unknown_ids) < 64
                    )
                    if self.capture_unknown and (
                        self.capture_unknown_timeline
                        or method_id in self.capture_unknown_method_ids
                        or first_sample
                    ):
                        if first_sample:
                            self.sampled_unknown_ids.add(method_id)
                        self.unknown_records.append(
                            {
                                'event_time': epoch_to_event_time(timestamp_epoch),
                                'capture_timestamp_ns': timestamp_ns,
                                'capture_event_id': capture_event_id,
                                'npcap_kcp_sequence': int(kcp_sequence),
                                'npcap_message_offset': message_offset,
                                'npcap_message_bytes': end,
                                'method_id': method_id,
                                'entity_id': entity_id,
                                'arguments': (
                                    _bounded_unknown_sample(call[2])
                                    if len(call) > 2
                                    else []
                                ),
                                'message_type': int(message_type),
                            }
                        )
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
            self.record_sequence += 1
            record = {
                    "event_time": epoch_to_event_time(timestamp_epoch),
                    "filetime_100ns": WINDOWS_EPOCH_SECONDS * FILETIME_TICKS_PER_SECOND + timestamp_ns // 100,
                    "capture_timestamp_ns": timestamp_ns,
                    "capture_event_id": capture_event_id,
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
                    "capture_decode_latency_ms": max(0.0, (time.time_ns() - timestamp_ns) / 1_000_000),
                    "arguments_synchronized": True,
                    "npcap_message_type": int(message_type),
                    "npcap_message_bytes": end,
                    "npcap_method_scope": method_scope,
                    "npcap_kcp_sequence": int(kcp_sequence),
                    "capture_source": "npcap",
                }
            if method_id is not None:
                record["npcap_method_id"] = method_id
            else:
                record["npcap_method_name"] = method
                if isinstance(call[0], str) and len(call[0]) <= 128:
                    record['npcap_recipient'] = call[0]
            if method == "RetOtherRoleShapeData":
                # Equipment/profile evidence is intentionally retained in full
                # by the parent append-only archive.  Keep raw bytes only for
                # this low-frequency response so ordinary combat records stay
                # compact.
                record["npcap_rpc_payload_base64"] = base64.b64encode(
                    raw_message
                ).decode("ascii")
                record["npcap_rpc_payload_sha256"] = hashlib.sha256(
                    raw_message
                ).hexdigest()
                record["npcap_rpc_payload_length"] = len(raw_message)
            records.append(record)
            self.diagnostics.retained_records += 1
        return records

    def feed_push(
        self, plaintext: bytes, sequence: int, timestamp_epoch: float, *, timestamp_ns: int | None = None
    ) -> list[dict]:
        if timestamp_ns is not None:
            self.capture_timestamps[int(sequence)] = int(timestamp_ns)
            while len(self.capture_timestamps) > 65536:
                self.capture_timestamps.popitem(last=False)
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
