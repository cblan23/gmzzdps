"""Offline native-page, privacy and detached-response acceptance tests."""
from copy import deepcopy
import queue
from types import SimpleNamespace
import tkinter as tk
import tkinter.font as tkfont
import unittest
from unittest.mock import Mock

from pvp_backend_ui import (
    PvpHistoryPage, PvpAlliancePage, HUNTER_REPORT_BANNER,
    HISTORY_COLUMNS, PAIR_TABS,
    filter_records, merge_records, parse_members, sum_known,
    pvp_opponent_history, pvp_hunter_opponent_history,
    pvp_snapshot_rosters, pvp_team_size,
)
from test_pvp_backend import record


def fixture():
    payload = record()
    payload["player"]["name"] = "莫雪"
    payload["opponents"] = [
        dict(opponent_id="enemy-a", name="墨爵", profession_id=1_200_002,
             extraordinary_rating=88_893, club_name="星空远征团", kills=1, assists=None,
             defeats=1, damage=800, taken=200, damage_share=2 / 3, death_taken_share=.5,
             skills_outgoing=[dict(skill_id=42, damage=800, hits=2, max_hit=500, share=1)],
             skills_incoming=[dict(skill_id=43, damage=200, hits=1, max_hit=200, share=1)],
             timeline=[dict(type="kill", timestamp_ns=payload["started_at_ns"] + 1_000_000_000)],
             last_interaction_ns=payload["started_at_ns"] + 1_000_000_000,
             equipment_snapshot=dict(captured_at_ns=payload["started_at_ns"], extraordinary_rating=88_893,
                                     equipment_score=None, equipment=[dict(slot=1, item_id=100)],
                                     gems=None, attributes=None, sets=None)),
        dict(opponent_id="enemy-b", name="碎星", profession_id=1_200_003,
             extraordinary_rating=None, kills=0, assists=None, defeats=0,
             damage=400, taken=100, damage_share=1 / 3, death_taken_share=.25,
             skills_outgoing=[dict(skill_id=44, damage=400, hits=1, max_hit=400, share=1)],
             skills_incoming=[], timeline=[], equipment_snapshot=None),
    ]
    payload["taken"] = 300
    return payload


def texts(widget):
    result = []
    try:
        result.append(str(widget.cget("text")))
    except tk.TclError:
        pass
    if isinstance(widget, tk.Canvas):
        for item in widget.find_all():
            try:
                value = str(widget.itemcget(item, "text"))
            except tk.TclError:
                value = ""
            if value:
                result.append(value)
    for child in widget.winfo_children():
        result.append(texts(child))
    return "\n".join(result)


def team_fixture(team_size):
    payload = fixture()
    mode = {
        1: (5_200_280, 5_500_017, "房间1v1模式", "勇者对决"),
        3: (5_208_003, 5_500_002, "主宰争锋", "雪地场景"),
        6: (5_208_004, 5_500_010, "神座之争", "神座之争"),
        12: (5_203_003, 5_500_003, "命运时刻", "命运时刻"),
    }[team_size]
    payload.update(map_id=mode[0], mode_id=mode[1], mode_name=mode[2], map_name=mode[3],
                   team_size=team_size, result="胜利")
    payload["player"]["character_id"] = "self-uid"
    payload["allies"] = [
        dict(character_id="self-uid", name="莫雪", profession_id=1_200_001,
             extraordinary_rating=80_616, kills=2, damage=12_000, healing=300,
             taken=4_000, is_self=True, is_ai=False),
        *[
            dict(character_id=f"ally-{index}", name=f"队友{index}", profession_id=1_200_001 + index,
                 extraordinary_rating=88_000 + index, kills=index, damage=9_000 - index * 100,
                 healing=None, taken=2_000 + index, is_ai=False)
            for index in range(1, team_size)
        ],
    ]
    payload["enemies"] = [
        dict(character_id=f"enemy-{index}", name=f"敌方{index}", profession_id=1_200_020 + index,
             extraordinary_rating=(91_000 + index if index != 1 else None), kills=index,
             damage=20_000 - index * 500, healing=100 * index, taken=3_000 + index,
             is_ai=False)
        for index in range(team_size)
    ]
    payload["opponents"] = [
        dict(opponent_id=f"enemy-{index}", character_id=f"enemy-{index}", name=f"敌方{index}",
             profession_id=1_200_020 + index, extraordinary_rating=(91_000 + index if index != 1 else None),
             kills=0, assists=0, defeats=0, damage=0, taken=0, damage_share=0,
             death_taken_share=None, skills_outgoing=[], skills_incoming=[], timeline=[],
             last_interaction_ns=payload["started_at_ns"] + index)
        for index in range(team_size)
    ]
    return payload


def make_host(root):
    from test_combat_model import MODULE, DpsWindow, IconFactory
    host = object.__new__(DpsWindow)
    host.root = root
    host.hide_names = False
    host.backend_current_page = "kings"
    host.control_messages = queue.Queue()
    host.pvp_recording = SimpleNamespace(account_key="preview-account")
    host.pvp_history_repository = SimpleNamespace(list=lambda _account: [fixture()])
    host.licensing = SimpleNamespace(session=SimpleNamespace(access_token="preview-token"),
                                    gateway=SimpleNamespace(pvp_request=lambda _s, _a, _p: {"ok": False}))
    host.professions = MODULE["load_skill_metadata"]().get("professions", {})
    host.model = SimpleNamespace(skill_names={42: "技能甲", 43: "技能乙", 44: "另一对手技能"}, runtime_skill_names={})
    host.icons = IconFactory(root)
    host.ui_fonts = {role: tkfont.Font(root=root, family="Microsoft YaHei UI", size=size, weight=weight)
                     for role, size, weight in (("body", 10, "normal"), ("small", 9, "normal"),
                        ("micro", 8, "normal"), ("strong", 11, "bold"),
                        ("number_strong", 11, "bold"), ("number_large", 16, "bold"),
                        ("title", 20, "bold"), ("settings_title", 24, "bold"),
                        ("hero_title", 28, "bold"), ("table_header", 11, "bold"))}
    return host, MODULE["ModernDropdown"], MODULE["ModernScrollbar"]


class PvpViewModelTests(unittest.TestCase):
    def test_hunter_history_counts_direct_matches_without_inventing_incoming_damage(self):
        first = fixture()
        first["map_id"] = 5200167
        first["opponents"][0]["character_id"] = "enemy-a"
        first["opponents"][0].update(damage=800, taken=None, kills=1, defeats=0)
        second = deepcopy(first)
        second["match_id"] = "pvp_" + "2" * 32
        second["started_at_ns"] += 100_000_000_000
        second["ended_at_ns"] += 100_000_000_000
        second["opponents"][0].update(damage=300, taken=120, kills=0, defeats=1)

        history = pvp_hunter_opponent_history([first, second], "enemy-a")

        self.assertEqual((history["total"], history["damage_to"],
                          history["damage_from"], history["known_incoming"]),
                         (2, 1100, 120, 1))
        self.assertEqual((history["kills"], history["deaths"]), (1, 1))
        self.assertIsNone(history["matches"][1]["damage_from"])

    def test_equipment_row_click_switches_the_selected_slot(self):
        from test_combat_model import DpsWindow

        host = object.__new__(DpsWindow)
        canvas = SimpleNamespace(
            winfo_exists=lambda: True,
            canvasx=lambda value: value,
            canvasy=lambda value: value,
            _pvp_equipment_hit_rows=[
                (0, 20, 200, 60, 0),
                (0, 60, 200, 100, 1),
            ],
        )
        host.history_duel_analysis_tab = "equipment"
        host.history_duel_body_canvas = canvas
        host.history_duel_equipment_selected_index = 0
        host._draw_pvp_duel_analysis = Mock()
        host._select_pvp_duel_equipment_item(
            SimpleNamespace(x=40, y=75)
        )
        self.assertEqual(host.history_duel_equipment_selected_index, 1)
        host._draw_pvp_duel_analysis.assert_called_once_with()

    def test_record_merge_is_idempotent_and_keeps_local_immutable_snapshots(self):
        local = fixture()
        remote = deepcopy(local)
        remote["opponents"][0]["equipment_snapshot"]["equipment"][0]["item_id"] = 999
        merged = merge_records([local], [remote, remote])
        self.assertEqual(merged, [local])
        self.assertEqual(merged[0]["opponents"][0]["equipment_snapshot"]["equipment"][0]["item_id"], 100)

    def test_mode_time_search_and_unknown_assists(self):
        payload = fixture()
        now = payload["started_at_ns"] / 1e9 + 2 * 86400
        self.assertEqual(filter_records([payload], query="墨爵"), [payload])
        self.assertEqual(filter_records([payload], mode="猎城战"), [])
        self.assertEqual(filter_records([payload], period="近7天", now=now), [payload])
        self.assertEqual(filter_records([payload], period="今天", now=now), [])
        self.assertIsNone(sum_known([payload], "assists"))
        self.assertEqual(sum_known([], "assists"), 0)

    def test_batch_import_accepts_explicit_csv_and_tabs_without_inference(self):
        imported = parse_members('甲,role-a,1200001,普通成员\n乙\trole-b\t1200002\t核心成员')
        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[1]["profession_id"], 1_200_002)
        for value in ("", "甲,id,职业,普通成员", "甲,id,1200001,管理员", "甲,id", "甲,,0,普通成员"):
            with self.assertRaises(ValueError):
                parse_members(value)


class PvpNativePageTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.host, self.dropdown, self.scrollbar = make_host(self.root)
        self.parent = tk.Frame(self.root)

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def history(self):
        return PvpHistoryPage(self.parent, self.host, self.dropdown, self.scrollbar, fixed_records=[fixture()])

    def test_hunter_team_panel_pages_thirty_and_removes_departed_member(self):
        rows = [
            {
                "user_token": f"member-{index}",
                "name": f"成员{index}",
                "profession_id": 1_200_001,
                "rating_text": str(90_000 + index),
            }
            for index in range(72)
        ]
        self.host._preferred_main_topmost = lambda: False
        self.host._pvp_team_composition_rows = lambda: rows
        self.host._hunter_team_panel_available = lambda: True
        self.host.hunter_team_window = None
        self.host.hunter_team_refresh_after_id = None

        self.host._open_hunter_team_panel()
        panel = self.host.hunter_team_window
        try:
            content = texts(panel)
            self.assertIn("共 72 人", content)
            self.assertIn("显示分组 1  ·  30 人", content)
            self.assertIn("显示分组 2  ·  30 人", content)
            self.assertIn("显示分组 3  ·  12 人", content)
            self.assertIn("成员0", content)
            self.assertNotIn("成员71", content)
            self.host._select_hunter_team_page(2)
            self.assertIn("成员71", texts(panel))
            rows.pop()
            self.host._refresh_hunter_team_panel(force=True)
            self.assertIn("共 71 人", texts(panel))
            self.assertNotIn("成员71", texts(panel))
        finally:
            self.host._close_hunter_team_panel()

    def test_overview_uses_pve_style_record_table_and_snapshot_with_real_fields(self):
        page = self.history()
        content = texts(page.content)
        for caption in (
            "战斗时长", "模式 / 地图", "角色名称", "非凡评分", "战绩",
            "造成伤害", "结果", "查看", "战斗快照",
            "承伤", "我方", "敌方", "历史交手", "查看战斗详情",
            "竞技战绩", "显示字段", "战斗场次", "今日新增",
            "平均伤害", "最高伤害", "平均时长", "显示角色名称",
        ):
            self.assertIn(caption, content)
        for caption in ("诸王战绩", "PVP BATTLE ARCHIVE", "导出记录", "开始统计", "结束统计", "记录状态", "总承伤", "总治疗"):
            self.assertNotIn(caption, content)

    def test_time_only_numeric_skill_is_not_rendered_as_used(self):
        canvas = tk.Canvas(self.parent, width=420, height=260)
        canvas.pack()
        self.parent.pack()
        self.root.geometry("440x300")
        self.root.deiconify()
        self.root.update()
        self.host.history_duel_selected_side = "ally"
        self.host._draw_pvp_duel_skill_analysis(
            canvas,
            420,
            260,
            {
                "profession_id": 1_200_001,
                "pvp_skills_to": [{
                    "skill_id": 999_999,
                    "name": "",
                    "damage": 0,
                    "hits": 0,
                    "casts": 3,
                    "hit_timestamps_ns": [123],
                    "cast_timestamps_ns": [122],
                }],
            },
        )
        self.assertNotIn("999999", texts(canvas).replace(",", ""))

    def test_duel_skill_label_keeps_boon_name_without_catalog_suffix(self):
        canvas = tk.Canvas(self.parent, width=420, height=260)
        canvas.pack()
        self.parent.pack()
        self.root.geometry("440x300")
        self.root.deiconify()
        self.root.update()
        self.host.history_duel_selected_side = "ally"
        skill = {
            "skill_id": 80_003_003,
            "name": "恩赐词条-武器-3-2",
            "damage": 120,
            "hits": 1,
        }

        self.host._draw_pvp_duel_skill_analysis(
            canvas, 420, 260,
            {"profession_id": 1_200_001, "pvp_skills_to": [skill]},
        )

        self.assertIn("恩赐词条", texts(canvas))
        self.assertNotIn("恩赐词条-武器-3-2", texts(canvas))
        self.assertEqual(skill["name"], "恩赐词条-武器-3-2")

    def test_narrow_duel_rating_capsule_stays_inside_its_card(self):
        canvas = tk.Canvas(self.parent, width=190, height=215)
        canvas.pack()
        self.parent.pack()
        self.root.geometry("210x240")
        self.root.deiconify()
        self.root.update()
        player = {
            "name": "LongName",
            "profession_id": 1_200_001,
            "level": 80,
            "extraordinary_rating": 123_456,
        }
        self.host.history_duel_side_canvases = {"ally": canvas}
        self.host.history_duel_selected_side = "ally"
        self.host._pvp_duel_players = lambda: ({}, player, None)
        self.host._draw_pvp_duel_side_card("ally")
        width = canvas.winfo_width()
        rating_items = [
            item
            for item in canvas.find_all()
            if canvas.type(item) == "text"
            and abs(float(canvas.coords(item)[1]) - 128) < 0.5
        ]
        self.assertEqual(len(rating_items), 1)
        left, _top, right, _bottom = canvas.bbox(rating_items[0])
        self.assertGreaterEqual(left, 8)
        self.assertLessEqual(right, width - 8)

    def test_duel_equipment_cards_use_real_icons_and_quality_surfaces(self):
        canvas = tk.Canvas(self.parent, width=720, height=520)
        canvas.pack()
        self.parent.pack()
        self.root.geometry("740x560")
        self.root.deiconify()
        self.root.update()
        player = {
            "equipment_snapshot": {
                "captured_at": "2026-09-21 12:00:00",
                "equipment": [
                    {
                        "slot": 1,
                        "slot_name": "武器",
                        "item_id": 3_060_643,
                        "quality": 6,
                        "quality_name": "黄色品质",
                        "equipment_mode": "pvp",
                        "is_pvp": True,
                        "base_score": 2_910,
                        "random_score": 3_916,
                        "enhance_score": 320,
                        "enhance_level": 4,
                        "enhance_overall_percent": 50,
                        "known_score": 6_826,
                        "total_score": 7_146,
                        "score_complete": True,
                        "metadata": {"icon": "3060643"},
                        "affixes": [
                            {
                                "word_id": 3_930_158,
                                "name": "攻击力",
                                "property_value": 332,
                                "score": 870,
                            }
                        ],
                        "special_affix": {
                            "name": "恩赐词条-武器-3-2",
                            "score": 200,
                        },
                    },
                    {
                        "slot": 2,
                        "slot_name": "胸针",
                        "item_id": 3_210_603,
                        "quality": 7,
                        "quality_name": "红色品质",
                        "equipment_mode": "adventure",
                        "is_pvp": False,
                        "base_score": 3_642,
                        "random_score": 953,
                        "known_score": 4_595,
                        "score_complete": False,
                        "metadata": {"icon": "3210603"},
                    },
                ],
            }
        }
        self.host.history_duel_equipment_selected_index = 0
        with unittest.mock.patch.object(
            self.host.icons,
            "equipment_quality_surface",
            wraps=self.host.icons.equipment_quality_surface,
        ) as quality_surface, unittest.mock.patch.object(
            self.host.icons,
            "equipment_item",
            wraps=self.host.icons.equipment_item,
        ) as equipment_item:
            self.host._draw_pvp_duel_equipment_analysis(
                canvas, 720, 520, player
            )
        qualities = [int(call.args[0]) for call in quality_surface.call_args_list]
        self.assertIn(6, qualities)
        self.assertIn(7, qualities)
        self.assertTrue(
            any(call.kwargs.get("selected") for call in quality_surface.call_args_list)
        )
        item_ids = [int(call.args[0]) for call in equipment_item.call_args_list]
        self.assertIn(3_060_643, item_ids)
        self.assertIn(3_210_603, item_ids)

        rendered = texts(canvas)
        self.assertIn("黄色品质", rendered)
        self.assertIn("竞技装备", rendered)
        self.assertIn("强化 +4", rendered)
        self.assertIn("词条详情", rendered)
        self.assertIn("特殊 · 恩赐词条-武器", rendered)
        self.assertNotIn("恩赐词条-武器-3-2", rendered)
        self.assertEqual(
            player["equipment_snapshot"]["equipment"][0]["special_affix"]["name"],
            "恩赐词条-武器-3-2",
        )
        self.assertIn("裂金之战刃", rendered)
        self.assertNotIn("装备 ID", rendered)
        self.assertNotIn("下一阶", rendered)
        detail_mode = next(
            item
            for item in canvas.find_all()
            if canvas.type(item) == "text"
            and canvas.itemcget(item, "text") == "竞技装备"
        )
        self.assertEqual(canvas.itemcget(detail_mode, "anchor"), "center")

    def test_pve_detail_gear_tab_reuses_cards_and_selects_item(self):
        canvas = tk.Canvas(self.parent, width=720, height=510)
        canvas.pack()
        self.parent.pack()
        self.root.geometry("740x540")
        self.root.deiconify()
        self.root.update()
        self.host.history_skill_canvas = canvas
        self.host.history_page_mode = "detail"
        self.host.history_detail_mode = "equipment"
        self.host.history_equipment_selected_index = 0
        self.host.history_selected_actor = 1
        self.host._selected_history_record = lambda: {
            "combat_mode": "pve",
            "participants": [{
                "actor_id": 1, "is_self": True, "name": "Player",
                "equipment_snapshot": {
                    "captured_at_ns": 1_800_000_000_000_000_000,
                    "equipment": [
                        {"slot": 1, "item_id": 3_060_643, "quality": 6,
                         "equipment_mode": "adventure", "metadata": {}},
                        {"slot": 2, "item_id": 3_210_603, "quality": 7,
                         "equipment_mode": "pvp", "metadata": {}},
                    ],
                },
            }],
        }

        self.host._draw_history_skills()

        self.assertIn("快照时间", texts(canvas))
        self.assertEqual(len(canvas._pvp_equipment_hit_rows), 2)
        left, top, right, bottom, _index = canvas._pvp_equipment_hit_rows[1]
        self.host._select_history_equipment_item(
            SimpleNamespace(x=(left + right) // 2, y=(top + bottom) // 2)
        )
        self.assertEqual(self.host.history_equipment_selected_index, 1)

    def test_partial_opponent_equipment_shows_plain_score_and_one_explanation(self):
        canvas = tk.Canvas(self.parent, width=720, height=520)
        roster = tk.Canvas(self.parent, width=480, height=140)
        header = tk.Canvas(self.parent, width=720, height=120)
        canvas.pack()
        roster.pack()
        header.pack()
        self.parent.pack()
        self.root.geometry("740x820")
        self.root.deiconify()
        self.root.update()
        snapshot = {
            "equipment_score_complete": False,
            "partial": True,
            "equipment": [{
                "slot": 1, "slot_name": "武器", "item_id": 3_010_455,
                "base_score": 2_910, "random_score": 3_916,
                "known_score": 6_826, "total_score": None,
            }],
        }
        opponent = {
            "actor_id": 2, "name": "对手", "side": "enemy",
            "equipment_snapshot": snapshot,
        }
        self.host.history_duel_roster_canvas = roster
        self.host.history_duel_player_header = header
        self.host.history_duel_body_canvas = canvas
        self.host.history_duel_section_subtitle = tk.Label(self.parent)
        self.host.history_duel_analysis_tab = "equipment"
        self.host.history_duel_equipment_selected_player = ""
        self.host._pvp_duel_participants = lambda: [opponent]

        self.host._draw_pvp_duel_equipment_roster()
        self.host._draw_pvp_duel_player_header()
        self.host._draw_pvp_duel_analysis()

        self.assertIn("部分获取", texts(roster))
        self.assertIn("部分获取", texts(header))
        self.assertEqual(self.host.history_duel_section_subtitle.cget("text"),
                         "非本人装备分数暂不包含强化分")
        self.assertIn("装备分", texts(canvas))
        self.assertIn("6,826", texts(canvas))
        self.assertNotIn("已知", texts(canvas))
        self.assertNotIn("强化等级未返回", texts(canvas))
        self.assertNotIn("最终分", texts(canvas))
        self.assertIsNone(snapshot["equipment"][0]["total_score"])

    def test_equipment_timestamp_prefers_capture_ns_and_converts_utc_to_local_seconds(self):
        from datetime import datetime, timezone

        captured = datetime(2026, 9, 21, 8, 27, 20, 533123, tzinfo=timezone.utc)
        expected = captured.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        iso = captured.isoformat()
        self.assertEqual(
            self.host._history_equipment_timestamp({
                "captured_at_ns": int(captured.timestamp() * 1_000_000_000),
                "captured_at": "2020-01-01T00:00:00+00:00",
            }),
            expected,
        )
        self.assertEqual(self.host._history_equipment_timestamp({"captured_at": iso}), expected)
        self.assertEqual(self.host._history_equipment_timestamp({"captured_at": "2026-09-21 16:27:20"}),
                         "2026-09-21 16:27:20")

    def test_duel_estimated_healing_has_settings_style_explanation(self):
        canvas = tk.Canvas(self.parent, width=570, height=162)
        canvas.pack()
        self.parent.pack()
        self.root.geometry("590x190")
        self.root.deiconify()
        self.root.update()
        self.host.history_duel_side_canvases = {"ally": canvas}
        self.host.history_duel_selected_side = "ally"
        self.host._pvp_duel_players = lambda: ({}, {
            "name": "本人", "healing": 300,
            "healing_source": "hp_balance_estimate",
        }, None)
        self.host._draw_pvp_duel_side_card("ally")
        self.assertIn("≈300", texts(canvas))
        badge = canvas._pvp_duel_healing_help
        self.assertIn("估算治疗", badge.help_text)
        self.assertIn("护盾", badge.help_text)
        self.host._draw_pvp_duel_side_card("ally")
        self.assertIs(canvas._pvp_duel_healing_help, badge)

    def test_duel_hero_does_not_render_mode_badges(self):
        canvas = tk.Canvas(self.parent, width=760, height=126)
        canvas.pack()
        self.parent.pack()
        self.root.geometry("780x160")
        self.root.deiconify()
        self.root.update()
        self.host.history_duel_hero_canvas = canvas
        self.host._pvp_duel_players = lambda: ({"result": "unknown"}, None, None)
        self.host._selected_history_summary = lambda: {
            "stage_name": "Double Duel",
            "dungeon_name": "Arena",
            "result": "unknown",
        }
        self.host._draw_pvp_duel_hero()
        rendered = texts(canvas)
        self.assertNotIn("1V1", rendered)
        self.assertNotIn("Double Duel", rendered)

    def test_team3v3_hero_selects_the_supplied_team_banner(self):
        canvas = tk.Canvas(self.parent, width=760, height=126)
        canvas.pack()
        self.parent.pack()
        self.root.geometry("780x160")
        self.root.deiconify()
        self.root.update()
        self.host.history_duel_hero_canvas = canvas
        self.host._pvp_duel_players = lambda: (
            {"result": "unknown", "pvp_team_size": 3},
            None,
            None,
        )
        self.host._selected_history_summary = lambda: {
            "dungeon_name": "Arena",
            "result": "unknown",
        }
        with unittest.mock.patch.object(
            self.host.icons,
            "pvp_duel_hero",
            wraps=self.host.icons.pvp_duel_hero,
        ) as hero:
            self.host._draw_pvp_duel_hero()
        self.assertEqual(hero.call_args.args[2], 3)

    def test_pair_tabs_stay_on_same_page_and_skills_are_selected_pair_only(self):
        page = self.history()
        original_path = str(page)
        payload = page.records[0]
        payload["opponents"][0]["skills_incoming"].append(
            dict(skill_id=99, name="未定向施法", casts=2, hits=0, damage=0)
        )
        page.open_record(payload)
        content = texts(page.content)
        for caption in (
            "返回战斗记录", "战斗详情", "竞技战绩",
            "战斗时长", "本场战绩", "战斗结果",
            "造成伤害", "承受伤害", "战斗分析", "本场交手玩家",
            "点击左侧玩家", "我 → 他", "他 → 我",
        ):
            self.assertIn(caption, content)
        for tab in PAIR_TABS:
            page.choose_tab(tab)
            self.assertEqual(str(page), original_path)
            self.assertIs(page.selected_record, payload)
        page.choose_tab("技能分布")
        content = texts(page.content)
        self.assertIn("技能甲", content)
        self.assertIn("技能乙", content)
        self.assertNotIn("未定向施法", content)
        self.assertNotIn("另一对手技能", content)
        page.choose_opponent(payload["opponents"][1])
        content = texts(page.content)
        self.assertIn("另一对手技能", content)
        self.assertNotIn("技能甲", content)

    def test_privacy_toggle_hides_real_names_in_overview_and_detail(self):
        page = self.history()
        page.show_names.set(False)
        self.assertNotIn("莫雪", texts(page.content))
        page.open_record(page.records[0])
        content = texts(page.content)
        for name in ("莫雪", "墨爵", "碎星"):
            self.assertNotIn(name, content)
        self.assertIn("玩家 1", content)

    def test_equipment_snapshot_has_time_and_never_mutates_payload(self):
        page = self.history()
        payload = page.records[0]
        frozen = deepcopy(payload)
        page.open_record(payload)
        page.choose_tab("装备快照")
        content = texts(page.content)
        self.assertIn("装备快照 ·", content)
        self.assertRegex(content, r"装备快照 · \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        self.assertIn("物品 100", content)
        self.assertIn("不代表对方当前装备", content)
        self.assertIn("非本人装备分数暂不包含强化分", content)
        self.assertIn("部分装备快照", content)
        self.assertNotIn("装备评分", content)
        self.assertNotIn("其他快照字段", content)
        page.choose_opponent(payload["opponents"][1])
        self.assertIn("本场未获取", texts(page.content))
        self.assertEqual(payload, frozen)

    def test_equipment_snapshot_uses_captured_rating_and_only_returned_details(self):
        page = self.history()
        payload = page.records[0]
        opponent = payload["opponents"][0]
        opponent["extraordinary_rating"] = 90_001
        opponent["equipment_snapshot"].update(
            equipment_score=123, gems=[456], attributes={"health": 789},
            sets=[101], partial=False,
        )
        frozen = deepcopy(payload)
        page.open_record(payload)
        page.choose_tab("装备快照")
        content = texts(page.content)
        for value in ("88,893", "90,001", "装备评分", "123", "其他快照字段", "[456]", "789", "[101]"):
            self.assertIn(value, content)
        self.assertNotIn("部分装备快照", content)
        self.assertEqual(payload, frozen)

    def test_alliance_menu_is_open_without_club_certification(self):
        page = PvpAlliancePage(self.parent, self.host, self.dropdown, self.scrollbar)
        content = texts(page.content)
        self.assertIn("查找角色", content)
        self.assertIn("搜索角色", content)
        self.assertNotIn("需要俱乐部管理员权限", content)
        self.assertNotIn("添加成员", content)
        page.status_received({"ok": True, "authorized": False, "members": [{"character_name": "不能泄露"}]})
        content = texts(page.content)
        self.assertIn("查找角色", content)
        self.assertNotIn("需要俱乐部管理员权限", content)
        self.assertNotIn("不能泄露", content)
        self.assertNotIn("成员列表", content)
        self.assertNotIn("打开战报与分析", content)

    def test_alliance_home_requests_admin_status_for_authorized_shortcut(self):
        page = PvpAlliancePage(self.parent, self.host, self.dropdown, self.scrollbar)
        actions = []
        page.request = lambda action, _payload, _callback, **_kwargs: actions.append(action)

        page.on_show()

        self.assertEqual(actions, ["alliance/subscriptions/status", "alliance/status"])
        page.dispose()

    def test_admin_search_subscription_filters_and_report(self):
        self.host.backend_current_page = "alliance"
        page = PvpAlliancePage(self.parent, self.host, self.dropdown, self.scrollbar)
        member_key = "a" * 64
        player = dict(member_key=member_key, character_name="墨爵",
                      profession_id=1_200_002, matches=1,
                      last_active=fixture()["started_at_ns"] / 1e9)
        page.status_received(dict(ok=True, authorized=True,
            alliance=dict(club_name="星空远征团", club_id="club-a", admin_role="盟主"),
            members=[], subscriptions=[]))
        content = texts(page.content)
        for caption in ("查找角色", "搜索角色", "订阅动态", "生成订阅战报"):
            self.assertIn(caption, content)
        self.assertIn("打开战报与分析", content)
        self.assertNotIn("添加成员", content)
        self.assertNotIn("猎龙之城战报与分析", content)
        def count_entries(widget):
            return int(isinstance(widget, tk.Entry)) + sum(
                count_entries(child) for child in widget.winfo_children())
        self.assertEqual(count_entries(page.content), 1)
        match = fixture()
        summary = dict(match_id=match["match_id"], started_at_ns=match["started_at_ns"],
                       mode_name="四方联赛", map_name="四方联赛", result="未知",
                       kills=1, deaths=2, damage=1200)
        subscriptions = [dict(member_key=member_key, character_name="墨爵",
                              matches=1, new_matches=0, last_active=player["last_active"]),
                         dict(member_key="b" * 64, character_name="夜巡",
                              matches=2, new_matches=0, last_active=player["last_active"])]
        report = dict(subscribed_count=1, summary=dict(matches=1, wins=0,
                      losses=0, kills=1, deaths=2, damage=1200,
                      taken=0, taken_matches=0),
                      days=30, range_start=player["last_active"] - 30 * 86400,
                      range_end=player["last_active"],
                      trend=[dict(started_at=player["last_active"] - (12 - index) * 86400,
                                  matches=1 if index == 10 else 0,
                                  damage=1200 if index == 10 else 0,
                                  taken=0, taken_matches=0,
                                  kills=1 if index == 10 else 0,
                                  deaths=2 if index == 10 else 0)
                             for index in range(12)],
                      members=[dict(member_key=member_key, character_name="墨爵",
                                    matches=1, wins=0, kills=1, deaths=2,
                                    damage=1200, taken=0, taken_matches=0)],
                      modes=[dict(mode_name="四方联赛", matches=1)],
                      opponents=[dict(opponent_key="enemy-a", name="对手甲",
                                      interactions=1, damage=800, taken=200,
                                      taken_matches=1, kills=1, deaths=1)])
        calls = []
        def request(action, payload, callback, **_options):
            calls.append((action, payload))
            if action == "alliance/players/search":
                callback({"ok": True, "players": [player]})
            elif action == "alliance/players/records":
                callback({"ok": True, "total": 1, "records": [summary]})
            elif action == "alliance/players/record":
                callback({"ok": True, "record": match})
            elif action == "alliance/subscriptions/update":
                callback({"ok": True, "subscriptions": subscriptions})
            elif action == "alliance/subscriptions/report":
                callback({"ok": True, "report": report})
        page.request = request
        page.query.set("墨爵")
        page.search_player()
        self.assertIn("墨爵", texts(page.content))
        page.select_player(player)
        self.assertIn("战斗记录 · 共 1 场", texts(page.content))
        page.period.set("近7天")
        page.mode.set("四方联赛")
        page.result_filter.set("未知")
        page.load_player_records()
        self.assertEqual(calls[-1][1]["days"], 7)
        self.assertEqual(calls[-1][1]["mode_name"], "四方联赛")
        self.assertEqual(calls[-1][1]["result"], "未知")
        page.toggle_subscription()
        self.assertTrue(page.is_subscribed(member_key))
        page.open_player_record(summary)
        self.assertEqual(page.child_history.records[0]["match_id"], match["match_id"])
        self.assertEqual(page.child_history.selected_record["match_id"], match["match_id"])
        page.return_from_player_record()
        page._show_player_record_page(summary, loading=True)
        page.player_record_received({"ok": False, "message": "单场读取失败"})
        self.assertIsNone(page.child_history)
        self.assertIn("单场读取失败", texts(page.content))
        page.render_admin()
        self.assertIn("墨爵", texts(page.content))
        page.open_report()
        self.assertIn("订阅角色 · 伤害对比", texts(page.content))
        self.assertIn("墨爵", page.report_text())
        self.assertIn("实际统计", texts(page.content))
        self.assertEqual(calls[-1][1]["member_keys"], [member_key, "b" * 64])
        page.report_selection_var("b" * 64).set(False)
        page.report_selection_changed()
        self.assertIn("已选 1/2 位角色", texts(page.content))
        page.load_report()
        self.assertEqual(calls[-1][1]["member_keys"], [member_key])
        self.assertNotIn("胜利", page.report_text())
        page.report_period.set("自定义日期")
        page.report_start_date.set("2026-09-01")
        page.report_end_date.set("2026-09-25")
        page.load_report()
        self.assertEqual(calls[-1][1]["start_date"], "2026-09-01")
        self.assertEqual(calls[-1][1]["end_date"], "2026-09-25")
        page.copy_report()
        self.assertIn("墨爵", page.clipboard_get())
        before = len(calls)
        page.report_end_date.set("2026-08-31")
        page.load_report()
        self.assertEqual(len(calls), before)
        self.assertIn("有效的起止日期", texts(page.content))
        page.dispose()

    def test_subscription_report_charts_render_values_and_empty_states(self):
        page = PvpAlliancePage(self.parent, self.host, self.dropdown, self.scrollbar)
        canvas = tk.Canvas(self.parent, width=900, height=220)
        trend = [dict(started_at=1_789_700_000 + index * 86400,
                      matches=3 if index == 5 else 0,
                      damage=300 if index == 5 else 0)
                 for index in range(12)]
        page.draw_report_trend(canvas, trend, 30 * 86400, 900)
        self.assertIn("300", texts(canvas))
        page.draw_report_results(canvas, dict(kills=4, deaths=2), 900)
        self.assertIn("击杀  4", texts(canvas))
        page.draw_report_members(canvas, [dict(member_key="a" * 64,
            character_name="墨爵", damage=1200, matches=1)], 900)
        self.assertIn("墨爵", texts(canvas))
        page.draw_report_members(canvas, [dict(opponent_key="enemy-a",
            name="对手甲", damage=500, interactions=1)], 900, opponent=True)
        self.assertIn("对手甲", texts(canvas))
        page.draw_report_pie(canvas, [dict(character_name="墨爵", damage=1200),
                                      dict(character_name="夜巡", damage=800)], 900)
        self.assertIn("墨爵", texts(canvas))
        self.assertIn("60.0%", texts(canvas))
        page.draw_report_trend(canvas, [], 30 * 86400, 900)
        self.assertIn("暂无此项记录", texts(canvas))
        page.dispose()

    def test_hunter_report_hero_uses_separate_dragon_hunt_artwork(self):
        page = PvpAlliancePage(self.parent, self.host, self.dropdown,
                               self.scrollbar)
        canvas = tk.Canvas(self.parent, width=1100, height=220)
        self.assertTrue(HUNTER_REPORT_BANNER.is_file())
        page.draw_report_hero(canvas, 1100, 220)
        self.assertTrue(any(canvas.type(item) == "image"
                            for item in canvas.find_all()))
        self.assertTrue(canvas.find_withtag("report-back"))
        self.assertIn("猎龙之城", texts(canvas))
        page.dispose()

    def test_hunter_city_history_uses_no_win_loss_outcome(self):
        match = fixture()
        match.update(map_id=5200131, mode_name="猎城战",
                     map_name="猎龙之城", result="胜利")
        page = PvpHistoryPage(self.parent, self.host, self.dropdown,
                              self.scrollbar, fixed_records=[match])
        self.assertEqual(page.overview_outcome(match)[0], "无胜负")
        page.open_record(page.records[0])
        content = texts(page.content)
        self.assertIn("猎龙之城不判定胜负", content)
        self.assertIn("无胜负", content)
        page.dispose()

    def test_hunter_snapshot_shows_only_saved_allies_and_known_rating_average(self):
        match = team_fixture(3)
        match.update(map_id=5200167, mode_name="终末猎杀", map_name="猎龙之城")
        match["allies"][1]["extraordinary_rating"] = None
        page = PvpHistoryPage(self.parent, self.host, self.dropdown,
                              self.scrollbar, fixed_records=[match])
        content = texts(page.content)
        self.assertIn("终末猎杀", content)
        self.assertIn("我方阵容", content)
        self.assertIn("2 人有评分", content)
        self.assertIn("该类型无结果", content)
        self.assertNotIn("VS", content)
        self.assertNotIn("敌方0", content)
        page.dispose()

    def test_hunter_detail_shows_pair_data_history_and_no_assists(self):
        match = fixture()
        match.update(map_id=5200167, mode_name="终末猎杀",
                     map_name="猎龙之城", kills=1, deaths=1)
        match["opponents"][0]["character_id"] = "enemy-a"
        match["opponents"][0]["timeline"].append({
            "type": "assist", "timestamp_ns": match["started_at_ns"] + 2_000_000_000,
        })
        earlier = deepcopy(match)
        earlier["match_id"] = "pvp_" + "2" * 32
        earlier["started_at_ns"] -= 100_000_000_000
        earlier["ended_at_ns"] -= 100_000_000_000
        page = PvpHistoryPage(
            self.parent, self.host, self.dropdown, self.scrollbar,
            fixed_records=[match, earlier],
        )
        page.open_record(page.records[0])
        content = texts(page.content)
        self.assertIn("击杀 / 阵亡", content)
        self.assertIn("历史交手", content)
        self.assertIn("承伤记录", content)
        self.assertNotIn("仅汇总有精确承伤的场次", content)
        self.assertNotIn("助攻", content)
        self.assertNotIn("伤害占比", content)
        self.assertNotIn("交手时间", content)
        page.render = Mock(side_effect=AssertionError("tab switch rebuilt the full detail page"))
        page.choose_tab("装备快照")
        self.assertIn("装备快照", texts(page.content))
        self.assertEqual(page.detail_tab_buttons["装备快照"].cget("fg"), "#6fe3bd")
        self.assertEqual(page.detail_tab_buttons["交手总览"].cget("fg"), "#8d99a8")
        page.choose_tab("历史交手")
        self.assertIn("历史交手 2 场", texts(page.content))
        page.choose_tab("技能分布")
        self.assertIn("本人使用的技能", texts(page.content))
        self.assertIn("对手使用的技能", texts(page.content))
        self.assertEqual(page.detail_tab_buttons["技能分布"].cget("fg"), "#6fe3bd")
        page.dispose()

    def test_native_history_opens_and_closes_hunter_opponent_overlay(self):
        match = fixture()
        match.update(map_id=5200167, mode_name="终末猎杀", map_name="猎龙之城")
        self.host.backend_page_host = self.parent
        self.host.history_hunter_detail_page = None
        self.host.pvp_history_repository = SimpleNamespace(list=lambda _account: [match])

        self.host._open_hunter_history_detail(match)

        page = self.host.history_hunter_detail_page
        self.assertIsNotNone(page)
        self.assertEqual(page.winfo_manager(), "grid")
        self.assertEqual(page.selected_record["match_id"], match["match_id"])
        self.assertIn("本场交手玩家", texts(page.content))
        self.host._close_hunter_history_detail()
        self.assertIsNone(self.host.history_hunter_detail_page)

    def test_late_response_from_old_account_page_or_request_is_ignored(self):
        page = self.history()
        callback = Mock()
        page.pending["records"] = ("new-request", callback)
        envelope = dict(instance=page.instance_key, account_key=page.account_key, channel="records", request_id="old-request", response={"ok": True})
        page.receive(envelope)
        self.assertFalse(callback.called)
        envelope["request_id"] = "new-request"
        self.host.pvp_recording.account_key = "another-account"
        page.receive(envelope)
        self.assertFalse(callback.called)
        self.host.pvp_recording.account_key = page.account_key
        page.receive(envelope)
        self.assertEqual(callback.call_count, 1)
        page.dispose()
        page.receive(envelope)
        self.assertEqual(callback.call_count, 1)

    def test_admin_hunter_analysis_query_and_record_entry(self):
        self.host.backend_current_page = "alliance"
        page = PvpAlliancePage(self.parent, self.host, self.dropdown, self.scrollbar)
        member_key = "a" * 64
        page.status_received(dict(ok=True, authorized=True,
            alliance=dict(club_name="星空远征团", club_id="club-a", admin_role="盟主"),
            members=[dict(member_key=member_key, character_name="墨爵",
                          profession_id=1_200_002, club_role="核心成员",
                          matches=1, kills=1, assists=0, deaths=1)]))
        self.assertNotIn("猎龙之城战报与分析", texts(page.content))
        match = fixture()
        match.update(map_id=5200167, mode_name="终末猎杀", map_name="猎龙之城")
        calls = []
        def request(action, payload, callback, **_options):
            calls.append((action, payload))
            if action == "alliance/status":
                callback(dict(ok=True, authorized=True,
                    alliance=dict(club_name="星空远征团", club_id="club-a", admin_role="盟主"),
                    members=[dict(member_key=member_key, character_name="墨爵")]))
                return
            if action == "alliance/hunter/record":
                callback({"ok": True, "record": match})
                return
            callback(dict(ok=True, days=30, member_key=payload["member_key"],
                summary=dict(matches=1, members=1, kills=1, deaths=1,
                             damage=1200, taken=300, taken_matches=1,
                             complete_matches=1),
                members=[dict(member_key=member_key, character_name="墨爵",
                              matches=1, kills=1, deaths=1, damage=1200,
                              taken=300, taken_matches=1)],
                skills_outgoing=[dict(skill_id=42, name="技能甲", hits=2,
                                      damage=800, max_hit=500)],
                skills_incoming=[dict(skill_id=43, name="技能乙", hits=1,
                                      damage=200, max_hit=200)],
                records=[match]))
        page.request = request
        page.open_hunter()
        content = texts(page.content)
        for caption in ("战斗场次", "成员表现", "我命中敌人的技能",
                        "敌人命中我的技能", "技能甲", "技能乙", "终末猎杀"):
            self.assertIn(caption, content)
        self.assertEqual(calls[0], ("alliance/status", {}))
        self.assertEqual(calls[1], ("alliance/hunter/analysis",
                                    {"days": 30, "member_key": ""}))
        page.select_hunter_member({"member_key": member_key})
        self.assertEqual(calls[-1][1]["member_key"], member_key)
        page.view_hunter_record(match)
        self.assertEqual(calls[-1], ("alliance/hunter/record", {"match_id": match["match_id"]}))
        self.assertEqual(page.child_history.records[0]["match_id"], match["match_id"])
        self.assertEqual(page.child_history.selected_record["match_id"], match["match_id"])
        page.return_from_hunter()
        self.assertIsNone(page.child_history)
        page.dispose()

    def test_worker_response_is_delivered_only_through_main_thread_queue(self):
        page = self.history()
        callback = Mock()
        self.host.licensing.gateway.pvp_request = lambda session, _a, _p: {"ok": True, "token": session.access_token}
        page.request("records/list", {}, callback, channel="records")
        kind, envelope = self.host.control_messages.get(timeout=2)
        self.assertEqual(kind, "pvp_backend_response")
        self.assertFalse(callback.called)
        page.receive(envelope)
        callback.assert_called_once_with({"ok": True, "token": "preview-token"})

    def test_history_rereads_local_store_when_returning_from_detail(self):
        page = PvpHistoryPage(self.parent, self.host, self.dropdown, self.scrollbar)
        first = fixture()
        second = deepcopy(first)
        second["match_id"] = "pvp_" + "2" * 32
        records = [first]
        self.host.pvp_history_repository.list = lambda _account: list(records)
        page.render()
        self.assertEqual(len(page.records), 1)
        page.open_record(first)
        records.append(second)
        page.overview()
        self.assertEqual(len(page.records), 2)

    def test_snapshot_switches_1v1_3v3_6v6_and_12v12_without_resizing_page(self):
        for team_size in (1, 3, 6, 12):
            with self.subTest(team_size=team_size):
                payload = team_fixture(team_size)
                page = PvpHistoryPage(
                    self.parent,
                    self.host,
                    self.dropdown,
                    self.scrollbar,
                    fixed_records=[payload],
                )
                content = texts(page.content)
                self.assertIn(f"{team_size}V{team_size}", content)
                self.assertIn("历史交手", content)
                self.assertIn("莫雪", content)
                self.assertIn("敌方0", content)
                if team_size == 1:
                    self.assertIn("对手", content)
                    self.assertIn("VS", content)
                else:
                    self.assertIn("我方", content)
                    self.assertIn("敌方", content)
                if team_size in {6, 12}:
                    self.assertIn("我方平均", content)
                    self.assertIn("敌方平均", content)
                    self.assertIn("超凡评分", content)
                else:
                    self.assertNotIn("我方平均", content)
                page.destroy()

    def test_team_snapshot_renders_profession_icon_for_each_real_row(self):
        with unittest.mock.patch.object(
            self.host.icons,
            "main_profession",
            wraps=self.host.icons.main_profession,
        ) as main_profession:
            page = PvpHistoryPage(
                self.parent,
                self.host,
                self.dropdown,
                self.scrollbar,
                fixed_records=[team_fixture(3)],
            )

        rendered = [
            int(call.args[0])
            for call in main_profession.call_args_list
            if call.args
            and call.kwargs.get("size") == 30
        ]
        self.assertIn(1_200_002, rendered)
        self.assertIn(1_200_003, rendered)
        page.destroy()

    def test_detail_uses_native_pve_component_dimensions_and_internal_scrollbars(self):
        from test_combat_model import MODULE

        page = PvpHistoryPage(
            self.parent,
            self.host,
            self.dropdown,
            self.scrollbar,
            fixed_records=[team_fixture(12)],
            split_pane=MODULE["ModernSplitPane"],
        )
        page.open_record(page.records[0])
        self.root.update_idletasks()
        children = page.content.winfo_children()
        native_parts = {
            getattr(child, "_native_history_detail_part", "")
            for child in children
        }
        self.assertTrue(
            {"toolbar", "hero", "metrics", "tabs", "workspace"}.issubset(
                native_parts
            )
        )
        self.assertTrue(any(isinstance(child, tk.Canvas) and int(child.cget("height")) == 190 for child in children))
        self.assertTrue(any(child.__class__.__name__ == "ModernSplitPane" and int(child.cget("height")) == 570
                            for child in children))
        fixed_heights = [int(child.cget("height")) for child in children if isinstance(child, tk.Frame)]
        self.assertIn(44, fixed_heights)
        self.assertIn(76, fixed_heights)
        self.assertIn(48, fixed_heights)
        self.assertGreaterEqual(len([child for child in page.winfo_children() if isinstance(child, tk.Canvas)]), 1)


class PvpSnapshotModelTests(unittest.TestCase):
    def test_team_size_comes_from_confirmed_mode_not_observed_opponent_count(self):
        payload = team_fixture(3)
        payload["opponents"] = payload["opponents"][:1]
        self.assertEqual(pvp_team_size(payload), 3)
        payload.pop("team_size")
        self.assertEqual(pvp_team_size(payload), 3)

    def test_team_modes_do_not_present_direct_interaction_as_enemy_match_totals(self):
        payload = fixture()
        payload.update(map_id=5_208_003, mode_id=5_500_002, team_size=3)
        allies, enemies = pvp_snapshot_rosters(payload)
        self.assertEqual(len(allies), 1)
        self.assertEqual(len(enemies), 2)
        self.assertTrue(all(row["damage"] is None and row["kills"] is None for row in enemies))

    def test_1v1_directional_damage_is_complete_and_can_be_shown_for_both_players(self):
        payload = fixture()
        payload.update(map_id=5_200_280, mode_id=5_500_017, team_size=1)
        payload["opponents"] = payload["opponents"][:1]
        _allies, enemies = pvp_snapshot_rosters(payload)
        self.assertEqual(enemies[0]["damage"], 200)
        self.assertEqual(enemies[0]["taken"], 800)
        self.assertEqual(enemies[0]["kills"], 1)

    def test_history_uses_uid_whole_match_result_and_only_finalized_records(self):
        rows = []
        for index, outcome in enumerate(("胜利", "失败", "未知", "胜利", "失败", "胜利")):
            payload = team_fixture(1)
            payload["match_id"] = f"pvp_{index:032x}"
            payload["result"] = outcome
            payload["started_at_ns"] += index
            payload["ended_at_ns"] += index
            rows.append(payload)
        same_name_different_uid = team_fixture(1)
        same_name_different_uid["opponents"][0]["character_id"] = "different-uid"
        same_name_different_uid["opponents"][0]["opponent_id"] = "different-uid"
        same_name_different_uid["enemies"][0]["character_id"] = "different-uid"
        rows.append(same_name_different_uid)
        active = team_fixture(1)
        active.pop("ended_at_ns")
        active.pop("ended_at", None)
        active.pop("end_time", None)
        rows.append(active)
        history = pvp_opponent_history(rows, "enemy-0")
        self.assertEqual(
            (history["total"], history["wins"], history["losses"], history["unknown"]),
            (6, 3, 2, 1),
        )
        self.assertEqual(len(history["recent"]), 5)


if __name__ == "__main__":
    unittest.main()
