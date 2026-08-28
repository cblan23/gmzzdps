#!/usr/bin/env python3
"""Export and load MonsterData metadata from the current C7 client cache."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from ksbc2_skill_names import (
    DEFAULT_CACHE,
    DEFAULT_ROOT_HANDLE,
    KSBC2Reader,
    LiveLocalizationScanner,
    LocalizedCandidate,
    TableRef,
    normalize_localization_id,
    table_ref,
    write_json,
)


MONSTER_METADATA_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MonsterSource:
    template_id: int
    boss_type: int
    level: int | None
    localization_id: int | None
    direct_name: str
    fc_paths: tuple[str, ...]


def _fc_paths(reader: KSBC2Reader, value: object, depth: int = 0) -> list[str]:
    if not isinstance(value, TableRef) or depth > 3:
        return []
    paths: list[str] = []
    for key, item in reader.table_entries(value.handle):
        if key == "FCPath" and isinstance(item, str) and item:
            paths.append(item)
        elif isinstance(item, TableRef):
            paths.extend(_fc_paths(reader, item, depth + 1))
    return paths


def extract_monster_sources(
    reader: KSBC2Reader, root_handle: int = DEFAULT_ROOT_HANDLE
) -> list[MonsterSource]:
    root = reader.table_dict(root_handle)
    monster_data = reader.table_dict(table_ref(root.get("MonsterData"), "MonsterData"))
    rows_handle = table_ref(monster_data.get("data"), "MonsterData.data")
    sources: list[MonsterSource] = []
    for raw_template_id, raw_row in reader.table_entries(rows_handle):
        if not isinstance(raw_template_id, (int, float)) or not isinstance(
            raw_row, TableRef
        ):
            continue
        template_id = int(raw_template_id)
        if template_id <= 0:
            continue
        row = reader.table_dict(raw_row.handle)
        try:
            boss_type = int(row.get("BossType", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            boss_type = 0
        try:
            parsed_level = int(row.get("Lv", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            parsed_level = 0
        direct_name = row.get("Name") if isinstance(row.get("Name"), str) else ""
        sources.append(
            MonsterSource(
                template_id=template_id,
                boss_type=boss_type,
                level=parsed_level if parsed_level > 0 else None,
                localization_id=normalize_localization_id(row.get("Name")),
                direct_name=direct_name.strip(),
                fc_paths=tuple(dict.fromkeys(_fc_paths(reader, row.get("FCPathList")))),
            )
        )
    return sorted(sources, key=lambda item: item.template_id)


def choose_localized_names(
    localization_ids: set[int],
    candidates: dict[int, list[LocalizedCandidate]],
) -> tuple[dict[int, str], dict[int, list[str]]]:
    names: dict[int, str] = {}
    ambiguous: dict[int, list[str]] = {}
    for localization_id in sorted(localization_ids):
        texts = sorted(
            {
                candidate.text.strip()
                for candidate in candidates.get(localization_id, [])
                if candidate.text.strip()
            }
        )
        if len(texts) == 1:
            names[localization_id] = texts[0]
        elif len(texts) > 1:
            ambiguous[localization_id] = texts
    return names, ambiguous


def build_monster_metadata(
    sources: list[MonsterSource],
    localized_names: dict[int, str] | None = None,
    existing: dict | None = None,
) -> dict:
    localized_names = localized_names or {}
    existing_templates = (
        existing.get("templates", {}) if isinstance(existing, dict) else {}
    )
    if not isinstance(existing_templates, dict):
        existing_templates = {}
    templates: dict[str, dict] = {}
    for source in sources:
        previous = existing_templates.get(str(source.template_id), {})
        previous_name = previous.get("name", "") if isinstance(previous, dict) else ""
        name = (
            source.direct_name
            or localized_names.get(int(source.localization_id or 0), "")
            or (previous_name if isinstance(previous_name, str) else "")
        ).strip()
        value: dict[str, object] = {
            "boss_type": source.boss_type,
        }
        if source.level is not None:
            value["level"] = source.level
        if source.localization_id is not None:
            value["localization_id"] = source.localization_id
        if name:
            value["name"] = name
        if source.fc_paths:
            value["fc_paths"] = list(source.fc_paths)
        templates[str(source.template_id)] = value
    return {
        "schema_version": MONSTER_METADATA_SCHEMA_VERSION,
        "templates": templates,
    }


def load_monster_metadata(path: Path) -> dict[str, dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    templates = value.get("templates", {}) if isinstance(value, dict) else {}
    if not isinstance(templates, dict):
        return {}
    return {
        str(template_id): dict(metadata)
        for template_id, metadata in templates.items()
        if isinstance(metadata, dict)
    }


def resolve_localization_names(pid: int, localization_ids: set[int]) -> dict[int, str]:
    if not localization_ids:
        return {}
    with LiveLocalizationScanner(pid) as scanner:
        candidates = scanner.scan(localization_ids)
    names, _ambiguous = choose_localized_names(localization_ids, candidates)
    return names


def integer(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--root", type=integer, default=DEFAULT_ROOT_HANDLE)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--output", type=Path, default=Path("monster_metadata.json"))
    parser.add_argument("--no-live-scan", action="store_true")
    parser.add_argument(
        "--boss-names-only",
        action="store_true",
        help="resolve only BossType=3 names while retaining metadata for every monster",
    )
    args = parser.parse_args()
    if not args.cache.is_file():
        raise SystemExit(f"KSBC2 cache not found: {args.cache}")

    reader = KSBC2Reader.from_path(args.cache)
    sources = extract_monster_sources(reader, args.root)
    try:
        existing = json.loads(args.output.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        existing = {}

    wanted = {
        source.localization_id
        for source in sources
        if source.localization_id is not None
        and (not args.boss_names_only or source.boss_type == 3)
    }
    names: dict[int, str] = {}
    if not args.no_live_scan:
        pid = args.pid
        if pid is None:
            from proc_inspect import find_pid

            pid = find_pid(args.process)
        names = resolve_localization_names(pid, wanted)
        if wanted and not names:
            raise SystemExit(
                "live localization scan returned no matches; existing output was unchanged"
            )

    metadata = build_monster_metadata(sources, names, existing)
    write_json(args.output, metadata)
    boss_count = sum(source.boss_type == 3 for source in sources)
    resolved_boss_names = sum(
        bool(metadata["templates"][str(source.template_id)].get("name"))
        for source in sources
        if source.boss_type == 3
    )
    print(
        f"templates={len(sources)} bosses={boss_count} "
        f"resolved_boss_names={resolved_boss_names} resolved_ids={len(names)}"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
