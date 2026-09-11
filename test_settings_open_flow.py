"""Settings opening must not leave the application with no visible window."""
from pathlib import Path
import tempfile
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
        window.backend_current_page = 'history'
        events = []
        callbacks = []
        window.root = SimpleNamespace(
            after=lambda delay, callback: callbacks.append(callback) or 'pending-settings',
            withdraw=lambda: events.append('main-hidden'),
            deiconify=lambda: events.append('main-restored'),
        )
        window.history_window = SimpleNamespace(
            winfo_exists=lambda: True,
            deiconify=lambda: events.append('settings-shown'),
            lift=lambda: events.append('settings-raised'),
            update_idletasks=lambda: events.append('settings-mapped'),
            winfo_viewable=lambda: True,
            focus_force=lambda: events.append('settings-focused'),
        )
        for name in ('_select_backend_page', '_hide_main_tooltip', '_close_notice_window',
                     '_remember_root_geometry', '_destroy_unlock_window', '_sync_backend_window_topmost',
                     '_schedule_layered_main_render', '_show_notice'):
            setattr(window, name, mock.Mock())
        return window, events, callbacks

    def test_open_is_deferred_and_duplicate_clicks_are_coalesced(self):
        window, events, callbacks = self.window()
        window.show_settings()
        window.show_settings()
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(events, [])
        callbacks[0]()
        self.assertLess(events.index('settings-mapped'), events.index('main-hidden'))
        self.assertEqual(events[-1], 'settings-focused')
        self.assertTrue(window.main_window_intentionally_hidden)
        window._select_backend_page.assert_called_once_with('settings')

    def test_failed_open_restores_main_and_records_error(self):
        window, events, callbacks = self.window()
        window.history_window.deiconify = mock.Mock(side_effect=RuntimeError('test map failure'))
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(DpsWindow.show_settings.__globals__, APP_DIR=Path(directory)):
                window.show_settings()
                callbacks[0]()
            self.assertIn('test map failure', (Path(directory) / 'dps_error.log').read_text(encoding='utf-8'))
        self.assertNotIn('main-hidden', events)
        self.assertIn('main-restored', events)
        self.assertFalse(window.main_window_intentionally_hidden)
        window._show_notice.assert_called_once()

    def test_closing_app_does_not_reopen_a_scheduled_settings_window(self):
        window, events, callbacks = self.window()
        window.show_settings()
        window.closing = True
        callbacks[0]()
        self.assertEqual(events, [])
