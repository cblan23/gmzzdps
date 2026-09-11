"""Resize and scroll the player viewport without touching combat data."""
import copy
from types import SimpleNamespace
import unittest
from unittest import mock

from main_hud import MainHudRenderer, clamp_visible_rows
from test_main_hud import snapshot, ROOT, COLORS
from test_main_hud_behavior import make_window
from test_combat_model import DpsWindow, migrate_main_display_config


class HudViewportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = MainHudRenderer(ROOT / 'assets', COLORS)

    def test_height_changes_keep_width_and_boss_fixed_and_do_not_drop_players(self):
        for dpi in (1, 1.25, 1.5, 1.75, 2):
            for deaths in (False, True):
                state = snapshot(row_count=12, deaths=deaths)
                state['prediction'] = dict(state='normal', message='正常 · +0:34', marker=0.5)
                full = self.renderer.render(state, pixel_scale=dpi)
                original = copy.deepcopy(state['rows'])
                for count in (1, 3, 6):
                    state['visible_rows'] = count
                    first = self.renderer.render(state, pixel_scale=dpi)
                    self.assertEqual(first.image.width, full.image.width)
                    self.assertLess(first.image.height, full.image.height)
                    self.assertEqual(len(first.actor_regions), count)
                    self.assertEqual(first.actor_regions[0][0][1], full.actor_regions[0][0][1])
                    self.assertEqual(first.hit_regions['prediction'], full.hit_regions['prediction'])
                    self.assertEqual(first.hit_regions['prediction_marker'], full.hit_regions['prediction_marker'])
                    state['start_index'] = 12 - count
                    last = self.renderer.render(state, pixel_scale=dpi)
                    self.assertEqual(last.image.size, first.image.size)
                    self.assertEqual(last.actor_regions[-1][1], 12)
                    self.assertEqual(last.hit_regions, first.hit_regions)
                    self.assertEqual(state['rows'], original)
                    state['start_index'] = 0

    def test_height_limit_and_resize_grip_do_not_overlap_footer_actions(self):
        state = snapshot(row_count=12)
        state['visible_rows'] = 0
        result = self.renderer.render(state)
        self.assertEqual(result.visible_rows, 1)
        grip = result.hit_regions['resize:height']
        for key, bounds in result.hit_regions.items():
            if key.startswith('action:'):
                self.assertLessEqual(bounds[2], grip[0])
        state['locked'] = True
        self.assertNotIn('resize:height', self.renderer.render(state).hit_regions)
        self.assertEqual(clamp_visible_rows(999), 12)
        self.assertEqual(clamp_visible_rows('invalid'), 12)

    def test_rating_preview_can_scroll_to_last_party_member(self):
        state = snapshot(row_count=12)
        state.update(visible_rows=1, start_index=11, rating_preview=True)
        for row in state['rows']:
            row['rating_text'] = '123456'
        result = self.renderer.render(state)
        self.assertEqual(len(result.actor_regions), 1)
        self.assertEqual(result.actor_regions[0][1], 12)

    def test_height_can_expand_with_one_or_no_player_without_creating_fake_members(self):
        for players in (0, 1):
            state = snapshot(row_count=players)
            state['visible_rows'] = 1
            small = self.renderer.render(state)
            state['visible_rows'] = 8
            expanded = self.renderer.render(state)
            self.assertEqual(expanded.visible_rows, 8)
            self.assertEqual(len(expanded.actor_regions), players)
            self.assertEqual(small.image.width, expanded.image.width)
            self.assertGreater(expanded.image.height, small.image.height)

    def make_interactive_window(self):
        window, _ = make_window()
        window._layered_main_active = lambda: True
        window.window_locked = False
        window.root = SimpleNamespace(configure=lambda **values: None)
        window._schedule_layered_main_render = mock.Mock()
        window._hide_enrage_tooltip = lambda: None
        window._drag_start = mock.Mock()
        window._drag_move = mock.Mock()
        window.main_visible_rows = 6
        window.layered_main_visible_rows = 6
        window.layered_main_hit_regions = {'resize:height': (290, 280, 305, 296)}
        window.layered_main_actor_regions = (((0, 70, 305, 250), 1),)
        window._main_row_height = lambda: 24
        return window

    def test_corner_drag_changes_only_height_and_persists_on_release(self):
        window = self.make_interactive_window()
        before = {row.actor_id: row.damage for row in window.model.current_stats()}
        press = SimpleNamespace(x=298, y=288, x_root=500, y_root=700)
        window._layered_main_press(press)
        window._layered_main_drag(SimpleNamespace(x_root=800, y_root=628))
        self.assertEqual(window.main_visible_rows, 3)
        window._drag_start.assert_not_called()
        window._drag_move.assert_not_called()
        window._layered_main_drag(SimpleNamespace(x_root=800, y_root=-200))
        self.assertEqual(window.main_visible_rows, 1)
        with mock.patch.dict(DpsWindow._layered_main_release.__globals__, save_config=mock.Mock()) as values:
            window._layered_main_release(press)
            values['save_config'].assert_called_once_with(window.config)
        self.assertEqual(window.config['main_visible_rows'], 1)
        self.assertIsNone(window.layered_main_resize_origin)
        self.assertEqual(before, {row.actor_id: row.damage for row in window.model.current_stats()})
        loaded, _changed = migrate_main_display_config(window.config)
        self.assertEqual(loaded['main_visible_rows'], 1)

    def test_scroll_clamps_to_members_and_ignores_header_footer_and_locked_window(self):
        window = self.make_interactive_window()
        window.main_visible_rows = 1
        window.main_scroll_offset = 0
        wheel = SimpleNamespace(delta=-120, num='??', x=150, y=100)
        for _ in range(12):
            window._scroll_main(wheel)
        self.assertEqual(window.main_scroll_offset, 5 * 24)
        self.assertEqual(window._layered_main_snapshot()['start_index'], 5)
        for y in (10, 285):
            window._scroll_main(SimpleNamespace(delta=120, num='??', x=150, y=y))
        self.assertEqual(window.main_scroll_offset, 5 * 24)
        window.window_locked = True
        window._scroll_main(SimpleNamespace(delta=120, num='??', x=150, y=100))
        self.assertEqual(window.main_scroll_offset, 5 * 24)
        window.window_locked = False
        for _ in range(12):
            window._scroll_main(SimpleNamespace(delta=120, num='??', x=150, y=100))
        self.assertEqual(window.main_scroll_offset, 0)
        window.main_scroll_offset = 5 * 24
        window.main_visible_rows = 12
        self.assertEqual(window._layered_main_snapshot()['start_index'], 0)

    def test_migration_defaults_to_full_raid_and_preserves_unrelated_settings(self):
        old = dict(geometry='300x400+80+90', toggle_hotkey='F10')
        migrated, _changed = migrate_main_display_config(old)
        self.assertEqual(migrated['main_visible_rows'], 12)
        self.assertEqual(migrated['geometry'], old['geometry'])
        self.assertEqual(migrated['toggle_hotkey'], old['toggle_hotkey'])
        self.assertNotIn('main_visible_rows', old)


if __name__ == '__main__':
    unittest.main()
