"""Project passive settlement encounters into the existing history format.

The encounter journal remains authoritative for matching and settlement state.
Projected records let the established history browser render the same encounter
without maintaining a second production UI or inventing unavailable values.
"""
from __future__ import annotations

from copy import deepcopy
from collections import deque
import datetime as dt
import math

from combat_history import rebase_relative_combat_logs
from encounter_tracker import EncounterRecord, EncounterTracker
from history_index import rebuild_team_dps_timeline
from network_state import boss_template_continues_encounter


SETTLEMENT_HISTORY_SOURCE = "passive_npcap_settlement"
_RESULTS = {
    "VICTORY": ("success", "target_defeated"),
    "WIPE": ("wipe", "party_wipe"),
    "RESET": ("interrupted", "scene_change"),
}


def _epoch_text(value: float | None) -> str:
    if value is None:
        return ""
    return dt.datetime.fromtimestamp(value).astimezone().isoformat()


def _actor_id(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed > 0 else 0


def visible_encounter_member_tokens(
    encounter: EncounterRecord, *, statistics_scope: str = "STAGE"
) -> list[str]:
    members = {
        str(row.get("id")): row
        for row in encounter.participants
        if isinstance(row, dict) and row.get("id")
    }
    statistics = (
        encounter.all_statistics
        if str(statistics_scope).upper() == "ALL"
        else encounter.stage_statistics
    )
    server_tokens = {
        str(row.get("id"))
        for row in (statistics or {}).get("members", ())
        if isinstance(row, dict) and row.get("id")
    }
    if str(statistics_scope).upper() == "ALL":
        return list(server_tokens)
    visible = []
    for identity in encounter.participants_snapshot:
        if not isinstance(identity, dict) or not identity.get("id"):
            continue
        token = str(identity["id"])
        member = members.get(token, {})
        if (
            identity.get("is_ai") is True
            and not _actor_id(identity.get("iid") or member.get("iid"))
            and token not in server_tokens
            and all(member.get(key) is None for key in ("damage", "bear", "heal"))
        ):
            continue
        visible.append(token)
    return visible


def _optional_nonnegative(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _optional_nonnegative_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _rate(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    if not 0 <= numerator <= denominator:
        return None
    return numerator / denominator


def _counter_total(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    amounts = [_optional_nonnegative(amount) for amount in value.values()]
    if any(amount is None for amount in amounts):
        return None
    return sum(amounts)


def _life_summary(record: EncounterRecord, token: str) -> tuple[int, int, float]:
    events = sorted(record.life_events.get(token, []))
    dead = False
    deaths = revives = 0
    dead_since: int | None = None
    dead_ns = 0
    for timestamp, now_dead in events:
        if timestamp < record.started_at_ns:
            continue
        if record.ended_at_ns is not None and timestamp > record.ended_at_ns:
            continue
        if now_dead and not dead:
            deaths += 1
            dead_since = timestamp
        elif not now_dead and dead:
            revives += 1
            if dead_since is not None:
                dead_ns += max(0, timestamp - dead_since)
            dead_since = None
        dead = bool(now_dead)
    if dead and dead_since is not None and record.ended_at_ns is not None:
        dead_ns += max(0, record.ended_at_ns - dead_since)
    return deaths, revives, dead_ns / 1_000_000_000


def _server_damage_skills(member: dict, total: int | None) -> list[dict]:
    damage_map = member.get("skill_damage")
    count_map = member.get("skill_count")
    damage_map = damage_map if isinstance(damage_map, dict) else {}
    count_map = count_map if isinstance(count_map, dict) else {}
    rows = []
    for raw_skill_id in sorted(
        set(damage_map) | set(count_map),
        key=lambda key: (-int(damage_map.get(key, 0) or 0), str(key)),
    ):
        skill_id = _actor_id(raw_skill_id)
        if not skill_id:
            continue
        damage = _optional_nonnegative(damage_map.get(raw_skill_id))
        count = _optional_nonnegative(count_map.get(raw_skill_id))
        rows.append(
            {
                "skill_id": skill_id,
                "name": f"技能 {skill_id}",
                "damage": damage,
                "share": (
                    damage / total
                    if damage is not None and total is not None and total > 0
                    else None
                ),
                "hits": count,
                "server_skill_count": count,
                "count_semantics": "server_skill_count",
                "max_hit": None,
                "source": "server_stage_statistics",
            }
        )
    gap = _optional_nonnegative(member.get("unclassified_damage"))
    if gap:
        rows.append(
            {
                "skill_id": 0,
                "name": "未归类伤害",
                "damage": gap,
                "share": gap / total if total else 0.0,
                "hits": None,
                "server_skill_count": None,
                "count_semantics": "unavailable",
                "max_hit": None,
                "source": "server_total_remainder",
            }
        )
    return rows


def _server_healing_skills(member: dict, total: int | None) -> list[dict]:
    heal_map = member.get("skill_heal")
    heal_map = heal_map if isinstance(heal_map, dict) else {}
    rows = []
    for raw_skill_id, raw_heal in sorted(
        heal_map.items(), key=lambda item: -int(item[1] or 0)
    ):
        skill_id = _actor_id(raw_skill_id)
        healing = _optional_nonnegative(raw_heal)
        if not skill_id or healing is None:
            continue
        rows.append(
            {
                "skill_id": skill_id,
                "name": f"技能 {skill_id}",
                "total_healing": None,
                "effective_healing": healing,
                "overhealing": None,
                "share": (
                    healing / total
                    if total is not None and total > 0
                    else None
                ),
                "events": None,
                "server_skill_count": _optional_nonnegative(
                    (member.get("skill_count") or {}).get(raw_skill_id)
                    if isinstance(member.get("skill_count"), dict)
                    else None
                ),
                "source": "server_stage_statistics",
            }
        )
    return rows


def _base_actor_rows(base: dict, collection_name: str) -> dict[int, dict]:
    rows = base.get(collection_name) if isinstance(base, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        _actor_id(row.get("actor_id")): deepcopy(row)
        for row in rows
        if isinstance(row, dict)
        and _actor_id(row.get("actor_id"))
    }


def encounter_history_record(
    encounter: EncounterRecord, base_record: dict | None = None
) -> dict:
    """Return one null-preserving record for the established history UI."""
    base = deepcopy(base_record) if isinstance(base_record, dict) else {}
    base_monster = (
        base.get("monster") if isinstance(base.get("monster"), dict) else {}
    )
    base_template = _actor_id(base_monster.get("template_id"))
    encounter_template = _actor_id(encounter.boss_template_id)
    if (
        base_template
        and encounter_template
        and not boss_template_continues_encounter(
            encounter_template, base_template
        )
        and not boss_template_continues_encounter(
            base_template, encounter_template
        )
    ):
        # An old explicit binding can point at another Boss's local archive.
        # Do not inherit its entity, HP, targets, or local player damage.
        base = {}
    existing_participants = _base_actor_rows(base, "participants")
    existing_healers = _base_actor_rows(base, "healers")
    existing_taken = _base_actor_rows(base, "damage_taken")
    existing_equipment = {}
    for collection_name in ("participants", "healers", "damage_taken"):
        collection = base.get(collection_name)
        if not isinstance(collection, list):
            continue
        for row in collection:
            if not isinstance(row, dict) or not row.get("user_token"):
                continue
            snapshot = row.get("equipment_snapshot")
            if isinstance(snapshot, dict):
                existing_equipment[str(row["user_token"])] = deepcopy(snapshot)
    settled = encounter.settlement_status == "SETTLED"
    members = {
        str(row.get("id")): row
        for row in encounter.participants
        if isinstance(row, dict) and row.get("id")
    }
    visible_tokens = set(visible_encounter_member_tokens(encounter))
    total_damage = None
    duration = encounter.encounter_duration_seconds
    if duration is not None and (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration <= 0
    ):
        duration = None

    participants = []
    healers = []
    taken_rows = []
    healing_values = []
    taken_values = []
    for identity in encounter.participants_snapshot:
        if not isinstance(identity, dict) or not identity.get("id"):
            continue
        token = str(identity["id"])
        values = members.get(token, {})
        actor = _actor_id(identity.get("iid") or values.get("iid"))
        is_self = token == encounter.self_token
        local = existing_participants.get(actor, {})
        if token not in visible_tokens:
            continue
        damage = _optional_nonnegative(values.get("damage")) if settled else None
        if not settled and local:
            damage = _optional_nonnegative(local.get("damage"))
        dps = damage / duration if settled and damage is not None and duration else None
        if not settled and local and damage is not None:
            # Team settlement readiness must not erase the independently
            # observed self DPS. Keep its local rate/clock separate from the
            # still-unknown official common duration and teammate values.
            dps = _optional_nonnegative_float(local.get("dps"))
            if dps is None:
                local_duration = _optional_nonnegative_float(
                    base.get("dps_duration_seconds")
                ) or _optional_nonnegative_float(base.get("duration_seconds"))
                if local_duration:
                    dps = damage / local_duration
        deaths, revives, dead_seconds = _life_summary(encounter, token)
        if settled:
            deaths = encounter.death_count(token)
        row = deepcopy(local) if local else {}
        row.update(
            {
                "actor_id": actor,
                "user_token": token,
                "name": values.get("name") or identity.get("name") or row.get("name") or "",
                "is_self": is_self,
                "profession_id": values.get("profession_id") or identity.get("profession_id") or row.get("profession_id") or 0,
                "extraordinary_rating": identity.get("extraordinary_rating", row.get("extraordinary_rating")),
                "is_ai": bool(identity.get("is_ai", row.get("is_ai", False))),
                "damage": damage,
                "dps": dps,
                "share": damage / total_damage if damage is not None and total_damage else None,
                "deaths": deaths,
                "revives": revives,
                "death_duration_seconds": dead_seconds,
                "member_battle_length": values.get("member_battle_length") if settled else None,
                "settlement_status": encounter.settlement_status,
                "settlement_hint": (
                    "延迟补齐"
                    if settled and encounter.result == "WIPE"
                    else "已结算"
                    if settled
                    else "未取得结算"
                    if encounter.settlement_status == "ABANDONED"
                    else "等待结算"
                ),
                "data_source": "server_stage_statistics" if settled else "verified_local_observation" if local else "unavailable",
            }
        )
        if token in existing_equipment:
            row["equipment_snapshot"] = deepcopy(existing_equipment[token])
        if settled:
            hits = _optional_nonnegative(values.get("damage_count"))
            critical = _optional_nonnegative(values.get("damage_critical_count"))
            excluded = _optional_nonnegative(values.get("penetration_excluded_count"))
            if hits is not None and excluded is None:
                excluded = 0
            local_damage = _optional_nonnegative(local.get("damage"))
            local_targets = local.get("targets")
            exact_local_targets = (
                local_damage == damage
                and isinstance(local_targets, list)
                and sum(
                    _optional_nonnegative(target.get("damage")) or 0
                    for target in local_targets
                    if isinstance(target, dict)
                ) == damage
            )
            row.update(
                {
                    "damage_hits": hits,
                    "critical_hits": critical if _rate(critical, hits) is not None else None,
                    "critical_rate": _rate(critical, hits),
                    "penetration_hits": hits - excluded if _rate(excluded, hits) is not None else None,
                    "penetration_rate": (
                        1.0 - _rate(excluded, hits)
                        if _rate(excluded, hits) is not None
                        else None
                    ),
                    "classified_skill_damage": _counter_total(
                        values.get("skill_damage")
                    ),
                    "unclassified_damage": values.get("unclassified_damage"),
                    "server_damage_difference": values.get("unclassified_damage"),
                    "skill_detail_status": "server_settlement",
                    "skill_source": "server_stage_statistics",
                    "skills": _server_damage_skills(values, damage),
                    "targets": (
                        deepcopy(local_targets) if exact_local_targets else []
                    ),
                    "local_detail_available": bool(
                        local
                        and _optional_nonnegative(local.get("damage")) == damage
                    ),
                }
            )
        else:
            row["share"] = None
            row["local_detail_available"] = bool(local)
            if not local:
                row.update(
                    {
                        "damage_hits": None,
                        "critical_hits": None,
                        "critical_rate": None,
                        "penetration_hits": None,
                        "penetration_rate": None,
                        "skill_detail_status": "pending_server_detail",
                        "skills": [],
                        "targets": [],
                    }
                )
        participants.append(row)

        bear = _optional_nonnegative(values.get("bear")) if settled else None
        local_taken = existing_taken.get(actor, {})
        if not settled and local_taken:
            bear = _optional_nonnegative(local_taken.get("taken"))
        taken_values.append(bear)
        taken_row = deepcopy(local_taken) if local_taken else {}
        taken_row.update(
            {
                "actor_id": actor,
                "name": row["name"],
                "is_self": is_self,
                "profession_id": row["profession_id"],
                "extraordinary_rating": row.get("extraordinary_rating"),
                "is_ai": row["is_ai"],
                "taken": bear,
                "share": None,
                "source": (
                    "server_stage_statistics"
                    if settled and bear is not None
                    else str(local_taken.get("source") or "verified_local_observation")
                    if bear is not None
                    else "unavailable"
                ),
            }
        )
        taken_rows.append(taken_row)
        heal = _optional_nonnegative(values.get("heal")) if settled else None
        local_healer = existing_healers.get(actor, {})
        if not settled and local_healer:
            heal = _optional_nonnegative(local_healer.get("effective_healing"))
        healing_values.append(heal)
        include_healer = (
            bool(local_healer) or not existing_healers
            if not settled
            else heal is None or heal > 0
        )
        if include_healer:
            healer_row = deepcopy(local_healer) if local_healer else {}
            healer_row.update(
                {
                    "actor_id": actor,
                    "name": row["name"],
                    "is_self": is_self,
                    "profession_id": row["profession_id"],
                    "extraordinary_rating": row.get("extraordinary_rating"),
                    "is_ai": row["is_ai"],
                    "hps": (
                        heal / duration
                        if settled and heal is not None and duration
                        else _optional_nonnegative_float(local_healer.get("hps"))
                        if heal is not None
                        else None
                    ),
                    "total_healing": (
                        None
                        if settled
                        else _optional_nonnegative(local_healer.get("total_healing"))
                    ),
                    "effective_healing": heal,
                    "overhealing": (
                        None
                        if settled
                        else _optional_nonnegative(local_healer.get("overhealing"))
                    ),
                    "overheal_rate": (
                        None
                        if settled
                        else _optional_nonnegative_float(local_healer.get("overheal_rate"))
                    ),
                    "skills": (
                        _server_healing_skills(values, heal)
                        if settled
                        else deepcopy(local_healer.get("skills", []))
                        if isinstance(local_healer.get("skills"), list)
                        else []
                    ),
                    "targets": (
                        []
                        if settled
                        else deepcopy(local_healer.get("targets", []))
                        if isinstance(local_healer.get("targets"), list)
                        else []
                    ),
                    "coverage": (
                        "server_stage_statistics"
                        if settled and heal is not None
                        else str(
                            local_healer.get("coverage")
                            or "verified_local_observation"
                        )
                        if heal is not None
                        else "unavailable"
                    ),
                }
            )
            healers.append(healer_row)

    damage_values = [row.get("damage") for row in participants]
    if damage_values and all(value is not None for value in damage_values):
        total_damage = sum(int(value) for value in damage_values)
        if total_damage > 0:
            for row in participants:
                row["share"] = int(row["damage"]) / total_damage

    team_taken = (
        sum(taken_values)
        if taken_values and all(value is not None for value in taken_values)
        else None
    )
    if team_taken is not None and team_taken > 0:
        for row in taken_rows:
            if row["taken"] is not None:
                row["share"] = row["taken"] / team_taken
    team_healing = (
        sum(healing_values)
        if healing_values and all(value is not None for value in healing_values)
        else None
    )
    if not settled:
        base_team_healing = _optional_nonnegative(
            base.get("team_effective_healing")
        )
        known_healing = [value for value in healing_values if value is not None]
        if (
            base_team_healing is not None
            and sum(known_healing) == base_team_healing
        ):
            team_healing = base_team_healing
        base_team_taken = _optional_nonnegative(base.get("team_taken"))
        known_taken = [value for value in taken_values if value is not None]
        if base_team_taken is not None and sum(known_taken) == base_team_taken:
            team_taken = base_team_taken
    result, archive_reason = _RESULTS.get(
        encounter.result, ("interrupted", "scene_change")
    )
    started = encounter.started_at_ns / 1_000_000_000
    ended = (
        encounter.ended_at_ns / 1_000_000_000
        if encounter.ended_at_ns is not None
        else None
    )
    monster = dict(base.get("monster") or {})
    monster.update(
        {
            "name": encounter.boss_name or monster.get("name") or "Boss",
            "template_id": encounter.boss_template_id or monster.get("template_id") or 0,
            "boss_type": 3,
            "boss_rank": 3,
        }
    )
    # A delayed server table can arrive after the next pull has already reset
    # the live Boss to full HP.  The Encounter-owned end snapshot is bound to
    # the original pull, so it must replace any later CurrentHP inherited from
    # the mutable local archive.  MaxHP can safely fall back to the archived
    # declaration when the end sample carried only CurrentHP.
    if encounter.boss_current_hp is not None:
        monster["current_hp"] = encounter.boss_current_hp
    if encounter.boss_max_hp is not None:
        monster["max_hp"] = encounter.boss_max_hp
        monster["observed_max_hp"] = encounter.boss_max_hp
    if str(encounter.result or "").upper() == "VICTORY":
        # A victory is the authoritative terminal boundary.  Older journals
        # can retain the last positive passive HP sample when the final HP=0
        # packet was missed; never project that stale value into history/UI.
        monster["current_hp"] = 0
    if encounter.boss_health_observed_at_ns is not None:
        monster["health_observed_at_ns"] = encounter.boss_health_observed_at_ns
    if encounter.boss_health_source:
        monster["health_source"] = encounter.boss_health_source
    output = base
    visible_tokens = {row["user_token"] for row in participants}
    visible_identities = [
        deepcopy(row) for row in encounter.participants_snapshot
        if isinstance(row, dict) and row.get("id") in visible_tokens
    ]
    base_duration = _optional_nonnegative_float(base.get("duration_seconds"))
    if duration is None and base_duration:
        duration = base_duration
    base_duration_source = str(base.get("duration_source") or "").strip()
    duration_source = (
        encounter.duration_source
        if encounter.encounter_duration_seconds is not None
        else base_duration_source or encounter.duration_source
    )
    stage_id = encounter.stage_id
    if stage_id is None:
        stage_id = _optional_nonnegative(
            base.get("stage_id")
            if base.get("stage_id") is not None
            else base.get("dungeon_stage_id")
        )
    dungeon_id = encounter.dungeon_id
    if dungeon_id is None:
        dungeon_id = _optional_nonnegative(base.get("dungeon_id"))
    map_id = encounter.map_id
    if map_id is None:
        map_id = _optional_nonnegative(base.get("map_id"))
    damage_accounting = {
        "source": "server_stage_statistics" if settled else "pending_server_statistics",
        "snapshot_semantics": "upsert_not_increment",
        "statistics_scope": "STAGE",
        "server_battle_id": encounter.server_battle_id,
        "match_confidence": encounter.match_confidence,
    }
    if not settled and total_damage is not None and isinstance(
        base.get("damage_accounting"), dict
    ):
        damage_accounting = deepcopy(base["damage_accounting"])

    output.update(
        {
            "encounter_id": encounter.local_encounter_id,
            "battle_id": encounter.local_encounter_id,
            "local_encounter_id": encounter.local_encounter_id,
            "server_battle_id": encounter.server_battle_id,
            "source": SETTLEMENT_HISTORY_SOURCE,
            "data_source": encounter.data_source,
            "archive_reason": archive_reason,
            "result": result,
            "started_at_epoch": started,
            "ended_at_epoch": ended,
            "started_at": _epoch_text(started),
            "ended_at": _epoch_text(ended),
            "duration_seconds": duration,
            "dps_duration_seconds": duration,
            "hps_duration_seconds": duration,
            "duration_source": duration_source,
            "encounter_duration_seconds": duration,
            "total_damage": total_damage,
            "team_dps": total_damage / duration if total_damage is not None and duration else None,
            "team_effective_healing": team_healing,
            "team_hps": team_healing / duration if team_healing is not None and duration else None,
            "team_taken": team_taken,
            "team_size": len(participants),
            "target_filter": "boss",
            "monster": monster,
            "participants": participants,
            "healers": healers,
            "damage_taken": taken_rows,
            "participant_identities": visible_identities,
            "settlement_status": encounter.settlement_status,
            "settlement_source": encounter.data_source,
            "settlement_received_at": (
                encounter.settlement_received_at_ns / 1_000_000_000
                if encounter.settlement_received_at_ns is not None
                else None
            ),
            "statistics_scope": "STAGE",
            "stage_id": stage_id,
            "dungeon_id": dungeon_id,
            "map_id": map_id,
            "dungeon_stage_id": stage_id,
            "dungeon_stage_phase": encounter.stage_index,
            "boss_token": encounter.boss_token,
            "match_confidence": encounter.match_confidence,
            "participants_snapshot": deepcopy(visible_identities),
            "settlement_revision": encounter.revision,
            "capture_complete": encounter.capture_complete,
            "completion_confirmed": bool(
                encounter.result == "VICTORY" and settled
            ),
            "damage_accounting": damage_accounting,
        }
    )
    return output


def _record_result(record: dict) -> str | None:
    result = str(record.get("result", "")).casefold()
    reason = str(record.get("archive_reason", "")).casefold()
    if result in {"success", "defeated", "completed"} or reason in {
        "target_defeated",
        "boss_defeated",
        "stage_completed",
    }:
        return "VICTORY"
    if result in {"wipe", "failed"} or reason in {"party_wipe", "wipe"}:
        return "WIPE"
    if result in {"interrupted", "reset"}:
        return "RESET"
    return None


def match_local_history_record(
    tracker: EncounterTracker, record: dict
) -> EncounterRecord | None:
    """Strictly bind a local archive to one independently observed encounter."""
    existing = str(record.get("local_encounter_id", "") or "")
    if existing:
        return tracker.encounters.get(existing)
    try:
        started = float(record.get("started_at_epoch"))
        ended = float(record.get("ended_at_epoch"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(started) or not math.isfinite(ended):
        return None
    expected_result = _record_result(record)
    monster = record.get("monster") if isinstance(record.get("monster"), dict) else {}
    template_id = _actor_id(monster.get("template_id"))
    local_actors = {
        _actor_id(row.get("actor_id"))
        for row in record.get("participants", [])
        if isinstance(row, dict) and _actor_id(row.get("actor_id"))
    }
    candidates = []
    for encounter in tracker.encounters.values():
        if encounter.ended_at_ns is None or encounter.history_deleted:
            continue
        encounter_started = encounter.started_at_ns / 1_000_000_000
        encounter_ended = encounter.ended_at_ns / 1_000_000_000
        if abs(encounter_ended - ended) > 5.0 or abs(encounter_started - started) > 30.0:
            continue
        if expected_result is not None and encounter.result != expected_result:
            continue
        if (
            template_id
            and encounter.boss_template_id
            and not boss_template_continues_encounter(
                encounter.boss_template_id, template_id
            )
            and not boss_template_continues_encounter(
                template_id, encounter.boss_template_id
            )
        ):
            continue
        encounter_actors = {
            _actor_id(row.get("iid"))
            for row in encounter.participants_snapshot
            if isinstance(row, dict) and _actor_id(row.get("iid"))
        }
        if local_actors and encounter_actors and not local_actors.intersection(encounter_actors):
            continue
        candidates.append(encounter)
    return candidates[0] if len(candidates) == 1 else None


class SettlementHistoryAdapter:
    def __init__(self, history_store, *, controller=None):
        self.history_store = history_store
        self.controller = controller
        self.local_bindings: dict[str, str] = {}
        self.live_team_dps_samples: dict[str, list[tuple[int, float]]] = {}
        self.observed_casts = deque(maxlen=20_000)
        self.observed_hits = deque(maxlen=50_000)
        self.observed_heals = deque(maxlen=50_000)
        self.details_dirty = set()

    def observe_detail(self, kind: str, update: dict) -> None:
        """Keep detached events even when the local damage model starts late."""
        if kind == 'skill_cast':
            self.observed_casts.append(dict(update))
        elif kind == 'event':
            self.observed_hits.append(dict(update))
        elif kind == 'heal':
            self.observed_heals.append(dict(update))
        else:
            return
        tracker = getattr(self.controller, 'tracker', None)
        timestamp_ns = (int(update.get('filetime_100ns', 0) or 0)
                        - 116_444_736_000_000_000) * 100
        for encounter in getattr(tracker, 'encounters', {}).values():
            if (encounter.ended_at_ns is not None
                    and encounter.started_at_ns <= timestamp_ns <= encounter.ended_at_ns):
                self.details_dirty.add(encounter.local_encounter_id)

    def _attach_observed_healing(
        self, record: dict, *, started: float, ended: float
    ) -> dict:
        """Add exact callback detail without replacing authoritative totals."""

        participant_rows = {
            _actor_id(row.get('actor_id')): row
            for row in record.get('participants', ())
            if isinstance(row, dict) and _actor_id(row.get('actor_id'))
        }
        if not participant_rows:
            return record
        observed: dict[int, dict] = {}
        seen = set()
        for event in self.observed_heals:
            try:
                timestamp_100ns = int(event.get('filetime_100ns', 0) or 0)
                sequence = int(event.get('sequence', 0) or 0)
                healer_id = _actor_id(event.get('healer_id'))
                target_id = _actor_id(event.get('target_id'))
                skill_id = _actor_id(event.get('skill_id'))
                total = int(event.get('total_healing', 0) or 0)
                effective = int(event.get('effective_healing', 0) or 0)
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
            timestamp = (
                timestamp_100ns - 116_444_736_000_000_000
            ) / 10_000_000
            if (
                healer_id not in participant_rows
                or target_id not in participant_rows
                or not started <= timestamp <= ended
                or skill_id <= 0
                or total < 0
                or not 0 <= effective <= total
            ):
                continue
            key = (
                timestamp_100ns, sequence, healer_id, target_id, skill_id,
                total, effective,
            )
            if key in seen:
                continue
            seen.add(key)
            actor = observed.setdefault(
                healer_id,
                {
                    'total': 0,
                    'effective': 0,
                    'events': 0,
                    'skills': {},
                    'targets': {},
                    'effective_events': [],
                },
            )
            actor['total'] += total
            actor['effective'] += effective
            actor['events'] += 1
            if effective > 0:
                actor['effective_events'].append((timestamp, effective))
            for collection, detail_id in (
                (actor['skills'], skill_id), (actor['targets'], target_id)
            ):
                detail = collection.setdefault(
                    detail_id, {'total': 0, 'effective': 0, 'events': 0}
                )
                detail['total'] += total
                detail['effective'] += effective
                detail['events'] += 1
        if not observed:
            return record

        duration = _optional_nonnegative_float(record.get('hps_duration_seconds'))
        if duration is None:
            duration = _optional_nonnegative_float(record.get('duration_seconds'))
        healer_rows = [
            row for row in record.get('healers', ()) if isinstance(row, dict)
        ]
        healers = {
            _actor_id(row.get('actor_id')): row
            for row in healer_rows
            if _actor_id(row.get('actor_id'))
        }
        for actor_id, raw in observed.items():
            healer = healers.get(actor_id)
            if healer is None:
                participant = participant_rows[actor_id]
                healer = {
                    key: deepcopy(participant.get(key))
                    for key in (
                        'actor_id', 'user_token', 'name', 'is_self',
                        'profession_id', 'extraordinary_rating', 'is_ai',
                    )
                }
                healer.update({
                    'effective_healing': int(raw['effective']),
                    'hps': (
                        int(raw['effective']) / duration if duration else None
                    ),
                    'total_healing': int(raw['total']),
                    'overhealing': int(raw['total']) - int(raw['effective']),
                })
                healer_rows.append(healer)
                healers[actor_id] = healer

            authoritative_effective = _optional_nonnegative(
                healer.get('effective_healing')
            )
            if authoritative_effective is None:
                authoritative_effective = int(raw['effective'])
                healer['effective_healing'] = authoritative_effective
                healer['hps'] = (
                    authoritative_effective / duration if duration else None
                )
                healer['total_healing'] = int(raw['total'])
                healer['overhealing'] = (
                    int(raw['total']) - int(raw['effective'])
                )
                authoritative_total = False
            else:
                authoritative_total = str(
                    healer.get('coverage') or ''
                ).startswith('server_')

            observed_total = int(raw['total'])
            observed_effective = int(raw['effective'])
            observed_overhealing = observed_total - observed_effective
            observed_overheal_rate = (
                observed_overhealing / observed_total
                if observed_total > 0 else None
            )
            healer.update({
                'observed_total_healing': observed_total,
                'observed_effective_healing': observed_effective,
                'observed_overhealing': observed_overhealing,
                'observed_overheal_rate': observed_overheal_rate,
                'events': int(raw['events']),
                'coverage': (
                    'server_team_counter_with_observed_callbacks'
                    if authoritative_total
                    else 'live_exact_callbacks_unverified'
                ),
            })
            if healer.get('overheal_rate') is None:
                healer['overheal_rate'] = observed_overheal_rate
                healer['overheal_rate_partial'] = authoritative_total
                healer['overheal_rate_source'] = (
                    'observed_partial' if authoritative_total else 'complete'
                )

            existing_skills = {
                _actor_id(row.get('skill_id')): row
                for row in healer.get('skills', ())
                if isinstance(row, dict) and _actor_id(row.get('skill_id'))
            }
            server_skills = {
                skill_id: deepcopy(row)
                for skill_id, row in existing_skills.items()
                if str(row.get('source') or '').startswith('server_')
                and row.get('source') != 'server_total_minus_observed_callbacks'
            }
            skill_rows = list(server_skills.values())
            for skill_id, values in sorted(
                raw['skills'].items(),
                key=lambda item: int(item[1]['total']),
                reverse=True,
            ):
                skill_total = int(values['total'])
                skill_effective = int(values['effective'])
                if skill_id in server_skills:
                    row = server_skills[skill_id]
                    row.update({
                        'observed_total_healing': skill_total,
                        'observed_effective_healing': skill_effective,
                        'observed_overhealing': skill_total - skill_effective,
                        'observed_events': int(values['events']),
                    })
                    continue
                skill_rows.append({
                    'skill_id': skill_id,
                    'name': str(
                        existing_skills.get(skill_id, {}).get('name')
                        or f'技能 {skill_id}'
                    ),
                    'total_healing': skill_total,
                    'effective_healing': skill_effective,
                    'overhealing': skill_total - skill_effective,
                    'share': (
                        skill_effective / authoritative_effective
                        if authoritative_effective > 0 else 0.0
                    ),
                    'events': int(values['events']),
                    'source': 'network_exact_unverified_coverage',
                })
            unclassified = max(0, authoritative_effective - observed_effective)
            if authoritative_total and not server_skills and unclassified:
                skill_rows.append({
                    'skill_id': 0,
                    'name': '未归类治疗',
                    'total_healing': None,
                    'effective_healing': unclassified,
                    'overhealing': None,
                    'share': (
                        unclassified / authoritative_effective
                        if authoritative_effective > 0 else 0.0
                    ),
                    'events': None,
                    'source': 'server_total_minus_observed_callbacks',
                })
            healer['skills'] = skill_rows

            target_rows = []
            for target_id, values in sorted(
                raw['targets'].items(),
                key=lambda item: int(item[1]['effective']),
                reverse=True,
            ):
                target_total = int(values['total'])
                target_effective = int(values['effective'])
                target_rows.append({
                    'target_id': target_id,
                    'name': str(
                        participant_rows.get(target_id, {}).get('name')
                        or f'玩家 {target_id}'
                    ),
                    'total_healing': target_total,
                    'effective_healing': target_effective,
                    'overhealing': target_total - target_effective,
                    'share': (
                        target_effective / observed_effective
                        if observed_effective > 0 else 0.0
                    ),
                    'events': int(values['events']),
                    'coverage': 'exact_observed_partial',
                })
            healer['targets'] = target_rows
        record['healers'] = healer_rows
        return record

    def _attach_observed_details(self, encounter: EncounterRecord, record: dict) -> dict:
        started = encounter.started_at_ns / 1_000_000_000
        ended = (encounter.ended_at_ns or encounter.started_at_ns) / 1_000_000_000
        duration = record.get('duration_seconds')
        if duration is None:
            duration = max(0.0, ended - started)
        record = rebase_relative_combat_logs(
            record, started_at_epoch=started, duration_seconds=duration,
        )
        actors = {_actor_id(row.get('actor_id')) for row in record.get('participants', ())
                  if isinstance(row, dict)} - {0}
        targets = {_actor_id((record.get('monster') or {}).get('entity_id'))} - {0}
        for entity, boss in getattr(self.controller, 'boss_entities', {}).items():
            template = _actor_id(boss.get('template_id'))
            if template and (
                boss_template_continues_encounter(template, encounter.boss_template_id)
                or boss_template_continues_encounter(encounter.boss_template_id, template)
            ):
                targets.add(_actor_id(entity))
        for name, events, actor_key, columns, coverage in (
            ('skill_cast_log', self.observed_casts, 'actor_id',
             ['time_ms', 'actor_id', 'skill_id', 'sequence'], 'observed_team_casts'),
            ('event_log', self.observed_hits, 'attacker_id',
             ['time_ms', 'actor_id', 'target_id', 'skill_id', 'damage', 'critical', 'penetrating'],
             'observed_damage_events'),
        ):
            existing = record.get(name, {})
            rows = {tuple(row) for row in existing.get('rows', ())
                    if isinstance(row, (list, tuple)) and len(row) == len(columns)}
            sources = dict(existing.get('actor_sources', {}))
            for event in events:
                actor = _actor_id(event.get(actor_key))
                timestamp = (int(event.get('filetime_100ns', 0) or 0)
                             - 116_444_736_000_000_000) / 10_000_000
                if actor not in actors or not started <= timestamp <= ended:
                    continue
                relative = max(0, int(round((timestamp - started) * 1000)))
                if name == 'skill_cast_log':
                    row = (relative, actor, int(event.get('skill_id', 0)),
                           int(event.get('sequence', 0) or 0))
                    sources[str(actor)] = (
                        'cast_broadcast' if event.get('cast_source') == 'network_cast_broadcast'
                        else 'successful_cast'
                    )
                else:
                    row = (relative, actor, int(event.get('target_id', 0)),
                           int(event.get('skill_id', 0)), int(event.get('damage', 0)),
                           event.get('critical'), event.get('penetrating'))
                    if row[4] <= 0 or row[2] not in targets:
                        continue
                rows.add(row)
            if rows:
                record[name] = {
                    'version': 1, 'columns': columns, 'coverage': coverage,
                    'origin_started_at_epoch': started,
                    'rows': [list(row) for row in sorted(rows, key=lambda row: row[0])],
                }
                if name == 'skill_cast_log':
                    record[name]['actor_sources'] = sources
        health_history = getattr(self.controller, 'boss_health_history', None)
        if callable(health_history):
            samples = health_history(encounter)
            if samples:
                record['boss_hp_damage_samples'] = samples
                record.pop('team_dps_timeline', None)
        timeline = rebuild_team_dps_timeline(record)
        if timeline:
            record['team_dps_timeline'] = timeline
        return self._attach_observed_healing(
            record, started=started, ended=ended
        )

    def sample_live_team_dps(
        self, tracker: EncounterTracker, timestamp: float, value: object,
        *, boss_template_id: int = 0,
    ) -> bool:
        """Bind one HUD value to the active passive encounter before reset."""

        encounter = tracker.encounters.get(tracker.current_id)
        if encounter is None or encounter.result != "IN_PROGRESS":
            return False
        if (
            boss_template_id and encounter.boss_template_id
            and not boss_template_continues_encounter(
                boss_template_id, encounter.boss_template_id
            )
            and not boss_template_continues_encounter(
                encounter.boss_template_id, boss_template_id
            )
        ):
            return False
        try:
            second = int(float(timestamp) - encounter.started_at_ns / 1_000_000_000)
            dps = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if second < 0 or second > 7200 or not math.isfinite(dps) or dps < 0:
            return False
        samples = self.live_team_dps_samples.setdefault(
            encounter.local_encounter_id, []
        )
        if samples and second < samples[-1][0]:
            return False
        if samples and second == samples[-1][0]:
            samples[-1] = (second, dps)
        elif len(samples) < 7200:
            samples.append((second, dps))
        return True

    def _attach_live_team_dps(self, encounter: EncounterRecord, record: dict) -> dict:
        if record.get("display_team_dps_samples"):
            if not record.get("team_dps_timeline"):
                timeline = rebuild_team_dps_timeline(record)
                if timeline:
                    record["team_dps_timeline"] = timeline
            return record
        samples = self.live_team_dps_samples.get(encounter.local_encounter_id, ())
        if len(samples) < 2:
            return record
        record["display_team_dps_samples"] = {
            "version": 1,
            "columns": ["time_seconds", "team_dps"],
            "coverage": "live_display_team_dps",
            "interval_seconds": 1,
            "origin_started_at_epoch": encounter.started_at_ns / 1_000_000_000,
            "rows": [[second, value] for second, value in samples],
        }
        timeline = rebuild_team_dps_timeline(record)
        if timeline:
            record["team_dps_timeline"] = timeline
        return record

    def bind(self, local_observation_id: object, encounter_id: object) -> bool:
        """Bind the live meter span to its independently tracked pull."""
        local_id = str(local_observation_id or "").strip()
        tracked_id = str(encounter_id or "").strip()
        if not local_id or not tracked_id:
            return False
        previous = self.local_bindings.get(local_id)
        if previous == tracked_id:
            return False
        if previous:
            return False
        self.local_bindings[local_id] = tracked_id
        return True

    def project_local_record(
        self, tracker: EncounterTracker, record: dict
    ) -> dict | None:
        local_id = str(record.get("encounter_id", "") or "")
        encounter = tracker.encounters.get(self.local_bindings.get(local_id, ""))
        if encounter is None:
            encounter = match_local_history_record(tracker, record)
        if encounter is None or encounter.history_deleted:
            return None
        if local_id:
            self.local_bindings[local_id] = encounter.local_encounter_id
        duration = encounter.encounter_duration_seconds
        if duration is None:
            duration = max(0.0, ((encounter.ended_at_ns or encounter.started_at_ns)
                                 - encounter.started_at_ns) / 1_000_000_000)
        record = rebase_relative_combat_logs(
            record, started_at_epoch=encounter.started_at_ns / 1_000_000_000,
            duration_seconds=duration,
        )
        existing = self.history_store.load(encounter.local_encounter_id)
        if isinstance(existing, dict):
            record = dict(record)
            for field in ('event_log', 'skill_cast_log', 'boss_hp_damage_samples',
                          'team_damage_samples', 'display_team_dps_samples'):
                stored = existing.get(field)
                incoming = record.get(field)
                if not isinstance(stored, dict) or not stored.get('rows'):
                    continue
                if not isinstance(incoming, dict) or not incoming.get('rows'):
                    record[field] = deepcopy(stored)
                elif (field in ('event_log', 'skill_cast_log')
                      and stored.get('columns') == incoming.get('columns')):
                    combined = dict(incoming)
                    rows = {tuple(row) for log in (stored, incoming) for row in log['rows']}
                    combined['rows'] = [list(row) for row in sorted(rows, key=lambda row: row[0])]
                    if field == 'skill_cast_log':
                        combined['actor_sources'] = {
                            **stored.get('actor_sources', {}), **incoming.get('actor_sources', {}),
                        }
                    record[field] = combined
                elif (len(stored['rows']) > len(incoming['rows'])
                      or len(stored.get('columns', ())) > len(incoming.get('columns', ()))):
                    record[field] = deepcopy(stored)
        projected = self._attach_live_team_dps(
            encounter, self._attach_observed_details(
                encounter, encounter_history_record(encounter, record)
            )
        )
        projected["local_observation_id"] = local_id
        return projected

    def sync(self, tracker: EncounterTracker) -> set[str]:
        saved = set()
        for encounter in tracker.encounters.values():
            if encounter.ended_at_ns is None or encounter.history_deleted:
                continue
            existing = self.history_store.load(encounter.local_encounter_id)
            if (
                isinstance(existing, dict)
                and existing.get("source") == SETTLEMENT_HISTORY_SOURCE
                and int(existing.get("settlement_revision", -1) or -1)
                == encounter.revision
                and [
                    str(row.get("user_token") or "")
                    for row in existing.get("participants", ())
                    if isinstance(row, dict)
                ] == visible_encounter_member_tokens(encounter)
                and (
                    encounter.local_encounter_id not in self.live_team_dps_samples
                    or existing.get("display_team_dps_samples")
                )
                and encounter.local_encounter_id not in self.details_dirty
            ):
                self.live_team_dps_samples.pop(encounter.local_encounter_id, None)
                continue
            projected = self._attach_live_team_dps(
                encounter, self._attach_observed_details(
                    encounter, encounter_history_record(encounter, existing)
                )
            )
            self.history_store.save(projected)
            self.live_team_dps_samples.pop(encounter.local_encounter_id, None)
            saved.add(encounter.local_encounter_id)
            self.details_dirty.discard(encounter.local_encounter_id)
        return saved
