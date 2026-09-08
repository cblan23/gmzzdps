#!/usr/bin/env python3
"""Durable, one-file-per-encounter combat history storage."""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from math import ceil
from pathlib import Path

from history_index import (
    HistoryIndex,
    build_history_summary,
    rebuild_dps_timeline,
    rebuild_team_dps_timeline,
)


HISTORY_SCHEMA_VERSION = 1
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000
_HEALER_PROFESSION_IDS = frozenset({1_200_002})
_WIPE_DETAIL_MAX_DELAY_SECONDS = 120.0
# A completed stage table can be deferred until the next stage is entered.  A
# long window is safe only for the strict matcher below: stage identity, team
# size, archived total, and every per-player damage value must all agree.
_EXACT_COMPLETION_DETAIL_MAX_DELAY_SECONDS = 24 * 60 * 60.0


@dataclass(frozen=True)
class ResolvedCombatInterval:
    """One canonical combat interval shared by UI and history calculations.

    ``duration_seconds`` is always derived from the stored endpoints.  The
    whole-second DPS/HPS divisor is exposed separately so a history record can
    never claim a duration that disagrees with ``ended - started``.
    """

    started_at_epoch: float = 0.0
    ended_at_epoch: float = 0.0
    duration_seconds: float = 0.0
    source: str = ""
    final: bool = False

    @property
    def valid(self) -> bool:
        return bool(
            self.started_at_epoch > 0.0
            and self.ended_at_epoch >= self.started_at_epoch
            and self.duration_seconds > 0.0
        )

    @property
    def divisor_seconds(self) -> float:
        if not self.valid:
            return 0.0
        return float(max(1, int(self.duration_seconds)))

    @classmethod
    def from_endpoints(
        cls,
        started_at_epoch: object,
        ended_at_epoch: object,
        *,
        source: str,
        final: bool,
        minimum_seconds: float = 1.0,
    ) -> "ResolvedCombatInterval":
        try:
            started_at = float(started_at_epoch)
            ended_at = float(ended_at_epoch)
            minimum = max(0.0, float(minimum_seconds))
        except (TypeError, ValueError, OverflowError):
            return cls(source=str(source), final=bool(final))
        if (
            not math.isfinite(started_at)
            or not math.isfinite(ended_at)
            or started_at <= 0.0
            or ended_at < started_at
        ):
            return cls(source=str(source), final=bool(final))
        if ended_at - started_at < minimum:
            started_at = max(0.0, ended_at - minimum)
            if started_at <= 0.0:
                ended_at = started_at + minimum
        duration = ended_at - started_at
        return cls(
            started_at_epoch=started_at,
            ended_at_epoch=ended_at,
            duration_seconds=duration,
            source=str(source),
            final=bool(final),
        )

    @classmethod
    def from_duration(
        cls,
        duration_seconds: object,
        *,
        source: str,
        final: bool,
        started_at_epoch: object = 0.0,
        ended_at_epoch: object = 0.0,
        minimum_seconds: float = 1.0,
    ) -> "ResolvedCombatInterval":
        try:
            duration = max(float(minimum_seconds), float(duration_seconds))
            started_at = float(started_at_epoch or 0.0)
            ended_at = float(ended_at_epoch or 0.0)
        except (TypeError, ValueError, OverflowError):
            return cls(source=str(source), final=bool(final))
        if not math.isfinite(duration) or duration <= 0.0:
            return cls(source=str(source), final=bool(final))
        if math.isfinite(ended_at) and ended_at > 0.0:
            started_at = ended_at - duration
        elif math.isfinite(started_at) and started_at > 0.0:
            ended_at = started_at + duration
        else:
            return cls(source=str(source), final=bool(final))
        return cls.from_endpoints(
            started_at,
            ended_at,
            source=source,
            final=final,
            minimum_seconds=minimum_seconds,
        )


def interval_iso_timestamp(value: object) -> str:
    """Format a canonical interval endpoint using the local timezone."""

    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(timestamp) or timestamp <= 0.0:
        return ""
    try:
        value = dt.datetime.fromtimestamp(
            timestamp, tz=dt.timezone.utc
        ).astimezone()
    except (OSError, OverflowError, ValueError):
        return ""
    return value.isoformat(timespec="seconds")


def rebase_relative_combat_logs(
    record: dict,
    *,
    started_at_epoch: object,
    duration_seconds: object,
) -> dict:
    """Keep relative event/sample times attached to their absolute moments.

    A shared or game-server clock can move the canonical opening edge earlier
    than the local observer's first packet. Relative history rows therefore
    need the same offset; their damage amounts and all other fields are copied
    unchanged.
    """

    updated = dict(record)
    try:
        new_start = float(started_at_epoch)
        new_duration = max(0.0, float(duration_seconds))
        old_record_start = float(
            record.get("started_at_epoch", new_start) or new_start
        )
    except (TypeError, ValueError, OverflowError):
        return updated
    if (
        not math.isfinite(new_start)
        or new_start <= 0.0
        or not math.isfinite(new_duration)
    ):
        return updated

    event_log = record.get("event_log")
    if isinstance(event_log, dict) and isinstance(event_log.get("rows"), list):
        next_log = dict(event_log)
        try:
            origin = float(
                event_log.get("origin_started_at_epoch", old_record_start)
                or old_record_start
            )
        except (TypeError, ValueError, OverflowError):
            origin = old_record_start
        offset_ms = int(round((origin - new_start) * 1000.0))
        maximum_ms = max(0, int(math.ceil(new_duration * 1000.0)))
        next_rows: list[list[object]] = []
        for raw_row in event_log.get("rows", []):
            if not isinstance(raw_row, (list, tuple)) or not raw_row:
                continue
            row = list(raw_row)
            try:
                row[0] = min(
                    maximum_ms,
                    max(0, int(row[0]) + offset_ms),
                )
            except (TypeError, ValueError, OverflowError):
                continue
            next_rows.append(row)
        next_rows.sort(key=lambda row: int(row[0]))
        next_log["rows"] = next_rows
        next_log["origin_started_at_epoch"] = new_start
        updated["event_log"] = next_log

    skill_cast_log = record.get("skill_cast_log")
    if isinstance(skill_cast_log, dict) and isinstance(
        skill_cast_log.get("rows"), list
    ):
        next_log = dict(skill_cast_log)
        try:
            origin = float(
                skill_cast_log.get("origin_started_at_epoch", old_record_start)
                or old_record_start
            )
        except (TypeError, ValueError, OverflowError):
            origin = old_record_start
        offset_ms = int(round((origin - new_start) * 1000.0))
        maximum_ms = max(0, int(math.ceil(new_duration * 1000.0)))
        next_rows: list[list[object]] = []
        for raw_row in skill_cast_log.get("rows", []):
            if not isinstance(raw_row, (list, tuple)) or not raw_row:
                continue
            row = list(raw_row)
            try:
                row[0] = min(
                    maximum_ms,
                    max(0, int(row[0]) + offset_ms),
                )
            except (TypeError, ValueError, OverflowError):
                continue
            next_rows.append(row)
        next_rows.sort(key=lambda row: int(row[0]))
        next_log["rows"] = next_rows
        next_log["origin_started_at_epoch"] = new_start
        updated["skill_cast_log"] = next_log

    boss_damage = record.get("boss_damage")
    if isinstance(boss_damage, dict):
        next_boss_damage = dict(boss_damage)
        boss_damage_changed = False
        for log_key in ("event_log", "death_event_log"):
            boss_event_log = boss_damage.get(log_key)
            if not isinstance(boss_event_log, dict) or not isinstance(
                boss_event_log.get("rows"), list
            ):
                continue
            next_log = dict(boss_event_log)
            try:
                origin = float(
                    boss_event_log.get(
                        "origin_started_at_epoch", old_record_start
                    )
                    or old_record_start
                )
            except (TypeError, ValueError, OverflowError):
                origin = old_record_start
            offset_ms = int(round((origin - new_start) * 1000.0))
            maximum_ms = max(0, int(math.ceil(new_duration * 1000.0)))
            next_rows: list[list[object]] = []
            for raw_row in boss_event_log.get("rows", []):
                if not isinstance(raw_row, (list, tuple)) or not raw_row:
                    continue
                row = list(raw_row)
                try:
                    row[0] = min(
                        maximum_ms,
                        max(0, int(row[0]) + offset_ms),
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                next_rows.append(row)
            next_rows.sort(key=lambda row: int(row[0]))
            next_log["rows"] = next_rows
            next_log["origin_started_at_epoch"] = new_start
            next_boss_damage[log_key] = next_log
            boss_damage_changed = True
        if boss_damage_changed:
            updated["boss_damage"] = next_boss_damage

    sample_log = record.get("team_damage_samples")
    if isinstance(sample_log, dict) and isinstance(sample_log.get("rows"), list):
        next_log = dict(sample_log)
        try:
            origin = float(
                sample_log.get("origin_started_at_epoch", old_record_start)
                or old_record_start
            )
        except (TypeError, ValueError, OverflowError):
            origin = old_record_start
        offset_seconds = int(origin - new_start)
        maximum_second = max(0, int(new_duration))
        samples: dict[int, int] = {}
        for raw_row in sample_log.get("rows", []):
            if not isinstance(raw_row, (list, tuple)) or len(raw_row) < 2:
                continue
            try:
                second = min(
                    maximum_second,
                    max(0, int(raw_row[0]) + offset_seconds),
                )
                total = max(0, int(raw_row[1]))
            except (TypeError, ValueError, OverflowError):
                continue
            samples[second] = max(samples.get(second, 0), total)
        next_log["rows"] = [
            [second, samples[second]] for second in sorted(samples)
        ]
        next_log["origin_started_at_epoch"] = new_start
        updated["team_damage_samples"] = next_log

    participant_log = record.get("participant_damage_samples")
    if isinstance(participant_log, dict) and isinstance(
        participant_log.get("rows"), list
    ):
        next_log = dict(participant_log)
        try:
            origin = float(
                participant_log.get(
                    "origin_started_at_epoch", old_record_start
                )
                or old_record_start
            )
        except (TypeError, ValueError, OverflowError):
            origin = old_record_start
        offset_seconds = int(origin - new_start)
        maximum_second = max(0, int(new_duration))
        samples: dict[tuple[int, int], int] = {}
        for raw_row in participant_log.get("rows", []):
            if not isinstance(raw_row, (list, tuple)) or len(raw_row) < 3:
                continue
            try:
                second = min(
                    maximum_second,
                    max(0, int(raw_row[0]) + offset_seconds),
                )
                actor_id = int(raw_row[1])
                total = max(0, int(raw_row[2]))
            except (TypeError, ValueError, OverflowError):
                continue
            if actor_id <= 0:
                continue
            key = (second, actor_id)
            samples[key] = max(samples.get(key, 0), total)
        next_log["rows"] = [
            [second, actor_id, samples[(second, actor_id)]]
            for second, actor_id in sorted(samples)
        ]
        next_log["origin_started_at_epoch"] = new_start
        updated["participant_damage_samples"] = next_log
    return updated


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
    def __init__(
        self,
        directory: str | Path,
        *,
        catalog_path: str | Path | None = None,
        profession_path: str | Path | None = None,
    ):
        self.directory = Path(directory)
        self.catalog_path = Path(catalog_path) if catalog_path is not None else None
        self.profession_path = (
            Path(profession_path) if profession_path is not None else None
        )
        # Keep the index lazy.  Capture-only sessions should not create a
        # database until the history page is opened.
        self._history_index: HistoryIndex | None = None

    def _index(self) -> HistoryIndex:
        if self._history_index is None:
            self._history_index = HistoryIndex(
                self.directory,
                catalog_path=self.catalog_path,
                profession_path=self.profession_path,
            )
        return self._history_index

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
                if "note" not in payload:
                    payload["note"] = str(existing.get("note", "") or "")
        payload["favorite"] = bool(payload.get("favorite", False))
        payload["note"] = str(payload.get("note", "") or "").strip()[:2000]
        payload["battle_id"] = encounter_id
        payload.setdefault("archive_format_version", 2)
        payload.setdefault("is_owner", True)
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
        if self._history_index is not None:
            self._history_index.upsert_path(target, self._valid_record)
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
        safe_id = self._safe_encounter_id(encounter_id)
        path = self.directory / f"{safe_id}.json"
        try:
            path.unlink()
            if self._history_index is not None:
                self._history_index.remove_ids([safe_id])
            return True
        except FileNotFoundError:
            return False

    def load(self, encounter_id: object) -> dict | None:
        path = self.directory / f"{self._safe_encounter_id(encounter_id)}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return None
        return record if self._valid_record(record) else None

    def refresh_index(self) -> dict[str, int]:
        return self._index().sync(self._valid_record)

    def query_summaries(
        self,
        filters: dict | None = None,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "time_desc",
        refresh: bool = False,
    ) -> dict[str, object]:
        index = self._index()
        if refresh:
            index.sync(self._valid_record)
        return index.query(filters, page=page, page_size=page_size, sort=sort)

    def history_overview(self, filters: dict | None = None) -> dict[str, object]:
        return self._index().overview(filters)

    def history_options(self) -> dict[str, object]:
        return self._index().options()

    def set_note(self, encounter_id: object, note: object) -> dict | None:
        record = self.load(encounter_id)
        if record is None:
            return None
        record["note"] = str(note or "").strip()[:2000]
        self.save(record)
        return record

    def delete_many(self, encounter_ids: object) -> int:
        if not isinstance(encounter_ids, (list, tuple, set, frozenset)):
            return 0
        deleted_ids: list[str] = []
        for encounter_id in encounter_ids:
            safe_id = self._safe_encounter_id(encounter_id)
            path = self.directory / f"{safe_id}.json"
            try:
                path.unlink()
                deleted_ids.append(safe_id)
            except FileNotFoundError:
                continue
        if deleted_ids and self._history_index is not None:
            self._history_index.remove_ids(deleted_ids)
        return len(deleted_ids)

    def delete_older_than(self, days: int) -> int:
        if not self.directory.is_dir():
            return 0
        cutoff = time.time() - max(0, int(days)) * 86400
        ids: list[str] = []
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
            except (OSError, TypeError, ValueError, OverflowError):
                continue
            if ended_at > 0 and ended_at < cutoff:
                ids.append(str(record.get("encounter_id", path.stem)))
        return self.delete_many(ids)

    def delete_all(self) -> int:
        if not self.directory.is_dir():
            return 0
        deleted = 0
        deleted_ids: list[str] = []
        for path in self.directory.glob("*.json"):
            try:
                path.unlink()
                deleted += 1
                deleted_ids.append(path.stem)
            except FileNotFoundError:
                continue
        if deleted_ids and self._history_index is not None:
            self._history_index.remove_ids(deleted_ids)
        return deleted

    def export_records(
        self,
        encounter_ids: object,
        destination: str | Path,
        export_format: str,
    ) -> int:
        if not isinstance(encounter_ids, (list, tuple, set, frozenset)):
            return 0
        records = [self.load(value) for value in encounter_ids]
        records = [record for record in records if isinstance(record, dict)]
        if not records:
            return 0
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        export_format = str(export_format or "").casefold()
        if export_format == "json":
            payload = {
                "export_version": 1,
                "exported_at_epoch": time.time(),
                "battle_count": len(records),
                "battles": records,
            }
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return len(records)
        if export_format != "csv":
            raise ValueError("unsupported combat history export format")

        # CSV is deliberately a flat summary export.  JSON retains complete
        # player, skill, target and diagnostic collections.
        summaries = [
            build_history_summary(
                record,
                self._index().catalog,
                self._index().profession_names,
            )
            for record in records
        ]
        fieldnames = [
            "battle_id",
            "started_at",
            "ended_at",
            "duration_seconds",
            "dungeon_name",
            "stage_id",
            "boss_name",
            "boss_count",
            "result",
            "team_size",
            "my_name",
            "profession_name",
            "my_damage",
            "my_dps",
            "my_share",
            "total_damage",
            "team_dps",
            "completeness",
            "note",
        ]
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for summary in summaries:
                writer.writerow({key: summary.get(key) for key in fieldnames})
        return len(records)

    def recalculate(self, encounter_id: object) -> tuple[dict | None, str]:
        record = self.load(encounter_id)
        if record is None:
            return None, "missing"
        try:
            duration = max(0.0, float(record.get("duration_seconds", 0.0) or 0.0))
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
        participants = record.get("participants")
        if isinstance(participants, list):
            total_damage = sum(
                max(0, int(row.get("damage", 0) or 0))
                for row in participants
                if isinstance(row, dict)
            )
            if total_damage > 0:
                record["total_damage"] = total_damage
            divisor = duration if duration > 0 else 1.0
            for row in participants:
                if not isinstance(row, dict):
                    continue
                damage = max(0, int(row.get("damage", 0) or 0))
                row["dps"] = damage / divisor if duration > 0 else 0.0
                row["share"] = damage / total_damage if total_damage else 0.0
            record["team_dps"] = (
                total_damage / divisor if duration > 0 else 0.0
            )
        timeline = rebuild_dps_timeline(record)
        if timeline:
            record["dps_timeline"] = timeline
            status = "complete"
        else:
            status = "summary_only"
        team_timeline = rebuild_team_dps_timeline(record)
        if team_timeline:
            record["team_dps_timeline"] = team_timeline
            status = "complete"
        else:
            record.pop("team_dps_timeline", None)
        record["recalculated_at_epoch"] = time.time()
        record["recalculation_status"] = status
        self.save(record)
        return record, status

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
        deleted_ids: list[str] = []
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
                deleted_ids.append(path.stem)
            except FileNotFoundError:
                continue
        if deleted_ids and self._history_index is not None:
            self._history_index.remove_ids(deleted_ids)
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
    def _validated_actor_merges(
        summary: object,
    ) -> tuple[dict[int, int], list[dict]]:
        """Return only token-proven provisional-to-real actor mappings.

        Names and professions are deliberately excluded from this decision.
        A mapping is accepted only when the completion table contains the same
        positive actor ID and user token carried by the parser's merge event.
        """
        if not isinstance(summary, dict):
            return {}, []
        summary_tokens: dict[int, str] = {}
        for raw_actor in summary.get("actors", []):
            if not isinstance(raw_actor, dict):
                continue
            try:
                actor_id = int(raw_actor.get("actor_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            token = str(raw_actor.get("user_token", "")).strip()
            if actor_id > 0 and token:
                summary_tokens[actor_id] = token

        raw_merges = summary.get("actor_merges", [])
        if not isinstance(raw_merges, list):
            return {}, []
        candidates: dict[int, dict] = {}
        targets: dict[int, int] = {}
        conflicts: set[int] = set()
        conflicting_targets: set[int] = set()
        for raw_merge in raw_merges:
            if not isinstance(raw_merge, dict):
                continue
            try:
                old_actor = int(raw_merge.get("from_actor_id", 0) or 0)
                new_actor = int(raw_merge.get("to_actor_id", 0) or 0)
                timestamp = int(raw_merge.get("filetime_100ns", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            token = str(raw_merge.get("user_token", "")).strip()
            if (
                old_actor >= 0
                or new_actor <= 0
                or old_actor == new_actor
                or not token
                or summary_tokens.get(new_actor) != token
            ):
                continue
            normalized = {
                "from_actor_id": old_actor,
                "to_actor_id": new_actor,
                "user_token": token,
            }
            if timestamp > 0:
                normalized["filetime_100ns"] = timestamp
            previous = candidates.get(old_actor)
            if previous is not None:
                same_identity = (
                    int(previous["to_actor_id"]) == new_actor
                    and str(previous["user_token"]) == token
                )
                if not same_identity:
                    conflicts.add(old_actor)
                    continue
                # Repeated parser evidence for the same token-backed mapping
                # is not a conflict merely because it was observed again at a
                # later FILETIME.  Retain the newest timestamp for diagnostics.
                previous_timestamp = int(
                    previous.get("filetime_100ns", 0) or 0
                )
                if timestamp > previous_timestamp:
                    candidates[old_actor] = normalized
                continue
            previous_old = targets.get(new_actor)
            if previous_old is not None and previous_old != old_actor:
                conflicting_targets.add(new_actor)
                continue
            candidates[old_actor] = normalized
            targets[new_actor] = old_actor

        for old_actor in conflicts:
            candidate = candidates.pop(old_actor, None)
            if candidate is not None:
                targets.pop(int(candidate["to_actor_id"]), None)
        for new_actor in conflicting_targets:
            old_actor = targets.pop(new_actor, None)
            if old_actor is not None:
                candidates.pop(old_actor, None)
        normalized_merges = [
            candidates[old_actor] for old_actor in sorted(candidates)
        ]
        return (
            {
                old_actor: int(candidate["to_actor_id"])
                for old_actor, candidate in candidates.items()
            },
            normalized_merges,
        )

    @staticmethod
    def _actor_damage(
        value: object,
        actor_aliases: dict[int, int] | None = None,
    ) -> dict[int, int]:
        if not isinstance(value, list):
            return {}
        aliases = actor_aliases or {}
        result: dict[int, int] = {}
        for row in value:
            if not isinstance(row, dict):
                continue
            try:
                actor_id = int(row.get("actor_id", 0) or 0)
                damage = max(0, int(row.get("damage", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            actor_id = aliases.get(actor_id, actor_id)
            if actor_id > 0:
                result[actor_id] = result.get(actor_id, 0) + damage
        return result

    @classmethod
    def _exact_wipe_detail_match(
        cls,
        record: object,
        summary: object,
        *,
        max_delay_seconds: float = _WIPE_DETAIL_MAX_DELAY_SECONDS,
    ) -> bool:
        """Accept an unconfirmed post-wipe table only on exact pull identity."""
        if not isinstance(record, dict) or not isinstance(summary, dict):
            return False
        if (
            str(record.get("archive_reason", "")).strip() != "party_wipe"
            or bool(summary.get("authoritative"))
            or bool(summary.get("completion_confirmed"))
            or bool(summary.get("realtime_detail"))
        ):
            return False
        summary_id = str(summary.get("summary_id", "")).strip()
        summary_parts = summary_id.split("|", 2)
        player_detail_query = bool(summary.get("player_detail_query"))
        try:
            stage_id = int(summary.get("stage_id", summary_parts[0]) or 0)
            timestamp = int(summary.get("filetime_100ns", 0) or 0)
            ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
            member_count = max(0, int(summary.get("member_count", 0) or 0))
            record_team_size = max(0, int(record.get("team_size", 0) or 0))
            record_total = max(0, int(record.get("total_damage", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            len(summary_parts) != 3
            or (stage_id <= 0 and not player_detail_query)
            or timestamp <= _FILETIME_EPOCH_OFFSET
            or ended_at <= 0
            or member_count < 2
            or (record_team_size > 0 and member_count != record_team_size)
        ):
            return False
        summary_time = (timestamp - _FILETIME_EPOCH_OFFSET) / 10_000_000
        delay = summary_time - ended_at
        if delay < -5.0 or delay > min(
            max_delay_seconds, _WIPE_DETAIL_MAX_DELAY_SECONDS
        ):
            return False

        actor_aliases, _normalized_merges = cls._validated_actor_merges(summary)
        expected = {
            actor_id: damage
            for actor_id, damage in cls._actor_damage(
                summary.get("actors")
            ).items()
            if damage > 0
        }
        observed = {
            actor_id: damage
            for actor_id, damage in cls._actor_damage(
                record.get("participants"), actor_aliases
            ).items()
            if damage > 0
        }
        if len(expected) < 2 or expected != observed:
            return False
        if record_total != sum(observed.values()):
            return False
        return any(
            isinstance(actor, dict)
            and (
                bool(actor.get("skills"))
                or actor.get("damage_hits") is not None
                or actor.get("critical_hits") is not None
                or actor.get("penetration_hits") is not None
            )
            for actor in summary.get("actors", [])
        )

    @staticmethod
    def _summary_stage_id(summary: object) -> int:
        if not isinstance(summary, dict):
            return 0
        try:
            stage_id = max(0, int(summary.get("stage_id", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            stage_id = 0
        if stage_id:
            return stage_id
        summary_id = str(summary.get("summary_id", "") or "")
        for part in summary_id.split("|"):
            try:
                candidate = int(part)
            except (TypeError, ValueError, OverflowError):
                continue
            if candidate >= 100_000:
                return candidate
        return 0

    @staticmethod
    def _record_stage_ids(record: object) -> set[int]:
        if not isinstance(record, dict):
            return set()
        sources = [record]
        capture = record.get("capture_pipeline_at_archive")
        if isinstance(capture, dict):
            sources.append(capture)
        stage_ids: set[int] = set()
        for source in sources:
            for key in ("dungeon_stage_id", "stage_id"):
                try:
                    stage_id = max(0, int(source.get(key, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    stage_id = 0
                if stage_id:
                    stage_ids.add(stage_id)
        return stage_ids

    @classmethod
    def _exact_delayed_completion_detail_match(
        cls,
        record: object,
        summary: object,
        *,
        normal_delay_seconds: float,
        maximum_delay_seconds: float = _EXACT_COMPLETION_DETAIL_MAX_DELAY_SECONDS,
    ) -> bool:
        """Prove that a delayed completion table belongs to one archived pull.

        This path deliberately does not use names, professions, percentages, or
        approximate totals.  It exists only for completion packets delayed past
        the ordinary matching window and is strict enough to remain detail-only.
        """
        if not isinstance(record, dict) or not isinstance(summary, dict):
            return False
        if not (
            bool(summary.get("authoritative"))
            and bool(summary.get("completion_confirmed"))
        ):
            return False
        stage_id = cls._summary_stage_id(summary)
        if stage_id <= 0 or stage_id not in cls._record_stage_ids(record):
            return False
        try:
            timestamp = int(summary.get("filetime_100ns", 0) or 0)
            ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
            record_total = max(0, int(record.get("total_damage", 0) or 0))
            record_team_size = max(0, int(record.get("team_size", 0) or 0))
            member_count = max(0, int(summary.get("member_count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return False
        if timestamp <= _FILETIME_EPOCH_OFFSET or ended_at <= 0:
            return False
        delay = (
            (timestamp - _FILETIME_EPOCH_OFFSET) / 10_000_000
        ) - ended_at
        if not normal_delay_seconds < delay <= maximum_delay_seconds:
            return False

        actor_aliases, _normalized_merges = cls._validated_actor_merges(summary)
        expected = cls._actor_damage(summary.get("actors"))
        observed = cls._actor_damage(record.get("participants"), actor_aliases)
        # Long-delay matching is team-only and requires the complete vector.
        # This rejects same-total pulls with even one player distributed
        # differently and also rejects partial detail responses.
        if len(expected) < 2 or expected != observed:
            return False
        if member_count != len(expected):
            return False
        if record_team_size > 0 and member_count != record_team_size:
            return False
        if record_total <= 0 or record_total != sum(observed.values()):
            return False
        return any(
            isinstance(actor, dict)
            and (
                bool(actor.get("skills"))
                or actor.get("damage_hits") is not None
                or actor.get("critical_hits") is not None
                or actor.get("penetration_hits") is not None
            )
            for actor in summary.get("actors", [])
        )

    @classmethod
    def _apply_game_server_team_clock(
        cls,
        record: dict,
        summary: dict,
        summary_id: str,
    ) -> tuple[dict, bool]:
        """Apply only a final settlement clock matched by damage identity.

        The settlement clock changes the canonical endpoints and DPS/HPS
        divisors together. Damage and healing totals remain byte-for-byte
        sourced from the existing record. No local-time tolerance is used
        here because a damage-matched game settlement is authoritative.
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

        actor_aliases, _normalized_merges = cls._validated_actor_merges(
            summary
        )
        expected = cls._actor_damage(summary.get("actors"))
        observed = cls._actor_damage(
            record.get("participants"), actor_aliases
        )
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
        interval = ResolvedCombatInterval.from_duration(
            duration,
            ended_at_epoch=ended_at,
            started_at_epoch=started_at,
            source="game_server_team_clock",
            final=True,
        )
        if not interval.valid:
            return record, False

        updated = dict(record)
        updated.pop("_shared_clock_request", None)
        updated = rebase_relative_combat_logs(
            updated,
            started_at_epoch=interval.started_at_epoch,
            duration_seconds=interval.duration_seconds,
        )
        updated["participants"] = participants
        if isinstance(raw_healers, list):
            updated["healers"] = healers
        updated["started_at_epoch"] = interval.started_at_epoch
        updated["ended_at_epoch"] = interval.ended_at_epoch
        updated["started_at"] = interval_iso_timestamp(
            interval.started_at_epoch
        )
        updated["ended_at"] = interval_iso_timestamp(interval.ended_at_epoch)
        updated["duration_seconds"] = (
            updated["ended_at_epoch"] - updated["started_at_epoch"]
        )
        updated["dps_duration_seconds"] = interval.divisor_seconds
        updated["hps_duration_seconds"] = interval.divisor_seconds
        updated["healing_started_at_epoch"] = interval.started_at_epoch
        updated["healing_ended_at_epoch"] = interval.ended_at_epoch
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
        if isinstance(updated.get("event_log"), dict):
            timeline = rebuild_dps_timeline(updated)
            if timeline:
                updated["dps_timeline"] = timeline
        team_timeline = rebuild_team_dps_timeline(updated)
        if team_timeline:
            updated["team_dps_timeline"] = team_timeline
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
    def _stage_actor_detail_match_mode(
        summary_damage: int,
        common_damage: int,
        classified_damage: int,
    ) -> str:
        """Return the actor-local proof used to accept server detail rows."""
        if summary_damage == common_damage:
            return "exact_actor_total"
        if summary_damage > common_damage and classified_damage == common_damage:
            return "classified_skills_match_common_total"
        return ""

    @staticmethod
    def _stage_actor_classified_damage(raw_actor: object) -> int:
        if not isinstance(raw_actor, dict):
            return 0
        classified_damage = 0
        for raw_skill in raw_actor.get("skills", []):
            if not isinstance(raw_skill, dict):
                continue
            try:
                skill_id = int(raw_skill.get("skill_id", 0) or 0)
                skill_damage = max(
                    0, int(raw_skill.get("damage", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                continue
            if skill_id > 0:
                classified_damage += skill_damage
        return classified_damage

    @staticmethod
    def _apply_exact_stage_skills(
        record: dict,
        summary: dict,
        summary_id: str,
        *,
        missing_only: bool = False,
    ) -> tuple[dict, list[int]]:
        """Apply actor-local detail proven by totals or classified skill sums."""
        actor_aliases, _normalized_merges = (
            CombatHistoryStore._validated_actor_merges(summary)
        )
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
            canonical_actor_id = actor_aliases.get(actor_id, actor_id)
            summary_actor = summary_actors.get(canonical_actor_id)
            if summary_actor is None or participant_damage <= 0:
                continue
            try:
                summary_damage = max(
                    0, int(summary_actor.get("damage", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
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
            match_mode = CombatHistoryStore._stage_actor_detail_match_mode(
                summary_damage,
                participant_damage,
                classified_damage,
            )
            if not match_mode:
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
            participant["skill_detail_status"] = "complete"
            participant["skill_reconciliation_mode"] = match_mode
            participant["server_reported_damage"] = summary_damage
            participant["server_reported_unclassified_damage"] = max(
                0, summary_damage - classified_damage
            )
            participant["server_damage_difference"] = (
                summary_damage - participant_damage
            )
            applied_actor_ids.append(canonical_actor_id)

        updated = dict(record)
        updated["participants"] = participants
        return updated, sorted(set(applied_actor_ids))

    @staticmethod
    def _apply_exact_stage_metrics(
        record: dict,
        summary: dict,
    ) -> tuple[dict, list[int]]:
        """Apply crit/penetration counters after actor-local reconciliation."""
        actor_aliases, _normalized_merges = (
            CombatHistoryStore._validated_actor_merges(summary)
        )
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
            if bool(summary.get("player_detail_query")) and bool(
                participant.get("is_self")
            ):
                continue
            try:
                actor_id = int(participant.get("actor_id", 0) or 0)
                participant_damage = max(
                    0, int(participant.get("damage", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                continue
            canonical_actor_id = actor_aliases.get(actor_id, actor_id)
            summary_actor = summary_actors.get(canonical_actor_id)
            if summary_actor is None or participant_damage <= 0:
                continue
            try:
                summary_damage = max(
                    0, int(summary_actor.get("damage", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                continue
            raw_damage_hits = summary_actor.get("damage_hits")
            classified_damage = CombatHistoryStore._stage_actor_classified_damage(
                summary_actor
            )
            match_mode = CombatHistoryStore._stage_actor_detail_match_mode(
                summary_damage,
                participant_damage,
                classified_damage,
            )
            if not match_mode or raw_damage_hits is None:
                continue
            try:
                damage_hits = max(0, int(raw_damage_hits))
            except (TypeError, ValueError, OverflowError):
                continue
            if damage_hits <= 0:
                continue
            applied = False
            raw_critical_hits = summary_actor.get("critical_hits")
            if raw_critical_hits is not None:
                try:
                    critical_hits = max(0, int(raw_critical_hits))
                except (TypeError, ValueError, OverflowError):
                    critical_hits = damage_hits + 1
                if critical_hits <= damage_hits:
                    participant["damage_hits"] = damage_hits
                    participant["critical_hits"] = critical_hits
                    participant["critical_rate"] = critical_hits / damage_hits
                    applied = True
            raw_penetration_hits = summary_actor.get("penetration_hits")
            if raw_penetration_hits is not None:
                try:
                    penetration_hits = max(0, int(raw_penetration_hits))
                except (TypeError, ValueError, OverflowError):
                    penetration_hits = damage_hits + 1
                if penetration_hits <= damage_hits:
                    participant["damage_hits"] = damage_hits
                    participant["penetration_hits"] = penetration_hits
                    participant["penetration_rate"] = (
                        penetration_hits / damage_hits
                    )
                    applied = True
            if applied:
                applied_actor_ids.append(canonical_actor_id)

        updated = dict(record)
        updated["participants"] = participants
        return updated, sorted(set(applied_actor_ids))

    @classmethod
    def restore_exact_stage_skills_for_display(cls, record: object) -> object:
        """Restore trusted skill/rate details without changing archived totals.

        Older history files can contain the authoritative completion table but
        predate the code that copied its exact teammate skill rows into the
        participant details. Exact post-wipe stage tables are also accepted,
        but only when every positive per-player damage value matches the wipe.
        This method is intentionally display-only and returns a copied record.
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
            if not isinstance(summary, dict) or not isinstance(
                summary.get("actors"), list
            ):
                continue
            trusted_completion = bool(
                summary.get("authoritative")
                and summary.get("completion_confirmed")
            )
            exact_wipe_detail = cls._exact_wipe_detail_match(record, summary)
            if not trusted_completion and not exact_wipe_detail:
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
            candidate, metric_actor_ids = cls._apply_exact_stage_metrics(
                restored, summary
            )
            if metric_actor_ids:
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
        actor_aliases, _normalized_merges = (
            CombatHistoryStore._validated_actor_merges(summary)
        )
        participant_damage = CombatHistoryStore._actor_damage(
            record.get("participants"), actor_aliases
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
                participant_id = actor_aliases.get(
                    participant_id, participant_id
                )
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
            actor_id = actor_aliases.get(actor_id, actor_id)
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
                "policy": (
                    "actor_total_or_classified_skills_match_common_total"
                ),
            }
        )
        accounting["stage_skill_summary_applications"] = applications
        accounting["stage_skill_policy"] = (
            "server_skill_amounts_are_used_when_the_actor_total_matches_or_"
            "classified_skills_exactly_match_the_common_total"
        )
        accounting["skill_reconciliation"] = cls._skill_reconciliation(
            participants
        )
        return accounting, True

    def attach_exact_wipe_details(
        self,
        summary: dict,
        *,
        max_delay_seconds: float = _WIPE_DETAIL_MAX_DELAY_SECONDS,
    ) -> dict | None:
        """Attach only skills and crit/penetration rates to an exact wipe."""
        if not isinstance(summary, dict):
            return None
        summary_id = str(summary.get("summary_id", "")).strip()
        if not summary_id:
            return None
        try:
            timestamp = int(summary.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if timestamp <= _FILETIME_EPOCH_OFFSET:
            return None
        summary_time = (timestamp - _FILETIME_EPOCH_OFFSET) / 10_000_000
        candidates: list[tuple[float, dict]] = []
        for record in self.load_recent(50):
            if not self._exact_wipe_detail_match(
                record,
                summary,
                max_delay_seconds=max_delay_seconds,
            ):
                continue
            try:
                ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                continue
            candidates.append((abs(summary_time - ended_at), record))
        if not candidates:
            return None

        record = min(candidates, key=lambda item: item[0])[1]
        actor_aliases, normalized_merges = self._validated_actor_merges(summary)
        expected = self._actor_damage(summary.get("actors"))
        observed = self._actor_damage(record.get("participants"), actor_aliases)
        actor_ids = sorted(set(expected) | set(observed))
        actor_differences = [
            {
                "actor_id": actor_id,
                "realtime_damage": observed.get(actor_id, 0),
                "summary_damage": expected.get(actor_id, 0),
                "difference": expected.get(actor_id, 0)
                - observed.get(actor_id, 0),
            }
            for actor_id in actor_ids
        ]
        updated, applied_skill_actor_ids = self._apply_exact_stage_skills(
            record, summary, summary_id
        )
        updated, applied_metric_actor_ids = self._apply_exact_stage_metrics(
            updated, summary
        )

        next_accounting = dict(
            updated.get("damage_accounting", {})
            if isinstance(updated.get("damage_accounting"), dict)
            else {}
        )
        validations = [
            dict(item)
            for item in next_accounting.get("stage_summary_validations", [])
            if isinstance(item, dict)
        ]
        existing_index = next(
            (
                index
                for index, item in enumerate(validations)
                if str(item.get("summary_id", "")) == summary_id
            ),
            None,
        )
        validation = {
            "summary_total": sum(expected.values()),
            "observed_total": sum(observed.values()),
            "difference": 0,
            "absolute_difference": 0,
            "summary_delay_seconds": round(
                summary_time - float(record.get("ended_at_epoch", 0.0) or 0.0),
                6,
            ),
            "end_snapshot": True,
            "would_have_matched": True,
            "summary_covers_observed": True,
            "encounter_concluded": True,
            "per_actor_exact_match": True,
            "actor_differences": actor_differences,
            "wipe_detail_confirmed": True,
            "damage_correction_applied": False,
            "server_skill_actor_ids": applied_skill_actor_ids,
            "server_metric_actor_ids": applied_metric_actor_ids,
            "attached_to_archived_record": True,
            "applied_fields": [
                "skills",
                "damage_hits",
                "critical_hits",
                "critical_rate",
                "penetration_hits",
                "penetration_rate",
            ],
        }
        attached = {
            "summary_id": summary_id,
            "filetime_100ns": timestamp,
            "authoritative": False,
            "completion_confirmed": False,
            "realtime_detail": False,
            "player_detail_query": bool(summary.get("player_detail_query")),
            "validation_only": True,
            "validation": validation,
            "member_count": int(summary.get("member_count", 0) or 0),
            "actors": [
                dict(row)
                for row in summary.get("actors", [])
                if isinstance(row, dict)
            ],
            "actor_merges": [dict(item) for item in normalized_merges],
        }
        if existing_index is None:
            validations.append(attached)
        else:
            validations[existing_index] = attached
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

    def _attach_exact_delayed_completion_details(
        self,
        record: dict,
        summary: dict,
    ) -> dict | None:
        """Attach only detail fields from a strictly matched delayed table."""
        summary_id = str(summary.get("summary_id", "")).strip()
        try:
            timestamp = int(summary.get("filetime_100ns", 0) or 0)
            ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return None
        if not summary_id or timestamp <= _FILETIME_EPOCH_OFFSET:
            return None

        actor_aliases, normalized_merges = self._validated_actor_merges(summary)
        expected = self._actor_damage(summary.get("actors"))
        observed = self._actor_damage(record.get("participants"), actor_aliases)
        updated, applied_skill_actor_ids = self._apply_exact_stage_skills(
            record, summary, summary_id
        )
        updated, applied_metric_actor_ids = self._apply_exact_stage_metrics(
            updated, summary
        )
        if not applied_skill_actor_ids and not applied_metric_actor_ids:
            return None

        summary_time = (timestamp - _FILETIME_EPOCH_OFFSET) / 10_000_000
        actor_ids = sorted(set(expected) | set(observed))
        actor_differences = [
            {
                "actor_id": actor_id,
                "realtime_damage": observed.get(actor_id, 0),
                "summary_damage": expected.get(actor_id, 0),
                "difference": expected.get(actor_id, 0) - observed.get(actor_id, 0),
            }
            for actor_id in actor_ids
        ]
        applied_fields: list[str] = []
        if applied_skill_actor_ids:
            applied_fields.extend(("skills", "skill_hits"))
        if applied_metric_actor_ids:
            applied_fields.extend(
                (
                    "damage_hits",
                    "critical_hits",
                    "critical_rate",
                    "penetration_hits",
                    "penetration_rate",
                )
            )
        validation = {
            "summary_total": sum(expected.values()),
            "observed_total": sum(observed.values()),
            "difference": 0,
            "absolute_difference": 0,
            "summary_delay_seconds": round(summary_time - ended_at, 6),
            "end_snapshot": True,
            "would_have_matched": True,
            "summary_covers_observed": True,
            "encounter_concluded": True,
            "per_actor_exact_match": True,
            "actor_differences": actor_differences,
            "delayed_exact_detail_only": True,
            "damage_correction_applied": False,
            "duration_correction_applied": False,
            "healing_correction_applied": False,
            "server_skill_actor_ids": applied_skill_actor_ids,
            "server_metric_actor_ids": applied_metric_actor_ids,
            "attached_to_archived_record": True,
            "applied_fields": applied_fields,
        }
        attached = {
            "summary_id": summary_id,
            "filetime_100ns": timestamp,
            "stage_id": self._summary_stage_id(summary),
            "authoritative": True,
            "completion_confirmed": True,
            "realtime_detail": False,
            "validation_only": True,
            "detail_only": True,
            "validation": validation,
            "member_count": int(summary.get("member_count", 0) or 0),
            "actors": [
                dict(row)
                for row in summary.get("actors", [])
                if isinstance(row, dict)
            ],
            "actor_merges": [dict(item) for item in normalized_merges],
        }

        next_accounting = dict(
            updated.get("damage_accounting", {})
            if isinstance(updated.get("damage_accounting"), dict)
            else {}
        )
        validations = [
            dict(item)
            for item in next_accounting.get("stage_summary_validations", [])
            if isinstance(item, dict)
            and str(item.get("summary_id", "")) != summary_id
        ]
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
        actor_aliases, normalized_merges = self._validated_actor_merges(
            summary
        )
        expected = self._actor_damage(summary.get("actors"))
        if timestamp <= _FILETIME_EPOCH_OFFSET or not expected:
            return None
        summary_time = (timestamp - _FILETIME_EPOCH_OFFSET) / 10_000_000
        candidates: list[tuple[tuple[float, ...], dict]] = []
        delayed_exact_candidates: list[dict] = []
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
                upgraded_with_metrics, _metric_actor_ids = (
                    self._apply_exact_stage_metrics(upgraded, summary)
                )
                metrics_changed = upgraded_with_metrics != upgraded
                upgraded = upgraded_with_metrics
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
                validation_evidence_changed = False
                next_validations: list[dict] = []
                for item in validations:
                    if not isinstance(item, dict):
                        continue
                    validation_item = dict(item)
                    if str(item.get("summary_id", "")) == summary_id:
                        validation = dict(
                            item.get("validation", {})
                            if isinstance(item.get("validation"), dict)
                            else {}
                        )
                        next_skill_ids = sorted(set(actor_ids))
                        next_metric_ids = sorted(set(_metric_actor_ids))
                        if validation.get("server_skill_actor_ids") != next_skill_ids:
                            validation["server_skill_actor_ids"] = next_skill_ids
                            validation_evidence_changed = True
                        if validation.get("server_metric_actor_ids") != next_metric_ids:
                            validation["server_metric_actor_ids"] = next_metric_ids
                            validation_evidence_changed = True
                        validation_item["validation"] = validation
                        if normalized_merges:
                            _aliases, existing_merges = (
                                self._validated_actor_merges(item)
                            )
                            if existing_merges != normalized_merges:
                                validation_item["actor_merges"] = [
                                    dict(actor_merge)
                                    for actor_merge in normalized_merges
                                ]
                                validation_evidence_changed = True
                    next_validations.append(validation_item)
                next_accounting["stage_summary_validations"] = next_validations
                next_accounting, changed = self._record_stage_skill_application(
                    next_accounting,
                    upgraded.get("participants"),
                    summary_id,
                    actor_ids,
                )
                if (
                    changed
                    or metrics_changed
                    or healing_changed
                    or clock_changed
                    or validation_evidence_changed
                ):
                    upgraded["damage_accounting"] = next_accounting
                    self.save(upgraded)
                    return upgraded
                return record
            observed = self._actor_damage(
                record.get("participants"), actor_aliases
            )
            overlap = set(expected).intersection(observed)
            if not observed or len(overlap) < ceil(min(len(expected), len(observed)) / 2):
                continue
            try:
                ended_at = float(record.get("ended_at_epoch", 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                continue
            delay = summary_time - ended_at
            if delay > max_delay_seconds:
                if self._exact_delayed_completion_detail_match(
                    record,
                    summary,
                    normal_delay_seconds=max_delay_seconds,
                ):
                    delayed_exact_candidates.append(record)
                continue
            if delay < -5.0:
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
        if delayed_exact_candidates:
            # An identical full damage vector appearing twice in the same
            # stage is ambiguous even if unlikely. Never guess which pull owns
            # a delayed packet.
            if len(delayed_exact_candidates) != 1:
                return None
            return self._attach_exact_delayed_completion_details(
                delayed_exact_candidates[0], summary
            )
        if not candidates:
            return None

        record = min(candidates, key=lambda item: item[0])[1]
        observed = self._actor_damage(
            record.get("participants"), actor_aliases
        )
        expected_total = sum(expected.values())
        observed_total = sum(observed.values())
        participant_names: dict[int, str] = {}
        for row in record.get("participants", []):
            if not isinstance(row, dict):
                continue
            try:
                actor_id = int(row.get("actor_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            actor_id = actor_aliases.get(actor_id, actor_id)
            if actor_id > 0:
                participant_names[actor_id] = str(row.get("name", ""))
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
            "actor_merges": [
                dict(actor_merge) for actor_merge in normalized_merges
            ],
        }
        updated, applied_skill_actor_ids = self._apply_exact_stage_skills(
            record, summary, summary_id
        )
        updated, applied_metric_actor_ids = self._apply_exact_stage_metrics(
            updated, summary
        )
        updated, _applied_healing_actor_ids = self._apply_exact_stage_healing(
            updated, summary, summary_id
        )
        validation["server_skill_actor_ids"] = applied_skill_actor_ids
        validation["server_metric_actor_ids"] = applied_metric_actor_ids
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
