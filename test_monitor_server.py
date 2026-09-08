#!/usr/bin/env python3

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http import HTTPStatus
from unittest import mock
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from diagnostic_report import submit_diagnostic_report
from licensing import (
    CARD_LOGIN_FAILURE_MESSAGE,
    LicensingConnectionError,
    LicensingService,
    ServerLicensingGateway,
    SYSTEM_TIME_SYNC_MESSAGE,
    TrialClaim,
    UpdateInfo,
    normalize_rollback_version,
    rollback_version_is_supported,
)
from server import dps_monitor_server as monitor
from runtime_capability import (
    RuntimeCapabilityError,
    RuntimeCapabilityTimeError,
    capability_public_key_to_base64,
)


class QuietMonitorHandler(monitor.MonitorHandler):
    def log_message(self, format_string: str, *args) -> None:
        del format_string, args


class MonitorServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_partner_card_key = monitor.PARTNER_CARD_KEY
        self.original_capability_key_id = monitor.CAPABILITY_SIGNING_KEY_ID
        self.original_capability_key_path = monitor.CAPABILITY_SIGNING_PRIVATE_KEY_PATH
        self.original_capability_key_cache = monitor._CAPABILITY_SIGNING_KEY_CACHE
        self.original_combat_clock_cleanup_state = (
            monitor._COMBAT_CLOCK_CLEANUP_STATE
        )
        self.original_rollback_metadata_path = monitor.ROLLBACK_METADATA_PATH
        self.original_rollback_backup_path = monitor.ROLLBACK_BACKUP_PATH
        monitor.PARTNER_CARD_KEY = "partner-test-key"
        monitor.DATABASE_PATH = Path(self.temporary.name) / "sessions.sqlite3"
        monitor.UPDATE_METADATA_PATH = Path(self.temporary.name) / "update.json"
        monitor.ROLLBACK_METADATA_PATH = (
            Path(self.temporary.name) / "release-metadata"
        )
        monitor.ROLLBACK_BACKUP_PATH = Path(self.temporary.name) / "release-backups"
        monitor.ADMIN_USER = "tester"
        monitor.ADMIN_PASSWORD = "test-password"
        self.capability_private_key = Ed25519PrivateKey.generate()
        self.capability_key_id = "lease-test-key"
        self.capability_key_path = Path(self.temporary.name) / "lease-key.pem"
        self.capability_key_path.write_bytes(
            self.capability_private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.capability_public_keys = {
            self.capability_key_id: capability_public_key_to_base64(
                self.capability_private_key.public_key()
            )
        }
        monitor.CAPABILITY_SIGNING_KEY_ID = self.capability_key_id
        monitor.CAPABILITY_SIGNING_PRIVATE_KEY_PATH = self.capability_key_path
        monitor._CAPABILITY_SIGNING_KEY_CACHE = None
        monitor._COMBAT_CLOCK_CLEANUP_STATE = None
        monitor.initialize_database()
        self.server = monitor.MonitorServer(("127.0.0.1", 0), QuietMonitorHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3.0)
        self.temporary.cleanup()
        monitor.PARTNER_CARD_KEY = self.original_partner_card_key
        monitor.CAPABILITY_SIGNING_KEY_ID = self.original_capability_key_id
        monitor.CAPABILITY_SIGNING_PRIVATE_KEY_PATH = self.original_capability_key_path
        monitor._CAPABILITY_SIGNING_KEY_CACHE = self.original_capability_key_cache
        monitor._COMBAT_CLOCK_CLEANUP_STATE = (
            self.original_combat_clock_cleanup_state
        )
        monitor.ROLLBACK_METADATA_PATH = self.original_rollback_metadata_path
        monitor.ROLLBACK_BACKUP_PATH = self.original_rollback_backup_path

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        token: str = "",
        admin: bool = False,
        authorization: str = "",
    ) -> tuple[int, dict | None]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif admin:
            credentials = base64.b64encode(b"tester:test-password").decode("ascii")
            headers["Authorization"] = f"Basic {credentials}"
        elif authorization:
            headers["Authorization"] = authorization
        request = Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=3.0) as response:
                status = response.status
                raw = response.read()
        except HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            finally:
                exc.close()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            value = None
        return status, value

    def test_update_download_stops_at_verified_size_without_waiting_for_eof(self):
        payload = b"MZ" + bytes(range(256)) * 8

        class Response:
            headers = {"Content-Length": str(len(payload))}

            def __init__(self):
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size: int) -> bytes:
                if self.offset >= len(payload):
                    raise AssertionError("client waited for EOF after full file")
                result = payload[self.offset : self.offset + size]
                self.offset += len(result)
                return result

        gateway = ServerLicensingGateway(
            self.base_url, "f" * 32, "0.1.1"
        )
        response = Response()
        gateway._open_get = lambda _path: response
        update = UpdateInfo(
            available=True,
            latest_version="0.1.2",
            download_path="/api/v1/dps/update/download",
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            filename="Dps-Logs-v0.1.2.exe",
        )
        destination = Path(self.temporary.name) / "downloaded.exe"

        self.assertEqual(
            gateway.download_update(update, destination), destination
        )
        self.assertEqual(destination.read_bytes(), payload)

    def test_only_protected_gateway_requires_v2_runtime_capability(self):
        legacy_value = {"schema_version": 1}
        legacy_gateway = ServerLicensingGateway(
            self.base_url,
            "1" * 32,
            "0.1.6+20260904.3",
        )
        protected_gateway = ServerLicensingGateway(
            self.base_url,
            "1" * 32,
            "0.1.7+20260905.1",
            require_runtime_capability=True,
            trusted_capability_public_keys=self.capability_public_keys,
        )

        self.assertIsNone(
            legacy_gateway._runtime_capability(
                legacy_value,
                session_id="1" * 32,
            )
        )
        with self.assertRaises(LicensingConnectionError):
            protected_gateway._runtime_capability(
                legacy_value,
                session_id="1" * 32,
            )

    def test_runtime_capability_clock_error_requests_system_time_sync(self):
        gateway = ServerLicensingGateway(
            self.base_url,
            "1" * 32,
            "0.1.7+20260905.1",
            require_runtime_capability=True,
        )

        with mock.patch(
            "licensing.RuntimeCapability.from_value",
            side_effect=RuntimeCapabilityTimeError(
                "runtime capability lease has expired or local clock is invalid"
            ),
        ):
            with self.assertRaises(LicensingConnectionError) as raised:
                gateway._runtime_capability(
                    {"schema_version": 2},
                    session_id="1" * 32,
                )
        self.assertEqual(str(raised.exception), SYSTEM_TIME_SYNC_MESSAGE)
        self.assertNotIn("runtime capability", str(raised.exception))

    def test_non_clock_runtime_capability_error_uses_plain_login_message(self):
        gateway = ServerLicensingGateway(
            self.base_url,
            "1" * 32,
            "0.1.7+20260905.1",
            require_runtime_capability=True,
        )

        with mock.patch(
            "licensing.RuntimeCapability.from_value",
            side_effect=RuntimeCapabilityError(
                "runtime capability signature is invalid"
            ),
        ):
            with self.assertRaises(LicensingConnectionError) as raised:
                gateway._runtime_capability(
                    {"schema_version": 2},
                    session_id="1" * 32,
                )
        self.assertEqual(str(raised.exception), CARD_LOGIN_FAILURE_MESSAGE)

    def create_card(
        self,
        *,
        duration_seconds: int = 86400,
        count: int = 1,
        note: str = "测试卡",
        card_type: str = "",
        custom_card_key: str = "",
        remark: str = "",
    ) -> list[str]:
        if not card_type:
            if "周卡" in note or duration_seconds == 7 * 86400:
                card_type = "weekly"
            elif "月卡" in note or duration_seconds == 30 * 86400:
                card_type = "monthly"
            elif "测试" in note or duration_seconds == 2 * 3600:
                card_type = "test_2h"
            else:
                card_type = "daily"
        if card_type == "partner" and not custom_card_key:
            custom_card_key = "partnerfriend001"
        status, value = self.request(
            "/api/v1/dps/admin/cards/create",
            method="POST",
            admin=True,
            body={
                "card_type": card_type,
                "count": count,
                "custom_card_key": custom_card_key,
                "remark": remark,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(value["ok"])
        self.assertEqual(len(value["cards"]), count)
        for card_key in value["cards"]:
            if card_type == "partner":
                self.assertEqual(
                    card_key, monitor.normalize_custom_card_key(custom_card_key)
                )
            else:
                self.assertRegex(card_key, r"^GMZZ[A-HJ-NP-Z2-9]{26}$")
                self.assertEqual(len(card_key), 30)
                self.assertNotIn("-", card_key)
        return value["cards"]

    def test_trial_claim_creates_bound_two_hour_card_and_gateway_parses_it(self):
        client_id = "a" * 32
        gateway = ServerLicensingGateway(self.base_url, client_id, "0.1.7-test")

        claim = gateway.claim_trial_card()

        self.assertIsInstance(claim, TrialClaim)
        self.assertTrue(claim.accepted)
        self.assertRegex(claim.card_key, r"^GMZZ[A-HJ-NP-Z2-9]{26}$")
        self.assertEqual(claim.duration_seconds, 2 * 60 * 60)
        self.assertIn("2 小时", claim.message)

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        card = next(
            value for value in summary["cards"] if value["card_key"] == claim.card_key
        )
        self.assertEqual(card["duration_seconds"], 2 * 60 * 60)
        self.assertEqual(card["bound_client_id"], client_id)
        self.assertEqual(card["note"], monitor.TRIAL_CARD_NOTE)
        self.assertEqual(card["remark"], monitor.TRIAL_CARD_REMARK)
        self.assertTrue(card["sold"])
        self.assertEqual(card["state"], "unused")

        session = gateway.sign_in_card(claim.card_key)
        self.assertTrue(session.active)
        self.assertIsNotNone(session.expires_at)
        remaining = session.expires_at.timestamp() - monitor.now_epoch()
        self.assertGreaterEqual(remaining, 2 * 60 * 60 - 3)
        self.assertLessEqual(remaining, 2 * 60 * 60 + 1)
        gateway.sign_out(session)

    def test_claimed_trial_card_cannot_rebind_before_twelve_hours(self):
        claimed_at = 2_000_000_000.0
        client_id = "a" * 32
        with mock.patch.object(monitor, "now_epoch", return_value=claimed_at):
            status, claim = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={
                    "client_id": client_id,
                    "app_version": "0.1.7-test",
                    "request_id": "1" * 32,
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(claim["ok"])

        with mock.patch.object(monitor, "now_epoch", return_value=claimed_at):
            status, denied = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": claim["card_key"]},
            )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "card_rebind_cooldown")
        self.assertEqual(denied["retry_after"], 12 * 60 * 60)
        self.assertIn("还需 12 小时", denied["message"])

        with mock.patch.object(monitor, "now_epoch", return_value=claimed_at):
            status, owner = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": client_id, "card_key": claim["card_key"]},
            )
        self.assertEqual(status, 200)
        self.assertTrue(owner["authorized"])

    def test_trial_claim_allows_only_one_new_card_per_beijing_day_and_device(self):
        client_id = "b" * 32
        other_client_id = "c" * 32
        timestamp = 2_000_000_000.25
        next_day = monitor.trial_next_available_at(timestamp) + 1
        first_request_id = "1" * 32

        with mock.patch.object(monitor, "now_epoch", return_value=timestamp):
            status, first = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={
                    "client_id": client_id,
                    "app_version": "0.1.7-test",
                    "request_id": first_request_id,
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(first["ok"])

            status, retried = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={
                    "client_id": client_id,
                    "app_version": "0.1.7-test",
                    "request_id": first_request_id,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(retried["card_key"], first["card_key"])

            status, repeated = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={
                    "client_id": client_id,
                    "app_version": "0.1.7-test",
                    "request_id": "2" * 32,
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(repeated["ok"])
            self.assertEqual(repeated["card_key"], first["card_key"])
            self.assertEqual(
                repeated["message"],
                "已恢复今天领取的 2 小时试用卡，请点击“登录”。",
            )

            status, other = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={
                    "client_id": other_client_id,
                    "app_version": "0.1.7-test",
                    "request_id": "3" * 32,
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(other["ok"])

            with monitor.database() as connection:
                connection.execute(
                    "DELETE FROM cards WHERE card_key=?", (first["card_key"],)
                )
            status, after_admin_delete = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={
                    "client_id": client_id,
                    "app_version": "0.1.7-test",
                    "request_id": first_request_id,
                },
            )
            self.assertEqual(status, 403)
            self.assertEqual(after_admin_delete["error"], "trial_daily_limit")
            self.assertEqual(
                after_admin_delete["message"],
                "今天已领取过试用卡，每台设备每天限领一次，请明天再试。",
            )

        with mock.patch.object(monitor, "now_epoch", return_value=next_day):
            status, following_day = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={
                    "client_id": client_id,
                    "app_version": "0.1.7-test",
                    "request_id": "4" * 32,
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(following_day["ok"])
            self.assertNotEqual(following_day["card_key"], first["card_key"])

        with monitor.database() as connection:
            cards = connection.execute(
                "SELECT COUNT(*) AS total FROM cards WHERE note=?",
                (monitor.TRIAL_CARD_NOTE,),
            ).fetchone()
            claims = connection.execute(
                "SELECT COUNT(*) AS total FROM trial_claims"
            ).fetchone()
        self.assertEqual(int(cards["total"]), 2)
        self.assertEqual(int(claims["total"]), 3)

    def test_feedback_schema_does_not_add_processing_status(self):
        legacy_path = Path(self.temporary.name) / "legacy-feedback.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """
                CREATE TABLE feedbacks (
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
                )
                """
            )
            connection.execute(
                """
                INSERT INTO feedbacks(
                    feedback_id, created_at, updated_at, content
                ) VALUES('FB1111111111111111', 1, 1, 'legacy')
                """
            )
            connection.commit()
        finally:
            connection.close()

        active_path = monitor.DATABASE_PATH
        try:
            monitor.DATABASE_PATH = legacy_path
            monitor.initialize_database()
        finally:
            monitor.DATABASE_PATH = active_path

        connection = sqlite3.connect(legacy_path)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(feedbacks)")
            }
        finally:
            connection.close()
        self.assertNotIn("status", columns)

    def test_update_metadata_version_comparison_and_download(self):
        update_bytes = b"MZ" + bytes(range(64))
        update_name = "叨叨诡秘-Dps-Logs-v0.0.4.exe"
        update_path = monitor.UPDATE_METADATA_PATH.parent / update_name
        update_path.write_bytes(update_bytes)
        digest = hashlib.sha256(update_bytes).hexdigest()
        monitor.UPDATE_METADATA_PATH.write_text(
            json.dumps(
                {
                    "latest_version": "0.0.4",
                    "filename": update_name,
                    "size": len(update_bytes),
                    "sha256": digest,
                    "notes": "修复 DPS 统计与目标锁定",
                    "required": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status, update = self.request(
            "/api/v1/dps/update?version=0.0.3%2B20260827.4"
        )
        self.assertEqual(status, 200)
        self.assertTrue(update["available"])
        self.assertEqual(update["latest_version"], "0.0.4")
        self.assertEqual(update["sha256"], digest)
        self.assertEqual(update["size"], len(update_bytes))

        with urlopen(
            self.base_url + update["download_path"], timeout=3.0
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(
                'filename="Dps-Logs-v0.0.4.exe"',
                response.headers.get("Content-Disposition", ""),
            )
            self.assertEqual(response.read(), update_bytes)

        gateway = ServerLicensingGateway(
            self.base_url, "f" * 32, "0.0.3+20260827.4"
        )
        gateway_update = gateway.check_update()
        self.assertTrue(gateway_update.available)
        progress = []
        downloaded = gateway.download_update(
            gateway_update,
            Path(self.temporary.name) / "downloaded.exe",
            lambda written, total: progress.append((written, total)),
        )
        self.assertEqual(downloaded.read_bytes(), update_bytes)
        self.assertEqual(progress[0], (0, len(update_bytes)))
        self.assertEqual(progress[-1], (len(update_bytes), len(update_bytes)))

        status, current = self.request(
            "/api/v1/dps/update?version=0.0.4%2B20260828.1"
        )
        self.assertEqual(status, 200)
        self.assertFalse(current["available"])
        self.assertEqual(current["size"], 0)

        status, future = self.request("/api/v1/dps/update?version=0.0.5")
        self.assertEqual(status, 200)
        self.assertFalse(future["available"])

        update_path.write_bytes(update_bytes + b"corrupt")
        status, missing = self.request("/api/v1/dps/update?version=0.0.3")
        self.assertEqual(status, 200)
        self.assertFalse(missing["available"])

    def test_rollback_version_floor_and_normalization(self):
        for value, expected in (
            ("v0.1.0", "0.1.0"),
            ("0.1.7B", "0.1.7b"),
            ("01.001.000", "1.1.0"),
            ("0.0.14", "0.0.14"),
            ("../../0.1.2", ""),
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_rollback_version(value), expected)
                self.assertEqual(monitor.normalize_rollback_version(value), expected)
        self.assertTrue(rollback_version_is_supported("0.1.0"))
        self.assertTrue(monitor.rollback_version_is_supported("v0.1.7b"))
        self.assertFalse(rollback_version_is_supported("0.0.14"))
        self.assertFalse(monitor.rollback_version_is_supported("../../0.1.2"))

    def test_version_rollback_metadata_and_download(self):
        rollback_bytes = b"MZ-rollback-0.1.4" + bytes(range(48))
        rollback_name = "叨叨诡秘-Dps-Logs-v0.1.4.exe"
        rollback_path = monitor.UPDATE_METADATA_PATH.parent / rollback_name
        rollback_path.write_bytes(rollback_bytes)
        digest = hashlib.sha256(rollback_bytes).hexdigest()
        metadata_dir = monitor.ROLLBACK_METADATA_PATH / "v0.1.4-test-build"
        metadata_dir.mkdir(parents=True)
        (metadata_dir / "update.json").write_text(
            json.dumps(
                {
                    "latest_version": "0.1.4",
                    "display_version": "0.1.4",
                    "client_build": "0.1.4+20260903.1",
                    "filename": rollback_name,
                    "size": len(rollback_bytes),
                    "sha256": digest,
                    "notes": "历史版本",
                    "required": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status, info = self.request(
            "/api/v1/dps/update/rollback?version=0.1.4"
        )
        self.assertEqual(status, 200)
        self.assertTrue(info["available"])
        self.assertTrue(info["rollback"])
        self.assertEqual(info["latest_version"], "0.1.4")
        self.assertEqual(info["sha256"], digest)
        self.assertEqual(
            info["download_path"],
            "/api/v1/dps/update/rollback/download?version=0.1.4",
        )

        with urlopen(self.base_url + info["download_path"], timeout=3.0) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), rollback_bytes)

        gateway = ServerLicensingGateway(
            self.base_url, "f" * 32, "0.1.7+20260905.3"
        )
        rollback = gateway.check_rollback("v0.1.4")
        self.assertTrue(rollback.available)
        self.assertTrue(rollback.rollback)
        destination = Path(self.temporary.name) / "rollback-downloaded.exe"
        self.assertEqual(
            gateway.download_update(rollback, destination), destination
        )
        self.assertEqual(destination.read_bytes(), rollback_bytes)

        status, unsupported = self.request(
            "/api/v1/dps/update/rollback?version=0.0.14"
        )
        self.assertEqual(status, 400)
        self.assertEqual(unsupported["error"], "rollback_version_unsupported")
        status, missing = self.request(
            "/api/v1/dps/update/rollback?version=0.1.3"
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"], "rollback_unavailable")

    def test_rollback_metadata_skips_unreadable_backup_directory(self):
        blocked_dir = monitor.ROLLBACK_BACKUP_PATH / "root-only-backup"
        blocked_dir.mkdir(parents=True)
        blocked_metadata = blocked_dir / "update.json"
        valid_dir = monitor.ROLLBACK_METADATA_PATH / "v0.1.4-valid"
        valid_dir.mkdir(parents=True)
        valid_metadata = valid_dir / "update.json"
        valid_metadata.write_text("{}", encoding="utf-8")
        original_is_file = Path.is_file

        def guarded_is_file(path):
            if path == blocked_metadata:
                raise PermissionError("root-only backup")
            return original_is_file(path)

        with mock.patch.object(Path, "is_file", guarded_is_file):
            candidates = monitor._rollback_metadata_candidates()

        self.assertIn(valid_metadata, candidates)
        self.assertNotIn(blocked_metadata, candidates)

    def test_update_metadata_can_replace_an_older_same_version_build(self):
        update_bytes = b"MZ" + bytes(range(32))
        update_name = "dps-logs-v0.0.14.exe"
        update_path = monitor.UPDATE_METADATA_PATH.parent / update_name
        update_path.write_bytes(update_bytes)
        monitor.UPDATE_METADATA_PATH.write_text(
            json.dumps(
                {
                    "latest_version": "0.0.14",
                    "display_version": "0.0.14b",
                    "client_build": "0.0.14+20260831.2",
                    "filename": update_name,
                    "size": len(update_bytes),
                    "sha256": hashlib.sha256(update_bytes).hexdigest(),
                    "notes": "",
                    "required": False,
                }
            ),
            encoding="utf-8",
        )

        for old_build in (
            "0.0.13+20260831.5",
            "0.0.14",
            "0.0.14+20260831.1",
        ):
            with self.subTest(old_build=old_build):
                status, update = self.request(
                    "/api/v1/dps/update?version=" + old_build.replace("+", "%2B")
                )
                self.assertEqual(status, 200)
                self.assertTrue(update["available"])
                self.assertEqual(update["latest_version"], "0.0.14b")
                self.assertEqual(update["notes"], "")

        for current_build in (
            "0.0.14+20260831.2",
            "0.0.14+20260831.3",
            "0.0.15",
        ):
            with self.subTest(current_build=current_build):
                status, update = self.request(
                    "/api/v1/dps/update?version="
                    + current_build.replace("+", "%2B")
                )
                self.assertEqual(status, 200)
                self.assertFalse(update["available"])

    def test_v017b_hotfix_updates_the_published_v017_build_once(self):
        self.assertTrue(
            monitor.update_is_available(
                "0.1.7+20260905.2",
                "0.1.7",
                "0.1.7+20260905.3",
            )
        )
        self.assertFalse(
            monitor.update_is_available(
                "0.1.7+20260905.3",
                "0.1.7",
                "0.1.7+20260905.3",
            )
        )

    def test_build_allowlist_binds_login_heartbeat_and_update_access(self):
        original_enforce = monitor.ENFORCE_BUILD_ALLOWLIST
        original_allowlist = monitor.BUILD_ALLOWLIST_PATH
        original_runtime_profile = monitor.RUNTIME_PROFILE_PATH
        client_build = "0.1.2+20260902.1"
        build_id = "a" * 32
        publisher = "B" * 40
        allowlist_path = Path(self.temporary.name) / "build-allowlist.json"
        allowlist_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "builds": [
                        {
                            "build_id": build_id,
                            "client_build": client_build,
                            "publisher_thumbprint": publisher,
                            "runtime_profile_id": "c7-2026-09-02",
                            "official": False,
                            "protected": True,
                            "capability_signing_key_id": self.capability_key_id,
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        monitor.BUILD_ALLOWLIST_PATH = allowlist_path
        monitor.RUNTIME_PROFILE_PATH = Path(__file__).resolve().with_name(
            "runtime-profile.dev.json"
        )
        monitor.ENFORCE_BUILD_ALLOWLIST = True
        try:
            status, missing_trial = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={"client_id": "e" * 32, "app_version": client_build},
            )
            self.assertEqual(status, 403)
            self.assertEqual(missing_trial["error"], "client_build_required")
            status, unknown_trial = self.request(
                "/api/v1/dps/trial/claim",
                method="POST",
                body={
                    "client_id": "e" * 32,
                    "app_version": client_build,
                    "build_id": "c" * 32,
                },
            )
            self.assertEqual(status, 403)
            self.assertEqual(unknown_trial["error"], "client_build_not_allowed")

            card_key = self.create_card()[0]
            status, missing = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={
                    "client_id": "a" * 32,
                    "card_key": card_key,
                    "app_version": client_build,
                },
            )
            self.assertEqual(status, 403)
            self.assertEqual(missing["error"], "client_build_required")

            status, unknown = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={
                    "client_id": "a" * 32,
                    "card_key": card_key,
                    "app_version": client_build,
                    "build_id": "c" * 32,
                },
            )
            self.assertEqual(status, 403)
            self.assertEqual(unknown["error"], "client_build_not_allowed")

            gateway = ServerLicensingGateway(
                self.base_url,
                "a" * 32,
                client_build,
                build_id=build_id,
                require_runtime_capability=True,
                trusted_capability_public_keys=self.capability_public_keys,
                timeout=2,
            )
            trial = gateway.claim_trial_card()
            self.assertTrue(trial.accepted)
            session = gateway.sign_in_card(card_key)
            self.assertTrue(session.active)
            self.assertIsNotNone(session.runtime_capability)
            self.assertEqual(
                session.runtime_capability.session_id,
                session.session_id,
            )
            heartbeat = gateway.heartbeat(
                session,
                using=True,
                character_name="莫雪",
                game_pid=1234,
            )
            self.assertTrue(heartbeat.authorized)
            self.assertIsNotNone(heartbeat.runtime_capability)
            self.assertGreater(
                heartbeat.runtime_capability.expires_at,
                heartbeat.runtime_capability.issued_at,
            )
            self.assertEqual(heartbeat.runtime_capability.lease_sequence, 2)

            status, anonymous_update = self.request(
                "/api/v1/dps/update?version=" + client_build.replace("+", "%2B")
            )
            self.assertEqual(status, 403)
            self.assertEqual(anonymous_update["error"], "invalid_session")
            self.assertFalse(gateway.check_update().available)

            status, mismatch = self.request(
                "/api/v1/dps/session/heartbeat",
                method="POST",
                token=session.access_token,
                body={
                    "using": True,
                    "app_version": client_build,
                    "build_id": "c" * 32,
                },
            )
            self.assertEqual(status, 403)
            self.assertEqual(mismatch["error"], "client_build_mismatch")

            connection = sqlite3.connect(monitor.DATABASE_PATH)
            try:
                row = connection.execute(
                    "SELECT build_id FROM sessions ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], build_id)
        finally:
            monitor.ENFORCE_BUILD_ALLOWLIST = original_enforce
            monitor.BUILD_ALLOWLIST_PATH = original_allowlist
            monitor.RUNTIME_PROFILE_PATH = original_runtime_profile

    def test_registered_legacy_build_stays_compatible_without_v2_lease(self):
        original_enforce = monitor.ENFORCE_BUILD_ALLOWLIST
        original_allowlist = monitor.BUILD_ALLOWLIST_PATH
        client_build = "0.1.6+20260904.3"
        build_id = "6" * 32
        allowlist_path = Path(self.temporary.name) / "legacy-build-allowlist.json"
        allowlist_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "builds": [
                        {
                            "build_id": build_id,
                            "client_build": client_build,
                            "publisher_thumbprint": "",
                            "runtime_profile_id": "c7-2026-09-02",
                            "official": False,
                            "protected": False,
                            "capability_signing_key_id": "",
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        monitor.BUILD_ALLOWLIST_PATH = allowlist_path
        monitor.ENFORCE_BUILD_ALLOWLIST = True
        try:
            card_key = self.create_card()[0]
            gateway = ServerLicensingGateway(
                self.base_url,
                "6" * 32,
                client_build,
                build_id=build_id,
                timeout=2,
            )

            session = gateway.sign_in_card(card_key)
            heartbeat = gateway.heartbeat(
                session,
                using=True,
                character_name="legacy",
                game_pid=1234,
            )

            self.assertTrue(session.active)
            self.assertIsNone(session.runtime_capability)
            self.assertTrue(heartbeat.authorized)
            self.assertIsNone(heartbeat.runtime_capability)
        finally:
            monitor.ENFORCE_BUILD_ALLOWLIST = original_enforce
            monitor.BUILD_ALLOWLIST_PATH = original_allowlist

    def test_enforced_build_cannot_login_without_server_runtime_profile(self):
        original_enforce = monitor.ENFORCE_BUILD_ALLOWLIST
        original_allowlist = monitor.BUILD_ALLOWLIST_PATH
        original_runtime_profile = monitor.RUNTIME_PROFILE_PATH
        client_build = "0.1.2+20260902.1"
        build_id = "d" * 32
        allowlist_path = Path(self.temporary.name) / "build-allowlist.json"
        allowlist_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "builds": [
                        {
                            "build_id": build_id,
                            "client_build": client_build,
                            "publisher_thumbprint": "E" * 40,
                            "runtime_profile_id": "c7-2026-09-02",
                            "official": False,
                            "protected": True,
                            "capability_signing_key_id": self.capability_key_id,
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        monitor.BUILD_ALLOWLIST_PATH = allowlist_path
        monitor.RUNTIME_PROFILE_PATH = (
            Path(self.temporary.name) / "missing-runtime-profile.json"
        )
        monitor.ENFORCE_BUILD_ALLOWLIST = True
        try:
            card_key = self.create_card()[0]
            status, value = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={
                    "client_id": "f" * 32,
                    "card_key": card_key,
                    "app_version": client_build,
                    "build_id": build_id,
                },
            )
            self.assertEqual(status, 403)
            self.assertEqual(
                value["error"], "runtime_capability_unavailable"
            )
            connection = sqlite3.connect(monitor.DATABASE_PATH)
            try:
                activated_at = connection.execute(
                    "SELECT activated_at FROM cards WHERE card_hash=?",
                    (monitor.token_digest(card_key),),
                ).fetchone()[0]
                session_count = connection.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertFalse(activated_at)
            self.assertEqual(session_count, 0)
        finally:
            monitor.ENFORCE_BUILD_ALLOWLIST = original_enforce
            monitor.BUILD_ALLOWLIST_PATH = original_allowlist
            monitor.RUNTIME_PROFILE_PATH = original_runtime_profile

    def test_session_lifecycle_status_and_revoke(self):
        status, health = self.request("/api/v1/dps/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])

        status, invalid = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "../../invalid"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"], "bad_client_id")

        client_id = "a" * 32
        card_key = self.create_card()[0]
        status, started = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={
                "client_id": client_id,
                "app_version": "0.0.1",
                "card_key": card_key,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(started["authorized"])
        self.assertEqual(started["card_tier"], "normal")
        token = started["access_token"]

        status, heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=token,
            body={
                "using": True,
                "character_name": "莫雪",
                "game_pid": 1234,
                "app_version": "0.0.1",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(heartbeat["authorized"])
        self.assertEqual(heartbeat["card_tier"], "normal")

        status, summary = self.request(
            "/api/v1/dps/admin/status", admin=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(summary["logged_in"], 1)
        self.assertEqual(summary["using_now"], 1)
        self.assertEqual(summary["sessions"][0]["character_name"], "莫雪")
        self.assertEqual(summary["sessions"][0]["card_key"], card_key)
        self.assertEqual(summary["sessions"][0]["card_suffix"], card_key[-4:])
        self.assertEqual(summary["cards"][0]["card_key"], card_key)
        self.assertEqual(summary["cards"][0]["state"], "active")

        status, revoked = self.request(
            "/api/v1/dps/admin/revoke",
            method="POST",
            admin=True,
            body={"client_id": client_id, "revoked": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(revoked["changed"])

        status, denied = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=token,
            body={"using": True},
        )
        self.assertEqual(status, 403)
        self.assertFalse(denied["authorized"])

    def test_card_required_and_device_rebind_observes_twelve_hour_cooldown(self):
        status, missing = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "a" * 32},
        )
        self.assertEqual(status, 403)
        self.assertEqual(missing["error"], "card_required")

        self.assertTrue(monitor.CARD_DEVICE_REBIND_ENABLED)
        self.assertEqual(monitor.CARD_REBIND_COOLDOWN_SECONDS, 12 * 60 * 60)
        card_key = self.create_card(card_type="daily")[0]
        first_bound_at = 2_000_000_000.0
        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, first = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "a" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 200)
        self.assertEqual(first["remaining_seconds"], 24 * 60 * 60)

        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, replacement = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={
                    "client_id": "a" * 32,
                    "card_key": card_key,
                    "app_version": "0.0.2",
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(replacement["authorized"])

        status, replaced = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=first["access_token"],
            body={"using": True},
        )
        self.assertEqual(status, 403)
        self.assertEqual(replaced["error"], "invalid_session")

        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, in_use = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 403)
        self.assertEqual(in_use["error"], "card_in_use")
        self.assertIn("请先在原设备完全退出程序", in_use["message"])

        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, ended = self.request(
                "/api/v1/dps/session/end",
                method="POST",
                token=replacement["access_token"],
                body={"session_id": replacement["session_id"]},
            )
        self.assertEqual(status, 200)
        self.assertTrue(ended["ok"])

        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, cooldown = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 403)
        self.assertEqual(cooldown["error"], "card_rebind_cooldown")
        self.assertEqual(cooldown["retry_after"], 12 * 60 * 60)
        self.assertIn("刚在另一台设备使用", cooldown["message"])
        self.assertIn("还需 12 小时", cooldown["message"])

        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        self.assertTrue(summary["device_rebind_enabled"])
        self.assertEqual(
            summary["default_rebind_cooldown_seconds"],
            12 * 60 * 60,
        )
        card = next(item for item in summary["cards"] if item["card_key"] == card_key)
        self.assertEqual(card["bound_client_id"], "a" * 32)
        self.assertFalse(card["revoked"])
        self.assertEqual(card["rebind_cooldown_seconds"], 12 * 60 * 60)
        self.assertEqual(card["rebind_remaining_seconds"], 12 * 60 * 60)

        almost_ready = first_bound_at + 12 * 60 * 60 - 1
        with mock.patch.object(monitor, "now_epoch", return_value=almost_ready):
            status, almost = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 403)
        self.assertEqual(almost["error"], "card_rebind_cooldown")
        self.assertEqual(almost["retry_after"], 1)
        self.assertIn("还需 1 分钟", almost["message"])

        rebind_at = first_bound_at + 12 * 60 * 60
        with mock.patch.object(monitor, "now_epoch", return_value=rebind_at):
            status, rebound = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 200)
        self.assertTrue(rebound["authorized"])
        with monitor.database() as connection:
            rebound_card = connection.execute(
                "SELECT bound_client_id, last_bound_at FROM cards WHERE card_key=?",
                (card_key,),
            ).fetchone()
        self.assertEqual(rebound_card["bound_client_id"], "b" * 32)
        self.assertEqual(rebound_card["last_bound_at"], rebind_at)

    def test_existing_ordinary_cards_are_migrated_to_twelve_hour_cooldown(self):
        card_key = self.create_card(duration_seconds=7200, note="测试2小时卡")[0]
        with monitor.database() as connection:
            connection.execute(
                "UPDATE cards SET rebind_cooldown_seconds=? WHERE card_key=?",
                (7 * 24 * 60 * 60, card_key),
            )

        monitor.initialize_database()

        with monitor.database() as connection:
            row = connection.execute(
                "SELECT rebind_cooldown_seconds, permanent FROM cards WHERE card_key=?",
                (card_key,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertFalse(bool(row["permanent"]))
        self.assertEqual(row["rebind_cooldown_seconds"], 12 * 60 * 60)

    def test_admin_rebind_controls_describe_the_twelve_hour_policy(self):
        self.assertIn("解绑后仍需等待 12 小时", monitor.ADMIN_PAGE)
        self.assertIn("默认策略为 12 小时", monitor.ADMIN_PAGE)
        self.assertIn(
            "刚在另一台设备使用",
            monitor.card_error("card_rebind_cooldown"),
        )

        card_key = self.create_card(card_type="daily")[0]
        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        self.assertTrue(summary["device_rebind_enabled"])
        self.assertEqual(summary["default_rebind_cooldown_seconds"], 12 * 60 * 60)
        card = next(item for item in summary["cards"] if item["card_key"] == card_key)

        status, changed = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={
                "card_id": card["card_id"],
                "action": "set_rebind_cooldown",
                "cooldown_hours": 6,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(changed["changed"])
        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        card = next(item for item in summary["cards"] if item["card_key"] == card_key)
        self.assertEqual(card["rebind_cooldown_seconds"], 6 * 60 * 60)

    def test_manual_unbind_starts_a_new_twelve_hour_cooldown(self):
        card_key = self.create_card(card_type="daily")[0]
        first_bound_at = 2_000_000_000.0
        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, _started = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "a" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 200)
        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        card = next(item for item in summary["cards"] if item["card_key"] == card_key)

        unbound_at = first_bound_at + 60
        with mock.patch.object(monitor, "now_epoch", return_value=unbound_at):
            status, unbound = self.request(
                "/api/v1/dps/admin/cards/update",
                method="POST",
                admin=True,
                body={"card_id": card["card_id"], "action": "unbind"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(unbound["changed"])

        with mock.patch.object(monitor, "now_epoch", return_value=unbound_at):
            status, denied = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "card_rebind_cooldown")
        self.assertEqual(denied["retry_after"], 12 * 60 * 60)

        with mock.patch.object(
            monitor,
            "now_epoch",
            return_value=unbound_at + 12 * 60 * 60,
        ):
            status, rebound = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 200)
        self.assertTrue(rebound["authorized"])

    def test_legacy_activated_card_without_binding_metadata_keeps_cooldown(self):
        card_key = self.create_card(card_type="daily")[0]
        first_bound_at = 2_000_000_000.0
        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, started = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "a" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 200)
        with mock.patch.object(monitor, "now_epoch", return_value=first_bound_at):
            status, ended = self.request(
                "/api/v1/dps/session/end",
                method="POST",
                token=started["access_token"],
                body={"session_id": started["session_id"]},
            )
        self.assertEqual(status, 200)
        self.assertTrue(ended["ok"])

        # Simulate an old database row where an administrative clear or a
        # pre-binding migration removed both binding fields after activation.
        with monitor.database() as connection:
            connection.execute(
                """
                UPDATE cards SET bound_client_id='', last_bound_at=NULL
                WHERE card_hash=?
                """,
                (monitor.token_digest(card_key),),
            )

        with mock.patch.object(
            monitor, "now_epoch", return_value=first_bound_at + 1
        ):
            status, denied = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "card_rebind_cooldown")
        self.assertEqual(denied["retry_after"], 12 * 60 * 60 - 1)

        with mock.patch.object(
            monitor,
            "now_epoch",
            return_value=first_bound_at + 12 * 60 * 60,
        ):
            status, rebound = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={"client_id": "b" * 32, "card_key": card_key},
            )
        self.assertEqual(status, 200)
        self.assertTrue(rebound["authorized"])

    def test_stale_session_does_not_lock_card_forever(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        client_id = "9" * 32
        status, first = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": client_id, "card_key": card_key},
        )
        self.assertEqual(status, 200)

        with monitor.database() as connection:
            connection.execute(
                "UPDATE sessions SET last_seen=? WHERE session_id=?",
                (
                    monitor.now_epoch() - monitor.ONLINE_WINDOW - 1,
                    first["session_id"],
                ),
            )

        status, restarted = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": client_id, "card_key": card_key},
        )
        self.assertEqual(status, 200)
        self.assertTrue(restarted["authorized"])
        with monitor.database() as connection:
            stale = connection.execute(
                "SELECT ended_at FROM sessions WHERE session_id=?",
                (first["session_id"],),
            ).fetchone()
        self.assertIsNotNone(stale["ended_at"])

    def test_same_version_sessions_on_same_device_do_not_revoke_each_other(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        client_id = "7" * 32
        sessions = []
        for _index in range(2):
            status, session = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={
                    "client_id": client_id,
                    "card_key": card_key,
                    "app_version": "0.0.14+20260831.2",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(session["authorized"])
            sessions.append(session)

        for session in sessions:
            status, heartbeat = self.request(
                "/api/v1/dps/session/heartbeat",
                method="POST",
                token=session["access_token"],
                body={"using": True},
            )
            self.assertEqual(status, 200)
            self.assertTrue(heartbeat["authorized"])

        with monitor.database() as connection:
            active_count = connection.execute(
                """
                SELECT COUNT(*) FROM sessions
                WHERE client_id=? AND ended_at IS NULL
                """,
                (client_id,),
            ).fetchone()[0]
        self.assertEqual(active_count, 2)

    def test_same_release_different_builds_do_not_revoke_each_other(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        client_id = "8" * 32
        sessions = []
        for app_version in (
            "0.0.14+20260831.1",
            "0.0.14+20260831.2",
        ):
            status, session = self.request(
                "/api/v1/dps/session/start",
                method="POST",
                body={
                    "client_id": client_id,
                    "card_key": card_key,
                    "app_version": app_version,
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(session["authorized"])
            sessions.append(session)

        for session in sessions:
            status, heartbeat = self.request(
                "/api/v1/dps/session/heartbeat",
                method="POST",
                token=session["access_token"],
                body={"using": True},
            )
            self.assertEqual(status, 200)
            self.assertTrue(heartbeat["authorized"])

        with monitor.database() as connection:
            active_versions = connection.execute(
                """
                SELECT app_version FROM sessions
                WHERE client_id=? AND ended_at IS NULL
                ORDER BY app_version
                """,
                (client_id,),
            ).fetchall()
        self.assertEqual(
            [row["app_version"] for row in active_versions],
            ["0.0.14+20260831.1", "0.0.14+20260831.2"],
        )

    def test_new_version_takes_over_same_device_without_rebinding_card(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        client_id = "7" * 32
        status, old_session = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={
                "client_id": client_id,
                "card_key": card_key,
                "app_version": "0.0.1",
            },
        )
        self.assertEqual(status, 200)

        with monitor.database() as connection:
            card_before = connection.execute(
                """
                SELECT activated_at, expires_at, bound_client_id, last_bound_at
                FROM cards WHERE card_hash=?
                """,
                (monitor.token_digest(card_key),),
            ).fetchone()

        status, new_session = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={
                "client_id": client_id,
                "card_key": card_key,
                "app_version": "0.0.2",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(new_session["authorized"])
        self.assertNotEqual(new_session["session_id"], old_session["session_id"])

        status, old_heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=old_session["access_token"],
            body={"using": True},
        )
        self.assertEqual(status, 403)
        self.assertEqual(old_heartbeat["error"], "invalid_session")

        status, new_heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=new_session["access_token"],
            body={"using": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(new_heartbeat["authorized"])

        with monitor.database() as connection:
            card_after = connection.execute(
                """
                SELECT activated_at, expires_at, bound_client_id, last_bound_at
                FROM cards WHERE card_hash=?
                """,
                (monitor.token_digest(card_key),),
            ).fetchone()
            active_sessions = connection.execute(
                """
                SELECT session_id, app_version FROM sessions
                WHERE card_hash=? AND ended_at IS NULL
                """,
                (monitor.token_digest(card_key),),
            ).fetchall()
        self.assertEqual(card_after["activated_at"], card_before["activated_at"])
        self.assertEqual(card_after["expires_at"], card_before["expires_at"])
        self.assertEqual(card_after["bound_client_id"], client_id)
        self.assertEqual(card_after["last_bound_at"], card_before["last_bound_at"])
        self.assertEqual(len(active_sessions), 1)
        self.assertEqual(active_sessions[0]["session_id"], new_session["session_id"])
        self.assertEqual(active_sessions[0]["app_version"], "0.0.2")

    def test_new_version_cannot_take_over_an_active_other_device(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        status, old_session = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={
                "client_id": "6" * 32,
                "card_key": card_key,
                "app_version": "0.0.1",
            },
        )
        self.assertEqual(status, 200)

        status, denied = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={
                "client_id": "5" * 32,
                "card_key": card_key,
                "app_version": "0.0.2",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "card_in_use")
        self.assertIn("请先在原设备完全退出程序", denied["message"])

        status, heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=old_session["access_token"],
            body={"using": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(heartbeat["authorized"])

    def test_online_status_groups_by_card_and_uses_recent_character_name(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        status, first = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "1" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 200)
        status, heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=first["access_token"],
            body={"using": True, "character_name": "莫雪"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(heartbeat["authorized"])
        status, ended = self.request(
            "/api/v1/dps/session/end",
            method="POST",
            token=first["access_token"],
            body={"session_id": first["session_id"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(ended["ok"])

        # This aggregation test needs an immediate transfer; the production
        # default remains covered by the dedicated twelve-hour policy tests.
        with monitor.database() as connection:
            connection.execute(
                "UPDATE cards SET rebind_cooldown_seconds=0 WHERE card_key=?",
                (card_key,),
            )

        status, second = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "2" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 200)
        self.assertTrue(second["authorized"])

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        self.assertEqual(summary["logged_in"], 1)
        self.assertEqual(summary["using_now"], 0)
        self.assertEqual(summary["seen_24h"], 2)
        self.assertEqual(len(summary["sessions"]), 1)
        self.assertEqual(summary["sessions"][0]["card_key"], card_key)
        self.assertEqual(summary["sessions"][0]["client_id"], "2" * 32)
        self.assertEqual(summary["sessions"][0]["character_name"], "莫雪")
        self.assertTrue(summary["sessions"][0]["online"])

        status, ended = self.request(
            "/api/v1/dps/session/end",
            method="POST",
            token=second["access_token"],
            body={"session_id": second["session_id"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(ended["ok"])
        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        self.assertEqual(summary["sessions"], [])

    def test_card_add_time_unbind_and_expiration(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        status, started = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "c" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 200)
        token = started["access_token"]
        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        card_id = summary["cards"][0]["card_id"]

        status, extended = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={
                "card_id": card_id,
                "action": "add_time",
                "duration_seconds": 3600,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(extended["changed"])
        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(summary["cards"][0]["duration_seconds"], 10800)
        self.assertGreaterEqual(summary["cards"][0]["remaining_seconds"], 10798)

        status, unbound = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={"card_id": card_id, "action": "unbind"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(unbound["changed"])
        status, cooldown = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "d" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(cooldown["error"], "card_rebind_cooldown")
        self.assertIn("刚在另一台设备使用", cooldown["message"])

        with monitor.database() as connection:
            connection.execute(
                "UPDATE cards SET expires_at=? WHERE card_hash=?",
                (monitor.now_epoch() - 1, card_id),
            )
        status, expired = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=token,
            body={"using": True},
        )
        self.assertEqual(status, 403)
        self.assertIn(expired["error"], ("card_expired", "invalid_session"))
        status, expired_login = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "d" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(expired_login["error"], "card_expired")

    def test_card_type_generation_sold_flag_and_batch_add_time(self):
        cards = self.create_card(card_type="weekly", count=3, note="周卡")
        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        generated = [card for card in summary["cards"] if card["card_key"] in cards]
        self.assertEqual(len(generated), 3)
        self.assertTrue(all(card["note"] == "周卡" for card in generated))
        self.assertTrue(
            all(card["duration_seconds"] == 7 * 86400 for card in generated)
        )

        sold_card = generated[0]
        status, changed = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={
                "card_id": sold_card["card_id"],
                "action": "set_sold",
                "sold": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(changed["changed"])

        selected_ids = [card["card_id"] for card in generated[:2]]
        status, extended = self.request(
            "/api/v1/dps/admin/cards/batch-add-time",
            method="POST",
            admin=True,
            body={"card_ids": selected_ids, "duration_seconds": 2 * 3600},
        )
        self.assertEqual(status, 200)
        self.assertEqual(extended["changed"], 2)

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        refreshed = {card["card_id"]: card for card in summary["cards"]}
        self.assertTrue(refreshed[sold_card["card_id"]]["sold"])
        for card_id in selected_ids:
            self.assertEqual(
                refreshed[card_id]["duration_seconds"], 7 * 86400 + 2 * 3600
            )

    def test_partner_type_can_only_be_added_one_at_a_time_and_is_permanent(self):
        card_key = self.create_card(
            card_type="partner",
            count=1,
            custom_card_key="MoXueFriend2026",
            remark="内部永久卡",
        )[0]
        self.assertEqual(card_key, "moxuefriend2026")

        status, rejected = self.request(
            "/api/v1/dps/admin/cards/create",
            method="POST",
            admin=True,
            body={"card_type": "partner", "count": 2},
        )
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"], "partner_single_only")

        status, invalid = self.request(
            "/api/v1/dps/admin/cards/create",
            method="POST",
            admin=True,
            body={"card_type": "partner", "count": 1, "custom_card_key": "短"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"], "bad_custom_card_key")

        status, duplicate = self.request(
            "/api/v1/dps/admin/cards/create",
            method="POST",
            admin=True,
            body={
                "card_type": "partner",
                "count": 1,
                "custom_card_key": "MOXUEFRIEND2026",
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(duplicate["error"], "card_exists")

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        partner = next(
            card for card in summary["cards"] if card["card_key"] == card_key
        )
        self.assertTrue(partner["permanent"])
        self.assertEqual(partner["card_tier"], "partner")
        self.assertEqual(partner["duration_seconds"], 0)
        self.assertIsNone(partner["remaining_seconds"])
        self.assertEqual(partner["remark"], "内部永久卡")
        self.assertTrue(partner["deletable"])

        status, started = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "9" * 32, "card_key": card_key.upper()},
        )
        self.assertEqual(status, 200)
        self.assertEqual(started["card_tier"], "partner")
        self.assertEqual(started["expires_at"], 0)
        self.assertIsNone(started["remaining_seconds"])

        status, heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=started["access_token"],
            body={"using": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(heartbeat["card_tier"], "partner")
        self.assertEqual(heartbeat["expires_at"], 0)

        status, unchanged = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={
                "card_id": partner["card_id"],
                "action": "add_time",
                "duration_seconds": 3600,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(unchanged["changed"])

    def test_card_delete_is_logical_and_ends_active_session(self):
        card_key = self.create_card(
            card_type="weekly", remark="售予张三"
        )[0]
        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        card = next(item for item in summary["cards"] if item["card_key"] == card_key)
        self.assertEqual(card["remark"], "售予张三")
        self.assertTrue(card["deletable"])

        status, changed = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={
                "card_id": card["card_id"],
                "action": "set_remark",
                "remark": "已转给李四",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(changed["changed"])

        status, started = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "8" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 200)

        status, deleted = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={"card_id": card["card_id"], "action": "delete"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(deleted["changed"])

        with monitor.database() as connection:
            retained = connection.execute(
                "SELECT card_key, deleted_at FROM cards WHERE card_hash=?",
                (card["card_id"],),
            ).fetchone()
        self.assertIsNotNone(retained)
        self.assertEqual(retained["card_key"], card_key)
        self.assertGreater(float(retained["deleted_at"] or 0), 0)

        status, heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=started["access_token"],
            body={"using": True},
        )
        self.assertEqual(status, 403)
        self.assertEqual(heartbeat["error"], "invalid_session")

        status, rejected = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "8" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(rejected["error"], "card_invalid")

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertFalse(any(item["card_key"] == card_key for item in summary["cards"]))

        protected_id = next(
            item["card_id"]
            for item in summary["cards"]
            if item["card_key"] == monitor.PARTNER_CARD_KEY
        )
        status, protected = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={"card_id": protected_id, "action": "delete"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(protected["error"], "protected_card")

    def test_admin_page_has_large_card_table_and_visible_horizontal_scroll(self):
        self.assertIn('id="onlineCardSearch"', monitor.ADMIN_PAGE)
        self.assertIn('id="onlineCount"', monitor.ADMIN_PAGE)
        self.assertIn("function renderOnline()", monitor.ADMIN_PAGE)
        self.assertIn("allSessions=d.sessions||[]", monitor.ADMIN_PAGE)
        self.assertIn("byId('onlineCardSearch').oninput=renderOnline", monitor.ADMIN_PAGE)
        self.assertIn('id="cardScrollTop"', monitor.ADMIN_PAGE)
        self.assertIn('id="cardTableWrap"', monitor.ADMIN_PAGE)
        self.assertIn(".cards-table{min-width:1980px}", monitor.ADMIN_PAGE)
        self.assertIn('name="card_type"', monitor.ADMIN_PAGE)
        self.assertIn('<option value="partner">莫雪的小伙伴（永久）</option>', monitor.ADMIN_PAGE)
        self.assertIn('id="customCardKey"', monitor.ADMIN_PAGE)
        self.assertIn('id="createCardRemark"', monitor.ADMIN_PAGE)
        self.assertIn('data-action="delete"', monitor.ADMIN_PAGE)
        self.assertIn("历史记录仍会保留", monitor.ADMIN_PAGE)
        self.assertIn("partner?'单个新增':'批量生成'", monitor.ADMIN_PAGE)
        self.assertIn('id="batchAddTime"', monitor.ADMIN_PAGE)
        self.assertIn('id="cardSoldFilter"', monitor.ADMIN_PAGE)
        self.assertIn('id="prevCardPage"', monitor.ADMIN_PAGE)
        self.assertIn("const CARD_PAGE_SIZE=100", monitor.ADMIN_PAGE)
        self.assertIn("if(!byId('cardsPanel').hidden)renderCards()", monitor.ADMIN_PAGE)
        self.assertIn('id="feedbackTab" type="button">反馈', monitor.ADMIN_PAGE)
        self.assertIn('data-feedback="\'+esc(x.feedback_id)+\'">查看</button>', monitor.ADMIN_PAGE)
        self.assertNotIn('id="feedbackStatusFilter"', monitor.ADMIN_PAGE)
        self.assertNotIn(".dataset.status", monitor.ADMIN_PAGE)
        self.assertNotIn("/api/v1/dps/admin/feedback/update", monitor.ADMIN_PAGE)

    def test_feedback_submission_and_admin_review_without_processing_status(self):
        card_key = self.create_card()[0]
        status, started = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={
                "client_id": "9" * 32,
                "card_key": card_key,
                "app_version": "0.0.2",
            },
        )
        self.assertEqual(status, 200)
        token = started["access_token"]

        status, missing = self.request(
            "/api/v1/dps/feedback",
            method="POST",
            token=token,
            body={"category": "dps", "content": ""},
        )
        self.assertEqual(status, 400)
        self.assertEqual(missing["error"], "feedback_content_required")

        status, submitted = self.request(
            "/api/v1/dps/feedback",
            method="POST",
            token=token,
            body={
                "category": "dps",
                "content": "进入战斗后没有 DPS。\n普通攻击也没有数据。",
                "character_name": "莫雪",
                "app_version": "0.0.2",
                "diagnostics": {
                    "boss_only": True,
                    "full_combat_snapshot": "combat-data|" * 10_000,
                    "capture_pipeline": {
                        "stage": "capturing",
                        "parsed_damage_events": 0,
                    },
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(submitted["ok"])
        feedback_id = submitted["feedback_id"]
        self.assertRegex(feedback_id, r"^FB[0-9A-F]{16}$")

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        self.assertEqual(summary["feedback_total"], 1)
        self.assertNotIn("feedback_pending", summary)
        self.assertNotIn("new_feedback", summary)

        status, listed = self.request(
            "/api/v1/dps/admin/feedback", admin=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(listed["total"], 1)
        self.assertNotIn("pending", listed)
        self.assertEqual(listed["feedbacks"][0]["feedback_id"], feedback_id)
        self.assertEqual(listed["feedbacks"][0]["card_key"], card_key)
        self.assertEqual(listed["feedbacks"][0]["character_name"], "莫雪")
        self.assertTrue(listed["feedbacks"][0]["has_diagnostics"])
        self.assertNotIn("status", listed["feedbacks"][0])

        status, detail = self.request(
            "/api/v1/dps/admin/feedback/detail",
            method="POST",
            admin=True,
            body={"feedback_id": feedback_id},
        )
        self.assertEqual(status, 200)
        feedback = detail["feedback"]
        self.assertIn("普通攻击", feedback["content"])
        self.assertTrue(feedback["diagnostics"]["boss_only"])
        self.assertGreater(
            len(feedback["diagnostics"]["full_combat_snapshot"]),
            80_000,
        )
        self.assertNotIn("status", feedback)
        self.assertEqual(
            feedback["diagnostics"]["capture_pipeline"]["stage"],
            "capturing",
        )

        status, removed_route = self.request(
            "/api/v1/dps/admin/feedback/update",
            method="POST",
            admin=True,
            body={"feedback_id": feedback_id, "status": "resolved"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(removed_route["error"], "not_found")

    def test_feedback_requires_an_active_session(self):
        status, denied = self.request(
            "/api/v1/dps/feedback",
            method="POST",
            body={"category": "other", "content": "无法使用"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "invalid_session")

    def test_anonymous_diagnostic_links_recent_device_session(self):
        card_key = self.create_card()[0]
        client_id = "d" * 32
        status, started = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={
                "client_id": client_id,
                "card_key": card_key,
                "app_version": "0.0.13+20260831.5",
            },
        )
        self.assertEqual(status, 200)
        status, heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=started["access_token"],
            body={
                "using": True,
                "character_name": "芒果凤梨",
                "game_pid": 9784,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(heartbeat["authorized"])

        diagnostic_body = {
            "tool_name": monitor.DIAGNOSTIC_TOOL_NAME,
            "tool_version": "1.0.0+20260831.1",
            "client_id": client_id,
            "diagnostics": {
                "schema_version": 1,
                "assessment": {"code": "native_hook_silent"},
                "capture": {
                    "network_damage_messages": 18,
                    "native_damage_records": 0,
                },
            },
        }
        self.assertNotIn("card_key", diagnostic_body)
        status, submitted = self.request(
            "/api/v1/dps/diagnostic",
            method="POST",
            body=diagnostic_body,
        )
        self.assertEqual(status, 200)
        self.assertTrue(submitted["ok"])
        diagnostic_id = submitted["diagnostic_id"]
        self.assertRegex(diagnostic_id, r"^DG[0-9A-F]{16}$")

        status, listed = self.request(
            "/api/v1/dps/admin/feedback", admin=True
        )
        self.assertEqual(status, 200)
        report = listed["feedbacks"][0]
        self.assertEqual(report["feedback_id"], diagnostic_id)
        self.assertEqual(report["category"], "diagnostic")
        self.assertEqual(report["category_label"], "问题检测")
        self.assertEqual(report["client_id"], client_id)
        self.assertEqual(report["card_key"], card_key)
        self.assertEqual(report["character_name"], "芒果凤梨")
        self.assertEqual(report["app_version"], "0.0.13+20260831.5")

        status, detail = self.request(
            "/api/v1/dps/admin/feedback/detail",
            method="POST",
            admin=True,
            body={"feedback_id": diagnostic_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            detail["feedback"]["diagnostics"]["assessment"]["code"],
            "native_hook_silent",
        )

    def test_anonymous_diagnostic_rejects_invalid_payload(self):
        status, invalid_tool = self.request(
            "/api/v1/dps/diagnostic",
            method="POST",
            body={
                "tool_name": "unknown",
                "client_id": "a" * 32,
                "diagnostics": {},
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid_tool["error"], "invalid_diagnostic_tool")

        status, missing_report = self.request(
            "/api/v1/dps/diagnostic",
            method="POST",
            body={"tool_name": monitor.DIAGNOSTIC_TOOL_NAME},
        )
        self.assertEqual(status, 400)
        self.assertEqual(missing_report["error"], "diagnostics_required")

    def test_diagnostic_uploader_round_trip(self):
        submission = submit_diagnostic_report(
            self.base_url,
            "c" * 32,
            {
                "schema_version": 1,
                "assessment": {"code": "network_hook_silent"},
            },
            timeout=2,
        )

        self.assertRegex(submission.diagnostic_id, r"^DG[0-9A-F]{16}$")
        self.assertEqual(submission.message, "检测报告已上传。")

    def test_non_ascii_invalid_card_returns_json_instead_of_closing_connection(self):
        status, denied = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "8" * 32, "card_key": "中文无效卡号！"},
        )
        self.assertEqual(status, 403)
        self.assertFalse(denied["authorized"])
        self.assertEqual(denied["error"], "card_invalid")

    def test_admin_page_contains_feedback_review_without_status_controls(self):
        self.assertIn('id="feedbackTab"', monitor.ADMIN_PAGE)
        self.assertIn('id="feedbackPanel"', monitor.ADMIN_PAGE)
        self.assertIn('id="feedbackModal"', monitor.ADMIN_PAGE)
        self.assertIn("/api/v1/dps/admin/feedback/detail", monitor.ADMIN_PAGE)
        self.assertNotIn("/api/v1/dps/admin/feedback/update", monitor.ADMIN_PAGE)
        self.assertNotIn(".dataset.status", monitor.ADMIN_PAGE)
        self.assertNotIn('id="feedbackStatusFilter"', monitor.ADMIN_PAGE)
        self.assertNotIn('id="toggleFeedbackStatus"', monitor.ADMIN_PAGE)
        self.assertNotIn("待处理", monitor.ADMIN_PAGE)
        self.assertNotIn("已处理", monitor.ADMIN_PAGE)

    def test_admin_rejects_malformed_basic_auth(self):
        status, value = self.request(
            "/api/v1/dps/admin/status", authorization="Basic !!!"
        )
        self.assertEqual(status, 401)
        self.assertIsNone(value)

    def test_client_gateway_card_login_and_heartbeat(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        gateway = ServerLicensingGateway(
            self.base_url, "e" * 32, "0.0.1", timeout=2
        )
        licensing = LicensingService(gateway)

        rejected = licensing.sign_in_card("GMZZAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertFalse(rejected.active)
        self.assertEqual(
            rejected.display_name,
            "卡号不存在或输入有误，请检查后重试。",
        )

        session = licensing.sign_in_card(card_key)
        self.assertTrue(session.active)
        self.assertIsNotNone(session.expires_at)
        self.assertEqual(session.card_tier, "normal")
        heartbeat = licensing.heartbeat(
            using=True, character_name="莫雪", game_pid=1234
        )
        self.assertTrue(heartbeat.authorized)
        self.assertIsNotNone(heartbeat.expires_at)
        feedback = licensing.submit_feedback(
            category="dps",
            content="测试反馈提交",
            character_name="莫雪",
            diagnostics={"capture_pipeline": {"stage": "capturing"}},
        )
        self.assertTrue(feedback.accepted)
        self.assertRegex(feedback.feedback_id, r"^FB[0-9A-F]{16}$")
        licensing.sign_out()

    def test_shared_combat_clock_unifies_two_clients_and_splits_next_pull(self):
        first_card, second_card = self.create_card(count=2)
        first = LicensingService(
            ServerLicensingGateway(
                self.base_url, "1" * 32, "0.0.14+clock-test", timeout=2
            )
        )
        second = LicensingService(
            ServerLicensingGateway(
                self.base_url, "2" * 32, "0.0.14+clock-test", timeout=2
            )
        )
        self.assertTrue(first.sign_in_card(first_card).active)
        self.assertTrue(second.sign_in_card(second_card).active)
        common = {
            "party_key": "a" * 64,
            "target_key": "b" * 64,
            "state": "active",
            "end_age_seconds": 0.0,
            "activity_age_seconds": 0.0,
        }

        first_active = first.sync_combat_clock(
            {
                **common,
                "encounter_id": "first-run-000001",
                "client_revision": 1,
                "elapsed_seconds": 12.0,
                "total_damage": 1_000_000,
            }
        )
        second_active = second.sync_combat_clock(
            {
                **common,
                "encounter_id": "second-run-000001",
                "client_revision": 1,
                "elapsed_seconds": 13.0,
                "total_damage": 1_000_000,
            }
        )
        self.assertTrue(first_active.synchronized)
        self.assertEqual(first_active.clock_id, second_active.clock_id)
        self.assertFalse(second_active.final)

        first_final = first.sync_combat_clock(
            {
                **common,
                "encounter_id": "first-run-000001",
                "state": "final",
                "client_revision": 2,
                "elapsed_seconds": 25.0,
                "end_age_seconds": 0.0,
                "total_damage": 5_000_000,
            }
        )
        second_final = second.sync_combat_clock(
            {
                **common,
                "encounter_id": "second-run-000001",
                "state": "final",
                "client_revision": 2,
                "elapsed_seconds": 26.0,
                "end_age_seconds": 0.0,
                "total_damage": 5_000_000,
            }
        )
        self.assertFalse(first_final.final)
        self.assertTrue(second_final.final)
        self.assertEqual(first_final.clock_id, second_final.clock_id)
        first_final_retry = first.sync_combat_clock(
            {
                **common,
                "encounter_id": "first-run-000001",
                "state": "final",
                "client_revision": 2,
                "elapsed_seconds": 25.0,
                "end_age_seconds": 0.0,
                "total_damage": 5_000_000,
            }
        )
        self.assertTrue(first_final_retry.final)
        self.assertEqual(
            first_final_retry.duration_seconds,
            second_final.duration_seconds,
        )
        self.assertEqual(first_final_retry.started_at, second_final.started_at)
        self.assertEqual(first_final_retry.ended_at, second_final.ended_at)

        next_pull = first.sync_combat_clock(
            {
                **common,
                "encounter_id": "first-run-000002",
                "client_revision": 1,
                "elapsed_seconds": 1.0,
                "total_damage": 10_000,
            }
        )
        self.assertTrue(next_pull.synchronized)
        self.assertNotEqual(first_final.clock_id, next_pull.clock_id)

    def test_shared_combat_clock_requires_an_authorized_session(self):
        status, value = self.request(
            "/api/v1/dps/combat/clock",
            method="POST",
            body={
                "party_key": "a" * 64,
                "target_key": "b" * 64,
                "encounter_id": "client-run-000001",
                "state": "active",
                "elapsed_seconds": 10.0,
                "end_age_seconds": 0.0,
                "total_damage": 100,
            },
        )
        self.assertEqual(status, 403)
        self.assertFalse(value["authorized"])

    def test_v2_combat_clock_recovers_final_when_higher_revision_is_active(self):
        first_card, second_card = self.create_card(count=2)
        first = LicensingService(
            ServerLicensingGateway(
                self.base_url, "3" * 32, "0.1.8+clock-v2", timeout=2
            )
        )
        second = LicensingService(
            ServerLicensingGateway(
                self.base_url, "4" * 32, "0.1.8+clock-v2", timeout=2
            )
        )
        self.assertTrue(first.sign_in_card(first_card).active)
        self.assertTrue(second.sign_in_card(second_card).active)
        common = {
            "party_key": "c" * 64,
            "target_key": "d" * 64,
            "end_age_seconds": 0.0,
            "activity_age_seconds": 0.0,
        }

        def report(
            service,
            encounter_id: str,
            revision: int,
            state: str,
            elapsed: float,
            total: int,
        ):
            return service.sync_combat_clock(
                {
                    **common,
                    "encounter_id": encounter_id,
                    "client_revision": revision,
                    "state": state,
                    "elapsed_seconds": elapsed,
                    "total_damage": total,
                    "end_reason": (
                        "game_server_settlement" if state == "final" else ""
                    ),
                }
            )

        first_active = report(first, "recover-a-000001", 1, "active", 10, 1_000)
        second_active = report(second, "recover-b-000001", 1, "active", 11, 1_000)
        self.assertEqual(first_active.clock_id, second_active.clock_id)
        self.assertFalse(report(first, "recover-a-000001", 2, "final", 20, 5_000).final)
        initial_final = report(
            second, "recover-b-000001", 2, "final", 21, 5_000
        )
        self.assertTrue(initial_final.final)

        recovered = report(
            first, "recover-a-000001", 3, "active", 30, 6_000
        )
        self.assertEqual(recovered.clock_id, initial_final.clock_id)
        self.assertEqual(recovered.state, "active")
        self.assertFalse(recovered.final)
        self.assertGreater(recovered.revision, initial_final.revision)
        second_recovered = report(
            second, "recover-b-000001", 3, "active", 31, 6_000
        )
        self.assertFalse(second_recovered.final)
        self.assertFalse(
            report(first, "recover-a-000001", 4, "final", 40, 8_000).final
        )
        recovered_final = report(
            second, "recover-b-000001", 4, "final", 41, 8_000
        )
        self.assertTrue(recovered_final.final)
        self.assertEqual(recovered_final.clock_id, initial_final.clock_id)
        self.assertEqual(recovered_final.total_damage, 8_000)

    def test_weekly_card_tier_is_returned_to_client(self):
        card_key = self.create_card(
            duration_seconds=7 * 86400, note="周卡"
        )[0]
        gateway = ServerLicensingGateway(
            self.base_url, "f" * 32, "0.0.1", timeout=2
        )
        licensing = LicensingService(gateway)
        session = licensing.sign_in_card(card_key)
        self.assertTrue(session.active)
        self.assertEqual(session.card_tier, "weekly")
        heartbeat = licensing.heartbeat(using=True, character_name="莫雪")
        self.assertEqual(heartbeat.card_tier, "weekly")

    def test_partner_card_is_revoked_when_device_changes(self):
        card_key = monitor.PARTNER_CARD_KEY
        self.assertEqual(card_key, "partner-test-key")
        first_client = "1" * 32
        status, started = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": first_client, "card_key": card_key},
        )
        self.assertEqual(status, 200)
        self.assertTrue(started["authorized"])
        self.assertEqual(started["card_tier"], "partner")
        self.assertEqual(started["expires_at"], 0)
        self.assertIsNone(started["remaining_seconds"])

        status, heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=started["access_token"],
            body={"using": True, "character_name": "莫雪"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(heartbeat["authorized"])
        self.assertEqual(heartbeat["card_tier"], "partner")
        self.assertEqual(heartbeat["expires_at"], 0)

        status, denied = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "2" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "partner_device_changed")

        status, old_session = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=started["access_token"],
            body={"using": True},
        )
        self.assertEqual(status, 403)
        self.assertEqual(old_session["error"], "invalid_session")

        status, denied = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "2" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "card_revoked")

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        partner = next(
            card for card in summary["cards"] if card["card_key"] == card_key
        )
        self.assertTrue(partner["permanent"])
        self.assertEqual(partner["card_tier"], "partner")
        self.assertEqual(partner["state"], "revoked")
        self.assertEqual(partner["bound_client_id"], first_client)
        self.assertEqual(
            partner["rebind_cooldown_seconds"],
            monitor.CARD_REBIND_COOLDOWN_SECONDS,
        )

    def test_request_connections_do_not_reapply_wal_mode(self):
        statements: list[str] = []
        original_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(monitor.sqlite3, "connect", side_effect=traced_connect):
            with monitor.database() as connection:
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

        normalized = [statement.casefold() for statement in statements]
        self.assertTrue(any("pragma busy_timeout" in item for item in normalized))
        self.assertFalse(any("pragma journal_mode" in item for item in normalized))

    def test_write_database_serializes_concurrent_transactions(self):
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0

        def hold_transaction(index: int) -> None:
            nonlocal active, maximum_active
            with monitor.write_database() as connection:
                with state_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    connection.execute("SELECT ?", (index,)).fetchone()
                    time.sleep(0.01)
                finally:
                    with state_lock:
                        active -= 1

        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(hold_transaction, range(36)))

        self.assertEqual(maximum_active, 1)

    def test_combat_clock_cleanup_runs_at_most_once_per_minute(self):
        timestamp = 2_000_000_000.0
        old_timestamp = timestamp - monitor.COMBAT_CLOCK_RETENTION_SECONDS - 1
        monitor._COMBAT_CLOCK_CLEANUP_STATE = None

        def insert_expired_rows(suffix: str) -> None:
            with monitor.write_database() as connection:
                connection.execute(
                    """
                    INSERT INTO combat_clocks(
                        clock_id, party_key, target_key, started_at,
                        created_at, last_seen
                    ) VALUES (?, 'party', 'target', ?, ?, ?)
                    """,
                    (f"v1-{suffix}", old_timestamp, old_timestamp, old_timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO combat_clocks_v2(
                        clock_id, party_key, target_key, started_at,
                        created_at, last_seen
                    ) VALUES (?, 'party', 'target', ?, ?, ?)
                    """,
                    (f"v2-{suffix}", old_timestamp, old_timestamp, old_timestamp),
                )

        insert_expired_rows("first")
        with monitor.write_database() as connection:
            self.assertTrue(
                monitor.cleanup_expired_combat_clocks(connection, timestamp)
            )

        insert_expired_rows("second")
        with monitor.write_database() as connection:
            self.assertFalse(
                monitor.cleanup_expired_combat_clocks(connection, timestamp + 1)
            )
            remaining = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM combat_clocks) AS v1_count,
                    (SELECT COUNT(*) FROM combat_clocks_v2) AS v2_count
                """
            ).fetchone()
        self.assertEqual((remaining["v1_count"], remaining["v2_count"]), (1, 1))

        with monitor.write_database() as connection:
            self.assertTrue(
                monitor.cleanup_expired_combat_clocks(connection, timestamp + 60)
            )
            remaining = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM combat_clocks) AS v1_count,
                    (SELECT COUNT(*) FROM combat_clocks_v2) AS v2_count
                """
            ).fetchone()
        self.assertEqual((remaining["v1_count"], remaining["v2_count"]), (0, 0))

    def test_sqlite_busy_returns_json_503_with_retry_after(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        status, session = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "6" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 200)

        @contextmanager
        def busy_write_database():
            raise sqlite3.OperationalError("database is locked")
            yield  # pragma: no cover

        request = Request(
            self.base_url + "/api/v1/dps/session/heartbeat",
            data=json.dumps({"using": True}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {session['access_token']}",
            },
            method="POST",
        )
        with mock.patch.object(
            monitor, "write_database", side_effect=busy_write_database
        ):
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3.0)
        error = raised.exception
        try:
            payload = json.loads(error.read().decode("utf-8"))
            self.assertEqual(error.code, HTTPStatus.SERVICE_UNAVAILABLE)
            self.assertEqual(error.headers.get("Retry-After"), "1")
        finally:
            error.close()
        self.assertEqual(payload["error"], "server_busy")
        self.assertEqual(payload["retry_after"], 1)

    def test_concurrent_heartbeat_and_combat_clock_writes_remain_available(self):
        card_key = self.create_card(duration_seconds=7200)[0]
        status, session = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "7" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 200)
        token = session["access_token"]

        def make_request(index: int) -> tuple[int, dict | None]:
            operation = index % 3
            if operation == 0:
                return self.request(
                    "/api/v1/dps/session/heartbeat",
                    method="POST",
                    token=token,
                    body={"using": True, "character_name": "并发测试"},
                )
            common = {
                "party_key": "e" * 64,
                "target_key": "f" * 64,
                "encounter_id": f"load-run-{index:06d}",
                "state": "active",
                "elapsed_seconds": 10.0 + (index % 5),
                "end_age_seconds": 0.0,
                "total_damage": 100_000 + index,
            }
            if operation == 1:
                return self.request(
                    "/api/v1/dps/combat/clock",
                    method="POST",
                    token=token,
                    body=common,
                )
            return self.request(
                "/api/v2/dps/combat/clock",
                method="POST",
                token=token,
                body={
                    **common,
                    "activity_age_seconds": 0.0,
                    "client_revision": 1,
                },
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(make_request, range(90)))

        self.assertEqual(len(results), 90)
        self.assertTrue(all(status == 200 for status, _value in results))
        self.assertTrue(all(value and value["ok"] for _status, value in results))

    def test_client_gateway_rejects_non_loopback_http(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            ServerLicensingGateway(
                "http://example.invalid", "e" * 32, "0.0.1", timeout=2
            )


if __name__ == "__main__":
    unittest.main()
