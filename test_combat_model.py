#!/usr/bin/env python3

from __future__ import annotations

import runpy
import json
import tempfile
import unittest
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).with_name("dps_meter.pyw")))
CombatModel = MODULE["CombatModel"]
DpsWindow = MODULE["DpsWindow"]
HookWorker = MODULE["HookWorker"]
NetworkPacketParser = MODULE["NetworkPacketParser"]
APP_VERSION = MODULE["APP_VERSION"]
CLIENT_BUILD = MODULE["CLIENT_BUILD"]
boss_name_is_allowed = MODULE["boss_name_is_allowed"]
load_boss_name_allowlist = MODULE["load_boss_name_allowlist"]
load_monster_catalog = MODULE["load_monster_catalog"]
load_skill_catalog = MODULE["load_skill_catalog"]
load_skill_metadata = MODULE["load_skill_metadata"]
resolve_program_path = MODULE["resolve_program_path"]
window_exstyle_for_lock = MODULE["window_exstyle_for_lock"]

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


def team_stat(sequence: int, actor: int, absolute_damage: int) -> dict:
    return {
        "filetime_100ns": BASE_FILETIME + sequence * 10_000,
        "actor_id": actor,
        "absolute_damage": absolute_damage,
    }


class CombatModelTests(unittest.TestCase):
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

        # Native template metadata can trail the first damage record. Before
        # that metadata arrives, the guard's player-range skill looks valid.
        mistaken_guard_hit = damage(2, guard_id, guard_id, 9_148)
        model.ingest(mistaken_guard_hit)
        model.ingest(damage(3, SELF_ID, guard_id, 50_000))
        self.assertIn(guard_id, model.stats)
        self.assertEqual(model.stats[guard_id].damage, 9_148)

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
        self.assertIn("7102873", catalog)
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

    def test_explicit_boss_death_freezes_encounter_and_ignores_late_team_total(self):
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
        total_before = model.stats[SELF_ID].damage
        self.assertFalse(model.ingest_team_stat(team_stat(6, SELF_ID, 900_000)))
        self.assertEqual(model.stats[SELF_ID].damage, total_before)

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

    def test_v007_keeps_feedback_update_lock_and_fixed_target_scope(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertEqual(APP_VERSION, "0.0.8")
        self.assertEqual(CLIENT_BUILD, "0.0.8+20260829.1")
        self.assertNotIn("toggle_boss_only", source)
        self.assertNotIn('self.footer, "只读 BOSS"', source)
        self.assertIn('"反馈",\n            self.show_feedback', source)
        self.assertIn('"更新",\n            lambda: self._start_update_check(manual=True)', source)
        self.assertIn("UPDATE_DIR = APP_DIR", source)
        self.assertIn('text=f"保存位置：{UPDATE_DIR}"', source)
        self.assertIn('text="附带运行摘要"', source)
        self.assertNotIn("load_recent_restart_context", source)
        self.assertNotIn("restart_target_profiles", source)
        self.assertIn("entity_names={}", source)
        self.assertIn("self.lock_button = self._label_button(", source)
        self.assertIn('tags=("compact_lock", "compact_lock_bg")', source)
        self.assertIn("self._set_window_click_through(self.root, self.window_locked)", source)
        self.assertIn('self.config["window_locked"] = self.window_locked', source)
        self.assertNotIn("不包含完整封包", source)
        self.assertNotIn("分享功能将在后续版本开放", source)
        self.assertIn('actions, "分享", self._share_current', source)
        self.assertNotIn("clear_if_expired", source)
        self.assertIn('text="暴击率"', source)
        self.assertIn('text="死亡"', source)
        self.assertIn("critical_x, top + 17", source)
        self.assertIn("deaths_x, top + 17", source)

    def test_history_share_copies_name_and_dps_within_110_characters(self):
        record = {
            "duration_seconds": 10,
            "participants": [
                {"name": "莫雪", "damage": 125_000, "dps": 12_500},
                {"name": "队友甲", "damage": 134_000, "dps": 13_400},
            ],
        }
        self.assertEqual(
            DpsWindow._history_share_text(record),
            "莫雪:1.2w 队友:1.3w",
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
            "玩家1:1.2w 玩家2:1.3w",
        )

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
                worker = HookWorker(
                    MODULE["queue"].Queue(), MODULE["threading"].Event()
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

    def test_team_snapshot_calibrates_old_hits_while_new_hits_remain_realtime(self):
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
        self.assertEqual(
            sum(model.stats[TEAMMATE_ID].target_damage.values()),
            200,
        )
        self.assertEqual(
            sum(skill.damage for skill in model.stats[TEAMMATE_ID].skills.values()),
            200,
        )
        self.assertTrue(model.combat_in_progress(model.last_damage_time))
        self.assertFalse(
            model.combat_in_progress(model.last_damage_time + model.idle_gap + 0.1)
        )

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
            "团队伤害汇总",
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

    def test_final_settlement_replaces_hp_inference_and_rewrites_same_encounter(self):
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
                for actor_id, amount in zip(actor_ids, exact)
                if amount
            },
        )
        corrected = model.pop_completed_combats()
        self.assertEqual(len(corrected), 1)
        self.assertEqual(corrected[0]["encounter_id"], encounter_id)
        self.assertEqual(corrected[0]["total_damage"], 2_594_381)
        self.assertEqual(
            [(row["name"], row["damage"]) for row in corrected[0]["participants"]],
            [
                ("知幻", 763_225),
                ("綠鱼", 660_893),
                ("杨再兴", 593_997),
                ("恋缘", 415_704),
                ("汐序", 160_562),
            ],
        )

    def test_multiphase_settlement_ends_and_archives_exact_result(self):
        """FBE64D87D66CCBE07C: settlement ends Astrologer explicitly."""
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
        self.assertEqual(model.combat_end_reason, "settlement")
        self.assertEqual(model.combat_end_time, realtime_end)
        self.assertEqual(sum(row.damage for row in model.stats.values()), 350_000)
        record = model.pop_completed_combats()[0]
        self.assertEqual(record["archive_reason"], "settlement")
        self.assertEqual(record["total_damage"], 350_000)

    def test_settlement_keeps_boss_add_split_critical_rate_and_death_count(self):
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
        self.assertEqual(self_row["damage"], 2_000)
        self.assertEqual(self_row["damage_hits"], 20)
        self.assertEqual(self_row["critical_hits"], 8)
        self.assertEqual(self_row["critical_rate"], 0.4)
        self.assertEqual(self_row["deaths"], 3)
        self.assertEqual(participants[TEAMMATE_ID]["deaths"], 2)
        self.assertEqual(
            [
                (row["kind"], row["name"], row["damage"], row["share"])
                for row in self_row["targets"]
            ],
            [
                ("Boss", "朗伯·绞索", 1_200, 0.6),
                ("小怪", "冰牢", 800, 0.4),
            ],
        )
        teammate_targets = participants[TEAMMATE_ID]["targets"]
        self.assertEqual(
            [(row["name"], row["damage"]) for row in teammate_targets],
            [("朗伯·绞索", 800), ("冰牢", 200)],
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

    def test_astrologer_full_hp_reset_does_not_wait_for_the_add_phase_gap(self):
        """FBB286B655A129BAC3: a full Boss refill ends the wiped pull."""
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
                "current_hp": 29_375_701,
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
                "current_hp": 33_270_350,
                "filetime_100ns": reset_time,
            }
        )

        self.assertFalse(model.combat_end_time)
        self.assertTrue(model.boss_reset_pending_100ns)
        self.assertTrue(
            model.finalize_if_idle(model.last_damage_time + model.idle_gap + 1.0)
        )
        self.assertEqual(model.combat_end_reason, "target_reset")
        self.assertEqual(model.combat_end_time, model.last_damage_time)

        next_pull = damage(2, TEAMMATE_ID, MONSTER_ID, 250_000)
        next_pull["filetime_100ns"] = reset_time + 10_000
        model.ingest(next_pull)
        self.assertEqual(set(model.stats), {TEAMMATE_ID})
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)
        history = model.pop_completed_combats()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["archive_reason"], "target_reset")

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
