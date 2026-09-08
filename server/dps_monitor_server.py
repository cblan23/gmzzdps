#!/usr/bin/env python3
"""Small HTTPS-proxied session monitor for the DPS client."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from runtime_capability import (
    SIGNING_KEY_ID_PATTERN,
    RuntimeCapabilityError,
    create_server_capability,
    load_capability_private_key_file,
    load_runtime_profile_file,
)


HOST = os.environ.get("GMZZ_MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("GMZZ_MONITOR_PORT", "8766"))
DATABASE_PATH = Path(
    os.environ.get("GMZZ_MONITOR_DB", "/var/lib/gmzz-dps-monitor/sessions.sqlite3")
)
ADMIN_USER = os.environ.get("GMZZ_MONITOR_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("GMZZ_MONITOR_ADMIN_PASSWORD", "")
HEARTBEAT_INTERVAL = 30
ONLINE_WINDOW = 75
MAX_BODY_BYTES = 512 * 1024
CLIENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
BUILD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TRIAL_REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CERTIFICATE_THUMBPRINT_PATTERN = re.compile(r"^[0-9A-F]{40}(?:[0-9A-F]{24})?$")
RUNTIME_PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,63}$")
CARD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CARD_PATTERN = re.compile(r"^GMZZ[A-HJ-NP-Z2-9]{26}$")
CUSTOM_CARD_PATTERN = re.compile(r"^[A-Za-z0-9]{6,64}$")
COMBAT_CLOCK_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMBAT_CLOCK_ENCOUNTER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
PARTNER_CARD_KEY = os.environ.get("GMZZ_MONITOR_PARTNER_CARD", "").strip()
MAX_CARD_DURATION_DAYS = 3650
MIN_CARD_DURATION_SECONDS = 60 * 60
MAX_CARD_DURATION_SECONDS = MAX_CARD_DURATION_DAYS * 86400
MAX_CARD_BATCH = 500
# Ordinary cards may move to another device after each binding's cooldown.
CARD_DEVICE_REBIND_ENABLED = True
CARD_REBIND_COOLDOWN_SECONDS = 12 * 60 * 60
MAX_FEEDBACK_CONTENT = 2000
MAX_FEEDBACK_DIAGNOSTICS_BYTES = 384 * 1024
MAX_DIAGNOSTIC_REPORT_BYTES = 192 * 1024
MAX_DIAGNOSTIC_REPORTS_PER_HOUR = 6
COMBAT_CLOCK_MAX_SECONDS = 24 * 60 * 60
COMBAT_CLOCK_ACTIVE_MATCH_SECONDS = 20.0
COMBAT_CLOCK_ENDED_MATCH_SECONDS = 20.0
COMBAT_CLOCK_LATE_REPORT_SECONDS = 45.0
COMBAT_CLOCK_RETENTION_SECONDS = 7 * 24 * 60 * 60
COMBAT_CLOCK_V2_CLIENT_LIVE_SECONDS = 15.0
COMBAT_CLOCK_V2_MAX_REVISION = (1 << 63) - 1
COMBAT_CLOCK_CLEANUP_INTERVAL_SECONDS = 60.0
SQLITE_BUSY_TIMEOUT_MS = 5000
SQLITE_BUSY_RETRY_ATTEMPTS = 2
SQLITE_BUSY_RETRY_DELAY_SECONDS = 0.05
SQLITE_WRITE_QUEUE_TIMEOUT_SECONDS = 5.0
_DATABASE_WRITE_LOCK = threading.RLock()
_COMBAT_CLOCK_CLEANUP_STATE: tuple[str, float] | None = None
DIAGNOSTIC_TOOL_NAME = "叨叨诡秘问题检测工具"
UPDATE_METADATA_PATH = Path(
    os.environ.get(
        "GMZZ_MONITOR_UPDATE_METADATA",
        "/var/lib/gmzz-dps-monitor/update.json",
    )
)
ROLLBACK_METADATA_PATH = Path(
    os.environ.get(
        "GMZZ_MONITOR_ROLLBACK_METADATA",
        str(UPDATE_METADATA_PATH.parent / "release-metadata"),
    )
)
ROLLBACK_BACKUP_PATH = Path(
    os.environ.get(
        "GMZZ_MONITOR_ROLLBACK_BACKUPS",
        str(UPDATE_METADATA_PATH.parent / "release-backups"),
    )
)
BUILD_ALLOWLIST_PATH = Path(
    os.environ.get(
        "GMZZ_MONITOR_BUILD_ALLOWLIST",
        "/var/lib/gmzz-dps-monitor/build-allowlist.json",
    )
)
ENFORCE_BUILD_ALLOWLIST = os.environ.get(
    "GMZZ_MONITOR_ENFORCE_BUILD_ALLOWLIST", "0"
).strip().casefold() in {"1", "true", "yes", "on"}
RUNTIME_PROFILE_PATH = Path(
    os.environ.get(
        "GMZZ_MONITOR_RUNTIME_PROFILE",
        "/var/lib/gmzz-dps-monitor/runtime-profile.json",
    )
)
CAPABILITY_SIGNING_KEY_ID = os.environ.get(
    "GMZZ_MONITOR_CAPABILITY_SIGNING_KEY_ID", ""
).strip()
CAPABILITY_SIGNING_PRIVATE_KEY_PATH = Path(
    os.environ.get(
        "GMZZ_MONITOR_CAPABILITY_SIGNING_PRIVATE_KEY",
        "/etc/gmzz-dps-capability-signing-key.pem",
    )
)
_CAPABILITY_SIGNING_KEY_LOCK = threading.Lock()
_CAPABILITY_SIGNING_KEY_CACHE: tuple[tuple[str, int, int], object] | None = None
try:
    RUNTIME_CAPABILITY_TTL_SECONDS = max(
        60,
        min(
            300,
            int(os.environ.get("GMZZ_MONITOR_RUNTIME_CAPABILITY_TTL", "120")),
        ),
    )
except (TypeError, ValueError, OverflowError):
    RUNTIME_CAPABILITY_TTL_SECONDS = 120
MAX_UPDATE_BYTES = 256 * 1024 * 1024
ROLLBACK_VERSION_PATTERN = re.compile(
    r"^(?P<numeric>\d+(?:\.\d+){2,3})(?P<suffix>[a-z]?)$",
    re.IGNORECASE,
)
MINIMUM_ROLLBACK_VERSION = (0, 1, 0)
FEEDBACK_CATEGORIES = {
    "dps": "DPS 统计",
    "boss": "BOSS 识别",
    "team": "队伍成员",
    "ui": "界面显示",
    "connection": "登录或连接",
    "diagnostic": "问题检测",
    "other": "其他问题",
}
CARD_TYPES = {
    "test_2h": ("测试2小时卡", 2 * 60 * 60, False),
    "daily": ("天卡", 24 * 60 * 60, False),
    "weekly": ("周卡", 7 * 24 * 60 * 60, False),
    "monthly": ("月卡", 30 * 24 * 60 * 60, False),
    "partner": ("莫雪的小伙伴", 0, True),
}
TRIAL_DURATION_SECONDS = 2 * 60 * 60
TRIAL_CARD_NOTE = "自助试用2小时卡"
TRIAL_CARD_REMARK = "客户端自助领取"
CHINA_UTC_OFFSET_SECONDS = 8 * 60 * 60


def now_epoch() -> float:
    return time.time()


def trial_claim_day(timestamp: float) -> int:
    """Return the Beijing-calendar day containing an epoch timestamp."""
    return int((float(timestamp) + CHINA_UTC_OFFSET_SECONDS) // 86400)


def trial_next_available_at(timestamp: float) -> float:
    return float(
        (trial_claim_day(timestamp) + 1) * 86400 - CHINA_UTC_OFFSET_SECONDS
    )


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_text(value: object, limit: int) -> str:
    text = str(value or "").strip().replace("\x00", "")
    return "".join(char for char in text if ord(char) >= 0x20)[:limit]


def clean_multiline_text(value: object, limit: int) -> str:
    text = str(value or "").replace("\x00", "").replace("\r\n", "\n")
    text = text.replace("\r", "\n").strip()
    return "".join(
        char for char in text if char in "\n\t" or ord(char) >= 0x20
    )[:limit]


def clean_client_id(value: object) -> str:
    client_id = clean_text(value, 80).lower()
    return client_id if CLIENT_ID_PATTERN.fullmatch(client_id) else ""


def clean_build_id(value: object) -> str:
    return clean_text(value, 64).lower()


def capability_signing_private_key():
    """Load and cache the server-only lease key while allowing safe rotation."""
    global _CAPABILITY_SIGNING_KEY_CACHE
    if not SIGNING_KEY_ID_PATTERN.fullmatch(CAPABILITY_SIGNING_KEY_ID):
        return None
    try:
        stat = CAPABILITY_SIGNING_PRIVATE_KEY_PATH.stat()
        identity = (
            str(CAPABILITY_SIGNING_PRIVATE_KEY_PATH.resolve()),
            int(stat.st_mtime_ns),
            int(stat.st_size),
        )
    except OSError:
        return None
    with _CAPABILITY_SIGNING_KEY_LOCK:
        if (
            _CAPABILITY_SIGNING_KEY_CACHE is not None
            and _CAPABILITY_SIGNING_KEY_CACHE[0] == identity
        ):
            return _CAPABILITY_SIGNING_KEY_CACHE[1]
        try:
            private_key = load_capability_private_key_file(
                CAPABILITY_SIGNING_PRIVATE_KEY_PATH
            )
        except RuntimeCapabilityError:
            return None
        _CAPABILITY_SIGNING_KEY_CACHE = (identity, private_key)
        return private_key


def load_build_allowlist() -> dict[str, dict[str, object]]:
    """Load server-controlled official build registrations.

    The allowlist contains only public release identifiers and signer
    thumbprints. Private signing material must never be placed on this server.
    """
    try:
        value = json.loads(BUILD_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) not in {
        1,
        2,
    }:
        return {}
    schema_version = int(value.get("schema_version", 0))
    raw_builds = value.get("builds", [])
    if not isinstance(raw_builds, list) or len(raw_builds) > 4096:
        return {}
    builds: dict[str, dict[str, object]] = {}
    for raw_build in raw_builds:
        if not isinstance(raw_build, dict):
            continue
        build_id = clean_build_id(raw_build.get("build_id"))
        client_build = clean_text(raw_build.get("client_build"), 64)
        publisher = re.sub(
            r"\s+", "", clean_text(raw_build.get("publisher_thumbprint"), 80)
        ).upper()
        runtime_profile_id = clean_text(
            raw_build.get("runtime_profile_id"), 64
        )
        official = bool(
            raw_build.get("official", bool(publisher))
            if schema_version >= 2
            else bool(publisher)
        )
        protected = bool(raw_build.get("protected"))
        capability_signing_key_id = clean_text(
            raw_build.get("capability_signing_key_id"), 64
        )
        if (
            not BUILD_ID_PATTERN.fullmatch(build_id)
            or not client_build
            or (publisher and not CERTIFICATE_THUMBPRINT_PATTERN.fullmatch(publisher))
            or (official and not CERTIFICATE_THUMBPRINT_PATTERN.fullmatch(publisher))
            or not RUNTIME_PROFILE_ID_PATTERN.fullmatch(runtime_profile_id)
            or (
                protected
                and not SIGNING_KEY_ID_PATTERN.fullmatch(
                    capability_signing_key_id
                )
            )
        ):
            continue
        builds[build_id] = {
            "build_id": build_id,
            "client_build": client_build,
            "publisher_thumbprint": publisher,
            "runtime_profile_id": runtime_profile_id,
            "official": official,
            "protected": protected,
            "capability_signing_key_id": capability_signing_key_id,
            "enabled": bool(raw_build.get("enabled", True)),
        }
    return builds


def client_build_registration(
    app_version: object, build_id: object
) -> dict[str, object] | None:
    normalized_build_id = clean_build_id(build_id)
    registration = load_build_allowlist().get(normalized_build_id)
    if registration is None or not registration.get("enabled"):
        return None
    if not hmac.compare_digest(
        str(registration.get("client_build", "")),
        clean_text(app_version, 64),
    ):
        return None
    return registration


def issue_runtime_capability(
    *,
    session_id: str,
    client_id: str,
    app_version: str,
    build_id: str,
    lease_sequence: int,
    timestamp: float,
    registration: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Load the server-only hook profile and bind a short lease to a session."""
    registration = registration or client_build_registration(app_version, build_id)
    if not registration or not registration.get("protected"):
        return None
    if not session_id or not client_id or not app_version or not build_id:
        return None
    signing_key_id = str(registration.get("capability_signing_key_id", ""))
    if (
        not hmac.compare_digest(signing_key_id, CAPABILITY_SIGNING_KEY_ID)
        or not SIGNING_KEY_ID_PATTERN.fullmatch(signing_key_id)
    ):
        return None
    private_key = capability_signing_private_key()
    if private_key is None:
        return None
    try:
        profile = load_runtime_profile_file(RUNTIME_PROFILE_PATH)
        expected_profile_id = str(registration.get("runtime_profile_id", ""))
        if profile.get("profile_id") != expected_profile_id:
            return None
        capability = create_server_capability(
            profile,
            session_id=session_id,
            client_id=client_id,
            build_id=build_id,
            client_build=app_version,
            lease_sequence=lease_sequence,
            signing_key_id=signing_key_id,
            signing_private_key=private_key,
            lease_seconds=RUNTIME_CAPABILITY_TTL_SECONDS,
            now=timestamp,
        )
    except RuntimeCapabilityError:
        return None
    return capability.to_wire()


def version_tuple(value: object) -> tuple[int, ...]:
    main = clean_text(value, 32).split("+", 1)[0].split("-", 1)[0]
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", main):
        return ()
    return tuple(int(part) for part in main.split("."))


def update_download_fallback_filename(version: object) -> str:
    """Return an ASCII filename that still carries the current release."""
    parts = version_tuple(version)
    if not parts:
        return "Dps-Logs-update.exe"
    return f"Dps-Logs-v{'.'.join(str(part) for part in parts)}.exe"


def same_release_version(left: object, right: object) -> bool:
    left_parts = version_tuple(left)
    right_parts = version_tuple(right)
    if left_parts and right_parts:
        return left_parts == right_parts
    return clean_text(left, 32) == clean_text(right, 32)


def version_is_older(current: object, latest: object) -> bool:
    current_parts = version_tuple(current)
    latest_parts = version_tuple(latest)
    if not current_parts or not latest_parts:
        return False
    width = max(len(current_parts), len(latest_parts))
    return current_parts + (0,) * (width - len(current_parts)) < latest_parts + (
        0,
    ) * (width - len(latest_parts))


def build_revision_tuple(value: object) -> tuple[int, ...]:
    _version, separator, revision = clean_text(value, 64).partition("+")
    if not separator or not re.fullmatch(r"\d+(?:\.\d+)*", revision):
        return ()
    return tuple(int(part) for part in revision.split("."))


def update_is_available(
    current: object,
    latest_version: object,
    client_build: object = "",
) -> bool:
    if version_is_older(current, latest_version):
        return True
    if version_tuple(current) != version_tuple(latest_version):
        return False
    latest_revision = build_revision_tuple(client_build)
    if (
        not latest_revision
        or version_tuple(client_build) != version_tuple(latest_version)
    ):
        return False
    current_revision = build_revision_tuple(current)
    return not current_revision or current_revision < latest_revision


def normalize_rollback_version(value: object) -> str:
    text = clean_text(value, 32)
    if text[:1].casefold() == "v":
        text = text[1:]
    match = ROLLBACK_VERSION_PATTERN.fullmatch(text)
    if match is None:
        return ""
    numeric = ".".join(
        str(int(part)) for part in match.group("numeric").split(".")
    )
    return numeric + match.group("suffix").casefold()


def rollback_version_is_supported(value: object) -> bool:
    normalized = normalize_rollback_version(value)
    if not normalized:
        return False
    match = ROLLBACK_VERSION_PATTERN.fullmatch(normalized)
    if match is None:
        return False
    parts = tuple(int(part) for part in match.group("numeric").split("."))
    width = max(len(parts), len(MINIMUM_ROLLBACK_VERSION))
    return parts + (0,) * (width - len(parts)) >= MINIMUM_ROLLBACK_VERSION + (
        0,
    ) * (width - len(MINIMUM_ROLLBACK_VERSION))


def _load_update_metadata_file(
    metadata_path: Path,
    *,
    binary_roots: tuple[Path, ...],
) -> tuple[dict[str, object], Path] | None:
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    latest_version = clean_text(value.get("latest_version"), 32)
    display_version = clean_text(value.get("display_version"), 32) or latest_version
    display_version_match = re.fullmatch(
        r"(?P<numeric>\d+(?:\.\d+){1,3})[a-z]?", display_version
    )
    client_build = clean_text(value.get("client_build"), 64)
    build_id = clean_build_id(value.get("build_id"))
    publisher_thumbprint = re.sub(
        r"\s+", "", clean_text(value.get("publisher_thumbprint"), 80)
    ).upper()
    signature_required = bool(value.get("signature_required"))
    protected = bool(value.get("protected"))
    filename = Path(clean_text(value.get("filename"), 160)).name
    sha256 = clean_text(value.get("sha256"), 64).lower()
    try:
        size = int(value.get("size", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    update_path = next(
        (
            root / filename
            for root in binary_roots
            if (root / filename).is_file()
        ),
        None,
    )
    registration = (
        client_build_registration(client_build, build_id)
        if ENFORCE_BUILD_ALLOWLIST
        else None
    )
    if (
        not version_tuple(latest_version)
        or display_version_match is None
        or version_tuple(display_version_match.group("numeric"))
        != version_tuple(latest_version)
        or (
            client_build
            and (
                not build_revision_tuple(client_build)
                or version_tuple(client_build) != version_tuple(latest_version)
            )
        )
        or not filename.casefold().endswith(".exe")
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or size <= 0
        or size > MAX_UPDATE_BYTES
        or update_path is None
        or (
            signature_required
            and (
                not BUILD_ID_PATTERN.fullmatch(build_id)
                or not CERTIFICATE_THUMBPRINT_PATTERN.fullmatch(
                    publisher_thumbprint
                )
            )
        )
        or (
            ENFORCE_BUILD_ALLOWLIST
            and (
                registration is None
                or bool(registration.get("official")) != signature_required
                or bool(registration.get("protected")) != protected
                or (
                    signature_required
                    and registration.get("publisher_thumbprint")
                    != publisher_thumbprint
                )
            )
        )
    ):
        return None
    try:
        assert update_path is not None
        if update_path.stat().st_size != size:
            return None
    except OSError:
        return None
    metadata = {
        "latest_version": latest_version,
        "display_version": display_version,
        "client_build": client_build,
        "build_id": build_id,
        "publisher_thumbprint": publisher_thumbprint,
        "signature_required": signature_required,
        "protected": protected,
        "filename": filename,
        "sha256": sha256,
        "size": size,
        "notes": clean_multiline_text(value.get("notes"), 1000),
        "required": bool(value.get("required")),
    }
    return metadata, update_path


def load_update_metadata() -> tuple[dict[str, object], Path] | None:
    return _load_update_metadata_file(
        UPDATE_METADATA_PATH,
        binary_roots=(UPDATE_METADATA_PATH.parent,),
    )


def _rollback_metadata_candidates() -> tuple[Path, ...]:
    candidates = [UPDATE_METADATA_PATH]
    for archive_root in (ROLLBACK_METADATA_PATH, ROLLBACK_BACKUP_PATH):
        try:
            children = sorted(archive_root.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children[:512]:
            metadata_path = child / "update.json"
            try:
                if child.is_dir() and metadata_path.is_file():
                    candidates.append(metadata_path)
            except OSError:
                # Operational backups may intentionally be root-only.  One
                # unreadable directory must not disable every rollback entry.
                continue
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.absolute()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return tuple(unique)


def load_rollback_metadata(
    version: object,
) -> tuple[dict[str, object], Path] | None:
    target_version = normalize_rollback_version(version)
    if not rollback_version_is_supported(target_version):
        return None
    matches: list[tuple[dict[str, object], Path]] = []
    for metadata_path in _rollback_metadata_candidates():
        loaded = _load_update_metadata_file(
            metadata_path,
            binary_roots=(metadata_path.parent, UPDATE_METADATA_PATH.parent),
        )
        if loaded is None:
            continue
        metadata, _update_path = loaded
        if normalize_rollback_version(metadata["display_version"]) == target_version:
            matches.append(loaded)
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: build_revision_tuple(item[0].get("client_build")),
    )


def normalize_card_key(value: object) -> str:
    raw = clean_text(value, 80)
    if is_partner_card(raw):
        return PARTNER_CARD_KEY
    compact = re.sub(r"[\s-]+", "", raw.upper())
    if CARD_PATTERN.fullmatch(compact):
        return compact
    return normalize_custom_card_key(raw)


def normalize_custom_card_key(value: object) -> str:
    raw = clean_text(value, 80)
    if not CUSTOM_CARD_PATTERN.fullmatch(raw):
        return ""
    standard = raw.upper()
    return standard if CARD_PATTERN.fullmatch(standard) else raw.casefold()


def is_partner_card(value: object) -> bool:
    if not PARTNER_CARD_KEY:
        return False
    candidate = clean_text(value, 80).casefold()
    return hmac.compare_digest(
        candidate.encode("utf-8"), PARTNER_CARD_KEY.casefold().encode("utf-8")
    )


def generate_card_key() -> str:
    payload = "".join(secrets.choice(CARD_ALPHABET) for _ in range(26))
    return "GMZZ" + payload


def classify_card_tier(
    note: object, duration_seconds: object, *, permanent: bool = False
) -> str:
    if permanent:
        return "partner"
    text = clean_text(note, 80).casefold()
    if any(marker in text for marker in ("莫雪", "伙伴", "小伙伴", "无限")):
        return "partner"
    if "月卡" in text:
        return "monthly"
    if "周卡" in text:
        return "weekly"
    if any(marker in text for marker in ("测试", "小时卡", "天卡", "日卡")):
        return "normal"
    try:
        duration = max(0, int(duration_seconds or 0))
    except (TypeError, ValueError, OverflowError):
        duration = 0
    if duration >= 28 * 86400:
        return "monthly"
    if duration >= 7 * 86400:
        return "weekly"
    return "normal"


def card_error(error: str) -> str:
    return {
        "card_required": "请输入卡号后再登录。",
        "card_invalid": "卡号不存在或输入有误，请检查后重试。",
        "card_expired": "该卡号已失效，请更换卡号后登录。",
        "card_revoked": "该卡号已被停用；如有疑问，请联系管理员。",
        "partner_device_changed": (
            "“莫雪的小伙伴”卡仅限首次登录的设备使用。"
            "检测到更换设备，该卡已永久停用；如需处理，请联系管理员。"
        ),
        "card_device_locked": (
            "该卡号已绑定其他设备，当前无法换机登录；如需处理，请联系管理员。"
        ),
        "card_bound": "该卡号已绑定其他设备，暂时无法在本机登录。",
        "card_in_use": (
            "该卡号正在另一台设备使用。请先在原设备完全退出程序（包括托盘），"
            "等待约 90 秒后重试。"
        ),
        "card_rebind_cooldown": "该卡号刚在另一台设备使用，请稍后再在本机登录。",
        "trial_daily_limit": "今天已领取过试用卡，每台设备每天限领一次，请明天再试。",
        "client_revoked": "当前设备已被停用，无法登录；如有疑问，请联系管理员。",
        "client_build_required": "当前客户端无法通过版本校验，请从群文件重新下载最新版。",
        "client_build_not_allowed": (
            "当前客户端版本已停止使用，请从群文件下载最新版后重新登录。"
        ),
        "client_build_mismatch": (
            "检测到客户端版本发生变化，请完全退出程序后重新打开并登录。"
        ),
        "runtime_capability_unavailable": (
            "登录服务暂时异常，请稍后重试；若持续出现，请联系管理员。"
        ),
        "invalid_session": "登录状态已失效，请重新登录。",
    }.get(error, "登录失败，请稍后重试；若持续出现，请联系管理员。")


def database_is_busy(error: BaseException) -> bool:
    """Return whether an SQLite error represents transient lock contention."""
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int) and (error_code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    message = str(error).casefold()
    return "database" in message and (
        "locked" in message or "busy" in message
    )


def _retry_database_operation(operation):
    for attempt in range(SQLITE_BUSY_RETRY_ATTEMPTS):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            if not database_is_busy(error) or attempt + 1 >= SQLITE_BUSY_RETRY_ATTEMPTS:
                raise
            time.sleep(SQLITE_BUSY_RETRY_DELAY_SECONDS * (2**attempt))
    raise AssertionError("unreachable SQLite retry state")


def _open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def database():
    """Open a configured connection without changing persistent journal mode."""
    connection = _open_database()
    try:
        yield connection
        _retry_database_operation(connection.commit)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def _database_write_guard():
    acquired = _DATABASE_WRITE_LOCK.acquire(
        timeout=SQLITE_WRITE_QUEUE_TIMEOUT_SECONDS
    )
    if not acquired:
        raise sqlite3.OperationalError("database write queue is busy")
    try:
        yield
    finally:
        _DATABASE_WRITE_LOCK.release()


@contextmanager
def write_database():
    """Serialize and bound all request-time SQLite write transactions."""
    with _database_write_guard():
        with database() as connection:
            _retry_database_operation(lambda: connection.execute("BEGIN IMMEDIATE"))
            yield connection


def cleanup_expired_combat_clocks(
    connection: sqlite3.Connection, timestamp: float
) -> bool:
    """Delete expired v1/v2 clocks at most once per database per interval."""
    global _COMBAT_CLOCK_CLEANUP_STATE
    database_key = os.path.normcase(str(DATABASE_PATH.absolute()))
    previous = _COMBAT_CLOCK_CLEANUP_STATE
    if previous is not None and previous[0] == database_key:
        elapsed = timestamp - previous[1]
        if 0.0 <= elapsed < COMBAT_CLOCK_CLEANUP_INTERVAL_SECONDS:
            return False
    cutoff = timestamp - COMBAT_CLOCK_RETENTION_SECONDS
    connection.execute("DELETE FROM combat_clocks WHERE last_seen<?", (cutoff,))
    connection.execute("DELETE FROM combat_clocks_v2 WHERE last_seen<?", (cutoff,))
    _COMBAT_CLOCK_CLEANUP_STATE = (database_key, timestamp)
    return True


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _database_write_guard(), database() as connection:
        # Journal mode is persistent. Setting it once before accepting
        # requests avoids a schema-level lock on every connection.
        journal_mode = _retry_database_operation(
            lambda: connection.execute("PRAGMA journal_mode=WAL").fetchone()
        )
        if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
            raise sqlite3.OperationalError("unable to enable SQLite WAL mode")
        connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL REFERENCES clients(client_id),
                token_hash TEXT NOT NULL UNIQUE,
                started_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                ended_at REAL,
                using_app INTEGER NOT NULL DEFAULT 0,
                app_version TEXT NOT NULL DEFAULT '',
                build_id TEXT NOT NULL DEFAULT '',
                character_name TEXT NOT NULL DEFAULT '',
                game_pid INTEGER NOT NULL DEFAULT 0,
                remote_ip TEXT NOT NULL DEFAULT '',
                lease_sequence INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cards (
                card_hash TEXT PRIMARY KEY,
                card_key TEXT NOT NULL DEFAULT '',
                card_suffix TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL,
                created_at REAL NOT NULL,
                activated_at REAL,
                expires_at REAL,
                bound_client_id TEXT NOT NULL DEFAULT '',
                last_bound_at REAL,
                rebind_cooldown_seconds INTEGER NOT NULL DEFAULT {CARD_REBIND_COOLDOWN_SECONDS},
                last_used_at REAL,
                deleted_at REAL,
                revoked INTEGER NOT NULL DEFAULT 0,
                sold INTEGER NOT NULL DEFAULT 0,
                permanent INTEGER NOT NULL DEFAULT 0,
                remark TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS trial_claims (
                client_id TEXT NOT NULL REFERENCES clients(client_id)
                    ON DELETE CASCADE,
                claim_day INTEGER NOT NULL,
                card_hash TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '',
                claimed_at REAL NOT NULL,
                remote_ip TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(client_id, claim_day),
                UNIQUE(card_hash)
            );
            CREATE TABLE IF NOT EXISTS feedbacks (
                feedback_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                client_id TEXT NOT NULL DEFAULT '',
                card_hash TEXT NOT NULL DEFAULT '',
                card_key TEXT NOT NULL DEFAULT '',
                character_name TEXT NOT NULL DEFAULT '',
                app_version TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'other',
                content TEXT NOT NULL,
                diagnostics_json TEXT NOT NULL DEFAULT '{{}}',
                remote_ip TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS combat_clocks (
                clock_id TEXT PRIMARY KEY,
                party_key TEXT NOT NULL,
                target_key TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                max_total_damage INTEGER NOT NULL DEFAULT 0,
                report_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS combat_clock_clients (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                client_encounter_id TEXT NOT NULL,
                clock_id TEXT NOT NULL REFERENCES combat_clocks(clock_id)
                    ON DELETE CASCADE,
                last_seen REAL NOT NULL,
                ended INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id, client_encounter_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_last_seen
                ON sessions(last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_client
                ON sessions(client_id, last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_cards_expires
                ON cards(expires_at);
            CREATE INDEX IF NOT EXISTS idx_trial_claims_day
                ON trial_claims(claim_day DESC);
            CREATE INDEX IF NOT EXISTS idx_feedbacks_created
                ON feedbacks(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_combat_clocks_match
                ON combat_clocks(party_key, target_key, last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_combat_clock_clients_clock
                ON combat_clock_clients(clock_id, last_seen DESC);
            CREATE TABLE IF NOT EXISTS combat_clocks_v2 (
                clock_id TEXT PRIMARY KEY,
                party_key TEXT NOT NULL,
                target_key TEXT NOT NULL,
                started_at REAL NOT NULL,
                provisional_ended_at REAL,
                ended_at REAL,
                state TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                max_total_damage INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                report_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS combat_clock_clients_v2 (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                client_encounter_id TEXT NOT NULL,
                clock_id TEXT NOT NULL REFERENCES combat_clocks_v2(clock_id)
                    ON DELETE CASCADE,
                client_revision INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'active',
                candidate_started_at REAL NOT NULL,
                candidate_ended_at REAL,
                last_activity_at REAL NOT NULL,
                total_damage INTEGER NOT NULL DEFAULT 0,
                end_reason TEXT NOT NULL DEFAULT '',
                last_seen REAL NOT NULL,
                PRIMARY KEY(session_id, client_encounter_id)
            );
            CREATE INDEX IF NOT EXISTS idx_combat_clocks_v2_match
                ON combat_clocks_v2(party_key, target_key, last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_combat_clock_clients_v2_clock
                ON combat_clock_clients_v2(clock_id, last_seen DESC);
            """
        )
        session_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "card_hash" not in session_columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN card_hash TEXT NOT NULL DEFAULT ''"
            )
        if "build_id" not in session_columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN build_id TEXT NOT NULL DEFAULT ''"
            )
        if "lease_sequence" not in session_columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN lease_sequence INTEGER NOT NULL DEFAULT 0"
            )
        trial_claim_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(trial_claims)"
            ).fetchall()
        }
        if "request_id" not in trial_claim_columns:
            connection.execute(
                "ALTER TABLE trial_claims ADD COLUMN request_id TEXT NOT NULL DEFAULT ''"
            )
        card_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(cards)").fetchall()
        }
        if "last_bound_at" not in card_columns:
            connection.execute("ALTER TABLE cards ADD COLUMN last_bound_at REAL")
        if "rebind_cooldown_seconds" not in card_columns:
            connection.execute(
                f"""
                ALTER TABLE cards ADD COLUMN rebind_cooldown_seconds
                INTEGER NOT NULL DEFAULT {CARD_REBIND_COOLDOWN_SECONDS}
                """
            )
        if "card_key" not in card_columns:
            connection.execute(
                "ALTER TABLE cards ADD COLUMN card_key TEXT NOT NULL DEFAULT ''"
            )
        if "sold" not in card_columns:
            connection.execute(
                "ALTER TABLE cards ADD COLUMN sold INTEGER NOT NULL DEFAULT 0"
            )
        if "permanent" not in card_columns:
            connection.execute(
                "ALTER TABLE cards ADD COLUMN permanent INTEGER NOT NULL DEFAULT 0"
            )
        if "remark" not in card_columns:
            connection.execute(
                "ALTER TABLE cards ADD COLUMN remark TEXT NOT NULL DEFAULT ''"
            )
        if "deleted_at" not in card_columns:
            connection.execute("ALTER TABLE cards ADD COLUMN deleted_at REAL")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cards_deleted_created
            ON cards(deleted_at, created_at)
            """
        )
        # Upgrade every ordinary card to the current twelve-hour policy. Partner
        # cards are permanent and use the device-change invalidation path.
        connection.execute(
            """
            UPDATE cards SET rebind_cooldown_seconds=?
            WHERE permanent=0 AND deleted_at IS NULL
            """,
            (CARD_REBIND_COOLDOWN_SECONDS,),
        )
        if PARTNER_CARD_KEY:
            partner_hash = token_digest(PARTNER_CARD_KEY)
            connection.execute(
                """
                INSERT INTO cards(
                    card_hash, card_key, card_suffix, duration_seconds,
                    created_at, rebind_cooldown_seconds, permanent, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_hash) DO UPDATE SET
                    card_key=excluded.card_key,
                    card_suffix=excluded.card_suffix,
                    duration_seconds=excluded.duration_seconds,
                    expires_at=NULL,
                    deleted_at=NULL,
                    permanent=1,
                    note=excluded.note
                """,
                (
                    partner_hash,
                    PARTNER_CARD_KEY,
                    PARTNER_CARD_KEY[-4:],
                    0,
                    now_epoch(),
                    CARD_REBIND_COOLDOWN_SECONDS,
                    1,
                    "莫雪的小伙伴",
                ),
            )
    if os.name != "nt":
        os.chmod(DATABASE_PATH, 0o600)


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MonitorHandler(BaseHTTPRequestHandler):
    server_version = "GMZZMonitor/1.0"

    def log_message(self, format_string: str, *args) -> None:
        print(
            f"{self.log_date_time_string()} {self.client_address[0]} "
            + format_string % args,
            flush=True,
        )

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _json(self, status: int, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _database_busy_response(self, error: sqlite3.OperationalError) -> None:
        self.close_connection = True
        print(
            f"{self.log_date_time_string()} {self.client_address[0]} "
            f"SQLite busy response: {error}",
            flush=True,
        )
        if getattr(self, "_response_started", False):
            return
        value = {
            "ok": False,
            "error": "server_busy",
            "message": "服务器当前繁忙，请稍后重试。",
            "retry_after": 1,
        }
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._response_started = True
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Retry-After", "1")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _run_request(self, operation) -> None:
        self._response_started = False
        try:
            operation()
        except sqlite3.OperationalError as error:
            if not database_is_busy(error):
                raise
            try:
                self._database_busy_response(error)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    def _html(self, status: int, value: str) -> None:
        payload = value.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _update_access_allowed(self) -> bool:
        if not ENFORCE_BUILD_ALLOWLIST:
            return True
        session = self._session_for_token(self._bearer_token())
        request_build_id = clean_build_id(
            self.headers.get("X-DPS-Build-ID", "")
        )
        if session is None:
            self._authorization_denied("invalid_session")
            return False
        session_build_id = clean_build_id(session["build_id"])
        registration = client_build_registration(
            session["app_version"], session_build_id
        )
        if (
            registration is None
            or not request_build_id
            or not hmac.compare_digest(request_build_id, session_build_id)
        ):
            self._authorization_denied("client_build_not_allowed")
            return False
        return True

    def _update_info(self, current_version: str) -> None:
        loaded = load_update_metadata()
        if loaded is None:
            self._json(
                HTTPStatus.OK,
                {"ok": True, "available": False, "latest_version": ""},
            )
            return
        metadata, _update_path = loaded
        available = update_is_available(
            current_version,
            metadata["latest_version"],
            metadata["client_build"],
        )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "available": available,
                "latest_version": (
                    metadata["display_version"]
                    if available
                    else metadata["latest_version"]
                ),
                "download_path": (
                    "/api/v1/dps/update/download" if available else ""
                ),
                "sha256": metadata["sha256"] if available else "",
                "size": metadata["size"] if available else 0,
                "filename": metadata["filename"] if available else "",
                "notes": metadata["notes"] if available else "",
                "required": bool(metadata["required"]) if available else False,
                "build_id": metadata["build_id"] if available else "",
                "publisher_thumbprint": (
                    metadata["publisher_thumbprint"] if available else ""
                ),
                "signature_required": (
                    bool(metadata["signature_required"]) if available else False
                ),
                "rollback": False,
            },
        )

    def _rollback_info(self, version: str) -> None:
        target_version = normalize_rollback_version(version)
        if not rollback_version_is_supported(target_version):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "rollback_version_unsupported"},
            )
            return
        loaded = load_rollback_metadata(target_version)
        if loaded is None:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "rollback_unavailable"},
            )
            return
        metadata, _update_path = loaded
        encoded_version = quote(target_version, safe=".-")
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "available": True,
                "latest_version": metadata["display_version"],
                "download_path": (
                    "/api/v1/dps/update/rollback/download"
                    f"?version={encoded_version}"
                ),
                "sha256": metadata["sha256"],
                "size": metadata["size"],
                "filename": metadata["filename"],
                "notes": metadata["notes"],
                "required": False,
                "build_id": metadata["build_id"],
                "publisher_thumbprint": metadata["publisher_thumbprint"],
                "signature_required": bool(metadata["signature_required"]),
                "rollback": True,
            },
        )

    def _download_update(self) -> None:
        loaded = load_update_metadata()
        if loaded is None:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "update_unavailable"},
            )
            return
        metadata, update_path = loaded
        self._send_update_file(metadata, update_path)

    def _download_rollback(self, version: str) -> None:
        target_version = normalize_rollback_version(version)
        if not rollback_version_is_supported(target_version):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "rollback_version_unsupported"},
            )
            return
        loaded = load_rollback_metadata(target_version)
        if loaded is None:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "rollback_unavailable"},
            )
            return
        metadata, update_path = loaded
        self._send_update_file(metadata, update_path)

    def _send_update_file(
        self, metadata: dict[str, object], update_path: Path
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(metadata["size"]))
        encoded_filename = quote(str(metadata["filename"]), safe="")
        fallback_filename = update_download_fallback_filename(
            metadata["latest_version"]
        )
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{fallback_filename}"; '
            f"filename*=UTF-8''{encoded_filename}",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            with update_path.open("rb") as handle:
                while True:
                    chunk = handle.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _remote_ip(self) -> str:
        forwarded = self.headers.get("X-Real-IP", "").strip()
        return clean_text(forwarded or self.client_address[0], 64)

    def _bearer_token(self) -> str:
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            return ""
        return clean_text(authorization[len(prefix) :], 256)

    def _admin_authenticated(self) -> bool:
        if not ADMIN_PASSWORD:
            return False
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(
                authorization[6:].encode("ascii"), validate=True
            ).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return hmac.compare_digest(username, ADMIN_USER) and hmac.compare_digest(
            password, ADMIN_PASSWORD
        )

    def _require_admin(self) -> bool:
        if self._admin_authenticated():
            return True
        payload = b"Authentication required"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="DPS Monitor"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
        return False

    def do_GET(self) -> None:
        self._run_request(self._do_GET)

    def _do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/v1/dps/health":
            self._json(HTTPStatus.OK, {"ok": True, "server_time": now_epoch()})
            return
        if path == "/api/v1/dps/update":
            if not self._update_access_allowed():
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            current_version = clean_text(query.get("version", [""])[0], 32)
            self._update_info(current_version)
            return
        if path == "/api/v1/dps/update/download":
            if not self._update_access_allowed():
                return
            self._download_update()
            return
        if path == "/api/v1/dps/update/rollback":
            if not self._update_access_allowed():
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            target_version = clean_text(query.get("version", [""])[0], 32)
            self._rollback_info(target_version)
            return
        if path == "/api/v1/dps/update/rollback/download":
            if not self._update_access_allowed():
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            target_version = clean_text(query.get("version", [""])[0], 32)
            self._download_rollback(target_version)
            return
        if path == "/api/v1/dps/admin/status":
            if self._require_admin():
                self._admin_status()
            return
        if path == "/api/v1/dps/admin/feedback":
            if self._require_admin():
                self._admin_feedback_list()
            return
        if path == "/dps-monitor":
            if self._require_admin():
                self._html(HTTPStatus.OK, ADMIN_PAGE)
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        self._run_request(self._do_POST)

    def _do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/api/v1/dps/session/start":
            self._start_session()
            return
        if path == "/api/v1/dps/trial/claim":
            self._claim_trial()
            return
        if path == "/api/v1/dps/session/heartbeat":
            self._heartbeat()
            return
        if path == "/api/v1/dps/session/end":
            self._end_session()
            return
        if path == "/api/v1/dps/feedback":
            self._submit_feedback()
            return
        if path == "/api/v1/dps/diagnostic":
            self._submit_diagnostic()
            return
        if path == "/api/v1/dps/combat/clock":
            self._sync_combat_clock()
            return
        if path == "/api/v2/dps/combat/clock":
            self._sync_combat_clock_v2()
            return
        if path == "/api/v1/dps/admin/revoke":
            if self._require_admin():
                self._set_revoked()
            return
        if path == "/api/v1/dps/admin/cards/create":
            if self._require_admin():
                self._create_cards()
            return
        if path == "/api/v1/dps/admin/cards/update":
            if self._require_admin():
                self._update_card()
            return
        if path == "/api/v1/dps/admin/cards/batch-add-time":
            if self._require_admin():
                self._batch_add_card_time()
            return
        if path == "/api/v1/dps/admin/feedback/detail":
            if self._require_admin():
                self._admin_feedback_detail()
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def _authorization_denied(
        self, error: str, *, message: str = "", retry_after: int = 0
    ) -> None:
        payload = {
            "ok": False,
            "authorized": False,
            "error": error,
            "message": message or card_error(error),
        }
        if retry_after > 0:
            payload["retry_after"] = int(retry_after)
        self._json(HTTPStatus.FORBIDDEN, payload)

    def _claim_trial(self) -> None:
        body = self._body()
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        client_id = clean_client_id(body.get("client_id"))
        if not client_id:
            self._json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_client_id"}
            )
            return
        app_version = clean_text(body.get("app_version"), 32)
        build_id = clean_build_id(body.get("build_id"))
        registration = client_build_registration(app_version, build_id)
        if ENFORCE_BUILD_ALLOWLIST:
            if not BUILD_ID_PATTERN.fullmatch(build_id):
                self._authorization_denied("client_build_required")
                return
            if registration is None:
                self._authorization_denied("client_build_not_allowed")
                return
        request_id = clean_text(body.get("request_id"), 40).lower()
        if not TRIAL_REQUEST_ID_PATTERN.fullmatch(request_id):
            self._json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_request_id"}
            )
            return

        timestamp = now_epoch()
        claim_day = trial_claim_day(timestamp)
        next_available_at = trial_next_available_at(timestamp)
        remote_ip = self._remote_ip()
        display_name = clean_text(body.get("display_name"), 48) or (
            f"DPS-{client_id[:8]}"
        )
        with write_database() as connection:
            client = connection.execute(
                "SELECT revoked FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
            if client is not None and int(client["revoked"]):
                self._authorization_denied("client_revoked")
                return
            existing_claim = connection.execute(
                """
                SELECT t.card_hash, c.card_key
                FROM trial_claims t
                LEFT JOIN cards c
                    ON c.card_hash=t.card_hash AND c.deleted_at IS NULL
                WHERE t.client_id=? AND t.claim_day=?
                """,
                (client_id, claim_day),
            ).fetchone()
            if existing_claim is not None:
                card_key = str(existing_claim["card_key"] or "")
                if CARD_PATTERN.fullmatch(card_key):
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "card_key": card_key,
                            "duration_seconds": TRIAL_DURATION_SECONDS,
                            "next_available_at": next_available_at,
                            "message": "已恢复今天领取的 2 小时试用卡，请点击“登录”。",
                        },
                    )
                    return
                retry_after = max(1, int(math.ceil(next_available_at - timestamp)))
                self._authorization_denied(
                    "trial_daily_limit",
                    message=(
                        "今天已领取过试用卡，每台设备每天限领一次，"
                        "请明天再试。"
                    ),
                    retry_after=retry_after,
                )
                return

            connection.execute(
                """
                INSERT INTO clients(client_id, display_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    last_seen=excluded.last_seen
                """,
                (client_id, display_name, timestamp, timestamp),
            )
            while True:
                card_key = generate_card_key()
                card_hash = token_digest(card_key)
                try:
                    connection.execute(
                        """
                        INSERT INTO cards(
                            card_hash, card_key, card_suffix, duration_seconds,
                            created_at, bound_client_id, last_bound_at, sold,
                            permanent, note, remark
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                        """,
                        (
                            card_hash,
                            card_key,
                            card_key[-4:],
                            TRIAL_DURATION_SECONDS,
                            timestamp,
                            client_id,
                            timestamp,
                            TRIAL_CARD_NOTE,
                            TRIAL_CARD_REMARK,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                break
            connection.execute(
                """
                INSERT INTO trial_claims(
                    client_id, claim_day, card_hash, request_id, claimed_at, remote_ip
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (client_id, claim_day, card_hash, request_id, timestamp, remote_ip),
            )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "card_key": card_key,
                "duration_seconds": TRIAL_DURATION_SECONDS,
                "next_available_at": next_available_at,
                "message": "2 小时试用卡领取成功，请点击“登录”。",
            },
        )

    def _start_session(self) -> None:
        body = self._body()
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        client_id = clean_client_id(body.get("client_id"))
        if not client_id:
            self._json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_client_id"}
            )
            return
        raw_card_key = clean_text(body.get("card_key"), 80)
        if not raw_card_key:
            self._authorization_denied("card_required")
            return
        card_key = normalize_card_key(raw_card_key)
        if not card_key:
            self._authorization_denied("card_invalid")
            return
        card_hash = token_digest(card_key)
        timestamp = now_epoch()
        display_name = clean_text(body.get("display_name"), 48) or (
            f"DPS-{client_id[:8]}"
        )
        app_version = clean_text(body.get("app_version"), 32)
        build_id = clean_build_id(body.get("build_id"))
        registration = client_build_registration(app_version, build_id)
        if ENFORCE_BUILD_ALLOWLIST:
            if not BUILD_ID_PATTERN.fullmatch(build_id):
                self._authorization_denied("client_build_required")
                return
            if registration is None:
                self._authorization_denied("client_build_not_allowed")
                return
        remote_ip = self._remote_ip()
        session_id = secrets.token_hex(16)
        access_token = secrets.token_urlsafe(32)
        runtime_capability = issue_runtime_capability(
            session_id=session_id,
            client_id=client_id,
            app_version=app_version,
            build_id=build_id,
            lease_sequence=1,
            timestamp=timestamp,
            registration=registration,
        )
        protected_build = bool(registration and registration.get("protected"))
        if protected_build and runtime_capability is None:
            self._authorization_denied("runtime_capability_unavailable")
            return
        with write_database() as connection:
            card = connection.execute(
                "SELECT * FROM cards WHERE card_hash=? AND deleted_at IS NULL",
                (card_hash,),
            ).fetchone()
            if card is None:
                self._authorization_denied("card_invalid")
                return
            if int(card["revoked"]):
                self._authorization_denied("card_revoked")
                return
            permanent_card = bool(card["permanent"])
            card_tier = classify_card_tier(
                card["note"], card["duration_seconds"], permanent=permanent_card
            )
            expires_at = 0.0 if permanent_card else float(card["expires_at"] or 0)
            if not permanent_card and expires_at and expires_at <= timestamp:
                self._authorization_denied("card_expired")
                return
            online_threshold = timestamp - ONLINE_WINDOW
            connection.execute(
                """
                UPDATE sessions SET ended_at=?, using_app=0
                WHERE card_hash=? AND ended_at IS NULL AND last_seen<?
                """,
                (timestamp, card_hash, online_threshold),
            )
            bound_client_id = str(card["bound_client_id"] or "")
            activated_at = float(card["activated_at"] or 0)
            last_bound_at = float(card["last_bound_at"] or 0)
            binding_changed = bound_client_id != client_id
            device_changed = bool(bound_client_id) and binding_changed
            # Keep an emergency off-switch without changing the normal
            # twelve-hour transfer policy.
            device_binding_locked = (
                not CARD_DEVICE_REBIND_ENABLED
                and binding_changed
                and bool(bound_client_id or activated_at)
            )
            if card_tier == "partner" and device_changed:
                # Partner cards are single-device credentials.  Revoke before
                # checking active sessions so a second computer cannot merely
                # receive the ordinary "card in use" response.
                connection.execute(
                    "UPDATE cards SET revoked=1, last_used_at=? "
                    "WHERE card_hash=?",
                    (timestamp, card_hash),
                )
                connection.execute(
                    """
                    UPDATE sessions SET ended_at=?, using_app=0
                    WHERE card_hash=? AND ended_at IS NULL
                    """,
                    (timestamp, card_hash),
                )
                # Send the denial only after the revocation is durable; the
                # client may immediately issue a heartbeat with its old token.
                connection.commit()
                self._authorization_denied("partner_device_changed")
                return
            if device_binding_locked:
                self._authorization_denied("card_device_locked")
                return
            active_sessions = connection.execute(
                """
                SELECT session_id, client_id FROM sessions
                WHERE card_hash=? AND ended_at IS NULL AND last_seen>=?
                """,
                (card_hash, online_threshold),
            ).fetchall()
            if any(
                str(active_session["client_id"]) != client_id
                for active_session in active_sessions
            ):
                self._authorization_denied("card_in_use")
                return
            rebind_cooldown = max(
                0, int(card["rebind_cooldown_seconds"] or 0)
            )
            cooldown_started_at = last_bound_at
            if activated_at and not cooldown_started_at:
                # A legacy activated row may have lost its binding timestamp.
                # Its latest known use is the safest recoverable cooldown anchor.
                cooldown_started_at = max(
                    activated_at, float(card["last_used_at"] or 0)
                )
            if binding_changed and cooldown_started_at:
                retry_after = max(
                    0,
                    int(
                        math.ceil(
                            cooldown_started_at + rebind_cooldown - timestamp
                        )
                    ),
                )
                if retry_after:
                    hours, minutes = divmod((retry_after + 59) // 60, 60)
                    if hours and minutes:
                        wait_text = f"{hours} 小时 {minutes} 分钟"
                    elif hours:
                        wait_text = f"{hours} 小时"
                    else:
                        wait_text = f"{minutes} 分钟"
                    self._authorization_denied(
                        "card_rebind_cooldown",
                        message=(
                            "该卡号刚在另一台设备使用，"
                            f"还需 {wait_text} 才能在本机登录，请到时重试。"
                        ),
                        retry_after=retry_after,
                    )
                    return
            connection.execute(
                """
                INSERT INTO clients(client_id, display_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    last_seen=excluded.last_seen
                """,
                (client_id, display_name, timestamp, timestamp),
            )
            client = connection.execute(
                "SELECT revoked FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
            if client is None or int(client["revoked"]):
                self._authorization_denied("client_revoked")
                return
            if permanent_card:
                activated_at = activated_at or timestamp
                expires_at = 0.0
            elif not activated_at:
                activated_at = timestamp
                expires_at = timestamp + int(card["duration_seconds"])
            if binding_changed:
                connection.execute(
                    """
                    UPDATE sessions SET ended_at=?, using_app=0
                    WHERE card_hash=? AND ended_at IS NULL
                    """,
                    (timestamp, card_hash),
                )
                last_bound_at = timestamp
            connection.execute(
                """
                UPDATE cards SET activated_at=?, expires_at=?,
                    bound_client_id=?, last_bound_at=?, last_used_at=?
                WHERE card_hash=? AND deleted_at IS NULL
                """,
                (
                    activated_at,
                    expires_at,
                    client_id,
                    last_bound_at,
                    timestamp,
                    card_hash,
                ),
            )
            client_sessions = connection.execute(
                """
                SELECT session_id, app_version, build_id FROM sessions
                WHERE client_id=? AND ended_at IS NULL
                """,
                (client_id,),
            ).fetchall()
            replaced_session_ids = [
                str(active_session["session_id"])
                for active_session in client_sessions
                if (
                    not same_release_version(
                        active_session["app_version"], app_version
                    )
                    or clean_build_id(active_session["build_id"]) != build_id
                )
            ]
            connection.executemany(
                """
                UPDATE sessions SET ended_at=?, using_app=0
                WHERE session_id=? AND ended_at IS NULL
                """,
                (
                    (timestamp, replaced_session_id)
                    for replaced_session_id in replaced_session_ids
                ),
            )
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, client_id, token_hash, started_at, last_seen,
                    app_version, build_id, remote_ip, card_hash, lease_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    client_id,
                    token_digest(access_token),
                    timestamp,
                    timestamp,
                    app_version,
                    build_id,
                    remote_ip,
                    card_hash,
                    1 if runtime_capability is not None else 0,
                ),
            )
        response = {
                "ok": True,
                "authorized": True,
                "session_id": session_id,
                "access_token": access_token,
                "display_name": display_name,
                "heartbeat_interval": HEARTBEAT_INTERVAL,
                "expires_at": expires_at,
                "remaining_seconds": (
                    None
                    if permanent_card
                    else max(0, int(expires_at - timestamp))
                ),
                "card_tier": card_tier,
                "build_id": build_id,
            }
        if runtime_capability is not None:
            response["runtime_capability"] = runtime_capability
        self._json(HTTPStatus.OK, response)

    def _session_for_token(self, token: str) -> sqlite3.Row | None:
        if not token:
            return None
        with database() as connection:
            return connection.execute(
                """
                SELECT s.*, c.revoked AS client_revoked,
                    k.revoked AS card_revoked, k.expires_at AS card_expires_at,
                    k.card_suffix AS card_suffix, k.note AS card_note,
                    k.duration_seconds AS card_duration_seconds,
                    k.card_key AS card_key, k.permanent AS card_permanent
                FROM sessions s
                JOIN clients c ON c.client_id=s.client_id
                LEFT JOIN cards k
                    ON k.card_hash=s.card_hash AND k.deleted_at IS NULL
                WHERE s.token_hash=? AND s.ended_at IS NULL
                """,
                (token_digest(token),),
            ).fetchone()

    def _heartbeat(self) -> None:
        body = self._body()
        token = self._bearer_token()
        session = self._session_for_token(token)
        if body is None or session is None:
            self._authorization_denied("invalid_session")
            return
        timestamp = now_epoch()
        if int(session["client_revoked"]):
            self._end_authorized_session(session["session_id"])
            self._authorization_denied("client_revoked")
            return
        session_card_hash = str(session["card_hash"] or "")
        permanent_card = bool(session["card_permanent"])
        if session_card_hash:
            if session["card_revoked"] is None:
                self._end_authorized_session(session["session_id"])
                self._authorization_denied("card_invalid")
                return
            if int(session["card_revoked"]):
                self._end_authorized_session(session["session_id"])
                self._authorization_denied("card_revoked")
                return
            expires_at = (
                0.0
                if permanent_card
                else float(session["card_expires_at"] or 0)
            )
            if not permanent_card and (not expires_at or expires_at <= timestamp):
                self._end_authorized_session(session["session_id"])
                self._authorization_denied("card_expired")
                return
        else:
            # Existing sessions created before card authorization was deployed
            # may finish normally. New sessions always require a valid card.
            expires_at = 0.0
        using_app = 1 if bool(body.get("using")) else 0
        character_name = clean_text(body.get("character_name"), 48)
        app_version = clean_text(body.get("app_version"), 32) or str(
            session["app_version"] or ""
        )
        build_id = clean_build_id(body.get("build_id")) or clean_build_id(
            session["build_id"]
        )
        registration = client_build_registration(app_version, build_id)
        if ENFORCE_BUILD_ALLOWLIST:
            session_version = str(session["app_version"] or "")
            session_build_id = clean_build_id(session["build_id"])
            if (
                registration is None
                or not hmac.compare_digest(app_version, session_version)
                or not hmac.compare_digest(build_id, session_build_id)
            ):
                self._end_authorized_session(session["session_id"])
                self._authorization_denied("client_build_mismatch")
                return
        protected_build = bool(registration and registration.get("protected"))
        try:
            game_pid = max(0, int(body.get("game_pid", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            game_pid = 0
        with write_database() as connection:
            connection.execute(
                """
                UPDATE sessions SET
                    last_seen=?, using_app=?, app_version=?, build_id=?, character_name=?,
                    game_pid=?, remote_ip=?, lease_sequence=lease_sequence+?
                WHERE session_id=?
                """,
                (
                    timestamp,
                    using_app,
                    app_version,
                    build_id,
                    character_name,
                    game_pid,
                    self._remote_ip(),
                    1 if protected_build else 0,
                    session["session_id"],
                ),
            )
            lease_row = connection.execute(
                "SELECT lease_sequence FROM sessions WHERE session_id=?",
                (session["session_id"],),
            ).fetchone()
            connection.execute(
                "UPDATE clients SET last_seen=? WHERE client_id=?",
                (timestamp, session["client_id"]),
            )
            if session_card_hash:
                connection.execute(
                    """UPDATE cards SET last_used_at=?
                       WHERE card_hash=? AND deleted_at IS NULL""",
                    (timestamp, session_card_hash),
                )
        lease_sequence = int(lease_row["lease_sequence"] if lease_row else 0)
        runtime_capability = issue_runtime_capability(
            session_id=str(session["session_id"]),
            client_id=str(session["client_id"]),
            app_version=app_version,
            build_id=build_id,
            lease_sequence=lease_sequence,
            timestamp=timestamp,
            registration=registration,
        )
        if protected_build and runtime_capability is None:
            self._end_authorized_session(session["session_id"])
            self._authorization_denied("runtime_capability_unavailable")
            return
        response = {
                "ok": True,
                "authorized": True,
                "heartbeat_interval": HEARTBEAT_INTERVAL,
                "server_time": timestamp,
                "expires_at": expires_at,
                "remaining_seconds": (
                    None
                    if permanent_card
                    else max(0, int(expires_at - timestamp))
                ),
                "card_tier": classify_card_tier(
                    session["card_note"],
                    session["card_duration_seconds"],
                    permanent=permanent_card,
                ),
                "build_id": build_id,
            }
        if runtime_capability is not None:
            response["runtime_capability"] = runtime_capability
        self._json(HTTPStatus.OK, response)

    def _end_authorized_session(self, session_id: object) -> None:
        with write_database() as connection:
            connection.execute(
                "UPDATE sessions SET ended_at=?, using_app=0 WHERE session_id=?",
                (now_epoch(), str(session_id)),
            )

    def _end_session(self) -> None:
        token = self._bearer_token()
        session = self._session_for_token(token)
        if session is not None:
            with write_database() as connection:
                connection.execute(
                    "UPDATE sessions SET ended_at=?, using_app=0 WHERE session_id=?",
                    (now_epoch(), session["session_id"]),
                )
        self._json(HTTPStatus.OK, {"ok": True})

    @staticmethod
    def _feedback_session_error(
        session: sqlite3.Row, timestamp: float
    ) -> str:
        if int(session["client_revoked"]):
            return "client_revoked"
        card_hash = str(session["card_hash"] or "")
        if not card_hash:
            return ""
        if session["card_revoked"] is None:
            return "card_invalid"
        if int(session["card_revoked"]):
            return "card_revoked"
        if not bool(session["card_permanent"]):
            expires_at = float(session["card_expires_at"] or 0)
            if not expires_at or expires_at <= timestamp:
                return "card_expired"
        return ""

    @staticmethod
    def _combat_clock_number(
        value: object, *, minimum: float = 0.0, maximum: float
    ) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
            return None
        return parsed

    def _sync_combat_clock(self) -> None:
        body = self._body()
        session = self._session_for_token(self._bearer_token())
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        if session is None:
            self._authorization_denied("invalid_session")
            return
        timestamp = now_epoch()
        authorization_error = self._feedback_session_error(session, timestamp)
        if authorization_error:
            self._end_authorized_session(session["session_id"])
            self._authorization_denied(authorization_error)
            return

        party_key = clean_text(body.get("party_key"), 64).casefold()
        target_key = clean_text(body.get("target_key"), 64).casefold()
        client_encounter_id = clean_text(body.get("encounter_id"), 96)
        state = clean_text(body.get("state"), 16).casefold()
        elapsed = self._combat_clock_number(
            body.get("elapsed_seconds"),
            minimum=0.001,
            maximum=float(COMBAT_CLOCK_MAX_SECONDS),
        )
        end_age = self._combat_clock_number(
            body.get("end_age_seconds", 0.0),
            minimum=0.0,
            maximum=float(COMBAT_CLOCK_MAX_SECONDS),
        )
        try:
            total_damage = max(0, int(body.get("total_damage", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            total_damage = -1
        if (
            not COMBAT_CLOCK_KEY_PATTERN.fullmatch(party_key)
            or not COMBAT_CLOCK_KEY_PATTERN.fullmatch(target_key)
            or not COMBAT_CLOCK_ENCOUNTER_PATTERN.fullmatch(client_encounter_id)
            or state not in {"active", "ended"}
            or elapsed is None
            or end_age is None
            or total_damage <= 0
        ):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "bad_combat_clock"},
            )
            return

        report_ended = state == "ended"
        candidate_end = timestamp - end_age if report_ended else 0.0
        candidate_start = (candidate_end or timestamp) - elapsed
        clock_id = ""
        with write_database() as connection:
            mapped = connection.execute(
                """
                SELECT c.* FROM combat_clock_clients m
                JOIN combat_clocks c ON c.clock_id=m.clock_id
                WHERE m.session_id=? AND m.client_encounter_id=?
                """,
                (session["session_id"], client_encounter_id),
            ).fetchone()
            clock = mapped
            if clock is None:
                candidates = connection.execute(
                    """
                    SELECT * FROM combat_clocks
                    WHERE party_key=? AND target_key=? AND last_seen>=?
                    ORDER BY last_seen DESC LIMIT 8
                    """,
                    (
                        party_key,
                        target_key,
                        timestamp - COMBAT_CLOCK_RETENTION_SECONDS,
                    ),
                ).fetchall()
                best: sqlite3.Row | None = None
                best_difference = float("inf")
                for candidate in candidates:
                    difference = abs(
                        float(candidate["started_at"]) - candidate_start
                    )
                    ended_at = candidate["ended_at"]
                    if ended_at is None:
                        eligible = difference <= COMBAT_CLOCK_ACTIVE_MATCH_SECONDS
                    else:
                        eligible = bool(
                            report_ended
                            and timestamp - float(ended_at)
                            <= COMBAT_CLOCK_LATE_REPORT_SECONDS
                            and difference <= COMBAT_CLOCK_ENDED_MATCH_SECONDS
                            and int(candidate["max_total_damage"] or 0)
                            == total_damage
                        )
                    if eligible and difference < best_difference:
                        best = candidate
                        best_difference = difference
                clock = best

            if clock is None:
                clock_id = secrets.token_hex(16)
                initial_end = candidate_end if report_ended else None
                connection.execute(
                    """
                    INSERT INTO combat_clocks(
                        clock_id, party_key, target_key, started_at, ended_at,
                        created_at, last_seen, max_total_damage, report_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        clock_id,
                        party_key,
                        target_key,
                        candidate_start,
                        initial_end,
                        timestamp,
                        timestamp,
                        total_damage,
                    ),
                )
                started_at = candidate_start
                ended_at = initial_end
            else:
                clock_id = str(clock["clock_id"])
                started_at = float(clock["started_at"])
                ended_at = (
                    None
                    if clock["ended_at"] is None
                    else float(clock["ended_at"])
                )
                if ended_at is None:
                    # A later peer may have observed the opening packet first.
                    # Only move the shared start within the strict match window.
                    if abs(candidate_start - started_at) <= COMBAT_CLOCK_ACTIVE_MATCH_SECONDS:
                        started_at = min(started_at, candidate_start)
                    if report_ended:
                        ended_at = max(started_at + 0.001, candidate_end)
                connection.execute(
                    """
                    UPDATE combat_clocks SET
                        started_at=?, ended_at=?, last_seen=?,
                        max_total_damage=MAX(max_total_damage, ?),
                        report_count=report_count+1
                    WHERE clock_id=?
                    """,
                    (
                        started_at,
                        ended_at,
                        timestamp,
                        total_damage,
                        clock_id,
                    ),
                )

            connection.execute(
                """
                INSERT INTO combat_clock_clients(
                    session_id, client_encounter_id, clock_id, last_seen, ended
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, client_encounter_id) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    ended=MAX(combat_clock_clients.ended, excluded.ended)
                """,
                (
                    session["session_id"],
                    client_encounter_id,
                    clock_id,
                    timestamp,
                    1 if report_ended else 0,
                ),
            )
            cleanup_expired_combat_clocks(connection, timestamp)

        effective_end = ended_at if ended_at is not None else timestamp
        duration = max(1.0, effective_end - started_at)
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "synchronized": True,
                "encounter_id": client_encounter_id,
                "clock_id": clock_id,
                "started_at": started_at,
                "ended_at": ended_at or 0.0,
                "duration_seconds": duration,
                "final": ended_at is not None,
                "server_time": timestamp,
            },
        )

    @staticmethod
    def _combat_clock_v2_payload(
        clock: sqlite3.Row,
        *,
        client_encounter_id: str,
        client_revision: int,
        timestamp: float,
    ) -> dict[str, object]:
        state = str(clock["state"] or "active").casefold()
        started_at = float(clock["started_at"])
        if state == "final" and clock["ended_at"] is not None:
            effective_end = float(clock["ended_at"])
            ended_at = effective_end
        elif state == "settling" and clock["provisional_ended_at"] is not None:
            effective_end = float(clock["provisional_ended_at"])
            ended_at = 0.0
        else:
            state = "active"
            effective_end = timestamp
            ended_at = 0.0
        return {
            "ok": True,
            "synchronized": True,
            "encounter_id": client_encounter_id,
            "clock_id": str(clock["clock_id"]),
            "party_key": str(clock["party_key"]),
            "target_key": str(clock["target_key"]),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": max(1.0, effective_end - started_at),
            "state": state,
            "final": state == "final",
            "revision": max(0, int(clock["revision"] or 0)),
            "client_revision": max(0, int(client_revision)),
            "total_damage": max(0, int(clock["max_total_damage"] or 0)),
            "server_time": timestamp,
        }

    def _sync_combat_clock_v2(self) -> None:
        """Synchronize a reversible, revisioned party combat clock.

        ``settling`` is deliberately provisional. A later, higher-revision
        ``active`` report (or continued damage) reopens the same clock instead
        of leaving a phase transition permanently frozen as v1 did.
        """

        body = self._body()
        session = self._session_for_token(self._bearer_token())
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        if session is None:
            self._authorization_denied("invalid_session")
            return
        timestamp = now_epoch()
        authorization_error = self._feedback_session_error(session, timestamp)
        if authorization_error:
            self._end_authorized_session(session["session_id"])
            self._authorization_denied(authorization_error)
            return

        party_key = clean_text(body.get("party_key"), 64).casefold()
        target_key = clean_text(body.get("target_key"), 64).casefold()
        client_encounter_id = clean_text(body.get("encounter_id"), 96)
        state = clean_text(body.get("state"), 16).casefold()
        end_reason = clean_text(body.get("end_reason"), 48).casefold()
        elapsed = self._combat_clock_number(
            body.get("elapsed_seconds"),
            minimum=0.001,
            maximum=float(COMBAT_CLOCK_MAX_SECONDS),
        )
        end_age = self._combat_clock_number(
            body.get("end_age_seconds", 0.0),
            minimum=0.0,
            maximum=float(COMBAT_CLOCK_MAX_SECONDS),
        )
        activity_age = self._combat_clock_number(
            body.get("activity_age_seconds", 0.0),
            minimum=0.0,
            maximum=float(COMBAT_CLOCK_MAX_SECONDS),
        )
        try:
            total_damage = max(0, int(body.get("total_damage", 0) or 0))
            client_revision = int(body.get("client_revision", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            total_damage = -1
            client_revision = 0
        if (
            not COMBAT_CLOCK_KEY_PATTERN.fullmatch(party_key)
            or not COMBAT_CLOCK_KEY_PATTERN.fullmatch(target_key)
            or not COMBAT_CLOCK_ENCOUNTER_PATTERN.fullmatch(client_encounter_id)
            or state not in {"active", "settling", "final"}
            or elapsed is None
            or end_age is None
            or activity_age is None
            or total_damage <= 0
            or not 1 <= client_revision <= COMBAT_CLOCK_V2_MAX_REVISION
        ):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "bad_combat_clock"},
            )
            return

        candidate_end = timestamp if state == "active" else timestamp - end_age
        candidate_start = candidate_end - elapsed
        last_activity_at = timestamp - activity_age
        response: dict[str, object]
        with write_database() as connection:
            mapping = connection.execute(
                """
                SELECT * FROM combat_clock_clients_v2
                WHERE session_id=? AND client_encounter_id=?
                """,
                (session["session_id"], client_encounter_id),
            ).fetchone()
            clock = None
            if mapping is not None:
                clock = connection.execute(
                    "SELECT * FROM combat_clocks_v2 WHERE clock_id=?",
                    (mapping["clock_id"],),
                ).fetchone()
                if clock is None:
                    mapping = None
                elif (
                    str(clock["party_key"]) != party_key
                    or str(clock["target_key"]) != target_key
                ):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "combat_clock_scope_changed"},
                    )
                    return

            if mapping is None:
                candidates = connection.execute(
                    """
                    SELECT * FROM combat_clocks_v2
                    WHERE party_key=? AND target_key=? AND last_seen>=?
                    ORDER BY last_seen DESC LIMIT 8
                    """,
                    (
                        party_key,
                        target_key,
                        timestamp - COMBAT_CLOCK_RETENTION_SECONDS,
                    ),
                ).fetchall()
                best: sqlite3.Row | None = None
                best_difference = float("inf")
                for candidate in candidates:
                    difference = abs(
                        float(candidate["started_at"]) - candidate_start
                    )
                    candidate_state = str(candidate["state"] or "active")
                    if candidate_state == "final":
                        recently_final = bool(
                            candidate["ended_at"] is not None
                            and timestamp - float(candidate["ended_at"])
                            <= COMBAT_CLOCK_LATE_REPORT_SECONDS
                            and difference <= COMBAT_CLOCK_ENDED_MATCH_SECONDS
                        )
                        if state == "final":
                            eligible = bool(
                                recently_final
                                and int(candidate["max_total_damage"] or 0)
                                == total_damage
                            )
                        else:
                            # A phase continuation can be the first report
                            # proving that an earlier final was premature.  A
                            # new pull starts below the old total and therefore
                            # cannot reopen this clock.
                            eligible = bool(
                                recently_final
                                and total_damage
                                >= int(candidate["max_total_damage"] or 0)
                            )
                    else:
                        eligible = difference <= COMBAT_CLOCK_ACTIVE_MATCH_SECONDS
                    if eligible and difference < best_difference:
                        best = candidate
                        best_difference = difference
                clock = best

            if clock is None:
                clock_id = secrets.token_hex(16)
                connection.execute(
                    """
                    INSERT INTO combat_clocks_v2(
                        clock_id, party_key, target_key, started_at,
                        provisional_ended_at, ended_at, state, created_at,
                        last_seen, max_total_damage, revision, report_count
                    ) VALUES (?, ?, ?, ?, NULL, NULL, 'active', ?, ?, 0, 0, 0)
                    """,
                    (
                        clock_id,
                        party_key,
                        target_key,
                        candidate_start,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                clock_id = str(clock["clock_id"])

            if mapping is None:
                connection.execute(
                    """
                    INSERT INTO combat_clock_clients_v2(
                        session_id, client_encounter_id, clock_id,
                        client_revision, state, candidate_started_at,
                        candidate_ended_at, last_activity_at, total_damage,
                        end_reason, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session["session_id"],
                        client_encounter_id,
                        clock_id,
                        client_revision,
                        state,
                        candidate_start,
                        None if state == "active" else candidate_end,
                        last_activity_at,
                        total_damage,
                        end_reason,
                        timestamp,
                    ),
                )
            else:
                previous_revision = int(mapping["client_revision"] or 0)
                if client_revision < previous_revision:
                    current = connection.execute(
                        "SELECT * FROM combat_clocks_v2 WHERE clock_id=?",
                        (clock_id,),
                    ).fetchone()
                    response = self._combat_clock_v2_payload(
                        current,
                        client_encounter_id=client_encounter_id,
                        client_revision=previous_revision,
                        timestamp=timestamp,
                    )
                    self._json(HTTPStatus.OK, response)
                    return
                if client_revision == previous_revision:
                    # Idempotent retries keep a final reporter live while the
                    # server waits briefly for another active peer to settle.
                    connection.execute(
                        """
                        UPDATE combat_clock_clients_v2 SET last_seen=?
                        WHERE session_id=? AND client_encounter_id=?
                        """,
                        (
                            timestamp,
                            session["session_id"],
                            client_encounter_id,
                        ),
                    )
                else:
                    previous_start = float(mapping["candidate_started_at"])
                    if (
                        abs(candidate_start - previous_start)
                        <= COMBAT_CLOCK_ACTIVE_MATCH_SECONDS
                    ):
                        candidate_start = min(previous_start, candidate_start)
                    else:
                        candidate_start = previous_start
                    connection.execute(
                        """
                        UPDATE combat_clock_clients_v2 SET
                            client_revision=?, state=?, candidate_started_at=?,
                            candidate_ended_at=?, last_activity_at=?,
                            total_damage=MAX(total_damage, ?), end_reason=?,
                            last_seen=?
                        WHERE session_id=? AND client_encounter_id=?
                        """,
                        (
                            client_revision,
                            state,
                            candidate_start,
                            None if state == "active" else candidate_end,
                            last_activity_at,
                            total_damage,
                            end_reason,
                            timestamp,
                            session["session_id"],
                            client_encounter_id,
                        ),
                    )

            client_rows = connection.execute(
                """
                SELECT * FROM combat_clock_clients_v2
                WHERE clock_id=?
                """,
                (clock_id,),
            ).fetchall()
            started_at = min(
                float(row["candidate_started_at"]) for row in client_rows
            )
            max_total_damage = max(
                max(0, int(row["total_damage"] or 0)) for row in client_rows
            )
            recent_cutoff = timestamp - COMBAT_CLOCK_V2_CLIENT_LIVE_SECONDS
            recent_rows = [
                row for row in client_rows if float(row["last_seen"]) >= recent_cutoff
            ]
            active_rows = [
                row for row in recent_rows if str(row["state"]) == "active"
            ]
            settling_rows = [
                row for row in recent_rows if str(row["state"]) == "settling"
            ]
            final_rows = [
                row for row in recent_rows if str(row["state"]) == "final"
            ]
            if active_rows:
                aggregate_state = "active"
                provisional_ended_at = None
                ended_at = None
            elif settling_rows:
                aggregate_state = "settling"
                end_candidates = [
                    float(row["candidate_ended_at"])
                    for row in settling_rows + final_rows
                    if row["candidate_ended_at"] is not None
                ]
                provisional_ended_at = max(end_candidates or [timestamp])
                provisional_ended_at = max(
                    started_at + 1.0, provisional_ended_at
                )
                ended_at = None
            else:
                aggregate_state = "final"
                end_candidates = [
                    float(row["candidate_ended_at"])
                    for row in final_rows
                    if row["candidate_ended_at"] is not None
                ]
                provisional_ended_at = max(end_candidates or [timestamp])
                provisional_ended_at = max(
                    started_at + 1.0, provisional_ended_at
                )
                ended_at = provisional_ended_at

            connection.execute(
                """
                UPDATE combat_clocks_v2 SET
                    started_at=?, provisional_ended_at=?, ended_at=?, state=?,
                    last_seen=?, max_total_damage=MAX(max_total_damage, ?),
                    revision=revision+1, report_count=report_count+1
                WHERE clock_id=?
                """,
                (
                    started_at,
                    provisional_ended_at,
                    ended_at,
                    aggregate_state,
                    timestamp,
                    max_total_damage,
                    clock_id,
                ),
            )
            cleanup_expired_combat_clocks(connection, timestamp)
            current = connection.execute(
                "SELECT * FROM combat_clocks_v2 WHERE clock_id=?",
                (clock_id,),
            ).fetchone()
            accepted_mapping = connection.execute(
                """
                SELECT client_revision FROM combat_clock_clients_v2
                WHERE session_id=? AND client_encounter_id=?
                """,
                (session["session_id"], client_encounter_id),
            ).fetchone()
            response = self._combat_clock_v2_payload(
                current,
                client_encounter_id=client_encounter_id,
                client_revision=int(accepted_mapping["client_revision"]),
                timestamp=timestamp,
            )

        self._json(HTTPStatus.OK, response)

    def _submit_feedback(self) -> None:
        body = self._body()
        session = self._session_for_token(self._bearer_token())
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        if session is None:
            self._authorization_denied("invalid_session")
            return
        timestamp = now_epoch()
        authorization_error = self._feedback_session_error(session, timestamp)
        if authorization_error:
            self._end_authorized_session(session["session_id"])
            self._authorization_denied(authorization_error)
            return
        content = clean_multiline_text(body.get("content"), MAX_FEEDBACK_CONTENT)
        if not content:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error": "feedback_content_required",
                    "message": "请填写问题描述。",
                },
            )
            return
        category = clean_text(body.get("category"), 32).casefold()
        if category not in FEEDBACK_CATEGORIES:
            category = "other"
        diagnostics = body.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        try:
            diagnostics_json = json.dumps(
                diagnostics, ensure_ascii=False, separators=(",", ":")
            )
        except (TypeError, ValueError, OverflowError):
            diagnostics_json = "{}"
        if len(diagnostics_json.encode("utf-8")) > MAX_FEEDBACK_DIAGNOSTICS_BYTES:
            diagnostics_json = json.dumps(
                {"omitted": "diagnostics_too_large"}, separators=(",", ":")
            )
        feedback_id = "FB" + secrets.token_hex(8).upper()
        character_name = clean_text(body.get("character_name"), 48) or str(
            session["character_name"] or ""
        )
        app_version = clean_text(body.get("app_version"), 32) or str(
            session["app_version"] or ""
        )
        with write_database() as connection:
            connection.execute(
                """
                INSERT INTO feedbacks(
                    feedback_id, created_at, updated_at, session_id,
                    client_id, card_hash, card_key, character_name,
                    app_version, category, content, diagnostics_json,
                    remote_ip
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    timestamp,
                    timestamp,
                    str(session["session_id"] or ""),
                    str(session["client_id"] or ""),
                    str(session["card_hash"] or ""),
                    str(session["card_key"] or ""),
                    character_name[:48],
                    app_version[:32],
                    category,
                    content,
                    diagnostics_json,
                    self._remote_ip(),
                ),
            )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "feedback_id": feedback_id,
                "message": "反馈已提交。",
            },
        )

    def _submit_diagnostic(self) -> None:
        body = self._body()
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        tool_name = clean_text(body.get("tool_name"), 64)
        if tool_name != DIAGNOSTIC_TOOL_NAME:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_diagnostic_tool"},
            )
            return
        diagnostics = body.get("diagnostics")
        if not isinstance(diagnostics, dict):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "diagnostics_required"},
            )
            return
        try:
            diagnostics_json = json.dumps(
                diagnostics, ensure_ascii=False, separators=(",", ":")
            )
        except (TypeError, ValueError, OverflowError):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_diagnostics"},
            )
            return
        if len(diagnostics_json.encode("utf-8")) > MAX_DIAGNOSTIC_REPORT_BYTES:
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "diagnostics_too_large"},
            )
            return

        timestamp = now_epoch()
        remote_ip = self._remote_ip()
        client_id = clean_client_id(body.get("client_id"))
        tool_version = clean_text(body.get("tool_version"), 32)
        diagnostic_id = "DG" + secrets.token_hex(8).upper()
        with write_database() as connection:
            recent_count = connection.execute(
                """
                SELECT COUNT(*) FROM feedbacks
                WHERE category='diagnostic' AND remote_ip=? AND created_at>=?
                """,
                (remote_ip, timestamp - 3600),
            ).fetchone()[0]
            if int(recent_count or 0) >= MAX_DIAGNOSTIC_REPORTS_PER_HOUR:
                self._json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {
                        "ok": False,
                        "error": "diagnostic_rate_limited",
                        "message": "检测报告上传过于频繁，请稍后重试。",
                    },
                )
                return

            recent_session = None
            if client_id:
                recent_session = connection.execute(
                    """
                    SELECT s.session_id, s.card_hash, s.character_name,
                        s.app_version, k.card_key
                    FROM sessions s
                    LEFT JOIN cards k ON k.card_hash=s.card_hash
                    WHERE s.client_id=?
                    ORDER BY s.last_seen DESC
                    LIMIT 1
                    """,
                    (client_id,),
                ).fetchone()
            connection.execute(
                """
                INSERT INTO feedbacks(
                    feedback_id, created_at, updated_at, session_id,
                    client_id, card_hash, card_key, character_name,
                    app_version, category, content, diagnostics_json,
                    remote_ip
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    diagnostic_id,
                    timestamp,
                    timestamp,
                    str(recent_session["session_id"] or "")
                    if recent_session is not None
                    else "",
                    client_id,
                    str(recent_session["card_hash"] or "")
                    if recent_session is not None
                    else "",
                    str(recent_session["card_key"] or "")
                    if recent_session is not None
                    else "",
                    str(recent_session["character_name"] or "")[:48]
                    if recent_session is not None
                    else "",
                    str(recent_session["app_version"] or "")[:32]
                    if recent_session is not None
                    else tool_version,
                    "diagnostic",
                    f"{DIAGNOSTIC_TOOL_NAME}自动报告",
                    diagnostics_json,
                    remote_ip,
                ),
            )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "diagnostic_id": diagnostic_id,
                "message": "检测报告已上传。",
            },
        )

    def _admin_feedback_list(self) -> None:
        with database() as connection:
            rows = connection.execute(
                """
                SELECT feedback_id, created_at, updated_at, client_id,
                    card_key, character_name, app_version, category,
                    content, remote_ip, diagnostics_json
                FROM feedbacks
                ORDER BY created_at DESC
                LIMIT 500
                """
            ).fetchall()
            counts = connection.execute(
                "SELECT COUNT(*) AS total FROM feedbacks"
            ).fetchone()
        feedbacks = []
        for row in rows:
            content = str(row["content"] or "")
            diagnostics_json = str(row["diagnostics_json"] or "{}")
            category = str(row["category"] or "other")
            feedbacks.append(
                {
                    "feedback_id": str(row["feedback_id"]),
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                    "client_id": str(row["client_id"] or ""),
                    "card_key": str(row["card_key"] or ""),
                    "character_name": str(row["character_name"] or ""),
                    "app_version": str(row["app_version"] or ""),
                    "category": category,
                    "category_label": FEEDBACK_CATEGORIES.get(
                        category, FEEDBACK_CATEGORIES["other"]
                    ),
                    "content_preview": content[:180],
                    "remote_ip": str(row["remote_ip"] or ""),
                    "has_diagnostics": diagnostics_json not in {"", "{}"},
                }
            )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "server_time": now_epoch(),
                "total": int(counts["total"] or 0),
                "feedbacks": feedbacks,
            },
        )

    def _admin_feedback_detail(self) -> None:
        body = self._body()
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        feedback_id = clean_text(body.get("feedback_id"), 32)
        with database() as connection:
            row = connection.execute(
                "SELECT * FROM feedbacks WHERE feedback_id=?", (feedback_id,)
            ).fetchone()
        if row is None:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "feedback_not_found"},
            )
            return
        try:
            diagnostics = json.loads(str(row["diagnostics_json"] or "{}"))
        except (TypeError, ValueError):
            diagnostics = {}
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "feedback": {
                    "feedback_id": str(row["feedback_id"]),
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                    "client_id": str(row["client_id"] or ""),
                    "card_key": str(row["card_key"] or ""),
                    "character_name": str(row["character_name"] or ""),
                    "app_version": str(row["app_version"] or ""),
                    "category": str(row["category"] or "other"),
                    "content": str(row["content"] or ""),
                    "diagnostics": diagnostics,
                    "remote_ip": str(row["remote_ip"] or ""),
                },
            },
        )

    def _admin_status(self) -> None:
        timestamp = now_epoch()
        threshold = timestamp - ONLINE_WINDOW
        with database() as connection:
            totals = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE WHEN s.last_seen>=? AND s.ended_at IS NULL
                        THEN CASE WHEN s.card_hash<>''
                            THEN 'card:' || s.card_hash
                            ELSE 'client:' || s.client_id END END) AS logged_in,
                    COUNT(DISTINCT CASE WHEN s.last_seen>=? AND s.ended_at IS NULL
                        AND s.using_app=1 THEN CASE WHEN s.card_hash<>''
                            THEN 'card:' || s.card_hash
                            ELSE 'client:' || s.client_id END END) AS using_now,
                    COUNT(DISTINCT CASE WHEN s.last_seen>=? THEN s.client_id END)
                        AS seen_24h
                FROM sessions s
                """,
                (threshold, threshold, timestamp - 86400),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT s.*, c.display_name, c.revoked,
                    k.card_key, k.card_suffix, k.expires_at AS card_expires_at
                FROM sessions s
                JOIN clients c ON c.client_id=s.client_id
                LEFT JOIN cards k ON k.card_hash=s.card_hash
                WHERE s.last_seen>=?
                ORDER BY s.last_seen DESC
                LIMIT 5000
                """,
                (timestamp - 86400,),
            ).fetchall()
            card_rows = connection.execute(
                """
                SELECT * FROM cards
                WHERE deleted_at IS NULL
                ORDER BY created_at DESC
                LIMIT 5000
                """
            ).fetchall()
            feedback_counts = connection.execute(
                "SELECT COUNT(*) AS total FROM feedbacks"
            ).fetchone()
        grouped_rows: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            client_id = str(row["client_id"])
            card_hash = str(row["card_hash"] or "")
            group_key = f"card:{card_hash}" if card_hash else f"client:{client_id}"
            grouped_rows.setdefault(group_key, []).append(row)

        sessions = []
        for group in grouped_rows.values():
            def priority(row: sqlite3.Row) -> tuple[bool, bool, float]:
                online = (
                    row["ended_at"] is None
                    and float(row["last_seen"]) >= threshold
                )
                return online, online and bool(row["using_app"]), float(row["last_seen"])

            ordered_group = sorted(group, key=priority, reverse=True)
            row = ordered_group[0]
            online = priority(row)[0]
            if not online:
                continue
            character_name = str(row["character_name"] or "").strip()
            if not character_name:
                recent_rows = sorted(
                    group, key=lambda value: float(value["last_seen"]), reverse=True
                )
                character_name = next(
                    (
                        str(value["character_name"] or "").strip()
                        for value in recent_rows
                        if str(value["character_name"] or "").strip()
                    ),
                    "",
                )
            sessions.append(
                {
                    "client_id": str(row["client_id"]),
                    "display_name": row["display_name"],
                    "character_name": character_name,
                    "app_version": row["app_version"],
                    "build_id": str(row["build_id"] or ""),
                    "game_pid": row["game_pid"],
                    "remote_ip": row["remote_ip"],
                    "started_at": row["started_at"],
                    "last_seen": row["last_seen"],
                    "online": online,
                    "using": online and bool(row["using_app"]),
                    "revoked": bool(row["revoked"]),
                    "card_key": str(row["card_key"] or ""),
                    "card_suffix": str(row["card_suffix"] or ""),
                    "expires_at": float(row["card_expires_at"] or 0),
                }
            )
        sessions.sort(
            key=lambda value: (
                bool(value["using"]),
                bool(value["online"]),
                float(value["last_seen"]),
            ),
            reverse=True,
        )
        cards = []
        for row in card_rows:
            expires_at = float(row["expires_at"] or 0)
            activated_at = float(row["activated_at"] or 0)
            permanent = bool(row["permanent"])
            card_tier = classify_card_tier(
                row["note"], row["duration_seconds"], permanent=permanent
            )
            if bool(row["revoked"]):
                state = "revoked"
            elif not permanent and expires_at and expires_at <= timestamp:
                state = "expired"
            elif activated_at:
                state = "active"
            else:
                state = "unused"
            cards.append(
                {
                    "card_id": str(row["card_hash"]),
                    "card_key": str(row["card_key"] or ""),
                    "card_suffix": str(row["card_suffix"]),
                    "duration_seconds": int(row["duration_seconds"]),
                    "created_at": float(row["created_at"]),
                    "activated_at": activated_at,
                    "expires_at": expires_at,
                    "remaining_seconds": (
                        None
                        if permanent
                        else (
                            max(0, int(expires_at - timestamp))
                            if expires_at
                            else None
                        )
                    ),
                    "bound_client_id": str(row["bound_client_id"] or ""),
                    "last_bound_at": float(row["last_bound_at"] or 0),
                    "rebind_cooldown_seconds": max(
                        0, int(row["rebind_cooldown_seconds"] or 0)
                    ),
                    "rebind_remaining_seconds": max(
                        0,
                        int(
                            float(row["last_bound_at"] or 0)
                            + max(0, int(row["rebind_cooldown_seconds"] or 0))
                            - timestamp
                        ),
                    ),
                    "last_used_at": float(row["last_used_at"] or 0),
                    "revoked": bool(row["revoked"]),
                    "sold": bool(row["sold"]),
                    "state": state,
                    "note": str(row["note"] or ""),
                    "remark": str(row["remark"] or ""),
                    "card_tier": card_tier,
                    "permanent": permanent,
                    "deletable": not is_partner_card(row["card_key"]),
                }
            )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "server_time": timestamp,
                "heartbeat_interval": HEARTBEAT_INTERVAL,
                "online_window": ONLINE_WINDOW,
                "device_rebind_enabled": bool(CARD_DEVICE_REBIND_ENABLED),
                "default_rebind_cooldown_seconds": CARD_REBIND_COOLDOWN_SECONDS,
                "logged_in": int(totals["logged_in"] or 0),
                "using_now": int(totals["using_now"] or 0),
                "seen_24h": int(totals["seen_24h"] or 0),
                "sessions": sessions,
                "cards": cards,
                "active_cards": sum(card["state"] == "active" for card in cards),
                "unused_cards": sum(card["state"] == "unused" for card in cards),
                "feedback_total": int(feedback_counts["total"] or 0),
            },
        )

    def _set_revoked(self) -> None:
        body = self._body()
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        client_id = clean_client_id(body.get("client_id"))
        if not client_id:
            self._json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_client_id"}
            )
            return
        revoked = 1 if bool(body.get("revoked", True)) else 0
        with write_database() as connection:
            changed = connection.execute(
                "UPDATE clients SET revoked=? WHERE client_id=?",
                (revoked, client_id),
            ).rowcount
            if revoked:
                connection.execute(
                    "UPDATE sessions SET ended_at=?, using_app=0 WHERE client_id=? AND ended_at IS NULL",
                    (now_epoch(), client_id),
                )
        self._json(HTTPStatus.OK, {"ok": True, "changed": bool(changed)})

    def _create_cards(self) -> None:
        body = self._body()
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        card_type = clean_text(body.get("card_type"), 32).lower()
        card_definition = CARD_TYPES.get(card_type)
        if card_definition is None:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "bad_card_type"},
            )
            return
        note, duration_seconds, permanent = card_definition
        remark = clean_text(body.get("remark"), 120)
        try:
            count = int(body.get("count", 1) or 1)
        except (TypeError, ValueError, OverflowError):
            count = 0
        if not 1 <= count <= MAX_CARD_BATCH:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "bad_count"},
            )
            return
        if permanent and count != 1:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error": "partner_single_only",
                    "message": "莫雪的小伙伴类型一次只能手动新增 1 张。",
                },
            )
            return
        custom_card_key = ""
        if permanent:
            custom_card_key = normalize_custom_card_key(body.get("custom_card_key"))
            if not custom_card_key:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "error": "bad_custom_card_key",
                        "message": "自定义卡号须为 6 至 64 位字母或数字。",
                    },
                )
                return
        timestamp = now_epoch()
        card_keys: list[str] = []
        with write_database() as connection:
            while len(card_keys) < count:
                card_key = custom_card_key if permanent else generate_card_key()
                card_hash = token_digest(card_key)
                try:
                    connection.execute(
                        """
                        INSERT INTO cards(
                            card_hash, card_key, card_suffix, duration_seconds,
                            created_at, permanent, note, remark
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            card_hash,
                            card_key,
                            card_key[-4:],
                            duration_seconds,
                            timestamp,
                            int(permanent),
                            note,
                            remark,
                        ),
                    )
                except sqlite3.IntegrityError:
                    if permanent:
                        self._json(
                            HTTPStatus.CONFLICT,
                            {
                                "ok": False,
                                "error": "card_exists",
                                "message": "该卡号已经存在，请换一个卡号。",
                            },
                        )
                        return
                    continue
                card_keys.append(card_key)
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "card_type": card_type,
                "card_type_label": note,
                "duration_seconds": duration_seconds,
                "permanent": permanent,
                "remark": remark,
                "cards": card_keys,
                "message": "卡号已新增并保存到卡号管理列表。",
            },
        )

    @staticmethod
    def _add_time_to_cards(
        connection: sqlite3.Connection,
        card_ids: list[str],
        duration_seconds: int,
        timestamp: float,
    ) -> int:
        changed = 0
        for card_id in card_ids:
            row = connection.execute(
                """
                SELECT activated_at, expires_at, permanent
                FROM cards WHERE card_hash=? AND deleted_at IS NULL
                """,
                (card_id,),
            ).fetchone()
            if row is None or bool(row["permanent"]):
                continue
            if float(row["activated_at"] or 0):
                expires_at = max(timestamp, float(row["expires_at"] or 0))
                changed += connection.execute(
                    """
                    UPDATE cards SET duration_seconds=duration_seconds+?,
                        expires_at=?
                    WHERE card_hash=? AND deleted_at IS NULL
                    """,
                    (duration_seconds, expires_at + duration_seconds, card_id),
                ).rowcount
            else:
                changed += connection.execute(
                    """
                    UPDATE cards SET duration_seconds=duration_seconds+?
                    WHERE card_hash=? AND deleted_at IS NULL
                    """,
                    (duration_seconds, card_id),
                ).rowcount
        return changed

    def _batch_add_card_time(self) -> None:
        body = self._body()
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        raw_card_ids = body.get("card_ids")
        if not isinstance(raw_card_ids, list):
            self._json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_card_ids"}
            )
            return
        card_ids: list[str] = []
        seen: set[str] = set()
        for value in raw_card_ids:
            card_id = clean_text(value, 80).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", card_id):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "bad_card_ids"},
                )
                return
            if card_id not in seen:
                seen.add(card_id)
                card_ids.append(card_id)
        if not 1 <= len(card_ids) <= MAX_CARD_BATCH:
            self._json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_count"}
            )
            return
        try:
            duration_seconds = int(body.get("duration_seconds", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            duration_seconds = 0
        if not MIN_CARD_DURATION_SECONDS <= duration_seconds <= MAX_CARD_DURATION_SECONDS:
            self._json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_duration"}
            )
            return
        timestamp = now_epoch()
        with write_database() as connection:
            changed = self._add_time_to_cards(
                connection, card_ids, duration_seconds, timestamp
            )
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "changed": changed,
                "requested": len(card_ids),
                "duration_seconds": duration_seconds,
            },
        )

    def _update_card(self) -> None:
        body = self._body()
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_json"})
            return
        card_id = clean_text(body.get("card_id"), 80).lower()
        action = clean_text(body.get("action"), 20).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", card_id):
            self._json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_card_id"}
            )
            return
        timestamp = now_epoch()
        with write_database() as connection:
            card = connection.execute(
                """SELECT card_key FROM cards
                   WHERE card_hash=? AND deleted_at IS NULL""",
                (card_id,),
            ).fetchone()
            if action == "delete":
                if card is not None and is_partner_card(card["card_key"]):
                    self._json(
                        HTTPStatus.CONFLICT,
                        {
                            "ok": False,
                            "error": "protected_card",
                            "message": "伙伴永久卡不能删除。",
                        },
                    )
                    return
                changed = connection.execute(
                    """UPDATE cards SET deleted_at=?
                       WHERE card_hash=? AND deleted_at IS NULL""",
                    (timestamp, card_id),
                ).rowcount
                if changed:
                    connection.execute(
                        """
                        UPDATE sessions SET ended_at=?, using_app=0
                        WHERE card_hash=? AND ended_at IS NULL
                        """,
                        (timestamp, card_id),
                    )
            elif action == "set_remark":
                remark = clean_text(body.get("remark"), 120)
                changed = connection.execute(
                    """UPDATE cards SET remark=?
                       WHERE card_hash=? AND deleted_at IS NULL""",
                    (remark, card_id),
                ).rowcount
            elif action == "revoke":
                changed = connection.execute(
                    """UPDATE cards SET revoked=1
                       WHERE card_hash=? AND deleted_at IS NULL""",
                    (card_id,),
                ).rowcount
                connection.execute(
                    """
                    UPDATE sessions SET ended_at=?, using_app=0
                    WHERE card_hash=? AND ended_at IS NULL
                    """,
                    (timestamp, card_id),
                )
            elif action == "restore":
                changed = connection.execute(
                    """UPDATE cards SET revoked=0
                       WHERE card_hash=? AND deleted_at IS NULL""",
                    (card_id,),
                ).rowcount
            elif action == "unbind":
                changed = connection.execute(
                    """
                    UPDATE cards SET bound_client_id='',
                        last_bound_at=CASE
                            WHEN bound_client_id<>'' THEN ? ELSE last_bound_at END
                    WHERE card_hash=? AND deleted_at IS NULL
                    """,
                    (timestamp, card_id),
                ).rowcount
                connection.execute(
                    """
                    UPDATE sessions SET ended_at=?, using_app=0
                    WHERE card_hash=? AND ended_at IS NULL
                    """,
                    (timestamp, card_id),
                )
            elif action == "add_time":
                try:
                    duration_seconds = int(body.get("duration_seconds", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    duration_seconds = 0
                if not MIN_CARD_DURATION_SECONDS <= duration_seconds <= MAX_CARD_DURATION_SECONDS:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "bad_duration"},
                    )
                    return
                changed = self._add_time_to_cards(
                    connection, [card_id], duration_seconds, timestamp
                )
            elif action == "set_sold":
                sold = body.get("sold")
                if not isinstance(sold, bool):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "bad_sold"},
                    )
                    return
                changed = connection.execute(
                    """UPDATE cards SET sold=?
                       WHERE card_hash=? AND deleted_at IS NULL""",
                    (int(sold), card_id),
                ).rowcount
            elif action == "set_rebind_cooldown":
                if not CARD_DEVICE_REBIND_ENABLED:
                    changed = connection.execute(
                        """
                        UPDATE cards SET rebind_cooldown_seconds=?
                        WHERE card_hash=? AND deleted_at IS NULL
                        """,
                        (CARD_REBIND_COOLDOWN_SECONDS, card_id),
                    ).rowcount
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "changed": bool(changed),
                            "cooldown_seconds": CARD_REBIND_COOLDOWN_SECONDS,
                            "device_rebind_enabled": False,
                        },
                    )
                    return
                try:
                    cooldown_hours = float(body.get("cooldown_hours", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    cooldown_hours = -1
                if not 0 <= cooldown_hours <= 720:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "bad_cooldown"},
                    )
                    return
                changed = connection.execute(
                    """
                    UPDATE cards SET rebind_cooldown_seconds=?
                    WHERE card_hash=? AND deleted_at IS NULL
                    """,
                    (int(cooldown_hours * 3600), card_id),
                ).rowcount
            else:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "bad_action"},
                )
                return
        self._json(HTTPStatus.OK, {"ok": True, "changed": bool(changed)})


LEGACY_ADMIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>叨叨诡秘 Dps-Logs 在线监控</title>
<style>
:root{color-scheme:dark;--bg:#0b0e12;--surface:#131820;--panel:#191f28;--line:#2b3541;--text:#f3f5f7;--muted:#929eac;--green:#61d8ae;--amber:#e9b96e;--red:#e66a73}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px "Microsoft YaHei UI",system-ui,sans-serif;letter-spacing:0}
header{height:54px;background:var(--surface);border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 24px}header strong{font-size:16px}header span{margin-left:auto;color:var(--muted);font-size:12px}
main{max-width:1180px;margin:0 auto;padding:22px}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:18px}.metric{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:14px 16px}.metric label{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}.metric b{font:700 26px "Segoe UI",sans-serif}.metric.using b{color:var(--green)}
.table-wrap{border:1px solid var(--line);background:var(--surface);overflow:auto}table{width:100%;border-collapse:collapse;min-width:900px}th,td{height:44px;padding:0 12px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{height:36px;color:var(--muted);font-size:12px;background:var(--panel)}tbody tr:hover{background:#181e26}.status{display:inline-flex;align-items:center;gap:6px}.dot{width:7px;height:7px;border-radius:50%;background:var(--muted)}.online .dot{background:var(--amber)}.using .dot{background:var(--green)}.revoked .dot{background:var(--red)}button{border:1px solid var(--line);border-radius:4px;background:var(--panel);color:var(--text);padding:5px 9px;cursor:pointer}button.danger{color:#ff9aa1}button:hover{border-color:#516070}.empty{padding:44px;text-align:center;color:var(--muted)}
@media(max-width:700px){main{padding:12px}.metrics{grid-template-columns:1fr}header{padding:0 14px}}
</style>
</head>
<body>
<header><strong>叨叨诡秘 Dps-Logs 在线监控</strong><span id="refresh">正在同步</span></header>
<main>
<section class="metrics"><div class="metric"><label>当前登录</label><b id="logged">0</b></div><div class="metric using"><label>正在使用</label><b id="using">0</b></div><div class="metric"><label>近 24 小时设备</label><b id="seen">0</b></div></section>
<section class="table-wrap"><table><thead><tr><th>客户端</th><th>角色</th><th>版本</th><th>状态</th><th>最近心跳</th><th>IP</th><th>管理</th></tr></thead><tbody id="rows"></tbody></table><div id="empty" class="empty" hidden>暂无在线记录</div></section>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ago=(now,value)=>{const s=Math.max(0,Math.round(now-value));if(s<60)return s+' 秒前';if(s<3600)return Math.floor(s/60)+' 分钟前';return Math.floor(s/3600)+' 小时前'};
async function revoke(id,value){if(value&&!confirm('停用该客户端？客户端会在下一次心跳时安全退出。'))return;await fetch('/api/v1/dps/admin/revoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:id,revoked:value})});load()}
async function load(){try{const r=await fetch('/api/v1/dps/admin/status',{cache:'no-store'});if(!r.ok)throw new Error(r.status);const d=await r.json();logged.textContent=d.logged_in;using.textContent=d.using_now;seen.textContent=d.seen_24h;refresh.textContent='每 30 秒自动刷新 · '+new Date(d.server_time*1000).toLocaleTimeString();rows.innerHTML=d.sessions.map(x=>{let state=x.revoked?'已停用':x.using?'使用中':x.online?'已登录':'离线';let cls=x.revoked?'revoked':x.using?'using':x.online?'online':'';return `<tr><td>${esc(x.display_name)}<br><small>${esc(x.client_id.slice(0,12))}</small></td><td>${esc(x.character_name||'--')}</td><td>${esc(x.app_version||'--')}</td><td><span class="status ${cls}"><i class="dot"></i>${state}</span></td><td>${ago(d.server_time,x.last_seen)}</td><td>${esc(x.remote_ip)}</td><td><button class="${x.revoked?'':'danger'}" onclick="revoke('${esc(x.client_id)}',${!x.revoked})">${x.revoked?'恢复':'停用'}</button></td></tr>`}).join('');empty.hidden=d.sessions.length>0}catch(e){refresh.textContent='连接失败'}}
load();setInterval(load,30000);
</script>
</body></html>"""


ADMIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>叨叨诡秘 Dps-Logs 管理后台</title>
<style>
:root{color-scheme:dark;--bg:#0b0e12;--surface:#131820;--panel:#191f28;--line:#2b3541;--text:#f3f5f7;--muted:#929eac;--green:#61d8ae;--amber:#e9b96e;--red:#e66a73;--blue:#73a7df}
*{box-sizing:border-box;scrollbar-width:auto;scrollbar-color:#647384 #11161d}
*::-webkit-scrollbar{width:12px;height:12px}*::-webkit-scrollbar-track{background:#11161d;border:1px solid #202832}*::-webkit-scrollbar-thumb{background:#647384;border:2px solid #11161d;border-radius:7px}*::-webkit-scrollbar-thumb:hover{background:#7b8b9d}
body{margin:0;min-width:760px;background:var(--bg);color:var(--text);font:15px "Microsoft YaHei UI",system-ui,sans-serif;letter-spacing:0}
header{height:62px;background:var(--surface);border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 30px}
header strong{font-size:18px}header span{margin-left:auto;color:var(--muted);font-size:13px}
nav{height:46px;background:var(--surface);border-bottom:1px solid var(--line);display:flex;padding:0 30px;gap:26px}
nav button{height:46px;padding:0 3px;border:0;border-bottom:2px solid transparent;border-radius:0;background:transparent;color:var(--muted)}
nav button.active{color:var(--text);border-bottom-color:var(--green)}
main{width:min(1680px,calc(100% - 40px));margin:0 auto;padding:24px 0 30px}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:20px}
.metric{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:16px 19px}
.metric label{display:block;color:var(--muted);font-size:13px;margin-bottom:6px}.metric b{font:700 28px "Segoe UI",sans-serif}
.metric.using b{color:var(--green)}.metric.cards b{color:var(--blue)}
.panel-head{min-height:50px;display:flex;align-items:center;gap:12px}.panel-head h2{font-size:17px;margin:0}
.panel-head form,.panel-tools{display:flex;align-items:center;gap:8px;margin-left:auto}
label.field{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:13px}
input,select{height:36px;border:1px solid var(--line);border-radius:4px;background:var(--panel);color:var(--text);padding:0 10px;outline:0;font:14px "Microsoft YaHei UI",sans-serif}
input:focus,select:focus{border-color:#607286}input[type=number]{width:82px}input.search{width:280px}input.create-key{width:210px}input.create-remark{width:190px}
button{border:1px solid var(--line);border-radius:4px;background:var(--panel);color:var(--text);padding:7px 12px;cursor:pointer;font:14px "Microsoft YaHei UI",sans-serif}
button:hover{border-color:#596878}button.primary{background:#27644f;border-color:#397a65}button.danger{color:#ff9aa1}button.link{padding:4px 7px}
.table-wrap{border:1px solid var(--line);background:var(--surface);overflow-x:scroll;overflow-y:auto;scrollbar-gutter:stable;max-height:calc(100vh - 236px)}
table{width:100%;border-collapse:collapse;min-width:1200px}.cards-table{min-width:1980px}th,td{height:50px;padding:0 13px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
.feedbacks-table{min-width:1540px}.feedback-preview{display:block;max-width:440px;overflow:hidden;text-overflow:ellipsis}.feedback-id{font:12px "Consolas",monospace;color:var(--muted)}
th{height:40px;color:var(--muted);font-size:13px;background:var(--panel);position:sticky;top:0;z-index:1}tbody tr:hover{background:#181e26}small{color:var(--muted)}
.horizontal-scroll{height:17px;margin-bottom:5px;border:1px solid var(--line);background:#11161d;overflow-x:scroll;overflow-y:hidden;scrollbar-gutter:stable}.horizontal-scroll>div{height:1px}
.selection-tools{padding:7px 10px;border:1px solid var(--line);border-radius:5px;background:var(--surface)}
.check-cell{width:42px;text-align:center}.check-cell input{width:17px;height:17px;vertical-align:middle;accent-color:#61d8ae}
.sold{color:var(--blue)}button:disabled{opacity:.45;cursor:not-allowed}
.status{display:inline-flex;align-items:center;gap:6px}.dot{width:7px;height:7px;border-radius:50%;background:var(--muted)}
.online .dot,.unused .dot{background:var(--amber)}.using .dot,.active .dot{background:var(--green)}.revoked .dot,.expired .dot{background:var(--red)}
.empty{padding:44px;text-align:center;color:var(--muted)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.68);display:grid;place-items:center;padding:18px}.modal[hidden]{display:none}
.modal-body{width:min(590px,100%);background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:18px}
.modal-head{display:flex;align-items:center;margin-bottom:12px}.modal-head h2{font-size:16px;margin:0}.modal-head button{margin-left:auto}
textarea{width:100%;height:260px;resize:none;background:#0d1116;color:var(--text);border:1px solid var(--line);border-radius:4px;padding:10px;font:14px "Consolas",monospace;line-height:1.7}
.feedback-modal-body{width:min(820px,100%)}.feedback-meta{color:var(--muted);font-size:13px;line-height:1.8;margin-bottom:12px}.feedback-section{margin-top:12px}.feedback-section h3{font-size:14px;margin:0 0 7px;color:var(--muted)}.feedback-content{min-height:84px;max-height:190px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:0;padding:11px;background:#0d1116;border:1px solid var(--line);border-radius:4px;font:14px "Microsoft YaHei UI",sans-serif;line-height:1.7}.feedback-diagnostics{height:230px}
.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}[hidden]{display:none!important}
@media(max-width:900px){header,nav{padding-left:16px;padding-right:16px}main{width:calc(100% - 24px);padding-top:14px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.panel-head{align-items:flex-start;flex-direction:column;padding-bottom:10px}.panel-head form,.panel-tools{margin-left:0;flex-wrap:wrap}}
</style>
</head>
<body>
<header><strong>叨叨诡秘 Dps-Logs 管理后台</strong><span id="refresh">正在同步</span></header>
<nav><button id="onlineTab" class="active" type="button">在线用户</button><button id="cardsTab" type="button">卡号管理</button><button id="feedbackTab" type="button">反馈</button></nav>
<main>
<section class="metrics"><div class="metric"><label>当前登录</label><b id="logged">0</b></div><div class="metric using"><label>正在使用</label><b id="using">0</b></div><div class="metric cards"><label>有效卡号</label><b id="validCards">0</b></div><div class="metric"><label>近 24 小时设备</label><b id="seen">0</b></div></section>
<section id="onlinePanel"><div class="panel-head"><h2>在线用户</h2><div class="panel-tools"><input id="onlineCardSearch" class="search" autocomplete="off" spellcheck="false" placeholder="输入卡号查询"><span id="onlineCount">0 人</span></div></div><div class="table-wrap"><table><thead><tr><th>卡号</th><th>角色</th><th>版本</th><th>状态</th><th>最近心跳</th><th>IP</th><th>管理</th></tr></thead><tbody id="userRows"></tbody></table><div id="userEmpty" class="empty" hidden>暂无在线记录</div></div></section>
<section id="cardsPanel" hidden>
<div class="panel-head"><h2>卡号管理</h2><form id="createForm"><label class="field">类型<select id="createCardType" name="card_type"><option value="test_2h">测试2小时卡</option><option value="daily">天卡</option><option value="weekly">周卡</option><option value="monthly">月卡</option><option value="partner">莫雪的小伙伴（永久）</option></select></label><label id="customCardKeyField" class="field" hidden>卡号<input id="customCardKey" class="create-key" name="custom_card_key" maxlength="64" autocomplete="off" placeholder="6-64 位字母或数字"></label><label class="field">备注<input id="createCardRemark" class="create-remark" name="remark" maxlength="120" placeholder="选填"></label><label class="field">数量<input id="createCardCount" name="count" type="number" min="1" max="500" value="1" required></label><button id="createCardsButton" class="primary" type="submit">批量生成</button></form></div>
<div class="panel-head"><div class="panel-tools"><input id="cardSearch" class="search" placeholder="搜索卡号、类型、备注或设备"><select id="cardStateFilter"><option value="">全部状态</option><option value="unused">未激活</option><option value="active">使用中</option><option value="expired">已到期</option><option value="revoked">已停用</option></select><select id="cardSoldFilter"><option value="">全部销售状态</option><option value="unsold">未售</option><option value="sold">已售</option></select><span id="cardCount"></span><button id="prevCardPage" class="link" type="button">上一页</button><span id="cardPageInfo">1 / 1</span><button id="nextCardPage" class="link" type="button">下一页</button></div><div class="panel-tools selection-tools"><span id="selectedCount">已选 0</span><label class="field">批量加时<input id="batchDuration" type="number" min="1" max="87600" value="1"><select id="batchDurationUnit"><option value="days">天</option><option value="hours">小时</option></select></label><button id="batchAddTime" class="primary" type="button" disabled>给已选卡号加时</button></div></div>
<div id="cardScrollTop" class="horizontal-scroll" title="拖动横向查看全部卡号字段"><div id="cardScrollSpacer"></div></div>
<div id="cardTableWrap" class="table-wrap"><table id="cardTable" class="cards-table"><thead><tr><th class="check-cell"><input id="selectVisibleCards" type="checkbox" title="选择当前筛选结果"></th><th>完整卡号</th><th>类型</th><th>备注</th><th>销售</th><th>状态</th><th>总时长</th><th>剩余时间</th><th>绑定设备</th><th>改绑限制</th><th>最近使用</th><th>管理</th></tr></thead><tbody id="cardRows"></tbody></table><div id="cardEmpty" class="empty" hidden>暂无卡号</div></div>
</section>
<section id="feedbackPanel" hidden>
<div class="panel-head"><h2>反馈</h2><div class="panel-tools"><input id="feedbackSearch" class="search" placeholder="搜索编号、卡号、角色或问题内容"><span id="feedbackCount">0 条</span></div></div>
<div class="table-wrap"><table class="feedbacks-table"><thead><tr><th>反馈编号</th><th>提交时间</th><th>问题类型</th><th>卡号</th><th>角色</th><th>版本</th><th>问题描述</th><th>运行摘要</th><th>管理</th></tr></thead><tbody id="feedbackRows"></tbody></table><div id="feedbackEmpty" class="empty" hidden>暂无问题反馈</div></div>
</section>
</main>
<div id="resultModal" class="modal" hidden><div class="modal-body"><div class="modal-head"><h2>新生成的卡号</h2><button id="closeModal" type="button">关闭</button></div><textarea id="generatedCards" readonly></textarea><div class="modal-actions"><button id="copyCards" class="primary" type="button">复制全部</button></div></div></div>
<div id="feedbackModal" class="modal" hidden><div class="modal-body feedback-modal-body"><div class="modal-head"><h2 id="feedbackModalTitle">反馈详情</h2><button id="closeFeedbackModal" type="button">关闭</button></div><div id="feedbackMeta" class="feedback-meta"></div><div class="feedback-section"><h3>问题描述</h3><pre id="feedbackFullContent" class="feedback-content"></pre></div><div class="feedback-section"><h3>运行摘要</h3><textarea id="feedbackDiagnostics" class="feedback-diagnostics" readonly></textarea></div><div class="modal-actions"><button id="closeFeedbackModalBottom" type="button">关闭</button></div></div></div>
<script>
const byId=function(id){return document.getElementById(id)};
const esc=function(value){return String(value==null?'':value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})};
const ago=function(now,value){if(!value)return '--';const s=Math.max(0,Math.round(now-value));if(s<60)return s+' 秒前';if(s<3600)return Math.floor(s/60)+' 分钟前';if(s<86400)return Math.floor(s/3600)+' 小时前';return Math.floor(s/86400)+' 天前'};
const duration=function(value){let s=Math.max(0,Math.floor(value||0));if(s%86400===0)return (s/86400)+' 天';if(s%3600===0)return (s/3600)+' 小时';return Math.floor(s/3600)+' 小时'};
const remaining=function(value){if(value==null)return '未激活';let s=Math.max(0,Math.floor(value));if(!s)return '已到期';const d=Math.floor(s/86400);s%=86400;const h=Math.floor(s/3600);const m=Math.floor((s%3600)/60);return (d?d+' 天 ':'')+(h?h+' 小时 ':'')+m+' 分钟'};
const stateText=function(value){return {unused:'未激活',active:'使用中',expired:'已到期',revoked:'已停用'}[value]||'--'};
let allCards=[];
let allFeedbacks=[];
let allSessions=[];
let visibleCardIds=[];
let serverNow=0;
let deviceRebindEnabled=false;
const CARD_PAGE_SIZE=100;
let cardPage=1;
const selectedCardIds=new Set();

function switchTab(name){
  const online=name==='online';
  const cards=name==='cards';
  const feedback=name==='feedback';
  byId('onlinePanel').hidden=!online;
  byId('cardsPanel').hidden=!cards;
  byId('feedbackPanel').hidden=!feedback;
  byId('onlineTab').classList.toggle('active',online);
  byId('cardsTab').classList.toggle('active',cards);
  byId('feedbackTab').classList.toggle('active',feedback);
  if(cards){renderCards();requestAnimationFrame(syncCardScrollWidth)}
  if(feedback)loadFeedback();
}
byId('onlineTab').onclick=function(){switchTab('online')};
byId('cardsTab').onclick=function(){switchTab('cards')};
byId('feedbackTab').onclick=function(){switchTab('feedback')};

async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok||!d.ok)throw new Error(d.message||d.error||r.status);
  return d;
}
async function setClient(id,revoked){
  if(revoked&&!confirm('停用该客户端？客户端会在下一次心跳时退回登录页。'))return;
  await post('/api/v1/dps/admin/revoke',{client_id:id,revoked:revoked});
  await load();
}
async function updateCard(id,action,extra){
  const body=Object.assign({card_id:id,action:action},extra||{});
  await post('/api/v1/dps/admin/cards/update',body);
  await load();
}
async function cardAction(button){
  const id=button.dataset.card;
  const action=button.dataset.action;
  if(action==='revoke'&&!confirm('停用该卡号？正在使用的客户端会退回登录页。'))return;
  if(action==='unbind'&&!confirm('解绑该设备？解绑后仍需等待 12 小时才能绑定新设备。'))return;
  if(action==='delete'){
    if(!confirm('确定删除卡号 '+(button.dataset.key||'')+'？\\n该卡号会从列表移除，在线会话立即结束，历史记录仍会保留。'))return;
    await updateCard(id,action);
    return;
  }
  if(action==='set_remark'){
    const value=prompt('填写该卡号的备注，留空可清除备注。',button.dataset.remark||'');
    if(value===null)return;
    await updateCard(id,action,{remark:value});
    return;
  }
  if(action==='add_time'){
    const hours=Number(prompt('增加多少小时？24 小时等于 1 天。','24'));
    if(!Number.isFinite(hours)||hours<1)return;
    await updateCard(id,action,{duration_seconds:Math.round(hours*3600)});
    return;
  }
  if(action==='set_rebind_cooldown'){
    const hours=Number(prompt('设置该卡号的改绑冷却小时数。默认策略为 12 小时。',button.dataset.hours||'12'));
    if(!Number.isFinite(hours)||hours<0)return;
    await updateCard(id,action,{cooldown_hours:hours});
    return;
  }
  if(action==='set_sold'){
    await updateCard(id,action,{sold:button.dataset.sold==='1'});
    return;
  }
  await updateCard(id,action);
}

function updateSelectionUi(){
  byId('selectedCount').textContent='已选 '+selectedCardIds.size;
  byId('batchAddTime').disabled=selectedCardIds.size===0;
  const selectAll=byId('selectVisibleCards');
  const selectedVisible=visibleCardIds.filter(function(id){return selectedCardIds.has(id)}).length;
  selectAll.checked=visibleCardIds.length>0&&selectedVisible===visibleCardIds.length;
  selectAll.indeterminate=selectedVisible>0&&selectedVisible<visibleCardIds.length;
}
function bindActions(){
  document.querySelectorAll('[data-client]').forEach(function(button){
    button.onclick=function(){setClient(button.dataset.client,button.dataset.action==='revoke').catch(function(e){alert('操作失败：'+e.message)})};
  });
  document.querySelectorAll('[data-card]').forEach(function(button){
    button.onclick=function(){cardAction(button).catch(function(e){alert('操作失败：'+e.message)})};
  });
  document.querySelectorAll('[data-copy]').forEach(function(button){
    button.onclick=function(){navigator.clipboard.writeText(button.dataset.copy).catch(function(){})};
  });
  document.querySelectorAll('[data-select-card]').forEach(function(box){
    box.onchange=function(){if(box.checked)selectedCardIds.add(box.dataset.selectCard);else selectedCardIds.delete(box.dataset.selectCard);updateSelectionUi()};
  });
}
function renderOnline(){
  const term=byId('onlineCardSearch').value.replace(/\\s+/g,'').toLowerCase();
  const rows=allSessions.filter(function(x){
    const card=String(x.card_key||'').toLowerCase();
    const suffix=String(x.card_suffix||'').toLowerCase();
    return !term||card.includes(term)||suffix.includes(term);
  });
  byId('onlineCount').textContent=rows.length+' / '+allSessions.length+' 人';
  byId('userRows').innerHTML=rows.map(function(x){
    const state=x.revoked?'已停用':x.using?'使用中':x.online?'已登录':'离线';
    const cls=x.revoked?'revoked':x.using?'using':x.online?'online':'';
    const card=x.card_key||(x.card_suffix?'****'+x.card_suffix:'旧会话');
    return '<tr><td>'+esc(card)+'</td><td>'+esc(x.character_name||'--')+'</td><td>'+esc(x.app_version||'--')+'</td><td><span class="status '+cls+'"><i class="dot"></i>'+state+'</span></td><td>'+ago(serverNow,x.last_seen)+'</td><td>'+esc(x.remote_ip)+'</td><td><button class="link '+(x.revoked?'':'danger')+'" data-client="'+esc(x.client_id)+'" data-action="'+(x.revoked?'restore':'revoke')+'">'+(x.revoked?'恢复':'停用')+'</button></td></tr>';
  }).join('');
  byId('userEmpty').textContent=term?'未找到匹配的在线卡号':'暂无在线记录';
  byId('userEmpty').hidden=rows.length>0;
  bindActions();
}
function renderCards(){
  const term=byId('cardSearch').value.trim().toLowerCase();
  const state=byId('cardStateFilter').value;
  const sold=byId('cardSoldFilter').value;
  const cards=allCards.filter(function(x){
    const matchesSold=!sold||(sold==='sold'&&x.sold)||(sold==='unsold'&&!x.sold);
    return (!state||x.state===state)&&matchesSold&&(!term||(x.card_key+' '+x.note+' '+(x.remark||'')+' '+x.bound_client_id).toLowerCase().includes(term));
  });
  const pageCount=Math.max(1,Math.ceil(cards.length/CARD_PAGE_SIZE));
  cardPage=Math.min(Math.max(1,cardPage),pageCount);
  const pageCards=cards.slice((cardPage-1)*CARD_PAGE_SIZE,cardPage*CARD_PAGE_SIZE);
  visibleCardIds=pageCards.filter(function(x){return !x.permanent}).map(function(x){return x.card_id});
  byId('cardCount').textContent=cards.length+' / '+allCards.length;
  byId('cardPageInfo').textContent=cardPage+' / '+pageCount;
  byId('prevCardPage').disabled=cardPage<=1;
  byId('nextCardPage').disabled=cardPage>=pageCount;
  byId('cardRows').innerHTML=pageCards.map(function(x){
    const rebindAction=deviceRebindEnabled
      ? '<button class="link" data-card="'+x.card_id+'" data-action="set_rebind_cooldown" data-hours="'+(x.rebind_cooldown_seconds/3600)+'">改绑设置</button> '
      : '<span title="设备改绑功能已暂停">改绑已暂停</span> ';
    const actions='<button class="link" data-copy="'+esc(x.card_key)+'">复制</button> '
      +'<button class="link" data-card="'+x.card_id+'" data-action="set_remark" data-remark="'+esc(x.remark||'')+'">备注</button> '
      +(x.permanent?'':'<button class="link" data-card="'+x.card_id+'" data-action="add_time">加时</button> ')
      +'<button class="link" data-card="'+x.card_id+'" data-action="set_sold" data-sold="'+(x.sold?'0':'1')+'">'+(x.sold?'改为未售':'标记已售')+'</button> '
      +rebindAction
      +(x.bound_client_id?'<button class="link" data-card="'+x.card_id+'" data-action="unbind">解绑</button> ':'')
      +'<button class="link '+(x.revoked?'':'danger')+'" data-card="'+x.card_id+'" data-action="'+(x.revoked?'restore':'revoke')+'">'+(x.revoked?'恢复':'停用')+'</button> '
      +(x.deletable?'<button class="link danger" data-card="'+x.card_id+'" data-action="delete" data-key="'+esc(x.card_key)+'">删除</button>':'');
    const rebind=duration(x.rebind_cooldown_seconds)+(x.rebind_remaining_seconds?' · 剩 '+remaining(x.rebind_remaining_seconds):'');
    const selection=x.permanent?'--':'<input type="checkbox" data-select-card="'+x.card_id+'" '+(selectedCardIds.has(x.card_id)?'checked':'')+'>';
    return '<tr><td class="check-cell">'+selection+'</td>'
      +'<td>'+esc(x.card_key||('****'+x.card_suffix))+'</td>'
      +'<td>'+esc(x.note||'--')+'</td>'
      +'<td>'+esc(x.remark||'--')+'</td>'
      +'<td class="'+(x.sold?'sold':'')+'">'+(x.sold?'已售':'未售')+'</td>'
      +'<td><span class="status '+x.state+'"><i class="dot"></i>'+stateText(x.state)+'</span></td>'
      +'<td>'+(x.permanent?'永久':duration(x.duration_seconds))+'</td><td>'+(x.permanent?'无限制':remaining(x.remaining_seconds))+'</td>'
      +'<td>'+(x.bound_client_id?esc(x.bound_client_id.slice(0,12)):'--')+'</td>'
      +'<td>'+rebind+'</td><td>'+ago(serverNow,x.last_used_at)+'</td><td>'+actions+'</td></tr>';
  }).join('');
  byId('cardEmpty').hidden=cards.length>0;
  bindActions();
  updateSelectionUi();
  requestAnimationFrame(syncCardScrollWidth);
}

function renderFeedbacks(){
  const term=byId('feedbackSearch').value.trim().toLowerCase();
  const rows=allFeedbacks.filter(function(x){
    const haystack=(x.feedback_id+' '+x.card_key+' '+x.character_name+' '+x.category_label+' '+x.content_preview).toLowerCase();
    return !term||haystack.includes(term);
  });
  byId('feedbackCount').textContent=rows.length+' / '+allFeedbacks.length+' 条';
  byId('feedbackRows').innerHTML=rows.map(function(x){
    const created=new Date(x.created_at*1000).toLocaleString();
    return '<tr><td><span class="feedback-id">'+esc(x.feedback_id)+'</span></td>'
      +'<td>'+esc(created)+'</td><td>'+esc(x.category_label)+'</td><td>'+esc(x.card_key||'--')+'</td>'
      +'<td>'+esc(x.character_name||'--')+'</td><td>'+esc(x.app_version||'--')+'</td>'
      +'<td><span class="feedback-preview" title="'+esc(x.content_preview)+'">'+esc(x.content_preview)+'</span></td>'
      +'<td>'+(x.has_diagnostics?'已附带':'未附带')+'</td><td><button class="link" data-feedback="'+esc(x.feedback_id)+'">查看</button></td></tr>';
  }).join('');
  byId('feedbackEmpty').hidden=rows.length>0;
  document.querySelectorAll('[data-feedback]').forEach(function(button){
    button.onclick=function(){openFeedback(button.dataset.feedback).catch(function(e){alert('读取反馈失败：'+e.message)})};
  });
}

async function loadFeedback(){
  try{
    const r=await fetch('/api/v1/dps/admin/feedback',{cache:'no-store'});
    if(!r.ok)throw new Error(r.status);
    const d=await r.json();
    allFeedbacks=d.feedbacks||[];
    renderFeedbacks();
  }catch(e){byId('feedbackCount').textContent='读取失败'}
}

async function openFeedback(id){
  const d=await post('/api/v1/dps/admin/feedback/detail',{feedback_id:id});
  const x=d.feedback;
  const summary=allFeedbacks.find(function(item){return item.feedback_id===x.feedback_id})||{};
  byId('feedbackModalTitle').textContent='反馈详情 · '+x.feedback_id;
  byId('feedbackMeta').textContent='提交时间：'+new Date(x.created_at*1000).toLocaleString()+'　类型：'+(summary.category_label||x.category)+'　卡号：'+(x.card_key||'--')+'　角色：'+(x.character_name||'--')+'　版本：'+(x.app_version||'--')+'　IP：'+(x.remote_ip||'--');
  byId('feedbackFullContent').textContent=x.content||'';
  const diagnostics=x.diagnostics||{};
  byId('feedbackDiagnostics').value=Object.keys(diagnostics).length?JSON.stringify(diagnostics,null,2):'用户未附带运行摘要';
  byId('feedbackModal').hidden=false;
}

function syncCardScrollWidth(){
  const table=byId('cardTable');
  const wrap=byId('cardTableWrap');
  const top=byId('cardScrollTop');
  byId('cardScrollSpacer').style.width=Math.max(table.scrollWidth,wrap.clientWidth+1)+'px';
  top.scrollLeft=wrap.scrollLeft;
}
let syncingScroll=false;
byId('cardScrollTop').onscroll=function(){if(syncingScroll)return;syncingScroll=true;byId('cardTableWrap').scrollLeft=byId('cardScrollTop').scrollLeft;requestAnimationFrame(function(){syncingScroll=false})};
byId('cardTableWrap').onscroll=function(){if(syncingScroll)return;syncingScroll=true;byId('cardScrollTop').scrollLeft=byId('cardTableWrap').scrollLeft;requestAnimationFrame(function(){syncingScroll=false})};
window.addEventListener('resize',syncCardScrollWidth);

async function load(){
  try{
    const r=await fetch('/api/v1/dps/admin/status',{cache:'no-store'});
    if(!r.ok)throw new Error(r.status);
    const d=await r.json();
    serverNow=d.server_time;
    deviceRebindEnabled=!!d.device_rebind_enabled;
    allCards=d.cards||[];
    allSessions=d.sessions||[];
    const liveIds=new Set(allCards.map(function(x){return x.card_id}));
    Array.from(selectedCardIds).forEach(function(id){if(!liveIds.has(id))selectedCardIds.delete(id)});
    byId('logged').textContent=d.logged_in;
    byId('using').textContent=d.using_now;
    byId('seen').textContent=d.seen_24h;
    byId('validCards').textContent=(d.active_cards||0)+(d.unused_cards||0);
    byId('refresh').textContent='每 30 秒自动刷新 · '+new Date(d.server_time*1000).toLocaleTimeString();
    renderOnline();
    if(!byId('cardsPanel').hidden)renderCards();
  }catch(e){byId('refresh').textContent='连接失败'}
}

function resetCardPageAndRender(){cardPage=1;renderCards()}
byId('onlineCardSearch').oninput=renderOnline;
byId('cardSearch').oninput=resetCardPageAndRender;
byId('cardStateFilter').onchange=resetCardPageAndRender;
byId('cardSoldFilter').onchange=resetCardPageAndRender;
byId('prevCardPage').onclick=function(){if(cardPage>1){cardPage-=1;renderCards()}};
byId('nextCardPage').onclick=function(){cardPage+=1;renderCards()};
byId('selectVisibleCards').onchange=function(){
  visibleCardIds.forEach(function(id){if(byId('selectVisibleCards').checked)selectedCardIds.add(id);else selectedCardIds.delete(id)});
  renderCards();
};
byId('batchAddTime').onclick=async function(){
  const value=Number(byId('batchDuration').value);
  const seconds=Math.round(value*(byId('batchDurationUnit').value==='hours'?3600:86400));
  if(!Number.isFinite(value)||value<1)return;
  if(!confirm('给已选的 '+selectedCardIds.size+' 个卡号统一增加 '+duration(seconds)+'？'))return;
  try{await post('/api/v1/dps/admin/cards/batch-add-time',{card_ids:Array.from(selectedCardIds),duration_seconds:seconds});await load()}catch(e){alert('批量加时失败：'+e.message)}
};
function syncCreateCardForm(){
  const partner=byId('createCardType').value==='partner';
  const count=byId('createCardCount');
  const keyField=byId('customCardKeyField');
  const keyInput=byId('customCardKey');
  if(partner)count.value='1';
  count.disabled=partner;
  keyField.hidden=!partner;
  keyInput.disabled=!partner;
  keyInput.required=partner;
  byId('createCardsButton').textContent=partner?'单个新增':'批量生成';
}
byId('createCardType').onchange=syncCreateCardForm;
byId('createForm').onsubmit=async function(event){
  event.preventDefault();
  const form=new FormData(event.currentTarget);
  const cardType=String(form.get('card_type'));
  const count=cardType==='partner'?1:Number(form.get('count'));
  try{
    const d=await post('/api/v1/dps/admin/cards/create',{card_type:cardType,count:count,custom_card_key:String(form.get('custom_card_key')||''),remark:String(form.get('remark')||'')});
    byId('generatedCards').value=d.cards.join('\\n');
    byId('resultModal').hidden=false;
    await load();
  }catch(e){alert('新增失败：'+e.message)}
};
syncCreateCardForm();
byId('closeModal').onclick=function(){byId('resultModal').hidden=true};
byId('resultModal').onclick=function(event){if(event.target===byId('resultModal'))byId('resultModal').hidden=true};
byId('copyCards').onclick=async function(){
  const value=byId('generatedCards').value;
  try{await navigator.clipboard.writeText(value);byId('copyCards').textContent='已复制';setTimeout(function(){byId('copyCards').textContent='复制全部'},1200)}catch(e){byId('generatedCards').select();document.execCommand('copy')}
};
byId('feedbackSearch').oninput=renderFeedbacks;
function closeFeedbackModal(){byId('feedbackModal').hidden=true}
byId('closeFeedbackModal').onclick=closeFeedbackModal;
byId('closeFeedbackModalBottom').onclick=closeFeedbackModal;
byId('feedbackModal').onclick=function(event){if(event.target===byId('feedbackModal'))closeFeedbackModal()};
load();
if(location.hash==='#cards')switchTab('cards');
if(location.hash==='#feedback')switchTab('feedback');
setInterval(function(){load();if(!byId('feedbackPanel').hidden)loadFeedback()},30000);
</script>
</body></html>"""


def main() -> None:
    if not ADMIN_PASSWORD:
        raise SystemExit("GMZZ_MONITOR_ADMIN_PASSWORD is required")
    initialize_database()
    server = MonitorServer((HOST, PORT), MonitorHandler)
    print(f"GMZZ DPS monitor listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
