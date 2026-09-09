#!/usr/bin/env python3
"""Indexed summaries for the local combat-history archive.

The JSON encounter files remain the source of truth.  This module keeps a
small, rebuildable SQLite index so opening and filtering history never has to
load every complete battle into memory.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator


HISTORY_INDEX_SCHEMA_VERSION = 10
HISTORY_INDEX_FILENAME = "history-index.sqlite3"

RESULT_DEFEATED = "defeated"
RESULT_FAILED = "failed"
RESULT_INTERRUPTED = "interrupted"
RESULT_UNDETERMINED = "undetermined"

COMPLETENESS_LIMITED = "limited"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_COMPLETE = "complete"

_COMPLETENESS_RANK = {
    COMPLETENESS_LIMITED: 0,
    COMPLETENESS_PARTIAL: 1,
    COMPLETENESS_COMPLETE: 2,
}
FIRST_BELIEVER_TEMPLATE_GROUPS = (
    frozenset({7_100_201, 7_100_202}),
    frozenset({7_100_208, 7_100_209}),
)
FIRST_BELIEVER_PHASE_TEMPLATE_IDS = frozenset(
    {
        7_100_201,
        7_100_202,
        7_100_203,
        7_100_208,
        7_100_209,
        7_100_210,
    }
)
FIRST_BELIEVER_CANONICAL_TEMPLATE_ID = 7_102_980
_STAGE_ID_RE = re.compile(r"(?:settlement|stage)[|:]([0-9]{6,})", re.I)
_EXTRAORDINARY_RATING_KEYS = (
    "extraordinary_rating",
    "extraordinary_score",
    "transcendent_rating",
    "transcendent_score",
    "非凡评分",
)

HISTORY_DUNGEON_BOSSES = {
    "黑荆棘事件簿": (
        "卡尔·埃德加",
        "朗伯·绞索",
        "洛克·金",
        "亚巴顿",
    ),
    "安提哥努斯笔记": ("瑞尔比伯", "小丑"),
    "五月庄园": (
        "异化猎犬",
        "先祖铠甲",
        "星象仪者",
        "子嗣守护",
        "一号信徒",
        "子爵夫人",
    ),
    "记忆的传承": ("强尼", "钻头", "邦尼", "战争巨龙"),
    "木桩": (),
}
HISTORY_DUNGEON_ALIASES = {
    "黑荆棘事件簿": ("黑荆棘事件簿", "黑荆棘"),
    "安提哥努斯笔记": ("安提哥努斯笔记",),
    # The old client-facing castle label said "记忆的传承·城堡". Keep it
    # searchable under 五月庄园 without mixing in the newer memory stages.
    "五月庄园": (
        "五月庄园",
        "五月庄园·花园",
        "五月庄园·城堡",
        "记忆的传承·城堡",
    ),
    "记忆的传承": (
        "记忆的传承",
        "记忆的传承·强尼",
        "记忆的传承·火龙",
        "记忆的传承·邦尼",
        "记忆的传承·钻头",
        "记忆的传承·噩梦",
    ),
    "木桩": ("木桩",),
}
HISTORY_BOSS_ALIASES = {
    "瑞尔比伯": (
        "瑞尔比伯",
        "瑞尔·比伯",
        "周本-瑞尔比伯",
        "英雄周本-瑞尔比伯",
    ),
    "强尼": (
        "强尼",
        '"剥面人" 强尼',
        "“剥面人” 强尼",
        "剥面人·强尼",
    ),
    "钻头": ("钻头", '"钻头"', "“钻头”"),
}
_HISTORY_DUNGEON_DISPLAY_NAMES = {
    "五月庄园": "五月庄园·花园",
    "记忆的传承": "记忆的传承",
}
_HISTORY_BOSS_DUNGEON_DISPLAY_NAMES = {
    "子嗣守护": "五月庄园·城堡",
    "一号信徒": "五月庄园·城堡",
    "子爵夫人": "五月庄园·城堡",
}
_HISTORY_BOSS_CANONICAL_NAMES = {
    alias.casefold(): canonical
    for canonical, aliases in HISTORY_BOSS_ALIASES.items()
    for alias in aliases
}
_HISTORY_BOSS_CANONICAL_NAMES["洛克·金·失控".casefold()] = "洛克·金"


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default


def is_first_believer_encounter(template_ids: Iterable[object]) -> bool:
    """Return whether the observed templates identify the full Boss encounter."""

    normalized = {
        template_id
        for value in template_ids
        if (template_id := _as_int(value)) > 0
    }
    return any(
        group.issubset(normalized)
        for group in FIRST_BELIEVER_TEMPLATE_GROUPS
    )


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _text(value: object) -> str:
    return str(value or "").strip()


def canonical_history_boss_name(value: object) -> str:
    name = _text(value)
    return _HISTORY_BOSS_CANONICAL_NAMES.get(name.casefold(), name)


def _inferred_history_dungeon_name(boss_names: Iterable[object]) -> str:
    names = {
        canonical_history_boss_name(value).casefold()
        for value in boss_names
        if canonical_history_boss_name(value)
    }
    if any("木桩" in name for name in names):
        return "木桩"
    for boss_name, dungeon_name in _HISTORY_BOSS_DUNGEON_DISPLAY_NAMES.items():
        if boss_name.casefold() in names:
            return dungeon_name
    for dungeon_name, configured_bosses in HISTORY_DUNGEON_BOSSES.items():
        if any(boss.casefold() in names for boss in configured_bosses):
            return _HISTORY_DUNGEON_DISPLAY_NAMES.get(dungeon_name, dungeon_name)
    return ""


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _dict_rows(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _extraordinary_rating(*sources: object) -> int | None:
    """Read only explicitly identified rating fields; never infer a score."""

    for source in sources:
        if not isinstance(source, dict):
            continue
        candidates = [source]
        for nested_key in ("profile", "character_profile", "self_profile"):
            nested = source.get(nested_key)
            if isinstance(nested, dict):
                candidates.append(nested)
        for candidate in candidates:
            for key in _EXTRAORDINARY_RATING_KEYS:
                parsed = _optional_int(candidate.get(key))
                if parsed is not None and parsed >= 0:
                    return parsed
    return None


class DungeonCatalog:
    """Resolve runtime dungeon/stage IDs against extracted client metadata."""

    def __init__(self, path: str | Path | None = None):
        default = (
            Path(__file__).resolve().parent
            / "assets"
            / "bosses"
            / "boss_icon_sources.json"
        )
        self.path = Path(path) if path is not None else default
        self.dungeons: dict[int, dict] = {}
        self.bosses: dict[int, dict] = {}
        self.client_stage_templates: dict[int, dict] = {}
        self.stages: dict[int, dict] = {}
        self.stage_name_index: dict[str, object] = {}
        self.stage_dungeons: dict[int, set[int]] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return
        for raw_id, raw_value in _dict(payload.get("dungeons")).items():
            dungeon_id = _as_int(raw_id)
            if dungeon_id <= 0 or not isinstance(raw_value, dict):
                continue
            value = dict(raw_value)
            self.dungeons[dungeon_id] = value
            for raw_stage_id in value.get("stage_ids", []):
                stage_id = _as_int(raw_stage_id)
                if stage_id > 0:
                    self.stage_dungeons.setdefault(stage_id, set()).add(dungeon_id)
        for raw_id, raw_value in _dict(payload.get("bosses")).items():
            template_id = _as_int(raw_id)
            if template_id > 0 and isinstance(raw_value, dict):
                self.bosses[template_id] = dict(raw_value)
        for raw_id, raw_value in _dict(
            payload.get("client_stage_templates")
        ).items():
            template_id = _as_int(raw_id)
            if template_id > 0 and isinstance(raw_value, dict):
                self.client_stage_templates[template_id] = dict(raw_value)
        for raw_id, raw_value in _dict(payload.get("stages")).items():
            stage_id = _as_int(raw_id)
            if stage_id > 0 and isinstance(raw_value, dict):
                self.stages[stage_id] = dict(raw_value)
        for raw_name, raw_value in _dict(payload.get("stage_name_index")).items():
            name = _text(raw_name).casefold()
            if name and isinstance(raw_value, (dict, list)):
                self.stage_name_index[name] = raw_value

    @staticmethod
    def _explicit_ids(record: dict) -> tuple[int, int]:
        dungeon = _dict(record.get("dungeon"))
        stage = _dict(record.get("stage"))
        dungeon_id = _as_int(
            record.get("dungeon_id", dungeon.get("dungeon_id", dungeon.get("id")))
        )
        stage_id = _as_int(
            record.get(
                "stage_id",
                record.get(
                    "dungeon_stage_id",
                    stage.get("stage_id", stage.get("id")),
                ),
            )
        )
        return dungeon_id, stage_id

    @staticmethod
    def _legacy_ids(record: dict) -> tuple[int, int]:
        capture = _dict(record.get("capture_pipeline_at_archive"))
        dungeon_id = _as_int(capture.get("dungeon_id"))
        stage_id = _as_int(capture.get("dungeon_stage_id"))
        if stage_id <= 0:
            accounting = _dict(record.get("damage_accounting"))
            for validation in _dict_rows(
                accounting.get("stage_summary_validations")
            ):
                stage_id = _as_int(validation.get("stage_id"))
                if stage_id <= 0:
                    match = _STAGE_ID_RE.search(_text(validation.get("summary_id")))
                    stage_id = _as_int(match.group(1)) if match else 0
                if stage_id > 0:
                    break
        return dungeon_id, stage_id

    def resolve(
        self, record: dict, boss_template_ids: Iterable[int] = ()
    ) -> dict[str, object]:
        dungeon = _dict(record.get("dungeon"))
        stage = _dict(record.get("stage"))
        explicit_dungeon_id, explicit_stage_id = self._explicit_ids(record)
        legacy_dungeon_id, legacy_stage_id = self._legacy_ids(record)
        dungeon_id = explicit_dungeon_id or legacy_dungeon_id
        stage_id = explicit_stage_id or legacy_stage_id
        source = "runtime_dungeon_id" if explicit_dungeon_id else ""
        if not source and legacy_dungeon_id:
            source = "legacy_runtime_dungeon_id"

        # The top-level stage ID is frozen when combat begins, so after a
        # reconnect it can briefly retain the previous encounter.  The
        # archive-time protocol context is newer; only accept it as a
        # correction when the observed Boss metadata confirms that stage.
        boss_stage_ids: set[int] | None = None
        for template_id in boss_template_ids:
            metadata = self.boss_metadata(template_id)
            current = {
                _as_int(value)
                for value in metadata.get("stage_ids", [])
                if _as_int(value) > 0
            }
            if current:
                boss_stage_ids = (
                    current
                    if boss_stage_ids is None
                    else boss_stage_ids.intersection(current)
                )
        if (
            stage_id > 0
            and legacy_stage_id > 0
            and legacy_stage_id != stage_id
            and boss_stage_ids
            and stage_id not in boss_stage_ids
            and legacy_stage_id in boss_stage_ids
        ):
            stage_id = legacy_stage_id

        candidates = set(self.stage_dungeons.get(stage_id, set()))
        if dungeon_id <= 0 and candidates:
            boss_candidates: set[int] = set()
            for template_id in boss_template_ids:
                metadata = self.bosses.get(_as_int(template_id), {})
                current = {
                    _as_int(value)
                    for value in metadata.get("dungeon_ids", [])
                    if _as_int(value) > 0
                }
                boss_candidates = (
                    current
                    if not boss_candidates
                    else boss_candidates.intersection(current)
                )
            narrowed = candidates.intersection(boss_candidates)
            if len(narrowed) == 1:
                dungeon_id = next(iter(narrowed))
                source = "stage_and_boss_mapping"
            elif len(candidates) == 1:
                dungeon_id = next(iter(candidates))
                source = "stage_mapping"

        # A Boss template is only a fallback when it identifies exactly one
        # dungeon.  The same Boss legitimately appears in multiple dungeons.
        if dungeon_id <= 0:
            boss_candidates: set[int] | None = None
            for template_id in boss_template_ids:
                metadata = self.bosses.get(_as_int(template_id), {})
                current = {
                    _as_int(value)
                    for value in metadata.get("dungeon_ids", [])
                    if _as_int(value) > 0
                }
                if current:
                    boss_candidates = (
                        current
                        if boss_candidates is None
                        else boss_candidates.intersection(current)
                    )
            if boss_candidates is not None and len(boss_candidates) == 1:
                dungeon_id = next(iter(boss_candidates))
                source = "unique_boss_mapping"

        dungeon_metadata = self.dungeons.get(dungeon_id, {})
        explicit_name = _text(
            record.get("dungeon_name", dungeon.get("name"))
        )
        metadata_name = _text(dungeon_metadata.get("name"))
        dungeon_name = explicit_name or metadata_name
        if dungeon_name == "记忆的传承·城堡":
            dungeon_name = "五月庄园·城堡"
        elif dungeon_name == "记忆的传承" and metadata_name:
            dungeon_name = metadata_name
        stage_name = _text(record.get("stage_name", stage.get("name")))
        if not stage_name and stage_id > 0:
            stage_metadata = self.stages.get(stage_id, {})
            stage_dungeon_ids = {
                _as_int(value)
                for value in stage_metadata.get("dungeon_ids", [])
                if _as_int(value) > 0
            }
            # A stale stage ID can briefly survive a scene transition. Only
            # use its label when it agrees with the resolved dungeon.
            if not dungeon_id or not stage_dungeon_ids or dungeon_id in stage_dungeon_ids:
                stage_name = _text(stage_metadata.get("name"))
        return {
            "dungeon_id": dungeon_id,
            "dungeon_name": dungeon_name,
            "stage_id": stage_id,
            "stage_name": stage_name,
            "source": source or ("record_name" if dungeon_name else "unknown"),
            "ambiguous": bool(dungeon_id <= 0 and len(candidates) > 1),
        }

    def boss_metadata(self, template_id: object) -> dict:
        parsed_id = _as_int(template_id)
        return self.bosses.get(parsed_id) or self.client_stage_templates.get(
            parsed_id, {}
        )

    def boss_icon(
        self,
        template_id: object,
        *,
        stage_id: object = 0,
        dungeon_id: object = 0,
        name: object = "",
    ) -> str:
        icon = _text(self.boss_metadata(template_id).get("icon"))
        if icon:
            return icon

        parsed_stage_id = _as_int(stage_id)
        if parsed_stage_id:
            icon = _text(self.stages.get(parsed_stage_id, {}).get("icon"))
            if icon:
                return icon

        raw_candidates = self.stage_name_index.get(_text(name).casefold())
        candidates = (
            [raw_candidates]
            if isinstance(raw_candidates, dict)
            else _dict_rows(raw_candidates)
        )
        parsed_dungeon_id = _as_int(dungeon_id)
        if parsed_dungeon_id and len(candidates) > 1:
            candidates = [
                candidate
                for candidate in candidates
                if any(
                    parsed_dungeon_id
                    in {
                        _as_int(value)
                        for value in self.stages.get(_as_int(candidate_stage_id), {}).get(
                            "dungeon_ids", []
                        )
                    }
                    for candidate_stage_id in candidate.get("stage_ids", [])
                )
            ]
        icons = {_text(candidate.get("icon")) for candidate in candidates}
        icons.discard("")
        return next(iter(icons)) if len(icons) == 1 else ""


def normalize_battle_result(record: dict) -> str:
    explicit = _text(record.get("result")).casefold()
    aliases = {
        "success": RESULT_DEFEATED,
        "defeated": RESULT_DEFEATED,
        "completed": RESULT_DEFEATED,
        "failed": RESULT_FAILED,
        "wipe": RESULT_FAILED,
        "interrupted": RESULT_INTERRUPTED,
        "unknown": RESULT_UNDETERMINED,
        "undetermined": RESULT_UNDETERMINED,
        "invalid": RESULT_UNDETERMINED,
    }
    if explicit in aliases:
        return aliases[explicit]

    accounting = _dict(record.get("damage_accounting"))
    for validation in _dict_rows(accounting.get("stage_summary_validations")):
        if bool(validation.get("completion_confirmed")):
            return RESULT_DEFEATED

    reason = _text(record.get("archive_reason")).casefold()
    if reason in {
        "target_defeated",
        "boss_defeated",
        "stage_completed",
        "completed",
    }:
        return RESULT_DEFEATED
    if reason in {"party_wipe", "wipe", "failed"}:
        return RESULT_FAILED
    if reason in {
        "party_exit",
        "scene_change",
        "scene_refresh",
        "target_reset",
        "manual_reset",
        "team_counter_reset",
        "boss_replaced",
        "shutdown",
        "capture_stopped",
        "idle",
    }:
        return RESULT_INTERRUPTED
    return RESULT_UNDETERMINED


def _is_boss_record(record: dict) -> bool:
    if _text(record.get("target_filter")).casefold() == "boss":
        return True
    monster = _dict(record.get("monster"))
    return bool(
        _as_int(monster.get("boss_rank")) >= 3
        or _as_int(monster.get("boss_type")) == 3
        or "boss" in _text(monster.get("entity_type")).casefold()
        or "首领" in _text(monster.get("entity_type"))
    )


def _bosses(record: dict, catalog: DungeonCatalog) -> list[dict]:
    candidates = _dict_rows(record.get("targets"))
    monster = _dict(record.get("monster"))
    if monster:
        candidates.insert(0, monster)
    rows: list[dict] = []
    seen: set[tuple[object, ...]] = set()
    explicit_dungeon_id, explicit_stage_id = catalog._explicit_ids(record)
    legacy_dungeon_id, legacy_stage_id = catalog._legacy_ids(record)
    dungeon_id = explicit_dungeon_id or legacy_dungeon_id
    stage_id = explicit_stage_id or legacy_stage_id
    for index, candidate in enumerate(candidates):
        template_id = _as_int(candidate.get("template_id"))
        entity_id = _as_int(candidate.get("entity_id"))
        metadata = catalog.boss_metadata(template_id)
        name = canonical_history_boss_name(
            _text(candidate.get("name")) or _text(metadata.get("name"))
        )
        entity_type = _text(candidate.get("entity_type")).casefold()
        is_primary_boss = bool(index == 0 and monster and _is_boss_record(record))
        is_confirmed_boss = bool(
            is_primary_boss
            or _as_int(candidate.get("boss_rank")) >= 3
            or _as_int(candidate.get("boss_type")) == 3
            or "boss" in entity_type
            or "首领" in entity_type
            or metadata
        )
        if not is_confirmed_boss:
            continue
        if not (entity_id or template_id or name):
            continue
        key: tuple[object, ...] = (
            ("entity", entity_id)
            if entity_id
            else ("template", template_id)
            if template_id
            else ("name", name.casefold())
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "entity_id": entity_id,
                "template_id": template_id,
                "name": name,
                "icon": catalog.boss_icon(
                    template_id,
                    stage_id=stage_id,
                    dungeon_id=dungeon_id,
                    name=name,
                ),
            }
        )
    template_ids = {
        _as_int(row.get("template_id"))
        for row in rows
        if _as_int(row.get("template_id"))
    }
    if is_first_believer_encounter(template_ids):
        canonical_metadata = catalog.boss_metadata(
            FIRST_BELIEVER_CANONICAL_TEMPLATE_ID
        )
        canonical_name = _text(canonical_metadata.get("name")) or "一号信徒"
        canonical = {
            "entity_id": 0,
            "template_id": FIRST_BELIEVER_CANONICAL_TEMPLATE_ID,
            "name": canonical_name,
            "icon": catalog.boss_icon(
                FIRST_BELIEVER_CANONICAL_TEMPLATE_ID,
                stage_id=stage_id,
                dungeon_id=dungeon_id,
                name=canonical_name,
            ),
        }
        merged: list[dict] = []
        inserted = False
        for row in rows:
            if _as_int(row.get("template_id")) in FIRST_BELIEVER_PHASE_TEMPLATE_IDS:
                if not inserted:
                    merged.append(canonical)
                    inserted = True
                continue
            merged.append(row)
        return merged
    return rows


def _self_rows(record: dict) -> tuple[dict | None, dict | None, dict | None, str]:
    participants = _dict_rows(record.get("participants"))
    healers = _dict_rows(record.get("healers"))
    taken_rows = _dict_rows(record.get("damage_taken"))
    participant = next((row for row in participants if row.get("is_self") is True), None)
    healer = next((row for row in healers if row.get("is_self") is True), None)
    taken = next((row for row in taken_rows if row.get("is_self") is True), None)
    source = "participant_flag" if participant is not None else ""

    actor_id = 0
    if participant is not None:
        actor_id = _as_int(participant.get("actor_id"))
    elif healer is not None:
        actor_id = _as_int(healer.get("actor_id"))
        participant = next(
            (row for row in participants if _as_int(row.get("actor_id")) == actor_id),
            None,
        )
        source = "healer_flag"
    elif taken is not None:
        actor_id = _as_int(taken.get("actor_id"))
        participant = next(
            (row for row in participants if _as_int(row.get("actor_id")) == actor_id),
            None,
        )
        source = "taken_flag"

    if actor_id:
        healer = healer or next(
            (row for row in healers if _as_int(row.get("actor_id")) == actor_id), None
        )
        taken = taken or next(
            (row for row in taken_rows if _as_int(row.get("actor_id")) == actor_id),
            None,
        )
    return participant, healer, taken, source


def build_history_summary(
    record: dict,
    catalog: DungeonCatalog | None = None,
    profession_names: dict[int, str] | None = None,
) -> dict[str, object]:
    """Build a null-preserving, list-safe summary from a complete battle."""

    catalog = catalog or DungeonCatalog()
    profession_names = profession_names or {}
    battle_id = _text(record.get("battle_id", record.get("encounter_id")))
    bosses = _bosses(record, catalog)
    boss_template_ids = [row["template_id"] for row in bosses if row["template_id"]]
    dungeon = catalog.resolve(record, boss_template_ids)
    boss_names = [row["name"] for row in bosses if _text(row.get("name"))]
    primary_boss = bosses[0] if bosses else {}
    inferred_dungeon_name = _inferred_history_dungeon_name(boss_names)
    resolved_dungeon_name = _text(dungeon.get("dungeon_name"))
    if not resolved_dungeon_name:
        dungeon["dungeon_name"] = inferred_dungeon_name
        if inferred_dungeon_name:
            dungeon["source"] = "boss_mapping"
    elif (
        resolved_dungeon_name == "记忆的传承"
        and inferred_dungeon_name == "五月庄园·城堡"
    ):
        # Older records used the entrance label for the castle wing. Boss
        # identity separates those records from the newer memory stages.
        dungeon["dungeon_name"] = inferred_dungeon_name
        dungeon["source"] = "boss_mapping_correction"
    if not _text(dungeon.get("stage_name")):
        dungeon["stage_name"] = _text(primary_boss.get("name"))
    participant, healer, taken, identity_source = _self_rows(record)
    identity = participant or healer or taken

    duration = _optional_float(record.get("duration_seconds"))
    if duration is not None:
        duration = max(0.0, duration)
    total_damage = _optional_int(record.get("total_damage"))
    team_dps = _optional_float(record.get("team_dps"))
    if team_dps is None and total_damage is not None and duration:
        team_dps = total_damage / duration

    my_actor_id = _optional_int(identity.get("actor_id")) if identity else None
    my_name = _text(identity.get("name")) if identity else ""
    profession_id = (
        _optional_int(identity.get("profession_id")) if identity else None
    )
    profession_name = (
        _text(identity.get("profession_name")) if identity else ""
    ) or profession_names.get(profession_id or 0, "")
    extraordinary_rating = _extraordinary_rating(identity, record)

    my_damage = _optional_int(participant.get("damage")) if participant else None
    my_dps = _optional_float(participant.get("dps")) if participant else None
    if my_dps is None and my_damage is not None and duration:
        my_dps = my_damage / duration
    my_share = _optional_float(participant.get("share")) if participant else None
    if my_share is None and my_damage is not None and total_damage:
        my_share = my_damage / total_damage
    my_healing = (
        _optional_int(healer.get("effective_healing")) if healer else None
    )
    my_hps = _optional_float(healer.get("hps")) if healer else None
    my_taken = _optional_int(taken.get("taken")) if taken else None
    deaths = _optional_int(participant.get("deaths")) if participant else None

    actor_ids = {
        _as_int(row.get("actor_id"))
        for collection in (
            record.get("participants"),
            record.get("healers"),
            record.get("damage_taken"),
        )
        for row in _dict_rows(collection)
        if _as_int(row.get("actor_id")) > 0
    }
    explicit_team_size = _optional_int(record.get("team_size"))
    team_size = (
        max(explicit_team_size or 0, len(actor_ids))
        if explicit_team_size is not None or actor_ids
        else None
    )

    result = normalize_battle_result(record)
    missing: list[str] = []
    if not dungeon["dungeon_name"]:
        missing.append("dungeon")
    if not boss_names:
        missing.append("boss")
    if duration is None or duration <= 0:
        missing.append("duration")
    if result == RESULT_UNDETERMINED:
        missing.append("result")
    if identity is None or my_actor_id is None:
        missing.append("self_identity")
    if my_damage is None:
        missing.append("self_damage")
    capture_pipeline = _dict(record.get("capture_pipeline_at_archive"))
    team_response_health = _text(
        capture_pipeline.get("team_stats_response_health")
    )
    team_stream_incomplete = bool(
        capture_pipeline.get("team_stats_data_incomplete", False)
    ) or team_response_health in {
            "hook_missing",
            "rearmed_waiting",
            "reinstalling",
            "reinstalled_waiting",
            "recovery_failed",
            "unhealthy",
        }
    if (
        (team_size or 0) > 1
        and team_stream_incomplete
    ):
        missing.append("team_stats_stream")

    has_detail = bool(
        participant
        and isinstance(participant.get("skills"), list)
        and participant.get("skills")
    )
    if not missing and has_detail:
        completeness = COMPLETENESS_COMPLETE
    elif len(missing) <= 2 and boss_names and duration:
        completeness = COMPLETENESS_PARTIAL
    else:
        completeness = COMPLETENESS_LIMITED

    timeline_available = bool(rebuild_team_dps_timeline(record))
    note = _text(record.get("note"))
    search_values = [
        _text(dungeon["dungeon_name"]),
        _text(dungeon["stage_name"]),
        *boss_names,
    ]
    boss_damage = record.get("boss_damage")
    boss_damage = boss_damage if isinstance(boss_damage, dict) else {}
    boss_sources = boss_damage.get("sources", [])
    boss_sources = boss_sources if isinstance(boss_sources, list) else []
    return {
        "battle_id": battle_id,
        "encounter_id": battle_id,
        "started_at_epoch": _optional_float(record.get("started_at_epoch")),
        "ended_at_epoch": _optional_float(record.get("ended_at_epoch")),
        "started_at": _text(record.get("started_at")),
        "ended_at": _text(record.get("ended_at")),
        "duration_seconds": duration,
        "dungeon_id": dungeon["dungeon_id"],
        "dungeon_name": dungeon["dungeon_name"],
        "dungeon_source": dungeon["source"],
        "dungeon_ambiguous": dungeon["ambiguous"],
        "stage_id": dungeon["stage_id"],
        "stage_name": dungeon["stage_name"],
        "boss_template_id": _as_int(primary_boss.get("template_id")),
        "boss_name": _text(primary_boss.get("name")),
        "boss_names": boss_names,
        "boss_names_text": "\n".join(boss_names).casefold(),
        "boss_count": len(bosses),
        "boss_icon": _text(primary_boss.get("icon")),
        "difficulty": _text(record.get("difficulty")),
        "result": result,
        "team_size": team_size,
        "total_damage": total_damage,
        "team_dps": team_dps,
        "team_hps": _optional_float(record.get("team_hps")),
        "team_taken": _optional_int(record.get("team_taken")),
        "boss_observed_damage": _optional_int(
            boss_damage.get("observed_damage")
        ),
        "boss_team_taken": _optional_int(boss_damage.get("team_taken")),
        "boss_classification_ratio": _optional_float(
            boss_damage.get("classification_ratio")
        ),
        "boss_max_hit": _optional_int(boss_damage.get("max_hit")),
        "boss_damage_coverage": _text(boss_damage.get("coverage")),
        "boss_damage_unavailable_reason": _text(
            boss_damage.get("unavailable_reason")
        ),
        "boss_source_count": len(
            [source for source in boss_sources if isinstance(source, dict)]
        ),
        "my_actor_id": my_actor_id,
        "my_name": my_name,
        "profession_id": profession_id,
        "profession_name": profession_name,
        "extraordinary_rating": extraordinary_rating,
        "my_damage": my_damage,
        "my_dps": my_dps,
        "my_share": my_share,
        "my_effective_healing": my_healing,
        "my_hps": my_hps,
        "my_taken": my_taken,
        "deaths": deaths,
        "favorite": bool(record.get("favorite", False)),
        "note": note,
        "is_owner": bool(record.get("is_owner", True)),
        "completeness": completeness,
        "completeness_rank": _COMPLETENESS_RANK[completeness],
        "missing_fields": missing,
        "timeline_available": timeline_available,
        "is_boss": _is_boss_record(record),
        "search_text": " ".join(value for value in search_values if value).casefold(),
    }


def _rebuild_event_dps_timeline(
    record: dict,
    actor_ids: set[int] | None,
    window_seconds: int,
) -> list[dict]:
    event_log = _dict(record.get("event_log"))
    rows = event_log.get("rows")
    if not isinstance(rows, list) or not rows:
        return []

    buckets: dict[int, int] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        second = max(0, _as_int(row[0]) // 1000)
        actor_id = _as_int(row[1])
        if actor_id <= 0 or (actor_ids is not None and actor_id not in actor_ids):
            continue
        damage = max(0, _as_int(row[4]))
        if damage:
            buckets[second] = buckets.get(second, 0) + damage
    if not buckets:
        return []

    duration = max(
        max(buckets),
        int(math.ceil(_as_float(record.get("duration_seconds")))),
    )
    values: list[dict] = []
    rolling = 0
    per_second = [buckets.get(second, 0) for second in range(duration + 1)]
    for second, damage in enumerate(per_second):
        rolling += damage
        expired = second - max(1, int(window_seconds))
        if expired >= 0:
            rolling -= per_second[expired]
        divisor = max(1, min(max(1, int(window_seconds)), second + 1))
        values.append(
            {
                "time_seconds": second,
                "dps": rolling / divisor,
            }
        )
    return values


def rebuild_dps_timeline(record: dict, window_seconds: int = 10) -> list[dict]:
    """Rebuild the self-player sliding DPS cache from a compact event log."""

    participant, healer, taken, _source = _self_rows(record)
    identity = participant or healer or taken
    actor_id = _as_int(identity.get("actor_id")) if identity else 0
    if actor_id <= 0:
        return []
    return _rebuild_event_dps_timeline(
        record,
        {actor_id},
        window_seconds,
    )


def rebuild_participant_dps_timelines(
    record: dict, window_seconds: int = 10
) -> dict[int, list[dict]]:
    """Rebuild validated per-player sliding DPS from cumulative samples.

    The returned mapping is intentionally empty for older records.  Callers
    may still render the separately validated team timeline and the local
    player's event-based timeline without pretending that teammate events
    were captured.
    """

    sample_log = _dict(record.get("participant_damage_samples"))
    if sample_log.get("coverage") != "live_participant_cumulative":
        return {}
    rows = sample_log.get("rows")
    participants = record.get("participants")
    if not isinstance(rows, list) or not rows or not isinstance(participants, list):
        return {}

    duration = max(
        0,
        int(
            _as_float(
                record.get("dps_duration_seconds", record.get("duration_seconds", 0.0))
            )
        ),
    )
    expected_totals: dict[int, int] = {}
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        actor_id = _as_int(participant.get("actor_id"))
        damage = max(0, _as_int(participant.get("damage")))
        if actor_id > 0 and damage > 0:
            expected_totals[actor_id] = damage
    if duration <= 0 or not expected_totals:
        return {}

    samples_by_actor: dict[int, dict[int, int]] = {
        actor_id: {} for actor_id in expected_totals
    }
    observed_seconds: set[int] = set()
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        second = max(0, min(duration, _as_int(row[0])))
        actor_id = _as_int(row[1])
        total = max(0, _as_int(row[2]))
        if actor_id not in expected_totals:
            continue
        current = samples_by_actor[actor_id].get(second)
        samples_by_actor[actor_id][second] = (
            total if current is None else max(current, total)
        )
        observed_seconds.add(second)
    if duration >= 5 and len(observed_seconds) / (duration + 1) < 0.65:
        return {}

    window = max(1, int(window_seconds))
    timelines: dict[int, list[dict]] = {}
    for actor_id, expected_total in expected_totals.items():
        samples = samples_by_actor.get(actor_id, {})
        if not samples:
            return {}
        previous_total = 0
        for second in sorted(samples):
            total = samples[second]
            if total < previous_total or total > expected_total:
                return {}
            previous_total = total
        tolerance = max(1, int(expected_total * 0.001))
        if abs(previous_total - expected_total) > tolerance:
            return {}

        per_second: list[int] = []
        last_total = 0
        current_total = 0
        for second in range(duration + 1):
            if second in samples:
                current_total = samples[second]
            if current_total < last_total:
                return {}
            per_second.append(current_total - last_total)
            last_total = current_total

        values: list[dict] = []
        rolling = 0
        for second, damage in enumerate(per_second):
            rolling += damage
            expired = second - window
            if expired >= 0:
                rolling -= per_second[expired]
            divisor = max(1, min(window, second + 1))
            values.append(
                {
                    "time_seconds": second,
                    "actor_id": actor_id,
                    "dps": rolling / divisor,
                    "source": "live_participant_cumulative",
                }
            )
        timelines[actor_id] = values
    return timelines


def _rebuild_cumulative_team_dps_timeline(
    record: dict, window_seconds: int
) -> list[dict]:
    sample_log = _dict(record.get("team_damage_samples"))
    if sample_log.get("coverage") != "live_team_cumulative":
        return []
    rows = sample_log.get("rows")
    if not isinstance(rows, list) or not rows:
        return []

    duration = max(
        0,
        int(
            _as_float(
                record.get("dps_duration_seconds", record.get("duration_seconds", 0.0))
            )
        ),
    )
    expected_total = max(0, _as_int(record.get("total_damage")))
    if duration <= 0 or expected_total <= 0:
        return []

    samples: dict[int, int] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        second = max(0, min(duration, _as_int(row[0])))
        total = max(0, _as_int(row[1]))
        samples[second] = total
    if not samples:
        return []

    previous_total = 0
    for second in sorted(samples):
        total = samples[second]
        if total < previous_total:
            return []
        previous_total = total
    tolerance = max(1, int(expected_total * 0.001))
    if abs(previous_total - expected_total) > tolerance:
        return []
    if duration >= 5 and len(samples) / (duration + 1) < 0.65:
        return []

    window = max(1, int(window_seconds))
    per_second: list[int] = []
    last_total = 0
    current_total = 0
    for second in range(duration + 1):
        if second in samples:
            current_total = samples[second]
        if current_total < last_total:
            return []
        per_second.append(current_total - last_total)
        last_total = current_total

    values: list[dict] = []
    rolling = 0
    for second, damage in enumerate(per_second):
        rolling += damage
        expired = second - window
        if expired >= 0:
            rolling -= per_second[expired]
        divisor = max(1, min(window, second + 1))
        values.append(
            {
                "time_seconds": second,
                "dps": rolling / divisor,
                "team_dps": rolling / divisor,
                "source": "live_team_cumulative",
            }
        )
    return values


def rebuild_team_dps_timeline(record: dict, window_seconds: int = 10) -> list[dict]:
    """Build team sliding DPS only from a source that covers the final total."""

    values = _rebuild_cumulative_team_dps_timeline(record, window_seconds)
    if values:
        return values

    event_log = _dict(record.get("event_log"))
    rows = event_log.get("rows")
    if not isinstance(rows, list) or not rows:
        return []
    event_total = sum(
        max(0, _as_int(row[4]))
        for row in rows
        if isinstance(row, (list, tuple))
        and len(row) >= 5
        and _as_int(row[1]) > 0
    )
    expected_total = max(0, _as_int(record.get("total_damage")))
    if expected_total <= 0:
        return []
    tolerance = max(1, int(expected_total * 0.005))
    if abs(event_total - expected_total) > tolerance:
        return []

    values = _rebuild_event_dps_timeline(record, None, window_seconds)
    scale = expected_total / event_total if event_total else 1.0
    for value in values:
        value["dps"] *= scale
        value["team_dps"] = value["dps"]
        value["source"] = "complete_damage_events"
    return values


class HistoryIndex:
    """Rebuildable SQLite index over one-file-per-battle JSON archives."""

    _SORTS = {
        "time_desc": "ended_at_epoch DESC, battle_id DESC",
        "time_asc": "ended_at_epoch ASC, battle_id ASC",
        "dps_desc": "my_dps IS NULL, my_dps DESC, ended_at_epoch DESC",
        "dps_asc": "my_dps IS NULL, my_dps ASC, ended_at_epoch DESC",
        "damage_desc": "my_damage IS NULL, my_damage DESC, ended_at_epoch DESC",
        "damage_asc": "my_damage IS NULL, my_damage ASC, ended_at_epoch DESC",
        "boss_damage_desc": "boss_damage_coverage = 'unavailable', boss_observed_damage IS NULL, boss_observed_damage DESC, ended_at_epoch DESC",
        "boss_damage_asc": "boss_damage_coverage = 'unavailable', boss_observed_damage IS NULL, boss_observed_damage ASC, ended_at_epoch DESC",
        "duration_desc": "duration_seconds IS NULL, duration_seconds DESC, ended_at_epoch DESC",
        "duration_asc": "duration_seconds IS NULL, duration_seconds ASC, ended_at_epoch DESC",
    }

    def __init__(
        self,
        directory: str | Path,
        *,
        catalog_path: str | Path | None = None,
        profession_path: str | Path | None = None,
    ):
        self.directory = Path(directory)
        self.path = self.directory / HISTORY_INDEX_FILENAME
        self.catalog = DungeonCatalog(catalog_path)
        default_professions = Path(__file__).resolve().parent / "skill_metadata.json"
        self.profession_path = (
            Path(profession_path) if profession_path is not None else default_professions
        )
        self.profession_names = self._load_profession_names()

    def _load_profession_names(self) -> dict[int, str]:
        try:
            payload = json.loads(self.profession_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        result: dict[int, str] = {}
        for raw_id, metadata in _dict(payload.get("professions")).items():
            profession_id = _as_int(raw_id)
            name = _text(metadata.get("name")) if isinstance(metadata, dict) else ""
            if profession_id > 0 and name:
                result[profession_id] = name
        return result

    @staticmethod
    def _dependency_signature(path: Path) -> str:
        try:
            stat = path.stat()
        except OSError:
            return f"{path.resolve()}|missing"
        return f"{path.resolve()}|{int(stat.st_mtime_ns)}|{int(stat.st_size)}"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=3.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS history_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS battles (
                battle_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL UNIQUE,
                source_mtime_ns INTEGER NOT NULL,
                source_size INTEGER NOT NULL,
                started_at_epoch REAL,
                ended_at_epoch REAL,
                duration_seconds REAL,
                dungeon_id INTEGER,
                dungeon_name TEXT NOT NULL,
                stage_id INTEGER,
                boss_template_id INTEGER,
                boss_name TEXT NOT NULL,
                boss_names_text TEXT NOT NULL,
                result TEXT NOT NULL,
                team_size INTEGER,
                total_damage INTEGER,
                team_dps REAL,
                boss_observed_damage INTEGER,
                boss_team_taken INTEGER,
                boss_classification_ratio REAL,
                boss_max_hit INTEGER,
                boss_damage_coverage TEXT NOT NULL,
                boss_source_count INTEGER NOT NULL,
                my_actor_id INTEGER,
                my_name TEXT NOT NULL,
                profession_id INTEGER,
                profession_name TEXT NOT NULL,
                my_damage INTEGER,
                my_dps REAL,
                my_share REAL,
                my_hps REAL,
                my_taken INTEGER,
                deaths INTEGER,
                favorite INTEGER NOT NULL,
                note TEXT NOT NULL,
                is_owner INTEGER NOT NULL,
                completeness TEXT NOT NULL,
                completeness_rank INTEGER NOT NULL,
                boss_count INTEGER NOT NULL,
                is_boss INTEGER NOT NULL,
                search_text TEXT NOT NULL,
                summary_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_battles_end ON battles(ended_at_epoch DESC);
            CREATE INDEX IF NOT EXISTS idx_battles_filters ON battles(result, profession_id, completeness_rank);
            CREATE INDEX IF NOT EXISTS idx_battles_dungeon ON battles(dungeon_name, boss_name);
            """
        )

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        self._create_schema(connection)
        row = connection.execute(
            "SELECT value FROM history_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None and _as_int(row["value"]) != HISTORY_INDEX_SCHEMA_VERSION:
            connection.executescript(
                "DROP TABLE IF EXISTS battles; DELETE FROM history_meta;"
            )
            self._create_schema(connection)
        connection.execute(
            "INSERT OR REPLACE INTO history_meta(key, value) VALUES('schema_version', ?)",
            (str(HISTORY_INDEX_SCHEMA_VERSION),),
        )
        dependency_paths = {
            "catalog_signature": self.catalog.path,
            "profession_signature": self.profession_path,
        }
        stored = {
            str(item["key"]): str(item["value"])
            for item in connection.execute(
                "SELECT key, value FROM history_meta WHERE key IN (?, ?)",
                tuple(dependency_paths),
            )
        }
        current = {
            key: self._dependency_signature(path)
            for key, path in dependency_paths.items()
        }
        if any(stored.get(key) != value for key, value in current.items()):
            # Dungeon names, portrait paths and profession labels are cached in
            # each summary. Rebuild when their extracted catalogs change while
            # leaving the JSON battle archives untouched.
            connection.execute("DELETE FROM battles")
            self.catalog = DungeonCatalog(self.catalog.path)
            self.profession_names = self._load_profession_names()
        connection.executemany(
            "INSERT OR REPLACE INTO history_meta(key, value) VALUES(?, ?)",
            list(current.items()),
        )

    def _upsert(
        self,
        connection: sqlite3.Connection,
        path: Path,
        record: dict,
        stat,
    ) -> dict[str, object]:
        summary = build_history_summary(
            record, self.catalog, self.profession_names
        )
        summary["source_path"] = str(path)
        values = (
            summary["battle_id"],
            str(path),
            int(stat.st_mtime_ns),
            int(stat.st_size),
            summary["started_at_epoch"],
            summary["ended_at_epoch"],
            summary["duration_seconds"],
            summary["dungeon_id"] or None,
            summary["dungeon_name"],
            summary["stage_id"] or None,
            summary["boss_template_id"] or None,
            summary["boss_name"],
            summary["boss_names_text"],
            summary["result"],
            summary["team_size"],
            summary["total_damage"],
            summary["team_dps"],
            summary["boss_observed_damage"],
            summary["boss_team_taken"],
            summary["boss_classification_ratio"],
            summary["boss_max_hit"],
            summary["boss_damage_coverage"],
            summary["boss_source_count"],
            summary["my_actor_id"],
            summary["my_name"],
            summary["profession_id"],
            summary["profession_name"],
            summary["my_damage"],
            summary["my_dps"],
            summary["my_share"],
            summary["my_hps"],
            summary["my_taken"],
            summary["deaths"],
            int(bool(summary["favorite"])),
            summary["note"],
            int(bool(summary["is_owner"])),
            summary["completeness"],
            summary["completeness_rank"],
            summary["boss_count"],
            int(bool(summary["is_boss"])),
            summary["search_text"],
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
        )
        connection.execute(
            f"""
            INSERT OR REPLACE INTO battles(
                battle_id, source_path, source_mtime_ns, source_size,
                started_at_epoch, ended_at_epoch, duration_seconds,
                dungeon_id, dungeon_name, stage_id, boss_template_id,
                boss_name, boss_names_text, result, team_size, total_damage, team_dps,
                boss_observed_damage, boss_team_taken,
                boss_classification_ratio, boss_max_hit,
                boss_damage_coverage, boss_source_count,
                my_actor_id, my_name, profession_id, profession_name,
                my_damage, my_dps, my_share, my_hps, my_taken,
                deaths, favorite, note,
                is_owner, completeness, completeness_rank, boss_count,
                is_boss, search_text, summary_json
            ) VALUES ({", ".join("?" for _value in values)})
            """,
            values,
        )
        return summary

    def upsert_path(
        self,
        path: str | Path,
        valid_record: Callable[[object], bool] | None = None,
    ) -> dict[str, object] | None:
        source = Path(path)
        try:
            stat = source.stat()
            record = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(record, dict) or (
            valid_record is not None and not valid_record(record)
        ):
            return None
        with self._connect() as connection:
            return self._upsert(connection, source, record, stat)

    def sync(
        self, valid_record: Callable[[object], bool] | None = None
    ) -> dict[str, int]:
        if not self.directory.is_dir():
            return {"indexed": 0, "updated": 0, "removed": 0}
        paths = list(self.directory.glob("*.json"))
        updated = 0
        removed = 0
        with self._connect() as connection:
            existing = {
                row["source_path"]: (row["source_mtime_ns"], row["source_size"])
                for row in connection.execute(
                    "SELECT source_path, source_mtime_ns, source_size FROM battles"
                )
            }
            seen: set[str] = set()
            for path in paths:
                source_path = str(path)
                seen.add(source_path)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                signature = (int(stat.st_mtime_ns), int(stat.st_size))
                if existing.get(source_path) == signature:
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, TypeError, ValueError):
                    connection.execute(
                        "DELETE FROM battles WHERE source_path = ?", (source_path,)
                    )
                    continue
                if not isinstance(record, dict) or (
                    valid_record is not None and not valid_record(record)
                ):
                    connection.execute(
                        "DELETE FROM battles WHERE source_path = ?", (source_path,)
                    )
                    continue
                self._upsert(connection, path, record, stat)
                updated += 1
            stale = set(existing).difference(seen)
            if stale:
                connection.executemany(
                    "DELETE FROM battles WHERE source_path = ?",
                    [(path,) for path in stale],
                )
                removed = len(stale)
            indexed = _as_int(
                connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0]
            )
        return {"indexed": indexed, "updated": updated, "removed": removed}

    def remove_ids(self, battle_ids: Iterable[object]) -> None:
        ids = [str(value) for value in battle_ids if _text(value)]
        if not ids or not self.path.is_file():
            return
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM battles WHERE battle_id = ?",
                [(value,) for value in ids],
            )

    @staticmethod
    def _where(filters: dict | None) -> tuple[str, list[object]]:
        filters = filters or {}
        clauses = ["1 = 1"]
        params: list[object] = []

        def minimum(column: str, key: str) -> None:
            value = _optional_float(filters.get(key))
            if value is not None:
                clauses.append(f"{column} >= ?")
                params.append(value)

        def maximum(column: str, key: str) -> None:
            value = _optional_float(filters.get(key))
            if value is not None:
                clauses.append(f"{column} <= ?")
                params.append(value)

        start_epoch = _optional_float(filters.get("start_epoch"))
        end_epoch = _optional_float(filters.get("end_epoch"))
        if start_epoch is not None:
            clauses.append("ended_at_epoch >= ?")
            params.append(start_epoch)
        if end_epoch is not None:
            clauses.append("ended_at_epoch < ?")
            params.append(end_epoch)

        query = _text(filters.get("query")).casefold()
        if query:
            clauses.append("search_text LIKE ? ESCAPE '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        dungeon = _text(filters.get("dungeon"))
        if dungeon:
            raw_aliases = filters.get("dungeon_aliases")
            aliases = (
                [_text(value) for value in raw_aliases if _text(value)]
                if isinstance(raw_aliases, (list, tuple, set, frozenset))
                else [dungeon]
            )
            raw_bosses = filters.get("dungeon_bosses")
            dungeon_bosses = (
                [_text(value).casefold() for value in raw_bosses if _text(value)]
                if isinstance(raw_bosses, (list, tuple, set, frozenset))
                else []
            )
            dungeon_clauses: list[str] = []
            for alias in dict.fromkeys(aliases):
                dungeon_clauses.append("dungeon_name = ? COLLATE NOCASE")
                params.append(alias)
            for dungeon_boss in dict.fromkeys(dungeon_bosses):
                escaped = (
                    dungeon_boss.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                dungeon_clauses.append("boss_names_text LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped}%")
            if dungeon == "木桩":
                dungeon_clauses.append("boss_names_text LIKE ?")
                params.append("%木桩%")
            if dungeon_clauses:
                clauses.append(f"({' OR '.join(dungeon_clauses)})")
        boss = _text(filters.get("boss"))
        if boss:
            raw_aliases = filters.get("boss_aliases")
            aliases = (
                [_text(value).casefold() for value in raw_aliases if _text(value)]
                if isinstance(raw_aliases, (list, tuple, set, frozenset))
                else [boss.casefold()]
            )
            if not aliases:
                aliases = [boss.casefold()]
            boss_clauses: list[str] = []
            for alias in dict.fromkeys(aliases):
                escaped = (
                    alias.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                boss_clauses.append("boss_names_text LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped}%")
            clauses.append(f"({' OR '.join(boss_clauses)})")
        result = _text(filters.get("result"))
        if result:
            clauses.append("result = ?")
            params.append(result)
        raw_results = filters.get("results")
        results = (
            list(
                dict.fromkeys(
                    _text(value)
                    for value in raw_results
                    if _text(value)
                )
            )
            if isinstance(raw_results, (list, tuple, set, frozenset))
            else []
        )
        if results:
            placeholders = ", ".join("?" for _value in results)
            clauses.append(f"result IN ({placeholders})")
            params.extend(results)
        profession_id = _optional_int(filters.get("profession_id"))
        if profession_id:
            clauses.append("profession_id = ?")
            params.append(profession_id)
        if filters.get("only_owner"):
            clauses.append("is_owner = 1")
        if filters.get("boss_only"):
            clauses.append("is_boss = 1")
        minimum("duration_seconds", "duration_min")
        maximum("duration_seconds", "duration_max")
        minimum("my_dps", "dps_min")
        maximum("my_dps", "dps_max")
        minimum("my_damage", "damage_min")
        maximum("my_damage", "damage_max")
        team_group = _text(filters.get("team_group"))
        if team_group == "solo":
            clauses.append("team_size = 1")
        elif team_group == "party":
            clauses.append("team_size BETWEEN 2 AND 6")
        elif team_group == "raid":
            clauses.append("team_size >= 7")
        target_scope = _text(filters.get("target_scope"))
        if target_scope == "single":
            clauses.append("boss_count <= 1")
        elif target_scope == "multiple":
            clauses.append("boss_count >= 2")
        if filters.get("has_death") is True:
            clauses.append("deaths > 0")
        elif filters.get("has_death") is False:
            clauses.append("deaths = 0")
        minimum_rank = _optional_int(filters.get("completeness_rank"))
        if minimum_rank is not None:
            clauses.append("completeness_rank >= ?")
            params.append(minimum_rank)
        return " AND ".join(clauses), params

    def query(
        self,
        filters: dict | None = None,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "time_desc",
    ) -> dict[str, object]:
        page_size = min(100, max(1, _as_int(page_size, 20)))
        page = max(1, _as_int(page, 1))
        where, params = self._where(filters)
        order = self._SORTS.get(sort, self._SORTS["time_desc"])
        with self._connect() as connection:
            total = _as_int(
                connection.execute(
                    f"SELECT COUNT(*) FROM battles WHERE {where}", params
                ).fetchone()[0]
            )
            page_count = max(1, int(math.ceil(total / page_size)))
            page = min(page, page_count)
            rows = connection.execute(
                f"SELECT summary_json FROM battles WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        records: list[dict] = []
        for row in rows:
            try:
                summary = json.loads(row["summary_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(summary, dict):
                records.append(summary)
        return {
            "records": records,
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": page_count,
        }

    def overview(self, filters: dict | None = None) -> dict[str, object]:
        where, params = self._where(filters)
        now = time.time()
        local = time.localtime(now)
        today_start = time.mktime(
            (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
        )
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS battle_count,
                    SUM(CASE WHEN ended_at_epoch >= ? THEN 1 ELSE 0 END) AS today_count,
                    AVG(CASE
                        WHEN result != ? AND duration_seconds > 0 AND my_damage > 0
                        THEN my_dps END
                    ) AS average_dps,
                    AVG(CASE
                        WHEN result != ? AND duration_seconds > 0 AND my_hps > 0
                        THEN my_hps END
                    ) AS average_hps,
                    AVG(CASE
                        WHEN result != ? AND my_taken IS NOT NULL
                        THEN my_taken END
                    ) AS average_taken,
                    AVG(CASE
                        WHEN boss_damage_coverage IN ('complete', 'observed_partial', 'conflict')
                             AND boss_classification_ratio IS NOT NULL
                        THEN boss_classification_ratio END
                    ) AS average_boss_classification_ratio,
                    AVG(CASE
                        WHEN duration_seconds > 0 THEN duration_seconds END
                    ) AS average_duration,
                    SUM(CASE
                        WHEN duration_seconds > 0 THEN 1 ELSE 0 END
                    ) AS timed_count,
                    MAX(my_damage) AS highest_damage,
                    MAX(CASE
                        WHEN boss_damage_coverage IN ('complete', 'observed_partial', 'conflict')
                        THEN boss_observed_damage END
                    ) AS highest_boss_observed_damage,
                    SUM(CASE WHEN result = ? THEN 1 ELSE 0 END) AS defeated_count,
                    SUM(CASE WHEN result = ? THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN result IN (?, ?) THEN 1 ELSE 0 END) AS interrupted_count
                FROM battles WHERE {where}
                """,
                [
                    today_start,
                    RESULT_UNDETERMINED,
                    RESULT_UNDETERMINED,
                    RESULT_UNDETERMINED,
                    RESULT_DEFEATED,
                    RESULT_FAILED,
                    RESULT_INTERRUPTED,
                    RESULT_UNDETERMINED,
                    *params,
                ],
            ).fetchone()
            highest = connection.execute(
                f"SELECT battle_id, dungeon_name, boss_name, summary_json FROM battles WHERE {where} AND my_damage IS NOT NULL ORDER BY my_damage DESC LIMIT 1",
                params,
            ).fetchone()
            highest_boss_damage = connection.execute(
                f"""
                SELECT battle_id, dungeon_name, boss_name, summary_json
                FROM battles
                WHERE {where}
                  AND boss_damage_coverage IN ('complete', 'observed_partial', 'conflict')
                  AND boss_observed_damage IS NOT NULL
                ORDER BY boss_observed_damage DESC, ended_at_epoch DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        highest_boss_name = _text(highest["boss_name"]) if highest else ""
        if highest:
            try:
                highest_summary = json.loads(highest["summary_json"])
            except (TypeError, ValueError):
                highest_summary = {}
            if isinstance(highest_summary, dict):
                highest_boss_name = (
                    _text(highest_summary.get("stage_name"))
                    or _text(highest_summary.get("boss_name"))
                    or highest_boss_name
                )
        highest_boss_damage_name = (
            _text(highest_boss_damage["boss_name"])
            if highest_boss_damage
            else ""
        )
        if highest_boss_damage:
            try:
                highest_boss_damage_summary = json.loads(
                    highest_boss_damage["summary_json"]
                )
            except (TypeError, ValueError):
                highest_boss_damage_summary = {}
            if isinstance(highest_boss_damage_summary, dict):
                highest_boss_damage_name = (
                    _text(highest_boss_damage_summary.get("stage_name"))
                    or _text(highest_boss_damage_summary.get("boss_name"))
                    or highest_boss_damage_name
                )
        defeated = _as_int(row["defeated_count"])
        failed = _as_int(row["failed_count"])
        interrupted = _as_int(row["interrupted_count"])
        judged = defeated + failed + interrupted
        return {
            "battle_count": _as_int(row["battle_count"]),
            "today_count": _as_int(row["today_count"]),
            "average_dps": _optional_float(row["average_dps"]),
            "average_hps": _optional_float(row["average_hps"]),
            "average_taken": _optional_float(row["average_taken"]),
            "average_boss_classification_ratio": _optional_float(
                row["average_boss_classification_ratio"]
            ),
            "average_duration": _optional_float(row["average_duration"]),
            "timed_count": _as_int(row["timed_count"]),
            "highest_damage": _optional_int(row["highest_damage"]),
            "highest_boss_observed_damage": _optional_int(
                row["highest_boss_observed_damage"]
            ),
            "highest_boss_battle_id": (
                _text(highest_boss_damage["battle_id"])
                if highest_boss_damage
                else ""
            ),
            "highest_boss_dungeon_name": (
                _text(highest_boss_damage["dungeon_name"])
                if highest_boss_damage
                else ""
            ),
            "highest_boss_damage_name": highest_boss_damage_name,
            "highest_battle_id": _text(highest["battle_id"]) if highest else "",
            "highest_dungeon_name": (
                _text(highest["dungeon_name"]) if highest else ""
            ),
            "highest_boss_name": highest_boss_name,
            "highest_source": (
                " / ".join(
                    value
                    for value in (
                        _text(highest["dungeon_name"]),
                        highest_boss_name,
                    )
                    if value
                )
                if highest
                else ""
            ),
            "defeat_rate": defeated / judged if judged else None,
            "defeated_count": defeated,
            "failed_count": failed,
            "interrupted_count": interrupted,
            "judged_count": judged,
        }

    def options(self) -> dict[str, list[dict[str, object]] | list[str]]:
        with self._connect() as connection:
            dungeons = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT dungeon_name FROM battles WHERE dungeon_name != '' ORDER BY dungeon_name"
                )
            ]
            bosses = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT boss_name FROM battles WHERE boss_name != '' ORDER BY boss_name"
                )
            ]
            professions = [
                {
                    "id": _as_int(row[0]),
                    "name": _text(row[1]) or str(_as_int(row[0])),
                }
                for row in connection.execute(
                    "SELECT DISTINCT profession_id, profession_name FROM battles WHERE profession_id IS NOT NULL ORDER BY profession_name, profession_id"
                )
            ]
        return {
            "dungeons": dungeons,
            "bosses": bosses,
            "professions": professions,
        }
