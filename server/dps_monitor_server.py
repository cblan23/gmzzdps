#!/usr/bin/env python3
"""Small HTTPS-proxied session monitor for the DPS client."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit


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
CARD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CARD_PATTERN = re.compile(r"^GMZZ[A-HJ-NP-Z2-9]{26}$")
CUSTOM_CARD_PATTERN = re.compile(r"^[A-Za-z0-9]{6,64}$")
PARTNER_CARD_KEY = os.environ.get("GMZZ_MONITOR_PARTNER_CARD", "").strip()
MAX_CARD_DURATION_DAYS = 3650
MIN_CARD_DURATION_SECONDS = 60 * 60
MAX_CARD_DURATION_SECONDS = MAX_CARD_DURATION_DAYS * 86400
MAX_CARD_BATCH = 500
CARD_REBIND_COOLDOWN_SECONDS = 12 * 60 * 60
MAX_FEEDBACK_CONTENT = 2000
MAX_FEEDBACK_DIAGNOSTICS_BYTES = 384 * 1024
UPDATE_METADATA_PATH = Path(
    os.environ.get(
        "GMZZ_MONITOR_UPDATE_METADATA",
        "/var/lib/gmzz-dps-monitor/update.json",
    )
)
MAX_UPDATE_BYTES = 256 * 1024 * 1024
FEEDBACK_CATEGORIES = {
    "dps": "DPS 统计",
    "boss": "BOSS 识别",
    "team": "队伍成员",
    "ui": "界面显示",
    "connection": "登录或连接",
    "other": "其他问题",
}
CARD_TYPES = {
    "test_2h": ("测试2小时卡", 2 * 60 * 60, False),
    "daily": ("天卡", 24 * 60 * 60, False),
    "weekly": ("周卡", 7 * 24 * 60 * 60, False),
    "monthly": ("月卡", 30 * 24 * 60 * 60, False),
    "partner": ("莫雪的小伙伴", 0, True),
}


def now_epoch() -> float:
    return time.time()


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


def version_tuple(value: object) -> tuple[int, ...]:
    main = clean_text(value, 32).split("+", 1)[0].split("-", 1)[0]
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", main):
        return ()
    return tuple(int(part) for part in main.split("."))


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


def load_update_metadata() -> tuple[dict[str, object], Path] | None:
    try:
        value = json.loads(UPDATE_METADATA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    latest_version = clean_text(value.get("latest_version"), 32)
    client_build = clean_text(value.get("client_build"), 64)
    filename = Path(clean_text(value.get("filename"), 160)).name
    sha256 = clean_text(value.get("sha256"), 64).lower()
    try:
        size = int(value.get("size", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    update_path = UPDATE_METADATA_PATH.parent / filename
    if (
        not version_tuple(latest_version)
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
        or not update_path.is_file()
    ):
        return None
    try:
        if update_path.stat().st_size != size:
            return None
    except OSError:
        return None
    metadata = {
        "latest_version": latest_version,
        "client_build": client_build,
        "filename": filename,
        "sha256": sha256,
        "size": size,
        "notes": clean_multiline_text(value.get("notes"), 1000),
        "required": bool(value.get("required")),
    }
    return metadata, update_path


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
        "card_required": "请输入卡号。",
        "card_invalid": "卡号不存在或格式不正确。",
        "card_expired": "卡号使用时间已结束。",
        "card_revoked": "卡号已被停用。",
        "card_bound": "卡号已绑定其他设备。",
        "card_in_use": "该卡号已在其他客户端登录。",
        "card_rebind_cooldown": "卡号正在设备改绑冷却中。",
        "client_revoked": "当前设备已被停用。",
        "invalid_session": "登录状态已失效，请重新输入卡号。",
    }.get(error, "服务器已拒绝本次登录。")


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with database() as connection:
        connection.executescript(
            """
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
                character_name TEXT NOT NULL DEFAULT '',
                game_pid INTEGER NOT NULL DEFAULT 0,
                remote_ip TEXT NOT NULL DEFAULT ''
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
                rebind_cooldown_seconds INTEGER NOT NULL DEFAULT 43200,
                last_used_at REAL,
                revoked INTEGER NOT NULL DEFAULT 0,
                sold INTEGER NOT NULL DEFAULT 0,
                permanent INTEGER NOT NULL DEFAULT 0,
                remark TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
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
                diagnostics_json TEXT NOT NULL DEFAULT '{}',
                remote_ip TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_last_seen
                ON sessions(last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_client
                ON sessions(client_id, last_seen DESC);
            CREATE INDEX IF NOT EXISTS idx_cards_expires
                ON cards(expires_at);
            CREATE INDEX IF NOT EXISTS idx_feedbacks_created
                ON feedbacks(created_at DESC);
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
        card_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(cards)").fetchall()
        }
        if "last_bound_at" not in card_columns:
            connection.execute("ALTER TABLE cards ADD COLUMN last_bound_at REAL")
        if "rebind_cooldown_seconds" not in card_columns:
            connection.execute(
                """
                ALTER TABLE cards ADD COLUMN rebind_cooldown_seconds
                INTEGER NOT NULL DEFAULT 43200
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

    def _html(self, status: int, value: str) -> None:
        payload = value.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(payload))
        self.wfile.write(payload)

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
                "latest_version": metadata["latest_version"],
                "download_path": (
                    "/api/v1/dps/update/download" if available else ""
                ),
                "sha256": metadata["sha256"] if available else "",
                "size": metadata["size"] if available else 0,
                "filename": metadata["filename"] if available else "",
                "notes": metadata["notes"] if available else "",
                "required": bool(metadata["required"]) if available else False,
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
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(metadata["size"]))
        encoded_filename = quote(str(metadata["filename"]), safe="")
        self.send_header(
            "Content-Disposition",
            "attachment; filename=\"Dps-Logs-update.exe\"; "
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
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/v1/dps/health":
            self._json(HTTPStatus.OK, {"ok": True, "server_time": now_epoch()})
            return
        if path == "/api/v1/dps/update":
            query = parse_qs(parsed.query, keep_blank_values=True)
            current_version = clean_text(query.get("version", [""])[0], 32)
            self._update_info(current_version)
            return
        if path == "/api/v1/dps/update/download":
            self._download_update()
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
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/api/v1/dps/session/start":
            self._start_session()
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
        remote_ip = self._remote_ip()
        session_id = secrets.token_hex(16)
        access_token = secrets.token_urlsafe(32)
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            card = connection.execute(
                "SELECT * FROM cards WHERE card_hash=?", (card_hash,)
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
            bound_client_id = str(card["bound_client_id"] or "")
            activated_at = float(card["activated_at"] or 0)
            last_bound_at = float(card["last_bound_at"] or 0)
            rebind_cooldown = max(
                0, int(card["rebind_cooldown_seconds"] or 0)
            )
            binding_changed = bound_client_id != client_id
            if binding_changed and activated_at and last_bound_at:
                retry_after = max(
                    0, int(last_bound_at + rebind_cooldown - timestamp)
                )
                if retry_after:
                    hours, remainder = divmod(retry_after, 3600)
                    minutes = (remainder + 59) // 60
                    wait_text = (
                        f"{hours} 小时 {minutes} 分钟"
                        if hours
                        else f"{minutes} 分钟"
                    )
                    self._authorization_denied(
                        "card_rebind_cooldown",
                        message=f"该卡号正在设备改绑冷却中，还需 {wait_text}。",
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
                WHERE card_hash=?
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
            connection.execute(
                """
                UPDATE sessions SET ended_at=?, using_app=0
                WHERE client_id=? AND ended_at IS NULL
                    AND COALESCE(app_version, '')<>?
                """,
                (timestamp, client_id, app_version),
            )
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, client_id, token_hash, started_at, last_seen,
                    app_version, remote_ip, card_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    client_id,
                    token_digest(access_token),
                    timestamp,
                    timestamp,
                    app_version,
                    remote_ip,
                    card_hash,
                ),
            )
        self._json(
            HTTPStatus.OK,
            {
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
            },
        )

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
                LEFT JOIN cards k ON k.card_hash=s.card_hash
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
        try:
            game_pid = max(0, int(body.get("game_pid", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            game_pid = 0
        with database() as connection:
            connection.execute(
                """
                UPDATE sessions SET
                    last_seen=?, using_app=?, app_version=?, character_name=?,
                    game_pid=?, remote_ip=?
                WHERE session_id=?
                """,
                (
                    timestamp,
                    using_app,
                    app_version,
                    character_name,
                    game_pid,
                    self._remote_ip(),
                    session["session_id"],
                ),
            )
            connection.execute(
                "UPDATE clients SET last_seen=? WHERE client_id=?",
                (timestamp, session["client_id"]),
            )
            if session_card_hash:
                connection.execute(
                    "UPDATE cards SET last_used_at=? WHERE card_hash=?",
                    (timestamp, session_card_hash),
                )
        self._json(
            HTTPStatus.OK,
            {
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
            },
        )

    def _end_authorized_session(self, session_id: object) -> None:
        with database() as connection:
            connection.execute(
                "UPDATE sessions SET ended_at=?, using_app=0 WHERE session_id=?",
                (now_epoch(), str(session_id)),
            )

    def _end_session(self) -> None:
        token = self._bearer_token()
        session = self._session_for_token(token)
        if session is not None:
            with database() as connection:
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
        with database() as connection:
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
        with database() as connection:
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
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
                FROM cards WHERE card_hash=?
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
                        expires_at=? WHERE card_hash=?
                    """,
                    (duration_seconds, expires_at + duration_seconds, card_id),
                ).rowcount
            else:
                changed += connection.execute(
                    """
                    UPDATE cards SET duration_seconds=duration_seconds+?
                    WHERE card_hash=?
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
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            card = connection.execute(
                "SELECT card_key FROM cards WHERE card_hash=?", (card_id,)
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
                connection.execute(
                    """
                    UPDATE sessions SET ended_at=?, using_app=0
                    WHERE card_hash=? AND ended_at IS NULL
                    """,
                    (timestamp, card_id),
                )
                changed = connection.execute(
                    "DELETE FROM cards WHERE card_hash=?", (card_id,)
                ).rowcount
            elif action == "set_remark":
                remark = clean_text(body.get("remark"), 120)
                changed = connection.execute(
                    "UPDATE cards SET remark=? WHERE card_hash=?",
                    (remark, card_id),
                ).rowcount
            elif action == "revoke":
                changed = connection.execute(
                    "UPDATE cards SET revoked=1 WHERE card_hash=?", (card_id,)
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
                    "UPDATE cards SET revoked=0 WHERE card_hash=?", (card_id,)
                ).rowcount
            elif action == "unbind":
                changed = connection.execute(
                    """
                    UPDATE cards SET bound_client_id='',
                        last_bound_at=CASE
                            WHEN bound_client_id<>'' THEN ? ELSE last_bound_at END
                    WHERE card_hash=?
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
                    "UPDATE cards SET sold=? WHERE card_hash=?",
                    (int(sold), card_id),
                ).rowcount
            elif action == "set_rebind_cooldown":
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
                    UPDATE cards SET rebind_cooldown_seconds=? WHERE card_hash=?
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
<section id="onlinePanel"><div class="panel-head"><h2>在线用户</h2></div><div class="table-wrap"><table><thead><tr><th>卡号</th><th>角色</th><th>版本</th><th>状态</th><th>最近心跳</th><th>IP</th><th>管理</th></tr></thead><tbody id="userRows"></tbody></table><div id="userEmpty" class="empty" hidden>暂无在线记录</div></div></section>
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
let visibleCardIds=[];
let serverNow=0;
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
  if(action==='unbind'&&!confirm('解绑该设备？重新绑定仍受当前改绑冷却限制。'))return;
  if(action==='delete'){
    if(!confirm('确定永久删除卡号 '+(button.dataset.key||'')+'？\\n该卡号的在线会话会立即结束，此操作无法撤销。'))return;
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
    const hours=Number(prompt('设置该卡号的改绑冷却小时数，0 代表允许立即改绑。',button.dataset.hours||'12'));
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
    const actions='<button class="link" data-copy="'+esc(x.card_key)+'">复制</button> '
      +'<button class="link" data-card="'+x.card_id+'" data-action="set_remark" data-remark="'+esc(x.remark||'')+'">备注</button> '
      +(x.permanent?'':'<button class="link" data-card="'+x.card_id+'" data-action="add_time">加时</button> ')
      +'<button class="link" data-card="'+x.card_id+'" data-action="set_sold" data-sold="'+(x.sold?'0':'1')+'">'+(x.sold?'改为未售':'标记已售')+'</button> '
      +'<button class="link" data-card="'+x.card_id+'" data-action="set_rebind_cooldown" data-hours="'+(x.rebind_cooldown_seconds/3600)+'">改绑设置</button> '
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
    allCards=d.cards||[];
    const liveIds=new Set(allCards.map(function(x){return x.card_id}));
    Array.from(selectedCardIds).forEach(function(id){if(!liveIds.has(id))selectedCardIds.delete(id)});
    byId('logged').textContent=d.logged_in;
    byId('using').textContent=d.using_now;
    byId('seen').textContent=d.seen_24h;
    byId('validCards').textContent=(d.active_cards||0)+(d.unused_cards||0);
    byId('refresh').textContent='每 30 秒自动刷新 · '+new Date(d.server_time*1000).toLocaleTimeString();
    byId('userRows').innerHTML=d.sessions.map(function(x){
      const state=x.revoked?'已停用':x.using?'使用中':x.online?'已登录':'离线';
      const cls=x.revoked?'revoked':x.using?'using':x.online?'online':'';
      const card=x.card_key||(x.card_suffix?'****'+x.card_suffix:'旧会话');
      return '<tr><td>'+esc(card)+'</td><td>'+esc(x.character_name||'--')+'</td><td>'+esc(x.app_version||'--')+'</td><td><span class="status '+cls+'"><i class="dot"></i>'+state+'</span></td><td>'+ago(d.server_time,x.last_seen)+'</td><td>'+esc(x.remote_ip)+'</td><td><button class="link '+(x.revoked?'':'danger')+'" data-client="'+esc(x.client_id)+'" data-action="'+(x.revoked?'restore':'revoke')+'">'+(x.revoked?'恢复':'停用')+'</button></td></tr>';
    }).join('');
    byId('userEmpty').hidden=d.sessions.length>0;
    bindActions();
    if(!byId('cardsPanel').hidden)renderCards();
  }catch(e){byId('refresh').textContent='连接失败'}
}

function resetCardPageAndRender(){cardPage=1;renderCards()}
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
