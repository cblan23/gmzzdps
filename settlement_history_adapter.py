"""Project passive settlement encounters into the existing history format.

The encounter journal remains authoritative for matching and settlement state.
Projected records let the established history browser render the same encounter
without maintaining a second production UI or inventing unavailable values.
"""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import math

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


def _base_participant_rows(base: dict) -> dict[int, dict]:
    rows = base.get("participants") if isinstance(base, dict) else None
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
    existing_participants = _base_participant_rows(base)
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
    total_damage = (
        sum(int(row.get("damage") or 0) for row in members.values())
        if settled and members and all(row.get("damage") is not None for row in members.values())
        else None
    )
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
        taken_values.append(bear)
        taken_rows.append(
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
                    else "unavailable"
                ),
            }
        )
        heal = _optional_nonnegative(values.get("heal")) if settled else None
        healing_values.append(heal)
        if not settled or heal is None or heal > 0:
            healers.append(
                {
                    "actor_id": actor,
                    "name": row["name"],
                    "is_self": is_self,
                    "profession_id": row["profession_id"],
                    "extraordinary_rating": row.get("extraordinary_rating"),
                    "is_ai": row["is_ai"],
                    "hps": heal / duration if heal is not None and duration else None,
                    "total_healing": None,
                    "effective_healing": heal,
                    "overhealing": None,
                    "overheal_rate": None,
                    "skills": _server_healing_skills(values, heal) if settled else [],
                    "targets": [],
                    "coverage": (
                        "server_stage_statistics"
                        if settled and heal is not None
                        else "unavailable"
                    ),
                }
            )

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
            "duration_source": encounter.duration_source,
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
            "stage_id": encounter.stage_id,
            "dungeon_id": encounter.dungeon_id,
            "map_id": encounter.map_id,
            "dungeon_stage_id": encounter.stage_id,
            "dungeon_stage_phase": encounter.stage_index,
            "boss_token": encounter.boss_token,
            "match_confidence": encounter.match_confidence,
            "participants_snapshot": deepcopy(visible_identities),
            "settlement_revision": encounter.revision,
            "capture_complete": encounter.capture_complete,
            "completion_confirmed": bool(
                encounter.result == "VICTORY" and settled
            ),
            "damage_accounting": {
                "source": "server_stage_statistics" if settled else "pending_server_statistics",
                "snapshot_semantics": "upsert_not_increment",
                "statistics_scope": "STAGE",
                "server_battle_id": encounter.server_battle_id,
                "match_confidence": encounter.match_confidence,
            },
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
    def __init__(self, history_store):
        self.history_store = history_store
        self.local_bindings: dict[str, str] = {}
        self.live_team_dps_samples: dict[str, list[tuple[int, float]]] = {}

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
        projected = self._attach_live_team_dps(
            encounter, encounter_history_record(encounter, record)
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
            ):
                self.live_team_dps_samples.pop(encounter.local_encounter_id, None)
                continue
            projected = self._attach_live_team_dps(
                encounter, encounter_history_record(encounter, existing)
            )
            self.history_store.save(projected)
            self.live_team_dps_samples.pop(encounter.local_encounter_id, None)
            saved.add(encounter.local_encounter_id)
        return saved
