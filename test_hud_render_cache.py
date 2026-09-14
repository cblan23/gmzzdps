import unittest
from unittest.mock import Mock
from hud_render_cache import HudRenderCache, hud_render_delay


class HudRenderCacheTests(unittest.TestCase):
    def test_identical_snapshot_reuses_frame(self):
        cache, renderer = HudRenderCache(), Mock()
        snapshot = {'rows': [{'damage': 100}], 'bosses': [{'hp': 50}]}
        first = cache.render(renderer, snapshot, pixel_scale=1, font_size=14)
        self.assertIs(first, cache.render(renderer, snapshot, pixel_scale=1, font_size=14))
        self.assertEqual(renderer.render.call_count, 1)
        self.assertEqual(cache.hits, 1)

    def test_mutated_nested_values_settings_and_renderer_invalidate(self):
        cache, renderer = HudRenderCache(), Mock()
        snapshot = {'rows': [{'damage': 100}], 'bosses': [{'hp': 50}]}
        cache.render(renderer, snapshot, pixel_scale=1, font_size=14)
        snapshot['rows'][0]['damage'] = 200
        cache.render(renderer, snapshot, pixel_scale=1, font_size=14)
        snapshot['bosses'][0]['hp'] = 40
        cache.render(renderer, snapshot, pixel_scale=1, font_size=14)
        cache.render(renderer, snapshot, pixel_scale=2, font_size=14)
        cache.render(renderer, snapshot, pixel_scale=2, font_size=18)
        self.assertEqual(renderer.render.call_count, 5)
        other = Mock()
        cache.render(other, snapshot, pixel_scale=2, font_size=18)
        other.render.assert_called_once()

    def test_failed_render_does_not_cache_new_snapshot(self):
        cache, renderer = HudRenderCache(), Mock()
        renderer.render.side_effect = [RuntimeError('paint'), object()]
        with self.assertRaises(RuntimeError):
            cache.render(renderer, {}, pixel_scale=1, font_size=14)
        cache.render(renderer, {}, pixel_scale=1, font_size=14)
        self.assertEqual(renderer.render.call_count, 2)

    def test_render_delay_is_bounded_and_keeps_explicit_delay(self):
        self.assertEqual(hud_render_delay(10, 0), 0)
        self.assertEqual(hud_render_delay(10, 9), 0)
        self.assertEqual(hud_render_delay(10, 10), 34)
        self.assertEqual(hud_render_delay(10.01, 10), 24)
        self.assertEqual(hud_render_delay(10, 10, 100), 100)
