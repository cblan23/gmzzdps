#!/usr/bin/env python3
"""Durable, one-file-per-encounter combat history storage."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from math import ceil
from pathlib import Path


HISTORY_SCHEMA_VERSION = 1
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000
_HEALER_PROFESSION_IDS = frozenset({1_200_002})


def authoritative_team_combat_seconds(
    raw_actors: object,
) -> tuple[float, str, list[int]]:
    """Return the deterministic team clock carried by a final settlement.

    Field 19 (``combat_seconds_total``) is the game's cumulative combat clock
    for each party member.  The team clock is the upper edge of those values,
    which every client receives in the same settlement table.  Captures show
    one boundary case where a single member is one second above at least two
    members on the otherwise agreed upper edge; only that exact +1 boundary is
    collapsed.  ``combat_seconds_delta`` is used only for legacy payloads that
    contain no cumulative values at all.
    """
    if not isinstance(raw_actors, (list, tuple)):
        return 0.0, "unavailable", []

    totals: list[int] = []
    deltas: list[int] = []
    for raw_actor in raw_actors:
        if not isinstance(raw_actor, dict):
            continue
        try:
            total = max(0, int(raw_actor.get("combat_seconds_total", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            total = 0
        try:
            delta = max(0, int(raw_actor.get("combat_seconds_delta", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            delta = 0
        if total > 0:
            totals.append(total)
        if delta > 0:
            deltas.append(delta)

    values = totals or deltas
    if not values:
        return 0.0, "unavailable", []
    source = "total" if totals else "legacy_delta"
    counts = Counter(values)
    maximum = max(values)
    if counts[maximum] == 1 and counts.get(maximum - 1, 0) >= 2:
        return float(maximum - 1), f"upper_team_consensus_{source}", values
    return float(maximum), f"maximum_member_{source}", values


def _parsed_profession_id(value: object) -> int:
    try:
        profession_id = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return profession_id if 1_200_001 <= profession_id <= 1_200_007 else 0


def _known_non_healer(value: object) -> bool:
    profession_id = _parsed_profession_id(value)
    return bool(profession_id and profession_id not in _HEALER_PROFESSION_IDS)


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
            total_damage = int(value.get("total_damage", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if total_damage > 0:
            return True
        # A treatment-dummy encounter is intentionally damage-free.  Accept it
        # only when the record carries positive, explicitly attributed healing;
        # arbitrary empty records remain invalid.
        if str(value.get("target_filter", "")).casefold() != "healing_dummy":
            return False
        # ``healers`` is optional in older damage-only records, but a
        # damage-free treatment-dummy record must retain its serialized
        # collection shape.  Otherwise a truncated/null collection could
        # masquerade as a valid pure-healing encounter.
        healers = value.get("healers", [])
        if "healers" in value and not isinstance(healers, list):
            return False
        try:
            effective = int(value.get("team_effective_healing", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            effective = 0
        if effective > 0:
            return True
        for healer in healers:
            if not isinstance(healer, dict):
                continue
            try:
                if int(healer.get("effective_healing", 0) or 0) > 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
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

    @staticmethod
    def normalize_healing_for_display(record: object) -> object:
        """Hide confirmed non-healers and obsolete averages in display copies."""
        if not isinstance(record, dict):
            return record
        raw_healers = record.get("healers")
        if not isinstance(raw_healers, list):
            return record

        participant_professions: dict[int, int] = {}
        raw_participants = record.get("participants", [])
        if isinstance(raw_participants, list):
            for participant in raw_participants:
                if not isinstance(participant, dict):
                    continue
                try:
                    actor_id = int(participant.get("actor_id", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                profession_id = _parsed_profession_id(
                    participant.get("profession_id", 0)
                )
                if actor_id > 0 and profession_id:
                    participant_professions[actor_id] = profession_id

        changed = False
        healers: list[dict] = []
        for raw_healer in raw_healers:
            if not isinstance(raw_healer, dict):
                changed = True
                continue
            try:
                actor_id = int(raw_healer.get("actor_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                actor_id = 0
            profession_id = _parsed_profession_id(
                raw_healer.get("profession_id", 0)
            ) or participant_professions.get(actor_id, 0)
            if _known_non_healer(profession_id):
                changed = True
                continue
            healer = dict(raw_healer)
            response = healer.get("response")
            if isinstance(response, dict) and "average_ms" in response:
                response = dict(response)
                response.pop("average_ms", None)
                healer["response"] = response
                changed = True
            healers.append(healer)

        if not changed:
            return record
        updated = dict(record)
        updated["healers"] = healers
        try:
            duration = float(
                updated.get(
                    "hps_duration_seconds",
                    updated.get("duration_seconds", 0.0),
                )
                or 0.0
            )
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
        divisor = float(max(1, int(duration))) if duration > 0 else 1.0
        team_effective = 0
        for row in healers:
            try:
                team_effective += max(
                    0, int(row.get("effective_healing", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                continue
        parsed_totals: list[int] = []
        complete_totals = True
        for row in healers:
            value = row.get("total_healing")
            if value is None:
                complete_totals = False
                break
            try:
                parsed_totals.append(max(0, int(value or 0)))
            except (TypeError, ValueError, OverflowError):
                complete_totals = False
                break
        team_total = sum(parsed_totals) if complete_totals else None
        updated["team_hps"] = team_effective / divisor
        updated["team_effective_healing"] = team_effective
        updated["team_total_healing"] = team_total
        updated["team_overhealing"] = (
            team_total - team_effective if team_total is not None else None
        )
        return updated

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

    @classmethod
    def _apply_game_server_team_clock(
        cls,
        record: dict,
        summary: dict,
        summary_id: str,
    ) -> tuple[dict, bool]:
        """Apply only a final settlement clock matched by damage identity.

        The settlement clock changes DPS/HPS divisors only.  Damage and
        healing totals remain byte-for-byte sourced from the existing record.
        No local-time tolerance is used here; the five-second rule belongs to
        the fallback shared-clock path in the live client.
        """
        if (
            not isinstance(record, dict)
            or not isinstance(summary, dict)
            or not bool(summary.get("authoritative"))
            or not bool(summary.get("completion_confirmed"))
        ):
            return record, False
        duration, policy, member_seconds = authoritative_team_combat_seconds(
            summary.get("actors")
        )
        if duration <= 0:
            return record, False

        expected = cls._actor_damage(summary.get("actors"))
        observed = cls._actor_damage(record.get("participants"))
        overlap = set(expected).intersection(observed)
        expected_total = sum(expected.values())
        observed_total = sum(observed.values())
        damage_tolerance = max(
            25_000,
            round(max(expected_total, observed_total) * 0.01),
        )
        if (
            not expected
            or not observed
            or not overlap
            or not set(observed).issubset(expected)
            or abs(expected_total - observed_total) > damage_tolerance
        ):
            return record, False

        divisor = float(max(1, int(duration)))
        participants: list[dict] = []
        for raw_participant in record.get("participants", []):
            if not isinstance(raw_participant, dict):
                continue
            participant = dict(raw_participant)
            try:
                damage = max(0, int(participant.get("damage", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                damage = 0
            participant["dps"] = damage / divisor
            participants.append(participant)

        healers: list[dict] = []
        raw_healers = record.get("healers", [])
        if isinstance(raw_healers, list):
            for raw_healer in raw_healers:
                if not isinstance(raw_healer, dict):
                    continue
                healer = dict(raw_healer)
                try:
                    effective = max(
                        0, int(healer.get("effective_healing", 0) or 0)
                    )
                except (TypeError, ValueError, OverflowError):
                    effective = 0
                healer["hps"] = effective / divisor
                healers.append(healer)

        try:
            total_damage = max(0, int(record.get("total_damage", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            total_damage = 0
        try:
            team_effective = max(
                0, int(record.get("team_effective_healing", 0) or 0)
            )
        except (TypeError, ValueError, OverflowError):
            team_effective = sum(
                max(0, int(row.get("effective_healing", 0) or 0))
                for row in healers
            )
        try:
            started_at = float(record.get("started_at_epoch", 0.0) or 0.0)
            ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            started_at = ended_at = 0.0
        local_duration = (
            max(1.0, ended_at - started_at)
            if started_at > 0 and ended_at >= started_at
            else 0.0
        )

        updated = dict(record)
        updated.pop("_shared_clock_request", None)
        updated["participants"] = participants
        if isinstance(raw_healers, list):
            updated["healers"] = healers
        updated["duration_seconds"] = duration
        updated["dps_duration_seconds"] = divisor
        updated["hps_duration_seconds"] = divisor
        updated["team_dps"] = total_damage / divisor
        updated["team_hps"] = team_effective / divisor
        updated["duration_source"] = "game_server_team_clock"
        updated["game_server_team_clock"] = {
            "summary_id": summary_id,
            "duration_seconds": duration,
            "policy": policy,
            "member_seconds": sorted(member_seconds, reverse=True),
            "local_event_duration_seconds": local_duration,
            "difference_seconds": (
                abs(duration - local_duration) if local_duration else None
            ),
            "local_comparison_used_for_acceptance": False,
            "accepted": True,
        }
        shared_clock = updated.get("shared_clock")
        if isinstance(shared_clock, dict):
            shared_clock = dict(shared_clock)
            shared_clock["accepted"] = False
            shared_clock["superseded_by"] = "game_server_team_clock"
            updated["shared_clock"] = shared_clock
        return updated, updated != record

    @classmethod
    def restore_game_server_team_clock_for_display(cls, record: object) -> object:
        """Repair display copies of old records that embed a final settlement."""
        if not isinstance(record, dict):
            return record
        accounting = record.get("damage_accounting")
        if not isinstance(accounting, dict):
            return record
        summaries = accounting.get("stage_summary_validations")
        if not isinstance(summaries, list):
            return record
        restored = record
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            summary_id = str(summary.get("summary_id", "")).strip()
            candidate, changed = cls._apply_game_server_team_clock(
                restored, summary, summary_id
            )
            if changed:
                restored = candidate
        return restored

    @staticmethod
    def _apply_exact_stage_skills(
        record: dict,
        summary: dict,
        summary_id: str,
        *,
        missing_only: bool = False,
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
            has_classified_skill_damage = False
            for raw_skill in participant.get("skills", []):
                if not isinstance(raw_skill, dict):
                    continue
                try:
                    skill_id = int(raw_skill.get("skill_id", 0) or 0)
                    skill_damage = max(
                        0, int(raw_skill.get("damage", 0) or 0)
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                name = str(raw_skill.get("name", "")).strip()
                if skill_id > 0 and name:
                    existing_names[skill_id] = name
                if skill_id > 0 and skill_damage > 0:
                    has_classified_skill_damage = True
            if missing_only and has_classified_skill_damage:
                continue

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

    @classmethod
    def restore_exact_stage_skills_for_display(cls, record: object) -> object:
        """Restore embedded settlement skills without changing archived totals.

        Older history files can contain the authoritative completion table but
        predate the code that copied its exact teammate skill rows into the
        participant details.  This method is intentionally display-only: it
        returns a copied record, requires an authoritative completed summary,
        and delegates to the same strict actor-total reconciliation used when
        a live settlement arrives.
        """
        if not isinstance(record, dict):
            return record
        accounting = record.get("damage_accounting")
        if not isinstance(accounting, dict):
            return record
        summaries = accounting.get("stage_summary_validations")
        if not isinstance(summaries, list):
            return record

        restored = record
        changed = False
        for summary in summaries:
            if (
                not isinstance(summary, dict)
                or not bool(summary.get("authoritative"))
                or not bool(summary.get("completion_confirmed"))
                or not isinstance(summary.get("actors"), list)
            ):
                continue
            summary_id = str(summary.get("summary_id", "")).strip()
            if not summary_id:
                continue
            candidate, actor_ids = cls._apply_exact_stage_skills(
                restored,
                summary,
                summary_id,
                missing_only=True,
            )
            if actor_ids:
                restored = candidate
                changed = True
        return restored if changed else record

    @staticmethod
    def _apply_exact_stage_healing(
        record: dict,
        summary: dict,
        summary_id: str,
    ) -> tuple[dict, list[int]]:
        """Apply settlement healing without inventing missing callback detail."""
        participant_damage = CombatHistoryStore._actor_damage(
            record.get("participants")
        )
        participant_professions: dict[int, int] = {}
        raw_participants = record.get("participants", [])
        if isinstance(raw_participants, list):
            for participant in raw_participants:
                if not isinstance(participant, dict):
                    continue
                try:
                    participant_id = int(
                        participant.get("actor_id", 0) or 0
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                participant_profession = _parsed_profession_id(
                    participant.get("profession_id", 0)
                )
                if participant_id > 0 and participant_profession:
                    participant_professions[participant_id] = (
                        participant_profession
                    )
        raw_healers = record.get("healers", [])
        if not isinstance(raw_healers, list):
            raw_healers = []
        existing_healers: dict[int, dict] = {}
        for raw_healer in raw_healers:
            if not isinstance(raw_healer, dict):
                continue
            try:
                actor_id = int(raw_healer.get("actor_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if actor_id <= 0:
                continue
            profession_id = _parsed_profession_id(
                raw_healer.get("profession_id", 0)
            ) or participant_professions.get(actor_id, 0)
            if _known_non_healer(profession_id):
                continue
            healer = dict(raw_healer)
            response = healer.get("response")
            if isinstance(response, dict) and "average_ms" in response:
                response = dict(response)
                response.pop("average_ms", None)
                healer["response"] = response
            existing_healers[actor_id] = healer
        try:
            duration = float(
                record.get(
                    "hps_duration_seconds",
                    record.get("duration_seconds", 0.0),
                )
                or 0.0
            )
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
        divisor = float(max(1, int(duration))) if duration > 0 else 1.0
        applied_actor_ids: list[int] = []

        for raw_actor in summary.get("actors", []):
            if not isinstance(raw_actor, dict):
                continue
            try:
                actor_id = int(raw_actor.get("actor_id", 0) or 0)
                actor_damage = max(0, int(raw_actor.get("damage", 0) or 0))
                effective = max(
                    0, int(raw_actor.get("effective_healing", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                continue
            if actor_id <= 0:
                continue
            profession_id = _parsed_profession_id(
                raw_actor.get("profession_id", 0)
            ) or _parsed_profession_id(
                existing_healers.get(actor_id, {}).get("profession_id", 0)
            ) or participant_professions.get(actor_id, 0)
            if _known_non_healer(profession_id):
                existing_healers.pop(actor_id, None)
                continue
            if actor_id in participant_damage:
                if participant_damage[actor_id] != actor_damage:
                    continue
            elif actor_damage != 0:
                continue

            parsed_skills: dict[int, int] = {}
            raw_healing_skills = raw_actor.get("healing_skills", [])
            if not isinstance(raw_healing_skills, list):
                raw_healing_skills = []
            for raw_skill in raw_healing_skills:
                if not isinstance(raw_skill, dict):
                    continue
                try:
                    skill_id = int(raw_skill.get("skill_id", 0) or 0)
                    healing = max(
                        0, int(raw_skill.get("effective_healing", 0) or 0)
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                if skill_id > 0 and healing > 0:
                    parsed_skills[skill_id] = (
                        parsed_skills.get(skill_id, 0) + healing
                    )
            classified = sum(parsed_skills.values())
            if classified > effective:
                continue
            existing = existing_healers.get(actor_id, {})
            if effective <= 0 and not existing:
                continue
            if str(existing.get("healing_summary_id", "")) == summary_id:
                continue

            try:
                observed_total = max(
                    0, int(existing.get("observed_total_healing", 0) or 0)
                )
                observed_effective = max(
                    0,
                    int(
                        existing.get(
                            "observed_effective_healing",
                            existing.get("effective_healing", 0),
                        )
                        or 0
                    ),
                )
            except (TypeError, ValueError, OverflowError):
                observed_total = observed_effective = 0
            observed_skill_effective: dict[int, int] = {}
            skill_names: dict[int, str] = {}
            observed_skill_rows: dict[int, dict] = {}
            existing_skills = existing.get("skills", [])
            if not isinstance(existing_skills, list):
                existing_skills = []
            for raw_skill in existing_skills:
                if not isinstance(raw_skill, dict):
                    continue
                try:
                    skill_id = int(raw_skill.get("skill_id", 0) or 0)
                    healing = max(
                        0, int(raw_skill.get("effective_healing", 0) or 0)
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                if skill_id > 0:
                    if healing > 0:
                        observed_skill_effective[skill_id] = (
                            observed_skill_effective.get(skill_id, 0) + healing
                        )
                    name = str(raw_skill.get("name", "")).strip()
                    if name:
                        skill_names[skill_id] = name
                    observed_skill_rows[skill_id] = dict(raw_skill)
            verified = bool(
                observed_effective == effective
                and observed_skill_effective == parsed_skills
            )
            total_healing = observed_total if verified else None
            overhealing = (
                max(0, observed_total - effective) if verified else None
            )
            observed_overhealing = max(
                0, observed_total - observed_effective
            )
            observed_overheal_rate = (
                observed_overhealing / observed_total
                if observed_total > 0
                else None
            )
            shown_overheal_rate = (
                overhealing / total_healing
                if total_healing and overhealing is not None
                else observed_overheal_rate
            )
            overheal_rate_partial = bool(
                not verified and observed_overheal_rate is not None
            )
            skills = []
            for skill_id, skill_healing in sorted(
                parsed_skills.items(), key=lambda item: item[1], reverse=True
            ):
                observed_skill = observed_skill_rows.get(skill_id, {})
                raw_skill_total = observed_skill.get("total_healing")
                try:
                    skill_total = (
                        max(0, int(raw_skill_total))
                        if verified and raw_skill_total is not None
                        else None
                    )
                except (TypeError, ValueError, OverflowError):
                    skill_total = None
                skills.append(
                    {
                        "skill_id": skill_id,
                        "name": skill_names.get(skill_id, f"技能 {skill_id}"),
                        "total_healing": skill_total,
                        "effective_healing": skill_healing,
                        "overhealing": (
                            skill_total - skill_healing
                            if skill_total is not None
                            else None
                        ),
                        "share": skill_healing / effective if effective else 0.0,
                        "events": (
                            observed_skill.get("events") if verified else None
                        ),
                        "source": "server_stage_summary",
                    }
                )
            if verified:
                for skill_id, observed_skill in observed_skill_rows.items():
                    if skill_id in parsed_skills:
                        continue
                    try:
                        skill_total = max(
                            0,
                            int(observed_skill.get("total_healing", 0) or 0),
                        )
                        skill_effective = max(
                            0,
                            int(observed_skill.get("effective_healing", 0) or 0),
                        )
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if skill_total <= 0 or skill_effective != 0:
                        continue
                    skills.append(
                        {
                            "skill_id": skill_id,
                            "name": skill_names.get(skill_id, f"技能 {skill_id}"),
                            "total_healing": skill_total,
                            "effective_healing": 0,
                            "overhealing": skill_total,
                            "share": 0.0,
                            "events": observed_skill.get("events"),
                            "source": "network_exact_zero_effective",
                        }
                    )
            unclassified = effective - classified
            if unclassified > 0:
                skills.append(
                    {
                        "skill_id": 0,
                        "name": "未归类治疗",
                        "total_healing": None,
                        "effective_healing": unclassified,
                        "overhealing": None,
                        "share": unclassified / effective,
                        "events": None,
                        "source": "server_total_minus_server_skills",
                    }
                )
            healer = dict(existing)
            healer.update(
                {
                    "actor_id": actor_id,
                    "name": str(raw_actor.get("name", "")).strip()
                    or str(existing.get("name", "")).strip()
                    or f"玩家 {actor_id}",
                    "profession_id": profession_id,
                    "hps": effective / divisor,
                    "total_healing": total_healing,
                    "effective_healing": effective,
                    "overhealing": overhealing,
                    "overheal_rate": shown_overheal_rate,
                    "overheal_rate_partial": overheal_rate_partial,
                    "overheal_rate_source": (
                        "observed_partial"
                        if overheal_rate_partial
                        else "complete"
                        if shown_overheal_rate is not None
                        else "unavailable"
                    ),
                    "peak_hps": existing.get("peak_hps") if verified else None,
                    "observed_total_healing": observed_total,
                    "observed_effective_healing": observed_effective,
                    "observed_overhealing": observed_overhealing,
                    "observed_overheal_rate": observed_overheal_rate,
                    "coverage": (
                        "server_verified_callbacks"
                        if verified
                        else "server_effective_with_partial_callbacks"
                    ),
                    "skills": skills,
                    "healing_summary_id": summary_id,
                }
            )
            existing_healers[actor_id] = healer
            if effective > 0:
                applied_actor_ids.append(actor_id)

        healers = sorted(
            existing_healers.values(),
            key=lambda row: int(row.get("effective_healing", 0) or 0),
            reverse=True,
        )
        team_effective = sum(
            int(row.get("effective_healing", 0) or 0) for row in healers
        )
        totals = [row.get("total_healing") for row in healers]
        team_total = (
            sum(int(value or 0) for value in totals)
            if all(value is not None for value in totals)
            else None
        )
        updated = dict(record)
        updated["healers"] = healers
        updated["team_hps"] = team_effective / divisor
        updated["team_effective_healing"] = team_effective
        updated["team_total_healing"] = team_total
        updated["team_overhealing"] = (
            team_total - team_effective if team_total is not None else None
        )
        accounting = dict(
            updated.get("healing_accounting", {})
            if isinstance(updated.get("healing_accounting"), dict)
            else {}
        )
        applications = list(accounting.get("stage_healing_summary_applications", []))
        if applied_actor_ids:
            applications.append(
                {
                    "summary_id": summary_id,
                    "actor_ids": sorted(set(applied_actor_ids)),
                    "policy": "exact_actor_damage_match_no_proportional_completion",
                }
            )
        accounting["stage_healing_summary_applications"] = applications
        updated["healing_accounting"] = accounting
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
                upgraded_with_healing, _healing_actor_ids = (
                    self._apply_exact_stage_healing(
                        upgraded, summary, summary_id
                    )
                )
                healing_changed = upgraded_with_healing != upgraded
                upgraded = upgraded_with_healing
                upgraded, clock_changed = self._apply_game_server_team_clock(
                    upgraded, summary, summary_id
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
                if changed or healing_changed or clock_changed:
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
        updated, _applied_healing_actor_ids = self._apply_exact_stage_healing(
            updated, summary, summary_id
        )
        updated, clock_changed = self._apply_game_server_team_clock(
            updated, summary, summary_id
        )
        validation["game_server_team_clock"] = {
            "accepted": clock_changed
            or str(updated.get("duration_source", ""))
            == "game_server_team_clock",
            "local_comparison_used_for_acceptance": False,
        }
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
