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
    7_102_401: {
        "name": "禁锢",
        "parent_template_ids": (7_102_400,),
        "infer_when_template_missing": True,
    },
    7_102_402: {
        "name": "星光守卫",
        "parent_template_ids": (7_102_400,),
        "keeps_encounter_alive": True,
    },
    7_102_404: {
        "name": "禁锢",
        "parent_template_ids": (7_102_403,),
        "infer_when_template_missing": True,
    },
    7_102_405: {
        "name": "星光守卫",
        "parent_template_ids": (7_102_403,),
        "keeps_encounter_alive": True,
    },
    7_107_121: {
        "name": "冰牢",
        "parent_template_ids": (7_107_105,),
    },
}
MULTIPHASE_BOSS_TEMPLATE_IDS = frozenset(
    int(parent_template_id)
    for auxiliary in ENCOUNTER_AUXILIARY_TEMPLATES.values()
    if auxiliary.get("keeps_encounter_alive")
    for parent_template_id in auxiliary.get("parent_template_ids", ())
    if int(parent_template_id)
)


def unique_inferred_auxiliary_for_parent(
    parent_template_id: int,
) -> tuple[int, dict[str, object]] | None:
    matches = [
        (int(template_id), auxiliary)
        for template_id, auxiliary in ENCOUNTER_AUXILIARY_TEMPLATES.items()
        if auxiliary.get("infer_when_template_missing")
        and int(parent_template_id or 0)
        in {
            int(value)
            for value in auxiliary.get("parent_template_ids", ())
            if int(value)
        }
    ]
    return matches[0] if len(matches) == 1 else None


ENCOUNTER_NON_BOSS_TEMPLATE_IDS = frozenset(
    {
        7_102_401,
        7_102_402,
        7_102_404,
        7_102_405,
        7_102_406,
        7_102_407,
        # LevelMap5200205.Boss has the display name "洛克·金", but the
        # native component reports boss_type=0 and its 120,110 HP encounter is
        # an ordinary scripted unit rather than a DPS Boss.
        7_110_551,
    }
)
# MonsterData currently marks these encounter bosses with a non-Boss type.
# Keep exceptions template-scoped; name-only exceptions would also promote
# same-name mechanics and story NPCs.
EXPLICIT_NON_TYPE3_BOSS_TEMPLATE_IDS = frozenset(
    {
        7_107_030,  # First Karl Edgar encounter (four linked manifestations).
        7_265_156,  # Dream Catcher.
    }
)
# Unlike encounter auxiliaries above, these templates must not be promoted by
# either catalog metadata or an allowlisted display name.
HARD_EXCLUDED_BOSS_TEMPLATE_IDS = frozenset({7_110_551})
# These are separate encounters even though the second Boss appears while the
# previous entity can still look alive to the client.  The ordered pair also
# prevents delayed damage for the old entity from stealing the new Boss lock.
SEPARATE_BOSS_ENCOUNTER_TRANSITIONS = frozenset(
    {
        (7_110_642, 7_110_641),  # 洛克·金 -> 洛克·金·失控
    }
)
TEAM_STATISTICS_METHODS = {
    "RetCommonCombatStatisticsByTeam",
    "RetDirtyCommonCombatStatisticsByTeam",
}
STAGE_COMBAT_STATISTICS_METHOD = "OnMsgUpdateStageCombatStatistics"
SETTLEMENT_COMBAT_STATISTICS_METHOD = "OnMsgSettlementCombatStatistics"
TEAM_JOIN_SUCCESS_METHODS = {
    "OnJoinGroupSuccess",
    "OnCreateGroupSuccess",
}
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
BOSS_HP_DROP_CONFIRM_WINDOW_100NS = 3 * 10_000_000
BOSS_HP_DROP_CONFIRM_RATIO = 0.2
BOSS_SIGNAL_BIND_WINDOW_100NS = 3 * 10_000_000
BOSS_SIGNAL_ACTIVE_WINDOW_100NS = 10 * 10_000_000
BOSS_POINTER_HIGH_HP_FLOOR = 1_000_000.0
SAME_TEMPLATE_BOSS_REPLACEMENT_STALE_100NS = 5 * 10_000_000
NETWORK_ARGUMENT_MAX_DELAY_MS = 250.0

NETWORK_ARGUMENT_METHODS = frozenset(
    {
        "RetCastSkillSuccessNew",
        "RetGetServerLevelInfo",
        "OnMsgRefreshSceneObjects",
        "OnMsgSceneObjectCreated",
        "OnMsgSyncFightMode",
        "OnMsgCreateBullet",
        "OnMsgCreateLUnitSpellField",
        "OnMsgCreateLUnitSpellAgent",
        "OnMsgCreateLUnitAura",
        "OnMsgCreateLUnitTrap",
        "OnMsgHealSyncV2",
        "OnMsgCastSkillNew",
        "OnMsgDamageSyncV2",
        "OnMsgBeatenSyncV2",
        "OnMsgHitFeedback",
        "OnMsgHitFeedbackByID",
        "OnMsgTakeLastAttackDamage",
        "OnMsgEndureExitHit",
        "OnMsgAddBuffNew",
        "OnMsgActorBuffStateSync",
        "OnMsgSyncCurrentHp",
        "OnMsgSyncCurrentMaxHp",
        "OnMsgSyncDirtyFightAttributes",
        "OnMsgEntityRelive",
        "OnMsgPostAkEvent",
        "OnMsgSetHUDShow",
        "OnMsgReconnectOrEnter",
        "OnMsgDungeonReadinessCheck",
        "OnMsgDungeonStageSettlement",
        STAGE_COMBAT_STATISTICS_METHOD,
        SETTLEMENT_COMBAT_STATISTICS_METHOD,
        "OnMsgUpdateDungeonBattleStatistics",
        "OnMsgUpdateDungeonTeamPlayerBattleStatistics",
        "RetDungeonBattleStatistics",
        "RetMonsterBattleStatistics",
        "RetNpcCombatStatisticsByTeam",
        "RetDirtyNpcCombatStatisticsByTeam",
    }
)
NETWORK_ARGUMENT_METHOD_MARKERS = (
    "team",
    "group",
    "party",
    "member",
    "role",
    "player",
    "friend",
    "whisper",
    "invite",
    "tarot",
    "sceneobject",
)

# These packets carry a useful boundary in the method name itself and do not
# need their argument graph decoded.  Every other retained method either has an
# explicit decoder above or matches one of the identity/team method markers.
NETWORK_METHOD_ONLY_METHODS = frozenset(
    {
        "OnMsgBeforeEnterNewSpace",
        # Death handling is pointer-based. Decoding its unused argument graph
        # used to synchronously stall the game thread once per dead trash mob.
        "OnMsgEntityDead",
    }
)


def should_decode_network_arguments(method: str) -> bool:
    if method in NETWORK_ARGUMENT_METHODS:
        return True
    folded = method.casefold()
    return any(marker in folded for marker in NETWORK_ARGUMENT_METHOD_MARKERS)


def should_retain_network_record(method: str) -> bool:
    """Return whether a decoded RPC can affect DPS state or diagnostics."""
    return bool(
        method in NETWORK_METHOD_ONLY_METHODS
        or should_decode_network_arguments(method)
    )


def normalize_boss_name(value: object) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


BOSS_PLACEHOLDER_NAMES = frozenset(
    normalize_boss_name(value) for value in ("Boss", "首领", "未命名Boss")
)


def boss_name_is_placeholder(value: object) -> bool:
    return normalize_boss_name(value) in BOSS_PLACEHOLDER_NAMES


BOSS_PHASE_NAME_GROUPS = (
    frozenset(
        {
            normalize_boss_name("先祖铠甲"),
            normalize_boss_name("伯德温·威瑟尔"),
        }
    ),
)
BOSS_PHASE_TEMPLATE_TRANSITIONS = frozenset(
    {
        (7_103_402, 7_103_401),  # 先祖铠甲 -> 伯德温·威瑟尔
    }
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
        self.native_self_id: int | None = None
        self.self_token: str | None = None
        self.self_confirmed = False
        self.token_actors: dict[str, int] = {}
        self.actor_tokens: dict[int, str] = {}
        self.token_max_hp: dict[str, float] = {}
        self.team_profile_markers: dict[str, int] = {}
        self.self_profile_marker = 0
        self.readiness_expected_members = 0
        self.readiness_tokens: set[str] = set()
        self.server_level = 0
        self.dungeon_id = 0
        self.dungeon_stage_id = 0
        self.dungeon_stage_phase = 0
        self.dungeon_context_time_100ns = 0
        self.reconnect_dungeon_candidates: tuple[int, ...] = ()
        self.stage_combat_seconds_by_actor: dict[tuple[int, str], int] = {}
        self.entity_max_hp: dict[int, float] = {}
        self.entity_max_hp_time: dict[int, int] = {}
        self.entity_current_hp: dict[int, float] = {}
        self.entity_current_hp_time: dict[int, int] = {}
        self.pending_boss_hp_drops: dict[int, tuple[float, float, int]] = {}
        self.pending_boss_hp_rises: dict[int, tuple[float, float, int]] = {}
        self.combat_source_actors: set[int] = set()
        self.actor_profession_hints: dict[int, int] = {}
        self.pending_target_hits: dict[int, list[tuple[int, int, int]]] = {}
        self.pending_entity_hits: dict[int, list[tuple[int, int, int]]] = {}
        self.pending_exact_damage: dict[
            int, list[tuple[int, int, int, int, bool | None]]
        ] = {}
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
        self.entity_boss_types: dict[int, int] = {}
        self.encounter_auxiliary_entities: dict[int, tuple[int, ...]] = {}
        self.defeated_boss_entities: set[int] = set()
        self.scene_retired_boss_entities: set[int] = set()
        self.active_boss_entity_id: int | None = None
        self.active_boss_time_100ns = 0
        self.active_boss_pointer: int | None = None
        self.active_boss_pointer_time_100ns = 0
        self.active_boss_hp_epoch = 0
        self.active_boss_damage_epoch = 0
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
                if (
                    template_id > 0
                    and template_id not in HARD_EXCLUDED_BOSS_TEMPLATE_IDS
                    and (
                        boss_type == HUD_BOSS_TYPE
                        or template_id in EXPLICIT_NON_TYPE3_BOSS_TEMPLATE_IDS
                    )
                ):
                    self.boss_template_catalog[str(template_id)] = dict(raw_metadata)
        self.team_profile_cache: dict[str, dict] = {}
        self.team_profile_cache_dirty = False
        self.live_team_profile_tokens: set[str] = set()
        self.stage_bound_tokens: set[str] = set()
        self.scene_rebind_tokens: set[str] = set()
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
        try:
            decode_delay_ms = float(record.get("decode_delay_ms", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return []
        if decode_delay_ms > NETWORK_ARGUMENT_MAX_DELAY_MS:
            return []
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
        if pointer in self.combat_mode_pointers:
            updates.append(
                (
                    "combat_state",
                    {
                        **self._base_update(record),
                        "entity_id": entity_id,
                        "in_combat": True,
                    },
                )
            )
        if pending:
            update = self._base_update(record)
            update.update(pending)
            update["entity_id"] = entity_id
            updates.append(("monster", update))
            player_health = self._player_health_update(
                entity_id, pending, record, pointer=pointer
            )
            if player_health is not None:
                updates.append(("actor_health", player_health))
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
                self.entity_max_hp_time.setdefault(entity_id, timestamp)
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
            updates = [("monster", update)]
            player_health = self._player_health_update(
                entity_id, values, record, pointer=pointer
            )
            if player_health is not None:
                updates.append(("actor_health", player_health))
            return updates
        pending = self.pointer_state.setdefault(pointer, {})
        pending.update(values)
        # Pointer lifetimes are short across scene changes; a bounded cache also
        # prevents non-combat entities from accumulating indefinitely.
        if len(self.pointer_state) > 4096:
            oldest = next(iter(self.pointer_state))
            self.pointer_state.pop(oldest, None)
        return []

    def _player_health_update(
        self,
        entity_id: int,
        values: dict,
        record: dict,
        *,
        pointer: int,
    ) -> dict | None:
        """Return a guarded real-time party HP sample.

        ScriptEntity pointers are reused between scene objects.  A pointer that
        used to belong to a player has been observed carrying a Boss HP value,
        so a player binding alone is not sufficient evidence.  Team max HP is
        preferred and every current/max value must remain inside that bound.
        """
        explicit_members = set(self.party_ids) | set(self.actor_tokens)
        if self.self_id is not None:
            explicit_members.add(self.self_id)
        if entity_id not in explicit_members:
            return None

        token = self.actor_tokens.get(entity_id, "")
        team_max_hp = float(self.token_max_hp.get(token, 0.0) or 0.0)
        try:
            incoming_max_hp = float(values.get("max_hp", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            incoming_max_hp = 0.0
        observed_max_hp = float(self.entity_max_hp.get(entity_id, 0.0) or 0.0)
        expected_max_hp = team_max_hp
        if expected_max_hp <= 0:
            for candidate in (incoming_max_hp, observed_max_hp):
                if 0 < candidate < BOSS_POINTER_HIGH_HP_FLOOR:
                    expected_max_hp = candidate
                    break
        if expected_max_hp <= 0:
            return None
        if (
            incoming_max_hp > 0
            and not 0.98 <= incoming_max_hp / expected_max_hp <= 1.02
        ):
            return None

        update = self._base_update(record)
        update.update(
            {
                "entity_id": entity_id,
                "max_hp": expected_max_hp,
                "script_entity": int(pointer or 0),
                "health_source": "bound_realtime_hp",
            }
        )
        if "current_hp" in values:
            try:
                current_hp = float(values["current_hp"])
            except (TypeError, ValueError, OverflowError):
                return None
            if (
                not self._valid_hp_value(current_hp)
                or current_hp > expected_max_hp * 1.02
            ):
                return None
            update["current_hp"] = current_hp
        return update

    def _name_matches_boss_allowlist(self, value: object) -> bool:
        normalized = normalize_boss_name(value)
        return bool(
            normalized
            and any(alias in normalized for alias in self.boss_name_allowlist)
        )

    def _is_excluded_boss_entity(self, entity_id: int) -> bool:
        return bool(
            entity_id
            and int(self.entity_template_ids.get(entity_id, 0) or 0)
            in HARD_EXCLUDED_BOSS_TEMPLATE_IDS
        )

    def _boss_identity_confirmed(self, entity_id: int, name: object = "") -> bool:
        """Require template/type evidence in addition to an allowlisted name."""
        if not entity_id or self._is_excluded_boss_entity(entity_id):
            return False
        template_id = int(self.entity_template_ids.get(entity_id, 0) or 0)
        if template_id and str(template_id) in self.boss_template_catalog:
            return True
        return bool(
            self.entity_boss_types.get(entity_id) == HUD_BOSS_TYPE
            and self._name_matches_boss_allowlist(name)
        )

    def _resolved_boss_name(
        self,
        entity_id: int,
        candidate: object = "",
        template_id: object = 0,
    ) -> str:
        """Return a real Boss name without allowing placeholders to win."""
        incoming = plausible_name(candidate)
        if incoming and not boss_name_is_placeholder(incoming):
            return incoming
        try:
            parsed_template_id = int(template_id or 0)
        except (TypeError, ValueError, OverflowError):
            parsed_template_id = 0
        if not parsed_template_id:
            parsed_template_id = int(
                self.entity_template_ids.get(entity_id, 0) or 0
            )
        existing_profile = self.entity_profiles.get(entity_id, {})
        for value in (
            self.runtime_entity_names.get(entity_id, ""),
            existing_profile.get("name", ""),
            self.boss_template_catalog.get(str(parsed_template_id), {}).get(
                "name", ""
            ),
        ):
            name = plausible_name(value)
            if name and not boss_name_is_placeholder(name):
                return name
        return ""

    def _profile_marks_boss(self, entity_id: int, values: dict) -> bool:
        try:
            template_id = int(
                values.get("template_id")
                or self.entity_template_ids.get(entity_id, 0)
                or 0
            )
            boss_type = int(values.get("boss_type", -1) or 0)
            boss_rank = int(values.get("boss_rank", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            template_id = boss_type = boss_rank = 0
        entity_type = str(
            values.get("entity_type")
            or self.entity_profiles.get(entity_id, {}).get("entity_type", "")
        ).casefold()
        return bool(
            entity_id in self.confirmed_boss_entities
            or entity_id == self.active_boss_entity_id
            or "boss" in entity_type
            or "首领" in entity_type
            or boss_type == HUD_BOSS_TYPE
            or boss_rank > 0
            or (
                template_id
                and str(template_id) in self.boss_template_catalog
            )
        )

    def _profile_update(
        self, entity_id: int, record: dict, **values
    ) -> tuple[str, dict] | None:
        raw_profile_name = values.get("name")
        profile_name = plausible_name(raw_profile_name)
        if (
            boss_name_is_placeholder(raw_profile_name)
            and self._profile_marks_boss(entity_id, values)
        ):
            profile_name = self._resolved_boss_name(
                entity_id,
                raw_profile_name,
                values.get("template_id", 0),
            )
            if profile_name:
                values["name"] = profile_name
            else:
                values.pop("name", None)
        late_target = bool(
            entity_id == self.recent_target_id
            or entity_id in self.pending_entity_hits
        )
        if (
            profile_name
            and late_target
            and self._boss_identity_confirmed(entity_id, profile_name)
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

    def current_dungeon_context(self) -> dict[str, object]:
        """Return only IDs observed in explicit dungeon protocol fields.

        Readiness packets carry the dungeon ID, while stage-statistics and
        stage-settlement packets carry the battle/stage ID. Reconnect payloads
        are retained as candidates until a live sample establishes their exact
        field meanings; candidates are diagnostic and are never sent back to
        the server automatically.
        """
        return {
            "dungeon_id": int(self.dungeon_id or 0),
            "dungeon_stage_id": int(self.dungeon_stage_id or 0),
            "dungeon_stage_phase": int(self.dungeon_stage_phase or 0),
            "dungeon_context_filetime": int(
                self.dungeon_context_time_100ns or 0
            ),
            "reconnect_dungeon_candidates": list(
                self.reconnect_dungeon_candidates
            ),
        }

    def _record_dungeon_context(self, record: dict, args: list) -> None:
        method = str(record.get("method", ""))
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        changed = False

        if method == "OnMsgDungeonReadinessCheck" and len(args) > 1:
            try:
                dungeon_id = max(0, int(args[1] or 0))
            except (TypeError, ValueError, OverflowError):
                dungeon_id = 0
            if dungeon_id and dungeon_id != self.dungeon_id:
                self.dungeon_id = dungeon_id
                changed = True
        elif method == "OnMsgDungeonStageSettlement" and args:
            try:
                stage_id = max(0, int(args[0] or 0))
            except (TypeError, ValueError, OverflowError):
                stage_id = 0
            if stage_id and stage_id != self.dungeon_stage_id:
                self.dungeon_stage_id = stage_id
                changed = True
        elif method in {
            STAGE_COMBAT_STATISTICS_METHOD,
            SETTLEMENT_COMBAT_STATISTICS_METHOD,
        }:
            for candidate in args:
                fields = direct_numeric_map(candidate)
                if not map_pairs(fields.get(5)):
                    continue
                try:
                    stage_id = max(0, int(fields.get(0, 0) or 0))
                    stage_phase = max(0, int(fields.get(1, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    stage_id = 0
                    stage_phase = 0
                if stage_id and stage_id != self.dungeon_stage_id:
                    self.dungeon_stage_id = stage_id
                    changed = True
                if stage_phase != self.dungeon_stage_phase:
                    self.dungeon_stage_phase = stage_phase
                    changed = True
                break
        elif method == "OnMsgReconnectOrEnter":
            candidates: set[int] = set()
            for value in walk_values(args):
                if isinstance(value, bool):
                    continue
                try:
                    candidate = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if 5_000_000 <= candidate < 6_000_000:
                    candidates.add(candidate)
            next_candidates = tuple(sorted(candidates))
            if next_candidates != self.reconnect_dungeon_candidates:
                self.reconnect_dungeon_candidates = next_candidates
                changed = True

        if changed and timestamp:
            self.dungeon_context_time_100ns = max(
                self.dungeon_context_time_100ns, timestamp
            )

    def current_active_boss_state(self) -> dict[str, object] | None:
        """Return network-confirmed Boss metadata that is safe to reuse.

        The caller scopes this state to the same live game process. Current HP
        is intentionally excluded because it must always come from the live
        ScriptEntity stream after the overlay reconnects.
        """
        entity_id = int(self.active_boss_entity_id or 0)
        if not parse_combat_entity_id(entity_id):
            return None
        profile = self.entity_profiles.get(entity_id, {})
        template_id = int(self.entity_template_ids.get(entity_id, 0) or 0)
        max_hp = float(self.entity_max_hp.get(entity_id, 0.0) or 0.0)
        name = self._resolved_boss_name(entity_id, template_id=template_id)
        result: dict[str, object] = {
            "entity_id": entity_id,
            "filetime_100ns": int(self.active_boss_time_100ns or 0),
            "max_hp": max_hp,
        }
        if template_id:
            result["template_id"] = template_id
        if name:
            result["name"] = name
        try:
            level = int(profile.get("level", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            level = 0
        if level:
            result["level"] = level
        return result

    def restore_active_boss_state(
        self, state: dict[str, object]
    ) -> list[tuple[str, dict]]:
        """Restore only a same-process Boss identity and its confirmed max HP."""
        if not isinstance(state, dict):
            return []
        entity_id = parse_combat_entity_id(state.get("entity_id"))
        try:
            template_id = int(state.get("template_id", 0) or 0)
            max_hp = float(state.get("max_hp", 0) or 0)
            timestamp = int(state.get("filetime_100ns", 0) or 0)
            level = int(state.get("level", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return []
        if (
            not entity_id
            or template_id in HARD_EXCLUDED_BOSS_TEMPLATE_IDS
            or not self._valid_hp_value(max_hp, maximum=True)
        ):
            return []
        template_profile = self.boss_template_catalog.get(str(template_id), {})
        name = self._resolved_boss_name(
            entity_id,
            state.get("name"),
            template_id,
        )
        if not template_profile and not self._name_matches_boss_allowlist(name):
            return []

        record = {
            "filetime_100ns": timestamp,
            "event_time": "",
            "method": "same_process_boss_restore",
        }
        self.confirmed_boss_entities.add(entity_id)
        self.active_boss_entity_id = entity_id
        self.active_boss_time_100ns = timestamp
        self.entity_max_hp[entity_id] = max_hp
        if template_id:
            self.entity_template_ids[entity_id] = template_id
        if name:
            self.runtime_entity_names[entity_id] = name

        values: dict[str, object] = {
            "entity_type": "Boss",
            "boss_type": HUD_BOSS_TYPE,
            "boss_rank": 3,
            "boss_source": "same_process_cache",
        }
        if template_id:
            values["template_id"] = template_id
        if name:
            values["name"] = name
        if level:
            values["level"] = level
        updates: list[tuple[str, dict]] = []
        profile = self._profile_update(entity_id, record, **values)
        if profile:
            updates.append(profile)
        updates.append(
            (
                "monster",
                {
                    **self._base_update(record),
                    "entity_id": entity_id,
                    "max_hp": max_hp,
                },
            )
        )
        return updates

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
            self.live_team_profile_tokens.add(token)
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
        self,
        entity_id: int,
        record: dict,
        *,
        damage_evidence: bool = False,
        corroborated_signal: bool = False,
    ) -> bool:
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        if entity_id in self.scene_retired_boss_entities:
            if not (damage_evidence or corroborated_signal):
                return False
            self.scene_retired_boss_entities.discard(entity_id)
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
            current_template_id = int(
                self.entity_template_ids.get(current_entity_id, 0) or 0
            )
            incoming_template_id = int(
                self.entity_template_ids.get(entity_id, 0) or 0
            )
            phase_continuation = bool(
                boss_phase_continues(current_name, incoming_name)
                or (current_template_id, incoming_template_id)
                in BOSS_PHASE_TEMPLATE_TRANSITIONS
            )
            separate_encounter = (
                current_template_id,
                incoming_template_id,
            ) in SEPARATE_BOSS_ENCOUNTER_TRANSITIONS
            stale_predecessor = (
                incoming_template_id,
                current_template_id,
            ) in SEPARATE_BOSS_ENCOUNTER_TRANSITIONS
            incoming_is_trusted = self._name_matches_boss_allowlist(
                incoming_name
            )
            active_activity = max(
                int(self.active_boss_time_100ns or 0),
                int(self.active_boss_damage_epoch or 0),
                int(self.entity_current_hp_time.get(current_entity_id, 0) or 0),
            )
            same_template_replacement = bool(
                current_template_id
                and current_template_id == incoming_template_id
                and incoming_is_trusted
                and (current_hp is not None or self.active_boss_damage_epoch)
                and timestamp > active_activity
                and timestamp - active_activity
                >= SAME_TEMPLATE_BOSS_REPLACEMENT_STALE_100NS
            )
            # Native metadata describes every spawned Boss-like unit, including
            # adds and mechanics. Metadata alone must never steal the active
            # lock. A confirmed same-template respawn may replace a stale live
            # entity after a wipe; otherwise a name-confirmed Boss needs direct
            # damage and untrusted Boss-like units wait for the old target to die.
            if stale_predecessor and current_is_alive:
                return False
            if not phase_continuation and not separate_encounter and (
                (not damage_evidence and not same_template_replacement)
                or (
                    current_is_alive
                    and not incoming_is_trusted
                    and not corroborated_signal
                )
            ):
                return False
        if entity_id in self.defeated_boss_entities:
            if not damage_evidence:
                return False
            self.defeated_boss_entities.discard(entity_id)
        if self.active_boss_entity_id != entity_id:
            self.active_boss_pointer = None
            self.active_boss_pointer_time_100ns = 0
            self.active_boss_hp_epoch = 0
            self.active_boss_damage_epoch = 0
            self.pending_target_hits.clear()
            self.pending_entity_hits.clear()
            self.pending_exact_damage.clear()
            self.pending_boss_hp_drops.clear()
            self.pending_boss_hp_rises.clear()
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

    def should_forward_damage_event(self, event: dict) -> bool:
        """Fail closed for a target already confirmed as ordinary trash."""
        try:
            target_id = int(event.get("target_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if not target_id:
            return False
        if (
            self.active_boss_entity_id is not None
            or target_id in self.confirmed_boss_entities
            or target_id in self.encounter_auxiliary_entities
        ):
            return True
        profile = self.entity_profiles.get(target_id, {})
        identity_known = bool(
            target_id in self.entity_template_ids
            or target_id in self.entity_boss_types
            or str(profile.get("entity_type", "")).strip()
        )
        return not identity_known

    def _infer_active_encounter_auxiliary(
        self, entity_id: int, record: dict
    ) -> list[tuple[str, dict]]:
        active_boss_id = int(self.active_boss_entity_id or 0)
        active_template_id = int(
            self.entity_template_ids.get(active_boss_id, 0) or 0
        )
        inferred = unique_inferred_auxiliary_for_parent(active_template_id)
        if (
            not entity_id
            or entity_id == active_boss_id
            or inferred is None
            or entity_id == self.self_id
            or entity_id in self.party_ids
            or entity_id in self.actor_tokens
            or entity_id in self.confirmed_boss_entities
        ):
            return []

        template_id, metadata = inferred
        existing_template_id = int(
            self.entity_template_ids.get(entity_id, 0) or 0
        )
        if existing_template_id and existing_template_id != template_id:
            return []
        existing_profile = self.entity_profiles.get(entity_id, {})
        if str(existing_profile.get("entity_type", "")).casefold() in {
            "player",
            "role",
        }:
            return []
        canonical_name = str(metadata.get("name", "")).strip()
        existing_name = str(
            self.runtime_entity_names.get(entity_id, "")
            or existing_profile.get("name", "")
        ).strip()
        if existing_name and existing_name.casefold() not in {
            canonical_name.casefold(),
            "monster",
            "小怪",
        }:
            return []

        parent_templates = tuple(
            int(value)
            for value in metadata.get("parent_template_ids", ())
            if int(value)
        )
        self.entity_template_ids[entity_id] = template_id
        self.encounter_auxiliary_entities[entity_id] = parent_templates
        self.runtime_entity_names[entity_id] = canonical_name
        party_changed = self._discard_player_classification(entity_id)
        updates: list[tuple[str, dict]] = []
        profile = self._profile_update(
            entity_id,
            record,
            name=canonical_name,
            entity_type="Monster",
            template_id=template_id,
            encounter_auxiliary=True,
            encounter_parent_template_ids=list(parent_templates),
            auxiliary_inferred=True,
        )
        if profile:
            updates.append(profile)
        if party_changed:
            updates.append(self._party_update(record, authoritative=True))
        return updates

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
            self.active_boss_hp_epoch = 0
            self.active_boss_damage_epoch = 0
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
        self.active_boss_hp_epoch = 0
        self.active_boss_damage_epoch = 0
        self.pending_boss_hp_drops.pop(entity_id, None)
        self.pending_boss_hp_rises.pop(entity_id, None)

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
            or int(self.entity_template_ids.get(entity_id, 0) or 0)
            in ENCOUNTER_NON_BOSS_TEMPLATE_IDS
            or str(
                self.entity_profiles.get(entity_id, {}).get("entity_type", "")
            ).casefold()
            in {"player", "role"}
        ):
            return None
        recent_hits = [
            (int(actor_id), int(skill_id), int(hit_time))
            for actor_id, skill_id, hit_time in self.pending_entity_hits.get(
                entity_id, []
            )
            if signal_time <= int(hit_time) <= timestamp
            and self._is_known_player_actor(int(actor_id), int(hit_time))
        ]
        if len({actor_id for actor_id, _skill_id, _time in recent_hits}) < 2:
            return None

        # Activation normally clears correlations from the previous target. The
        # corroborating hits are also what tie the still-unbound HP pointer to
        # this entity, so retain only the recent evidence across the promotion.
        cutoff = timestamp - TEAM_HIT_SKILL_WINDOW_100NS
        preserved_target_hits = {
            pointer: [
                item
                for item in hits
                if cutoff <= int(item[2]) <= timestamp
            ]
            for pointer, hits in self.pending_target_hits.items()
        }
        preserved_target_hits = {
            pointer: hits
            for pointer, hits in preserved_target_hits.items()
            if hits
        }
        if not self._activate_boss(
            entity_id,
            record,
            damage_evidence=True,
            corroborated_signal=True,
        ):
            return None
        self.pending_entity_hits[entity_id] = recent_hits
        self.pending_target_hits.update(preserved_target_hits)
        self.active_boss_damage_epoch = max(
            hit_time for _actor_id, _skill_id, hit_time in recent_hits
        )
        values: dict[str, object] = {
            "entity_type": "Boss",
            "boss_type": HUD_BOSS_TYPE,
            "boss_rank": 3,
            "template_path": signal[:160],
            "boss_source": "hud_signal_team_target",
        }
        resolved_name = self._resolved_boss_name(entity_id)
        if resolved_name:
            values["name"] = resolved_name
        return self._profile_update(entity_id, record, **values)

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

    def _is_confirmed_non_player_actor(self, actor_id: int) -> bool:
        return bool(
            actor_id
            and (
                actor_id in self.confirmed_boss_entities
                or actor_id == self.active_boss_entity_id
                or actor_id in self.encounter_auxiliary_entities
            )
        )

    def _parse_known_player_actor_id(self, value: object) -> int | None:
        """Accept roster-confirmed players in the skill-instance ID gap.

        Most combat entities are outside 8e12..1e13, which is also occupied by
        skill-instance IDs. Some real players use that range, though, so an ID
        already bound by an authoritative team packet is safe to accept in an
        explicit actor field without admitting unknown skill instances.
        """
        parsed = parse_combat_entity_id(value)
        if parsed is not None:
            return parsed
        if isinstance(value, bool):
            return None
        try:
            candidate = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not LOW_COMBAT_ENTITY_ID_MIN <= candidate <= ENTITY_ID_MAX:
            return None
        if (
            candidate == self.self_id
            or candidate in self.party_ids
            or candidate in self.actor_tokens
            or candidate in self.token_actors.values()
        ):
            return candidate
        return None

    def _discard_player_classification(self, entity_id: int) -> bool:
        """Undo player inference when native template metadata arrives late."""
        if not entity_id:
            return False
        self.player_attackers.discard(entity_id)
        self.combat_source_actors.discard(entity_id)
        self.actor_profession_hints.pop(entity_id, None)
        self.recent_skill_sources = [
            source
            for source in self.recent_skill_sources
            if source[1] != entity_id
        ]

        party_changed = entity_id in self.party_ids
        self.party_ids.discard(entity_id)
        token = self.actor_tokens.pop(entity_id, None)
        if token and self.token_actors.get(token) == entity_id:
            cached_profile = self.team_profile_cache.get(token, {})
            replacement = stable_team_actor_id(
                token, cached_profile.get("role_number", 0)
            )
            self.token_actors[token] = replacement
            self.actor_tokens[replacement] = token
            if token != self.self_token and token in self.party_tokens:
                self.party_ids.add(replacement)
                party_changed = True
        if self.self_id == entity_id:
            self.self_id = None
            self.native_self_id = None
            self.self_confirmed = False
            party_changed = True

        cached = self.entity_profiles.get(entity_id)
        if cached is not None:
            for key in (
                "name",
                "entity_type",
                "profession_id",
                "role_number",
                "user_token",
            ):
                cached.pop(key, None)
        return party_changed

    def _rebind_team_actor(
        self,
        token: str,
        actor_id: int,
        record: dict,
        *,
        allow_positive_rebind: bool = False,
    ) -> list[tuple[str, dict]]:
        old_actor = self.token_actors.get(token)
        if (
            old_actor == actor_id
            or not actor_id
            or self._is_confirmed_non_player_actor(actor_id)
        ):
            return []
        if token in self.stage_bound_tokens and old_actor is not None and old_actor > 0:
            # Stage combat statistics provide the exact live actor ID. HP and
            # profession matching are only fallbacks before that packet arrives.
            return []
        if (
            old_actor is not None
            and old_actor > 0
            and not allow_positive_rebind
            and token not in self.scene_rebind_tokens
        ):
            # A positive actor already came from a live entity namespace. A
            # single matching class/HP value cannot prove it should be replaced
            # by another positive actor and previously caused names to rotate.
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
        self.scene_rebind_tokens.discard(token)
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
            # Profiles received for this live scene are authoritative. Cached
            # projection rosters only fill tokens missed before capture began;
            # they must never replace names delivered moments earlier by the
            # current OnMsgOtherJoinTeamGroup packet.
            if token in self.live_team_profile_tokens:
                continue
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
            and not self._is_confirmed_non_player_actor(entity_id)
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
            updates.extend(
                self._rebind_team_actor(
                    token,
                    actor_id,
                    record,
                    allow_positive_rebind=True,
                )
            )
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
            if not self._is_confirmed_non_player_actor(actor_id)
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
                if (
                    entity_id == self.self_id
                    or entity_id in self.actor_tokens
                    or self._is_confirmed_non_player_actor(entity_id)
                ):
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
        self.scene_retired_boss_entities.update(self.confirmed_boss_entities)
        if self.active_boss_entity_id is not None:
            self.scene_retired_boss_entities.add(self.active_boss_entity_id)
        self.scene_rebind_tokens = set(self.party_tokens)
        self.scene_rebind_tokens.discard(self.self_token or "")
        self.pointer_entities.clear()
        self.pointer_state.clear()
        self.pointer_candidates.clear()
        self.recent_target_id = None
        self.recent_target_time_100ns = 0
        self.entity_max_hp.clear()
        self.entity_max_hp_time.clear()
        self.entity_current_hp.clear()
        self.entity_current_hp_time.clear()
        self.pending_boss_hp_drops.clear()
        self.pending_boss_hp_rises.clear()
        self.combat_source_actors.clear()
        self.actor_profession_hints.clear()
        self.pending_target_hits.clear()
        self.pending_entity_hits.clear()
        self.pending_exact_damage.clear()
        self.pending_local_casts.clear()
        self.recent_skill_sources.clear()
        self.pending_boss_signal = None
        self.native_self_id = None
        self.confirmed_boss_entities.clear()
        self.entity_template_ids.clear()
        self.entity_boss_types.clear()
        self.encounter_auxiliary_entities.clear()
        self.defeated_boss_entities.clear()
        self.active_boss_entity_id = None
        self.active_boss_time_100ns = 0
        self.active_boss_pointer = None
        self.active_boss_pointer_time_100ns = 0
        self.active_boss_hp_epoch = 0
        self.active_boss_damage_epoch = 0
        self.combat_mode_pointers.clear()
        self.stage_bound_tokens.clear()

    def _record_combat_source(
        self,
        record: dict,
        actor_id: int,
        skill_id: object = 0,
        *,
        bind_pointer: bool = False,
    ) -> list[tuple[str, dict]]:
        actor_id = self._parse_known_player_actor_id(actor_id) or 0
        if not actor_id or self._is_confirmed_non_player_actor(actor_id):
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
            and not self._is_confirmed_non_player_actor(actor_id)
            and (
                actor_id == self.self_id
                or actor_id in self.party_ids
                or actor_id in self.actor_tokens
                or actor_id in self.player_attackers
                or actor_id in self.actor_profession_hints
                or self._recent_actor_skill(actor_id, timestamp)
            )
        )

    def _party_attacker_classification(
        self, actor_id: int, skill_id: int
    ) -> bool | None:
        """Classify a damage source against the live party without guessing IDs.

        Team packets can expose role/token actors while DamageSync uses the live
        combat entity.  Until those namespaces are rebound, profession capacity
        is the strongest roster evidence available.  A complete roster can
        reject an impossible profession; incomplete rosters remain undecided so
        the combat model can apply its participant-count limit.
        """
        if not actor_id:
            return False
        if (
            actor_id == self.self_id
            or actor_id in self.party_ids
            or actor_id in self.actor_tokens
        ):
            return True
        if not self.party_seen or self.party_member_count <= 1:
            return False

        profession_id = int(
            self.actor_profession_hints.get(actor_id, 0)
            or profession_from_skill(skill_id)
            or 0
        )
        roster_tokens = set(self.authoritative_party_tokens or self.party_tokens)
        if self.self_token:
            roster_tokens.add(self.self_token)
        roster_professions = [
            self._token_profession_id(token) for token in roster_tokens
        ]
        complete_roster = bool(
            len(roster_tokens) >= self.party_member_count
            and all(roster_professions)
        )
        if profession_id and profession_id in roster_professions:
            return True
        if profession_id and complete_roster:
            return False
        return None

    def _record_target_hit(
        self, pointer: int, actor_id: int, record: dict
    ) -> None:
        actor_id = self._parse_known_player_actor_id(actor_id) or 0
        if (
            not pointer
            or pointer in self.root_pointers
            or not actor_id
            or actor_id == self.active_boss_entity_id
        ):
            return
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        pending = self.pending_target_hits.setdefault(pointer, [])
        pending.append(
            (actor_id, self._recent_actor_skill(actor_id, timestamp), timestamp)
        )
        if self.pointer_entities.get(pointer) == self.active_boss_entity_id:
            self.active_boss_damage_epoch = max(
                self.active_boss_damage_epoch, timestamp
            )
        if len(pending) > 4096:
            del pending[:-2048]

    def _record_entity_hit(
        self,
        entity_id: int | None,
        actor_id: int | None,
        skill_id: object,
        record: dict,
    ) -> list[tuple[str, dict]]:
        """Remember a player action that explicitly names its damage target."""
        actor_id = self._parse_known_player_actor_id(actor_id) or 0
        if (
            not entity_id
            or not actor_id
            or not parse_combat_entity_id(entity_id)
            or actor_id == entity_id
            or actor_id in self.confirmed_boss_entities
        ):
            return []
        normalized_skill_id = normalize_network_skill_id(skill_id)
        if not is_player_skill(normalized_skill_id):
            return []
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        if not timestamp:
            return []
        if entity_id == self.active_boss_entity_id:
            self.active_boss_damage_epoch = max(
                self.active_boss_damage_epoch, timestamp
            )
        pending = self.pending_entity_hits.setdefault(entity_id, [])
        # Cast, bullet and spell-agent messages can describe the same action.
        # Collapse only near-identical markers while retaining real repeats.
        if any(
            old_actor == actor_id
            and old_skill == normalized_skill_id
            and 0 <= timestamp - old_time <= 1_500_000
            for old_actor, old_skill, old_time in pending[-24:]
        ):
            return []
        pending.append((actor_id, normalized_skill_id, timestamp))
        if len(pending) > 4096:
            del pending[:-2048]
        updates: list[tuple[str, dict]] = []
        signal_profile = self._boss_signal_profile_update(entity_id, record)
        if signal_profile:
            updates.append(signal_profile)
        if (
            entity_id == self.active_boss_entity_id
            or entity_id in self.confirmed_boss_entities
            or self._is_active_encounter_auxiliary(entity_id)
        ):
            updates.append(
                (
                    "monster",
                    {
                        **self._base_update(record),
                        "entity_id": entity_id,
                    },
                )
            )
        return updates

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
            for exact_time, actor_id, _skill_id, _damage, _critical
            in self.pending_exact_damage.get(boss_id, [])
            if exact_time <= timestamp
            and timestamp - exact_time <= TEAM_HIT_SKILL_WINDOW_100NS
        }
        return bool(
            pointer in self.combat_mode_pointers
            or len(distinct_actors) >= 2
            or distinct_actors.intersection(exact_actors)
        )

    def _stale_player_pointer_matches_active_boss(
        self, pointer: int, current_hp: float, timestamp: int
    ) -> bool:
        """Recognize a reused Boss pointer that still carries a player binding."""
        boss_id = int(self.active_boss_entity_id or 0)
        mapped_actor = int(self.pointer_entities.get(pointer, 0) or 0)
        if (
            not boss_id
            or not mapped_actor
            or mapped_actor == boss_id
            or pointer in self.root_pointers
            or not self._is_known_player_actor(mapped_actor, timestamp)
        ):
            return False

        token = self.actor_tokens.get(mapped_actor, "")
        expected_player_hp = float(self.token_max_hp.get(token, 0.0) or 0.0)
        observed_player_max_hp = float(
            self.entity_max_hp.get(mapped_actor, 0.0) or 0.0
        )
        if (
            expected_player_hp <= 0
            and 0 < observed_player_max_hp < BOSS_POINTER_HIGH_HP_FLOOR
        ):
            expected_player_hp = observed_player_max_hp
        suspicious_hp_floor = (
            expected_player_hp * 4
            if expected_player_hp > 0
            else BOSS_POINTER_HIGH_HP_FLOOR
        )
        if current_hp <= suspicious_hp_floor:
            return False

        pointer_actors = {
            int(actor_id)
            for actor_id, _skill_id, hit_time in self.pending_target_hits.get(
                pointer, []
            )
            if 0 <= timestamp - int(hit_time) <= TEAM_HIT_SKILL_WINDOW_100NS
        }
        directed_actors = {
            int(actor_id)
            for actor_id, _skill_id, hit_time in self.pending_entity_hits.get(
                boss_id, []
            )
            if 0 <= timestamp - int(hit_time) <= TEAM_HIT_SKILL_WINDOW_100NS
        }
        # Two independently observed actors tie both streams to the same Boss.
        # This is deliberately stronger than the evidence used for an unbound
        # pointer so ordinary player HP streams cannot be stolen mid-fight.
        return len(pointer_actors.intersection(directed_actors)) >= 2

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
        pending_drop = self.pending_boss_hp_drops.pop(entity_id, None)
        if pending_drop is not None:
            baseline_hp, candidate_hp, pending_time = pending_drop
            elapsed = timestamp - pending_time
            if (
                0 < elapsed <= BOSS_HP_DROP_CONFIRM_WINDOW_100NS
                and current_hp < baseline_hp * 0.5
                and abs(current_hp - candidate_hp)
                <= max(250_000.0, baseline_hp * 0.05, candidate_hp * 0.25)
            ):
                return True
        pending_rise = self.pending_boss_hp_rises.pop(entity_id, None)
        if pending_rise is not None:
            baseline_hp, candidate_hp, pending_time = pending_rise
            elapsed = timestamp - pending_time
            if (
                0 < elapsed <= BOSS_HP_DROP_CONFIRM_WINDOW_100NS
                and current_hp > baseline_hp * 1.5
                and abs(current_hp - candidate_hp)
                <= max(250_000.0, candidate_hp * 0.1)
            ):
                return True
        if previous_hp is None or previous_hp <= 0:
            return True
        if current_hp <= previous_hp:
            # A damaged argument buffer can make one CurrentHp call decode as a
            # tiny skill/status value (the live 20,875,602 -> 2,011 sample).
            # Hold one extreme drop until the next HP sample confirms that the
            # Boss really remained in the lower half of the old health range.
            if (
                current_hp < previous_hp * BOSS_HP_DROP_CONFIRM_RATIO
                and previous_hp - current_hp
                >= max(250_000.0, previous_hp * 0.5)
            ):
                self.pending_boss_hp_drops[entity_id] = (
                    previous_hp,
                    current_hp,
                    timestamp,
                )
                return False
            return True
        # An ordinary heal is valid. A multi-fold rise must persist for a second
        # sample; otherwise one stale object value raises the baseline and the
        # next real HP sample becomes a large, fabricated damage event.
        upper_bound = max(previous_hp * 1.5, previous_hp + 1_000_000.0)
        if current_hp <= upper_bound:
            return True
        if (
            entity_id == self.active_boss_entity_id
            and self.active_boss_hp_epoch == timestamp
        ):
            return True
        self.pending_boss_hp_rises[entity_id] = (
            previous_hp,
            current_hp,
            timestamp,
        )
        return False

    def _boss_full_hp_reset_candidate(
        self, entity_id: int, current_hp: float
    ) -> bool:
        if entity_id != self.active_boss_entity_id:
            return False
        max_hp = float(self.entity_max_hp.get(entity_id, 0) or 0)
        previous_hp = self.entity_current_hp.get(entity_id)
        return bool(
            max_hp > 0
            and previous_hp is not None
            and previous_hp > 0
            and current_hp > previous_hp
            and current_hp >= max_hp * 0.995
            and max_hp - previous_hp >= max_hp * 0.2
        )

    def _boss_full_hp_reset_update(
        self, entity_id: int, current_hp: float, record: dict
    ) -> tuple[str, dict]:
        return (
            "monster",
            {
                **self._base_update(record),
                "entity_id": entity_id,
                "reset_candidate_hp": current_hp,
                "max_hp": float(self.entity_max_hp.get(entity_id, 0) or 0),
            },
        )

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

    def _discard_hit_correlations(
        self, pointer: int, entity_id: int, timestamp: int
    ) -> None:
        for mapping, key in (
            (self.pending_target_hits, pointer),
            (self.pending_entity_hits, entity_id),
        ):
            _discarded, remaining = self._consume_timed_items(
                mapping.get(key, []), timestamp
            )
            if remaining:
                mapping[key] = remaining
            else:
                mapping.pop(key, None)

    def _inferred_team_damage_updates(
        self,
        pointer: int,
        entity_id: int,
        previous_hp: float | None,
        current_hp: float,
        record: dict,
    ) -> list[tuple[str, dict]]:
        """Expire HP/hit correlations without fabricating player damage.

        Boss HP supplies only a team-wide loss and cannot identify each
        player's amount. Exact per-player totals now come exclusively from the
        Common/Dirty team-statistics RPCs, so an HP sample must never emit a
        guessed damage event.
        """
        del previous_hp, current_hp
        timestamp = int(record.get("filetime_100ns", 0) or 0)
        self._discard_hit_correlations(pointer, entity_id, timestamp)
        remaining_exact = [
            item
            for item in self.pending_exact_damage.get(entity_id, [])
            if int(item[0]) > timestamp
        ]
        if remaining_exact:
            self.pending_exact_damage[entity_id] = remaining_exact
        else:
            self.pending_exact_damage.pop(entity_id, None)
        return []

    def _confirm_local_actor(
        self,
        actor_id: int,
        record: dict,
        *,
        native: bool = False,
    ) -> list[tuple[str, dict]]:
        if not parse_combat_entity_id(actor_id):
            return []
        if (
            not native
            and self.self_confirmed
            and self.self_id is not None
            and self.self_id > 0
            and self.self_id != actor_id
        ):
            return []
        if self.self_token:
            token_actor = self.token_actors.get(self.self_token)
            if token_actor == actor_id:
                if native:
                    self.native_self_id = actor_id
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
            if not native and (token_actor is None or token_actor > 0):
                return []
            if native:
                self.native_self_id = actor_id
            return self._rebind_team_actor(
                self.self_token,
                actor_id,
                record,
                allow_positive_rebind=native,
            )
        if self.self_id == actor_id and self.self_confirmed:
            if native:
                self.native_self_id = actor_id
            return []
        old_actor = self.self_id
        merge_confirmed_native_form = bool(
            native
            and old_actor
            and old_actor == self.native_self_id
            and old_actor != actor_id
        )
        self.self_id = actor_id
        self.self_confirmed = True
        if native:
            self.native_self_id = actor_id
        updates: list[tuple[str, dict]] = []
        if (
            old_actor
            and old_actor != actor_id
            and (old_actor < 0 or merge_confirmed_native_form)
        ):
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
        if str(record.get("method", "")) not in TEAM_JOIN_SUCCESS_METHODS:
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
            self.live_team_profile_tokens.add(token)
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

    def _party_update(
        self,
        record: dict,
        *,
        authoritative: bool,
        left_team: bool = False,
    ) -> tuple[str, dict]:
        self.party_seen = True
        update = {
            **self._base_update(record),
            "entity_ids": sorted(self.party_ids),
            "member_count": min(MAX_PARTY_MEMBERS, self.party_member_count),
            "authoritative": authoritative,
        }
        if left_team:
            update["left_team"] = True
        return ("party", update)

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
        self.live_team_profile_tokens.clear()
        self.stage_bound_tokens.clear()
        self.scene_rebind_tokens.clear()
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
            entity_id = self._parse_known_player_actor_id(args[0])
            if entity_id:
                entity_type = args[1].strip()[:64] if len(args) > 1 and isinstance(args[1], str) else ""
                update = self._profile_update(entity_id, record, entity_type=entity_type)
                if update:
                    updates.append(update)
        elif method == "OnMsgRefreshSceneObjects" and len(args) > 1:
            for entity_key, descriptor in map_pairs(args[1]):
                entity_id = self._parse_known_player_actor_id(entity_key)
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
        if entity_id and name and not boss_name_is_placeholder(name):
            self.runtime_entity_names[entity_id] = name
        excluded_template = self._is_excluded_boss_entity(entity_id)
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
            and self._boss_identity_confirmed(entity_id, name)
            and late_start_evidence
            and entity_id != self.self_id
            and entity_id not in self.party_ids
            and entity_id not in self.actor_tokens
        ):
            self._activate_boss(entity_id, record, damage_evidence=True)
        if (
            not entity_id
            or excluded_template
            or entity_id != self.active_boss_entity_id
            or not name
            or boss_name_is_placeholder(name)
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
        if entity_id and boss_type >= 0:
            self.entity_boss_types[entity_id] = boss_type
        if template_id in HARD_EXCLUDED_BOSS_TEMPLATE_IDS:
            if entity_id:
                self.confirmed_boss_entities.discard(entity_id)
                if self.active_boss_entity_id == entity_id:
                    self._release_active_boss(entity_id)
            return []
        if entity_id in self.scene_retired_boss_entities:
            # The native component scan can see an object from the previous
            # space for several seconds after a transition. Metadata alone is
            # not evidence that this exact entity is alive in the new scene.
            return []
        template_profile = self.boss_template_catalog.get(str(template_id))
        auxiliary = ENCOUNTER_AUXILIARY_TEMPLATES.get(template_id)
        if entity_id and auxiliary is not None:
            parent_templates = tuple(
                int(value)
                for value in auxiliary.get("parent_template_ids", ())
                if int(value)
            )
            self.encounter_auxiliary_entities[entity_id] = parent_templates
            party_changed = self._discard_player_classification(entity_id)
            values: dict[str, object] = {
                "name": str(auxiliary.get("name", "")).strip() or "小怪",
                "entity_type": "Monster",
                "template_id": template_id,
                "boss_type": boss_type,
                "encounter_auxiliary": True,
                "encounter_parent_template_ids": list(parent_templates),
                "auxiliary_inferred": False,
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
            if party_changed:
                updates.append(self._party_update(record, authoritative=False))
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
            else None
        )
        if runtime_level in (None, ""):
            runtime_level = record.get("level")
        if runtime_level in (None, ""):
            runtime_level = self.server_level
        try:
            parsed_runtime_level = int(runtime_level or 0)
        except (TypeError, ValueError, OverflowError):
            parsed_runtime_level = 0
        if 1 <= parsed_runtime_level <= 200:
            values["level"] = parsed_runtime_level
        metadata_name = plausible_name(
            template_profile.get("name")
            if template_profile is not None
            else record.get("name")
        )
        runtime_name = self.runtime_entity_names.get(entity_id, "")
        if metadata_name or runtime_name:
            values["name"] = metadata_name or runtime_name
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
        method = str(record.get("method", ""))
        if method not in {
            STAGE_COMBAT_STATISTICS_METHOD,
            SETTLEMENT_COMBAT_STATISTICS_METHOD,
        } or not args:
            return []
        settlement_method = method == SETTLEMENT_COMBAT_STATISTICS_METHOD
        candidates = args[1:] + args[:1] if settlement_method else args[:1]
        stage: dict[int, object] = {}
        for candidate in candidates:
            parsed_stage = direct_numeric_map(candidate)
            if map_pairs(parsed_stage.get(5)):
                stage = parsed_stage
                break
        if not stage:
            return []
        try:
            stage_id = max(0, int(stage.get(0, 0) or 0))
        except (TypeError, ValueError, OverflowError):
            stage_id = 0
        completion_confirmed = stage.get(3) is True
        authoritative = settlement_method or completion_confirmed
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
            if actor_id <= 0 and token:
                actor_id = int(self.token_actors.get(token, 0) or 0)
            provisional_actor = bool(
                actor_id < 0
                and token
                and int(self.token_actors.get(token, 0) or 0) == actor_id
            )
            if (
                not token
                or token in used_tokens
                or actor_id == 0
                or (actor_id < 0 and not provisional_actor)
                or actor_id > ENTITY_ID_MAX
                or actor_id in used_actors
            ):
                continue
            used_tokens.add(token)
            used_actors.add(actor_id)
            members.append((token, actor_id, fields))
        if not members:
            return []
        # Stage statistics can retain a departed member after a replacement
        # has already joined. If the live roster is complete, use it to remove
        # those stale rows instead of discarding the remaining exact actor/name
        # bindings. Without a complete live roster an oversized table remains
        # unsafe and is rejected.
        live_roster_tokens = set(self.party_tokens) | set(
            self.authoritative_party_tokens
        )
        if self.self_token:
            live_roster_tokens.add(self.self_token)
        live_roster_complete = bool(
            self.party_seen
            and 0 < self.party_member_count <= MAX_PARTY_MEMBERS
            and len(live_roster_tokens) == self.party_member_count
        )
        if live_roster_complete:
            live_members = [
                member for member in members if member[0] in live_roster_tokens
            ]
            if len(live_members) == self.party_member_count:
                members = live_members
            elif len(members) > MAX_PARTY_MEMBERS:
                return []
        elif len(members) > MAX_PARTY_MEMBERS:
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
        self.stage_bound_tokens = set(desired)
        self.scene_rebind_tokens.difference_update(desired)

        updates: list[tuple[str, dict]] = []
        for token, actor_id, fields in members:
            if plausible_name(fields.get(5)):
                self.live_team_profile_tokens.add(token)
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
            self._profile_update(
                actor_id,
                record,
                name=plausible_name(fields.get(5)),
                profession_id=fields.get(4),
                level=fields.get(2),
                user_token=token,
                entity_type="Player",
            )
            # Always emit the complete exact snapshot. The parser cache can be
            # correct while a downstream model still carries an earlier
            # heuristic merge, so a change-only update is insufficient here.
            cached_profile = self.entity_profiles.get(actor_id, {})
            profile_snapshot = {
                **self._base_update(record),
                "entity_id": actor_id,
                "user_token": token,
                "entity_type": "Player",
            }
            for key in ("name", "profession_id", "level", "role_number"):
                value = cached_profile.get(key)
                if value not in (None, ""):
                    profile_snapshot[key] = value
            updates.append(("profile", profile_snapshot))

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
            skill_healing = direct_numeric_map(fields.get(35))
            try:
                damage_hits = max(0, int(fields.get(27, 0) or 0))
                critical_hits = max(0, int(fields.get(25, 0) or 0))
                deaths = max(0, int(fields.get(18, 0) or 0))
                profession_id = max(0, int(fields.get(4, 0) or 0))
                effective_healing = max(0, int(fields.get(17, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                damage_hits = 0
                critical_hits = 0
                deaths = 0
                profession_id = 0
                effective_healing = 0
            try:
                combat_seconds_total = max(0, int(fields.get(19, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                combat_seconds_total = 0
            combat_seconds_delta = 0
            if stage_id and combat_seconds_total:
                seconds_key = (stage_id, token)
                previous_seconds = self.stage_combat_seconds_by_actor.get(
                    seconds_key, 0
                )
                combat_seconds_delta = (
                    combat_seconds_total - previous_seconds
                    if combat_seconds_total >= previous_seconds
                    else combat_seconds_total
                )
                self.stage_combat_seconds_by_actor[seconds_key] = (
                    combat_seconds_total
                )
            valid_critical_counts = bool(
                damage_hits > 0 and critical_hits <= damage_hits
            )
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
            healing_skills: list[dict[str, int]] = []
            for raw_skill_id, raw_healing in skill_healing.items():
                skill_id = normalize_network_skill_id(raw_skill_id)
                try:
                    parsed_healing = max(0, int(raw_healing or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if skill_id and parsed_healing > 0:
                    healing_skills.append(
                        {
                            "skill_id": skill_id,
                            "effective_healing": parsed_healing,
                        }
                    )
            summary_row: dict[str, object] = {
                "actor_id": actor_id,
                "user_token": token,
                "name": plausible_name(fields.get(5)),
                "profession_id": profession_id,
                "damage": damage,
                "damage_hits": damage_hits if valid_critical_counts else None,
                "critical_hits": critical_hits if valid_critical_counts else None,
                "combat_seconds_total": combat_seconds_total,
                "combat_seconds_delta": combat_seconds_delta,
                "skills": skills,
                "effective_healing": effective_healing,
                "healing_skills": healing_skills,
            }
            if authoritative or 18 in fields:
                # Settlement rows omit field 18 when its value is zero.
                summary_row["deaths"] = deaths
            summary_rows.append(summary_row)
        summary_key = "|".join(str(stage.get(key, "")) for key in (0, 1, 2))
        if authoritative:
            summary_key = f"settlement|{summary_key}"
        updates.append(
            (
                "stage_summary",
                {
                    **self._base_update(record),
                    "summary_id": summary_key
                    or str(record.get("filetime_100ns", 0) or 0),
                    "stage_id": stage.get(0),
                    "stage_phase": stage.get(1),
                    "stage_token": str(stage.get(2, "") or ""),
                    "member_count": len(desired),
                    "actors": summary_rows,
                    "authoritative": authoritative,
                    "completion_confirmed": completion_confirmed,
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
            self.stage_bound_tokens.discard(token)
            self.scene_rebind_tokens.discard(token)
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
            self._clear_party_roster()
            # A self-leave notification is also an encounter boundary. Emit it
            # even when the local roster was already empty so the combat model
            # cannot retain a restored or partially observed fight.
            updates.append(
                self._party_update(
                    record,
                    authoritative=True,
                    left_team=True,
                )
            )
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
        elif method == "OnMsgEntityRelive":
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
            explicit_dead = False
            if token:
                actor_id = (
                    self.self_id
                    if token == self.self_token
                    else self.token_actors.get(token)
                )
            else:
                # Tokenless EntityRelive is delivered to the local entity.
                actor_id = self.self_id
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
            "explicit_transition": method == "OnMsgEntityRelive",
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
        if update["dead"]:
            update["death_confirmed"] = bool(
                method in {TEAM_SELF_PROPS_METHOD, "OnUpdateTeamGroupMemberProps"}
                and update.get("current_hp") == 0
            )
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
        if target_id == self.active_boss_entity_id:
            self.active_boss_damage_epoch = max(
                self.active_boss_damage_epoch, timestamp
            )
        skill_id = normalize_network_skill_id(raw_skill_id)
        updates: list[tuple[str, dict]] = []
        if target_id in self.confirmed_boss_entities:
            self._activate_boss(target_id, record, damage_evidence=True)
        else:
            runtime_name = self.runtime_entity_names.get(target_id, "")
            if (
                self._boss_identity_confirmed(target_id, runtime_name)
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
        confirmed_non_player = self._is_confirmed_non_player_actor(attacker_id)
        player_skill = is_player_skill(skill_id)
        if player_skill and not confirmed_non_player:
            self.player_attackers.add(attacker_id)
        if infer_identity and player_skill and not confirmed_non_player:
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
        if not confirmed_non_player:
            updates.extend(
                self._record_combat_source(record, attacker_id, skill_id)
            )
        known_player_attacker = bool(
            not confirmed_non_player
            and self._is_known_player_actor(attacker_id, timestamp)
        )
        if known_player_attacker:
            updates.extend(
                self._infer_active_encounter_auxiliary(target_id, record)
            )
        if (
            str(record.get("method", ""))
            in {"OnMsgDamageSyncV2", "KAPI_HandleDamageSyncV2"}
            and (
                target_id == self.active_boss_entity_id
                or self._is_active_encounter_auxiliary(target_id)
            )
        ):
            pending = self.pending_exact_damage.setdefault(target_id, [])
            pending.append(
                (
                    timestamp,
                    attacker_id,
                    skill_id,
                    damage,
                    int(args[3]) == 2,
                )
            )
            if len(pending) > 4096:
                del pending[:-2048]
        event = {
            **self._base_update(record),
            "function": "OnMsgDamageSyncV2/network",
            "attacker_id": attacker_id,
            "target_id": target_id,
            "active_boss": target_id == self.active_boss_entity_id,
            "player_attacker": (
                False
                if confirmed_non_player
                else True if known_player_attacker else None
            ),
            "party_attacker": (
                False
                if confirmed_non_player
                else self._party_attacker_classification(attacker_id, skill_id)
            ),
            "arg4_u64": raw_skill_id // 10_000 if raw_skill_id >= 1_000_000_000_000 else raw_skill_id,
            "skill_id": skill_id,
            "damage_source": "network_exact",
            "provisional_damage": False,
            "arg5_i32": int(args[3]),
            "critical": int(args[3]) == 2,
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
        full_snapshot = method == "RetCommonCombatStatisticsByTeam"
        authoritative = full_snapshot
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
            if plausible_name(fields.get(4)):
                self.live_team_profile_tokens.add(token)
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
            # Dirty snapshots omit field 5 when a member did not change. A
            # full Common snapshot, however, omits the same field only for a
            # zero value. Emit that zero so the model can establish an exact
            # pre-pull baseline before the member's first damaging update.
            omitted_zero = 5 not in fields
            if omitted_zero and not full_snapshot:
                continue
            try:
                absolute_damage = max(0, int(fields.get(5, 0) or 0))
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
                        "omitted_zero": omitted_zero,
                        "full_snapshot": full_snapshot,
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
            updates.extend(
                self._confirm_local_actor(
                    local_player_id,
                    native_record,
                    native=True,
                )
            )
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

        self._record_boss_signal(record, args)
        self._record_dungeon_context(record, args)

        if method == "OnMsgBeforeEnterNewSpace":
            previous_scene_id = self.scene_id
            self._reset_scene_combat_bindings()
            self.scene_id = None
            updates.append(
                (
                    "scene",
                    {
                        **self._base_update(record),
                        "scene_id": 0,
                        "previous_scene_id": previous_scene_id,
                        "force_reset": True,
                        "transition": True,
                        "entity_ids": [],
                    },
                )
            )

        if method == "RetCastSkillSuccessNew":
            updates.extend(self._local_cast_updates(record, args))

        if (
            method == "OnMsgRefreshSceneObjects"
            and len(args) > 1
            and isinstance(args[1], dict)
        ):
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
                # A real full refresh carries the scene ID followed by an object
                # map. The same RPC name is also used by another three-number
                # payload; treating its first value as a scene ID clears a live
                # encounter and all actor bindings in the middle of a fight.
                # Partial in-scene refreshes carry None as the scene ID.
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
            entity_id = self.pointer_entities.get(pointer)
            if fight_mode >= 0 and entity_id:
                updates.append(
                    (
                        "combat_state",
                        {
                            **self._base_update(record),
                            "entity_id": entity_id,
                            "in_combat": fight_mode == 2,
                        },
                    )
                )

        if method == "OnMsgCreateBullet" and len(args) >= 3:
            actor_id = self._parse_known_player_actor_id(args[2])
            if actor_id:
                updates.extend(
                    self._record_combat_source(
                        record, actor_id, args[0], bind_pointer=True
                    )
                )
                target_id = (
                    parse_combat_entity_id(args[10]) if len(args) > 10 else None
                )
                updates.extend(
                    self._record_entity_hit(target_id, actor_id, args[0], record)
                )
        elif method in {
            "OnMsgCreateLUnitSpellField",
            "OnMsgCreateLUnitSpellAgent",
            "OnMsgCreateLUnitAura",
            "OnMsgCreateLUnitTrap",
        } and args:
            fields = direct_numeric_map(args[0])
            actor_id = self._parse_known_player_actor_id(fields.get(0))
            if actor_id:
                updates.extend(
                    self._record_combat_source(
                        record, actor_id, fields.get(2, 0), bind_pointer=True
                    )
                )
                target_id = parse_combat_entity_id(
                    fields.get(17)
                ) or parse_combat_entity_id(fields.get(6))
                updates.extend(
                    self._record_entity_hit(
                        target_id, actor_id, fields.get(2, 0), record
                    )
                )
        elif method == "OnMsgHealSyncV2" and len(args) >= 3:
            actor_id = self._parse_known_player_actor_id(args[0])
            if actor_id:
                updates.extend(
                    self._record_combat_source(record, actor_id, args[2])
                )
                if len(args) >= 5:
                    target_id = parse_combat_entity_id(args[1])
                    skill_id = normalize_network_skill_id(args[2])
                    try:
                        attempted_healing = int(args[3])
                        effective_healing = int(args[4])
                    except (TypeError, ValueError, OverflowError):
                        attempted_healing = -1
                        effective_healing = -1
                    if (
                        target_id
                        and is_player_skill(skill_id)
                        and attempted_healing >= 0
                        and 0 <= effective_healing <= attempted_healing
                    ):
                        updates.append(
                            (
                                "heal",
                                {
                                    **self._base_update(record),
                                    "sequence": int(record.get("sequence", 0) or 0),
                                    "healer_id": actor_id,
                                    "target_id": target_id,
                                    "skill_id": skill_id,
                                    "total_healing": attempted_healing,
                                    "effective_healing": effective_healing,
                                    "overhealing": (
                                        attempted_healing - effective_healing
                                    ),
                                    "healing_source": "network_exact",
                                },
                            )
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
                updates.extend(
                    self._record_entity_hit(target_id, actor_id, args[0], record)
                )

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
            actor_id = self._parse_known_player_actor_id(args[0])
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
        elif method == "OnMsgSyncCurrentHp" and len(args) == 1:
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
                    if self._stale_player_pointer_matches_active_boss(
                        pointer, current_hp, timestamp
                    ):
                        stale_actor = int(
                            self.pointer_entities.pop(pointer, 0) or 0
                        )
                        token = self.actor_tokens.get(stale_actor, "")
                        expected_player_hp = float(
                            self.token_max_hp.get(token, 0.0) or 0.0
                        )
                        observed_player_max_hp = float(
                            self.entity_max_hp.get(stale_actor, 0.0) or 0.0
                        )
                        if (
                            expected_player_hp <= 0
                            and 0
                            < observed_player_max_hp
                            < BOSS_POINTER_HIGH_HP_FLOOR
                        ):
                            expected_player_hp = observed_player_max_hp
                        suspicious_hp_floor = (
                            expected_player_hp * 4
                            if expected_player_hp > 0
                            else BOSS_POINTER_HIGH_HP_FLOOR
                        )
                        for values, times in (
                            (self.entity_current_hp, self.entity_current_hp_time),
                            (self.entity_max_hp, self.entity_max_hp_time),
                        ):
                            if float(
                                values.get(stale_actor, 0.0) or 0.0
                            ) > suspicious_hp_floor:
                                values.pop(stale_actor, None)
                                times.pop(stale_actor, None)
                        self.pointer_state.pop(pointer, None)
                        updates.extend(
                            self._bind_pointer(
                                pointer,
                                int(self.active_boss_entity_id or 0),
                                record,
                            )
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
                                self._discard_hit_correlations(
                                    pointer, entity_id, timestamp
                                )
                                if self._boss_full_hp_reset_candidate(
                                    entity_id, current_hp
                                ):
                                    updates.append(
                                        self._boss_full_hp_reset_update(
                                            entity_id, current_hp, record
                                        )
                                    )
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
                                self._discard_hit_correlations(
                                    pointer, entity_id, timestamp
                                )
                            if accept_update:
                                previous_hp = self.entity_current_hp.get(entity_id)
                                updates.extend(
                                    self._inferred_team_damage_updates(
                                        pointer,
                                        entity_id,
                                        previous_hp,
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
        elif method == "OnMsgSyncCurrentMaxHp" and len(args) == 2:
            values: dict[str, float] = {}
            try:
                current_hp = max(0.0, float(args[0]))
                max_hp = max(0.0, float(args[1]))
                if (
                    self._valid_hp_value(current_hp)
                    and self._valid_hp_value(max_hp, maximum=True)
                    and current_hp <= max_hp * 1.02
                ):
                    values["current_hp"] = current_hp
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
                    if "max_hp" in values:
                        known_max_hp = float(
                            self.entity_max_hp.get(entity_id, 0) or 0
                        )
                        incoming_max_hp = float(values.get("max_hp", 0) or 0)
                        material_change = bool(
                            known_max_hp > 0
                            and incoming_max_hp > 0
                            and abs(incoming_max_hp - known_max_hp)
                            > max(100_000.0, known_max_hp * 0.005)
                        )
                        if material_change:
                            self.active_boss_hp_epoch = timestamp
                            self.pending_target_hits.pop(pointer, None)
                            self.pending_entity_hits.pop(entity_id, None)
                            self.pending_exact_damage.pop(entity_id, None)
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
                        self._discard_hit_correlations(
                            pointer, entity_id, timestamp
                        )
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
                        self._discard_hit_correlations(
                            pointer, entity_id, timestamp
                        )
                    if accept_update and current_hp is not None:
                        previous_hp = self.entity_current_hp.get(entity_id)
                        updates.extend(
                            self._inferred_team_damage_updates(
                                pointer,
                                entity_id,
                                previous_hp,
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
                            self.entity_max_hp_time.setdefault(entity_id, timestamp)
                        updates.extend(self._team_hp_bindings(record))
        elif method == "OnMsgSyncDirtyFightAttributes" and args:
            timestamp = int(record.get("filetime_100ns", 0) or 0)
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
                        self.entity_max_hp_time.setdefault(entity_id, timestamp)
                        updates.extend(self._team_hp_bindings(record))
        elif method == "OnMsgEntityDead":
            timestamp = int(record.get("filetime_100ns", 0) or 0)
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
                updates.append(
                    (
                        "combat_state",
                        {
                            **self._base_update(record),
                            "entity_id": entity_id,
                            "in_combat": False,
                        },
                    )
                )
                self.entity_current_hp[entity_id] = 0.0
                self.entity_current_hp_time[entity_id] = timestamp
                if entity_id == self.active_boss_entity_id:
                    self._release_active_boss(entity_id)

        if method == "RetGetServerLevelInfo" and args:
            try:
                level = int(args[0])
            except (TypeError, ValueError, OverflowError):
                level = 0
            if 1 <= level <= 200:
                self.server_level = level
                if self.self_id:
                    update = self._profile_update(self.self_id, record, level=level)
                    if update:
                        updates.append(update)
                for entity_id in sorted(self.confirmed_boss_entities):
                    try:
                        known_level = int(
                            self.entity_profiles.get(entity_id, {}).get("level", 0)
                            or 0
                        )
                    except (TypeError, ValueError, OverflowError):
                        known_level = 0
                    if 1 <= known_level <= 200:
                        continue
                    update = self._profile_update(entity_id, record, level=level)
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
