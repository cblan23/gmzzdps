"""Settings opening must not leave the application with no visible window."""
import ctypes
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from test_combat_model import DpsWindow


class SettingsOpenFlowTests(unittest.TestCase):
    def window(self):
        window = object.__new__(DpsWindow)
        window.closing = False
        window.compact_mode = False
        window.main_window_intentionally_hidden = False
        events = []
        callbacks = []
        window.root = SimpleNamespace(
            after=lambda delay, callback: callbacks.append(callback) or 'pending-settings',
            withdraw=lambda: events.append('main-hidden'),
            deiconify=lambda: events.append('main-restored'),
        )
        for name in ('_hide_main_tooltip', '_close_notice_window',
                     '_remember_root_geometry', '_destroy_unlock_window',
                     '_schedule_layered_main_render', '_show_notice'):
            setattr(window, name, mock.Mock())
        return window, events, callbacks




    def interaction_window(self, *, layered):
        window, events, callbacks = self.window()
        window.window_locked = False
        window.startup_identity_pending = True
        window.startup_capture_pending = True
        window.resize_state = {}
        window._layered_main_active = lambda: layered
        window.root.configure = mock.Mock()
        window._hide_enrage_tooltip = mock.Mock()
        window._drag_start = mock.Mock()
        window._drag_move = mock.Mock()
        window._drag_end = mock.Mock()
        return window, events, callbacks


    def test_layered_startup_gate_still_rejects_stale_non_settings_regions(self):
        for region in ('action:pin', 'action:lock',
                       'drag:window', 'resize:height', 'scrollbar:rows'):
            with self.subTest(region=region):
                window, events, callbacks = self.interaction_window(layered=True)
                window.layered_main_hit_regions = {region: (0, 0, 30, 30)}
                window._set_main_combat_mode = mock.Mock()
                window.toggle_topmost = mock.Mock()
                window.toggle_window_lock = mock.Mock()
                event = SimpleNamespace(x=10, y=10, x_root=110, y_root=110)

                window._layered_main_press(event)
                window.layered_main_press_action = region.removeprefix('action:')
                window._layered_main_release(event)

                self.assertEqual(events, [])
                self.assertEqual(callbacks, [])
                window._drag_start.assert_not_called()
                window._set_main_combat_mode.assert_not_called()
                window.toggle_topmost.assert_not_called()
                window.toggle_window_lock.assert_not_called()


    def test_fallback_startup_gate_still_blocks_non_settings_press(self):
        window, _events, callbacks = self.interaction_window(layered=False)
        window.settings_button = object()

        window._fallback_main_press(SimpleNamespace(widget=object(), x_root=110, y_root=110))

        self.assertIsNone(getattr(window, 'main_fallback_press_origin', None))
        window._drag_start.assert_not_called()
        self.assertEqual(callbacks, [])

    def test_startup_button_sync_enables_settings_but_not_pin_or_lock(self):
        window, _events, _callbacks = self.interaction_window(layered=False)
        window._sync_main_icon_button_visual = mock.Mock()
        window._preferred_main_topmost = lambda: True
        window._main_clear_blocked = lambda: True
        for name in ('settings_button', 'pin_button', 'lock_button'):
            setattr(window, name, SimpleNamespace(winfo_exists=lambda: True, configure=mock.Mock()))

        window._sync_action_buttons()

        self.assertFalse(window.settings_button._disabled)
        window.settings_button.configure.assert_called_with(cursor='hand2')
        self.assertTrue(window.pin_button._disabled)
        self.assertTrue(window.lock_button._disabled)

    def test_temporary_settings_disable_still_applies_while_starting(self):
        window, events, callbacks = self.interaction_window(layered=True)
        window.layered_main_hit_regions = {'action:settings': (0, 0, 30, 30)}
        event = SimpleNamespace(x=10, y=10, x_root=110, y_root=110)
        with mock.patch.dict(DpsWindow.show_settings.__globals__, SETTINGS_TEMPORARILY_DISABLED=True):
            window.show_settings()
            window._layered_main_press(event)
            window._layered_main_release(event)
        self.assertEqual(callbacks, [])
        self.assertEqual(events, [])



    def test_settings_work_area_uses_an_uncached_monitor_api_binding(self):
        class NativeCall:
            def __init__(self, callback):
                self.callback = callback
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.callback(*args)

        def fill_monitor_info(_monitor, info_pointer):
            work = info_pointer._obj.rcWork
            work.left = -1920
            work.top = 0
            work.right = 0
            work.bottom = 1040
            return True

        monitor_api = SimpleNamespace(
            MonitorFromWindow=NativeCall(lambda _handle, _flags: 7),
            GetMonitorInfoW=NativeCall(fill_monitor_info),
        )
        cached_api = SimpleNamespace(
            MonitorFromWindow=mock.Mock(side_effect=AssertionError('cached API used')),
            GetMonitorInfoW=mock.Mock(side_effect=AssertionError('cached API used')),
        )
        target = SimpleNamespace(
            winfo_id=lambda: 123,
            winfo_screenwidth=lambda: 1920,
            winfo_screenheight=lambda: 1080,
        )
        window = object.__new__(DpsWindow)

        with (
            mock.patch.object(sys, 'platform', 'win32'),
            mock.patch.object(ctypes, 'WinDLL', return_value=monitor_api),
            mock.patch.object(
                ctypes,
                'windll',
                SimpleNamespace(user32=cached_api),
                create=True,
            ),
        ):
            work_area = window._work_area(target)

        self.assertEqual(work_area, (-1920, 0, 1920, 1040))
        cached_api.MonitorFromWindow.assert_not_called()
        cached_api.GetMonitorInfoW.assert_not_called()

    def test_close_hides_hud_before_shutdown_cleanup(self):
        window = object.__new__(DpsWindow)
        window.closing = False
        window.root = mock.Mock()
        window.tray_icon = None
        window.login_window = None
        window.status_label = mock.Mock()
        window.close_status_text = ""
        window.heartbeat_stop_event = mock.Mock()
        window.combat_clock_stop_event = mock.Mock()
        window._hide_main_tooltip = mock.Mock(
            side_effect=lambda: self.assertTrue(window.root.withdraw.called)
        )
        window._hide_enrage_tooltip = mock.Mock()
        window._set_window_click_through = mock.Mock()
        window._destroy_unlock_window = mock.Mock()
        window._destroy_main_content_overlay = mock.Mock()
        window._close_notice_window = mock.Mock()
        window._close_user_notice = mock.Mock()
        window._save_preferences = mock.Mock()
        window._request_capture_stop = mock.Mock()

        window.close()

        window.root.withdraw.assert_called_once_with()
        window._hide_enrage_tooltip.assert_called_once_with()
        window.root.after.assert_called_once_with(50, window._finish_close)
