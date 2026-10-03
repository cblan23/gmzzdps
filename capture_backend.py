#!/usr/bin/env python3
"""v0.3.0 passive capture entry. Old manifests cannot re-enable process hooks.

The standard application retains its existing user-data directory and identity.
Explicit isolated build metadata can still select a separate user-data directory.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


TEAM_STATS_MODE_UNKNOWN = 0
TEAM_STATS_MODE_DUMMY = 1
TEAM_STATS_MODE_TEAM = 2
TEAM_STATS_MODE_NAMES = {
    TEAM_STATS_MODE_UNKNOWN: "unknown",
    TEAM_STATS_MODE_DUMMY: "dummy",
    TEAM_STATS_MODE_TEAM: "team",
}
TEAM_STATS_MODE_CODES = {
    name: code for code, name in TEAM_STATS_MODE_NAMES.items()
}


def normalize_team_stats_mode(
    value: object, *, default: int = TEAM_STATS_MODE_TEAM
) -> int:
    """Return the shared numeric scene mode used by both capture backends."""
    if isinstance(value, str):
        return TEAM_STATS_MODE_CODES.get(value.strip().casefold(), int(default))
    try:
        candidate = int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)
    return candidate if candidate in TEAM_STATS_MODE_NAMES else int(default)


def team_stats_mode_name(value: object) -> str:
    return TEAM_STATS_MODE_NAMES.get(
        normalize_team_stats_mode(value),
        TEAM_STATS_MODE_NAMES[TEAM_STATS_MODE_TEAM],
    )


def _bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _load_variant() -> dict[str, object]:
    path = _bundle_dir() / "_capture_variant.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


CAPTURE_VARIANT = _load_variant()
CAPTURE_BACKEND_NAME = str(
    CAPTURE_VARIANT.get("backend", "windows_raw") or "windows_raw"
).strip().casefold()
if CAPTURE_BACKEND_NAME != "windows_raw":
    raise RuntimeError(
        f"Unsupported capture backend: {CAPTURE_BACKEND_NAME}; "
        "v0.3.0 requires the built-in Windows Raw Socket backend"
    )
# The protocol decoder is shared with the historical receive-only parser; the
# transport selected below is always the Windows built-in source.
IS_PASSIVE_PROTOCOL_ADAPTER = True
# Compatibility export for older diagnostics. It is permanently false and is
# never consulted for source selection.
IS_NPCAP_BACKEND = False
IS_WINDOWS_RAW_BACKEND = True
CAPTURE_DISPLAY_VERSION = str(
    CAPTURE_VARIANT.get("display_version", "") or ""
).strip()
CAPTURE_DATA_DIRECTORY = str(
    CAPTURE_VARIANT.get("data_directory", "") or ""
).strip()
CAPTURE_MUTEX_NAME = str(
    CAPTURE_VARIANT.get("mutex_name", "") or ""
).strip()
CAPTURE_TRAY_CLASS_PREFIX = str(
    CAPTURE_VARIANT.get("tray_class_prefix", "") or ""
).strip()

_implementation = importlib.import_module("windows_capture_process")
CaptureProcessClient = _implementation.CaptureProcessClient
