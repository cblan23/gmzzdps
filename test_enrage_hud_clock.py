"""The HUD and enrage countdown must consume the same resolved duration."""
import unittest

from boss_enrage import BossEnrageCatalog, BossEnragePredictor, BossEnrageRule
from test_combat_model import (
    CombatModel, DpsWindow, MONSTER_ID, SELF_ID, damage, format_duration,
)


class EnrageHudClockTests(unittest.TestCase):
    def make_window(self, *, phase=False):
        window = object.__new__(DpsWindow)
        rule = BossEnrageRule(rule_id='test', enrage_seconds=300 if phase else 480,
            template_ids=frozenset({7102990}),
            countdown_start_signal='phase-two' if phase else '', first_sample_is_baseline=phase)
        window.enrage_predictor = BossEnragePredictor(BossEnrageCatalog((rule,)))
        window.boss_enrage_prediction_enabled = True
        window._set_enrage_prediction_visible = lambda value: setattr(window, 'enrage_prediction_visible', value)
        window._draw_enrage_prediction = lambda: None
        model = CombatModel(run_id='enrage-hud-clock')
        model.ingest_identity(dict(entity_id=SELF_ID))
        model.ingest_profile(dict(entity_id=MONSTER_ID, name='子爵夫人', template_id=7102990,
                                  entity_type='Boss', boss_rank=3, boss_type=3))
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.monsters[MONSTER_ID].current_hp = 600
        model.monsters[MONSTER_ID].max_hp = 1000
        window.model = model
        return window

    def test_server_epoch_offset_does_not_add_to_the_eight_minute_budget(self):
        for offset in (7.6571953, -7.6571953, 21600):
            with self.subTest(offset=offset):
                window = self.make_window()
                model = window.model
                now = model.first_damage_time + 240.375
                model.shared_clock_started_at = model.first_damage_time + offset
                model.shared_clock_duration_seconds = 240.375
                model.shared_clock_received_at = now
                model.shared_clock_state = 'active'
                for tick in (0, 1, 17):
                    duration = model.duration(now + tick)
                    window._update_enrage_prediction(now + tick)
                    prediction = window.enrage_prediction
                    self.assertAlmostEqual(prediction.elapsed_seconds, duration)
                    self.assertAlmostEqual(prediction.time_to_enrage_seconds + duration, 480)
                self.assertEqual(model.stats[SELF_ID].damage, 100)

    def test_shared_clock_correction_is_used_by_both_displays(self):
        window = self.make_window()
        model = window.model
        now = model.first_damage_time + 245
        window._update_enrage_prediction(now)
        model.shared_clock_started_at = model.first_damage_time + 7.657
        model.shared_clock_duration_seconds = 240.375
        model.shared_clock_received_at = now
        model.shared_clock_state = 'active'
        window._update_enrage_prediction(now)
        self.assertAlmostEqual(window.enrage_prediction.elapsed_seconds, model.duration(now))
        self.assertAlmostEqual(window.enrage_prediction.time_to_enrage_seconds + model.duration(now), 480)

    def test_phase_signal_still_owns_the_special_five_minute_countdown(self):
        window = self.make_window(phase=True)
        model = window.model
        now = model.first_damage_time + 240
        model.shared_clock_started_at = model.first_damage_time + 7.657
        model.shared_clock_duration_seconds = 240
        model.shared_clock_received_at = now
        model.shared_clock_state = 'active'
        window._update_enrage_prediction(now)
        self.assertIsNone(window.enrage_prediction)
        model.enrage_countdown_signal = 'phase-two'
        model.enrage_countdown_start_100ns = int((now - 60) * 10000000) + 116444736000000000
        window._update_enrage_prediction(now)
        self.assertAlmostEqual(window.enrage_prediction.elapsed_seconds, 60, places=5)
        self.assertAlmostEqual(window.enrage_prediction.time_to_enrage_seconds, 240, places=5)
        self.assertEqual(model.duration(now), 240)
        self.assertEqual(window.enrage_prediction.countdown_start_signal, 'phase-two')

    def test_tooltip_and_main_timer_show_exact_eight_minutes_after_rounding(self):
        class Canvas:
            def __init__(self):
                self.text = []
            def delete(self, *args): pass
            def create_rectangle(self, *args, **kwargs): pass
            def create_line(self, *args, **kwargs): pass
            def create_text(self, *args, **kwargs): self.text.append(kwargs['text'])
        window = self.make_window()
        window._ui_font = lambda role: None
        for elapsed in (0, 0.01, 60.01, 239.999, 479.99, 480):
            prediction = window.enrage_predictor.update(encounter_key='format-test', elapsed_seconds=elapsed,
                bosses=[window.model.monsters[MONSTER_ID]])
            canvas = Canvas()
            window._draw_enrage_tooltip_contents(canvas, prediction)
            remaining = canvas.text[canvas.text.index('狂暴剩余时间') + 1]
            minutes, seconds = map(int, remaining.split(':'))
            main_minutes, main_seconds = map(int, format_duration(elapsed).split(':'))
            self.assertEqual((minutes + main_minutes) * 60 + seconds + main_seconds, 480)

    def test_hover_region_moves_with_the_warning_pointer(self):
        window = self.make_window()
        window.layered_main_hit_regions = dict(prediction=(80, 10, 150, 35), prediction_marker=(30, 38, 42, 50))
        self.assertEqual(window._layered_main_region_at(36, 44), 'prediction')
        self.assertEqual(window._layered_main_region_at(100, 20), 'prediction')
        self.assertEqual(window._layered_main_region_at(160, 20), '')


if __name__ == '__main__':
    unittest.main()
