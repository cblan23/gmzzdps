#!/usr/bin/env python3
"""Replay a captured JSONL log through the production parser and model.

This diagnostic is intentionally read-only. It follows the same update routing
as HookWorker/DpsWindow, then compares archived realtime values with any game
completion tables found in the capture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from combat_history import CombatHistoryStore


APP = runpy.run_path(str(Path(__file__).with_name("dps_meter.pyw")))
CombatModel = APP["CombatModel"]
NetworkPacketParser = APP["NetworkPacketParser"]


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def iter_records(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line_number, line in enumerate(source, 1):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                yield line_number, record


def record_template_id(record: dict[str, Any]) -> int:
    monster = record.get("monster")
    if not isinstance(monster, dict):
        return 0
    try:
        return int(monster.get("template_id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def actor_damage(record: dict[str, Any]) -> dict[int, int]:
    result: dict[int, int] = {}
    for row in record.get("participants", []):
        if not isinstance(row, dict):
            continue
        try:
            actor_id = int(row.get("actor_id", 0) or 0)
            damage = max(0, int(row.get("damage", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if actor_id:
            result[actor_id] = damage
    return result


def summary_damage(summary: dict[str, Any]) -> dict[int, int]:
    result: dict[int, int] = {}
    for row in summary.get("actors", []):
        if not isinstance(row, dict):
            continue
        try:
            actor_id = int(row.get("actor_id", 0) or 0)
            damage = max(0, int(row.get("damage", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if actor_id:
            result[actor_id] = damage
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def target_damage_evidence(record: dict[str, Any]) -> dict[str, int]:
    assigned = 0
    unassigned = 0
    for participant in record.get("participants", []):
        if not isinstance(participant, dict):
            continue
        for target in participant.get("targets", []):
            if not isinstance(target, dict):
                continue
            try:
                amount = max(0, int(target.get("damage", 0) or 0))
                entity_id = int(target.get("entity_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if entity_id:
                assigned += amount
            else:
                unassigned += amount
    return {"assigned": assigned, "unassigned": unassigned}


def anonymous_replay_evidence(
    *,
    source_path: Path,
    line_count: int,
    records: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    update_counts: Counter[str],
) -> dict[str, Any]:
    combats: list[dict[str, Any]] = []
    damage_material: list[list[Any]] = []
    for index, record in enumerate(records, 1):
        damage_by_actor = sorted(actor_damage(record).values(), reverse=True)
        total_damage = max(0, int(record.get("total_damage", 0) or 0))
        template_id = record_template_id(record)
        damage_material.append([template_id, total_damage, damage_by_actor])
        accounting = record.get("damage_accounting", {})
        accounting = accounting if isinstance(accounting, dict) else {}
        target_evidence = target_damage_evidence(record)
        healers = [
            row for row in record.get("healers", []) if isinstance(row, dict)
        ]
        server_skill_actors = sum(
            1
            for row in record.get("participants", [])
            if isinstance(row, dict)
            and str(row.get("skill_source", "")) == "server_stage_summary"
        )
        combats.append(
            {
                "ordinal": index,
                "template_id": template_id,
                "archive_reason": str(record.get("archive_reason", "")),
                "duration_seconds": round(
                    float(record.get("duration_seconds", 0.0) or 0.0), 3
                ),
                "participant_count": len(damage_by_actor),
                "participant_damage_desc": damage_by_actor,
                "total_damage": total_damage,
                "exact_packet_damage": max(
                    0, int(accounting.get("packet_event_total", 0) or 0)
                ),
                "assigned_target_damage": target_evidence["assigned"],
                "unassigned_target_damage": target_evidence["unassigned"],
                "server_skill_actor_count": server_skill_actors,
                "team_effective_healing": max(
                    0, int(record.get("team_effective_healing", 0) or 0)
                ),
                "team_total_healing": record.get("team_total_healing"),
                "healing_coverage": sorted(
                    {str(row.get("coverage", "")) for row in healers}
                ),
                "healing_response_samples": sum(
                    int((row.get("response") or {}).get("samples", 0) or 0)
                    for row in healers
                ),
            }
        )
    encoded_material = json.dumps(
        damage_material,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    validations: list[dict[str, Any]] = []
    for index, summary in enumerate(summaries, 1):
        match = choose_record_for_summary(records, summary)
        expected_total = sum(summary_damage(summary).values())
        observed_total = sum(actor_damage(match).values()) if match else 0
        validations.append(
            {
                "ordinal": index,
                "matched": match is not None,
                "template_id": record_template_id(match) if match else 0,
                "expected_total": expected_total,
                "observed_total": observed_total,
                "difference": expected_total - observed_total,
            }
        )
    return {
        "schema_version": 1,
        "privacy": "no character names, user tokens, actor IDs or encounter IDs",
        "source_sha256": file_sha256(source_path),
        "line_count": int(line_count),
        "update_counts": dict(sorted(update_counts.items())),
        "combat_count": len(records),
        "summary_count": len(summaries),
        "damage_result_fingerprint": hashlib.sha256(encoded_material).hexdigest().upper(),
        "damage_result_material": damage_material,
        "combats": combats,
        "settlement_validations": validations,
    }


def choose_record_for_summary(
    records: list[dict[str, Any]], summary: dict[str, Any]
) -> dict[str, Any] | None:
    expected = summary_damage(summary)
    if not expected:
        return None
    try:
        summary_time = (
            int(summary.get("filetime_100ns", 0) or 0)
            - 116_444_736_000_000_000
        ) / 10_000_000
    except (TypeError, ValueError, OverflowError):
        summary_time = 0.0
    candidates: list[tuple[tuple[float, ...], dict[str, Any]]] = []
    for record in records:
        observed = actor_damage(record)
        overlap = set(expected).intersection(observed)
        if not overlap:
            continue
        ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
        delay = summary_time - ended_at if summary_time and ended_at else 0.0
        if delay < -5.0 or delay > 300.0:
            continue
        per_actor_error = sum(
            abs(expected[actor_id] - observed[actor_id]) for actor_id in overlap
        )
        missing = len(set(expected).symmetric_difference(observed))
        total_error = abs(sum(expected.values()) - sum(observed.values()))
        score = (
            float(missing),
            -float(len(overlap)),
            float(per_actor_error),
            float(total_error),
            abs(delay),
        )
        candidates.append((score, record))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--template", action="append", type=int, default=[])
    parser.add_argument("--trace-actor", type=int, default=0)
    parser.add_argument("--trace-resets", action="store_true")
    parser.add_argument("--show-skills", action="store_true")
    parser.add_argument("--max-line", type=int, default=0)
    parser.add_argument("--repair-history-id", default="")
    parser.add_argument("--anonymous-output", type=Path)
    parser.add_argument("--anonymous-only", action="store_true")
    args = parser.parse_args()
    selected_templates = set(args.template)

    config = APP["load_config"]()
    identity = read_object(APP["SELF_IDENTITY_CACHE_PATH"])
    team_profiles = read_object(APP["TEAM_PROFILE_CACHE_PATH"])
    packet_parser = NetworkPacketParser(
        team_profiles,
        APP["load_monster_catalog"](),
        remembered_self_token=str(identity.get("user_token", "")),
        allow_cached_projection_roster=True,
        boss_name_allowlist=APP["load_boss_name_allowlist"](),
    )
    model = CombatModel(
        skill_names=APP["load_skill_catalog"](),
        runtime_skill_names=config.get("runtime_skill_names", {}),
        run_id=f"replay-{args.log.stem}",
        boss_only=True,
    )
    ignore_update = lambda _update: False
    handlers = {
        "event": model.ingest,
        "active_boss": model.ingest_active_boss,
        "identity": model.ingest_identity,
        "actor_merge": model.merge_actor,
        "party": model.ingest_party,
        "profile": model.ingest_profile,
        "monster": model.ingest_monster,
        "team_stat": model.ingest_team_stat,
        "stage_summary": model.ingest_stage_summary,
        "heal": getattr(model, "ingest_heal", ignore_update),
        "actor_health": getattr(model, "ingest_actor_health", ignore_update),
        "combat_state": model.ingest_combat_state,
        "life": model.ingest_life,
        "scene": model.ingest_scene,
        "name": model.ingest_name,
        "skill_name": model.ingest_skill_name,
    }
    update_counts: Counter[str] = Counter()
    summaries: list[dict[str, Any]] = []
    line_count = 0
    trace_count = 0
    last_parser_boss_id = 0
    traced_all_out_sessions: set[int] = set()
    traced_drill_out_entities: set[tuple[int, int]] = set()

    def apply_updates(
        updates: Iterable[tuple[str, dict[str, Any]]], line_number: int
    ) -> None:
        nonlocal trace_count
        for kind, update in updates:
            update_counts[kind] += 1
            if kind == "actor_merge" and args.trace_actor:
                try:
                    merge_ids = {
                        int(update.get("from_actor_id", 0) or 0),
                        int(update.get("to_actor_id", 0) or 0),
                    }
                except (TypeError, ValueError, OverflowError):
                    merge_ids = set()
                if args.trace_actor in merge_ids:
                    print(
                        f"TRACE_MERGE line={line_number} update="
                        + json.dumps(update, ensure_ascii=False, sort_keys=True)
                    )
            if kind == "stage_summary":
                summaries.append(dict(update))
            handler = handlers.get(kind)
            before_session = model.session_number
            before_target = model.combat_target_id
            before = None
            if (
                kind == "team_stat"
                and int(update.get("actor_id", 0) or 0) == args.trace_actor
            ):
                state = model.team_damage_states.get(args.trace_actor)
                before = (
                    state.last_absolute,
                    state.baseline_absolute,
                    state.accepted_damage,
                    state.baseline_snapshot_time_100ns,
                ) if state else None
            if handler is not None:
                handler(update)
            current = model.current_monster()
            current_is_drill = bool(
                current is not None and int(current.template_id or 0) == 7_115_080
            )
            if args.trace_resets and current_is_drill and kind == "life" and update.get("dead"):
                print(
                    "TRACE_LIFE "
                    f"line={line_number} session={model.session_number} "
                    f"time={update.get('event_time')} actor={update.get('actor_id')} "
                    f"confirmed={update.get('death_confirmed')} "
                    f"members={sorted(model._current_member_ids())}"
                )
            if args.trace_resets and current_is_drill and kind == "combat_state" and update.get("in_combat") is False:
                state_key = (
                    model.session_number,
                    int(update.get("entity_id", 0) or 0),
                )
                relevant_ids = model.encounter_member_ids | {
                    int(model.combat_target_id or 0)
                }
                if state_key not in traced_drill_out_entities and state_key[1] in relevant_ids:
                    print(
                        "TRACE_OUT "
                        f"line={line_number} session={model.session_number} "
                        f"time={update.get('event_time')} entity={state_key[1]} "
                        f"participants={sorted(model.encounter_member_ids)}"
                    )
                    traced_drill_out_entities.add(state_key)
            if (
                args.trace_resets
                and kind in {"combat_state", "life"}
                and model.session_number not in traced_all_out_sessions
                and model._all_encounter_combatants_out()
            ):
                if current is not None and int(current.template_id or 0) == 7_115_080:
                    print(
                        "TRACE_ALL_OUT "
                        f"line={line_number} kind={kind} "
                        f"session={model.session_number} "
                        f"time={update.get('event_time')} "
                        f"target={model.combat_target_id} "
                        f"boss_state={model.entity_combat_states.get(int(model.combat_target_id or 0))} "
                        f"states={model.entity_combat_states}"
                    )
                    traced_all_out_sessions.add(model.session_number)
            if args.trace_resets and model.session_number != before_session:
                print(
                    "TRACE_RESET "
                    f"line={line_number} kind={kind} "
                    f"time={update.get('event_time')} "
                    f"session={before_session}->{model.session_number} "
                    f"target={before_target}->{model.combat_target_id} "
                    f"source={update.get('source_method', '')}"
                )
            if (
                args.trace_resets
                and kind == "event"
                and int(update.get("attacker_id", 0) or 0) == args.trace_actor
                and trace_count < 120
            ):
                actor = model.stats.get(args.trace_actor)
                print(
                    "TRACE_EVENT "
                    f"line={line_number} time={update.get('event_time')} "
                    f"target={update.get('target_id')} "
                    f"combat_target={model.combat_target_id} "
                    f"known_monster={int(update.get('target_id', 0) or 0) in model.monsters} "
                    f"visible_damage={actor.damage if actor else 0}"
                )
                trace_count += 1
            if before is not None or (
                kind == "team_stat"
                and int(update.get("actor_id", 0) or 0) == args.trace_actor
            ):
                state = model.team_damage_states.get(args.trace_actor)
                after = (
                    state.last_absolute,
                    state.baseline_absolute,
                    state.accepted_damage,
                    state.baseline_snapshot_time_100ns,
                ) if state else None
                current = model.current_monster()
                current_template = int(current.template_id or 0) if current else 0
                baseline_changed = bool(
                    before is not None
                    and after is not None
                    and before[1:] != after[1:]
                )
                selected_trace = bool(
                    not selected_templates
                    or current_template in selected_templates
                )
                if selected_trace and (trace_count < 80 or baseline_changed):
                    print(
                        "TRACE "
                        f"line={line_number} template={current_template} "
                        f"time={update.get('event_time')} "
                        f"absolute={update.get('absolute_damage')} "
                        f"omitted_zero={bool(update.get('omitted_zero'))} "
                        f"start_signal={model.encounter_start_signal_100ns} "
                        f"before={before} after={after}"
                    )
                    trace_count += 1

    for line_count, record in iter_records(args.log):
        if args.max_line and line_count > args.max_line:
            line_count -= 1
            break
        function = str(record.get("function", ""))
        if function in {
            "KAPI_Common_SetBossType",
            "CommonComponent_ExistingBossType",
            "CommonComponent_TemplateBossType",
            "CommonComponent_TargetIdLookup",
        }:
            apply_updates(
                packet_parser.process_native_boss_type(record), line_count
            )
        elif function == "KAPI_HandleDamageSyncV2":
            apply_updates(packet_parser.process_native_damage(record), line_count)
        elif "CacheSkillAgentData" in function:
            model.ingest_skill_name(record)
            update_counts["skill_name"] += 1
        elif "CacheEntityName" in function:
            boss_name_update = packet_parser.apply_runtime_boss_name(record)
            if boss_name_update:
                apply_updates([boss_name_update], line_count)
            model.ingest_name(record)
            update_counts["name"] += 1
        else:
            apply_updates(
                packet_parser.process(record, include_damage=False), line_count
            )

        current_parser_boss_id = int(packet_parser.active_boss_entity_id or 0)
        if current_parser_boss_id != last_parser_boss_id:
            active_state = packet_parser.current_active_boss_state()
            if active_state is not None:
                model.ingest_active_boss(active_state)
                if args.trace_resets:
                    print(
                        "TRACE_ACTIVE_BOSS "
                        f"line={line_count} previous={last_parser_boss_id} "
                        f"current={current_parser_boss_id} "
                        f"time={record.get('event_time')}"
                    )
            last_parser_boss_id = current_parser_boss_id

        try:
            event_now = (
                int(record.get("filetime_100ns", 0) or 0)
                - 116_444_736_000_000_000
            ) / 10_000_000
        except (TypeError, ValueError, OverflowError):
            event_now = 0.0
        if event_now > 0:
            model.finalize_if_idle(event_now)

    model.archive_current("replay_end")
    latest_by_id = {
        str(record.get("encounter_id", "")): record
        for record in model.completed_combats
        if isinstance(record, dict)
    }
    records = list(latest_by_id.values())
    selected = [
        record
        for record in records
        if not selected_templates
        or record_template_id(record) in selected_templates
    ]

    if args.anonymous_output is not None:
        evidence = anonymous_replay_evidence(
            source_path=args.log,
            line_count=line_count,
            records=selected,
            summaries=summaries,
            update_counts=update_counts,
        )
        args.anonymous_output.parent.mkdir(parents=True, exist_ok=True)
        args.anonymous_output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.anonymous_only:
        if args.anonymous_output is None:
            parser.error("--anonymous-only requires --anonymous-output")
        if args.repair_history_id:
            parser.error("--anonymous-only cannot be combined with history repair")
        return 0

    print(f"lines={line_count} records={len(records)} summaries={len(summaries)}")
    print("updates=" + json.dumps(update_counts, sort_keys=True))
    if args.trace_resets:
        print(
            "FINAL_STATE "
            f"session={model.session_number} target={model.combat_target_id} "
            f"monsters={len(model.monsters)} events={len(model.events)} "
            f"stats={actor_damage(model.build_combat_record() or {})} "
            f"active={model.active()} parser_boss={packet_parser.active_boss_entity_id}"
        )
        print(
            "FINAL_MONSTERS "
            + json.dumps(
                {
                    entity_id: {
                        "name": monster.name,
                        "type": monster.entity_type,
                        "template": monster.template_id,
                        "boss_type": monster.boss_type,
                        "boss_rank": monster.boss_rank,
                        "hp": monster.current_hp,
                        "max_hp": monster.max_hp,
                    }
                    for entity_id, monster in model.monsters.items()
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    for record in selected:
        accounting = record.get("damage_accounting", {})
        states = accounting.get("team_cumulative_states", []) if isinstance(accounting, dict) else []
        nonzero_baselines = {
            int(row.get("actor_id", 0) or 0): int(
                row.get("baseline_absolute", 0) or 0
            )
            for row in states
            if isinstance(row, dict)
            and int(row.get("baseline_absolute", 0) or 0) != 0
        }
        print(
            "COMBAT "
            f"id={record.get('encounter_id')} "
            f"template={record_template_id(record)} "
            f"start={record.get('started_at')} end={record.get('ended_at')} "
            f"reason={record.get('archive_reason')} "
            f"duration={float(record.get('duration_seconds', 0.0) or 0.0):.3f} "
            f"members={len(actor_damage(record))} "
            f"damage={sum(actor_damage(record).values())} "
            f"nonzero_baselines={nonzero_baselines}"
        )
        packet_by_actor = (
            accounting.get("packet_event_by_actor", [])
            if isinstance(accounting, dict)
            else []
        )
        packet_by_target = (
            accounting.get("packet_event_by_target", [])
            if isinstance(accounting, dict)
            else []
        )
        target_evidence = target_damage_evidence(record)
        print(
            "  TARGET_EVIDENCE "
            f"final={int(record.get('total_damage', 0) or 0)} "
            f"exact_packets={int(accounting.get('packet_event_total', 0) or 0)} "
            f"packet_actors={len(packet_by_actor)} "
            f"packet_targets={len(packet_by_target)} "
            f"assigned={target_evidence['assigned']} "
            f"unassigned={target_evidence['unassigned']}"
        )
        healers = [
            row for row in record.get("healers", []) if isinstance(row, dict)
        ]
        if healers:
            print(
                "  HEALING "
                f"effective={int(record.get('team_effective_healing', 0) or 0)} "
                f"total={record.get('team_total_healing')} "
                f"hps={float(record.get('team_hps', 0.0) or 0.0):.3f} "
                f"coverage={sorted({str(row.get('coverage', '')) for row in healers})} "
                f"response_samples={sum(int((row.get('response') or {}).get('samples', 0) or 0) for row in healers)}"
            )
        for actor_id, damage in sorted(
            actor_damage(record).items(), key=lambda item: item[1], reverse=True
        ):
            print(f"  actor={actor_id} damage={damage}")
            if args.show_skills:
                participant = next(
                    (
                        row
                        for row in record.get("participants", [])
                        if isinstance(row, dict)
                        and int(row.get("actor_id", 0) or 0) == actor_id
                    ),
                    {},
                )
                skills = [
                    row
                    for row in participant.get("skills", [])
                    if isinstance(row, dict)
                ]
                print(
                    "    skill_source="
                    f"{participant.get('skill_source', '')} "
                    f"classified={participant.get('classified_skill_damage', 0)} "
                    f"unclassified={participant.get('unclassified_damage', 0)} "
                    f"skills={len(skills)}"
                )
                print(
                    "    skill_damage="
                    + ", ".join(
                        f"{int(row.get('skill_id', 0) or 0)}:"
                        f"{int(row.get('damage', 0) or 0)}"
                        for row in skills
                    )
                )

    for summary in summaries:
        match = choose_record_for_summary(records, summary)
        expected = summary_damage(summary)
        if selected_templates and (
            match is None or record_template_id(match) not in selected_templates
        ):
            continue
        observed = actor_damage(match) if match else {}
        print(
            "SUMMARY "
            f"id={summary.get('summary_id')} "
            f"match={match.get('encounter_id') if match else '-'} "
            f"template={record_template_id(match) if match else 0} "
            f"expected={sum(expected.values())} "
            f"observed={sum(observed.values())} "
            f"difference={sum(expected.values()) - sum(observed.values())}"
        )
        for actor_id in sorted(
            set(expected) | set(observed),
            key=lambda value: expected.get(value, 0),
            reverse=True,
        ):
            difference = expected.get(actor_id, 0) - observed.get(actor_id, 0)
            print(
                f"  actor={actor_id} expected={expected.get(actor_id, 0)} "
                f"observed={observed.get(actor_id, 0)} difference={difference:+d}"
            )

    repair_id = str(args.repair_history_id).strip()
    if repair_id:
        if len(selected) != 1:
            parser.error("--repair-history-id requires exactly one selected combat")
        history_store = CombatHistoryStore(APP["HISTORY_DIR"])
        history_path = history_store.directory / (
            history_store._safe_encounter_id(repair_id) + ".json"
        )
        existing = read_object(history_path)
        repaired = dict(selected[0])
        repaired["encounter_id"] = repair_id
        for key in (
            "capture_pipeline_at_archive",
            "favorite",
            "saved_at_epoch",
            "saved_at",
        ):
            if key in existing:
                repaired[key] = existing[key]
        history_store.save(repaired)
        attached = 0
        for summary in summaries:
            if history_store.attach_stage_summary_validation(summary) is not None:
                attached += 1
        print(
            f"REPAIRED encounter={repair_id} damage={repaired.get('total_damage')} "
            f"attached_summaries={attached}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
