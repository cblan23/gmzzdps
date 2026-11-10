"""Application retention and identity boundaries from the reported team issues."""
import unittest
from types import SimpleNamespace
from unittest import mock

from test_main_hud_behavior import make_window
from test_combat_model import CombatModel, damage, SELF_ID, TEAMMATE_ID, MONSTER_ID, BASE_FILETIME


class ApplicationSessionTests(unittest.TestCase):
    def test_burst_remains_readable_and_duplicate_keeps_its_position(self):
        window, _ = make_window()
        for index in range(20):
            window._ingest_team_application(dict(user_token=f"applicant-{index}",
                name=f"玩家{index}", extraordinary_rating=100_000 + index), now=100 + index)
        before = window._team_application_section_rows(now=7200)
        self.assertEqual(len(before), 20)
        window._ingest_team_application(dict(user_token="applicant-0", name="玩家0",
            extraordinary_rating=188_888), now=7201)
        after = window._team_application_section_rows(now=7300)
        self.assertEqual([row["application_token"] for row in after],
                         [row["application_token"] for row in before])
        self.assertEqual(after[0]["rating_text"], "188,888")

    def test_application_event_keeps_main_scroll_position(self):
        window, _ = make_window()
        window.main_scroll_offset = 120
        window._schedule_layered_main_render = mock.Mock()
        window._dispatch_message("team_application", dict(user_token="applicant",
            name="申请者", extraordinary_rating=90_000))
        self.assertEqual(window.main_scroll_offset, 120)

    def test_old_session_applications_are_rejected_and_reset_collapses_section(self):
        window, _ = make_window()
        window.model.party_session_id = 7
        self.assertFalse(window._ingest_team_application(dict(user_token="old",
            name="旧团申请", party_session_id=6)))
        window._ingest_team_application(dict(user_token="new", name="新团申请",
            party_session_id=7))
        window.team_applications_expanded = True
        window._reset_team_applications()
        self.assertEqual(window._team_application_section_rows(), [])
        self.assertFalse(window.team_applications_expanded)

    def test_missing_rating_and_hidden_names_in_application_panel(self):
        window, _ = make_window()
        window.hide_names = True
        window._ingest_team_application(dict(user_token="applicant", name="私人名称"))
        row, = window._team_application_section_rows()
        self.assertEqual(row["name"], "申请玩家1")
        self.assertEqual(row["rating_text"], "--")

    def test_cached_departed_member_is_not_part_of_current_roster(self):
        window, members = make_window()
        members.intersection_update({1, 2})
        window._main_rating_display_rows = lambda: [
            dict(actor_id=1, name="本人", rating=100_000),
            dict(actor_id=99, name="上一团成员", rating=180_000)]
        rows = window._main_team_rating_section_rows()
        self.assertEqual({row["actor_id"] for row in rows}, {1, 2})

    def test_support_button_opens_the_requested_shop(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: True
        window._startup_interaction_blocked = lambda: False
        window._layered_main_region_at = lambda _x, _y: "action:support"
        event = SimpleNamespace(x=10, y=10, x_root=10, y_root=10)
        window._layered_main_press(event)
        self.assertFalse(window.layered_main_drag_armed)
        with mock.patch("webbrowser.open") as browser:
            window._layered_main_release(event)
        browser.assert_called_once_with("https://wzyp.cn/shop/QDAWZ5KZ", new=2)

    def team_window(self):
        window, _ = make_window()
        window.pve_hud_view = "team"
        window._main_row_height = lambda: 24
        window._schedule_layered_main_render = mock.Mock()
        window._team_rating_preview_rows = lambda: [dict(
            actor_id=i, display_name=f"队员{i}", profession_id=1_200_003,
            extraordinary_rating=100_000) for i in range(1, 7)]
        for index in range(20):
            window._ingest_team_application(dict(user_token=f"applicant-{index}",
                name=f"申请{index}", extraordinary_rating=120_000 + index,
                profession_id=1_200_003))
        return window

    def test_applications_expand_inside_team_view_and_remain_separate_from_members(self):
        window = self.team_window()
        collapsed = window._layered_main_snapshot()
        heading = next(row for row in collapsed["rows"] if row.get("section_text") == "团队构成")
        self.assertEqual(heading["application_count"], 20)
        self.assertNotIn("team_application", {row.get("row_kind") for row in collapsed["rows"]})
        window.window_locked = False
        window._layered_main_active = lambda: True
        window._startup_interaction_blocked = lambda: False
        window._layered_main_region_at = lambda _x, _y: "action:applications"
        event = SimpleNamespace(x=10, y=10, x_root=10, y_root=10)
        window._layered_main_press(event)
        self.assertFalse(window.layered_main_drag_armed)
        window._layered_main_release(event)
        expanded = window._layered_main_snapshot()
        applicants = [row for row in expanded["rows"] if row.get("row_kind") == "team_application"]
        self.assertEqual(len(applicants), 20)
        self.assertEqual(window.pve_hud_view, "team")
        self.assertGreater(window.main_scroll_offset, 0)
        self.assertEqual(len(window.model.current_stats()), 6)
        window._toggle_team_applications()
        self.assertFalse(window.team_applications_expanded)
        self.assertEqual(window.main_scroll_offset, 0)

    def test_application_list_is_not_inserted_in_dps_view(self):
        window = self.team_window()
        window.team_applications_expanded = True
        window.pve_hud_view = "recent_battle"
        self.assertNotIn("team_application", {row.get("row_kind") for row in window._layered_main_snapshot()["rows"]})


class TeamIdentityBoundaryTests(unittest.TestCase):
    def model(self):
        model = CombatModel()
        model.ingest_identity(dict(entity_id=SELF_ID))
        model.ingest_party(dict(entity_ids=[TEAMMATE_ID], member_count=2,
            party_session_id=7, authoritative=True, user_tokens=["AQAAABEREREHAAAA"]))
        model.ingest_profile(dict(entity_id=TEAMMATE_ID, name="原队员",
            profession_id=1_200_003, user_token="AQAAABEREREHAAAA", entity_type="Player"))
        model.ingest_profile(dict(entity_id=MONSTER_ID, entity_type="Boss", boss_rank=3, name="测试首领"))
        model.ingest(damage(1, TEAMMATE_ID, MONSTER_ID, 1000))
        return model

    def test_new_player_counter_cannot_be_applied_to_old_players_actor(self):
        model = self.model()
        before = model.stats[TEAMMATE_ID].damage
        self.assertFalse(model.ingest_team_stat(dict(actor_id=TEAMMATE_ID,
            user_token="AQAAAOwNKLYHAAAA", party_session_id=7,
            absolute_damage=5000, full_snapshot=True, filetime_100ns=BASE_FILETIME + 50_000)))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, before)

    def test_merge_cannot_relabel_old_players_damage_as_another_character(self):
        model = self.model()
        self.assertFalse(model.merge_actor(dict(from_actor_id=TEAMMATE_ID,
            to_actor_id=TEAMMATE_ID + 100, user_token="AQAAAOwNKLYHAAAA", replace_profile=True)))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 1000)
        self.assertEqual(model.display_name(TEAMMATE_ID), "原队员")

    def test_merge_cannot_combine_two_known_characters_even_with_a_source_token(self):
        model = self.model()
        other_actor = TEAMMATE_ID + 100
        model.ingest_profile(dict(entity_id=other_actor, name="新队员",
            user_token="AQAAAOwNKLYHAAAA", entity_type="Player"))
        self.assertFalse(model.merge_actor(dict(from_actor_id=TEAMMATE_ID,
            to_actor_id=other_actor, user_token="AQAAABEREREHAAAA", replace_profile=True)))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 1000)
        self.assertEqual(model.display_name(other_actor), "新队员")

    def test_departed_identity_stays_in_the_battle_without_live_cache_rows(self):
        model = self.model()
        model.ingest_party(dict(entity_ids=[TEAMMATE_ID + 100], member_count=2,
            party_session_id=7, authoritative=True, roster_replace=True,
            user_tokens=["AQAAAOwNKLYHAAAA"]))
        self.assertNotIn(TEAMMATE_ID, model.entity_names)
        record = model.build_combat_record()
        old_row = next(row for row in record["participants"] if row["actor_id"] == TEAMMATE_ID)
        self.assertEqual(old_row["name"], "原队员")
        self.assertEqual(old_row["damage"], 1000)
        self.assertEqual(old_row["profession_id"], 1_200_003)
        self.assertIn(dict(actor_id=TEAMMATE_ID, character_id="AQAAABEREREHAAAA"), record["participant_identities"])

    def test_new_team_archives_old_battle_and_rejects_late_counter(self):
        model = self.model()
        old_encounter = model.encounter_id
        model.ingest_party(dict(entity_ids=[TEAMMATE_ID + 100], member_count=2,
            party_session_id=8, authoritative=True, roster_replace=True,
            user_tokens=["AQAAAOwNKLYHAAAA"]))
        self.assertNotEqual(model.encounter_id, old_encounter)
        self.assertFalse(model.events)
        archived = next(row for row in model.completed_combats if row["encounter_id"] == old_encounter)
        self.assertEqual(archived["participants"][0]["name"], "原队员")
        self.assertFalse(model.ingest_team_stat(dict(actor_id=TEAMMATE_ID,
            user_token="AQAAABEREREHAAAA", party_session_id=7, absolute_damage=9000,
            full_snapshot=True, filetime_100ns=BASE_FILETIME + 100_000)))

    def test_departed_players_identity_also_guards_later_merges(self):
        model = self.model()
        model.ingest_party(dict(entity_ids=[TEAMMATE_ID + 100], member_count=2,
            party_session_id=7, authoritative=True, roster_replace=True,
            user_tokens=["AQAAAOwNKLYHAAAA"]))
        self.assertFalse(model.merge_actor(dict(from_actor_id=TEAMMATE_ID,
            to_actor_id=TEAMMATE_ID + 100, user_token="AQAAAOwNKLYHAAAA", replace_profile=True)))
        self.assertEqual(model.stats[TEAMMATE_ID].damage, 1000)


if __name__ == "__main__":
    unittest.main()
