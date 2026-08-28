#!/usr/bin/env python3
"""Durable, one-file-per-encounter combat history storage."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path


HISTORY_SCHEMA_VERSION = 1
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")


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
