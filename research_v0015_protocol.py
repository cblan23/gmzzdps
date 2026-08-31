#!/usr/bin/env python3
"""Produce reproducible evidence for the v0.0.15 combat-data work.

The script is intentionally read-only. It summarizes decoded network JSONL
records without changing the capture or combat model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable


TARGET_STATISTICS_CANDIDATES = (
    "RetDungeonBattleStatistics",
    "RetMonsterBattleStatistics",
    "RetNpcCombatStatisticsByTeam",
    "RetDirtyNpcCombatStatisticsByTeam",
    "OnMsgUpdateDungeonBattleStatistics",
    "OnMsgUpdateDungeonTeamPlayerBattleStatistics",
)


def event_epoch(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def normalized_skill_id(value: object) -> int:
    try:
        skill_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if skill_id >= 1_000_000_000_000:
        skill_id //= 10_000
    if 100_000_000 <= skill_id <= 9_999_999_999 and skill_id % 10 == 0:
        skill_id //= 10
    return skill_id


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def map_pairs(value: object) -> list[tuple[object, object]]:
    if not isinstance(value, dict):
        return []
    raw_pairs = value.get("$map")
    if isinstance(raw_pairs, list):
        return [
            (pair[0], pair[1])
            for pair in raw_pairs
            if isinstance(pair, list) and len(pair) >= 2
        ]
    return list(value.items())


def direct_numeric_map(value: object) -> dict[int, object]:
    result: dict[int, object] = {}
    for raw_key, raw_value in map_pairs(value):
        try:
            key = int(raw_key)
        except (TypeError, ValueError, OverflowError):
            continue
        result[key] = raw_value
    return result


def numeric_map_summary(value: object) -> dict[str, int]:
    values = []
    for _key, raw_value in map_pairs(value):
        try:
            values.append(int(raw_value))
        except (TypeError, ValueError, OverflowError):
            continue
    return {"entries": len(values), "sum": sum(values)}


def iter_records(paths: Iterable[Path]) -> Iterable[dict]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    continue
                if isinstance(value, dict):
                    value["_source_file"] = path.name
                    value["_source_line"] = line_number
                    yield value


def in_window(record: dict, start: float, end: float) -> bool:
    timestamp = event_epoch(record.get("event_time"))
    if start and timestamp < start:
        return False
    if end and timestamp > end:
        return False
    return True


def summarize(paths: list[Path], start: float, end: float) -> dict:
    methods: Counter[str] = Counter()
    damage: dict[tuple[int, int], dict] = defaultdict(
        lambda: {"events": 0, "damage": 0, "skills": Counter()}
    )
    healing: dict[tuple[int, int], dict] = defaultdict(
        lambda: {
            "events": 0,
            "attempted": 0,
            "effective": 0,
            "overheal": 0,
            "skills": Counter(),
        }
    )
    invalid_heals: list[dict] = []
    statistics_records: list[dict] = []
    min_time = 0.0
    max_time = 0.0

    for record in iter_records(paths):
        if not in_window(record, start, end):
            continue
        timestamp = event_epoch(record.get("event_time"))
        if timestamp:
            min_time = min(min_time or timestamp, timestamp)
            max_time = max(max_time, timestamp)
        method = str(record.get("method") or record.get("function") or "")
        methods[method] += 1
        args = record.get("decoded_arguments")
        if not isinstance(args, list):
            continue

        if method == "OnMsgDamageSyncV2" and len(args) >= 9:
            try:
                actor_id = int(args[0])
                target_id = int(args[1])
                skill_id = normalized_skill_id(args[2])
                amount = max(0, int(args[7]))
            except (TypeError, ValueError, OverflowError):
                continue
            row = damage[(actor_id, target_id)]
            row["events"] += 1
            row["damage"] += amount
            row["skills"][skill_id] += amount
            continue

        if method == "OnMsgHealSyncV2" and len(args) >= 5:
            try:
                actor_id = int(args[0])
                target_id = int(args[1])
                skill_id = normalized_skill_id(args[2])
                attempted = max(0, int(args[3]))
                effective = max(0, int(args[4]))
            except (TypeError, ValueError, OverflowError):
                continue
            if effective > attempted:
                invalid_heals.append(
                    {
                        "source_file": record.get("_source_file", ""),
                        "source_line": record.get("_source_line", 0),
                        "attempted": attempted,
                        "effective": effective,
                    }
                )
            overheal = max(0, attempted - effective)
            row = healing[(actor_id, target_id)]
            row["events"] += 1
            row["attempted"] += attempted
            row["effective"] += min(attempted, effective)
            row["overheal"] += overheal
            row["skills"][skill_id] += min(attempted, effective)
            continue

        lowered = method.casefold()
        if any(
            marker in lowered
            for marker in ("statistics", "settlement", "battle")
        ):
            statistics_records.append(
                {
                    "event_time": record.get("event_time", ""),
                    "method": method,
                    "arguments": args,
                }
            )

    def damage_rows() -> list[dict]:
        result = []
        for (actor_id, target_id), value in damage.items():
            result.append(
                {
                    "actor_id": actor_id,
                    "target_id": target_id,
                    "events": value["events"],
                    "damage": value["damage"],
                    "skills": [
                        {"skill_id": skill_id, "damage": amount}
                        for skill_id, amount in value["skills"].most_common()
                    ],
                }
            )
        return sorted(result, key=lambda item: item["damage"], reverse=True)

    def healing_rows() -> list[dict]:
        result = []
        for (actor_id, target_id), value in healing.items():
            result.append(
                {
                    "actor_id": actor_id,
                    "target_id": target_id,
                    "events": value["events"],
                    "attempted": value["attempted"],
                    "effective": value["effective"],
                    "overheal": value["overheal"],
                    "overheal_rate": (
                        value["overheal"] / value["attempted"]
                        if value["attempted"]
                        else 0.0
                    ),
                    "skills": [
                        {"skill_id": skill_id, "effective": amount}
                        for skill_id, amount in value["skills"].most_common()
                    ],
                }
            )
        return sorted(result, key=lambda item: item["effective"], reverse=True)

    return {
        "schema_version": 1,
        "source_files": [path.name for path in paths],
        "observed_start": datetime.fromtimestamp(min_time).astimezone().isoformat()
        if min_time
        else "",
        "observed_end": datetime.fromtimestamp(max_time).astimezone().isoformat()
        if max_time
        else "",
        "method_counts": dict(methods.most_common()),
        "damage": damage_rows(),
        "healing": healing_rows(),
        "invalid_heal_count": len(invalid_heals),
        "invalid_heal_samples": invalid_heals[:20],
        "statistics_records": statistics_records,
    }


def anonymous_summary(report: dict) -> dict:
    """Strip identities while retaining field-level protocol evidence."""
    damage_rows = [row for row in report.get("damage", []) if isinstance(row, dict)]
    healing_rows = [row for row in report.get("healing", []) if isinstance(row, dict)]
    settlement_rows: list[dict] = []
    for record in report.get("statistics_records", []):
        if not isinstance(record, dict):
            continue
        method = str(record.get("method", ""))
        if method not in {
            "OnMsgDungeonStageSettlement",
            "OnMsgSettlementCombatStatistics",
            "OnMsgUpdateStageCombatStatistics",
        }:
            continue
        arguments = record.get("arguments", [])
        if not isinstance(arguments, list):
            continue
        candidates = arguments[1:] + arguments[:1] if method == "OnMsgSettlementCombatStatistics" else arguments[:1]
        stage: dict[int, object] = {}
        for candidate in candidates:
            parsed = direct_numeric_map(candidate)
            if map_pairs(parsed.get(5)):
                stage = parsed
                break
        if not stage:
            continue
        for _token, raw_profile in map_pairs(stage.get(5)):
            fields = direct_numeric_map(raw_profile)
            try:
                damage = max(0, int(fields.get(6, 0) or 0))
                received_damage = max(0, int(fields.get(7, 0) or 0))
                effective_healing = max(0, int(fields.get(17, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            skill_hits = numeric_map_summary(fields.get(33))
            skill_damage = numeric_map_summary(fields.get(34))
            skill_healing = numeric_map_summary(fields.get(35))
            incoming_damage = numeric_map_summary(fields.get(36))
            relation_field = numeric_map_summary(fields.get(37))
            settlement_rows.append(
                {
                    "slot": len(settlement_rows) + 1,
                    "damage": damage,
                    "received_damage_field_7": received_damage,
                    "effective_healing_field_17": effective_healing,
                    "skill_hit_entries_field_33": skill_hits["entries"],
                    "skill_hit_total_field_33": skill_hits["sum"],
                    "skill_damage_entries_field_34": skill_damage["entries"],
                    "skill_damage_total_field_34": skill_damage["sum"],
                    "skill_healing_entries_field_35": skill_healing["entries"],
                    "skill_healing_total_field_35": skill_healing["sum"],
                    "incoming_entries_field_36": incoming_damage["entries"],
                    "incoming_total_field_36": incoming_damage["sum"],
                    "relation_entries_field_37": relation_field["entries"],
                    "relation_total_field_37": relation_field["sum"],
                }
            )
    total_effective = sum(
        row["effective_healing_field_17"] for row in settlement_rows
    )
    total_skill_healing = sum(
        row["skill_healing_total_field_35"] for row in settlement_rows
    )
    return {
        "schema_version": 1,
        "privacy": "no character names, user tokens, actor IDs or target IDs",
        "source_files": list(report.get("source_files", [])),
        "observed_start": str(report.get("observed_start", "")),
        "observed_end": str(report.get("observed_end", "")),
        "method_counts": dict(report.get("method_counts", {})),
        "candidate_target_statistics_counts": {
            method: int(report.get("method_counts", {}).get(method, 0) or 0)
            for method in TARGET_STATISTICS_CANDIDATES
        },
        "damage": {
            "actor_target_pair_count": len(damage_rows),
            "event_count": sum(int(row.get("events", 0) or 0) for row in damage_rows),
            "total": sum(int(row.get("damage", 0) or 0) for row in damage_rows),
        },
        "healing": {
            "healer_target_pair_count": len(healing_rows),
            "event_count": sum(int(row.get("events", 0) or 0) for row in healing_rows),
            "attempted": sum(int(row.get("attempted", 0) or 0) for row in healing_rows),
            "effective": sum(int(row.get("effective", 0) or 0) for row in healing_rows),
            "overheal": sum(int(row.get("overheal", 0) or 0) for row in healing_rows),
            "invalid_event_count": int(report.get("invalid_heal_count", 0) or 0),
        },
        "settlement_rows": settlement_rows,
        "verified_invariants": {
            "field_17_total": total_effective,
            "field_35_total": total_skill_healing,
            "field_17_equals_field_35": total_effective == total_skill_healing,
            "field_36_equals_field_7_for_every_row": bool(settlement_rows)
            and all(
                row["incoming_total_field_36"]
                == row["received_damage_field_7"]
                for row in settlement_rows
            ),
            "field_34_never_exceeds_actor_damage": bool(settlement_rows)
            and all(
                row["skill_damage_total_field_34"] <= row["damage"]
                for row in settlement_rows
            ),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-statistics", action="store_true")
    parser.add_argument("--anonymous", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing = [path for path in args.paths if not path.is_file()]
    if missing:
        raise SystemExit(f"missing input: {missing[0]}")
    start = event_epoch(args.start) if args.start else 0.0
    end = event_epoch(args.end) if args.end else 0.0
    report = summarize(args.paths, start, end)
    if args.anonymous:
        report = anonymous_summary(report)
        report["source_sha256"] = {
            path.name: file_sha256(path) for path in args.paths
        }
    elif not args.include_statistics:
        report.pop("statistics_records", None)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
