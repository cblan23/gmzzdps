"""Null and source semantics in the established combat-history detail UI."""
import runpy
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent


class LabelProbe:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class CanvasProbe:
    def __init__(self, width=800, height=320):
        self.width = width
        self.height = height
        self.texts = []

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    def winfo_exists(self):
        return True

    def delete(self, *_args):
        self.texts.clear()

    def create_text(self, *_args, **options):
        self.texts.append(str(options.get("text", "")))
        return len(self.texts)

    def create_rectangle(self, *_args, **_options):
        return 0

    def create_image(self, *_args, **_options):
        return 0

    def create_oval(self, *_args, **_options):
        return 0

    def create_line(self, *_args, **_options):
        return 0

    def create_polygon(self, *_args, **_options):
        return 0

    def tag_bind(self, *_args, **_options):
        return None

    def configure(self, **_options):
        return None


class IconProbe:
    @staticmethod
    def skill(*_args):
        return object()

    @staticmethod
    def history_hero(*_args):
        return object()

    @staticmethod
    def boss(*_args, **_kwargs):
        return object()


class FontProbe:
    @staticmethod
    def measure(value):
        return len(str(value)) * 8


class OfficialHistorySettlementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = runpy.run_path(
            str(ROOT / "dps_meter.pyw"), run_name="settlement_official_ui_test"
        )
        cls.window_type = cls.app["DpsWindow"]

    def player_summary_window(self):
        window = object.__new__(self.window_type)
        window.history_player_name_label = LabelProbe()
        window.history_player_rank_label = LabelProbe()
        window.history_player_rating_label = LabelProbe()
        window.history_player_icon_label = None
        window.history_player_primary_label = LabelProbe()
        window.history_player_primary_caption = LabelProbe()
        window.history_player_stat_labels = {
            key: (LabelProbe(), LabelProbe())
            for key in ("amount", "share", "rates")
        }
        window.history_player_share_canvas = None
        window.history_skill_share_canvas = None
        window.history_detail_label = LabelProbe()
        window.history_local_detail_modules_visible = False
        window._profession_info = lambda _class_id: ("", "#57cdb5")
        window._history_names_hidden = lambda: False
        return window

    def test_missing_healing_and_taken_values_render_as_dashes(self):
        for mode, participant, total_key in (
            (
                "hps",
                {"actor_id": 1, "name": "A", "effective_healing": None, "hps": None},
                "team_effective_healing",
            ),
            (
                "dt",
                {"actor_id": 1, "name": "A", "taken": None, "share": None},
                "team_taken",
            ),
        ):
            with self.subTest(mode=mode):
                window = self.player_summary_window()
                window.history_meter_mode = mode
                record = {
                    "duration_seconds": None,
                    total_key: None,
                    "settlement_status": "PENDING",
                }
                window._render_history_player_summary(
                    record,
                    [participant],
                    participant,
                    healing_mode=mode == "hps",
                    taken_mode=mode == "dt",
                )

                self.assertEqual(
                    window.history_player_primary_label.options["text"], "--"
                )
                self.assertEqual(
                    window.history_player_stat_labels["amount"][1].options["text"],
                    "--",
                )
                self.assertEqual(
                    window.history_player_stat_labels["share"][1].options["text"],
                    "--",
                )

    def test_skill_count_is_plain_and_empty_max_hit_column_is_hidden(self):
        window = object.__new__(self.window_type)
        canvas = CanvasProbe()
        participant = {
            "actor_id": 1,
            "profession_id": 0,
            "skills": [
                {
                    "skill_id": 123,
                    "name": "测试技能",
                    "damage": 100,
                    "share": 1.0,
                    "hits": 3,
                    "max_hit": None,
                    "count_semantics": "server_skill_count",
                }
            ],
        }
        record = {
            "encounter_id": "settlement",
            "duration_seconds": None,
            "dps_duration_seconds": None,
            "participants": [participant],
        }
        window.history_skill_canvas = canvas
        window.history_meter_mode = "dps"
        window.history_detail_mode = "skills"
        window.history_page_mode = "detail"
        window.history_selected_actor = 1
        window.history_selected_id = "settlement"
        window.history_loaded_record = record
        window.history_records = []
        window.icons = IconProbe()
        window._ui_font = lambda _role: "TkDefaultFont"
        window._profession_info = lambda _class_id: ("", "#57cdb5")

        window._draw_history_skills()

        self.assertIn("次数", canvas.texts)
        self.assertNotIn("服务器次数", canvas.texts)
        self.assertNotIn("打击次数", canvas.texts)
        self.assertNotIn("最高一击", canvas.texts)
        self.assertNotIn("?", canvas.texts)
        self.assertGreaterEqual(canvas.texts.count("--"), 1)
        heading, help_text = window._history_skill_count_presentation(participant)
        self.assertEqual(heading, "次数")
        self.assertEqual(help_text, "")

        participant["skills"][0]["max_hit"] = 80
        window._draw_history_skills()
        self.assertIn("最高一击", canvas.texts)
        self.assertIn("80", canvas.texts)

    def test_history_self_dps_is_not_replaced_by_team_settlement_status(self):
        window = self.player_summary_window()
        window.history_meter_mode = "dps"
        participant = {
            "actor_id": 1, "name": "Self", "is_self": True,
            "damage": 56789, "dps": 1234, "skills": [],
        }
        for status in ("PENDING", "ABANDONED", "SETTLED"):
            with self.subTest(status=status):
                record = {
                    "duration_seconds": None, "result": "failed",
                    "settlement_status": status,
                }
                summary = {
                    **record,
                    "team_dps": 4321,
                    "my_dps": 1234,
                    "my_damage": 56789,
                }
                self.assertEqual(
                    window._history_list_performance(summary),
                    ("团队 4,321 DPS", "我的 1,234 DPS"),
                )
                self.assertIsNone(window._history_settlement_presentation(summary))
                window._render_history_player_summary(
                    record, [participant], participant,
                    healing_mode=False, taken_mode=False,
                )
                self.assertEqual(window.history_player_primary_label.options["text"], "1,234")
                detail = window.history_detail_label.options["text"]
                for text in ("等待", "延迟", "未取得结算"):
                    self.assertNotIn(text, detail)

        self.assertEqual(window._history_list_performance({"settlement_status": "PENDING"}),
                         ("团队 DPS --", "我的 DPS --"))

    def test_history_hero_keeps_unknown_duration_without_settlement_badge(self):
        window = object.__new__(self.window_type)
        canvas = CanvasProbe(height=190)
        record = {
            "encounter_id": "settlement",
            "duration_seconds": None,
            "team_size": 2,
            "participants": [{"actor_id": 1}, {"actor_id": 2}],
            "settlement_status": "SETTLED",
            "result": "failed",
        }
        summary = {
            "battle_id": "settlement",
            "dungeon_name": "五月庄园·花园",
            "boss_name": "异化猎犬",
            "stage_name": "异化猎犬",
            "boss_count": 1,
            "settlement_status": "SETTLED",
            "result": "failed",
        }
        window.history_detail_hero_canvas = canvas
        window.history_meter_mode = "dps"
        window.history_selected_id = "settlement"
        window.history_loaded_record = record
        window.history_records = [summary]
        window.icons = IconProbe()
        window._ui_font = lambda _role: FontProbe()

        window._draw_history_detail_hero()

        self.assertIn("BOSS ENCOUNTER ARCHIVE", canvas.texts)
        self.assertNotIn(
            "BOSS ENCOUNTER ARCHIVE · ENCOUNTER ARCHIVE", canvas.texts
        )
        self.assertTrue(
            any("战斗时长 --" in text for text in canvas.texts), canvas.texts
        )
        self.assertFalse(
            any("战斗时长 00:00" in text for text in canvas.texts)
        )
        self.assertNotIn("延迟补齐", canvas.texts)
        self.assertNotIn("等待结算", canvas.texts)
        self.assertNotIn("未取得结算", canvas.texts)
        self.assertIn("难度未知", canvas.texts)
        self.assertNotIn("普通模式", canvas.texts)
        self.assertTrue(any("失败" in text for text in canvas.texts))

    def test_age_cleanup_uses_encounter_end_boundary(self):
        now_ns = int(time.time() * 1_000_000_000)
        recent_end = SimpleNamespace(
            local_encounter_id="recent-end",
            started_at_ns=now_ns - 40 * 86_400 * 1_000_000_000,
            ended_at_ns=now_ns - 10 * 86_400 * 1_000_000_000,
            history_deleted=False,
        )
        old_end = SimpleNamespace(
            local_encounter_id="old-end",
            started_at_ns=now_ns - 40 * 86_400 * 1_000_000_000,
            ended_at_ns=now_ns - 35 * 86_400 * 1_000_000_000,
            history_deleted=False,
        )
        hidden = []
        deleted_days = []
        window = object.__new__(self.window_type)
        window.settlement_ui = SimpleNamespace(
            tracker=SimpleNamespace(
                encounters={"recent-end": recent_end, "old-end": old_end}
            )
        )
        window.history_selected_ids = set()
        window.history_selected_id = ""
        window.history_loaded_record = None
        window.history_window = None
        window.history_store = SimpleNamespace(
            delete_older_than=lambda days: deleted_days.append(days)
        )
        window._close_history_modal = lambda: None
        window._hide_settlement_history_records = (
            lambda encounter_ids: hidden.append(set(encounter_ids))
        )
        window._show_history_browser = lambda: None
        window._refresh_history_records = lambda: None
        window._show_notice = (
            lambda *_args, **kwargs: kwargs["on_confirm"]()
        )

        window._confirm_history_cleanup("30")

        self.assertEqual(hidden, [{"old-end"}])
        self.assertEqual(deleted_days, [30])


if __name__ == "__main__":
    unittest.main()
