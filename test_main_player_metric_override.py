import copy
import unittest
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
            self.assertEqual(window._main_player_display_metric(profession, 5000.01), 'dps')

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
        self.assertEqual([by_id[i]['metric'] for i in (1,2,3,4)], ['dps','hps','dps','dt'])
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
