"""Validated, short-lived runtime configuration for the capture boundary.

This module intentionally contains no game addresses, hook signatures, or
protocol method names.  Official clients receive those values from the
licensed server over TLS.  Source development may load them from an explicit
external JSON file that is never included in a release build.
"""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


RUNTIME_PROFILE_SCHEMA_VERSION = 1
RUNTIME_CAPABILITY_SCHEMA_VERSION = 2
PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,63}$")
CAPABILITY_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
CLIENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
BUILD_BINDING_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{7,63}$")
SIGNING_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,63}$")
HEX_BYTES_PATTERN = re.compile(r"^[0-9a-f]+$")
REQUIRED_HOOKS = (
    "network_message",
    "damage",
    "entity_name",
    "boss_type",
    "boss_init",
    "template_id",
    "template_bulk",
    "team_stats",
)
MAX_RUNTIME_PROFILE_BYTES = 64 * 1024
MAX_SIGNING_KEY_BYTES = 16 * 1024
MAX_SERVER_LEASE_SECONDS = 10 * 60
MAX_DEVELOPMENT_LEASE_SECONDS = 7 * 24 * 60 * 60


class RuntimeCapabilityError(ValueError):
    """Raised when a capture capability is missing, stale, or malformed."""


class RuntimeCapabilityTimeError(RuntimeCapabilityError):
    """Raised when a valid lease falls outside the local system clock window."""


def _clean_text(value: object, limit: int) -> str:
    text = str(value or "").strip().replace("\x00", "")
    return "".join(char for char in text if ord(char) >= 0x20)[:limit]


def _base64_bytes(value: object, label: str, *, expected_length: int) -> bytes:
    text = _clean_text(value, 256)
    try:
        result = base64.b64decode(text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise RuntimeCapabilityError(f"{label} is not valid base64") from exc
    if len(result) != expected_length:
        raise RuntimeCapabilityError(f"{label} has an invalid length")
    return result


def capability_public_key_to_base64(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def load_capability_private_key(value: object) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from an object, PEM, or raw base64 seed."""
    if isinstance(value, Ed25519PrivateKey):
        return value
    try:
        if isinstance(value, (bytes, bytearray)) and len(value) == 32:
            return Ed25519PrivateKey.from_private_bytes(bytes(value))
        raw = (
            bytes(value)
            if isinstance(value, (bytes, bytearray))
            else str(value or "").encode("ascii")
        )
        if raw.lstrip().startswith(b"-----BEGIN"):
            key = serialization.load_pem_private_key(raw, password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise RuntimeCapabilityError("runtime capability private key is not Ed25519")
            return key
        seed = _base64_bytes(raw.decode("ascii"), "runtime capability private key", expected_length=32)
        return Ed25519PrivateKey.from_private_bytes(seed)
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        if isinstance(exc, RuntimeCapabilityError):
            raise
        raise RuntimeCapabilityError("runtime capability private key is invalid") from exc


def load_capability_private_key_file(path: Path) -> Ed25519PrivateKey:
    target = Path(path)
    try:
        if target.stat().st_size > MAX_SIGNING_KEY_BYTES:
            raise RuntimeCapabilityError("runtime capability private key file is too large")
        payload = target.read_bytes()
    except OSError as exc:
        raise RuntimeCapabilityError("runtime capability private key is unavailable") from exc
    return load_capability_private_key(payload)


def _load_capability_public_key(value: object) -> Ed25519PublicKey:
    if isinstance(value, Ed25519PublicKey):
        return value
    if isinstance(value, (bytes, bytearray)) and len(value) == 32:
        raw = bytes(value)
    else:
        raw = _base64_bytes(
            value,
            "runtime capability public key",
            expected_length=32,
        )
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeCapabilityError("runtime capability public key is invalid") from exc


def _positive_rva(value: object, label: str) -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeCapabilityError(f"{label} is not an integer") from exc
    if result <= 0 or result > 0x7FFF_FFFF:
        raise RuntimeCapabilityError(f"{label} is outside the supported range")
    return result


def _hex_bytes(value: object, label: str, *, minimum: int = 1) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        result = bytes(value)
        if minimum <= len(result) <= 128:
            return result
        raise RuntimeCapabilityError(f"{label} is not valid bounded byte data")
    text = re.sub(r"\s+", "", str(value or "")).lower()
    if (
        len(text) % 2
        or len(text) < minimum * 2
        or len(text) > 256
        or not HEX_BYTES_PATTERN.fullmatch(text)
    ):
        raise RuntimeCapabilityError(f"{label} is not valid bounded hex data")
    return bytes.fromhex(text)


def _method_name(value: object, label: str) -> str:
    text = _clean_text(value, 96)
    try:
        encoded = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeCapabilityError(f"{label} must be ASCII") from exc
    if not encoded or any(byte < 0x21 or byte > 0x7E for byte in encoded):
        raise RuntimeCapabilityError(f"{label} is invalid")
    return text


def normalize_runtime_profile(value: object) -> dict[str, object]:
    """Return a bounded canonical profile with decoded hook byte strings."""
    if not isinstance(value, Mapping):
        raise RuntimeCapabilityError("runtime profile is not an object")
    if int(value.get("schema_version", 0) or 0) != RUNTIME_PROFILE_SCHEMA_VERSION:
        raise RuntimeCapabilityError("runtime profile schema is unsupported")
    profile_id = _clean_text(value.get("profile_id"), 64)
    if not PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise RuntimeCapabilityError("runtime profile ID is invalid")
    game_module = Path(_clean_text(value.get("game_module"), 96)).name
    if (
        not game_module
        or game_module != _clean_text(value.get("game_module"), 96)
        or not game_module.casefold().endswith(".exe")
    ):
        raise RuntimeCapabilityError("runtime game module is invalid")

    raw_hooks = value.get("hooks")
    if not isinstance(raw_hooks, Mapping):
        raise RuntimeCapabilityError("runtime hook inventory is missing")
    hooks: dict[str, dict[str, object]] = {}
    for name in REQUIRED_HOOKS:
        raw_hook = raw_hooks.get(name)
        if not isinstance(raw_hook, Mapping):
            raise RuntimeCapabilityError(f"runtime hook {name} is missing")
        prologue = _hex_bytes(
            raw_hook.get("prologue"), f"runtime hook {name} prologue", minimum=14
        )
        signature = _hex_bytes(
            raw_hook.get("signature"), f"runtime hook {name} signature", minimum=14
        )
        if not signature.startswith(prologue):
            raise RuntimeCapabilityError(
                f"runtime hook {name} signature does not contain its prologue"
            )
        hooks[name] = {
            "rva": _positive_rva(raw_hook.get("rva"), f"runtime hook {name} RVA"),
            "prologue": prologue,
            "signature": signature,
        }

    raw_protocol = value.get("protocol")
    if not isinstance(raw_protocol, Mapping):
        raise RuntimeCapabilityError("runtime protocol profile is missing")
    raw_methods = raw_protocol.get("synchronized_methods")
    if not isinstance(raw_methods, (list, tuple)) or not 1 <= len(raw_methods) <= 64:
        raise RuntimeCapabilityError("runtime synchronized method set is invalid")
    synchronized_methods = tuple(
        dict.fromkeys(
            _method_name(method, "runtime synchronized method")
            for method in raw_methods
        )
    )
    if not synchronized_methods:
        raise RuntimeCapabilityError("runtime synchronized method set is empty")
    request_method = _method_name(
        raw_protocol.get("team_stats_request_method"),
        "runtime team-stat request method",
    )

    raw_object_table = value.get("object_table")
    if not isinstance(raw_object_table, Mapping):
        raise RuntimeCapabilityError("runtime object-table profile is missing")
    object_table = {
        "chunks_rva": _positive_rva(
            raw_object_table.get("chunks_rva"), "runtime object-table chunks RVA"
        ),
        "count_rva": _positive_rva(
            raw_object_table.get("count_rva"), "runtime object-table count RVA"
        ),
    }
    return {
        "schema_version": RUNTIME_PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "game_module": game_module,
        "hooks": hooks,
        "protocol": {
            "synchronized_methods": synchronized_methods,
            "team_stats_request_method": request_method,
        },
        "object_table": object_table,
    }


def runtime_profile_to_wire(profile: Mapping[str, object]) -> dict[str, object]:
    """Convert a normalized profile to a JSON-safe canonical mapping."""
    normalized = normalize_runtime_profile(profile)
    hooks = normalized["hooks"]
    assert isinstance(hooks, dict)
    protocol = normalized["protocol"]
    assert isinstance(protocol, dict)
    return {
        "schema_version": RUNTIME_PROFILE_SCHEMA_VERSION,
        "profile_id": normalized["profile_id"],
        "game_module": normalized["game_module"],
        "hooks": {
            name: {
                "rva": int(hook["rva"]),
                "prologue": bytes(hook["prologue"]).hex(),
                "signature": bytes(hook["signature"]).hex(),
            }
            for name, hook in hooks.items()
        },
        "protocol": {
            "synchronized_methods": list(protocol["synchronized_methods"]),
            "team_stats_request_method": protocol["team_stats_request_method"],
        },
        "object_table": dict(normalized["object_table"]),
    }


def runtime_profile_digest(profile: Mapping[str, object]) -> str:
    payload = json.dumps(
        runtime_profile_to_wire(profile),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def runtime_profile_hook(
    profile: Mapping[str, object], name: str
) -> dict[str, object]:
    normalized = normalize_runtime_profile(profile)
    hooks = normalized["hooks"]
    assert isinstance(hooks, dict)
    hook = hooks.get(str(name))
    if not isinstance(hook, dict):
        raise RuntimeCapabilityError(f"runtime hook {name} is unavailable")
    return dict(hook)


def runtime_capability_signing_payload(value: Mapping[str, object]) -> bytes:
    """Return the stable JSON payload covered by the Ed25519 signature."""
    payload = {
        "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
        "capability_id": _clean_text(value.get("capability_id"), 64).lower(),
        "session_id": _clean_text(value.get("session_id"), 128),
        "client_id": _clean_text(value.get("client_id"), 64).lower(),
        "build_id": _clean_text(value.get("build_id"), 64).lower(),
        "client_build": _clean_text(value.get("client_build"), 64),
        "lease_sequence": int(value.get("lease_sequence", 0) or 0),
        "issued_at": float(value.get("issued_at", 0) or 0),
        "expires_at": float(value.get("expires_at", 0) or 0),
        "profile_digest": _clean_text(value.get("profile_digest"), 64).lower(),
        "development": bool(value.get("development")),
        "signing_key_id": _clean_text(value.get("signing_key_id"), 64),
        "profile": runtime_profile_to_wire(value.get("profile")),
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


@dataclass(frozen=True)
class RuntimeCapability:
    capability_id: str
    session_id: str
    client_id: str
    build_id: str
    client_build: str
    lease_sequence: int
    issued_at: float
    expires_at: float
    profile_digest: str
    profile: dict[str, object]
    development: bool = False
    signing_key_id: str = ""
    signature: str = ""

    @property
    def active(self) -> bool:
        return self.expires_at > time.time()

    @property
    def profile_id(self) -> str:
        return str(self.profile.get("profile_id", ""))

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
            "capability_id": self.capability_id,
            "session_id": self.session_id,
            "client_id": self.client_id,
            "build_id": self.build_id,
            "client_build": self.client_build,
            "lease_sequence": self.lease_sequence,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "profile_digest": self.profile_digest,
            "development": self.development,
            "signing_key_id": self.signing_key_id,
            "signature": self.signature,
            "profile": runtime_profile_to_wire(self.profile),
        }

    @classmethod
    def from_value(
        cls,
        value: object,
        *,
        expected_session_id: str = "",
        expected_client_id: str = "",
        expected_build_id: str = "",
        expected_client_build: str = "",
        trusted_public_keys: Mapping[str, object] | None = None,
        allow_development: bool = False,
        now: float | None = None,
    ) -> "RuntimeCapability":
        if isinstance(value, cls):
            value = value.to_wire()
        if not isinstance(value, Mapping):
            raise RuntimeCapabilityError("runtime capability is missing")
        if int(value.get("schema_version", 0) or 0) != RUNTIME_CAPABILITY_SCHEMA_VERSION:
            raise RuntimeCapabilityError("runtime capability schema is unsupported")
        capability_id = _clean_text(value.get("capability_id"), 64).lower()
        session_id = _clean_text(value.get("session_id"), 128)
        client_id = _clean_text(value.get("client_id"), 64).lower()
        build_id = _clean_text(value.get("build_id"), 64).lower()
        client_build = _clean_text(value.get("client_build"), 64)
        development = bool(value.get("development"))
        signing_key_id = _clean_text(value.get("signing_key_id"), 64)
        signature = _clean_text(value.get("signature"), 128)
        if not CAPABILITY_ID_PATTERN.fullmatch(capability_id):
            raise RuntimeCapabilityError("runtime capability ID is invalid")
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise RuntimeCapabilityError("runtime capability session binding is invalid")
        if not CLIENT_ID_PATTERN.fullmatch(client_id):
            raise RuntimeCapabilityError("runtime capability client binding is invalid")
        if not BUILD_BINDING_PATTERN.fullmatch(build_id):
            raise RuntimeCapabilityError("runtime capability build binding is invalid")
        if not client_build:
            raise RuntimeCapabilityError("runtime capability client build is missing")
        if development and not allow_development:
            raise RuntimeCapabilityError("development runtime capability is not allowed")
        try:
            lease_sequence = int(value.get("lease_sequence", 0) or 0)
            issued_at = float(value.get("issued_at", 0) or 0)
            expires_at = float(value.get("expires_at", 0) or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeCapabilityError("runtime capability lease is invalid") from exc
        if isinstance(value.get("lease_sequence"), bool) or not 1 <= lease_sequence < 2**63:
            raise RuntimeCapabilityError("runtime capability lease sequence is invalid")
        current = time.time() if now is None else float(now)
        maximum_lease = (
            MAX_DEVELOPMENT_LEASE_SECONDS
            if development
            else MAX_SERVER_LEASE_SECONDS
        )
        if (
            not math.isfinite(issued_at)
            or not math.isfinite(expires_at)
            or issued_at <= 0
            or expires_at <= issued_at
            or expires_at - issued_at > maximum_lease
        ):
            raise RuntimeCapabilityError("runtime capability lease is invalid")
        if expires_at <= current or issued_at > current + 60:
            raise RuntimeCapabilityTimeError(
                "runtime capability lease has expired or local clock is invalid"
            )
        profile = normalize_runtime_profile(value.get("profile"))
        digest = _clean_text(value.get("profile_digest"), 64).lower()
        actual_digest = runtime_profile_digest(profile)
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != actual_digest:
            raise RuntimeCapabilityError("runtime profile digest does not match")
        if development:
            if signing_key_id or signature:
                raise RuntimeCapabilityError(
                    "development runtime capability must be unsigned"
                )
        else:
            if not SIGNING_KEY_ID_PATTERN.fullmatch(signing_key_id):
                raise RuntimeCapabilityError(
                    "runtime capability signing key ID is invalid"
                )
            raw_key = (
                trusted_public_keys.get(signing_key_id)
                if isinstance(trusted_public_keys, Mapping)
                else None
            )
            if raw_key is None:
                raise RuntimeCapabilityError(
                    "runtime capability signing key is not trusted"
                )
            public_key = _load_capability_public_key(raw_key)
            signature_bytes = _base64_bytes(
                signature,
                "runtime capability signature",
                expected_length=64,
            )
            try:
                public_key.verify(
                    signature_bytes,
                    runtime_capability_signing_payload(value),
                )
            except InvalidSignature as exc:
                raise RuntimeCapabilityError(
                    "runtime capability signature is invalid"
                ) from exc
        if expected_session_id and session_id != expected_session_id:
            raise RuntimeCapabilityError("runtime capability belongs to another session")
        if expected_client_id and client_id != expected_client_id.strip().lower():
            raise RuntimeCapabilityError("runtime capability belongs to another client")
        if expected_build_id and build_id != expected_build_id.strip().lower():
            raise RuntimeCapabilityError("runtime capability belongs to another build")
        if expected_client_build and client_build != expected_client_build.strip():
            raise RuntimeCapabilityError("runtime capability belongs to another client build")
        return cls(
            capability_id=capability_id,
            session_id=session_id,
            client_id=client_id,
            build_id=build_id,
            client_build=client_build,
            lease_sequence=lease_sequence,
            issued_at=issued_at,
            expires_at=expires_at,
            profile_digest=digest,
            profile=profile,
            development=development,
            signing_key_id=signing_key_id,
            signature=signature,
        )

    def same_binding(self, other: "RuntimeCapability") -> bool:
        return bool(
            self.session_id == other.session_id
            and self.client_id == other.client_id
            and self.build_id == other.build_id
            and self.client_build == other.client_build
            and self.profile_digest == other.profile_digest
            and self.development == other.development
        )


def load_runtime_profile_file(path: Path) -> dict[str, object]:
    target = Path(path)
    try:
        if target.stat().st_size > MAX_RUNTIME_PROFILE_BYTES:
            raise RuntimeCapabilityError("runtime profile file is too large")
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise RuntimeCapabilityError(f"runtime profile could not be loaded: {exc}") from exc
    return normalize_runtime_profile(value)


def create_development_capability(
    profile_path: Path,
    *,
    session_id: str,
    client_id: str,
    build_id: str,
    client_build: str,
    lease_seconds: float = 24 * 60 * 60,
) -> RuntimeCapability:
    profile = load_runtime_profile_file(profile_path)
    issued_at = time.time()
    lease = min(
        MAX_DEVELOPMENT_LEASE_SECONDS,
        max(60.0, float(lease_seconds)),
    )
    value = {
        "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
        "capability_id": secrets.token_hex(16),
        "session_id": session_id,
        "client_id": client_id,
        "build_id": build_id,
        "client_build": client_build,
        "lease_sequence": 1,
        "issued_at": issued_at,
        "expires_at": issued_at + lease,
        "profile_digest": runtime_profile_digest(profile),
        "development": True,
        "signing_key_id": "",
        "signature": "",
        "profile": runtime_profile_to_wire(profile),
    }
    return RuntimeCapability.from_value(
        value,
        expected_session_id=session_id,
        expected_client_id=client_id,
        expected_build_id=build_id,
        expected_client_build=client_build,
        allow_development=True,
    )


def create_server_capability(
    profile: Mapping[str, object],
    *,
    session_id: str,
    client_id: str,
    build_id: str,
    client_build: str,
    lease_sequence: int,
    signing_key_id: str,
    signing_private_key: object,
    lease_seconds: float = 120.0,
    now: float | None = None,
) -> RuntimeCapability:
    """Issue one signed server lease without embedding any server secret."""
    normalized = normalize_runtime_profile(profile)
    private_key = load_capability_private_key(signing_private_key)
    issued_at = time.time() if now is None else float(now)
    lease = min(
        MAX_SERVER_LEASE_SECONDS,
        max(60.0, float(lease_seconds)),
    )
    value = {
        "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
        "capability_id": secrets.token_hex(16),
        "session_id": session_id,
        "client_id": client_id,
        "build_id": build_id,
        "client_build": client_build,
        "lease_sequence": int(lease_sequence),
        "issued_at": issued_at,
        "expires_at": issued_at + lease,
        "profile_digest": runtime_profile_digest(normalized),
        "development": False,
        "signing_key_id": signing_key_id,
        "profile": runtime_profile_to_wire(normalized),
    }
    value["signature"] = base64.b64encode(
        private_key.sign(runtime_capability_signing_payload(value))
    ).decode("ascii")
    trusted_public_keys = {
        signing_key_id: capability_public_key_to_base64(private_key.public_key())
    }
    return RuntimeCapability.from_value(
        value,
        expected_session_id=session_id,
        expected_client_id=client_id,
        expected_build_id=build_id,
        expected_client_build=client_build,
        trusted_public_keys=trusted_public_keys,
        allow_development=False,
        now=issued_at,
    )
