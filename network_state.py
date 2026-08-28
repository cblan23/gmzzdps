#!/usr/bin/env python3
"""Turn decoded C7 RPC packets into combat and entity-state updates."""

from __future__ import annotations

import base64
import hashlib
import math
import re
import time
from collections.abc import Iterable


ENTITY_ID_MIN = 10_000_000_000_000
ENTITY_ID_MAX = 999_999_999_999_999
LOW_COMBAT_ENTITY_ID_MIN = 1_000_000_000_000
LOW_COMBAT_ENTITY_ID_MAX = 7_999_999_999_999
MAX_PARTY_MEMBERS = 12
PLAYER_SKILL_MIN = 86_000_000
PLAYER_SKILL_MAX = 87_999_999
MAX_PROFILE_TEXT = 64
POINTER_BIND_WINDOW_100NS = 20_000_000
GUILD_SCENE_ID = 5_200_021
GUILD_HIT_DUMMY_POSITION = (2626.044386, 13530.408447)
GUILD_HIT_DUMMY_TEMPLATE_ID = 7_114_225
GUILD_HIT_DUMMY_LEVEL = 62
GUILD_HIT_DUMMY_NAME = "公会伤害木桩"
GUILD_HIT_DUMMY_BOSS_TYPE = 3
HUD_BOSS_TYPE = 3
HUD_BOSS_TEMPLATE_RE = re.compile(r"(?:^|[._])Boss(?:$|[._])", re.I)
ENCOUNTER_AUXILIARY_TEMPLATES: dict[int, dict[str, object]] = {
    7_102_402: {
        "name": "星光守卫",
        "parent_template_ids": (7_102_400,),
    },
    7_102_405: {
        "name": "星光守卫",
        "parent_template_ids": (7_102_403,),
    },
}
MULTIPHASE_BOSS_TEMPLATE_IDS = frozenset(
    int(parent_template_id)
    for auxiliary in ENCOUNTER_AUXILIARY_TEMPLATES.values()
    for parent_template_id in auxiliary.get("parent_template_ids", ())
    if int(parent_template_id)
)
ENCOUNTER_NON_BOSS_TEMPLATE_IDS = frozenset(
    {7_102_401, 7_102_402, 7_102_404, 7_102_405, 7_102_406, 7_102_407}
)
TEAM_STATISTICS_METHODS = {
    "RetCommonCombatStatisticsByTeam",
    "RetDirtyCommonCombatStatisticsByTeam",
}
STAGE_COMBAT_STATISTICS_METHOD = "OnMsgUpdateStageCombatStatistics"
TEAM_JOIN_SUCCESS_METHOD = "OnJoinGroupSuccess"
TEAM_SELF_PROPS_METHOD = "OnUpdateTeamGroupSelfProps"
TEAM_MEMBER_JOIN_METHOD = "OnMsgOtherJoinTeamGroup"
TEAM_MEMBERS_JOIN_METHOD = "OnMsgMembersJoinTeam"
TEAM_OTHER_MEMBER_TOKEN_METHODS = {
    "OnSyncTeamGroupPropsForceRefresh",
}
TEAM_MEMBER_LEAVE_MARKERS = (
    "otherleaveteam",
    "otherquitteam",
    "otherquitgroup",
    "memberleaveteam",
    "removeteamgroupmember",
)
TEAM_CLEAR_MARKERS = (
    "selfleaveteam",
    "leaveteamgroup",
    "quitteam",
    "dismiss team",
    "dismissteam",
    "disbandteam",
)
CURRENT_TEAM_ACTIVITY_METHODS = {
    "OnUpdateTeamGroupSelfProps",
    "OnUpdateTeamGroupMemberProps",
    "OnMsgSyncTeamGroupMemberFreqProp",
    "OnUpdateTeamGroupDetail",
}
LOCAL_CAST_MATCH_WINDOW_100NS = 5_000_000
TEAM_HIT_SKILL_WINDOW_100NS = 10 * 10_000_000
HP_SKILL_COLLISION_WINDOW_100NS = 3 * 10_000_000
BOSS_SIGNAL_BIND_WINDOW_100NS = 3 * 10_000_000
BOSS_SIGNAL_ACTIVE_WINDOW_100NS = 10 * 10_000_000


def normalize_boss_name(value: object) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


BOSS_PHASE_NAME_GROUPS = (
    frozenset(
        {
            normalize_boss_name("先祖铠甲"),
            normalize_boss_name("伯德温·威瑟尔"),
        }
    ),
)


def boss_names_share_phase(left: object, right: object) -> bool:
    left_name = normalize_boss_name(left)
    right_name = normalize_boss_name(right)
    return bool(
        left_name
        and right_name
        and any(
            any(alias in left_name for alias in group)
            and any(alias in right_name for alias in group)
            for group in BOSS_PHASE_NAME_GROUPS
        )
    )


def boss_phase_continues(left: object, right: object) -> bool:
    left_name = normalize_boss_name(left)
    right_name = normalize_boss_name(right)
    ancestor = normalize_boss_name("先祖铠甲")
    baldwin = normalize_boss_name("伯德温·威瑟尔")
    return bool(
        left_name
        and right_name
        and ancestor in left_name
        and baldwin in right_name
    )

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_PROFILE_METHOD_RE = re.compile(
    r"(?:Role|Player|Team|Party|Member|Friend|Actor|Entity|SceneObject)", re.I
)


def map_pairs(value) -> list[tuple[object, object]]:
    """Return pairs from the JSON-safe representation of a msgpack map."""
    if not isinstance(value, dict):
        return []
    encoded = value.get("$map")
    if isinstance(encoded, list):
        pairs: list[tuple[object, object]] = []
        for item in encoded:
            if isinstance(item, list) and len(item) == 2:
                pairs.append((item[0], item[1]))
        return pairs
    return list(value.items())


def walk_values(value) -> Iterable[object]:
    yield value
    if isinstance(value, list):
        for item in value:
            yield from walk_values(item)
    elif isinstance(value, dict):
        for key, item in map_pairs(value):
            yield from walk_values(key)
            yield from walk_values(item)


def parse_entity_id(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if ENTITY_ID_MIN <= parsed <= ENTITY_ID_MAX:
        return parsed
    return None


def parse_combat_entity_id(value) -> int | None:
    """Parse IDs from fields that explicitly carry an actor or target.

    Some game instances use 13-digit entity IDs. The gap above that range is
    deliberately excluded because 86/87-series skill instances occupy it.
    Generic packet scanning continues to use the stricter ``parse_entity_id``.
    """
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if LOW_COMBAT_ENTITY_ID_MIN <= parsed <= LOW_COMBAT_ENTITY_ID_MAX:
        return parsed
    return parse_entity_id(parsed)


def normalize_network_skill_id(value) -> int:
    """Normalize a damage-source instance ID to its eight-digit skill ID."""
    try:
        skill_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    # DamageSync appends a five-digit source-instance suffix. The resulting
    # nine-digit effect ID retains one final variant digit.
    if skill_id >= 1_000_000_000_000:
        skill_id //= 10_000
    if 100_000_000 <= skill_id <= 9_999_999_999 and skill_id % 10 == 0:
        skill_id //= 10
    return skill_id


def is_player_skill(skill_id: int) -> bool:
    return PLAYER_SKILL_MIN <= skill_id <= PLAYER_SKILL_MAX


def profession_from_skill(value) -> int:
    """Infer a player profession from the class-specific 86xx skill range."""
    skill_id = normalize_network_skill_id(value)
    if 86_010_000 <= skill_id < 86_080_000:
        profession = (skill_id // 10_000) % 100
        if 1 <= profession <= 7:
            return 1_200_000 + profession
    return 0


def plausible_name(value) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().replace("\x00", "")
    if not text or len(text) > MAX_PROFILE_TEXT or not _CJK_RE.search(text):
        return ""
    if any(ord(char) < 0x20 for char in text):
        return ""
    # Names are compact labels. This avoids treating dialogue and descriptions
    # as entity names when a profile response contains both.
    if len(text) > 20 or any(char in text for char in "，。！？；：\n\r"):
        return ""
    return text


def direct_numeric_map(value) -> dict[int, object]:
    result: dict[int, object] = {}
    for key, item in map_pairs(value):
        if isinstance(key, bool):
            continue
        try:
            parsed = int(key)
        except (TypeError, ValueError, OverflowError):
            continue
        result[parsed] = item
    return result


def stable_team_actor_id(token: str, role_number: object = 0) -> int:
    """Build a non-entity actor ID for a network team-member token."""
    try:
        parsed_role_number = abs(int(role_number))
    except (TypeError, ValueError, OverflowError):
        parsed_role_number = 0
    if parsed_role_number:
        return -parsed_role_number
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    token_number = int.from_bytes(digest, "big") & ((1 << 63) - 1)
    return -(token_number or 1)


class NetworkPacketParser:
    """Stateful parser for packets captured after decrypt and RPC decoding."""

    def __init__(
        self,
        team_profile_cache: dict[str, dict] | None = None,
        boss_template_catalog: dict[str, dict] | None = None,
        *,
        remembered_self_token: str = "",
        allow_cached_projection_roster: bool = False,
        boss_name_allowlist: Iterable[str] | None = None,
    ):
        self.pointer_entities: dict[int, int] = {}
        self.pointer_state: dict[int, dict] = {}
        self.entity_profiles: dict[int, dict] = {}
        self.party_ids: set[int] = set()
        self.party_tokens: set[str] = set()
        self.authoritative_party_tokens: set[str] = set()
        self.other_party_tokens: set[str] = set()
        self.party_member_count = 0
        self.party_seen = False
        self.last_party_activity_100ns = 0
        self.self_id: int | None = None
        self.self_token: str | None = None
        self.self_confirmed = False
        self.token_actors: dict[str, int] = {}
        self.actor_tokens: dict[int, str] = {}
        self.token_max_hp: dict[str, float] = {}
        self.team_profile_markers: dict[str, int] = {}
        self.self_profile_marker = 0
        self.readiness_expected_members = 0
        self.readiness_tokens: set[str] = set()
        self.entity_max_hp: dict[int, float] = {}
        self.entity_current_hp: dict[int, float] = {}
        self.entity_current_hp_time: dict[int, int] = {}
        self.combat_source_actors: set[int] = set()
        self.actor_profession_hints: dict[int, int] = {}
        self.pending_target_hits: dict[int, list[tuple[int, int, int]]] = {}
        self.pending_entity_hits: dict[int, list[tuple[int, int, int]]] = {}
        self.pending_exact_damage: dict[int, list[tuple[int, int, int]]] = {}
        self.player_attackers: set[int] = set()
        self.root_pointers: set[int] = set()
        self.pointer_candidates: dict[int, tuple[int, int]] = {}
        self.recent_target_id: int | None = None
        self.recent_target_time_100ns = 0
        self.scene_id: int | None = None
        self.pending_local_casts: list[tuple[int, int]] = []
        self.recent_skill_sources: list[tuple[int, int, int]] = []
        self.pending_boss_signal: tuple[int, str] | None = None
        self.confirmed_boss_entities: set[int] = set()
        self.entity_template_ids: dict[int, int] = {}
        self.encounter_auxiliary_entities: dict[int, tuple[int, ...]] = {}
        self.defeated_boss_entities: set[int] = set()
        self.active_boss_entity_id: int | None = None
        self.active_boss_time_100ns = 0
        self.active_boss_pointer: int | None = None
        self.active_boss_pointer_time_100ns = 0
        self.combat_mode_pointers: set[int] = set()
        self.runtime_entity_names: dict[int, str] = {}
        self.allow_cached_projection_roster = bool(
            allow_cached_projection_roster
        )
        self.boss_template_catalog_enabled = boss_template_catalog is not None
        self.boss_template_catalog: dict[str, dict] = {}
        self.boss_name_allowlist = tuple(
            dict.fromkeys(
                normalized
                for value in (boss_name_allowlist or ())
                if (normalized := normalize_boss_name(value))
            )
        )
        if isinstance(boss_template_catalog, dict):
            for raw_template_id, raw_metadata in boss_template_catalog.items():
                if not isinstance(raw_metadata, dict):
                    continue
                try:
                    template_id = int(raw_template_id)
                    boss_type = int(raw_metadata.get("boss_type", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if template_id > 0 and boss_type == HUD_BOSS_TYPE:
                    self.boss_template_catalog[str(template_id)] = dict(raw_metadata)
        self.team_profile_cache: dict[str, dict] = {}
        self.team_profile_cache_dirty = False
        if isinstance(team_profile_cache, dict):
            for raw_token, raw_profile in team_profile_cache.items():
                token = self._team_token(raw_token)
                if not token or not isinstance(raw_profile, dict):
                    continue
                profile = self._clean_cached_team_profile(raw_profile)
                if profile:
                    self.team_profile_cache[token] = profile
        remembered_token = self._team_token(remembered_self_token)
        self.remembered_self_token = (
            remembered_token
            if remembered_token in self.team_profile_cache
            else ""
        )

    @staticmethod
    def _args(record: dict) -> list:
        value = record.get("decoded_arguments")
        return value if isinstance(value, list) else []

    @staticmethod
    def _base_update(record: dict) -> dict:
        return {
            "filetime_100ns": int(record.get("filetime_100ns", 0)),
            "event_time": record.get("event_time", ""),
            "source_method": str(record.get("method", "")),
        }

    def _bind_pointer(
        self, pointer: int, entity_id: int, record: dict
    ) -> list[tuple[str, dict]]:
        if not pointer or not entity_id or pointer in self.root_pointers:
            return []
        existing_entity_id = self.pointer_entities.get(pointer)
        if existing_entity_id is not None and existing_entity_id != entity_id:
            # ActorBuffStateSync carries a buff source, not necessarily the
            # ScriptEntity owner. Never let it replace a pointer that already
            # has stronger spell/damage evidence.
            return []
        if (
            entity_id == self.active_boss_entity_id
            and self.active_boss_pointer not in (None, pointer)
        ):
            # A ScriptEntity has one authoritative HP stream. Reusing cached
            # state from another pointer is how an add's HP used to overwrite
            # the locked Boss midway through an encounter.
            self.pointer_candidates.pop(pointer, None)
            self.pointer_state.pop(pointer, None)
            return []
        pending_state = self.pointer_state.get(pointer)
        if entity_id == self.active_boss_entity_id and pending_state:
            try:
                pending_max_hp = float(pending_state.get("max_hp", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                pending_max_hp = 0.0
            known_max_hp = float(self.entity_max_hp.get(entity_id, 0) or 0)
            if (
                known_max_hp > 0
                and pending_max_hp > 0
                and not 0.5 <= pending_max_hp / known_max_hp <= 1.5
            ):
                # A respawning add can reuse the same hit cadence as its Boss.
                # Its much smaller HP stream must never take over the Boss lock.
                self.pointer_candidates.pop(pointer, None)
                self.pointer_state.pop(pointer, None)
                return []
        self.pointer_candidates.pop(pointer, None)
        self.pointer_entities[pointer] = entity_id
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        if (
            entity_id == self.active_boss_entity_id
            and self.active_boss_pointer is None
            and self._boss_pointer_has_evidence(pointer, timestamp)
        ):
            self.active_boss_pointer = pointer
            self.active_boss_pointer_time_100ns = timestamp
        pending = self.pointer_state.pop(pointer, None)
        updates: list[tuple[str, dict]] = []
        if pending:
            update = self._base_update(record)
            update.update(pending)
            update["entity_id"] = entity_id
            updates.append(("monster", update))
            try:
                max_hp = float(pending.get("max_hp", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                max_hp = 0.0
            try:
                current_hp = float(pending.get("current_hp", -1))
            except (TypeError, ValueError, OverflowError):
                current_hp = -1.0
            if current_hp >= 0:
                self.entity_current_hp[entity_id] = current_hp
                self.entity_current_hp_time[entity_id] = int(
                    record.get("filetime_100ns", 0) or 0
                )
            if max_hp > 0:
                if entity_id == self.active_boss_entity_id:
                    max_hp = max(max_hp, self.entity_max_hp.get(entity_id, 0.0))
                self.entity_max_hp[entity_id] = max_hp
            if int(pending.get("boss_type", 0) or 0) == HUD_BOSS_TYPE:
                self._activate_boss(entity_id, record)
        updates.extend(self._team_hp_bindings(record))
        return updates

    def _bind_candidate_pointers(
        self, entity_id: int, record: dict
    ) -> list[tuple[str, dict]]:
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        updates: list[tuple[str, dict]] = []
        for pointer, (candidate_id, candidate_time) in list(
            self.pointer_candidates.items()
        ):
            elapsed = timestamp - candidate_time
            if candidate_id != entity_id or not 0 <= elapsed <= POINTER_BIND_WINDOW_100NS:
                continue
            updates.extend(self._bind_pointer(pointer, entity_id, record))
        return updates

    def _pointer_update(
        self, pointer: int, values: dict, record: dict
    ) -> list[tuple[str, dict]]:
        if not pointer or not values:
            return []
        entity_id = self.pointer_entities.get(pointer)
        if entity_id:
            update = self._base_update(record)
            update.update(values)
            update["entity_id"] = entity_id
            return [("monster", update)]
        pending = self.pointer_state.setdefault(pointer, {})
        pending.update(values)
        # Pointer lifetimes are short across scene changes; a bounded cache also
        # prevents non-combat entities from accumulating indefinitely.
        if len(self.pointer_state) > 4096:
            oldest = next(iter(self.pointer_state))
            self.pointer_state.pop(oldest, None)
        return []

    def _name_matches_boss_allowlist(self, value: object) -> bool:
        normalized = normalize_boss_name(value)
        return bool(
            normalized
            and any(alias in normalized for alias in self.boss_name_allowlist)
        )

    def _profile_update(
        self, entity_id: int, record: dict, **values
    ) -> tuple[str, dict] | None:
        profile_name = plausible_name(values.get("name"))
        late_target = bool(
            entity_id == self.recent_target_id
            or entity_id in self.pending_entity_hits
        )
        if (
            profile_name
            and late_target
            and self._name_matches_boss_allowlist(profile_name)
            and entity_id != self.self_id
            and entity_id not in self.party_ids
            and entity_id not in self.actor_tokens
        ):
            self.runtime_entity_names[entity_id] = profile_name
            self._activate_boss(entity_id, record, damage_evidence=True)
            values.setdefault("entity_type", "Boss")
            values.setdefault("boss_type", HUD_BOSS_TYPE)
            values.setdefault("boss_rank", 3)
        token = self._team_token(values.get("user_token"))
        if token:
            cached_profile = self.team_profile_cache.get(token, {})
            for key in ("name", "profession_id", "level", "role_number"):
                if values.get(key) in (None, "") and cached_profile.get(key) not in (
                    None,
                    "",
                ):
                    values[key] = cached_profile[key]
            next_cached = self._clean_cached_team_profile(values)
            if next_cached:
                merged_cached = dict(cached_profile)
                merged_cached.update(next_cached)
                if merged_cached != cached_profile:
                    self.team_profile_cache[token] = merged_cached
                    self.team_profile_cache_dirty = True
        values = {key: value for key, value in values.items() if value not in (None, "")}
        if not values:
            return None
        cached = self.entity_profiles.setdefault(entity_id, {})
        changed = {key: value for key, value in values.items() if cached.get(key) != value}
        if not changed:
            return None
        cached.update(changed)
        update = self._base_update(record)
        update["entity_id"] = entity_id
        update.update(changed)
        return "profile", update

    @staticmethod
    def _clean_cached_team_profile(value: dict) -> dict:
        result: dict[str, object] = {}
        name = plausible_name(value.get("name"))
        if name:
            result["name"] = name
        for key in ("profession_id", "level", "role_number"):
            try:
                parsed = int(value.get(key, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                parsed = 0
            if parsed:
                result[key] = parsed
        return result

    def take_team_profile_cache(self) -> dict[str, dict] | None:
        if not self.team_profile_cache_dirty:
            return None
        self.team_profile_cache_dirty = False
        return {
            token: dict(profile)
            for token, profile in self.team_profile_cache.items()
        }

    def current_self_identity(self) -> dict[str, object] | None:
        """Return only identity fields previously confirmed by network packets."""
        if not self.self_confirmed or not self.self_token:
            return None
        profile = self._clean_cached_team_profile(
            self.team_profile_cache.get(self.self_token, {})
        )
        if not profile.get("name"):
            return None
        return {"user_token": self.self_token, **profile}

    def _cache_token_profiles(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        method = str(record.get("method", "")).casefold()
        if not any(
            marker in method
            for marker in ("team", "group", "friend", "whisper", "invite", "tarot")
        ):
            return []
        updates: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for node in walk_values(args):
            fields = direct_numeric_map(node)
            if not fields:
                continue
            token_at_two = self._team_token(fields.get(2))
            token_at_zero = self._team_token(fields.get(0))
            token = token_at_two or token_at_zero
            if not token or token in seen:
                continue

            profile: dict[str, object] = {}
            marker = 0
            if token_at_two and plausible_name(fields.get(5)):
                profile = {
                    "name": plausible_name(fields.get(5)),
                    "role_number": fields.get(6),
                    "profession_id": fields.get(8),
                    "level": fields.get(9),
                }
                marker = fields.get(27, 0)
            elif token_at_two and plausible_name(fields.get(4)):
                profile = {
                    "name": plausible_name(fields.get(4)),
                    "profession_id": fields.get(5),
                    "level": fields.get(7),
                }
            elif token_at_zero and plausible_name(fields.get(7)):
                profile = {
                    "name": plausible_name(fields.get(7)),
                    "role_number": fields.get(1),
                    "profession_id": fields.get(11),
                    "level": fields.get(10),
                }
                marker = fields.get(17, 0)
            elif token_at_zero and plausible_name(fields.get(5)):
                profile = {
                    "name": plausible_name(fields.get(5)),
                    "profession_id": fields.get(4),
                    "level": fields.get(2),
                }
            elif token_at_zero and plausible_name(fields.get(4)):
                profile = {
                    "name": plausible_name(fields.get(4)),
                    "profession_id": fields.get(3),
                    "level": fields.get(1),
                }
            elif token_at_zero and plausible_name(fields.get(3)):
                profile = {
                    "name": plausible_name(fields.get(3)),
                    "role_number": fields.get(2),
                    "profession_id": fields.get(5),
                    "level": fields.get(6),
                }
            else:
                continue

            cleaned = self._clean_cached_team_profile(profile)
            if not cleaned:
                continue
            seen.add(token)
            cached = self.team_profile_cache.get(token, {})
            merged = dict(cached)
            merged.update(cleaned)
            if merged != cached:
                self.team_profile_cache[token] = merged
                self.team_profile_cache_dirty = True
            try:
                parsed_marker = int(marker or 0)
            except (TypeError, ValueError, OverflowError):
                parsed_marker = 0
            if parsed_marker:
                self.team_profile_markers[token] = parsed_marker

            actor_id = self.token_actors.get(token)
            if actor_id is None or not (
                token == self.self_token
                or token in self.party_tokens
                or token in self.authoritative_party_tokens
            ):
                continue
            update = self._profile_update(
                actor_id,
                record,
                user_token=token,
                entity_type="Player",
                **cleaned,
            )
            if update:
                updates.append(update)
        return updates

    def _activate_boss(
        self, entity_id: int, record: dict, *, damage_evidence: bool = False
    ) -> bool:
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        self.confirmed_boss_entities.add(entity_id)
        current_entity_id = self.active_boss_entity_id
        if current_entity_id is not None and current_entity_id != entity_id:
            current_hp = self.entity_current_hp.get(current_entity_id)
            current_is_alive = current_hp is None or current_hp > 0
            current_name = self.runtime_entity_names.get(current_entity_id) or str(
                self.entity_profiles.get(current_entity_id, {}).get("name", "")
            )
            incoming_name = self.runtime_entity_names.get(entity_id) or str(
                self.entity_profiles.get(entity_id, {}).get("name", "")
            )
            phase_continuation = boss_phase_continues(
                current_name, incoming_name
            )
            incoming_is_trusted = self._name_matches_boss_allowlist(
                incoming_name
            )
            # Native metadata describes every spawned Boss-like unit, including
            # adds and mechanics. Metadata alone must never steal the active
            # lock. A name-confirmed allowlisted Boss can take over on direct
            # damage; untrusted Boss-like units wait for the old target to die.
            if not phase_continuation and (
                not damage_evidence
                or (current_is_alive and not incoming_is_trusted)
            ):
                return False
        if entity_id in self.defeated_boss_entities:
            if not damage_evidence:
                return False
            self.defeated_boss_entities.discard(entity_id)
        if self.active_boss_entity_id != entity_id:
            self.active_boss_pointer = None
            self.active_boss_pointer_time_100ns = 0
            self.pending_target_hits.clear()
            self.pending_entity_hits.clear()
            self.pending_exact_damage.clear()
        self.active_boss_entity_id = entity_id
        self.active_boss_time_100ns = timestamp
        self.pending_boss_signal = None
        return True

    def _is_active_encounter_auxiliary(self, entity_id: int) -> bool:
        parent_templates = self.encounter_auxiliary_entities.get(entity_id, ())
        active_template = self.entity_template_ids.get(
            int(self.active_boss_entity_id or 0), 0
        )
        if active_template and active_template in parent_templates:
            return True
        active_id = int(self.active_boss_entity_id or 0)
        active_name = self.runtime_entity_names.get(active_id, "") or str(
            self.entity_profiles.get(active_id, {}).get("name", "")
        )
        return bool(
            parent_templates
            and normalize_boss_name("星象仪者")
            in normalize_boss_name(active_name)
        )

    def _release_active_boss(self, entity_id: int) -> None:
        if entity_id != self.active_boss_entity_id:
            return
        template_id = int(self.entity_template_ids.get(entity_id, 0) or 0)
        if template_id in MULTIPHASE_BOSS_TEMPLATE_IDS:
            # This entity repeatedly becomes untargetable while encounter adds
            # are active. Clear only the expired ScriptEntity pointer; keeping
            # the Boss lock prevents every add death from becoming a new pull.
            for pointer, mapped_entity_id in list(self.pointer_entities.items()):
                if mapped_entity_id != entity_id:
                    continue
                self.pointer_entities.pop(pointer, None)
                self.pointer_state.pop(pointer, None)
                self.pointer_candidates.pop(pointer, None)
                self.pending_target_hits.pop(pointer, None)
                self.combat_mode_pointers.discard(pointer)
            self.active_boss_pointer = None
            self.active_boss_pointer_time_100ns = 0
            return
        self.defeated_boss_entities.add(entity_id)
        for pointer, mapped_entity_id in list(self.pointer_entities.items()):
            if mapped_entity_id != entity_id:
                continue
            self.pointer_entities.pop(pointer, None)
            self.pointer_state.pop(pointer, None)
            self.pointer_candidates.pop(pointer, None)
            self.pending_target_hits.pop(pointer, None)
            self.combat_mode_pointers.discard(pointer)
        self.pending_exact_damage.pop(entity_id, None)
        self.pending_entity_hits.pop(entity_id, None)
        self.active_boss_entity_id = None
        self.active_boss_time_100ns = 0
        self.active_boss_pointer = None
        self.active_boss_pointer_time_100ns = 0

    def _record_boss_signal(self, record: dict, args: list) -> None:
        if str(record.get("method", "")) != "OnMsgPostAkEvent":
            return
        signal = next(
            (
                value.strip()
                for value in walk_values(args)
                if isinstance(value, str)
                and HUD_BOSS_TEMPLATE_RE.search(value.strip())
            ),
            "",
        )
        if not signal:
            return
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        if (
            self.active_boss_entity_id is not None
            and timestamp >= self.active_boss_time_100ns
            and timestamp - self.active_boss_time_100ns
            <= BOSS_SIGNAL_ACTIVE_WINDOW_100NS
        ):
            return
        self.pending_boss_signal = (timestamp, signal)

    def _boss_signal_profile_update(
        self, entity_id: int, record: dict
    ) -> tuple[str, dict] | None:
        pending = self.pending_boss_signal
        if pending is None:
            return None
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        signal_time, signal = pending
        elapsed = timestamp - signal_time
        if elapsed < 0 or elapsed > BOSS_SIGNAL_BIND_WINDOW_100NS:
            if elapsed > BOSS_SIGNAL_BIND_WINDOW_100NS:
                self.pending_boss_signal = None
            return None
        if (
            entity_id == self.self_id
            or entity_id in self.party_ids
            or entity_id in self.actor_tokens
        ):
            return None
        self._activate_boss(entity_id, record)
        return self._profile_update(
            entity_id,
            record,
            name="首领",
            entity_type="Boss",
            boss_type=HUD_BOSS_TYPE,
            boss_rank=3,
            template_path=signal[:160],
        )

    @staticmethod
    def _team_token(value: object) -> str:
        if not isinstance(value, str):
            return ""
        token = value.strip().replace("\x00", "")
        if (
            len(token) < 12
            or len(token) > 128
            or any(ord(char) < 0x20 for char in token)
        ):
            return ""
        return token

    def _bind_team_token(self, token: str, actor_id: int) -> int:
        existing = self.token_actors.get(token)
        if existing is not None:
            return existing
        self.token_actors[token] = actor_id
        self.actor_tokens[actor_id] = token
        return actor_id

    def _rebind_team_actor(
        self, token: str, actor_id: int, record: dict
    ) -> list[tuple[str, dict]]:
        old_actor = self.token_actors.get(token)
        if old_actor == actor_id or not actor_id:
            return []
        self_token = token == self.self_token
        if (
            old_actor is not None
            and old_actor > 0
            and old_actor in self.combat_source_actors
        ):
            return []
        existing_token = self.actor_tokens.get(actor_id)
        if existing_token and existing_token != token:
            return []
        updates: list[tuple[str, dict]] = []
        if old_actor is not None:
            self.actor_tokens.pop(old_actor, None)
            if old_actor in self.party_ids:
                self.party_ids.remove(old_actor)
                self.party_ids.add(actor_id)
            updates.append(
                (
                    "actor_merge",
                    {
                        **self._base_update(record),
                        "from_actor_id": old_actor,
                        "to_actor_id": actor_id,
                    },
                )
            )
            old_profile = self.entity_profiles.pop(old_actor, {})
            if old_profile:
                canonical = self.entity_profiles.setdefault(actor_id, {})
                changed = {
                    key: value
                    for key, value in old_profile.items()
                    if value not in (None, "") and canonical.get(key) != value
                }
                if changed:
                    canonical.update(changed)
                    updates.append(
                        (
                            "profile",
                            {
                                **self._base_update(record),
                                "entity_id": actor_id,
                                **changed,
                            },
                        )
                    )
        self.token_actors[token] = actor_id
        self.actor_tokens[actor_id] = token
        cached_profile = self.team_profile_cache.get(token, {})
        cached_update = self._profile_update(
            actor_id,
            record,
            user_token=token,
            entity_type="Player",
            **cached_profile,
        )
        if cached_update:
            updates.append(cached_update)
        if self_token:
            self.self_id = actor_id
            self.self_confirmed = True
            self.party_ids.discard(actor_id)
            self.party_tokens.discard(token)
            updates.append(
                (
                    "identity",
                    {
                        **self._base_update(record),
                        "entity_id": actor_id,
                        "tentative": False,
                    },
                )
            )
        elif token in self.party_tokens and actor_id != self.self_id:
            self.party_ids.add(actor_id)
        if updates:
            updates.append(self._party_update(record, authoritative=False))
        return updates

    def _token_profession_id(self, token: str) -> int:
        actor_id = self.token_actors.get(token, 0)
        sources = (
            self.entity_profiles.get(actor_id, {}),
            self.team_profile_cache.get(token, {}),
        )
        for source in sources:
            try:
                profession_id = int(source.get("profession_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                profession_id = 0
            if profession_id:
                return profession_id
        return 0

    @staticmethod
    def _ordered_team_token_key(token: str) -> bytes | None:
        """Decode the per-scene team token so its embedded order is sortable."""
        try:
            encoded = token.encode("ascii")
            decoded = base64.urlsafe_b64decode(
                encoded + b"=" * ((-len(encoded)) % 4)
            )
        except (UnicodeEncodeError, ValueError):
            return None
        return decoded if len(decoded) >= 8 else None

    def _ordered_projection_profiles(
        self, ordered_tokens: list[str]
    ) -> list[dict]:
        current_profiles = [
            self.team_profile_cache.get(token, {}) for token in ordered_tokens
        ]
        current_names = [
            plausible_name(profile.get("name")) for profile in current_profiles
        ]
        if current_names and all(
            name and "投影" in name for name in current_names
        ):
            return [dict(profile) for profile in current_profiles]
        if not self.allow_cached_projection_roster:
            return []

        current_keys = [
            self._ordered_team_token_key(token) for token in ordered_tokens
        ]
        if any(key is None or len(key) < 9 for key in current_keys):
            return []
        current_counter = min(
            int.from_bytes(key[:4], "big")
            for key in current_keys
            if key is not None
        )

        groups: dict[tuple[bytes, bytes], list[tuple[bytes, dict]]] = {}
        for token, profile in self.team_profile_cache.items():
            name = plausible_name(profile.get("name"))
            key = self._ordered_team_token_key(token)
            if not name or "投影" not in name or key is None or len(key) < 9:
                continue
            groups.setdefault((key[:3], key[4:9]), []).append(
                (key, dict(profile))
            )

        candidates: list[tuple[int, list[dict]]] = []
        for values in groups.values():
            if len(values) != len(ordered_tokens):
                continue
            ordered_values = sorted(values, key=lambda item: item[0])
            candidate_counter = min(
                int.from_bytes(key[:4], "big") for key, _profile in values
            )
            distance = current_counter - candidate_counter
            if not (0 < distance <= 0x10000):
                continue
            candidates.append(
                (distance, [profile for _key, profile in ordered_values])
            )
        if not candidates:
            return []
        return min(candidates, key=lambda item: item[0])[1]

    def _ordered_projection_bindings(
        self, record: dict
    ) -> list[tuple[str, dict]]:
        """Rebind projection actors after a scene gives them new entity IDs.

        Projection instances allocate both their team tokens and entity IDs in
        the same order.  Use that relationship only for a complete projection
        roster and only when every available profession agrees, so ordinary
        teams and partial combat observations can never be guessed by order.
        """
        roster_tokens = set(self.party_tokens)
        if len(roster_tokens) < 2:
            return []

        token_keys = {
            token: self._ordered_team_token_key(token) for token in roster_tokens
        }
        if any(key is None for key in token_keys.values()):
            return []
        ordered_tokens = sorted(roster_tokens, key=lambda token: token_keys[token])
        ordered_profiles = self._ordered_projection_profiles(ordered_tokens)
        if len(ordered_profiles) != len(ordered_tokens):
            return []

        for token, profile in zip(ordered_tokens, ordered_profiles):
            cached = self.team_profile_cache.get(token, {})
            merged = dict(cached)
            merged.update(profile)
            if merged != cached:
                self.team_profile_cache[token] = merged
                self.team_profile_cache_dirty = True

        roster_actors = sorted(
            entity_id
            for entity_id in self.combat_source_actors
            if entity_id != self.self_id
            and entity_id not in self.confirmed_boss_entities
        )
        if len(roster_actors) != len(ordered_tokens):
            return []

        active_actor_set = set(roster_actors)
        for token, actor_id in zip(ordered_tokens, roster_actors):
            bound_actor = self.token_actors.get(token)
            if bound_actor in active_actor_set and bound_actor != actor_id:
                return []
            actor_profession = int(self.actor_profession_hints.get(actor_id, 0) or 0)
            token_profession = self._token_profession_id(token)
            if (
                not actor_profession
                or not token_profession
                or actor_profession != token_profession
            ):
                return []

        pending = [
            (token, actor_id)
            for token, actor_id in zip(ordered_tokens, roster_actors)
            if self.token_actors.get(token) != actor_id
        ]
        if not pending:
            return []

        updates: list[tuple[str, dict]] = []
        for token, actor_id in pending:
            updates.extend(self._rebind_team_actor(token, actor_id, record))
        return updates

    def _repair_projection_profession_bindings(
        self, record: dict
    ) -> list[tuple[str, dict]]:
        roster_tokens = set(self.party_tokens)
        if self.self_token:
            roster_tokens.add(self.self_token)
        projection_tokens = {
            token
            for token in roster_tokens
            if "投影"
            in plausible_name(self.team_profile_cache.get(token, {}).get("name"))
        }
        if len(projection_tokens) < 2:
            return []

        candidate_tokens = set(projection_tokens)
        if self.self_token and self._token_profession_id(self.self_token):
            candidate_tokens.add(self.self_token)
        candidate_actors = {
            actor_id
            for actor_id in self.combat_source_actors
            if actor_id not in self.confirmed_boss_entities
            and self.actor_profession_hints.get(actor_id, 0)
        }
        tokens_by_profession: dict[int, list[str]] = {}
        actors_by_profession: dict[int, list[int]] = {}
        for token in candidate_tokens:
            profession_id = self._token_profession_id(token)
            if profession_id:
                tokens_by_profession.setdefault(profession_id, []).append(token)
        for actor_id in candidate_actors:
            profession_id = int(self.actor_profession_hints.get(actor_id, 0) or 0)
            if profession_id:
                actors_by_profession.setdefault(profession_id, []).append(actor_id)

        bindings: dict[str, int] = {}
        for profession_id, tokens in tokens_by_profession.items():
            actors = actors_by_profession.get(profession_id, [])
            if len(tokens) == 1 and len(actors) == 1:
                bindings[tokens[0]] = actors[0]
        if len(bindings) < 2 or all(
            self.token_actors.get(token) == actor_id
            for token, actor_id in bindings.items()
        ):
            return []

        old_actors = {token: self.token_actors.get(token) for token in bindings}
        for token, old_actor in old_actors.items():
            if old_actor is not None and self.actor_tokens.get(old_actor) == token:
                self.actor_tokens.pop(old_actor, None)
        for actor_id in bindings.values():
            old_token = self.actor_tokens.pop(actor_id, None)
            if old_token in candidate_tokens:
                self.token_actors.pop(old_token, None)

        updates: list[tuple[str, dict]] = []
        for token, actor_id in bindings.items():
            old_actor = old_actors.get(token)
            self.token_actors[token] = actor_id
            self.actor_tokens[actor_id] = token
            if old_actor is not None and old_actor < 0 and old_actor != actor_id:
                updates.append(
                    (
                        "actor_merge",
                        {
                            **self._base_update(record),
                            "from_actor_id": old_actor,
                            "to_actor_id": actor_id,
                        },
                    )
                )
            cached_profile = self.team_profile_cache.get(token, {})
            profile = self._profile_update(
                actor_id,
                record,
                user_token=token,
                entity_type="Player",
                **cached_profile,
            )
            if profile:
                updates.append(profile)
            if token == self.self_token:
                self.self_id = actor_id
                self.self_confirmed = True
                # A projection roster initially treats every token as a remote
                # member.  Once profession evidence resolves the local token,
                # remove its real actor from the remote-member set as well.
                # Leaving it there makes the model count the same person twice
                # and can cause a later party update to attach another name to
                # the local actor.
                self.party_ids.discard(actor_id)
                self.party_tokens.discard(token)
                self.other_party_tokens.discard(token)
                updates.append(
                    (
                        "identity",
                        {
                            **self._base_update(record),
                            "entity_id": actor_id,
                            "tentative": False,
                        },
                    )
                )
            else:
                if actor_id != self.self_id:
                    self.party_ids.add(actor_id)
        if updates:
            self._refresh_party_member_count()
            self.party_member_count = min(
                MAX_PARTY_MEMBERS,
                max(self.party_member_count, len(candidate_tokens)),
            )
            updates.append(self._party_update(record, authoritative=False))
        return updates

    def _team_hp_bindings(self, record: dict) -> list[tuple[str, dict]]:
        updates = self._repair_projection_profession_bindings(record)
        # A max-HP value alone is not enough: unrelated scene entities can have
        # the same value. Only actors observed as combat sources are eligible.
        # Repeat after each merge because resolving one duplicate profession can
        # make the remaining member unambiguous.
        changed = True
        while changed:
            changed = False
            for entity_id in list(self.combat_source_actors):
                if entity_id == self.self_id or entity_id in self.actor_tokens:
                    continue
                available = [
                    token
                    for token in self.party_tokens
                    if self.token_actors.get(token, 0) <= 0
                    or self.token_actors.get(token, 0)
                    not in self.combat_source_actors
                ]
                if not available:
                    continue

                profession_id = self.actor_profession_hints.get(entity_id, 0)
                profession_matches = [
                    token
                    for token in available
                    if not profession_id
                    or self._token_profession_id(token) == profession_id
                ]

                max_hp = self.entity_max_hp.get(entity_id, 0.0)
                max_hp_matches = [
                    token
                    for token in profession_matches
                    if max_hp > 0
                    and abs(self.token_max_hp.get(token, -1.0) - max_hp) <= 0.5
                ]
                current_hp = self.entity_current_hp.get(entity_id, -1.0)
                full_hp_matches = [
                    token
                    for token in profession_matches
                    if current_hp > 0
                    and abs(self.token_max_hp.get(token, -1.0) - current_hp) <= 0.5
                ]

                candidates: list[str] = []
                if len(max_hp_matches) == 1:
                    candidates = max_hp_matches
                elif len(full_hp_matches) == 1:
                    candidates = full_hp_matches
                elif profession_id and len(profession_matches) == 1:
                    candidates = profession_matches
                if len(candidates) != 1:
                    continue
                merged = self._rebind_team_actor(candidates[0], entity_id, record)
                if merged:
                    updates.extend(merged)
                    changed = True
                    break
        updates.extend(self._ordered_projection_bindings(record))
        return updates

    def _reset_scene_combat_bindings(self) -> None:
        self.pointer_entities.clear()
        self.pointer_state.clear()
        self.pointer_candidates.clear()
        self.recent_target_id = None
        self.recent_target_time_100ns = 0
        self.entity_max_hp.clear()
        self.entity_current_hp.clear()
        self.entity_current_hp_time.clear()
        self.combat_source_actors.clear()
        self.actor_profession_hints.clear()
        self.pending_target_hits.clear()
        self.pending_entity_hits.clear()
        self.pending_exact_damage.clear()
        self.pending_local_casts.clear()
        self.recent_skill_sources.clear()
        self.pending_boss_signal = None
        self.confirmed_boss_entities.clear()
        self.entity_template_ids.clear()
        self.encounter_auxiliary_entities.clear()
        self.defeated_boss_entities.clear()
        self.active_boss_entity_id = None
        self.active_boss_time_100ns = 0
        self.active_boss_pointer = None
        self.active_boss_pointer_time_100ns = 0
        self.combat_mode_pointers.clear()

    def _record_combat_source(
        self,
        record: dict,
        actor_id: int,
        skill_id: object = 0,
        *,
        bind_pointer: bool = False,
    ) -> list[tuple[str, dict]]:
        if not parse_combat_entity_id(actor_id):
            return []
        self.combat_source_actors.add(actor_id)
        normalized_skill_id = normalize_network_skill_id(skill_id)
        profession_id = profession_from_skill(normalized_skill_id)
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        if profession_id:
            self.actor_profession_hints[actor_id] = profession_id
        updates: list[tuple[str, dict]] = []
        if profession_id:
            matching_casts = [
                item
                for item in self.pending_local_casts
                if item[0] == normalized_skill_id
                and 0 <= timestamp - item[1] <= LOCAL_CAST_MATCH_WINDOW_100NS
            ]
            if matching_casts:
                updates.extend(self._confirm_local_actor(actor_id, record))
                self.pending_local_casts = [
                    item for item in self.pending_local_casts if item not in matching_casts
                ]
        if actor_id == self.self_id and self.self_confirmed:
            updates.extend(
                self._remembered_self_identity_updates(
                    record,
                    profession_id=profession_id,
                )
            )
        if is_player_skill(normalized_skill_id):
            source = (normalized_skill_id, actor_id, timestamp)
            if not self.recent_skill_sources or self.recent_skill_sources[-1] != source:
                self.recent_skill_sources.append(source)
            if len(self.recent_skill_sources) > 4096:
                self.recent_skill_sources = self.recent_skill_sources[-2048:]
        pointer = int(record.get("script_entity", 0) or 0)
        if bind_pointer and pointer and pointer not in self.root_pointers:
            updates.extend(self._bind_pointer(pointer, actor_id, record))
        updates.extend(self._team_hp_bindings(record))
        return updates

    def _recent_actor_skill(self, actor_id: int, timestamp: int) -> int:
        for skill_id, source_actor_id, source_time in reversed(
            self.recent_skill_sources
        ):
            elapsed = timestamp - source_time
            if source_actor_id == actor_id and 0 <= elapsed:
                if elapsed <= TEAM_HIT_SKILL_WINDOW_100NS:
                    return skill_id
        return 0

    def _is_known_player_actor(self, actor_id: int, timestamp: int) -> bool:
        return bool(
            actor_id
            and actor_id not in self.confirmed_boss_entities
            and (
                actor_id == self.self_id
                or actor_id in self.party_ids
                or actor_id in self.actor_tokens
                or actor_id in self.player_attackers
                or actor_id in self.actor_profession_hints
                or self._recent_actor_skill(actor_id, timestamp)
            )
        )

    def _record_target_hit(
        self, pointer: int, actor_id: int, record: dict
    ) -> None:
        if (
            not pointer
            or pointer in self.root_pointers
            or not parse_combat_entity_id(actor_id)
            or actor_id == self.active_boss_entity_id
        ):
            return
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        pending = self.pending_target_hits.setdefault(pointer, [])
        pending.append(
            (actor_id, self._recent_actor_skill(actor_id, timestamp), timestamp)
        )
        if len(pending) > 4096:
            del pending[:-2048]

    def _record_entity_hit(
        self,
        entity_id: int | None,
        actor_id: int | None,
        skill_id: object,
        record: dict,
    ) -> None:
        """Remember a player action that explicitly names its damage target."""
        if (
            not entity_id
            or not actor_id
            or not parse_combat_entity_id(entity_id)
            or not parse_combat_entity_id(actor_id)
            or actor_id == entity_id
            or actor_id in self.confirmed_boss_entities
        ):
            return
        normalized_skill_id = normalize_network_skill_id(skill_id)
        if not is_player_skill(normalized_skill_id):
            return
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        if not timestamp:
            return
        pending = self.pending_entity_hits.setdefault(entity_id, [])
        # Cast, bullet and spell-agent messages can describe the same action.
        # Collapse only near-identical markers while retaining real repeats.
        if any(
            old_actor == actor_id
            and old_skill == normalized_skill_id
            and 0 <= timestamp - old_time <= 1_500_000
            for old_actor, old_skill, old_time in pending[-24:]
        ):
            return
        pending.append((actor_id, normalized_skill_id, timestamp))
        if len(pending) > 4096:
            del pending[:-2048]

    def _boss_pointer_has_evidence(self, pointer: int, timestamp: int) -> bool:
        boss_id = self.active_boss_entity_id
        if boss_id is None or pointer in self.root_pointers:
            return False
        mapped_entity_id = self.pointer_entities.get(pointer)
        if mapped_entity_id not in (None, boss_id):
            return False
        candidate = self.pointer_candidates.get(pointer)
        if candidate is not None and candidate[0] != boss_id:
            return False
        pending = self.pointer_state.get(pointer, {})
        try:
            pending_max_hp = float(pending.get("max_hp", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            pending_max_hp = 0.0
        known_max_hp = float(self.entity_max_hp.get(boss_id, 0) or 0)
        if (
            known_max_hp > 0
            and pending_max_hp > 0
            and not 0.5 <= pending_max_hp / known_max_hp <= 1.5
        ):
            return False
        hits = [
            item
            for item in self.pending_target_hits.get(pointer, [])
            if item[2] <= timestamp
            and timestamp - item[2] <= TEAM_HIT_SKILL_WINDOW_100NS
            and self._is_known_player_actor(item[0], item[2])
        ]
        if not hits:
            return False
        distinct_actors = {actor_id for actor_id, _skill_id, _time in hits}
        exact_actors = {
            actor_id
            for exact_time, actor_id, _damage in self.pending_exact_damage.get(
                boss_id, []
            )
            if exact_time <= timestamp
            and timestamp - exact_time <= TEAM_HIT_SKILL_WINDOW_100NS
        }
        return bool(
            pointer in self.combat_mode_pointers
            or len(distinct_actors) >= 2
            or distinct_actors.intersection(exact_actors)
        )

    def _recent_skill_matches_hp(self, current_hp: float, timestamp: int) -> bool:
        rounded = int(round(current_hp))
        if abs(current_hp - rounded) > 0.001:
            return False
        for skill_id, _actor_id, source_time in reversed(self.recent_skill_sources):
            elapsed = timestamp - source_time
            if 0 <= elapsed and rounded == skill_id:
                if elapsed <= HP_SKILL_COLLISION_WINDOW_100NS:
                    return True
        return False

    def _valid_boss_hp(self, entity_id: int, current_hp: float, timestamp: int) -> bool:
        if not self._valid_hp_value(current_hp):
            return False
        if self._recent_skill_matches_hp(current_hp, timestamp):
            return False
        max_hp = self.entity_max_hp.get(entity_id, 0.0)
        if max_hp > 0 and current_hp > max_hp * 1.02:
            return False
        previous_hp = self.entity_current_hp.get(entity_id)
        if previous_hp is None or previous_hp <= 0 or current_hp <= previous_hp:
            return True
        # Healing is valid, but an abrupt multi-fold jump is a decoded skill or
        # another entity's HP, not this locked Boss stream.
        upper_bound = max(previous_hp * 1.5, previous_hp + 1_000_000.0)
        if max_hp > 0:
            upper_bound = max(upper_bound, max_hp * 1.02)
        return current_hp <= upper_bound

    @staticmethod
    def _valid_hp_value(value: float, *, maximum: bool = False) -> bool:
        if not math.isfinite(value):
            return False
        if maximum and value <= 0:
            return False
        return 0 <= value < LOW_COMBAT_ENTITY_ID_MIN

    def _valid_encounter_auxiliary_hp(
        self, entity_id: int, current_hp: float, timestamp: int
    ) -> bool:
        # Team members commonly share small max-HP values. A player HP packet
        # can briefly arrive on an auxiliary pointer while the unit respawns;
        # accepting it would turn that collision into a huge false HP drop.
        if any(
            value > 0 and abs(value - current_hp) <= 0.5
            for value in self.token_max_hp.values()
        ):
            return False
        return self._valid_boss_hp(entity_id, current_hp, timestamp)

    @staticmethod
    def _consume_timed_items(items: list[tuple], timestamp: int) -> tuple[list, list]:
        consumed = [item for item in items if int(item[-1]) <= timestamp]
        remaining = [item for item in items if int(item[-1]) > timestamp]
        return consumed, remaining

    def _inferred_team_damage_updates(
        self,
        pointer: int,
        entity_id: int,
        previous_hp: float | None,
        current_hp: float,
        record: dict,
    ) -> list[tuple[str, dict]]:
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        hits, remaining_hits = self._consume_timed_items(
            self.pending_target_hits.get(pointer, []), timestamp
        )
        if remaining_hits:
            self.pending_target_hits[pointer] = remaining_hits
        else:
            self.pending_target_hits.pop(pointer, None)

        directed_hits, remaining_directed = self._consume_timed_items(
            self.pending_entity_hits.get(entity_id, []), timestamp
        )
        if remaining_directed:
            self.pending_entity_hits[entity_id] = remaining_directed
        else:
            self.pending_entity_hits.pop(entity_id, None)
        if directed_hits:
            observed_actors = {
                actor_id for actor_id, _skill_id, _hit_time in hits
            }
            hits.extend(
                item for item in directed_hits if item[0] not in observed_actors
            )

        exact_items = self.pending_exact_damage.get(entity_id, [])
        consumed_exact = [item for item in exact_items if item[0] <= timestamp]
        remaining_exact = [item for item in exact_items if item[0] > timestamp]
        if remaining_exact:
            self.pending_exact_damage[entity_id] = remaining_exact
        else:
            self.pending_exact_damage.pop(entity_id, None)

        if previous_hp is None or current_hp >= previous_hp or not hits:
            return []
        hp_damage = max(0, int(round(previous_hp - current_hp)))
        exact_damage = sum(max(0, damage) for _time, _actor, damage in consumed_exact)
        # Phase changes and respawning adds reset their HP upwards.  Hits and
        # exact local damage are consumed before this point, while the early
        # ``current_hp >= previous_hp`` return above deliberately emits no
        # damage for that reset.  A later monotonic decrease on the same,
        # ownership-confirmed pointer is real encounter damage and must remain
        # eligible; disabling the whole multi-phase template is what made only
        # the local player visible during Astrologer/Ancestor Armor fights.
        residual = max(0, hp_damage - min(hp_damage, exact_damage))
        exact_actors = {actor_id for _time, actor_id, _damage in consumed_exact}
        eligible_hits = [
            (actor_id, skill_id, hit_time)
            for actor_id, skill_id, hit_time in hits
            if parse_combat_entity_id(actor_id)
            and actor_id not in exact_actors
            and actor_id not in self.confirmed_boss_entities
            and self._is_known_player_actor(actor_id, hit_time)
        ]
        if residual <= 0 or not eligible_hits:
            return []

        per_hit, remainder = divmod(residual, len(eligible_hits))
        updates: list[tuple[str, dict]] = []
        for index, (actor_id, skill_id, hit_time) in enumerate(eligible_hits):
            damage = per_hit + (1 if index < remainder else 0)
            if damage <= 0:
                continue
            updates.append(
                (
                    "event",
                    {
                        **self._base_update(record),
                        "filetime_100ns": hit_time,
                        "function": "OnMsgSyncCurrentHp/team-hit",
                        "attacker_id": actor_id,
                        "target_id": entity_id,
                        "skill_id": skill_id,
                        "player_attacker": True,
                        "raw_damage": damage,
                        "damage": damage,
                    },
                )
            )
        return updates

    def _confirm_local_actor(
        self, actor_id: int, record: dict
    ) -> list[tuple[str, dict]]:
        if not parse_combat_entity_id(actor_id):
            return []
        if self.self_token:
            if self.token_actors.get(self.self_token) == actor_id:
                if self.self_id == actor_id and self.self_confirmed:
                    return []
                self.self_id = actor_id
                self.self_confirmed = True
                return [
                    (
                        "identity",
                        {
                            **self._base_update(record),
                            "entity_id": actor_id,
                            "tentative": False,
                        },
                    )
                ]
            return self._rebind_team_actor(self.self_token, actor_id, record)
        if self.self_id == actor_id and self.self_confirmed:
            return []
        old_actor = self.self_id
        self.self_id = actor_id
        self.self_confirmed = True
        updates: list[tuple[str, dict]] = []
        if old_actor and old_actor != actor_id:
            updates.append(
                (
                    "actor_merge",
                    {
                        **self._base_update(record),
                        "from_actor_id": old_actor,
                        "to_actor_id": actor_id,
                    },
                )
            )
        updates.append(
            (
                "identity",
                {
                    **self._base_update(record),
                    "entity_id": actor_id,
                    "tentative": False,
                },
            )
        )
        return updates

    def _local_cast_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        if not args:
            return []
        skill_id = normalize_network_skill_id(args[0])
        if not is_player_skill(skill_id):
            return []
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        recent = [
            (actor_id, source_time)
            for source_skill, actor_id, source_time in self.recent_skill_sources
            if source_skill == skill_id
            and 0 <= timestamp - source_time <= LOCAL_CAST_MATCH_WINDOW_100NS
        ]
        actors = {actor_id for actor_id, _source_time in recent}
        if len(actors) == 1:
            return self._confirm_local_actor(next(iter(actors)), record)
        self.pending_local_casts.append((skill_id, timestamp))
        if len(self.pending_local_casts) > 64:
            self.pending_local_casts = self.pending_local_casts[-32:]
        return []

    def _team_join_success_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        if str(record.get("method", "")) != TEAM_JOIN_SUCCESS_METHOD:
            return []
        updates: list[tuple[str, dict]] = []
        changed = False
        seen: set[str] = set()
        members: list[tuple[str, str, dict[int, object]]] = []
        for node in walk_values(args):
            fields = direct_numeric_map(node)
            token = self._team_token(fields.get(2))
            name = plausible_name(fields.get(5))
            if not token or token in seen or not name:
                continue
            seen.add(token)
            members.append((token, name, fields))

        if self.self_id is not None and self.self_token is None:
            known_self_name = plausible_name(
                self.entity_profiles.get(self.self_id, {}).get("name")
            )
            self_candidates = [
                token
                for token, name, _fields in members
                if known_self_name and name == known_self_name
            ]
            if len(self_candidates) == 1:
                updates.extend(
                    self._confirm_self_token(self_candidates[0], record)
                )

        for token, name, fields in members:
            actor_id = self.token_actors.get(token)
            if actor_id is None:
                actor_id = self._bind_team_token(
                    token, stable_team_actor_id(token, fields.get(6))
                )
            if token != self.self_token:
                changed |= token not in self.party_tokens or actor_id not in self.party_ids
                self.party_tokens.add(token)
                self.party_ids.add(actor_id)
            try:
                max_hp = float(fields.get(13, fields.get(12, 0)) or 0)
            except (TypeError, ValueError, OverflowError):
                max_hp = 0.0
            if max_hp > 0:
                self.token_max_hp[token] = max_hp
            try:
                marker = int(fields.get(27, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                marker = 0
            if marker:
                self.team_profile_markers[token] = marker
            profile = self._profile_update(
                actor_id,
                record,
                name=name,
                role_number=fields.get(6),
                profession_id=fields.get(8),
                level=fields.get(9),
                user_token=token,
                entity_type="Player",
            )
            if profile:
                updates.append(profile)
        if seen:
            self._refresh_party_member_count()
            self._record_party_activity(record)
            if changed or not self.party_seen:
                updates.append(self._party_update(record, authoritative=False))
        return updates

    def _readiness_identity_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        """Resolve the local token in one-real-player projection instances."""
        if str(record.get("method", "")) != "OnMsgDungeonReadinessCheck":
            return []
        if not args:
            return []
        try:
            state = int(args[0])
        except (TypeError, ValueError, OverflowError):
            return []
        if state == 0:
            try:
                expected = max(0, int(args[4] or 0)) if len(args) > 4 else 0
            except (TypeError, ValueError, OverflowError):
                expected = 0
            self.readiness_expected_members = expected
            self.readiness_tokens.clear()
            return []
        if state not in {3, 4} or len(args) < 3:
            return []
        token = self._team_token(args[2])
        if not token:
            return []
        self.readiness_tokens.add(token)
        if (
            self.readiness_expected_members != 1
            or len(self.readiness_tokens) != 1
            or self.self_token == token
        ):
            return []
        return self._confirm_self_token(token, record)

    def _party_update(self, record: dict, *, authoritative: bool) -> tuple[str, dict]:
        self.party_seen = True
        return (
            "party",
            {
                **self._base_update(record),
                "entity_ids": sorted(self.party_ids),
                "member_count": min(MAX_PARTY_MEMBERS, self.party_member_count),
                "authoritative": authoritative,
            },
        )

    def _refresh_party_member_count(self) -> None:
        if self.authoritative_party_tokens:
            self.party_member_count = len(self.authoritative_party_tokens)
        elif self.other_party_tokens:
            self.party_member_count = len(self.other_party_tokens) + 1
        elif self.party_ids or self.party_tokens:
            self.party_member_count = len(self.party_ids) + 1
        else:
            self.party_member_count = 1 if self.self_id is not None else 0
        self.party_member_count = min(
            MAX_PARTY_MEMBERS, self.party_member_count
        )

    def _clear_party_roster(self) -> None:
        self_max_hp = self.token_max_hp.get(self.self_token or "")
        self.party_ids.clear()
        self.party_tokens.clear()
        self.authoritative_party_tokens.clear()
        self.other_party_tokens.clear()
        self.token_max_hp.clear()
        if self.self_token and self.self_id is not None:
            self.token_actors[self.self_token] = self.self_id
            self.actor_tokens[self.self_id] = self.self_token
            if self_max_hp is not None:
                self.token_max_hp[self.self_token] = self_max_hp
        self.team_profile_markers.clear()
        self.party_member_count = 1 if self.self_id is not None else 0
        self.last_party_activity_100ns = 0

    def _record_party_activity(self, record: dict) -> None:
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        if timestamp:
            self.last_party_activity_100ns = max(
                self.last_party_activity_100ns, timestamp
            )

    def _infer_self_token(self, record: dict) -> list[tuple[str, dict]]:
        if self.self_id is None or self.self_token is not None:
            return []
        candidates = self.authoritative_party_tokens - self.other_party_tokens
        if len(candidates) != 1:
            return []
        return self._confirm_self_token(next(iter(candidates)), record)

    def _infer_self_token_from_profile(
        self, record: dict
    ) -> list[tuple[str, dict]]:
        if self.self_id is None or self.self_token is not None:
            return []
        candidates = set(self.authoritative_party_tokens)
        if not candidates:
            return []
        if self.self_profile_marker:
            matching_markers = [
                token
                for token in candidates
                if self.team_profile_markers.get(token) == self.self_profile_marker
            ]
            if len(matching_markers) == 1:
                return self._confirm_self_token(matching_markers[0], record)
        profession_id = int(self.actor_profession_hints.get(self.self_id, 0) or 0)
        if profession_id:
            matching_professions = [
                token
                for token in candidates
                if int(
                    self.team_profile_cache.get(token, {}).get(
                        "profession_id", 0
                    )
                    or 0
                )
                == profession_id
            ]
            if len(matching_professions) == 1:
                return self._confirm_self_token(
                    matching_professions[0], record
                )
        known_self_name = plausible_name(
            self.entity_profiles.get(self.self_id, {}).get("name")
        ) or plausible_name(self.runtime_entity_names.get(self.self_id))
        if not known_self_name:
            return []
        matching_names = [
            token
            for token in candidates
            if plausible_name(self.team_profile_cache.get(token, {}).get("name"))
            == known_self_name
        ]
        if len(matching_names) == 1:
            return self._confirm_self_token(matching_names[0], record)
        return []

    def _remembered_self_identity_updates(
        self,
        record: dict,
        *,
        profession_id: int = 0,
    ) -> list[tuple[str, dict]]:
        """Reuse a network-confirmed identity after restarting the overlay.

        The caller only supplies a remembered token for the same live game
        process. Requiring the first observed class to match keeps an old
        account or character from being attached to an unrelated actor.
        """
        token = self.remembered_self_token
        if (
            self.self_token
            or not token
            or self.self_id is None
            or not self.self_confirmed
        ):
            return []
        try:
            observed_profession = int(
                profession_id
                or self.actor_profession_hints.get(self.self_id, 0)
                or 0
            )
            cached_profession = int(
                self.team_profile_cache.get(token, {}).get("profession_id", 0)
                or 0
            )
        except (TypeError, ValueError, OverflowError):
            return []
        if (
            not observed_profession
            or not cached_profession
            or observed_profession != cached_profession
        ):
            return []
        return self._confirm_self_token(token, record)

    def _confirm_self_token(
        self, token: str, record: dict
    ) -> list[tuple[str, dict]]:
        if self.self_id is None:
            actor_id = self.token_actors.get(token)
            if actor_id is None:
                actor_id = self._bind_team_token(token, stable_team_actor_id(token))
            self.self_id = actor_id
        previous_self_token = self.self_token
        if previous_self_token and previous_self_token != token:
            if self.token_actors.get(previous_self_token) == self.self_id:
                self.token_actors.pop(previous_self_token, None)
            if self.actor_tokens.get(self.self_id) == previous_self_token:
                self.actor_tokens.pop(self.self_id, None)
            self.party_tokens.discard(previous_self_token)
            self.authoritative_party_tokens.discard(previous_self_token)
            self.other_party_tokens.discard(previous_self_token)
        old_actor = self.token_actors.get(token)
        party_changed = False
        updates: list[tuple[str, dict]] = []
        if old_actor is not None and old_actor != self.self_id:
            self.actor_tokens.pop(old_actor, None)
            if old_actor in self.party_ids:
                self.party_ids.remove(old_actor)
                party_changed = True
            updates.append(
                (
                    "actor_merge",
                    {
                        **self._base_update(record),
                        "from_actor_id": old_actor,
                        "to_actor_id": self.self_id,
                    },
                )
            )
            old_profile = self.entity_profiles.pop(old_actor, {})
            if old_profile:
                canonical = self.entity_profiles.setdefault(self.self_id, {})
                changed_profile = {
                    key: value
                    for key, value in old_profile.items()
                    if value not in (None, "") and canonical.get(key) != value
                }
                if changed_profile:
                    canonical.update(changed_profile)
                    updates.append(
                        (
                            "profile",
                            {
                                **self._base_update(record),
                                "entity_id": self.self_id,
                                **changed_profile,
                            },
                        )
                    )
        self.token_actors[token] = self.self_id
        self.actor_tokens[self.self_id] = token
        self.self_token = token
        self.remembered_self_token = token
        cached_profile = self.team_profile_cache.get(token, {})
        cached_update = self._profile_update(
            self.self_id,
            record,
            user_token=token,
            entity_type="Player",
            **cached_profile,
        )
        if cached_update:
            updates.append(cached_update)
        if self.self_id in self.party_ids:
            self.party_ids.remove(self.self_id)
            party_changed = True
        if token in self.party_tokens:
            self.party_tokens.remove(token)
            party_changed = True
        self._refresh_party_member_count()
        if not self.self_confirmed:
            self.self_confirmed = True
            updates.append(
                (
                    "identity",
                    {
                        **self._base_update(record),
                        "entity_id": self.self_id,
                        "tentative": False,
                    },
                )
            )
        if party_changed:
            updates.append(self._party_update(record, authoritative=True))
        return updates

    def _scene_updates(self, record: dict, args: list) -> list[tuple[str, dict]]:
        method = str(record.get("method", ""))
        updates: list[tuple[str, dict]] = []
        if method == "OnMsgSceneObjectCreated" and args:
            entity_id = parse_combat_entity_id(args[0])
            if entity_id:
                entity_type = args[1].strip()[:64] if len(args) > 1 and isinstance(args[1], str) else ""
                update = self._profile_update(entity_id, record, entity_type=entity_type)
                if update:
                    updates.append(update)
        elif method == "OnMsgRefreshSceneObjects" and len(args) > 1:
            for entity_key, descriptor in map_pairs(args[1]):
                entity_id = parse_combat_entity_id(entity_key)
                if not entity_id:
                    continue
                entity_type = descriptor.strip()[:64] if isinstance(descriptor, str) else ""
                update = self._profile_update(entity_id, record, entity_type=entity_type)
                if update:
                    updates.append(update)
        return updates

    def _known_target_profile_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        if not args:
            return []

        method = str(record.get("method", ""))
        target_id: int | None = None
        x: float
        y: float
        if method == "OnMsgCreateLUnitSpellField":
            fields = direct_numeric_map(args[0])
            target_id = parse_combat_entity_id(fields.get(17))
            position = direct_numeric_map(fields.get(9))
            try:
                x = float(position[0])
                y = float(position[1])
            except (KeyError, TypeError, ValueError, OverflowError):
                return []
        elif method == "OnMsgCreateBullet" and len(args) >= 13:
            target_id = parse_combat_entity_id(args[10])
            try:
                x = float(args[11])
                y = float(args[12])
            except (TypeError, ValueError, OverflowError):
                return []
        else:
            return []
        if not target_id:
            return []
        expected_x, expected_y = GUILD_HIT_DUMMY_POSITION
        if abs(x - expected_x) > 1.0 or abs(y - expected_y) > 1.0:
            return []
        template_profile = self.boss_template_catalog.get(
            str(GUILD_HIT_DUMMY_TEMPLATE_ID)
        )
        if self.boss_template_catalog_enabled and template_profile is None:
            return []
        template_profile = template_profile or {}
        self._activate_boss(target_id, record)
        update = self._profile_update(
            target_id,
            record,
            name=plausible_name(template_profile.get("name"))
            or GUILD_HIT_DUMMY_NAME,
            level=template_profile.get("level", GUILD_HIT_DUMMY_LEVEL),
            entity_type="Monster",
            template_id=GUILD_HIT_DUMMY_TEMPLATE_ID,
            boss_type=GUILD_HIT_DUMMY_BOSS_TYPE,
            boss_rank=3,
        )
        return [update] if update else []

    def _hud_boss_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        if str(record.get("method", "")) != "OnMsgSetHUDShow":
            return []
        template_path = next(
            (
                value.strip()
                for value in walk_values(args)
                if isinstance(value, str) and HUD_BOSS_TEMPLATE_RE.search(value.strip())
            ),
            "",
        )
        if not template_path:
            return []
        values = {
            "entity_type": "Monster",
            "boss_type": HUD_BOSS_TYPE,
            "boss_rank": 3,
            "template_path": template_path[:160],
        }
        pointer = int(record.get("script_entity", 0) or 0)
        entity_id = self.pointer_entities.get(pointer)
        if entity_id:
            self._activate_boss(entity_id, record)
        return self._pointer_update(pointer, values, record)

    def apply_runtime_boss_name(
        self, record: dict
    ) -> tuple[str, dict] | None:
        try:
            entity_id = int(record.get("entity_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        name = plausible_name(record.get("name"))
        if entity_id and name:
            self.runtime_entity_names[entity_id] = name
        known_boss_name = self._name_matches_boss_allowlist(name)
        late_start_evidence = bool(
            entity_id
            and (
                entity_id == self.recent_target_id
                or entity_id in self.pending_entity_hits
            )
        )
        if (
            entity_id
            and entity_id != self.active_boss_entity_id
            and known_boss_name
            and late_start_evidence
            and entity_id != self.self_id
            and entity_id not in self.party_ids
            and entity_id not in self.actor_tokens
        ):
            self._activate_boss(entity_id, record, damage_evidence=True)
        if (
            not entity_id
            or entity_id != self.active_boss_entity_id
            or not name
            or name.casefold() in {"boss", "首领"}
        ):
            return None
        return self._profile_update(
            entity_id,
            record,
            name=name,
            entity_type="Boss",
            boss_type=HUD_BOSS_TYPE,
            boss_rank=3,
        )

    def process_native_boss_type(
        self, record: dict
    ) -> list[tuple[str, dict]]:
        """Promote targets from the exported Boss template allowlist."""
        entity_id = parse_combat_entity_id(record.get("entity_id"))
        try:
            boss_type = int(record.get("boss_type", -1))
        except (TypeError, ValueError, OverflowError):
            return []
        try:
            template_id = int(record.get("template_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            template_id = 0
        if entity_id and template_id:
            self.entity_template_ids[entity_id] = template_id
        template_profile = self.boss_template_catalog.get(str(template_id))
        auxiliary = ENCOUNTER_AUXILIARY_TEMPLATES.get(template_id)
        if entity_id and auxiliary is not None:
            parent_templates = tuple(
                int(value)
                for value in auxiliary.get("parent_template_ids", ())
                if int(value)
            )
            self.encounter_auxiliary_entities[entity_id] = parent_templates
            values: dict[str, object] = {
                "name": str(auxiliary.get("name", "")).strip() or "星光守卫",
                "entity_type": "Monster",
                "template_id": template_id,
                "boss_type": boss_type,
                "encounter_auxiliary": True,
                "encounter_parent_template_ids": list(parent_templates),
            }
            if template_profile and template_profile.get("level") not in (None, ""):
                values["level"] = template_profile["level"]
            updates: list[tuple[str, dict]] = []
            profile = self._profile_update(entity_id, record, **values)
            if profile:
                updates.append(profile)
            updates.extend(self._bind_candidate_pointers(entity_id, record))
            monster_values: dict[str, object] = {}
            if entity_id in self.entity_current_hp:
                monster_values["current_hp"] = self.entity_current_hp[entity_id]
            if entity_id in self.entity_max_hp:
                monster_values["max_hp"] = self.entity_max_hp[entity_id]
            if monster_values:
                monster_update = self._base_update(record)
                monster_update["entity_id"] = entity_id
                monster_update.update(monster_values)
                updates.append(("monster", monster_update))
            return updates
        if self.boss_template_catalog_enabled:
            confirmed = template_profile is not None
        else:
            # Standalone parser users that omit a catalog retain the native
            # enum behavior. The production client always supplies a catalog,
            # including an empty one, and therefore fails closed by template ID.
            confirmed = boss_type == HUD_BOSS_TYPE
        if not entity_id or not confirmed:
            return []
        if (
            entity_id == self.self_id
            or entity_id in self.party_ids
            or entity_id in self.actor_tokens
        ):
            return []
        values: dict[str, object] = {
            "entity_type": "Boss",
            "boss_type": HUD_BOSS_TYPE,
            "boss_rank": 3,
            "boss_source": (
                "monster_template_catalog"
                if template_profile is not None
                else str(record.get("boss_source", "runtime_common_component"))[:64]
            ),
        }
        if template_id:
            values["template_id"] = template_id
        runtime_level = (
            template_profile.get("level")
            if template_profile is not None
            else record.get("level")
        )
        if runtime_level not in (None, ""):
            values["level"] = runtime_level
        metadata_name = plausible_name(
            template_profile.get("name")
            if template_profile is not None
            else record.get("name")
        )
        runtime_name = self.runtime_entity_names.get(entity_id, "")
        if metadata_name or runtime_name:
            values["name"] = metadata_name or runtime_name
        else:
            values["name"] = "Boss"
        updates: list[tuple[str, dict]] = []
        profile = self._profile_update(entity_id, record, **values)
        if profile:
            updates.append(profile)
        # Store the catalog name before activation so explicit multi-entity
        # phases (for example Ancestor Armor -> Baldwin) can be recognized.
        self._activate_boss(entity_id, record)
        updates.extend(self._bind_candidate_pointers(entity_id, record))
        monster_values: dict[str, object] = {}
        if entity_id in self.entity_current_hp:
            monster_values["current_hp"] = self.entity_current_hp[entity_id]
        if entity_id in self.entity_max_hp:
            monster_values["max_hp"] = self.entity_max_hp[entity_id]
        if monster_values:
            monster_update = self._base_update(record)
            monster_update["entity_id"] = entity_id
            monster_update.update(monster_values)
            updates.append(("monster", monster_update))
        return updates

    def _stage_combat_statistics_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        """Bind stage damage rows directly to their packet-provided actors."""
        if str(record.get("method", "")) != STAGE_COMBAT_STATISTICS_METHOD or not args:
            return []
        stage = direct_numeric_map(args[0])
        raw_entries = map_pairs(stage.get(5))
        members: list[tuple[str, int, dict[int, object]]] = []
        used_tokens: set[str] = set()
        used_actors: set[int] = set()
        for raw_token, raw_profile in raw_entries:
            fields = direct_numeric_map(raw_profile)
            token = self._team_token(raw_token) or self._team_token(fields.get(0))
            try:
                actor_id = int(fields.get(1, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                actor_id = 0
            if (
                not token
                or token in used_tokens
                or actor_id <= 0
                or actor_id > ENTITY_ID_MAX
                or actor_id in used_actors
            ):
                continue
            used_tokens.add(token)
            used_actors.add(actor_id)
            members.append((token, actor_id, fields))
        if not members:
            return []
        # Stage statistics can retain players from an earlier pull. A real
        # party never exceeds 12, so an oversized table is stale and must not
        # replace the current roster or damage rows.
        if len(members) > MAX_PARTY_MEMBERS:
            return []

        desired = {token: actor_id for token, actor_id, _fields in members}
        old_actors = {token: self.token_actors.get(token) for token in desired}
        for token, old_actor in old_actors.items():
            if old_actor is not None and self.actor_tokens.get(old_actor) == token:
                self.actor_tokens.pop(old_actor, None)
        for actor_id in desired.values():
            existing_token = self.actor_tokens.get(actor_id)
            if existing_token in desired:
                self.actor_tokens.pop(actor_id, None)
        for token, actor_id in desired.items():
            self.token_actors[token] = actor_id
            self.actor_tokens[actor_id] = token

        updates: list[tuple[str, dict]] = []
        for token, actor_id, fields in members:
            old_actor = old_actors.get(token)
            if old_actor is not None and old_actor < 0 and old_actor != actor_id:
                updates.append(
                    (
                        "actor_merge",
                        {
                            **self._base_update(record),
                            "from_actor_id": old_actor,
                            "to_actor_id": actor_id,
                        },
                    )
                )
            profile = self._profile_update(
                actor_id,
                record,
                name=plausible_name(fields.get(5)),
                profession_id=fields.get(4),
                level=fields.get(2),
                user_token=token,
                entity_type="Player",
            )
            if profile:
                updates.append(profile)

        self.authoritative_party_tokens = set(desired)
        matching_self = [
            token
            for token, actor_id in desired.items()
            if self.self_id is not None and actor_id == self.self_id
        ]
        if len(matching_self) == 1:
            self.self_token = matching_self[0]
            self.self_confirmed = True
        self.party_tokens = set(desired)
        self.party_ids = set(desired.values())
        if self.self_token:
            self.party_tokens.discard(self.self_token)
        if self.self_id is not None:
            self.party_ids.discard(self.self_id)
        self.other_party_tokens = set(self.party_tokens)
        self.party_member_count = min(MAX_PARTY_MEMBERS, len(desired))
        self._record_party_activity(record)
        updates.append(self._party_update(record, authoritative=True))

        summary_rows: list[dict[str, object]] = []
        for token, actor_id, fields in members:
            try:
                damage = max(0, int(fields.get(6, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                damage = 0
            skill_damage = direct_numeric_map(fields.get(34))
            skill_hits = direct_numeric_map(fields.get(33))
            skills: list[dict[str, int]] = []
            for raw_skill_id, raw_damage in skill_damage.items():
                skill_id = normalize_network_skill_id(raw_skill_id)
                try:
                    parsed_damage = max(0, int(raw_damage or 0))
                    hits = max(0, int(skill_hits.get(raw_skill_id, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if skill_id and parsed_damage > 0:
                    skills.append(
                        {
                            "skill_id": skill_id,
                            "damage": parsed_damage,
                            "hits": hits,
                        }
                    )
            summary_rows.append(
                {
                    "actor_id": actor_id,
                    "user_token": token,
                    "damage": damage,
                    "skills": skills,
                }
            )
        summary_key = "|".join(str(stage.get(key, "")) for key in (0, 1, 2))
        updates.append(
            (
                "stage_summary",
                {
                    **self._base_update(record),
                    "summary_id": summary_key
                    or str(record.get("filetime_100ns", 0) or 0),
                    "member_count": len(desired),
                    "actors": summary_rows,
                },
            )
        )
        return updates

    def _generic_profile_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        method = str(record.get("method", ""))
        if not _PROFILE_METHOD_RE.search(method):
            return []
        if "team" in method.casefold() or "party" in method.casefold():
            return []
        updates: list[tuple[str, dict]] = []
        seen: set[tuple[int, str]] = set()
        for node in walk_values(args):
            pairs = map_pairs(node)
            if not pairs:
                continue
            ids = [parse_entity_id(value) for pair in pairs for value in pair]
            ids = [value for value in ids if value]
            names = [plausible_name(value) for pair in pairs for value in pair]
            names = [value for value in names if value]
            if len(set(ids)) != 1 or not names:
                continue
            entity_id = ids[0]
            name = min(names, key=len)
            marker = (entity_id, name)
            if marker in seen:
                continue
            seen.add(marker)
            level_candidates: set[int] = set()
            for _key, item in pairs:
                if isinstance(item, bool):
                    continue
                try:
                    candidate = int(item)
                except (TypeError, ValueError, OverflowError):
                    continue
                if 1 <= candidate <= 200:
                    level_candidates.add(candidate)
            level = next(iter(level_candidates)) if len(level_candidates) == 1 else None
            update = self._profile_update(entity_id, record, name=name, level=level)
            if update:
                updates.append(update)
            role_method = (
                "role" in method.casefold()
                and "team" not in method.casefold()
                and "friend" not in method.casefold()
            )
            if role_method and self.self_id != entity_id:
                self.self_id = entity_id
                self.self_confirmed = True
                updates.append(
                    (
                        "identity",
                        {
                            **self._base_update(record),
                            "entity_id": entity_id,
                            "tentative": False,
                        },
                    )
                )
                updates.extend(self._infer_self_token(record))
        return updates

    def _party_updates(self, record: dict, args: list) -> list[tuple[str, dict]]:
        method = str(record.get("method", ""))
        folded = method.casefold()
        updates: list[tuple[str, dict]] = []

        if method == TEAM_SELF_PROPS_METHOD and args:
            fields = direct_numeric_map(args[0])
            self._record_party_activity(record)
            candidate = ""
            try:
                marker = int(fields.get(11, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                marker = 0
            if marker:
                self.self_profile_marker = marker
                matches = [
                    token
                    for token, value in self.team_profile_markers.items()
                    if value == marker
                ]
                if len(matches) == 1:
                    candidate = matches[0]
            if not candidate:
                try:
                    max_hp = float(fields.get(6, 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    max_hp = 0.0
                matches = [
                    token
                    for token, value in self.token_max_hp.items()
                    if max_hp > 0 and abs(value - max_hp) <= 0.5
                ]
                if len(matches) == 1:
                    candidate = matches[0]
            if candidate:
                updates.extend(self._confirm_self_token(candidate, record))
            if self.self_token:
                try:
                    max_hp = float(fields.get(6, 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    max_hp = 0.0
                if max_hp > 0:
                    self.token_max_hp[self.self_token] = max_hp
            return updates

        if method in TEAM_OTHER_MEMBER_TOKEN_METHODS and args:
            token = self._team_token(args[0])
            if token and token != self.self_token:
                actor_id = self.token_actors.get(token)
                if actor_id is None:
                    actor_id = self._bind_team_token(token, stable_team_actor_id(token))
                changed = (
                    token not in self.other_party_tokens
                    or token not in self.party_tokens
                    or actor_id not in self.party_ids
                )
                self.other_party_tokens.add(token)
                self.party_tokens.add(token)
                self.party_ids.add(actor_id)
                self._refresh_party_member_count()
                self._record_party_activity(record)
                if changed or not self.party_seen:
                    updates.append(self._party_update(record, authoritative=False))
                updates.extend(self._infer_self_token(record))
                if len(args) > 2:
                    try:
                        max_hp = float(args[2] or 0)
                    except (TypeError, ValueError, OverflowError):
                        max_hp = 0.0
                    if max_hp > 0:
                        self.token_max_hp[token] = max_hp
                        updates.extend(self._team_hp_bindings(record))
            return updates

        if method in {TEAM_MEMBER_JOIN_METHOD, TEAM_MEMBERS_JOIN_METHOD}:
            if method == TEAM_MEMBER_JOIN_METHOD and len(args) >= 3:
                raw_members = [args[2]]
            elif method == TEAM_MEMBERS_JOIN_METHOD and len(args) >= 2:
                raw_members = args[1] if isinstance(args[1], list) else []
            else:
                raw_members = []
            changed = False
            joined = False
            for raw_member in raw_members:
                fields = direct_numeric_map(raw_member)
                token = self._team_token(fields.get(2))
                if not token:
                    continue
                joined = True
                actor_id = self.token_actors.get(token)
                if actor_id is None:
                    actor_id = self._bind_team_token(
                        token, stable_team_actor_id(token, fields.get(6))
                    )
                profile = self._profile_update(
                    actor_id,
                    record,
                    name=plausible_name(fields.get(5)),
                    role_number=fields.get(6),
                    profession_id=fields.get(8),
                    level=fields.get(9),
                    user_token=token,
                    entity_type="Player",
                )
                if profile:
                    updates.append(profile)
                try:
                    max_hp = float(fields.get(13, fields.get(12, 0)) or 0)
                except (TypeError, ValueError, OverflowError):
                    max_hp = 0.0
                if max_hp > 0:
                    self.token_max_hp[token] = max_hp
                try:
                    marker = int(fields.get(27, 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    marker = 0
                if marker:
                    self.team_profile_markers[token] = marker
                if token == self.self_token:
                    continue
                changed |= (
                    actor_id not in self.party_ids
                    or token not in self.party_tokens
                )
                self.party_ids.add(actor_id)
                self.party_tokens.add(token)
                self.other_party_tokens.add(token)
                if self.authoritative_party_tokens:
                    self.authoritative_party_tokens.add(token)
            if joined:
                self._refresh_party_member_count()
                self._record_party_activity(record)
                if changed or not self.party_seen:
                    updates.append(
                        self._party_update(record, authoritative=False)
                    )
            return updates

        if any(marker in folded for marker in TEAM_MEMBER_LEAVE_MARKERS):
            token = next(
                (
                    parsed
                    for value in walk_values(args)
                    if (parsed := self._team_token(value)) in self.token_actors
                ),
                "",
            )
            actor_id = self.token_actors.get(token) if token else None
            changed = False
            if token in self.party_tokens:
                self.party_tokens.remove(token)
                changed = True
            self.other_party_tokens.discard(token)
            self.authoritative_party_tokens.discard(token)
            self.token_max_hp.pop(token, None)
            self.team_profile_markers.pop(token, None)
            if actor_id in self.party_ids:
                self.party_ids.remove(actor_id)
                changed = True
            if changed:
                self._refresh_party_member_count()
                self._record_party_activity(record)
                updates.append(self._party_update(record, authoritative=False))
            return updates

        if (
            ("team" in folded or "party" in folded)
            and any(marker in folded for marker in TEAM_CLEAR_MARKERS)
            and "other" not in folded
        ):
            changed = bool(self.party_ids or self.party_tokens) or not self.party_seen
            self._clear_party_roster()
            if changed:
                updates.append(self._party_update(record, authoritative=True))
        return updates

    def _life_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        method = str(record.get("method", ""))
        token = ""
        actor_id: int | None = None
        fields: dict[int, object] = {}
        explicit_dead: bool | None = None

        if method == TEAM_SELF_PROPS_METHOD and args:
            actor_id = self.self_id
            token = self.self_token or ""
            fields = direct_numeric_map(args[0])
        elif method == "OnUpdateTeamGroupMemberProps" and len(args) >= 2:
            token = self._team_token(args[0])
            if not token or token == self.self_token:
                return []
            actor_id = self.token_actors.get(token)
            fields = direct_numeric_map(args[1])
        elif method in {"OnMsgEntityDead", "OnMsgEntityRelive"}:
            token = next(
                (
                    candidate
                    for value in walk_values(args)
                    if (candidate := self._team_token(value))
                    and (
                        candidate == self.self_token
                        or candidate in self.party_tokens
                        or candidate in self.authoritative_party_tokens
                    )
                ),
                "",
            )
            if not token:
                return []
            actor_id = (
                self.self_id
                if token == self.self_token
                else self.token_actors.get(token)
            )
            explicit_dead = method == "OnMsgEntityDead"
        else:
            return []

        if not actor_id:
            return []
        if explicit_dead is None:
            if 4 in fields:
                explicit_dead = bool(fields[4])
            elif 5 in fields:
                try:
                    explicit_dead = float(fields[5]) <= 0
                except (TypeError, ValueError, OverflowError):
                    return []
            else:
                return []

        update = {
            **self._base_update(record),
            "actor_id": actor_id,
            "dead": bool(explicit_dead),
        }
        if token:
            update["user_token"] = token
        for source_key, target_key in ((5, "current_hp"), (6, "max_hp")):
            if source_key not in fields:
                continue
            try:
                update[target_key] = max(0.0, float(fields[source_key]))
            except (TypeError, ValueError, OverflowError):
                pass
        return [("life", update)]

    def _damage_updates(
        self, record: dict, args: list, *, infer_identity: bool = True
    ) -> list[tuple[str, dict]]:
        if len(args) < 9:
            return []
        try:
            attacker_id = int(args[0])
            target_id = int(args[1])
            raw_skill_id = int(args[2])
            raw_damage = int(args[5])
            damage = int(args[7])
        except (TypeError, ValueError, OverflowError):
            return []
        if not attacker_id or not target_id or damage <= 0:
            return []
        pointer = int(record.get("script_entity", 0) or 0)
        if pointer:
            self.root_pointers.add(pointer)
            self.pointer_candidates.pop(pointer, None)
            self.pointer_entities.pop(pointer, None)
            self.pointer_state.pop(pointer, None)
        self.recent_target_id = target_id
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        self.recent_target_time_100ns = timestamp
        skill_id = normalize_network_skill_id(raw_skill_id)
        updates: list[tuple[str, dict]] = []
        if target_id in self.confirmed_boss_entities:
            self._activate_boss(target_id, record, damage_evidence=True)
        else:
            runtime_name = self.runtime_entity_names.get(target_id, "")
            if (
                self._name_matches_boss_allowlist(runtime_name)
                and target_id != self.self_id
                and target_id not in self.party_ids
                and target_id not in self.actor_tokens
                and self._activate_boss(
                    target_id, record, damage_evidence=True
                )
            ):
                profile = self._profile_update(
                    target_id,
                    record,
                    name=runtime_name,
                    entity_type="Boss",
                    boss_type=HUD_BOSS_TYPE,
                    boss_rank=3,
                    boss_source="runtime_name_cache",
                )
                if profile:
                    updates.append(profile)
        player_skill = is_player_skill(skill_id)
        if player_skill:
            self.player_attackers.add(attacker_id)
        if infer_identity and player_skill:
            if self.self_id is None and len(self.player_attackers) == 1:
                self.self_id = attacker_id
                self.self_confirmed = False
                updates.append(
                    (
                        "identity",
                        {
                            **self._base_update(record),
                            "entity_id": attacker_id,
                            "tentative": True,
                        },
                    )
                )
                updates.extend(self._infer_self_token(record))
        updates.extend(self._record_combat_source(record, attacker_id, skill_id))
        if (
            str(record.get("method", ""))
            in {"OnMsgDamageSyncV2", "KAPI_HandleDamageSyncV2"}
            and (
                target_id == self.active_boss_entity_id
                or self._is_active_encounter_auxiliary(target_id)
            )
        ):
            pending = self.pending_exact_damage.setdefault(target_id, [])
            pending.append((timestamp, attacker_id, damage))
            if len(pending) > 4096:
                del pending[:-2048]
        known_player_attacker = self._is_known_player_actor(attacker_id, timestamp)
        confirmed_non_player = bool(
            attacker_id in self.confirmed_boss_entities
            or attacker_id == self.active_boss_entity_id
            or attacker_id in self.encounter_auxiliary_entities
        )
        event = {
            **self._base_update(record),
            "function": "OnMsgDamageSyncV2/network",
            "attacker_id": attacker_id,
            "target_id": target_id,
            "player_attacker": (
                True
                if known_player_attacker
                else False if confirmed_non_player else None
            ),
            "arg4_u64": raw_skill_id // 10_000 if raw_skill_id >= 1_000_000_000_000 else raw_skill_id,
            "skill_id": skill_id,
            "arg5_i32": int(args[3]),
            "arg6_i32": int(args[4]),
            "arg7_i32": raw_damage,
            "arg8_i32": int(args[6]),
            "arg9_i32": damage,
            "arg10_bool": bool(args[8]),
            "raw_damage": raw_damage,
            "damage": damage,
        }
        updates.append(("event", event))
        return updates

    def _team_statistics_updates(
        self, record: dict, args: list
    ) -> list[tuple[str, dict]]:
        method = str(record.get("method", ""))
        if method not in TEAM_STATISTICS_METHODS:
            return []
        if not args:
            return []
        entries = map_pairs(args[0])
        if not entries:
            return []
        parsed_entries: list[tuple[str, dict[int, object]]] = []
        for raw_token, raw_profile in entries:
            token = self._team_token(raw_token)
            if token:
                parsed_entries.append((token, direct_numeric_map(raw_profile)))
        if not parsed_entries:
            return []
        if len(parsed_entries) > MAX_PARTY_MEMBERS:
            return []
        if self.party_ids or len(parsed_entries) > 1:
            self._record_party_activity(record)

        updates: list[tuple[str, dict]] = []
        entry_tokens = {token for token, _fields in parsed_entries}
        authoritative = method == "RetCommonCombatStatisticsByTeam"
        if not self.party_seen and len(parsed_entries) >= 1:
            authoritative = True
        if authoritative:
            self.authoritative_party_tokens = set(entry_tokens)
            self.party_member_count = len(entry_tokens)

        unknown_tokens = [
            token for token, _fields in parsed_entries if token not in self.token_actors
        ]
        if self.self_id is not None and self.self_token is None:
            self_candidate = ""
            if len(parsed_entries) == 1:
                self_candidate = parsed_entries[0][0]
            elif len(unknown_tokens) == 1:
                self_candidate = unknown_tokens[0]
            else:
                known_self_name = plausible_name(
                    self.entity_profiles.get(self.self_id, {}).get("name")
                )
                matching = [
                    token
                    for token, fields in parsed_entries
                    if known_self_name and plausible_name(fields.get(4)) == known_self_name
                ]
                if len(matching) == 1:
                    self_candidate = matching[0]
            if self_candidate:
                updates.extend(self._confirm_self_token(self_candidate, record))
            else:
                updates.extend(self._infer_self_token(record))

        parsed_token_set: set[str] = set()
        for token, fields in parsed_entries:
            actor_id = self.token_actors.get(token)
            if actor_id is None:
                actor_id = self._bind_team_token(token, stable_team_actor_id(token))
            parsed_token_set.add(token)
            profile = self._profile_update(
                actor_id,
                record,
                name=plausible_name(fields.get(4)),
                level=fields.get(1),
                profession_id=fields.get(3),
                user_token=token,
                entity_type="Player",
            )
            if profile:
                updates.append(profile)
            # Dirty team snapshots omit field 5 when this member's cumulative
            # damage did not change.  Treating an omitted field as zero causes
            # a false snapshot reset for every unchanged teammate.
            if 5 not in fields:
                continue
            try:
                absolute_damage = max(0, int(fields[5] or 0))
            except (TypeError, ValueError, OverflowError):
                absolute_damage = 0
            try:
                server_time = max(0, int(fields.get(10, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                server_time = 0
            updates.append(
                (
                    "team_stat",
                    {
                        **self._base_update(record),
                        "actor_id": actor_id,
                        "user_token": token,
                        "absolute_damage": absolute_damage,
                        "server_time": server_time,
                    },
                )
            )

        updates.extend(self._infer_self_token_from_profile(record))
        if authoritative:
            next_party_ids = {
                self.token_actors[token]
                for token in parsed_token_set
                if token in self.token_actors
            }
            next_party_tokens = set(parsed_token_set)
            if self.self_id is not None:
                next_party_ids.discard(self.self_id)
            if self.self_token:
                next_party_tokens.discard(self.self_token)
            if (
                not self.party_seen
                or next_party_ids != self.party_ids
                or next_party_tokens != self.party_tokens
            ):
                self.party_ids = next_party_ids
                self.party_tokens = next_party_tokens
                self._refresh_party_member_count()
                if self.party_ids:
                    self._record_party_activity(record)
                updates.append(self._party_update(record, authoritative=True))
        return updates

    def process_native_damage(self, record: dict) -> list[tuple[str, dict]]:
        try:
            args = [
                int(record["attacker_id"]),
                int(record["target_id"]),
                int(record["arg4_u64"]),
                int(record.get("arg5_i32", 0)),
                int(record.get("arg6_i32", 0)),
                int(record.get("raw_damage", record.get("arg7_i32", 0))),
                int(record.get("arg8_i32", 0)),
                int(record.get("damage", record.get("arg9_i32", 0))),
                bool(record.get("arg10_bool", False)),
            ]
        except (KeyError, TypeError, ValueError, OverflowError):
            return []
        native_record = dict(record)
        native_record["method"] = "KAPI_HandleDamageSyncV2"
        native_record["script_entity"] = 0
        updates: list[tuple[str, dict]] = []
        local_player_id = parse_combat_entity_id(record.get("local_player_id"))
        if local_player_id:
            updates.extend(self._confirm_local_actor(local_player_id, native_record))
        updates.extend(
            self._damage_updates(native_record, args, infer_identity=False)
        )
        return updates

    def process(
        self, record: dict, *, include_damage: bool = True
    ) -> list[tuple[str, dict]]:
        method = str(record.get("method", ""))
        args = self._args(record)
        pointer = int(record.get("script_entity", 0) or 0)
        updates: list[tuple[str, dict]] = []

        if method == "RetCastSkillSuccessNew":
            updates.extend(self._local_cast_updates(record, args))

        if method == "OnMsgRefreshSceneObjects" and args:
            try:
                scene_id = int(args[0] or 0)
            except (TypeError, ValueError, OverflowError):
                scene_id = 0
            if scene_id > 0:
                previous_scene_id = self.scene_id
                visible_entity_ids: list[int] = []
                if len(args) > 1:
                    for raw_entity_id, _descriptor in map_pairs(args[1]):
                        entity_id = parse_combat_entity_id(raw_entity_id)
                        if entity_id:
                            visible_entity_ids.append(entity_id)
                # A numeric scene argument denotes a full object refresh. The
                # game uses the same scene ID when leaving some instances, so
                # pointer and Boss state must be invalidated even when the ID is
                # unchanged. Partial in-scene refreshes carry None here.
                self._reset_scene_combat_bindings()
                self.scene_id = scene_id
                updates.append(
                    (
                        "scene",
                        {
                            **self._base_update(record),
                            "scene_id": scene_id,
                            "previous_scene_id": previous_scene_id,
                            "force_reset": previous_scene_id is not None,
                            "entity_ids": visible_entity_ids,
                        },
                    )
                )

        if method == "OnMsgSyncFightMode" and args and pointer:
            try:
                fight_mode = int(args[0])
            except (TypeError, ValueError, OverflowError):
                fight_mode = -1
            if fight_mode == 2:
                self.combat_mode_pointers.add(pointer)
            elif fight_mode >= 0:
                self.combat_mode_pointers.discard(pointer)

        if method == "OnMsgCreateBullet" and len(args) >= 3:
            actor_id = parse_combat_entity_id(args[2])
            if actor_id:
                updates.extend(
                    self._record_combat_source(
                        record, actor_id, args[0], bind_pointer=True
                    )
                )
                target_id = (
                    parse_combat_entity_id(args[10]) if len(args) > 10 else None
                )
                self._record_entity_hit(target_id, actor_id, args[0], record)
        elif method in {
            "OnMsgCreateLUnitSpellField",
            "OnMsgCreateLUnitSpellAgent",
            "OnMsgCreateLUnitAura",
            "OnMsgCreateLUnitTrap",
        } and args:
            fields = direct_numeric_map(args[0])
            actor_id = parse_combat_entity_id(fields.get(0))
            if actor_id:
                updates.extend(
                    self._record_combat_source(
                        record, actor_id, fields.get(2, 0), bind_pointer=True
                    )
                )
                target_id = parse_combat_entity_id(
                    fields.get(17)
                ) or parse_combat_entity_id(fields.get(6))
                self._record_entity_hit(
                    target_id, actor_id, fields.get(2, 0), record
                )
        elif method == "OnMsgHealSyncV2" and len(args) >= 3:
            actor_id = parse_combat_entity_id(args[0])
            if actor_id:
                updates.extend(
                    self._record_combat_source(record, actor_id, args[2])
                )
        elif method == "OnMsgCastSkillNew" and args:
            actor_id = self.pointer_entities.get(pointer)
            if actor_id:
                updates.extend(
                    self._record_combat_source(record, actor_id, args[0])
                )
                target_id = (
                    parse_combat_entity_id(args[1]) if len(args) > 1 else None
                )
                self._record_entity_hit(target_id, actor_id, args[0], record)

        if method == "OnMsgDamageSyncV2":
            damage_updates = self._damage_updates(record, args)
            updates.extend(
                damage_updates
                if include_damage
                else [item for item in damage_updates if item[0] != "event"]
            )
        elif method == "OnMsgBeatenSyncV2" and len(args) >= 2:
            target_id = parse_combat_entity_id(args[1])
            if target_id:
                timestamp = int(record.get("filetime_100ns", 0) or 0)
                self.recent_target_id = target_id
                self.recent_target_time_100ns = timestamp
                if pointer and pointer not in self.root_pointers:
                    self.pointer_candidates[pointer] = (target_id, timestamp)
        elif method == "OnMsgEndureExitHit" and args:
            actor_id = parse_combat_entity_id(args[0])
            target_id = self.pointer_entities.get(pointer)
            if actor_id:
                self._record_target_hit(pointer, actor_id, record)
                if (
                    self.active_boss_entity_id is not None
                    and target_id == self.active_boss_entity_id
                    and (
                        self.active_boss_pointer is None
                        or self.active_boss_pointer == pointer
                    )
                ):
                    self.active_boss_pointer = pointer
                    self.active_boss_pointer_time_100ns = int(
                        record.get("filetime_100ns", 0) or 0
                    )
                    updates.extend(self._record_combat_source(record, actor_id))
        elif method == "OnMsgAddBuffNew" and len(args) >= 3:
            target_id = parse_combat_entity_id(args[1])
            source_id = parse_combat_entity_id(args[2])
            if (
                pointer
                and pointer not in self.root_pointers
                and target_id
                and target_id == source_id
            ):
                # Spawn initialization sends a self-owned buff on the unit's
                # own ScriptEntity before native template metadata arrives.
                # This is stronger ownership evidence than shared hit timing.
                self.pointer_candidates[pointer] = (
                    target_id,
                    int(record.get("filetime_100ns", 0) or 0),
                )
        elif method == "OnMsgActorBuffStateSync" and len(args) >= 3:
            entity_id = parse_combat_entity_id(args[2])
            timestamp = int(record.get("filetime_100ns", 0) or 0)
            if (
                pointer not in self.pointer_entities
                and self.active_boss_entity_id is not None
                and self._boss_pointer_has_evidence(pointer, timestamp)
            ):
                # The third argument is a buff source, not the ScriptEntity
                # owner. On Boss actors this value is often a player, so prefer
                # the pointer's fight mode, hit and HP evidence.
                updates.extend(
                    self._bind_pointer(
                        pointer, self.active_boss_entity_id, record
                    )
                )
            elif (
                entity_id
                and entity_id not in self.confirmed_boss_entities
                and self._is_known_player_actor(
                    entity_id, timestamp
                )
            ):
                updates.extend(self._bind_pointer(pointer, entity_id, record))
        elif method == "OnMsgSyncCurrentHp" and args:
            try:
                current_hp = max(0.0, float(args[0]))
            except (TypeError, ValueError, OverflowError):
                current_hp = -1.0
            if self._valid_hp_value(current_hp):
                if pointer in self.root_pointers:
                    self.pointer_candidates.pop(pointer, None)
                    self.pointer_entities.pop(pointer, None)
                    self.pointer_state.pop(pointer, None)
                else:
                    timestamp = int(record.get("filetime_100ns", 0) or 0)
                    candidate = self.pointer_candidates.pop(pointer, None)
                    if candidate is not None:
                        target_id, candidate_time = candidate
                        elapsed = timestamp - candidate_time
                        if 0 <= elapsed <= POINTER_BIND_WINDOW_100NS:
                            updates.extend(
                                self._bind_pointer(pointer, target_id, record)
                            )
                    if (
                        pointer not in self.pointer_entities
                        and self.active_boss_entity_id is not None
                        and not self._recent_skill_matches_hp(current_hp, timestamp)
                        and self._boss_pointer_has_evidence(pointer, timestamp)
                    ):
                        updates.extend(
                            self._bind_pointer(
                                pointer, self.active_boss_entity_id, record
                            )
                        )
                    entity_id = self.pointer_entities.get(pointer)
                    if entity_id:
                        accept_update = True
                        encounter_auxiliary = self._is_active_encounter_auxiliary(
                            entity_id
                        )
                        if entity_id == self.active_boss_entity_id:
                            if (
                                self.active_boss_pointer is None
                                and self._boss_pointer_has_evidence(pointer, timestamp)
                            ):
                                self.active_boss_pointer = pointer
                            if self.active_boss_pointer != pointer:
                                accept_update = False
                            elif not self._valid_boss_hp(
                                entity_id, current_hp, timestamp
                            ):
                                accept_update = False
                            if accept_update:
                                previous_hp = self.entity_current_hp.get(entity_id)
                                inferred = self._inferred_team_damage_updates(
                                    pointer,
                                    entity_id,
                                    previous_hp,
                                    current_hp,
                                    record,
                                )
                                updates.extend(inferred)
                                self.active_boss_time_100ns = timestamp
                                self.active_boss_pointer_time_100ns = timestamp
                        elif encounter_auxiliary:
                            if not self._valid_encounter_auxiliary_hp(
                                entity_id, current_hp, timestamp
                            ):
                                accept_update = False
                            if accept_update:
                                updates.extend(
                                    self._inferred_team_damage_updates(
                                        pointer,
                                        entity_id,
                                        self.entity_current_hp.get(entity_id),
                                        current_hp,
                                        record,
                                    )
                                )
                        if accept_update:
                            updates.extend(
                                self._pointer_update(
                                    pointer, {"current_hp": current_hp}, record
                                )
                            )
                            self.entity_current_hp[entity_id] = current_hp
                            self.entity_current_hp_time[entity_id] = timestamp
                            updates.extend(self._team_hp_bindings(record))
                    else:
                        updates.extend(
                            self._pointer_update(
                                pointer, {"current_hp": current_hp}, record
                            )
                        )
        elif method == "OnMsgSyncCurrentMaxHp" and args:
            values: dict[str, float] = {}
            try:
                current_hp = max(0.0, float(args[0]))
                if self._valid_hp_value(current_hp):
                    values["current_hp"] = current_hp
            except (TypeError, ValueError, OverflowError):
                pass
            if len(args) > 1:
                try:
                    max_hp = max(0.0, float(args[1]))
                    if self._valid_hp_value(max_hp, maximum=True):
                        values["max_hp"] = max_hp
                except (TypeError, ValueError, OverflowError):
                    pass
            if pointer in self.root_pointers:
                self.pointer_candidates.pop(pointer, None)
                self.pointer_entities.pop(pointer, None)
                self.pointer_state.pop(pointer, None)
            elif values:
                timestamp = int(record.get("filetime_100ns", 0) or 0)
                entity_id = self.pointer_entities.get(pointer)
                accept_update = True
                if (
                    entity_id is not None
                    and entity_id == self.active_boss_entity_id
                ):
                    if (
                        self.active_boss_pointer is None
                        and self._boss_pointer_has_evidence(pointer, timestamp)
                    ):
                        self.active_boss_pointer = pointer
                    if self.active_boss_pointer != pointer:
                        accept_update = False
                    if (
                        accept_update
                        and "current_hp" in values
                        and not self._valid_boss_hp(
                            entity_id, values["current_hp"], timestamp
                        )
                    ):
                        accept_update = False
                    if accept_update and "current_hp" in values:
                        updates.extend(
                            self._inferred_team_damage_updates(
                                pointer,
                                entity_id,
                                self.entity_current_hp.get(entity_id),
                                values["current_hp"],
                                record,
                            )
                        )
                elif entity_id is not None and self._is_active_encounter_auxiliary(
                    entity_id
                ):
                    current_hp = values.get("current_hp")
                    if current_hp is not None and not self._valid_encounter_auxiliary_hp(
                        entity_id, current_hp, timestamp
                    ):
                        accept_update = False
                    if accept_update and current_hp is not None:
                        updates.extend(
                            self._inferred_team_damage_updates(
                                pointer,
                                entity_id,
                                self.entity_current_hp.get(entity_id),
                                current_hp,
                                record,
                                )
                            )
                if (
                    accept_update
                    and entity_id == self.active_boss_entity_id
                    and "max_hp" in values
                ):
                    known_max_hp = float(self.entity_max_hp.get(entity_id, 0) or 0)
                    incoming_max_hp = float(values.get("max_hp", 0) or 0)
                    if known_max_hp > 0 and incoming_max_hp < known_max_hp * 0.5:
                        values.pop("max_hp", None)
                if accept_update and values:
                    updates.extend(self._pointer_update(pointer, values, record))
                    if entity_id:
                        if "current_hp" in values:
                            self.entity_current_hp[entity_id] = values["current_hp"]
                            self.entity_current_hp_time[entity_id] = timestamp
                        if values.get("max_hp", 0) > 0:
                            if entity_id == self.active_boss_entity_id:
                                self.entity_max_hp[entity_id] = max(
                                    values["max_hp"],
                                    self.entity_max_hp.get(entity_id, 0.0),
                                )
                            else:
                                self.entity_max_hp[entity_id] = values["max_hp"]
                        updates.extend(self._team_hp_bindings(record))
        elif method == "OnMsgSyncDirtyFightAttributes" and args:
            attributes = direct_numeric_map(args[0])
            try:
                max_hp = max(0.0, float(attributes[21]))
            except (KeyError, TypeError, ValueError, OverflowError):
                max_hp = -1.0
            if self._valid_hp_value(max_hp, maximum=True):
                entity_id = self.pointer_entities.get(pointer)
                known_boss_max = (
                    float(self.entity_max_hp.get(entity_id, 0) or 0)
                    if entity_id == self.active_boss_entity_id
                    else 0.0
                )
                if not (
                    entity_id == self.active_boss_entity_id
                    and self.active_boss_pointer not in (None, pointer)
                ) and not (
                    known_boss_max > 0 and max_hp < known_boss_max * 0.5
                ):
                    updates.extend(
                        self._pointer_update(pointer, {"max_hp": max_hp}, record)
                    )
                    if entity_id and max_hp > 0:
                        if entity_id == self.active_boss_entity_id:
                            self.entity_max_hp[entity_id] = max(
                                max_hp, self.entity_max_hp.get(entity_id, 0.0)
                            )
                        else:
                            self.entity_max_hp[entity_id] = max_hp
                        updates.extend(self._team_hp_bindings(record))
        elif method == "OnMsgEntityDead":
            entity_id = self.pointer_entities.get(pointer)
            if entity_id:
                if (
                    entity_id == self.active_boss_entity_id
                    and self.active_boss_pointer != pointer
                ):
                    return updates
                if (
                    entity_id == self.active_boss_entity_id
                    and self.active_boss_pointer == pointer
                ):
                    updates.extend(
                        self._inferred_team_damage_updates(
                            pointer,
                            entity_id,
                            self.entity_current_hp.get(entity_id),
                            0.0,
                            record,
                            )
                        )
                elif self._is_active_encounter_auxiliary(entity_id):
                    updates.extend(
                        self._inferred_team_damage_updates(
                            pointer,
                            entity_id,
                            self.entity_current_hp.get(entity_id),
                            0.0,
                            record,
                        )
                    )
                updates.extend(
                    self._pointer_update(
                        pointer,
                        {"current_hp": 0.0, "death_confirmed": True},
                        record,
                    )
                )
                self.entity_current_hp[entity_id] = 0.0
                self.entity_current_hp_time[entity_id] = int(
                    record.get("filetime_100ns", 0) or 0
                )
                if entity_id == self.active_boss_entity_id:
                    self._release_active_boss(entity_id)

        if method == "RetGetServerLevelInfo" and args and self.self_id:
            try:
                level = int(args[0])
            except (TypeError, ValueError, OverflowError):
                level = 0
            if 1 <= level <= 200:
                update = self._profile_update(self.self_id, record, level=level)
                if update:
                    updates.append(update)

        updates.extend(self._stage_combat_statistics_updates(record, args))
        updates.extend(self._cache_token_profiles(record, args))
        updates.extend(self._scene_updates(record, args))
        updates.extend(self._known_target_profile_updates(record, args))
        updates.extend(self._generic_profile_updates(record, args))
        updates.extend(self._team_join_success_updates(record, args))
        updates.extend(self._readiness_identity_updates(record, args))
        updates.extend(self._team_statistics_updates(record, args))
        updates.extend(self._party_updates(record, args))
        updates.extend(self._life_updates(record, args))
        if method in CURRENT_TEAM_ACTIVITY_METHODS and self.party_ids:
            if method == "OnUpdateTeamGroupSelfProps" or any(
                self._team_token(value) in self.party_tokens
                for value in walk_values(args)
            ):
                self._record_party_activity(record)
        return updates


def monotonic_timestamp() -> float:
    """Small injectable clock helper for diagnostics and future expiry logic."""
    return time.monotonic()
