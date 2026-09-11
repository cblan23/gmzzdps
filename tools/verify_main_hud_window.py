"""Exercise the real Windows HUD adapter without login, capture or config I/O."""
from __future__ import annotations

import argparse
import runpy
import sys
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import ImageGrab
from main_hud import MainHudRenderer, WindowsLayeredPresenter
from tools.preview_main_hud import COLORS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.codex-tmp' / 'hud-acceptance')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    module = runpy.run_path(str(ROOT / 'dps_meter.pyw'))
    Window = module['DpsWindow']
    desktop = tk.Tk()
    desktop.overrideredirect(True)
    desktop.configure(bg='white')
    desktop.attributes('-topmost', True)
    desktop.geometry('535x630+220+100')
    desktop.update()
    root = tk.Toplevel(desktop)
    root.withdraw()
    root.overrideredirect(True)
    root.geometry('459x470+245+140')
    root.attributes('-topmost', True)
    root.update_idletasks()

    window = object.__new__(Window)
    window.root = root
    window.config = {}
    window.closing = False
    window.window_locked = False
    window.window_alpha = 1.0
    window.window_dpi = 144
    window.main_ui_scale = 1.0
    window.ui_font_size = 14
    window.layered_main_enabled = True
    window.layered_main_renderer = MainHudRenderer(ROOT / 'assets', COLORS)
    window.layered_main_presenter = WindowsLayeredPresenter(root)
    window.layered_main_after_id = None
    window.layered_main_rendering = False
    window.layered_main_hover_action = ''
    window.layered_main_hit_regions = {}
    window.layered_main_press_action = ''
    window.layered_main_press_origin = None
    window.layered_main_dragged = False
    window.drag_state = {}
    window.restore_geometry = {}
    window.window_geometry_after_ids = {}
    window.pending_window_geometry = {}
    window.main_scroll_offset = 0
    window.window_lock_original_styles = {}
    window.unlock_window = None
    window.unlock_button = None
    window.main_time_text = '01:36'
    window.team_dps_text = '3,962,748'
    window.enrage_prediction = None
    window.show_deaths = True
    window.enrage_tooltip = None
    window.enrage_tooltip_canvas = None
    monster = SimpleNamespace(name='测试首领', entity_id=501, current_hp=815_000_000, max_hp=1_260_000_000, observed_max_hp=1_260_000_000)
    window.model = SimpleNamespace(current_monster=lambda: monster)
    rows = [dict(actor_id=i + 1, profession_id=1_200_001 + i % 7, name=f'预览角色{i + 1}', metric='hps' if i % 7 == 1 else 'dps', stat_value=123_450 + i * 42_345, total_value=4_281_910 + i * 782_350, deaths=i % 3, is_self=i == 3) for i in range(10)]
    preview = [False]
    window._main_display_rows = lambda: (rows, preview[0])
    calls = []
    window.show_settings = lambda: calls.append('settings')
    window._request_close = lambda: calls.append('close')

    def toggle_pin():
        current = window._preferred_main_topmost()
        window._set_main_topmost(not current)
        calls.append('pin')

    window.toggle_topmost = toggle_pin

    def toggle_lock():
        window.window_locked = not window.window_locked
        Window._set_window_click_through(window, root, window.window_locked)
        window._render_layered_main_hud()
        if window.window_locked:
            window._show_unlock_window()
        else:
            window._destroy_unlock_window()
        calls.append('lock')

    window.toggle_window_lock = toggle_lock
    for sequence, handler in (('<Motion>', window._layered_main_motion), ('<ButtonPress-1>', window._layered_main_press), ('<B1-Motion>', window._layered_main_drag), ('<ButtonRelease-1>', window._layered_main_release)):
        root.bind(sequence, handler)
    root.deiconify()
    root.update()

    def render():
        window._render_layered_main_hud()
        root.update()
        assert not getattr(window, 'layered_main_last_error', ''), window.layered_main_last_error
        assert window.layered_main_hit_regions

    def shot(name):
        left, top = root.winfo_x(), root.winfo_y()
        ImageGrab.grab(bbox=(left - 16, top - 16, left + root.winfo_width() + 16, top + root.winfo_height() + 16), all_screens=True).save(args.output / name)

    try:
        render()
        before = window.layered_main_last_image.tobytes()
        rows[0]['stat_value'] = 2_631_751
        rows[0]['total_value'] = 93_782_712
        window.main_time_text = '01:37'
        monster.current_hp = 711_000_000
        render()
        assert window.layered_main_last_image.tobytes() != before
        assert window._layered_main_snapshot()['rows'][0]['stat_text'] == '2,631,751/s'
        shot('window-live-data-white.png')

        def click(name):
            l, t, r, b = window.layered_main_hit_regions['action:' + name]
            x, y = (l + r) // 2, (t + b) // 2
            event = SimpleNamespace(x=x, y=y, x_root=root.winfo_x() + x, y_root=root.winfo_y() + y)
            window._layered_main_press(event)
            window._layered_main_release(event)
            root.update()

        click('pvp')
        assert calls == [], calls
        click('settings')
        assert calls == ['settings'], calls
        with mock.patch.object(window, '_clear_main_display') as clear:
            click('clear')
            clear.assert_called_once()
        click('lock')
        render()
        assert window.window_locked and window.unlock_window.winfo_exists()
        box = window.layered_main_hit_regions['action:lock']
        assert abs(window.unlock_window.winfo_x() - (root.winfo_x() + box[0])) <= 1
        assert abs(window.unlock_window.winfo_y() - (root.winfo_y() + box[1])) <= 1
        shot('window-locked-white.png')
        window.unlock_window.event_generate('<Button-1>', x=8, y=8)
        root.update()
        assert not window.window_locked
        assert window.unlock_window is None
        render()

        old_x, old_y = root.winfo_x(), root.winfo_y()
        event = SimpleNamespace(x=3, y=3, x_root=old_x + 3, y_root=old_y + 3)
        window._layered_main_press(event)
        window._layered_main_drag(SimpleNamespace(x_root=old_x + 33, y_root=old_y + 23))
        root.update()
        window._layered_main_release(SimpleNamespace(x=3, y=3))
        assert (root.winfo_x(), root.winfo_y()) == (old_x + 30, old_y + 20)

        full_width, full_height = root.winfo_width(), root.winfo_height()
        l, t, r, b = window.layered_main_hit_regions['resize:height']
        event = SimpleNamespace(x=(l+r)//2, y=(t+b)//2,
            x_root=root.winfo_x()+(l+r)//2, y_root=root.winfo_y()+(t+b)//2)
        row_height = window._main_row_height()
        window._layered_main_press(event)
        shrink_rows = window.layered_main_visible_rows - 3
        window._layered_main_drag(SimpleNamespace(x_root=event.x_root + 50, y_root=event.y_root - shrink_rows * row_height))
        with mock.patch.dict(Window._layered_main_release.__globals__, save_config=mock.Mock()) as values:
            window._layered_main_release(event)
            values['save_config'].assert_called_once_with(window.config)
        render()
        assert window.main_visible_rows == 3
        assert root.winfo_width() == full_width and root.winfo_height() < full_height
        assert (root.winfo_x(), root.winfo_y()) == (old_x + 30, old_y + 20)
        assert len(window.layered_main_actor_regions) == 3 and len(rows) == 10
        shot('window-height-three-rows.png')
        viewport_y = window.layered_main_actor_regions[0][0][1] + 4
        window._scroll_main(SimpleNamespace(delta=-1200, num='??', x=10, y=viewport_y))
        render()
        assert window.layered_main_actor_regions[-1][1] == 10
        shot('window-height-three-rows-scrolled.png')
        window.main_visible_rows = 1
        render()
        assert len(window.layered_main_actor_regions) == 1
        shot('window-height-one-row.png')
        window.main_visible_rows = 12
        window.main_scroll_offset = 0
        render()

        window.window_alpha = 0.45
        window._apply_main_transparency()
        root.update()
        render()
        assert window.window_alpha == 1.0
        assert window.config['alpha'] == 1.0
        shot('window-opacity-forced-100-white.png')
        window.window_alpha = 1.0
        root.minsize(500, 500)  # Old settings helpers must not hold the HUD open.
        window.show_deaths = False
        render()
        assert root.winfo_width() < 459
        window.show_deaths = True
        preview[0] = True
        for i, row in enumerate(rows): row['rating'] = 75900 + i * 510 if i != 2 else None
        render()
        assert window._layered_main_snapshot()['rows'][2]['rating_text'] == '--'
        shot('window-rating-white.png')
        preview[0] = False
        render()
        for dpi in (96, 120, 144, 168, 192):
            window.window_dpi = dpi
            render()
        previous_topmost = window._preferred_main_topmost()
        click('pin')
        assert calls[-1] == 'pin'
        assert window._preferred_main_topmost() != previous_topmost
        assert 'action:close' not in window.layered_main_hit_regions
        print('PASS: real HWND, live values, PVP disabled, settings/pin, lock/unlock, drag, alpha, deaths, rating, DPI 100-200%')
    finally:
        window.closing = True
        window._destroy_unlock_window()
        pending = getattr(window, 'layered_main_after_id', None)
        if pending is not None: root.after_cancel(pending)
        window.layered_main_presenter.close()
        root.destroy()
        desktop.destroy()


if __name__ == '__main__':
    main()
