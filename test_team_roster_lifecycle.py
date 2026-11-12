"""Exercise team replacements through the parser, model and live HUD."""
import copy
import unittest
from unittest import mock

from test_combat_model import CombatModel, damage
from test_main_hud_behavior import make_window
from npcap_parser_adapter import NpcapParserAdapter
from test_network_state import (
    NetworkPacketParser, packet, PLAYER_ID, MONSTER_ID,
    SELF_TOKEN, TEAMMATE_TOKEN,
)


NEW_TOKEN = "AQAAABEREREHAAAA"
SECOND_NEW_TOKEN = "AQAAACIiIiIHAAAA"
BASE_FILETIME = 134_321_845_085_764_054
WINDOWS_EPOCH = 116_444_736_000_000_000


class TeamRosterLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.parser = NetworkPacketParser()
        self.parser.self_id = PLAYER_ID
        self.parser.self_token = SELF_TOKEN
        self.parser.self_confirmed = True
        self.parser._bind_team_token(SELF_TOKEN, PLAYER_ID)
        self.window, _ = make_window()
        self.model = CombatModel()
        self.model.ingest_identity(dict(entity_id=PLAYER_ID))
        self.model.ingest_profile(dict(entity_id=PLAYER_ID, user_token=SELF_TOKEN,
                                      name="本人", entity_type="Player"))
        self.window.model = self.model
        self.window._shown_actor_name = self.model.display_name
        self.window._schedule_layered_main_render = mock.Mock()
        self.window._schedule_team_equipment_profiles = mock.Mock()
        self.window._invalidate_team_rating_preview_rows = mock.Mock()
        self.window._rebaseline_team_equipment_rating_after_capture_change = mock.Mock(return_value=False)
        self.window._sync_startup_identity_state = mock.Mock()
        self.window._hide_enrage_tooltip = mock.Mock()
        self.window._set_pve_hud_view = mock.Mock()
        self.window._main_recent_settlement_record = mock.Mock(return_value=None)
        self.window.settlement_ui = None
        self.window.live_hud_combat_state = None
        self.window.live_hud_boss_state = None
        self.window.live_hud_dps_segment = None
        self.feed("OnJoinGroupSuccess", [self.roster(TEAMMATE_TOKEN, "原队员")], 1)

    @staticmethod
    def roster(token, name):
        return [
            {"$map": [[2, SELF_TOKEN], [5, "本人"], [8, 1_200_003]]},
            {"$map": [[2, token], [5, name], [8, 1_200_003]]},
        ]

    def feed(self, method, arguments, sequence):
        record = packet(method, arguments, sequence=sequence)
        record["filetime_100ns"] = BASE_FILETIME + sequence * 10_000_000
        updates = self.parser.process(record)
        for kind, payload in updates:
            self.window._dispatch_message(kind, payload)
        return updates

    def start_battle(self):
        actor = self.parser.token_actors[TEAMMATE_TOKEN]
        self.model.ingest_profile(dict(entity_id=MONSTER_ID, entity_type="Boss",
                                       boss_rank=3, name="测试首领"))
        event = damage(1, actor, MONSTER_ID, 1000)
        event["filetime_100ns"] = BASE_FILETIME + 20_000_000
        self.model.ingest(event)
        self.window._begin_live_hud_dps_segment(
            kind="boss", segment_key=MONSTER_ID,
            started_at_epoch=(BASE_FILETIME - WINDOWS_EPOCH) / 10_000_000,
        )
        return actor

    def stat(self, token, actor, *, session=None, sequence=4, amount=5000):
        return dict(
            actor_id=actor, user_token=token, absolute_damage=amount,
            party_session_id=self.model.party_session_id if session is None else session,
            full_snapshot=True, snapshot_tokens=[SELF_TOKEN, token],
            filetime_100ns=BASE_FILETIME + sequence * 10_000_000,
        )

    def test_wrong_character_counter_cannot_enter_live_hud_pending_rows(self):
        old_actor = self.start_battle()
        before = copy.deepcopy(self.window.live_hud_dps_segment)
        self.window._dispatch_message("team_stat", self.stat(NEW_TOKEN, old_actor))
        self.assertEqual(self.window.live_hud_dps_segment, before)
        self.assertEqual(self.model.stats[old_actor].damage, 1000)

    def test_confirmed_join_promotes_application_and_keeps_known_profile(self):
        application = {"$map": [
            [2, NEW_TOKEN], [4, "申请队友"], [5, 1_200_007], [8, 196_882],
        ]}
        self.feed("OnMsgNewTeamGroupApply", [application], 2)
        self.assertEqual(len(self.window._team_application_section_rows()), 1)
        self.assertNotIn(NEW_TOKEN, self.model.party_user_tokens)

        # The first membership confirmation has HP but no name/profession.
        self.feed("OnSyncTeamGroupPropsForceRefresh",
                  [NEW_TOKEN, 30_756, 30_756, 0, 0, 0, False], 3)
        actor = self.parser.token_actors[NEW_TOKEN]
        self.assertEqual(self.window._team_application_section_rows(), [])
        row = next(row for row in self.window._main_team_rating_section_rows()
                   if row["actor_id"] == actor)
        self.assertEqual(row["name"], "申请队友")
        self.assertEqual(row["profession_id"], 1_200_007)
        self.assertEqual(row["rating"], 196_882)

        # A delayed duplicate application cannot reappear after admission.
        self.feed("OnMsgNewTeamGroupApply", [application], 4)
        self.assertEqual(self.window._team_application_section_rows(), [])

    def test_partial_stage_snapshot_keeps_incrementally_confirmed_teammate(self):
        self.parser = NetworkPacketParser()
        self.parser.self_id = PLAYER_ID
        self.parser.self_token = SELF_TOKEN
        self.parser.self_confirmed = True
        self.parser._bind_team_token(SELF_TOKEN, PLAYER_ID)
        self.feed("OnSyncTeamGroupPropsForceRefresh",
                  [TEAMMATE_TOKEN, 100, 100, 0, 0, 0, False], 2)
        self.assertFalse(self.parser.explicit_party_roster_seen)
        actor = self.parser.token_actors[TEAMMATE_TOKEN]
        self.window._ingest_stage_summary = mock.Mock()
        stage = {"$map": [[0, 5_150_059], [3, False], [5, {
            SELF_TOKEN: {"$map": [
                [0, SELF_TOKEN], [1, PLAYER_ID], [4, 1_200_002],
                [5, "本人"], [6, 500],
            ]},
        }]]}
        self.feed("OnMsgUpdateStageCombatStatistics", [stage], 3)
        self.assertIn(TEAMMATE_TOKEN, self.parser.current_team_profile_tokens())
        self.assertIn(actor, self.model._current_member_ids())

    def test_first_inferred_session_retains_pending_applications(self):
        self.model.party_session_id = 0
        self.window._ingest_team_application(dict(
            user_token=NEW_TOKEN, name="等待入队", extraordinary_rating=196_882))
        self.window._ingest_team_application(dict(
            user_token=TEAMMATE_TOKEN, name="原队员", extraordinary_rating=100_000))
        self.window._dispatch_message("party", dict(
            entity_ids=list(self.model.party_ids),
            user_tokens=[SELF_TOKEN, TEAMMATE_TOKEN], member_count=2,
            in_team=True, party_session_id=1, authoritative=True))
        applications = self.window._team_application_section_rows()
        self.assertEqual([row["application_token"] for row in applications], [NEW_TOKEN])

    def test_group_transfer_preserves_two_applications_until_join_confirmation(self):
        applications = (
            (NEW_TOKEN, "申请队友甲", 196_882),
            (SECOND_NEW_TOKEN, "申请队友乙", 201_817),
        )
        for sequence, (token, name, rating) in enumerate(applications, start=2):
            self.feed("OnMsgNewTeamGroupApply", [{"$map": [
                [2, token], [4, name], [5, 1_200_003], [8, rating],
            ]}], sequence)
        self.assertEqual(
            [row["application_token"] for row in self.window._team_application_section_rows()],
            [NEW_TOKEN, SECOND_NEW_TOKEN],
        )

        self.feed("OnMsgQuitTeamGroup", [57_353_919_529_918], 4)
        self.assertEqual(self.model.party_session_id, 0)
        self.assertEqual(len(self.window._team_application_section_rows()), 2)

        roster = [
            {"$map": [[2, SELF_TOKEN], [5, "self"], [8, 1_200_002]]}
        ] + [
            {"$map": [[2, token], [5, name], [8, 1_200_003]]}
            for token, name, _rating in applications
        ]
        self.feed("OnJoinGroupSuccess", roster, 5)

        self.assertEqual(self.window._team_application_section_rows(), [])
        self.assertTrue({NEW_TOKEN, SECOND_NEW_TOKEN} <= self.model.party_user_tokens)
        for token, _name, rating in applications:
            actor_id = next(
                actor for actor, character in self.model.actor_character_ids.items()
                if character == token
            )
            self.assertIn(actor_id, self.model._current_member_ids())
            self.assertEqual(self.model.entity_extraordinary_ratings[actor_id], rating)

    def test_dungeon_roster_kick_and_replacements_keep_each_players_own_damage(self):
        self.parser = NpcapParserAdapter()
        self.parser.self_id = PLAYER_ID
        self.parser.self_token = SELF_TOKEN
        self.parser.self_confirmed = True
        self.parser._bind_team_token(SELF_TOKEN, PLAYER_ID)

        def roster(token, name, sequence):
            members = [dict(user_token=SELF_TOKEN, name="本人", team_id=123)]
            if token:
                members.append(dict(user_token=token, name=name, team_id=123))
            updates = self.parser.apply_read_only_group_roster(dict(
                method="ReadOnlyCurrentDungeonRoster", dungeon_id=5_100_055,
                group_id=0, local_user_token=SELF_TOKEN, members=members,
                filetime_100ns=BASE_FILETIME + sequence * 10_000_000))
            for kind, payload in updates:
                self.window._dispatch_message(kind, payload)

        roster(TEAMMATE_TOKEN, "原队员", 1)
        old_actor = self.start_battle()
        session = self.model.party_session_id
        for index, token in enumerate((NEW_TOKEN, "AQAAACIiIiIHAAAA")):
            with self.subTest(replacement=index):
                sequence = 3 + index * 3
                # A kick can be proven by the complete Lua roster even when
                # no explicit leave RPC was captured for the departing player.
                roster(None, "", sequence)
                self.assertEqual(self.model._current_member_ids(), {PLAYER_ID})
                self.assertFalse(self.model.ingest_team_stat(
                    self.stat(TEAMMATE_TOKEN, old_actor, sequence=sequence)))
                self.window._ingest_team_application(dict(
                    user_token=token, name=f"新队员{index}", extraordinary_rating=190_000))
                roster(token, f"新队员{index}", sequence + 1)
                actor = self.parser.token_actors[token]
                self.assertEqual(self.model._current_member_ids(), {PLAYER_ID, actor})
                self.assertEqual(self.model.party_session_id, session)
                self.assertEqual(self.window._team_application_section_rows(), [])
                counters = {"$map": [
                    [SELF_TOKEN, {"$map": [[4, "本人"], [5, 200]]}],
                    [token, {"$map": [[4, f"新队员{index}"], [5, 500 + index]]}],
                ]}
                self.feed("RetCommonCombatStatisticsByTeam", [counters], sequence + 2)
                rows = self.window.live_hud_dps_segment["rows"]
                row = next(row for row in rows if row["actor_id"] == actor)
                self.assertEqual(row["total_value"], 500 + index)
                self.assertNotIn(old_actor, {row["actor_id"] for row in rows})

        record = self.model.build_combat_record()
        old_row = next(row for row in record["participants"] if row["actor_id"] == old_actor)
        self.assertEqual((old_row["name"], old_row["damage"]), ("原队员", 1000))

    def test_join_application_does_not_overwrite_newer_member_profile(self):
        self.window._ingest_team_application(dict(
            user_token=NEW_TOKEN, name="旧申请名", profession_id=1_200_007,
            extraordinary_rating=196_882))
        self.feed("OnMsgOtherJoinTeamGroup", [0, 0, {"$map": [
            [2, NEW_TOKEN], [5, "当前队友名"], [8, 1_200_006], [27, 200_000],
        ]}], 2)
        actor = self.parser.token_actors[NEW_TOKEN]
        self.assertEqual(self.window._team_application_section_rows(), [])
        self.assertEqual(self.model.entity_names[actor], "当前队友名")
        self.assertEqual(self.model.entity_professions[actor], 1_200_006)
        self.assertEqual(self.model.entity_extraordinary_ratings[actor], 200_000)

    def test_wrong_session_counter_cannot_enter_live_hud_pending_rows(self):
        old_actor = self.start_battle()
        before = copy.deepcopy(self.window.live_hud_dps_segment)
        self.window._dispatch_message("team_stat", self.stat(
            TEAMMATE_TOKEN, old_actor, session=self.model.party_session_id + 1))
        self.assertEqual(self.window.live_hud_dps_segment, before)

    def test_queued_departed_member_counter_does_not_reenter_the_same_team(self):
        old_actor = self.start_battle()
        self.feed("OnMsgOtherQuitTeam", [TEAMMATE_TOKEN], 3)
        before = copy.deepcopy(self.window.live_hud_dps_segment)
        self.window._dispatch_message("team_stat", self.stat(TEAMMATE_TOKEN, old_actor))
        self.assertEqual(self.window.live_hud_dps_segment, before)
        self.assertEqual(self.model.stats[old_actor].damage, 1000)

    def test_explicit_leave_clears_live_projection_during_active_battle(self):
        self.start_battle()
        self.window.live_hud_combat_state = {"active": True}
        self.window.live_hud_boss_state = {"active": True}
        self.feed("OnMsgQuitTeam", [], 5)
        self.assertEqual(self.model.party_session_id, 0)
        self.assertIsNone(self.window.live_hud_dps_segment)
        self.assertIsNone(self.window.live_hud_combat_state)
        self.assertIsNone(self.window.live_hud_boss_state)
        self.assertFalse(self.model.current_stats())

    def test_changed_join_without_leave_packet_starts_a_fresh_team_and_battle(self):
        old_actor = self.start_battle()
        previous_session = self.model.party_session_id
        previous_encounter = self.model.encounter_id
        self.feed("OnJoinGroupSuccess", [self.roster(NEW_TOKEN, "新队员")], 6)
        self.assertGreater(self.model.party_session_id, previous_session)
        self.assertNotEqual(self.model.encounter_id, previous_encounter)
        self.assertIsNone(self.window.live_hud_dps_segment)
        self.assertNotIn(old_actor, self.model._current_member_ids())
        self.assertFalse(self.model.current_stats())
        self.assertFalse(self.window._main_context_display_is_stale())
        record = next(row for row in self.model.completed_combats
                      if row["encounter_id"] == previous_encounter)
        old_row = next(row for row in record["participants"] if row["actor_id"] == old_actor)
        self.assertEqual((old_row["name"], old_row["damage"]), ("原队员", 1000))

    def test_identical_join_snapshot_preserves_current_battle(self):
        old_actor = self.start_battle()
        session = self.model.party_session_id
        encounter = self.model.encounter_id
        segment = self.window.live_hud_dps_segment
        self.feed("OnJoinGroupSuccess", [self.roster(TEAMMATE_TOKEN, "原队员")], 4)
        self.assertEqual(self.model.party_session_id, session)
        self.assertEqual(self.model.encounter_id, encounter)
        self.assertIs(self.window.live_hud_dps_segment, segment)
        self.assertEqual(self.model.stats[old_actor].damage, 1000)

    def test_full_roster_removal_is_not_undone_by_late_old_member_heartbeat(self):
        self.feed("OnSyncTeamGroupInfo", [self.roster(NEW_TOKEN, "新队员")], 4)
        self.feed("OnUpdateTeamGroupMemberProps", [TEAMMATE_TOKEN,
                  {"$map": [[4, False], [5, 100], [6, 100]]}], 5)
        self.assertNotIn(TEAMMATE_TOKEN, self.parser.current_team_profile_tokens())
        self.assertNotIn(TEAMMATE_TOKEN, self.model.party_user_tokens)

    def test_departed_session_counter_is_rejected_even_before_rejoin(self):
        old_actor = self.start_battle()
        old_session = self.model.party_session_id
        self.feed("OnMsgQuitTeam", [], 4)
        self.assertFalse(self.model.ingest_team_stat(
            self.stat(TEAMMATE_TOKEN, old_actor, session=old_session, sequence=5)))
        self.assertFalse(self.model.team_damage_states)

    def test_new_team_clears_targetless_counters_without_a_started_encounter(self):
        counters = {"$map": [
            [SELF_TOKEN, {"$map": [[4, "本人"], [5, 200]]}],
            [TEAMMATE_TOKEN, {"$map": [[4, "原队员"], [5, 1000]]}],
        ]}
        self.feed("RetCommonCombatStatisticsByTeam", [counters], 2)
        self.assertFalse(self.model._encounter_started())
        self.assertTrue(self.model.targetless_team_states)
        self.feed("OnJoinGroupSuccess", [self.roster(NEW_TOKEN, "新队员")], 5)
        self.assertFalse(self.model.targetless_team_states)
        self.assertEqual(self.model.targetless_team_started_at, 0)

    def test_late_start_counter_snapshot_survives_its_first_inferred_roster(self):
        self.parser = NetworkPacketParser()
        self.parser.self_id = PLAYER_ID
        self.parser.self_token = SELF_TOKEN
        self.parser.self_confirmed = True
        self.parser._bind_team_token(SELF_TOKEN, PLAYER_ID)
        self.model = CombatModel()
        self.model.ingest_identity(dict(entity_id=PLAYER_ID))
        self.window.model = self.model
        self.window._shown_actor_name = self.model.display_name
        counters = {"$map": [
            [SELF_TOKEN, {"$map": [[4, "本人"], [5, 200]]}],
            [TEAMMATE_TOKEN, {"$map": [[4, "原队员"], [5, 1000]]}],
        ]}
        self.feed("RetCommonCombatStatisticsByTeam", [counters], 2)
        self.assertGreater(self.model.party_session_id, 0)
        self.assertEqual(len(self.model.targetless_team_states), 2)
        self.assertEqual(sorted(state.last_absolute
                                for state in self.model.targetless_team_states.values()),
                         [200, 1000])

    def test_leave_and_new_team_rejects_late_old_member_heartbeat(self):
        self.feed("OnMsgQuitTeam", [], 4)
        self.feed("OnJoinGroupSuccess", [self.roster(NEW_TOKEN, "新队员")], 5)
        self.feed("OnUpdateTeamGroupMemberProps", [TEAMMATE_TOKEN,
                  {"$map": [[4, False], [5, 100], [6, 100]]}], 6)
        self.assertNotIn(TEAMMATE_TOKEN, self.parser.current_team_profile_tokens())
        self.assertNotIn(TEAMMATE_TOKEN, self.model.party_user_tokens)

    def test_other_member_replacement_keeps_session_and_separates_damage(self):
        old_actor = self.start_battle()
        session = self.model.party_session_id
        self.feed("OnMsgOtherQuitTeam", [TEAMMATE_TOKEN], 3)
        self.feed("OnMsgOtherJoinTeamGroup", [0, 0, {"$map": [
            [2, NEW_TOKEN], [5, "新队员"], [8, 1_200_003],
        ]}], 4)
        new_actor = self.parser.token_actors[NEW_TOKEN]
        self.assertNotEqual(new_actor, old_actor)
        counters = {"$map": [
            [SELF_TOKEN, {"$map": [[4, "本人"], [5, 200]]}],
            [NEW_TOKEN, {"$map": [[4, "新队员"], [5, 500]]}],
        ]}
        self.feed("RetCommonCombatStatisticsByTeam", [counters], 5)
        self.assertEqual(self.model.party_session_id, session)
        rows = self.window.live_hud_dps_segment["rows"]
        new_row = next(row for row in rows if row["actor_id"] == new_actor)
        self.assertEqual((new_row["name"], new_row["total_value"]), ("新队员", 500))
        self.assertNotIn(old_actor, {row["actor_id"] for row in rows})
        record = self.model.build_combat_record()
        old_row = next(row for row in record["participants"] if row["actor_id"] == old_actor)
        self.assertEqual((old_row["name"], old_row["damage"]), ("原队员", 1000))


if __name__ == "__main__":
    unittest.main()
