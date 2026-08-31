#!/usr/bin/env python3

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from diagnostic_report import submit_diagnostic_report
from licensing import LicensingService, ServerLicensingGateway
from server import dps_monitor_server as monitor


class QuietMonitorHandler(monitor.MonitorHandler):
    def log_message(self, format_string: str, *args) -> None:
        del format_string, args


class MonitorServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_partner_card_key = monitor.PARTNER_CARD_KEY
        monitor.PARTNER_CARD_KEY = "partner-test-key"
        monitor.DATABASE_PATH = Path(self.temporary.name) / "sessions.sqlite3"
        monitor.UPDATE_METADATA_PATH = Path(self.temporary.name) / "update.json"
        monitor.ADMIN_USER = "tester"
        monitor.ADMIN_PASSWORD = "test-password"
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

    def test_update_metadata_can_replace_an_older_same_version_build(self):
        update_bytes = b"MZ" + bytes(range(32))
        update_name = "dps-logs-v0.0.14.exe"
        update_path = monitor.UPDATE_METADATA_PATH.parent / update_name
        update_path.write_bytes(update_bytes)
        monitor.UPDATE_METADATA_PATH.write_text(
            json.dumps(
                {
                    "latest_version": "0.0.14",
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
                self.assertEqual(update["latest_version"], "0.0.14")
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

    def test_card_required_rebind_cooldown_and_admin_override(self):
        status, missing = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "a" * 32},
        )
        self.assertEqual(status, 403)
        self.assertEqual(missing["error"], "card_required")

        card_key = self.create_card(duration_seconds=7200, note="测试2小时卡")[0]
        status, first = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "a" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(first["remaining_seconds"], 7198)

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

        status, cooldown = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "b" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(cooldown["error"], "card_in_use")

        status, ended = self.request(
            "/api/v1/dps/session/end",
            method="POST",
            token=replacement["access_token"],
            body={"session_id": replacement["session_id"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(ended["ok"])

        status, cooldown = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "b" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(cooldown["error"], "card_rebind_cooldown")
        self.assertGreater(cooldown["retry_after"], 43000)

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        card_id = summary["cards"][0]["card_id"]
        status, changed = self.request(
            "/api/v1/dps/admin/cards/update",
            method="POST",
            admin=True,
            body={
                "card_id": card_id,
                "action": "set_rebind_cooldown",
                "cooldown_hours": 0,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(changed["changed"])

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

    def test_new_version_cannot_take_over_another_device(self):
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

    def test_card_remark_can_be_changed_and_delete_ends_active_session(self):
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

        status, heartbeat = self.request(
            "/api/v1/dps/session/heartbeat",
            method="POST",
            token=started["access_token"],
            body={"using": True},
        )
        self.assertEqual(status, 403)
        self.assertEqual(heartbeat["error"], "invalid_session")

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
        self.assertIn('id="cardScrollTop"', monitor.ADMIN_PAGE)
        self.assertIn('id="cardTableWrap"', monitor.ADMIN_PAGE)
        self.assertIn(".cards-table{min-width:1980px}", monitor.ADMIN_PAGE)
        self.assertIn('name="card_type"', monitor.ADMIN_PAGE)
        self.assertIn('<option value="partner">莫雪的小伙伴（永久）</option>', monitor.ADMIN_PAGE)
        self.assertIn('id="customCardKey"', monitor.ADMIN_PAGE)
        self.assertIn('id="createCardRemark"', monitor.ADMIN_PAGE)
        self.assertIn('data-action="delete"', monitor.ADMIN_PAGE)
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
        self.assertIn("不存在", rejected.display_name)

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

    def test_partner_card_is_permanent_but_keeps_device_binding(self):
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
        self.assertEqual(denied["error"], "card_in_use")

        status, ended = self.request(
            "/api/v1/dps/session/end",
            method="POST",
            token=started["access_token"],
            body={"session_id": started["session_id"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(ended["ok"])

        status, denied = self.request(
            "/api/v1/dps/session/start",
            method="POST",
            body={"client_id": "2" * 32, "card_key": card_key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "card_rebind_cooldown")
        self.assertGreater(denied["retry_after"], 43_000)

        status, summary = self.request("/api/v1/dps/admin/status", admin=True)
        self.assertEqual(status, 200)
        partner = next(
            card for card in summary["cards"] if card["card_key"] == card_key
        )
        self.assertTrue(partner["permanent"])
        self.assertEqual(partner["card_tier"], "partner")
        self.assertEqual(partner["state"], "active")
        self.assertEqual(partner["bound_client_id"], first_client)
        self.assertEqual(
            partner["rebind_cooldown_seconds"],
            monitor.CARD_REBIND_COOLDOWN_SECONDS,
        )

    def test_client_gateway_rejects_non_loopback_http(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            ServerLicensingGateway(
                "http://example.invalid", "e" * 32, "0.0.1", timeout=2
            )


if __name__ == "__main__":
    unittest.main()
