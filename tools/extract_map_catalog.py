#!/usr/bin/env python3
"""Export the current C7 client map catalog with gameplay classifications.

The authoritative list is ``LevelMapData`` from the newest KSBC2 cache.  Map,
PVP-mode, and dungeon names are localized by read-only scanning of the running
game's already-loaded Lua localization table.  ``tag.cache`` is deliberately
reported separately: it also contains retired/resource-only map tags and is
not an authoritative list of maps available in the current client config.

Example::

    py -3.14 -X utf8 tools/extract_map_catalog.py

The command writes ``map_catalog.json``, a dated catalog CSV, and a dated asset
tag CSV.  A prior ``map_catalog.json`` can be used as an offline localization
snapshot when the game is not running.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from ksbc2_skill_names import (  # noqa: E402
    KSBC2Reader,
    LiveLocalizationScanner,
    TableRef,
    discover_root_handle,
    latest_default_cache,
)


GAME_PROCESS_NAME = "C7-Win64-Shipping.exe"
CURRENT_EXPECTED_MAP_COUNT = 405

# These are labels for LevelMapData.Type, not invented map names.  Exact PVP
# and DungeonData relationships take precedence over these fallback labels.
LEVEL_TYPE_LABELS: dict[int, tuple[str, str, str]] = {
    0: ("系统", "系统场景", "系统场景"),
    1: ("开放世界", "主城/开放区域", "开放世界"),
    2: ("PVE", "PVE副本", "PVE副本"),
    3: ("PVP", "竞技场", "竞技场"),
    4: ("社交", "俱乐部驻地", "俱乐部驻地"),
    6: ("剧情", "剧情/任务实例", "剧情任务"),
    7: ("活动", "特殊活动", "特殊活动"),
    8: ("探索", "城市/室内/探索区域", "探索区域"),
    10: ("PVP", "PVP", "四方联赛"),
    11: ("活动", "特殊活动", "特殊活动"),
    12: ("PVE", "世界事件", "世界事件"),
    14: ("PVP", "12v12", "12v12"),
    15: ("PVP", "赛事", "众神之巅"),
    16: ("PVP", "赛事", "众神之巅"),
    17: ("活动", "特殊活动", "特殊活动"),
    19: ("PVP", "PVP", "猎城战"),
    20: ("活动", "特殊活动", "特殊活动"),
    21: ("PVP", "PVP", "高原战"),
    22: ("PVP", "6v6", "6v6"),
    23: ("PVP", "PVP", "终末猎杀"),
    24: ("PVP", "自走棋", "自走棋"),
    25: ("活动", "特殊活动", "特殊活动"),
    26: ("PVP", "PVP", "俱乐部宣战"),
    27: ("活动", "特殊活动", "特殊活动"),
    29: ("PVP", "PVP", "霜陨领主"),
    34: ("活动", "特殊活动", "特殊活动"),
    35: ("活动", "特殊活动", "特殊活动"),
    36: ("活动", "特殊活动", "特殊活动"),
    38: ("活动", "特殊活动", "特殊活动"),
    39: ("活动", "特殊活动", "特殊活动"),
    40: ("活动", "特殊活动", "特殊活动"),
}

PVP_MODE_TYPES = {
    "TEAM3V3": "3v3",
    "TEAM6V6": "6v6",
    "TEAM12V12": "12v12",
    "MATCH_TYPE_1V1": "1v1",
    "AUTO_CHESS": "自走棋",
}

# The client assigns the same localized map name (主宰争锋) to all six 3v3
# scenes.  Keep the official name in map_name and derive only the display
# variant from the explicit LevelPathName shipped for each scene.
PVP_RANDOM_SCENE_VARIANTS = {
    5208002: "湖区场景",
    5208003: "雪地场景",
    5208012: "陆地场景",
    5208017: "梅园场景",
    5208018: "教堂场景",
}

CATALOG_FIELDS = (
    "map_id",
    "map_name",
    "map_variant",
    "mode_category",
    "mode_type",
    "mode_name",
    "display_label",
    "level_type_id",
    "level_type_label",
    "level_path_name",
    "level_path",
    "map_ui_id",
    "belong_city_id",
    "pvp_mode_ids",
    "pvp_mode_names",
    "pvp_consts",
    "pvp_player_counts",
    "dungeon_ids",
    "dungeon_names",
    "classification_source",
    "source_status",
    "asset_tag_present",
)


def integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    return int(value)


def localization_id(value: object) -> int | None:
    result = integer(value)
    return result if result is not None and result > 1_000_000 else None


def table_rows(
    reader: KSBC2Reader,
    root: dict[object, object],
    table_name: str,
    *,
    required: bool = True,
) -> dict[int, dict[object, object]]:
    outer_ref = root.get(table_name)
    if not isinstance(outer_ref, TableRef):
        if required:
            raise RuntimeError(f"KSBC2 table missing: {table_name}")
        return {}
    outer = reader.table_dict(outer_ref.handle)
    data_ref = outer.get("data")
    if not isinstance(data_ref, TableRef):
        if required:
            raise RuntimeError(f"KSBC2 table has no data table: {table_name}")
        return {}
    result: dict[int, dict[object, object]] = {}
    for raw_key, row_ref in reader.table_entries(data_ref.handle):
        key = integer(raw_key)
        if key is None or not isinstance(row_ref, TableRef):
            continue
        result[key] = reader.table_dict(row_ref.handle)
    return result


def nested_integer_values(reader: KSBC2Reader, value: object) -> list[int]:
    result: list[int] = []

    def visit(item: object, seen: set[int]) -> None:
        if isinstance(item, TableRef):
            if item.handle in seen:
                return
            seen.add(item.handle)
            for _key, nested in reader.table_entries(item.handle):
                visit(nested, seen)
            return
        parsed = integer(item)
        if parsed is not None:
            result.append(parsed)

    visit(value, set())
    return result


def default_tag_cache(cache_path: Path) -> Path:
    # .../Saved/kscache/14/<cache> -> .../Saved/kscache/tag.cache
    candidate = cache_path.parent.parent / "tag.cache"
    return candidate


def read_asset_map_tags(path: Path) -> set[int]:
    if not path.is_file():
        raise FileNotFoundError(f"tag cache not found: {path}")
    return {
        int(match)
        for match in re.findall(rb"(?<![0-9])52[0-9]{5}(?![0-9])", path.read_bytes())
    }


def load_localization_snapshot(path: Path | None) -> dict[int, str]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    raw = payload.get("localization", payload) if isinstance(payload, dict) else {}
    if not isinstance(raw, dict):
        return {}
    result: dict[int, str] = {}
    for raw_key, raw_value in raw.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            continue
        if isinstance(raw_value, str) and raw_value.strip():
            result[key] = raw_value.strip()
    return result


def scan_localization(
    wanted: set[int],
    *,
    pid: int | None,
    fallback: dict[int, str],
    no_live_scan: bool,
) -> tuple[dict[int, str], dict[str, object]]:
    live_names: dict[int, str] = {}
    ambiguous: dict[int, list[str]] = {}
    live_error = ""
    resolved_pid = pid

    if not no_live_scan:
        try:
            if resolved_pid is None:
                from proc_inspect import find_pid

                resolved_pid = find_pid(GAME_PROCESS_NAME)
            with LiveLocalizationScanner(resolved_pid) as scanner:
                candidates = scanner.scan(wanted)
            for loc_id in sorted(wanted):
                texts = sorted({item.text for item in candidates.get(loc_id, [])})
                if len(texts) == 1:
                    live_names[loc_id] = texts[0]
                elif len(texts) > 1:
                    ambiguous[loc_id] = texts
        except Exception as exc:  # offline fallback is an intentional feature
            live_error = f"{type(exc).__name__}: {exc}"

    names = {key: value for key, value in fallback.items() if key in wanted}
    names.update(live_names)
    unresolved = sorted(wanted - names.keys())
    diagnostics: dict[str, object] = {
        "requested": len(wanted),
        "game_pid": resolved_pid,
        "resolved_live": len(live_names),
        "resolved_from_snapshot": sum(
            key not in live_names and key in fallback for key in wanted
        ),
        "ambiguous": {str(key): value for key, value in sorted(ambiguous.items())},
        "unresolved": unresolved,
        "live_scan_error": live_error,
    }
    return names, diagnostics


def resolve_text(value: object, localization: dict[int, str]) -> str:
    if isinstance(value, str):
        return value.strip()
    loc_id = localization_id(value)
    return localization.get(loc_id, "") if loc_id is not None else ""


def unique(values: Iterable[object]) -> list[object]:
    result: list[object] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def fallback_classification(level_type: int) -> tuple[str, str, str]:
    return LEVEL_TYPE_LABELS.get(
        level_type,
        ("其他", f"未命名地图类型 {level_type}", "其他玩法"),
    )


def pvp_mode_type(constants: list[str]) -> str:
    specific = unique(PVP_MODE_TYPES.get(value, "PVP") for value in constants)
    if len(specific) == 1:
        return str(specific[0])
    non_generic = [value for value in specific if value != "PVP"]
    return " / ".join(non_generic or specific)


def display_map_name(map_id: int, official_name: str) -> tuple[str, str]:
    variant = PVP_RANDOM_SCENE_VARIANTS.get(map_id, "")
    return (variant or official_name, variant)


def build_catalog(
    *,
    reader: KSBC2Reader,
    levels: dict[int, dict[object, object]],
    pvp_modes: dict[int, dict[object, object]],
    dungeons: dict[int, dict[object, object]],
    pvp_random: dict[int, dict[object, object]],
    regular_pvp_random: dict[int, dict[object, object]],
    match_pools: dict[int, dict[object, object]],
    gta_maps: dict[int, dict[object, object]],
    world_bosses: dict[int, dict[object, object]],
    localization: dict[int, str],
    asset_tags: set[int],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    scene_modes: dict[int, list[int]] = defaultdict(list)
    scene_mode_sources: dict[int, list[str]] = defaultdict(list)
    for mode_id, row in pvp_modes.items():
        scene_id = integer(row.get("BattleScene"))
        if scene_id is not None:
            scene_modes[scene_id].append(mode_id)
            scene_mode_sources[scene_id].append("PVPGameModeData.BattleScene")

    for row in match_pools.values():
        scene_id = integer(row.get("BattleScene"))
        mode_id = integer(row.get("GameMode"))
        if scene_id is None or mode_id not in pvp_modes:
            continue
        if mode_id not in scene_modes[scene_id]:
            scene_modes[scene_id].append(mode_id)
        scene_mode_sources[scene_id].append("MatchPoolData")

    # PVPRandomSceneData is the current 3v3 random-scene pool.  Its base mode
    # is identified from the direct 5200020 relationship, not from map Type.
    team_3v3_ids = [
        mode_id
        for mode_id, row in pvp_modes.items()
        if str(row.get("Const", "")) == "TEAM3V3"
    ]
    for scene_id in pvp_random:
        for mode_id in team_3v3_ids:
            if mode_id not in scene_modes[scene_id]:
                scene_modes[scene_id].append(mode_id)
            scene_mode_sources[scene_id].append("PVPRandomSceneData")

    for scene_id in regular_pvp_random:
        if scene_id in scene_modes:
            scene_mode_sources[scene_id].append("RegularPVPRandomSceneData")

    map_dungeons: dict[int, list[int]] = defaultdict(list)
    for dungeon_id, row in dungeons.items():
        for map_id in nested_integer_values(reader, row.get("WorldID")):
            if map_id in levels and dungeon_id not in map_dungeons[map_id]:
                map_dungeons[map_id].append(dungeon_id)

    gta_by_level: dict[int, list[int]] = defaultdict(list)
    for gta_id, row in gta_maps.items():
        map_id = integer(row.get("LevelID"))
        if map_id is not None:
            gta_by_level[map_id].append(gta_id)

    world_boss_by_level: dict[int, list[int]] = defaultdict(list)
    for world_boss_id, row in world_bosses.items():
        map_id = integer(row.get("LevelMapID"))
        if map_id is not None:
            world_boss_by_level[map_id].append(world_boss_id)

    catalog: list[dict[str, object]] = []
    for map_id, level in sorted(levels.items()):
        map_name = resolve_text(level.get("Name"), localization)
        level_type = integer(level.get("Type"))
        if level_type is None:
            level_type = -1
        fallback_category, fallback_type, fallback_mode = fallback_classification(
            level_type
        )
        map_display_name, variant = display_map_name(map_id, map_name)
        mode_ids = sorted(unique(scene_modes.get(map_id, [])))
        dungeon_ids = sorted(unique(map_dungeons.get(map_id, [])))
        mode_names: list[str] = []
        mode_consts: list[str] = []
        player_counts: list[int] = []
        for mode_id in mode_ids:
            row = pvp_modes[mode_id]
            name = resolve_text(row.get("Marks"), localization)
            const = str(row.get("Const", "")).strip()
            player_count = integer(row.get("PlayerNum"))
            if name:
                mode_names.append(name)
            if const:
                mode_consts.append(const)
            if player_count is not None:
                player_counts.append(player_count)
        mode_names = [str(value) for value in unique(mode_names)]
        mode_consts = [str(value) for value in unique(mode_consts)]
        player_counts = [int(value) for value in unique(player_counts)]

        dungeon_names = [
            resolve_text(dungeons[dungeon_id].get("Name"), localization)
            for dungeon_id in dungeon_ids
        ]
        dungeon_names = [
            str(value) for value in unique(name for name in dungeon_names if name)
        ]

        if mode_ids:
            category = "PVP"
            mode_type = pvp_mode_type(mode_consts)
            mode_name = " / ".join(mode_names) or " / ".join(mode_consts) or "PVP"
            sources = unique(scene_mode_sources.get(map_id, ["PVPGameModeData"]))
        elif dungeon_ids:
            category = "PVE"
            mode_type = "PVE副本"
            mode_name = " / ".join(dungeon_names) or "PVE副本"
            sources = ["DungeonData.WorldID"]
        elif world_boss_by_level.get(map_id):
            category = "PVE"
            mode_type = "世界Boss"
            mode_name = "世界首领"
            sources = ["WorldBossData.LevelMapID"]
        elif gta_by_level.get(map_id):
            gta_names = [
                resolve_text(gta_maps[gta_id].get("Name"), localization)
                for gta_id in gta_by_level[map_id]
            ]
            category = "探索"
            mode_type = "探索玩法"
            mode_name = " / ".join(
                str(value) for value in unique(name for name in gta_names if name)
            ) or "探索玩法"
            sources = ["GTAMapIDData.LevelID"]
        else:
            category = fallback_category
            mode_type = fallback_type
            mode_name = fallback_mode
            sources = ["LevelMapData.Type"]

        label = f"{mode_name}（{mode_type}）- {map_display_name or map_name or map_id}"
        catalog.append(
            {
                "map_id": map_id,
                "map_name": map_name,
                "map_name_localization_id": localization_id(level.get("Name")),
                "map_variant": variant,
                "mode_category": category,
                "mode_type": mode_type,
                "mode_name": mode_name,
                "display_label": label,
                "level_type_id": level_type,
                "level_type_label": fallback_type,
                "level_path_name": str(level.get("LevelPathName", "") or ""),
                "level_path": str(level.get("LevelPath", "") or ""),
                "map_ui_id": integer(level.get("MapID")),
                "belong_city_id": integer(level.get("BelongCity")),
                "pvp_mode_ids": mode_ids,
                "pvp_mode_names": mode_names,
                "pvp_consts": mode_consts,
                "pvp_player_counts": player_counts,
                "dungeon_ids": dungeon_ids,
                "dungeon_names": dungeon_names,
                "classification_source": [str(value) for value in sources],
                "source_status": "current_formal_map",
                "asset_tag_present": map_id in asset_tags,
            }
        )

    tag_rows: list[dict[str, object]] = []
    catalog_by_id = {int(row["map_id"]): row for row in catalog}
    for map_id in sorted(asset_tags):
        formal = catalog_by_id.get(map_id)
        tag_rows.append(
            {
                "map_id": map_id,
                "map_name": formal["map_name"] if formal else "",
                "source_status": (
                    "current_formal_map" if formal else "asset_tag_only"
                ),
                "level_path_name": formal["level_path_name"] if formal else "",
            }
        )
    return catalog, tag_rows


def csv_value(value: object) -> object:
    if isinstance(value, list):
        return " | ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return value


def write_catalog_csv(path: Path, catalog: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in catalog:
            writer.writerow({key: csv_value(row.get(key)) for key in CATALOG_FIELDS})


def write_tag_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = ("map_id", "map_name", "source_status", "level_path_name")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def validate_catalog(
    catalog: list[dict[str, object]],
    tag_rows: list[dict[str, object]],
    pvp_modes: dict[int, dict[object, object]],
    pvp_reference_tables: dict[str, dict[int, dict[object, object]]],
    pvp_random: dict[int, dict[object, object]],
    regular_pvp_random: dict[int, dict[object, object]],
    *,
    expected_map_count: int | None,
) -> dict[str, object]:
    ids = [int(row["map_id"]) for row in catalog]
    duplicate_ids = sorted(key for key, count in Counter(ids).items() if count > 1)
    unnamed_ids = [int(row["map_id"]) for row in catalog if not row["map_name"]]
    pvp_map_ids = {
        int(row["map_id"])
        for row in catalog
        if row["classification_source"]
        and any(
            "PVP" in str(source) for source in row["classification_source"]
        )
    }
    direct_scenes = {
        value
        for row in pvp_modes.values()
        for value in [integer(row.get("BattleScene"))]
        if value is not None
    }
    missing_pvp_scenes = sorted(direct_scenes - set(ids))
    referenced_mode_ids = {
        mode_id
        for rows in pvp_reference_tables.values()
        for row in rows.values()
        for mode_id in [integer(row.get("GameMode"))]
        if mode_id is not None and mode_id > 0
    }
    orphan_mode_references = sorted(referenced_mode_ids - set(pvp_modes))
    random_scenes_missing_from_levels = sorted(
        (set(pvp_random) | set(regular_pvp_random)) - set(ids)
    )
    tag_counts = Counter(str(row["source_status"]) for row in tag_rows)
    validation = {
        "formal_map_count": len(catalog),
        "expected_formal_map_count": expected_map_count,
        "unique_map_id_count": len(set(ids)),
        "duplicate_map_ids": duplicate_ids,
        "unnamed_map_ids": unnamed_ids,
        "pvp_mode_count": len(pvp_modes),
        "pvp_classified_map_count": len(pvp_map_ids),
        "missing_direct_pvp_scenes": missing_pvp_scenes,
        "orphan_pvp_mode_references": orphan_mode_references,
        "random_pvp_scenes_missing_from_levels": random_scenes_missing_from_levels,
        "pvp_reference_table_row_counts": {
            name: len(rows) for name, rows in sorted(pvp_reference_tables.items())
        },
        "asset_tag_count": len(tag_rows),
        "asset_tags_matching_formal_maps": tag_counts["current_formal_map"],
        "asset_tag_only_count": tag_counts["asset_tag_only"],
        "formal_maps_without_asset_tag": sum(
            not bool(row["asset_tag_present"]) for row in catalog
        ),
    }
    problems = []
    if expected_map_count is not None and len(catalog) != expected_map_count:
        problems.append(
            f"formal map count {len(catalog)} != expected {expected_map_count}"
        )
    if duplicate_ids:
        problems.append(f"duplicate map IDs: {duplicate_ids}")
    if unnamed_ids:
        problems.append(f"maps without localized names: {unnamed_ids}")
    if missing_pvp_scenes:
        problems.append(f"PVP scenes absent from LevelMapData: {missing_pvp_scenes}")
    if orphan_mode_references:
        problems.append(f"unknown referenced PVP modes: {orphan_mode_references}")
    if random_scenes_missing_from_levels:
        problems.append(
            "random PVP scenes absent from LevelMapData: "
            f"{random_scenes_missing_from_levels}"
        )
    validation["passed"] = not problems
    validation["problems"] = problems
    if problems:
        raise RuntimeError("catalog validation failed: " + "; ".join(problems))
    return validation


def parse_args() -> argparse.Namespace:
    default_output = REPOSITORY_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=latest_default_cache())
    parser.add_argument("--tag-cache", type=Path)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--date-tag", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--pid", type=int)
    parser.add_argument(
        "--localization-snapshot",
        type=Path,
        help="prior catalog JSON or a plain localization-ID-to-text JSON mapping",
    )
    parser.add_argument(
        "--no-live-scan",
        action="store_true",
        help="do not inspect the running game; require names from a snapshot",
    )
    parser.add_argument(
        "--expected-map-count",
        type=int,
        default=CURRENT_EXPECTED_MAP_COUNT,
        help="set to 0 to accept a changed client map count",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cache_path = args.cache.resolve()
    tag_cache_path = (args.tag_cache or default_tag_cache(cache_path)).resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "map_catalog.json"
    csv_path = output_dir / f"map_catalog_{args.date_tag}.csv"
    tag_csv_path = output_dir / f"map_asset_tags_{args.date_tag}.csv"
    snapshot_path = args.localization_snapshot
    if snapshot_path is None and json_path.is_file():
        snapshot_path = json_path

    reader = KSBC2Reader.from_path(cache_path)
    root_handle = discover_root_handle(reader)
    root = reader.table_dict(root_handle)
    levels = table_rows(reader, root, "LevelMapData")
    dungeons = table_rows(reader, root, "DungeonData")
    pvp_modes = table_rows(reader, root, "PVPGameModeData")
    pvp_random = table_rows(reader, root, "PVPRandomSceneData")
    regular_pvp_random = table_rows(reader, root, "RegularPVPRandomSceneData")
    pvp_entrances = table_rows(reader, root, "PVPEntranceInfoData")
    play_pvp_types = table_rows(reader, root, "PlayEntrancePVPTypeData")
    match_pools = table_rows(reader, root, "MatchPoolData")
    battle_rooms = table_rows(reader, root, "BattleRoomData")
    four_faction_battles = table_rows(reader, root, "FourFactionBattleData")
    gta_maps = table_rows(reader, root, "GTAMapIDData", required=False)
    world_bosses = table_rows(reader, root, "WorldBossData", required=False)
    asset_tags = read_asset_map_tags(tag_cache_path)

    localized_fields = (
        (levels, ("Name",)),
        (dungeons, ("Name",)),
        (pvp_modes, ("Marks",)),
        (pvp_entrances, ("Name", "SubName")),
        (play_pvp_types, ("Name", "MiniName", "Subtitle")),
        (battle_rooms, ("Name", "RuleDesc")),
        (gta_maps, ("Name",)),
    )
    wanted_localization = {
        loc_id
        for rows, fields in localized_fields
        for row in rows.values()
        for field in fields
        for loc_id in [localization_id(row.get(field))]
        if loc_id is not None
    }
    localization, localization_diagnostics = scan_localization(
        wanted_localization,
        pid=args.pid,
        fallback=load_localization_snapshot(snapshot_path),
        no_live_scan=args.no_live_scan,
    )
    catalog, tag_rows = build_catalog(
        reader=reader,
        levels=levels,
        pvp_modes=pvp_modes,
        dungeons=dungeons,
        pvp_random=pvp_random,
        regular_pvp_random=regular_pvp_random,
        match_pools=match_pools,
        gta_maps=gta_maps,
        world_bosses=world_bosses,
        localization=localization,
        asset_tags=asset_tags,
    )
    validation = validate_catalog(
        catalog,
        tag_rows,
        pvp_modes,
        pvp_reference_tables={
            "PVPEntranceInfoData": pvp_entrances,
            "PlayEntrancePVPTypeData": play_pvp_types,
            "MatchPoolData": match_pools,
            "BattleRoomData": battle_rooms,
            "FourFactionBattleData": four_faction_battles,
        },
        pvp_random=pvp_random,
        regular_pvp_random=regular_pvp_random,
        expected_map_count=(
            args.expected_map_count if args.expected_map_count > 0 else None
        ),
    )
    if localization_diagnostics["unresolved"]:
        raise RuntimeError(
            "localization scan incomplete; unresolved IDs: "
            + ", ".join(str(value) for value in localization_diagnostics["unresolved"])
        )

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": {
            "client_cache": str(cache_path),
            "ksbc2_root_handle": f"0x{root_handle:x}",
            "tag_cache": str(tag_cache_path),
            "authoritative_map_table": "LevelMapData",
            "table_row_counts": {
                "LevelMapData": len(levels),
                "DungeonData": len(dungeons),
                "PVPGameModeData": len(pvp_modes),
                "PVPEntranceInfoData": len(pvp_entrances),
                "PlayEntrancePVPTypeData": len(play_pvp_types),
                "PVPRandomSceneData": len(pvp_random),
                "RegularPVPRandomSceneData": len(regular_pvp_random),
                "MatchPoolData": len(match_pools),
                "BattleRoomData": len(battle_rooms),
                "FourFactionBattleData": len(four_faction_battles),
                "GTAMapIDData": len(gta_maps),
                "WorldBossData": len(world_bosses),
            },
        },
        "classification_priority": [
            "PVPGameModeData/PVPRandomSceneData exact relationship",
            "DungeonData.WorldID exact relationship",
            "special activity table exact relationship",
            "LevelMapData.Type fallback",
        ],
        "validation": validation,
        "localization_diagnostics": localization_diagnostics,
        "level_type_legend": {
            str(key): {
                "mode_category": value[0],
                "mode_type": value[1],
                "fallback_mode_name": value[2],
            }
            for key, value in sorted(LEVEL_TYPE_LABELS.items())
        },
        "localization": {
            str(key): value for key, value in sorted(localization.items())
        },
        "maps": catalog,
        "asset_tag_summary": {
            "all_tag_count": validation["asset_tag_count"],
            "matching_formal_map_count": validation[
                "asset_tags_matching_formal_maps"
            ],
            "asset_tag_only_count": validation["asset_tag_only_count"],
            "formal_maps_without_asset_tag": validation[
                "formal_maps_without_asset_tag"
            ],
        },
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_catalog_csv(csv_path, catalog)
    write_tag_csv(tag_csv_path, tag_rows)

    print(
        json.dumps(
            {
                "outputs": [str(json_path), str(csv_path), str(tag_csv_path)],
                "validation": validation,
                "localization_diagnostics": localization_diagnostics,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
