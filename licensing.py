"""Authentication, entitlement, and server-presence boundary."""

from __future__ import annotations

import json
import hashlib
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
FREE_TRIAL_END = datetime(2026, 9, 3, 23, 59, 59, tzinfo=CHINA_TIMEZONE)
DEFAULT_SERVER_URL = "https://daodaogame.vip"
REQUEST_TIMEOUT_SECONDS = 6.0
CLIENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CA_BUNDLE_PATH = Path(__file__).resolve().with_name("cacert.pem")

SERVER_ERROR_MESSAGES = {
    "card_required": "请输入卡号。",
    "card_invalid": "卡号不存在或格式不正确。",
    "card_expired": "卡号使用时间已结束。",
    "card_revoked": "卡号已被停用。",
    "card_bound": "卡号已绑定其他设备。",
    "card_in_use": "该卡号已在其他客户端登录。",
    "card_rebind_cooldown": "卡号正在设备改绑冷却中。",
    "client_revoked": "当前设备已被停用。",
    "invalid_session": "登录状态已失效，请重新输入卡号。",
    "forbidden": "服务器已拒绝本次登录。",
}


def china_now() -> datetime:
    return datetime.now(CHINA_TIMEZONE)


@dataclass(frozen=True)
class LicenseSession:
    account_id: str = ""
    display_name: str = "未登录"
    access_token: str = ""
    expires_at: datetime | None = None
    entitlement: str = "none"
    session_id: str = ""
    card_tier: str = "normal"

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
class HeartbeatResult:
    authorized: bool
    heartbeat_interval: int = 30
    message: str = ""
    expires_at: datetime | None = None
    card_tier: str = ""


@dataclass(frozen=True)
class FeedbackResult:
    accepted: bool
    feedback_id: str = ""
    message: str = ""


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


class LicensingConnectionError(RuntimeError):
    pass


class LicensingGateway(Protocol):
    """Server adapter contract; never persist passwords or raw card keys."""

    def sign_in(self, account: str, password: str) -> LicenseSession: ...

    def sign_in_card(self, card_key: str) -> LicenseSession: ...

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

    def redeem_card(self, session: LicenseSession, card_key: str) -> CardRedemption: ...

    def sign_out(self, session: LicenseSession) -> None: ...

    def check_update(self) -> UpdateInfo: ...

    def download_update(self, update: UpdateInfo, destination: Path) -> Path: ...


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

    def redeem_card(self, session: LicenseSession, card_key: str) -> CardRedemption:
        del session, card_key
        return CardRedemption(False, "卡密服务尚未启用")

    def sign_out(self, session: LicenseSession) -> None:
        del session

    def check_update(self) -> UpdateInfo:
        return UpdateInfo()

    def download_update(self, update: UpdateInfo, destination: Path) -> Path:
        del update, destination
        raise LicensingConnectionError("当前模式不支持在线更新")


class ServerLicensingGateway:
    def __init__(
        self,
        server_url: str,
        client_id: str,
        app_version: str,
        *,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ):
        self.server_url = str(server_url).strip().rstrip("/")
        self.client_id = str(client_id).strip().lower()
        self.app_version = str(app_version).strip()
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
        request = Request(
            self.server_url + path,
            headers={
                "Accept": "application/json, application/octet-stream",
                "User-Agent": f"GMZZ-DPS/{self.app_version}",
            },
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
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"GMZZ-DPS/{self.app_version}",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        request = Request(
            self.server_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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

    def sign_in(self, account: str, password: str) -> LicenseSession:
        return self.sign_in_card(password or account)

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
            error = str(value.get("error", "forbidden"))
            return LicenseSession(
                display_name=self._server_message(value, "服务器已拒绝本次登录。"),
                entitlement=error,
            )
        token = str(value.get("access_token", "")).strip()
        session_id = str(value.get("session_id", "")).strip()
        if not token or not session_id:
            raise LicensingConnectionError("授权服务器未返回有效会话")
        return LicenseSession(
            account_id=self.client_id,
            display_name=str(value.get("display_name", "在线用户"))[:48],
            access_token=token,
            expires_at=self._expires_at(value.get("expires_at")),
            entitlement="server",
            session_id=session_id,
            card_tier=self._card_tier(value.get("card_tier")),
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
        return HeartbeatResult(
            authorized=bool(value.get("authorized")),
            heartbeat_interval=max(
                10, min(120, int(value.get("heartbeat_interval", 30) or 30))
            ),
            message=self._server_message(value, "登录状态已失效，请重新输入卡号。"),
            expires_at=self._expires_at(value.get("expires_at")),
            card_tier=self._card_tier(value.get("card_tier"), ""),
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

    def redeem_card(self, session: LicenseSession, card_key: str) -> CardRedemption:
        del session, card_key
        return CardRedemption(False, "卡密服务尚未启用")

    def sign_out(self, session: LicenseSession) -> None:
        if not session.access_token:
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

    def check_update(self) -> UpdateInfo:
        path = "/api/v1/dps/update?version=" + quote(
            self.app_version, safe=".-"
        )
        raw = b""
        for attempt in range(2):
            try:
                with self._open_get(path) as response:
                    raw = response.read(64 * 1024)
                break
            except HTTPError as exc:
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
        if available and (
            download_path != "/api/v1/dps/update/download"
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or size <= 0
            or size > 256 * 1024 * 1024
            or not filename.casefold().endswith(".exe")
        ):
            raise LicensingConnectionError("更新信息不完整")
        return UpdateInfo(
            available=available,
            latest_version=str(value.get("latest_version", "")).strip()[:32],
            download_path=download_path,
            sha256=sha256,
            size=size,
            filename=filename,
            notes=str(value.get("notes", "")).strip()[:1000],
            required=bool(value.get("required")),
        )

    def download_update(self, update: UpdateInfo, destination: Path) -> Path:
        if not update.available or not update.download_path:
            raise LicensingConnectionError("没有可下载的更新")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".download")
        last_error: BaseException | None = None
        for attempt in range(2):
            digest = hashlib.sha256()
            written = 0
            try:
                with self._open_get(update.download_path) as response, temporary.open(
                    "wb"
                ) as handle:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > 256 * 1024 * 1024:
                            raise LicensingConnectionError("更新文件大小异常")
                        digest.update(chunk)
                        handle.write(chunk)
                if written != update.size or digest.hexdigest() != update.sha256:
                    raise LicensingConnectionError(
                        "更新文件校验失败，请重新下载"
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
            if changes:
                self.session = replace(self.session, **changes)
        return result

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

    def check_update(self) -> UpdateInfo:
        return self.gateway.check_update()

    def download_update(self, update: UpdateInfo, destination: Path) -> Path:
        return self.gateway.download_update(update, destination)

    def sign_out(self) -> None:
        self.gateway.sign_out(self.session)
        self.session = LicenseSession()
