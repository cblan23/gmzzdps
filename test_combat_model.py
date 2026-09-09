#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import runpy
import json
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image
from history_index import build_history_summary


WINDOWS_TRAY_MODULE = (
    __import__("windows_tray") if sys.platform == "win32" else None
)

MODULE = runpy.run_path(str(Path(__file__).with_name("dps_meter.pyw")))
CombatModel = MODULE["CombatModel"]
DpsWindow = MODULE["DpsWindow"]
ActorStats = MODULE["ActorStats"]
MonsterStats = MODULE["MonsterStats"]
enrage_marker_row_index = MODULE["enrage_marker_row_index"]
enrage_marker_ratio = MODULE["enrage_marker_ratio"]
TeamDamageState = MODULE["TeamDamageState"]
TeamTakenState = MODULE["TeamTakenState"]
TeamHealingState = MODULE["TeamHealingState"]
IconFactory = MODULE["IconFactory"]
ModernDropdown = MODULE["ModernDropdown"]
ModernPagination = MODULE["ModernPagination"]
render_switch_control_image = MODULE["render_switch_control_image"]
HookWorker = MODULE["HookWorker"]
NetworkPacketParser = MODULE["NetworkPacketParser"]
UpdateInfo = MODULE["UpdateInfo"]
TrialClaim = MODULE["TrialClaim"]
CombatClockResult = MODULE["CombatClockResult"]
CombatClockWorker = MODULE["CombatClockWorker"]
combat_clock_sync_interval = MODULE["combat_clock_sync_interval"]
ResolvedCombatInterval = MODULE["ResolvedCombatInterval"]
apply_combat_clock_to_record = MODULE["apply_combat_clock_to_record"]
authoritative_team_combat_seconds = MODULE[
    "authoritative_team_combat_seconds"
]
APP_VERSION = MODULE["APP_VERSION"]
CLIENT_BUILD = MODULE["CLIENT_BUILD"]
TEAM_RATING_PREVIEW_AVAILABLE = MODULE["TEAM_RATING_PREVIEW_AVAILABLE"]
MAIN_MIN_HEIGHT = MODULE["MAIN_MIN_HEIGHT"]
MAIN_SUMMARY_BASE_HEIGHT = MODULE["MAIN_SUMMARY_BASE_HEIGHT"]
MONSTER_HP_ROW_HEIGHT = MODULE["MONSTER_HP_ROW_HEIGHT"]
HISTORY_DETAIL_MIN_CONTENT_HEIGHT = MODULE[
    "HISTORY_DETAIL_MIN_CONTENT_HEIGHT"
]
HISTORY_DETAIL_LOCAL_MODULES_HEIGHT = MODULE[
    "HISTORY_DETAIL_LOCAL_MODULES_HEIGHT"
]
DPS_BAR_COLOR_STRENGTH = MODULE["DPS_BAR_COLOR_STRENGTH"]
PROFESSION_COLORS = MODULE["PROFESSION_COLORS"]
BG = MODULE["BG"]
ACCENT = MODULE["ACCENT"]
boss_name_is_allowed = MODULE["boss_name_is_allowed"]
load_boss_name_allowlist = MODULE["load_boss_name_allowlist"]
load_monster_catalog = MODULE["load_monster_catalog"]
load_target_identity_catalog = MODULE["load_target_identity_catalog"]
load_skill_catalog = MODULE["load_skill_catalog"]
load_skill_metadata = MODULE["load_skill_metadata"]
restore_history_boss_names = MODULE["restore_history_boss_names"]
restore_history_skill_names = MODULE["restore_history_skill_names"]
resolve_program_path = MODULE["resolve_program_path"]
update_install_paths = MODULE["update_install_paths"]
window_exstyle_for_lock = MODULE["window_exstyle_for_lock"]
window_exstyle_for_taskbar = MODULE["window_exstyle_for_taskbar"]
normalize_toggle_hotkey = MODULE["normalize_toggle_hotkey"]
normalize_history_visible_fields = MODULE["normalize_history_visible_fields"]
toggle_hotkey_windows_parameters = MODULE["toggle_hotkey_windows_parameters"]
toggle_hotkey_from_tk_event = MODULE["toggle_hotkey_from_tk_event"]
membership_label_for_card_tier = MODULE["membership_label_for_card_tier"]
membership_badge_for_card_tier = MODULE["membership_badge_for_card_tier"]
membership_availability_text = MODULE["membership_availability_text"]
membership_contract_text = MODULE["membership_contract_text"]
format_duration = MODULE["format_duration"]
format_response_time = MODULE["format_response_time"]
format_overheal_rate = MODULE["format_overheal_rate"]
format_team_health_number = MODULE["format_team_health_number"]
format_team_health_percent = MODULE["format_team_health_percent"]
normalize_extraordinary_rating = MODULE["normalize_extraordinary_rating"]
format_extraordinary_rating = MODULE["format_extraordinary_rating"]
team_average_extraordinary_rating = MODULE[
    "team_average_extraordinary_rating"
]
healing_coverage_label = MODULE["healing_coverage_label"]
dps_duration_seconds = MODULE["dps_duration_seconds"]
relative_damage_bar_ratio = MODULE["relative_damage_bar_ratio"]
compact_width_for_visible_metrics = MODULE["compact_width_for_visible_metrics"]
clamp_geometry_to_work_areas = MODULE["clamp_geometry_to_work_areas"]
format_absolute_tk_position = MODULE["format_absolute_tk_position"]
format_absolute_tk_geometry = MODULE["format_absolute_tk_geometry"]
clamp_popup_position = MODULE["clamp_popup_position"]
help_popup_position = MODULE["help_popup_position"]
dropdown_popup_bounds = MODULE["dropdown_popup_bounds"]
clipped_overlay_bounds = MODULE["clipped_overlay_bounds"]
parse_absolute_tk_geometry = MODULE["parse_absolute_tk_geometry"]
login_window_dimensions = MODULE["login_window_dimensions"]
HEALING_TARGET_TEMPLATE_IDS = MODULE["HEALING_TARGET_TEMPLATE_IDS"]
main = MODULE["main"]

SELF_ID = 57_266_949_828_970
TEAMMATE_ID = 57_266_949_828_971
ZERO_TEAMMATE_ID = -645_797_907_948
NEARBY_ID = 57_266_949_828_999
MONSTER_ID = 57_236_882_400_409
SECOND_MONSTER_ID = 57_236_882_400_410
THIRD_MONSTER_ID = 57_236_882_400_411
HEALING_DUMMY_ID = 57_236_882_400_412
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
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
                "user_tokens": ["clock-self", "clock-teammate"],
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 800_000))
        model.ingest(damage(15_001, TEAMMATE_ID, MONSTER_ID, 1_200_000))
        return model

    def test_enrage_countdown_edge_is_encounter_scoped(self):
        model = CombatModel(run_id="enrage-phase-edge")
        payload = {
            "signal": "Set_MUS_B_WYZY_Boss_GuanJia_Stage2",
            "filetime_100ns": BASE_FILETIME,
        }
        self.assertTrue(model.ingest_enrage_countdown(payload))
        self.assertFalse(model.ingest_enrage_countdown(dict(payload)))
        self.assertEqual(
            model.enrage_countdown_signal,
            "Set_MUS_B_WYZY_Boss_GuanJia_Stage2",
        )
        self.assertEqual(model.enrage_countdown_start_100ns, BASE_FILETIME)

        model.reset(keep_identity=True, archive_reason="manual_reset")
        self.assertEqual(model.enrage_countdown_signal, "")
        self.assertEqual(model.enrage_countdown_start_100ns, 0)

    def test_local_cast_log_deduplicates_packets_but_keeps_repeat_casts(self):
        model = CombatModel(run_id="local-cast-log")
        started_at = 1_000.0

        def cast(milliseconds: int, sequence: int, actor_id: int = 0) -> dict:
            epoch = started_at + milliseconds / 1000.0
            return {
                "filetime_100ns": int(
                    (epoch + 11_644_473_600.0) * 10_000_000
                ),
                "sequence": sequence,
                "actor_id": actor_id,
                "skill_id": 86_021_030,
                "source_method": "RetCastSkillSuccessNew",
                "cast_source": "local_success_response",
            }

        first = cast(500, 7)
        self.assertTrue(model.ingest_skill_cast(first))
        self.assertFalse(model.ingest_skill_cast(dict(first)))
        self.assertTrue(model.ingest_skill_cast(cast(1_200, 8)))
        self.assertTrue(model.ingest_skill_cast(cast(500, 7, SELF_ID)))
        model.ingest_identity({"entity_id": SELF_ID})

        cast_log = model._history_skill_cast_log(started_at, 60.0)

        self.assertEqual(
            cast_log["columns"],
            ["time_ms", "actor_id", "skill_id", "sequence"],
        )
        self.assertEqual(
            cast_log["rows"],
            [
                [500, SELF_ID, 86_021_030, 7],
                [1200, SELF_ID, 86_021_030, 8],
            ],
        )

    def test_local_cast_channel_does_not_change_combat_totals_or_clocks(self):
        model = self._clock_ready_model("cast-side-channel")
        before = (
            model.first_damage_time,
            model.last_damage_time,
            model.combat_target_id,
            {actor_id: actor.damage for actor_id, actor in model.stats.items()},
        )
        cast_timestamp = BASE_FILETIME + 5_000 * 10_000

        self.assertTrue(
            model.ingest_skill_cast(
                {
                    "filetime_100ns": cast_timestamp,
                    "sequence": 5_000,
                    "actor_id": SELF_ID,
                    "skill_id": 86_021_030,
                    "source_method": "RetCastSkillSuccessNew",
                    "cast_source": "local_success_response",
                }
            )
        )

        after = (
            model.first_damage_time,
            model.last_damage_time,
            model.combat_target_id,
            {actor_id: actor.damage for actor_id, actor in model.stats.items()},
        )
        self.assertEqual(after, before)
        record = model.build_combat_record("test")
        self.assertEqual(
            record["skill_cast_log"]["rows"],
            [[4_999, SELF_ID, 86_021_030, 5_000]],
        )

    def test_history_opener_uses_only_exact_first_thirty_second_cast_rows(self):
        record = {
            "event_log": {
                "columns": ["time_ms", "actor_id", "skill_id", "damage"],
                "rows": [[250, SELF_ID, 86_021_030, 999]],
            },
            "skill_cast_log": {
                "coverage": "local_successful_active_casts",
                "columns": ["time_ms", "actor_id", "skill_id", "sequence"],
                "rows": [
                    [0, SELF_ID, 86_021_030, 1],
                    [900, SELF_ID, 86_021_030, 2],
                    [30_000, SELF_ID, 86_030_040, 3],
                    [30_001, SELF_ID, 86_021_030, 4],
                    [500, TEAMMATE_ID, 86_021_030, 5],
                ],
            },
        }

        opener = DpsWindow._history_opener_events(record, SELF_ID)

        self.assertEqual([row["time_ms"] for row in opener], [0, 900, 30_000])
        self.assertEqual([row["ordinal"] for row in opener], [1, 2, 1])
        self.assertEqual([row["interval_ms"] for row in opener], [None, 900, 29_100])
        provisional_record = {
            "skill_cast_log": {
                **record["skill_cast_log"],
                "rows": [[0, ZERO_TEAMMATE_ID, 86_021_030, 1]],
            }
        }
        self.assertEqual(
            len(DpsWindow._history_opener_events(provisional_record, ZERO_TEAMMATE_ID)),
            1,
        )
        self.assertEqual(
            DpsWindow._history_opener_events(
                {"event_log": record["event_log"]}, SELF_ID
            ),
            [],
        )

    def test_new_encounter_reset_keeps_only_casts_after_previous_end(self):
        model = CombatModel(run_id="carry-opening-cast")
        model.ingest_identity({"entity_id": SELF_ID})
        previous_end = 1_000.0
        model.last_damage_time = previous_end

        def cast(epoch: float, sequence: int) -> dict:
            return {
                "filetime_100ns": int(
                    (epoch + 11_644_473_600.0) * 10_000_000
                ),
                "sequence": sequence,
                "actor_id": SELF_ID,
                "skill_id": 86_021_030,
                "source_method": "RetCastSkillSuccessNew",
                "cast_source": "local_success_response",
            }

        model.ingest_skill_cast(cast(previous_end - 1.0, 1))
        model.ingest_skill_cast(cast(previous_end + 1.0, 2))

        model.reset(
            archive_reason="new_encounter",
            archive_current_record=False,
        )

        self.assertEqual(
            [event["sequence"] for event in model.skill_cast_events], [2]
        )
        model.reset(
            archive_reason="manual_reset",
            archive_current_record=False,
        )
        self.assertEqual(model.skill_cast_events, [])

    def test_opener_click_focuses_real_cast_time_and_matching_skill_page(self):
        window = object.__new__(DpsWindow)
        window.history_skill_timeline_page = 0
        window.history_skill_timeline_selected_hit = (1, 2)
        window.history_skill_timeline_hover_hit = (1, 2)
        window.history_page_canvas = None
        window._history_skill_timeline_payload = lambda: (
            {},
            {},
            [{"skill_id": value} for value in range(100, 108)],
            {},
            100.0,
        )
        window._draw_history_skill_timeline = mock.Mock()

        result = window._focus_history_skill_timeline_from_cast(
            {"time_seconds": 24.0, "skill_id": 107}
        )

        self.assertEqual(result, "break")
        self.assertEqual(window.history_skill_timeline_page, 1)
        self.assertEqual(window.history_skill_timeline_focus_time, 24.0)
        self.assertIsNone(window.history_skill_timeline_selected_hit)
        self.assertAlmostEqual(window.history_skill_timeline_range[0], 19.0 / 100.0)
        self.assertAlmostEqual(window.history_skill_timeline_range[1], 49.0 / 100.0)
        window._draw_history_skill_timeline.assert_called_once_with()

    def test_combat_clock_identity_is_shared_without_uploading_entity_ids(self):
        first = self._clock_ready_model("clock-first")
        second = self._clock_ready_model("clock-second")

        first_snapshot = first.combat_clock_snapshot(first.last_damage_time + 2.0)
        second_snapshot = second.combat_clock_snapshot(second.last_damage_time + 3.0)

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
        self.assertNotIn("clock-self", serialized)
        self.assertNotIn("clock-teammate", serialized)

    def test_combat_clock_party_identity_survives_client_actor_id_differences(self):
        tokens = ["same-party-player-a", "same-party-player-b"]

        def snapshot(self_id: int, teammate_id: int, run_id: str) -> dict:
            model = CombatModel(run_id=run_id)
            model.ingest_identity({"entity_id": self_id})
            model.ingest_party(
                {
                    "entity_ids": [teammate_id],
                    "member_count": 2,
                    "authoritative": True,
                    "user_tokens": tokens,
                }
            )
            model.ingest_profile(
                {
                    "entity_id": MONSTER_ID,
                    "entity_type": "Boss",
                    "boss_rank": 3,
                }
            )
            model.ingest(damage(1, self_id, MONSTER_ID, 100_000))
            result = model.combat_clock_snapshot(model.last_damage_time + 1.0)
            self.assertIsNotNone(result)
            return result

        first = snapshot(SELF_ID, TEAMMATE_ID, "clock-binding-a")
        second = snapshot(SELF_ID + 100, TEAMMATE_ID + 100, "clock-binding-b")

        self.assertEqual(first["party_key"], second["party_key"])
        self.assertEqual(first["target_key"], second["target_key"])

    def test_combat_clock_uses_one_key_for_known_boss_phase_families(self):
        tokens = ["phase-clock-a", "phase-clock-b"]

        def target_key(template_id: int, run_id: str) -> str:
            model = CombatModel(run_id=run_id)
            model.ingest_identity({"entity_id": SELF_ID})
            model.ingest_party(
                {
                    "entity_ids": [TEAMMATE_ID],
                    "member_count": 2,
                    "authoritative": True,
                    "user_tokens": tokens,
                }
            )
            model.ingest_profile(
                {
                    "entity_id": MONSTER_ID,
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
            model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
            snapshot = model.combat_clock_snapshot(
                model.last_damage_time + 1.0
            )
            self.assertIsNotNone(snapshot)
            return str(snapshot["target_key"])

        dual_phase_keys = {
            target_key(template_id, f"dual-family-{template_id}")
            for template_id in (7_100_208, 7_100_209, 7_100_210)
        }
        ancestor_phase_keys = {
            target_key(template_id, f"ancestor-family-{template_id}")
            for template_id in (7_103_402, 7_103_401)
        }
        karl_phase_keys = {
            target_key(template_id, f"karl-family-{template_id}")
            for template_id in (7_107_030, 7_107_081)
        }
        self.assertEqual(len(dual_phase_keys), 1)
        self.assertEqual(len(ancestor_phase_keys), 1)
        self.assertEqual(len(karl_phase_keys), 1)
        self.assertNotEqual(dual_phase_keys, ancestor_phase_keys)
        self.assertNotEqual(ancestor_phase_keys, karl_phase_keys)

    def test_combat_clock_snapshot_reopens_after_provisional_end(self):
        model = self._clock_ready_model("clock-reversible-snapshot")
        total_before = sum(actor.damage for actor in model.stats.values())
        active = model.combat_clock_snapshot(model.last_damage_time + 1.0)
        self.assertEqual(active["state"], "active")

        model.combat_end_time = model.last_damage_time
        model.combat_end_reason = "target_defeated"
        settling = model.combat_clock_snapshot(model.last_damage_time + 2.0)
        self.assertEqual(settling["state"], "settling")
        self.assertGreater(
            settling["client_revision"], active["client_revision"]
        )

        model.combat_end_time = 0.0
        model.combat_end_reason = ""
        reopened = model.combat_clock_snapshot(model.last_damage_time + 3.0)
        self.assertEqual(reopened["state"], "active")
        self.assertGreater(
            reopened["client_revision"], settling["client_revision"]
        )
        final = model.combat_clock_snapshot(
            model.last_damage_time + 4.0,
            state="final",
            end_reason="new_encounter",
        )
        self.assertEqual(final["state"], "final")
        self.assertGreater(final["client_revision"], reopened["client_revision"])
        self.assertEqual(
            sum(actor.damage for actor in model.stats.values()), total_before
        )

    def test_living_wounded_boss_resumes_after_ten_and_sixty_second_gaps(self):
        model = CombatModel(run_id="clock-live-boss-gap")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
                "user_tokens": ["gap-player-a", "gap-player-b"],
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_109_999,
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
                "current_hp": 900_000,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME + 1_000_000,
            }
        )
        encounter_id = model.encounter_id
        settling = model.combat_clock_snapshot(model.last_damage_time + 10.0)
        self.assertEqual(settling["state"], "settling")

        resumed = damage(2, TEAMMATE_ID, MONSTER_ID, 200_000)
        resumed["filetime_100ns"] = BASE_FILETIME + 61 * 10_000_000
        model.ingest(resumed)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(
            sum(actor.damage for actor in model.stats.values()), 300_000
        )
        active = model.combat_clock_snapshot(model.last_damage_time)
        self.assertEqual(active["state"], "active")
        self.assertGreater(
            active["client_revision"], settling["client_revision"]
        )

    def test_combat_clock_worker_accepts_active_above_confirmed_final_revision(self):
        worker = CombatClockWorker(object(), queue.Queue(), threading.Event())
        encounter_id = "worker-recovery-000001"
        base = {
            "encounter_id": encounter_id,
            "party_key": "a" * 64,
            "target_key": "b" * 64,
            "elapsed_seconds": 20.0,
            "end_age_seconds": 0.0,
            "activity_age_seconds": 0.0,
            "total_damage": 1_000,
        }
        worker.submit(
            {**base, "client_revision": 2, "state": "final"}
        )
        worker.confirmed_final_revision[encounter_id] = 2
        worker.pending.pop(encounter_id)
        worker.submit(
            {**base, "client_revision": 3, "state": "active"}
        )
        worker.submit(
            {**base, "client_revision": 2, "state": "final"}
        )
        self.assertEqual(worker.pending[encounter_id]["state"], "active")
        self.assertEqual(
            worker.pending[encounter_id]["client_revision"], 3
        )
        worker.last_sent[encounter_id] = 123.0
        worker.submit(
            {
                **base,
                "client_revision": 4,
                "state": "active",
                "elapsed_seconds": 21.0,
            }
        )
        self.assertEqual(worker.last_sent[encounter_id], 123.0)
        worker.submit(
            {**base, "client_revision": 5, "state": "settling"}
        )
        self.assertNotIn(encounter_id, worker.last_sent)

    def test_combat_clock_worker_reduces_steady_state_server_requests(self):
        opening = {
            "encounter_id": "adaptive-clock-000001",
            "client_revision": 1,
            "state": "active",
            "elapsed_seconds": 3.0,
        }
        steady = {
            **opening,
            "client_revision": 2,
            "elapsed_seconds": 30.0,
        }
        settling = {
            **steady,
            "client_revision": 3,
            "state": "settling",
        }

        self.assertEqual(combat_clock_sync_interval(opening), 1.0)
        self.assertEqual(combat_clock_sync_interval(steady), 8.0)
        self.assertEqual(combat_clock_sync_interval(settling), 2.0)

        worker = CombatClockWorker(object(), queue.Queue(), threading.Event())
        worker.submit(opening)
        with mock.patch.object(time, "monotonic", return_value=100.0):
            self.assertIsNotNone(worker._due_snapshot())
        worker.submit(steady)
        with mock.patch.object(time, "monotonic", return_value=107.9):
            self.assertIsNone(worker._due_snapshot())
        with mock.patch.object(time, "monotonic", return_value=108.0):
            self.assertIsNotNone(worker._due_snapshot())

        # State changes bypass the cadence so the end report is still sent
        # immediately after the Boss settles.
        worker.submit(settling)
        with mock.patch.object(time, "monotonic", return_value=108.01):
            self.assertIsNotNone(worker._due_snapshot())

    def test_higher_server_revision_reopens_model_after_final(self):
        model = self._clock_ready_model("clock-model-recovery")
        snapshot = model.combat_clock_snapshot(model.last_damage_time + 1.0)
        total = sum(actor.damage for actor in model.stats.values())
        shared_start = model.first_damage_time - 5.5
        shared_end = model.last_damage_time + 1.0
        final = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id="d" * 32,
            started_at=shared_start,
            ended_at=shared_end,
            duration_seconds=shared_end - shared_start,
            final=True,
            state="final",
            revision=10,
            client_revision=snapshot["client_revision"],
            party_key=snapshot["party_key"],
            target_key=snapshot["target_key"],
            total_damage=total,
        )
        self.assertTrue(model.apply_combat_clock(final, received_at=shared_end))
        self.assertTrue(model.shared_clock_final)

        active_duration = shared_end - shared_start + 10.0
        reopened = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id=final.clock_id,
            started_at=shared_start,
            duration_seconds=active_duration,
            state="active",
            revision=11,
            client_revision=snapshot["client_revision"] + 1,
            party_key=snapshot["party_key"],
            target_key=snapshot["target_key"],
            total_damage=total,
        )
        self.assertTrue(
            model.apply_combat_clock(
                reopened, received_at=shared_start + active_duration
            )
        )
        self.assertFalse(model.shared_clock_final)
        self.assertEqual(model.shared_clock_state, "active")
        self.assertEqual(sum(actor.damage for actor in model.stats.values()), total)

    def test_shared_clock_unifies_clients_with_five_second_opening_difference(self):
        earliest = self._clock_ready_model("clock-early-observer")
        later = self._clock_ready_model("clock-late-observer")
        later.first_damage_time += 5.5
        early_snapshot = earliest.combat_clock_snapshot(
            earliest.last_damage_time + 1.0
        )
        late_snapshot = later.combat_clock_snapshot(later.last_damage_time + 1.0)
        canonical_start = earliest.first_damage_time
        canonical_end = earliest.last_damage_time + 5.0
        canonical_duration = canonical_end - canonical_start
        total = sum(actor.damage for actor in earliest.stats.values())

        for model, snapshot in (
            (earliest, early_snapshot),
            (later, late_snapshot),
        ):
            result = CombatClockResult(
                synchronized=True,
                encounter_id=model.encounter_id,
                clock_id="e" * 32,
                started_at=canonical_start,
                duration_seconds=canonical_duration,
                state="active",
                revision=5,
                client_revision=snapshot["client_revision"],
                party_key=snapshot["party_key"],
                target_key=snapshot["target_key"],
                total_damage=total,
            )
            self.assertTrue(
                model.apply_combat_clock(result, received_at=canonical_end)
            )
        self.assertAlmostEqual(
            earliest.duration(canonical_end),
            later.duration(canonical_end),
            places=6,
        )

    def test_shared_clock_ignores_large_client_wall_clock_offset(self):
        first = self._clock_ready_model("clock-wall-offset-a")
        second = self._clock_ready_model("clock-wall-offset-b")
        wall_clock_offset = 6 * 60 * 60
        second.first_damage_time += wall_clock_offset
        second.last_damage_time += wall_clock_offset
        canonical_duration = 23.75
        server_started_at = 4_000_000.0

        for index, model in enumerate((first, second)):
            snapshot = model.combat_clock_snapshot(
                model.last_damage_time + 1.0
            )
            local_received_at = model.last_damage_time + 2.0
            result = CombatClockResult(
                synchronized=True,
                encounter_id=model.encounter_id,
                clock_id="7" * 32,
                started_at=server_started_at,
                duration_seconds=canonical_duration,
                server_time=server_started_at + canonical_duration,
                state="active",
                revision=index + 1,
                client_revision=snapshot["client_revision"],
                party_key=snapshot["party_key"],
                target_key=snapshot["target_key"],
                total_damage=sum(row.damage for row in model.stats.values()),
            )
            self.assertTrue(
                model.apply_combat_clock(
                    result,
                    received_at=local_received_at,
                )
            )
            self.assertAlmostEqual(
                model.duration(local_received_at + 3.0),
                canonical_duration + 3.0,
                places=6,
            )

    def test_midfight_start_accepts_earlier_shared_edge_without_changing_total(self):
        model = CombatModel(run_id="clock-midfight-start")
        tokens = ["midfight-clock-a", "midfight-clock-b"]
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
                "user_tokens": tokens,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_100_208,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        first_snapshot_time = BASE_FILETIME + 30 * 10_000_000
        model.ingest_active_boss(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_100_208,
                "filetime_100ns": first_snapshot_time - 10_000,
            }
        )
        model.ingest_team_stat(
            {
                "filetime_100ns": first_snapshot_time,
                "actor_id": TEAMMATE_ID,
                "absolute_damage": 500_000,
                "full_snapshot": True,
                "team_tokens": tokens,
            }
        )
        model.ingest_team_stat(
            {
                "filetime_100ns": first_snapshot_time + 10_000_000,
                "actor_id": TEAMMATE_ID,
                "absolute_damage": 600_000,
                "full_snapshot": True,
                "team_tokens": tokens,
            }
        )
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 600_000)
        local_start = model.first_damage_time
        snapshot = model.combat_clock_snapshot(model.last_damage_time + 1.0)
        shared_start = local_start - 30.0
        shared_end = model.last_damage_time + 1.0
        result = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id="8" * 32,
            started_at=shared_start,
            duration_seconds=shared_end - shared_start,
            state="active",
            revision=3,
            client_revision=snapshot["client_revision"],
            party_key=snapshot["party_key"],
            target_key=snapshot["target_key"],
            total_damage=600_000,
        )
        self.assertTrue(model.apply_combat_clock(result, received_at=shared_end))
        self.assertEqual(model.resolve_combat_interval(shared_end).started_at_epoch, shared_start)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 600_000)

    def test_combat_clock_changes_only_the_duration_divisor(self):
        model = self._clock_ready_model("clock-duration")
        before_damage = {actor_id: row.damage for actor_id, row in model.stats.items()}
        shared_start = model.first_damage_time - 5.5
        active_duration = model.last_damage_time - shared_start
        active = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id="a" * 32,
            started_at=shared_start,
            duration_seconds=active_duration,
            server_time=model.last_damage_time,
        )
        self.assertTrue(
            model.apply_combat_clock(active, received_at=model.last_damage_time)
        )
        self.assertEqual(
            model.duration(model.last_damage_time + 2.0),
            active_duration + 2.0,
        )

        final = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id="a" * 32,
            started_at=shared_start,
            ended_at=shared_start + 20.9,
            duration_seconds=20.9,
            final=True,
            server_time=521.0,
        )
        model.combat_end_time = model.first_damage_time + 20.9
        self.assertTrue(
            model.apply_combat_clock(
                final, received_at=model.last_damage_time + 3.0
            )
        )
        self.assertAlmostEqual(
            model.duration(model.last_damage_time + 100.0), 20.9, places=5
        )
        self.assertEqual(
            before_damage,
            {actor_id: row.damage for actor_id, row in model.stats.items()},
        )

    def test_real_boss_hps_uses_the_same_local_and_server_clock_as_dps(self):
        model = CombatModel(run_id="clock-hps-parity")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000))
        damage_start = model.first_damage_time
        damage_end = model.last_damage_time
        model.ingest_heal(
            healing(
                BASE_FILETIME + 3 * 10_000_000,
                SELF_ID,
                TEAMMATE_ID,
                total=600,
                effective=500,
            )
        )
        model.combat_end_time = damage_end + 20.0

        # Before server synchronization both metrics must use the exact same
        # local interval, even though the first heal arrived later.
        self.assertEqual(model.healing_duration(damage_end + 2.0), model.duration(damage_end + 2.0))
        self.assertEqual(model.healing_active(damage_end + 2.0), model.active(damage_end + 2.0))
        self.assertEqual(model.first_damage_time, damage_start)
        self.assertEqual(model.last_damage_time, damage_end)

        final = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id="c" * 32,
            started_at=damage_start - 5.5,
            ended_at=damage_start - 5.5 + 20.9,
            duration_seconds=20.9,
            final=True,
            server_time=132.0,
        )
        before_damage = {
            actor_id: actor.damage for actor_id, actor in model.stats.items()
        }
        self.assertTrue(model.apply_combat_clock(final, received_at=132.0))
        self.assertAlmostEqual(model.duration(9_999.0), 20.9, places=5)
        self.assertAlmostEqual(
            model.healing_duration(9_999.0), 20.9, places=5
        )
        self.assertFalse(model.active(9_999.0))
        self.assertFalse(model.healing_active(9_999.0))
        self.assertEqual(
            before_damage,
            {actor_id: actor.damage for actor_id, actor in model.stats.items()},
        )

        record = model.build_combat_record("target_defeated")
        self.assertIsNotNone(record)
        self.assertEqual(
            record["started_at_epoch"], record["healing_started_at_epoch"]
        )
        self.assertEqual(record["ended_at_epoch"], record["healing_ended_at_epoch"])
        self.assertEqual(record["dps_duration_seconds"], record["hps_duration_seconds"])
        corrected = apply_combat_clock_to_record(record, final)
        self.assertEqual(
            corrected["started_at_epoch"], corrected["healing_started_at_epoch"]
        )
        self.assertEqual(corrected["ended_at_epoch"], corrected["healing_ended_at_epoch"])
        self.assertEqual(
            corrected["dps_duration_seconds"], corrected["hps_duration_seconds"]
        )
        self.assertEqual(
            corrected["duration_seconds"],
            corrected["ended_at_epoch"] - corrected["started_at_epoch"],
        )

    def test_treatment_dummy_hps_is_independent_without_starting_dps(self):
        model = CombatModel(run_id="healing-dummy-only")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        template_id = next(iter(HEALING_TARGET_TEMPLATE_IDS))
        self.assertTrue(
            model.ingest_profile(
                {
                    "entity_id": HEALING_DUMMY_ID,
                    "template_id": template_id,
                    "entity_type": "TrainingDummy",
                    "healing_target": True,
                    "level": 25,
                }
            )
        )
        self.assertTrue(
            model.ingest_heal(
                {
                    **healing(
                        BASE_FILETIME + 5 * 10_000_000,
                        SELF_ID,
                        HEALING_DUMMY_ID,
                        total=1_000,
                        effective=900,
                    ),
                    "target_template_id": template_id,
                }
            )
        )
        self.assertEqual(model.first_damage_time, 0.0)
        self.assertIsNone(model.combat_target_id)
        self.assertEqual(model.current_stats(), [])
        self.assertEqual(model.healing_duration(model.last_healing_time + 11.0), 1.0)

        self.assertTrue(model.finalize_if_idle(model.last_healing_time + 11.0))
        record = model.pop_completed_combats()[0]
        self.assertEqual(record["target_filter"], "healing_dummy")
        self.assertEqual(record["total_damage"], 0)
        self.assertEqual(record["team_dps"], 0.0)
        self.assertGreater(record["team_effective_healing"], 0)
        self.assertEqual(record["dps_duration_seconds"], 0.0)
        self.assertEqual(record["hps_duration_seconds"], 1.0)
        self.assertEqual(record["targets"][0]["template_id"], template_id)

    def test_treatment_dummy_generic_profile_cannot_promote_it_to_boss(self):
        model = CombatModel(run_id="healing-dummy-profile-guard")
        template_id = next(iter(HEALING_TARGET_TEMPLATE_IDS))
        model.ingest_profile(
            {
                "entity_id": HEALING_DUMMY_ID,
                "template_id": template_id,
                "entity_type": "TrainingDummy",
                "healing_target": True,
                "level": 25,
            }
        )
        model.ingest_profile(
            {
                "entity_id": HEALING_DUMMY_ID,
                "template_id": 999_999,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
                "level": 99,
                "name": "stale profile",
            }
        )
        monster = model.monsters[HEALING_DUMMY_ID]
        self.assertTrue(monster.healing_target)
        self.assertEqual(monster.template_id, template_id)
        self.assertEqual(monster.entity_type, "TrainingDummy")
        self.assertEqual(monster.boss_type, 0)
        self.assertEqual(monster.boss_rank, 0)
        self.assertEqual(monster.level, 25)
        self.assertFalse(model._is_priority_target(HEALING_DUMMY_ID))

    def test_unknown_target_healing_is_rejected_without_exact_dummy_identity(self):
        model = CombatModel(run_id="healing-target-guard")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        event = healing(
            BASE_FILETIME + 2 * 10_000_000,
            SELF_ID,
            HEALING_DUMMY_ID,
            total=500,
            effective=500,
        )
        event["target_template_id"] = 123456
        self.assertFalse(model.ingest_heal(event))
        self.assertEqual(model.healing_events, [])
        self.assertEqual(model.first_healing_time, 0.0)

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
        self.assertEqual(
            updated["duration_seconds"],
            updated["ended_at_epoch"] - updated["started_at_epoch"],
        )

    def test_game_server_team_clock_uses_upper_consensus_or_longest_member(self):
        duration, policy, values = authoritative_team_combat_seconds(
            [
                {"combat_seconds_total": value}
                for value in (191, 190, 190, 190, 149)
            ]
        )
        self.assertEqual(duration, 190.0)
        self.assertEqual(policy, "upper_team_consensus_total")
        self.assertEqual(values, [191, 190, 190, 190, 149])

        duration, policy, _values = authoritative_team_combat_seconds(
            [
                {"combat_seconds_total": value}
                for value in (202, 231, 229, 159, 180, 213)
            ]
        )
        self.assertEqual(duration, 231.0)
        self.assertEqual(policy, "maximum_member_total")

        duration, policy, values = authoritative_team_combat_seconds(
            [
                {
                    "combat_seconds_total": total,
                    "combat_seconds_delta": delta,
                }
                for total, delta in ((231, 17), (229, 16), (213, 14))
            ]
        )
        self.assertEqual(duration, 231.0)
        self.assertEqual(policy, "maximum_member_total")
        self.assertEqual(values, [231, 229, 213])

    def test_shared_clock_preserves_all_raw_teamstats_and_detail_fields(self):
        record = {
            "encounter_id": "clock-raw-invariants-000001",
            "started_at_epoch": 1_000.0,
            "ended_at_epoch": 1_245.0,
            "duration_seconds": 245.0,
            "dps_duration_seconds": 245.0,
            "hps_duration_seconds": 245.0,
            "duration_source": "local_network_events",
            "local_event_interval": {
                "started_at_epoch": 1_000.0,
                "ended_at_epoch": 1_245.0,
                "duration_seconds": 245.0,
            },
            "combat_clock_party_key": "1" * 64,
            "combat_clock_target_key": "2" * 64,
            "total_damage": 9_000,
            "team_dps": 9_000 / 245,
            "team_effective_healing": 4_000,
            "team_total_healing": 5_000,
            "team_overhealing": 1_000,
            "team_taken": 7_000,
            "damage_taken": [{"actor_id": SELF_ID, "taken": 7_000}],
            "participants": [
                {
                    "actor_id": SELF_ID,
                    "damage": 9_000,
                    "dps": 9_000 / 245,
                    "damage_hits": 20,
                    "critical_hits": 7,
                    "penetration_hits": 3,
                    "critical_rate": 0.35,
                    "penetration_rate": 0.15,
                    "skills": [
                        {
                            "skill_id": 123,
                            "damage": 9_000,
                            "hits": 20,
                            "max_hit": 900,
                        }
                    ],
                }
            ],
            "healers": [
                {
                    "actor_id": SELF_ID,
                    "total_healing": 5_000,
                    "effective_healing": 4_000,
                    "overhealing": 1_000,
                    "hps": 4_000 / 245,
                    "skills": [{"skill_id": 456, "effective_healing": 4_000}],
                }
            ],
            "event_log": {
                "version": 1,
                "columns": [
                    "time_ms",
                    "actor_id",
                    "target_id",
                    "skill_id",
                    "damage",
                    "critical",
                    "penetrating",
                ],
                "coverage": "observed_damage_events",
                "origin_started_at_epoch": 1_000.0,
                "rows": [
                    [0, SELF_ID, MONSTER_ID, 123, 4_000, False, False],
                    [245_000, SELF_ID, MONSTER_ID, 123, 5_000, True, True],
                ],
            },
            "team_damage_samples": {
                "version": 1,
                "columns": ["time_seconds", "total_damage"],
                "coverage": "live_team_cumulative",
                "origin_started_at_epoch": 1_000.0,
                "rows": [[0, 4_000], [245, 9_000]],
            },
            "participant_damage_samples": {
                "version": 1,
                "columns": ["time_seconds", "actor_id", "total_damage"],
                "coverage": "live_participant_cumulative",
                "origin_started_at_epoch": 1_000.0,
                "rows": [
                    [0, SELF_ID, 4_000],
                    [245, SELF_ID, 9_000],
                ],
            },
            "_shared_clock_request": {
                "party_key": "1" * 64,
                "target_key": "2" * 64,
                "client_revision": 8,
                "state": "final",
            },
        }
        raw_before = {
            "total_damage": record["total_damage"],
            "team_effective_healing": record["team_effective_healing"],
            "team_total_healing": record["team_total_healing"],
            "team_overhealing": record["team_overhealing"],
            "team_taken": record["team_taken"],
            "damage_taken": json.loads(json.dumps(record["damage_taken"])),
            "participant": {
                key: json.loads(json.dumps(record["participants"][0][key]))
                for key in (
                    "damage",
                    "damage_hits",
                    "critical_hits",
                    "penetration_hits",
                    "critical_rate",
                    "penetration_rate",
                    "skills",
                )
            },
            "healer": {
                key: json.loads(json.dumps(record["healers"][0][key]))
                for key in (
                    "total_healing",
                    "effective_healing",
                    "overhealing",
                    "skills",
                )
            },
        }
        result = CombatClockResult(
            synchronized=True,
            encounter_id=record["encounter_id"],
            clock_id="3" * 32,
            started_at=994.5,
            ended_at=1_245.0,
            duration_seconds=250.5,
            final=True,
            state="final",
            revision=20,
            client_revision=8,
            party_key="1" * 64,
            target_key="2" * 64,
            total_damage=9_000,
        )

        updated = apply_combat_clock_to_record(record, result)

        raw_after = {
            "total_damage": updated["total_damage"],
            "team_effective_healing": updated["team_effective_healing"],
            "team_total_healing": updated["team_total_healing"],
            "team_overhealing": updated["team_overhealing"],
            "team_taken": updated["team_taken"],
            "damage_taken": updated["damage_taken"],
            "participant": {
                key: updated["participants"][0][key]
                for key in raw_before["participant"]
            },
            "healer": {
                key: updated["healers"][0][key]
                for key in raw_before["healer"]
            },
        }
        self.assertEqual(raw_after, raw_before)
        self.assertEqual(updated["duration_source"], "server_shared_clock")
        self.assertEqual(updated["dps_duration_seconds"], 250.0)
        self.assertEqual(updated["hps_duration_seconds"], 250.0)
        self.assertEqual(
            [row[0] for row in updated["event_log"]["rows"]],
            [5_500, 250_500],
        )
        self.assertEqual(
            [row[4] for row in updated["event_log"]["rows"]],
            [4_000, 5_000],
        )
        self.assertEqual(
            updated["team_damage_samples"]["rows"],
            [[5, 4_000], [250, 9_000]],
        )
        self.assertEqual(
            updated["participant_damage_samples"]["rows"],
            [[5, SELF_ID, 4_000], [250, SELF_ID, 9_000]],
        )

    def test_shared_clock_rejects_wrong_scope_revision_and_total(self):
        base_record = {
            "encounter_id": "clock-validation-000001",
            "started_at_epoch": 100.0,
            "ended_at_epoch": 200.0,
            "duration_seconds": 100.0,
            "duration_source": "local_network_events",
            "total_damage": 1_000,
            "participants": [{"actor_id": SELF_ID, "damage": 1_000}],
            "healers": [],
            "_shared_clock_request": {
                "party_key": "4" * 64,
                "target_key": "5" * 64,
                "client_revision": 7,
                "state": "final",
            },
        }

        def result(**changes):
            values = {
                "synchronized": True,
                "encounter_id": base_record["encounter_id"],
                "clock_id": "6" * 32,
                "started_at": 95.0,
                "ended_at": 200.0,
                "duration_seconds": 105.0,
                "final": True,
                "state": "final",
                "revision": 10,
                "client_revision": 7,
                "party_key": "4" * 64,
                "target_key": "5" * 64,
                "total_damage": 1_000,
            }
            values.update(changes)
            return CombatClockResult(**values)

        wrong_scope = apply_combat_clock_to_record(
            base_record, result(target_key="7" * 64)
        )
        self.assertEqual(
            wrong_scope["shared_clock_rejected"]["reason"], "scope_mismatch"
        )
        stale = apply_combat_clock_to_record(
            base_record, result(client_revision=6)
        )
        self.assertEqual(
            stale["shared_clock_rejected"]["reason"], "stale_client_revision"
        )
        wrong_total = apply_combat_clock_to_record(
            base_record, result(total_damage=999)
        )
        self.assertEqual(
            wrong_total["shared_clock_rejected"]["reason"],
            "total_damage_mismatch",
        )

    def test_validated_game_server_team_clock_is_the_final_dps_divisor(self):
        model = self._clock_ready_model("game-team-clock")
        filetime = int(
            (model.last_damage_time + 11_644_473_600) * 10_000_000
        )
        accepted = model.ingest_stage_summary(
            {
                "summary_id": "settlement|clock-test",
                "filetime_100ns": filetime,
                "member_count": 2,
                "authoritative": True,
                "completion_confirmed": True,
                "actors": [
                    {
                        "actor_id": SELF_ID,
                        "damage": 800_000,
                        "combat_seconds_total": 16,
                        "combat_seconds_delta": 16,
                    },
                    {
                        "actor_id": TEAMMATE_ID,
                        "damage": 1_200_000,
                        "combat_seconds_total": 16,
                        "combat_seconds_delta": 16,
                    },
                ],
            }
        )
        self.assertTrue(accepted)
        model.combat_end_time = model.last_damage_time
        self.assertEqual(model.duration(model.last_damage_time + 100.0), 16.0)

        record = model.build_combat_record("target_defeated")
        self.assertEqual(record["duration_source"], "game_server_team_clock")
        self.assertEqual(record["dps_duration_seconds"], 16.0)
        self.assertEqual(record["team_dps"], 125_000.0)
        self.assertEqual(
            record["game_server_team_clock"]["member_seconds"], [16, 16]
        )
        self.assertEqual(record["_shared_clock_request"]["state"], "final")
        self.assertEqual(
            record["duration_seconds"],
            record["ended_at_epoch"] - record["started_at_epoch"],
        )

        stale_shared = CombatClockResult(
            synchronized=True,
            encounter_id=model.encounter_id,
            clock_id="f" * 32,
            started_at=100.0,
            ended_at=190.0,
            duration_seconds=90.0,
            final=True,
            server_time=191.0,
        )
        self.assertEqual(
            apply_combat_clock_to_record(record, stale_shared), record
        )

    def test_matched_game_settlement_is_not_vetoed_by_five_second_fallback_rule(self):
        model = self._clock_ready_model("game-team-clock-no-local-veto")
        filetime = int(
            (model.last_damage_time + 11_644_473_600) * 10_000_000
        )
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "settlement|clock-no-local-veto",
                    "filetime_100ns": filetime,
                    "member_count": 2,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 800_000,
                            "combat_seconds_total": 30,
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 1_200_000,
                            "combat_seconds_total": 30,
                        },
                    ],
                }
            )
        )
        model.combat_end_time = model.last_damage_time

        record = model.build_combat_record("target_defeated")

        self.assertEqual(record["duration_source"], "game_server_team_clock")
        self.assertEqual(record["dps_duration_seconds"], 30.0)
        audit = record["game_server_team_clock"]
        self.assertGreater(audit["difference_seconds"], 5.0)
        self.assertFalse(audit["local_comparison_used_for_acceptance"])

    def test_shared_clock_90_seconds_cannot_replace_230_second_event_span(self):
        record = {
            "encounter_id": "real-bonnie-regression",
            "started_at_epoch": 1788270036.0627675,
            "ended_at_epoch": 1788270266.376329,
            "duration_seconds": 230.313561439514,
            "dps_duration_seconds": 230.0,
            "duration_source": "local_network_events",
            "total_damage": 9_551_438,
            "team_dps": 9_551_438 / 230,
            "participants": [
                {"actor_id": SELF_ID, "damage": 9_551_438, "dps": 9_551_438 / 230}
            ],
            "healers": [],
            "_shared_clock_request": {"state": "ended"},
        }
        wrong = CombatClockResult(
            synchronized=True,
            encounter_id=record["encounter_id"],
            clock_id="9" * 32,
            started_at=1788270046.189268,
            ended_at=1788270137.0303168,
            duration_seconds=90.84104871749878,
            final=True,
            server_time=1788270147.0643907,
        )

        updated = apply_combat_clock_to_record(record, wrong)

        self.assertEqual(updated["dps_duration_seconds"], 230.0)
        self.assertEqual(updated["team_dps"], 9_551_438 / 230)
        self.assertEqual(
            updated["shared_clock_rejected"]["reason"],
            "shorter_than_local_observed_damage_span",
        )

    def test_shared_clock_106_seconds_cannot_replace_245_second_event_span(self):
        started_at = 1_788_661_563.0
        ended_at = started_at + 244.985575
        record = {
            "encounter_id": "现场长阶段回归-000001",
            "started_at_epoch": started_at,
            "ended_at_epoch": ended_at,
            "duration_seconds": ended_at - started_at,
            "dps_duration_seconds": 244.0,
            "duration_source": "local_network_events",
            "local_event_interval": {
                "started_at_epoch": started_at,
                "ended_at_epoch": ended_at,
                "duration_seconds": ended_at - started_at,
            },
            "total_damage": 36_482_848,
            "participants": [
                {
                    "actor_id": SELF_ID,
                    "damage": 36_482_848,
                    "dps": 36_482_848 / 244.0,
                }
            ],
            "healers": [],
            "_shared_clock_request": {"state": "settling"},
        }
        wrong_end = started_at + 105.805791
        wrong = CombatClockResult(
            synchronized=True,
            encounter_id=record["encounter_id"],
            clock_id="9" * 32,
            started_at=started_at,
            ended_at=wrong_end,
            duration_seconds=wrong_end - started_at,
            final=True,
            state="final",
            revision=7,
            total_damage=36_482_848,
        )

        updated = apply_combat_clock_to_record(record, wrong)

        self.assertEqual(updated["duration_seconds"], record["duration_seconds"])
        self.assertEqual(updated["total_damage"], record["total_damage"])
        self.assertEqual(
            updated["shared_clock_rejected"]["reason"],
            "shorter_than_local_observed_damage_span",
        )

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
                "history",
                "peak",
                "analytics",
                "updates",
                "backend_settings",
                "identity",
                "share",
                "reset",
                "eye",
                "eye_off",
                "lock",
                "unlock",
                "menu",
                "user",
                "settings",
                "pin",
                "pin_off",
                "minimize",
                "close",
            )
        }
        self.assertTrue(all(image.size == (20, 20) for image in rendered.values()))
        self.assertTrue(
            all(image.getchannel("A").getbbox() is not None for image in rendered.values())
        )
        backend_icons = (
            "history",
            "peak",
            "analytics",
            "updates",
            "backend_settings",
            "identity",
        )
        self.assertEqual(
            len({rendered[name].tobytes() for name in backend_icons}),
            len(backend_icons),
        )
        self.assertNotEqual(rendered["lock"].tobytes(), rendered["unlock"].tobytes())
        self.assertNotEqual(rendered["pin"].tobytes(), rendered["pin_off"].tobytes())

    def test_clear_icon_is_a_distinct_non_refresh_action(self):
        trash = IconFactory._draw_toolbar_icon("trash", 20, "#f4f6f8")
        reset = IconFactory._draw_toolbar_icon("reset", 20, "#f4f6f8")
        self.assertIsNotNone(trash.getchannel("A").getbbox())
        self.assertNotEqual(trash.tobytes(), reset.tobytes())

    def test_unknown_positive_skill_icon_is_transparent_placeholder(self):
        # The fallback renderer remains available for app/profession badges,
        # but the skill path must no longer invent icons for unknown positive IDs.
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        skill_source = source[
            source.index("    def skill(\n") : source.index(
                "    def _profession_info", source.index("    def skill(\n")
            )
        ]
        self.assertIn("Image.new(\"RGBA\", (size, size), (0, 0, 0, 0))", skill_source)
        self.assertNotIn("or self._draw_badge(", skill_source)

    def test_unclassified_damage_icon_is_a_visible_transparent_asset(self):
        path = Path(__file__).with_name("assets") / "skills" / "0.png"

        with Image.open(path) as image:
            rgba = image.convert("RGBA")

        self.assertEqual(rgba.size, (128, 128))
        alpha = rgba.getchannel("A")
        self.assertIsNotNone(alpha.getbbox())
        self.assertEqual(alpha.getpixel((0, 0)), 0)
        self.assertGreater(alpha.getextrema()[1], 0)

    def test_client_effect_names_and_icons_are_bundled(self):
        names = load_skill_catalog()
        metadata = load_skill_metadata().get("skills", {})
        expected = {
            "80003004": (
                "铁火铸就的盟约",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Equipment/BeyonderIcon/"
                "Convergence_1_7_8.Convergence_1_7_8",
            ),
            "80003005": (
                "血谋共舞的旗帜",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Equipment/BeyonderIcon/"
                "Convergence_1_7_7.Convergence_1_7_7",
            ),
            "80004017": (
                "自由与提线木偶",
                "/Game/Arts/UI_2/Resource/Item/Large/3240556.3240556",
            ),
            "800040172": (
                "自由与提线木偶",
                "/Game/Arts/UI_2/Resource/Item/Large/3240556.3240556",
            ),
            "81001264": (
                "持续回血",
                "/Game/Arts/UI_2/Resource/Item/Large/2003509.2003509",
            ),
            "81001265": (
                "瞬间回血",
                "/Game/Arts/UI_2/Resource/Item/Large/2002100.2002100",
            ),
            "81002101": (
                "生命织线",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Buff/147.147",
            ),
            "87912040": (
                "恶灵",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Buff/147.147",
            ),
            "87912064": (
                "蠕动的饥饿",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Buff/147.147",
            ),
            "80075010": (
                "知识漩涡",
                "/Game/Arts/UI_2/Resource/Skill/Profession/MysteryPryer/"
                "MysteryPryer_Skill_11.MysteryPryer_Skill_11",
            ),
            "82030003": (
                "愚弄标记",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Buff/63.63",
            ),
            "80020001": (
                "空想姿态",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Buff/54.54",
            ),
            "804000001": (
                "无瞳的将军",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Buff/147.147",
            ),
            "814010009": (
                "粉碎灵魂",
                "/Game/Arts/UI_2/Resource/ConfigIcon/Buff/147.147",
            ),
        }
        asset_root = Path(__file__).with_name("assets") / "skills"
        for skill_id, (name, icon_path) in expected.items():
            self.assertEqual(names[skill_id], name)
            self.assertEqual(metadata[skill_id]["icon_path"], icon_path)
            icon = asset_root / f"{skill_id}.png"
            self.assertTrue(icon.is_file())
            self.assertGreater(icon.stat().st_size, 0)

        self.assertEqual(names["81002101"], "生命织线")
        self.assertEqual(names["83310623"], "蓬勃生长")

        for alias_id in ("800011802", "820600101", "833103901", "86011051"):
            icon = asset_root / f"{alias_id}.png"
            self.assertTrue(icon.is_file())
            self.assertGreater(icon.stat().st_size, 0)

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

    def test_update_install_paths_stage_and_target_latest_filename(self):
        staging, target = update_install_paths(
            Path(r"D:\Apps\DpsMeter"),
            Path(r"D:\Apps\DpsMeter\叨叨诡秘-Dps-Logs-v0.0.14.exe"),
            "叨叨诡秘-Dps-Logs-v0.1.0.exe",
        )
        self.assertEqual(
            target,
            Path(r"D:\Apps\DpsMeter\叨叨诡秘-Dps-Logs-v0.1.0.exe"),
        )
        self.assertEqual(
            staging,
            Path(r"D:\Apps\DpsMeter\叨叨诡秘-Dps-Logs-v0.1.0.update.exe"),
        )

        _staging, fallback_target = update_install_paths(
            Path(r"D:\Apps\DpsMeter"),
            Path(r"D:\Apps\DpsMeter\current.exe"),
            "..\\bad-name.txt",
        )
        self.assertEqual(
            fallback_target,
            Path(r"D:\Apps\DpsMeter\Dps-Logs-update.exe"),
        )

    def test_build_scripts_derive_artifact_names_from_source_versions(self):
        root = Path(__file__).parent
        main_build = (root / "build_exe.ps1").read_text(encoding="utf-8")
        diagnostic_build = (root / "build_diagnostic_tool.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$SourcePath = Join-Path $ProjectDir \"dps_meter.pyw\"", main_build)
        self.assertIn("$AppVersion", main_build)
        self.assertIn("$SourcePath = Join-Path $ProjectDir \"diagnostic_report.py\"", diagnostic_build)
        self.assertIn("$ToolVersion", diagnostic_build)
        self.assertNotIn("v0.1.0.exe", main_build)
        self.assertNotIn("$ProductName.exe", diagnostic_build)

    def test_update_replacer_launches_latest_target_and_removes_previous_name(self):
        window = object.__new__(DpsWindow)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "latest.update.exe"
            source.write_bytes(b"MZ")
            target = directory / "叨叨诡秘-Dps-Logs-v0.1.0.exe"
            legacy = directory / "叨叨诡秘-Dps-Logs-v0.0.14.exe"
            calls = []
            function_globals = window._launch_update_replacer.__globals__
            original_update_dir = function_globals["UPDATE_DIR"]
            original_popen = function_globals["subprocess"].Popen
            function_globals["UPDATE_DIR"] = directory
            function_globals["subprocess"].Popen = lambda *args, **kwargs: calls.append(
                (args, kwargs)
            )
            try:
                window._launch_update_replacer(
                    source,
                    target_path=target,
                    legacy_target_path=legacy,
                )
            finally:
                function_globals["UPDATE_DIR"] = original_update_dir
                function_globals["subprocess"].Popen = original_popen

            script = (directory / "apply-update.ps1").read_text(
                encoding="utf-8-sig"
            )
            self.assertEqual(len(calls), 1)
            command = list(calls[0][0][0])
            self.assertIn("-PreviousTarget", command)
            self.assertEqual(command[command.index("-Target") + 1], str(target))
            self.assertEqual(
                command[command.index("-PreviousTarget") + 1], str(legacy)
            )
            self.assertIn(
                "Remove-Item -LiteralPath $PreviousTarget", script
            )
            self.assertNotIn(
                "Copy-Item -LiteralPath $Source -Destination $PreviousTarget",
                script,
            )
            self.assertNotIn("Wait-Process", script)

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

    def test_login_taskbar_style_is_interactive_and_can_show_without_activation(self):
        tool_window = 0x00000080
        app_window = 0x00040000
        layered = 0x00080000
        no_activate = 0x08000000
        original = tool_window | layered | no_activate

        interactive = window_exstyle_for_taskbar(original)
        self.assertTrue(interactive & app_window)
        self.assertTrue(interactive & layered)
        self.assertFalse(interactive & tool_window)
        self.assertFalse(interactive & no_activate)

        background = window_exstyle_for_taskbar(original, no_activate=True)
        self.assertTrue(background & app_window)
        self.assertFalse(background & tool_window)
        self.assertTrue(background & no_activate)

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
        self.assertEqual(membership_label_for_card_tier("monthly"), "VVVVIP")
        self.assertEqual(membership_label_for_card_tier("weekly"), "VIP")
        self.assertEqual(membership_label_for_card_tier("normal"), "普通")
        self.assertEqual(membership_label_for_card_tier("unknown"), "普通")
        self.assertEqual(membership_badge_for_card_tier("partner"), "小伙伴")
        self.assertEqual(membership_badge_for_card_tier("monthly"), "VVVVIP")
        self.assertEqual(membership_badge_for_card_tier("weekly"), "VIP")
        self.assertEqual(membership_badge_for_card_tier("normal"), "普通")
        self.assertEqual(membership_badge_for_card_tier("unknown"), "普通")

    def test_main_close_action_requests_confirmation_before_safe_close(self):
        window = object.__new__(DpsWindow)
        window.closing = False
        window.root = object()
        window.close = mock.Mock()
        window._hide_main_tooltip = mock.Mock()
        window._show_notice = mock.Mock()

        window._request_close()

        window._hide_main_tooltip.assert_called_once_with()
        window._show_notice.assert_called_once()
        args, kwargs = window._show_notice.call_args
        self.assertEqual(args[0], "退出程序")
        self.assertEqual(kwargs["confirm_text"], "退出")
        self.assertEqual(kwargs["cancel_text"], "取消")
        self.assertIs(kwargs["on_confirm"], window.close)
        kwargs["on_confirm"]()
        window.close.assert_called_once_with()

    def test_membership_availability_is_discreet_and_precise_to_seconds(self):
        expires_at = dt.datetime(2026, 9, 4, 13, 14, 15)
        self.assertEqual(
            membership_availability_text("monthly", expires_at),
            "至 2026-09-04 13:14:15",
        )
        self.assertEqual(membership_availability_text("partner", expires_at), "长期可用")
        self.assertEqual(membership_availability_text("normal", None), "本次可用")
        self.assertEqual(
            membership_contract_text("monthly", expires_at),
            "同行契约至：2026-09-04 13:14:15",
        )
        self.assertEqual(
            membership_contract_text("partner", expires_at),
            "同行契约：长期可用",
        )

    def test_backend_sidebar_connection_copy_follows_capture_state(self):
        class Label:
            def __init__(self, **options):
                self.options = dict(options)

            def winfo_exists(self):
                return True

            def cget(self, key):
                return self.options.get(key, "")

            def configure(self, **options):
                self.options.update(options)

        window = object.__new__(DpsWindow)
        window.backend_connection_dot = Label(fg="")
        window.backend_connection_label = Label(text="", fg="")

        window.connected = False
        window.capture_started = False
        window._sync_backend_sidebar_status()
        self.assertEqual(
            window.backend_connection_label.options["text"], "尚未连接游戏"
        )

        window.capture_started = True
        window._sync_backend_sidebar_status()
        self.assertEqual(
            window.backend_connection_label.options["text"], "等待连接游戏"
        )

        window.connected = True
        window._sync_backend_sidebar_status()
        self.assertEqual(
            window.backend_connection_label.options["text"], "已连接游戏"
        )

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
            self.assertAlmostEqual(float(root.attributes("-alpha")), 0.85)
            self.assertTrue(window._set_window_click_through(root, False))
            restored = window._win32_extended_style(hwnd)
            self.assertIsNotNone(restored)
            self.assertEqual(restored & 0x00080020, original & 0x00080020)
            self.assertAlmostEqual(float(root.attributes("-alpha")), 0.85)
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
                "extraordinary_rating": 54_817,
            }
        )
        self.assertEqual(model.display_target_name(guard_id), "星光守卫")
        self.assertNotIn(guard_id, model.entity_professions)
        self.assertNotIn(guard_id, model.entity_extraordinary_ratings)
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
        self.assertTrue(boss_name_is_allowed("安西娅", allowlist))
        self.assertTrue(boss_name_is_allowed("巴尼先生", allowlist))
        self.assertFalse(boss_name_is_allowed("纸人替身-保护", allowlist))
        catalog = load_monster_catalog()
        self.assertEqual(catalog["7100208"]["name"], "安西娅")
        self.assertEqual(catalog["7100209"]["name"], "巴尼先生")
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
        self.assertNotIn("7110642", catalog)
        for template_id in ("7100215", "7102938"):
            self.assertIn(template_id, catalog)
            self.assertEqual(catalog[template_id]["name"], "子嗣守护")
            self.assertEqual(catalog[template_id]["boss_type"], 3)
        for template_id in (
            "7102932",
            "7102936",
            "7102990",
            "7102991",
            "7106130",
        ):
            self.assertIn(template_id, catalog)
            self.assertTrue(catalog[template_id]["name"].startswith("子爵夫人"))
            self.assertEqual(catalog[template_id]["boss_type"], 3)
        self.assertIn("7102980", catalog)
        self.assertEqual(catalog["7102980"]["name"], "一号信徒")
        self.assertEqual(catalog["7102980"]["boss_type"], 3)
        # Adjacent phase rows have not been independently named, so the
        # template-scoped exception must not promote them by proximity.
        self.assertNotIn("7102981", catalog)
        self.assertNotIn("7102982", catalog)

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

    def test_exact_template_restores_live_dps_target_name_and_level(self):
        model = CombatModel(
            run_id="target-template-display-test",
            target_catalog={
                "7109821": {
                    "boss_type": 3,
                    "name": "异化猎犬",
                    "level": 27,
                }
            },
        )
        model.ingest_identity({"entity_id": SELF_ID})
        self.assertTrue(
            model.ingest_profile(
                {
                    "entity_id": MONSTER_ID,
                    "name": "未知目标",
                    "entity_type": "Boss",
                    "template_id": 7_109_821,
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 500))

        self.assertEqual(model.display_target_name(MONSTER_ID), "异化猎犬")
        self.assertEqual(model.monsters[MONSTER_ID].level, 27)
        self.assertEqual(model.actor_target_rows(SELF_ID)[0]["name"], "异化猎犬")

    def test_display_identity_catalog_keeps_non_boss_exact_templates(self):
        full_catalog = load_target_identity_catalog()
        filtered_catalog = load_monster_catalog()
        non_boss_with_name = next(
            (
                template_id
                for template_id, metadata in full_catalog.items()
                if template_id not in filtered_catalog
                and isinstance(metadata, dict)
                and str(metadata.get("name", "")).strip()
            ),
            None,
        )
        self.assertIsNotNone(non_boss_with_name)

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
                    "level": 27,
                }
            },
        )

        self.assertEqual(record["monster"]["name"], "首领")
        self.assertEqual(restored["monster"]["name"], "异化猎犬")
        self.assertEqual(restored["monster"]["level"], 27)
        self.assertEqual(restored["targets"][0]["name"], "异化猎犬")
        self.assertEqual(restored["targets"][0]["level"], 27)
        self.assertEqual(
            restored["participants"][0]["targets"][0]["name"],
            "异化猎犬",
        )
        self.assertEqual(
            restored["participants"][0]["targets"][1]["name"],
            "未分配目标",
        )

    def test_history_first_believer_target_damage_is_one_encounter_row(self):
        first_phase_id = 246_380_262_337_520
        partner_id = 246_380_262_337_521
        second_phase_id = 246_380_262_337_577
        record = {
            "monster": {
                "entity_id": second_phase_id,
                "template_id": 7_100_210,
                "name": "安西娅",
                "boss_rank": 3,
            },
            "targets": [
                {
                    "entity_id": second_phase_id,
                    "template_id": 7_100_210,
                    "name": "安西娅",
                    "boss_rank": 3,
                },
                {
                    "entity_id": first_phase_id,
                    "template_id": 7_100_208,
                    "name": "安西娅",
                    "boss_rank": 3,
                },
                {
                    "entity_id": partner_id,
                    "template_id": 7_100_209,
                    "name": "巴尼先生",
                    "boss_rank": 3,
                },
            ],
            "participants": [
                {
                    "actor_id": SELF_ID,
                    "damage": 4_493,
                    "targets": [
                        {
                            "entity_id": partner_id,
                            "entity_ids": [partner_id],
                            "name": "巴尼先生",
                            "kind": "Boss",
                            "damage": 4_340,
                            "share": 4_340 / 4_493,
                        },
                        {
                            "entity_id": first_phase_id,
                            "entity_ids": [first_phase_id],
                            "name": "安西娅",
                            "kind": "Boss",
                            "damage": 153,
                            "share": 153 / 4_493,
                        },
                    ],
                }
            ],
        }
        catalog = {
            "7100208": {"name": "安西娅", "boss_type": 3},
            "7100209": {"name": "巴尼先生", "boss_type": 3},
            "7100210": {"name": "安西娅", "boss_type": 3},
            "7102980": {"name": "一号信徒", "boss_type": 3},
        }

        restored = restore_history_boss_names(record, catalog)

        self.assertEqual(len(record["participants"][0]["targets"]), 2)
        self.assertEqual(len(restored["targets"]), 3)
        actor_targets = restored["participants"][0]["targets"]
        self.assertEqual(len(actor_targets), 1)
        self.assertEqual(actor_targets[0]["name"], "一号信徒")
        self.assertEqual(actor_targets[0]["damage"], 4_493)
        self.assertEqual(actor_targets[0]["share"], 1.0)
        self.assertEqual(
            actor_targets[0]["entity_ids"],
            [partner_id, first_phase_id],
        )
        self.assertTrue(actor_targets[0]["history_encounter_group"])

    def test_history_single_first_believer_phase_is_not_relabelled(self):
        record = {
            "monster": {
                "entity_id": MONSTER_ID,
                "template_id": 7_100_208,
                "name": "安西娅",
                "boss_rank": 3,
            },
            "targets": [
                {
                    "entity_id": MONSTER_ID,
                    "template_id": 7_100_208,
                    "name": "安西娅",
                    "boss_rank": 3,
                }
            ],
            "participants": [
                {
                    "actor_id": SELF_ID,
                    "damage": 100,
                    "targets": [
                        {
                            "entity_id": MONSTER_ID,
                            "entity_ids": [MONSTER_ID],
                            "name": "安西娅",
                            "kind": "Boss",
                            "damage": 100,
                            "share": 1.0,
                        }
                    ],
                }
            ],
        }

        restored = restore_history_boss_names(
            record,
            {"7100208": {"name": "安西娅", "boss_type": 3}},
        )

        self.assertIs(restored, record)
        self.assertEqual(
            restored["participants"][0]["targets"][0]["name"],
            "安西娅",
        )

    def test_history_placeholder_skill_names_use_verified_current_catalog(self):
        record = {
            "participants": [
                {
                    "actor_id": TEAMMATE_ID,
                    "skills": [
                        {
                            "skill_id": 83_310_390,
                            "name": "技能 83310390",
                            "damage": 900,
                        },
                        {
                            "skill_id": 123,
                            "name": "历史有效名称",
                            "damage": 100,
                        },
                        {
                            "skill_id": 800_610_802,
                            "name": "未知技能",
                            "damage": 80,
                        },
                        {
                            "skill_id": 800_011_802,
                            "name": "飓风之斧",
                            "damage": 70,
                        },
                    ],
                }
            ],
            "healers": [
                {
                    "actor_id": TEAMMATE_ID,
                    "skills": [
                        {
                            "skill_id": 86_011_050,
                            "name": "未知技能",
                            "effective_healing": 500,
                        }
                    ],
                }
            ],
            "event_log": {
                "columns": [
                    "time_ms",
                    "actor_id",
                    "target_id",
                    "skill_id",
                    "damage",
                ],
                "rows": [
                    [10, TEAMMATE_ID, MONSTER_ID, 800_610_802, 80],
                    [20, TEAMMATE_ID, MONSTER_ID, 800_011_802, 70],
                ],
            },
            "skill_cast_log": {
                "columns": ["time_ms", "actor_id", "skill_id", "sequence"],
                "rows": [[5, TEAMMATE_ID, 800_610_802, 9]],
            },
            "boss_damage": {
                "skills": [
                    {
                        "skill_id": 83_310_390,
                        "name": "技能 83310390",
                        "damage": 600,
                    }
                ],
                "sources": [
                    {
                        "entity_id": MONSTER_ID,
                        "skills": [
                            {
                                "skill_id": 86_011_050,
                                "name": "未知技能",
                                "damage": 500,
                            }
                        ],
                    }
                ],
            },
        }

        restored = restore_history_skill_names(
            record,
            {
                "83310390": "光芒牢笼",
                "86011050": "圣光净化",
                "80061080": "怒意猛击被动",
                "800011802": "飓风之斧",
                "123": "不应覆盖历史有效名称",
            },
        )

        self.assertEqual(
            record["participants"][0]["skills"][0]["name"],
            "技能 83310390",
        )
        self.assertEqual(
            restored["participants"][0]["skills"][0]["name"],
            "光芒牢笼",
        )
        self.assertEqual(
            restored["participants"][0]["skills"][1]["name"],
            "历史有效名称",
        )
        self.assertEqual(
            restored["healers"][0]["skills"][0]["name"],
            "圣光净化",
        )
        self.assertEqual(
            restored["participants"][0]["skills"][2]["skill_id"],
            80_061_080,
        )
        self.assertEqual(
            restored["participants"][0]["skills"][2]["name"],
            "怒意猛击被动",
        )
        self.assertEqual(
            restored["participants"][0]["skills"][3]["skill_id"],
            800_011_802,
        )
        self.assertEqual(restored["event_log"]["rows"][0][3], 80_061_080)
        self.assertEqual(restored["event_log"]["rows"][1][3], 800_011_802)
        self.assertEqual(restored["skill_cast_log"]["rows"][0][2], 80_061_080)
        self.assertEqual(record["event_log"]["rows"][0][3], 800_610_802)
        self.assertEqual(record["skill_cast_log"]["rows"][0][2], 800_610_802)
        self.assertEqual(
            restored["boss_damage"]["skills"][0]["name"], "光芒牢笼"
        )
        self.assertEqual(
            restored["boss_damage"]["sources"][0]["skills"][0]["name"],
            "圣光净化",
        )
        self.assertEqual(
            record["boss_damage"]["skills"][0]["name"], "技能 83310390"
        )

    def test_damage_effect_suffix_is_collapsed_only_when_catalog_confirms_root(self):
        model = CombatModel(
            run_id="catalog-confirmed-skill-root",
            skill_names={
                "80061080": "怒意猛击被动",
                "80060002": "银白细剑",
                "814010009": "粉碎灵魂",
                "800011802": "飓风之斧",
            },
        )
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )

        observed = (
            (800_610_802, 80_061_080),
            (800_600_022, 80_060_002),
            (8_140_100_094, 814_010_009),
            (800_011_802, 800_011_802),
            (899_999_999, 899_999_999),
        )
        for sequence, (raw_skill_id, _expected_skill_id) in enumerate(
            observed, start=1
        ):
            event = damage(sequence, SELF_ID, MONSTER_ID, 100)
            event["skill_id"] = raw_skill_id
            model.ingest(event)

        expected_ids = {expected for _raw, expected in observed}
        self.assertEqual(set(model.stats[SELF_ID].skills), expected_ids)
        self.assertEqual(
            {int(event["skill_id"]) for event in model.events},
            expected_ids,
        )
        self.assertEqual(
            model.display_skill_name(SELF_ID, 80_061_080),
            "怒意猛击被动",
        )

    def test_verified_skill_catalog_contains_restored_settlement_ids(self):
        catalog = load_skill_catalog()

        self.assertEqual(catalog["80001180"], "飓风之斧")
        self.assertEqual(catalog["800011802"], "飓风之斧")
        self.assertEqual(catalog["83310390"], "光芒牢笼")
        self.assertEqual(catalog["833103901"], "光芒牢笼")
        self.assertEqual(catalog["814010009"], "粉碎灵魂")
        self.assertEqual(catalog["86011050"], "圣光净化")

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

    def test_delayed_exact_wipe_table_adds_only_skills_and_rates(self):
        model = CombatModel(run_id="party-wipe-detail-test")
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
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest(damage(2, TEAMMATE_ID, MONSTER_ID, 200))
        for actor_id, second in ((SELF_ID, 3), (TEAMMATE_ID, 4)):
            model.ingest_life(
                {
                    "actor_id": actor_id,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + second * 10_000_000,
                }
            )
        self.assertEqual(model.combat_end_reason, "party_wipe")
        self.assertTrue(model.finalize_if_idle(model.combat_end_time))

        original_damage = {
            actor_id: actor.damage for actor_id, actor in model.stats.items()
        }
        original_end_time = model.combat_end_time
        original_target_hp = model.monsters[MONSTER_ID].current_hp
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "5150108|1|wipe-detail",
                    "stage_id": 5_150_108,
                    "filetime_100ns": BASE_FILETIME + 33 * 10_000_000,
                    "member_count": 2,
                    "authoritative": False,
                    "completion_confirmed": False,
                    "realtime_detail": False,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 100,
                            "taken": 90_000,
                            "damage_hits": 5,
                            "critical_hits": 1,
                            "penetration_hits": 4,
                            "combat_seconds_total": 999,
                            "effective_healing": 80_000,
                            "skills": [
                                {"skill_id": 86_021_010, "damage": 100, "hits": 5}
                            ],
                            "healing_skills": [
                                {"skill_id": 86_021_030, "effective_healing": 80_000}
                            ],
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 200,
                            "taken": 70_000,
                            "damage_hits": 10,
                            "critical_hits": 4,
                            "penetration_hits": 8,
                            "combat_seconds_total": 999,
                            "effective_healing": 60_000,
                            "skills": [
                                {"skill_id": 86_031_010, "damage": 120, "hits": 6},
                                {"skill_id": 86_031_020, "damage": 70, "hits": 4},
                            ],
                            "healing_skills": [
                                {"skill_id": 86_031_030, "effective_healing": 60_000}
                            ],
                        },
                    ],
                }
            )
        )

        self.assertEqual(
            {actor_id: actor.damage for actor_id, actor in model.stats.items()},
            original_damage,
        )
        self.assertEqual(model.combat_end_time, original_end_time)
        self.assertEqual(model.monsters[MONSTER_ID].current_hp, original_target_hp)
        self.assertEqual(model.stage_actor_taken, {})
        self.assertEqual(model.stage_healing_snapshots, {})
        self.assertEqual(model.game_server_duration_seconds, 0.0)
        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual(
            {
                skill_id: (skill.damage, skill.hits)
                for skill_id, skill in teammate.skills.items()
            },
            {
                86_031_010: (120, 6),
                86_031_020: (70, 4),
                0: (10, 0),
            },
        )
        self.assertEqual(teammate.damage_hits, 10)
        self.assertEqual(teammate.critical_hits, 4)
        self.assertEqual(teammate.penetration_hits, 8)
        record = model.build_combat_record("party_wipe")
        teammate_record = next(
            row for row in record["participants"] if row["actor_id"] == TEAMMATE_ID
        )
        self.assertEqual(teammate_record["critical_rate"], 0.4)
        self.assertEqual(teammate_record["penetration_rate"], 0.8)
        self.assertEqual(teammate_record["deaths"], 1)
        validation = record["damage_accounting"]["stage_summary_validations"][0]
        self.assertTrue(validation["validation"]["wipe_detail_confirmed"])

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

    def test_two_live_bosses_share_one_encounter_and_end_after_both_die(self):
        model = CombatModel(
            run_id="simultaneous-boss-test",
            target_catalog=load_target_identity_catalog(),
        )
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, template_id, name, maximum in (
            (MONSTER_ID, 7_100_208, "安西娅", 37_590_045),
            (SECOND_MONSTER_ID, 7_100_209, "巴尼先生", 17_291_421),
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
                    "current_hp": maximum,
                    "max_hp": maximum,
                    "filetime_100ns": BASE_FILETIME,
                }
            )
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + 5_000,
                }
            )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        encounter_id = model.encounter_id
        self.assertEqual(
            [monster.entity_id for monster in model.current_bosses()],
            [MONSTER_ID, SECOND_MONSTER_ID],
        )
        model.ingest(damage(2, SELF_ID, SECOND_MONSTER_ID, 200_000))

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.combat_target_id, MONSTER_ID)
        self.assertEqual(
            [monster.entity_id for monster in model.current_bosses()],
            [MONSTER_ID, SECOND_MONSTER_ID],
        )
        self.assertEqual(
            model._encounter_damage_target_ids(),
            {MONSTER_ID, SECOND_MONSTER_ID},
        )
        self.assertEqual(model.stats[SELF_ID].damage, 300_000)

        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 1 * 10_000_000,
            }
        )
        self.assertFalse(model.combat_end_time)
        self.assertFalse(model._target_hp_depleted())
        self.assertTrue(model.active(model.last_damage_time + 1.0))
        self.assertEqual(
            [monster.entity_id for monster in model.current_bosses()],
            [MONSTER_ID, SECOND_MONSTER_ID],
        )

        # Reproduce the live two-bar transition: the first Boss leaves combat,
        # while the second remains alive, keeps its bar, and continues taking
        # damage in the same encounter.
        model.ingest_combat_state(
            {
                "entity_id": MONSTER_ID,
                "in_combat": False,
                "filetime_100ns": BASE_FILETIME + 11_000_000,
            }
        )
        model.ingest_monster(
            {
                "entity_id": SECOND_MONSTER_ID,
                "current_hp": 12_000_000,
                "filetime_100ns": BASE_FILETIME + 15_000_000,
            }
        )
        continued_hit = damage(3, SELF_ID, SECOND_MONSTER_ID, 300_000)
        continued_hit["filetime_100ns"] = BASE_FILETIME + 2 * 10_000_000
        model.ingest(continued_hit)

        self.assertFalse(model.combat_end_time)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats[SELF_ID].damage, 600_000)
        surviving_bosses = model.current_bosses()
        self.assertEqual(
            [monster.entity_id for monster in surviving_bosses],
            [MONSTER_ID, SECOND_MONSTER_ID],
        )
        self.assertEqual(surviving_bosses[0].current_hp, 0)
        self.assertEqual(surviving_bosses[1].current_hp, 12_000_000)

        model.ingest_monster(
            {
                "entity_id": SECOND_MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 4 * 10_000_000,
            }
        )
        self.assertFalse(model.combat_end_time)
        self.assertTrue(model._target_hp_depleted())
        model.ingest_combat_state(
            {
                "entity_id": SELF_ID,
                "in_combat": False,
                "filetime_100ns": BASE_FILETIME + 4 * 10_000_000 + 10_000,
            }
        )

        self.assertTrue(model.combat_end_time)
        self.assertEqual(model.combat_end_reason, "target_defeated")
        record = model.build_combat_record("test")
        self.assertEqual(record["total_damage"], 600_000)
        self.assertEqual(
            {target["entity_id"] for target in record["targets"]},
            {MONSTER_ID, SECOND_MONSTER_ID},
        )

        # The live client sent one stale positive HP sample immediately after
        # both explicit death packets. It must not revive either completed bar.
        model.ingest_monster(
            {
                "entity_id": SECOND_MONSTER_ID,
                "current_hp": 11_500_000,
                "filetime_100ns": BASE_FILETIME + 4 * 10_000_000 + 100_000,
            }
        )
        self.assertEqual(model.monsters[SECOND_MONSTER_ID].current_hp, 0)
        self.assertTrue(model.monsters[SECOND_MONSTER_ID].death_confirmed)

        old_session = model.session_number
        first_damage_time = model.first_damage_time
        self.assertTrue(model.finalize_if_idle())
        self.assertEqual(len(model.completed_combats), 1)
        model.ingest_active_boss(
            {
                "entity_id": THIRD_MONSTER_ID,
                "template_id": 7_100_210,
                "name": "错误的运行时名称",
                "max_hp": 35_297_052,
                "filetime_100ns": BASE_FILETIME + 24 * 10_000_000,
            }
        )

        self.assertEqual(model.session_number, old_session)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertFalse(model.combat_end_time)
        self.assertEqual(model.stats[SELF_ID].damage, 600_000)
        self.assertEqual(model.combat_target_id, THIRD_MONSTER_ID)
        self.assertEqual(model.pending_active_boss_id, None)
        self.assertEqual(
            [monster.entity_id for monster in model.current_bosses()],
            [THIRD_MONSTER_ID],
        )
        self.assertEqual(model.current_monster().name, "安西娅")
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(
            model._encounter_damage_target_ids(),
            {MONSTER_ID, SECOND_MONSTER_ID, THIRD_MONSTER_ID},
        )

        phase_two_hit = damage(4, SELF_ID, THIRD_MONSTER_ID, 400_000)
        phase_two_hit["filetime_100ns"] = BASE_FILETIME + 25 * 10_000_000
        model.ingest(phase_two_hit)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.stats[SELF_ID].damage, 1_000_000)
        self.assertGreater(model.duration(model.last_damage_time), 1.0)

    def test_verified_overlapping_hp_streams_link_active_second_boss(self):
        model = CombatModel(run_id="simultaneous-boss-hp-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, template_id, maximum in (
            (MONSTER_ID, 7_100_208, 37_590_045),
            (SECOND_MONSTER_ID, 7_100_209, 17_291_421),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": maximum,
                    "max_hp": maximum,
                    "filetime_100ns": BASE_FILETIME,
                }
            )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        second_hit = damage(2, SELF_ID, SECOND_MONSTER_ID, 200_000)
        second_hit["active_boss"] = True
        model.ingest(second_hit)

        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.stats[SELF_ID].damage, 300_000)
        self.assertEqual(
            [monster.entity_id for monster in model.current_bosses()],
            [MONSTER_ID, SECOND_MONSTER_ID],
        )

    def test_dual_boss_tank_on_partner_keeps_one_counter_and_clock(self):
        model = CombatModel(run_id="dual-boss-split-target-team-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID, NEARBY_ID],
                "member_count": 3,
                "authoritative": True,
            }
        )
        model.ingest_active_boss(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_100_208,
                "max_hp": 37_590_045,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest_profile(
            {
                "entity_id": SECOND_MONSTER_ID,
                "template_id": 7_100_209,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 37_590_045,
                "max_hp": 37_590_045,
                "filetime_100ns": BASE_FILETIME + 10_000,
            }
        )
        model.ingest_monster(
            {
                "entity_id": SECOND_MONSTER_ID,
                "current_hp": 17_291_421,
                "max_hp": 17_291_421,
                "filetime_100ns": BASE_FILETIME + 20_000,
            }
        )

        # This observer never receives the tank's per-hit callback for the
        # partner Boss. The verified overlapping HP streams must still bind
        # both targets before the first Boss dies.
        self.assertEqual(model.events, [])
        self.assertEqual(
            model._current_boss_target_ids(),
            {MONSTER_ID, SECOND_MONSTER_ID},
        )
        model.ingest_combat_state(
            {
                "entity_id": MONSTER_ID,
                "in_combat": True,
                "filetime_100ns": BASE_FILETIME + 30_000,
            }
        )
        for sequence, actor_id in enumerate(
            (SELF_ID, TEAMMATE_ID, NEARBY_ID), start=4
        ):
            baseline = team_stat(sequence, actor_id, 0, server_time=100)
            baseline["full_snapshot"] = True
            model.ingest_team_stat(baseline)
        model.ingest_team_stat(
            team_stat(10, TEAMMATE_ID, 200_000, server_time=100)
        )
        model.ingest_team_stat(
            team_stat(11, NEARBY_ID, 1_000_000, server_time=100)
        )
        encounter_id = model.encounter_id
        first_damage_time = model.first_damage_time

        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 60_000_000,
            }
        )
        model.ingest_combat_state(
            {
                "entity_id": MONSTER_ID,
                "in_combat": False,
                "filetime_100ns": BASE_FILETIME + 60_010_000,
            }
        )
        self.assertFalse(model.combat_end_time)

        # The game can start a fresh per-target Common epoch when everyone
        # switches to the surviving Boss. Carry each member's first-target
        # damage forward rather than presenting it as a restarted DPS row.
        for sequence, actor_id in enumerate(
            (TEAMMATE_ID, NEARBY_ID), start=6_002
        ):
            reset = team_stat(sequence, actor_id, 0, server_time=200)
            reset["full_snapshot"] = True
            reset["omitted_zero"] = True
            model.ingest_team_stat(reset)
        model.ingest_active_boss(
            {
                "entity_id": SECOND_MONSTER_ID,
                "template_id": 7_100_209,
                "max_hp": 17_291_421,
                "filetime_100ns": BASE_FILETIME + 61_000_000,
            }
        )
        model.ingest_team_stat(
            team_stat(6_101, TEAMMATE_ID, 50_000, server_time=201)
        )
        model.ingest_team_stat(
            team_stat(6_102, NEARBY_ID, 100_000, server_time=201)
        )

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)
        self.assertEqual(model.stats[NEARBY_ID].damage, 1_100_000)
        self.assertEqual(
            model.team_damage_states[TEAMMATE_ID].carried_damage,
            200_000,
        )
        self.assertEqual(model.pop_completed_combats(), [])

    def test_missing_dual_boss_profile_is_recovered_only_after_player_damage(self):
        model = CombatModel(
            run_id="missing-dual-boss-profile-test",
            target_catalog=load_target_identity_catalog(),
        )
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_100_209,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 17_291_421,
                "max_hp": 17_291_421,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest_monster(
            {
                "entity_id": SECOND_MONSTER_ID,
                "current_hp": 37_590_045,
                "max_hp": 37_590_045,
                "filetime_100ns": BASE_FILETIME + 15_000,
            }
        )

        self.assertIsNone(model.monsters[SECOND_MONSTER_ID].template_id)
        model.ingest(damage(2, SELF_ID, SECOND_MONSTER_ID, 200_000))

        partner = model.monsters[SECOND_MONSTER_ID]
        self.assertEqual(partner.template_id, 7_100_208)
        self.assertEqual(partner.name, "安西娅")
        self.assertEqual(partner.boss_type, 3)
        self.assertEqual(partner.boss_rank, 3)
        self.assertNotIn(SECOND_MONSTER_ID, model.encounter_add_target_ids)
        self.assertEqual(
            model.simultaneous_boss_target_ids,
            {MONSTER_ID, SECOND_MONSTER_ID},
        )
        self.assertEqual(model.stats[SELF_ID].damage, 300_000)

    def test_missing_dual_boss_profile_is_recovered_when_hp_arrives_late(self):
        model = CombatModel(
            run_id="late-dual-boss-hp-test",
            target_catalog=load_target_identity_catalog(),
        )
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_100_209,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, SELF_ID, SECOND_MONSTER_ID, 200_000))

        self.assertIn(SECOND_MONSTER_ID, model.encounter_add_target_ids)
        model.ingest_monster(
            {
                "entity_id": SECOND_MONSTER_ID,
                "current_hp": 37_590_045,
                "max_hp": 37_590_045,
                "filetime_100ns": BASE_FILETIME + 30_000,
            }
        )

        self.assertEqual(
            model.monsters[SECOND_MONSTER_ID].template_id, 7_100_208
        )
        self.assertNotIn(SECOND_MONSTER_ID, model.encounter_add_target_ids)
        self.assertEqual(
            model.simultaneous_boss_target_ids,
            {MONSTER_ID, SECOND_MONSTER_ID},
        )
        self.assertEqual(model.stats[SELF_ID].damage, 300_000)

    def test_dual_boss_member_counter_reset_preserves_only_that_members_damage(self):
        model = CombatModel(run_id="dual-boss-member-counter-reset-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        for entity_id, template_id, maximum in (
            (MONSTER_ID, 7_100_208, 37_590_045),
            (SECOND_MONSTER_ID, 7_100_209, 17_291_421),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": maximum,
                    "max_hp": maximum,
                    "filetime_100ns": BASE_FILETIME,
                }
            )
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + 5_000,
                }
            )
        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=1):
            baseline = team_stat(sequence, actor_id, 0, server_time=100)
            baseline["full_snapshot"] = True
            model.ingest_team_stat(baseline)
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 1_000))
        model.ingest_team_stat(team_stat(4, SELF_ID, 100_000, server_time=100))
        model.ingest_team_stat(
            team_stat(5, TEAMMATE_ID, 200_000, server_time=100)
        )
        encounter_id = model.encounter_id
        first_damage_time = model.first_damage_time

        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 60_000,
            }
        )
        model.ingest_team_stat(team_stat(7, SELF_ID, 0, server_time=200))

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200_000)
        self.assertEqual(model.team_damage_states[SELF_ID].carried_damage, 100_000)

        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": False,
                "filetime_100ns": BASE_FILETIME + 80_000,
            }
        )
        model.ingest_team_stat(team_stat(9, SELF_ID, 25_000, server_time=201))
        self.assertEqual(model.stats[SELF_ID].damage, 125_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200_000)

    def test_dual_boss_late_active_partner_keeps_all_phase_totals(self):
        model = CombatModel(run_id="dual-boss-late-active-partner-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_active_boss(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_100_208,
                "max_hp": 37_590_045,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest_profile(
            {
                "entity_id": SECOND_MONSTER_ID,
                "template_id": 7_100_209,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )

        def phase_stat(
            sequence: int,
            actor_id: int,
            damage_total: int,
            taken_total: int,
            healing_total: int,
            server_time: int,
        ) -> dict:
            update = team_stat(
                sequence,
                actor_id,
                damage_total,
                server_time=server_time,
            )
            update.update(
                {
                    "absolute_taken": taken_total,
                    "absolute_effective_healing": healing_total,
                    "full_snapshot": True,
                }
            )
            return update

        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=1):
            model.ingest_team_stat(
                phase_stat(sequence, actor_id, 0, 0, 0, 100)
            )
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 1_000))
        model.ingest_team_stat(
            phase_stat(4, SELF_ID, 100_000, 10_000, 5_000, 100)
        )
        model.ingest_team_stat(
            phase_stat(5, TEAMMATE_ID, 200_000, 20_000, 6_000, 100)
        )
        encounter_id = model.encounter_id
        first_damage_time = model.first_damage_time

        # The real client can switch its active-Boss pointer to the partner
        # long after both exact templates arrived, without refreshing either
        # HP stream. This is still the same verified two-Boss encounter.
        model.ingest_active_boss(
            {
                "entity_id": SECOND_MONSTER_ID,
                "template_id": 7_100_209,
                "filetime_100ns": BASE_FILETIME + 30 * 10_000_000,
            }
        )

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertFalse(model.combat_end_time)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(
            model._current_boss_target_ids(),
            {MONSTER_ID, SECOND_MONSTER_ID},
        )

        for sequence, actor_id in enumerate(
            (SELF_ID, TEAMMATE_ID), start=30_001
        ):
            model.ingest_team_stat(
                phase_stat(sequence, actor_id, 0, 0, 0, 200)
            )
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200_000)

        for sequence, entity_id in enumerate(
            (MONSTER_ID, SECOND_MONSTER_ID), start=60
        ):
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": 0,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME
                    + sequence * 10_000_000,
                }
            )
        for sequence, actor_id in enumerate(
            (SELF_ID, TEAMMATE_ID), start=62
        ):
            model.ingest_combat_state(
                {
                    "entity_id": actor_id,
                    "in_combat": False,
                    "filetime_100ns": BASE_FILETIME
                    + sequence * 10_000_000,
                }
            )
        self.assertTrue(model.combat_end_time)
        self.assertEqual(model.combat_end_reason, "target_defeated")
        self.assertFalse(
            model.finalize_if_idle(
                model.combat_end_time
                + MODULE["SIMULTANEOUS_BOSS_SUCCESSOR_WAIT_SECONDS"]
                - 1.0
            )
        )
        self.assertEqual(model.pop_completed_combats(), [])
        model.ingest_active_boss(
            {
                "entity_id": THIRD_MONSTER_ID,
                "template_id": 7_100_210,
                "max_hp": 35_297_052,
                "filetime_100ns": BASE_FILETIME + 90 * 10_000_000,
            }
        )
        model.ingest_team_stat(
            phase_stat(90_001, SELF_ID, 50_000, 3_000, 1_000, 201)
        )
        model.ingest_team_stat(
            phase_stat(90_002, TEAMMATE_ID, 80_000, 4_000, 2_000, 201)
        )

        record = model.build_combat_record("test")
        self.assertIsNotNone(record)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.combat_target_id, THIRD_MONSTER_ID)
        self.assertEqual(record["total_damage"], 430_000)
        self.assertEqual(record["team_taken"], 37_000)
        self.assertEqual(record["team_effective_healing"], 14_000)
        self.assertEqual(model.pop_completed_combats(), [])

    def test_dual_boss_phase_counter_reset_continues_into_successor(self):
        model = CombatModel(run_id="dual-boss-phase-counter-reset-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        for entity_id, template_id, maximum in (
            (MONSTER_ID, 7_100_208, 37_590_045),
            (SECOND_MONSTER_ID, 7_100_209, 17_291_421),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": maximum,
                    "max_hp": maximum,
                    "filetime_100ns": BASE_FILETIME,
                }
            )
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + 5_000,
                }
            )
        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=1):
            baseline = team_stat(sequence, actor_id, 0, server_time=100)
            baseline["full_snapshot"] = True
            model.ingest_team_stat(baseline)
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 1_000))
        model.ingest_team_stat(team_stat(4, SELF_ID, 100_000, server_time=100))
        model.ingest_team_stat(
            team_stat(5, TEAMMATE_ID, 200_000, server_time=100)
        )
        encounter_id = model.encounter_id
        first_damage_time = model.first_damage_time

        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 60_000,
            }
        )
        model.ingest_team_stat(team_stat(7, SELF_ID, 0, server_time=200))
        model.ingest_team_stat(team_stat(8, TEAMMATE_ID, 0, server_time=200))
        model.ingest_active_boss(
            {
                "entity_id": THIRD_MONSTER_ID,
                "template_id": 7_100_210,
                "max_hp": 35_297_052,
                "filetime_100ns": BASE_FILETIME + 90_000,
            }
        )

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.combat_target_id, THIRD_MONSTER_ID)
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 200_000)
        self.assertEqual(model.pop_completed_combats(), [])

        model.ingest_monster(
            {
                "entity_id": THIRD_MONSTER_ID,
                "current_hp": 35_297_052,
                "filetime_100ns": BASE_FILETIME + 100_000,
            }
        )
        model.ingest(damage(11, SELF_ID, THIRD_MONSTER_ID, 1_000))
        model.ingest_team_stat(team_stat(12, SELF_ID, 50_000, server_time=201))
        model.ingest_team_stat(
            team_stat(13, TEAMMATE_ID, 80_000, server_time=201)
        )

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats[SELF_ID].damage, 150_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 280_000)
        self.assertEqual(model.build_combat_record("test")["total_damage"], 430_000)

        model.ingest_monster(
            {
                "entity_id": THIRD_MONSTER_ID,
                "current_hp": 0,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 140_000,
            }
        )
        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=15):
            model.ingest_combat_state(
                {
                    "entity_id": actor_id,
                    "in_combat": False,
                    "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                }
            )
        self.assertEqual(model.combat_end_reason, "target_defeated")
        self.assertTrue(model.finalize_if_idle(model.combat_end_time))
        records = model.pop_completed_combats()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["encounter_id"], encounter_id)
        self.assertEqual(records[0]["total_damage"], 430_000)

    def test_true_wipe_still_resets_verified_dual_boss_team_counters(self):
        model = CombatModel(run_id="dual-boss-real-wipe-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        for entity_id, template_id in (
            (MONSTER_ID, 7_100_208),
            (SECOND_MONSTER_ID, 7_100_209),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + 5_000,
                }
            )
        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=1):
            baseline = team_stat(sequence, actor_id, 0, server_time=100)
            baseline["full_snapshot"] = True
            model.ingest_team_stat(baseline)
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 1_000))
        model.ingest_team_stat(team_stat(4, SELF_ID, 100_000, server_time=100))
        model.ingest_team_stat(
            team_stat(5, TEAMMATE_ID, 200_000, server_time=100)
        )
        encounter_id = model.encounter_id
        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=6):
            model.ingest_life(
                {
                    "actor_id": actor_id,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                }
            )
        self.assertEqual(model.combat_end_reason, "party_wipe")

        model.ingest_team_stat(team_stat(8, SELF_ID, 0, server_time=200))

        self.assertNotEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats, {})
        records = model.pop_completed_combats()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["archive_reason"], "party_wipe")
        self.assertEqual(records[0]["total_damage"], 300_000)
        self.assertEqual(model.team_damage_states[SELF_ID].carried_damage, 0)

        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=9):
            model.ingest_life(
                {
                    "actor_id": actor_id,
                    "dead": False,
                    "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                }
            )
        model.ingest(damage(11, SELF_ID, MONSTER_ID, 1_000))
        model.ingest_team_stat(team_stat(12, SELF_ID, 30_000, server_time=201))
        self.assertEqual(model.stats[SELF_ID].damage, 30_000)
        self.assertNotIn(TEAMMATE_ID, model.stats)

    def test_unverified_concurrent_bosses_do_not_gain_a_second_bar(self):
        model = CombatModel(run_id="simultaneous-boss-scope-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, template_id in (
            (MONSTER_ID, 7_200_031),
            (SECOND_MONSTER_ID, 7_200_032),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "name": f"Other Boss {template_id}",
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": 10_000_000,
                    "max_hp": 10_000_000,
                    "filetime_100ns": BASE_FILETIME,
                }
            )
            model.ingest_combat_state(
                {
                    "entity_id": entity_id,
                    "in_combat": True,
                    "filetime_100ns": BASE_FILETIME + 5_000,
                }
            )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))

        self.assertEqual(
            [monster.entity_id for monster in model.current_bosses()],
            [MONSTER_ID],
        )
        self.assertEqual(model.simultaneous_boss_target_ids, set())

    def test_fresh_bosslike_add_needs_active_or_concurrent_combat_evidence(self):
        model = CombatModel(run_id="simultaneous-boss-safety-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, template_id in (
            (MONSTER_ID, 7_200_021),
            (SECOND_MONSTER_ID, 7_200_022),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
            model.ingest_monster(
                {
                    "entity_id": entity_id,
                    "current_hp": 10_000_000,
                    "max_hp": 10_000_000,
                    "filetime_100ns": BASE_FILETIME,
                }
            )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        model.ingest(damage(2, SELF_ID, SECOND_MONSTER_ID, 900_000))

        self.assertEqual(model.stats[SELF_ID].damage, 100_000)
        self.assertEqual(
            [monster.entity_id for monster in model.current_bosses()],
            [MONSTER_ID],
        )

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

    def test_daily_karl_phases_archive_as_one_continuous_battle(self):
        """7107030 -> 7107081 keeps one history row and cumulative metrics."""

        model = CombatModel(run_id="daily-karl-phase-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": SELF_ID,
                "entity_type": "Player",
                "profession_id": 1_200_002,
            }
        )
        for entity_id, template_id, maximum in (
            (MONSTER_ID, 7_107_030, 8_760_000),
            (SECOND_MONSTER_ID, 7_107_081, 3_315_043),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": "卡尔·埃德加",
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                    "max_hp": maximum,
                }
            )

        opening_time = BASE_FILETIME + 10 * 10_000_000
        self.assertTrue(
            model.ingest_active_boss(
                {
                    "entity_id": MONSTER_ID,
                    "name": "卡尔·埃德加",
                    "template_id": 7_107_030,
                    "max_hp": 8_760_000,
                    "filetime_100ns": opening_time,
                }
            )
        )
        baseline = team_stat(1, SELF_ID, 0, server_time=100)
        baseline.update(
            {
                "filetime_100ns": opening_time - 10_000,
                "absolute_taken": 0,
                "absolute_effective_healing": 0,
                "full_snapshot": True,
            }
        )
        model.ingest_team_stat(baseline)
        model.ingest_combat_state(
            {
                "entity_id": MONSTER_ID,
                "in_combat": True,
                "filetime_100ns": opening_time,
            }
        )
        first_phase = team_stat(2, SELF_ID, 20_000_000, server_time=101)
        first_phase.update(
            {
                "filetime_100ns": opening_time + 20 * 10_000_000,
                "absolute_taken": 1_000,
                "absolute_effective_healing": 4_000,
            }
        )
        model.ingest_team_stat(first_phase)
        model.sample_team_damage(model.last_damage_time)

        encounter_id = model.encounter_id
        first_damage_time = model.first_damage_time
        self.assertFalse(model.finalize_if_idle(model.last_damage_time + 180.0))
        self.assertEqual(model.pop_completed_combats(), [])

        final_phase_time = opening_time + 240 * 10_000_000
        self.assertTrue(
            model.ingest_active_boss(
                {
                    "entity_id": SECOND_MONSTER_ID,
                    "name": "卡尔·埃德加",
                    "template_id": 7_107_081,
                    "max_hp": 3_315_043,
                    "filetime_100ns": final_phase_time,
                }
            )
        )
        phase_reset = team_stat(3, SELF_ID, 0, server_time=200)
        phase_reset.update(
            {
                "filetime_100ns": final_phase_time + 10_000,
                "absolute_taken": 0,
                "absolute_effective_healing": 0,
                "full_snapshot": True,
            }
        )
        model.ingest_team_stat(phase_reset)
        final_phase = team_stat(4, SELF_ID, 3_000_000, server_time=201)
        final_phase.update(
            {
                "filetime_100ns": final_phase_time + 25 * 10_000_000,
                "absolute_taken": 500,
                "absolute_effective_healing": 1_000,
            }
        )
        model.ingest_team_stat(final_phase)
        model.sample_team_damage(model.last_damage_time)

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.combat_target_id, SECOND_MONSTER_ID)
        self.assertEqual(model.stats[SELF_ID].damage, 23_000_000)
        self.assertEqual(model.team_taken_states[SELF_ID].accepted_taken, 1_500)
        self.assertEqual(
            model.team_healing_states[SELF_ID].accepted_effective_healing,
            5_000,
        )
        self.assertEqual(
            [total for _second, total in model.team_damage_samples],
            [20_000_000, 23_000_000],
        )

        model.combat_end_time = model.last_damage_time
        model.combat_end_reason = "target_defeated"
        self.assertTrue(model.finalize_if_idle(model.combat_end_time))
        records = model.pop_completed_combats()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["encounter_id"], encounter_id)
        self.assertEqual(records[0]["monster"]["template_id"], 7_107_081)
        self.assertEqual(records[0]["total_damage"], 23_000_000)
        self.assertEqual(records[0]["team_effective_healing"], 5_000)
        self.assertEqual(records[0]["team_taken"], 1_500)
        self.assertGreater(records[0]["duration_seconds"], 240.0)

    def test_daily_karl_scene_intermission_freezes_and_resumes_all_totals(self):
        """Scripted scene counters must not contaminate either Karl phase."""

        model = CombatModel(run_id="daily-karl-scene-intermission-test")
        model.ingest_scene({"scene_id": 5_200_189})
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": SELF_ID,
                "entity_type": "Player",
                "profession_id": 1_200_002,
            }
        )
        opening_time = BASE_FILETIME + 10 * 10_000_000
        model.ingest_active_boss(
            {
                "entity_id": MONSTER_ID,
                "name": "Karl Edgar",
                "template_id": 7_107_030,
                "max_hp": 8_760_000,
                "filetime_100ns": opening_time,
            }
        )
        baseline = team_stat(1, SELF_ID, 0, server_time=100)
        baseline.update(
            {
                "filetime_100ns": opening_time - 10_000,
                "absolute_taken": 0,
                "absolute_effective_healing": 0,
                "full_snapshot": True,
            }
        )
        model.ingest_team_stat(baseline)
        model.ingest_combat_state(
            {
                "entity_id": MONSTER_ID,
                "in_combat": True,
                "filetime_100ns": opening_time,
            }
        )
        first_phase = team_stat(2, SELF_ID, 20_000_000, server_time=101)
        first_phase.update(
            {
                "filetime_100ns": opening_time + 20 * 10_000_000,
                "absolute_taken": 1_000,
                "absolute_effective_healing": 4_000,
            }
        )
        model.ingest_team_stat(first_phase)
        encounter_id = model.encounter_id
        first_damage_time = model.first_damage_time

        scene_one_time = opening_time + 51 * 10_000_000
        self.assertTrue(
            model.ingest_scene(
                {
                    "scene_id": 5_200_186,
                    "filetime_100ns": scene_one_time,
                }
            )
        )
        self.assertTrue(model.long_gap_phase_suspended)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.pop_completed_combats(), [])

        intermission = team_stat(3, SELF_ID, 27_000_000, server_time=150)
        intermission.update(
            {
                "filetime_100ns": scene_one_time + 20 * 10_000_000,
                "absolute_taken": 9_000,
                "absolute_effective_healing": 12_000,
            }
        )
        model.ingest_team_stat(intermission)
        ignored_hit = damage(4, SELF_ID, MONSTER_ID, 999_999)
        ignored_hit["filetime_100ns"] = scene_one_time + 25 * 10_000_000
        model.ingest(ignored_hit)
        self.assertFalse(
            model.ingest_heal(
                healing(
                    scene_one_time + 30 * 10_000_000,
                    SELF_ID,
                    SELF_ID,
                    total=50_000,
                    effective=50_000,
                )
            )
        )
        self.assertEqual(model.stats[SELF_ID].damage, 20_000_000)
        self.assertEqual(model.team_taken_states[SELF_ID].accepted_taken, 1_000)
        self.assertEqual(
            model.team_healing_states[SELF_ID].accepted_effective_healing,
            4_000,
        )

        scene_two_time = opening_time + 147 * 10_000_000
        self.assertTrue(
            model.ingest_scene(
                {
                    "scene_id": 5_200_187,
                    "filetime_100ns": scene_two_time,
                }
            )
        )
        self.assertTrue(model.long_gap_phase_suspended)
        final_phase_time = opening_time + 240 * 10_000_000
        self.assertTrue(
            model.ingest_active_boss(
                {
                    "entity_id": SECOND_MONSTER_ID,
                    "name": "Karl Edgar",
                    "template_id": 7_107_081,
                    "max_hp": 3_315_043,
                    "filetime_100ns": final_phase_time,
                }
            )
        )
        self.assertFalse(model.long_gap_phase_suspended)
        self.assertEqual(model.encounter_id, encounter_id)

        phase_reset = team_stat(5, SELF_ID, 0, server_time=200)
        phase_reset.update(
            {
                "filetime_100ns": final_phase_time + 10_000,
                "absolute_taken": 0,
                "absolute_effective_healing": 0,
                "full_snapshot": True,
            }
        )
        model.ingest_team_stat(phase_reset)
        final_phase = team_stat(6, SELF_ID, 3_000_000, server_time=201)
        final_phase.update(
            {
                "filetime_100ns": final_phase_time + 25 * 10_000_000,
                "absolute_taken": 500,
                "absolute_effective_healing": 1_000,
            }
        )
        model.ingest_team_stat(final_phase)

        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.stats[SELF_ID].damage, 23_000_000)
        self.assertEqual(model.team_taken_states[SELF_ID].accepted_taken, 1_500)
        self.assertEqual(
            model.team_healing_states[SELF_ID].accepted_effective_healing,
            5_000,
        )
        model.combat_end_time = model.last_damage_time
        model.combat_end_reason = "target_defeated"
        self.assertTrue(model.finalize_if_idle(model.combat_end_time))
        records = model.pop_completed_combats()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["encounter_id"], encounter_id)
        self.assertEqual(records[0]["monster"]["template_id"], 7_107_081)
        self.assertEqual(records[0]["total_damage"], 23_000_000)
        self.assertEqual(records[0]["team_effective_healing"], 5_000)
        self.assertEqual(records[0]["team_taken"], 1_500)
        self.assertGreater(records[0]["duration_seconds"], 240.0)

    def test_daily_karl_scene_intermission_exit_archives_opening_once(self):
        model = CombatModel(run_id="daily-karl-scene-exit-test")
        model.ingest_scene({"scene_id": 5_200_189})
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "Karl Edgar",
                "template_id": 7_107_030,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        encounter_id = model.encounter_id
        model.ingest_scene(
            {
                "scene_id": 5_200_186,
                "filetime_100ns": BASE_FILETIME + 60 * 10_000_000,
            }
        )
        self.assertTrue(model.long_gap_phase_suspended)

        self.assertTrue(
            model.ingest_scene(
                {
                    "transition": True,
                    "filetime_100ns": BASE_FILETIME + 90 * 10_000_000,
                }
            )
        )
        records = model.pop_completed_combats()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["encounter_id"], encounter_id)
        self.assertEqual(records[0]["total_damage"], 100_000)
        self.assertEqual(records[0]["archive_reason"], "space_transition")
        self.assertFalse(model.long_gap_phase_suspended)

    def test_daily_karl_scene_intermission_unmapped_boss_starts_new_battle(self):
        model = CombatModel(run_id="daily-karl-scene-unmapped-test")
        model.ingest_scene({"scene_id": 5_200_189})
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "Karl Edgar",
                "template_id": 7_107_030,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        opening_encounter_id = model.encounter_id
        model.ingest_scene(
            {
                "scene_id": 5_200_186,
                "filetime_100ns": BASE_FILETIME + 60 * 10_000_000,
            }
        )
        model.ingest_active_boss(
            {
                "entity_id": SECOND_MONSTER_ID,
                "name": "Unmapped Boss",
                "template_id": 7_107_084,
                "filetime_100ns": BASE_FILETIME + 120 * 10_000_000,
            }
        )
        next_hit = damage(2, SELF_ID, SECOND_MONSTER_ID, 200_000)
        next_hit["filetime_100ns"] = BASE_FILETIME + 121 * 10_000_000
        model.ingest(next_hit)

        self.assertFalse(model.long_gap_phase_suspended)
        self.assertNotEqual(model.encounter_id, opening_encounter_id)
        self.assertEqual(model.stats[SELF_ID].damage, 200_000)
        records = model.pop_completed_combats()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["encounter_id"], opening_encounter_id)
        self.assertEqual(records[0]["total_damage"], 100_000)

    def test_daily_karl_scene_intermission_life_noise_does_not_fake_wipe(self):
        model = CombatModel(run_id="daily-karl-scene-life-noise-test")
        model.ingest_scene({"scene_id": 5_200_189})
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
                "name": "Karl Edgar",
                "template_id": 7_107_030,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        encounter_id = model.encounter_id
        model.ingest_scene(
            {
                "scene_id": 5_200_186,
                "filetime_100ns": BASE_FILETIME + 60 * 10_000_000,
            }
        )
        self.assertTrue(model.long_gap_phase_suspended)
        for offset, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=61):
            model.ingest_life(
                {
                    "actor_id": actor_id,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + offset * 10_000_000,
                }
            )
        self.assertEqual(model.combat_end_reason, "")
        self.assertEqual(model.member_death_counts, {})

        final_time = BASE_FILETIME + 240 * 10_000_000
        model.ingest_active_boss(
            {
                "entity_id": SECOND_MONSTER_ID,
                "name": "Karl Edgar",
                "template_id": 7_107_081,
                "filetime_100ns": final_time,
            }
        )
        final_hit = damage(3, SELF_ID, SECOND_MONSTER_ID, 200_000)
        final_hit["filetime_100ns"] = final_time + 10_000
        model.ingest(final_hit)

        self.assertFalse(model.long_gap_phase_suspended)
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats[SELF_ID].damage, 300_000)
        self.assertEqual(model.pop_completed_combats(), [])

    def test_unmapped_same_name_boss_after_daily_karl_starts_new_battle(self):
        model = CombatModel(run_id="daily-karl-unmapped-successor-test")
        model.ingest_identity({"entity_id": SELF_ID})
        for entity_id, template_id in (
            (MONSTER_ID, 7_107_030),
            (SECOND_MONSTER_ID, 7_107_084),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": "卡尔·埃德加",
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )

        first = damage(1, SELF_ID, MONSTER_ID, 100_000)
        first["filetime_100ns"] = BASE_FILETIME
        second = damage(2, SELF_ID, SECOND_MONSTER_ID, 200_000)
        second["filetime_100ns"] = BASE_FILETIME + 240 * 10_000_000
        model.ingest(first)
        first_encounter_id = model.encounter_id
        model.ingest(second)

        self.assertNotEqual(model.encounter_id, first_encounter_id)
        self.assertEqual(model.stats[SELF_ID].damage, 200_000)
        previous = model.pop_completed_combats()
        self.assertEqual(len(previous), 1)
        self.assertEqual(previous[0]["monster"]["template_id"], 7_107_030)
        self.assertEqual(previous[0]["total_damage"], 100_000)

    def test_daily_karl_final_form_does_not_reopen_a_wiped_attempt(self):
        model = CombatModel(run_id="daily-karl-wipe-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        for entity_id, template_id in (
            (MONSTER_ID, 7_107_030),
            (SECOND_MONSTER_ID, 7_107_081),
        ):
            model.ingest_profile(
                {
                    "entity_id": entity_id,
                    "name": "卡尔·埃德加",
                    "template_id": template_id,
                    "entity_type": "Boss",
                    "boss_type": 3,
                    "boss_rank": 3,
                }
            )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100_000))
        wiped_encounter = model.encounter_id
        for offset, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=2):
            model.ingest_life(
                {
                    "actor_id": actor_id,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + offset * 10_000,
                }
            )
        self.assertEqual(model.combat_end_reason, "party_wipe")

        final_time = BASE_FILETIME + 240 * 10_000_000
        model.ingest_active_boss(
            {
                "entity_id": SECOND_MONSTER_ID,
                "name": "卡尔·埃德加",
                "template_id": 7_107_081,
                "filetime_100ns": final_time,
            }
        )
        final_hit = damage(4, SELF_ID, SECOND_MONSTER_ID, 200_000)
        final_hit["filetime_100ns"] = final_time + 10_000
        model.ingest(final_hit)

        self.assertNotEqual(model.encounter_id, wiped_encounter)
        self.assertEqual(model.stats[SELF_ID].damage, 200_000)
        records = model.pop_completed_combats()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["encounter_id"], wiped_encounter)
        self.assertEqual(records[0]["archive_reason"], "party_wipe")
        self.assertEqual(records[0]["total_damage"], 100_000)

    def test_unknown_midfight_ancestor_to_baldwin_preserves_shared_encounter(self):
        """A missed P1 template must not split the pull when P2 is identified."""
        model = CombatModel(
            run_id="unknown-ancestor-phase-test",
            target_catalog={
                "7103402": {"boss_type": 3, "name": "先祖铠甲", "level": 62},
                "7103401": {
                    "boss_type": 3,
                    "name": "伯德温·威瑟尔",
                    "level": 62,
                },
            },
        )
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        model.ingest_dungeon_context(
            {
                "dungeon_stage_id": 5_150_058,
                "dungeon_stage_phase": 1,
                "dungeon_context_filetime": BASE_FILETIME,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "Boss",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 100_000))
        encounter_id = model.encounter_id
        start_signal = model.encounter_start_signal_100ns
        first_damage_time = model.first_damage_time
        model.shared_clock_id = "shared-clock-before-phase"
        model.shared_clock_started_at = first_damage_time
        model.shared_clock_duration_seconds = 65.0
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 2 * 10_000_000,
            }
        )
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": False,
                "filetime_100ns": BASE_FILETIME + 8 * 10_000_000,
            }
        )
        model.ingest_profile(
            {
                "entity_id": SECOND_MONSTER_ID,
                "name": "伯德温·威瑟尔",
                "entity_type": "Boss",
                "template_id": 7_103_401,
                "boss_type": 3,
                "boss_rank": 3,
            }
        )

        self.assertTrue(
            model.ingest_active_boss(
                {
                    "entity_id": SECOND_MONSTER_ID,
                    "template_id": 7_103_401,
                    "name": "伯德温·威瑟尔",
                    "filetime_100ns": BASE_FILETIME + 9 * 10_000_000,
                }
            )
        )

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.session_number, 0)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.combat_target_id, SECOND_MONSTER_ID)
        self.assertEqual(model.monsters[MONSTER_ID].template_id, 7_103_402)
        self.assertEqual(model.monsters[MONSTER_ID].name, "先祖铠甲")
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 100_000)
        self.assertEqual(model.encounter_start_signal_100ns, start_signal)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.shared_clock_id, "shared-clock-before-phase")
        self.assertEqual(model.shared_clock_started_at, first_damage_time)
        self.assertEqual(model.member_death_counts[SELF_ID], 1)
        self.assertEqual(model.member_revive_counts[SELF_ID], 1)

        model.ingest(damage(10_001, SELF_ID, SECOND_MONSTER_ID, 200_000))
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 100_000)
        self.assertEqual(model.stats[SELF_ID].damage, 200_000)

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

    def test_lost_control_lokin_opening_counter_epoch_stays_one_encounter(self):
        model = CombatModel(run_id="lokin-opening-counter-rebase-test")
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
                "template_id": 7_110_641,
                "name": "洛克·金·失控",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
            }
        )
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 2_161_985,
                "max_hp": 2_161_985,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        for sequence, actor_id, previous_total in (
            (1, SELF_ID, 3_700_000),
            (2, TEAMMATE_ID, 1_400_000),
        ):
            snapshot = team_stat(
                sequence, actor_id, previous_total, server_time=100
            )
            model.ingest_team_stat(snapshot)

        model.ingest(damage(3, SELF_ID, MONSTER_ID, 1_000))
        encounter_id = model.encounter_id
        started_at = model.first_damage_time
        model.ingest_team_stat(team_stat(4, SELF_ID, 3_750_000, server_time=100))
        self.assertEqual(model.stats[SELF_ID].damage, 50_000)

        model.ingest_team_stat(team_stat(5, SELF_ID, 40_000, server_time=200))
        model.ingest_team_stat(
            team_stat(6, TEAMMATE_ID, 70_000, server_time=200)
        )

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.first_damage_time, started_at)
        self.assertEqual(model.combat_target_id, MONSTER_ID)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.stats[SELF_ID].damage, 40_000)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 70_000)
        self.assertEqual(model.build_combat_record("test")["total_damage"], 110_000)

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

    def test_v013_keeps_feedback_update_lock_and_fixed_target_scope(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertEqual(APP_VERSION, "0.2.2")
        self.assertEqual(CLIENT_BUILD, "0.2.2+20260909.1")
        self.assertNotIn('self.config["topmost"] = True', source)
        self.assertNotIn("toggle_boss_only", source)
        self.assertNotIn('self.footer, "只读 BOSS"', source)
        self.assertIn('(\"反馈\", self._feedback_selected_history)', source)
        self.assertIn(
            'heading, "检查更新", self._update_page_action', source
        )
        self.assertIn("UPDATE_DIR = APP_DIR", source)
        self.assertIn('text=f"保存位置：{UPDATE_DIR}"', source)
        self.assertIn('ModernCheckControl(', source)
        self.assertIn('"附带运行摘要"', source)
        self.assertNotIn("load_recent_restart_context", source)
        self.assertNotIn("restart_target_profiles", source)
        self.assertIn("entity_names={}", source)
        self.assertIn("self.lock_button = self._main_icon_button(", source)
        self.assertIn('tags=("compact_lock", "compact_lock_bg")', source)
        self.assertIn("click_through_applied = self._set_window_click_through(", source)
        self.assertIn("get_ancestor = user32.GetAncestor", source)
        self.assertIn('locked_icon = self.icons.toolbar("lock", 16, ACCENT)', source)
        self.assertIn('self.config["window_locked"] = self.window_locked', source)
        self.assertNotIn("不包含完整封包", source)
        self.assertNotIn("分享功能将在后续版本开放", source)
        self.assertIn('actions, "share", "分享", self._share_current', source)
        self.assertNotIn("clear_if_expired", source)
        self.assertIn('"critical": "暴击率"', source)
        self.assertIn('("显示死亡次数", tk.BooleanVar', source)
        self.assertIn('"critical": critical_text', source)
        self.assertIn('("hps", "HPS治疗", True)', source)
        self.assertNotIn('"HDPS"', source)
        self.assertIn(
            "self.titlebar = tk.Frame(self.body, bg=BG, height=44)",
            source,
        )
        self.assertIn("self.body, bg=BG, height=MAIN_SUMMARY_BASE_HEIGHT", source)
        self.assertIn("height=MONSTER_HP_ROW_HEIGHT,\n            bg=BG,", source)
        self.assertIn("+ MONSTER_HP_ROW_HEIGHT * (row_count - 1)", source)
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
            "draw_content=False",
            source,
        )
        self.assertNotIn("opaque_background", source)
        self.assertIn('"DPS颜色条保持不透明"', source)
        self.assertIn(
            'self.config.get("keep_dps_bars_opaque", False)', source
        )
        self.assertIn('uniform="dps_settings"', source)
        self.assertIn('"dps": "秒伤"', source)
        self.assertIn('("显示秒伤", self.settings_show_dps_var, False)', source)
        self.assertIn('self.config["show_dps"] = self.show_dps', source)
        self.assertIn('("hps", "HPS治疗设置")', source)
        self.assertIn(
            '("显示有效治疗", self.settings_show_effective_healing_var)',
            source,
        )
        self.assertIn('("显示 HPS", self.settings_show_hps_var)', source)
        self.assertIn(
            '("显示过量率", self.settings_show_overheal_rate_var)', source
        )
        self.assertNotIn("settings_show_healing_response_var", source)
        self.assertNotIn('self.config["show_healing_response"] =', source)
        self.assertIn('self.config["layout_version"] = 15', source)
        self.assertIn("class ModernSlider(tk.Canvas):", source)
        self.assertIn("class ModernScrollbar(tk.Canvas):", source)
        self.assertNotIn("tk.Scrollbar(", source)
        self.assertNotIn("ttk.Scrollbar(", source)
        self.assertGreaterEqual(source.count("ModernScrollbar("), 6)
        self.assertIn('text="主窗口透明度"', source)
        self.assertIn('team_caption.configure(text="团队 DPS")', source)
        self.assertIn('actions, "compact", "迷你模式"', source)
        self.assertIn('actions, "menu", "主菜单", self.show_main_menu', source)
        self.assertIn('def show_main_menu(self)', source)
        self.assertIn("MAIN_MIN_WIDTH = 430", source)
        self.assertIn("MINI_DEFAULT_WIDTH = 340", source)
        self.assertIn("MINI_MIN_WIDTH = 228", source)
        self.assertIn("MINI_DEFAULT_HEIGHT = 118", source)
        self.assertIn('tags=("compact_restore", "compact_restore_bg")', source)
        self.assertNotIn('text="当前身份"', source)
        self.assertIn("membership_badge = membership_label_for_card_tier(", source)
        self.assertIn("self.licensing.session.card_tier", source)
        self.assertNotIn('identity_icon = self.icons.toolbar("user"', source)
        self.assertIn('self.root.bind("<MouseWheel>", self._scroll_main, add="+")', source)
        self.assertIn("self._dismiss_compact_auxiliary_windows()", source)
        nav_source = source[
            source.index("    def _backend_nav_button(") : source.index(
                "    def _sync_backend_navigation("
            )
        ]
        self.assertIn("caption_label = tk.Label(", nav_source)
        self.assertIn("image=icon_image", nav_source)
        self.assertIn('self.icons.toolbar(', nav_source)
        self.assertNotIn('text=f"{icon_name}   {caption}"', nav_source)
        checkbox_source = source[
            source.index("    def _settings_check_row(") : source.index(
                "    def _select_settings_section("
            )
        ]
        self.assertIn("ModernCheckControl(", checkbox_source)
        self.assertIn("cell.grid(", checkbox_source)
        self.assertIn("row=row", checkbox_source)
        self.assertIn("column=column", checkbox_source)
        self.assertNotIn("tk.Checkbutton(", checkbox_source)
        switch_source = source[
            source.index("class ModernCheckControl(") : source.index(
                "class ModernSlider("
            )
        ]
        help_source = source[
            source.index("class ModernHelpBadge(") : source.index(
                "class ModernCheckControl("
            )
        ]
        self.assertIn("width=44", switch_source)
        self.assertIn("height=26", switch_source)
        self.assertIn("self.help_badge = ModernHelpBadge(", switch_source)
        self.assertIn("class ModernHelpBadge(tk.Canvas):", help_source)
        self.assertIn("def _show_tooltip(", help_source)
        self.assertNotIn("checkbox", switch_source.casefold())
        main_rows_source = source[
            source.index("    def _draw_main_rows_on_canvas(") : source.index(
                "    def _drag_start("
            )
        ]
        self.assertNotIn("show_skill_details(", main_rows_source)
        self.assertNotIn("cursor=\"hand2\"", main_rows_source)
        self.assertNotIn("main_row_detail_targets", source)

    def test_login_requires_disclaimer_for_mouse_and_enter_submission_path(self):
        class BooleanValue:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Entry:
            @staticmethod
            def get():
                return "GMZZTESTCARD"

        class Licensing:
            def __init__(self):
                self.calls = []

            def sign_in_card(self, card_key):
                self.calls.append(card_key)
                raise AssertionError("login must not reach the server")

        window = object.__new__(DpsWindow)
        window.closing = False
        window.authorization_resetting = False
        window.login_disclaimer_var = BooleanValue(False)
        window.login_card_entry = Entry()
        window.login_placeholder_active = False
        window.login_button = None
        window.login_window = None
        window.login_status_label = None
        window.login_status_message = ""
        window.licensing = Licensing()

        window._complete_card_login()

        self.assertEqual(window.licensing.calls, [])
        self.assertIn("用户须知", window.login_status_message)
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'window.bind("<Return>", lambda _event: self._complete_card_login())',
            source,
        )
        self.assertIn("本工具仅用于战斗数据统计与分析。", source)
        self.assertIn('text="《用户须知》"', source)
        self.assertIn("self._show_user_notice()", source)
        login_source = source[
            source.index("    def _show_login(") : source.index(
                "    def _show_login_placeholder("
            )
        ]
        self.assertNotIn("disclaimer_panel", login_source)
        self.assertNotIn("tk.Checkbutton(", login_source)
        self.assertIn("disclaimer_check = tk.Canvas(", login_source)
        self.assertIn("width=px(22)", login_source)
        self.assertIn("height=px(22)", login_source)
        self.assertIn('fill = ACCENT if checked else PANEL_2', login_source)
        self.assertIn('"<ButtonRelease-1>"', login_source)
        self.assertNotIn("if self.login_status_message:", login_source)
        self.assertIn(
            'self.login_status_label.pack(fill="x", pady=(px(3), px(3)))',
            login_source,
        )
        self.assertIn("LOGIN_ADMIN_REMINDER", login_source)

        self.assertIn(
            'notice_consent.pack(fill="x", pady=(px(6), px(8)))',
            login_source,
        )
        self.assertNotIn(
            'self.login_status_label.pack(fill="x", pady=(5, 5))',
            login_source,
        )
        self.assertNotIn(
            "for widget in (notice_consent, disclaimer_check, consent_label)",
            login_source,
        )
        self.assertIn("login_window_dimensions(", login_source)
        self.assertIn("dpi = get_window_dpi(window)", login_source)
        self.assertIn("window.withdraw()", login_source)
        self.assertIn("fitted_height = max(height, window.winfo_reqheight())", login_source)
        self.assertIn("titlebar = tk.Frame(body, bg=SURFACE, height=px(44))", login_source)
        self.assertIn('login_group_label = tk.Label(', login_source)
        self.assertIn('text="QQ群:1094925831 165966739"', login_source)
        self.assertIn(
            'login_group_label.pack(fill="x", pady=(0, px(5)))',
            login_source,
        )
        self.assertNotIn("login_group_label = tk.Label(\n            titlebar,", login_source)
        self.assertIn("login_group_label = tk.Label(\n            content,", login_source)
        self.assertNotIn("login_group = tk.Frame(field", login_source)
        login_builder_source = source[
            source.index("    def _show_login(") : source.index(
                "    def _finish_login_window_presentation("
            )
        ]
        self.assertIn("*, activate: bool = True", login_builder_source)
        self.assertIn('window.attributes("-topmost", False)', login_builder_source)
        self.assertNotIn('window.attributes("-topmost", True)', login_builder_source)
        self.assertNotIn("window.grab_set()", login_builder_source)
        self.assertIn('"minimize"', login_builder_source)
        self.assertIn("self._sync_login_window_taskbar(", login_builder_source)

        return_source = source[
            source.index("    def _finish_return_to_login(") : source.index(
                "    @staticmethod\n    def _label_button("
            )
        ]
        self.assertIn("self._show_login(activate=False)", return_source)

    def test_login_capability_error_never_exposes_technical_details(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        login_source = source[
            source.index("    def _complete_card_login(") : source.index(
                "    def _start_update_check("
            )
        ]

        self.assertIn("SYSTEM_TIME_SYNC_MESSAGE", login_source)
        self.assertIn("RuntimeCapabilityTimeError", login_source)
        self.assertIn("else CARD_LOGIN_FAILURE_MESSAGE", login_source)
        self.assertNotIn("采集授权不可用：", login_source)
        self.assertNotIn("服务器返回的采集授权无效：", login_source)

    def test_trial_claim_fills_login_card_without_bypassing_login(self):
        class Entry:
            def __init__(self):
                self.value = "请输入卡号"
                self.foreground = ""
                self.selected = None
                self.cursor = None
                self.focused = False

            @staticmethod
            def winfo_exists():
                return True

            def delete(self, _start, _end):
                self.value = ""

            def configure(self, **values):
                self.foreground = values.get("fg", self.foreground)

            def insert(self, _index, value):
                self.value = value

            def selection_range(self, start, end):
                self.selected = (start, end)

            def icursor(self, value):
                self.cursor = value

            def focus_set(self):
                self.focused = True

        window = object.__new__(DpsWindow)
        window.trial_claim_in_progress = True
        window.login_trial_button = None
        window.login_card_entry = Entry()
        window.login_status_label = None
        window.login_status_message = ""
        window.login_placeholder_active = True
        card_key = "GMZZ" + "A" * 26

        window._handle_trial_claim_result(
            TrialClaim(
                True,
                "2 小时试用卡领取成功，请点击“登录”。",
                card_key=card_key,
                duration_seconds=7200,
            )
        )

        self.assertFalse(window.trial_claim_in_progress)
        self.assertEqual(window.login_card_entry.value, card_key)
        self.assertEqual(window.login_card_entry.selected, (0, "end"))
        self.assertEqual(window.login_card_entry.cursor, "end")
        self.assertTrue(window.login_card_entry.focused)
        self.assertFalse(window.login_placeholder_active)
        self.assertIn("2 小时", window.login_status_message)

        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        login_source = source[
            source.index("    def _show_login(") : source.index(
                "    def _show_login_placeholder("
            )
        ]
        self.assertIn('text="获取试用"', login_source)
        self.assertIn(
            'self.login_trial_button.pack(side="right", fill="y")',
            login_source,
        )
        claim_source = source[
            source.index("    def _request_trial_card(") : source.index(
                "    def _complete_card_login("
            )
        ]
        self.assertIn('name="trial-card-claim"', claim_source)
        self.assertNotIn("_complete_card_login()", claim_source)
        self.assertNotIn("TrialClaim(False, str(exc))", claim_source)
        self.assertIn("TRIAL_CLAIM_FAILURE_MESSAGE", claim_source)

        window._handle_trial_claim_result(object())
        self.assertEqual(
            window.login_status_message,
            "获取失败，请群内联系群主或管理员。",
        )

    def test_backend_geometry_restores_saved_size_and_enforces_minimum(self):
        window = object.__new__(DpsWindow)
        window._visible_geometry = lambda value, _width, _height: value

        window.config = {"history_geometry": "1050x680-1750+90"}
        self.assertEqual(
            window._initial_history_geometry(24, 24),
            "1050x680+-1750+90",
        )

        window.config = {"history_geometry": "700x420+12-8"}
        self.assertEqual(
            window._initial_history_geometry(24, 24),
            "900x600+12+-8",
        )

        window.config = {"history_geometry": "invalid"}
        self.assertEqual(
            window._initial_history_geometry(-120, 36),
            "1180x760+-120+36",
        )

    def test_multimonitor_geometry_is_preserved_until_fully_offscreen(self):
        work_areas = [
            (-1920, 0, 0, 1040),
            (0, 0, 1920, 1040),
            (1920, 0, 3840, 1040),
        ]

        self.assertEqual(
            clamp_geometry_to_work_areas(590, 400, -1800, 120, work_areas),
            (590, 400, -1800, 120),
        )
        self.assertEqual(
            clamp_geometry_to_work_areas(590, 400, 2200, 120, work_areas),
            (590, 400, 2200, 120),
        )
        self.assertEqual(
            clamp_geometry_to_work_areas(590, 400, 1700, 120, work_areas),
            (590, 400, 1700, 120),
        )
        self.assertEqual(
            clamp_geometry_to_work_areas(590, 400, -5000, 120, work_areas),
            (590, 400, -1920, 120),
        )

    def test_help_popup_stays_on_badge_monitor_with_negative_coordinates(self):
        work_area = (-1920, 0, 0, 1040)

        self.assertEqual(
            help_popup_position(-45, 120, 18, 18, 300, 90, work_area),
            (-353, 143),
        )
        x, y = help_popup_position(-1915, 990, 18, 18, 300, 90, work_area)

        self.assertGreaterEqual(x, -1916)
        self.assertLessEqual(x + 300, -4)
        self.assertGreaterEqual(y, 4)
        self.assertLessEqual(y + 90, 1036)

    def test_dropdown_opens_upward_and_limits_height_on_current_monitor(self):
        work_area = (1920, -200, 3200, 824)

        self.assertEqual(
            dropdown_popup_bounds(2960, 760, 220, 34, 360, 322, work_area),
            (2836, 436, 360, 322),
        )
        x, y, width, height = dropdown_popup_bounds(
            2060, 250, 220, 34, 360, 900, work_area
        )

        self.assertGreaterEqual(x, 1924)
        self.assertLessEqual(x + width, 3196)
        self.assertGreaterEqual(y, -196)
        self.assertLessEqual(y + height, 820)
        self.assertLess(height, 900)

    def test_drag_end_does_not_schedule_a_position_adjustment(self):
        class Root:
            @staticmethod
            def winfo_exists():
                return True

        window = object.__new__(DpsWindow)
        window.root = Root()
        window.closing = False
        window.drag_state = {id(window.root): (12, 8)}
        window._remember_root_geometry = mock.Mock()
        window._apply_window_dpi_if_changed = mock.Mock(return_value=True)

        window._drag_end(None, window.root)

        self.assertNotIn(id(window.root), window.drag_state)
        window._remember_root_geometry.assert_called_once_with()
        window._apply_window_dpi_if_changed.assert_not_called()

    def test_tray_restore_resets_window_geometry_mode_and_lock(self):
        class Root:
            def __init__(self):
                self.geometry_calls = []
                self.minsize_calls = []

            def geometry(self, value):
                self.geometry_calls.append(value)

            def minsize(self, width, height):
                self.minsize_calls.append((width, height))

        root = Root()
        window = object.__new__(DpsWindow)
        window.root = root
        window.closing = False
        window.window_locked = True
        window.compact_mode = True
        window.window_alpha = 0.0
        window.config = {
            "window_locked": True,
            "compact_mode": True,
            "alpha": 0.0,
            "geometry": "590x400+20+-390",
            "compact_geometry": "340x118+20+-110",
        }
        window.drag_state = {id(root): (8, 8)}
        window.resize_state = {id(root): (0, 0, 340, 118)}
        window.restore_geometry = {id(root): "340x118+20+-110"}
        window.login_window = None
        window._flush_window_geometry = mock.Mock()
        window._destroy_unlock_window = mock.Mock()
        window._apply_layout_mode = mock.Mock()
        window._main_minimum_width = mock.Mock(return_value=778)
        window._compact_target_width = mock.Mock(return_value=610)
        window.show_from_tray = mock.Mock()

        with mock.patch.dict(
            DpsWindow.restore_window_from_tray.__globals__,
            {"save_config": mock.Mock()},
        ):
            window.restore_window_from_tray()

        self.assertFalse(window.window_locked)
        self.assertFalse(window.compact_mode)
        self.assertFalse(window.config["window_locked"])
        self.assertFalse(window.config["compact_mode"])
        self.assertEqual(window.window_alpha, 1.0)
        self.assertEqual(window.config["alpha"], 1.0)
        self.assertEqual(window.config["geometry"], "778x400+32+120")
        self.assertEqual(
            window.config["compact_geometry"], "610x118+32+120"
        )
        self.assertEqual(root.geometry_calls, ["778x400+32+120"])
        self.assertEqual(root.minsize_calls, [(778, MAIN_MIN_HEIGHT)])
        self.assertNotIn(id(root), window.drag_state)
        self.assertNotIn(id(root), window.resize_state)
        self.assertNotIn(id(root), window.restore_geometry)
        window._apply_layout_mode.assert_called_once_with()
        window.show_from_tray.assert_called_once_with()

    def test_tray_restore_action_is_dispatched(self):
        window = object.__new__(DpsWindow)
        window.restore_window_from_tray = mock.Mock()

        window._dispatch_message("tray_restore_window", None)

        window.restore_window_from_tray.assert_called_once_with()
        tray_source = Path(__file__).with_name("windows_tray.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('CMD_RESTORE_WINDOW = 1003', tray_source)
        self.assertIn('"恢复窗口"', tray_source)
        self.assertIn('self._emit("tray_restore_window")', tray_source)

    def test_skill_cast_message_is_dispatched_only_to_cast_history_channel(self):
        window = object.__new__(DpsWindow)
        window.model = mock.Mock()
        payload = {
            "skill_id": 86_021_030,
            "source_method": "RetCastSkillSuccessNew",
        }

        window._dispatch_message("skill_cast", payload)

        window.model.ingest_skill_cast.assert_called_once_with(payload)
        window.model.ingest_boss_damage.assert_not_called()
        window.model.ingest_heal.assert_not_called()

    def test_visibility_hotkeys_normalize_and_map_to_windows(self):
        self.assertEqual(normalize_toggle_hotkey("home"), "Home")
        self.assertEqual(normalize_toggle_hotkey("alt+h"), "Alt+H")
        self.assertEqual(
            normalize_toggle_hotkey("shift+alt+control+f2"),
            "Ctrl+Alt+Shift+F2",
        )
        self.assertEqual(toggle_hotkey_windows_parameters("Home"), (0, 0x24))
        self.assertEqual(
            toggle_hotkey_windows_parameters("Ctrl+Alt+F2"),
            (0x0003, 0x71),
        )
        self.assertEqual(normalize_toggle_hotkey("F12"), "F12")
        self.assertEqual(toggle_hotkey_windows_parameters("F12"), (0, 0x7B))
        self.assertEqual(normalize_toggle_hotkey("F13"), "")
        self.assertEqual(normalize_toggle_hotkey("Ctrl+F13"), "")
        self.assertEqual(toggle_hotkey_from_tk_event("h", 0x000C), "Ctrl+Alt+H")
        self.assertEqual(toggle_hotkey_from_tk_event("Prior", 0), "PageUp")
        self.assertEqual(normalize_toggle_hotkey("H"), "")
        self.assertEqual(normalize_toggle_hotkey("Shift+H"), "")
        self.assertIsNone(toggle_hotkey_windows_parameters(""))

    def test_visibility_hotkey_dispatch_toggles_visible_and_hidden_windows(self):
        window = object.__new__(DpsWindow)
        window.closing = False
        window.login_window = None
        window._application_window_is_visible = mock.Mock(return_value=True)
        window.minimize = mock.Mock()
        window.show_from_tray = mock.Mock()

        window._dispatch_message("hotkey_toggle_visibility", None)

        window.minimize.assert_called_once_with()
        window.show_from_tray.assert_not_called()

        window._application_window_is_visible.return_value = False
        window._dispatch_message("hotkey_toggle_visibility", None)

        window.show_from_tray.assert_called_once_with()

    def test_occupied_visibility_hotkey_restores_previous_key_after_conflict(self):
        window = object.__new__(DpsWindow)
        window.tray_icon = mock.Mock()
        window.tray_icon.set_hotkey.side_effect = [False, True]
        window.toggle_visibility_hotkey = "Home"
        window.toggle_hotkey_registered = False
        window.toggle_hotkey_error = ""
        window.toggle_hotkey_capture_active = True
        window.toggle_hotkey_capture_modifiers = set()
        window.settings_hotkey_value_label = None
        window.settings_hotkey_status_label = None
        window.config = {"toggle_visibility_hotkey": "Home"}
        event = type("Event", (), {"keysym": "h", "state": 0x0008})()

        with mock.patch.dict(
            DpsWindow._capture_toggle_hotkey_key.__globals__,
            {"save_config": mock.Mock()},
        ):
            result = window._capture_toggle_hotkey_key(event)

        self.assertEqual(result, "break")
        self.assertEqual(window.toggle_visibility_hotkey, "Home")
        self.assertTrue(window.toggle_hotkey_registered)
        self.assertEqual(
            window.tray_icon.set_hotkey.call_args_list,
            [mock.call(0x0001, ord("H")), mock.call(0, 0x24)],
        )

    def test_visibility_hotkey_switch_preserves_key_while_disabled(self):
        window = object.__new__(DpsWindow)
        window.tray_icon = mock.Mock()
        window.tray_icon.set_hotkey.return_value = True
        window.toggle_visibility_hotkey = "Alt+E"
        window.toggle_visibility_hotkey_enabled = True
        window.toggle_hotkey_registered = True
        window.toggle_hotkey_error = ""
        window.toggle_hotkey_capture_active = False
        window.toggle_hotkey_capture_modifiers = set()
        window.toggle_hotkey_enabled_syncing = False
        window.settings_hotkey_value_label = None
        window.settings_hotkey_status_label = None
        window.settings_hotkey_enabled_var = None
        window.config = {
            "toggle_visibility_hotkey": "Alt+E",
            "toggle_visibility_hotkey_enabled": True,
        }

        with mock.patch.dict(
            DpsWindow._set_toggle_visibility_hotkey_enabled.__globals__,
            {"save_config": mock.Mock()},
        ):
            self.assertTrue(
                window._set_toggle_visibility_hotkey_enabled(False)
            )
            self.assertEqual(window.toggle_visibility_hotkey, "Alt+E")
            self.assertFalse(window.toggle_visibility_hotkey_enabled)
            self.assertFalse(window.toggle_hotkey_registered)
            self.assertFalse(
                window.config["toggle_visibility_hotkey_enabled"]
            )

            self.assertTrue(
                window._set_toggle_visibility_hotkey_enabled(True)
            )
            self.assertEqual(window.toggle_visibility_hotkey, "Alt+E")
            self.assertTrue(window.toggle_visibility_hotkey_enabled)
            self.assertTrue(window.toggle_hotkey_registered)

        self.assertEqual(
            window.tray_icon.set_hotkey.call_args_list,
            [mock.call(0, 0), mock.call(0x0001, ord("E"))],
        )

    def test_tray_hotkey_uses_no_repeat_and_dispatches_toggle(self):
        tray_source = Path(__file__).with_name("windows_tray.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("WM_HOTKEY = 0x0312", tray_source)
        self.assertIn("requested[0] | MOD_NOREPEAT", tray_source)
        self.assertIn('self._emit("hotkey_toggle_visibility")', tray_source)

    @unittest.skipUnless(sys.platform == "win32", "Windows hotkey API only")
    def test_native_hotkey_accepts_home_without_modifiers_and_preserves_old_key(self):
        tray_class = WINDOWS_TRAY_MODULE.WindowsTrayIcon
        tray = object.__new__(tray_class)
        tray.hwnd = 123
        tray._active_hotkey_id = 0
        tray._hotkey = (0, 0)

        register = mock.Mock(side_effect=[True, False])
        unregister = mock.Mock(return_value=True)
        with mock.patch.object(
            WINDOWS_TRAY_MODULE.user32, "RegisterHotKey", register
        ), mock.patch.object(
            WINDOWS_TRAY_MODULE.user32, "UnregisterHotKey", unregister
        ):
            self.assertTrue(tray._apply_hotkey(0, 0x24))
            self.assertFalse(tray._apply_hotkey(0x0002, ord("H")))
            self.assertEqual(tray._hotkey, (0, 0x24))
            self.assertTrue(tray._apply_hotkey(0, 0))

        self.assertEqual(
            register.call_args_list[0],
            mock.call(
                123,
                WINDOWS_TRAY_MODULE.HOTKEY_ID_PRIMARY,
                WINDOWS_TRAY_MODULE.MOD_NOREPEAT,
                0x24,
            ),
        )
        unregister.assert_called_once_with(
            123, WINDOWS_TRAY_MODULE.HOTKEY_ID_PRIMARY
        )

    def test_drag_to_left_screen_edge_uses_absolute_negative_coordinate(self):
        class Root:
            def __init__(self):
                self.geometry_calls = []

            def geometry(self, value):
                self.geometry_calls.append(value)

        root = Root()
        window = object.__new__(DpsWindow)
        window.root = root
        window.window_locked = False
        window.restore_geometry = {}
        window.drag_state = {id(root): (90, 20)}
        event = type("Event", (), {"x_root": 0, "y_root": 100})()

        window._drag_move(event, root)

        self.assertEqual(root.geometry_calls, ["+-90+80"])
        self.assertEqual(format_absolute_tk_position(-90, 80), "+-90+80")
        self.assertEqual(
            format_absolute_tk_geometry(657, 471, -90, 80),
            "657x471+-90+80",
        )
        self.assertEqual(
            parse_absolute_tk_geometry("657x471+-90+80"),
            (657, 471, -90, 80),
        )

    def test_login_window_dimensions_follow_display_dpi(self):
        self.assertEqual(login_window_dimensions(96, True), (460, 246))
        self.assertEqual(login_window_dimensions(96, False), (460, 304))
        self.assertEqual(login_window_dimensions(120, True), (575, 308))
        self.assertEqual(login_window_dimensions(120, False), (575, 380))
        self.assertEqual(login_window_dimensions(144, True), (690, 369))
        self.assertEqual(login_window_dimensions(192, False), (920, 608))

    def test_dpi_refresh_uses_visible_login_instead_of_withdrawn_root(self):
        class Window:
            @staticmethod
            def winfo_exists():
                return True

        class Root(Window):
            def __init__(self):
                self.scheduled = []

            def after(self, delay, callback):
                self.scheduled.append((delay, callback))

        root = Root()
        login = Window()
        window = object.__new__(DpsWindow)
        window.root = root
        window.login_window = login
        window.user_notice_window = None
        window.closing = False
        window.window_dpi = 120
        window._apply_window_dpi_if_changed = mock.Mock()
        window._prepare_toplevel_dpi = mock.Mock()
        method_globals = DpsWindow._refresh_window_dpi.__globals__

        with mock.patch.dict(method_globals, {"get_window_dpi": lambda target: 120}):
            window._refresh_window_dpi()

        window._apply_window_dpi_if_changed.assert_not_called()
        window._prepare_toplevel_dpi.assert_not_called()
        self.assertEqual(root.scheduled[0][0], 250)

        with mock.patch.dict(method_globals, {"get_window_dpi": lambda target: 144}):
            window._refresh_window_dpi()

        window._apply_window_dpi_if_changed.assert_not_called()
        window._prepare_toplevel_dpi.assert_called_once_with(login)

    def test_dpi_refresh_preserves_every_edge_and_cross_monitor_position(self):
        class Root:
            def __init__(self, x, y):
                self.x = x
                self.y = y
                self.geometry_calls = []

            @staticmethod
            def winfo_exists():
                return True

            def winfo_x(self):
                return self.x

            def winfo_y(self):
                return self.y

            @staticmethod
            def update_idletasks():
                return None

            def geometry(self, value):
                self.geometry_calls.append(value)
                match = __import__("re").fullmatch(r"\+(-?\d+)\+(-?\d+)", value)
                if match:
                    self.x = int(match.group(1))
                    self.y = int(match.group(2))

        positions = (
            (0, 120),       # left edge
            (1330, 120),    # right edge for a 590px window at 1920px
            (520, 0),       # top edge
            (520, 640),     # bottom edge for a 400px window at 1040px
            (-1920, 120),   # left-hand monitor
            (1920, 120),    # right-hand monitor
        )
        method_globals = DpsWindow._apply_window_dpi_if_changed.__globals__
        for position in positions:
            with self.subTest(position=position):
                root = Root(*position)
                window = object.__new__(DpsWindow)
                window.root = root
                window.closing = False
                window.drag_state = {}
                window.window_dpi = 96

                def simulate_dpi_reposition(_root):
                    root.x, root.y = 160, 96
                    return 120

                with mock.patch.dict(
                    method_globals,
                    {
                        "get_window_dpi": lambda _root: 120,
                        "configure_tk_dpi_scaling": simulate_dpi_reposition,
                    },
                ):
                    self.assertTrue(window._apply_window_dpi_if_changed())

                self.assertEqual((root.x, root.y), position)
                self.assertEqual(
                    root.geometry_calls[-1],
                    format_absolute_tk_position(*position),
                )

    def test_accidental_iconification_is_recovered_but_intentional_hide_is_not(self):
        class Root:
            def __init__(self):
                self.calls = []

            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def state():
                return "iconic"

            def overrideredirect(self, value):
                self.calls.append(("overrideredirect", value))

            def deiconify(self):
                self.calls.append(("deiconify",))

            def update_idletasks(self):
                self.calls.append(("update_idletasks",))

            def lift(self):
                self.calls.append(("lift",))

            def after(self, delay, callback):
                self.calls.append(("after", delay, callback))

        window = object.__new__(DpsWindow)
        window.root = Root()
        window.closing = False
        window.main_window_intentionally_hidden = False
        window._apply_window_dpi_if_changed = mock.Mock()
        window._apply_windows_style = mock.Mock()
        window._apply_main_transparency = mock.Mock()
        window._apply_window_lock_state = mock.Mock()

        window._recover_accidentally_iconified_main_window()

        self.assertIn(("deiconify",), window.root.calls)
        window.main_window_intentionally_hidden = True
        window.root.calls.clear()
        window._recover_accidentally_iconified_main_window()
        self.assertEqual(window.root.calls, [])

    def test_dt_uses_only_exact_taken_and_share_without_inference(self):
        model = CombatModel(run_id="dt-exact-data-test")
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
        self.assertFalse(
            model.ingest_team_stat(
                {
                    "actor_id": SELF_ID,
                    "absolute_taken": 100,
                    "filetime_100ns": BASE_FILETIME,
                    "full_snapshot": True,
                }
            )
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000))
        self.assertTrue(
            model.ingest_team_stat(
                {
                    "actor_id": SELF_ID,
                    "absolute_taken": 700,
                    "filetime_100ns": BASE_FILETIME + 2 * 10_000,
                    "full_snapshot": True,
                }
            )
        )

        incomplete = {
            row["actor_id"]: row for row in model.current_taken_rows()
        }
        self.assertEqual(incomplete[SELF_ID]["taken"], 600)
        self.assertIsNone(incomplete[TEAMMATE_ID]["taken"])
        self.assertIsNone(incomplete[SELF_ID]["share"])

        model.stage_actor_taken[TEAMMATE_ID] = 400
        rows = {row["actor_id"]: row for row in model.current_taken_rows()}
        self.assertEqual(rows[SELF_ID]["source"], "server_team_counter")
        self.assertEqual(rows[SELF_ID]["taken"], 600)
        self.assertAlmostEqual(rows[SELF_ID]["share"], 0.6)
        self.assertEqual(rows[TEAMMATE_ID]["source"], "server_stage_summary")
        self.assertEqual(rows[TEAMMATE_ID]["taken"], 400)
        self.assertAlmostEqual(rows[TEAMMATE_ID]["share"], 0.4)
        self.assertTrue(all("max_hit" not in row for row in rows.values()))

        record = model.build_combat_record("target_defeated")
        self.assertEqual(record["team_taken"], 1_000)
        self.assertEqual(
            {row["actor_id"]: row["taken"] for row in record["damage_taken"]},
            {SELF_ID: 600, TEAMMATE_ID: 400},
        )
        self.assertTrue(
            all("max_hit" not in row for row in record["damage_taken"])
        )
        self.assertTrue(
            all("taken_max_hit" not in row for row in record["participants"])
        )

    def test_boss_damage_side_channel_archives_without_changing_dps(self):
        model = CombatModel(
            run_id="boss-damage-side-channel-test",
            skill_names={"88008120": "知识禁制"},
        )
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
                "template_id": 7_102_403,
                "name": "星象仪者",
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 10_000))
        model.team_damage_states[SELF_ID] = TeamDamageState(
            SELF_ID,
            last_absolute=10_000,
            has_snapshot=True,
            authoritative_snapshot=True,
            accepted_damage=10_000,
        )
        model.team_taken_states[SELF_ID] = TeamTakenState(
            SELF_ID,
            last_absolute=1_000,
            has_snapshot=True,
            accepted_taken=1_000,
            exact_for_encounter=True,
        )
        model.team_healing_states[SELF_ID] = TeamHealingState(
            SELF_ID,
            last_absolute=250,
            has_snapshot=True,
            accepted_effective_healing=250,
            exact_for_encounter=True,
        )
        model.member_death_counts[SELF_ID] = 1
        model.member_revive_counts[SELF_ID] = 1
        model.member_dead_durations[SELF_ID] = 2.5
        original_damage = model.stats[SELF_ID].damage
        original_team_damage = dict(model.team_damage_states[SELF_ID].__dict__)
        original_team_taken = dict(model.team_taken_states[SELF_ID].__dict__)
        original_team_healing = dict(model.team_healing_states[SELF_ID].__dict__)
        original_taken_rows = model.current_taken_rows()
        original_healing = model.healing_summary(duration=10.0)
        original_life = (
            dict(model.member_death_counts),
            dict(model.member_revive_counts),
            dict(model.member_dead_durations),
            dict(model.member_dead_since),
        )
        original_clock = (
            model.first_damage_time,
            model.last_damage_time,
            model.combat_end_time,
            model.encounter_start_signal_100ns,
            model.combat_clock_client_revision,
        )

        self.assertTrue(
            model.ingest_boss_damage(
                {
                    "filetime_100ns": BASE_FILETIME + 2 * 10_000,
                    "source_id": MONSTER_ID,
                    "target_id": SELF_ID,
                    "skill_id": 88_008_120,
                    "damage": 600,
                    "source_template_id": 7_102_403,
                    "source_kind": "boss",
                    "boss_source_confirmed": True,
                    "party_target_confirmed": True,
                    "active_boss_source": True,
                }
            )
        )
        self.assertEqual(model.stats[SELF_ID].damage, original_damage)
        self.assertEqual(
            model.team_damage_states[SELF_ID].__dict__, original_team_damage
        )
        self.assertEqual(
            model.team_taken_states[SELF_ID].__dict__, original_team_taken
        )
        self.assertEqual(
            model.team_healing_states[SELF_ID].__dict__, original_team_healing
        )
        self.assertEqual(model.current_taken_rows(), original_taken_rows)
        self.assertEqual(model.healing_summary(duration=10.0), original_healing)
        self.assertEqual(
            (
                dict(model.member_death_counts),
                dict(model.member_revive_counts),
                dict(model.member_dead_durations),
                dict(model.member_dead_since),
            ),
            original_life,
        )
        self.assertEqual(
            (
                model.first_damage_time,
                model.last_damage_time,
                model.combat_end_time,
                model.encounter_start_signal_100ns,
                model.combat_clock_client_revision,
            ),
            original_clock,
        )

        live = model.current_boss_damage_summary()
        self.assertEqual(live["coverage"], "observed_partial")
        self.assertEqual(live["observed_damage"], 600)
        self.assertEqual(live["unassigned_taken"], 400)
        self.assertEqual(live["skills"][0]["name"], "知识禁制")

        model.ingest_boss_damage(
            {
                "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                "source_id": MONSTER_ID,
                "target_id": SELF_ID,
                "skill_id": 88_008_120,
                "damage": 400,
                "source_template_id": 7_102_403,
                "source_kind": "boss",
                "boss_source_confirmed": True,
                "party_target_confirmed": True,
                "active_boss_source": True,
            }
        )
        record = model.build_combat_record("target_defeated")

        self.assertEqual(record["total_damage"], 10_000)
        self.assertEqual(record["team_taken"], 1_000)
        self.assertEqual(record["team_effective_healing"], 250)
        self.assertEqual(record["participants"][0]["deaths"], 1)
        self.assertEqual(record["participants"][0]["revives"], 1)
        self.assertEqual(record["boss_damage"]["coverage"], "complete")
        self.assertEqual(record["boss_damage"]["observed_damage"], 1_000)
        self.assertEqual(len(record["boss_damage"]["event_log"]["rows"]), 2)

    def test_boss_damage_model_rejects_untrusted_or_foreign_events(self):
        model = CombatModel(run_id="boss-damage-rejection-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
                "template_id": 7_102_403,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000))
        base = {
            "filetime_100ns": BASE_FILETIME + 2 * 10_000,
            "source_id": MONSTER_ID,
            "target_id": SELF_ID,
            "skill_id": 88_008_120,
            "damage": 100,
            "source_template_id": 7_102_403,
            "source_kind": "boss",
            "active_boss_source": True,
        }
        self.assertFalse(model.ingest_boss_damage(base))
        foreign = dict(base)
        foreign.update(
            {
                "target_id": NEARBY_ID,
                "boss_source_confirmed": True,
                "party_target_confirmed": True,
            }
        )
        self.assertFalse(model.ingest_boss_damage(foreign))
        self.assertEqual(model.current_boss_damage_summary()["observed_damage"], 0)

    def test_stage_summary_accepts_taken_for_verified_zero_damage_self(self):
        model = CombatModel(run_id="dt-zero-damage-self-settlement-test")
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

        self.assertFalse(
            model.ingest_team_stat(
                {
                    "actor_id": SELF_ID,
                    "absolute_taken": 1_000,
                    "filetime_100ns": BASE_FILETIME,
                    "full_snapshot": True,
                }
            )
        )
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 100_000))
        self.assertTrue(
            model.ingest_team_stat(
                {
                    "actor_id": SELF_ID,
                    "absolute_taken": 4_967,
                    "filetime_100ns": BASE_FILETIME + 2 * 10_000,
                    "full_snapshot": True,
                }
            )
        )
        self.assertNotIn(SELF_ID, model.stats)
        self.assertEqual(
            {
                row["actor_id"]: row["taken"]
                for row in model.current_taken_rows()
            }[SELF_ID],
            3_967,
        )

        death_time = BASE_FILETIME + 3 * 10_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 1_000_000,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        summary_id = "settlement|zero-damage-self|exact"
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": summary_id,
                    "filetime_100ns": death_time + 10_000,
                    "member_count": 2,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 0,
                            "taken": 47_002,
                            "taken_present": True,
                            "skills": [],
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 100_000,
                            "taken": 150_000,
                            "taken_present": True,
                            "skills": [],
                        },
                    ],
                }
            )
        )

        rows = {row["actor_id"]: row for row in model.current_taken_rows()}
        self.assertEqual(rows[SELF_ID]["taken"], 47_002)
        self.assertEqual(rows[SELF_ID]["source"], "server_stage_summary")
        self.assertEqual(rows[TEAMMATE_ID]["taken"], 150_000)
        self.assertEqual(
            model.stage_summaries[summary_id]["validation"][
                "server_taken_actor_ids"
            ],
            [SELF_ID, TEAMMATE_ID],
        )

    def test_common_effective_healing_is_exact_live_total_without_rescaling_details(self):
        model = CombatModel(run_id="hps-common-counter-test")
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
                "entity_id": TEAMMATE_ID,
                "name": "healer",
                "entity_type": "Player",
                "profession_id": 1_200_002,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        self.assertFalse(
            model.ingest_team_stat(
                {
                    "actor_id": TEAMMATE_ID,
                    "absolute_effective_healing": 1_000,
                    "filetime_100ns": BASE_FILETIME,
                    "full_snapshot": True,
                }
            )
        )

        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000))
        self.assertTrue(
            model.ingest_heal(
                healing(
                    BASE_FILETIME + 15_000,
                    TEAMMATE_ID,
                    SELF_ID,
                    total=1_200,
                    effective=1_000,
                )
            )
        )
        self.assertTrue(
            model.ingest_team_stat(
                {
                    "actor_id": TEAMMATE_ID,
                    "absolute_effective_healing": 4_000,
                    "filetime_100ns": BASE_FILETIME + 20_000,
                    "full_snapshot": True,
                }
            )
        )

        summary = model.healing_summary(duration=10.0)
        self.assertEqual(summary["team_effective_healing"], 3_000)
        healer = summary["healers"][0]
        self.assertEqual(healer["effective_healing"], 3_000)
        self.assertEqual(
            healer["coverage"], "server_team_counter_with_observed_callbacks"
        )
        self.assertIsNone(healer["total_healing"])
        self.assertEqual(
            {
                row["skill_id"]: row["effective_healing"]
                for row in healer["skills"]
            },
            {86_021_030: 1_000, 0: 2_000},
        )

        model.reset(keep_identity=True, keep_monsters=True)
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 500))
        self.assertTrue(
            model.ingest_team_stat(
                {
                    "actor_id": TEAMMATE_ID,
                    "absolute_effective_healing": 4_500,
                    "filetime_100ns": BASE_FILETIME + 40_000,
                    "full_snapshot": True,
                }
            )
        )
        self.assertEqual(
            model.healing_summary(duration=10.0)["team_effective_healing"], 500
        )

    def test_first_full_zero_is_exact_for_member_missing_from_prepull_snapshot(self):
        model = CombatModel(run_id="late-zero-baseline-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {
                "entity_id": TEAMMATE_ID,
                "entity_type": "Player",
                "profession_id": 1_200_002,
            }
        )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest_team_stat(
            {
                "actor_id": SELF_ID,
                "absolute_taken": 10,
                "absolute_effective_healing": 10,
                "filetime_100ns": BASE_FILETIME,
                "full_snapshot": True,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))

        self.assertTrue(
            model.ingest_team_stat(
                {
                    "actor_id": TEAMMATE_ID,
                    "absolute_taken": 0,
                    "absolute_effective_healing": 0,
                    "filetime_100ns": BASE_FILETIME + 20_000,
                    "full_snapshot": True,
                    "taken_omitted_zero": True,
                    "healing_omitted_zero": True,
                }
            )
        )
        self.assertTrue(model.team_taken_states[TEAMMATE_ID].exact_for_encounter)
        self.assertTrue(model.team_healing_states[TEAMMATE_ID].exact_for_encounter)

        self.assertTrue(
            model.ingest_team_stat(
                {
                    "actor_id": TEAMMATE_ID,
                    "absolute_taken": 124_190,
                    "absolute_effective_healing": 117_572,
                    "filetime_100ns": BASE_FILETIME + 30_000,
                    "full_snapshot": True,
                }
            )
        )
        self.assertEqual(
            model.team_taken_states[TEAMMATE_ID].accepted_taken, 124_190
        )
        self.assertEqual(
            model.team_healing_states[TEAMMATE_ID].accepted_effective_healing,
            117_572,
        )

    def test_confirmed_death_revive_and_dead_duration_are_recorded(self):
        model = CombatModel(run_id="life-metrics-test")
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
        death_time = BASE_FILETIME + 2 * 10_000_000
        revive_time = BASE_FILETIME + 7 * 10_000_000

        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": death_time,
                }
            )
        )
        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": False,
                    "filetime_100ns": revive_time,
                }
            )
        )

        self.assertEqual(model.member_death_counts[SELF_ID], 1)
        self.assertEqual(model.member_revive_counts[SELF_ID], 1)
        self.assertAlmostEqual(model.member_death_duration(SELF_ID), 5.0)
        participant = model.build_combat_record("target_defeated")["participants"][0]
        self.assertEqual(participant["deaths"], 1)
        self.assertEqual(participant["revives"], 1)
        self.assertAlmostEqual(participant["death_duration_seconds"], 5.0)

    def test_self_life_metrics_start_from_boss_state_without_self_attack(self):
        model = CombatModel(run_id="self-life-without-attack-test")
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
        boss_start = BASE_FILETIME + 1 * 10_000_000
        self.assertTrue(
            model.ingest_combat_state(
                {
                    "entity_id": MONSTER_ID,
                    "in_combat": True,
                    "filetime_100ns": boss_start,
                }
            )
        )
        teammate_hit = damage(1, TEAMMATE_ID, MONSTER_ID, 250_000)
        teammate_hit["filetime_100ns"] = boss_start + 1 * 10_000_000
        model.ingest(teammate_hit)

        death_time = boss_start + 2 * 10_000_000
        revive_time = boss_start + 6 * 10_000_000
        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": death_time,
                }
            )
        )
        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": False,
                    "filetime_100ns": revive_time,
                }
            )
        )

        self.assertNotIn(SELF_ID, model.stats)
        self.assertEqual(model.member_death_counts[SELF_ID], 1)
        self.assertEqual(model.member_revive_counts[SELF_ID], 1)
        self.assertAlmostEqual(model.member_death_duration(SELF_ID), 4.0)

    def test_life_metrics_recover_when_boss_identity_arrives_late(self):
        model = CombatModel(run_id="late-boss-life-replay-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
        boss_start = BASE_FILETIME + 1 * 10_000_000
        self.assertTrue(
            model.ingest_combat_state(
                {
                    "entity_id": MONSTER_ID,
                    "in_combat": True,
                    "filetime_100ns": boss_start,
                }
            )
        )
        self.assertEqual(model.first_damage_time, 0.0)

        death_time = boss_start + 1 * 10_000_000
        revive_time = boss_start + 4 * 10_000_000
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": False,
                "filetime_100ns": revive_time,
            }
        )
        self.assertNotIn(SELF_ID, model.member_death_counts)
        self.assertNotIn(SELF_ID, model.member_revive_counts)

        self.assertTrue(
            model.ingest_profile(
                {
                    "entity_id": MONSTER_ID,
                    "entity_type": "Boss",
                    "boss_rank": 3,
                }
            )
        )
        self.assertEqual(model.encounter_start_signal_100ns, boss_start)
        self.assertEqual(model.member_death_counts[SELF_ID], 1)
        self.assertEqual(model.member_revive_counts[SELF_ID], 1)
        self.assertAlmostEqual(model.member_death_duration(SELF_ID), 3.0)

    def test_pre_pull_and_duplicate_life_events_do_not_inflate_metrics(self):
        model = CombatModel(run_id="life-boundary-dedup-test")
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
        pre_pull_death = BASE_FILETIME + 1 * 10_000_000
        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": pre_pull_death,
                }
            )
        )
        self.assertFalse(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": pre_pull_death + 1_000,
                }
            )
        )
        boss_start = BASE_FILETIME + 3 * 10_000_000
        model.ingest_combat_state(
            {
                "entity_id": MONSTER_ID,
                "in_combat": True,
                "filetime_100ns": boss_start,
            }
        )
        self.assertNotIn(SELF_ID, model.member_death_counts)
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": False,
                "filetime_100ns": boss_start + 1 * 10_000_000,
            }
        )

        encounter_death = boss_start + 2 * 10_000_000
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": encounter_death,
            }
        )
        self.assertFalse(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": encounter_death + 1_000,
                }
            )
        )
        self.assertFalse(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": False,
                    "filetime_100ns": encounter_death - 1,
                }
            )
        )
        revive_time = boss_start + 5 * 10_000_000
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": False,
                "filetime_100ns": revive_time,
            }
        )

        self.assertEqual(model.member_death_counts[SELF_ID], 1)
        self.assertEqual(model.member_revive_counts[SELF_ID], 1)
        self.assertAlmostEqual(model.member_death_duration(SELF_ID), 3.0)

    def test_earlier_boss_edge_backfills_life_without_changing_damage(self):
        model = CombatModel(run_id="life-backdate-isolation-test")
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
        earlier_start = BASE_FILETIME + 1 * 10_000_000
        death_time = BASE_FILETIME + 2 * 10_000_000
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": True,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )
        opening_hit = damage(1, TEAMMATE_ID, MONSTER_ID, 345_678)
        opening_hit["filetime_100ns"] = BASE_FILETIME + 3 * 10_000_000
        opening_hit["critical"] = True
        opening_hit["penetrating"] = True
        model.ingest(opening_hit)
        actor = model.stats[TEAMMATE_ID]
        metrics_before = (
            actor.damage,
            actor.hits,
            actor.damage_hits,
            actor.critical_hits,
            actor.penetration_hits,
            actor.skills[opening_hit["skill_id"]].damage,
            tuple(model.events),
        )
        self.assertNotIn(SELF_ID, model.member_death_counts)

        self.assertTrue(
            model.ingest_combat_state(
                {
                    "entity_id": MONSTER_ID,
                    "in_combat": True,
                    "filetime_100ns": earlier_start,
                }
            )
        )
        actor = model.stats[TEAMMATE_ID]
        self.assertEqual(model.member_death_counts[SELF_ID], 1)
        self.assertEqual(
            (
                actor.damage,
                actor.hits,
                actor.damage_hits,
                actor.critical_hits,
                actor.penetration_hits,
                actor.skills[opening_hit["skill_id"]].damage,
                tuple(model.events),
            ),
            metrics_before,
        )
        revive_time = BASE_FILETIME + 5 * 10_000_000
        model.ingest_life(
            {
                "actor_id": SELF_ID,
                "dead": False,
                "filetime_100ns": revive_time,
            }
        )
        self.assertEqual(model.member_revive_counts[SELF_ID], 1)
        self.assertAlmostEqual(model.member_death_duration(SELF_ID), 3.0)

    def test_post_combat_revive_closes_duration_without_counting_revive(self):
        """Standing up after the encounter must not count as an encounter revive."""
        model = CombatModel(run_id="backdated-life-boundary-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000))
        death_time = BASE_FILETIME + 2 * 10_000_000
        combat_end_time = death_time + 5_000_000
        revive_time = death_time + 12_450_000

        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": death_time,
                }
            )
        )
        model.combat_end_time = model._event_seconds(
            {"filetime_100ns": combat_end_time}
        )
        model.combat_end_reason = "target_defeated"
        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": False,
                    "filetime_100ns": revive_time,
                }
            )
        )

        self.assertEqual(model.member_death_counts[SELF_ID], 1)
        self.assertEqual(model.member_revive_counts.get(SELF_ID, 0), 0)
        self.assertAlmostEqual(model.member_death_duration(SELF_ID), 0.5, places=6)
        participant = model.build_combat_record("target_defeated")["participants"][0]
        self.assertEqual(participant["deaths"], 1)
        self.assertEqual(participant["revives"], 0)
        self.assertAlmostEqual(
            participant["death_duration_seconds"], 0.5, places=6
        )

    def test_new_death_after_known_end_is_not_attached_to_finished_encounter(self):
        model = CombatModel(run_id="post-end-life-boundary-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 1_000))
        model.combat_end_time = model.last_damage_time
        model.combat_end_reason = "target_defeated"

        self.assertTrue(
            model.ingest_life(
                {
                    "actor_id": SELF_ID,
                    "dead": True,
                    "death_confirmed": True,
                    "filetime_100ns": BASE_FILETIME + 2 * 10_000_000,
                }
            )
        )
        self.assertNotIn(SELF_ID, model.member_death_counts)
        self.assertNotIn(SELF_ID, model.member_dead_since)

    def test_final_exact_settlement_supplies_teammate_hit_rates(self):
        model = CombatModel(run_id="final-critical-rate-test")
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
        model.combat_end_time = model.last_damage_time
        model.combat_end_reason = "target_defeated"

        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "settlement|critical-rate",
                    "filetime_100ns": BASE_FILETIME + 3 * 10_000,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "member_count": 2,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 1_000,
                            "damage_hits": 10,
                            "critical_hits": 4,
                            "penetration_hits": 9,
                            "skills": [],
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 2_000,
                            "damage_hits": 40,
                            "critical_hits": 30,
                            "penetration_hits": 38,
                            "skills": [],
                        },
                    ],
                }
            )
        )

        self.assertEqual(model.stage_actor_metrics[TEAMMATE_ID], (40, 30))
        self.assertEqual(
            model.stage_actor_penetration_metrics[TEAMMATE_ID], (40, 38)
        )
        participants = {
            row["actor_id"]: row
            for row in model.build_combat_record("target_defeated")["participants"]
        }
        self.assertAlmostEqual(participants[TEAMMATE_ID]["critical_rate"], 0.75)
        self.assertAlmostEqual(
            participants[TEAMMATE_ID]["penetration_rate"], 0.95
        )

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
        apply_transparency = mock.Mock(wraps=window._apply_main_transparency)
        window._apply_main_transparency = apply_transparency

        window._set_window_alpha(100, persist=False)
        apply_transparency.assert_called_once_with()

        window._set_window_alpha(40, persist=False)
        self.assertEqual(window.window_alpha, 0.40)
        self.assertEqual(window.root.alpha, 0.40)
        window._set_window_alpha(10, persist=False)
        self.assertEqual(window.window_alpha, 0.10)
        self.assertEqual(window.root.alpha, 0.10)
        window._set_window_alpha(-10, persist=False)
        self.assertEqual(window.window_alpha, 0.10)
        self.assertEqual(window.root.alpha, 0.10)
        window._set_window_alpha(82, persist=False)
        self.assertEqual(window.window_alpha, 0.82)
        window._set_window_alpha(0.73, persist=False)
        self.assertEqual(window.window_alpha, 0.73)
        self.assertEqual(window.config["alpha"], 0.73)
        self.assertEqual(window.history_window.alpha_changes, [])
        self.assertEqual(apply_transparency.call_count, 6)

    def test_opacity_slider_applies_directly_without_fading_settings_window(self):
        class Window:
            def __init__(self):
                self.alpha = 1.0

            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def state():
                return "normal"

            @staticmethod
            def update_idletasks():
                return None

            @staticmethod
            def configure(**_options):
                return None

            def attributes(self, name, *values):
                if name != "-alpha":
                    raise AssertionError(name)
                if values:
                    self.alpha = float(values[0])
                return self.alpha

        window = object.__new__(DpsWindow)
        window.root = Window()
        window.history_window = Window()
        window.backend_current_page = "settings"
        window.window_alpha = 1.0
        window.window_locked = False
        window.closing = False
        window.main_content_overlay_supported = False
        window.opacity_value_label = None
        window.settings_opacity_var = None
        window.settings_live_apply_ready = False
        window.config = {}

        with mock.patch.dict(
            DpsWindow._set_window_alpha.__globals__,
            {"save_config": lambda _config: None},
        ):
            window._preview_window_alpha(68)

        self.assertEqual(window.window_alpha, 0.68)
        self.assertEqual(window.root.alpha, 0.68)
        self.assertEqual(window.history_window.alpha, 1.0)
        self.assertEqual(window.config["alpha"], 0.68)
        window.history_window.alpha = 0.42
        window._ensure_backend_window_opaque()
        self.assertEqual(window.history_window.alpha, 1.0)

    def test_lock_reasserts_configured_alpha_after_native_style_change(self):
        class Root:
            def __init__(self):
                self.alpha = 1.0
                self.topmost = False
                self.callbacks = []

            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def state():
                return "normal"

            def attributes(self, name, *values):
                if name == "-alpha":
                    if values:
                        self.alpha = float(values[0])
                    return self.alpha
                if name == "-topmost":
                    if values:
                        self.topmost = bool(values[0])
                    return self.topmost
                raise AssertionError(name)

            def after(self, delay, callback):
                self.callbacks.append((delay, callback))

        root = Root()
        window = object.__new__(DpsWindow)
        window.root = root
        window.closing = False
        window.window_alpha = 0.64
        window.window_locked = True
        window.window_lock_topmost_restore = None
        window.config = {"topmost": False}
        window.main_content_overlay_window = None
        window.main_content_overlay_topmost = None
        window._apply_main_transparency = lambda: setattr(
            root, "alpha", window.window_alpha
        )
        click_states = []
        window._set_window_click_through = (
            lambda _root, locked: click_states.append(locked) or True
        )
        window._show_unlock_window = lambda: None
        window._destroy_unlock_window = lambda: None
        window._sync_action_buttons = lambda: None
        window._draw_main_header = lambda: None
        window._schedule_main_content_overlay_sync = lambda **_options: None

        window._apply_window_lock_state()
        root.alpha = 1.0
        self.assertEqual(root.callbacks[0][0], 60)
        root.callbacks[0][1]()

        self.assertEqual(root.alpha, 0.64)
        self.assertTrue(click_states[-1])

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
        window.show_extraordinary_rating = False
        window.team_rating_preview_enabled = False
        self.assertEqual(window._compact_target_width(), 220)
        window.show_extraordinary_rating = True
        self.assertEqual(window._compact_target_width(), 260)
        window.show_total_damage = True
        window.show_damage_share = True
        window.show_critical_rate = True
        self.assertEqual(window._compact_target_width(), 380)

        window.show_total_damage = False
        window.show_damage_share = False
        window.show_critical_rate = False
        window.compact_mode = True
        window.main_meter_mode = "dps"
        window.show_extraordinary_rating = False
        plain_columns = window._main_columns(220)
        window.show_extraordinary_rating = True
        rating_columns = window._main_columns(260)
        self.assertEqual(rating_columns["name_limit"], plain_columns["name_limit"])
        self.assertEqual(rating_columns["dps"] - plain_columns["dps"], 40)

    def test_main_extraordinary_rating_copy_and_adaptive_badge_layout(self):
        self.assertIsNone(normalize_extraordinary_rating(None))
        self.assertIsNone(normalize_extraordinary_rating(-1))
        self.assertIsNone(normalize_extraordinary_rating(True))
        self.assertEqual(normalize_extraordinary_rating("37000"), 37_000)
        self.assertEqual(format_extraordinary_rating(37_000), "37,000")
        self.assertEqual(
            format_extraordinary_rating(41_820, compact=True), "41.8k"
        )
        self.assertEqual(format_extraordinary_rating(None), "--")

        window = object.__new__(DpsWindow)
        window.ui_font_size = 14
        full = window._main_rating_badge_layout(
            name_x=38,
            name_limit=220,
            top=0,
            row_height=32,
            compact=False,
        )
        self.assertIsNotNone(full)
        self.assertFalse(full["compact"])
        self.assertEqual(full["bounds"][2] - full["bounds"][0], 92)
        self.assertGreaterEqual(full["name_width"], 70)

        ai = window._main_rating_badge_layout(
            name_x=38,
            name_limit=220,
            top=0,
            row_height=32,
            compact=False,
            identity_label="人机",
        )
        self.assertIsNotNone(ai)
        self.assertTrue(ai["compact"])
        self.assertLess(ai["bounds"][2] - ai["bounds"][0], 60)
        self.assertGreater(ai["name_width"], full["name_width"])

        narrow = window._main_rating_badge_layout(
            name_x=31,
            name_limit=134,
            top=0,
            row_height=28,
            compact=True,
        )
        self.assertIsNotNone(narrow)
        self.assertTrue(narrow["compact"])
        self.assertEqual(narrow["bounds"][2] - narrow["bounds"][0], 44)
        self.assertGreaterEqual(narrow["name_width"], 48)

        inline = window._main_rating_badge_layout(
            name_x=38,
            name_limit=220,
            top=0,
            row_height=32,
            compact=False,
            displayed_name_width=42,
        )
        self.assertIsNotNone(inline)
        self.assertEqual(inline["bounds"][0], 38 + 42 + 6)
        self.assertEqual(inline["bounds"][2] - inline["bounds"][0], 92)

        long_name = window._main_rating_badge_layout(
            name_x=38,
            name_limit=220,
            top=0,
            row_height=32,
            compact=False,
            displayed_name_width=500,
        )
        self.assertIsNotNone(long_name)
        self.assertEqual(long_name["bounds"][2], 220 - 5)

        class Canvas:
            def __init__(self):
                self.texts = []

            @staticmethod
            def create_polygon(*_args, **_options):
                return None

            def create_text(self, *_args, **options):
                self.texts.append(options.get("text"))

        canvas = Canvas()
        window._ui_font = lambda _role: "test-font"
        window._draw_main_rating_badge(
            canvas,
            rating=41_820,
            layout=narrow,
            actor_tag="actor:1",
        )
        self.assertEqual(canvas.texts[-1], "41.8k")

        window._draw_main_rating_badge(
            canvas,
            rating=41_820,
            layout=narrow,
            actor_tag="actor:2",
            identity_label="人机",
        )
        self.assertEqual(canvas.texts[-1], "人机")

        window.show_extraordinary_rating = False
        self.assertFalse(
            window._main_extraordinary_rating_visible(rating_preview=False)
        )
        window.show_extraordinary_rating = True
        self.assertTrue(
            window._main_extraordinary_rating_visible(rating_preview=False)
        )
        window.show_extraordinary_rating = False
        self.assertTrue(
            window._main_extraordinary_rating_visible(rating_preview=True)
        )

    def test_team_rating_preview_is_available_and_caches_party_rows(self):
        class Model:
            def __init__(self):
                self.self_id = SELF_ID
                self.friend_order = [SELF_ID, TEAMMATE_ID]
                self.party_member_count = 2
                self.party_active = True
                self.non_player_actor_ids = set()
                self.entity_extraordinary_ratings = {
                    SELF_ID: 37_000,
                    TEAMMATE_ID: 41_820,
                }
                self.entity_professions = {
                    SELF_ID: 1_200_001,
                    TEAMMATE_ID: 1_200_002,
                }
                self.entity_names = {
                    SELF_ID: "本机玩家",
                    TEAMMATE_ID: "评分队友",
                }
                self.members = {SELF_ID, TEAMMATE_ID}
                self.started = False
                self.profession_lookups = 0
                self.name_lookups = 0

            def _current_member_ids(self):
                return set(self.members)

            def _encounter_started(self):
                return self.started

            def actor_profession_id(self, actor_id):
                self.profession_lookups += 1
                return self.entity_professions.get(actor_id, 0)

            def display_name(self, actor_id):
                self.name_lookups += 1
                return self.entity_names.get(actor_id, "")

        window = object.__new__(DpsWindow)
        window.model = Model()
        window.team_rating_preview_enabled = False
        self.assertFalse(window._team_rating_preview_active())

        window.team_rating_preview_enabled = True
        self.assertTrue(TEAM_RATING_PREVIEW_AVAILABLE)
        self.assertTrue(window._team_rating_preview_active())
        rows = window._team_rating_preview_rows()
        self.assertEqual(
            [row["actor_id"] for row in rows], [SELF_ID, TEAMMATE_ID]
        )
        self.assertEqual(rows[1]["extraordinary_rating"], 41_820)
        self.assertEqual(rows[1]["display_name"], "评分队友")
        self.assertEqual(window.model.profession_lookups, 2)
        self.assertEqual(window.model.name_lookups, 2)

        self.assertIs(window._team_rating_preview_rows(), rows)
        self.assertTrue(window._team_rating_preview_active())
        self.assertEqual(window.model.profession_lookups, 2)
        self.assertEqual(window.model.name_lookups, 2)

        window.model.members.add(NEARBY_ID)
        window.model.friend_order.append(NEARBY_ID)
        window.model.party_member_count = 3
        window.model.entity_professions[NEARBY_ID] = 1_200_003
        window.model.entity_names[NEARBY_ID] = "新进队员"
        joined_rows = window._team_rating_preview_rows()
        self.assertEqual(
            [row["actor_id"] for row in joined_rows],
            [SELF_ID, TEAMMATE_ID, NEARBY_ID],
        )
        self.assertEqual(window.model.profession_lookups, 3)
        self.assertEqual(window.model.name_lookups, 3)

        window.model.entity_extraordinary_ratings[TEAMMATE_ID] = 42_100
        refreshed_rows = window._team_rating_preview_rows()
        self.assertEqual(refreshed_rows[1]["extraordinary_rating"], 42_100)
        self.assertEqual(window.model.profession_lookups, 4)
        self.assertEqual(window.model.name_lookups, 4)

        window.model.entity_names[TEAMMATE_ID] = "评分队友·投影"
        ai_rows = window._team_rating_preview_rows()
        self.assertTrue(ai_rows[1]["is_ai"])
        self.assertTrue(
            window._main_actor_is_ai(TEAMMATE_ID, ai_rows[1])
        )

        window.model.members = {SELF_ID}
        window.model.friend_order = [SELF_ID]
        window.model.party_member_count = 1
        window.model.party_active = False
        self.assertFalse(window._team_rating_preview_active())
        window.model.party_active = True
        self.assertTrue(window._team_rating_preview_active())
        self.assertEqual(
            [row["actor_id"] for row in window._team_rating_preview_rows()],
            [SELF_ID],
        )

        window.model.started = True
        self.assertFalse(window._team_rating_preview_active())

    def test_combat_model_tracks_single_member_team_activity(self):
        model = CombatModel()
        model.ingest_identity({"entity_id": SELF_ID})

        self.assertTrue(
            model.ingest_party(
                {
                    "entity_ids": [],
                    "member_count": 1,
                    "in_team": True,
                }
            )
        )
        self.assertTrue(model.party_active)
        self.assertEqual(model.party_member_count, 1)

        self.assertTrue(
            model.ingest_party(
                {
                    "entity_ids": [],
                    "member_count": 0,
                    "authoritative": True,
                    "left_team": True,
                    "in_team": False,
                }
            )
        )
        self.assertFalse(model.party_active)

    def test_team_rating_preview_keeps_overview_and_boss_banner(self):
        class Widget:
            def __init__(self):
                self.manager = "pack"
                self.options = {}

            def winfo_manager(self):
                return self.manager

            def pack_forget(self):
                self.manager = ""

            def pack(self, **options):
                self.manager = "pack"
                self.options.update(options)

            def configure(self, **options):
                self.options.update(options)

        window = object.__new__(DpsWindow)
        window.compact_mode = False
        window.team_rating_preview_layout_active = False
        window._monster_hp_row_count = 2
        window.summary = Widget()
        window.summary.options["height"] = (
            MAIN_SUMMARY_BASE_HEIGHT + MONSTER_HP_ROW_HEIGHT
        )
        window.metric_frame = Widget()
        window.monster_hp_canvas = Widget()
        window.table_panel = Widget()
        header_redraws = []
        window._draw_main_header = lambda: header_redraws.append(True)

        window._sync_team_rating_preview_layout(True)
        self.assertEqual(window.summary.winfo_manager(), "pack")
        self.assertEqual(window.metric_frame.winfo_manager(), "pack")
        self.assertEqual(window.monster_hp_canvas.winfo_manager(), "pack")
        self.assertEqual(
            window.summary.options["height"],
            MAIN_SUMMARY_BASE_HEIGHT + MONSTER_HP_ROW_HEIGHT,
        )

        window._sync_team_rating_preview_layout(False)
        self.assertEqual(window.summary.winfo_manager(), "pack")
        self.assertEqual(window.metric_frame.winfo_manager(), "pack")
        self.assertEqual(window.monster_hp_canvas.winfo_manager(), "pack")
        self.assertEqual(
            window.summary.options["height"],
            MAIN_SUMMARY_BASE_HEIGHT + MONSTER_HP_ROW_HEIGHT,
        )
        self.assertEqual(len(header_redraws), 2)

    def test_team_rating_preview_invalidates_only_changed_member_profile(self):
        class Model:
            profile_changed = False
            party_changed = False

            def ingest_profile(self, _payload):
                return self.profile_changed

            def ingest_party(self, _payload):
                return self.party_changed

        window = object.__new__(DpsWindow)
        window.model = Model()
        window.team_rating_preview_rows_dirty = False
        window.team_rating_preview_profile_cache = {
            SELF_ID: {"actor_id": SELF_ID},
            TEAMMATE_ID: {"actor_id": TEAMMATE_ID},
        }

        window._dispatch_message("profile", {"entity_id": TEAMMATE_ID})
        self.assertFalse(window.team_rating_preview_rows_dirty)
        self.assertIn(SELF_ID, window.team_rating_preview_profile_cache)
        self.assertIn(TEAMMATE_ID, window.team_rating_preview_profile_cache)

        window.model.profile_changed = True
        window._dispatch_message("profile", {"entity_id": TEAMMATE_ID})
        self.assertTrue(window.team_rating_preview_rows_dirty)
        self.assertIn(SELF_ID, window.team_rating_preview_profile_cache)
        self.assertNotIn(TEAMMATE_ID, window.team_rating_preview_profile_cache)

        window.team_rating_preview_rows_dirty = False
        window.model.party_changed = True
        window._dispatch_message("party", {"entity_ids": [SELF_ID]})
        self.assertTrue(window.team_rating_preview_rows_dirty)
        self.assertIn(SELF_ID, window.team_rating_preview_profile_cache)

    def test_rating_settings_default_off_and_team_preview_is_available(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'SHOW_EXTRAORDINARY_RATING_CONFIG_KEY = "show_extraordinary_rating"',
            source,
        )
        self.assertIn(
            'self.config.get(SHOW_EXTRAORDINARY_RATING_CONFIG_KEY, False)',
            source,
        )
        self.assertIn('"显示非凡评分"', source)
        self.assertIn(
            "self.settings_show_extraordinary_rating_var.get()", source
        )
        self.assertIn(
            "self.config[SHOW_EXTRAORDINARY_RATING_CONFIG_KEY]", source
        )
        self.assertIn('TEAM_RATING_PREVIEW_CONFIG_KEY = "team_rating_preview"', source)
        self.assertIn(
            'self.config.get(TEAM_RATING_PREVIEW_CONFIG_KEY, False)', source
        )
        self.assertIn("TEAM_RATING_PREVIEW_AVAILABLE = True", source)
        self.assertIn('"队伍非凡评分预览"', source)
        self.assertNotIn('"队伍非凡评分预览（暂不可用）"', source)
        self.assertIn(
            "self.settings_team_rating_preview_var.get()", source
        )
        self.assertNotIn("disabled=not TEAM_RATING_PREVIEW_AVAILABLE", source)
        self.assertIn(
            "self._sync_team_rating_preview_layout(\n"
            "            self._team_rating_preview_active()",
            source,
        )

    def test_compact_resize_survives_expand_and_restore_cycle(self):
        class Root:
            def __init__(self):
                self.value = "277x166+31+47"

            def geometry(self, value=None):
                if value is not None:
                    self.value = value
                return self.value

            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def winfo_x():
                return 31

            @staticmethod
            def winfo_y():
                return 47

            @staticmethod
            def winfo_screenwidth():
                return 1920

            @staticmethod
            def winfo_screenheight():
                return 1080

            @staticmethod
            def update_idletasks():
                return None

        window = object.__new__(DpsWindow)
        window.root = Root()
        window.config = {
            "compact_geometry": "277x166+31+47",
            "geometry": "590x400+31+47",
            "compact_layout_version": 3,
        }
        window.compact_mode = True
        window.main_meter_mode = "dps"
        window.show_total_damage = True
        window.show_dps = True
        window.show_damage_share = True
        window.show_critical_rate = True
        window.window_locked = False
        window.closing = False
        window.restore_geometry = {}
        window._apply_layout_mode = lambda: None

        self.assertEqual(window._initial_geometry(), "277x166+31+47")
        with mock.patch.dict(
            DpsWindow.toggle_compact_mode.__globals__,
            {"save_config": lambda _config: None},
        ):
            window.toggle_compact_mode()
            self.assertEqual(window.root.geometry(), "590x400+31+47")
            window.toggle_compact_mode()

        self.assertEqual(window.root.geometry(), "277x166+31+47")
        window.root.geometry("289x177+31+47")
        window._sync_compact_geometry_width()
        self.assertEqual(window.config["compact_geometry"], "289x177+31+47")
        self.assertEqual(window.root.geometry(), "289x177+31+47")

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

    def test_two_bosses_render_as_two_stable_full_height_hp_rows(self):
        class Canvas:
            def __init__(self):
                self.rectangles = []
                self.texts = []
                self.configured = []

            @staticmethod
            def delete(*_args):
                return None

            @staticmethod
            def winfo_width():
                return 500

            def configure(self, **kwargs):
                self.configured.append(kwargs)

            def create_rectangle(self, *args, **kwargs):
                self.rectangles.append((args, kwargs))

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

        first = MonsterStats(
            MONSTER_ID,
            name="Boss A",
            level=60,
            boss_rank=3,
            current_hp=50,
            max_hp=100,
            observed_max_hp=100,
        )
        second = MonsterStats(
            SECOND_MONSTER_ID,
            name="Boss B",
            level=61,
            boss_rank=3,
            current_hp=75,
            max_hp=100,
            observed_max_hp=100,
        )
        bosses = [first, second]
        window = object.__new__(DpsWindow)
        window.monster_hp_canvas = Canvas()
        window.summary = Canvas()
        window.model = type(
            "Model",
            (),
            {
                "current_bosses": lambda _self: list(bosses),
                "_monster_rank": lambda _self, monster: monster.boss_rank,
            },
        )()
        window._fit_main_actor_name = lambda value, _width: value
        window._ui_font = lambda _role: None

        window._draw_monster_hp()

        self.assertEqual(window.monster_hp_canvas.configured, [{"height": 72}])
        self.assertEqual(window.summary.configured, [{"height": 112}])
        self.assertEqual(len(window.monster_hp_canvas.rectangles), 4)
        self.assertEqual(len(window.monster_hp_canvas.texts), 2)
        self.assertEqual(
            [text[0][1] for text in window.monster_hp_canvas.texts], [18, 54]
        )
        self.assertEqual(
            [text[1]["text"] for text in window.monster_hp_canvas.texts],
            [
                "Lv.60   Boss A   50 / 100   50.0%",
                "Lv.61   Boss B   75 / 100   75.0%",
            ],
        )

        first.current_hp = 0
        first.death_confirmed = True
        second.current_hp = 25
        window.monster_hp_canvas.rectangles.clear()
        window.monster_hp_canvas.texts.clear()
        window.monster_hp_canvas.configured.clear()
        window.summary.configured.clear()

        window._draw_monster_hp()

        self.assertEqual(window.monster_hp_canvas.configured, [])
        self.assertEqual(window.summary.configured, [])
        self.assertEqual(len(window.monster_hp_canvas.rectangles), 3)
        self.assertEqual(len(window.monster_hp_canvas.texts), 2)
        self.assertEqual(
            [text[1]["text"] for text in window.monster_hp_canvas.texts],
            [
                "Lv.60   Boss A   0 / 100   0%",
                "Lv.61   Boss B   25 / 100   25.0%",
            ],
        )

        bosses[:] = [second]
        window.monster_hp_canvas.rectangles.clear()
        window.monster_hp_canvas.texts.clear()
        window._draw_monster_hp()

        self.assertEqual(window.monster_hp_canvas.configured, [{"height": 36}])
        self.assertEqual(window.summary.configured, [{"height": 76}])
        self.assertEqual(len(window.monster_hp_canvas.texts), 1)

    def test_first_believer_forecast_marker_moves_from_barney_to_anxia(self):
        barney = MonsterStats(
            MONSTER_ID,
            name="巴尼先生",
            template_id=7_100_209,
            current_hp=10,
            max_hp=100,
        )
        anxia = MonsterStats(
            SECOND_MONSTER_ID,
            name="安西娅",
            template_id=7_100_208,
            current_hp=70,
            max_hp=100,
        )

        self.assertEqual(
            enrage_marker_row_index([anxia, barney], anxia.entity_id), 1
        )

        barney.current_hp = 0
        barney.death_confirmed = True
        self.assertEqual(
            enrage_marker_row_index([anxia, barney], barney.entity_id), 0
        )

    def test_enrage_marker_moves_from_right_to_left_with_countdown(self):
        prediction = type(
            "Prediction",
            (),
            {"enrage_seconds": 480.0, "time_to_enrage_seconds": 480.0},
        )()
        self.assertEqual(enrage_marker_ratio(prediction), 1.0)
        prediction.time_to_enrage_seconds = 240.0
        self.assertEqual(enrage_marker_ratio(prediction), 0.5)
        prediction.time_to_enrage_seconds = 0.0
        self.assertEqual(enrage_marker_ratio(prediction), 0.0)

    def test_enrage_marker_respects_a_late_phase_hp_baseline(self):
        prediction = type(
            "Prediction",
            (),
            {
                "enrage_seconds": 300.0,
                "time_to_enrage_seconds": 300.0,
                "schedule_start_hp_percent": 70.0,
            },
        )()
        self.assertEqual(enrage_marker_ratio(prediction), 0.7)
        prediction.time_to_enrage_seconds = 150.0
        self.assertEqual(enrage_marker_ratio(prediction), 0.35)

    def test_ready_enrage_marker_floats_above_centered_unchanged_hp_bar(self):
        class Font:
            @staticmethod
            def measure(text):
                return len(text) * 6

        class Canvas:
            def __init__(self):
                self.configured = []
                self.rectangles = []
                self.texts = []
                self.lines = []
                self.polygons = []

            @staticmethod
            def delete(*_args):
                return None

            @staticmethod
            def winfo_width():
                return 500

            def configure(self, **kwargs):
                self.configured.append(kwargs)

            def create_rectangle(self, *args, **kwargs):
                self.rectangles.append((args, kwargs))

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

            def create_line(self, *args, **kwargs):
                self.lines.append((args, kwargs))

            def create_polygon(self, *args, **kwargs):
                self.polygons.append((args, kwargs))

        monster = MonsterStats(
            MONSTER_ID,
            name="测试 Boss",
            template_id=7_100_001,
            level=60,
            boss_rank=3,
            current_hp=50,
            max_hp=100,
            observed_max_hp=100,
        )
        prediction = type(
            "Prediction",
            (),
            {
                "state": "normal",
                "message": "正常 · +0:24",
                "calculating": False,
                "enrage_seconds": 120.0,
                "time_to_enrage_seconds": 60.0,
                "schedule_start_hp_percent": 100.0,
            },
        )()
        window = object.__new__(DpsWindow)
        window.monster_hp_canvas = Canvas()
        window.summary = Canvas()
        window.model = type(
            "Model",
            (),
            {
                "current_bosses": lambda _self: [monster],
                "_monster_rank": lambda _self, value: value.boss_rank,
                "combat_target_id": MONSTER_ID,
                "active_target_id": MONSTER_ID,
            },
        )()
        window.enrage_prediction = prediction
        window.enrage_prediction_visible = True
        window._monster_hp_row_count = 1
        window._monster_hp_prediction_spacing = 0
        window._fit_main_actor_name = lambda value, _width: value
        window._ui_font = lambda _role: Font()

        window._draw_monster_hp()

        self.assertEqual(window.monster_hp_canvas.configured, [{"height": 44}])
        self.assertEqual(window.summary.configured, [{"height": 84}])
        track = window.monster_hp_canvas.rectangles[0][0]
        label = window.monster_hp_canvas.rectangles[2][0]
        status = window.monster_hp_canvas.texts[-1][0]
        self.assertEqual((track[1], track[3]), (11, 41))
        self.assertEqual((label[1], label[3]), (0, 13))
        self.assertEqual(status[1], 26)
        self.assertEqual(status[1], (track[1] + track[3]) // 2)
        self.assertEqual(len(window.monster_hp_canvas.lines), 1)
        self.assertEqual(len(window.monster_hp_canvas.polygons), 1)

        first_label_width = label[2] - label[0]
        prediction.message = "危险 · -8:08"
        window._draw_monster_hp()
        second_label = window.monster_hp_canvas.rectangles[-1][0]
        self.assertEqual(second_label[2] - second_label[0], first_label_width)

    def test_first_believer_successor_overrides_stale_barney_marker(self):
        stale_barney = MonsterStats(
            MONSTER_ID,
            name="巴尼先生",
            template_id=7_100_209,
            current_hp=10,
            max_hp=100,
        )
        successor_anxia = MonsterStats(
            SECOND_MONSTER_ID,
            name="安西娅",
            template_id=7_100_210,
            current_hp=100,
            max_hp=100,
        )

        self.assertEqual(
            enrage_marker_row_index(
                [stale_barney, successor_anxia], stale_barney.entity_id
            ),
            1,
        )

    def test_other_dual_boss_marker_follows_active_target(self):
        first = MonsterStats(MONSTER_ID, name="Boss A", template_id=1)
        second = MonsterStats(SECOND_MONSTER_ID, name="Boss B", template_id=2)

        self.assertEqual(
            enrage_marker_row_index([first, second], second.entity_id), 1
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

    def test_dense_dps_columns_shrink_names_and_prioritize_life_metrics(self):
        window = object.__new__(DpsWindow)
        window.compact_mode = False
        window.main_meter_mode = "dps"
        window.history_meter_mode = "dps"
        window.show_total_damage = True
        window.show_dps = True
        window.show_damage_share = True
        window.show_critical_rate = True
        window.show_deaths = True
        window.show_revives = True
        window.show_death_duration = True

        columns = window._main_columns(720)
        self.assertLessEqual(columns["name_limit"], 200)
        self.assertGreaterEqual(
            columns["revives"] - columns["deaths"], 50
        )
        self.assertGreater(
            columns["death_time"] - columns["revives"],
            columns["revives"] - columns["deaths"],
        )

        history_columns = window._history_participant_columns(720)
        self.assertLessEqual(history_columns["name_limit"], 200)
        self.assertGreater(
            history_columns["death_time"] - history_columns["revives"],
            history_columns["revives"] - history_columns["deaths"],
        )

    def test_backend_resize_uses_adjustable_sidebar_and_hides_inactive_pages(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        build_source = source[
            source.index("    def _build_history_window(") : source.index(
                "    def _backend_nav_button("
            )
        ]
        self.assertIn("workspace = ModernSplitPane(", build_source)
        self.assertIn("on_resize=self._remember_backend_sidebar_width", build_source)
        self.assertIn("backend_sidebar_width", build_source)
        self.assertIn('getattr(self, "history_window", None) is window', source)
        self.assertIn("and self._start_native_window_move(window)", source)
        self.assertIn("self.root.after(16, apply_latest)", source)
        self.assertIn('font=self._ui_font("nav")', source)
        self.assertIn('"table_header": (self.ui_font_family, base + 1, "bold")', source)
        self.assertIn('table_font = self._ui_font(', source)
        self.assertGreaterEqual(source.count("font=table_font"), 4)
        self.assertIn('font=self._ui_font("table_header")', source)
        self.assertIn('font=self._ui_font("table")', source)
        for icon_name in ("close", "maximize", "restore", "minimize"):
            self.assertIn(f'"{icon_name}"', source)
        self.assertIn("def _titlebar_icon_button(", source)
        select_source = source[
            source.index("    def _select_backend_page(") : source.index(
                "    def _backend_action_button("
            )
        ]
        self.assertIn("frame.grid_remove()", select_source)
        settings_source = source[
            source.index("    def _select_settings_section(") : source.index(
                "    def _preview_font_size("
            )
        ]
        self.assertIn("frame.grid_remove()", settings_source)

    def test_backend_sidebar_uses_starry_art_anonymous_identity_and_tier_copy(self):
        project = Path(__file__).parent
        source = (project / "dps_meter.pyw").read_text(encoding="utf-8")
        build_source = (project / "build_exe.ps1").read_text(encoding="utf-8")

        self.assertIn(
            'SIDEBAR_ART_PATH = ASSET_DIR / "sidebar_starry_swing_v2.png"',
            source,
        )
        self.assertIn(
            'SIDEBAR_AVATAR_PATH = ASSET_DIR / "sidebar_nightwalker_avatar_v2.png"',
            source,
        )
        self.assertIn("def sidebar_art(", source)
        self.assertIn("def sidebar_avatar(", source)
        self.assertIn("def membership_badge(", source)
        sidebar_source = source[
            source.index("        account_strip = tk.Frame(") : source.index(
                "        page_host = tk.Frame("
            )
        ]
        self.assertNotIn("brand_mark", sidebar_source)
        self.assertNotIn('text="叨叨诡秘"', sidebar_source)
        self.assertIn("self.icons.sidebar_avatar(52)", sidebar_source)
        self.assertIn('text="匿名"', sidebar_source)
        self.assertNotIn('text="修改"', sidebar_source)
        self.assertIn("membership_label_for_card_tier(", sidebar_source)
        self.assertIn("membership_contract_text(", sidebar_source)
        self.assertIn('text="回到主窗口"', sidebar_source)
        self.assertNotIn('text="⌃  收起"', sidebar_source)
        self.assertIn("self._sync_backend_sidebar_status()", source)
        self.assertIn('("statistics", "analytics", "数据统计", False)', sidebar_source)
        self.assertIn('("upload", "peak", "巅峰记录", False)', sidebar_source)
        self.assertNotIn('text="筹备"', source)
        self.assertIn('text="记录每一次战斗"', source)
        self.assertIn('text="让数据说话"', source)
        self.assertIn('button, "#07323a", "#e5ffff", selected=True', source)
        self.assertIn(
            "assets/sidebar_starry_swing_v2.png=assets/sidebar_starry_swing_v2.png",
            build_source,
        )
        self.assertIn(
            "assets/sidebar_nightwalker_avatar_v2.png=assets/sidebar_nightwalker_avatar_v2.png",
            build_source,
        )
        art_path = project / "assets" / "sidebar_starry_swing_v2.png"
        self.assertTrue(art_path.is_file())
        with Image.open(art_path) as image:
            self.assertGreater(image.width, image.height)
            self.assertGreaterEqual(image.width, 1500)
            self.assertGreaterEqual(image.height, 1000)
        avatar_path = project / "assets" / "sidebar_nightwalker_avatar_v2.png"
        self.assertTrue(avatar_path.is_file())
        with Image.open(avatar_path) as image:
            self.assertEqual(image.width, image.height)
            self.assertGreaterEqual(image.width, 1000)
            self.assertIn("A", image.mode)

    def test_backend_page_switch_closes_active_dropdown_popup(self):
        class Owner:
            pass

        class Dropdown:
            def __init__(self, owner):
                self.owner = owner
                self.close_count = 0

            def _close_popup(self):
                self.close_count += 1
                setattr(
                    self.owner,
                    ModernDropdown._OWNER_ACTIVE_ATTRIBUTE,
                    None,
                )

        class Frame:
            def __init__(self):
                self.visible = None

            def grid(self, **_options):
                self.visible = True

            def grid_remove(self):
                self.visible = False

        owner = Owner()
        dropdown = Dropdown(owner)
        setattr(owner, ModernDropdown._OWNER_ACTIVE_ATTRIBUTE, dropdown)
        history_frame = Frame()
        updates_frame = Frame()
        window = object.__new__(DpsWindow)
        window.history_window = owner
        window.backend_current_page = "history"
        window.backend_pages = {
            "history": history_frame,
            "updates": updates_frame,
        }
        window._ensure_backend_window_opaque = lambda: None
        window._sync_backend_navigation = lambda: None
        window._sync_backend_window_topmost = lambda: None

        window._select_backend_page("updates")

        self.assertEqual(dropdown.close_count, 1)
        self.assertFalse(history_frame.visible)
        self.assertTrue(updates_frame.visible)

    def test_history_dungeon_and_boss_filters_use_plain_names(self):
        class Variable:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        dungeon_bosses = MODULE["HISTORY_DUNGEON_BOSSES"]
        dungeon, bosses = next(
            (name, values)
            for name, values in dungeon_bosses.items()
            if values
        )
        boss = bosses[0]
        window = object.__new__(DpsWindow)
        window.history_filter_time_var = None
        window.history_filter_dungeon_var = Variable(dungeon)
        window.history_filter_boss_var = Variable(boss)
        window.history_filter_result_var = None
        window._history_filter_value = lambda _key: ""

        filters = window._history_filter_payload()

        self.assertEqual(filters["dungeon"], dungeon)
        self.assertEqual(filters["boss"], boss)
        self.assertFalse(dungeon.startswith("副本  "))
        self.assertFalse(boss.startswith("Boss  "))

        class BossDropdown:
            choices = ()

        boss_dropdown = BossDropdown()
        window.history_filter_dropdowns = {"boss": boss_dropdown}
        window._sync_history_boss_filter_options()
        self.assertEqual(boss_dropdown.choices[0], "全部")
        self.assertTrue(
            all(not choice.startswith("Boss  ") for choice in boss_dropdown.choices)
        )

    def test_settings_apply_immediately_without_a_save_button(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        build_source = source[
            source.index("    def _build_backend_settings_page(") : source.index(
                "    def _settings_check_row("
            )
        ]
        dps_options_source = build_source[
            build_source.index("        dps_options =") : build_source.index(
                "        self.settings_show_effective_healing_var ="
            )
        ]

        self.assertNotIn('text="保存设置"', build_source)
        self.assertIn(
            'variable.trace_add("write", self._apply_live_ui_settings)',
            build_source,
        )
        self.assertIn('opaque_bar_cell = tk.Frame(general_controls', build_source)
        self.assertIn('"DPS颜色条保持不透明"', build_source)
        self.assertNotIn("DPS颜色条保持不透明", dps_options_source)
        self.assertIn(
            '"本人数据实时更新；队友技能、暴击率和穿刺率仅在服务器实际返回结算明细时显示。"',
            dps_options_source,
        )
        self.assertIn(
            'caption in {"显示暴击率", "显示穿刺率"}',
            dps_options_source,
        )
        self.assertNotIn("detail_note_row", dps_options_source)
        self.assertIn("help_text=help_text", source)
        for role in (
            "settings",
            "settings_strong",
            "settings_title",
            "settings_number",
        ):
            self.assertIn(f'self._ui_font("{role}")', build_source)
        self.assertIn("height=54", build_source)
        save_source = source[
            source.index("    def _save_ui_settings(") : source.index(
                "    def _feedback_selected_history("
            )
        ]
        self.assertIn("previous_font_size = self.ui_font_size", save_source)
        self.assertIn(
            "if self.ui_font_size != previous_font_size:", save_source
        )

        window = object.__new__(DpsWindow)
        calls = []
        window._save_ui_settings = lambda: calls.append("applied")
        window.settings_live_apply_ready = False
        window.settings_live_apply_running = False
        window._apply_live_ui_settings()
        self.assertEqual(calls, [])

        window.settings_live_apply_ready = True
        window._apply_live_ui_settings()
        self.assertEqual(calls, ["applied"])
        self.assertFalse(window.settings_live_apply_running)

    def test_ui_text_and_numbers_share_one_font_family(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        font_source = source[
            source.index("    def _initialize_ui_fonts(") : source.index(
                "    def _configure_tk_dpi_scaling("
            )
        ]
        application_source = source[source.index("class DpsWindow:") :]

        self.assertIn("self.number_font_family = self.ui_font_family", font_source)
        for named_font in (
            "TkDefaultFont",
            "TkTextFont",
            "TkFixedFont",
            "TkMenuFont",
            "TkHeadingFont",
        ):
            self.assertIn(f'"{named_font}"', font_source)
        self.assertNotIn('tk_font_spec("Segoe UI",', application_source)
        self.assertIn('tk_font_spec("Segoe UI Symbol",', application_source)

    def test_hps_detail_copy_omits_peak_and_response_sample_count(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('"峰值 HPS "', source)
        self.assertNotIn('f"峰值HPS {peak_text}', source)
        self.assertNotIn('f"（{sample_count}次）"', source)
        self.assertGreaterEqual(source.count('"响应 快"'), 2)

    def test_hotkey_settings_explain_single_key_and_modifier_rules(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"单键：Home、End、Insert、Delete、PageUp、PageDown、Pause、F1-F12',
            source,
        )
        self.assertIn('"字母或数字：需要搭配 Alt 或 Ctrl。', source)
        self.assertIn(
            '"关闭开关只会停用快捷键，不会清除已录入的按键。"',
            source,
        )
        self.assertIn("hotkey_control = ModernCheckControl(", source)
        self.assertIn("opacity_help = ModernHelpBadge(", source)
        self.assertNotIn('status = (\n                    "等待按键"', source)
        self.assertNotIn('"已启用"', source)

    def test_settings_switch_is_antialiased_without_changing_its_size(self):
        image = render_switch_control_image(
            "#141619", "#30363d", "#c3cbd4", False
        )

        self.assertEqual(image.size, (44, 26))
        self.assertEqual(image.getpixel((0, 0)), (20, 22, 25))
        self.assertEqual(image.getpixel((13, 13)), (195, 203, 212))
        self.assertGreater(len(image.getcolors(maxcolors=44 * 26)), 3)

    def test_dt_and_footer_visible_labels_match_requested_copy(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        header_source = source[
            source.index("    def _draw_main_header(") : source.index(
                "    def _toggle_lock_from_header("
            )
        ]
        self.assertIn('"taken": "承伤" if compact else "总承伤"', header_source)
        self.assertNotIn("Taken 总承伤", header_source)
        self.assertNotIn("Share 承伤占比", header_source)
        self.assertNotIn("Max Hit", header_source)
        self.assertIn('("dps", "DPS伤害设置")', source)
        self.assertIn('("hps", "HPS治疗设置")', source)
        self.assertIn('("dt", "DT承伤设置")', source)
        self.assertIn('("boss", "BOSS首领设置")', source)
        self.assertIn('settings_section("DT承伤设置")', source)
        self.assertIn('settings_section("BOSS首领设置")', source)
        self.assertIn('("显示总承伤", self.settings_show_taken_var)', source)
        self.assertIn(
            '("显示已归类伤害", self.settings_show_boss_damage_var)', source
        )
        for label in ("DPS伤害", "HPS治疗", "DT承伤", "BOSS首领"):
            self.assertIn(f'"{label}"', source)
        self.assertEqual(source.count('text="QQ群:1094925831 165966739"'), 2)

    def test_dense_dps_columns_shrink_names_and_prioritize_life_metrics(self):
        window = object.__new__(DpsWindow)
        window.compact_mode = False
        window.main_meter_mode = "dps"
        window.history_meter_mode = "dps"
        window.show_total_damage = True
        window.show_dps = True
        window.show_damage_share = True
        window.show_critical_rate = True
        window.show_deaths = True
        window.show_revives = True
        window.show_death_duration = True

        columns = window._main_columns(720)
        self.assertLessEqual(columns["name_limit"], 200)
        self.assertGreaterEqual(
            columns["revives"] - columns["deaths"], 50
        )
        self.assertGreater(
            columns["death_time"] - columns["revives"],
            columns["revives"] - columns["deaths"],
        )

        history_columns = window._history_participant_columns(720)
        self.assertLessEqual(history_columns["name_limit"], 200)
        self.assertGreater(
            history_columns["death_time"] - history_columns["revives"],
            history_columns["revives"] - history_columns["deaths"],
        )

    def test_backend_resize_uses_adjustable_sidebar_and_hides_inactive_pages(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        build_source = source[
            source.index("    def _build_history_window(") : source.index(
                "    def _backend_nav_button("
            )
        ]
        self.assertIn("workspace = ModernSplitPane(", build_source)
        self.assertIn("on_resize=self._remember_backend_sidebar_width", build_source)
        self.assertIn("backend_sidebar_width", build_source)
        select_source = source[
            source.index("    def _select_backend_page(") : source.index(
                "    def _backend_action_button("
            )
        ]
        self.assertIn("frame.grid_remove()", select_source)
        settings_source = source[
            source.index("    def _select_settings_section(") : source.index(
                "    def _preview_font_size("
            )
        ]
        self.assertIn("frame.grid_remove()", settings_source)

    def test_dt_visible_labels_are_chinese_only(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        header_source = source[
            source.index("    def _draw_main_header(") : source.index(
                "    def _toggle_lock_from_header("
            )
        ]
        self.assertIn('"taken": "承伤" if compact else "总承伤"', header_source)
        self.assertNotIn("Taken 总承伤", header_source)
        self.assertNotIn("Share 承伤占比", header_source)
        self.assertNotIn("Max Hit", header_source)
        self.assertIn('("dt", "DT承伤设置")', source)
        self.assertIn('settings_section("DT承伤设置")', source)
        self.assertIn('("显示总承伤", self.settings_show_taken_var)', source)
        self.assertIn('text="QQ群:1094925831 165966739"', source)

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

    def test_hps_columns_follow_independent_visibility_settings(self):
        window = object.__new__(DpsWindow)
        window.compact_mode = False
        window.main_meter_mode = "hps"
        window.history_meter_mode = "hps"
        window.show_effective_healing = True
        window.show_hps = True
        window.show_overheal_rate = True

        columns = window._main_columns(620)
        self.assertLess(columns["effective"], columns["hps"])
        self.assertLess(columns["hps"], columns["overheal"])
        self.assertNotIn("response", columns)

        window.show_effective_healing = False
        window.show_overheal_rate = False
        main_reduced = window._main_columns(620)
        history_reduced = window._history_participant_columns(720)
        for reduced in (main_reduced, history_reduced):
            self.assertNotIn("effective", reduced)
            self.assertIn("hps", reduced)
            self.assertNotIn("overheal", reduced)
            self.assertNotIn("response", reduced)

        window.show_hps = False
        self.assertEqual(window._enabled_healing_metrics(), ())
        self.assertEqual(set(window._main_columns(620)), {"name_limit"})

    def test_boss_columns_follow_independent_visibility_settings(self):
        window = object.__new__(DpsWindow)
        window.compact_mode = False
        window.main_meter_mode = "boss"
        window.show_boss_damage = True
        window.show_boss_share = True
        window.show_boss_hits = True
        window.show_boss_max_hit = True

        columns = window._main_columns(720)
        self.assertLess(columns["boss_damage"], columns["boss_share"])
        self.assertLess(columns["boss_share"], columns["boss_hits"])
        self.assertLess(columns["boss_hits"], columns["boss_max_hit"])

        window.show_boss_share = False
        window.show_boss_hits = False
        reduced = window._main_columns(720)
        self.assertIn("boss_damage", reduced)
        self.assertNotIn("boss_share", reduced)
        self.assertNotIn("boss_hits", reduced)
        self.assertIn("boss_max_hit", reduced)

    def test_boss_settings_apply_to_live_columns(self):
        class Variable:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        window = object.__new__(DpsWindow)
        window.ui_font_size = 14
        window.compact_mode = False
        for name in (
            "settings_font_size_var",
            "settings_opacity_var",
            "settings_target_boss_lookup_var",
            "settings_show_names_var",
            "settings_show_damage_var",
            "settings_show_dps_var",
            "settings_show_share_var",
            "settings_show_critical_var",
            "settings_show_penetration_var",
            "settings_keep_dps_bars_opaque_var",
            "settings_show_extraordinary_rating_var",
            "settings_team_rating_preview_var",
            "settings_boss_enrage_prediction_var",
            "settings_show_deaths_var",
            "settings_show_revives_var",
            "settings_show_death_duration_var",
            "settings_show_taken_var",
            "settings_show_taken_share_var",
            "settings_show_effective_healing_var",
            "settings_show_hps_var",
            "settings_show_overheal_rate_var",
        ):
            setattr(window, name, None)
        window.settings_show_boss_damage_var = Variable(False)
        window.settings_show_boss_share_var = Variable(True)
        window.settings_show_boss_hits_var = Variable(False)
        window.settings_show_boss_max_hit_var = Variable(True)
        window._team_rating_preview_active = lambda: False
        window._sync_team_rating_preview_layout = mock.Mock()
        window._sync_compact_geometry_width = mock.Mock()
        window._main_minimum_width = lambda: 430
        window.root = mock.Mock()
        window._configure_ui_fonts = mock.Mock()
        window._sync_action_buttons = mock.Mock()
        window._draw_main_header = mock.Mock()
        window._draw_main_rows = mock.Mock()
        window._schedule_main_content_overlay_sync = mock.Mock()
        window._draw_history_participant_header = mock.Mock()
        window._render_history_selection = mock.Mock()
        window._save_preferences = mock.Mock()

        window._save_ui_settings()

        self.assertFalse(window.show_boss_damage)
        self.assertTrue(window.show_boss_share)
        self.assertFalse(window.show_boss_hits)
        self.assertTrue(window.show_boss_max_hit)
        self.assertEqual(window._enabled_boss_metrics(), (
            "boss_share",
            "boss_max_hit",
        ))
        window._save_preferences.assert_called_once_with()

    def test_transparency_overlay_redraws_empty_message_without_black_mask(self):
        class Canvas:
            def __init__(self):
                self.texts = []
                self.rectangles = []

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

            def create_rectangle(self, *args, **kwargs):
                self.rectangles.append((args, kwargs))

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
            draw_content=False,
            opaque_bar_layer=True,
        )

        self.assertEqual(
            [options["text"] for _args, options in main_canvas.texts],
            ["暂无伤害记录"],
        )
        self.assertEqual(overlay_canvas.texts, [])
        self.assertEqual(overlay_canvas.rectangles, [])

    def test_opaque_dps_rows_do_not_overlay_total_dps_or_black_background(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("main_content_overlay_dps", source)
        self.assertNotIn("_draw_main_content_overlay_dps", source)
        self.assertNotIn("opaque_background", source)
        self.assertIn(
            'window.attributes("-alpha", DPS_BAR_COLOR_STRENGTH)', source
        )
        self.assertIn("draw_bars=False", source)
        self.assertIn("opaque_bar_layer=True", source)
        self.assertIn("if opaque_bar_layer", source)
        self.assertIn(
            "else blend_color(BG, color, DPS_BAR_COLOR_STRENGTH)", source
        )

    def test_opaque_dps_rows_are_opt_in_and_only_apply_to_dps(self):
        window = object.__new__(DpsWindow)
        window.window_alpha = 0.8
        window.main_meter_mode = "dps"
        window.keep_dps_bars_opaque = False

        self.assertFalse(window._main_content_overlay_requested())

        window.keep_dps_bars_opaque = True
        self.assertTrue(window._main_content_overlay_requested())

        window.main_meter_mode = "hps"
        self.assertFalse(window._main_content_overlay_requested())

        window.main_meter_mode = "dt"
        self.assertFalse(window._main_content_overlay_requested())

        window.main_meter_mode = "dps"
        window.window_alpha = 1.0
        self.assertFalse(window._main_content_overlay_requested())

    def test_disabled_opaque_dps_option_does_not_schedule_an_overlay(self):
        class Root:
            def __init__(self):
                self.scheduled = []

            def after(self, *args):
                self.scheduled.append(args)
                return "scheduled"

        window = object.__new__(DpsWindow)
        window.root = Root()
        window.closing = False
        window.main_content_overlay_supported = True
        window.main_content_overlay_sync_after_id = None
        window.main_content_overlay_root_geometry = (1, 2, 3, 4)
        window.main_content_overlay_window = None
        window.keep_dps_bars_opaque = False
        window.main_meter_mode = "dps"
        window.window_alpha = 0.8

        window._schedule_main_content_overlay_sync()

        self.assertEqual(window.root.scheduled, [])
        self.assertIsNone(window.main_content_overlay_root_geometry)

    def test_opaque_bar_overlay_is_clipped_to_the_visible_rows_area(self):
        self.assertEqual(
            clipped_overlay_bounds(40, 60, 500, 300, 48, 150, 480, 160),
            (48, 150, 480, 160),
        )
        self.assertEqual(
            clipped_overlay_bounds(40, 60, 500, 300, 20, 40, 560, 350),
            (40, 60, 500, 300),
        )
        self.assertIsNone(
            clipped_overlay_bounds(40, 60, 500, 300, 600, 150, 100, 100)
        )
        self.assertIsNone(
            clipped_overlay_bounds(40, 60, 500, 300, 48, 150, 0, 160)
        )

    def test_restarting_overlay_sync_cancels_stale_position_callback(self):
        class Root:
            def __init__(self):
                self.cancelled = []
                self.scheduled = []

            def after_cancel(self, value):
                self.cancelled.append(value)

            def after(self, delay, callback):
                self.scheduled.append((delay, callback))
                return "new-position"

        window = object.__new__(DpsWindow)
        window.root = Root()
        window.closing = False
        window.main_content_overlay_supported = True
        window.main_content_overlay_sync_after_id = "stale-position"
        window.keep_dps_bars_opaque = True
        window.main_meter_mode = "dps"
        window.window_alpha = 0.8

        window._schedule_main_content_overlay_sync(restart=True)

        self.assertEqual(window.root.cancelled, ["stale-position"])
        self.assertEqual(window.root.scheduled[0][0], 16)
        self.assertEqual(window.main_content_overlay_sync_after_id, "new-position")

    def test_rows_layout_change_hides_stale_overlay_before_redraw(self):
        class OverlayWindow:
            def __init__(self):
                self.withdraw_count = 0

            @staticmethod
            def winfo_exists():
                return True

            def withdraw(self):
                self.withdraw_count += 1

        overlay = OverlayWindow()
        window = object.__new__(DpsWindow)
        window.main_content_overlay_window = overlay
        window.main_content_overlay_root_geometry = (1, 2, 3, 4)
        window.keep_dps_bars_opaque = True
        window.main_meter_mode = "dps"
        window.window_alpha = 0.8
        window._draw_main_rows = mock.Mock()
        window._schedule_main_content_overlay_sync = mock.Mock()

        window._main_rows_canvas_configure()

        self.assertEqual(overlay.withdraw_count, 1)
        self.assertIsNone(window.main_content_overlay_root_geometry)
        window._draw_main_rows.assert_called_once_with()
        window._schedule_main_content_overlay_sync.assert_called_once_with(
            restart=True
        )

    def test_transparency_overlay_is_independent_from_main_window_alpha(self):
        class OverlayWindow:
            def __init__(self):
                self.options = {}

            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def withdraw():
                return None

            @staticmethod
            def title(_value):
                return None

            @staticmethod
            def configure(**_options):
                return None

            @staticmethod
            def overrideredirect(_value):
                return None

            @staticmethod
            def transient(_root):
                return None

            def attributes(self, key, value):
                self.options[key] = value

            @staticmethod
            def update_idletasks():
                return None

        root = object()
        overlay = OverlayWindow()
        canvases = [object()]
        window = object.__new__(DpsWindow)
        window.root = root
        window.main_content_overlay_supported = True
        window.main_content_overlay_window = None
        window.main_content_overlay_rows = None
        window.main_content_overlay_sync_after_id = None
        window.main_content_overlay_root_geometry = None
        window.main_content_overlay_click_through_ready = False
        window.main_content_overlay_topmost = None
        window._prepare_toplevel_dpi = lambda _window: None
        window._win32_root_handle = lambda _window: 321
        window._win32_extended_style = lambda _hwnd: 0
        window._set_win32_extended_style = lambda *_args: True
        with mock.patch.object(MODULE["tk"], "Toplevel", return_value=overlay), mock.patch.object(
            MODULE["tk"], "Canvas", side_effect=canvases
        ):
            self.assertTrue(window._ensure_main_content_overlay())

        self.assertTrue(window.main_content_overlay_supported)
        self.assertTrue(window.main_content_overlay_click_through_ready)
        self.assertEqual(
            overlay.options["-alpha"], DPS_BAR_COLOR_STRENGTH
        )

    def test_backend_topmost_is_independent_from_main_window(self):
        class BackendWindow:
            def __init__(self):
                self.topmost = True
                self.topmost_values = []

            @staticmethod
            def winfo_exists():
                return True

            def attributes(self, key, *values):
                if key != "-topmost":
                    raise AssertionError(key)
                if values:
                    self.topmost = bool(values[0])
                    self.topmost_values.append(self.topmost)
                return self.topmost

        backend = BackendWindow()
        window = object.__new__(DpsWindow)
        window.history_window = backend
        window.history_pin_button = None
        window.backend_topmost = False
        window._preferred_main_topmost = lambda: True
        native_topmost_values = []
        window._set_window_topmost_noactivate = (
            lambda _window, value: native_topmost_values.append(value) or True
        )

        window._sync_backend_window_topmost()
        window.backend_topmost = True
        window._preferred_main_topmost = lambda: False
        window._sync_backend_window_topmost()

        self.assertEqual(backend.topmost_values, [False, True])
        self.assertEqual(native_topmost_values, [False, True])

    def test_backend_pin_toggles_persists_and_updates_visual_state(self):
        class BackendWindow:
            def __init__(self):
                self.topmost = False

            @staticmethod
            def winfo_exists():
                return True

            def attributes(self, key, *values):
                if key != "-topmost":
                    raise AssertionError(key)
                if values:
                    self.topmost = bool(values[0])
                return self.topmost

        class FakeButton:
            def __init__(self):
                self._icon_name = "pin_off"
                self._normal_bg = MODULE["SURFACE"]
                self._hover_bg = MODULE["PANEL_2"]
                self._normal_fg = MODULE["TEXT"]
                self._hover_fg = MODULE["TEXT"]
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

        backend = BackendWindow()
        button = FakeButton()
        window = object.__new__(DpsWindow)
        window.history_window = backend
        window.history_pin_button = button
        window.backend_topmost = False
        window.config = {}
        window.icons = FakeIcons()
        native_topmost_values = []
        window._set_window_topmost_noactivate = (
            lambda _window, value: native_topmost_values.append(value) or True
        )
        save = mock.Mock()

        with mock.patch.dict(
            DpsWindow.toggle_backend_topmost.__globals__, {"save_config": save}
        ):
            window.toggle_backend_topmost()
            self.assertTrue(window.backend_topmost)
            self.assertTrue(window.config["backend_topmost"])
            self.assertTrue(backend.topmost)
            self.assertEqual(button._icon_name, "pin")
            self.assertEqual(button._normal_fg, MODULE["ACCENT"])
            self.assertEqual(button.image, ("pin", 16, MODULE["ACCENT"]))

            window.toggle_backend_topmost()

        self.assertFalse(window.backend_topmost)
        self.assertFalse(window.config["backend_topmost"])
        self.assertFalse(backend.topmost)
        self.assertEqual(button._icon_name, "pin_off")
        self.assertEqual(native_topmost_values, [True, False])
        self.assertEqual(save.call_count, 2)

    def test_main_pin_toggle_does_not_change_backend_topmost(self):
        window = object.__new__(DpsWindow)
        window.window_locked = False
        window._preferred_main_topmost = lambda: False
        window._set_main_topmost = mock.Mock()
        window.skill_window = None
        window.history_window = object()
        window.feedback_window = None
        window._sync_backend_window_topmost = mock.Mock()
        window._sync_action_buttons = mock.Mock()
        window._save_preferences = mock.Mock()

        window.toggle_topmost()

        window._set_main_topmost.assert_called_once_with(True)
        window._sync_backend_window_topmost.assert_not_called()
        window._sync_action_buttons.assert_called_once_with()
        window._save_preferences.assert_called_once_with()

    def test_main_and_backend_titlebars_include_independent_pin_buttons(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        main_source = source[
            source.index("    def _build_ui(") : source.index(
                "    def _sync_main_meter_tabs("
            )
        ]
        backend_source = source[
            source.index("    def _build_history_window(") : source.index(
                "    def _backend_nav_button("
            )
        ]
        self.assertIn("self.pin_button = self._main_icon_button(", main_source)
        self.assertIn("self.toggle_topmost", main_source)
        self.assertIn(
            "self.history_pin_button = self._titlebar_icon_button(", backend_source
        )
        self.assertIn("self.toggle_backend_topmost", backend_source)
        self.assertIn('window.attributes("-topmost", self.backend_topmost)', backend_source)
        self.assertNotIn('self.config["topmost"] = True', source)
        for removed_name in (
            "backend_main_topmost_restore",
            "backend_topmost_guard_after_id",
            "_enforce_settings_window_not_topmost",
        ):
            self.assertNotIn(removed_name, source)

    def test_history_detail_toolbar_is_fixed_and_hidden_in_browser_mode(self):
        class Toolbar:
            def __init__(self):
                self.place_options = None
                self.lift_count = 0
                self.hidden = False

            def place(self, **options):
                self.place_options = options

            def lift(self):
                self.lift_count += 1

            def place_forget(self):
                self.hidden = True

        toolbar = Toolbar()
        window = object.__new__(DpsWindow)
        window.history_detail_sticky_toolbar = toolbar

        window._sync_history_detail_toolbar_visibility(True)

        self.assertEqual(
            toolbar.place_options,
            {"x": 0, "y": 0, "relwidth": 1.0, "height": 44},
        )
        self.assertEqual(toolbar.lift_count, 1)

        window._sync_history_detail_toolbar_visibility(False)

        self.assertTrue(toolbar.hidden)

    def test_transparent_main_overlay_owns_rows_without_black_mask(self):
        class Root:
            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def update_idletasks():
                return None

            @staticmethod
            def winfo_rootx():
                return 40

            @staticmethod
            def winfo_rooty():
                return 60

            @staticmethod
            def winfo_width():
                return 500

            @staticmethod
            def winfo_height():
                return 300

            @staticmethod
            def state():
                return "normal"

            @staticmethod
            def attributes(_name):
                return False

        class OverlayWindow:
            @staticmethod
            def state():
                return "normal"

            @staticmethod
            def withdraw():
                return None

            @staticmethod
            def update_idletasks():
                return None

            @staticmethod
            def winfo_exists():
                return True

        calls = []
        window = object.__new__(DpsWindow)
        window.main_content_overlay_sync_after_id = "pending"
        window.closing = False
        window.root = Root()
        window.window_alpha = 0.8
        window.keep_dps_bars_opaque = True
        window.main_meter_mode = "dps"
        window.main_content_overlay_window = OverlayWindow()
        window.main_content_overlay_rows = object()
        window.rows_canvas = object()
        window.main_content_overlay_topmost = False
        window._set_window_topmost_noactivate = lambda *_args: True
        window.main_content_overlay_root_geometry = None
        window._ensure_main_content_overlay = lambda: True
        bounds_calls = []
        place_calls = []
        window._main_content_overlay_layout = lambda: (
            (40, 60, 500, 300, 48, 150, 480, 160),
            (48, 150, 480, 160),
        )
        window._set_main_content_overlay_bounds = (
            lambda *args: bounds_calls.append(args)
        )
        window._place_overlay_canvas = (
            lambda *args: place_calls.append(args) or True
        )
        window._draw_main_rows_on_canvas = (
            lambda canvas, **options: calls.append((canvas, options))
        )
        window._sync_main_content_overlay()

        self.assertEqual(
            bounds_calls,
            [(window.main_content_overlay_window, 480, 160, 48, 150)],
        )
        self.assertEqual(
            place_calls,
            [(window.main_content_overlay_rows, window.rows_canvas, 48, 150)],
        )
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0][0], window.rows_canvas)
        self.assertTrue(calls[0][1]["update_scroll_state"])
        self.assertFalse(calls[0][1]["draw_bars"])
        self.assertIs(calls[1][0], window.main_content_overlay_rows)
        self.assertFalse(calls[1][1]["update_scroll_state"])
        self.assertFalse(calls[1][1]["draw_content"])
        self.assertTrue(calls[1][1]["opaque_bar_layer"])
        self.assertNotIn("opaque_background", calls[1][1])

    def test_opaque_row_overlay_failure_restores_normal_translucent_rows(self):
        class Root:
            @staticmethod
            def winfo_exists():
                return True

            @staticmethod
            def state():
                return "normal"

            @staticmethod
            def attributes(_name):
                return False

        calls = []
        window = object.__new__(DpsWindow)
        window.main_content_overlay_sync_after_id = "pending"
        window.closing = False
        window.root = Root()
        window.window_alpha = 0.8
        window.keep_dps_bars_opaque = True
        window.main_meter_mode = "dps"
        window.rows_canvas = object()
        window._ensure_main_content_overlay = lambda: False
        window._draw_main_rows_on_canvas = (
            lambda canvas, **options: calls.append((canvas, options))
        )

        window._sync_main_content_overlay()

        self.assertEqual(calls, [
            (window.rows_canvas, {"update_scroll_state": True})
        ])

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

    def test_hps_main_rows_show_effective_hps_and_overheal_without_average(self):
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
                    "overheal_rate_partial": True,
                },
                {
                    "actor_id": TEAMMATE_ID,
                    "profession_id": 1_200_002,
                    "effective_healing": 0,
                    "observed_total_healing": 500,
                    "total_healing": 500,
                    "hps": 0,
                    "overheal_rate": 1.0,
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
        self.assertNotIn("25.0%*", rendered)
        self.assertIn("100.0%", rendered)
        self.assertNotIn("500ms", rendered)
        self.assertEqual(format_overheal_rate(0.25, partial=True), "25.0%")
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
        self.assertEqual(image_args, (48, 7))
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
        window._draw_history_critical_luck = lambda: None

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

    def test_history_list_performance_switches_without_profession_text(self):
        window = object.__new__(DpsWindow)
        summary = {
            "my_dps": 1234,
            "my_damage": 56789,
            "my_hps": 321,
            "my_effective_healing": 9876,
            "my_taken": 250,
            "team_taken": 1000,
            "boss_observed_damage": 750,
            "boss_classification_ratio": 0.75,
            "boss_damage_coverage": "observed_partial",
            "profession_name": "空想家途径",
        }

        window.history_meter_mode = "dps"
        self.assertEqual(
            window._history_list_performance(summary),
            ("1,234 DPS", "56,789 伤害"),
        )
        window.history_meter_mode = "hps"
        self.assertEqual(
            window._history_list_performance(summary),
            ("321 HPS", "9,876 有效治疗"),
        )
        window.history_meter_mode = "dt"
        self.assertEqual(
            window._history_list_performance(summary),
            ("250 DT", "承伤占比 25.0%"),
        )
        window.history_meter_mode = "boss_damage"
        self.assertEqual(
            window._history_list_performance(summary),
            ("750 首领伤害", "归类比例 75.0%"),
        )
        for mode in ("dps", "hps", "dt", "boss_damage"):
            window.history_meter_mode = mode
            self.assertNotIn(
                "空想家途径", " ".join(window._history_list_performance(summary))
            )

    def test_history_visible_fields_are_ordered_and_drive_optional_columns(self):
        class Font:
            @staticmethod
            def measure(value):
                return len(str(value)) * 7

        self.assertEqual(
            normalize_history_visible_fields(None),
            ("character", "extraordinary_rating", "performance", "result"),
        )
        self.assertEqual(
            normalize_history_visible_fields(
                ["result", "unknown", "character", "result"]
            ),
            ("character", "result"),
        )
        self.assertEqual(normalize_history_visible_fields([]), ())

        window = object.__new__(DpsWindow)
        window.window_dpi = 96
        window._ui_font = lambda _kind: Font()
        window.history_visible_fields = {
            "character",
            "extraordinary_rating",
            "performance",
            "result",
        }
        content_width = window._history_list_content_width(500)
        columns = window._history_list_columns(content_width)
        self.assertGreater(content_width, 500)
        self.assertLess(columns["content_right"], columns["character"])
        self.assertLess(
            columns["character"], columns["extraordinary_rating"]
        )
        self.assertLess(
            columns["extraordinary_rating"], columns["performance"]
        )
        self.assertLess(columns["performance"], columns["result_left"])
        self.assertLess(columns["result"], columns["operation_left"])

        window.history_visible_fields = set()
        compact_columns = window._history_list_columns(
            window._history_list_content_width(500)
        )
        for key in (
            "character",
            "extraordinary_rating",
            "performance",
            "result",
        ):
            self.assertNotIn(key, compact_columns)

    def test_history_backend_mousewheel_scrolls_the_page_canvas(self):
        class Canvas:
            def __init__(self):
                self.calls = []

            def yview_scroll(self, amount, unit):
                self.calls.append((amount, unit))

        class Event:
            delta = -240
            num = 0

        window = object.__new__(DpsWindow)
        window.backend_current_page = "history"
        window.history_page_canvas = Canvas()

        result = window._scroll_backend_page(Event())

        self.assertEqual(result, "break")
        self.assertEqual(window.history_page_canvas.calls, [(2, "units")])

    def test_all_history_overview_cards_have_distinct_decor(self):
        class Canvas:
            def __init__(self):
                self.calls = []

            def delete(self, *args):
                self.calls.append(("delete", args))

            @staticmethod
            def winfo_width():
                return 42

            @staticmethod
            def winfo_height():
                return 42

            def __getattr__(self, name):
                if name.startswith("create_"):
                    return lambda *args, **kwargs: self.calls.append(
                        (name, args, kwargs)
                    )
                raise AttributeError(name)

        window = object.__new__(DpsWindow)
        expected_shape = {
            "battle_count": "create_rectangle",
            "today_count": "create_rectangle",
            "average_dps": "create_rectangle",
            "highest_damage": "create_oval",
            "average_duration": "create_oval",
        }
        for key, shape in expected_shape.items():
            canvas = Canvas()
            window._draw_history_overview_decor(canvas, key)
            self.assertIn(shape, [call[0] for call in canvas.calls], key)

    def test_history_encounter_title_never_concatenates_multiple_bosses(self):
        self.assertEqual(
            DpsWindow._history_encounter_title(
                {
                    "boss_count": 2,
                    "boss_name": "安西娅",
                    "boss_names": ["安西娅", "巴尼先生"],
                    "dungeon_name": "五月庄园·城堡",
                }
            ),
            "五月庄园·城堡",
        )
        self.assertEqual(
            DpsWindow._history_encounter_title(
                {
                    "boss_count": 1,
                    "stage_name": "一号信徒",
                    "boss_name": "安西娅",
                }
            ),
            "一号信徒",
        )

    def test_history_pages_are_balanced_to_avoid_sparse_last_page(self):
        self.assertEqual(DpsWindow._balanced_history_page_size(23, 10), 8)
        self.assertEqual(DpsWindow._balanced_history_page_size(31, 10), 8)
        self.assertEqual(DpsWindow._balanced_history_page_size(20, 10), 10)
        self.assertEqual(DpsWindow._balanced_history_page_size(7, 10), 10)

    def test_history_pagination_uses_a_compact_elided_page_window(self):
        self.assertEqual(ModernPagination._visible_pages(1, 3), (1, 2, 3))
        self.assertEqual(
            ModernPagination._visible_pages(1, 12),
            (1, 2, 3, "ellipsis", 12),
        )
        self.assertEqual(
            ModernPagination._visible_pages(6, 12),
            (1, "ellipsis", 6, "ellipsis", 12),
        )
        self.assertEqual(
            ModernPagination._visible_pages(12, 12),
            (1, "ellipsis", 10, 11, 12),
        )

    def test_history_pagination_shows_current_record_range(self):
        class Control:
            def __init__(self):
                self.values = []

            def set(self, page, page_count):
                self.values.append((page, page_count))

        class Label:
            def __init__(self):
                self.text = ""

            def configure(self, **values):
                self.text = values.get("text", self.text)

        window = object.__new__(DpsWindow)
        window.history_pagination_control = Control()
        window.history_page_range_label = Label()
        window.history_page_number = 2
        window.history_page_count = 4
        window.history_page_size = 10
        window.history_effective_page_size = 8
        window.history_total_records = 29
        window.history_records = [{} for _index in range(8)]

        window._draw_history_pagination()

        self.assertEqual(window.history_pagination_control.values, [(2, 4)])
        self.assertEqual(window.history_page_range_label.text, "当前 9–16 条")

        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        draw_source = source[
            source.index("    def _draw_history_pagination(") : source.index(
                "    def _toggle_history_select_all("
            )
        ]
        self.assertNotIn("tk.Label(", draw_source)
        self.assertIn("class ModernPagination(tk.Canvas):", source)
        self.assertIn("footer = tk.Frame(parent, bg=SURFACE, height=50)", source)

    def test_history_dt_mode_uses_serialized_exact_taken_fields(self):
        class Label:
            def __init__(self):
                self.values = {}

            def configure(self, **values):
                self.values.update(values)

        record = {
            "monster": {"name": "测试首领"},
            "duration_seconds": 12,
            "team_size": 2,
            "team_taken": 1_000,
            "damage_taken": [
                {
                    "actor_id": SELF_ID,
                    "name": "甲",
                    "taken": 600,
                    "share": 0.6,
                    "max_hit": 350,
                },
                {
                    "actor_id": TEAMMATE_ID,
                    "name": "乙",
                    "taken": 400,
                    "share": 0.4,
                    "max_hit": 500,
                },
            ],
            "participants": [
                {"actor_id": SELF_ID, "name": "甲", "damage": 9_999_999}
            ],
            "healers": [
                {"actor_id": SELF_ID, "name": "甲", "effective_healing": 8_888_888}
            ],
        }
        window = object.__new__(DpsWindow)
        window.history_meter_mode = "dt"
        window.history_selected_actor = 0
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
        window._selected_history_record = lambda: record
        window._history_is_boss = lambda _record: True
        window._history_timestamp = lambda _record: "2026-09-02  21:00"
        window._history_display_duration = lambda _record, _healing: 12
        window._set_history_detail_mode = lambda _mode: None
        window._draw_history_list = lambda: None
        window._draw_history_participant_header = lambda: None
        window._draw_history_participants = lambda: None
        window._draw_history_skills = lambda: None
        window._draw_history_critical_luck = lambda: None

        window._render_history_selection()

        self.assertEqual(window.history_selected_actor, SELF_ID)
        self.assertEqual(window.history_total_value.values["text"], "1,000")
        self.assertEqual(
            window.history_total_value._caption_label.values["text"],
            "团队总承伤",
        )
        self.assertEqual(window.history_dps_value.values["text"], "60.0%")
        self.assertEqual(
            window.history_dps_value._caption_label.values["text"],
            "最高承伤占比",
        )
        self.assertEqual(window.history_team_value.values["text"], "2 人")
        self.assertEqual(
            window.history_detail_label.values["text"],
            "承伤当前仅提供玩家汇总，暂无可靠的技能归属",
        )

        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        for mode, caption in (
            ("dps", "DPS伤害"),
            ("hps", "HPS治疗"),
            ("dt", "DT承伤"),
        ):
            self.assertIn(f'("{mode}", "{caption}")', source)
        self.assertIn(
            'if mode not in {"dps", "hps", "dt", "boss_damage"}',
            source,
        )

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

    def test_share_is_dps_only_regardless_of_main_or_history_tab(self):
        record = {
            "participants": [
                {"name": "伤害玩家", "damage": 123_400, "dps": 12_340}
            ],
            "healers": [
                {"name": "治疗玩家", "effective_healing": 999_999, "hps": 99_999}
            ],
            "damage_taken": [
                {"name": "承伤玩家", "taken": 888_888}
            ],
        }
        copied = []
        window = object.__new__(DpsWindow)
        window.hide_names = False
        window.history_meter_mode = "hps"
        window.history_window = object()
        window._selected_history_record = lambda: record
        window._copy_share_text = (
            lambda text, parent: copied.append((text, parent))
        )

        window._share_history()

        self.assertEqual(copied[-1][0], "伤害玩家:1.23w")
        self.assertNotIn("治疗玩家", copied[-1][0])
        self.assertNotIn("承伤玩家", copied[-1][0])

        class Model:
            @staticmethod
            def build_combat_record(_reason):
                return record

        window.model = Model()
        window.main_meter_mode = "dt"
        window.root = object()

        window._share_current()

        self.assertEqual(copied[-1][0], "伤害玩家:1.23w")
        self.assertNotIn("治疗玩家", copied[-1][0])
        self.assertNotIn("承伤玩家", copied[-1][0])

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

    def test_available_update_is_cached_without_opening_a_window(self):
        update = UpdateInfo(
            available=True,
            latest_version="0.1.2",
            download_path="/api/v1/dps/update/download",
            sha256="a" * 64,
            size=1024,
            filename="dps.exe",
        )
        refreshed = []
        window = object.__new__(DpsWindow)
        window.closing = False
        window.update_check_in_progress = True
        window.pending_update = None
        window._refresh_update_page = lambda status="": refreshed.append(status)
        window.show_backend = mock.Mock()

        window._handle_update_check_result(
            {"manual": False, "update": update, "error": ""}
        )

        self.assertIs(window.pending_update, update)
        self.assertFalse(window.update_check_in_progress)
        self.assertEqual(refreshed, [""])
        window.show_backend.assert_not_called()
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("def _show_update_window", source)
        self.assertNotIn("self.update_window", source)

    def test_update_log_page_scrolls_without_affecting_other_pages(self):
        class Canvas:
            def __init__(self):
                self.calls = []

            def yview_scroll(self, amount, units):
                self.calls.append((amount, units))

        window = object.__new__(DpsWindow)
        window.backend_current_page = "updates"
        window.update_log_canvas = Canvas()

        result = window._scroll_backend_page(mock.Mock(delta=-240, num="??"))

        self.assertEqual(result, "break")
        self.assertEqual(window.update_log_canvas.calls, [(2, "units")])
        window._scroll_backend_page(mock.Mock(delta=0, num=4))
        self.assertEqual(
            window.update_log_canvas.calls,
            [(2, "units"), (-1, "units")],
        )

        window.backend_current_page = "history"
        self.assertIsNone(
            window._scroll_backend_page(mock.Mock(delta=-120, num="??"))
        )
        self.assertEqual(len(window.update_log_canvas.calls), 2)

        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        updates_source = source[
            source.index("    def _build_backend_updates_page(") : source.index(
                "    def _build_backend_settings_page("
            )
        ]
        self.assertIn("self.update_log_canvas = tk.Canvas(", updates_source)
        self.assertIn("self.update_log_scrollbar = ModernScrollbar(", updates_source)
        self.assertIn("self.update_log_canvas.create_window(", updates_source)
        self.assertIn("scrollregion=bounds", updates_source)
        self.assertIn(
            'window.bind("<MouseWheel>", self._scroll_backend_page', source
        )

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

    def test_successful_update_uses_pending_metadata_and_stops_capture_first(self):
        window = object.__new__(DpsWindow)
        window.pending_update = UpdateInfo(
            available=True,
            latest_version="0.1.2",
            download_path="/api/v1/dps/update/download",
            sha256="a" * 64,
            size=1024,
            filename="Dps-Logs-v0.1.2.exe",
        )
        window.update_downloading = True
        window.pending_update_install = None
        window.update_status_label = mock.Mock()
        window.update_action_button = mock.Mock()
        window.root = mock.Mock()
        window.close_status_text = ""
        module_globals = window._handle_update_downloaded.__globals__
        original_frozen = module_globals["IS_FROZEN"]
        original_update_dir = module_globals["UPDATE_DIR"]
        original_executable = module_globals["APP_EXECUTABLE_PATH"]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            module_globals["IS_FROZEN"] = True
            module_globals["UPDATE_DIR"] = directory
            module_globals["APP_EXECUTABLE_PATH"] = directory / "old.exe"
            try:
                window._handle_update_downloaded(
                    directory / "Dps-Logs-v0.1.2.update.exe"
                )
            finally:
                module_globals["IS_FROZEN"] = original_frozen
                module_globals["UPDATE_DIR"] = original_update_dir
                module_globals["APP_EXECUTABLE_PATH"] = original_executable

        self.assertFalse(window.update_downloading)
        self.assertIsNotNone(window.pending_update_install)
        assert window.pending_update_install is not None
        self.assertEqual(
            window.pending_update_install[1].name,
            "Dps-Logs-v0.1.2.exe",
        )
        window.root.after.assert_called_once_with(120, window.close)
        self.assertIn("安全停止", window.close_status_text)

    def test_feedback_window_dimensions_scale_with_the_shared_ui_font(self):
        window = object.__new__(DpsWindow)
        window.ui_font_size = 13
        self.assertEqual(window._feedback_window_dimensions(), (520, 520))
        window.ui_font_size = 18
        self.assertEqual(window._feedback_window_dimensions(), (520, 600))

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
        batch = DpsWindow._feedback_history_batch_summary(complete_record)
        self.assertEqual(len(batch["participants"][0]["skills"]), 3)
        self.assertEqual(len(batch["participants"][0]["targets"]), 2)
        self.assertNotIn("capture_pipeline_at_archive", batch)
        self.assertEqual(
            DpsWindow._normalize_feedback_record_ids(
                ["encounter-new", "encounter-new", "", "encounter-old"]
            ),
            ("encounter-new", "encounter-old"),
        )

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

    def test_history_feedback_uses_multi_selection_action(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('feedback_tag = f"history-feedback:{index}"', source)
        self.assertIn('actions, "反馈选中", self._feedback_selected_histories', source)
        self.assertIn("self.show_feedback(selected_record_ids=selected_ids)", source)
        self.assertNotIn('actions, "批量导出"', source)
        self.assertNotIn('actions, "导出本场"', source)
        self.assertNotIn('\n            "自定义",\n', source)

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

    def test_team_response_diagnostics_report_members_and_real_progress(self):
        worker = HookWorker(MODULE["queue"].Queue(), MODULE["threading"].Event())
        record = {
            "method": "RetCommonCombatStatisticsByTeam",
            "filetime_100ns": BASE_FILETIME,
        }
        rows = [
            (
                "team_stat",
                {
                    "actor_id": SELF_ID,
                    "user_token": "self-token",
                    "absolute_damage": 1_000,
                },
            ),
            (
                "team_stat",
                {
                    "actor_id": TEAMMATE_ID,
                    "user_token": "team-token",
                    "absolute_damage": 2_000,
                },
            ),
        ]

        worker._record_team_stats_response_diagnostics(record, rows)
        worker._record_team_stats_response_diagnostics(record, rows)

        snapshot = worker.diagnostic_snapshot()
        self.assertEqual(snapshot["team_stats_parsed_responses"], 2)
        self.assertEqual(snapshot["team_stats_last_member_count"], 2)
        self.assertEqual(snapshot["team_stats_last_damage_total"], 3_000)
        self.assertEqual(snapshot["team_stats_damage_progress_count"], 1)
        self.assertEqual(
            snapshot["team_stats_last_progress_filetime"], BASE_FILETIME
        )

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

    def test_known_catalog_non_boss_cannot_be_promoted_by_runtime_boss_fields(self):
        trash_id = MONSTER_ID
        boss_id = SECOND_MONSTER_ID
        model = CombatModel(
            run_id="known-non-boss-guard-test",
            target_catalog={
                "7102829": {"boss_type": 0, "level": 52},
                "7102834": {
                    "boss_type": 3,
                    "name": "Ray Biber",
                    "level": 52,
                },
            },
        )
        model.ingest_identity({"entity_id": SELF_ID})
        initial_session = model.session_number
        initial_encounter_id = model.encounter_id

        model.ingest_active_boss(
            {
                "entity_id": trash_id,
                "template_id": 7_102_829,
                "name": "Boss",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
                "max_hp": 69_274,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        trash = model.monsters[trash_id]
        self.assertEqual(trash.entity_type, "Monster")
        self.assertEqual(trash.boss_type, 0)
        self.assertEqual(trash.boss_rank, 0)
        self.assertEqual(model._monster_rank(trash), 0)
        self.assertIsNone(model.combat_target_id)

        model.ingest(damage(1, SELF_ID, trash_id, 3_255_874))
        common = team_stat(2, SELF_ID, 3_255_874)
        common["full_snapshot"] = True
        self.assertFalse(model.ingest_team_stat(common))
        self.assertEqual(model.events, [])
        self.assertEqual(model.stats, {})
        self.assertEqual(model.first_damage_time, 0.0)
        self.assertEqual(model.session_number, initial_session)
        self.assertEqual(model.encounter_id, initial_encounter_id)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertIsNone(model.build_combat_record())

        model.ingest_active_boss(
            {
                "entity_id": boss_id,
                "template_id": 7_102_834,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
                "max_hp": 1_000_000,
                "filetime_100ns": BASE_FILETIME + 30_000,
            }
        )
        model.ingest(damage(4, SELF_ID, boss_id, 100_000))
        self.assertEqual(model.combat_target_id, boss_id)
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)

    def test_late_known_non_boss_identity_cancels_false_standalone_encounter(self):
        trash_id = MONSTER_ID
        boss_id = SECOND_MONSTER_ID
        model = CombatModel(
            run_id="late-known-non-boss-guard-test",
            target_catalog={
                "7102829": {"boss_type": 0, "level": 52},
                "7102834": {
                    "boss_type": 3,
                    "name": "Ray Biber",
                    "level": 52,
                },
            },
        )
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_profile(
            {
                "entity_id": trash_id,
                "name": "Boss",
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
                "filetime_100ns": BASE_FILETIME,
            }
        )
        model.ingest(damage(1, SELF_ID, trash_id, 50_000))
        common = team_stat(2, SELF_ID, 3_255_874)
        common["full_snapshot"] = True
        self.assertTrue(model.ingest_team_stat(common))
        self.assertEqual(model.combat_target_id, trash_id)
        self.assertEqual(model.stats[SELF_ID].damage, 3_255_874)

        self.assertTrue(
            model.ingest_profile(
                {
                    "entity_id": trash_id,
                    "template_id": 7_102_829,
                    "entity_type": "Monster",
                    "boss_type": 0,
                    "boss_rank": 0,
                    "filetime_100ns": BASE_FILETIME + 30_000,
                }
            )
        )
        self.assertIsNone(model.combat_target_id)
        self.assertEqual(model.events, [])
        self.assertEqual(model.stats, {})
        self.assertEqual(model.first_damage_time, 0.0)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertIsNone(model.build_combat_record())
        self.assertEqual(
            model.team_damage_states[SELF_ID].baseline_absolute,
            3_255_874,
        )

        model.ingest_profile(
            {
                "entity_id": boss_id,
                "template_id": 7_102_834,
                "entity_type": "Boss",
                "boss_type": 3,
                "boss_rank": 3,
                "filetime_100ns": BASE_FILETIME + 40_000,
            }
        )
        model.ingest(damage(3, SELF_ID, boss_id, 100_000))
        self.assertEqual(model.combat_target_id, boss_id)
        self.assertEqual(model.stats[SELF_ID].damage, 100_000)

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

    def test_late_boss_identity_preserves_opening_clock_and_team_total(self):
        model = CombatModel(run_id="late-boss-opening-clock-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )

        first_hit_100ns = BASE_FILETIME
        for index in range(240):
            event = damage(index, TEAMMATE_ID, MONSTER_ID, 100)
            event["filetime_100ns"] = first_hit_100ns + index * 250_000
            model.ingest(event)

        opening_snapshot_100ns = BASE_FILETIME + 65_000_000
        self.assertFalse(
            model.ingest_team_stat(
                {
                    "filetime_100ns": opening_snapshot_100ns,
                    "actor_id": TEAMMATE_ID,
                    "absolute_damage": 24_000,
                    "server_time": 1_788_020_043,
                    "full_snapshot": True,
                }
            )
        )
        self.assertEqual(model.first_damage_time, 0.0)

        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "template_id": 7_103_401,
                "boss_type": 3,
                "boss_rank": 3,
                "filetime_100ns": BASE_FILETIME + 80_000_000,
            }
        )
        model.ingest_team_stat(
            {
                "filetime_100ns": BASE_FILETIME + 82_000_000,
                "actor_id": TEAMMATE_ID,
                "absolute_damage": 25_000,
                "server_time": 1_788_020_043,
                "full_snapshot": True,
            }
        )

        expected_start = model._event_seconds(
            {"filetime_100ns": first_hit_100ns}
        )
        self.assertEqual(len(model.pending_target_events), 0)
        self.assertEqual(len(model.events), 240)
        self.assertEqual(model.first_damage_time, expected_start)
        self.assertEqual(model.encounter_start_signal_100ns, first_hit_100ns)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 25_000)
        self.assertEqual(
            sum(actor.damage for actor in model.stats.values()), 25_000
        )
        self.assertEqual(model.duration(expected_start + 10.0), 10.0)

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

    def test_opening_member_counter_rollover_does_not_reset_shared_dps_clock(self):
        model = CombatModel(run_id="opening-member-rollover-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID, NEARBY_ID]})

        for sequence, actor_id, old_counter in (
            (1, TEAMMATE_ID, 900_000),
            (2, NEARBY_ID, 800_000),
        ):
            stale = team_stat(
                sequence, actor_id, old_counter, server_time=100 + sequence
            )
            stale["full_snapshot"] = True
            self.assertFalse(model.ingest_team_stat(stale))

        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(3, SELF_ID, MONSTER_ID, 100))
        encounter_id = model.encounter_id
        session_number = model.session_number
        first_damage_time = model.first_damage_time

        for sequence, actor_id, new_counter in (
            (4, TEAMMATE_ID, 25_000),
            (5, NEARBY_ID, 40_000),
        ):
            current = team_stat(
                sequence, actor_id, new_counter, server_time=200 + sequence
            )
            current["full_snapshot"] = True
            self.assertTrue(model.ingest_team_stat(current))

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.session_number, session_number)
        self.assertEqual(model.first_damage_time, first_damage_time)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.stats[SELF_ID].damage, 100)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 25_000)
        self.assertEqual(model.stats[NEARBY_ID].damage, 40_000)
        self.assertEqual(
            sum(actor.damage for actor in model.stats.values()), 65_100
        )

        self.assertTrue(
            model.ingest_team_stat(
                team_stat(6, TEAMMATE_ID, 35_000, server_time=204)
            )
        )
        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 35_000)

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

    def test_new_pull_zero_epoch_clears_stale_counters_before_opening_damage(self):
        """Reproduce the 1.7-second fragment observed before encounter 000023."""
        model = CombatModel(run_id="opening-zero-epoch-reset-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID, NEARBY_ID]})

        for sequence, actor_id, stale_damage in (
            (1, TEAMMATE_ID, 136_499),
            (2, NEARBY_ID, 127_310),
        ):
            stale = team_stat(sequence, actor_id, stale_damage)
            stale["full_snapshot"] = True
            self.assertFalse(model.ingest_team_stat(stale))

        for sequence, actor_id in (
            (3, TEAMMATE_ID),
            (4, NEARBY_ID),
        ):
            zero = team_stat(sequence, actor_id, 0, server_time=1_788_182_471)
            zero["full_snapshot"] = True
            zero["omitted_zero"] = True
            self.assertFalse(model.ingest_team_stat(zero))

        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(5, SELF_ID, MONSTER_ID, 100))
        encounter_id = model.encounter_id

        first = team_stat(
            6, TEAMMATE_ID, 32_573, server_time=1_788_182_471
        )
        first["full_snapshot"] = True
        second = team_stat(
            7, NEARBY_ID, 6_579, server_time=1_788_182_471
        )
        second["full_snapshot"] = True
        self.assertTrue(model.ingest_team_stat(first))
        self.assertTrue(model.ingest_team_stat(second))

        self.assertEqual(model.encounter_id, encounter_id)
        self.assertEqual(model.pop_completed_combats(), [])
        self.assertEqual(model.stats[SELF_ID].damage, 100)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 32_573)
        self.assertEqual(model.stats[NEARBY_ID].damage, 6_579)

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
        expected_start = model._event_seconds(
            {"filetime_100ns": shared_start}
        )
        self.assertEqual(model.encounter_start_signal_100ns, shared_start)
        self.assertEqual(model.first_damage_time, expected_start)
        self.assertEqual(model.last_damage_time, expected_start)
        self.assertEqual(model.duration(expected_start + 5.0), 5.0)
        self.assertTrue(model.active(expected_start + 5.0))
        self.assertEqual(model.stats, {})
        self.assertEqual(
            model.monsters[MONSTER_ID].last_hp_drop_100ns, shared_start
        )

        # A zero/unchanged reconciliation before the first team damage must
        # not erase the already-running shared clock.
        model._recompute()
        self.assertEqual(model.first_damage_time, expected_start)
        self.assertEqual(model.last_damage_time, expected_start)

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
        self.assertEqual(model.first_damage_time, expected_start)

    def test_boss_combat_state_starts_single_observer_before_any_attack(self):
        model = CombatModel(run_id="boss-state-single-observer")
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
                "boss_rank": 3,
            }
        )
        boss_start = BASE_FILETIME + 2 * 10_000_000

        self.assertTrue(
            model.ingest_combat_state(
                {
                    "entity_id": MONSTER_ID,
                    "in_combat": True,
                    "filetime_100ns": boss_start,
                }
            )
        )
        expected_start = model._event_seconds(
            {"filetime_100ns": boss_start}
        )
        self.assertEqual(model.encounter_start_signal_100ns, boss_start)
        self.assertEqual(model.first_damage_time, expected_start)
        self.assertEqual(model.last_damage_time, expected_start)
        self.assertEqual(model.stats, {})
        self.assertEqual(model.duration(expected_start + 4.0), 4.0)

        teammate_hit = damage(1, TEAMMATE_ID, MONSTER_ID, 250_000)
        teammate_hit["filetime_100ns"] = boss_start + 3 * 10_000_000
        model.ingest(teammate_hit)
        self.assertEqual(model.first_damage_time, expected_start)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 250_000)
        self.assertNotIn(SELF_ID, model.stats)

    def test_boss_identity_after_combat_state_recovers_original_start(self):
        model = CombatModel(run_id="boss-state-before-identity")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        boss_start = BASE_FILETIME + 1 * 10_000_000

        self.assertTrue(
            model.ingest_combat_state(
                {
                    "entity_id": MONSTER_ID,
                    "in_combat": True,
                    "filetime_100ns": boss_start,
                }
            )
        )
        self.assertEqual(model.first_damage_time, 0.0)
        self.assertTrue(
            model.ingest_profile(
                {
                    "entity_id": MONSTER_ID,
                    "entity_type": "Boss",
                    "boss_rank": 3,
                }
            )
        )

        self.assertEqual(model.encounter_start_signal_100ns, boss_start)
        self.assertEqual(
            model.first_damage_time,
            model._event_seconds({"filetime_100ns": boss_start}),
        )

    def test_earlier_boss_state_backdates_team_hit_without_changing_metrics(self):
        model = CombatModel(run_id="boss-state-backdates-team-hit")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        hit = damage(1, TEAMMATE_ID, MONSTER_ID, 345_678)
        hit["filetime_100ns"] = BASE_FILETIME + 3 * 10_000_000
        hit["critical"] = True
        hit["penetrating"] = True
        model.ingest(hit)
        actor = model.stats[TEAMMATE_ID]
        raw_metrics = (
            actor.damage,
            actor.hits,
            actor.damage_hits,
            actor.critical_hits,
            actor.penetration_hits,
            actor.skills[hit["skill_id"]].damage,
            actor.skills[hit["skill_id"]].hits,
            tuple(model.events),
        )

        earlier_start = BASE_FILETIME + 1 * 10_000_000
        self.assertTrue(
            model.ingest_combat_state(
                {
                    "entity_id": MONSTER_ID,
                    "in_combat": True,
                    "filetime_100ns": earlier_start,
                }
            )
        )
        actor = model.stats[TEAMMATE_ID]
        self.assertEqual(model.encounter_start_signal_100ns, earlier_start)
        self.assertEqual(
            model.first_damage_time,
            model._event_seconds({"filetime_100ns": earlier_start}),
        )
        self.assertEqual(
            (
                actor.damage,
                actor.hits,
                actor.damage_hits,
                actor.critical_hits,
                actor.penetration_hits,
                actor.skills[hit["skill_id"]].damage,
                actor.skills[hit["skill_id"]].hits,
                tuple(model.events),
            ),
            raw_metrics,
        )

    def test_non_boss_and_training_dummy_combat_state_do_not_arm_boss_clock(self):
        model = CombatModel(run_id="boss-state-target-guards")
        model.ingest_identity({"entity_id": SELF_ID})
        normal_id = SECOND_MONSTER_ID
        dummy_id = THIRD_MONSTER_ID
        model.ingest_profile(
            {"entity_id": normal_id, "entity_type": "Monster", "boss_rank": 0}
        )
        model.ingest_profile(
            {
                "entity_id": dummy_id,
                "name": "训练木桩",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )

        for offset, entity_id in enumerate((normal_id, dummy_id), start=1):
            self.assertTrue(
                model.ingest_combat_state(
                    {
                        "entity_id": entity_id,
                        "in_combat": True,
                        "filetime_100ns": BASE_FILETIME + offset * 10_000_000,
                    }
                )
            )
        self.assertEqual(model.first_damage_time, 0.0)
        self.assertEqual(model.encounter_start_signal_100ns, 0)

    def test_team_stats_teammate_damage_starts_healer_clock_without_hp_drop(self):
        model = CombatModel(run_id="healer-team-stats-start-test")
        healer_id = SELF_ID
        damage_actor_id = TEAMMATE_ID
        tokens = ["healer-token", "damage-token"]
        model.ingest_identity({"entity_id": healer_id})
        model.ingest_party(
            {
                "entity_ids": [damage_actor_id],
                "member_count": 2,
                "authoritative": True,
                "user_tokens": tokens,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "测试 Boss",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest_active_boss(
            {
                "entity_id": MONSTER_ID,
                "name": "测试 Boss",
                "filetime_100ns": BASE_FILETIME,
            }
        )

        for sequence, (actor_id, token) in enumerate(
            ((healer_id, tokens[0]), (damage_actor_id, tokens[1])), start=1
        ):
            self.assertFalse(
                model.ingest_team_stat(
                    {
                        "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                        "actor_id": actor_id,
                        "user_token": token,
                        "team_tokens": tokens,
                        "absolute_damage": 0,
                        "omitted_zero": True,
                        "full_snapshot": True,
                    }
                )
            )
        self.assertEqual(model.first_damage_time, 0.0)

        shared_start = BASE_FILETIME + 2 * 10_000_000
        self.assertTrue(
            model.ingest_team_stat(
                {
                    "filetime_100ns": shared_start,
                    "actor_id": damage_actor_id,
                    "user_token": tokens[1],
                    "team_tokens": tokens,
                    "absolute_damage": 125_000,
                    "full_snapshot": True,
                }
            )
        )

        expected_start = model._event_seconds({"filetime_100ns": shared_start})
        self.assertEqual(model.encounter_start_signal_100ns, shared_start)
        self.assertEqual(model.first_damage_time, expected_start)
        self.assertEqual(model.last_damage_time, expected_start)
        self.assertEqual(model.stats[damage_actor_id].damage, 125_000)
        self.assertNotIn(healer_id, model.stats)
        self.assertEqual(
            model.healing_duration(expected_start + 5.0),
            model.duration(expected_start + 5.0),
        )
        self.assertIsNotNone(
            model.combat_clock_snapshot(expected_start + 5.0)
        )

    def test_earlier_boss_hp_evidence_backdates_team_stats_start(self):
        model = CombatModel(run_id="team-start-backdate-test")
        tokens = ["healer-token", "damage-token"]
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
                "user_tokens": tokens,
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
        for sequence, actor_id in enumerate((SELF_ID, TEAMMATE_ID), start=1):
            model.ingest_team_stat(
                {
                    "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                    "actor_id": actor_id,
                    "team_tokens": tokens,
                    "absolute_damage": 0,
                    "omitted_zero": True,
                    "full_snapshot": True,
                }
            )

        team_stats_start = BASE_FILETIME + 3 * 10_000_000
        self.assertTrue(
            model.ingest_team_stat(
                {
                    "filetime_100ns": team_stats_start,
                    "actor_id": TEAMMATE_ID,
                    "team_tokens": tokens,
                    "absolute_damage": 125_000,
                    "full_snapshot": True,
                }
            )
        )
        earlier_hp_start = BASE_FILETIME + 1 * 10_000_000
        self.assertTrue(
            model.ingest_monster(
                {
                    "entity_id": MONSTER_ID,
                    "current_hp": 999_000,
                    "max_hp": 1_000_000,
                    "filetime_100ns": earlier_hp_start,
                }
            )
        )

        expected_start = model._event_seconds(
            {"filetime_100ns": earlier_hp_start}
        )
        self.assertEqual(model.encounter_start_signal_100ns, earlier_hp_start)
        self.assertEqual(model.first_damage_time, expected_start)
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 125_000)

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
            self.assertEqual(model.first_damage_time, expected_start)
            self.assertEqual(model.last_damage_time, expected_start)
            self.assertTrue(model.active(expected_start + 0.5))

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
        participant = next(
            row
            for row in record["participants"]
            if row["actor_id"] == TEAMMATE_ID
        )
        unclassified = next(
            row for row in participant["skills"] if row["skill_id"] == 0
        )
        self.assertEqual(unclassified["damage"], 150)
        self.assertEqual(unclassified["share"], 0.75)
        self.assertEqual(
            sum(row["damage"] for row in participant["skills"]),
            participant["damage"],
        )
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

    def test_settlement_skills_wait_for_final_common_and_then_activate(self):
        model = CombatModel(run_id="stage-skill-delayed-common-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {
                "entity_ids": [TEAMMATE_ID],
                "member_count": 2,
                "authoritative": True,
            }
        )
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
        self.assertTrue(model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 150)))
        death_time = BASE_FILETIME + 4 * 10_000
        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 1_000,
                "death_confirmed": True,
                "filetime_100ns": death_time,
            }
        )

        summary_id = "stage-skill-before-final-common"
        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": summary_id,
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

        self.assertIsNone(model.active_stage_skill_snapshot(TEAMMATE_ID))
        self.assertEqual(
            model.pending_stage_skill_snapshots[TEAMMATE_ID].actor_damage,
            200,
        )
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 150)
        self.assertEqual(
            model.stage_summaries[summary_id]["validation"][
                "pending_server_skill_actor_ids"
            ],
            [TEAMMATE_ID],
        )
        self.assertEqual(
            model.stage_summaries[summary_id]["validation"][
                "server_skill_actor_ids"
            ],
            [],
        )

        self.assertTrue(model.ingest_team_stat(team_stat(6, TEAMMATE_ID, 200)))

        teammate = model.stats[TEAMMATE_ID]
        self.assertNotIn(TEAMMATE_ID, model.pending_stage_skill_snapshots)
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
        self.assertEqual(
            model.stage_summaries[summary_id]["validation"][
                "pending_server_skill_actor_ids"
            ],
            [],
        )
        self.assertEqual(
            model.stage_summaries[summary_id]["validation"][
                "server_skill_actor_ids"
            ],
            [TEAMMATE_ID],
        )
        record = model.build_combat_record("target_defeated")
        teammate_record = next(
            row for row in record["participants"] if row["actor_id"] == TEAMMATE_ID
        )
        self.assertEqual(teammate_record["skill_source"], "server_stage_summary")
        self.assertEqual(
            sum(row["damage"] for row in teammate_record["skills"]),
            teammate_record["damage"],
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
        self.assertNotIn("average_ms", healer["response"])
        self.assertEqual(healer["response"]["fastest_ms"], 500.0)
        self.assertEqual(healer["response"]["slowest_ms"], 500.0)
        self.assertEqual(healer["response"]["samples"], 1)
        self.assertEqual(healer["response"]["covered_target_count"], 1)

    def test_team_health_summary_aggregates_only_complete_bound_samples(self):
        model = CombatModel(run_id="team-health-summary")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))

        self.assertTrue(
            model.ingest_actor_health(
                actor_health(BASE_FILETIME + 10_000, SELF_ID, 20_000, 25_000)
            )
        )
        self.assertTrue(
            model.ingest_actor_health(
                actor_health(BASE_FILETIME + 20_000, TEAMMATE_ID, 23_100, 26_700)
            )
        )

        summary = model.team_health_summary()
        self.assertTrue(summary["available"])
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["expected_member_count"], 2)
        self.assertEqual(summary["observed_member_count"], 2)
        self.assertEqual(summary["current_hp"], 43_100)
        self.assertEqual(summary["max_hp"], 51_700)
        self.assertEqual(summary["lowest"]["actor_id"], SELF_ID)
        self.assertEqual(summary["lowest"]["ratio"], 0.8)
        self.assertEqual(format_team_health_number(summary["current_hp"]), "4.31万")
        self.assertEqual(format_team_health_number(summary["max_hp"]), "5.17万")
        self.assertEqual(format_team_health_percent(summary["ratio"]), "83.4%")
        self.assertEqual(format_team_health_percent(summary["lowest"]["ratio"]), "80%")

        model.ingest_monster(
            {
                "entity_id": MONSTER_ID,
                "current_hp": 0,
                "max_hp": 1_000,
                "death_confirmed": True,
                "filetime_100ns": BASE_FILETIME + 30_000,
            }
        )
        model.ingest_actor_health(
            actor_health(BASE_FILETIME + 40_000, SELF_ID, 25_000, 25_000)
        )
        model.ingest_actor_health(
            actor_health(BASE_FILETIME + 50_000, TEAMMATE_ID, 26_700, 26_700)
        )
        healed = model.team_health_summary()
        self.assertEqual(healed["ratio"], 1.0)
        self.assertEqual(healed["lowest"]["actor_id"], SELF_ID)
        self.assertEqual(healed["lowest"]["ratio"], 0.8)

        model.party_member_count = 3
        incomplete = model.team_health_summary()
        self.assertFalse(incomplete["available"])
        self.assertFalse(incomplete["complete"])
        self.assertIsNone(incomplete["current_hp"])
        self.assertEqual(incomplete["observed_member_count"], 2)

    def test_team_health_does_not_label_idle_full_hp_as_encounter_low(self):
        model = CombatModel(run_id="team-health-idle-low")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_actor_health(
            actor_health(BASE_FILETIME + 10_000, SELF_ID, 25_000, 25_000)
        )

        summary = model.team_health_summary()
        self.assertTrue(summary["available"])
        self.assertEqual(summary["ratio"], 1.0)
        self.assertIsNone(summary["lowest"])

    def test_hps_banner_uses_same_current_boss_as_dps(self):
        class Canvas:
            def __init__(self):
                self.rectangles = []
                self.texts = []

            @staticmethod
            def delete(*_args):
                return None

            @staticmethod
            def winfo_width():
                return 620

            @staticmethod
            def winfo_height():
                return 36

            def create_rectangle(self, *args, **kwargs):
                self.rectangles.append((args, kwargs))

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

        window = object.__new__(DpsWindow)
        window.main_meter_mode = "hps"
        window.monster_hp_canvas = Canvas()
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
        window.model = type(
            "Model",
            (),
            {
                "team_health_summary": lambda _self: (_ for _ in ()).throw(
                    AssertionError("HPS banner must not replace Boss with team HP")
                ),
                "current_monster": lambda _self: monster,
            },
        )()
        window._fit_main_actor_name = lambda value, _width: value
        window._ui_font = lambda _role: None

        window._draw_monster_hp()

        self.assertEqual(len(window.monster_hp_canvas.rectangles), 2)
        self.assertEqual(len(window.monster_hp_canvas.texts), 1)
        text_args, text_options = window.monster_hp_canvas.texts[0]
        self.assertEqual(text_args[:2], (310, 18))
        self.assertEqual(
            text_options["text"],
            "Lv.60   测试 Boss   50 / 100   50.0%",
        )

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
        self.assertNotIn("average_ms", response)
        self.assertEqual(response["fastest_ms"], 1_000.0)
        self.assertEqual(response["slowest_ms"], 1_000.0)

    def test_each_healer_gets_own_first_response_to_same_health_drop(self):
        second_healer_id = NEARBY_ID
        model = CombatModel(run_id="healing-response-two-healers")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party(
            {"entity_ids": [TEAMMATE_ID, second_healer_id]}
        )
        for actor_id in (SELF_ID, second_healer_id):
            model.ingest_profile(
                {
                    "entity_id": actor_id,
                    "entity_type": "Player",
                    "profession_id": 1_200_002,
                }
            )
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest_actor_health(
            actor_health(BASE_FILETIME + 10_000_000, TEAMMATE_ID, 10_000, 10_000)
        )
        model.ingest_actor_health(
            actor_health(BASE_FILETIME + 20_000_000, TEAMMATE_ID, 6_000, 10_000)
        )
        model.ingest_heal(
            healing(
                BASE_FILETIME + 25_000_000,
                SELF_ID,
                TEAMMATE_ID,
                total=500,
                effective=500,
            )
        )
        model.ingest_heal(
            healing(
                BASE_FILETIME + 28_000_000,
                second_healer_id,
                TEAMMATE_ID,
                total=400,
                effective=400,
            )
        )

        by_actor = {
            row["actor_id"]: row
            for row in model.healing_summary(duration=10.0)["healers"]
        }
        self.assertEqual(by_actor[SELF_ID]["response"]["samples"], 1)
        self.assertEqual(
            by_actor[SELF_ID]["response"]["fastest_ms"], 500.0
        )
        self.assertEqual(
            by_actor[second_healer_id]["response"]["samples"], 1
        )
        self.assertEqual(
            by_actor[second_healer_id]["response"]["fastest_ms"], 800.0
        )

    def test_atomic_team_actor_rebind_swaps_only_token_derived_state(self):
        model = CombatModel(run_id="same-class-positive-swap")
        actor_a = 57_207_359_270_029
        actor_b = 57_270_710_244_165
        model.entity_names = {actor_a: "player-a", actor_b: "player-b"}
        model.team_damage_states = {
            actor_a: TeamDamageState(
                actor_a,
                last_absolute=5_116_636,
                has_snapshot=True,
                authoritative_snapshot=True,
                accepted_damage=5_116_636,
            ),
            actor_b: TeamDamageState(
                actor_b,
                last_absolute=3_564_784,
                has_snapshot=True,
                authoritative_snapshot=True,
                accepted_damage=3_564_784,
            ),
        }
        model.team_taken_states = {
            actor_a: TeamTakenState(
                actor_a,
                last_absolute=26_672,
                has_snapshot=True,
                accepted_taken=26_672,
                exact_for_encounter=True,
            ),
            actor_b: TeamTakenState(
                actor_b,
                last_absolute=23_307,
                has_snapshot=True,
                accepted_taken=23_307,
                exact_for_encounter=True,
            ),
        }
        model.team_healing_states = {
            actor_a: TeamHealingState(
                actor_a,
                last_absolute=222,
                has_snapshot=True,
                accepted_effective_healing=222,
            ),
            actor_b: TeamHealingState(
                actor_b,
                last_absolute=111,
                has_snapshot=True,
                accepted_effective_healing=111,
            ),
        }
        model.events = [
            damage(1, actor_a, MONSTER_ID, 17),
            damage(2, actor_b, MONSTER_ID, 29),
        ]
        physical_event_actors = [event["attacker_id"] for event in model.events]

        changed = model.rebind_team_actors(
            {
                "bindings": [
                    {
                        "from_actor_id": actor_b,
                        "to_actor_id": actor_a,
                        "user_token": "same-class-settlement-a",
                    },
                    {
                        "from_actor_id": actor_a,
                        "to_actor_id": actor_b,
                        "user_token": "same-class-settlement-b",
                    },
                ]
            }
        )

        self.assertTrue(changed)
        self.assertEqual(
            model.team_damage_states[actor_a].accepted_damage, 3_564_784
        )
        self.assertEqual(
            model.team_damage_states[actor_b].accepted_damage, 5_116_636
        )
        self.assertEqual(model.team_taken_states[actor_a].accepted_taken, 23_307)
        self.assertEqual(model.team_taken_states[actor_b].accepted_taken, 26_672)
        self.assertEqual(
            model.team_healing_states[actor_a].accepted_effective_healing, 111
        )
        self.assertEqual(
            model.team_healing_states[actor_b].accepted_effective_healing, 222
        )
        self.assertEqual(
            [event["attacker_id"] for event in model.events],
            physical_event_actors,
        )
        self.assertEqual(model.entity_names, {actor_a: "player-a", actor_b: "player-b"})

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
        self.assertEqual(healer["overheal_rate"], 0.29)
        self.assertTrue(healer["overheal_rate_partial"])
        self.assertIsNone(healer["peak_hps"])
        self.assertEqual(healer["observed_effective_healing"], 71)
        self.assertEqual(healer["skills"][0]["effective_healing"], 1_928)
        self.assertIsNone(healer["skills"][0]["total_healing"])

    def test_stage_profession_excludes_confirmed_non_healer_from_hps(self):
        model = CombatModel(run_id="healing-role-filter")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(
            model.ingest_heal(
                healing(
                    BASE_FILETIME + 20_000,
                    SELF_ID,
                    TEAMMATE_ID,
                    total=1_000,
                    effective=800,
                )
            )
        )
        self.assertTrue(
            model.ingest_heal(
                healing(
                    BASE_FILETIME + 21_000,
                    TEAMMATE_ID,
                    SELF_ID,
                    total=500,
                    effective=400,
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
                    "summary_id": "healing-role-settlement",
                    "filetime_100ns": death_time + 10_000,
                    "member_count": 2,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "profession_id": 1_200_002,
                            "damage": 100,
                            "effective_healing": 800,
                            "healing_skills": [
                                {
                                    "skill_id": 86_021_030,
                                    "effective_healing": 800,
                                }
                            ],
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "profession_id": 1_200_003,
                            "damage": 0,
                            "effective_healing": 400,
                            "healing_skills": [
                                {
                                    "skill_id": 86_021_030,
                                    "effective_healing": 400,
                                }
                            ],
                        },
                    ],
                }
            )
        )

        summary = model.healing_summary(duration=10.0)
        self.assertEqual(
            [row["actor_id"] for row in summary["healers"]], [SELF_ID]
        )
        self.assertNotIn(TEAMMATE_ID, model.stage_healing_snapshots)
        self.assertEqual(model.entity_professions[TEAMMATE_ID], 1_200_003)

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

    def test_realtime_detail_refreshes_only_in_timestamp_and_common_order(self):
        model = CombatModel(run_id="realtime-detail-refresh")
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
        self.assertFalse(model.ingest_team_stat(team_stat(1, TEAMMATE_ID, 0)))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 200)))

        summary_id = "realtime|RetDungeonBattleStatistics|5150060|2|live"

        def detail(
            sequence: int,
            actor_damage: int,
            skill_damage: int,
            *,
            critical_hits: int,
            penetration_hits: int,
            taken: int,
        ) -> dict:
            return {
                "summary_id": summary_id,
                "filetime_100ns": BASE_FILETIME + sequence * 10_000,
                "member_count": 2,
                "authoritative": False,
                "completion_confirmed": False,
                "realtime_detail": True,
                "actors": [
                    {
                        "actor_id": TEAMMATE_ID,
                        "damage": actor_damage,
                        "taken": taken,
                        "taken_present": True,
                        "damage_hits": 20,
                        "critical_hits": critical_hits,
                        "penetration_hits": penetration_hits,
                        "skills": [
                            {
                                "skill_id": 86_021_010,
                                "damage": skill_damage,
                                "hits": 12,
                            }
                        ],
                    }
                ],
            }

        first = detail(
            4,
            200,
            150,
            critical_hits=6,
            penetration_hits=14,
            taken=80,
        )
        self.assertTrue(model.ingest_stage_summary(first))
        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual(
            {skill_id: row.damage for skill_id, row in teammate.skills.items()},
            {86_021_010: 150, 0: 50},
        )
        self.assertEqual((teammate.damage_hits, teammate.critical_hits), (20, 6))
        self.assertEqual(teammate.penetration_hits, 14)
        self.assertEqual(model.stage_actor_taken[TEAMMATE_ID], 80)

        duplicate = detail(
            4,
            200,
            190,
            critical_hits=19,
            penetration_hits=19,
            taken=180,
        )
        older = detail(
            3,
            200,
            190,
            critical_hits=19,
            penetration_hits=19,
            taken=180,
        )
        self.assertFalse(model.ingest_stage_summary(duplicate))
        self.assertFalse(model.ingest_stage_summary(older))
        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual(teammate.skills[86_021_010].damage, 150)
        self.assertEqual(teammate.critical_hits, 6)
        self.assertEqual(teammate.penetration_hits, 14)
        self.assertEqual(model.stage_actor_taken[TEAMMATE_ID], 80)

        ahead_of_common = detail(
            5,
            260,
            230,
            critical_hits=10,
            penetration_hits=18,
            taken=120,
        )
        self.assertTrue(model.ingest_stage_summary(ahead_of_common))
        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual(teammate.damage, 200)
        self.assertEqual(teammate.skills[86_021_010].damage, 150)
        self.assertEqual(teammate.critical_hits, 6)
        self.assertEqual(teammate.penetration_hits, 14)
        self.assertEqual(model.stage_actor_taken[TEAMMATE_ID], 80)
        self.assertEqual(
            model.pending_stage_skill_snapshots[TEAMMATE_ID].actor_damage,
            260,
        )
        self.assertEqual(model.pending_stage_actor_taken[TEAMMATE_ID][0], 260)

        self.assertTrue(model.ingest_team_stat(team_stat(6, TEAMMATE_ID, 260)))
        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual(
            {skill_id: row.damage for skill_id, row in teammate.skills.items()},
            {86_021_010: 230, 0: 30},
        )
        self.assertEqual(model.stage_actor_taken[TEAMMATE_ID], 120)
        # Hit rates stay on the last total-matched snapshot until another
        # detail response confirms the new Common total.
        self.assertEqual(teammate.critical_hits, 6)
        self.assertEqual(teammate.penetration_hits, 14)

        matched_refresh = detail(
            7,
            260,
            235,
            critical_hits=10,
            penetration_hits=18,
            taken=125,
        )
        self.assertTrue(model.ingest_stage_summary(matched_refresh))
        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual(teammate.skills[86_021_010].damage, 235)
        self.assertEqual(teammate.skills[0].damage, 25)
        self.assertEqual((teammate.damage_hits, teammate.critical_hits), (20, 10))
        self.assertEqual(teammate.penetration_hits, 18)
        self.assertEqual(model.stage_actor_taken[TEAMMATE_ID], 125)

    def test_player_detail_query_never_overwrites_local_player_metrics(self):
        model = CombatModel(run_id="player-detail-keeps-local-metrics")
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

        self_hit = damage(1, SELF_ID, MONSTER_ID, 100)
        self_hit["critical"] = True
        self_hit["penetrating"] = False
        model.ingest(self_hit)
        self.assertFalse(model.ingest_team_stat(team_stat(2, TEAMMATE_ID, 0)))
        self.assertTrue(model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 200)))

        self.assertTrue(
            model.ingest_stage_summary(
                {
                    "summary_id": "player-detail-query|live",
                    "filetime_100ns": BASE_FILETIME + 4 * 10_000,
                    "member_count": 2,
                    "authoritative": False,
                    "completion_confirmed": False,
                    "realtime_detail": True,
                    "player_detail_query": True,
                    "actors": [
                        {
                            "actor_id": SELF_ID,
                            "damage": 100,
                            "damage_hits": 10,
                            "critical_hits": 9,
                            "penetration_hits": 9,
                            "skills": [
                                {
                                    "skill_id": 86_021_010,
                                    "damage": 100,
                                    "hits": 10,
                                }
                            ],
                        },
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 200,
                            "damage_hits": 20,
                            "critical_hits": 6,
                            "penetration_hits": 14,
                            "skills": [
                                {
                                    "skill_id": 86_021_020,
                                    "damage": 180,
                                    "hits": 12,
                                }
                            ],
                        },
                    ],
                }
            )
        )

        local_player = model.stats[SELF_ID]
        self.assertEqual((local_player.damage_hits, local_player.critical_hits), (1, 1))
        self.assertEqual(local_player.penetration_hits, 0)
        self.assertEqual(
            {skill_id: row.damage for skill_id, row in local_player.skills.items()},
            {86_021_070: 100},
        )

        teammate = model.stats[TEAMMATE_ID]
        self.assertEqual((teammate.damage_hits, teammate.critical_hits), (20, 6))
        self.assertEqual(teammate.penetration_hits, 14)
        self.assertEqual(
            {skill_id: row.damage for skill_id, row in teammate.skills.items()},
            {86_021_020: 180, 0: 20},
        )

    def test_stage_detail_survives_server_only_unclassified_remainder(self):
        model = CombatModel(run_id="stage-server-remainder-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        self.assertFalse(model.ingest_team_stat(team_stat(1, TEAMMATE_ID, 0)))
        model.ingest(damage(2, SELF_ID, MONSTER_ID, 100))
        self.assertTrue(
            model.ingest_team_stat(team_stat(3, TEAMMATE_ID, 136_492))
        )
        death_time = BASE_FILETIME + 4 * 10_000
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
                    "summary_id": "stage-server-remainder",
                    "filetime_100ns": death_time + 10_000,
                    "member_count": 2,
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": TEAMMATE_ID,
                            "damage": 136_492,
                            "detail_reported_damage": 137_159,
                            "damage_hits": 100,
                            "critical_hits": 20,
                            "penetration_hits": 30,
                            "skills": [
                                {
                                    "skill_id": 86_021_030,
                                    "damage": 100_000,
                                    "hits": 40,
                                },
                                {
                                    "skill_id": 82_030_003,
                                    "damage": 36_492,
                                    "hits": 12,
                                },
                            ],
                        }
                    ],
                }
            )
        )

        teammate = model.stats[TEAMMATE_ID]
        self.assertIsNotNone(model.active_stage_skill_snapshot(TEAMMATE_ID))
        self.assertEqual(teammate.damage, 136_492)
        self.assertEqual(
            {skill_id: row.damage for skill_id, row in teammate.skills.items()},
            {86_021_030: 100_000, 82_030_003: 36_492},
        )
        self.assertEqual((teammate.damage_hits, teammate.critical_hits), (100, 20))
        self.assertEqual(teammate.penetration_hits, 30)
        record = model.build_combat_record("target_defeated")
        actor = next(
            row for row in record["participants"] if row["actor_id"] == TEAMMATE_ID
        )
        self.assertEqual(
            actor["skill_reconciliation_mode"],
            "classified_skills_match_common_total",
        )
        self.assertEqual(actor["server_reported_damage"], 137_159)
        self.assertEqual(actor["server_reported_unclassified_damage"], 667)
        self.assertEqual(actor["server_damage_difference"], 667)

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

    def test_token_proven_actor_merge_clears_previous_occupant_rating(self):
        model = CombatModel()
        provisional_actor = -781_628_999_061_186_694
        real_actor = TEAMMATE_ID + 120
        model.ingest_profile(
            {
                "entity_id": provisional_actor,
                "name": "incoming-player",
                "profession_id": 1_200_006,
                "entity_type": "Player",
            }
        )
        model.ingest_profile(
            {
                "entity_id": real_actor,
                "name": "previous-player",
                "profession_id": 1_200_003,
                "extraordinary_rating": 88_888,
                "entity_type": "Player",
            }
        )

        self.assertTrue(
            model.merge_actor(
                {
                    "from_actor_id": provisional_actor,
                    "to_actor_id": real_actor,
                    "user_token": "exact-team-member-token",
                    "replace_profile": True,
                }
            )
        )

        self.assertEqual(model.display_name(real_actor), "incoming-player")
        self.assertEqual(model.actor_profession_id(real_actor), 1_200_006)
        self.assertNotIn(real_actor, model.entity_extraordinary_ratings)

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

    def test_realtime_hit_rates_use_explicit_hit_type_without_settlement(self):
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
        for sequence, (critical, penetrating) in enumerate(
            ((False, False), (True, True), (True, True)), start=1
        ):
            event = damage(sequence, SELF_ID, MONSTER_ID, 1_000)
            event["critical"] = critical
            event["penetrating"] = penetrating
            model.ingest(event)

        actor = model.stats[SELF_ID]
        self.assertEqual(actor.damage_hits, 3)
        self.assertEqual(actor.critical_hits, 2)
        self.assertEqual(actor.penetration_hits, 2)
        record = model.build_combat_record("test")
        participant = record["participants"][0]
        self.assertEqual(participant["critical_rate"], 2 / 3)
        self.assertEqual(participant["penetration_rate"], 2 / 3)

    def test_observed_dummy_penetration_rate_rounds_to_game_display(self):
        actor = ActorStats(
            SELF_ID,
            damage_hits=344,
            critical_hits=175,
            penetration_hits=337,
        )
        self.assertEqual(DpsWindow._actor_penetration_text(actor), "98.0%")

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

    def test_team_damage_samples_are_saved_with_the_settlement_total(self):
        model = CombatModel(run_id="history-team-samples-test")
        model.ingest_identity({"entity_id": SELF_ID})
        model.ingest_party({"entity_ids": [TEAMMATE_ID]})
        model.ingest_profile(
            {"entity_id": MONSTER_ID, "entity_type": "Boss", "boss_rank": 3}
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 300_000))
        self.assertTrue(model.sample_team_damage(model.first_damage_time))
        model.ingest(damage(1001, TEAMMATE_ID, MONSTER_ID, 500_000))
        self.assertTrue(
            model.sample_team_damage(model.first_damage_time + 1.0)
        )

        record = model.build_combat_record("target_defeated")

        self.assertIsNotNone(record)
        self.assertEqual(
            record["team_damage_samples"]["coverage"],
            "live_team_cumulative",
        )
        self.assertEqual(
            record["team_damage_samples"]["rows"][-1], [1, 800_000]
        )
        self.assertEqual(
            record["participant_damage_samples"]["coverage"],
            "live_participant_cumulative",
        )
        self.assertEqual(
            record["participant_damage_samples"]["rows"],
            [
                [0, SELF_ID, 300_000],
                [1, SELF_ID, 300_000],
                [1, TEAMMATE_ID, 500_000],
            ],
        )
        self.assertEqual(
            record["team_dps_timeline"][-1]["source"],
            "live_team_cumulative",
        )

    def test_history_team_trend_ignores_incomplete_cached_timeline(self):
        record = {
            "encounter_id": "old-incomplete-timeline",
            "duration_seconds": 20,
            "dps_duration_seconds": 20,
            "total_damage": 3_000,
            "team_dps": 150,
            "event_log": {
                "rows": [[0, SELF_ID, MONSTER_ID, 1, 100, None, None]],
            },
            "team_dps_timeline": [
                {"time_seconds": 0, "team_dps": 100},
            ],
        }
        window = DpsWindow.__new__(DpsWindow)
        window.history_selected_id = record["encounter_id"]
        window.history_trend_record_id = ""
        window.history_trend_points = []
        window.history_trend_source = "settlement_average"
        window._selected_history_record = lambda: record

        points = window._history_snapshot_team_dps_points()

        self.assertEqual(points, [(0.0, 150.0), (20.0, 150.0)])
        self.assertEqual(window.history_trend_source, "settlement_average")

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

    def test_extraordinary_rating_flows_from_profile_into_history_summary(self):
        model = CombatModel(run_id="extraordinary-rating-history")
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
                "entity_id": SELF_ID,
                "name": "本人",
                "entity_type": "Player",
                "profession_id": 1_200_002,
                "extraordinary_rating": 63_268,
            }
        )
        model.ingest_profile(
            {
                "entity_id": TEAMMATE_ID,
                "name": "队友",
                "entity_type": "Player",
                "profession_id": 1_200_003,
                "extraordinary_rating": 54_817,
            }
        )
        model.ingest_profile(
            {
                "entity_id": MONSTER_ID,
                "name": "测试首领",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 2_000))
        model.ingest(damage(10_001, TEAMMATE_ID, MONSTER_ID, 1_000))

        record = model.build_combat_record("target_defeated")

        self.assertIsNotNone(record)
        participants = {
            row["actor_id"]: row for row in record["participants"]
        }
        self.assertEqual(
            participants[SELF_ID]["extraordinary_rating"], 63_268
        )
        self.assertEqual(
            participants[TEAMMATE_ID]["extraordinary_rating"], 54_817
        )
        summary = build_history_summary(record)
        self.assertEqual(summary["extraordinary_rating"], 63_268)

    def test_team_average_extraordinary_rating_ignores_missing_values(self):
        self.assertEqual(
            team_average_extraordinary_rating(
                [
                    {"extraordinary_rating": 63_268},
                    {"extraordinary_rating": 54_817},
                    {},
                    {"extraordinary_rating": None},
                    {
                        "name": "莫雪·投影",
                        "extraordinary_rating": 99_999,
                    },
                ]
            ),
            (59_043, 2, 4),
        )
        average, rated_count, team_size = team_average_extraordinary_rating(
            [{}, {"extraordinary_rating": None}]
        )
        self.assertIsNone(average)
        self.assertEqual((rated_count, team_size), (0, 2))
        self.assertEqual(format_extraordinary_rating(average), "--")
        self.assertEqual(
            team_average_extraordinary_rating(
                [
                    {"name": "投影甲·投影", "extraordinary_rating": 80_000},
                    {
                        "name": "兼容旧记录",
                        "is_ai": True,
                        "extraordinary_rating": 90_000,
                    },
                ]
            ),
            (None, 0, 0),
        )

    def test_history_detail_all_tabs_show_team_average_rating(self):
        class Label:
            def __init__(self):
                self.options = {}

            def configure(self, **options):
                self.options.update(options)

        record = {
            "participants": [
                {"extraordinary_rating": 63_268},
                {"extraordinary_rating": 54_817},
                {},
                {
                    "name": "队员·投影",
                    "extraordinary_rating": 99_999,
                },
            ]
        }
        window = object.__new__(DpsWindow)
        window.history_page_mode = "detail"
        window.history_total_value = None
        window.history_dps_value = None
        window.history_team_value = None
        window.history_duration_value = None
        window.history_my_value = Label()
        window.history_my_value._caption_label = Label()
        window.history_my_value._subtitle_label = Label()

        for mode in ("dps", "hps", "dt", "boss_damage"):
            with self.subTest(mode=mode):
                window._sync_history_detail_metric_copy(
                    healing_mode=mode == "hps",
                    taken_mode=mode == "dt",
                    boss_mode=mode == "boss_damage",
                )
                window._update_history_team_average_rating(record)
                self.assertEqual(
                    window.history_my_value._caption_label.options["text"],
                    "队伍平均非凡评分",
                )
                self.assertEqual(
                    window.history_my_value.options["text"], "59,043"
                )
                self.assertEqual(
                    window.history_my_value._subtitle_label.options["text"],
                    "真人评分 2 / 3 人",
                )

        window._update_history_team_average_rating(
            {"participants": [{}, {"extraordinary_rating": None}]}
        )
        self.assertEqual(window.history_my_value.options["text"], "--")
        self.assertEqual(
            window.history_my_value._subtitle_label.options["text"],
            "真人评分 0 / 2 人",
        )

    def test_history_detail_labels_projection_rating_as_ai(self):
        class Label:
            def __init__(self):
                self.options = {}

            def configure(self, **options):
                self.options.update(options)

        window = object.__new__(DpsWindow)
        window.professions = {}
        window.history_player_name_label = None
        window.history_player_rank_label = None
        window.history_player_rating_label = Label()
        window.history_player_icon_label = None
        window.history_player_primary_label = None
        window.history_player_primary_caption = None
        window.history_player_stat_labels = {}
        window.history_player_share_canvas = None
        window.history_detail_label = None
        projection = {
            "actor_id": TEAMMATE_ID,
            "name": "莫雪·投影",
            "extraordinary_rating": 77_897,
            "damage": 100,
            "dps": 100,
            "share": 1.0,
        }

        window._render_history_player_summary(
            {"duration_seconds": 1.0, "total_damage": 100},
            [projection],
            projection,
            healing_mode=False,
            taken_mode=False,
        )

        self.assertEqual(
            window.history_player_rating_label.options["text"],
            "非凡评分 人机",
        )

    def test_pending_teammate_detail_does_not_invent_unclassified_skill(self):
        pending = {
            "damage": 1_368_000,
            "skill_detail_status": "pending_server_detail",
            "skills": [
                {
                    "skill_id": 0,
                    "name": "未归类伤害",
                    "damage": 1_368_000,
                    "share": 1.0,
                    "aggregate": True,
                }
            ],
        }

        self.assertEqual(DpsWindow._history_damage_skills(pending), [])

        complete = dict(pending)
        complete["skill_detail_status"] = "complete"
        self.assertEqual(
            DpsWindow._history_damage_skills(complete)[0]["name"],
            "未归类伤害",
        )

    def test_pending_teammate_skill_share_shows_missing_detail_notice(self):
        class Canvas:
            def __init__(self):
                self.texts = []

            @staticmethod
            def winfo_width():
                return 580

            @staticmethod
            def winfo_height():
                return 240

            @staticmethod
            def delete(*_args, **_kwargs):
                return None

            @staticmethod
            def create_rectangle(*_args, **_kwargs):
                return None

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

        class Label:
            def __init__(self):
                self.options = {}

            def configure(self, **options):
                self.options.update(options)

        participant = {
            "actor_id": TEAMMATE_ID,
            "damage": 1_368_000,
            "skill_detail_status": "pending_server_detail",
            "skills": [
                {
                    "skill_id": 0,
                    "name": "未归类伤害",
                    "damage": 1_368_000,
                    "share": 1.0,
                    "aggregate": True,
                }
            ],
        }
        window = object.__new__(DpsWindow)
        window.history_meter_mode = "dps"
        window.history_skill_share_canvas = Canvas()
        window.history_skill_share_caption_label = Label()
        window._selected_history_damage_participant = lambda: ({}, participant)
        window._ui_font = lambda _kind: None

        window._draw_history_skill_share()

        rendered = [
            options.get("text")
            for _args, options in window.history_skill_share_canvas.texts
        ]
        self.assertEqual(window.history_skill_share_segments, [])
        self.assertIn("暂无技能明细", rendered)
        self.assertIn(
            "本场服务器未返回该队友的技能明细；总伤害数据不受影响。",
            rendered,
        )
        self.assertEqual(
            window.history_skill_share_caption_label.options["text"],
            "服务器未返回技能明细",
        )

    def test_history_player_header_shows_rating_and_team_detail_notice(self):
        class Label:
            def __init__(self):
                self.options = {}
                self.image = None

            def configure(self, **options):
                self.options.update(options)

        class Icons:
            @staticmethod
            def profession(_class_id, _size):
                return object()

        participant = {
            "actor_id": TEAMMATE_ID,
            "name": "队友甲",
            "profession_id": 1_200_002,
            "extraordinary_rating": 67_893,
            "damage": 1_368_000,
            "dps": 15_373,
            "share": 0.147,
            "skill_detail_status": "pending_server_detail",
        }
        window = object.__new__(DpsWindow)
        window.history_player_name_label = Label()
        window.history_player_rank_label = Label()
        window.history_player_rating_label = Label()
        window.history_player_icon_label = Label()
        window.history_player_primary_label = Label()
        window.history_player_primary_caption = Label()
        window.history_player_stat_labels = {}
        window.history_player_share_canvas = None
        window.history_detail_label = Label()
        window.hide_names = False
        window.professions = {}
        window.icons = Icons()
        window._draw_history_player_share = lambda: None
        window._draw_history_skill_share = lambda: None
        window._draw_history_skill_quality = lambda: None
        window._draw_history_critical_luck = lambda: None
        window._draw_history_skill_timeline = lambda: None

        window._render_history_player_summary(
            {"duration_seconds": 89, "total_damage": 9_277_000},
            [participant],
            participant,
            healing_mode=False,
            taken_mode=False,
        )

        self.assertEqual(
            window.history_player_rating_label.options["text"],
            "非凡评分 67,893",
        )
        self.assertEqual(
            window.history_detail_label.options["text"],
            "本场服务器未返回该队友的技能明细；总伤害数据不受影响。",
        )

        window.history_hide_names = True
        window._render_history_player_summary(
            {"duration_seconds": 89, "total_damage": 9_277_000},
            [participant],
            participant,
            healing_mode=False,
            taken_mode=False,
        )
        self.assertEqual(
            window.history_player_name_label.options["text"], "玩家1"
        )

    def test_history_local_detail_modules_follow_selected_player(self):
        class Panel:
            def __init__(self):
                self.manager = "pack"
                self.pack_calls = []
                self.forget_calls = 0

            def winfo_manager(self):
                return self.manager

            def pack(self, **options):
                self.manager = "pack"
                self.pack_calls.append(options)

            def pack_forget(self):
                self.manager = ""
                self.forget_calls += 1

        record = {
            "participants": [
                {"actor_id": SELF_ID, "is_self": True},
                {"actor_id": TEAMMATE_ID, "is_self": False},
            ]
        }
        window = object.__new__(DpsWindow)
        window.history_selected_actor = TEAMMATE_ID
        window.history_page_mode = "detail"
        window.history_local_detail_modules_visible = True
        window.history_skill_quality_panel = Panel()
        window.history_critical_luck_panel = Panel()
        window.history_opener_panel = Panel()
        window.history_skill_timeline_panel = Panel()
        window._selected_history_record = lambda: record
        window._refresh_history_page_extent = mock.Mock()

        self.assertFalse(window._sync_history_local_detail_modules())
        self.assertEqual(
            window._history_detail_minimum_content_height(),
            HISTORY_DETAIL_MIN_CONTENT_HEIGHT
            - HISTORY_DETAIL_LOCAL_MODULES_HEIGHT,
        )
        for panel in (
            window.history_skill_quality_panel,
            window.history_critical_luck_panel,
            window.history_opener_panel,
            window.history_skill_timeline_panel,
        ):
            self.assertEqual(panel.manager, "")
            self.assertEqual(panel.forget_calls, 1)
        window._refresh_history_page_extent.assert_called_once_with()

        window.history_selected_actor = SELF_ID
        self.assertTrue(window._sync_history_local_detail_modules())
        self.assertEqual(
            window._history_detail_minimum_content_height(),
            HISTORY_DETAIL_MIN_CONTENT_HEIGHT,
        )
        self.assertEqual(
            window.history_skill_quality_panel.pack_calls,
            [{"fill": "x", "padx": 18, "pady": (8, 0)}],
        )
        self.assertEqual(
            window.history_critical_luck_panel.pack_calls,
            [{"fill": "x", "padx": 18, "pady": (8, 0)}],
        )
        self.assertEqual(
            window.history_opener_panel.pack_calls,
            [{"fill": "x", "padx": 18, "pady": (8, 0)}],
        )
        self.assertEqual(
            window.history_skill_timeline_panel.pack_calls,
            [{"fill": "x", "padx": 18, "pady": (8, 14)}],
        )
        self.assertEqual(window._refresh_history_page_extent.call_count, 2)

    def test_history_self_actor_detection_checks_each_player_collection(self):
        window = object.__new__(DpsWindow)
        window.history_selected_actor = SELF_ID

        for collection_name in ("participants", "healers", "damage_taken"):
            with self.subTest(collection_name=collection_name):
                record = {
                    collection_name: [
                        {"actor_id": SELF_ID, "is_self": True}
                    ]
                }
                window._selected_history_record = lambda record=record: record
                self.assertTrue(window._history_selected_actor_is_self())

        window._selected_history_record = lambda: {
            "participants": [{"actor_id": SELF_ID, "is_self": False}]
        }
        self.assertFalse(window._history_selected_actor_is_self())

    def test_history_privacy_toggle_is_independent_from_main_window(self):
        class Button:
            def __init__(self):
                self.options = {}

            def configure(self, **options):
                self.options.update(options)

        window = object.__new__(DpsWindow)
        window.hide_names = False
        window.history_hide_names = False
        window.history_privacy_buttons = [Button(), Button()]
        window.history_page_mode = "browser"
        window._draw_history_list = mock.Mock()
        window._render_history_snapshot = mock.Mock()
        window._draw_history_snapshot_trend = mock.Mock()

        window._toggle_history_names()

        self.assertTrue(window.history_hide_names)
        self.assertFalse(window.hide_names)
        self.assertEqual(
            [button.options["text"] for button in window.history_privacy_buttons],
            ["显示名称", "显示名称"],
        )
        window._draw_history_list.assert_called_once_with()
        window._render_history_snapshot.assert_called_once_with()
        window._draw_history_snapshot_trend.assert_called_once_with()

    def test_history_privacy_controls_match_browser_and_detail_layouts(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        browser_source = source[
            source.index("    def _build_history_browser_view(") : source.index(
                "    def _build_history_list_panel("
            )
        ]
        field_source = source[
            source.index("    def _build_history_list_panel(") : source.index(
                "    def _history_field_visible("
            )
        ]
        detail_source = source[
            source.index("    def _build_history_detail_view(") : source.index(
                "    def _history_detail_metric_card("
            )
        ]

        self.assertNotIn(
            'actions, "隐藏名称", self._toggle_history_names', browser_source
        )
        self.assertNotIn(
            'actions, "隐藏名称", self._toggle_history_names', detail_source
        )
        privacy_index = field_source.index('            "隐藏名称",')
        character_index = field_source.index('            ("character", "角色名称"),')
        self.assertLess(privacy_index, character_index)
        self.assertIn("command=self._history_privacy_field_changed", field_source)
        team_heading_index = detail_source.index(
            "self.history_team_heading_label.pack"
        )
        detail_privacy_index = detail_source.index(
            "self.history_detail_privacy_control = ModernCheckboxControl("
        )
        team_subtitle_index = detail_source.index(
            "self.history_team_subtitle_label = tk.Label("
        )
        self.assertLess(team_heading_index, detail_privacy_index)
        self.assertLess(detail_privacy_index, team_subtitle_index)
        self.assertIn('            "隐藏名称",', detail_source)
        self.assertIn("command=self._history_privacy_field_changed", detail_source)

    def test_history_detail_privacy_control_is_hidden_only_for_boss_tab(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )

        class Control:
            def __init__(self):
                self.manager = "pack"
                self.pack_options = None

            def winfo_manager(self):
                return self.manager

            def pack_forget(self):
                self.manager = ""

            def pack(self, **options):
                self.manager = "pack"
                self.pack_options = options

        window = object.__new__(DpsWindow)
        window.history_detail_privacy_control = Control()
        window.history_meter_mode = "boss_damage"

        window._sync_history_detail_privacy_control()
        self.assertEqual(window.history_detail_privacy_control.manager, "")

        for mode in ("dps", "hps", "dt"):
            window.history_meter_mode = mode
            window._sync_history_detail_privacy_control()
            self.assertEqual(window.history_detail_privacy_control.manager, "pack")
            self.assertEqual(
                window.history_detail_privacy_control.pack_options,
                {"side": "left", "padx": (0, 8), "pady": 17},
            )
            window.history_detail_privacy_control.pack_forget()

        sync_source = source[
            source.index("    def _sync_history_meter_tabs(") : source.index(
                "    def _set_history_meter_mode("
            )
        ]
        self.assertIn("self._sync_history_detail_privacy_control()", sync_source)

    def test_boss_history_tab_is_enabled_while_future_items_stay_disabled(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '("boss_damage", "Boss 伤害", True)', source
        )
        self.assertIn(
            '("teammate_gear", "队友装备快照", False)', source
        )
        self.assertIn(
            '("upload", "peak", "巅峰记录", False)', source
        )
        self.assertIn(
            '("statistics", "analytics", "数据统计", False)', source
        )
        self.assertIn(
            '("history", "history", "战斗记录", True)', source
        )
        window = object.__new__(DpsWindow)
        window.history_meter_mode = "dps"
        window.history_selected_actor = SELF_ID
        window.history_skill_timeline_selected_hit = object()
        window.history_skill_timeline_hover_hit = object()
        window._sync_history_meter_tabs = mock.Mock()
        window._update_history_overview = mock.Mock()
        window._render_history_selection = mock.Mock()
        window._draw_history_list_header = mock.Mock()
        window._render_history_snapshot = mock.Mock()

        window._set_history_meter_mode("boss_damage")
        window._set_history_meter_mode("teammate_gear")

        self.assertEqual(window.history_meter_mode, "boss_damage")
        self.assertEqual(window.history_selected_actor, 0)
        window._sync_history_meter_tabs.assert_called_once_with()
        window._render_history_selection.assert_called_once_with()

    def test_main_boss_tab_switches_to_independent_boss_meter(self):
        source = Path(__file__).with_name("dps_meter.pyw").read_text(
            encoding="utf-8"
        )
        self.assertIn('("boss", "BOSS首领", True)', source)

        window = object.__new__(DpsWindow)
        window.main_meter_mode = "dps"
        window.skill_panel_mode = "damage"
        window.main_scroll_offset = 99
        window.main_scroll_content_height = 100
        window.config = {}
        window.root = mock.Mock()
        window.compact_mode = False
        window._sync_main_meter_tabs = mock.Mock()
        window._main_minimum_width = mock.Mock(return_value=700)
        window._sync_compact_geometry_width = mock.Mock()
        window._draw_main_header = mock.Mock()
        window._draw_main_rows = mock.Mock()
        window._render_skill_details = mock.Mock()
        window._main_content_overlay_requested = mock.Mock(return_value=False)
        window._hide_main_content_overlay = mock.Mock()

        with mock.patch.dict(
            DpsWindow._set_main_meter_mode.__globals__,
            {"save_config": mock.Mock()},
        ):
            window._set_main_meter_mode("boss")

        self.assertEqual(window.main_meter_mode, "boss")
        self.assertEqual(window.config["main_meter_mode"], "boss")
        self.assertEqual(window.main_scroll_offset, 0)
        window._draw_main_rows.assert_called_once_with()

    def test_history_boss_snapshot_uses_boss_metrics_and_empty_states(self):
        class Label:
            def __init__(self):
                self.values = {}

            def configure(self, **values):
                self.values.update(values)

        value_keys = (
            "team_dps",
            "my_dps",
            "my_damage",
            "my_share",
            "team_size",
            "duration",
        )
        caption_keys = ("team_dps", "my_dps", "my_damage", "my_share")
        labels = {key: Label() for key in value_keys}
        labels.update({f"{key}_caption": Label() for key in caption_keys})
        summary = {
            "battle_id": "boss-summary",
            "team_size": 6,
            "duration_seconds": 45,
            "result": "defeated",
            "boss_team_taken": 1000,
            "boss_observed_damage": 750,
            "boss_classification_ratio": 0.75,
            "boss_max_hit": 400,
            "boss_damage_coverage": "observed_partial",
        }
        window = object.__new__(DpsWindow)
        window.history_meter_mode = "boss_damage"
        window.history_snapshot_labels = labels
        window.history_snapshot_trend_title_label = Label()
        window.history_snapshot_status_label = Label()
        window._selected_history_summary = lambda: summary
        window._render_history_snapshot_identity = mock.Mock()
        window._draw_history_snapshot_result = mock.Mock()
        window._draw_history_snapshot_trend = mock.Mock()

        window._render_history_snapshot()

        self.assertEqual(labels["team_dps"].values["text"], "1,000")
        self.assertEqual(labels["my_dps"].values["text"], "750")
        self.assertEqual(labels["my_damage"].values["text"], "75.0%")
        self.assertEqual(labels["my_share"].values["text"], "400")
        self.assertEqual(
            labels["team_dps_caption"].values["text"], "团队总承伤"
        )
        self.assertEqual(
            window.history_snapshot_status_label.values["text"], "已观测部分"
        )

        summary.clear()
        summary.update(
            {
                "battle_id": "old-history",
                "team_size": 6,
                "duration_seconds": 45,
                "result": "defeated",
            }
        )
        window._render_history_snapshot()
        self.assertEqual(labels["my_dps"].values["text"], "--")
        self.assertEqual(
            window.history_snapshot_status_label.values["text"],
            "该场未记录首领伤害",
        )
        self.assertEqual(
            DpsWindow._history_boss_coverage_text(
                {
                    "coverage": "unavailable",
                    "unavailable_reason": "training_dummy",
                }
            )[0],
            "训练目标不会主动造成伤害",
        )

    def test_history_boss_report_rows_use_sources_instead_of_players(self):
        record = {
            "participants": [
                {"actor_id": SELF_ID, "name": "本人", "damage": 999_999}
            ],
            "boss_damage": {
                "observed_damage": 1000,
                "sources": [
                    {
                        "entity_id": MONSTER_ID,
                        "template_id": 7_102_403,
                        "name": "星象仪者",
                        "kind": "boss",
                        "damage": 800,
                        "hits": 4,
                        "share": 0.8,
                    },
                    {
                        "entity_id": SECOND_MONSTER_ID,
                        "name": "星光守卫",
                        "kind": "mechanism",
                        "damage": 200,
                        "hits": 2,
                        "share": 0.2,
                    },
                ],
            },
        }
        window = object.__new__(DpsWindow)

        rows = window._history_team_report_rows(record, "boss_damage")

        self.assertEqual([row["name"] for row in rows], ["星象仪者", "星光守卫"])
        self.assertEqual([row["amount"] for row in rows], [800, 200])
        self.assertEqual([row["rate"] for row in rows], [4, 2])
        self.assertNotIn("本人", {str(row["name"]) for row in rows})

    def test_history_boss_report_handles_malformed_ratio_and_renders_image(self):
        record = {
            "duration_seconds": 12,
            "team_size": 6,
            "dungeon_name": "五月庄园·花园",
            "stage_name": "星象仪者",
            "boss_name": "星象仪者",
            "result": "defeated",
            "started_at_epoch": 1_700_000_000,
            "boss_damage": {
                "coverage": "observed_partial",
                "observed_damage": 1_000,
                "team_taken": 1_500,
                "classification_ratio": "旧记录坏值",
                "sources": [
                    {
                        "entity_id": MONSTER_ID,
                        "template_id": 7_102_403,
                        "name": "星象仪者",
                        "kind": "boss",
                        "damage": 1_000,
                        "hits": 3,
                        "share": 1.0,
                    }
                ],
            },
        }
        window = object.__new__(DpsWindow)
        window.professions = {}
        window._history_boss_icon_name = lambda _source: ""

        report = window._render_history_team_report_image(
            record, "boss_damage", hide_names=False
        )

        self.assertEqual(report.mode, "RGB")
        self.assertEqual(report.width, 1240)
        self.assertGreater(report.height, 400)
        for malformed in ("旧记录坏值", float("nan"), float("inf")):
            self.assertEqual(DpsWindow._history_rate_text(malformed), "--")

    def test_history_boss_timeline_ignores_malformed_source_ids(self):
        record = {
            "duration_seconds": 10,
            "boss_damage": {
                "sources": [
                    {
                        "entity_id": MONSTER_ID,
                        "entity_ids": ["invalid", SECOND_MONSTER_ID],
                        "name": "星象仪者",
                        "skills": [
                            {
                                "skill_id": 88_008_120,
                                "name": "知识禁制",
                                "damage": 300,
                            }
                        ],
                        "targets": [
                            {"actor_id": "invalid", "name": "坏数据"},
                            {"actor_id": SELF_ID, "name": "本人"},
                        ],
                    }
                ],
                "event_log": {
                    "columns": [
                        "time_ms",
                        "source_id",
                        "target_id",
                        "skill_id",
                        "damage",
                    ],
                    "rows": [
                        [1000, SECOND_MONSTER_ID, SELF_ID, 88_008_120, 300]
                    ],
                },
            },
        }
        window = object.__new__(DpsWindow)
        window.history_meter_mode = "boss_damage"
        window.history_selected_actor = MONSTER_ID
        window.history_hide_names = False
        window._selected_history_record = lambda: record

        _record, participant, skills, events, duration = (
            window._history_skill_timeline_payload()
        )

        self.assertEqual(participant["actor_id"], MONSTER_ID)
        self.assertEqual(skills[0]["name"], "知识禁制")
        self.assertEqual(events[88_008_120][0]["damage"], 300)
        self.assertEqual(events[88_008_120][0]["target_name"], "本人")
        self.assertEqual(duration, 10)

    def test_history_hps_skill_share_uses_existing_effective_healing(self):
        class Canvas:
            def __init__(self):
                self.arcs = []
                self.lines = []
                self.texts = []
                self.rectangles = []
                self.images = []

            @staticmethod
            def winfo_width():
                return 580

            @staticmethod
            def winfo_height():
                return 240

            @staticmethod
            def delete(*_args, **_kwargs):
                return None

            def create_arc(self, *args, **kwargs):
                self.arcs.append((args, kwargs))

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

            def create_line(self, *args, **kwargs):
                self.lines.append((args, kwargs))

            @staticmethod
            def create_oval(*_args, **_kwargs):
                return None

            def create_rectangle(self, *args, **kwargs):
                self.rectangles.append((args, kwargs))

            def create_image(self, *args, **kwargs):
                self.images.append((args, kwargs))

        class Icons:
            @staticmethod
            def skill(_skill_id, _name, _class_id, _size):
                return object()

        record = {
            "healers": [
                {
                    "actor_id": SELF_ID,
                    "name": "治疗者",
                    "effective_healing": 400,
                    "skills": [
                        {"name": "治疗甲", "effective_healing": 300},
                        {"name": "治疗乙", "effective_healing": 100},
                    ],
                }
            ]
        }
        window = object.__new__(DpsWindow)
        window.history_meter_mode = "hps"
        window.history_selected_actor = SELF_ID
        window.history_skill_share_canvas = Canvas()
        window.icons = Icons()
        window._selected_history_damage_participant = lambda: (record, None)
        window._selected_history_record = lambda: record
        window._ui_font = lambda _kind: None
        window._fit_ui_text = lambda value, _width, _kind: value

        window._draw_history_skill_share()

        rendered = [options.get("text") for _args, options in window.history_skill_share_canvas.texts]
        self.assertEqual(len(window.history_skill_share_canvas.arcs), 2)
        self.assertEqual(len(window.history_skill_share_canvas.images), 2)
        self.assertGreaterEqual(len(window.history_skill_share_canvas.rectangles), 6)
        self.assertIn("400", rendered)
        self.assertIn("个人有效治疗", rendered)
        self.assertIn("75.0%", " ".join(str(value) for value in rendered))
        self.assertIn("25.0%", " ".join(str(value) for value in rendered))

    def test_history_skill_share_hover_links_ring_row_and_center(self):
        class Canvas:
            def __init__(self):
                self.items = {}
                self.options = {}
                self.raised = []

            def itemconfigure(self, item, **options):
                self.items.setdefault(item, {}).update(options)

            def tag_raise(self, tag):
                self.raised.append(tag)

            def configure(self, **options):
                self.options.update(options)

        window = object.__new__(DpsWindow)
        window.history_skill_share_canvas = Canvas()
        window.history_skill_share_segments = [
            {
                "name": "技能甲",
                "amount": 750,
                "share": 0.75,
                "color": "#a879e7",
            },
            {
                "name": "技能乙",
                "amount": 250,
                "share": 0.25,
                "color": "#5e91ed",
            },
        ]
        window.history_skill_share_render = {
            "items": [
                {
                    "arc": "arc-0",
                    "marker": "marker-0",
                    "row_background": "row-0",
                    "color": "#a879e7",
                },
                {
                    "arc": "arc-1",
                    "marker": "marker-1",
                    "row_background": "row-1",
                    "color": "#5e91ed",
                },
            ],
            "center": {
                "kicker": "center-kicker",
                "value": "center-value",
                "caption": "center-caption",
                "detail": "center-detail",
            },
            "ring_width": 42.0,
            "stage_color": "#101419",
            "center_caption": "个人总伤害",
            "detail_caption": "占个人总伤害",
            "total": 1_000,
        }

        window._history_skill_share_enter(None, 1)

        canvas = window.history_skill_share_canvas
        self.assertEqual(canvas.items["arc-1"]["width"], 46.0)
        self.assertEqual(canvas.items["row-1"]["fill"], "#182027")
        self.assertEqual(canvas.items["center-kicker"]["text"], "技能乙")
        self.assertEqual(canvas.items["center-value"]["text"], "25.0%")
        self.assertEqual(canvas.items["center-caption"]["text"], "250")
        self.assertEqual(canvas.options["cursor"], "hand2")
        self.assertEqual(canvas.raised, ["history-share-segment:1"])

        window._history_skill_share_leave()

        self.assertEqual(canvas.items["arc-1"]["width"], 42.0)
        self.assertEqual(canvas.items["row-1"]["fill"], "#101419")
        self.assertEqual(canvas.items["center-kicker"]["text"], "个人总伤害")
        self.assertEqual(canvas.items["center-value"]["text"], "1,000")
        self.assertEqual(canvas.options["cursor"], "")

    def test_history_inner_scroll_hands_off_to_page_at_its_boundary(self):
        class Canvas:
            def __init__(self, view, *, moves=True):
                self.view = view
                self.moves = moves
                self.calls = []

            def yview(self):
                return self.view

            def yview_scroll(self, amount, unit):
                self.calls.append((amount, unit))
                if self.moves and self.view == (0.0, 0.5) and amount > 0:
                    self.view = (0.5, 1.0)

            @staticmethod
            def winfo_exists():
                return True

        class Event:
            delta = -120
            num = 0

        window = object.__new__(DpsWindow)
        inner = Canvas((0.0, 1.0))
        outer = Canvas((0.2, 0.7))
        window.history_page_canvas = outer

        result = window._scroll_history_inner_canvas(inner, Event())

        self.assertEqual(result, "break")
        self.assertEqual(inner.calls, [])
        self.assertEqual(outer.calls, [(1, "units")])

        inner.view = (0.0, 0.5)
        window._scroll_history_inner_canvas(inner, Event())
        self.assertEqual(inner.calls, [(1, "units")])
        self.assertEqual(outer.calls, [(1, "units")])

        stalled = Canvas((0.25, 0.75), moves=False)
        window._scroll_history_inner_canvas(stalled, Event())
        self.assertEqual(stalled.calls, [(1, "units")])
        self.assertEqual(outer.calls, [(1, "units"), (1, "units")])

    def test_history_inner_scroll_binding_includes_its_scrollbar(self):
        class Widget:
            def __init__(self):
                self.bindings = {}

            def bind(self, event_name, callback):
                self.bindings[event_name] = callback

        window = object.__new__(DpsWindow)
        window._scroll_history_inner_canvas = mock.Mock(return_value="break")
        canvas = Widget()
        scrollbar = Widget()

        window._bind_history_inner_mousewheel(canvas, scrollbar)

        expected_events = {"<MouseWheel>", "<Button-4>", "<Button-5>"}
        self.assertEqual(set(canvas.bindings), expected_events)
        self.assertEqual(set(scrollbar.bindings), expected_events)
        event = object()
        self.assertEqual(scrollbar.bindings["<MouseWheel>"](event), "break")
        window._scroll_history_inner_canvas.assert_called_once_with(canvas, event)

    def test_history_critical_luck_requires_eight_known_hit_samples(self):
        window = object.__new__(DpsWindow)
        window._history_self_skill_events = lambda _record, _participant: {
            101: [
                {
                    "damage": 1_000 + index * 100,
                    "critical": index % 2 == 0,
                }
                for index in range(7)
            ]
        }

        model = window._history_critical_luck_model(
            {}, {"is_self": True, "damage": 9_100}
        )

        self.assertIsNone(model)

    def test_history_critical_luck_rewards_crits_on_high_damage_hits(self):
        base_damage = [100 * index for index in range(1, 11)]
        events = [
            {
                "damage": damage * (2 if index >= 5 else 1),
                "critical": index >= 5,
            }
            for index, damage in enumerate(base_damage)
        ]
        observed_damage = sum(int(row["damage"]) for row in events)
        window = object.__new__(DpsWindow)
        window._history_self_skill_events = lambda _record, _participant: {
            101: events
        }

        model = window._history_critical_luck_model(
            {}, {"is_self": True, "damage": observed_damage}
        )

        self.assertIsNotNone(model)
        self.assertEqual(model["critical_hits"], 5)
        self.assertAlmostEqual(model["observed"], observed_damage)
        self.assertGreater(model["extra"], 0)
        self.assertGreater(model["percentile"], 0.5)

    def test_history_skill_hit_navigation_uses_real_hit_order_and_maximum(self):
        window = object.__new__(DpsWindow)
        window.history_skill_timeline_hits = {
            (101, 0): {"damage": 200},
            (101, 1): {"damage": 900},
            (101, 2): {"damage": 400},
            (202, 0): {"damage": 5_000},
        }
        window.history_skill_timeline_selected_hit = (101, 0)
        window.history_skill_timeline_hover_hit = None
        window._draw_history_skill_timeline = lambda: None

        window._step_history_skill_hit(1)
        self.assertEqual(window.history_skill_timeline_selected_hit, (101, 1))
        window._step_history_skill_hit(1)
        self.assertEqual(window.history_skill_timeline_selected_hit, (101, 2))
        window._select_history_skill_max_hit()
        self.assertEqual(window.history_skill_timeline_selected_hit, (101, 1))
        self.assertEqual(window._format_history_hit_time(65.4326), "01:05")

    def test_history_skill_timeline_drag_selects_nearest_hit_in_pointed_row(self):
        class Canvas:
            def __init__(self):
                self.options = {}

            @staticmethod
            def canvasx(value):
                return value

            @staticmethod
            def canvasy(value):
                return value

            @staticmethod
            def delete(*_args, **_kwargs):
                return None

            @staticmethod
            def itemconfigure(*_args, **_kwargs):
                return None

            @staticmethod
            def create_line(*_args, **_kwargs):
                return None

            @staticmethod
            def create_rectangle(*_args, **_kwargs):
                return None

            @staticmethod
            def create_text(*_args, **_kwargs):
                return None

            @staticmethod
            def tag_raise(*_args, **_kwargs):
                return None

            def configure(self, **options):
                self.options.update(options)

        class Event:
            def __init__(self, x, y):
                self.x = x
                self.y = y

        window = object.__new__(DpsWindow)
        window.history_skill_timeline_canvas = Canvas()
        window.history_skill_timeline_plot_bounds = (100.0, 500.0, 32.0, 140.0, 10.0)
        window.history_skill_timeline_hits = {
            (101, 0): {
                "time_seconds": 2.0,
                "_x": 180.0,
                "_center_y": 59.0,
                "_row_top": 32.0,
                "_row_bottom": 86.0,
                "color": "#71d6b1",
            },
            (101, 1): {
                "time_seconds": 8.0,
                "_x": 420.0,
                "_center_y": 59.0,
                "_row_top": 32.0,
                "_row_bottom": 86.0,
                "color": "#71d6b1",
            },
            (202, 0): {
                "time_seconds": 5.0,
                "_x": 300.0,
                "_center_y": 113.0,
                "_row_top": 86.0,
                "_row_bottom": 140.0,
                "color": "#6dbce7",
            },
        }
        window.history_skill_timeline_selected_hit = None
        window.history_skill_timeline_hover_hit = None
        window.history_skill_timeline_dragging = False
        window._draw_history_skill_hit_detail = mock.Mock()
        window._ui_font = lambda _kind: None

        self.assertEqual(
            window._history_skill_timeline_drag_start(Event(410, 55)), "break"
        )
        self.assertTrue(window.history_skill_timeline_dragging)
        self.assertEqual(window.history_skill_timeline_selected_hit, (101, 1))

        self.assertEqual(
            window._history_skill_timeline_drag_motion(Event(170, 105)), "break"
        )
        self.assertEqual(window.history_skill_timeline_selected_hit, (202, 0))
        self.assertEqual(
            window._history_skill_timeline_drag_end(Event(190, 105)), "break"
        )
        self.assertFalse(window.history_skill_timeline_dragging)
        self.assertEqual(
            window.history_skill_timeline_canvas.options["cursor"], "hand2"
        )

    def test_history_team_report_rows_keep_the_full_roster(self):
        participants = [
            {
                "actor_id": index + 1,
                "name": f"队员{index + 1}",
                "profession_id": 1_200_001 + index % 7,
                "damage": (12 - index) * 1_000,
                "dps": (12 - index) * 100,
                "share": (12 - index) / 78,
                "is_self": index == 4,
            }
            for index in range(12)
        ]
        record = {
            "participants": participants,
            "healers": [
                {
                    "actor_id": 5,
                    "name": "队员5",
                    "effective_healing": 3_000,
                    "hps": 300,
                }
            ],
            "duration_seconds": 10,
            "hps_duration_seconds": 10,
            "total_damage": 78_000,
            "team_effective_healing": 3_000,
            "team_size": 12,
            "team_dps": 7_800,
            "dungeon_name": "五月庄园·花园",
            "stage_name": "星象仪者",
            "boss_name": "星象仪者",
            "boss_count": 1,
            "result": "defeated",
            "completeness": "complete",
            "started_at_epoch": 1_700_000_000,
        }
        window = object.__new__(DpsWindow)
        window.hide_names = False
        window.professions = {}

        damage_rows = window._history_team_report_rows(record, "dps")
        healing_rows = window._history_team_report_rows(record, "hps")

        self.assertEqual(len(damage_rows), 12)
        self.assertEqual(len(healing_rows), 12)
        self.assertEqual(damage_rows[0]["name"], "队员1")
        self.assertEqual(healing_rows[0]["actor_id"], 5)
        self.assertEqual(sum(row["amount"] for row in healing_rows), 3_000)
        report = window._render_history_team_report_image(
            record, "dps", hide_names=True
        )
        self.assertEqual(report.width, 1240)
        self.assertGreaterEqual(report.height, 900)
        output_globals = window._history_team_report_output_path.__globals__
        original_app_dir = output_globals["APP_DIR"]
        with tempfile.TemporaryDirectory() as temporary:
            output_globals["APP_DIR"] = Path(temporary)
            try:
                output_path = window._history_team_report_output_path(
                    record, "dps"
                )
            finally:
                output_globals["APP_DIR"] = original_app_dir
        self.assertEqual(output_path.parent, Path(temporary))

    def test_history_non_dps_timelines_state_the_available_data_scope(self):
        class Canvas:
            def __init__(self):
                self.texts = []
                self.options = {}

            @staticmethod
            def winfo_width():
                return 700

            @staticmethod
            def winfo_height():
                return 260

            def delete(self, *_args, **_kwargs):
                self.texts.clear()

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

            def configure(self, **kwargs):
                self.options.update(kwargs)

        class Label:
            def __init__(self):
                self.options = {}

            def configure(self, **kwargs):
                self.options.update(kwargs)

        damage_row = {"actor_id": SELF_ID, "name": "本机", "is_self": True}
        record = {
            "participants": [damage_row],
            "healers": [{"actor_id": SELF_ID, "name": "治疗者"}],
            "damage_taken": [{"actor_id": SELF_ID, "name": "承伤者"}],
        }
        window = object.__new__(DpsWindow)
        window.history_selected_actor = SELF_ID
        window.history_skill_timeline_canvas = Canvas()
        window.history_skill_timeline_title_label = Label()
        window._selected_history_damage_participant = lambda: (record, damage_row)
        window._ui_font = lambda _kind: None

        window.history_meter_mode = "hps"
        window._draw_history_skill_timeline()
        rendered = [options.get("text") for _args, options in window.history_skill_timeline_canvas.texts]
        self.assertEqual(
            window.history_skill_timeline_title_label.options["text"],
            "治疗者 · 治疗时间轴",
        )
        self.assertIn("暂无逐次治疗时间戳", rendered)
        self.assertIn("治疗页保留结算技能构成，不生成推测时间线", rendered)

        window.history_hide_names = True
        window._draw_history_skill_timeline()
        self.assertEqual(
            window.history_skill_timeline_title_label.options["text"],
            "玩家1 · 治疗时间轴",
        )

        window.history_meter_mode = "dt"
        window._draw_history_skill_timeline()
        rendered = [options.get("text") for _args, options in window.history_skill_timeline_canvas.texts]
        self.assertEqual(
            window.history_skill_timeline_title_label.options["text"],
            "玩家1 · 承伤时间轴",
        )
        self.assertIn("暂无逐次承伤时间戳", rendered)
        self.assertIn("承伤页只展示可靠汇总，不生成推测来源与时间线", rendered)

    def test_history_team_trend_draws_twelve_players_below_raised_team_line(self):
        class Canvas:
            def __init__(self):
                self.lines = []
                self.texts = []
                self.raised = []

            @staticmethod
            def winfo_width():
                return 320

            @staticmethod
            def winfo_height():
                return 190

            def delete(self, target, *_args, **_kwargs):
                if target == "all":
                    self.lines.clear()
                    self.texts.clear()
                    self.raised.clear()

            def create_line(self, *args, **kwargs):
                self.lines.append((args, kwargs))

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

            @staticmethod
            def create_polygon(*_args, **_kwargs):
                return None

            @staticmethod
            def create_rectangle(*_args, **_kwargs):
                return None

            @staticmethod
            def create_oval(*_args, **_kwargs):
                return None

            def tag_raise(self, tag):
                self.raised.append(tag)

        class Label:
            def __init__(self):
                self.options = {}

            def configure(self, **kwargs):
                self.options.update(kwargs)

        participants = [
            {"actor_id": index, "name": f"队员{index}", "profession_id": 0}
            for index in range(1, 13)
        ]
        player_series = {
            index: [(0.0, 50.0 + index), (10.0, 75.0 + index)]
            for index in range(1, 13)
        }
        window = object.__new__(DpsWindow)
        window.history_meter_mode = "dps"
        window.history_snapshot_trend_canvas = Canvas()
        window.history_snapshot_trend_title_label = Label()
        window.history_snapshot_peak_label = Label()
        window.history_trend_source = "live_team_cumulative"
        window.history_participant_trend_scope = "all_players"
        window.history_trend_hover_time = None
        window.history_trend_selected_time = None
        window.hide_names = False
        window._history_snapshot_team_dps_points = lambda: [
            (0.0, 700.0),
            (10.0, 1_000.0),
        ]
        window._history_snapshot_participant_dps_points = lambda: player_series
        window._selected_history_record = lambda: {"participants": participants}
        window._ui_font = lambda _kind: None
        window._fit_ui_text = lambda value, _width, _kind: value

        window._draw_history_snapshot_trend()

        player_lines = [
            options
            for _args, options in window.history_snapshot_trend_canvas.lines
            if options.get("tags") == ("history-player-line",)
        ]
        team_line = next(
            options
            for _args, options in window.history_snapshot_trend_canvas.lines
            if options.get("tags") == ("history-team-line",)
        )
        self.assertEqual(len(player_lines), 12)
        self.assertEqual(len({line["fill"] for line in player_lines}), 12)
        self.assertEqual(team_line["width"], 3)
        self.assertIn("history-team-line", window.history_snapshot_trend_canvas.raised)
        self.assertEqual(
            window.history_snapshot_trend_title_label.options["text"],
            "全员 + 团队 DPS",
        )
        self.assertEqual(
            window.history_snapshot_peak_label.options["text"],
            "全员 + 团队 · 10 秒滑动",
        )

        window.history_trend_hover_time = 5
        window._draw_history_snapshot_trend()
        rendered = {
            options.get("text")
            for _args, options in window.history_snapshot_trend_canvas.texts
        }
        for index in range(1, 13):
            self.assertIn(f"队员{index}", rendered)

    def test_history_snapshot_displays_final_team_dps(self):
        class Label:
            def __init__(self):
                self.options = {}

            def configure(self, **options):
                self.options.update(options)

        summary = {
            "battle_id": "team-dps-snapshot",
            "team_dps": 108_099.21,
            "team_size": 6,
            "duration_seconds": 24.0,
            "my_dps": 20_000.0,
            "my_damage": 480_000,
            "my_share": 0.185,
            "result": "victory",
            "completeness": "complete",
        }
        window = DpsWindow.__new__(DpsWindow)
        window.history_records = [summary]
        window.history_selected_id = summary["battle_id"]
        window.history_meter_mode = "dps"
        window.history_snapshot_labels = {
            key: Label()
            for key in (
                "team_dps",
                "my_dps",
                "my_damage",
                "my_share",
                "team_size",
                "duration",
                "result",
                "my_dps_caption",
                "my_damage_caption",
                "my_share_caption",
            )
        }
        window.history_snapshot_trend_title_label = Label()
        window.history_snapshot_status_label = Label()
        window._render_history_snapshot_identity = lambda **_kwargs: None
        window._draw_history_snapshot_result = lambda: None
        window._draw_history_snapshot_trend = lambda: None

        window._render_history_snapshot()

        self.assertEqual(
            window.history_snapshot_labels["team_dps"].options["text"],
            "108,099",
        )


if __name__ == "__main__":
    unittest.main()
