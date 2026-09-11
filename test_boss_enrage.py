#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from boss_enrage import (
    BossEnrageCatalog,
    BossEnragePredictor,
    BossEnrageRule,
    load_boss_enrage_catalog,
)


def boss(
    entity_id: int,
    current_hp: float,
    max_hp: float = 1_000.0,
    *,
    template_id: int = 7_100_001,
    name: str = "测试 Boss",
) -> dict:
    return {
        "entity_id": entity_id,
        "template_id": template_id,
        "name": name,
        "current_hp": current_hp,
        "max_hp": max_hp,
    }


def predictor(**overrides) -> BossEnragePredictor:
    values = {
        "rule_id": "test",
        "enrage_seconds": 120.0,
        "template_ids": frozenset({7_100_001, 7_100_002}),
        "state_hold_seconds": 3.0,
    }
    values.update(overrides)
    return BossEnragePredictor(
        BossEnrageCatalog((BossEnrageRule(**values),))
    )


class BossEnrageTests(unittest.TestCase):
    def test_catalog_skips_unverified_zero_duration_and_prefers_template(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text(
                json.dumps(
                    {
                        "defaults": {"state_hold_seconds": 2.5},
                        "encounters": [
                            {
                                "id": "unverified",
                                "boss_names": ["未知 Boss"],
                                "enrage_seconds": 0,
                            },
                            {
                                "id": "verified",
                                "boss_names": ["测试 Boss"],
                                "template_ids": [7100001],
                                "enrage_seconds": 180,
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            catalog = load_boss_enrage_catalog(path)

        self.assertEqual(len(catalog.rules), 1)
        rule = catalog.match([boss(1, 900)])
        self.assertIsNotNone(rule)
        self.assertEqual(rule.rule_id, "verified")
        self.assertEqual(rule.state_hold_seconds, 2.5)

    def test_first_frame_is_ready_without_warmup(self):
        forecast = predictor()
        result = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=0,
            bosses=[boss(1, 1_000)],
            monotonic_seconds=0,
        )
        self.assertIsNotNone(result)
        self.assertFalse(result.calculating)
        self.assertEqual(result.message, "临界 · +0:00")

    def test_half_time_at_thirty_five_percent_hp_is_ahead_by_72_seconds(self):
        forecast = predictor(enrage_seconds=480.0)
        result = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=240,
            bosses=[boss(1, 350)],
            monotonic_seconds=240,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.theoretical_remaining_hp_percent, 50.0)
        self.assertAlmostEqual(result.progress_delta_percent, 15.0)
        self.assertAlmostEqual(result.safety_margin_seconds, 72.0)
        self.assertEqual(result.message, "充裕 · +1:12")

    def test_half_time_at_fifty_two_percent_hp_is_behind_by_ten_seconds(self):
        forecast = predictor(enrage_seconds=480.0)
        result = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=240,
            bosses=[boss(1, 520)],
            monotonic_seconds=240,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.safety_margin_seconds, -9.6)
        self.assertEqual(result.message, "临界 · -0:10")

    def test_forced_invulnerability_does_not_pause_elapsed_time(self):
        forecast = predictor(enrage_seconds=100.0)
        result = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=50,
            bosses=[boss(1, 500)],
            forced_invulnerability=True,
            monotonic_seconds=50,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.time_to_enrage_seconds, 50.0)
        self.assertEqual(result.countdown_paused_seconds, 0.0)
        self.assertEqual(result.blended_hp_per_second, 0.0)

    def test_same_encounter_clock_correction_keeps_countdown_consistent(self):
        forecast = predictor(enrage_seconds=120.0)
        initial = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=40.0,
            bosses=[boss(1, 700)],
            monotonic_seconds=40.0,
        )
        corrected_backwards = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=12.0,
            bosses=[boss(1, 700)],
            monotonic_seconds=41.0,
        )
        next_tick = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=41.0,
            bosses=[boss(1, 700)],
            monotonic_seconds=42.0,
        )
        new_encounter = forecast.update(
            encounter_key="pull-2",
            elapsed_seconds=2.0,
            bosses=[boss(1, 990)],
            monotonic_seconds=43.0,
        )

        self.assertIsNotNone(initial)
        self.assertIsNotNone(corrected_backwards)
        self.assertIsNotNone(next_tick)
        self.assertIsNotNone(new_encounter)
        self.assertEqual(corrected_backwards.elapsed_seconds, 12.0)
        self.assertEqual(corrected_backwards.time_to_enrage_seconds, 108.0)
        self.assertEqual(next_tick.elapsed_seconds, 41.0)
        self.assertEqual(new_encounter.elapsed_seconds, 2.0)

    def test_two_boss_hp_is_aggregated(self):
        forecast = predictor(enrage_seconds=200.0)
        first = boss(1, 1_000, template_id=7_100_001, name="Boss A")
        second = boss(2, 1_000, template_id=7_100_002, name="Boss B")
        forecast.update(
            encounter_key="dual",
            elapsed_seconds=0,
            bosses=[first, second],
            monotonic_seconds=0,
        )
        first["current_hp"] = 800
        second["current_hp"] = 700
        result = forecast.update(
            encounter_key="dual",
            elapsed_seconds=20,
            bosses=[first, second],
            monotonic_seconds=20,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.boss_current_hp, 1_500)
        self.assertEqual(result.boss_max_hp, 2_000)
        self.assertAlmostEqual(result.boss_hp_percent, 75.0)

    def test_astrologer_waits_for_stage_two_and_uses_phase_hp_as_baseline(self):
        catalog = load_boss_enrage_catalog(
            Path(__file__).with_name("boss_enrage_config.json")
        )
        forecast = BossEnragePredictor(catalog)

        waiting = forecast.update(
            encounter_key="astrologer",
            elapsed_seconds=75,
            bosses=[
                boss(
                    1,
                    23_000_000,
                    33_270_350,
                    template_id=7_102_403,
                    name="星象仪者",
                )
            ],
            monotonic_seconds=75,
        )
        self.assertIsNone(waiting)

        started = forecast.update(
            encounter_key="astrologer",
            elapsed_seconds=90,
            countdown_elapsed_seconds=0,
            bosses=[
                boss(
                    1,
                    23_000_000,
                    33_270_350,
                    template_id=7_102_403,
                    name="星象仪者",
                )
            ],
            monotonic_seconds=90,
        )
        result = forecast.update(
            encounter_key="astrologer",
            elapsed_seconds=110,
            countdown_elapsed_seconds=20,
            bosses=[
                boss(
                    1,
                    21_000_000,
                    33_270_350,
                    template_id=7_102_403,
                    name="星象仪者",
                )
            ],
            monotonic_seconds=110,
        )
        self.assertIsNotNone(started)
        self.assertIsNotNone(result)
        self.assertEqual(result.elapsed_seconds, 20)
        self.assertEqual(result.time_to_enrage_seconds, 280)
        self.assertAlmostEqual(
            result.schedule_start_hp_percent,
            23_000_000 / 33_270_350 * 100.0,
        )
        self.assertAlmostEqual(
            result.actual_stage_remaining_percent,
            21_000_000 / 23_000_000 * 100.0,
        )
        self.assertAlmostEqual(result.safety_margin_seconds, 6.0869565)

    def test_first_believer_keeps_unseen_boss_in_remaining_hp(self):
        catalog = load_boss_enrage_catalog(
            Path(__file__).with_name("boss_enrage_config.json")
        )
        forecast = BossEnragePredictor(catalog)
        result = forecast.update(
            encounter_key="first-believer",
            elapsed_seconds=0,
            bosses=[
                boss(
                    2,
                    17_291_421,
                    17_291_421,
                    template_id=7_100_209,
                    name="巴尼先生",
                )
            ],
            monotonic_seconds=0,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.boss_current_hp, 54_881_466)
        self.assertEqual(result.boss_max_hp, 54_881_466)

    def test_first_believer_pre_heal_anxia_hp_does_not_reduce_final_work(self):
        catalog = load_boss_enrage_catalog(
            Path(__file__).with_name("boss_enrage_config.json")
        )
        forecast = BossEnragePredictor(catalog)
        result = forecast.update(
            encounter_key="first-believer",
            elapsed_seconds=10,
            bosses=[
                boss(
                    1,
                    20_000_000,
                    37_590_045,
                    template_id=7_100_208,
                    name="安西娅",
                ),
                boss(
                    2,
                    8_000_000,
                    17_291_421,
                    template_id=7_100_209,
                    name="巴尼先生",
                ),
            ],
            monotonic_seconds=10,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.boss_current_hp, 45_590_045)
        self.assertEqual(result.boss_max_hp, 54_881_466)

    def test_first_believer_successor_heal_preserves_total_work(self):
        catalog = load_boss_enrage_catalog(
            Path(__file__).with_name("boss_enrage_config.json")
        )
        forecast = BossEnragePredictor(catalog)
        for elapsed, anxia_hp, barney_hp in (
            (0, 37_590_045, 17_291_421),
            (20, 30_000_000, 0),
        ):
            before_heal = forecast.update(
                encounter_key="first-believer",
                elapsed_seconds=elapsed,
                bosses=[
                    boss(
                        1,
                        anxia_hp,
                        37_590_045,
                        template_id=7_100_208,
                        name="安西娅",
                    ),
                    boss(
                        2,
                        barney_hp,
                        17_291_421,
                        template_id=7_100_209,
                        name="巴尼先生",
                    ),
                ],
                monotonic_seconds=elapsed,
            )
        after_heal = forecast.update(
            encounter_key="first-believer",
            elapsed_seconds=30,
            bosses=[
                boss(
                    3,
                    37_590_045,
                    37_590_045,
                    template_id=7_100_210,
                    name="安西娅",
                )
            ],
            monotonic_seconds=30,
        )
        self.assertIsNotNone(before_heal)
        self.assertIsNotNone(after_heal)
        self.assertEqual(after_heal.boss_current_hp, 37_590_045)
        self.assertEqual(after_heal.boss_max_hp, 54_881_466)
        self.assertEqual(after_heal.elapsed_seconds, 30)
        self.assertEqual(after_heal.lifetime_hp_per_second, 0)
        self.assertGreater(after_heal.safety_margin_seconds, 0)

    def test_first_believer_clock_and_work_continue_from_barney_to_anxia(self):
        catalog = load_boss_enrage_catalog(
            Path(__file__).with_name("boss_enrage_config.json")
        )
        forecast = BossEnragePredictor(catalog)
        forecast.update(
            encounter_key="first-believer",
            elapsed_seconds=0,
            bosses=[
                boss(
                    2,
                    17_291_421,
                    17_291_421,
                    template_id=7_100_209,
                    name="巴尼先生",
                )
            ],
            monotonic_seconds=0,
        )
        forecast.update(
            encounter_key="first-believer",
            elapsed_seconds=20,
            bosses=[
                boss(
                    2,
                    8_000_000,
                    17_291_421,
                    template_id=7_100_209,
                    name="巴尼先生",
                )
            ],
            monotonic_seconds=20,
        )
        result = forecast.update(
            encounter_key="first-believer",
            elapsed_seconds=40,
            bosses=[
                boss(
                    3,
                    37_590_045,
                    37_590_045,
                    template_id=7_100_210,
                    name="安西娅",
                )
            ],
            monotonic_seconds=40,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.elapsed_seconds, 40)
        self.assertEqual(result.time_to_enrage_seconds, 440)
        self.assertEqual(result.boss_current_hp, 37_590_045)
        self.assertEqual(result.boss_max_hp, 54_881_466)
        self.assertEqual(result.lifetime_hp_per_second, 0)
        self.assertGreater(result.safety_margin_seconds, 0)

    def test_prediction_does_not_compute_team_damage_rates(self):
        forecast = predictor(enrage_seconds=100.0)
        result = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=20,
            bosses=[boss(1, 900)],
            monotonic_seconds=20,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.recent_hp_per_second, 0)
        self.assertEqual(result.lifetime_hp_per_second, 0)
        self.assertEqual(result.blended_hp_per_second, 0)
        self.assertIsNone(result.estimated_kill_seconds)
        self.assertIsNone(result.required_output_increase_percent)

    def test_state_change_waits_three_seconds(self):
        forecast = predictor(enrage_seconds=100.0, state_hold_seconds=3.0)
        initial = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=0,
            bosses=[boss(1, 1_000)],
            monotonic_seconds=0,
        )
        self.assertEqual(initial.state, "critical")
        pending = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=10,
            bosses=[boss(1, 500)],
            monotonic_seconds=10,
        )
        self.assertEqual(pending.state, "critical")
        self.assertEqual(pending.desired_state, "ample")
        still_pending = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=12.9,
            bosses=[boss(1, 500)],
            monotonic_seconds=12.9,
        )
        self.assertEqual(still_pending.state, "critical")
        changed = forecast.update(
            encounter_key="pull-1",
            elapsed_seconds=13.1,
            bosses=[boss(1, 500)],
            monotonic_seconds=13.1,
        )
        self.assertEqual(changed.state, "ample")

    def test_margin_states_and_compact_messages_use_requested_boundaries(self):
        rule = predictor().catalog.rules[0]
        cases = (
            (30.0, "ample", "充裕 · +0:30"),
            (29.9, "normal", "正常 · +0:30"),
            (10.0, "normal", "正常 · +0:10"),
            (9.9, "critical", "临界 · +0:10"),
            (-10.0, "critical", "临界 · -0:10"),
            (-10.1, "danger", "危险 · -0:10"),
        )
        from boss_enrage import _prediction_message

        for margin, expected_state, expected_message in cases:
            state = BossEnragePredictor._classify(
                rule,
                margin_seconds=margin,
                required_increase_percent=0.0,
            )
            message = _prediction_message(
                state,
                margin_seconds=margin,
                expected_remaining_percent=0.0,
                required_increase_percent=0.0,
            )
            self.assertEqual(state, expected_state)
            self.assertEqual(message, expected_message)

    def test_production_catalog_contains_only_verified_normal_bosses(self):
        catalog = load_boss_enrage_catalog(
            Path(__file__).with_name("boss_enrage_config.json")
        )
        expected = {
            7_107_030: ("karl_edgar", 300),
            7_107_081: ("karl_edgar", 300),
            7_107_105: ("lambert_noose", 180),
            7_110_641: ("rock_king", 240),
            7_107_352: ("abaddon", 300),
            7_107_450: ("abaddon", 300),
            7_102_834: ("ray_biber", 360),
            7_102_873: ("joker", 360),
            7_109_821: ("mutated_hound", 480),
            7_103_402: ("ancestral_armor", 480),
            7_103_401: ("ancestral_armor", 480),
            7_102_403: ("astrologer", 300),
            7_100_215: ("descendant_guardian", 480),
            7_100_208: ("first_believer", 480),
            7_100_209: ("first_believer", 480),
            7_100_210: ("first_believer", 480),
            7_102_990: ("viscountess", 480),
            7_102_991: ("viscountess", 480),
        }

        self.assertEqual(len(catalog.rules), 12)
        for template_id, (rule_id, seconds) in expected.items():
            matched = catalog.match(
                [boss(1, 900, template_id=template_id, name="任意名称")]
            )
            self.assertIsNotNone(matched, template_id)
            self.assertEqual(matched.rule_id, rule_id)
            self.assertEqual(matched.enrage_seconds, seconds)

        astrologer = next(
            rule for rule in catalog.rules if rule.rule_id == "astrologer"
        )
        self.assertEqual(
            astrologer.countdown_start_signal,
            "Set_MUS_B_WYZY_Boss_GuanJia_Stage2",
        )
        self.assertTrue(astrologer.first_sample_is_baseline)
        first_believer = next(
            rule for rule in catalog.rules if rule.rule_id == "first_believer"
        )
        self.assertEqual(len(first_believer.hp_slots), 2)

    def test_production_catalog_does_not_match_unverified_variants_or_dummy(self):
        catalog = load_boss_enrage_catalog(
            Path(__file__).with_name("boss_enrage_config.json")
        )
        unverified_templates = (
            7_100_201,
            7_100_202,
            7_100_203,
            7_102_400,
            7_102_932,
            7_102_936,
            7_102_938,
            7_103_001,
            7_109_801,
            7_110_581,
            7_110_642,
            7_114_223,
        )
        for template_id in unverified_templates:
            matched = catalog.match(
                [boss(1, 900, template_id=template_id, name="小丑")]
            )
            self.assertIsNone(matched, template_id)

    def test_only_lost_control_rock_king_has_enrage_prediction(self):
        catalog = load_boss_enrage_catalog(
            Path(__file__).with_name("boss_enrage_config.json")
        )
        forecast = BossEnragePredictor(catalog)
        self.assertIsNone(
            forecast.update(
                encounter_key="rock-add",
                elapsed_seconds=60,
                bosses=[boss(1, 400, template_id=7_110_642)],
                monotonic_seconds=60,
            )
        )
        result = forecast.update(
            encounter_key="rock-lost-control",
            elapsed_seconds=20,
            bosses=[boss(2, 1_800, 2_000, template_id=7_110_641)],
            monotonic_seconds=80,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.elapsed_seconds, 20)
        self.assertEqual(result.time_to_enrage_seconds, 220)


if __name__ == "__main__":
    unittest.main()
