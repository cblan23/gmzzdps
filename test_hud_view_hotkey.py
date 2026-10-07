import queue
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from test_combat_model import DpsWindow, WINDOWS_TRAY_MODULE


class HudViewHotkeyTests(unittest.TestCase):
    def window(self, key="", enabled=False):
        window = object.__new__(DpsWindow)
        window.closing = False
        window.config = {"hud_view_hotkey": key, "hud_view_hotkey_enabled": enabled}
        window.hud_view_hotkey = key
        window.hud_view_hotkey_enabled = enabled
        window.hud_view_hotkey_registered = enabled
        window.hud_view_hotkey_capture_active = False
        window.hud_view_hotkey_capture_modifiers = set()
        window.hud_view_hotkey_error = ""
        window.tray_icon = mock.Mock(alive=True)
        window.tray_icon.set_hotkey.return_value = True
        save = mock.patch.dict(DpsWindow._clear_hud_view_hotkey.__globals__, save_config=mock.Mock())
        save.start()
        self.addCleanup(save.stop)
        return window

    def test_unconfigured_shortcut_does_not_register_a_key(self):
        window = self.window()
        self.assertTrue(window._register_configured_hud_view_hotkey())
        window.tray_icon.set_hotkey.assert_called_once_with(0, 0, action="hud_view")
        self.assertFalse(window.hud_view_hotkey_registered)
        self.assertFalse(window._set_hud_view_hotkey_enabled(True))
        self.assertFalse(window.hud_view_hotkey_enabled)

    def test_recorded_key_enables_immediately_and_disable_keeps_binding(self):
        window = self.window()
        window._begin_hud_view_hotkey_capture()
        window._capture_hud_view_hotkey_key(SimpleNamespace(keysym="F8", state=0))
        window.tray_icon.set_hotkey.assert_called_with(0, 0x77, action="hud_view")
        self.assertEqual(window.config, {"hud_view_hotkey": "F8", "hud_view_hotkey_enabled": True})
        self.assertTrue(window.hud_view_hotkey_registered)
        self.assertFalse(window.hud_view_hotkey_capture_active)

        self.assertTrue(window._set_hud_view_hotkey_enabled(False))
        self.assertEqual(window.hud_view_hotkey, "F8")
        self.assertFalse(window.hud_view_hotkey_registered)
        self.assertTrue(window._set_hud_view_hotkey_enabled(True))
        window._clear_hud_view_hotkey()
        self.assertEqual(window.config, {"hud_view_hotkey": "", "hud_view_hotkey_enabled": False})
        self.assertFalse(window.hud_view_hotkey_registered)

    def test_conflicting_capture_can_cancel_and_restore_previous_binding(self):
        window = self.window("F8", True)
        window.tray_icon.set_hotkey.side_effect = [True, False, True]
        window._begin_hud_view_hotkey_capture()
        self.assertFalse(window.hud_view_hotkey_registered)
        window._capture_hud_view_hotkey_key(SimpleNamespace(keysym="F9", state=0))
        self.assertTrue(window.hud_view_hotkey_capture_active)
        self.assertEqual(window.hud_view_hotkey, "F8")
        self.assertIn("占用", window.hud_view_hotkey_error)
        window._capture_hud_view_hotkey_key(SimpleNamespace(keysym="Escape", state=0))
        self.assertTrue(window.hud_view_hotkey_registered)
        self.assertFalse(window.hud_view_hotkey_capture_active)
        self.assertEqual(window.tray_icon.set_hotkey.call_args_list, [
            mock.call(0, 0, action="hud_view"),
            mock.call(0, 0x78, action="hud_view"),
            mock.call(0, 0x77, action="hud_view"),
        ])

    def test_modifiers_are_recorded_and_released_before_next_key(self):
        window = self.window()
        window._begin_hud_view_hotkey_capture()
        window._capture_hud_view_hotkey_key(SimpleNamespace(keysym="Control_L", state=0))
        window._capture_hud_view_hotkey_key(SimpleNamespace(keysym="h", state=0))
        self.assertEqual(window.hud_view_hotkey, "Ctrl+H")
        window.tray_icon.set_hotkey.assert_called_with(2, ord("H"), action="hud_view")

        window._begin_hud_view_hotkey_capture()
        window._capture_hud_view_hotkey_key(SimpleNamespace(keysym="Alt_L", state=0))
        window._release_hud_view_hotkey_modifier(SimpleNamespace(keysym="Alt_L"))
        window._capture_hud_view_hotkey_key(SimpleNamespace(keysym="F9", state=0))
        self.assertEqual(window.hud_view_hotkey, "F9")

    def test_bare_letter_is_rejected_without_changing_configuration(self):
        window = self.window()
        window._begin_hud_view_hotkey_capture()
        window._capture_hud_view_hotkey_key(SimpleNamespace(keysym="h", state=0))
        self.assertTrue(window.hud_view_hotkey_capture_active)
        self.assertEqual(window.hud_view_hotkey, "")
        window.tray_icon.set_hotkey.assert_not_called()

    def test_dispatch_switches_current_mode_without_touching_combat(self):
        window = self.window("F8", True)
        window.model = object()
        original_model = window.model
        window._set_pve_hud_view = mock.Mock(side_effect=lambda view: setattr(window, "pve_hud_view", view))
        window._set_pvp_hud_view = mock.Mock(side_effect=lambda view: setattr(window, "pvp_hud_view", view))
        window.main_combat_mode = "pve"
        window.pve_hud_view = "team"
        window.pvp_hud_view = "team"
        for _ in range(2):
            window._dispatch_message("hotkey_toggle_hud_view", None)
        self.assertEqual(window._set_pve_hud_view.call_args_list, [mock.call("recent_battle"), mock.call("team")])
        window._set_pvp_hud_view.assert_not_called()

        window.main_combat_mode = "pvp"
        for _ in range(2):
            window._dispatch_message("hotkey_toggle_hud_view", None)
        self.assertEqual(window._set_pvp_hud_view.call_args_list, [mock.call("live"), mock.call("team")])
        self.assertIs(window.model, original_model)
        window._set_pvp_hud_view.reset_mock()
        window.hud_view_hotkey_capture_active = True
        window._dispatch_message("hotkey_toggle_hud_view", None)
        window.hud_view_hotkey_capture_active = False
        window.hud_view_hotkey_enabled = False
        window._dispatch_message("hotkey_toggle_hud_view", None)
        window._set_pvp_hud_view.assert_not_called()

    def test_native_conflict_preserves_binding_and_other_shortcuts(self):
        tray = object.__new__(WINDOWS_TRAY_MODULE.WindowsTrayIcon)
        tray.hwnd = 123
        tray._active_hotkey_id = 1
        tray._unlock_hotkey_id = 5
        register = mock.Mock(side_effect=[True, False])
        unregister = mock.Mock(return_value=True)
        with mock.patch.object(WINDOWS_TRAY_MODULE.user32, "RegisterHotKey", register), mock.patch.object(
            WINDOWS_TRAY_MODULE.user32, "UnregisterHotKey", unregister
        ):
            self.assertTrue(tray._apply_hud_view_hotkey(0, 0x77))
            self.assertFalse(tray._apply_hud_view_hotkey(0, 0x78))
            self.assertEqual(tray._hud_view_hotkey, (0, 0x77))
            unregister.assert_not_called()
            self.assertTrue(tray._apply_hud_view_hotkey(0, 0))
        unregister.assert_called_once_with(123, WINDOWS_TRAY_MODULE.HOTKEY_ID_HUD_VIEW_PRIMARY)
        self.assertEqual(tray._active_hotkey_id, 1)
        self.assertEqual(tray._unlock_hotkey_id, 5)
        self.assertEqual(register.call_args_list[0].args[2], WINDOWS_TRAY_MODULE.MOD_NOREPEAT)

    def test_tray_configuration_and_notification_route_to_view_action(self):
        tray = object.__new__(WINDOWS_TRAY_MODULE.WindowsTrayIcon)
        tray._hotkey_requests = queue.Queue()
        tray._apply_hud_view_hotkey = mock.Mock(return_value=True)
        tray._apply_hotkey = mock.Mock()
        tray._apply_unlock_hotkey = mock.Mock()
        completed, result = threading.Event(), []
        tray._hotkey_requests.put((3, 0x77, completed, result, threading.Lock(), "hud_view"))
        tray._process_hotkey_request()
        self.assertTrue(completed.is_set())
        self.assertEqual(result, [True])
        tray._apply_hud_view_hotkey.assert_called_once_with(3, 0x77)
        tray._apply_hotkey.assert_not_called()
        tray._apply_unlock_hotkey.assert_not_called()
        tray._hud_view_hotkey_id = WINDOWS_TRAY_MODULE.HOTKEY_ID_HUD_VIEW_PRIMARY
        tray._emit = mock.Mock()
        self.assertEqual(tray._window_proc(123, WINDOWS_TRAY_MODULE.WM_HOTKEY, tray._hud_view_hotkey_id, 0), 0)
        tray._emit.assert_called_once_with("hotkey_toggle_hud_view")


if __name__ == "__main__":
    unittest.main()
