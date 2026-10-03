"""Independent, passive PVP counters. No game access, RPCs or PVE mutations.

Only exact parsed damage between identified players and involving the local
player enters the PvP counters. Hunter City dragon damage is kept separately.
Death attribution uses the source token in EntityDead,
never last-hit timing. A duel result records a win/loss without requiring an
EntityDead callback. Duels have zero assists; 6V6/12V12 use the verified
settlement assist counter when present.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timezone
from array import array
import time

from network_state import (
    SCENE_TRANSITION_METHODS,
    normalize_network_skill_id,
    parse_combat_amount,
)
from combat_statistics import fields, pairs


WINDOWS_EPOCH = 116_444_736_000_000_000
IDLE_TIMEOUT_NS = 8_000_000_000
PENDING_LIMIT = 512
PENDING_TEAM_EVENT_LIMIT = 4096
SEEN_LIMIT = 8192
# The server can flush the final hit notifications after the local duel result
# callback.  Keep that bounded tail, but never let a later match reopen the
# completed session.
DUEL_LATE_EXACT_DAMAGE_GRACE_NS = 10_000_000_000
# A single hit may be represented by both DamageSync and BeatenSyncV2.  The
# parser already deduplicates the native/network pair; this small PVP-only
# mirror prevents the newly accepted exact BeatenSync amount from being added
# a second time when a matching DamageSync is also present.
PVP_CROSS_SOURCE_DEDUP_WINDOW_NS = 50_000_000
PVP_CROSS_SOURCE_DEDUP_LIMIT = 1024
HUNTER_CITY_MAP_IDS = frozenset({5_200_131, 5_200_167})
HUNTER_DRAGON_TEMPLATE_IDS = frozenset({
    7_100_625, 7_100_628, 7_115_040, 7_115_041, 7_115_042,
})
# A PvP league roster is independent of the map that delivered it.  The
# server can expose fewer members for a particular mode, but the shared
# roster shape always has at most five raids with five six-person subgroups
# (30 members per raid, 150 members in total).
PVP_MAX_RAIDS = 5
PVP_MAX_MEMBERS_PER_RAID = 30
PVP_MAX_LEAGUE_MEMBERS = PVP_MAX_RAIDS * PVP_MAX_MEMBERS_PER_RAID
PVP_MAX_SUBGROUPS = 5
# Both codes are confirmed by the consecutive win/loss capture on 2026-09-18.
# Do not guess a loss for 0 (or any other unverified result code).
DUEL_RESULT_LABELS = {1: '胜利', 2: '失败'}
PVP_CONTROL_METHODS = frozenset({
    'OnMsgSyncBattleType', 'OnMsgIndividualPVP', 'RetIndividualPVP',
    'OnMsgIndividualPVPResponse', 'OnMsgIndividualPVPState',
    'OnMsgIndividualPVPResult', 'OnMsgIndividualPVPLeave',
    'OnMsgAddFightRelationship', 'OnMsgDelFightRelationship',
    'OnMsgSyncTeamPVPInfo', 'OnMsgTeamPVPSettlement',
    'RetGetTeamArenaBattleInfo', 'OnMsgSyncLeagueInfo',
    'OnMsgRefreshLeagueGroupInfo',
})
PVP_LIFE_METHODS = frozenset({'OnMsgEntityDead', 'OnMsgEntityRelive'})
PVP_TEAM_MEMBER_METHODS = frozenset({
    'OnSyncTeamGroupPropsForceRefresh', 'OnUpdateTeamGroupMemberProps',
})
OBSERVED_METHODS = PVP_CONTROL_METHODS | PVP_LIFE_METHODS | SCENE_TRANSITION_METHODS | {
    'OnMsgDamageSyncV2', 'KAPI_HandleDamageSyncV2', 'OnMsgBeatenSyncV2', 'NpcapEntityCreated',
    'OnMsgCastSkillNew', 'RetCastSkillSuccessNew',
    'OnMsgSyncFightMode',
    'OnMsgRefreshSceneObjects',
    'RetOtherRoleShapeData',
} | PVP_TEAM_MEMBER_METHODS


def _integer(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def skill_has_activity(value: object) -> bool:
    """Return whether a PvP skill row has observable use evidence."""

    if not isinstance(value, dict):
        return False
    return any(
        _integer(value.get(field)) > 0
        for field in ("damage", "hits", "casts")
    )


def _append_timestamp(row: dict, field: str, stamp: object) -> None:
    """Keep long combat timelines in compact native integer buffers."""

    values = row.get(field)
    if isinstance(values, array):
        buffer = values
    else:
        buffer = array(
            "Q",
            (max(0, int(value)) for value in (values or ()) if _integer(value) >= 0),
        )
    try:
        buffer.append(max(0, int(stamp)))
    except (TypeError, ValueError, OverflowError):
        return
    row[field] = buffer


def _skill_output(value: dict) -> dict:
    result = dict(value)
    for field in ("hit_timestamps_ns", "cast_timestamps_ns"):
        if field in result:
            result[field] = list(result[field])
    return result


def _optional_integer(value):
    """Parse an optional wire counter without turning absence into zero.

    Arena settlement packets legitimately omit some counters for spectators,
    bots, or roles whose source stream was not available.  A literal numeric
    zero is still a real value and must remain distinguishable from a missing
    field so the UI can render ``--`` for unknown data.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return None


def _profession_id(value):
    profession = _integer(value)
    return profession if 1_000_000 <= profession <= 1_999_999 else 0


def pvp_bot_evidence(value):
    """Use the arena's explicit bot template, never a player's display name."""

    return _integer(value.get('ai_template_id')) > 0


def _league_roster(arguments):
    """Read the server's numbered raid and subgroup table."""

    if not isinstance(arguments, list) or len(arguments) != 1:
        return None
    root = fields(arguments[0])
    raids = root.get(0)
    if not isinstance(raids, dict):
        return None
    result = {}
    for raw_raid_id, raw_raid in pairs(raids):
        if len(result) >= PVP_MAX_RAIDS:
            break
        raid_id = _integer(raw_raid_id)
        raid = fields(raw_raid)
        subgroups = raid.get(17)
        if raid_id <= 0 or not isinstance(subgroups, dict):
            continue
        groups = {}
        raid_tokens = set()
        raid_member_count = 0
        for _subgroup_id, raw_group in pairs(subgroups):
            group = fields(raw_group)
            number = _integer(group.get(0))
            raw_members = group.get(1)
            if not 1 <= number <= PVP_MAX_SUBGROUPS or not isinstance(raw_members, list):
                continue
            members = []
            for raw_member in raw_members:
                if raid_member_count >= PVP_MAX_MEMBERS_PER_RAID:
                    break
                member = fields(raw_member)
                token = member.get(2)
                if (
                    not isinstance(token, str)
                    or not 8 <= len(token) <= 128
                    or token in raid_tokens
                ):
                    continue
                name = str(member.get(5) or '').strip()
                ai_template = _integer(member.get(3))
                members.append({
                    'user_token': token,
                    'name': name,
                    'profession_id': _profession_id(member.get(8)),
                    'level': _integer(member.get(9)),
                    'extraordinary_rating': _integer(member.get(27)) or None,
                    'ai_template_id': ai_template or None,
                    'is_ai': ai_template > 0,
                    'subgroup': number,
                })
                raid_tokens.add(token)
                raid_member_count += 1
            if members:
                groups[number] = members
        if groups:
            result[raid_id] = {
                'number': _integer(raid.get(6)),
                'groups': groups,
            }
    return result or None


def _league_group_refresh(arguments):
    if not isinstance(arguments, list) or len(arguments) != 1:
        return None
    raid = fields(arguments[0])
    raid_id = _integer(raid.get(7) or raid.get(16))
    if raid_id <= 0 or not isinstance(raid.get(17), dict):
        return None
    return _league_roster([{0: {raid_id: raid}}])


def _team_pvp_field(value, key):
    if key in value:
        return value[key]
    return value.get(str(key))


def _team_pvp_members(arguments):
    """Return only members from the verified TeamPVPInfo wire shape."""

    if not isinstance(arguments, list) or not 2 <= len(arguments) <= 13:
        return None
    count = arguments[0]
    if isinstance(count, bool) or not isinstance(count, int) or count != len(arguments) - 1:
        return None
    result = []
    seen = set()
    for value in arguments[1:]:
        if not isinstance(value, dict):
            return None
        token = _team_pvp_field(value, 0)
        profession = _team_pvp_field(value, 2)
        level = _team_pvp_field(value, 3)
        name = _team_pvp_field(value, 4)
        avatar_template = _team_pvp_field(value, 6)
        if (
            not isinstance(token, str)
            or not 8 <= len(token) <= 128
            or not token.isascii()
            or any(char.isspace() for char in token)
            or token in seen
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
            return None
        seen.add(token)
        result.append({
            'user_token': token,
            'name': name.strip(),
            'profession_id': profession,
            'level': level,
            'avatar_template_id': avatar_template,
            # The captured 3V3 wire shape uses field 5 == 1 for the opposing
            # side.  Its absence is the friendly side.  Keep this exact bit;
            # do not infer a side from names, professions or damage direction.
            'side': 'enemy' if _integer(_team_pvp_field(value, 5)) == 1 else 'ally',
            'entity_type': 'Player',
            'is_ai': pvp_bot_evidence({'name': name}),
        })
    return result


def _team_pvp_roster_teams(arguments):
    """Normalize the verified complete arena roster carried by method 824."""

    if not isinstance(arguments, list) or len(arguments) != 1:
        return None
    raw_teams = arguments[0]
    if not isinstance(raw_teams, dict) or not 2 <= len(raw_teams) <= 4:
        return None
    teams = OrderedDict()
    seen = set()
    team_size = None
    for raw_team_id, raw_team in raw_teams.items():
        team_id = _integer(raw_team_id)
        if team_id <= 0 or team_id in teams or not isinstance(raw_team, dict):
            return None
        members = _team_pvp_field(raw_team, 0)
        if not isinstance(members, list) or not 1 <= len(members) <= 60:
            return None
        if team_size is None:
            team_size = len(members)
        elif len(raw_teams) == 2 and len(members) != team_size:
            return None
        normalized = []
        for raw_member in members:
            if not isinstance(raw_member, dict):
                return None
            token = _team_pvp_field(raw_member, 0)
            name = _team_pvp_field(raw_member, 1)
            profession = _team_pvp_field(raw_member, 3)
            raw_ai_template = _team_pvp_field(raw_member, 2)
            ai_template = _integer(raw_ai_template)
            if (
                not isinstance(token, str)
                or not 8 <= len(token) <= 128
                or not token.isascii()
                or any(char.isspace() for char in token)
                or token in seen
                or not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 64
                or _profession_id(profession) <= 0
                or (
                    raw_ai_template is not None
                    and (
                        isinstance(raw_ai_template, bool)
                        or not isinstance(raw_ai_template, int)
                        or not 1 <= raw_ai_template <= 99_999_999
                    )
                )
            ):
                return None
            seen.add(token)
            normalized.append({
                'user_token': token,
                'name': name.strip(),
                'profession_id': profession,
                'ai_template_id': ai_template or None,
                'is_ai': ai_template > 0,
                'entity_type': 'Player',
            })
        teams[team_id] = normalized
    return teams


def _team_pvp_settlement(arguments, self_token=''):
    """Normalize the verified method-798 result around the local team."""

    if not isinstance(arguments, list) or len(arguments) != 2:
        return None
    raw_teams, server_stamp = arguments
    if not isinstance(raw_teams, dict) or not 2 <= len(raw_teams) <= 4:
        return None
    teams = []
    team_size = None
    seen = set()
    own_index = None

    def sparse_counter(value):
        return 0 if value is None else value if value >= 0 else None

    for raw_team_id, raw_team in raw_teams.items():
        team_id = _integer(raw_team_id)
        if team_id <= 0 or not isinstance(raw_team, dict):
            return None
        raw_members = _team_pvp_field(raw_team, 4)
        if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= 60:
            return None
        if team_size is None:
            team_size = len(raw_members)
        elif len(raw_teams) == 2 and len(raw_members) != team_size:
            return None
        members = []
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                return None
            token = _team_pvp_field(raw_member, 0)
            name = _team_pvp_field(raw_member, 4)
            profession = _profession_id(_team_pvp_field(raw_member, 2))
            level = _integer(_team_pvp_field(raw_member, 3))
            if (
                not isinstance(token, str)
                or not 8 <= len(token) <= 128
                or not token.isascii()
                or any(char.isspace() for char in token)
                or token in seen
                or not isinstance(name, str)
                or not name.strip()
                or profession <= 0
                or not 1 <= level <= 1_000
            ):
                return None
            seen.add(token)
            raw_rating = _optional_integer(_team_pvp_field(raw_member, 14))
            raw_damage = _optional_integer(_team_pvp_field(raw_member, 15))
            raw_healing = _optional_integer(_team_pvp_field(raw_member, 16))
            raw_taken = _optional_integer(_team_pvp_field(raw_member, 17))
            raw_kills = _optional_integer(_team_pvp_field(raw_member, 18))
            raw_deaths = _optional_integer(_team_pvp_field(raw_member, 19))
            raw_assists = (
                _optional_integer(_team_pvp_field(raw_member, 23))
                if team_size in (6, 12, 60)
                else None
            )
            members.append({
                'character_id': token,
                'user_token': token,
                'name': name.strip(),
                'profession_id': profession,
                'level': level,
                'extraordinary_rating': (
                    raw_rating if raw_rating is not None and raw_rating > 0 else None
                ),
                'damage': sparse_counter(raw_damage),
                'healing': sparse_counter(raw_healing),
                'taken': sparse_counter(raw_taken),
                'kills': sparse_counter(raw_kills),
                'assists': raw_assists if raw_assists is None or raw_assists >= 0 else None,
                'deaths': sparse_counter(raw_deaths),
                'is_ai': pvp_bot_evidence({'name': name}),
                'statistics_authoritative': True,
                'metrics_scope': 'authoritative',
                # Method 798 carries aggregate counters, not per-skill rows.
                'skills_scope': 'identity_only',
            })
        if self_token and any(row['character_id'] == self_token for row in members):
            own_index = len(teams)
        raw_winner = _optional_integer(_team_pvp_field(raw_team, 0))
        teams.append({
            'team_id': team_id,
            # Keep an omitted result distinct from an explicit loss (0).
            'winner': None if raw_winner is None else raw_winner == 1,
            'started_at_seconds': _integer(_team_pvp_field(raw_team, 2)),
            'ended_at_seconds': _integer(_team_pvp_field(raw_team, 3)),
            'members': members,
        })
    if own_index is None:
        return None
    # In captured 6V6/12V12 settlements, field 23 counts assists: each
    # player's kills plus this value never exceeds opposing team deaths, and
    # zero entries are omitted from the same sparse counter map.  Leave 3V3
    # unchanged until its rules expose a verified assist counter.
    if team_size in (6, 12, 60) and any(
        member['assists'] is not None
        for team in teams for member in team['members']
    ):
        for team in teams:
            for member in team['members']:
                if member['assists'] is None:
                    member['assists'] = 0
    # The final arena payload commonly carries ``0: 1`` only on the winning
    # team and omits field 0 entirely on the losing team.  Once exactly one
    # winner is explicit, the other side is an authoritative loss; keeping it
    # as ``None`` makes every losing 3V3/6V6/12V12 record appear "unknown".
    explicit_winners = [
        index for index, team in enumerate(teams)
        if team.get('winner') is True
    ]
    if len(explicit_winners) == 1:
        for index, team in enumerate(teams):
            if index != explicit_winners[0] and team.get('winner') is None:
                team['winner'] = False
    own = teams[own_index]
    team_size = len(own['members'])
    enemies = [team for index, team in enumerate(teams) if index != own_index]
    for row in own['members']:
        row['side'] = 'ally'
        row['is_self'] = row['character_id'] == self_token
    enemy_members = []
    for enemy in enemies:
        for row in enemy['members']:
            row['side'] = 'enemy'
            row['is_self'] = False
            enemy_members.append(row)
    own_result = (
        '胜利' if own['winner'] is True
        else '失败' if own['winner'] is False
        else '未知'
    )
    return {
        'allies': own['members'],
        'enemies': enemy_members,
        'team_size': team_size,
        'result': own_result,
        'started_at_seconds': own['started_at_seconds'],
        'ended_at_seconds': own['ended_at_seconds'],
        'server_timestamp': _integer(server_stamp),
        'authoritative': True,
        'finished': True,
    }


def timestamp_ns(record):
    stamp = _integer(record.get('capture_timestamp_ns'))
    if stamp > 0:
        return stamp
    return max(0, (_integer(record.get('filetime_100ns')) - WINDOWS_EPOCH) * 100)


def _beaten_endpoints(record, arguments):
    """Return (attacker, target) for both observed BeatenSync layouts.

    The callback is dispatched on the affected actor.  Before the 2026-09-19
    game update that actor appeared in slot 1; current TeamPvpSpace traffic
    places it in slot 0.  The RPC owner therefore selects the layout without
    guessing from names, skills or amounts.
    """

    if not isinstance(arguments, list) or len(arguments) < 2:
        return 0, 0
    first, second = _integer(arguments[0]), _integer(arguments[1])
    owner = _integer(record.get('network_entity_id') or record.get('script_entity'))
    if owner > 0 and owner == first and owner != second:
        return second, first
    return first, second


def shape_equipment_snapshot(profile, stamp):
    """Keep only verified shape fields; unknown attributes are not inferred."""
    equipment = []
    try:
        items = pairs(profile.get(11, {}))[:64]
    except ValueError:
        return None
    for slot, raw_item in items:
        try:
            item = fields(raw_item)
        except ValueError:
            continue
        item_id = _integer(item.get(2))
        if item_id > 0:
            equipment.append({'slot': _integer(slot), 'item_id': item_id})
    if not equipment:
        return None
    return {'captured_at_ns': stamp,
            'captured_at': datetime.fromtimestamp(stamp / 1e9, timezone.utc).isoformat(),
            'extraordinary_rating': profile.get(19),
            'equipment_score': None, 'equipment': equipment,
            'gems': None, 'attributes': None, 'sets': None,
            'source': 'RetOtherRoleShapeData', 'partial': True}


def shape_appearance(profile):
    """Return the verified selected portrait and frame IDs from field 25."""

    try:
        appearance = fields(profile.get(25, {}))
    except ValueError:
        return 0, 0
    return _integer(appearance.get(0)), _integer(appearance.get(1))


def parser_observation(parser, record, updates):
    """Detach identities at parse time, including deferred scoped records.

    The raw life callback is observed BEFORE LocalRole/party-only gating: its
    recipient is the affected wire player, and its token is the death source
    (or reviver for Relive). Those two identities must never be interchanged.
    """
    method = str(record.get('method') or '')
    # Scene refresh and Space creation are both authoritative map evidence.
    # Remember it on the passive parser so subsequent observations do not
    # resurrect the old wire template when a refresh precedes Space creation.
    live_self_token = getattr(parser, 'self_token', None)
    previous_self_token = getattr(parser, 'pvp_identity_token', None)
    if (any(kind == 'identity_session_reset' for kind, _value in updates)
            or (previous_self_token and live_self_token and previous_self_token != live_self_token)):
        parser.pvp_map_id = None
        parser.pvp_instance_id = None
    if live_self_token:
        parser.pvp_identity_token = live_self_token
    for kind, value in updates:
        if kind == 'scene':
            scene_id = _integer(value.get('scene_id'))
            previous_map = getattr(parser, 'pvp_map_id', getattr(parser, 'wire_map_id', None))
            if value.get('transition') or scene_id <= 0:
                parser.pvp_map_id = None
                parser.pvp_instance_id = None
            else:
                if scene_id != previous_map:
                    # A scene refresh can precede Space creation. Do not
                    # attach the previous map's wire instance to the new map.
                    parser.pvp_instance_id = None
                parser.pvp_map_id = scene_id
        elif kind == 'instance_context':
            parser.pvp_map_id = _integer(value.get('map_id')) or None
            parser.pvp_instance_id = value.get('instance_id')
    if method in SCENE_TRANSITION_METHODS:
        parser.pvp_map_id = None
        parser.pvp_instance_id = None
    metadata = [(k, v) for k, v in updates if k in ('profile', 'identity', 'name')]
    events = [v for k, v in updates if k == 'event'
              and v.get('provisional_damage') is not True
              and v.get('damage_source') in ('network_exact', 'native_exact')]
    if method not in OBSERVED_METHODS and not metadata:
        return None
    stamp = timestamp_ns(record)
    if stamp <= 0:
        return None
    self_id = _integer(getattr(parser, 'native_self_id', None)
                       or getattr(parser, 'self_id', None))
    owner = _integer(record.get('network_entity_id'))
    args = record.get('decoded_arguments', [])
    involved = {self_id, owner}
    if method == 'OnMsgBeatenSyncV2' and isinstance(args, list) and len(args) >= 2:
        # BeatenSync is dispatched on the affected actor.  The endpoint order
        # changed in current TeamPvpSpace traffic, so resolve it against the
        # RPC owner.  The 2026-09-19 3V3 capture proves this independently: the
        # local DamageSync stream totals 714 outgoing; reading the current
        # BeatenSync layout as the legacy layout inflates it to 28,082.
        # Three-field (identity-only) packets remain metadata and cannot turn
        # into fabricated damage.
        involved.update((_integer(args[0]), _integer(args[1])))
        attacker, target = _beaten_endpoints(record, args)
        if self_id > 0 and self_id in (attacker, target) and attacker > 0 and target > 0 and attacker != target and len(args) >= 5:
            raw_damage = parse_combat_amount(args[3])
            reported_damage = parse_combat_amount(args[4])
            # Zero effective damage is authoritative (shield/immunity), not
            # a missing field. Never substitute the pre-mitigation amount.
            damage = reported_damage
            if damage is not None:
                skill_id = normalize_network_skill_id(args[2])
                events.append({
                    **{key: record[key] for key in (
                        'capture_event_id', 'sequence', 'filetime_100ns',
                    ) if key in record},
                    'function': 'OnMsgBeatenSyncV2/network',
                    'attacker_id': attacker,
                    'target_id': target,
                    'skill_id': skill_id,
                    'arg4_u64': _integer(args[2]),
                    'raw_damage': raw_damage or damage,
                    'damage': damage,
                    'arg7_i32': raw_damage or damage,
                    'arg9_i32': reported_damage or damage,
                    'critical': None,
                    'penetrating': None,
                    'damage_source': 'network_exact',
                    'source_method': 'OnMsgBeatenSyncV2',
                    'beaten_exact': True,
                    'provisional_damage': False,
                })
    for event in events:
        involved.update((_integer(event.get('attacker_id')), _integer(event.get('target_id'))))
    for _kind, value in metadata:
        involved.add(_integer(value.get('entity_id', value.get('actor_id'))))
    if method in PVP_LIFE_METHODS and isinstance(args, list) and len(args) > 1:
        source_index = 1 if method == 'OnMsgEntityDead' else 3
        if len(args) > source_index and isinstance(args[source_index], str):
            involved.add(_integer(getattr(parser, 'token_actors', {}).get(args[source_index])))
    profiles = []
    classes = getattr(parser, 'wire_classes', {})
    for entity in involved - {0}:
        profile = dict(getattr(parser, 'entity_profiles', {}).get(entity, {}))
        if classes.get(entity) == 'AvatarActor':
            profile['entity_type'] = 'Player'
        elif classes.get(entity) == 'NpcActor' or parser._is_confirmed_non_player_actor(entity):
            profile['entity_type'] = 'NPC'
            template_id = _integer(
                getattr(parser, 'entity_template_ids', {}).get(entity)
            )
            if template_id:
                profile['template_id'] = template_id
        token = (getattr(parser, 'wire_entity_tokens', {}).get(entity)
                 or getattr(parser, 'actor_tokens', {}).get(entity))
        if token:
            profile['user_token'] = token
        if profile:
            profiles.append({'entity_id': entity, **profile})
    if method == 'RetOtherRoleShapeData' and isinstance(args, list) and args:
        # The verified shape profile has a stable token and fields 0/2/19.
        # Consume naturally received replies only; never request opponents.
        for token, value in pairs(args[0]):
            if not isinstance(token, str):
                continue
            profile = fields(value)
            if not isinstance(profile.get(0), str):
                continue
            actor = _integer(getattr(parser, 'token_actors', {}).get(token))
            avatar_id, avatar_frame_id = shape_appearance(profile)
            profiles.append({'entity_id': actor, 'user_token': token,
                             'name': profile[0], 'profession_id': profile.get(2),
                             'level': profile.get(4),
                             'extraordinary_rating': profile.get(19),
                             'avatar_id': avatar_id,
                             'avatar_frame_id': avatar_frame_id,
                             'equipment_snapshot': shape_equipment_snapshot(profile, stamp)})
    # No large equipment payloads or mutable parser dictionaries cross threads.
    raw = {key: record[key] for key in (
        'method', 'network_entity_id', 'capture_event_id', 'sequence',
        'filetime_100ns', 'capture_source', 'npcap_method_scope', 'npcap_recipient',
    ) if key in record}
    if method in OBSERVED_METHODS and method != 'RetOtherRoleShapeData':
        raw['decoded_arguments'] = args
    context_self_token = getattr(parser, 'self_token', None)
    if (
        not context_self_token
        and self_id > 0
        and owner == self_id
        and (record.get('npcap_method_scope') == 'local_role'
             or method in PVP_CONTROL_METHODS)
    ):
        # A late-start Npcap session can prove the local runtime actor before
        # another roster packet repeats its token.  The worker only supplies
        # remembered_self_token for the exact game PID being captured, so the
        # same-process token is valid PVP identity evidence for this
        # local-role callback.  Keep the fallback inside the detached PVP
        # observation; it must not weaken the formal PVE identity gate.
        context_self_token = getattr(parser, 'remembered_self_token', None)
    pvp_context = {
        field: getattr(parser, 'pvp_' + field, None)
        for field in ('series_id', 'round_id', 'event_id', 'war_id', 'source_battle_id', 'mode_id')
        if getattr(parser, 'pvp_' + field, None) not in (None, '')
    }
    observation = {
        'record': raw, 'timestamp_ns': stamp,
        'context': {'self_id': self_id, 'self_token': context_self_token,
                    'instance_id': getattr(parser, 'pvp_instance_id', getattr(parser, 'wire_instance_id', None)),
                    'map_id': getattr(parser, 'pvp_map_id', getattr(parser, 'wire_map_id', None)),
                    'profiles': profiles, **pvp_context},
        'updates': metadata, 'events': events,
    }
    if method == 'OnMsgTeamPVPSettlement':
        settlement = _team_pvp_settlement(args, context_self_token or '')
        if settlement is not None:
            observation['team_pvp_settlement'] = settlement
    return deepcopy(observation)


class PvpHudTotals:
    """In-memory window totals, independent of immutable single-match history.

    Keep only merged counters, not a growing list of matches. Stable player
    tokens join opponents across rounds; unbound runtime IDs stay scene-local.
    """

    def __init__(self):
        self.state = None

    @staticmethod
    def _merge(left, right):
        states = [state for state in (left, right) if state is not None]
        result = dict(right)
        known = bool(states) and all(
            state.get('total_damage') != '--' and state.get('damage') is not None
            for state in states
        )
        damage_values = [state.get('damage') for state in states]
        taken_values = [state.get('taken') for state in states]
        healing_values = [state.get('healing') for state in states]
        result['damage'] = (
            sum(int(value or 0) for value in damage_values)
            if all(value is not None for value in damage_values)
            else None
        )
        result['taken'] = (
            sum(int(value or 0) for value in taken_values)
            if all(value is not None for value in taken_values)
            else None
        )
        result['healing'] = (
            sum(int(value or 0) for value in healing_values)
            if all(value is not None for value in healing_values)
            else None
        )
        result['total_damage'] = (
            f"{result['damage']:,}"
            if known and result['damage'] is not None
            else '--'
        )
        result['total_healing'] = (
            f"{result['healing']:,}"
            if known and result['healing'] is not None
            else '--'
        )
        result['healing_source_available'] = any(
            state.get('healing_source_available') for state in states
        )
        known_states = [state for state in states if state.get('total_damage') != '--']
        incoming_available = bool(known_states) and all(
            state.get('incoming_source_available') for state in known_states
        )
        result['incoming_source_available'] = incoming_available
        result['total_taken'] = (
            f"{result['taken']:,}"
            if incoming_available and result['taken'] is not None
            else '--'
        )
        for field in ('kills', 'deaths', 'assists'):
            values = [state.get(field) for state in known_states]
            result[field] = (sum(values) if values and all(isinstance(value, int) for value in values)
                             else '--')
        result['capture_complete'] = all(state.get('capture_complete', True) for state in states)
        result['elapsed_ns'] = sum(state.get('elapsed_ns', 0) for state in states)

        for direction, count_key in (('outgoing', 'kills'), ('incoming', 'defeats')):
            merged = {}
            for state in states:
                for row in state.get(direction, ()):
                    key = row.get('opponent_id') or f"entity:{row.get('actor_id', 0)}"
                    if key.startswith('entity:'):
                        key = f"session:{state['session_id']}:{key}"
                    current = merged.setdefault(key, {'opponent_id': key, 'actor_id': 0,
                                                       'name': '未知玩家', 'damage': 0,
                                                       count_key: 0, 'assists': 0})
                    for field in ('actor_id', 'name', 'profession_id', 'rating'):
                        if row.get(field) not in (None, '', 0, '未知玩家'):
                            current[field] = row[field]
                    amount = row.get('damage')
                    current['damage'] = (current['damage'] + amount
                                         if current['damage'] is not None and amount is not None
                                         else None)
                    current[count_key] += row.get(count_key, 0)
                    assists = row.get('assists')
                    current['assists'] = (current['assists'] + assists
                                          if isinstance(current['assists'], int) and isinstance(assists, int)
                                          else '--')
            total = result['damage'] if direction == 'outgoing' else result['taken']
            for row in merged.values():
                amount = row['damage']
                row['damage_text'] = f'{amount:,}' if amount is not None else '--'
                row['share_text'] = (f'{amount / total * 100:.1f}%' if total else '0.0%') if amount is not None else '--'
            result[direction] = sorted(merged.values(), key=lambda row: (
                -(row['damage'] or 0), -row[count_key], row['opponent_id']))
        for direction in ('outgoing', 'incoming'):
            field = 'skills_' + direction
            merged = {}
            for state in states:
                for skill in state.get(field, ()):
                    current = merged.setdefault(skill['skill_id'], {
                        'skill_id': skill['skill_id'], 'damage': 0, 'hits': 0,
                        'critical_hits': 0, 'max_hit': 0, 'casts': 0})
                    for counter in ('damage', 'hits', 'critical_hits', 'casts'):
                        current[counter] += skill.get(counter, 0)
                    current['max_hit'] = max(current['max_hit'], skill.get('max_hit', 0))
                    current['cast_timestamps_ns'] = sorted({
                        *current.get('cast_timestamps_ns', ()),
                        *skill.get('cast_timestamps_ns', ()),
                    })
            total = sum(skill['damage'] for skill in merged.values())
            result[field] = [dict(skill, share=skill['damage'] / total if total else 0)
                             for skill in sorted(merged.values(), key=lambda skill: (-skill['damage'], skill['skill_id']))]
        return result

    def add(self, state):
        if state.get('total_damage') != '--':
            self.state = self._merge(self.state, state)

    def snapshot(self, state):
        return self._merge(self.state, state)


class PvpTracker:
    """A duel is one session; world PVP accumulates within the current space.

    Fight-mode exit/death pauses the active clock without throwing away match
    counters. Scene/account changes and a new duel clear single-match counters.
    The optional cumulative tracker archives them for the lifetime of the HUD.
    Switching the HUD tab has no effect on either tracker.
    """

    def __init__(self, *, map_scoped=False, cumulative=False):
        # Durable map-scoped matches are not split or erased by an individual
        # duel state/result. The legacy transient tracker keeps its duel flow.
        self.map_scoped = bool(map_scoped)
        self.hud_totals = PvpHudTotals() if cumulative else None
        self.generation = 0
        self.session_id = 0
        self.reset()

    def reset(self):
        self._archive_session()
        self.self_id = 0
        self.self_token = ''
        self._known_self_max_hp = None
        self._known_self_max_hp_stamp = 0
        self.profiles = {}
        self.tokens = {}
        self.token_profiles = {}
        self.pvp_allies = OrderedDict()
        self.pvp_enemies = OrderedDict()
        self.pvp_roster_members = OrderedDict()
        self.pvp_live_party_tokens = set()
        self.pvp_full_roster_teams = OrderedDict()
        self.pvp_league_roster = {}
        self.pvp_self_avatar_template_id = 0
        self.team_size = 0
        self.instance_id = None
        self.map_id = None
        self.seen = getattr(self, 'seen', OrderedDict()) if self.hud_totals is not None else OrderedDict()
        self.pending = deque(maxlen=PENDING_LIMIT)
        self._clear_session(archive=False)

    def _archive_session(self):
        if self.hud_totals is not None and getattr(self, 'started_at_ns', None) is not None:
            stamp = self.ended_at_ns or self.paused_at_ns or self.last_activity_ns or self.started_at_ns
            self.hud_totals.add(self.session_snapshot(stamp))

    def _clear_session(self, *, archive=True):
        if archive:
            self._archive_session()
        self.session_id += 1
        self.outgoing = {}
        self.incoming = {}
        self.skills_outgoing = {}
        self.skills_incoming = {}
        self.opponent_skills_outgoing = {}
        self.opponent_skills_incoming = {}
        self.healing_by_actor = {}
        self.healing_skills = {}
        self.dragon_targets = {}
        self.team_player_stats = {}
        self.team_damage_contributions = {}
        # Exact team-arena events can arrive before TeamPVPInfo/settlement has
        # assigned both endpoints to a side.  Keep them separate from the
        # ordinary identity retry queue so a busy 6V6/12V12 does not rescan
        # hundreds of already-decoded hits on every following packet.
        self.pending_team_events = deque(maxlen=PENDING_TEAM_EVENT_LIMIT)
        self.life_outgoing = {}
        self.life_incoming = {}
        self.death_events = []
        self.dead = set()
        self.pending.clear()
        self.started_at_ns = None
        self.ended_at_ns = None
        self.active_since_ns = None
        self.elapsed_ns = 0
        self.last_activity_ns = 0
        self.paused_at_ns = None
        self.self_dead_at_ns = None
        self.duel = False
        self.duel_finished = False
        self.duel_opponent_token = ''
        self.duel_opponent_actor = 0
        self.duel_opponent_actor_authoritative = False
        self.duel_outcome = ''
        self.duel_prepare_id = None
        self.duel_start_id = None
        self.result = '等待中'
        self.module_name = ''
        self.capture_complete = True
        self.has_exact_incoming = False
        self.has_exact_healing = False
        self._duel_max_hp = None
        self._duel_max_hp_changed = False
        self._duel_last_hp = None
        self._duel_result_hp = None
        self._duel_result_taken = None
        self._pvp_damage_stream_events = {
            'network': deque(maxlen=PVP_CROSS_SOURCE_DEDUP_LIMIT),
            'native': deque(maxlen=PVP_CROSS_SOURCE_DEDUP_LIMIT),
            'beaten': deque(maxlen=PVP_CROSS_SOURCE_DEDUP_LIMIT),
        }
        self.generation += 1

    def mark_gap(self):
        self.capture_complete = False
        self.generation += 1

    def _key(self, actor):
        return self.profiles.get(actor, {}).get('user_token') or f'entity:{actor}'

    def _known_player(self, actor):
        if actor == self.self_id and actor > 0:
            return True
        profile = self.profiles.get(actor, {})
        if profile.get('entity_type') != 'Player':
            return False
        if profile.get('is_ai') is not True:
            return True
        token = str(profile.get('user_token') or '').strip()
        return bool(self._team_side(actor, token))

    def _non_player(self, actor):
        profile = self.profiles.get(actor, {})
        if profile.get('entity_type') in ('NPC', 'Npc', 'Boss', 'Monster', 'TrainingDummy'):
            return True
        token = str(profile.get('user_token') or '').strip()
        return profile.get('is_ai') is True and not self._team_side(actor, token)

    def _set_identity(self, actor, token=''):
        if actor <= 0:
            return
        token = str(token or '').strip()
        if self.self_token and token and token != self.self_token:
            self.reset()
        elif self.self_id and actor != self.self_id and not (token and token == self.self_token):
            self.reset()
        changed = self.self_id != actor or bool(token and token != self.self_token)
        self.self_id = actor
        if token:
            self.self_token = token
        self._profile({'entity_id': actor, 'entity_type': 'Player', 'user_token': token})
        if self.pvp_full_roster_teams:
            self._rebuild_pvp_full_roster()
        cached_self = self.pvp_roster_members.get(self.self_token, {})
        cached_template = _integer(cached_self.get('avatar_template_id'))
        if cached_template and cached_template != self.pvp_self_avatar_template_id:
            self.pvp_self_avatar_template_id = cached_template
            self._rebuild_pvp_template_roster()
        if changed:
            self.generation += 1

    def rotate_scene(self, map_id=None, instance_id=None):
        """Clear runtime endpoints without dropping the window aggregate."""
        actor, token = self.self_id, self.self_token
        profile = deepcopy(self.profiles.get(actor, {}))
        self.reset()
        if actor:
            self._set_identity(actor, token)
            if profile:
                self._profile({'entity_id': actor, **profile})
        self.map_id, self.instance_id = map_id, instance_id

    def clear_window_totals(self):
        """Start a new HUD total without touching already-saved history."""
        if self.hud_totals is not None:
            self.hud_totals = PvpHudTotals()
        self._clear_session(archive=False)

    def _remember_duel_opponent(self, token='', name=''):
        token = str(token or '').strip()
        if not token or token == self.self_token:
            return
        changed = token != self.duel_opponent_token
        self.duel_opponent_token = token
        profile = {'entity_id': self.duel_opponent_actor,
                   'entity_type': 'Player', 'user_token': token}
        if str(name or '').strip():
            profile['name'] = str(name).strip()
        self._profile(profile)
        if changed:
            self.generation += 1

    def _bind_duel_opponent_actor(self, actor, *, authoritative=False):
        """Bind the sole duel peer token to its runtime damage endpoint.

        IndividualPVPResponse/FightRelationship identifies the peer by stable
        token, while DamageSync identifies it by runtime entity ID.  The game
        does not necessarily repeat an AvatarActor creation between those two
        messages.  During a verified two-player duel, their non-self endpoint
        is therefore the authoritative bridge between both identities.
        """

        actor = _integer(actor)
        if not self.duel or actor <= 0 or actor == self.self_id:
            return
        if self.duel_opponent_actor and actor != self.duel_opponent_actor:
            # A cast observed during duel preparation can belong to the
            # opponent's old scene actor.  The arena then replaces it with a
            # combat actor before the first exact Beaten/Damage callback.
            # Permit that one evidence upgrade, but never let later unrelated
            # damage endpoints make the verified 1V1 peer oscillate.
            if (
                not authoritative
                or self.duel_opponent_actor_authoritative
                or self.duel_finished
            ):
                return
        if self._non_player(actor):
            return
        changed = actor != self.duel_opponent_actor
        self.duel_opponent_actor = actor
        if authoritative:
            self.duel_opponent_actor_authoritative = True
        profile = {'entity_id': actor, 'entity_type': 'Player'}
        if self.duel_opponent_token:
            profile['user_token'] = self.duel_opponent_token
        self._profile(profile)
        if changed:
            self.generation += 1

    def _duel_opponent_key(self):
        if self.duel_opponent_token:
            return self.duel_opponent_token
        if self.duel_opponent_actor:
            return self._key(self.duel_opponent_actor)
        return 'duel:opponent'

    def _team_side(self, actor=0, token=''):
        token = str(token or '').strip()
        if not token and actor:
            token = str(self.profiles.get(actor, {}).get('user_token') or '').strip()
        if (actor and actor == self.self_id) or (token and token == self.self_token):
            return 'ally'
        # A formal duel has one verified peer even when the normal arena
        # roster callbacks are absent.  The duel binding is stronger than a
        # stale party-side copy of the same actor.
        if self.duel and (
            (actor and actor == self.duel_opponent_actor)
            or (token and token == self.duel_opponent_token)
        ):
            return 'enemy'
        if token in self.pvp_allies:
            return 'ally'
        if token in self.pvp_enemies:
            return 'enemy'
        return ''

    @staticmethod
    def _merge_team_skill(target, source):
        for field in ('damage', 'hits', 'critical_hits', 'casts'):
            if source.get(field) is not None:
                target[field] = int(target.get(field, 0) or 0) + int(source.get(field, 0) or 0)
        target['max_hit'] = max(
            int(target.get('max_hit', 0) or 0),
            int(source.get('max_hit', 0) or 0),
        )
        for field in ('hit_timestamps_ns', 'cast_timestamps_ns'):
            values = [int(value) for value in source.get(field, ()) if _integer(value) > 0]
            if values:
                target[field] = array("Q", sorted({*target.get(field, ()), *values}))

    def _merge_team_stat_key(self, old_key, new_key, actor=0):
        if not old_key or old_key == new_key:
            return
        old = self.team_player_stats.pop(old_key, None)
        if old is not None:
            current = self.team_player_stats.setdefault(new_key, {})
            for field in ('damage', 'healing', 'taken', 'kills', 'assists', 'deaths'):
                value = old.get(field)
                if value is not None:
                    current[field] = int(current.get(field, 0) or 0) + int(value or 0)
            for field in ('actor_id', 'user_token', 'name', 'profession_id', 'level',
                          'extraordinary_rating', 'avatar_id', 'avatar_frame_id',
                          'equipment_snapshot', 'side'):
                if old.get(field) not in (None, '', 0):
                    current[field] = deepcopy(old[field])
            current['actor_id'] = actor or current.get('actor_id', 0)
            current['current_dead'] = bool(current.get('current_dead') or old.get('current_dead'))
            skills = current.setdefault('skills', {})
            for skill_id, raw_skill in old.get('skills', {}).items():
                skill = skills.setdefault(skill_id, {'skill_id': skill_id})
                self._merge_team_skill(skill, raw_skill)
        old_contributions = self.team_damage_contributions.pop(old_key, None)
        if old_contributions is not None:
            current = self.team_damage_contributions.setdefault(new_key, {})
            for attacker_key, amount in old_contributions.items():
                current[attacker_key] = current.get(attacker_key, 0) + amount
        for contributions in self.team_damage_contributions.values():
            if old_key in contributions:
                contributions[new_key] = (
                    contributions.get(new_key, 0) + contributions.pop(old_key)
                )

    def _team_row(self, actor=0, token='', *, include_equipment=True):
        actor = _integer(actor)
        token = str(token or '').strip()
        if not token and actor:
            token = str(self.profiles.get(actor, {}).get('user_token') or '').strip()
        key = token or (f'entity:{actor}' if actor > 0 else '')
        if not key:
            return None
        row = self.team_player_stats.setdefault(
            key,
            {
                'actor_id': actor,
                'user_token': token,
                'damage': None,
                'healing': None,
                'taken': None,
                'kills': None,
                'assists': None,
                'deaths': None,
                'current_dead': False,
                'skills': {},
            },
        )
        if actor:
            row['actor_id'] = actor
        if token:
            row['user_token'] = token
        profile = {
            **self.token_profiles.get(token, {}),
            **self.profiles.get(actor, {}),
        }
        for field in ('name', 'profession_id', 'level', 'extraordinary_rating',
                      'avatar_id', 'avatar_frame_id', 'equipment_snapshot', 'is_ai'):
            if field == 'equipment_snapshot' and not include_equipment:
                continue
            value = profile.get(field)
            if value not in (None, '', 0):
                row[field] = deepcopy(value)
        side = self._team_side(actor, token)
        if side:
            row['side'] = side
        return row

    def _profile(self, value):
        actor = _integer(value.get('entity_id', value.get('actor_id')))
        token = str(value.get('user_token') or '')
        if token:
            cached = self.token_profiles.setdefault(token, {})
            for key in (
                'name', 'profession_id', 'level', 'extraordinary_rating',
                'avatar_id', 'avatar_frame_id', 'club_name',
                'equipment_snapshot', 'entity_type', 'is_ai', 'ai_template_id',
            ):
                item = value.get(key)
                if key == 'profession_id':
                    item = _profession_id(item) or None
                if item not in (None, ''):
                    cached[key] = item
            actor = actor or self.tokens.get(token, 0)
        if actor <= 0:
            if token:
                self._team_row(token=token)
            return
        previous_key = self._key(actor)
        profile = self.profiles.setdefault(actor, {})
        changed = False
        live_values = {**self.token_profiles.get(token, {}), **value}
        for key in (
            'name', 'profession_id', 'level', 'extraordinary_rating',
            'avatar_id', 'avatar_frame_id', 'entity_type',
            'user_token', 'is_ai', 'ai_template_id', 'template_id', 'club_name',
            'equipment_snapshot',
        ):
            item = live_values.get(key)
            if key == 'is_ai' and token:
                # The generic network parser still marks the synthetic-looking
                # 0x6a token form as AI for PVE projection handling. In a
                # formal arena that prefix is also used by real roles; PVP
                # classification requires explicit template evidence and must
                # override the generic flag.
                item = pvp_bot_evidence(live_values)
            if key == 'profession_id':
                item = _profession_id(item) or None
            if key == 'extraordinary_rating':
                item = parse_combat_amount(item)
                if item is not None and not 0 < item <= 1_000_000:
                    item = None
            if item not in (None, '') and profile.get(key) != item:
                profile[key] = item
                changed = True
        token = str(profile.get('user_token') or '')
        if token:
            self.tokens[token] = actor
            # An actor can be observed before its stable token. Re-key already
            # accepted counters rather than showing the same opponent twice.
            if previous_key != token:
                self._merge_team_stat_key(previous_key, token, actor)
                for counters in (self.outgoing, self.incoming):
                    old = counters.pop(previous_key, None)
                    if old is not None:
                        current = counters.setdefault(token, {'actor_id': actor, 'damage': 0})
                        current['damage'] += old['damage']
                for skills_by_opponent in (self.opponent_skills_outgoing, self.opponent_skills_incoming):
                    old_skills = skills_by_opponent.pop(previous_key, {})
                    current_skills = skills_by_opponent.setdefault(token, {})
                    for skill_id, old_skill in old_skills.items():
                        current = current_skills.setdefault(skill_id, {'skill_id': skill_id, 'damage': 0, 'hits': 0, 'critical_hits': 0, 'max_hit': 0})
                        for field in ('damage', 'hits', 'critical_hits'):
                            current[field] += old_skill[field]
                        current['max_hit'] = max(current['max_hit'], old_skill.get('max_hit', 0))
                if previous_key in self.life_incoming:
                    self.life_incoming[token] = self.life_incoming.get(token, 0) + self.life_incoming.pop(previous_key)
                if previous_key in self.life_outgoing:
                    self.life_outgoing[token] = self.life_outgoing.get(token, 0) + self.life_outgoing.pop(previous_key)
                if previous_key in self.healing_by_actor:
                    self.healing_by_actor[token] = (
                        self.healing_by_actor.get(token, 0)
                        + self.healing_by_actor.pop(previous_key)
                    )
                if previous_key in self.healing_skills:
                    target_skills = self.healing_skills.setdefault(token, {})
                    for skill_id, old_skill in self.healing_skills.pop(previous_key).items():
                        current_skill = target_skills.setdefault(
                            skill_id,
                            {
                                'skill_id': skill_id,
                                'effective_healing': 0,
                                'total_healing': 0,
                                'casts': 0,
                            },
                        )
                        for field in ('effective_healing', 'total_healing', 'casts'):
                            current_skill[field] += int(old_skill.get(field, 0) or 0)
                if previous_key in self.dead:
                    self.dead.discard(previous_key)
                    self.dead.add(token)
        if changed:
            self.generation += 1

    def _replace_pvp_full_roster(self, arguments):
        teams = _team_pvp_roster_teams(arguments)
        if teams is None:
            return False
        self.pvp_full_roster_teams = deepcopy(teams)
        return self._rebuild_pvp_full_roster()

    def _accept_league_roster(self, arguments, *, refresh=False):
        try:
            raids = (
                _league_group_refresh(arguments) if refresh
                else _league_roster(arguments)
            )
        except (TypeError, ValueError):
            return False
        if not raids or not self.self_token:
            return False
        if refresh and self.pvp_league_roster.get('raids'):
            previous = self.pvp_league_roster['raids']
            raids = {**previous, **raids}
        own_raid_id = next((
            raid_id for raid_id, raid in raids.items()
            if any(
                member['user_token'] == self.self_token
                for members in raid['groups'].values() for member in members
            )
        ), 0)
        if not own_raid_id:
            return False
        raid_items = list(raids.items())
        if len(raid_items) > PVP_MAX_RAIDS:
            # Keep the local player's official raid visible even if a stale
            # refresh briefly leaves more than the protocol maximum in the
            # merged cache.
            own_item = next((item for item in raid_items if item[0] == own_raid_id), None)
            raid_items = raid_items[:PVP_MAX_RAIDS]
            if own_item is not None and not any(item[0] == own_raid_id for item in raid_items):
                raid_items[-1] = own_item
        numbered_raids = {}
        used_numbers = set()
        for index, (raid_id, raid) in enumerate(raid_items, 1):
            number = _integer(raid.get('number'))
            if not 1 <= number <= PVP_MAX_RAIDS or number in used_numbers:
                number = next(
                    (candidate for candidate in range(1, PVP_MAX_RAIDS + 1)
                     if candidate not in used_numbers),
                    index,
                )
            used_numbers.add(number)
            groups = {}
            remaining = PVP_MAX_MEMBERS_PER_RAID
            for group_number, members in sorted(raid['groups'].items()):
                if remaining <= 0:
                    break
                limited = list(members)[:remaining]
                if limited:
                    groups[group_number] = limited
                    remaining -= len(limited)
            numbered_raids[raid_id] = {
                'number': number,
                'groups': groups,
            }
        own_raid = numbered_raids[own_raid_id]
        current = {
            'raid_id': own_raid_id,
            'raid_number': own_raid['number'],
            'groups': own_raid['groups'],
            'raids': numbered_raids,
        }
        if self.pvp_league_roster == current:
            return False
        self.pvp_league_roster = deepcopy(current)
        allies = OrderedDict()
        for raid in sorted(numbered_raids.values(), key=lambda item: item['number']):
            for group_number in sorted(raid['groups']):
                for member in raid['groups'][group_number]:
                    token = member['user_token']
                    actor = self.tokens.get(token, 0)
                    self._profile({'entity_id': actor, **member, 'entity_type': 'Player'})
                    if token != self.self_token:
                        allies[token] = dict(member, side='ally', entity_type='Player')
        self.pvp_allies = allies
        self.generation += 1
        return True

    def _rebuild_pvp_full_roster(self):
        """Split the complete two-team roster using the stable local token."""

        if not self.self_token or len(self.pvp_full_roster_teams) != 2:
            return False
        own_team_id = None
        for team_id, members in self.pvp_full_roster_teams.items():
            if any(member.get('user_token') == self.self_token for member in members):
                own_team_id = team_id
                break
        if own_team_id is None:
            return False
        allies = OrderedDict()
        enemies = OrderedDict()
        own_members = self.pvp_full_roster_teams[own_team_id]
        for team_id, members in self.pvp_full_roster_teams.items():
            side = 'ally' if team_id == own_team_id else 'enemy'
            target = allies if side == 'ally' else enemies
            for raw_member in members:
                member = {**raw_member, 'side': side}
                token = member['user_token']
                actor = self.self_id if token == self.self_token else self.tokens.get(token, 0)
                self._profile({'entity_id': actor, **member})
                self._team_row(actor, token)
                if token != self.self_token:
                    target[token] = member
        previous = (self.pvp_allies, self.pvp_enemies, self.team_size)
        next_state = (allies, enemies, len(own_members))
        if previous == next_state:
            return True
        self.pvp_allies = allies
        self.pvp_enemies = enemies
        self.team_size = len(own_members)
        self.generation += 1
        return True

    def _replace_pvp_allies(self, arguments):
        members = _team_pvp_members(arguments)
        if members is None:
            return False
        for member in members:
            self.pvp_roster_members[member['user_token']] = dict(member)
            if member['user_token'] == self.self_token:
                self.pvp_self_avatar_template_id = _integer(
                    member.get('avatar_template_id')
                )
        if self.pvp_full_roster_teams:
            for member in members:
                actor = (
                    self.self_id
                    if member['user_token'] == self.self_token
                    else self.tokens.get(member['user_token'], 0)
                )
                self._profile({'entity_id': actor, **member})
            if self._rebuild_pvp_full_roster():
                return True
        if self.pvp_self_avatar_template_id:
            return self._rebuild_pvp_template_roster()
        allies = OrderedDict()
        enemies = OrderedDict()
        for member in members:
            token = member['user_token']
            if token == self.self_token:
                continue
            actor = self.tokens.get(token, 0)
            self._profile({'entity_id': actor, **member})
            target = enemies if member.get('side') == 'enemy' else allies
            target[token] = dict(member)
        previous = (deepcopy(self.pvp_allies), deepcopy(self.pvp_enemies))
        # A verified TeamPVPInfo side always wins over an older party/team
        # snapshot.  Without removing the opposite-side copy first, a player
        # who was carried into the arena as a normal party member can remain in
        # ``pvp_allies`` after the wire roster identifies them as an enemy.
        for token in enemies:
            self.pvp_allies.pop(token, None)
        for token in allies:
            self.pvp_enemies.pop(token, None)
        if allies and enemies:
            # Current 3V3 captures send verified friendly/enemy pairs more than
            # once as the roster is discovered. Merge those pairs for this map.
            self.pvp_allies.update(allies)
            self.pvp_enemies.update(enemies)
        elif allies:
            # A one-sided payload is a complete list for that side (legacy
            # TeamPVPInfo shape); keep its replacement semantics.
            self.pvp_allies = allies
        elif enemies:
            self.pvp_enemies = enemies
        for token in (*allies, *enemies):
            self._team_row(self.tokens.get(token, 0), token)
        if previous == (self.pvp_allies, self.pvp_enemies):
            return True
        self.generation += 1
        return True

    def _rebuild_pvp_template_roster(self):
        """Split a team-arena roster by the local avatar team template.

        Field 5 in ``OnMsgSyncTeamPVPInfo`` is a transient combat/status bit in
        current 6V6/12V12 traffic.  Field 6 is the stable team avatar template:
        every captured member on the local side shares the local player's
        template and the opposing side uses the other template.
        """

        local_template = _integer(self.pvp_self_avatar_template_id)
        if local_template <= 0:
            return False
        allies = OrderedDict()
        enemies = OrderedDict()
        for token, raw_member in self.pvp_roster_members.items():
            member = dict(raw_member)
            if token == self.self_token:
                self._profile({'entity_id': self.self_id, **member})
                self._team_row(self.self_id, token)
                continue
            side = (
                'ally'
                if _integer(member.get('avatar_template_id')) == local_template
                else 'enemy'
            )
            member['side'] = side
            actor = self.tokens.get(token, 0)
            self._profile({'entity_id': actor, **member})
            (allies if side == 'ally' else enemies)[token] = member
            self._team_row(actor, token)
        # TeamPVPInfo arrives as partial pairs in a 12V12. Keep teammates
        # independently observed in this arena's local party stream while the
        # verified pairs catch up; pre-entry party seeds are not retained.
        for token in sorted(self.pvp_live_party_tokens):
            if token in allies or token in enemies:
                continue
            if self.team_size and len(allies) >= self.team_size - 1:
                break
            member = {
                **self.token_profiles.get(token, {}),
                **self.pvp_allies.get(token, {}),
                'user_token': token,
                'side': 'ally',
                'entity_type': 'Player',
            }
            allies[token] = member
        previous = (self.pvp_allies, self.pvp_enemies)
        if previous == (allies, enemies):
            return True
        self.pvp_allies = allies
        self.pvp_enemies = enemies
        self.generation += 1
        return True

    def team_death_totals(self):
        """Return observed deaths split by the verified PvP side."""

        totals = {'ally': 0, 'enemy': 0}
        for event in self.death_events:
            actor = _integer(event.get('victim_id'))
            key = str(event.get('victim_key') or '').strip()
            token = '' if key.startswith('entity:') else key
            side = self._team_side(actor, token)
            if side in totals:
                totals[side] += 1
        return totals

    def pvp_team_members(self, *, include_self=False, side='ally'):
        """Return the latest verified PVP roster with live profile data."""

        roster = self.pvp_enemies if str(side).casefold() == 'enemy' else self.pvp_allies
        tokens = list(roster)
        if (
            include_self and roster is self.pvp_allies
            and self.self_token and self.self_token not in roster
        ):
            tokens.insert(0, self.self_token)
        result = []
        for token in tokens:
            actor = self.tokens.get(token, 0)
            if token == self.self_token:
                actor = actor or self.self_id
            profile = {
                **roster.get(token, {}),
                **self.token_profiles.get(token, {}),
                **self.profiles.get(actor, {}),
            }
            result.append({
                **profile,
                'actor_id': actor,
                'entity_id': actor,
                'user_token': token,
                'name': profile.get('name') or '',
                'profession_id': _profession_id(profile.get('profession_id')),
                'level': _integer(profile.get('level')),
                'extraordinary_rating': profile.get('extraordinary_rating'),
                'avatar_id': _integer(profile.get('avatar_id')),
                'avatar_frame_id': _integer(profile.get('avatar_frame_id')),
                'is_ai': pvp_bot_evidence(profile),
                'is_self': token == self.self_token,
                'side': 'enemy' if roster is self.pvp_enemies else 'ally',
            })
        return result

    def _inferred_enemy_tokens(self):
        """Infer only the remainder of a complete, size-verified arena roster."""

        if self.team_size <= 1:
            return []
        allies = {self.self_token, *self.pvp_allies}
        allies.discard('')
        if len(allies) < self.team_size:
            return []
        known = allies | set(self.pvp_enemies)
        candidates = []
        for token, profile in self.token_profiles.items():
            if token in known or not token:
                continue
            actor = self.tokens.get(token, 0)
            live = {**profile, **self.profiles.get(actor, {})}
            if live.get('entity_type') in ('Player', 'Avatar', 'AvatarActor') and live.get('is_ai') is not True:
                candidates.append(token)
        missing = max(0, self.team_size - len(self.pvp_enemies))
        return candidates[:missing]

    def team_battle_rows(self, *, include_details=True, include_equipment=None):
        """Return reliable friendly/enemy rows for the live team arena HUD."""

        if include_equipment is None:
            include_equipment = include_details

        ally_tokens = ([self.self_token] if self.self_token else []) + list(self.pvp_allies)
        enemy_tokens = list(self.pvp_enemies)
        for token in self._inferred_enemy_tokens():
            if token not in enemy_tokens:
                enemy_tokens.append(token)

        def build(token, side):
            actor = self.tokens.get(token, 0)
            stats = self._team_row(
                actor, token, include_equipment=include_equipment
            ) or {}
            profile = {
                **(self.pvp_allies if side == 'ally' else self.pvp_enemies).get(token, {}),
                **self.token_profiles.get(token, {}),
                **self.profiles.get(actor, {}),
            }
            skill_stats = (
                deepcopy(stats.get('skills', {})) if include_details else {}
            )
            is_self = token == self.self_token
            statistics_authoritative = bool(
                stats.get('statistics_authoritative')
            )
            if is_self:
                # The local exact stream is complete even when some remote
                # actors have not yet been assigned to a side.  Do not let the
                # partial side-aware team accumulator replace the official
                # local total (for example 138 replacing 714 in 3V3).
                if include_details:
                    for skill_id, raw in self.skills_outgoing.items():
                        current = skill_stats.setdefault(skill_id, {'skill_id': skill_id})
                        for field in (
                            'damage', 'hits', 'critical_hits', 'max_hit', 'casts',
                            'hit_timestamps_ns', 'cast_timestamps_ns',
                        ):
                            if raw.get(field) is not None:
                                current[field] = deepcopy(raw[field])
                kills, _defeats, deaths = self._death_counts()
                exact_healing = self.healing_by_actor.get(
                    self._key(self.self_id)
                )
                estimated_healing = (
                    self.duel_healing_estimate()
                    if exact_healing is None and self.duel
                    else None
                )
                local_metrics = {
                    'damage': sum(row['damage'] for row in self.outgoing.values()),
                    'taken': (
                        sum(row['damage'] for row in self.incoming.values())
                        if self.has_exact_incoming else None
                    ),
                    # Keep the same source precedence as the finalized
                    # record: exact HealSync first, duel HP-balance estimate
                    # second, and None when neither is available.  A zero
                    # fallback would overwrite the estimate during the team
                    # row merge.
                    'healing': (
                        exact_healing
                        if exact_healing is not None
                        else estimated_healing
                    ),
                    'healing_source': (
                        'network_exact'
                        if exact_healing is not None
                        else 'hp_balance_estimate'
                        if estimated_healing is not None
                        else ''
                    ),
                    'kills': sum(kills.values()),
                    'assists': sum(self._assist_counts().values()),
                    'deaths': deaths,
                }
            else:
                local_metrics = {}
            metrics_scope = (
                'self_exact'
                if is_self
                else 'authoritative'
                if statistics_authoritative
                else 'observed_partial'
            )
            if is_self:
                skills_scope = 'self_exact'
            elif stats.get('skills_authoritative') is True:
                skills_scope = 'authoritative'
            elif any(skill_has_activity(raw) for raw in (
                skill_stats if include_details else stats.get('skills', {})
            ).values()):
                # The final arena settlement is authoritative for player
                # totals, but does not carry a complete per-skill breakdown.
                # Exact callbacks observed locally remain useful partial data.
                skills_scope = 'observed_partial'
            else:
                skills_scope = 'identity_only'
            skills = []
            if include_details:
                for skill_id, raw in skill_stats.items():
                    if not skill_has_activity(raw):
                        continue
                    row = _skill_output(raw)
                    row['skill_id'] = _integer(row.get('skill_id', skill_id))
                    skills.append(row)
            skills.sort(key=lambda row: (-(row.get('damage') or 0), row.get('skill_id') or 0))
            key = token or f'entity:{actor}'
            pair = (
                self.incoming.get(token) or self.incoming.get(self._key(actor))
                if side == 'enemy' else None
            )
            damage_to_self = (
                max(0, _integer(pair.get('damage')))
                if isinstance(pair, dict) and pair.get('damage') is not None
                else None
            )
            return {
                'character_id': token,
                'user_token': token,
                'actor_id': actor,
                'name': profile.get('name') or stats.get('name') or '未知玩家',
                'profession_id': _profession_id(
                    profile.get('profession_id') or stats.get('profession_id')
                ),
                'extraordinary_rating': (
                    profile.get('extraordinary_rating')
                    if profile.get('extraordinary_rating') is not None
                    else stats.get('extraordinary_rating')
                ),
                'level': _integer(profile.get('level') or stats.get('level')),
                'avatar_id': _integer(
                    profile.get('avatar_id') or stats.get('avatar_id')
                ),
                'avatar_frame_id': _integer(
                    profile.get('avatar_frame_id') or stats.get('avatar_frame_id')
                ),
                'equipment_snapshot': (
                    deepcopy(profile.get('equipment_snapshot') or stats.get('equipment_snapshot'))
                    if include_equipment else None
                ),
                'is_ai': pvp_bot_evidence({**stats, **profile}),
                'is_self': is_self,
                'side': side,
                'damage_to_self': damage_to_self,
                'damage': (
                    stats.get('damage')
                    if statistics_authoritative
                    else local_metrics.get('damage', stats.get('damage'))
                ),
                # The local player has an exact heal stream even before a
                # whole-team settlement arrives.  Do not discard it merely
                # because the side row is still observational; remote rows
                # continue to use only server-provided values.
                'healing': (
                    stats.get('healing')
                    if statistics_authoritative and stats.get('healing') is not None
                    else local_metrics.get('healing', stats.get('healing'))
                ),
                'healing_source': (
                    stats.get('healing_source')
                    if statistics_authoritative and stats.get('healing_source')
                    else local_metrics.get('healing_source', stats.get('healing_source', ''))
                ),
                'taken': (
                    stats.get('taken')
                    if statistics_authoritative
                    else local_metrics.get('taken', stats.get('taken'))
                ),
                'kills': (
                    stats.get('kills')
                    if statistics_authoritative
                    else local_metrics.get('kills', stats.get('kills'))
                ),
                'assists': (
                    stats.get('assists')
                    if statistics_authoritative and stats.get('assists') is not None
                    else local_metrics.get('assists', stats.get('assists'))
                ),
                'deaths': (
                    stats.get('deaths')
                    if statistics_authoritative
                    else local_metrics.get('deaths', stats.get('deaths'))
                ),
                'current_dead': (
                    bool(stats.get('deaths', 0) or 0)
                    if statistics_authoritative and self.result in ('胜利', '失败')
                    else bool(stats.get('current_dead') or key in self.dead)
                ),
                'skills': skills,
                'metrics_scope': metrics_scope,
                'skills_scope': skills_scope,
                'statistics_authoritative': statistics_authoritative,
            }

        allies = [build(token, 'ally') for token in dict.fromkeys(ally_tokens) if token]
        enemies = [build(token, 'enemy') for token in dict.fromkeys(enemy_tokens) if token]
        if self.team_size >= 12:
            sort_key = lambda row: (row.get('damage') is None, -(row.get('damage') or 0), row.get('name') or '')
            allies.sort(key=sort_key)
            enemies.sort(key=sort_key)
        return {'allies': allies, 'enemies': enemies}

    def _accept_team_summary(self, value):
        actors = value.get('actors') if isinstance(value, dict) else None
        if not isinstance(actors, list):
            return False
        changed = False
        for raw in actors:
            if not isinstance(raw, dict):
                continue
            actor = _integer(raw.get('actor_id'))
            token = str(raw.get('user_token') or '').strip()
            if actor <= 0 and not token:
                continue
            profile = {
                'entity_id': actor,
                'user_token': token,
                'name': raw.get('name'),
                'profession_id': raw.get('profession_id'),
                'entity_type': 'Player',
                'is_ai': bool(raw.get('is_ai')),
            }
            self._profile(profile)
            actor = actor or self.tokens.get(token, 0)
            row = self._team_row(actor, token)
            if row is None or not self._team_side(actor, token):
                continue
            before = deepcopy(row)
            for source, target in (
                ('damage', 'damage'),
                ('effective_healing', 'healing'),
            ):
                if source in raw and raw.get(source) is not None:
                    amount = parse_combat_amount(raw.get(source))
                    if amount is not None:
                        row[target] = amount
            if raw.get('taken_present') is True and raw.get('taken') is not None:
                amount = parse_combat_amount(raw.get('taken'))
                if amount is not None:
                    row['taken'] = amount
            if 'deaths' in raw and raw.get('deaths') is not None:
                row['deaths'] = max(0, _integer(raw.get('deaths')))
            if isinstance(raw.get('skills'), list):
                skills = row.setdefault('skills', {})
                for item in raw['skills']:
                    if not isinstance(item, dict):
                        continue
                    skill_id = _integer(item.get('skill_id'))
                    damage = parse_combat_amount(item.get('damage'))
                    if skill_id <= 0 or damage is None:
                        continue
                    skill = skills.setdefault(skill_id, {'skill_id': skill_id})
                    skill['damage'] = damage
                    if item.get('hits') is not None:
                        skill['hits'] = max(0, _integer(item.get('hits')))
                if value.get('authoritative') is True and raw.get('skills'):
                    row['skills_authoritative'] = True
            row['statistics_authoritative'] = bool(value.get('authoritative'))
            changed = changed or row != before
        if changed:
            self.team_settlement = self.team_battle_rows()
            self.generation += 1
        return changed

    def _accept_team_settlement(self, value):
        """Apply one already validated whole-team result without accumulating."""

        if not isinstance(value, dict):
            return False
        allies = value.get('allies')
        enemies = value.get('enemies')
        if not isinstance(allies, list) or not isinstance(enemies, list):
            return False
        changed = False
        settled_tokens = {'ally': set(), 'enemy': set()}
        for side, rows in (('ally', allies), ('enemy', enemies)):
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                token = str(
                    raw.get('character_id') or raw.get('user_token') or ''
                ).strip()
                if not token:
                    continue
                settled_tokens[side].add(token)
                actor = (
                    self.self_id
                    if token == self.self_token
                    else self.tokens.get(token, 0)
                )
                cached_profile = {
                    **self.token_profiles.get(token, {}),
                    **self.profiles.get(actor, {}),
                }
                cached_name = str(cached_profile.get('name') or '').strip()
                incoming_name = str(raw.get('name') or '').strip()
                # The aggregate result is authoritative for counters and side,
                # but some captures decode its display-name bytes poorly.  A
                # profile/shape response for the same stable UID is the better
                # identity source and must not be replaced by mojibake here.
                if (
                    cached_name
                    and cached_name != '未知玩家'
                    and not cached_name.startswith('玩家 ')
                    and '\ufffd' not in cached_name
                ):
                    display_name = cached_name
                else:
                    display_name = incoming_name or cached_name
                self._profile({
                    'entity_id': actor,
                    'user_token': token,
                    'name': display_name,
                    'profession_id': raw.get('profession_id'),
                    'level': raw.get('level'),
                    'extraordinary_rating': raw.get('extraordinary_rating'),
                    'is_ai': bool(raw.get('is_ai')),
                    'entity_type': 'Player',
                })
                member = {
                    'user_token': token,
                    'name': display_name,
                    'profession_id': _profession_id(raw.get('profession_id')),
                    'level': _integer(raw.get('level')),
                    'side': side,
                    'entity_type': 'Player',
                    'is_ai': bool(raw.get('is_ai')),
                }
                roster = self.pvp_allies if side == 'ally' else self.pvp_enemies
                opposite = self.pvp_enemies if side == 'ally' else self.pvp_allies
                roster[token] = member
                opposite.pop(token, None)
                row = self._team_row(actor, token)
                if row is None:
                    continue
                before = deepcopy(row)
                for field in (
                    'damage', 'healing', 'taken', 'kills', 'assists', 'deaths'
                ):
                    if field in raw and raw.get(field) is not None:
                        row[field] = max(0, _integer(raw.get(field)))
                row['side'] = side
                row['statistics_authoritative'] = True
                changed = changed or row != before
        # Method 798 contains the complete final roster.  Earlier inferred hit
        # targets and partial TeamPVPInfo rows are not extra participants; keep
        # exactly the two authoritative sides so a 3V3 can never become 3+4.
        for roster, side in (
            (self.pvp_allies, 'ally'),
            (self.pvp_enemies, 'enemy'),
        ):
            for stale_token in set(roster) - settled_tokens[side]:
                roster.pop(stale_token, None)
                changed = True
        if self.pvp_roster_members or self.pvp_full_roster_teams:
            self.pvp_roster_members.clear()
            self.pvp_full_roster_teams.clear()
            self.pvp_league_roster.clear()
            self.pvp_self_avatar_template_id = 0
            changed = True
        team_size = _integer(value.get('team_size'))
        if team_size > 0:
            self.team_size = team_size
        result = str(value.get('result') or '')
        if result in ('胜利', '失败') and self.result != result:
            self.result = result
            changed = True
        if changed:
            self.team_settlement = self.team_battle_rows()
            self.generation += 1
        return changed

    def _accept_cast(self, actor, skill_value, stamp, *, defer=True):
        actor = _integer(actor)
        skill_id = normalize_network_skill_id(skill_value)
        if actor <= 0 or skill_id <= 0:
            return False
        if self.duel and actor != self.self_id and not self.duel_opponent_actor:
            self._bind_duel_opponent_actor(actor)
        if not self._known_player(actor):
            if defer and self.map_scoped and not self._non_player(actor):
                self.pending_team_events.append(
                    ('cast', stamp, (actor, skill_value))
                )
            return False
        token = str(self.profiles.get(actor, {}).get('user_token') or '').strip()
        if not self._team_side(actor, token):
            if defer and self.map_scoped:
                self.pending_team_events.append(
                    ('cast', stamp, (actor, skill_value))
                )
            return False
        row = self._team_row(actor, token)
        if row is None:
            return False
        skill = row.setdefault('skills', {}).setdefault(
            skill_id, {'skill_id': skill_id, 'damage': 0, 'hits': 0, 'casts': 0}
        )
        skill['casts'] = int(skill.get('casts', 0) or 0) + 1
        _append_timestamp(skill, 'cast_timestamps_ns', stamp)
        # Keep the aggregate direction-specific skill projection in sync even
        # when a cast has no corresponding damage packet.
        outgoing = actor == self.self_id
        aggregate = self.skills_outgoing if outgoing else self.skills_incoming
        aggregate_skill = aggregate.setdefault(
            skill_id,
            {
                'skill_id': skill_id,
                'damage': 0,
                'hits': 0,
                'casts': 0,
                'critical_hits': 0,
                'max_hit': 0,
            },
        )
        aggregate_skill['casts'] = int(aggregate_skill.get('casts', 0) or 0) + 1
        _append_timestamp(aggregate_skill, 'cast_timestamps_ns', stamp)
        if not outgoing:
            key = self._key(actor)
            pair = self.opponent_skills_incoming.setdefault(key, {}).setdefault(
                skill_id,
                {
                    'skill_id': skill_id,
                    'damage': 0,
                    'hits': 0,
                    'casts': 0,
                    'critical_hits': 0,
                    'max_hit': 0,
                },
            )
            pair['casts'] = int(pair.get('casts', 0) or 0) + 1
            _append_timestamp(pair, 'cast_timestamps_ns', stamp)
        self.generation += 1
        return True

    def _accept_heal(self, value):
        """Accumulate only exact HealSyncV2 healing from PvP participants."""

        if not isinstance(value, dict):
            return False
        healer = _integer(value.get('healer_id', value.get('actor_id')))
        target = _integer(value.get('target_id'))
        effective = parse_combat_amount(value.get('effective_healing'))
        attempted = parse_combat_amount(value.get('total_healing'))
        if healer <= 0 or effective is None or effective < 0:
            return False
        if attempted is not None and (attempted < 0 or effective > attempted):
            return False
        if value.get('healing_source') not in (None, '', 'network_exact', 'native_exact'):
            return False
        if self.duel and healer != self.self_id and not self.duel_opponent_actor:
            self._bind_duel_opponent_actor(healer)
        if target > 0 and self.duel and target != self.self_id and not self.duel_opponent_actor:
            self._bind_duel_opponent_actor(target)
        if target > 0 and self._non_player(target):
            return False
        if not self.self_id or not self._known_player(healer):
            return False
        healer_token = str(
            self.profiles.get(healer, {}).get('user_token') or ''
        ).strip()
        healer_side = self._team_side(healer, healer_token)
        if not healer_side:
            return False
        if self.started_at_ns is None:
            return False
        row = self._team_row(healer, healer_token)
        if row is None:
            return False
        row['healing'] = int(row.get('healing', 0) or 0) + int(effective)
        self.has_exact_healing = True
        key = self._key(healer)
        self.healing_by_actor[key] = int(self.healing_by_actor.get(key, 0) or 0) + int(effective)
        skill_id = normalize_network_skill_id(value.get('skill_id'))
        if skill_id > 0:
            skill = self.healing_skills.setdefault(
                key, {}
            ).setdefault(
                skill_id,
                {'skill_id': skill_id, 'effective_healing': 0, 'total_healing': 0, 'casts': 0},
            )
            skill['effective_healing'] += int(effective)
            if attempted is not None:
                skill['total_healing'] += int(attempted)
        if healer == self.self_id:
            self.generation += 1
        else:
            self.generation += 1
        return True

    def _accept_actor_health(self, value):
        if _integer(value.get('entity_id')) != self.self_id or self.self_id <= 0:
            return
        if value.get('health_source') != 'bound_realtime_hp':
            return
        try:
            maximum = float(value.get('max_hp') or 0)
            current = float(value.get('current_hp'))
        except (TypeError, ValueError, OverflowError):
            return
        if not (0 < maximum < 1_000_000 and 0 <= current <= maximum * 1.02):
            return
        stamp = timestamp_ns(value)
        if stamp <= 0 or stamp < self._known_self_max_hp_stamp:
            return
        self._known_self_max_hp = maximum
        self._known_self_max_hp_stamp = stamp
        if not self.duel or self.started_at_ns is None or stamp < self.started_at_ns:
            return
        if self.ended_at_ns is not None and stamp > self.ended_at_ns:
            return
        if self._duel_max_hp is None:
            self._duel_max_hp = maximum
        elif abs(maximum - self._duel_max_hp) > self._duel_max_hp * 0.02:
            self._duel_max_hp_changed = True
        if self._duel_last_hp is None or stamp >= self._duel_last_hp[0]:
            taken = sum(row['damage'] for row in self.incoming.values())
            self._duel_last_hp = (stamp, current, taken)

    def duel_healing_estimate(self):
        """Estimate own duel HP recovery; this is not exact skill healing."""
        if (not self.duel or not self.duel_finished
                or self.duel_outcome not in ('胜利', '失败')
                or not self.capture_complete or not self.has_exact_incoming
                or self._duel_max_hp_changed or not self._duel_max_hp
                or self._duel_result_hp is None or self._duel_result_taken is None):
            return None
        return max(0, round(
            self._duel_result_taken - self._duel_max_hp + self._duel_result_hp
        ))

    def ingest_update(self, kind, value):
        if not isinstance(value, dict):
            return False
        before = self.generation
        if kind == 'identity' and (value.get('self_confirmed') is not False):
            if value.get('tentative') is not True:
                self._set_identity(_integer(value.get('self_id', value.get('entity_id'))), value.get('user_token', value.get('self_token')))
        elif kind == 'self_character':
            token = str(value.get('character_id', value.get('user_token')) or '')
            actor = _integer(value.get('entity_id')) or self.self_id
            if actor and token:
                self._set_identity(actor, token)
                self._profile(value)
        elif kind in ('profile', 'name'):
            self._profile(value)
        elif kind == 'equipment_profile':
            token = str(value.get('user_token') or '').strip()
            actor = _integer(value.get('actor_id')) or self.tokens.get(token, 0)
            self._profile({
                'entity_id': actor,
                'user_token': token,
                'name': value.get('name'),
                'profession_id': value.get('profession_id'),
                'extraordinary_rating': value.get('extraordinary_rating'),
                'equipment_snapshot': value.get('equipment_snapshot'),
                'entity_type': 'Player',
            })
        elif kind == 'pvp_team_summary':
            self._accept_team_summary(value)
        elif kind == 'pvp_team_settlement':
            self._accept_team_settlement(value)
        elif kind == 'heal':
            self._accept_heal(value)
        elif kind == 'actor_health':
            self._accept_actor_health(value)
        self._retry_pending()
        return before != self.generation

    def _begin(self, stamp):
        if self.started_at_ns is None:
            self.started_at_ns = stamp
        if (self.duel and self._duel_max_hp is None
                and 0 <= stamp - self._known_self_max_hp_stamp <= 60_000_000_000):
            self._duel_max_hp = self._known_self_max_hp
        if self.active_since_ns is None:
            self.active_since_ns = stamp
        self.paused_at_ns = None
        self.last_activity_ns = max(self.last_activity_ns, stamp)
        self.result = '进行中'
        self.generation += 1

    def _pause(self, stamp, result='已脱战'):
        if self.active_since_ns is not None:
            self.elapsed_ns += max(0, stamp - self.active_since_ns)
            self.active_since_ns = None
            self.paused_at_ns = stamp
            self.generation += 1
        if self.started_at_ns is not None and self.result != result:
            self.result = result
            self.generation += 1

    def _claim_pvp_damage_stream_event(self, source, stamp, signature):
        """Deduplicate one BeatenSync amount against a matching DamageSync.

        Repeated hits from the same skill are retained.  Only the opposite
        source and the exact wire signature within the parser's 50 ms pairing
        window are considered duplicates.
        """

        if source not in ('network', 'native', 'beaten') or stamp <= 0:
            return True
        streams = getattr(self, '_pvp_damage_stream_events', None)
        if streams is None:
            return True
        opposite_sources = ('beaten',) if source in ('network', 'native') else ('network', 'native')
        for opposite in opposite_sources:
            candidates = streams.get(opposite, ())
            for index, (candidate_stamp, candidate_signature) in enumerate(candidates):
                if candidate_signature != signature:
                    continue
                if abs(stamp - candidate_stamp) <= PVP_CROSS_SOURCE_DEDUP_WINDOW_NS:
                    # deque has no indexed deletion; rebuild this bounded
                    # source queue while preserving all other observations.
                    streams[opposite] = deque(
                        (item for item_index, item in enumerate(candidates) if item_index != index),
                        maxlen=PVP_CROSS_SOURCE_DEDUP_LIMIT,
                    )
                    return False
        streams[source].append((stamp, signature))
        return True

    def poll(self, now_ns=None):
        now_ns = time.time_ns() if now_ns is None else now_ns
        if (not self.duel and self.active_since_ns is not None and self.last_activity_ns
                and now_ns - self.last_activity_ns >= IDLE_TIMEOUT_NS):
            # Missing exit packets must not leave the clock running forever.
            self._pause(self.last_activity_ns)

    def _record_team_damage(self, attacker, target, skill_id, damage, stamp, event):
        attacker_token = str(self.profiles.get(attacker, {}).get('user_token') or '').strip()
        target_token = str(self.profiles.get(target, {}).get('user_token') or '').strip()
        attacker_side = self._team_side(attacker, attacker_token)
        target_side = self._team_side(target, target_token)
        if not attacker_side or not target_side or attacker_side == target_side:
            return False
        attacker_row = self._team_row(attacker, attacker_token)
        target_row = self._team_row(target, target_token)
        if attacker_row is None or target_row is None:
            return False
        # A late exact callback may be replayed after method 798 has supplied
        # authoritative player totals.  Preserve those totals while still
        # enriching the otherwise unavailable per-skill breakdown.
        if attacker_row.get('statistics_authoritative') is not True:
            attacker_row['damage'] = int(attacker_row.get('damage', 0) or 0) + damage
        if target_row.get('statistics_authoritative') is not True:
            target_row['taken'] = int(target_row.get('taken', 0) or 0) + damage
        skill = attacker_row.setdefault('skills', {}).setdefault(
            skill_id,
            {
                'skill_id': skill_id,
                'damage': 0,
                'hits': 0,
                'critical_hits': 0,
                'max_hit': 0,
                'hit_timestamps_ns': [],
            },
        )
        skill['damage'] = int(skill.get('damage', 0) or 0) + damage
        skill['hits'] = int(skill.get('hits', 0) or 0) + 1
        skill['critical_hits'] = int(skill.get('critical_hits', 0) or 0) + int(
            event.get('critical') is True
        )
        skill['max_hit'] = max(int(skill.get('max_hit', 0) or 0), damage)
        _append_timestamp(skill, 'hit_timestamps_ns', stamp)
        attacker_key = attacker_token or f'entity:{attacker}'
        target_key = target_token or f'entity:{target}'
        contributions = self.team_damage_contributions.setdefault(target_key, {})
        contributions[attacker_key] = contributions.get(attacker_key, 0) + damage
        return True

    def _accept_damage(self, event, stamp, *, defer=True):
        attacker = _integer(event.get('attacker_id'))
        target = _integer(event.get('target_id'))
        damage = parse_combat_amount(event.get('damage'))
        if (damage is None or attacker <= 0 or target <= 0 or attacker == target
                or event.get('provisional_damage') is True
                or event.get('damage_source') not in ('network_exact', 'native_exact')):
            return True
        exact_beaten = event.get('source_method') == 'OnMsgBeatenSyncV2' and event.get('beaten_exact') is True
        late_exact_beaten = bool(
            exact_beaten and self.duel_finished and self.ended_at_ns is not None
            and 0 <= stamp - self.ended_at_ns <= DUEL_LATE_EXACT_DAMAGE_GRACE_NS
        )
        if self.duel and self.self_id in (attacker, target):
            self._bind_duel_opponent_actor(
                target if attacker == self.self_id else attacker,
                authoritative=True,
            )
        if self.map_id in HUNTER_CITY_MAP_IDS and self.self_id in (attacker, target):
            dragon_actor = target if attacker == self.self_id else attacker
            dragon_profile = self.profiles.get(dragon_actor, {})
            dragon_template = _integer(dragon_profile.get('template_id'))
            if dragon_template in HUNTER_DRAGON_TEMPLATE_IDS:
                return self._accept_dragon_damage(
                    event, stamp, dragon_actor, dragon_template, damage
                )
            if self._non_player(dragon_actor) and not dragon_template:
                if defer:
                    self.pending.append(('damage', stamp, deepcopy(event)))
                return False
        if self._non_player(attacker) or self._non_player(target):
            return True
        if not self.self_id or not self._known_player(attacker) or not self._known_player(target):
            if defer:
                queue = (
                    self.pending_team_events
                    if self.map_scoped and self.self_id not in (attacker, target)
                    else self.pending
                )
                queue.append(('damage', stamp, deepcopy(event)))
            return False
        local_interaction = self.self_id in (attacker, target)
        attacker_side = self._team_side(attacker)
        target_side = self._team_side(target)
        other_actor = target if attacker == self.self_id else attacker
        verified_duel_pair = bool(
            self.duel
            and local_interaction
            and self.duel_opponent_actor > 0
            and other_actor == self.duel_opponent_actor
        )
        if (
            self.duel
            and local_interaction
            and self.duel_opponent_actor > 0
            and not verified_duel_pair
        ):
            return True
        if (
            attacker_side
            and target_side
            and attacker_side == target_side
            and not verified_duel_pair
        ):
            # Team abilities can use the same wire callback.  They are not
            # Player-vs-Player damage and must not enter outgoing or taken.
            # A formal duel can involve a normal party member, however; its
            # verified opponent binding takes precedence over party side.
            return True
        team_interaction = bool(
            attacker_side and target_side and attacker_side != target_side
        )
        if not local_interaction and not team_interaction:
            if (
                defer
                and self.map_scoped
                and (not attacker_side or not target_side)
            ):
                self.pending_team_events.append(
                    ('damage', stamp, deepcopy(event))
                )
                return False
            return True
        if (local_interaction and self.duel_finished
                and (self.ended_at_ns is None or stamp > self.ended_at_ns)
                and not late_exact_beaten):
            return True
        if (local_interaction and self._key(self.self_id) in self.dead
                and (self.self_dead_at_ns is None or stamp > self.self_dead_at_ns)
                and not late_exact_beaten):
            return True
        if damage == 0:
            if target == self.self_id and not self.has_exact_incoming:
                self.has_exact_incoming = True
                self.generation += 1
            return True
        # Late identity may fill an already-ended duel, but must not reopen
        # its clock or overwrite its result with "in progress".
        late = ((self.ended_at_ns is not None and stamp <= self.ended_at_ns)
                or (self.paused_at_ns is not None and stamp <= self.paused_at_ns))
        if not late and not late_exact_beaten:
            if not (self.map_scoped and self.map_id == 5_200_021
                    and not self.pvp_league_roster):
                self._begin(stamp)
        source = (
            'beaten' if exact_beaten else
            'native' if event.get('damage_source') == 'native_exact' else 'network'
        )
        signature = (
            attacker, target, _integer(event.get('skill_id')),
            _integer(event.get('raw_damage')), damage,
        )
        if not self._claim_pvp_damage_stream_event(source, stamp, signature):
            return True
        self.module_name = '双人切磋' if self.duel else 'PVP 对战'
        skill_id = _integer(event.get('skill_id'))
        if team_interaction:
            self._record_team_damage(
                attacker, target, skill_id, damage, stamp, event
            )
        if not local_interaction:
            self.generation += 1
            return True
        outgoing = attacker == self.self_id
        if not outgoing:
            self.has_exact_incoming = True
        actor = target if outgoing else attacker
        rows = self.outgoing if outgoing else self.incoming
        counter = rows.setdefault(self._key(actor), {'actor_id': actor, 'damage': 0})
        counter['actor_id'] = actor
        counter['damage'] += damage
        counter['last_interaction_ns'] = max(counter.get('last_interaction_ns', 0), stamp)
        skills = self.skills_outgoing if outgoing else self.skills_incoming
        skill = skills.setdefault(skill_id, {'skill_id': skill_id, 'damage': 0, 'hits': 0, 'critical_hits': 0, 'max_hit': 0, 'hit_timestamps_ns': []})
        skill['damage'] += damage
        skill['hits'] += 1
        skill['critical_hits'] += int(event.get('critical') is True)
        skill['max_hit'] = max(skill['max_hit'], damage)
        _append_timestamp(skill, 'hit_timestamps_ns', stamp)
        by_opponent = self.opponent_skills_outgoing if outgoing else self.opponent_skills_incoming
        pair_skill = by_opponent.setdefault(self._key(actor), {}).setdefault(
            skill_id, {'skill_id': skill_id, 'damage': 0, 'hits': 0, 'critical_hits': 0, 'max_hit': 0, 'hit_timestamps_ns': []})
        pair_skill['damage'] += damage
        pair_skill['hits'] += 1
        pair_skill['critical_hits'] += int(event.get('critical') is True)
        pair_skill['max_hit'] = max(pair_skill['max_hit'], damage)
        _append_timestamp(pair_skill, 'hit_timestamps_ns', stamp)
        if not outgoing:
            self.life_incoming[self._key(actor)] = self.life_incoming.get(self._key(actor), 0) + damage
        else:
            self.life_outgoing[self._key(actor)] = self.life_outgoing.get(self._key(actor), 0) + damage
        self.generation += 1
        return True

    def _accept_dragon_damage(self, event, stamp, actor, template_id, damage):
        if damage <= 0:
            return True
        source = (
            'beaten' if event.get('source_method') == 'OnMsgBeatenSyncV2'
            and event.get('beaten_exact') is True else
            'native' if event.get('damage_source') == 'native_exact' else 'network'
        )
        signature = (
            _integer(event.get('attacker_id')),
            _integer(event.get('target_id')),
            _integer(event.get('skill_id')),
            _integer(event.get('raw_damage')),
            damage,
        )
        if not self._claim_pvp_damage_stream_event(source, stamp, signature):
            return True
        if not (self.map_scoped and self.map_id == 5_200_021
                and not self.pvp_league_roster):
            self._begin(stamp)
        profile = self.profiles.get(actor, {})
        row = self.dragon_targets.setdefault(template_id, {
            'template_id': template_id,
            'name': str(profile.get('name') or '战争巨龙'),
            'damage_to': 0,
            'damage_from': 0,
            'skills_outgoing': {},
            'skills_incoming': {},
            'defeated': False,
        })
        outgoing = _integer(event.get('attacker_id')) == self.self_id
        amount_key = 'damage_to' if outgoing else 'damage_from'
        row[amount_key] += damage
        row['last_interaction_ns'] = max(
            int(row.get('last_interaction_ns', 0) or 0), stamp
        )
        skill_id = normalize_network_skill_id(event.get('skill_id'))
        if skill_id > 0:
            skills = row['skills_outgoing' if outgoing else 'skills_incoming']
            skill = skills.setdefault(skill_id, {
                'skill_id': skill_id,
                'damage': 0,
                'hits': 0,
                'critical_hits': 0,
                'max_hit': 0,
            })
            skill['damage'] += damage
            skill['hits'] += 1
            skill['critical_hits'] += int(event.get('critical') is True)
            skill['max_hit'] = max(skill['max_hit'], damage)
        self.generation += 1
        return True

    def _accept_life(self, method, actor, args, stamp, *, defer=True):
        if (method == 'OnMsgEntityDead' and self.map_id in HUNTER_CITY_MAP_IDS
                and self.profiles.get(actor, {}).get('entity_type') in ('NPC', 'Npc', 'Boss', 'Monster')):
            template_id = _integer(self.profiles.get(actor, {}).get('template_id'))
            row = self.dragon_targets.get(template_id)
            if row is not None and template_id in HUNTER_DRAGON_TEMPLATE_IDS:
                row['defeated'] = True
                row['death_timestamp_ns'] = stamp
                self.generation += 1
            return True
        if self._non_player(actor):
            return True
        if not self.self_id or not self._known_player(actor):
            if defer:
                self.pending.append(('life', stamp, (method, actor, deepcopy(args))))
            return False
        late_death = bool(
            method == 'OnMsgEntityDead' and self.duel_finished
            and self.ended_at_ns is not None
            and 0 <= stamp - self.ended_at_ns <= DUEL_LATE_EXACT_DAMAGE_GRACE_NS
        )
        if (self.started_at_ns is None
                or (self.duel_finished and (self.ended_at_ns is None or stamp > self.ended_at_ns)
                    and not late_death)):
            return True
        key = self._key(actor)
        if method == 'OnMsgEntityRelive':
            # args[3] is the REVIVER, never a player who died.
            if key in self.dead:
                self.dead.discard(key)
                team_row = self._team_row(
                    actor, self.profiles.get(actor, {}).get('user_token')
                )
                if team_row is not None:
                    team_row['current_dead'] = False
                self.life_outgoing.pop(key, None)
                if actor == self.self_id:
                    self.self_dead_at_ns = None
                    self.life_incoming.clear()
                self.generation += 1
            return True
        if key in self.dead:
            return True
        self.dead.add(key)
        source_token = args[1] if len(args) > 1 and isinstance(args[1], str) else ''
        victim_token = str(self.profiles.get(actor, {}).get('user_token') or '').strip()
        victim_row = self._team_row(actor, victim_token)
        victim_side = self._team_side(actor, victim_token)
        if victim_row is not None and victim_side:
            victim_row['deaths'] = int(victim_row.get('deaths', 0) or 0) + 1
            victim_row['current_dead'] = True
            source_actor = self.tokens.get(source_token, 0)
            source_side = self._team_side(source_actor, source_token)
            if source_token and source_side and source_side != victim_side:
                killer_row = self._team_row(source_actor, source_token)
                if killer_row is not None:
                    killer_row['kills'] = int(killer_row.get('kills', 0) or 0) + 1
                for contributor in self.team_damage_contributions.get(key, {}):
                    if contributor == source_token:
                        continue
                    contributor_actor = self.tokens.get(contributor, 0)
                    contributor_side = self._team_side(
                        contributor_actor, contributor
                    )
                    if contributor_side != source_side:
                        continue
                    assist_row = self._team_row(contributor_actor, contributor)
                    if assist_row is not None:
                        assist_row['assists'] = int(
                            assist_row.get('assists', 0) or 0
                        ) + 1
            self.team_damage_contributions.pop(key, None)
        assisted_by_self = bool(
            actor != self.self_id
            and source_token
            and source_token != self.self_token
            and (
                not self.map_scoped
                or self._team_side(
                    self.tokens.get(source_token, 0), source_token
                ) == self._team_side(self.self_id, self.self_token)
            )
            and self.life_outgoing.get(key, 0) > 0
        )
        self.death_events.append({'victim_id': actor, 'victim_key': key,
                                  'source_token': source_token, 'timestamp_ns': stamp,
                                  'assisted_by_self': assisted_by_self,
                                  'incoming_contributions': dict(self.life_incoming) if actor == self.self_id else {}})
        if actor != self.self_id:
            self.life_outgoing.pop(key, None)
        if actor == self.self_id:
            self.self_dead_at_ns = stamp
            if not self.duel_finished:
                self._pause(stamp, '已阵亡')
        self.generation += 1
        return True

    def _retry_pending(self):
        waiting, self.pending = self.pending, deque(maxlen=PENDING_LIMIT)
        for kind, stamp, value in waiting:
            accepted = (self._accept_damage(value, stamp, defer=False) if kind == 'damage'
                        else self._accept_life(*value, stamp, defer=False))
            if not accepted:
                self.pending.append((kind, stamp, value))

    def _retry_pending_team_events(self):
        """Replay exact arena events only after roster identity has advanced."""

        waiting = self.pending_team_events
        self.pending_team_events = deque(maxlen=PENDING_TEAM_EVENT_LIMIT)
        for kind, stamp, value in waiting:
            if kind == 'damage':
                accepted = self._accept_damage(value, stamp, defer=False)
            else:
                actor, skill_value = value
                accepted = self._accept_cast(
                    actor, skill_value, stamp, defer=False
                )
            if not accepted:
                self.pending_team_events.append((kind, stamp, value))

    def consume(self, observation):
        if not isinstance(observation, dict):
            return False
        before = self.generation
        record = observation.get('record', {})
        context = observation.get('context', {})
        stamp = _integer(observation.get('timestamp_ns'))
        if stamp <= 0:
            return False
        self._set_identity(_integer(context.get('self_id')), context.get('self_token'))
        if observation.get('capture_gap'):
            self.mark_gap()
        method = str(record.get('method') or '')
        event_id = str(record.get('capture_event_id') or '')
        if not event_id:
            event_id = f"{record.get('capture_source')}:{method}:{record.get('network_entity_id')}:{record.get('sequence')}:{stamp}"
        if event_id in self.seen:
            return before != self.generation
        self.seen[event_id] = None
        if len(self.seen) > SEEN_LIMIT:
            self.seen.popitem(last=False)
        if method in SCENE_TRANSITION_METHODS:
            self._clear_session()
            self.profiles.clear()
            self.tokens.clear()
            self.token_profiles.clear()
            self.pvp_allies.clear()
            self.pvp_enemies.clear()
            self.pvp_roster_members.clear()
            self.pvp_live_party_tokens.clear()
            self.pvp_full_roster_teams.clear()
            self.pvp_self_avatar_template_id = 0
            self.team_size = 0
            self.instance_id = None
            self.map_id = None
            return True
        profile_generation = self.generation
        for value in context.get('profiles', ()):
            self._profile(value)
        for kind, value in observation.get('updates', ()):
            if kind in ('profile', 'name'):
                self._profile(value)
        identity_advanced = self.generation != profile_generation
        self.instance_id = context.get('instance_id') or self.instance_id
        self.map_id = context.get('map_id') or self.map_id
        args = record.get('decoded_arguments', [])
        if not isinstance(args, list):
            args = []
        actor = _integer(record.get('network_entity_id'))
        local_role_control = bool(
            record.get('npcap_method_scope') == 'local_role'
            and method in PVP_CONTROL_METHODS
            and self.self_id
        )
        local = bool(self.self_id and actor == self.self_id) or local_role_control
        if record.get('npcap_recipient') and self.self_token:
            local = record['npcap_recipient'] == self.self_token
        roster_generation = self.generation
        if method == 'OnMsgSyncTeamPVPInfo':
            self._replace_pvp_allies(args)
        elif method == 'OnMsgTeamPVPSettlement':
            settlement = _team_pvp_settlement(args, self.self_token)
            if settlement is not None:
                self._accept_team_settlement(settlement)
        elif method == 'RetGetTeamArenaBattleInfo':
            self._replace_pvp_full_roster(args)
        elif method == 'OnMsgSyncLeagueInfo':
            self._accept_league_roster(args)
        elif method == 'OnMsgRefreshLeagueGroupInfo':
            self._accept_league_roster(args, refresh=True)
        if method in PVP_TEAM_MEMBER_METHODS and local and args:
            token = str(args[0] or '').strip()
            if (
                token and token != self.self_token and 8 <= len(token) <= 128
                and token.isascii() and not any(char.isspace() for char in token)
                and token not in self.pvp_enemies
                and (not self.pvp_full_roster_teams or token in self.pvp_allies)
            ):
                self.pvp_live_party_tokens.add(token)
                previous = deepcopy(self.pvp_allies.get(token))
                self.pvp_enemies.pop(token, None)
                self.pvp_allies.setdefault(
                    token,
                    {'user_token': token, 'side': 'ally', 'entity_type': 'Player'},
                )
                self._profile({
                    'entity_id': self.tokens.get(token, 0),
                    'user_token': token,
                    'entity_type': 'Player',
                })
                self._team_row(self.tokens.get(token, 0), token)
                if previous != self.pvp_allies.get(token):
                    self.generation += 1
        if identity_advanced or self.generation != roster_generation:
            # Identity-only events may now resolve, and non-local team events
            # can finally be assigned to ally/enemy skill rows.
            self._retry_pending()
            self._retry_pending_team_events()
        if method == 'OnMsgTeamPVPSettlement' and settlement is not None:
            result = settlement.get('result')
            if result in ('胜利', '失败') and self.result != result:
                self.result = result
                self.generation += 1
        if method == 'OnMsgCastSkillNew' and args:
            self._accept_cast(actor, args[0], stamp)
        if method == 'OnMsgBeatenSyncV2' and self.duel and len(args) >= 2:
            attacker, target = _beaten_endpoints(record, args)
            if self.self_id in (attacker, target) and attacker != target:
                self._bind_duel_opponent_actor(
                    target if attacker == self.self_id else attacker,
                    authoritative=len(args) >= 5,
                )
        if local and method == 'OnMsgIndividualPVPResponse' and not self.map_scoped:
            if len(args) >= 2 and args[0] is True:
                if self.duel_finished:
                    # Archive the old peer before a new invitation can rebind
                    # its runtime ID/counters to somebody else.
                    self._clear_session()
                self._remember_duel_opponent(
                    args[1], args[2] if len(args) > 2 else ''
                )
        elif local and method == 'OnMsgIndividualPVPState' and args and not self.map_scoped:
            state = _integer(args[0], -1)
            prepare_id = args[1] if len(args) > 1 else stamp
            duplicate_start = state == 3 and prepare_id == self.duel_start_id
            if (state == 2 and prepare_id != self.duel_prepare_id) or (state == 3 and not duplicate_start and (not self.duel or self.duel_finished)):
                opponent_token = self.duel_opponent_token
                self._clear_session()
                self.duel = True
                if state == 2:
                    self.duel_prepare_id = prepare_id
                if opponent_token:
                    self._remember_duel_opponent(opponent_token)
                self.module_name = '双人切磋'
                self.result = '准备中'
            if state == 3 and not duplicate_start:
                self.duel_start_id = prepare_id
                self._begin(stamp)
            elif state in (4, 5):
                self._pause(stamp, '切磋结束')
                if self.ended_at_ns is None:
                    self.ended_at_ns = stamp
                self.duel_finished = True
        elif (local and method == 'OnMsgIndividualPVPResult' and not self.map_scoped
              and self.duel and self.started_at_ns is not None):
            result_code = _integer(args[0], -1) if args else -1
            outcome = DUEL_RESULT_LABELS.get(result_code, '')
            if outcome and not self.duel_outcome:
                self.duel_outcome = outcome
                self.generation += 1
            if outcome and self._duel_result_taken is None:
                self._duel_result_taken = sum(
                    row['damage'] for row in self.incoming.values()
                )
                sample = self._duel_last_hp
                if sample is not None and 0 <= stamp - sample[0] <= 2_000_000_000:
                    self._duel_result_hp = max(
                        0.0, sample[1] - max(0, self._duel_result_taken - sample[2])
                    )
            self._pause(stamp, self.duel_outcome or '切磋结束')
            if self.ended_at_ns is None:
                self.ended_at_ns = stamp
            self.duel_finished = True
        elif local and method == 'OnMsgAddFightRelationship' and args and not self.map_scoped:
            self._remember_duel_opponent(args[0])
        elif (local and method == 'OnMsgSyncFightMode' and args
              and _integer(args[0]) == 0 and not self.duel_finished):
            self._pause(stamp)
        elif method in PVP_LIFE_METHODS:
            self._accept_life(method, actor, args, stamp)
        for event in observation.get('events', ()):
            self._accept_damage(event, stamp)
        return self.generation != before

    def _death_counts(self):
        kills = {}
        defeats = {}
        deaths = 0
        for event in self.death_events:
            if event['victim_id'] == self.self_id or event['victim_key'] == self.self_token:
                deaths += 1
                source_token = str(event.get('source_token') or '')
                source = self.tokens.get(source_token, 0)
                known_source = bool(
                    source and source != self.self_id and self._known_player(source)
                )
                known_token = bool(
                    source_token and source_token != self.self_token
                    and self.token_profiles.get(source_token, {}).get('entity_type') == 'Player'
                    and source_token not in self.pvp_allies
                )
                if known_source or known_token:
                    key = self._key(source) if known_source else source_token
                    defeats[key] = defeats.get(key, 0) + 1
            elif self.self_token and event['source_token'] == self.self_token:
                key = event['victim_key']
                if key.startswith('entity:'):
                    key = self._key(event['victim_id'])
                kills[key] = kills.get(key, 0) + 1
        # IndividualPVPResult is authoritative for a duel. The protocol does
        # not necessarily emit EntityDead, so synthesize the fixed outcome
        # counters: winner 1/0/0, loser 0/1/0.
        if self.duel_finished and self.duel_outcome in DUEL_RESULT_LABELS.values():
            opponent_key = self._duel_opponent_key()
            if self.duel_outcome == '胜利':
                kills[opponent_key] = max(1, kills.get(opponent_key, 0))
            elif self.duel_outcome == '失败':
                deaths = max(1, deaths)
                defeats[opponent_key] = max(1, defeats.get(opponent_key, 0))
        return kills, defeats, deaths

    def _assist_counts(self):
        assists = {}
        if self.duel:
            return assists
        for event in self.death_events:
            if event.get('assisted_by_self') is not True:
                continue
            key = event['victim_key']
            if key.startswith('entity:'):
                key = self._key(event['victim_id'])
            assists[key] = assists.get(key, 0) + 1
        return assists

    def dragon_rows(self, *, include_skills=True):
        """Keep exact dragon interactions separate from player damage."""

        total = sum(row['damage_to'] for row in self.dragon_targets.values())
        result = []
        for template_id, raw in sorted(self.dragon_targets.items()):
            row = {
                key: deepcopy(value)
                for key, value in raw.items()
                if key not in ('skills_outgoing', 'skills_incoming')
            }
            if include_skills:
                for direction in ('skills_outgoing', 'skills_incoming'):
                    row[direction] = sorted(
                        (_skill_output(skill) for skill in raw[direction].values()),
                        key=lambda skill: (-(skill.get('damage') or 0), skill['skill_id']),
                    )
            row['damage'] = row['damage_to']
            row['taken'] = row['damage_from']
            row['damage_share'] = row['damage_to'] / total if total else 0
            result.append(row)
        return result

    def session_snapshot(self, now_ns=None, *, include_details=True,
                         include_teams=True):
        now_ns = time.time_ns() if now_ns is None else now_ns
        self.poll(now_ns)
        kills, defeats, deaths = self._death_counts()
        assists = self._assist_counts()
        damage = sum(row['damage'] for row in self.outgoing.values())
        taken = sum(row['damage'] for row in self.incoming.values())
        # Absence of a HealSync event is unknown, not a measured zero.  Keep
        # an explicit zero when the local actor has an exact entry so the UI
        # can distinguish "no healing" from "healing data unavailable".
        self_key = self._key(self.self_id) if self.self_id else ''
        healing = (
            int(self.healing_by_actor[self_key] or 0)
            if self_key in self.healing_by_actor
            else None
        )

        def rows(counters, total, counts, count_key, *, available=True, assist_counts=None):
            result = []
            keys = set(counters) | set(counts) | set(assist_counts or {})
            for key in keys:
                actor = counters.get(key, {}).get('actor_id') or self.tokens.get(key)
                profile = {
                    **self.token_profiles.get(key, {}),
                    **self.profiles.get(actor, {}),
                }
                value = counters.get(key, {}).get('damage', 0)
                result.append({'opponent_id': key, 'actor_id': actor or 0,
                               'name': profile.get('name') or '未知玩家',
                               'profession_id': _integer(profile.get('profession_id')),
                               'rating': profile.get('extraordinary_rating'),
                               'damage': value if available else None,
                               'damage_text': f'{value:,}' if available else '--',
                               'share_text': (f'{value / total * 100:.1f}%' if total else '0.0%') if available else '--',
                               count_key: counts.get(key, 0),
                               'assists': ((assist_counts or {}).get(key, 0)
                                           if assist_counts is not None else
                                           0 if self.duel else '--')})
            return sorted(result, key=lambda row: (-(row['damage'] or 0), -row[count_key], row['actor_id']))

        elapsed = self.elapsed_ns
        if self.active_since_ns is not None:
            elapsed += max(0, now_ns - self.active_since_ns)
        seconds = elapsed // 1_000_000_000
        known = self.started_at_ns is not None
        team_battle = (
            self.team_battle_rows(include_details=include_details)
            if include_teams else {'allies': [], 'enemies': []}
        )
        return {'active': self.active_since_ns is not None,
                'time': f'{seconds // 60:02d}:{seconds % 60:02d}',
                'result': self.result, 'status': '统计中' if self.active_since_ns is not None else '等待 PVP 战斗数据',
                # Once the result arrives, leave the "双人切磋" scene label
                # while retaining the completed counters and result below.
                'module_name': '' if self.duel_finished else self.module_name,
                'duel_active': bool(self.duel and not self.duel_finished),
                'duel_finished': self.duel_finished,
                'duel_outcome': self.duel_outcome,
                'kills': sum(kills.values()) if known and self.self_token else '--', 'deaths': deaths if known else '--',
                'assists': 0 if self.duel else sum(assists.values()) if known else '--',
                'outgoing': rows(self.outgoing, damage, kills, 'kills', assist_counts=assists),
                # All confirmed incoming hits contribute to total_taken and
                # skill/history aggregates; the death table is killers only.
                'incoming': rows(
                                 {
                                     **{
                                         key: value
                                         for key, value in self.incoming.items()
                                         if key in defeats
                                     },
                                     **{
                                         key: {
                                             'actor_id': self.tokens.get(key, 0),
                                             'damage': 0,
                                         }
                                         for key in defeats
                                         if key not in self.incoming
                                     },
                                 },
                                 taken, defeats, 'defeats',
                                 available=self.has_exact_incoming),
                'total_damage': f'{damage:,}' if known else '--',
                'total_taken': f'{taken:,}' if known and self.has_exact_incoming else '--',
                'damage': damage, 'taken': taken, 'healing': healing,
                'monsters': self.dragon_rows(include_skills=include_details),
                'monster_damage': sum(
                    row['damage_to'] for row in self.dragon_targets.values()
                ),
                'monster_taken': sum(
                    row['damage_from'] for row in self.dragon_targets.values()
                ),
                 'total_healing': (
                     f'{healing:,}'
                     if known and healing is not None
                     else '--'
                 ),
                'healing_source_available': self.has_exact_healing,
                'capture_complete': self.capture_complete,
                'incoming_source_available': self.has_exact_incoming,
                'assist_source_available': not self.duel,
                'team_size': self.team_size,
                'team_battle': bool(
                    self.team_size in (3, 6, 12, 60)
                    and (team_battle['allies'] or team_battle['enemies'])
                ),
                'allies': team_battle['allies'],
                'enemies': team_battle['enemies'],
                'elapsed_ns': elapsed,
                'session_id': self.session_id,
                'skills_outgoing': self.skill_distribution('outgoing'),
                'skills_incoming': self.skill_distribution('incoming')}

    def snapshot(self, now_ns=None):
        state = self.session_snapshot(now_ns)
        return self.hud_totals.snapshot(state) if self.hud_totals is not None else state

    def skill_distribution(self, direction='outgoing'):
        skills = self.skills_outgoing if direction == 'outgoing' else self.skills_incoming
        # Cast callbacks can create a row before any hit.  Retain those rows
        # because their cast count is meaningful, but drop placeholders with
        # no cast, hit, or damage evidence (usually numeric/unknown skill IDs
        # from a roster snapshot).
        active = [value for value in skills.values() if skill_has_activity(value)]
        total = sum(int(value.get('damage', 0) or 0) for value in active)
        return [
            dict(
                _skill_output(value),
                share=(value.get('damage', 0) or 0) / total if total else 0,
            )
            for value in sorted(
                active,
                key=lambda value: (
                    -(value.get('damage', 0) or 0),
                    -(value.get('casts', 0) or 0),
                    value.get('skill_id', 0),
                ),
            )
        ]
