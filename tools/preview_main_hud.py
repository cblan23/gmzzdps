"""Offline visual acceptance: no game hooks, card login or production writes.

Usage: py -3 tools/preview_main_hud.py --output .codex-tmp/hud-acceptance
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main_hud import MainHudRenderer
from main_hud_artwork import REFERENCE_BOUNDS


COLORS = {1200001: '#f2cd32', 1200002: '#7ecfa5', 1200003: '#5869c4', 1200004: '#6687c5', 1200005: '#68b6e5', 1200006: '#ee8c2f', 1200007: '#a255c7'}


def reference_fixture():
    # Explicit visual-test values from the supplied design, never live data.
    examples = [
        ('失眠喵', 1200007, 'dps', '1,048,208/s', '(1782万)', 0),
        ('繁英', 1200002, 'hps', '1,129,211/s', '(1315万)', 0),
        ('不死川玄弥', 1200004, 'dps', '1,139,360/s', '(1288万)', 1),
        ('灌灌爱喝星…', 1200002, 'hps', '1,539,077/s', '(847万)', 1),
        ('Ccccccccccc', 1200006, 'dps', '1,457,749/s', '(721万)', 0),
        ('杰森Nice', 1200003, 'dps', '874,778/s', '(704万)', 0),
        ('YamiNeko', 1200002, 'hps', '333,764/s', '(577万)', 2),
        ('咕咕嘎嘎企鹅', 1200005, 'dps', '707,379/s', '(481万)', 3),
        ('斩心琉璃', 1200001, 'dps', '222,202/s', '(334万)', 1),
        ('吃饱了不会饿', 1200007, 'dps', '518,369/s', '(225万)', 4),
    ]
    return {
        'time': '00:19', 'boss_name': '泰南拉斯', 'boss_hp': '14.4亿 / 27.1亿',
        'boss_percent': '53.2%', 'boss_ratio': 0.532,
        'prediction': {'state': 'danger', 'message': '预计延后12s (剩余3.2%)', 'marker': 0.84},
        'team_dps': '8,970,096', 'show_deaths': True, 'locked': True,
        'rows': [dict(actor_id=i + 1, name=name, profession_id=pid, metric=metric, stat_text=rate, total_text=total, deaths=deaths, is_self=i == 4)
                 for i, (name, pid, metric, rate, total, deaths) in enumerate(examples)],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, default=ROOT / '.codex-tmp' / 'hud-acceptance')
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    renderer = MainHudRenderer(ROOT / 'assets', COLORS)
    source = Image.open(ROOT / 'assets' / 'main_hud_reference.png').convert('RGBA').crop(REFERENCE_BOUNDS)
    base = reference_fixture()
    for dpi in (100, 125, 150, 175, 200):
        frame = renderer.render(base, pixel_scale=dpi / 100).image
        frame.save(args.output / f'hud-{dpi}-alpha.png')
        for bg in ('white', '#12232e'):
            color_name = 'white' if bg == 'white' else 'dark'
            reference = source.resize(frame.size, Image.Resampling.LANCZOS)
            board = Image.new('RGBA', (frame.width * 2 + 60, frame.height + 64), bg)
            draw = ImageDraw.Draw(board)
            font = ImageFont.truetype(r'C:\Windows\Fonts\msyh.ttc', 14)
            ink = '#24374b' if bg == 'white' else '#ffffff'
            draw.text((20, 12), f'设计原图 · {dpi}%', font=font, fill=ink)
            draw.text((frame.width + 40, 12), '实际渲染 · 职业图标使用游戏原图', font=font, fill=ink)
            board.alpha_composite(reference, (20, 40))
            board.alpha_composite(frame, (frame.width + 40, 40))
            board.convert('RGB').save(args.output / f'compare-{dpi}-{color_name}.png')
    base['rating_preview'] = True
    for i, row in enumerate(base['rows']):
        row['rating_text'] = str(75900 + 9570 * i) if i != 2 else '--'
        row['is_ai'] = i in (1, 5, 7)
    renderer.render(base, pixel_scale=1.5).image.save(args.output / 'hud-rating.png')
    idle = dict(base, boss_name='暂无目标', boss_available=False, boss_hp='-- / --',
                boss_percent='--', boss_ratio=0, prediction=None, time='00:00')
    idle['rows'] = base['rows'][:6]
    idle_frame = renderer.render(idle, pixel_scale=1.5).image
    idle_board = Image.new('RGBA', (idle_frame.width + 40, idle_frame.height + 40), 'white')
    idle_board.alpha_composite(idle_frame, (20, 20))
    idle_board.save(args.output / 'idle-six-rating.png')
    raid = reference_fixture()
    raid['rows'].extend([
        dict(actor_id=11, name='观众队友甲', profession_id=1200002, metric='hps', stat_text='18,239/s', total_text='(312万)', deaths=0),
        dict(actor_id=12, name='观众队友乙', profession_id=1200002, metric='hps', stat_text='21,597/s', total_text='(345万)', deaths=1),
    ])
    raid['bosses'] = [dict(boss_name='邦尼', boss_hp='1234万 / 1729万', boss_percent='71.4%', boss_ratio=0.714),
                      dict(boss_name='安夏', boss_hp='2519万 / 3759万', boss_percent='67.0%', boss_ratio=0.67)]
    raid_frame = renderer.render(raid, pixel_scale=1.5).image
    raid_board = Image.new('RGBA', (raid_frame.width + 40, raid_frame.height + 40), '#12232e')
    raid_board.alpha_composite(raid_frame, (20, 20))
    raid_board.save(args.output / 'raid-12-dual-boss.png')
    badges = Image.new('RGBA', (7 * 112 + 24, 256), '#ffffff')
    ImageDraw.Draw(badges).rectangle((0, 128, badges.width, 256), fill='#101e2b')
    for i, profession_id in enumerate(COLORS):
        sprite = renderer._profession(profession_id)
        badges.alpha_composite(sprite, (24 + i * 112, 12))
        badges.alpha_composite(sprite, (24 + i * 112, 140))
    badges.save(args.output / 'profession-badges.png')
    print(args.output)


if __name__ == '__main__':
    main()
