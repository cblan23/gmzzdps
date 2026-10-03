"""Settings opening must not leave the application with no visible window."""
from pathlib import Path
import ctypes
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from test_combat_model import DpsWindow


class GeometryProbe:
    def __init__(self, geometry='900x600+10+20'):
        self.value = geometry

    def geometry(self, value=None):
        if value is not None:
            self.value = value
        return self.value

    def _size(self):
        size = self.value.split('+', 1)[0]
        width, height = size.split('x', 1)
        return int(width), int(height)

    def winfo_width(self):
        return self._size()[0]

    def winfo_height(self):
        return self._size()[1]

    def winfo_exists(self):
        return True

    def update_idletasks(self):
        return None


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
        with mock.patch.dict(
            DpsWindow.show_settings.__globals__,
            SETTINGS_TEMPORARILY_DISABLED=False,
        ):
            window.show_settings()
            window.show_settings()
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(events, [])
            callbacks[0]()
        self.assertLess(events.index('settings-shown'), events.index('main-hidden'))
        self.assertEqual(events[-1], 'settings-focused')
        self.assertTrue(window.main_window_intentionally_hidden)
        window._select_backend_page.assert_called_once_with('settings')

    def test_runtime_settings_entry_is_enabled(self):
        window, events, callbacks = self.window()

        window.show_settings()

        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertIn('settings-shown', events)
        window._select_backend_page.assert_called_once_with('settings')

    def test_settings_open_during_capture_and_identity_initialization(self):
        for identity_pending, capture_pending in ((True, False), (False, True), (True, True)):
            with self.subTest(identity=identity_pending, capture=capture_pending):
                window, events, callbacks = self.window()
                window.startup_identity_pending = identity_pending
                window.startup_capture_pending = capture_pending
                self.assertTrue(window._startup_interaction_blocked())

                window.show_settings()

                self.assertEqual(len(callbacks), 1)
                callbacks[0]()
                self.assertIn('settings-shown', events)
                window._select_backend_page.assert_called_once_with('settings')
                self.assertTrue(window._startup_interaction_blocked())

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

    def test_layered_settings_hover_and_click_work_while_starting_without_drag(self):
        window, events, callbacks = self.interaction_window(layered=True)
        window.layered_main_hit_regions = {'action:settings': (0, 0, 30, 30)}
        event = SimpleNamespace(x=10, y=10, x_root=110, y_root=110)

        window._layered_main_motion(event)
        window._layered_main_press(event)
        window._layered_main_drag(event)
        window._layered_main_release(event)

        window.root.configure.assert_called_with(cursor='hand2')
        self.assertEqual(window.layered_main_hover_action, 'settings')
        window._drag_start.assert_not_called()
        window._drag_move.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertIn('settings-shown', events)
        self.assertTrue(window._startup_interaction_blocked())

        window._layered_main_motion(SimpleNamespace(x=50, y=50))
        window.root.configure.assert_called_with(cursor='arrow')
        self.assertEqual(window.layered_main_hover_action, '')

    def test_layered_startup_gate_still_rejects_stale_non_settings_regions(self):
        for region in ('action:pvp', 'action:pve', 'action:pin', 'action:lock',
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

    def test_fallback_settings_click_works_while_starting_without_window_drag(self):
        window, events, callbacks = self.interaction_window(layered=False)
        button = SimpleNamespace(winfo_width=lambda: 30, winfo_height=lambda: 30)
        window.settings_button = button
        event = SimpleNamespace(widget=button, x=10, y=10, x_root=110, y_root=110)

        window._fallback_main_press(event)
        window._fallback_main_drag(SimpleNamespace(x_root=130, y_root=110))
        other_command = mock.Mock()
        window._fallback_main_icon_release(event, object(), other_command)
        window._fallback_main_icon_release(event, button, window.show_settings)
        window._fallback_main_release(event)

        other_command.assert_not_called()
        window._drag_start.assert_not_called()
        window._drag_move.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertIn('settings-shown', events)
        self.assertTrue(window._startup_interaction_blocked())

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

    def test_failed_open_restores_main_and_records_error(self):
        window, events, callbacks = self.window()
        window.history_window.deiconify = mock.Mock(side_effect=RuntimeError('test map failure'))
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                DpsWindow.show_settings.__globals__,
                APP_DIR=Path(directory),
                SETTINGS_TEMPORARILY_DISABLED=False,
            ):
                window.show_settings()
                callbacks[0]()
            self.assertIn('test map failure', (Path(directory) / 'dps_error.log').read_text(encoding='utf-8'))
        self.assertNotIn('main-hidden', events)
        self.assertIn('main-restored', events)
        self.assertFalse(window.main_window_intentionally_hidden)
        window._show_notice.assert_called_once()

    def test_closing_app_does_not_reopen_a_scheduled_settings_window(self):
        window, events, callbacks = self.window()
        with mock.patch.dict(
            DpsWindow.show_settings.__globals__,
            SETTINGS_TEMPORARILY_DISABLED=False,
        ):
            window.show_settings()
        window.closing = True
        callbacks[0]()
        self.assertEqual(events, [])

    def test_settings_resize_and_maximize_cannot_overwrite_each_other(self):
        window = object.__new__(DpsWindow)
        root_callbacks = {}
        cancelled = []
        root = GeometryProbe('306x500+0+0')
        root.after = lambda _delay, callback: (
            root_callbacks.setdefault('resize', callback) or 'resize'
        ) and 'resize'
        root.after_cancel = lambda after_id: cancelled.append(after_id)
        settings = GeometryProbe()
        button = SimpleNamespace(configure=mock.Mock())
        window.root = root
        window.history_window = settings
        window.history_max_button = button
        window.skill_max_button = None
        window.max_button = None
        window.login_window = None
        window.user_notice_window = None
        window.window_locked = False
        window.resize_state = {id(settings): (0, 0, 900, 600)}
        window.pending_window_geometry = {id(settings): (settings, '1000x700+10+20')}
        window.window_geometry_after_ids = {id(settings): 'pending-resize'}
        window.restore_geometry = {}
        window._work_area = lambda _target: (0, 0, 1920, 1040)
        window._remember_root_geometry = mock.Mock()

        window._toggle_maximize(settings)

        self.assertEqual(window.restore_geometry[id(settings)], '1000x700+10+20')
        self.assertEqual(settings.geometry(), '1920x1040+0+0')
        self.assertNotIn(id(settings), window.resize_state)
        self.assertNotIn(id(settings), window.pending_window_geometry)
        self.assertEqual(cancelled, ['pending-resize'])

        # Pressing the bottom-right grip while maximized restores normal size
        # first, then uses that size as the drag origin.
        event = SimpleNamespace(x_root=1000, y_root=700)
        window._resize_start(event, settings)
        self.assertNotIn(id(settings), window.restore_geometry)
        self.assertEqual(window.resize_state[id(settings)][2:], (1000, 700))
        window._resize_move(
            SimpleNamespace(x_root=1040, y_root=730),
            settings,
            900,
            600,
        )
        window._resize_end(None, settings)
        self.assertEqual(settings.geometry(), '1040x730')
        self.assertNotIn(id(settings), window.resize_state)

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

    def test_backend_pages_are_built_lazily_and_cached(self):
        window = object.__new__(DpsWindow)
        window.backend_pages = {}
        window.backend_page_host = object()
        window.pvp_history_page = None
        window.pvp_alliance_page = None
        settings = SimpleNamespace(winfo_exists=lambda: True)
        window._build_backend_settings_page = mock.Mock(return_value=settings)
        window._build_backend_history_page = mock.Mock()
        window._build_backend_updates_page = mock.Mock()

        first = window._ensure_backend_page('settings')
        second = window._ensure_backend_page('settings')

        self.assertIs(first, settings)
        self.assertIs(second, settings)
        self.assertEqual(window.backend_pages, {'settings': settings})
        window._build_backend_settings_page.assert_called_once_with(
            window.backend_page_host
        )
        window._build_backend_history_page.assert_not_called()
        window._build_backend_updates_page.assert_not_called()

    def test_pvp_history_route_reuses_exact_native_pve_page(self):
        window = object.__new__(DpsWindow)
        window.backend_pages = {}
        window.backend_page_host = object()
        window.pvp_history_page = object()
        history = SimpleNamespace(winfo_exists=lambda: True)
        window._build_backend_history_page = mock.Mock(return_value=history)

        pvp = window._ensure_backend_page('kings')

        self.assertIs(pvp, history)
        self.assertIs(window.backend_pages['history'], history)
        self.assertIs(window.backend_pages['kings'], history)
        self.assertIsNone(window.pvp_history_page)
        window._build_backend_history_page.assert_called_once_with(
            window.backend_page_host
        )

    def test_pvp_history_replica_queries_only_pvp_repository(self):
        window = object.__new__(DpsWindow)
        window.backend_current_page = 'kings'
        window.history_store = SimpleNamespace(
            query_summaries=mock.Mock(),
            history_overview=mock.Mock(),
        )
        window.pvp_recording = SimpleNamespace(account_key='card-a')
        window.pvp_history_repository = SimpleNamespace(list=mock.Mock(return_value=[]))
        window.history_filter_time_var = None
        window.history_filter_entries = {}
        window.history_filter_dungeon_var = None
        window.history_filter_boss_var = None
        window.history_filter_result_var = None
        window.history_sort_var = None
        window.history_page_size = 10
        window.history_page_number = 1

        result, overview = window._query_pvp_history_summaries()

        self.assertEqual(result['records'], [])
        self.assertEqual(overview['battle_count'], 0)
        window.pvp_history_repository.list.assert_called_once_with('card-a')
        window.history_store.query_summaries.assert_not_called()
        window.history_store.history_overview.assert_not_called()

    def test_return_to_main_hides_and_reuses_backend_window(self):
        events = []
        backend = SimpleNamespace(
            winfo_exists=lambda: True,
            geometry=lambda: '1200x800+20+30',
            withdraw=lambda: events.append('backend-hidden'),
        )
        root = SimpleNamespace(
            winfo_exists=lambda: True,
            deiconify=lambda: events.append('main-shown'),
            update_idletasks=lambda: None,
            lift=lambda: None,
            focus_force=lambda: None,
            after=lambda *_args: None,
        )
        window = object.__new__(DpsWindow)
        window.history_window = backend
        window.history_filter_after_id = None
        window.pvp_alliance_page = None
        window.backend_pages = {'settings': object()}
        window.config = {}
        window.restore_geometry = {}
        window.tray_history_hidden = False
        window.closing = False
        window.authorization_resetting = False
        window.compact_mode = False
        window.window_locked = False
        window.root = root
        window._cancel_toggle_hotkey_capture = mock.Mock()
        window._flush_window_geometry = mock.Mock()
        window._apply_window_dpi_if_changed = mock.Mock()
        window._apply_windows_style = mock.Mock()
        window._apply_main_transparency = mock.Mock()
        window._apply_window_lock_state = mock.Mock()
        with mock.patch.object(
            DpsWindow._close_history_window.__globals__['ModernDropdown'],
            'close_active_popup',
        ), mock.patch.dict(
            DpsWindow._close_history_window.__globals__,
            save_config=mock.Mock(),
        ):
            window._close_history_window()

        self.assertEqual(events, ['backend-hidden', 'main-shown'])
        self.assertIs(window.history_window, backend)
        self.assertEqual(window.backend_pages, {'settings': mock.ANY})
        self.assertFalse(window.main_window_intentionally_hidden)

    def test_logout_and_application_exit_still_destroy_cached_backend(self):
        source = Path(__file__).with_name('dps_meter.pyw').read_text(
            encoding='utf-8'
        )
        self.assertGreaterEqual(
            source.count('self._close_history_window(destroy=True)'), 2
        )
