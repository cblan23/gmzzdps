import unittest

from network_state import NetworkPacketParser
from test_combat_model import CombatModel, DpsWindow, BASE_FILETIME, SELF_ID, MONSTER_ID

SELF_TOKEN = 'AQAAAOwNKLYHAAAA'
PEER_TOKEN = 'AQAAAOwN0ga-AAAA'
PEER = SELF_ID + 99


class HookSelfIdentityTests(unittest.TestCase):
    def setUp(self):
        self.parser = NetworkPacketParser({
            SELF_TOKEN: {'name': '本人', 'profession_id': 1200006},
            PEER_TOKEN: {'name': '同职业队友', 'profession_id': 1200006},
        })
        self.model = CombatModel(run_id='hook-identity')
        self.model.ingest_profile({'entity_id': MONSTER_ID, 'entity_type': 'Boss', 'boss_type': 3,
                                   'boss_rank': 3, 'template_id': 7102990})
        self.apply(self.parser.process_native_damage({
            'attacker_id': SELF_ID, 'target_id': MONSTER_ID, 'local_player_id': SELF_ID,
            'arg4_u64': 8_606_101_000_396, 'damage': 100, 'raw_damage': 100,
            'filetime_100ns': BASE_FILETIME+10_000_000,
        }))
        self.apply(self.parser._confirm_self_token(SELF_TOKEN, {'filetime_100ns': BASE_FILETIME+10_000_001}))
        self.parser._bind_team_token(PEER_TOKEN, PEER)
        self.snapshot({SELF_TOKEN: self.fields('本人', 100), PEER_TOKEN: self.fields('同职业队友', 200)}, 2)

    def fields(self, name, damage):
        return {0: SELF_TOKEN if name == '本人' else PEER_TOKEN, 3: 1200006, 4: name, 5: damage}

    def apply(self, updates):
        handlers = {'identity': self.model.ingest_identity, 'event': self.model.ingest,
                    'profile': self.model.ingest_profile, 'party': self.model.ingest_party,
                    'actor_merge': self.model.merge_actor, 'team_stat': self.model.ingest_team_stat,
                    'team_actor_rebind': self.model.rebind_team_actors, 'life': self.model.ingest_life}
        for kind, value in updates:
            if kind in handlers: handlers[kind](value)

    def snapshot(self, entries, second):
        updates = self.parser.process({'method': 'RetCommonCombatStatisticsByTeam',
                                       'decoded_arguments': [entries], 'script_entity': 1,
                                       'filetime_100ns': BASE_FILETIME+second*10_000_000})
        self.apply(updates)
        return updates

    def test_omitted_self_never_replaced_or_merged_with_same_class_peer(self):
        updates = self.snapshot({PEER_TOKEN: self.fields('同职业队友', 250)}, 3)
        self.assertEqual(self.parser.self_token, SELF_TOKEN)
        self.assertEqual(self.parser.token_actors[SELF_TOKEN], SELF_ID)
        self.assertEqual(self.parser.token_actors[PEER_TOKEN], PEER)
        self.assertFalse(any(kind == 'actor_merge' and value.get('from_actor_id') == PEER for kind, value in updates))
        self.assertEqual(self.model.display_name(SELF_ID), '本人')
        self.assertEqual({row.actor_id: row.damage for row in self.model.current_stats()}, {SELF_ID: 100, PEER: 250})
        record = self.model.build_combat_record('idle')
        self.assertEqual(len(record['participants']), 2)
        self.assertEqual(next(row for row in record['participants'] if row['is_self'])['actor_id'], SELF_ID)
        window = object.__new__(DpsWindow)
        window.model = self.model
        window.latest_healing_summary = {}
        window._shown_actor_name = self.model.display_name
        window._profession_display_metric = lambda _: 'dps'
        window._main_visible_roster_members = self.model._current_member_ids
        rows = window._main_combat_display_rows(self.model.last_damage_time)
        self.assertEqual(next(row for row in rows if row['is_self'])['name'], '本人')
        self.assertEqual(len(rows), 2)

    def test_stats_omission_does_not_remove_an_existing_teammate(self):
        self.snapshot({SELF_TOKEN: self.fields('本人', 150)}, 3)
        self.assertIn(PEER, self.parser.party_ids)
        self.assertIn(PEER, self.model._current_member_ids())
        self.assertEqual(self.parser.party_member_count, 2)

    def test_scene_change_without_controlled_actor_change_cannot_replace_self(self):
        self.apply(self.parser.process({'method': 'OnMsgBeforeEnterNewSpace', 'decoded_arguments': [],
                                        'filetime_100ns': BASE_FILETIME+3*10_000_000}))
        self.snapshot({PEER_TOKEN: self.fields('同职业队友', 300)}, 4)
        self.assertEqual(self.parser.self_token, SELF_TOKEN)
        self.assertEqual(self.parser.self_id, SELF_ID)

    def test_known_peer_cannot_become_self_from_matching_cast(self):
        parser = NetworkPacketParser()
        parser._bind_team_token(PEER_TOKEN, PEER)
        parser.party_tokens.add(PEER_TOKEN)
        parser.other_party_tokens.add(PEER_TOKEN)
        parser.party_ids.add(PEER)
        parser.recent_skill_sources.append((86061010, PEER, BASE_FILETIME))
        updates = parser.process({'method': 'RetCastSkillSuccessNew', 'decoded_arguments': [86061010, 10002184, 1],
                                  'filetime_100ns': BASE_FILETIME+10000, 'sequence': 1})
        self.assertIsNone(parser.self_id)
        self.assertFalse(any(kind == 'identity' for kind, _ in updates))
        self.assertEqual(next(value for kind, value in updates if kind == 'skill_cast')['actor_id'], 0)

    def test_known_peer_only_snapshot_cannot_bootstrap_local_token(self):
        parser = NetworkPacketParser()
        parser._bind_team_token(PEER_TOKEN, PEER)
        parser.other_party_tokens.add(PEER_TOKEN)
        parser.process_native_damage({'attacker_id': SELF_ID, 'target_id': MONSTER_ID,
                                      'local_player_id': SELF_ID, 'arg4_u64': 8_606_101_000_396,
                                      'damage': 100, 'raw_damage': 100, 'filetime_100ns': BASE_FILETIME})
        parser.process({'method': 'RetCommonCombatStatisticsByTeam',
                        'decoded_arguments': [{PEER_TOKEN: self.fields('同职业队友', 200)}],
                        'filetime_100ns': BASE_FILETIME+10000})
        self.assertEqual(parser.self_id, SELF_ID)
        self.assertIsNone(parser.self_token)
        self.assertEqual(parser.token_actors[PEER_TOKEN], PEER)


if __name__ == '__main__':
    unittest.main()
