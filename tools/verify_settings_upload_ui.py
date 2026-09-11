"""Local settings/upload layout checks; never sends an upload or signs in."""
import runpy
import sys
import time
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import ImageGrab


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def main():
    app = runpy.run_path(str(ROOT / 'dps_meter.pyw'))
    DpsWindow = app['DpsWindow']
    root = tk.Tk()
    root.title('设置与上传弹窗 · 离线检查')
    root.attributes('-topmost', True)
    root.geometry('1420x980+180+30')
    root.update()
    output = ROOT / '.codex-tmp' / 'hud-acceptance'
    output.mkdir(parents=True, exist_ok=True)
    try:
        for dpi, size in ((96, 14), (144, 18), (192, 18)):
            root.tk.call('tk', 'scaling', dpi / 72)
            window = object.__new__(DpsWindow)
            window.root = root
            window.history_window = root
            window.config = {'font_size': size}
            window.window_dpi = dpi
            window.dpi_scale = dpi / 96
            window.main_ui_scale = 1.0
            window.window_alpha = 1.0
            window.main_row_mask_opacity = 0
            window.hide_names = False
            window.team_rating_preview_enabled = True
            window.profession_display_metrics = app['normalize_profession_display_metrics']({})
            window.toggle_visibility_hotkey_enabled = True
            window.closing = False
            for key in ('show_deaths', 'show_main_totals', 'show_team_dps', 'highlight_self',
                        'show_combat_time', 'show_pvp_button', 'show_boss_hp_bar', 'boss_enrage_prediction_enabled'):
                setattr(window, key, True)
            window._initialize_ui_fonts()
            window.icons = app['IconFactory'](root)
            window.icons.set_dpi(dpi)
            window._sync_toggle_hotkey_controls = lambda: None
            window._apply_live_ui_settings = lambda *_args: None
            window._preview_font_size = lambda *_args: None
            window._preview_main_ui_scale = lambda *_args: None
            window._preview_row_mask_opacity = lambda *_args: None
            host = tk.Frame(root)
            host.pack(fill='both', expand=True)
            page = window._build_backend_settings_page(host)
            page.pack(fill='both', expand=True)
            root.update()
            root.lift()
            root.focus_force()
            time.sleep(0.25)
            labels = [str(widget.cget('text')) for widget in descendants(page) if isinstance(widget, tk.Label)]
            for title in ('常用显示', '职业显示指标', '首领信息', '外观与缩放', '快捷操作'):
                assert title in labels, title
            for text in ('通用设置', '统计展示设置', '主窗口透明度', '目标 Boss 资料识别',
                         '显示非凡评分（旧版）', 'DPS颜色条保持不透明（旧版）'):
                assert text not in labels, text
            assert not window.backend_settings_buttons
            selectors = [widget for widget in descendants(page) if isinstance(widget, app['ModernDropdown'])]
            assert {selector.choices for selector in selectors} == {('HPS', 'DPS'), ('DPS', 'DT')}
            assert window.settings_audience_metric_var.get() == 'HPS'
            assert window.settings_warrior_metric_var.get() == 'DPS'
            ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx()+root.winfo_width(), root.winfo_rooty()+root.winfo_height()), all_screens=True).save(output / f'settings-{dpi}-{size}.png')
            page.destroy()
            window.backend_page_host = host
            window.history_page_root = host
            window.history_modal_overlay = None
            window.history_upload_in_progress = set()
            window.upload_public_mode = 'anonymous'
            record = {'battle_id': 'offline', 'participants': []}
            window._history_record_for_upload = lambda _battle: record
            window._build_history_upload_payload = lambda _battle: (record, {}, '预览玩家')
            window._history_upload_display_summary = lambda *_args: {}
            window._history_encounter_title = lambda _record: '测试首领'
            # Rendering only: no actual upload callback is invoked.
            with mock.patch.dict(DpsWindow._show_history_upload_confirmation.__globals__,
                                 {'encounter_upload_rejection_code': lambda _record: ''}):
                window._show_history_upload_confirmation('offline')
            root.update()
            root.lift()
            time.sleep(0.25)
            buttons = [widget for widget in descendants(host) if isinstance(widget, tk.Label)
                       and widget.cget('text') in ('确认上传', '取消')]
            assert len(buttons) == 2
            shell = window.history_modal_layout[0]
            for button in buttons:
                font = tkfont.Font(root=root, font=button.cget('font'))
                minimum = font.metrics('linespace') + 2 * int(button.cget('pady'))
                assert button.winfo_height() >= minimum, (dpi, size, button.cget('text'), button.winfo_height(), minimum)
                assert button.winfo_rooty() + button.winfo_height() <= shell.winfo_rooty() + shell.winfo_height()
            ImageGrab.grab(bbox=(shell.winfo_rootx(), shell.winfo_rooty(), shell.winfo_rootx()+shell.winfo_width(), shell.winfo_rooty()+shell.winfo_height()), all_screens=True).save(output / f'upload-{dpi}-{size}.png')
            window._close_history_modal()
            host.destroy()
        print('PASS: single-page settings and upload footer at 100/150/200% DPI, 14/18 font sizes')
    finally:
        root.destroy()


if __name__ == '__main__':
    main()
