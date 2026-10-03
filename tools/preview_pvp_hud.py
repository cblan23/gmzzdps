"""Render the PVP HUD with explicit preview-only data."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main_hud import MainHudRenderer
from tools.preview_main_hud import COLORS


def preview_snapshot(view="live") -> dict[str, object]:
    snapshot = {
        "combat_mode": "pvp",
        "pvp_hud_view": "live" if view == "dragon" else view,
        "time": "08:42",
        "pvp_player_name": "莫雪",
        "pvp_rating": "80616",
        "pvp_profession_id": 1200004,
        "pvp_map_name": "诸王战纪",
        "pvp_in_map": True,
        "pvp_teammates": [
            {"name": "墨爵", "profession_id": 1200002},
            {"name": "碎星", "profession_id": 1200003},
        ],
        "pvp_teammate_offset": 0,
        "pvp_kills": 2,
        # Assists are deliberately unknown until a reliable source is wired.
        "pvp_assists": "--",
        "pvp_deaths": 1,
        "pvp_outgoing": [
            {
                "actor_id": 101,
                "name": "逐风",
                "profession_id": 1200007,
                "rating": "79,840",
                "kills": 1,
                "assists": "--",
                "damage_text": "328.6万",
                "share_text": "38.1%",
            },
            {
                "actor_id": 102,
                "name": "墨爵",
                "profession_id": 1200002,
                "rating": "88,893",
                "kills": 1,
                "assists": "--",
                "damage_text": "247.3万",
                "share_text": "28.7%",
            },
            {
                "actor_id": 103,
                "name": "碎星",
                "profession_id": 1200003,
                "rating": "82,410",
                "kills": 0,
                "assists": "--",
                "damage_text": "186.9万",
                "share_text": "21.7%",
            },
            {
                "actor_id": 104,
                "name": "临渊",
                "profession_id": 1200006,
                "rating": "77,520",
                "kills": 0,
                "assists": "--",
                "damage_text": "99.6万",
                "share_text": "11.5%",
            },
        ],
        "pvp_incoming": [
            {
                "actor_id": 201,
                "name": "折光",
                "profession_id": 1200001,
                "rating": "84,270",
                "defeats": 1,
                "damage_text": "214.8万",
                "share_text": "46.4%",
            },
            {
                "actor_id": 202,
                "name": "逐风",
                "profession_id": 1200007,
                "rating": "79,840",
                "defeats": 0,
                "damage_text": "156.2万",
                "share_text": "33.7%",
            },
            {
                "actor_id": 203,
                "name": "临渊",
                "profession_id": 1200006,
                "rating": "77,520",
                "defeats": 0,
                "damage_text": "92.1万",
                "share_text": "19.9%",
            },
            {
                "actor_id": 204,
                "name": "夜行",
                "profession_id": 1200005,
                "rating": "76,820",
                "defeats": 0,
                "damage_text": "67.1万",
                "share_text": "14.5%",
            },
        ],
        "pvp_average_kill_rating": "84367",
        "pvp_team_average_rating": "84,906",
        "pvp_total_damage": "862.4万",
        "pvp_total_taken": "463.1万",
        "pvp_status": "预览数据 · 统计中",
        "pvp_result": "进行中",
        "pvp_active": True,
        "admin_elevated": True,
        "topmost": True,
        "locked": False,
        "hover_action": "",
        "settings_disabled": False,
        "interaction_blocked": False,
    }
    if view == "team":
        snapshot.update(
            pvp_team_rows=[
                {
                    "actor_id": index + 1,
                    "name": name,
                    "profession_id": profession,
                    "rating": rating,
                    "is_self": index == 0,
                    "equipment_profile_ready": True,
                    "equipment_count": 8,
                    "pvp_equipment_count": pvp_count,
                    "active_word_count": beans,
                    "total_word_count": 10,
                }
                for index, (name, profession, rating, pvp_count, beans) in enumerate(
                    (
                        ("莫雪", 1200004, "80,616", 8, 4),
                        ("墨爵", 1200002, "88,893", 6, 3),
                        ("碎星", 1200003, "82,410", 8, 5),
                        ("临渊", 1200006, "87,705", 7, 2),
                        ("逐风", 1200007, "84,905", 8, 4),
                        ("夜行", 1200005, "89,001", 5, 3),
                    )
                )
            ],
        )
    elif view == "dragon":
        snapshot.update(
            pvp_self_only=True,
            pvp_map_name="终末猎杀",
            pvp_kills=1,
            pvp_deaths=2,
            pvp_total_damage="391.2万",
            pvp_total_taken="84.7万",
            pvp_monsters=[
                {"name": "战争巨龙", "damage_to": 391_200, "damage_from": 84_700,
                 "defeated": True},
            ],
        )
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".codex-tmp" / "pvp-hud-preview.png",
    )
    parser.add_argument("--view", choices=("live", "team", "dragon"), default="live")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    renderer = MainHudRenderer(ROOT / "assets", COLORS)
    frame = renderer.render(preview_snapshot(args.view), pixel_scale=1.5).image
    alpha_output = args.output.with_name(args.output.stem + "-alpha.png")
    frame.save(alpha_output)

    margin = 28
    board = Image.new(
        "RGBA",
        (frame.width + margin * 2, frame.height + margin * 2 + 34),
        "#d8dee5",
    )
    draw = ImageDraw.Draw(board)
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 14)
    draw.text((margin, 10), "PVP 模式 · 布局预览（演示数据）", font=font, fill="#24313d")
    board.alpha_composite(frame, (margin, margin + 24))
    board.convert("RGB").save(args.output)
    print(args.output)


if __name__ == "__main__":
    main()
