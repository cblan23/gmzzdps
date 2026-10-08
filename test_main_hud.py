from pathlib import Path
import copy
import colorsys
import unittest
from unittest import mock
from types import SimpleNamespace

from PIL import Image, ImageChops

from main_hud import (
    HUD_DEFAULT_VISIBLE_ROWS,
    HUD_LOGICAL_WIDTH,
    HUD_LOGICAL_WIDTH_WITHOUT_DEATHS,
    HUD_MAX_VISIBLE_ROWS,
    MainHudRenderer,
    WindowsLayeredPresenter,
)
from main_hud_artwork import (
    ACTION_BOXES,
    REFERENCE_BOUNDS,
    REFERENCE_SCALE,
    _dilate,
    equipment_type_badge,
    pvp_equipment_type_badge,
)


ROOT = Path(__file__).resolve().parent
COLORS = {
    1_200_001: "#f2cd32",
    1_200_002: "#7ecfa5",
    1_200_003: "#5869c4",
    1_200_004: "#6687c5",
    1_200_005: "#68b6e5",
    1_200_006: "#ee8c2f",
    1_200_007: "#a255c7",
}


def snapshot(*, deaths=True, row_count=1):
    return {
        "time": "00:19",
        "boss_name": "伤害木桩",
        "boss_hp": "5.2亿 / 5.2亿",
        "boss_percent": "100%",
        "boss_ratio": 1.0,
        "rows": [
            {
                "actor_id": index + 1,
                "profession_id": 1_200_001 + index % 7,
                "name": f"玩家{index + 1}",
                "metric": "dps",
                "stat_text": "1,048,208/s",
                "total_text": "(1782万)",
                "deaths": index,
                "is_self": index == 0,
            }
            for index in range(row_count)
        ],
        "show_time": True,
        "show_boss": True,
        "show_totals": True,
        "show_deaths": deaths,
        "show_team_dps": True,
        "show_pvp": True,
        "highlight_self": True,
        "team_dps": "8,970,096",
    }


class MainHudRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = MainHudRenderer(ROOT / "assets", COLORS, supersample=2)

    def test_footer_has_only_the_four_persistent_actions(self):
        result = self.renderer.render(snapshot(), pixel_scale=1.0)
        actions = {key for key in result.hit_regions if key.startswith("action:")}
        self.assertEqual(
            actions,
            {"action:pvp", "action:settings", "action:lock", "action:pin"},
        )

    def test_no_boss_header_renders_generic_monster_without_fake_health(self):
        state = snapshot()
        mode = {
            "boss_name": "当前没有Boss · 小怪战斗",
            "boss_available": True,
            "boss_icon": "live-small-monsters-v2.png",
            "live_no_boss": True,
            "boss_hp": "",
            "boss_percent": "",
            "boss_ratio": None,
        }
        state.update(mode)
        state["bosses"] = [mode]

        with mock.patch.object(
            self.renderer,
            "_boss_portrait",
            wraps=self.renderer._boss_portrait,
        ) as portrait:
            result = self.renderer.render(state, pixel_scale=1.0)

        self.assertEqual(
            result.image.size,
            self.renderer.render(snapshot(), pixel_scale=1.0).image.size,
        )
        self.assertIsNotNone(result.image.getbbox())
        portrait.assert_any_call(None, "live-small-monsters-v2.png")

    def test_live_hud_boss_uses_its_display_only_portrait_alias(self):
        state = snapshot()
        boss = {
            "boss_name": "卡尔·埃德加",
            "boss_available": True,
            "boss_template_id": 7_115_718,
            "boss_icon": "karl-edgar.png",
            "live_hud_boss": True,
            "boss_hp": "1409.2万 / 1409.5万",
            "boss_percent": "99.9%",
            "boss_ratio": 0.999,
        }
        state.update(boss)
        state["bosses"] = [boss]

        with mock.patch.object(
            self.renderer,
            "_boss_portrait",
            wraps=self.renderer._boss_portrait,
        ) as portrait:
            result = self.renderer.render(state, pixel_scale=1.0)

        self.assertIsNotNone(result.image.getbbox())
        portrait.assert_any_call(7_115_718, "karl-edgar.png")

    def test_pvp_page_has_dedicated_rows_and_blue_pve_return_action(self):
        state = {
            "combat_mode": "pvp",
            "time": "08:42",
            "pvp_player_name": "莫雪",
            "pvp_rating": "80616",
            "pvp_kills": 2,
            "pvp_assists": "--",
            "pvp_deaths": 1,
            "pvp_outgoing": [
                {
                    "actor_id": 101,
                    "name": "对手甲",
                    "rating": "79840",
                    "kills": 1,
                    "assists": "--",
                    "damage_text": "328.6万",
                    "share_text": "38.1%",
                }
            ],
            "pvp_incoming": [
                {
                    "actor_id": 201,
                    "name": "对手乙",
                    "rating": "84270",
                    "defeats": 1,
                    "damage_text": "214.8万",
                    "share_text": "46.4%",
                }
            ],
            "pvp_total_damage": "862.4万",
            "pvp_total_taken": "463.1万",
            "pvp_status": "统计中",
            "pvp_result": "进行中",
            "pvp_active": True,
        }

        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            result = self.renderer.render(state, pixel_scale=1.0)

        actions = {key for key in result.hit_regions if key.startswith("action:")}
        self.assertEqual(
            actions,
            {
                "action:pvp_live",
                "action:pvp_team",
                "action:pve",
                "action:settings",
                "action:lock",
                "action:pin",
            },
        )
        texts = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("战果", texts)
        self.assertIn("阵亡", texts)
        self.assertIn("对手甲", texts)
        self.assertNotIn("全队秒伤", texts)
        self.assertEqual(
            [actor for _bounds, actor in result.actor_regions],
            [101, 201],
        )

    def test_hunter_city_pvp_hud_keeps_team_tab_and_personal_combat(self):
        state = dict(
            combat_mode="pvp", pvp_self_only=True, pvp_hud_view="team",
            pvp_in_map=True, pvp_map_name="终末猎杀",
            pvp_player_name="本人", pvp_rating="80616",
            pvp_teammates=[dict(name="队友", profession_id=1_200_002)],
            pvp_team_rows=[dict(actor_id=2, name="队友", rating="90000",
                                profession_id=1_200_002)],
            pvp_team_average_rating="90000",
            pvp_outgoing=[dict(actor_id=101, name="敌人甲", kills=1,
                               damage=1200, damage_text="1200")],
            pvp_incoming=[dict(actor_id=102, name="敌人乙", defeats=1,
                               damage=300, damage_text="300")],
            pvp_total_damage="1200", pvp_total_taken="300",
            pvp_kills=1, pvp_deaths=1,
        )
        with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
            result = self.renderer.render(state, pixel_scale=1.0)
        captions = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("团队构成", captions)
        self.assertIn("共 1 人", captions)
        self.assertIn("个人战果", captions)
        self.assertIn("团队超凡评分", captions)
        self.assertIn("队友", captions)
        self.assertIn("action:pvp_team", result.hit_regions)
        self.assertIn("action:pvp_live", result.hit_regions)
        state["pvp_hud_view"] = "live"
        with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
            result = self.renderer.render(state, pixel_scale=1.0)
        captions = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("本人超凡评分", captions)
        self.assertIn("团队构成", captions)
        self.assertIn("输出占比", captions)
        self.assertNotIn("助攻", captions)
        self.assertNotIn("击杀 / 助攻", captions)
        self.assertIn("action:pvp_team", result.hit_regions)
        self.assertEqual([actor for _bounds, actor in result.actor_regions], [101, 102])

    def test_hunter_healer_hud_shows_effective_healing_and_keeps_death(self):
        state = dict(
            combat_mode="pvp", pvp_self_only=True, pvp_healer=True,
            pvp_hud_view="live", pvp_in_map=True, pvp_map_name="终末猎杀",
            time="13:04", pvp_player_name="本人", pvp_deaths=1,
            pvp_kills=2, pvp_effective_healing="123,456",
            pvp_total_damage="999", pvp_total_taken="50",
            pvp_teammates=[dict(name="已隐藏的队友", profession_id=1_200_002)],
            pvp_outgoing=[dict(name="敌人", damage=999, kills=2)],
            pvp_incoming=[],
        )
        with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
            self.renderer.render(state, pixel_scale=1.0)
        captions = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("终末猎杀", captions)
        self.assertIn("有效治疗", captions)
        self.assertIn("123,456", captions)
        self.assertIn("阵亡", captions)
        self.assertNotIn("已隐藏的队友", captions)
        self.assertNotIn("13:04", captions)
        self.assertNotIn("击杀", captions)
        self.assertNotIn("伤害", captions)

    def test_hunter_dragon_view_separates_monster_damage_from_player_damage(self):
        state = dict(
            combat_mode="pvp", pvp_self_only=True, pvp_hud_view="live",
            pvp_in_map=True, pvp_map_name="终末猎杀",
            pvp_player_name="本人", pvp_kills=0, pvp_deaths=1,
            pvp_total_damage="1,200", pvp_total_taken="300",
            pvp_monsters=[{
                "name": "战争巨龙", "damage_to": 4500,
                "damage_from": 200, "defeated": True,
            }],
        )
        with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
            result = self.renderer.render(state, pixel_scale=1.0)
        captions = [str(call.args[0]) for call in labels.call_args_list]
        dragon_caption = next(caption for caption in captions if "战争巨龙" in caption)
        self.assertIn("对龙伤害 4500", dragon_caption)
        self.assertIn("承受龙伤害 200", dragon_caption)
        self.assertIn("1,200", captions)
        self.assertIn("300", captions)
        self.assertIn("action:pvp_live", result.hit_regions)

    def test_pvp_map_header_shows_only_mode_name(self):
        state = dict(combat_mode="pvp", pvp_in_map=True,
                     pvp_map_name="四方联赛", pvp_player_name="本人",
                     pvp_teammates=[dict(name="墨爵", profession_id=1_200_002),
                                    dict(name="碎星", profession_id=1_200_003)],
                     pvp_teammate_offset=1)
        with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
            with mock.patch.object(self.renderer, "_profession", wraps=self.renderer._profession) as professions:
                result = self.renderer.render(state)
        rendered = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("四方联赛", rendered)
        self.assertNotIn("碎星", rendered)
        self.assertNotIn("墨爵", rendered)
        self.assertNotIn("2/2", rendered)
        professions.assert_not_called()
        self.assertNotIn("scroll:pvp_teammates", result.hit_regions)
        state["pvp_teammates"] = []
        state["pvp_map_name"] = "主宰争锋"
        with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
            result = self.renderer.render(state)
        rendered = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("主宰争锋", rendered)
        self.assertNotIn("暂无队友", rendered)
        self.assertNotIn("scroll:pvp_teammates", result.hit_regions)

    def test_pvp_live_team_name_renders_rating_or_ai_label(self):
        state = dict(
            combat_mode="pvp", pvp_hud_view="live", pvp_team_battle=True,
            pvp_team_size=6, pvp_player_name="本人",
            pvp_allies=[
                dict(name="真人", profession_id=1_200_002, rating="133344"),
                dict(name="人机", profession_id=1_200_003, rating="人机", is_ai=True),
            ],
            pvp_enemies=[
                dict(name="未取到评分", profession_id=1_200_004, rating="--"),
            ],
        )
        with mock.patch.object(
            self.renderer, "_fitted_label", wraps=self.renderer._fitted_label
        ) as fitted:
            self.renderer.render(state)

        rendered = [str(call.args[0]) for call in fitted.call_args_list]
        self.assertIn("（133344）", rendered)
        self.assertIn("（人机）", rendered)
        self.assertIn("（--）", rendered)

    def test_pvp_team_hides_bot_equipment_and_assist_labels(self):
        team = dict(
            combat_mode="pvp", pvp_hud_view="team", pvp_assists=5,
            pvp_team_rows=[dict(
                actor_id=7, name="机器人", profession_id=1_200_001,
                rating="0", is_ai=True, equipment_profile_ready=True,
                equipment_count=1, pvp_equipment_count=0,
                active_word_count=0, total_word_count=1,
            )],
        )
        with mock.patch.object(self.renderer, "_fitted_label", wraps=self.renderer._fitted_label) as fitted:
            with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
                self.renderer.render(team)
        fitted_text = [str(call.args[0]) for call in fitted.call_args_list]
        label_text = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("（人机）", fitted_text)
        self.assertNotIn("冒险装备 ×1", fitted_text)
        self.assertNotIn("0/1", fitted_text)
        self.assertIn("1组", label_text)
        self.assertIn("共 1 人", label_text)
        self.assertNotIn("助攻", label_text)

        live = dict(team, pvp_hud_view="live", pvp_team_battle=True,
                    pvp_allies=[dict(name="队友", assists=5)],
                    pvp_enemies=[dict(name="敌人", assists=3)])
        with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
            self.renderer.render(live)
        label_text = [str(call.args[0]) for call in labels.call_args_list]
        self.assertNotIn("助攻", label_text)
        self.assertNotIn("击杀 / 助攻", label_text)

    def test_pvp_enemy_live_row_shows_only_observed_damage_to_self(self):
        state = dict(
            combat_mode="pvp", pvp_hud_view="live", pvp_team_battle=True,
            pvp_team_size=3, pvp_player_name="本人",
            pvp_allies=[dict(name="本人", profession_id=1_200_002, is_self=True)],
            pvp_enemies=[dict(
                name="敌方", profession_id=1_200_001,
                damage=10_000, damage_to_self=2039, current_dead=True,
            )],
        )
        with mock.patch.object(self.renderer, "_label", wraps=self.renderer._label) as labels:
            self.renderer.render(state)

        rendered = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("对我伤害", rendered)
        self.assertTrue(any(value in rendered for value in ("2039", "2,039")))
        self.assertNotIn("状态", rendered)
        self.assertNotIn("☠", rendered)
        self.assertNotIn("10000", rendered)

    def test_domination_header_stays_mode_only_after_teammates_arrive(self):
        state = dict(
            combat_mode="pvp",
            pvp_in_map=True,
            pvp_map_name="主宰争锋",
            pvp_player_name="本人",
            pvp_teammates=[
                dict(name="墨爵", profession_id=1_200_002),
                dict(name="碎星", profession_id=1_200_003),
            ],
            pvp_teammate_offset=1,
        )
        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            with mock.patch.object(
                self.renderer, "_profession", wraps=self.renderer._profession
            ) as professions:
                result = self.renderer.render(state)

        rendered = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("主宰争锋", rendered)
        self.assertNotIn("墨爵", rendered)
        self.assertNotIn("碎星", rendered)
        self.assertNotIn("2/2", rendered)
        professions.assert_not_called()
        self.assertNotIn("scroll:pvp_teammates", result.hit_regions)

    def test_shared_title_tabs_clear_timer_and_idle_pvp_title_stays_complete(self):
        state = dict(
            combat_mode="pvp",
            time="00:00",
            pvp_hud_view="live",
            pvp_map_name="PVP 对战",
            pvp_team_average_rating="88801",
        )
        with mock.patch.object(
            self.renderer, "_fitted_label", wraps=self.renderer._fitted_label
        ) as fitted:
            with mock.patch.object(
                self.renderer, "_label", wraps=self.renderer._label
            ) as labels:
                pvp = self.renderer.render(state)

        pve_state = snapshot()
        pve_state["pve_hud_view"] = "team"
        pve = self.renderer.render(pve_state)
        self.assertEqual(
            pvp.hit_regions["action:pvp_team"][0],
            pve.hit_regions["action:pve_team"][0],
        )
        self.assertGreaterEqual(pvp.hit_regions["action:pvp_team"][0], 55)

        title_calls = [
            call for call in fitted.call_args_list if call.args[0] == "PVP 对战"
        ]
        self.assertEqual(len(title_calls), 1)
        self.assertGreaterEqual(title_calls[0].args[1], 300)
        self.assertFalse(title_calls[0].kwargs["truncate"])

        captions = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("团队超凡评分", captions)
        self.assertIn("88801", captions)
        self.assertNotIn("平均击杀战力：", captions)

    def test_pve_title_rail_has_web_database_action_right_of_dps_tab(self):
        for deaths in (False, True):
            state = snapshot(deaths=deaths)
            state["pve_hud_view"] = "recent_battle"
            result = self.renderer.render(state)

            recent = result.hit_regions["action:pve_recent_battle"]
            database = result.hit_regions["action:web_database"]
            self.assertGreater(database[0], recent[2])
            self.assertLessEqual(database[2], result.image.width)

    def test_pvp_team_view_renders_pve_style_equipment_and_scrolls_all_members(self):
        state = dict(
            combat_mode="pvp",
            pvp_hud_view="team",
            pvp_player_name="莫雪",
            pvp_team_average_rating="85600",
            pvp_outgoing=[dict(actor_id=999, name="不应保留的战果行")],
            pvp_team_rows=[
                dict(
                    actor_id=index + 1,
                    name=f"队友{index + 1}",
                    profession_id=1_200_001 + index % 7,
                    rating=str(80_000 + index),
                    is_self=index == 0,
                    equipment_profile_ready=True,
                    equipment_count=8,
                    pvp_equipment_count=6 if index == 0 else 8,
                    active_word_count=3,
                    total_word_count=10,
                )
                for index in range(13)
            ],
            pvp_team_offset=0,
        )
        with mock.patch.object(
            self.renderer,
            "_fitted_label",
            wraps=self.renderer._fitted_label,
        ) as fitted:
            with mock.patch.object(
                self.renderer,
                "_label",
                wraps=self.renderer._label,
            ) as labels:
                result = self.renderer.render(state)

        rendered = [str(call.args[0]) for call in fitted.call_args_list]
        captions = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("冒险装备 ×2", rendered)
        self.assertIn("竞技装备", rendered)
        self.assertIn("3/10", rendered)
        self.assertIn("团队构成", captions)
        self.assertIn("实时战斗", captions)
        self.assertIn("action:pvp_live", result.hit_regions)
        self.assertIn("action:pvp_team", result.hit_regions)
        self.assertIn("action:pvp_team_page_0", result.hit_regions)
        self.assertNotIn("action:pvp_team_page_1", result.hit_regions)
        self.assertIn("scroll:pvp_team", result.hit_regions)
        self.assertIn("scrollbar:pvp_team", result.hit_regions)
        self.assertNotIn("scroll:pvp_outgoing", result.hit_regions)
        self.assertEqual(
            [actor for _bounds, actor in result.actor_regions],
            list(range(1, 11)),
        )

        next_page = self.renderer.render(dict(state, pvp_team_offset=10))
        self.assertEqual(
            [actor for _bounds, actor in next_page.actor_regions],
            list(range(4, 14)),
        )

    def test_pvp_league_tabs_use_real_raid_numbers_and_members(self):
        rows = [
            dict(actor_id=actor, name=f'Player {actor}', rating='90000',
                 profession_id=1_200_001, raid_number=1 if actor <= 3 else 2,
                 subgroup=1 if actor <= 3 else 2)
            for actor in range(1, 6)
        ]
        league = {
            'raid_number': 2,
            'raids': {101: {'number': 1}, 102: {'number': 2}},
        }
        state = {
            'combat_mode': 'pvp', 'pvp_hud_view': 'team',
            'pvp_team_rows': rows, 'pvp_league_roster': league,
        }

        own = self.renderer.render(state)
        first = self.renderer.render(dict(state, pvp_league_selected_raid=1))

        self.assertEqual([actor for _box, actor in own.actor_regions], [4, 5])
        self.assertEqual([actor for _box, actor in first.actor_regions], [1, 2, 3])
        self.assertIn('action:pvp_team_page_0', own.hit_regions)
        self.assertIn('action:pvp_team_page_1', own.hit_regions)

    def test_pvp_display_groups_use_thirty_members_per_tab(self):
        rows = [
            dict(actor_id=actor, name=f'Player {actor}', rating='90000',
                 profession_id=1_200_001)
            for actor in range(1, 151)
        ]
        state = {
            'combat_mode': 'pvp', 'pvp_hud_view': 'team',
            'pvp_team_rows': rows,
        }
        for offset, expected in (
            (0, list(range(1, 11))),
            (30, list(range(31, 41))),
            (120, list(range(121, 131))),
            (140, list(range(141, 151))),
        ):
            with self.subTest(offset=offset):
                result = self.renderer.render(dict(state, pvp_team_offset=offset))
                self.assertEqual(
                    [actor for _bounds, actor in result.actor_regions], expected
                )
                self.assertEqual(
                    len([key for key in result.hit_regions
                         if key.startswith('action:pvp_team_page_')]),
                    5,
                )
                self.assertIn('scrollbar:pvp_team', result.hit_regions)

    def test_pvp_empty_team_message_is_centered_in_available_space(self):
        image = self.renderer.render({
            'combat_mode': 'pvp', 'pvp_hud_view': 'team',
            'pvp_team_rows': [],
        }).image.convert('RGBA')
        message_pixels = [
            y for y in range(80, 320) for x in range(100, 210)
            if max(image.getpixel((x, y))[:3]) > 100
            and image.getpixel((x, y))[3] > 30
        ]
        self.assertTrue(message_pixels)
        self.assertLessEqual(
            abs((min(message_pixels) + max(message_pixels)) / 2 - 202), 8
        )

    def test_pvp_page_keeps_empty_state_geometry_stable(self):
        state = {
            "combat_mode": "pvp",
            "time": "00:00",
            "pvp_player_name": "等待识别",
            "pvp_assists": "--",
            "pvp_outgoing": [],
            "pvp_incoming": [],
        }
        waiting = self.renderer.render(state, pixel_scale=1.5)
        populated = copy.deepcopy(state)
        populated["pvp_outgoing"] = [{"actor_id": 8, "name": "玩家"}]
        live = self.renderer.render(populated, pixel_scale=1.5)

        self.assertEqual(waiting.image.size, live.image.size)
        self.assertEqual(waiting.image.width, round(HUD_LOGICAL_WIDTH * 1.5))
        self.assertIn("action:pve", waiting.hit_regions)

    def test_pvp_page_keeps_four_incoming_player_rows(self):
        state = {
            "combat_mode": "pvp",
            "pvp_outgoing": [],
            "pvp_incoming": [
                {"actor_id": actor_id, "name": f"玩家{actor_id}"}
                for actor_id in range(1, 6)
            ],
        }

        result = self.renderer.render(state)

        self.assertEqual(
            [actor for _bounds, actor in result.actor_regions],
            [1, 2, 3, 4],
        )
        self.assertEqual(result.visible_rows, HUD_DEFAULT_VISIBLE_ROWS)

    def test_pvp_default_size_matches_pve_at_each_dpi(self):
        for scale in (1.0, 1.25, 1.5, 2.0):
            with self.subTest(scale=scale):
                pve = snapshot(row_count=HUD_DEFAULT_VISIBLE_ROWS)
                pve['visible_rows'] = HUD_DEFAULT_VISIBLE_ROWS
                pvp = {'combat_mode': 'pvp'}
                self.assertEqual(
                    self.renderer.render(pve, pixel_scale=scale).image.size,
                    self.renderer.render(pvp, pixel_scale=scale).image.size,
                )

    def test_pvp_scrolling_can_show_fifth_opponent_without_resizing(self):
        state = {'combat_mode': 'pvp', 'pvp_outgoing': [
            {'actor_id': actor, 'name': f'玩家{actor}'} for actor in range(1, 7)
        ], 'pvp_outgoing_offset': 2}
        result = self.renderer.render(state)
        self.assertEqual([actor for _, actor in result.actor_regions], [3, 4, 5, 6])
        self.assertIn('scroll:pvp_outgoing', result.hit_regions)
        self.assertIn('scrollbar:pvp_outgoing', result.hit_regions)
        self.assertEqual(result.image.size, self.renderer.render({'combat_mode': 'pvp'}).image.size)

    def test_pvp_resize_grip_and_two_live_scrollbars(self):
        state = {
            'combat_mode': 'pvp', 'visible_rows': 16,
            'pvp_outgoing': [
                {'actor_id': actor, 'name': f'Player {actor}'}
                for actor in range(1, 7)
            ],
            'pvp_incoming': [
                {'actor_id': actor, 'name': f'Player {actor}'}
                for actor in range(11, 18)
            ],
        }
        result = self.renderer.render(state)

        self.assertEqual(result.visible_rows, 16)
        self.assertGreater(
            result.image.height,
            self.renderer.render({'combat_mode': 'pvp'}).image.height,
        )
        self.assertIn('resize:height', result.hit_regions)
        self.assertIn('scrollbar:pvp_outgoing', result.hit_regions)
        self.assertIn('scrollbar:pvp_incoming', result.hit_regions)

    def test_pvp_rows_never_cover_footer_at_any_supported_height(self):
        for visible in range(12, 17):
            for view in ('team', 'live'):
                with self.subTest(visible=visible, view=view):
                    result = self.renderer.render(dict(
                        combat_mode='pvp', pvp_hud_view=view, visible_rows=visible,
                        pvp_team_rows=[dict(actor_id=i, name=f'玩家{i}') for i in range(1, 31)],
                        pvp_incoming=[dict(actor_id=i, name=f'敌人{i}') for i in range(1, 7)],
                    ))
                    footer_top = min(result.hit_regions[key][1] for key in ('action:pve', 'action:settings', 'action:lock', 'action:pin'))
                    for key, bounds in result.hit_regions.items():
                        if key.startswith(('scroll:pvp_', 'scrollbar:pvp_')):
                            self.assertLess(bounds[3], footer_top)
                    for bounds, _actor in result.actor_regions:
                        self.assertLess(bounds[3], footer_top)

    def test_equipment_refresh_buttons_fit_rating_and_statistic_rows(self):
        for mode in ('pve', 'pvp'):
            for view in ('team', 'live'):
                for font in (12, 20):
                    for deaths in (True, False):
                        with self.subTest(mode=mode, view=view, font=font, deaths=deaths):
                            state = snapshot(deaths=deaths)
                            row = state['rows'][0]
                            row.update(name='一个比较长的角色名', inline_rating_text='123456',
                                       rating_text='123456', rating='123456',
                                       is_self=False,
                                       equipment_refresh_token='stale-player',
                                       equipment_profile_ready=True, equipment_count=8,
                                       pvp_equipment_count=4, active_word_count=3, total_word_count=10)
                            state.update(rating_preview=view == 'team')
                            if mode == 'pvp':
                                state.update(combat_mode='pvp', pvp_hud_view=view,
                                             pvp_team_battle=True, pvp_allies=[row], pvp_team_rows=[row])
                            result = self.renderer.render(state, font_size=font)
                            bounds = result.hit_regions['action:refresh_equipment:stale-player']
                            limit = (740 - (0 if deaths or mode == 'pvp' else 138)) if view == 'team' else 595 if mode == 'pve' else 558
                            self.assertLessEqual(bounds[2], round((limit - REFERENCE_BOUNDS[0]) * REFERENCE_SCALE))
                            self.assertGreater(bounds[2] - bounds[0], 20)
        state = snapshot()
        state['rows'][0].update(
            is_self=False,
            equipment_refresh_token='stale-player',
            equipment_refresh_pending=True,
        )
        result = self.renderer.render(state)
        self.assertNotIn('action:refresh_equipment:stale-player', result.hit_regions)
        self.assertIn('equipment_refresh_pending:stale-player', result.hit_regions)
        state['rows'][0]['is_ai'] = True
        result = self.renderer.render(state)
        self.assertNotIn('equipment_refresh_pending:stale-player', result.hit_regions)

    def test_pvp_self_never_shows_refresh_button(self):
        fields = dict(equipment_refresh_token='self-token')
        result = self.renderer.render(dict(
            combat_mode='pvp', pvp_hud_view='live', pvp_team_battle=True,
            pvp_player_name='莫雪', pvp_rating='100001', pvp_profession_id=1_200_001,
            pvp_self_equipment=fields,
            pvp_allies=[dict(fields, actor_id=1, name='莫雪', rating='100001', is_self=True)],
        ))
        self.assertNotIn(
            'action:refresh_equipment:self-token', result.hit_regions
        )

    def test_blue_return_emblem_crops_export_noise_and_fills_button(self):
        source = Image.new('RGBA', (400, 400), (0, 0, 0, 1))
        source.paste((255, 255, 255, 255), (170, 170, 230, 230))
        with mock.patch('main_hud_artwork.Image.open', return_value=source):
            sprite = self.renderer._pve_action_sprite()
        ink = [(x, y) for y in range(sprite.height) for x in range(sprite.width)
               if min(sprite.getpixel((x, y))[:3]) >= 240 and sprite.getpixel((x, y))[3] >= 200]
        self.assertTrue(ink)
        self.assertGreaterEqual(max(x for x, _ in ink) - min(x for x, _ in ink), 66)
        self.assertEqual(sprite.size, self.renderer._actions['pvp'].size)

    def test_temporarily_disabled_footer_actions_have_no_hit_regions(self):
        state = snapshot()
        state["clear_disabled"] = True
        state["settings_disabled"] = True

        result = self.renderer.render(state, pixel_scale=1.0)

        # Clear is no longer a visible footer action, disabled or otherwise.
        self.assertNotIn("action:clear", result.hit_regions)
        self.assertNotIn("action:settings", result.hit_regions)
        self.assertIn("action:pin", result.hit_regions)
        self.assertIn("action:lock", result.hit_regions)

    def test_startup_only_keeps_settings_interactive_in_pve_and_pvp(self):
        for mode in ('pve', 'pvp'):
            with self.subTest(mode=mode):
                state = snapshot(row_count=7)
                state.update(
                    combat_mode=mode,
                    pvp_outgoing=[{'actor_id': actor, 'name': f'玩家{actor}'}
                                  for actor in range(1, 7)],
                    pvp_incoming=[{'actor_id': 10, 'name': '对手'}],
                )
                ready = self.renderer.render(state)
                self.assertTrue(ready.actor_regions)
                settings_box = ready.hit_regions['action:settings']
                state['interaction_blocked'] = True

                starting = self.renderer.render(state)

                self.assertEqual(set(starting.hit_regions), {'action:settings'})
                self.assertEqual(starting.actor_regions, ())
                self.assertEqual(starting.hit_regions['action:settings'], settings_box)
                # Ignore the resampling fringe shared with adjacent buttons,
                # whose disabled appearance intentionally changes at startup.
                left, top, right, bottom = settings_box
                settings_core = (left + 3, top + 3, right - 3, bottom - 3)
                self.assertEqual(
                    starting.image.crop(settings_core).tobytes(),
                    ready.image.crop(settings_core).tobytes(),
                )

                state['settings_disabled'] = True
                disabled = self.renderer.render(state)
                self.assertEqual(disabled.hit_regions, {})
                self.assertEqual(disabled.actor_regions, ())

    def test_recent_battle_heading_is_centered_between_actor_rows(self):
        state = snapshot(row_count=2)
        state["rows"].insert(
            1,
            {"row_kind": "section", "section_text": "以下为最近战斗记录"},
        )
        state["visible_rows"] = 3
        state["dps_summary_caption"] = "本人秒伤"
        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            result = self.renderer.render(state)

        texts = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("以下为最近战斗记录", texts)
        self.assertNotIn("action:recent_battle", result.hit_regions)
        self.assertIn("本人秒伤", texts)
        self.assertEqual([actor for _bounds, actor in result.actor_regions], [1, 2])

    def test_recent_battle_is_static_section_without_rating_tab(self):
        state = snapshot(row_count=2)
        state["rows"].insert(
            1,
            {
                "row_kind": "section",
                "section_text": "最近战斗记录",
                "expanded": True,
            },
        )
        state["visible_rows"] = 3

        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            result = self.renderer.render(state)

        texts = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("最近战斗记录", texts)
        self.assertNotIn("队伍非凡评分", texts)
        self.assertNotIn("action:recent_battle", result.hit_regions)
        self.assertNotIn("action:team_rating_tab", result.hit_regions)

    def test_live_team_dps_is_drawn_beside_recent_battle_heading(self):
        state = snapshot(row_count=2)
        state["rows"].insert(
            1,
            {
                "row_kind": "section",
                "section_text": "最近战斗记录",
                "live_team_dps": "286,420/s",
            },
        )
        state["visible_rows"] = 3

        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            self.renderer.render(state)

        texts = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("最近战斗记录", texts)
        self.assertIn("实时团队秒伤", texts)
        self.assertIn("286,420/s", texts)
        self.assertNotIn("按血量", texts)

    def test_dummy_live_dps_draws_player_caption(self):
        state = snapshot()
        state["rows"].append({
            "row_kind": "section",
            "section_text": "实时战斗/战斗记录",
            "live_team_dps": "18,135/s",
            "live_dps_caption": "实时玩家秒伤",
        })
        state["visible_rows"] = 2

        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            self.renderer.render(state)

        texts = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("实时玩家秒伤", texts)
        self.assertIn("18,135/s", texts)
        self.assertNotIn("实时团队秒伤", texts)

    def test_team_application_uses_dedicated_green_overlay_row(self):
        state = snapshot(row_count=2)
        original_rows = copy.deepcopy(state["rows"])
        state["application_rows"] = [
            {
                "row_kind": "team_application",
                "application_text": "入队申请  测试玩家  非凡评分 88888",
            }
        ]
        state["visible_rows"] = 2

        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            result = self.renderer.render(state)

        texts = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("入队申请  测试玩家  非凡评分 88888", texts)
        self.assertEqual(state["rows"], original_rows)
        self.assertEqual([actor for _bounds, actor in result.actor_regions], [1, 2])
        # Row 0 is the local player. The notice begins over the DPS area at
        # row 1 (y=395) without consuming or replacing either source row.
        scale = REFERENCE_SCALE
        self_pixel = result.image.getpixel(
            (round((220 - 146) * scale), round((319 - 126) * scale))
        )
        pixel = result.image.getpixel(
            (round((220 - 146) * scale), round((395 - 126) * scale))
        )
        self.assertFalse(
            self_pixel[1] > self_pixel[0] * 2
            and self_pixel[1] > self_pixel[2]
        )
        self.assertGreater(pixel[1], pixel[0] * 2)
        self.assertGreater(pixel[1], pixel[2])

    def test_scrollable_recent_rows_expose_wheel_and_drag_regions(self):
        state = snapshot(row_count=12)
        state["visible_rows"] = 4

        result = self.renderer.render(state)

        self.assertIn("scroll:rows", result.hit_regions)
        self.assertIn("scrollbar:rows", result.hit_regions)
        list_region = result.hit_regions["scroll:rows"]
        thumb_region = result.hit_regions["scrollbar:rows"]
        self.assertLess(list_region[1], list_region[3])
        self.assertLess(thumb_region[1], thumb_region[3])
        self.assertGreaterEqual(thumb_region[0], list_region[0])
        self.assertLessEqual(thumb_region[2], list_region[2])

    def test_rating_tab_rows_do_not_render_current_battle_deaths(self):
        state = snapshot(row_count=1)
        state["rows"][0].update(
            metric="rating",
            rating_text="88893",
            deaths=7,
            hide_deaths=True,
        )
        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            self.renderer.render(state)
        texts = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("（88893）", texts)
        self.assertNotIn("非凡评分：", texts)
        self.assertNotIn("7", texts)

    def test_empty_recent_battle_message_uses_waiting_state_presentation(self):
        message = "该数据将在战斗胜利或下一次挑战boss时体现"
        state = snapshot(row_count=1)
        state["rows"].extend(
            [
                {"row_kind": "section", "section_text": "以下为最近战斗记录"},
                {"row_kind": "message", "message_text": message},
            ]
        )
        state["visible_rows"] = 3

        with mock.patch.object(
            self.renderer, "_fitted_label", wraps=self.renderer._fitted_label
        ) as labels:
            result = self.renderer.render(state)

        message_call = next(
            call for call in labels.call_args_list if call.args[0] == message
        )
        self.assertEqual(message_call.args[2], 31)
        self.assertEqual(message_call.kwargs["color"], (183, 200, 216))
        self.assertTrue(message_call.kwargs["contour"])
        self.assertEqual([actor for _bounds, actor in result.actor_regions], [1])

    def test_recent_boss_card_precedes_dps_rows_with_portrait_and_health(self):
        state = snapshot(row_count=2)
        state["rows"].insert(1, {
            "row_kind": "section",
            "section_text": "最近战斗记录",
            "expanded": True,
        })
        state["rows"].insert(2, {
            "row_kind": "recent_boss",
            "boss_name": "异化猎犬",
            "boss_template_id": 7109821,
            "boss_hp": "862.5万 / 3450.2万",
            "boss_percent": "25.0%",
            "boss_ratio": 0.25,
        })
        state["visible_rows"] = 4

        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels, mock.patch.object(
            self.renderer, "_boss_portrait", wraps=self.renderer._boss_portrait
        ) as portraits:
            result = self.renderer.render(state)

        texts = [str(call.args[0]) for call in labels.call_args_list]
        self.assertIn("异化猎犬", texts)
        self.assertIn("862.5万 / 3450.2万  25.0%", texts)
        portraits.assert_any_call(7109821)
        self.assertEqual(
            [actor for _bounds, actor in result.actor_regions], [1, 2]
        )

    def test_recent_boss_bar_uses_ratio_and_unknown_ratio_is_not_full_red(self):
        def rendered_bar(ratio, hp, percent):
            state = snapshot(row_count=2)
            state["rows"].insert(
                1, {"row_kind": "section", "section_text": "最近战斗记录"}
            )
            state["rows"].insert(
                2,
                {
                    "row_kind": "recent_boss",
                    "boss_name": "异化猎犬",
                    "boss_template_id": 7109821,
                    "boss_hp": hp,
                    "boss_percent": percent,
                    "boss_ratio": ratio,
                },
            )
            state["visible_rows"] = 4
            return self.renderer.render(state, pixel_scale=2).image

        scale = REFERENCE_SCALE * 2

        def source_pixel(image, x, y):
            return image.getpixel(
                (round((x - 146) * scale), round((y - 126) * scale))
            )

        quarter = rendered_bar(0.25, "862.5万 / 3450.2万", "25.0%")
        remaining = source_pixel(quarter, 450, 497)
        depleted = source_pixel(quarter, 600, 497)
        self.assertGreater(remaining[0] - depleted[0], 120)

        full = rendered_bar(1.0, "3450.2万 / 3450.2万", "100%")
        full_left = source_pixel(full, 290, 497)
        full_right = source_pixel(full, 1050, 497)
        self.assertGreater(full_left[0] - full_left[1], 120)
        self.assertGreater(full_right[0] - full_right[1], 120)

        unknown = rendered_bar(None, "862.5万", "")
        unknown_left = source_pixel(unknown, 290, 497)
        unknown_right = source_pixel(unknown, 1050, 497)
        self.assertLess(unknown_left[0], 40)
        self.assertLess(unknown_right[0], 40)

    def test_live_boss_with_unknown_ratio_keeps_the_track_dark(self):
        state = snapshot()
        state.update(
            boss_available=True,
            boss_hp="862.5万",
            boss_percent="",
            boss_ratio=None,
        )
        image = self.renderer.render(state, pixel_scale=2).image
        scale = REFERENCE_SCALE * 2
        middle = image.getpixel(
            (round((700 - 146) * scale), round((253 - 126) * scale))
        )
        self.assertLess(middle[0], 40)

    def test_recent_boss_portrait_can_use_stage_icon_without_fake_template(self):
        portrait = self.renderer._boss_portrait(0, "astrologer.png")

        self.assertEqual(portrait.size, (128, 128))
        self.assertNotEqual(portrait.tobytes(), self.renderer._boss.tobytes())

    def test_death_column_removal_reclaims_the_full_column_width(self):
        with_deaths = self.renderer.render(snapshot(deaths=True), pixel_scale=1.0)
        without_deaths = self.renderer.render(snapshot(deaths=False), pixel_scale=1.0)
        self.assertEqual(with_deaths.image.width, HUD_LOGICAL_WIDTH)
        self.assertEqual(
            without_deaths.image.width, HUD_LOGICAL_WIDTH_WITHOUT_DEATHS
        )
        self.assertLess(without_deaths.image.width, with_deaths.image.width)

    def test_visible_rows_are_bounded_without_losing_scrollable_actor_data(self):
        state = snapshot(row_count=50)
        state["visible_rows"] = HUD_MAX_VISIBLE_ROWS
        result = self.renderer.render(state, pixel_scale=1.0)
        self.assertEqual(result.visible_rows, HUD_MAX_VISIBLE_ROWS)
        self.assertEqual(len(result.actor_regions), HUD_MAX_VISIBLE_ROWS)
        state["start_index"] = 50 - HUD_MAX_VISIBLE_ROWS
        last = self.renderer.render(state, pixel_scale=1.0)
        self.assertEqual(
            [actor_id for _bounds, actor_id in last.actor_regions],
            list(range(51 - HUD_MAX_VISIBLE_ROWS, 51)),
        )

    def test_whole_hud_rectangle_remains_a_nearly_invisible_drag_surface(self):
        result = self.renderer.render(snapshot(), pixel_scale=1.0)
        self.assertGreater(result.image.getchannel("A").getpixel((0, 0)), 0)

    def test_only_header_is_exposed_as_window_drag_region(self):
        result = self.renderer.render(snapshot(row_count=4), pixel_scale=1.0)
        left, top, right, bottom = result.hit_regions["drag:window"]

        self.assertEqual(top, 0)
        self.assertLessEqual(bottom, round(65 * REFERENCE_SCALE) + 1)
        self.assertEqual(left, 0)
        self.assertEqual(right, result.image.width)
        first_actor = result.actor_regions[0][0]
        self.assertGreater(first_actor[1], bottom)

    def test_admin_indicator_is_green_or_red_without_status_copy(self):
        elevated_state = snapshot()
        elevated_state.update(
            admin_elevated=True,
            connection_notice="运行中",
        )
        standard_state = dict(elevated_state, admin_elevated=False)
        with mock.patch.object(
            self.renderer, "_label", wraps=self.renderer._label
        ) as labels:
            elevated = self.renderer.render(elevated_state, pixel_scale=1.0)
        standard = self.renderer.render(standard_state, pixel_scale=1.0)

        self.assertNotIn(
            "运行中", [str(call.args[0]) for call in labels.call_args_list]
        )
        # The lamp center now shares the title-switch center line.
        elevated_pixel = elevated.image.getpixel((272, 10))
        standard_pixel = standard.image.getpixel((272, 10))
        self.assertGreater(elevated_pixel[1], elevated_pixel[0])
        self.assertGreater(standard_pixel[0], standard_pixel[1])

    def test_six_digit_team_rating_fits_footer_at_large_font(self):
        for font_size in (14, 16, 18, 20):
            with self.subTest(font_size=font_size):
                factor = max(0.8, min(1.4, font_size / 14))
                for width in (333, 398):
                    caption, value = self.renderer._footer_summary_pair(
                        "团队超凡评分", "107,479", width, factor,
                    )
                    self.assertLessEqual(caption.width + value.width + 4, width)
                    self.assertGreaterEqual(value.width, 140)

    def test_static_actions_use_the_supplied_art_not_substitute_icons(self):
        source = Image.open(ROOT / "assets/main_hud_reference.png").convert("RGBA")
        for key, bounds in ACTION_BOXES.items():
            if key == 'pvp':
                continue  # Its source swords now share the blue action's rim.
            with self.subTest(key=key):
                expected = source.crop(bounds)
                expected.putalpha(expected.getchannel("A").point(lambda n: 0 if n < 8 else n))
                self.assertEqual(self.renderer._actions[key].tobytes(), expected.tobytes())

    def test_mode_switches_share_plate_geometry_and_reuse_source_swords(self):
        red = self.renderer._mode_action_plate((246, 80, 110))
        blue = self.renderer._mode_action_plate((68, 183, 255))
        self.assertEqual(red.size, blue.size)
        self.assertEqual(red.getchannel('A').tobytes(), blue.getchannel('A').tobytes())
        with mock.patch.object(self.renderer, '_crop', wraps=self.renderer._crop) as crop:
            sprite = self.renderer._pvp_action_sprite()
        crop.assert_called_once_with(ACTION_BOXES['pvp'])
        self.assertEqual(sprite.size, self.renderer._pve_action.size)
        self.assertEqual(red.getpixel((40, 50)), blue.getpixel((40, 50)))

    def test_logo_is_loaded_from_current_project_logo(self):
        self.assertEqual(self.renderer.logo_path.resolve(), (ROOT / "assets/app_logo.png").resolve())

    def test_reference_font_contains_every_digit_and_renders_new_values(self):
        self.assertTrue(set('0123456789,/s') <= self.renderer.source_font_characters)
        first = self.renderer._ink('9,876,543/s', 29, numeric=True)
        second = self.renderer._ink('2,345,678/s', 29, numeric=True)
        self.assertNotEqual(first.tobytes(), second.tobytes())

    def test_live_fields_all_change_without_resizing_or_moving_columns(self):
        state = snapshot(row_count=3)
        baseline = self.renderer.render(state)
        changes = (
            ('time', '05:43'),
            ('boss_hp', '1.37亿 / 9.45亿'), ('boss_percent', '14.5%'),
            ('boss_ratio', 0.145), ('team_dps', '567,890'),
        )
        for key, value in changes:
            changed = copy.deepcopy(state)
            changed[key] = value
            result = self.renderer.render(changed)
            with self.subTest(key=key):
                self.assertEqual(result.image.size, baseline.image.size)
                self.assertEqual(result.hit_regions, baseline.hit_regions)
                self.assertNotEqual(result.image.tobytes(), baseline.image.tobytes())
        for key, value in (('name', '动态新角色'), ('stat_text', '72,391/s'), ('total_text', '(39.24万)'), ('deaths', 7)):
            changed = copy.deepcopy(state)
            changed['rows'][0][key] = value
            result = self.renderer.render(changed)
            with self.subTest(key=key):
                self.assertEqual(result.actor_regions, baseline.actor_regions)
                self.assertEqual(result.image.size, baseline.image.size)
                self.assertNotEqual(result.image.tobytes(), baseline.image.tobytes())

    def test_renderer_does_not_mutate_or_sort_the_existing_combat_data(self):
        state = snapshot(row_count=7)
        before = copy.deepcopy(state)
        self.renderer.render(state)
        self.assertEqual(state, before)

    def test_rating_preview_replaces_both_rate_and_total(self):
        state = snapshot()
        state['rating_preview'] = True
        state['rows'][0]['rating_text'] = '75900'
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as render:
            self.renderer.render(state)
        texts = [str(call.args[0]) for call in render.call_args_list]
        self.assertIn('（75900）', texts)
        self.assertNotIn('非凡评分：', texts)
        self.assertNotIn('1,048,208/s', texts)
        self.assertNotIn('(1782万)', texts)

    def test_equipment_type_badge_uses_pvp_count_or_adventure_label(self):
        self.assertEqual(equipment_type_badge(2), ("竞技装备 ×2", True))
        self.assertEqual(equipment_type_badge(0), ("冒险装备", False))
        self.assertEqual(equipment_type_badge(None), ("冒险装备", False))

    def test_pvp_equipment_badge_warns_only_about_adventure_pieces(self):
        self.assertEqual(
            pvp_equipment_type_badge(8, 6),
            ("冒险装备 ×2", True),
        )
        self.assertEqual(
            pvp_equipment_type_badge(6, 6),
            ("竞技装备", False),
        )

    def test_rating_preview_renders_red_pvp_or_blue_adventure_badge(self):
        state = snapshot(deaths=False)
        state["rating_preview"] = True
        state["rows"][0].update(
            rating_text="75900",
            equipment_profile_ready=True,
            pvp_equipment_count=2,
            active_word_count=6,
            total_word_count=17,
        )
        with mock.patch.object(
            self.renderer, "_fitted_label", wraps=self.renderer._fitted_label
        ) as fitted:
            self.renderer.render(state)
        values = {str(call.args[0]): call for call in fitted.call_args_list}
        self.assertIn("竞技装备 ×2", values)
        self.assertEqual(values["竞技装备 ×2"].kwargs["color"], (255, 145, 161))

        state["rows"][0]["pvp_equipment_count"] = 0
        with mock.patch.object(
            self.renderer, "_fitted_label", wraps=self.renderer._fitted_label
        ) as fitted:
            self.renderer.render(state)
        values = {str(call.args[0]): call for call in fitted.call_args_list}
        self.assertIn("冒险装备", values)
        self.assertNotIn("竞技装备 0", values)
        self.assertEqual(values["冒险装备"].kwargs["color"], (105, 202, 255))

    def test_rating_row_can_share_the_page_with_a_live_dps_row(self):
        state = snapshot(row_count=2)
        state['rating_preview'] = False
        state['rows'][1].update(
            metric='rating', rating_text='88893', total_text='(999万)'
        )
        with mock.patch.object(
            self.renderer, '_label', wraps=self.renderer._label
        ) as render:
            self.renderer.render(state)
        texts = [str(call.args[0]) for call in render.call_args_list]
        self.assertIn('1,048,208/s', texts)
        self.assertIn('（88893）', texts)
        self.assertNotIn('非凡评分：', texts)
        self.assertNotIn('(999万)', texts)

    def test_missing_values_stay_unknown_and_do_not_use_sample_data(self):
        state = {'rows': [dict(actor_id=1, name='新角色', profession_id=1200002)], 'rating_preview': True}
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as render:
            self.renderer.render(state)
        texts = [str(call.args[0]) for call in render.call_args_list]
        self.assertIn('（--）', texts)
        self.assertNotIn('非凡评分：', texts)
        self.assertIn('', texts)
        self.assertNotIn('泰南拉斯', texts)
        self.assertNotIn('8,970,096', texts)
        bounds = self.renderer._ink('--', 29).getbbox()
        self.assertLess(bounds[3] - bounds[1], 10)

    def test_numeric_totals_never_get_an_ellipsis(self):
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as render:
            self.renderer._fitted_label('(9876.54亿)', 150, 32, regular=True, truncate=False)
        self.assertTrue(render.call_args_list)
        self.assertTrue(all('…' not in str(call.args[0]) for call in render.call_args_list))

    def test_all_seven_classes_keep_distinct_game_shapes_and_hues(self):
        signatures = set()
        for profession_id, color in COLORS.items():
            icon = self.renderer._profession(profession_id)
            signatures.add(icon.tobytes())
            # Symbols are coloured, without a round badge. Inspect a bright
            # opaque glyph pixel rather than the now-transparent background.
            pixels = [icon.getpixel((x, y)) for y in range(94) for x in range(94)
                      if icon.getpixel((x, y))[3] > 225 and max(icon.getpixel((x, y))[:3]) > 140]
            self.assertTrue(pixels)
            r, g, b, alpha = max(pixels, key=lambda p: max(p[:3]) - min(p[:3]))
            expected = colorsys.rgb_to_hsv(*(int(color[i:i+2],16) / 255 for i in (1, 3, 5)))
            actual = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            self.assertLess(min(abs(actual[0] - expected[0]), 1 - abs(actual[0] - expected[0])), 0.06)
            self.assertGreater(actual[1], 0.45)
            self.assertGreater(alpha, 225)
        self.assertEqual(len(signatures), 7)

    def test_profession_symbols_have_no_circular_background(self):
        for profession_id in COLORS:
            icon = self.renderer._profession(profession_id)
            alpha = icon.getchannel('A')
            for point in ((12, 12), (81, 12), (12, 81), (81, 81)):
                self.assertLess(alpha.getpixel(point), 50)
            self.assertLess(sum(alpha.histogram()[226:]), 94 * 94 * 0.45)

    def test_self_highlight_does_not_change_label_geometry(self):
        for text, numeric in (('六位角色名字', False), ('1,048,208/s', True), ('（123456）', False)):
            ordinary = self.renderer._label(text, 29, numeric=numeric, contour=True, padding=13)
            highlighted = self.renderer._label(text, 29, numeric=numeric, contour=False, padding=13)
            self.assertEqual(ordinary.size, highlighted.size)
            # Exact white foreground positions stay unchanged; only the
            # background/outline layers may differ with self highlighting.
            def white_mask(image):
                r,g,b,a = image.split()
                return ImageChops.darker(ImageChops.darker(r,g),b).point(lambda n: 255 if n > 220 else 0)
            self.assertEqual(white_mask(ordinary).tobytes(), white_mask(highlighted).tobytes())

    def test_total_columns_do_not_create_background_pills(self):
        state = snapshot(row_count=1, deaths=False)
        state['show_time'] = False
        state['show_boss'] = False
        state['rows'][0]['is_self'] = False
        with mock.patch.object(self.renderer, '_pill', wraps=self.renderer._pill) as pills:
            self.renderer.render(state)
        pills.assert_not_called()

    def test_six_digit_rating_and_ai_are_parenthesized_after_the_name(self):
        state = snapshot(row_count=3)
        state['rating_preview'] = True
        state['rows'][0]['rating_text'] = '75900'
        state['rows'][1]['rating_text'] = '123456'
        state['rows'][2].update(is_ai=True, rating_text='888888')
        with mock.patch.object(self.renderer, '_fitted_label', wraps=self.renderer._fitted_label) as fitted:
            self.renderer.render(state)
        values = {str(call.args[0]): call for call in fitted.call_args_list}
        self.assertNotIn('888888', values)
        for value in ('（75900）', '（123456）', '（人机）'):
            self.assertIn(value, values)
        self.assertEqual(values['（75900）'].args[1], 180)
        self.assertEqual(values['（75900）'].kwargs['padding'], 6)
        for value in ('（123456）', '（人机）'):
            self.assertEqual(values[value].args[1], 170)
            self.assertEqual(values[value].kwargs['padding'], 13)
        self.assertNotIn('非凡评分：', values)

    def test_inline_rating_keeps_current_gold_and_ai_blue_colors(self):
        state = snapshot(row_count=2)
        state['rating_preview'] = True
        state['rows'][0]['rating_text'] = '75900'
        state['rows'][1].update(is_ai=True, rating_text='888888')
        with mock.patch.object(
            self.renderer, '_fitted_label', wraps=self.renderer._fitted_label
        ) as fitted:
            self.renderer.render(state)
        values = {str(call.args[0]): call for call in fitted.call_args_list}
        self.assertEqual(values['（75900）'].kwargs['color'], (255, 226, 92))
        self.assertEqual(values['（人机）'].kwargs['color'], (116, 210, 235))

    def test_six_participants_are_not_padded_to_the_maximum(self):
        result = self.renderer.render(snapshot(row_count=6))
        self.assertEqual(result.visible_rows, 6)
        self.assertEqual(len(result.actor_regions), 6)

    def test_boss_emblem_overlaps_the_increased_bar_and_logo_has_gold_rim(self):
        self.assertEqual(self.renderer._boss.size, (128, 128))
        r,g,b,a = self.renderer._avatar.getpixel((51, 3))
        self.assertGreater(r, 170)
        self.assertGreater(g, 90)
        self.assertGreater(r, b * 1.3)
        self.assertGreater(a, 200)

    def test_dpi_scaling_and_all_hit_regions_remain_inside_the_hud(self):
        for dpi in (1, 1.25, 1.5, 1.75, 2):
            for deaths in (False, True):
                for row_count in (1, 6, 10, 12):
                    result = self.renderer.render(snapshot(deaths=deaths, row_count=row_count), pixel_scale=dpi)
                    for rect in result.hit_regions.values():
                        self.assertGreaterEqual(rect[0], 0)
                        self.assertGreaterEqual(rect[1], 0)
                        self.assertLessEqual(rect[2], result.image.width)
                        self.assertLessEqual(rect[3], result.image.height)

    def test_prediction_label_and_tip_follow_the_full_hp_track(self):
        for dpi in (1, 1.5, 2):
            for deaths in (False, True):
                with self.subTest(dpi=dpi, deaths=deaths):
                    state = snapshot(deaths=deaths)
                    state['prediction'] = dict(state='normal', message='正常 · +0:34')
                    tips, labels = [], []
                    for ratio in (0, 0.25, 0.5, 0.75, 1):
                        state['prediction']['marker'] = ratio
                        result = self.renderer.render(state, pixel_scale=dpi)
                        tip = result.hit_regions['prediction_marker']
                        tips.append((tip[0] + tip[2]) / 2)
                        labels.append(result.hit_regions['prediction'][0])
                        for bounds in result.hit_regions.values():
                            self.assertGreaterEqual(bounds[0], 0)
                            self.assertLessEqual(bounds[2], result.image.width)
                            self.assertLessEqual(bounds[3], result.image.height)
                    self.assertEqual(tips, sorted(set(tips)))
                    self.assertGreater(tips[-1] - tips[0], result.image.width * 0.7)
                    for index in (1, 2, 3):
                        self.assertAlmostEqual(tips[index], tips[0] + (tips[-1] - tips[0]) * index / 4, delta=1)
                    self.assertLess(labels[1], labels[2])
                    self.assertLess(labels[2], labels[3])

    def test_prediction_marker_can_reach_zero_without_falling_back_to_eighty_two_percent(self):
        state = snapshot()
        state['prediction'] = dict(state='danger', message='危险 · -0:34', marker=0)
        result = self.renderer.render(state)
        tip = result.hit_regions['prediction_marker']
        self.assertLess((tip[0] + tip[2]) / 2, result.image.width * 0.15)

    def test_scrolling_exposes_remaining_participants_once(self):
        state = snapshot(row_count=14)
        first = self.renderer.render(state)
        state['start_index'] = 2
        last = self.renderer.render(state)
        self.assertEqual([actor for _, actor in first.actor_regions], list(range(1, 13)))
        self.assertEqual([actor for _, actor in last.actor_regions], list(range(3, 15)))

    def test_pvp_visibility_and_lock_state_are_independent(self):
        state = snapshot()
        state['show_pvp'] = False
        unlocked = self.renderer.render(state)
        self.assertNotIn('action:pvp', unlocked.hit_regions)
        state['locked'] = True
        locked = self.renderer.render(state)
        region = locked.hit_regions['action:lock']
        self.assertNotEqual(locked.image.crop(region).tobytes(), unlocked.image.crop(region).tobytes())

    def test_pvp_button_remains_clickable_outside_a_pvp_map(self):
        state = snapshot()
        state['pvp_available'] = False
        rendered = self.renderer.render(state)
        self.assertIn('action:pvp', rendered.hit_regions)

    def test_pin_replaces_close_and_displays_the_persisted_topmost_state(self):
        state = snapshot()
        state['topmost'] = False
        unpinned = self.renderer.render(state)
        state['topmost'] = True
        pinned = self.renderer.render(state)
        self.assertNotIn('action:close', pinned.hit_regions)
        self.assertEqual(
            {key for key in pinned.hit_regions if key.startswith('action:')},
            {'action:pvp', 'action:settings', 'action:lock', 'action:pin'},
        )
        box = pinned.hit_regions['action:pin']
        self.assertNotEqual(pinned.image.crop(box).tobytes(), unpinned.image.crop(box).tobytes())

    def test_clock_is_text_only_and_boss_fonts_match_team_summary(self):
        from main_hud_artwork import SUMMARY_FONT_HEIGHT
        state = snapshot()
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as labels, mock.patch.object(self.renderer, '_pill', wraps=self.renderer._pill) as pills:
            self.renderer.render(state)
        clock = [call for call in labels.call_args_list if call.args[0] == '00:19']
        self.assertTrue(clock)
        self.assertFalse(any(len(call.args) > 1 and call.args[1] == 58 for call in pills.call_args_list))
        for text in ('5.2亿 / 5.2亿', '100%', '全队秒伤', '8,970,096'):
            expected_height = SUMMARY_FONT_HEIGHT - 4 if text in ('5.2亿 / 5.2亿', '100%') else SUMMARY_FONT_HEIGHT
            self.assertTrue(any(call.args[0] == text and call.args[1] == expected_height for call in labels.call_args_list), text)

    def test_boss_level_is_visible_without_changing_window_width_or_health(self):
        state = snapshot()
        before = self.renderer.render(state)
        state['boss_level'] = 72
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as labels:
            after = self.renderer.render(state)
        self.assertEqual(after.image.size, before.image.size)
        texts = [call.args[0] for call in labels.call_args_list]
        self.assertIn('Lv.72', texts)
        self.assertIn('5.2亿 / 5.2亿', texts)
        self.assertIn('100%', texts)

    def test_twelve_person_raid_includes_bottom_healers_without_scrolling(self):
        result = self.renderer.render(snapshot(row_count=12))
        self.assertEqual(result.visible_rows, 12)
        self.assertEqual(len(result.actor_regions), 12)
        self.assertEqual(result.actor_regions[-1][1], 12)

    def test_simultaneous_bosses_expand_height_and_keep_both_hp_values(self):
        state = snapshot(row_count=12)
        first = self.renderer.render(state)
        state['bosses'] = [dict(boss_name='首领甲', boss_hp='1亿 / 2亿', boss_ratio=0.5, boss_percent='50%'),
                           dict(boss_name='首领乙', boss_hp='3亿 / 4亿', boss_ratio=0.75, boss_percent='75%')]
        with mock.patch.object(self.renderer, '_label', wraps=self.renderer._label) as labels:
            second = self.renderer.render(state)
        texts = [call.args[0] for call in labels.call_args_list]
        self.assertIn('首领甲', texts)
        self.assertIn('首领乙', texts)
        self.assertIn('1亿 / 2亿', texts)
        self.assertIn('3亿 / 4亿', texts)
        self.assertEqual(second.image.width, first.image.width)
        self.assertAlmostEqual(second.image.height - first.image.height, 110 * REFERENCE_SCALE, delta=1)
        self.assertEqual(len(second.actor_regions), 12)
        self.assertGreater(second.actor_regions[0][0][1], first.actor_regions[0][0][1])

    def test_numeric_cells_and_rate_font_are_consistent_for_same_digit_count(self):
        first = self.renderer._ink('23,597/s', 29, numeric=True)
        second = self.renderer._ink('23,101/s', 29, numeric=True)
        self.assertEqual(first.size, second.size)

    def test_total_is_attached_to_rate_and_text_uses_visible_vertical_center(self):
        from main_hud_artwork import attached_total_left, text_top_for_center
        self.assertEqual(attached_total_left(778) + 7 - (778 - 13), 2)
        for value in ('全队秒伤', '89,273', '暂无目标', '53.2%'):
            tile = self.renderer._label(value, 40)
            top = text_top_for_center(tile, 100)
            bounds = tile.info['hud_ink_bounds']
            self.assertAlmostEqual(top + (bounds[1] + bounds[3]) / 2, 100)

    def test_unavailable_values_are_blank_while_actual_zero_remains_visible(self):
        for value in (None, '', '--', '-- / --'):
            self.assertIsNone(self.renderer._label(value, 30).getbbox())
        self.assertIsNotNone(self.renderer._label('0', 30).getbbox())
        self.assertIsNotNone(self.renderer._label('人机', 30).getbbox())

    def test_short_enrage_message_does_not_leave_a_large_empty_pill(self):
        state = snapshot()
        state['prediction'] = dict(state='normal', message='正常 · +0:34')
        short = self.renderer.render(state).hit_regions['prediction']
        state['prediction']['message'] = '预计延后12s (剩余3.2%)'
        long = self.renderer.render(state).hit_regions['prediction']
        self.assertLess(short[2] - short[0], long[2] - long[0])

    def test_lost_boss_hp_is_much_darker_than_remaining_hp(self):
        state = snapshot()
        state.update(boss_name='', boss_hp='', boss_percent='', boss_ratio=0.5)
        image = self.renderer.render(state, pixel_scale=2).image
        scale = REFERENCE_SCALE * 2
        remaining = image.getpixel((round((500 - 146) * scale), round((253 - 126) * scale)))
        lost = image.getpixel((round((930 - 146) * scale), round((253 - 126) * scale)))
        self.assertGreater(remaining[0] - lost[0], 60)

    def test_boss_bar_is_slightly_taller_without_widening_the_window(self):
        from main_hud_artwork import BOSS_BAR_EXTRA_HEIGHT
        result = self.renderer.render(snapshot(row_count=10))
        self.assertEqual(result.image.width, HUD_LOGICAL_WIDTH)
        self.assertEqual(result.image.height, round((1004 + BOSS_BAR_EXTRA_HEIGHT) * REFERENCE_SCALE))

    def test_boss_placeholders_are_centered_after_the_name(self):
        from main_hud_artwork import boss_placeholder_anchors
        for bar_right in (944, 1082):
            left, percent_left = boss_placeholder_anchors(452, bar_right, 130, 54)
            self.assertAlmostEqual((left + percent_left + 54) / 2, (452 + bar_right) / 2)
            self.assertGreater(left, 452)
            self.assertLess(percent_left + 54, bar_right)

    def test_audience_damage_is_shown_when_existing_setting_selects_dps(self):
        from test_combat_model import DpsWindow, ActorStats, normalize_profession_display_metrics
        actor = ActorStats(actor_id=71)
        actor.damage = 80_000  # Below the new per-player DPS override threshold.
        window = object.__new__(DpsWindow)
        window.model = SimpleNamespace(
            current_stats=lambda: [actor], current_taken_rows=lambda: [],
            _current_member_ids=lambda: {71}, non_player_actor_ids=set(),
            friend_order=[71], self_id=71, member_death_counts={},
            actor_profession_id=lambda _actor: 1200002, duration=lambda _now=None: 20,
        )
        window._shown_actor_name = lambda _actor: '本人'
        window.latest_healing_summary = {'healers': []}
        window.profession_display_metrics = normalize_profession_display_metrics({})
        healer = window._main_combat_display_rows()[0]
        self.assertEqual(healer['metric'], 'hps')
        self.assertIsNone(healer['stat_value'])
        actor.damage = 312_760
        window.profession_display_metrics['1200002'] = 'dps'
        damage = window._main_combat_display_rows()[0]
        self.assertTrue(damage['is_self'])
        self.assertEqual(damage['stat_value'], 15_638)
        self.assertEqual(damage['total_value'], 312_760)
        self.assertEqual(actor.damage, 312_760)

    def test_pinned_audience_row_keeps_hps_when_healing_is_zero(self):
        from test_combat_model import DpsWindow, ActorStats, normalize_profession_display_metrics
        actor = ActorStats(actor_id=71)
        actor.damage = 80_000
        window = object.__new__(DpsWindow)
        window.model = SimpleNamespace(
            current_stats=lambda: [actor], self_id=71,
            actor_profession_id=lambda _actor: 1200002,
            duration=lambda _now=None: 20, member_death_counts={},
            entity_extraordinary_ratings={}, entity_ai_states={},
        )
        window._shown_actor_name = lambda _actor: '本人'
        window.latest_healing_summary = {
            'healers': [dict(actor_id=71, effective_healing=0)]
        }
        window.profession_display_metrics = normalize_profession_display_metrics({})

        row = window._main_live_self_dps_row(now=20)

        self.assertEqual(row['metric'], 'hps')
        self.assertEqual(row['stat_value'], 0)
        self.assertEqual(row['total_value'], 0)

    def test_premultiplied_alpha_has_no_colorkey_contamination(self):
        source = Image.new('RGBA', (1, 1), (200, 100, 50, 128))
        self.assertEqual(WindowsLayeredPresenter._premultiplied_bgra(source), bytes((25, 50, 100, 128)))

    def test_fast_contour_dilation_matches_a_padded_rank_filter(self):
        from PIL import ImageDraw, ImageFilter
        mask = Image.new('L', (80, 80))
        ImageDraw.Draw(mask).ellipse((30, 30, 46, 49), fill=220)
        for radius in (3, 6, 9, 11):
            self.assertEqual(_dilate(mask, radius).tobytes(), mask.filter(ImageFilter.MaxFilter(radius * 2 + 1)).tobytes())


if __name__ == "__main__":
    unittest.main()
