from pathlib import Path
import copy
import colorsys
import unittest
from unittest import mock
from types import SimpleNamespace

from PIL import Image, ImageChops

from main_hud import (
    HUD_LOGICAL_WIDTH,
    HUD_LOGICAL_WIDTH_WITHOUT_DEATHS,
    HUD_MAX_VISIBLE_ROWS,
    MainHudRenderer,
    WindowsLayeredPresenter,
)
from main_hud_artwork import ACTION_BOXES, REFERENCE_BOUNDS, REFERENCE_SCALE, _dilate


ROOT = Path(__file__).resolve().parent
COLORS = {
    1_200_001: "#f2cd32",
    1_200_002: "#7ecfa5",
    1_200_003: "#5869c4",
    1_200_004: "#6687c5",
    1_200_005: "#68b6e5",
    1_200_006: "#ee8c2f",
    1_200_007: "#a255c7",
}


def snapshot(*, deaths=True, row_count=1):
    return {
        "time": "00:19",
        "boss_name": "伤害木桩",
        "boss_hp": "5.2亿 / 5.2亿",
        "boss_percent": "100%",
        "boss_ratio": 1.0,
        "rows": [
            {
                "actor_id": index + 1,
                "profession_id": 1_200_001 + index % 7,
                "name": f"玩家{index + 1}",
                "metric": "dps",
                "stat_text": "1,048,208/s",
                "total_text": "(1782万)",
                "deaths": index,
                "is_self": index == 0,
            }
            for index in range(row_count)
        ],
        "show_time": True,
        "show_boss": True,
        "show_totals": True,
        "show_deaths": deaths,
        "show_team_dps": True,
        "show_pvp": True,
        "highlight_self": True,
        "team_dps": "8,970,096",
    }


class MainHudRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = MainHudRenderer(ROOT / "assets", COLORS, supersample=2)

    def test_footer_has_clear_and_the_four_existing_actions(self):
        result = self.renderer.render(snapshot(), pixel_scale=1.0)
        actions = {key for key in result.hit_regions if key.startswith("action:")}
        self.assertEqual(
            actions,
            {"action:clear", "action:pvp", "action:settings", "action:lock", "action:pin"},
        )

    def test_death_column_removal_reclaims_the_full_column_width(self):
        with_deaths = self.renderer.render(snapshot(deaths=True), pixel_scale=1.0)
        without_deaths = self.renderer.render(snapshot(deaths=False), pixel_scale=1.0)
        self.assertEqual(with_deaths.image.width, HUD_LOGICAL_WIDTH)
        self.assertEqual(
            without_deaths.image.width, HUD_LOGICAL_WIDTH_WITHOUT_DEATHS
        )
        self.assertLess(without_deaths.image.width, with_deaths.image.width)

    def test_visible_rows_are_bounded_without_losing_scrollable_actor_data(self):
        result = self.renderer.render(
            snapshot(row_count=HUD_MAX_VISIBLE_ROWS + 2), pixel_scale=1.0
        )
        self.assertEqual(result.visible_rows, HUD_MAX_VISIBLE_ROWS)
        self.assertEqual(len(result.actor_regions), HUD_MAX_VISIBLE_ROWS)

    def test_whole_hud_rectangle_remains_a_nearly_invisible_drag_surface(self):
        result = self.renderer.render(snapshot(), pixel_scale=1.0)
        self.assertGreater(result.image.getchannel("A").getpixel((0, 0)), 0)

    def test_static_actions_use_the_supplied_art_not_substitute_icons(self):
        source = Image.open(ROOT / "assets/main_hud_reference.png").convert("RGBA")
        for key, bounds in ACTION_BOXES.items():
            with self.subTest(key=key):
                expected = source.crop(bounds)
                expected.putalpha(expected.getchannel("A").point(lambda n: 0 if n < 8 else n))
                self.assertEqual(self.renderer._actions[key].tobytes(), expected.tobytes())

    def test_logo_is_loaded_from_current_project_logo(self):
        self.assertEqual(self.renderer.logo_path.resolve(), (ROOT / "assets/app_logo.png").resolve())

    def test_reference_font_contains_every_digit_and_renders_new_values(self):
        self.assertTrue(set('0123456789,/s') <= self.renderer.source_font_characters)
        first = self.renderer._ink('9,876,543/s', 29, numeric=True)
        second = self.renderer._ink('2,345,678/s', 29, numeric=True)
        self.assertNotEqual(first.tobytes(), second.tobytes())

    def test_live_fields_all_change_without_resizing_or_moving_columns(self):
        state = snapshot(row_count=3)
        baseline = self.renderer.render(state)
        changes = (
            ('time', '05:43'),
            ('boss_hp', '1.37亿 / 9.45亿'), ('boss_percent', '14.5%'),
            ('boss_ratio', 0.145), ('team_dps', '567,890'),
        )
        for key, value in changes:
            changed = copy.deepcopy(state)
            changed[key] = value
            result = self.renderer.render(changed)
            with self.subTest(key=key):
                self.assertEqual(result.image.size, baseline.image.size)
                self.assertEqual(result.hit_regions, baseline.hit_regions)
                self.assertNotEqual(result.image.tobytes(), baseline.image.tobytes())
        for key, value in (('name', '动态新角色'), ('stat_text', '72,391/s'), ('total_text', '(39.24万)'), ('deaths', 7)):
            changed = copy.deepcopy(state)
            changed['rows'][0][key] = value
            result = self.renderer.render(changed)
            with self.subTest(key=key):
                self.assertEqual(result.actor_regions, baseline.actor_regions)
                self.assertEqual(result.image.size, baseline.image.size)
                self.assertNotEqual(result.image.tobytes(), baseline.image.tobytes())

    def test_renderer_does_not_mutate_or_sort_the_existing_combat_data(self):
        state = snapshot(row_count=7)
        before = copy.deepcopy(state)
        self.renderer.render(state)
        self.assertEqual(state, before)

    def test_rating_preview_replaces_both_rate_and_total(self):
        state = snapshot()
        state['rating_preview'] = True
        state['rows'][0]['rating_text'] = '75900'
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as render:
            self.renderer.render(state)
        texts = [str(call.args[0]) for call in render.call_args_list]
        self.assertIn('非凡评分：', texts)
        self.assertIn('75900', texts)
        self.assertNotIn('1,048,208/s', texts)
        self.assertNotIn('(1782万)', texts)

    def test_missing_values_stay_unknown_and_do_not_use_sample_data(self):
        state = {'rows': [dict(actor_id=1, name='新角色', profession_id=1200002)], 'rating_preview': True}
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as render:
            self.renderer.render(state)
        texts = [str(call.args[0]) for call in render.call_args_list]
        self.assertIn('非凡评分：', texts)
        self.assertIn('--', texts)
        self.assertIn('', texts)
        self.assertNotIn('泰南拉斯', texts)
        self.assertNotIn('8,970,096', texts)
        bounds = self.renderer._ink('--', 29).getbbox()
        self.assertLess(bounds[3] - bounds[1], 10)

    def test_numeric_totals_never_get_an_ellipsis(self):
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as render:
            self.renderer._fitted_label('(9876.54亿)', 150, 32, regular=True, truncate=False)
        self.assertTrue(render.call_args_list)
        self.assertTrue(all('…' not in str(call.args[0]) for call in render.call_args_list))

    def test_all_seven_classes_keep_distinct_game_shapes_and_hues(self):
        signatures = set()
        for profession_id, color in COLORS.items():
            icon = self.renderer._profession(profession_id)
            signatures.add(icon.tobytes())
            # Symbols are coloured, without a round badge. Inspect a bright
            # opaque glyph pixel rather than the now-transparent background.
            pixels = [icon.getpixel((x, y)) for y in range(94) for x in range(94)
                      if icon.getpixel((x, y))[3] > 225 and max(icon.getpixel((x, y))[:3]) > 140]
            self.assertTrue(pixels)
            r, g, b, alpha = max(pixels, key=lambda p: max(p[:3]) - min(p[:3]))
            expected = colorsys.rgb_to_hsv(*(int(color[i:i+2],16) / 255 for i in (1, 3, 5)))
            actual = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            self.assertLess(min(abs(actual[0] - expected[0]), 1 - abs(actual[0] - expected[0])), 0.06)
            self.assertGreater(actual[1], 0.45)
            self.assertGreater(alpha, 225)
        self.assertEqual(len(signatures), 7)

    def test_profession_symbols_have_no_circular_background(self):
        for profession_id in COLORS:
            icon = self.renderer._profession(profession_id)
            alpha = icon.getchannel('A')
            for point in ((12, 12), (81, 12), (12, 81), (81, 81)):
                self.assertLess(alpha.getpixel(point), 50)
            self.assertLess(sum(alpha.histogram()[226:]), 94 * 94 * 0.45)

    def test_self_highlight_does_not_change_label_geometry(self):
        for text, numeric in (('六位角色名字', False), ('1,048,208/s', True), ('非凡评分：', False), ('123456', True)):
            ordinary = self.renderer._label(text, 29, numeric=numeric, contour=True, padding=13)
            highlighted = self.renderer._label(text, 29, numeric=numeric, contour=False, padding=13)
            self.assertEqual(ordinary.size, highlighted.size)
            # Exact white foreground positions stay unchanged; only the
            # background/outline layers may differ with self highlighting.
            def white_mask(image):
                r,g,b,a = image.split()
                return ImageChops.darker(ImageChops.darker(r,g),b).point(lambda n: 255 if n > 220 else 0)
            self.assertEqual(white_mask(ordinary).tobytes(), white_mask(highlighted).tobytes())

    def test_total_columns_do_not_create_background_pills(self):
        state = snapshot(row_count=1, deaths=False)
        state['show_time'] = False
        state['show_boss'] = False
        state['rows'][0]['is_self'] = False
        with mock.patch.object(self.renderer, '_pill', wraps=self.renderer._pill) as pills:
            self.renderer.render(state)
        pills.assert_not_called()

    def test_six_digit_rating_and_ai_use_fixed_caption_value_fields(self):
        state = snapshot(row_count=3)
        state['rating_preview'] = True
        state['rows'][0]['rating_text'] = '75900'
        state['rows'][1]['rating_text'] = '123456'
        state['rows'][2].update(is_ai=True, rating_text='888888')
        with mock.patch.object(self.renderer, '_fitted_label', wraps=self.renderer._fitted_label) as fitted:
            self.renderer.render(state)
        values = {str(call.args[0]): call for call in fitted.call_args_list}
        self.assertNotIn('888888', values)
        for value in ('75900', '123456', '人机'):
            self.assertIn(value, values)
            self.assertEqual(values[value].args[1], 169)
            self.assertEqual(values[value].kwargs['padding'], 13)

    def test_rating_values_share_a_center_and_use_a_short_caption_gap(self):
        from main_hud_artwork import rating_field_layout
        caption = self.renderer._fitted_label('非凡评分：', 214, 30, truncate=False, padding=13)
        left, center = rating_field_layout(caption.width)
        # The visible edges of the widest six-digit field and the caption
        # have only an eight-source-pixel gap, rather than a stretched column.
        self.assertAlmostEqual((center - 169 / 2 + 13) - (left + caption.width - 13), 8)
        for value in ('75900', '123456', '人机', '--'):
            tile = self.renderer._fitted_label(value, 169, 29, truncate=False, numeric=value.isdigit(), padding=13)
            actual_left = center - tile.width / 2
            self.assertAlmostEqual((actual_left + actual_left + tile.width) / 2, center)

    def test_six_participants_are_not_padded_to_the_maximum(self):
        result = self.renderer.render(snapshot(row_count=6))
        self.assertEqual(result.visible_rows, 6)
        self.assertEqual(len(result.actor_regions), 6)

    def test_boss_emblem_overlaps_the_increased_bar_and_logo_has_gold_rim(self):
        self.assertEqual(self.renderer._boss.size, (128, 128))
        r,g,b,a = self.renderer._avatar.getpixel((51, 3))
        self.assertGreater(r, 170)
        self.assertGreater(g, 90)
        self.assertGreater(r, b * 1.3)
        self.assertGreater(a, 200)

    def test_dpi_scaling_and_all_hit_regions_remain_inside_the_hud(self):
        for dpi in (1, 1.25, 1.5, 1.75, 2):
            for deaths in (False, True):
                for row_count in (1, 6, 10, 12):
                    result = self.renderer.render(snapshot(deaths=deaths, row_count=row_count), pixel_scale=dpi)
                    for rect in result.hit_regions.values():
                        self.assertGreaterEqual(rect[0], 0)
                        self.assertGreaterEqual(rect[1], 0)
                        self.assertLessEqual(rect[2], result.image.width)
                        self.assertLessEqual(rect[3], result.image.height)

    def test_prediction_label_and_tip_follow_the_full_hp_track(self):
        for dpi in (1, 1.5, 2):
            for deaths in (False, True):
                with self.subTest(dpi=dpi, deaths=deaths):
                    state = snapshot(deaths=deaths)
                    state['prediction'] = dict(state='normal', message='正常 · +0:34')
                    tips, labels = [], []
                    for ratio in (0, 0.25, 0.5, 0.75, 1):
                        state['prediction']['marker'] = ratio
                        result = self.renderer.render(state, pixel_scale=dpi)
                        tip = result.hit_regions['prediction_marker']
                        tips.append((tip[0] + tip[2]) / 2)
                        labels.append(result.hit_regions['prediction'][0])
                        for bounds in result.hit_regions.values():
                            self.assertGreaterEqual(bounds[0], 0)
                            self.assertLessEqual(bounds[2], result.image.width)
                            self.assertLessEqual(bounds[3], result.image.height)
                    self.assertEqual(tips, sorted(set(tips)))
                    self.assertGreater(tips[-1] - tips[0], result.image.width * 0.7)
                    for index in (1, 2, 3):
                        self.assertAlmostEqual(tips[index], tips[0] + (tips[-1] - tips[0]) * index / 4, delta=1)
                    self.assertLess(labels[1], labels[2])
                    self.assertLess(labels[2], labels[3])

    def test_prediction_marker_can_reach_zero_without_falling_back_to_eighty_two_percent(self):
        state = snapshot()
        state['prediction'] = dict(state='danger', message='危险 · -0:34', marker=0)
        result = self.renderer.render(state)
        tip = result.hit_regions['prediction_marker']
        self.assertLess((tip[0] + tip[2]) / 2, result.image.width * 0.15)

    def test_scrolling_exposes_remaining_participants_once(self):
        state = snapshot(row_count=14)
        first = self.renderer.render(state)
        state['start_index'] = 2
        last = self.renderer.render(state)
        self.assertEqual([actor for _, actor in first.actor_regions], list(range(1, 13)))
        self.assertEqual([actor for _, actor in last.actor_regions], list(range(3, 15)))

    def test_pvp_visibility_and_lock_state_are_independent(self):
        state = snapshot()
        state['show_pvp'] = False
        unlocked = self.renderer.render(state)
        self.assertNotIn('action:pvp', unlocked.hit_regions)
        state['locked'] = True
        locked = self.renderer.render(state)
        region = locked.hit_regions['action:lock']
        self.assertNotEqual(locked.image.crop(region).tobytes(), unlocked.image.crop(region).tobytes())

    def test_pin_replaces_close_and_displays_the_persisted_topmost_state(self):
        state = snapshot()
        state['topmost'] = False
        unpinned = self.renderer.render(state)
        state['topmost'] = True
        pinned = self.renderer.render(state)
        self.assertNotIn('action:close', pinned.hit_regions)
        self.assertEqual(len(pinned.hit_regions), 6)
        box = pinned.hit_regions['action:pin']
        self.assertNotEqual(pinned.image.crop(box).tobytes(), unpinned.image.crop(box).tobytes())

    def test_clock_is_text_only_and_boss_fonts_match_team_summary(self):
        from main_hud_artwork import SUMMARY_FONT_HEIGHT
        state = snapshot()
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as labels, mock.patch.object(self.renderer, '_pill', wraps=self.renderer._pill) as pills:
            self.renderer.render(state)
        clock = [call for call in labels.call_args_list if call.args[0] == '00:19']
        self.assertTrue(clock)
        self.assertFalse(any(len(call.args) > 1 and call.args[1] == 58 for call in pills.call_args_list))
        for text in ('5.2亿 / 5.2亿', '100%', '全队秒伤', '8,970,096'):
            expected_height = SUMMARY_FONT_HEIGHT - 4 if text in ('5.2亿 / 5.2亿', '100%') else SUMMARY_FONT_HEIGHT
            self.assertTrue(any(call.args[0] == text and call.args[1] == expected_height for call in labels.call_args_list), text)

    def test_boss_level_is_visible_without_changing_window_width_or_health(self):
        state = snapshot()
        before = self.renderer.render(state)
        state['boss_level'] = 72
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as labels:
            after = self.renderer.render(state)
        self.assertEqual(after.image.size, before.image.size)
        texts = [call.args[0] for call in labels.call_args_list]
        self.assertIn('Lv.72', texts)
        self.assertIn('5.2亿 / 5.2亿', texts)
        self.assertIn('100%', texts)

    def test_twelve_person_raid_includes_bottom_healers_without_scrolling(self):
        result = self.renderer.render(snapshot(row_count=12))
        self.assertEqual(result.visible_rows, 12)
        self.assertEqual(len(result.actor_regions), 12)
        self.assertEqual(result.actor_regions[-1][1], 12)

    def test_simultaneous_bosses_expand_height_and_keep_both_hp_values(self):
        state = snapshot(row_count=12)
        first = self.renderer.render(state)
        state['bosses'] = [dict(boss_name='首领甲', boss_hp='1亿 / 2亿', boss_ratio=0.5, boss_percent='50%'),
                           dict(boss_name='首领乙', boss_hp='3亿 / 4亿', boss_ratio=0.75, boss_percent='75%')]
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as labels:
            second = self.renderer.render(state)
        texts = [call.args[0] for call in labels.call_args_list]
        self.assertNotIn('首领甲', texts)
        self.assertNotIn('首领乙', texts)
        self.assertIn('1亿 / 2亿', texts)
        self.assertIn('3亿 / 4亿', texts)
        self.assertEqual(second.image.width, first.image.width)
        self.assertAlmostEqual(second.image.height - first.image.height, 110 * REFERENCE_SCALE, delta=1)
        self.assertEqual(len(second.actor_regions), 12)
        self.assertGreater(second.actor_regions[0][0][1], first.actor_regions[0][0][1])

    def test_numeric_cells_and_rate_font_are_consistent_for_same_digit_count(self):
        first = self.renderer._ink('23,597/s', 29, numeric=True)
        second = self.renderer._ink('23,101/s', 29, numeric=True)
        self.assertEqual(first.size, second.size)

    def test_total_is_attached_to_rate_and_text_uses_visible_vertical_center(self):
        from main_hud_artwork import attached_total_left, text_top_for_center
        self.assertEqual(attached_total_left(778) + 7 - (778 - 13), 2)
        for value in ('全队秒伤', '89,273', '暂无目标', '53.2%'):
            tile = self.renderer._label(value, 40)
            top = text_top_for_center(tile, 100)
            bounds = tile.info['hud_ink_bounds']
            self.assertAlmostEqual(top + (bounds[1] + bounds[3]) / 2, 100)

    def test_unavailable_values_are_blank_while_actual_zero_remains_visible(self):
        for value in (None, '', '--', '-- / --'):
            self.assertIsNone(self.renderer._label(value, 30).getbbox())
        self.assertIsNotNone(self.renderer._label('0', 30).getbbox())
        self.assertIsNotNone(self.renderer._label('人机', 30).getbbox())

    def test_short_enrage_message_does_not_leave_a_large_empty_pill(self):
        state = snapshot()
        state['prediction'] = dict(state='normal', message='正常 · +0:34')
        short = self.renderer.render(state).hit_regions['prediction']
        state['prediction']['message'] = '预计延后12s (剩余3.2%)'
        long = self.renderer.render(state).hit_regions['prediction']
        self.assertLess(short[2] - short[0], long[2] - long[0])

    def test_lost_boss_hp_is_much_darker_than_remaining_hp(self):
        state = snapshot()
        state.update(boss_name='', boss_hp='', boss_percent='', boss_ratio=0.5)
        image = self.renderer.render(state, pixel_scale=2).image
        scale = REFERENCE_SCALE * 2
        remaining = image.getpixel((round((500 - 146) * scale), round((253 - 126) * scale)))
        lost = image.getpixel((round((930 - 146) * scale), round((253 - 126) * scale)))
        self.assertGreater(remaining[0] - lost[0], 60)

    def test_boss_bar_is_slightly_taller_without_widening_the_window(self):
        from main_hud_artwork import BOSS_BAR_EXTRA_HEIGHT
        result = self.renderer.render(snapshot(row_count=10))
        self.assertEqual(result.image.width, HUD_LOGICAL_WIDTH)
        self.assertEqual(result.image.height, round((1004 + BOSS_BAR_EXTRA_HEIGHT) * REFERENCE_SCALE))

    def test_boss_placeholders_are_centered_after_the_name(self):
        from main_hud_artwork import boss_placeholder_anchors
        for bar_right in (944, 1082):
            left, percent_left = boss_placeholder_anchors(452, bar_right, 130, 54)
            self.assertAlmostEqual((left + percent_left + 54) / 2, (452 + bar_right) / 2)
            self.assertGreater(left, 452)
            self.assertLess(percent_left + 54, bar_right)

    def test_audience_damage_is_shown_when_existing_setting_selects_dps(self):
        from test_combat_model import DpsWindow, ActorStats, normalize_profession_display_metrics
        actor = ActorStats(actor_id=71)
        actor.damage = 312_760
        window = object.__new__(DpsWindow)
        window.model = SimpleNamespace(
            current_stats=lambda: [actor], current_taken_rows=lambda: [],
            _current_member_ids=lambda: {71}, non_player_actor_ids=set(),
            friend_order=[71], self_id=71, member_death_counts={},
            actor_profession_id=lambda _actor: 1200002, duration=lambda _now=None: 20,
        )
        window._shown_actor_name = lambda _actor: '本人'
        window.latest_healing_summary = {'healers': []}
        window.profession_display_metrics = normalize_profession_display_metrics({})
        healer = window._main_combat_display_rows()[0]
        self.assertEqual(healer['metric'], 'hps')
        self.assertIsNone(healer['stat_value'])
        window.profession_display_metrics['1200002'] = 'dps'
        damage = window._main_combat_display_rows()[0]
        self.assertTrue(damage['is_self'])
        self.assertEqual(damage['stat_value'], 15_638)
        self.assertEqual(damage['total_value'], 312_760)
        self.assertEqual(actor.damage, 312_760)

    def test_premultiplied_alpha_has_no_colorkey_contamination(self):
        source = Image.new('RGBA', (1, 1), (200, 100, 50, 128))
        self.assertEqual(WindowsLayeredPresenter._premultiplied_bgra(source), bytes((25, 50, 100, 128)))

    def test_fast_contour_dilation_matches_a_padded_rank_filter(self):
        from PIL import ImageDraw, ImageFilter
        mask = Image.new('L', (80, 80))
        ImageDraw.Draw(mask).ellipse((30, 30, 46, 49), fill=220)
        for radius in (3, 6, 9, 11):
            self.assertEqual(_dilate(mask, radius).tobytes(), mask.filter(ImageFilter.MaxFilter(radius * 2 + 1)).tobytes())


if __name__ == "__main__":
    unittest.main()
