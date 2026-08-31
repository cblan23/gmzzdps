#!/usr/bin/env python3
"""Privacy-bounded capture analysis and anonymous diagnostic upload."""

from __future__ import annotations

import json
import locale
import os
import platform
import socket
import ssl
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from network_state import NetworkPacketParser


TOOL_NAME = "叨叨诡秘问题检测工具"
TOOL_VERSION = "1.0.1+20260831.2"
DIAGNOSTIC_SCHEMA_VERSION = 1
MAX_REPORTED_METHODS = 80
MAX_REPORTED_ERRORS = 20
MAX_REPORTED_BOSSES = 24
MAX_UPLOAD_BYTES = 224 * 1024


class DiagnosticUploadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiagnosticSubmission:
    diagnostic_id: str
    message: str


def _last_error_line(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("details", "")
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:900]


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_bool(value: object) -> bool:
    return bool(value)


class DiagnosticAnalyzer:
    """Aggregate capture behavior without retaining raw RPC payloads."""

    def __init__(
        self,
        boss_template_catalog: dict[str, dict] | None = None,
        boss_name_allowlist: tuple[str, ...] = (),
    ):
        self.catalog = boss_template_catalog or {}
        self.parser = NetworkPacketParser(
            boss_template_catalog=self.catalog,
            boss_name_allowlist=boss_name_allowlist,
        )
        self.started_at = time.time()
        self.finished_at = 0.0
        self.stages: list[str] = []
        self.errors: list[dict[str, str]] = []
        self.connected_payload: dict[str, object] = {}
        self.process_payload: dict[str, object] = {}
        self.method_counts: Counter[str] = Counter()
        self.parser_update_counts: Counter[str] = Counter()
        self.network_records = 0
        self.decoded_network_records = 0
        self.network_damage_messages = 0
        self.network_damage_positive = 0
        self.network_damage_suppressed_by_native = 0
        self.native_damage_records = 0
        self.native_positive_damage_records = 0
        self.native_boss_records = 0
        self.native_name_records = 0
        self.native_skill_name_records = 0
        self.capture_batches = 0
        self.sequence_gaps = 0
        self.parsed_damage_events = 0
        self.forwarded_damage_events = 0
        self.filtered_damage_events = 0
        self.team_stat_updates = 0
        self.team_status: dict[str, int] = {}
        self.bosses: dict[tuple[int, str], dict[str, object]] = {}

    def _stage(self, value: object) -> None:
        stage = str(value or "").strip()[:64]
        if stage and (not self.stages or self.stages[-1] != stage):
            self.stages.append(stage)
            del self.stages[:-32]

    def _error(self, component: object, details: object) -> None:
        detail = _last_error_line(details)
        if not detail:
            return
        self.errors.append(
            {
                "component": str(component or "capture")[:64],
                "detail": detail,
            }
        )
        del self.errors[:-MAX_REPORTED_ERRORS]

    def _remember_boss(self, payload: dict) -> None:
        entity_type = str(payload.get("entity_type", "")).casefold()
        boss_type = _safe_int(payload.get("boss_type"), -1)
        if entity_type != "boss" and boss_type != 3:
            return
        template_id = _safe_int(payload.get("template_id"))
        name = str(payload.get("name", "")).strip()[:64]
        key = (template_id, name)
        self.bosses[key] = {
            "template_id": template_id,
            "name": name,
            "boss_type": boss_type,
            "source": str(payload.get("boss_source", ""))[:64],
        }
        while len(self.bosses) > MAX_REPORTED_BOSSES:
            self.bosses.pop(next(iter(self.bosses)))

    def _consume_updates(self, updates: list[tuple[str, dict]]) -> None:
        for kind, payload in updates:
            self.parser_update_counts[str(kind)] += 1
            if not isinstance(payload, dict):
                continue
            if kind == "event":
                self.parsed_damage_events += 1
                if self.parser.should_forward_damage_event(payload):
                    self.forwarded_damage_events += 1
                else:
                    self.filtered_damage_events += 1
            elif kind == "team_stat":
                self.team_stat_updates += 1
            elif kind == "profile":
                self._remember_boss(payload)

    @staticmethod
    def _positive_network_damage(record: dict) -> bool:
        if str(record.get("method", "")) != "OnMsgDamageSyncV2":
            return False
        args = NetworkPacketParser._args(record)
        if len(args) < 9:
            return False
        try:
            return bool(int(args[0]) and int(args[1]) and int(args[7]) > 0)
        except (TypeError, ValueError, OverflowError):
            return False

    def _enrich_native_boss(self, record: dict) -> dict:
        enriched = dict(record)
        template_id = _safe_int(enriched.get("template_id"))
        metadata = self.catalog.get(str(template_id), {})
        if not isinstance(metadata, dict):
            return enriched
        name = str(metadata.get("name", "")).strip()
        if name:
            enriched["name"] = name
        if metadata.get("level") not in (None, ""):
            enriched["level"] = metadata["level"]
        return enriched

    def _handle_batch(self, payload: dict) -> None:
        self.capture_batches += 1
        records = [item for item in payload.get("records", []) if isinstance(item, dict)]
        native_records = [
            item for item in payload.get("native_records", []) if isinstance(item, dict)
        ]
        native_boss_records = [
            item
            for item in payload.get("native_boss_records", [])
            if isinstance(item, dict)
        ]
        native_name_records = [
            item
            for item in payload.get("native_name_records", [])
            if isinstance(item, dict)
        ]
        native_skill_name_records = [
            item
            for item in payload.get("native_skill_name_records", [])
            if isinstance(item, dict)
        ]
        native_active = bool(payload.get("native_damage_hook_installed"))
        self.network_records += len(records)
        self.native_damage_records += len(native_records)
        self.native_boss_records += len(native_boss_records)
        self.native_name_records += len(native_name_records)
        self.native_skill_name_records += len(native_skill_name_records)
        self.sequence_gaps += len(payload.get("sequence_gaps", []) or [])

        for record in native_boss_records:
            try:
                self._consume_updates(
                    self.parser.process_native_boss_type(
                        self._enrich_native_boss(record)
                    )
                )
            except Exception as exc:
                self._error("native_boss_parser", exc)
        for record in native_name_records:
            try:
                update = self.parser.apply_runtime_boss_name(record)
                if update:
                    self._consume_updates([update])
            except Exception as exc:
                self._error("native_name_parser", exc)
        for record in native_records:
            if _safe_int(record.get("damage", record.get("arg9_i32", 0))) > 0:
                self.native_positive_damage_records += 1
            try:
                self._consume_updates(self.parser.process_native_damage(record))
            except Exception as exc:
                self._error("native_damage_parser", exc)

        for record in records:
            method = str(record.get("method", "")).strip()[:160] or "<empty>"
            self.method_counts[method] += 1
            if isinstance(record.get("decoded_arguments"), list):
                self.decoded_network_records += 1
            positive_damage = self._positive_network_damage(record)
            if method == "OnMsgDamageSyncV2":
                self.network_damage_messages += 1
            if positive_damage:
                self.network_damage_positive += 1
                if native_active:
                    self.network_damage_suppressed_by_native += 1
            try:
                self._consume_updates(
                    self.parser.process(record, include_damage=not native_active)
                )
            except Exception as exc:
                self._error("network_parser", exc)

        team_status = payload.get("team_status")
        if isinstance(team_status, dict):
            self.team_status = {
                "request_count": _safe_int(team_status.get("request_count")),
                "last_result": _safe_int(team_status.get("last_result")),
                "last_request_filetime": _safe_int(
                    team_status.get("last_request_filetime")
                ),
            }

    def handle(self, kind: str, payload: object = None) -> None:
        kind = str(kind or "")
        if kind == "process_started" and isinstance(payload, dict):
            self.process_payload = {
                "capture_process_pid": _safe_int(payload.get("pid")),
                "priority_class": str(payload.get("priority_class", ""))[:32],
                "priority_applied": _safe_bool(payload.get("priority_applied")),
                "main_thread_priority_applied": _safe_bool(
                    payload.get("main_thread_priority_applied")
                ),
            }
        elif kind == "state" and isinstance(payload, dict):
            self._stage(payload.get("stage"))
        elif kind == "connected" and isinstance(payload, dict):
            self._stage("capturing")
            keys = (
                "game_pid",
                "network_hook_adopted",
                "native_damage_hook_installed",
                "native_damage_hook_adopted",
                "native_name_hook_installed",
                "native_boss_type_hook_installed",
                "native_boss_init_hook_installed",
                "team_stats_hook_installed",
                "damage_source",
            )
            self.connected_payload = {key: payload.get(key) for key in keys}
        elif kind == "batch" and isinstance(payload, dict):
            self._handle_batch(payload)
        elif kind in {"diagnostic", "capture_error", "cleanup_error", "fatal"}:
            component = kind
            if isinstance(payload, dict):
                component = payload.get("component") or payload.get("stage") or kind
            self._error(component, payload)
            if kind in {"capture_error", "fatal"}:
                self._stage(
                    payload.get("stage", kind) if isinstance(payload, dict) else kind
                )
        elif kind == "session_closed" and isinstance(payload, dict):
            self._stage(payload.get("reason", "session_closed"))
        elif kind == "stopped":
            self._stage("stopped")

    def assessment(self) -> dict[str, str]:
        connected = bool(self.connected_payload)
        native_installed = bool(
            self.connected_payload.get("native_damage_hook_installed")
        )
        if not connected:
            code = (
                "network_hook_failed"
                if any("network" in item["component"] for item in self.errors)
                else "game_not_connected"
            )
            return {
                "code": code,
                "confidence": "high" if self.errors else "medium",
                "summary": "未建立游戏采集连接，请查看钩子错误。",
            }
        if (
            native_installed
            and self.network_damage_positive > 0
            and self.native_positive_damage_records == 0
            and self.network_damage_suppressed_by_native > 0
        ):
            return {
                "code": "native_hook_silent_network_fallback_blocked",
                "confidence": "high",
                "summary": "原生伤害入口已安装但没有产出，网络伤害回退同时被抑制。",
            }
        if self.forwarded_damage_events > 0:
            return {
                "code": "capture_pipeline_ok",
                "confidence": "high",
                "summary": "采集、解析与目标过滤均产生了可显示伤害。",
            }
        if self.parsed_damage_events > 0 and self.filtered_damage_events > 0:
            return {
                "code": "damage_target_not_confirmed",
                "confidence": "high",
                "summary": "已解析到伤害，但目标未被确认为 Boss 或木桩。",
            }
        if self.network_damage_messages > 0 or self.native_damage_records > 0:
            return {
                "code": "damage_decode_or_identity_failed",
                "confidence": "medium",
                "summary": "捕获到伤害入口，但没有形成可显示的伤害事件。",
            }
        if self.network_records > 0:
            return {
                "code": "no_damage_observed",
                "confidence": "medium",
                "summary": "网络采集正常，但检测期间没有观察到伤害消息。",
            }
        return {
            "code": "network_hook_silent",
            "confidence": "high",
            "summary": "采集连接已建立，但没有收到任何可用网络消息。",
        }

    def finish(self, environment: dict[str, object]) -> dict[str, object]:
        self.finished_at = time.time()
        methods = sorted(
            self.method_counts.items(), key=lambda item: (-item[1], item[0])
        )[:MAX_REPORTED_METHODS]
        connection = {
            key: (
                _safe_int(value)
                if key == "game_pid"
                else _safe_bool(value)
                if key.endswith("installed") or key.endswith("adopted")
                else str(value or "")[:32]
            )
            for key, value in self.connected_payload.items()
        }
        return {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "environment": dict(environment),
            "assessment": self.assessment(),
            "capture": {
                "duration_seconds": round(
                    max(0.0, self.finished_at - self.started_at), 3
                ),
                "stages": list(self.stages),
                "process": dict(self.process_payload),
                "connection": connection,
                "capture_batches": self.capture_batches,
                "network_records": self.network_records,
                "decoded_network_records": self.decoded_network_records,
                "network_methods": [
                    {"method": method, "count": count}
                    for method, count in methods
                ],
                "network_damage_messages": self.network_damage_messages,
                "network_damage_positive": self.network_damage_positive,
                "network_damage_suppressed_by_native": (
                    self.network_damage_suppressed_by_native
                ),
                "native_damage_records": self.native_damage_records,
                "native_positive_damage_records": (
                    self.native_positive_damage_records
                ),
                "native_boss_records": self.native_boss_records,
                "native_name_records": self.native_name_records,
                "native_skill_name_records": self.native_skill_name_records,
                "sequence_gaps": self.sequence_gaps,
                "parser_updates": dict(self.parser_update_counts),
                "parsed_damage_events": self.parsed_damage_events,
                "forwarded_damage_events": self.forwarded_damage_events,
                "filtered_damage_events": self.filtered_damage_events,
                "team_stat_updates": self.team_stat_updates,
                "team_status": dict(self.team_status),
                "bosses": list(self.bosses.values()),
                "errors": list(self.errors),
            },
            "privacy": {
                "raw_packets_uploaded": False,
                "card_number_uploaded": False,
                "player_names_uploaded": False,
                "local_paths_uploaded": False,
            },
        }


def diagnostic_environment(*, elevated: bool, packaged: bool) -> dict[str, object]:
    windows_release, windows_version, _csd, _ptype = platform.win32_ver()
    get_encoding = getattr(locale, "getencoding", None)
    locale_encoding = (
        get_encoding()
        if callable(get_encoding)
        else locale.getpreferredencoding(False)
    )
    return {
        "platform": sys.platform,
        "windows_release": windows_release,
        "windows_version": windows_version or platform.version(),
        "machine": platform.machine(),
        "python_bits": platform.architecture()[0],
        "locale_encoding": locale_encoding,
        "cpu_count": int(os.cpu_count() or 0),
        "elevated": bool(elevated),
        "packaged": bool(packaged),
    }


def submit_diagnostic_report(
    server_url: str,
    client_id: str,
    diagnostics: dict[str, object],
    *,
    ca_bundle_path: Path | None = None,
    timeout: float = 12.0,
) -> DiagnosticSubmission:
    server_url = str(server_url).strip().rstrip("/")
    parsed = urlsplit(server_url)
    loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "::1",
        "localhost",
    }
    if parsed.scheme != "https" and not loopback_http:
        raise DiagnosticUploadError("检测服务器地址无效。")
    payload = json.dumps(
        {
            "tool_name": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "client_id": str(client_id).strip().lower()[:80],
            "diagnostics": diagnostics,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise DiagnosticUploadError("检测报告过大，无法上传。")
    request = Request(
        server_url + "/api/v1/dps/diagnostic",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"GMZZ-Diagnostic/{TOOL_VERSION}",
        },
        method="POST",
    )
    context = None
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        if ca_bundle_path is not None and ca_bundle_path.is_file():
            context.load_verify_locations(cafile=str(ca_bundle_path))
    raw = b""
    for attempt in range(2):
        try:
            options: dict[str, object] = {"timeout": max(2.0, float(timeout))}
            if context is not None:
                options["context"] = context
            with urlopen(request, **options) as response:
                raw = response.read(64 * 1024)
            break
        except HTTPError as exc:
            try:
                value = json.loads(exc.read(64 * 1024).decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError):
                value = {}
            finally:
                exc.close()
            message = str(value.get("message", "")).strip()
            if not message:
                message = (
                    "上传过于频繁，请稍后重试。"
                    if exc.code == 429
                    else f"服务器返回 HTTP {exc.code}。"
                )
            raise DiagnosticUploadError(message) from exc
        except (URLError, OSError, TimeoutError, socket.timeout) as exc:
            if attempt == 0:
                time.sleep(0.25)
                continue
            raise DiagnosticUploadError("无法上传检测报告，请检查网络后重试。") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise DiagnosticUploadError("检测服务器响应无效。") from exc
    diagnostic_id = str(value.get("diagnostic_id", "")).strip()
    if not value.get("ok") or not diagnostic_id.startswith("DG"):
        raise DiagnosticUploadError(
            str(value.get("message", "")).strip() or "检测报告上传失败。"
        )
    return DiagnosticSubmission(
        diagnostic_id=diagnostic_id[:32],
        message=str(value.get("message", "")).strip() or "检测报告已上传。",
    )
