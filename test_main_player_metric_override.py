import copy
import unittest
from types import SimpleNamespace

from encounter_tracker import EncounterTracker
from settlement_presenter import encounter_view
from test_main_hud_behavior import make_window, completed_record
from test_combat_model import MODULE

AUDIENCE = MODULE['AUDIENCE_PROFESSION_ID']
WARRIOR = MODULE['WARRIOR_PROFESSION_ID']

class PlayerMetricOverrideTests(unittest.TestCase):
    def test_threshold_is_strict_and_unknown_does_not_override(self):
        window, _ = make_window()
        window.profession_display_metrics[str(AUDIENCE)] = 'hps'
        window.profession_display_metrics[str(WARRIOR)] = 'dt'
        for profession, normal in ((AUDIENCE, 'hps'), (WARRIOR, 'dt')):
            for rate in (None, 'unknown', float('nan'), float('inf'), 0, 4999.9, 5000):
                self.assertEqual(window._main_player_display_metric(profession, rate), normal)
        self.assertEqual(window._main_player_display_metric(AUDIENCE, 5000.01), 'dps')
        self.assertEqual(window._main_player_display_metric(WARRIOR, 5000.01), 'dt')

    def test_only_high_damage_actor_changes_and_config_is_untouched(self):
        window, _ = make_window()
        window.profession_display_metrics[str(AUDIENCE)] = 'hps'
        window.profession_display_metrics[str(WARRIOR)] = 'dt'
        config = copy.deepcopy(window.profession_display_metrics)
        actors = window.model.current_stats()
        for actor, profession, damage in ((actors[0], AUDIENCE, 150001),
                                          (actors[1], AUDIENCE, 150000),
                                          (actors[2], WARRIOR, 150001),
                                          (actors[3], WARRIOR, 149999)):
            window.model.entity_professions[actor.actor_id] = profession
            actor.damage = damage
        rows = window._main_combat_display_rows()
        by_id = {row['actor_id']:row for row in rows}
        self.assertEqual([by_id[i]['metric'] for i in (1,2,3,4)], ['dps','hps','dt','dt'])
        self.assertEqual(by_id[1]['total_value'],150001)
        self.assertEqual(window.profession_display_metrics,config)
        self.assertEqual([r['damage_sort'] for r in rows], sorted((r['damage_sort'] for r in rows),reverse=True))
        actors[0].damage = 149999
        self.assertEqual(next(r for r in window._main_combat_display_rows() if r['actor_id']==1)['metric'],'hps')

    def test_retained_result_uses_each_players_final_dps(self):
        window, _ = make_window()
        record = completed_record()
        record['participants'][0].update(profession_id=AUDIENCE,dps=5001)
        record['participants'][1].update(profession_id=AUDIENCE,dps=5000)
        window._remember_main_battle_result(record)
        rows = {row['actor_id']:row for row in window._main_retained_battle_rows()}
        self.assertEqual(rows[1]['metric'],'dps')
        self.assertEqual(rows[2]['metric'],'hps')


class CurrentMainPlayerMetricTests(unittest.TestCase):
    def make_settled_window(self):
        window, _ = make_window()
        window.profession_display_metrics[str(AUDIENCE)] = 'hps'
        window.profession_display_metrics[str(WARRIOR)] = 'dt'
        roster = [
            dict(id=f'player-{i}', iid=i, name=f'玩家{i}', profession_id=profession)
            for i, profession in ((1, AUDIENCE), (2, AUDIENCE),
                                  (3, WARRIOR), (4, AUDIENCE))
        ]
        tracker = EncounterTracker()
        record = tracker.begin(
            instance_id='instance', started_at_ns=100_000_000_000,
            participants_snapshot=roster, self_token='player-1',
        )
        tracker.end('WIPE', 130_000_000_000)
        record.settlement_status = 'SETTLED'
        record.encounter_duration_seconds = 30
        record.participants = [
            dict(identity, damage=damage, dps=damage / 30, heal=heal, bear=bear)
            for identity, damage, heal, bear in (
                (roster[0], 120_000, 600_000, 30_000),
                (roster[1], 150_000, 450_000, 60_000),
                (roster[2], 300_000, None, 120_000),
                (roster[3], 150_001, 900_000, None),
            )
        ]
        window.model.actor_character_ids = {row['iid']: row['id'] for row in roster}
        window.model.self_character_id = 'player-1'
        window.model.entity_professions.update({row['iid']: row['profession_id'] for row in roster})
        window._team_rating_preview_rows = lambda: [
            dict(actor_id=row['iid'], profession_id=row['profession_id'],
                 display_name=row['name']) for row in roster
        ]
        window.settlement_ui = SimpleNamespace(
            tracker=tracker, visible=lambda _live=None: encounter_view(record),
        )
        return window, record, roster

    def test_live_self_uses_current_healing_and_keeps_damage_separate(self):
        window, _ = make_window()
        actor = window.model.current_stats()[0]
        actor.damage = 120_000
        window.model.entity_professions[1] = AUDIENCE
        window.latest_healing_summary = {'healers': [dict(actor_id=1, effective_healing=999_999)]}
        window.model.healing_summary = lambda _duration: {
            'healers': [dict(actor_id=1, effective_healing=60_000)]
        }
        row = window._main_live_self_dps_row()
        self.assertEqual((row['metric'], row['stat_value'], row['total_value']),
                         ('hps', 2000, 60_000))
        self.assertEqual((row['dps_value'], row['damage_sort']), (4000, 120_000))
        actor.damage = 150_000
        self.assertEqual(window._main_live_self_dps_row()['metric'], 'hps')
        actor.damage = 150_001
        self.assertEqual(window._main_live_self_dps_row()['metric'], 'dps')
        window.profession_display_metrics[str(AUDIENCE)] = 'dps'
        actor.damage = 120_000
        self.assertEqual(window._main_live_self_dps_row()['stat_value'], 4000)

    def test_live_warrior_respects_dt_even_with_high_dps(self):
        window, _ = make_window()
        window.model.entity_professions[1] = WARRIOR
        window.profession_display_metrics[str(WARRIOR)] = 'dt'
        row = window._main_live_self_dps_row()
        self.assertEqual(row['metric'], 'dt')
        self.assertEqual(row['total_value'], 230)
        self.assertAlmostEqual(row['stat_value'], 230 / 30)
        self.assertGreater(row['dps_value'], 5000)
        window.profession_display_metrics[str(WARRIOR)] = 'dps'
        self.assertEqual(window._main_live_self_dps_row()['metric'], 'dps')

    def test_settlement_and_current_roster_use_same_record_metrics(self):
        window, record, _ = self.make_settled_window()
        original = copy.deepcopy(record.to_dict())
        for rows in (window._settlement_record_main_rows(record),
                     window._current_party_recent_rows(record)):
            with self.subTest(projection=rows):
                by_id = {row['actor_id']: row for row in rows}
                self.assertEqual([by_id[i]['metric'] for i in (1, 2, 3, 4)],
                                 ['hps', 'hps', 'dt', 'dps'])
                self.assertEqual([by_id[i]['total_value'] for i in (1, 2, 3, 4)],
                                 [600_000, 450_000, 120_000, 150_001])
                self.assertEqual([by_id[i]['stat_value'] for i in (1, 2, 3)],
                                 [20_000, 15_000, 4000])
                self.assertEqual([row['actor_id'] for row in rows], [3, 4, 2, 1])
        self.assertEqual(record.to_dict(), original)

    def test_main_rows_keep_live_and_previous_settlement_metrics_separate(self):
        window, record, roster = self.make_settled_window()
        window.model.current_stats()[0].damage = 30_000
        window.model.healing_summary = lambda _duration: {
            'healers': [dict(actor_id=1, effective_healing=60_000)]
        }
        window.settlement_ui.tracker.begin(
            instance_id='instance', started_at_ns=200_000_000_000,
            participants_snapshot=roster, self_token='player-1',
        )
        rows, rating_preview = window._main_display_rows()
        self.assertFalse(rating_preview)
        self.assertTrue(rows[0]['is_live_self'])
        self.assertEqual((rows[0]['metric'], rows[0]['stat_value']), ('hps', 2000))
        recent_self = next(row for row in rows if row.get('is_recent_battle') and row.get('is_self'))
        self.assertEqual((recent_self['metric'], recent_self['stat_value']), ('hps', 20_000))
        self.assertEqual(recent_self['dps_value'], record.participants[0]['dps'])

    def test_final_boss_uses_official_self_metric(self):
        window, record, _ = self.make_settled_window()
        record.result = 'VICTORY'
        rows, rating_preview = window._main_display_rows()
        self.assertFalse(rating_preview)
        self.assertTrue(rows[0]['is_official_self'])
        self.assertEqual((rows[0]['metric'], rows[0]['stat_value']), ('hps', 20_000))
        self.assertEqual(rows[0]['dps_value'], 4000)

    def test_unavailable_metrics_or_clock_are_not_fabricated(self):
        window, record, _ = self.make_settled_window()
        for member in record.participants:
            member.update(heal=None, bear=None)
        rows = {row['actor_id']: row for row in window._settlement_record_main_rows(record)}
        for actor_id in (1, 2, 3):
            self.assertIsNone(rows[actor_id]['total_value'])
            self.assertIsNone(rows[actor_id]['stat_value'])
        record.participants[0]['heal'] = 0
        self.assertEqual(next(row for row in window._settlement_record_main_rows(record)
                              if row['actor_id'] == 1)['stat_value'], 0)
        record.encounter_duration_seconds = None
        rows = {row['actor_id']: row for row in window._current_party_recent_rows(record)}
        self.assertEqual(rows[1]['total_value'], 0)
        self.assertIsNone(rows[1]['stat_value'])
        self.assertIsNotNone(rows[4]['stat_value'])
        record.settlement_status = 'PENDING'
        self.assertTrue(all(row['stat_value'] is None
                            for row in window._current_party_recent_rows(record)))

    def test_live_unknown_values_stay_unavailable(self):
        window, _ = make_window()
        window.model.current_stats()[0].damage = 120_000
        window.model.entity_professions[1] = AUDIENCE
        self.assertIsNone(window._main_live_self_dps_row()['stat_value'])
        window.model.entity_professions[1] = WARRIOR
        window.profession_display_metrics[str(WARRIOR)] = 'dt'
        window.model.current_taken_rows = lambda: [dict(actor_id=1, taken=None)]
        self.assertIsNone(window._main_live_self_dps_row()['stat_value'])
