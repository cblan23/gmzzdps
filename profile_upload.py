"""Profile identity and privacy-safe combat upload domain logic."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from statistics import median
from typing import Callable, Iterable, Mapping


NICKNAME_MIN_LENGTH = 2
NICKNAME_MAX_LENGTH = 12
MAX_ENCOUNTER_PARTICIPANTS = 24
MAX_UPLOAD_TARGETS = 64
MAX_UPLOAD_SKILL_TIMELINE_EVENTS = 100_000
MAX_UPLOAD_OPENING_EVENTS = 512
MAX_UPLOAD_BOSS_EVENTS = 50_000
MAX_UPLOAD_BOSS_HP_SAMPLES = 8_000
OPENING_SEQUENCE_MAX_MS = 30_000
LINK_CODE_TTL_SECONDS = 10 * 60
NICKNAME_RESERVATION_SECONDS = 30 * 24 * 60 * 60
CHARACTER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16}$")
APP_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){2,3}(?:[A-Za-z0-9.+_-]*)$")
LINK_CODE_PATTERN = re.compile(r"^[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){2}$")
LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_RESERVED_NICKNAMES = frozenset(
    {"叨叨诡秘助手", "Dps-Logs", "官方", "叨叨"}
)
TRAINING_DUMMY_TEMPLATE_IDS = frozenset(
    {
        7_100_632,
        7_101_004,
        7_101_006,
        7_101_017,
        7_101_025,
        7_101_029,
        7_101_030,
        7_106_050,
        7_107_304,
        7_114_223,
        7_114_224,
        7_114_225,
        7_114_226,
        7_114_227,
        7_114_228,
        7_114_233,
    }
)
UPLOAD_VICTORY_ARCHIVE_REASONS = frozenset(
    {"target_defeated", "boss_defeated", "stage_completed", "completed"}
)

MAP_DIFFICULTIES = {
    5_200_079: "hard",
    5_200_102: "hard",
    5_200_138: "normal",
    5_200_139: "hard",
    5_200_142: "normal",
    5_200_143: "normal",
    5_200_224: "normal",
    5_200_225: "epic",
    5_200_226: "final_challenge",
}
DUNGEON_DIFFICULTIES = {
    5_100_045: "hard",
    5_100_047: "hard",
    5_100_052: "normal",
    5_100_053: "hard",
    5_100_054: "normal",
    5_100_055: "normal",
    5_100_064: "normal",
    5_100_065: "epic",
    5_100_075: "final_challenge",
}

PUBLIC_PERFORMANCE_BOSSES: dict[str, dict[str, object]] = {
    "drill": {
        "name": "钻头",
        "dungeon_name": "记忆的传承",
        "stage_ids": (5_150_106, 5_150_110),
        "aliases": ("钻头", "\"钻头\"", "“钻头”", "”钻头“"),
    },
    "viscountess": {
        "name": "子爵夫人",
        "dungeon_name": "五月庄园·城堡",
        "stage_ids": (5_150_055, 5_150_063),
        "aliases": ("子爵夫人", "子爵夫人-神话姿态"),
    },
}
PUBLIC_PERFORMANCE_METRICS = frozenset({"dps", "boss_damage"})
PUBLIC_PERFORMANCE_SORTS = frozenset(
    {"p10", "p25", "p50", "p75", "p90", "best", "sample_count"}
)
PUBLIC_NO_PROJECTION_SQL = """NOT EXISTS (
    SELECT 1 FROM encounter_participants projection
    WHERE projection.encounter_id=e.encounter_id
      AND (projection.is_ai=1 OR projection.public_character_name LIKE '%·投影')
)"""


PROFILE_ERROR_MESSAGES = {
    "INVALID_CHARACTER_ID": "未能确认当前游戏角色，请进入游戏后重试。",
    "AI_CHARACTER_NOT_ALLOWED": "人机角色不能创建或关联上传身份。",
    "CHARACTER_ALREADY_LINKED": "当前角色已经关联了其他上传身份。",
    "PROFILE_NOT_FOUND": "当前角色尚未创建或关联上传身份。",
    "NICKNAME_INVALID_FORMAT": "昵称只能使用中文、英文字母和数字，不能包含空格或符号。",
    "NICKNAME_TOO_SHORT": "昵称至少需要 2 个字符。",
    "NICKNAME_TOO_LONG": "昵称最多只能有 12 个字符。",
    "NICKNAME_ALREADY_EXISTS": "该昵称已被使用，请换一个昵称。",
    "NICKNAME_RESERVED": "该昵称为保留名称，请换一个昵称。",
    "NICKNAME_SENSITIVE": "该昵称包含不可用内容，请换一个昵称。",
    "LINK_CODE_INVALID": "角色关联码无效，请检查后重试。",
    "LINK_CODE_EXPIRED": "角色关联码已过期，请重新生成。",
    "LINK_CODE_USED": "角色关联码已经使用，请重新生成。",
    "BAD_ENCOUNTER": "战斗记录格式不完整，无法上传。",
    "UPLOADER_NOT_IN_ENCOUNTER": "无法确认这场战斗中的本机角色。",
    "UPLOAD_VICTORY_REQUIRED": "只有战斗胜利的记录才能上传。",
    "UPLOAD_TRAINING_DUMMY_NOT_ALLOWED": "伤害木桩和治疗木桩记录不能上传。",
    "UPLOAD_NOT_ALLOWED": "当前登录状态没有数据上传权限。",
}


class ProfileUploadError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = str(code)
        self.message = message or PROFILE_ERROR_MESSAGES.get(
            self.code, "请求无法完成，请稍后重试。"
        )
        super().__init__(self.message)


def encounter_upload_rejection_code(encounter: object) -> str:
    """Return the hard upload rejection shared by the desktop and server.

    The input may be a complete local archive, a history-index summary, the
    compact API payload, or the server's parsed representation.  A concrete
    target-death archive reason and a confirmed settlement are both accepted
    forms of victory evidence, matching the result displayed in battle history.
    """

    if not isinstance(encounter, Mapping):
        return "BAD_ENCOUNTER"
    nested_payload = encounter.get("payload")
    nested_payload = nested_payload if isinstance(nested_payload, Mapping) else {}

    target_filter = _safe_text(
        encounter.get("target_filter", nested_payload.get("target_filter")), 32
    ).casefold()
    if target_filter in {
        "dummy",
        "damage_dummy",
        "healing_dummy",
        "training_dummy",
    }:
        return "UPLOAD_TRAINING_DUMMY_NOT_ALLOWED"

    template_ids: set[int] = set()

    def remember_template(value: object) -> None:
        template_id = _as_int(value, maximum=2_000_000_000)
        if template_id > 0:
            template_ids.add(template_id)

    for field in ("boss_template_ids", "template_ids"):
        values = encounter.get(field)
        if isinstance(values, (list, tuple, set, frozenset)):
            for value in values:
                remember_template(value)
    remember_template(encounter.get("boss_template_id"))

    names: list[str] = []

    def remember_target(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        remember_template(value.get("template_id"))
        name = _safe_text(value.get("name"), 96)
        if name:
            names.append(name)

    remember_target(encounter.get("monster"))
    targets = encounter.get("targets")
    if isinstance(targets, list):
        for target in targets:
            remember_target(target)
    for field in ("boss_name", "dungeon_name", "stage_name"):
        name = _safe_text(encounter.get(field), 96)
        if name:
            names.append(name)
    boss_names = encounter.get("boss_names")
    if isinstance(boss_names, (list, tuple)):
        names.extend(_safe_text(name, 96) for name in boss_names)

    if template_ids.intersection(TRAINING_DUMMY_TEMPLATE_IDS) or any(
        "木桩" in unicodedata.normalize("NFKC", name) for name in names if name
    ):
        return "UPLOAD_TRAINING_DUMMY_NOT_ALLOWED"

    result = _safe_text(encounter.get("result"), 24).casefold()
    result_aliases = {
        "胜利": "defeated",
        "已击败": "defeated",
        "defeated": "defeated",
        "失败": "failed",
        "failed": "failed",
        "中断": "interrupted",
        "interrupted": "interrupted",
        "unknown": "undetermined",
        "undetermined": "undetermined",
    }
    normalized_result = result_aliases.get(result, "")
    archive_reason = _safe_text(encounter.get("archive_reason", nested_payload.get("archive_reason")), 48).casefold()
    completion_value = encounter.get(
        "completion_confirmed", nested_payload.get("completion_confirmed")
    )
    completion_confirmed = bool(completion_value)
    accounting = encounter.get("damage_accounting")
    if not completion_confirmed and isinstance(accounting, Mapping):
        validations = accounting.get("stage_summary_validations")
        completion_confirmed = bool(
            isinstance(validations, list)
            and any(
                bool(item.get("completion_confirmed"))
                for item in validations
                if isinstance(item, Mapping)
            )
        )
    if not normalized_result and (
        completion_confirmed or archive_reason in UPLOAD_VICTORY_ARCHIVE_REASONS
    ):
        normalized_result = "defeated"
    # Upload eligibility and statistics/ranking qualification are separate.
    # A captured target-death verdict is valid victory evidence even when a
    # settlement detail packet never arrived; the latter still gates ranking.
    victory = normalized_result == "defeated" and (
        completion_confirmed or archive_reason in UPLOAD_VICTORY_ARCHIVE_REASONS
    )
    return "" if victory else "UPLOAD_VICTORY_REQUIRED"


@dataclass(frozen=True)
class CharacterIdentity:
    canonical: str
    kind: str
    role_number: int


def _loose_nickname_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def normalize_nickname(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ProfileUploadError("NICKNAME_INVALID_FORMAT")
    display = unicodedata.normalize("NFKC", value).strip()
    length = len(display)
    if length < NICKNAME_MIN_LENGTH:
        raise ProfileUploadError("NICKNAME_TOO_SHORT")
    if length > NICKNAME_MAX_LENGTH:
        raise ProfileUploadError("NICKNAME_TOO_LONG")
    for character in display:
        codepoint = ord(character)
        allowed = bool(
            "0" <= character <= "9"
            or "A" <= character <= "Z"
            or "a" <= character <= "z"
            or 0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        )
        if not allowed or unicodedata.category(character).startswith(("C", "Z")):
            raise ProfileUploadError("NICKNAME_INVALID_FORMAT")
    return display, display.casefold()


def canonical_character_identity(
    value: object, *, allow_ai: bool = False
) -> CharacterIdentity:
    if not isinstance(value, str):
        raise ProfileUploadError("INVALID_CHARACTER_ID")
    text = value.strip().rstrip("=")
    if not CHARACTER_ID_PATTERN.fullmatch(text):
        raise ProfileUploadError("INVALID_CHARACTER_ID")
    try:
        decoded = base64.urlsafe_b64decode(text + "=" * ((-len(text)) % 4))
    except (ValueError, TypeError):
        raise ProfileUploadError("INVALID_CHARACTER_ID") from None
    if len(decoded) != 12:
        raise ProfileUploadError("INVALID_CHARACTER_ID")
    if decoded[0] == 0x01:
        kind = "human"
    elif decoded[0] == 0x6A:
        kind = "ai"
    else:
        raise ProfileUploadError("INVALID_CHARACTER_ID")
    role_number = int.from_bytes(decoded[4:12], "little", signed=False)
    if role_number <= 0:
        raise ProfileUploadError("INVALID_CHARACTER_ID")
    if kind == "ai" and not allow_ai:
        raise ProfileUploadError("AI_CHARACTER_NOT_ALLOWED")
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    return CharacterIdentity(canonical, kind, role_number)


def character_hash(value: object, key: bytes, *, allow_ai: bool = False) -> str:
    identity = canonical_character_identity(value, allow_ai=allow_ai)
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("character HMAC key must contain at least 32 bytes")
    return hmac.new(
        key,
        b"gmzz-profile-character-v1\0" + identity.canonical.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def initialize_profile_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            profile_id TEXT PRIMARY KEY,
            nickname TEXT NOT NULL,
            normalized_nickname TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profile_characters (
            character_hash TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL REFERENCES profiles(profile_id)
                ON DELETE CASCADE,
            character_name TEXT NOT NULL DEFAULT '',
            profession_id INTEGER NOT NULL DEFAULT 0,
            linked_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_profile_characters_profile
            ON profile_characters(profile_id, linked_at);
        CREATE TABLE IF NOT EXISTS nickname_reservations (
            normalized_nickname TEXT PRIMARY KEY,
            nickname TEXT NOT NULL,
            profile_id TEXT NOT NULL REFERENCES profiles(profile_id)
                ON DELETE CASCADE,
            reserved_at REAL NOT NULL,
            reserved_until REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT 'renamed'
        );
        CREATE INDEX IF NOT EXISTS idx_nickname_reservations_expiry
            ON nickname_reservations(reserved_until);
        CREATE TABLE IF NOT EXISTS profile_link_codes (
            code_hash TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL REFERENCES profiles(profile_id)
                ON DELETE CASCADE,
            issuer_character_hash TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL,
            consumed_by_character_hash TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_profile_link_codes_profile
            ON profile_link_codes(profile_id, expires_at DESC);
        CREATE TABLE IF NOT EXISTS encounters (
            encounter_id TEXT PRIMARY KEY,
            encounter_fingerprint TEXT NOT NULL UNIQUE,
            client_first_encounter_id TEXT NOT NULL DEFAULT '',
            boss_key TEXT NOT NULL,
            boss_name TEXT NOT NULL DEFAULT '',
            boss_template_ids_json TEXT NOT NULL DEFAULT '[]',
            dungeon_id INTEGER NOT NULL DEFAULT 0,
            stage_id INTEGER NOT NULL DEFAULT 0,
            difficulty TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            ended_at REAL NOT NULL,
            duration_seconds REAL NOT NULL,
            team_size INTEGER NOT NULL,
            team_total_damage INTEGER NOT NULL,
            game_version TEXT NOT NULL DEFAULT '',
            participant_set_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            data_quality INTEGER NOT NULL DEFAULT 0,
            statistics_status TEXT NOT NULL DEFAULT 'not_eligible',
            ranking_status TEXT NOT NULL DEFAULT 'not_eligible',
            validation_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_encounters_match
            ON encounters(boss_key, stage_id, ended_at DESC);
        CREATE INDEX IF NOT EXISTS idx_encounters_statistics
            ON encounters(statistics_status, ended_at DESC);
        CREATE TABLE IF NOT EXISTS encounter_participants (
            encounter_id TEXT NOT NULL REFERENCES encounters(encounter_id)
                ON DELETE CASCADE,
            character_hash TEXT NOT NULL,
            slot_number INTEGER NOT NULL,
            identity_resolved INTEGER NOT NULL DEFAULT 1,
            is_ai INTEGER NOT NULL DEFAULT 0,
            profile_id TEXT REFERENCES profiles(profile_id),
            profession_id INTEGER NOT NULL DEFAULT 0,
            public_mode TEXT NOT NULL DEFAULT 'anonymous',
            public_character_name TEXT NOT NULL DEFAULT '',
            damage INTEGER NOT NULL DEFAULT 0,
            dps REAL NOT NULL DEFAULT 0,
            hps REAL NOT NULL DEFAULT 0,
            taken INTEGER NOT NULL DEFAULT 0,
            stats_json TEXT NOT NULL DEFAULT '{}',
            data_quality INTEGER NOT NULL DEFAULT 0,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            PRIMARY KEY(encounter_id, character_hash),
            UNIQUE(encounter_id, slot_number)
        );
        CREATE INDEX IF NOT EXISTS idx_encounter_participants_profile
            ON encounter_participants(profile_id, encounter_id);
        CREATE INDEX IF NOT EXISTS idx_encounter_participants_character_names
            ON encounter_participants(public_character_name, encounter_id)
            WHERE public_character_name!='';
        CREATE INDEX IF NOT EXISTS idx_encounter_participants_public_character
            ON encounter_participants(public_character_name, encounter_id)
            WHERE public_mode='character';
        CREATE INDEX IF NOT EXISTS idx_encounter_participants_public_nickname
            ON encounter_participants(profile_id, encounter_id)
            WHERE public_mode='nickname';
        CREATE INDEX IF NOT EXISTS idx_encounter_participants_profession
            ON encounter_participants(profession_id, encounter_id);
        CREATE TABLE IF NOT EXISTS uploads (
            upload_id TEXT PRIMARY KEY,
            encounter_id TEXT NOT NULL REFERENCES encounters(encounter_id)
                ON DELETE CASCADE,
            uploader_character_hash TEXT NOT NULL,
            profile_id TEXT REFERENCES profiles(profile_id),
            public_mode TEXT NOT NULL,
            public_character_name TEXT NOT NULL DEFAULT '',
            uploaded_at REAL NOT NULL,
            last_uploaded_at REAL NOT NULL,
            app_version TEXT NOT NULL DEFAULT '',
            game_version TEXT NOT NULL DEFAULT '',
            payload_hash TEXT NOT NULL,
            data_completeness TEXT NOT NULL DEFAULT '',
            verification_status TEXT NOT NULL DEFAULT 'accepted',
            upload_status TEXT NOT NULL DEFAULT 'uploaded',
            statistics_status TEXT NOT NULL DEFAULT 'not_eligible',
            ranking_status TEXT NOT NULL DEFAULT 'not_eligible',
            validation_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(encounter_id, uploader_character_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_uploads_profile
            ON uploads(profile_id, last_uploaded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_uploads_rankings
            ON uploads(ranking_status, encounter_id);
        """
    )
    upload_columns = {
        str(row[1]): row for row in connection.execute("PRAGMA table_info(uploads)")
    }
    profile_column = upload_columns.get("profile_id")
    if profile_column is not None and int(profile_column[3] or 0):
        # v1 required a Profile before every upload. Rebuild only this table so
        # existing receipts remain intact while anonymous users can leave the
        # optional display identity empty.
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP INDEX IF EXISTS idx_uploads_profile;
                DROP INDEX IF EXISTS idx_uploads_rankings;
                ALTER TABLE uploads RENAME TO uploads_profile_required_v1;
                CREATE TABLE uploads (
                    upload_id TEXT PRIMARY KEY,
                    encounter_id TEXT NOT NULL REFERENCES encounters(encounter_id)
                        ON DELETE CASCADE,
                    uploader_character_hash TEXT NOT NULL,
                    profile_id TEXT REFERENCES profiles(profile_id),
                    public_mode TEXT NOT NULL,
                    public_character_name TEXT NOT NULL DEFAULT '',
                    uploaded_at REAL NOT NULL,
                    last_uploaded_at REAL NOT NULL,
                    app_version TEXT NOT NULL DEFAULT '',
                    game_version TEXT NOT NULL DEFAULT '',
                    payload_hash TEXT NOT NULL,
                    data_completeness TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL DEFAULT 'accepted',
                    upload_status TEXT NOT NULL DEFAULT 'uploaded',
                    statistics_status TEXT NOT NULL DEFAULT 'not_eligible',
                    ranking_status TEXT NOT NULL DEFAULT 'not_eligible',
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(encounter_id, uploader_character_hash)
                );
                INSERT INTO uploads(
                    upload_id, encounter_id, uploader_character_hash, profile_id,
                    public_mode, public_character_name, uploaded_at,
                    last_uploaded_at, app_version, game_version, payload_hash,
                    data_completeness, verification_status, upload_status,
                    statistics_status, ranking_status, validation_json
                )
                SELECT
                    upload_id, encounter_id, uploader_character_hash, profile_id,
                    public_mode, public_character_name, uploaded_at,
                    last_uploaded_at, app_version, game_version, payload_hash,
                    data_completeness, verification_status, upload_status,
                    statistics_status, ranking_status, validation_json
                FROM uploads_profile_required_v1;
                DROP TABLE uploads_profile_required_v1;
                CREATE INDEX idx_uploads_profile
                    ON uploads(profile_id, last_uploaded_at DESC);
                CREATE INDEX idx_uploads_rankings
                    ON uploads(ranking_status, encounter_id);
                COMMIT;
                """
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise


def _safe_text(value: object, maximum: int) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return "".join(
        character
        for character in text
        if not unicodedata.category(character).startswith("C")
    )[:maximum]


def _as_int(value: object, minimum: int = 0, maximum: int = (1 << 63) - 1) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return minimum
    return min(maximum, max(minimum, parsed))


def _as_float(
    value: object, minimum: float = 0.0, maximum: float = 86400.0
) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return minimum
    if not math.isfinite(parsed):
        return minimum
    return min(maximum, max(minimum, parsed))


def _encounter_difficulty(record: Mapping[str, object]) -> tuple[str, str]:
    explicit = _safe_text(record.get("difficulty"), 32).casefold()
    if explicit:
        return {
            "普通": "normal",
            "困难": "hard",
            "史诗": "epic",
            "英雄": "heroic",
            "神话": "mythic",
            "终局挑战": "final_challenge",
        }.get(explicit, explicit), "record"
    map_id = _as_int(record.get("map_id"), maximum=2_000_000_000)
    if map_id in MAP_DIFFICULTIES:
        return MAP_DIFFICULTIES[map_id], "map_id"
    dungeon_id = _as_int(record.get("dungeon_id"), maximum=2_000_000_000)
    if dungeon_id in DUNGEON_DIFFICULTIES:
        return DUNGEON_DIFFICULTIES[dungeon_id], "dungeon_id"
    context = " ".join(
        _safe_text(record.get(field), 96)
        for field in ("dungeon_name", "stage_name")
    )
    for marker, difficulty in (
        ("终局挑战", "final_challenge"),
        ("史诗", "epic"),
        ("英雄", "heroic"),
        ("神话", "mythic"),
        ("困难", "hard"),
        ("普通", "normal"),
    ):
        if marker in context:
            return difficulty, "display_name"
    return "", "unavailable"


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _prefer_richer_detail(existing: object, incoming: object) -> object:
    """Merge repeated uploads without discarding a previously captured detail."""

    if isinstance(existing, Mapping) and isinstance(incoming, Mapping):
        merged = dict(existing)
        for key, value in incoming.items():
            merged[key] = _prefer_richer_detail(existing.get(key), value)
        return merged
    if isinstance(existing, list) and isinstance(incoming, list):
        existing_score = (len(existing), len(_json_text(existing)))
        incoming_score = (len(incoming), len(_json_text(incoming)))
        return incoming if incoming_score >= existing_score else existing
    if incoming is None or incoming == "" or incoming == [] or incoming == {}:
        return existing
    return incoming


def _linear_percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * min(1.0, max(0.0, percentile))
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


def _public_performance_boss_key(value: object) -> str:
    text = _safe_text(value, 64).casefold()
    if not text:
        return "drill"
    unquoted = text.strip(" \"'“”‘’")
    for key, config in PUBLIC_PERFORMANCE_BOSSES.items():
        if text == key:
            return key
        aliases = config.get("aliases", ())
        for alias in aliases if isinstance(aliases, tuple) else ():
            normalized = _safe_text(alias, 64).casefold()
            if text == normalized or unquoted == normalized.strip(" \"'“”‘’"):
                return key
    return "drill"


def _public_performance_difficulty(value: object) -> str:
    text = _safe_text(value, 24).casefold()
    aliases = {
        "": "all",
        "all": "all",
        "normal": "normal",
        "普通": "normal",
        "hard": "hard",
        "困难": "hard",
        "nightmare": "nightmare",
        "噩梦": "nightmare",
    }
    return aliases.get(text, "all")


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(18).rstrip("=")


def _link_code_digest(code: str, key: bytes) -> str:
    return hmac.new(
        key,
        b"gmzz-profile-link-code-v1\0" + code.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _unresolved_hash(seed: str, key: bytes) -> str:
    return hmac.new(
        key,
        b"gmzz-unresolved-participant-v1\0" + seed.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class ProfileUploadStore:
    def __init__(
        self,
        hmac_key: bytes,
        *,
        now: Callable[[], float] = time.time,
        reserved_nicknames: Iterable[str] = DEFAULT_RESERVED_NICKNAMES,
        sensitive_words: Iterable[str] = (),
        supported_boss_template_ids: Iterable[int] = (),
        supported_game_versions: Iterable[str] = (),
    ):
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise ValueError("character HMAC key must contain at least 32 bytes")
        self.hmac_key = hmac_key
        self.now = now
        self.reserved_nicknames = {
            key
            for value in reserved_nicknames
            if (key := _loose_nickname_key(value))
        }
        self.sensitive_words = {
            key
            for value in sensitive_words
            if (key := _loose_nickname_key(value))
        }
        self.supported_boss_template_ids = {
            _as_int(value) for value in supported_boss_template_ids if _as_int(value)
        }
        self.supported_game_versions = {
            _safe_text(value, 48).casefold()
            for value in supported_game_versions
            if _safe_text(value, 48)
        }

    def _hash_character(self, value: object, *, allow_ai: bool = False) -> str:
        return character_hash(value, self.hmac_key, allow_ai=allow_ai)

    @staticmethod
    def _profile_payload(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
        return {
            "profile_id": str(row["profile_id"]),
            "nickname": str(row["nickname"]),
        }

    def nickname_availability(
        self,
        connection: sqlite3.Connection,
        nickname: object,
        *,
        current_profile_id: str = "",
    ) -> dict[str, object]:
        loose_key = _loose_nickname_key(nickname)
        if loose_key in self.reserved_nicknames:
            return {"status": "RESERVED", "error": "NICKNAME_RESERVED"}
        try:
            display, normalized = normalize_nickname(nickname)
        except ProfileUploadError as error:
            return {"status": "INVALID", "error": error.code}
        if any(word in normalized for word in self.sensitive_words):
            return {"status": "SENSITIVE", "error": "NICKNAME_SENSITIVE"}
        claimed = connection.execute(
            "SELECT profile_id FROM profiles WHERE normalized_nickname=?",
            (normalized,),
        ).fetchone()
        if claimed is not None and str(claimed["profile_id"]) != current_profile_id:
            return {"status": "CLAIMED", "error": "NICKNAME_ALREADY_EXISTS"}
        timestamp = self.now()
        reserved = connection.execute(
            """
            SELECT profile_id FROM nickname_reservations
            WHERE normalized_nickname=? AND reserved_until>?
            """,
            (normalized, timestamp),
        ).fetchone()
        if reserved is not None and str(reserved["profile_id"]) != current_profile_id:
            return {"status": "RESERVED", "error": "NICKNAME_RESERVED"}
        return {
            "status": "AVAILABLE",
            "nickname": display,
            "normalized_nickname": normalized,
        }

    def _require_available_nickname(
        self,
        connection: sqlite3.Connection,
        nickname: object,
        *,
        current_profile_id: str = "",
    ) -> tuple[str, str]:
        result = self.nickname_availability(
            connection, nickname, current_profile_id=current_profile_id
        )
        if result["status"] != "AVAILABLE":
            raise ProfileUploadError(str(result.get("error", "NICKNAME_INVALID_FORMAT")))
        return str(result["nickname"]), str(result["normalized_nickname"])

    def resolve_profile(
        self,
        connection: sqlite3.Connection,
        character_id: object,
        *,
        character_name: object = "",
        profession_id: object = 0,
    ) -> dict[str, object]:
        identity = canonical_character_identity(character_id)
        hashed = self._hash_character(identity.canonical)
        row = connection.execute(
            """
            SELECT p.profile_id, p.nickname
            FROM profile_characters c
            JOIN profiles p ON p.profile_id=c.profile_id
            WHERE c.character_hash=?
            """,
            (hashed,),
        ).fetchone()
        if row is None:
            return {"status": "UNLINKED", "profile": None}
        clean_character_name = _safe_text(character_name, 48)
        clean_profession_id = _as_int(profession_id, maximum=2_000_000_000)
        connection.execute(
            """
            UPDATE profile_characters
            SET character_name=CASE WHEN ?<>'' THEN ? ELSE character_name END,
                profession_id=CASE WHEN ?>0 THEN ? ELSE profession_id END,
                last_seen_at=?
            WHERE character_hash=?
            """,
            (
                clean_character_name,
                clean_character_name,
                clean_profession_id,
                clean_profession_id,
                self.now(),
                hashed,
            ),
        )
        return {"status": "LINKED", "profile": self._profile_payload(row)}

    def create_profile(
        self,
        connection: sqlite3.Connection,
        character_id: object,
        nickname: object,
        *,
        character_name: object = "",
        profession_id: object = 0,
    ) -> dict[str, object]:
        identity = canonical_character_identity(character_id)
        hashed = self._hash_character(identity.canonical)
        existing = connection.execute(
            "SELECT profile_id FROM profile_characters WHERE character_hash=?",
            (hashed,),
        ).fetchone()
        if existing is not None:
            row = connection.execute(
                "SELECT profile_id, nickname FROM profiles WHERE profile_id=?",
                (existing["profile_id"],),
            ).fetchone()
            if row is None:
                raise ProfileUploadError("PROFILE_NOT_FOUND")
            return {"created": False, "profile": self._profile_payload(row)}
        display, normalized = self._require_available_nickname(connection, nickname)
        timestamp = self.now()
        profile_id = _new_id("prf_")
        try:
            connection.execute(
                """
                INSERT INTO profiles(
                    profile_id, nickname, normalized_nickname, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (profile_id, display, normalized, timestamp, timestamp),
            )
        except sqlite3.IntegrityError:
            raise ProfileUploadError("NICKNAME_ALREADY_EXISTS") from None
        connection.execute(
            """
            INSERT INTO profile_characters(
                character_hash, profile_id, character_name, profession_id,
                linked_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                hashed,
                profile_id,
                _safe_text(character_name, 48),
                _as_int(profession_id, maximum=2_000_000_000),
                timestamp,
                timestamp,
            ),
        )
        return {
            "created": True,
            "profile": {"profile_id": profile_id, "nickname": display},
        }

    def rename_profile(
        self,
        connection: sqlite3.Connection,
        character_id: object,
        nickname: object,
    ) -> dict[str, object]:
        hashed = self._hash_character(character_id)
        row = connection.execute(
            """
            SELECT p.profile_id, p.nickname, p.normalized_nickname
            FROM profile_characters c
            JOIN profiles p ON p.profile_id=c.profile_id
            WHERE c.character_hash=?
            """,
            (hashed,),
        ).fetchone()
        if row is None:
            raise ProfileUploadError("PROFILE_NOT_FOUND")
        profile_id = str(row["profile_id"])
        display, normalized = self._require_available_nickname(
            connection, nickname, current_profile_id=profile_id
        )
        if normalized == str(row["normalized_nickname"]):
            if display != str(row["nickname"]):
                connection.execute(
                    "UPDATE profiles SET nickname=?, updated_at=? WHERE profile_id=?",
                    (display, self.now(), profile_id),
                )
            return {"changed": display != str(row["nickname"]), "profile": {"profile_id": profile_id, "nickname": display}}
        timestamp = self.now()
        try:
            connection.execute(
                """
                UPDATE profiles SET nickname=?, normalized_nickname=?, updated_at=?
                WHERE profile_id=?
                """,
                (display, normalized, timestamp, profile_id),
            )
        except sqlite3.IntegrityError:
            raise ProfileUploadError("NICKNAME_ALREADY_EXISTS") from None
        connection.execute(
            """
            INSERT INTO nickname_reservations(
                normalized_nickname, nickname, profile_id, reserved_at,
                reserved_until, reason
            ) VALUES (?, ?, ?, ?, ?, 'renamed')
            ON CONFLICT(normalized_nickname) DO UPDATE SET
                nickname=excluded.nickname,
                profile_id=excluded.profile_id,
                reserved_at=excluded.reserved_at,
                reserved_until=excluded.reserved_until,
                reason=excluded.reason
            """,
            (
                str(row["normalized_nickname"]),
                str(row["nickname"]),
                profile_id,
                timestamp,
                timestamp + NICKNAME_RESERVATION_SECONDS,
            ),
        )
        return {"changed": True, "profile": {"profile_id": profile_id, "nickname": display}}

    def create_link_code(
        self, connection: sqlite3.Connection, character_id: object
    ) -> dict[str, object]:
        hashed = self._hash_character(character_id)
        row = connection.execute(
            """
            SELECT c.profile_id, p.nickname
            FROM profile_characters c
            JOIN profiles p ON p.profile_id=c.profile_id
            WHERE c.character_hash=?
            """,
            (hashed,),
        ).fetchone()
        if row is None:
            raise ProfileUploadError("PROFILE_NOT_FOUND")
        timestamp = self.now()
        connection.execute(
            """
            UPDATE profile_link_codes SET consumed_at=?
            WHERE issuer_character_hash=? AND consumed_at IS NULL
            """,
            (timestamp, hashed),
        )
        code = "-".join(
            "".join(secrets.choice(LINK_CODE_ALPHABET) for _index in range(4))
            for _group in range(3)
        )
        connection.execute(
            """
            INSERT INTO profile_link_codes(
                code_hash, profile_id, issuer_character_hash, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _link_code_digest(code, self.hmac_key),
                str(row["profile_id"]),
                hashed,
                timestamp,
                timestamp + LINK_CODE_TTL_SECONDS,
            ),
        )
        return {
            "code": code,
            "expires_at": timestamp + LINK_CODE_TTL_SECONDS,
            "profile": self._profile_payload(row),
        }

    def redeem_link_code(
        self,
        connection: sqlite3.Connection,
        character_id: object,
        code: object,
        *,
        character_name: object = "",
        profession_id: object = 0,
    ) -> dict[str, object]:
        identity = canonical_character_identity(character_id)
        hashed = self._hash_character(identity.canonical)
        existing = connection.execute(
            "SELECT profile_id FROM profile_characters WHERE character_hash=?",
            (hashed,),
        ).fetchone()
        if existing is not None:
            raise ProfileUploadError("CHARACTER_ALREADY_LINKED")
        normalized_code = _safe_text(code, 20).upper()
        if not LINK_CODE_PATTERN.fullmatch(normalized_code):
            raise ProfileUploadError("LINK_CODE_INVALID")
        row = connection.execute(
            """
            SELECT * FROM profile_link_codes WHERE code_hash=?
            """,
            (_link_code_digest(normalized_code, self.hmac_key),),
        ).fetchone()
        if row is None:
            raise ProfileUploadError("LINK_CODE_INVALID")
        timestamp = self.now()
        if row["consumed_at"] is not None:
            raise ProfileUploadError("LINK_CODE_USED")
        if float(row["expires_at"]) <= timestamp:
            raise ProfileUploadError("LINK_CODE_EXPIRED")
        profile_id = str(row["profile_id"])
        connection.execute(
            """
            INSERT INTO profile_characters(
                character_hash, profile_id, character_name, profession_id,
                linked_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                hashed,
                profile_id,
                _safe_text(character_name, 48),
                _as_int(profession_id, maximum=2_000_000_000),
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            UPDATE profile_link_codes
            SET consumed_at=?, consumed_by_character_hash=?
            WHERE code_hash=? AND consumed_at IS NULL
            """,
            (timestamp, hashed, str(row["code_hash"])),
        )
        profile = connection.execute(
            "SELECT profile_id, nickname FROM profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        if profile is None:
            raise ProfileUploadError("PROFILE_NOT_FOUND")
        return {"linked": True, "profile": self._profile_payload(profile)}

    @staticmethod
    def _clean_skill_rows(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        rows: list[dict[str, object]] = []
        for raw in value[:96]:
            if not isinstance(raw, dict):
                continue
            skill_id = _as_int(raw.get("skill_id"), maximum=2_000_000_000)
            damage = _as_int(raw.get("damage"))
            effective_healing = _as_int(raw.get("effective_healing"))
            if skill_id <= 0 and damage <= 0 and effective_healing <= 0:
                continue
            row = {
                    "skill_id": skill_id,
                    "name": _safe_text(raw.get("name"), 64),
                    "damage": damage,
                    "effective_healing": effective_healing,
                    "share": _as_float(raw.get("share"), maximum=1.0),
                    "hits": _as_int(raw.get("hits"), maximum=10_000_000),
                    "max_hit": _as_int(raw.get("max_hit")),
                    "source": _safe_text(raw.get("source"), 48),
                }
            for key in ('total_healing', 'overhealing'):
                if raw.get(key) is not None:
                    row[key] = _as_int(raw[key])
            for key in ('healing_skill_count', 'healing_events'):
                if raw.get(key) is not None:
                    row[key] = _as_int(raw[key], maximum=10_000_000)
            for key in ('count_semantics',):
                if raw.get(key):
                    row[key] = _safe_text(raw[key], 48)
            if raw.get('server_skill_count') is not None:
                row['server_skill_count'] = _as_int(raw['server_skill_count'], maximum=10_000_000)
            for key in ('critical_hits', 'penetration_hits', 'damage_hits', 'healing_hits'):
                if raw.get(key) is not None:
                    row[key] = _as_int(raw[key], maximum=10_000_000)
            for key in ('critical_rate', 'penetration_rate'):
                if raw.get(key) is not None:
                    row[key] = _as_float(raw[key], maximum=1.0)
            rows.append(row)
        return rows

    @staticmethod
    def _clean_target_rows(value: object, *, maximum: int = MAX_UPLOAD_TARGETS) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        rows: list[dict[str, object]] = []
        for raw in value[:maximum]:
            if not isinstance(raw, Mapping):
                continue
            row: dict[str, object] = {}
            for key in ("name", "kind", "entity_type", "health_source"):
                if raw.get(key) is not None:
                    row[key] = _safe_text(raw.get(key), 96)
            for key in (
                "entity_id",
                "actor_id",
                "template_id",
                "level",
                "boss_type",
                "boss_rank",
                "damage",
                "hits",
                "max_hit",
                "current_hp",
                "max_hp",
                "observed_max_hp",
            ):
                if raw.get(key) is not None:
                    row[key] = _as_int(raw.get(key), maximum=4_102_444_800_000_000_000)
            if raw.get("share") is not None:
                row["share"] = _as_float(raw.get("share"), maximum=1.0)
            entity_ids = raw.get("entity_ids")
            if isinstance(entity_ids, list):
                row["entity_ids"] = [
                    _as_int(entity_id, maximum=4_102_444_800_000_000_000)
                    for entity_id in entity_ids[:32]
                    if _as_int(entity_id, maximum=4_102_444_800_000_000_000) > 0
                ]
            if row.get("name") or row.get("damage") or row.get("template_id"):
                rows.append(row)
        return rows

    @staticmethod
    def _clean_opening_sequence(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        rows: list[dict[str, object]] = []
        for raw in value[:MAX_UPLOAD_OPENING_EVENTS]:
            if not isinstance(raw, Mapping):
                continue
            time_ms = _as_int(raw.get("time_ms"), maximum=OPENING_SEQUENCE_MAX_MS)
            skill_id = _as_int(raw.get("skill_id"), maximum=2_000_000_000)
            if skill_id <= 0:
                continue
            row: dict[str, object] = {
                "time_ms": time_ms,
                "skill_id": skill_id,
                "sequence": _as_int(raw.get("sequence"), maximum=10_000_000),
                "source": _safe_text(raw.get("source"), 32),
            }
            for key in ("damage", "target_id", "target_template_id"):
                if raw.get(key) is not None:
                    row[key] = _as_int(raw.get(key), maximum=4_102_444_800_000_000_000)
            if raw.get("target_name") is not None:
                row["target_name"] = _safe_text(raw.get("target_name"), 96)
            rows.append(row)
        return rows

    @staticmethod
    def _clean_compact_log(
        value: object,
        *,
        allowed_columns: Mapping[str, str],
        required_columns: Iterable[str],
        maximum_rows: int,
    ) -> dict[str, object]:
        if not isinstance(value, Mapping):
            return {}
        raw_columns = value.get("columns")
        raw_rows = value.get("rows")
        if not isinstance(raw_columns, list) or not isinstance(raw_rows, list):
            return {}
        columns = [str(column) for column in raw_columns if str(column) in allowed_columns]
        if not set(required_columns).issubset(columns) or len(columns) != len(raw_columns):
            return {}
        cleaned_rows: list[list[object]] = []
        for raw_row in raw_rows[:maximum_rows]:
            if not isinstance(raw_row, (list, tuple)) or len(raw_row) < len(columns):
                continue
            row: list[object] = []
            for index, column in enumerate(columns):
                kind = allowed_columns[column]
                if kind == "text":
                    row.append(_safe_text(raw_row[index], 96))
                elif kind == "float":
                    row.append(_as_float(raw_row[index], maximum=4_102_444_800_000_000_000.0))
                else:
                    row.append(_as_int(raw_row[index], maximum=4_102_444_800_000_000_000))
            cleaned_rows.append(row)
        if not cleaned_rows:
            return {}
        result: dict[str, object] = {
            "version": _as_int(value.get("version"), maximum=100),
            "columns": columns,
            "rows": cleaned_rows,
        }
        for key in ("coverage", "source"):
            if value.get(key) is not None:
                result[key] = _safe_text(value.get(key), 64)
        if value.get("origin_started_at_epoch") is not None:
            result["origin_started_at_epoch"] = _as_float(
                value.get("origin_started_at_epoch"), maximum=4_102_444_800.0
            )
        if value.get("interval_seconds") is not None:
            result["interval_seconds"] = _as_float(
                value.get("interval_seconds"), maximum=86_400.0
            )
        result["truncated"] = len(raw_rows) > maximum_rows or bool(value.get("truncated"))
        result["total_rows"] = _as_int(
            value.get("total_rows", len(raw_rows)), maximum=10_000_000
        )
        return result

    @classmethod
    def _clean_boss_damage(cls, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, object] = {}
        for key in ("coverage", "unavailable_reason"):
            if value.get(key) is not None:
                result[key] = _safe_text(value.get(key), 96)
        for key in (
            "version",
            "observed_damage",
            "team_taken",
            "unassigned_taken",
            "hits",
            "max_hit",
            "rejected_after_limit",
        ):
            if value.get(key) is not None:
                result[key] = _as_int(value.get(key))
        if value.get("classification_ratio") is not None:
            result["classification_ratio"] = _as_float(
                value.get("classification_ratio"), maximum=1.0
            )
        result["truncated"] = bool(value.get("truncated"))

        skills: list[dict[str, object]] = []
        for raw in value.get("skills", [])[:256] if isinstance(value.get("skills"), list) else []:
            if not isinstance(raw, Mapping):
                continue
            row = {
                "skill_id": _as_int(raw.get("skill_id"), maximum=2_000_000_000),
                "name": _safe_text(raw.get("name"), 96),
                "damage": _as_int(raw.get("damage")),
                "hits": _as_int(raw.get("hits"), maximum=10_000_000),
                "max_hit": _as_int(raw.get("max_hit")),
                "source_count": _as_int(raw.get("source_count"), maximum=10_000),
                "share": _as_float(raw.get("share"), maximum=1.0),
                "average_hit": _as_float(raw.get("average_hit"), maximum=1e18),
            }
            if row["skill_id"] > 0 or row["damage"] > 0:
                skills.append(row)
        result["skills"] = skills

        targets: list[dict[str, object]] = []
        for raw in value.get("targets", [])[:MAX_UPLOAD_TARGETS] if isinstance(value.get("targets"), list) else []:
            if not isinstance(raw, Mapping):
                continue
            targets.append({
                "actor_id": _as_int(raw.get("actor_id"), maximum=4_102_444_800_000_000_000),
                "name": _safe_text(raw.get("name"), 48),
                "damage": _as_int(raw.get("damage")),
                "hits": _as_int(raw.get("hits"), maximum=10_000_000),
                "max_hit": _as_int(raw.get("max_hit")),
                "share": _as_float(raw.get("share"), maximum=1.0),
            })
        result["targets"] = targets

        sources: list[dict[str, object]] = []
        for raw in value.get("sources", [])[:MAX_UPLOAD_TARGETS] if isinstance(value.get("sources"), list) else []:
            if not isinstance(raw, Mapping):
                continue
            source = {
                "source_key": _safe_text(raw.get("source_key"), 96),
                "entity_id": _as_int(raw.get("entity_id"), maximum=4_102_444_800_000_000_000),
                "template_id": _as_int(raw.get("template_id"), maximum=2_000_000_000),
                "name": _safe_text(raw.get("name"), 96),
                "kind": _safe_text(raw.get("kind"), 32),
                "damage": _as_int(raw.get("damage")),
                "hits": _as_int(raw.get("hits"), maximum=10_000_000),
                "max_hit": _as_int(raw.get("max_hit")),
                "share": _as_float(raw.get("share"), maximum=1.0),
                "average_hit": _as_float(raw.get("average_hit"), maximum=1e18),
            }
            source["skills"] = cls._clean_skill_rows(raw.get("skills"))
            source["targets"] = cls._clean_target_rows(raw.get("targets"))
            if isinstance(raw.get("entity_ids"), list):
                source["entity_ids"] = [
                    _as_int(value, maximum=4_102_444_800_000_000_000)
                    for value in raw["entity_ids"][:32]
                    if _as_int(value, maximum=4_102_444_800_000_000_000) > 0
                ]
            sources.append(source)
        result["sources"] = sources

        event_log = cls._clean_compact_log(
            value.get("event_log"),
            allowed_columns={
                "time_ms": "int",
                "source_id": "int",
                "target_id": "int",
                "skill_id": "int",
                "damage": "int",
            },
            required_columns=("time_ms", "source_id", "target_id", "skill_id", "damage"),
            maximum_rows=MAX_UPLOAD_BOSS_EVENTS,
        )
        if event_log:
            result["event_log"] = event_log
        death_log = cls._clean_compact_log(
            value.get("death_event_log"),
            allowed_columns={"time_ms": "int", "actor_id": "int", "name": "text"},
            required_columns=("time_ms", "actor_id"),
            maximum_rows=10_000,
        )
        if death_log:
            result["death_event_log"] = death_log
        return result

    @staticmethod
    def clean_equipment_snapshot(value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, object] = {}
        for key in ('captured_at', 'source', 'equipment_score_source'):
            if key in value:
                result[key] = _safe_text(value[key], 96)
        for key in ('captured_at_ns', 'extraordinary_rating', 'equipment_score',
                    'equipment_known_score', 'active_word_count', 'total_word_count', 'pvp_equipment_count'):
            if value.get(key) is not None:
                result[key] = _as_int(value[key], maximum=4_102_444_800_000_000_000)
        for key in ('partial', 'equipment_score_complete'):
            if key in value:
                result[key] = bool(value[key])
        attributes = value.get('attributes')
        if isinstance(attributes, Mapping):
            result['attributes'] = {str(key): _as_float(number, maximum=1e18)
                for key, number in list(attributes.items())[:96]
                if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', str(key))
                and isinstance(number, (int, float)) and math.isfinite(number)}

        def properties(raw):
            return [{'key': _safe_text(row.get('key'), 64), 'name': _safe_text(row.get('name'), 96),
                     'value': row.get('value') if isinstance(row.get('value'), (int, float)) and math.isfinite(row['value']) else None}
                    for row in raw[:32] if isinstance(row, Mapping)] if isinstance(raw, list) else []

        items = []
        for raw in value.get('equipment', [])[:24] if isinstance(value.get('equipment'), list) else []:
            if not isinstance(raw, Mapping):
                continue
            item = {}
            for key in ('slot_name', 'item_name', 'quality_name', 'equipment_mode', 'score_source'):
                if key in raw:
                    item[key] = _safe_text(raw[key], 96)
            for key in (
                'slot', 'item_id', 'quality', 'item_score', 'known_score', 'total_score',
                'base_score', 'random_score', 'enhance_score', 'enhance_level',
                'enhance_completed_level', 'enhance_completed_score', 'enhance_level_score',
                'enhance_stage_count', 'enhance_level_progress_percent', 'enhance_level_remaining',
                'enhance_overall_percent', 'enhance_progress_percent', 'next_enhance_increment',
                'next_enhance_level', 'next_enhance_remaining', 'next_enhance_score',
                'score_breakdown_total', 'auxiliary_id', 'grow_body_id',
                'active_word_count', 'total_word_count', 'word_score',
            ):
                if raw.get(key) is not None:
                    item[key] = _as_int(raw[key])
            for key in ('score_complete', 'score_breakdown_complete', 'score_breakdown_matches', 'is_pvp'):
                if key in raw:
                    item[key] = bool(raw[key])
            metadata = raw.get('metadata')
            if isinstance(metadata, Mapping):
                item['metadata'] = {
                    key: (
                        _safe_text(metadata[key], 96)
                        if key in {'icon', 'mode', 'random_group'}
                        else _as_int(metadata[key])
                    )
                    for key in (
                        'icon', 'mode', 'sub_type', 'tag', 'item_level', 'required_level',
                        'quality', 'base_score', 'season_id', 'item_description_id',
                        'item_name_id', 'random_group', 'tag_name_id',
                    )
                    if key in metadata and metadata[key] is not None
                }
            affixes = []
            for affix in raw.get('affixes', [])[:32] if isinstance(raw.get('affixes'), list) else []:
                if isinstance(affix, Mapping):
                    affixes.append({
                        'name': _safe_text(affix.get('name'), 96),
                        'category': _safe_text(affix.get('category'), 32),
                        'effect_type': _safe_text(affix.get('effect_type'), 48),
                        'property_key': _safe_text(affix.get('property_key'), 64),
                        'score': _as_int(affix.get('score')),
                        'word_id': _as_int(affix.get('word_id')),
                        'class_type': _as_int(affix.get('class_type')),
                        'property_value': (
                            affix.get('property_value')
                            if isinstance(affix.get('property_value'), (int, float))
                            and math.isfinite(affix['property_value'])
                            else None
                        ),
                        'properties': properties(affix.get('properties')),
                    })
            item['affixes'] = affixes
            special = raw.get('special_affix')
            if isinstance(special, Mapping):
                item['special_affix'] = {'name': _safe_text(special.get('name'), 96),
                    'icon': _safe_text(special.get('icon'), 96),
                    'name_id': _as_int(special.get('name_id')),
                    'auxiliary_id': _as_int(special.get('auxiliary_id')),
                    'score': _as_int(special.get('score')), 'properties': properties(special.get('properties')),
                    'passive_skill_ids': [_as_int(sid, maximum=2_000_000_000) for sid in special.get('passive_skill_ids', [])[:16]]
                        if isinstance(special.get('passive_skill_ids'), list) else []}
            for key in ('word_ids', 'word_scores', 'word_class_types'):
                if isinstance(raw.get(key), list):
                    item[key] = [_as_int(value) for value in raw[key][:32]]
            for key, fields in (
                ('enhance_schedule', ('level', 'score', 'cumulative_score')),
                ('enhance_stages', ('stage', 'level', 'percent')),
            ):
                if isinstance(raw.get(key), list):
                    item[key] = [
                        {field: _as_int(row.get(field)) for field in fields}
                        for row in raw[key][:32]
                        if isinstance(row, Mapping)
                    ]
            items.append(item)
        result['equipment'] = items
        for key in ('gems', 'sets'):
            if key in value:
                result[key] = [{'name': _safe_text(row.get('name'), 96), 'item_id': _as_int(row.get('item_id')),
                    'level': _as_int(row.get('level')), 'properties': properties(row.get('properties'))}
                    for row in value[key][:32] if isinstance(row, Mapping)] if isinstance(value[key], list) else None
        return result

    @classmethod
    def _clean_participant_stats(cls, raw: Mapping[str, object]) -> dict[str, object]:
        result: dict[str, object] = {}
        integer_fields = (
            "extraordinary_rating",
            "damage",
            "hits",
            "max_hit",
            "damage_hits",
            "critical_hits",
            "penetration_hits",
            "deaths",
            "revives",
            "taken",
            "classified_skill_damage",
            "unclassified_damage",
            "accounted_skill_damage",
            "effective_healing",
            "total_healing",
            "overhealing",
        )
        for field in integer_fields:
            if field in raw and raw.get(field) is not None:
                result[field] = _as_int(raw.get(field))
        for field in (
            "dps",
            "hps",
            "share",
            "critical_rate",
            "penetration_rate",
            "taken_share",
            "death_duration_seconds",
        ):
            if field in raw and raw.get(field) is not None:
                maximum = 1.0 if field.endswith("rate") or field.endswith("share") else 1e18
                result[field] = _as_float(raw.get(field), maximum=maximum)
        for field in ("skill_source", "skill_detail_status", "taken_source"):
            if field in raw:
                result[field] = _safe_text(raw.get(field), 48)
        result["skills"] = cls._clean_skill_rows(raw.get("skills"))
        targets = cls._clean_target_rows(raw.get("targets"))
        if targets:
            result["targets"] = targets
        opening_sequence = cls._clean_opening_sequence(raw.get("opening_sequence"))
        if opening_sequence:
            result["opening_sequence"] = opening_sequence
        if raw.get("opening_sequence_source") is not None:
            result["opening_sequence_source"] = _safe_text(
                raw.get("opening_sequence_source"), 32
            )
        equipment = cls.clean_equipment_snapshot(raw.get('equipment_snapshot'))
        if equipment:
            result['equipment_snapshot'] = equipment
            if equipment.get('equipment_score') is not None and equipment.get('equipment_score_complete'):
                result['equipment_rating'] = equipment['equipment_score']
        timeline = raw.get("skill_timeline")
        if isinstance(timeline, list):
            cleaned_timeline: list[dict[str, object]] = []
            for item in timeline[:MAX_UPLOAD_SKILL_TIMELINE_EVENTS]:
                if not isinstance(item, dict):
                    continue
                cleaned = {
                    "time_ms": _as_int(item.get("time_ms"), maximum=86_400_000),
                    "skill_id": _as_int(item.get("skill_id"), maximum=2_000_000_000),
                    "damage": _as_int(item.get("damage")),
                    "critical": item.get("critical") if isinstance(item.get("critical"), bool) else None,
                    "penetrating": item.get("penetrating") if isinstance(item.get("penetrating"), bool) else None,
                }
                for key in ("target_id", "target_template_id"):
                    if item.get(key) is not None:
                        cleaned[key] = _as_int(
                            item.get(key), maximum=4_102_444_800_000_000_000
                        )
                if item.get("target_name") is not None:
                    cleaned["target_name"] = _safe_text(item.get("target_name"), 96)
                cleaned_timeline.append(cleaned)
            if cleaned_timeline:
                result["skill_timeline"] = cleaned_timeline
                result["skill_timeline_total"] = _as_int(
                    raw.get("skill_timeline_total", len(timeline)), maximum=10_000_000
                )
                result["skill_timeline_truncated"] = (
                    len(timeline) > MAX_UPLOAD_SKILL_TIMELINE_EVENTS
                    or bool(raw.get("skill_timeline_truncated"))
                )
        return result

    def _parse_participants(
        self,
        value: object,
        *,
        uploader_hash: str,
        client_encounter_id: str,
    ) -> tuple[list[dict[str, object]], bool]:
        if not isinstance(value, list) or not value or len(value) > MAX_ENCOUNTER_PARTICIPANTS:
            raise ProfileUploadError("BAD_ENCOUNTER")
        participants: list[dict[str, object]] = []
        seen_hashes: set[str] = set()
        uploader_found = False
        all_resolved = True
        for index, raw in enumerate(value, start=1):
            if not isinstance(raw, dict):
                raise ProfileUploadError("BAD_ENCOUNTER")
            raw_identity = raw.get("character_id")
            resolved = bool(raw_identity)
            is_ai = bool(raw.get("is_ai"))
            if resolved:
                identity = canonical_character_identity(raw_identity, allow_ai=True)
                is_ai = identity.kind == "ai"
                hashed = self._hash_character(identity.canonical, allow_ai=True)
            else:
                all_resolved = False
                seed = "|".join(
                    (
                        client_encounter_id,
                        str(index),
                        str(_as_int(raw.get("profession_id"), maximum=2_000_000_000)),
                        str(_as_int(raw.get("damage"))),
                    )
                )
                hashed = _unresolved_hash(seed, self.hmac_key)
            if hashed in seen_hashes:
                raise ProfileUploadError("BAD_ENCOUNTER")
            seen_hashes.add(hashed)
            is_uploader = bool(raw.get("is_uploader"))
            if is_uploader:
                if uploader_found or hashed != uploader_hash or is_ai or not resolved:
                    raise ProfileUploadError("UPLOADER_NOT_IN_ENCOUNTER")
                uploader_found = True
            stats = self._clean_participant_stats(raw)
            damage = _as_int(stats.get("damage"))
            dps = _as_float(stats.get("dps"), maximum=1e18)
            hps = _as_float(stats.get("hps"), maximum=1e18)
            taken = _as_int(stats.get("taken"))
            skill_count = len(stats.get("skills", []))
            timeline_count = len(stats.get("skill_timeline", []))
            quality = (
                (80 if resolved else 0)
                + (20 if damage > 0 else 0)
                + min(30, skill_count)
                + min(20, timeline_count // 10)
                + (15 if stats.get("equipment_snapshot") else 0)
                + (10 if stats.get("targets") else 0)
                + (10 if stats.get("opening_sequence") else 0)
                + (10 if "effective_healing" in stats or "hps" in stats else 0)
                + (10 if "taken" in stats else 0)
                + (5 if "deaths" in stats else 0)
            )
            participants.append(
                {
                    "character_hash": hashed,
                    "slot_number": index,
                    "identity_resolved": resolved,
                    "is_ai": is_ai,
                    "is_uploader": is_uploader,
                    "character_name": _safe_text(raw.get("game_character_name"), 48),
                    "profession_id": _as_int(
                        raw.get("profession_id"), maximum=2_000_000_000
                    ),
                    "damage": damage,
                    "dps": dps,
                    "hps": hps,
                    "taken": taken,
                    "stats": stats,
                    "data_quality": quality,
                }
            )
        if not uploader_found:
            raise ProfileUploadError("UPLOADER_NOT_IN_ENCOUNTER")
        return participants, all_resolved

    def _parse_encounter(
        self,
        encounter: object,
        *,
        uploader_hash: str,
    ) -> dict[str, object]:
        if not isinstance(encounter, dict):
            raise ProfileUploadError("BAD_ENCOUNTER")
        client_encounter_id = _safe_text(encounter.get("client_encounter_id"), 96)
        started_at = _as_float(encounter.get("started_at_epoch"), maximum=4_102_444_800.0)
        ended_at = _as_float(encounter.get("ended_at_epoch"), maximum=4_102_444_800.0)
        duration = _as_float(encounter.get("duration_seconds"), maximum=86_400.0)
        if not client_encounter_id or started_at <= 0 or ended_at < started_at or duration <= 0:
            raise ProfileUploadError("BAD_ENCOUNTER")
        observed_duration = ended_at - started_at
        if abs(observed_duration - duration) > max(10.0, duration * 0.1):
            raise ProfileUploadError("BAD_ENCOUNTER")
        raw_template_ids = encounter.get("boss_template_ids")
        template_ids = sorted(
            {
                _as_int(value, maximum=2_000_000_000)
                for value in raw_template_ids
            }
        ) if isinstance(raw_template_ids, list) else []
        template_ids = [value for value in template_ids if value > 0][:16]
        boss_name = _safe_text(encounter.get("boss_name"), 96)
        dungeon_id = _as_int(encounter.get("dungeon_id"), maximum=2_000_000_000)
        stage_id = _as_int(encounter.get("stage_id"), maximum=2_000_000_000)
        difficulty = _safe_text(encounter.get("difficulty"), 32).casefold()
        boss_key_source = {
            "templates": template_ids,
            "boss": "" if template_ids else boss_name.casefold(),
            "dungeon": dungeon_id,
            "stage": stage_id,
            "difficulty": difficulty,
        }
        boss_key = hashlib.sha256(_json_text(boss_key_source).encode("utf-8")).hexdigest()
        participants, all_resolved = self._parse_participants(
            encounter.get("participants"),
            uploader_hash=uploader_hash,
            client_encounter_id=client_encounter_id,
        )
        declared_team_size = _as_int(
            encounter.get("team_size"), minimum=1, maximum=MAX_ENCOUNTER_PARTICIPANTS
        )
        team_size = max(len(participants), declared_team_size)
        if team_size > MAX_ENCOUNTER_PARTICIPANTS:
            raise ProfileUploadError("BAD_ENCOUNTER")
        team_total_damage = _as_int(encounter.get("team_total_damage"))
        participant_hashes = sorted(str(item["character_hash"]) for item in participants)
        participant_set_hash = hashlib.sha256("|".join(participant_hashes).encode("ascii")).hexdigest()
        game_version = _safe_text(encounter.get("game_version"), 48)
        fingerprint_source = {
            "boss": boss_key,
            "started_5s": int(round(started_at / 5.0)),
            "ended_5s": int(round(ended_at / 5.0)),
            "duration": int(round(duration)),
            "participants": participant_hashes,
            "team_total_damage": team_total_damage,
            "game_version": game_version,
        }
        fingerprint = hashlib.sha256(
            _json_text(fingerprint_source).encode("utf-8")
        ).hexdigest()
        result = _safe_text(encounter.get("result"), 24).casefold()
        archive_reason = _safe_text(encounter.get("archive_reason"), 48).casefold()
        completion_confirmed = bool(encounter.get("completion_confirmed"))
        if result not in {"defeated", "failed", "interrupted", "undetermined"}:
            result = "defeated" if archive_reason in {"target_defeated", "completed"} and completion_confirmed else "undetermined"
        completeness = _safe_text(encounter.get("data_completeness"), 32).casefold()
        if completeness not in {"complete", "partial", "incomplete", "mid_encounter"}:
            completeness = "complete" if all_resolved else "partial"
        payload = {
            "result": result,
            "archive_reason": archive_reason,
            "completion_confirmed": completion_confirmed,
            "target_filter": _safe_text(encounter.get("target_filter"), 32),
            "map_id": _as_int(encounter.get("map_id"), maximum=2_000_000_000),
            "dungeon_name": _safe_text(encounter.get("dungeon_name"), 96),
            "stage_name": _safe_text(encounter.get("stage_name"), 96),
            "difficulty_source": _safe_text(encounter.get("difficulty_source"), 32),
            "team_dps": _as_float(encounter.get("team_dps"), maximum=1e18),
            "team_hps": _as_float(encounter.get("team_hps"), maximum=1e18),
            "team_effective_healing": _as_int(encounter.get("team_effective_healing")),
            "team_taken": _as_int(encounter.get("team_taken")),
        }
        raw_monster = encounter.get("monster")
        if isinstance(raw_monster, Mapping):
            cleaned_monster = self._clean_target_rows([raw_monster], maximum=1)
            if cleaned_monster:
                payload["monster"] = cleaned_monster[0]
        cleaned_targets = self._clean_target_rows(encounter.get("targets"))
        if cleaned_targets:
            payload["targets"] = cleaned_targets
        boss_damage = self._clean_boss_damage(encounter.get("boss_damage"))
        if boss_damage:
            payload["boss_damage"] = boss_damage
        boss_hp_samples = self._clean_compact_log(
            encounter.get("boss_hp_damage_samples"),
            allowed_columns={
                "time_seconds": "float",
                "observed_boss_hp_loss": "int",
            },
            required_columns=("time_seconds", "observed_boss_hp_loss"),
            maximum_rows=MAX_UPLOAD_BOSS_HP_SAMPLES,
        )
        if boss_hp_samples:
            payload["boss_hp_damage_samples"] = boss_hp_samples
        raw_coverage = encounter.get("module_coverage")
        if isinstance(raw_coverage, Mapping):
            payload["module_coverage"] = {
                str(key)[:64]: (
                    bool(value)
                    if isinstance(value, bool)
                    else _as_int(value, maximum=10_000_000)
                )
                for key, value in list(raw_coverage.items())[:64]
                if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(key))
            }
        raw_timeline = encounter.get("team_dps_timeline")
        if isinstance(raw_timeline, list):
            timeline: list[dict[str, object]] = []
            for item in raw_timeline[:4000]:
                if not isinstance(item, dict):
                    continue
                timeline.append(
                    {
                        "time": _as_float(item.get("time"), maximum=86_400.0),
                        "dps": _as_float(item.get("dps"), maximum=1e18),
                        "team_dps": _as_float(item.get("team_dps"), maximum=1e18),
                        "source": _safe_text(item.get("source"), 48),
                    }
                )
            if timeline:
                payload["team_dps_timeline"] = timeline
        data_quality = (
            (100 if all_resolved and len(participants) == team_size else 0)
            + (40 if completion_confirmed else 0)
            + (30 if completeness == "complete" else 0)
            + min(50, sum(int(item["data_quality"]) for item in participants) // max(1, len(participants)))
            + (10 if difficulty else 0)
            + (10 if payload.get("monster") else 0)
            + (10 if payload.get("targets") else 0)
            + (20 if payload.get("boss_damage") else 0)
            + (20 if payload.get("boss_hp_damage_samples") else 0)
            + (10 if payload.get("module_coverage") else 0)
        )
        return {
            "client_encounter_id": client_encounter_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration": duration,
            "boss_key": boss_key,
            "boss_name": boss_name,
            "template_ids": template_ids,
            "dungeon_id": dungeon_id,
            "stage_id": stage_id,
            "difficulty": difficulty,
            "participants": participants,
            "all_resolved": all_resolved,
            "team_size": team_size,
            "team_total_damage": team_total_damage,
            "participant_set_hash": participant_set_hash,
            "game_version": game_version,
            "fingerprint": fingerprint,
            "result": result,
            "completion_confirmed": completion_confirmed,
            "completeness": completeness,
            "capture_started_mid_encounter": bool(encounter.get("capture_started_mid_encounter")),
            "payload": payload,
            "data_quality": data_quality,
        }

    def _qualification(
        self,
        parsed: Mapping[str, object],
        *,
        app_version: str,
        uploader: Mapping[str, object],
    ) -> dict[str, object]:
        reasons: list[str] = []
        template_ids = list(parsed["template_ids"])
        supported_boss = bool(template_ids) and (
            not self.supported_boss_template_ids
            or bool(set(template_ids).intersection(self.supported_boss_template_ids))
        )
        if not supported_boss:
            reasons.append("BOSS_UNSUPPORTED")
        if int(parsed["stage_id"]) <= 0:
            reasons.append("DIFFICULTY_INVALID")
        duration = float(parsed["duration"])
        if duration < 3.0 or duration > 4 * 60 * 60:
            reasons.append("DURATION_INVALID")
        if not APP_VERSION_PATTERN.fullmatch(app_version):
            reasons.append("CLIENT_VERSION_UNSUPPORTED")
        game_version = str(parsed["game_version"] or "").casefold()
        if (
            self.supported_game_versions
            and game_version not in self.supported_game_versions
        ):
            reasons.append("GAME_VERSION_UNSUPPORTED")
        if not bool(parsed["completion_confirmed"]) or parsed["result"] != "defeated":
            reasons.append("ENCOUNTER_NOT_COMPLETED")
        if bool(parsed["capture_started_mid_encounter"]) or parsed["completeness"] in {"incomplete", "mid_encounter"}:
            reasons.append("CAPTURE_INCOMPLETE")
        if not bool(parsed["all_resolved"]) or len(parsed["participants"]) != int(parsed["team_size"]):
            reasons.append("PARTICIPANT_IDENTITY_INCOMPLETE")
        participant_total = sum(int(item["damage"]) for item in parsed["participants"])
        team_total = int(parsed["team_total_damage"])
        if team_total <= 0 or participant_total <= 0:
            reasons.append("KEY_DATA_MISSING")
        elif abs(participant_total - team_total) > max(1000, int(team_total * 0.01)):
            reasons.append("TEAM_TOTAL_MISMATCH")
        statistics_blockers = {
            "BOSS_UNSUPPORTED",
            "DIFFICULTY_INVALID",
            "DURATION_INVALID",
            "CLIENT_VERSION_UNSUPPORTED",
            "GAME_VERSION_UNSUPPORTED",
            "ENCOUNTER_NOT_COMPLETED",
            "CAPTURE_INCOMPLETE",
            "KEY_DATA_MISSING",
            "TEAM_TOTAL_MISMATCH",
        }
        statistics_status = (
            "included"
            if not statistics_blockers.intersection(reasons)
            else "not_eligible"
        )
        ranking_blockers = statistics_blockers | {
            "CAPTURE_INCOMPLETE",
            "PARTICIPANT_IDENTITY_INCOMPLETE",
        }
        ranking_status = (
            "eligible"
            if not ranking_blockers.intersection(reasons)
            and int(uploader["damage"]) > 0
            and not bool(uploader["is_ai"])
            else "not_eligible"
        )
        return {
            "statistics_status": statistics_status,
            "ranking_status": ranking_status,
            "reasons": reasons,
        }

    def _matching_encounter(
        self,
        connection: sqlite3.Connection,
        parsed: Mapping[str, object],
    ) -> sqlite3.Row | None:
        incoming_uploader = next(
            str(item["character_hash"])
            for item in parsed["participants"]
            if bool(item["is_uploader"])
        )
        same_client_record = connection.execute(
            """
            SELECT e.* FROM encounters e
            JOIN uploads u ON u.encounter_id=e.encounter_id
            WHERE e.client_first_encounter_id=? AND u.uploader_character_hash=?
            ORDER BY u.last_uploaded_at DESC LIMIT 1
            """,
            (parsed["client_encounter_id"], incoming_uploader),
        ).fetchone()
        if same_client_record is not None:
            return same_client_record
        exact = connection.execute(
            "SELECT * FROM encounters WHERE encounter_fingerprint=?",
            (parsed["fingerprint"],),
        ).fetchone()
        if exact is not None:
            return exact
        candidates = connection.execute(
            """
            SELECT * FROM encounters
            WHERE boss_key=? AND dungeon_id=? AND stage_id=? AND team_size=?
                AND ended_at BETWEEN ? AND ?
            ORDER BY ABS(ended_at-?) ASC
            LIMIT 12
            """,
            (
                parsed["boss_key"],
                parsed["dungeon_id"],
                parsed["stage_id"],
                parsed["team_size"],
                float(parsed["ended_at"]) - 30.0,
                float(parsed["ended_at"]) + 30.0,
                parsed["ended_at"],
            ),
        ).fetchall()
        incoming_hashes = {
            str(item["character_hash"])
            for item in parsed["participants"]
            if bool(item["identity_resolved"])
        }
        for candidate in candidates:
            if abs(float(candidate["duration_seconds"]) - float(parsed["duration"])) > 5.0:
                continue
            total_tolerance = max(1000, int(int(parsed["team_total_damage"]) * 0.01))
            if abs(int(candidate["team_total_damage"]) - int(parsed["team_total_damage"])) > total_tolerance:
                continue
            rows = connection.execute(
                """
                SELECT character_hash, identity_resolved, profession_id, damage
                FROM encounter_participants WHERE encounter_id=?
                """,
                (candidate["encounter_id"],),
            ).fetchall()
            existing_hashes = {
                str(row["character_hash"])
                for row in rows
                if bool(row["identity_resolved"])
            }
            all_existing_resolved = len(existing_hashes) == len(rows) == int(candidate["team_size"])
            if all_existing_resolved and bool(parsed["all_resolved"]):
                if existing_hashes != incoming_hashes:
                    continue
            else:
                overlap = len(existing_hashes.intersection(incoming_hashes))
                needed = max(1, math.ceil(min(len(existing_hashes), len(incoming_hashes)) * 0.75))
                if incoming_uploader not in existing_hashes and overlap < needed:
                    existing_signatures = [
                        (int(row["profession_id"]), int(row["damage"]))
                        for row in rows
                        if int(row["damage"]) > 0
                    ]
                    incoming_signatures = [
                        (int(item["profession_id"]), int(item["damage"]))
                        for item in parsed["participants"]
                        if int(item["damage"]) > 0
                    ]
                    remaining = list(existing_signatures)
                    signature_overlap = 0
                    for signature in incoming_signatures:
                        if signature in remaining:
                            remaining.remove(signature)
                            signature_overlap += 1
                    signature_needed = max(
                        1,
                        math.ceil(
                            min(
                                len(existing_signatures),
                                len(incoming_signatures),
                            )
                            * 0.75
                        ),
                    )
                    if (
                        not existing_signatures
                        or not incoming_signatures
                        or signature_overlap < signature_needed
                    ):
                        continue
            return candidate
        return None

    @staticmethod
    def _find_unresolved_match(
        connection: sqlite3.Connection,
        encounter_id: str,
        participant: Mapping[str, object],
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM encounter_participants
            WHERE encounter_id=? AND identity_resolved=0
                AND profession_id=? AND damage=?
            ORDER BY slot_number LIMIT 1
            """,
            (
                encounter_id,
                participant["profession_id"],
                participant["damage"],
            ),
        ).fetchone()

    @staticmethod
    def _find_stat_match(
        connection: sqlite3.Connection,
        encounter_id: str,
        participant: Mapping[str, object],
    ) -> sqlite3.Row | None:
        profession_id = int(participant["profession_id"])
        damage = int(participant["damage"])
        if damage <= 0:
            return None
        rows = connection.execute(
            """
            SELECT * FROM encounter_participants
            WHERE encounter_id=? AND profession_id=? AND damage=?
            ORDER BY slot_number
            """,
            (encounter_id, profession_id, damage),
        ).fetchall()
        if not rows:
            return None
        requested_slot = int(participant["slot_number"])
        by_slot = [row for row in rows if int(row["slot_number"]) == requested_slot]
        if len(by_slot) == 1:
            return by_slot[0]
        return rows[0] if len(rows) == 1 else None

    def _upsert_participant(
        self,
        connection: sqlite3.Connection,
        encounter_id: str,
        participant: Mapping[str, object],
        timestamp: float,
    ) -> None:
        hashed = str(participant["character_hash"])
        existing = connection.execute(
            """
            SELECT * FROM encounter_participants
            WHERE encounter_id=? AND character_hash=?
            """,
            (encounter_id, hashed),
        ).fetchone()
        slot_number = int(participant["slot_number"])
        if existing is None and bool(participant["identity_resolved"]):
            unresolved = self._find_unresolved_match(connection, encounter_id, participant)
            if unresolved is not None:
                slot_number = int(unresolved["slot_number"])
                connection.execute(
                    "DELETE FROM encounter_participants WHERE encounter_id=? AND character_hash=?",
                    (encounter_id, unresolved["character_hash"]),
                )
        elif existing is None:
            stat_match = self._find_stat_match(
                connection, encounter_id, participant
            )
            if stat_match is not None:
                existing = stat_match
                hashed = str(stat_match["character_hash"])
                slot_number = int(stat_match["slot_number"])
        if existing is None:
            occupied = connection.execute(
                "SELECT 1 FROM encounter_participants WHERE encounter_id=? AND slot_number=?",
                (encounter_id, slot_number),
            ).fetchone()
            if occupied is not None:
                slot_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(slot_number), 0)+1 FROM encounter_participants WHERE encounter_id=?",
                        (encounter_id,),
                    ).fetchone()[0]
                )
            profile = connection.execute(
                "SELECT profile_id FROM profile_characters WHERE character_hash=?",
                (hashed,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO encounter_participants(
                    encounter_id, character_hash, slot_number, identity_resolved,
                    is_ai, profile_id, profession_id, public_mode, public_character_name, damage, dps, hps, taken,
                    stats_json, data_quality, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    encounter_id,
                    hashed,
                    slot_number,
                    int(bool(participant["identity_resolved"])),
                    int(bool(participant["is_ai"])),
                    str(profile["profile_id"]) if profile is not None else None,
                    participant["profession_id"],
                    'character' if participant['character_name'] else 'nickname' if profile is not None else 'anonymous',
                    participant['character_name'],
                    participant["damage"],
                    participant["dps"],
                    participant["hps"],
                    participant["taken"],
                    _json_text(participant["stats"]),
                    participant["data_quality"],
                    timestamp,
                    timestamp,
                ),
            )
            return
        if participant['character_name']:
            connection.execute("""UPDATE encounter_participants SET public_character_name=?,
                public_mode=CASE WHEN public_mode='nickname' THEN public_mode ELSE 'character' END
                WHERE encounter_id=? AND character_hash=?""",
                (participant['character_name'], encounter_id, hashed))
        try:
            stored_stats = json.loads(str(existing['stats_json']))
        except (TypeError, ValueError):
            stored_stats = {}
        if not isinstance(stored_stats, dict):
            stored_stats = {}
        incoming_stats = participant['stats']
        incoming_stats = incoming_stats if isinstance(incoming_stats, Mapping) else {}
        incoming_quality = int(participant["data_quality"])
        stored_quality = int(existing["data_quality"])
        replace_core = incoming_quality >= stored_quality
        merged_stats = dict(stored_stats)
        for field, value in incoming_stats.items():
            if field in {
                'equipment_snapshot', 'targets', 'opening_sequence',
                'skill_timeline',
            }:
                merged_stats[field] = _prefer_richer_detail(
                    stored_stats.get(field), value
                )
            elif field != 'skills' and (replace_core or field not in stored_stats):
                merged_stats[field] = value

        stored_skills = {
            skill.get('skill_id'): skill
            for skill in stored_stats.get('skills', [])
            if isinstance(skill, dict)
        }
        merged_skills: list[dict[str, object]] = []
        for skill in incoming_stats.get('skills', []):
            if not isinstance(skill, Mapping):
                continue
            stored_skill = stored_skills.pop(skill.get('skill_id'), None)
            merged_skill = dict(stored_skill) if isinstance(stored_skill, Mapping) else {}
            if replace_core:
                merged_skill.update(skill)
            else:
                for field, value in skill.items():
                    if field not in merged_skill or merged_skill[field] in (None, "", 0):
                        if value not in (None, ""):
                            merged_skill[field] = value
            if isinstance(stored_skill, Mapping):
                for field in (
                    'effective_healing', 'total_healing', 'overhealing',
                    'healing_skill_count', 'healing_events',
                ):
                    if stored_skill.get(field) and not skill.get(field):
                        merged_skill[field] = stored_skill[field]
            merged_skills.append(merged_skill)
        merged_skills.extend(
            dict(skill) for skill in stored_skills.values() if isinstance(skill, Mapping)
        )
        if merged_skills or 'skills' in incoming_stats or 'skills' in stored_stats:
            merged_stats['skills'] = merged_skills

        if 'skill_timeline_total' in incoming_stats or 'skill_timeline_total' in stored_stats:
            merged_stats['skill_timeline_total'] = max(
                _as_int(incoming_stats.get('skill_timeline_total')),
                _as_int(stored_stats.get('skill_timeline_total')),
                len(merged_stats.get('skill_timeline', [])),
            )
        if incoming_stats.get('skill_timeline_truncated') or stored_stats.get('skill_timeline_truncated'):
            merged_stats['skill_timeline_truncated'] = True
        if merged_stats.get('opening_sequence') is stored_stats.get('opening_sequence'):
            if stored_stats.get('opening_sequence_source'):
                merged_stats['opening_sequence_source'] = stored_stats['opening_sequence_source']

        connection.execute(
            """
            UPDATE encounter_participants SET
                identity_resolved=?, is_ai=?, profession_id=?, damage=?,
                dps=?, hps=?, taken=?, stats_json=?, data_quality=?, last_seen_at=?
            WHERE encounter_id=? AND character_hash=?
            """,
            (
                int(bool(participant["identity_resolved"])) if replace_core else int(existing["identity_resolved"]),
                int(bool(participant["is_ai"])) if replace_core else int(existing["is_ai"]),
                participant["profession_id"] if replace_core else existing["profession_id"],
                participant["damage"] if replace_core else existing["damage"],
                participant["dps"] if replace_core else existing["dps"],
                _as_float(merged_stats.get("hps"), maximum=1e18)
                if "hps" in merged_stats else float(existing["hps"]),
                _as_int(merged_stats.get("taken"))
                if "taken" in merged_stats else int(existing["taken"]),
                _json_text(merged_stats),
                max(incoming_quality, stored_quality),
                timestamp,
                encounter_id,
                hashed,
            ),
        )

    def upload_encounter(
        self,
        connection: sqlite3.Connection,
        character_id: object,
        encounter: object,
        *,
        public_mode: object,
        character_name: object = "",
        app_version: object = "",
    ) -> dict[str, object]:
        uploader_hash = self._hash_character(character_id)
        profile = connection.execute(
            """
            SELECT p.profile_id, p.nickname
            FROM profile_characters c
            JOIN profiles p ON p.profile_id=c.profile_id
            WHERE c.character_hash=?
            """,
            (uploader_hash,),
        ).fetchone()
        mode = _safe_text(public_mode, 24).casefold()
        if mode not in {"anonymous", "nickname", "character"}:
            raise ProfileUploadError("BAD_ENCOUNTER")
        if mode == "nickname" and profile is None:
            raise ProfileUploadError("PROFILE_NOT_FOUND")
        profile_id = str(profile["profile_id"]) if profile is not None else None
        parsed = self._parse_encounter(encounter, uploader_hash=uploader_hash)
        rejection_code = encounter_upload_rejection_code(parsed)
        if rejection_code:
            raise ProfileUploadError(rejection_code)
        uploader = next(
            item for item in parsed["participants"] if bool(item["is_uploader"])
        )
        public_character_name = _safe_text(
            character_name or uploader["character_name"], 48
        )
        if mode == "character" and not public_character_name:
            raise ProfileUploadError("BAD_ENCOUNTER")
        if mode == 'anonymous':
            mode = 'character' if public_character_name else 'nickname' if profile is not None else 'anonymous'
        clean_app_version = _safe_text(app_version, 48)
        qualification = self._qualification(
            parsed, app_version=clean_app_version, uploader=uploader
        )
        timestamp = self.now()
        existing_encounter = self._matching_encounter(connection, parsed)
        created = existing_encounter is None
        if created:
            encounter_id = _new_id("enc_")
            connection.execute(
                """
                INSERT INTO encounters(
                    encounter_id, encounter_fingerprint, client_first_encounter_id,
                    boss_key, boss_name, boss_template_ids_json, dungeon_id,
                    stage_id, difficulty, started_at, ended_at, duration_seconds,
                    team_size, team_total_damage, game_version,
                    participant_set_hash, payload_json, data_quality,
                    statistics_status, ranking_status, validation_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    encounter_id,
                    parsed["fingerprint"],
                    parsed["client_encounter_id"],
                    parsed["boss_key"],
                    parsed["boss_name"],
                    _json_text(parsed["template_ids"]),
                    parsed["dungeon_id"],
                    parsed["stage_id"],
                    parsed["difficulty"],
                    parsed["started_at"],
                    parsed["ended_at"],
                    parsed["duration"],
                    parsed["team_size"],
                    parsed["team_total_damage"],
                    parsed["game_version"],
                    parsed["participant_set_hash"],
                    _json_text(parsed["payload"]),
                    parsed["data_quality"],
                    qualification["statistics_status"],
                    qualification["ranking_status"],
                    _json_text(qualification),
                    timestamp,
                    timestamp,
                ),
            )
        else:
            encounter_id = str(existing_encounter["encounter_id"])
            incoming_quality = int(parsed["data_quality"])
            stored_quality = int(existing_encounter["data_quality"])
            replace_encounter_core = incoming_quality >= stored_quality
            try:
                stored_payload = json.loads(str(existing_encounter["payload_json"]))
            except (TypeError, ValueError):
                stored_payload = {}
            if not isinstance(stored_payload, dict):
                stored_payload = {}
            merged_payload = dict(stored_payload)
            for field, value in parsed["payload"].items():
                if field in {
                    "monster",
                    "targets",
                    "boss_damage",
                    "boss_hp_damage_samples",
                    "module_coverage",
                    "team_dps_timeline",
                }:
                    merged_payload[field] = _prefer_richer_detail(
                        stored_payload.get(field), value
                    )
                elif replace_encounter_core or field not in stored_payload:
                    merged_payload[field] = value
            try:
                stored_template_ids = json.loads(
                    str(existing_encounter["boss_template_ids_json"])
                )
            except (TypeError, ValueError):
                stored_template_ids = []
            stored_template_ids = (
                stored_template_ids if isinstance(stored_template_ids, list) else []
            )
            template_ids = (
                parsed["template_ids"]
                if len(parsed["template_ids"]) >= len(stored_template_ids)
                else stored_template_ids
            )
            boss_name = (
                parsed["boss_name"]
                if parsed["boss_name"]
                and (replace_encounter_core or not existing_encounter["boss_name"])
                else existing_encounter["boss_name"]
            )
            dungeon_id = (
                parsed["dungeon_id"]
                if parsed["dungeon_id"]
                and (replace_encounter_core or not existing_encounter["dungeon_id"])
                else existing_encounter["dungeon_id"]
            )
            stage_id = (
                parsed["stage_id"]
                if parsed["stage_id"]
                and (replace_encounter_core or not existing_encounter["stage_id"])
                else existing_encounter["stage_id"]
            )
            difficulty = (
                parsed["difficulty"]
                if parsed["difficulty"]
                and (replace_encounter_core or not existing_encounter["difficulty"])
                else existing_encounter["difficulty"]
            )
            game_version = (
                parsed["game_version"]
                if parsed["game_version"]
                and (replace_encounter_core or not existing_encounter["game_version"])
                else existing_encounter["game_version"]
            )
            connection.execute(
                """
                UPDATE encounters SET
                    boss_key=?, boss_name=?, boss_template_ids_json=?,
                    dungeon_id=?, stage_id=?, difficulty=?, started_at=?,
                    ended_at=?, duration_seconds=?, team_size=?,
                    team_total_damage=?, game_version=?, participant_set_hash=?,
                    payload_json=?, data_quality=?, statistics_status=?,
                    ranking_status=?, validation_json=?, updated_at=?
                WHERE encounter_id=?
                """,
                (
                    parsed["boss_key"] if replace_encounter_core else existing_encounter["boss_key"],
                    boss_name,
                    _json_text(template_ids),
                    dungeon_id,
                    stage_id,
                    difficulty,
                    parsed["started_at"] if replace_encounter_core else existing_encounter["started_at"],
                    parsed["ended_at"] if replace_encounter_core else existing_encounter["ended_at"],
                    parsed["duration"] if replace_encounter_core else existing_encounter["duration_seconds"],
                    parsed["team_size"] if replace_encounter_core else existing_encounter["team_size"],
                    parsed["team_total_damage"] if replace_encounter_core else existing_encounter["team_total_damage"],
                    game_version,
                    parsed["participant_set_hash"] if replace_encounter_core else existing_encounter["participant_set_hash"],
                    _json_text(merged_payload),
                    max(incoming_quality, stored_quality),
                    qualification["statistics_status"] if replace_encounter_core else existing_encounter["statistics_status"],
                    qualification["ranking_status"] if replace_encounter_core else existing_encounter["ranking_status"],
                    _json_text(qualification) if replace_encounter_core else existing_encounter["validation_json"],
                    timestamp,
                    encounter_id,
                ),
            )
        for participant in parsed["participants"]:
            self._upsert_participant(connection, encounter_id, participant, timestamp)
        connection.execute(
            """
            UPDATE encounter_participants SET
                profile_id=?, public_mode=?, public_character_name=COALESCE(NULLIF(?, ''), public_character_name), last_seen_at=?
            WHERE encounter_id=? AND character_hash=?
            """,
            (
                profile_id,
                mode,
                public_character_name,
                timestamp,
                encounter_id,
                uploader_hash,
            ),
        )
        compact_request = {
            "character": uploader_hash,
            "public_mode": mode,
            "encounter": parsed,
        }
        payload_hash = hashlib.sha256(
            _json_text(compact_request).encode("utf-8")
        ).hexdigest()
        existing_upload = connection.execute(
            """
            SELECT upload_id FROM uploads
            WHERE encounter_id=? AND uploader_character_hash=?
            """,
            (encounter_id, uploader_hash),
        ).fetchone()
        duplicate = existing_upload is not None
        upload_id = (
            str(existing_upload["upload_id"])
            if existing_upload is not None
            else _new_id("upl_")
        )
        connection.execute(
            """
            INSERT INTO uploads(
                upload_id, encounter_id, uploader_character_hash, profile_id,
                public_mode, public_character_name, uploaded_at, last_uploaded_at,
                app_version, game_version, payload_hash, data_completeness,
                verification_status, upload_status, statistics_status,
                ranking_status, validation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', 'uploaded', ?, ?, ?)
            ON CONFLICT(encounter_id, uploader_character_hash) DO UPDATE SET
                profile_id=excluded.profile_id,
                public_mode=excluded.public_mode,
                public_character_name=excluded.public_character_name,
                last_uploaded_at=excluded.last_uploaded_at,
                app_version=excluded.app_version,
                game_version=excluded.game_version,
                payload_hash=excluded.payload_hash,
                data_completeness=excluded.data_completeness,
                verification_status=excluded.verification_status,
                upload_status=excluded.upload_status,
                statistics_status=excluded.statistics_status,
                ranking_status=excluded.ranking_status,
                validation_json=excluded.validation_json
            """,
            (
                upload_id,
                encounter_id,
                uploader_hash,
                profile_id,
                mode,
                public_character_name if mode == "character" else "",
                timestamp,
                timestamp,
                clean_app_version,
                parsed["game_version"],
                payload_hash,
                parsed["completeness"],
                qualification["statistics_status"],
                qualification["ranking_status"],
                _json_text(qualification),
            ),
        )
        aggregate = connection.execute(
            """
            SELECT
                MAX(statistics_status='included') AS included,
                MAX(ranking_status='eligible') AS eligible
            FROM uploads WHERE encounter_id=?
            """,
            (encounter_id,),
        ).fetchone()
        encounter_statistics = "included" if aggregate and int(aggregate["included"] or 0) else "not_eligible"
        encounter_ranking = "eligible" if aggregate and int(aggregate["eligible"] or 0) else "not_eligible"
        connection.execute(
            """
            UPDATE encounters SET statistics_status=?, ranking_status=?, updated_at=?
            WHERE encounter_id=?
            """,
            (encounter_statistics, encounter_ranking, timestamp, encounter_id),
        )
        rank = None
        if qualification["ranking_status"] == "eligible":
            rank = 1 + int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM uploads other_upload
                    JOIN encounters other_encounter
                        ON other_encounter.encounter_id=other_upload.encounter_id
                    JOIN encounter_participants other_participant
                        ON other_participant.encounter_id=other_upload.encounter_id
                        AND other_participant.character_hash=other_upload.uploader_character_hash
                    WHERE other_upload.ranking_status='eligible'
                        AND other_encounter.boss_key=?
                        AND other_encounter.stage_id=?
                        AND other_participant.profession_id=?
                        AND other_participant.dps>?
                    """,
                    (
                        parsed["boss_key"],
                        parsed["stage_id"],
                        uploader["profession_id"],
                        uploader["dps"],
                    ),
                ).fetchone()[0]
            )
        upload_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM uploads WHERE encounter_id=?",
                (encounter_id,),
            ).fetchone()[0]
        )
        return {
            "upload_id": upload_id,
            "encounter_id": encounter_id,
            "created_encounter": created,
            "duplicate_upload": duplicate,
            "upload_status": "uploaded",
            "statistics_status": qualification["statistics_status"],
            "ranking_status": qualification["ranking_status"],
            "rank": rank,
            "validation_reasons": list(qualification["reasons"]),
            "upload_source_count": upload_count,
        }

    def profile_uploads(
        self,
        connection: sqlite3.Connection,
        character_id: object,
        *,
        scope: str = "profile",
        limit: int = 100,
    ) -> dict[str, object]:
        hashed = self._hash_character(character_id)
        linked = connection.execute(
            """
            SELECT c.profile_id, p.nickname
            FROM profile_characters c JOIN profiles p ON p.profile_id=c.profile_id
            WHERE c.character_hash=?
            """,
            (hashed,),
        ).fetchone()
        if linked is None:
            raise ProfileUploadError("PROFILE_NOT_FOUND")
        params: list[object] = [str(linked["profile_id"])]
        character_clause = ""
        if scope == "character":
            character_clause = " AND u.uploader_character_hash=?"
            params.append(hashed)
        params.append(min(200, max(1, int(limit))))
        rows = connection.execute(
            f"""
            SELECT u.upload_id, u.encounter_id, u.last_uploaded_at,
                u.upload_status, u.statistics_status, u.ranking_status,
                e.boss_name, e.stage_id, e.ended_at,
                p.damage, p.dps, p.profession_id
            FROM uploads u
            JOIN encounters e ON e.encounter_id=u.encounter_id
            JOIN encounter_participants p
                ON p.encounter_id=u.encounter_id
                AND p.character_hash=u.uploader_character_hash
            WHERE u.profile_id=? {character_clause}
            ORDER BY u.last_uploaded_at DESC LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return {
            "profile": self._profile_payload(linked),
            "scope": "character" if scope == "character" else "profile",
            "uploads": [
                {
                    "upload_id": str(row["upload_id"]),
                    "encounter_id": str(row["encounter_id"]),
                    "uploaded_at": float(row["last_uploaded_at"]),
                    "ended_at": float(row["ended_at"]),
                    "boss_name": str(row["boss_name"]),
                    "stage_id": int(row["stage_id"]),
                    "profession_id": int(row["profession_id"]),
                    "damage": int(row["damage"]),
                    "dps": float(row["dps"]),
                    "upload_status": str(row["upload_status"]),
                    "statistics_status": str(row["statistics_status"]),
                    "ranking_status": str(row["ranking_status"]),
                }
                for row in rows
            ],
        }

    @staticmethod
    def _public_participant_name(row: sqlite3.Row, anonymous_number: int) -> tuple[str, str, str]:
        mode = str(row["public_mode"])
        if mode == "nickname" and row["nickname"]:
            return str(row["nickname"]), "nickname", str(row["profile_id"] or "")
        if row["public_character_name"]:
            return str(row["public_character_name"]), "character", str(row["profile_id"] or "")
        if row['nickname']:
            return str(row['nickname']), 'nickname', str(row['profile_id'] or '')
        return f"未记录姓名 · 成员{anonymous_number:02d}", "unrecorded", ""

    @staticmethod
    def critical_luck_model(stats: Mapping[str, object], damage: int) -> dict[str, object] | None:
        """Use the desktop crit-placement estimate, keeping observed crit counts fixed."""
        groups: dict[int, list[dict[str, object]]] = {}
        for event in stats.get('skill_timeline', []) if isinstance(stats.get('skill_timeline'), list) else []:
            if isinstance(event, dict) and isinstance(event.get('critical'), bool) and _as_int(event.get('damage')) > 0:
                groups.setdefault(_as_int(event.get('skill_id')), []).append(event)
        count = sum(map(len, groups.values()))
        if count < 8:
            return None
        ratios = {}
        for sid, events in groups.items():
            crit = [float(e['damage']) for e in events if e['critical']]
            normal = [float(e['damage']) for e in events if not e['critical']]
            if crit and normal:
                ratios[sid] = min(2.8, max(1.1, median(crit) / median(normal)))
        multiplier = median(ratios.values()) if ratios else 1.5
        observed = expected = variance = 0.0
        for sid, events in groups.items():
            n, k = len(events), sum(e['critical'] for e in events)
            ratio = ratios.get(sid, multiplier)
            bases = [float(e['damage']) / ratio if e['critical'] else float(e['damage']) for e in events]
            deltas = [base * (ratio - 1) for base in bases]
            observed += sum(float(e['damage']) for e in events)
            expected += sum(bases) + sum(deltas) * k / n
            if n > 1 and 0 < k < n:
                mean = sum(deltas) / n
                variance += k * (n - k) / (n * (n - 1)) * sum((d - mean) ** 2 for d in deltas)
        if expected <= 0:
            return None
        sample_damage = observed
        if damage > 0 and observed > 0:
            scale = damage / observed
            observed *= scale
            expected *= scale
            variance *= scale * scale
        sigma = max(math.sqrt(variance), expected * .0008, 1)
        z = (observed - expected) / sigma
        percentile = min(.999, max(.001, .5 * (1 + math.erf(z / math.sqrt(2)))))
        verdict = '非常好运' if percentile >= .9 else '略有好运' if percentile >= .67 else '接近期望' if percentile > .33 else '略显倒霉' if percentile > .1 else '非常倒霉'
        return {'observed': observed, 'expected': expected, 'sigma': sigma, 'z_score': z,
                'percentile': percentile, 'verdict': verdict, 'extra': observed - expected,
                'extra_ratio': (observed - expected) / expected, 'known_hits': count,
                'critical_hits': sum(e['critical'] for events in groups.values() for e in events),
                'critical_rate': sum(e['critical'] for events in groups.values() for e in events) / count,
                'multiplier': multiplier, 'sample_damage': sample_damage,
                'coverage': min(1, sample_damage / damage) if damage > 0 else None}

    @staticmethod
    def _death_summary(stats: list[Mapping[str, object]], team_size: int = 0) -> dict[str, object]:
        known = [row['deaths'] for row in stats if row.get('deaths') is not None]
        recorded = sum(_as_int(value) for value in known)
        total = max(len(stats), team_size)
        return {'team_deaths': recorded if total and len(known) == total else None,
                'recorded_deaths': recorded if known else None,
                'death_recorded_members': len(known), 'death_total_members': total}

    def public_encounter(
        self, connection: sqlite3.Connection, encounter_id: object
    ) -> dict[str, object] | None:
        public_id = _safe_text(encounter_id, 64)
        encounter = connection.execute(
            f"SELECT e.* FROM encounters e WHERE e.encounter_id=? AND {PUBLIC_NO_PROJECTION_SQL}",
            (public_id,),
        ).fetchone()
        if encounter is None:
            return None
        rows = connection.execute(
            """
            SELECT ep.*, pr.nickname
            FROM encounter_participants ep
            LEFT JOIN profiles pr ON pr.profile_id=ep.profile_id
            WHERE ep.encounter_id=? ORDER BY ep.slot_number
            """,
            (public_id,),
        ).fetchall()
        participants: list[dict[str, object]] = []
        for row in rows:
            name, mode, profile_id = self._public_participant_name(
                row, int(row["slot_number"])
            )
            try:
                stats = json.loads(str(row["stats_json"]))
            except (TypeError, ValueError):
                stats = {}
            if isinstance(stats, dict):
                stats['critical_luck'] = self.critical_luck_model(stats, int(row['damage']))
            participants.append(
                {
                    "slot": int(row["slot_number"]),
                    "display_name": name,
                    "public_mode": mode,
                    "profile_id": profile_id,
                    "profession_id": int(row["profession_id"]),
                    "is_ai": bool(row["is_ai"]),
                    "damage": int(row["damage"]),
                    "dps": float(row["dps"]),
                    "hps": float(row["hps"]),
                    "taken": int(row["taken"]),
                    "deaths": stats.get('deaths') if isinstance(stats, dict) else None,
                    "stats": stats if isinstance(stats, dict) else {},
                }
            )
        try:
            payload = json.loads(str(encounter["payload_json"]))
        except (TypeError, ValueError):
            payload = {}
        return {
            "encounter_id": str(encounter["encounter_id"]),
            "boss_name": str(encounter["boss_name"]),
            "boss_template_ids": json.loads(str(encounter["boss_template_ids_json"])),
            "dungeon_id": int(encounter["dungeon_id"]),
            "stage_id": int(encounter["stage_id"]),
            "difficulty": str(encounter["difficulty"]),
            "started_at": float(encounter["started_at"]),
            "ended_at": float(encounter["ended_at"]),
            "duration_seconds": float(encounter["duration_seconds"]),
            "team_size": int(encounter["team_size"]),
            "team_total_damage": int(encounter["team_total_damage"]),
            "statistics_status": str(encounter["statistics_status"]),
            "ranking_status": str(encounter["ranking_status"]),
            "game_version": str(encounter["game_version"]),
            "qualification_reasons": json.loads(str(encounter["validation_json"])).get("reasons", []),
            "data": payload if isinstance(payload, dict) else {},
            "participants": participants,
            **self._death_summary([participant['stats'] for participant in participants], int(encounter['team_size'])),
        }

    def public_history(
        self, connection: sqlite3.Connection, *, query: object = "",
        boss: object = "", profession: object = 0, difficulty: object = "",
        eligibility: object = "all", started_after: object = 0,
        ended_before: object = 0, limit: object = 25, offset: object = 0,
    ) -> dict[str, object]:
        """Browse stored battles independently of leaderboard qualification."""
        needle = _safe_text(query, 48).strip()
        conditions = ["EXISTS (SELECT 1 FROM uploads u WHERE u.encounter_id=e.encounter_id)",
                      PUBLIC_NO_PROJECTION_SQL]
        params: list[object] = []
        boss_name = _safe_text(boss, 96)
        if boss_name:
            conditions.append("e.boss_name=?")
            params.append(boss_name)
        selected_profession = _as_int(profession, maximum=2_000_000_000)
        if selected_profession:
            conditions.append("e.encounter_id IN (SELECT p.encounter_id FROM encounter_participants p WHERE p.profession_id=?)")
            params.append(selected_profession)
        selected_difficulty = _safe_text(difficulty, 32).casefold()
        if selected_difficulty and selected_difficulty != "all":
            conditions.append("e.difficulty=?")
            params.append(selected_difficulty)
        if eligibility == "included":
            conditions.append("e.statistics_status='included'")
        elif eligibility == "not_eligible":
            conditions.append("e.statistics_status!='included'")
        after = _as_float(started_after, maximum=4_102_444_800)
        before = _as_float(ended_before, maximum=4_102_444_800)
        if after:
            conditions.append("e.ended_at>=?")
            params.append(after)
        if before:
            conditions.append("e.ended_at<?")
            params.append(before)
        if needle:
            # Captured character names and profile nicknames are searchable.
            conditions.append("""e.encounter_id IN (
                SELECT p.encounter_id FROM encounter_participants p
                WHERE p.public_character_name!=''
                    AND instr(lower(p.public_character_name), lower(?))>0
                UNION
                SELECT p.encounter_id FROM encounter_participants p WHERE p.profile_id IN (
                    SELECT pr.profile_id FROM profiles pr WHERE instr(lower(pr.nickname), lower(?))>0)
            )""")
            params.extend((needle, needle))
        where = " AND ".join(conditions)
        total = connection.execute(f"SELECT COUNT(*) FROM encounters e WHERE {where}", params).fetchone()[0]
        page_size = _as_int(limit, minimum=1, maximum=50)
        page_offset = _as_int(offset, maximum=1_000_000)
        rows = connection.execute(
            f"SELECT e.* FROM encounters e WHERE {where} ORDER BY e.ended_at DESC, e.encounter_id DESC LIMIT ? OFFSET ?",
            (*params, page_size, page_offset),
        ).fetchall()
        members: dict[str, list[sqlite3.Row]] = {}
        if rows:
            ids = [str(row['encounter_id']) for row in rows]
            placeholders = ','.join('?' for _ in ids)
            for member in connection.execute(
                f"""SELECT p.encounter_id, p.slot_number, p.public_mode,
                    p.public_character_name, p.profile_id, p.profession_id,
                    p.dps, p.damage, p.stats_json, pr.nickname FROM encounter_participants p
                    LEFT JOIN profiles pr ON pr.profile_id=p.profile_id
                    WHERE p.encounter_id IN ({placeholders}) ORDER BY p.slot_number""", ids,
            ):
                members.setdefault(str(member['encounter_id']), []).append(member)
        records = []
        for row in rows:
            public_members = []
            for member in members.get(str(row['encounter_id']), []):
                name, mode, profile_id = self._public_participant_name(member, int(member['slot_number']))
                public_members.append((member, name, mode, profile_id))
            selected = next((item for item in public_members if item[2] != 'unrecorded'
                             and (not needle or needle.casefold() in item[1].casefold()
                                  or needle.casefold() in str(item[0]['public_character_name']).casefold()
                                  or needle.casefold() in str(item[0]['nickname'] or '').casefold())), None)
            if selected is None and public_members:
                selected = public_members[0]
            payload = json.loads(str(row['payload_json']))
            validation = json.loads(str(row['validation_json']))
            record = {
                'encounter_id': str(row['encounter_id']), 'boss_name': str(row['boss_name']),
                'stage_id': int(row['stage_id']), 'difficulty': str(row['difficulty']),
                'ended_at': float(row['ended_at']), 'duration_seconds': float(row['duration_seconds']),
                'team_size': int(row['team_size']), 'team_total_damage': int(row['team_total_damage']),
                'team_dps': payload.get('team_dps'), 'result': payload.get('result', 'undetermined'),
                'completion_confirmed': bool(payload.get('completion_confirmed')),
                'statistics_status': str(row['statistics_status']), 'ranking_status': str(row['ranking_status']),
                'qualification_reasons': validation.get('reasons', []), 'game_version': str(row['game_version']),
                **self._death_summary([json.loads(str(member['stats_json'])) for member in members.get(str(row['encounter_id']), [])], int(row['team_size'])),
            }
            if selected:
                member, name, mode, profile_id = selected
                record.update(display_name=name, public_mode=mode, profile_id=profile_id,
                              profession_id=int(member['profession_id']), slot=int(member['slot_number']),
                              dps=float(member['dps']), damage=int(member['damage']))
                record['deaths'] = json.loads(str(member['stats_json'])).get('deaths')
            records.append(record)
        return {'records': records, 'total': int(total), 'limit': page_size, 'offset': page_offset}

    def public_catalog(self, connection: sqlite3.Connection) -> dict[str, object]:
        rows = connection.execute(f"""SELECT boss_name, COUNT(*) AS records,
            SUM(statistics_status='included') AS included, MAX(ended_at) AS last_at
            FROM encounters e WHERE EXISTS (SELECT 1 FROM uploads u WHERE u.encounter_id=e.encounter_id)
                AND {PUBLIC_NO_PROJECTION_SQL}
            GROUP BY boss_name ORDER BY included DESC, last_at DESC""").fetchall()
        return {
            'bosses': [{'name': str(row['boss_name']), 'records': int(row['records']),
                        'included': int(row['included']), 'last_at': float(row['last_at'])} for row in rows],
            'difficulties': [str(row[0]) for row in connection.execute(
                f"SELECT DISTINCT e.difficulty FROM encounters e WHERE e.difficulty!='' AND {PUBLIC_NO_PROJECTION_SQL} ORDER BY e.difficulty")],
        }

    def public_statistics(self, connection: sqlite3.Connection) -> dict[str, object]:
        summary = connection.execute(
            f"""
            SELECT COUNT(*) AS encounters,
                COALESCE(SUM(team_total_damage), 0) AS total_damage,
                COALESCE(SUM(duration_seconds), 0) AS duration_seconds
            FROM encounters e WHERE e.statistics_status='included'
                AND {PUBLIC_NO_PROJECTION_SQL}
            """
        ).fetchone()
        uploads = connection.execute(
            f"""SELECT COUNT(*) FROM uploads u
                JOIN encounters e ON e.encounter_id=u.encounter_id
                WHERE u.statistics_status='included' AND {PUBLIC_NO_PROJECTION_SQL}"""
        ).fetchone()[0]
        history = connection.execute(f"""SELECT COUNT(*) AS encounters, MAX(ended_at) AS last_at
            FROM encounters e WHERE EXISTS (SELECT 1 FROM uploads u WHERE u.encounter_id=e.encounter_id)
                AND {PUBLIC_NO_PROJECTION_SQL}""").fetchone()
        last_uploaded = connection.execute(f"""SELECT MAX(u.last_uploaded_at) FROM uploads u
            JOIN encounters e ON e.encounter_id=u.encounter_id
            WHERE {PUBLIC_NO_PROJECTION_SQL}""").fetchone()[0]
        return {
            "encounters": int(summary["encounters"] if summary else 0),
            "uploads": int(uploads),
            "total_damage": int(summary["total_damage"] if summary else 0),
            "duration_seconds": float(summary["duration_seconds"] if summary else 0),
            "history_encounters": int(history['encounters']),
            "last_encounter_at": history['last_at'],
            "last_uploaded_at": last_uploaded,
        }

    def public_performance(
        self,
        connection: sqlite3.Connection,
        *,
        boss: object = "drill",
        difficulty: object = "all",
        metric: object = "dps",
        rating_basis: object = "extraordinary",
        min_rating: object = 0,
        max_rating: object = 200_000,
        game_version: object = "all",
        sort_by: object = "p50",
        calibrated: bool = False,
        profile_id: object = "",
    ) -> dict[str, object]:
        """Return privacy-safe profession percentiles for public raid insights."""

        requested_boss = _safe_text(boss, 128)
        if requested_boss.startswith('name:'):
            boss_key = requested_boss
            boss_config = {'name': requested_boss[5:], 'dungeon_name': '',
                           'stage_ids': (), 'aliases': (requested_boss[5:],)}
        else:
            boss_key = _public_performance_boss_key(boss)
            boss_config = PUBLIC_PERFORMANCE_BOSSES[boss_key]
        selected_difficulty = _public_performance_difficulty(difficulty)
        selected_metric = _safe_text(metric, 24).casefold()
        if selected_metric not in PUBLIC_PERFORMANCE_METRICS:
            selected_metric = "dps"
        selected_rating_basis = _safe_text(rating_basis, 24).casefold()
        if selected_rating_basis not in {"extraordinary", "equipment"}:
            selected_rating_basis = "extraordinary"
        selected_version = _safe_text(game_version, 48)
        if not selected_version:
            selected_version = "all"
        selected_sort = _safe_text(sort_by, 24).casefold()
        if selected_sort not in PUBLIC_PERFORMANCE_SORTS:
            selected_sort = "p50"
        selected_profile_id = _safe_text(profile_id, 64)
        rating_min = _as_int(min_rating, maximum=1_000_000)
        rating_max = _as_int(max_rating, maximum=1_000_000)
        if rating_max <= 0:
            rating_max = 200_000
        if rating_min > rating_max:
            rating_min, rating_max = rating_max, rating_min

        stage_ids = tuple(int(value) for value in boss_config["stage_ids"])
        aliases = tuple(str(value) for value in boss_config["aliases"])
        stage_placeholders = ",".join("?" for _ in stage_ids) or "NULL"
        alias_placeholders = ",".join("?" for _ in aliases)
        rows = connection.execute(
            f"""
            SELECT e.encounter_id, e.boss_name, e.stage_id, e.difficulty,
                e.ended_at, e.game_version, e.payload_json,
                ep.profession_id, ep.damage, ep.dps, ep.profile_id,
                ep.stats_json
            FROM encounters e
            JOIN encounter_participants ep ON ep.encounter_id=e.encounter_id
            WHERE e.statistics_status='included'
                AND {PUBLIC_NO_PROJECTION_SQL}
                AND ep.identity_resolved=1
                AND ep.is_ai=0
                AND ep.profession_id>0
                AND (e.stage_id IN ({stage_placeholders})
                    OR e.boss_name IN ({alias_placeholders}))
            ORDER BY e.ended_at ASC, ep.slot_number ASC
            """,
            stage_ids + aliases,
        ).fetchall()

        candidates: list[dict[str, object]] = []
        available_difficulties: set[str] = set()
        available_versions: set[str] = set()
        extraordinary_available = False
        equipment_available = False
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if str(payload.get("result", "")).casefold() != "defeated":
                continue
            if not bool(payload.get("completion_confirmed")):
                continue
            raw_difficulty = _safe_text(row["difficulty"], 24).casefold()
            record_difficulty = (
                "normal" if not raw_difficulty else _public_performance_difficulty(raw_difficulty)
            )
            if record_difficulty == "all":
                record_difficulty = raw_difficulty or "normal"
            raw_version = _safe_text(row["game_version"], 48)
            record_version = raw_version or "unknown"
            available_difficulties.add(record_difficulty)
            available_versions.add(record_version)
            if selected_difficulty != "all" and record_difficulty != selected_difficulty:
                continue
            if selected_version != "all" and record_version != selected_version:
                continue
            try:
                stats = json.loads(str(row["stats_json"] or "{}"))
            except (TypeError, ValueError):
                stats = {}
            if not isinstance(stats, dict):
                stats = {}
            extraordinary_rating = _as_int(
                stats.get("extraordinary_rating"), maximum=1_000_000
            )
            equipment_rating = _as_int(
                stats.get("equipment_rating", stats.get("equipment_score")),
                maximum=1_000_000,
            )
            extraordinary_available = extraordinary_available or extraordinary_rating > 0
            equipment_available = equipment_available or equipment_rating > 0
            rating = (
                equipment_rating
                if selected_rating_basis == "equipment"
                else extraordinary_rating
            )
            metric_value = (
                float(row["damage"])
                if selected_metric == "boss_damage"
                else float(row["dps"])
            )
            if metric_value <= 0 or rating <= 0:
                continue
            candidates.append(
                {
                    "encounter_id": str(row["encounter_id"]),
                    "profession_id": int(row["profession_id"]),
                    "profile_id": str(row["profile_id"] or ""),
                    "rating": rating,
                    "metric": metric_value,
                    "ended_at": float(row["ended_at"]),
                }
            )

        available_ratings = sorted(int(row["rating"]) for row in candidates)
        filtered = [
            row
            for row in candidates
            if rating_min <= int(row["rating"]) <= rating_max
        ]
        grouped: dict[int, list[dict[str, object]]] = {}
        for row in filtered:
            grouped.setdefault(int(row["profession_id"]), []).append(row)

        groups: list[dict[str, object]] = []
        calibration_applied_groups = 0
        for profession_id, profession_rows in grouped.items():
            ratings = [float(row["rating"]) for row in profession_rows]
            target_rating = _linear_percentile(sorted(ratings), 0.5)
            slope = 0.0
            calibration_applied = False
            if calibrated and len(profession_rows) >= 4 and max(ratings) - min(ratings) >= 1_000:
                logs = [math.log(max(1.0, float(row["metric"]))) for row in profession_rows]
                mean_rating = sum(ratings) / len(ratings)
                mean_log = sum(logs) / len(logs)
                variance = sum((value - mean_rating) ** 2 for value in ratings)
                if variance > 0:
                    raw_slope = sum(
                        (rating - mean_rating) * (logged - mean_log)
                        for rating, logged in zip(ratings, logs)
                    ) / variance
                    slope = min(math.log(2.0) / 20_000.0, max(0.0, raw_slope))
                    calibration_applied = slope > 0
            for row in profession_rows:
                value = float(row["metric"])
                if calibration_applied:
                    value *= math.exp(slope * (target_rating - float(row["rating"])))
                row["display_metric"] = value
            if calibration_applied:
                calibration_applied_groups += 1

            values = sorted(float(row["display_metric"]) for row in profession_rows)
            percentiles = {
                "p10": _linear_percentile(values, 0.10),
                "p25": _linear_percentile(values, 0.25),
                "p50": _linear_percentile(values, 0.50),
                "p75": _linear_percentile(values, 0.75),
                "p90": _linear_percentile(values, 0.90),
                "best": float(values[-1]),
            }
            best_row = max(
                profession_rows, key=lambda item: float(item["display_metric"])
            )
            mine = None
            if selected_profile_id:
                matching = [
                    row
                    for row in profession_rows
                    if hmac.compare_digest(str(row["profile_id"]), selected_profile_id)
                ]
                if matching:
                    mine_row = max(matching, key=lambda item: float(item["ended_at"]))
                    mine_value = float(mine_row["display_metric"])
                    less = sum(1 for value in values if value < mine_value)
                    equal = sum(1 for value in values if value == mine_value)
                    percentile = 100.0 * (less + equal * 0.5) / len(values)
                    mine = {
                        "encounter_id": str(mine_row["encounter_id"]),
                        "value": mine_value,
                        "raw_value": float(mine_row["metric"]),
                        "percentile": percentile,
                        "exceeds_percent": 100.0 * less / len(values),
                        "gap_to_p75": percentiles["p75"] - mine_value,
                        "gap_to_p90": percentiles["p90"] - mine_value,
                    }
            groups.append(
                {
                    "profession_id": profession_id,
                    "sample_count": len(profession_rows),
                    **percentiles,
                    "target_rating": target_rating,
                    "calibration_applied": calibration_applied,
                    "best_record": {
                        "encounter_id": str(best_row["encounter_id"]),
                        "value": float(best_row["display_metric"]),
                        "raw_value": float(best_row["metric"]),
                        "rating": int(best_row["rating"]),
                        "ended_at": float(best_row["ended_at"]),
                    },
                    "mine": mine,
                }
            )

        groups.sort(
            key=lambda row: (
                float(row[selected_sort]),
                int(row["sample_count"]),
                -int(row["profession_id"]),
            ),
            reverse=True,
        )
        encounter_count = len({str(row["encounter_id"]) for row in filtered})
        updated_at = max((float(row["ended_at"]) for row in candidates), default=0.0)
        return {
            "source": "real_uploads",
            "bosses": [
                {
                    "key": key,
                    "name": str(config["name"]),
                    "dungeon_name": str(config["dungeon_name"]),
                    "stage_ids": [int(value) for value in config["stage_ids"]],
                }
                for key, config in PUBLIC_PERFORMANCE_BOSSES.items()
            ],
            "selection": {
                "boss": boss_key,
                "boss_name": str(boss_config["name"]),
                "dungeon_name": str(boss_config["dungeon_name"]),
                "difficulty": selected_difficulty,
                "metric": selected_metric,
                "rating_basis": selected_rating_basis,
                "min_rating": rating_min,
                "max_rating": rating_max,
                "game_version": selected_version,
                "sort": selected_sort,
                "calibrated": bool(calibrated),
            },
            "availability": {
                "difficulties": sorted(available_difficulties),
                "game_versions": sorted(available_versions),
                "extraordinary_rating": extraordinary_available,
                "equipment_rating": equipment_available,
                "rating_min": available_ratings[0] if available_ratings else None,
                "rating_max": available_ratings[-1] if available_ratings else None,
            },
            "total_samples": len(filtered),
            "total_encounters": encounter_count,
            "updated_at": updated_at,
            "groups": groups,
            "calibration": {
                "requested": bool(calibrated),
                "applied_groups": calibration_applied_groups,
                "minimum_samples_per_profession": 4,
            },
        }

    def public_leaderboards(
        self, connection: sqlite3.Connection, *, limit: int = 100
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            f"""
            SELECT u.encounter_id, u.public_mode, u.public_character_name,
                u.profile_id, pr.nickname, e.boss_name, e.stage_id, e.ended_at,
                ep.profession_id, ep.damage, ep.dps,
                ROW_NUMBER() OVER (
                    PARTITION BY e.boss_key, e.stage_id, ep.profession_id
                    ORDER BY ep.dps DESC, e.ended_at ASC
                ) AS rank_number
            FROM uploads u
            JOIN encounters e ON e.encounter_id=u.encounter_id
            JOIN encounter_participants ep
                ON ep.encounter_id=u.encounter_id
                AND ep.character_hash=u.uploader_character_hash
            LEFT JOIN profiles pr ON pr.profile_id=u.profile_id
            WHERE u.ranking_status='eligible'
                AND {PUBLIC_NO_PROJECTION_SQL}
            ORDER BY e.ended_at DESC, ep.dps DESC
            LIMIT ?
            """,
            (min(500, max(1, int(limit))),),
        ).fetchall()
        result = []
        for row in rows:
            display_name, public_mode, profile_id = self._public_participant_name(row, 0)
            if public_mode == "unrecorded":
                display_name = "未记录姓名"
            result.append({
                "encounter_id": str(row["encounter_id"]),
                "profile_id": profile_id,
                "display_name": display_name,
                "public_mode": public_mode,
                "boss_name": str(row["boss_name"]),
                "stage_id": int(row["stage_id"]),
                "profession_id": int(row["profession_id"]),
                "damage": int(row["damage"]),
                "dps": float(row["dps"]),
                "rank": int(row["rank_number"]),
                "ended_at": float(row["ended_at"]),
            })
        return result


def _record_actor_identity_map(record: Mapping[str, object]) -> dict[int, str]:
    identities: dict[int, str] = {}

    def remember(actor_value: object, token_value: object) -> None:
        actor_id = _as_int(actor_value, minimum=-(1 << 63), maximum=(1 << 63) - 1)
        if actor_id == 0 or not token_value:
            return
        try:
            identity = canonical_character_identity(token_value, allow_ai=True)
        except ProfileUploadError:
            return
        previous = identities.get(actor_id)
        if previous in (None, identity.canonical):
            identities[actor_id] = identity.canonical

    raw_participants = record.get("participants")
    if isinstance(raw_participants, list):
        for participant in raw_participants:
            if isinstance(participant, dict):
                remember(
                    participant.get("actor_id"),
                    participant.get("_character_id", participant.get("character_id")),
                )
    raw_identities = record.get("participant_identities")
    if isinstance(raw_identities, list):
        for item in raw_identities:
            if isinstance(item, dict):
                remember(
                    item.get("actor_id"),
                    item.get("character_id", item.get("user_token")),
                )
    accounting = record.get("damage_accounting")
    if isinstance(accounting, dict):
        validations = accounting.get("stage_summary_validations")
        if isinstance(validations, list):
            for validation in validations:
                if not isinstance(validation, dict):
                    continue
                actors = validation.get("actors")
                if isinstance(actors, list):
                    for actor in actors:
                        if isinstance(actor, dict):
                            remember(actor.get("actor_id"), actor.get("user_token"))
                merges = validation.get("actor_merges")
                if isinstance(merges, list):
                    for merge in merges:
                        if isinstance(merge, dict):
                            remember(merge.get("to_actor_id"), merge.get("user_token"))
        merges = accounting.get("actor_merges")
        if isinstance(merges, list):
            for merge in merges:
                if isinstance(merge, dict):
                    remember(merge.get("to_actor_id"), merge.get("user_token"))
    return identities


def recorded_self_character_id(record: object) -> str:
    """Resolve the uploader from the archived encounter, independent of live state."""

    if not isinstance(record, Mapping):
        raise ProfileUploadError("BAD_ENCOUNTER")
    participants = record.get("participants")
    if not isinstance(participants, list):
        raise ProfileUploadError("BAD_ENCOUNTER")
    self_actor_ids = [
        _as_int(
            participant.get("actor_id"),
            minimum=-(1 << 63),
            maximum=(1 << 63) - 1,
        )
        for participant in participants
        if isinstance(participant, Mapping) and bool(participant.get("is_self"))
    ]
    self_actor_ids = [actor_id for actor_id in self_actor_ids if actor_id]
    if len(self_actor_ids) != 1:
        raise ProfileUploadError("UPLOADER_NOT_IN_ENCOUNTER")

    mapped_identity = _record_actor_identity_map(record).get(self_actor_ids[0], "")
    top_level_identity = ""
    if record.get("self_character_id"):
        try:
            top_level_identity = canonical_character_identity(
                record.get("self_character_id")
            ).canonical
        except ProfileUploadError:
            top_level_identity = ""
    if (
        mapped_identity
        and top_level_identity
        and mapped_identity != top_level_identity
    ):
        raise ProfileUploadError("UPLOADER_NOT_IN_ENCOUNTER")
    resolved = mapped_identity or top_level_identity
    if not resolved:
        raise ProfileUploadError("UPLOADER_NOT_IN_ENCOUNTER")
    return canonical_character_identity(resolved).canonical


def _record_stage_actor_rows(record: Mapping[str, object]) -> dict[int, dict[str, object]]:
    result: dict[int, dict[str, object]] = {}
    accounting = record.get("damage_accounting")
    if not isinstance(accounting, dict):
        return result
    validations = accounting.get("stage_summary_validations")
    if not isinstance(validations, list):
        return result
    ordered = sorted(
        (item for item in validations if isinstance(item, dict)),
        key=lambda item: _as_int(item.get("filetime_100ns")),
    )
    for validation in ordered:
        actors = validation.get("actors")
        if not isinstance(actors, list):
            continue
        for actor in actors:
            if not isinstance(actor, dict):
                continue
            actor_id = _as_int(
                actor.get("actor_id"), minimum=-(1 << 63), maximum=(1 << 63) - 1
            )
            if actor_id:
                result[actor_id] = dict(actor)
    return result


def _timeline_rows_by_actor(record: Mapping[str, object]) -> dict[int, list[dict[str, object]]]:
    event_log = record.get("event_log")
    if not isinstance(event_log, dict):
        return {}
    columns = event_log.get("columns")
    rows = event_log.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return {}
    positions = {str(name): index for index, name in enumerate(columns)}
    required = {"time_ms", "actor_id", "skill_id", "damage"}
    if not required.issubset(positions):
        return {}
    target_metadata: dict[int, dict[str, object]] = {}
    target_collections: list[object] = [record.get("targets")]
    monster = record.get("monster")
    if isinstance(monster, Mapping):
        target_collections.append([monster])
    for collection_name in ("participants", "healers"):
        collection = record.get(collection_name)
        if not isinstance(collection, list):
            continue
        target_collections.extend(
            row.get("targets")
            for row in collection
            if isinstance(row, Mapping)
        )
    for collection in target_collections:
        if not isinstance(collection, list):
            continue
        for target in collection:
            if not isinstance(target, Mapping):
                continue
            ids = [target.get("entity_id")]
            if isinstance(target.get("entity_ids"), list):
                ids.extend(target["entity_ids"])
            metadata = {
                "target_template_id": _as_int(
                    target.get("template_id"), maximum=2_000_000_000
                ),
                "target_name": _safe_text(target.get("name"), 96),
            }
            for value in ids:
                target_id = _as_int(
                    value, maximum=4_102_444_800_000_000_000
                )
                if target_id:
                    previous = target_metadata.get(target_id, {})
                    target_metadata[target_id] = {
                        "target_template_id": (
                            metadata["target_template_id"]
                            or previous.get("target_template_id", 0)
                        ),
                        "target_name": (
                            metadata["target_name"]
                            or previous.get("target_name", "")
                        ),
                    }

    result: dict[int, list[dict[str, object]]] = {}
    for raw in rows[:MAX_UPLOAD_SKILL_TIMELINE_EVENTS]:
        if not isinstance(raw, list):
            continue
        try:
            actor_id = int(raw[positions["actor_id"]])
            item = {
                "time_ms": raw[positions["time_ms"]],
                "skill_id": raw[positions["skill_id"]],
                "damage": raw[positions["damage"]],
                "critical": (
                    bool(raw[positions["critical"]])
                    if "critical" in positions and raw[positions['critical']] in (True, False, 0, 1)
                    else None
                ),
                "penetrating": (
                    bool(raw[positions["penetrating"]])
                    if "penetrating" in positions and raw[positions['penetrating']] in (True, False, 0, 1)
                    else None
                ),
            }
            if "target_id" in positions:
                target_id = _as_int(
                    raw[positions["target_id"]],
                    maximum=4_102_444_800_000_000_000,
                )
                item["target_id"] = target_id
                metadata = target_metadata.get(target_id, {})
                if metadata.get("target_template_id"):
                    item["target_template_id"] = metadata["target_template_id"]
                if metadata.get("target_name"):
                    item["target_name"] = metadata["target_name"]
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
        bucket = result.setdefault(actor_id, [])
        bucket.append(item)
    return result


def _opening_rows_by_actor(
    record: Mapping[str, object],
    timelines: Mapping[int, list[dict[str, object]]],
) -> dict[int, list[dict[str, object]]]:
    result: dict[int, list[dict[str, object]]] = {}
    cast_log = record.get("skill_cast_log")
    if isinstance(cast_log, Mapping):
        columns = cast_log.get("columns")
        rows = cast_log.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            positions = {str(name): index for index, name in enumerate(columns)}
            if {"time_ms", "actor_id", "skill_id"}.issubset(positions):
                for raw in rows[:MAX_UPLOAD_SKILL_TIMELINE_EVENTS]:
                    if not isinstance(raw, (list, tuple)):
                        continue
                    try:
                        time_ms = int(raw[positions["time_ms"]])
                        actor_id = int(raw[positions["actor_id"]])
                        skill_id = int(raw[positions["skill_id"]])
                        sequence = (
                            int(raw[positions["sequence"]] or 0)
                            if "sequence" in positions
                            else 0
                        )
                    except (IndexError, TypeError, ValueError, OverflowError):
                        continue
                    if not 0 <= time_ms <= OPENING_SEQUENCE_MAX_MS or actor_id == 0 or skill_id <= 0:
                        continue
                    bucket = result.setdefault(actor_id, [])
                    if len(bucket) < MAX_UPLOAD_OPENING_EVENTS:
                        item: dict[str, object] = {
                            "time_ms": time_ms,
                            "skill_id": skill_id,
                            "sequence": sequence,
                            "source": "successful_cast",
                        }
                        for key in ("target_id", "target_template_id"):
                            if key in positions:
                                item[key] = _as_int(
                                    raw[positions[key]],
                                    maximum=4_102_444_800_000_000_000,
                                )
                        if "target_name" in positions:
                            item["target_name"] = _safe_text(
                                raw[positions["target_name"]], 96
                            )
                        bucket.append(item)
    for actor_id, events in timelines.items():
        if actor_id in result:
            result[actor_id].sort(key=lambda row: (int(row["time_ms"]), int(row["sequence"])))
            continue
        bucket: list[dict[str, object]] = []
        for sequence, event in enumerate(events, start=1):
            time_ms = _as_int(event.get("time_ms"), maximum=86_400_000)
            skill_id = _as_int(event.get("skill_id"), maximum=2_000_000_000)
            if time_ms > OPENING_SEQUENCE_MAX_MS or skill_id <= 0:
                continue
            row = {
                "time_ms": time_ms,
                "skill_id": skill_id,
                "sequence": sequence,
                "source": "damage_hit",
                "damage": event.get("damage", 0),
            }
            if event.get("target_id") is not None:
                row["target_id"] = event["target_id"]
            if event.get("target_template_id") is not None:
                row["target_template_id"] = event["target_template_id"]
            if event.get("target_name"):
                row["target_name"] = event["target_name"]
            bucket.append(row)
            if len(bucket) >= MAX_UPLOAD_OPENING_EVENTS:
                break
        if bucket:
            result[actor_id] = bucket
    return result


def build_upload_encounter(
    record: object,
    current_character_id: object,
    *,
    current_character_name: object = "",
    game_version: object = "",
) -> dict[str, object]:
    """Build the compact private desktop payload used by the upload API."""

    if not isinstance(record, dict):
        raise ProfileUploadError("BAD_ENCOUNTER")
    current_identity = canonical_character_identity(current_character_id)
    identities = _record_actor_identity_map(record)
    raw_participants = record.get("participants")
    if not isinstance(raw_participants, list) or not raw_participants:
        raise ProfileUploadError("BAD_ENCOUNTER")
    participant_by_actor: dict[int, dict[str, object]] = {}
    order: list[int] = []
    self_actor_ids: list[int] = []
    for raw in raw_participants:
        if not isinstance(raw, dict):
            continue
        actor_id = _as_int(
            raw.get("actor_id"), minimum=-(1 << 63), maximum=(1 << 63) - 1
        )
        if not actor_id:
            continue
        participant_by_actor[actor_id] = dict(raw)
        order.append(actor_id)
        if bool(raw.get("is_self")):
            self_actor_ids.append(actor_id)
    if len(self_actor_ids) != 1:
        raise ProfileUploadError("UPLOADER_NOT_IN_ENCOUNTER")
    self_actor_id = self_actor_ids[0]
    recorded_self_identity = recorded_self_character_id(record)
    if recorded_self_identity != current_identity.canonical:
        raise ProfileUploadError("UPLOADER_NOT_IN_ENCOUNTER")
    identities[self_actor_id] = recorded_self_identity

    stage_rows = _record_stage_actor_rows(record)
    for actor_id, stage_row in stage_rows.items():
        if actor_id not in participant_by_actor:
            participant_by_actor[actor_id] = stage_row
            order.append(actor_id)
        else:
            merged = dict(stage_row)
            merged.update(participant_by_actor[actor_id])
            participant_by_actor[actor_id] = merged
    raw_healers = record.get("healers")
    included_healers: list[dict] = []
    if isinstance(raw_healers, list):
        for healer in raw_healers:
            if not isinstance(healer, dict):
                continue
            actor_id = _as_int(
                healer.get("actor_id"), minimum=-(1 << 63), maximum=(1 << 63) - 1
            )
            if not actor_id:
                continue
            included_healers.append(healer)
            if actor_id not in participant_by_actor:
                participant_by_actor[actor_id] = {"actor_id": actor_id}
                order.append(actor_id)
            member = participant_by_actor[actor_id]
            for field in ("name", "profession_id", "extraordinary_rating", "equipment_snapshot"):
                if not member.get(field) and healer.get(field) is not None:
                    member[field] = healer[field]
            for field in ("effective_healing", "total_healing", "overhealing", "hps"):
                if healer.get(field) is not None:
                    member[field] = healer[field]
            raw_damage_skills = member.get("skills")
            damage_skills = [dict(skill) for skill in raw_damage_skills if isinstance(skill, dict)] if isinstance(raw_damage_skills, list) else []
            by_skill_id = {skill.get("skill_id"): skill for skill in damage_skills}
            for raw_skill in healer.get("skills", []) if isinstance(healer.get("skills"), list) else []:
                if not isinstance(raw_skill, dict):
                    continue
                skill_id = raw_skill.get("skill_id")
                skill = by_skill_id.get(skill_id)
                if skill is None:
                    skill = {"skill_id": skill_id, "name": raw_skill.get("name", ""),
                             "source": raw_skill.get("source", "")}
                    damage_skills.append(skill)
                    by_skill_id[skill_id] = skill
                if not skill.get("name") and raw_skill.get("name"):
                    skill["name"] = raw_skill["name"]
                for field in ("effective_healing", "total_healing", "overhealing"):
                    if raw_skill.get(field) is not None:
                        skill[field] = raw_skill[field]
                if raw_skill.get("server_skill_count") is not None:
                    skill["healing_skill_count"] = raw_skill["server_skill_count"]
                if raw_skill.get("events") is not None:
                    skill["healing_events"] = raw_skill["events"]
            member["skills"] = damage_skills

    raw_damage_taken = record.get("damage_taken")
    if isinstance(raw_damage_taken, list):
        for taken_row in raw_damage_taken:
            if not isinstance(taken_row, dict):
                continue
            actor_id = _as_int(
                taken_row.get("actor_id"),
                minimum=-(1 << 63),
                maximum=(1 << 63) - 1,
            )
            if not actor_id:
                continue
            if actor_id not in participant_by_actor:
                participant_by_actor[actor_id] = {"actor_id": actor_id}
                order.append(actor_id)
            member = participant_by_actor[actor_id]
            for field in (
                "name",
                "profession_id",
                "extraordinary_rating",
                "equipment_snapshot",
            ):
                if not member.get(field) and taken_row.get(field) is not None:
                    member[field] = taken_row[field]
            if taken_row.get("taken") is not None:
                member["taken"] = taken_row["taken"]
            if taken_row.get("share") is not None:
                member["taken_share"] = taken_row["share"]
            if taken_row.get("source") is not None:
                member["taken_source"] = taken_row["source"]
    for actor_id in identities:
        if actor_id not in participant_by_actor:
            participant_by_actor[actor_id] = {"actor_id": actor_id}
            order.append(actor_id)
    order = list(dict.fromkeys(order))[:MAX_ENCOUNTER_PARTICIPANTS]
    timelines = _timeline_rows_by_actor(record)
    openings = _opening_rows_by_actor(record, timelines)
    raw_event_log = record.get("event_log")
    raw_event_rows = (
        raw_event_log.get("rows") if isinstance(raw_event_log, Mapping) else None
    )
    timeline_globally_truncated = (
        isinstance(raw_event_rows, list)
        and len(raw_event_rows) > MAX_UPLOAD_SKILL_TIMELINE_EVENTS
    )
    participants: list[dict[str, object]] = []
    for actor_id in order:
        raw = participant_by_actor[actor_id]
        identity_value = identities.get(actor_id, "")
        is_uploader = actor_id == self_actor_id
        is_ai = False
        if identity_value:
            is_ai = canonical_character_identity(
                identity_value, allow_ai=True
            ).kind == "ai"
        participant = {
            "character_id": identity_value,
            "is_ai": is_ai,
            "is_uploader": is_uploader,
            "game_character_name": _safe_text(current_character_name if is_uploader and current_character_name else raw.get("name"), 48),
            "profession_id": _as_int(
                raw.get("profession_id"), maximum=2_000_000_000
            ),
        }
        for field in (
            "extraordinary_rating",
            "damage",
            "dps",
            "share",
            "hits",
            "max_hit",
            "damage_hits",
            "critical_hits",
            "penetration_hits",
            "critical_rate",
            "penetration_rate",
            "deaths",
            "revives",
            "death_duration_seconds",
            "taken",
            "taken_share",
            "taken_source",
            "classified_skill_damage",
            "unclassified_damage",
            "accounted_skill_damage",
            "skill_source",
            "skill_detail_status",
            "effective_healing",
            "total_healing",
            "overhealing",
            "hps",
            "skills",
            "targets",
            "equipment_snapshot",
        ):
            if field in raw:
                participant[field] = raw[field]
        if actor_id in timelines:
            participant["skill_timeline"] = timelines[actor_id]
            participant["skill_timeline_total"] = len(timelines[actor_id])
            participant["skill_timeline_truncated"] = timeline_globally_truncated
        if actor_id in openings:
            participant["opening_sequence"] = openings[actor_id]
            participant["opening_sequence_source"] = _safe_text(
                openings[actor_id][0].get("source"), 32
            )
        participants.append(participant)

    deduplicated: list[dict[str, object]] = []
    by_identity_and_damage: dict[tuple[str, int, int], dict[str, object]] = {}
    for participant in participants:
        identity_value = str(participant["character_id"] or "")
        key = (identity_value, _as_int(participant["profession_id"]), _as_int(participant.get("damage")))
        previous = by_identity_and_damage.get(key) if identity_value else None
        if previous is None:
            deduplicated.append(participant)
            if identity_value:
                by_identity_and_damage[key] = participant
            continue
        for field in ("effective_healing", "total_healing", "overhealing", "hps",
                      "deaths", "revives", "death_duration_seconds", "taken",
                      "taken_share", "taken_source", "equipment_snapshot"):
            if not previous.get(field) and participant.get(field):
                previous[field] = participant[field]
        for field in ("skills", "targets", "opening_sequence", "skill_timeline"):
            if len(participant.get(field) or []) > len(previous.get(field) or []):
                previous[field] = participant[field]
                if field == "opening_sequence":
                    previous["opening_sequence_source"] = participant.get(
                        "opening_sequence_source", ""
                    )
                elif field == "skill_timeline":
                    previous["skill_timeline_total"] = participant.get(
                        "skill_timeline_total", len(participant[field])
                    )
                    previous["skill_timeline_truncated"] = participant.get(
                        "skill_timeline_truncated", False
                    )
    participants = deduplicated

    raw_team_size = _as_int(
        record.get("team_size"), minimum=1, maximum=MAX_ENCOUNTER_PARTICIPANTS
    )
    team_size = max(raw_team_size, len(participants))
    targets = record.get("targets")
    template_ids: set[int] = set()
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            template_id = _as_int(target.get("template_id"), maximum=2_000_000_000)
            boss_type = _as_int(target.get("boss_type"), maximum=10)
            boss_rank = _as_int(target.get("boss_rank"), maximum=10)
            if template_id > 0 and (boss_type == 3 or boss_rank > 0):
                template_ids.add(template_id)
    monster = record.get("monster") if isinstance(record.get("monster"), dict) else {}
    primary_template = _as_int(monster.get("template_id"), maximum=2_000_000_000)
    if primary_template > 0:
        template_ids.add(primary_template)
    accounting = record.get("damage_accounting")
    validations = accounting.get("stage_summary_validations") if isinstance(accounting, dict) else []
    completion_confirmed = any(
        bool(item.get("completion_confirmed"))
        for item in validations
        if isinstance(item, dict)
    ) if isinstance(validations, list) else False
    archive_reason = _safe_text(record.get("archive_reason"), 48).casefold()
    raw_result = _safe_text(record.get("result"), 32).casefold()
    result_aliases = {
        "胜利": "defeated",
        "已击败": "defeated",
        "defeated": "defeated",
        "失败": "failed",
        "failed": "failed",
        "中断": "interrupted",
        "interrupted": "interrupted",
    }
    result = result_aliases.get(raw_result, "")
    if not result:
        if archive_reason == "target_defeated" or completion_confirmed:
            result = "defeated"
        elif archive_reason in {"party_wipe", "failed"}:
            result = "failed"
        elif archive_reason in {"party_exit", "space_transition", "exit", "reset"}:
            result = "interrupted"
        else:
            result = "undetermined"
    all_resolved = bool(participants) and all(item["character_id"] for item in participants)
    completeness = "complete" if all_resolved and len(participants) == team_size else "partial"
    team_timeline: list[dict[str, object]] = []
    raw_timeline = record.get("team_dps_timeline")
    if isinstance(raw_timeline, list):
        for item in raw_timeline[:4000]:
            if not isinstance(item, dict):
                continue
            team_timeline.append(
                {
                    "time": item.get(
                        "time", item.get("time_seconds", item.get("elapsed_seconds", item.get("second", 0)))
                    ),
                    "dps": item.get("dps", item.get("team_dps", 0)),
                    "team_dps": item.get("team_dps", item.get("dps", 0)),
                    "source": _safe_text(item.get("source"), 48),
                }
            )
    started_at = record.get("started_at_epoch")
    ended_at = record.get("ended_at_epoch")
    duration = record.get("duration_seconds", record.get("dps_duration_seconds"))
    if str(record.get("duration_source") or "") == "server_encounter_clock":
        parsed_start = _as_float(started_at, maximum=4_102_444_800.0)
        parsed_end = _as_float(ended_at, maximum=4_102_444_800.0)
        parsed_duration = _as_float(duration, maximum=86_400.0)
        if parsed_start > 0 and parsed_end >= parsed_start and parsed_duration > 0:
            if abs((parsed_end - parsed_start) - parsed_duration) > max(10.0, parsed_duration * .1):
                started_at = parsed_end - parsed_duration
    team_healing = record.get("team_effective_healing", 0)
    team_hps = record.get("team_hps", 0)
    if isinstance(raw_healers, list) and raw_healers and all(
        healer.get("effective_healing") is not None for healer in included_healers
    ):
        team_healing = sum(_as_int(healer.get("effective_healing")) for healer in included_healers)
        healing_duration = _as_float(record.get("hps_duration_seconds", duration), maximum=86_400.0)
        team_hps = team_healing / max(1, int(healing_duration)) if healing_duration > 0 else 0
    team_taken = record.get("team_taken", 0)
    if (
        isinstance(raw_damage_taken, list)
        and raw_damage_taken
        and all(
            isinstance(row, Mapping) and row.get("taken") is not None
            for row in raw_damage_taken
        )
    ):
        team_taken = sum(_as_int(row.get("taken")) for row in raw_damage_taken)

    difficulty, difficulty_source = _encounter_difficulty(record)
    module_coverage = {
        "total_members": len(participants),
        "resolved_members": sum(bool(row.get("character_id")) for row in participants),
        "named_members": sum(bool(row.get("game_character_name")) for row in participants),
        "dps_members": sum(
            "damage" in row or "dps" in row for row in participants
        ),
        "hps_members": sum(
            "effective_healing" in row or "hps" in row for row in participants
        ),
        "dt_members": sum("taken" in row for row in participants),
        "death_members": sum("deaths" in row for row in participants),
        "skill_members": sum(bool(row.get("skills")) for row in participants),
        "target_members": sum(bool(row.get("targets")) for row in participants),
        "equipment_members": sum(
            bool(row.get("equipment_snapshot")) for row in participants
        ),
        "skill_timeline_members": sum(
            bool(row.get("skill_timeline")) for row in participants
        ),
        "opening_sequence_members": sum(
            bool(row.get("opening_sequence")) for row in participants
        ),
        "successful_cast_opening_members": sum(
            row.get("opening_sequence_source") == "successful_cast"
            for row in participants
        ),
        "damage_hit_opening_members": sum(
            row.get("opening_sequence_source") == "damage_hit"
            for row in participants
        ),
        "skill_timeline_truncated": timeline_globally_truncated,
        "boss_damage": bool(record.get("boss_damage")),
        "boss_hp_timeline": bool(record.get("boss_hp_damage_samples")),
        "boss_max_hp": _as_int(monster.get("max_hp")) > 0,
        "difficulty": bool(difficulty),
    }
    required_member_modules = (
        "dps_members",
        "hps_members",
        "dt_members",
        "death_members",
        "skill_members",
        "target_members",
        "equipment_members",
        "skill_timeline_members",
        "opening_sequence_members",
    )
    module_coverage["details_complete"] = bool(participants) and all(
        int(module_coverage[key]) == len(participants)
        for key in required_member_modules
    ) and all(
        bool(module_coverage[key])
        for key in ("boss_damage", "boss_hp_timeline", "boss_max_hp", "difficulty")
    )
    return {
        "client_encounter_id": _safe_text(
            record.get("battle_id", record.get("encounter_id")), 96
        ),
        "started_at_epoch": started_at,
        "ended_at_epoch": ended_at,
        "duration_seconds": duration,
        "dungeon_id": record.get("dungeon_id", 0),
        "stage_id": record.get("dungeon_stage_id", record.get("stage_id", 0)),
        "map_id": record.get("map_id", 0),
        "dungeon_name": record.get("dungeon_name", ""),
        "stage_name": record.get("stage_name", ""),
        "difficulty": difficulty,
        "difficulty_source": difficulty_source,
        "boss_name": _safe_text(monster.get("name"), 96),
        "boss_template_ids": sorted(template_ids),
        "target_filter": record.get("target_filter", ""),
        "archive_reason": archive_reason,
        "result": result,
        "completion_confirmed": completion_confirmed,
        "capture_started_mid_encounter": bool(record.get("capture_started_mid_encounter")),
        "data_completeness": completeness,
        "team_size": team_size,
        "team_total_damage": record.get("total_damage", 0),
        "team_dps": record.get("team_dps", 0),
        "team_hps": team_hps,
        "team_effective_healing": team_healing,
        "team_taken": team_taken,
        "game_version": _safe_text(game_version, 48),
        "participants": participants,
        "team_dps_timeline": team_timeline,
        "monster": monster,
        "targets": targets if isinstance(targets, list) else [],
        "boss_damage": record.get("boss_damage", {}),
        "boss_hp_damage_samples": record.get("boss_hp_damage_samples", {}),
        "module_coverage": module_coverage,
    }
