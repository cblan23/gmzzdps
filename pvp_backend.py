"""Private PVP records and alliance administration for the monitor server."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from profile_upload import ProfileUploadError, character_hash


MATCH_ID_PATTERN = re.compile(r"^pvp_[0-9a-f]{32}$")
EQUIPMENT_SNAPSHOT_ID_PATTERN = re.compile(r"^pvp_eq_[0-9a-f]{32}$")
EQUIPMENT_SNAPSHOT_HISTORY_LIMIT = 2
CLUB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,48}$")
PVP_MAPS = {
    5200020, 5200021, 5200086, 5200110, 5200111, 5200131,
    5200148, 5200167, 5200223, 5200229, 5200253, 5200280, 5203003,
    5208002, 5208003, 5208004, 5208012, 5208017, 5208018,
}
HUNTER_CITY_MAPS = (5200131, 5200167)
HUNTER_DRAGON_TEMPLATE_IDS = frozenset({
    7100625, 7100628, 7115040, 7115041, 7115042,
})
RESULTS = {"胜利", "失败", "未知"}
ROLES = {"盟主", "副盟主", "精英成员", "核心成员", "普通成员", "预备成员"}
PVP_DATA_SCOPES = {
    "identity_only",
    "observed_partial",
    "self_exact",
    "pair_exact",
    "authoritative",
}
PVP_ROSTER_SCOPES = {"partial", "verified"}


class PvpBackendError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def initialize_pvp_schema(connection: sqlite3.Connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS pvp_matches (
            match_id TEXT PRIMARY KEY, uploader_card_hash TEXT NOT NULL,
            player_hash TEXT NOT NULL, player_name TEXT NOT NULL,
            profession_id INTEGER NOT NULL DEFAULT 0,
            map_id INTEGER NOT NULL, mode_id INTEGER NOT NULL DEFAULT 0,
            mode_name TEXT NOT NULL, map_name TEXT NOT NULL,
            started_at REAL NOT NULL, ended_at REAL NOT NULL,
            result TEXT NOT NULL, kills INTEGER NOT NULL, assists INTEGER,
            deaths INTEGER NOT NULL, damage_done INTEGER NOT NULL DEFAULT 0,
            damage_taken INTEGER, extraordinary_rating INTEGER,
            series_id TEXT NOT NULL DEFAULT '', event_id TEXT NOT NULL DEFAULT '',
            war_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
            created_at REAL NOT NULL, last_uploaded_at REAL NOT NULL,
            upload_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_pvp_matches_owner
            ON pvp_matches(uploader_card_hash, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pvp_matches_player
            ON pvp_matches(player_hash, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pvp_matches_map_time
            ON pvp_matches(map_id, started_at DESC);
        CREATE TABLE IF NOT EXISTS pvp_opponents (
            match_id TEXT NOT NULL REFERENCES pvp_matches(match_id) ON DELETE CASCADE,
            opponent_id TEXT NOT NULL, character_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '未知玩家', damage_to INTEGER NOT NULL DEFAULT 0,
            damage_from INTEGER, kills_on INTEGER NOT NULL DEFAULT 0,
            deaths_to INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(match_id, opponent_id)
        );
        CREATE INDEX IF NOT EXISTS idx_pvp_opponents_opponent
            ON pvp_opponents(opponent_id);
        CREATE TABLE IF NOT EXISTS pvp_equipment_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            uploader_card_hash TEXT NOT NULL,
            owner_character_hash TEXT NOT NULL,
            target_character_hash TEXT NOT NULL,
            target_name TEXT NOT NULL DEFAULT '',
            profession_id INTEGER NOT NULL DEFAULT 0,
            extraordinary_rating INTEGER,
            captured_at_ns INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_uploaded_at REAL NOT NULL,
            upload_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_pvp_equipment_snapshot_owner
            ON pvp_equipment_snapshots(
                uploader_card_hash, target_character_hash, captured_at_ns DESC
            );
        CREATE TABLE IF NOT EXISTS pvp_alliances (
            club_id TEXT PRIMARY KEY, club_name TEXT NOT NULL, server_name TEXT NOT NULL DEFAULT '',
            certification_status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pvp_alliance_admins (
            club_id TEXT NOT NULL REFERENCES pvp_alliances(club_id) ON DELETE CASCADE,
            card_hash TEXT NOT NULL, admin_role TEXT NOT NULL DEFAULT '盟主',
            active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
            PRIMARY KEY(club_id, card_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_pvp_alliance_admin_card
            ON pvp_alliance_admins(card_hash, active);
        CREATE TABLE IF NOT EXISTS pvp_alliance_members (
            club_id TEXT NOT NULL REFERENCES pvp_alliances(club_id) ON DELETE CASCADE,
            character_hash TEXT NOT NULL, character_name TEXT NOT NULL,
            profession_id INTEGER NOT NULL DEFAULT 0, club_role TEXT NOT NULL DEFAULT '普通成员',
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(club_id, character_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_pvp_alliance_member_character
            ON pvp_alliance_members(character_hash);
        CREATE TABLE IF NOT EXISTS pvp_alliance_subscriptions (
            club_id TEXT NOT NULL REFERENCES pvp_alliances(club_id) ON DELETE CASCADE,
            card_hash TEXT NOT NULL,
            player_hash TEXT NOT NULL,
            player_name TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            PRIMARY KEY(club_id,card_hash,player_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_pvp_alliance_subscription_owner
            ON pvp_alliance_subscriptions(club_id,card_hash,created_at DESC);
        CREATE TABLE IF NOT EXISTS pvp_subscriptions (
            card_hash TEXT NOT NULL,
            player_hash TEXT NOT NULL,
            player_name TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            PRIMARY KEY(card_hash,player_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_pvp_subscription_owner
            ON pvp_subscriptions(card_hash,created_at DESC);
    """)
    connection.execute("""INSERT OR IGNORE INTO pvp_subscriptions
        (card_hash,player_hash,player_name,seen_count,created_at)
        SELECT card_hash,player_hash,MAX(player_name),MAX(seen_count),MIN(created_at)
        FROM pvp_alliance_subscriptions GROUP BY card_hash,player_hash""")
    connection.execute("DELETE FROM pvp_alliance_subscriptions")
    # Production databases created by the first PVP preview are upgraded in
    # place.  Payload JSON remains the complete source; these columns make the
    # requested per-match totals queryable without rewriting old records.
    existing = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(pvp_matches)")
    }
    additions = {
        "mode_id": "INTEGER NOT NULL DEFAULT 0",
        "damage_done": "INTEGER NOT NULL DEFAULT 0",
        "damage_taken": "INTEGER",
        "series_id": "TEXT NOT NULL DEFAULT ''",
        "event_id": "TEXT NOT NULL DEFAULT ''",
        "war_id": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in additions.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE pvp_matches ADD COLUMN {name} {declaration}"
            )


def _text(value, limit, required=False):
    text = str(value or "").strip()
    if required and not text:
        raise PvpBackendError("PVP_BAD_RECORD", "缺少必要的 PvP 字段。")
    return text[:limit]


def _integer(value, minimum=0, maximum=(1 << 63) - 1, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 数值字段无效。")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 整数字段无效。")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 数值字段无效。") from None
    if not minimum <= number <= maximum:
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 数值字段超出范围。")
    return number


def _number(value, minimum=0.0, maximum=86_400.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 时间字段无效。") from None
    if not minimum <= number <= maximum:
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 时间字段超出范围。")
    return number


def _scope(value, allowed, *, field):
    if value in (None, ""):
        return ""
    normalized = str(value).strip().casefold()
    if normalized not in allowed:
        raise PvpBackendError(
            "PVP_BAD_RECORD", f"PvP {field} 数据来源标记无效。"
        )
    return normalized


def _clean_data_scope(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 数据来源标记无效。")
    allowed = {
        "roster": PVP_ROSTER_SCOPES,
        "self_metrics": PVP_DATA_SCOPES,
        "team_metrics": PVP_DATA_SCOPES,
        "opponent_interactions": PVP_DATA_SCOPES,
    }
    result = {}
    for field, choices in allowed.items():
        cleaned = _scope(value.get(field), choices, field=field)
        if cleaned:
            result[field] = cleaned
    return result


def _clean_integer_list(value, *, limit=20_000, maximum=(1 << 63) - 1):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 明细数量超出限制。")
    return [_integer(item, maximum=maximum) for item in value]


def _clean_skills(value):
    rows = value if isinstance(value, list) else []
    if len(rows) > 2048:
        raise PvpBackendError("PVP_BAD_RECORD", "技能记录数量超出限制。")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Do not persist roster placeholders as used skills.  A cast-only row
        # remains valid; a row with no damage, hit, or cast evidence does not.
        if not any(
            _integer(row.get(field), optional=True) not in (None, 0)
            for field in ("damage", "hits", "casts")
        ):
            continue
        share = row.get("share")
        result.append({
            "skill_id": _integer(
                row.get("skill_id"), maximum=(1 << 63) - 1
            ),
            "name": _text(row.get("name"), 96),
            "hits": _integer(
                row.get("hits"), maximum=1_000_000, optional=True
            ),
            "casts": _integer(
                row.get("casts"), maximum=1_000_000, optional=True
            ),
            "critical_hits": _integer(
                row.get("critical_hits"), maximum=1_000_000, optional=True
            ),
            "damage": _integer(row.get("damage"), optional=True),
            "max_hit": _integer(row.get("max_hit"), optional=True),
            "share": (
                None if share is None else _number(share, maximum=1.0)
            ),
            "hit_timestamps_ns": _clean_integer_list(
                row.get("hit_timestamps_ns")
            ),
            "cast_timestamps_ns": _clean_integer_list(
                row.get("cast_timestamps_ns")
            ),
        })
    return result


def _clean_equipment_detail(value, depth=0):
    """Keep click-through equipment details bounded and JSON-safe."""

    if depth >= 4:
        return "..."
    if value is None or isinstance(value, (str, bool, int, float)):
        return value if not isinstance(value, str) else value[:256]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _clean_equipment_detail(item, depth + 1)
            for index, (key, item) in enumerate(value.items())
            if index < 32
        }
    if isinstance(value, list):
        return [_clean_equipment_detail(item, depth + 1) for item in value[:32]]
    return str(value)[:256]


def _clean_equipment(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PvpBackendError("PVP_BAD_RECORD", "装备快照格式无效。")
    equipment = value.get("equipment")
    if not isinstance(equipment, list) or len(equipment) > 64:
        raise PvpBackendError("PVP_BAD_RECORD", "装备快照数量无效。")
    cleaned_equipment = []
    for row in equipment:
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        cleaned_metadata = {
            key: metadata[key]
            for key in (
                "tag", "mode", "tag_name_id", "item_name_id",
                "item_description_id", "icon", "quality", "sub_type",
                "base_score", "item_level", "required_level",
                "season_id", "random_group",
            )
            if key in metadata and isinstance(
                metadata[key], (str, int, float, bool)
            )
        }
        cleaned_equipment.append({
            "slot": _integer(row.get("slot"), maximum=255),
            "slot_name": _text(row.get("slot_name"), 24),
            "item_id": _integer(row.get("item_id")),
            "item_name": _text(row.get("item_name"), 64),
            "word_ids": _clean_integer_list(
                row.get("word_ids"), limit=64
            ),
            "auxiliary_id": _integer(
                row.get("auxiliary_id"), optional=True
            ),
            "word_score": _integer(
                row.get("word_score"), maximum=10_000_000, optional=True
            ),
            "item_score": _integer(
                row.get("item_score"), maximum=10_000_000, optional=True
            ),
            "base_score": _integer(
                row.get("base_score"), maximum=10_000_000, optional=True
            ),
            "random_score": _integer(
                row.get("random_score"), maximum=10_000_000, optional=True
            ),
            "enhance_score": _integer(
                row.get("enhance_score"), maximum=10_000_000, optional=True
            ),
            "enhance_level": _integer(
                row.get("enhance_level"), maximum=1_000, optional=True
            ),
            "enhance_completed_level": _integer(
                row.get("enhance_completed_level"),
                maximum=1_000,
                optional=True,
            ),
            "enhance_stage_count": _integer(
                row.get("enhance_stage_count"), maximum=1_000, optional=True
            ),
            "enhance_stages": _clean_equipment_detail(
                row.get("enhance_stages")
            ) if isinstance(row.get("enhance_stages"), list) else [],
            "grow_body_id": _integer(
                row.get("grow_body_id"), maximum=10_000_000, optional=True
            ),
            "enhance_schedule": _clean_equipment_detail(
                row.get("enhance_schedule")
            ) if isinstance(row.get("enhance_schedule"), list) else [],
            "enhance_completed_score": _integer(
                row.get("enhance_completed_score"),
                maximum=10_000_000,
                optional=True,
            ),
            "enhance_level_score": _integer(
                row.get("enhance_level_score"),
                maximum=10_000_000,
                optional=True,
            ),
            "enhance_level_remaining": _integer(
                row.get("enhance_level_remaining"),
                maximum=10_000_000,
                optional=True,
            ),
            "enhance_level_progress_percent": _integer(
                row.get("enhance_level_progress_percent"),
                maximum=100,
                optional=True,
            ),
            "enhance_overall_percent": _integer(
                row.get("enhance_overall_percent"),
                maximum=100,
                optional=True,
            ),
            "enhance_progress_percent": _integer(
                row.get("enhance_progress_percent"),
                maximum=100,
                optional=True,
            ),
            "next_enhance_level": _integer(
                row.get("next_enhance_level"), maximum=1_000, optional=True
            ),
            "next_enhance_score": _integer(
                row.get("next_enhance_score"),
                maximum=10_000_000,
                optional=True,
            ),
            "next_enhance_increment": _integer(
                row.get("next_enhance_increment"),
                maximum=10_000_000,
                optional=True,
            ),
            "next_enhance_remaining": _integer(
                row.get("next_enhance_remaining"),
                maximum=10_000_000,
                optional=True,
            ),
            "known_score": _integer(
                row.get("known_score"), maximum=10_000_000, optional=True
            ),
            "total_score": _integer(
                row.get("total_score"), maximum=10_000_000, optional=True
            ),
            "score_complete": bool(row.get("score_complete")),
            "score_source": _text(row.get("score_source"), 48),
            "quality": _integer(
                row.get("quality"), maximum=255, optional=True
            ),
            "quality_name": _text(row.get("quality_name"), 24),
            "word_scores": _clean_integer_list(
                row.get("word_scores"), limit=64, maximum=10_000_000
            ),
            "affixes": _clean_equipment_detail(row.get("affixes"))
            if isinstance(row.get("affixes"), list)
            else [],
            "special_affix": _clean_equipment_detail(row.get("special_affix"))
            if isinstance(row.get("special_affix"), dict)
            else None,
            "score_breakdown_total": _integer(
                row.get("score_breakdown_total"),
                maximum=10_000_000,
                optional=True,
            ),
            "score_breakdown_complete": bool(
                row.get("score_breakdown_complete")
            ),
            "score_breakdown_matches": bool(
                row.get("score_breakdown_matches")
            ),
            "details": _clean_equipment_detail(row.get("details")),
            "equipment_mode": _text(row.get("equipment_mode"), 24),
            "is_pvp": bool(row.get("is_pvp")),
            "word_class_types": _clean_integer_list(
                row.get("word_class_types"), limit=64, maximum=1_000_000
            ),
            "active_word_count": _integer(
                row.get("active_word_count"), maximum=1_000, optional=True
            ),
            "total_word_count": _integer(
                row.get("total_word_count"), maximum=1_000, optional=True
            ),
            "metadata": cleaned_metadata,
        })
    return {
        "captured_at": _text(value.get("captured_at"), 48),
        "captured_at_ns": _integer(
            value.get("captured_at_ns"), optional=True
        ),
        "extraordinary_rating": _integer(
            value.get("extraordinary_rating"),
            maximum=1_000_000,
            optional=True,
        ),
        "equipment_score": _integer(
            value.get("equipment_score"),
            maximum=10_000_000,
            optional=True,
        ),
        "equipment_known_score": _integer(
            value.get("equipment_known_score"),
            maximum=10_000_000,
            optional=True,
        ),
        "equipment_score_complete": bool(value.get("equipment_score_complete")),
        "equipment_score_source": _text(value.get("equipment_score_source"), 32),
        "equipment": cleaned_equipment,
        "gems": value.get("gems") if isinstance(value.get("gems"), list) else None,
        "attributes": value.get("attributes") if isinstance(value.get("attributes"), dict) else None,
        "sets": value.get("sets") if isinstance(value.get("sets"), list) else None,
        "affixes": value.get("affixes") if isinstance(value.get("affixes"), list) else None,
        "pvp_equipment_count": _integer(
            value.get("pvp_equipment_count"), maximum=64, optional=True
        ),
        "active_word_count": _integer(
            value.get("active_word_count"), maximum=1_000, optional=True
        ),
        "total_word_count": _integer(
            value.get("total_word_count"), maximum=1_000, optional=True
        ),
        "source": _text(value.get("source"), 48),
        "partial": bool(value.get("partial", True)),
    }


def clean_equipment_snapshot_upload(raw, hmac_key):
    if not isinstance(raw, dict):
        raise PvpBackendError("PVP_BAD_EQUIPMENT_SNAPSHOT", "装备快照格式无效。")
    snapshot_id = _text(raw.get("snapshot_id"), 48, True)
    if not EQUIPMENT_SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
        raise PvpBackendError("PVP_BAD_EQUIPMENT_SNAPSHOT", "装备快照编号无效。")
    try:
        owner_hash = character_hash(raw.get("owner_character_id"), hmac_key)
        target_hash = character_hash(raw.get("target_character_id"), hmac_key)
    except ProfileUploadError as error:
        raise PvpBackendError(
            "PVP_BAD_EQUIPMENT_SNAPSHOT", error.message
        ) from None
    captured_at_ns = _integer(raw.get("captured_at_ns"))
    if captured_at_ns <= 0:
        raise PvpBackendError("PVP_BAD_EQUIPMENT_SNAPSHOT", "装备快照时间无效。")
    equipment_snapshot = _clean_equipment(raw.get("equipment_snapshot"))
    if equipment_snapshot is None:
        raise PvpBackendError("PVP_BAD_EQUIPMENT_SNAPSHOT", "装备快照内容为空。")
    nested_stamp = equipment_snapshot.get("captured_at_ns")
    if nested_stamp not in (None, 0, captured_at_ns):
        raise PvpBackendError(
            "PVP_BAD_EQUIPMENT_SNAPSHOT", "装备快照时间不一致。"
        )
    equipment_snapshot["captured_at_ns"] = captured_at_ns
    captured_at = _text(raw.get("captured_at"), 48)
    if captured_at:
        equipment_snapshot["captured_at"] = captured_at
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "owner_character_hash": owner_hash,
        "target_character_hash": target_hash,
        "target_name": _text(raw.get("target_name"), 64),
        "profession_id": _integer(
            raw.get("profession_id"), maximum=9_999_999, optional=True
        ),
        "extraordinary_rating": _integer(
            raw.get("extraordinary_rating"),
            maximum=1_000_000,
            optional=True,
        ),
        "captured_at_ns": captured_at_ns,
        "captured_at": captured_at,
        "source": _text(raw.get("source"), 48),
        "equipment_snapshot": equipment_snapshot,
    }


def _clean_team_participants(value, *, max_rows=24):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_rows:
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 阵容数量无效。")
    result = []
    seen = set()
    for row in value:
        if not isinstance(row, dict):
            continue
        character_id = _text(row.get("character_id"), 64)
        if not character_id or character_id in seen:
            continue
        seen.add(character_id)
        metrics_scope = _scope(
            row.get("metrics_scope"),
            PVP_DATA_SCOPES,
            field="metrics_scope",
        )
        skills_scope = _scope(
            row.get("skills_scope"),
            PVP_DATA_SCOPES,
            field="skills_scope",
        )
        authoritative = row.get("statistics_authoritative")
        if authoritative is not None and not isinstance(authoritative, bool):
            raise PvpBackendError(
                "PVP_BAD_RECORD",
                "PvP statistics_authoritative 标记无效。",
            )
        cleaned = {
            "character_id": character_id,
            "name": _text(row.get("name"), 64) or "未知玩家",
            "profession_id": _integer(row.get("profession_id"), maximum=9_999_999, optional=True),
            "level": _integer(row.get("level"), maximum=1_000, optional=True),
            "extraordinary_rating": _integer(row.get("extraordinary_rating"), maximum=1_000_000, optional=True),
            "avatar_id": _integer(row.get("avatar_id"), maximum=9_999_999, optional=True),
            "avatar_frame_id": _integer(row.get("avatar_frame_id"), maximum=9_999_999, optional=True),
            "is_ai": bool(row.get("is_ai")),
            "is_self": bool(row.get("is_self")),
            "kills": _integer(row.get("kills", row.get("kills_on")), maximum=1_000_000, optional=True),
            "assists": _integer(row.get("assists"), maximum=1_000_000, optional=True),
            "deaths": _integer(row.get("deaths", row.get("defeats", row.get("deaths_to"))), maximum=1_000_000, optional=True),
            "damage": _integer(row.get("damage", row.get("damage_done")), optional=True),
            "healing": _integer(row.get("healing", row.get("healing_done", row.get("effective_healing"))), optional=True),
            "taken": _integer(row.get("taken", row.get("damage_taken", row.get("damage_from"))), optional=True),
            "current_dead": bool(row.get("current_dead")),
            "skills": _clean_skills(row.get("skills")),
            "equipment_snapshot": _clean_equipment(
                row.get("equipment_snapshot")
            ),
        }
        if row.get("healing_source") in {"network_exact", "hp_balance_estimate"}:
            cleaned["healing_source"] = row["healing_source"]
        if metrics_scope:
            cleaned["metrics_scope"] = metrics_scope
        if skills_scope:
            cleaned["skills_scope"] = skills_scope
        if authoritative is not None:
            cleaned["statistics_authoritative"] = authoritative
        result.append(cleaned)
    return result


def _clean_hunter_monsters(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > len(HUNTER_DRAGON_TEMPLATE_IDS):
        raise PvpBackendError("PVP_BAD_RECORD", "猎龙目标数量无效。")
    result = []
    seen = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        template_id = _integer(raw.get("template_id"), maximum=99_999_999)
        if template_id not in HUNTER_DRAGON_TEMPLATE_IDS or template_id in seen:
            raise PvpBackendError("PVP_BAD_RECORD", "猎龙目标模板无效。")
        seen.add(template_id)
        result.append({
            "template_id": template_id,
            "name": "战争巨龙",
            "damage_to": _integer(raw.get("damage_to", raw.get("damage"))),
            "damage_from": _integer(raw.get("damage_from", raw.get("taken"))),
            "skills_outgoing": _clean_skills(raw.get("skills_outgoing")),
            "skills_incoming": _clean_skills(raw.get("skills_incoming")),
            "defeated": bool(raw.get("defeated")),
            "last_interaction_ns": _integer(
                raw.get("last_interaction_ns"), optional=True
            ),
            "death_timestamp_ns": _integer(
                raw.get("death_timestamp_ns"), optional=True
            ),
        })
    return result


def clean_pvp_record(raw, hmac_key):
    if not isinstance(raw, dict):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 记录不是对象。")
    match_id = _text(raw.get("match_id") or raw.get("battle_id"), 48, True)
    if not MATCH_ID_PATTERN.fullmatch(match_id):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 场次编号无效。")
    if raw.get("battle_id") not in (None, "", match_id):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP battle_id 与场次编号不一致。")
    map_id = _integer(raw.get("map_id"), maximum=99_999_999)
    match_kind = _text(raw.get("match_kind"), 24)
    if map_id not in PVP_MAPS and not (map_id in {0, 5200002} and match_kind == 'duel'):
        raise PvpBackendError("PVP_BAD_MAP", "该地图不在当前 PvP 自动上传范围内。")
    started_ns = _integer(raw.get("started_at_ns"))
    ended_ns = _integer(raw.get("ended_at_ns"))
    if ended_ns < started_ns:
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 结束时间早于开始时间。")
    duration = _number(raw.get("duration_seconds"))
    if abs(duration - (ended_ns - started_ns) / 1e9) > 2:
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 场次时长不一致。")
    result = _text(raw.get("result"), 8, True)
    if result not in RESULTS:
        raise PvpBackendError("PVP_BAD_RESULT", "PvP 结果必须是胜利、失败或未知。")
    player = raw.get("player")
    if not isinstance(player, dict):
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 本人数据缺失。")
    character_id = _text(player.get("character_id"), 64, True)
    try:
        player_hash = character_hash(character_id, hmac_key)
    except ProfileUploadError as exc:
        raise PvpBackendError("PVP_BAD_CHARACTER", "PvP 角色标识无效。") from exc
    opponents = raw.get("opponents")
    opponent_limit = 180 if map_id == 5200223 else 128
    if not isinstance(opponents, list) or len(opponents) > opponent_limit:
        raise PvpBackendError("PVP_BAD_RECORD", "PvP 对手数量无效。")
    clean_opponents = []
    for item in opponents:
        if not isinstance(item, dict):
            continue
        timeline = item.get("timeline") if isinstance(item.get("timeline"), list) else []
        if len(timeline) > 2048:
            raise PvpBackendError("PVP_BAD_RECORD", "交手记录数量超出限制。")
        damage_to = _integer(item.get("damage", item.get("damage_to")))
        damage_from = _integer(item.get("taken", item.get("damage_from")), optional=True)
        kills_on = _integer(item.get("kills", item.get("kills_on")), maximum=1_000_000)
        deaths_to = _integer(item.get("defeats", item.get("deaths_to")), maximum=1_000_000)
        alias_pairs = (
            ('damage', 'damage_to', {}),
            ('taken', 'damage_from', {'optional': True}),
            ('kills', 'kills_on', {'maximum': 1_000_000}),
            ('defeats', 'deaths_to', {'maximum': 1_000_000}),
        )
        for old_name, new_name, options in alias_pairs:
            if (old_name in item and new_name in item
                    and _integer(item[old_name], **options)
                    != _integer(item[new_name], **options)):
                raise PvpBackendError(
                    "PVP_BAD_RECORD", f"PvP 对手 {new_name} 与旧字段不一致。"
                )
        interaction_scope = _scope(
            item.get("interaction_scope"),
            PVP_DATA_SCOPES,
            field="interaction_scope",
        )
        cleaned_opponent = {"opponent_id": _text(item.get("opponent_id"), 96, True),
            "character_id": _text(item.get("character_id"), 64), "name": _text(item.get("name"), 64) or "未知玩家",
            "profession_id": _integer(item.get("profession_id"), maximum=9_999_999, optional=True),
            "level": _integer(item.get("level"), maximum=1_000, optional=True),
            "club_name": _text(item.get("club_name"), 64),
            "extraordinary_rating": _integer(item.get("extraordinary_rating"), maximum=1_000_000, optional=True),
            "avatar_id": _integer(item.get("avatar_id"), maximum=9_999_999, optional=True),
            "avatar_frame_id": _integer(item.get("avatar_frame_id"), maximum=9_999_999, optional=True),
            "is_ai": bool(item.get("is_ai")),
            "kills": kills_on, "assists": _integer(item.get("assists"), maximum=1_000_000, optional=True),
            "defeats": deaths_to, "damage": damage_to, "taken": damage_from,
            "damage_to": damage_to, "damage_from": damage_from,
            "kills_on": kills_on, "deaths_to": deaths_to,
            "damage_share": _number(item.get("damage_share", 0) or 0, maximum=1.0),
            "death_taken_share": (None if item.get("death_taken_share") is None else _number(item["death_taken_share"], maximum=1.0)),
            "skills_outgoing": _clean_skills(item.get("skills_outgoing")),
            "skills_incoming": _clean_skills(item.get("skills_incoming")),
            "timeline": [{"type": _text(event.get("type"), 12),
                          "timestamp_ns": _integer(event.get("timestamp_ns"))}
                         for event in timeline if isinstance(event, dict)],
            "last_interaction_ns": _integer(item.get("last_interaction_ns"), optional=True),
            "equipment_snapshot": _clean_equipment(item.get("equipment_snapshot"))}
        if interaction_scope:
            cleaned_opponent["interaction_scope"] = interaction_scope
        clean_opponents.append(cleaned_opponent)
    damage_done = _integer(raw.get("damage", raw.get("damage_done")))
    damage_taken = _integer(raw.get("taken", raw.get("damage_taken")), optional=True)
    for old_name, new_name, options in (
        ("damage", "damage_done", {}),
        ("taken", "damage_taken", {'optional': True}),
    ):
        if (old_name in raw and new_name in raw
                and _integer(raw[old_name], **options)
                != _integer(raw[new_name], **options)):
            raise PvpBackendError("PVP_BAD_RECORD", f"PvP {new_name} 与旧字段不一致。")
    raw_teams = raw.get("teams") if isinstance(raw.get("teams"), dict) else None
    if raw_teams is None and isinstance(raw.get("team_stats"), dict):
        raw_teams = raw.get("team_stats")
    if raw_teams is None and isinstance(raw.get("team_participants"), list):
        grouped = {"allies": [], "enemies": []}
        for item in raw.get("team_participants"):
            if not isinstance(item, dict):
                continue
            side = str(item.get("side") or item.get("team") or "").casefold()
            if side in {"ally", "allies", "friendly", "ours", "我方", "1"}:
                grouped["allies"].append(item)
            elif side in {"enemy", "enemies", "opponent", "opposing", "敌方", "2"}:
                grouped["enemies"].append(item)
        raw_teams = grouped if any(grouped.values()) else None
    clean_teams = None
    roster_max_rows = (
        180 if map_id == 5200223
        else 128 if map_id in HUNTER_CITY_MAPS
        else 24
    )
    monsters = (
        _clean_hunter_monsters(raw.get("monsters"))
        if map_id in HUNTER_CITY_MAPS else []
    )
    if raw_teams is not None:
        clean_teams = {
            "allies": _clean_team_participants(raw_teams.get("allies", raw_teams.get("friendly")), max_rows=roster_max_rows),
            "enemies": _clean_team_participants(raw_teams.get("enemies", raw_teams.get("opposing")), max_rows=roster_max_rows),
        }
    clean = {"schema_version": 1, "match_id": match_id, "battle_id": match_id,
             "map_id": map_id,
             "match_kind": match_kind,
             "mode_id": _integer(raw.get("mode_id"), maximum=99_999_999),
             "team_size": _integer(raw.get("team_size"), maximum=60, optional=True),
             "mode_name": _text(raw.get("mode_name"), 48, True), "map_name": _text(raw.get("map_name"), 48, True),
             "instance_id": _text(raw.get("instance_id"), 96), "started_at_ns": started_ns,
             "ended_at_ns": ended_ns, "started_at": _text(raw.get("started_at") or raw.get("start_time"), 48),
             "ended_at": _text(raw.get("ended_at") or raw.get("end_time"), 48),
             "start_time": _text(raw.get("started_at") or raw.get("start_time"), 48),
             "end_time": _text(raw.get("ended_at") or raw.get("end_time"), 48),
             "duration_seconds": duration,
             "result": result, "end_reason": _text(raw.get("end_reason"), 32),
             "player": {"character_id": character_id, "name": _text(player.get("name"), 64) or "未知玩家",
                        "profession_id": _integer(player.get("profession_id"), maximum=9_999_999, optional=True),
                        "level": _integer(player.get("level"), maximum=1_000, optional=True),
                        "extraordinary_rating": _integer(player.get("extraordinary_rating"), maximum=1_000_000, optional=True),
                        "avatar_id": _integer(player.get("avatar_id"), maximum=9_999_999, optional=True),
                        "avatar_frame_id": _integer(player.get("avatar_frame_id"), maximum=9_999_999, optional=True),
                        "equipment_snapshot": _clean_equipment(player.get("equipment_snapshot"))},
             "kills": _integer(raw.get("kills"), maximum=1_000_000),
             "assists": _integer(raw.get("assists"), maximum=1_000_000, optional=True),
             "deaths": _integer(raw.get("deaths"), maximum=1_000_000),
             "damage": damage_done, "taken": damage_taken,
             "damage_done": damage_done, "damage_taken": damage_taken,
             "series_id": _text(raw.get("series_id"), 128),
             "event_id": _text(raw.get("event_id"), 128),
             "war_id": _text(raw.get("war_id"), 128),
             "source_battle_id": _text(raw.get("source_battle_id"), 128),
             "capture_complete": bool(raw.get("capture_complete")),
             "assist_source_available": bool(raw.get("assist_source_available")),
             "incoming_source_available": bool(raw.get("incoming_source_available")),
             "data_scope": _clean_data_scope(raw.get("data_scope")),
             "opponents": clean_opponents,
             "monsters": monsters,
             "monster_damage": sum(row["damage_to"] for row in monsters),
             "monster_taken": sum(row["damage_from"] for row in monsters),
             "allies": _clean_team_participants(raw.get("allies"), max_rows=roster_max_rows),
             "enemies": _clean_team_participants(raw.get("enemies"), max_rows=roster_max_rows)}
    if raw.get("healing") is not None:
        clean["healing"] = _integer(raw["healing"], optional=True)
    if raw.get("healing_source") in {"network_exact", "hp_balance_estimate"}:
        clean["healing_source"] = raw["healing_source"]
    if clean_teams is not None:
        clean["teams"] = clean_teams
    return clean, player_hash


class PvpBackendStore:
    def __init__(self, hmac_key, now=time.time):
        self.hmac_key, self.now = hmac_key, now

    def upload(self, connection, card_hash, raw):
        record, player_hash = clean_pvp_record(raw, self.hmac_key)
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = connection.execute("SELECT uploader_card_hash,payload_json,payload_digest,upload_count FROM pvp_matches WHERE match_id=?",
                                      (record["match_id"],)).fetchone()
        if existing:
            same_payload = existing["payload_digest"] == digest
            if not same_payload:
                try:
                    previous, _player_hash = clean_pvp_record(
                        json.loads(existing['payload_json']), self.hmac_key
                    )
                    previous_text = json.dumps(
                        previous, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    )
                    same_payload = previous_text == text
                except (PvpBackendError, TypeError, ValueError, json.JSONDecodeError):
                    same_payload = False
            if existing["uploader_card_hash"] != card_hash or not same_payload:
                raise PvpBackendError("PVP_MATCH_CONFLICT", "该场次编号已绑定其他内容。")
            connection.execute("""UPDATE pvp_matches SET
                upload_count=upload_count+1,last_uploaded_at=?,payload_json=?,payload_digest=?,
                mode_id=?,damage_done=?,damage_taken=?,series_id=?,event_id=?,war_id=?
                WHERE match_id=?""",
                (self.now(), text, digest, record['mode_id'], record['damage_done'],
                 record['damage_taken'], record['series_id'], record['event_id'],
                 record['war_id'], record["match_id"]))
            self._store_opponents(connection, record)
            return {"match_id": record["match_id"], "duplicate": True,
                    "upload_count": int(existing["upload_count"]) + 1}
        timestamp = self.now()
        player = record["player"]
        connection.execute("""INSERT INTO pvp_matches
            (match_id,uploader_card_hash,player_hash,player_name,profession_id,
             map_id,mode_id,mode_name,map_name,started_at,ended_at,result,kills,
             assists,deaths,damage_done,damage_taken,extraordinary_rating,
             series_id,event_id,war_id,payload_json,payload_digest,created_at,
             last_uploaded_at,upload_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["match_id"], card_hash, player_hash, player["name"], player["profession_id"] or 0,
             record["map_id"], record["mode_id"], record["mode_name"], record["map_name"],
             record["started_at_ns"] / 1e9, record["ended_at_ns"] / 1e9,
             record["result"], record["kills"], record["assists"], record["deaths"],
             record["damage_done"], record["damage_taken"], player["extraordinary_rating"],
             record["series_id"], record["event_id"], record["war_id"], text,
             digest, timestamp, timestamp, 1))
        self._store_opponents(connection, record)
        return {"match_id": record["match_id"], "duplicate": False, "upload_count": 1}

    def upload_equipment_snapshot(self, connection, card_hash, raw):
        snapshot = clean_equipment_snapshot_upload(raw, self.hmac_key)
        text = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = connection.execute(
            """SELECT uploader_card_hash,payload_digest,upload_count
               FROM pvp_equipment_snapshots WHERE snapshot_id=?""",
            (snapshot["snapshot_id"],),
        ).fetchone()
        timestamp = self.now()
        if existing:
            if (
                existing["uploader_card_hash"] != card_hash
                or existing["payload_digest"] != digest
            ):
                raise PvpBackendError(
                    "PVP_EQUIPMENT_SNAPSHOT_CONFLICT",
                    "该装备快照编号已绑定其他内容。",
                )
            connection.execute(
                """UPDATE pvp_equipment_snapshots
                   SET upload_count=upload_count+1,last_uploaded_at=?
                   WHERE snapshot_id=?""",
                (timestamp, snapshot["snapshot_id"]),
            )
            return {
                "snapshot_id": snapshot["snapshot_id"],
                "duplicate": True,
                "upload_count": int(existing["upload_count"]) + 1,
            }
        connection.execute(
            """INSERT INTO pvp_equipment_snapshots
               (snapshot_id,uploader_card_hash,owner_character_hash,
                target_character_hash,target_name,profession_id,
                extraordinary_rating,captured_at_ns,source,payload_json,
                payload_digest,created_at,last_uploaded_at,upload_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                snapshot["snapshot_id"],
                card_hash,
                snapshot["owner_character_hash"],
                snapshot["target_character_hash"],
                snapshot["target_name"],
                snapshot["profession_id"] or 0,
                snapshot["extraordinary_rating"],
                snapshot["captured_at_ns"],
                snapshot["source"],
                text,
                digest,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """DELETE FROM pvp_equipment_snapshots
               WHERE rowid IN (
                   SELECT rowid FROM pvp_equipment_snapshots
                   WHERE uploader_card_hash=? AND target_character_hash=?
                   ORDER BY captured_at_ns DESC,rowid DESC
                   LIMIT -1 OFFSET ?
               )""",
            (
                card_hash,
                snapshot["target_character_hash"],
                EQUIPMENT_SNAPSHOT_HISTORY_LIMIT,
            ),
        )
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "duplicate": False,
            "upload_count": 1,
        }

    @staticmethod
    def _store_opponents(connection, record):
        for opponent in record.get('opponents', ()):
            connection.execute("""INSERT INTO pvp_opponents
                (match_id,opponent_id,character_id,name,damage_to,damage_from,kills_on,deaths_to)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(match_id,opponent_id) DO UPDATE SET
                character_id=excluded.character_id,name=excluded.name,
                damage_to=excluded.damage_to,damage_from=excluded.damage_from,
                kills_on=excluded.kills_on,deaths_to=excluded.deaths_to""",
                (record['match_id'], opponent['opponent_id'],
                 opponent.get('character_id') or '', opponent.get('name') or '未知玩家',
                 opponent['damage_to'], opponent['damage_from'],
                 opponent['kills_on'], opponent['deaths_to']))

    def records(self, connection, card_hash, limit=200):
        limit = min(500, max(1, int(limit)))
        rows = connection.execute("SELECT payload_json FROM pvp_matches WHERE uploader_card_hash=? ORDER BY started_at DESC LIMIT ?",
                                  (card_hash, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def matchups(
        self,
        connection,
        card_hash,
        opponent_ids,
        team_size,
        *,
        exclude_match_id="",
    ):
        """Aggregate this card owner's completed matches by opponent UID."""

        if not isinstance(opponent_ids, list) or len(opponent_ids) > 12:
            raise PvpBackendError("PVP_BAD_QUERY", "对手角色数量无效。")
        normalized_ids = []
        seen_ids = set()
        for value in opponent_ids:
            token = str(value or "").strip()
            if (
                not 8 <= len(token) <= 128
                or not token.isascii()
                or any(character.isspace() for character in token)
            ):
                raise PvpBackendError("PVP_BAD_QUERY", "对手角色标识无效。")
            if token not in seen_ids:
                normalized_ids.append(token)
                seen_ids.add(token)
        try:
            normalized_team_size = int(team_size)
        except (TypeError, ValueError, OverflowError):
            normalized_team_size = 0
        if normalized_team_size not in {1, 3, 6, 12, 60}:
            raise PvpBackendError("PVP_BAD_QUERY", "PvP 队伍规模无效。")
        excluded = str(exclude_match_id or "").strip()
        if excluded and not MATCH_ID_PATTERN.fullmatch(excluded):
            raise PvpBackendError("PVP_BAD_QUERY", "排除的 PvP 场次编号无效。")

        history = {
            token: {
                "total": 0,
                "wins": 0,
                "losses": 0,
                "unknown": 0,
                "win_rate": 0.0,
                "recent": [],
            }
            for token in normalized_ids
        }
        if not history:
            return history
        rows = connection.execute(
            """SELECT match_id,payload_json FROM pvp_matches
               WHERE uploader_card_hash=? ORDER BY started_at DESC""",
            (card_hash,),
        ).fetchall()
        for row in rows:
            if excluded and str(row["match_id"]) == excluded:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            try:
                saved_team_size = int(
                    payload.get("team_size")
                    or (1 if payload.get("match_kind") == "duel" else 0)
                )
            except (TypeError, ValueError, OverflowError):
                saved_team_size = 0
            if saved_team_size != normalized_team_size:
                continue
            opponent_tokens = set()
            for key in ("enemies", "opponents"):
                values = payload.get(key)
                if not isinstance(values, list):
                    continue
                for opponent in values:
                    if not isinstance(opponent, dict):
                        continue
                    token = str(
                        opponent.get("character_id")
                        or opponent.get("user_token")
                        or ""
                    ).strip()
                    if token in history:
                        opponent_tokens.add(token)
            if not opponent_tokens:
                continue
            outcome = str(payload.get("result") or "未知").strip()
            for token in opponent_tokens:
                item = history[token]
                item["total"] += 1
                if outcome == "胜利":
                    item["wins"] += 1
                    marker = "胜"
                elif outcome == "失败":
                    item["losses"] += 1
                    marker = "负"
                else:
                    item["unknown"] += 1
                    marker = "未"
                if len(item["recent"]) < 5:
                    item["recent"].append(marker)
        for item in history.values():
            decided = item["wins"] + item["losses"]
            item["win_rate"] = round(item["wins"] / decided, 4) if decided else 0.0
        return history

    def _alliance_row(self, connection, card_hash):
        return connection.execute("""SELECT a.*,x.admin_role FROM pvp_alliance_admins x
            JOIN pvp_alliances a ON a.club_id=x.club_id WHERE x.card_hash=? AND x.active=1
            AND a.certification_status='active' ORDER BY x.created_at LIMIT 1""", (card_hash,)).fetchone()

    @staticmethod
    def _alliance_metadata(row):
        return {key: row[key] for key in ("club_id", "club_name", "server_name", "certification_status",
                                          "admin_role", "created_at", "updated_at")}

    def alliance(self, connection, card_hash, *, include_members=True):
        row = self._alliance_row(connection, card_hash)
        if not row:
            return {"authorized": False}
        club_id = row["club_id"]
        if not include_members:
            return {"authorized": True, "alliance": self._alliance_metadata(row),
                    "subscriptions": self.subscriptions(connection, card_hash)}
        members = connection.execute("SELECT * FROM pvp_alliance_members WHERE club_id=? ORDER BY created_at", (club_id,)).fetchall()
        result = []
        for member in members:
            totals = connection.execute(
                """SELECT COUNT(*) AS matches,COALESCE(SUM(kills),0) AS kills,
                   COALESCE(SUM(deaths),0) AS deaths,
                   COALESCE(SUM(assists),0) AS assists,
                   COUNT(assists) AS known_assists
                   FROM pvp_matches WHERE player_hash=?""",
                (member["character_hash"],),
            ).fetchone()
            latest_row = connection.execute(
                "SELECT payload_json FROM pvp_matches WHERE player_hash=? ORDER BY started_at DESC LIMIT 1",
                (member["character_hash"],),
            ).fetchone()
            latest = json.loads(latest_row[0]) if latest_row else {}
            result.append({"member_key": member["character_hash"], "character_name": member["character_name"], "profession_id": member["profession_id"],
                           "club_role": member["club_role"], "extraordinary_rating": latest.get("player", {}).get("extraordinary_rating"),
                           "equipment_score": (latest.get("player", {}).get("equipment_snapshot") or {}).get("equipment_score"),
                           "last_active": latest.get("ended_at"), "matches": totals["matches"],
                           "kills": totals["kills"],
                           "assists": totals["assists"] if totals["known_assists"] == totals["matches"] else None,
                           "deaths": totals["deaths"]})
        return {"authorized": True, "alliance": self._alliance_metadata(row),
                "members": result,
                "subscriptions": self.subscriptions(connection, card_hash)}

    def require_alliance(self, connection, card_hash):
        row = self._alliance_row(connection, card_hash)
        if row is None:
            raise PvpBackendError("PVP_ALLIANCE_FORBIDDEN", "当前账号没有已认证俱乐部的管理权限。")
        return self._alliance_metadata(row)

    def member_mutation(self, connection, card_hash, action, body):
        if not isinstance(body, dict):
            raise PvpBackendError("PVP_BAD_MEMBER", "成员资料格式无效。")
        alliance = self.require_alliance(connection, card_hash)
        club_id = alliance["club_id"]
        if action == "batch":
            rows = body.get("members") if isinstance(body.get("members"), list) else []
            if not 1 <= len(rows) <= 500:
                raise PvpBackendError("PVP_BAD_MEMBER", "批量成员数量应为 1–500。")
            for row in rows:
                self.member_mutation(connection, card_hash, "upsert", row)
            return {"changed": len(rows)}
        character_id = _text(body.get("character_id"), 64)
        if character_id:
            try:
                hashed = character_hash(character_id, self.hmac_key)
            except ProfileUploadError as exc:
                raise PvpBackendError("PVP_BAD_CHARACTER", "成员角色 ID 无效。") from exc
        else:
            hashed = _text(body.get("member_key"), 64, True)
            existing = connection.execute(
                "SELECT 1 FROM pvp_alliance_members WHERE club_id=? AND character_hash=?",
                (club_id, hashed),
            ).fetchone()
            if existing is None:
                raise PvpBackendError("PVP_BAD_MEMBER", "该成员不属于当前战盟。")
        if action == "remove":
            cursor = connection.execute("DELETE FROM pvp_alliance_members WHERE club_id=? AND character_hash=?", (club_id, hashed))
            connection.execute("UPDATE pvp_alliances SET updated_at=? WHERE club_id=?", (self.now(), club_id))
            return {"changed": cursor.rowcount}
        role = _text(body.get("club_role"), 16) or "普通成员"
        if role not in ROLES:
            raise PvpBackendError("PVP_BAD_MEMBER", "战盟职务无效。")
        name = _text(body.get("character_name"), 64, True)
        profession = _integer(body.get("profession_id"), maximum=9_999_999, optional=True) or 0
        timestamp = self.now()
        connection.execute("""INSERT INTO pvp_alliance_members
            (club_id,character_hash,character_name,profession_id,club_role,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(club_id,character_hash) DO UPDATE SET
            character_name=excluded.character_name,profession_id=excluded.profession_id,
            club_role=excluded.club_role,updated_at=excluded.updated_at""",
            (club_id, hashed, name, profession, role, timestamp, timestamp))
        connection.execute("UPDATE pvp_alliances SET updated_at=? WHERE club_id=?", (timestamp, club_id))
        return {"changed": 1}

    def member_records(self, connection, card_hash, member_key, limit=200):
        alliance = self.require_alliance(connection, card_hash)
        member = connection.execute(
            "SELECT character_hash FROM pvp_alliance_members WHERE club_id=? AND character_hash=?",
            (alliance["club_id"], _text(member_key, 64, True)),
        ).fetchone()
        if member is None:
            raise PvpBackendError("PVP_BAD_MEMBER", "该成员不属于当前战盟。")
        limit = _integer(limit, minimum=1, maximum=500)
        rows = connection.execute(
            "SELECT payload_json FROM pvp_matches WHERE player_hash=? ORDER BY started_at DESC LIMIT ?",
            (member["character_hash"], limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _record_filter(days, mode_name, now, result=""):
        try:
            days = int(days)
        except (TypeError, ValueError, OverflowError):
            days = -1
        if days not in (0, 1, 7, 30, 90):
            raise PvpBackendError("PVP_BAD_QUERY", "查询时间范围无效。")
        mode_name = _text(mode_name, 48)
        clause = ""
        params = []
        if days:
            clause += " AND started_at BETWEEN ? AND ?"
            params.extend((now - days * 86400, now))
        if mode_name:
            clause += " AND mode_name=?"
            params.append(mode_name)
        result = _text(result, 8)
        if result not in ("", "胜利", "失败", "未知", "无胜负"):
            raise PvpBackendError("PVP_BAD_QUERY", "战斗结果筛选无效。")
        if result == "无胜负":
            clause += " AND map_id IN (5200131,5200167)"
        elif result:
            clause += " AND map_id NOT IN (5200131,5200167)"
            clause += " AND result=?"
            params.append(result)
        return days, mode_name, clause, params

    def _require_uploaded_player(self, connection, member_key):
        player = connection.execute(
            """SELECT player_hash,player_name,profession_id FROM pvp_matches
               WHERE player_hash=? ORDER BY started_at DESC LIMIT 1""",
            (_text(member_key, 64, True),),
        ).fetchone()
        if player is None:
            raise PvpBackendError("PVP_BAD_MEMBER", "该角色暂无已上传的 PvP 记录。")
        return player

    def search_alliance_players(self, connection, card_hash, query):
        query = _text(query, 64, True)
        pattern = "%" + query + "%"
        rows = connection.execute(
            """SELECT player_hash,COUNT(*) AS matches,MAX(started_at) AS last_active
               FROM pvp_matches WHERE player_hash IN
               (SELECT player_hash FROM pvp_matches WHERE player_name LIKE ?)
               GROUP BY player_hash ORDER BY last_active DESC LIMIT 30""",
            (pattern,),
        ).fetchall()
        result = []
        for row in rows:
            latest = connection.execute(
                """SELECT player_name,profession_id FROM pvp_matches
                   WHERE player_hash=? ORDER BY started_at DESC LIMIT 1""",
                (row["player_hash"],),
            ).fetchone()
            result.append({"member_key": row["player_hash"],
                           "character_name": latest["player_name"],
                           "profession_id": latest["profession_id"],
                           "matches": row["matches"],
                           "last_active": row["last_active"]})
        return result

    def subscriptions(self, connection, card_hash):
        rows = connection.execute(
            """SELECT s.player_hash,s.player_name,s.seen_count,s.created_at
               FROM pvp_subscriptions s
               WHERE s.card_hash=? ORDER BY s.created_at DESC""",
            (card_hash,),
        ).fetchall()
        result = []
        for row in rows:
            activity = connection.execute(
                "SELECT COUNT(*) AS matches,MAX(started_at) AS last_active FROM pvp_matches WHERE player_hash=?",
                (row["player_hash"],),
            ).fetchone()
            latest = connection.execute(
                """SELECT player_name FROM pvp_matches WHERE player_hash=?
                   ORDER BY started_at DESC LIMIT 1""",
                (row["player_hash"],),
            ).fetchone()
            result.append({
                "member_key": row["player_hash"],
                "character_name": latest["player_name"] if latest else row["player_name"],
                "matches": activity["matches"], "last_active": activity["last_active"],
                "new_matches": max(0, activity["matches"] - row["seen_count"]),
            })
        return result

    def subscription_action(self, connection, card_hash, action, member_key):
        player = self._require_uploaded_player(connection, member_key)
        member_key = player["player_hash"]
        if action == "unsubscribe":
            connection.execute(
                "DELETE FROM pvp_subscriptions WHERE card_hash=? AND player_hash=?",
                (card_hash, member_key),
            )
        elif action in ("subscribe", "seen"):
            count = connection.execute(
                "SELECT COUNT(*) FROM pvp_matches WHERE player_hash=?",
                (member_key,),
            ).fetchone()[0]
            if not count:
                raise PvpBackendError("PVP_BAD_MEMBER", "该角色暂无已上传的 PvP 记录。")
            if action == "subscribe":
                connection.execute(
                    """INSERT INTO pvp_subscriptions
                       (card_hash,player_hash,player_name,seen_count,created_at)
                       VALUES (?,?,?,?,?) ON CONFLICT(card_hash,player_hash)
                       DO UPDATE SET player_name=excluded.player_name""",
                    (card_hash, member_key, player["player_name"], count,
                     self.now()),
                )
            else:
                cursor = connection.execute(
                    """UPDATE pvp_subscriptions SET seen_count=?
                       WHERE card_hash=? AND player_hash=?""",
                    (count, card_hash, member_key),
                )
                if cursor.rowcount == 0:
                    raise PvpBackendError("PVP_BAD_MEMBER", "尚未订阅该角色。")
        else:
            raise PvpBackendError("PVP_BAD_QUERY", "订阅操作无效。")
        return self.subscriptions(connection, card_hash)

    def alliance_player_records(self, connection, card_hash, member_key,
                                days=0, mode_name="", offset=0, result=""):
        player = self._require_uploaded_player(connection, member_key)
        days, mode_name, clause, params = self._record_filter(
            days, mode_name, self.now(), result)
        offset = _integer(offset, minimum=0)
        key = player["player_hash"]
        values = (key, *params)
        total = connection.execute(
            "SELECT COUNT(*) FROM pvp_matches WHERE player_hash=?" + clause,
            values,
        ).fetchone()[0]
        rows = connection.execute(
            """SELECT match_id,map_id,mode_name,map_name,started_at,result,
                      kills,assists,deaths,damage_done,damage_taken
               FROM pvp_matches WHERE player_hash=?""" + clause
            + " ORDER BY started_at DESC,match_id DESC LIMIT 50 OFFSET ?",
            (*values, offset),
        ).fetchall()
        records = [{"match_id": row["match_id"], "map_id": row["map_id"],
                    "mode_name": row["mode_name"], "map_name": row["map_name"],
                    "started_at_ns": int(row["started_at"] * 1e9),
                    "result": "无胜负" if row["map_id"] in HUNTER_CITY_MAPS else row["result"],
                    "kills": row["kills"],
                    "assists": row["assists"], "deaths": row["deaths"],
                    "damage": row["damage_done"], "taken": row["damage_taken"]}
                   for row in rows]
        return {"member_key": key, "character_name": player["player_name"],
                "days": days, "mode_name": mode_name, "offset": offset,
                "total": total, "records": records}

    def alliance_player_record(self, connection, card_hash, member_key, match_id):
        player = self._require_uploaded_player(connection, member_key)
        match_id = _text(match_id, 48, True)
        if not MATCH_ID_PATTERN.fullmatch(match_id):
            raise PvpBackendError("PVP_BAD_RECORD", "PvP 场次编号无效。")
        row = connection.execute(
            "SELECT payload_json FROM pvp_matches WHERE player_hash=? AND match_id=?",
            (player["player_hash"], match_id),
        ).fetchone()
        if row is None:
            raise PvpBackendError("PVP_BAD_RECORD", "无权查看该战报或战报不存在。")
        return json.loads(row["payload_json"])

    def subscribed_report(self, connection, card_hash, days=30, mode_name="",
                          start_date="", end_date="", member_keys=None):
        now = self.now()
        if _text(mode_name, 48) not in ("", "猎龙之城"):
            raise PvpBackendError("PVP_BAD_QUERY", "订阅统计当前只支持猎龙之城。")
        days, _mode_name, clause, params = self._record_filter(days, "", now)
        start_date = _text(start_date, 10)
        end_date = _text(end_date, 10)
        custom_range = bool(start_date or end_date)
        if custom_range:
            if not start_date or not end_date:
                raise PvpBackendError("PVP_BAD_QUERY", "请填写完整的开始和结束日期。")
            try:
                first = datetime.strptime(start_date, "%Y-%m-%d")
                last = datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                raise PvpBackendError("PVP_BAD_QUERY", "日期格式应为 YYYY-MM-DD。") from None
            if (first.strftime("%Y-%m-%d") != start_date
                    or last.strftime("%Y-%m-%d") != end_date
                    or first > last):
                raise PvpBackendError("PVP_BAD_QUERY", "查询日期范围无效。")
            china_time = timezone(timedelta(hours=8))
            range_start = first.replace(tzinfo=china_time).timestamp()
            range_end = (last + timedelta(days=1)).replace(
                tzinfo=china_time).timestamp()
            days, _mode_name, clause, params = self._record_filter(0, "", now)
            clause += " AND started_at>=? AND started_at<?"
            params.extend((range_start, range_end))
        else:
            range_start = now - (days or 30) * 86400
            range_end = now
        clause += " AND map_id IN (5200131,5200167)"
        subscribed = self.subscriptions(connection, card_hash)
        selected_clause = ""
        selected_params = ()
        if member_keys is not None:
            if not isinstance(member_keys, list) or len(member_keys) > 500:
                raise PvpBackendError("PVP_BAD_QUERY", "统计角色列表无效。")
            selected = {_text(key, 64, True) for key in member_keys}
            available = {item["member_key"] for item in subscribed}
            if len(selected) != len(member_keys) or not selected.issubset(available):
                raise PvpBackendError("PVP_BAD_QUERY", "只能统计当前账号已订阅的角色。")
            subscribed = [item for item in subscribed if item["member_key"] in selected]
            if subscribed:
                selected_clause = " AND m.player_hash IN (" + ",".join(
                    "?" for _ in subscribed) + ")"
                selected_params = tuple(item["member_key"] for item in subscribed)
        members = []
        for item in subscribed:
            totals = connection.execute(
                """SELECT COUNT(*) AS matches,COALESCE(SUM(kills),0) AS kills,
                          COALESCE(SUM(deaths),0) AS deaths,
                          COALESCE(SUM(damage_done),0) AS damage,
                          COALESCE(SUM(damage_taken),0) AS taken,
                          COUNT(damage_taken) AS taken_matches,
                          SUM(CASE WHEN map_id NOT IN (5200131,5200167)
                                   AND result='胜利' THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN map_id NOT IN (5200131,5200167)
                                   AND result='失败' THEN 1 ELSE 0 END) AS losses
                   FROM pvp_matches WHERE player_hash=?""" + clause,
                (item["member_key"], *params),
            ).fetchone()
            members.append({"member_key": item["member_key"],
                            "character_name": item["character_name"],
                            **{field: totals[field] or 0 for field in
                               ("matches", "kills", "deaths", "damage", "taken",
                                "taken_matches", "wins", "losses")}})
        modes = []
        trend_totals = [dict(matches=0, damage=0, taken=0,
                             taken_matches=0, kills=0, deaths=0)
                        for _ in range(12)]
        trend_start = range_start
        if subscribed:
            rows = connection.execute(
                """SELECT m.mode_name,COUNT(*) AS matches FROM pvp_matches m
                   JOIN pvp_subscriptions s ON s.player_hash=m.player_hash
                   WHERE s.card_hash=?""" + selected_clause + clause
                + " GROUP BY m.mode_name ORDER BY matches DESC,m.mode_name",
                (card_hash, *selected_params, *params),
            ).fetchall()
            modes = [{"mode_name": row["mode_name"], "matches": row["matches"]}
                     for row in rows]
            if not days and not custom_range:
                earliest = connection.execute(
                    """SELECT MIN(m.started_at) FROM pvp_matches m
                       JOIN pvp_subscriptions s ON s.player_hash=m.player_hash
                       WHERE s.card_hash=?""" + selected_clause + clause,
                    (card_hash, *selected_params, *params),
                ).fetchone()[0]
                if earliest is not None:
                    trend_start = earliest
            bucket_seconds = max(1.0, (range_end - trend_start) / len(trend_totals))
            rows = connection.execute(
                """SELECT CAST((m.started_at-?)/? AS INTEGER) AS bucket,
                          COUNT(*) AS matches,
                          COALESCE(SUM(m.damage_done),0) AS damage,
                          COALESCE(SUM(m.damage_taken),0) AS taken,
                          COUNT(m.damage_taken) AS taken_matches,
                          COALESCE(SUM(m.kills),0) AS kills,
                          COALESCE(SUM(m.deaths),0) AS deaths
                   FROM pvp_matches m
                   JOIN pvp_subscriptions s ON s.player_hash=m.player_hash
                   WHERE s.card_hash=?""" + selected_clause + clause + " GROUP BY bucket",
                (trend_start, bucket_seconds, card_hash,
                 *selected_params, *params),
            ).fetchall()
            for row in rows:
                index = min(len(trend_totals) - 1, max(0, int(row["bucket"])))
                for field in trend_totals[index]:
                    trend_totals[index][field] += row[field]
        bucket_seconds = max(1.0, (range_end - trend_start) / len(trend_totals))
        trend = [{"started_at": trend_start + index * bucket_seconds,
                  **totals}
                 for index, totals in enumerate(trend_totals)]
        opponents = []
        if subscribed:
            rows = connection.execute(
                """SELECT CASE WHEN o.character_id<>'' THEN o.character_id
                              ELSE o.match_id||':'||o.opponent_id END AS opponent_key,
                          MAX(o.name) AS name,COUNT(*) AS interactions,
                          COALESCE(SUM(o.damage_to),0) AS damage,
                          COALESCE(SUM(o.damage_from),0) AS taken,
                          COUNT(o.damage_from) AS taken_matches,
                          COALESCE(SUM(o.kills_on),0) AS kills,
                          COALESCE(SUM(o.deaths_to),0) AS deaths
                   FROM pvp_opponents o
                   JOIN pvp_matches m ON m.match_id=o.match_id
                   JOIN pvp_subscriptions s ON s.player_hash=m.player_hash
                   WHERE s.card_hash=?""" + selected_clause + clause
                + " GROUP BY opponent_key",
                (card_hash, *selected_params, *params),
            ).fetchall()
            opponents = [{field: row[field] for field in
                          ("opponent_key", "name", "interactions", "damage",
                           "taken", "taken_matches", "kills", "deaths")}
                         for row in rows]
        summary = {field: sum(row[field] for row in members)
                   for field in ("matches", "kills", "deaths", "damage", "taken",
                                 "taken_matches", "wins", "losses")}
        return {"days": days, "mode_name": "猎龙之城",
                "battle_name": "猎龙之城",
                "generated_at": now, "subscribed_count": len(subscribed),
                "summary": summary, "members": members, "modes": modes,
                "trend": trend, "opponents": opponents,
                "start_date": start_date,
                "end_date": end_date, "range_start": trend_start,
                "range_end": range_end,
                "selected_member_keys": [item["member_key"] for item in subscribed]}

    def hunter_city_analysis(self, connection, card_hash, days=30, member_key=""):
        alliance = self.require_alliance(connection, card_hash)
        try:
            days = int(days)
        except (TypeError, ValueError, OverflowError):
            days = 0
        if days not in (7, 30, 90):
            raise PvpBackendError("PVP_BAD_QUERY", "查询时间范围无效。")
        member_key = _text(member_key, 64)
        members = connection.execute(
            "SELECT character_hash,character_name,profession_id FROM pvp_alliance_members WHERE club_id=?",
            (alliance["club_id"],),
        ).fetchall()
        roster = {row["character_hash"]: row for row in members}
        if member_key and member_key not in roster:
            raise PvpBackendError("PVP_BAD_MEMBER", "该成员不属于当前战盟。")
        now = self.now()
        summary = dict(matches=0, members=0, kills=0, deaths=0, damage=0,
                       taken=0, taken_matches=0, complete_matches=0)
        by_member = {}
        outgoing_skills, incoming_skills = {}, {}
        recent = []
        rows = connection.execute(
            """SELECT player_hash,payload_json FROM pvp_matches
               WHERE map_id IN (?,?) AND started_at BETWEEN ? AND ?
               AND player_hash IN (SELECT character_hash FROM pvp_alliance_members WHERE club_id=?)
               AND (?='' OR player_hash=?) ORDER BY started_at DESC""",
            (*HUNTER_CITY_MAPS, now - days * 86400, now,
             alliance["club_id"], member_key, member_key),
        )
        for row in rows:
            payload = json.loads(row["payload_json"])
            owner = row["player_hash"]
            if owner not in roster:
                continue
            member = by_member.setdefault(owner, {
                "member_key": owner,
                "character_name": roster[owner]["character_name"],
                "profession_id": roster[owner]["profession_id"],
                "matches": 0, "kills": 0, "deaths": 0, "damage": 0,
                "taken": 0, "taken_matches": 0,
            })
            for totals in (summary, member):
                totals["matches"] += 1
                totals["kills"] += int(payload.get("kills") or 0)
                totals["deaths"] += int(payload.get("deaths") or 0)
                totals["damage"] += int(payload.get("damage") or 0)
                if payload.get("taken") is not None:
                    totals["taken"] += int(payload["taken"])
                    totals["taken_matches"] += 1
            if payload.get("capture_complete") is True:
                summary["complete_matches"] += 1
            if len(recent) < 50:
                recent.append({
                    "match_id": payload["match_id"],
                    "map_id": payload["map_id"],
                    "mode_name": payload["mode_name"],
                    "started_at_ns": payload["started_at_ns"],
                    "player": {"name": payload["player"]["name"]},
                    "kills": payload["kills"],
                    "deaths": payload["deaths"],
                    "damage": payload["damage"],
                })
            for opponent in payload.get("opponents") or ():
                if not isinstance(opponent, dict):
                    continue
                for field, totals in (("skills_outgoing", outgoing_skills),
                                      ("skills_incoming", incoming_skills)):
                    for skill in opponent.get(field) or ():
                        if not isinstance(skill, dict):
                            continue
                        # A cast callback has no target. Only a confirmed hit
                        # belongs in the player-to-player skill analysis.
                        if not (int(skill.get("hits") or 0) or int(skill.get("damage") or 0)):
                            continue
                        skill_id = int(skill.get("skill_id") or 0)
                        item = totals.setdefault(skill_id, {
                            "skill_id": skill_id, "name": "", "hits": 0,
                            "damage": 0, "max_hit": 0,
                        })
                        item["name"] = item["name"] or str(skill.get("name") or "")
                        item["hits"] += int(skill.get("hits") or 0)
                        item["damage"] += int(skill.get("damage") or 0)
                        item["max_hit"] = max(item["max_hit"], int(skill.get("max_hit") or 0))
        summary["members"] = len(by_member)
        return {
            "days": days, "member_key": member_key, "summary": summary,
            "members": sorted(by_member.values(), key=lambda item: (-item["damage"], item["character_name"])),
            "skills_outgoing": sorted(outgoing_skills.values(), key=lambda item: (-item["damage"], item["skill_id"]))[:12],
            "skills_incoming": sorted(incoming_skills.values(), key=lambda item: (-item["damage"], item["skill_id"]))[:12],
            "records": recent,
        }

    def hunter_city_record(self, connection, card_hash, match_id):
        alliance = self.require_alliance(connection, card_hash)
        match_id = _text(match_id, 48, True)
        if not MATCH_ID_PATTERN.fullmatch(match_id):
            raise PvpBackendError("PVP_BAD_RECORD", "PvP 场次编号无效。")
        row = connection.execute(
            """SELECT m.payload_json FROM pvp_matches m
               JOIN pvp_alliance_members a ON a.character_hash=m.player_hash
               WHERE m.match_id=? AND m.map_id IN (?,?) AND a.club_id=?""",
            (match_id, *HUNTER_CITY_MAPS, alliance["club_id"]),
        ).fetchone()
        if row is None:
            raise PvpBackendError("PVP_BAD_RECORD", "无权查看该战报或战报不存在。")
        return json.loads(row["payload_json"])

    def certify(self, connection, club_id, club_name, server_name, admin_card_hash, admin_role="盟主"):
        club_id = _text(club_id, 48, True)
        if not CLUB_ID_PATTERN.fullmatch(club_id):
            raise PvpBackendError("PVP_BAD_ALLIANCE", "俱乐部 ID 无效。")
        timestamp = self.now()
        connection.execute("""INSERT INTO pvp_alliances VALUES (?,?,?,?,?,?)
            ON CONFLICT(club_id) DO UPDATE SET club_name=excluded.club_name,server_name=excluded.server_name,
            certification_status='active',updated_at=excluded.updated_at""",
            (club_id, _text(club_name, 64, True), _text(server_name, 64), "active", timestamp, timestamp))
        connection.execute("""INSERT INTO pvp_alliance_admins VALUES (?,?,?,?,?)
            ON CONFLICT(club_id,card_hash) DO UPDATE SET admin_role=excluded.admin_role,active=1""",
            (club_id, admin_card_hash, _text(admin_role, 16) or "盟主", 1, timestamp))
        return {"club_id": club_id}
