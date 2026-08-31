#!/usr/bin/env python3

from __future__ import annotations

import runpy
import json
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).with_name("dps_meter.pyw")))
CombatModel = MODULE["CombatModel"]
DpsWindow = MODULE["DpsWindow"]
ActorStats = MODULE["ActorStats"]
IconFactory = MODULE["IconFactory"]
HookWorker = MODULE["HookWorker"]
NetworkPacketParser = MODULE["NetworkPacketParser"]
UpdateInfo = MODULE["UpdateInfo"]
CombatClockResult = MODULE["CombatClockResult"]
apply_combat_clock_to_record = MODULE["apply_combat_clock_to_record"]
APP_VERSION = MODULE["APP_VERSION"]
CLIENT_BUILD = MODULE["CLIENT_BUILD"]
PROFESSION_COLORS = MODULE["PROFESSION_COLORS"]
boss_name_is_allowed = MODULE["boss_name_is_allowed"]
load_boss_name_allowlist = MODULE["load_boss_name_allowlist"]
load_monster_catalog = MODULE["load_monster_catalog"]
load_skill_catalog = MODULE["load_skill_catalog"]
load_skill_metadata = MODULE["load_skill_metadata"]
restore_history_boss_names = MODULE["restore_history_boss_names"]
resolve_program_path = MODULE["resolve_program_path"]
window_exstyle_for_lock = MODULE["window_exstyle_for_lock"]
membership_label_for_card_tier = MODULE["membership_label_for_card_tier"]
format_duration = MODULE["format_duration"]
format_response_time = MODULE["format_response_time"]
healing_coverage_label = MODULE["healing_coverage_label"]
dps_duration_seconds = MODULE["dps_duration_seconds"]
relative_damage_bar_ratio = MODULE["relative_damage_bar_ratio"]
compact_width_for_visible_metrics = MODULE["compact_width_for_visible_metrics"]
main = MODULE["main"]

SELF_ID = 57_266_949_828_970
TEAMMATE_ID = 57_266_949_828_971
ZERO_TEAMMATE_ID = -645_797_907_948
NEARBY_ID = 57_266_949_828_999
MONSTER_ID = 57_236_882_400_409
SECOND_MONSTER_ID = 57_236_882_400_410
THIRD_MONSTER_ID = 57_236_882_400_411
BASE_FILETIME = 134_321_845_000_000_000


def damage(sequence: int, attacker: int, target: int, amount: int = 100) -> dict:
    return {
        "filetime_100ns": BASE_FILETIME + sequence * 10_000,
        "attacker_id": attacker,
        "target_id": target,
        "skill_id": 86_021_070,
        "damage": amount,
        "player_attacker": True,
    }


def team_stat(
    sequence: int,
    actor: int,
    absolute_damage: int,
    *,
    server_time: int = 0,
) -> dict:
    update = {
        "filetime_100ns": BASE_FILETIME + sequence * 10_000,
        "actor_id": actor,
        "absolute_damage": absolute_damage,
    }
    if server_time:
        update["server_time"] = server_time
    return update


def healing(
    timestamp: int,
    healer: int,
    target: int,
    *,
    total: int,
    effective: int,
    skill_id: int = 86_021_030,
) -> dict:
    return {
        "filetime_100ns": timestamp,
        "healer_id": healer,
        "target_id": target,
        "skill_id": skill_id,
        "total_healing": total,
        "effective_healing": effective,
        "overhealing": total - effective,
        "healing_source": "network_exact",
    }


def actor_health(timestamp: int, actor: int, current: int, maximum: int) -> dict:
    return {
        "filetime_100ns": timestamp,
        "entity_id": actor,
        "current_hp": current,
        "max_hp": maximum,
        "health_source": "bound_realtime_hp",
    }


class CombatModelTests(unittest.TestCase):
    @staticmethod
    def _clock_ready_model(run_id: str) -> object:
        model = CombatModel(run_id=run_id)
        model.self_id = SELF_ID
        model.party_ids = {TEAMMATE_ID}
        model.party_known = True
        model.party_member_count = 2
        model.combat_target_id = MONSTER_ID
        model.first_damage_time = 990.0
        model.last_damage_time = 1005.0
        model.stats = {
            SELF_ID: ActorStats(SELF_ID, damage=800_000),
            TEAMMATE_ID: ActorStats(TEAMMATE_ID, damage=1_200_000),
        }
        return model

    def test_combat_clock_identity_is_shared_without_uploading_entity_ids(self):
        first = self._clock_ready_model("clock-first")
        second = self._clock_ready_model("clock-second")

        first_snapshot = first.combat_clock_snapshot(1007.0)
        second_snapshot = second.combat_clock_snapshot(1008.0)

        self.assertIsNotNone(first_snapshot)
        self.assertIsNotNone(second_snapshot)
        self.assertEqual(first_snapshot["party_key"], second_snapshot["party_key"])
        self.assertEqual(first_snapshot["target_key"], second_snapshot["target_key"])
        self.assertNotEqual(
            first_snapshot["encounter_id"], second_snapshot["encounter_id"]
        )
        serialized = json.dumps(first_snapshot)
        self.assertNotIn(str(SELF_ID), serialized)
        self.assertNotIn(str(TEAMMATE_ID), serialized)
        self.assertNotIn(str(MONSTER_ID), serialized)

    def test_combat_clock_changes_only_the_duration_divisor(self):
        model = self._clock_ready_model("clock-duration")
        before_damage = {actor_id: row.damage for actor_id, row in model.stats.items()}
        active = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id="a" * 32,
            started_at=500.0,
            duration_seconds=12.0,
            server_time=512.0,
        )
        self.assertTrue(model.apply_combat_clock(active, received_at=1005.0))
        self.assertEqual(model.duration(1007.0), 14.0)

        final = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id="a" * 32,
            started_at=500.0,
            ended_at=520.9,
            duration_seconds=20.9,
            final=True,
            server_time=521.0,
        )
        self.assertTrue(model.apply_combat_clock(final, received_at=1008.0))
        self.assertEqual(model.duration(5000.0), 20.9)
        self.assertEqual(
            before_damage,
            {actor_id: row.damage for actor_id, row in model.stats.items()},
        )

    def test_final_combat_clock_recalculates_history_dps_and_hps_only(self):
        record = {
            "encounter_id": "clock-history-000001",
            "duration_seconds": 21.9,
            "dps_duration_seconds": 21.0,
            "total_damage": 2_000_000,
            "team_dps": 2_000_000 / 21,
            "participants": [
                {"actor_id": SELF_ID, "damage": 800_000, "dps": 800_000 / 21},
                {
                    "actor_id": TEAMMATE_ID,
                    "damage": 1_200_000,
                    "dps": 1_200_000 / 21,
                },
            ],
            "team_effective_healing": 105_000,
            "team_total_healing": 125_000,
            "team_hps": 105_000 / 21,
            "healers": [
                {
                    "actor_id": SELF_ID,
                    "effective_healing": 105_000,
                    "total_healing": 125_000,
                    "hps": 105_000 / 21,
                }
            ],
            "_shared_clock_request": {"state": "ended"},
        }
        result = CombatClockResult(
            synchronized=True,
            encounter_id=record["encounter_id"],
            clock_id="b" * 32,
            started_at=100.0,
            ended_at=120.9,
            duration_seconds=20.9,
            final=True,
            server_time=121.0,
        )

        updated = apply_combat_clock_to_record(record, result)

        self.assertEqual(updated["total_damage"], 2_000_000)
        self.assertEqual(updated["dps_duration_seconds"], 20.0)
        self.assertEqual(updated["team_dps"], 100_000.0)
        self.assertEqual(updated["participants"][0]["dps"], 40_000.0)
        self.assertEqual(updated["team_effective_healing"], 105_000)
        self.assertEqual(updated["team_total_healing"], 125_000)
        self.assertEqual(updated["team_hps"], 5_250.0)
        self.assertEqual(updated["healers"][0]["hps"], 5_250.0)
        self.assertNotIn("_shared_clock_request", updated)
        self.assertEqual(updated["duration_source"], "server_shared_clock")

    def test_second_instance_restores_existing_window_without_starting_ui(self):
        calls = []

        class FakeGuard:
            already_running = False

            def acquire(self):
                calls.append("acquire")
                self.already_running = True

            def close(self):
                calls.append("close")

        class UnexpectedWindow:
            def __init__(self):
                calls.append("window")

            def run(self):
                calls.append("run")

        with mock.patch.dict(
            main.__globals__,
            {
                "SingleInstanceGuard": FakeGuard,
                "activate_existing_instance": (
                    lambda attempts=0: calls.append(("activate", attempts))
                ),
                "DpsWindow": UnexpectedWindow,
            },
        ):
            main()

        self.assertEqual(calls, ["acquire", ("activate", 20), "close"])

    def test_damage_bar_is_relative_to_highest_actor_not_team_total(self):
        self.assertEqual(relative_damage_bar_ratio(4_000_000, 4_000_000), 1.0)
        self.assertEqual(relative_damage_bar_ratio(2_000_000, 4_000_000), 0.5)
        self.assertEqual(relative_damage_bar_ratio(1_000_000, 4_000_000), 0.25)
        self.assertEqual(relative_damage_bar_ratio(0, 0), 0.0)

    def test_toolbar_icons_are_nonblank_and_lock_states_are_distinct(self):
        rendered = {
            name: IconFactory._draw_toolbar_icon(name, 20, "#f4f6f8")
            for name in (
                "share",
                "reset",
                "eye",
                "eye_off",
                "lock",
                "unlock",
                "menu",
                "user",
                "settings",
                "minimize",
            )
        }
        self.assertTrue(all(image.size == (20, 20) for image in rendered.values()))
        self.assertTrue(
            all(image.getchannel("A").getbbox() is not None for image in rendered.values())
        )
        self.assertNotEqual(rendered["lock"].tobytes(), rendered["unlock"].tobytes())

    def test_dps_is_continuous_across_minute_boundaries(self):
        """FB985AA2E794890AB5: 00:59/01:00 cannot reset elapsed time."""
        model = CombatModel(run_id="minute-boundary-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩",
                "entity_type": "Boss",
                "template_id": 7_114_233,
                "boss_rank": 3,
            }
        )
        # Keep the encounter active past both boundaries without leaving an
        # idle gap that would intentionally freeze the timer.
        for sequence, elapsed in enumerate(range(0, 126, 5), start=1):
            event = damage(sequence, SELF_ID, MONSTER_ID, 100_000)
            event["filetime_100ns"] = BASE_FILETIME + elapsed * 10_000_000
            model.ingest(event)

        start = model.first_damage_time
        durations = [
            model.duration(start + offset)
            for offset in (59.999, 60.001, 119.999, 120.001)
        ]
        self.assertEqual(
            [format_duration(value) for value in durations],
            ["00:59", "01:00", "01:59", "02:00"],
        )
        self.assertAlmostEqual(durations[1] - durations[0], 0.002, places=5)
        self.assertAlmostEqual(durations[3] - durations[2], 0.002, places=5)

        total = sum(actor.damage for actor in model.current_stats())
        dps = [total / value for value in durations]
        self.assertGreater(dps[0], dps[1])
        self.assertGreater(dps[2], dps[3])

    def test_dps_uses_shared_whole_second_divisor_for_subsecond_jitter(self):
        def record_for_duration(duration: float) -> dict:
            model = CombatModel(run_id=f"dps-duration-{duration}")
            model.ingest_identity({"entity_id": SELF_ID})
            model.ingest_profile(
                {
                    "entity_id": MONSTER_ID,
                    "name": "测试 Boss",
                    "entity_type": "Boss",
                    "boss_rank": 3,
                }
            )
            opening = damage(1, SELF_ID, MONSTER_ID, 100_000)
            opening["filetime_100ns"] = BASE_FILETIME
            model.ingest(opening)
            closing = damage(2, SELF_ID, MONSTER_ID, 200_000)
            closing["filetime_100ns"] = BASE_FILETIME + round(
                duration * 10_000_000
            )
            model.ingest(closing)
            return model.build_combat_record("test")

        early_observer = record_for_duration(45.12)
        late_observer = record_for_duration(45.82)

        self.assertNotEqual(
            early_observer["duration_seconds"],
            late_observer["duration_seconds"],
        )
        self.assertEqual(early_observer["dps_duration_seconds"], 45.0)
        self.assertEqual(
            early_observer["dps_duration_seconds"],
            late_observer["dps_duration_seconds"],
        )
        self.assertEqual(early_observer["team_dps"], late_observer["team_dps"])
        self.assertEqual(
            early_observer["participants"][0]["dps"],
            late_observer["participants"][0]["dps"],
        )

    def test_profession_colors_match_game_party_tiles(self):
        self.assertEqual(
            PROFESSION_COLORS,
            {
                1_200_001: "#f2cd32",  # Bard / Singer
                1_200_002: "#7ecfa5",  # Spectator
                1_200_003: "#5869c4",  # Fortune Teller
                1_200_004: "#6687c5",  # Arbitrator
                1_200_005: "#68b6e5",  # Apprentice
                1_200_006: "#ee8c2f",  # Warrior
                1_200_007: "#a255c7",  # Mystery Pryer
            },
        )

    def test_drill_feedback_profile_repair_keeps_each_players_skills_and_damage(self):
        """FB3F3C449BEB21689D: correcting labels must not move damage rows."""
        actors = [
            57_409_759_241_038,
            57_178_363_545_090,
            57_178_363_545_088,
            57_178_363_545_084,
            57_178_363_545_087,
            57_178_363_545_089,
        ]
        names = [
            "飘飘来了·投影",
            "莫雪·投影",
            "墨爵·投影",
            "旧缘·投影",
            "碎星碎星·投影",
            "荡漾丶·投影",
        ]
        professions = [
            1_200_001,
            1_200_002,
            1_200_003,
            1_200_005,
            1_200_006,
            1_200_007,
        ]
        skills = [
            86_011_071,
            86_021_100,
            86_033_030,
            86_051_080,
            86_061_020,
            86_071_030,
        ]
        metadata = load_skill_metadata()
        model = CombatModel(
            skill_names=load_skill_catalog(),
            skill_professions=metadata.get("professions", {}),
            run_id="drill-feedback",
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "“钻头”",
                "entity_type": "Boss",
                "template_id": 7_115_080,
                "boss_rank": 3,
            }
        )
        model.ingest_party(
            {
                "entity_ids": actors,
                "member_count": 6,
                "authoritative": True,
            }
        )

        # The old build put the next projection profile on each real actor.
        for index, actor_id in enumerate(actors):
            wrong_index = (index + 1) % len(actors)
            model.ingest_profile(
                {
                    "entity_id": actor_id,
                    "name": names[wrong_index],
                    "profession_id": professions[wrong_index],
                    "entity_type": "Player",
                }
            )
            model.ingest(
                {
                    "filetime_100ns": BASE_FILETIME + index * 10_000,
                    "attacker_id": actor_id,
                    "target_id": MONSTER_ID,
                    "skill_id": skills[index],
                    "damage": 10_000 + index,
                    "player_attacker": True,
                }
            )

        # Parser correction updates only actor metadata. Existing event rows
        # stay attached to the actual damage-source entity.
        for actor_id, name, profession, skill_id in zip(
            actors, names, professions, skills
        ):
            model.ingest_profile(
                {
                    "entity_id": actor_id,
                    "name": name,
                    "profession_id": profession,
                    "entity_type": "Player",
                }
            )
            self.assertEqual(model.display_name(actor_id), name)
            self.assertEqual(model.actor_profession_id(actor_id), profession)
            self.assertEqual(model.stats[actor_id].damage, 10_000 + actors.index(actor_id))
            self.assertIn(skill_id, model.stats[actor_id].skills)
            self.assertNotEqual(
                model.display_skill_name(actor_id, skill_id), "未知技能"
            )

    def test_onefile_update_path_uses_outer_executable(self):
        resolved = resolve_program_path(
            True,
            r"D:\Apps\DpsMeter\meter.exe",
            r"C:\Users\tester\AppData\Local\Temp\onefile\meter.exe",
            r"C:\Users\tester\AppData\Local\Temp\onefile\dps_meter.pyw",
        )
        self.assertEqual(resolved, Path(r"D:\Apps\DpsMeter\meter.exe"))

    def test_known_solo_roster_does_not_hide_a_confirmed_boss_damage_actor(self):
        model = CombatModel(run_id="damage-participant-test")
        model.ingest_party(
            {"entity_ids": [], "member_count": 1, "authoritative": True}
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, NEARBY_ID, MONSTER_ID, 25_000))
        self.assertEqual(model.stats[NEARBY_ID].damage, 25_000)
        self.assertEqual(model.pending_member_events, [])

        model.ingest_identity({"entity_id": SELF_ID})
        self.assertEqual(model.stats[NEARBY_ID].damage, 25_000)
        self.assertEqual(model.pending_member_events, [])
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 2_500))
        self.assertEqual(model.stats[SELF_ID].damage, 2_500)
        self.assertEqual(set(model.stats), {SELF_ID, NEARBY_ID})

    def test_unresolved_party_actor_is_admitted_then_bound_without_delay(self):
        model = CombatModel(run_id="party-buffer-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [ZERO_TEAMMATE_ID],
                "member_count": 2,
                "authoritative": False,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )

        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 25_000))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 25_000)
        self.assertIn(TEAMMATE_ID, model.encounter_member_ids)
        self.assertNotIn(TEAMMATE_ID, model.provisional_party_ids)
        self.assertEqual(model.pending_member_events, [])

        model.merge_actor(
            {"from_actor_id": ZERO_TEAMMATE_ID, "to_actor_id": TEAMMATE_ID}
        )
        self.assertEqual(model.pending_member_events, [])
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 25_000)

    def test_mid_scene_projection_roster_shows_every_damage_actor_immediately(self):
        """FB9E953CB4B4E53FEB: a restart inside the map must not show only self."""
        model = CombatModel(run_id="mid-scene-six-player-test")
        model.ingest_identity({"entity_id": SELF_ID})
        unresolved_tokens = [-(900_000 + index) for index in range(5)]
        model.ingest_party(
            {
                "entity_ids": unresolved_tokens,
                "member_count": 6,
                "authoritative": False,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "“钻头”",
                "entity_type": "Boss",
                "template_id": 7_115_080,
                "boss_rank": 3,
            }
        )

        real_teammates = [TEAMMATE_ID + index for index in range(5)]
        all_attackers = [SELF_ID, *real_teammates]
        for index, actor_id in enumerate(all_attackers, start=1):
            event = damage(index, actor_id, MONSTER_ID, 10_000 * index)
            event["player_attacker"] = True
            model.ingest(event)
            self.assertIn(actor_id, model.stats)

        self.assertEqual(set(model.stats), set(all_attackers))
        self.assertEqual(len(model.current_stats()), 6)
        self.assertEqual(model.pending_member_events, [])
        self.assertEqual(model.provisional_party_ids, set())
        self.assertEqual(model.encounter_member_ids, set(all_attackers))

        # When network identity binding catches up, metadata replaces the
        # provisional roster without replaying or dropping existing damage.
        totals_before = {
            actor_id: stats.damage for actor_id, stats in model.stats.items()
        }
        model.ingest_party(
            {
                "entity_ids": real_teammates,
                "member_count": 6,
                "authoritative": True,
            }
        )
        self.assertEqual(
            {actor_id: stats.damage for actor_id, stats in model.stats.items()},
            totals_before,
        )
        self.assertEqual(model.provisional_party_ids, set())

    def test_full_positive_roster_admits_live_damage_actor_namespace(self):
        """FBAF3C425C269BAFE1: a full roster must not hide every teammate."""
        model = CombatModel(run_id="full-roster-live-actor-test")
        stale_roster_ids = [SELF_ID + 10_000 + index for index in range(11)]
        live_teammates = [SELF_ID + 20_000 + index for index in range(11)]
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": stale_roster_ids,
                "member_count": 12,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )

        local_hit = damage(1, SELF_ID, MONSTER_ID, 1_000)
        local_hit["player_attacker"] = True
        local_hit["party_attacker"] = True
        model.ingest(local_hit)
        for sequence, actor_id in enumerate(live_teammates, start=2):
            event = damage(sequence, actor_id, MONSTER_ID, sequence * 1_000)
            event["player_attacker"] = True
            event["party_attacker"] = True
            model.ingest(event)

        self.assertEqual(set(model.stats), {SELF_ID, *live_teammates})
        self.assertEqual(len(model.current_stats()), 12)
        self.assertEqual(model.encounter_team_size, 12)
        self.assertEqual(model.provisional_party_ids, set())

        overflow = damage(20, NEARBY_ID, MONSTER_ID, 999_999)
        overflow["player_attacker"] = True
        overflow["party_attacker"] = True
        model.ingest(overflow)
        self.assertNotIn(NEARBY_ID, model.stats)

    def test_party_profession_conflict_does_not_hide_confirmed_boss_damage(self):
        model = CombatModel(run_id="damage-over-roster-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [SELF_ID + 10_000],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        event = damage(1, NEARBY_ID, MONSTER_ID, 100_000)
        event["player_attacker"] = True
        event["party_attacker"] = False
        model.ingest(event)
        self.assertEqual(model.stats[NEARBY_ID].damage, 100_000)

    def test_single_line_notice_is_compact(self):
        compact = DpsWindow._notice_dimensions("已复制到剪贴板。")
        detailed = DpsWindow._notice_dimensions(
            "第一行包含较长的说明文字，需要自动换行并增加高度。\n第二行继续说明。"
        )
        self.assertEqual(compact, (350, 132))
        self.assertGreater(detailed[1], compact[1])

    def test_window_lock_enables_click_through_and_restores_managed_style_bits(self):
        original = 0x00000080
        locked = window_exstyle_for_lock(original, True)
        self.assertEqual(locked, original | 0x00080020)
        self.assertEqual(window_exstyle_for_lock(locked, False, original), original)

        layered_original = original | 0x00080000
        layered_locked = window_exstyle_for_lock(layered_original, True)
        self.assertEqual(
            window_exstyle_for_lock(layered_locked, False, layered_original),
            layered_original,
        )

    def test_window_lock_temporarily_forces_topmost_and_restores_preference(self):
        class FakeRoot:
            def __init__(self, topmost):
                self.topmost = topmost
                self.lift_calls = 0
                self.focus_calls = 0

            def attributes(self, name, *values):
                self.assert_attribute_name(name)
                if values:
                    self.topmost = bool(values[0])
                return self.topmost

            @staticmethod
            def assert_attribute_name(name):
                if name != "-topmost":
                    raise AssertionError(name)

            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def state():
                return "normal"

            def lift(self):
                self.lift_calls += 1

            def focus_force(self):
                self.focus_calls += 1

        def make_window(topmost):
            window = object.__new__(DpsWindow)
            window.root = FakeRoot(topmost)
            window.config = {"topmost": topmost}
            window.closing = False
            window.window_locked = True
            window.window_lock_topmost_restore = None
            window.main_content_overlay_window = None
            window.main_content_overlay_topmost = None
            window._apply_main_transparency = lambda: None
            window._set_window_click_through = lambda _root, _locked: True
            window._show_unlock_window = lambda: None
            window._destroy_unlock_window = lambda: None
            window._sync_action_buttons = lambda: None
            window._draw_main_header = lambda: None
            return window

        window = make_window(False)
        window._apply_window_lock_state()
        self.assertTrue(window.root.topmost)
        self.assertFalse(window.window_lock_topmost_restore)
        self.assertFalse(window.config["topmost"])

        window.model = mock.Mock(runtime_skill_names={})
        window.hide_names = False
        window.show_total_damage = True
        window.show_dps = True
        window.show_damage_share = True
        window.show_critical_rate = True
        window.ui_font_size = 10
        window.window_alpha = 0.85
        window.history_window = None
        window._remember_root_geometry = lambda: None
        with mock.patch.dict(
            DpsWindow._save_preferences.__globals__,
            {"save_config": lambda _config: None},
        ):
            window._save_preferences()
        self.assertFalse(window.config["topmost"])

        window.window_locked = False
        window._apply_window_lock_state()
        self.assertFalse(window.root.topmost)
        self.assertIsNone(window.window_lock_topmost_restore)
        self.assertEqual(window.root.lift_calls, 1)
        self.assertEqual(window.root.focus_calls, 1)

        pinned = make_window(True)
        pinned._apply_window_lock_state()
        pinned.window_locked = False
        pinned._apply_window_lock_state()
        self.assertTrue(pinned.root.topmost)
        self.assertEqual(pinned.root.lift_calls, 1)

        startup_locked = make_window(True)
        startup_locked.config["topmost"] = False
        startup_locked.window_lock_topmost_restore = False
        startup_locked._apply_window_lock_state()
        startup_locked.window_locked = False
        startup_locked._apply_window_lock_state()
        self.assertFalse(startup_locked.root.topmost)

    def test_action_button_hover_survives_periodic_state_sync(self):
        class FakeButton:
            def __init__(self, icon_name):
                self._icon_name = icon_name
                self._description = ""
                self._normal_color = MODULE["TEXT"]
                self._disabled = False
                self._hovered = False
                self.options = {}
                self.image = None

            @staticmethod
            def winfo_exists():
                return True

            def configure(self, **options):
                self.options.update(options)

        class FakeIcons:
            @staticmethod
            def toolbar(icon_name, size, color):
                return icon_name, size, color

        window = object.__new__(DpsWindow)
        window.icons = FakeIcons()
        window.window_locked = False
        window.hide_names = False
        window.lock_button = FakeButton("unlock")
        window.privacy_button = FakeButton("eye")
        window.reset_button = FakeButton("reset")
        window.pin_button = None
        window.compact_button = None
        window.model = mock.Mock()
        window.model.combat_in_progress.return_value = False
        window._hide_main_tooltip = lambda: None

        buttons = (
            window.reset_button,
            window.privacy_button,
            window.lock_button,
        )
        for button in buttons:
            window._main_icon_enter(button)
        window._sync_action_buttons()

        for button in buttons:
            self.assertTrue(button._hovered)
            self.assertEqual(button.options["bg"], MODULE["PANEL_2"])
            self.assertEqual(button.image[2], MODULE["ACCENT"])

    def test_membership_label_follows_server_card_tier(self):
        self.assertEqual(membership_label_for_card_tier("partner"), "莫雪的小伙伴")
        self.assertEqual(membership_label_for_card_tier("monthly"), "VVVVVIP用户")
        self.assertEqual(membership_label_for_card_tier("weekly"), "VIP用户")
        self.assertEqual(membership_label_for_card_tier("normal"), "尊贵的用户")
        self.assertEqual(membership_label_for_card_tier("unknown"), "尊贵的用户")

    @unittest.skipUnless(sys.platform == "win32", "requires native Windows HWNDs")
    def test_window_lock_is_written_to_real_tk_top_level_handle(self):
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-alpha", 0.85)
        root.update_idletasks()
        window = DpsWindow.__new__(DpsWindow)
        window.window_lock_original_styles = {}
        try:
            hwnd = window._win32_root_handle(root)
            original = window._win32_extended_style(hwnd)
            self.assertIsNotNone(original)
            self.assertTrue(window._set_window_click_through(root, True))
            locked = window._win32_extended_style(hwnd)
            self.assertIsNotNone(locked)
            self.assertEqual(locked & 0x00080020, 0x00080020)
            self.assertTrue(window._set_window_click_through(root, False))
            restored = window._win32_extended_style(hwnd)
            self.assertIsNotNone(restored)
            self.assertEqual(restored & 0x00080020, original & 0x00080020)
        finally:
            root.destroy()

    def test_star_guard_damage_merges_into_astrologer_encounter(self):
        guard_id = SECOND_MONSTER_ID + 10_000
        model = CombatModel(run_id="astrologer-guard-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_rank": 3,
            }
        )
        model.ingest_profile(
            {
                "entity_id": guard_id,
                "name": "星光守卫",
                "entity_type": "Monster",
                "template_id": 7_102_405,
                "encounter_auxiliary": True,
                "encounter_parent_template_ids": [7_102_403],
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, TEAMMATE_ID, guard_id, 200_000))

        self.assertEqual(model.combat_target_id, MONSTER_ID)
        self.assertEqual(model.current_monster().name, "星象仪者")
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200_000)
        self.assertEqual(
            sum(actor.damage for actor in model.current_stats()),
            300_000,
        )
        self.assertEqual(
            [
                (row["name"], row["kind"], row["damage"])
                for row in model.actor_target_rows(TEAMMATE_ID)
            ],
            [("星光守卫", "小怪", 200_000)],
        )
        self.assertNotIn(guard_id, model.stats)
        self.assertIn(guard_id, model._encounter_damage_target_ids())

    def test_astrologer_guards_and_imprisonments_group_separately(self):
        """FB7408AA158422D3F4: unknown targets 1/3/5 are imprisonments."""
        model = CombatModel(run_id="astrologer-auxiliary-groups-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        guard_ids = [SECOND_MONSTER_ID + 20_000 + index for index in range(6)]
        imprisonment_ids = [
            SECOND_MONSTER_ID + 20_006 + index for index in range(3)
        ]
        for guard_id in guard_ids:
            model.ingest_profile(
                {
                    "entity_id": guard_id,
                    "name": "星光守卫",
                    "entity_type": "Monster",
                    "template_id": 7_102_405,
                    "encounter_auxiliary": True,
                    "encounter_parent_template_ids": [7_102_403],
                }
            )
        for sequence, target_id in enumerate(
            [*guard_ids, *imprisonment_ids], start=2
        ):
            model.ingest(damage(sequence, SELF_ID, target_id, 10_000))

        rows = {
            row["name"]: row for row in model.actor_target_rows(SELF_ID)
        }
        self.assertEqual(rows["星光守卫"]["damage"], 60_000)
        self.assertEqual(set(rows["星光守卫"]["entity_ids"]), set(guard_ids))
        self.assertEqual(rows["禁锢"]["damage"], 30_000)
        self.assertEqual(set(rows["禁锢"]["entity_ids"]), set(imprisonment_ids))
        self.assertFalse(
            any(row["name"].startswith("小怪 ") for row in model.actor_target_rows(SELF_ID))
        )
        for imprisonment_id in imprisonment_ids:
            self.assertEqual(
                model.monsters[imprisonment_id].template_id, 7_102_404
            )
            self.assertTrue(
                model.monsters[imprisonment_id].encounter_auxiliary
            )

    def test_late_star_guard_identity_removes_attacker_but_keeps_target_damage(self):
        guard_id = SECOND_MONSTER_ID + 10_050
        model = CombatModel(run_id="late-star-guard-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))

        # Missing metadata initially uses the encounter's template-less
        # auxiliary identity. Explicit native metadata must correct it later.
        mistaken_guard_hit = damage(2, guard_id, guard_id, 9_148)
        model.ingest(mistaken_guard_hit)
        model.ingest(damage(3, SELF_ID, guard_id, 50_000))
        self.assertNotIn(guard_id, model.stats)
        self.assertEqual(model.stats[SELF_ID].damage, 150_000)
        self.assertEqual(model.stats[SELF_ID].target_damage[guard_id], 50_000)
        self.assertEqual(model.display_target_name(guard_id), "禁锢")

        model.ingest_profile(
            {
                "entity_id": guard_id,
                "name": "星光守卫",
                "entity_type": "Monster",
                "template_id": 7_102_405,
                "boss_type": 1,
                "encounter_auxiliary": True,
                "encounter_parent_template_ids": [7_102_403],
            }
        )

        self.assertNotIn(guard_id, model.stats)
        self.assertFalse(
            any(
                int(event.get("attacker_id", 0)) == guard_id
                for event in model.events
            )
        )
        self.assertEqual(model.stats[SELF_ID].damage, 150_000)
        self.assertEqual(model.stats[SELF_ID].target_damage[guard_id], 50_000)
        self.assertIn(guard_id, model._encounter_damage_target_ids())

        # A later erroneous team-profile binding must not rename the monster
        # or make it a player again.
        model.ingest_profile(
            {
                "entity_id": guard_id,
                "name": "错误的队员名字",
                "entity_type": "Player",
                "profession_id": 1_200_003,
            }
        )
        self.assertEqual(model.display_target_name(guard_id), "星光守卫")
        self.assertNotIn(guard_id, model.entity_professions)
        self.assertNotIn(guard_id, model._current_member_ids())

    def test_astrologer_guard_death_keeps_boss_hp_and_same_encounter(self):
        guard_id = SECOND_MONSTER_ID + 10_001
        model = CombatModel(run_id="astrologer-phase-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 33_270_350,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest_profile(
            {
                "entity_id": guard_id,
                "name": "星光守卫",
                "entity_type": "Monster",
                "template_id": 7_102_405,
                "encounter_auxiliary": True,
                "encounter_parent_template_ids": [7_102_403],
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        encounter_id = model.encounter_id
        model.ingest(damage(2, TEAMMATE_ID, guard_id, 200_000))
        model.ingest_monster(
            {
                "entity_id": guard_id,
                "current_hp": 0,
                "max_hp": 1_325_792,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 3 * 10_000_000,
            }
        )
        # Even a late, wrongly routed small max-HP update cannot shrink the
        # already locked Boss ceiling.
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 30_000_000,
                "max_hp": 1_325_792,
                "filetime_100ns": BASE_FILETIME + 4 * 10_000_000,
            }
        )

        self.assertFalse(model.combat_end_time)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.current_monster().name, "星象仪者")
        self.assertEqual(model.current_monster().current_hp, 30_000_000)
        self.assertEqual(model.current_monster().max_hp, 33_270_350)
        self.assertEqual(sum(row.damage for row in model.stats.values()), 300_000)

    def test_boss_name_allowlist_normalizes_punctuation_and_excludes_protect_add(self):
        allowlist = load_boss_name_allowlist()
        self.assertTrue(boss_name_is_allowed("瑞尔·比伯", allowlist))
        self.assertTrue(boss_name_is_allowed("英雄周本-瑞尔比伯", allowlist))
        self.assertTrue(boss_name_is_allowed('"剥面人" 强尼', allowlist))
        self.assertTrue(boss_name_is_allowed("剥面人·强尼", allowlist))
        self.assertTrue(boss_name_is_allowed("梦境捕手", allowlist))
        self.assertFalse(boss_name_is_allowed("纸人替身-保护", allowlist))
        catalog = load_monster_catalog()
        for template_id in ("7102873", "7103012", "7103309"):
            self.assertIn(template_id, catalog)
            self.assertEqual(catalog[template_id]["name"], "小丑")
        for template_id in (
            "7110820",
            "7110825",
            "7115020",
            "7115026",
            "7265156",
        ):
            self.assertIn(template_id, catalog)
            self.assertEqual(catalog[template_id]["boss_type"], 3)
        self.assertEqual(catalog["7265156"]["name"], "梦境捕手")
        self.assertNotIn("7102892", catalog)
        self.assertNotIn("7110510", catalog)
        self.assertNotIn("7110551", catalog)
        self.assertNotIn("7110616", catalog)
        self.assertNotIn("7113014", catalog)
        self.assertIn("7110641", catalog)
        self.assertIn("7110642", catalog)

    def test_placeholder_name_cannot_replace_real_boss_name(self):
        model = CombatModel(run_id="boss-placeholder-name-test")
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "异化猎犬",
                "entity_type": "Boss",
                "template_id": 7_109_821,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "首领",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )

        self.assertEqual(model.entity_names[MONSTER_ID], "异化猎犬")
        self.assertEqual(model.monsters[MONSTER_ID].name, "异化猎犬")

    def test_history_placeholder_boss_names_are_restored_from_template(self):
        record = {
            "monster": {
                "entity_id": MONSTER_ID,
                "template_id": 7_109_821,
                "name": "首领",
            },
            "targets": [
                {
                    "entity_id": MONSTER_ID,
                    "template_id": 7_109_821,
                    "name": "未命名Boss",
                }
            ],
            "participants": [
                {
                    "actor_id": SELF_ID,
                    "targets": [
                        {
                            "entity_id": MONSTER_ID,
                            "entity_ids": [MONSTER_ID],
                            "kind": "Boss",
                            "name": "Boss",
                            "damage": 100,
                        },
                        {
                            "entity_id": 0,
                            "kind": "汇总",
                            "name": "未分配目标",
                            "damage": 50,
                        },
                    ],
                }
            ],
        }
        restored = restore_history_boss_names(
            record,
            {
                "7109821": {
                    "boss_type": 3,
                    "name": "异化猎犬",
                }
            },
        )

        self.assertEqual(record["monster"]["name"], "首领")
        self.assertEqual(restored["monster"]["name"], "异化猎犬")
        self.assertEqual(restored["targets"][0]["name"], "异化猎犬")
        self.assertEqual(
            restored["participants"][0]["targets"][0]["name"],
            "异化猎犬",
        )
        self.assertEqual(
            restored["participants"][0]["targets"][1]["name"],
            "未分配目标",
        )

    def test_boss_profile_timestamp_makes_target_immediately_visible(self):
        model = CombatModel(run_id="boss-profile-activity-test")
        model.latest_network_time_100ns = BASE_FILETIME

        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "瑞尔·比伯",
                "entity_type": "Boss",
                "template_id": 7_102_834,
                "boss_type": 3,
                "boss_rank": 3,
                "filetime_100ns": BASE_FILETIME + 10_000,
            }
        )

        self.assertEqual(model.current_monster().entity_id, MONSTER_ID)
        self.assertEqual(
            model.target_activity_100ns[MONSTER_ID], BASE_FILETIME + 10_000
        )

    def test_explicit_boss_death_accepts_late_common_without_reopening(self):
        model = CombatModel(run_id="explicit-death-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest_combat_state(
            {
                "entity_id": SELF_ID,
                "in_combat": True,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            }
        )
        death_time = BASE_FILETIME + 5 * 10_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertFalse(model.combat_end_time)
        model.ingest_combat_state(
            {
                "entity_id": SELF_ID,
                "in_combat": False,
                "filetime_100ns": death_time + 10_000,
            }
        )
        self.assertTrue(model.combat_end_time)
        self.assertEqual(model.combat_end_reason, "target_defeated")
        self.assertTrue(model.finalize_if_idle(model.combat_end_time))
        ended_at = model.combat_end_time
        self.assertTrue(model.ingest_team_stat(team_stat(6, SELF_ID, 900_000)))
        self.assertEqual(model.stats[SELF_ID].damage, 900_000)
        self.assertEqual(model.combat_end_time, ended_at)
        self.assertFalse(model.combat_in_progress(ended_at + 1.0))

    def test_same_template_respawn_ends_wipe_with_stale_local_flag(self):
        model = CombatModel(run_id="combat-state-wipe-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "template_id": 7_115_080,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 5_000_000,
                "max_hp": 7_500_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 200))
        for sequence, entity_id in enumerate(
            (SELF_ID, TEAMMATE_ID, MONSTER_ID), start=3
        ):
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                }
            )
        model.ingest_combat_state(
            {
                "entity_id": TEAMMATE_ID,
                "in_combat": False,
                "filetime_100ns": BASE_FILETIME + 6 * 10_000,
            }
        )
        self.assertFalse(model.combat_end_time)
        wipe_time = BASE_FILETIME + 7 * 10_000
        model.ingest_combat_state(
            {
                "entity_id": MONSTER_ID,
                "in_combat": False,
                "filetime_100ns": wipe_time,
            }
        )
        self.assertFalse(model.combat_end_time)
        replacement_time = BASE_FILETIME + 8 * 10_000
        model.ingest_profile(
            {
                "entity_id": SECOND_MONSTER_ID,
                "entity_type": "Boss",
                "template_id": 7_115_080,
                "boss_rank": 3,
                "filetime_100ns": replacement_time,
            }
        )

        self.assertEqual(model.combat_end_reason, "party_wipe")
        self.assertEqual(
            model.combat_end_time,
            model._event_seconds({"filetime_100ns": replacement_time}),
        )
        self.assertFalse(model.combat_in_progress(model.combat_end_time + 0.1))

    def test_multiphase_boss_fight_mode_gap_does_not_end_encounter(self):
        model = CombatModel(run_id="multiphase-combat-state-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID], "member_count": 2})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 20_000_000,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 200))
        for sequence, (entity_id, in_combat) in enumerate(
            (
                (SELF_ID, True),
                (TEAMMATE_ID, True),
                (MONSTER_ID, True),
                (TEAMMATE_ID, False),
                (MONSTER_ID, False),
            ),
            start=3,
        ):
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": in_combat,
                    "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                }
            )
        model.ingest_profile(
            {
                "entity_id": SECOND_MONSTER_ID,
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_rank": 3,
                "filetime_100ns": BASE_FILETIME + 8 * 10_000,
            }
        )

        self.assertFalse(model.combat_end_time)

    def test_explicit_boss_death_is_not_delayed_by_recent_add_damage(self):
        model = CombatModel(run_id="boss-add-death-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, TEAMMATE_ID, SECOND_MONSTER_ID, 50_000))
        self.assertIn(SECOND_MONSTER_ID, model.encounter_add_target_ids)
        for entity_id in (SELF_ID, TEAMMATE_ID):
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                }
            )

        death_time = BASE_FILETIME + 5 * 10_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertFalse(model.combat_end_time)
        for sequence, entity_id in enumerate((SELF_ID, TEAMMATE_ID), start=1):
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": False,
                    "filetime_100ns": death_time + sequence * 10_000,
                }
            )

        self.assertEqual(
            model.combat_end_time,
            model._event_seconds({"filetime_100ns": death_time}),
        )
        self.assertEqual(model.combat_end_reason, "target_defeated")
        frozen_duration = model.duration(model.combat_end_time + 30)
        self.assertEqual(frozen_duration, model.duration(model.combat_end_time + 300))

    def test_zero_hp_daily_boss_ignores_late_linked_monster_damage(self):
        """FBD2EA0E4E3620B20E: post-Boss trash cannot change DPS."""
        model = CombatModel(run_id="daily-boss-zero-hp-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "洛克·金",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 120_110,
                "max_hp": 120_110,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 50_000))
        model.ingest(damage(2, TEAMMATE_ID, SECOND_MONSTER_ID, 20_000))
        self.assertIn(SECOND_MONSTER_ID, model.encounter_add_target_ids)
        total_before = sum(actor.damage for actor in model.stats.values())
        last_damage_before = model.last_damage_time

        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "filetime_100ns": BASE_FILETIME + 3 * 10_000,
            }
        )
        late_add_hit = damage(4, TEAMMATE_ID, SECOND_MONSTER_ID, 999_999)
        model.ingest(late_add_hit)

        self.assertEqual(
            sum(actor.damage for actor in model.stats.values()), total_before
        )
        self.assertEqual(model.last_damage_time, last_damage_before)
        self.assertFalse(model.active(model._event_seconds(late_add_hit)))

    def test_full_party_wipe_ends_pull_and_next_hit_starts_fresh(self):
        model = CombatModel(run_id="party-wipe-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 200_000))
        first_encounter = model.encounter_id

        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                }
            )
        )
        self.assertFalse(model.combat_end_time)
        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": TEAMMATE_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + 4 * 10_000,
                }
            )
        )
        self.assertTrue(model.combat_end_time)
        self.assertEqual(model.combat_end_reason, "party_wipe")
        self.assertTrue(model.finalize_if_idle(model.combat_end_time))

        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": False,
                    "filetime_100ns": BASE_FILETIME + 5 * 10_000,
                }
            )
        )
        model.ingest(damage(6, SELF_ID, MONSTER_ID, 50_000))

        self.assertNotEqual(model.encounter_id, first_encounter)
        self.assertFalse(model.combat_end_time)
        self.assertEqual(model.stats[SELF_ID].damage, 50_000)
        self.assertNotIn(TEAMMATE_ID, model.stats)
        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["archive_reason"], "party_wipe")
        self.assertEqual(history[0]["total_damage"], 300_000)

    def test_partial_roster_cannot_trigger_party_wipe(self):
        model = CombatModel(run_id="partial-party-wipe-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 3,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=2):
            model.ingest_life(
                {
                    "actor_id": actor_id,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                }
            )
        self.assertFalse(model.combat_end_time)

    def test_confirmed_active_boss_replacement_freezes_incomplete_wipe(self):
        model = CombatModel(run_id="active-boss-wipe-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        for entity_id in (MONSTER_ID, SECOND_MONSTER_ID):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": "Drill",
                    "entity_type": "Boss",
                    "template_id": 7_115_080,
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest_team_stat(team_stat(2, SELF_ID, 500))
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 3 * 10_000,
            }
        )
        self.assertFalse(model.combat_end_time)

        model.ingest_active_boss(
            {
                "entity_id": SECOND_MONSTER_ID,
                "name": "Drill",
                "template_id": 7_115_080,
                "filetime_100ns": BASE_FILETIME + 4 * 10_000,
            }
        )

        self.assertEqual(model.combat_end_time, model.last_damage_time)
        self.assertEqual(model.combat_end_reason, "boss_replaced")
        self.assertFalse(model.combat_in_progress(model.last_damage_time + 0.1))
        self.assertEqual(model.pending_active_boss_id, SECOND_MONSTER_ID)

        model.ingest_team_stat(team_stat(5, SELF_ID, 0))
        self.assertEqual(model.combat_target_id, SECOND_MONSTER_ID)
        self.assertIsNone(model.pending_active_boss_id)
        model.ingest(damage(6, SELF_ID, SECOND_MONSTER_ID, 200))

        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["archive_reason"], "boss_replaced")
        self.assertEqual(history[0]["monster"]["entity_id"], MONSTER_ID)
        self.assertEqual(history[0]["total_damage"], 500)
        self.assertEqual(model.current_monster().entity_id, SECOND_MONSTER_ID)
        self.assertEqual(model.stats[SELF_ID].damage, 200)

    def test_active_boss_damage_switches_target_before_idle_timeout(self):
        model = CombatModel(run_id="active-boss-first-hit-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id in (MONSTER_ID, SECOND_MONSTER_ID):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": "Drill",
                    "entity_type": "Boss",
                    "template_id": 7_115_080,
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        repull = damage(2, SELF_ID, SECOND_MONSTER_ID, 250)
        repull["active_boss"] = True
        model.ingest(repull)

        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_damage"], 100)
        self.assertEqual(history[0]["monster"]["entity_id"], MONSTER_ID)
        self.assertEqual(model.combat_target_id, SECOND_MONSTER_ID)
        self.assertEqual(model.stats[SELF_ID].damage, 250)

    def test_higher_level_add_cannot_replace_living_boss(self):
        model = CombatModel(run_id="boss-lock-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "小丑",
                "entity_type": "Boss",
                "boss_rank": 3,
                "level": 52,
            }
        )
        model.ingest_profile(
            {
                "entity_id": SECOND_MONSTER_ID,
                "name": "纸人替身-保护",
                "entity_type": "Boss",
                "boss_rank": 3,
                "level": 99,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 20_000_000,
                "max_hp": 20_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, TEAMMATE_ID, SECOND_MONSTER_ID, 500_000))
        self.assertEqual(model.combat_target_id, MONSTER_ID)
        self.assertEqual(set(model.stats), {SELF_ID})
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)

    def test_ancestor_armor_and_baldwin_share_one_encounter(self):
        model = CombatModel(run_id="boss-phase-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, name in (
            (MONSTER_ID, "先祖铠甲"),
            (SECOND_MONSTER_ID, "伯德温·威瑟尔"),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": name,
                    "entity_type": "Boss",
                    "boss_rank": 3,
                }
            )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        encounter_id = model.encounter_id
        model.ingest(damage(2, TEAMMATE_ID, SECOND_MONSTER_ID, 200_000))
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.combat_target_id, SECOND_MONSTER_ID)
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200_000)
        self.assertEqual(
            model._encounter_damage_target_ids(),
            {MONSTER_ID, SECOND_MONSTER_ID},
        )
        record = model.build_combat_record("test")
        self.assertEqual(record["monster"]["name"], "伯德温·威瑟尔")
        self.assertEqual(record["total_damage"], 300_000)
        self.assertEqual(len(record["targets"]), 2)

    def test_generic_ancestor_to_baldwin_long_transition_stays_one_encounter(self):
        """FBFDDB9EEE0DF9294F/FB3B530638A93D7659: P1 and P2 are one pull."""
        model = CombatModel(run_id="generic-boss-phase-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, template_id in (
            (MONSTER_ID, 7_103_402),
            (SECOND_MONSTER_ID, 7_103_401),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": "首领",
                    "entity_type": "Boss",
                    "template_id": template_id,
                    "boss_rank": 3,
                }
            )
        first = damage(1, SELF_ID, MONSTER_ID, 100_000)
        first["filetime_100ns"] = BASE_FILETIME
        second = damage(2, TEAMMATE_ID, SECOND_MONSTER_ID, 200_000)
        second["filetime_100ns"] = BASE_FILETIME + 65 * 10_000_000

        model.ingest(first)
        encounter_id = model.encounter_id
        model.ingest(second)

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.combat_target_id, SECOND_MONSTER_ID)
        self.assertEqual(
            {actor_id: actor.damage for actor_id, actor in model.stats.items()},
            {SELF_ID: 100_000, TEAMMATE_ID: 200_000},
        )
        self.assertEqual(
            model._encounter_damage_target_ids(),
            {MONSTER_ID, SECOND_MONSTER_ID},
        )

    def test_baldwin_to_new_ancestor_starts_a_fresh_pull(self):
        model = CombatModel(run_id="boss-phase-repull-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, name in (
            (MONSTER_ID, "先祖铠甲"),
            (SECOND_MONSTER_ID, "伯德温·威瑟尔"),
            (THIRD_MONSTER_ID, "先祖铠甲"),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": name,
                    "entity_type": "Boss",
                    "boss_rank": 3,
                }
            )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, TEAMMATE_ID, SECOND_MONSTER_ID, 200_000))
        previous_encounter = model.encounter_id

        model.ingest(damage(3, SELF_ID, THIRD_MONSTER_ID, 50_000))

        self.assertNotEqual(model.encounter_id, previous_encounter)
        self.assertEqual(model.combat_target_id, THIRD_MONSTER_ID)
        self.assertEqual(set(model.stats), {SELF_ID})
        self.assertEqual(model.stats[SELF_ID].damage, 50_000)
        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_damage"], 300_000)
        self.assertEqual(history[0]["monster"]["name"], "伯德温·威瑟尔")

    def test_lost_control_lokin_starts_a_separate_encounter(self):
        model = CombatModel(run_id="lokin-separate-encounter-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, template_id, name, max_hp in (
            (MONSTER_ID, 7_110_642, "洛克·金", 600_551),
            (SECOND_MONSTER_ID, 7_110_641, "洛克·金·失控", 2_161_985),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "name": name,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": max_hp,
                    "max_hp": max_hp,
                    "filetime_100ns": BASE_FILETIME,
                }
            )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        first_encounter_id = model.encounter_id
        model.ingest(damage(2, SELF_ID, SECOND_MONSTER_ID, 250_000))

        self.assertNotEqual(model.encounter_id, first_encounter_id)
        self.assertEqual(model.combat_target_id, SECOND_MONSTER_ID)
        self.assertEqual(model.current_monster().name, "洛克·金·失控")
        self.assertEqual(model.stats[SELF_ID].damage, 250_000)
        previous = model.pop_completed_combats()
        self.assertEqual(len(previous), 1)
        self.assertEqual(previous[0]["monster"]["template_id"], 7_110_642)
        self.assertEqual(previous[0]["total_damage"], 100_000)

    def test_same_scene_full_refresh_archives_and_clears_combat(self):
        model = CombatModel(run_id="same-scene-refresh-test")
        model.ingest_scene({"scene_id": 5_200_002})
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 88_000))
        self.assertTrue(
            model.ingest_scene({"scene_id": 5_200_002, "force_reset": True})
        )
        self.assertEqual(model.stats, {})
        self.assertIsNone(model.current_monster())
        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["archive_reason"], "scene_refresh")

    def test_self_party_exit_archives_combat_and_returns_to_idle(self):
        model = CombatModel(run_id="party-exit-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 88_000))

        self.assertTrue(
            model.ingest_party(
                {
                    "entity_ids": [],
                    "member_count": 0,
                    "authoritative": True,
                    "left_team": True,
                }
            )
        )
        self.assertFalse(model.active(model.last_damage_time))
        self.assertEqual(model.stats, {})
        self.assertIsNone(model.current_monster())
        self.assertEqual(model.party_ids, set())
        self.assertEqual(model.party_member_count, 1)
        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["archive_reason"], "party_exit")
        self.assertEqual(history[0]["total_damage"], 88_000)

    def test_first_scene_refresh_without_active_boss_ends_restored_encounter(self):
        model = CombatModel(run_id="first-scene-exit-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 88_000))

        self.assertTrue(
            model.ingest_scene(
                {"scene_id": 5_200_002, "entity_ids": [SELF_ID]}
            )
        )
        self.assertEqual(model.stats, {})
        self.assertIsNone(model.current_monster())
        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["archive_reason"], "scene_change")

    def test_first_scene_refresh_with_active_boss_preserves_encounter(self):
        model = CombatModel(run_id="first-scene-preserve-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 88_000))
        encounter_id = model.encounter_id

        self.assertFalse(
            model.ingest_scene(
                {
                    "scene_id": 5_200_001,
                    "entity_ids": [SELF_ID, MONSTER_ID],
                }
            )
        )
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats[SELF_ID].damage, 88_000)

    def test_v011_keeps_feedback_update_lock_and_fixed_target_scope(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertEqual(APP_VERSION, "0.0.15")
        self.assertEqual(CLIENT_BUILD, "0.0.15+20260901.1")
        self.assertIn('self.config["topmost"] = True', source)
        self.assertNotIn("toggle_boss_only", source)
        self.assertNotIn('self.footer, "只读 BOSS"', source)
        self.assertIn('(\"反馈\", self._feedback_selected_history)', source)
        self.assertIn(
            'lambda _event: self._start_update_check(manual=True)', source
        )
        self.assertIn("UPDATE_DIR = APP_DIR", source)
        self.assertIn('text=f"保存位置：{UPDATE_DIR}"', source)
        self.assertIn('text="附带运行摘要"', source)
        self.assertNotIn("load_recent_restart_context", source)
        self.assertNotIn("restart_target_profiles", source)
        self.assertIn("entity_names={}", source)
        self.assertIn("self.lock_button = self._main_icon_button(", source)
        self.assertIn('tags=("compact_lock", "compact_lock_bg")', source)
        self.assertIn("click_through_applied = self._set_window_click_through(", source)
        self.assertIn("get_ancestor = user32.GetAncestor", source)
        self.assertIn('unlock_icon = self.icons.toolbar("unlock", 16, ACCENT)', source)
        self.assertIn('self.config["window_locked"] = self.window_locked', source)
        self.assertNotIn("不包含完整封包", source)
        self.assertNotIn("分享功能将在后续版本开放", source)
        self.assertIn('actions, "share", "分享", self._share_current', source)
        self.assertNotIn("clear_if_expired", source)
        self.assertIn('"critical": "暴击率"', source)
        self.assertIn('("显示死亡次数", tk.BooleanVar', source)
        self.assertIn('"critical": critical_text', source)
        self.assertIn('("hps", "HPS", True)', source)
        self.assertNotIn('"HDPS"', source)
        self.assertIn(
            "self.titlebar = tk.Frame(self.body, bg=BG, height=44)",
            source,
        )
        self.assertIn("self.summary = tk.Frame(self.body, bg=BG, height=76)", source)
        self.assertIn("height=36,\n            bg=BG,", source)
        self.assertIn("height=28,\n            bg=BG,", source)
        self.assertIn(
            "self.rows_canvas = tk.Canvas(\n            self.table_panel,\n            bg=BG,",
            source,
        )
        self.assertIn(
            'window.attributes("-transparentcolor", MAIN_CONTENT_OVERLAY_KEY)',
            source,
        )
        self.assertIn(
            "draw_empty_state=False",
            source,
        )
        self.assertIn('"dps": "秒伤"', source)
        self.assertIn('("显示秒伤", self.settings_show_dps_var, False)', source)
        self.assertIn('self.config["show_dps"] = self.show_dps', source)
        self.assertIn('self.config["layout_version"] = 15', source)
        self.assertIn("class ModernSlider(tk.Canvas):", source)
        self.assertIn('text="主窗口透明度"', source)
        self.assertNotIn('text="团队 DPS"', source)
        self.assertIn('actions, "compact", "迷你模式"', source)
        self.assertIn('actions, "menu", "主菜单", self.show_main_menu', source)
        self.assertIn('def show_main_menu(self)', source)
        self.assertIn("MAIN_MIN_WIDTH = 430", source)
        self.assertIn("MINI_DEFAULT_WIDTH = 340", source)
        self.assertIn("MINI_MIN_WIDTH = 188", source)
        self.assertIn("MINI_DEFAULT_HEIGHT = 118", source)
        self.assertIn('tags=("compact_restore", "compact_restore_bg")', source)
        self.assertIn('text="当前身份"', source)
        self.assertIn(
            "membership_label_for_card_tier(self.licensing.session.card_tier)",
            source,
        )
        self.assertNotIn('identity_icon = self.icons.toolbar("user"', source)
        self.assertIn('self.root.bind("<MouseWheel>", self._scroll_main, add="+")', source)
        self.assertIn("self._dismiss_compact_auxiliary_windows()", source)
        nav_source = source[
            source.index("    def _backend_nav_button(") : source.index(
                "    def _sync_backend_navigation("
            )
        ]
        self.assertIn("caption_label = tk.Label(", nav_source)
        self.assertIn("width=2", nav_source)
        self.assertNotIn('text=f"{icon_text}   {caption}"', nav_source)
        checkbox_source = source[
            source.index("    def _settings_check_row(") : source.index(
                "    def _select_settings_section("
            )
        ]
        self.assertIn("checkbox = tk.Canvas(", checkbox_source)
        self.assertIn("width=22", checkbox_source)
        self.assertIn("height=22", checkbox_source)
        self.assertNotIn("tk.Checkbutton(", checkbox_source)
        self.assertIn("selected_kind=detail_kind", source)

    def test_window_alpha_clamps_and_accepts_percent_or_fraction(self):
        class Root:
            def __init__(self):
                self.alpha = 1.0

            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def update_idletasks():
                return None

            @staticmethod
            def configure(**_kwargs):
                return None

            def attributes(self, name, *values):
                self.assert_alpha_name(name)
                if values:
                    self.alpha = float(values[0])
                return self.alpha

            @staticmethod
            def assert_alpha_name(name):
                if name != "-alpha":
                    raise AssertionError(name)

        window = object.__new__(DpsWindow)
        window.root = Root()
        window.history_window = type(
            "AuxiliaryWindow", (), {"alpha_changes": []}
        )()
        window.closing = False
        window.main_content_overlay_supported = False
        window.window_alpha = 1.0
        window.opacity_value_label = None
        window.settings_opacity_var = None
        window.config = {}

        window._set_window_alpha(40, persist=False)
        self.assertEqual(window.window_alpha, 0.55)
        self.assertEqual(window.root.alpha, 0.55)
        window._set_window_alpha(82, persist=False)
        self.assertEqual(window.window_alpha, 0.82)
        window._set_window_alpha(0.73, persist=False)
        self.assertEqual(window.window_alpha, 0.73)
        self.assertEqual(window.config["alpha"], 0.73)
        self.assertEqual(window.history_window.alpha_changes, [])

    def test_compact_width_tracks_the_number_of_visible_metrics(self):
        self.assertEqual(
            [compact_width_for_visible_metrics(count) for count in range(5)],
            [188, 220, 260, 300, 340],
        )
        window = object.__new__(DpsWindow)
        window.show_total_damage = False
        window.show_dps = True
        window.show_damage_share = False
        window.show_critical_rate = False
        self.assertEqual(window._compact_target_width(), 220)
        window.show_total_damage = True
        window.show_damage_share = True
        window.show_critical_rate = True
        self.assertEqual(window._compact_target_width(), 340)

    def test_boss_hp_uses_one_continuous_fill_without_a_name_block(self):
        class Canvas:
            def __init__(self):
                self.rectangles = []
                self.texts = []

            @staticmethod
            def delete(*_args):
                return None

            @staticmethod
            def winfo_width():
                return 500

            @staticmethod
            def winfo_height():
                return 36

            def create_rectangle(self, *args, **kwargs):
                self.rectangles.append((args, kwargs))

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

        monster = type(
            "Monster",
            (),
            {
                "name": "测试 Boss",
                "level": 60,
                "current_hp": 50,
                "max_hp": 100,
                "observed_max_hp": 100,
            },
        )()
        window = object.__new__(DpsWindow)
        window.monster_hp_canvas = Canvas()
        window.model = type(
            "Model", (), {"current_monster": lambda _self: monster}
        )()
        window._fit_main_actor_name = lambda value, _width: value
        window._ui_font = lambda _role: None

        window._draw_monster_hp()

        self.assertEqual(len(window.monster_hp_canvas.rectangles), 2)
        track, fill = window.monster_hp_canvas.rectangles
        self.assertEqual(track[0][:2], (1, 3))
        self.assertEqual(fill[0][:2], (2, 4))
        self.assertAlmostEqual(fill[0][2], 248, delta=1)
        self.assertEqual(len(window.monster_hp_canvas.texts), 1)
        text_args, text_options = window.monster_hp_canvas.texts[0]
        self.assertEqual(text_args[:2], (250, 18))
        self.assertEqual(
            text_options["text"], "Lv.60   测试 Boss   50 / 100   50.0%"
        )

    def test_main_columns_include_configurable_dps_in_expected_order(self):
        window = object.__new__(DpsWindow)
        window.show_total_damage = True
        window.show_dps = True
        window.show_damage_share = True
        window.show_critical_rate = True

        columns = window._main_columns(600)
        self.assertLess(columns["damage"], columns["dps"])
        self.assertLess(columns["dps"], columns["share"])
        self.assertLess(columns["share"], columns["critical"])

        window.show_dps = False
        without_dps = window._main_columns(600)
        self.assertNotIn("dps", without_dps)
        self.assertEqual(set(columns) - {"dps"}, set(without_dps))

    def test_history_columns_follow_main_dps_visibility_settings(self):
        window = object.__new__(DpsWindow)
        window.show_total_damage = True
        window.show_dps = True
        window.show_damage_share = True
        window.show_critical_rate = True

        columns = window._history_participant_columns(720)
        self.assertLess(columns["damage"], columns["dps"])
        self.assertLess(columns["dps"], columns["share"])
        self.assertLess(columns["share"], columns["critical"])

        window.show_total_damage = False
        window.show_damage_share = False
        window.show_critical_rate = False
        dps_only = window._history_participant_columns(720)
        self.assertIn("dps", dps_only)
        self.assertNotIn("damage", dps_only)
        self.assertNotIn("share", dps_only)
        self.assertNotIn("critical", dps_only)

    def test_transparency_overlay_does_not_duplicate_empty_damage_message(self):
        class Canvas:
            def __init__(self):
                self.texts = []

            @staticmethod
            def delete(*_args):
                return None

            @staticmethod
            def winfo_width():
                return 430

            @staticmethod
            def winfo_height():
                return 180

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

            @staticmethod
            def configure(**_kwargs):
                return None

            @staticmethod
            def yview_moveto(*_args):
                return None

        window = object.__new__(DpsWindow)
        window.model = type(
            "Model",
            (),
            {
                "current_stats": lambda _self: [],
                "duration": lambda _self: 0.0,
            },
        )()
        window.compact_mode = False
        window.main_scroll_offset = 0
        window.main_scroll_content_height = 0
        window._main_columns = lambda _width: {}
        window._main_row_height = lambda: 32
        window._ui_font = lambda _role: None
        main_canvas = Canvas()
        overlay_canvas = Canvas()

        window._draw_main_rows_on_canvas(
            main_canvas, update_scroll_state=True
        )
        window._draw_main_rows_on_canvas(
            overlay_canvas,
            update_scroll_state=False,
            draw_empty_state=False,
        )

        self.assertEqual(
            [options["text"] for _args, options in main_canvas.texts],
            ["暂无伤害记录"],
        )
        self.assertEqual(overlay_canvas.texts, [])

    def test_main_wheel_scrolls_normal_and_compact_dps_lists(self):
        class Canvas:
            def __init__(self):
                self.positions = []
                self.indicators = []

            @staticmethod
            def winfo_width():
                return 220

            @staticmethod
            def winfo_height():
                return 60

            @staticmethod
            def canvasy(_value):
                return 0.0

            @staticmethod
            def delete(*_args):
                return None

            def yview_moveto(self, position):
                self.positions.append(position)

            def create_rectangle(self, *args, **kwargs):
                self.indicators.append((args, kwargs))

        window = object.__new__(DpsWindow)
        window.rows_canvas = Canvas()
        window.window_locked = False
        window.ui_font_size = 14
        window.compact_mode = False
        window.main_scroll_offset = 0
        window.main_scroll_content_height = 192
        wheel_down = type("Wheel", (), {"delta": -120, "num": "??"})()
        wheel_up = type("Wheel", (), {"delta": 120, "num": "??"})()

        self.assertEqual(window._scroll_main(wheel_down), "break")
        self.assertEqual(window.main_scroll_offset, 32)
        self.assertAlmostEqual(window.rows_canvas.positions[-1], 32 / 192)
        window._scroll_main(wheel_up)
        self.assertEqual(window.main_scroll_offset, 0)

        window.compact_mode = True
        window.main_scroll_content_height = 112
        window._scroll_main(wheel_down)
        self.assertEqual(window.main_scroll_offset, 28)
        self.assertTrue(window.rows_canvas.indicators)

    def test_main_player_rows_are_compact_and_touch_without_a_gap(self):
        class Canvas:
            def __init__(self):
                self.rectangles = []
                self.options = {}

            @staticmethod
            def winfo_width():
                return 600

            @staticmethod
            def winfo_height():
                return 180

            @staticmethod
            def delete(*_args, **_kwargs):
                return None

            @staticmethod
            def yview():
                return (0.0, 1.0)

            @staticmethod
            def yview_moveto(*_args, **_kwargs):
                return None

            def create_rectangle(self, *args, **kwargs):
                self.rectangles.append((args, kwargs))

            @staticmethod
            def create_image(*_args, **_kwargs):
                return None

            @staticmethod
            def create_text(*_args, **_kwargs):
                return None

            def configure(self, **kwargs):
                self.options.update(kwargs)

        first = ActorStats(SELF_ID, damage=200, damage_hits=2, critical_hits=1)
        second = ActorStats(TEAMMATE_ID, damage=100)
        window = object.__new__(DpsWindow)
        window.rows_canvas = Canvas()
        window.main_content_overlay_rows = None
        window.ui_font_size = 14
        window.show_total_damage = True
        window.show_dps = True
        window.show_damage_share = True
        window.show_critical_rate = True
        window.model = type(
            "Model",
            (),
            {
                "current_stats": lambda _self: [first, second],
                "duration": lambda _self: 10.0,
                "actor_profession_id": lambda _self, _actor_id: 1_200_003,
            },
        )()
        window.icons = type(
            "Icons", (), {"profession": lambda _self, _class_id, _size: object()}
        )()
        window._profession_info = lambda _class_id: ("", "#5869c4")
        window._shown_actor_name = lambda actor_id: str(actor_id)
        window._fit_main_actor_name = lambda value, _maximum: value
        window._ui_font = lambda _role: None

        window._draw_main_rows()

        first_rect, second_rect = window.rows_canvas.rectangles
        self.assertEqual(first_rect[0][3], second_rect[0][1])
        self.assertEqual(second_rect[0][3] - second_rect[0][1], 32)
        self.assertEqual(window.rows_canvas.options["yscrollincrement"], 1)

    def test_hps_main_rows_show_effective_hps_overheal_and_response(self):
        class Canvas:
            def __init__(self):
                self.texts = []

            @staticmethod
            def winfo_width():
                return 620

            @staticmethod
            def winfo_height():
                return 180

            @staticmethod
            def delete(*_args, **_kwargs):
                return None

            @staticmethod
            def create_rectangle(*_args, **_kwargs):
                return None

            @staticmethod
            def create_image(*_args, **_kwargs):
                return None

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

            @staticmethod
            def configure(**_kwargs):
                return None

            @staticmethod
            def yview_moveto(*_args):
                return None

        window = object.__new__(DpsWindow)
        window.main_meter_mode = "hps"
        window.compact_mode = False
        window.ui_font_size = 14
        window.main_scroll_offset = 0
        window.main_scroll_content_height = 0
        window.latest_healing_summary = {
            "team_effective_healing": 900,
            "healers": [
                {
                    "actor_id": SELF_ID,
                    "profession_id": 1_200_002,
                    "effective_healing": 900,
                    "hps": 90,
                    "overheal_rate": 0.25,
                    "response": {"average_ms": 500},
                },
                {
                    "actor_id": TEAMMATE_ID,
                    "profession_id": 1_200_002,
                    "effective_healing": 0,
                    "observed_total_healing": 500,
                    "total_healing": 500,
                    "hps": 0,
                    "overheal_rate": 1.0,
                    "response": {"average_ms": None},
                },
            ],
        }
        window.model = type(
            "Model",
            (),
            {
                "duration": lambda _self: 10.0,
                "actor_profession_id": lambda _self, _actor: 1_200_002,
            },
        )()
        window.icons = type(
            "Icons", (), {"profession": lambda _self, _class_id, _size: object()}
        )()
        window._profession_info = lambda _class_id: ("", "#6fe3bd")
        window._shown_actor_name = lambda _actor_id: "治疗者"
        window._fit_main_actor_name = lambda value, _maximum: value
        window._ui_font = lambda _role: None
        canvas = Canvas()
        window.rows_canvas = canvas

        window._draw_main_rows_on_canvas(canvas, update_scroll_state=True)

        rendered = [options["text"] for _args, options in canvas.texts]
        self.assertIn("治疗者", rendered)
        self.assertIn("900", rendered)
        self.assertIn("90", rendered)
        self.assertIn("25.0%", rendered)
        self.assertIn("100.0%", rendered)
        self.assertIn("500ms", rendered)
        self.assertEqual(format_response_time(1250), "1.25s")
        self.assertIn("明细为已观测部分", healing_coverage_label(
            "server_effective_with_partial_callbacks"
        ))

    def test_history_participant_name_has_profession_icon(self):
        class Canvas:
            def __init__(self):
                self.images = []
                self.texts = []

            @staticmethod
            def winfo_width():
                return 640

            @staticmethod
            def winfo_height():
                return 220

            @staticmethod
            def delete(*_args, **_kwargs):
                return None

            @staticmethod
            def create_rectangle(*_args, **_kwargs):
                return None

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

            def create_image(self, *args, **kwargs):
                self.images.append((args, kwargs))

            @staticmethod
            def tag_bind(*_args, **_kwargs):
                return None

            @staticmethod
            def configure(*_args, **_kwargs):
                return None

        class Icons:
            def __init__(self):
                self.calls = []

            def profession(self, class_id, size):
                self.calls.append((class_id, size))
                return f"profession:{class_id}:{size}"

        participant = {
            "actor_id": SELF_ID,
            "name": "莫雪",
            "profession_id": 1_200_003,
            "damage": 123_456,
            "dps": 12_345,
            "share": 1.0,
        }
        window = object.__new__(DpsWindow)
        window.history_participant_canvas = Canvas()
        window.history_selected_actor = SELF_ID
        window.hide_names = False
        window.show_total_damage = False
        window.show_dps = True
        window.show_damage_share = False
        window.show_critical_rate = False
        window.icons = Icons()
        window._selected_history_record = lambda: {
            "duration_seconds": 10,
            "participants": [participant],
        }
        window._profession_info = lambda _class_id: ("", "#4fd1c5")
        window._ui_font = lambda _kind: ("Microsoft YaHei UI", 9)
        window._fit_main_actor_name = lambda value, _maximum: value

        window._draw_history_participants()

        self.assertEqual(window.icons.calls, [(1_200_003, 20)])
        self.assertEqual(len(window.history_participant_canvas.images), 1)
        image_args, image_options = window.history_participant_canvas.images[0]
        self.assertEqual(image_args, (34, 7))
        self.assertEqual(image_options["image"], "profession:1200003:20")
        self.assertEqual(image_options["tags"], ("history-actor:0",))
        rendered_text = [
            options.get("text")
            for _args, options in window.history_participant_canvas.texts
        ]
        self.assertIn("12,345", rendered_text)
        self.assertNotIn("123,456", rendered_text)

    def test_empty_history_hps_mode_resets_captions_and_detail(self):
        class Label:
            def __init__(self):
                self.values = {}

            def configure(self, **values):
                self.values.update(values)

        window = object.__new__(DpsWindow)
        window.history_meter_mode = "hps"
        window.history_selected_actor = SELF_ID
        window.history_target_label = Label()
        window.history_time_label = Label()
        window.history_total_value = Label()
        window.history_total_value._caption_label = Label()
        window.history_dps_value = Label()
        window.history_dps_value._caption_label = Label()
        window.history_team_value = Label()
        window.history_favorite_button = Label()
        window.history_detail_label = Label()
        window.history_detail_mode = "skills"
        window._selected_history_record = lambda: None
        window._set_history_detail_mode = lambda _mode: None
        window._draw_history_list = lambda: None
        window._draw_history_participant_header = lambda: None
        window._draw_history_participants = lambda: None
        window._draw_history_skills = lambda: None

        window._render_history_selection()

        self.assertEqual(window.history_selected_actor, 0)
        self.assertEqual(window.history_target_label.values["text"], "暂无战斗记录")
        self.assertEqual(
            window.history_total_value._caption_label.values["text"],
            "总有效治疗",
        )
        self.assertEqual(
            window.history_dps_value._caption_label.values["text"], "团队 HPS"
        )
        self.assertEqual(window.history_detail_label.values["text"], "暂无治疗详情")

    def test_history_share_uses_two_decimal_w_dps_and_expands_collisions(self):
        record = {
            "duration_seconds": 10,
            "participants": [
                {"name": "莫雪", "damage": 123_450, "dps": 12_345},
                {"name": "队友甲乙丙", "damage": 134_000, "dps": 13_400},
            ],
        }
        self.assertEqual(
            DpsWindow._history_share_text(record),
            "莫雪:1.23w 队友甲乙:1.34w",
        )
        crowded = {
            "duration_seconds": 1,
            "participants": [
                {"name": f"很长的角色名称{index}", "damage": 10_000 - index}
                for index in range(20)
            ],
        }
        shared = DpsWindow._history_share_text(crowded)
        self.assertLessEqual(len(shared), 110)
        self.assertFalse(shared.endswith(" "))
        self.assertTrue(all(":" in part for part in shared.split(" ")))
        self.assertEqual(
            DpsWindow._history_share_text(record, hide_names=True),
            "玩家1:1.23w 玩家2:1.34w",
        )
        collision = {
            "duration_seconds": 10,
            "participants": [
                {"name": "甲", "dps": 12_340},
                {"name": "乙", "dps": 12_349},
                {"name": "丙", "dps": 13_400},
            ],
        }
        self.assertEqual(
            DpsWindow._history_share_text(collision),
            "甲:1.234w 乙:1.235w 丙:1.34w",
        )
        exact_tie = {
            "participants": [
                {"name": "甲", "dps": 12_345},
                {"name": "乙", "dps": 12_345},
            ]
        }
        self.assertEqual(
            DpsWindow._history_share_text(exact_tie),
            "甲:1.23w 乙:1.23w",
        )

    def test_share_uses_inline_status_instead_of_dialog(self):
        clipboard_calls = []
        notices = []

        class ClipboardRoot:
            def clipboard_clear(self):
                clipboard_calls.append(("clear", ""))

            def clipboard_append(self, value):
                clipboard_calls.append(("append", value))

            def update_idletasks(self):
                clipboard_calls.append(("update", ""))

        window = object.__new__(DpsWindow)
        window.root = ClipboardRoot()
        window._show_notice = lambda *args, **kwargs: notices.append((args, kwargs))

        window._copy_share_text("莫雪:12500", None)

        self.assertEqual(
            clipboard_calls,
            [("clear", ""), ("append", "莫雪:12500"), ("update", "")],
        )
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0][0][1], "已复制到剪贴板。")

        window._copy_share_text("", None)
        self.assertEqual(len(notices), 2)
        self.assertEqual(notices[1][0][1], "当前没有可分享的 DPS。")

    def test_message_drain_yields_to_tk_while_capture_queue_has_backlog(self):
        scheduled = []

        class ScheduledRoot:
            def after(self, delay, callback):
                scheduled.append((delay, callback))

        window = object.__new__(DpsWindow)
        window.closing = False
        window.root = ScheduledRoot()
        window.messages = MODULE["queue"].Queue()
        ingested = []
        window._ingest_combat_event = ingested.append
        batch_size = MODULE["MESSAGE_DRAIN_BATCH_SIZE"]
        for index in range(batch_size + 3):
            window.messages.put(("event", {"index": index}))

        window._drain_messages()

        self.assertEqual(len(ingested), batch_size)
        self.assertEqual(window.messages.qsize(), 3)
        self.assertEqual(scheduled[0][0], 1)

        window._drain_messages()
        self.assertEqual(len(ingested), batch_size + 3)
        self.assertTrue(window.messages.empty())
        self.assertEqual(scheduled[1][0], 50)

    def test_message_drain_stress_keeps_every_message_and_honors_time_budget(self):
        scheduled = []
        ingested = []
        batch_sizes = []
        last_ingested = [0]

        class ScheduledRoot:
            def after(self, delay, callback):
                batch_sizes.append(len(ingested) - last_ingested[0])
                last_ingested[0] = len(ingested)
                scheduled.append((delay, callback))

        window = object.__new__(DpsWindow)
        window.closing = False
        window.root = ScheduledRoot()
        window.messages = MODULE["queue"].Queue()
        window._ingest_combat_event = lambda payload: ingested.append(
            payload["index"]
        )
        message_count = 20_000
        for index in range(message_count):
            window.messages.put(("event", {"index": index}))

        clock = [0.0]
        original_counter = MODULE["time"].perf_counter

        def fake_counter():
            clock[0] += 0.005
            return clock[0]

        MODULE["time"].perf_counter = fake_counter
        try:
            window._drain_messages()
            callback_index = 0
            while not window.messages.empty():
                _delay, callback = scheduled[callback_index]
                callback_index += 1
                callback()
        finally:
            MODULE["time"].perf_counter = original_counter

        self.assertEqual(ingested, list(range(message_count)))
        self.assertTrue(window.messages.empty())
        self.assertLessEqual(max(batch_sizes), 3)
        self.assertTrue(all(delay == 1 for delay, _callback in scheduled[:-1]))
        self.assertEqual(scheduled[-1][0], 50)

    def test_update_control_result_bypasses_combat_queue_backlog(self):
        scheduled = []
        handled = []

        class ScheduledRoot:
            def after(self, delay, callback):
                scheduled.append((delay, callback))

        window = object.__new__(DpsWindow)
        window.closing = False
        window.root = ScheduledRoot()
        window.messages = MODULE["queue"].Queue()
        window.control_messages = MODULE["queue"].Queue()
        window._ingest_combat_event = lambda payload: handled.append(
            ("event", payload["index"])
        )
        window._handle_update_check_result = lambda payload: handled.append(
            ("update", payload["status"])
        )
        for index in range(20_000):
            window.messages.put(("event", {"index": index}))
        window.control_messages.put(
            ("update_check_result", {"status": "complete"})
        )

        window._drain_messages()

        self.assertEqual(handled[0], ("update", "complete"))
        self.assertGreater(window.messages.qsize(), 0)
        self.assertEqual(scheduled[0][0], 1)

    def test_close_drains_capture_backlog_in_bounded_batches(self):
        scheduled = []
        destroyed = []
        ingested = []

        class ScheduledRoot:
            def after(self, delay, callback):
                scheduled.append((delay, callback))

            def destroy(self):
                destroyed.append(True)

        class StoppedWorker:
            @staticmethod
            def is_alive():
                return False

        class Model:
            @staticmethod
            def ingest(payload):
                ingested.append(payload["index"])

            @staticmethod
            def archive_current(_reason):
                return True

            def __getattr__(self, _name):
                return lambda _payload: None

        window = object.__new__(DpsWindow)
        window.root = ScheduledRoot()
        window.worker = StoppedWorker()
        window.heartbeat_worker = None
        window.close_started_at = time.monotonic()
        window.close_finalized = False
        window.messages = MODULE["queue"].Queue()
        window.model = Model()
        window._ingest_combat_event = window.model.ingest
        window._ingest_stage_summary = lambda _payload: None
        window._flush_combat_history = lambda **_kwargs: None
        window._save_preferences = lambda: None
        for index in range(MODULE["MESSAGE_DRAIN_BATCH_SIZE"] + 5):
            window.messages.put(("event", {"index": index}))

        window._finish_close()

        self.assertFalse(destroyed)
        self.assertEqual(len(ingested), MODULE["MESSAGE_DRAIN_BATCH_SIZE"])
        self.assertEqual(scheduled[0][0], 1)
        scheduled.pop(0)[1]()
        self.assertTrue(destroyed)
        self.assertEqual(
            ingested,
            list(range(MODULE["MESSAGE_DRAIN_BATCH_SIZE"] + 5)),
        )

    def test_update_check_unexpected_error_always_posts_terminal_result(self):
        class FailingLicensing:
            @staticmethod
            def check_update():
                raise ValueError("unexpected")

        window = object.__new__(DpsWindow)
        window.closing = False
        window.update_check_started = False
        window.update_check_in_progress = False
        window.update_button = None
        window.licensing = FailingLicensing()
        window.control_messages = MODULE["queue"].Queue()

        window._start_update_check(manual=True)
        kind, payload = window.control_messages.get(timeout=1.0)

        self.assertEqual(kind, "update_check_result")
        self.assertTrue(payload["manual"])
        self.assertIsNone(payload["update"])
        self.assertIn("失败", payload["error"])

    def test_update_download_unexpected_error_always_posts_terminal_result(self):
        class FailingLicensing:
            @staticmethod
            def download_update(_update, _destination, _progress):
                raise ValueError("unexpected")

        window = object.__new__(DpsWindow)
        window.pending_update = UpdateInfo(
            available=True,
            latest_version="0.0.12",
            download_path="/api/v1/dps/update/download",
            sha256="a" * 64,
            size=1024,
            filename="dps.exe",
        )
        window.update_downloading = False
        window.closing = False
        window.update_status_label = None
        window.update_action_button = None
        window.licensing = FailingLicensing()
        window.control_messages = MODULE["queue"].Queue()

        window._download_pending_update()
        kind, payload = window.control_messages.get(timeout=1.0)

        self.assertEqual(kind, "update_download_failed")
        self.assertIn("失败", payload)
        window._handle_update_download_failed(payload)
        self.assertFalse(window.update_downloading)

    def test_feedback_can_select_exactly_one_combat_record(self):
        records = [
            {
                "encounter_id": "encounter-new",
                "ended_at_epoch": 1_787_990_000,
                "total_damage": 5_500_000,
                "team_size": 2,
                "monster": {"name": "星象仪者"},
                "targets": [{"name": "星象仪者", "kind": "Boss"}],
                "participants": [
                    {
                        "actor_id": SELF_ID,
                        "name": "莫雪",
                        "profession_id": 1_200_002,
                        "damage": 3_000_000,
                        "critical_rate": 0.4,
                        "deaths": 1,
                        "skills": [
                            {
                                "skill_id": 86_021_070,
                                "name": "测试技能",
                                "damage": 3_000_000,
                                "hits": 20,
                            }
                        ],
                        "targets": [
                            {
                                "entity_id": MONSTER_ID,
                                "name": "星象仪者",
                                "kind": "Boss",
                                "damage": 2_000_000,
                            },
                            {
                                "entity_id": SECOND_MONSTER_ID,
                                "name": "星光守卫",
                                "kind": "小怪",
                                "damage": 1_000_000,
                            },
                        ],
                    }
                ],
            },
            {
                "encounter_id": "encounter-old",
                "ended_at_epoch": 1_787_980_000,
                "total_damage": 1_000_000,
                "monster": {"name": "小丑"},
                "participants": [],
            },
        ]

        choices = DpsWindow._feedback_record_choices(records, True)
        self.assertEqual(choices[0], ("当前战斗（进行中）", "__current__"))
        self.assertEqual([value for _label, value in choices[1:]], [
            "encounter-new",
            "encounter-old",
        ])
        self.assertIn("星象仪者", choices[1][0])

        selected = DpsWindow._feedback_history_summary(records[0])
        self.assertEqual(selected["encounter_id"], "encounter-new")
        self.assertEqual(len(selected["participants"]), 1)
        self.assertEqual(selected["participants"][0]["skills"][0]["damage"], 3_000_000)
        self.assertEqual(
            [target["name"] for target in selected["participants"][0]["targets"]],
            ["星象仪者", "星光守卫"],
        )

        complete_record = dict(records[0])
        complete_record["participants"] = [
            {
                **records[0]["participants"][0],
                "skills": [
                    {"skill_id": index + 1, "damage": 100 + index}
                    for index in range(16)
                ],
                "targets": [
                    {"entity_id": index + 1, "damage": 200 + index}
                    for index in range(14)
                ],
            }
        ]
        complete_record["damage_accounting"] = {"packet_event_count": 99}
        complete = DpsWindow._feedback_history_summary(complete_record)
        self.assertEqual(len(complete["participants"][0]["skills"]), 16)
        self.assertEqual(len(complete["participants"][0]["targets"]), 14)
        self.assertEqual(complete["damage_accounting"]["packet_event_count"], 99)

        older_records = [
            {
                "encounter_id": f"encounter-{index}",
                "ended_at_epoch": 1_787_990_000 - index,
                "total_damage": index,
                "monster": {"name": f"Boss {index}"},
            }
            for index in range(25)
        ]
        older_choices = DpsWindow._feedback_record_choices(
            older_records,
            False,
            "encounter-24",
        )
        self.assertEqual(len(older_choices), 21)
        self.assertIn("encounter-24", dict(older_choices).values())

    def test_each_history_row_has_direct_feedback_action(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertIn('feedback_tag = f"history-feedback:{index}"', source)
        self.assertIn("self._feedback_history_record(", source)
        self.assertIn("self.show_feedback(encounter_id)", source)

    def test_feedback_diagnostics_match_damage_target_to_boss_catalog(self):
        worker = HookWorker(MODULE["queue"].Queue(), MODULE["threading"].Event())
        worker._record_native_boss_observation(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_103_401,
                "boss_type": 0,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        worker._record_native_damage_observation(
            {
                "target_id": MONSTER_ID,
                "damage": 123_456,
                "filetime_100ns": BASE_FILETIME + 10_000,
            }
        )
        snapshot = worker.diagnostic_snapshot()
        target = snapshot["recent_damage_targets"][-1]
        self.assertEqual(target["template_id"], 7_103_401)
        self.assertEqual(target["name"], "伯德温·威瑟尔")
        self.assertTrue(target["catalog_match"])
        self.assertGreaterEqual(snapshot["boss_catalog_size"], 1)

    def test_active_boss_cache_is_pid_scoped_and_cleared_with_parser_state(self):
        worker_globals = HookWorker._set_active_boss_cache.__globals__
        original_path = worker_globals["ACTIVE_BOSS_CACHE_PATH"]
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "active_boss.json"
            worker_globals["ACTIVE_BOSS_CACHE_PATH"] = cache_path
            try:
                messages = MODULE["queue"].Queue()
                worker = HookWorker(
                    messages, MODULE["threading"].Event()
                )
                game_pid = 43210
                template_id = 7_102_403
                cached_state = {
                    "game_pid": game_pid,
                    "entity_id": MONSTER_ID,
                    "template_id": template_id,
                    "name": "\u661f\u8c61\u4eea\u8005",
                    "level": 61,
                    "max_hp": 9_876_543.0,
                    "filetime_100ns": BASE_FILETIME,
                }
                worker._set_active_boss_cache(cached_state)
                parser = NetworkPacketParser(
                    boss_template_catalog=worker.monster_catalog,
                    boss_name_allowlist=MODULE["load_boss_name_allowlist"](),
                )

                self.assertTrue(
                    worker._restore_same_process_boss(parser, game_pid)
                )
                self.assertNotIn(MONSTER_ID, parser.entity_current_hp)
                worker._sync_active_boss_cache(parser, game_pid)
                saved = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["game_pid"], game_pid)
                self.assertEqual(saved["max_hp"], 9_876_543.0)
                self.assertNotIn("current_hp", saved)

                while not messages.empty():
                    messages.get_nowait()
                parser.active_boss_entity_id = SECOND_MONSTER_ID
                parser.active_boss_time_100ns = BASE_FILETIME + 10_000
                parser.entity_template_ids[SECOND_MONSTER_ID] = template_id
                parser.entity_profiles[SECOND_MONSTER_ID] = {
                    "name": "Replacement Boss",
                    "level": 61,
                }
                parser.entity_max_hp[SECOND_MONSTER_ID] = 8_765_432.0
                worker._sync_active_boss_cache(parser, game_pid)
                kind, payload = messages.get_nowait()
                self.assertEqual(kind, "active_boss")
                self.assertEqual(payload["entity_id"], SECOND_MONSTER_ID)

                parser.active_boss_entity_id = None
                worker._sync_active_boss_cache(parser, game_pid)
                self.assertEqual(
                    json.loads(cache_path.read_text(encoding="utf-8")), {}
                )

                worker._set_active_boss_cache(saved)
                self.assertFalse(
                    worker._restore_same_process_boss(parser, game_pid + 1)
                )
                self.assertEqual(worker.active_boss_cache, {})
            finally:
                worker_globals["ACTIVE_BOSS_CACHE_PATH"] = original_path

    def test_history_only_displays_packet_confirmed_bosses(self):
        self.assertFalse(
            DpsWindow._history_is_boss(
                {"monster": {"boss_rank": 1, "max_hp": 20_000_000}}
            )
        )
        self.assertTrue(
            DpsWindow._history_is_boss(
                {"monster": {"boss_rank": 3, "entity_type": "Boss"}}
            )
        )

    def test_only_confirmed_player_damage_to_the_boss_enters_dps(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [SELF_ID, TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 200))
        model.ingest(damage(3, NEARBY_ID, MONSTER_ID, 999))
        model.ingest(damage(4, MONSTER_ID, SELF_ID, 500))
        self.assertEqual(set(model.stats), {SELF_ID, TEAMMATE_ID, NEARBY_ID})
        self.assertEqual(model.stats[SELF_ID].damage, 100)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200)
        self.assertEqual(model.stats[NEARBY_ID].damage, 999)
        self.assertIn(NEARBY_ID, model.friendly_ids)
        self.assertNotIn(MONSTER_ID, model.stats)

    def test_hp_panel_only_selects_a_damage_target(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": []})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {"entity_id": SELF_ID, "current_hp": 8_536, "max_hp": 8_536}
        )
        model.ingest_monster(
            {"entity_id": MONSTER_ID, "current_hp": 478_353, "max_hp": 563_096}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID))
        selected = model.current_monster()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.entity_id, MONSTER_ID)
        self.assertNotEqual(selected.entity_id, SELF_ID)

    def test_boss_metadata_delivered_as_monster_update_recomputes_damage(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": []})
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 125_000))
        self.assertFalse(model.stats)

        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "name": "洛克·金",
                "entity_type": "Monster",
                "boss_type": 3,
                "boss_rank": 3,
                "filetime_100ns": BASE_FILETIME + 20_000,
            }
        )

        self.assertEqual(model.current_monster().entity_id, MONSTER_ID)
        self.assertEqual(model.current_monster().name, "洛克·金")
        self.assertEqual(model.stats[SELF_ID].damage, 125_000)

    def test_manual_clear_keeps_boss_profile_for_followup_basic_attacks(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": []})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "公会伤害木桩",
                "entity_type": "Monster",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 500))
        self.assertEqual(model.stats[SELF_ID].damage, 500)

        model.reset(
            keep_identity=True,
            keep_monsters=True,
            archive_reason="manual_reset",
        )
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 300))

        self.assertEqual(model.stats[SELF_ID].damage, 300)
        self.assertEqual(model.current_monster().name, "公会伤害木桩")

    def test_manual_clear_requires_new_damage_before_team_updates_restart(self):
        """A stale cumulative packet must not restart a manually cleared pull."""
        model = CombatModel(run_id="manual-clear-team-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        self.assertTrue(model.ingest_team_stat(team_stat(2, TEAMMATE_ID, 250_000)))

        model.reset(
            keep_identity=True,
            keep_monsters=True,
            preserve_active_target=True,
            archive_reason="manual_reset",
        )

        self.assertEqual(model.combat_target_id, MONSTER_ID)
        self.assertFalse(model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 400_000)))
        self.assertEqual(model.stats, {})

        model.ingest(damage(4, SELF_ID, MONSTER_ID, 50_000))
        self.assertTrue(model.ingest_team_stat(team_stat(5, TEAMMATE_ID, 500_000)))
        self.assertEqual(set(model.stats), {SELF_ID, TEAMMATE_ID})
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 100_000)

    def test_manual_clear_after_boss_death_accepts_same_boss_again(self):
        model = CombatModel(run_id="manual-dead-boss-reset")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "异化猎犬",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 100_000,
                "max_hp": 100_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 20_000,
            }
        )
        model.reset(
            keep_identity=True,
            keep_monsters=True,
            archive_reason="manual_reset",
        )
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 12_345))

        self.assertEqual(model.current_monster().name, "异化猎犬")
        self.assertEqual(model.stats[SELF_ID].damage, 12_345)

    def test_network_id_is_not_used_as_display_name(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        self.assertEqual(model.display_name(SELF_ID), "")

    def test_first_observed_monster_hp_is_progress_baseline(self):
        model = CombatModel()
        model.ingest_monster({"entity_id": MONSTER_ID, "current_hp": 125_000_000})
        model.ingest_monster({"entity_id": MONSTER_ID, "current_hp": 100_000_000})
        monster = model.monsters[MONSTER_ID]
        self.assertEqual(monster.current_hp, 100_000_000)
        self.assertEqual(monster.observed_max_hp, 125_000_000)

    def test_small_monsters_do_not_enter_dps_or_target_panel(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": []})
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 120_110,
                "max_hp": 120_110,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 50_000))
        self.assertEqual(model.stats, {})
        self.assertIsNone(model.current_monster())

    def test_small_monster_damage_stress_is_discarded_before_history(self):
        model = CombatModel(run_id="small-monster-stress-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Monster",
                "template_id": 7_000_001,
                "boss_type": 1,
            }
        )

        started = time.perf_counter()
        for sequence in range(20_000):
            model.ingest(damage(sequence, SELF_ID, MONSTER_ID, 100))
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual(model.events, [])
        self.assertEqual(len(model.pending_target_events), 0)
        self.assertEqual(model.stats, {})
        self.assertEqual(model.discarded_non_encounter_events, 20_000)

    def test_hound_late_flower_profiles_do_not_recompute_long_history(self):
        model = CombatModel(run_id="hound-flower-stress-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "异化猎犬",
                "entity_type": "Boss",
                "template_id": 7_109_801,
                "boss_rank": 3,
            }
        )
        for sequence in range(1, 20_001):
            model.ingest(damage(sequence, SELF_ID, MONSTER_ID, 100))

        flower_ids = [SECOND_MONSTER_ID + index for index in range(512)]
        started = time.perf_counter()
        for index, flower_id in enumerate(flower_ids, start=20_001):
            model.ingest(damage(index, SELF_ID, flower_id, 100))
            model.ingest_profile(
                {
                    "entity_id": flower_id,
                    "name": "花",
                    "entity_type": "Monster",
                    "template_id": 7_109_802,
                    "boss_type": 1,
                }
            )
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(model.encounter_add_target_ids), len(flower_ids))
        self.assertEqual(model.stats[SELF_ID].damage, 2_051_200)
        flower_rows = [
            row for row in model.actor_target_rows(SELF_ID) if row["name"] == "花"
        ]
        self.assertEqual(len(flower_rows), 1)
        self.assertEqual(flower_rows[0]["damage"], 51_200)
        self.assertEqual(set(flower_rows[0]["entity_ids"]), set(flower_ids))

    def test_ancestor_cavalry_remain_identified_under_damage_stress(self):
        model = CombatModel(run_id="ancestor-cavalry-stress-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "先祖铠甲",
                "entity_type": "Boss",
                "template_id": 7_103_402,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000))

        cavalry_ids = [SECOND_MONSTER_ID + index for index in range(512)]
        for cavalry_id in cavalry_ids:
            model.ingest_profile(
                {
                    "entity_id": cavalry_id,
                    "name": "骑兵",
                    "entity_type": "Monster",
                    "template_id": 7_103_403,
                    "boss_type": 2,
                }
            )

        started = time.perf_counter()
        for sequence in range(2, 20_002):
            model.ingest(
                damage(
                    sequence,
                    SELF_ID,
                    cavalry_ids[sequence % len(cavalry_ids)],
                    100,
                )
            )
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(model.encounter_add_target_ids), len(cavalry_ids))
        self.assertEqual(model.stats[SELF_ID].damage, 2_001_000)
        cavalry_rows = [
            row for row in model.actor_target_rows(SELF_ID) if row["name"] == "骑兵"
        ]
        self.assertEqual(len(cavalry_rows), 1)
        self.assertEqual(cavalry_rows[0]["damage"], 2_000_000)
        self.assertEqual(set(cavalry_rows[0]["entity_ids"]), set(cavalry_ids))

    def test_unknown_first_hits_replay_when_boss_identity_arrives(self):
        model = CombatModel(run_id="late-boss-identity-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for sequence in range(1, 6):
            model.ingest(damage(sequence, SELF_ID, MONSTER_ID, 1_000))

        self.assertEqual(model.events, [])
        self.assertEqual(len(model.pending_target_events), 5)

        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "template_id": 7_103_401,
                "boss_rank": 3,
                "filetime_100ns": BASE_FILETIME + 6 * 10_000,
            }
        )

        self.assertEqual(len(model.pending_target_events), 0)
        self.assertEqual(len(model.events), 5)
        self.assertEqual(model.stats[SELF_ID].damage, 5_000)

    def test_unknown_target_buffer_is_strictly_bounded(self):
        model = CombatModel(run_id="unknown-target-bound-test")
        model.ingest_identity({"entity_id": SELF_ID})

        for sequence in range(20_000):
            model.ingest(damage(sequence, SELF_ID, MONSTER_ID, 100))

        self.assertLessEqual(
            len(model.pending_target_events),
            MODULE["UNKNOWN_TARGET_EVENT_LIMIT_PER_TARGET"],
        )
        self.assertLessEqual(
            len(model.pending_target_events),
            MODULE["UNKNOWN_TARGET_EVENT_LIMIT_GLOBAL"],
        )
        self.assertEqual(model.events, [])
        self.assertEqual(model.stats, {})

    def test_all_monsters_mode_tracks_non_boss_damage_and_history(self):
        model = CombatModel(boss_only=False)
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": []})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "普通怪物",
                "entity_type": "Monster",
                "level": 12,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 120_110,
                "max_hp": 120_110,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 50_000))

        self.assertEqual(model.stats[SELF_ID].damage, 50_000)
        self.assertEqual(model.current_monster().entity_id, MONSTER_ID)
        record = model.build_combat_record()
        self.assertEqual(record["target_filter"], "all_monsters")
        self.assertEqual(record["monster"]["name"], "普通怪物")
        self.assertEqual(record["monster"]["boss_rank"], 0)

    def test_switching_target_filter_starts_a_clean_encounter(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "普通怪物",
                "entity_type": "Monster",
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1000,
                "max_hp": 1000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        self.assertFalse(model.stats)

        self.assertTrue(model.set_boss_only(False))
        self.assertFalse(model.stats)
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 200))
        self.assertEqual(model.stats[SELF_ID].damage, 200)

        self.assertTrue(model.set_boss_only(True))
        self.assertFalse(model.stats)
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 300))
        self.assertFalse(model.stats)

    def test_all_monsters_mode_still_rejects_player_targets(self):
        model = CombatModel(boss_only=False)
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Player", "name": "玩家"}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        self.assertFalse(model.stats)
        self.assertIsNone(model.current_monster())

    def test_all_monsters_mode_starts_from_friendly_hit_before_profile(self):
        model = CombatModel(boss_only=False)
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 150))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 250))

        self.assertEqual(model.stats[SELF_ID].damage, 150)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250)
        self.assertEqual(model.current_monster().entity_id, MONSTER_ID)
        self.assertEqual(model.current_monster().entity_type, "Monster")

    def test_all_monsters_mode_aggregates_team_damage_across_targets(self):
        model = CombatModel(boss_only=False)
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        for entity_id in (MONSTER_ID, SECOND_MONSTER_ID):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "entity_type": "Monster",
                    "level": 62,
                }
            )
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": 500_000,
                    "max_hp": 500_000,
                    "filetime_100ns": BASE_FILETIME,
                }
            )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, TEAMMATE_ID, SECOND_MONSTER_ID, 250_000))

        self.assertEqual(set(model.stats), {SELF_ID, TEAMMATE_ID})
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)
        self.assertEqual(
            model.encounter_target_ids,
            {MONSTER_ID, SECOND_MONSTER_ID},
        )
        record = model.build_combat_record("test")
        self.assertEqual(record["team_size"], 2)
        self.assertEqual(record["total_damage"], 350_000)
        self.assertEqual(
            {target["entity_id"] for target in record["targets"]},
            {MONSTER_ID, SECOND_MONSTER_ID},
        )

    def test_all_monsters_mode_ends_the_pack_on_idle_not_first_death(self):
        model = CombatModel(boss_only=False, run_id="monster-pack-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        for entity_id in (MONSTER_ID, SECOND_MONSTER_ID, THIRD_MONSTER_ID):
            model.ingest_profile(
                {"entity_id": entity_id, "entity_type": "Monster"}
            )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            }
        )
        self.assertFalse(model.combat_end_time)

        model.ingest(damage(3, TEAMMATE_ID, SECOND_MONSTER_ID, 200_000))
        self.assertEqual(set(model.stats), {SELF_ID, TEAMMATE_ID})

        next_pack = damage(4, TEAMMATE_ID, THIRD_MONSTER_ID, 300_000)
        next_pack["filetime_100ns"] = BASE_FILETIME + 12 * 10_000_000
        model.ingest(next_pack)

        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_damage"], 300_000)
        self.assertEqual(set(model.stats), {TEAMMATE_ID})
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 300_000)

    def test_high_hp_and_elite_units_are_ignored_without_boss_marker(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": []})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Elite",
                "boss_rank": 2,
                "level": 80,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 20_000_000,
                "max_hp": 20_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 500_000))
        self.assertEqual(model.stats, {})
        self.assertIsNone(model.current_monster())

    def test_target_priority_is_boss_then_level(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": []})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Elite", "boss_rank": 2, "level": 80}
        )
        model.ingest_profile(
            {"entity_id": SECOND_MONSTER_ID, "entity_type": "Boss", "boss_rank": 3, "level": 60}
        )
        model.ingest_profile(
            {"entity_id": THIRD_MONSTER_ID, "entity_type": "Boss", "boss_rank": 3, "level": 70}
        )
        for index, entity_id in enumerate(
            (MONSTER_ID, SECOND_MONSTER_ID, THIRD_MONSTER_ID), start=1
        ):
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": 1_000_000,
                    "max_hp": 1_000_000,
                    "filetime_100ns": BASE_FILETIME + index * 10_000,
                }
            )
        selected = model.current_monster()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.entity_id, THIRD_MONSTER_ID)

    def test_team_absolute_damage_is_deduplicated_and_survives_leave(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {"entity_ids": [TEAMMATE_ID, ZERO_TEAMMATE_ID]}
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 1))
        model.ingest_team_stat(team_stat(2, SELF_ID, 100))
        model.ingest_team_stat(team_stat(2, TEAMMATE_ID, 200))
        model.ingest_team_stat(team_stat(2, ZERO_TEAMMATE_ID, 0))
        model.ingest_team_stat(team_stat(3, SELF_ID, 100))
        model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 200))
        self.assertEqual(model.stats[SELF_ID].damage, 100)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200)
        self.assertNotIn(ZERO_TEAMMATE_ID, model.stats)

        model.ingest(damage(4, SELF_ID, MONSTER_ID, 50))
        model.ingest_team_stat(team_stat(5, SELF_ID, 150))
        model.ingest_team_stat(team_stat(5, TEAMMATE_ID, 260))
        self.assertEqual(model.stats[SELF_ID].damage, 150)
        self.assertEqual(
            sum(skill.damage for skill in model.stats[SELF_ID].skills.values()),
            150,
        )
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 260)

        model.ingest_party({"entity_ids": []})
        self.assertEqual(model.party_ids, set())
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 260)
        self.assertEqual(
            {row.actor_id for row in model.current_stats()},
            {SELF_ID, TEAMMATE_ID},
        )
        model.ingest_party({"entity_ids": [NEARBY_ID], "member_count": 2})
        self.assertEqual(
            {row.actor_id for row in model.current_stats()},
            {SELF_ID, TEAMMATE_ID},
        )
        self.assertIn(TEAMMATE_ID, model.friend_order)

    def test_unchanged_team_snapshot_does_not_extend_damage_time(self):
        model = CombatModel(run_id="unchanged-team-time-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "异化猎犬",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        baseline = team_stat(1, TEAMMATE_ID, 0)
        baseline["full_snapshot"] = True
        self.assertFalse(model.ingest_team_stat(baseline))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(
            model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 200))
        )
        damage_time = model.last_damage_time
        duration = model.duration(damage_time + model.idle_gap + 1.0)

        duplicate = team_stat(60_000, TEAMMATE_ID, 200)
        self.assertFalse(model.ingest_team_stat(duplicate))
        self.assertEqual(model.last_damage_time, damage_time)
        self.assertEqual(
            model.duration(damage_time + model.idle_gap + 1.0), duration
        )
        self.assertFalse(model.active(damage_time + model.idle_gap + 1.0))

    def test_team_absolute_drop_starts_a_new_round_without_old_damage(self):
        model = CombatModel(run_id="team-absolute-drop-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )

        instance = 1_788_020_043
        self.assertFalse(
            model.ingest_team_stat(
                team_stat(1, TEAMMATE_ID, 50_000, server_time=instance)
            )
        )
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(3, TEAMMATE_ID, 350_000, server_time=instance)
            )
        )
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 300_000)

        # A wipe resets the game's absolute counter even when field 10 keeps
        # the same instance value. The lower snapshot is the new baseline.
        self.assertFalse(
            model.ingest_team_stat(
                team_stat(4, TEAMMATE_ID, 10_000, server_time=instance)
            )
        )
        self.assertEqual(model.stats, {})
        self.assertEqual(
            model.team_damage_states[TEAMMATE_ID].baseline_absolute,
            10_000,
        )

        model.ingest(damage(5, SELF_ID, MONSTER_ID, 200))
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(6, TEAMMATE_ID, 90_000, server_time=instance)
            )
        )
        self.assertEqual(model.stats[SELF_ID].damage, 200)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 80_000)

    def test_member_server_time_changes_do_not_split_continuous_team_damage(self):
        model = CombatModel(run_id="member-server-time-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID, NEARBY_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )

        self.assertFalse(
            model.ingest_team_stat(
                team_stat(1, TEAMMATE_ID, 20_000, server_time=100)
            )
        )
        self.assertFalse(
            model.ingest_team_stat(
                team_stat(2, NEARBY_ID, 30_000, server_time=200)
            )
        )
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(4, TEAMMATE_ID, 120_000, server_time=101)
            )
        )
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(5, NEARBY_ID, 230_000, server_time=205)
            )
        )
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 100_000)
        self.assertEqual(model.stats[NEARBY_ID].damage, 200_000)

        # Field 10 rolls independently for active members while their absolute
        # counters remain monotonic. Neither change is an encounter boundary.
        encounter_id = model.encounter_id
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(6, TEAMMATE_ID, 500_000, server_time=300)
            )
        )
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(7, NEARBY_ID, 550_000, server_time=205)
            )
        )
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.team_server_time, 300)
        self.assertEqual(model.team_damage_states[TEAMMATE_ID].server_time, 300)
        self.assertEqual(model.team_damage_states[NEARBY_ID].server_time, 205)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 480_000)
        self.assertEqual(model.stats[NEARBY_ID].damage, 520_000)
        self.assertEqual(
            sum(actor.damage for actor in model.stats.values()),
            1_000_100,
        )

    def test_omitted_common_zero_preserves_first_member_damage(self):
        model = CombatModel(run_id="omitted-common-zero-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        zero = team_stat(1, TEAMMATE_ID, 0, server_time=100)
        zero["omitted_zero"] = True
        self.assertFalse(model.ingest_team_stat(zero))

        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(3, TEAMMATE_ID, 250_000, server_time=101)
            )
        )
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)

        omitted_after_damage = team_stat(
            4, TEAMMATE_ID, 0, server_time=101
        )
        omitted_after_damage["omitted_zero"] = True
        self.assertFalse(model.ingest_team_stat(omitted_after_damage))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)
        self.assertEqual(model.pop_completed_combats(), [])

    def test_zero_baseline_survives_damage_before_delayed_boss_signal(self):
        model = CombatModel(run_id="pre-signal-common-damage-test")
        model.ingest_identity({"entity_id": SELF_ID})
        provisional_id = -12_723_086_689
        model.ingest_party({"entity_ids": [provisional_id]})
        opening = team_stat(1, provisional_id, 5_046, server_time=101)
        opening["full_snapshot"] = True
        self.assertFalse(
            model.ingest_team_stat(opening)
        )
        state = model.team_damage_states[provisional_id]
        self.assertEqual(state.baseline_absolute, 0)
        self.assertEqual(state.accepted_damage, 0)

        model.merge_actor(
            {
                "from_actor_id": provisional_id,
                "to_actor_id": TEAMMATE_ID,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(3, TEAMMATE_ID, 5_827, server_time=101)
            )
        )
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 5_827)

    def test_final_common_snapshot_updates_finished_pull_without_reopening(self):
        model = CombatModel(run_id="late-final-common-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 300,
                "max_hp": 300,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        instance = 1_788_020_043
        model.ingest_team_stat(
            team_stat(1, SELF_ID, 1_000_000, server_time=instance)
        )
        model.ingest_team_stat(
            team_stat(2, TEAMMATE_ID, 2_000_000, server_time=instance)
        )
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 100))
        model.ingest(damage(4, TEAMMATE_ID, MONSTER_ID, 200))
        model.ingest_team_stat(
            team_stat(5, SELF_ID, 1_000_080, server_time=instance)
        )
        model.ingest_team_stat(
            team_stat(6, TEAMMATE_ID, 2_000_150, server_time=instance)
        )

        for offset, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=1):
            model.ingest_combat_state(
                {
                    "entity_id": actor_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + 60_000 + offset,
                }
            )

        death_timestamp = BASE_FILETIME + 7 * 10_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 300,
                "death_confirmed": True,
                "filetime_100ns": death_timestamp,
            }
        )
        for offset, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=1):
            model.ingest_combat_state(
                {
                    "entity_id": actor_id,
                    "in_combat": False,
                    "filetime_100ns": death_timestamp + offset,
                }
            )
        ended_at = model.combat_end_time
        self.assertTrue(ended_at)
        self.assertEqual(sum(row.damage for row in model.stats.values()), 230)

        self.assertTrue(
            model.ingest_team_stat(
                team_stat(10, SELF_ID, 1_000_100, server_time=instance)
            )
        )
        self.assertTrue(
            model.ingest_team_stat(
                team_stat(11, TEAMMATE_ID, 2_000_200, server_time=instance)
            )
        )
        self.assertEqual(model.combat_end_time, ended_at)
        self.assertFalse(model.combat_in_progress(ended_at + 1.0))
        self.assertLessEqual(model.last_damage_time, ended_at)
        self.assertEqual(
            {actor_id: row.damage for actor_id, row in model.stats.items()},
            {SELF_ID: 100, TEAMMATE_ID: 200},
        )
        self.assertEqual(model.pop_completed_combats()[-1]["total_damage"], 300)

    def test_identical_common_snapshots_match_on_all_twelve_clients(self):
        actors = [SELF_ID + index for index in range(12)]
        baselines = {
            actor_id: (index + 1) * 1_000_000
            for index, actor_id in enumerate(actors)
        }
        expected = {
            actor_id: (index + 1) * 12_345
            for index, actor_id in enumerate(actors)
        }

        def client_result(self_index: int) -> dict[int, int]:
            model = CombatModel(run_id=f"common-client-{self_index}")
            self_actor = actors[self_index]
            model.ingest_identity({"entity_id": self_actor})
            model.ingest_party(
                {"entity_ids": [actor for actor in actors if actor != self_actor]}
            )
            model.ingest_profile(
                {
                    "entity_id": MONSTER_ID,
                    "entity_type": "Boss",
                    "boss_rank": 3,
                }
            )
            for sequence, actor_id in enumerate(actors, start=1):
                model.ingest_team_stat(
                    team_stat(
                        sequence,
                        actor_id,
                        baselines[actor_id],
                        server_time=1_788_020_043,
                    )
                )

            # Each machine can observe a different local callback amount. The
            # shared Common absolute values must replace that observer detail.
            model.ingest(
                damage(20, self_actor, MONSTER_ID, 7_777 + self_index * 9_001)
            )
            for sequence, actor_id in enumerate(actors, start=21):
                model.ingest_team_stat(
                    team_stat(
                        sequence,
                        actor_id,
                        baselines[actor_id] + expected[actor_id],
                        server_time=1_788_020_043,
                    )
                )
            return {
                actor_id: row.damage
                for actor_id, row in model.stats.items()
            }

        for self_index in range(12):
            with self.subTest(self_actor=actors[self_index]):
                self.assertEqual(client_result(self_index), expected)

    def test_boss_hp_drop_starts_team_stats_for_healer_without_local_hit(self):
        model = CombatModel(run_id="healer-shared-start-test")
        damage_actor_ids = (TEAMMATE_ID, NEARBY_ID)
        healer_id = SELF_ID
        model.ingest_identity({"entity_id": healer_id})
        model.ingest_party(
            {
                "entity_ids": list(damage_actor_ids),
                "member_count": 3,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )

        baselines = {
            healer_id: 200_000,
            TEAMMATE_ID: 300_000,
            NEARBY_ID: 400_000,
        }
        for sequence, actor_id in enumerate(baselines, start=1):
            model.ingest_team_stat(
                team_stat(
                    sequence,
                    actor_id,
                    baselines[actor_id],
                    server_time=1_788_020_043,
                )
            )

        shared_start = BASE_FILETIME + 1_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 999_900,
                "max_hp": 1_000_000,
                "filetime_100ns": shared_start,
            }
        )
        self.assertEqual(model.encounter_start_signal_100ns, shared_start)
        self.assertEqual(
            model.monsters[MONSTER_ID].last_hp_drop_100ns, shared_start
        )

        final_damage = {
            healer_id: 0,
            TEAMMATE_ID: 125_000,
            NEARBY_ID: 275_000,
        }
        for offset, actor_id in enumerate(final_damage, start=1):
            model.ingest_team_stat(
                {
                    "filetime_100ns": shared_start + 1_000_000 + offset,
                    "actor_id": actor_id,
                    "absolute_damage": (
                        baselines[actor_id] + final_damage[actor_id]
                    ),
                    "server_time": 1_788_020_043,
                }
            )

        self.assertEqual(
            {actor_id: row.damage for actor_id, row in model.stats.items()},
            {TEAMMATE_ID: 125_000, NEARBY_ID: 275_000},
        )
        self.assertNotIn(healer_id, model.stats)
        self.assertEqual(
            model.first_damage_time,
            model._event_seconds({"filetime_100ns": shared_start}),
        )

    def test_shared_boss_hp_start_matches_when_local_first_hits_differ(self):
        actors = [SELF_ID + index for index in range(12)]
        healer_id = actors[-1]
        baselines = {
            actor_id: (index + 1) * 500_000
            for index, actor_id in enumerate(actors)
        }
        expected = {
            actor_id: (index + 1) * 23_457
            for index, actor_id in enumerate(actors[:-1])
        }
        shared_start = BASE_FILETIME + 1_000_000
        expected_start = (
            shared_start - 116_444_736_000_000_000
        ) / 10_000_000

        def client_result(self_index: int) -> tuple[dict[int, int], float, int]:
            model = CombatModel(run_id=f"shared-start-client-{self_index}")
            self_actor = actors[self_index]
            model.ingest_identity({"entity_id": self_actor})
            model.ingest_party(
                {
                    "entity_ids": [
                        actor_id for actor_id in actors if actor_id != self_actor
                    ],
                    "member_count": 12,
                    "authoritative": True,
                }
            )
            model.ingest_profile(
                {
                    "entity_id": MONSTER_ID,
                    "entity_type": "Boss",
                    "boss_rank": 3,
                }
            )
            model.ingest_monster(
                {
                    "entity_id": MONSTER_ID,
                    "current_hp": 10_000_000,
                    "max_hp": 10_000_000,
                    "filetime_100ns": BASE_FILETIME,
                }
            )
            for sequence, actor_id in enumerate(actors, start=1):
                model.ingest_team_stat(
                    team_stat(
                        sequence,
                        actor_id,
                        baselines[actor_id],
                        server_time=1_788_020_043,
                    )
                )
            model.ingest_monster(
                {
                    "entity_id": MONSTER_ID,
                    "current_hp": 9_999_000,
                    "max_hp": 10_000_000,
                    "filetime_100ns": shared_start,
                }
            )

            if self_actor != healer_id:
                local_hit = damage(
                    1,
                    self_actor,
                    MONSTER_ID,
                    expected[self_actor],
                )
                local_hit["filetime_100ns"] = (
                    shared_start + (self_index + 1) * 100_000
                )
                model.ingest(local_hit)

            for offset, actor_id in enumerate(actors, start=1):
                model.ingest_team_stat(
                    {
                        "filetime_100ns": shared_start + 2_000_000 + offset,
                        "actor_id": actor_id,
                        "absolute_damage": (
                            baselines[actor_id] + expected.get(actor_id, 0)
                        ),
                        "server_time": 1_788_020_043,
                    }
                )
            return (
                {
                    actor_id: row.damage
                    for actor_id, row in model.stats.items()
                },
                model.first_damage_time,
                model.encounter_start_signal_100ns,
            )

        for self_index in range(12):
            with self.subTest(self_actor=actors[self_index]):
                damage_by_actor, first_damage_time, start_signal = client_result(
                    self_index
                )
                self.assertEqual(damage_by_actor, expected)
                self.assertEqual(first_damage_time, expected_start)
                self.assertEqual(start_signal, shared_start)

    def test_post_pull_counter_drop_reapplies_zero_baseline_to_team(self):
        model = CombatModel(run_id="team-zero-baseline-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        instance = 1_788_020_043
        model.ingest_team_stat(
            team_stat(1, SELF_ID, 900_000, server_time=instance)
        )
        model.ingest_team_stat(
            team_stat(2, TEAMMATE_ID, 800_000, server_time=instance)
        )

        shared_start = BASE_FILETIME + 1_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 999_000,
                "max_hp": 1_000_000,
                "filetime_100ns": shared_start,
            }
        )
        self.assertTrue(
            model.ingest_team_stat(
                {
                    "filetime_100ns": shared_start + 1,
                    "actor_id": SELF_ID,
                    "absolute_damage": 12_000,
                    "server_time": instance,
                }
            )
        )
        self.assertTrue(
            model.ingest_team_stat(
                {
                    "filetime_100ns": shared_start + 2,
                    "actor_id": TEAMMATE_ID,
                    "absolute_damage": 34_000,
                    "server_time": instance,
                }
            )
        )

        self.assertEqual(
            {actor_id: row.damage for actor_id, row in model.stats.items()},
            {SELF_ID: 12_000, TEAMMATE_ID: 34_000},
        )
        for state in model.team_damage_states.values():
            self.assertEqual(state.baseline_absolute, 0)
            self.assertEqual(
                state.baseline_snapshot_time_100ns, shared_start
            )

    def test_team_snapshot_never_rescales_exact_skill_or_target_amounts(self):
        model = CombatModel(run_id="realtime-calibration-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )

        # Establish the cumulative counter before the pull, then start combat.
        self.assertFalse(model.ingest_team_stat(team_stat(1, TEAMMATE_ID, 0)))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 10))

        first = damage(3, TEAMMATE_ID, MONSTER_ID, 120)
        first["provisional_damage"] = True
        second = damage(4, TEAMMATE_ID, MONSTER_ID, 80)
        second["provisional_damage"] = True
        model.ingest(first)
        model.ingest(second)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200)

        # The cumulative packet corrects only the covered interval.
        self.assertTrue(model.ingest_team_stat(team_stat(5, TEAMMATE_ID, 150)))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 150)

        after_snapshot = damage(6, TEAMMATE_ID, MONSTER_ID, 40)
        after_snapshot["provisional_damage"] = True
        model.ingest(after_snapshot)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 190)

        # A later cumulative packet can correct the live value downward, and
        # subsequent hits still appear immediately without ending the fight.
        self.assertTrue(model.ingest_team_stat(team_stat(7, TEAMMATE_ID, 180)))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 180)
        final_live_hit = damage(8, TEAMMATE_ID, MONSTER_ID, 20)
        final_live_hit["provisional_damage"] = True
        model.ingest(final_live_hit)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200)
        # The Common total is authoritative for the actor total, but it cannot
        # explain an observer stream that already contains 260. Exact skill and
        # target amounts remain untouched instead of being silently shrunk to
        # 200; the conflict stays visible for diagnostics.
        self.assertEqual(
            sum(model.stats[TEAMMATE_ID].target_damage.values()),
            260,
        )
        self.assertEqual(
            sum(skill.damage for skill in model.stats[TEAMMATE_ID].skills.values()),
            260,
        )
        self.assertTrue(model.combat_in_progress(model.last_damage_time))
        self.assertFalse(
            model.combat_in_progress(model.last_damage_time + model.idle_gap + 0.1)
        )

    def test_team_snapshot_puts_only_positive_unknown_remainder_in_unclassified(self):
        model = CombatModel(run_id="unclassified-skill-gap-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": TEAMMATE_ID, "name": "队友", "entity_type": "Player"}
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "测试首领",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )

        self.assertFalse(model.ingest_team_stat(team_stat(1, TEAMMATE_ID, 0)))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 10))
        first = damage(3, TEAMMATE_ID, MONSTER_ID, 30)
        first["skill_id"] = 86_021_010
        second = damage(4, TEAMMATE_ID, MONSTER_ID, 20)
        second["skill_id"] = 86_021_020
        model.ingest(first)
        model.ingest(second)

        self.assertTrue(model.ingest_team_stat(team_stat(5, TEAMMATE_ID, 200)))
        actor = model.stats[TEAMMATE_ID]
        self.assertEqual(actor.damage, 200)
        self.assertEqual(
            {skill_id: skill.damage for skill_id, skill in actor.skills.items()},
            {86_021_010: 30, 86_021_020: 20, 0: 150},
        )
        self.assertEqual(model.display_skill_name(TEAMMATE_ID, 0), "未归类伤害")
        self.assertEqual(actor.target_damage, {MONSTER_ID: 50})
        target_rows = model.actor_target_rows(TEAMMATE_ID)
        self.assertEqual(
            [(row["name"], row["damage"]) for row in target_rows],
            [("测试首领", 50), ("未分配目标", 150)],
        )
        record = model.build_combat_record()
        self.assertEqual(record["damage_accounting"]["skill_amount_policy"], "absolute_only")
        self.assertFalse(record["damage_accounting"]["skill_amounts_rescaled"])
        reconciliation = next(
            row
            for row in record["damage_accounting"]["skill_reconciliation"]
            if row["actor_id"] == TEAMMATE_ID
        )
        self.assertEqual(
            reconciliation,
            {
                "actor_id": TEAMMATE_ID,
                "name": "队友",
                "damage": 200,
                "classified_skill_damage": 50,
                "unclassified_damage": 150,
                "accounted_skill_damage": 200,
                "difference": 0,
            },
        )

    def test_matching_stage_summary_adds_exact_teammate_skills_without_rewriting_damage(self):
        model = CombatModel(run_id="stage-skill-snapshot-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": TEAMMATE_ID, "name": "队友", "entity_type": "Player"}
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "测试首领",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )

        self.assertFalse(model.ingest_team_stat(team_stat(1, TEAMMATE_ID, 0)))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        teammate_hit = damage(3, TEAMMATE_ID, MONSTER_ID, 50)
        teammate_hit["skill_id"] = 86_021_010
        model.ingest(teammate_hit)
        self.assertTrue(model.ingest_team_stat(team_stat(4, TEAMMATE_ID, 200)))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200)
        self.assertEqual(
            {skill_id: row.damage for skill_id, row in model.stats[TEAMMATE_ID].skills.items()},
            {86_021_010: 50, 0: 150},
        )

        death_time = BASE_FILETIME + 5 * 10_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 1_000,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "stage-skill-exact",
                    "filetime_100ns": death_time + 10_000,
                    "member_count": 2,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 200,
                            "skills": [
                                {"skill_id": 86_021_010, "damage": 120, "hits": 3},
                                {"skill_id": 86_021_020, "damage": 70, "hits": 2},
                            ],
                        }
                    ],
                }
            )
        )

        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual(teammate.damage, 200)
        self.assertEqual(
            {
                skill_id: (row.damage, row.hits)
                for skill_id, row in teammate.skills.items()
            },
            {
                86_021_010: (120, 3),
                86_021_020: (70, 2),
                0: (10, 0),
            },
        )
        self.assertEqual(model.stats[SELF_ID].damage, 100)
        record = model.build_combat_record("target_defeated")
        teammate_record = next(
            row for row in record["participants"] if row["actor_id"] == TEAMMATE_ID
        )
        self.assertEqual(teammate_record["skill_source"], "server_stage_summary")
        self.assertEqual(teammate_record["damage"], 200)
        self.assertEqual(sum(row["damage"] for row in teammate_record["skills"]), 200)
        self.assertTrue(
            all(row["max_hit"] is None for row in teammate_record["skills"])
        )
        self.assertEqual(
            record["damage_accounting"]["stage_skill_snapshots"][0][
                "unclassified_damage"
            ],
            10,
        )

    def test_exact_heal_callbacks_build_hps_overheal_peak_target_and_response(self):
        model = CombatModel(run_id="healing-callback-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": SELF_ID, "name": "治疗者", "entity_type": "Player"}
        )
        model.ingest_profile(
            {"entity_id": TEAMMATE_ID, "name": "队友", "entity_type": "Player"}
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))

        self.assertTrue(
            model.ingest_actor_health(
                actor_health(BASE_FILETIME + 10_000_000, TEAMMATE_ID, 10_000, 10_000)
            )
        )
        self.assertTrue(
            model.ingest_actor_health(
                actor_health(BASE_FILETIME + 20_000_000, TEAMMATE_ID, 7_000, 10_000)
            )
        )
        self.assertTrue(
            model.ingest_heal(
                healing(
                    BASE_FILETIME + 25_000_000,
                    SELF_ID,
                    TEAMMATE_ID,
                    total=1_200,
                    effective=900,
                )
            )
        )

        summary = model.healing_summary(duration=10.0)
        healer = summary["healers"][0]
        self.assertEqual(healer["coverage"], "live_exact_callbacks_unverified")
        self.assertEqual(healer["total_healing"], 1_200)
        self.assertEqual(healer["effective_healing"], 900)
        self.assertEqual(healer["overhealing"], 300)
        self.assertEqual(healer["overheal_rate"], 0.25)
        self.assertEqual(healer["hps"], 90.0)
        self.assertEqual(healer["peak_hps"], 180.0)
        self.assertEqual(healer["skills"][0]["share"], 1.0)
        self.assertEqual(healer["targets"][0]["name"], "队友")
        self.assertEqual(healer["response"]["average_ms"], 500.0)
        self.assertEqual(healer["response"]["fastest_ms"], 500.0)
        self.assertEqual(healer["response"]["slowest_ms"], 500.0)
        self.assertEqual(healer["response"]["samples"], 1)
        self.assertEqual(healer["response"]["covered_target_count"], 1)

    def test_healing_summary_reuses_same_second_aggregate_cache(self):
        model = CombatModel(run_id="healing-cache-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest_heal(
            healing(
                BASE_FILETIME + 20_000,
                SELF_ID,
                TEAMMATE_ID,
                total=1_000,
                effective=800,
            )
        )
        calls = []
        original = model._raw_healing_by_actor

        def counted():
            calls.append(True)
            return original()

        model._raw_healing_by_actor = counted
        first = model.healing_summary(duration=10.1)
        second = model.healing_summary(duration=10.9)

        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)
        third = model.healing_summary(duration=11.0)
        self.assertIsNot(first, third)
        self.assertEqual(len(calls), 2)

    def test_distinct_heals_with_same_timestamp_and_amount_are_not_deduplicated(self):
        model = CombatModel(run_id="healing-sequence-dedup")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        first = healing(
            BASE_FILETIME + 20_000,
            SELF_ID,
            TEAMMATE_ID,
            total=500,
            effective=400,
        )
        first["sequence"] = 101
        second = dict(first)
        second["sequence"] = 102

        self.assertTrue(model.ingest_heal(first))
        self.assertTrue(model.ingest_heal(second))
        healer = model.healing_summary(duration=10.0)["healers"][0]
        self.assertEqual(healer["events"], 2)
        self.assertEqual(healer["total_healing"], 1_000)
        self.assertEqual(healer["effective_healing"], 800)

    def test_healing_response_starts_at_first_unanswered_hp_drop(self):
        model = CombatModel(run_id="healing-response-first-drop")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest_actor_health(
            actor_health(BASE_FILETIME + 10_000_000, TEAMMATE_ID, 10_000, 10_000)
        )
        model.ingest_actor_health(
            actor_health(BASE_FILETIME + 20_000_000, TEAMMATE_ID, 8_000, 10_000)
        )
        model.ingest_actor_health(
            actor_health(BASE_FILETIME + 24_000_000, TEAMMATE_ID, 6_000, 10_000)
        )
        model.ingest_heal(
            healing(
                BASE_FILETIME + 30_000_000,
                SELF_ID,
                TEAMMATE_ID,
                total=1_500,
                effective=1_500,
            )
        )

        response = model.healing_summary(duration=10.0)["healers"][0]["response"]
        self.assertEqual(response["samples"], 1)
        self.assertEqual(response["average_ms"], 1_000.0)
        self.assertEqual(response["fastest_ms"], 1_000.0)
        self.assertEqual(response["slowest_ms"], 1_000.0)

    def test_actor_merge_keeps_earliest_unanswered_hp_drop(self):
        model = CombatModel(run_id="healing-response-actor-merge")
        provisional_id = TEAMMATE_ID + 99
        first_drop = BASE_FILETIME + 20_000_000
        later_drop = BASE_FILETIME + 24_000_000
        model.pending_health_drops[provisional_id] = first_drop
        model.pending_health_drops[TEAMMATE_ID] = later_drop

        self.assertTrue(
            model.merge_actor(
                {
                    "from_actor_id": provisional_id,
                    "to_actor_id": TEAMMATE_ID,
                }
            )
        )
        self.assertNotIn(provisional_id, model.pending_health_drops)
        self.assertEqual(model.pending_health_drops[TEAMMATE_ID], first_drop)

    def test_server_healing_total_does_not_scale_partial_callback_details(self):
        model = CombatModel(run_id="healing-partial-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": TEAMMATE_ID, "name": "远端治疗", "entity_type": "Player"}
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(
            model.ingest_heal(
                healing(
                    BASE_FILETIME + 20_000,
                    TEAMMATE_ID,
                    SELF_ID,
                    total=100,
                    effective=71,
                    skill_id=86_011_010,
                )
            )
        )
        death_time = BASE_FILETIME + 30_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 1_000,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "healing-server-partial",
                    "filetime_100ns": death_time + 10_000,
                    "member_count": 2,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {"actor_id": SELF_ID, "damage": 100},
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 0,
                            "effective_healing": 1_928,
                            "healing_skills": [
                                {
                                    "skill_id": 86_011_010,
                                    "effective_healing": 1_928,
                                }
                            ],
                        },
                    ],
                }
            )
        )

        summary = model.healing_summary(duration=10.0)
        healer = summary["healers"][0]
        self.assertEqual(
            healer["coverage"], "server_effective_with_partial_callbacks"
        )
        self.assertEqual(healer["effective_healing"], 1_928)
        self.assertEqual(healer["hps"], 192.8)
        self.assertIsNone(healer["total_healing"])
        self.assertIsNone(healer["overhealing"])
        self.assertIsNone(healer["peak_hps"])
        self.assertEqual(healer["observed_effective_healing"], 71)
        self.assertEqual(healer["skills"][0]["effective_healing"], 1_928)
        self.assertIsNone(healer["skills"][0]["total_healing"])

    def test_server_verified_healing_keeps_fully_overhealed_skill(self):
        model = CombatModel(run_id="healing-zero-effective-skill")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        overheal = healing(
            BASE_FILETIME + 20_000,
            SELF_ID,
            SELF_ID,
            total=500,
            effective=0,
            skill_id=86_021_100,
        )
        overheal["sequence"] = 91
        self.assertTrue(model.ingest_heal(overheal))
        death_time = BASE_FILETIME + 30_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 1_000,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "healing-zero-effective-server",
                    "filetime_100ns": death_time + 10_000,
                    "member_count": 1,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 100,
                            "effective_healing": 0,
                            "healing_skills": [],
                        }
                    ],
                }
            )
        )

        healer = model.healing_summary(duration=10.0)["healers"][0]
        self.assertEqual(healer["coverage"], "server_verified_callbacks")
        self.assertEqual(healer["total_healing"], 500)
        self.assertEqual(healer["effective_healing"], 0)
        self.assertEqual(healer["overhealing"], 500)
        self.assertEqual(healer["overheal_rate"], 1.0)
        self.assertEqual(len(healer["skills"]), 1)
        self.assertEqual(healer["skills"][0]["total_healing"], 500)
        self.assertEqual(healer["skills"][0]["effective_healing"], 0)
        self.assertEqual(healer["skills"][0]["overhealing"], 500)

    def test_midfight_exact_stage_total_enables_teammate_skill_distribution(self):
        model = CombatModel(run_id="midfight-stage-skills")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        self.assertFalse(model.ingest_team_stat(team_stat(1, TEAMMATE_ID, 0)))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 200)))

        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "midfight-skill-exact",
                    "filetime_100ns": BASE_FILETIME + 40_000,
                    "member_count": 2,
                    "authoritative": False,
                    "completion_confirmed": False,
                    "actors": [
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 200,
                            "skills": [
                                {"skill_id": 86_021_010, "damage": 120, "hits": 3},
                                {"skill_id": 86_021_020, "damage": 70, "hits": 2},
                            ],
                        }
                    ],
                }
            )
        )

        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual(
            {skill_id: row.damage for skill_id, row in teammate.skills.items()},
            {86_021_010: 120, 86_021_020: 70, 0: 10},
        )

    def test_mismatched_stage_actor_total_cannot_replace_teammate_skills(self):
        model = CombatModel(run_id="stage-skill-mismatch-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        self.assertFalse(model.ingest_team_stat(team_stat(1, TEAMMATE_ID, 0)))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        teammate_hit = damage(3, TEAMMATE_ID, MONSTER_ID, 50)
        teammate_hit["skill_id"] = 86_021_010
        model.ingest(teammate_hit)
        self.assertTrue(model.ingest_team_stat(team_stat(4, TEAMMATE_ID, 200)))
        death_time = BASE_FILETIME + 5 * 10_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 1_000,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "stage-skill-mismatch",
                    "filetime_100ns": death_time + 10_000,
                    "member_count": 2,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 201,
                            "skills": [
                                {"skill_id": 86_021_020, "damage": 201, "hits": 4}
                            ],
                        }
                    ],
                }
            )
        )
        self.assertIsNone(model.active_stage_skill_snapshot(TEAMMATE_ID))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200)
        self.assertEqual(
            {skill_id: row.damage for skill_id, row in model.stats[TEAMMATE_ID].skills.items()},
            {86_021_010: 50, 0: 150},
        )

    def test_legacy_hp_correlated_events_are_rejected(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )

        local_exact = damage(2, SELF_ID, MONSTER_ID, 900)
        local_exact["damage_source"] = "network_exact"
        model.ingest(local_exact)

        for sequence, actor_id in ((3, SELF_ID), (4, TEAMMATE_ID)):
            correlated = damage(sequence, actor_id, MONSTER_ID, 500)
            correlated.update(
                {
                    "damage_source": "hp_correlated",
                    "provisional_damage": True,
                }
            )
            model.ingest(correlated)

        self.assertEqual(model.stats[SELF_ID].damage, 900)
        self.assertNotIn(TEAMMATE_ID, model.stats)
        self.assertEqual(len(model.events), 1)

        fallback = damage(7, SELF_ID, MONSTER_ID, 50)
        fallback["damage_source"] = "network_exact"
        model.ingest(fallback)
        self.assertEqual(model.stats[SELF_ID].damage, 950)

    def test_team_snapshot_adds_positive_party_member_without_exact_hit(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID, ZERO_TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))

        self.assertTrue(model.ingest_team_stat(team_stat(2, TEAMMATE_ID, 250)))
        self.assertFalse(model.ingest_team_stat(team_stat(2, ZERO_TEAMMATE_ID, 0)))

        self.assertEqual(set(model.stats), {SELF_ID, TEAMMATE_ID})
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250)
        self.assertNotIn(ZERO_TEAMMATE_ID, model.stats)
        self.assertEqual(
            model.display_skill_name(TEAMMATE_ID, 0),
            "未归类伤害",
        )

    def test_inferred_teammate_damage_survives_party_exit_and_archives(self):
        model = CombatModel(run_id="party-exit-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {"entity_ids": [TEAMMATE_ID], "member_count": 2, "authoritative": True}
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "测试首领",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 250_000))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)

        model.ingest_party(
            {"entity_ids": [], "member_count": 1, "authoritative": True}
        )
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)
        model.ingest_scene({"scene_id": 5_200_001})
        model.ingest_scene({"scene_id": 5_200_002})
        record = model.pop_completed_combats()[0]
        self.assertEqual(record["total_damage"], 250_000)
        self.assertEqual(record["team_size"], 1)

    def test_team_damage_is_not_accepted_during_trash_combat(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 120_110,
                "max_hp": 120_110,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        self.assertFalse(
            model.ingest_team_stat(team_stat(1, TEAMMATE_ID, 90_000))
        )
        self.assertNotIn(TEAMMATE_ID, model.stats)

    def test_twelve_member_team_has_no_member_cap(self):
        model = CombatModel()
        teammate_ids = [-900_000_000_000 - index for index in range(11)]
        actor_ids = [SELF_ID, *teammate_ids]
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": teammate_ids})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 10_000_000,
                "max_hp": 10_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        for index, actor_id in enumerate(actor_ids, start=1):
            model.ingest(damage(index, actor_id, MONSTER_ID, index * 100_000))
        self.assertGreaterEqual(len(model.friendly_ids), 12)
        self.assertEqual(len(model.stats), 12)
        self.assertEqual(
            sum(actor.damage for actor in model.stats.values()),
            sum(index * 100_000 for index in range(1, 13)),
        )

    def test_six_member_history_excludes_zero_damage_members(self):
        model = CombatModel(run_id="six-member-test")
        teammate_ids = [-700_000_000_000 - index for index in range(5)]
        actor_ids = [SELF_ID, *teammate_ids]
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {"entity_ids": teammate_ids, "member_count": 6, "authoritative": True}
        )
        for index, actor_id in enumerate(actor_ids):
            model.ingest_profile(
                {
                    "entity_id": actor_id,
                    "name": "莫雪" if actor_id == SELF_ID else f"队员{index}",
                    "profession_id": 1_200_001 + index % 7,
                    "entity_type": "Player",
                }
            )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 10_000_000,
                "max_hp": 10_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 600_000))

        self.assertEqual(len(model.stats), 1)
        self.assertEqual(model.party_member_count, 6)
        self.assertTrue(all(actor.damage > 0 for actor in model.stats.values()))
        record = model.build_combat_record("test")
        self.assertIsNotNone(record)
        self.assertEqual(record["team_size"], 1)
        self.assertEqual(len(record["participants"]), 1)
        self_row = next(item for item in record["participants"] if item["is_self"])
        self.assertEqual(self_row["damage"], 600_000)

    def test_late_party_changes_do_not_rewrite_finished_encounter_size(self):
        model = CombatModel(run_id="late-roster-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        self.assertEqual(model.encounter_team_size, 1)

        late_members = [-810_000_000_000 - index for index in range(11)]
        model.ingest_party(
            {
                "entity_ids": late_members,
                "member_count": 12,
                "authoritative": False,
                "filetime_100ns": BASE_FILETIME + 30 * 10_000_000,
            }
        )
        model.ingest_profile(
            {
                "entity_id": NEARBY_ID,
                "entity_type": "Monster",
                "filetime_100ns": BASE_FILETIME + 31 * 10_000_000,
            }
        )

        self.assertEqual(model.party_member_count, 12)
        self.assertEqual(model.encounter_team_size, 1)
        self.assertEqual(model.build_combat_record()["team_size"], 1)

    def test_live_authoritative_roster_replaces_stale_encounter_size(self):
        model = CombatModel(run_id="authoritative-roster-test")
        stale_members = [-820_000_000_000 - index for index in range(11)]
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": stale_members,
                "member_count": 12,
                "authoritative": False,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        self.assertEqual(model.encounter_team_size, 1)

        model.ingest_party(
            {
                "entity_ids": [],
                "member_count": 1,
                "authoritative": True,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            }
        )

        self.assertEqual(model.party_member_count, 1)
        self.assertEqual(model.encounter_team_size, 1)

    def test_twelve_member_roster_does_not_create_zero_damage_rows(self):
        model = CombatModel()
        teammate_ids = [-800_000_000_000 - index for index in range(11)]
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {"entity_ids": teammate_ids, "member_count": 12, "authoritative": True}
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 10_000_000,
                "max_hp": 10_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, teammate_ids[0], MONSTER_ID, 200_000))
        self.assertEqual(len(model.stats), 2)
        self.assertEqual(model.encounter_team_size, 2)

    def test_actor_merge_moves_packet_name_profession_and_team_damage(self):
        model = CombatModel()
        old_self_actor = -781_628_999_061_186_693
        model.ingest_party(
            {
                "entity_ids": [old_self_actor, TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": old_self_actor,
                "name": "莫雪",
                "profession_id": 1_200_002,
                "entity_type": "Player",
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 10_000_000,
                "max_hp": 10_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, old_self_actor, MONSTER_ID, 350_000))
        model.ingest_life(
            {
                "actor_id": old_self_actor,
                "dead": False,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            }
        )
        model.ingest_life(
            {
                "actor_id": old_self_actor,
                "dead": True,
                "death_confirmed": True,
                "explicit_transition": True,
                "filetime_100ns": BASE_FILETIME + 3 * 10_000,
            }
        )
        model.ingest_identity({"entity_id": SELF_ID})
        self.assertTrue(
            model.merge_actor(
                {"from_actor_id": old_self_actor, "to_actor_id": SELF_ID}
            )
        )
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        self.assertNotIn(old_self_actor, model.stats)
        self.assertNotIn(old_self_actor, model.team_damage_states)
        self.assertEqual(model.stats[SELF_ID].damage, 350_000)
        self.assertEqual(model.display_name(SELF_ID), "莫雪")
        self.assertEqual(model.actor_profession_id(SELF_ID), 1_200_002)
        self.assertEqual(model.member_death_counts[SELF_ID], 1)
        self.assertEqual(model.build_combat_record()["participants"][0]["deaths"], 1)
        self.assertEqual(model.encounter_team_size, 1)

    def test_boss_zero_hp_waits_for_idle_confirmation(self):
        model = CombatModel(run_id="boss-death-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [], "member_count": 1})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "测试首领",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        death_filetime = BASE_FILETIME + 5 * 10_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "filetime_100ns": death_filetime,
            }
        )
        self.assertFalse(model.combat_end_time)
        self.assertFalse(model.active(model.last_damage_time + 5))
        frozen_duration = model.duration(model.last_damage_time + 5)
        self.assertEqual(
            frozen_duration,
            model.duration(model.last_damage_time + model.idle_gap - 0.1),
        )
        self.assertFalse(model.finalize_if_idle(model.last_damage_time + 9.9))
        self.assertTrue(model.finalize_if_idle(model.last_damage_time + 10.0))
        record = model.pop_completed_combats()[0]
        self.assertEqual(record["archive_reason"], "target_defeated")
        self.assertEqual(record["ended_at_epoch"], model.last_damage_time)

    def test_damage_resuming_after_zero_hp_keeps_the_same_encounter(self):
        model = CombatModel(run_id="boss-phase-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000_000,
            }
        )
        resumed = damage(2, TEAMMATE_ID, MONSTER_ID, 200_000)
        resumed["filetime_100ns"] = BASE_FILETIME + 3 * 10_000_000
        model.ingest(resumed)

        self.assertFalse(model.combat_end_time)
        self.assertIsNone(model.current_monster().current_hp)
        self.assertTrue(model.active(model.last_damage_time))
        self.assertEqual(set(model.stats), {SELF_ID, TEAMMATE_ID})
        self.assertEqual(sum(row.damage for row in model.stats.values()), 300_000)

    def test_same_boss_resumes_after_phase_gap_without_clearing_damage(self):
        model = CombatModel(run_id="boss-long-phase-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        first_encounter = model.encounter_id
        self.assertTrue(model.finalize_if_idle(model.last_damage_time + 10.0))
        model.pop_completed_combats()

        resumed = damage(2, TEAMMATE_ID, MONSTER_ID, 200_000)
        resumed["filetime_100ns"] = BASE_FILETIME + 45 * 10_000_000
        model.ingest(resumed)

        self.assertEqual(model.encounter_id, first_encounter)
        self.assertEqual(set(model.stats), {SELF_ID, TEAMMATE_ID})
        self.assertEqual(sum(row.damage for row in model.stats.values()), 300_000)

    def test_previous_stage_summary_is_rejected_at_new_boss_start(self):
        model = CombatModel(run_id="stale-stage-summary-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.reset(keep_identity=True, keep_monsters=False)
        model.ingest_profile(
            {
                "entity_id": SECOND_MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        first_hit = damage(1, SELF_ID, SECOND_MONSTER_ID, 245)
        first_hit["filetime_100ns"] = BASE_FILETIME + 100 * 10_000_000
        model.ingest(first_hit)

        self.assertFalse(
            model.ingest_stage_summary(
                {
                    "summary_id": "5150059|2|previous-stage",
                    "filetime_100ns": first_hit["filetime_100ns"],
                    "member_count": 2,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 18_000_000,
                            "skills": [],
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 18_000_000,
                            "skills": [],
                        },
                    ],
                }
            )
        )
        self.assertEqual(sum(row.damage for row in model.stats.values()), 245)
        self.assertIn(
            "5150059|2|previous-stage",
            model.rejected_stage_summary_ids,
        )

    def test_stage_summary_updates_roster_without_replacing_pull_damage(self):
        model = CombatModel(run_id="stage-summary-test")
        teammate_ids = [TEAMMATE_ID + index for index in range(11)]
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": teammate_ids,
                "member_count": 12,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": SELF_ID, "name": "莫雪", "profession_id": 1_200_002}
        )
        model.ingest_profile(
            {
                "entity_id": teammate_ids[0],
                "name": "准确队友",
                "profession_id": 1_200_003,
            }
        )
        model.ingest_profile(
            {
                "entity_id": teammate_ids[1],
                "name": "零伤害奶妈",
                "profession_id": 1_200_002,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000_000))
        model.ingest(damage(2, teammate_ids[1], MONSTER_ID, 500_000))
        summary_time = BASE_FILETIME + 3 * 10_000_000
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "5150058|1|pull-a",
                    "filetime_100ns": summary_time,
                    "member_count": 12,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 800_000,
                            "skills": [
                                {
                                    "skill_id": 86_020_040,
                                    "damage": 800_000,
                                    "hits": 20,
                                }
                            ],
                        },
                        {
                            "actor_id": teammate_ids[0],
                            "damage": 400_000,
                            "skills": [
                                {
                                    "skill_id": 86_030_010,
                                    "damage": 400_000,
                                    "hits": 10,
                                }
                            ],
                        },
                        {
                            "actor_id": teammate_ids[1],
                            "damage": 0,
                            "skills": [],
                        },
                    ],
                }
            )
        )
        self.assertEqual(set(model.stats), {SELF_ID, teammate_ids[1]})
        self.assertEqual(model.stats[SELF_ID].damage, 1_000_000)
        self.assertEqual(model.stats[teammate_ids[1]].damage, 500_000)
        record = model.build_combat_record("stage")
        self.assertEqual(record["team_size"], 2)
        self.assertEqual(len(record["participants"]), 2)

        post_summary = damage(4, SELF_ID, MONSTER_ID, 100_000)
        post_summary["filetime_100ns"] = summary_time + 10_000
        model.ingest(post_summary)
        self.assertEqual(model.stats[SELF_ID].damage, 1_100_000)

    def test_final_settlement_is_validation_only_and_never_rewrites_damage(self):
        model = CombatModel(run_id="settlement-correction-test")
        actor_ids = [SELF_ID + index for index in range(6)]
        names = ["莫雪", "知幻", "綠鱼", "杨再兴", "恋缘", "汐序"]
        professions = [
            1_200_002,
            1_200_007,
            1_200_005,
            1_200_006,
            1_200_001,
            1_200_006,
        ]
        model.ingest_identity({"entity_id": actor_ids[0]})
        model.ingest_party(
            {
                "entity_ids": actor_ids[1:],
                "member_count": 6,
                "authoritative": True,
            }
        )
        for actor_id, name, profession_id in zip(actor_ids, names, professions):
            model.ingest_profile(
                {
                    "entity_id": actor_id,
                    "name": name,
                    "profession_id": profession_id,
                    "entity_type": "Player",
                }
            )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "朗伯·绞索",
                "entity_type": "Boss",
                "template_id": 7_107_105,
                "boss_rank": 3,
            }
        )

        inferred = [0, 657_311, 493_477, 369_930, 319_049, 312_867]
        for sequence, (actor_id, amount) in enumerate(
            zip(actor_ids, inferred), start=1
        ):
            if amount:
                model.ingest(damage(sequence, actor_id, MONSTER_ID, amount))
        encounter_id = model.encounter_id
        death_time = BASE_FILETIME + 10 * 10_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 2_161_985,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertTrue(model.finalize_if_idle())
        initial = model.pop_completed_combats()
        self.assertEqual(initial[0]["total_damage"], 2_152_634)

        exact = [0, 763_225, 660_893, 593_997, 415_704, 160_562]
        settlement_time = death_time + 2_000_000
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "settlement|5150002|1|sample",
                    "filetime_100ns": settlement_time,
                    "member_count": 6,
                    "authoritative": True,
                    "actors": [
                        {
                            "actor_id": actor_id,
                            "damage": amount,
                            "skills": (
                                []
                                if not amount
                                else [
                                    {
                                        "skill_id": 86_010_010 + index,
                                        "damage": amount,
                                        "hits": index + 1,
                                    }
                                ]
                            ),
                        }
                        for index, (actor_id, amount) in enumerate(
                            zip(actor_ids, exact)
                        )
                    ],
                }
            )
        )

        self.assertEqual(
            {actor_id: actor.damage for actor_id, actor in model.stats.items()},
            {
                actor_id: amount
                for actor_id, amount in zip(actor_ids, inferred)
                if amount
            },
        )
        refreshed = model.pop_completed_combats()
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["archive_reason"], "target_defeated")
        self.assertEqual(refreshed[0]["total_damage"], sum(inferred))
        validated = model.build_combat_record("target_defeated")
        self.assertEqual(validated["encounter_id"], encounter_id)
        self.assertEqual(validated["total_damage"], 2_152_634)
        self.assertEqual(
            [row["damage"] for row in validated["participants"]],
            inferred[1:],
        )
        validation = validated["damage_accounting"][
            "stage_summary_validations"
        ][0]
        self.assertTrue(validation["validation_only"])
        self.assertFalse(validation["validation"]["damage_correction_applied"])
        self.assertTrue(
            validation["validation"]["would_allow_legacy_correction"]
        )
        self.assertFalse(validation["validation"]["per_actor_exact_match"])
        self.assertEqual(validation["validation"]["summary_total"], 2_594_381)
        self.assertEqual(validation["validation"]["observed_total"], 2_152_634)
        self.assertEqual(validation["validation"]["difference"], 441_747)

    def test_feedback_fb9466_common_matches_all_twelve_completion_rows(self):
        """FB9466C5D00ECF5699: Common is exact before completion arrives."""
        model = CombatModel(run_id="fb9466-astrologer-completion-test")
        actors = [
            (57_185_883_868_347, "歌莉雅丶", 1_200_003, 3_925_579, 3_925_579),
            (57_386_136_789_472, "紫丶涩", 1_200_002, 25_457, 13_431),
            (57_192_862_116_237, "烟雨乂江南", 1_200_003, 4_391_742, 4_337_458),
            (57_202_526_989_526, "曲戈", 1_200_003, 3_641_908, 4_334_496),
            (57_271_246_667_082, "风华", 1_200_006, 727_606, 423_219),
            (57_425_865_521_285, "羽落凡辰", 1_200_005, 4_427_567, 4_121_193),
            (57_338_355_340_533, "露娜丶", 1_200_007, 1_894_287, 3_077_155),
            (57_338_892_134_378, "小孩这庙灵吗", 1_200_001, 2_108_340, 2_606_822),
            (57_404_390_650_055, "哈哈嘿", 1_200_007, 3_399_390, 3_956_911),
            (57_427_476_113_084, "中奖名单", 1_200_003, 3_712_152, 4_195_377),
            (57_430_160_023_251, "夜空中最靓的星", 1_200_002, 101_416, 459),
            (57_421_570_080_938, "与其追风去", 1_200_005, 5_234_877, 4_105_790),
        ]
        self_id = actors[0][0]
        model.ingest_identity({"entity_id": self_id})
        model.ingest_party(
            {
                "entity_ids": [actor_id for actor_id, *_rest in actors[1:]],
                "member_count": 12,
                "authoritative": True,
            }
        )
        for actor_id, name, profession_id, _packet_damage, _game_damage in actors:
            model.ingest_profile(
                {
                    "entity_id": actor_id,
                    "name": name,
                    "profession_id": profession_id,
                    "entity_type": "Player",
                }
            )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 33_270_350,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        instance = 1_788_020_043
        for actor_id, *_rest in actors:
            model.ingest_team_stat(
                {
                    "filetime_100ns": BASE_FILETIME,
                    "actor_id": actor_id,
                    "absolute_damage": 0,
                    "server_time": instance,
                }
            )
        for sequence, (actor_id, _name, _profession, packet_damage, _game) in enumerate(
            actors, start=1
        ):
            model.ingest(damage(sequence, actor_id, MONSTER_ID, packet_damage))
        self.assertEqual(sum(row.damage for row in model.stats.values()), 33_590_321)

        for sequence, (actor_id, _name, _profession, _packet, game_damage) in enumerate(
            actors, start=20
        ):
            model.ingest_team_stat(
                team_stat(
                    sequence,
                    actor_id,
                    game_damage,
                    server_time=instance,
                )
            )
        self.assertEqual(sum(row.damage for row in model.stats.values()), 35_097_890)

        death_time = BASE_FILETIME + 20 * 10_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 33_270_350,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "settlement|5150060|3|apLvgXqQbOZkQ5DX",
                    "filetime_100ns": death_time + 149_642_340,
                    "member_count": 12,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": actor_id,
                            "name": name,
                            "profession_id": profession_id,
                            "damage": game_damage,
                            "skills": [],
                        }
                        for actor_id, name, profession_id, _packet, game_damage in actors
                    ],
                }
            )
        )

        exact_damage = {
            actor_id: game_damage
            for actor_id, _name, _profession, _packet, game_damage in actors
        }
        self.assertEqual(
            {actor_id: row.damage for actor_id, row in model.stats.items()},
            exact_damage,
        )
        self.assertEqual(sum(row.damage for row in model.stats.values()), 35_097_890)
        self.assertEqual(model.stats[57_421_570_080_938].damage, 4_105_790)
        self.assertEqual(model.stats[57_425_865_521_285].damage, 4_121_193)
        record = model.build_combat_record("target_defeated")
        validation = record["damage_accounting"]["stage_summary_validations"][0]
        self.assertTrue(validation["validation_only"])
        self.assertTrue(validation["validation"]["confirmed_multiphase_completion"])
        self.assertFalse(validation["validation"]["damage_correction_applied"])
        self.assertEqual(validation["validation"]["difference"], 0)
        self.assertTrue(validation["validation"]["per_actor_exact_match"])
        self.assertTrue(
            all(
                row["difference"] == 0
                for row in validation["validation"]["actor_differences"]
            )
        )
        self.assertEqual(record["total_damage"], 35_097_890)

    def test_feedback_fb9561_common_replaces_inflated_observer_damage(self):
        """FB9561E555D7298DFF: Common replaces observer-local inflation."""
        model = CombatModel(run_id="fb9561-astrologer-common-test")
        self_id = 26_469_350_884_919
        exact_damage = {
            39_655_974_394_423: 4_698_584,
            44_114_686_660_146: 743_738,
            57_405_461_980_853: 556_930,
            39_841_194_850_739: 2_799_002,
            39_859_448_560_268: 0,
            self_id: 3_656_999,
            88_021_600_320_006: 3_188_306,
            87_966_302_592_094: 3_677_249,
            88_021_600_332_760: 2_659_879,
            88_232_053_545_579: 3_191_948,
            61_700_430_230_730: 3_721_554,
            57_234_739_124_420: 0,
        }
        model.ingest_identity({"entity_id": self_id})
        model.ingest_party(
            {
                "entity_ids": [
                    actor_id for actor_id in exact_damage if actor_id != self_id
                ],
                "member_count": 12,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 33_270_350,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        instance = 1_788_020_043
        for sequence, actor_id in enumerate(exact_damage, start=1):
            model.ingest_team_stat(
                team_stat(sequence, actor_id, 0, server_time=instance)
            )

        # The affected build accumulated 8,390,816 from observer-local events
        # for this player even though the game's per-player value was 3,656,999.
        model.ingest(damage(20, self_id, MONSTER_ID, 8_390_816))
        self.assertEqual(model.stats[self_id].damage, 8_390_816)
        for sequence, (actor_id, amount) in enumerate(
            exact_damage.items(), start=30
        ):
            model.ingest_team_stat(
                team_stat(
                    sequence,
                    actor_id,
                    amount,
                    server_time=instance,
                )
            )

        expected_visible = {
            actor_id: amount for actor_id, amount in exact_damage.items() if amount
        }
        self.assertEqual(
            {actor_id: row.damage for actor_id, row in model.stats.items()},
            expected_visible,
        )
        self.assertEqual(model.stats[self_id].damage, 3_656_999)
        self.assertEqual(sum(row.damage for row in model.stats.values()), 28_894_189)

        death_time = BASE_FILETIME + 20 * 10_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 33_270_350,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "5150060|3|fb9561",
                    "filetime_100ns": death_time + 31 * 10_000_000,
                    "member_count": 12,
                    "authoritative": False,
                    "completion_confirmed": True,
                    "actors": [
                        {"actor_id": actor_id, "damage": amount, "skills": []}
                        for actor_id, amount in exact_damage.items()
                    ],
                }
            )
        )
        self.assertEqual(
            {actor_id: row.damage for actor_id, row in model.stats.items()},
            expected_visible,
        )
        validation = model.build_combat_record("target_defeated")[
            "damage_accounting"
        ]["stage_summary_validations"][0]
        self.assertTrue(validation["validation_only"])
        self.assertEqual(validation["validation"]["difference"], 0)
        self.assertTrue(validation["validation"]["per_actor_exact_match"])
        self.assertFalse(validation["validation"]["damage_correction_applied"])

    def test_delayed_final_common_matches_baldwin_completion_table(self):
        """The last Common is exact before the table arrives 171 seconds later."""
        model = CombatModel(run_id="baldwin-completed-stage-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID, NEARBY_ID],
                "member_count": 3,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "Baldwin",
                "entity_type": "Boss",
                "template_id": 7_103_401,
                "boss_rank": 3,
            }
        )
        instance = 1_788_020_043
        for actor_id in (SELF_ID, TEAMMATE_ID, NEARBY_ID):
            model.ingest_team_stat(
                {
                    "filetime_100ns": BASE_FILETIME,
                    "actor_id": actor_id,
                    "absolute_damage": 0,
                    "server_time": instance,
                }
            )

        self_damage = damage(1, SELF_ID, MONSTER_ID, 46_776)
        self_damage["filetime_100ns"] = BASE_FILETIME + 1 * 10_000_000
        model.ingest(self_damage)
        teammate_damage = damage(2, TEAMMATE_ID, MONSTER_ID, 35_666_894)
        teammate_damage["filetime_100ns"] = BASE_FILETIME + 2 * 10_000_000
        model.ingest(teammate_damage)
        omitted_zero_damage = damage(3, NEARBY_ID, MONSTER_ID, 40_787)
        omitted_zero_damage["filetime_100ns"] = BASE_FILETIME + 3 * 10_000_000
        model.ingest(omitted_zero_damage)
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 35_754_457,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 3 * 10_000_000 + 1,
            }
        )
        self.assertTrue(model.finalize_if_idle(model.last_damage_time + 10.1))
        initial = model.pop_completed_combats()
        self.assertEqual(initial[0]["total_damage"], 35_754_457)

        final_common = (
            (SELF_ID, 5_832),
            (TEAMMATE_ID, 38_528_659),
            (NEARBY_ID, 0),
        )
        for sequence, (actor_id, amount) in enumerate(final_common, start=4_000):
            model.ingest_team_stat(
                team_stat(
                    sequence,
                    actor_id,
                    amount,
                    server_time=instance,
                )
            )
        self.assertEqual(sum(row.damage for row in model.stats.values()), 38_534_491)
        common_refreshes = model.pop_completed_combats()
        self.assertTrue(common_refreshes)
        self.assertEqual(common_refreshes[-1]["total_damage"], 38_534_491)

        completion_time = BASE_FILETIME + 174 * 10_000_000
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "settlement|5150059|2|completed-stage",
                    "filetime_100ns": completion_time,
                    "member_count": 3,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {"actor_id": SELF_ID, "damage": 5_832, "skills": []},
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 38_528_659,
                            "skills": [],
                        },
                        {"actor_id": NEARBY_ID, "damage": 0, "skills": []},
                    ],
                }
            )
        )

        self.assertEqual(sum(row.damage for row in model.stats.values()), 38_534_491)
        self.assertNotIn(NEARBY_ID, model.stats)
        refreshed = model.pop_completed_combats()
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["total_damage"], 38_534_491)
        validation = refreshed[0]["damage_accounting"][
            "stage_summary_validations"
        ][0]
        self.assertTrue(validation["completion_confirmed"])
        self.assertTrue(validation["validation"]["end_snapshot"])
        self.assertAlmostEqual(
            validation["validation"]["summary_delay_seconds"], 171.0
        )
        self.assertFalse(validation["validation"]["damage_correction_applied"])
        self.assertEqual(validation["validation"]["difference"], 0)
        self.assertTrue(validation["validation"]["per_actor_exact_match"])

    def test_ancestor_knight_damage_survives_form_changes_and_final_mvp(self):
        model = CombatModel(run_id="ancestor-knight-test")
        knight_actor = SELF_ID + 90_000
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "先祖铠甲",
                "entity_type": "Boss",
                "template_id": 7_103_402,
                "boss_rank": 3,
            }
        )

        normal = damage(1, SELF_ID, MONSTER_ID, 1_000)
        normal["skill_id"] = 86_060_040
        model.ingest(normal)

        model.merge_actor(
            {"from_actor_id": SELF_ID, "to_actor_id": knight_actor}
        )
        model.ingest_identity({"entity_id": knight_actor})
        for sequence, (skill_id, amount) in enumerate(
            ((820_600_101, 2_000), (800_011_802, 1_500)), start=2
        ):
            transformed = damage(sequence, knight_actor, MONSTER_ID, amount)
            transformed["skill_id"] = skill_id
            model.ingest(transformed)

        model.merge_actor(
            {"from_actor_id": knight_actor, "to_actor_id": SELF_ID}
        )
        model.ingest_identity({"entity_id": SELF_ID})
        self.assertEqual(model.stats[SELF_ID].damage, 4_500)

        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "settlement|ancestor|cross-boss-mvp",
                    "filetime_100ns": BASE_FILETIME + 5 * 10_000_000,
                    "member_count": 1,
                    "authoritative": True,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 1_000,
                            "skills": [],
                        }
                    ],
                }
            )
        )

        actor = model.stats[SELF_ID]
        self.assertEqual(actor.damage, 4_500)
        self.assertEqual(
            {skill_id: skill.damage for skill_id, skill in actor.skills.items()},
            {86_060_040: 1_000, 820_600_101: 2_000, 800_011_802: 1_500},
        )
        validation = model.build_combat_record()["damage_accounting"][
            "stage_summary_validations"
        ][0]["validation"]
        self.assertEqual(validation["summary_total"], 1_000)
        self.assertEqual(validation["observed_total"], 4_500)

    def test_multiphase_settlement_does_not_end_or_replace_live_encounter(self):
        """A cross-Boss MVP table cannot control a live Astrologer pull."""
        model = CombatModel(run_id="multiphase-settlement-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 200_000))
        realtime_end = model.last_damage_time
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 33_270_350,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 3 * 10_000_000,
            }
        )
        self.assertFalse(model.combat_end_time)

        settlement_time = BASE_FILETIME + 4 * 10_000_000
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "settlement|astrologer|exact",
                    "filetime_100ns": settlement_time,
                    "member_count": 2,
                    "authoritative": True,
                    "actors": [
                        {"actor_id": SELF_ID, "damage": 125_000, "skills": []},
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 225_000,
                            "skills": [],
                        },
                    ],
                }
            )
        )
        self.assertFalse(model.combat_end_reason)
        self.assertFalse(model.combat_end_time)
        self.assertEqual(model.last_damage_time, realtime_end)
        self.assertEqual(sum(row.damage for row in model.stats.values()), 300_000)
        self.assertEqual(model.pop_completed_combats(), [])
        validation = model.build_combat_record()["damage_accounting"][
            "stage_summary_validations"
        ][0]
        self.assertTrue(validation["validation_only"])
        self.assertTrue(validation["validation"]["would_have_matched"])

    def test_settlement_cannot_overwrite_target_split_or_death_count(self):
        model = CombatModel(run_id="settlement-detail-test")
        ice_one = SECOND_MONSTER_ID + 20_001
        ice_two = SECOND_MONSTER_ID + 20_002
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "朗伯·绞索",
                "entity_type": "Boss",
                "template_id": 7_107_105,
                "boss_rank": 3,
            }
        )
        for entity_id in (ice_one, ice_two):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": "冰牢",
                    "entity_type": "Monster",
                    "template_id": 7_107_121,
                    "encounter_auxiliary": True,
                    "encounter_parent_template_ids": [7_107_105],
                }
            )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 600))
        model.ingest(damage(2, SELF_ID, ice_one, 100))
        model.ingest(damage(3, SELF_ID, ice_two, 300))
        model.ingest(damage(4, TEAMMATE_ID, MONSTER_ID, 400))
        model.ingest(damage(5, TEAMMATE_ID, ice_one, 100))
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": False,
                "filetime_100ns": BASE_FILETIME + 6 * 10_000,
            }
        )
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": True,
                "death_confirmed": True,
                "explicit_transition": True,
                "filetime_100ns": BASE_FILETIME + 7 * 10_000,
            }
        )
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": True,
                "death_confirmed": True,
                "explicit_transition": True,
                "filetime_100ns": BASE_FILETIME + 8 * 10_000,
            }
        )
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": False,
                "explicit_transition": True,
                "filetime_100ns": BASE_FILETIME + 9 * 10_000,
            }
        )
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "settlement|langbo-target-split",
                    "filetime_100ns": BASE_FILETIME + 10 * 10_000,
                    "member_count": 2,
                    "authoritative": True,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 2_000,
                            "damage_hits": 20,
                            "critical_hits": 8,
                            "deaths": 3,
                            "skills": [],
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 1_000,
                            "damage_hits": 10,
                            "critical_hits": 5,
                            "deaths": 2,
                            "skills": [],
                        },
                    ],
                }
            )
        )

        record = model.build_combat_record("test")
        self.assertIsNotNone(record)
        participants = {
            int(row["actor_id"]): row for row in record["participants"]
        }
        self_row = participants[SELF_ID]
        self.assertEqual(self_row["damage"], 1_000)
        self.assertIsNone(self_row["damage_hits"])
        self.assertIsNone(self_row["critical_hits"])
        self.assertIsNone(self_row["critical_rate"])
        self.assertEqual(self_row["deaths"], 1)
        self.assertEqual(participants[TEAMMATE_ID]["deaths"], 0)
        self.assertEqual(
            [
                (row["kind"], row["name"], row["damage"], row["share"])
                for row in self_row["targets"]
            ],
            [
                ("Boss", "朗伯·绞索", 600, 0.6),
                ("小怪", "冰牢", 400, 0.4),
            ],
        )
        teammate_targets = participants[TEAMMATE_ID]["targets"]
        self.assertEqual(
            [(row["name"], row["damage"]) for row in teammate_targets],
            [("朗伯·绞索", 400), ("冰牢", 100)],
        )

    def test_realtime_critical_rate_uses_explicit_hit_type_without_settlement(self):
        model = CombatModel(run_id="realtime-critical-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        for sequence, critical in enumerate((False, True, True), start=1):
            event = damage(sequence, SELF_ID, MONSTER_ID, 1_000)
            event["critical"] = critical
            model.ingest(event)

        actor = model.stats[SELF_ID]
        self.assertEqual(actor.damage_hits, 3)
        self.assertEqual(actor.critical_hits, 2)
        record = model.build_combat_record("test")
        participant = record["participants"][0]
        self.assertEqual(participant["critical_rate"], 2 / 3)

    def test_dummy_encounter_only_shows_local_player(self):
        model = CombatModel(run_id="dummy-local-only-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "公会伤害木桩",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )

        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 9_000))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 1_500))
        self.assertFalse(model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 9_000)))

        self.assertEqual(set(model.stats), {SELF_ID})
        self.assertEqual([row.actor_id for row in model.current_stats()], [SELF_ID])
        self.assertEqual(model.team_damage_states[TEAMMATE_ID].last_absolute, 9_000)
        self.assertEqual(model.team_damage_states[TEAMMATE_ID].accepted_damage, 0)
        record = model.build_combat_record("test")
        self.assertEqual(record["total_damage"], 1_500)
        self.assertEqual(record["team_size"], 1)
        self.assertEqual(
            [row["actor_id"] for row in record["participants"]], [SELF_ID]
        )

    def test_dummy_local_only_filter_recovers_after_late_identity(self):
        model = CombatModel(run_id="dummy-late-identity-test")
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩(匀速移动）",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 8_000))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 2_000))
        self.assertEqual(model.stats, {})

        model.ingest_identity({"entity_id": SELF_ID})

        self.assertEqual(set(model.stats), {SELF_ID})
        self.assertEqual(model.stats[SELF_ID].damage, 2_000)
        self.assertEqual(model.encounter_member_ids, {SELF_ID})
        self.assertEqual(model.encounter_team_size, 1)

    def test_damage_switches_immediately_between_dummy_entities(self):
        model = CombatModel(run_id="dummy-target-switch-test")
        next_dummy_id = MONSTER_ID + 1
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_500))
        model.reset(
            keep_identity=True,
            keep_monsters=True,
            preserve_active_target=True,
            archive_reason="manual_reset",
        )

        # A newly selected dummy may initially have only its packet name. Its
        # first real hit must release the stale dummy lock without an idle wait.
        model.ingest_profile(
            {
                "entity_id": next_dummy_id,
                "name": "伤害木桩(匀速移动）",
                "entity_type": "Monster",
            }
        )
        model.ingest(damage(2, SELF_ID, next_dummy_id, 2_500))

        self.assertEqual(model.combat_target_id, next_dummy_id)
        self.assertEqual(model.stats[SELF_ID].damage, 2_500)
        self.assertEqual(model.encounter_member_ids, {SELF_ID})
        self.assertEqual(model.encounter_team_size, 1)

    def test_delayed_dummy_team_total_cannot_remove_exact_local_hit(self):
        model = CombatModel(run_id="dummy-delayed-total-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 812))
        self.assertFalse(model.ingest_team_stat(team_stat(2, SELF_ID, 812)))
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 1_183))
        self.assertFalse(model.ingest_team_stat(team_stat(4, SELF_ID, 812)))

        self.assertEqual(model.stats[SELF_ID].damage, 1_995)
        self.assertEqual(model.stats[SELF_ID].hits, 2)
        self.assertEqual(
            model.stats[SELF_ID].skills[0].damage
            if 0 in model.stats[SELF_ID].skills
            else sum(
                skill.damage for skill in model.stats[SELF_ID].skills.values()
            ),
            1_995,
        )

    def test_pre_pull_team_snapshot_cannot_shadow_exact_dummy_hits(self):
        model = CombatModel(run_id="dummy-pre-pull-snapshot-test")
        model.ingest_identity({"entity_id": SELF_ID})
        pre_pull = team_stat(1, SELF_ID, 37_500)
        pre_pull["full_snapshot"] = True
        model.ingest_team_stat(pre_pull)
        self.assertTrue(
            model.team_damage_states[SELF_ID].authoritative_snapshot
        )

        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩",
                "entity_type": "Boss",
                "template_id": 7_114_223,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 812))
        self.assertFalse(model.ingest_team_stat(team_stat(3, SELF_ID, 812)))
        self.assertEqual(model.stats[SELF_ID].damage, 812)
        self.assertFalse(
            model.team_damage_states[SELF_ID].authoritative_snapshot
        )

        model.ingest(damage(4, SELF_ID, MONSTER_ID, 1_183))
        self.assertFalse(model.ingest_team_stat(team_stat(5, SELF_ID, 1_995)))
        self.assertEqual(model.stats[SELF_ID].damage, 1_995)
        self.assertEqual(model.stats[SELF_ID].hits, 2)

    def test_midfight_stage_metrics_update_critical_without_importing_old_deaths(self):
        model = CombatModel(run_id="stage-metrics-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 2_000))
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "stage-metrics-exact",
                    "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                    "member_count": 2,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 4_000,
                            "damage_hits": 20,
                            "critical_hits": 8,
                            "deaths": 1,
                            "skills": [],
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 8_000,
                            "damage_hits": 25,
                            "critical_hits": 15,
                            "deaths": 2,
                            "skills": [],
                        },
                    ],
                }
            )
        )

        self.assertEqual(model.stats[SELF_ID].damage, 1_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 2_000)
        self.assertEqual(model.stats[SELF_ID].critical_hits, 8)
        self.assertEqual(model.stats[TEAMMATE_ID].critical_hits, 15)
        model.combat_end_time = model.last_damage_time
        model.combat_end_reason = "target_defeated"
        self.assertTrue(model.archive_current("target_defeated"))
        completed = model.pop_completed_combats()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["total_damage"], 3_000)
        participants = {
            row["actor_id"]: row for row in completed[0]["participants"]
        }
        self.assertEqual(participants[SELF_ID]["critical_rate"], 0.4)
        self.assertEqual(participants[TEAMMATE_ID]["critical_rate"], 0.6)
        self.assertEqual(participants[SELF_ID]["deaths"], 0)
        self.assertEqual(participants[TEAMMATE_ID]["deaths"], 0)

    def test_stage_metrics_preserve_current_pull_life_transition_deaths(self):
        model = CombatModel(run_id="stage-death-isolation-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 1_000))
        model.ingest_life(
            {
                "actor_id": TEAMMATE_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            }
        )

        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "previous-pulls-cumulative-deaths",
                    "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                    "member_count": 2,
                    "actors": [
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 1_000,
                            "damage_hits": 10,
                            "critical_hits": 5,
                            "deaths": 7,
                            "skills": [],
                        }
                    ],
                }
            )
        )
        self.assertEqual(model.member_death_counts[TEAMMATE_ID], 1)
        self.assertEqual(
            model.build_combat_record()["participants"][0]["deaths"], 1
        )

    def test_first_observed_dead_state_counts_during_active_encounter(self):
        model = CombatModel(run_id="first-death-state-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 1_000))

        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": TEAMMATE_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + 2 * 10_000,
                }
            )
        )
        self.assertEqual(model.member_death_counts[TEAMMATE_ID], 1)

    def test_nonzero_unconfirmed_dead_state_does_not_increment_deaths(self):
        """FBD54AD8CFD46B5926: only a zero-HP team state confirms death."""
        model = CombatModel(run_id="confirmed-death-evidence-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 1_000))

        self.assertFalse(
            model.ingest_life(
                {
                    "actor_id": TEAMMATE_ID,
                    "dead": True,
                    "current_hp": 4_218,
                    "death_confirmed": False,
                    "filetime_100ns": BASE_FILETIME + 2 * 10_000,
                }
            )
        )
        self.assertNotIn(TEAMMATE_ID, model.member_death_counts)
        self.assertNotIn(TEAMMATE_ID, model.member_death_states)

        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": TEAMMATE_ID,
                    "dead": True,
                    "current_hp": 0,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                }
            )
        )
        self.assertEqual(model.member_death_counts[TEAMMATE_ID], 1)
        self.assertFalse(
            model.ingest_life(
                {
                    "actor_id": TEAMMATE_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                }
            )
        )
        self.assertEqual(model.member_death_counts[TEAMMATE_ID], 1)

    def test_delayed_damage_before_death_does_not_create_a_second_death(self):
        """FBD2EA0E4E3620B20E: out-of-order hits keep the dead state."""
        model = CombatModel(run_id="delayed-hit-death-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 1_000))
        death_time = BASE_FILETIME + 3 * 10_000
        model.ingest_life(
            {
                "actor_id": TEAMMATE_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )

        delayed_hit = damage(2, TEAMMATE_ID, MONSTER_ID, 500)
        model.ingest(delayed_hit)
        self.assertTrue(model.member_death_states[TEAMMATE_ID])
        self.assertFalse(
            model.ingest_life(
                {
                    "actor_id": TEAMMATE_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": death_time + 1,
                }
            )
        )
        self.assertEqual(model.member_death_counts[TEAMMATE_ID], 1)

    def test_damage_after_death_does_not_implicitly_revive_or_recount(self):
        model = CombatModel(run_id="post-death-damage-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 1_000))
        model.ingest_life(
            {
                "actor_id": TEAMMATE_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            }
        )

        model.ingest(damage(3, TEAMMATE_ID, MONSTER_ID, 500))
        self.assertTrue(model.member_death_states[TEAMMATE_ID])
        self.assertFalse(
            model.ingest_life(
                {
                    "actor_id": TEAMMATE_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + 4 * 10_000,
                }
            )
        )
        self.assertEqual(model.member_death_counts[TEAMMATE_ID], 1)

    def test_large_instance_stage_total_cannot_inflate_dummy_pull(self):
        model = CombatModel(run_id="dummy-cumulative-stage-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_760_003))
        summary_time = BASE_FILETIME + 144 * 10_000_000
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "dummy-instance-cumulative",
                    "filetime_100ns": summary_time,
                    "member_count": 1,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 252_800_000,
                            "damage_hits": 5_000,
                            "critical_hits": 4_000,
                            "skills": [],
                        }
                    ],
                }
            )
        )
        self.assertEqual(model.stats[SELF_ID].damage, 1_760_003)
        self.assertIsNone(model.stats[SELF_ID].critical_hits)
        self.assertEqual(model.build_combat_record()["total_damage"], 1_760_003)

    def test_idle_combat_builds_complete_history_record_once(self):
        model = CombatModel(run_id="history-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {
                "entity_id": SELF_ID,
                "name": "莫雪",
                "profession_id": 1_200_001,
                "entity_type": "player",
            }
        )
        model.ingest_profile(
            {
                "entity_id": TEAMMATE_ID,
                "name": "队友甲",
                "profession_id": 1_200_002,
                "entity_type": "player",
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "测试首领",
                "level": 65,
                "entity_type": "boss",
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 10_000_000,
                "max_hp": 10_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 300_000))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 500_000))

        self.assertTrue(model.finalize_if_idle(model.last_damage_time + 11.0))
        self.assertFalse(model.finalize_if_idle(model.last_damage_time + 12.0))
        records = model.pop_completed_combats()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["encounter_id"], "history-test-000000")
        self.assertEqual(record["monster"]["name"], "测试首领")
        self.assertEqual(record["team_size"], 2)
        self.assertEqual(record["total_damage"], 800_000)
        self.assertEqual(
            [participant["name"] for participant in record["participants"]],
            ["队友甲", "莫雪"],
        )

    def test_finished_combat_remains_until_manual_reset_and_keeps_history(self):
        model = CombatModel(run_id="manual-clear-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))

        finished_at = model.last_damage_time + model.idle_gap
        self.assertTrue(model.finalize_if_idle(finished_at))
        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_damage"], 100_000)
        self.assertIn(SELF_ID, model.stats)
        self.assertFalse(model.finalize_if_idle(finished_at + 24 * 60 * 60))
        self.assertIn(SELF_ID, model.stats)
        model.reset(
            keep_identity=True,
            keep_monsters=True,
            archive_reason="manual_reset",
        )
        self.assertEqual(model.stats, {})
        self.assertEqual(model.pop_completed_combats(), [])

    def test_idle_old_boss_and_new_boss_are_always_separate_encounters(self):
        model = CombatModel(run_id="boss-switch-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_profile(
            {
                "entity_id": SECOND_MONSTER_ID,
                "name": "瑞尔·比伯",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 10_000))
        next_fight = damage(2, TEAMMATE_ID, SECOND_MONSTER_ID, 20_000)
        next_fight["filetime_100ns"] = (
            BASE_FILETIME + 100 * 10_000_000
        )
        model.ingest(next_fight)

        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["monster"]["name"], "伤害木桩")
        self.assertEqual(history[0]["total_damage"], 10_000)
        self.assertEqual(model.current_monster().name, "瑞尔·比伯")
        self.assertEqual(set(model.stats), {TEAMMATE_ID})
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 20_000)

    def test_quiet_full_hp_reset_ends_pull_and_reused_entity_starts_new_one(self):
        model = CombatModel(run_id="boss-reset-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "测试首领",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 500_000,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000_000,
            }
        )
        reset_time = int(
            (model.last_damage_time + model.idle_gap + 1.0) * 10_000_000
            + 116_444_736_000_000_000
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "filetime_100ns": reset_time,
            }
        )
        self.assertFalse(model.combat_end_time)
        self.assertTrue(model.boss_reset_pending_100ns)
        self.assertTrue(
            model.finalize_if_idle(model.last_damage_time + model.idle_gap + 1.0)
        )
        self.assertEqual(model.combat_end_time, model.last_damage_time)
        self.assertEqual(model.combat_end_reason, "target_reset")
        history = model.pop_completed_combats()
        self.assertEqual(history[0]["archive_reason"], "target_reset")

        next_pull = damage(2, TEAMMATE_ID, MONSTER_ID, 50_000)
        next_pull["filetime_100ns"] = reset_time + 10_000
        model.ingest(next_pull)
        self.assertEqual(set(model.stats), {TEAMMATE_ID})
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 50_000)

    def test_astrologer_phase_refill_and_long_gap_stay_in_one_encounter(self):
        """FB3958DB48DB1D4CE8: a guard phase must not split one Boss pull."""
        model = CombatModel(run_id="astrologer-wipe-reset-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 33_270_350,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 3_894_649))
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 20_888_137,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000_000,
            }
        )
        self.assertTrue(model._multiphase_encounter())
        self.assertEqual(model._encounter_idle_timeout(), model.encounter_gap)

        reset_time = int(
            (model.last_damage_time + model.idle_gap + 1.0) * 10_000_000
            + 116_444_736_000_000_000
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "reset_candidate_hp": 33_270_350,
                "max_hp": 33_270_350,
                "filetime_100ns": reset_time,
            }
        )

        self.assertFalse(model.combat_end_time)
        self.assertTrue(model.boss_reset_pending_100ns)
        encounter_id = model.encounter_id
        self.assertFalse(
            model.finalize_if_idle(
                model.last_damage_time + model.encounter_gap + 14.0
            )
        )
        self.assertFalse(model.combat_end_reason)
        self.assertFalse(model.combat_end_time)

        resumed_hit = damage(2, TEAMMATE_ID, MONSTER_ID, 250_000)
        resumed_hit["filetime_100ns"] = int(
            (model.last_damage_time + model.encounter_gap + 14.0) * 10_000_000
            + 116_444_736_000_000_000
        )
        model.ingest(resumed_hit)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(set(model.stats), {SELF_ID, TEAMMATE_ID})
        self.assertEqual(model.stats[SELF_ID].damage, 3_894_649)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)
        self.assertFalse(model.boss_reset_pending_100ns)
        self.assertEqual(model.pop_completed_combats(), [])

    def test_astrologer_final_death_freezes_dps_before_delayed_archive(self):
        """FB7408AA158422D3F4: final DPS must not fall and rebound."""
        model = CombatModel(run_id="astrologer-final-death-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 33_270_350,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000_000))

        guard_phase_time = BASE_FILETIME + 5 * 10_000_000
        model.ingest_monster(
            {
                "entity_id": SECOND_MONSTER_ID,
                "template_id": 7_102_405,
                "current_hp": 1_325_792,
                "max_hp": 1_325_792,
                "filetime_100ns": guard_phase_time + 10_000,
            }
        )
        phase_hit = damage(2, SELF_ID, SECOND_MONSTER_ID, 200_000)
        phase_hit["filetime_100ns"] = guard_phase_time + 20_000
        model.ingest(phase_hit)
        self.assertTrue(model.active(model._event_seconds(phase_hit)))
        self.assertEqual(model.stats[SELF_ID].damage, 1_200_000)

        final_hit = damage(3, SELF_ID, MONSTER_ID, 500_000)
        final_hit["filetime_100ns"] = guard_phase_time + 50_000
        model.ingest(final_hit)
        final_death_time = guard_phase_time + 5 * 10_000_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": final_death_time,
            }
        )

        # The real settlement sequence leaves a live guard on screen and sends
        # one final in-flight HP drop after the primary Boss death. Neither may
        # reopen the encounter or lower the frozen DPS result.
        model.ingest_monster(
            {
                "entity_id": SECOND_MONSTER_ID,
                "current_hp": 941_056,
                "filetime_100ns": final_death_time + 400_000,
            }
        )
        damage_before_cleanup = model.stats[SELF_ID].damage
        cleanup_hit = damage(4, SELF_ID, SECOND_MONSTER_ID, 10_667)
        cleanup_hit["filetime_100ns"] = final_death_time + 410_000
        model.ingest(cleanup_hit)

        frozen_duration = model.duration(model.last_damage_time + 1)
        self.assertFalse(model.active(model.last_damage_time + 1))
        self.assertEqual(model.stats[SELF_ID].damage, damage_before_cleanup)
        self.assertEqual(
            frozen_duration,
            model.duration(model.last_damage_time + model.encounter_gap - 0.1),
        )
        self.assertFalse(
            model.finalize_if_idle(
                model.last_damage_time + model.encounter_gap - 0.1
            )
        )
        self.assertTrue(
            model.finalize_if_idle(model.last_damage_time + model.encounter_gap)
        )
        record = model.pop_completed_combats()[0]
        self.assertEqual(record["archive_reason"], "target_defeated")
        self.assertEqual(record["duration_seconds"], frozen_duration)

    def test_large_full_hp_candidate_waits_for_all_damage_actors_to_leave_combat(self):
        model = CombatModel(run_id="boss-reset-candidate-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "template_id": 7_102_403,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 30_000_000,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 100_000))
        for entity_id in (SELF_ID, TEAMMATE_ID, MONSTER_ID):
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                }
            )

        # A normal top-up during active combat is not a new pull.
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME + 4 * 10_000,
            }
        )
        self.assertFalse(model.combat_end_time)

        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 2_000_000,
                "filetime_100ns": BASE_FILETIME + 5 * 10_000,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "reset_candidate_hp": 33_270_350,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME + 6 * 10_000,
            }
        )
        self.assertFalse(model.combat_end_time)
        self.assertTrue(model.boss_reset_pending_100ns)

        for sequence, entity_id in enumerate(
            (MONSTER_ID, SELF_ID), start=7
        ):
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": False,
                    "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                }
            )
        self.assertFalse(model.combat_end_time)
        model.ingest_combat_state(
            {
                "entity_id": TEAMMATE_ID,
                "in_combat": False,
                "filetime_100ns": BASE_FILETIME + 9 * 10_000,
            }
        )
        self.assertEqual(model.combat_end_reason, "target_reset")
        self.assertEqual(model.combat_end_time, model.last_damage_time)

    def test_damage_after_full_heal_cancels_unconfirmed_reset_and_stays_live(self):
        model = CombatModel(run_id="boss-heal-continues-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 10_000_000,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "reset_candidate_hp": 33_270_350,
                "max_hp": 33_270_350,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            }
        )
        self.assertTrue(model.boss_reset_pending_100ns)
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 50_000))
        self.assertFalse(model.boss_reset_pending_100ns)
        self.assertFalse(model.combat_end_time)
        self.assertEqual(model.stats[SELF_ID].damage, 150_000)

    def test_duel_npc_damage_cannot_extend_or_block_target_reset(self):
        model = CombatModel(run_id="duel-npc-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伯德温·威瑟尔",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 36_409_062,
                "max_hp": 36_409_062,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 2_000_000))
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 20_000_000,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            }
        )
        last_player_damage = model.last_damage_time
        npc_event = damage(3, SECOND_MONSTER_ID, MONSTER_ID, 3_798)
        npc_event["filetime_100ns"] = BASE_FILETIME + 11 * 10_000_000
        npc_event["player_attacker"] = False
        model.ingest(npc_event)

        self.assertEqual(model.last_damage_time, last_player_damage)
        self.assertNotIn(SECOND_MONSTER_ID, model.stats)
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 36_405_982,
                "filetime_100ns": BASE_FILETIME + 12 * 10_000_000,
            }
        )
        self.assertFalse(model.combat_end_time)
        self.assertTrue(model.boss_reset_pending_100ns)
        self.assertTrue(
            model.finalize_if_idle(last_player_damage + model.idle_gap + 1.0)
        )
        self.assertEqual(model.combat_end_reason, "target_reset")
        self.assertEqual(model.combat_end_time, last_player_damage)

    def test_early_wipe_at_high_boss_hp_separates_two_pulls(self):
        model = CombatModel(run_id="early-boss-reset-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "先祖铠甲",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 34_902_480,
                "max_hp": 34_902_480,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_606_024))
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 33_296_456,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000_000,
            }
        )
        reset_time = int(
            (model.last_damage_time + model.idle_gap + 1.0) * 10_000_000
            + 116_444_736_000_000_000
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 34_902_480,
                "filetime_100ns": reset_time,
            }
        )
        self.assertFalse(model.combat_end_time)
        self.assertTrue(model.boss_reset_pending_100ns)
        self.assertTrue(
            model.finalize_if_idle(model.last_damage_time + model.idle_gap + 1.0)
        )
        self.assertEqual(model.combat_end_time, model.last_damage_time)
        self.assertEqual(model.combat_end_reason, "target_reset")

        # The first pull's stage summary can arrive after the HP reset. It may
        # refine that completed pull, but must never be carried into pull two.
        model.ingest_stage_summary(
            {
                "summary_id": "first-pull",
                "filetime_100ns": reset_time + 10_000,
                "member_count": 1,
                "actors": [
                    {
                        "actor_id": SELF_ID,
                        "damage": 1_606_024,
                        "skills": [],
                    }
                ],
            }
        )
        next_pull = damage(2, TEAMMATE_ID, MONSTER_ID, 3_770_046)
        next_pull["filetime_100ns"] = reset_time + 20_000
        model.ingest(next_pull)

        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["archive_reason"], "target_reset")
        self.assertEqual(history[0]["total_damage"], 1_606_024)
        self.assertEqual(set(model.stats), {TEAMMATE_ID})
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 3_770_046)
        self.assertEqual(model.build_combat_record()["total_damage"], 3_770_046)

    def test_scene_change_archives_and_clears_previous_damage(self):
        model = CombatModel(run_id="scene-change-test")
        model.ingest_scene({"scene_id": 5_200_001})
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 99_296))
        self.assertEqual(model.stats[SELF_ID].damage, 99_296)

        self.assertTrue(model.ingest_scene({"scene_id": 5_200_002}))
        self.assertEqual(model.stats, {})
        self.assertEqual(model.events, [])
        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_damage"], 99_296)

    def test_space_transition_clears_stale_dummy_before_new_boss(self):
        model = CombatModel(run_id="space-transition-test")
        model.ingest_scene({"scene_id": 5_200_002})
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "伤害木桩",
                "entity_type": "Boss",
                "template_id": 7_114_223,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 1_000_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 900_000,
                "filetime_100ns": BASE_FILETIME + 10_000,
            }
        )
        self.assertEqual(model.combat_target_id, MONSTER_ID)

        self.assertTrue(
            model.ingest_scene(
                {
                    "scene_id": 0,
                    "previous_scene_id": 5_200_002,
                    "force_reset": True,
                    "transition": True,
                    "entity_ids": [],
                }
            )
        )
        self.assertIsNone(model.scene_id)
        self.assertIsNone(model.combat_target_id)
        self.assertEqual(model.monsters, {})

        boss_id = MONSTER_ID + 1
        model.ingest_profile(
            {
                "entity_id": boss_id,
                "name": "小丑",
                "entity_type": "Boss",
                "template_id": 7_102_873,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": boss_id,
                "current_hp": 5_000_000,
                "max_hp": 5_000_000,
                "filetime_100ns": BASE_FILETIME + 30_000,
            }
        )
        for sequence, actor_id in ((4, SELF_ID), (5, TEAMMATE_ID)):
            baseline = team_stat(sequence, actor_id, 0)
            baseline["full_snapshot"] = True
            baseline["omitted_zero"] = True
            model.ingest_team_stat(baseline)
        model.ingest_monster(
            {
                "entity_id": boss_id,
                "current_hp": 4_900_000,
                "filetime_100ns": BASE_FILETIME + 60_000,
            }
        )
        for sequence, actor_id, amount in (
            (7, SELF_ID, 6_222),
            (8, TEAMMATE_ID, 2_044_958),
        ):
            final = team_stat(sequence, actor_id, amount)
            final["full_snapshot"] = True
            model.ingest_team_stat(final)

        self.assertEqual(model.combat_target_id, boss_id)
        self.assertEqual(model.current_monster().name, "小丑")
        self.assertEqual(model.stats[SELF_ID].damage, 6_222)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 2_044_958)
        self.assertEqual(
            sum(actor.damage for actor in model.current_stats()),
            2_051_180,
        )
        self.assertTrue(
            all(
                state.baseline_absolute == 0
                for state in model.team_damage_states.values()
                if state.accepted_damage > 0
            )
        )

    def test_same_scene_respawn_refresh_preserves_visible_boss(self):
        model = CombatModel(run_id="same-scene-respawn-test")
        model.ingest_scene({"scene_id": 5_200_001})
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 99_296))
        encounter_id = model.encounter_id

        self.assertFalse(
            model.ingest_scene(
                {
                    "scene_id": 5_200_001,
                    "force_reset": True,
                    "entity_ids": [SELF_ID, MONSTER_ID],
                }
            )
        )
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats[SELF_ID].damage, 99_296)

        self.assertTrue(
            model.ingest_scene(
                {
                    "scene_id": 5_200_001,
                    "force_reset": True,
                    "entity_ids": [SELF_ID],
                }
            )
        )
        self.assertEqual(model.stats, {})


if __name__ == "__main__":
    unittest.main()
