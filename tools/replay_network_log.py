#!/usr/bin/env python3
"""Replay a captured network log and report inferred-damage outliers."""

from __future__ import annotations

import argparse
import json
import runpy
import sys
from collections import Counter
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from monster_metadata import load_monster_metadata  # noqa: E402
from network_state import NetworkPacketParser  # noqa: E402


NATIVE_BOSS_FUNCTIONS = {
    "KAPI_Common_SetBossType",
    "CommonComponent_TemplateBossType",
    "CommonComponent_ExistingBossType",
}


def load_combat_model_class():
    module = runpy.run_path(str(PROJECT_DIR / "dps_meter.pyw"))
    return module["CombatModel"]


def apply_model_update(model, kind: str, update: dict) -> None:
    handlers = {
        "event": model.ingest,
        "identity": model.ingest_identity,
        "actor_merge": model.merge_actor,
        "party": model.ingest_party,
        "profile": model.ingest_profile,
        "monster": model.ingest_monster,
        "team_stat": model.ingest_team_stat,
        "stage_summary": model.ingest_stage_summary,
        "combat_state": model.ingest_combat_state,
        "life": model.ingest_life,
        "scene": model.ingest_scene,
        "name": model.ingest_name,
    }
    handler = handlers.get(kind)
    if handler is not None and isinstance(update, dict):
        handler(update)


def compact_combat_record(record: dict) -> dict:
    monster = record.get("monster", {})
    participants = []
    for row in record.get("participants", []):
        participants.append(
            {
                "actor_id": int(row.get("actor_id", 0) or 0),
                "name": str(row.get("name", "")),
                "damage": int(row.get("damage", 0) or 0),
                "dps": round(float(row.get("dps", 0) or 0), 2),
                "critical_rate": row.get("critical_rate"),
                "deaths": int(row.get("deaths", 0) or 0),
            }
        )
    return {
        "encounter_id": str(record.get("encounter_id", "")),
        "archive_reason": str(record.get("archive_reason", "")),
        "started_at": str(record.get("started_at", "")),
        "ended_at": str(record.get("ended_at", "")),
        "started_at_epoch": float(record.get("started_at_epoch", 0) or 0),
        "ended_at_epoch": float(record.get("ended_at_epoch", 0) or 0),
        "boss": str(monster.get("name", "")),
        "template_id": int(monster.get("template_id", 0) or 0),
        "duration": round(float(record.get("duration_seconds", 0) or 0), 3),
        "team_size": int(record.get("team_size", 0) or 0),
        "participant_count": len(participants),
        "total_damage": int(record.get("total_damage", 0) or 0),
        "participants": participants,
    }


def compact_parser_state(parser: NetworkPacketParser) -> dict:
    tokens = sorted(
        set(parser.token_actors)
        | set(parser.party_tokens)
        | set(parser.authoritative_party_tokens)
    )
    token_rows = []
    for token in tokens:
        actor_id = int(parser.token_actors.get(token, 0) or 0)
        profile = parser.entity_profiles.get(actor_id, {})
        cached = parser.team_profile_cache.get(token, {})
        token_rows.append(
            {
                "token": token,
                "actor_id": actor_id,
                "name": str(profile.get("name", "") or cached.get("name", "")),
                "profession_id": int(
                    profile.get("profession_id", 0)
                    or cached.get("profession_id", 0)
                    or 0
                ),
                "token_max_hp": parser.token_max_hp.get(token),
                "actor_max_hp": parser.entity_max_hp.get(actor_id),
                "actor_current_hp": parser.entity_current_hp.get(actor_id),
                "authoritative": token in parser.authoritative_party_tokens,
                "in_party": token in parser.party_tokens or token == parser.self_token,
            }
        )
    actor_rows = []
    for actor_id in sorted(parser.combat_source_actors | parser.player_attackers):
        profile = parser.entity_profiles.get(actor_id, {})
        token = parser.actor_tokens.get(actor_id, "")
        actor_rows.append(
            {
                "actor_id": actor_id,
                "token": token,
                "name": str(profile.get("name", "")),
                "profession_id": int(
                    profile.get("profession_id", 0)
                    or parser.actor_profession_hints.get(actor_id, 0)
                    or 0
                ),
                "max_hp": parser.entity_max_hp.get(actor_id),
                "current_hp": parser.entity_current_hp.get(actor_id),
            }
        )
    return {
        "self_id": parser.self_id,
        "self_token": parser.self_token,
        "party_member_count": parser.party_member_count,
        "tokens": token_rows,
        "combat_actors": actor_rows,
    }


def load_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_allowlist(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def log_has_native_damage(path: Path) -> bool:
    marker = '"function": "KAPI_HandleDamageSyncV2"'
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return any(marker in line for line in handle)


def replay(
    path: Path,
    *,
    combat_model_from_line: int | None = None,
    combat_model_to_line: int | None = None,
    stop_after_model_range: bool = False,
) -> dict:
    parser = NetworkPacketParser(
        load_json_object(PROJECT_DIR / "team_profiles.json"),
        load_monster_metadata(PROJECT_DIR / "monster_metadata.json"),
        boss_name_allowlist=load_allowlist(PROJECT_DIR / "boss_allowlist.txt"),
    )
    native_damage = log_has_native_damage(path)
    model = None
    if combat_model_from_line is not None:
        CombatModel = load_combat_model_class()
        model = CombatModel(run_id=f"replay-{path.stem}")
    records = 0
    decode_errors = 0
    update_counts: Counter[str] = Counter()
    inferred: list[dict] = []
    rejected_hp: list[dict] = []
    stage_summaries: list[dict] = []
    update_trace: list[dict] = []
    last_filetime_100ns = 0
    last_model_filetime_100ns = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if (
                stop_after_model_range
                and combat_model_to_line is not None
                and line_number > combat_model_to_line
            ):
                break
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            records += 1
            try:
                last_filetime_100ns = max(
                    last_filetime_100ns,
                    int(record.get("filetime_100ns", 0) or 0),
                )
            except (TypeError, ValueError, OverflowError):
                pass
            decode_errors += int("decode_error" in record)
            function = str(record.get("function", ""))
            method = str(record.get("method", ""))

            if function in NATIVE_BOSS_FUNCTIONS:
                updates = parser.process_native_boss_type(record)
            elif function == "KAPI_DataCache_CacheEntityName":
                name_update = parser.apply_runtime_boss_name(record)
                updates = [name_update] if name_update else []
            elif function == "KAPI_HandleDamageSyncV2":
                updates = parser.process_native_damage(record)
            else:
                before_hp = dict(parser.entity_current_hp)
                updates = parser.process(record, include_damage=not native_damage)
                if method in {"OnMsgSyncCurrentHp", "OnMsgSyncCurrentMaxHp"}:
                    args = record.get("decoded_arguments")
                    pointer = int(record.get("script_entity", 0) or 0)
                    entity_id = parser.pointer_entities.get(pointer)
                    if entity_id and isinstance(args, list) and args:
                        try:
                            incoming_hp = float(args[0])
                        except (TypeError, ValueError, OverflowError):
                            incoming_hp = None
                        if (
                            incoming_hp is not None
                            and before_hp.get(entity_id) == parser.entity_current_hp.get(entity_id)
                            and before_hp.get(entity_id) != incoming_hp
                        ):
                            rejected_hp.append(
                                {
                                    "line": line_number,
                                    "time": record.get("event_time", ""),
                                    "entity_id": entity_id,
                                    "previous_hp": before_hp.get(entity_id),
                                    "rejected_hp": incoming_hp,
                                }
                            )

            for kind, update in updates:
                update_counts[kind] += 1
                if kind in {"actor_merge", "identity", "party", "profile", "scene"}:
                    compact_update = {
                        key: update.get(key)
                        for key in (
                            "entity_id",
                            "from_actor_id",
                            "to_actor_id",
                            "name",
                            "profession_id",
                            "user_token",
                            "entity_ids",
                            "member_count",
                            "authoritative",
                            "scene_id",
                            "force_reset",
                        )
                        if key in update
                    }
                    update_trace.append(
                        {
                            "line": line_number,
                            "time": update.get("event_time", ""),
                            "kind": kind,
                            "update": compact_update,
                        }
                    )
                if kind == "stage_summary":
                    stage_summaries.append(
                        {
                            "line": line_number,
                            "time": update.get("event_time", ""),
                            "authoritative": bool(update.get("authoritative")),
                            "member_count": int(update.get("member_count", 0) or 0),
                            "actors": list(update.get("actors", [])),
                        }
                    )
                if model is not None:
                    bootstrap_kind = kind in {
                        "identity",
                        "actor_merge",
                        "party",
                        "profile",
                        "monster",
                        "scene",
                        "name",
                    }
                    inside_model_range = bool(
                        line_number >= int(combat_model_from_line or 1)
                        and (
                            combat_model_to_line is None
                            or line_number <= combat_model_to_line
                        )
                    )
                    bootstrap_phase = line_number < int(
                        combat_model_from_line or 1
                    )
                    if (bootstrap_kind and bootstrap_phase) or inside_model_range:
                        apply_model_update(model, kind, update)
                        if inside_model_range:
                            try:
                                last_model_filetime_100ns = max(
                                    last_model_filetime_100ns,
                                    int(update.get("filetime_100ns", 0) or 0),
                                )
                            except (TypeError, ValueError, OverflowError):
                                pass
                if (
                    kind == "event"
                    and str(update.get("function", "")).endswith("/team-hit")
                ):
                    inferred.append(
                        {
                            "line": line_number,
                            "time": update.get("event_time", ""),
                            "attacker_id": int(update.get("attacker_id", 0) or 0),
                            "target_id": int(update.get("target_id", 0) or 0),
                            "skill_id": int(update.get("skill_id", 0) or 0),
                            "damage": int(update.get("damage", 0) or 0),
                        }
                    )

    if model is not None and last_model_filetime_100ns:
        final_time = (
            last_model_filetime_100ns - 116_444_736_000_000_000
        ) / 10_000_000
        model.finalize_if_idle(final_time + model.encounter_gap + 1.0)
    combat_records = (
        [
            compact_combat_record(record)
            for record in model.pop_completed_combats()
        ]
        if model is not None
        else []
    )
    model_state = None
    if model is not None:
        model_actor_ids = set(model.entity_names) | set(model.stats)
        model_state = {
            "self_id": model.self_id,
            "party_ids": sorted(model.party_ids),
            "party_member_count": model.party_member_count,
            "names": {
                str(actor_id): model.entity_names.get(actor_id, "")
                for actor_id in sorted(model_actor_ids)
            },
            "professions": {
                str(actor_id): model.entity_professions.get(actor_id, 0)
                for actor_id in sorted(model_actor_ids)
                if model.entity_professions.get(actor_id, 0)
            },
        }
    combat_anomalies: list[dict] = []
    for combat in combat_records:
        participants = combat["participants"]
        actor_ids = [row["actor_id"] for row in participants]
        if (
            combat["team_size"] > 12
            or combat["participant_count"] > 12
            or len(actor_ids) != len(set(actor_ids))
            or combat["participant_count"] > combat["team_size"]
        ):
            combat_anomalies.append(
                {
                    "encounter_id": combat["encounter_id"],
                    "boss": combat["boss"],
                    "team_size": combat["team_size"],
                    "participant_count": combat["participant_count"],
                    "actor_ids": actor_ids,
                }
            )

    return {
        "log": str(path.resolve()),
        "records": records,
        "decode_errors": decode_errors,
        "native_damage": native_damage,
        "update_counts": dict(update_counts),
        "inferred_event_count": len(inferred),
        "inferred_damage_total": sum(item["damage"] for item in inferred),
        "largest_inferred_events": sorted(
            inferred, key=lambda item: item["damage"], reverse=True
        )[:20],
        "rejected_hp_samples": rejected_hp[-50:],
        "stage_summaries": stage_summaries,
        "update_trace": update_trace,
        "parser_state": compact_parser_state(parser),
        "model_state": model_state,
        "combat_count": len(combat_records),
        "combat_anomalies": combat_anomalies,
        "combats": combat_records,
    }


def main() -> int:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("log", type=Path)
    argument_parser.add_argument("--output", type=Path)
    argument_parser.add_argument("--combat-model-from-line", type=int)
    argument_parser.add_argument("--combat-model-to-line", type=int)
    argument_parser.add_argument("--stop-after-model-range", action="store_true")
    args = argument_parser.parse_args()
    result = replay(
        args.log,
        combat_model_from_line=args.combat_model_from_line,
        combat_model_to_line=args.combat_model_to_line,
        stop_after_model_range=args.stop_after_model_range,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
