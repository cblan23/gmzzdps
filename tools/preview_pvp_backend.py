"""Preview the production 竞技战绩 route with isolated fixture records."""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import queue
import runpy
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from combat_history import CombatHistoryStore
from pvp_records import PvpHistoryRepository
from tools.preview_official_settlement_history import (
    initialize_official_history_window,
)


ACCOUNT_KEY = "pvp-native-preview"


def skill(skill_id: int, damage: int, stamp: int) -> dict:
    return {
        "skill_id": skill_id,
        "damage": damage,
        "hits": 3,
        "casts": 2,
        "critical_hits": 1,
        "max_hit": int(damage * 0.48),
        "hit_timestamps_ns": [stamp, stamp + 2_000_000_000, stamp + 4_000_000_000],
        "cast_timestamps_ns": [stamp - 500_000_000, stamp + 1_500_000_000],
    }


def equipment_snapshot(rating: int, seed: int) -> dict:
    slot_names = {
        1: "武器",
        2: "胸针",
        3: "指环",
        4: "护符",
        5: "护甲",
        6: "鞋靴",
        7: "帽子",
        8: "披风",
    }
    item_ids = (
        3_060_643,
        3_210_603,
        3_060_623,
        3_210_623,
        3_060_663,
        3_210_643,
        3_060_683,
        3_210_683,
    )
    local_exact = seed < 20
    captured_at_ns = 1_790_000_000_000_000_000 + seed * 1_000_000
    equipment = []
    for slot, item_id in enumerate(item_ids, 1):
        quality = 6 if slot in {1, 5, 6} else 7
        base_score = 2_910 + slot * 37
        affixes = [
            {
                "word_id": 3_930_100 + slot * 10 + index,
                "name": name,
                "property_key": key,
                "property_value": value,
                "score": score,
            }
            for index, (name, key, value, score) in enumerate(
                (
                    ("攻击力", "Atk_N", 332 + slot, 870),
                    ("暴击率", "Crit_Rate", round(2.1 + slot * 0.1, 1), 675),
                    ("对玩家伤害", "PvP_Damage", round(3.8 + slot * 0.1, 1), 301),
                )
            )
        ]
        special = {
            "auxiliary_id": 100 + slot,
            "name": "竞技专精" if slot <= 5 else "冒险增幅",
            "score": 1_200,
            "properties": [
                {
                    "key": "PvP_Damage" if slot <= 5 else "Atk_N",
                    "name": "对玩家伤害" if slot <= 5 else "攻击力",
                    "value": round(4.2 + slot * 0.1, 1) if slot <= 5 else 300 + slot,
                }
            ],
        }
        random_score = sum(row["score"] for row in affixes) + special["score"]
        enhance_score = 320 + slot * 16 if local_exact else None
        total_score = (
            base_score + random_score + enhance_score
            if enhance_score is not None
            else None
        )
        equipment.append(
            {
                "slot": slot,
                "slot_name": slot_names[slot],
                "item_id": item_id,
                "item_score": total_score or base_score + random_score,
                "base_score": base_score,
                "random_score": random_score,
                "known_score": base_score + random_score,
                "total_score": total_score,
                "score_complete": total_score is not None,
                "score_source": (
                    "local_equipment_model" if local_exact else "wire_base_random_subtotal"
                ),
                "enhance_score": enhance_score,
                "enhance_level": 4 + slot % 2 if local_exact else None,
                "enhance_level_score": 400 if local_exact else None,
                "enhance_level_remaining": (
                    max(0, 400 - enhance_score) if enhance_score is not None else None
                ),
                "enhance_level_progress_percent": 80 if local_exact else None,
                "enhance_overall_percent": 50 + slot * 4 if local_exact else None,
                "next_enhance_level": 6 if local_exact else None,
                "next_enhance_score": 480 if local_exact else None,
                "next_enhance_increment": 80 if local_exact else None,
                "quality": quality,
                "quality_name": "黄色品质" if quality == 6 else "红色品质",
                "word_ids": [row["word_id"] for row in affixes],
                "word_scores": [row["score"] for row in affixes],
                "affixes": affixes,
                "special_affix": special,
                "score_breakdown_total": random_score,
                "score_breakdown_complete": True,
                "score_breakdown_matches": True,
                "details": {
                    "quality": quality,
                    "source": "RetOtherRoleShapeData",
                    "slot": slot_names[slot],
                },
                "metadata": {
                    "icon": str(item_id),
                    "quality": quality,
                },
                "equipment_mode": "pvp" if slot <= 5 else "adventure",
                "is_pvp": slot <= 5,
                "active_word_count": 3,
                "total_word_count": 3,
            }
        )
    equipment_score = sum(
        int(row.get("total_score") or 0) for row in equipment
    ) or None
    equipment_known_score = sum(int(row["known_score"]) for row in equipment)
    return {
        "captured_at": "2026-09-20 20:30:00",
        "captured_at_ns": captured_at_ns,
        "extraordinary_rating": rating,
        "equipment_score": equipment_score,
        "equipment_known_score": equipment_known_score,
        "equipment_score_complete": equipment_score is not None,
        "equipment_score_source": (
            "local_equipment_model" if local_exact else "wire_base_random_subtotal"
        ),
        "equipment": equipment,
        "affixes": [
            {"name": "竞技增伤", "score": 82 + seed},
            {"name": "控制抗性", "score": 76 + seed},
        ],
        "pvp_equipment_count": 5,
        "active_word_count": 8,
        "total_word_count": 8,
        "source": "RetOtherRoleShapeData",
        "partial": not local_exact,
    }


def fixture(team_size: int, ordinal: int) -> dict:
    now_ns = time.time_ns() - ordinal * 3_600_000_000_000
    duration_ns = (95 + ordinal * 7) * 1_000_000_000
    started_ns = now_ns - duration_ns
    allies = []
    enemies = []
    for index in range(team_size):
        ally_rating = 89_532 - index * 317
        enemy_rating = 91_248 - index * 241
        allies.append(
            {
                "character_id": f"ally-{team_size}-{index}",
                "name": "莫雪" if index == 0 else f"星海队友{index}",
                "profession_id": 1_200_001 + index % 8,
                "level": 80,
                "extraordinary_rating": ally_rating,
                "avatar_id": 4_270_019 if index == 0 else 4_270_001 + index,
                "avatar_frame_id": 4_271_009 if index == 0 else 4_271_000,
                "is_self": index == 0,
                "kills": max(0, 7 - index),
                "assists": 3 + index,
                "deaths": index % 3,
                "damage": 4_860_000 - index * 183_000,
                "healing": 920_000 + index * 42_000,
                "taken": 2_340_000 + index * 91_000,
                "current_dead": index == team_size - 1 and team_size > 1,
                "skills": [
                    skill(10_100 + index, 2_360_000 - index * 71_000, started_ns + 4_000_000_000),
                    skill(10_200 + index, 1_480_000 - index * 43_000, started_ns + 8_000_000_000),
                ],
                "equipment_snapshot": equipment_snapshot(ally_rating, index),
            }
        )
        enemies.append(
            {
                "character_id": f"enemy-{team_size}-{index}",
                "name": f"霜火敌手{index + 1}",
                "profession_id": 1_200_008 - index % 8,
                "level": 80,
                "extraordinary_rating": enemy_rating,
                "avatar_id": 4_270_045 + index,
                "avatar_frame_id": 4_271_002,
                "kills": max(0, 6 - index),
                "assists": 2 + index,
                "deaths": (index + 1) % 3,
                "damage": 4_520_000 - index * 161_000,
                "healing": 780_000 + index * 31_000,
                "taken": 2_510_000 + index * 86_000,
                "current_dead": index == 1,
                "skills": [
                    skill(20_100 + index, 2_080_000 - index * 61_000, started_ns + 5_000_000_000),
                    skill(20_200 + index, 1_210_000 - index * 39_000, started_ns + 9_000_000_000),
                ],
                "equipment_snapshot": equipment_snapshot(enemy_rating, 20 + index),
            }
        )
    local = allies[0]
    opponent = enemies[0]
    ally_skill_ids = (
        (86_061_010, 86_060_040)
        if team_size == 1
        else (10_100, 10_200)
    )
    enemy_skill_ids = (
        (86_021_070, 86_021_010)
        if team_size == 1
        else (20_100, 20_200)
    )
    allies[0]["skills"] = [
        skill(skill_id, damage, started_ns + stamp)
        for skill_id, damage, stamp in (
            (ally_skill_ids[0], 2_360_000, 4_000_000_000),
            (ally_skill_ids[1], 1_480_000, 8_000_000_000),
        )
    ]
    enemies[0]["skills"] = [
        skill(skill_id, damage, started_ns + stamp)
        for skill_id, damage, stamp in (
            (enemy_skill_ids[0], 2_080_000, 5_000_000_000),
            (enemy_skill_ids[1], 1_210_000, 9_000_000_000),
        )
    ]
    opponents = [
        {
            "opponent_id": opponent["character_id"],
            "character_id": opponent["character_id"],
            "name": opponent["name"],
            "profession_id": opponent["profession_id"],
            "level": opponent["level"],
            "extraordinary_rating": opponent["extraordinary_rating"],
            "avatar_id": opponent["avatar_id"],
            "avatar_frame_id": opponent["avatar_frame_id"],
            "damage_to": 1_760_000,
            "damage_from": 1_340_000,
            "kills_on": 2,
            "deaths_to": 1,
            "skills_outgoing": [
                skill(enemy_skill_ids[0], 1_090_000, started_ns + 7_000_000_000),
                skill(enemy_skill_ids[1], 670_000, started_ns + 14_000_000_000),
            ],
            "skills_incoming": [
                skill(ally_skill_ids[0], 820_000, started_ns + 6_000_000_000),
                skill(ally_skill_ids[1], 520_000, started_ns + 13_000_000_000),
            ],
            "equipment_snapshot": deepcopy(opponent["equipment_snapshot"]),
        }
    ]
    mode_name = (
        "双人切磋" if team_size == 1
        else "主宰争锋" if team_size == 3
        else "神座之争" if team_size == 6
        else "命运时刻"
    )
    map_name = (
        "勇者对决" if team_size == 1
        else "梅园场景" if team_size == 3
        else "神座竞技场" if team_size == 6
        else "苍穹战场"
    )
    return {
        "schema_version": 1,
        "match_id": f"preview-{team_size}v{team_size}-{ordinal}",
        "battle_id": f"preview-{team_size}v{team_size}-{ordinal}",
        "map_id": 5200280 if team_size == 1 else 5208017 if team_size == 3 else 5208004 if team_size == 6 else 5203003,
        "mode_id": 0 if team_size == 1 else 5_500_002,
        "mode_name": mode_name,
        "map_name": map_name,
        "match_kind": "duel" if team_size == 1 else "arena",
        "team_size": team_size,
        "started_at_ns": started_ns,
        "ended_at_ns": now_ns,
        "duration_seconds": duration_ns / 1e9,
        "result": ("胜利", "失败", "未知", "胜利")[ordinal % 4],
        "end_reason": "match_result",
        "player": {
            "character_id": local["character_id"],
            "name": local["name"],
            "profession_id": local["profession_id"],
            "level": local["level"],
            "extraordinary_rating": local["extraordinary_rating"],
            "avatar_id": local["avatar_id"],
            "avatar_frame_id": local["avatar_frame_id"],
            "equipment_snapshot": deepcopy(local["equipment_snapshot"]),
        },
        "kills": local["kills"],
        "assists": 0 if team_size == 1 else local["assists"],
        "deaths": local["deaths"],
        "damage": local["damage"],
        "damage_done": local["damage"],
        "taken": local["taken"],
        "damage_taken": local["taken"],
        "capture_complete": True,
        "incoming_source_available": True,
        "allies": allies,
        "enemies": enemies,
        "opponents": opponents,
    }


def hunter_fixture(ordinal: int) -> dict:
    """Create one realistic 终末猎杀 pair for the native detail preview."""

    payload = fixture(1, ordinal)
    payload.update(
        map_id=5_200_167,
        mode_id=5_500_012,
        mode_name="终末猎杀",
        map_name="猎龙之城",
        match_kind="hunter_city",
        team_size=None,
        result="未知",
        end_reason="left_map",
        assists=None,
    )
    opponent = payload["opponents"][0]
    enemy = payload["enemies"][0]
    stable_id = "hunter-opponent-001"
    opponent.update(
        opponent_id=stable_id,
        character_id=stable_id,
        name="赤焰猎手",
        extraordinary_rating=92_418,
        damage=1_260_000 + ordinal * 115_000,
        taken=740_000 + ordinal * 42_000,
        damage_to=1_260_000 + ordinal * 115_000,
        damage_from=740_000 + ordinal * 42_000,
        kills=1 if ordinal in {0} else 0,
        defeats=1 if ordinal in {0, 1} else 0,
        kills_on=1 if ordinal in {0} else 0,
        deaths_to=1 if ordinal in {0, 1} else 0,
        assists=None,
        damage_share=0.46,
        timeline=(
            [
                {"type": "kill", "timestamp_ns": payload["started_at_ns"] + 31_000_000_000},
                {"type": "death", "timestamp_ns": payload["started_at_ns"] + 48_000_000_000},
            ]
            if ordinal == 0
            else [
                {"type": "death", "timestamp_ns": payload["started_at_ns"] + 48_000_000_000}
            ]
        ),
    )
    opponent["skills_outgoing"] = [
        skill(86_021_070, 760_000 + ordinal * 40_000, payload["started_at_ns"] + 7_000_000_000),
        skill(86_021_010, 500_000 + ordinal * 28_000, payload["started_at_ns"] + 18_000_000_000),
    ]
    opponent["skills_incoming"] = [
        skill(86_061_010, 820_000 + ordinal * 31_000, payload["started_at_ns"] + 6_000_000_000),
        skill(86_060_040, 440_000 + ordinal * 21_000, payload["started_at_ns"] + 16_000_000_000),
    ]
    enemy.update(
        character_id=stable_id,
        name="赤焰猎手",
        extraordinary_rating=92_418,
        kills=None,
        assists=None,
        deaths=None,
        equipment_snapshot=deepcopy(opponent["equipment_snapshot"]),
    )
    payload["allies"][0].update(
        kills=opponent["defeats"],
        assists=None,
        deaths=opponent["kills"],
        damage=opponent["damage"],
        taken=opponent["taken"],
    )
    payload["kills"] = opponent["defeats"]
    payload["deaths"] = opponent["kills"]
    payload["assists"] = None
    payload["damage"] = opponent["damage"]
    payload["taken"] = opponent["taken"]
    return payload


def capture(window: tk.Toplevel, destination: Path) -> None:
    from PIL import ImageGrab

    window.deiconify()
    window.attributes("-topmost", True)
    window.lift()
    window.focus_force()
    window.update_idletasks()
    if window.winfo_width() < 100 or window.winfo_height() < 100:
        raise RuntimeError(
            f"Preview window is not mapped: {window.winfo_geometry()}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(
        bbox=(
            window.winfo_rootx(),
            window.winfo_rooty(),
            window.winfo_rootx() + window.winfo_width(),
            window.winfo_rooty() + window.winfo_height(),
        ),
        all_screens=True,
    ).save(destination)
    print(destination, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--close-after", type=float, default=0.0)
    parser.add_argument(
        "--server-history",
        action="store_true",
        help="render fixture server matchup data in the PvP snapshot",
    )
    parser.add_argument(
        "--hunter",
        action="store_true",
        help="render the 终末猎杀 opponent-focused detail preview",
    )
    args = parser.parse_args()

    app = runpy.run_path(
        str(ROOT / "dps_meter.pyw"), run_name="pvp_native_history_preview"
    )
    app["enable_windows_dpi_awareness"]()
    temporary = tempfile.TemporaryDirectory(prefix="gmzz-pvp-native-preview-")
    temp_root = Path(temporary.name)
    pve_store = CombatHistoryStore(
        temp_root / "pve",
        catalog_path=app["ASSET_DIR"] / "bosses" / "boss_icon_sources.json",
        profession_path=app["SKILL_METADATA_PATH"],
    )
    pvp_store = PvpHistoryRepository(temp_root / "pvp-history.sqlite3")
    payloads = (
        [hunter_fixture(index) for index in range(3)]
        if args.hunter
        else [fixture(size, index) for index, size in enumerate((1, 3, 6, 12))]
    )
    for payload in payloads:
        pvp_store.save(ACCOUNT_KEY, payload, upload_state="local_only")

    root = tk.Tk()
    root.title("竞技战绩原生页面预览宿主")
    root.geometry("1x1+8+8")
    root.overrideredirect(True)
    root.update_idletasks()
    host = initialize_official_history_window(
        app,
        root,
        pve_store,
        enabled_pages=("history", "kings"),
    )
    host.backend_current_page = "kings"
    host.history_data_domain = "pve"
    host.pvp_history_repository = pvp_store
    host.pvp_recording = SimpleNamespace(account_key=ACCOUNT_KEY)
    host.control_messages = queue.Queue()
    if args.server_history:
        def fixture_matchups(_action, payload):
            opponent_ids = payload.get("opponent_ids", []) if isinstance(payload, dict) else []
            return {
                "ok": True,
                "matchups": {
                    str(uid): {
                        "total": 7,
                        "wins": 4,
                        "losses": 2,
                        "unknown": 1,
                        "win_rate": 0.6667,
                        "recent": ["胜", "负", "胜", "胜", "未"],
                    }
                    for uid in opponent_ids
                },
            }
        host.licensing.pvp_request = fixture_matchups
    host._build_history_window()
    preview = host.history_window
    if preview is None:
        raise RuntimeError("Production backend window was not created")
    root.withdraw()
    preview.deiconify()
    preview.lift()
    selected_id = (
        payloads[-1]["match_id"]
        if args.hunter
        else next(
            payload["match_id"]
            for payload in payloads
            if payload["team_size"] == 1
        )
    )

    def drain_control_messages() -> None:
        while True:
            try:
                kind, payload = host.control_messages.get_nowait()
            except queue.Empty:
                break
            host._dispatch_message(kind, payload)
        if preview.winfo_exists():
            preview.after(50, drain_control_messages)

    preview.after(50, drain_control_messages)

    def overview() -> None:
        preview.update_idletasks()
        if args.screenshots:
            capture(preview, args.screenshots / "pvp-native-history.png")
        host.history_selected_id = selected_id
        host.history_loaded_record = None
        host._open_selected_history_detail()
        preview.after(700, detail)

    def detail() -> None:
        if args.screenshots:
            capture(preview, args.screenshots / "pvp-native-detail.png")
        if args.hunter:
            preview.after(500, hunter_tabs)
            return
        record = host._selected_history_record()
        enemies = [
            row
            for row in (record or {}).get("participants", [])
            if isinstance(row, dict) and row.get("side") == "enemy"
        ]
        if enemies:
            host.history_selected_actor = int(enemies[0].get("actor_id", 0) or 0)
            host._render_history_selection()
        canvas = getattr(host, "history_page_canvas", None)
        if canvas is not None:
            canvas.yview_moveto(1.0)
        preview.after(700, skills)

    def hunter_tabs() -> None:
        page = getattr(host, "history_hunter_detail_page", None)
        if page is None:
            raise RuntimeError("Hunter detail page was not created")
        if args.screenshots:
            page.choose_tab("技能分布")
            preview.update_idletasks()
            capture(preview, args.screenshots / "pvp-hunter-detail-skills.png")
            page.choose_tab("装备快照")
            preview.update_idletasks()
            capture(preview, args.screenshots / "pvp-hunter-detail-equipment.png")
            page.choose_tab("历史交手")
            preview.update_idletasks()
            capture(preview, args.screenshots / "pvp-hunter-detail-history.png")
        print("PVP_HUNTER_DETAIL_PREVIEW_READY", flush=True)
        root.after(250, root.destroy)

    def skills() -> None:
        if args.screenshots:
            capture(preview, args.screenshots / "pvp-native-detail-skills.png")
            host._set_pvp_duel_analysis_tab("equipment")
            preview.after(500, equipment)
        else:
            print("PVP_NATIVE_HISTORY_PREVIEW_READY", flush=True)

    def equipment() -> None:
        capture(preview, args.screenshots / "pvp-native-detail-equipment.png")
        players = host._pvp_duel_participants()
        opponent = next(
            (
                row
                for row in players
                if host._pvp_duel_player_side(row) == "enemy"
            ),
            None,
        )
        if opponent is not None:
            host.history_duel_equipment_selected_player = (
                host._pvp_duel_player_key(opponent, players.index(opponent))
            )
            host.history_duel_selected_side = "enemy"
            host.history_duel_equipment_selected_index = 0
            host._draw_pvp_duel_equipment_roster()
            host._draw_pvp_duel_player_header()
            host._draw_pvp_duel_analysis()
            preview.update_idletasks()
            capture(
                preview,
                args.screenshots / "pvp-native-detail-equipment-opponent.png",
            )
            host.history_duel_body_canvas.yview_moveto(1.0)
            preview.update_idletasks()
            capture(
                preview,
                args.screenshots / "pvp-native-detail-equipment-opponent-bottom.png",
            )
        print("PVP_NATIVE_HISTORY_PREVIEW_READY", flush=True)
        root.after(250, root.destroy)

    preview.after(1_200, overview)
    if args.close_after > 0:
        preview.after(int(args.close_after * 1000), root.destroy)
    try:
        root.mainloop()
    finally:
        temporary.cleanup()


if __name__ == "__main__":
    main()
