"""Authentication, entitlement, and server-presence boundary."""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import socket
import ssl
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from release_security import (
    BUILD_ID_PATTERN,
    CERTIFICATE_THUMBPRINT_PATTERN,
    verify_windows_publisher,
)
from runtime_capability import (
    RuntimeCapability,
    RuntimeCapabilityError,
    RuntimeCapabilityTimeError,
)


CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
FREE_TRIAL_END = datetime(2026, 9, 3, 23, 59, 59, tzinfo=CHINA_TIMEZONE)
DEFAULT_SERVER_URL = "https://daodaogame.vip"
REQUEST_TIMEOUT_SECONDS = 6.0
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 300.0
CLIENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TRIAL_CARD_PATTERN = re.compile(r"^GMZZ[A-HJ-NP-Z2-9]{26}$")
CA_BUNDLE_PATH = Path(__file__).resolve().with_name("cacert.pem")
CARD_LOGIN_FAILURE_MESSAGE = "登录失败，请稍后重试；若持续出现，请联系管理员。"
SYSTEM_TIME_SYNC_MESSAGE = (
    "本机时间异常，请在 Windows 设置中同步时间后重新登录。"
)
ROLLBACK_VERSION_PATTERN = re.compile(
    r"^(?P<numeric>\d+(?:\.\d+){2,3})(?P<suffix>[a-z]?)$",
    re.IGNORECASE,
)
MINIMUM_ROLLBACK_VERSION = (0, 1, 0)

SERVER_ERROR_MESSAGES = {
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
    "card_in_use": "已在原设备登录过",
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
    "forbidden": "登录失败，请稍后重试；若持续出现，请联系管理员。",
}


def china_now() -> datetime:
    return datetime.now(CHINA_TIMEZONE)


def normalize_rollback_version(value: object) -> str:
    """Return a canonical rollback release label, or an empty string."""

    text = str(value or "").strip()
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


@dataclass(frozen=True)
class LicenseSession:
    account_id: str = ""
    display_name: str = "未登录"
    access_token: str = ""
    expires_at: datetime | None = None
    entitlement: str = "none"
    session_id: str = ""
    card_tier: str = "normal"
    runtime_capability: RuntimeCapability | None = None

    @property
    def active(self) -> bool:
        if self.entitlement == "local":
            return True
        if not self.access_token:
            return False
        if self.entitlement == "server":
            return True
        if self.expires_at is None:
            return True
        now = china_now() if self.expires_at.tzinfo else datetime.now()
        return self.expires_at > now


@dataclass(frozen=True)
class CardRedemption:
    accepted: bool
    message: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class TrialClaim:
    accepted: bool
    message: str
    card_key: str = ""
    duration_seconds: int = 0
    next_available_at: datetime | None = None


@dataclass(frozen=True)
class HeartbeatResult:
    authorized: bool
    heartbeat_interval: int = 30
    message: str = ""
    expires_at: datetime | None = None
    card_tier: str = ""
    runtime_capability: RuntimeCapability | None = None


@dataclass(frozen=True)
class FeedbackResult:
    accepted: bool
    feedback_id: str = ""
    message: str = ""


@dataclass(frozen=True)
class CombatClockResult:
    synchronized: bool = False
    encounter_id: str = ""
    clock_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_seconds: float = 0.0
    final: bool = False
    server_time: float = 0.0
    state: str = ""
    revision: int = 0
    client_revision: int = 0
    party_key: str = ""
    target_key: str = ""
    total_damage: int = 0


@dataclass(frozen=True)
class UpdateInfo:
    available: bool = False
    latest_version: str = ""
    download_path: str = ""
    sha256: str = ""
    size: int = 0
    filename: str = ""
    notes: str = ""
    required: bool = False
    build_id: str = ""
    publisher_thumbprint: str = ""
    signature_required: bool = False
    rollback: bool = False


class LicensingConnectionError(RuntimeError):
    pass


class LicensingGateway(Protocol):
    """Server adapter contract; never persist passwords or raw card keys."""

    def sign_in(self, account: str, password: str) -> LicenseSession: ...

    def sign_in_card(self, card_key: str) -> LicenseSession: ...

    def claim_trial_card(self) -> TrialClaim: ...

    def refresh(self, session: LicenseSession) -> LicenseSession: ...

    def heartbeat(
        self,
        session: LicenseSession,
        *,
        using: bool,
        character_name: str,
        game_pid: int,
    ) -> HeartbeatResult: ...

    def submit_feedback(
        self,
        session: LicenseSession,
        *,
        category: str,
        content: str,
        character_name: str,
        diagnostics: dict[str, object] | None = None,
    ) -> FeedbackResult: ...

    def sync_combat_clock(
        self, session: LicenseSession, snapshot: dict[str, object]
    ) -> CombatClockResult: ...

    def redeem_card(self, session: LicenseSession, card_key: str) -> CardRedemption: ...

    def sign_out(self, session: LicenseSession) -> None: ...

    def check_update(self) -> UpdateInfo: ...

    def check_rollback(self, version: str) -> UpdateInfo: ...

    def download_update(
        self,
        update: UpdateInfo,
        destination: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path: ...


class LocalLicensingGateway:
    """Free-period implementation used until the account service is enabled."""

    def sign_in(self, account: str, password: str) -> LicenseSession:
        del account, password
        if china_now() <= FREE_TRIAL_END:
            return LicenseSession(
                account_id="free-trial",
                display_name="免费用户",
                access_token="local-free-trial",
                expires_at=FREE_TRIAL_END,
                entitlement="trial",
            )
        return LicenseSession(display_name="免费期已结束", entitlement="expired")

    def sign_in_card(self, card_key: str) -> LicenseSession:
        del card_key
        return LicenseSession(display_name="卡号服务需要连接服务器", entitlement="offline")

    def claim_trial_card(self) -> TrialClaim:
        return TrialClaim(False, "试用卡服务需要连接服务器")

    def refresh(self, session: LicenseSession) -> LicenseSession:
        return session if session.active else LicenseSession(
            display_name="登录已过期", entitlement="expired"
        )

    def heartbeat(
        self,
        session: LicenseSession,
        *,
        using: bool,
        character_name: str,
        game_pid: int,
    ) -> HeartbeatResult:
        del using, character_name, game_pid
        return HeartbeatResult(session.active)

    def submit_feedback(
        self,
        session: LicenseSession,
        *,
        category: str,
        content: str,
        character_name: str,
        diagnostics: dict[str, object] | None = None,
    ) -> FeedbackResult:
        del session, category, content, character_name, diagnostics
        return FeedbackResult(False, message="反馈服务需要连接服务器")

    def sync_combat_clock(
        self, session: LicenseSession, snapshot: dict[str, object]
    ) -> CombatClockResult:
        del session, snapshot
        return CombatClockResult()

    def redeem_card(self, session: LicenseSession, card_key: str) -> CardRedemption:
        del session, card_key
        return CardRedemption(False, "卡密服务尚未启用")

    def sign_out(self, session: LicenseSession) -> None:
        del session

    def check_update(self) -> UpdateInfo:
        return UpdateInfo()

    def check_rollback(self, version: str) -> UpdateInfo:
        del version
        raise LicensingConnectionError("当前模式不支持版本回滚")

    def download_update(
        self,
        update: UpdateInfo,
        destination: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        del update, destination, progress
        raise LicensingConnectionError("当前模式不支持在线更新")


class ServerLicensingGateway:
    def __init__(
        self,
        server_url: str,
        client_id: str,
        app_version: str,
        *,
        build_id: str = "",
        require_runtime_capability: bool = False,
        require_signed_updates: bool = False,
        trusted_publisher_thumbprints: tuple[str, ...] = (),
        trusted_capability_public_keys: Mapping[str, object] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ):
        self.server_url = str(server_url).strip().rstrip("/")
        self.client_id = str(client_id).strip().lower()
        self.app_version = str(app_version).strip()
        self.build_id = str(build_id).strip().lower()[:64]
        self.require_runtime_capability = bool(require_runtime_capability)
        self.require_signed_updates = bool(require_signed_updates)
        self.trusted_publisher_thumbprints = tuple(
            dict.fromkeys(
                re.sub(r"\s+", "", str(value or "")).upper()
                for value in trusted_publisher_thumbprints
                if CERTIFICATE_THUMBPRINT_PATTERN.fullmatch(
                    re.sub(r"\s+", "", str(value or "")).upper()
                )
            )
        )
        self.trusted_capability_public_keys = dict(
            trusted_capability_public_keys or {}
        )
        self._access_token = ""
        self.timeout = max(1.0, float(timeout))
        parsed_url = urlsplit(self.server_url)
        loopback_http = (
            parsed_url.scheme == "http"
            and parsed_url.hostname in {"127.0.0.1", "::1", "localhost"}
        )
        if parsed_url.scheme != "https" and not loopback_http:
            raise ValueError("server_url must use HTTPS")
        if not CLIENT_ID_PATTERN.fullmatch(self.client_id):
            raise ValueError("client_id is invalid")
        self.ssl_context: ssl.SSLContext | None = None
        if parsed_url.scheme == "https":
            # Keep Windows' trusted roots and add the bundled Mozilla roots.
            # Standalone EXEs must not depend on Python being installed or on
            # C:\Program Files\Common Files\SSL\cert.pem existing locally.
            context = ssl.create_default_context()
            if CA_BUNDLE_PATH.is_file():
                context.load_verify_locations(cafile=str(CA_BUNDLE_PATH))
            self.ssl_context = context

    def _open_get(self, path: str):
        headers = {
            "Accept": "application/json, application/octet-stream",
            "User-Agent": f"GMZZ-DPS/{self.app_version}",
        }
        if self.build_id:
            headers["X-DPS-Build-ID"] = self.build_id
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        request = Request(
            self.server_url + path,
            headers=headers,
            method="GET",
        )
        options = {"timeout": self.timeout}
        if self.ssl_context is not None:
            options["context"] = self.ssl_context
        return urlopen(request, **options)

    @staticmethod
    def _connection_error_message(exc: BaseException) -> str:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(reason, ssl.SSLCertVerificationError):
            return "安全连接验证失败，请校准系统日期和时间后重试。"
        if isinstance(reason, socket.gaierror):
            return "无法找到授权服务器，请检查网络或 DNS 后重试。"
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "连接授权服务器超时，请检查网络后重试。"
        if isinstance(reason, ssl.SSLError):
            return "无法建立安全连接，请校准系统日期和时间后重试。"
        if isinstance(reason, OSError) and getattr(reason, "winerror", 0) == 10013:
            return "程序的网络访问被拦截，请允许联网后重试。"
        return "无法连接授权服务器"

    def _request(
        self,
        path: str,
        payload: dict,
        *,
        access_token: str = "",
        allow_forbidden: bool = False,
    ) -> dict:
        request_payload = dict(payload)
        if self.build_id:
            request_payload.setdefault("build_id", self.build_id)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"GMZZ-DPS/{self.app_version}",
        }
        if self.build_id:
            headers["X-DPS-Build-ID"] = self.build_id
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        request = Request(
            self.server_url + path,
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        raw = b""
        for attempt in range(2):
            try:
                open_options = {"timeout": self.timeout}
                if self.ssl_context is not None:
                    open_options["context"] = self.ssl_context
                with urlopen(request, **open_options) as response:
                    raw = response.read(64 * 1024)
                break
            except HTTPError as exc:
                if allow_forbidden and exc.code in (401, 403):
                    try:
                        raw = exc.read(64 * 1024)
                        value = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, TypeError):
                        value = {}
                    finally:
                        exc.close()
                    if not isinstance(value, dict):
                        value = {}
                    value.setdefault("ok", False)
                    value.setdefault("authorized", False)
                    value.setdefault("error", "forbidden")
                    return value
                raise LicensingConnectionError(
                    f"服务器返回 HTTP {exc.code}"
                ) from exc
            except (URLError, OSError, TimeoutError) as exc:
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                raise LicensingConnectionError(
                    self._connection_error_message(exc)
                ) from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise LicensingConnectionError("授权服务器响应无效") from exc
        if not isinstance(value, dict):
            raise LicensingConnectionError("授权服务器响应无效")
        return value

    @staticmethod
    def _expires_at(value: object) -> datetime | None:
        try:
            timestamp = float(value or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if timestamp <= 0:
            return None
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(
                CHINA_TIMEZONE
            )
        except (OSError, OverflowError, ValueError):
            return None

    @staticmethod
    def _server_message(value: dict, fallback: str) -> str:
        message = str(value.get("message", "")).strip()
        if message:
            return message[:128]
        return SERVER_ERROR_MESSAGES.get(str(value.get("error", "")), fallback)

    @staticmethod
    def _card_tier(value: object, fallback: str = "normal") -> str:
        tier = str(value or "").strip().casefold()
        return tier if tier in {"normal", "weekly", "monthly", "partner"} else fallback

    def _runtime_capability(
        self,
        value: object,
        *,
        session_id: str,
    ) -> RuntimeCapability | None:
        if value is None:
            return None
        try:
            return RuntimeCapability.from_value(
                value,
                expected_session_id=session_id,
                expected_client_id=self.client_id,
                expected_build_id=self.build_id,
                expected_client_build=self.app_version,
                trusted_public_keys=self.trusted_capability_public_keys,
                allow_development=False,
            )
        except RuntimeCapabilityTimeError as exc:
            if not self.require_runtime_capability:
                return None
            raise LicensingConnectionError(SYSTEM_TIME_SYNC_MESSAGE) from exc
        except RuntimeCapabilityError as exc:
            if not self.require_runtime_capability:
                # Development and legacy builds may talk to an older server
                # while carrying their explicit local runtime profile.
                return None
            raise LicensingConnectionError(CARD_LOGIN_FAILURE_MESSAGE) from exc

    def sign_in(self, account: str, password: str) -> LicenseSession:
        return self.sign_in_card(password or account)

    def claim_trial_card(self) -> TrialClaim:
        value = self._request(
            "/api/v1/dps/trial/claim",
            {
                "client_id": self.client_id,
                "display_name": f"DPS-{self.client_id[:8]}",
                "app_version": self.app_version,
                "request_id": secrets.token_hex(16),
            },
            allow_forbidden=True,
        )
        if not value.get("ok"):
            return TrialClaim(
                False,
                self._server_message(value, "暂时无法领取试用卡。"),
                next_available_at=self._expires_at(value.get("next_available_at")),
            )
        card_key = str(value.get("card_key", "")).strip().upper()
        if not TRIAL_CARD_PATTERN.fullmatch(card_key):
            raise LicensingConnectionError("授权服务器未返回有效试用卡")
        try:
            duration_seconds = max(0, int(value.get("duration_seconds", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            duration_seconds = 0
        if duration_seconds <= 0:
            raise LicensingConnectionError("授权服务器返回的试用时长无效")
        return TrialClaim(
            True,
            self._server_message(value, "已领取试用卡，请点击登录。"),
            card_key=card_key,
            duration_seconds=duration_seconds,
            next_available_at=self._expires_at(value.get("next_available_at")),
        )

    def sign_in_card(self, card_key: str) -> LicenseSession:
        value = self._request(
            "/api/v1/dps/session/start",
            {
                "client_id": self.client_id,
                "display_name": f"DPS-{self.client_id[:8]}",
                "app_version": self.app_version,
                "card_key": str(card_key).strip(),
            },
            allow_forbidden=True,
        )
        if not value.get("authorized"):
            self._access_token = ""
            error = str(value.get("error", "forbidden"))
            return LicenseSession(
                display_name=self._server_message(
                    value, CARD_LOGIN_FAILURE_MESSAGE
                ),
                entitlement=error,
            )
        token = str(value.get("access_token", "")).strip()
        session_id = str(value.get("session_id", "")).strip()
        if not token or not session_id:
            raise LicensingConnectionError(CARD_LOGIN_FAILURE_MESSAGE)
        runtime_capability = self._runtime_capability(
            value.get("runtime_capability"),
            session_id=session_id,
        )
        if self.require_runtime_capability and runtime_capability is None:
            try:
                self._request(
                    "/api/v1/dps/session/end",
                    {},
                    access_token=token,
                    allow_forbidden=True,
                )
            finally:
                self._access_token = ""
            raise LicensingConnectionError(CARD_LOGIN_FAILURE_MESSAGE)
        self._access_token = token
        return LicenseSession(
            account_id=self.client_id,
            display_name=str(value.get("display_name", "在线用户"))[:48],
            access_token=token,
            expires_at=self._expires_at(value.get("expires_at")),
            entitlement="server",
            session_id=session_id,
            card_tier=self._card_tier(value.get("card_tier")),
            runtime_capability=runtime_capability,
        )

    def refresh(self, session: LicenseSession) -> LicenseSession:
        result = self.heartbeat(
            session, using=False, character_name="", game_pid=0
        )
        return session if result.authorized else LicenseSession(
            display_name="服务器已拒绝访问", entitlement="revoked"
        )

    def heartbeat(
        self,
        session: LicenseSession,
        *,
        using: bool,
        character_name: str,
        game_pid: int,
    ) -> HeartbeatResult:
        if not session.access_token:
            return HeartbeatResult(False, message="登录会话无效")
        value = self._request(
            "/api/v1/dps/session/heartbeat",
            {
                "using": bool(using),
                "character_name": str(character_name).strip()[:48],
                "game_pid": max(0, int(game_pid or 0)),
                "app_version": self.app_version,
            },
            access_token=session.access_token,
            allow_forbidden=True,
        )
        runtime_capability = None
        if value.get("authorized"):
            runtime_capability = self._runtime_capability(
                value.get("runtime_capability"),
                session_id=session.session_id,
            )
            if (
                runtime_capability is not None
                and session.runtime_capability is not None
                and runtime_capability.lease_sequence
                <= session.runtime_capability.lease_sequence
            ):
                raise LicensingConnectionError(
                    "服务器返回了重复或过期的采集授权。"
                )
            if self.require_runtime_capability and runtime_capability is None:
                return HeartbeatResult(
                    False,
                    message="正式客户端的采集授权未能续期，请重新登录。",
                )
        return HeartbeatResult(
            authorized=bool(value.get("authorized")),
            heartbeat_interval=max(
                10, min(120, int(value.get("heartbeat_interval", 30) or 30))
            ),
            message=self._server_message(value, "登录状态已失效，请重新登录。"),
            expires_at=self._expires_at(value.get("expires_at")),
            card_tier=self._card_tier(value.get("card_tier"), ""),
            runtime_capability=runtime_capability,
        )

    def submit_feedback(
        self,
        session: LicenseSession,
        *,
        category: str,
        content: str,
        character_name: str,
        diagnostics: dict[str, object] | None = None,
    ) -> FeedbackResult:
        if not session.access_token:
            return FeedbackResult(False, message="登录状态已失效，请重新登录。")
        value = self._request(
            "/api/v1/dps/feedback",
            {
                "category": str(category).strip()[:32],
                "content": str(content).strip()[:2000],
                "character_name": str(character_name).strip()[:48],
                "app_version": self.app_version,
                "diagnostics": diagnostics if isinstance(diagnostics, dict) else {},
            },
            access_token=session.access_token,
            allow_forbidden=True,
        )
        accepted = bool(value.get("ok"))
        return FeedbackResult(
            accepted=accepted,
            feedback_id=str(value.get("feedback_id", "")).strip()[:32],
            message=self._server_message(
                value,
                "反馈已提交。" if accepted else "反馈提交失败，请稍后再试。",
            ),
        )

    def sync_combat_clock(
        self, session: LicenseSession, snapshot: dict[str, object]
    ) -> CombatClockResult:
        if not session.access_token:
            return CombatClockResult()
        value = self._request(
            "/api/v2/dps/combat/clock",
            dict(snapshot),
            access_token=session.access_token,
            allow_forbidden=True,
        )
        if not value.get("synchronized"):
            return CombatClockResult(
                encounter_id=str(snapshot.get("encounter_id", ""))[:96]
            )
        try:
            started_at = max(0.0, float(value.get("started_at", 0.0) or 0.0))
            ended_at = max(0.0, float(value.get("ended_at", 0.0) or 0.0))
            duration = max(
                1.0, float(value.get("duration_seconds", 0.0) or 0.0)
            )
            server_time = max(0.0, float(value.get("server_time", 0.0) or 0.0))
            revision = max(0, int(value.get("revision", 0) or 0))
            client_revision = max(
                0, int(value.get("client_revision", 0) or 0)
            )
            total_damage = max(0, int(value.get("total_damage", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return CombatClockResult()
        state = str(value.get("state", "")).strip().casefold()
        if state not in {"active", "settling", "final"}:
            state = "final" if bool(value.get("final")) else "active"
        return CombatClockResult(
            synchronized=True,
            encounter_id=str(value.get("encounter_id", "")).strip()[:96],
            clock_id=str(value.get("clock_id", "")).strip()[:64],
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration,
            final=state == "final",
            server_time=server_time,
            state=state,
            revision=revision,
            client_revision=client_revision,
            party_key=str(value.get("party_key", "")).strip()[:64],
            target_key=str(value.get("target_key", "")).strip()[:64],
            total_damage=total_damage,
        )

    def redeem_card(self, session: LicenseSession, card_key: str) -> CardRedemption:
        del session, card_key
        return CardRedemption(False, "卡密服务尚未启用")

    def sign_out(self, session: LicenseSession) -> None:
        if not session.access_token:
            self._access_token = ""
            return
        try:
            self._request(
                "/api/v1/dps/session/end",
                {"session_id": session.session_id},
                access_token=session.access_token,
                allow_forbidden=True,
            )
        except LicensingConnectionError:
            pass
        finally:
            self._access_token = ""

    def check_update(self) -> UpdateInfo:
        path = "/api/v1/dps/update?version=" + quote(
            self.app_version, safe=".-"
        )
        return self._check_update_path(
            path,
            expected_download_path="/api/v1/dps/update/download",
            rollback=False,
        )

    def check_rollback(self, version: str) -> UpdateInfo:
        target_version = normalize_rollback_version(version)
        if not rollback_version_is_supported(target_version):
            raise LicensingConnectionError("仅支持回滚到 v0.1.0 及以上版本")
        encoded_version = quote(target_version, safe=".-")
        path = f"/api/v1/dps/update/rollback?version={encoded_version}"
        download_path = (
            f"/api/v1/dps/update/rollback/download?version={encoded_version}"
        )
        update = self._check_update_path(
            path,
            expected_download_path=download_path,
            rollback=True,
        )
        if (
            not update.available
            or normalize_rollback_version(update.latest_version) != target_version
        ):
            raise LicensingConnectionError(
                f"服务器暂未提供 v{target_version} 的回滚包"
            )
        return update

    def _check_update_path(
        self,
        path: str,
        *,
        expected_download_path: str,
        rollback: bool,
    ) -> UpdateInfo:
        raw = b""
        for attempt in range(2):
            try:
                with self._open_get(path) as response:
                    raw = response.read(64 * 1024)
                break
            except HTTPError as exc:
                if rollback and exc.code in {400, 404}:
                    raise LicensingConnectionError(
                        "服务器暂未提供该版本的回滚包"
                    ) from exc
                raise LicensingConnectionError(
                    f"更新服务器返回 HTTP {exc.code}"
                ) from exc
            except (URLError, OSError, TimeoutError) as exc:
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                raise LicensingConnectionError(
                    self._connection_error_message(exc)
                ) from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise LicensingConnectionError("更新服务器响应无效") from exc
        if not isinstance(value, dict) or not value.get("ok"):
            raise LicensingConnectionError("更新服务器响应无效")
        try:
            size = max(0, int(value.get("size", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            size = 0
        download_path = str(value.get("download_path", "")).strip()
        sha256 = str(value.get("sha256", "")).strip().lower()
        available = bool(value.get("available"))
        filename = Path(str(value.get("filename", "update.exe"))).name
        build_id = str(value.get("build_id", "")).strip().lower()
        publisher_thumbprint = re.sub(
            r"\s+", "", str(value.get("publisher_thumbprint", ""))
        ).upper()
        signature_required = bool(value.get("signature_required"))
        if available and (
            download_path != expected_download_path
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or size <= 0
            or size > 256 * 1024 * 1024
            or not filename.casefold().endswith(".exe")
        ):
            raise LicensingConnectionError("更新信息不完整")
        if available and signature_required and (
            not BUILD_ID_PATTERN.fullmatch(build_id)
            or not CERTIFICATE_THUMBPRINT_PATTERN.fullmatch(publisher_thumbprint)
        ):
            raise LicensingConnectionError("更新签名信息不完整")
        if available and self.require_signed_updates and not signature_required:
            raise LicensingConnectionError("服务器未提供已签名的官方更新")
        if (
            available
            and signature_required
            and self.trusted_publisher_thumbprints
            and publisher_thumbprint not in self.trusted_publisher_thumbprints
        ):
            raise LicensingConnectionError("更新包发布者不在受信任列表中")
        return UpdateInfo(
            available=available,
            latest_version=str(value.get("latest_version", "")).strip()[:32],
            download_path=download_path,
            sha256=sha256,
            size=size,
            filename=filename,
            notes=str(value.get("notes", "")).strip()[:1000],
            required=bool(value.get("required")),
            build_id=build_id,
            publisher_thumbprint=publisher_thumbprint,
            signature_required=signature_required,
            rollback=rollback,
        )

    def download_update(
        self,
        update: UpdateInfo,
        destination: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        if not update.available or not update.download_path:
            raise LicensingConnectionError("没有可下载的更新")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".download")
        last_error: BaseException | None = None
        deadline = time.monotonic() + UPDATE_DOWNLOAD_TIMEOUT_SECONDS
        for attempt in range(2):
            digest = hashlib.sha256()
            written = 0
            try:
                if progress is not None:
                    progress(0, update.size)
                with self._open_get(update.download_path) as response, temporary.open(
                    "wb"
                ) as handle:
                    response_length = response.headers.get("Content-Length")
                    if response_length:
                        try:
                            declared_length = int(response_length)
                        except (TypeError, ValueError, OverflowError):
                            declared_length = 0
                        if declared_length != update.size:
                            raise LicensingConnectionError(
                                "更新文件大小与服务器声明不一致，请重试"
                            )
                    # The update metadata already gives us the exact signed/hash-
                    # checked byte count.  Stop as soon as that many bytes arrive;
                    # waiting for a proxy or keep-alive connection to signal EOF
                    # can otherwise leave the UI sitting at 100% indefinitely.
                    while written < update.size:
                        if time.monotonic() >= deadline:
                            raise LicensingConnectionError("更新下载超时，请重试")
                        chunk = response.read(
                            min(256 * 1024, update.size - written)
                        )
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > 256 * 1024 * 1024:
                            raise LicensingConnectionError("更新文件大小异常")
                        digest.update(chunk)
                        handle.write(chunk)
                        if progress is not None:
                            progress(written, update.size)
                if written != update.size or digest.hexdigest() != update.sha256:
                    raise LicensingConnectionError(
                        "更新文件校验失败，请重新下载"
                    )
                if update.signature_required or self.require_signed_updates:
                    expected_publishers = (
                        self.trusted_publisher_thumbprints
                        or (update.publisher_thumbprint,)
                    )
                    verification = verify_windows_publisher(
                        temporary,
                        expected_publishers,
                        require_trusted_chain=True,
                    )
                    if not verification.verified:
                        raise LicensingConnectionError(
                            "更新包数字签名验证失败，已拒绝安装"
                        )
                os.replace(temporary, destination)
                return destination
            except HTTPError as exc:
                last_error = LicensingConnectionError(
                    f"更新下载返回 HTTP {exc.code}"
                )
                break
            except LicensingConnectionError as exc:
                last_error = exc
            except (URLError, OSError, TimeoutError) as exc:
                last_error = LicensingConnectionError(
                    self._connection_error_message(exc)
                )
            finally:
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
            if attempt == 0:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        if last_error is not None:
            raise last_error
        raise LicensingConnectionError("更新下载失败")

class LicensingService:
    def __init__(self, gateway: LicensingGateway | None = None):
        self.gateway = gateway or LocalLicensingGateway()
        self.session = LicenseSession()

    @property
    def status_text(self) -> str:
        return self.session.display_name if self.session.active else "未登录"

    @property
    def free_trial_deadline_text(self) -> str:
        return FREE_TRIAL_END.strftime("%Y年%m月%d日 %H:%M")

    @property
    def free_trial_available(self) -> bool:
        return china_now() <= FREE_TRIAL_END

    def sign_in_free(self) -> LicenseSession:
        self.session = self.gateway.sign_in("", "")
        return self.session

    def sign_in_card(self, card_key: str) -> LicenseSession:
        self.session = self.gateway.sign_in_card(card_key)
        return self.session

    def claim_trial_card(self) -> TrialClaim:
        return self.gateway.claim_trial_card()

    def heartbeat(
        self, *, using: bool, character_name: str = "", game_pid: int = 0
    ) -> HeartbeatResult:
        result = self.gateway.heartbeat(
            self.session,
            using=using,
            character_name=character_name,
            game_pid=game_pid,
        )
        if result.authorized:
            changes = {}
            if result.expires_at != self.session.expires_at:
                changes["expires_at"] = result.expires_at
            if result.card_tier and result.card_tier != self.session.card_tier:
                changes["card_tier"] = result.card_tier
            if result.runtime_capability is not None:
                changes["runtime_capability"] = result.runtime_capability
            if changes:
                self.session = replace(self.session, **changes)
        return result

    def set_runtime_capability(
        self, capability: RuntimeCapability
    ) -> LicenseSession:
        self.session = replace(
            self.session,
            runtime_capability=capability,
        )
        return self.session

    def submit_feedback(
        self,
        *,
        category: str,
        content: str,
        character_name: str = "",
        diagnostics: dict[str, object] | None = None,
    ) -> FeedbackResult:
        return self.gateway.submit_feedback(
            self.session,
            category=category,
            content=content,
            character_name=character_name,
            diagnostics=diagnostics,
        )

    def sync_combat_clock(
        self, snapshot: dict[str, object]
    ) -> CombatClockResult:
        return self.gateway.sync_combat_clock(self.session, snapshot)

    def check_update(self) -> UpdateInfo:
        return self.gateway.check_update()

    def check_rollback(self, version: str) -> UpdateInfo:
        return self.gateway.check_rollback(version)

    def download_update(
        self,
        update: UpdateInfo,
        destination: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        if progress is None:
            return self.gateway.download_update(update, destination)
        return self.gateway.download_update(update, destination, progress)

    def sign_out(self) -> None:
        self.gateway.sign_out(self.session)
        self.session = LicenseSession()
