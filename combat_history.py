#!/usr/bin/env python3
"""Durable, one-file-per-encounter combat history storage."""

from __future__ import annotations

import json
import os
import re
import uuid
from math import ceil
from pathlib import Path


HISTORY_SCHEMA_VERSION = 1
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000


class CombatHistoryStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    @staticmethod
    def _safe_encounter_id(value: object) -> str:
        cleaned = _SAFE_ID_RE.sub("-", str(value or "").strip()).strip("-_")
        return cleaned[:96] or uuid.uuid4().hex

    @staticmethod
    def _valid_record(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        try:
            schema_version = int(value.get("schema_version", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if schema_version != HISTORY_SCHEMA_VERSION:
            return False
        if not str(value.get("encounter_id", "")).strip():
            return False
        if not isinstance(value.get("participants"), list):
            return False
        try:
            return int(value.get("total_damage", 0)) > 0
        except (TypeError, ValueError, OverflowError):
            return False

    def save(self, record: dict) -> Path:
        payload = dict(record)
        payload["schema_version"] = HISTORY_SCHEMA_VERSION
        encounter_id = self._safe_encounter_id(payload.get("encounter_id"))
        payload["encounter_id"] = encounter_id
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{encounter_id}.json"
        if "favorite" not in payload and target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                existing = None
            if isinstance(existing, dict):
                payload["favorite"] = bool(existing.get("favorite", False))
        payload["favorite"] = bool(payload.get("favorite", False))
        if not self._valid_record(payload):
            raise ValueError("combat history record is incomplete")

        temporary = self.directory / f".{encounter_id}.{os.getpid()}.tmp"
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return target

    def delete(self, encounter_id: object) -> bool:
        path = self.directory / f"{self._safe_encounter_id(encounter_id)}.json"
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def unfavorited_count(self) -> int:
        if not self.directory.is_dir():
            return 0
        count = 0
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                record = None
            if not isinstance(record, dict) or not bool(record.get("favorite", False)):
                count += 1
        return count

    def clear_unfavorited(self) -> int:
        """Delete uncollected encounters while preserving favorites."""
        if not self.directory.is_dir():
            return 0
        deleted = 0
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                record = None
            if isinstance(record, dict) and bool(record.get("favorite", False)):
                continue
            try:
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                continue
        return deleted

    def set_favorite(self, encounter_id: object, favorite: bool) -> dict | None:
        path = self.directory / f"{self._safe_encounter_id(encounter_id)}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not self._valid_record(record):
            return None
        record["favorite"] = bool(favorite)
        self.save(record)
        return record

    @staticmethod
    def _actor_damage(value: object) -> dict[int, int]:
        if not isinstance(value, list):
            return {}
        result: dict[int, int] = {}
        for row in value:
            if not isinstance(row, dict):
                continue
            try:
                actor_id = int(row.get("actor_id", 0) or 0)
                damage = max(0, int(row.get("damage", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if actor_id > 0:
                result[actor_id] = damage
        return result

    @staticmethod
    def _apply_exact_stage_skills(
        record: dict,
        summary: dict,
        summary_id: str,
    ) -> tuple[dict, list[int]]:
        """Apply only server skill rows whose actor total exactly matches history."""
        summary_actors: dict[int, dict] = {}
        for raw_actor in summary.get("actors", []):
            if not isinstance(raw_actor, dict):
                continue
            try:
                actor_id = int(raw_actor.get("actor_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if actor_id > 0:
                summary_actors[actor_id] = raw_actor

        participants: list[dict] = []
        applied_actor_ids: list[int] = []
        for raw_participant in record.get("participants", []):
            if not isinstance(raw_participant, dict):
                continue
            participant = dict(raw_participant)
            participants.append(participant)
            if bool(participant.get("is_self")):
                # Local callbacks contain max-hit information that the stage
                # table does not, so retain the richer exact local breakdown.
                continue
            try:
                actor_id = int(participant.get("actor_id", 0) or 0)
                participant_damage = max(
                    0, int(participant.get("damage", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                continue
            summary_actor = summary_actors.get(actor_id)
            if summary_actor is None or participant_damage <= 0:
                continue
            try:
                summary_damage = max(
                    0, int(summary_actor.get("damage", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                continue
            if summary_damage != participant_damage:
                continue

            existing_names: dict[int, str] = {}
            for raw_skill in participant.get("skills", []):
                if not isinstance(raw_skill, dict):
                    continue
                try:
                    skill_id = int(raw_skill.get("skill_id", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                name = str(raw_skill.get("name", "")).strip()
                if skill_id > 0 and name:
                    existing_names[skill_id] = name

            parsed_skills: dict[int, tuple[int, int]] = {}
            for raw_skill in summary_actor.get("skills", []):
                if not isinstance(raw_skill, dict):
                    continue
                try:
                    skill_id = int(raw_skill.get("skill_id", 0) or 0)
                    damage = max(0, int(raw_skill.get("damage", 0) or 0))
                    hits = max(0, int(raw_skill.get("hits", 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if skill_id <= 0 or damage <= 0:
                    continue
                previous_damage, previous_hits = parsed_skills.get(
                    skill_id, (0, 0)
                )
                parsed_skills[skill_id] = (
                    previous_damage + damage,
                    previous_hits + hits,
                )
            classified_damage = sum(
                damage for damage, _hits in parsed_skills.values()
            )
            if not parsed_skills or classified_damage > participant_damage:
                continue

            skills = [
                {
                    "skill_id": skill_id,
                    "name": existing_names.get(skill_id, f"技能 {skill_id}"),
                    "damage": damage,
                    "share": damage / participant_damage,
                    "hits": hits if hits > 0 else None,
                    "max_hit": None,
                    "source": "server_stage_summary",
                }
                for skill_id, (damage, hits) in sorted(
                    parsed_skills.items(),
                    key=lambda item: item[1][0],
                    reverse=True,
                )
            ]
            unclassified_damage = participant_damage - classified_damage
            if unclassified_damage > 0:
                skills.append(
                    {
                        "skill_id": 0,
                        "name": "未归类伤害",
                        "damage": unclassified_damage,
                        "share": unclassified_damage / participant_damage,
                        "hits": None,
                        "max_hit": None,
                        "aggregate": True,
                        "source": "exact_total_minus_exact_skills",
                    }
                )
            participant["skills"] = skills
            participant["classified_skill_damage"] = classified_damage
            participant["unclassified_damage"] = unclassified_damage
            participant["accounted_skill_damage"] = participant_damage
            participant["skill_damage_difference"] = 0
            participant["skill_source"] = "server_stage_summary"
            participant["skill_summary_id"] = summary_id
            applied_actor_ids.append(actor_id)

        updated = dict(record)
        updated["participants"] = participants
        return updated, sorted(set(applied_actor_ids))

    @staticmethod
    def _skill_reconciliation(participants: object) -> list[dict]:
        rows: list[dict] = []
        if not isinstance(participants, list):
            return rows
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            try:
                actor_id = int(participant.get("actor_id", 0) or 0)
                damage = max(0, int(participant.get("damage", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            classified = 0
            unclassified = 0
            for skill in participant.get("skills", []):
                if not isinstance(skill, dict):
                    continue
                try:
                    skill_id = int(skill.get("skill_id", 0) or 0)
                    skill_damage = max(0, int(skill.get("damage", 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if skill_id == 0:
                    unclassified += skill_damage
                else:
                    classified += skill_damage
            accounted = classified + unclassified
            rows.append(
                {
                    "actor_id": actor_id,
                    "name": str(participant.get("name", "")),
                    "damage": damage,
                    "classified_skill_damage": classified,
                    "unclassified_damage": unclassified,
                    "accounted_skill_damage": accounted,
                    "difference": damage - accounted,
                }
            )
        return rows

    @staticmethod
    def _normalized_actor_ids(value: object) -> list[int]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return []
        actor_ids: list[int] = []
        for raw_value in value:
            if isinstance(raw_value, bool):
                continue
            try:
                actor_id = int(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if actor_id > 0:
                actor_ids.append(actor_id)
        return sorted(set(actor_ids))

    @classmethod
    def _record_stage_skill_application(
        cls,
        accounting: dict,
        participants: object,
        summary_id: str,
        actor_ids: list[int],
    ) -> tuple[dict, bool]:
        actor_ids = cls._normalized_actor_ids(actor_ids)
        if not actor_ids:
            return accounting, False
        applications = list(
            accounting.get("stage_skill_summary_applications", [])
        )
        if any(
            isinstance(item, dict)
            and str(item.get("summary_id", "")) == summary_id
            and cls._normalized_actor_ids(item.get("actor_ids")) == actor_ids
            for item in applications
        ):
            return accounting, False
        applications.append(
            {
                "summary_id": summary_id,
                "actor_ids": actor_ids,
                "policy": "exact_actor_total_match_only",
            }
        )
        accounting["stage_skill_summary_applications"] = applications
        accounting["stage_skill_policy"] = (
            "server_skill_amounts_are_used_only_when_the_stage_actor_total_"
            "exactly_matches_the_common_total"
        )
        accounting["skill_reconciliation"] = cls._skill_reconciliation(
            participants
        )
        return accounting, True

    def attach_stage_summary_validation(
        self,
        summary: dict,
        *,
        max_delay_seconds: float = 300.0,
    ) -> dict | None:
        """Attach a late completion table without changing archived damage."""
        if not isinstance(summary, dict):
            return None
        summary_id = str(summary.get("summary_id", "")).strip()
        if (
            not summary_id
            or not bool(summary.get("authoritative"))
            or not bool(summary.get("completion_confirmed"))
        ):
            return None
        try:
            timestamp = int(summary.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        expected = self._actor_damage(summary.get("actors"))
        if timestamp <= _FILETIME_EPOCH_OFFSET or not expected:
            return None
        summary_time = (timestamp - _FILETIME_EPOCH_OFFSET) / 10_000_000
        candidates: list[tuple[tuple[float, ...], dict]] = []
        for record in self.load_recent(50):
            accounting = record.get("damage_accounting", {})
            validations = (
                accounting.get("stage_summary_validations", [])
                if isinstance(accounting, dict)
                else []
            )
            duplicate = any(
                isinstance(item, dict)
                and str(item.get("summary_id", "")) == summary_id
                for item in validations
            )
            if duplicate:
                upgraded, actor_ids = self._apply_exact_stage_skills(
                    record, summary, summary_id
                )
                next_accounting = dict(
                    upgraded.get("damage_accounting", {})
                    if isinstance(upgraded.get("damage_accounting"), dict)
                    else {}
                )
                next_accounting, changed = self._record_stage_skill_application(
                    next_accounting,
                    upgraded.get("participants"),
                    summary_id,
                    actor_ids,
                )
                if changed:
                    upgraded["damage_accounting"] = next_accounting
                    self.save(upgraded)
                    return upgraded
                return record
            observed = self._actor_damage(record.get("participants"))
            overlap = set(expected).intersection(observed)
            if not observed or len(overlap) < ceil(min(len(expected), len(observed)) / 2):
                continue
            try:
                ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                continue
            delay = summary_time - ended_at
            if delay < -5.0 or delay > max_delay_seconds:
                continue
            expected_total = sum(expected.values())
            observed_total = sum(observed.values())
            if (
                expected_total <= 0
                or observed_total <= 0
                or min(expected_total, observed_total)
                < max(expected_total, observed_total) * 0.5
            ):
                continue
            per_actor_error = sum(
                abs(expected[actor_id] - observed[actor_id])
                for actor_id in overlap
            )
            missing = len(set(expected).symmetric_difference(observed))
            score = (
                float(missing),
                -float(len(overlap)),
                float(per_actor_error),
                float(abs(expected_total - observed_total)),
                abs(delay),
            )
            candidates.append((score, record))
        if not candidates:
            return None

        record = min(candidates, key=lambda item: item[0])[1]
        observed = self._actor_damage(record.get("participants"))
        expected_total = sum(expected.values())
        observed_total = sum(observed.values())
        participant_names = {
            int(row.get("actor_id", 0) or 0): str(row.get("name", ""))
            for row in record.get("participants", [])
            if isinstance(row, dict)
        }
        summary_names = {
            int(row.get("actor_id", 0) or 0): str(row.get("name", ""))
            for row in summary.get("actors", [])
            if isinstance(row, dict)
        }
        actor_ids = sorted(set(expected) | set(observed))
        actor_differences = [
            {
                "actor_id": actor_id,
                "name": participant_names.get(actor_id)
                or summary_names.get(actor_id, ""),
                "realtime_damage": observed.get(actor_id, 0),
                "summary_damage": expected.get(actor_id, 0),
                "difference": expected.get(actor_id, 0)
                - observed.get(actor_id, 0),
            }
            for actor_id in actor_ids
        ]
        difference = expected_total - observed_total
        total_tolerance = max(
            25_000,
            round(max(expected_total, observed_total) * 0.35),
        )
        summary_covers_observed = set(observed).issubset(expected)
        per_actor_exact_match = bool(
            actor_differences
            and summary_covers_observed
            and all(not row["difference"] for row in actor_differences)
        )
        delay = summary_time - float(record.get("ended_at_epoch", 0.0) or 0.0)
        validation = {
            "summary_total": expected_total,
            "observed_total": observed_total,
            "difference": difference,
            "absolute_difference": abs(difference),
            "total_tolerance": total_tolerance,
            "total_plausible": abs(difference) <= total_tolerance,
            "self_plausible": True,
            "actor_overlap_count": len(set(expected).intersection(observed)),
            "summary_delay_seconds": round(delay, 6),
            "end_snapshot": True,
            "would_have_matched": per_actor_exact_match,
            "summary_covers_observed": summary_covers_observed,
            "authoritative_total_plausible": True,
            "encounter_concluded": True,
            "confirmed_multiphase_completion": bool(
                summary.get("completion_confirmed")
            ),
            "would_allow_legacy_correction": True,
            "damage_correction_applied": False,
            "per_actor_exact_match": per_actor_exact_match,
            "actor_differences": actor_differences,
            "attached_to_archived_record": True,
        }
        attached = {
            "summary_id": summary_id,
            "filetime_100ns": timestamp,
            "authoritative": True,
            "completion_confirmed": True,
            "validation_only": True,
            "validation": validation,
            "member_count": int(summary.get("member_count", 0) or 0),
            "actors": [
                dict(row)
                for row in summary.get("actors", [])
                if isinstance(row, dict)
            ],
        }
        updated, applied_skill_actor_ids = self._apply_exact_stage_skills(
            record, summary, summary_id
        )
        next_accounting = dict(
            record.get("damage_accounting", {})
            if isinstance(record.get("damage_accounting"), dict)
            else {}
        )
        validations = list(next_accounting.get("stage_summary_validations", []))
        validations.append(attached)
        next_accounting["stage_summary_validations"] = validations
        seen_ids = set(next_accounting.get("seen_stage_summary_ids", []))
        seen_ids.add(summary_id)
        next_accounting["seen_stage_summary_ids"] = sorted(seen_ids)
        next_accounting, _skills_changed = self._record_stage_skill_application(
            next_accounting,
            updated.get("participants"),
            summary_id,
            applied_skill_actor_ids,
        )
        updated["damage_accounting"] = next_accounting
        self.save(updated)
        return updated

    def load_recent(self, limit: int = 500) -> list[dict]:
        if limit <= 0 or not self.directory.is_dir():
            return []
        paths = sorted(
            self.directory.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        records: list[dict] = []
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if self._valid_record(value):
                records.append(value)
        records.sort(
            key=lambda item: float(item.get("ended_at_epoch", 0.0) or 0.0),
            reverse=True,
        )
        return records[:limit]
