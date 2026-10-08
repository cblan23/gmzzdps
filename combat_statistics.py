"""Server cumulative statistics contract. No capture, memory access or UI objects.

Missing is None; explicit zero remains zero. Only STAGE settles a Boss encounter.
ALL is retained separately and never divided, apportioned, or used as an increment.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any

STAGE_MESSAGE = 'OnMsgUpdateStageCombatStatistics'
SETTLEMENT_MESSAGE = 'OnMsgSettlementCombatStatistics'
STATISTICS_MESSAGES = frozenset({STAGE_MESSAGE, SETTLEMENT_MESSAGE})
MAX_MEMBERS = 64  # validation bound, not a six-person UI limit
MAX_SKILLS = 4096


def text_value(value: Any) -> str:
    if isinstance(value, dict) and set(value) == {'binary_base64'}:
        value = base64.b64decode(value['binary_base64'], validate=True)
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='strict')
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError('Expected a bounded protocol string')
    return value


def unsigned(value: Any, *, missing: bool = True) -> int | None:
    if value is None and missing:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**64:
        raise ValueError('Invalid server integer; refusing to replace it with zero')
    return value


def pairs(value: Any) -> list[tuple[Any, Any]]:
    if isinstance(value, dict):
        if len(value) == 1 and ('$map' in value or 'map_pairs' in value):
            value = value.get('$map', value.get('map_pairs'))
        else:
            return list(value.items())
    if isinstance(value, list) and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in value):
        return [(p[0], p[1]) for p in value]
    raise ValueError('Expected an explicit protocol map')


def fields(value: Any) -> dict[int, Any]:
    result = {}
    for key, val in pairs(value):
        if isinstance(key, bytes):
            key = key.decode('ascii')
        if isinstance(key, str) and key.isascii() and key.isdecimal():
            key = int(key)
        if isinstance(key, bool) or not isinstance(key, int) or key < 0 or key in result:
            raise ValueError('Invalid or duplicate numeric protocol field')
        result[key] = val
    return result


def counters(value: Any, *, entity_keys: bool = False) -> dict[str, int] | None:
    if value is None:
        return None
    rows = pairs(value)
    if len(rows) > MAX_SKILLS:
        raise ValueError('Counter map exceeds bound')
    result = {}
    for key, amount in rows:
        if entity_keys:
            key = text_value(key)
            if not key:
                raise ValueError('Empty entity key')
        else:
            if isinstance(key, str) and key.isascii() and key.isdecimal():
                key = int(key)
            key = str(unsigned(key, missing=False))
        if key in result:
            raise ValueError('Duplicate counter key')
        result[key] = unsigned(amount, missing=False)
    return result


@dataclass(frozen=True)
class NormalizedMemberStatistics:
    id: str
    iid: int | None
    name: str | None
    damage: int | None
    bear: int | None
    heal: int | None
    member_battle_length: int | None
    skill_count: dict[str, int] | None
    skill_damage: dict[str, int] | None
    skill_heal: dict[str, int] | None
    bear_map: dict[str, int] | None
    killer_map: dict[str, int] | None
    profession_id: int | None = None
    damage_critical_count: int | None = None
    damage_count: int | None = None
    penetration_excluded_count: int | None = None

    @property
    def unclassified_damage(self) -> int | None:
        if self.damage is None or self.skill_damage is None:
            return None
        difference = self.damage - sum(self.skill_damage.values())
        return difference if difference >= 0 else None

    @property
    def skills_exceed_total(self) -> bool:
        return self.damage is not None and self.skill_damage is not None and sum(self.skill_damage.values()) > self.damage


def normalize_members(value: Any) -> tuple[NormalizedMemberStatistics, ...]:
    members = []
    tokens, actors = set(), set()
    entries = pairs(value)
    if not entries or len(entries) > MAX_MEMBERS:
        raise ValueError('Empty or oversized server roster')
    for token, raw in entries:
        token = text_value(token)
        f = fields(raw)
        if not token or token in tokens or (0 in f and text_value(f[0]) != token):
            raise ValueError('Ambiguous member token')
        actor = unsigned(f.get(1))
        if actor == 0:
            raise ValueError('Invalid explicit entity id')
        if actor is not None:
            if actor in actors:
                raise ValueError('Same entity assigned to multiple members')
            actors.add(actor)
        tokens.add(token)
        skill_damage = counters(f.get(34))
        damage = unsigned(f.get(6))
        if damage is None and skill_damage == {}:
            # Healers can legitimately omit the aggregate damage field while
            # still sending an explicit, empty per-skill damage table.  That
            # combination proves zero damage; other missing combinations stay
            # unknown rather than being fabricated as zero.
            damage = 0
        damage_count = unsigned(f.get(27))
        penetration_excluded_count = unsigned(f.get(14))
        if damage_count is not None and 14 not in f:
            # The settlement schema omits field 14 when no hit was excluded
            # from penetration. Field 27 proves the denominator is present.
            penetration_excluded_count = 0
        members.append(NormalizedMemberStatistics(
            id=token, iid=actor, name=text_value(f[5]) if 5 in f else None,
            damage=damage, bear=unsigned(f.get(7)), heal=unsigned(f.get(17)),
            member_battle_length=unsigned(f.get(19)), skill_count=counters(f.get(33)),
            skill_damage=skill_damage, skill_heal=counters(f.get(35)),
            bear_map=counters(f.get(36), entity_keys=True), killer_map=counters(f.get(37), entity_keys=True),
            profession_id=unsigned(f.get(4)),
            damage_critical_count=unsigned(f.get(25)), damage_count=damage_count,
            penetration_excluded_count=penetration_excluded_count))
    return tuple(sorted(members, key=lambda m: m.id))


@dataclass(frozen=True)
class NormalizedCombatStatistics:
    battle_id: str | None
    stage_id: int | None
    stage_index: int | None
    scope: str
    received_at_ns: int
    is_stage_success: bool | None
    members: tuple[NormalizedMemberStatistics, ...]
    source: str
    server_started_at: int | None = None
    instance_id: str | None = None
    dungeon_id: int | None = None
    map_id: int | None = None
    # ALL can travel with a current-stage identity but is not that stage's total.
    associated_battle_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> 'NormalizedCombatStatistics':
        copied = dict(value)
        members = []
        for raw_member in copied['members']:
            member = dict(raw_member)
            if member.get('damage') is None and member.get('skill_damage') == {}:
                member['damage'] = 0
            if (
                member.get('damage_count') is not None
                and member.get('penetration_excluded_count') is None
            ):
                member['penetration_excluded_count'] = 0
            members.append(NormalizedMemberStatistics(**member))
        copied['members'] = tuple(members)
        return cls(**copied)

    @property
    def fingerprint(self) -> str:
        payload = self.to_dict()
        payload.pop('received_at_ns')
        payload.pop('source')
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def receipt_ns(record: dict) -> int:
    if 'capture_timestamp_ns' in record:
        return unsigned(record['capture_timestamp_ns'], missing=False)
    value = unsigned(record.get('filetime_100ns'), missing=False)
    epoch = (value - 116444736000000000) * 100
    if epoch < 0:
        raise ValueError('Invalid receipt timestamp')
    return epoch


def normalize_statistics(record: dict, *, instance_id: str | None = None,
                         dungeon_id: int | None = None, map_id: int | None = None) -> tuple[NormalizedCombatStatistics, ...]:
    method = record.get('method')
    if method not in STATISTICS_MESSAGES:
        return ()
    args = record.get('decoded_arguments')
    if not isinstance(args, list) or len(args) != (1 if method == STAGE_MESSAGE else 2):
        raise ValueError('Unexpected stage/settlement argument contract')
    received = receipt_ns(record)
    context = dict(instance_id=instance_id, dungeon_id=dungeon_id, map_id=map_id)

    def stage(raw):
        f = fields(raw)
        success = f.get(3)
        if success is not None and type(success) is not bool:
            raise ValueError('Invalid success flag')
        return NormalizedCombatStatistics(
            battle_id=text_value(f[2]) or None if 2 in f else None,
            stage_id=unsigned(f.get(0)), stage_index=unsigned(f.get(1)), scope='STAGE',
            received_at_ns=received, is_stage_success=success, members=normalize_members(f[5]),
            source=method, server_started_at=unsigned(f.get(4)), **context)

    if method == STAGE_MESSAGE:
        return (stage(args[0]),)
    current = stage(args[1])
    result = [current]
    for group_id, members in fields(args[0]).items():
        if not pairs(members):
            # Settlement bundles can include an empty auxiliary stage.  It has
            # no statistics to retain; the authoritative current stage above
            # remains subject to the normal non-empty roster validation.
            continue
        normalized = normalize_members(members)
        if group_id == current.stage_id:
            # The second argument has battleID and is the Boss-level source of truth.
            if normalized != current.members:
                raise ValueError('Conflicting copies of current stage in one settlement')
            continue
        result.append(NormalizedCombatStatistics(
            battle_id=None, stage_id=None if group_id == 0 else group_id,
            stage_index=None, scope='ALL' if group_id == 0 else 'STAGE',
            received_at_ns=received, is_stage_success=None, members=normalized, source=method,
            associated_battle_id=current.battle_id, **context))
    return tuple(result)
