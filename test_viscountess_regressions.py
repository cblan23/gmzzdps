"""Late capture, local/projection identity and ordinary -> mythic regressions."""
import unittest
from types import SimpleNamespace

from test_network_state import NetworkPacketParser, packet, SELF_TOKEN, NAMED_AI_TOKEN, PLAYER_ID
from test_combat_model import (
    CombatModel, DpsWindow, BASE_FILETIME, SELF_ID, MONSTER_ID,
    SECOND_MONSTER_ID, THIRD_MONSTER_ID, damage, team_stat,
)
from history_index import build_history_summary
from combat_history import CombatHistoryStore


class LocalIdentityRegressionTests(unittest.TestCase):
    def test_midfight_healer_with_same_class_projection_resolves_on_first_confirmation(self):
        parser = NetworkPacketParser(team_profile_cache={
            SELF_TOKEN: dict(name='本机玩家', profession_id=1200002),
            NAMED_AI_TOKEN: dict(name='队友投影', profession_id=1200002),
        })
        parser.authoritative_party_tokens = {NAMED_AI_TOKEN}
        parser.party_tokens = {NAMED_AI_TOKEN}
        parser.other_party_tokens = {NAMED_AI_TOKEN}
        parser.live_team_profile_tokens = {SELF_TOKEN, NAMED_AI_TOKEN}
        parser.actor_profession_hints[PLAYER_ID] = 1200002
        # The incoming roster matched its sole remote healer before a local
        # cast arrived. A real player must never inherit that projection token.
        parser.token_actors[NAMED_AI_TOKEN] = PLAYER_ID
        parser.actor_tokens[PLAYER_ID] = NAMED_AI_TOKEN
        parser.entity_profiles[PLAYER_ID] = dict(name='队友投影', is_ai=True)
        parser._confirm_local_actor(PLAYER_ID, packet('RetCastSkillSuccessNew', []))
        self.assertEqual(parser.self_token, SELF_TOKEN)
        self.assertEqual(parser.self_id, PLAYER_ID)
        self.assertEqual(parser.entity_profiles[PLAYER_ID]['name'], '本机玩家')
        self.assertFalse(parser.entity_profiles[PLAYER_ID]['is_ai'])
        self.assertNotEqual(parser.token_actors[NAMED_AI_TOKEN], PLAYER_ID)
        parser._confirm_local_actor(PLAYER_ID, packet('', []), native=True)
        self.assertEqual(parser.native_self_id, PLAYER_ID)
        self.assertEqual(parser.self_token, SELF_TOKEN)

    def test_projection_cannot_claim_self_through_hp_or_rating(self):
        for fields in ({6: 15071}, {11: 75000}):
            with self.subTest(fields=fields):
                parser = NetworkPacketParser()
                parser.self_id, parser.self_confirmed = PLAYER_ID, True
                parser.token_max_hp[NAMED_AI_TOKEN] = 15071
                parser.team_profile_markers[NAMED_AI_TOKEN] = 75000
                parser.process(packet('OnUpdateTeamGroupSelfProps', [{'$map': list(fields.items())}]))
                self.assertIsNone(parser.self_token)
                self.assertEqual(parser.self_id, PLAYER_ID)

    def test_projection_repair_cannot_take_confirmed_local_actor(self):
        second_ai = 'aqNEj5owFs79RsJ3'
        parser = NetworkPacketParser(team_profile_cache={
            NAMED_AI_TOKEN: dict(name='投影甲', profession_id=1200002),
            second_ai: dict(name='投影乙', profession_id=1200005),
        })
        parser.self_id, parser.self_confirmed = PLAYER_ID, True
        parser.party_tokens = {NAMED_AI_TOKEN, second_ai}
        parser.combat_source_actors = {PLAYER_ID, PLAYER_ID + 1}
        parser.actor_profession_hints = {PLAYER_ID: 1200002, PLAYER_ID + 1: 1200005}
        parser._team_hp_bindings(packet('', []))
        self.assertNotIn(PLAYER_ID, parser.actor_tokens)
        self.assertEqual(parser.self_id, PLAYER_ID)


class BossHpRegressionTests(unittest.TestCase):
    def test_current_hp_is_never_displayed_as_declared_maximum(self):
        window = object.__new__(DpsWindow)
        window.model = SimpleNamespace()
        monster = SimpleNamespace(name='子爵夫人', current_hp=66624984,
                                  max_hp=None, observed_max_hp=66624984)
        first = window._main_boss_display_values(monster)
        self.assertNotIn('/', first['boss_hp'])
        self.assertTrue(first['boss_hp'])
        self.assertEqual(first['boss_percent'], '')
        self.assertIsNone(first['boss_ratio'])
        monster.max_hp = 66735474
        exact = window._main_boss_display_values(monster)
        self.assertIn('6673.5', exact['boss_hp'])
        self.assertAlmostEqual(exact['boss_ratio'], 66624984 / 66735474)
        monster.current_hp = 0
        self.assertEqual(window._main_boss_display_values(monster)['boss_percent'], '0%')
        empty = window._main_boss_display_values(None)
        self.assertEqual(empty['boss_hp'], '')
        self.assertEqual(empty['boss_percent'], '')
        self.assertIsNone(empty['boss_ratio'])


class ViscountessPhaseRegressionTests(unittest.TestCase):
    def make_battle(self, unknown=False):
        model = CombatModel(run_id='viscountess-regression')
        model.ingest_identity({'entity_id': SELF_ID})
        for actor, template, name in (
            (MONSTER_ID, 0 if unknown else 7102990, 'Boss' if unknown else '子爵夫人'),
            (SECOND_MONSTER_ID, 7102991, '子爵夫人-神话姿态'),
            (THIRD_MONSTER_ID, 7102990, '子爵夫人'),
        ):
            model.ingest_profile(dict(entity_id=actor, template_id=template, name=name,
                                      entity_type='Boss', boss_rank=3, boss_type=3))
        model.ingest_active_boss(dict(entity_id=MONSTER_ID, filetime_100ns=BASE_FILETIME))
        baseline = team_stat(1, SELF_ID, 0, server_time=100)
        baseline.update(absolute_taken=0, absolute_effective_healing=0, full_snapshot=True)
        model.ingest_team_stat(baseline)
        model.ingest_combat_state(dict(entity_id=MONSTER_ID, in_combat=True,
                                      filetime_100ns=BASE_FILETIME + 20000))
        first = team_stat(3000, SELF_ID, 20000000, server_time=101)
        first.update(absolute_taken=1000, absolute_effective_healing=4000)
        model.ingest_team_stat(first)
        model.sample_team_damage(model.last_damage_time)
        return model

    def test_phase_keeps_identity_clock_dps_hps_dt_and_one_history_record(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                model = self.make_battle(unknown)
                encounter, started = model.encounter_id, model.first_damage_time
                model.combat_end_time = model.last_damage_time
                model.combat_end_reason = 'boss_replaced'
                model.healing_end_time = model.last_damage_time
                model.archive_current('boss_replaced')
                model.ingest_active_boss(dict(entity_id=SECOND_MONSTER_ID,
                    filetime_100ns=BASE_FILETIME + 30 * 10000000))
                self.assertEqual(model.combat_end_time, 0)
                self.assertEqual(model.healing_end_time, 0)
                reset = team_stat(31000, SELF_ID, 0, server_time=200)
                reset.update(absolute_taken=0, absolute_effective_healing=0, full_snapshot=True)
                model.ingest_team_stat(reset)
                second = team_stat(35000, SELF_ID, 3000000, server_time=201)
                second.update(absolute_taken=500, absolute_effective_healing=1000)
                model.ingest_team_stat(second)
                self.assertEqual(model.encounter_id, encounter)
                self.assertEqual(model.first_damage_time, started)
                self.assertEqual(model.stats[SELF_ID].damage, 23000000)
                self.assertEqual(model.team_taken_states[SELF_ID].accepted_taken, 1500)
                self.assertEqual(model.team_healing_states[SELF_ID].accepted_effective_healing, 5000)
                self.assertEqual(model.pop_completed_combats(), [])
                model.archive_current('party_exit')
                records = model.pop_completed_combats()
                self.assertEqual(len(records), 1)
                self.assertEqual(build_history_summary(records[0])['boss_name'], '子爵夫人')

    def test_explicit_failed_pull_does_not_resume_into_mythic(self):
        for reason in ('party_wipe', 'target_reset'):
            with self.subTest(reason=reason):
                model = self.make_battle()
                encounter = model.encounter_id
                model.combat_end_time = model.last_damage_time
                model.combat_end_reason = reason
                model.ingest_active_boss(dict(entity_id=SECOND_MONSTER_ID,
                    filetime_100ns=BASE_FILETIME + 300000000))
                self.assertTrue(model.combat_end_time)
                model.ingest(damage(31000, SELF_ID, SECOND_MONSTER_ID, 100))
                self.assertNotEqual(model.encounter_id, encounter)
                self.assertEqual(model.stats[SELF_ID].damage, 100)

    def test_mythic_to_new_ordinary_entity_is_a_new_pull(self):
        model = self.make_battle()
        encounter = model.encounter_id
        model.ingest_active_boss(dict(entity_id=SECOND_MONSTER_ID,
            filetime_100ns=BASE_FILETIME + 300000000))
        model.ingest(damage(31000, SELF_ID, SECOND_MONSTER_ID, 100))
        model.ingest_active_boss(dict(entity_id=THIRD_MONSTER_ID,
            filetime_100ns=BASE_FILETIME + 400000000))
        model.ingest(damage(41000, SELF_ID, THIRD_MONSTER_ID, 200))
        self.assertNotEqual(model.encounter_id, encounter)
        self.assertEqual(model.stats[SELF_ID].damage, 200)

    def test_one_minute_forty_four_second_settlement_cannot_shorten_both_phases(self):
        model = self.make_battle()
        model.monsters[SECOND_MONSTER_ID].current_hp = 20000000
        model.monsters[SECOND_MONSTER_ID].max_hp = 66735474
        model.ingest_active_boss(dict(entity_id=SECOND_MONSTER_ID,
            filetime_100ns=BASE_FILETIME + 300 * 10000000))
        model.ingest_combat_state(dict(entity_id=SECOND_MONSTER_ID, in_combat=True,
            filetime_100ns=BASE_FILETIME + 300 * 10000000))
        model.ingest_team_stat(team_stat(400000, SELF_ID, 23000000, server_time=401))
        total = model.stats[SELF_ID].damage
        summary = dict(summary_id='settlement|phase-clock', filetime_100ns=BASE_FILETIME + 401 * 10000000,
            authoritative=True, completion_confirmed=True, member_count=1,
            actors=[dict(actor_id=SELF_ID, damage=total, combat_seconds_total=104)])
        model.ingest_stage_summary(summary)
        self.assertEqual(model.game_server_duration_seconds, 0)
        self.assertEqual(model.game_server_duration_rejection['reason'], 'phase_clock_shorter_than_observed_encounter')
        model.combat_end_time = model.last_damage_time
        record = model.build_combat_record('target_defeated')
        self.assertGreater(record['duration_seconds'], 390)
        updated, changed = CombatHistoryStore._apply_game_server_team_clock(record, summary, summary['summary_id'])
        self.assertFalse(changed)
        self.assertEqual(updated['duration_seconds'], record['duration_seconds'])
        self.assertEqual(updated['total_damage'], total)


if __name__ == '__main__':
    unittest.main()
