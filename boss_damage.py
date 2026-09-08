#!/usr/bin/env python3
"""Strict, side-channel aggregation for Boss-to-party damage.

The tracker deliberately has no knowledge of DPS, combat clocks, encounter
switching, or TeamStats ingestion.  The combat model validates an event before
passing it here; this module only de-duplicates, bounds memory, and serializes
the accepted facts for live/history views.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping


BOSS_DAMAGE_SCHEMA_VERSION = 1
BOSS_DAMAGE_MAX_EVENTS = 50_000
_FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _text(value: object, maximum: int = 64) -> str:
    return str(value or "").strip()[:maximum]


def _filetime_epoch(value: object) -> float:
    timestamp = _integer(value)
    if timestamp <= _FILETIME_EPOCH_OFFSET:
        return 0.0
    return (timestamp - _FILETIME_EPOCH_OFFSET) / 10_000_000.0


@dataclass(frozen=True, slots=True)
class BossDamageEvent:
    """One already-validated exact hit from an encounter enemy to a player."""

    filetime_100ns: int
    event_time_epoch: float
    source_id: int
    target_id: int
    skill_id: int
    damage: int
    source_template_id: int = 0
    source_name: str = ""
    source_kind: str = "boss"
    target_name: str = ""
    skill_name: str = ""

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        event_time_epoch: object = 0.0,
    ) -> "BossDamageEvent | None":
        if not isinstance(payload, dict):
            return None
        filetime_100ns = _integer(payload.get("filetime_100ns"))
        source_id = _integer(
            payload.get("source_id", payload.get("attacker_id"))
        )
        target_id = _integer(payload.get("target_id"))
        skill_id = _integer(payload.get("skill_id"))
        damage = _integer(payload.get("damage"))
        if (
            filetime_100ns <= 0
            or source_id <= 0
            or target_id <= 0
            or source_id == target_id
            or damage <= 0
        ):
            return None
        try:
            parsed_epoch = float(event_time_epoch or 0.0)
        except (TypeError, ValueError, OverflowError):
            parsed_epoch = 0.0
        if parsed_epoch <= 0.0:
            parsed_epoch = _filetime_epoch(filetime_100ns)
        if parsed_epoch <= 0.0:
            return None
        source_kind = _text(payload.get("source_kind"), 16).casefold()
        if source_kind not in {"boss", "mechanism"}:
            source_kind = "boss"
        return cls(
            filetime_100ns=filetime_100ns,
            event_time_epoch=parsed_epoch,
            source_id=source_id,
            target_id=target_id,
            skill_id=max(0, skill_id),
            damage=damage,
            source_template_id=max(
                0, _integer(payload.get("source_template_id"))
            ),
            source_name=_text(payload.get("source_name")),
            source_kind=source_kind,
            target_name=_text(payload.get("target_name")),
            skill_name=_text(payload.get("skill_name")),
        )

    @property
    def key(self) -> tuple[int, int, int, int, int]:
        return (
            self.filetime_100ns,
            self.source_id,
            self.target_id,
            self.skill_id,
            self.damage,
        )


class BossDamageTracker:
    """Bounded storage and deterministic summaries for accepted events."""

    def __init__(self, maximum_events: int = BOSS_DAMAGE_MAX_EVENTS):
        self.maximum_events = max(1, int(maximum_events))
        self.events: list[BossDamageEvent] = []
        self._keys: set[tuple[int, int, int, int, int]] = set()
        self.truncated = False
        self.rejected_after_limit = 0
        self.revision = 0

    def clear(self) -> None:
        self.events.clear()
        self._keys.clear()
        self.truncated = False
        self.rejected_after_limit = 0
        self.revision += 1

    def ingest(
        self,
        payload: object,
        *,
        event_time_epoch: object = 0.0,
    ) -> bool:
        event = BossDamageEvent.from_payload(
            payload, event_time_epoch=event_time_epoch
        )
        if event is None or event.key in self._keys:
            return False
        if len(self.events) >= self.maximum_events:
            self.truncated = True
            self.rejected_after_limit += 1
            self.revision += 1
            return False
        self.events.append(event)
        self._keys.add(event.key)
        self.revision += 1
        return True

    def rebind_target(self, old_actor_id: object, new_actor_id: object) -> bool:
        old_actor = _integer(old_actor_id)
        new_actor = _integer(new_actor_id)
        if not old_actor or not new_actor or old_actor == new_actor:
            return False
        changed = False
        rebound: list[BossDamageEvent] = []
        keys: set[tuple[int, int, int, int, int]] = set()
        for event in self.events:
            candidate = (
                replace(event, target_id=new_actor)
                if event.target_id == old_actor
                else event
            )
            if candidate.key in keys:
                changed = True
                continue
            rebound.append(candidate)
            keys.add(candidate.key)
            changed |= candidate is not event
        if changed:
            self.events = rebound
            self._keys = keys
            self.revision += 1
        return changed

    def summary(
        self,
        *,
        team_taken: int | None,
        source_profiles: Mapping[int, Mapping[str, object]] | None = None,
        target_names: Mapping[int, str] | None = None,
        skill_names: Mapping[int, str] | None = None,
        started_at_epoch: float = 0.0,
        duration_seconds: float = 0.0,
        include_event_log: bool = True,
        unavailable_reason: str = "",
    ) -> dict[str, object]:
        source_profiles = source_profiles or {}
        target_names = target_names or {}
        skill_names = skill_names or {}
        observed_damage = sum(event.damage for event in self.events)
        hits = len(self.events)
        max_hit = max((event.damage for event in self.events), default=0)

        if unavailable_reason:
            coverage = "unavailable"
        elif not self.events:
            coverage = "unavailable"
        elif team_taken is None:
            coverage = "observed_partial"
        elif observed_damage > max(0, int(team_taken)):
            coverage = "conflict"
        elif observed_damage == max(0, int(team_taken)) and not self.truncated:
            coverage = "complete"
        else:
            coverage = "observed_partial"

        parsed_team_taken = (
            None if team_taken is None else max(0, int(team_taken))
        )
        unassigned_taken = (
            None
            if parsed_team_taken is None
            else max(0, parsed_team_taken - observed_damage)
        )
        classification_ratio = (
            observed_damage / parsed_team_taken
            if parsed_team_taken
            else 0.0 if parsed_team_taken == 0 and observed_damage == 0 else None
        )

        source_groups: dict[tuple[str, int], dict[str, object]] = {}
        top_skills: dict[int, dict[str, object]] = {}
        top_targets: dict[int, dict[str, object]] = {}
        for event in self.events:
            profile = source_profiles.get(event.source_id, {})
            try:
                profile_template_id = max(
                    0, int(profile.get("template_id", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                profile_template_id = 0
            template_id = profile_template_id or event.source_template_id
            kind = _text(profile.get("kind", event.source_kind), 16).casefold()
            if kind not in {"boss", "mechanism"}:
                kind = event.source_kind
            group_identity = template_id or event.source_id
            group_key = (kind, group_identity)
            source = source_groups.setdefault(
                group_key,
                {
                    "source_key": f"{kind}:{group_identity}",
                    "entity_id": event.source_id,
                    "entity_ids": [],
                    "template_id": template_id,
                    "name": _text(profile.get("name"))
                    or event.source_name
                    or ("首领机制" if kind == "mechanism" else "首领"),
                    "kind": kind,
                    "damage": 0,
                    "hits": 0,
                    "max_hit": 0,
                    "skills_by_id": {},
                    "targets_by_id": {},
                },
            )
            entity_ids = source["entity_ids"]
            if event.source_id not in entity_ids:
                entity_ids.append(event.source_id)
            source["damage"] += event.damage
            source["hits"] += 1
            source["max_hit"] = max(int(source["max_hit"]), event.damage)

            resolved_skill_name = (
                _text(skill_names.get(event.skill_id))
                or event.skill_name
                or (
                    f"技能 {event.skill_id}"
                    if event.skill_id
                    else "未命名技能"
                )
            )
            skill = source["skills_by_id"].setdefault(
                event.skill_id,
                {
                    "skill_id": event.skill_id,
                    "name": resolved_skill_name,
                    "damage": 0,
                    "hits": 0,
                    "max_hit": 0,
                },
            )
            skill["damage"] += event.damage
            skill["hits"] += 1
            skill["max_hit"] = max(int(skill["max_hit"]), event.damage)

            target_name = (
                _text(target_names.get(event.target_id))
                or event.target_name
                or "队伍成员"
            )
            target = source["targets_by_id"].setdefault(
                event.target_id,
                {
                    "actor_id": event.target_id,
                    "name": target_name,
                    "damage": 0,
                    "hits": 0,
                    "max_hit": 0,
                },
            )
            target["damage"] += event.damage
            target["hits"] += 1
            target["max_hit"] = max(int(target["max_hit"]), event.damage)

            all_skill = top_skills.setdefault(
                event.skill_id,
                {
                    "skill_id": event.skill_id,
                    "name": resolved_skill_name,
                    "damage": 0,
                    "hits": 0,
                    "max_hit": 0,
                    "source_ids": set(),
                },
            )
            all_skill["damage"] += event.damage
            all_skill["hits"] += 1
            all_skill["max_hit"] = max(int(all_skill["max_hit"]), event.damage)
            all_skill["source_ids"].add(event.source_id)

            all_target = top_targets.setdefault(
                event.target_id,
                {
                    "actor_id": event.target_id,
                    "name": target_name,
                    "damage": 0,
                    "hits": 0,
                    "max_hit": 0,
                },
            )
            all_target["damage"] += event.damage
            all_target["hits"] += 1
            all_target["max_hit"] = max(
                int(all_target["max_hit"]), event.damage
            )

        sources: list[dict[str, object]] = []
        for source in source_groups.values():
            source_damage = max(0, int(source["damage"]))
            skills = list(source.pop("skills_by_id").values())
            targets = list(source.pop("targets_by_id").values())
            for skill in skills:
                skill["share"] = (
                    int(skill["damage"]) / source_damage if source_damage else 0.0
                )
                skill["average_hit"] = (
                    int(skill["damage"]) / int(skill["hits"])
                    if int(skill["hits"])
                    else 0.0
                )
            for target in targets:
                target["share"] = (
                    int(target["damage"]) / source_damage if source_damage else 0.0
                )
            skills.sort(key=lambda row: (-int(row["damage"]), int(row["skill_id"])))
            targets.sort(key=lambda row: (-int(row["damage"]), int(row["actor_id"])))
            source["skills"] = skills
            source["targets"] = targets
            source["share"] = (
                source_damage / observed_damage if observed_damage else 0.0
            )
            source["average_hit"] = (
                source_damage / int(source["hits"])
                if int(source["hits"])
                else 0.0
            )
            sources.append(source)
        sources.sort(
            key=lambda row: (
                -int(row["damage"]),
                str(row["kind"]),
                int(row["template_id"] or 0),
            )
        )

        skills: list[dict[str, object]] = []
        for row in top_skills.values():
            row = dict(row)
            source_ids = row.pop("source_ids")
            row["source_count"] = len(source_ids)
            row["share"] = (
                int(row["damage"]) / observed_damage if observed_damage else 0.0
            )
            row["average_hit"] = (
                int(row["damage"]) / int(row["hits"])
                if int(row["hits"])
                else 0.0
            )
            skills.append(row)
        skills.sort(key=lambda row: (-int(row["damage"]), int(row["skill_id"])))

        targets = list(top_targets.values())
        for target in targets:
            target["share"] = (
                int(target["damage"]) / observed_damage if observed_damage else 0.0
            )
        targets.sort(key=lambda row: (-int(row["damage"]), int(row["actor_id"])))

        result: dict[str, object] = {
            "version": BOSS_DAMAGE_SCHEMA_VERSION,
            "coverage": coverage,
            "unavailable_reason": _text(unavailable_reason),
            "observed_damage": observed_damage,
            "team_taken": parsed_team_taken,
            "unassigned_taken": unassigned_taken,
            "classification_ratio": classification_ratio,
            "hits": hits,
            "max_hit": max_hit,
            "truncated": self.truncated,
            "rejected_after_limit": self.rejected_after_limit,
            "sources": sources,
            "skills": skills,
            "targets": targets,
        }
        if include_event_log and self.events:
            try:
                start = float(started_at_epoch or 0.0)
                duration = max(0.0, float(duration_seconds or 0.0))
            except (TypeError, ValueError, OverflowError):
                start = duration = 0.0
            maximum_ms = max(0, int(round(duration * 1000.0)))
            rows = []
            for event in sorted(
                self.events,
                key=lambda item: (item.filetime_100ns, item.source_id),
            ):
                relative_ms = (
                    max(0, int(round((event.event_time_epoch - start) * 1000.0)))
                    if start > 0.0
                    else 0
                )
                if maximum_ms:
                    relative_ms = min(maximum_ms, relative_ms)
                rows.append(
                    [
                        relative_ms,
                        event.source_id,
                        event.target_id,
                        event.skill_id,
                        event.damage,
                    ]
                )
            result["event_log"] = {
                "version": 1,
                "columns": [
                    "time_ms",
                    "source_id",
                    "target_id",
                    "skill_id",
                    "damage",
                ],
                "coverage": "observed_boss_damage_events",
                "origin_started_at_epoch": start,
                "rows": rows,
            }
        return result
