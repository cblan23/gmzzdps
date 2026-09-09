#!/usr/bin/env python3
"""Select the capture implementation embedded in this application build.

Source checkouts and normal releases keep using the established capture
process.  The isolated Npcap build contains ``_capture_variant.json`` and
loads only the passive Npcap implementation.  Keeping the selection here
prevents the Npcap executable from importing (or needing to bundle) any of the
legacy hook capture modules.
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
    CAPTURE_VARIANT.get("backend", "legacy") or "legacy"
).strip().casefold()
IS_NPCAP_BACKEND = CAPTURE_BACKEND_NAME == "npcap"
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

_module_name = "npcap_capture_process" if IS_NPCAP_BACKEND else "capture_process"
_implementation = importlib.import_module(_module_name)
CaptureProcessClient = _implementation.CaptureProcessClient
