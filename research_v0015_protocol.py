#!/usr/bin/env python3
"""Produce reproducible evidence for the v0.0.15 combat-data work.

The script is intentionally read-only. It summarizes decoded network JSONL
records without changing the capture or combat model.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-statistics", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing = [path for path in args.paths if not path.is_file()]
    if missing:
        raise SystemExit(f"missing input: {missing[0]}")
    start = event_epoch(args.start) if args.start else 0.0
    end = event_epoch(args.end) if args.end else 0.0
    report = summarize(args.paths, start, end)
    if not args.include_statistics:
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
