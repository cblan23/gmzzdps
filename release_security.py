"""Release identity, resource integrity, and Windows publisher verification.

The private signing key is deliberately absent from this module. Official
builds embed only a public release identity and certificate thumbprints; the
build script accesses the private key through the Windows certificate store.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import binascii
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


RELEASE_IDENTITY_FILENAME = "_release_identity.json"
BUILD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CERTIFICATE_THUMBPRINT_PATTERN = re.compile(r"^[0-9A-F]{40}(?:[0-9A-F]{24})?$")
RESOURCE_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CAPABILITY_SIGNING_KEY_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,63}$"
)
MAX_RELEASE_RESOURCES = 4096
MAX_RELEASE_IDENTITY_BYTES = 512 * 1024
MAX_CAPABILITY_PUBLIC_KEYS = 8


def _normalized_thumbprints(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        thumbprint = re.sub(r"\s+", "", str(value or "")).upper()
        if CERTIFICATE_THUMBPRINT_PATTERN.fullmatch(thumbprint):
            result.append(thumbprint)
    return tuple(dict.fromkeys(result))


def _normalized_capability_public_keys(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > MAX_CAPABILITY_PUBLIC_KEYS:
        raise ValueError("release capability public keys are invalid")
    result: dict[str, str] = {}
    for raw_key_id, raw_public_key in value.items():
        key_id = str(raw_key_id or "").strip()
        public_key = str(raw_public_key or "").strip()
        if not CAPABILITY_SIGNING_KEY_ID_PATTERN.fullmatch(key_id):
            raise ValueError("release capability signing key ID is invalid")
        try:
            decoded = base64.b64decode(public_key.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError("release capability public key is invalid") from exc
        if len(decoded) != 32:
            raise ValueError("release capability public key is invalid")
        result[key_id] = public_key
    return result


@dataclass(frozen=True)
class ReleaseIdentity:
    build_id: str = "source-development"
    version: str = ""
    client_build: str = ""
    official: bool = False
    protected: bool = False
    publisher_thumbprints: tuple[str, ...] = ()
    capability_public_keys: dict[str, str] = field(default_factory=dict)
    resource_hashes: dict[str, str] = field(default_factory=dict)
    identity_path: Path | None = None
    error: str = ""

    @property
    def valid_build(self) -> bool:
        return bool(
            BUILD_ID_PATTERN.fullmatch(self.build_id)
            and self.version
            and self.client_build
            and not self.error
        )

    @property
    def valid_official(self) -> bool:
        return bool(
            self.official
            and self.valid_build
            and self.publisher_thumbprints
        )

    @property
    def valid_protected(self) -> bool:
        return bool(
            self.protected
            and self.valid_build
            and self.capability_public_keys
        )


@dataclass(frozen=True)
class PublisherVerification:
    verified: bool
    status: str = ""
    thumbprint: str = ""
    subject: str = ""
    message: str = ""


def load_release_identity(base_dir: Path | None = None) -> ReleaseIdentity:
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    path = root / RELEASE_IDENTITY_FILENAME
    if not path.is_file():
        return ReleaseIdentity(identity_path=path)
    try:
        if path.stat().st_size > MAX_RELEASE_IDENTITY_BYTES:
            raise ValueError("release identity is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or int(value.get("schema_version", 0)) not in {
            1,
            2,
        }:
            raise ValueError("release identity schema is invalid")
        build_id = str(value.get("build_id", "")).strip().lower()
        version = str(value.get("version", "")).strip()[:32]
        client_build = str(value.get("client_build", "")).strip()[:64]
        official = bool(value.get("official"))
        protected = bool(value.get("protected"))
        thumbprints = _normalized_thumbprints(
            value.get("publisher_thumbprints", [])
            if isinstance(value.get("publisher_thumbprints"), list)
            else []
        )
        capability_public_keys = _normalized_capability_public_keys(
            value.get("capability_public_keys", {})
        )
        raw_hashes = value.get("resource_hashes", {})
        if not isinstance(raw_hashes, dict) or len(raw_hashes) > MAX_RELEASE_RESOURCES:
            raise ValueError("release resource inventory is invalid")
        resource_hashes: dict[str, str] = {}
        for raw_name, raw_digest in raw_hashes.items():
            name = str(raw_name).replace("\\", "/").strip("/")
            digest = str(raw_digest).strip().lower()
            parts = Path(name).parts
            if (
                not name
                or Path(name).is_absolute()
                or ".." in parts
                or not RESOURCE_DIGEST_PATTERN.fullmatch(digest)
            ):
                raise ValueError("release resource inventory contains invalid data")
            resource_hashes[name] = digest
        identity = ReleaseIdentity(
            build_id=build_id,
            version=version,
            client_build=client_build,
            official=official,
            protected=protected,
            publisher_thumbprints=thumbprints,
            capability_public_keys=capability_public_keys,
            resource_hashes=resource_hashes,
            identity_path=path,
        )
        if official and not identity.valid_official:
            raise ValueError("official release identity is incomplete")
        if protected and not identity.valid_protected:
            raise ValueError("protected release identity is incomplete")
        return identity
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        return ReleaseIdentity(identity_path=path, error=str(exc)[:160])


def verify_release_resources(
    identity: ReleaseIdentity,
    base_dir: Path | None = None,
) -> tuple[bool, str]:
    """Verify the immutable inventory embedded in a protected or signed build."""
    if not (identity.official or identity.protected):
        return True, "development release"
    if identity.official and not identity.valid_official:
        return False, identity.error or "official release identity is invalid"
    if identity.protected and not identity.valid_protected:
        return False, identity.error or "protected release identity is invalid"
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    for name, expected in sorted(identity.resource_hashes.items()):
        path = (root / Path(name)).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return False, f"resource path escaped release root: {name}"
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False, f"release resource is missing: {name}"
        if not hmac.compare_digest(digest, expected):
            return False, f"release resource was modified: {name}"
    return True, ""


def _windows_powershell_path() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def verify_windows_publisher(
    path: Path,
    expected_thumbprints: Iterable[object],
    *,
    require_trusted_chain: bool = True,
) -> PublisherVerification:
    """Verify Authenticode and pin the signer certificate thumbprint."""
    expected = set(_normalized_thumbprints(expected_thumbprints))
    target = Path(path).resolve()
    if sys.platform != "win32":
        return PublisherVerification(False, message="publisher verification requires Windows")
    if not expected:
        return PublisherVerification(False, message="no trusted publisher is configured")
    if not target.is_file():
        return PublisherVerification(False, message="signed file does not exist")
    powershell = _windows_powershell_path()
    if not powershell.is_file():
        return PublisherVerification(False, message="Windows signature verifier is unavailable")
    environment = os.environ.copy()
    environment["GMZZ_SIGNATURE_TARGET"] = str(target)
    command = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:GMZZ_SIGNATURE_TARGET;"
        "$o=[ordered]@{Status=[string]$s.Status;StatusMessage=[string]$s.StatusMessage;"
        "Thumbprint=if($s.SignerCertificate){[string]$s.SignerCertificate.Thumbprint}else{''};"
        "Subject=if($s.SignerCertificate){[string]$s.SignerCertificate.Subject}else{''}};"
        "$o|ConvertTo-Json -Compress"
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=environment,
            creationflags=creation_flags,
        )
        value = json.loads(completed.stdout.strip() or "{}")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return PublisherVerification(False, message=f"signature verification failed: {exc}")
    if not isinstance(value, dict):
        return PublisherVerification(False, message="signature verifier returned invalid data")
    status = str(value.get("Status", "")).strip()
    thumbprint = re.sub(r"\s+", "", str(value.get("Thumbprint", ""))).upper()
    subject = str(value.get("Subject", "")).strip()[:256]
    status_message = str(value.get("StatusMessage", "")).strip()[:256]
    trusted = status.casefold() == "valid" if require_trusted_chain else bool(thumbprint)
    if not trusted:
        return PublisherVerification(
            False,
            status=status,
            thumbprint=thumbprint,
            subject=subject,
            message=status_message or f"signature status is {status or 'unknown'}",
        )
    if thumbprint not in expected:
        return PublisherVerification(
            False,
            status=status,
            thumbprint=thumbprint,
            subject=subject,
            message="publisher certificate is not trusted for this release",
        )
    return PublisherVerification(
        True,
        status=status,
        thumbprint=thumbprint,
        subject=subject,
    )
