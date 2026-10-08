"""Display-only regressions for result retention, resolved roster and AI labels."""
import copy
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

from encounter_tracker import EncounterTracker
from settlement_presenter import encounter_view
from test_combat_model import (
    DpsWindow,
    CombatModel,
    ActorStats,
    MonsterStats,
    TeamDamageState,
    chinese_error_message,
    normalize_profession_display_metrics,
)


def make_window():
    window = object.__new__(DpsWindow)
    members = set(range(1, 7)) | {-10, -11, -12, -13}
    actors = []
    for actor_id in range(1, 7):
        actor = ActorStats(actor_id=actor_id)
        actor.damage = 700_000 - actor_id * 70_000
        actors.append(actor)
    model = SimpleNamespace(
        self_id=1, started=True, combat_end_time=0, party_active=True, party_member_count=6,
        party_known=True, encounter_member_ids=set(range(1, 7)),
        non_player_actor_ids=set(), entity_names={i: f'队员{i}' for i in range(1, 7)},
        entity_professions={i: 1200001 for i in range(1, 7)},
        entity_extraordinary_ratings={}, entity_ai_states={},
        member_death_counts={}, friend_order=list(members),
    )
    model._encounter_started = lambda: model.started
    model.combat_in_progress = lambda: model.started and not model.combat_end_time
    model._current_member_ids = lambda: set(members)
    model.current_stats = lambda: actors if model.started else []
    model.current_taken_rows = lambda: [dict(actor_id=i, taken=230 if i > 0 else None, source='server_team_counter' if i > 0 else 'unavailable') for i in members]
    model.duration = lambda _now=None: 30
    model.actor_profession_id = lambda actor: model.entity_professions.get(actor, 0)
    model.display_name = lambda actor: model.entity_names.get(actor, '')
    model.current_monster = lambda: None
    window.model = model
    window._shown_actor_name = model.display_name
    window.latest_healing_summary = {'healers': []}
    window.profession_display_metrics = normalize_profession_display_metrics({})
    window.team_rating_preview_enabled = True
    window.hide_names = False
    window._preferred_main_topmost = lambda: True
    window.config = {}
    window.main_recent_battle_expanded = True
    window.team_application_notices = deque()
    return window, members


def completed_record():
    return dict(encounter_id='finished-six-person-battle', ended_at_epoch=1000,
                duration_seconds=30, dps_duration_seconds=30, team_dps=105_000,
                monster=dict(entity_id=90, name='战斗首领', current_hp=0, max_hp=3_000_000),
                participants=[dict(actor_id=i, name=f'队员{i}', is_self=i == 1,
                                   profession_id=1200001, is_ai=i in (3, 4), damage=700_000-i*70_000,
                                   dps=(700_000-i*70_000)/30, taken=230, deaths=0)
                              for i in range(1, 7)], healers=[])


def make_equipment_window(mode="pve"):
    window, members = make_window()
    members.intersection_update({1, 2})
    window.main_combat_mode = mode
    window.model.party_session_id = 7
    window.model.party_member_count = 2
    window.model.self_character_id = "self-token"
    window.model.party_user_tokens = {"peer-token"}
    window.model.actor_character_ids = {1: "self-token", 2: "peer-token"}
    window.model.entity_extraordinary_ratings.update({1: 100_000, 2: 90_000})
    window.model.actor_is_ai = lambda _actor: False
    window.team_equipment_profiles = {
        token: {"user_token": token, "equipment_count": 8,
                "extraordinary_rating": rating, "equipment_snapshot": {"old": True}}
        for token, rating in (("self-token", 100_000), ("peer-token", 90_000))
    }
    window.team_equipment_profile_ratings = {"self-token": 100_000, "peer-token": 90_000}
    window.team_equipment_requested_tokens = {"self-token", "peer-token"}
    window.team_equipment_requested_ratings = {}
    window.team_equipment_requested_at = {}
    submissions = []
    window.worker = SimpleNamespace(
        equipment_session=lambda: (222, 1),
        schedule_equipment_profiles=lambda payload: submissions.append(payload) or True,
    )
    window._invalidate_team_rating_preview_rows = mock.Mock()
    window._schedule_layered_main_render = mock.Mock()
    if mode == "pvp":
        def roster(**options):
            if options.get("side") == "enemy":
                return []
            return [
                dict(user_token=token, actor_id=actor, is_self=actor == 1,
                     name=f"玩家{actor}", profession_id=1_200_001,
                     extraordinary_rating=window.model.entity_extraordinary_ratings[actor])
                for actor, token in ((1, "self-token"), (2, "peer-token"))
                if actor != 1 or options.get("include_self")
            ]
        tracker = SimpleNamespace(
            self_token="self-token", map_id=5208003, generation=1,
            pvp_allies={"peer-token": {}}, pvp_team_members=roster,
        )
        window.pvp_recording = SimpleNamespace(
            active=True, map_id=5208003, display_tracker=tracker,
            recording={"map_id": 5208003, "match_id": "manual-equipment-refresh"},
            snapshot=lambda: {"session_id": "manual-equipment-refresh", "allies": roster(include_self=True)},
        )
    window.team_equipment_context = (222, "self-token", window._team_equipment_party_session_id())
    return window, submissions


def equipment_reply(window, token="peer-token", rating=90_001):
    return dict(
        game_pid=222, capture_session_id=1, local_user_token="self-token",
        party_session_id=window._team_equipment_party_session_id(),
        user_token=token, extraordinary_rating=rating, equipment_count=8,
        pvp_equipment_count=4, active_word_count=3, total_word_count=10,
        batch_id="manual-refresh", equipment_snapshot={"new": True},
    )


def make_official_clock_window():
    window, _ = make_window()
    window.model = CombatModel(run_id="official-hud-clock")
    window.model.ingest_server_clock(1_024, 1_000)
    window._shown_actor_name = window.model.display_name
    window.live_hud_combat_state = None
    window.live_hud_boss_state = None
    window.live_hud_dps_segment = None
    window._schedule_layered_main_render = lambda: None
    return window


def official_clock_snapshot(window, epoch, damages, *, server_time, seconds=0,
                            member_clocks=None):
    timestamp = 116_444_736_000_000_000 + round(epoch * 10_000_000)
    tokens = [f"clock-member-{actor_id}" for actor_id in damages]
    for actor_id, damage in damages.items():
        member_seconds, member_start = (member_clocks or {}).get(
            actor_id, (seconds, server_time)
        )
        window._dispatch_message(
            "team_stat",
            {"filetime_100ns": timestamp, "actor_id": actor_id,
             "user_token": f"clock-member-{actor_id}", "snapshot_tokens": tokens,
             "absolute_damage": damage, "server_time": member_start,
             "combat_seconds_total": member_seconds, "full_snapshot": True,
             "omitted_zero": damage == 0},
        )


class MainHudBehaviorTests(unittest.TestCase):
    def test_binding_live_history_fills_tracker_context_from_local_model(self):
        window, _ = make_window()
        tracker = EncounterTracker()
        encounter = tracker.begin(
            instance_id="npcap-bootstrap:300",
            started_at_ns=100_000_000_000,
            participants_snapshot=[{"id": "self", "iid": 1, "name": "Self"}],
            boss_template_id=7_110_200,
            boss_token="entity:300",
            self_token="self",
        )
        saved = []
        bound = []
        window.model.encounter_id = "local-encounter"
        window.model.encounter_dungeon_id = 5_100_064
        window.model.encounter_map_id = 5_200_224
        window.model.encounter_dungeon_stage_id = 5_150_064
        window.model.encounter_dungeon_stage_phase = 1
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            repository=SimpleNamespace(save=lambda current: saved.append(current)),
        )
        window.settlement_history_adapter = SimpleNamespace(
            bind=lambda local_id, tracked_id: bound.append((local_id, tracked_id)) or True
        )

        self.assertTrue(window._bind_active_settlement_history())
        self.assertEqual(encounter.dungeon_id, 5_100_064)
        self.assertEqual(encounter.map_id, 5_200_224)
        self.assertEqual(encounter.stage_id, 5_150_064)
        self.assertEqual(encounter.stage_index, 1)
        self.assertEqual(encounter.revision, 1)
        self.assertEqual(saved, [tracker])
        self.assertEqual(bound, [("local-encounter", encounter.local_encounter_id)])

    def test_main_combat_mode_switches_without_mutating_pve_state(self):
        window, _ = make_window()
        scheduled = []
        hidden = []
        window.main_combat_mode = "pve"
        window.show_pvp_button = True
        window.main_scroll_offset = 180
        window.layered_main_hover_action = "pvp"
        window._layered_main_active = lambda: True
        window._schedule_layered_main_render = lambda: scheduled.append(True)
        window._hide_enrage_tooltip = lambda: hidden.append(True)
        retained = {"encounter_id": "pve-result"}
        window.main_last_battle_result = retained

        globals_ = window._set_main_combat_mode.__globals__
        original_enabled = globals_["PVP_FEATURE_ENABLED"]
        globals_["PVP_FEATURE_ENABLED"] = True
        self.addCleanup(globals_.__setitem__, "PVP_FEATURE_ENABLED", original_enabled)
        window._set_main_combat_mode("pvp")

        self.assertEqual(window.main_combat_mode, "pvp")
        self.assertEqual(window.main_scroll_offset, 0)
        self.assertEqual(window.layered_main_hover_action, "")
        self.assertIs(window.main_last_battle_result, retained)
        self.assertEqual(len(scheduled), 1)

        window._set_main_combat_mode("pve")

        self.assertEqual(window.main_combat_mode, "pve")
        self.assertIs(window.main_last_battle_result, retained)
        self.assertEqual(len(scheduled), 2)
        self.assertEqual(len(hidden), 2)

    def test_pve_combat_entry_switches_to_recent_battle_only_once(self):
        window, _ = make_window()
        window.main_combat_mode = "pve"
        window.pve_hud_view = "team"
        window.main_scroll_offset = 5
        window.live_hud_combat_state = None
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window._schedule_layered_main_render = lambda: None

        payload = {
            "active": True,
            "segment_id": 1,
            "started_at_epoch": 1_000.0,
        }
        window._dispatch_message("live_hud_combat", payload)

        self.assertEqual(window.pve_hud_view, "recent_battle")
        self.assertEqual(window.main_scroll_offset, 0)

        window._set_pve_hud_view("team")
        window.main_scroll_offset = 7
        window._dispatch_message("live_hud_combat", payload)

        self.assertEqual(window.pve_hud_view, "team")
        self.assertEqual(window.main_scroll_offset, 7)

    def test_pvp_combat_entry_does_not_change_pve_content_view(self):
        window, _ = make_window()
        window.main_combat_mode = "pvp"
        window.pve_hud_view = "team"
        window.live_hud_combat_state = None
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window._schedule_layered_main_render = lambda: None

        window._dispatch_message(
            "live_hud_combat",
            {
                "active": True,
                "segment_id": 1,
                "started_at_epoch": 1_000.0,
            },
        )

        self.assertEqual(window.pve_hud_view, "team")

    def test_disabled_pvp_feature_cannot_change_main_mode(self):
        window, _ = make_window()
        window.main_combat_mode = "pve"
        window.show_pvp_button = True

        globals_ = window._set_main_combat_mode.__globals__
        original_enabled = globals_["PVP_FEATURE_ENABLED"]
        globals_["PVP_FEATURE_ENABLED"] = False
        self.addCleanup(globals_.__setitem__, "PVP_FEATURE_ENABLED", original_enabled)

        window._set_main_combat_mode("pvp")

        self.assertEqual(window.main_combat_mode, "pve")

    def test_pvp_mode_can_be_selected_before_entering_a_pvp_map(self):
        window, _ = make_window()
        window.main_combat_mode = "pve"
        window.show_pvp_button = True
        window._pvp_hud_map_active = lambda: False
        window._layered_main_active = lambda: True
        window._schedule_layered_main_render = lambda: None
        window._hide_enrage_tooltip = lambda: None

        window._set_main_combat_mode("pvp")

        self.assertEqual(window.main_combat_mode, "pvp")

    def test_pvp_hud_switches_live_and_team_without_changing_main_mode(self):
        window, _ = make_window()
        window.main_combat_mode = "pvp"
        window.pvp_hud_view = "live"
        window.pvp_team_offset = 5
        window.layered_main_hover_action = "pvp_team"
        queries = []
        renders = []
        window._schedule_team_equipment_profiles = lambda: queries.append(True)
        window._layered_main_active = lambda: True
        window._schedule_layered_main_render = lambda: renders.append(True)

        window._set_pvp_hud_view("team")

        self.assertEqual(window.main_combat_mode, "pvp")
        self.assertEqual(window.pvp_hud_view, "team")
        self.assertEqual(window.pvp_team_offset, 0)
        self.assertEqual(window.layered_main_hover_action, "")
        self.assertEqual(queries, [True])
        self.assertEqual(renders, [True])

        window._set_pvp_hud_view("live")
        self.assertEqual(window.pvp_hud_view, "live")
        self.assertEqual(renders, [True, True])

    def test_pvp_snapshot_reuses_the_live_self_identity_and_profession(self):
        window, _ = make_window()
        window.current_character_name = "莫雪"
        window.current_character_profession_id = 1_200_004
        window.model.entity_extraordinary_ratings[1] = 80_616
        window.pvp_hud_state = {
            "map_name": "诸王战纪",
            # Presentation state must never replace the live local identity.
            "player_name": "旧角色",
            "rating": 1,
            "profession_id": 1_200_007,
            "outgoing": [
                {"rating": "79,840", "kills": 1},
                {"rating": 88_893, "kills": 2},
                {"rating": 99_999, "kills": 0},
            ],
        }
        window._main_display_rows = lambda: self.fail(
            "PVP identity must not borrow PVE encounter rows"
        )
        window._startup_interaction_blocked = lambda: False
        window.process_is_elevated = True
        window.window_locked = False
        window.layered_main_hover_action = ""

        snapshot = window._pvp_layered_main_snapshot()

        self.assertEqual(snapshot["pvp_player_name"], "莫雪")
        self.assertEqual(snapshot["pvp_rating"], "80616")
        self.assertEqual(snapshot["pvp_profession_id"], 1_200_004)
        self.assertEqual(snapshot["pvp_map_name"], "诸王战纪")
        self.assertEqual(snapshot["pvp_average_kill_rating"], "85875")

    def test_hunter_city_keeps_team_composition_and_local_combat_views(self):
        window, _ = make_window()
        window.pvp_hud_view = "team"
        window._startup_interaction_blocked = lambda: False
        window._pvp_team_composition_rows = lambda: [
            {"name": "队友", "rating_value": 90000},
        ]
        window._pvp_current_teammates = lambda: [{"name": "队友"}]
        state = {
            "session_id": "hunter-1", "kills": 1, "deaths": 1,
            "total_damage": "100", "total_taken": "50",
            "outgoing": [{"name": "敌人甲", "kills": 1, "damage": 100}],
            "incoming": [{"name": "敌人乙", "defeats": 1, "damage": 50}],
            "allies": [{"name": "队友"}], "enemies": [{"name": "敌方全队"}],
        }
        window.pvp_recording = SimpleNamespace(
            display_tracker=SimpleNamespace(map_id=5200167),
            snapshot=lambda: state, active=True,
            recording={"map_id": 5200167}, map_id=5200167,
        )

        snapshot = window._pvp_layered_main_snapshot()

        self.assertTrue(snapshot["pvp_self_only"])
        self.assertEqual(snapshot["pvp_hud_view"], "team")
        self.assertFalse(snapshot["pvp_team_battle"])
        self.assertEqual(snapshot["pvp_allies"], [])
        self.assertEqual(snapshot["pvp_enemies"], [])
        self.assertEqual(snapshot["pvp_teammates"], [])
        self.assertEqual(snapshot["pvp_team_rows"][0]["name"], "队友")
        self.assertEqual(snapshot["pvp_team_average_rating"], "90000")
        window.pvp_hud_view = "live"
        snapshot = window._pvp_layered_main_snapshot()
        self.assertEqual(snapshot["pvp_hud_view"], "live")
        self.assertEqual(snapshot["pvp_team_rows"], [])
        self.assertEqual(snapshot["pvp_outgoing"][0]["name"], "敌人甲")
        self.assertEqual(snapshot["pvp_incoming"][0]["name"], "敌人乙")

    def test_hunter_healer_uses_exact_effective_healing_without_changing_dps(self):
        window, _ = make_window()
        window._startup_interaction_blocked = lambda: False
        window._pvp_team_composition_rows = lambda: []
        window._pvp_current_teammates = lambda: []
        state = {
            "session_id": "hunter-healer", "kills": 2, "deaths": 1,
            "healing": 123456, "total_damage": "200", "total_taken": "50",
            "outgoing": [{"name": "敌人", "kills": 2, "damage": 200}],
        }
        window.pvp_recording = SimpleNamespace(
            display_tracker=SimpleNamespace(map_id=5200167),
            snapshot=lambda: state, active=True,
            recording={"map_id": 5200167}, map_id=5200167,
        )
        window.current_character_profession_id = 1_200_002
        healer = window._pvp_layered_main_snapshot()
        self.assertTrue(healer["pvp_healer"])
        self.assertEqual(healer["pvp_effective_healing"], "123,456")
        self.assertEqual(healer["pvp_deaths"], 1)
        self.assertEqual(healer["pvp_outgoing"][0]["damage"], 200)

        window.current_character_profession_id = 1_200_001
        dps = window._pvp_layered_main_snapshot()
        self.assertFalse(dps["pvp_healer"])
        self.assertEqual(dps["pvp_kills"], 2)
        self.assertEqual(dps["pvp_outgoing"][0]["damage"], 200)

        state["healing"] = None
        window.current_character_profession_id = 1_200_002
        self.assertEqual(
            window._pvp_layered_main_snapshot()["pvp_effective_healing"],
            "未记录",
        )

    def test_hunter_unattributed_deaths_are_visible_without_guessing_killer(self):
        window, _ = make_window()
        window._startup_interaction_blocked = lambda: False
        window._pvp_team_composition_rows = lambda: []
        window._pvp_current_teammates = lambda: []
        window.pvp_recording = SimpleNamespace(
            display_tracker=SimpleNamespace(map_id=5200167),
            snapshot=lambda: {
                "session_id": "hunter-deaths", "deaths": 7,
                "incoming": [{"name": "已确认敌人", "defeats": 2, "damage": 300}],
            },
            active=True, recording={"map_id": 5200167}, map_id=5200167,
        )

        incoming = window._pvp_layered_main_snapshot()["pvp_incoming"]

        self.assertEqual(incoming[0]["name"], "已确认敌人")
        self.assertEqual(incoming[1]["name"], "击败者未识别")
        self.assertEqual(incoming[1]["defeats"], 5)

    def test_pvp_live_team_rows_label_real_rating_ai_and_missing_rating(self):
        window, _ = make_window()
        window.model.entity_extraordinary_ratings[1] = 94_764
        window.pvp_hud_state = {
            "team_size": 6,
            "allies": [
                {"name": "本人", "is_self": True, "is_ai": False,
                 "extraordinary_rating": None},
                {"name": "真人队友", "user_token": "human", "is_ai": False,
                 "extraordinary_rating": 0},
                {"name": "人机队友", "is_ai": True,
                 "extraordinary_rating": 0},
            ],
            "enemies": [
                {"name": "未知敌人", "is_ai": False,
                 "extraordinary_rating": 0},
            ],
        }
        window.pvp_tracker = SimpleNamespace(
            snapshot=lambda: window.pvp_hud_state,
            token_profiles={"human": {"extraordinary_rating": 133_344}},
            pvp_allies={}, pvp_enemies={}, profiles={},
        )
        window._pvp_team_composition_rows = lambda: []
        window._pvp_current_teammates = lambda: []
        window._startup_interaction_blocked = lambda: False

        snapshot = window._pvp_layered_main_snapshot()

        self.assertEqual(
            [row["rating"] for row in snapshot["pvp_allies"]],
            ["94764", "133344", "人机"],
        )
        self.assertEqual(snapshot["pvp_enemies"][0]["rating"], "--")

    def test_pvp_team_skips_bot_equipment_lookup(self):
        window, _ = make_window()
        window._pvp_current_team_members = lambda: [
            {"user_token": "bot", "actor_id": 3, "name": "机器人",
             "is_ai": True},
        ]
        queries = []
        window._equipment_summary_for_actor = lambda actor, token: queries.append((actor, token)) or {
            "equipment_count": 1, "pvp_equipment_count": 0,
        }

        rows = window._pvp_team_composition_rows()

        self.assertEqual(queries, [])
        self.assertEqual(rows[0]["rating"], "人机")
        self.assertFalse(rows[0]["equipment_profile_ready"])

    def test_previous_settled_team_dps_is_hidden_during_new_pull(self):
        window, _ = make_window()
        window.model._is_dummy_encounter = lambda: False
        window._main_display_is_cleared = lambda: False
        window._main_context_display_is_stale = lambda: False
        window.settlement_ui = SimpleNamespace(
            tracker=SimpleNamespace(
                current_id="new-pull",
                encounters={
                    "new-pull": SimpleNamespace(result="IN_PROGRESS"),
                },
            )
        )

        self.assertIsNone(window._main_displayable_team_dps_record())

    def test_live_boss_team_dps_is_separate_from_recent_battle_total(self):
        window, members = make_window()
        members.clear()
        members.add(1)
        window.model.friend_order = [1]
        window.model.party_active = False
        window.model.party_member_count = 1
        window.model.party_session_id = 0
        window.model.actor_character_ids = {1: "self-token"}
        window.model.self_character_id = "self-token"
        window.model.entity_extraordinary_ratings[1] = 107_753
        window.model.duration = lambda _now=None: 19.0
        window.model.observed_boss_damage_taken = lambda: 4_199.0
        boss = MonsterStats(
            entity_id=91,
            template_id=7_100_215,
            name="Current Boss",
            current_hp=22_024_600,
            max_hp=31_387_100,
        )
        window.model.current_monster = lambda: boss
        window.model.current_bosses = lambda: [boss]

        roster = [{
            "id": "self-token",
            "iid": 1,
            "name": "Self",
            "profession_id": 1_200_001,
        }]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="same-instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7_100_215,
            boss_token="previous-boss",
            self_token="self-token",
            party_session_id=0,
        )
        tracker.end("WIPE", 110_000_000_000)
        previous.settlement_status = "SETTLED"
        previous.participants = [
            dict(roster[0], damage=3_095_000, dps=309_500.0)
        ]
        current = tracker.begin(
            instance_id="same-instance",
            started_at_ns=111_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7_100_215,
            boss_token="current-boss",
            self_token="self-token",
            party_session_id=0,
        )
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            instance_id="same-instance",
            session_encounter_ids={
                previous.local_encounter_id,
                current.local_encounter_id,
            },
        )
        window.history_store = SimpleNamespace(load=lambda _key: None)
        rows = [
            {
                "actor_id": 1,
                "profession_id": 1_200_001,
                "name": "Self",
                "metric": "dps",
                "stat_value": 221.0,
                "total_value": 4_199,
                "dps_value": 221.0,
                "is_self": True,
                "is_live_self": True,
            },
            {"row_kind": "section", "section_text": "实时战斗/战斗记录"},
            {
                "actor_id": 1,
                "profession_id": 1_200_001,
                "name": "Self",
                "metric": "dps",
                "stat_value": 309_500.0,
                "total_value": 3_095_000,
                "dps_value": 309_500.0,
                "is_recent_battle": True,
            },
        ]
        window._main_display_rows = lambda: ([dict(row) for row in rows], False)
        window.live_hud_combat_state = {
            "active": True,
            "segment_id": 1,
            "started_at_epoch": 1_000.0,
        }
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window.pve_hud_view = "recent_battle"
        window.team_dps_text = "309,500"

        live_snapshot = window._layered_main_snapshot()
        live_section = next(
            row for row in live_snapshot["rows"]
            if row.get("row_kind") == "section"
        )

        self.assertEqual(live_snapshot["team_dps"], "309,500/s")
        self.assertEqual(live_section["live_team_dps"], "221/s")

        window._main_display_rows = lambda: ([dict(rows[0])], False)
        current_team_snapshot = window._layered_main_snapshot()
        current_team_section = next(
            row for row in current_team_snapshot["rows"]
            if row.get("row_kind") == "section"
        )

        self.assertEqual(current_team_section["section_text"], "实时战斗/战斗记录")
        self.assertEqual(current_team_section["live_team_dps"], "221/s")

        window.show_team_dps = False
        hidden_live_snapshot = window._layered_main_snapshot()
        self.assertFalse(
            any(row.get("live_team_dps") for row in hidden_live_snapshot["rows"])
        )

        window.model.started = False
        window.live_hud_combat_state = None
        window.show_team_dps = True
        window._main_display_rows = lambda: ([dict(row) for row in rows], False)
        ended_snapshot = window._layered_main_snapshot()
        ended_section = next(
            row for row in ended_snapshot["rows"]
            if row.get("row_kind") == "section"
        )

        self.assertEqual(ended_snapshot["team_dps"], "309,500/s")
        self.assertEqual(ended_section["live_team_dps"], "--")

    def test_pvp_unconnected_statistics_use_placeholders(self):
        window, _ = make_window()
        window.current_character_name = "莫雪"
        window.current_character_profession_id = 1_200_004
        window.model.entity_extraordinary_ratings[1] = 80_616
        window.pvp_hud_state = {}
        window._startup_interaction_blocked = lambda: False
        window.process_is_elevated = True
        window.window_locked = False
        window.layered_main_hover_action = ""

        snapshot = window._pvp_layered_main_snapshot()

        self.assertEqual(snapshot["pvp_player_name"], "莫雪")
        self.assertEqual(snapshot["pvp_rating"], "80616")
        self.assertEqual(snapshot["pvp_kills"], "--")
        self.assertEqual(snapshot["pvp_assists"], "--")
        self.assertEqual(snapshot["pvp_deaths"], "--")
        self.assertEqual(snapshot["pvp_total_damage"], "--")
        self.assertEqual(snapshot["pvp_total_taken"], "--")
        self.assertEqual(snapshot["pvp_outgoing"], [])
        self.assertEqual(snapshot["pvp_incoming"], [])

    def test_pvp_hud_shows_damaged_opponents_without_kills(self):
        window, _ = make_window()
        damage_only_outgoing = {
            "opponent_id": "damage-only",
            "name": "only damaged",
            "damage": 1200,
            "kills": 0,
            "share_text": "80.0%",
        }
        damage_only_incoming = {
            "opponent_id": "incoming-only",
            "name": "only hit self",
            "damage": 900,
            "defeats": 0,
        }
        window.pvp_hud_state = {
            "total_damage": "1,500",
            "total_taken": "1,100",
            "outgoing": [
                damage_only_outgoing,
                {
                    "opponent_id": "defeated",
                    "name": "defeated player",
                    "damage": 300,
                    "kills": 1,
                },
            ],
            "incoming": [
                damage_only_incoming,
                {
                    "opponent_id": "killer",
                    "name": "killer player",
                    "damage": 200,
                    "defeats": 1,
                },
            ],
        }
        window._startup_interaction_blocked = lambda: False
        window.process_is_elevated = True
        window.window_locked = False
        window.layered_main_hover_action = ""

        snapshot = window._pvp_layered_main_snapshot()

        self.assertEqual(
            [row["opponent_id"] for row in snapshot["pvp_outgoing"]],
            ["damage-only", "defeated"],
        )
        self.assertEqual(snapshot["pvp_outgoing"][0]["name"], "only damaged")
        self.assertEqual(snapshot["pvp_outgoing"][0]["share_text"], "80.0%")
        self.assertEqual(
            [row["opponent_id"] for row in snapshot["pvp_incoming"]],
            ["killer"],
        )
        self.assertEqual(snapshot["pvp_total_damage"], "1,500")
        self.assertEqual(snapshot["pvp_total_taken"], "1,100")
        self.assertEqual(window.pvp_hud_state["outgoing"][0], damage_only_outgoing)
        self.assertEqual(window.pvp_hud_state["incoming"][0], damage_only_incoming)

    def test_pvp_live_view_still_provides_team_rating_for_shared_footer(self):
        window, _ = make_window()
        window.pvp_hud_view = "live"
        window.pvp_hud_state = {}
        window._pvp_team_composition_rows = lambda: [
            {"rating_value": 88_801, "is_ai": False},
            {"rating_value": 91_199, "is_ai": False},
            {"rating_value": None, "is_ai": False},
            {"rating_value": 999_999, "is_ai": True},
        ]
        window._startup_interaction_blocked = lambda: False
        window.process_is_elevated = True
        window.window_locked = False
        window.layered_main_hover_action = ""

        snapshot = window._pvp_layered_main_snapshot()

        self.assertEqual(snapshot["pvp_hud_view"], "live")
        self.assertEqual(snapshot["pvp_team_rows"], [])
        self.assertEqual(snapshot["pvp_team_average_rating"], "90000")

    def test_capture_fatal_reports_details_and_does_not_restart_forever(self):
        window, _ = make_window()
        scheduled = []
        labels = []
        window.connected = True
        window.game_pid = 222
        window.closing = False
        window.authorization_resetting = False
        window.license_network_paused = False
        window.capture_started = True
        window.capture_restart_after_id = None
        window.startup_identity_pending = True
        window.startup_capture_pending = True
        window.startup_wait_reason = "starting"
        window.startup_wait_started_at = 100.0
        window.heartbeat_worker = None
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(
            configure=lambda **kwargs: labels.append(kwargs["text"])
        )
        window.root = SimpleNamespace(
            after=lambda *args: scheduled.append(args)
        )
        window._show_notice = lambda *args, **kwargs: None
        window._schedule_layered_main_render = lambda: None

        failure = {
            "stage": "built_in_capture_unavailable",
            "details": "Windows IPv6 被动采集无法启动（WinDivert 错误 5）",
        }
        window._dispatch_message("fatal", failure)
        window._handle_capture_worker_stopped(
            {
                "reason": "capture_loop_returned",
                "requested": False,
                "terminal": True,
            }
        )

        self.assertEqual(
            chinese_error_message(failure),
            "连接异常：Windows IPv6 被动采集无法启动（WinDivert 错误 5）",
        )
        self.assertEqual(
            labels,
            ["连接异常：Windows IPv6 被动采集无法启动（WinDivert 错误 5）"],
        )
        self.assertFalse(window.startup_identity_pending)
        self.assertFalse(window.startup_capture_pending)
        self.assertEqual(scheduled, [])

    def test_blocked_capture_driver_reports_actionable_notice(self):
        window, _ = make_window()
        notices = []
        window.closing = False
        window.authorization_resetting = False
        window.license_network_paused = False
        window.capture_started = True
        window.capture_restart_after_id = None
        window.heartbeat_worker = None
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        window._show_notice = lambda *args, **_kwargs: notices.append(args)
        window._schedule_layered_main_render = lambda: None
        failure = {
            "stage": "built_in_capture_unavailable",
            "details": "WinDivert 错误 1275：驱动被系统阻止",
        }

        window._dispatch_message("fatal", failure)
        window._handle_capture_worker_stopped(
            {"reason": "capture_loop_returned", "requested": False, "terminal": True}
        )

        self.assertIn("1275", notices[0][1])
        self.assertIn("安全软件拦截记录", notices[0][1])
        self.assertIsNone(window.capture_restart_after_id)
        self.assertIn("1237", chinese_error_message("Windows IPv6 receive failed (1237)"))

    def test_terminal_capture_stop_submits_automatic_feedback_once(self):
        window, _ = make_window()
        notices = []
        lifecycle_events = []
        feedback_content = object()
        feedback_status_label = object()
        feedback_submit_button = object()
        submit_feedback = mock.Mock(
            return_value=SimpleNamespace(
                accepted=True,
                feedback_id="FB-AUTOMATIC",
                message="反馈已提交。",
            )
        )
        window.connected = True
        window.closing = False
        window.authorization_resetting = False
        window.license_network_paused = False
        window.capture_started = True
        window.capture_restart_after_id = None
        window.capture_restart_attempts = 2
        window.capture_recovery_notice_shown = False
        window.automatic_capture_feedback_attempted = False
        window.automatic_capture_feedback_in_progress = False
        window.startup_identity_pending = True
        window.startup_capture_pending = True
        window.startup_wait_reason = "starting"
        window.startup_wait_started_at = 100.0
        window.current_character_name = "自动反馈角色"
        window.heartbeat_worker = None
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        window._show_notice = lambda *args, **_kwargs: notices.append(args)
        window._schedule_layered_main_render = lambda: None
        window.licensing = SimpleNamespace(submit_feedback=submit_feedback)
        window.worker = SimpleNamespace(
            diagnostic_snapshot=lambda: {
                "stage": "fatal",
                "capture_backend": "windows-hybrid",
            }
        )
        window.feedback_submitting = True
        window.feedback_content = feedback_content
        window.feedback_status_label = feedback_status_label
        window.feedback_submit_button = feedback_submit_button
        failure = {
            "stage": "built_in_capture_unavailable",
            "details": "WinDivert 错误 1275：驱动被系统阻止",
        }
        window.capture_fatal_payload = failure
        stopped = {
            "reason": "capture_loop_returned",
            "requested": False,
            "terminal": True,
        }

        class ImmediateThread:
            def __init__(self, *, target, name, daemon):
                self.target = target

            def start(self):
                self.target()

        globals_ = window._queue_automatic_capture_feedback.__globals__
        with (
            mock.patch.object(globals_["threading"], "Thread", ImmediateThread),
            mock.patch.dict(
                globals_,
                {
                    "write_capture_lifecycle_event": lambda message, **fields: (
                        lifecycle_events.append((message, fields))
                    )
                },
            ),
        ):
            window._handle_capture_worker_stopped(stopped)
            window.capture_started = True
            window.capture_fatal_payload = failure
            window._handle_capture_worker_stopped(stopped)

        submit_feedback.assert_called_once()
        submitted = submit_feedback.call_args.kwargs
        self.assertEqual(submitted["category"], "connection")
        self.assertEqual(submitted["character_name"], "自动反馈角色")
        self.assertIn("自动反馈：采集已停止", submitted["content"])
        self.assertIn("错误码：1275", submitted["content"])
        self.assertEqual(
            submitted["diagnostics"]["automatic_capture_stop"],
            {
                "reason": "capture_loop_returned",
                "stage": "built_in_capture_unavailable",
                "details": "WinDivert 错误 1275：驱动被系统阻止",
                "error_code": 1275,
                "restart_attempts": 2,
            },
        )
        self.assertEqual(
            submitted["diagnostics"]["capture_pipeline"]["stage"], "fatal"
        )
        self.assertTrue(window.automatic_capture_feedback_attempted)
        self.assertFalse(window.automatic_capture_feedback_in_progress)
        self.assertEqual(
            [
                event[0]
                for event in lifecycle_events
                if event[0].startswith("automatic_capture_feedback_")
            ],
            [
                "automatic_capture_feedback_queued",
                "automatic_capture_feedback_completed",
            ],
        )
        self.assertTrue(window.feedback_submitting)
        self.assertIs(window.feedback_content, feedback_content)
        self.assertIs(window.feedback_status_label, feedback_status_label)
        self.assertIs(window.feedback_submit_button, feedback_submit_button)

    def test_intentional_capture_stops_do_not_queue_automatic_feedback(self):
        window, _ = make_window()
        queued = mock.Mock()
        window.capture_started = True
        window.capture_restart_after_id = None
        window.capture_fatal_payload = {
            "details": "WinDivert 错误 1275：驱动被系统阻止"
        }
        window._queue_automatic_capture_feedback = queued
        window.closing = True
        window.authorization_resetting = False
        window.license_network_paused = False

        window._handle_capture_worker_stopped(
            {"reason": "window_close", "requested": True, "terminal": True}
        )
        window.capture_started = True
        window.closing = False
        window.authorization_resetting = True
        window._handle_capture_worker_stopped(
            {"reason": "authorization_reset", "requested": False, "terminal": True}
        )

        queued.assert_not_called()

    def test_transient_capture_resource_error_retries_then_prompts_restart(self):
        window, _ = make_window()
        scheduled = []
        notices = []
        automatic_feedback_attempts = []
        window.connected = True
        window.closing = False
        window.authorization_resetting = False
        window.license_network_paused = False
        window.capture_started = True
        window.capture_restart_after_id = None
        window.capture_restart_attempts = 0
        window.heartbeat_worker = None
        window.startup_identity_pending = True
        window.startup_capture_pending = True
        window.startup_wait_reason = "starting"
        window.startup_wait_started_at = 100.0
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        window.root = SimpleNamespace(
            after=lambda *args: scheduled.append(args) or "after-id"
        )
        window._show_notice = lambda *args, **kwargs: notices.append((args, kwargs))
        window._schedule_layered_main_render = lambda: None
        window._queue_automatic_capture_feedback = (
            lambda *_args: automatic_feedback_attempts.append(
                window.capture_restart_attempts
            )
        )

        failure = {
            "stage": "built_in_capture_unavailable",
            "details": "Windows IPv6 被动采集无法启动（WinDivert 错误 1450）：系统资源不足",
        }
        for _attempt in range(6):
            window.capture_started = True
            window.capture_restart_after_id = None
            window._dispatch_message("fatal", failure)
            window._handle_capture_worker_stopped(
                {
                    "reason": "capture_loop_returned",
                    "requested": False,
                    "terminal": True,
                }
            )

        self.assertEqual(len(scheduled), 5)
        self.assertEqual(len(notices), 1)
        self.assertIn("请重启助手", notices[0][0][1])
        self.assertEqual(automatic_feedback_attempts, [5])

    def test_team_application_notices_overlay_above_heading_for_ten_seconds(self):
        window, _ = make_window()
        window.model.entity_names[1] = "本人"
        window.settlement_ui = SimpleNamespace(tracker=EncounterTracker())

        self.assertTrue(
            window._ingest_team_application(
                {
                    "user_token": "applicant-a",
                    "name": "申请甲",
                    "extraordinary_rating": 63_737,
                }
            )
        )
        rows = window._settlement_main_rows()
        applications = window._team_application_section_rows()

        self.assertEqual(rows[1]["row_kind"], "section")
        self.assertNotIn(
            "team_application", {row.get("row_kind") for row in rows}
        )
        self.assertEqual(
            applications[0]["application_text"],
            "入队申请  申请甲  非凡评分 63737",
        )
        self.assertIn(
            "实时战斗/战斗记录",
            [row.get("section_text") for row in rows],
        )

        window.team_application_notices[0]["expires_at"] = (
            time.monotonic() - 0.01
        )
        expired = window._settlement_main_rows()
        self.assertEqual(expired[1]["section_text"], "实时战斗/战斗记录")
        self.assertEqual(window._team_application_section_rows(), [])

    def test_team_application_notices_have_no_row_limit_and_refresh_duplicate(self):
        window, _ = make_window()
        for index, name in enumerate(("甲", "乙", "丙", "丁")):
            self.assertTrue(
                window._ingest_team_application(
                    {
                        "user_token": f"applicant-{name}",
                        "name": name,
                        "extraordinary_rating": 60_000 + index,
                    },
                    now=100.0 + index,
                )
            )

        rows = window._team_application_section_rows(now=103.5)
        self.assertEqual(
            [row["application_text"] for row in rows],
            [
                "入队申请  甲  非凡评分 60000",
                "入队申请  乙  非凡评分 60001",
                "入队申请  丙  非凡评分 60002",
                "入队申请  丁  非凡评分 60003",
            ],
        )

        window._ingest_team_application(
            {
                "user_token": "applicant-丙",
                "name": "丙",
                "extraordinary_rating": 70_002,
            },
            now=104.0,
        )
        refreshed = window._team_application_section_rows(now=104.5)
        self.assertEqual(
            [row["application_text"] for row in refreshed],
            [
                "入队申请  甲  非凡评分 60000",
                "入队申请  乙  非凡评分 60001",
                "入队申请  丁  非凡评分 60003",
                "入队申请  丙  非凡评分 70002",
            ],
        )
        self.assertEqual(
            window._team_application_section_rows(now=114.1), []
        )

    def test_team_application_notice_respects_hidden_names(self):
        window, _ = make_window()
        window.hide_names = True
        window._ingest_team_application(
            {
                "user_token": "private-applicant",
                "name": "真实名字",
                "extraordinary_rating": 88_888,
            },
            now=200.0,
        )

        rows = window._team_application_section_rows(now=201.0)

        self.assertEqual(
            rows[0]["application_text"],
            "入队申请  申请玩家1  非凡评分 88888",
        )
        self.assertNotIn("真实名字", rows[0]["application_text"])

    def test_team_application_rows_expire_independently_and_compact_upward(self):
        window, _ = make_window()
        for offset, name in enumerate(("甲", "乙", "丙")):
            window._ingest_team_application(
                {
                    "user_token": f"applicant-{name}",
                    "name": name,
                    "extraordinary_rating": 70_000 + offset,
                },
                now=100.0 + offset,
            )

        shifted = window._team_application_section_rows(now=110.5)

        self.assertEqual(
            [row["application_text"] for row in shifted],
            [
                "入队申请  乙  非凡评分 70001",
                "入队申请  丙  非凡评分 70002",
            ],
        )
        self.assertEqual(
            [item["user_token"] for item in window.team_application_notices],
            ["applicant-乙", "applicant-丙"],
        )

    def test_startup_identity_does_not_require_a_cached_rating(self):
        window, _ = make_window()
        window.current_character_name = "队员1"
        window.model.entity_extraordinary_ratings.clear()

        self.assertTrue(window._startup_self_profile_ready())

    def test_startup_gate_explains_identity_and_capture_phases(self):
        window, _ = make_window()
        window.startup_identity_pending = True
        window.startup_capture_pending = True
        window.startup_wait_reason = "starting"

        self.assertTrue(window._startup_interaction_blocked())
        self.assertEqual(
            window._startup_wait_message(),
            "正在连接启动中…",
        )

        window.startup_identity_pending = False
        window.startup_wait_reason = "capture"
        self.assertEqual(
            window._startup_wait_message(),
            "正在连接启动中…",
        )

        window.startup_capture_pending = False
        window.startup_wait_reason = ""
        self.assertFalse(window._startup_interaction_blocked())
        self.assertEqual(window._startup_wait_message(), "")

    def test_initial_connection_failure_is_visible_until_capture_ready(self):
        window, _ = make_window()
        window.connected = False
        window.active_capture_session_id = 0
        window.active_capture_game_pid = 0
        window.startup_identity_pending = False
        window.startup_capture_pending = True
        window.startup_wait_reason = "starting"
        labels = []
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(
            configure=lambda **kwargs: labels.append(kwargs['text'])
        )
        window._schedule_layered_main_render = lambda: None

        window._dispatch_message('capture_waiting', {
            'pid': 123, 'session_id': 1, 'reason': 'initial_state',
        })

        self.assertEqual(window._startup_wait_message(), '采集未就绪，自动重试中')
        self.assertEqual(labels, ['自动重试中'])
        self.assertTrue(window._startup_interaction_blocked())

    def test_only_matching_capture_ready_message_unlocks_startup_gate(self):
        window, _ = make_window()
        window.active_capture_session_id = 5
        window.active_capture_game_pid = 222
        window.startup_identity_pending = False
        window.startup_capture_pending = True
        window.startup_wait_reason = "capture"
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        window._schedule_layered_main_render = lambda: None

        window._dispatch_message(
            "capture_ready", {"pid": 222, "session_id": 4}
        )
        self.assertTrue(window.startup_capture_pending)

        window._dispatch_message(
            "capture_ready", {"pid": 222, "session_id": 5}
        )
        self.assertFalse(window.startup_capture_pending)
        self.assertEqual(window.startup_wait_reason, "")

    def test_transport_resync_keeps_existing_hud_visible(self):
        window, _ = make_window()
        window.connected = True
        window.active_capture_session_id = 5
        window.active_capture_game_pid = 222
        window.startup_identity_pending = False
        window.startup_capture_pending = False
        window.capture_transport_resyncing = False
        window.startup_wait_reason = ""
        labels = []
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(
            configure=lambda **kwargs: labels.append(kwargs["text"])
        )
        window._schedule_layered_main_render = lambda: None

        window._dispatch_message(
            "capture_initializing",
            {"pid": 222, "session_id": 5, "reason": "resynchronizing"},
        )

        self.assertFalse(window.startup_capture_pending)
        self.assertTrue(window.capture_transport_resyncing)
        self.assertEqual(window.startup_wait_reason, "transport_resync")
        self.assertEqual(window._startup_wait_message(), "")
        self.assertEqual(labels, ["场景同步"])

    def test_startup_snapshot_keeps_self_row_and_enables_settings_only(self):
        window, _ = make_window()
        window.current_character_name = "队员1"
        window.model.entity_extraordinary_ratings[1] = 88_893
        window.startup_identity_pending = False
        window.startup_capture_pending = True
        window.startup_wait_reason = "capture"

        snapshot = window._layered_main_snapshot()

        self.assertTrue(snapshot["interaction_blocked"])
        self.assertFalse(snapshot["settings_disabled"])
        self.assertEqual(len(snapshot["rows"]), 2)
        self.assertEqual(snapshot["rows"][0]["actor_id"], 1)
        self.assertEqual(snapshot["rows"][0]["inline_rating_text"], "88893")
        self.assertEqual(
            snapshot["rows"][1]["message_text"],
            "正在连接启动中…",
        )

    def test_failed_connection_shows_pause_not_endless_initialization(self):
        window, _ = make_window()
        window.connected = True
        window.active_capture_session_id = 5
        window.active_capture_game_pid = 222
        window.startup_identity_pending = False
        window.startup_capture_pending = False
        window.capture_transport_resyncing = False
        window.startup_wait_reason = ''
        labels = []
        window.dot = SimpleNamespace(configure=lambda **kwargs: None)
        window.status_label = SimpleNamespace(configure=lambda **kwargs: labels.append(kwargs['text']))
        window._schedule_layered_main_render = lambda: None
        window._dispatch_message('capture_waiting', {'pid': 222, 'session_id': 5, 'reason': 'connection_change'})
        self.assertEqual(labels, ['场景同步'])
        self.assertEqual(window._startup_wait_message(), '')
        self.assertFalse(window.startup_capture_pending)
        self.assertTrue(window.capture_transport_resyncing)
        window._dispatch_message('capture_ready', {'pid': 222, 'session_id': 5})
        self.assertFalse(window.startup_capture_pending)
        self.assertFalse(window.capture_transport_resyncing)

    def test_connected_startup_gate_releases_after_bounded_wait(self):
        window, _ = make_window()
        window.connected = True
        window.active_capture_game_pid = 222
        window.startup_identity_pending = True
        window.startup_capture_pending = True
        window.startup_wait_reason = "game_traffic"
        window.startup_wait_started_at = 100.0
        window.settlement_experiment = None
        labels = []
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(
            configure=lambda **kwargs: labels.append(kwargs["text"])
        )
        window._schedule_layered_main_render = lambda: None

        self.assertFalse(window._release_stalled_startup_gate(now=101.9))
        self.assertTrue(window._startup_interaction_blocked())
        self.assertTrue(window._release_stalled_startup_gate(now=102.0))
        self.assertFalse(window._startup_interaction_blocked())
        self.assertEqual(labels, ["等待游戏数据"])

    def test_identity_only_timeout_keeps_valid_capture_running(self):
        window, _ = make_window()
        window.connected = True
        window.active_capture_game_pid = 222
        window.startup_identity_pending = True
        window.startup_capture_pending = False
        window.startup_wait_reason = "identity"
        window.startup_wait_started_at = 100.0
        window.settlement_experiment = None
        labels = []
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(
            configure=lambda **kwargs: labels.append(kwargs["text"])
        )
        window._schedule_layered_main_render = lambda: None

        self.assertTrue(window._release_stalled_startup_gate(now=112.0))
        self.assertFalse(window.startup_identity_pending)
        self.assertEqual(labels, ["运行中"])

    def test_startup_gate_does_not_hide_missing_game_or_capture_error(self):
        window, _ = make_window()
        window.connected = False
        window.active_capture_game_pid = 0
        window.startup_identity_pending = True
        window.startup_capture_pending = True
        window.startup_wait_reason = "game_not_found"
        window.startup_wait_started_at = 100.0

        self.assertFalse(window._release_stalled_startup_gate(now=500.0))
        self.assertTrue(window._startup_interaction_blocked())

    def test_connection_change_wait_also_releases_without_faking_ready(self):
        window, _ = make_window()
        window.connected = True
        window.active_capture_game_pid = 222
        window.startup_identity_pending = False
        window.startup_capture_pending = True
        window.startup_wait_reason = "connection_change"
        window.startup_wait_started_at = 100.0
        window.settlement_experiment = None
        labels = []
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(
            configure=lambda **kwargs: labels.append(kwargs["text"])
        )
        window._schedule_layered_main_render = lambda: None

        self.assertTrue(window._release_stalled_startup_gate(now=112.0))
        self.assertFalse(window._startup_interaction_blocked())
        self.assertEqual(labels, ["等待游戏数据"])

    def test_same_pid_capture_session_keeps_role_before_marking_connected(self):
        window, _ = make_window()
        window.connected = True
        window.active_capture_session_id = 4
        window.active_capture_game_pid = 222
        window.game_pid = 222
        window.startup_identity_pending = False
        window.startup_capture_pending = False
        window.capture_transport_resyncing = False
        window.team_equipment_profiles = {"peer-token": {"name": "莫雪"}}
        window.team_equipment_requested_tokens = {"peer-token"}
        window.team_equipment_context = (222, "self-token", 1)
        reset_calls = []
        window._reset_for_capture_session_change = lambda: reset_calls.append(True)
        window.heartbeat_worker = None
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        window._schedule_layered_main_render = lambda: None

        window._dispatch_message(
            'connected', {'pid': 222, 'session_id': 5}
        )

        self.assertEqual(reset_calls, [])
        self.assertEqual(window.active_capture_session_id, 5)
        self.assertEqual(window.active_capture_game_pid, 222)
        self.assertTrue(window.connected)
        self.assertFalse(window.startup_capture_pending)
        self.assertTrue(window.capture_transport_resyncing)
        self.assertIn("peer-token", window.team_equipment_profiles)
        self.assertEqual(
            window.team_equipment_requested_tokens, {"peer-token"}
        )

    def test_equipment_queries_once_then_only_queries_new_real_member(self):
        window, _ = make_window()
        window.team_equipment_profiles = {}
        window.team_equipment_requested_tokens = set()
        window.team_equipment_context = None
        window.model.party_active = True
        window.model.party_session_id = 7
        window.model.party_member_count = 3
        window.model.self_character_id = "self-token"
        window.model.party_user_tokens = {"peer-token", "robot-token"}
        window.model.actor_character_ids = {
            1: "self-token",
            2: "peer-token",
            3: "robot-token",
        }
        window.model.entity_names.update(
            {1: "本人", 2: "莫雪", 3: "人机"}
        )
        window.model.actor_is_ai = lambda actor_id: actor_id == 3
        capture_session = [1]
        submissions = []
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, capture_session[0]),
            schedule_equipment_profiles=lambda payload: (
                submissions.append(payload) or True
            ),
        )
        window._invalidate_team_rating_preview_rows = lambda **_options: None

        self.assertTrue(window._schedule_team_equipment_profiles())
        self.assertEqual(
            {
                member["user_token"]
                for submission in submissions
                for member in submission["members"]
            },
            {"self-token", "peer-token"},
        )
        self.assertNotIn(
            "robot-token",
            window.team_equipment_requested_tokens,
        )

        capture_session[0] = 2
        self.assertFalse(window._schedule_team_equipment_profiles())
        self.assertEqual(len(submissions), 2)

        window.model.party_user_tokens.add("new-peer-token")
        window.model.actor_character_ids[4] = "new-peer-token"
        window.model.entity_names[4] = "新队友"
        window.model.party_member_count = 4
        self.assertTrue(window._schedule_team_equipment_profiles())
        self.assertEqual(
            [
                member["user_token"]
                for member in submissions[2]["members"]
            ],
            ["new-peer-token"],
        )

    def test_pve_equipment_skips_cached_projection_before_actor_binding(self):
        window, _ = make_window()
        window.team_equipment_profiles = {}
        window.team_equipment_requested_tokens = set()
        window.team_equipment_context = None
        window.model.party_active = True
        window.model.party_session_id = 7
        window.model.party_member_count = 2
        window.model.self_character_id = "self-token"
        window.model.party_user_tokens = {"projection-token"}
        window.model.actor_character_ids = {1: "self-token"}
        window.model.actor_is_ai = lambda _actor: False
        submissions = []
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, 1),
            team_profile_cache={
                "projection-token": {"name": "卡西利亚斯·投影"},
            },
            schedule_equipment_profiles=lambda payload: (
                submissions.append(payload) or True
            ),
        )
        window._invalidate_team_rating_preview_rows = lambda **_options: None

        self.assertTrue(window._schedule_team_equipment_profiles())
        self.assertEqual(
            [
                member["user_token"]
                for submission in submissions
                for member in submission["members"]
            ],
            ["self-token"],
        )
        self.assertNotIn(
            "projection-token", window.team_equipment_requested_tokens
        )

    def test_late_pve_projection_equipment_response_is_discarded(self):
        window, _ = make_window()
        token = "projection-token"
        window.model.actor_character_ids = {}
        window.model.actor_is_ai = lambda _actor: False
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, 1),
            team_profile_cache={token: {"name": "卡西利亚斯·投影"}},
        )
        window._team_equipment_local_token = lambda: "self-token"
        window._team_equipment_party_session_id = lambda: 7
        window._current_team_equipment_tokens = lambda: {
            "self-token", token,
        }
        window.team_equipment_profiles = {token: {"equipment_count": 1}}
        window.team_equipment_requested_tokens = {token}
        window.team_equipment_profile_ratings = {token: None}
        window.team_equipment_requested_ratings = {token: None}
        window.team_equipment_requested_at = {token: 1.0}
        window.team_equipment_attempts = {token: 1}
        window.team_equipment_attempt_ratings = {token: None}
        invalidated = []
        window._invalidate_team_rating_preview_rows = (
            lambda actor=0, **_options: invalidated.append(actor)
        )
        uploads = []
        window._queue_equipment_snapshot_upload = (
            lambda payload: uploads.append(payload) or True
        )
        payload = {
            "game_pid": 222,
            "capture_session_id": 1,
            "local_user_token": "self-token",
            "party_session_id": 7,
            "user_token": token,
            "name": "卡西利亚斯·投影",
            "equipment_count": 1,
        }

        self.assertFalse(window._ingest_team_equipment_profile(payload))
        self.assertNotIn(token, window.team_equipment_profiles)
        self.assertNotIn(token, window.team_equipment_requested_tokens)
        self.assertNotIn(token, window.team_equipment_attempts)
        self.assertEqual(uploads, [])
        self.assertEqual(invalidated, [0])

    def test_unanswered_equipment_query_is_bounded_until_rating_changes(self):
        window, _ = make_window()
        window.team_equipment_profiles = {}
        window.team_equipment_requested_tokens = set()
        window.team_equipment_context = None
        window.model.party_active = True
        window.model.party_session_id = 7
        window.model.party_member_count = 1
        window.model.self_character_id = 'self-token'
        window.model.party_user_tokens = set()
        window.model.actor_character_ids = {1: 'self-token'}
        window.model.actor_is_ai = lambda _actor: False
        window._invalidate_team_rating_preview_rows = lambda **_options: None
        submissions = []
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, 1),
            schedule_equipment_profiles=lambda payload: (
                submissions.append(payload) or True
            ),
        )

        for attempt in range(3):
            self.assertTrue(window._schedule_team_equipment_profiles())
            window.team_equipment_requested_at['self-token'] = (
                time.monotonic() - 21
            )
        self.assertFalse(window._schedule_team_equipment_profiles())
        self.assertEqual(len(submissions), 3)

        self.assertTrue(window._schedule_team_equipment_profiles(
            extra_attempt_tokens={'self-token'}
        ))
        window.team_equipment_requested_at['self-token'] = (
            time.monotonic() - 21
        )
        self.assertFalse(window._schedule_team_equipment_profiles(
            extra_attempt_tokens={'self-token'}
        ))
        self.assertEqual(len(submissions), 4)

        window.model.entity_extraordinary_ratings[1] = 90_001
        self.assertTrue(window._schedule_team_equipment_profiles())
        self.assertEqual(len(submissions), 5)
        self.assertEqual(window.team_equipment_attempts['self-token'], 1)

    def test_equipment_reply_rating_does_not_invalidate_its_own_snapshot(self):
        window, _ = make_window()
        window.model.party_active = True
        window.model.party_session_id = 7
        window.model.party_member_count = 1
        window.model.self_character_id = 'self-token'
        window.model.party_user_tokens = set()
        window.model.actor_character_ids = {1: 'self-token'}
        window.model.actor_is_ai = lambda _actor: False
        window.team_equipment_profiles = {
            'self-token': {'extraordinary_rating': 90_001, 'equipment_count': 8}
        }
        window.team_equipment_requested_tokens = {'self-token'}
        window.team_equipment_profile_ratings = {'self-token': 90_001}
        window.team_equipment_requested_ratings = {}
        window.team_equipment_attempt_ratings = {'self-token': 90_001}
        window.team_equipment_context = (222, 'self-token', 7)
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, 1),
            schedule_equipment_profiles=lambda _payload: self.fail(
                'unchanged equipment was queried again'
            ),
        )

        self.assertFalse(window._schedule_team_equipment_profiles())
        self.assertIn('self-token', window.team_equipment_profiles)

    def test_restored_roster_rating_keeps_matching_shape_equipment(self):
        window, _ = make_window()
        window.model.party_session_id = 7
        window.model.self_character_id = 'self-token'
        window.model.party_user_tokens = {'peer-token'}
        window.model.actor_character_ids = {1: 'self-token', 2: 'peer-token'}
        window.model.entity_extraordinary_ratings.update({1: 110914, 2: 133215})
        window.model.actor_is_ai = lambda _actor: False
        window.team_equipment_profiles = {
            'self-token': {'extraordinary_rating': 110914, 'equipment_count': 8},
            'peer-token': {'extraordinary_rating': 133215, 'equipment_count': 8},
        }
        # The request used a transient roster rating, while its shape reply
        # already returned the rating now restored by the next roster update.
        window.team_equipment_profile_ratings = {'self-token': 110914, 'peer-token': 133106}
        window.team_equipment_context = (222, 'self-token', 7)
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, 1),
            schedule_equipment_profiles=lambda _payload: self.fail('matching equipment was queried again'),
        )

        self.assertFalse(window._schedule_team_equipment_profiles())
        self.assertEqual(window.team_equipment_profiles['peer-token']['equipment_count'], 8)
        self.assertEqual(window.team_equipment_profile_ratings['peer-token'], 133215)

    def test_entering_team_keeps_current_character_equipment(self):
        window, _ = make_window()
        window.model.party_active = True
        window.model.party_session_id = 8
        window.model.party_member_count = 2
        window.model.self_character_id = "self-token"
        window.model.party_user_tokens = {"new-peer-token"}
        window.model.actor_character_ids = {
            1: "self-token", 2: "new-peer-token",
        }
        window.model.entity_names.update({1: "本人", 2: "新队友"})
        window.model.entity_extraordinary_ratings[1] = 100_475
        window.model.actor_is_ai = lambda _actor_id: False
        window.team_equipment_profiles = {
            "self-token": {"equipment_count": 8},
            "old-peer-token": {"equipment_count": 8},
        }
        window.team_equipment_profile_ratings = {
            "self-token": 100_475,
            "old-peer-token": 90_000,
        }
        window.team_equipment_requested_tokens = {
            "self-token", "old-peer-token",
        }
        window.team_equipment_context = (222, "self-token", 7)
        submissions = []
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, 1),
            schedule_equipment_profiles=lambda payload: (
                submissions.append(payload) or True
            ),
        )
        window._invalidate_team_rating_preview_rows = lambda **_options: None

        self.assertTrue(window._schedule_team_equipment_profiles())
        self.assertEqual(window.team_equipment_context, (222, "self-token", 8))
        self.assertIn("self-token", window.team_equipment_profiles)
        self.assertNotIn("old-peer-token", window.team_equipment_profiles)
        self.assertEqual(
            [row["user_token"] for row in submissions[0]["members"]],
            ["new-peer-token"],
        )

        window.model.self_character_id = "other-self-token"
        window.model.actor_character_ids[1] = "other-self-token"
        window._schedule_team_equipment_profiles()
        self.assertNotIn("self-token", window.team_equipment_profiles)

    def test_party_session_change_keeps_unchanged_member_equipment(self):
        window, _ = make_window()
        window.model.party_active = True
        window.model.party_session_id = 8
        window.model.party_member_count = 3
        window.model.self_character_id = "self-token"
        window.model.party_user_tokens = {"peer-token", "new-peer-token"}
        window.model.actor_character_ids = {
            1: "self-token",
            2: "peer-token",
            3: "new-peer-token",
        }
        window.model.entity_names.update(
            {1: "Self", 2: "Peer", 3: "New Peer"}
        )
        window.model.entity_extraordinary_ratings.update(
            {1: 100_475, 2: 90_001, 3: 88_000}
        )
        window.model.actor_is_ai = lambda _actor_id: False
        window.team_equipment_profiles = {
            "self-token": {"equipment_count": 8},
            "peer-token": {"equipment_count": 8},
        }
        window.team_equipment_profile_ratings = {
            "self-token": 100_475,
            "peer-token": 90_001,
        }
        window.team_equipment_requested_tokens = {
            "self-token",
            "peer-token",
        }
        window.team_equipment_context = (222, "self-token", 7)
        submissions = []
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, 1),
            schedule_equipment_profiles=lambda payload: (
                submissions.append(payload) or True
            ),
        )
        window._invalidate_team_rating_preview_rows = lambda **_options: None

        self.assertTrue(window._schedule_team_equipment_profiles())
        self.assertEqual(
            set(window.team_equipment_profiles),
            {"self-token", "peer-token"},
        )
        self.assertEqual(
            [row["user_token"] for row in submissions[0]["members"]],
            ["new-peer-token"],
        )

    def test_rating_change_waits_for_manual_refresh_in_pve_and_pvp(self):
        for mode in ("pve", "pvp"):
            with self.subTest(mode=mode):
                window, submissions = make_equipment_window(mode)
                old_profiles = copy.deepcopy(window.team_equipment_profiles)
                window.model.entity_extraordinary_ratings[2] += 1
                for _ in range(3):
                    self.assertFalse(window._schedule_team_equipment_profiles())
                self.assertEqual(submissions, [])
                self.assertEqual(window.team_equipment_profiles, old_profiles)
                self.assertEqual(window._equipment_refresh_fields(1), {})
                self.assertEqual(window._equipment_refresh_fields(2)["equipment_refresh_token"], "peer-token")
                if mode == "pvp":
                    rows = window._pvp_layered_main_snapshot()["pvp_team_rows"]
                else:
                    rows = window._layered_main_snapshot()["rows"]
                peer = next(row for row in rows if row.get("actor_id") == 2)
                self.assertEqual(peer["equipment_refresh_token"], "peer-token")

                self.assertTrue(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
                self.assertEqual([row["user_token"] for row in submissions[0]["members"]], ["peer-token"])
                self.assertTrue(window._equipment_refresh_fields(2)["equipment_refresh_pending"])
                self.assertFalse(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
                self.assertEqual(len(submissions), 1)
                self.assertTrue(window._ingest_team_equipment_profile(equipment_reply(window)))
                self.assertEqual(window._equipment_refresh_fields(2), {})
                self.assertEqual(window.team_equipment_profiles["peer-token"]["equipment_snapshot"], {"new": True})
                self.assertEqual(window.team_equipment_profiles["self-token"], old_profiles["self-token"])
                self.assertFalse(window._schedule_team_equipment_profiles())

    def test_self_rating_change_refreshes_automatically_without_button(self):
        for mode in ("pve", "pvp"):
            with self.subTest(mode=mode):
                window, submissions = make_equipment_window(mode)
                window.model.entity_extraordinary_ratings[1] += 1

                self.assertEqual(window._equipment_refresh_fields(1), {})
                self.assertTrue(window._schedule_team_equipment_profiles())
                self.assertEqual(
                    [row["user_token"] for row in submissions[0]["members"]],
                    ["self-token"],
                )
                self.assertEqual(window._equipment_refresh_fields(1), {})
                self.assertFalse(window._schedule_team_equipment_profiles())

    def test_manual_equipment_timeout_preserves_snapshot_and_never_retries_automatically(self):
        for mode in ("pve", "pvp"):
            with self.subTest(mode=mode):
                window, submissions = make_equipment_window(mode)
                old_profile = copy.deepcopy(window.team_equipment_profiles["peer-token"])
                window.model.entity_extraordinary_ratings[2] += 1
                self.assertTrue(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
                window.team_equipment_requested_at["peer-token"] = time.monotonic() - 21
                self.assertFalse(window._schedule_team_equipment_profiles())
                self.assertEqual(len(submissions), 1)
                self.assertEqual(window.team_equipment_profiles["peer-token"], old_profile)
                self.assertFalse(window._equipment_refresh_fields(2)["equipment_refresh_pending"])
                self.assertTrue(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
                self.assertEqual(len(submissions), 2)

    def test_failed_manual_equipment_submission_can_be_clicked_again(self):
        window, submissions = make_equipment_window()
        window.model.entity_extraordinary_ratings[2] += 1
        window.worker.schedule_equipment_profiles = mock.Mock(return_value=False)
        self.assertFalse(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
        self.assertFalse(window._equipment_refresh_fields(2)["equipment_refresh_pending"])
        window.worker.schedule_equipment_profiles.return_value = True
        self.assertTrue(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
        self.assertEqual(window.worker.schedule_equipment_profiles.call_count, 2)

    def test_rating_changes_during_manual_query_require_another_click(self):
        window, submissions = make_equipment_window("pvp")
        window.model.entity_extraordinary_ratings[2] = 90_001
        self.assertTrue(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
        window.model.entity_extraordinary_ratings[2] = 90_002
        self.assertFalse(window._schedule_team_equipment_profiles())
        self.assertTrue(window._equipment_refresh_fields(2)["equipment_refresh_pending"])
        self.assertEqual(window.team_equipment_requested_ratings["peer-token"], 90_001)
        self.assertTrue(window._ingest_team_equipment_profile(equipment_reply(window)))
        self.assertFalse(window._equipment_refresh_fields(2)["equipment_refresh_pending"])
        self.assertFalse(window._schedule_team_equipment_profiles())
        self.assertEqual(len(submissions), 1)
        self.assertTrue(window._schedule_team_equipment_profiles(refresh_token="peer-token"))

    def test_map_context_change_rebaselines_successful_equipment_rating(self):
        for mode in ("pve", "pvp"):
            with self.subTest(mode=mode):
                window, submissions = make_equipment_window(mode)
                window.model.entity_extraordinary_ratings[2] = 90_001
                window.team_equipment_context = (222, "self-token", 8)
                self.assertFalse(window._schedule_team_equipment_profiles())
                self.assertEqual(window.team_equipment_profile_ratings["peer-token"], 90_001)
                self.assertEqual(window._equipment_refresh_fields(2), {})
                window.model.entity_extraordinary_ratings[2] = 90_002
                self.assertIn("equipment_refresh_token", window._equipment_refresh_fields(2))
                window.model.entity_extraordinary_ratings[2] = 90_001
                self.assertEqual(window._equipment_refresh_fields(2), {})
                self.assertFalse(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
                self.assertFalse(window._schedule_team_equipment_profiles(refresh_token="departed-token"))
                self.assertEqual(submissions, [])

    def test_capture_session_change_waits_for_new_map_rating_before_refresh(self):
        for mode in ("pve", "pvp"):
            with self.subTest(mode=mode):
                window, submissions = make_equipment_window(mode)
                window.team_equipment_profile_capture_sessions = {
                    "self-token": 1,
                    "peer-token": 1,
                }
                window.active_capture_session_id = 2
                window.model.entity_extraordinary_ratings[1] = 100_224

                self.assertEqual(window._equipment_refresh_fields(1), {})
                self.assertFalse(
                    window._rebaseline_team_equipment_rating_after_capture_change(
                        {
                            "entity_id": 1,
                            "extraordinary_rating": 100_224,
                            "source_method": "PeriodicLiveTeamRatingSnapshot",
                        }
                    )
                )
                self.assertTrue(
                    window._rebaseline_team_equipment_rating_after_capture_change(
                        {
                            "entity_id": 1,
                            "extraordinary_rating": 100_224,
                            "source_method": "OnJoinGroupSuccess",
                        }
                    )
                )
                self.assertEqual(
                    window.team_equipment_profile_ratings["self-token"],
                    100_224,
                )
                self.assertEqual(window._equipment_refresh_fields(1), {})
                self.assertEqual(submissions, [])

                window.model.entity_extraordinary_ratings[1] = 100_225
                self.assertEqual(window._equipment_refresh_fields(1), {})
                self.assertTrue(window._schedule_team_equipment_profiles())
                self.assertEqual(
                    [row["user_token"] for row in submissions[0]["members"]],
                    ["self-token"],
                )

    def test_pvp_manual_refresh_uses_stable_token_before_actor_binding(self):
        window, submissions = make_equipment_window("pvp")
        window.model.actor_character_ids.pop(2)
        window.model.entity_extraordinary_ratings[2] = 90_001
        window.pvp_recording.display_tracker.pvp_team_members = lambda **options: [] if options.get("side") == "enemy" else [
            {"user_token": "self-token", "actor_id": 1, "is_self": True, "extraordinary_rating": 100_000},
            {"user_token": "peer-token", "actor_id": 0, "extraordinary_rating": 90_001},
        ]
        self.assertFalse(window._schedule_team_equipment_profiles())
        row = next(row for row in window._pvp_layered_main_snapshot()["pvp_team_rows"] if row["user_token"] == "peer-token")
        self.assertEqual(row["equipment_refresh_token"], "peer-token")
        self.assertTrue(window._schedule_team_equipment_profiles(refresh_token="peer-token"))
        self.assertEqual(submissions[0]["members"][0]["actor_id"], 0)

    def test_large_roster_equipment_queries_are_batched_without_dropping_members(self):
        window, _members = make_window()
        roster_size = 25
        window.team_equipment_profiles = {}
        window.team_equipment_requested_tokens = set()
        window.team_equipment_profile_ratings = {}
        window.team_equipment_requested_ratings = {}
        window.team_equipment_context = None
        tokens = {f"token-{index:02d}" for index in range(roster_size)}
        actor_tokens = {
            index + 1: f"token-{index:02d}" for index in range(roster_size)
        }
        window.model.party_active = True
        window.model.party_session_id = 11
        window.model.party_member_count = roster_size
        window.model.self_character_id = "token-00"
        window.model.party_user_tokens = set(tokens)
        window.model.actor_character_ids = actor_tokens
        window.model.entity_names = {
            index + 1: f"队员{index + 1}" for index in range(roster_size)
        }
        window.model.actor_is_ai = lambda _actor_id: False
        window._invalidate_team_rating_preview_rows = lambda **_options: None
        submissions = []
        window.worker = SimpleNamespace(
            equipment_session=lambda: (222, 3),
            schedule_equipment_profiles=lambda payload: (
                submissions.append(payload) or True
            ),
        )

        self.assertTrue(window._schedule_team_equipment_profiles())
        self.assertEqual(len(submissions), roster_size)
        self.assertTrue(
            all(len(batch["members"]) == 1 for batch in submissions)
        )
        submitted = [
            member["user_token"]
            for batch in submissions
            for member in batch["members"]
        ]
        self.assertEqual(set(submitted), tokens)
        self.assertEqual(len(submitted), len(set(submitted)))
        self.assertEqual(window.team_equipment_requested_tokens, tokens)

    def test_pvp_team_composition_does_not_cap_the_resolved_roster(self):
        window, members = make_window()
        members.update(range(7, 18))
        for actor_id in range(7, 18):
            window.model.entity_names[actor_id] = f"队员{actor_id}"
            window.model.entity_professions[actor_id] = 1_200_001
            window.model.entity_extraordinary_ratings[actor_id] = 80_000 + actor_id
        window.model.actor_character_ids = {
            actor_id: f"token-{actor_id}" for actor_id in members
        }
        window.team_equipment_profiles = {
            f"token-{actor_id}": {
                "equipment_count": 8,
                "pvp_equipment_count": 8,
                "active_word_count": 3,
                "total_word_count": 10,
            }
            for actor_id in members
        }

        rows = window._pvp_team_composition_rows()

        self.assertEqual(len(rows), 17)
        self.assertEqual(rows[0]["actor_id"], 1)
        self.assertTrue(all(row["equipment_profile_ready"] for row in rows))

    def test_hunter_live_team_excludes_departed_member_without_mutating_match(self):
        window, _ = make_window()
        members = [
            {"user_token": "self-token", "actor_id": 1, "name": "本人", "is_self": True},
            {"user_token": "current-token", "actor_id": 2, "name": "仍在队伍"},
            {"user_token": "departed-token", "actor_id": 3, "name": "夜柯"},
        ]
        tracker = SimpleNamespace(
            self_token="self-token",
            pvp_allies={row["user_token"]: row for row in members[1:]},
            pvp_team_members=lambda **options: (
                members if options.get("include_self") else members[1:]
            ),
        )
        window.model.actor_character_ids = {
            1: "self-token", 2: "current-token", 3: "departed-token",
        }
        window.pvp_recording = SimpleNamespace(
            party_seen=True, party_active=True,
            party_tokens={"self-token", "current-token"},
            active=True, duel_recording=None,
            recording={"map_id": 5200167, "party_observed_in_match": True},
            display_tracker=tracker, map_id=5200167,
        )

        rows = window._pvp_team_composition_rows()
        teammates = window._pvp_current_teammates()

        self.assertEqual([row["name"] for row in rows], ["本人", "仍在队伍"])
        self.assertEqual([row["name"] for row in teammates], ["仍在队伍"])
        self.assertIn("departed-token", tracker.pvp_allies)

    def test_hunter_teammates_and_team_composition_share_fresh_party(self):
        window, _ = make_window()
        window.model.party_ids = {1, 2, 3}
        window.model.provisional_party_ids = set()
        window.model.actor_character_ids = {
            1: "self-token", 2: "ally-two", 3: "ally-three", 4: "old-member",
        }
        tracker = SimpleNamespace(
            self_token="self-token",
            pvp_allies={},
            pvp_team_members=lambda **options: (
                [{"user_token": "self-token", "actor_id": 1,
                  "name": "Self", "is_self": True}]
                if options.get("include_self") else []
            ),
        )
        window.pvp_recording = SimpleNamespace(
            party_seen=True, party_active=True, active=True,
            party_tokens={"self-token", "ally-two", "ally-three"},
            recording={"map_id": 5200167, "party_observed_in_match": True},
            display_tracker=tracker, map_id=5200167,
        )

        composition = window._pvp_team_composition_rows()
        teammates = window._pvp_current_teammates()

        self.assertEqual(
            {row["actor_id"] for row in composition if not row["is_self"]},
            {row["actor_id"] for row in teammates},
        )
        self.assertEqual({row["actor_id"] for row in teammates}, {2, 3})

    def test_pvp_footer_action_survives_transparent_release_region_miss(self):
        window, _ = make_window()
        window.main_combat_mode = "pvp"
        window.window_locked = False
        window.layered_main_dragged = False
        window.layered_main_resize_origin = None
        window.layered_main_scroll_drag_origin = None
        window.layered_main_pvp_scroll_drag_origin = None
        window.layered_main_press_action = "pve"
        window.layered_main_press_origin = (10, 10)
        window._layered_main_active = lambda: True
        window._startup_interaction_blocked = lambda: False
        window._layered_main_region_at = lambda _x, _y: ""
        window._set_main_combat_mode = lambda mode: setattr(
            window, "main_combat_mode", mode
        )
        window._schedule_layered_main_render = lambda: None

        window._layered_main_release(
            SimpleNamespace(x=999, y=999, x_root=999, y_root=999)
        )

        self.assertEqual(window.main_combat_mode, "pve")

    def test_large_pvp_team_scroll_uses_last_painted_count(self):
        window, _ = make_window()
        window.main_combat_mode = "pvp"
        window.pvp_hud_view = "team"
        window.pvp_team_row_count = 30
        window.pvp_team_offset = 0
        window._startup_interaction_blocked = lambda: False
        window._layered_main_active = lambda: True
        window._layered_main_region_at = lambda _x, _y: "scroll:pvp_team"
        window._pvp_layered_main_snapshot = lambda: self.fail(
            "wheel event rebuilt the complete PVP snapshot"
        )
        renders = []
        window._schedule_layered_main_render = lambda: renders.append(True)

        window._scroll_main(SimpleNamespace(delta=-120, num=0, x=1, y=1))

        self.assertEqual(window.pvp_team_offset, 1)
        self.assertEqual(renders, [True])
        window.layered_main_hit_regions = {"scroll:pvp_team": (0, 0, 20, 100)}
        for region in ("action:refresh_equipment:peer-token", "equipment_refresh_pending:peer-token"):
            window._layered_main_region_at = lambda _x, _y, selected=region: selected
            window._scroll_main(SimpleNamespace(delta=-120, num=0, x=1, y=1))
        self.assertEqual(window.pvp_team_offset, 3)
        window.main_visible_rows = 16
        window._scroll_main(SimpleNamespace(delta=-3600, num=0, x=1, y=1))
        self.assertEqual(window.pvp_team_offset, 20)

    def test_victory_upload_is_queued_with_character_without_manual_button(self):
        window, _ = make_window()
        callbacks = []
        uploads = []
        window.closing = False
        window.automatic_upload_queued = set()
        window.history_upload_in_progress = set()
        window.root = SimpleNamespace(
            after=lambda _delay, callback: callbacks.append(callback)
        )
        window._history_upload_state = lambda _battle_id: {"state": "pending"}
        window._start_history_upload = (
            lambda battle_id, mode, **options: uploads.append(
                (battle_id, mode, options)
            )
        )
        victory = {
            "encounter_id": "victory-one",
            "result": "defeated",
            "completion_confirmed": True,
            "boss_name": "副本首领",
            "boss_template_id": 7_100_210,
        }

        self.assertTrue(window._queue_automatic_victory_upload(victory))
        self.assertFalse(window._queue_automatic_victory_upload(victory))
        callbacks[0]()

        self.assertEqual(
            uploads,
            [("victory-one", "character", {"silent": True})],
        )

    def test_different_game_pid_resets_old_role_before_marking_connected(self):
        window, _ = make_window()
        window.active_capture_session_id = 4
        window.active_capture_game_pid = 222
        window.game_pid = 0
        reset_calls = []
        window._reset_for_capture_session_change = lambda: reset_calls.append(True)
        window.heartbeat_worker = None
        window.dot = SimpleNamespace(configure=lambda **_kwargs: None)
        window.status_label = SimpleNamespace(configure=lambda **_kwargs: None)

        window._dispatch_message(
            'connected', {'pid': 333, 'session_id': 5}
        )

        self.assertEqual(reset_calls, [True])
        self.assertEqual(window.active_capture_session_id, 5)
        self.assertEqual(window.active_capture_game_pid, 333)
        self.assertTrue(window.connected)

    def test_capture_session_reset_clears_role_boss_dummy_and_rating_state(self):
        window, _ = make_window()
        reset_options = []
        window.model.reset = lambda **options: reset_options.append(options)
        window.model.entity_names = {1: '旧角色'}
        window.model.entity_professions = {1: 1200001}
        window.model.entity_extraordinary_ratings = {1: 88893}
        window.model.entity_ai_states = {1: False}
        window.model.local_player_name = '旧角色'
        window.model.scene_id = 77
        window.model.current_dungeon_id = 10
        window.model.current_dungeon_stage_id = 20
        window.model.current_dungeon_stage_phase = 1
        window.model.current_dungeon_context_filetime = 123
        window.model.encounter_dungeon_id = 10
        window.model.encounter_dungeon_stage_id = 20
        window.model.encounter_dungeon_stage_phase = 1
        window.model.encounter_dungeon_context_filetime = 123
        window.model.closed_encounter_target_ids = {900}
        window.model.awaiting_post_exit_context = True
        settlement_resets = []
        window.settlement_ui = SimpleNamespace(
            tracker=EncounterTracker(),
            reset_capture_session=lambda _timestamp_ns=None: settlement_resets.append(True),
        )
        synced = []
        window.settlement_history_adapter = SimpleNamespace(
            sync=lambda tracker: synced.append(tracker)
        )
        window.current_character_id = 'old-token'
        window.current_character_name = '旧角色'
        window.current_character_profession_id = 1200001
        window.upload_profile = object()
        window.profile_resolved_character_id = 'old-token'
        window.profile_resolve_in_progress = True
        window.pending_clock_records = {'old': ({}, 1.0, False)}
        window.main_last_battle_result = completed_record()
        window.main_last_live_display_snapshot = {'encounter_id': 'old'}
        window.main_scroll_offset = 300
        window.main_time_text = '01:00'
        window.team_dps_text = '100,000'
        window.main_recent_battle_expanded = True
        window.enrage_prediction = object()
        window.enrage_prediction_visible = True
        invalidated = []
        flushed = []
        painted = []
        window._invalidate_team_rating_preview_rows = (
            lambda **options: invalidated.append(options)
        )
        window._flush_combat_history = (
            lambda **options: flushed.append(options)
        )
        window._schedule_layered_main_render = lambda: painted.append(True)

        window._reset_for_capture_session_change()

        self.assertEqual(settlement_resets, [True])
        self.assertEqual(len(synced), 1)
        self.assertEqual(
            reset_options,
            [dict(
                keep_identity=False,
                keep_monsters=False,
                archive_reason='capture_session_changed',
            )],
        )
        self.assertFalse(window.model.entity_names)
        self.assertFalse(window.model.entity_extraordinary_ratings)
        self.assertIsNone(window.model.scene_id)
        self.assertFalse(window.model.closed_encounter_target_ids)
        self.assertEqual(window.current_character_id, '')
        self.assertFalse(window.pending_clock_records)
        self.assertIsNone(window.main_last_battle_result)
        self.assertTrue(window.main_recent_battle_expanded)
        self.assertEqual(invalidated, [dict(clear_profiles=True)])
        self.assertEqual(flushed, [dict(force=True)])
        self.assertEqual(painted, [True])

    def test_unproven_identity_reset_keeps_live_pve_session(self):
        window, _ = make_window()
        old_token = "AQAAAOwNKLYHAAAA"
        new_token = "AQAAAOwNkGB8AAAA"
        window.current_character_id = old_token
        window.capture_transport_resyncing = False
        resets = []
        window._reset_for_capture_session_change = (
            lambda timestamp_ns=None: resets.append(timestamp_ns)
        )
        timestamp_ns = 1_790_646_014_998_312_600

        window._dispatch_message(
            "identity_session_reset",
            {
                "capture_timestamp_ns": timestamp_ns,
                "previous_user_token": old_token,
                "user_token": "",
                "reason": "authoritative_local_actor_changed",
            },
        )

        self.assertEqual(resets, [])
        self.assertTrue(window.capture_transport_resyncing)

        window._dispatch_message(
            "identity_session_reset",
            {
                "capture_timestamp_ns": timestamp_ns + 1,
                "previous_user_token": old_token,
                "user_token": new_token,
                "reason": "wire_local_token_changed",
            },
        )

        self.assertEqual(resets, [timestamp_ns + 1])

    def test_recent_heading_is_not_an_action_and_settings_still_works(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: True
        window.layered_main_dragged = False
        window.layered_main_resize_origin = None
        window.main_scroll_offset = 200
        paints = []
        settings = []
        window._schedule_layered_main_render = lambda: paints.append(True)
        window.show_settings = lambda: settings.append(True)
        event = SimpleNamespace(x=10, y=10, x_root=10, y_root=10)

        window.layered_main_press_action = 'settings'
        window.layered_main_press_origin = (10, 10)
        window._layered_main_region_at = lambda _x, _y: 'action:settings'
        window._layered_main_release(event)
        self.assertEqual(settings, [True])

    def test_web_database_action_opens_default_browser_without_arming_drag(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: True
        window._startup_interaction_blocked = lambda: False
        window._layered_main_region_at = lambda _x, _y: "action:web_database"
        event = SimpleNamespace(x=10, y=10, x_root=10, y_root=10)
        browser = SimpleNamespace(open=mock.Mock(return_value=True))

        window._layered_main_press(event)
        self.assertFalse(window.layered_main_drag_armed)
        with mock.patch.dict(
            DpsWindow._layered_main_release.__globals__, {"webbrowser": browser}
        ):
            window._layered_main_release(event)

        browser.open.assert_called_once_with(
            "https://gmzz.daodaogame.vip/", new=2
        )

    def test_stale_layered_clear_region_cannot_clear_after_combat_starts(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: True
        window.layered_main_dragged = False
        window.layered_main_resize_origin = None
        window.layered_main_press_action = 'clear'
        window.layered_main_press_origin = (10, 10)
        window._layered_main_region_at = lambda _x, _y: 'action:clear'
        window._main_clear_blocked = lambda: True
        clears = []
        window._clear_main_display = lambda: clears.append(True)

        window._layered_main_release(
            SimpleNamespace(x=10, y=10, x_root=10, y_root=10)
        )

        self.assertEqual(clears, [])

    def test_layered_recent_rows_scroll_from_section_and_scrollbar_drag(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: True
        window._main_display_rows = lambda: ([{} for _ in range(10)], False)
        window._layered_main_snapshot = lambda: {
            "rows": [{} for _ in range(10)], "visible_rows": 3,
        }
        window._main_row_height = lambda: 10
        window.layered_main_visible_rows = 3
        window.layered_main_actor_regions = ()
        window.layered_main_hit_regions = {
            "scrollbar:rows": (280, 100, 306, 400),
            "scroll:rows": (0, 100, 306, 400),
        }
        window.main_scroll_offset = 0
        paints = []
        window._schedule_layered_main_render = lambda: paints.append(True)

        # The accordion heading is not an actor row, but its viewport still
        # accepts the wheel.
        wheel = SimpleNamespace(x=120, y=160, delta=-120, num=0)
        window._scroll_main(wheel)
        self.assertEqual(window.main_scroll_offset, 10)

        press = SimpleNamespace(
            x=290, y=145, x_root=290, y_root=145
        )
        window._layered_main_press(press)
        initial = window.main_scroll_offset
        window._layered_main_drag(
            SimpleNamespace(x=290, y=295, x_root=290, y_root=295)
        )
        self.assertGreater(window.main_scroll_offset, initial)
        window._layered_main_release(
            SimpleNamespace(x=290, y=295, x_root=290, y_root=295)
        )
        self.assertIsNone(window.layered_main_scroll_drag_origin)
        self.assertTrue(paints)

    def test_layered_player_rows_allow_deliberate_window_drag(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: True
        window.layered_main_hit_regions = {}
        window.drag_state = {}
        window.restore_geometry = {}
        window._flush_window_geometry = lambda _window: None
        window.root = SimpleNamespace(winfo_x=lambda: 100, winfo_y=lambda: 100)
        moves = []
        window._drag_move = lambda event, _window: moves.append(
            (event.x_root, event.y_root)
        )

        window._layered_main_press(
            SimpleNamespace(x=120, y=160, x_root=220, y_root=260)
        )
        window._layered_main_drag(
            SimpleNamespace(x=150, y=190, x_root=250, y_root=290)
        )

        self.assertTrue(window.layered_main_drag_armed)
        self.assertEqual(moves, [(250, 290)])
        self.assertIn(id(window.root), window.drag_state)

    def test_dragging_from_action_does_not_click_and_cleans_drag_state(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: True
        window._layered_main_region_at = lambda _x, _y: 'action:settings'
        window.drag_state, window.restore_geometry = {}, {}
        window._flush_window_geometry = lambda _window: None
        window.root = SimpleNamespace(winfo_x=lambda: 100, winfo_y=lambda: 100)
        window._drag_move = lambda _event, _window: None
        window._remember_root_geometry = lambda: None
        settings = []
        window.show_settings = lambda: settings.append(True)
        event = SimpleNamespace(x=10, y=10, x_root=110, y_root=110)
        window._layered_main_press(event)
        window._layered_main_drag(SimpleNamespace(x=30, y=10, x_root=130, y_root=110))
        window._layered_main_release(SimpleNamespace(x=10, y=10, x_root=130, y_root=110))
        self.assertEqual(settings, [])
        self.assertFalse(window.drag_state)

    def test_fallback_drag_from_button_does_not_execute_its_command(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: False
        window.drag_state, window.resize_state, window.restore_geometry = {}, {}, {}
        window._flush_window_geometry = lambda _window: None
        window.root = SimpleNamespace(winfo_x=lambda: 100, winfo_y=lambda: 100)
        window._drag_move = lambda _event, _window: None
        window._remember_root_geometry = lambda: None
        window._hide_main_tooltip = lambda: None
        button = SimpleNamespace(winfo_width=lambda: 30, winfo_height=lambda: 30)
        commands = []
        window._fallback_main_press(SimpleNamespace(x_root=110, y_root=110))
        window._fallback_main_drag(SimpleNamespace(x_root=130, y_root=110))
        event = SimpleNamespace(x=10, y=10, x_root=130, y_root=110)
        window._fallback_main_icon_release(event, button, lambda: commands.append(True))
        window._fallback_main_release(event)
        self.assertEqual(commands, [])
        self.assertFalse(window.drag_state)
        window._fallback_main_press(SimpleNamespace(x_root=110, y_root=110))
        event = SimpleNamespace(x=10, y=10, x_root=112, y_root=110)
        window._fallback_main_icon_release(event, button, lambda: commands.append(True))
        window._fallback_main_release(event)
        self.assertEqual(commands, [True])

    def test_layered_header_drag_waits_for_deliberate_pointer_movement(self):
        window, _ = make_window()
        window.window_locked = False
        window._layered_main_active = lambda: True
        window.layered_main_hit_regions = {"drag:window": (0, 0, 300, 30)}
        window.drag_state = {}
        window.restore_geometry = {}
        window._flush_window_geometry = lambda _window: None
        window.root = SimpleNamespace(winfo_x=lambda: 100, winfo_y=lambda: 100)
        moves = []
        window._drag_move = lambda event, _window: moves.append(
            (event.x_root, event.y_root)
        )
        press = SimpleNamespace(x=100, y=15, x_root=200, y_root=115)

        window._layered_main_press(press)
        window._layered_main_drag(
            SimpleNamespace(x=104, y=18, x_root=204, y_root=118)
        )
        self.assertEqual(moves, [])
        self.assertFalse(window.layered_main_dragged)

        window._layered_main_drag(
            SimpleNamespace(x=108, y=15, x_root=208, y_root=115)
        )
        self.assertEqual(moves, [(208, 115)])
        self.assertTrue(window.layered_main_dragged)

    def test_fallback_header_drag_also_ignores_pointer_jitter(self):
        window, _ = make_window()

        class Root:
            def __init__(self):
                self.geometry_calls = []

            @staticmethod
            def winfo_x():
                return 100

            @staticmethod
            def winfo_y():
                return 100

            def geometry(self, value):
                self.geometry_calls.append(value)

        root = Root()
        window.root = root
        window.window_locked = False
        window.restore_geometry = {}
        window.drag_state = {}
        window.drag_press_origins = {}
        window._flush_window_geometry = lambda _window: None

        window._drag_start(
            SimpleNamespace(x_root=200, y_root=115), root
        )
        window._drag_move(
            SimpleNamespace(x_root=204, y_root=118), root
        )
        self.assertEqual(root.geometry_calls, [])

        window._drag_move(
            SimpleNamespace(x_root=208, y_root=115), root
        )
        self.assertEqual(root.geometry_calls, ["+108+100"])

    def test_main_drag_band_does_not_double_click_maximize(self):
        window, _ = make_window()

        class Widget:
            def __init__(self):
                self.bindings = {}

            def bind(self, sequence, callback):
                self.bindings[sequence] = callback

        root = object()
        window.root = root
        main_header = Widget()
        settings_header = Widget()

        window._bind_drag(main_header, root)
        window._bind_drag(settings_header, object())

        self.assertNotIn("<Double-Button-1>", main_header.bindings)
        self.assertIn("<Double-Button-1>", settings_header.bindings)

    def test_missing_entity_uses_same_process_identity_name(self):
        window, _ = make_window()
        window.model.self_id = None
        window.game_pid = 123
        window.worker = SimpleNamespace(self_identity_cache={"game_pid": 123, "name": "莫雪"})
        window.settlement_ui = SimpleNamespace(tracker=EncounterTracker())
        self.assertEqual(window._settlement_main_rows()[0]["name"], "莫雪")
        window.game_pid = 456
        self.assertNotIn("莫雪", window._settlement_main_rows()[0]["name"])
        window.game_pid = 123
        window.hide_names = True
        self.assertNotIn("莫雪", window._settlement_main_rows()[0]["name"])

    def test_dummy_without_boss_encounter_keeps_new_layout(self):
        window, _ = make_window()
        window.model.started = False
        window.main_recent_battle_expanded = True
        window.model.entity_names[1] = "莫雪"
        window.model.entity_extraordinary_ratings.update(
            {1: 80_702, 2: 80_902}
        )
        window.model.combat_target_id = 90
        window.model.entity_combat_states = {90: True}
        window.model._is_dummy_encounter = lambda: True
        window.model._is_dummy_target = lambda entity_id: entity_id == 90
        window.model.current_bosses = lambda: []
        window.settlement_ui = SimpleNamespace(
            tracker=EncounterTracker(), visible=lambda _live=None: None
        )
        window.main_last_battle_result = None
        window.main_scroll_offset = 0
        window.main_time_text = "00:00"
        window.team_dps_text = "0"
        rows, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertEqual(rows[0]["name"], "莫雪")
        self.assertEqual(rows[1]["row_kind"], "section")
        self.assertEqual(rows[1]["section_text"], "实时战斗/战斗记录")
        self.assertTrue(rows[1]["expanded"])
        rating_rows = [row for row in rows[2:] if row.get("metric") == "rating"]
        self.assertEqual(rating_rows, [])
        self.assertEqual(rows[2]["row_kind"], "message")
        self.assertFalse(window._main_battle_signal_active())

        snapshot = window._layered_main_snapshot()
        self.assertEqual(snapshot["dps_summary_caption"], "团队超凡评分")
        self.assertEqual(snapshot["team_dps"], "80,802")

    def test_first_boss_without_team_dps_keeps_team_rating_rows(self):
        window, _ = make_window()
        # A legacy folded value must not turn the static heading back into an
        # accordion after an update or capture-session handoff.
        window.main_recent_battle_expanded = False
        window.model.entity_names[1] = "莫雪"
        roster = [
            {
                "id": "self-token",
                "iid": 1,
                "name": "本人",
                "profession_id": 1200001,
            }
        ]
        tracker = EncounterTracker()
        current = tracker.begin(
            instance_id="first-instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
        )
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            last_visible_id=current.local_encounter_id,
        )

        rows = window._settlement_main_rows(now=205)

        self.assertEqual(rows[0]["actor_id"], 1)
        self.assertEqual(rows[0]["name"], "莫雪")
        self.assertEqual(rows[1]["row_kind"], "section")
        self.assertEqual(rows[1]["section_text"], "实时战斗/战斗记录")
        self.assertTrue(rows[1]["expanded"])
        self.assertNotIn("section_action", rows[1])
        self.assertEqual(rows[2]["row_kind"], "message")
        self.assertFalse(any(row.get("metric") == "rating" for row in rows[2:]))

        window._shown_actor_name = DpsWindow._shown_actor_name.__get__(
            window, DpsWindow
        )
        window.hide_names = True
        hidden_rows = window._settlement_main_rows(now=205)
        self.assertEqual(hidden_rows[0]["name"], "玩家1")
        self.assertNotIn("莫雪", hidden_rows[0]["name"])

    def test_local_dummy_damage_schedules_an_immediate_layered_paint(self):
        window = object.__new__(DpsWindow)
        ingested = []
        window.model = SimpleNamespace(
            self_id=17,
            ingest=lambda payload: ingested.append(dict(payload)),
            _is_dummy_target=lambda entity_id: entity_id == 90,
        )
        window._bind_active_settlement_history = lambda: None
        scheduled = []
        window._schedule_layered_main_render = lambda: scheduled.append(True)

        window._ingest_combat_event(
            {"attacker_id": 17, "target_id": 90, "damage": 409_552}
        )

        self.assertEqual(len(ingested), 1)
        self.assertEqual(scheduled, [True])

    def test_live_settlement_pins_self_then_shows_previous_battle(self):
        window, members = make_window()
        window.pve_hud_view = "recent_battle"
        window.main_recent_battle_expanded = True
        members.clear()
        members.update({1, 2})
        window.model.friend_order = [1, 2]
        window.model.actor_character_ids = {
            1: "self-token",
            2: "ally-token",
        }
        window.model.self_character_id = "self-token"
        window.model.entity_extraordinary_ratings = {1: 80_001, 2: 88_893}
        actors = window.model.current_stats()
        actors[0].damage = 30_000
        actors[1].damage = 600_000
        window.model.duration = lambda _now=None: 5

        roster = [
            {
                "id": "self-token",
                "iid": 1,
                "name": "本人",
                "profession_id": 1200001,
            },
            {
                "id": "ally-token",
                "iid": 2,
                "name": "队友",
                "profession_id": 1200001,
            },
        ]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="same-instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
        )
        tracker.end("WIPE", 110_000_000_000)
        previous.boss_name = "异化猎犬"
        previous.boss_template_id = 7109821
        previous.settlement_status = "SETTLED"
        previous.encounter_duration_seconds = 10
        previous.duration_source = "server_encounter_clock"
        previous.participants = [
            dict(roster[0], damage=100_000, dps=10_000, bear=None, heal=None),
            dict(roster[1], damage=300_000, dps=30_000, bear=None, heal=None),
        ]
        current = tracker.begin(
            instance_id="same-instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
        )
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            last_visible_id=current.local_encounter_id,
            visible=lambda _live=None: encounter_view(current),
        )
        window.main_last_battle_result = None
        window.history_store = SimpleNamespace(load=lambda _encounter_id: {
            "monster": {
                "name": "异化猎犬",
                "template_id": 7109821,
                "current_hp": 8_625_426,
                "max_hp": 34_501_705,
            }
        })
        window.main_visible_rows = 1
        window.main_scroll_offset = 0
        window.main_time_text = "00:05"
        window.team_dps_text = "126,000"

        rows = window._settlement_main_rows(now=205)

        self.assertEqual(rows[0]["actor_id"], 1)
        self.assertTrue(rows[0]["is_live_self"])
        self.assertEqual(rows[0]["name"], "队员1")
        self.assertEqual(rows[0]["total_value"], 30_000)
        self.assertEqual(rows[0]["stat_value"], 6_000)
        self.assertEqual(rows[0]["inline_rating_text"], "80001")
        self.assertEqual(rows[1]["row_kind"], "section")
        self.assertTrue(rows[1]["expanded"])
        self.assertEqual(rows[2]["row_kind"], "recent_boss")
        self.assertEqual(rows[2]["boss_name"], "异化猎犬")
        self.assertEqual(rows[2]["boss_template_id"], 7109821)
        self.assertEqual(rows[2]["boss_percent"], "25.0%")
        self.assertEqual(
            [row["actor_id"] for row in rows[3:]],
            [2, 1],
        )
        self.assertEqual(
            [row["total_value"] for row in rows[3:]],
            [300_000, 100_000],
        )
        self.assertTrue(
            all(not row["hide_total"] for row in rows[3:])
        )
        self.assertTrue(all(not row["interactive"] for row in rows[3:]))
        self.assertEqual(
            [row["inline_rating_text"] for row in rows[3:]],
            ["88893", "80001"],
        )
        self.assertNotIn("message", {row.get("row_kind") for row in rows})

        snapshot = window._layered_main_snapshot()
        self.assertEqual(snapshot["visible_rows"], 4)
        self.assertEqual(snapshot["dps_summary_caption"], "团队总秒伤")
        self.assertEqual(snapshot["team_dps"], "40,000/s")
        window.main_recent_battle_expanded = False
        still_open = window._settlement_main_rows(now=205)
        self.assertEqual(len(still_open), len(rows))
        self.assertTrue(still_open[1]["expanded"])
        self.assertEqual(window._layered_main_snapshot()["visible_rows"], 4)

    def test_new_party_does_not_mutate_previous_settlement_rows(self):
        window, members = make_window()
        window.main_recent_battle_expanded = True
        members.clear()
        members.update({1, 7})
        window.model.friend_order = [1, 7]
        window.model.party_active = True
        window.model.party_member_count = 2
        window.model.actor_character_ids = {
            1: "self-token",
            7: "new-token",
        }
        window.model.self_character_id = "self-token"
        window.model.entity_names.update({1: "Self", 7: "New member"})
        window.model.entity_professions[7] = 1200003
        window.model.entity_extraordinary_ratings = {
            1: 82_929,
            7: 91_234,
        }

        old_roster = [
            {
                "id": "self-token",
                "iid": 1,
                "name": "Self",
                "profession_id": 1200001,
            },
            {
                "id": "departed-token",
                "iid": 2,
                "name": "Departed member",
                "profession_id": 1200001,
            },
        ]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="old-instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=old_roster,
            self_token="self-token",
        )
        tracker.life("self-token", 105_000_000_000, True)
        tracker.end("WIPE", 110_000_000_000)
        previous.boss_name = "旧队伍首领"
        previous.settlement_status = "SETTLED"
        previous.encounter_duration_seconds = 10
        previous.duration_source = "verified_shared_encounter_clock"
        previous.participants = [
            dict(old_roster[0], damage=100_000, dps=10_000),
            dict(old_roster[1], damage=300_000, dps=30_000),
        ]
        window.settlement_ui = SimpleNamespace(tracker=tracker)

        rows = window._settlement_main_rows(now=205)
        self.assertEqual(rows[2]["row_kind"], "recent_boss")
        recent = rows[3:]

        self.assertEqual([row["actor_id"] for row in recent], [2, 1])
        self.assertNotIn(7, {row["actor_id"] for row in recent})
        self.assertEqual(recent[0]["stat_value"], 30_000)
        self.assertEqual(recent[0]["total_value"], 300_000)
        self.assertEqual(recent[0]["deaths"], 0)
        self.assertEqual(recent[1]["stat_value"], 10_000)
        self.assertEqual(recent[1]["total_value"], 100_000)
        self.assertEqual(recent[1]["deaths"], 1)
        self.assertEqual(
            [row["inline_rating_text"] for row in recent],
            ["", "82929"],
        )

    def test_next_boss_retains_previous_team_result_after_teammates_leave(self):
        window, members = make_window()
        window.model.party_active = True
        window.model.party_member_count = 6
        window.model.party_session_id = 7
        window.model.self_character_id = "self-token"
        roster = [
            {"id": "self-token", "iid": 1, "name": "本人", "profession_id": 1200001},
            *(
                {"id": f"peer-{index}", "iid": index + 1,
                 "name": f"队友{index}", "profession_id": 1200001}
                for index in range(1, 6)
            ),
        ]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="same-instance", started_at_ns=100_000_000_000,
            participants_snapshot=roster, boss_template_id=7109821,
            self_token="self-token", party_session_id=7,
        )
        tracker.end("WIPE", 110_000_000_000)
        previous.settlement_status = "SETTLED"
        previous.encounter_duration_seconds = 10
        previous.participants = [
            dict(member, damage=index * 100_000, dps=index * 10_000)
            for index, member in enumerate(roster, 1)
        ]
        successor = tracker.begin(
            instance_id="same-instance", started_at_ns=200_000_000_000,
            participants_snapshot=roster, boss_template_id=7115042,
            self_token="self-token", party_session_id=7,
        )
        window.settlement_ui = SimpleNamespace(tracker=tracker)
        self.assertIs(window._main_recent_settlement_record(), previous)

        # Actual order: the third Boss starts in the six-player team, then
        # removing the final teammate disbands it without changing that
        # already-created Encounter's party_session_id.
        members.clear()
        members.add(1)
        window.model.friend_order = [1]
        window.model.party_active = False
        window.model.party_member_count = 1
        window.model.party_session_id = 0
        window.model.actor_character_ids = {1: "self-token"}

        rows = window._settlement_main_rows(now=205)
        previous_rows = [row for row in rows if row.get("is_recent_battle")]
        self.assertEqual(len(previous_rows), 6)
        self.assertEqual(
            {row["total_value"] for row in previous_rows},
            {100_000, 200_000, 300_000, 400_000, 500_000, 600_000},
        )

        successor.instance_id = "another-dungeon"
        self.assertIsNone(window._main_recent_settlement_record())
        successor.instance_id = "same-instance"
        window.model.party_session_id = 8
        self.assertIs(window._main_recent_settlement_record(), previous)

    def test_same_dungeon_roster_replacement_keeps_previous_boss_dps(self):
        window, members = make_window()
        window.model.party_session_id = 7
        window.model.self_character_id = "self-token"
        window.model.started = False
        roster = [
            {"id": "self-token", "iid": 1, "name": "本人"},
            {"id": "old-peer", "iid": 2, "name": "旧队友"},
        ]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="same-instance", started_at_ns=100_000_000_000,
            participants_snapshot=roster, self_token="self-token",
            party_session_id=7, boss_template_id=7100471,
        )
        tracker.end("VICTORY", 110_000_000_000)
        previous.settlement_status = "SETTLED"
        previous.encounter_duration_seconds = 10
        previous.participants = [
            dict(roster[0], damage=100_000, dps=10_000),
            dict(roster[1], damage=300_000, dps=30_000),
        ]
        tracker.begin(
            instance_id="same-instance", started_at_ns=200_000_000_000,
            participants_snapshot=[{"id": "self-token", "iid": 1, "name": "本人"}],
            self_token="self-token", party_session_id=8,
            boss_template_id=7100401,
        )
        window.settlement_ui = SimpleNamespace(
            tracker=tracker, instance_id="same-instance",
            session_encounter_ids=set(tracker.encounters),
        )
        window.main_recent_battle_scope_started_ns = 0
        window.main_cleared_encounter_ids = deque(maxlen=64)
        window._invalidate_team_rating_preview_rows = lambda *args, **kwargs: None
        window._schedule_layered_main_render = lambda *args, **kwargs: None
        window._schedule_team_equipment_profiles = lambda: None
        window._main_boundary_timestamp_ns = lambda _payload: 201_000_000_000

        def replace_party(_payload):
            window.model.party_session_id = 8
            window.model.party_member_count = 2
            members.clear()
            members.update({1, 3})
            window.model.friend_order = [1, 3]
            return True

        window.model.ingest_party = replace_party
        window._dispatch_message("party", {
            "party_session_id": 8, "roster_replace": True,
        })

        self.assertEqual(window.main_recent_battle_scope_started_ns, 0)
        self.assertIs(window._main_recent_settlement_record(), previous)
        rows = window._settlement_main_rows(now=205)
        self.assertEqual(
            {row.get("total_value") for row in rows if row.get("is_recent_battle")},
            {100_000, 300_000},
        )

        # A roster change can advance the HUD scope after the prior Boss
        # started. The same dungeon and character still own that result.
        window.main_recent_battle_scope_started_ns = 201_000_000_000
        self.assertIs(window._main_recent_settlement_record(), previous)
        self.assertEqual(
            {row.get("total_value") for row in window._settlement_main_rows(now=205)
             if row.get("is_recent_battle")},
            {100_000, 300_000},
        )
        window.model.self_character_id = "another-character"
        self.assertIsNone(window._main_recent_settlement_record())
        window.model.self_character_id = "self-token"
        tracker.encounters[tracker.current_id].instance_id = "another-dungeon"
        self.assertIsNone(window._main_recent_settlement_record())

    def test_final_boss_settlement_is_immediately_displayable_without_successor(self):
        window, members = make_window()
        members.clear()
        members.update({1, 2})
        window.model.friend_order = [1, 2]
        window.model.actor_character_ids = {1: "self-token", 2: "peer-token"}
        window.model.self_character_id = "self-token"
        window.model.party_session_id = 7
        window.model.started = False
        roster = [
            {"id": "self-token", "iid": 1, "name": "Self"},
            {"id": "peer-token", "iid": 2, "name": "Peer"},
        ]
        tracker = EncounterTracker()
        final = tracker.begin(
            instance_id="same-instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
            party_session_id=7,
            boss_template_id=7_100_471,
        )
        tracker.end("VICTORY", 110_000_000_000)
        final.settlement_status = "SETTLED"
        final.encounter_duration_seconds = 10
        final.participants = [
            dict(roster[0], damage=100_000, dps=10_000),
            dict(roster[1], damage=300_000, dps=30_000),
        ]
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            instance_id="same-instance",
            session_encounter_ids={final.local_encounter_id},
        )
        window.main_recent_battle_scope_started_ns = 0
        window.main_cleared_encounter_ids = deque(maxlen=64)

        self.assertIsNone(tracker.current_id)
        self.assertIs(window._main_recent_settlement_record(), final)
        self.assertIs(window._main_displayable_team_dps_record(), final)
        self.assertEqual(
            {
                row.get("total_value")
                for row in window._settlement_main_rows(now=111)
                if row.get("is_recent_battle")
            },
            {100_000, 300_000},
        )

    def test_recent_battle_omits_unbound_projection_slots(self):
        window, _ = make_window()
        window.model.self_character_id = 'self-token'
        roster = [
            {'id': 'self-token', 'iid': 1, 'name': '本人', 'is_ai': False},
            {'id': 'peer-token', 'iid': 2, 'name': '莫雪', 'is_ai': False},
            *({'id': f'projection-{index}', 'iid': None, 'name': None,
               'is_ai': True} for index in range(4)),
        ]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id='instance', started_at_ns=100_000_000_000,
            participants_snapshot=roster, self_token='self-token',
            boss_template_id=7100401,
        )
        tracker.end('WIPE', 110_000_000_000)
        previous.settlement_status = 'SETTLED'
        previous.encounter_duration_seconds = 10
        previous.stage_statistics = {'members': [
            {'id': 'self-token'}, {'id': 'peer-token'},
        ]}
        previous.participants = [
            dict(roster[0], damage=100_000, dps=10_000),
            dict(roster[1], damage=20_000, dps=2_000),
            *(dict(row, damage=None, dps=None) for row in roster[2:]),
        ]
        tracker.begin(
            instance_id='instance', started_at_ns=200_000_000_000,
            participants_snapshot=[roster[0]], self_token='self-token',
            boss_template_id=7100401,
        )
        window.settlement_ui = SimpleNamespace(tracker=tracker)

        rows = window._settlement_main_rows(now=205)

        self.assertEqual(
            [row['total_value'] for row in rows if row.get('is_recent_battle')],
            [100_000, 20_000],
        )
        self.assertEqual(len(previous.participants_snapshot), 6)

    def test_party_session_prevents_old_team_result_from_reappearing(self):
        window, members = make_window()
        members.clear()
        members.update({1, 7})
        window.model.friend_order = [1, 7]
        window.model.party_active = True
        window.model.party_member_count = 2
        window.model.party_session_id = 2
        window.model.actor_character_ids = {
            1: "self-token",
            7: "new-token",
        }
        window.model.self_character_id = "self-token"
        window.model.entity_names.update({1: "Self", 7: "New member"})
        window.model.entity_extraordinary_ratings = {1: 80_616, 7: 91_234}

        old_roster = [
            {"id": "self-token", "iid": 1, "name": "Self"},
            {"id": "old-token", "iid": 2, "name": "Old member"},
        ]
        tracker = EncounterTracker()
        old = tracker.begin(
            instance_id="old-instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=old_roster,
            self_token="self-token",
            party_session_id=1,
        )
        tracker.end("WIPE", 110_000_000_000)
        old.settlement_status = "SETTLED"
        old.participants = [
            dict(old_roster[0], damage=100_000, dps=10_000),
            dict(old_roster[1], damage=300_000, dps=30_000),
        ]
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            session_encounter_ids={old.local_encounter_id},
        )

        rows = window._settlement_main_rows()
        self.assertNotIn("recent_boss", {row.get("row_kind") for row in rows})
        lower = [row for row in rows[2:] if row.get("actor_id") is not None]
        self.assertEqual(lower, [])
        team_rows = window._main_team_rating_section_rows()
        self.assertEqual([row["actor_id"] for row in team_rows], [1, 7])
        self.assertTrue(all(row["total_value"] is None for row in team_rows))

        new_roster = [
            {"id": "self-token", "iid": 1, "name": "Self"},
            {"id": "new-token", "iid": 7, "name": "New member"},
        ]
        current = tracker.begin(
            instance_id="new-instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=new_roster,
            self_token="self-token",
            party_session_id=2,
        )
        tracker.end("VICTORY", 210_000_000_000)
        current.settlement_status = "SETTLED"
        current.participants = [
            dict(new_roster[0], damage=222_000, dps=22_200),
            dict(new_roster[1], damage=444_000, dps=44_400),
        ]
        window.settlement_ui.session_encounter_ids.add(current.local_encounter_id)

        settled_rows = window._settlement_main_rows()
        settled_lower = [
            row for row in settled_rows[2:] if row.get("actor_id") is not None
        ]
        self.assertEqual(
            [row["total_value"] for row in settled_lower],
            [444_000, 222_000],
        )

        window.model.party_active = False
        window.model.party_session_id = 0
        self.assertEqual(len(window._settlement_main_rows()), 3)
        self.assertEqual(window._settlement_main_rows()[2]["row_kind"], "message")

    def test_solo_next_pull_shows_previous_settlement(self):
        window, members = make_window()
        members.clear()
        members.add(1)
        window.model.friend_order = [1]
        window.model.party_active = False
        window.model.party_member_count = 1
        window.model.party_session_id = 0
        window.model.actor_character_ids = {1: "self-token"}
        window.model.self_character_id = "self-token"
        window.model.entity_extraordinary_ratings = {1: 80_616}
        roster = [{
            "id": "self-token",
            "iid": 1,
            "name": "Self",
            "profession_id": 1200001,
        }]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="solo-instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7115042,
            boss_token="solo-boss-one",
            self_token="self-token",
            party_session_id=0,
        )
        previous.boss_name = "Solo Boss"
        tracker.end("WIPE", 110_000_000_000)
        previous.settlement_status = "SETTLED"
        previous.participants = [
            dict(roster[0], damage=11_228, dps=863.6923076923077)
        ]
        tracker.begin(
            instance_id="solo-instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7115042,
            boss_token="solo-boss-two",
            self_token="self-token",
            party_session_id=0,
        )
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            session_encounter_ids=set(tracker.encounters),
        )

        rows = window._settlement_main_rows(now=205)

        self.assertEqual(rows[2]["row_kind"], "recent_boss")
        self.assertEqual(rows[2]["boss_name"], "Solo Boss")
        self.assertEqual(rows[3]["actor_id"], 1)
        self.assertEqual(rows[3]["total_value"], 11_228)
        self.assertAlmostEqual(rows[3]["stat_value"], 863.6923076923077)

        window.live_hud_combat_state = {
            "active": True,
            "segment_id": 1,
            "started_at_epoch": 200.0,
        }
        window.live_hud_dps_segment = {
            "kind": "small_monsters",
            "active": True,
            "started_at_epoch": 200.0,
            "active_started_at_epoch": 200.0,
            "ended_at_epoch": 0.0,
            "elapsed_seconds": 5.0,
            "rows": [],
        }
        waiting_rows, _ = window._main_display_rows(now=205)
        waiting_snapshot = window._layered_main_snapshot()

        self.assertEqual(waiting_rows[2]["row_kind"], "recent_boss")
        self.assertEqual(waiting_rows[3]["total_value"], 11_228)
        self.assertFalse(waiting_snapshot.get("live_no_boss", False))

        window.live_hud_dps_segment["rows"] = [
            {
                "actor_id": 91,
                "total_value": 90_000,
                "targetless_team_live": True,
            }
        ]
        live_rows, _ = window._main_display_rows(now=205)
        self.assertTrue(live_rows[0]["is_live_self"])
        self.assertEqual(live_rows[1]["row_kind"], "section")
        self.assertEqual([row["actor_id"] for row in live_rows[2:]], [91])
        self.assertIs(window._main_recent_settlement_record(), previous)

        window.live_hud_combat_state = None
        window.live_hud_dps_segment["active"] = False
        window.live_hud_dps_segment["ended_at_epoch"] = 205.0
        restored_rows, _ = window._main_display_rows(now=205)
        self.assertEqual(restored_rows[2]["row_kind"], "recent_boss")
        self.assertEqual(restored_rows[3]["total_value"], 11_228)

    def test_hundred_wipes_keep_latest_result_visible_on_next_pull(self):
        window, members = make_window()
        members.clear()
        members.update({1, 2})
        window.model.friend_order = [1, 2]
        window.model.party_active = True
        window.model.party_member_count = 2
        window.model.party_session_id = 7
        window.model.actor_character_ids = {1: "self-token", 2: "peer-token"}
        window.model.self_character_id = "self-token"
        roster = [
            {
                "id": "self-token",
                "iid": 1,
                "name": "Self",
                "profession_id": 1200001,
            },
            {
                "id": "peer-token",
                "iid": 2,
                "name": "Peer",
                "profession_id": 1200001,
            },
        ]
        tracker = EncounterTracker()
        completed = []
        for attempt in range(100):
            started_at_ns = (100 + attempt * 20) * 1_000_000_000
            encounter = tracker.begin(
                instance_id="same-instance",
                started_at_ns=started_at_ns,
                participants_snapshot=roster,
                self_token="self-token",
                party_session_id=7,
                boss_template_id=7_100_401,
                boss_token=f"boss-{attempt + 1}",
            )
            tracker.end("WIPE", started_at_ns + 10_000_000_000)
            encounter.settlement_status = "SETTLED"
            encounter.encounter_duration_seconds = 10
            encounter.participants = [
                dict(roster[0], damage=10_000 + attempt, dps=1_000 + attempt / 10),
                dict(roster[1], damage=20_000 + attempt, dps=2_000 + attempt / 10),
            ]
            completed.append(encounter)
        tracker.begin(
            instance_id="same-instance",
            started_at_ns=2_100_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
            party_session_id=7,
            boss_template_id=7_100_401,
            boss_token="boss-101",
        )
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            instance_id="same-instance",
            session_encounter_ids=set(tracker.encounters),
        )
        window.main_recent_battle_scope_started_ns = 0
        window.main_cleared_encounter_ids = deque(maxlen=64)
        window.history_store = SimpleNamespace(load=lambda _encounter_id: None)

        latest = window._main_recent_settlement_record(keep_last_settled=False)
        rows = window._settlement_main_rows(now=2_105)
        recent_rows = [row for row in rows if row.get("is_recent_battle")]

        self.assertIs(latest, completed[-1])
        self.assertEqual(
            [row["total_value"] for row in recent_rows],
            [20_099, 10_099],
        )
        self.assertEqual(
            [row["stat_value"] for row in recent_rows],
            [2_009.9, 1_009.9],
        )

    def test_party_boundary_hides_older_solo_result_after_leaving(self):
        window, members = make_window()
        members.clear()
        members.add(1)
        window.model.friend_order = [1]
        window.model.party_active = True
        window.model.party_member_count = 2
        window.model.party_session_id = 7
        window.model.started = False
        window.model.actor_character_ids = {1: "self-token"}
        window.model.self_character_id = "self-token"
        roster = [{"id": "self-token", "iid": 1, "name": "Self"}]
        tracker = EncounterTracker()
        old_solo = tracker.begin(
            instance_id="old-solo-instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
            party_session_id=0,
        )
        tracker.end("WIPE", 110_000_000_000)
        old_solo.settlement_status = "SETTLED"
        old_solo.participants = [dict(roster[0], damage=10_000, dps=1_000)]
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            session_encounter_ids={old_solo.local_encounter_id},
        )
        window.main_recent_battle_scope_started_ns = 0
        window.main_cleared_encounter_ids = []
        window.main_last_battle_result = None
        window.main_last_live_display_snapshot = None
        window.main_manual_clear_state = None
        window.main_hidden_combat_encounter_id = ""
        window.main_scroll_offset = 0
        window.main_time_text = "00:00"
        window.team_dps_text = "0"
        window.enrage_prediction = None
        window.enrage_prediction_visible = False
        window._hide_enrage_tooltip = lambda: None
        window._schedule_layered_main_render = lambda: None
        window.model.ingest_party = lambda _payload: (
            setattr(window.model, "party_session_id", 0)
            or setattr(window.model, "party_active", False)
            or setattr(window.model, "party_member_count", 1)
            or True
        )

        window._dispatch_message(
            "party",
            {
                "party_session_id": 0,
                "in_team": False,
                "capture_timestamp_ns": 200_000_000_000,
            },
        )

        self.assertEqual(window.main_recent_battle_scope_started_ns, 200_000_000_000)
        self.assertEqual(len(window._settlement_main_rows()), 3)
        self.assertEqual(window._settlement_main_rows()[2]["row_kind"], "message")

    def test_new_party_session_masks_previous_local_dps_until_next_encounter(self):
        window, members = make_window()
        members.clear()
        members.update({1, 2})
        window.model.friend_order = [1, 2]
        window.model.party_session_id = 1
        window.model.encounter_id = "old-team-encounter"
        window.model.combat_end_time = 123.0
        window.model.actor_character_ids = {
            1: "self-token",
            2: "peer-token",
        }
        window.model.self_character_id = "self-token"
        window.model.entity_extraordinary_ratings = {1: 80_616, 2: 88_893}

        def ingest_party(_payload):
            window.model.party_session_id = 2
            window.model.party_active = True
            window.model.party_member_count = 2
            return True

        window.model.ingest_party = ingest_party
        window.settlement_ui = SimpleNamespace(
            tracker=SimpleNamespace(current_id=None, encounters={}),
            session_encounter_ids=set(),
        )
        window.main_last_battle_result = completed_record()
        window.main_last_live_display_snapshot = {
            "encounter_id": "old-team-encounter"
        }
        window.main_time_text = "00:30"
        window.team_dps_text = "105,000"
        window.enrage_prediction = object()
        window.enrage_prediction_visible = True
        renders = []
        window._schedule_layered_main_render = lambda: renders.append(True)

        window._dispatch_message(
            "party", {"party_session_id": 2, "in_team": True}
        )
        rows = window._settlement_main_rows()

        self.assertEqual(window.main_hidden_combat_encounter_id, "old-team-encounter")
        self.assertIsNone(window.main_last_battle_result)
        self.assertEqual(rows[0]["actor_id"], 1)
        self.assertIsNone(rows[0]["stat_value"])
        self.assertIsNone(rows[0]["total_value"])
        self.assertTrue(
            all(
                row.get("total_value") is None
                for row in rows[2:]
                if row.get("actor_id") is not None
            )
        )
        self.assertEqual(renders, [True])

        window.model.encounter_id = "new-team-encounter"
        self.assertFalse(window._main_context_display_is_stale())

    def test_new_dungeon_scope_rejects_old_or_late_settlement(self):
        window, _ = make_window()
        window.model.started = False
        window.model.party_session_id = 7
        roster = [{
            "id": "self-token",
            "iid": 1,
            "name": "Self",
            "profession_id": 1200001,
        }]
        tracker = EncounterTracker()
        old = tracker.begin(
            instance_id="old-dungeon",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
            party_session_id=7,
        )
        tracker.end("VICTORY", 110_000_000_000)
        old.settlement_status = "SETTLED"
        old.participants = [dict(roster[0], damage=100_000, dps=10_000)]
        new = tracker.begin(
            instance_id="new-dungeon",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
            party_session_id=7,
        )
        tracker.end("VICTORY", 210_000_000_000)
        new.settlement_status = "SETTLED"
        new.participants = [dict(roster[0], damage=200_000, dps=20_000)]
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            session_encounter_ids={
                old.local_encounter_id,
                new.local_encounter_id,
            },
        )
        window.main_recent_battle_scope_started_ns = 150_000_000_000

        self.assertIs(window._main_recent_settlement_record(), new)
        new.history_deleted = True
        self.assertIsNone(window._main_recent_settlement_record())

    def test_newer_pending_pull_does_not_fall_back_to_older_same_boss_result(self):
        window, members = make_window()
        window.main_recent_battle_expanded = True
        members.clear()
        members.add(1)
        window.model.friend_order = [1]
        window.model.actor_character_ids = {1: "self-token"}
        window.model.self_character_id = "self-token"
        roster = [{
            "id": "self-token",
            "iid": 1,
            "name": "Self",
            "profession_id": 1200001,
        }]
        tracker = EncounterTracker()
        settled = tracker.begin(
            instance_id="instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7109821,
            boss_token="boss-one",
            self_token="self-token",
        )
        settled.boss_name = "异化猎犬"
        tracker.end("WIPE", 110_000_000_000)
        settled.settlement_status = "SETTLED"
        settled.participants = [dict(roster[0], damage=123_456, dps=12_345.6)]
        pending = tracker.begin(
            instance_id="instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7109821,
            boss_token="boss-two",
            self_token="self-token",
        )
        pending.boss_name = "异化猎犬"
        tracker.end("WIPE", 210_000_000_000)
        tracker.begin(
            instance_id="instance",
            started_at_ns=300_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7109821,
            boss_token="boss-three",
            self_token="self-token",
        )
        window.settlement_ui = SimpleNamespace(tracker=tracker)

        rows = window._settlement_main_rows(now=215)

        self.assertIs(
            window._main_recent_settlement_record(keep_last_settled=False),
            pending,
        )
        self.assertEqual(rows[2]["row_kind"], "recent_boss")
        self.assertEqual(rows[2]["boss_name"], "异化猎犬")
        self.assertIsNone(rows[3]["total_value"])
        self.assertIsNone(rows[3]["stat_value"])

    def test_newer_different_boss_pending_uses_its_bound_frozen_rows(self):
        window, members = make_window()
        window.main_recent_battle_expanded = True
        members.clear()
        members.add(1)
        window.model.friend_order = [1]
        window.model.actor_character_ids = {1: "self-token"}
        window.model.self_character_id = "self-token"
        roster = [{
            "id": "self-token",
            "iid": 1,
            "name": "Self",
            "profession_id": 1200001,
        }]
        tracker = EncounterTracker()
        settled = tracker.begin(
            instance_id="instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7109821,
            boss_token="boss-one",
            self_token="self-token",
        )
        settled.boss_name = "First Boss"
        tracker.end("VICTORY", 110_000_000_000)
        settled.settlement_status = "SETTLED"
        settled.participants = [
            dict(roster[0], damage=123_456, dps=12_345.6)
        ]
        pending = tracker.begin(
            instance_id="instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7115042,
            boss_token="boss-two",
            self_token="self-token",
        )
        pending.boss_name = "Second Boss"
        tracker.end("WIPE", 210_000_000_000)
        next_roster = [
            roster[0],
            {
                "id": "replacement-token",
                "iid": 7,
                "name": "Replacement",
                "profession_id": 1200002,
            },
        ]
        tracker.begin(
            instance_id="instance",
            started_at_ns=300_000_000_000,
            participants_snapshot=next_roster,
            boss_template_id=7100401,
            boss_token="boss-three",
            self_token="self-token",
        )
        window.settlement_ui = SimpleNamespace(tracker=tracker)
        retained = completed_record()
        retained["encounter_id"] = "local-second-boss"
        retained["participants"] = [
            dict(
                retained["participants"][0],
                damage=222_222,
                dps=22_222.2,
            )
        ]
        window.main_last_battle_result = retained
        window.settlement_history_adapter = SimpleNamespace(
            local_bindings={
                "local-second-boss": pending.local_encounter_id,
            }
        )

        self.assertIs(
            window._main_recent_settlement_record(keep_last_settled=False),
            pending,
        )
        self.assertIsNone(window._main_displayable_team_dps_record())
        self.assertFalse(window._main_has_displayable_team_dps())
        rows = window._settlement_main_rows(now=215)
        self.assertEqual(rows[1]["row_kind"], "section")
        self.assertEqual(rows[2]["row_kind"], "recent_boss")
        self.assertEqual(rows[2]["boss_name"], "Second Boss")
        self.assertEqual(rows[3]["total_value"], 222_222)
        self.assertEqual(rows[3]["stat_value"], 22_222.2)
        self.assertIn("等待服务器结算", rows[3]["settlement_status_text"])

    def test_new_boss_live_keeps_previous_result_below_current_self(self):
        window, _ = make_window()
        window.model.actor_character_ids = {1: "self-token"}
        window.model.self_character_id = "self-token"
        roster = [{
            "id": "self-token", "iid": 1, "name": "Self",
            "profession_id": 1200001,
        }]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="instance", started_at_ns=100_000_000_000,
            participants_snapshot=roster, boss_template_id=7100471,
            boss_token="boss-three", self_token="self-token",
        )
        previous.boss_name = "Boss 3"
        tracker.end("VICTORY", 110_000_000_000)
        previous.settlement_status = "SETTLED"
        previous.encounter_duration_seconds = 10
        previous.participants = [
            dict(roster[0], damage=123_456, dps=12_345.6)
        ]
        tracker.begin(
            instance_id="instance", started_at_ns=200_000_000_000,
            participants_snapshot=roster, boss_template_id=7100401,
            boss_token="boss-two", self_token="self-token",
        )
        window.settlement_ui = SimpleNamespace(tracker=tracker)

        rows = window._settlement_main_rows(now=205)

        self.assertIsNone(window._main_displayable_team_dps_record())
        self.assertFalse(window._main_has_displayable_team_dps())
        self.assertTrue(rows[0]["is_live_self"])
        self.assertNotEqual(rows[0]["total_value"], 123_456)
        self.assertEqual(rows[2]["row_kind"], "recent_boss")
        self.assertEqual(rows[2]["boss_name"], "Boss 3")
        self.assertEqual(rows[3]["total_value"], 123_456)

        window.model.party_session_id = 2
        self.assertIs(
            window._main_recent_settlement_record(settled_only=True), previous
        )
        self.assertIn(
            "recent_boss",
            {row.get("row_kind") for row in window._settlement_main_rows(now=205)},
        )

    def test_finished_boss_without_server_table_shows_bound_local_team_rows(self):
        window, _ = make_window()
        window.model.encounter_id = "local-first-boss"
        window.model.actor_character_ids = {
            actor_id: f"member-{actor_id}" for actor_id in range(1, 7)
        }
        window.model.self_character_id = "member-1"
        window.model.combat_in_progress = lambda _now=None: False
        window.model.resolve_combat_interval = lambda _now=None: SimpleNamespace(
            final=True
        )
        roster = [
            {
                "id": f"member-{actor_id}",
                "iid": actor_id,
                "name": f"队员{actor_id}",
                "profession_id": 1200001,
            }
            for actor_id in range(1, 7)
        ]
        tracker = EncounterTracker()
        current = tracker.begin(
            instance_id="instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7_150_042,
            boss_token="first-boss",
            self_token="member-1",
        )
        current.boss_name = "厄水巨龟"
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            session_encounter_ids={current.local_encounter_id},
        )
        window.settlement_history_adapter = SimpleNamespace(
            local_bindings={
                "local-first-boss": current.local_encounter_id,
            }
        )

        rows, rating_preview = window._main_display_rows(now=205)
        history = [row for row in rows if row.get("is_recent_battle")]

        self.assertFalse(rating_preview)
        self.assertTrue(rows[0]["is_live_self"])
        self.assertEqual(rows[2]["row_kind"], "recent_boss")
        self.assertEqual(rows[2]["boss_name"], "厄水巨龟")
        self.assertEqual([row["actor_id"] for row in history], list(range(1, 7)))
        self.assertEqual(history[0]["total_value"], 630_000)
        self.assertEqual(history[0]["stat_value"], 21_000)
        self.assertTrue(
            all("等待服务器结算" in row["settlement_status_text"] for row in history)
        )
        self.assertEqual(window._main_recent_battle_team_dps_text(), "91,000/s")

        window.settlement_history_adapter.local_bindings[
            "local-first-boss"
        ] = "another-encounter"
        stale_rows = window._settlement_main_rows(now=205)
        self.assertNotIn(
            "recent_boss", {row.get("row_kind") for row in stale_rows}
        )

    def test_bootstrap_boss_rebind_keeps_previous_settlement_visible(self):
        window, _ = make_window()
        window.model.actor_character_ids = {1: "self-token"}
        window.model.self_character_id = "self-token"
        roster = [{
            "id": "self-token", "iid": 1, "name": "Self",
            "profession_id": 1200001,
        }]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="npcap-bootstrap:57423712847775",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7115080,
            boss_token="previous-drill",
            self_token="self-token",
            party_session_id=1,
        )
        previous.boss_name = "Drill"
        tracker.end("VICTORY", 110_000_000_000)
        previous.settlement_status = "SETTLED"
        previous.encounter_duration_seconds = 10
        previous.participants = [
            dict(roster[0], damage=7_578_453, dps=757_845.3)
        ]
        current = tracker.begin(
            instance_id="npcap-bootstrap:57350161522149",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7115080,
            boss_token="next-drill",
            self_token="self-token",
            party_session_id=1,
        )
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            session_encounter_ids={
                previous.local_encounter_id,
                current.local_encounter_id,
            },
        )
        window.main_recent_battle_scope_started_ns = 90_000_000_000

        self.assertIs(
            window._main_recent_settlement_record(settled_only=True), previous
        )
        rows = window._settlement_main_rows(now=205)
        self.assertIn(
            "recent_boss", {row.get("row_kind") for row in rows}
        )
        self.assertTrue(
            any(row.get("total_value") == 7_578_453 for row in rows)
        )

    def test_real_instance_change_does_not_reuse_previous_settlement(self):
        window, _ = make_window()
        window.model.self_character_id = "self-token"
        roster = [{"id": "self-token", "iid": 1, "name": "Self"}]
        tracker = EncounterTracker()
        previous = tracker.begin(
            instance_id="old-real-instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
        )
        tracker.end("VICTORY", 110_000_000_000)
        previous.settlement_status = "SETTLED"
        previous.participants = [dict(roster[0], damage=100, dps=10)]
        current = tracker.begin(
            instance_id="new-real-instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
        )
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            session_encounter_ids={
                previous.local_encounter_id,
                current.local_encounter_id,
            },
        )

        self.assertIsNone(window._main_recent_settlement_record())

    def test_recent_rows_take_dps_and_total_from_the_same_latest_settlement(self):
        window, members = make_window()
        window.model.started = False
        window.model.party_active = True
        window.model.party_member_count = 2
        members.clear()
        members.update({1, 2})
        window.model.friend_order = [1, 2]
        window.model.actor_character_ids = {
            1: "self-token",
            2: "peer-token",
        }
        window.model.self_character_id = "self-token"
        roster = [
            {
                "id": "self-token",
                "iid": 1,
                "name": "Self",
                "profession_id": 1200001,
            },
            {
                "id": "peer-token",
                "iid": 2,
                "name": "Peer",
                "profession_id": 1200001,
            },
        ]
        tracker = EncounterTracker()
        older = tracker.begin(
            instance_id="instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7109821,
            boss_token="older-boss",
            self_token="self-token",
        )
        older.boss_name = "第一场"
        tracker.end("WIPE", 110_000_000_000)
        older.settlement_status = "SETTLED"
        older.participants = [
            dict(roster[0], damage=100_000, dps=10_000),
            dict(roster[1], damage=300_000, dps=30_000),
        ]

        latest = tracker.begin(
            instance_id="instance",
            started_at_ns=200_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7115042,
            boss_token="latest-boss",
            self_token="self-token",
        )
        latest.boss_name = "第二场"
        tracker.end("VICTORY", 220_000_000_000)
        latest.settlement_status = "SETTLED"
        latest.participants = [
            dict(roster[0], damage=222_000, dps=11_100),
            dict(roster[1], damage=444_000, dps=22_200),
        ]
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            session_encounter_ids={
                older.local_encounter_id,
                latest.local_encounter_id,
            },
        )

        rows = window._settlement_main_rows(now=225)

        self.assertEqual(rows[2]["boss_name"], "第二场")
        self.assertEqual(rows[0]["total_value"], 222_000)
        self.assertEqual(rows[0]["stat_value"], 11_100)
        peer = next(row for row in rows[3:] if row["actor_id"] == 2)
        self.assertEqual(peer["total_value"], 444_000)
        self.assertEqual(peer["stat_value"], 22_200)

    def test_completed_boss_rows_keep_kicked_members_while_live_roster_shrinks(self):
        window, members = make_window()
        window.main_recent_battle_expanded = True
        window.model.self_character_id = "self-token"
        window.model.actor_character_ids = {
            1: "self-token", 2: "peer-two", 3: "peer-three",
        }
        live_profiles = [
            dict(actor_id=actor, display_name=name, profession_id=1200001,
                 extraordinary_rating=80_000)
            for actor, name in ((1, "本人"), (2, "队友二"), (3, "队友三"))
        ]
        window._team_rating_preview_rows = lambda: list(live_profiles)
        roster = [
            dict(id=token, iid=actor, name=name, profession_id=1200001)
            for actor, token, name in ((1, "self-token", "本人"),
                                       (2, "peer-two", "队友二"),
                                       (3, "peer-three", "队友三"))
        ]
        tracker = EncounterTracker()
        completed = tracker.begin(
            instance_id="same-dungeon", started_at_ns=100_000_000_000,
            participants_snapshot=roster, boss_template_id=7109821,
            self_token="self-token",
        )
        completed.boss_name = "第一只 Boss"
        tracker.end("VICTORY", 110_000_000_000)
        completed.settlement_status = "SETTLED"
        completed.participants = [
            dict(row, damage=damage, dps=damage / 10)
            for row, damage in zip(roster, (100_000, 200_000, 300_000))
        ]
        tracker.begin(
            instance_id="same-dungeon", started_at_ns=200_000_000_000,
            participants_snapshot=roster, boss_template_id=7115042,
            self_token="self-token",
        )
        window.settlement_ui = SimpleNamespace(tracker=tracker)

        before = window._settlement_main_rows(now=205)
        before_history = {row["actor_id"]: row["total_value"]
                          for row in before if row.get("is_recent_battle")}
        self.assertEqual(before_history, {1: 100_000, 2: 200_000, 3: 300_000})

        live_profiles.pop()
        members.intersection_update({1, 2})
        window.model.party_member_count = 2
        after = window._settlement_main_rows(now=215)
        after_history = {row["actor_id"]: row["total_value"]
                         for row in after if row.get("is_recent_battle")}
        self.assertEqual(after_history, before_history)
        self.assertEqual(len(window._main_team_rating_section_rows()), 2)

    def test_recent_battle_ai_without_numeric_score_always_shows_ai_label(self):
        window, _ = make_window()
        roster = [
            {
                "id": "self-token",
                "iid": 1,
                "name": "Self",
                "profession_id": 1200001,
                "is_ai": False,
            },
            {
                "id": "projection-token",
                "iid": 7,
                "name": "Projection",
                "profession_id": 1200003,
                "is_ai": True,
            },
        ]
        tracker = EncounterTracker()
        record = tracker.begin(
            instance_id="instance",
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            self_token="self-token",
        )
        tracker.end("WIPE", 110_000_000_000)
        record.settlement_status = "SETTLED"
        record.participants = [
            dict(roster[0], damage=100_000, dps=10_000),
            dict(roster[1], damage=200_000, dps=20_000),
        ]

        rows = window._settlement_record_main_rows(record)
        ai_row = next(row for row in rows if row["actor_id"] == 7)

        self.assertTrue(ai_row["is_ai"])
        self.assertEqual(ai_row["inline_rating_text"], "人机")

    def test_settlement_rows_fall_back_to_live_score_when_snapshot_lacks_it(self):
        window, _ = make_window()
        window.model.started = False
        window.model.entity_extraordinary_ratings = {2: 104148}
        roster = [
            {
                "id": "self-token", "iid": 1, "name": "Self",
                "profession_id": 1200001, "is_ai": False,
            },
            {
                "id": "peer-token", "iid": 2, "name": "墨爵",
                "profession_id": 1200003, "is_ai": False,
            },
        ]
        tracker = EncounterTracker()
        record = tracker.begin(
            instance_id="instance", started_at_ns=100_000_000_000,
            participants_snapshot=roster, self_token="self-token",
        )
        tracker.end("VICTORY", 110_000_000_000)
        record.settlement_status = "SETTLED"
        record.participants = [
            dict(roster[0], damage=100_000, dps=10_000),
            dict(roster[1], damage=200_000, dps=20_000),
        ]

        rows = window._settlement_record_main_rows(record)
        peer = next(row for row in rows if row["actor_id"] == 2)

        self.assertEqual(peer["rating"], 104148)
        self.assertEqual(peer["rating_text"], "104148")
        self.assertEqual(peer["inline_rating_text"], "104148")

    def test_settlement_rating_persistence_only_fills_current_session_gaps(self):
        window, _ = make_window()
        window.model.entity_extraordinary_ratings = {2: 104148}
        roster = [
            {"id": "peer-token", "iid": 2, "name": "Peer"},
        ]
        tracker = EncounterTracker()
        old = tracker.begin(
            instance_id="old", started_at_ns=100_000_000_000,
            participants_snapshot=roster, self_token="peer-token",
        )
        tracker.end("VICTORY", 110_000_000_000)
        current = tracker.begin(
            instance_id="current", started_at_ns=200_000_000_000,
            participants_snapshot=roster, self_token="peer-token",
        )
        tracker.end("VICTORY", 210_000_000_000)
        saved = []
        window.settlement_ui = SimpleNamespace(
            session_encounter_ids=set(),
            repository=SimpleNamespace(save=lambda value: saved.append(value)),
        )
        self.assertFalse(
            window._enrich_settlement_ratings_from_live_profiles(tracker)
        )
        self.assertIsNone(old.participants_snapshot[0].get("extraordinary_rating"))

        window.settlement_ui.session_encounter_ids.add(current.local_encounter_id)
        self.assertTrue(
            window._enrich_settlement_ratings_from_live_profiles(tracker)
        )
        self.assertEqual(current.participants_snapshot[0]["extraordinary_rating"], 104148)
        self.assertIsNone(old.participants_snapshot[0].get("extraordinary_rating"))
        self.assertEqual(len(saved), 1)

        window.model.entity_extraordinary_ratings[2] = 105000
        self.assertFalse(
            window._enrich_settlement_ratings_from_live_profiles(tracker)
        )
        self.assertEqual(current.participants_snapshot[0]["extraordinary_rating"], 104148)
        self.assertEqual(len(saved), 1)

    def test_rating_and_deaths_default_on_without_overwriting_explicit_choices(self):
        from test_combat_model import migrate_main_display_config
        fresh, _ = migrate_main_display_config({})
        self.assertTrue(fresh['team_rating_preview'])
        self.assertTrue(fresh['show_deaths'])
        configured, _ = migrate_main_display_config(dict(team_rating_preview=False, show_deaths=False))
        self.assertFalse(configured['team_rating_preview'])
        self.assertFalse(configured['show_deaths'])

    def test_settlement_layout_puts_self_rating_after_name_without_rating_tab(self):
        window, _ = make_window()
        window.model.started = False
        window.model.entity_extraordinary_ratings = {1: 80_001}
        window.settlement_ui = SimpleNamespace(tracker=SimpleNamespace(
            current_id=None, encounters={}
        ))
        rows, rating_preview = window._main_display_rows()
        self.assertFalse(rating_preview)
        self.assertTrue(rows[0]['is_live_self'])
        self.assertEqual(rows[0]['inline_rating_text'], '80001')
        self.assertEqual(rows[1]['row_kind'], 'section')
        self.assertNotIn('active_tab', rows[1])

    def test_current_party_lower_section_includes_live_self_rating(self):
        window, members = make_window()
        members.clear()
        members.update({1, 2})
        window.model.friend_order = [1, 2]
        window.model.party_active = True
        window.model.party_member_count = 2
        window.model.actor_character_ids = {
            1: "self-token",
            2: "peer-token",
        }
        window.model.self_character_id = "self-token"
        window.model.entity_extraordinary_ratings = {
            1: 80_616,
            2: 88_893,
        }
        window.settlement_ui = SimpleNamespace(
            tracker=SimpleNamespace(current_id=None, encounters={}),
            session_encounter_ids=set(),
        )

        party_rows = window._main_team_rating_section_rows()

        self.assertEqual(
            [row["actor_id"] for row in party_rows],
            [1, 2],
        )
        self.assertEqual(
            [row["inline_rating_text"] for row in party_rows],
            ["80616", "88893"],
        )

    def test_no_active_party_clears_the_entire_lower_section(self):
        window, members = make_window()
        members.clear()
        members.add(1)
        window.model.friend_order = [1]
        window.model.party_active = False
        window.model.party_member_count = 1
        window.model.actor_character_ids = {1: "self-token"}
        window.model.self_character_id = "self-token"
        window.model.entity_extraordinary_ratings = {1: 80_616}
        window.settlement_ui = SimpleNamespace(
            tracker=SimpleNamespace(current_id=None, encounters={}),
            session_encounter_ids=set(),
        )

        rows = window._settlement_main_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["section_text"], "实时战斗/战斗记录")
        self.assertEqual(rows[2]["row_kind"], "message")
        self.assertEqual(rows[0]["actor_id"], window.model.self_id)
        self.assertEqual(rows[0]["inline_rating_text"], "80616")

    def test_boss_level_uses_only_the_current_monster_metadata(self):
        window, _ = make_window()
        monster = SimpleNamespace(name='子爵夫人', level=72, current_hp=50000000, max_hp=66735474)
        self.assertEqual(window._main_boss_display_values(monster)['boss_level'], 72)
        for level in (None, 0, 'unknown', -1, 1000):
            monster.level = level
            self.assertIsNone(window._main_boss_display_values(monster)['boss_level'])

    def test_observed_hp_restores_maximum_and_percent_when_rpc_omits_max_hp(self):
        window, _ = make_window()
        monster = SimpleNamespace(
            name='异化猎犬', level=62, template_id=7109821,
            current_hp=30_000_000, max_hp=None,
            observed_max_hp=34_778_940,
        )
        values = window._main_boss_display_values(monster)
        self.assertAlmostEqual(values['boss_ratio'], 30_000_000 / 34_778_940)
        self.assertIn('/', values['boss_hp'])
        self.assertIn('3000', values['boss_hp'])
        self.assertEqual(values['boss_percent'], '86.3%')

    def test_live_boss_uses_repeated_hp_peak_when_first_packet_is_damaged(self):
        window, _ = make_window()
        window._boss_max_hp_references = {(7_100_471, 74): 36_519_704}
        monster = MonsterStats(
            entity_id=246332481014948,
            name='西尔维娅',
            level=74,
            template_id=7_100_471,
            current_hp=29_365_054,
            observed_max_hp=35_971_650,
        )

        values = window._main_boss_display_values(monster)

        self.assertEqual(values['boss_percent'], '80.4%')
        self.assertAlmostEqual(values['boss_ratio'], 29_365_054 / 36_519_704)
        self.assertIn('3651.97', values['boss_hp'])

        monster.max_hp = 37_000_000
        self.assertAlmostEqual(
            window._main_boss_display_values(monster)['boss_ratio'],
            29_365_054 / 37_000_000,
        )

    def test_midfight_start_uses_confirmed_hp_reference_below_ninety_five_percent(self):
        window, _ = make_window()
        window._boss_max_hp_references = {(7_115_080, 63): 7_578_453}
        monster = MonsterStats(
            entity_id=220_161_634_656_540,
            name='Memory Boss',
            level=63,
            template_id=7_115_080,
            current_hp=3_363_055,
            observed_max_hp=6_988_788,
        )

        values = window._main_boss_display_values(monster)

        self.assertEqual(values['boss_percent'], '44.4%')
        self.assertAlmostEqual(values['boss_ratio'], 3_363_055 / 7_578_453)

    def test_recent_boss_uses_stage_identity_without_faking_template_or_hp(self):
        window, _ = make_window()
        window.history_store = SimpleNamespace(load=lambda _encounter_id: None)
        encounter = SimpleNamespace(
            local_encounter_id='final-stage',
            boss_name=None,
            boss_template_id=None,
            stage_id=5150060,
        )

        row = window._main_recent_boss_row(encounter)

        self.assertEqual(row['boss_name'], '星象仪者')
        self.assertEqual(row['boss_icon'], 'astrologer.png')
        self.assertEqual(row['boss_template_id'], 0)
        self.assertEqual(row['boss_hp'], '')
        self.assertEqual(row['boss_percent'], '')
        self.assertIsNone(row['boss_ratio'])

    def test_confirmed_victory_overrides_stale_live_boss_health(self):
        window, _ = make_window()
        window.model.started = False
        window.model.combat_end_reason = 'target_defeated'
        boss = SimpleNamespace(
            entity_id=90,
            name='Boss',
            template_id=7_109_821,
            current_hp=500,
            max_hp=1_000,
            last_hp_update_100ns=0,
        )
        window.model.current_monster = lambda: boss
        window.model.current_bosses = lambda: [boss]
        window.model.entity_combat_states = {90: False}
        retained = completed_record()
        retained['monster']['current_hp'] = 500
        window.main_last_battle_result = retained
        window.main_manual_clear_state = None
        window.main_cleared_encounter_ids = []
        window.pve_hud_view = 'recent_battle'
        window.main_time_text = '00:00'
        window.team_dps_text = '0'
        window.main_scroll_offset = 0
        window.main_visible_rows = 10
        window.main_ui_scale = 1.0
        window.window_dpi = 96
        window.show_combat_time = True
        window.show_boss_hp_bar = True
        window.show_main_totals = True
        window.show_deaths = True
        window.show_team_dps = True
        window.show_pvp_button = True
        window.highlight_self = True
        window.main_row_mask_opacity = 0
        window.window_locked = False
        window.config = {}

        snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot['boss_percent'], '0%')
        self.assertTrue(snapshot['boss_hp'].startswith('0 /'))

    def test_packet_terminal_zero_overrides_stale_model_health_before_settlement(self):
        window, _ = make_window()
        boss = SimpleNamespace(
            entity_id=90,
            name='Boss',
            template_id=7_109_821,
            current_hp=68_739,
            max_hp=31_941_215,
            last_hp_update_100ns=0,
        )
        window.model.current_monster = lambda: boss
        window.model.current_bosses = lambda: [boss]
        window.model.entity_combat_states = {90: False}
        window.model.combat_end_reason = ''
        window.model.pending_active_boss_id = None
        window.live_hud_boss_state = {
            'active': False,
            'terminal_zero': True,
            'entity_id': 90,
            'template_id': 7_109_821,
            'current_hp': 0.0,
            'ended_at_epoch': 1_000.0,
        }
        window.live_hud_combat_state = None
        window.live_hud_dps_segment = None
        window.main_last_battle_result = None
        window.main_manual_clear_state = None
        window.main_cleared_encounter_ids = []
        window.pve_hud_view = 'recent_battle'
        window.main_time_text = '02:51'
        window.team_dps_text = '0'
        window.main_scroll_offset = 0
        window.main_visible_rows = 10
        window.main_ui_scale = 1.0
        window.window_dpi = 96
        window.show_combat_time = True
        window.show_boss_hp_bar = True
        window.show_main_totals = True
        window.show_deaths = True
        window.show_team_dps = True
        window.show_pvp_button = True
        window.highlight_self = True
        window.main_row_mask_opacity = 0
        window.window_locked = False

        snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot['boss_percent'], '0%')
        self.assertTrue(snapshot['boss_hp'].startswith('0 /'))

    def test_recent_victory_zeroes_archived_positive_health_sample(self):
        window, _ = make_window()
        tracker = EncounterTracker()
        encounter = tracker.begin(
            instance_id='instance',
            started_at_ns=100_000_000_000,
            participants_snapshot=[{'id': 'self', 'iid': 1, 'name': 'Self'}],
            self_token='self',
            boss_template_id=7_109_821,
            boss_token='entity:90',
        )
        tracker.end('VICTORY', 130_000_000_000)
        encounter.settlement_status = 'SETTLED'
        encounter.boss_name = 'Boss'
        encounter.boss_current_hp = 750
        encounter.boss_max_hp = 1_000
        window.history_store = SimpleNamespace(
            load=lambda _encounter_id: {
                'monster': {
                    'entity_id': 90,
                    'name': 'Boss',
                    'template_id': 7_109_821,
                    'current_hp': 750,
                    'max_hp': 1_000,
                }
            }
        )

        monster = window._main_recent_settlement_monster(encounter)
        values = window._main_boss_display_values(monster)

        self.assertEqual(values['boss_percent'], '0%')
        self.assertEqual(values['boss_ratio'], 0.0)

    def test_nearly_defeated_live_boss_does_not_display_zero_percent(self):
        window, _ = make_window()
        monster = SimpleNamespace(
            entity_id=90,
            name='Boss',
            template_id=7_100_215,
            current_hp=2_422,
            max_hp=59_392_271,
        )

        values = window._main_boss_display_values(monster)

        self.assertEqual(values['boss_percent'], '<0.1%')
        self.assertTrue(values['boss_hp'].startswith('2,422 /'))

    def test_real_wire_boss_token_matches_confirmed_victory(self):
        window, _ = make_window()
        wire_token = 'arySSqyWHxYtctkg'
        ended_at_ns = 1790743280709017500
        encounter = SimpleNamespace(
            result='VICTORY',
            boss_template_id=7_100_215,
            boss_token=wire_token,
            boss_name='Boss',
            ended_at_ns=ended_at_ns,
        )
        window.settlement_ui = SimpleNamespace(
            boss_entities={'90': {'token': wire_token}},
        )
        monster = SimpleNamespace(
            entity_id=90,
            name='Boss',
            template_id=7_100_215,
            last_hp_update_100ns=ended_at_ns // 100 + 116_444_736_000_000_000,
        )

        self.assertTrue(window._main_recent_victory_matches_live_boss(encounter, monster))
        window.settlement_ui.boss_entities['90']['token'] = 'new-pull-token'
        self.assertFalse(window._main_recent_victory_matches_live_boss(encounter, monster))

    def test_archived_entity_matches_victory_when_wire_entity_map_is_missing(self):
        window, _ = make_window()
        encounter = SimpleNamespace(
            local_encounter_id='completed-boss',
            result='VICTORY',
            boss_template_id=7_100_215,
            boss_token='arydJ7bSuqkfSnlH',
            boss_name='Boss',
            ended_at_ns=130_000_000_000,
        )
        window.settlement_ui = SimpleNamespace(boss_entities={})
        window.history_store = SimpleNamespace(
            load=lambda _encounter_id: {
                'monster': {'entity_id': 90, 'template_id': 7_100_215}
            }
        )
        monster = SimpleNamespace(
            entity_id=90,
            name='Boss',
            template_id=7_100_215,
            last_hp_update_100ns=116_444_736_000_000_000 + 1_310_000_000,
        )

        self.assertTrue(window._main_recent_victory_matches_live_boss(encounter, monster))
        monster.entity_id = 91
        self.assertFalse(window._main_recent_victory_matches_live_boss(encounter, monster))

    def test_confirmed_wire_victory_zeroes_hud_with_stale_live_fight_state(self):
        window, _ = make_window()
        wire_token = 'arySSqyWHxYtctkg'
        tracker = EncounterTracker()
        encounter = tracker.begin(
            instance_id='instance',
            started_at_ns=100_000_000_000,
            participants_snapshot=[{'id': 'self', 'iid': 1, 'name': 'Self'}],
            self_token='self',
            boss_template_id=7_100_215,
            boss_token=wire_token,
        )
        tracker.end('VICTORY', 130_000_000_000)
        encounter.settlement_status = 'SETTLED'
        encounter.boss_name = 'Boss'
        encounter.boss_current_hp = 0
        encounter.boss_max_hp = 59_392_271
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            instance_id='instance',
            session_encounter_ids={encounter.local_encounter_id},
            boss_entities={'90': {'token': wire_token}},
        )
        window.history_store = SimpleNamespace(load=lambda _encounter_id: None)
        window.model.first_damage_time = 100.0
        window.model.self_character_id = 'self'
        window.model.entity_combat_states = {90: True}
        monster = SimpleNamespace(
            entity_id=90,
            name='Boss',
            template_id=7_100_215,
            current_hp=2_422,
            max_hp=59_392_271,
            last_hp_update_100ns=116_444_736_000_000_000 + 1_290_000_000,
        )
        window.model.current_monster = lambda: monster
        window.model.current_bosses = lambda: [monster]
        window.main_last_battle_result = None
        window.main_manual_clear_state = None
        window.main_cleared_encounter_ids = []
        window.main_recent_battle_scope_started_ns = 0
        window.pve_hud_view = 'recent_battle'
        window.main_time_text = '02:44'
        window.team_dps_text = '0'
        window.main_scroll_offset = 0
        window.main_visible_rows = 10
        window.main_ui_scale = 1.0
        window.window_dpi = 96
        window.show_combat_time = True
        window.show_boss_hp_bar = True
        window.show_main_totals = True
        window.show_deaths = True
        window.show_team_dps = True
        window.show_pvp_button = True
        window.highlight_self = True
        window.main_row_mask_opacity = 0
        window.window_locked = False
        window.config = {}

        snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot['boss_percent'], '0%')
        self.assertTrue(snapshot['boss_hp'].startswith('0 /'))

    def test_delayed_victory_freezes_the_bound_model_encounter(self):
        window, _ = make_window()
        calls = []
        encounter = SimpleNamespace(
            result='VICTORY',
            ended_at_ns=130_000_000_000,
        )
        window.model.encounter_id = 'local-encounter'
        window.model.freeze_verified_encounter = (
            lambda ended_at_ns, result: calls.append((ended_at_ns, result)) or True
        )
        window.settlement_history_adapter = SimpleNamespace(
            local_bindings={'local-encounter': 'tracked-encounter'}
        )
        controller = SimpleNamespace(
            tracker=SimpleNamespace(
                encounters={'tracked-encounter': encounter}
            )
        )

        self.assertTrue(window._freeze_model_for_victory_settlement(controller))
        self.assertEqual(calls, [(130_000_000_000, 'VICTORY')])

        encounter.result = 'WIPE'
        self.assertFalse(window._freeze_model_for_victory_settlement(controller))

    def test_previous_victory_token_cannot_zero_a_new_same_template_boss(self):
        window, _ = make_window()
        encounter = SimpleNamespace(
            result='VICTORY',
            boss_template_id=7_109_821,
            boss_token='entity:90',
            boss_name='Boss',
            ended_at_ns=130_000_000_000,
        )
        new_boss = SimpleNamespace(
            entity_id=91,
            template_id=7_109_821,
            name='Boss',
            last_hp_update_100ns=0,
        )

        self.assertFalse(
            window._main_recent_victory_matches_live_boss(encounter, new_boss)
        )

    def test_six_member_combat_does_not_import_four_empty_counter_slots(self):
        window, members = make_window()
        before = set(members)
        rows = window._main_combat_display_rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual({row['actor_id'] for row in rows}, set(range(1, 7)))
        self.assertEqual(members, before)
        self.assertEqual([row['actor_id'] for row in rows], list(range(1, 7)))

    def test_main_combat_rows_are_not_truncated_at_twelve_members(self):
        window, members = make_window()
        actors = []
        for actor_id in range(1, 51):
            members.add(actor_id)
            window.model.entity_names[actor_id] = f"闃熷憳{actor_id}"
            window.model.entity_professions[actor_id] = 1200001
            actor = ActorStats(actor_id=actor_id)
            actor.damage = 1_000_000 - actor_id
            actors.append(actor)
        window.model.current_stats = lambda: actors
        window.model.encounter_member_ids = set(range(1, 51))

        rows = window._main_combat_display_rows()

        self.assertEqual(len(rows), 50)
        self.assertEqual({row["actor_id"] for row in rows}, set(range(1, 51)))

    def test_server_team_snapshot_prioritizes_live_team_rows(self):
        window, _ = make_window()
        window.settlement_ui = SimpleNamespace(tracker=EncounterTracker())
        window.model.team_damage_states = {
            actor_id: TeamDamageState(
                actor_id=actor_id,
                has_snapshot=True,
                authoritative_snapshot=True,
                accepted_damage=700_000 - actor_id * 70_000,
                snapshot_time_100ns=200_000_000_000,
                live_dps=(700_000 - actor_id * 70_000) / 10,
                live_dps_damage=700_000 - actor_id * 70_000,
            )
            for actor_id in range(1, 7)
        }

        rows, rating_preview = window._main_display_rows(now=205)

        self.assertFalse(rating_preview)
        self.assertTrue(rows[0]["is_live_self"])
        self.assertEqual(rows[0]["actor_id"], 1)
        self.assertEqual(rows[1]["row_kind"], "section")
        self.assertEqual(
            [row["actor_id"] for row in rows[2:]], list(range(1, 7))
        )
        self.assertEqual(rows[2]["total_value"], 630_000)
        self.assertEqual(rows[2]["stat_value"], 63_000)
        self.assertEqual(rows[2]["dps_value"], 63_000)

    def test_live_team_snapshot_without_self_keeps_live_self_pinned(self):
        window, _ = make_window()
        window.settlement_ui = SimpleNamespace(tracker=EncounterTracker())
        window.model.team_damage_states = {
            2: TeamDamageState(
                actor_id=2,
                has_snapshot=True,
                authoritative_snapshot=True,
                accepted_damage=560_000,
                snapshot_time_100ns=200_000_000_000,
                live_dps=56_000,
                live_dps_damage=560_000,
            )
        }

        rows, rating_preview = window._main_display_rows(now=205)

        self.assertFalse(rating_preview)
        self.assertTrue(rows[0]["is_live_self"])
        self.assertEqual(rows[0]["actor_id"], 1)
        self.assertEqual(rows[0]["total_value"], 630_000)
        self.assertEqual(rows[1]["row_kind"], "section")
        self.assertEqual([row["actor_id"] for row in rows[2:]], [2])

    def test_empty_live_team_snapshot_keeps_self_and_previous_result(self):
        window, _ = make_window()
        expected = [
            {"actor_id": 1, "is_live_self": True},
            {"row_kind": "section"},
            {"row_kind": "recent_boss", "boss_name": "上一只Boss"},
            {"actor_id": 2, "is_recent_battle": True},
        ]
        window._main_authoritative_live_team_rows = lambda _now=None: []
        window._settlement_main_rows = lambda _now=None: expected

        rows, rating_preview = window._main_display_rows(now=205)

        self.assertFalse(rating_preview)
        self.assertIs(rows, expected)

    def test_targetless_team_snapshot_prioritizes_live_rows_and_no_boss_header(self):
        window, _ = make_window()
        actors = []
        for actor_id, damage in ((1, 320_000), (2, 180_000)):
            actor = ActorStats(actor_id=actor_id)
            actor.damage = damage
            actors.append(actor)
        window.model.started = False
        window.model.targetless_team_statistics = lambda _now=None: actors
        window.model.targetless_team_duration = lambda _now=None: 10.0
        window.model.targetless_team_states = {
            1: SimpleNamespace(live_dps=40_000.0),
            2: SimpleNamespace(live_dps=20_000.0),
        }
        window.model.scene_id = 5_200_138
        window.model.current_dungeon_id = 5_100_052
        window.live_hud_combat_state = None
        window.pve_hud_view = "recent_battle"
        window.main_last_battle_result = completed_record()

        rows, rating_preview = window._main_display_rows(now=100.0)
        snapshot = window._layered_main_snapshot()

        self.assertFalse(rating_preview)
        self.assertTrue(rows[0]["is_live_self"])
        self.assertEqual(rows[1]["row_kind"], "section")
        self.assertEqual([row["actor_id"] for row in rows[2:]], [1, 2])
        self.assertEqual(rows[0]["stat_value"], 40_000)
        self.assertEqual(rows[0]["dps_value"], 40_000)
        self.assertEqual(rows[0]["total_value"], 320_000)
        self.assertNotIn("boss_mode_only", snapshot)
        self.assertTrue(snapshot["boss_available"])
        self.assertEqual(
            snapshot["boss_name"], "当前没有Boss · 小怪战斗"
        )
        self.assertEqual(snapshot["boss_icon"], "live-small-monsters-v2.png")
        self.assertTrue(snapshot["live_no_boss"])
        self.assertEqual(snapshot["boss_hp"], "")
        self.assertEqual(snapshot["boss_percent"], "")
        self.assertIsNone(snapshot["boss_ratio"])
        self.assertEqual(snapshot["time"], "00:10")
        self.assertEqual(snapshot["team_dps"], "105,000")
        section = next(
            row for row in snapshot["rows"] if row.get("row_kind") == "section"
        )
        self.assertEqual(section["live_team_dps"], "60,000/s")

    def test_finished_small_monster_projection_keeps_recent_battle_visible(self):
        window, _ = make_window()
        actors = []
        for actor_id, damage in ((1, 320_000), (2, 180_000)):
            actor = ActorStats(actor_id=actor_id)
            actor.damage = damage
            actors.append(actor)
        window.model.started = False
        window.model.targetless_team_statistics = lambda _now=None: actors
        window.model.targetless_team_duration = lambda _now=None: 10.0
        window.model.targetless_team_states = {
            1: SimpleNamespace(live_dps=40_000.0),
            2: SimpleNamespace(live_dps=20_000.0),
        }
        window.live_hud_combat_state = None
        window.live_hud_dps_segment = {
            "kind": "small_monsters",
            "active": False,
            "started_at_epoch": 90.0,
            "ended_at_epoch": 100.0,
            "elapsed_seconds": 10.0,
            "rows": [],
        }
        window.pve_hud_view = "recent_battle"
        window.main_last_battle_result = completed_record()

        snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot["boss_name"], "Boss")
        self.assertFalse(snapshot.get("live_no_boss", False))
        self.assertEqual(snapshot["time"], "00:30")
        self.assertEqual(snapshot["team_dps"], "105,000")
        self.assertEqual(
            [
                row["actor_id"] for row in snapshot["rows"]
                if row.get("actor_id") is not None
            ],
            list(range(1, 7)),
        )
        section = next(
            row for row in snapshot["rows"] if row.get("row_kind") == "section"
        )
        self.assertEqual(section["live_team_dps"], "--")

    def test_non_live_combat_does_not_show_small_monster_header(self):
        window, _ = make_window()
        window.live_hud_combat_state = None
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window.pve_hud_view = "recent_battle"
        window.main_last_battle_result = completed_record()

        snapshot = window._layered_main_snapshot()

        self.assertFalse(snapshot.get("live_no_boss", False))
        self.assertNotEqual(
            snapshot.get("boss_name"), "当前没有Boss · 小怪战斗"
        )

    def test_live_small_monster_projection_does_not_override_model_boss(self):
        window, _ = make_window()
        boss = MonsterStats(
            entity_id=220_136_938_647_578,
            template_id=7_100_215,
            name="子嗣守护",
            level=73,
            current_hp=81_000_000,
            max_hp=119_018_657,
        )
        window.model.current_monster = lambda: boss
        window.model.current_bosses = lambda: [boss]
        window.model.entity_combat_states = {boss.entity_id: True}
        window.model.team_damage_states = {
            actor_id: TeamDamageState(
                actor_id=actor_id,
                has_snapshot=True,
                authoritative_snapshot=True,
                accepted_damage=700_000 - actor_id * 70_000,
                snapshot_time_100ns=200_000_000_000,
                live_dps=(700_000 - actor_id * 70_000) / 10,
                live_dps_damage=700_000 - actor_id * 70_000,
            )
            for actor_id in range(1, 7)
        }
        window.model.targetless_team_statistics = lambda _now=None: []
        window.model.targetless_team_duration = lambda _now=None: 0.0
        window.live_hud_combat_state = {
            "active": True,
            "segment_id": 3,
            "started_at_epoch": 1_000.0,
        }
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = {
            "kind": "small_monsters",
            "active": True,
            "started_at_epoch": 1_000.0,
            "active_started_at_epoch": 1_000.0,
            "ended_at_epoch": 0.0,
            "elapsed_seconds": 12.0,
            "rows": [
                {
                    "actor_id": 91,
                    "name": "旧小怪缓存",
                    "metric": "dps",
                    "stat_value": 9_999,
                    "total_value": 99_999,
                    "dps_value": 9_999,
                    "targetless_team_live": True,
                }
            ],
        }
        window.pve_hud_view = "recent_battle"
        retained = completed_record()
        window.main_last_battle_result = retained
        window.main_time_text = "00:42"

        snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot["boss_name"], "子嗣守护")
        self.assertEqual(snapshot["boss_template_id"], 7_100_215)
        self.assertAlmostEqual(
            snapshot["boss_ratio"], 81_000_000 / 119_018_657
        )
        self.assertFalse(snapshot.get("live_no_boss", False))
        self.assertEqual(snapshot["time"], "00:42")
        self.assertEqual(snapshot["team_dps"], "105,000")
        self.assertEqual(
            [
                row["actor_id"] for row in snapshot["rows"]
                if row.get("actor_id") is not None
            ],
            [1, *range(1, 7)],
        )
        section = next(
            row for row in snapshot["rows"] if row.get("row_kind") == "section"
        )
        self.assertEqual(section["live_team_dps"], "273,000/s")
        self.assertTrue(
            all(not row.get("targetless_team_live") for row in snapshot["rows"])
        )
        self.assertIs(window.main_last_battle_result, retained)

    def test_targetless_team_snapshot_uses_live_hud_boss_and_fight_clock(self):
        window, _ = make_window()
        actors = []
        for actor_id, damage in ((1, 320_000), (2, 180_000)):
            actor = ActorStats(actor_id=actor_id)
            actor.damage = damage
            actors.append(actor)
        window.model.started = False
        window.model.targetless_team_statistics = lambda _now=None: actors
        window.model.targetless_team_duration = lambda _now=None: 10.0
        window.model.targetless_team_states = {
            1: SimpleNamespace(live_dps=40_000.0),
            2: SimpleNamespace(live_dps=20_000.0),
        }
        window.pve_hud_view = "recent_battle"
        retained = completed_record()
        window.main_last_battle_result = retained
        window.live_hud_boss_state = {
            "active": True,
            "entity_id": 233_115_793_081_227,
            "template_id": 7_115_718,
            "name": "卡尔·埃德加",
            "icon": "karl-edgar.png",
            "level": 83,
            "current_hp": 14_092_149.0,
            "max_hp": 14_095_294.0,
            "started_at_epoch": 1_000.0,
        }
        clock = DpsWindow._main_display_time_text.__globals__["time"]

        with mock.patch.object(clock, "time", return_value=1_037.0):
            snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot["boss_name"], "卡尔·埃德加")
        self.assertEqual(snapshot["boss_template_id"], 7_115_718)
        self.assertEqual(snapshot["boss_icon"], "karl-edgar.png")
        self.assertEqual(snapshot["boss_level"], 83)
        self.assertTrue(snapshot["live_hud_boss"])
        self.assertFalse(snapshot.get("live_no_boss", False))
        self.assertAlmostEqual(
            snapshot["boss_ratio"], 14_092_149.0 / 14_095_294.0
        )
        self.assertEqual(snapshot["time"], "00:37")
        self.assertEqual(snapshot["team_dps"], "105,000")
        section = next(
            row for row in snapshot["rows"] if row.get("row_kind") == "section"
        )
        self.assertEqual(section["live_team_dps"], "60,000/s")
        self.assertIs(window.main_last_battle_result, retained)

    def test_live_hud_boss_keeps_model_enrage_prediction_visible(self):
        window, _ = make_window()
        boss = SimpleNamespace(
            entity_id=57_450_019_565_812,
            template_id=7_110_200,
            name="Boss",
            current_hp=109_003_818,
            max_hp=148_759_025,
        )
        window.model.current_monster = lambda: boss
        window.model.current_bosses = lambda: [boss]
        window.model.entity_combat_states = {boss.entity_id: True}
        window.live_hud_boss_state = {
            "active": True,
            "entity_id": boss.entity_id,
            "template_id": boss.template_id,
            "boss_type": 3,
            "name": boss.name,
            "current_hp": boss.current_hp,
            "max_hp": boss.max_hp,
            "started_at_epoch": 1_000.0,
        }
        window.boss_enrage_prediction_enabled = True
        window.enrage_prediction_visible = True
        window.enrage_prediction = SimpleNamespace(
            state="danger",
            message="危险 · -7:16",
            calculating=False,
            enrage_seconds=600.0,
            time_to_enrage_seconds=476.0,
            schedule_start_hp_percent=100.0,
            schedule_end_hp_percent=2.0,
        )

        snapshot = window._layered_main_snapshot()

        self.assertEqual(
            snapshot["prediction"],
            {
                "state": "danger",
                "message": "危险 · -7:16",
                "marker": 0.02 + 476.0 / 600.0 * 0.98,
            },
        )

    def test_live_hud_boss_message_stays_out_of_combat_model(self):
        window = object.__new__(DpsWindow)
        renders = []
        window.model = SimpleNamespace()
        window._schedule_layered_main_render = lambda: renders.append(True)
        payload = {
            "active": True,
            "entity_id": 233_115_793_081_227,
            "template_id": 7_115_718,
            "started_at_epoch": 1_000.0,
        }

        window._dispatch_message("live_hud_boss", payload)

        self.assertEqual(window.live_hud_boss_state, payload)
        self.assertEqual(vars(window.model), {})
        window._dispatch_message("live_hud_boss", {"active": False})
        self.assertIsNone(window.live_hud_boss_state)
        self.assertEqual(renders, [True, True])

    def test_lambert_packet_confirmed_boss_replaces_small_monster_hud(self):
        window, _ = make_window()
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window._schedule_layered_main_render = lambda: None
        window._dispatch_message(
            "live_hud_combat",
            {
                "active": True,
                "segment_id": 1,
                "started_at_epoch": 1_000.0,
            },
        )
        window.live_hud_dps_segment["rows"] = [
            {"actor_id": 1, "total_value": 300_000}
        ]

        window._dispatch_message(
            "live_hud_boss",
            {
                "active": True,
                "entity_id": 242_053_620_061_587,
                "template_id": 7_115_703,
                "boss_type": 3,
                "name": "战斗首领",
                "icon": "",
                "level": 87,
                "current_hp": 14_000_000.0,
                "max_hp": 14_800_926.0,
                "started_at_epoch": 1_012.0,
            },
        )

        self.assertEqual(window.live_hud_dps_segment["kind"], "boss")
        self.assertEqual(window.live_hud_dps_segment["rows"], [])
        with mock.patch.object(time, "time", return_value=1_013.0):
            snapshot = window._layered_main_snapshot()
        self.assertEqual(snapshot["boss_name"], "朗伯·绞索")
        self.assertEqual(snapshot["boss_template_id"], 7_115_703)
        self.assertEqual(snapshot["boss_level"], 87)
        self.assertTrue(snapshot["live_hud_boss"])
        self.assertAlmostEqual(
            snapshot["boss_ratio"], 14_000_000.0 / 14_800_926.0
        )

    def test_same_live_hud_boss_starts_a_new_segment_after_wipe(self):
        window, _ = make_window()
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window._schedule_layered_main_render = lambda: None
        first_pull = {
            "active": True,
            "entity_id": 220_136_938_647_578,
            "template_id": 7_100_215,
            "started_at_epoch": 1_000.0,
        }

        window._dispatch_message("live_hud_boss", first_pull)
        first_segment = window.live_hud_dps_segment
        first_segment["rows"] = [{"actor_id": 1, "total_value": 300_000}]
        window._dispatch_message(
            "live_hud_boss",
            {
                "active": False,
                "entity_id": first_pull["entity_id"],
                "ended_at_epoch": 1_180.0,
            },
        )
        window._dispatch_message(
            "live_hud_boss",
            dict(first_pull, started_at_epoch=1_200.0),
        )

        self.assertIsNot(window.live_hud_dps_segment, first_segment)
        self.assertEqual(window.live_hud_dps_segment["kind"], "boss")
        self.assertEqual(window.live_hud_dps_segment["started_at_epoch"], 1_200.0)
        self.assertEqual(window.live_hud_dps_segment["rows"], [])

    def test_live_hud_boss_starts_a_fresh_snapshot_cached_dps_segment(self):
        window, _ = make_window()
        windows_epoch = 116_444_736_000_000_000

        def filetime(epoch):
            return windows_epoch + int(epoch * 10_000_000)

        def state(actor_id, token, damage, server_time):
            return SimpleNamespace(
                actor_id=actor_id,
                user_token=token,
                total_damage=damage,
                server_time=server_time,
            )

        def statistics(_now=None):
            rows = []
            for actor_id, value in window.model.targetless_team_states.items():
                actor = ActorStats(actor_id=actor_id)
                actor.damage = value.total_damage
                rows.append(actor)
            return rows

        window.live_hud_combat_state = None
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window._schedule_layered_main_render = lambda: None
        window.model.targetless_team_states = {
            1: state(1, "self", 300_000, 100),
            2: state(2, "peer", 200_000, 100),
        }
        window.model.targetless_team_last_committed_time_100ns = filetime(1_000)
        window.model.targetless_team_statistics = statistics
        window.model.targetless_team_duration = lambda _now=None: 50.0

        window._dispatch_message(
            "live_hud_combat",
            {
                "active": True,
                "segment_id": 1,
                "started_at_epoch": 1_000.0,
            },
        )
        self.assertEqual(window._main_targetless_team_rows(1_001.0), [])

        window.model.targetless_team_states = {
            1: state(1, "self", 320_000, 100),
            2: state(2, "peer", 230_000, 100),
        }
        window.model.targetless_team_last_committed_time_100ns = filetime(1_005)
        small_rows = window._main_targetless_team_rows(1_005.0)
        small_by_actor = {row["actor_id"]: row for row in small_rows}
        self.assertEqual(small_by_actor[1]["total_value"], 20_000)
        self.assertEqual(small_by_actor[2]["total_value"], 30_000)
        self.assertEqual(small_by_actor[1]["dps_value"], 4_000.0)
        self.assertEqual(small_by_actor[2]["dps_value"], 6_000.0)

        held_rows = window._main_targetless_team_rows(1_011.0)
        self.assertEqual(
            [row["dps_value"] for row in held_rows],
            [row["dps_value"] for row in small_rows],
        )

        window._dispatch_message(
            "live_hud_boss",
            {
                "active": True,
                "entity_id": 220_096_136_477_298,
                "template_id": 7_115_731,
                "started_at_epoch": 1_012.0,
            },
        )
        self.assertEqual(window.live_hud_dps_segment["kind"], "boss")
        self.assertEqual(window._main_targetless_team_rows(1_012), [])

        window.model.targetless_team_states = {
            1: state(1, "self", 50_000, 200),
            2: state(2, "peer", 30_000, 200),
        }
        window.model.targetless_team_last_committed_time_100ns = filetime(1_014)
        boss_rows = window._main_targetless_team_rows(1_014.0)
        boss_by_actor = {row["actor_id"]: row for row in boss_rows}
        self.assertEqual(boss_by_actor[1]["total_value"], 50_000)
        self.assertEqual(boss_by_actor[2]["total_value"], 30_000)
        self.assertEqual(boss_by_actor[1]["dps_value"], 25_000.0)
        self.assertEqual(boss_by_actor[2]["dps_value"], 15_000.0)

        clock = DpsWindow._main_display_time_text.__globals__["time"]
        with mock.patch.object(clock, "time", return_value=1_016.0):
            self.assertEqual(window._main_display_time_text(), "00:04")
            frozen = window._main_targetless_team_rows(1_016.0)
        self.assertEqual(
            [row["dps_value"] for row in frozen],
            [row["dps_value"] for row in boss_rows],
        )

        window._dispatch_message(
            "live_hud_combat",
            {
                "active": False,
                "segment_id": 2,
                "started_at_epoch": 1_012.0,
                "ended_at_epoch": 1_015.0,
            },
        )
        window.model.targetless_team_states = {
            1: state(1, "self", 75_000, 200),
            2: state(2, "peer", 45_000, 200),
        }
        window.model.targetless_team_last_committed_time_100ns = filetime(1_017)
        final_rows = window._main_targetless_team_rows(1_017.0)
        final_by_actor = {row["actor_id"]: row for row in final_rows}
        self.assertEqual(final_by_actor[1]["dps_value"], 25_000.0)
        self.assertEqual(final_by_actor[2]["dps_value"], 15_000.0)
        with mock.patch.object(clock, "time", return_value=1_020.0):
            self.assertEqual(window._main_display_time_text(), "00:03")

    def test_boss_team_snapshot_subtracts_trash_after_model_clears_targetless(self):
        window, _ = make_window()
        windows_epoch = 116_444_736_000_000_000

        def filetime(epoch):
            return windows_epoch + int(epoch * 10_000_000)

        def state(actor_id, token, damage, server_time):
            return SimpleNamespace(
                actor_id=actor_id,
                user_token=token,
                total_damage=damage,
                last_absolute=damage,
                server_time=server_time,
            )

        window.live_hud_combat_state = None
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window._schedule_layered_main_render = lambda: None
        window.model.targetless_team_states = {
            1: state(1, "self", 300_000, 100),
            2: state(2, "peer", 200_000, 100),
        }
        window.model.targetless_team_last_committed_time_100ns = filetime(1_011)
        window._dispatch_message(
            "live_hud_boss",
            {
                "active": True,
                "entity_id": 220_096_136_477_298,
                "template_id": 7_115_731,
                "started_at_epoch": 1_012.0,
            },
        )

        # Boss promotion deliberately clears this model cache.  The live HUD
        # must still own the baseline it captured at the Boss edge.
        window.model.targetless_team_states = {}
        window.model.targetless_team_statistics = lambda _now=None: []
        window.model.targetless_team_duration = lambda _now=None: 0.0
        ingested = []
        window.model.ingest_team_stat = (
            lambda payload: ingested.append(dict(payload)) or False
        )
        tokens = ["self", "peer"]

        def snapshot(token, actor_id, damage):
            return {
                "filetime_100ns": filetime(1_014),
                "actor_id": actor_id,
                "user_token": token,
                "snapshot_tokens": tokens,
                "absolute_damage": damage,
                "server_time": 200,
                "full_snapshot": True,
            }

        window._dispatch_message("team_stat", snapshot("self", 1, 320_000))
        self.assertEqual(window._main_authoritative_live_team_rows(1_014), [])
        window._dispatch_message("team_stat", snapshot("peer", 2, 230_000))

        rows = window._main_authoritative_live_team_rows(1_014)
        by_actor = {row["actor_id"]: row for row in rows}
        self.assertEqual(by_actor[1]["total_value"], 20_000)
        self.assertEqual(by_actor[2]["total_value"], 30_000)
        self.assertEqual(by_actor[1]["dps_value"], 10_000.0)
        self.assertEqual(by_actor[2]["dps_value"], 15_000.0)
        self.assertEqual(
            window._main_live_team_dps_text(rows, now=1_014), "25,000/s"
        )
        self.assertEqual(len(ingested), 2)

    def test_single_boss_team_snapshot_without_trash_keeps_full_damage(self):
        window, _ = make_window()
        windows_epoch = 116_444_736_000_000_000

        def filetime(epoch):
            return windows_epoch + int(epoch * 10_000_000)

        window.live_hud_combat_state = None
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window._schedule_layered_main_render = lambda: None
        window.model.targetless_team_states = {}
        window.model.targetless_team_last_committed_time_100ns = 0
        window.model.targetless_team_statistics = lambda _now=None: []
        window.model.targetless_team_duration = lambda _now=None: 0.0
        window.model.ingest_team_stat = lambda _payload: False
        window._dispatch_message(
            "live_hud_boss",
            {
                "active": True,
                "entity_id": 220_096_136_477_298,
                "template_id": 7_115_731,
                "started_at_epoch": 2_000.0,
            },
        )
        tokens = ["self", "peer"]
        for token, actor_id, damage in (
            ("self", 1, 30_000),
            ("peer", 2, 20_000),
        ):
            window._dispatch_message(
                "team_stat",
                {
                    "filetime_100ns": filetime(2_010),
                    "actor_id": actor_id,
                    "user_token": token,
                    "snapshot_tokens": tokens,
                    "absolute_damage": damage,
                    "server_time": 300,
                    "full_snapshot": True,
                },
            )

        rows = window._main_targetless_team_rows(2_010)
        self.assertEqual(
            {row["actor_id"]: row["total_value"] for row in rows},
            {1: 30_000, 2: 20_000},
        )
        self.assertEqual(
            window._main_live_team_dps_text(rows, now=2_010), "5,000/s"
        )

    def test_small_monster_hud_uses_server_clock_with_local_time_24_seconds_slow(self):
        window = make_official_clock_window()
        official_clock_snapshot(window, 1_953, {1: 0, 2: 0}, server_time=1_962)
        window._dispatch_message(
            "live_hud_combat",
            {"active": True, "segment_id": 1, "started_at_epoch": 1_953.0},
        )
        official_clock_snapshot(
            window, 2_000, {1: 7_122_784, 2: 5_310_751}, server_time=1_962
        )

        rows = window._main_targetless_team_rows(2_000)
        by_actor = {row["actor_id"]: row for row in rows}
        self.assertEqual(by_actor[1]["total_value"], 7_122_784)
        self.assertAlmostEqual(by_actor[1]["dps_value"], 7_122_784 / 62)
        self.assertAlmostEqual(by_actor[2]["dps_value"], 5_310_751 / 62)
        self.assertEqual(window._main_live_hud_dps_duration(2_000), 62.0)
        self.assertEqual(window._main_live_hud_dps_duration(2_001), 63.0)
        self.assertEqual(window._main_targetless_team_rows(2_001), rows)

    def test_small_monster_live_self_survives_two_minutes_without_server_clock(self):
        window = make_official_clock_window()
        window.model.server_clock_offset_seconds = None
        window.model.self_id = 1
        window.model.entity_names.update({1: "观众", 2: "队友"})
        window.model.entity_professions.update({1: 1_200_002, 2: 1_200_001})
        window.settlement_ui = SimpleNamespace(tracker=EncounterTracker())
        window._dispatch_message(
            "live_hud_combat",
            {"active": True, "segment_id": 1, "started_at_epoch": 1_000.0},
        )

        official_clock_snapshot(
            window,
            1_130,
            {1: 1_300_000, 2: 650_000},
            server_time=1_001,
        )
        official_clock_snapshot(
            window,
            1_140,
            {1: 1_400_000, 2: 700_000},
            server_time=1_001,
        )

        rows, rating_preview = window._main_display_rows(now=1_140)
        segment = window.live_hud_dps_segment
        self.assertFalse(rating_preview)
        self.assertFalse(segment["official_team_clock"])
        self.assertEqual(segment["snapshot_duration"], 140.0)
        self.assertEqual(rows[0]["actor_id"], 1)
        self.assertTrue(rows[0]["is_live_self"])
        self.assertEqual(rows[0]["metric"], "dps")
        self.assertEqual(rows[0]["total_value"], 1_400_000)
        self.assertEqual(rows[0]["stat_value"], 10_000.0)
        self.assertEqual(
            [row["actor_id"] for row in rows[2:]],
            [1, 2],
        )

    def test_daily_boss_hud_keeps_common_damage_and_common_accumulated_clock_together(self):
        window = make_official_clock_window()
        window.model._clear_targetless_team_statistics()
        window._dispatch_message(
            "live_hud_boss",
            {"active": True, "entity_id": 220_096_136_477_298,
             "template_id": 7_115_731, "started_at_epoch": 2_093.0},
        )
        official_clock_snapshot(
            window, 2_114.2, {1: 19_740_787, 2: 13_891_755},
            server_time=2_117, seconds=154,
        )

        rows = window._main_targetless_team_rows(2_114.2)
        by_actor = {row["actor_id"]: row for row in rows}
        self.assertAlmostEqual(by_actor[1]["dps_value"], 19_740_787 / 175)
        self.assertAlmostEqual(by_actor[2]["dps_value"], 13_891_755 / 175)
        self.assertEqual(window.live_hud_dps_segment["snapshot_duration"], 175.0)
        self.assertAlmostEqual(window._main_live_hud_dps_duration(2_114.2), 21.2)

    def test_twelve_member_raid_common_reset_uses_new_boss_clock_and_full_counter(self):
        window = make_official_clock_window()
        damages = {actor_id: 300_000 for actor_id in range(1, 13)}
        official_clock_snapshot(window, 2_900, damages, server_time=2_824)
        window._dispatch_message(
            "live_hud_boss",
            {"active": True, "entity_id": 220_096_136_477_298,
             "template_id": 7_115_731, "started_at_epoch": 3_000.0},
        )
        damages = {actor_id: actor_id * 120_000 for actor_id in range(1, 13)}
        official_clock_snapshot(window, 3_010, damages, server_time=3_024)

        rows = window._main_targetless_team_rows(3_010)
        self.assertEqual(len(rows), 12)
        for row in rows:
            self.assertEqual(row["total_value"], damages[row["actor_id"]])
            self.assertEqual(row["dps_value"], damages[row["actor_id"]] / 10)

    def test_official_hud_clock_handles_member_pauses_and_resumed_raid_phase(self):
        window = make_official_clock_window()
        window._dispatch_message(
            "live_hud_combat",
            {"active": True, "segment_id": 1, "started_at_epoch": 3_000.0},
        )
        damages = {actor_id: 350_000 for actor_id in range(1, 13)}
        official_clock_snapshot(window, 3_050, damages, server_time=3_069,
                                seconds=30, member_clocks={12: (28, 0)})
        rows = {row["actor_id"]: row for row in window._main_targetless_team_rows(3_050)}
        self.assertEqual(rows[1]["dps_value"], 350_000 / 35)
        self.assertEqual(rows[12]["dps_value"], 350_000 / 28)
        self.assertEqual(window._main_live_hud_dps_duration(3_050), 35.0)

        official_clock_snapshot(window, 3_053, damages, server_time=0,
                                seconds=38, member_clocks={12: (28, 0)})
        final = {row["actor_id"]: row for row in window._main_targetless_team_rows(3_060)}
        self.assertEqual(final[1]["dps_value"], 350_000 / 38)
        self.assertEqual(final[12]["dps_value"], 350_000 / 28)
        self.assertEqual(window._main_live_hud_dps_duration(3_060), 38.0)

        window._dispatch_message(
            "live_hud_combat",
            {"active": True, "segment_id": 1, "started_at_epoch": 3_000.0,
             "resumed_at_epoch": 3_100.0, "resumed": True},
        )
        official_clock_snapshot(window, 3_105, {i: 430_000 for i in damages},
                                server_time=3_124, seconds=38)
        resumed = window._main_targetless_team_rows(3_105)
        self.assertEqual(len(resumed), 12)
        self.assertTrue(all(row["dps_value"] == 430_000 / 43 for row in resumed))
        self.assertEqual(window._main_live_hud_dps_duration(3_105), 43.0)

    def test_live_small_monster_hud_uses_team_start_and_resumes_same_segment(self):
        window, _ = make_window()
        windows_epoch = 116_444_736_000_000_000

        def filetime(epoch):
            return windows_epoch + int(epoch * 10_000_000)

        def state(actor_id, token, damage, server_time):
            return SimpleNamespace(
                actor_id=actor_id,
                user_token=token,
                total_damage=damage,
                server_time=server_time,
            )

        def statistics(_now=None):
            rows = []
            for actor_id, value in window.model.targetless_team_states.items():
                actor = ActorStats(actor_id=actor_id)
                actor.damage = value.total_damage
                rows.append(actor)
            return rows

        window.live_hud_combat_state = None
        window.live_hud_boss_state = None
        window.live_hud_dps_segment = None
        window._schedule_layered_main_render = lambda: None
        window.model.targetless_team_started_at = 990.0
        window.model.targetless_team_last_snapshot_at = 999.5
        window.model.targetless_team_states = {
            1: state(1, "self", 300_000, 100),
            2: state(2, "peer", 200_000, 100),
        }
        window.model.targetless_team_last_committed_time_100ns = filetime(999.5)
        window.model.targetless_team_statistics = statistics
        window.model.targetless_team_duration = lambda _now=None: 10.0

        window._dispatch_message(
            "live_hud_combat",
            {
                "active": True,
                "segment_id": 1,
                "started_at_epoch": 1_000.0,
                "resumed_at_epoch": 1_000.0,
            },
        )
        segment = window.live_hud_dps_segment
        self.assertEqual(segment["started_at_epoch"], 990.0)
        self.assertEqual(segment["baseline_states"]["self"], (0, 0))

        window.model.targetless_team_states = {
            1: state(1, "self", 320_000, 100),
            2: state(2, "peer", 230_000, 100),
        }
        window.model.targetless_team_last_committed_time_100ns = filetime(1_001)
        first_rows = window._main_targetless_team_rows(1_001.0)
        first_by_actor = {row["actor_id"]: row for row in first_rows}
        self.assertEqual(first_by_actor[1]["total_value"], 320_000)
        self.assertEqual(first_by_actor[2]["total_value"], 230_000)
        self.assertAlmostEqual(first_by_actor[1]["dps_value"], 320_000 / 11)
        self.assertEqual(window._main_live_hud_dps_duration(1_001.0), 11.0)

        # A Common team snapshot can pause for longer than the model's stale
        # threshold while the packet-confirmed small-monster segment is still
        # active. Keep the last complete rows visible until a boundary or the
        # next complete snapshot replaces them.
        window.model.targetless_team_statistics = lambda _now=None: []
        window.model.targetless_team_duration = lambda _now=None: 0.0
        self.assertEqual(
            window._main_targetless_team_rows(1_020.0), first_rows
        )
        window.model.targetless_team_statistics = statistics
        window.model.targetless_team_duration = lambda _now=None: 10.0

        window._dispatch_message(
            "live_hud_combat",
            {
                "active": False,
                "segment_id": 1,
                "started_at_epoch": 1_000.0,
                "ended_at_epoch": 1_002.0,
            },
        )
        window.model.targetless_team_server_duration_seconds = 13.0
        self.assertEqual(window._main_live_hud_dps_duration(1_020.0), 13.0)
        final_rows = window._main_targetless_team_rows(1_020.0)
        final_by_actor = {row["actor_id"]: row for row in final_rows}
        self.assertEqual(final_by_actor[1]["total_value"], 320_000)
        self.assertEqual(final_by_actor[2]["total_value"], 230_000)
        self.assertAlmostEqual(final_by_actor[1]["dps_value"], 320_000 / 13)
        self.assertAlmostEqual(final_by_actor[2]["dps_value"], 230_000 / 13)
        self.assertEqual(segment["snapshot_duration"], 13.0)

        window.main_scroll_offset = 1
        window._dispatch_message(
            "live_hud_combat",
            {
                "active": True,
                "segment_id": 1,
                "started_at_epoch": 1_000.0,
                "resumed_at_epoch": 1_007.0,
                "elapsed_seconds": 2.0,
                "resumed": True,
            },
        )
        self.assertIs(window.live_hud_dps_segment, segment)
        self.assertEqual(window.main_scroll_offset, 1)
        self.assertEqual(segment["rows"], final_rows)

        window.model.targetless_team_states = {
            1: state(1, "self", 350_000, 200),
            2: state(2, "peer", 250_000, 200),
        }
        window.model.targetless_team_last_committed_time_100ns = filetime(1_009)
        resumed_rows = window._main_targetless_team_rows(1_009.0)
        resumed_by_actor = {row["actor_id"]: row for row in resumed_rows}
        self.assertEqual(resumed_by_actor[1]["total_value"], 350_000)
        self.assertEqual(resumed_by_actor[2]["total_value"], 250_000)
        self.assertAlmostEqual(resumed_by_actor[1]["dps_value"], 25_000.0)
        self.assertAlmostEqual(
            resumed_by_actor[2]["dps_value"], 250_000 / 14
        )
        self.assertEqual(window._main_live_hud_dps_duration(1_009.0), 14.0)

    def test_self_only_team_snapshot_keeps_original_settlement_display(self):
        window, _ = make_window()
        window.settlement_ui = SimpleNamespace(tracker=EncounterTracker())
        window.model.team_damage_states = {
            1: TeamDamageState(
                actor_id=1,
                has_snapshot=True,
                authoritative_snapshot=True,
                accepted_damage=630_000,
                snapshot_time_100ns=200_000_000_000,
            )
        }

        rows, rating_preview = window._main_display_rows(now=205)

        self.assertFalse(rating_preview)
        self.assertTrue(rows[0]["is_live_self"])
        self.assertEqual(rows[1]["row_kind"], "section")

    def test_team_preview_rows_are_not_truncated_at_twelve_members(self):
        window, members = make_window()
        for actor_id in range(1, 51):
            members.add(actor_id)
            window.model.entity_names[actor_id] = f"闃熷憳{actor_id}"
            window.model.entity_professions[actor_id] = 1200001

        rows = window._team_rating_preview_rows()

        self.assertEqual(len(rows), 50)
        self.assertEqual({row["actor_id"] for row in rows}, set(range(1, 51)))

    def test_zero_damage_real_member_and_named_temporary_member_are_retained(self):
        window, members = make_window()
        members.add(7)
        window.model.entity_names[-10] = '已识别队员'
        rows = window._main_combat_display_rows()
        ids = {row['actor_id'] for row in rows}
        self.assertIn(7, ids)
        self.assertIn(-10, ids)
        self.assertNotIn(-11, ids)

    def test_ended_battle_has_priority_over_rating_preview(self):
        window, _ = make_window()
        self.assertFalse(window._team_rating_preview_active())
        window.model.combat_end_time = 1000
        self.assertFalse(window._team_rating_preview_active())
        rows, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]['total_value'], 630_000)

    def test_fight_mode_switches_before_first_damage_without_arming_statistics(self):
        window, _ = make_window()
        window.model.started = False
        window.model.entity_combat_states = {1: True}
        self.assertFalse(window._team_rating_preview_active())
        self.assertFalse(window._main_has_battle_values())
        _, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertFalse(window.model.started)
        window._remember_main_battle_result(completed_record())
        self.assertFalse(window._main_retained_battle_active())

    def test_old_opacity_preferences_are_migrated_and_other_values_are_preserved(self):
        from test_combat_model import migrate_main_display_config
        for value in (0, 0.1, 0.45, 1.0, None, 'bad'):
            config = dict(alpha=value, geometry='123x456+78+90', font_size=16)
            updated, _ = migrate_main_display_config(config)
            self.assertEqual(updated['alpha'], 1.0)
            self.assertEqual(updated['geometry'], config['geometry'])
            self.assertEqual(updated['font_size'], 16)

    def test_boss_hp_updates_schedule_paint_without_waiting_for_summary_tick(self):
        window, _ = make_window()
        updates=[]
        window.model.ingest_monster = lambda value: updates.append(value) or True
        paints=[]
        window._schedule_layered_main_render = lambda: paints.append(True)
        packet=dict(entity_id=99, current_hp=238900, max_hp=1000000)
        window._dispatch_message('monster', packet)
        self.assertEqual(updates, [packet])
        self.assertEqual(paints, [True])

    def test_received_rating_schedules_paint_without_summary_timer(self):
        window, _ = make_window()
        window.model.started = False
        window.model.entity_extraordinary_ratings = {}
        def profile(value):
            window.model.entity_extraordinary_ratings[value['entity_id']] = value['extraordinary_rating']
            return True
        window.model.ingest_profile = profile
        paints = []
        window._schedule_layered_main_render = (
            lambda *args, **kwargs: paints.append((args, kwargs))
        )
        window._dispatch_message('profile', dict(entity_id=2, extraordinary_rating=83428))
        self.assertEqual(paints, [((), {"force": True})])
        self.assertEqual(window._main_extraordinary_rating(2), 83428)

    def test_current_team_equipment_rating_fills_only_missing_live_rating(self):
        window, _ = make_window()
        window.model.actor_character_ids = {2: "peer-token"}
        window.team_equipment_profiles = {
            "peer-token": {"extraordinary_rating": 90_001}
        }

        self.assertEqual(window._main_extraordinary_rating(2), 90_001)
        window.model.entity_extraordinary_ratings[2] = 90_002
        self.assertEqual(window._main_extraordinary_rating(2), 90_002)
        window.team_equipment_profiles.clear()
        window.model.entity_extraordinary_ratings.clear()
        self.assertIsNone(window._main_extraordinary_rating(2))

    def test_same_map_model_reset_retains_finished_battle_until_new_pull(self):
        window, _ = make_window()
        window.pve_hud_view = "recent_battle"
        record = completed_record()
        original = copy.deepcopy(record)
        window._remember_main_battle_result(record)
        window.model.started = False
        self.assertFalse(window._team_rating_preview_active())
        self.assertTrue(window._main_retained_battle_active())
        snapshot = window._layered_main_snapshot()
        self.assertEqual(len([row for row in snapshot['rows'] if not row.get('row_kind')]), 6)
        self.assertEqual(snapshot['boss_name'], 'Boss')
        self.assertEqual(snapshot['boss_percent'], '0%')
        self.assertEqual(snapshot['time'], '00:30')
        self.assertEqual(snapshot['team_dps'], '105,000')
        self.assertEqual(record, original)
        window.model.started = True
        self.assertFalse(window._main_retained_battle_active())
        self.assertFalse(window._team_rating_preview_active())

    def test_single_boss_terminal_record_replaces_stale_live_health_frame(self):
        window, _ = make_window()
        window.main_last_live_display_snapshot = {
            "encounter_id": "finished-six-person-battle",
            "bosses": [
                {
                    "boss_name": "战斗首领",
                    "boss_percent": "0.9%",
                    "boss_ratio": 0.009,
                }
            ],
        }

        window._remember_main_battle_result(completed_record())

        self.assertNotIn("bosses", window.main_last_battle_result)
        self.assertEqual(window.main_last_battle_result["monster"]["current_hp"], 0)

    def test_final_clear_hud_uses_stage_rows_team_dps_and_stage_clock(self):
        window, _ = make_window()
        window.model.started = False
        window.model.self_character_id = 'self-token'
        window.model.actor_character_ids = {1: 'self-token', 2: 'peer-token'}
        roster = [
            {'id': 'self-token', 'iid': 1, 'name': 'Self',
             'profession_id': 1200001},
            {'id': 'peer-token', 'iid': 2, 'name': 'Peer',
             'profession_id': 1200001},
        ]
        tracker = EncounterTracker()
        record = tracker.begin(
            instance_id='instance', started_at_ns=100_000_000_000,
            participants_snapshot=roster, self_token='self-token',
            boss_template_id=7109821, boss_token='boss-token',
        )
        tracker.end('VICTORY', 110_000_000_000)
        record.settlement_status = 'SETTLED'
        record.encounter_duration_seconds = 10
        record.duration_source = 'server_encounter_clock'
        record.participants = [
            dict(roster[0], damage=100, dps=10, bear=0, heal=0),
            dict(roster[1], damage=300, dps=30, bear=0, heal=0),
        ]
        record.all_statistics = {
            'members': [
                dict(roster[0], damage=600, bear=0, heal=0,
                     member_battle_length=20),
                dict(roster[1], damage=400, bear=0, heal=0,
                     member_battle_length=20),
                dict(id='old-member', iid=3, name='Previous Boss Member',
                     damage=500, bear=0, heal=0, member_battle_length=20),
            ],
        }
        window.settlement_ui = SimpleNamespace(
            tracker=tracker, instance_id='instance',
            session_encounter_ids={record.local_encounter_id},
            visible=lambda: encounter_view(record, statistics_scope='ALL'),
        )
        window.history_store = SimpleNamespace(load=lambda _encounter_id: None)
        window.main_recent_battle_scope_started_ns = 0
        window.main_cleared_encounter_ids = deque(maxlen=64)
        window.main_last_battle_result = None
        window.main_manual_clear_state = None
        window.pve_hud_view = 'recent_battle'
        window.main_time_text = '00:00'
        window.team_dps_text = '0'
        window.main_scroll_offset = 0
        window.main_visible_rows = 10
        window.main_ui_scale = 1.0
        window.window_dpi = 96
        window.show_combat_time = True
        window.show_boss_hp_bar = True
        window.show_main_totals = True
        window.show_deaths = True
        window.show_team_dps = True
        window.show_pvp_button = True
        window.highlight_self = True
        window.main_row_mask_opacity = 0
        window.window_locked = False

        snapshot = window._layered_main_snapshot()
        recent_rows = [
            row for row in snapshot['rows']
            if row.get('is_recent_battle') and not row.get('is_official_self')
        ]

        self.assertEqual(
            [row['total_value'] for row in recent_rows], [300, 100]
        )
        self.assertEqual(snapshot['team_dps'], '40/s')
        self.assertEqual(snapshot['time'], '00:10')

    def test_hud_all_fallback_requires_a_verified_one_boss_dungeon(self):
        window, _ = make_window()
        record = SimpleNamespace(
            result='VICTORY',
            settlement_status='SETTLED',
            stage_id=5150111,
            dungeon_id=0,
            stage_statistics=None,
            all_statistics={'members': [{'id': 'self', 'damage': 100}]},
        )

        self.assertEqual(window._main_hud_statistics_scope(record), 'ALL')

        record.stage_statistics = {
            'members': [{'id': 'self', 'damage': 100}]
        }
        self.assertEqual(window._main_hud_statistics_scope(record), 'STAGE')

        record.stage_statistics = None
        record.stage_id = 5150063
        self.assertEqual(window._main_hud_statistics_scope(record), 'STAGE')

        window._main_recent_stage_catalog = {
            '999001': {'dungeon_ids': [999]}
        }
        window._main_recent_dungeon_catalog = {
            '999': {'stage_ids': [999001, 999002]}
        }
        record.stage_id = 999001
        self.assertEqual(window._main_hud_statistics_scope(record), 'STAGE')

    def test_idle_recent_settlement_drives_the_same_header_boss_and_time(self):
        window, _ = make_window()
        window.model.started = False
        roster = [{
            'id': 'self-token',
            'iid': 1,
            'name': 'Self',
            'profession_id': 1200001,
        }]
        tracker = EncounterTracker()
        record = tracker.begin(
            instance_id='instance',
            started_at_ns=100_000_000_000,
            participants_snapshot=roster,
            boss_template_id=7109821,
            boss_token='boss-token',
            self_token='self-token',
        )
        record.boss_name = '异化猎犬'
        tracker.end('VICTORY', 137_000_000_000)
        record.settlement_status = 'SETTLED'
        record.encounter_duration_seconds = 37
        record.duration_source = 'verified_shared_encounter_clock'
        window.settlement_ui = SimpleNamespace(
            tracker=tracker,
            visible=lambda: encounter_view(record),
        )
        loaded_encounters = []
        window.history_store = SimpleNamespace(
            load=lambda encounter_id: (
                loaded_encounters.append(encounter_id)
                or {
                    'encounter_id': encounter_id,
                    'monster': {
                        'entity_id': 300,
                        'name': '旧名称不能覆盖 Encounter',
                        'template_id': 7109821,
                        'level': 73,
                        'current_hp': 12_000_000,
                        'max_hp': 34_501_705,
                    },
                }
            )
        )
        window.main_last_battle_result = None
        window.main_manual_clear_state = None
        window.main_time_text = '00:00'
        window.team_dps_text = '0'
        window.main_scroll_offset = 0
        window.main_visible_rows = 10
        window.main_ui_scale = 1.0
        window.window_dpi = 96
        window.show_combat_time = True
        window.show_boss_hp_bar = True
        window.show_main_totals = True
        window.show_deaths = True
        window.show_team_dps = True
        window.show_pvp_button = True
        window.highlight_self = True
        window.main_row_mask_opacity = 0
        window.window_locked = False
        window.config = {}

        snapshot = window._layered_main_snapshot()

        self.assertEqual(snapshot['boss_name'], '异化猎犬')
        self.assertEqual(snapshot['boss_template_id'], 7109821)
        self.assertEqual(snapshot['boss_level'], 73)
        self.assertTrue(snapshot['boss_hp'])
        self.assertEqual(snapshot['boss_percent'], '0%')
        self.assertEqual(snapshot['boss_ratio'], 0.0)
        self.assertEqual(snapshot['time'], '00:37')
        self.assertTrue(loaded_encounters)
        self.assertEqual(set(loaded_encounters), {record.local_encounter_id})

        live_boss = SimpleNamespace(
            entity_id=301,
            name='Current Boss',
            template_id=7102403,
            icon='',
            level=62,
            current_hp=25_000_000,
            max_hp=33_270_350,
        )
        window.model.current_monster = lambda: live_boss
        window.model.current_bosses = lambda: [live_boss]
        window.model.started = True
        window.model.entity_combat_states = {live_boss.entity_id: True}

        live_snapshot = window._layered_main_snapshot()

        self.assertEqual(live_snapshot['boss_name'], 'Current Boss')
        self.assertEqual(live_snapshot['boss_template_id'], 7102403)
        self.assertAlmostEqual(
            live_snapshot['boss_ratio'], 25_000_000 / 33_270_350
        )

    def test_map_transition_keeps_final_result_until_new_dungeon_context(self):
        window, _ = make_window()
        record = completed_record()
        window._remember_main_battle_result(record)
        window.model.encounter_id = record['encounter_id']
        archived = []
        def scene_update(_payload):
            archived.append(record)
            window.model.started = False
            window.model.encounter_id = 'new-map'
            return True
        window.model.ingest_scene = scene_update
        window._invalidate_team_rating_preview_rows = lambda: None
        scheduled = []
        window._schedule_layered_main_render = lambda: scheduled.append(True)
        window._dispatch_message('scene', {'transition': True})
        self.assertEqual(
            window.main_last_battle_result['encounter_id'],
            record['encounter_id'],
        )
        self.assertNotIn(
            record['encounter_id'],
            getattr(window, 'main_cleared_encounter_ids', ()),
        )
        self.assertEqual(archived, [record])
        window._remember_main_battle_result(record)
        self.assertEqual(
            window.main_last_battle_result['team_dps'], record['team_dps']
        )
        self.assertFalse(window._team_rating_preview_active())
        self.assertEqual(scheduled, [True])

    def test_new_dungeon_context_starts_an_empty_main_window_scope(self):
        window, _ = make_window()
        window.model.current_dungeon_id = 5_100_001
        window.model.encounter_id = "previous-dungeon-encounter"
        window.model.combat_end_time = 100.0

        def ingest_context(payload):
            window.model.current_dungeon_id = int(payload["dungeon_id"])

        window.model.ingest_dungeon_context = ingest_context
        window.main_last_battle_result = completed_record()
        window.main_time_text = "00:30"
        window.team_dps_text = "105,000"
        window.enrage_prediction = object()
        window.enrage_prediction_visible = True
        renders = []
        window._schedule_layered_main_render = lambda: renders.append(True)
        boundary_ns = 150_000_000_000

        window._dispatch_message(
            "dungeon_context",
            {
                "dungeon_id": 5_100_002,
                "dungeon_context_filetime": (
                    116_444_736_000_000_000 + boundary_ns // 100
                ),
            },
        )

        self.assertEqual(
            window.main_recent_battle_scope_started_ns, boundary_ns
        )
        self.assertEqual(
            window.main_hidden_combat_encounter_id,
            "previous-dungeon-encounter",
        )
        self.assertIsNone(window.main_last_battle_result)
        self.assertEqual(window.main_time_text, "00:00")
        self.assertEqual(window.team_dps_text, "0")
        self.assertEqual(renders, [True])

    def test_special_boss_scripted_phase_scene_does_not_clear_model_statistics(self):
        window, _ = make_window()
        record = completed_record()
        window._remember_main_battle_result(record)
        window.model.long_gap_phase_suspended = True
        window.model.ingest_scene = lambda _payload: True
        window._invalidate_team_rating_preview_rows = lambda: None
        window._dispatch_message('scene', {'scene_id': 200})
        self.assertIsNotNone(window.main_last_battle_result)
        self.assertTrue(window.model.started)

    def test_retained_hps_and_dt_are_read_from_existing_record_fields(self):
        window, _ = make_window()
        record = completed_record()
        # Keep this configured-metric test below the per-player DPS override.
        for actor in record['participants']:
            actor['damage'] //= 10
            actor['dps'] /= 10
        record['participants'][0]['profession_id'] = 1200002
        record['participants'][1]['profession_id'] = 1200006
        record['healers'] = [dict(actor_id=1, hps=31_500, effective_healing=945_000)]
        window.profession_display_metrics['1200006'] = 'dt'
        window._remember_main_battle_result(record)
        window.model.started = False
        rows, rating = window._main_display_rows()
        self.assertFalse(rating)
        self.assertEqual(rows[0]['metric'], 'hps')
        self.assertEqual(rows[0]['stat_value'], 31_500)
        self.assertEqual(rows[0]['total_value'], 945_000)
        self.assertEqual(rows[1]['metric'], 'dt')
        self.assertEqual(rows[1]['stat_value'], 230 / 30)
        self.assertEqual([row['actor_id'] for row in rows], list(range(1, 7)))

    def test_dual_boss_snapshot_keeps_independent_health_without_resetting_clock(self):
        window, _ = make_window()
        bosses = [SimpleNamespace(entity_id=101, name='甲', current_hp=100, max_hp=400),
                  SimpleNamespace(entity_id=102, name='乙', current_hp=200, max_hp=300)]
        window.model.current_monster = lambda: bosses[0]
        window.model.current_bosses = lambda: bosses
        window.main_time_text = '01:25'
        window.team_dps_text = '7,500'
        snapshot = window._layered_main_snapshot()
        self.assertEqual(len(snapshot['bosses']), 2)
        self.assertEqual([row['boss_name'] for row in snapshot['bosses']], ['甲', '乙'])
        self.assertEqual([row['boss_percent'] for row in snapshot['bosses']], ['25.0%', '66.7%'])
        self.assertEqual(snapshot['time'], '01:25')
        bosses.pop(0)
        next_stage = window._layered_main_snapshot()
        self.assertEqual(len(next_stage['bosses']), 1)
        self.assertEqual(next_stage['time'], '01:25')

    def test_first_believer_forecast_owner_barney_is_the_first_bar(self):
        window, _ = make_window()
        anxia = SimpleNamespace(entity_id=101, template_id=7100208, name='安西娅', current_hp=300, max_hp=400)
        barney = SimpleNamespace(entity_id=102, template_id=7100209, name='巴尼先生', current_hp=200, max_hp=300)
        bosses = [anxia, barney]
        window.model.current_monster = lambda: anxia
        window.model.current_bosses = lambda: bosses
        first = window._layered_main_snapshot()
        self.assertEqual([boss['boss_name'] for boss in first['bosses']], ['巴尼先生', '安西娅'])
        self.assertEqual(bosses, [anxia, barney])
        barney.current_hp = 0
        barney.death_confirmed = True
        second = window._layered_main_snapshot()
        self.assertEqual(second['bosses'][0]['boss_name'], '安西娅')

    def test_confirmed_self_keeps_own_row_during_roster_handoff(self):
        window, members = make_window()
        window.model.self_id = 21
        window.model.entity_names[21] = '本人'
        window.model.current_stats = lambda: []
        window.model.current_taken_rows = lambda: []
        window.model.encounter_member_ids = set()
        rows = window._main_combat_display_rows()
        self.assertIn(21, {row['actor_id'] for row in rows})
        self.assertNotIn(21, members)

    def test_exact_dps_appears_before_token_actor_gets_positive_entity_id(self):
        window, members = make_window()
        temporary = -9912233
        members.add(temporary)
        actor = ActorStats(actor_id=temporary)
        actor.damage = 300_000
        window.model.current_stats = lambda: [actor]
        window.model.encounter_member_ids = {temporary}
        window.model.entity_names[temporary] = '已确认队友'
        window.model.entity_professions[temporary] = 1200001
        rows = window._main_combat_display_rows()
        teammate = next(row for row in rows if row['actor_id'] == temporary)
        self.assertEqual(teammate['stat_value'], 10000)
        self.assertEqual(teammate['total_value'], 300000)
        self.assertEqual(rows[0]['actor_id'], temporary)
        self.assertEqual(actor.damage, 300000)

    def test_party_fight_signal_immediately_leaves_pre_pull_preview(self):
        window, _ = make_window()
        window.model.started = False
        window.model.entity_combat_states = {2: True}
        self.assertFalse(window._team_rating_preview_active())
        window.model.entity_combat_states = {999: True}
        self.assertTrue(window._team_rating_preview_active())

    def test_healing_only_participant_is_retained_after_scene_reset(self):
        window, _ = make_window()
        record = completed_record()
        record['healers'] = [dict(actor_id=12, name='观众', is_self=False, profession_id=1200002,
                                  hps=1000, effective_healing=30000)]
        window._remember_main_battle_result(record)
        window.model.started = False
        rows, _ = window._main_display_rows()
        healer = next(row for row in rows if row['actor_id'] == 12)
        self.assertEqual(healer['metric'], 'hps')
        self.assertEqual(healer['stat_value'], 1000)

    def test_ai_rating_text_overrides_numeric_rating_without_mutating_profile(self):
        window, _ = make_window()
        window.model.started = False
        profiles = [dict(actor_id=1, profession_id=1200001, display_name='玩家', extraordinary_rating=123456, is_ai=False),
                    dict(actor_id=2, profession_id=1200002, display_name='队员·投影', extraordinary_rating=75900, is_ai=True)]
        window._team_rating_preview_rows = lambda: profiles
        snapshot = window._layered_main_snapshot()
        self.assertTrue(snapshot['rating_preview'])
        rows = {row['actor_id']: row for row in snapshot['rows'] if not row.get('row_kind')}
        self.assertEqual(rows[1]['rating_text'], '123456')
        self.assertEqual(rows[2]['rating_text'], '人机')
        self.assertEqual(profiles[1]['extraordinary_rating'], 75900)

    def test_team_composition_keeps_current_roster_member_without_rating_profile(self):
        window, members = make_window()
        window.model.started = False
        members.clear()
        members.update({1, 2, 3, 4, 5})
        window.model.friend_order = [1, 2, 3, 4, 5]
        window.model.entity_names[5] = '刚入队的队友'
        window._team_rating_preview_rows = lambda: [
            dict(actor_id=actor, profession_id=1200001,
                 display_name=f'队员{actor}', extraordinary_rating=90000)
            for actor in (1, 2, 3, 4)
        ]

        rows = window._main_team_rating_section_rows()

        self.assertEqual([row['actor_id'] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual(rows[-1]['name'], '刚入队的队友')
        self.assertEqual(rows[-1]['rating_text'], '--')

    def test_pre_pull_preview_omits_unbound_roster_slots(self):
        window, _ = make_window()
        window.model.started = False
        rows = window._team_rating_preview_rows()
        self.assertEqual(len(rows), 6)

    def test_authorization_reset_does_not_preserve_previous_identity_result(self):
        window, _ = make_window()
        window.authorization_resetting = True
        window._remember_main_battle_result(completed_record())
        self.assertIsNone(getattr(window, 'main_last_battle_result', None))

    def test_character_switch_flushes_old_record_before_clearing_visible_role(self):
        old_token = "AQAAAOwNkGB8AAAA"
        new_token = "AQAAAOwNGOEuAAAA"
        window = object.__new__(DpsWindow)
        reset_calls = []
        flush_calls = []
        window.model = SimpleNamespace(
            reset=lambda **options: reset_calls.append(options),
            entity_names={1: "Old"}, entity_professions={1: 1_200_001},
            entity_extraordinary_ratings={1: 80_000}, entity_ai_states={},
            actor_character_ids={1: old_token}, tokens={},
            local_player_name="Old",
            ingest_profile=lambda _profile: None,
            ingest_identity=lambda _identity: None,
        )
        window.current_character_id = old_token
        window.main_last_battle_result = {"encounter_id": "old"}
        window.main_last_live_display_snapshot = {"encounter_id": "old"}
        window.upload_profile = object()
        window.pvp_recording = None
        window._flush_combat_history = lambda **options: flush_calls.append(options)
        window._clear_team_equipment_profiles = lambda: None
        window._invalidate_team_rating_preview_rows = lambda *_args, **_kwargs: None
        window._schedule_layered_main_render = lambda: None
        window._sync_startup_identity_state = lambda: None
        window._sync_upload_profile_label = lambda: None
        window._resolve_upload_profile_async = lambda: None

        window._handle_self_character({
            "user_token": new_token, "entity_id": 2, "name": "New",
            "profession_id": 1_200_002,
        })

        self.assertEqual(len(reset_calls), 1)
        self.assertEqual(flush_calls, [{"force": True}])
        self.assertEqual(window.current_character_id, new_token)
        self.assertIsNone(window.main_last_battle_result)
        self.assertFalse(window.model.entity_names)
        window._remember_main_battle_result({
            "encounter_id": "old", "self_character_id": old_token,
            "participants": [{"actor_id": 1, "name": "Old", "is_self": True}],
        })
        self.assertIsNone(window.main_last_battle_result)

    def test_manual_clear_discards_result_and_restores_pre_pull_preview(self):
        window, _ = make_window()
        window._remember_main_battle_result(completed_record())
        window.model.started = False
        window.model.reset = lambda **_kwargs: None
        window._flush_combat_history = lambda: None
        window._draw_main_rows = lambda: None
        window._render_skill_details = lambda: None
        window.reset()
        self.assertIsNone(window.main_last_battle_result)
        self.assertTrue(window._team_rating_preview_active())


if __name__ == '__main__':
    unittest.main()
