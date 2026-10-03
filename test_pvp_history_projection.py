"""PvP records must project losslessly onto the native PVE history widgets."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest import mock

from test_combat_model import DpsWindow


class LabelProbe:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class PvpHistoryProjectionTests(unittest.TestCase):
    def window(self):
        window = object.__new__(DpsWindow)
        window.model = SimpleNamespace(runtime_skill_names={})
        return window

    def test_legacy_six_vs_six_real_role_is_not_shown_as_bot(self):
        from pvp_backend_ui import pvp_snapshot_rosters

        role = "agMDAwMDAwMDAwMD"
        payload = {
            "map_id": 5208004, "team_size": 6,
            "player": {"character_id": "AQAAAOwNkGB8AAAA", "name": "Self"},
            "allies": [
                {"character_id": "AQAAAOwNkGB8AAAA", "name": "Self", "is_self": True},
                {"character_id": role, "name": "Real teammate", "is_ai": True},
            ],
            "enemies": [
                {"character_id": role + "A", "name": "Real opponent", "is_ai": True},
            ],
        }

        native_allies, native_enemies, _ = self.window()._pvp_snapshot_rosters(payload)
        page_allies, page_enemies = pvp_snapshot_rosters(payload)

        self.assertFalse(native_allies[1]["is_ai"])
        self.assertFalse(native_enemies[0]["is_ai"])
        self.assertFalse(page_allies[1]["is_ai"])
        self.assertFalse(page_enemies[0]["is_ai"])
        self.assertTrue(payload["allies"][1]["is_ai"])

    def test_hunter_history_detail_uses_opponent_view(self):
        window = self.window()
        payload = {"map_id": 5200167, "match_id": "hunter-match"}
        window.history_selected_id = "hunter-match"
        window._selected_history_record = lambda: {"_pvp_record": payload}
        window._open_hunter_history_detail = mock.Mock()

        window._open_selected_history_detail()

        window._open_hunter_history_detail.assert_called_once_with(payload)

    def test_hunter_summary_without_nested_payload_still_uses_opponent_view(self):
        window = self.window()
        summary = {"map_id": 5200167, "match_id": "hunter-summary"}
        window.history_selected_id = "hunter-summary"
        window._selected_history_record = lambda: summary
        window._open_hunter_history_detail = mock.Mock()

        window._open_selected_history_detail()

        window._open_hunter_history_detail.assert_called_once_with(summary)

    def test_hunter_history_summary_has_allied_roster_and_no_result(self):
        window = self.window()
        payload = self.payload(3)
        payload.update(map_id=5200167, mode_name="终末猎杀",
                       map_name="猎龙之城", team_size=None, result="未知")

        summary = window._pvp_history_summary(payload)
        allies, enemies, size = window._pvp_snapshot_rosters(summary)

        self.assertEqual(summary["pvp_category"], "猎龙之城")
        self.assertEqual(summary["pvp_result"], "该类型无结果")
        self.assertEqual(summary["result"], "pvp_no_result")
        self.assertEqual(size, len(allies))
        self.assertEqual(enemies, [])

    def test_late_equipment_update_refreshes_the_native_pvp_detail_only(self):
        window = self.window()
        window.pvp_history_page = None
        scheduled = []
        window.history_window = SimpleNamespace(
            winfo_exists=lambda: True,
            state=lambda: "normal",
            after=lambda delay, callback: scheduled.append((delay, callback)) or "refresh",
        )
        window.history_loaded_record = {"combat_mode": "pvp"}
        window._query_history_records = mock.Mock()
        window.backend_current_page = "kings"

        for _ in range(20):
            window._dispatch_message("pvp_history_changed", None)

        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], 250)
        window._query_history_records.assert_not_called()
        scheduled[0][1]()
        self.assertIsNone(window.history_loaded_record)
        window._query_history_records.assert_called_once_with()
        window.backend_current_page = "history"
        window._query_history_records.reset_mock()
        window._dispatch_message("pvp_history_changed", None)
        window._query_history_records.assert_not_called()

    @staticmethod
    def payload(team_size=3):
        snapshot = {
            "extraordinary_rating": 90_000,
            "equipment_score": 720,
            "equipment": [{"slot": 1, "item_id": 10001}],
            "affixes": [{"name": "竞技增伤", "score": 88}],
        }
        allies = []
        enemies = []
        for index in range(team_size):
            allies.append(
                {
                    "character_id": f"ally-{index}",
                    "name": "本人" if index == 0 else f"队友{index}",
                    "profession_id": 1_200_001 + index,
                    "level": 80,
                    "extraordinary_rating": 90_000 - index * 1_000,
                    "avatar_id": 4_270_019 + index,
                    "avatar_frame_id": 4_271_009,
                    "is_self": index == 0,
                    "is_ai": index == team_size - 1 and team_size >= 6,
                    "kills": 4 - min(index, 3),
                    "assists": index,
                    "deaths": index % 2,
                    "damage": 3_000 - index * 100,
                    "healing": 500 + index,
                    "taken": 1_500 + index * 10,
                    "metrics_scope": "authoritative",
                    "skills_scope": "authoritative",
                    "statistics_authoritative": True,
                    "skills": [
                        {
                            "skill_id": 101 + index,
                            "damage": 300,
                            "hits": 2,
                            "casts": 1,
                            "max_hit": 180,
                            "hit_timestamps_ns": [10, 20],
                            "cast_timestamps_ns": [5],
                        }
                    ],
                    "equipment_snapshot": deepcopy(snapshot) if index == 0 else None,
                }
            )
            enemies.append(
                {
                    "character_id": f"enemy-{index}",
                    "name": f"敌方{index + 1}",
                    "profession_id": 1_200_101 + index,
                    "level": 79,
                    "extraordinary_rating": None if index == team_size - 1 else 92_000 + index,
                    "avatar_id": 4_270_045 + index,
                    "avatar_frame_id": 4_271_002,
                    "kills": index + 1,
                    "assists": index + 2,
                    "deaths": index,
                    "damage": 2_000 + index * 100,
                    "healing": None if index == team_size - 1 else 200 + index,
                    "taken": 2_500 + index * 100,
                    "metrics_scope": "authoritative",
                    "skills_scope": "authoritative",
                    "statistics_authoritative": True,
                    "skills": [
                        {
                            "skill_id": 301 + index,
                            "damage": 600,
                            "hits": 3,
                            "casts": 2,
                            "hit_timestamps_ns": [30, 40, 50],
                            "cast_timestamps_ns": [25, 45],
                        }
                    ],
                    "equipment_snapshot": None,
                }
            )
        # A stale side snapshot may repeat a friendly UID as an enemy.  The
        # native projection must retain only the authoritative friendly row.
        enemies.append(
            {
                "character_id": "ally-0",
                "name": "错误敌方副本",
                "damage": 999_999,
            }
        )
        opponents = [
            {
                "opponent_id": "enemy-key-0",
                "character_id": "enemy-0",
                "name": "敌方1",
                "profession_id": 1_200_101,
                "level": 79,
                "extraordinary_rating": 92_000,
                "avatar_id": 4_270_045,
                "avatar_frame_id": 4_271_002,
                "damage_to": 250,
                "damage_from": 180,
                "kills_on": 1,
                "deaths_to": 2,
                "skills_outgoing": [
                    {
                        "skill_id": 701,
                        "damage": 250,
                        "hits": 2,
                        "casts": 1,
                        "max_hit": 150,
                        "hit_timestamps_ns": [101, 103],
                        "cast_timestamps_ns": [100],
                    }
                ],
                "skills_incoming": [
                    {
                        "skill_id": 801,
                        "damage": 180,
                        "hits": 1,
                        "casts": 1,
                        "max_hit": 180,
                        "hit_timestamps_ns": [102],
                        "cast_timestamps_ns": [99],
                    }
                ],
                "interaction_scope": "pair_exact",
                "equipment_snapshot": {
                    "extraordinary_rating": 92_000,
                    "equipment": [{"slot": 2, "item_id": 20002}],
                    "affixes": [{"name": "竞技减伤", "score": 66}],
                },
            }
        ]
        if team_size >= 3:
            opponents.append(
                {
                    "opponent_id": "enemy-key-1",
                    "character_id": "enemy-1",
                    "name": "敌方2",
                    "damage_to": 90,
                    "damage_from": 70,
                    "kills_on": 0,
                    "deaths_to": 0,
                    "skills_outgoing": [
                        {
                            "skill_id": 702,
                            "damage": 90,
                            "hits": 1,
                            "hit_timestamps_ns": [105],
                        }
                    ],
                    "skills_incoming": [
                        {
                            "skill_id": 801,
                            "damage": 70,
                            "hits": 1,
                            "hit_timestamps_ns": [104],
                        }
                    ],
                    "interaction_scope": "pair_exact",
                }
            )
        return {
            "match_id": f"match-{team_size}",
            "battle_id": f"match-{team_size}",
            "map_id": 5200020,
            "mode_name": "主宰争锋",
            "map_name": "测试竞技地图",
            "team_size": team_size,
            "duration_seconds": 60,
            "started_at_ns": 1_000_000_000,
            "ended_at_ns": 61_000_000_000,
            "result": "胜利",
            "player": {
                "character_id": "ally-0",
                "name": "本人",
                "profession_id": 1_200_001,
                "level": 80,
                "extraordinary_rating": 90_000,
                "avatar_id": 4_270_019,
                "avatar_frame_id": 4_271_009,
                "equipment_snapshot": deepcopy(snapshot),
            },
            "kills": 4,
            "assists": 3,
            "deaths": 2,
            "damage": 3_000,
            "damage_taken": 1_500,
            "data_scope": {
                "roster": "verified",
                "self_metrics": "self_exact",
                "team_metrics": "authoritative",
                "opponent_interactions": "pair_exact",
            },
            "allies": allies,
            "enemies": enemies,
            "opponents": opponents,
        }

    def project(self, payload):
        window = self.window()
        summary = window._pvp_history_summary(payload)
        return window._pvp_history_detail_record(summary, payload)

    def test_1v1_3v3_6v6_and_12v12_keep_complete_separate_rosters(self):
        for size in (1, 3, 6, 12):
            with self.subTest(size=size):
                record = self.project(self.payload(size))
                allies = [row for row in record["participants"] if row["side"] == "ally"]
                enemies = [row for row in record["participants"] if row["side"] == "enemy"]
                self.assertEqual(len(allies), size)
                self.assertEqual(len(enemies), size)
                self.assertEqual(record["pvp_team_size"], size)
                self.assertEqual(record["pvp_total_players"], size * 2)
                self.assertEqual(sum(row["is_self"] is True for row in allies), 1)
                self.assertNotIn("错误敌方副本", {row["name"] for row in enemies})
                self.assertTrue(all(row["profession_id"] for row in record["participants"]))
                self.assertTrue(all(row["level"] for row in record["participants"]))
                self.assertTrue(all(row["avatar_id"] for row in record["participants"]))
                self.assertTrue(all(row["avatar_frame_id"] for row in record["participants"]))

    def test_legacy_authoritative_3v3_keeps_recorded_partial_skills(self):
        payload = self.payload(3)
        for row in [*payload["allies"], *payload["enemies"]]:
            row.pop("metrics_scope", None)
            row.pop("skills_scope", None)

        record = self.project(payload)
        players = record["participants"]

        self.assertEqual(len(players), 6)
        self.assertTrue(all(row["damage"] is not None for row in players))
        self.assertTrue(all(row["skills"] for row in players))
        self.assertTrue(
            all(row["skills_scope"] == "observed_partial" for row in players)
        )
        self.assertTrue(
            all(row["skill_detail_status"] == "observed_partial" for row in players)
        )

    def test_3v3_detail_can_select_all_six_equipment_snapshots(self):
        payload = self.payload(3)
        expected_items = set()
        for index, member in enumerate(
            [*payload["allies"], *payload["enemies"][:3]], 1
        ):
            item_id = 30_000 + index
            expected_items.add(item_id)
            member["equipment_snapshot"] = {
                "captured_at_ns": 1_000_000_000 + index,
                "equipment": [{"slot": 1, "item_id": item_id}],
            }
        record = self.project(payload)
        window = self.window()
        window.history_loaded_record = record
        window.history_selected_id = record["battle_id"]
        window.history_records = []

        players = window._pvp_duel_participants()
        selected_items = set()
        for index, player in enumerate(players):
            window.history_duel_equipment_selected_player = (
                window._pvp_duel_player_key(player, index)
            )
            selected = window._selected_pvp_duel_equipment_player()
            selected_items.add(
                selected["equipment_snapshot"]["equipment"][0]["item_id"]
            )

        self.assertEqual(len(players), 6)
        self.assertEqual(selected_items, expected_items)

    def test_all_supported_arena_sizes_use_the_same_native_pvp_detail_route(self):
        for size in (1, 3, 6, 12):
            with self.subTest(size=size):
                record = self.project(self.payload(size))
                self.assertTrue(
                    DpsWindow._is_pvp_duel_history_record(record)
                )

    def test_duel_player_selection_keeps_self_and_opponent_appearance(self):
        window = self.window()
        payload = self.payload(1)
        summary = window._pvp_history_summary(payload)
        record = window._pvp_history_detail_record(summary, payload)
        window._selected_history_record = lambda: record

        selected_record, ally, enemy = window._pvp_duel_players()

        self.assertIs(selected_record, record)
        self.assertTrue(ally["is_self"])
        self.assertEqual((ally["level"], ally["avatar_id"], ally["avatar_frame_id"]), (80, 4_270_019, 4_271_009))
        self.assertEqual((enemy["level"], enemy["avatar_id"], enemy["avatar_frame_id"]), (79, 4_270_045, 4_271_002))

    def test_pair_skills_equipment_and_timestamps_merge_into_existing_enemy(self):
        record = self.project(self.payload(3))
        participants = {row["character_id"]: row for row in record["participants"]}
        local = participants["ally-0"]
        enemy = participants["enemy-0"]

        # Full-team totals remain authoritative; pair damage is not presented
        # as the enemy's whole-match total in a team mode.
        self.assertEqual(enemy["damage"], 2_000)
        self.assertEqual(enemy["taken"], 2_500)
        self.assertEqual(enemy["pvp_skills_to"][0]["damage"], 250)
        self.assertEqual(enemy["pvp_skills_to"][0]["hits"], 2)
        self.assertEqual(enemy["pvp_skills_to"][0]["casts"], 1)
        self.assertEqual(enemy["pvp_skills_to"][0]["hit_timestamps_ns"], [101, 103])
        self.assertEqual(enemy["pvp_skills_to"][0]["cast_timestamps_ns"], [100])
        self.assertEqual(enemy["pvp_skills_from"][0]["damage"], 180)
        self.assertEqual(enemy["equipment_snapshot"]["equipment"][0]["item_id"], 20002)
        self.assertEqual(enemy["equipment_snapshot"]["affixes"][0]["score"], 66)

        # Same incoming skill from two different enemies is combined for the
        # local player's exact incoming direction without losing timestamps.
        incoming = {row["skill_id"]: row for row in local["pvp_skills_from"]}
        self.assertEqual(incoming[801]["damage"], 250)
        self.assertEqual(incoming[801]["hits"], 2)
        self.assertEqual(incoming[801]["hit_timestamps_ns"], [102, 104])
        self.assertEqual(local["equipment_snapshot"]["affixes"][0]["name"], "竞技增伤")

    def test_duel_restores_catalog_names_for_directional_skill_lists(self):
        payload = self.payload(1)
        payload["allies"][0]["skills"][0].update(
            skill_id=86_061_010,
            name=None,
        )
        payload["opponents"][0]["skills_outgoing"][0].update(
            skill_id=86_061_010,
            name=None,
        )
        payload["opponents"][0]["skills_incoming"][0].update(
            skill_id=86_021_070,
            name="未命名技能",
        )

        record = self.project(payload)
        participants = {row["side"]: row for row in record["participants"]}

        self.assertEqual(
            participants["ally"]["pvp_skills_to"][0]["name"],
            "荣耀之斩",
        )
        self.assertEqual(
            participants["enemy"]["pvp_skills_to"][0]["name"],
            "荣耀之斩",
        )
        self.assertEqual(
            participants["enemy"]["pvp_skills_from"][0]["name"],
            "梦境编织",
        )

    def test_missing_values_stay_unknown_and_average_ignores_ai_and_unknown(self):
        payload = self.payload(6)
        payload["allies"][1]["extraordinary_rating"] = None
        record = self.project(payload)
        enemies = [row for row in record["participants"] if row["side"] == "enemy"]
        allies = [row for row in record["participants"] if row["side"] == "ally"]
        self.assertIsNone(enemies[-1]["effective_healing"])
        expected = round(
            sum(row["extraordinary_rating"] for row in allies if not row["is_ai"] and row["extraordinary_rating"] is not None)
            / sum(1 for row in allies if not row["is_ai"] and row["extraordinary_rating"] is not None)
        )
        self.assertEqual(record["pvp_ally_average_rating"], expected)
        self.assertEqual(
            record["pvp_enemy_average_rating"],
            round(sum(92_000 + index for index in range(5)) / 5),
        )

    def test_3v3_partial_skill_details_keep_official_totals_and_visible_hits(self):
        payload = self.payload(3)
        ally = payload['allies'][1]
        ally['skills_scope'] = 'observed_partial'
        ally['skills'] = [{
            'skill_id': 901,
            'name': '已观测攻击',
            'damage': 321,
            'hits': 1,
            'max_hit': 321,
        }]

        record = self.project(payload)
        projected = next(
            row for row in record['participants']
            if row['character_id'] == ally['character_id']
        )
        self.assertEqual(projected['damage'], 2_900)
        self.assertEqual(projected['skills_scope'], 'observed_partial')
        self.assertEqual(projected['skill_detail_status'], 'observed_partial')
        self.assertEqual(projected['skills'][0]['damage'], 321)

    def test_3v3_skill_tab_renders_each_selected_participant(self):
        payload = self.payload(3)
        expected_names = {}
        for index, row in enumerate(
            [*payload['allies'], *payload['enemies'][:3]]
        ):
            skill_name = f"已验证技能{index + 1}"
            row['skills'][0]['name'] = skill_name
            expected_names[row['character_id']] = skill_name

        record = self.project(payload)
        window = self.window()
        window.history_loaded_record = record
        window.history_selected_id = record['battle_id']
        window.history_records = []
        window.history_duel_analysis_tab = 'skills'
        window.icons = mock.Mock()
        window.icons.skill.return_value = object()
        window._ui_font = lambda _role: None
        window._profession_info = lambda _profession_id: ('', '#59c9ee')
        window._fit_ui_text = lambda value, _width, _role: value

        players = window._pvp_duel_participants()
        self.assertEqual(len(players), 6)
        for index, player in enumerate(players):
            with self.subTest(player=player['name']):
                window.history_duel_equipment_selected_player = (
                    window._pvp_duel_player_key(player, index)
                )
                window.history_duel_selected_side = (
                    window._pvp_duel_player_side(player)
                )
                selected = window._selected_pvp_duel_player()
                self.assertEqual(
                    selected['character_id'], player['character_id']
                )
                canvas = mock.Mock()
                window._draw_pvp_duel_skill_analysis(
                    canvas, 620, 360, selected
                )
                rendered = [
                    call.kwargs.get('text')
                    for call in canvas.create_text.call_args_list
                ]
                self.assertIn(
                    expected_names[player['character_id']], rendered
                )

    def test_cast_only_team_skill_data_reports_observation_without_fake_damage(self):
        window = self.window()
        window._ui_font = lambda _role: None
        canvas = mock.Mock()

        window._draw_pvp_duel_skill_analysis(canvas, 600, 400, {
            "side": "ally",
            "skills_scope": "observed_partial",
            "skills": [
                {"skill_id": 81000085, "casts": 2, "hits": 0, "damage": 0},
                {"skill_id": 81000098, "casts": 1, "hits": 0, "damage": 0},
            ],
        })

        messages = [call.kwargs.get("text") for call in canvas.create_text.call_args_list]
        self.assertIn("已观测 3 次技能释放，暂无可归属的命中伤害", messages)
        self.assertFalse(any("技能 81000085" in str(value) for value in messages))

    def test_native_pvp_skill_detail_separates_casts_from_damage_to_self(self):
        window = self.window()
        canvas = mock.Mock()
        canvas.winfo_width.return_value = 640
        canvas.winfo_height.return_value = 420
        window.history_skill_canvas = canvas
        window.history_selected_actor = 2
        window._selected_history_record = mock.Mock(return_value={
            "participants": [{
                "actor_id": 2,
                "side": "enemy",
                "skills": [
                    {"skill_id": 81000085, "casts": 2, "hits": 0, "damage": 0},
                ],
                "pvp_skills_from": [
                    {"skill_id": 86020010, "damage": 180, "hits": 2},
                ],
            }],
        })
        window.icons = mock.Mock()
        window._ui_font = lambda _role: None
        window._fit_ui_text = lambda value, _width, _role: value

        window._draw_pvp_history_skills()

        messages = [call.kwargs.get("text") for call in canvas.create_text.call_args_list]
        self.assertIn("已观测技能使用", messages)
        self.assertIn("技能 ID 81000085", messages)
        self.assertIn("2", messages)
        self.assertIn("--", messages)
        self.assertIn("对我的伤害（已观测）", messages)
        self.assertIn("已观测 180", messages)
        self.assertNotIn("占比", messages)

    def test_snapshot_roster_does_not_replace_team_totals_with_pair_totals(self):
        window = self.window()
        payload = self.payload(3)
        summary = window._pvp_history_summary(payload)
        allies, enemies, size = window._pvp_snapshot_rosters(summary)
        self.assertEqual((len(allies), len(enemies), size), (3, 3, 3))
        enemy = next(row for row in enemies if row["character_id"] == "enemy-0")
        self.assertEqual(enemy["damage"], 2_000)
        self.assertEqual(enemy["taken"], 2_500)

    def test_history_prefers_complete_record_over_same_match_partial_duplicate(self):
        window = self.window()
        complete = self.payload(3)
        complete["match_id"] = "complete-match"
        partial = deepcopy(complete)
        partial["match_id"] = "partial-match"
        partial["result"] = "未知"
        partial["allies"] = partial["allies"][:1]
        partial["enemies"] = partial["enemies"][:1]
        partial["opponents"] = partial["opponents"][:1]
        partial["data_scope"] = {
            "roster": "partial",
            "self_metrics": "self_exact",
            "team_metrics": "observed_partial",
            "opponent_interactions": "pair_exact",
        }
        for row in [*partial["allies"], *partial["enemies"]]:
            row["skills"] = []
            row["equipment_snapshot"] = None

        window.pvp_recording = SimpleNamespace(account_key="account-a")
        window.pvp_history_repository = SimpleNamespace(
            list=lambda _account_key: [partial, complete]
        )
        window.history_filter_time_var = SimpleNamespace(get=lambda: "all")
        window.history_filter_boss_var = None
        window.history_filter_result_var = None
        window.history_filter_entries = {}
        window.history_sort_var = None
        window.history_page_size = 10
        window.history_page_number = 1

        page, overview = window._query_pvp_history_summaries()

        self.assertEqual(page["total"], 1)
        self.assertEqual(overview["battle_count"], 1)
        self.assertEqual(page["records"][0]["battle_id"], "complete-match")

    def test_legacy_team_metrics_are_not_presented_as_verified_totals(self):
        window = self.window()
        payload = self.payload(3)
        payload.pop("data_scope")
        for collection in (payload["allies"], payload["enemies"]):
            for row in collection:
                row.pop("metrics_scope", None)
                row.pop("skills_scope", None)
                row.pop("statistics_authoritative", None)
        summary = window._pvp_history_summary(payload)
        self.assertIsNone(summary["kills"])
        self.assertIsNone(summary["my_damage"])
        allies, enemies, size = window._pvp_snapshot_rosters(summary)
        self.assertEqual((len(allies), len(enemies), size), (3, 3, 3))
        self.assertTrue(
            all(
                row[field] is None
                for row in [*allies, *enemies]
                for field in ("kills", "assists", "deaths", "damage", "healing", "taken")
            )
        )
        self.assertEqual(enemies[0]["extraordinary_rating"], 92_000)

    def test_legacy_1v1_keeps_exact_pair_metrics(self):
        window = self.window()
        payload = self.payload(1)
        payload.pop("data_scope")
        for row in [*payload["allies"], *payload["enemies"]]:
            row.pop("metrics_scope", None)
            row.pop("skills_scope", None)
            row.pop("statistics_authoritative", None)
        payload["enemies"][0].update(damage=180, taken=250)
        summary = window._pvp_history_summary(payload)
        allies, enemies, size = window._pvp_snapshot_rosters(summary)
        self.assertEqual(size, 1)
        self.assertEqual(summary["my_damage"], 3_000)
        self.assertEqual(enemies[0]["damage"], 180)
        self.assertEqual(enemies[0]["taken"], 250)

    def test_rating_stats_ignore_ai_and_missing_values(self):
        stats = DpsWindow._pvp_roster_rating_stats(
            [
                {"extraordinary_rating": 90_000, "is_ai": False},
                {"extraordinary_rating": None, "is_ai": False},
                {"extraordinary_rating": 999_999, "is_ai": True},
            ]
        )
        self.assertEqual(
            stats,
            {
                "average": 90_000,
                "highest": 90_000,
                "lowest": 90_000,
                "valid": 1,
                "total": 2,
            },
        )

    def test_roster_preview_uses_one_three_or_three_plus_remainder(self):
        for team_size, visible, omitted in (
            (1, 1, 0),
            (3, 3, 0),
            (6, 3, 3),
            (12, 3, 9),
        ):
            with self.subTest(team_size=team_size):
                allies = [
                    {
                        "character_id": f"ally-{index}",
                        "name": f"队友{index}",
                        "extraordinary_rating": 100_000 - index,
                        "is_self": index == 0,
                        "is_ai": False,
                    }
                    for index in range(team_size)
                ]
                enemies = [
                    {
                        "character_id": f"enemy-{index}",
                        "name": f"敌方{index}",
                        "extraordinary_rating": 90_000 + index,
                        "is_ai": False,
                    }
                    for index in range(team_size)
                ]
                preview = DpsWindow._pvp_roster_preview(
                    allies, enemies, team_size
                )
                self.assertEqual(len(preview["allies"]["rows"]), visible)
                self.assertEqual(len(preview["enemies"]["rows"]), visible)
                self.assertEqual(preview["allies"]["omitted"], omitted)
                self.assertEqual(preview["enemies"]["omitted"], omitted)
                self.assertTrue(
                    any(row["is_self"] for row in preview["allies"]["rows"])
                )
                expected_rating_markers = 0 if team_size == 3 else 1
                self.assertEqual(
                    sum(
                        bool(row["preview_highest"])
                        for row in preview["allies"]["rows"]
                    ),
                    expected_rating_markers,
                )
                self.assertTrue(preview["allies"]["complete"])
                if team_size > 1 and team_size != 3:
                    self.assertEqual(
                        sum(
                            bool(row["preview_lowest"])
                            for row in preview["allies"]["rows"]
                        ),
                        1,
                    )

    def test_roster_preview_never_marks_lowest_until_rating_is_complete(self):
        rows = [
            {
                "character_id": f"ally-{index}",
                "extraordinary_rating": None if index == 1 else 90_000 + index,
                "is_self": index == 0,
                "is_ai": False,
            }
            for index in range(6)
        ]
        preview = DpsWindow._pvp_roster_preview(rows, deepcopy(rows), 6)
        self.assertFalse(preview["allies"]["complete"])
        self.assertFalse(
            any(row["preview_lowest"] for row in preview["allies"]["rows"])
        )
        self.assertTrue(
            any(row["preview_highest"] for row in preview["allies"]["rows"])
        )

    def test_3v3_matchup_hides_ratings_and_shows_roster_members(self):
        payload = self.payload(3)
        allies = payload["allies"]
        enemies = payload["enemies"][:3]
        allies[1]["extraordinary_rating"] = None
        enemies[2]["extraordinary_rating"] = None

        summary = DpsWindow._pvp_history_matchup_summary(
            allies, enemies, 3
        )

        self.assertEqual(summary["title"], "双方阵容")
        self.assertEqual(summary["rows"], (("我方", "3人"), ("敌方", "3人")))
        self.assertEqual(summary["footer"], "")
        self.assertFalse(summary["ratings_visible"])
        preview = DpsWindow._pvp_roster_preview(allies, enemies, 3)
        self.assertFalse(
            any(
                row["preview_highest"] or row["preview_lowest"]
                for side in ("allies", "enemies")
                for row in preview[side]["rows"]
            )
        )

    def test_6v6_matchup_keeps_average_rating_without_completeness_text(self):
        payload = self.payload(6)
        allies = payload["allies"]
        enemies = payload["enemies"][:6]

        summary = DpsWindow._pvp_history_matchup_summary(
            allies, enemies, 6
        )

        self.assertEqual(summary["title"], "双方平均评分")
        self.assertTrue(summary["ratings_visible"])
        self.assertEqual(summary["rows"][0], ("我方", "88,000"))
        self.assertEqual(summary["footer"], "")

    def test_roster_preview_occupies_existing_gap_without_growing_list_width(self):
        for width in (700, 900, 1_100):
            with self.subTest(width=width):
                columns = DpsWindow._pvp_history_list_columns(width)
                self.assertEqual(columns["width"], width)
                self.assertLess(columns["info_left"], columns["preview_left"])
                self.assertLess(columns["preview_left"], columns["performance_left"])
                self.assertLess(columns["performance_left"], columns["matchup_left"])
                self.assertLess(columns["matchup_left"], columns["result_left"])
                self.assertLess(columns["result_left"], columns["action_left"])
                self.assertLess(columns["action_left"], columns["width"])

    def test_snapshot_cards_show_personal_kad_damage_and_taken(self):
        window = self.window()
        summary = window._pvp_history_summary(self.payload(3))
        window.backend_current_page = "kings"
        window.history_meter_mode = "dps"
        window.history_records = [summary]
        window.history_selected_id = summary["battle_id"]
        window.history_snapshot_labels = {
            key: LabelProbe()
            for key in (
                "team_dps", "my_dps", "my_damage", "my_share", "team_size", "duration",
                "team_dps_caption", "my_dps_caption", "my_damage_caption",
                "my_share_caption", "team_size_caption", "duration_caption",
            )
        }
        window.history_snapshot_trend_title_label = LabelProbe()
        window.history_snapshot_status_label = LabelProbe()
        window._render_history_snapshot_identity = mock.Mock()
        window._draw_history_snapshot_result = mock.Mock()
        window._draw_pvp_history_snapshot = mock.Mock()

        window._render_history_snapshot()

        labels = window.history_snapshot_labels
        self.assertEqual(labels["team_dps"].options["text"], "4 / 2 / 3")
        self.assertEqual(labels["team_dps_caption"].options["text"], "K/D/A")
        self.assertEqual(labels["my_damage"].options["text"], "3,000")
        self.assertEqual(labels["my_share"].options["text"], "92,000")
        self.assertEqual(labels["my_damage_caption"].options["text"], "我的伤害")
        self.assertEqual(labels["my_share_caption"].options["text"], "敌方平均评分")


if __name__ == "__main__":
    unittest.main()
