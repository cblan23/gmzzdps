"""PVP protocol/production HUD regressions, independent of game access."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import msgpack

from npcap_parser_adapter import NpcapParserAdapter
from npcap_protocol import (
    NpcapProtocolDecoder, _valid_team_pvp_roster,
    _valid_team_pvp_settlement,
)
from pvp_tracker import (
    PvpTracker, WINDOWS_EPOCH, _team_pvp_settlement,
    PVP_MAX_LEAGUE_MEMBERS, PVP_MAX_MEMBERS_PER_RAID, PVP_MAX_RAIDS,
    pvp_bot_evidence,
)


SELF = 57_407_071_770_816
ENEMY = 57_314_193_098_263
OTHER = 57_205_745_171_959
SELF_TOKEN = 'AQAAAOwNkGB8AAAA'
ENEMY_TOKEN = 'AQAAAOwNGOEuAAAA'
OTHER_TOKEN = 'AQAAAOwN3nm0AAAA'
BASE_NS = 1_789_653_800_000_000_000
TEAM_INFO_ARGS = [
    2,
    {0: ENEMY_TOKEN, 2: 1_200_003, 3: 70, 4: '队友甲', 6: 1_400_007},
    {0: OTHER_TOKEN, 2: 1_200_007, 3: 70, 4: '队友乙', 6: 1_400_008},
]
FULL_ROSTER_ARGS = [{
    101: {0: [
        {0: SELF_TOKEN, 1: 'Self', 3: 1_200_002},
        {0: ENEMY_TOKEN, 1: 'Ally', 3: 1_200_003},
        {0: 'aq-ArenaAllyBot', 1: 'AllyBot', 2: 3_000_138, 3: 1_200_001},
    ]},
    102: {0: [
        {0: OTHER_TOKEN, 1: 'Enemy', 3: 1_200_007},
        {0: 'AQAAArenaEnemyTwo', 1: 'EnemyTwo', 3: 1_200_005},
        {0: 'aq-ArenaEnemyBot', 1: 'EnemyBot', 2: 3_000_174, 3: 1_200_006},
    ]},
}]
TEAM_SETTLEMENT_ARGS = [{
    101: {
        0: 1, 1: SELF_TOKEN, 2: 1_789_700_001, 3: 1_789_700_161,
        4: [
            {0: SELF_TOKEN, 2: 1_200_002, 3: 70, 4: 'Self',
             14: 89_726, 15: 0, 16: 18_580, 17: 7_594},
            {0: ENEMY_TOKEN, 2: 1_200_005, 3: 70, 4: 'Ally',
             14: 106_717, 15: 41_608, 16: 4_240, 17: 14_685, 18: 3},
            {0: 'AQAAArenaAllyTwo', 2: 1_200_003, 3: 70, 4: 'AllyTwo',
             15: 15_545, 16: 2_700, 17: 14_450},
        ],
    },
    102: {
        1: OTHER_TOKEN, 2: 1_789_700_001, 3: 1_789_700_161,
        4: [
            {0: OTHER_TOKEN, 2: 1_200_002, 3: 70, 4: 'Enemy',
             15: 48, 16: 16_241, 17: 19_341, 19: 1},
            {0: 'AQAAArenaEnemyTwo', 2: 1_200_003, 3: 70, 4: 'EnemyTwo',
             15: 29_954, 16: 5_400, 17: 18_285, 18: 1, 19: 1},
            {0: 'AQAAArenaEnemyTri', 2: 1_200_005, 3: 70, 4: 'EnemyThree',
             15: 6_814, 16: 2_700, 17: 15_787, 19: 1},
        ],
    },
}, 1_789_700_186]


def packet(method, actor=SELF, args=(), sequence=1, seconds=0, **kwargs):
    stamp = BASE_NS + int(seconds * 1_000_000_000)
    return {'method': method, 'network_entity_id': actor, 'script_entity': actor,
            'capture_source': 'npcap', 'arguments_synchronized': True,
            'capture_timestamp_ns': stamp, 'filetime_100ns': WINDOWS_EPOCH + stamp // 100,
            'capture_event_id': f'test:{sequence}', 'sequence': sequence,
            'decoded_arguments': list(args), **kwargs}


def avatar(actor, token, name, sequence=1, **kwargs):
    return packet('NpcapEntityCreated', actor, [{'entity_id': actor,
                  'entity_token': token, 'entity_class': 'AvatarActor',
                  'properties': {'Name': name, 'Level': 70, 'Profession': 1_200_001}}],
                  sequence, **kwargs)


class Pipeline:
    def __init__(self, enemy=True, *, cumulative=False, map_scoped=False):
        self.parser = NpcapParserAdapter()
        self.tracker = PvpTracker(
            cumulative=cumulative, map_scoped=map_scoped
        )
        self.sequence = 10
        self.feed(avatar(SELF, SELF_TOKEN, '本人', 1))
        self.feed(packet('RetNTP', args=[1_789_653_800_000, 1_789_653_800_030], sequence=2,
                         npcap_method_scope='local_role'))
        if enemy:
            self.feed(avatar(ENEMY, ENEMY_TOKEN, '对手', 3))

    def feed(self, record):
        updates = self.parser.process(record)
        for kind, value in updates:
            self.tracker.ingest_update(kind, value)
        for observation in self.parser.take_pvp_observations():
            self.tracker.consume(observation)
        return updates

    def rpc(self, method, actor=SELF, args=(), seconds=1, **kwargs):
        self.sequence += 1
        record = packet(method, actor, args, self.sequence, seconds=seconds, **kwargs)
        self.feed(record)
        return record

    def damage(self, amount=100, attacker=SELF, target=ENEMY, seconds=1):
        return self.rpc('OnMsgDamageSyncV2', attacker,
                        [attacker, target, 8_601_107_100_055, 0, 1, amount, 0, amount, False],
                        seconds=seconds)

    def view(self, seconds=1):
        return self.tracker.snapshot(BASE_NS + int(seconds * 1_000_000_000))


class LeagueRosterTests(unittest.TestCase):
    def test_projection_suffix_is_not_arena_bot_evidence(self):
        self.assertFalse(pvp_bot_evidence({'name': 'Player·投影'}))
        self.assertTrue(pvp_bot_evidence({
            'name': 'Player·投影',
            'ai_template_id': 3_000_174,
        }))

    def test_real_raid_numbers_and_subgroups_replace_fake_paging(self):
        tracker = PvpTracker(map_scoped=True)
        tracker._set_identity(SELF, SELF_TOKEN)
        tracker.map_id = 5_200_131
        args = [{0: {
            101: {6: 1, 17: {
                11: {0: 1, 1: [
                    {2: ENEMY_TOKEN, 5: "First raid·投影", 8: 1_200_003, 27: 90_000},
                ]},
            }},
            102: {6: 2, 17: {
                21: {0: 1, 1: [
                    {2: SELF_TOKEN, 5: "Self", 8: 1_200_002, 27: 95_000},
                    {2: OTHER_TOKEN, 5: "Second raid", 8: 1_200_007, 27: 101_000},
                ]},
            }},
        }}]
        observation = {
            'record': {'method': 'OnMsgSyncLeagueInfo',
                       'decoded_arguments': args, 'capture_event_id': 'league-1'},
            'timestamp_ns': BASE_NS + 40_000_000_000,
            'context': {'self_id': SELF, 'self_token': SELF_TOKEN,
                        'map_id': 5_200_131},
            'updates': [], 'events': [],
        }

        self.assertTrue(tracker.consume(observation))
        league = tracker.pvp_league_roster
        self.assertEqual(league['raid_number'], 2)
        self.assertEqual(
            [(raid['number'], sum(len(group) for group in raid['groups'].values()))
             for raid in league['raids'].values()],
            [(1, 1), (2, 2)],
        )
        self.assertEqual(set(tracker.pvp_allies), {ENEMY_TOKEN, OTHER_TOKEN})
        self.assertFalse(tracker.pvp_allies[ENEMY_TOKEN]['is_ai'])

    def test_league_roster_is_map_agnostic_and_caps_at_five_thirty_member_raids(self):
        raids = {}
        for raid_number in range(1, PVP_MAX_RAIDS + 2):
            members = []
            for index in range(PVP_MAX_MEMBERS_PER_RAID + 5):
                token = (
                    SELF_TOKEN
                    if raid_number == 1 and index == 0
                    else f'AQAAA{raid_number:02d}{index:03d}token'
                )
                members.append({
                    2: token, 5: f'Player {raid_number}-{index}',
                    8: 1_200_001, 9: 70, 27: 90_000,
                })
            groups = {
                subgroup: {
                    0: subgroup,
                    1: members[(subgroup - 1) * 7: subgroup * 7],
                }
                for subgroup in range(1, 6)
            }
            raids[raid_number] = {6: raid_number, 17: groups}
        args = [{0: raids}]
        tracker = PvpTracker(map_scoped=True)
        tracker._set_identity(SELF, SELF_TOKEN)
        tracker.map_id = 5_200_131
        observation = {
            'record': {
                'method': 'OnMsgSyncLeagueInfo',
                'decoded_arguments': args,
                'capture_event_id': 'league-cap',
            },
            'timestamp_ns': BASE_NS + 40_000_000_000,
            'context': {
                'self_id': SELF, 'self_token': SELF_TOKEN,
                'map_id': 5_200_131,
            },
            'updates': [], 'events': [],
        }

        self.assertTrue(tracker.consume(observation))
        stored_raids = tracker.pvp_league_roster['raids']
        self.assertEqual(len(stored_raids), PVP_MAX_RAIDS)
        self.assertEqual(
            sum(
                len(group)
                for raid in stored_raids.values()
                for group in raid['groups'].values()
            ),
            PVP_MAX_LEAGUE_MEMBERS,
        )
        self.assertTrue(all(
            sum(len(group) for group in raid['groups'].values())
            <= PVP_MAX_MEMBERS_PER_RAID
            for raid in stored_raids.values()
        ))


class PvpRosterPerformanceTests(unittest.TestCase):
    def test_unchanged_roster_does_not_rebuild_all_players_but_name_change_does(self):
        tracker = PvpTracker(map_scoped=True)
        tracker._set_identity(SELF, SELF_TOKEN)
        args = [2,
                {0: SELF_TOKEN, 2: 1200002, 3: 70, 4: 'Self', 6: 1400039},
                {0: ENEMY_TOKEN, 2: 1200007, 3: 70, 4: 'Enemy', 6: 1400036}]
        tracker._replace_pvp_allies(args)
        rebuild = Mock(wraps=tracker._rebuild_pvp_template_roster)
        tracker._rebuild_pvp_template_roster = rebuild
        tracker._replace_pvp_allies(deepcopy(args))
        rebuild.assert_not_called()
        renamed = deepcopy(args)
        renamed[2][4] = 'Renamed'
        tracker._replace_pvp_allies(renamed)
        rebuild.assert_called_once()
        self.assertEqual(tracker.pvp_enemies[ENEMY_TOKEN]['name'], 'Renamed')


class PvpTrackerTests(unittest.TestCase):
    def test_hunter_dragon_hits_do_not_enter_player_damage(self):
        tracker = PvpTracker(map_scoped=True)
        tracker._set_identity(SELF, SELF_TOKEN)
        tracker.map_id = 5200167
        tracker._profile({
            "entity_id": ENEMY, "entity_type": "NPC",
            "template_id": 7100625, "name": "战争巨龙",
        })
        first = BASE_NS + 1_000_000_000
        self.assertTrue(tracker._accept_damage({
            "attacker_id": SELF, "target_id": ENEMY,
            "damage": 4500, "raw_damage": 4500,
            "skill_id": 42, "damage_source": "network_exact",
        }, first))
        self.assertTrue(tracker._accept_damage({
            "attacker_id": ENEMY, "target_id": SELF,
            "damage": 300, "raw_damage": 300,
            "skill_id": 43, "damage_source": "network_exact",
        }, first + 1_000_000_000))
        self.assertTrue(tracker._accept_life(
            "OnMsgEntityDead", ENEMY, [0, SELF_TOKEN],
            first + 2_000_000_000,
        ))

        snapshot = tracker.session_snapshot(first + 3_000_000_000)

        self.assertEqual(snapshot["damage"], 0)
        self.assertEqual(snapshot["taken"], 0)
        self.assertEqual(snapshot["kills"], 0)
        self.assertEqual(snapshot["monster_damage"], 4500)
        self.assertEqual(snapshot["monster_taken"], 300)
        self.assertTrue(snapshot["monsters"][0]["defeated"])
        self.assertEqual(snapshot["monsters"][0]["skills_outgoing"][0]["damage"], 4500)

    def test_known_killer_token_counts_death_without_actor_binding(self):
        tracker = PvpTracker(map_scoped=True)
        tracker._set_identity(SELF, SELF_TOKEN)
        tracker.token_profiles[OTHER_TOKEN] = {
            'entity_type': 'Player', 'name': '已确认敌人',
        }
        tracker.death_events.append({
            'victim_id': SELF, 'victim_key': SELF_TOKEN,
            'source_token': OTHER_TOKEN,
        })

        kills, defeats, deaths = tracker._death_counts()

        self.assertEqual(kills, {})
        self.assertEqual(defeats, {OTHER_TOKEN: 1})
        self.assertEqual(deaths, 1)

    def test_enemy_damage_to_self_survives_late_team_roster(self):
        pipeline = Pipeline(enemy=False, map_scoped=True)
        pipeline.feed(avatar(OTHER, OTHER_TOKEN, 'Enemy', sequence=70))
        pipeline.damage(2039, attacker=OTHER, target=SELF, seconds=2)
        pipeline.rpc(
            'RetGetTeamArenaBattleInfo', args=FULL_ROSTER_ARGS, seconds=3
        )

        enemy = next(
            row for row in pipeline.tracker.team_battle_rows()['enemies']
            if row['character_id'] == OTHER_TOKEN
        )
        self.assertEqual(enemy['damage_to_self'], 2039)
        self.assertNotEqual(enemy['damage'], 2039)

    def test_six_player_settlement_field_23_supplies_official_assists(self):
        def member(token, index, *, kills=0, deaths=0, assists=0):
            fields = {0: token, 2: 1_200_002, 3: 70, 4: f'Player {index}'}
            if kills:
                fields[18] = kills
            if deaths:
                fields[19] = deaths
            if assists:
                fields[23] = assists
            return fields

        own = [
            member(SELF_TOKEN, 0, assists=3),
            member(ENEMY_TOKEN, 1, assists=2),
            member(OTHER_TOKEN, 2, kills=2, assists=1),
            member('AQAAATeamAllyFour', 3, kills=1, assists=2),
            member('AQAAATeamAllyFive', 4, assists=3),
            member('AQAAATeamAllySix', 5, assists=3),
        ]
        enemy = [
            member(f'AQAAAEnemyBot{i:02d}', i, deaths=1 if i < 3 else 0)
            for i in range(6)
        ]
        settlement = _team_pvp_settlement([
            {101: {0: 1, 4: own}, 102: {4: enemy}}, 123,
        ], SELF_TOKEN)
        self.assertIsNotNone(settlement)
        self.assertEqual(settlement['allies'][0]['assists'], 3)
        self.assertEqual(settlement['enemies'][0]['assists'], 0)

    def test_three_player_settlement_does_not_treat_field_23_as_assists(self):
        arguments = deepcopy(TEAM_SETTLEMENT_ARGS)
        arguments[0][101][4] = arguments[0][101][4][:3]
        arguments[0][102][4] = arguments[0][102][4][:3]
        for member in arguments[0][101][4] + arguments[0][102][4]:
            member[23] = 99
        # The parser validates equal team sizes; this fixture is only a
        # contract check for the 3V3 assist gate, so use the helper directly
        # with a compact valid 3-player shape.
        valid = [{
            101: {0: 1, 4: [
                {0: SELF_TOKEN, 2: 1_200_002, 3: 70, 4: 'Self', 23: 99},
                {0: ENEMY_TOKEN, 2: 1_200_005, 3: 70, 4: 'Ally', 23: 99},
                {0: OTHER_TOKEN, 2: 1_200_003, 3: 70, 4: 'AllyTwo', 23: 99},
            ]},
            102: {4: [
                {0: 'AQAAArenaEnemyOne', 2: 1_200_002, 3: 70, 4: 'Enemy', 23: 99},
                {0: 'AQAAArenaEnemyTwo', 2: 1_200_003, 3: 70, 4: 'EnemyTwo', 23: 99},
                {0: 'AQAAArenaEnemyTri', 2: 1_200_005, 3: 70, 4: 'EnemyThree', 23: 99},
            ]},
        }, 123]
        settlement = _team_pvp_settlement(valid, SELF_TOKEN)
        self.assertEqual(settlement['allies'][0]['assists'], None)

    def test_four_faction_settlement_keeps_all_other_factions_as_enemies(self):
        def member(token, index):
            return {0: token, 2: 1_200_001 + index, 3: 70,
                    4: f'Player{index}', 15: index * 10}

        teams = {}
        for team_id in range(101, 105):
            tokens = [
                SELF_TOKEN if team_id == 101 and index == 0
                else f'AQAAFourFaction{team_id}{index:02d}'
                for index in range(3)
            ]
            teams[team_id] = {
                0: 1 if team_id == 101 else 0,
                4: [member(token, index) for index, token in enumerate(tokens)],
            }

        settlement = _team_pvp_settlement([teams, 123], SELF_TOKEN)
        self.assertIsNotNone(settlement)
        self.assertEqual(settlement['team_size'], 3)
        self.assertEqual(len(settlement['allies']), 3)
        self.assertEqual(len(settlement['enemies']), 9)

    def test_four_faction_wire_roster_and_settlement_accept_full_capacity(self):
        roster = {}
        settled = {}
        for faction in range(4):
            team_id = 101 + faction
            tokens = [
                SELF_TOKEN if faction == 0 and index == 0
                else f'AQAAFour{faction:01d}Player{index:02d}'
                for index in range(60)
            ]
            roster[team_id] = {0: [
                {0: token, 1: f'Player{faction}-{index}', 3: 1_200_001}
                for index, token in enumerate(tokens)
            ]}
            settled[team_id] = {
                0: 1 if faction == 0 else 0,
                1: tokens[0], 2: 1_789_700_001, 3: 1_789_700_161,
                4: [{0: token, 2: 1_200_001, 3: 70,
                     4: f'Player{faction}-{index}'}
                    for index, token in enumerate(tokens)],
            }

        self.assertTrue(_valid_team_pvp_roster([roster]))
        self.assertTrue(_valid_team_pvp_settlement([settled, 1_789_700_186]))
        normalized = _team_pvp_settlement([settled, 1_789_700_186], SELF_TOKEN)
        self.assertEqual(len(normalized['allies']), 60)
        self.assertEqual(len(normalized['enemies']), 180)
        roster[104][0].pop()
        settled[104][4].pop()
        self.assertTrue(_valid_team_pvp_roster([roster]))
        self.assertTrue(_valid_team_pvp_settlement([settled, 1_789_700_186]))
        self.assertEqual(
            len(_team_pvp_settlement([settled, 1_789_700_186], SELF_TOKEN)['enemies']),
            179,
        )

    def test_team_info_does_not_infer_ai_from_token_prefix(self):
        p = Pipeline(enemy=False, map_scoped=True)
        bot_token = 'aqFyZFqM2Vv1Z3Uy'
        p.rpc('OnMsgSyncTeamPVPInfo', args=[
            2,
            {0: SELF_TOKEN, 2: 1_200_002, 3: 70, 4: '本人', 6: 1_400_007},
            {0: bot_token, 2: 1_200_003, 3: 70, 4: '人机队友', 6: 1_400_007},
        ])

        members = p.tracker.pvp_team_members()
        self.assertEqual(len(members), 1)
        self.assertFalse(members[0]['is_ai'])

    def test_profile_prefix_ai_flag_is_overridden_without_bot_evidence(self):
        p = Pipeline(enemy=False, map_scoped=True)
        human_style_token = 'agMDAwMDAwMDAwMD'
        p.tracker._profile({
            'entity_id': OTHER,
            'user_token': human_style_token,
            'name': 'Real arena player',
            'profession_id': 1_200_003,
            'is_ai': True,
            'entity_type': 'Player',
        })

        self.assertFalse(
            p.tracker.profiles[OTHER].get('is_ai')
        )

    def test_live_team_snapshot_skips_detail_copies_but_full_snapshot_keeps_them(self):
        p = Pipeline()
        equipment = {
            'captured_at_ns': BASE_NS,
            'equipment': [{'slot': 1, 'item_id': 12345}],
        }
        p.tracker.ingest_update('equipment_profile', {
            'user_token': SELF_TOKEN,
            'actor_id': SELF,
            'equipment_snapshot': equipment,
        })
        p.damage(123)

        full = p.tracker.team_battle_rows()['allies'][0]
        light = p.tracker.session_snapshot(include_details=False)['allies'][0]

        self.assertEqual(full['equipment_snapshot'], equipment)
        self.assertTrue(full['skills'])
        self.assertIsNone(light['equipment_snapshot'])
        self.assertEqual(light['skills'], [])
        self.assertEqual(light['damage'], full['damage'])

        class UncopyableEquipment(dict):
            def __deepcopy__(self, _memo):
                raise AssertionError('live HUD copied full equipment')

        p.tracker.profiles[SELF]['equipment_snapshot'] = UncopyableEquipment()
        p.tracker.token_profiles[SELF_TOKEN]['equipment_snapshot'] = UncopyableEquipment()
        self.assertIsNone(
            p.tracker.session_snapshot(include_details=False)['allies'][0][
                'equipment_snapshot'
            ]
        )

    def test_new_formal_roster_replaces_provisional_names_incrementally(self):
        p = Pipeline(enemy=False, map_scoped=True)
        p.tracker.team_size = 12
        stale_tokens = [f"stale-token-{index}" for index in range(8)]
        for token in stale_tokens:
            p.tracker.pvp_allies[token] = {
                'user_token': token, 'name': '玩家识别中', 'side': 'ally'
            }
        local_template = 1_400_007
        enemy_template = 1_400_008

        first = p.tracker._replace_pvp_allies([
            2,
            {0: SELF_TOKEN, 2: 1_200_002, 3: 70, 4: 'Self', 6: local_template},
            {0: ENEMY_TOKEN, 2: 1_200_003, 3: 70, 4: 'Ally', 6: local_template},
        ])

        self.assertTrue(first)
        self.assertEqual(list(p.tracker.pvp_allies), [ENEMY_TOKEN])
        self.assertTrue(all(token not in p.tracker.pvp_allies for token in stale_tokens))

        p.tracker._replace_pvp_allies([
            1,
            {0: OTHER_TOKEN, 2: 1_200_007, 3: 70, 4: 'Enemy', 6: enemy_template},
        ])
        self.assertEqual(list(p.tracker.pvp_allies), [ENEMY_TOKEN])
        self.assertEqual(list(p.tracker.pvp_enemies), [OTHER_TOKEN])

    def test_live_arena_party_members_do_not_disappear_on_partial_pvp_info(self):
        p = Pipeline(enemy=False, map_scoped=True)
        p.tracker.team_size = 12
        p.rpc('OnSyncTeamGroupPropsForceRefresh', args=[ENEMY_TOKEN], seconds=1)
        p.rpc('OnSyncTeamGroupPropsForceRefresh', args=[OTHER_TOKEN], seconds=2)
        self.assertEqual(set(p.tracker.pvp_allies), {ENEMY_TOKEN, OTHER_TOKEN})

        enemy_token = 'AQAAArenaEnemyOne'
        partial = [
            2,
            {0: SELF_TOKEN, 2: 1_200_002, 3: 70, 4: '本人', 6: 1_400_007},
            {0: enemy_token, 2: 1_200_003, 3: 70, 4: '敌方', 6: 1_400_008},
        ]
        p.rpc('OnMsgSyncTeamPVPInfo', args=partial, seconds=3)
        self.assertEqual(set(p.tracker.pvp_allies), {ENEMY_TOKEN, OTHER_TOKEN})
        self.assertEqual(set(p.tracker.pvp_enemies), {enemy_token})
        p.rpc('OnMsgSyncTeamPVPInfo', args=partial, seconds=4)
        self.assertEqual(set(p.tracker.pvp_allies), {ENEMY_TOKEN, OTHER_TOKEN})

    def test_arena_hud_does_not_borrow_old_pve_party_before_roster_arrives(self):
        from test_combat_model import DpsWindow

        p = Pipeline(enemy=False, map_scoped=True)
        window = object.__new__(DpsWindow)
        window.pvp_recording = SimpleNamespace(
            display_tracker=p.tracker, party_seen=False, party_active=True,
            active=True, duel_recording=None, map_id=5_203_003,
        )
        window.model = SimpleNamespace(
            party_active=True, party_ids={999}, provisional_party_ids=set(),
            self_id=SELF, actor_character_ids={}, non_player_actor_ids=set(),
            actor_profession_id=lambda _actor: 0,
        )
        window.hide_names = False
        window._main_visible_roster_members = lambda: {999}
        window._main_extraordinary_rating = lambda _actor: None
        window._equipment_summary_for_actor = lambda _actor, _token='': {}

        self.assertEqual(window._pvp_current_teammates(), [])
        self.assertEqual(
            [row['user_token'] for row in window._pvp_team_composition_rows()],
            [SELF_TOKEN],
        )

    def test_exact_damage_reaches_pvp_despite_pve_filter(self):
        p = Pipeline()
        updates = p.damage(431)
        event = next(value for kind, value in p.parser.process(
            packet('OnMsgDamageSyncV2', args=[SELF, ENEMY, 8_601_107_100_055, 0, 1, 414, 0, 414, False], sequence=99)
        ) if kind == 'event')
        self.assertFalse(p.parser.should_forward_damage_event(event))
        self.assertEqual(p.view()['damage'], 431)
        self.assertEqual(p.view()['outgoing'][0]['name'], '对手')

    def test_hits_and_percentages_and_incoming_are_independent(self):
        p = Pipeline()
        p.feed(avatar(OTHER, OTHER_TOKEN, '另一个玩家', 4))
        p.damage(300)
        p.damage(100, target=OTHER, seconds=2)
        p.damage(50, attacker=ENEMY, target=SELF, seconds=3)
        view = p.view(3)
        self.assertEqual(view['total_damage'], '400')
        self.assertEqual(view['total_taken'], '50')
        self.assertEqual([row['share_text'] for row in view['outgoing']], ['75.0%', '25.0%'])
        self.assertEqual(view['skills_outgoing'][0]['hits'], 2)
        self.assertEqual(view['skills_outgoing'][0]['damage'], 400)
        self.assertEqual(view['skills_outgoing'][0]['share'], 1)
        self.assertEqual(view['skills_incoming'][0]['hits'], 1)

    def test_native_incoming_exact_event_uses_same_deduped_pipeline(self):
        p = Pipeline()
        updates = p.parser.process_native_damage({
            'capture_source': 'native_damage', 'capture_timestamp_ns': BASE_NS,
            'filetime_100ns': WINDOWS_EPOCH + BASE_NS // 100,
            'capture_event_id': 'native:incoming:1', 'sequence': 1,
            'attacker_id': ENEMY, 'target_id': SELF,
            'arg4_u64': 860230100, 'arg5_i32': 0, 'arg6_i32': 2,
            'arg7_i32': 524, 'arg8_i32': 0, 'arg9_i32': 524,
            'raw_damage': 524, 'damage': 524, 'arg10_bool': False,
            'local_player_id': SELF,
        })
        for kind, value in updates:
            p.tracker.ingest_update(kind, value)
        for observation in p.parser.take_pvp_observations():
            p.tracker.consume(observation)
        view = p.view()
        self.assertEqual(view['total_taken'], '524')
        self.assertEqual(view['incoming'], [])
        self.assertEqual(p.tracker.incoming[ENEMY_TOKEN]['damage'], 524)
        self.assertEqual(view['skills_incoming'][0]['damage'], 524)

    def test_native_then_matching_network_damage_counts_once(self):
        p = Pipeline()
        native = {'capture_source': 'native_damage', 'capture_timestamp_ns': BASE_NS,
                  'filetime_100ns': WINDOWS_EPOCH + BASE_NS // 100,
                  'capture_event_id': 'native:out:1', 'sequence': 1,
                  'attacker_id': SELF, 'target_id': ENEMY, 'arg4_u64': 860230100,
                  'arg5_i32': 0, 'arg6_i32': 1, 'arg7_i32': 431, 'arg8_i32': 0,
                  'arg9_i32': 431, 'raw_damage': 431, 'damage': 431,
                  'arg10_bool': False, 'local_player_id': SELF}
        updates = p.parser.process_native_damage(native)
        for kind, value in updates:
            p.tracker.ingest_update(kind, value)
        for observation in p.parser.take_pvp_observations():
            p.tracker.consume(observation)
        p.feed(packet('OnMsgDamageSyncV2', args=[SELF, ENEMY, 860230100, 0, 1, 431, 0, 431, False], sequence=45))
        self.assertEqual(p.view()['damage'], 431)
        self.assertEqual(p.view()['skills_outgoing'][0]['hits'], 1)

    def test_beaten_sync_requires_exact_five_field_local_target(self):
        p = Pipeline()
        p.rpc('OnMsgBeatenSyncV2', args=[ENEMY, SELF, 860211000], seconds=1)
        p.rpc('OnMsgBeatenSyncV2', args=[ENEMY, OTHER, 860211000, 509, 509], seconds=2)
        self.assertEqual(p.view(2)['total_taken'], '--')
        p.rpc('OnMsgBeatenSyncV2', args=[ENEMY, SELF, 860211000, 509, 509], seconds=3)
        self.assertEqual(p.view(3)['total_taken'], '509')

    def test_matching_damage_and_beaten_sync_for_one_hit_count_once(self):
        p = Pipeline()
        p.rpc('OnMsgDamageSyncV2', actor=ENEMY,
              args=[ENEMY, SELF, 860211000, 0, 1, 509, 0, 509, False], seconds=1)
        p.rpc('OnMsgBeatenSyncV2', args=[ENEMY, SELF, 860211000, 509, 509], seconds=1.01)
        self.assertEqual(p.view(2)['total_taken'], '509')
        self.assertEqual(p.view(2)['skills_incoming'][0]['hits'], 1)

    def test_outgoing_beaten_sync_uses_effective_amount_and_deduplicates_damage_sync(self):
        p = Pipeline()
        p.rpc('OnMsgBeatenSyncV2', actor=ENEMY,
              args=[SELF, ENEMY, 860200400, 120, 100], seconds=1)
        self.assertEqual(p.view()['damage'], 100)
        p.rpc('OnMsgDamageSyncV2', args=[SELF, ENEMY, 860200400, 0, 1, 120, 0, 100, False], seconds=1.01)
        self.assertEqual(p.view()['damage'], 100)
        self.assertEqual(p.view()['skills_outgoing'][0]['hits'], 1)
        p.rpc('OnMsgBeatenSyncV2', actor=ENEMY,
              args=[SELF, ENEMY, 860200400, 1039, 0], seconds=2)
        self.assertEqual(p.view(2)['damage'], 100)

    def test_current_beaten_layout_uses_rpc_owner_not_slot_order(self):
        p = Pipeline()
        p.damage(714, seconds=1)
        # Current TeamPvpSpace traffic places the affected local actor in
        # slot 0.  It is incoming and cannot inflate the official outgoing
        # DamageSync total.
        p.rpc('OnMsgBeatenSyncV2', actor=SELF,
              args=[SELF, ENEMY, 860211000, 28_082, 28_082], seconds=2)
        view = p.view(2)
        self.assertEqual(view['damage'], 714)
        self.assertEqual(view['taken'], 28_082)

    def test_zero_effective_beaten_damage_never_falls_back_to_raw(self):
        p = Pipeline()
        p.rpc('OnMsgIndividualPVPState', args=[3, 1000], seconds=0.5)
        p.rpc('OnMsgBeatenSyncV2', args=[ENEMY, SELF, 870012100, 1039, 0], seconds=1)
        self.assertEqual(p.view()['taken'], 0)
        self.assertEqual(p.view()['incoming'], [])
        self.assertEqual(p.view()['skills_incoming'], [])
        self.assertEqual(p.view()['total_taken'], '0')
        self.assertTrue(p.view()['incoming_source_available'])

    def test_incoming_hits_do_not_populate_death_table_until_actual_death(self):
        p = Pipeline()
        p.feed(avatar(OTHER, OTHER_TOKEN, '另一个玩家', 4))
        p.damage(80, attacker=ENEMY, target=SELF, seconds=1)
        p.damage(20, attacker=OTHER, target=SELF, seconds=2)
        self.assertEqual(p.view(2)['total_taken'], '100')
        self.assertEqual(p.view(2)['incoming'], [])
        p.rpc('OnMsgEntityDead', args=[0, ENEMY_TOKEN], seconds=3)
        view = p.view(3)
        self.assertEqual(view['deaths'], 1)
        self.assertEqual([row['opponent_id'] for row in view['incoming']], [ENEMY_TOKEN])
        self.assertEqual(view['incoming'][0]['damage'], 80)
        self.assertEqual(view['incoming'][0]['defeats'], 1)

    def test_actual_duel_death_after_result_does_not_double_fixed_loss_counter(self):
        p = Pipeline()
        p.rpc('OnMsgIndividualPVPState', args=[3, 1000], seconds=1)
        p.damage(80, attacker=ENEMY, target=SELF, seconds=2)
        p.rpc('OnMsgIndividualPVPResult', args=[2], seconds=3)
        self.assertEqual(p.view(3)['deaths'], 1)
        p.rpc('OnMsgEntityDead', args=[0, ENEMY_TOKEN], seconds=3.01)
        p.rpc('OnMsgEntityDead', args=[0, ENEMY_TOKEN], seconds=3.02)
        view = p.view(100)
        self.assertEqual(view['deaths'], 1)
        self.assertEqual(view['incoming'][0]['defeats'], 1)
        self.assertEqual(view['result'], '失败')
        self.assertEqual(view['time'], '00:02')
        self.assertFalse(view['active'])

    def test_character_reset_drops_queued_old_role_observations(self):
        p = Pipeline()
        p.damage(100)
        self.assertTrue(p.parser.take_pvp_observations() == [])
        # Queue a raw old-role observation without delivering it to tracker.
        p.parser.process(packet('OnMsgDamageSyncV2', args=[SELF, ENEMY, 860230100, 0, 1, 200, 0, 200, False], sequence=45))
        self.assertTrue(p.parser.completed_pvp_observations)
        p.parser._reset_local_role_session(OTHER, packet('RetNTP', OTHER, [], sequence=46),
                                           authoritative_self_token=OTHER_TOKEN)
        self.assertFalse(p.parser.completed_pvp_observations)

    def test_missing_incoming_stays_unknown_while_observed_assists_start_at_zero(self):
        p = Pipeline()
        p.damage()
        view = p.view()
        self.assertEqual(view['total_taken'], '--')
        self.assertEqual(view['assists'], 0)
        self.assertFalse(view['incoming_source_available'])
        self.assertTrue(view['assist_source_available'])

    def test_damage_contribution_then_teammate_kill_counts_one_assist(self):
        p = Pipeline()
        p.feed(avatar(OTHER, OTHER_TOKEN, '队友', 4))
        p.damage(180, seconds=1)
        p.rpc('OnMsgEntityDead', actor=ENEMY, args=[0, OTHER_TOKEN], seconds=2)
        view = p.view(2)
        self.assertEqual((view['kills'], view['assists']), (0, 1))
        self.assertEqual(view['outgoing'][0]['assists'], 1)
        p.rpc('OnMsgEntityRelive', actor=ENEMY, args=[0, 0, 0, OTHER_TOKEN], seconds=3)
        p.damage(90, seconds=4)
        p.rpc('OnMsgEntityDead', actor=ENEMY, args=[0, SELF_TOKEN], seconds=5)
        view = p.view(5)
        self.assertEqual((view['kills'], view['assists']), (1, 1))

    def test_team_arena_assist_requires_verified_teammate_kill(self):
        p = Pipeline(map_scoped=True)
        p.tracker.team_size = 3
        p.feed(avatar(OTHER, OTHER_TOKEN, '队友', 4))
        p.rpc('OnMsgSyncTeamPVPInfo', args=[
            3,
            {0: SELF_TOKEN, 2: 1_200_002, 3: 70, 4: '本人', 6: 1_400_007},
            {0: OTHER_TOKEN, 2: 1_200_003, 3: 70, 4: '队友', 6: 1_400_007},
            {0: ENEMY_TOKEN, 2: 1_200_004, 3: 70, 4: '敌方', 6: 1_400_008},
        ])
        p.damage(180, seconds=2)
        p.rpc('OnMsgEntityDead', actor=ENEMY, args=[0, ''], seconds=3)
        self.assertEqual(p.view(3)['assists'], 0)

        p.rpc('OnMsgEntityRelive', actor=ENEMY, args=[0, 0, 0, OTHER_TOKEN], seconds=4)
        p.damage(90, seconds=5)
        p.rpc('OnMsgEntityDead', actor=ENEMY, args=[0, OTHER_TOKEN], seconds=6)
        self.assertEqual(p.view(6)['assists'], 1)

    def test_other_players_pve_and_dummy_hits_never_count(self):
        p = Pipeline()
        npc = 57_000_000_000_001
        p.feed(packet('NpcapEntityCreated', npc, [{'entity_id': npc, 'entity_token': 'npc',
                      'entity_class': 'NpcActor', 'properties': {'TemplateID': 99999, 'BossType': 1}}], sequence=6))
        p.damage(100, target=npc)
        p.damage(100, attacker=npc, target=SELF)
        p.feed(avatar(OTHER, OTHER_TOKEN, '第三人', 4))
        p.damage(100, attacker=OTHER, target=ENEMY)
        self.assertEqual(p.view()['outgoing'], [])
        self.assertEqual(p.view()['incoming'], [])
        self.assertEqual(p.view()['total_damage'], '--')

    def test_unknown_opponent_is_filled_only_by_authoritative_identity(self):
        p = Pipeline(enemy=False)
        p.damage(180)
        self.assertEqual(p.view()['outgoing'], [])
        p.feed(avatar(ENEMY, ENEMY_TOKEN, '后来识别到的玩家', 20, seconds=2))
        self.assertEqual(p.view(2)['damage'], 180)
        self.assertEqual(p.view(2)['outgoing'][0]['name'], '后来识别到的玩家')
        self.assertEqual(p.view(2)['skills_outgoing'][0]['hits'], 1)

    def test_unknown_target_later_confirmed_npc_is_discarded(self):
        p = Pipeline(enemy=False)
        p.damage(180)
        p.feed(packet('NpcapEntityCreated', ENEMY, [{'entity_id': ENEMY, 'entity_token': 'npc',
                      'entity_class': 'NpcActor', 'properties': {'TemplateID': 99999, 'BossType': 1}}], sequence=20))
        self.assertEqual(p.view()['damage'], 0)
        self.assertEqual(len(p.tracker.pending), 0)

    def test_repeated_wire_record_is_deduplicated_not_equal_hits(self):
        p = Pipeline()
        first = p.damage(100)
        observations = p.parser.take_pvp_observations()
        # Replay the same frozen observation through a duplicate raw record.
        p.feed(first)
        self.assertEqual(p.view()['damage'], 100)
        p.damage(100)
        self.assertEqual(p.view()['damage'], 200)
        self.assertEqual(p.view()['skills_outgoing'][0]['hits'], 2)

    def test_enemy_dead_is_observed_even_when_local_role_gate_rejects_it(self):
        p = Pipeline()
        p.damage()
        updates = p.rpc('OnMsgEntityDead', ENEMY, [0, SELF_TOKEN], seconds=2,
                        npcap_method_scope='local_role')
        self.assertIsInstance(updates, dict)
        self.assertEqual(p.view(2)['kills'], 1)
        self.assertEqual(p.view(2)['outgoing'][0]['kills'], 1)
        p.rpc('OnMsgEntityDead', ENEMY, [0, SELF_TOKEN], seconds=3,
              npcap_method_scope='local_role')
        self.assertEqual(p.view(3)['kills'], 1)

    def test_relive_recipient_not_reviver_and_multiple_deaths(self):
        p = Pipeline()
        p.damage()
        p.rpc('OnMsgEntityDead', ENEMY, [0, SELF_TOKEN], seconds=2)
        p.rpc('OnMsgEntityRelive', ENEMY, [103, {}, {}, SELF_TOKEN], seconds=3)
        self.assertEqual(p.view(3)['deaths'], 0)
        p.damage(seconds=4)
        p.rpc('OnMsgEntityDead', ENEMY, [0, SELF_TOKEN], seconds=5)
        self.assertEqual(p.view(5)['kills'], 2)
        p.rpc('OnMsgEntityDead', SELF, [0, ENEMY_TOKEN], seconds=6)
        view = p.view(60)
        self.assertEqual(view['deaths'], 1)
        self.assertEqual(view['incoming'][0]['name'], '对手')
        self.assertEqual(view['incoming'][0]['defeats'], 1)
        self.assertEqual(view['incoming'][0]['damage_text'], '--')
        self.assertEqual(view['time'], '00:05')
        self.assertFalse(view['active'])
        p.rpc('OnMsgEntityRelive', SELF, [102, {}, {}, ''], seconds=61)
        p.damage(seconds=62)
        self.assertEqual(p.view(63)['deaths'], 1)
        self.assertEqual(p.view(63)['kills'], 2)
        self.assertEqual(p.view(63)['time'], '00:06')

    def test_duel_result_counts_without_entity_death_and_new_duel_resets(self):
        p = Pipeline()
        p.rpc('OnMsgIndividualPVPState', args=[2, 1_789_653_800_000, 0, 0, 0], seconds=1)
        p.rpc('OnMsgIndividualPVPLeave', args=[2], seconds=1.01)
        self.assertEqual(p.view(1)['result'], '准备中')
        p.rpc('OnMsgIndividualPVPState', args=[3, 1_789_653_805_000, 0, 0, 0], seconds=6)
        p.damage(200, seconds=7)
        p.rpc('OnMsgIndividualPVPState', args=[4, 1_789_653_825_000, 0, 0, 0], seconds=26)
        p.rpc('OnMsgIndividualPVPResult', args=[1], seconds=27)
        view = p.view(70)
        self.assertEqual(view['time'], '00:20')
        self.assertEqual(view['kills'], 1)
        self.assertEqual(view['deaths'], 0)
        self.assertEqual(view['result'], '胜利')
        self.assertEqual(view['module_name'], '')
        self.assertFalse(view['duel_active'])
        self.assertEqual(view['outgoing'][0]['kills'], 1)
        p.damage(999, seconds=28)
        self.assertEqual(p.view(70)['damage'], 200)
        p.rpc('OnMsgIndividualPVPState', args=[2, 1_789_653_900_000, 0, 0, 0], seconds=100)
        self.assertEqual(p.view(100)['outgoing'], [])
        p.rpc('OnMsgIndividualPVPState', args=[3, 1_789_653_905_000, 0, 0, 0], seconds=105)
        p.damage(321, seconds=106)
        self.assertEqual(p.view(106)['damage'], 321)

    def test_duel_health_balance_only_estimates_after_fresh_result(self):
        p = Pipeline()
        p.rpc('OnMsgIndividualPVPState', args=[3, 1000], seconds=1)
        p.tracker.ingest_update('actor_health', {
            'entity_id': SELF, 'health_source': 'bound_realtime_hp',
            'current_hp': 1000, 'max_hp': 1000,
            'capture_timestamp_ns': BASE_NS + 1_000_000_000,
        })
        p.damage(800, attacker=ENEMY, target=SELF, seconds=2)
        p.tracker.ingest_update('actor_health', {
            'entity_id': SELF, 'health_source': 'bound_realtime_hp',
            'current_hp': 500, 'max_hp': 1000,
            'capture_timestamp_ns': BASE_NS + 2_500_000_000,
        })
        p.rpc('OnMsgIndividualPVPResult', args=[2], seconds=3)
        self.assertEqual(p.tracker.duel_healing_estimate(), 300)
        # The duel scene can clear HP or heal the player after the result.
        p.tracker.ingest_update('actor_health', {
            'entity_id': SELF, 'health_source': 'bound_realtime_hp',
            'current_hp': 0, 'max_hp': 1000,
            'capture_timestamp_ns': BASE_NS + 4_000_000_000,
        })
        self.assertEqual(p.tracker.duel_healing_estimate(), 300)
        p.rpc('OnMsgIndividualPVPState', args=[2, 5000], seconds=5)
        self.assertIsNone(p.tracker.duel_healing_estimate())

    def test_late_identity_can_fill_finished_duel_without_reopening_clock(self):
        p = Pipeline(enemy=False)
        p.rpc('OnMsgIndividualPVPState', args=[3, 1_789_653_800_000], seconds=1)
        p.damage(180, seconds=2)
        p.rpc('OnMsgIndividualPVPResult', args=[1], seconds=3)
        p.feed(avatar(ENEMY, ENEMY_TOKEN, '延迟的名字', 25, seconds=4))
        view = p.view(100)
        self.assertEqual(view['damage'], 180)
        self.assertEqual(view['time'], '00:02')
        self.assertFalse(view['active'])
        self.assertEqual(view['result'], '胜利')

    def test_pending_damage_waits_for_identity_instead_of_every_event(self):
        p = Pipeline(enemy=False)
        p.tracker.pending.append((
            'damage', BASE_NS + 2_000_000_000,
            {'attacker_id': OTHER, 'target_id': SELF, 'damage': 180,
             'damage_source': 'network_exact'},
        ))
        retry = Mock(wraps=p.tracker._retry_pending)
        p.tracker._retry_pending = retry

        for index in range(100):
            p.tracker.consume({
                'record': {'method': 'NoOp', 'decoded_arguments': []},
                'context': {},
                'timestamp_ns': BASE_NS + (index + 3) * 1_000_000,
            })

        retry.assert_not_called()
        p.feed(avatar(OTHER, OTHER_TOKEN, 'Opponent', 25, seconds=4))
        self.assertGreater(retry.call_count, 0)
        self.assertFalse(p.tracker.pending)

    def test_duel_response_binds_shape_token_to_unknown_damage_actor(self):
        p = Pipeline(enemy=False)
        p.rpc(
            'RetOtherRoleShapeData',
            args=[{
                ENEMY_TOKEN: {
                    '0': '莫雪',
                    '2': 1_200_002,
                    '19': 81_227,
                }
            }, 1],
            seconds=0.5,
        )
        p.rpc(
            'OnMsgIndividualPVPResponse',
            args=[True, ENEMY_TOKEN, '莫雪'],
            seconds=1,
            npcap_method_scope='local_role',
        )
        p.rpc(
            'OnMsgIndividualPVPState',
            args=[2, 1_789_653_801_000, 0, 0, 0],
            seconds=1,
            npcap_method_scope='local_role',
        )
        p.rpc(
            'OnMsgIndividualPVPState',
            args=[3, 1_789_653_805_000, 0, 0, 0],
            seconds=5,
            npcap_method_scope='local_role',
        )
        p.rpc(
            'OnMsgAddFightRelationship',
            args=[ENEMY_TOKEN, 1],
            seconds=5,
            npcap_method_scope='local_role',
        )
        p.damage(16_931, seconds=6)
        p.rpc(
            'OnMsgIndividualPVPResult',
            args=[1],
            seconds=7,
            npcap_method_scope='local_role',
        )
        # The later fight-mode exit must not overwrite the authoritative
        # result that ended the duel.
        p.rpc('OnMsgSyncFightMode', args=[0], seconds=8)

        view = p.view(20)
        self.assertEqual(view['result'], '胜利')
        self.assertEqual(view['kills'], 1)
        self.assertEqual(view['assists'], 0)
        self.assertEqual(view['deaths'], 0)
        self.assertEqual(view['total_damage'], '16,931')
        self.assertEqual(view['module_name'], '')
        self.assertEqual(len(view['outgoing']), 1)
        self.assertEqual(view['outgoing'][0]['actor_id'], ENEMY)
        self.assertEqual(view['outgoing'][0]['name'], '莫雪')
        self.assertEqual(view['outgoing'][0]['rating'], 81_227)
        self.assertEqual(view['outgoing'][0]['kills'], 1)
        self.assertEqual(view['outgoing'][0]['assists'], 0)

    def test_exact_duel_damage_rebinds_stale_preparation_actor(self):
        p = Pipeline(enemy=False)
        p.feed(avatar(ENEMY, ENEMY_TOKEN, 'Preparation actor', 4))
        p.rpc(
            'OnMsgIndividualPVPResponse',
            args=[True, ENEMY_TOKEN, 'Opponent'],
            seconds=1,
            npcap_method_scope='local_role',
        )
        p.rpc(
            'OnMsgIndividualPVPState',
            args=[2, 1_789_653_801_000, 0, 0, 0],
            seconds=1,
            npcap_method_scope='local_role',
        )
        p.rpc(
            'OnMsgCastSkillNew',
            actor=ENEMY,
            args=[81_110_032, ENEMY, 1],
            seconds=2,
        )
        self.assertEqual(p.tracker.duel_opponent_actor, ENEMY)
        self.assertFalse(p.tracker.duel_opponent_actor_authoritative)
        p.rpc(
            'OnMsgIndividualPVPState',
            args=[3, 1_789_653_805_000, 0, 0, 0],
            seconds=3,
            npcap_method_scope='local_role',
        )
        p.damage(431, target=OTHER, seconds=4)

        view = p.view(4)
        self.assertEqual(p.tracker.duel_opponent_actor, OTHER)
        self.assertTrue(p.tracker.duel_opponent_actor_authoritative)
        self.assertEqual(view['total_damage'], '431')
        self.assertEqual(view['skills_outgoing'][0]['damage'], 431)
        self.assertEqual(view['skills_outgoing'][0]['hits'], 1)

        # Once exact damage has identified the arena peer, a different local
        # endpoint must neither steal the binding nor enter the duel totals.
        p.damage(999, target=ENEMY, seconds=5)
        self.assertEqual(p.tracker.duel_opponent_actor, OTHER)
        self.assertEqual(p.view(5)['total_damage'], '431')

    def test_duel_defeat_from_peer_owned_result_uses_fixed_loss_counter(self):
        p = Pipeline(enemy=False)
        p.rpc(
            'OnMsgIndividualPVPResponse',
            args=[True, ENEMY_TOKEN, '对手'],
            seconds=1,
            npcap_method_scope='local_role',
        )
        p.rpc(
            'OnMsgIndividualPVPState',
            args=[3, 1_789_653_801_000, 0, 0, 0],
            seconds=1,
            npcap_method_scope='local_role',
        )
        p.damage(120, attacker=ENEMY, target=SELF, seconds=2)
        p.rpc(
            'OnMsgIndividualPVPResult',
            actor=ENEMY,
            args=[2],
            seconds=3,
            npcap_method_scope='local_role',
        )

        view = p.view(10)
        self.assertEqual(view['result'], '失败')
        self.assertEqual(view['kills'], 0)
        self.assertEqual(view['assists'], 0)
        self.assertEqual(view['deaths'], 1)
        self.assertEqual(view['total_taken'], '120')
        self.assertEqual(len(view['incoming']), 1)
        self.assertEqual(view['incoming'][0]['opponent_id'], ENEMY_TOKEN)
        self.assertEqual(view['incoming'][0]['defeats'], 1)

    def test_late_start_local_pvp_callback_uses_same_pid_remembered_token(self):
        parser = NpcapParserAdapter(
            {SELF_TOKEN: {'name': '本人', 'role_number': 534_195_998_188}},
            remembered_self_token=SELF_TOKEN,
        )
        parser.self_id = SELF
        parser.native_self_id = SELF
        parser.process(packet(
            'OnMsgIndividualPVPResponse',
            args=[True, ENEMY_TOKEN, '对手'],
            sequence=77,
            npcap_method_scope='local_role',
        ))
        observation = parser.take_pvp_observations()[0]
        self.assertEqual(observation['context']['self_token'], SELF_TOKEN)

    def test_remembered_token_accepts_verified_local_control_with_empty_scope(self):
        parser = NpcapParserAdapter({SELF_TOKEN: {'name': '本人', 'role_number': 534_195_998_188}},
                                   remembered_self_token=SELF_TOKEN)
        parser.self_id = parser.native_self_id = SELF
        parser.process(packet('OnMsgIndividualPVPResponse', args=[True, ENEMY_TOKEN, '对手'], sequence=77))
        self.assertEqual(parser.take_pvp_observations()[0]['context']['self_token'], SELF_TOKEN)

    def test_defeat_without_any_damage_still_uses_fixed_loss_counter(self):
        p = Pipeline(enemy=False)
        p.rpc('OnMsgIndividualPVPResponse', args=[True, ENEMY_TOKEN, '莫雪'], seconds=1)
        p.rpc('OnMsgIndividualPVPState', args=[3, 1000], seconds=2)
        p.rpc('OnMsgIndividualPVPState', args=[4, 2000], seconds=3)
        p.rpc('OnMsgIndividualPVPState', args=[5, 2000], seconds=3)
        before = p.tracker.generation
        p.rpc('OnMsgIndividualPVPResult', args=[2], seconds=3)
        self.assertGreater(p.tracker.generation, before)
        view = p.view(100)
        self.assertEqual((view['kills'], view['deaths'], view['assists']), (0, 1, 0))
        self.assertEqual(view['incoming'][0]['defeats'], 1)
        self.assertEqual(view['total_taken'], '--')
        p.rpc('OnMsgBeatenSyncV2', args=[ENEMY, SELF, 860211000, 509, 509], seconds=4)
        self.assertEqual(p.view(100)['incoming'][0]['damage'], 509)
        self.assertEqual(p.view(100)['incoming'][0]['defeats'], 1)
        self.assertEqual(p.view(100)['total_taken'], '509')

    def test_unverified_result_code_cannot_be_guessed_as_defeat(self):
        p = Pipeline()
        p.rpc('OnMsgIndividualPVPState', args=[3, 1000], seconds=1)
        p.rpc('OnMsgIndividualPVPResult', args=[0], seconds=2)
        self.assertEqual(p.view(5)['deaths'], 0)
        self.assertEqual(p.view(5)['duel_outcome'], '')

    def test_window_cumulative_win_then_loss_and_scene_change_keep_both_sides(self):
        p = Pipeline(cumulative=True)
        p.rpc('OnMsgIndividualPVPResponse', args=[True, ENEMY_TOKEN, '对手'], seconds=1)
        p.rpc('OnMsgIndividualPVPState', args=[3, 1000], seconds=2)
        p.damage(300, seconds=3)
        p.rpc('OnMsgIndividualPVPResult', args=[1], seconds=4)
        p.rpc('OnMsgIndividualPVPResponse', args=[True, ENEMY_TOKEN, '对手'], seconds=5)
        self.assertEqual(p.view(5)['damage'], 300)
        p.rpc('OnMsgIndividualPVPState', args=[2, 2000], seconds=5)
        p.rpc('OnMsgIndividualPVPState', args=[3, 3000], seconds=6)
        p.damage(200, seconds=7)
        p.rpc('OnMsgEntityDead', args=[0, ENEMY_TOKEN], seconds=7.5)
        p.rpc('OnMsgIndividualPVPResult', args=[2], seconds=8)
        view = p.view(100)
        self.assertEqual((view['kills'], view['deaths'], view['assists']), (1, 1, 0))
        self.assertEqual(view['damage'], 500)
        self.assertEqual(len(view['outgoing']), 1)
        self.assertEqual(view['outgoing'][0]['damage'], 500)
        self.assertEqual(view['outgoing'][0]['kills'], 1)
        self.assertEqual(view['incoming'][0]['name'], '对手')
        self.assertEqual(view['incoming'][0]['defeats'], 1)
        self.assertEqual(view['skills_outgoing'][0]['hits'], 2)
        p.rpc('OnMsgBeforeEnterNewSpace', args=[123, 0], seconds=9)
        p.tracker.reset()
        view = p.view(100)
        self.assertEqual((view['damage'], view['kills'], view['deaths']), (500, 1, 1))
        self.assertEqual(view['incoming'][0]['name'], '对手')

    def test_new_peer_cannot_steal_previous_duel_counters(self):
        p = Pipeline(cumulative=True)
        p.feed(avatar(OTHER, OTHER_TOKEN, '新对手', 4))
        p.rpc('OnMsgIndividualPVPResponse', args=[True, ENEMY_TOKEN, '对手'], seconds=1)
        p.rpc('OnMsgIndividualPVPState', args=[3, 1000], seconds=2)
        p.damage(300, seconds=3)
        p.rpc('OnMsgIndividualPVPResult', args=[1], seconds=4)
        p.rpc('OnMsgIndividualPVPResponse', args=[True, OTHER_TOKEN, '新对手'], seconds=5)
        p.rpc('OnMsgIndividualPVPState', args=[3, 2000], seconds=6)
        p.damage(200, target=OTHER, seconds=7)
        p.rpc('OnMsgIndividualPVPResult', args=[2], seconds=8)
        outgoing = {row['opponent_id']: row for row in p.view(100)['outgoing']}
        self.assertEqual(outgoing[ENEMY_TOKEN]['damage'], 300)
        self.assertEqual(outgoing[ENEMY_TOKEN]['kills'], 1)
        self.assertEqual(outgoing[OTHER_TOKEN]['damage'], 200)
        self.assertEqual(outgoing[OTHER_TOKEN]['kills'], 0)

    def test_duplicate_prepare_start_and_result_never_reset_or_double_count(self):
        p = Pipeline(cumulative=True)
        p.rpc('OnMsgIndividualPVPState', args=[2, 1000], seconds=1)
        p.rpc('OnMsgIndividualPVPState', args=[3, 2000], seconds=2)
        p.damage(300, seconds=3)
        p.rpc('OnMsgIndividualPVPState', args=[2, 1000], seconds=4)
        p.rpc('OnMsgIndividualPVPResult', args=[2], seconds=5)
        p.rpc('OnMsgIndividualPVPResult', args=[2], seconds=6)
        p.rpc('OnMsgIndividualPVPState', args=[3, 2000], seconds=7)
        self.assertEqual((p.view(100)['damage'], p.view(100)['deaths']), (300, 1))
        self.assertFalse(p.view(100)['active'])

    def test_scene_change_clears_opponents_and_character_switch_clears_everything(self):
        p = Pipeline()
        p.damage()
        p.rpc('OnMsgBeforeEnterNewSpace', args=[123, 0], seconds=2)
        self.assertEqual(p.view(2)['outgoing'], [])
        self.assertNotIn(ENEMY, p.tracker.profiles)
        p.tracker.ingest_update('self_character', {'entity_id': OTHER, 'user_token': OTHER_TOKEN, 'name': '新角色'})
        self.assertEqual(p.tracker.self_id, OTHER)
        self.assertEqual(p.tracker.self_token, OTHER_TOKEN)
        self.assertEqual(p.view(2)['outgoing'], [])
        self.assertNotIn(SELF, p.tracker.profiles)

    def test_idle_clock_pauses_without_resetting_map_counters(self):
        p = Pipeline()
        p.damage(100, seconds=1)
        p.damage(100, seconds=3)
        self.assertEqual(p.view(20)['time'], '00:02')
        self.assertFalse(p.view(20)['active'])
        p.damage(50, seconds=30)
        self.assertEqual(p.view(31)['time'], '00:03')
        self.assertEqual(p.view(31)['damage'], 250)

    def test_live_peer_shape_rating_updates_without_active_query(self):
        p = Pipeline()
        p.damage()
        p.rpc('RetOtherRoleShapeData', args=[{'$map': [[ENEMY_TOKEN, {'$map': [[0, '对手'], [2, 1_200_003], [4, 80], [19, 88_893], [25, {'$map': [[0, 4_270_019], [1, 4_271_009]]}]]}]]}, 1], seconds=2)
        self.assertEqual(p.view(2)['outgoing'][0]['rating'], 88_893)
        self.assertEqual(p.tracker.profiles[ENEMY]['level'], 80)
        self.assertEqual(p.tracker.profiles[ENEMY]['avatar_id'], 4_270_019)
        self.assertEqual(p.tracker.profiles[ENEMY]['avatar_frame_id'], 4_271_009)
        p.rpc('RetOtherRoleShapeData', args=[{ENEMY_TOKEN: {'0': '对手', '2': 1_200_003, '19': 90_001}}, 2], seconds=3)
        self.assertEqual(p.view(3)['outgoing'][0]['rating'], 90_001)

    def test_shape_before_avatar_is_filled_on_creation(self):
        p = Pipeline(enemy=False)
        p.rpc('RetOtherRoleShapeData', args=[{ENEMY_TOKEN: {'0': '真实玩家', '2': 1_200_003, '19': 88_893}}, 1])
        p.damage()
        p.feed(avatar(ENEMY, ENEMY_TOKEN, '真实玩家', 25, seconds=2))
        self.assertEqual(p.view(2)['outgoing'][0]['rating'], 88_893)

    def test_natural_equipment_snapshot_binds_both_interaction_directions(self):
        for incoming in (False, True):
            with self.subTest(incoming=incoming):
                p = Pipeline()
                p.damage(attacker=ENEMY if incoming else SELF,
                         target=SELF if incoming else ENEMY)
                shape = {0: '对手', 2: 1_200_003, 19: 88_893,
                         11: {1: {2: 12345}, 2: {2: 67890}}}
                p.rpc('RetOtherRoleShapeData', args=[{ENEMY_TOKEN: shape}, 1], seconds=2)
                snapshot = p.tracker.profiles[ENEMY]['equipment_snapshot']
                self.assertEqual(snapshot['equipment'], [
                    {'slot': 1, 'item_id': 12345}, {'slot': 2, 'item_id': 67890},
                ])
                self.assertEqual(snapshot['extraordinary_rating'], 88_893)
                self.assertEqual(snapshot['captured_at_ns'], BASE_NS + 2_000_000_000)
                self.assertEqual(snapshot['source'], 'RetOtherRoleShapeData')
                self.assertTrue(snapshot['partial'])
                self.assertIsNone(snapshot['attributes'])
                shape[11][1][2] = 99999
                self.assertEqual(snapshot['equipment'][0]['item_id'], 12345)

    def test_malformed_optional_profile_never_breaks_pve_parser(self):
        p = Pipeline()
        p.rpc('RetOtherRoleShapeData', args=[None, 1])
        self.assertEqual(p.parser.pvp_observation_errors, 1)
        p.damage()
        self.assertEqual(p.view()['damage'], 100)

    def test_frozen_observation_cannot_be_changed_by_future_parser_mutation(self):
        p = Pipeline()
        p.parser.process(packet('OnMsgEntityDead', ENEMY, [0, SELF_TOKEN], sequence=35))
        observation = p.parser.take_pvp_observations()[0]
        p.parser.entity_profiles[ENEMY]['name'] = '后来修改的名字'
        profile = next(v for v in observation['context']['profiles'] if v['entity_id'] == ENEMY)
        self.assertEqual(profile['name'], '对手')

    def test_current_verified_pvp_descriptor_ids_decode(self):
        decoder = NpcapProtocolDecoder()
        expected = {848: 'OnMsgAddFightRelationship', 849: 'OnMsgDelFightRelationship',
                    850: 'OnMsgSyncBattleType', 1061: 'OnMsgIndividualPVPResponse',
                    1062: 'OnMsgIndividualPVPState', 1063: 'OnMsgIndividualPVPResult',
                    1064: 'OnMsgIndividualPVPLeave'}
        for method_id in expected:
            packed = msgpack.packb([{}, [SELF, method_id, []]], use_bin_type=True)
            decoder._append_application(struct.pack('<IH', 2 + len(packed), 22) + packed, method_id, 10.0)
        records = decoder._rpc_records()
        self.assertEqual([r['method'] for r in records], list(expected.values()))

    def test_old_and_current_team_pvp_info_ids_require_verified_shape(self):
        decoder = NpcapProtocolDecoder(capture_unknown=True)
        payloads = (
            (795, TEAM_INFO_ARGS),
            (799, TEAM_INFO_ARGS),
            (795, [2, {0: ENEMY_TOKEN}, {0: OTHER_TOKEN}]),
            (799, [1, {0: ENEMY_TOKEN, 2: 1_200_003, 3: 70, 4: 'x'}]),
        )
        for sequence, (method_id, arguments) in enumerate(payloads, 1):
            packed = msgpack.packb(
                [{}, [SELF, method_id, arguments]], use_bin_type=True
            )
            decoder._append_application(
                struct.pack('<IH', 2 + len(packed), 22) + packed,
                sequence,
                10.0 + sequence,
            )
        records = decoder._rpc_records()
        self.assertEqual(
            [record['npcap_method_id'] for record in records], [795, 799]
        )
        self.assertTrue(all(
            record['method'] == 'OnMsgSyncTeamPVPInfo'
            and record['npcap_method_scope'] == 'team_pvp_info_shape_compat'
            for record in records
        ))

    def test_method_824_requires_complete_two_side_roster_shape(self):
        decoder = NpcapProtocolDecoder(capture_unknown=True)
        payloads = (
            (FULL_ROSTER_ARGS, 1),
            ([{101: {0: FULL_ROSTER_ARGS[0][101][0]}}], 2),
        )
        for arguments, sequence in payloads:
            packed = msgpack.packb(
                [{}, [SELF, 824, arguments]], use_bin_type=True
            )
            decoder._append_application(
                struct.pack('<IH', 2 + len(packed), 22) + packed,
                sequence,
                10.0 + sequence,
            )

        records = decoder._rpc_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['method'], 'RetGetTeamArenaBattleInfo')
        self.assertEqual(
            records[0]['npcap_method_scope'],
            'team_pvp_roster_shape_compat',
        )
        self.assertEqual(
            [row['method_id'] for row in decoder.unknown_records], [824]
        )

    def test_method_798_requires_complete_team_settlement_shape(self):
        decoder = NpcapProtocolDecoder(capture_unknown=True)
        payloads = (
            (TEAM_SETTLEMENT_ARGS, 1),
            ([{101: {4: TEAM_SETTLEMENT_ARGS[0][101][4]}}], 2),
        )
        for arguments, sequence in payloads:
            packed = msgpack.packb(
                [{}, [SELF, 798, arguments]], use_bin_type=True
            )
            decoder._append_application(
                struct.pack('<IH', 2 + len(packed), 22) + packed,
                sequence,
                10.0 + sequence,
            )

        records = decoder._rpc_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['method'], 'OnMsgTeamPVPSettlement')
        self.assertEqual(
            records[0]['npcap_method_scope'],
            'team_pvp_settlement_shape_compat',
        )
        self.assertEqual(records[0]['decoded_arguments'], TEAM_SETTLEMENT_ARGS)
        self.assertEqual(
            [row['method_id'] for row in decoder.unknown_records], [798]
        )

    def test_complete_roster_uses_self_team_and_survives_partial_team_info(self):
        p = Pipeline(enemy=False)
        p.rpc('RetGetTeamArenaBattleInfo', args=FULL_ROSTER_ARGS)

        allies = p.tracker.pvp_team_members(include_self=True)
        enemies = p.tracker.pvp_team_members(side='enemy')
        self.assertEqual(
            [row['user_token'] for row in allies],
            [SELF_TOKEN, ENEMY_TOKEN, 'aq-ArenaAllyBot'],
        )
        self.assertEqual(
            [row['user_token'] for row in enemies],
            [OTHER_TOKEN, 'AQAAArenaEnemyTwo', 'aq-ArenaEnemyBot'],
        )
        self.assertEqual(p.tracker.team_size, 3)
        self.assertTrue(allies[-1]['is_ai'])
        self.assertTrue(enemies[-1]['is_ai'])

        p.rpc(
            'OnMsgSyncTeamPVPInfo',
            args=[
                2,
                {0: SELF_TOKEN, 2: 1_200_002, 3: 70, 4: 'Self',
                 6: 1_400_007},
                {0: OTHER_TOKEN, 2: 1_200_007, 3: 70, 4: 'Enemy',
                 6: 1_400_007},
            ],
            seconds=2,
        )
        self.assertEqual(
            [row['user_token'] for row in p.tracker.pvp_team_members(side='enemy')],
            [OTHER_TOKEN, 'AQAAArenaEnemyTwo', 'aq-ArenaEnemyBot'],
        )

    def test_team_settlement_replaces_all_six_aggregate_rows(self):
        p = Pipeline(enemy=False)
        p.tracker.pvp_enemies['AQAAArenaStale'] = {
            'user_token': 'AQAAArenaStale',
            'name': '旧交互对象',
            'side': 'enemy',
            'entity_type': 'Player',
        }
        p.rpc(
            'OnMsgTeamPVPSettlement',
            args=TEAM_SETTLEMENT_ARGS,
            seconds=161,
        )

        rows = p.tracker.team_battle_rows()

        self.assertEqual(p.tracker.result, '胜利')
        self.assertEqual(len(rows['allies']), 3)
        self.assertEqual(len(rows['enemies']), 3)
        self_row = next(row for row in rows['allies'] if row['is_self'])
        self.assertEqual(
            (self_row['damage'], self_row['healing'], self_row['taken']),
            (0, 18_580, 7_594),
        )
        self.assertEqual((self_row['kills'], self_row['deaths']), (0, 0))
        top_ally = next(
            row for row in rows['allies']
            if row['character_id'] == ENEMY_TOKEN
        )
        self.assertEqual(
            (top_ally['damage'], top_ally['kills']), (41_608, 3)
        )
        enemy = next(
            row for row in rows['enemies']
            if row['character_id'] == 'AQAAArenaEnemyTwo'
        )
        self.assertEqual(
            (enemy['damage'], enemy['healing'], enemy['taken'],
             enemy['kills'], enemy['deaths']),
            (29_954, 5_400, 18_285, 1, 1),
        )
        self.assertTrue(enemy['current_dead'])
        defeated_without_kill = next(
            row for row in rows['enemies']
            if row['character_id'] == OTHER_TOKEN
        )
        self.assertEqual(defeated_without_kill['kills'], 0)
        self.assertTrue(defeated_without_kill['current_dead'])
        self.assertNotIn(
            'AQAAArenaStale',
            {row['character_id'] for row in rows['enemies']},
        )

    def test_pre_roster_team_damage_replays_as_partial_skills_without_duplicating_totals(self):
        p = Pipeline(enemy=False, map_scoped=True)
        p.feed(avatar(ENEMY, ENEMY_TOKEN, 'Ally', sequence=71))
        p.feed(avatar(OTHER, OTHER_TOKEN, 'Enemy', sequence=72))
        p.damage(321, attacker=ENEMY, target=OTHER, seconds=2)

        self.assertEqual(len(p.tracker.pending_team_events), 1)
        p.rpc(
            'OnMsgTeamPVPSettlement',
            args=deepcopy(TEAM_SETTLEMENT_ARGS),
            seconds=161,
        )

        rows = p.tracker.team_battle_rows()
        ally = next(
            row for row in rows['allies']
            if row['character_id'] == ENEMY_TOKEN
        )
        enemy = next(
            row for row in rows['enemies']
            if row['character_id'] == OTHER_TOKEN
        )
        self.assertEqual(ally['damage'], 41_608)
        self.assertEqual(enemy['taken'], 19_341)
        self.assertEqual(sum(skill['damage'] for skill in ally['skills']), 321)
        self.assertEqual(ally['skills_scope'], 'observed_partial')
        self.assertEqual(len(p.tracker.pending_team_events), 0)

    def test_active_arena_roster_survives_normal_party_leave_for_equipment(self):
        from test_combat_model import DpsWindow

        p = Pipeline(enemy=False, map_scoped=True)
        p.tracker.pvp_allies.clear()
        p.tracker.pvp_enemies.clear()
        p.rpc(
            'OnMsgTeamPVPSettlement',
            args=deepcopy(TEAM_SETTLEMENT_ARGS),
            seconds=161,
        )
        controller = SimpleNamespace(
            active=True,
            recording={'match_id': 'pvp-current'},
            duel_recording=None,
            display_tracker=p.tracker,
            party_seen=True,
            party_active=False,
            map_id=5_208_003,
            presented_match_id='pvp-current',
            equipment_query_active=lambda: True,
        )
        window = object.__new__(DpsWindow)
        window.pvp_recording = controller
        window.pvp_tracker = p.tracker
        window.model = SimpleNamespace(
            party_user_tokens={'AQAAAStalePveMember'},
            self_character_id=SELF_TOKEN,
            party_active=True,
            party_session_id=333,
        )

        members = window._pvp_equipment_members()
        tokens = window._current_team_equipment_tokens()

        self.assertEqual(len(members), 6)
        self.assertEqual(
            tokens,
            {
                row['user_token']
                for row in p.tracker.pvp_team_members(include_self=True)
                + p.tracker.pvp_team_members(side='enemy')
            },
        )
        self.assertNotIn('AQAAAStalePveMember', tokens)
        self.assertNotEqual(window._team_equipment_party_session_id(), 333)

    def test_team_settlement_infers_omitted_loser_from_single_winner(self):
        """The wire omits field 0 on the losing side in real settlements."""
        p = Pipeline(enemy=False)
        arguments = deepcopy(TEAM_SETTLEMENT_ARGS)
        own_team = arguments[0][101]
        enemy_team = arguments[0][102]
        own_team.pop(0, None)
        enemy_team[0] = 1

        p.rpc('OnMsgTeamPVPSettlement', args=arguments, seconds=161)

        self.assertEqual(p.tracker.result, '失败')
        rows = p.tracker.team_battle_rows()
        self.assertTrue(all(row['side'] == 'ally' for row in rows['allies']))
        self.assertTrue(all(row['side'] == 'enemy' for row in rows['enemies']))

    def test_team_settlement_keeps_profile_name_for_same_uid(self):
        p = Pipeline(enemy=False)
        p.tracker.ingest_update('equipment_profile', {
            'user_token': OTHER_TOKEN,
            'name': '莞莞类卿',
            'profession_id': 1_200_002,
            'equipment_snapshot': {'equipment': [{'slot': 1}]},
        })
        arguments = deepcopy(TEAM_SETTLEMENT_ARGS)
        arguments[0][102][4][0][4] = '���'

        p.rpc('OnMsgTeamPVPSettlement', args=arguments, seconds=161)

        enemy = next(
            row for row in p.tracker.team_battle_rows()['enemies']
            if row['character_id'] == OTHER_TOKEN
        )
        self.assertEqual(enemy['name'], '莞莞类卿')

    def test_verified_arena_ai_is_counted_but_unrelated_npc_is_not(self):
        p = Pipeline(enemy=False)
        ai_actor = OTHER + 100
        p.rpc('RetGetTeamArenaBattleInfo', args=FULL_ROSTER_ARGS)
        p.feed(avatar(ai_actor, 'aq-ArenaEnemyBot', 'EnemyBot', sequence=82))
        p.damage(222, target=ai_actor, seconds=2)
        p.feed(packet(
            'NpcapEntityCreated',
            OTHER + 200,
            [{
                'entity_id': OTHER + 200,
                'entity_token': 'npc-unrelated',
                'entity_class': 'NpcActor',
                'properties': {'TemplateID': 3_000_999},
            }],
            sequence=83,
            seconds=3,
        ))
        p.damage(999, target=OTHER + 200, seconds=4)

        view = p.view(4)
        self.assertEqual(view['damage'], 222)
        self.assertEqual(view['outgoing'][0]['opponent_id'], 'aq-ArenaEnemyBot')

    def test_team_pvp_info_binds_profiles_when_avatars_arrive_later(self):
        p = Pipeline(enemy=False)
        p.rpc('OnMsgSyncTeamPVPInfo', args=TEAM_INFO_ARGS)
        rows = p.tracker.pvp_team_members()
        self.assertEqual([row['name'] for row in rows], ['队友甲', '队友乙'])
        self.assertEqual([row['actor_id'] for row in rows], [0, 0])
        self.assertEqual(
            [row['profession_id'] for row in rows], [1_200_003, 1_200_007]
        )

        p.feed(avatar(ENEMY, ENEMY_TOKEN, '队友甲', sequence=80))
        p.feed(avatar(OTHER, OTHER_TOKEN, '队友乙', sequence=81))
        rows = p.tracker.pvp_team_members()
        self.assertEqual([row['actor_id'] for row in rows], [ENEMY, OTHER])

    def test_team_pvp_info_uses_local_avatar_template_not_status_bit(self):
        p = Pipeline(enemy=False)
        ally_token = 'AQAAATemplateAlly'
        enemy_token = 'AQAAATemplateEnemy'
        p.rpc(
            'OnMsgSyncTeamPVPInfo',
            args=[
                2,
                {0: SELF_TOKEN, 2: 1_200_001, 3: 70, 4: '本人',
                 5: 1, 6: 1_400_007},
                {0: enemy_token, 2: 1_200_003, 3: 70, 4: '敌方',
                 6: 1_400_008},
            ],
        )
        p.rpc(
            'OnMsgSyncTeamPVPInfo',
            args=[
                2,
                {0: ally_token, 2: 1_200_005, 3: 70, 4: '队友',
                 6: 1_400_007},
                {0: enemy_token, 2: 1_200_003, 3: 70, 4: '敌方',
                 5: 1, 6: 1_400_008},
            ],
            seconds=2,
        )

        self.assertEqual(
            [row['user_token'] for row in p.tracker.pvp_team_members()],
            [ally_token],
        )
        self.assertEqual(
            [row['user_token'] for row in p.tracker.pvp_team_members(side='enemy')],
            [enemy_token],
        )

    def test_invalid_profession_update_does_not_replace_verified_profession(self):
        p = Pipeline()
        p.tracker.ingest_update(
            'profile',
            {
                'entity_id': ENEMY,
                'user_token': ENEMY_TOKEN,
                'profession_id': 1_200_001,
            },
        )
        p.tracker.ingest_update(
            'profile',
            {
                'entity_id': ENEMY,
                'user_token': ENEMY_TOKEN,
                'profession_id': 70,
            },
        )

        self.assertEqual(
            p.tracker.profiles[ENEMY]['profession_id'],
            1_200_001,
        )
        self.assertEqual(
            p.tracker.token_profiles[ENEMY_TOKEN]['profession_id'],
            1_200_001,
        )

    def test_new_team_pvp_info_replaces_old_roster_without_touching_pve_party(self):
        p = Pipeline(enemy=False)
        p.parser.party_ids = {999}
        p.rpc('OnMsgSyncTeamPVPInfo', args=TEAM_INFO_ARGS)
        replacement = [
            1,
            {0: 'AQAAANewTeamMember', 2: 1_200_005, 3: 70,
             4: '新队友', 6: 1_400_005},
        ]
        p.rpc('OnMsgSyncTeamPVPInfo', args=replacement, seconds=2)
        self.assertEqual(
            [row['user_token'] for row in p.tracker.pvp_team_members()],
            ['AQAAANewTeamMember'],
        )
        self.assertEqual(p.parser.party_ids, {999})

    def test_invalid_named_team_pvp_info_never_enters_roster(self):
        p = Pipeline(enemy=False)
        p.rpc(
            'OnMsgSyncTeamPVPInfo',
            args=[2, {0: ENEMY_TOKEN}, {0: OTHER_TOKEN}],
        )
        self.assertEqual(p.tracker.pvp_team_members(), [])

    def test_pvp_hud_prefers_verified_roster_over_inactive_pve_party(self):
        from test_combat_model import DpsWindow

        p = Pipeline(enemy=False)
        p.rpc('OnMsgSyncTeamPVPInfo', args=TEAM_INFO_ARGS)
        window = object.__new__(DpsWindow)
        window.pvp_recording = None
        window.pvp_tracker = p.tracker
        window.hide_names = False
        window.model = SimpleNamespace(
            party_active=False,
            self_id=SELF,
            actor_character_ids={},
            actor_profession_id=lambda _actor: 0,
        )
        window._main_extraordinary_rating = lambda _actor: None
        window._equipment_summary_for_actor = lambda _actor, _token='': {}

        teammates = window._pvp_current_teammates()
        self.assertEqual([row['name'] for row in teammates], ['队友甲', '队友乙'])
        composition = window._pvp_team_composition_rows()
        self.assertEqual(
            {row['user_token'] for row in composition},
            {SELF_TOKEN, ENEMY_TOKEN, OTHER_TOKEN},
        )

    def test_formal_ui_dispatch_uses_independent_tracker_and_switch_does_not_reset(self):
        from test_combat_model import DpsWindow
        p = Pipeline()
        window = object.__new__(DpsWindow)
        window.pvp_tracker = p.tracker
        window.pvp_recording = None
        window.main_combat_mode = 'pve'
        window.show_pvp_button = True
        window._layered_main_active = lambda: True
        window._schedule_layered_main_render = Mock()
        window._hide_enrage_tooltip = Mock()
        window.model = SimpleNamespace(self_id=SELF, entity_names={SELF: '实时本人'},
                                       entity_extraordinary_ratings={SELF: 80_616}, entity_professions={SELF: 1_200_001})
        window._startup_interaction_blocked = lambda: False
        window._preferred_main_topmost = lambda: True
        p.parser.process(packet('OnMsgDamageSyncV2', args=[SELF, ENEMY, 8_601_107_100_055, 0, 1, 567, 0, 567, False], sequence=50))
        observation = p.parser.take_pvp_observations()[0]
        window._dispatch_message('pvp_observation', observation)
        self.assertEqual(window.pvp_tracker.snapshot(BASE_NS)['damage'], 567)
        self.assertFalse(window._schedule_layered_main_render.called)
        window.pvp_recording = SimpleNamespace(
            map_id=5_208_004,
            active=False,
            recording=None,
            display_tracker=p.tracker,
            snapshot=p.tracker.snapshot,
        )
        window._set_main_combat_mode('pvp')
        view = window._pvp_layered_main_snapshot()
        self.assertEqual(view['pvp_player_name'], '实时本人')
        self.assertEqual(view['pvp_rating'], '80616')
        self.assertEqual(view['pvp_total_damage'], '567')
        window._set_main_combat_mode('pve')
        self.assertEqual(window.pvp_tracker.snapshot(BASE_NS)['damage'], 567)

    def test_pvp_hud_switch_outside_formal_map_does_not_enable_recording(self):
        from test_combat_model import DpsWindow

        window = object.__new__(DpsWindow)
        window.pvp_recording = SimpleNamespace(map_id=5_100_001)
        window.main_combat_mode = 'pve'
        window.show_pvp_button = True
        window._layered_main_active = lambda: True
        window._schedule_layered_main_render = Mock()
        window._hide_enrage_tooltip = Mock()

        window._set_main_combat_mode('pvp')

        self.assertEqual(window.main_combat_mode, 'pvp')
        self.assertFalse(window._pvp_hud_map_active())
        window._schedule_layered_main_render.assert_called_once_with()

        window._set_main_combat_mode('pve')
        window._schedule_layered_main_render.reset_mock()
        window.pvp_recording.map_id = 5_208_004
        window._set_main_combat_mode('pvp')
        self.assertEqual(window.main_combat_mode, 'pvp')
        window._schedule_layered_main_render.assert_called_once_with()

    def test_official_arena_roster_bypasses_pending_slow_hud_paint(self):
        from test_combat_model import DpsWindow

        window = object.__new__(DpsWindow)
        window.main_combat_mode = 'pvp'
        window.layered_main_after_id = 'pending-paint'
        window.root = SimpleNamespace(after_cancel=Mock())
        window._schedule_layered_main_render = Mock()
        tracker = SimpleNamespace(generation=0)
        tracker.consume = lambda _payload: setattr(
            tracker, 'generation', tracker.generation + 1
        ) or True
        window.pvp_tracker = tracker
        window.pvp_recording = None

        window._handle_pvp_observation({
            'record': {'method': 'RetGetTeamArenaBattleInfo'},
        })

        window.root.after_cancel.assert_called_once_with('pending-paint')
        self.assertIsNone(window.layered_main_after_id)
        window._schedule_layered_main_render.assert_called_once_with(delay=0)

    def test_formal_capture_batch_emits_pvp_before_damage_is_lost_to_pve_gate(self):
        from test_combat_model import HookWorker, LiveHudCombatTracker, LiveHudBossTracker
        p = Pipeline()
        worker = object.__new__(HookWorker)
        worker.live_hud_combat_tracker = LiveHudCombatTracker()
        worker.live_hud_boss_tracker = LiveHudBossTracker()
        worker._sync_active_boss_cache = Mock()
        worker.emit = Mock()
        worker._add_diagnostic_counts = Mock()
        worker._update_diagnostics = Mock()
        worker._record_team_stats_response_diagnostics = Mock()
        worker._republish_live_team_ratings = Mock()
        worker._sync_parser_runtime_state = Mock(return_value=False)
        worker.settlement_experiment_enabled = False
        worker.team_stats_hybrid_enabled = False
        worker.hybrid_receive_hook_authoritative = False
        record = packet('OnMsgDamageSyncV2', args=[SELF, ENEMY, 8_601_107_100_055, 0, 1, 431, 0, 431, False], sequence=55)
        worker._process_capture_batch(p.parser, {'records': [record]}, Mock(), 1234)
        self.assertFalse(any(call.args[0] == 'event' for call in worker.emit.call_args_list))
        observations = [call.args[1] for call in worker.emit.call_args_list if call.args[0] == 'pvp_observation']
        self.assertEqual(len(observations), 1)
        p.tracker.consume(observations[0])
        self.assertEqual(p.view()['damage'], 431)

    def test_pvp_panel_mousewheel_never_uses_pve_rows(self):
        from test_combat_model import DpsWindow
        window = object.__new__(DpsWindow)
        window.main_combat_mode = 'pvp'
        window.pvp_hud_state = {
            'outgoing': [{'actor_id': i, 'kills': 1} for i in range(6)],
            'incoming': [{'actor_id': i, 'defeats': 1} for i in range(5)],
        }
        window.model = SimpleNamespace(self_id=SELF)
        window._startup_interaction_blocked = lambda: False
        window._layered_main_active = lambda: True
        window.layered_main_hit_regions = {'scroll:pvp_outgoing': (0, 10, 100, 100), 'scroll:pvp_incoming': (0, 110, 100, 200)}
        window._schedule_layered_main_render = Mock()
        window._main_display_rows = Mock(side_effect=AssertionError('PVP cannot scroll PVE rows'))
        event = SimpleNamespace(delta=-120, num='??', x=50, y=50)
        window._scroll_main(event)
        self.assertEqual(window.pvp_outgoing_offset, 1)
        event.y = 150
        window._scroll_main(event)
        self.assertEqual(window.pvp_incoming_offset, 1)
        self.assertEqual(window.pvp_outgoing_offset, 1)

    def test_average_kill_rating_unknown_if_any_killed_opponent_is_unrated(self):
        from test_main_hud_behavior import make_window
        window, _ = make_window()
        window.pvp_hud_state = {'outgoing': [{'kills': 1, 'rating': 88_893}, {'kills': 1, 'rating': None}]}
        window._startup_interaction_blocked = lambda: False
        self.assertEqual(window._pvp_layered_main_snapshot()['pvp_average_kill_rating'], '')

    def test_recorded_battleground_damage_and_death_attribution_match_native_evidence(self):
        path = Path('logs/network_20260917_215040.jsonl')
        native_path = Path('.codex-tmp/pvp-battleground-native-20260917-2203.jsonl')
        if not path.is_file() or not native_path.is_file():
            self.skipTest('local PVP capture evidence is unavailable')
        # This startup token is proven by the log's live local profile and the
        # native capture's local_player_id; it is not a guessed first attacker.
        parser = NpcapParserAdapter(remembered_self_token=SELF_TOKEN)
        tracker = PvpTracker()
        view = None
        with path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                record = json.loads(line)
                if record.get('diagnostic_type'):
                    continue
                updates = (parser.apply_read_only_team_profile(record)
                           if record.get('method') == 'NpcapLiveTeamProfile' else parser.process(record))
                for kind, value in updates:
                    tracker.ingest_update(kind, value)
                for observation in parser.take_pvp_observations():
                    tracker.consume(observation)
                if (record.get('method') == 'OnMsgEntityDead' and record.get('network_entity_id') == SELF
                        and 'T22:04:' in record.get('event_time', '')):
                    view = tracker.snapshot(record['capture_timestamp_ns'])
                    break
        self.assertIsNotNone(view)
        with native_path.open(encoding='utf-8-sig') as stream:
            outgoing = [r for line in stream if (r := json.loads(line)).get('function') == 'KAPI_HandleDamageSyncV2'
                        and r.get('attacker_id') == SELF
                        and '2026-09-17T22:03:00' <= r.get('event_time', '') <= '2026-09-17T22:04:42.333860']
        self.assertEqual(view['damage'], sum(r['arg9_i32'] for r in outgoing))
        self.assertEqual(view['damage'], 82_570)
        self.assertEqual(sum(s['hits'] for s in view['skills_outgoing']), len(outgoing))
        self.assertEqual(view['kills'], 2)
        self.assertEqual(view['deaths'], 1)
        self.assertEqual({r['name'] for r in view['outgoing']}, {'奶酒', '蛋挞挞', '尛馋猫', '凯特琳·杰', '爱淡期'})
        self.assertEqual(view['incoming'][0]['name'], '蛋挞挞')
        self.assertEqual(view['incoming'][0]['defeats'], 1)
        self.assertEqual(view['total_taken'], '--')
        self.assertEqual(parser.pvp_observation_errors, 0)


class EquipmentQuerySchedulingTests(unittest.TestCase):
    @staticmethod
    def window(tokens, ratings=None, pvp_members=None):
        from test_combat_model import DpsWindow

        ratings = dict(ratings or {})
        actors = {index + 1: token for index, token in enumerate(tokens)}
        host = object.__new__(DpsWindow)
        submit = Mock(return_value=True)
        host.worker = SimpleNamespace(
            equipment_session=lambda: (111, 222),
            schedule_equipment_profiles=submit,
        )
        host.model = SimpleNamespace(
            party_user_tokens=set(tokens),
            self_character_id=tokens[0],
            party_active=True,
            party_session_id=333,
            party_member_count=len(tokens),
            actor_character_ids=actors,
            display_name=lambda actor: f"Player {actor}",
            actor_is_ai=lambda _actor: False,
        )
        host._pvp_equipment_members = lambda: list(pvp_members or [])
        host._team_equipment_local_token = lambda: tokens[0]
        host._team_equipment_party_session_id = lambda: 333
        host._equipment_test_ratings = ratings
        host._main_extraordinary_rating = lambda actor: (
            host._equipment_test_ratings.get(actor, 80_000)
        )
        host._invalidate_team_rating_preview_rows = Mock()
        host.team_equipment_profiles = {}
        host.team_equipment_requested_tokens = set()
        host.team_equipment_profile_ratings = {}
        host.team_equipment_requested_ratings = {}
        host.team_equipment_requested_at = {}
        host.team_equipment_context = None
        host.team_equipment_next_retry_check_at = 0.0
        return host, submit, actors

    def test_fifty_people_are_submitted_individually(self):
        tokens = [f"PlayerToken-{index:02d}" for index in range(50)]
        host, submit, _actors = self.window(tokens)
        self.assertTrue(host._schedule_team_equipment_profiles())
        batches = [call.args[0]["members"] for call in submit.call_args_list]
        self.assertEqual([len(batch) for batch in batches], [1] * 50)
        self.assertEqual(
            {row["user_token"] for batch in batches for row in batch},
            set(tokens),
        )

    def test_enemy_is_queried_but_ai_member_is_not(self):
        tokens = [SELF_TOKEN]
        members = [
            {
                "user_token": ENEMY_TOKEN,
                "actor_id": ENEMY,
                "name": "Enemy",
                "side": "enemy",
                "is_ai": False,
                "extraordinary_rating": 90_001,
            },
            {
                "user_token": "aq-ArenaEnemyBot",
                "actor_id": OTHER,
                "name": "Bot",
                "side": "enemy",
                "is_ai": True,
                "ai_template_id": 3_000_174,
            },
        ]
        host, submit, _actors = self.window(tokens, pvp_members=members)
        self.assertTrue(host._schedule_team_equipment_profiles())
        queried = {
            row["user_token"]
            for call in submit.call_args_list
            for row in call.args[0]["members"]
        }
        self.assertIn(ENEMY_TOKEN, queried)
        self.assertNotIn("aq-ArenaEnemyBot", queried)

    def test_human_named_projection_is_still_queried(self):
        host, submit, _actors = self.window(
            [SELF_TOKEN],
            pvp_members=[{
                "user_token": ENEMY_TOKEN,
                "actor_id": ENEMY,
                "name": "Player·投影",
                "side": "enemy",
                "is_ai": False,
            }],
        )
        host._pvp_equipment_context_active = lambda: True

        self.assertTrue(host._schedule_team_equipment_profiles())
        queried = {
            row["user_token"]
            for call in submit.call_args_list
            for row in call.args[0]["members"]
        }
        self.assertIn(ENEMY_TOKEN, queried)

    def test_human_named_projection_equipment_response_is_accepted(self):
        host, _submit, _actors = self.window(
            [SELF_TOKEN],
            pvp_members=[{
                "user_token": ENEMY_TOKEN,
                "actor_id": ENEMY,
                "name": "Player·投影",
                "side": "enemy",
                "is_ai": False,
            }],
        )
        host._pvp_equipment_context_active = lambda: True
        host._queue_equipment_snapshot_upload = Mock()
        host.team_equipment_attempt_ratings = {}
        payload = {
            "game_pid": 111,
            "capture_session_id": 222,
            "local_user_token": SELF_TOKEN,
            "party_session_id": 333,
            "user_token": ENEMY_TOKEN,
            "name": "Player·投影",
            "equipment_count": 8,
            "pvp_equipment_count": 0,
            "active_word_count": 4,
            "total_word_count": 4,
        }

        self.assertTrue(host._ingest_team_equipment_profile(payload))
        self.assertEqual(
            host.team_equipment_profiles[ENEMY_TOKEN]["name"],
            "Player·投影",
        )

    def test_hunter_direct_opponent_queries_before_large_team(self):
        tokens = [SELF_TOKEN, *[f"AQAAATeamMember{i:02d}" for i in range(30)]]
        enemy_token = "AQAAAEnemyPriority"
        host, submit, _actors = self.window(
            tokens,
            pvp_members=[{
                "user_token": enemy_token, "actor_id": ENEMY,
                "name": "交手对手", "side": "enemy", "is_ai": False,
            }],
        )
        host.pvp_recording = SimpleNamespace(recording={"map_id": 5200167})

        self.assertTrue(host._schedule_team_equipment_profiles())

        first_batch = submit.call_args_list[0].args[0]["members"]
        self.assertEqual(first_batch[0]["user_token"], enemy_token)

    def test_rating_change_requeries_only_that_player(self):
        tokens = [SELF_TOKEN, ENEMY_TOKEN, OTHER_TOKEN]
        ratings = {1: 80_000, 2: 90_001, 3: 90_002}
        host, submit, actors = self.window(tokens, ratings=ratings)
        host.team_equipment_context = (111, SELF_TOKEN, 333)
        host.team_equipment_profiles = {
            token: {"equipment_snapshot": {}} for token in tokens
        }
        host.team_equipment_profile_ratings = {
            token: ratings[actor] for actor, token in actors.items()
        }
        host._equipment_test_ratings[2] = 91_001
        self.assertTrue(host._schedule_team_equipment_profiles())
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(
            [
                row["user_token"]
                for row in submit.call_args.args[0]["members"]
            ],
            [ENEMY_TOKEN],
        )
        self.assertIn(SELF_TOKEN, host.team_equipment_profiles)
        self.assertIn(OTHER_TOKEN, host.team_equipment_profiles)

    def test_each_arena_mode_queries_its_verified_humans_individually(self):
        from test_combat_model import DpsWindow

        for size in (1, 3, 6, 12):
            with self.subTest(team_size=size):
                tracker = PvpTracker(map_scoped=size > 1)
                tracker._set_identity(SELF, SELF_TOKEN)
                tracker.team_size = size
                if size == 1:
                    tracker.duel = True
                    tracker.duel_opponent_token = OTHER_TOKEN
                    tracker.duel_opponent_actor = OTHER
                else:
                    for index in range(size - 1):
                        token = f'PlayerAlly-{size:02d}-{index:02d}'
                        tracker.pvp_allies[token] = {
                            'user_token': token, 'side': 'ally',
                        }
                    for index in range(size):
                        token = f'PlayerEnemy-{size:02d}-{index:02d}'
                        tracker.pvp_enemies[token] = {
                            'user_token': token, 'side': 'enemy',
                        }
                submit = Mock(return_value=True)
                host = object.__new__(DpsWindow)
                host.worker = SimpleNamespace(
                    equipment_session=lambda: (111, 222),
                    schedule_equipment_profiles=submit,
                )
                host.model = SimpleNamespace(
                    party_user_tokens={'AQAAAStalePveMember'},
                    self_character_id=SELF_TOKEN,
                    party_active=False,
                    party_session_id=333,
                    party_member_count=2,
                    actor_character_ids={},
                    display_name=lambda _actor: '',
                    actor_is_ai=lambda _actor: False,
                )
                match_id = f'pvp-mode-{size}'
                host.pvp_recording = SimpleNamespace(
                    active=size > 1,
                    recording={'match_id': match_id} if size > 1 else None,
                    duel_recording={'match_id': match_id} if size == 1 else None,
                    display_tracker=tracker,
                    party_seen=True,
                    party_active=False,
                    equipment_query_active=lambda: True,
                )
                host._main_extraordinary_rating = lambda _actor: None
                host._invalidate_team_rating_preview_rows = Mock()
                host.team_equipment_profiles = {}
                host.team_equipment_requested_tokens = set()
                host.team_equipment_profile_ratings = {}
                host.team_equipment_requested_ratings = {}
                host.team_equipment_requested_at = {}
                host.team_equipment_context = None
                host.team_equipment_next_retry_check_at = 0.0

                self.assertTrue(host._schedule_team_equipment_profiles())
                queried = [
                    member['user_token']
                    for call in submit.call_args_list
                    for member in call.args[0]['members']
                ]
                self.assertEqual(len(queried), size * 2)
                self.assertEqual(len(set(queried)), size * 2)
                self.assertIn(SELF_TOKEN, queried)
                self.assertNotIn('AQAAAStalePveMember', queried)
                self.assertEqual(
                    [len(call.args[0]['members']) for call in submit.call_args_list],
                    [1] * (size * 2),
                )


if __name__ == '__main__':
    unittest.main()
