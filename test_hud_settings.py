"""The settings page lives in the HUD and uses the current card session."""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

from hud_settings import render_hud_settings, settings_panel_position
from test_combat_model import DpsWindow


FONT_PATH = str(Path("C:/Windows/Fonts/msyh.ttc"))


class HudSettingsTests(unittest.TestCase):
    def test_settings_image_contains_expiry_and_click_targets(self):
        first, regions = render_hud_settings(
            {"expiry": "到期时间：2026-10-15 23:59:59"}, 0, FONT_PATH
        )
        later, _ = render_hud_settings(
            {"expiry": "到期时间：2027-10-15 23:59:59"}, 0, FONT_PATH
        )
        self.assertNotEqual(first.tobytes(), later.tobytes())
        self.assertIn("action:settings_close", regions)
        self.assertEqual(regions["action:settings_close"], (264, 10, 296, 43))
        self.assertIn("action:settings_toggle:upload", regions)
        self.assertIn("action:settings_page:2", regions)
        self.assertEqual(first.size, (306, 294))
        panel_alpha = first.getpixel((153, 85))[3]
        self.assertGreater(panel_alpha, 150)
        self.assertLess(panel_alpha, 235)

    def test_settings_opens_beside_hud_with_top_edges_aligned(self):
        self.assertEqual(
            settings_panel_position(
                (1500, 100, 306, 500),
                (306, 316),
                (0, 0, 1920, 1080),
            ),
            (1182, 100),
        )
        self.assertEqual(
            settings_panel_position(
                (20, 700, 306, 300),
                (306, 316),
                (0, 0, 1920, 1080),
            ),
            (338, 700),
        )

    def test_settings_position_clamps_at_the_bottom_of_the_work_area(self):
        self.assertEqual(
            settings_panel_position(
                (1500, 900, 306, 160),
                (306, 316),
                (0, 0, 1920, 1080),
            ),
            (1182, 752),
        )

    def test_saving_settings_keeps_the_hud_geometry(self):
        window = object.__new__(DpsWindow)
        window.root = SimpleNamespace(geometry=lambda: "306x342+12+12")
        window.config = {}
        window.restore_geometry = {}
        window.window_locked = False
        window.hud_settings_open = True
        window.hud_settings_anchor_bounds = (1614, 232, 306, 455)

        window._remember_root_geometry()

        self.assertEqual(window.config["geometry"], "306x455+1614+232")

    def test_expiry_comes_from_current_card_session(self):
        window = object.__new__(DpsWindow)
        window.licensing = SimpleNamespace(session=SimpleNamespace(
            card_tier="normal", expires_at=dt.datetime(2026, 11, 1, 12, 30)
        ))
        for name in (
            "toggle_hotkey_capture_active", "hud_view_hotkey_capture_active",
            "unlock_hotkey_capture_active", "hide_names", "show_deaths",
            "show_main_totals", "show_team_dps", "highlight_self",
            "show_combat_time", "show_boss_hp_bar",
            "boss_enrage_prediction_enabled", "auto_upload_combat_records_enabled",
            "toggle_visibility_hotkey_enabled", "hud_view_hotkey_enabled",
            "unlock_hotkey_enabled", "lock_toggle_hotkey_enabled",
        ):
            setattr(window, name, False)
        window.team_rating_preview_enabled = True
        window.ui_font_size = 14
        window.main_ui_scale = 1.0
        window.main_row_mask_opacity = 50
        window.toggle_visibility_hotkey = "Home"
        window.hud_view_hotkey = "Ctrl+Tab"
        window.unlock_hotkey = "Ctrl+U"
        window.toggle_hotkey_error = ""
        window.hud_view_hotkey_error = ""
        window.unlock_hotkey_error = ""
        window._profession_display_metric = lambda profession_id: "hps"

        self.assertEqual(
            window._hud_settings_values()["expiry"],
            "到期时间：2026-11-01 12:30:00",
        )

    def test_session_change_repaints_expiry_without_legacy_sidebar(self):
        window = object.__new__(DpsWindow)
        window.hud_settings_open = True
        window._schedule_layered_main_render = mock.Mock()

        window._sync_expiry_label()

        window._schedule_layered_main_render.assert_called_once_with(force=True)

    def test_gear_opens_settings_and_upload_toggle_persists(self):
        window = object.__new__(DpsWindow)
        window.closing = False
        window.hud_settings_open = False
        window.hud_settings_page = 2
        window.layered_main_hover_action = "settings"
        window.root = SimpleNamespace()
        window._schedule_layered_main_render = mock.Mock()
        window.show_settings()
        self.assertTrue(window.hud_settings_open)
        self.assertEqual(window.hud_settings_page, 0)
        window._schedule_layered_main_render.assert_called_once_with(force=True)

        window.auto_upload_combat_records_enabled = True
        window.automatic_upload_queued = {"battle-a"}
        window._sync_main_component_visibility = mock.Mock()
        window._save_preferences = mock.Mock()
        window._apply_hud_settings_action("settings_toggle:upload")
        self.assertFalse(window.auto_upload_combat_records_enabled)
        self.assertEqual(window.automatic_upload_queued, set())
        window._save_preferences.assert_called_once()

    def test_non_layered_hud_uses_the_same_root_window(self):
        window = object.__new__(DpsWindow)
        geometry = ["306x500+10+20"]
        window.root = SimpleNamespace(geometry=lambda value=None: (
            geometry.__setitem__(0, value) if value is not None else geometry[0]
        ))
        window.main_shell = mock.Mock()
        window.hud_settings_page = 0
        window.hud_settings_open = True
        window.hud_settings_fallback_label = None
        window.hud_settings_fallback_geometry = None
        window.window_dpi = 96
        window.main_ui_scale = 1.0
        window.layered_main_renderer = None
        window._hud_settings_values = mock.Mock(return_value={})
        label = mock.Mock()
        with (
            mock.patch.dict(
                DpsWindow._render_fallback_hud_settings.__globals__,
                render_hud_settings=lambda *_args: (
                    Image.new("RGBA", (392, 565)),
                    {"action:settings_close": (337, 13, 372, 47)},
                ),
            ),
            mock.patch("tkinter.Label", return_value=label),
            mock.patch("PIL.ImageTk.PhotoImage", return_value=object()),
        ):
            window._render_fallback_hud_settings()
        self.assertEqual(geometry[0], "392x565")
        window.main_shell.pack_forget.assert_called_once()
        window._apply_hud_settings_action = mock.Mock()
        window._fallback_hud_settings_release(SimpleNamespace(x=350, y=25))
        window._apply_hud_settings_action.assert_called_once_with("settings_close")

        for name in (
            "_cancel_toggle_hotkey_capture", "_cancel_hud_view_hotkey_capture",
            "_cancel_unlock_hotkey_capture", "_schedule_layered_main_render",
        ):
            setattr(window, name, mock.Mock())
        window._close_hud_settings()
        label.destroy.assert_called_once()
        window.main_shell.pack.assert_called_once_with(fill="both", expand=True)
        self.assertEqual(geometry[0], "306x500+10+20")


if __name__ == "__main__":
    unittest.main()
