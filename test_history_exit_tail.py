"""Regressions from the 2026-09-11 17:55 Hook capture."""
import unittest

from history_index import build_history_summary, DungeonCatalog
from test_combat_model import CombatModel, SELF_ID, MONSTER_ID, SECOND_MONSTER_ID, damage, BASE_FILETIME


class HistoryExitTailTests(unittest.TestCase):
    def test_viscountess_phases_override_previous_stage_label(self):
        for targets in (
            [dict(name='子爵夫人', template_id=7102990, entity_id=100),
             dict(name='子爵夫人-神话姿态', template_id=7102991, entity_id=101)],
            [dict(name='子爵夫人-神话姿态', entity_id=101)],
        ):
            with self.subTest(targets=targets):
                record = dict(monster=targets[-1], targets=targets, target_filter='boss',
                              dungeon_id=5100055, dungeon_stage_id=5150062)
                summary = build_history_summary(record)
                self.assertEqual(summary['stage_name'], '子爵夫人')
                self.assertEqual(DungeonCatalog().boss_icon(0, stage_id=5150062,
                                 name='子爵夫人'), 'viscountess.png')

    def test_quit_tail_cannot_restart_encounter_but_new_scene_can(self):
        model = CombatModel(run_id='exit-tail')
        model.ingest_identity(dict(entity_id=SELF_ID))
        model.ingest_profile(dict(entity_id=MONSTER_ID, entity_type='Boss', boss_type=3,
                                  boss_rank=3, template_id=7102991, name='子爵夫人-神话姿态'))
        model.ingest(damage(1000, SELF_ID, MONSTER_ID, 200))
        model.ingest_party(dict(left_team=True, entity_ids=[]))
        model.ingest(dict(damage(2000, SELF_ID, MONSTER_ID, 50),
                          active_boss=True, name='子爵夫人-神话姿态'))
        model.ingest_team_stat(dict(actor_id=SELF_ID, absolute_damage=250, full_snapshot=True,
                                    filetime_100ns=BASE_FILETIME + 25_000_000))
        self.assertFalse(model._encounter_started())
        model.ingest_scene(dict(transition=True))
        self.assertEqual(len(model.completed_combats), 1)
        model.ingest_scene(dict(scene_id=123, filetime_100ns=BASE_FILETIME + 30_000_000))
        model.ingest_profile(dict(entity_id=SECOND_MONSTER_ID, entity_type='Boss', boss_type=3,
                                  boss_rank=3, template_id=7100208, name='安西娅'))
        model.ingest(damage(4000, SELF_ID, SECOND_MONSTER_ID, 300))
        self.assertTrue(model._encounter_started())
        self.assertEqual(model.build_combat_record('test')['total_damage'], 300)


if __name__ == '__main__':
    unittest.main()
