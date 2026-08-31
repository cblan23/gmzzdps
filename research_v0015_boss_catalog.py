#!/usr/bin/env python3
"""Build a source-labelled Boss catalog for the v0.0.15 research work.

Inputs are the client metadata export, the decoded KSBC2 cache and combat
history records. The tool does not attach to or modify the game process.
"""

from __future__ import annotations

import argparse
import json
import runpy
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from ksbc2_skill_names import DEFAULT_CACHE, DEFAULT_ROOT_HANDLE, KSBC2Reader, TableRef, table_ref
from monster_metadata import load_monster_metadata


ROOT = Path(__file__).resolve().parent
APP = runpy.run_path(str(ROOT / "dps_meter.pyw"))


def json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def history_records(path: Path) -> Iterable[dict]:
    if not path.is_dir():
        return
    for record_path in sorted(path.glob("*.json")):
        record = json_object(record_path)
        if record:
            record["_source_file"] = record_path.name
            yield record


def primitive(value: object) -> object:
    if isinstance(value, TableRef):
        return {"table_handle": value.handle}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def relevant_monster_fields(reader: KSBC2Reader, template_ids: set[int]) -> dict[str, dict]:
    root = reader.table_dict(DEFAULT_ROOT_HANDLE)
    monster_data = reader.table_dict(table_ref(root.get("MonsterData"), "MonsterData"))
    rows_handle = table_ref(monster_data.get("data"), "MonsterData.data")
    result: dict[str, dict] = {}
    field_markers = (
        "hp",
        "health",
        "attr",
        "attack",
        "def",
        "resist",
        "skill",
        "ability",
        "level",
        "lv",
        "boss",
        "name",
        "fcpath",
    )
    for raw_template_id, raw_row in reader.table_entries(rows_handle):
        template_id = integer(raw_template_id)
        if template_id not in template_ids or not isinstance(raw_row, TableRef):
            continue
        row = reader.table_dict(raw_row.handle)
        scalar_fields = {
            str(key): primitive(value)
            for key, value in row.items()
            if not isinstance(value, TableRef)
        }
        table_fields = {
            str(key): value.handle
            for key, value in row.items()
            if isinstance(value, TableRef)
        }
        selected = {
            str(key): primitive(value)
            for key, value in row.items()
            if any(marker in str(key).casefold() for marker in field_markers)
        }
        result[str(template_id)] = {
            "row_handle": raw_row.handle,
            "field_count": len(row),
            "field_names": sorted(str(key) for key in row),
            "scalar_fields": scalar_fields,
            "table_fields": table_fields,
            "relevant_fields": selected,
        }
    return result


def cache_protocol_strings(reader: KSBC2Reader) -> dict[str, list[int]]:
    names = (
        "ReqCommonCombatStatisticsByTeam",
        "ReqDirtyCommonCombatStatisticsByTeam",
        "RetCommonCombatStatisticsByTeam",
        "RetDirtyCommonCombatStatisticsByTeam",
        "ReqDungeonBattleStatistics",
        "RetDungeonBattleStatistics",
        "ReqMonsterBattleStatistics",
        "RetMonsterBattleStatistics",
        "ReqNpcCombatStatisticsByTeam",
        "RetNpcCombatStatisticsByTeam",
        "RetDirtyNpcCombatStatisticsByTeam",
        "OnMsgUpdateStageCombatStatistics",
        "OnMsgSettlementCombatStatistics",
        "OnMsgHealSyncV2",
    )
    result: dict[str, list[int]] = {}
    for name in names:
        needle = name.encode("ascii")
        offsets: list[int] = []
        cursor = 0
        while len(offsets) < 32:
            found = reader.data.find(needle, cursor)
            if found < 0:
                break
            offsets.append(found)
            cursor = found + 1
        result[name] = offsets
    return result


def related_root_tables(reader: KSBC2Reader) -> list[str]:
    root = reader.table_dict(DEFAULT_ROOT_HANDLE)
    markers = ("monster", "attr", "skill", "battle", "combat", "npc")
    return sorted(
        str(key)
        for key, value in root.items()
        if isinstance(value, TableRef)
        and any(marker in str(key).casefold() for marker in markers)
    )


def selected_table_overviews(reader: KSBC2Reader) -> dict[str, dict]:
    root = reader.table_dict(DEFAULT_ROOT_HANDLE)
    wanted = (
        "MonsterData",
        "MonsterWeaknessData",
        "NpcCombatLevelData",
        "NpcMonsterTemplateData",
        "CombatStatsTabData",
        "CombatStatsTagData",
        "StatisticsBuffAndSkillFilterData",
        "KSBC_EffectSkill",
    )
    result: dict[str, dict] = {}
    for name in wanted:
        reference = root.get(name)
        if not isinstance(reference, TableRef):
            continue
        container = reader.table_dict(reference.handle)
        overview: dict[str, object] = {
            "handle": reference.handle,
            "container_fields": {
                str(key): primitive(value) for key, value in container.items()
            },
        }
        data_reference = container.get("data")
        if isinstance(data_reference, TableRef):
            count = 0
            samples: list[dict] = []
            for key, value in reader.table_entries(data_reference.handle):
                count += 1
                if len(samples) >= 12:
                    continue
                if isinstance(value, TableRef):
                    row = reader.table_dict(value.handle)
                    samples.append(
                        {
                            "key": primitive(key),
                            "row_handle": value.handle,
                            "fields": {
                                str(field): primitive(item)
                                for field, item in row.items()
                                if not isinstance(item, TableRef)
                            },
                            "table_fields": {
                                str(field): item.handle
                                for field, item in row.items()
                                if isinstance(item, TableRef)
                            },
                        }
                    )
                else:
                    samples.append({"key": primitive(key), "value": primitive(value)})
            overview["row_count"] = count
            overview["samples"] = samples
        result[name] = overview
    return result


def build_history_evidence(records: Iterable[dict]) -> dict[str, dict]:
    evidence: dict[str, dict] = {}
    for record in records:
        monster = record.get("monster")
        if not isinstance(monster, dict):
            continue
        template_id = integer(monster.get("template_id"))
        if not template_id:
            continue
        row = evidence.setdefault(
            str(template_id),
            {
                "encounter_count": 0,
                "names": Counter(),
                "levels": Counter(),
                "max_hp": Counter(),
                "source_files": [],
            },
        )
        row["encounter_count"] += 1
        name = str(monster.get("name", "")).strip()
        if name:
            row["names"][name] += 1
        level = integer(monster.get("level"))
        if level:
            row["levels"][level] += 1
        max_hp = integer(monster.get("max_hp"))
        if max_hp:
            row["max_hp"][max_hp] += 1
        row["source_files"].append(str(record.get("_source_file", "")))
    for row in evidence.values():
        for key in ("names", "levels", "max_hp"):
            row[key] = [
                {"value": value, "encounters": count}
                for value, count in row[key].most_common()
            ]
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--history", type=Path, default=APP["HISTORY_DIR"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.cache.is_file():
        raise SystemExit(f"missing KSBC2 cache: {args.cache}")

    catalog = APP["load_monster_catalog"]()
    standard_metadata = load_monster_metadata(ROOT / "monster_metadata.json")
    boss_templates = {
        integer(template_id): {
            **dict(metadata),
            **(
                {"name": standard_metadata[str(template_id)]["name"]}
                if isinstance(standard_metadata.get(str(template_id), {}).get("name"), str)
                and standard_metadata[str(template_id)]["name"].strip()
                else {}
            ),
        }
        for template_id, metadata in catalog.items()
        if integer(template_id)
    }
    reader = KSBC2Reader.from_path(args.cache)
    raw_fields = relevant_monster_fields(reader, set(boss_templates))
    history = build_history_evidence(history_records(args.history))

    rows = []
    for template_id, metadata in sorted(boss_templates.items()):
        rows.append(
            {
                "template_id": template_id,
                "name": str(metadata.get("name", "")),
                "name_source": "monster_metadata.json",
                "level": metadata.get("level"),
                "boss_type": metadata.get("boss_type"),
                "boss_rank": metadata.get("boss_rank", 0),
                "encounter_auxiliary": bool(metadata.get("encounter_auxiliary")),
                "metadata_source": "client_KSBC2_MonsterData",
                "raw_metadata": raw_fields.get(str(template_id), {}),
                "observed_history": history.get(str(template_id), {}),
            }
        )
    report = {
        "schema_version": 1,
        "cache_file": args.cache.name,
        "boss_template_count": len(rows),
        "bosses": rows,
        "protocol_string_offsets": cache_protocol_strings(reader),
        "related_root_tables": related_root_tables(reader),
        "selected_table_overviews": selected_table_overviews(reader),
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
