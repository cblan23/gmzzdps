"""Map-scoped PVP matches, immutable local history and a durable upload outbox.

This domain never reads the game or invokes game RPCs. It consumes only the
existing passive observations, independently of the PVE Encounter pipeline.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from array import array
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import sqlite3
import threading
import time
import uuid

from network_state import SCENE_TRANSITION_METHODS
from pvp_tracker import PvpTracker, pvp_bot_evidence, skill_has_activity, _guild_league_settlement


# Distinct template IDs are required even when two modes share 猎龙之城.
# All current formal PVP scenes exported from the 2026-09-18 client catalog.
# Entry is the start boundary and leaving/switching space is the end boundary.
RECORDABLE_MAPS = {
    5200020: {"mode_name": "主宰争锋", "map_name": "主宰争锋", "mode_id": 5500002, "team_size": 3},
    5200021: {"mode_name": "俱乐部乱斗", "map_name": "俱乐部", "mode_id": 5500013},
    5200086: {"mode_name": "本服俱乐部宣战", "map_name": "霍纳奇斯山脉", "mode_id": 5500018},
    5200110: {"mode_name": "众神之巅小组赛", "map_name": "众神之巅", "mode_id": 5500004, "team_size": 6},
    5200111: {"mode_name": "众神之巅淘汰赛", "map_name": "众神之巅", "mode_id": 5500005, "team_size": 6},
    5200131: {"mode_name": "猎城战", "map_name": "猎龙之城", "mode_id": 5500008},
    5200148: {"mode_name": "高原战", "map_name": "星星高原", "mode_id": 5500011},
    5200167: {"mode_name": "终末猎杀", "map_name": "猎龙之城", "mode_id": 5500012},
    5200223: {"mode_name": "四方联赛", "map_name": "四方联赛", "mode_id": 5500009, "team_size": 60},
    5200229: {"mode_name": "战略服俱乐部宣战", "map_name": "征服宣令", "mode_id": 5500014},
    5200253: {"mode_name": "霜殒领主", "map_name": "霜陨领主", "mode_id": 5500015},
    5200280: {"mode_name": "房间1v1模式", "map_name": "勇者对决", "mode_id": 5500017, "team_size": 1},
    5203003: {"mode_name": "命运时刻", "map_name": "命运时刻", "mode_id": 5500003, "team_size": 12},
    5208002: {"mode_name": "主宰争锋", "map_name": "湖区场景", "mode_id": 5500002, "team_size": 3},
    5208003: {"mode_name": "主宰争锋", "map_name": "雪地场景", "mode_id": 5500002, "team_size": 3},
    5208004: {"mode_name": "神座之争", "map_name": "神座之争", "mode_id": 5500010, "team_size": 6},
    5208012: {"mode_name": "主宰争锋", "map_name": "陆地场景", "mode_id": 5500002, "team_size": 3},
    5208017: {"mode_name": "主宰争锋", "map_name": "梅园场景", "mode_id": 5500002, "team_size": 3},
    5208018: {"mode_name": "主宰争锋", "map_name": "教堂场景", "mode_id": 5500002, "team_size": 3},
}
# Kept as a public compatibility name for older imports.  No mode is cumulative
# across matches anymore: every formal match and every duel owns one HUD window.
CUMULATIVE_PVP_MAPS = frozenset()
CHAMPION_MAPS = frozenset({5200110, 5200111})
LARGE_BATTLE_MAPS = frozenset({5200131, 5200148, 5200167, 5200223, 5200253})
CLUB_EVENT_MAPS = frozenset({5200021, 5200086, 5200229})
HUNTER_CITY_MAPS = frozenset({5200131, 5200167})
# A Hunter City visit is one match. Leaving must commit it to the upload
# outbox immediately, even if the same map or instance is entered again.
RESUMABLE_PVP_MAPS = (LARGE_BATTLE_MAPS - HUNTER_CITY_MAPS) | CLUB_EVENT_MAPS
PVP_CONTEXT_FIELDS = ('series_id', 'round_id', 'event_id', 'war_id', 'source_battle_id')
PVP_RECORDING_CONTEXT_FIELDS = PVP_CONTEXT_FIELDS + (
    'mode_id', 'teams', 'team_stats', 'team_participants',
)
DUEL_FINALIZE_GRACE_NS = 10_000_000_000
RESUMABLE_LEAVE_GRACE_NS = 600_000_000_000
# Match the active-query retry boundary: a missing opponent response can delay
# one record for at most one request timeout, never block the outbox forever.
TEAM_EQUIPMENT_UPLOAD_GRACE_SECONDS = 20.0
PVP_CHECKPOINT_INTERVAL_NS = 8_000_000_000
RESULTS = frozenset({"胜利", "失败", "未知"})
PVP_DATA_SCOPES = frozenset(
    {"identity_only", "observed_partial", "self_exact", "pair_exact", "authoritative"}
)


def project_duel_result_counters(payload, *, copy_payload=True):
    """Project the authoritative 1v1 result onto legacy saved counters.

    Older records can contain a verified ``IndividualPVPResult`` but still
    have zero kills/deaths because 1v1 does not necessarily emit EntityDead.
    Keep the immutable SQLite payload untouched while presenting and uploading
    the fixed duel contract: winner 1 kill, loser 1 death, assists always 0.
    """

    if not isinstance(payload, dict):
        return payload
    record = deepcopy(payload) if copy_payload else payload
    try:
        team_size = int(record.get("team_size", 0) or 0)
        map_id = int(record.get("map_id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        team_size = 0
        map_id = 0
    match_kind = str(record.get("match_kind") or "").strip().casefold()
    mode_name = str(record.get("mode_name") or "").strip()
    is_duel = bool(
        match_kind == "duel"
        or (team_size == 1 and map_id == 0 and mode_name == "双人切磋")
    )
    result = str(record.get("result") or "").strip()
    if not is_duel or result not in {"胜利", "失败"}:
        return record

    local_won = result == "胜利"
    local_kills = 1 if local_won else 0
    local_deaths = 0 if local_won else 1
    record.update(kills=local_kills, assists=0, deaths=local_deaths)

    player = record.get("player")
    player = player if isinstance(player, dict) else {}
    player_id = str(player.get("character_id") or "").strip()
    allies = record.get("allies")
    if isinstance(allies, list):
        for row in allies:
            if not isinstance(row, dict):
                continue
            character_id = str(row.get("character_id") or "").strip()
            if row.get("is_self") is True or (
                player_id and character_id == player_id
            ):
                row.update(
                    kills=local_kills,
                    assists=0,
                    deaths=local_deaths,
                )

    opponents = record.get("opponents")
    opponents = opponents if isinstance(opponents, list) else []
    opponent = next(
        (row for row in opponents if isinstance(row, dict)),
        None,
    )
    opponent_id = ""
    if opponent is not None:
        opponent_id = str(
            opponent.get("character_id") or opponent.get("opponent_id") or ""
        ).strip()
        opponent.update(
            kills=local_kills,
            assists=0,
            defeats=local_deaths,
            deaths=local_kills,
            kills_on=local_kills,
            deaths_to=local_deaths,
        )

    enemies = record.get("enemies")
    if isinstance(enemies, list):
        enemy = next(
            (
                row
                for row in enemies
                if isinstance(row, dict)
                and opponent_id
                and str(row.get("character_id") or "").strip() == opponent_id
            ),
            None,
        )
        if enemy is None:
            enemy = next(
                (row for row in enemies if isinstance(row, dict)),
                None,
            )
        if enemy is not None:
            enemy.update(
                kills=local_deaths,
                assists=0,
                deaths=local_kills,
            )
    return record


def iso_time(stamp_ns):
    return datetime.fromtimestamp(stamp_ns / 1e9, timezone.utc).isoformat()


def canonical_json(payload):
    def default(value):
        if isinstance(value, array):
            return list(value)
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=default,
    )


def _team_equipment_snapshots_pending(payload):
    """Return whether a match still awaits a requested human snapshot."""

    if not isinstance(payload, dict):
        return False
    if payload.get('map_id') in HUNTER_CITY_MAPS:
        return any(
            isinstance(row, dict)
            and str(row.get('character_id') or '').strip()
            and row.get('is_ai') is not True
            and not isinstance(row.get('equipment_snapshot'), dict)
            for row in payload.get('opponents', ())
        )
    if payload.get('end_reason') != 'match_result':
        return False
    try:
        team_size = int(payload.get('team_size', 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if team_size <= 1:
        return False
    members = [
        row
        for side in ('allies', 'enemies')
        for row in payload.get(side, ())
        if isinstance(row, dict)
        and str(row.get('character_id') or '').strip()
        and row.get('is_ai') is not True
    ]
    if not members:
        return False
    return any(
        not isinstance(row.get('equipment_snapshot'), dict)
        for row in members
    )


def build_equipment_snapshot_upload(profile):
    """Create one immutable upload unit from a correlated shape response."""

    if not isinstance(profile, dict):
        raise ValueError("Equipment profile is not an object")
    owner_character_id = str(profile.get("local_user_token", "") or "").strip()
    target_character_id = str(profile.get("user_token", "") or "").strip()
    snapshot = profile.get("equipment_snapshot")
    if not owner_character_id or not target_character_id or not isinstance(snapshot, dict):
        raise ValueError("Equipment profile identity or snapshot is incomplete")
    try:
        captured_at_ns = int(
            snapshot.get("captured_at_ns", profile.get("captured_at_ns", 0)) or 0
        )
    except (TypeError, ValueError, OverflowError):
        captured_at_ns = 0
    if captured_at_ns <= 0:
        raise ValueError("Equipment snapshot time is unavailable")
    captured_at = str(snapshot.get("captured_at", "") or "").strip()
    if not captured_at:
        captured_at = iso_time(captured_at_ns)
    payload = {
        "schema_version": 1,
        "owner_character_id": owner_character_id,
        "target_character_id": target_character_id,
        "target_name": str(profile.get("name", "") or "").strip(),
        "profession_id": int(profile.get("profession_id", 0) or 0),
        "extraordinary_rating": int(
            profile.get("extraordinary_rating", 0) or 0
        ) or None,
        "captured_at_ns": captured_at_ns,
        "captured_at": captured_at,
        "source": str(
            profile.get("source_method")
            or snapshot.get("source")
            or "RetOtherRoleShapeData"
        ).strip(),
        "equipment_snapshot": deepcopy(snapshot),
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    payload["snapshot_id"] = "pvp_eq_" + digest[:32]
    return payload


def pair_skills(skills):
    active = [value for value in skills.values() if skill_has_activity(value)]
    total = sum(int(value.get("damage", 0) or 0) for value in active)
    result = []
    for value in sorted(
            active,
            key=lambda value: (
                -(value.get("damage", 0) or 0),
                -(value.get("casts", 0) or 0),
                value.get("skill_id", 0),
            ),
        ):
        row = deepcopy(value)
        for field in ("hit_timestamps_ns", "cast_timestamps_ns"):
            if field in row:
                row[field] = list(row[field])
        row["share"] = (value.get("damage", 0) or 0) / total if total else 0
        result.append(row)
    return result


def _optional_team_settlement(recording, tracker):
    """Read an optional full-team settlement supplied by a newer parser.

    The current passive tracker only guarantees local-player interactions, so
    this helper deliberately returns an empty shape unless an adapter has
    attached a verified ``teams``/``team_stats`` payload.  It gives the record
    writer one stable seam for 3v3/6v6/12v12 captures without turning missing
    teammate totals into zeros.
    """
    candidates = []
    if isinstance(recording, dict):
        for key in ("teams", "team_stats", "team_settlement", "team_participants"):
            value = recording.get(key)
            if value is not None:
                candidates.append((value, True))
    for key in ("teams", "team_stats", "team_settlement", "team_participants"):
        value = getattr(tracker, key, None)
        if value is not None:
            candidates.append((value, False))
    for value, recording_source in candidates:
        if not recording_source:
            rows = []
            if isinstance(value, dict):
                for side_key in ("allies", "friendly", "our_team", "enemies", "opposing", "enemy_team"):
                    side_rows = value.get(side_key)
                    if isinstance(side_rows, list):
                        rows.extend(side_rows)
                explicitly_authoritative = value.get("authoritative") is True
            elif isinstance(value, list):
                rows = value
                explicitly_authoritative = False
            else:
                explicitly_authoritative = False
            if not explicitly_authoritative and not any(
                isinstance(row, dict)
                and row.get("statistics_authoritative") is True
                for row in rows
            ):
                continue
        if isinstance(value, dict):
            allies = value.get("allies", value.get("friendly", value.get("our_team")))
            enemies = value.get("enemies", value.get("opposing", value.get("enemy_team")))
            if isinstance(allies, list) or isinstance(enemies, list):
                return (
                    allies if isinstance(allies, list) else [],
                    enemies if isinstance(enemies, list) else [],
                )
        elif isinstance(value, list):
            allies, enemies = [], []
            for row in value:
                if not isinstance(row, dict):
                    continue
                side = str(row.get("side") or row.get("team") or "").casefold()
                if side in {"ally", "allies", "friendly", "ours", "我方", "1"}:
                    allies.append(row)
                elif side in {"enemy", "enemies", "opponent", "opposing", "敌方", "2"}:
                    enemies.append(row)
            if allies or enemies:
                return allies, enemies
    return [], []


def _merge_team_settlement_rows(base_rows, verified_rows, *, source_scope=None):
    """Overlay only fields explicitly returned by a verified team result."""
    result = [deepcopy(row) for row in base_rows]
    index_by_token = {
        str(row.get("character_id") or "").strip(): index
        for index, row in enumerate(result)
    }
    for raw in verified_rows:
        if not isinstance(raw, dict):
            continue
        token = str(raw.get("character_id") or raw.get("user_token") or "").strip()
        if not token:
            continue
        normalized = {
            "character_id": token,
            "name": raw.get("name") or raw.get("character_name") or "未知玩家",
            "profession_id": raw.get("profession_id"),
            "level": raw.get("level"),
            "extraordinary_rating": raw.get("extraordinary_rating", raw.get("rating")),
            "avatar_id": raw.get("avatar_id"),
            "avatar_frame_id": raw.get("avatar_frame_id"),
        }
        if "is_ai" in raw:
            normalized["is_ai"] = bool(raw.get("is_ai"))
        if "is_self" in raw:
            normalized["is_self"] = bool(raw.get("is_self"))
        has_metrics = False
        aliases = {
            "kills": ("kills", "kills_on"),
            "assists": ("assists",),
            "deaths": ("deaths", "defeats", "deaths_to"),
            "damage": ("damage", "damage_done"),
            "healing": ("healing", "healing_done", "effective_healing", "heal"),
            "taken": ("taken", "damage_taken", "damage_from"),
        }
        for target, names in aliases.items():
            for name in names:
                if name in raw and raw.get(name) is not None:
                    normalized[target] = raw.get(name)
                    has_metrics = True
                    break
        for field in ("current_dead", "skills", "equipment_snapshot", "guild_counters_authoritative"):
            if field in raw and raw.get(field) is not None:
                normalized[field] = deepcopy(raw.get(field))
        raw_metrics_scope = str(raw.get("metrics_scope") or "").strip().casefold()
        raw_skills_scope = str(raw.get("skills_scope") or "").strip().casefold()
        if raw_metrics_scope in PVP_DATA_SCOPES:
            normalized["metrics_scope"] = raw_metrics_scope
        elif source_scope in PVP_DATA_SCOPES and has_metrics:
            normalized["metrics_scope"] = source_scope
        if raw_skills_scope in PVP_DATA_SCOPES:
            normalized["skills_scope"] = raw_skills_scope
        elif (
            source_scope in PVP_DATA_SCOPES
            and isinstance(raw.get("skills"), list)
            and raw.get("skills")
        ):
            normalized["skills_scope"] = source_scope
        if raw.get("statistics_authoritative") is True or source_scope == "authoritative":
            normalized["statistics_authoritative"] = True
        index = index_by_token.get(token)
        if index is None:
            normalized.setdefault("is_ai", False)
            normalized.setdefault("is_self", False)
            result.append(normalized)
            index_by_token[token] = len(result) - 1
        else:
            merged = result[index]
            for key, value in normalized.items():
                if value is not None and value != "未知玩家":
                    merged[key] = value
    return result


class PvpHistoryRepository:
    """Every finalized match is committed before any upload is attempted."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS pvp_local_records (
                    match_id TEXT PRIMARY KEY, account_key TEXT NOT NULL,
                    character_id TEXT NOT NULL, started_ns INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    upload_state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                    uploaded_at REAL, server_match_id TEXT NOT NULL DEFAULT '',
                    favorite INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_pvp_local_outbox
                    ON pvp_local_records(account_key, upload_state, retry_at);
                CREATE TABLE IF NOT EXISTS pvp_equipment_snapshot_outbox (
                    snapshot_id TEXT PRIMARY KEY, account_key TEXT NOT NULL,
                    target_character_id TEXT NOT NULL, captured_at_ns INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    upload_state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                    uploaded_at REAL, server_snapshot_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pvp_equipment_snapshot_outbox
                    ON pvp_equipment_snapshot_outbox(
                        account_key, upload_state, retry_at, captured_at_ns
                    );
                CREATE INDEX IF NOT EXISTS idx_pvp_equipment_snapshot_lookup
                    ON pvp_equipment_snapshot_outbox(
                        account_key, target_character_id, captured_at_ns DESC
                    );
                CREATE TABLE IF NOT EXISTS pvp_active_checkpoint (
                    account_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL
                );
            """)
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(pvp_local_records)"
                )
            }
            if "favorite" not in columns:
                connection.execute(
                    "ALTER TABLE pvp_local_records "
                    "ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
                )

    def connect(self, *, timeout=5):
        connection = sqlite3.connect(self.path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def session(self, *, timeout=5):
        """Commit/rollback and always release the Windows file handle."""

        connection = self.connect(timeout=timeout)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, account_key, payload, *, upload_state='pending'):
        if not account_key:
            raise ValueError("PVP history requires a signed-in account")
        if upload_state not in {'pending', 'local_only'}:
            raise ValueError("Invalid initial PVP upload state")
        text = canonical_json(payload)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        retry_at = (
            time.time() + TEAM_EQUIPMENT_UPLOAD_GRACE_SECONDS
            if upload_state == 'pending'
            and _team_equipment_snapshots_pending(payload)
            else 0
        )
        with self.session() as connection:
            existing = connection.execute("SELECT payload_digest,account_key FROM pvp_local_records WHERE match_id=?",
                                          (payload["match_id"],)).fetchone()
            if existing and existing[1] != account_key:
                raise ValueError("A PVP match cannot change its signed-in owner")
            if existing and existing[0] != digest:
                raise ValueError("A finalized PVP match is immutable")
            connection.execute("""INSERT OR IGNORE INTO pvp_local_records
                (match_id, account_key, character_id, started_ns, payload_json,
                 payload_digest, upload_state, retry_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (payload["match_id"], account_key, payload["player"]["character_id"],
                 payload["started_at_ns"], text, digest, upload_state, retry_at))
            connection.execute("DELETE FROM pvp_active_checkpoint WHERE account_key=?", (account_key,))

    def checkpoint(self, account_key, payload):
        with self.session() as connection:
            connection.execute("INSERT OR REPLACE INTO pvp_active_checkpoint VALUES (?, ?)",
                               (account_key, canonical_json(payload)))

    def recover(self, account_key):
        with self.session() as connection:
            row = connection.execute("SELECT payload_json FROM pvp_active_checkpoint WHERE account_key=?",
                                     (account_key,)).fetchone()
        if row:
            payload = json.loads(row[0])
            # Live checkpoints omit bulky equipment bodies to keep the Tk
            # thread responsive. Each successful shape reply is durably saved
            # in the separate outbox; restore same-match snapshots once on
            # crash recovery before saving the interrupted record.
            started_ns = int(payload.get('started_at_ns', 0) or 0)
            ended_ns = int(payload.get('ended_at_ns', 0) or 0)
            recovered_equipment = {}
            members = [payload.get('player', {})]
            members.extend(
                member
                for collection in ('opponents', 'allies', 'enemies')
                for member in (payload.get(collection) or ())
            )
            for member in members:
                if not isinstance(member, dict) or member.get('equipment_snapshot'):
                    continue
                token = str(member.get('character_id') or '').strip()
                if not token:
                    continue
                if token not in recovered_equipment:
                    snapshot = self.equipment_snapshot_at_or_before(
                        account_key, token, ended_ns,
                        extraordinary_rating=member.get('extraordinary_rating'),
                    )
                    captured_ns = (
                        int(snapshot.get('captured_at_ns', 0) or 0)
                        if isinstance(snapshot, dict) else 0
                    )
                    recovered_equipment[token] = (
                        snapshot if captured_ns >= started_ns else None
                    )
                snapshot = recovered_equipment[token]
                if snapshot is not None:
                    member['equipment_snapshot'] = deepcopy(snapshot)
                    if member.get('extraordinary_rating') is None:
                        member['extraordinary_rating'] = snapshot.get('extraordinary_rating')
            # Never include time after the last durable observation in a crash.
            completed_duel = bool(
                payload.get('match_kind') == 'duel'
                and payload.get('result') in {'胜利', '失败'}
            )
            if not completed_duel:
                payload["result"] = "未知"
                payload["end_reason"] = "interrupted"
            payload["capture_complete"] = False
            self.save(account_key, payload, upload_state='pending')
            return payload
        return None

    def list(self, account_key):
        with self.session() as connection:
            rows = connection.execute("SELECT payload_json,favorite FROM pvp_local_records WHERE account_key=? ORDER BY started_ns DESC",
                                      (account_key,)).fetchall()
        result = []
        for row in rows:
            payload = project_duel_result_counters(
                json.loads(row[0]), copy_payload=False
            )
            if bool(row[1]):
                payload["favorite"] = True
            result.append(payload)
        return result

    def set_favorite(self, account_key, match_id, favorite):
        with self.session() as connection:
            cursor = connection.execute(
                "UPDATE pvp_local_records SET favorite=? "
                "WHERE account_key=? AND match_id=?",
                (1 if favorite else 0, account_key, str(match_id)),
            )
        if cursor.rowcount <= 0:
            return None
        return next(
            (
                row
                for row in self.list(account_key)
                if str(row.get("match_id") or "") == str(match_id)
            ),
            None,
        )

    def attach_equipment_snapshot(
        self, account_key, match_id, character_id, snapshot
    ):
        """Attach a late same-match equipment reply without changing counters."""

        return self.attach_equipment_snapshots(
            account_key, match_id, {str(character_id): snapshot}
        )

    def attach_equipment_snapshots(self, account_key, match_id, snapshots):
        """Apply a response batch to one pending match in one transaction."""

        snapshots = {
            str(character_id): snapshot
            for character_id, snapshot in snapshots.items()
            if str(character_id).strip() and isinstance(snapshot, dict)
        }
        if not snapshots:
            return False

        with self.session() as connection:
            row = connection.execute(
                "SELECT payload_json,account_key,upload_state,retry_at FROM "
                "pvp_local_records WHERE match_id=?",
                (str(match_id),),
            ).fetchone()
            if row is None or row[1] != account_key or row[2] == 'uploaded':
                return False
            payload = json.loads(row[0])
            changed = False
            for collection in ('allies', 'enemies'):
                rows = payload.get(collection)
                if not isinstance(rows, list):
                    continue
                for member in rows:
                    character_id = (
                        str(member.get('character_id') or '').strip()
                        if isinstance(member, dict) else ''
                    )
                    snapshot = snapshots.get(character_id)
                    if (
                        snapshot is not None
                        and member.get('equipment_snapshot') != snapshot
                    ):
                        member['equipment_snapshot'] = deepcopy(snapshot)
                        changed = True
                    rating = snapshot.get('extraordinary_rating') if snapshot else None
                    if rating is not None and member.get('extraordinary_rating') is None:
                        member['extraordinary_rating'] = rating
                        changed = True
            for opponent in payload.get('opponents', ()):
                if not isinstance(opponent, dict):
                    continue
                snapshot = snapshots.get(str(opponent.get('character_id') or '').strip())
                if snapshot is None:
                    continue
                if opponent.get('equipment_snapshot') != snapshot:
                    opponent['equipment_snapshot'] = deepcopy(snapshot)
                    changed = True
                rating = snapshot.get('extraordinary_rating')
                if rating is not None and opponent.get('extraordinary_rating') is None:
                    opponent['extraordinary_rating'] = rating
                    changed = True
            # ``teams`` is the canonical immutable copy used by the detail
            # page and by the upload payload.  Keep it in lockstep with the
            # legacy top-level arrays when a response arrives after the
            # match was finalized; otherwise local history would show the
            # snapshot while the server upload still contained ``null``.
            teams = payload.get('teams')
            if isinstance(teams, dict):
                for collection in ('allies', 'enemies'):
                    rows = teams.get(collection)
                    if not isinstance(rows, list):
                        continue
                    for member in rows:
                        character_id = (
                            str(member.get('character_id') or '').strip()
                            if isinstance(member, dict) else ''
                        )
                        snapshot = snapshots.get(character_id)
                        if (
                            snapshot is not None
                            and member.get('equipment_snapshot') != snapshot
                        ):
                            member['equipment_snapshot'] = deepcopy(snapshot)
                            changed = True
                        rating = snapshot.get('extraordinary_rating') if snapshot else None
                        if rating is not None and member.get('extraordinary_rating') is None:
                            member['extraordinary_rating'] = rating
                            changed = True
            player = payload.get('player')
            player_snapshot = (
                snapshots.get(str(player.get('character_id') or '').strip())
                if isinstance(player, dict) else None
            )
            if (
                player_snapshot is not None
                and player.get('equipment_snapshot') != player_snapshot
            ):
                player['equipment_snapshot'] = deepcopy(player_snapshot)
                changed = True
            if player_snapshot is not None and player.get('extraordinary_rating') is None:
                rating = player_snapshot.get('extraordinary_rating')
                if rating is not None:
                    player['extraordinary_rating'] = rating
                    changed = True
            if not changed:
                return False
            text = canonical_json(payload)
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            # A pending record has not left this durable outbox yet, so its
            # snapshot can be completed. Never mutate an acknowledged upload.
            retry_at = (
                float(row[3] or 0)
                if _team_equipment_snapshots_pending(payload)
                else 0
            )
            connection.execute(
                "UPDATE pvp_local_records SET payload_json=?,payload_digest=?,"
                "upload_state='pending',attempts=0,retry_at=?,last_error='' "
                "WHERE match_id=? AND account_key=?",
                (text, digest, retry_at, str(match_id), account_key),
            )
        return True

    def due(self, account_key, now=None):
        with self.session() as connection:
            row = connection.execute("""SELECT * FROM pvp_local_records WHERE account_key=?
                AND upload_state IN ('pending','failed') AND retry_at<=?
                ORDER BY started_ns LIMIT 1""", (account_key, time.time() if now is None else now)).fetchone()
        return dict(row) if row else None

    def acknowledge(self, match_id, server_match_id):
        if not server_match_id or match_id != server_match_id:
            raise ValueError("Upload acknowledgement does not identify this match")
        with self.session() as connection:
            connection.execute("""UPDATE pvp_local_records SET upload_state='uploaded', uploaded_at=?,
                server_match_id=?, last_error='' WHERE match_id=?""", (time.time(), server_match_id, match_id))

    def fail(self, match_id, error, now=None):
        now = time.time() if now is None else now
        with self.session() as connection:
            row = connection.execute("SELECT attempts FROM pvp_local_records WHERE match_id=?", (match_id,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            delay = min(300, 2 ** min(attempts, 8))
            connection.execute("""UPDATE pvp_local_records SET upload_state='failed', attempts=?,
                retry_at=?, last_error=? WHERE match_id=? AND upload_state!='uploaded'""",
                (attempts, now + delay, str(error)[:512], match_id))

    def save_equipment_snapshots(self, entries):
        """Commit one response batch with a single SQLite transaction."""

        prepared = []
        for account_key, payload in entries:
            if not account_key:
                raise ValueError("Equipment snapshot requires a signed-in account")
            snapshot_id = str(payload.get("snapshot_id", "") or "").strip()
            target_character_id = str(
                payload.get("target_character_id", "") or ""
            ).strip()
            try:
                captured_at_ns = int(payload.get("captured_at_ns", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                captured_at_ns = 0
            if not snapshot_id or not target_character_id or captured_at_ns <= 0:
                raise ValueError("Equipment snapshot upload is incomplete")
            text = canonical_json(payload)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            prepared.append((
                snapshot_id, account_key, target_character_id,
                captured_at_ns, text, digest, time.time(),
            ))
        if not prepared:
            return []
        with self.session() as connection:
            for row in prepared:
                existing = connection.execute(
                    "SELECT payload_digest,account_key FROM "
                    "pvp_equipment_snapshot_outbox WHERE snapshot_id=?",
                    (row[0],),
                ).fetchone()
                if existing and existing[1] != row[1]:
                    raise ValueError("An equipment snapshot cannot change its owner")
                if existing and existing[0] != row[5]:
                    raise ValueError("An equipment snapshot is immutable")
                connection.execute(
                    """INSERT OR IGNORE INTO pvp_equipment_snapshot_outbox
                       (snapshot_id,account_key,target_character_id,captured_at_ns,
                        payload_json,payload_digest,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    row,
                )
        return [row[0] for row in prepared]

    def save_equipment_snapshot(self, account_key, payload):
        """Persist a successful active query before network upload begins."""

        return self.save_equipment_snapshots(((account_key, payload),))[0]

    def equipment_snapshot_at_or_before(
        self,
        account_key,
        target_character_id,
        captured_at_ns,
        *,
        extraordinary_rating=None,
    ):
        """Return the newest exact-character snapshot valid at a battle end."""

        account_key = str(account_key or "").strip()
        target_character_id = str(target_character_id or "").strip()
        try:
            captured_at_ns = int(captured_at_ns or 0)
        except (TypeError, ValueError, OverflowError):
            captured_at_ns = 0
        try:
            expected_rating = int(extraordinary_rating or 0) or None
        except (TypeError, ValueError, OverflowError):
            expected_rating = None
        if not account_key or not target_character_id or captured_at_ns <= 0:
            return None
        try:
            # This optional PVE-history enrichment runs on the Tk thread.
            # A busy writer must not freeze the whole window for SQLite's
            # ordinary five-second timeout (once per former teammate).
            with self.session(timeout=0) as connection:
                rows = connection.execute(
                    """SELECT payload_json FROM pvp_equipment_snapshot_outbox
                       WHERE account_key=? AND target_character_id=?
                       AND captured_at_ns<=?
                       ORDER BY captured_at_ns DESC LIMIT 64""",
                    (account_key, target_character_id, captured_at_ns),
                ).fetchall()
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                return None
            raise
        for row in rows:
            try:
                payload = json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            snapshot = payload.get("equipment_snapshot")
            if not isinstance(snapshot, dict) or not isinstance(
                snapshot.get("equipment"), list
            ) or not snapshot.get("equipment"):
                continue
            if expected_rating is not None:
                try:
                    snapshot_rating = int(
                        payload.get(
                            "extraordinary_rating",
                            snapshot.get("extraordinary_rating", 0),
                        )
                        or 0
                    ) or None
                except (TypeError, ValueError, OverflowError):
                    snapshot_rating = None
                if snapshot_rating != expected_rating:
                    continue
            return deepcopy(snapshot)
        return None

    def due_equipment_snapshot(self, account_key, now=None):
        with self.session() as connection:
            row = connection.execute(
                """SELECT * FROM pvp_equipment_snapshot_outbox
                   WHERE account_key=? AND upload_state IN ('pending','failed')
                   AND retry_at<=? ORDER BY captured_at_ns LIMIT 1""",
                (account_key, time.time() if now is None else now),
            ).fetchone()
        return dict(row) if row else None

    def acknowledge_equipment_snapshot(self, snapshot_id, server_snapshot_id):
        if not server_snapshot_id or snapshot_id != server_snapshot_id:
            raise ValueError("Upload acknowledgement does not identify this snapshot")
        with self.session() as connection:
            connection.execute(
                """UPDATE pvp_equipment_snapshot_outbox
                   SET upload_state='uploaded',uploaded_at=?,server_snapshot_id=?,
                       last_error=''
                   WHERE snapshot_id=?""",
                (time.time(), server_snapshot_id, snapshot_id),
            )

    def fail_equipment_snapshot(self, snapshot_id, error, now=None):
        now = time.time() if now is None else now
        with self.session() as connection:
            row = connection.execute(
                "SELECT attempts FROM pvp_equipment_snapshot_outbox "
                "WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            delay = min(300, 2 ** min(attempts, 8))
            connection.execute(
                """UPDATE pvp_equipment_snapshot_outbox
                   SET upload_state='failed',attempts=?,retry_at=?,last_error=?
                   WHERE snapshot_id=? AND upload_state!='uploaded'""",
                (attempts, now + delay, str(error)[:512], snapshot_id),
            )


class PvpRecordingController:
    def __init__(self, repository):
        self.repository = repository
        self.account_key = ""
        self.map_id = None
        self.instance_id = None
        self.instance_confirmed = False
        self.current = PvpTracker()
        self.recording = None
        self.suspended_recording = None
        self.suspended_left_at_ns = 0
        self.suspended_deadline_ns = 0
        self.duel_recording = None
        # The completed tracker remains the HUD source until a genuinely new
        # match starts.  Scene exit and result packets therefore do not blank
        # the window, while the next match gets a fresh tracker and counters.
        self.presented_tracker = self.current
        self.presented_match_id = None
        self.duel_finalize_deadline_ns = 0
        self.duel_finalized_session_id = None
        self.last_checkpoint_ns = 0
        self.last_finalized = None
        self.map_entered_at_ns = 0
        self.last_boundary_ns = 0
        self.last_observation_ns = 0
        self.match_context = {}
        self.awaiting_next_match = False
        self.pending_observations = deque(maxlen=512)
        self.party_tokens = set()
        self.party_session_id = 0
        self.party_profiles = {}
        # Party updates are a live UI boundary, not a history boundary.  Keep
        # this separate from the frozen tracker so a leave event can hide the
        # old roster immediately without deleting the completed match.
        self.party_seen = False
        self.party_active = False
        # A finalized team result can be followed by equipment replies that
        # were requested while the match was active. Keep only its identity so
        # those immutable snapshots can be attached without reopening combat.
        self.finalized_equipment_match_id = None
        self.finalized_equipment_tokens = set()
        self.finalized_equipment_deadline_ns = 0
        self.equipment_attach = None
        self._snapshot_cache_key = None
        self._snapshot_cache_state = None

    @property
    def active(self):
        return self.recording is not None

    @property
    def display_tracker(self):
        """Return the tracker for the map currently being recorded.

        ``current`` keeps enough identity/profile context to start a match when
        role discovery arrives after the map entry packet.  Match counters,
        however, live in the isolated tracker frozen into ``recording``.  The
        HUD must use that tracker or a direct PVP-to-PVP map switch can briefly
        show counters from the map that just ended.
        """

        if self.recording is not None:
            return self.recording["tracker"]
        if self.duel_recording is not None:
            return self.duel_recording["tracker"]
        return self.presented_tracker or self.current

    @property
    def can_start(self):
        return bool(self.account_key and self.map_id in RECORDABLE_MAPS
                    and self.current.self_token and not self.active
                    and self.suspended_recording is None
                    and not self.awaiting_next_match)

    def equipment_query_active(self, now_ns=None):
        """Keep one match-scoped equipment context through its reply grace."""

        if self.recording is not None or self.duel_recording is not None:
            return True
        now_ns = time.time_ns() if now_ns is None else int(now_ns)
        return bool(
            self.finalized_equipment_match_id
            and self.finalized_equipment_tokens
            and now_ns <= self.finalized_equipment_deadline_ns
        )

    def snapshot(self, now_ns=None):
        now_ns = time.time_ns() if now_ns is None else now_ns
        tracker = self.display_tracker
        recording = self.recording or self.suspended_recording or self.duel_recording
        started_ns = int(recording.get("started_at_ns", 0) or 0) if recording else 0
        if self.map_id == 5_200_021 and recording:
            started_ns = int(recording.get("battle_started_at_ns", 0) or 0)
        elapsed_second = max(0, int(now_ns - started_ns) // 1_000_000_000) if started_ns else 0
        cache_key = (
            id(tracker),
            int(getattr(tracker, "generation", 0) or 0),
            self.map_id,
            self.presented_match_id,
            bool(self.active),
            elapsed_second,
            self.map_id not in HUNTER_CITY_MAPS,
        )
        if cache_key == self._snapshot_cache_key and self._snapshot_cache_state is not None:
            return dict(self._snapshot_cache_state)
        state = tracker.session_snapshot(
            now_ns, include_details=False,
            include_teams=self.map_id not in HUNTER_CITY_MAPS,
        )
        metadata = RECORDABLE_MAPS.get(self.map_id, {})
        if metadata:
            # The live HUD identifies the rule set. The physical scene name is
            # retained in the immutable record/settings page, not duplicated
            # in the compact top-left match bar.
            state['map_name'] = metadata['mode_name']
            state['module_name'] = metadata['mode_name']
            state['team_size'] = int(metadata.get('team_size', 0) or 0)
        if self.presented_match_id:
            state['session_id'] = self.presented_match_id
        if self.active:
            timer_started_ns = int(
                self.recording.get("battle_started_at_ns", 0) or 0
            ) if self.map_id == 5_200_021 else int(
                self.recording["started_at_ns"]
            )
            elapsed = max(0, now_ns - timer_started_ns) if timer_started_ns else 0
            seconds = elapsed // 1_000_000_000
            state.update(
                active=True,
                time=f"{seconds // 60:02d}:{seconds % 60:02d}",
                session_id=self.recording["match_id"],
                result="进行中",
                status="已进入 PVP 地图，正在自动统计",
            )
        self._snapshot_cache_key = cache_key
        self._snapshot_cache_state = state
        return dict(state)

    def bind_account(self, account_key):
        if account_key == self.account_key:
            return
        if self.account_key:
            self.reset_context(reason="account_changed")
        self.account_key = account_key
        if account_key:
            self.repository.recover(account_key)

    def _rotate_current_scene(self, map_id=None, instance_id=None):
        """Clear match-scoped actors while retaining the confirmed local role."""

        self.current.rotate_scene(map_id, instance_id)
        self.pending_observations.clear()
        if map_id in RECORDABLE_MAPS:
            # Entry must replace the retained result even when local-role
            # identity arrives slightly later than the Space creation.
            self.presented_tracker = self.current
            self.presented_match_id = ''

    @staticmethod
    def _match_context(value, *, allow_battle_alias=False):
        if not isinstance(value, dict):
            return {}
        result = {}
        for field in PVP_CONTEXT_FIELDS:
            item = value.get(field, value.get('pvp_' + field))
            if item not in (None, ''):
                result[field] = str(item)[:128]
        if allow_battle_alias and 'source_battle_id' not in result:
            item = value.get('battle_id')
            if item not in (None, ''):
                result['source_battle_id'] = str(item)[:128]
        try:
            mode_id = int(value.get('mode_id', value.get('pvp_mode_id', 0)) or 0)
        except (TypeError, ValueError, OverflowError):
            mode_id = 0
        if 0 < mode_id <= 99_999_999:
            result['mode_id'] = mode_id
        for field in ('teams', 'team_stats', 'team_participants'):
            candidate = value.get(field)
            if isinstance(candidate, (dict, list)):
                # Keep the optional settlement detached from the parser's
                # mutable object; the payload writer will validate only the
                # participant fields it actually consumes.
                result[field] = deepcopy(candidate)
        outcome = value.get('result')
        if outcome in RESULTS:
            result['result'] = outcome
        if value.get('finished') is True or value.get('match_finished') is True:
            result['finished'] = True
        return result

    @staticmethod
    def _context_identity_changed(recording, incoming):
        map_id = recording.get('map_id')
        if map_id in CHAMPION_MAPS:
            fields_to_compare = ('round_id', 'source_battle_id')
        elif map_id in CLUB_EVENT_MAPS:
            fields_to_compare = ('event_id', 'war_id', 'source_battle_id')
        elif map_id in LARGE_BATTLE_MAPS:
            fields_to_compare = ('event_id', 'source_battle_id')
        else:
            fields_to_compare = ('source_battle_id',)
        if (recording.get('mode_id') and incoming.get('mode_id')
                and int(recording['mode_id']) != int(incoming['mode_id'])):
            return True
        return any(
            recording.get(field) not in (None, '')
            and incoming.get(field) not in (None, '')
            and str(recording[field]) != str(incoming[field])
            for field in fields_to_compare
        )

    @staticmethod
    def _continuity_fields(map_id):
        if map_id in CLUB_EVENT_MAPS:
            return ('event_id', 'war_id', 'source_battle_id')
        if map_id in LARGE_BATTLE_MAPS:
            return ('event_id', 'source_battle_id')
        return ()

    def _suspended_continuity(self, map_id, instance_id, context, stamp):
        recording = self.suspended_recording
        if recording is None or recording.get('map_id') != map_id:
            return False
        compared = False
        for field in self._continuity_fields(map_id):
            old, new = recording.get(field), context.get(field)
            if old not in (None, '') and new not in (None, ''):
                compared = True
                if str(old) != str(new):
                    return False
        if compared:
            return True
        old_instance = str(recording.get('instance_id') or '')
        new_instance = str(instance_id or '')
        if old_instance and new_instance:
            return old_instance == new_instance
        return bool(stamp <= self.suspended_deadline_ns)

    def _suspend(self, stamp):
        if not self.active:
            return False
        recording = self.recording
        recording['tracker']._pause(stamp, '暂时离开 PVP 场景')
        self.repository.checkpoint(
            self.account_key,
            self.record_payload(
                stamp, 'temporarily_left', recording=recording,
                include_equipment=False,
            ),
        )
        self.recording = None
        self.suspended_recording = recording
        self.suspended_left_at_ns = stamp
        self.suspended_deadline_ns = stamp + RESUMABLE_LEAVE_GRACE_NS
        self.presented_tracker = recording['tracker']
        self.presented_match_id = recording['match_id']
        return True

    def _resume_or_finalize_suspended(self, stamp):
        recording = self.suspended_recording
        if recording is None or self.map_id not in RECORDABLE_MAPS:
            return None
        context = dict(self.match_context)
        if self._suspended_continuity(self.map_id, self.instance_id, context, stamp):
            old_instance = str(recording.get('instance_id') or '')
            strong_identity_observed = any(
                recording.get(field) not in (None, '')
                and context.get(field) not in (None, '')
                for field in self._continuity_fields(self.map_id)
            )
            provisional = bool(
                old_instance and not self.instance_id and not strong_identity_observed
            )
            self.recording = recording
            self.recording['_provisional_resume'] = provisional
            self.recording['_resume_started_at_ns'] = stamp
            self.recording['_previous_left_at_ns'] = self.suspended_left_at_ns
            self.suspended_recording = None
            self.suspended_left_at_ns = 0
            self.suspended_deadline_ns = 0
            if self.instance_id and not self.recording.get('instance_id'):
                self.recording['instance_id'] = str(self.instance_id)
            self.recording.update({key: item for key, item in context.items()
                                   if key in PVP_RECORDING_CONTEXT_FIELDS})
            self.presented_tracker = self.recording['tracker']
            self.presented_match_id = self.recording['match_id']
            return None
        return self._finalize_suspended(reason='next_activity')

    @staticmethod
    def _timestamp_ns(value, fallback=None):
        if isinstance(value, dict):
            for key in ("capture_timestamp_ns", "timestamp_ns"):
                try:
                    stamp = int(value.get(key, 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    stamp = 0
                if stamp > 0:
                    return stamp
            try:
                filetime = int(value.get("filetime_100ns", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                filetime = 0
            if filetime > 116_444_736_000_000_000:
                return (filetime - 116_444_736_000_000_000) * 100
        return time.time_ns() if fallback is None else int(fallback)

    def ingest_scene(self, value):
        """Apply the authoritative map boundary emitted by the parser.

        Entering one of ``RECORDABLE_MAPS`` is the automatic start boundary.
        Team arenas end on exit; large/club activities are suspended briefly
        so a same event/war/instance re-entry continues the original record.
        A direct PVP A -> PVP B switch always finalizes A before B is created.
        """

        if not isinstance(value, dict):
            return None
        stamp = self._timestamp_ns(value)
        if stamp < self.last_boundary_ns:
            return None
        self.last_observation_ns = max(self.last_observation_ns, stamp)
        try:
            scene_id = int(value.get("scene_id", value.get("map_id", 0)) or 0)
        except (TypeError, ValueError, OverflowError):
            scene_id = 0
        transition = bool(value.get("transition")) or scene_id <= 0
        scene_context = self._match_context(value, allow_battle_alias=True)
        previous_map = self.map_id
        if (
            self.map_entered_at_ns
            and stamp < self.map_entered_at_ns
            and scene_id != previous_map
        ):
            return None
        finalized = None
        if self.duel_recording is not None and (transition or scene_id != previous_map):
            finalized = self._stop_duel(stamp, 'left_map')
        if self.active and (
            transition
            or scene_id != previous_map
            or scene_id not in RECORDABLE_MAPS
        ):
            if previous_map in RESUMABLE_PVP_MAPS and (
                transition or scene_id not in RECORDABLE_MAPS
            ):
                self._suspend(stamp)
            else:
                finalized = self.stop(stamp, reason="left_map")
        if transition:
            self.last_boundary_ns = max(self.last_boundary_ns, stamp)
            self.map_id = None
            self.instance_id = None
            self.instance_confirmed = False
            self.map_entered_at_ns = 0
            self.match_context = {}
            self.awaiting_next_match = False
            self._rotate_current_scene()
            return finalized

        if scene_id != previous_map:
            self.last_boundary_ns = max(self.last_boundary_ns, stamp)
            self.map_id = scene_id
            self.instance_id = None
            self.instance_confirmed = False
            self.map_entered_at_ns = stamp
            self.match_context = scene_context
            self.awaiting_next_match = False
            self._rotate_current_scene(scene_id)
            self.current.team_size = int(
                RECORDABLE_MAPS.get(scene_id, {}).get('team_size', 0) or 0
            )
        elif self.map_id is None:
            self.map_id = scene_id
            self.map_entered_at_ns = stamp
            self.current.map_id = scene_id
            self.current.team_size = int(
                RECORDABLE_MAPS.get(scene_id, {}).get('team_size', 0) or 0
            )
        if scene_context:
            self.match_context.update(scene_context)
            if self.active:
                self.recording.update({key: item for key, item in scene_context.items()
                                       if key in PVP_RECORDING_CONTEXT_FIELDS})
        suspended_finalized = self._resume_or_finalize_suspended(stamp)
        if suspended_finalized is not None:
            finalized = suspended_finalized
        if self.can_start:
            self.start(self.map_entered_at_ns or stamp)
        return finalized

    def ingest_update(self, kind, value):
        if not isinstance(value, dict):
            return None
        finalized = None
        if kind == 'equipment_profile' and not self.active:
            self._attach_finalized_equipment_profile(value)
        if kind == 'party':
            try:
                session_id = int(value.get('party_session_id', 0) or 0)
            except (TypeError, ValueError, OverflowError):
                session_id = 0
            has_boundary = (
                'in_team' in value
                or 'left_team' in value
                or session_id > 0
            )
            in_team = bool(value.get('in_team')) and not bool(value.get('left_team'))
            if has_boundary:
                self.party_seen = True
                self.party_active = in_team
            tokens = {
                str(token).strip()
                for token in value.get('user_tokens', ())
                if str(token or '').strip()
            }
            if not in_team:
                self.party_tokens.clear()
                self.party_session_id = 0
                self.party_profiles.clear()
            else:
                self.party_tokens = tokens
                if self.current.self_token:
                    self.party_tokens.add(self.current.self_token)
                self.party_session_id = session_id
                self.party_profiles = {
                    token: {
                        **self.current.token_profiles.get(token, {}),
                        **self.current.profiles.get(self.current.tokens.get(token, 0), {}),
                    }
                    for token in self.party_tokens
                }
            if self.active:
                self.recording['ally_tokens'] = sorted(self.party_tokens)
                self.recording['party_session_id'] = self.party_session_id
                self.recording['ally_profiles'] = deepcopy(self.party_profiles)
                if in_team:
                    # A party update received after this map started is live
                    # arena evidence.  It may complete a partial TeamPVPInfo
                    # roster, while a party snapshot from before entry remains
                    # intentionally replaceable by the verified PVP roster.
                    self.recording['party_observed_in_match'] = True
            return None
        if kind == 'pvp_match_context':
            stamp = self._timestamp_ns(value, self.last_observation_ns or None)
            incoming = self._match_context(value, allow_battle_alias=True)
            if (self.awaiting_next_match and self.last_finalized is not None
                    and self._context_identity_changed(self.last_finalized, incoming)):
                self.awaiting_next_match = False
            if self.active and self._context_identity_changed(self.recording, incoming):
                finalized = self.stop(stamp, reason='match_identity_changed')
            self.match_context.update(incoming)
            if self.can_start:
                self.start(stamp)
            if self.active:
                self.recording.update({key: item for key, item in incoming.items()
                                       if key in PVP_RECORDING_CONTEXT_FIELDS})
                if incoming.get('result') in RESULTS:
                    self.recording['result'] = incoming['result']
                if incoming.get('finished') is True:
                    finalized = self.stop(stamp, reason='match_result')
            return finalized
        if kind in {"identity", "self_character"} and value.get("tentative") is not True and value.get("self_confirmed") is not False:
            token = value.get("character_id") if kind == "self_character" else None
            token = token or value.get("user_token", value.get("self_token"))
            if token and self.current.self_token and token != self.current.self_token:
                finalized = self.reset_context(
                    self._timestamp_ns(value, self.last_observation_ns or None),
                    reason="character_changed",
                )
        self.current.ingest_update(kind, value)
        if kind in {'profile', 'name', 'identity', 'self_character', 'equipment_profile'}:
            token = str(
                value.get('character_id')
                or value.get('user_token')
                or value.get('self_token')
                or ''
            ).strip()
            if token and token in self.party_tokens:
                actor = self.current.tokens.get(token, 0)
                profile = {
                    **self.current.token_profiles.get(token, {}),
                    **self.current.profiles.get(actor, {}),
                }
                self.party_profiles[token] = profile
                if self.active and token in self.recording.get('ally_tokens', ()):
                    self.recording.setdefault('ally_profiles', {})[token] = deepcopy(profile)
        if self.active:
            self.recording["tracker"].ingest_update(kind, value)
        elif self.presented_tracker is not self.current and kind in {
            'profile', 'name', 'equipment_profile', 'pvp_team_summary'
        }:
            # A late profile can improve the retained HUD row, but it must not
            # alter counters or revive a completed match.
            self.presented_tracker.ingest_update(kind, value)
        elif self.can_start:
            self.start(self.map_entered_at_ns or time.time_ns())
        return finalized

    def _attach_finalized_equipment_profile(self, value):
        token = str(value.get('user_token') or '').strip()
        snapshot = value.get('equipment_snapshot')
        match_id = str(self.finalized_equipment_match_id or '')
        if (
            not match_id
            or token not in self.finalized_equipment_tokens
            or not isinstance(snapshot, dict)
        ):
            return False
        attach = self.equipment_attach or self.repository.attach_equipment_snapshot
        return attach(self.account_key, match_id, token, snapshot)

    def observe(self, observation):
        if not isinstance(observation, dict):
            return None
        record = observation.get("record", {})
        context = observation.get("context", {})
        incoming_match_context = self._match_context(context)
        stamp = int(observation.get("timestamp_ns") or time.time_ns())
        if stamp < self.last_boundary_ns:
            return None
        self.last_observation_ns = max(self.last_observation_ns, stamp)
        if self.active and stamp < self.recording["started_at_ns"]:
            # Scoped parser work can be emitted after the next map is already
            # live. An old leave/damage/profile packet must not close or
            # contaminate the newly created match.
            return None
        if (self.duel_recording is not None
                and stamp < self.duel_recording['started_at_ns']):
            return None
        method = record.get("method")
        if method == 'RetGuildLeagueSettlementRecord':
            recording = self.recording or self.suspended_recording
            if recording is None or recording.get('map_id') != 5_200_223:
                return None
            tracker = recording['tracker']
            if context.get('self_token') and context['self_token'] != tracker.self_token:
                return None
            settlement = _guild_league_settlement(
                record.get('decoded_arguments'), tracker.self_token,
                recording['started_at_ns'], stamp,
            )
            if settlement is None:
                return None
            tracker._accept_team_settlement(settlement)
            if self.recording is not None:
                return self.stop(settlement['ended_at_ns'], reason='match_result')
            # Settlement can arrive after leaving the map, within the existing
            # suspended-match lifecycle. Preserve the official end boundary.
            self.suspended_left_at_ns = settlement['ended_at_ns']
            return self._finalize_suspended(settlement['ended_at_ns'], reason='match_result')
        new_token = context.get("self_token")
        new_map = context.get("map_id")
        new_instance = context.get("instance_id")
        transition = method in SCENE_TRANSITION_METHODS
        changed_role = bool(new_token and self.current.self_token and new_token != self.current.self_token)
        resumed_instance_conflict = bool(
            self.active and self.recording.get('_provisional_resume')
            and self.recording.get('instance_id') and new_instance
            and str(new_instance) != str(self.recording.get('instance_id'))
        )
        changed_space = bool(
            (new_instance and self.instance_confirmed and self.instance_id
             and new_instance != self.instance_id)
            or resumed_instance_conflict
        )
        changed_map = bool(new_map and self.map_id and new_map != self.map_id)
        changed_match = bool(
            self.active and self._context_identity_changed(
                self.recording, incoming_match_context
            )
        )
        if (self.active and self.map_id in HUNTER_CITY_MAPS
                and new_map == self.map_id and not transition
                and not changed_role and not changed_match):
            # The live client can recreate its Space entity while the player
            # remains in 猎龙之城. Its wire instance changes, but this is not
            # a map exit and must not erase the current match's HUD totals.
            changed_space = False
        next_match_evidence = bool(
            self.awaiting_next_match
            and (
                changed_space or changed_map
                or (self.last_finalized is not None
                    and self._context_identity_changed(
                        self.last_finalized, incoming_match_context
                    ))
            )
        )
        if next_match_evidence:
            self.awaiting_next_match = False
        provisional_instance_split = bool(
            self.active and changed_space
            and self.recording.get('_provisional_resume')
        )
        provisional_previous_left = (
            self.recording.get('_previous_left_at_ns', stamp)
            if provisional_instance_split else stamp
        )
        provisional_resume_start = (
            self.recording.get('_resume_started_at_ns', stamp)
            if provisional_instance_split else stamp
        )
        finalized = None
        if changed_role:
            finalized = self.reset_context(stamp, reason="character_changed")
        elif self.active and (transition or changed_space or changed_map or changed_match):
            if (self.recording.get('map_id') in RESUMABLE_PVP_MAPS
                    and transition and not changed_space and not changed_match):
                self._suspend(stamp)
            else:
                finalized = self.stop(
                    provisional_previous_left,
                    reason='match_identity_changed' if changed_match else 'left_map',
                )
            if changed_match or provisional_instance_split:
                self.map_entered_at_ns = provisional_resume_start
                self.pending_observations.clear()
        elif self.duel_recording is not None and (transition or changed_space or changed_map):
            finalized = self._stop_duel(stamp, 'left_map')
        if transition:
            self.last_boundary_ns = max(self.last_boundary_ns, stamp)
            self.map_id = self.instance_id = None
            self.map_entered_at_ns = 0
            self.match_context = {}
            self.awaiting_next_match = False
        else:
            previous_map = self.map_id
            self.map_id = new_map or self.map_id
            if new_instance:
                self.instance_id = new_instance
                self.instance_confirmed = True
            entered_new_map = bool(new_map and new_map != previous_map)
            if entered_new_map or changed_space:
                self.last_boundary_ns = max(self.last_boundary_ns, stamp)
                self.map_entered_at_ns = (
                    provisional_resume_start if provisional_instance_split else stamp
                )
                self._rotate_current_scene(self.map_id, self.instance_id)
            if incoming_match_context:
                self.match_context.update(incoming_match_context)
            suspended_finalized = self._resume_or_finalize_suspended(stamp)
            if suspended_finalized is not None:
                finalized = suspended_finalized
        args = record.get('decoded_arguments', [])
        local_role_control = bool(
            record.get('npcap_method_scope') == 'local_role'
            and str(method or '') in {
                'OnMsgSyncBattleType', 'OnMsgIndividualPVP',
                'RetIndividualPVP', 'OnMsgIndividualPVPResponse',
                'OnMsgIndividualPVPState', 'OnMsgIndividualPVPResult',
                'OnMsgIndividualPVPLeave', 'OnMsgAddFightRelationship',
                'OnMsgDelFightRelationship', 'OnMsgSyncTeamPVPInfo',
                'OnMsgTeamPVPSettlement', 'RetGetTeamArenaBattleInfo',
            }
            and self.current.self_id
        )
        local = (
            record.get('network_entity_id') == self.current.self_id
            or local_role_control
        )
        if record.get('npcap_recipient') and self.current.self_token:
            local = record['npcap_recipient'] == self.current.self_token
        if self.duel_recording is not None and local and method == 'OnMsgIndividualPVPState' and args:
            prepare_id = args[1] if len(args) > 1 else stamp
            if ((args[0] == 2 and prepare_id != self.current.duel_prepare_id)
                    or (args[0] == 3 and self.current.duel_finished
                        and prepare_id != self.current.duel_start_id)):
                finalized = self._stop_duel(stamp, 'next_duel')
        elif (self.duel_recording is not None and local and method == 'OnMsgIndividualPVPResponse'
              and args and args[0] is True and self.current.duel_finished):
            finalized = self._stop_duel(stamp, 'next_duel')
        self.current.consume(observation)
        if self.active:
            if stamp < self.recording["started_at_ns"]:
                return finalized  # deferred pre-start observations cannot enter this match
            self.recording["tracker"].consume(observation)
            # A verified league roster marks the actual PvP battle start on
            # every map that uses the multi-raid format.  This is deliberately
            # independent of the club-brawl map ID; other PvP modes can carry
            # the same roster with fewer occupied members.
            league = self.recording['tracker'].pvp_league_roster
            if league and not self.recording.get('battle_started_at_ns'):
                self.recording['battle_started_at_ns'] = stamp
                self.recording['tracker'].active_since_ns = None
                self.recording['tracker'].elapsed_ns = 0
                self.recording['tracker'].generation += 1
            settlement = observation.get('team_pvp_settlement')
            if isinstance(settlement, dict) and settlement.get('finished') is True:
                settled_rows = self.recording['tracker'].team_battle_rows()
                self.recording['teams'] = {
                    'allies': deepcopy(settled_rows.get('allies', [])),
                    'enemies': deepcopy(settled_rows.get('enemies', [])),
                }
                self.recording['result'] = settlement.get('result')
                # Team timestamps and the capture clock have a stable offset;
                # the packet arrival itself is the verified reveal boundary.
                finalized = self.stop(stamp, reason='match_result')
                return finalized
            if new_instance and not self.recording["instance_id"]:
                self.recording["instance_id"] = str(new_instance)
            if (new_instance and self.recording.get('_provisional_resume')
                    and str(new_instance) == str(self.recording.get('instance_id') or '')):
                self.recording.pop('_provisional_resume', None)
                self.recording.pop('_resume_started_at_ns', None)
                self.recording.pop('_previous_left_at_ns', None)
            if stamp - self.last_checkpoint_ns >= PVP_CHECKPOINT_INTERVAL_NS:
                self.checkpoint(stamp)
        elif self.can_start:
            self.pending_observations.append(deepcopy(observation))
            self.start(self.map_entered_at_ns or stamp)
        elif self.map_id in RECORDABLE_MAPS:
            self.pending_observations.append(deepcopy(observation))
        elif (self.account_key and self.current.self_token and self.current.duel
              and self.current.started_at_ns is not None
              and self.current.session_id != self.duel_finalized_session_id):
            if self.duel_recording is None:
                self.duel_recording = {
                    'match_id': 'pvp_' + uuid.uuid4().hex, 'match_kind': 'duel',
                    'started_at_ns': self.current.started_at_ns,
                    'map_id': self.map_id or 0, 'instance_id': str(self.instance_id or ''),
                    'tracker': self.current,
                }
                self.presented_tracker = self.current
                self.presented_match_id = self.duel_recording['match_id']
            if self.current.duel_finished and self.current.duel_outcome:
                if not self.duel_finalize_deadline_ns:
                    result_stamp = self.current.ended_at_ns or stamp
                    self.duel_finalize_deadline_ns = result_stamp + DUEL_FINALIZE_GRACE_NS
                self.checkpoint(stamp)
            elif stamp - self.last_checkpoint_ns >= PVP_CHECKPOINT_INTERVAL_NS:
                self.checkpoint(stamp)
        return finalized

    def start(self, now_ns=None):
        if not self.can_start:
            raise ValueError("请先识别当前角色并进入正式 PVP 地图。")
        now_ns = time.time_ns() if now_ns is None else now_ns
        self.awaiting_next_match = False
        self.finalized_equipment_match_id = None
        self.finalized_equipment_tokens.clear()
        self.finalized_equipment_deadline_ns = 0
        tracker = PvpTracker(map_scoped=True)
        tracker._set_identity(self.current.self_id, self.current.self_token)
        tracker.profiles = deepcopy(self.current.profiles)
        tracker.tokens = deepcopy(self.current.tokens)
        tracker.token_profiles = deepcopy(self.current.token_profiles)
        tracker.pvp_allies = deepcopy(self.current.pvp_allies)
        tracker.pvp_enemies = deepcopy(self.current.pvp_enemies)
        tracker.pvp_roster_members = deepcopy(self.current.pvp_roster_members)
        tracker.pvp_live_party_tokens = set(self.current.pvp_live_party_tokens)
        tracker.pvp_full_roster_teams = deepcopy(
            self.current.pvp_full_roster_teams
        )
        tracker.pvp_league_roster = deepcopy(self.current.pvp_league_roster)
        tracker.pvp_self_avatar_template_id = int(
            self.current.pvp_self_avatar_template_id or 0
        )
        tracker.team_size = int(
            RECORDABLE_MAPS.get(self.map_id, {}).get('team_size', 0) or 0
        )
        # Pre-entry party members remain optional history identities, but are
        # not live HUD rows until a new-map callback or PVP roster proves them.
        # Do not seed a new match with a snapshot obtained in an earlier match.
        for profile in tracker.profiles.values():
            profile.pop("equipment_snapshot", None)
        for profile in tracker.token_profiles.values():
            profile.pop("equipment_snapshot", None)
        for profile in (*tracker.pvp_allies.values(), *tracker.pvp_enemies.values()):
            profile.pop("equipment_snapshot", None)
        tracker.started_at_ns = now_ns
        tracker.map_id = self.map_id
        tracker.instance_id = self.instance_id
        self.recording = {"match_id": "pvp_" + uuid.uuid4().hex,
                          "started_at_ns": now_ns, "map_id": self.map_id,
                          "instance_id": str(self.instance_id or ""), "tracker": tracker,
                          "ally_tokens": sorted(self.party_tokens),
                          "ally_profiles": deepcopy(self.party_profiles),
                          "party_session_id": self.party_session_id,
                          "party_observed_in_match": False}
        if self.map_id == 5_200_021:
            tracker.active_since_ns = None
            tracker.elapsed_ns = 0
        self.recording.update({key: value for key, value in self.match_context.items()
                               if key in PVP_RECORDING_CONTEXT_FIELDS})
        self.presented_tracker = tracker
        self.presented_match_id = self.recording['match_id']
        for observation in self.pending_observations:
            if int(observation.get("timestamp_ns") or 0) >= now_ns:
                tracker.consume(observation)
        self.pending_observations.clear()
        self.checkpoint(now_ns)
        return self.recording["match_id"]

    def record_payload(self, end_ns, reason="left_map", *, recording=None,
                       include_equipment=True, include_roster=True,
                       include_skills=True):
        recording = recording or self.recording
        tracker = recording["tracker"]
        def equipment(profile):
            return deepcopy(profile.get("equipment_snapshot")) if include_equipment else None
        duel = recording.get('match_kind') == 'duel'
        start_ns = recording["started_at_ns"]
        end_ns = max(start_ns, end_ns)
        kills, defeats, deaths = tracker._death_counts()
        assists = tracker._assist_counts()
        outgoing_total = sum(row["damage"] for row in tracker.outgoing.values())
        death_contributions = {}
        for event in tracker.death_events:
            if event["victim_id"] == tracker.self_id:
                for key, value in event.get("incoming_contributions", {}).items():
                    if key.startswith("entity:"):
                        key = tracker._key(int(key.split(":", 1)[1]))
                    death_contributions[key] = death_contributions.get(key, 0) + value
        death_damage_total = sum(death_contributions.values())
        opponents = []
        keys = set(tracker.outgoing) | set(tracker.incoming) | set(kills) | set(defeats) | set(assists)
        for key in sorted(keys):
            actor = (tracker.outgoing.get(key, {}).get("actor_id") or
                     tracker.incoming.get(key, {}).get("actor_id") or tracker.tokens.get(key, 0))
            profile = {**tracker.token_profiles.get(key, {}), **tracker.profiles.get(actor, {})}
            damage = tracker.outgoing.get(key, {}).get("damage", 0)
            taken = tracker.incoming.get(key, {}).get("damage", 0) if tracker.has_exact_incoming else None
            timeline = []
            if include_skills:
                for event in tracker.death_events:
                    victim_key = tracker._key(event["victim_id"])
                    if victim_key == key and event["source_token"] == tracker.self_token:
                        timeline.append({"type": "kill", "timestamp_ns": event["timestamp_ns"]})
                    elif victim_key == key and event.get('assisted_by_self') is True:
                        timeline.append({"type": "assist", "timestamp_ns": event["timestamp_ns"]})
                    elif event["victim_id"] == tracker.self_id and event["source_token"] == key:
                        timeline.append({"type": "death", "timestamp_ns": event["timestamp_ns"]})
            if duel and key == tracker._duel_opponent_key() and tracker.duel_outcome == '胜利':
                event_type = 'kill'
                if not any(event['type'] == event_type for event in timeline):
                    timeline.append({'type': event_type, 'timestamp_ns': tracker.ended_at_ns or end_ns,
                                     'source': 'OnMsgIndividualPVPResult'})
            elif duel and key == tracker._duel_opponent_key() and tracker.duel_outcome == '失败':
                event_type = 'death'
                if not any(event['type'] == event_type for event in timeline):
                    timeline.append({'type': event_type, 'timestamp_ns': tracker.ended_at_ns or end_ns,
                                     'source': 'OnMsgIndividualPVPResult'})
            healing = (
                tracker.healing_by_actor[key]
                if key in tracker.healing_by_actor
                else None
            )
            opponent = {"opponent_id": key, "character_id": profile.get("user_token") or "",
                              "name": profile.get("name") or "未知玩家", "profession_id": profile.get("profession_id"),
                              "level": profile.get("level"),
                              "club_name": profile.get("club_name"), "extraordinary_rating": profile.get("extraordinary_rating"),
                              "avatar_id": profile.get("avatar_id"),
                              "avatar_frame_id": profile.get("avatar_frame_id"),
                              "is_ai": bool(profile.get("is_ai")),
                              "kills": kills.get(key, 0), "assists": 0 if duel else assists.get(key, 0), "defeats": defeats.get(key, 0),
                              "deaths": kills.get(key, 0),
                               "damage": damage, "healing": healing, "taken": taken,
                              "damage_share": damage / outgoing_total if outgoing_total else 0,
                              "death_taken_share": (death_contributions.get(key, 0) / death_damage_total
                                                    if death_damage_total else None),
                              "skills_outgoing": pair_skills(tracker.opponent_skills_outgoing.get(key, {})) if include_skills else [],
                               "skills_incoming": pair_skills(tracker.opponent_skills_incoming.get(key, {})) if include_skills else [],
                              "interaction_scope": "pair_exact",
                              "timeline": sorted(timeline, key=lambda row: row["timestamp_ns"]),
                              "last_interaction_ns": max(tracker.outgoing.get(key, {}).get("last_interaction_ns", 0),
                                                          tracker.incoming.get(key, {}).get("last_interaction_ns", 0)),
                              "equipment_snapshot": equipment(profile)}
            opponent.update(
                damage_to=opponent['damage'], damage_from=opponent['taken'],
                kills_on=opponent['kills'], deaths_to=opponent['defeats'],
            )
            opponents.append(opponent)
        player = tracker.profiles.get(tracker.self_id, {})
        metadata = ({'mode_name': '双人切磋', 'map_name': '双人切磋', 'mode_id': 0,
                     'match_kind': 'duel', 'upload_policy': 'automatic', 'team_size': 1} if duel
                    else dict(RECORDABLE_MAPS[recording['map_id']]))
        if not duel and recording.get('mode_id'):
            metadata['mode_id'] = int(recording['mode_id'])
        damage_taken = (sum(row["damage"] for row in tracker.incoming.values())
                        if tracker.has_exact_incoming else None)
        own_key = tracker._key(tracker.self_id) if tracker.self_id else ''
        own_exact_heal = own_key in tracker.healing_by_actor
        estimated_heal = (
            tracker.duel_healing_estimate() if duel and not own_exact_heal else None
        )
        own_healing = (
            tracker.healing_by_actor[own_key] if own_exact_heal
            else estimated_heal if duel else None
        )
        healing_source = (
            'network_exact' if own_exact_heal
            else 'hp_balance_estimate' if estimated_heal is not None
            else ''
        )
        started_at = iso_time(start_ns)
        ended_at = iso_time(end_ns)
        verified_pvp_allies = tracker.pvp_team_members() if include_roster else []
        verified_pvp_enemies = tracker.pvp_team_members(side='enemy') if include_roster else []
        verified_enemy_tokens = {
            str(row.get('user_token') or '').strip()
            for row in verified_pvp_enemies
            if str(row.get('user_token') or '').strip()
        }
        recorded_ally_tokens = {
            str(token).strip()
            for token in recording.get('ally_tokens', ())
            if str(token or '').strip()
        } if include_roster else set()
        if verified_pvp_allies:
            ally_tokens = {
                str(row.get('user_token') or '').strip()
                for row in verified_pvp_allies
                if str(row.get('user_token') or '').strip()
            }
            if recording.get('party_observed_in_match') is True:
                ally_tokens.update(recorded_ally_tokens - verified_enemy_tokens)
        else:
            ally_tokens = recorded_ally_tokens - verified_enemy_tokens
        if tracker.self_token:
            ally_tokens.add(tracker.self_token)
        allies = []
        saved_ally_profiles = recording.get('ally_profiles', {})
        saved_ally_profiles = saved_ally_profiles if isinstance(saved_ally_profiles, dict) else {}
        if verified_pvp_allies:
            saved_ally_profiles = {
                **saved_ally_profiles,
                **{
                    str(row.get('user_token') or '').strip(): row
                    for row in verified_pvp_allies
                    if str(row.get('user_token') or '').strip()
                },
            }
        for token in sorted(ally_tokens):
            actor = tracker.tokens.get(token, 0)
            ally_profile = {
                **saved_ally_profiles.get(token, {}),
                **tracker.token_profiles.get(token, {}),
                **tracker.profiles.get(actor, {}),
            }
            local_ally = token == tracker.self_token
            allies.append({
                "character_id": token,
                "name": ally_profile.get("name") or (player.get("name") if local_ally else "未知玩家"),
                "profession_id": ally_profile.get("profession_id"),
                "level": ally_profile.get("level"),
                "extraordinary_rating": ally_profile.get("extraordinary_rating"),
                "avatar_id": ally_profile.get("avatar_id"),
                "avatar_frame_id": ally_profile.get("avatar_frame_id"),
                "is_ai": bool(ally_profile.get("is_ai")),
                "is_self": local_ally,
                "kills": sum(kills.values()) if local_ally else None,
                "assists": (0 if duel else sum(assists.values())) if local_ally else None,
                "deaths": deaths if local_ally else None,
                "damage": outgoing_total if local_ally else None,
                "healing": (
                    own_healing if local_ally else None
                ),
                "healing_source": healing_source if local_ally else '',
                "taken": damage_taken if local_ally else None,
                "current_dead": bool(local_ally and tracker._key(tracker.self_id) in tracker.dead),
                "skills": pair_skills(tracker.skills_outgoing) if local_ally and include_skills else [],
                "equipment_snapshot": equipment(ally_profile),
                "metrics_scope": "self_exact" if local_ally else "identity_only",
                "skills_scope": "self_exact" if local_ally else "identity_only",
            })
        team_size = int(metadata.get('team_size', 0) or 0)
        enemies = []
        for opponent in opponents:
            character_id = str(opponent.get('character_id') or '').strip()
            if not character_id or character_id in ally_tokens:
                continue
            enemy = {
                "character_id": character_id,
                "name": opponent.get("name") or "未知玩家",
                "profession_id": opponent.get("profession_id"),
                "level": opponent.get("level"),
                "extraordinary_rating": opponent.get("extraordinary_rating"),
                "avatar_id": opponent.get("avatar_id"),
                "avatar_frame_id": opponent.get("avatar_frame_id"),
                "is_ai": bool(opponent.get("is_ai")),
                "kills": None,
                "assists": None,
                "deaths": None,
                "damage": None,
                "healing": None,
                "taken": None,
                "current_dead": False,
                "skills": deepcopy(opponent.get("skills_incoming") or []) if include_skills else [],
                "equipment_snapshot": equipment(opponent),
                "metrics_scope": "identity_only",
                "skills_scope": "identity_only",
            }
            if team_size == 1:
                enemy.update(
                    kills=opponent.get('defeats'),
                    assists=0,
                    deaths=opponent.get('kills'),
                    damage=opponent.get('taken'),
                    healing=opponent.get('healing'),
                    taken=opponent.get('damage'),
                    metrics_scope="pair_exact",
                    skills_scope="pair_exact",
                )
            enemies.append(enemy)
        # Preserve every verified non-party player profile even when that
        # player never exchanged a hit with the local character.  Their
        # counters remain unknown until a whole-team settlement is supplied,
        # but the roster/score snapshot can still identify them accurately.
        known_enemy_tokens = {
            str(row.get("character_id") or "").strip()
            for row in enemies
            if str(row.get("character_id") or "").strip()
        }
        profile_items = list(tracker.token_profiles.items()) if include_roster else []
        for _actor, profile in (tracker.profiles.items() if include_roster else ()):
            token = str(profile.get("user_token") or "").strip()
            if token and token not in {tracker.self_token, *ally_tokens}:
                profile_items.append((token, profile))
        for token, profile in profile_items:
            token = str(token or "").strip()
            if not token or token in {tracker.self_token, *ally_tokens} or token in known_enemy_tokens:
                continue
            if profile.get("entity_type") not in {"Player", "Avatar", "AvatarActor"} and not profile.get("is_ai"):
                continue
            enemies.append({
                "character_id": token,
                "name": profile.get("name") or "未知玩家",
                "profession_id": profile.get("profession_id"),
                "level": profile.get("level"),
                "extraordinary_rating": profile.get("extraordinary_rating"),
                "avatar_id": profile.get("avatar_id"),
                "avatar_frame_id": profile.get("avatar_frame_id"),
                "is_ai": bool(profile.get("is_ai")),
                "kills": None,
                "assists": None,
                "deaths": None,
                "damage": None,
                "healing": None,
                "taken": None,
                "current_dead": False,
                "skills": [],
                "equipment_snapshot": equipment(profile),
                "metrics_scope": "identity_only",
                "skills_scope": "identity_only",
            })
            known_enemy_tokens.add(token)
        # A protocol adapter may attach a verified whole-team settlement to
        # the tracker.  Overlay it only when present; the ordinary passive
        # path keeps teammate metrics as None rather than guessing totals.
        live_teams = (
            tracker.team_battle_rows(
                include_details=include_skills,
                include_equipment=include_equipment,
            )
            if include_roster else {'allies': [], 'enemies': []}
        )
        if live_teams.get("allies"):
            allies = _merge_team_settlement_rows(allies, live_teams["allies"])
        if live_teams.get("enemies"):
            enemies = _merge_team_settlement_rows(enemies, live_teams["enemies"])
        verified_allies, verified_enemies = (
            _optional_team_settlement(recording, tracker)
            if include_roster else ([], [])
        )
        if verified_allies:
            allies = _merge_team_settlement_rows(
                allies, verified_allies, source_scope="authoritative"
            )
        if verified_enemies:
            enemies = _merge_team_settlement_rows(
                enemies, verified_enemies, source_scope="authoritative"
            )
        settlement_ally_tokens = {
            str(row.get('character_id') or row.get('user_token') or '').strip()
            for row in verified_allies
            if isinstance(row, dict)
            and str(
                row.get('character_id') or row.get('user_token') or ''
            ).strip()
        }
        settlement_enemy_tokens = {
            str(row.get('character_id') or row.get('user_token') or '').strip()
            for row in verified_enemies
            if isinstance(row, dict)
            and str(
                row.get('character_id') or row.get('user_token') or ''
            ).strip()
        }
        four_faction = int(recording.get('map_id') or 0) == 5_200_223
        if (
            team_size > 1
            and len(settlement_ally_tokens) == team_size
            and (
                len(settlement_enemy_tokens) >= team_size
                if four_faction
                else len(settlement_enemy_tokens) == team_size
            )
        ):
            # A verified whole-team result is the final participant boundary.
            # Profiles observed earlier can still enrich matching rows, but an
            # unrelated hit target must not survive as a seventh player.
            allies = [
                row for row in allies
                if str(row.get('character_id') or '').strip()
                in settlement_ally_tokens
            ]
            enemies = [
                row for row in enemies
                if str(row.get('character_id') or '').strip()
                in settlement_enemy_tokens
            ]
        # Apply the verified arena side one last time after every optional
        # settlement overlay, then remove duplicate UIDs within each side.
        verified_ally_tokens = {
            str(row.get('user_token') or '').strip()
            for row in verified_pvp_allies
            if str(row.get('user_token') or '').strip()
        }

        def unique_rows(rows, rejected=()):
            rejected = set(rejected)
            result = []
            seen = set()
            for row in rows:
                token = str(row.get('character_id') or '').strip()
                if token and (token in rejected or token in seen):
                    continue
                if token:
                    seen.add(token)
                result.append(row)
            return result

        allies = unique_rows(allies, verified_enemy_tokens)
        ally_tokens = {
            str(row.get('character_id') or '').strip()
            for row in allies
            if str(row.get('character_id') or '').strip()
        }
        enemies = unique_rows(enemies, ally_tokens | verified_ally_tokens)
        for row in (*allies, *enemies, *opponents):
            token = str(row.get('character_id') or '').strip()
            evidence = {**tracker.token_profiles.get(token, {}), **row}
            if evidence.get('ai_template_id'):
                row['ai_template_id'] = evidence['ai_template_id']
            row['is_ai'] = pvp_bot_evidence(evidence)
        roster_complete = bool(
            team_size == 1
            or (
                team_size > 1
                and len(allies) >= team_size
                and len(enemies) >= team_size
                and verified_pvp_allies
                and verified_pvp_enemies
            )
        )
        team_rows = [*allies, *enemies]
        team_metrics_authoritative = bool(
            team_rows
            and all(
                row.get("metrics_scope") in {"self_exact", "authoritative"}
                for row in team_rows
            )
        )
        data_scope = {
            "roster": "verified" if roster_complete else "partial",
            "self_metrics": "self_exact",
            "team_metrics": (
                "authoritative" if team_metrics_authoritative else "observed_partial"
            ),
            "opponent_interactions": "pair_exact",
        }
        official_self = next(
            (
                row
                for row in allies
                if row.get('is_self') is True
                or (
                    tracker.self_token
                    and str(row.get('character_id') or '').strip()
                    == tracker.self_token
                )
            ),
            {},
        )

        def official_or(value, field):
            if (
                (
                    official_self.get('statistics_authoritative') is True
                    or (field in ('kills', 'assists', 'deaths')
                        and official_self.get('guild_counters_authoritative') is True)
                )
                and official_self.get(field) is not None
            ):
                return official_self.get(field)
            return value

        payload_damage = official_or(outgoing_total, 'damage')
        payload_healing = official_or(own_healing, 'healing')
        payload_taken = official_or(damage_taken, 'taken')
        payload_kills = official_or(sum(kills.values()), 'kills')
        payload_assists = official_or(0 if duel else sum(assists.values()), 'assists')
        payload_deaths = official_or(deaths, 'deaths')
        payload = {"schema_version": 1, "match_id": recording["match_id"],
                "battle_id": recording["match_id"],
                "map_id": recording["map_id"], **metadata,
                "instance_id": recording["instance_id"],
                "started_at_ns": start_ns, "ended_at_ns": end_ns,
                "started_at": started_at, "ended_at": ended_at,
                "start_time": started_at, "end_time": ended_at,
                "duration_seconds": (end_ns - start_ns) / 1e9,
                "result": (tracker.duel_outcome if duel and reason == 'duel_result'
                           else recording.get('result') if recording.get('result') in RESULTS
                           else "未知"), "end_reason": reason,
                "player": {"character_id": tracker.self_token, "name": player.get("name") or "未知玩家",
                           "profession_id": player.get("profession_id"), "extraordinary_rating": player.get("extraordinary_rating"),
                           "level": player.get("level"),
                           "avatar_id": player.get("avatar_id"),
                           "avatar_frame_id": player.get("avatar_frame_id"),
                           "equipment_snapshot": equipment(player)},
                "kills": payload_kills, "assists": payload_assists, "deaths": payload_deaths,
                "damage": payload_damage,
                "healing": payload_healing,
                "healing_source": healing_source,
                "taken": payload_taken,
                "damage_done": payload_damage, "damage_taken": payload_taken,
                 "healing_source_available": tracker.has_exact_healing,
                 "capture_complete": tracker.capture_complete, "assist_source_available": not duel,
                 "incoming_source_available": tracker.has_exact_incoming, "opponents": opponents,
                 "monsters": tracker.dragon_rows(include_skills=include_skills),
                 "monster_damage": sum(
                     row['damage_to'] for row in tracker.dragon_targets.values()
                 ),
                 "monster_taken": sum(
                     row['damage_from'] for row in tracker.dragon_targets.values()
                 ),
                 "allies": allies, "enemies": enemies, "data_scope": data_scope}
        # Keep the verified whole-team settlement in its canonical shape as
        # well as the legacy top-level participant arrays.  The backend already
        # validates this optional field, and the detail/history readers use it
        # to distinguish an authoritative team result from local observations.
        has_team_settlement = any(
            recording.get(field) is not None
            for field in ("teams", "team_stats", "team_settlement", "team_participants")
        )
        if has_team_settlement:
            payload["teams"] = {
                "allies": deepcopy(allies),
                "enemies": deepcopy(enemies),
            }
        for field in PVP_CONTEXT_FIELDS:
            if recording.get(field) not in (None, ''):
                payload[field] = recording[field]
        return payload

    def checkpoint(self, now_ns=None):
        recording = self.recording or self.suspended_recording or self.duel_recording
        if recording is not None:
            now_ns = time.time_ns() if now_ns is None else now_ns
            tracker = recording['tracker']
            completed_duel = bool(
                recording.get('match_kind') == 'duel' and tracker.duel_outcome
            )
            end_ns = tracker.ended_at_ns if completed_duel and tracker.ended_at_ns else now_ns
            reason = 'duel_result' if completed_duel else 'interrupted'
            self.repository.checkpoint(
                self.account_key,
                self.record_payload(
                    end_ns, reason, recording=recording,
                    include_equipment=False, include_roster=False,
                    include_skills=False,
                ),
            )
            self.last_checkpoint_ns = now_ns

    def stop(self, now_ns=None, reason="left_map"):
        if not self.active:
            if self.suspended_recording is not None:
                return self._finalize_suspended(now_ns, reason=reason)
            return self._stop_duel(now_ns, reason)
        payload = self.record_payload(time.time_ns() if now_ns is None else now_ns, reason)
        self.repository.save(self.account_key, payload)
        self.finalized_equipment_match_id = payload['match_id']
        self.finalized_equipment_tokens = {
            str(row.get('character_id') or '').strip()
            for row in (*payload.get('allies', ()), *payload.get('enemies', ()))
            if isinstance(row, dict) and str(row.get('character_id') or '').strip()
        }
        self.finalized_equipment_deadline_ns = payload['ended_at_ns'] + int(
            TEAM_EQUIPMENT_UPLOAD_GRACE_SECONDS * 1_000_000_000
        )
        self.recording['tracker']._pause(payload['ended_at_ns'])
        self.current._pause(payload['ended_at_ns'])
        self.presented_tracker = self.recording['tracker']
        self.presented_match_id = self.recording['match_id']
        self.recording = None
        self.awaiting_next_match = reason == 'match_result'
        self.last_finalized = payload
        return payload

    def _finalize_suspended(self, now_ns=None, reason='left_map'):
        recording = self.suspended_recording
        if recording is None:
            return None
        # The actual end boundary is when the player left this activity, not a
        # later grace timeout or the next activity's entry timestamp.
        requested = time.time_ns() if now_ns is None else int(now_ns)
        end_ns = self.suspended_left_at_ns or requested
        payload = self.record_payload(end_ns, reason, recording=recording)
        self.repository.save(self.account_key, payload)
        self.finalized_equipment_match_id = payload['match_id']
        self.finalized_equipment_tokens = {
            str(row.get('character_id') or '').strip()
            for row in (*payload.get('allies', ()), *payload.get('enemies', ()))
            if isinstance(row, dict) and str(row.get('character_id') or '').strip()
        }
        self.finalized_equipment_deadline_ns = payload['ended_at_ns'] + int(
            TEAM_EQUIPMENT_UPLOAD_GRACE_SECONDS * 1_000_000_000
        )
        self.presented_tracker = recording['tracker']
        self.presented_match_id = recording['match_id']
        self.suspended_recording = None
        self.suspended_left_at_ns = 0
        self.suspended_deadline_ns = 0
        self.last_finalized = payload
        return payload

    def _stop_duel(self, now_ns=None, reason='duel_result'):
        if self.duel_recording is None:
            return None
        recording = self.duel_recording
        tracker = recording['tracker']
        effective_reason = 'duel_result' if tracker.duel_outcome else reason
        requested_end = time.time_ns() if now_ns is None else now_ns
        end_ns = tracker.ended_at_ns if tracker.duel_outcome and tracker.ended_at_ns else requested_end
        payload = self.record_payload(end_ns, effective_reason, recording=recording)
        # A verified IndividualPVP session is a real match even outside a
        # formal map.  The backend accepts map_id=0 only with match_kind=duel.
        self.repository.save(self.account_key, payload)
        self.finalized_equipment_match_id = payload['match_id']
        self.finalized_equipment_tokens = {
            str(row.get('character_id') or '').strip()
            for row in (*payload.get('allies', ()), *payload.get('enemies', ()))
            if isinstance(row, dict) and str(row.get('character_id') or '').strip()
        }
        self.finalized_equipment_deadline_ns = payload['ended_at_ns'] + int(
            TEAM_EQUIPMENT_UPLOAD_GRACE_SECONDS * 1_000_000_000
        )
        self.duel_finalized_session_id = self.current.session_id
        # ``current`` is reused by identity discovery and the next invitation.
        # Freeze the completed view so neither can clear the visible result
        # before the next duel actually starts.
        self.presented_tracker = deepcopy(tracker)
        self.presented_match_id = recording['match_id']
        self.duel_recording = None
        self.duel_finalize_deadline_ns = 0
        self.last_finalized = payload
        return payload

    def poll(self, now_ns=None):
        """Finalize bounded late duel packets without blocking the UI thread."""

        now_ns = time.time_ns() if now_ns is None else int(now_ns)
        self.display_tracker.poll(now_ns)
        if (self.duel_recording is not None and self.duel_finalize_deadline_ns
                and now_ns >= self.duel_finalize_deadline_ns):
            return self._stop_duel(now_ns, 'duel_result')
        if (self.suspended_recording is not None and self.suspended_deadline_ns
                and now_ns >= self.suspended_deadline_ns):
            fields = self._continuity_fields(self.suspended_recording.get('map_id'))
            has_stable_identity = any(
                self.suspended_recording.get(field) not in (None, '')
                for field in fields
            )
            if not has_stable_identity:
                return self._finalize_suspended(now_ns, reason='left_map_timeout')
        return None

    def reset_context(self, now_ns=None, reason="capture_reset"):
        finalized = self.stop(now_ns, reason)
        self.last_boundary_ns = max(self.last_boundary_ns, int(now_ns or self.last_observation_ns))
        self.current.reset()
        self.presented_tracker = self.current
        self.presented_match_id = None
        self.duel_finalize_deadline_ns = 0
        self.suspended_recording = None
        self.suspended_left_at_ns = 0
        self.suspended_deadline_ns = 0
        self.map_id = self.instance_id = None
        self.instance_confirmed = False
        self.map_entered_at_ns = 0
        self.match_context = {}
        self.awaiting_next_match = False
        self.pending_observations.clear()
        self.party_tokens.clear()
        self.party_session_id = 0
        self.party_profiles.clear()
        self.party_seen = False
        self.party_active = False
        self.finalized_equipment_match_id = None
        self.finalized_equipment_tokens.clear()
        self.finalized_equipment_deadline_ns = 0
        return finalized


class PvpUploadWorker:
    """Background outbox persistence and uploads; no Tk or game access."""
    def __init__(self, repository, request, principal):
        self.repository, self.request, self.principal = repository, request, principal
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.equipment_queue = Queue()
        self.equipment_thread = None
        self.equipment_persistence_closing = False
        self.equipment_persist_errors = deque(maxlen=16)
        self.request_lock = threading.Lock()
        self.bound_request = None

    def enqueue_equipment_snapshot(self, account_key, profile):
        if not account_key or not isinstance(profile, dict) or self.equipment_persistence_closing:
            return False
        self._start_equipment_persistence()
        self.equipment_queue.put(("snapshot", account_key, profile))
        return True

    def enqueue_equipment_attachment(self, account_key, match_id, character_id, snapshot):
        if (
            not account_key or not match_id or not character_id
            or not isinstance(snapshot, dict) or self.equipment_persistence_closing
        ):
            return False
        self._start_equipment_persistence()
        self.equipment_queue.put((
            "attachment", account_key, str(match_id), str(character_id), snapshot
        ))
        return True

    def _start_equipment_persistence(self):
        if self.equipment_thread is not None:
            return
        self.equipment_thread = threading.Thread(
            target=self._persist_equipment_snapshots,
            name="pvp-equipment-outbox",
            daemon=True,
        )
        self.equipment_thread.start()

    def _persist_equipment_snapshots(self):
        while True:
            first = self.equipment_queue.get()
            if first is None:
                self.equipment_queue.task_done()
                return
            batch = [first]
            closing = False
            while len(batch) < 100:
                try:
                    item = self.equipment_queue.get_nowait()
                except Empty:
                    break
                if item is None:
                    self.equipment_queue.task_done()
                    closing = True
                    break
                batch.append(item)
            prepared = []
            attachments = {}
            for item in batch:
                if item[0] == "attachment":
                    _kind, account_key, match_id, character_id, snapshot = item
                    attachments.setdefault((account_key, match_id), {})[
                        character_id
                    ] = snapshot
                    continue
                _kind, account_key, profile = item
                try:
                    prepared.append((
                        account_key, build_equipment_snapshot_upload(profile)
                    ))
                except Exception as error:
                    self.equipment_persist_errors.append(str(error))
            try:
                if prepared:
                    self.repository.save_equipment_snapshots(prepared)
                    self.wake()
            except ValueError as error:
                if len(prepared) == 1:
                    self.equipment_persist_errors.append(str(error))
                else:
                    for account_key, snapshot in prepared:
                        try:
                            self.repository.save_equipment_snapshot(
                                account_key, snapshot
                            )
                            self.wake()
                        except Exception as item_error:
                            self.equipment_persist_errors.append(str(item_error))
            except Exception as error:
                self.equipment_persist_errors.append(str(error))
            for (account_key, match_id), snapshots in attachments.items():
                try:
                    if self.repository.attach_equipment_snapshots(
                        account_key, match_id, snapshots
                    ):
                        self.wake()
                except Exception as error:
                    self.equipment_persist_errors.append(str(error))
            for _ in batch:
                self.equipment_queue.task_done()
            if closing:
                return

    def finish_equipment_snapshots(self, timeout=5.0):
        if self.equipment_thread is None:
            return True
        if not self.equipment_persistence_closing:
            self.equipment_persistence_closing = True
            self.equipment_queue.put(None)
        self.equipment_thread.join(timeout)
        return not self.equipment_thread.is_alive() and not self.equipment_persist_errors

    def bind_request(self, account_key, request):
        """Freeze the authorization session with its outbox owner.

        Signing into another account while a request is in flight must never
        upload an old account's record with the new account's bearer token.
        """
        with self.request_lock:
            self.bound_request = (account_key, request)

    def unbind_request(self):
        with self.request_lock:
            self.bound_request = None

    def attempt_one(self, now=None):
        self.equipment_queue.join()
        account_key = self.principal()
        if not account_key:
            return False
        with self.request_lock:
            binding = self.bound_request
        if binding is None or binding[0] != account_key:
            return False
        request = binding[1]
        row = self.repository.due(account_key, now)
        equipment_row = None
        if not row:
            equipment_row = self.repository.due_equipment_snapshot(
                account_key, now
            )
            if not equipment_row:
                return False
        try:
            if row:
                response = request(
                    "records/upload",
                    {
                        "record": project_duel_result_counters(
                            json.loads(row["payload_json"]), copy_payload=False
                        )
                    },
                )
            else:
                response = request(
                    "equipment/snapshots/upload",
                    {"snapshot": json.loads(equipment_row["payload_json"])},
                )
            if self.principal() != account_key:
                raise ValueError("Account changed while upload was in flight")
            if not response.get("ok"):
                raise ValueError(response.get("message") or response.get("error") or "上传未被确认")
            if row:
                self.repository.acknowledge(
                    row["match_id"], response.get("match_id")
                )
            else:
                self.repository.acknowledge_equipment_snapshot(
                    equipment_row["snapshot_id"], response.get("snapshot_id")
                )
        except Exception as exc:
            if row:
                self.repository.fail(row["match_id"], exc, now)
            else:
                self.repository.fail_equipment_snapshot(
                    equipment_row["snapshot_id"], exc, now
                )
        return True

    def start(self):
        self._start_equipment_persistence()
        if self.thread and self.thread.is_alive():
            return
        def run():
            while not self.stop_event.is_set():
                if not self.attempt_one():
                    self.wake_event.wait(2)
                    self.wake_event.clear()
        self.thread = threading.Thread(target=run, name="pvp-upload-outbox", daemon=True)
        self.thread.start()

    def wake(self):
        self.wake_event.set()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
