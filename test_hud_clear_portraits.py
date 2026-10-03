"""Internal clear guards and exact-template portrait regressions."""
import unittest
from unittest import mock
from main_hud import MainHudRenderer
from test_main_hud import ROOT, COLORS, snapshot
from test_main_hud_behavior import make_window, completed_record


class ClearDisplayTests(unittest.TestCase):
    def test_clear_finished_result_cannot_mask_reentry_with_same_encounter(self):
        window = self.window()
        window.model.combat_end_time = 100
        window._clear_main_display()
        self.assertTrue(window._main_display_is_cleared())
        window.model.combat_end_time = 0
        self.assertFalse(window._main_display_is_cleared())
        self.assertNotIn('fight-1', window.main_cleared_encounter_ids)

    def window(self):
        window, _ = make_window()
        window.model.encounter_id = 'fight-1'
        window._render_layered_main_hud = mock.Mock()
        window._hide_enrage_tooltip = mock.Mock()
        window._team_rating_preview_rows = lambda: [dict(actor_id=1, display_name='本人', profession_id=1200001, extraordinary_rating=123456)]
        window.model.reset = mock.Mock()
        return window

    def test_clear_during_battle_is_ignored_without_resetting_collected_damage(self):
        window = self.window()
        before = [row.damage for row in window.model.current_stats()]
        window._clear_main_display()
        rows, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertGreater(rows[0]['stat_value'], 0)
        window.model.reset.assert_not_called()
        window._render_layered_main_hud.assert_not_called()
        self.assertEqual([row.damage for row in window.model.current_stats()], before)
        window.model.encounter_id = 'fight-2'
        self.assertFalse(window._main_display_rows()[1])
        self.assertGreater(window._main_display_rows()[0][0]['stat_value'], 0)

    def test_clear_without_rating_stays_blank_despite_late_refreshes(self):
        window = self.window()
        window.team_rating_preview_enabled = False
        window.model.combat_end_time = 100
        window._clear_main_display()
        rows, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertTrue(all(row['stat_value'] is None and row['total_value'] is None for row in rows))
        window.team_dps_text = '999,999'
        self.assertEqual(window._layered_main_snapshot()['team_dps'], '0')
        record = completed_record()
        record['encounter_id'] = 'fight-1'
        window._remember_main_battle_result(record)
        self.assertIsNone(window.main_last_battle_result)

    def test_clear_before_pull_does_not_hide_the_next_battle_in_same_session(self):
        window = self.window()
        window.model.started = False
        window._clear_main_display()
        self.assertTrue(window._main_display_rows()[1])
        window.model.started = True
        self.assertFalse(window._main_display_rows()[1])


class PortraitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = MainHudRenderer(ROOT / 'assets', COLORS)

    def test_portraits_follow_exact_template_and_keep_names_and_levels_visible(self):
        state = snapshot()
        state.update(boss_template_id=7102990, boss_level=72, boss_name='子爵夫人')
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as labels:
            first = self.renderer.render(state)
        texts = [call.args[0] for call in labels.call_args_list]
        self.assertIn('子爵夫人', texts)
        self.assertIn('Lv.72', texts)
        state['boss_template_id'] = 7102991
        self.assertTrue(self.renderer.render(state).image.tobytes() == first.image.tobytes())
        state['boss_template_id'] = 7100209
        self.assertFalse(self.renderer.render(state).image.tobytes() == first.image.tobytes())
        self.assertIs(self.renderer._boss_portrait(999999), self.renderer._boss)

    def test_every_manifest_portrait_is_present(self):
        for filename in set(self.renderer._boss_portrait_templates.values()):
            self.assertTrue((ROOT / 'assets/bosses/hud' / filename).is_file(), filename)

    def test_normal_jqh_bosses_have_distinct_portraits(self):
        expected = {
            7100471: 'sylvia.png',
            7100401: 'evil-water-turtle.png',
            7100441: 'dance-king-baboon.png',
        }
        for template_id, filename in expected.items():
            self.assertEqual(
                self.renderer._boss_portrait_templates.get(str(template_id)),
                filename,
            )
            self.assertTrue((ROOT / 'assets/bosses/hud' / filename).is_file())
            self.assertIsNot(
                self.renderer._boss_portrait(template_id), self.renderer._boss
            )

    def test_footer_omits_clear_and_keeps_remaining_actions_contiguous(self):
        regions = self.renderer.render(snapshot()).hit_regions
        self.assertNotIn('action:clear', regions)
        actions = ('pvp', 'settings', 'lock', 'pin')
        for left, right in zip(actions, actions[1:]):
            self.assertEqual(
                regions[f'action:{left}'][2],
                regions[f'action:{right}'][0],
            )


if __name__ == '__main__':
    unittest.main()
