#!/usr/bin/env python3
"""Extract damage-confirmed Boss skills from captured combat logs.

Only one attribution is accepted:

* a damage packet whose attacker has a catalog-confirmed Boss template ID.

``OnMsgCastSkillNew`` is deliberately excluded. Real captures prove that a
Boss-bound ScriptEntity can receive player skill messages, so its pointer is
not reliable caster attribution. The report never expands BornSkill/DeathSkill
into a claimed rotation and never copies skills between same-name templates.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from network_state import NetworkPacketParser, normalize_network_skill_id


ROOT = Path(__file__).resolve().parent
DEFAULT_EVIDENCE = ROOT / "research-v0.0.15" / "boss-catalog-evidence.json"
DEFAULT_OUTPUT = ROOT / "research-v0.0.15" / "boss-skills-evidence.json"
DEFAULT_MARKDOWN = ROOT / "research-v0.0.15" / "boss-catalog-v0.0.15.md"
DEFAULT_LOG_DIR = (
    Path(os.environ.get("LOCALAPPDATA", ROOT)) / "GMZZDpsMeter" / "logs"
)


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


def arguments(record: dict) -> list:
    value = record.get("decoded_arguments")
    return value if isinstance(value, list) else []


def skill_name(skill_names: dict, skill_id: int) -> str:
    return str(skill_names.get(str(skill_id), "")).strip() or f"技能 {skill_id}"


def source_sample(record: dict, source_file: Path) -> dict:
    return {
        "event_time": str(record.get("event_time", "")),
        "source_file": source_file.name,
        "sequence": integer(record.get("sequence")),
    }


def add_observation(
    observations: dict[int, dict[int, dict]],
    template_id: int,
    skill_id: int,
    kind: str,
    record: dict,
    source_file: Path,
    amount: int = 0,
) -> None:
    if template_id <= 0 or skill_id <= 0:
        return
    row = observations[template_id].setdefault(
        skill_id,
        {
            "cast_count": 0,
            "damage_event_count": 0,
            "observed_damage": 0,
            "source_files": set(),
            "samples": [],
        },
    )
    row["source_files"].add(source_file.name)
    if kind == "cast":
        row["cast_count"] += 1
    elif kind == "damage":
        row["damage_event_count"] += 1
        row["observed_damage"] += max(0, amount)
    if len(row["samples"]) < 5:
        sample = source_sample(record, source_file)
        sample["evidence"] = kind
        row["samples"].append(sample)


def scan_logs(
    paths: list[Path], catalog: dict[str, dict]
) -> tuple[dict[int, dict[int, dict]], dict]:
    observations: dict[int, dict[int, dict]] = defaultdict(dict)
    counters: Counter[str] = Counter()

    for path in paths:
        # Every capture file starts with a fresh game/capture process. Entity and
        # ScriptEntity pointer values are process-local and are routinely reused,
        # so carrying parser bindings across files creates false Boss casts.
        parser = NetworkPacketParser(boss_template_catalog=catalog)
        counters["source_files"] += 1
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            counters["unreadable_files"] += 1
            continue
        with handle:
            for line_number, line in enumerate(handle, 1):
                counters["lines"] += 1
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    counters["invalid_json_lines"] += 1
                    continue
                if not isinstance(record, dict):
                    continue
                function = str(record.get("function", ""))
                method = str(record.get("method", ""))
                try:
                    if "TemplateBossType" in function:
                        parser.process_native_boss_type(record)
                        counters["native_template_records"] += 1
                        continue

                    if function == "KAPI_HandleDamageSyncV2":
                        attacker_id = integer(record.get("attacker_id"))
                        template_id = integer(
                            parser.entity_template_ids.get(attacker_id, 0)
                        )
                        if str(template_id) in catalog:
                            add_observation(
                                observations,
                                template_id,
                                normalize_network_skill_id(
                                    record.get("arg4_u64", 0)
                                ),
                                "damage",
                                record,
                                path,
                                integer(record.get("damage", 0)),
                            )
                        parser.process_native_damage(record)
                        continue

                    if not method:
                        continue
                    parser.process(record, include_damage=False)
                    args = arguments(record)
                    if method == "OnMsgDamageSyncV2" and len(args) >= 8:
                        attacker_id = integer(args[0])
                        template_id = integer(
                            parser.entity_template_ids.get(attacker_id, 0)
                        )
                        if str(template_id) in catalog:
                            add_observation(
                                observations,
                                template_id,
                                normalize_network_skill_id(args[2]),
                                "damage",
                                record,
                                path,
                                integer(args[7]),
                            )
                except (
                    AttributeError,
                    KeyError,
                    TypeError,
                    ValueError,
                    OverflowError,
                ) as exc:
                    counters["parser_errors"] += 1
                    if counters["parser_errors"] <= 20:
                        counters[f"parser_error_file:{path.name}:{line_number}"] += 1
                        counters[
                            f"parser_error_kind:{type(exc).__name__}:{function or method}"
                        ] += 1
    return observations, dict(counters)


def metadata_skill_ids(boss: dict) -> list[tuple[str, int]]:
    raw = boss.get("raw_metadata", {})
    scalar = raw.get("scalar_fields", {}) if isinstance(raw, dict) else {}
    scalar = scalar if isinstance(scalar, dict) else {}
    result = []
    for field, kind in (("BornSkill", "born"), ("DeathSkill", "death")):
        skill_id = normalize_network_skill_id(scalar.get(field, 0))
        if skill_id > 0:
            result.append((kind, skill_id))
    return result


def build_report(
    evidence: dict,
    observations: dict[int, dict[int, dict]],
    counters: dict,
    skill_names: dict,
    paths: list[Path],
) -> dict:
    bosses = []
    observed_templates = 0
    for boss in evidence.get("bosses", []):
        if not isinstance(boss, dict):
            continue
        template_id = integer(boss.get("template_id"))
        observed = []
        for skill_id, row in sorted(
            observations.get(template_id, {}).items(),
            key=lambda item: (
                item[1]["cast_count"] + item[1]["damage_event_count"],
                item[1]["observed_damage"],
            ),
            reverse=True,
        ):
            observed.append(
                {
                    "skill_id": skill_id,
                    "name": skill_name(skill_names, skill_id),
                    "cast_count": row["cast_count"],
                    "damage_event_count": row["damage_event_count"],
                    "observed_damage": row["observed_damage"],
                    "source_files": sorted(row["source_files"]),
                    "samples": row["samples"],
                    "source": "exact_damage_attacker_bound_to_boss_template",
                }
            )
        if observed:
            observed_templates += 1
        declared = [
            {
                "kind": kind,
                "skill_id": skill_id,
                "name": skill_name(skill_names, skill_id),
                "source": "client_MonsterData",
                "scope": "spawn_or_death_only_not_complete_rotation",
            }
            for kind, skill_id in metadata_skill_ids(boss)
        ]
        raw = boss.get("raw_metadata", {})
        bosses.append(
            {
                "template_id": template_id,
                "name": str(boss.get("name", "")),
                "level": boss.get("level"),
                "boss_type": boss.get("boss_type"),
                "encounter_auxiliary": bool(boss.get("encounter_auxiliary")),
                "attributes": (
                    raw.get("scalar_fields", {}) if isinstance(raw, dict) else {}
                ),
                "table_attributes": (
                    raw.get("table_fields", {}) if isinstance(raw, dict) else {}
                ),
                "declared_lifecycle_skills": declared,
                "observed_combat_skills": observed,
                "combat_skill_coverage": (
                    "observed_in_available_logs" if observed else "not_observed"
                ),
                "history_evidence": boss.get("observed_history", {}),
            }
        )
    return {
        "schema_version": 1,
        "policy": {
            "no_inference": True,
            "cast_acceptance": "excluded: Boss ScriptEntity receives non-caster player skill messages",
            "damage_acceptance": "exact packet attacker has a catalog-confirmed Boss template",
            "lifecycle_skill_scope": "BornSkill and DeathSkill are not a complete Boss skill list",
            "unobserved_meaning": "not present in available local evidence, not proof the Boss has no skills",
        },
        "source_files": [path.name for path in paths],
        "scan_counters": counters,
        "boss_template_count": len(bosses),
        "boss_templates_with_observed_skills": observed_templates,
        "bosses": bosses,
    }


def markdown(report: dict) -> str:
    lines = [
        "# 0.0.15 Boss 属性与技能实证目录",
        "",
        "> 本目录只记录客户端配置和本机日志中的直接证据。出生/死亡技能不等于完整技能表；“未观察到”不等于 Boss 没有技能。",
        "",
        f"- 当前生产识别模板：{report.get('boss_template_count', 0)} 个",
        f"- 在现有日志中观察到精确攻击技能的模板：{report.get('boss_templates_with_observed_skills', 0)} 个",
        f"- 扫描日志：{len(report.get('source_files', []))} 个",
        "",
        "| 模板 ID | Boss | 配置等级 | BossType | 历史场次 | 精确攻击技能数 | 出生/死亡技能 |",
        "| ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for boss in report.get("bosses", []):
        history = boss.get("history_evidence", {})
        lifecycle = "、".join(
            f"{row['name']} ({row['skill_id']})"
            for row in boss.get("declared_lifecycle_skills", [])
        ) or "--"
        lines.append(
            f"| {boss.get('template_id', 0)} | {boss.get('name', '')} | "
            f"{boss.get('level') if boss.get('level') is not None else '--'} | "
            f"{boss.get('boss_type', '--')} | {history.get('encounter_count', 0)} | "
            f"{len(boss.get('observed_combat_skills', []))} | {lifecycle} |"
        )
    lines.extend(["", "## 已观察到的精确攻击技能", ""])
    observed_any = False
    for boss in report.get("bosses", []):
        skills = boss.get("observed_combat_skills", [])
        if not skills:
            continue
        observed_any = True
        lines.extend(
            [
                f"### {boss.get('name', '')} ({boss.get('template_id', 0)})",
                "",
                "| 技能 ID | 名称 | 精确伤害包 | 已观察伤害 | 证据文件数 |",
                "| ---: | --- | ---: | ---: | ---: |",
            ]
        )
        for skill in skills:
            lines.append(
                f"| {skill['skill_id']} | {skill['name']} | "
                f"{skill['damage_event_count']} | {skill['observed_damage']} | "
                f"{len(skill['source_files'])} |"
            )
        lines.append("")
    if not observed_any:
        lines.extend(["现有日志没有形成可接受的 Boss 攻击技能绑定证据。", ""])
    lines.extend(
        [
            "## 属性字段说明",
            "",
            "每个模板的完整原始标量属性、FCPathList/WanderMoveParams 等表句柄，以及逐条技能证据位于 `boss-skills-evidence.json`。`OnMsgCastSkillNew` 已实测会在 Boss 指针上承载玩家技能，因此不用于技能归属；没有造成伤害的机制只能标为未观察到。`Lv=-1/-2` 是客户端配置中的动态等级标记，不会被改写为推测等级。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--max-files", type=int, default=0)
    args = parser.parse_args()
    evidence = json_object(args.evidence)
    if not evidence:
        raise SystemExit(f"missing catalog evidence: {args.evidence}")
    paths = sorted(
        path
        for path in args.logs.glob("network_*.jsonl")
        if path.is_file() and path.stat().st_size > 0
    )
    if args.max_files > 0:
        paths = paths[-args.max_files :]
    catalog = {
        str(row.get("template_id")): {
            "name": row.get("name", ""),
            "boss_type": row.get("boss_type", 0),
            "level": row.get("level"),
        }
        for row in evidence.get("bosses", [])
        if isinstance(row, dict) and integer(row.get("template_id")) > 0
    }
    observations, counters = scan_logs(paths, catalog)
    report = build_report(
        evidence,
        observations,
        counters,
        json_object(ROOT / "skill_names.json"),
        paths,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown.write_text(markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "boss_templates": report["boss_template_count"],
                "observed_templates": report[
                    "boss_templates_with_observed_skills"
                ],
                "source_files": len(paths),
                "lines": counters.get("lines", 0),
                "parser_errors": counters.get("parser_errors", 0),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
