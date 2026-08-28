#!/usr/bin/env python3
"""Keep one licensing identity across source and packaged launches."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path


CLIENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def normalize_client_id(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if CLIENT_ID_PATTERN.fullmatch(candidate) else ""


def _client_id_from_config(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(value, dict):
        return ""
    return normalize_client_id(value.get("client_id"))


def _write_identity(path: Path, client_id: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(client_id + "\n", encoding="ascii")
        os.replace(temporary, path)
    except OSError:
        pass


def resolve_client_id(
    config: dict,
    identity_path: Path,
    shared_config_path: Path | None = None,
) -> str:
    """Resolve and persist a stable 32-character client identifier."""
    try:
        persisted = normalize_client_id(identity_path.read_text(encoding="ascii"))
    except OSError:
        persisted = ""
    client_id = (
        persisted
        or _client_id_from_config(shared_config_path)
        or normalize_client_id(config.get("client_id"))
        or uuid.uuid4().hex
    )
    config["client_id"] = client_id
    _write_identity(identity_path, client_id)
    return client_id
