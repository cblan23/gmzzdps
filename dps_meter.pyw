#!/usr/bin/env python3
"""当前 C7 游戏版本的悬浮伤害统计窗口。"""

from __future__ import annotations

import datetime as dt
import json
import math
import multiprocessing
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageTk

from capture_process import CaptureProcessClient
from combat_history import CombatHistoryStore, HISTORY_SCHEMA_VERSION
from device_identity import resolve_client_id
from licensing import (
    DEFAULT_SERVER_URL,
    LicensingConnectionError,
    LicensingService,
    ServerLicensingGateway,
    UpdateInfo,
)
from monster_metadata import load_monster_metadata, resolve_localization_names
from network_state import (
    BOSS_PHASE_TEMPLATE_TRANSITIONS,
    ENCOUNTER_AUXILIARY_TEMPLATES,
    ENCOUNTER_NON_BOSS_TEMPLATE_IDS,
    EXPLICIT_NON_TYPE3_BOSS_TEMPLATE_IDS,
    MAX_PARTY_MEMBERS,
    NetworkPacketParser,
    SEPARATE_BOSS_ENCOUNTER_TRANSITIONS,
    boss_name_is_placeholder,
    unique_inferred_auxiliary_for_parent,
)
from remembered_card import remember_card, remembered_card
from single_instance import SingleInstanceGuard, activate_existing_instance

if sys.platform == "win32":
    from windows_tray import WindowsTrayIcon
else:
    WindowsTrayIcon = None


BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
IS_FROZEN = bool(
    getattr(sys, "frozen", False)
    or "__compiled__" in globals()
    or (
        bool(sys.argv)
        and Path(str(sys.argv[0])).suffix.casefold() == ".exe"
    )
)


def resolve_program_path(
    frozen: bool,
    argv0: object,
    executable: object,
    source_path: object,
    compiled: object = None,
) -> Path:
    """Return the outer onefile executable rather than its temporary payload."""
    if not frozen:
        return Path(str(source_path)).resolve()
    original_argv0 = getattr(compiled, "original_argv0", "")
    for candidate in (original_argv0, argv0):
        text = str(candidate or "").strip()
        if text:
            return Path(text).expanduser().resolve()
    return Path(str(executable)).expanduser().resolve()


APP_EXECUTABLE_PATH = resolve_program_path(
    IS_FROZEN,
    sys.argv[0] if sys.argv else "",
    sys.executable,
    __file__,
    globals().get("__compiled__"),
)
APP_DIR = APP_EXECUTABLE_PATH.parent if IS_FROZEN else BUNDLE_DIR
SHARED_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "GMZZDpsMeter"
DATA_DIR = (
    SHARED_DATA_DIR
    if IS_FROZEN
    else APP_DIR
)
CONFIG_PATH = DATA_DIR / "dps_config.json"
SHARED_CONFIG_PATH = SHARED_DATA_DIR / "dps_config.json"
DEVICE_ID_PATH = SHARED_DATA_DIR / "device_id"
SKILL_NAMES_PATH = BUNDLE_DIR / "skill_names.json"
SKILL_METADATA_PATH = BUNDLE_DIR / "skill_metadata.json"
MONSTER_METADATA_PATH = BUNDLE_DIR / "monster_metadata.json"
BOSS_NAME_ALLOWLIST_PATH = BUNDLE_DIR / "boss_allowlist.txt"
ASSET_DIR = BUNDLE_DIR / "assets"
ICON_SOURCES_PATH = ASSET_DIR / "icon_sources.json"
APP_LOGO_PATH = ASSET_DIR / "app_logo.png"
APP_ICON_PATH = ASSET_DIR / "app_icon.ico"
LOG_DIR = DATA_DIR / "logs"
HISTORY_DIR = DATA_DIR / "combat_history"
TEAM_PROFILE_CACHE_PATH = DATA_DIR / "team_profiles.json"
SELF_IDENTITY_CACHE_PATH = DATA_DIR / "network_self_identity.json"
ACTIVE_BOSS_CACHE_PATH = DATA_DIR / "network_active_boss.json"
MONSTER_NAME_CACHE_PATH = DATA_DIR / "monster_name_cache.json"
UPDATE_DIR = APP_DIR

APP_NAME = "叨叨诡秘 Dps-Logs"
APP_VERSION = "0.0.14"
CLIENT_BUILD = "0.0.14+20260831.2"
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
UI_BRAND = APP_NAME
BG = "#08090b"
SURFACE = "#0f1114"
PANEL = "#141619"
PANEL_2 = "#1a1d21"
BORDER = "#2b2f34"
TEXT = "#f4f6f8"
MUTED = "#8d99a8"
SUBTLE = "#556170"
ACCENT = "#6fe3bd"
WARN = "#f0bc72"
ERROR = "#ef6b73"
BACKEND_WIDTH = 1180
BACKEND_HEIGHT = 760
MAIN_MIN_WIDTH = 430
MAIN_MIN_HEIGHT = 260
MINI_DEFAULT_WIDTH = 340
MINI_DEFAULT_HEIGHT = 118
MINI_MIN_WIDTH = 188
MINI_MIN_HEIGHT = 86
MINI_WIDTH_BY_VISIBLE_METRICS = (188, 220, 260, 300, 340)
MINI_ACTION_AREA_WIDTH = 66
MIN_WINDOW_ALPHA_PERCENT = 55
MAIN_CONTENT_OVERLAY_KEY = "#010203"
WINDOW_EXSTYLE_TRANSPARENT = 0x00000020
WINDOW_EXSTYLE_TOOLWINDOW = 0x00000080
WINDOW_EXSTYLE_LAYERED = 0x00080000
WINDOW_EXSTYLE_NOACTIVATE = 0x08000000
CARD_MEMBERSHIP_LABELS = {
    "normal": "尊贵的用户",
    "weekly": "VIP用户",
    "monthly": "VVVVVIP用户",
    "partner": "莫雪的小伙伴",
}

PROFESSION_COLORS = {
    1_200_001: "#f2cd32",
    1_200_002: "#7ecfa5",
    1_200_003: "#5869c4",
    1_200_004: "#6687c5",
    1_200_005: "#68b6e5",
    1_200_006: "#ee8c2f",
    1_200_007: "#a255c7",
}

TEAM_TARGET_ACTIVE_SECONDS = 10.0
MONSTER_DISPLAY_ACTIVE_SECONDS = 30.0
LICENSE_HEARTBEAT_FAILURE_GRACE_SECONDS = 50.0
UNVERIFIED_MEMBER_EVENT_WINDOW_SECONDS = 90.0
UNKNOWN_TARGET_EVENT_WINDOW_SECONDS = 3.0
UNKNOWN_TARGET_EVENT_LIMIT_PER_TARGET = 128
UNKNOWN_TARGET_EVENT_LIMIT_GLOBAL = 1024
STAGE_SUMMARY_SELF_DAMAGE_TOLERANCE = 150_000
STAGE_SUMMARY_FINAL_WINDOW_SECONDS = 120.0
STAGE_SUMMARY_CONFIRMED_FINAL_WINDOW_SECONDS = 300.0
STAGE_SUMMARY_TOTAL_TOLERANCE_RATIO = 0.35
STAGE_SUMMARY_TOTAL_TOLERANCE_ABSOLUTE = 25_000
DUMMY_NAME_MARKER = "木桩"
MESSAGE_DRAIN_BATCH_SIZE = 64
MESSAGE_DRAIN_TIME_BUDGET_SECONDS = 0.012
CONTROL_MESSAGE_DRAIN_BATCH_SIZE = 64
CLOSE_DRAIN_TIME_BUDGET_SECONDS = 0.008
CLOSE_HEARTBEAT_GRACE_SECONDS = 1.0
CAPTURE_PROCESS_SLOW_SHUTDOWN_SECONDS = 10.0
MULTIPHASE_BOSS_TEMPLATE_IDS = frozenset(
    int(parent_template_id)
    for auxiliary in ENCOUNTER_AUXILIARY_TEMPLATES.values()
    if auxiliary.get("keeps_encounter_alive")
    for parent_template_id in auxiliary.get("parent_template_ids", ())
    if int(parent_template_id)
)
FEEDBACK_CATEGORIES = (
    ("DPS 统计", "dps"),
    ("BOSS 识别", "boss"),
    ("队伍成员", "team"),
    ("界面显示", "ui"),
    ("登录或连接", "connection"),
    ("其他问题", "other"),
)


def preferred_font_family(
    available_families: object, candidates: tuple[str, ...]
) -> str:
    """Return the first installed family while preserving its registered case."""
    available = {
        str(family).casefold(): str(family)
        for family in (available_families or ())
        if str(family).strip()
    }
    for candidate in candidates:
        installed = available.get(candidate.casefold())
        if installed:
            return installed
    return candidates[-1]


def window_exstyle_for_lock(
    style: int, locked: bool, original_style: int | None = None
) -> int:
    style = int(style)
    if locked:
        return style | WINDOW_EXSTYLE_TRANSPARENT | WINDOW_EXSTYLE_LAYERED
    if original_style is None:
        return style & ~WINDOW_EXSTYLE_TRANSPARENT
    managed_bits = WINDOW_EXSTYLE_TRANSPARENT | WINDOW_EXSTYLE_LAYERED
    return (style & ~managed_bits) | (int(original_style) & managed_bits)


def membership_label_for_card_tier(card_tier: object) -> str:
    tier = str(card_tier or "").strip().casefold()
    return CARD_MEMBERSHIP_LABELS.get(tier, CARD_MEMBERSHIP_LABELS["normal"])


def is_process_elevated(
    platform: object = None,
    admin_probe=None,
) -> bool:
    """Return whether the current Windows process has an elevated token."""
    current_platform = sys.platform if platform is None else str(platform)
    if current_platform != "win32":
        return True
    try:
        if admin_probe is None:
            import ctypes

            admin_probe = ctypes.windll.shell32.IsUserAnAdmin
        return bool(admin_probe())
    except (AttributeError, OSError, TypeError, ValueError):
        return False


CHINESE_NUMBERS = (
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
    "十八",
    "十九",
    "二十",
)


def format_number(value: float | int) -> str:
    value = int(round(value))
    if value < 1_000_000:
        return f"{value:,}"
    if value < 100_000_000:
        return f"{value / 10_000:.1f}万"
    return f"{value / 100_000_000:.2f}亿"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def relative_damage_bar_ratio(damage: object, highest_damage: object) -> float:
    try:
        value = max(0.0, float(damage))
        maximum = float(highest_damage)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(value) or not math.isfinite(maximum) or maximum <= 0:
        return 0.0
    return max(0.0, min(1.0, value / maximum))


def compact_width_for_visible_metrics(count: object) -> int:
    try:
        index = int(count)
    except (TypeError, ValueError, OverflowError):
        index = 0
    index = min(len(MINI_WIDTH_BY_VISIBLE_METRICS) - 1, max(0, index))
    return MINI_WIDTH_BY_VISIBLE_METRICS[index]


def chinese_number(index: int) -> str:
    if 1 <= index <= len(CHINESE_NUMBERS):
        return CHINESE_NUMBERS[index - 1]
    return str(index)


def chinese_error_message(value) -> str:
    text = str(value).strip()
    lower = text.casefold()
    if "process not found" in lower:
        return "游戏未运行"
    if "version/signature mismatch" in lower or "target rva is outside" in lower:
        return "游戏连接正在重新尝试。"
    if "openprocess" in lower:
        return "无法连接游戏进程，请确认游戏已启动。"
    if "ring buffer" in lower:
        return "战斗数据缓冲区异常，正在尝试重新连接。"
    first_line = text.splitlines()[0] if text else "未知错误"
    return f"连接异常：{first_line}"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def load_skill_catalog() -> dict:
    """Load the client-derived, exact skill ID to Chinese name catalog."""
    try:
        value = json.loads(SKILL_NAMES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_skill_metadata() -> dict:
    try:
        value = json.loads(SKILL_METADATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"professions": {}, "skills": {}}
    if not isinstance(value, dict):
        return {"professions": {}, "skills": {}}
    return value


def load_icon_sources() -> dict:
    try:
        value = json.loads(ICON_SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json_object(path: Path, value: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass


def normalize_boss_name(value: object) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


def load_boss_name_allowlist(path: Path = BOSS_NAME_ALLOWLIST_PATH) -> tuple[str, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    return tuple(
        dict.fromkeys(
            normalized
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
            if (normalized := normalize_boss_name(line))
        )
    )


def boss_name_is_allowed(name: object, allowlist: tuple[str, ...]) -> bool:
    normalized = normalize_boss_name(name)
    return bool(normalized and any(item in normalized for item in allowlist))


BOSS_PHASE_NAME_GROUPS = (
    frozenset(
        {
            normalize_boss_name("先祖铠甲"),
            normalize_boss_name("伯德温·威瑟尔"),
        }
    ),
)


def boss_names_share_phase(left: object, right: object) -> bool:
    left_name = normalize_boss_name(left)
    right_name = normalize_boss_name(right)
    return bool(
        left_name
        and right_name
        and any(
            any(alias in left_name for alias in group)
            and any(alias in right_name for alias in group)
            for group in BOSS_PHASE_NAME_GROUPS
        )
    )


def boss_phase_continues(left: object, right: object) -> bool:
    left_name = normalize_boss_name(left)
    right_name = normalize_boss_name(right)
    ancestor = normalize_boss_name("先祖铠甲")
    baldwin = normalize_boss_name("伯德温·威瑟尔")
    return bool(
        left_name
        and right_name
        and ancestor in left_name
        and baldwin in right_name
    )


def load_monster_catalog() -> dict[str, dict]:
    catalog = load_monster_metadata(MONSTER_METADATA_PATH)
    cached_names = load_json_object(MONSTER_NAME_CACHE_PATH)
    for template_id, name in cached_names.items():
        if isinstance(name, str) and name.strip():
            catalog.setdefault(str(template_id), {})["name"] = name.strip()
    allowlist = load_boss_name_allowlist()
    if allowlist:
        filtered: dict[str, dict] = {}
        for template_id, metadata in catalog.items():
            try:
                parsed_template_id = int(template_id)
            except (TypeError, ValueError, OverflowError):
                parsed_template_id = 0
            if parsed_template_id in ENCOUNTER_NON_BOSS_TEMPLATE_IDS:
                continue
            if not boss_name_is_allowed(metadata.get("name", ""), allowlist):
                continue
            try:
                metadata_boss_type = int(metadata.get("boss_type", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                metadata_boss_type = 0
            if (
                metadata_boss_type != 3
                and parsed_template_id
                not in EXPLICIT_NON_TYPE3_BOSS_TEMPLATE_IDS
            ):
                continue
            boss_metadata = dict(metadata)
            boss_metadata["boss_type"] = 3
            filtered[template_id] = boss_metadata
        for template_id, auxiliary in ENCOUNTER_AUXILIARY_TEMPLATES.items():
            metadata = dict(catalog.get(str(template_id), {}))
            metadata.update(
                {
                    "name": auxiliary["name"],
                    "encounter_auxiliary": True,
                    "encounter_parent_template_ids": list(
                        auxiliary["parent_template_ids"]
                    ),
                }
            )
            filtered[str(template_id)] = metadata
        return filtered
    return catalog


def _catalog_boss_name(
    catalog: object,
    template_id: object,
) -> str:
    if not isinstance(catalog, dict):
        return ""
    try:
        parsed_template_id = int(template_id or 0)
    except (TypeError, ValueError, OverflowError):
        return ""
    metadata = catalog.get(str(parsed_template_id), {})
    if not isinstance(metadata, dict):
        return ""
    name = str(metadata.get("name", "")).strip()
    return name if name and not boss_name_is_placeholder(name) else ""


def restore_history_boss_names(record: object, catalog: object) -> object:
    """Return a display-only history record with placeholder Boss names fixed."""
    if not isinstance(record, dict) or not isinstance(catalog, dict):
        return record

    changed = False
    updated = dict(record)
    canonical_by_entity: dict[int, str] = {}
    primary_name = ""

    raw_monster = record.get("monster")
    if isinstance(raw_monster, dict):
        primary_name = _catalog_boss_name(
            catalog, raw_monster.get("template_id", 0)
        )
        try:
            primary_entity_id = int(raw_monster.get("entity_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            primary_entity_id = 0
        if primary_entity_id and primary_name:
            canonical_by_entity[primary_entity_id] = primary_name
        if primary_name and boss_name_is_placeholder(raw_monster.get("name", "")):
            monster = dict(raw_monster)
            monster["name"] = primary_name
            updated["monster"] = monster
            changed = True

    raw_targets = record.get("targets")
    if isinstance(raw_targets, list):
        targets: list[object] = []
        targets_changed = False
        for raw_target in raw_targets:
            if not isinstance(raw_target, dict):
                targets.append(raw_target)
                continue
            try:
                entity_id = int(raw_target.get("entity_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                entity_id = 0
            canonical_name = _catalog_boss_name(
                catalog, raw_target.get("template_id", 0)
            ) or canonical_by_entity.get(entity_id, "")
            if entity_id and canonical_name:
                canonical_by_entity[entity_id] = canonical_name
            if canonical_name and boss_name_is_placeholder(
                raw_target.get("name", "")
            ):
                target = dict(raw_target)
                target["name"] = canonical_name
                targets.append(target)
                targets_changed = True
            else:
                targets.append(raw_target)
        if targets_changed:
            updated["targets"] = targets
            changed = True

    raw_participants = record.get("participants")
    if isinstance(raw_participants, list):
        participants: list[object] = []
        participants_changed = False
        for raw_participant in raw_participants:
            if not isinstance(raw_participant, dict):
                participants.append(raw_participant)
                continue
            raw_actor_targets = raw_participant.get("targets")
            if not isinstance(raw_actor_targets, list):
                participants.append(raw_participant)
                continue
            actor_targets: list[object] = []
            actor_targets_changed = False
            for raw_target in raw_actor_targets:
                if not isinstance(raw_target, dict) or not boss_name_is_placeholder(
                    raw_target.get("name", "")
                ):
                    actor_targets.append(raw_target)
                    continue
                candidate_names: set[str] = set()
                try:
                    entity_id = int(raw_target.get("entity_id", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    entity_id = 0
                if entity_id and entity_id in canonical_by_entity:
                    candidate_names.add(canonical_by_entity[entity_id])
                raw_entity_ids = raw_target.get("entity_ids", ())
                if isinstance(raw_entity_ids, (list, tuple, set)):
                    for raw_entity_id in raw_entity_ids:
                        try:
                            grouped_entity_id = int(raw_entity_id or 0)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if grouped_entity_id in canonical_by_entity:
                            candidate_names.add(
                                canonical_by_entity[grouped_entity_id]
                            )
                if (
                    not candidate_names
                    and str(raw_target.get("kind", "")).casefold() == "boss"
                    and primary_name
                ):
                    candidate_names.add(primary_name)
                if len(candidate_names) == 1:
                    target = dict(raw_target)
                    target["name"] = next(iter(candidate_names))
                    actor_targets.append(target)
                    actor_targets_changed = True
                else:
                    actor_targets.append(raw_target)
            if actor_targets_changed:
                participant = dict(raw_participant)
                participant["targets"] = actor_targets
                participants.append(participant)
                participants_changed = True
            else:
                participants.append(raw_participant)
        if participants_changed:
            updated["participants"] = participants
            changed = True

    return updated if changed else record


def save_config(config: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


@dataclass
class SkillStats:
    skill_id: int
    damage: int = 0
    hits: int = 0
    max_hit: int = 0
    first_time: float = 0.0
    last_time: float = 0.0

    def add(self, damage: int, event_time: float) -> None:
        self.damage += damage
        self.hits += 1
        self.max_hit = max(self.max_hit, damage)
        if not self.first_time:
            self.first_time = event_time
        self.last_time = event_time


@dataclass
class ActorStats:
    actor_id: int
    damage: int = 0
    hits: int = 0
    max_hit: int = 0
    first_time: float = 0.0
    last_time: float = 0.0
    skills: dict[int, SkillStats] = field(default_factory=dict)
    target_damage: dict[int, int] = field(default_factory=dict)
    damage_hits: int | None = None
    critical_hits: int | None = None

    def add(
        self,
        damage: int,
        event_time: float,
        skill_id: int,
        target_id: int = 0,
        critical: bool | None = None,
    ) -> None:
        self.damage += damage
        self.hits += 1
        self.max_hit = max(self.max_hit, damage)
        if not self.first_time:
            self.first_time = event_time
        self.last_time = event_time
        skill = self.skills.setdefault(skill_id, SkillStats(skill_id))
        skill.add(damage, event_time)
        if target_id:
            self.target_damage[target_id] = (
                self.target_damage.get(target_id, 0) + damage
            )
        if critical is not None:
            self.damage_hits = (self.damage_hits or 0) + 1
            self.critical_hits = (self.critical_hits or 0) + int(critical)


@dataclass
class MonsterStats:
    entity_id: int
    name: str = ""
    entity_type: str = ""
    template_id: int | None = None
    level: int | None = None
    boss_type: int | None = None
    boss_rank: int = 0
    encounter_auxiliary: bool = False
    encounter_parent_template_ids: tuple[int, ...] = ()
    current_hp: float | None = None
    max_hp: float | None = None
    observed_max_hp: float | None = None
    last_update_100ns: int = 0
    last_hp_drop_100ns: int = 0
    death_time_100ns: int = 0
    death_confirmed: bool = False


@dataclass
class TeamDamageState:
    actor_id: int
    last_absolute: int = 0
    baseline_absolute: int = 0
    has_snapshot: bool = False
    authoritative_snapshot: bool = False
    accepted_damage: int = 0
    snapshot_time_100ns: int = 0
    baseline_snapshot_time_100ns: int = 0
    server_time: int = 0
    first_time: float = 0.0
    last_time: float = 0.0


@dataclass
class StageSkillSnapshot:
    summary_id: str
    actor_id: int
    actor_damage: int
    filetime_100ns: int
    skills: dict[int, tuple[int, int]]
    unclassified_damage: int = 0


class CombatModel:
    def __init__(
        self,
        *,
        skill_names: dict[str, str] | None = None,
        runtime_skill_names: dict[str, str] | None = None,
        skill_professions: dict[str, list[int]] | None = None,
        entity_names: dict[str, str] | None = None,
        local_player_name: str = "",
        run_id: str | None = None,
        boss_only: bool = True,
    ):
        self.skill_names: dict[int, str] = {}
        for skill_id, name in (skill_names or {}).items():
            try:
                parsed_id = int(skill_id)
            except (TypeError, ValueError):
                continue
            cleaned_name = str(name).strip()
            if parsed_id and cleaned_name:
                self.skill_names[parsed_id] = cleaned_name[:64]
        self.runtime_skill_names: dict[int, str] = {}
        for skill_id, name in (runtime_skill_names or {}).items():
            try:
                parsed_id = int(skill_id)
            except (TypeError, ValueError):
                continue
            cleaned_name = str(name).strip()
            if parsed_id and cleaned_name and parsed_id not in self.skill_names:
                self.runtime_skill_names[parsed_id] = cleaned_name[:64]
        self.skill_professions: dict[int, tuple[int, ...]] = {}
        for skill_id, class_ids in (skill_professions or {}).items():
            try:
                parsed_id = int(skill_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(class_ids, (list, tuple)):
                continue
            parsed_classes: list[int] = []
            for class_id in class_ids:
                try:
                    value = int(class_id)
                except (TypeError, ValueError):
                    continue
                if 1_200_001 <= value <= 1_200_007:
                    parsed_classes.append(value)
            if parsed_id and parsed_classes:
                self.skill_professions[parsed_id] = tuple(sorted(set(parsed_classes)))
        self.entity_names: dict[int, str] = {}
        self.entity_professions: dict[int, int] = {}
        for entity_id, name in (entity_names or {}).items():
            try:
                parsed_id = int(entity_id)
            except (TypeError, ValueError):
                continue
            cleaned_name = str(name).strip()
            if parsed_id and cleaned_name:
                self.entity_names[parsed_id] = cleaned_name[:64]
        self.local_player_name = str(local_player_name).strip()[:64]
        self.boss_only = bool(boss_only)
        self.self_id: int | None = None
        self.party_ids: set[int] = set()
        self.provisional_party_ids: set[int] = set()
        self.party_member_count = 0
        self.party_known = False
        self.party_roster_authoritative = False
        self.friendly_ids: set[int] = set()
        self.enemy_ids: set[int] = set()
        self.non_player_actor_ids: set[int] = set()
        self.monsters: dict[int, MonsterStats] = {}
        self.active_target_id: int | None = None
        self.events: list[dict] = []
        self.pending_member_events: list[dict] = []
        self.pending_target_events: OrderedDict[int, tuple[int, dict]] = OrderedDict()
        self.pending_target_event_ids: dict[int, deque[int]] = {}
        self.pending_target_event_sequence = 0
        self.discarded_non_encounter_events = 0
        self.stats: dict[int, ActorStats] = {}
        self.team_damage_states: dict[int, TeamDamageState] = {}
        self.team_server_time = 0
        self.team_server_update_100ns = 0
        self.encounter_start_signal_100ns = 0
        self.team_zero_baseline_signal_100ns = 0
        self.stage_summaries: dict[str, dict] = {}
        self.stage_actor_metrics: dict[int, tuple[int, int]] = {}
        self.stage_skill_snapshots: dict[int, StageSkillSnapshot] = {}
        self.seen_stage_summary_ids: set[str] = set()
        self.rejected_stage_summary_ids: set[str] = set()
        self.stage_summary_guard_until = 0.0
        self.member_death_states: dict[int, bool] = {}
        self.member_life_times: dict[int, int] = {}
        self.member_death_counts: dict[int, int] = {}
        self.entity_combat_states: dict[int, bool] = {}
        self.entity_combat_state_times: dict[int, int] = {}
        self.boss_reset_pending_100ns = 0
        self.team_reset_pending = False
        self.friend_order: list[int] = []
        self.target_activity_100ns: dict[int, int] = {}
        self.latest_network_time_100ns = 0
        self.scene_id: int | None = None
        self.combat_target_id: int | None = None
        self.encounter_target_ids: set[int] = set()
        self.encounter_add_target_ids: set[int] = set()
        self.encounter_target_order: list[int] = []
        self.linked_boss_target_ids: set[int] = set()
        self.encounter_member_ids: set[int] = set()
        self.encounter_team_size = 0
        self.encounter_authoritative_team_size = 0
        self.completed_combats: list[dict] = []
        self.run_id = str(run_id or uuid.uuid4().hex[:12])
        self.last_archive_signature: tuple | None = None
        self.first_damage_time = 0.0
        self.last_damage_time = 0.0
        self.combat_end_time = 0.0
        self.combat_end_reason = ""
        self.idle_gap = 10.0
        self.encounter_gap = 60.0
        self.session_number = 0
        self.encounter_id = f"{self.run_id}-{self.session_number:06d}"

    def set_boss_only(self, value: bool) -> bool:
        value = bool(value)
        if value == self.boss_only:
            return False
        self.reset(
            keep_identity=True,
            keep_monsters=True,
            archive_reason="target_filter_changed",
        )
        self.boss_only = value
        self._resolve_combat_sides()
        self._recompute()
        return True

    def reset(
        self,
        *,
        keep_identity: bool = True,
        keep_monsters: bool = False,
        preserve_active_target: bool = False,
        archive_reason: str = "reset",
    ) -> None:
        preserved_target_id: int | None = None
        if preserve_active_target and not self.combat_end_time:
            candidate = self.monsters.get(int(self.combat_target_id or 0))
            if (
                candidate is not None
                and self._is_priority_target(candidate.entity_id)
                and not (
                    candidate.death_confirmed
                    and candidate.current_hp is not None
                    and candidate.current_hp <= 0
                )
            ):
                preserved_target_id = candidate.entity_id
        self.archive_current(archive_reason)
        if not keep_identity:
            self.self_id = None
            self.party_ids.clear()
            self.provisional_party_ids.clear()
            self.party_member_count = 0
            self.party_known = False
            self.party_roster_authoritative = False
            self.friendly_ids.clear()
            self.friend_order.clear()
            self.team_damage_states.clear()
            self.team_server_time = 0
            self.team_server_update_100ns = 0
            self.member_death_states.clear()
            self.member_life_times.clear()
        elif self.self_id is not None:
            self.party_member_count = max(1, self.party_member_count)
            self.friendly_ids = (
                {self.self_id} | self.party_ids | self.provisional_party_ids
            )
            self.friend_order = [self.self_id] + sorted(
                (self.party_ids | self.provisional_party_ids) - {self.self_id}
            )
        self.enemy_ids.clear()
        if not keep_monsters:
            self.monsters.clear()
            self.non_player_actor_ids.clear()
            self.target_activity_100ns.clear()
            self.latest_network_time_100ns = 0
        self.active_target_id = preserved_target_id
        self.combat_target_id = preserved_target_id
        self.encounter_target_ids.clear()
        self.encounter_add_target_ids.clear()
        self.encounter_target_order.clear()
        self.linked_boss_target_ids.clear()
        self.encounter_member_ids.clear()
        self.encounter_team_size = 0
        self.encounter_authoritative_team_size = 0
        self.member_death_counts.clear()
        self.entity_combat_states.clear()
        self.entity_combat_state_times.clear()
        self.boss_reset_pending_100ns = 0
        self.team_reset_pending = False
        self.encounter_start_signal_100ns = 0
        self.team_zero_baseline_signal_100ns = 0
        self.events.clear()
        self.pending_member_events.clear()
        self.pending_target_events.clear()
        self.pending_target_event_ids.clear()
        self.stats.clear()
        self.stage_summaries.clear()
        self.stage_actor_metrics.clear()
        self.stage_skill_snapshots.clear()
        self.seen_stage_summary_ids.clear()
        self.rejected_stage_summary_ids.clear()
        self.stage_summary_guard_until = 0.0
        for state in self.team_damage_states.values():
            state.baseline_absolute = state.last_absolute
            state.baseline_snapshot_time_100ns = 0
            state.accepted_damage = 0
            state.authoritative_snapshot = False
            state.snapshot_time_100ns = 0
            state.first_time = 0.0
            state.last_time = 0.0
        self.first_damage_time = 0.0
        self.last_damage_time = 0.0
        self.combat_end_time = 0.0
        self.combat_end_reason = ""
        self.session_number += 1
        self.encounter_id = f"{self.run_id}-{self.session_number:06d}"
        self.last_archive_signature = None
        if preserved_target_id is not None:
            # A manual clear starts a new local record while the same live Boss
            # is still being fought. Keeping that target lets the very next
            # cumulative team update reappear immediately, without waiting for
            # another local/direct hit to rediscover the encounter.
            self._register_encounter_target(preserved_target_id)
            self._resolve_combat_sides()

    def _event_seconds(self, event: dict) -> float:
        return (event["filetime_100ns"] - 116_444_736_000_000_000) / 10_000_000

    def _current_member_ids(self) -> set[int]:
        members = set(self.party_ids) | self.provisional_party_ids
        if self.self_id is not None:
            members.add(self.self_id)
        return members - self.non_player_actor_ids

    def _mark_non_player_actor(self, entity_id: int) -> bool:
        """Remove a late-confirmed monster from every player-only data source."""
        if not entity_id:
            return False
        was_encounter_member = entity_id in self.encounter_member_ids
        changed = entity_id not in self.non_player_actor_ids
        self.non_player_actor_ids.add(entity_id)

        if self.self_id == entity_id:
            self.self_id = None
            changed = True
        for values in (
            self.party_ids,
            self.provisional_party_ids,
            self.friendly_ids,
            self.encounter_member_ids,
        ):
            if entity_id in values:
                values.discard(entity_id)
                changed = True
        filtered_order = [
            actor_id for actor_id in self.friend_order if actor_id != entity_id
        ]
        if filtered_order != self.friend_order:
            self.friend_order = filtered_order
            changed = True

        for mapping in (
            self.entity_professions,
            self.team_damage_states,
            self.stage_actor_metrics,
            self.member_death_states,
            self.member_life_times,
            self.member_death_counts,
        ):
            if mapping.pop(entity_id, None) is not None:
                changed = True

        filtered_events = [
            event
            for event in self.events
            if int(event.get("attacker_id", 0) or 0) != entity_id
        ]
        if len(filtered_events) != len(self.events):
            self.events = filtered_events
            changed = True
        filtered_pending = [
            event
            for event in self.pending_member_events
            if int(event.get("attacker_id", 0) or 0) != entity_id
        ]
        if len(filtered_pending) != len(self.pending_member_events):
            self.pending_member_events = filtered_pending
            changed = True

        for summary_id, summary in list(self.stage_summaries.items()):
            actors = summary.get("actors", [])
            if not isinstance(actors, list):
                continue
            filtered_actors = [
                row
                for row in actors
                if not isinstance(row, dict)
                or int(row.get("actor_id", 0) or 0) != entity_id
            ]
            if len(filtered_actors) != len(actors):
                cleaned_summary = dict(summary)
                cleaned_summary["actors"] = filtered_actors
                self.stage_summaries[summary_id] = cleaned_summary
                changed = True

        if was_encounter_member and self.encounter_team_size > 0:
            self.encounter_team_size = min(
                MAX_PARTY_MEMBERS,
                max(
                    len(self.encounter_member_ids),
                    self.encounter_team_size - 1,
                ),
            )
        return changed

    def _party_roster_is_resolved(self) -> bool:
        if not self.party_known:
            return False
        resolved_members = {
            actor_id for actor_id in self._current_member_ids() if actor_id > 0
        }
        expected = max(1, self.party_member_count)
        return len(resolved_members) >= expected

    def _admit_provisional_party_actor(self, event: dict) -> bool:
        if (
            not self.party_known
            or self.party_member_count <= 1
            or event.get("player_attacker") is not True
            or event.get("party_attacker") is False
        ):
            return False
        try:
            attacker = int(event.get("attacker_id", 0) or 0)
            target = int(event.get("target_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if attacker <= 0 or attacker in self._current_member_ids():
            return attacker in self.provisional_party_ids
        if not (
            self._is_priority_target(target)
            or self._is_encounter_damage_target(target)
        ):
            return False
        if attacker in self.encounter_member_ids:
            return True

        # Role/token packets and DamageSync can use different actor IDs for the
        # same person.  Count verified damage participants, not every positive
        # roster placeholder, otherwise a seemingly complete 12-player roster
        # rejects all live teammate damage except the local player.
        verified_members = set(self.encounter_member_ids)
        if self.self_id is not None:
            verified_members.add(self.self_id)
        if len(verified_members) >= self.party_member_count:
            return False

        resolved_members = {
            actor_id for actor_id in self._current_member_ids() if actor_id > 0
        }
        if len(resolved_members) < self.party_member_count:
            self.provisional_party_ids.add(attacker)
        self.encounter_member_ids.add(attacker)
        self.friendly_ids.add(attacker)
        if attacker not in self.friend_order:
            self.friend_order.append(attacker)
        self._replay_pending_member_events()
        return True

    def _register_encounter_target(self, entity_id: int, *, add: bool = False) -> None:
        if not entity_id:
            return
        self.encounter_target_ids.add(entity_id)
        if entity_id not in self.encounter_target_order:
            self.encounter_target_order.append(entity_id)
        if add:
            self.encounter_add_target_ids.add(entity_id)

    def _multiphase_encounter(self) -> bool:
        monster = self.monsters.get(int(self.combat_target_id or 0))
        return bool(
            monster is not None
            and int(monster.template_id or 0) in MULTIPHASE_BOSS_TEMPLATE_IDS
        )

    @staticmethod
    def _bosses_share_phase(
        previous: MonsterStats | None, incoming: MonsterStats | None
    ) -> bool:
        if previous is None or incoming is None:
            return False
        templates = (
            int(previous.template_id or 0),
            int(incoming.template_id or 0),
        )
        return bool(
            boss_names_share_phase(previous.name, incoming.name)
            or templates in BOSS_PHASE_TEMPLATE_TRANSITIONS
            or tuple(reversed(templates)) in BOSS_PHASE_TEMPLATE_TRANSITIONS
        )

    @staticmethod
    def _boss_phase_continues(
        previous: MonsterStats | None, incoming: MonsterStats | None
    ) -> bool:
        if previous is None or incoming is None:
            return False
        return bool(
            boss_phase_continues(previous.name, incoming.name)
            or (
                int(previous.template_id or 0),
                int(incoming.template_id or 0),
            )
            in BOSS_PHASE_TEMPLATE_TRANSITIONS
        )

    def _damage_gap_starts_new_encounter(self) -> bool:
        if not self._multiphase_encounter():
            return True
        monster = self.monsters.get(int(self.combat_target_id or 0))
        if monster is None or monster.current_hp is None:
            return False
        hp_ceiling = self._monster_max_hp(monster)
        return bool(
            self.boss_reset_pending_100ns
            and hp_ceiling > 0
            and monster.current_hp >= hp_ceiling * 0.995
        )

    def _encounter_idle_timeout(self) -> float:
        if self._multiphase_encounter():
            return self.encounter_gap
        return self.idle_gap

    def _target_hp_depleted(self) -> bool:
        monster = self.monsters.get(int(self.combat_target_id or 0))
        depleted = bool(
            monster is not None
            and self._is_priority_target(monster.entity_id)
            and monster.current_hp is not None
            and monster.current_hp <= 0
        )
        if not depleted:
            return False
        # 星象仪者的守卫阶段发生时主 Boss 仍有血。只有主 Boss 自身的
        # 明确死亡包到达后才结束，避免结算清场时残留守卫的血量包让
        # 已经归零的 Boss 重新显示为战斗中。
        return not self._multiphase_encounter() or bool(monster.death_confirmed)

    def _result_frozen_by_target_death(self) -> bool:
        return self._target_hp_depleted()

    def _buffer_pending_member_event(self, event: dict) -> None:
        event_time = self._event_seconds(event)
        cutoff = event_time - UNVERIFIED_MEMBER_EVENT_WINDOW_SECONDS
        self.pending_member_events = [
            pending
            for pending in self.pending_member_events
            if self._event_seconds(pending) >= cutoff
        ]
        self.pending_member_events.append(dict(event))
        if len(self.pending_member_events) > 8192:
            self.pending_member_events = self.pending_member_events[-4096:]

    def _target_event_disposition(self, target_id: int) -> str:
        """Return accept, pending, or reject for an incoming damage target."""
        if not self.boss_only:
            return "accept"
        if self._is_priority_target(target_id):
            return "accept"
        if self.combat_target_id is not None:
            # Preserve the existing in-encounter auxiliary inference. The
            # capture parser normally resolves these targets first, while the
            # model remains a fallback for late or missing metadata.
            return "accept"

        monster = self.monsters.get(target_id)
        if monster is None:
            return "pending"
        if monster.encounter_auxiliary:
            parent_ids = set(monster.encounter_parent_template_ids)
            if any(
                int(candidate.template_id or 0) in parent_ids
                and self._monster_rank(candidate) > 0
                for candidate in self.monsters.values()
            ):
                return "accept"
            return "pending"
        if monster.entity_type.casefold() in {"player", "role"}:
            return "reject"
        identity_known = bool(
            monster.entity_type
            or monster.template_id not in (None, 0)
            or monster.boss_type is not None
        )
        return "reject" if identity_known else "pending"

    def _remove_pending_target_event(self, token: int) -> tuple[int, dict] | None:
        item = self.pending_target_events.pop(token, None)
        if item is None:
            return None
        target_id, _event = item
        target_tokens = self.pending_target_event_ids.get(target_id)
        if target_tokens is not None:
            while target_tokens and target_tokens[0] not in self.pending_target_events:
                target_tokens.popleft()
            if not target_tokens:
                self.pending_target_event_ids.pop(target_id, None)
        return item

    def _prune_pending_target_events(self, timestamp: int) -> None:
        cutoff = timestamp - int(UNKNOWN_TARGET_EVENT_WINDOW_SECONDS * 10_000_000)
        while self.pending_target_events:
            token, (_target_id, event) = next(iter(self.pending_target_events.items()))
            event_timestamp = int(event.get("filetime_100ns", 0) or 0)
            if event_timestamp >= cutoff:
                break
            self._remove_pending_target_event(token)
            self.discarded_non_encounter_events += 1

    def _buffer_pending_target_event(self, event: dict) -> None:
        target_id = int(event.get("target_id", 0) or 0)
        timestamp = int(event.get("filetime_100ns", 0) or 0)
        if not target_id or not timestamp:
            self.discarded_non_encounter_events += 1
            return
        self._prune_pending_target_events(
            max(timestamp, self.latest_network_time_100ns)
        )
        self.pending_target_event_sequence += 1
        token = self.pending_target_event_sequence
        self.pending_target_events[token] = (target_id, dict(event))
        target_tokens = self.pending_target_event_ids.setdefault(target_id, deque())
        while target_tokens and target_tokens[0] not in self.pending_target_events:
            target_tokens.popleft()
        target_tokens.append(token)

        while len(target_tokens) > UNKNOWN_TARGET_EVENT_LIMIT_PER_TARGET:
            oldest = target_tokens.popleft()
            if self.pending_target_events.pop(oldest, None) is not None:
                self.discarded_non_encounter_events += 1
        while len(self.pending_target_events) > UNKNOWN_TARGET_EVENT_LIMIT_GLOBAL:
            _oldest, (oldest_target, _event) = self.pending_target_events.popitem(
                last=False
            )
            self.discarded_non_encounter_events += 1
            tokens = self.pending_target_event_ids.get(oldest_target)
            if tokens is not None:
                while tokens and tokens[0] not in self.pending_target_events:
                    tokens.popleft()
                if not tokens:
                    self.pending_target_event_ids.pop(oldest_target, None)

    def _resolve_pending_target_events(self, target_id: int) -> bool:
        if self.latest_network_time_100ns:
            self._prune_pending_target_events(self.latest_network_time_100ns)
        target_tokens = self.pending_target_event_ids.get(int(target_id), deque())
        if not target_tokens:
            return False
        disposition = self._target_event_disposition(int(target_id))
        if disposition == "pending":
            return False
        events: list[dict] = []
        for token in list(target_tokens):
            item = self.pending_target_events.pop(token, None)
            if item is not None:
                events.append(item[1])
        self.pending_target_event_ids.pop(int(target_id), None)
        if disposition == "reject":
            self.discarded_non_encounter_events += len(events)
            return False
        events.sort(key=lambda event: int(event.get("filetime_100ns", 0) or 0))
        for event in events:
            self.ingest(event)
        return bool(events)

    def _resolve_known_pending_target_events(self) -> bool:
        replayed = False
        for target_id in list(self.pending_target_event_ids):
            monster = self.monsters.get(target_id)
            if monster is None or not monster.encounter_auxiliary:
                continue
            replayed |= self._resolve_pending_target_events(target_id)
        return replayed

    def _replay_pending_member_events(self) -> bool:
        if not self.pending_member_events:
            return False
        members = self._current_member_ids() | self.encounter_member_ids
        newest_time = max(
            self._event_seconds(event) for event in self.pending_member_events
        )
        cutoff = newest_time - UNVERIFIED_MEMBER_EVENT_WINDOW_SECONDS
        roster_resolved = self._party_roster_is_resolved()
        accepted: list[dict] = []
        remaining: list[dict] = []
        for event in self.pending_member_events:
            attacker = int(event.get("attacker_id", 0) or 0)
            if attacker in members:
                accepted.append(event)
            elif not roster_resolved and self._event_seconds(event) >= cutoff:
                remaining.append(event)
        self.pending_member_events = remaining
        for event in accepted:
            self.ingest(event)
        return bool(accepted)

    def current_stats(self) -> list[ActorStats]:
        return [
            stats
            for stats in self.stats.values()
            if stats.damage > 0
            and self._actor_counts_for_encounter(stats.actor_id)
        ]

    def _encounter_started(self) -> bool:
        return bool(
            self.first_damage_time
            or self.last_damage_time
            or any(
                state.accepted_damage > 0
                for state in self.team_damage_states.values()
            )
        )

    def _update_encounter_roster(self, *extra_actor_ids: int) -> None:
        # A participant exists only after an observed hit on the current Boss.
        # Party packets remain useful for names, but never create zero-DPS rows.
        members = {
            actor_id
            for actor_id in extra_actor_ids
            if actor_id and actor_id not in self.non_player_actor_ids
        }
        if self._is_dummy_encounter():
            members = {
                actor_id
                for actor_id in members
                if self.self_id is not None and actor_id == self.self_id
            }
        if not members and not self._encounter_started():
            return
        self.encounter_member_ids.update(members)
        self.encounter_team_size = min(
            MAX_PARTY_MEMBERS,
            max(
                self.encounter_team_size,
                len(self.encounter_member_ids),
            ),
        )
        for actor_id in members:
            if actor_id not in self.friend_order:
                self.friend_order.append(actor_id)

    def _all_encounter_combatants_out(self) -> bool:
        participants = {
            actor_id
            for actor_id in self.encounter_member_ids
            if actor_id not in self.non_player_actor_ids
        }
        if not participants:
            return False
        known_states = {
            actor_id: self.entity_combat_states[actor_id]
            for actor_id in participants
            if actor_id in self.entity_combat_states
        }
        if len(known_states) == len(participants):
            return not any(known_states.values())
        boss_out = self.entity_combat_states.get(
            int(self.combat_target_id or 0)
        ) is False
        return bool(boss_out and known_states and not any(known_states.values()))

    def _mark_pending_reset_complete(self) -> bool:
        if (
            not self.boss_reset_pending_100ns
            or not self.first_damage_time
            or self.combat_end_time
            or not self._all_encounter_combatants_out()
        ):
            return False
        self.combat_end_time = self.last_damage_time
        self.combat_end_reason = "target_reset"
        return True

    def _mark_target_defeated(self, monster: MonsterStats) -> bool:
        # One defeated unit does not end an all-monsters encounter while
        # the party may still be fighting the rest of the same pack.
        if not self.boss_only:
            return False
        if (
            not self.first_damage_time
            or monster.entity_id != self.combat_target_id
            or not self._is_priority_target(monster.entity_id)
            or monster.current_hp is None
            or monster.current_hp > 0
            or not monster.death_time_100ns
            or not monster.death_confirmed
            or self.combat_end_time
        ):
            return False
        if self._multiphase_encounter():
            return False
        if not self._all_encounter_combatants_out():
            return False
        end_time = self._event_seconds(
            {"filetime_100ns": monster.death_time_100ns}
        )
        if end_time < self.first_damage_time:
            return False
        self.combat_end_time = end_time
        self.combat_end_reason = "target_defeated"
        return True

    def _mark_party_wipe_if_complete(self, event_time: float) -> bool:
        if (
            not self.first_damage_time
            or self.combat_end_time
            or event_time < self.first_damage_time
        ):
            return False
        members = self._current_member_ids()
        if not members:
            return False
        if self.party_known and self.party_member_count > len(members):
            # Some roster entries have not been resolved yet. Never declare a
            # wipe from only the visible subset of the party.
            return False
        if not all(
            actor_id in self.member_death_states
            and self.member_death_states[actor_id]
            for actor_id in members
        ):
            return False
        self.combat_end_time = max(self.last_damage_time, event_time)
        self.combat_end_reason = "party_wipe"
        return True

    @staticmethod
    def _monster_max_hp(monster: MonsterStats) -> float:
        return max(monster.max_hp or 0.0, monster.observed_max_hp or 0.0)

    def _is_dummy_target(self, entity_id: int) -> bool:
        monster = self.monsters.get(int(entity_id or 0))
        name = (
            (monster.name if monster is not None else "")
            or self.entity_names.get(int(entity_id or 0), "")
        ).strip()
        return DUMMY_NAME_MARKER in name

    def _is_dummy_encounter(self) -> bool:
        return bool(
            self.combat_target_id is not None
            and self._is_dummy_target(self.combat_target_id)
        )

    def _actor_counts_for_encounter(self, actor_id: int) -> bool:
        if not actor_id or actor_id in self.non_player_actor_ids:
            return False
        if not self._is_dummy_encounter():
            return True
        return self.self_id is not None and actor_id == self.self_id

    def _monster_rank(self, monster: MonsterStats) -> int:
        if self._is_dummy_target(monster.entity_id):
            return 3
        if monster.boss_rank >= 3 or monster.boss_type == 3:
            return 3
        entity_type = monster.entity_type.casefold()
        if "boss" in entity_type or "首领" in entity_type:
            return 3
        return 0

    def _monster_activity(self, monster: MonsterStats) -> int:
        return max(
            monster.last_update_100ns,
            self.target_activity_100ns.get(monster.entity_id, 0),
        )

    def _monster_priority(self, monster: MonsterStats) -> tuple:
        current_hp = monster.current_hp
        alive = 1 if current_hp is None or current_hp > 0 else 0
        return (
            self._monster_rank(monster),
            int(monster.level or 0),
            alive,
            self._monster_max_hp(monster),
            self._monster_activity(monster),
        )

    def _select_priority_monster(
        self, *, active_at_100ns: int | None = None
    ) -> MonsterStats | None:
        candidates: list[MonsterStats] = []
        reference_time = active_at_100ns or self.latest_network_time_100ns
        active_seconds = (
            TEAM_TARGET_ACTIVE_SECONDS
            if active_at_100ns is not None
            else MONSTER_DISPLAY_ACTIVE_SECONDS
        )
        active_window = int(active_seconds * 10_000_000)
        for entity_id, monster in self.monsters.items():
            if entity_id in self.friendly_ids:
                continue
            if monster.entity_type.casefold() in {"player", "role"}:
                continue
            if self.boss_only and self._monster_rank(monster) <= 0:
                continue
            if reference_time:
                activity = self._monster_activity(monster)
                elapsed = reference_time - activity
                if not activity or elapsed < 0 or elapsed > active_window:
                    continue
            candidates.append(monster)
        return max(candidates, key=self._monster_priority) if candidates else None

    def _is_priority_target(self, entity_id: int) -> bool:
        monster = self.monsters.get(entity_id)
        if monster is None or entity_id in self.friendly_ids:
            return False
        if monster.entity_type.casefold() in {"player", "role"}:
            return False
        return not self.boss_only or self._monster_rank(monster) > 0

    def _encounter_damage_target_ids(self) -> set[int]:
        if self.boss_only:
            if self.combat_target_id is None:
                return set()
            target_ids = {
                self.combat_target_id,
                *self.linked_boss_target_ids,
                *self.encounter_add_target_ids,
            }
            return target_ids
        target_ids = set(self.encounter_target_ids)
        if self.combat_target_id is not None:
            target_ids.add(self.combat_target_id)
        return {
            entity_id
            for entity_id in target_ids
            if self._is_priority_target(entity_id)
        }

    def _is_encounter_damage_target(self, entity_id: int) -> bool:
        if not entity_id:
            return False
        if self.boss_only:
            return bool(
                self.combat_target_id is not None
                and (
                    entity_id == self.combat_target_id
                    or entity_id in self.linked_boss_target_ids
                    or entity_id in self.encounter_add_target_ids
                )
            )
        return bool(
            (
                entity_id == self.combat_target_id
                or entity_id in self.encounter_target_ids
            )
            and self._is_priority_target(entity_id)
        )

    def _link_boss_scene_target(
        self, target: int, event_time: float, timestamp: int
    ) -> bool:
        if (
            not self.boss_only
            or not self.first_damage_time
            or self.combat_target_id is None
            or target == self.combat_target_id
            or target in self._current_member_ids()
            or target in self.friendly_ids
            or self._is_priority_target(target)
        ):
            return False
        primary = self.monsters.get(self.combat_target_id)
        if primary is None or self._monster_rank(primary) <= 0:
            return False
        monster = self.monsters.get(target)
        if monster is not None and monster.entity_type.casefold() in {
            "player",
            "role",
        }:
            return False
        if (
            self.last_damage_time
            and event_time - self.last_damage_time > self.encounter_gap
        ):
            return False
        if monster is None:
            monster = MonsterStats(target, entity_type="Monster")
            self.monsters[target] = monster
        self._infer_missing_encounter_auxiliary(primary, monster)
        self._register_encounter_target(target, add=True)
        if timestamp:
            self.target_activity_100ns[target] = max(
                self.target_activity_100ns.get(target, 0), timestamp
            )
        if (
            self.combat_end_time
            and self.combat_end_reason == "target_defeated"
            and 0 <= event_time - self.combat_end_time <= self.encounter_gap
        ):
            self.combat_end_time = 0.0
            self.combat_end_reason = ""
        return True

    def _infer_missing_encounter_auxiliary(
        self, primary: MonsterStats, monster: MonsterStats
    ) -> bool:
        inferred = unique_inferred_auxiliary_for_parent(
            int(primary.template_id or 0)
        )
        if inferred is None:
            return False
        template_id, metadata = inferred
        if monster.template_id not in (None, 0, template_id):
            return False
        canonical_name = str(metadata.get("name", "")).strip()
        existing_name = (
            monster.name or self.entity_names.get(monster.entity_id, "")
        ).strip()
        if existing_name and existing_name.casefold() not in {
            canonical_name.casefold(),
            "monster",
            "小怪",
        }:
            return False
        parent_template_ids = tuple(
            sorted(
                {
                    int(value)
                    for value in metadata.get("parent_template_ids", ())
                    if int(value)
                }
            )
        )
        monster.name = canonical_name
        monster.entity_type = "Monster"
        monster.template_id = template_id
        monster.encounter_auxiliary = True
        monster.encounter_parent_template_ids = parent_template_ids
        self.entity_names[monster.entity_id] = canonical_name
        self._mark_non_player_actor(monster.entity_id)
        return True

    @staticmethod
    def _iso_timestamp(value: float) -> str:
        return dt.datetime.fromtimestamp(value).astimezone().isoformat(
            timespec="seconds"
        )

    def _archive_signature(self) -> tuple | None:
        total = sum(actor.damage for actor in self.stats.values())
        if total <= 0 or not self.first_damage_time or not self.last_damage_time:
            return None
        actors = tuple(
            sorted(
                (
                    actor_id,
                    actor.damage,
                    actor.hits,
                    actor.max_hit,
                    actor.damage_hits,
                    actor.critical_hits,
                    self.member_death_counts.get(actor_id, 0),
                    self.display_name(actor_id),
                    tuple(
                        sorted(
                            (
                                skill.skill_id,
                                skill.damage,
                                skill.hits,
                                skill.max_hit,
                                self.display_skill_name(actor_id, skill.skill_id),
                            )
                            for skill in actor.skills.values()
                        )
                    ),
                    tuple(sorted(actor.target_damage.items())),
                )
                for actor_id, actor in self.stats.items()
            )
        )
        targets = tuple(
            sorted(
                (
                    entity_id,
                    monster.name,
                    monster.level,
                    monster.boss_rank,
                    monster.current_hp,
                    monster.max_hp,
                    monster.observed_max_hp,
                )
                for entity_id in self.encounter_target_ids
                if (monster := self.monsters.get(entity_id)) is not None
            )
        )
        return (
            self.encounter_id,
            round(self.first_damage_time, 3),
            round(self.last_damage_time, 3),
            round(self.combat_end_time, 3),
            self.encounter_team_size,
            tuple(sorted(self.encounter_member_ids)),
            actors,
            targets,
        )

    def build_combat_record(self, reason: str = "completed") -> dict | None:
        signature = self._archive_signature()
        if signature is None:
            return None
        ended_at = self.combat_end_time or self.last_damage_time
        duration = max(1.0, ended_at - self.first_damage_time)
        rows = sorted(
            (actor for actor in self.stats.values() if actor.damage > 0),
            key=lambda actor: actor.damage,
            reverse=True,
        )
        total_damage = sum(actor.damage for actor in rows)
        ordered_ids = list(self.friend_order)
        ordered_ids.extend(
            actor.actor_id for actor in rows if actor.actor_id not in ordered_ids
        )
        participants: list[dict] = []
        for actor in rows:
            actor_id = actor.actor_id
            stage_skill_snapshot = self.active_stage_skill_snapshot(
                actor_id, actor
            )
            try:
                fallback_index = ordered_ids.index(actor_id) + 1
            except ValueError:
                fallback_index = len(participants) + 1
            name = self.display_name(actor_id) or f"玩家{fallback_index}"
            skills = []
            for skill in sorted(
                actor.skills.values(), key=lambda item: item.damage, reverse=True
            ):
                skills.append(
                    {
                        "skill_id": skill.skill_id,
                        "name": self.display_skill_name(actor_id, skill.skill_id),
                        "damage": skill.damage,
                        "share": skill.damage / actor.damage if actor.damage else 0.0,
                        "hits": (
                            None
                            if stage_skill_snapshot is not None
                            and skill.skill_id == 0
                            else skill.hits
                        ),
                        "max_hit": (
                            None
                            if stage_skill_snapshot is not None
                            else skill.max_hit
                        ),
                        "source": (
                            "server_stage_summary"
                            if stage_skill_snapshot is not None
                            else "exact_callback"
                        ),
                    }
                )
            if not skills and actor.damage > 0:
                skills.append(
                    {
                        "skill_id": 0,
                        "name": "未归类伤害",
                        "damage": actor.damage,
                        "share": 1.0,
                        "hits": None,
                        "max_hit": None,
                        "aggregate": True,
                    }
                )
            classified_skill_damage = sum(
                skill.damage
                for skill in actor.skills.values()
                if skill.skill_id != 0
            )
            unclassified_damage = sum(
                skill.damage
                for skill in actor.skills.values()
                if skill.skill_id == 0
            )
            accounted_skill_damage = (
                classified_skill_damage + unclassified_damage
            )
            participants.append(
                {
                    "actor_id": actor_id,
                    "name": name,
                    "is_self": actor_id == self.self_id,
                    "profession_id": self.actor_profession_id(actor_id),
                    "damage": actor.damage,
                    "dps": actor.damage / duration,
                    "share": actor.damage / total_damage if total_damage else 0.0,
                    "hits": actor.hits,
                    "max_hit": actor.max_hit,
                    "damage_hits": actor.damage_hits,
                    "critical_hits": actor.critical_hits,
                    "critical_rate": (
                        actor.critical_hits / actor.damage_hits
                        if actor.damage_hits
                        and actor.critical_hits is not None
                        else None
                    ),
                    "deaths": self.member_death_counts.get(actor_id, 0),
                    "classified_skill_damage": classified_skill_damage,
                    "unclassified_damage": unclassified_damage,
                    "accounted_skill_damage": accounted_skill_damage,
                    "skill_damage_difference": (
                        actor.damage - accounted_skill_damage
                    ),
                    "skill_source": (
                        "server_stage_summary"
                        if stage_skill_snapshot is not None
                        else "exact_callbacks_with_explicit_unclassified"
                    ),
                    "skill_summary_id": (
                        stage_skill_snapshot.summary_id
                        if stage_skill_snapshot is not None
                        else ""
                    ),
                    "skills": skills,
                    "targets": self.actor_target_rows(actor_id),
                }
            )

        target_ids = self._encounter_damage_target_ids()
        target_models = [
            self.monsters[entity_id]
            for entity_id in target_ids
            if entity_id in self.monsters
        ]
        target_models.sort(
            key=lambda monster: (
                monster.entity_id == self.combat_target_id,
                self._monster_priority(monster),
            ),
            reverse=True,
        )
        targets: list[dict] = []
        for monster in target_models:
            hp_ceiling = self._monster_max_hp(monster)
            targets.append(
                {
                    "entity_id": monster.entity_id,
                    "name": self.display_target_name(monster.entity_id),
                    "level": monster.level,
                    "entity_type": monster.entity_type,
                    "template_id": monster.template_id,
                    "boss_type": monster.boss_type,
                    "boss_rank": self._monster_rank(monster),
                    "current_hp": monster.current_hp,
                    "max_hp": hp_ceiling or None,
                }
            )
        primary_target = targets[0] if targets else None
        event_damage_by_actor: dict[int, int] = {}
        event_damage_by_target: dict[int, int] = {}
        event_count = 0
        for event in self.events:
            try:
                attacker_id = int(event.get("attacker_id", 0) or 0)
                target_id = int(event.get("target_id", 0) or 0)
                event_damage = max(0, int(event.get("damage", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                event_damage <= 0
                or target_id not in target_ids
                or not self._actor_counts_for_encounter(attacker_id)
            ):
                continue
            event_count += 1
            event_damage_by_actor[attacker_id] = (
                event_damage_by_actor.get(attacker_id, 0) + event_damage
            )
            event_damage_by_target[target_id] = (
                event_damage_by_target.get(target_id, 0) + event_damage
            )
        damage_accounting = {
            "final_total": total_damage,
            "skill_amount_policy": "absolute_only",
            "skill_amounts_rescaled": False,
            "stage_skill_policy": (
                "server_skill_amounts_are_used_only_when_the_stage_actor_total_"
                "exactly_matches_the_common_total"
            ),
            "stage_skill_snapshots": [
                {
                    "summary_id": snapshot.summary_id,
                    "actor_id": actor_id,
                    "actor_damage": snapshot.actor_damage,
                    "filetime_100ns": snapshot.filetime_100ns,
                    "classified_damage": sum(
                        damage for damage, _hits in snapshot.skills.values()
                    ),
                    "unclassified_damage": snapshot.unclassified_damage,
                    "skill_count": len(snapshot.skills),
                }
                for actor_id, snapshot in sorted(
                    self.stage_skill_snapshots.items()
                )
                if self.active_stage_skill_snapshot(actor_id) is not None
            ],
            "skill_reconciliation": [
                {
                    "actor_id": int(participant.get("actor_id", 0) or 0),
                    "name": str(participant.get("name", "")),
                    "damage": int(participant.get("damage", 0) or 0),
                    "classified_skill_damage": int(
                        participant.get("classified_skill_damage", 0) or 0
                    ),
                    "unclassified_damage": int(
                        participant.get("unclassified_damage", 0) or 0
                    ),
                    "accounted_skill_damage": int(
                        participant.get("accounted_skill_damage", 0) or 0
                    ),
                    "difference": int(
                        participant.get("skill_damage_difference", 0) or 0
                    ),
                }
                for participant in participants
            ],
            "packet_event_count": event_count,
            "packet_event_total": sum(event_damage_by_actor.values()),
            "packet_event_by_actor": [
                {
                    "actor_id": actor_id,
                    "name": self.display_name(actor_id),
                    "damage": damage,
                }
                for actor_id, damage in sorted(
                    event_damage_by_actor.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ],
            "packet_event_by_target": [
                {
                    "entity_id": target_id,
                    "name": self.display_target_name(target_id),
                    "damage": damage,
                }
                for target_id, damage in sorted(
                    event_damage_by_target.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ],
            "team_cumulative_states": [
                {
                    "actor_id": actor_id,
                    "name": self.display_name(actor_id),
                    "last_absolute": state.last_absolute,
                    "baseline_absolute": state.baseline_absolute,
                    "accepted_damage": state.accepted_damage,
                    "has_snapshot": state.has_snapshot,
                    "authoritative_snapshot": state.authoritative_snapshot,
                    "snapshot_time_100ns": state.snapshot_time_100ns,
                    "server_time": state.server_time,
                    "first_time": state.first_time,
                    "last_time": state.last_time,
                }
                for actor_id, state in self.team_damage_states.items()
                if state.has_snapshot or state.accepted_damage > 0
            ],
            "stage_summary_validations": [
                {
                    "summary_id": summary_id,
                    "filetime_100ns": int(
                        summary.get("filetime_100ns", 0) or 0
                    ),
                    "authoritative": bool(summary.get("authoritative")),
                    "completion_confirmed": bool(
                        summary.get("completion_confirmed")
                    ),
                    "validation_only": bool(summary.get("validation_only")),
                    "validation": dict(summary.get("validation", {})),
                    "member_count": int(summary.get("member_count", 0) or 0),
                    "actors": [
                        dict(actor)
                        for actor in summary.get("actors", [])
                        if isinstance(actor, dict)
                    ],
                }
                for summary_id, summary in self.stage_summaries.items()
            ],
            "seen_stage_summary_ids": sorted(self.seen_stage_summary_ids),
            "rejected_stage_summary_ids": sorted(
                self.rejected_stage_summary_ids
            ),
            "encounter_member_ids": sorted(self.encounter_member_ids),
            "encounter_target_ids": sorted(target_ids),
        }
        now = time.time()
        return {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "encounter_id": self.encounter_id,
            "source": "network_rpc",
            "archive_reason": str(reason),
            "started_at_epoch": self.first_damage_time,
            "ended_at_epoch": ended_at,
            "started_at": self._iso_timestamp(self.first_damage_time),
            "ended_at": self._iso_timestamp(ended_at),
            "saved_at_epoch": now,
            "saved_at": self._iso_timestamp(now),
            "duration_seconds": duration,
            "total_damage": total_damage,
            "team_dps": total_damage / duration,
            "team_size": (
                len(participants)
                if self._is_dummy_encounter()
                else min(
                    MAX_PARTY_MEMBERS,
                    max(len(participants), self.encounter_team_size),
                )
            ),
            "target_filter": "boss" if self.boss_only else "all_monsters",
            "monster": primary_target,
            "targets": targets,
            "participants": participants,
            "damage_accounting": damage_accounting,
        }

    def _queue_current_record_refresh(self) -> bool:
        target_depleted = self._target_hp_depleted()
        if not self.combat_end_time and (
            not target_depleted or self._multiphase_encounter()
        ):
            return False
        reason = self.combat_end_reason or (
            "target_defeated" if target_depleted else "completed"
        )
        record = self.build_combat_record(reason)
        signature = self._archive_signature()
        if record is None or signature is None:
            return False
        for index in range(len(self.completed_combats) - 1, -1, -1):
            if self.completed_combats[index].get("encounter_id") == self.encounter_id:
                self.completed_combats[index] = record
                self.last_archive_signature = signature
                return True
        # The first copy may already be on disk. Saving the same encounter ID
        # again attaches late validation or crit metadata without changing DPS.
        self.completed_combats.append(record)
        self.last_archive_signature = signature
        return True

    def archive_current(self, reason: str = "completed") -> bool:
        signature = self._archive_signature()
        if signature is None or signature == self.last_archive_signature:
            return False
        record = self.build_combat_record(reason)
        if record is None:
            return False
        for index in range(len(self.completed_combats) - 1, -1, -1):
            if self.completed_combats[index].get("encounter_id") == self.encounter_id:
                self.completed_combats[index] = record
                self.last_archive_signature = signature
                return True
        self.completed_combats.append(record)
        self.last_archive_signature = signature
        return True

    def finalize_if_idle(self, now: float | None = None) -> bool:
        if not self.last_damage_time:
            return False
        if self.combat_end_time:
            return self.archive_current(
                self.combat_end_reason or "completed"
            )
        now = time.time() if now is None else now
        if (
            self.boss_reset_pending_100ns
            and now - self.last_damage_time >= self.idle_gap
            and not self._multiphase_encounter()
        ):
            # Some clients do not expose every entity's fight-mode transition.
            # A confirmed full refill followed by a quiet interval is the
            # fallback for the same all-out-of-combat reset condition.
            self.combat_end_time = self.last_damage_time
            self.combat_end_reason = "target_reset"
            return self.archive_current("target_reset")
        if now - self.last_damage_time < self._encounter_idle_timeout():
            return False
        monster = self.current_monster()
        if (
            self._multiphase_encounter()
            and not (
                monster is not None
                and monster.current_hp is not None
                and monster.current_hp <= 0
                and monster.death_time_100ns
            )
        ):
            # 星象仪者的守卫阶段可能超过普通脱战间隔，Boss 还存活时
            # 不能仅凭静默或一次回满候选把同一场战斗拆开。
            return False
        reason = (
            "target_defeated"
            if monster is not None
            and monster.current_hp is not None
            and monster.current_hp <= 0
            and monster.death_time_100ns
            else "idle"
        )
        return self.archive_current(reason)

    def pop_completed_combats(self) -> list[dict]:
        records = self.completed_combats
        self.completed_combats = []
        return records

    def _apply_event_incrementally(self, event: dict) -> bool:
        attacker = int(event.get("attacker_id", 0) or 0)
        target = int(event.get("target_id", 0) or 0)
        damage = int(event.get("damage", 0) or 0)
        timestamp = int(event.get("filetime_100ns", 0) or 0)
        if (
            damage <= 0
            or not self._is_encounter_damage_target(target)
            or not self._actor_counts_for_encounter(attacker)
        ):
            return True

        actor = self.stats.get(attacker)
        team_state = self.team_damage_states.get(attacker)
        if (
            attacker in self.stage_actor_metrics
            or self.active_stage_skill_snapshot(attacker, actor) is not None
            or (
                team_state is not None
                and team_state.authoritative_snapshot
                and team_state.snapshot_time_100ns > 0
                and timestamp <= team_state.snapshot_time_100ns
            )
        ):
            return False

        event_time = self._event_seconds(event)
        actor = self.stats.setdefault(attacker, ActorStats(attacker))
        skill_id = int(event.get("skill_id", 0)) or self.normalize_damage_skill_id(
            event.get("arg4_u64", 0)
        )
        raw_critical = event.get("critical")
        actor.add(
            damage,
            event_time,
            skill_id,
            target,
            bool(raw_critical) if isinstance(raw_critical, bool) else None,
        )
        if not self.first_damage_time or event_time < self.first_damage_time:
            self.first_damage_time = event_time
        self.last_damage_time = max(self.last_damage_time, event_time)
        return True

    def ingest(self, event: dict) -> None:
        if event.get("damage_source") == "hp_correlated":
            return
        try:
            damage = int(event.get("damage", 0))
            attacker = int(event["attacker_id"])
            target = int(event["target_id"])
            timestamp = int(event.get("filetime_100ns", 0) or 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        if (
            damage <= 0
            or event.get("player_attacker") is False
            or attacker in self.non_player_actor_ids
        ):
            return
        self.latest_network_time_100ns = max(self.latest_network_time_100ns, timestamp)
        target_disposition = self._target_event_disposition(target)
        if target_disposition == "pending":
            self._buffer_pending_target_event(event)
            return
        if target_disposition == "reject":
            self.discarded_non_encounter_events += 1
            return
        # Participation is established by a confirmed player damaging one of
        # this encounter's targets. Party snapshots are still used for names
        # and life state, but a stale join/leave list must never hide a real
        # contributor or keep a departed zero-damage row on screen.
        if (
            attacker not in self._current_member_ids()
            and event.get("player_attacker") is not True
        ):
            if self.party_known and not self._party_roster_is_resolved():
                self._buffer_pending_member_event(event)
            return
        previous_session_number = self.session_number
        encounter_was_empty = not self.first_damage_time
        event_time = self._event_seconds(event)
        if (
            self._is_dummy_target(target)
            and self.self_id is not None
            and attacker != self.self_id
        ):
            return
        if (
            not self.boss_only
            and target not in self.monsters
            and target not in self.friendly_ids
            and attacker in self.friendly_ids
        ):
            # Ordinary monster profile/HP packets may arrive after the first
            # hit.  In all-monsters mode, a confirmed friendly hit is enough
            # to create a provisional target; later profile packets refine it.
            self.monsters[target] = MonsterStats(
                target, entity_type="Monster"
            )
        priority_target = self._is_priority_target(target)
        if not priority_target and self.combat_target_id is None:
            auxiliary = self.monsters.get(target)
            if auxiliary is not None and auxiliary.encounter_auxiliary:
                parents = set(auxiliary.encounter_parent_template_ids)
                candidates = [
                    monster
                    for monster in self.monsters.values()
                    if int(monster.template_id or 0) in parents
                    and self._monster_rank(monster) > 0
                ]
                if candidates:
                    self.combat_target_id = max(
                        candidates, key=self._monster_priority
                    ).entity_id
                    self._register_encounter_target(self.combat_target_id)
        if (
            not priority_target
            and self.combat_target_id is not None
            and not self._is_encounter_damage_target(target)
        ):
            self._link_boss_scene_target(target, event_time, timestamp)
        linked_target = self._is_encounter_damage_target(target)
        if (
            (priority_target or linked_target)
            and attacker not in self.encounter_member_ids
            and len(self.encounter_member_ids) >= MAX_PARTY_MEMBERS
        ):
            return
        if priority_target:
            target_before_gap = self.monsters.get(
                int(self.combat_target_id or 0)
            )
            incoming_before_gap = self.monsters.get(target)
            phase_target_continuation = bool(
                self.combat_target_id is not None
                and target != self.combat_target_id
                and self._boss_phase_continues(
                    target_before_gap, incoming_before_gap
                )
            )
            if (
                self.last_damage_time
                and event_time - self.last_damage_time
                > (self.encounter_gap if self.boss_only else self.idle_gap)
                and self._damage_gap_starts_new_encounter()
                and not phase_target_continuation
            ):
                self.reset(
                    keep_identity=True,
                    keep_monsters=True,
                    archive_reason="new_encounter",
                )
            current_target = self.monsters.get(int(self.combat_target_id or 0))
            current_defeated = bool(
                current_target is not None
                and current_target.current_hp is not None
                and current_target.current_hp <= 0
                and current_target.death_confirmed
            )
            if self.combat_end_time:
                if phase_target_continuation:
                    self.combat_end_time = 0.0
                    self.combat_end_reason = ""
                elif (
                    target == self.combat_target_id
                    and current_target is not None
                    and current_target.death_confirmed
                ):
                    # Ignore damage already in flight when the explicit death
                    # packet arrived; it cannot reopen the completed encounter.
                    return
                self.reset(
                    keep_identity=True,
                    keep_monsters=True,
                    archive_reason=self.combat_end_reason or "new_encounter",
                )
            elif self.combat_target_id is not None and target != self.combat_target_id:
                previous = self.monsters.get(self.combat_target_id)
                incoming = self.monsters.get(target)
                dummy_switch = bool(
                    self.boss_only
                    and self._is_dummy_target(self.combat_target_id)
                    and self._is_dummy_target(target)
                )
                same_phase_group = bool(
                    self.boss_only
                    and self._bosses_share_phase(previous, incoming)
                )
                phase_continuation = bool(
                    same_phase_group
                    and self._boss_phase_continues(previous, incoming)
                )
                separate_encounter = bool(
                    previous is not None
                    and incoming is not None
                    and (
                        int(previous.template_id or 0),
                        int(incoming.template_id or 0),
                    )
                    in SEPARATE_BOSS_ENCOUNTER_TRANSITIONS
                )
                idle_switch = bool(
                    self.last_damage_time
                    and event_time - self.last_damage_time
                    > self.idle_gap
                )
                if dummy_switch:
                    self.reset(
                        keep_identity=True,
                        keep_monsters=True,
                        archive_reason="new_encounter",
                    )
                elif separate_encounter:
                    # 洛克·金·失控 is a new Boss battle, not a second phase of
                    # the preceding 洛克·金 record. Archive the completed first
                    # fight and let this very hit start a clean encounter.
                    self.reset(
                        keep_identity=True,
                        keep_monsters=True,
                        archive_reason="new_encounter",
                    )
                elif phase_continuation:
                    self.linked_boss_target_ids.update(
                        {self.combat_target_id, target}
                    )
                    self.combat_target_id = target
                    self._register_encounter_target(target)
                elif same_phase_group:
                    # The only linked direction is Ancestor Armor -> Baldwin.
                    # Baldwin -> a new Ancestor Armor entity is a repull after
                    # a wipe, even if the old Boss never emitted a death packet.
                    self.reset(
                        keep_identity=True,
                        keep_monsters=True,
                        archive_reason="new_encounter",
                    )
                elif idle_switch:
                    self.reset(
                        keep_identity=True,
                        keep_monsters=True,
                        archive_reason="new_encounter",
                    )
                elif self.boss_only and current_defeated:
                    self.reset(
                        keep_identity=True,
                        keep_monsters=True,
                        archive_reason="new_encounter",
                    )
                elif self.boss_only:
                    # A secondary/lower Boss must not contaminate the locked
                    # encounter while the primary target is still active.
                    return
            if self.combat_target_id is None:
                self.combat_target_id = target
            elif not self.boss_only and target != self.combat_target_id:
                current_target = self.monsters.get(self.combat_target_id)
                incoming_target = self.monsters.get(target)
                current_priority = (
                    (
                        self._monster_rank(current_target),
                        int(current_target.level or 0),
                    )
                    if current_target is not None
                    else (-1, -1)
                )
                incoming_priority = (
                    (
                        self._monster_rank(incoming_target),
                        int(incoming_target.level or 0),
                    )
                    if incoming_target is not None
                    else (-1, -1)
                )
                if current_defeated or incoming_priority > current_priority:
                    self.combat_target_id = target
            current_target = self.monsters.get(target)
            if (
                current_target is not None
                and current_target.current_hp is not None
                and current_target.current_hp <= 0
            ):
                current_target.current_hp = None
                current_target.death_time_100ns = 0
            self._register_encounter_target(target)
            self._arm_team_encounter(target, timestamp)
            self._update_encounter_roster(attacker)
        elif linked_target:
            if self.combat_end_time or self._target_hp_depleted():
                # Adds belong to this encounter only while the Boss is alive.
                # Daily dungeons can leave ordinary monsters on screen after
                # the Boss reaches zero HP; those late hits must not extend or
                # change the finished DPS result. Multi-phase encounters still
                # return False from _target_hp_depleted() by design.
                return
            self._register_encounter_target(
                target, add=target in self.encounter_add_target_ids
            )
            self._update_encounter_roster(attacker)
        if not priority_target and not linked_target:
            self.discarded_non_encounter_events += 1
            return
        if (priority_target or linked_target) and timestamp:
            self.target_activity_100ns[target] = timestamp
            if (
                self.boss_reset_pending_100ns
                and timestamp > self.boss_reset_pending_100ns
            ):
                # Damage continuing before the party leaves fight mode means
                # this was an ordinary Boss heal, not a completed wipe/reset.
                self.boss_reset_pending_100ns = 0
        self.events.append(event)
        # Retain ample history for a long boss fight without unbounded growth.
        history_trimmed = False
        if len(self.events) > 200_000:
            self.events = self.events[-150_000:]
            history_trimmed = True
        self.enemy_ids.add(target)
        self.friendly_ids.add(attacker)
        if attacker not in self.friend_order:
            self.friend_order.append(attacker)
        self.active_target_id = self.combat_target_id
        if history_trimmed or not self._apply_event_incrementally(event):
            self._recompute()
        if self.first_damage_time and (
            self.session_number != previous_session_number
            or (encounter_was_empty and self.session_number > 0)
        ):
            # A completed stage summary can arrive beside the first hit on the
            # next Boss. Keep a short plausibility gate so those old totals do
            # not flash as the new pull's DPS.
            self.stage_summary_guard_until = max(
                self.stage_summary_guard_until,
                event_time + 3.0,
            )

    def _resolve_combat_sides(self) -> None:
        current_friendly = set(self.party_ids) | self.provisional_party_ids
        if self.self_id is not None:
            current_friendly.add(self.self_id)

        if self.combat_target_id is None:
            target_candidates = {
                int(event["target_id"])
                for event in self.events
                if int(event.get("damage", 0)) > 0
                and self._is_priority_target(int(event["target_id"]))
            }
            if target_candidates:
                self.combat_target_id = max(
                    target_candidates,
                    key=lambda entity_id: self._monster_priority(
                        self.monsters[entity_id]
                    ),
                )

        damage_target_ids = self._encounter_damage_target_ids()
        observed_attackers = {
            int(event["attacker_id"])
            for event in self.events
            if int(event.get("damage", 0)) > 0
            and int(event["target_id"]) in damage_target_ids
            and self._actor_counts_for_encounter(int(event["attacker_id"]))
        }
        friendly = current_friendly | observed_attackers
        enemies = damage_target_ids
        for actor_id in [*current_friendly, *observed_attackers]:
            if actor_id not in self.friend_order:
                self.friend_order.append(actor_id)
        self.friendly_ids = friendly
        self.enemy_ids = enemies
        self.active_target_id = self.combat_target_id

    def _recompute(self) -> None:
        stats: dict[int, ActorStats] = {}
        first = 0.0
        last = 0.0
        damage_target_ids = self._encounter_damage_target_ids()
        team_snapshot_events: dict[int, ActorStats] = {}

        for event in self.events:
            attacker = int(event["attacker_id"])
            target = int(event["target_id"])
            damage = int(event["damage"])
            timestamp = int(event.get("filetime_100ns", 0) or 0)
            if (
                damage <= 0
                or target not in damage_target_ids
                or not self._actor_counts_for_encounter(attacker)
            ):
                continue
            event_time = self._event_seconds(event)
            if not first or event_time < first:
                first = event_time
            last = max(last, event_time)
            skill_id = int(event.get("skill_id", 0)) or self.normalize_damage_skill_id(
                event.get("arg4_u64", 0)
            )
            team_state = self.team_damage_states.get(attacker)
            if (
                team_state is not None
                and team_state.authoritative_snapshot
                and team_state.snapshot_time_100ns > 0
                and timestamp <= team_state.snapshot_time_100ns
            ):
                actor = team_snapshot_events.setdefault(
                    attacker, ActorStats(attacker)
                )
                raw_critical = event.get("critical")
                actor.add(
                    damage,
                    event_time,
                    skill_id,
                    target,
                    (
                        bool(raw_critical)
                        if isinstance(raw_critical, bool)
                        else None
                    ),
                )
                continue
            actor = stats.setdefault(attacker, ActorStats(attacker))
            raw_critical = event.get("critical")
            actor.add(
                damage,
                event_time,
                skill_id,
                target,
                (
                    bool(raw_critical)
                    if isinstance(raw_critical, bool)
                    else None
                ),
            )
        for actor_id, state in self.team_damage_states.items():
            if (
                state.accepted_damage <= 0
                or not self._actor_counts_for_encounter(actor_id)
            ):
                continue
            actor = stats.setdefault(actor_id, ActorStats(actor_id))
            observed = team_snapshot_events.get(actor_id)
            actor.damage += state.accepted_damage
            if observed is not None and observed.damage > 0:
                # Every amount in ``observed`` came from an exact damage
                # callback. Preserve those absolute amounts byte-for-byte.
                # The Common total does not contain enough information to
                # distribute its remainder among skills or targets, so it is
                # never used as a scaling factor.
                for skill_id, source in observed.skills.items():
                    skill = actor.skills.setdefault(skill_id, SkillStats(skill_id))
                    skill.damage += source.damage
                    skill.hits += source.hits
                    skill.max_hit = max(skill.max_hit, source.max_hit)
                    skill.first_time = (
                        min(skill.first_time, source.first_time)
                        if skill.first_time and source.first_time
                        else skill.first_time or source.first_time
                    )
                    skill.last_time = max(skill.last_time, source.last_time)
                for target_id, exact_damage in observed.target_damage.items():
                    actor.target_damage[target_id] = (
                        actor.target_damage.get(target_id, 0) + exact_damage
                    )
                actor.hits += observed.hits
                actor.max_hit = max(actor.max_hit, observed.max_hit)
                if observed.damage_hits is not None:
                    actor.damage_hits = (
                        (actor.damage_hits or 0) + observed.damage_hits
                    )
                    actor.critical_hits = (
                        (actor.critical_hits or 0)
                        + int(observed.critical_hits or 0)
                    )
                if observed.first_time and (
                    not actor.first_time or observed.first_time < actor.first_time
                ):
                    actor.first_time = observed.first_time
                actor.last_time = max(actor.last_time, observed.last_time)

            # The positive difference is known only as an aggregate. Keeping
            # it under an explicit zero-ID row makes the skill sum reconcile
            # without changing a single known skill amount. If exact callbacks
            # exceed the Common total, retain the conflict for diagnostics;
            # silently shrinking exact skills would manufacture data.
            unclassified_damage = max(
                0,
                state.accepted_damage
                - (observed.damage if observed is not None else 0),
            )
            if unclassified_damage:
                aggregate = actor.skills.setdefault(0, SkillStats(0))
                aggregate.damage += unclassified_damage
                aggregate.first_time = (
                    min(aggregate.first_time, state.first_time)
                    if aggregate.first_time and state.first_time
                    else aggregate.first_time or state.first_time
                )
                aggregate.last_time = max(aggregate.last_time, state.last_time)
            if state.first_time and (
                not actor.first_time or state.first_time < actor.first_time
            ):
                actor.first_time = state.first_time
            actor.last_time = max(actor.last_time, state.last_time)
            if state.first_time and (not first or state.first_time < first):
                first = state.first_time
            last = max(last, state.last_time)
        for actor_id, (damage_hits, critical_hits) in self.stage_actor_metrics.items():
            actor = stats.get(actor_id)
            if actor is None or damage_hits <= 0 or critical_hits > damage_hits:
                continue
            actor.damage_hits = damage_hits
            actor.critical_hits = critical_hits
        self._apply_stage_skill_snapshots(stats)
        self.stats = stats
        self.first_damage_time = first
        self.last_damage_time = last
        if self._is_dummy_encounter():
            visible_members = {
                actor_id
                for actor_id, actor in stats.items()
                if actor.damage > 0 and self._actor_counts_for_encounter(actor_id)
            }
            self.encounter_member_ids = visible_members
            self.encounter_team_size = len(visible_members)

    @staticmethod
    def _stage_skill_snapshot(
        summary_id: str,
        timestamp: int,
        raw_actor: dict,
    ) -> StageSkillSnapshot | None:
        try:
            actor_id = int(raw_actor.get("actor_id", 0) or 0)
            actor_damage = max(0, int(raw_actor.get("damage", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return None
        if actor_id <= 0 or actor_damage <= 0:
            return None
        parsed_skills: dict[int, tuple[int, int]] = {}
        for raw_skill in raw_actor.get("skills", []):
            if not isinstance(raw_skill, dict):
                continue
            try:
                skill_id = int(raw_skill.get("skill_id", 0) or 0)
                damage = max(0, int(raw_skill.get("damage", 0) or 0))
                hits = max(0, int(raw_skill.get("hits", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if skill_id <= 0 or damage <= 0:
                continue
            previous_damage, previous_hits = parsed_skills.get(skill_id, (0, 0))
            parsed_skills[skill_id] = (
                previous_damage + damage,
                previous_hits + hits,
            )
        classified_damage = sum(
            damage for damage, _hits in parsed_skills.values()
        )
        if not parsed_skills or classified_damage > actor_damage:
            return None
        return StageSkillSnapshot(
            summary_id=summary_id,
            actor_id=actor_id,
            actor_damage=actor_damage,
            filetime_100ns=timestamp,
            skills=parsed_skills,
            unclassified_damage=actor_damage - classified_damage,
        )

    def active_stage_skill_snapshot(
        self,
        actor_id: int,
        actor: ActorStats | None = None,
    ) -> StageSkillSnapshot | None:
        snapshot = self.stage_skill_snapshots.get(int(actor_id))
        if snapshot is None:
            return None
        current = actor if actor is not None else self.stats.get(int(actor_id))
        if current is None or current.damage != snapshot.actor_damage:
            return None
        return snapshot

    def _apply_stage_skill_snapshots(
        self, stats: dict[int, ActorStats]
    ) -> None:
        for actor_id, snapshot in self.stage_skill_snapshots.items():
            actor = stats.get(actor_id)
            if actor is None or actor.damage != snapshot.actor_damage:
                continue
            exact_skills: dict[int, SkillStats] = {}
            for skill_id, (damage, hits) in snapshot.skills.items():
                exact_skills[skill_id] = SkillStats(
                    skill_id=skill_id,
                    damage=damage,
                    hits=hits,
                    max_hit=0,
                    first_time=actor.first_time,
                    last_time=actor.last_time,
                )
            if snapshot.unclassified_damage > 0:
                exact_skills[0] = SkillStats(
                    skill_id=0,
                    damage=snapshot.unclassified_damage,
                    hits=0,
                    max_hit=0,
                    first_time=actor.first_time,
                    last_time=actor.last_time,
                )
            actor.skills = exact_skills

    def ingest_stage_summary(self, update: dict) -> bool:
        summary_id = str(update.get("summary_id", "")).strip()
        authoritative = bool(update.get("authoritative"))
        completion_confirmed = bool(update.get("completion_confirmed"))
        try:
            timestamp = int(update.get("filetime_100ns", 0) or 0)
            member_count = max(0, int(update.get("member_count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return False
        if not summary_id or timestamp <= 0:
            return False
        if (
            summary_id in self.seen_stage_summary_ids
            or summary_id in self.rejected_stage_summary_ids
        ):
            return False
        if not self._encounter_damage_target_ids() or not self.first_damage_time:
            self.rejected_stage_summary_ids.add(summary_id)
            return False

        summary_total = 0
        summary_actor_ids: set[int] = set()
        summary_damage_by_actor: dict[int, int] = {}
        for raw_actor in update.get("actors", []):
            if not isinstance(raw_actor, dict):
                continue
            try:
                actor_id = int(raw_actor.get("actor_id", 0) or 0)
                damage = max(0, int(raw_actor.get("damage", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if actor_id <= 0 or not self._actor_counts_for_encounter(actor_id):
                continue
            summary_damage_by_actor[actor_id] = damage
            summary_total += damage
            if damage > 0:
                summary_actor_ids.add(actor_id)
        summary_time = self._event_seconds({"filetime_100ns": timestamp})
        observed_total = sum(actor.damage for actor in self.stats.values())
        encounter_edge = self.combat_end_time or self.last_damage_time
        summary_delay = (
            summary_time - encounter_edge if encounter_edge else float("inf")
        )
        observed_actor_ids = {
            actor_id for actor_id, actor in self.stats.items() if actor.damage > 0
        }
        observed_damage_by_actor = {
            actor_id: int(actor.damage)
            for actor_id, actor in self.stats.items()
        }
        actor_overlap = summary_actor_ids.intersection(observed_actor_ids)
        total_tolerance = max(
            STAGE_SUMMARY_TOTAL_TOLERANCE_ABSOLUTE,
            round(
                max(summary_total, observed_total)
                * STAGE_SUMMARY_TOTAL_TOLERANCE_RATIO
            ),
        )
        total_plausible = bool(
            summary_total > 0
            and observed_total > 0
            and abs(summary_total - observed_total) <= total_tolerance
            and min(summary_total, observed_total)
            >= max(summary_total, observed_total) * 0.5
        )
        self_plausible = True
        if self.self_id is not None and self.self_id in summary_damage_by_actor:
            observed_self = self.stats.get(self.self_id)
            observed_self_damage = observed_self.damage if observed_self else 0
            summary_self_damage = summary_damage_by_actor[self.self_id]
            self_tolerance = max(
                STAGE_SUMMARY_SELF_DAMAGE_TOLERANCE,
                round(max(observed_self_damage, summary_self_damage) * 0.10),
            )
            self_plausible = (
                abs(summary_self_damage - observed_self_damage) <= self_tolerance
            )
        final_window = (
            STAGE_SUMMARY_CONFIRMED_FINAL_WINDOW_SECONDS
            if completion_confirmed
            else STAGE_SUMMARY_FINAL_WINDOW_SECONDS
        )
        end_snapshot = bool(
            0 <= summary_delay <= final_window
            and (
                authoritative
                or
                self.combat_end_time
                or self._target_hp_depleted()
                or summary_delay >= self._encounter_idle_timeout()
            )
        )
        exact_snapshot = bool(
            summary_total > 0
            and actor_overlap
            and self_plausible
            and end_snapshot
            and (authoritative or total_plausible)
        )

        summary_covers_observed = observed_actor_ids.issubset(
            summary_damage_by_actor
        )
        comparison_actor_ids = sorted(
            set(summary_damage_by_actor) | observed_actor_ids
        )
        actor_differences = [
            {
                "actor_id": actor_id,
                "name": self.display_name(actor_id),
                "realtime_damage": observed_damage_by_actor.get(actor_id, 0),
                "summary_damage": int(summary_damage_by_actor.get(actor_id, 0)),
                "difference": int(summary_damage_by_actor.get(actor_id, 0))
                - observed_damage_by_actor.get(actor_id, 0),
            }
            for actor_id in comparison_actor_ids
        ]
        per_actor_exact_match = bool(
            actor_differences
            and summary_covers_observed
            and all(not row["difference"] for row in actor_differences)
        )
        authoritative_total_plausible = bool(
            summary_total > 0
            and observed_total > 0
            and min(summary_total, observed_total)
            >= max(summary_total, observed_total) * 0.5
        )
        encounter_concluded = bool(
            self.combat_end_time or self._target_hp_depleted()
        )
        confirmed_multiphase_completion = bool(
            self._multiphase_encounter()
            and completion_confirmed
            and self._target_hp_depleted()
        )
        would_allow_legacy_correction = bool(
            authoritative
            and end_snapshot
            and encounter_concluded
            and (
                not self._multiphase_encounter()
                or confirmed_multiphase_completion
            )
            and summary_covers_observed
            and self_plausible
            and authoritative_total_plausible
        )

        # Completion tables are retained only for diagnostics. Live and final
        # damage both come from Common/Dirty absolute counters; a later table
        # must never rewrite values that were already visible during combat.
        validation_update = dict(update)
        validation_update["validation_only"] = True
        validation_update["exact_for_encounter"] = False
        validation_update["validation"] = {
            "summary_total": summary_total,
            "observed_total": observed_total,
            "difference": summary_total - observed_total,
            "absolute_difference": abs(summary_total - observed_total),
            "total_tolerance": total_tolerance,
            "total_plausible": total_plausible,
            "self_plausible": self_plausible,
            "actor_overlap_count": len(actor_overlap),
            "summary_delay_seconds": (
                None
                if summary_delay == float("inf")
                else round(summary_delay, 6)
            ),
            "end_snapshot": end_snapshot,
            "would_have_matched": exact_snapshot,
            "summary_covers_observed": summary_covers_observed,
            "authoritative_total_plausible": authoritative_total_plausible,
            "encounter_concluded": encounter_concluded,
            "confirmed_multiphase_completion": confirmed_multiphase_completion,
            "would_allow_legacy_correction": would_allow_legacy_correction,
            "damage_correction_applied": False,
            "per_actor_exact_match": per_actor_exact_match,
            "actor_differences": actor_differences,
        }
        self.stage_summaries[summary_id] = validation_update

        if summary_time <= self.stage_summary_guard_until or (
            not authoritative and end_snapshot and not total_plausible
        ):
            self.rejected_stage_summary_ids.add(summary_id)
            self._queue_current_record_refresh()
            return False

        skill_snapshots_changed = False
        applied_skill_actor_ids: list[int] = []
        if end_snapshot and (authoritative or completion_confirmed):
            for raw_actor in update.get("actors", []):
                if not isinstance(raw_actor, dict):
                    continue
                snapshot = self._stage_skill_snapshot(
                    summary_id, timestamp, raw_actor
                )
                if snapshot is None or snapshot.actor_id == self.self_id:
                    continue
                observed_actor = self.stats.get(snapshot.actor_id)
                if (
                    observed_actor is None
                    or observed_actor.damage != snapshot.actor_damage
                    or not self._actor_counts_for_encounter(snapshot.actor_id)
                ):
                    continue
                if self.stage_skill_snapshots.get(snapshot.actor_id) != snapshot:
                    self.stage_skill_snapshots[snapshot.actor_id] = snapshot
                    skill_snapshots_changed = True
                applied_skill_actor_ids.append(snapshot.actor_id)
        validation_update["validation"]["server_skill_actor_ids"] = sorted(
            set(applied_skill_actor_ids)
        )

        metrics_changed = False
        if (
            not authoritative
            and self.first_damage_time
            and summary_time >= self.first_damage_time
            and -0.5 <= summary_delay <= 8.0
        ):
            active_actor_ids = set(self.stats)
            for raw_actor in update.get("actors", []):
                if not isinstance(raw_actor, dict):
                    continue
                try:
                    actor_id = int(raw_actor.get("actor_id", 0) or 0)
                    damage = max(0, int(raw_actor.get("damage", 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    not actor_id
                    or damage <= 0
                    or actor_id not in active_actor_ids
                    or not self._actor_counts_for_encounter(actor_id)
                ):
                    continue
                raw_hits = raw_actor.get("damage_hits")
                raw_critical = raw_actor.get("critical_hits")
                if raw_hits is not None and raw_critical is not None:
                    try:
                        damage_hits = max(0, int(raw_hits))
                        critical_hits = max(0, int(raw_critical))
                    except (TypeError, ValueError, OverflowError):
                        damage_hits = 0
                        critical_hits = 0
                    if damage_hits > 0 and critical_hits <= damage_hits:
                        metrics = (damage_hits, critical_hits)
                        if self.stage_actor_metrics.get(actor_id) != metrics:
                            self.stage_actor_metrics[actor_id] = metrics
                            metrics_changed = True
                # Stage rows can retain deaths from earlier pulls. Deaths are
                # therefore always counted from live life-state transitions.
        if skill_snapshots_changed or metrics_changed:
            self._recompute()
            self._queue_current_record_refresh()

        # Mid-fight stage rows may supply crit counters. A validated final
        # settlement may also have reconciled the damage totals above.
        self.seen_stage_summary_ids.add(summary_id)
        self.latest_network_time_100ns = max(self.latest_network_time_100ns, timestamp)
        self._queue_current_record_refresh()
        return True

    def ingest_identity(self, update: dict) -> bool:
        try:
            entity_id = int(update.get("entity_id", 0))
        except (TypeError, ValueError):
            return False
        if not entity_id or entity_id in self.non_player_actor_ids:
            return False
        previous_self_id = self.self_id
        changed = previous_self_id != entity_id
        self.self_id = entity_id
        if previous_self_id and previous_self_id != entity_id:
            previous_time = self.member_life_times.pop(previous_self_id, 0)
            previous_state = self.member_death_states.pop(
                previous_self_id, None
            )
            if previous_time >= self.member_life_times.get(entity_id, 0):
                if previous_state is not None:
                    self.member_death_states[entity_id] = previous_state
                    self.member_life_times[entity_id] = previous_time
            previous_deaths = self.member_death_counts.pop(previous_self_id, 0)
            if previous_deaths:
                self.member_death_counts[entity_id] = (
                    self.member_death_counts.get(entity_id, 0) + previous_deaths
                )
        self.party_ids.discard(entity_id)
        self.provisional_party_ids.discard(entity_id)
        self.party_member_count = max(1, self.party_member_count)
        if entity_id in self.friend_order:
            self.friend_order.remove(entity_id)
        self.friend_order.insert(0, entity_id)
        automatic_name = self.entity_names.get(entity_id, "").strip()
        if automatic_name:
            self.local_player_name = automatic_name
        self._resolve_combat_sides()
        self._recompute()
        replayed = self._replay_pending_member_events()
        return changed or replayed

    def ingest_party(self, update: dict) -> bool:
        left_team = bool(update.get("left_team"))
        if left_team and self._encounter_started():
            self.reset(
                keep_identity=True,
                keep_monsters=False,
                archive_reason="party_exit",
            )

        parsed: set[int] = set()
        values = update.get("entity_ids", [])
        if isinstance(values, (list, tuple, set)):
            for value in values:
                try:
                    entity_id = int(value)
                except (TypeError, ValueError):
                    continue
                if entity_id:
                    parsed.add(entity_id)
        parsed.difference_update(self.non_player_actor_ids)
        if self.self_id is not None:
            parsed.discard(self.self_id)
        inferred_count = len(parsed) + (1 if self.self_id is not None else 0)
        try:
            member_count = max(0, int(update.get("member_count", inferred_count)))
        except (TypeError, ValueError, OverflowError):
            member_count = inferred_count
        member_count = min(
            MAX_PARTY_MEMBERS, max(member_count, inferred_count)
        )
        previous_provisional_ids = set(self.provisional_party_ids)
        official_positive_ids = {entity_id for entity_id in parsed if entity_id > 0}
        self.provisional_party_ids.difference_update(official_positive_ids)
        resolved_count = len(official_positive_ids) + (
            1 if self.self_id is not None else 0
        )
        provisional_slots = max(0, member_count - resolved_count)
        if len(self.provisional_party_ids) > provisional_slots:
            ordered_provisional = [
                actor_id
                for actor_id in self.friend_order
                if actor_id in self.provisional_party_ids
            ]
            ordered_provisional.extend(
                sorted(self.provisional_party_ids - set(ordered_provisional))
            )
            self.provisional_party_ids = set(
                ordered_provisional[:provisional_slots]
            )
        changed = (
            not self.party_known
            or parsed != self.party_ids
            or member_count != self.party_member_count
            or self.provisional_party_ids != previous_provisional_ids
        )
        authoritative = bool(update.get("authoritative"))
        self.party_known = True
        if authoritative:
            self.party_roster_authoritative = True
        self.party_ids = parsed
        self.party_member_count = member_count
        if left_team:
            self.provisional_party_ids.clear()
            self.party_member_count = 1 if self.self_id is not None else 0
            self.team_damage_states.clear()
        current_members = self._current_member_ids()
        self.member_death_states = {
            actor_id: dead
            for actor_id, dead in self.member_death_states.items()
            if actor_id in current_members
        }
        self.member_life_times = {
            actor_id: timestamp
            for actor_id, timestamp in self.member_life_times.items()
            if actor_id in current_members
        }
        update_is_during_encounter = True
        try:
            update_timestamp = int(update.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            update_timestamp = 0
        if update_timestamp and self.last_damage_time:
            update_time = self._event_seconds({"filetime_100ns": update_timestamp})
            elapsed = update_time - self.last_damage_time
            update_is_during_encounter = (
                -1.0 <= elapsed <= self._encounter_idle_timeout()
            )
        if (
            self._encounter_started()
            and not self.combat_end_time
            and update_is_during_encounter
        ):
            # Party membership is volatile and is not the participant count.
            # Only actors with accepted encounter damage appear in the record.
            self.encounter_team_size = min(
                MAX_PARTY_MEMBERS, len(self.encounter_member_ids)
            )
        reordered = [
            actor_id
            for actor_id in self.friend_order
            if actor_id in current_members and actor_id != self.self_id
        ]
        for actor_id in sorted(parsed):
            if actor_id not in reordered:
                reordered.append(actor_id)
        for actor_id in self.provisional_party_ids:
            if actor_id not in reordered:
                reordered.append(actor_id)
        self.friend_order = (
            [self.self_id, *reordered]
            if self.self_id is not None
            else reordered
        )
        self._resolve_combat_sides()
        self._recompute()
        replayed = self._replay_pending_member_events()
        return left_team or changed or replayed

    def ingest_scene(self, update: dict) -> bool:
        if update.get("transition"):
            had_scene_state = bool(
                self.scene_id is not None
                or self.monsters
                or self.combat_target_id is not None
                or self._encounter_started()
            )
            self.provisional_party_ids.clear()
            self.reset(
                keep_identity=True,
                keep_monsters=False,
                archive_reason="space_transition",
            )
            self.member_death_states.clear()
            self.member_life_times.clear()
            self.scene_id = None
            return had_scene_state
        try:
            scene_id = int(update.get("scene_id", 0))
        except (TypeError, ValueError, OverflowError):
            return False
        if scene_id <= 0:
            return False
        previous_scene_id = self.scene_id
        force_reset = bool(update.get("force_reset"))
        visible_entity_ids: set[int] = set()
        raw_entity_ids = update.get("entity_ids", ())
        if isinstance(raw_entity_ids, (list, tuple, set)):
            for raw_entity_id in raw_entity_ids:
                try:
                    entity_id = int(raw_entity_id or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if entity_id:
                    visible_entity_ids.add(entity_id)
        refreshed_targets = self._encounter_damage_target_ids() | set(
            self.encounter_target_ids
        )
        preserves_active_encounter = bool(
            previous_scene_id == scene_id
            and force_reset
            and self._encounter_started()
            and refreshed_targets.intersection(visible_entity_ids)
        )
        first_refresh_ends_encounter = bool(
            previous_scene_id is None
            and self._encounter_started()
            and refreshed_targets
            and not refreshed_targets.intersection(visible_entity_ids)
        )
        changed = first_refresh_ends_encounter or (
            previous_scene_id is not None
            and (
                scene_id != previous_scene_id
                or (force_reset and not preserves_active_encounter)
            )
        )
        if changed:
            self.provisional_party_ids.clear()
            self.reset(
                keep_identity=True,
                keep_monsters=False,
                archive_reason=(
                    "scene_refresh"
                    if scene_id == previous_scene_id
                    else "scene_change"
                ),
            )
            self.member_death_states.clear()
            self.member_life_times.clear()
        self.scene_id = scene_id
        return changed

    def merge_actor(self, update: dict) -> bool:
        try:
            old_actor = int(update.get("from_actor_id", 0))
            new_actor = int(update.get("to_actor_id", 0))
        except (TypeError, ValueError, OverflowError):
            return False
        if not old_actor or not new_actor or old_actor == new_actor:
            return False

        changed = False
        if old_actor in self.provisional_party_ids:
            self.provisional_party_ids.discard(old_actor)
            if new_actor != self.self_id:
                self.provisional_party_ids.add(new_actor)
            changed = True
        elif new_actor in self.provisional_party_ids:
            self.provisional_party_ids.discard(new_actor)
            changed = True
        if old_actor in self.party_ids:
            self.party_ids.discard(old_actor)
            if new_actor != self.self_id:
                self.party_ids.add(new_actor)
            changed = True
        duplicate_encounter_member = (
            old_actor in self.encounter_member_ids
            and new_actor in self.encounter_member_ids
        )
        if old_actor in self.encounter_member_ids:
            self.encounter_member_ids.discard(old_actor)
            self.encounter_member_ids.add(new_actor)
            if duplicate_encounter_member and self.encounter_team_size > 0:
                self.encounter_team_size = max(
                    len(self.encounter_member_ids),
                    self.encounter_team_size - 1,
                )
                self.encounter_team_size = min(
                    MAX_PARTY_MEMBERS, self.encounter_team_size
                )
            changed = True

        reordered: list[int] = []
        for actor_id in self.friend_order:
            candidate = new_actor if actor_id == old_actor else actor_id
            if candidate not in reordered:
                reordered.append(candidate)
        self.friend_order = reordered

        old_name = self.entity_names.pop(old_actor, "")
        if old_name and not self.entity_names.get(new_actor):
            self.entity_names[new_actor] = old_name
            changed = True
        old_profession = self.entity_professions.pop(old_actor, None)
        if old_profession and not self.entity_professions.get(new_actor):
            self.entity_professions[new_actor] = old_profession
            changed = True

        old_life_time = self.member_life_times.pop(old_actor, 0)
        old_dead = self.member_death_states.pop(old_actor, None)
        if old_life_time >= self.member_life_times.get(new_actor, 0):
            if old_dead is not None:
                self.member_death_states[new_actor] = old_dead
                self.member_life_times[new_actor] = old_life_time
                changed = True

        old_deaths = self.member_death_counts.pop(old_actor, 0)
        if old_deaths:
            self.member_death_counts[new_actor] = (
                self.member_death_counts.get(new_actor, 0) + old_deaths
            )
            changed = True

        old_combat_time = self.entity_combat_state_times.pop(old_actor, 0)
        old_in_combat = self.entity_combat_states.pop(old_actor, None)
        if old_combat_time >= self.entity_combat_state_times.get(new_actor, 0):
            if old_in_combat is not None:
                self.entity_combat_states[new_actor] = old_in_combat
                self.entity_combat_state_times[new_actor] = old_combat_time
                changed = True

        old_state = self.team_damage_states.pop(old_actor, None)
        if old_state is not None:
            new_state = self.team_damage_states.get(new_actor)
            if new_state is None:
                old_state.actor_id = new_actor
                self.team_damage_states[new_actor] = old_state
            else:
                old_authoritative_is_newer = bool(
                    old_state.authoritative_snapshot
                    and (
                        not new_state.authoritative_snapshot
                        or old_state.snapshot_time_100ns
                        >= new_state.snapshot_time_100ns
                    )
                )
                if old_authoritative_is_newer:
                    new_state.last_absolute = old_state.last_absolute
                    new_state.baseline_absolute = old_state.baseline_absolute
                    new_state.accepted_damage = old_state.accepted_damage
                    new_state.server_time = old_state.server_time
                elif (
                    not new_state.authoritative_snapshot
                    and old_state.snapshot_time_100ns
                    >= new_state.snapshot_time_100ns
                ):
                    new_state.last_absolute = old_state.last_absolute
                    new_state.baseline_absolute = old_state.baseline_absolute
                    new_state.accepted_damage = old_state.accepted_damage
                    new_state.server_time = old_state.server_time
                new_state.has_snapshot |= old_state.has_snapshot
                new_state.authoritative_snapshot |= (
                    old_state.authoritative_snapshot
                )
                new_state.snapshot_time_100ns = max(
                    new_state.snapshot_time_100ns,
                    old_state.snapshot_time_100ns,
                )
                times = [
                    value
                    for value in (new_state.first_time, old_state.first_time)
                    if value
                ]
                new_state.first_time = min(times) if times else 0.0
                new_state.last_time = max(new_state.last_time, old_state.last_time)
            changed = True

        old_metrics = self.stage_actor_metrics.pop(old_actor, None)
        if old_metrics is not None:
            current_metrics = self.stage_actor_metrics.get(new_actor)
            if current_metrics is None or old_metrics[0] > current_metrics[0]:
                self.stage_actor_metrics[new_actor] = old_metrics
            changed = True

        for event in self.events:
            if int(event.get("attacker_id", 0)) == old_actor:
                event["attacker_id"] = new_actor
                changed = True
            if int(event.get("target_id", 0)) == old_actor:
                event["target_id"] = new_actor
                changed = True
        for event in self.pending_member_events:
            if int(event.get("attacker_id", 0)) == old_actor:
                event["attacker_id"] = new_actor
                changed = True
            if int(event.get("target_id", 0)) == old_actor:
                event["target_id"] = new_actor
                changed = True
        self.monsters.pop(old_actor, None)
        if self.self_id == new_actor and self.entity_names.get(new_actor):
            self.local_player_name = self.entity_names[new_actor]
        self._resolve_combat_sides()
        self._recompute()
        replayed = self._replay_pending_member_events()
        return changed or replayed

    def ingest_life(self, update: dict) -> bool:
        try:
            actor_id = int(update.get("actor_id", 0) or 0)
            timestamp = int(update.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            not actor_id
            or not timestamp
            or "dead" not in update
            or actor_id not in self._current_member_ids()
            or timestamp < self.member_life_times.get(actor_id, 0)
        ):
            return False
        dead = bool(update.get("dead"))
        if dead and update.get("death_confirmed") is not True:
            return False
        previous_dead = self.member_death_states.get(actor_id)
        changed = previous_dead != dead
        self.member_death_states[actor_id] = dead
        self.member_life_times[actor_id] = timestamp
        event_time = self._event_seconds(update)
        if (
            dead
            and previous_dead is not True
            and self.first_damage_time
            and event_time >= self.first_damage_time
        ):
            self.member_death_counts[actor_id] = (
                self.member_death_counts.get(actor_id, 0) + 1
            )
        wiped = self._mark_party_wipe_if_complete(event_time)
        return changed or wiped

    def ingest_combat_state(self, update: dict) -> bool:
        try:
            entity_id = int(update.get("entity_id", 0) or 0)
            timestamp = int(update.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            not entity_id
            or not timestamp
            or "in_combat" not in update
            or timestamp < self.entity_combat_state_times.get(entity_id, 0)
        ):
            return False
        in_combat = bool(update.get("in_combat"))
        previous = self.entity_combat_states.get(entity_id)
        self.entity_combat_states[entity_id] = in_combat
        self.entity_combat_state_times[entity_id] = timestamp
        self.latest_network_time_100ns = max(
            self.latest_network_time_100ns, timestamp
        )
        changed = previous is None or previous != in_combat
        if in_combat or not self.first_damage_time or self.combat_end_time:
            return changed

        reset_completed = self._mark_pending_reset_complete()
        monster = self.monsters.get(int(self.combat_target_id or 0))
        defeated = bool(monster and self._mark_target_defeated(monster))
        return changed or reset_completed or defeated

    def ingest_profile(self, update: dict) -> bool:
        try:
            entity_id = int(update.get("entity_id", 0))
        except (TypeError, ValueError):
            return False
        if not entity_id:
            return False
        was_priority_target = self._is_priority_target(entity_id)
        changed = False
        monster = self.monsters.setdefault(entity_id, MonsterStats(entity_id))
        try:
            boss_type_hint = int(update.get("boss_type", -1) or 0)
            boss_rank_hint = int(update.get("boss_rank", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            boss_type_hint = boss_rank_hint = 0
        entity_type_hint = str(update.get("entity_type", "")).casefold()
        known_boss = bool(
            self._monster_rank(monster) > 0
            or entity_id == self.combat_target_id
            or "boss" in entity_type_hint
            or "首领" in entity_type_hint
            or boss_type_hint == 3
            or boss_rank_hint > 0
        )
        if update.get("name") and not (
            known_boss and boss_name_is_placeholder(update.get("name"))
        ):
            changed |= self.ingest_name(update)
        entity_type = str(update.get("entity_type", "")).strip()[:64]
        if entity_type and monster.entity_type != entity_type:
            monster.entity_type = entity_type
            changed = True
        try:
            level = int(update["level"])
        except (KeyError, TypeError, ValueError):
            level = None
        if level is not None and 1 <= level <= 999 and monster.level != level:
            monster.level = level
            changed = True
        try:
            profession_id = int(update["profession_id"])
        except (KeyError, TypeError, ValueError):
            profession_id = 0
        if profession_id and self.entity_professions.get(entity_id) != profession_id:
            self.entity_professions[entity_id] = profession_id
            changed = True
        try:
            template_id = int(update["template_id"])
        except (KeyError, TypeError, ValueError):
            template_id = 0
        if template_id and monster.template_id != template_id:
            monster.template_id = template_id
            changed = True
        try:
            boss_type = int(update["boss_type"])
        except (KeyError, TypeError, ValueError):
            boss_type = None
        if boss_type is not None and monster.boss_type != boss_type:
            monster.boss_type = boss_type
            changed = True
        try:
            boss_rank = max(0, int(update["boss_rank"]))
        except (KeyError, TypeError, ValueError):
            boss_rank = 0
        if boss_rank and monster.boss_rank != boss_rank:
            monster.boss_rank = boss_rank
            changed = True
        if "encounter_auxiliary" in update:
            encounter_auxiliary = bool(update.get("encounter_auxiliary"))
            if monster.encounter_auxiliary != encounter_auxiliary:
                monster.encounter_auxiliary = encounter_auxiliary
                changed = True
        if "encounter_parent_template_ids" in update:
            raw_parent_ids = update.get("encounter_parent_template_ids", ())
            if not isinstance(raw_parent_ids, (list, tuple, set)):
                raw_parent_ids = (raw_parent_ids,)
            parent_ids: list[int] = []
            for raw_parent_id in raw_parent_ids:
                try:
                    parent_id = int(raw_parent_id or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if parent_id:
                    parent_ids.append(parent_id)
            normalized_parent_ids = tuple(sorted(set(parent_ids)))
            if monster.encounter_parent_template_ids != normalized_parent_ids:
                monster.encounter_parent_template_ids = normalized_parent_ids
                changed = True
        auxiliary_metadata = ENCOUNTER_AUXILIARY_TEMPLATES.get(
            int(monster.template_id or 0)
        )
        if auxiliary_metadata is not None:
            canonical_parent_ids = tuple(
                sorted(
                    {
                        int(parent_id)
                        for parent_id in auxiliary_metadata.get(
                            "parent_template_ids", ()
                        )
                        if int(parent_id)
                    }
                )
            )
            canonical_name = str(auxiliary_metadata.get("name", "")).strip()
            if not monster.encounter_auxiliary:
                monster.encounter_auxiliary = True
                changed = True
            if monster.encounter_parent_template_ids != canonical_parent_ids:
                monster.encounter_parent_template_ids = canonical_parent_ids
                changed = True
            if monster.entity_type != "Monster":
                monster.entity_type = "Monster"
                changed = True
            if canonical_name:
                if self.entity_names.get(entity_id) != canonical_name:
                    self.entity_names[entity_id] = canonical_name
                    changed = True
                if monster.name != canonical_name:
                    monster.name = canonical_name
                    changed = True
        name = self.entity_names.get(entity_id, "")
        if name and monster.name != name:
            monster.name = name
            changed = True
        confirmed_non_player = bool(
            monster.encounter_auxiliary
            or monster.boss_rank > 0
            or (
                monster.boss_type == 3
                and monster.entity_type.casefold() not in {"player", "role"}
            )
        )
        try:
            timestamp = int(update.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            timestamp = 0
        if timestamp and confirmed_non_player:
            monster.last_update_100ns = max(monster.last_update_100ns, timestamp)
            self.target_activity_100ns[entity_id] = max(
                self.target_activity_100ns.get(entity_id, 0), timestamp
            )
            self.latest_network_time_100ns = max(
                self.latest_network_time_100ns, timestamp
            )
        non_player_changed = False
        if confirmed_non_player:
            non_player_changed = self._mark_non_player_actor(entity_id)
            changed |= non_player_changed
        requires_recompute = bool(
            non_player_changed
            or was_priority_target != self._is_priority_target(entity_id)
        )
        if changed and requires_recompute:
            self._resolve_combat_sides()
            if self.combat_target_id == entity_id and any(
                int(event.get("target_id", 0)) == entity_id
                and int(event.get("damage", 0)) > 0
                for event in self.events
            ):
                self.encounter_target_ids.add(entity_id)
            self._recompute()
        self._mark_target_defeated(monster)
        replayed = False
        if self._is_priority_target(entity_id):
            replayed |= self._resolve_known_pending_target_events()
        replayed |= self._resolve_pending_target_events(entity_id)
        return changed or replayed

    def ingest_monster(self, update: dict) -> bool:
        try:
            entity_id = int(update.get("entity_id", 0))
        except (TypeError, ValueError):
            return False
        if not entity_id:
            return False
        previous_monster = self.monsters.get(entity_id)
        previous_update_100ns = (
            previous_monster.last_update_100ns
            if previous_monster is not None
            else 0
        )
        profile_changed = self.ingest_profile(update)
        monster = self.monsters.setdefault(entity_id, MonsterStats(entity_id))
        previous_current_hp = monster.current_hp
        changed = False
        for field_name in ("current_hp", "max_hp"):
            if field_name not in update:
                continue
            try:
                value = max(0.0, float(update[field_name]))
            except (TypeError, ValueError):
                continue
            if (
                field_name == "max_hp"
                and entity_id == self.combat_target_id
                and self.first_damage_time
                and monster.max_hp is not None
                and monster.max_hp > 0
            ):
                value = max(value, monster.max_hp)
            if getattr(monster, field_name) != value:
                setattr(monster, field_name, value)
                changed = True
            if field_name == "current_hp" and value > 0:
                monster.death_time_100ns = 0
                monster.death_confirmed = False
                observed = monster.observed_max_hp or 0.0
                if value > observed:
                    monster.observed_max_hp = value
                    changed = True
        timestamp = int(update.get("filetime_100ns", 0) or 0)
        if timestamp:
            monster.last_update_100ns = max(monster.last_update_100ns, timestamp)
            self.target_activity_100ns[entity_id] = max(
                self.target_activity_100ns.get(entity_id, 0), timestamp
            )
            self.latest_network_time_100ns = max(
                self.latest_network_time_100ns, timestamp
            )
            if (
                "current_hp" in update
                and previous_current_hp is not None
                and monster.current_hp is not None
                and monster.current_hp < previous_current_hp
                and (
                    not previous_update_100ns
                    or timestamp >= previous_update_100ns
                )
            ):
                monster.last_hp_drop_100ns = max(
                    monster.last_hp_drop_100ns, timestamp
                )
                self._arm_team_encounter(entity_id, timestamp)
            try:
                reset_candidate_hp = float(update["reset_candidate_hp"])
            except (KeyError, TypeError, ValueError, OverflowError):
                reset_candidate_hp = -1.0
            if (
                reset_candidate_hp >= 0
                and entity_id == self.combat_target_id
                and self.first_damage_time
                and not self.combat_end_time
                and previous_current_hp is not None
            ):
                hp_ceiling = self._monster_max_hp(monster)
                event_time = self._event_seconds(
                    {"filetime_100ns": timestamp}
                )
                missing_hp = max(0.0, hp_ceiling - previous_current_hp)
                if (
                    hp_ceiling > 0
                    and reset_candidate_hp >= hp_ceiling * 0.995
                    and missing_hp >= hp_ceiling * 0.2
                ):
                    self.boss_reset_pending_100ns = max(
                        self.boss_reset_pending_100ns, timestamp
                    )
                    self._mark_pending_reset_complete()
            if "current_hp" in update and monster.current_hp is not None:
                if monster.current_hp <= 0:
                    monster.death_time_100ns = max(
                        monster.death_time_100ns, timestamp
                    )
                    if update.get("death_confirmed"):
                        monster.death_confirmed = True
                elif (
                    entity_id == self.combat_target_id
                    and self.first_damage_time
                    and not self.combat_end_time
                    and previous_current_hp is not None
                    and monster.current_hp > previous_current_hp
                ):
                    hp_ceiling = self._monster_max_hp(monster)
                    event_time = self._event_seconds(
                        {"filetime_100ns": timestamp}
                    )
                    missing_hp_before_reset = max(
                        0.0, hp_ceiling - previous_current_hp
                    )
                    recovered_hp = monster.current_hp - previous_current_hp
                    meaningful_reset = max(1.0, hp_ceiling * 0.005)
                    hard_reset = missing_hp_before_reset >= hp_ceiling * 0.2
                    if (
                        hp_ceiling > 0
                        and missing_hp_before_reset >= meaningful_reset
                        and recovered_hp >= meaningful_reset
                        and monster.current_hp >= hp_ceiling * 0.995
                        and (
                            hard_reset
                            or event_time - self.last_damage_time
                            >= self.idle_gap
                        )
                    ):
                        # Full HP is only the first half of a reset signal. Keep
                        # the live encounter intact until fight-mode confirms
                        # that its combatants have left combat; ordinary Boss
                        # healing must not interrupt real-time DPS.
                        self.boss_reset_pending_100ns = max(
                            self.boss_reset_pending_100ns, timestamp
                        )
                        self._mark_pending_reset_complete()
        name = self.entity_names.get(entity_id, "")
        if name:
            monster.name = name
        if self._is_priority_target(entity_id) and any(
            int(event.get("target_id", 0)) == entity_id
            and int(event.get("attacker_id", 0)) in self.friendly_ids
            for event in self.events
        ):
            self.encounter_target_ids.add(entity_id)
        self._mark_target_defeated(monster)
        return changed or profile_changed

    def _begin_team_counter_reset(
        self, server_time: int, timestamp: int
    ) -> None:
        previous_update = self.team_server_update_100ns
        first_damage_100ns = (
            int(
                self.first_damage_time * 10_000_000
                + 116_444_736_000_000_000
            )
            if self.first_damage_time
            else 0
        )
        current_started_after_previous_snapshot = bool(
            first_damage_100ns
            and previous_update
            and first_damage_100ns > previous_update
        )
        has_current_encounter = bool(
            self.first_damage_time
            and (
                self.events
                or any(
                    state.accepted_damage > 0
                    for state in self.team_damage_states.values()
                )
            )
        )
        if has_current_encounter and not current_started_after_previous_snapshot:
            self.reset(
                keep_identity=True,
                keep_monsters=True,
                preserve_active_target=True,
                archive_reason="team_counter_reset",
            )
        for state in self.team_damage_states.values():
            state.last_absolute = 0
            state.baseline_absolute = 0
            state.has_snapshot = False
            state.authoritative_snapshot = False
            state.accepted_damage = 0
            state.snapshot_time_100ns = 0
            state.baseline_snapshot_time_100ns = (
                self.encounter_start_signal_100ns
            )
            state.server_time = 0
            state.first_time = 0.0
            state.last_time = 0.0
        self.team_server_time = max(0, int(server_time))
        self.team_server_update_100ns = max(0, int(timestamp))
        self.team_reset_pending = True
        self.team_zero_baseline_signal_100ns = (
            self.encounter_start_signal_100ns
        )

    def _arm_team_encounter(self, target_id: int, timestamp: int) -> bool:
        """Freeze pre-pull team counters on a confirmed encounter edge."""
        if (
            not target_id
            or not timestamp
            or self.first_damage_time
            or self.combat_end_time
            or self.encounter_start_signal_100ns
            or not self._is_priority_target(target_id)
        ):
            return False
        if self.combat_target_id is None:
            self.combat_target_id = target_id
        elif self.combat_target_id != target_id:
            return False
        had_pre_pull_snapshot = any(
            state.has_snapshot for state in self.team_damage_states.values()
        )
        self.encounter_start_signal_100ns = timestamp
        self.team_zero_baseline_signal_100ns = (
            0 if had_pre_pull_snapshot else timestamp
        )
        self._register_encounter_target(target_id)
        for state in self.team_damage_states.values():
            if not state.has_snapshot:
                continue
            preserve_zero_baseline = bool(
                state.last_absolute > 0
                and state.baseline_absolute == 0
                and state.baseline_snapshot_time_100ns > 0
                and state.baseline_snapshot_time_100ns < timestamp
            )
            if not preserve_zero_baseline:
                state.baseline_absolute = state.last_absolute
            state.baseline_snapshot_time_100ns = timestamp
            state.accepted_damage = 0
            state.first_time = 0.0
            state.last_time = 0.0
        self._resolve_combat_sides()
        return True

    def _apply_team_zero_baseline(self, state: TeamDamageState) -> None:
        signal = self.team_zero_baseline_signal_100ns
        if (
            signal
            and signal == self.encounter_start_signal_100ns
            and not state.baseline_snapshot_time_100ns
        ):
            state.baseline_absolute = 0
            state.baseline_snapshot_time_100ns = signal

    def ingest_team_stat(self, update: dict) -> bool:
        try:
            actor_id = int(update.get("actor_id", 0))
            absolute_damage = max(0, int(update.get("absolute_damage", 0)))
            timestamp = int(update.get("filetime_100ns", 0) or 0)
            server_time = max(0, int(update.get("server_time", 0) or 0))
            omitted_zero = bool(update.get("omitted_zero"))
            full_snapshot = bool(update.get("full_snapshot"))
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            not actor_id
            or not timestamp
            or actor_id in self.non_player_actor_ids
        ):
            return False
        self.latest_network_time_100ns = max(self.latest_network_time_100ns, timestamp)

        state = self.team_damage_states.setdefault(
            actor_id, TeamDamageState(actor_id)
        )
        if omitted_zero and state.has_snapshot and state.last_absolute > 0:
            # A full Common row can omit an unchanged positive counter during
            # teardown. It must not look like an explicit counter reset.
            return False
        if server_time:
            # Common field 10 changes independently for each member during a
            # single pull. It is useful for rejecting an older snapshot of the
            # same actor, but it is not a team-wide encounter identifier.
            if state.server_time and server_time < state.server_time:
                return False
            self.team_server_time = max(self.team_server_time, server_time)
        self._apply_team_zero_baseline(state)
        if (
            state.snapshot_time_100ns
            and timestamp < state.snapshot_time_100ns
            and (not server_time or server_time == state.server_time)
        ):
            return False
        if state.has_snapshot and absolute_damage < state.last_absolute:
            self._begin_team_counter_reset(
                server_time or self.team_server_time,
                timestamp,
            )
            state = self.team_damage_states.setdefault(
                actor_id, TeamDamageState(actor_id)
            )
            self._apply_team_zero_baseline(state)

        if self._is_dummy_encounter():
            # Dummy encounters are deliberately local-only. Native per-hit
            # damage is complete here, while the cumulative team value can
            # arrive late and temporarily omit a hit that was already seen.
            # A pre-pull Common snapshot must not remain authoritative and
            # shadow those exact local hits after the dummy is selected.
            state.authoritative_snapshot = False
            state.last_absolute = absolute_damage
            state.baseline_absolute = absolute_damage
            state.has_snapshot = True
            state.snapshot_time_100ns = timestamp
            state.baseline_snapshot_time_100ns = timestamp
            state.server_time = server_time or state.server_time
            state.accepted_damage = 0
            self.team_server_update_100ns = max(
                self.team_server_update_100ns, timestamp
            )
            self._recompute()
            return False

        state.authoritative_snapshot = True
        event_time = self._event_seconds(update)
        if (
            self.last_damage_time
            and event_time - self.last_damage_time
            > (self.encounter_gap if self.boss_only else self.idle_gap)
            and self._damage_gap_starts_new_encounter()
        ):
            self.reset(
                keep_identity=True,
                keep_monsters=True,
                archive_reason="new_encounter",
            )
            state = self.team_damage_states.setdefault(
                actor_id, TeamDamageState(actor_id)
            )

        first_full_snapshot = bool(full_snapshot and not state.has_snapshot)
        state.last_absolute = absolute_damage
        state.has_snapshot = True
        state.snapshot_time_100ns = max(state.snapshot_time_100ns, timestamp)
        state.server_time = server_time or state.server_time
        self.team_server_update_100ns = max(
            self.team_server_update_100ns, timestamp
        )
        finished_time = self.combat_end_time
        if not finished_time and self._target_hp_depleted():
            finished_time = self.last_damage_time
        if self.combat_target_id is None:
            target = self._select_priority_monster(active_at_100ns=timestamp)
            if target is not None:
                self.combat_target_id = target.entity_id
                self._register_encounter_target(target.entity_id)
                self._resolve_combat_sides()

        damage_target_ids = self._encounter_damage_target_ids()
        start_signal = self.encounter_start_signal_100ns
        if first_full_snapshot:
            # A full Common row is the server's complete current counter, not
            # a delta. Its first positive value can arrive milliseconds before
            # this client binds the Boss entity. Starting that member at the
            # positive value drops real opening damage and makes observers
            # disagree according to packet timing.
            state.baseline_absolute = 0
            state.baseline_snapshot_time_100ns = start_signal or timestamp
        if not damage_target_ids or (
            not self.first_damage_time and not start_signal
        ):
            preserve_zero_baseline = bool(
                first_full_snapshot
                or (
                    absolute_damage > 0
                    and state.baseline_absolute == 0
                    and state.baseline_snapshot_time_100ns > 0
                    and state.baseline_snapshot_time_100ns < timestamp
                )
            )
            if not preserve_zero_baseline:
                state.baseline_absolute = absolute_damage
                state.baseline_snapshot_time_100ns = timestamp
            state.accepted_damage = 0
            return False
        if (
            start_signal
            and state.baseline_snapshot_time_100ns < start_signal
        ):
            # Capture began after this pull started, or this member was absent
            # from every pre-pull full snapshot. Their first value is a local
            # baseline; claiming older damage would not be exact.
            state.baseline_absolute = absolute_damage
            state.baseline_snapshot_time_100ns = timestamp
            state.accepted_damage = 0
            return False
        if (
            actor_id not in self.encounter_member_ids
            and len(self.encounter_member_ids) >= MAX_PARTY_MEMBERS
        ):
            return False

        previous_accepted = state.accepted_damage
        previous_visible_damage = (
            self.stats[actor_id].damage if actor_id in self.stats else 0
        )
        state.accepted_damage = max(
            0, absolute_damage - state.baseline_absolute
        )
        self.team_reset_pending = False
        if state.accepted_damage <= 0:
            self._recompute()
            current_visible_damage = (
                self.stats[actor_id].damage if actor_id in self.stats else 0
            )
            changed = bool(
                previous_accepted != state.accepted_damage
                or previous_visible_damage != current_visible_damage
            )
            if changed and finished_time:
                self._queue_current_record_refresh()
            return changed
        if not state.first_time:
            start_time = event_time
            if start_signal:
                start_time = self._event_seconds(
                    {"filetime_100ns": start_signal}
                )
            state.first_time = min(
                start_time,
                finished_time or start_time,
            )
        if state.accepted_damage != previous_accepted:
            # Common snapshots continue at a fixed cadence after combat. Only
            # a changed authoritative counter marks actual new damage; moving
            # this timestamp for an identical row makes elapsed time grow and
            # DPS fall forever after the fight has stopped.
            state.last_time = finished_time or event_time
        self._update_encounter_roster(actor_id)
        if actor_id not in self.friend_order:
            self.friend_order.append(actor_id)
        self._recompute()
        current_visible_damage = (
            self.stats[actor_id].damage if actor_id in self.stats else 0
        )
        changed = bool(
            state.accepted_damage != previous_accepted
            or current_visible_damage != previous_visible_damage
        )
        if finished_time and changed:
            # The final Common/Dirty snapshot often arrives just after the
            # death packet. Update the archived values without reopening the
            # encounter or extending its duration.
            self._queue_current_record_refresh()
        return changed

    def current_monster(self) -> MonsterStats | None:
        if self.combat_target_id is not None:
            monster = self.monsters.get(self.combat_target_id)
            if monster is not None and self._is_priority_target(monster.entity_id):
                return monster
        return self._select_priority_monster()

    def display_name(self, actor_id: int) -> str:
        automatic_name = self.entity_names.get(actor_id, "").strip()
        if automatic_name:
            return automatic_name
        if actor_id == self.self_id:
            return self.local_player_name
        return ""

    def display_target_name(self, entity_id: int) -> str:
        if not entity_id:
            return "未分配目标"
        monster = self.monsters.get(entity_id)
        name = (
            (monster.name if monster is not None else "")
            or self.entity_names.get(entity_id, "")
        ).strip()
        if name and not boss_name_is_placeholder(name):
            return name
        if entity_id == self.combat_target_id:
            return name or "Boss"
        add_order = [
            target_id
            for target_id in self.encounter_target_order
            if target_id in self.encounter_add_target_ids
        ]
        try:
            index = add_order.index(entity_id) + 1
        except ValueError:
            index = len(add_order) + 1
        return f"小怪 {index}"

    def _target_kind(self, entity_id: int) -> str:
        if (
            entity_id == self.combat_target_id
            or entity_id in self.linked_boss_target_ids
        ):
            return "Boss"
        monster = self.monsters.get(entity_id)
        if (
            monster is not None
            and self._monster_rank(monster) > 0
            and entity_id not in self.encounter_add_target_ids
        ):
            return "Boss"
        return "小怪"

    def actor_target_rows(self, actor_id: int) -> list[dict]:
        actor = self.stats.get(actor_id)
        if actor is None or actor.damage <= 0:
            return []
        target_damage = {
            int(target_id): int(damage)
            for target_id, damage in actor.target_damage.items()
            if int(target_id) and int(damage) > 0
        }

        grouped: dict[tuple[str, str], dict] = {}
        for target_id, damage in target_damage.items():
            name = self.display_target_name(target_id)
            kind = self._target_kind(target_id)
            key = (kind, name)
            row = grouped.setdefault(
                key,
                {
                    "entity_id": target_id,
                    "entity_ids": [],
                    "name": name,
                    "kind": kind,
                    "damage": 0,
                },
            )
            row["entity_ids"].append(target_id)
            row["damage"] += damage
            if target_id == self.combat_target_id:
                row["entity_id"] = target_id
        rows = list(grouped.values())
        for row in rows:
            row["share"] = int(row["damage"]) / actor.damage
        tracked_damage = sum(int(row["damage"]) for row in rows)
        unassigned_damage = max(0, actor.damage - tracked_damage)
        if unassigned_damage:
            rows.append(
                {
                    "entity_id": 0,
                    "name": "未分配目标",
                    "kind": "汇总",
                    "damage": unassigned_damage,
                    "share": unassigned_damage / actor.damage,
                }
            )
        rows.sort(
            key=lambda row: (
                row["kind"] == "Boss",
                int(row["damage"]),
            ),
            reverse=True,
        )
        return rows

    def display_skill_name(self, actor_id: int, skill_id: int) -> str:
        automatic_name = self.skill_names.get(skill_id, "").strip()
        if automatic_name:
            return automatic_name
        runtime_name = self.runtime_skill_names.get(skill_id, "").strip()
        if runtime_name:
            return runtime_name
        actor = self.stats.get(actor_id)
        if actor is None:
            return "未归类伤害" if not skill_id else "未知技能"
        if not skill_id:
            return "未归类伤害"
        return "未知技能"

    def actor_profession_id(self, actor_id: int) -> int | None:
        explicit = self.entity_professions.get(actor_id)
        if explicit:
            return explicit
        actor = self.stats.get(actor_id)
        if actor is None:
            return None
        scores: dict[int, int] = {}
        for skill in actor.skills.values():
            class_ids = self.skill_professions.get(skill.skill_id, ())
            if not class_ids and 86_010_000 <= skill.skill_id < 86_080_000:
                index = (skill.skill_id - 86_000_000) // 10_000
                if 1 <= index <= 7:
                    class_ids = (1_200_000 + index,)
            for class_id in class_ids:
                scores[class_id] = scores.get(class_id, 0) + max(1, skill.damage)
        return max(scores, key=scores.get) if scores else None

    @staticmethod
    def normalize_damage_skill_id(value) -> int:
        try:
            skill_id = int(value)
        except (TypeError, ValueError):
            return 0
        # Damage sources append a decimal effect index to their eight-digit
        # root skill ID (for example 860210200 -> 86021020).
        if 100_000_000 <= skill_id <= 9_999_999_999 and skill_id % 10 == 0:
            return skill_id // 10
        return skill_id

    def ingest_skill_name(self, update: dict) -> bool:
        try:
            skill_id = int(update.get("skill_id", 0))
        except (TypeError, ValueError):
            return False
        name = str(update.get("name", "")).strip().replace("\x00", "")
        if not skill_id or not name or len(name) > 64:
            return False
        if any(ord(char) < 0x20 for char in name):
            return False
        # The client catalog is authoritative.  Runtime discovery only fills a
        # missing exact ID: nearby IDs can be different skills (for example
        # 89004600 is 挥砍 while 89004604 is 飞弹).
        if skill_id in self.skill_names:
            return False
        if self.runtime_skill_names.get(skill_id) == name:
            return False
        self.runtime_skill_names[skill_id] = name
        return True

    def ingest_name(self, update: dict) -> bool:
        try:
            entity_id = int(update.get("entity_id", 0))
        except (TypeError, ValueError):
            return False
        name = str(update.get("name", "")).strip().replace("\x00", "")
        if not entity_id or not name or len(name) > 64:
            return False
        if any(ord(char) < 0x20 for char in name):
            return False
        if self.entity_names.get(entity_id) == name:
            return False
        self.entity_names[entity_id] = name
        if entity_id == self.self_id:
            self.local_player_name = name
        return True

    def duration(self, now: float | None = None) -> float:
        if not self.first_damage_time:
            return 0.0
        now = now if now is not None else time.time()
        if self.combat_end_time:
            end = self.combat_end_time
        elif self._result_frozen_by_target_death():
            # Freeze the visible result as soon as the Boss bar reaches zero.
            # Multi-stage encounters remain live while a known auxiliary phase
            # is active, but their final death must not keep lowering DPS until
            # the longer archive timeout expires.
            end = self.last_damage_time
        elif now - self.last_damage_time < self._encounter_idle_timeout():
            end = now
        else:
            end = self.last_damage_time
        return max(1.0, end - self.first_damage_time)

    def combat_in_progress(self, now: float | None = None) -> bool:
        return self.active(now)

    def active(self, now: float | None = None) -> bool:
        if not self.last_damage_time:
            return False
        if self.combat_end_time or self._result_frozen_by_target_death():
            return False
        now = now if now is not None else time.time()
        return now - self.last_damage_time < self._encounter_idle_timeout()


def blend_color(left: str, right: str, amount: float) -> str:
    amount = min(1.0, max(0.0, amount))
    lhs = tuple(int(left[index : index + 2], 16) for index in (1, 3, 5))
    rhs = tuple(int(right[index : index + 2], 16) for index in (1, 3, 5))
    mixed = tuple(round(a + (b - a) * amount) for a, b in zip(lhs, rhs))
    return "#" + "".join(f"{value:02x}" for value in mixed)


class IconFactory:
    """Load extracted PNGs when present, otherwise draw deterministic badges."""

    PROFESSION_GLYPHS = {
        1_200_001: "日",
        1_200_002: "心",
        1_200_003: "愚",
        1_200_004: "审",
        1_200_005: "门",
        1_200_006: "暮",
        1_200_007: "隐",
    }

    def __init__(self, root: tk.Misc):
        self.root = root
        self.cache: dict[tuple[object, ...], ImageTk.PhotoImage] = {}

    @staticmethod
    def _font(size: int, *, bold: bool = False):
        windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates = (
            windows / ("msyhbd.ttc" if bold else "msyh.ttc"),
            windows / ("segoeuib.ttf" if bold else "segoeui.ttf"),
        )
        for path in candidates:
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _rounded(image: Image.Image, radius: int) -> Image.Image:
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, image.width - 1, image.height - 1), radius=radius, fill=255
        )
        # Keep the original game's transparent pixels.  Replacing the alpha
        # channel would turn transparent texture padding into a black tile.
        image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
        return image

    def _from_file(self, path: Path, size: int) -> Image.Image | None:
        if not path.is_file():
            return None
        try:
            image = Image.open(path).convert("RGBA")
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.alpha_composite(
                image, ((size - image.width) // 2, (size - image.height) // 2)
            )
            return self._rounded(canvas, max(4, size // 5))
        except (OSError, ValueError):
            return None

    def _draw_badge(self, text: str, color: str, size: int, seed: int) -> Image.Image:
        image = Image.new("RGBA", (size, size), color)
        draw = ImageDraw.Draw(image, "RGBA")
        for y in range(size):
            alpha = int(38 + 70 * y / max(1, size - 1))
            draw.line((0, y, size, y), fill=(3, 8, 14, alpha))
        offset = seed % max(5, size // 3)
        draw.polygon(
            ((-size // 3 + offset, size), (size // 2 + offset, 0), (size, 0), (size // 5, size)),
            fill=(255, 255, 255, 20),
        )
        draw.rounded_rectangle(
            (1, 1, size - 2, size - 2),
            radius=max(4, size // 5),
            outline=(255, 255, 255, 75),
            width=max(1, size // 24),
        )
        text = (text or "?")[:1]
        font = self._font(max(12, int(size * 0.48)), bold=True)
        box = draw.textbbox((0, 0), text, font=font)
        x = (size - (box[2] - box[0])) / 2 - box[0]
        y = (size - (box[3] - box[1])) / 2 - box[1] - 1
        draw.text((x + 1, y + 2), text, font=font, fill=(0, 0, 0, 110))
        draw.text((x, y), text, font=font, fill=(248, 250, 252, 245))
        return self._rounded(image, max(4, size // 5))

    def app_logo(self, size: int = 18) -> ImageTk.PhotoImage:
        key = ("app", 0, size, ACCENT)
        if key not in self.cache:
            image = self._from_file(APP_LOGO_PATH, size) or self._draw_badge(
                "D", ACCENT, size, 0
            )
            self.cache[key] = ImageTk.PhotoImage(image, master=self.root)
        return self.cache[key]

    @staticmethod
    def _draw_toolbar_icon(name: str, size: int, color: str) -> Image.Image:
        render_scale = 4
        extent = max(1, int(size)) * render_scale
        image = Image.new("RGBA", (extent, extent), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        unit = extent / 24.0
        point = lambda value: round(float(value) * unit)
        width = max(render_scale, point(1.8))
        stroke = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5)) + (
            255,
        )

        if name == "share":
            draw.rounded_rectangle(
                (point(4), point(9), point(20), point(21)),
                radius=point(2),
                outline=stroke,
                width=width,
            )
            draw.line(
                (point(12), point(15), point(12), point(3)),
                fill=stroke,
                width=width,
            )
            draw.line(
                (point(7), point(8), point(12), point(3), point(17), point(8)),
                fill=stroke,
                width=width,
                joint="curve",
            )
        elif name == "reset":
            draw.arc(
                (point(4), point(4), point(20), point(20)),
                start=35,
                end=330,
                fill=stroke,
                width=width,
            )
            draw.line(
                (point(4), point(9), point(4), point(4), point(9), point(4)),
                fill=stroke,
                width=width,
                joint="curve",
            )
        elif name in {"eye", "eye_off"}:
            draw.ellipse(
                (point(2), point(6), point(22), point(18)),
                outline=stroke,
                width=width,
            )
            draw.ellipse(
                (point(9), point(9), point(15), point(15)),
                outline=stroke,
                width=width,
            )
            if name == "eye_off":
                draw.line(
                    (point(3), point(3), point(21), point(21)),
                    fill=stroke,
                    width=width,
                )
        elif name in {"lock", "unlock"}:
            draw.rounded_rectangle(
                (point(5), point(10), point(19), point(21)),
                radius=point(2),
                outline=stroke,
                width=width,
            )
            if name == "lock":
                draw.arc(
                    (point(7), point(3), point(17), point(13)),
                    start=180,
                    end=360,
                    fill=stroke,
                    width=width,
                )
                draw.line(
                    (point(7), point(8), point(7), point(11)),
                    fill=stroke,
                    width=width,
                )
                draw.line(
                    (point(17), point(8), point(17), point(11)),
                    fill=stroke,
                    width=width,
                )
            else:
                draw.arc(
                    (point(8), point(3), point(18), point(13)),
                    start=180,
                    end=325,
                    fill=stroke,
                    width=width,
                )
                draw.line(
                    (point(8), point(8), point(8), point(11)),
                    fill=stroke,
                    width=width,
                )
        elif name == "menu":
            for y in (6, 12, 18):
                draw.line(
                    (point(4), point(y), point(20), point(y)),
                    fill=stroke,
                    width=width,
                )
        elif name == "user":
            draw.ellipse(
                (point(8), point(3), point(16), point(11)),
                outline=stroke,
                width=width,
            )
            draw.arc(
                (point(4), point(10), point(20), point(23)),
                start=180,
                end=360,
                fill=stroke,
                width=width,
            )
        elif name == "settings":
            draw.ellipse(
                (point(8), point(8), point(16), point(16)),
                outline=stroke,
                width=width,
            )
            for line in (
                (12, 2, 12, 6),
                (12, 18, 12, 22),
                (2, 12, 6, 12),
                (18, 12, 22, 12),
                (5, 5, 8, 8),
                (16, 16, 19, 19),
                (19, 5, 16, 8),
                (8, 16, 5, 19),
            ):
                draw.line(tuple(point(value) for value in line), fill=stroke, width=width)
        elif name == "minimize":
            draw.line(
                (point(5), point(12), point(19), point(12)),
                fill=stroke,
                width=width,
            )
        elif name in {"compact", "expand"}:
            if name == "compact":
                segments = (
                    (4, 4, 10, 10),
                    (10, 5, 10, 10, 5, 10),
                    (20, 4, 14, 10),
                    (14, 5, 14, 10, 19, 10),
                    (4, 20, 10, 14),
                    (5, 14, 10, 14, 10, 19),
                    (20, 20, 14, 14),
                    (14, 19, 14, 14, 19, 14),
                )
            else:
                segments = (
                    (10, 10, 4, 4),
                    (4, 9, 4, 4, 9, 4),
                    (14, 10, 20, 4),
                    (15, 4, 20, 4, 20, 9),
                    (10, 14, 4, 20),
                    (4, 15, 4, 20, 9, 20),
                    (14, 14, 20, 20),
                    (15, 20, 20, 20, 20, 15),
                )
            for segment in segments:
                draw.line(
                    tuple(point(value) for value in segment),
                    fill=stroke,
                    width=width,
                    joint="curve",
                )
        else:
            draw.ellipse(
                (point(5), point(5), point(19), point(19)),
                outline=stroke,
                width=width,
            )
        return image.resize((size, size), Image.Resampling.LANCZOS)

    def toolbar(
        self, name: str, size: int = 20, color: str = TEXT
    ) -> ImageTk.PhotoImage:
        key = ("toolbar", str(name), int(size), str(color))
        if key not in self.cache:
            self.cache[key] = ImageTk.PhotoImage(
                self._draw_toolbar_icon(str(name), int(size), str(color)),
                master=self.root,
            )
        return self.cache[key]

    def profession(self, class_id: int | None, size: int = 34) -> ImageTk.PhotoImage:
        class_id = int(class_id or 0)
        color = PROFESSION_COLORS.get(class_id, SUBTLE)
        key = ("profession", class_id, size, color)
        if key not in self.cache:
            path = ASSET_DIR / "professions" / f"{class_id}.png"
            image = self._from_file(path, size) or self._draw_badge(
                self.PROFESSION_GLYPHS.get(class_id, "?"), color, size, class_id
            )
            self.cache[key] = ImageTk.PhotoImage(image, master=self.root)
        return self.cache[key]

    def skill(
        self,
        skill_id: int,
        name: str,
        class_id: int | None,
        size: int = 36,
    ) -> ImageTk.PhotoImage:
        color = PROFESSION_COLORS.get(int(class_id or 0), "#526273")
        key = ("skill", int(skill_id), size, color)
        if key not in self.cache:
            path = ASSET_DIR / "skills" / f"{int(skill_id)}.png"
            image = self._from_file(path, size) or self._draw_badge(
                (name or "?")[:1], color, size, int(skill_id)
            )
            self.cache[key] = ImageTk.PhotoImage(image, master=self.root)
        return self.cache[key]


class ModernSlider(tk.Canvas):
    """Small canvas slider matching the meter's dark visual language."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        variable: tk.Variable,
        from_: float,
        to: float,
        resolution: float = 1.0,
        command=None,
        background: str = PANEL,
        length: int = 420,
    ) -> None:
        super().__init__(
            parent,
            width=length,
            height=36,
            bg=background,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=1,
        )
        self.variable = variable
        self.minimum = float(from_)
        self.maximum = float(to)
        self.resolution = max(0.000001, float(resolution))
        self.command = command
        self.hovered = False
        self.dragging = False
        self._trace_id = self.variable.trace_add("write", self._variable_changed)
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._pointer_down)
        self.bind("<B1-Motion>", self._pointer_move)
        self.bind("<ButtonRelease-1>", self._pointer_up)
        self.bind("<MouseWheel>", self._mouse_wheel)
        self.bind("<Button-4>", lambda _event: self._step(1))
        self.bind("<Button-5>", lambda _event: self._step(-1))
        self.bind("<Left>", lambda _event: self._step(-1))
        self.bind("<Down>", lambda _event: self._step(-1))
        self.bind("<Right>", lambda _event: self._step(1))
        self.bind("<Up>", lambda _event: self._step(1))
        self.bind("<Home>", lambda _event: self._set_value(self.minimum))
        self.bind("<End>", lambda _event: self._set_value(self.maximum))
        self.after_idle(self._redraw)

    def destroy(self) -> None:
        try:
            self.variable.trace_remove("write", self._trace_id)
        except (tk.TclError, AttributeError):
            pass
        super().destroy()

    def _current_value(self) -> float:
        try:
            value = float(self.variable.get())
        except (TypeError, ValueError, tk.TclError):
            value = self.minimum
        return min(self.maximum, max(self.minimum, value))

    def _normalized_value(self, value: object) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            numeric = self.minimum
        numeric = min(self.maximum, max(self.minimum, numeric))
        steps = round((numeric - self.minimum) / self.resolution)
        return min(
            self.maximum,
            max(self.minimum, self.minimum + steps * self.resolution),
        )

    def _set_value(self, value: object) -> str:
        normalized = self._normalized_value(value)
        if abs(normalized - self._current_value()) < self.resolution / 2:
            return "break"
        stored_value: float | int = normalized
        if self.resolution.is_integer() and self.minimum.is_integer():
            stored_value = int(round(normalized))
        self.variable.set(stored_value)
        if self.command is not None:
            self.command(stored_value)
        return "break"

    def _value_from_x(self, x: int) -> float:
        start = 13
        end = max(start + 1, self.winfo_width() - 13)
        ratio = min(1.0, max(0.0, (int(x) - start) / (end - start)))
        return self.minimum + ratio * (self.maximum - self.minimum)

    def _pointer_down(self, event) -> str:
        self.focus_set()
        self.dragging = True
        return self._set_value(self._value_from_x(event.x))

    def _pointer_move(self, event) -> str:
        if self.dragging:
            return self._set_value(self._value_from_x(event.x))
        return "break"

    def _pointer_up(self, event) -> str:
        self.dragging = False
        return self._set_value(self._value_from_x(event.x))

    def _mouse_wheel(self, event) -> str:
        return self._step(1 if event.delta > 0 else -1)

    def _step(self, direction: int) -> str:
        return self._set_value(
            self._current_value() + (1 if direction > 0 else -1) * self.resolution
        )

    def _variable_changed(self, *_args) -> None:
        self.after_idle(self._redraw)

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self._redraw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self.dragging = False
        self._redraw()

    def _redraw(self, _event=None) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        width = max(28, self.winfo_width())
        height = max(24, self.winfo_height())
        start = 13
        end = max(start + 1, width - 13)
        center_y = height // 2
        span = max(0.000001, self.maximum - self.minimum)
        ratio = (self._current_value() - self.minimum) / span
        thumb_x = start + int((end - start) * ratio)
        self.create_line(
            start,
            center_y,
            end,
            center_y,
            fill="#30353b",
            width=8,
            capstyle=tk.ROUND,
        )
        if thumb_x > start:
            self.create_line(
                start,
                center_y,
                thumb_x,
                center_y,
                fill=ACCENT,
                width=8,
                capstyle=tk.ROUND,
            )
        radius = 9 if self.hovered or self.dragging else 8
        self.create_oval(
            thumb_x - radius - 2,
            center_y - radius - 2,
            thumb_x + radius + 2,
            center_y + radius + 2,
            fill=blend_color(PANEL, ACCENT, 0.18),
            outline="",
        )
        self.create_oval(
            thumb_x - radius,
            center_y - radius,
            thumb_x + radius,
            center_y + radius,
            fill=ACCENT,
            outline="#d8fff2" if self.hovered or self.dragging else ACCENT,
            width=2,
        )


class LicenseHeartbeatWorker(threading.Thread):
    def __init__(
        self,
        licensing: LicensingService,
        messages: queue.Queue,
        stop_event: threading.Event,
    ):
        super().__init__(name="DpsLicenseHeartbeat", daemon=True)
        self.licensing = licensing
        self.messages = messages
        self.stop_event = stop_event
        self.state_lock = threading.Lock()
        self.using = False
        self.character_name = ""
        self.game_pid = 0

    def update_state(
        self, *, using: bool, character_name: str = "", game_pid: int = 0
    ) -> None:
        with self.state_lock:
            self.using = bool(using)
            self.character_name = str(character_name).strip()[:48]
            self.game_pid = max(0, int(game_pid or 0))

    def _state(self) -> tuple[bool, str, int]:
        with self.state_lock:
            return self.using, self.character_name, self.game_pid

    def run(self) -> None:
        last_success = time.monotonic()
        interval = 30
        try:
            while not self.stop_event.is_set():
                using, character_name, game_pid = self._state()
                try:
                    result = self.licensing.heartbeat(
                        using=using,
                        character_name=character_name,
                        game_pid=game_pid,
                    )
                except LicensingConnectionError:
                    if (
                        time.monotonic() - last_success
                        >= LICENSE_HEARTBEAT_FAILURE_GRACE_SECONDS
                    ):
                        self.messages.put(
                            (
                                "license_required",
                                "授权服务器连接中断，请重新输入卡号。",
                            )
                        )
                        return
                    self.stop_event.wait(10.0)
                    continue
                if not result.authorized:
                    self.messages.put(
                        (
                            "license_required",
                            result.message or "登录状态已失效，请重新输入卡号。",
                        )
                    )
                    return
                last_success = time.monotonic()
                interval = result.heartbeat_interval
                self.stop_event.wait(interval)
        finally:
            self.licensing.sign_out()


class BossNameResolver(threading.Thread):
    def __init__(
        self,
        messages: queue.Queue,
        stop_event: threading.Event,
        monster_catalog: dict[str, dict],
    ):
        super().__init__(name="C7BossNames", daemon=True)
        self.messages = messages
        self.stop_event = stop_event
        self.monster_catalog = monster_catalog
        self.requests: queue.Queue = queue.Queue()
        self.requested: set[tuple[int, int, int]] = set()
        self.cached_names = load_json_object(MONSTER_NAME_CACHE_PATH)

    def request(
        self,
        pid: int,
        entity_id: int,
        template_id: int,
        localization_id: int,
        filetime_100ns: int,
    ) -> None:
        if not pid or not entity_id or not template_id or not localization_id:
            return
        key = (pid, entity_id, template_id)
        if key in self.requested:
            return
        self.requested.add(key)
        self.requests.put(
            (pid, entity_id, template_id, localization_id, filetime_100ns)
        )

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                first = self.requests.get(timeout=0.2)
            except queue.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                try:
                    candidate = self.requests.get_nowait()
                except queue.Empty:
                    self.stop_event.wait(0.02)
                    continue
                if candidate[0] == first[0]:
                    batch.append(candidate)
                else:
                    self.requests.put(candidate)
                    break
            localization_ids = {item[3] for item in batch}
            try:
                names = resolve_localization_names(first[0], localization_ids)
            except Exception as exc:
                self.messages.put(("diagnostic", f"Boss 名称后台解析失败：{exc}"))
                continue
            cache_changed = False
            for _pid, entity_id, template_id, localization_id, timestamp in batch:
                name = str(names.get(localization_id, "")).strip()
                if not name:
                    continue
                self.monster_catalog.setdefault(str(template_id), {})["name"] = name
                if self.cached_names.get(str(template_id)) != name:
                    self.cached_names[str(template_id)] = name
                    cache_changed = True
                self.messages.put(
                    (
                        "profile",
                        {
                            "filetime_100ns": timestamp,
                            "entity_id": entity_id,
                            "template_id": template_id,
                            "name": name,
                            "entity_type": "Boss",
                            "boss_type": 3,
                            "boss_rank": 3,
                        },
                    )
                )
            if cache_changed:
                write_json_object(MONSTER_NAME_CACHE_PATH, self.cached_names)


class HookWorker(threading.Thread):
    def __init__(self, messages: queue.Queue, stop_event: threading.Event):
        super().__init__(name="C7NetworkPackets", daemon=False)
        self.messages = messages
        self.stop_event = stop_event
        self.diagnostic_lock = threading.Lock()
        self.diagnostics: dict[str, object] = {
            "stage": "created",
            "process_found": False,
            "network_hook_installed": False,
            "native_damage_hook_installed": False,
            "team_stats_hook_installed": False,
            "team_stats_requests": 0,
            "team_stats_last_result": 0,
            "team_stats_last_request_filetime": 0,
            "capture_process_pid": 0,
            "capture_process_priority": "default",
            "capture_process_priority_applied": False,
            "capture_batches": 0,
            "network_sequence_gaps": 0,
            "dungeon_id": 0,
            "dungeon_stage_id": 0,
            "dungeon_stage_phase": 0,
            "dungeon_context_filetime": 0,
            "reconnect_dungeon_candidates": [],
            "damage_source": "none",
            "game_pid": 0,
            "network_records": 0,
            "native_damage_records": 0,
            "native_boss_records": 0,
            "boss_catalog_size": 0,
            "boss_catalog_promotions": 0,
            "parsed_damage_events": 0,
            "filtered_non_boss_damage_events": 0,
            "monster_updates": 0,
            "last_network_record_at": 0.0,
            "last_native_damage_at": 0.0,
            "last_parsed_damage_at": 0.0,
            "messages": [],
        }
        self.monster_catalog = load_monster_catalog()
        self.diagnostics["boss_catalog_size"] = sum(
            int(metadata.get("boss_type", 0) or 0) == 3
            for metadata in self.monster_catalog.values()
            if isinstance(metadata, dict)
        )
        self._diagnostic_entity_templates: dict[int, dict[str, object]] = {}
        self._diagnostic_damage_targets: dict[int, dict[str, object]] = {}
        self.team_profile_cache = load_json_object(TEAM_PROFILE_CACHE_PATH)
        self.self_identity_cache = load_json_object(SELF_IDENTITY_CACHE_PATH)
        self.active_boss_cache = load_json_object(ACTIVE_BOSS_CACHE_PATH)
        self.boss_name_resolver = BossNameResolver(
            messages,
            stop_event,
            self.monster_catalog,
        )

    def _set_active_boss_cache(self, value: dict) -> None:
        if value == self.active_boss_cache:
            return
        self.active_boss_cache = dict(value)
        write_json_object(ACTIVE_BOSS_CACHE_PATH, self.active_boss_cache)

    def _restore_same_process_boss(
        self, parser: NetworkPacketParser, game_pid: int
    ) -> bool:
        try:
            cached_pid = int(self.active_boss_cache.get("game_pid", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            cached_pid = 0
        if cached_pid != int(game_pid or 0):
            if self.active_boss_cache:
                self._set_active_boss_cache({})
            return False
        updates = parser.restore_active_boss_state(self.active_boss_cache)
        if not updates:
            self._set_active_boss_cache({})
            return False
        for kind, update in updates:
            self.emit(kind, update)
        return True

    def _sync_active_boss_cache(
        self, parser: NetworkPacketParser, game_pid: int
    ) -> None:
        state = parser.current_active_boss_state()
        if state is None:
            try:
                cached_pid = int(self.active_boss_cache.get("game_pid", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                cached_pid = 0
            if cached_pid == int(game_pid or 0):
                self._set_active_boss_cache({})
            return
        self._set_active_boss_cache(
            {
                "game_pid": int(game_pid or 0),
                **state,
            }
        )

    def _update_diagnostics(self, **values: object) -> None:
        with self.diagnostic_lock:
            self.diagnostics.update(values)

    def _add_diagnostic_counts(self, **values: int) -> None:
        with self.diagnostic_lock:
            for key, value in values.items():
                self.diagnostics[key] = int(self.diagnostics.get(key, 0) or 0) + int(
                    value
                )

    def diagnostic_snapshot(self) -> dict[str, object]:
        with self.diagnostic_lock:
            snapshot = dict(self.diagnostics)
            snapshot["messages"] = list(self.diagnostics.get("messages", []))
            snapshot["recent_damage_targets"] = [
                dict(value)
                for value in list(self._diagnostic_damage_targets.values())[-16:]
            ]
            snapshot["recent_boss_templates"] = [
                dict(value)
                for value in self._diagnostic_entity_templates.values()
                if bool(value.get("catalog_match"))
                or int(value.get("runtime_boss_type", 0) or 0) == 3
            ][-24:]
        return snapshot

    def _record_native_boss_observation(self, record: dict) -> None:
        try:
            entity_id = int(record.get("entity_id", 0) or 0)
            template_id = int(record.get("template_id", 0) or 0)
            runtime_boss_type = int(record.get("boss_type", -1))
            filetime_100ns = int(record.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if not entity_id:
            return
        metadata = self.monster_catalog.get(str(template_id), {})
        try:
            catalog_match = bool(
                isinstance(metadata, dict)
                and int(metadata.get("boss_type", 0) or 0) == 3
            )
        except (TypeError, ValueError, OverflowError):
            catalog_match = False
        name = ""
        if isinstance(metadata, dict):
            name = str(metadata.get("name", "")).strip()[:64]
        if not name:
            name = str(record.get("name", "")).strip()[:64]
        observation: dict[str, object] = {
            "entity_id": str(entity_id),
            "template_id": template_id,
            "runtime_boss_type": runtime_boss_type,
            "catalog_match": catalog_match,
            "name": name,
            "filetime_100ns": filetime_100ns,
        }
        with self.diagnostic_lock:
            self._diagnostic_entity_templates.pop(entity_id, None)
            self._diagnostic_entity_templates[entity_id] = observation
            while len(self._diagnostic_entity_templates) > 4096:
                oldest = next(iter(self._diagnostic_entity_templates))
                self._diagnostic_entity_templates.pop(oldest, None)
            target = self._diagnostic_damage_targets.get(entity_id)
            if target is not None:
                target.update(
                    {
                        "template_id": template_id,
                        "runtime_boss_type": runtime_boss_type,
                        "catalog_match": catalog_match,
                        "name": name,
                    }
                )

    def _record_native_damage_observation(self, record: dict) -> None:
        try:
            target_id = int(record.get("target_id", 0) or 0)
            damage = max(0, int(record.get("damage", 0) or 0))
            filetime_100ns = int(record.get("filetime_100ns", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if not target_id or damage <= 0:
            return
        with self.diagnostic_lock:
            existing = self._diagnostic_damage_targets.pop(target_id, None) or {
                "target_id": str(target_id),
                "records": 0,
                "damage": 0,
                "template_id": 0,
                "runtime_boss_type": -1,
                "catalog_match": False,
                "name": "",
            }
            existing["records"] = int(existing.get("records", 0) or 0) + 1
            existing["damage"] = int(existing.get("damage", 0) or 0) + damage
            existing["filetime_100ns"] = filetime_100ns
            template = self._diagnostic_entity_templates.get(target_id)
            if template is not None:
                existing.update(
                    {
                        "template_id": int(template.get("template_id", 0) or 0),
                        "runtime_boss_type": int(
                            template.get("runtime_boss_type", -1)
                        ),
                        "catalog_match": bool(template.get("catalog_match")),
                        "name": str(template.get("name", ""))[:64],
                    }
                )
            self._diagnostic_damage_targets[target_id] = existing
            while len(self._diagnostic_damage_targets) > 32:
                oldest = next(iter(self._diagnostic_damage_targets))
                self._diagnostic_damage_targets.pop(oldest, None)

    def _remember_diagnostic_message(self, message: object) -> None:
        text = str(message or "").strip()
        if not text:
            return
        with self.diagnostic_lock:
            messages = list(self.diagnostics.get("messages", []))
            messages.append({"time": time.time(), "message": text[:500]})
            self.diagnostics["messages"] = messages[-12:]

    def emit(self, kind: str, payload=None) -> None:
        if kind == "event":
            self._add_diagnostic_counts(parsed_damage_events=1)
            self._update_diagnostics(last_parsed_damage_at=time.time())
        elif (
            kind == "profile"
            and isinstance(payload, dict)
            and payload.get("boss_source") == "monster_template_catalog"
        ):
            self._add_diagnostic_counts(boss_catalog_promotions=1)
        elif kind == "monster":
            self._add_diagnostic_counts(monster_updates=1)
        elif kind in {"diagnostic", "error", "fatal"}:
            self._remember_diagnostic_message(payload)
        self.messages.put((kind, payload))

    def _emit_parser_update(
        self, parser: NetworkPacketParser, kind: str, payload: object
    ) -> None:
        if (
            kind == "event"
            and isinstance(payload, dict)
            and not parser.should_forward_damage_event(payload)
        ):
            self._add_diagnostic_counts(filtered_non_boss_damage_events=1)
            return
        self.emit(kind, payload)

    def _enrich_boss_record(self, record: dict, pid: int) -> None:
        try:
            template_id = int(record.get("template_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            template_id = 0
        if not template_id:
            return
        metadata = self.monster_catalog.get(str(template_id), {})
        if not isinstance(metadata, dict):
            return
        name = str(metadata.get("name", "")).strip()
        if name and not boss_name_is_placeholder(name):
            record["name"] = name
        if metadata.get("level") not in (None, ""):
            record["level"] = metadata["level"]
        paths = metadata.get("fc_paths", [])
        if isinstance(paths, list) and paths:
            record["template_path"] = str(paths[0])[:160]
        try:
            localization_id = int(metadata.get("localization_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            localization_id = 0
        if not record.get("name") and localization_id:
            self.boss_name_resolver.request(
                pid,
                int(record.get("entity_id", 0) or 0),
                template_id,
                localization_id,
                int(record.get("filetime_100ns", 0) or 0),
            )

    @staticmethod
    def _capture_detail(payload: object) -> str:
        if isinstance(payload, dict):
            details = str(payload.get("details", "") or "").strip()
        else:
            details = str(payload or "").strip()
        lines = [line.strip() for line in details.splitlines() if line.strip()]
        return lines[-1] if lines else details

    def _create_network_parser(self, game_pid: int) -> NetworkPacketParser:
        remembered_self_token = ""
        try:
            remembered_game_pid = int(
                self.self_identity_cache.get("game_pid", 0) or 0
            )
        except (TypeError, ValueError, OverflowError):
            remembered_game_pid = 0
        if remembered_game_pid == int(game_pid or 0):
            remembered_self_token = str(
                self.self_identity_cache.get("user_token", "")
            ).strip()
        return NetworkPacketParser(
            self.team_profile_cache,
            self.monster_catalog,
            remembered_self_token=remembered_self_token,
            allow_cached_projection_roster=(
                remembered_game_pid == int(game_pid or 0)
            ),
            boss_name_allowlist=load_boss_name_allowlist(),
        )

    def _sync_parser_runtime_state(
        self, parser: NetworkPacketParser, game_pid: int
    ) -> bool:
        self._update_diagnostics(**parser.current_dungeon_context())
        profile_cache_dirty = False
        cached_profiles = parser.take_team_profile_cache()
        if cached_profiles is not None:
            self.team_profile_cache = cached_profiles
            profile_cache_dirty = True
        current_self_identity = parser.current_self_identity()
        if current_self_identity is not None:
            next_self_identity = {
                "game_pid": int(game_pid or 0),
                **current_self_identity,
            }
            if next_self_identity != self.self_identity_cache:
                self.self_identity_cache = next_self_identity
                write_json_object(
                    SELF_IDENTITY_CACHE_PATH,
                    self.self_identity_cache,
                )
        self._sync_active_boss_cache(parser, game_pid)
        return profile_cache_dirty

    def _process_capture_batch(
        self,
        parser: NetworkPacketParser,
        batch: dict,
        log_handle,
        game_pid: int,
    ) -> bool:
        records = list(batch.get("records", []) or [])
        native_records = list(batch.get("native_records", []) or [])
        native_boss_records = list(
            batch.get("native_boss_records", []) or []
        )
        native_name_records = list(
            batch.get("native_name_records", []) or []
        )
        native_skill_name_records = list(
            batch.get("native_skill_name_records", []) or []
        )
        native_damage_active = bool(
            batch.get("native_damage_hook_installed", False)
        )
        sequence_gaps = list(batch.get("sequence_gaps", []) or [])

        self._add_diagnostic_counts(capture_batches=1)
        if records:
            self._add_diagnostic_counts(network_records=len(records))
            self._update_diagnostics(last_network_record_at=time.time())
        if native_records:
            self._add_diagnostic_counts(
                native_damage_records=len(native_records)
            )
            self._update_diagnostics(last_native_damage_at=time.time())
        if native_boss_records:
            self._add_diagnostic_counts(
                native_boss_records=len(native_boss_records)
            )
        if sequence_gaps:
            self._add_diagnostic_counts(
                network_sequence_gaps=len(sequence_gaps)
            )
            first_gap = sequence_gaps[0]
            self.emit(
                "diagnostic",
                "网络采集序号不连续："
                f"期望 {first_gap.get('expected')}，实际 {first_gap.get('actual')}",
            )

        # Preserve the former worker's processing contract exactly. Native
        # metadata and damage are applied before the network records captured
        # at the beginning of the same polling cycle.
        for record in native_boss_records:
            self._enrich_boss_record(record, game_pid)
            self._record_native_boss_observation(record)
            log_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            for kind, update in parser.process_native_boss_type(record):
                self.emit(kind, update)
        for record in native_name_records:
            log_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            boss_name_update = parser.apply_runtime_boss_name(record)
            if boss_name_update:
                self.emit(*boss_name_update)
            runtime_name = str(record.get("name", "")).strip()
            if not boss_name_is_placeholder(runtime_name):
                self.emit("name", record)
        for record in native_skill_name_records:
            log_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.emit("skill_name", record)
        for record in native_records:
            self._record_native_damage_observation(record)
            log_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            for kind, update in parser.process_native_damage(record):
                self._emit_parser_update(parser, kind, update)
        for record in records:
            log_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            for kind, update in parser.process(
                record, include_damage=not native_damage_active
            ):
                self._emit_parser_update(parser, kind, update)

        team_status = batch.get("team_status")
        if isinstance(team_status, dict):
            self._update_diagnostics(
                team_stats_requests=int(
                    team_status.get("request_count", 0) or 0
                ),
                team_stats_last_result=int(
                    team_status.get("last_result", 0) or 0
                ),
                team_stats_last_request_filetime=int(
                    team_status.get("last_request_filetime", 0) or 0
                ),
            )
        return self._sync_parser_runtime_state(parser, game_pid)

    def _handle_capture_diagnostic(self, payload: object) -> None:
        component = str(payload.get("component", "") if isinstance(payload, dict) else "")
        detail = self._capture_detail(payload)
        prefixes = {
            "team_install": "团队精确伤害主动查询不可用",
            "native_install": "原生伤害入口不可用，已使用脚本封包",
            "native_poll": "原生伤害入口已回退",
            "team_status": "读取团队精确查询状态失败",
        }
        prefix = prefixes.get(component, "采集诊断")
        self.emit("diagnostic", f"{prefix}：{detail}" if detail else prefix)
        if component == "native_poll":
            self._update_diagnostics(
                native_damage_hook_installed=False,
                damage_source="script",
            )

    def _handle_cleanup_error(self, payload: object) -> None:
        component = str(payload.get("component", "") if isinstance(payload, dict) else "")
        detail = self._capture_detail(payload)
        prefixes = {
            "team": "恢复团队精确查询函数失败",
            "native": "恢复原生伤害函数失败",
            "network": "恢复游戏函数失败",
        }
        prefix = prefixes.get(component, "恢复采集函数失败")
        self.emit("error", f"{prefix}：{detail}" if detail else prefix)

    def _shutdown_capture_client(self, capture: CaptureProcessClient) -> None:
        capture.request_stop()
        warning_at = time.monotonic() + CAPTURE_PROCESS_SLOW_SHUTDOWN_SECONDS
        warned = False
        while capture.is_alive():
            try:
                capture.get(timeout=0.05)
            except queue.Empty:
                pass
            capture.join(0)
            if not warned and time.monotonic() >= warning_at:
                warned = True
                self.emit("diagnostic", "采集进程正在继续执行安全清理")

    def run(self) -> None:
        log_handle = None
        capture = None
        parser: NetworkPacketParser | None = None
        profile_cache_dirty = False
        last_profile_cache_save = 0.0
        active_session_id = 0
        expected_batch_id = 0
        game_pid = 0
        shutdown_requested = False
        shutdown_warning_at: float | None = None
        shutdown_warning_emitted = False
        child_exit_seen_at: float | None = None
        received_stopped = False
        try:
            self.boss_name_resolver.start()
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOG_DIR / time.strftime("network_%Y%m%d_%H%M%S.jsonl")
            log_handle = log_path.open("a", encoding="utf-8", buffering=256 * 1024)
            capture = CaptureProcessClient(parent_pid=os.getpid())
            capture.start()
            self._update_diagnostics(
                stage="capture_process_starting",
                capture_process_pid=capture.pid,
            )

            while True:
                now = time.monotonic()
                if self.stop_event.is_set() and not shutdown_requested:
                    capture.request_stop()
                    shutdown_requested = True
                    shutdown_warning_at = (
                        now + CAPTURE_PROCESS_SLOW_SHUTDOWN_SECONDS
                    )
                if (
                    shutdown_warning_at is not None
                    and not shutdown_warning_emitted
                    and now >= shutdown_warning_at
                    and capture.is_alive()
                ):
                    shutdown_warning_emitted = True
                    self.emit("diagnostic", "采集进程正在继续执行安全清理")

                try:
                    kind, payload = capture.get(timeout=0.05)
                    child_exit_seen_at = None
                except queue.Empty:
                    if profile_cache_dirty and now - last_profile_cache_save >= 1.0:
                        write_json_object(
                            TEAM_PROFILE_CACHE_PATH,
                            self.team_profile_cache,
                        )
                        profile_cache_dirty = False
                        last_profile_cache_save = now
                    if capture.is_alive():
                        continue
                    if child_exit_seen_at is None:
                        child_exit_seen_at = now
                        continue
                    if now - child_exit_seen_at < 0.1:
                        continue
                    if not received_stopped and not self.stop_event.is_set():
                        self._update_diagnostics(stage="capture_process_exited")
                        self.emit("fatal", "采集进程意外退出")
                    break

                if kind == "process_started" and isinstance(payload, dict):
                    self._update_diagnostics(
                        capture_process_pid=int(payload.get("pid", 0) or 0),
                        capture_process_priority=str(
                            payload.get("priority_class", "default")
                        ),
                        capture_process_priority_applied=bool(
                            payload.get("priority_applied", False)
                        ),
                    )
                elif kind == "state" and isinstance(payload, dict):
                    stage = str(payload.get("stage", ""))
                    self._update_diagnostics(**payload)
                    if stage == "game_not_found":
                        self.emit("waiting", "游戏未运行")
                    elif stage == "game_exited" and not self.stop_event.is_set():
                        self.emit("waiting", "游戏已退出")
                elif kind == "connected" and isinstance(payload, dict):
                    if parser is not None:
                        pending_cache = parser.take_team_profile_cache()
                        if pending_cache is not None:
                            self.team_profile_cache = pending_cache
                            profile_cache_dirty = True
                    active_session_id = int(payload.get("session_id", 0) or 0)
                    expected_batch_id = 0
                    game_pid = int(payload.get("game_pid", 0) or 0)
                    parser = self._create_network_parser(game_pid)
                    self._update_diagnostics(
                        stage="capturing",
                        process_found=True,
                        network_hook_installed=True,
                        native_damage_hook_installed=bool(
                            payload.get("native_damage_hook_installed", False)
                        ),
                        team_stats_hook_installed=bool(
                            payload.get("team_stats_hook_installed", False)
                        ),
                        damage_source=str(payload.get("damage_source", "none")),
                        game_pid=game_pid,
                    )
                    self.emit("connected", {"pid": game_pid, "log": str(log_path)})
                    restored_boss = self._restore_same_process_boss(parser, game_pid)
                    self._update_diagnostics(boss_state_restored=restored_boss)
                elif kind == "batch" and isinstance(payload, dict):
                    batch_session_id = int(payload.get("session_id", 0) or 0)
                    batch_id = int(payload.get("batch_id", -1))
                    if parser is None or batch_session_id != active_session_id:
                        self.emit(
                            "diagnostic",
                            f"忽略无对应会话的采集批次：{batch_session_id}/{batch_id}",
                        )
                        continue
                    if batch_id != expected_batch_id:
                        self.emit(
                            "diagnostic",
                            "采集批次序号不连续："
                            f"期望 {expected_batch_id}，实际 {batch_id}",
                        )
                    expected_batch_id = batch_id + 1
                    profile_cache_dirty = (
                        self._process_capture_batch(
                            parser,
                            payload,
                            log_handle,
                            game_pid,
                        )
                        or profile_cache_dirty
                    )
                elif kind == "diagnostic":
                    self._handle_capture_diagnostic(payload)
                elif kind == "cleanup_error":
                    self._handle_cleanup_error(payload)
                elif kind == "capture_error":
                    stage = str(
                        payload.get("stage", "capture_failed")
                        if isinstance(payload, dict)
                        else "capture_failed"
                    )
                    self._update_diagnostics(stage=stage)
                    detail = self._capture_detail(payload)
                    prefix = "连接失败" if stage == "network_hook_failed" else "采集异常"
                    self.emit("error", f"{prefix}：{detail}" if detail else prefix)
                elif kind == "session_closed" and isinstance(payload, dict):
                    self._update_diagnostics(
                        network_hook_installed=False,
                        native_damage_hook_installed=False,
                        team_stats_hook_installed=False,
                        damage_source="none",
                    )
                elif kind == "fatal":
                    self._update_diagnostics(stage="fatal")
                    self.emit("fatal", payload)
                elif kind == "stopped":
                    received_stopped = True
                    capture.join(1.0)
                    if not capture.is_alive():
                        break

                now = time.monotonic()
                if profile_cache_dirty and now - last_profile_cache_save >= 1.0:
                    write_json_object(
                        TEAM_PROFILE_CACHE_PATH,
                        self.team_profile_cache,
                    )
                    profile_cache_dirty = False
                    last_profile_cache_save = now
        except Exception:
            self._update_diagnostics(stage="fatal")
            self.emit("fatal", traceback.format_exc())
        finally:
            if capture is not None:
                try:
                    if capture.is_alive():
                        self._shutdown_capture_client(capture)
                finally:
                    try:
                        capture.close()
                    except Exception:
                        pass
            if parser is not None:
                pending_cache = parser.take_team_profile_cache()
                if pending_cache is not None:
                    self.team_profile_cache = pending_cache
                    profile_cache_dirty = True
            if profile_cache_dirty:
                write_json_object(TEAM_PROFILE_CACHE_PATH, self.team_profile_cache)
            if log_handle:
                log_handle.close()
            self._update_diagnostics(
                stage="stopped",
                network_hook_installed=False,
                native_damage_hook_installed=False,
                team_stats_hook_installed=False,
                damage_source="none",
                capture_process_pid=0,
            )
            self.emit("stopped", None)


class MetadataWorker(threading.Thread):
    def __init__(self, messages: queue.Queue, stop_event: threading.Event):
        super().__init__(name="C7RuntimeMetadata", daemon=False)
        self.messages = messages
        self.stop_event = stop_event

    def emit(self, kind: str, payload=None) -> None:
        self.messages.put((kind, payload))

    def run(self) -> None:
        # Kept only for the unused legacy window class. Runtime identity now
        # comes exclusively from decoded network packets in HookWorker.
        return


class _LegacyDpsWindow:
    def __init__(self):
        self.config = load_config()
        self.config.pop("boss_only", None)
        skill_names = load_skill_catalog()
        runtime_skill_names = self.config.get("runtime_skill_names", {})
        self.model = CombatModel(
            skill_names=(skill_names if isinstance(skill_names, dict) else {}),
            runtime_skill_names=(
                runtime_skill_names if isinstance(runtime_skill_names, dict) else {}
            ),
            # Entity IDs and automatic character names belong to the current
            # game session.  They must be confirmed again from live game data
            # on every DPS launch, never restored from local preferences.
            entity_names={},
            local_player_name="",
        )
        self.messages: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = HookWorker(self.messages, self.stop_event)
        self.connected = False
        self.closing = False
        self.drag_offset = (0, 0)
        self.skill_window: tk.Toplevel | None = None
        self.skill_tree: ttk.Treeview | None = None
        self.skill_actor_id: int | None = None
        self.skill_title_label: tk.Label | None = None
        self.skill_total_label: tk.Label | None = None

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=BG)
        self.root.geometry(self.config.get("geometry", "500x355+24+180"))
        self.root.minsize(440, 280)
        self.root.attributes("-topmost", bool(self.config.get("topmost", True)))
        self.root.attributes("-alpha", float(self.config.get("alpha", 0.94)))
        self.root.overrideredirect(True)
        self._build_styles()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(50, self._drain_messages)
        self.root.after(200, self._render)
        self.worker.start()
        self.metadata_worker.start()

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dps.Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            rowheight=30,
            font=("Microsoft YaHei UI", 10),
        )
        style.map(
            "Dps.Treeview",
            background=[("selected", "#27483f")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Dps.Treeview.Heading",
            background=PANEL_2,
            foreground=MUTED,
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.map("Dps.Treeview.Heading", background=[("active", PANEL_2)])

    def _button(self, parent, text: str, command, width=5, color=MUTED):
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=BG,
            fg=color,
            activebackground=PANEL_2,
            activeforeground=TEXT,
            borderwidth=0,
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        )

    def _build_ui(self) -> None:
        top = tk.Frame(self.root, bg=BG, height=38)
        top.pack(fill="x")
        top.pack_propagate(False)
        top.bind("<ButtonPress-1>", self._drag_start)
        top.bind("<B1-Motion>", self._drag_move)

        self.dot = tk.Label(top, text="●", bg=BG, fg=WARN, font=("Segoe UI", 10))
        self.dot.pack(side="left", padx=(12, 6))
        title = tk.Label(
            top,
            text="诡秘之主  伤害统计",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        title.pack(side="left")
        title.bind("<ButtonPress-1>", self._drag_start)
        title.bind("<B1-Motion>", self._drag_move)

        self._button(top, "×", self.close, width=3, color=ERROR).pack(side="right")
        self._button(top, "—", self.minimize, width=3).pack(side="right")
        self.pin_button = self._button(top, "置顶", self.toggle_topmost, width=5)
        self.pin_button.pack(side="right")
        self._button(top, "清零", self.reset, width=5).pack(side="right")

        summary = tk.Frame(self.root, bg=PANEL_2, height=52)
        summary.pack(fill="x", padx=8)
        summary.pack_propagate(False)
        self.total_label = tk.Label(
            summary,
            text="总伤害  0",
            bg=PANEL_2,
            fg=TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
        )
        self.total_label.pack(side="left", padx=12)
        self.dps_label = tk.Label(
            summary,
            text="每秒  0",
            bg=PANEL_2,
            fg=ACCENT,
            font=("Microsoft YaHei UI", 13, "bold"),
        )
        self.dps_label.pack(side="left", padx=(8, 0))
        self.time_label = tk.Label(
            summary,
            text="00:00",
            bg=PANEL_2,
            fg=MUTED,
            font=("Segoe UI", 10),
        )
        self.time_label.pack(side="right", padx=12)

        table_frame = tk.Frame(self.root, bg=PANEL)
        table_frame.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        columns = ("name", "damage", "dps", "share", "hits", "max_hit")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            style="Dps.Treeview",
            selectmode="browse",
        )
        headings = {
            "name": "队伍成员（点击看技能）",
            "damage": "伤害",
            "dps": "每秒",
            "share": "占比",
            "hits": "次数",
            "max_hit": "最大",
        }
        widths = {"name": 135, "damage": 90, "dps": 76, "share": 54, "hits": 48, "max_hit": 76}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=42,
                anchor="w" if column == "name" else "e",
                stretch=column in ("name", "damage"),
            )
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<ButtonRelease-1>", self.show_skill_details)
        self.tree.bind("<Return>", self.show_skill_details)

        bottom = tk.Frame(self.root, bg=BG, height=28)
        bottom.pack(fill="x")
        bottom.pack_propagate(False)
        self.status_label = tk.Label(
            bottom,
            text="正在连接…",
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 8),
        )
        self.status_label.pack(fill="both", padx=11)

        grip = tk.Label(self.root, text="◢", bg=BG, fg="#42505d", cursor="size_nw_se")
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

    def _drag_start(self, event) -> None:
        self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        x = event.x_root - self.drag_offset[0]
        y = event.y_root - self.drag_offset[1]
        self.root.geometry(f"+{x}+{y}")

    def _resize_start(self, event) -> None:
        self.resize_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_width(),
            self.root.winfo_height(),
        )

    def _resize_move(self, event) -> None:
        x, y, width, height = self.resize_origin
        self.root.geometry(f"{max(440, width + event.x_root - x)}x{max(280, height + event.y_root - y)}")

    def _drain_messages(self) -> None:
        if self.closing:
            return
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "event":
                    self.model.ingest(payload)
                elif kind == "name":
                    if self.model.ingest_name(payload):
                        self._save_preferences()
                elif kind == "skill_name":
                    if self.model.ingest_skill_name(payload):
                        self._save_preferences()
                elif kind == "connected":
                    self.connected = True
                    self.dot.configure(fg=ACCENT)
                    self.status_label.configure(
                        text="运行中",
                        fg=MUTED,
                    )
                elif kind == "waiting":
                    self.connected = False
                    self.dot.configure(fg=WARN)
                    self.status_label.configure(text=str(payload), fg=MUTED)
                elif kind in ("error", "fatal"):
                    self.connected = False
                    self.dot.configure(fg=ERROR)
                    self.status_label.configure(
                        text=chinese_error_message(payload), fg=ERROR
                    )
                elif kind == "metadata_error":
                    # Metadata is an optional read-only enhancement.  Native
                    # damage and name capture continue even if it is unavailable.
                    pass
                elif kind == "stopped" and self.closing:
                    self.root.destroy()
        except queue.Empty:
            pass
        self.root.after(50, self._drain_messages)

    def _render(self) -> None:
        if self.closing:
            return
        now = time.time()
        duration = self.model.duration(now)
        rows = sorted(self.model.stats.values(), key=lambda item: item.damage, reverse=True)
        total = sum(row.damage for row in rows)
        total_dps = total / duration if duration else 0.0
        self.total_label.configure(text=f"总伤害  {format_number(total)}")
        self.dps_label.configure(text=f"每秒  {format_number(total_dps)}")
        state = "战斗中" if self.model.active(now) else ("已结束" if total else "待机")
        self.time_label.configure(text=f"{state}  {format_duration(duration)}")

        present = set()
        for row in rows:
            item_id = str(row.actor_id)
            present.add(item_id)
            actor_dps = row.damage / duration if duration else 0.0
            share = row.damage / total * 100 if total else 0.0
            values = (
                self.model.display_name(row.actor_id),
                format_number(row.damage),
                format_number(actor_dps),
                f"{share:.1f}%",
                str(row.hits),
                format_number(row.max_hit),
            )
            if self.tree.exists(item_id):
                self.tree.item(item_id, values=values)
            else:
                self.tree.insert("", "end", iid=item_id, values=values)
        for item_id in self.tree.get_children(""):
            if item_id not in present:
                self.tree.delete(item_id)
        self._render_skill_details()
        self.root.after(200, self._render)

    def reset(self) -> None:
        self.model.reset(keep_identity=True, keep_monsters=True)
        self.status_label.configure(text="统计已清零", fg=MUTED)

    def _save_preferences(self) -> None:
        recent_skill_names = list(self.model.runtime_skill_names.items())[-2048:]
        self.config["runtime_skill_names"] = {
            str(skill_id): name for skill_id, name in recent_skill_names
        }
        # Drop the old cache because older builds could assign a variant name
        # to a neighbouring root ID.  The static client catalog is kept in its
        # own file and is never copied into preferences.
        self.config.pop("skill_names", None)
        self.config.pop("local_player_name", None)
        self.config.pop("entity_names", None)
        save_config(self.config)

    def show_skill_details(self, event=None) -> None:
        if event is not None and getattr(event, "num", None) == 1:
            item_id = self.tree.identify_row(event.y)
            if not item_id:
                return
            self.tree.selection_set(item_id)
        selected = self.tree.selection()
        if not selected:
            return
        self.skill_actor_id = int(selected[0])
        if self.skill_window is None or not self.skill_window.winfo_exists():
            self._build_skill_window()
        else:
            self.skill_window.deiconify()
            self.skill_window.lift()
            self.skill_window.focus_force()
        self._render_skill_details()

    def _build_skill_window(self) -> None:
        x = max(0, self.root.winfo_rootx() + self.root.winfo_width() + 12)
        y = max(0, self.root.winfo_rooty())
        window = tk.Toplevel(self.root)
        self.skill_window = window
        window.title("技能伤害分布")
        window.configure(bg=BG)
        window.geometry(f"540x360+{x}+{y}")
        window.minsize(480, 300)
        window.attributes("-topmost", bool(self.root.attributes("-topmost")))
        window.attributes("-alpha", 1.0)
        window.protocol("WM_DELETE_WINDOW", self._close_skill_window)

        header = tk.Frame(window, bg=BG, height=48)
        header.pack(fill="x", padx=10, pady=(8, 0))
        header.pack_propagate(False)
        self.skill_title_label = tk.Label(
            header,
            text="技能伤害分布",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.skill_title_label.pack(side="left", fill="both", expand=True)

        self.skill_total_label = tk.Label(
            window,
            text="总伤害  0",
            bg=PANEL_2,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        )
        self.skill_total_label.pack(fill="x", padx=10, pady=(0, 6), ipady=7)

        frame = tk.Frame(window, bg=PANEL)
        frame.pack(fill="both", expand=True, padx=10)
        columns = ("skill", "damage", "share", "hits", "max_hit")
        tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            style="Dps.Treeview",
            selectmode="browse",
        )
        self.skill_tree = tree
        headings = {
            "skill": "技能",
            "damage": "伤害",
            "share": "占比",
            "hits": "次数",
            "max_hit": "最大伤害",
        }
        widths = {"skill": 170, "damage": 95, "share": 65, "hits": 55, "max_hit": 90}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(
                column,
                width=widths[column],
                minwidth=48,
                anchor="w" if column == "skill" else "e",
                stretch=column in ("skill", "damage"),
            )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _render_skill_details(self) -> None:
        if (
            self.skill_window is None
            or not self.skill_window.winfo_exists()
            or self.skill_tree is None
            or self.skill_actor_id is None
        ):
            return
        actor_id = self.skill_actor_id
        actor = self.model.stats.get(actor_id)
        actor_name = self.model.display_name(actor_id)
        if self.skill_title_label is not None:
            self.skill_title_label.configure(text=f"{actor_name} · 技能伤害分布")
        skills = (
            sorted(actor.skills.values(), key=lambda item: item.damage, reverse=True)
            if actor is not None
            else []
        )
        total = actor.damage if actor is not None else 0
        if self.skill_total_label is not None:
            self.skill_total_label.configure(
                text=f"总伤害  {format_number(total)}    技能数量  {len(skills)}"
            )
        present = set()
        for skill in skills:
            item_id = str(skill.skill_id)
            present.add(item_id)
            share = skill.damage / total * 100 if total else 0.0
            values = (
                self.model.display_skill_name(actor_id, skill.skill_id),
                format_number(skill.damage),
                f"{share:.1f}%",
                str(skill.hits),
                format_number(skill.max_hit),
            )
            if self.skill_tree.exists(item_id):
                self.skill_tree.item(item_id, values=values)
            else:
                self.skill_tree.insert("", "end", iid=item_id, values=values)
        for item_id in self.skill_tree.get_children(""):
            if item_id not in present:
                self.skill_tree.delete(item_id)

    def _close_skill_window(self) -> None:
        if self.skill_window is not None and self.skill_window.winfo_exists():
            self.skill_window.destroy()
        self.skill_window = None
        self.skill_tree = None
        self.skill_actor_id = None
        self.skill_title_label = None
        self.skill_total_label = None

    def toggle_topmost(self) -> None:
        current = bool(self.root.attributes("-topmost"))
        self.root.attributes("-topmost", not current)
        if self.skill_window is not None and self.skill_window.winfo_exists():
            self.skill_window.attributes("-topmost", not current)
        self.pin_button.configure(fg=ACCENT if not current else MUTED)

    def minimize(self) -> None:
        self.root.overrideredirect(False)
        self.root.iconify()
        self.root.after(200, self._restore_borderless)

    def _restore_borderless(self) -> None:
        if self.root.state() == "normal":
            self.root.overrideredirect(True)
        else:
            self.root.after(200, self._restore_borderless)

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.status_label.configure(text="正在恢复游戏函数并退出…", fg=WARN)
        self._save_preferences()
        self.config["geometry"] = self.root.geometry()
        self.config["topmost"] = bool(self.root.attributes("-topmost"))
        self.config["alpha"] = float(self.root.attributes("-alpha"))
        save_config(self.config)
        self.stop_event.set()
        self.root.after(50, self._finish_close)

    def _finish_close(self) -> None:
        if self.worker.is_alive() or self.metadata_worker.is_alive():
            self.root.after(50, self._finish_close)
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()



class DpsWindow:
    """Borderless LOA-Logs-inspired live meter with custom Windows chrome."""

    def __init__(self):
        self.config = load_config()
        # The redesigned window has no pin toggle.  A legacy false value makes
        # the meter fall behind the game as soon as the game receives focus.
        self.config["topmost"] = True
        self.metadata = load_skill_metadata()
        self.professions = (
            self.metadata.get("professions", {})
            if isinstance(self.metadata.get("professions"), dict)
            else {}
        )
        raw_skill_metadata = self.metadata.get("skills", {})
        skill_professions: dict[str, list[int]] = {}
        if isinstance(raw_skill_metadata, dict):
            for skill_id, value in raw_skill_metadata.items():
                if not isinstance(value, dict):
                    continue
                class_ids = value.get("profession_ids", [])
                if isinstance(class_ids, list) and class_ids:
                    skill_professions[str(skill_id)] = class_ids

        # Effect/sub-skill IDs can have a client-derived icon alias even when
        # SkillDataNew has no row for that exact packet ID.  The extraction
        # manifest only supplies a profession when every matching source skill
        # agrees on one class, so ambiguous shared skills remain unidentified.
        icon_sources = load_icon_sources()
        for section_name in ("skills", "skill_icon_aliases"):
            section = icon_sources.get(section_name, {})
            if not isinstance(section, dict):
                continue
            for skill_id, value in section.items():
                if not isinstance(value, dict):
                    continue
                class_ids = value.get("profession_ids", [])
                if isinstance(class_ids, list) and class_ids:
                    skill_professions.setdefault(str(skill_id), class_ids)

        skill_names = load_skill_catalog()
        runtime_skill_names = self.config.get("runtime_skill_names", {})
        self.history_store = CombatHistoryStore(HISTORY_DIR)
        self.model = CombatModel(
            skill_names=skill_names,
            runtime_skill_names=(
                runtime_skill_names if isinstance(runtime_skill_names, dict) else {}
            ),
            skill_professions=skill_professions,
            entity_names={},
            local_player_name="",
            boss_only=True,
        )

        self.messages: queue.Queue = queue.Queue()
        self.control_messages: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = HookWorker(self.messages, self.stop_event)
        self.heartbeat_stop_event = threading.Event()
        self.heartbeat_worker: LicenseHeartbeatWorker | None = None
        self.connected = False
        self.game_pid = 0
        self.closing = False
        self.close_started_at = 0.0
        self.close_finalized = False
        self.authorization_resetting = False
        self.close_status_text = ""
        self.capture_started = False
        self.hide_names = bool(self.config.get("hide_names", False))
        self.compact_mode = bool(self.config.get("compact_mode", False))
        self.show_total_damage = bool(
            self.config.get("show_total_damage", True)
        )
        self.show_dps = bool(self.config.get("show_dps", True))
        self.show_damage_share = bool(
            self.config.get("show_damage_share", True)
        )
        self.show_critical_rate = bool(
            self.config.get("show_critical_rate", True)
        )
        self.window_locked = bool(self.config.get("window_locked", False))
        self.window_lock_topmost_restore: bool | None = (
            bool(self.config.get("topmost", True))
            if self.window_locked
            else None
        )
        self.unlock_window: tk.Toplevel | None = None
        self.unlock_button: tk.Label | None = None
        self.window_lock_original_styles: dict[int, int] = {}
        self.main_scroll_offset = 0
        self.main_scroll_content_height = 0
        self.drag_state: dict[int, tuple[int, int]] = {}
        self.resize_state: dict[int, tuple[int, int, int, int]] = {}
        self.restore_geometry: dict[int, str] = {}
        self.skill_window: tk.Toplevel | None = None
        self.skill_actor_id: int | None = None
        self.skill_detail_mode = "skills"
        self.skill_mode_buttons: dict[str, tk.Label] = {}
        self.skill_combat_metrics_label: tk.Label | None = None
        self.history_window: tk.Toplevel | None = None
        self.history_records: list[dict] = []
        self.history_selected_id = ""
        self.history_selected_actor = 0
        self.history_detail_mode = "skills"
        self.history_detail_buttons: dict[str, tk.Label] = {}
        self.backend_current_page = "history"
        self.backend_pages: dict[str, tk.Frame] = {}
        self.backend_nav_buttons: dict[str, tk.Frame] = {}
        self.backend_settings_buttons: dict[str, tk.Label] = {}
        self.backend_settings_frames: dict[str, tk.Frame] = {}
        self.history_list_canvas: tk.Canvas | None = None
        self.history_list_scrollbar: tk.Scrollbar | None = None
        self.history_query_var: tk.StringVar | None = None
        self.history_participant_panel: tk.Frame | None = None
        self.history_participant_header_canvas: tk.Canvas | None = None
        self.history_participant_canvas: tk.Canvas | None = None
        self.history_participant_scrollbar: tk.Scrollbar | None = None
        self.history_skill_canvas: tk.Canvas | None = None
        self.history_skill_scrollbar: tk.Scrollbar | None = None
        self.history_count_label: tk.Label | None = None
        self.history_target_label: tk.Label | None = None
        self.history_time_label: tk.Label | None = None
        self.history_metrics_label: tk.Label | None = None
        self.history_total_value: tk.Label | None = None
        self.history_dps_value: tk.Label | None = None
        self.history_team_value: tk.Label | None = None
        self.history_detail_label: tk.Label | None = None
        self.history_favorite_button: tk.Label | None = None
        self.history_max_button: tk.Label | None = None
        self.feedback_window: tk.Toplevel | None = None
        self.feedback_category_var: tk.StringVar | None = None
        self.feedback_record_var: tk.StringVar | None = None
        self.feedback_record_ids: dict[str, str] = {}
        self.feedback_content: tk.Text | None = None
        self.feedback_diagnostics_var: tk.BooleanVar | None = None
        self.feedback_status_label: tk.Label | None = None
        self.feedback_submit_button: tk.Label | None = None
        self.feedback_submitting = False
        self.notice_window: tk.Frame | None = None
        self.update_window: tk.Toplevel | None = None
        self.update_status_label: tk.Label | None = None
        self.update_summary_label: tk.Label | None = None
        self.update_action_button: tk.Label | None = None
        self.update_button: tk.Label | None = None
        self.pending_update: UpdateInfo | None = None
        self.update_check_started = False
        self.update_check_in_progress = False
        self.update_downloading = False
        self.login_window: tk.Toplevel | None = None
        self.login_card_entry: tk.Entry | None = None
        self.login_status_label: tk.Label | None = None
        self.login_button: tk.Label | None = None
        self.login_placeholder_active = False
        self.membership_label: tk.Label | None = None
        self.expiry_label: tk.Label | None = None
        self.login_status_message = ""
        self.opacity_value_label: tk.Label | None = None
        self.opacity_scale: ModernSlider | None = None
        self.settings_opacity_var: tk.IntVar | None = None
        self.settings_font_size_var: tk.IntVar | None = None
        self.settings_show_names_var: tk.BooleanVar | None = None
        self.settings_show_damage_var: tk.BooleanVar | None = None
        self.settings_show_dps_var: tk.BooleanVar | None = None
        self.settings_show_share_var: tk.BooleanVar | None = None
        self.settings_show_critical_var: tk.BooleanVar | None = None
        self.settings_font_value_label: tk.Label | None = None
        self.main_tooltip: tk.Toplevel | None = None
        self.main_tooltip_after_id: str | None = None
        self.main_content_overlay_supported = sys.platform == "win32"
        self.main_content_overlay_window: tk.Toplevel | None = None
        self.main_content_overlay_header: tk.Canvas | None = None
        self.main_content_overlay_rows: tk.Canvas | None = None
        self.main_content_overlay_dps: tk.Canvas | None = None
        self.main_content_overlay_sync_after_id: str | None = None
        self.main_content_overlay_root_geometry: tuple[int, int, int, int] | None = None
        self.main_content_overlay_click_through_ready = False
        self.main_content_overlay_topmost: bool | None = None
        self.process_is_elevated = is_process_elevated()
        self.tray_icon = None
        self.tray_skill_hidden = False
        self.tray_history_hidden = False
        self.tray_feedback_hidden = False
        client_id = resolve_client_id(
            self.config,
            DEVICE_ID_PATH,
            SHARED_CONFIG_PATH,
        )
        server_url = str(
            os.environ.get(
                "GMZZ_DPS_SERVER_URL",
                self.config.get("server_url", DEFAULT_SERVER_URL),
            )
        ).strip()
        self.config["server_url"] = server_url
        self.licensing = LicensingService(
            ServerLicensingGateway(server_url, client_id, CLIENT_BUILD)
        )
        save_config(self.config)

        try:
            configured_alpha = float(self.config.get("alpha", 1.0))
        except (TypeError, ValueError, OverflowError):
            configured_alpha = 1.0
        self.window_alpha = min(
            1.0,
            max(MIN_WINDOW_ALPHA_PERCENT / 100.0, configured_alpha),
        )

        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "GMZZ.DPSMeter"
                )
            except (AttributeError, OSError):
                pass

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.withdraw()
        self.root.configure(bg=BG)
        self.root.geometry(self._initial_geometry())
        self.root.minsize(
            MINI_MIN_WIDTH if self.compact_mode else MAIN_MIN_WIDTH,
            MINI_MIN_HEIGHT if self.compact_mode else MAIN_MIN_HEIGHT,
        )
        self.root.attributes(
            "-topmost",
            self.window_locked or bool(self.config.get("topmost", True)),
        )
        self.root.attributes("-alpha", self.window_alpha)
        self.root.overrideredirect(True)
        self._initialize_ui_fonts()
        self.icons = IconFactory(self.root)
        self.app_window_icon = self.icons.app_logo(64)
        try:
            self.root.iconphoto(True, self.app_window_icon)
            if sys.platform == "win32" and APP_ICON_PATH.is_file():
                self.root.iconbitmap(default=str(APP_ICON_PATH))
        except tk.TclError:
            pass
        self._build_ui()
        self._apply_layout_mode()
        if WindowsTrayIcon is not None:
            try:
                self.tray_icon = WindowsTrayIcon(
                    APP_NAME,
                    APP_ICON_PATH,
                    lambda action: self.control_messages.put((action, None)),
                ).start()
            except Exception:
                pass
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(50, self._drain_messages)
        self.root.after(160, self._render)
        self.root.after(20, lambda: self._apply_windows_style(self.root))
        self.root.after(45, self._apply_main_transparency)
        self.root.after(0, self._show_login)

    def _enabled_main_metrics(self) -> tuple[str, ...]:
        return tuple(
            key
            for key, enabled in (
                ("damage", self.show_total_damage),
                ("dps", self.show_dps),
                ("share", self.show_damage_share),
                ("critical", self.show_critical_rate),
            )
            if enabled
        )

    def _compact_target_width(self) -> int:
        return compact_width_for_visible_metrics(len(self._enabled_main_metrics()))

    def _adaptive_compact_geometry(
        self, value: object, fallback_x: int = 32, fallback_y: int = 120
    ) -> str:
        match = re.fullmatch(
            r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", str(value or "")
        )
        if match:
            _width, height, x, y = match.groups()
            parsed_height = max(MINI_MIN_HEIGHT, int(height))
            suffix = f"{x}{y}"
        else:
            parsed_height = MINI_DEFAULT_HEIGHT
            suffix = f"{int(fallback_x):+d}{int(fallback_y):+d}"
        return f"{self._compact_target_width()}x{parsed_height}{suffix}"

    def _initial_geometry(self) -> str:
        if self.compact_mode:
            saved = str(self.config.get("compact_geometry", ""))
            saved = self._adaptive_compact_geometry(saved)
            self.config["compact_geometry"] = saved
            return self._visible_geometry(
                saved, MINI_DEFAULT_WIDTH, MINI_DEFAULT_HEIGHT
            )
        saved = str(self.config.get("geometry", ""))
        if int(self.config.get("layout_version", 0)) < 14:
            match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+[+-]\d+)", saved)
            if match:
                _width, _height, suffix = match.groups()
                upgraded = f"590x400{suffix}"
            else:
                upgraded = "590x400+32+120"
            return self._visible_geometry(upgraded, 590, 400)
        return self._visible_geometry(saved, 590, 400)

    def _visible_geometry(self, value: str, default_width: int, default_height: int) -> str:
        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", str(value))
        if match:
            width, height, x, y = (int(part) for part in match.groups())
        else:
            width, height, x, y = default_width, default_height, 32, 120
        width = max(1, min(width, self.root.winfo_screenwidth()))
        height = max(1, min(height, self.root.winfo_screenheight()))
        x = max(0, min(x, self.root.winfo_screenwidth() - width))
        y = max(0, min(y, self.root.winfo_screenheight() - height))
        return f"{width}x{height}+{x}+{y}"

    def _initialize_ui_fonts(self) -> None:
        available = tkfont.families(self.root)
        self.ui_font_family = preferred_font_family(
            available,
            ("MiSans", "Microsoft YaHei UI", "Microsoft YaHei"),
        )
        self.number_font_family = preferred_font_family(
            available,
            ("Segoe UI Variable Text", "Segoe UI", self.ui_font_family),
        )
        try:
            configured_size = int(self.config.get("font_size", 14))
        except (TypeError, ValueError, OverflowError):
            configured_size = 14
        self.ui_font_size = min(18, max(12, configured_size))
        self.ui_fonts: dict[str, tkfont.Font] = {}
        self._configure_ui_fonts()

    def _configure_ui_fonts(self) -> None:
        base = int(self.ui_font_size)
        specs = {
            "micro": (self.ui_font_family, max(9, base - 3), "normal"),
            "small": (self.ui_font_family, max(10, base - 2), "normal"),
            "body": (self.ui_font_family, base, "normal"),
            "strong": (self.ui_font_family, base, "bold"),
            "title": (self.ui_font_family, base + 2, "bold"),
            "heading": (self.ui_font_family, base + 5, "bold"),
            "number": (self.number_font_family, base, "normal"),
            "number_small": (
                self.number_font_family,
                max(10, base - 2),
                "bold",
            ),
            "number_strong": (self.number_font_family, base, "bold"),
            "number_large": (self.number_font_family, base + 5, "bold"),
            "icon": ("Segoe UI Symbol", base + 3, "normal"),
        }
        for role, (family, pixels, weight) in specs.items():
            options = {"family": family, "size": -int(pixels), "weight": weight}
            if role in self.ui_fonts:
                self.ui_fonts[role].configure(**options)
            else:
                self.ui_fonts[role] = tkfont.Font(root=self.root, **options)

    def _ui_font(self, role: str = "body") -> tkfont.Font:
        return self.ui_fonts.get(role, self.ui_fonts["body"])

    def _show_login(self) -> None:
        if self.closing or (
            self.login_window is not None and self.login_window.winfo_exists()
        ):
            return
        width = 420
        height = 354 if not self.process_is_elevated else 286
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        window = tk.Toplevel(self.root)
        self.login_window = window
        window.title(f"{UI_BRAND} · 登录")
        window.configure(bg=BORDER)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.resizable(False, False)
        window.attributes("-topmost", True)
        window.attributes("-alpha", 1.0)
        window.overrideredirect(True)
        window.protocol("WM_DELETE_WINDOW", self.close)

        shell = tk.Frame(window, bg=BORDER, bd=0)
        shell.pack(fill="both", expand=True)
        body = tk.Frame(shell, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        titlebar = tk.Frame(body, bg=SURFACE, height=48)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        self._bind_drag(titlebar, window)

        logo_image = self.icons.app_logo(26)
        logo = tk.Label(titlebar, image=logo_image, bg=SURFACE, bd=0)
        logo.image = logo_image
        logo.pack(side="left", padx=(18, 9), pady=10)
        self._bind_drag(logo, window)

        title = tk.Label(
            titlebar,
            text=UI_BRAND,
            bg=SURFACE,
            fg=TEXT,
            font=self._ui_font("title"),
        )
        title.pack(side="left")
        self._bind_drag(title, window)
        close_button = self._label_button(
            titlebar,
            "×",
            self.close,
            width=46,
            hover="#7f2d35",
            fg="#c5ccd3",
            font=self._ui_font("icon"),
        )
        close_button.pack(side="right", fill="y")

        content = tk.Frame(body, bg=BG)
        content.pack(
            fill="both",
            expand=True,
            padx=26,
            pady=(18 if not self.process_is_elevated else 24, 20),
        )

        if not self.process_is_elevated:
            admin_notice = tk.Frame(
                content,
                bg="#241b10",
                highlightthickness=1,
                highlightbackground="#73572d",
                height=54,
            )
            admin_notice.pack(fill="x", pady=(0, 13))
            admin_notice.pack_propagate(False)
            tk.Label(
                admin_notice,
                text="请使用管理员模式启动",
                bg="#241b10",
                fg=WARN,
                anchor="w",
                font=self._ui_font("strong"),
            ).pack(fill="x", padx=13, pady=(7, 0))
            tk.Label(
                admin_notice,
                text="否则可能无法连接游戏并读取战斗数据",
                bg="#241b10",
                fg="#c9b28b",
                anchor="w",
                font=self._ui_font("small"),
            ).pack(fill="x", padx=13, pady=(1, 7))

        field = tk.Frame(
            content,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground="#344353",
            height=54,
        )
        field.pack(fill="x")
        field.pack_propagate(False)
        self.login_card_entry = tk.Entry(
            field,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#315d52",
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            font=self._ui_font("number"),
        )
        self.login_card_entry.pack(
            side="left", fill="both", expand=True, padx=15, pady=10
        )
        saved_card_key = remembered_card(self.config)
        if saved_card_key:
            self.login_placeholder_active = False
            self.login_card_entry.insert(0, saved_card_key)
            self.login_card_entry.selection_range(0, "end")
        else:
            self._show_login_placeholder()
        self.login_card_entry.bind("<FocusIn>", self._login_entry_focus_in)
        self.login_card_entry.bind("<FocusOut>", self._login_entry_focus_out)
        self.login_card_entry.bind("<KeyPress>", self._login_entry_key_press)
        self.login_card_entry.bind("<<Paste>>", self._login_entry_prepare_input)

        self.login_status_label = tk.Label(
            content,
            text=self.login_status_message,
            bg=BG,
            fg=ERROR if self.login_status_message else MUTED,
            anchor="w",
            font=self._ui_font("small"),
        )
        self.login_status_label.pack(fill="x", pady=(7, 7))
        self.login_button = tk.Label(
            content,
            text="登录",
            bg=ACCENT,
            fg="#07110e",
            cursor="hand2",
            pady=11,
            font=self._ui_font("strong"),
        )
        self.login_button.pack(fill="x")
        self.login_button.bind(
            "<Enter>", lambda _event: self.login_button.configure(bg="#86edca")
        )
        self.login_button.bind(
            "<Leave>", lambda _event: self.login_button.configure(bg=ACCENT)
        )
        self.login_button.bind(
            "<Button-1>", lambda _event: self._complete_card_login()
        )

        group_label = tk.Label(
            content,
            text="QQ群：1094925831",
            bg=BG,
            fg=MUTED,
            font=self._ui_font("small"),
        )
        group_label.pack(fill="x", pady=(13, 0))

        window.bind("<Return>", lambda _event: self._complete_card_login())
        window.after(20, lambda: self._apply_windows_style(window))
        window.after(50, self.login_card_entry.focus_set)
        window.grab_set()
        window.focus_force()

    def _show_login_placeholder(self) -> None:
        entry = self.login_card_entry
        if entry is None or not entry.winfo_exists() or entry.get():
            return
        self.login_placeholder_active = True
        entry.configure(fg=MUTED)
        entry.insert(0, "请输入卡号")

    def _login_entry_focus_in(self, _event=None) -> None:
        entry = self.login_card_entry
        if entry is None:
            return
        if self.login_placeholder_active:
            entry.icursor(0)

    def _login_entry_prepare_input(self, _event=None) -> None:
        entry = self.login_card_entry
        if entry is None or not self.login_placeholder_active:
            return
        entry.delete(0, "end")
        entry.configure(fg=TEXT)
        self.login_placeholder_active = False

    def _login_entry_key_press(self, event=None) -> None:
        if event is not None and event.keysym in {
            "Control_L",
            "Control_R",
            "Shift_L",
            "Shift_R",
            "Alt_L",
            "Alt_R",
            "Tab",
        }:
            return
        self._login_entry_prepare_input()

    def _login_entry_focus_out(self, _event=None) -> None:
        entry = self.login_card_entry
        if entry is None or entry.get().strip():
            return
        self._show_login_placeholder()

    def _set_login_status(self, message: str, *, error: bool = True) -> None:
        self.login_status_message = str(message).strip()
        if self.login_status_label is not None and self.login_status_label.winfo_exists():
            self.login_status_label.configure(
                text=self.login_status_message,
                fg=ERROR if error else MUTED,
            )

    def _complete_card_login(self) -> None:
        if self.closing or self.authorization_resetting:
            return
        card_key = (
            self.login_card_entry.get().strip()
            if self.login_card_entry is not None
            and not self.login_placeholder_active
            else ""
        )
        if not card_key:
            self._set_login_status("请输入卡号。")
            return
        if self.login_button is not None:
            self.login_button.configure(text="正在验证", bg=PANEL_2, fg=MUTED)
        if self.login_window is not None:
            self.login_window.update_idletasks()
        try:
            session = self.licensing.sign_in_card(card_key)
        except LicensingConnectionError as exc:
            self._set_login_status(str(exc))
            if self.login_button is not None:
                self.login_button.configure(text="登录", bg=ACCENT, fg="#07110e")
            return
        if not session.active:
            self._set_login_status(session.display_name or "服务器已拒绝本次登录。")
            if self.login_button is not None:
                self.login_button.configure(text="登录", bg=ACCENT, fg="#07110e")
            return
        if remember_card(self.config, card_key):
            save_config(self.config)
        self.login_status_message = ""
        if self.login_window is not None and self.login_window.winfo_exists():
            try:
                self.login_window.grab_release()
            except tk.TclError:
                pass
            self.login_window.destroy()
        self.login_window = None
        self.login_card_entry = None
        self.login_status_label = None
        self.login_button = None
        self.root.deiconify()
        self.root.lift()
        if not self.window_locked:
            self.root.focus_force()
        self._sync_expiry_label()
        self.root.after(20, lambda: self._apply_windows_style(self.root))
        self.root.after(30, self._apply_window_lock_state)
        self.root.after(45, self._apply_main_transparency)
        self.heartbeat_stop_event = threading.Event()
        self.heartbeat_worker = LicenseHeartbeatWorker(
            self.licensing, self.control_messages, self.heartbeat_stop_event
        )
        self.heartbeat_worker.start()
        self._start_capture()
        self._start_update_check()

    def _start_capture(self) -> None:
        if self.capture_started or self.closing:
            return
        self.stop_event = threading.Event()
        self.worker = HookWorker(self.messages, self.stop_event)
        self.capture_started = True
        self.worker.start()

    def _start_update_check(self, *, manual: bool = False) -> None:
        if self.update_check_in_progress or self.closing:
            return
        if self.update_check_started and not manual:
            return
        self.update_check_started = True
        self.update_check_in_progress = True
        self._refresh_update_page()

        def check() -> None:
            try:
                update = self.licensing.check_update()
                payload = {"manual": manual, "update": update, "error": ""}
            except LicensingConnectionError as exc:
                payload = {"manual": manual, "update": None, "error": str(exc)}
            except Exception:
                payload = {
                    "manual": manual,
                    "update": None,
                    "error": "检查更新失败，请稍后重试。",
                }
            self.control_messages.put(("update_check_result", payload))

        threading.Thread(target=check, name="update-check", daemon=True).start()

    def _handle_update_check_result(self, payload: object) -> None:
        self.update_check_in_progress = False
        if not isinstance(payload, dict):
            self._refresh_update_page("更新检查返回了无效结果。")
            return
        manual = bool(payload.get("manual"))
        update = payload.get("update")
        error = str(payload.get("error", "")).strip()
        if isinstance(update, UpdateInfo) and update.available:
            self._show_update_window(update)
            return
        if not manual:
            self._refresh_update_page()
            return
        self.pending_update = None
        if error:
            self._refresh_update_page(error)
            return
        self._refresh_update_page(
            f"当前已是最新版本，更新文件保存位置：{UPDATE_DIR}"
        )

    def _show_update_window(self, update: UpdateInfo) -> None:
        if self.closing:
            return
        self.pending_update = update
        self._refresh_update_page()

    def _update_page_action(self) -> None:
        if self.pending_update is not None and self.pending_update.available:
            self._download_pending_update()
        else:
            self._start_update_check(manual=True)

    def _refresh_update_page(self, status_text: str = "") -> None:
        update = getattr(self, "pending_update", None)
        update_summary_label = getattr(self, "update_summary_label", None)
        update_status_label = getattr(self, "update_status_label", None)
        update_action_button = getattr(self, "update_action_button", None)
        if update_summary_label is not None:
            if update is not None and update.available:
                size_text = (
                    f" · {update.size / 1024 / 1024:.1f} MB"
                    if update.size > 0
                    else ""
                )
                update_summary_label.configure(
                    text=f"发现新版本 v{update.latest_version}{size_text}",
                    fg=ACCENT,
                )
            else:
                update_summary_label.configure(
                    text=f"当前版本 v{APP_VERSION}", fg=TEXT
                )
        if update_status_label is not None:
            if status_text:
                update_status_label.configure(text=status_text, fg=MUTED)
            elif update is not None and update.available:
                update_status_label.configure(
                    text=(update.notes or "新版本可下载。"), fg=MUTED
                )
            else:
                update_status_label.configure(
                    text="更新检查和下载均在本页显示，不会弹出新窗口。",
                    fg=MUTED,
                )
        if update_action_button is not None:
            if bool(getattr(self, "update_downloading", False)):
                text, disabled = "下载中…", True
            elif bool(getattr(self, "update_check_in_progress", False)):
                text, disabled = "检查中…", True
            elif update is not None and update.available:
                text, disabled = "立即更新", False
            else:
                text, disabled = "检查更新", False
            update_action_button._disabled = disabled
            update_action_button.configure(
                text=text,
                cursor="arrow" if disabled else "hand2",
                bg=PANEL_2 if disabled else ACCENT,
                fg=MUTED if disabled else "#07110e",
            )

    def _download_pending_update(self) -> None:
        update = self.pending_update
        if update is None or self.update_downloading or self.closing:
            return
        self.update_downloading = True
        if self.update_status_label is not None:
            self.update_status_label.configure(text="正在下载并校验…", fg=MUTED)
        if self.update_action_button is not None:
            self.update_action_button.configure(text="下载中", bg=PANEL_2, fg=MUTED)

        def download() -> None:
            last_progress_at = 0.0
            last_percentage = -1

            def report_progress(written: int, total: int) -> None:
                nonlocal last_progress_at, last_percentage
                percentage = min(100, int(written * 100 / total)) if total > 0 else 0
                now = time.monotonic()
                if (
                    percentage == last_percentage
                    or (percentage < 100 and now - last_progress_at < 0.1)
                ):
                    return
                last_progress_at = now
                last_percentage = percentage
                self.control_messages.put(
                    ("update_download_progress", (written, total))
                )

            try:
                destination = UPDATE_DIR / update.filename
                if IS_FROZEN and destination.resolve() == APP_EXECUTABLE_PATH:
                    destination = destination.with_name(
                        f"{destination.stem}.update{destination.suffix}"
                    )
                path = self.licensing.download_update(
                    update, destination, report_progress
                )
                result = ("update_downloaded", path)
            except (LicensingConnectionError, OSError) as exc:
                result = ("update_download_failed", str(exc))
            except Exception:
                result = (
                    "update_download_failed",
                    "更新下载失败，请稍后重试。",
                )
            self.control_messages.put(result)

        threading.Thread(target=download, name="update-download", daemon=True).start()

    def _handle_update_download_progress(self, payload: object) -> None:
        if not self.update_downloading or self.update_status_label is None:
            return
        try:
            written, total = payload
            written = max(0, int(written))
            total = max(0, int(total))
        except (TypeError, ValueError, OverflowError):
            return
        if total <= 0:
            text = f"正在下载 {written / 1024 / 1024:.1f} MB…"
        else:
            percentage = min(100, int(written * 100 / total))
            text = f"正在下载 {percentage}%…"
        self.update_status_label.configure(text=text, fg=MUTED)

    def _handle_update_downloaded(self, payload: object) -> None:
        self.update_downloading = False
        update_path = Path(str(payload))
        if not IS_FROZEN or sys.platform != "win32":
            if self.update_status_label is not None:
                self.update_status_label.configure(
                    text=f"安装包已保存：{update_path}", fg=ACCENT
                )
            if self.update_action_button is not None:
                self.update_action_button.configure(text="已下载", bg=PANEL_2, fg=ACCENT)
            return
        try:
            self._launch_update_replacer(update_path)
        except OSError as exc:
            self._handle_update_download_failed(str(exc))
            return
        if self.update_status_label is not None:
            self.update_status_label.configure(text="即将重启并完成更新…", fg=ACCENT)
        self.close_status_text = "正在安装更新…"
        self.root.after(120, self.close)

    def _handle_update_download_failed(self, payload: object) -> None:
        self.update_downloading = False
        message = str(payload).strip() or "更新下载失败，请稍后重试。"
        if self.update_status_label is not None:
            self.update_status_label.configure(text=message, fg=ERROR)
        if self.update_action_button is not None:
            self.update_action_button.configure(text="重试", bg=ACCENT, fg="#07110e")

    def _launch_update_replacer(self, update_path: Path) -> None:
        target_path = APP_EXECUTABLE_PATH
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        script_path = UPDATE_DIR / "apply-update.ps1"
        script_path.write_text(
            "param([int]$TargetPid,[string]$Source,[string]$Target)\n"
            "$ErrorActionPreference = 'Stop'\n"
            "Wait-Process -Id $TargetPid -ErrorAction SilentlyContinue\n"
            "$installed = $false\n"
            "for ($attempt = 0; $attempt -lt 20; $attempt++) {\n"
            "  try { Copy-Item -LiteralPath $Source -Destination $Target -Force; $installed = $true; break }\n"
            "  catch { Start-Sleep -Milliseconds 500 }\n"
            "}\n"
            "if ($installed) {\n"
            "  Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target)\n"
            "  Remove-Item -LiteralPath $Source -Force -ErrorAction SilentlyContinue\n"
            "}\n"
            "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue\n",
            encoding="utf-8-sig",
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script_path),
                "-TargetPid",
                str(os.getpid()),
                "-Source",
                str(update_path.resolve()),
                "-Target",
                str(target_path),
            ],
            close_fds=True,
            creationflags=creation_flags,
        )

    def _close_update_window(self, *, force: bool = False) -> None:
        if self.update_downloading and not force:
            return
        if self.update_window is not None and self.update_window.winfo_exists():
            self.update_window.destroy()
        self.update_window = None
        self.update_status_label = None
        self.update_action_button = None

    def _return_to_login(self, message: str) -> None:
        if self.closing or self.authorization_resetting:
            return
        self.authorization_resetting = True
        self._set_window_click_through(self.root, False)
        self._destroy_unlock_window()
        self.connected = False
        self.game_pid = 0
        self._close_notice_window()
        self._close_skill_window()
        self._close_history_window()
        self._close_feedback_window()
        self._close_update_window(force=True)
        self.dot.configure(fg=WARN)
        self.status_label.configure(text="正在安全停止", fg=WARN)
        self.heartbeat_stop_event.set()
        self.stop_event.set()
        self.login_status_message = str(message).strip() or "请重新输入卡号。"
        self.root.after(50, self._finish_return_to_login)

    def _finish_return_to_login(self) -> None:
        if self.closing:
            return
        if self.worker.is_alive() or (
            self.heartbeat_worker is not None and self.heartbeat_worker.is_alive()
        ):
            self.root.after(50, self._finish_return_to_login)
            return
        if not self._ingest_pending_capture_messages():
            self.root.after(1, self._finish_return_to_login)
            return
        self.model.reset(keep_identity=False, archive_reason="authorization")
        self.model.entity_names.clear()
        self.model.entity_professions.clear()
        self.model.local_player_name = ""
        self._flush_combat_history()
        self.capture_started = False
        self.heartbeat_worker = None
        self.update_check_started = False
        self.pending_update = None
        self.connected = False
        self.game_pid = 0
        self.dot.configure(fg=WARN)
        self.status_label.configure(text="待机", fg=MUTED)
        self.root.withdraw()
        self.authorization_resetting = False
        self._show_login()

    @staticmethod
    def _label_button(
        parent: tk.Misc,
        text: str,
        command,
        *,
        width: int = 38,
        bg: str = SURFACE,
        hover: str = PANEL_2,
        fg: str = MUTED,
        font=("Microsoft YaHei UI", 10),
    ) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            width=1,
            height=1,
            anchor="center",
            cursor="hand2",
            font=font,
        )
        label.configure(padx=max(4, (width - 12) // 2))
        label.bind("<Enter>", lambda _event: label.configure(bg=hover, fg=TEXT))
        label.bind("<Leave>", lambda _event: label.configure(bg=bg, fg=fg))
        label.bind("<Button-1>", lambda _event: command())
        return label

    def _bind_drag(self, widget: tk.Misc, window: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", lambda event: self._drag_start(event, window))
        widget.bind("<B1-Motion>", lambda event: self._drag_move(event, window))
        widget.bind("<Double-Button-1>", lambda _event: self._toggle_maximize(window))

    @staticmethod
    def _win32_root_handle(window: tk.Misc) -> int:
        """Resolve Tk's client HWND to the real native top-level window."""
        if sys.platform != "win32" or not window.winfo_exists():
            return 0
        import ctypes
        from ctypes import wintypes

        window.update_idletasks()
        client_handle = int(window.winfo_id())
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = (wintypes.HWND, wintypes.UINT)
        get_ancestor.restype = wintypes.HWND
        root_handle = get_ancestor(client_handle, 2)  # GA_ROOT
        return int(root_handle or client_handle)

    @staticmethod
    def _win32_extended_style(hwnd: int) -> int | None:
        if sys.platform != "win32" or not hwnd:
            return None
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            get_style = user32.GetWindowLongPtrW
            get_style.restype = ctypes.c_ssize_t
        else:
            get_style = user32.GetWindowLongW
            get_style.restype = wintypes.LONG
        get_style.argtypes = (wintypes.HWND, ctypes.c_int)
        ctypes.set_last_error(0)
        value = int(get_style(hwnd, -20))
        if value == 0 and ctypes.get_last_error():
            return None
        return value & 0xFFFFFFFF

    @staticmethod
    def _set_win32_extended_style(hwnd: int, style: int) -> bool:
        if sys.platform != "win32" or not hwnd:
            return False
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        pointer_sized = ctypes.sizeof(ctypes.c_void_p) == 8
        if pointer_sized:
            set_style = user32.SetWindowLongPtrW
            set_style.restype = ctypes.c_ssize_t
            encoded_style = int(style) & 0xFFFFFFFF
        else:
            set_style = user32.SetWindowLongW
            set_style.restype = wintypes.LONG
            encoded_style = ctypes.c_long(int(style) & 0xFFFFFFFF).value
        set_style.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_ssize_t if pointer_sized else wintypes.LONG,
        )
        ctypes.set_last_error(0)
        previous = int(set_style(hwnd, -20, encoded_style))
        if previous == 0 and ctypes.get_last_error():
            return False

        set_position = user32.SetWindowPos
        set_position.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        set_position.restype = wintypes.BOOL
        if not set_position(hwnd, 0, 0, 0, 0, 0, 0x0037):
            return False
        return DpsWindow._win32_extended_style(hwnd) == (int(style) & 0xFFFFFFFF)

    @staticmethod
    def _set_window_topmost_noactivate(window: tk.Misc, topmost: bool) -> bool:
        """Change native z-order without activating an auxiliary window."""
        if sys.platform != "win32" or not window.winfo_exists():
            return False
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = DpsWindow._win32_root_handle(window)
            if not hwnd:
                return False
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            set_position = user32.SetWindowPos
            set_position.argtypes = (
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            )
            set_position.restype = wintypes.BOOL
            insert_after = -1 if topmost else -2  # HWND_TOPMOST / HWND_NOTOPMOST
            flags = 0x0013  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            return bool(set_position(hwnd, insert_after, 0, 0, 0, 0, flags))
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            return False

    def _set_window_click_through(self, window: tk.Misc, locked: bool) -> bool:
        if sys.platform != "win32" or not window.winfo_exists():
            return not locked
        try:
            hwnd = self._win32_root_handle(window)
            current = self._win32_extended_style(hwnd)
            if not hwnd or current is None:
                return False
            style_key = int(hwnd)
            if locked:
                original = self.window_lock_original_styles.setdefault(
                    style_key, current
                )
            else:
                original = self.window_lock_original_styles.pop(style_key, None)
            updated = window_exstyle_for_lock(current, locked, original)
            applied = updated == current or self._set_win32_extended_style(hwnd, updated)
            verified = self._win32_extended_style(hwnd)
            if not locked:
                self.window_lock_original_styles.clear()
            if verified is None:
                return False
            managed_bits = WINDOW_EXSTYLE_TRANSPARENT | WINDOW_EXSTYLE_LAYERED
            expected_bits = updated & managed_bits
            return bool(applied and (verified & managed_bits) == expected_bits)
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            return False

    def _destroy_unlock_window(self) -> None:
        if self.unlock_window is not None and self.unlock_window.winfo_exists():
            self.unlock_window.destroy()
        self.unlock_window = None
        self.unlock_button = None

    def _preferred_main_topmost(self) -> bool:
        restore = getattr(self, "window_lock_topmost_restore", None)
        if restore is not None:
            return bool(restore)
        return bool(self.root.attributes("-topmost"))

    def _set_main_topmost(self, value: bool) -> None:
        value = bool(value)
        self.root.attributes("-topmost", value)
        window = getattr(self, "main_content_overlay_window", None)
        if window is not None and window.winfo_exists():
            if self._set_window_topmost_noactivate(window, value):
                self.main_content_overlay_topmost = value

    def _force_main_topmost_while_locked(self) -> None:
        if self.window_lock_topmost_restore is None:
            self.window_lock_topmost_restore = bool(
                self.root.attributes("-topmost")
            )
        self.config["topmost"] = self.window_lock_topmost_restore
        self._set_main_topmost(True)

    def _restore_main_topmost_after_unlock(self) -> bool:
        restore = self.window_lock_topmost_restore
        if restore is None:
            return False
        self._set_main_topmost(restore)
        self.config["topmost"] = bool(restore)
        self.window_lock_topmost_restore = None
        return True

    def _sync_unlock_window_position(self) -> None:
        window = self.unlock_window
        if (
            not self.window_locked
            or window is None
            or not window.winfo_exists()
            or self.root.state() != "normal"
        ):
            return
        self.root.update_idletasks()
        if self.compact_mode:
            width, height = 28, 28
            x = self.root.winfo_rootx() + self.root.winfo_width() - 63
            y = self.root.winfo_rooty() + 2
        elif self.lock_button.winfo_ismapped():
            width = max(27, self.lock_button.winfo_width())
            height = max(24, self.lock_button.winfo_height())
            x = self.lock_button.winfo_rootx()
            y = self.lock_button.winfo_rooty()
        else:
            width, height = 27, 28
            x = self.root.winfo_rootx() + self.root.winfo_width() - 135
            y = self.root.winfo_rooty()
        geometry = f"{width}x{height}{x:+d}{y:+d}"
        if window.geometry() != geometry:
            window.geometry(geometry)

    def _show_unlock_window(self) -> None:
        if self.root.state() != "normal":
            return
        if self.unlock_window is None or not self.unlock_window.winfo_exists():
            window = tk.Toplevel(self.root)
            window.withdraw()
            self.unlock_window = window
            window.title("解锁 DPS 窗口")
            window.configure(bg=BORDER)
            window.overrideredirect(True)
            window.attributes("-alpha", 1.0)
            unlock_icon = self.icons.toolbar("unlock", 16, ACCENT)
            unlock_hover_icon = self.icons.toolbar("unlock", 16, TEXT)
            label = tk.Label(
                window,
                image=unlock_icon,
                bg=SURFACE,
                cursor="hand2",
                bd=0,
            )
            label.image = unlock_icon
            self.unlock_button = label
            label.pack(fill="both", expand=True, padx=1, pady=1)
            label.bind(
                "<Enter>",
                lambda _event: label.configure(
                    bg=PANEL_2, image=unlock_hover_icon
                ),
            )
            label.bind(
                "<Leave>",
                lambda _event: label.configure(bg=SURFACE, image=unlock_icon),
            )
            label.bind("<Button-1>", lambda _event: self.toggle_window_lock())
            window.update_idletasks()
            hwnd = self._win32_root_handle(window)
            style = self._win32_extended_style(hwnd)
            if style is not None:
                self._set_win32_extended_style(
                    hwnd,
                    style
                    | WINDOW_EXSTYLE_TOOLWINDOW
                    | WINDOW_EXSTYLE_NOACTIVATE,
                )
        self._sync_unlock_window_position()
        if self.unlock_window.state() != "normal":
            self.unlock_window.deiconify()
        self._set_window_topmost_noactivate(self.unlock_window, True)

    def _apply_window_lock_state(self) -> None:
        if self.closing or not self.root.winfo_exists():
            return
        restored_after_unlock = False
        if self.window_locked:
            self._force_main_topmost_while_locked()
        else:
            restored_after_unlock = self._restore_main_topmost_after_unlock()
        # Tk may rewrite WS_EX_LAYERED when applying alpha. Apply opacity first,
        # then make click-through the final native style operation.
        self._apply_main_transparency()
        click_through_applied = self._set_window_click_through(
            self.root, self.window_locked
        )
        if self.window_locked:
            self._show_unlock_window()
            if not click_through_applied:
                self.root.after(
                    40,
                    lambda: self.window_locked
                    and self._set_window_click_through(self.root, True),
                )
        else:
            self._destroy_unlock_window()
            if restored_after_unlock and self.root.state() == "normal":
                self.root.lift()
                self.root.focus_force()
        self._sync_action_buttons()
        self._draw_main_header()

    def toggle_window_lock(self) -> None:
        if self.closing:
            return
        self.window_locked = not self.window_locked
        self.config["window_locked"] = self.window_locked
        self.drag_state.pop(id(self.root), None)
        self.resize_state.pop(id(self.root), None)
        self._apply_window_lock_state()
        save_config(self.config)

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=BG, bd=0)
        shell.pack(fill="both", expand=True)
        self.body = tk.Frame(shell, bg=BG, bd=0)
        self.body.pack(fill="both", expand=True)

        self.titlebar = tk.Frame(self.body, bg=BG, height=44)
        self.titlebar.pack(fill="x", padx=8, pady=(2, 0))
        self.titlebar.pack_propagate(False)
        self._bind_drag(self.titlebar, self.root)

        logo_image = self.icons.app_logo(28)
        logo = tk.Label(
            self.titlebar, image=logo_image, bg=BG, bd=0
        )
        logo.image = logo_image
        logo.pack(side="left", padx=(2, 7), pady=7)
        self._bind_drag(logo, self.root)

        self.dot = tk.Label(
            self.titlebar,
            text="●",
            bg=BG,
            fg=WARN,
            font=self._ui_font("micro"),
        )
        self.dot.pack(side="left", padx=(0, 5))
        self._bind_drag(self.dot, self.root)
        self.time_value = tk.Label(
            self.titlebar,
            text="00:00",
            bg=BG,
            fg=TEXT,
            font=self._ui_font("number_large"),
        )
        self.time_value.pack(side="left")
        self._bind_drag(self.time_value, self.root)

        self.group_label = tk.Label(
            self.titlebar,
            text="QQ群：1094925831",
            bg=BG,
            fg=MUTED,
            font=self._ui_font("small"),
        )
        self.group_label.pack(side="left", padx=(18, 0))
        self._bind_drag(self.group_label, self.root)

        actions = tk.Frame(self.titlebar, bg=BG)
        actions.pack(side="right", fill="y")
        self.share_button = self._main_icon_button(
            actions, "share", "分享", self._share_current
        )
        self.reset_button = self._main_icon_button(
            actions, "reset", "清空", self.reset
        )
        self.privacy_button = self._main_icon_button(
            actions,
            "eye_off" if self.hide_names else "eye",
            "显示名称" if self.hide_names else "隐藏名称",
            self.toggle_names,
        )
        self.lock_button = self._main_icon_button(
            actions,
            "lock" if self.window_locked else "unlock",
            "解锁" if self.window_locked else "锁定",
            self.toggle_window_lock,
        )
        self.main_menu_button = self._main_icon_button(
            actions, "menu", "主菜单", self.show_main_menu
        )
        self.compact_button = self._main_icon_button(
            actions, "compact", "迷你模式", self.toggle_compact_mode
        )
        self.min_button = self._main_icon_button(
            actions, "minimize", "最小化到托盘", self.minimize
        )
        for button in (
            self.share_button,
            self.reset_button,
            self.privacy_button,
            self.lock_button,
            self.main_menu_button,
            self.compact_button,
            self.min_button,
        ):
            button.pack(side="left", fill="y", padx=1)

        # These legacy references are intentionally kept nullable because the
        # old chrome actions no longer appear in the redesigned meter.
        self.close_button = None
        self.max_button = None
        self.feedback_button = None
        self.history_button = None
        self.pin_button = None
        self.toolbar = tk.Frame(self.body, bg=BG)
        self.membership_label = None
        self.expiry_label = None
        self.opacity_scale = None
        self.opacity_value_label = None

        self.status_label = tk.Label(
            self.titlebar,
            text="准备中",
            bg=BG,
            fg=MUTED,
            font=self._ui_font("micro"),
        )

        self._sync_action_buttons()
        self._sync_expiry_label()

        self.summary = tk.Frame(self.body, bg=BG, height=76)
        self.summary.pack(fill="x", padx=8, pady=(0, 2))
        self.summary.pack_propagate(False)
        self._bind_drag(self.summary, self.root)

        metric_frame = tk.Frame(self.summary, bg=BG, height=34)
        metric_frame.pack(fill="x")
        metric_frame.pack_propagate(False)
        self._bind_drag(metric_frame, self.root)
        total_group = tk.Frame(metric_frame, bg=BG)
        total_group.pack(side="left", fill="both", expand=True, padx=(28, 8))
        self._bind_drag(total_group, self.root)
        total_caption = tk.Label(
            total_group,
            text="总伤害",
            bg=BG,
            fg=TEXT,
            font=self._ui_font("body"),
        )
        total_caption.pack(side="left", padx=(0, 8))
        self._bind_drag(total_caption, self.root)
        self.total_value = tk.Label(
            total_group,
            text="0",
            bg=BG,
            fg=ACCENT,
            font=self._ui_font("number_large"),
        )
        self.total_value.pack(side="left")
        self._bind_drag(self.total_value, self.root)
        self.dps_group = tk.Frame(metric_frame, bg=BG)
        self.dps_group.pack(side="left", fill="both", expand=True, padx=(8, 24))
        self._bind_drag(self.dps_group, self.root)
        self.dps_caption = tk.Label(
            self.dps_group,
            text="总 DPS",
            bg=BG,
            fg=TEXT,
            font=self._ui_font("body"),
        )
        self.dps_caption.pack(side="left", padx=(0, 8))
        self._bind_drag(self.dps_caption, self.root)
        self.dps_value = tk.Label(
            self.dps_group,
            text="0",
            bg=BG,
            fg=ACCENT,
            font=self._ui_font("number_large"),
        )
        self.dps_value.pack(side="left")
        self._bind_drag(self.dps_value, self.root)

        self.monster_hp_canvas = tk.Canvas(
            self.summary,
            height=36,
            bg=BG,
            bd=0,
            highlightthickness=0,
        )
        self.monster_hp_canvas.pack(fill="x", pady=(1, 0))
        self.monster_hp_canvas.bind(
            "<Configure>", lambda _event: self._draw_monster_hp()
        )
        self._bind_drag(self.monster_hp_canvas, self.root)

        self.table_panel = tk.Frame(
            self.body,
            bg=BG,
            highlightthickness=0,
        )
        self.table_panel.pack(fill="both", expand=True, padx=8)
        self.header_canvas = tk.Canvas(
            self.table_panel,
            height=28,
            bg=BG,
            bd=0,
            highlightthickness=0,
        )
        self.header_canvas.pack(fill="x")
        self.header_canvas.bind("<Configure>", lambda _event: self._draw_main_header())
        self.header_canvas.bind(
            "<ButtonPress-1>", lambda event: self._drag_start(event, self.root)
        )
        self.header_canvas.bind(
            "<B1-Motion>", lambda event: self._drag_move(event, self.root)
        )
        self.header_canvas.bind(
            "<Double-Button-1>", self._restore_compact_from_header
        )
        self.rows_canvas = tk.Canvas(
            self.table_panel,
            bg=BG,
            bd=0,
            highlightthickness=0,
            yscrollincrement=1,
        )
        self.rows_canvas.pack(fill="both", expand=True)
        self.rows_canvas.bind("<MouseWheel>", self._scroll_main)
        self.rows_canvas.bind("<Button-4>", self._scroll_main)
        self.rows_canvas.bind("<Button-5>", self._scroll_main)
        self.rows_canvas.bind("<Configure>", lambda _event: self._draw_main_rows())
        # On Windows, wheel messages for an inactive borderless window can land
        # on the top-level bind tag instead of the canvas widget itself.
        self.root.bind("<MouseWheel>", self._scroll_main, add="+")
        self.root.bind("<Button-4>", self._scroll_main, add="+")
        self.root.bind("<Button-5>", self._scroll_main, add="+")
        self.root.bind("<Map>", self._main_window_map_state, add="+")
        self.root.bind("<Unmap>", self._main_window_map_state, add="+")

        self.footer = tk.Frame(self.body, bg=BG, height=38)
        self.footer.pack(
            side="bottom",
            fill="x",
            padx=8,
            pady=(1, 1),
            before=self.table_panel,
        )
        self.footer.pack_propagate(False)
        for index, (caption, enabled) in enumerate(
            (("DPS", True), ("HPS", False), ("TANK", False), ("BOSS", False))
        ):
            tab = tk.Frame(self.footer, bg=BG, width=62, height=36)
            tab.pack(side="left", padx=(0 if index == 0 else 6, 0))
            tab.pack_propagate(False)
            tk.Label(
                tab,
                text=caption,
                bg=BG,
                fg=ACCENT if enabled else MUTED,
                font=self._ui_font("strong" if enabled else "body"),
            ).pack(fill="both", expand=True)
            if enabled:
                tk.Frame(tab, bg=ACCENT, height=2).pack(side="bottom", fill="x")
        self.footer_brand_label = tk.Label(
            self.footer,
            text=f"{UI_BRAND} v{APP_VERSION}",
            bg=BG,
            fg=MUTED,
            font=self._ui_font("small"),
        )
        self.footer_brand_label.pack(side="right", fill="y", padx=(8, 14))
        self._sync_action_buttons()
        self.resize_grip = tk.Label(
            self.body,
            text="◢",
            bg=BG,
            fg=SUBTLE,
            cursor="size_nw_se",
            font=self._ui_font("small"),
        )
        self.resize_grip.place(relx=1.0, rely=1.0, anchor="se")
        self.resize_grip.bind(
            "<ButtonPress-1>", lambda event: self._resize_start(event, self.root)
        )
        self.resize_grip.bind(
            "<B1-Motion>",
            lambda event: self._resize_move(
                event,
                self.root,
                MINI_MIN_WIDTH if self.compact_mode else MAIN_MIN_WIDTH,
                MINI_MIN_HEIGHT if self.compact_mode else MAIN_MIN_HEIGHT,
            ),
        )
        self.root.bind("<Configure>", self._main_window_configure, add="+")

    def _main_icon_button(
        self, parent: tk.Misc, icon_name: str, description: str, command
    ) -> tk.Label:
        image = self.icons.toolbar(icon_name, 18, TEXT)
        label = tk.Label(
            parent,
            image=image,
            bg=BG,
            width=34,
            height=34,
            bd=0,
            cursor="hand2",
        )
        label.image = image
        label._disabled = False
        label._description = description
        label._icon_name = icon_name
        label._normal_color = TEXT
        label._hovered = False
        label.bind(
            "<Enter>",
            lambda _event: self._main_icon_enter(label),
        )
        label.bind(
            "<Leave>",
            lambda _event: self._main_icon_leave(label),
        )
        label.bind(
            "<Button-1>",
            lambda _event: (
                None
                if getattr(label, "_disabled", False)
                else (self._hide_main_tooltip(), command())
            ),
        )
        return label

    def _render_main_icon_button(
        self, button: tk.Label, color: str, background: str = BG
    ) -> None:
        image = self.icons.toolbar(button._icon_name, 18, color)
        button.configure(image=image, bg=background)
        button.image = image

    def _sync_main_icon_button_visual(self, button: tk.Label) -> None:
        hovered = bool(getattr(button, "_hovered", False)) and not bool(
            getattr(button, "_disabled", False)
        )
        self._render_main_icon_button(
            button,
            ACCENT if hovered else button._normal_color,
            PANEL_2 if hovered else BG,
        )

    def _main_icon_enter(self, button: tk.Label) -> None:
        button._hovered = True
        self._sync_main_icon_button_visual(button)
        self._hide_main_tooltip()

    def _main_icon_leave(self, button: tk.Label) -> None:
        button._hovered = False
        self._hide_main_tooltip()
        self._sync_main_icon_button_visual(button)

    def _show_main_tooltip(self, button: tk.Label) -> None:
        # Icon hover is rendered in-place.  Extra tooltip windows are avoided
        # so the meter never steals focus while the player is in combat.
        self.main_tooltip_after_id = None

    def _hide_main_tooltip(self) -> None:
        if self.main_tooltip_after_id is not None:
            try:
                self.root.after_cancel(self.main_tooltip_after_id)
            except tk.TclError:
                pass
            self.main_tooltip_after_id = None
        if self.main_tooltip is not None and self.main_tooltip.winfo_exists():
            self.main_tooltip.destroy()
        self.main_tooltip = None

    def _destroy_main_content_overlay(self) -> None:
        if self.main_content_overlay_sync_after_id is not None:
            try:
                self.root.after_cancel(self.main_content_overlay_sync_after_id)
            except tk.TclError:
                pass
            self.main_content_overlay_sync_after_id = None
        window = self.main_content_overlay_window
        if window is not None and window.winfo_exists():
            window.destroy()
        self.main_content_overlay_window = None
        self.main_content_overlay_header = None
        self.main_content_overlay_rows = None
        self.main_content_overlay_dps = None
        self.main_content_overlay_root_geometry = None
        self.main_content_overlay_click_through_ready = False
        self.main_content_overlay_topmost = None

    def _ensure_main_content_overlay(self) -> bool:
        if not self.main_content_overlay_supported:
            return False
        window = self.main_content_overlay_window
        if window is not None and window.winfo_exists():
            return True
        try:
            window = tk.Toplevel(self.root)
            window.withdraw()
            window.title(f"{UI_BRAND} · 前景内容")
            window.configure(bg=MAIN_CONTENT_OVERLAY_KEY)
            window.overrideredirect(True)
            window.attributes("-alpha", 1.0)
            window.attributes("-transparentcolor", MAIN_CONTENT_OVERLAY_KEY)
            self.main_content_overlay_window = window
            self.main_content_overlay_rows = tk.Canvas(
                window,
                bg=MAIN_CONTENT_OVERLAY_KEY,
                bd=0,
                highlightthickness=0,
                yscrollincrement=1,
            )
            self.main_content_overlay_dps = tk.Canvas(
                window,
                bg=MAIN_CONTENT_OVERLAY_KEY,
                bd=0,
                highlightthickness=0,
            )
            window.update_idletasks()
            hwnd = self._win32_root_handle(window)
            style = self._win32_extended_style(hwnd)
            if style is None or not self._set_win32_extended_style(
                hwnd,
                style
                | WINDOW_EXSTYLE_TRANSPARENT
                | WINDOW_EXSTYLE_TOOLWINDOW
                | WINDOW_EXSTYLE_LAYERED
                | WINDOW_EXSTYLE_NOACTIVATE,
            ):
                raise tk.TclError("could not configure the content overlay")
            self.main_content_overlay_click_through_ready = True
            return True
        except (AttributeError, OSError, tk.TclError):
            self.main_content_overlay_supported = False
            self._destroy_main_content_overlay()
            return False

    def _schedule_main_content_overlay_sync(self) -> None:
        if self.closing or not self.main_content_overlay_supported:
            return
        if self.main_content_overlay_sync_after_id is not None:
            return
        try:
            self.main_content_overlay_sync_after_id = self.root.after(
                16,
                self._sync_main_content_overlay
            )
        except tk.TclError:
            self.main_content_overlay_sync_after_id = None

    @staticmethod
    def _place_overlay_canvas(
        canvas: tk.Canvas,
        source: tk.Misc,
        root_x: int,
        root_y: int,
    ) -> bool:
        if not source.winfo_ismapped():
            canvas.place_forget()
            return False
        width = max(1, source.winfo_width())
        height = max(1, source.winfo_height())
        canvas.place(
            x=source.winfo_rootx() - root_x,
            y=source.winfo_rooty() - root_y,
            width=width,
            height=height,
        )
        return True

    def _sync_main_content_overlay(self) -> None:
        self.main_content_overlay_sync_after_id = None
        if self.closing or not self.root.winfo_exists():
            self._destroy_main_content_overlay()
            return
        if self.window_alpha >= 0.999 or self.root.state() != "normal":
            self.main_content_overlay_root_geometry = None
            window = self.main_content_overlay_window
            if window is not None and window.winfo_exists():
                window.withdraw()
            return
        if not self._ensure_main_content_overlay():
            return
        window = self.main_content_overlay_window
        rows_canvas = self.main_content_overlay_rows
        dps_canvas = self.main_content_overlay_dps
        if window is None or rows_canvas is None or dps_canvas is None:
            return
        try:
            self.root.update_idletasks()
            root_x = self.root.winfo_rootx()
            root_y = self.root.winfo_rooty()
            root_width = max(1, self.root.winfo_width())
            root_height = max(1, self.root.winfo_height())
            geometry_signature = (
                root_x,
                root_y,
                root_width,
                root_height,
            )
            desired_geometry = (
                f"{root_width}x{root_height}{root_x:+d}{root_y:+d}"
            )
            if geometry_signature != self.main_content_overlay_root_geometry:
                window.geometry(desired_geometry)
                self.main_content_overlay_root_geometry = geometry_signature
            overlay_topmost = bool(self.root.attributes("-topmost"))
            if overlay_topmost != self.main_content_overlay_topmost:
                if self._set_window_topmost_noactivate(window, overlay_topmost):
                    self.main_content_overlay_topmost = overlay_topmost
            rows_visible = self._place_overlay_canvas(
                rows_canvas, self.rows_canvas, root_x, root_y
            )
            dps_visible = (
                not self.compact_mode
                and self._place_overlay_canvas(
                    dps_canvas, self.dps_value, root_x, root_y
                )
            )
            if not dps_visible:
                dps_canvas.place_forget()
            window.update_idletasks()
            if rows_visible:
                self._draw_main_rows_on_canvas(
                    rows_canvas,
                    update_scroll_state=False,
                    draw_empty_state=False,
                )
            self._draw_main_content_overlay_dps()
            if window.state() != "normal":
                window.deiconify()
        except tk.TclError:
            if window.winfo_exists():
                window.withdraw()

    def _draw_main_content_overlay_dps(self) -> None:
        canvas = self.main_content_overlay_dps
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        if self.compact_mode or not self.dps_value.winfo_ismapped():
            return
        canvas.create_text(
            max(1, canvas.winfo_width()) // 2,
            max(1, canvas.winfo_height()) // 2,
            text=self.dps_value.cget("text"),
            fill=ACCENT,
            anchor="center",
            font=self._ui_font("number_large"),
        )

    def _main_window_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        self._sync_titlebar_density(int(event.width))
        geometry_signature = (
            self.root.winfo_rootx(),
            self.root.winfo_rooty(),
            int(event.width),
            int(event.height),
        )
        if geometry_signature != self.main_content_overlay_root_geometry:
            self._schedule_main_content_overlay_sync()
        if self.window_locked:
            self._sync_unlock_window_position()

    def _main_window_map_state(self, event) -> None:
        if event.widget is not self.root:
            return
        self.main_content_overlay_root_geometry = None
        self._schedule_main_content_overlay_sync()

    def _sync_titlebar_density(self, width: int | None = None) -> None:
        if not hasattr(self, "group_label") or not self.group_label.winfo_exists():
            return
        width = int(width if width is not None else self.root.winfo_width())
        should_show = not self.compact_mode and width >= 520
        if should_show and not self.group_label.winfo_manager():
            self.group_label.pack(side="left", padx=(18, 0))
        elif not should_show and self.group_label.winfo_manager():
            self.group_label.pack_forget()
        if hasattr(self, "footer_brand_label"):
            show_brand = not self.compact_mode and width >= 560
            if show_brand and not self.footer_brand_label.winfo_manager():
                self.footer_brand_label.pack(
                    side="right", fill="y", padx=(8, 14)
                )
            elif not show_brand and self.footer_brand_label.winfo_manager():
                self.footer_brand_label.pack_forget()

    def _apply_layout_mode(self) -> None:
        self._hide_main_tooltip()
        if self.compact_mode:
            for widget in (self.titlebar, self.summary, self.footer):
                if widget.winfo_manager():
                    widget.pack_forget()
            if not self.table_panel.winfo_manager():
                self.table_panel.pack(fill="both", expand=True, padx=2, pady=2)
            else:
                self.table_panel.pack_configure(padx=2, pady=2)
            self.resize_grip.place_forget()
            self.root.minsize(MINI_MIN_WIDTH, MINI_MIN_HEIGHT)
            self.config["compact_layout_version"] = 3
            self._dismiss_compact_auxiliary_windows()
        else:
            if not self.titlebar.winfo_manager():
                self.titlebar.pack(
                    fill="x", padx=8, pady=(2, 0), before=self.table_panel
                )
            if not self.summary.winfo_manager():
                self.summary.pack(
                    fill="x", padx=8, pady=(0, 2), before=self.table_panel
                )
            if not self.table_panel.winfo_manager():
                self.table_panel.pack(fill="both", expand=True, padx=8)
            else:
                self.table_panel.pack_configure(padx=8, pady=0)
            if not self.footer.winfo_manager():
                self.footer.pack(
                    side="bottom",
                    fill="x",
                    padx=8,
                    pady=(1, 1),
                    before=self.table_panel,
                )
            self.resize_grip.place(relx=1.0, rely=1.0, anchor="se")
            self.root.minsize(MAIN_MIN_WIDTH, MAIN_MIN_HEIGHT)
        self._draw_main_header()
        self._draw_main_rows()
        self._sync_titlebar_density()
        self._sync_action_buttons()
        self.root.after(10, self._apply_main_transparency)

    def _dismiss_compact_auxiliary_windows(self) -> None:
        self._close_notice_window()
        for attribute in (
            "skill_window",
            "history_window",
            "feedback_window",
            "update_window",
        ):
            window = getattr(self, attribute, None)
            if window is not None and window.winfo_exists():
                window.withdraw()

    def _remember_root_geometry(self) -> None:
        geometry = self.restore_geometry.get(id(self.root), self.root.geometry())
        key = "compact_geometry" if self.compact_mode else "geometry"
        self.config[key] = geometry
        self.config["compact_mode"] = self.compact_mode
        self.config["window_locked"] = self.window_locked

    def _sync_compact_geometry_width(self) -> None:
        fallback_x = self.root.winfo_x() if self.root.winfo_exists() else 32
        fallback_y = self.root.winfo_y() if self.root.winfo_exists() else 120
        geometry = self._adaptive_compact_geometry(
            self.config.get("compact_geometry", ""), fallback_x, fallback_y
        )
        self.config["compact_geometry"] = geometry
        if self.compact_mode and self.root.winfo_exists():
            self.root.geometry(
                self._visible_geometry(
                    geometry, self._compact_target_width(), MINI_DEFAULT_HEIGHT
                )
            )
            self.root.update_idletasks()

    def toggle_compact_mode(self) -> None:
        if self.closing or self.window_locked:
            return
        root_key = id(self.root)
        if root_key in self.restore_geometry:
            self.root.geometry(self.restore_geometry.pop(root_key))
            self.root.update_idletasks()

        self._remember_root_geometry()
        current_x = self.root.winfo_x()
        current_y = self.root.winfo_y()
        compact_layout_is_current = int(
            self.config.get("compact_layout_version", 0)
        ) >= 3

        self.compact_mode = not self.compact_mode
        self.config["compact_mode"] = self.compact_mode
        self._apply_layout_mode()

        geometry_key = "compact_geometry" if self.compact_mode else "geometry"
        target_geometry = str(self.config.get(geometry_key, ""))
        if self.compact_mode and not compact_layout_is_current:
            target_geometry = ""
        if self.compact_mode:
            target_geometry = self._adaptive_compact_geometry(
                target_geometry, current_x, current_y
            )
        match = re.fullmatch(
            r"(\d+)x(\d+)([+-]\d+[+-]\d+)", target_geometry
        )
        if match:
            width, height, suffix = match.groups()
            minimum_width = MINI_MIN_WIDTH if self.compact_mode else MAIN_MIN_WIDTH
            minimum_height = (
                MINI_MIN_HEIGHT if self.compact_mode else MAIN_MIN_HEIGHT
            )
            if self.compact_mode:
                width = str(self._compact_target_width())
            target_geometry = (
                f"{max(minimum_width, int(width))}x"
                f"{max(minimum_height, int(height))}{suffix}"
            )
        else:
            width = MINI_DEFAULT_WIDTH if self.compact_mode else max(
                MAIN_MIN_WIDTH, self.root.winfo_width()
            )
            height = MINI_DEFAULT_HEIGHT if self.compact_mode else max(
                MAIN_MIN_HEIGHT, self.root.winfo_height()
            )
            target_geometry = f"{width}x{height}{current_x:+d}{current_y:+d}"
        self.root.geometry(
            self._visible_geometry(
                target_geometry,
                MINI_DEFAULT_WIDTH if self.compact_mode else 590,
                MINI_DEFAULT_HEIGHT if self.compact_mode else 400,
            )
        )
        self.root.update_idletasks()
        self._remember_root_geometry()
        save_config(self.config)

    def _metric(self, parent: tk.Misc, caption: str, value: str, column: int) -> tk.Label:
        frame = tk.Frame(parent, bg=BG)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 0))
        parent.grid_columnconfigure(column, weight=1)
        tk.Label(
            frame,
            text=caption,
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 7, "bold"),
        ).pack(side="left", fill="y", padx=(5, 5))
        label = tk.Label(
            frame,
            text=value,
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI", 9, "bold"),
        )
        label.pack(side="left", fill="both", expand=True)
        return label

    @staticmethod
    def _action_button(parent: tk.Misc, text: str, command) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            bg=PANEL_2,
            fg=MUTED,
            padx=4,
            pady=2,
            cursor="hand2",
            font=("Microsoft YaHei UI", 7, "bold"),
        )
        label._disabled = False
        label.bind(
            "<Enter>",
            lambda _event: (
                None
                if getattr(label, "_disabled", False)
                else label.configure(
                    bg=blend_color(PANEL_2, ACCENT, 0.13), fg=TEXT
                )
            ),
        )
        label.bind(
            "<Leave>",
            lambda _event: label.configure(
                bg=PANEL_2,
                fg=("#4e5862" if getattr(label, "_disabled", False) else MUTED),
            ),
        )
        label.bind(
            "<Button-1>",
            lambda _event: (
                None if getattr(label, "_disabled", False) else command()
            ),
        )
        return label

    def show_feedback(self, selected_record_id: str | None = None) -> None:
        if self.compact_mode:
            return
        selected_record_id = str(selected_record_id or "").strip()
        if (
            selected_record_id
            and self.feedback_window is not None
            and self.feedback_window.winfo_exists()
            and selected_record_id not in self.feedback_record_ids.values()
        ):
            self._close_feedback_window()
        if self.feedback_window is None or not self.feedback_window.winfo_exists():
            self._build_feedback_window(selected_record_id or None)
        if selected_record_id and self.feedback_record_var is not None:
            selected_label = next(
                (
                    label
                    for label, encounter_id in self.feedback_record_ids.items()
                    if encounter_id == selected_record_id
                ),
                "",
            )
            if selected_label:
                self.feedback_record_var.set(selected_label)
        self.tray_feedback_hidden = False
        self.feedback_window.deiconify()
        feedback_parent = (
            self.history_window
            if (
                selected_record_id
                and self.history_window is not None
                and self.history_window.winfo_exists()
                and self.history_window.state() == "normal"
            )
            else self.root
        )
        self.feedback_window.lift(feedback_parent)
        self.feedback_window.after_idle(
            lambda window=self.feedback_window, parent=feedback_parent: (
                window.lift(parent) if window.winfo_exists() else None
            )
        )
        self.feedback_window.focus_force()
        if self.feedback_content is not None:
            self.feedback_content.focus_set()

    def _build_feedback_window(self, selected_record_id: str | None = None) -> None:
        width, height = 500, 462
        feedback_parent = (
            self.history_window
            if (
                selected_record_id
                and self.history_window is not None
                and self.history_window.winfo_exists()
                and self.history_window.state() == "normal"
            )
            else self.root
        )
        x = max(0, feedback_parent.winfo_rootx() + 20)
        y = max(0, feedback_parent.winfo_rooty() + 20)
        window = tk.Toplevel(feedback_parent)
        self.feedback_window = window
        window.transient(feedback_parent)
        window.title(f"{APP_TITLE} · 问题反馈")
        window.configure(bg=BORDER)
        window.geometry(self._visible_geometry(f"{width}x{height}+{x}+{y}", width, height))
        window.resizable(False, False)
        window.attributes("-topmost", bool(self.root.attributes("-topmost")))
        window.attributes("-alpha", 1.0)
        window.overrideredirect(True)
        window.protocol("WM_DELETE_WINDOW", self._close_feedback_window)
        window.bind("<Escape>", lambda _event: self._close_feedback_window())

        shell = tk.Frame(window, bg=BORDER)
        shell.pack(fill="both", expand=True)
        body = tk.Frame(shell, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        titlebar = tk.Frame(body, bg=SURFACE, height=34)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        self._bind_drag(titlebar, window)
        logo_image = self.icons.app_logo(18)
        logo = tk.Label(titlebar, image=logo_image, bg=SURFACE, bd=0)
        logo.image = logo_image
        logo.pack(side="left", padx=(8, 3), pady=8)
        self._bind_drag(logo, window)
        title = tk.Label(
            titlebar,
            text="问题反馈",
            bg=SURFACE,
            fg=TEXT,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        title.pack(side="left")
        self._bind_drag(title, window)
        self._label_button(
            titlebar,
            "×",
            self._close_feedback_window,
            width=34,
            hover="#7f2d35",
            fg="#c5ccd3",
            font=("Segoe UI", 13),
        ).pack(side="right", fill="y")

        content = tk.Frame(body, bg=BG)
        content.pack(fill="both", expand=True, padx=22, pady=(16, 18))
        tk.Label(
            content,
            text="请描述出现问题前后的操作和实际表现，信息越具体越容易定位。",
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        ).pack(fill="x", pady=(0, 12))

        category_row = tk.Frame(content, bg=BG)
        category_row.pack(fill="x", pady=(0, 9))
        tk.Label(
            category_row,
            text="问题类型",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        self.feedback_category_var = tk.StringVar(
            master=window, value=FEEDBACK_CATEGORIES[0][0]
        )
        category_menu = tk.OptionMenu(
            category_row,
            self.feedback_category_var,
            *(label for label, _value in FEEDBACK_CATEGORIES),
        )
        category_menu.configure(
            bg=PANEL,
            fg=TEXT,
            activebackground=PANEL_2,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            width=14,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        )
        category_menu["menu"].configure(
            bg=PANEL,
            fg=TEXT,
            activebackground=PANEL_2,
            activeforeground=TEXT,
            font=("Microsoft YaHei UI", 9),
        )
        category_menu.pack(side="right")

        self._flush_combat_history()
        record_choices = self._feedback_record_choices(
            self._load_recent_history(500),
            self.model.combat_in_progress(),
            selected_record_id,
        )
        self.feedback_record_ids = dict(record_choices)
        default_record_label = record_choices[0][0]
        selected_record_label = next(
            (
                label
                for label, encounter_id in record_choices
                if encounter_id == selected_record_id
            ),
            "",
        )
        if selected_record_label:
            default_record_label = selected_record_label
        elif not self.model.combat_in_progress() and len(record_choices) > 1:
            default_record_label = record_choices[1][0]
        record_row = tk.Frame(content, bg=BG)
        record_row.pack(fill="x", pady=(0, 9))
        tk.Label(
            record_row,
            text="关联战斗",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        self.feedback_record_var = tk.StringVar(
            master=window, value=default_record_label
        )
        record_menu = tk.OptionMenu(
            record_row,
            self.feedback_record_var,
            *(label for label, _encounter_id in record_choices),
        )
        record_menu.configure(
            bg=PANEL,
            fg=TEXT,
            activebackground=PANEL_2,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            width=29,
            anchor="w",
            font=("Microsoft YaHei UI", 8),
        )
        record_menu["menu"].configure(
            bg=PANEL,
            fg=TEXT,
            activebackground=PANEL_2,
            activeforeground=TEXT,
            font=("Microsoft YaHei UI", 8),
        )
        record_menu.pack(side="right")

        tk.Label(
            content,
            text="问题描述",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(fill="x", pady=(0, 6))
        self.feedback_content = tk.Text(
            content,
            height=8,
            wrap="word",
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#315d52",
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            font=("Microsoft YaHei UI", 9),
        )
        self.feedback_content.pack(fill="both", expand=True)

        self.feedback_diagnostics_var = tk.BooleanVar(master=window, value=True)
        diagnostics_toggle = tk.Checkbutton(
            content,
            text="附带运行摘要",
            variable=self.feedback_diagnostics_var,
            bg=BG,
            fg=MUTED,
            activebackground=BG,
            activeforeground=TEXT,
            selectcolor=PANEL,
            cursor="hand2",
            bd=0,
            highlightthickness=0,
            font=("Microsoft YaHei UI", 8),
        )
        diagnostics_toggle.pack(anchor="w", pady=(8, 3))

        footer = tk.Frame(content, bg=BG, height=35)
        footer.pack(fill="x", pady=(3, 0))
        footer.pack_propagate(False)
        self.feedback_status_label = tk.Label(
            footer,
            text="",
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 8),
        )
        self.feedback_status_label.pack(side="left", fill="both", expand=True)
        self.feedback_submit_button = self._label_button(
            footer,
            "提交反馈",
            self._submit_feedback,
            width=88,
            bg=ACCENT,
            hover="#86edca",
            fg="#07110e",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.feedback_submit_button.pack(side="right", fill="y")
        window.after(20, lambda: self._apply_windows_style(window))

    def _feedback_character_name(self) -> str:
        if self.model.self_id is None:
            return ""
        return self.model.display_name(int(self.model.self_id or 0)).strip()[:48]

    @staticmethod
    def _feedback_record_choices(
        records: list[dict],
        current_active: bool,
        selected_record_id: str | None = None,
    ) -> list[tuple[str, str]]:
        current_label = "当前战斗（进行中）" if current_active else "当前状态（暂无战斗）"
        choices: list[tuple[str, str]] = [(current_label, "__current__")]
        used_labels = {current_label}
        selected_record_id = str(selected_record_id or "").strip()
        eligible_records = [
            record
            for record in records
            if isinstance(record, dict)
            and str(record.get("encounter_id", "")).strip()
        ]
        shown_records = eligible_records[:20]
        if selected_record_id and not any(
            str(record.get("encounter_id", "")).strip() == selected_record_id
            for record in shown_records
        ):
            selected_record = next(
                (
                    record
                    for record in eligible_records
                    if str(record.get("encounter_id", "")).strip()
                    == selected_record_id
                ),
                None,
            )
            if selected_record is not None:
                shown_records = [selected_record, *shown_records[:19]]
        for record in shown_records:
            if not isinstance(record, dict):
                continue
            encounter_id = str(record.get("encounter_id", "")).strip()
            if not encounter_id:
                continue
            monster = record.get("monster")
            monster_name = (
                str(monster.get("name", "")).strip()
                if isinstance(monster, dict)
                else ""
            ) or "未知目标"
            try:
                ended = dt.datetime.fromtimestamp(
                    float(record.get("ended_at_epoch", 0.0) or 0.0)
                ).strftime("%m-%d %H:%M")
            except (OSError, OverflowError, TypeError, ValueError):
                ended = "历史"
            label = (
                f"{ended}  {monster_name[:12]}  "
                f"{format_number(record.get('total_damage', 0))}"
            )
            if label in used_labels:
                label = f"{label} · {encounter_id[-6:]}"
            used_labels.add(label)
            choices.append((label, encounter_id))
        return choices

    @staticmethod
    def _feedback_history_summary(record: dict) -> dict[str, object]:
        monster_record = record.get("monster")
        participants = []
        for item in record.get("participants", [])[:MAX_PARTY_MEMBERS]:
            if not isinstance(item, dict):
                continue
            participants.append(
                {
                    "actor_id": str(item.get("actor_id", "")),
                    "name": str(item.get("name", "")),
                    "is_self": bool(item.get("is_self", False)),
                    "profession_id": item.get("profession_id"),
                    "damage": int(item.get("damage", 0) or 0),
                    "dps": float(item.get("dps", 0.0) or 0.0),
                    "share": float(item.get("share", 0.0) or 0.0),
                    "hits": int(item.get("hits", 0) or 0),
                    "max_hit": int(item.get("max_hit", 0) or 0),
                    "damage_hits": item.get("damage_hits"),
                    "critical_hits": item.get("critical_hits"),
                    "critical_rate": item.get("critical_rate"),
                    "deaths": int(item.get("deaths", 0) or 0),
                    "skills": [
                        dict(skill)
                        for skill in item.get("skills", [])
                        if isinstance(skill, dict)
                    ],
                    "targets": [
                        dict(target)
                        for target in item.get("targets", [])
                        if isinstance(target, dict)
                    ],
                }
            )
        return {
            "schema_version": record.get("schema_version"),
            "encounter_id": str(record.get("encounter_id", "")),
            "source": str(record.get("source", "")),
            "started_at": str(record.get("started_at", "")),
            "started_at_epoch": float(
                record.get("started_at_epoch", 0.0) or 0.0
            ),
            "ended_at": str(record.get("ended_at", "")),
            "ended_at_epoch": float(record.get("ended_at_epoch", 0.0) or 0.0),
            "saved_at": str(record.get("saved_at", "")),
            "saved_at_epoch": float(record.get("saved_at_epoch", 0.0) or 0.0),
            "archive_reason": str(record.get("archive_reason", "")),
            "duration_seconds": float(record.get("duration_seconds", 0.0) or 0.0),
            "total_damage": int(record.get("total_damage", 0) or 0),
            "team_dps": float(record.get("team_dps", 0.0) or 0.0),
            "team_size": int(record.get("team_size", 0) or 0),
            "target_filter": str(record.get("target_filter", "")),
            "favorite": bool(record.get("favorite", False)),
            "monster": (
                dict(monster_record) if isinstance(monster_record, dict) else None
            ),
            "targets": [
                dict(target)
                for target in record.get("targets", [])
                if isinstance(target, dict)
            ],
            "participants": participants,
            "damage_accounting": (
                dict(record.get("damage_accounting", {}))
                if isinstance(record.get("damage_accounting"), dict)
                else {}
            ),
            "capture_pipeline_at_archive": (
                dict(record.get("capture_pipeline_at_archive", {}))
                if isinstance(record.get("capture_pipeline_at_archive"), dict)
                else {}
            ),
        }

    @staticmethod
    def _recent_error_excerpt() -> dict[str, object]:
        path = APP_DIR / "dps_error.log"
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size - 8192))
                text = handle.read(8192).decode("utf-8", errors="replace")
            home = str(Path.home())
            if home:
                text = text.replace(home, "%USERPROFILE%")
            return {
                "updated_at": path.stat().st_mtime,
                "tail": text[-6000:],
            }
        except OSError:
            return {}

    def _collect_feedback_diagnostics(
        self, selected_record_id: str = "__current__"
    ) -> dict[str, object]:
        now = time.time()
        selected_record_id = str(selected_record_id or "__current__").strip()
        include_current_combat = selected_record_id == "__current__"
        participants = []
        for actor in self.model.current_stats()[:12]:
            skills = sorted(
                actor.skills.values(), key=lambda value: value.damage, reverse=True
            )[:8]
            participants.append(
                {
                    "actor_id": str(actor.actor_id),
                    "name": self.model.display_name(actor.actor_id),
                    "profession_id": self.model.actor_profession_id(actor.actor_id),
                    "damage": int(actor.damage),
                    "hits": int(actor.hits),
                    "damage_hits": actor.damage_hits,
                    "critical_hits": actor.critical_hits,
                    "critical_rate": (
                        actor.critical_hits / actor.damage_hits
                        if actor.damage_hits
                        and actor.critical_hits is not None
                        else None
                    ),
                    "deaths": int(
                        self.model.member_death_counts.get(actor.actor_id, 0)
                    ),
                    "skills": [
                        {
                            "skill_id": int(skill.skill_id),
                            "name": self.model.display_skill_name(
                                actor.actor_id, skill.skill_id
                            ),
                            "damage": int(skill.damage),
                            "hits": int(skill.hits),
                        }
                        for skill in skills
                    ],
                }
            )
        monster = self.model.current_monster()
        monster_summary = None
        if monster is not None:
            monster_summary = {
                "entity_id": str(monster.entity_id),
                "name": monster.name,
                "entity_type": monster.entity_type,
                "template_id": monster.template_id,
                "level": monster.level,
                "boss_type": monster.boss_type,
                "boss_rank": monster.boss_rank,
                "current_hp": monster.current_hp,
                "max_hp": monster.max_hp,
                "observed_max_hp": monster.observed_max_hp,
            }
        capture_files = []
        try:
            recent_logs = sorted(
                LOG_DIR.glob("network_*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:3]
            for path in recent_logs:
                stat = path.stat()
                capture_files.append(
                    {
                        "name": path.name,
                        "size": stat.st_size,
                        "updated_at": stat.st_mtime,
                    }
                )
        except OSError:
            capture_files = []
        recent_combats = [
            self._feedback_history_summary(record)
            for record in self._load_recent_history(20)
            if not include_current_combat
            and str(record.get("encounter_id", "")) == selected_record_id
        ][:1]
        current_combat = {
            "active": bool(self.model.active(now)),
            "duration_seconds": round(self.model.duration(now), 3),
            "end_reason": self.model.combat_end_reason,
            "participant_count": len(participants),
            "party_known": bool(self.model.party_known),
            "party_member_count": int(self.model.party_member_count),
            "encounter_target_ids": [
                str(entity_id)
                for entity_id in sorted(self.model.encounter_target_ids)
            ],
            "self_id": (
                str(self.model.self_id) if self.model.self_id is not None else ""
            ),
            "monster": monster_summary,
            "participants": participants,
            "team_damage_states": [
                {
                    "actor_id": str(actor_id),
                    "last_absolute": int(state.last_absolute),
                    "baseline_absolute": int(state.baseline_absolute),
                    "accepted_damage": int(state.accepted_damage),
                    "has_snapshot": bool(state.has_snapshot),
                    "server_time": int(state.server_time),
                }
                for actor_id, state in list(
                    self.model.team_damage_states.items()
                )[:MAX_PARTY_MEMBERS]
            ],
            "record_snapshot": (
                self._feedback_history_summary(current_record)
                if (
                    include_current_combat
                    and (current_record := self.model.build_combat_record("feedback"))
                    is not None
                )
                else None
            ),
        }
        return {
            "app_version": CLIENT_BUILD,
            "display_version": APP_VERSION,
            "runtime": "packaged" if IS_FROZEN else "source",
            "platform": sys.platform,
            "connected": bool(self.connected),
            "game_pid": int(self.game_pid or 0),
            "capture_started": bool(self.capture_started),
            "capture_pipeline": self.worker.diagnostic_snapshot(),
            "compact_mode": bool(self.compact_mode),
            "boss_only": bool(self.model.boss_only),
            "selected_record_id": selected_record_id,
            "combat": current_combat if include_current_combat else None,
            "recent_combats": recent_combats,
            "capture_files": capture_files,
            "last_error": self._recent_error_excerpt(),
        }

    def _submit_feedback(self) -> None:
        if self.feedback_submitting or self.feedback_content is None:
            return
        content = self.feedback_content.get("1.0", "end-1c").strip()
        if not content:
            if self.feedback_status_label is not None:
                self.feedback_status_label.configure(
                    text="请先填写问题描述。", fg=ERROR
                )
            return
        if len(content) > 2000:
            if self.feedback_status_label is not None:
                self.feedback_status_label.configure(
                    text="问题描述最多 2000 个字。", fg=ERROR
                )
            return
        selected_label = (
            self.feedback_category_var.get()
            if self.feedback_category_var is not None
            else FEEDBACK_CATEGORIES[0][0]
        )
        category = next(
            (
                value
                for label, value in FEEDBACK_CATEGORIES
                if label == selected_label
            ),
            "other",
        )
        selected_record_id = (
            self.feedback_record_ids.get(self.feedback_record_var.get(), "__current__")
            if self.feedback_record_var is not None
            else "__current__"
        )
        diagnostics = (
            self._collect_feedback_diagnostics(selected_record_id)
            if self.feedback_diagnostics_var is not None
            and self.feedback_diagnostics_var.get()
            else {}
        )
        character_name = self._feedback_character_name()
        self.feedback_submitting = True
        if self.feedback_status_label is not None:
            self.feedback_status_label.configure(text="正在提交…", fg=MUTED)
        if self.feedback_submit_button is not None:
            self.feedback_submit_button.configure(
                text="正在提交", bg=PANEL_2, fg=MUTED
            )

        def submit() -> None:
            try:
                result = self.licensing.submit_feedback(
                    category=category,
                    content=content,
                    character_name=character_name,
                    diagnostics=diagnostics,
                )
                payload = {
                    "accepted": result.accepted,
                    "feedback_id": result.feedback_id,
                    "message": result.message,
                }
            except LicensingConnectionError as exc:
                payload = {"accepted": False, "message": str(exc)}
            except Exception:
                payload = {
                    "accepted": False,
                    "message": "反馈提交失败，请稍后重试。",
                }
            self.control_messages.put(("feedback_result", payload))

        threading.Thread(target=submit, name="feedback-submit", daemon=True).start()

    def _handle_feedback_result(self, payload: object) -> None:
        self.feedback_submitting = False
        if self.feedback_submit_button is not None:
            self.feedback_submit_button.configure(
                text="提交反馈", bg=ACCENT, fg="#07110e"
            )
        if not isinstance(payload, dict):
            payload = {}
        accepted = bool(payload.get("accepted"))
        feedback_id = str(payload.get("feedback_id", "")).strip()
        message = str(payload.get("message", "")).strip()
        if self.feedback_status_label is not None:
            if accepted:
                shown = message or "反馈已提交。"
                if feedback_id:
                    shown = f"{shown} 编号：{feedback_id}"
                self.feedback_status_label.configure(text=shown, fg=ACCENT)
            else:
                self.feedback_status_label.configure(
                    text=message or "反馈提交失败，请稍后重试。", fg=ERROR
                )
        if accepted and self.feedback_content is not None:
            self.feedback_content.delete("1.0", "end")

    def _close_feedback_window(self) -> None:
        if self.feedback_window is not None and self.feedback_window.winfo_exists():
            self.feedback_window.destroy()
        self.feedback_window = None
        self.feedback_category_var = None
        self.feedback_record_var = None
        self.feedback_record_ids = {}
        self.feedback_content = None
        self.feedback_diagnostics_var = None
        self.feedback_status_label = None
        self.feedback_submit_button = None
        self.tray_feedback_hidden = False

    def _flush_combat_history(self) -> int:
        records = self.model.pop_completed_combats()
        saved = 0
        failed: list[dict] = []
        for record in records:
            try:
                enriched_record = dict(record)
                enriched_record["capture_pipeline_at_archive"] = (
                    self.worker.diagnostic_snapshot()
                )
                self.history_store.save(enriched_record)
                saved += 1
            except (OSError, ValueError, TypeError):
                failed.append(record)
        if failed:
            self.model.completed_combats.extend(failed)
        if (
            saved
            and self.history_window is not None
            and self.history_window.winfo_exists()
            and self.history_window.state() == "normal"
        ):
            self._refresh_history_records()
        return saved

    def _load_recent_history(self, limit: int) -> list[dict]:
        worker = getattr(self, "worker", None)
        catalog = getattr(worker, "monster_catalog", {})
        records: list[dict] = []
        for record in self.history_store.load_recent(limit):
            restored = restore_history_boss_names(record, catalog)
            if isinstance(restored, dict):
                records.append(restored)
        return records

    def _ingest_stage_summary(self, payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        if self.model.ingest_stage_summary(payload):
            return True
        attached = self.history_store.attach_stage_summary_validation(payload)
        if attached is None:
            return False
        if (
            self.history_window is not None
            and self.history_window.winfo_exists()
            and self.history_window.state() == "normal"
        ):
            self._refresh_history_records()
        return True

    def _selected_history_record(self) -> dict | None:
        for record in self.history_records:
            if str(record.get("encounter_id", "")) == self.history_selected_id:
                return record
        return None

    @staticmethod
    def _history_actor_name(participant: dict, index: int, hide_names: bool) -> str:
        if hide_names:
            return f"玩家{index + 1}"
        return str(participant.get("name", "")).strip() or f"玩家{index + 1}"

    @staticmethod
    def _history_share_text(
        record: dict, hide_names: bool = False, max_length: int = 110
    ) -> str:
        participants = record.get("participants", [])
        if not isinstance(participants, list) or max_length <= 0:
            return ""
        try:
            duration = max(1.0, float(record.get("duration_seconds", 0.0) or 0.0))
        except (TypeError, ValueError, OverflowError):
            duration = 1.0
        share_rows: list[tuple[str, int]] = []
        for index, participant in enumerate(participants):
            if not isinstance(participant, dict):
                continue
            name = DpsWindow._history_actor_name(participant, index, hide_names)
            name = re.sub(r"[\s:：;；]+", "", name).strip()
            if not name:
                name = f"玩家{index + 1}"
            if not hide_names:
                name = name[:4]
            try:
                damage = max(0.0, float(participant.get("damage", 0.0) or 0.0))
                dps = float(participant.get("dps", damage / duration) or 0.0)
            except (TypeError, ValueError, OverflowError):
                dps = 0.0
            share_rows.append((name, int(round(max(0.0, dps)))))

        precisions = [2] * len(share_rows)
        while True:
            groups: dict[str, list[int]] = {}
            for index, (_name, dps) in enumerate(share_rows):
                precision = precisions[index]
                shown = f"{dps / 10_000:.{precision}f}"
                groups.setdefault(shown, []).append(index)
            changed = False
            for indexes in groups.values():
                if (
                    len(indexes) < 2
                    or len({share_rows[index][1] for index in indexes}) < 2
                ):
                    continue
                for index in indexes:
                    if precisions[index] < 4:
                        precisions[index] += 1
                        changed = True
            if not changed:
                break

        parts: list[str] = []
        for index, (name, dps) in enumerate(share_rows):
            precision = precisions[index]
            part = f"{name}:{dps / 10_000:.{precision}f}w"
            candidate = " ".join([*parts, part])
            if len(candidate) > max_length:
                break
            parts.append(part)
        return " ".join(parts)

    @staticmethod
    def _history_timestamp(record: dict) -> str:
        try:
            value = float(record.get("ended_at_epoch", 0.0) or 0.0)
            return dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d  %H:%M")
        except (OSError, OverflowError, TypeError, ValueError):
            return "时间未知"

    @staticmethod
    def _history_is_boss(record: dict) -> bool:
        monster = record.get("monster")
        if not isinstance(monster, dict):
            return False
        try:
            boss_rank = int(monster.get("boss_rank", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            boss_rank = 0
        try:
            boss_type = int(monster.get("boss_type", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            boss_type = 0
        entity_type = str(monster.get("entity_type", "")).casefold()
        return (
            boss_rank >= 3
            or boss_type == 3
            or "boss" in entity_type
            or "首领" in entity_type
        )

    def _initial_history_geometry(self, x: int, y: int) -> str:
        saved = str(self.config.get("history_geometry", ""))
        match = re.fullmatch(r"\d+x\d+([+-]\d+[+-]\d+)", saved)
        position = match.group(1) if match else f"+{x}+{y}"
        return self._visible_geometry(
            f"{BACKEND_WIDTH}x{BACKEND_HEIGHT}{position}",
            BACKEND_WIDTH,
            BACKEND_HEIGHT,
        )

    def show_history(self) -> None:
        self.show_backend("history")

    def show_main_menu(self) -> None:
        self.show_backend("history")

    def show_settings(self) -> None:
        self.show_backend("settings")

    def show_backend(self, page: str = "history") -> None:
        if self.compact_mode:
            return
        if self.history_window is None or not self.history_window.winfo_exists():
            self._build_history_window()
        self._select_backend_page(page)
        self.tray_history_hidden = False
        self._hide_main_tooltip()
        self._close_notice_window()
        self._remember_root_geometry()
        self._destroy_unlock_window()
        self.root.withdraw()
        self.history_window.deiconify()
        self.history_window.lift()
        self.history_window.focus_force()
        if page == "history":
            self._refresh_history_records()

    def _build_history_window(self) -> None:
        x = max(0, self.root.winfo_rootx() + 24)
        y = max(0, self.root.winfo_rooty() + 24)
        window = tk.Toplevel(self.root)
        self.history_window = window
        window.title(UI_BRAND)
        window.configure(bg=BORDER)
        window.geometry(self._initial_history_geometry(x, y))
        window.resizable(False, False)
        window.attributes("-topmost", bool(self.root.attributes("-topmost")))
        window.attributes("-alpha", 1.0)
        window.overrideredirect(True)
        window.protocol("WM_DELETE_WINDOW", self._close_history_window)
        window.bind("<Escape>", lambda _event: self._close_history_window())

        shell = tk.Frame(window, bg=BORDER)
        shell.pack(fill="both", expand=True)
        body = tk.Frame(shell, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        titlebar = tk.Frame(body, bg=SURFACE, height=44)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        self._bind_drag(titlebar, window)
        logo_image = self.icons.app_logo(22)
        logo = tk.Label(titlebar, image=logo_image, bg=SURFACE, bd=0)
        logo.image = logo_image
        logo.pack(side="left", padx=(14, 8), pady=10)
        self._bind_drag(logo, window)
        title = tk.Label(
            titlebar,
            text=UI_BRAND,
            bg=SURFACE,
            fg=TEXT,
            font=self._ui_font("title"),
        )
        title.pack(side="left")
        self._bind_drag(title, window)
        self._label_button(
            titlebar,
            "×",
            self._close_history_window,
            width=44,
            hover="#7f2d35",
            fg="#c5ccd3",
            font=self._ui_font("icon"),
        ).pack(side="right", fill="y")
        self.history_max_button = self._label_button(
            titlebar,
            "□",
            lambda: self._toggle_maximize(window),
            width=44,
            font=self._ui_font("icon"),
        )
        self.history_max_button.pack(side="right", fill="y")
        self._label_button(
            titlebar,
            "—",
            self._minimize_history,
            width=44,
            font=self._ui_font("icon"),
        ).pack(side="right", fill="y")

        workspace = tk.Frame(body, bg=BG)
        workspace.pack(fill="both", expand=True)
        sidebar = tk.Frame(
            workspace,
            bg="#0d0f12",
            width=168,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        account = tk.Frame(sidebar, bg="#0d0f12", height=126)
        account.pack(fill="x")
        account.pack_propagate(False)
        tk.Label(
            account,
            text="当前身份",
            bg="#0d0f12",
            fg=SUBTLE,
            anchor="w",
            font=self._ui_font("micro"),
        ).pack(fill="x", padx=15, pady=(13, 7))
        identity = tk.Frame(
            account,
            bg=SURFACE,
            height=66,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        identity.pack(fill="x", padx=14)
        identity.pack_propagate(False)
        tk.Frame(identity, bg=ACCENT, width=3).pack(side="left", fill="y")
        identity_text = tk.Frame(identity, bg=SURFACE)
        identity_text.pack(
            side="left", fill="both", expand=True, padx=(10, 6), pady=(8, 6)
        )
        self.membership_label = tk.Label(
            identity_text,
            text=membership_label_for_card_tier(self.licensing.session.card_tier),
            bg=SURFACE,
            fg=ACCENT,
            anchor="w",
            font=self._ui_font("title"),
        )
        self.membership_label.pack(fill="x")
        self.expiry_label = tk.Label(
            identity_text,
            text="有效时间：永久",
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
            font=self._ui_font("small"),
        )
        self.expiry_label.pack(fill="x", pady=(2, 0))
        tk.Frame(account, bg=BORDER, height=1).pack(
            side="bottom", fill="x", padx=18
        )

        nav = tk.Frame(sidebar, bg="#0d0f12")
        nav.pack(fill="x", pady=(8, 0))
        self.backend_nav_buttons = {}
        for key, icon_text, caption, enabled in (
            ("history", "◷", "历史记录", True),
            ("upload", "☁", "上传数据", False),
            ("updates", "▤", "更新日志", True),
            ("settings", "⚙", "设置", True),
        ):
            button = self._backend_nav_button(
                nav, key, icon_text, caption, enabled=enabled
            )
            button.pack(fill="x", pady=1)
            self.backend_nav_buttons[key] = button

        sidebar_bottom = tk.Frame(sidebar, bg="#0d0f12", height=54)
        sidebar_bottom.pack(side="bottom", fill="x")
        sidebar_bottom.pack_propagate(False)
        tk.Label(
            sidebar_bottom,
            text=f"v{APP_VERSION}",
            bg="#0d0f12",
            fg=MUTED,
            font=self._ui_font("small"),
        ).pack(side="left", padx=(22, 0), fill="y")
        self.update_button = tk.Label(
            sidebar_bottom,
            text="↻",
            bg="#0d0f12",
            fg=TEXT,
            cursor="hand2",
            width=4,
            font=self._ui_font("icon"),
        )
        self.update_button.pack(side="right", padx=(0, 12), fill="y")
        self.update_button.bind(
            "<Button-1>", lambda _event: self._start_update_check(manual=True)
        )
        self.update_button.bind(
            "<Enter>", lambda _event: self.update_button.configure(fg=ACCENT)
        )
        self.update_button.bind(
            "<Leave>", lambda _event: self.update_button.configure(fg=TEXT)
        )

        page_host = tk.Frame(workspace, bg=BG)
        page_host.pack(side="left", fill="both", expand=True)
        page_host.grid_rowconfigure(0, weight=1)
        page_host.grid_columnconfigure(0, weight=1)
        self.backend_pages = {
            "history": self._build_backend_history_page(page_host),
            "updates": self._build_backend_updates_page(page_host),
            "settings": self._build_backend_settings_page(page_host),
        }
        for page in self.backend_pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self._sync_expiry_label()
        self._select_backend_page(self.backend_current_page)
        window.after(20, lambda: self._apply_windows_style(window))

    def _backend_nav_button(
        self,
        parent: tk.Misc,
        key: str,
        icon_text: str,
        caption: str,
        *,
        enabled: bool,
    ) -> tk.Frame:
        row = tk.Frame(
            parent,
            bg="#0d0f12",
            height=48,
            cursor="hand2" if enabled else "arrow",
        )
        row.pack_propagate(False)
        indicator = tk.Frame(row, bg="#0d0f12", width=3)
        indicator.pack(side="left", fill="y")
        icon_label = tk.Label(
            row,
            text=icon_text,
            bg="#0d0f12",
            fg=TEXT if enabled else SUBTLE,
            width=2,
            anchor="center",
            bd=0,
            cursor="hand2" if enabled else "arrow",
            font=self._ui_font("icon"),
        )
        icon_label.pack(side="left", fill="y", padx=(15, 8))
        caption_label = tk.Label(
            row,
            text=caption,
            bg="#0d0f12",
            fg=TEXT if enabled else SUBTLE,
            anchor="w",
            bd=0,
            cursor="hand2" if enabled else "arrow",
            font=self._ui_font("body"),
        )
        caption_label.pack(side="left", fill="both", expand=True)
        row._disabled = not enabled
        row._page_key = key
        row._indicator = indicator
        row._icon_label = icon_label
        row._caption_label = caption_label
        if enabled:
            for widget in (row, indicator, icon_label, caption_label):
                widget.bind(
                    "<Button-1>",
                    lambda _event, page_key=key: self._select_backend_page(
                        page_key
                    ),
                )
                widget.bind(
                    "<Enter>",
                    lambda _event, button=row: (
                        self._style_backend_nav_button(button, PANEL, TEXT)
                        if self.backend_current_page != button._page_key
                        else None
                    ),
                )
                widget.bind(
                    "<Leave>",
                    lambda _event: self._sync_backend_navigation(),
                )
        return row

    @staticmethod
    def _style_backend_nav_button(
        button: tk.Frame, background: str, foreground: str, *, selected: bool = False
    ) -> None:
        button.configure(bg=background)
        button._indicator.configure(bg=ACCENT if selected else background)
        button._icon_label.configure(bg=background, fg=foreground)
        button._caption_label.configure(bg=background, fg=foreground)

    def _sync_backend_navigation(self) -> None:
        for key, button in self.backend_nav_buttons.items():
            if getattr(button, "_disabled", False):
                self._style_backend_nav_button(button, "#0d0f12", SUBTLE)
            elif key == self.backend_current_page:
                self._style_backend_nav_button(
                    button, PANEL_2, ACCENT, selected=True
                )
            else:
                self._style_backend_nav_button(button, "#0d0f12", TEXT)

    def _select_backend_page(self, page: str) -> None:
        page = str(page or "history")
        if page not in self.backend_pages:
            page = "history"
        self.backend_current_page = page
        frame = self.backend_pages.get(page)
        if frame is not None:
            frame.tkraise()
        self._sync_backend_navigation()
        if page == "history" and self.history_window is not None:
            self._refresh_history_records()

    def _backend_action_button(
        self, parent: tk.Misc, text: str, command
    ) -> tk.Label:
        button = self._action_button(parent, text, command)
        button.configure(
            bg=PANEL,
            fg=TEXT,
            padx=10,
            pady=6,
            font=self._ui_font("small"),
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        return button

    def _build_backend_history_page(self, parent: tk.Misc) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        heading = tk.Frame(page, bg=BG, height=58)
        heading.pack(fill="x", padx=22)
        heading.pack_propagate(False)
        tk.Label(
            heading,
            text="历史记录",
            bg=BG,
            fg=TEXT,
            font=self._ui_font("heading"),
        ).pack(side="left", fill="y")

        content = tk.Frame(page, bg=BG)
        content.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        left = tk.Frame(
            content,
            width=244,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        list_heading = tk.Frame(left, bg=SURFACE, height=50)
        list_heading.pack(fill="x")
        list_heading.pack_propagate(False)
        tk.Label(
            list_heading,
            text="战斗列表",
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
            font=self._ui_font("strong"),
        ).pack(side="left", padx=14, fill="y")
        self.history_count_label = tk.Label(
            list_heading,
            text="0 场",
            bg=SURFACE,
            fg=MUTED,
            anchor="e",
            font=self._ui_font("small"),
        )
        self.history_count_label.pack(side="right", padx=12, fill="y")
        list_area = tk.Frame(left, bg=PANEL)
        list_area.pack(fill="both", expand=True)
        self.history_list_canvas = tk.Canvas(
            list_area,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            yscrollincrement=66,
        )
        self.history_list_scrollbar = tk.Scrollbar(
            list_area,
            orient="vertical",
            command=self.history_list_canvas.yview,
            bg=SUBTLE,
            troughcolor=PANEL,
            activebackground=MUTED,
            bd=0,
            highlightthickness=0,
            width=9,
        )
        self.history_list_canvas.configure(
            yscrollcommand=self.history_list_scrollbar.set
        )
        self.history_list_scrollbar.pack(side="right", fill="y")
        self.history_list_canvas.pack(side="left", fill="both", expand=True)
        self.history_list_canvas.bind(
            "<MouseWheel>",
            lambda event: self.history_list_canvas.yview_scroll(
                -1 if event.delta > 0 else 1, "units"
            ),
        )
        self.history_list_canvas.bind(
            "<Configure>", lambda _event: self._draw_history_list()
        )

        right = tk.Frame(content, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        hero = tk.Frame(right, bg=BG, height=58)
        hero.pack(fill="x")
        hero.pack_propagate(False)
        identity = tk.Frame(hero, bg=BG)
        identity.pack(side="left", fill="both", expand=True)
        self.history_target_label = tk.Label(
            identity,
            text="请选择战斗记录",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=self._ui_font("title"),
        )
        self.history_target_label.pack(fill="x", pady=(3, 0))
        self.history_time_label = tk.Label(
            identity,
            text="--",
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=self._ui_font("small"),
        )
        self.history_time_label.pack(fill="x", pady=(2, 0))
        actions = tk.Frame(hero, bg=BG)
        actions.pack(side="right", fill="y")
        self.history_favorite_button = self._backend_action_button(
            actions, "☆ 收藏", self._toggle_history_favorite
        )
        self.history_favorite_button.pack(side="left", padx=3, pady=9)
        for caption, command in (
            ("删除", self._delete_history),
            ("一键清理", self._clear_unfavorited_history),
            ("分享", self._share_history),
            ("反馈", self._feedback_selected_history),
        ):
            self._backend_action_button(actions, caption, command).pack(
                side="left", padx=3, pady=9
            )

        metrics = tk.Frame(
            right,
            bg=SURFACE,
            height=70,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        metrics.pack(fill="x", pady=(2, 8))
        metrics.pack_propagate(False)
        self.history_total_value = self._history_metric(
            metrics, "总伤害", "0", 0
        )
        self.history_dps_value = self._history_metric(
            metrics, "总 DPS", "0", 1
        )
        self.history_team_value = self._history_metric(
            metrics, "队伍", "0 人", 2
        )
        self.history_metrics_label = self.history_total_value

        participant_heading = tk.Frame(right, bg=BG, height=26)
        participant_heading.pack(fill="x")
        participant_heading.pack_propagate(False)
        tk.Label(
            participant_heading,
            text="团队排行",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=self._ui_font("strong"),
        ).pack(side="left", fill="y")
        self.history_participant_panel = tk.Frame(
            right,
            bg=PANEL,
            height=304,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.history_participant_panel.pack(fill="x")
        self.history_participant_panel.pack_propagate(False)
        self.history_participant_header_canvas = tk.Canvas(
            self.history_participant_panel,
            bg=SURFACE,
            height=30,
            bd=0,
            highlightthickness=0,
        )
        self.history_participant_header_canvas.pack(fill="x")
        self.history_participant_header_canvas.bind(
            "<Configure>", lambda _event: self._draw_history_participant_header()
        )
        participant_body = tk.Frame(self.history_participant_panel, bg=PANEL)
        participant_body.pack(fill="both", expand=True)
        self.history_participant_canvas = tk.Canvas(
            participant_body,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            yscrollincrement=34,
        )
        self.history_participant_scrollbar = tk.Scrollbar(
            participant_body,
            orient="vertical",
            command=self.history_participant_canvas.yview,
            bg=SUBTLE,
            troughcolor=PANEL,
            activebackground=MUTED,
            bd=0,
            highlightthickness=0,
            width=9,
        )
        self.history_participant_canvas.configure(
            yscrollcommand=self.history_participant_scrollbar.set
        )
        self.history_participant_scrollbar.pack(side="right", fill="y")
        self.history_participant_canvas.pack(side="left", fill="both", expand=True)
        self.history_participant_canvas.bind(
            "<MouseWheel>",
            lambda event: self.history_participant_canvas.yview_scroll(
                -1 if event.delta > 0 else 1, "units"
            ),
        )
        self.history_participant_canvas.bind(
            "<Configure>", lambda _event: self._draw_history_participants()
        )

        detail_header = tk.Frame(right, bg=BG, height=36)
        detail_header.pack(fill="x", pady=(6, 0))
        detail_header.pack_propagate(False)
        self.history_detail_buttons = {}
        for mode, caption in (("skills", "技能详情"), ("targets", "目标伤害")):
            tab = tk.Label(
                detail_header,
                text=caption,
                bg=BG,
                fg=TEXT,
                padx=12,
                cursor="hand2",
                font=self._ui_font("strong"),
            )
            tab.pack(side="left", fill="y")
            tab.bind(
                "<Button-1>",
                lambda _event, selected_mode=mode: self._set_history_detail_mode(
                    selected_mode
                ),
            )
            self.history_detail_buttons[mode] = tab
        self.history_detail_label = tk.Label(
            detail_header,
            text="暴击率  --     死亡  0 次",
            bg=BG,
            fg=MUTED,
            anchor="e",
            font=self._ui_font("small"),
        )
        self.history_detail_label.pack(side="right", fill="y")
        skill_panel = tk.Frame(
            right,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        skill_panel.pack(fill="both", expand=True)
        self.history_skill_canvas = tk.Canvas(
            skill_panel,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            yscrollincrement=34,
        )
        self.history_skill_scrollbar = tk.Scrollbar(
            skill_panel,
            orient="vertical",
            command=self.history_skill_canvas.yview,
            bg=SUBTLE,
            troughcolor=PANEL,
            activebackground=MUTED,
            bd=0,
            highlightthickness=0,
            width=9,
        )
        self.history_skill_canvas.configure(
            yscrollcommand=self.history_skill_scrollbar.set
        )
        self.history_skill_scrollbar.pack(side="right", fill="y")
        self.history_skill_canvas.pack(side="left", fill="both", expand=True)
        self.history_skill_canvas.bind(
            "<MouseWheel>",
            lambda event: self.history_skill_canvas.yview_scroll(
                -1 if event.delta > 0 else 1, "units"
            ),
        )
        self.history_skill_canvas.bind(
            "<Configure>", lambda _event: self._draw_history_skills()
        )

        self._set_history_detail_mode(self.history_detail_mode)
        return page

    def _history_metric(
        self, parent: tk.Misc, caption: str, value: str, column: int
    ) -> tk.Label:
        parent.grid_columnconfigure(column, weight=1, uniform="history_metrics")
        group = tk.Frame(parent, bg=SURFACE)
        group.grid(row=0, column=column, sticky="nsew", padx=1, pady=8)
        if column:
            tk.Frame(group, bg=BORDER, width=1).pack(side="left", fill="y")
        tk.Label(
            group,
            text=caption,
            bg=SURFACE,
            fg=MUTED,
            font=self._ui_font("small"),
        ).pack(pady=(2, 1))
        label = tk.Label(
            group,
            text=value,
            bg=SURFACE,
            fg=ACCENT,
            font=self._ui_font("number_large"),
        )
        label.pack()
        return label

    def _build_backend_updates_page(self, parent: tk.Misc) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        heading = tk.Frame(page, bg=BG, height=74)
        heading.pack(fill="x", padx=40, pady=(18, 4))
        heading.pack_propagate(False)
        tk.Label(
            heading,
            text="更新日志",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=self._ui_font("heading"),
        ).pack(side="left", fill="y")
        self.update_action_button = self._backend_action_button(
            heading, "检查更新", self._update_page_action
        )
        self.update_action_button.configure(bg=ACCENT, fg="#07110e", padx=18)
        self.update_action_button.pack(side="right", pady=18)
        self.update_button = self.update_action_button

        update_state = tk.Frame(
            page,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        update_state.pack(fill="x", padx=40, pady=(0, 12))
        self.update_summary_label = tk.Label(
            update_state,
            text=f"当前版本 v{APP_VERSION}",
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
            font=self._ui_font("title"),
        )
        self.update_summary_label.pack(fill="x", padx=20, pady=(14, 3))
        self.update_status_label = tk.Label(
            update_state,
            text="更新检查和下载均在本页显示，不会弹出新窗口。",
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=760,
            font=self._ui_font("small"),
        )
        self.update_status_label.pack(fill="x", padx=20, pady=(0, 14))
        tk.Label(
            update_state,
            text=f"保存位置：{UPDATE_DIR}",
            bg=SURFACE,
            fg=SUBTLE,
            anchor="w",
            font=self._ui_font("micro"),
        ).pack(fill="x", padx=20, pady=(0, 14))
        content = tk.Frame(
            page,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        content.pack(fill="both", expand=True, padx=40, pady=(0, 26))
        releases = (
            (
                "v0.0.14",
                "• 修复部分用户锁定后主窗口被游戏覆盖、只剩解锁按钮的问题\n"
                "• 锁定期间保持主窗口可见，解锁后恢复原来的置顶设置\n"
                "• 修复点击解锁后主窗口没有立即回到前台的问题\n"
                "• 修复程序重启或从托盘恢复时已锁定窗口层级异常\n"
                "• 修复刷新/清空、隐藏名称、锁定按钮缺少悬停反馈的问题\n"
                "• 修复旧配置导致点击游戏后主窗口被盖住的问题\n"
                "• 历史团队排行新增秒伤，并跟随 DPS 设置显示对应列",
            ),
            (
                "v0.0.13",
                "• 根除小怪密集场景的重复扫描、同步等待和无关消息开销\n"
                "• 新增主窗口透明度与新版字体、透明度滑杆\n"
                "• 迷你模式改为可滚动玩家排行，列跟随设置页勾选\n"
                "• 迷你表头补回锁定，宽度按显示列自动调整\n"
                "• 标题栏设置入口改为主菜单，设置保留在菜单导航中\n"
                "• Boss 血条、用户身份区和单窗口透明度行为统一优化",
            ),
            (
                "v0.0.12",
                "• 优化怪物数量较多时的流畅度\n"
                "• 修复部分 Boss 名称显示为“首领”的问题\n"
                "• 改善更新过程中偶尔停住的情况\n"
                "• 使用全新的黑色界面",
            ),
            (
                "v0.0.10",
                "• 增加战斗历史、收藏和分享\n"
                "• 完善队伍成员与技能伤害统计\n"
                "• 提升 Boss 识别和连接稳定性",
            ),
        )
        for index, (version, notes) in enumerate(releases):
            card = tk.Frame(content, bg=SURFACE)
            card.pack(fill="x", padx=22, pady=(22 if index == 0 else 8, 0))
            tk.Label(
                card,
                text=version,
                bg=SURFACE,
                fg=ACCENT,
                anchor="w",
                font=self._ui_font("title"),
            ).pack(fill="x", padx=20, pady=(16, 8))
            tk.Label(
                card,
                text=notes,
                bg=SURFACE,
                fg=TEXT,
                justify="left",
                anchor="w",
                font=self._ui_font("body"),
            ).pack(fill="x", padx=20, pady=(0, 18))
        self._refresh_update_page()
        return page

    def _build_backend_settings_page(self, parent: tk.Misc) -> tk.Frame:
        page = tk.Frame(parent, bg=BG)
        tk.Label(
            page,
            text="设置",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=self._ui_font("heading"),
        ).pack(fill="x", padx=40, pady=(28, 12))
        tabs = tk.Frame(page, bg=BG, height=42)
        tabs.pack(fill="x", padx=40)
        tabs.pack_propagate(False)
        self.backend_settings_buttons = {}
        for key, caption in (("general", "总体设置"), ("dps", "DPS 设置")):
            button = tk.Label(
                tabs,
                text=caption,
                bg=BG,
                fg=TEXT,
                padx=16,
                cursor="hand2",
                font=self._ui_font("strong"),
            )
            button.pack(side="left", fill="y", padx=(0, 12))
            button.bind(
                "<Button-1>",
                lambda _event, selected=key: self._select_settings_section(
                    selected
                ),
            )
            self.backend_settings_buttons[key] = button

        host = tk.Frame(page, bg=BG)
        host.pack(fill="both", expand=True, padx=40, pady=(8, 0))
        host.grid_rowconfigure(0, weight=1)
        host.grid_columnconfigure(0, weight=1)
        general = tk.Frame(
            host,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        dps = tk.Frame(
            host,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.backend_settings_frames = {"general": general, "dps": dps}
        for frame in self.backend_settings_frames.values():
            frame.grid(row=0, column=0, sticky="nsew")

        tk.Label(
            general,
            text="总体设置",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=self._ui_font("title"),
        ).pack(fill="x", padx=24, pady=(22, 14))
        tk.Frame(general, bg=BORDER, height=1).pack(fill="x", padx=24)
        font_row = tk.Frame(general, bg=PANEL, height=92)
        font_row.pack(fill="x", padx=24, pady=14)
        font_row.pack_propagate(False)
        tk.Label(
            font_row,
            text="字体大小",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            width=14,
            font=self._ui_font("strong"),
        ).pack(side="left", padx=(0, 12))
        self.settings_font_size_var = tk.IntVar(
            master=self.history_window, value=self.ui_font_size
        )
        font_scale = ModernSlider(
            font_row,
            from_=12,
            to=18,
            variable=self.settings_font_size_var,
            resolution=1,
            command=self._preview_font_size,
            length=470,
            background=PANEL,
        )
        font_scale.pack(side="left", fill="x", expand=True)
        self.settings_font_value_label = tk.Label(
            font_row,
            text=f"{self.ui_font_size}px",
            bg=SURFACE,
            fg=ACCENT,
            width=6,
            padx=8,
            pady=6,
            font=self._ui_font("number_strong"),
        )
        self.settings_font_value_label.pack(side="right", padx=(18, 4))

        opacity_row = tk.Frame(general, bg=PANEL, height=92)
        opacity_row.pack(fill="x", padx=24, pady=(0, 14))
        opacity_row.pack_propagate(False)
        tk.Label(
            opacity_row,
            text="主窗口透明度",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            width=14,
            font=self._ui_font("strong"),
        ).pack(side="left", padx=(0, 12))
        self.settings_opacity_var = tk.IntVar(
            master=self.history_window,
            value=round(self.window_alpha * 100),
        )
        self.opacity_scale = ModernSlider(
            opacity_row,
            from_=MIN_WINDOW_ALPHA_PERCENT,
            to=100,
            variable=self.settings_opacity_var,
            resolution=1,
            command=self._preview_window_alpha,
            length=470,
            background=PANEL,
        )
        self.opacity_scale.pack(side="left", fill="x", expand=True)
        self.opacity_value_label = tk.Label(
            opacity_row,
            text=f"{round(self.window_alpha * 100)}%",
            bg=SURFACE,
            fg=ACCENT,
            width=6,
            padx=8,
            pady=6,
            font=self._ui_font("number_strong"),
        )
        self.opacity_value_label.pack(side="right", padx=(18, 4))

        tk.Label(
            dps,
            text="DPS 设置",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=self._ui_font("title"),
        ).pack(fill="x", padx=24, pady=(22, 14))
        tk.Frame(dps, bg=BORDER, height=1).pack(fill="x", padx=24)
        self.settings_show_names_var = tk.BooleanVar(
            master=self.history_window, value=not self.hide_names
        )
        self.settings_show_damage_var = tk.BooleanVar(
            master=self.history_window, value=self.show_total_damage
        )
        self.settings_show_dps_var = tk.BooleanVar(
            master=self.history_window, value=self.show_dps
        )
        self.settings_show_share_var = tk.BooleanVar(
            master=self.history_window, value=self.show_damage_share
        )
        self.settings_show_critical_var = tk.BooleanVar(
            master=self.history_window, value=self.show_critical_rate
        )
        for caption, variable, disabled in (
            ("显示名称", self.settings_show_names_var, False),
            ("显示总伤害", self.settings_show_damage_var, False),
            ("显示秒伤", self.settings_show_dps_var, False),
            ("显示占比", self.settings_show_share_var, False),
            ("显示暴击率", self.settings_show_critical_var, False),
            ("显示死亡次数", tk.BooleanVar(master=self.history_window), True),
            ("显示复活次数", tk.BooleanVar(master=self.history_window), True),
            ("显示死亡时间", tk.BooleanVar(master=self.history_window), True),
        ):
            self._settings_check_row(dps, caption, variable, disabled=disabled)

        save_bar = tk.Frame(page, bg=BG, height=76)
        save_bar.pack(fill="x", padx=40)
        save_bar.pack_propagate(False)
        save_button = tk.Label(
            save_bar,
            text="保存设置",
            bg=ACCENT,
            fg="#06100d",
            padx=30,
            pady=10,
            cursor="hand2",
            font=self._ui_font("strong"),
        )
        save_button.pack(side="left", pady=16)
        save_button.bind("<Button-1>", lambda _event: self._save_ui_settings())
        save_button.bind(
            "<Enter>", lambda _event: save_button.configure(bg="#86edca")
        )
        save_button.bind(
            "<Leave>", lambda _event: save_button.configure(bg=ACCENT)
        )
        self._select_settings_section("general")
        return page

    def _settings_check_row(
        self,
        parent: tk.Misc,
        caption: str,
        variable: tk.BooleanVar,
        *,
        disabled: bool,
    ) -> None:
        row = tk.Frame(parent, bg=PANEL, height=48)
        row.pack(fill="x", padx=24)
        row.pack_propagate(False)
        checkbox = tk.Canvas(
            row,
            width=22,
            height=22,
            bg=PANEL,
            highlightthickness=0,
            bd=0,
            cursor="arrow" if disabled else "hand2",
        )
        checkbox.pack(side="left", padx=(2, 12))
        label = tk.Label(
            row,
            text=caption,
            bg=PANEL,
            fg=MUTED if disabled else TEXT,
            anchor="w",
            cursor="arrow" if disabled else "hand2",
            font=self._ui_font("body"),
        )
        label.pack(side="left", fill="both", expand=True)

        hovered = False

        def redraw(*_args) -> None:
            checkbox.delete("all")
            checked = bool(variable.get())
            if disabled:
                fill = SURFACE
                outline = BORDER
                check_color = SUBTLE
            else:
                fill = ACCENT if checked else PANEL_2
                outline = ACCENT if checked or hovered else MUTED
                check_color = BG
            checkbox.create_rectangle(
                1,
                1,
                21,
                21,
                fill=fill,
                outline=outline,
                width=2,
            )
            if checked:
                checkbox.create_line(
                    5,
                    11,
                    9,
                    15,
                    17,
                    7,
                    fill=check_color,
                    width=3,
                    capstyle="round",
                    joinstyle="round",
                )

        def toggle(_event=None) -> str:
            if not disabled:
                variable.set(not bool(variable.get()))
            return "break"

        def enter(_event=None) -> None:
            nonlocal hovered
            if disabled:
                return
            hovered = True
            label.configure(fg=ACCENT)
            redraw()

        def leave(_event=None) -> None:
            nonlocal hovered
            hovered = False
            label.configure(fg=MUTED if disabled else TEXT)
            redraw()

        variable.trace_add("write", redraw)
        for widget in (row, checkbox, label):
            widget.bind("<Button-1>", toggle)
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)
        redraw()
        if disabled:
            tk.Label(
                row,
                text="暂不可用",
                bg=PANEL,
                fg=SUBTLE,
                font=self._ui_font("small"),
            ).pack(side="right", padx=8)

    def _select_settings_section(self, section: str) -> None:
        if section not in self.backend_settings_frames:
            section = "general"
        self.backend_settings_frames[section].tkraise()
        for key, button in self.backend_settings_buttons.items():
            button.configure(fg=ACCENT if key == section else TEXT)

    def _preview_font_size(self, value: object) -> None:
        if self.settings_font_value_label is None:
            return
        try:
            size = int(round(float(value)))
        except (TypeError, ValueError, OverflowError):
            return
        self.settings_font_value_label.configure(text=f"{size}px")

    def _preview_window_alpha(self, value: object) -> None:
        self._set_window_alpha(value)

    def _save_ui_settings(self) -> None:
        if self.settings_font_size_var is not None:
            self.ui_font_size = min(18, max(12, self.settings_font_size_var.get()))
        if self.settings_opacity_var is not None:
            self._set_window_alpha(self.settings_opacity_var.get(), persist=False)
        if self.settings_show_names_var is not None:
            self.hide_names = not bool(self.settings_show_names_var.get())
        if self.settings_show_damage_var is not None:
            self.show_total_damage = bool(self.settings_show_damage_var.get())
        if self.settings_show_dps_var is not None:
            self.show_dps = bool(self.settings_show_dps_var.get())
        if self.settings_show_share_var is not None:
            self.show_damage_share = bool(self.settings_show_share_var.get())
        if self.settings_show_critical_var is not None:
            self.show_critical_rate = bool(self.settings_show_critical_var.get())
        self._sync_compact_geometry_width()
        self._configure_ui_fonts()
        self._sync_action_buttons()
        self._draw_main_header()
        self._draw_main_rows()
        self._draw_history_participant_header()
        self._render_history_selection()
        self._save_preferences()

    def _feedback_selected_history(self) -> None:
        record = self._selected_history_record()
        if record is None:
            return
        self._feedback_history_record(str(record.get("encounter_id", "")))

    def _set_history_detail_mode(self, mode: str) -> None:
        self.history_detail_mode = "targets" if mode == "targets" else "skills"
        for key, button in self.history_detail_buttons.items():
            button.configure(
                fg=ACCENT if key == self.history_detail_mode else MUTED
            )
        self._draw_history_skills()

    def _refresh_history_records(self) -> None:
        current = self.history_selected_id
        self.history_records = [
            record
            for record in self._load_recent_history(500)
            if not self.model.boss_only or self._history_is_boss(record)
        ]
        self.history_records.sort(
            key=lambda item: (
                bool(item.get("favorite", False)),
                float(item.get("ended_at_epoch", 0.0) or 0.0),
            ),
            reverse=True,
        )
        ids = {str(item.get("encounter_id", "")) for item in self.history_records}
        if current not in ids:
            current = (
                str(self.history_records[0].get("encounter_id", ""))
                if self.history_records
                else ""
            )
            self.history_selected_actor = 0
        self.history_selected_id = current
        if self.history_count_label is not None:
            favorites = sum(bool(item.get("favorite")) for item in self.history_records)
            suffix = f" · ★{favorites}" if favorites else ""
            self.history_count_label.configure(
                text=f"{len(self.history_records)} 场{suffix}"
            )
        self._render_history_selection()

    def _draw_history_list(self) -> None:
        canvas = self.history_list_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        row_height = 66
        if not self.history_records:
            canvas.create_text(
                width // 2,
                height // 2,
                text="暂无战斗记录",
                fill=MUTED,
                font=self._ui_font("strong"),
            )
            canvas.configure(scrollregion=(0, 0, width, height))
            return
        for index, record in enumerate(self.history_records):
            top = index * row_height
            bottom = top + row_height
            encounter_id = str(record.get("encounter_id", ""))
            selected = encounter_id == self.history_selected_id
            base = PANEL_2 if selected else (PANEL if index % 2 == 0 else SURFACE)
            accent = ACCENT if selected else BORDER
            monster = record.get("monster")
            monster = monster if isinstance(monster, dict) else {}
            target_name = str(monster.get("name", "")).strip() or (
                "Boss战斗" if self._history_is_boss(record) else "怪物战斗"
            )
            level = int(monster.get("level", 0) or 0)
            if level:
                target_name += f"  Lv.{level}"
            favorite = "★  " if record.get("favorite") else ""
            target_name = self._fit_main_actor_name(
                f"{favorite}{target_name}", max(32, width - 82)
            )
            tag = f"history:{index}"
            feedback_tag = f"history-feedback:{index}"
            canvas.create_rectangle(
                0, top, width, bottom - 1, fill=base, outline="", tags=(tag,)
            )
            canvas.create_rectangle(
                0, top, 3, bottom - 1, fill=accent, outline="", tags=(tag,)
            )
            canvas.create_text(
                11,
                top + 13,
                text=target_name,
                fill=TEXT,
                anchor="w",
                font=self._ui_font("strong"),
                tags=(tag,),
            )
            canvas.create_text(
                11,
                top + 34,
                text=self._history_timestamp(record),
                fill=MUTED,
                anchor="w",
                font=self._ui_font("micro"),
                tags=(tag,),
            )
            canvas.create_text(
                11,
                top + 52,
                text=(
                    f"{format_duration(float(record.get('duration_seconds', 0) or 0))}"
                    f"   总伤害 {format_number(record.get('total_damage', 0))}"
                ),
                fill=ACCENT if selected else MUTED,
                anchor="w",
                font=self._ui_font("small"),
                tags=(tag,),
            )
            canvas.create_line(
                0, bottom - 1, width, bottom - 1, fill=BORDER, tags=(tag,)
            )
            feedback_bg = canvas.create_rectangle(
                width - 53,
                top + 7,
                width - 8,
                top + 29,
                fill=PANEL_2,
                outline=BORDER,
                tags=(feedback_tag,),
            )
            canvas.create_text(
                width - 30,
                top + 18,
                text="反馈",
                fill=ACCENT,
                font=self._ui_font("micro"),
                tags=(feedback_tag,),
            )
            canvas.tag_bind(
                tag,
                "<Button-1>",
                lambda _event, selected_id=encounter_id: self._select_history(
                    selected_id
                ),
            )
            canvas.tag_bind(tag, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind(tag, "<Leave>", lambda _event: canvas.configure(cursor=""))
            canvas.tag_bind(
                feedback_tag,
                "<Button-1>",
                lambda _event, selected_id=encounter_id: self._feedback_history_record(
                    selected_id
                ),
            )
            canvas.tag_bind(
                feedback_tag,
                "<Enter>",
                lambda _event, item=feedback_bg: (
                    canvas.itemconfigure(
                        item, fill=blend_color(PANEL_2, ACCENT, 0.16)
                    ),
                    canvas.configure(cursor="hand2"),
                ),
            )
            canvas.tag_bind(
                feedback_tag,
                "<Leave>",
                lambda _event, item=feedback_bg: (
                    canvas.itemconfigure(item, fill=PANEL_2),
                    canvas.configure(cursor=""),
                ),
            )
        canvas.configure(
            scrollregion=(0, 0, width, max(height, len(self.history_records) * row_height))
        )

    def _select_history(self, encounter_id: str) -> None:
        self.history_selected_id = str(encounter_id)
        self.history_selected_actor = 0
        self._render_history_selection()

    def _feedback_history_record(self, encounter_id: str) -> None:
        self._select_history(encounter_id)
        self.show_feedback(encounter_id)

    def _render_history_selection(self) -> None:
        record = self._selected_history_record()
        participants: list[dict] = []
        if record is None:
            if self.history_target_label is not None:
                self.history_target_label.configure(text="暂无战斗记录", fg=MUTED)
                self.history_time_label.configure(text="--")
            if self.history_total_value is not None:
                self.history_total_value.configure(text="0")
            if self.history_dps_value is not None:
                self.history_dps_value.configure(text="0")
            if self.history_team_value is not None:
                self.history_team_value.configure(text="0 人")
            if self.history_favorite_button is not None:
                self.history_favorite_button.configure(text="☆ 收藏", fg=MUTED)
            if self.history_detail_label is not None:
                self.history_detail_label.configure(
                    text="暴击率  --     死亡  0 次"
                )
        else:
            participants = record.get("participants", [])
            participants = participants if isinstance(participants, list) else []
            try:
                team_size = max(
                    len(participants),
                    int(record.get("team_size", len(participants))),
                )
            except (TypeError, ValueError, OverflowError):
                team_size = len(participants)
            actor_ids = [int(item.get("actor_id", 0) or 0) for item in participants]
            if self.history_selected_actor not in actor_ids:
                self.history_selected_actor = actor_ids[0] if actor_ids else 0
            monster = record.get("monster")
            monster = monster if isinstance(monster, dict) else {}
            target_name = str(monster.get("name", "")).strip() or (
                "Boss战斗" if self._history_is_boss(record) else "怪物战斗"
            )
            level = int(monster.get("level", 0) or 0)
            if level:
                target_name += f"  Lv.{level}"
            self.history_target_label.configure(text=target_name, fg=TEXT)
            self.history_time_label.configure(
                text=(
                    f"{self._history_timestamp(record)}   ·   "
                    f"{format_duration(float(record.get('duration_seconds', 0) or 0))}"
                )
            )
            if self.history_total_value is not None:
                self.history_total_value.configure(
                    text=format_number(record.get("total_damage", 0))
                )
            if self.history_dps_value is not None:
                self.history_dps_value.configure(
                    text=format_number(record.get("team_dps", 0))
                )
            if self.history_team_value is not None:
                self.history_team_value.configure(text=f"{team_size} 人")
            if self.history_favorite_button is not None:
                favorite = bool(record.get("favorite"))
                self.history_favorite_button.configure(
                    text="★ 已收藏" if favorite else "☆ 收藏",
                    fg=ACCENT if favorite else MUTED,
                )
            selected_participant = next(
                (
                    item
                    for item in participants
                    if int(item.get("actor_id", 0) or 0)
                    == self.history_selected_actor
                ),
                None,
            )
            if self.history_detail_label is not None:
                critical_rate = (
                    selected_participant.get("critical_rate")
                    if isinstance(selected_participant, dict)
                    else None
                )
                try:
                    critical_text = (
                        f"{float(critical_rate) * 100:.1f}%"
                        if critical_rate is not None
                        else "--"
                    )
                except (TypeError, ValueError, OverflowError):
                    critical_text = "--"
                try:
                    deaths = max(
                        0,
                        int(selected_participant.get("deaths", 0) or 0),
                    )
                except (AttributeError, TypeError, ValueError, OverflowError):
                    deaths = 0
                self.history_detail_label.configure(
                    text=f"暴击率  {critical_text}     死亡  {deaths} 次"
                )
        self._draw_history_list()
        self._draw_history_participant_header()
        self._draw_history_participants()
        self._draw_history_skills()

    def _history_participant_columns(self, width: int) -> dict[str, int]:
        enabled = list(self._enabled_main_metrics())
        right_edge = max(100, width - 16)
        name_ratio = 0.52 if len(enabled) <= 2 else 0.42
        name_limit = min(right_edge, max(100, int(width * name_ratio)))
        columns = {
            "rank": 20,
            "name": 58,
            "name_limit": name_limit,
        }
        if not enabled:
            return columns
        step = max(1, (right_edge - name_limit) / len(enabled))
        for index, key in enumerate(enabled, start=1):
            columns[key] = int(name_limit + step * index)
        return columns

    def _draw_history_participant_header(self) -> None:
        canvas = self.history_participant_header_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        columns = self._history_participant_columns(width)
        for text, x, anchor in (
            ("排名", columns["rank"], "w"),
            ("玩家名称", columns["name"], "w"),
        ):
            canvas.create_text(
                x,
                15,
                text=text,
                fill=MUTED,
                anchor=anchor,
                font=self._ui_font("small"),
            )
        captions = {
            "damage": "总伤害",
            "dps": "秒伤",
            "share": "占比",
            "critical": "暴击率",
        }
        for key in ("damage", "dps", "share", "critical"):
            if key not in columns:
                continue
            canvas.create_text(
                columns[key],
                15,
                text=captions[key],
                fill=MUTED,
                anchor="e",
                font=self._ui_font("small"),
            )

    def _draw_history_participants(self) -> None:
        canvas = self.history_participant_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        record = self._selected_history_record()
        participants = record.get("participants", []) if record else []
        participants = participants if isinstance(participants, list) else []
        if not participants:
            canvas.create_text(
                width // 2,
                height // 2,
                text="暂无团队伤害",
                fill=MUTED,
                font=("Microsoft YaHei UI", 9, "bold"),
            )
            canvas.configure(scrollregion=(0, 0, width, height))
            return
        highest_damage = max(
            (
                max(0, int(participant.get("damage", 0) or 0))
                for participant in participants
                if isinstance(participant, dict)
            ),
            default=0,
        )
        columns = self._history_participant_columns(width)
        try:
            duration = max(
                0.0, float(record.get("duration_seconds", 0.0) or 0.0)
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            duration = 0.0
        row_height = 34
        for index, participant in enumerate(participants):
            top = index * row_height
            bottom = top + row_height
            actor_id = int(participant.get("actor_id", 0) or 0)
            selected = actor_id == self.history_selected_actor
            class_id = int(participant.get("profession_id", 0) or 0)
            _profession, color = self._profession_info(class_id)
            base = PANEL_2 if selected else (PANEL if index % 2 == 0 else SURFACE)
            share = float(participant.get("share", 0.0) or 0.0)
            bar_ratio = relative_damage_bar_ratio(
                participant.get("damage", 0), highest_damage
            )
            tag = f"history-actor:{index}"
            canvas.create_rectangle(
                0, top, width, bottom - 1, fill=base, outline="", tags=(tag,)
            )
            canvas.create_rectangle(
                0,
                top,
                max(3, int(width * bar_ratio)),
                bottom - 1,
                fill=blend_color(base, color, 0.22),
                outline="",
                tags=(tag,),
            )
            canvas.create_text(
                columns["rank"],
                top + 17,
                text=str(index + 1),
                fill=ACCENT if index < 3 else MUTED,
                anchor="w",
                font=self._ui_font("number_strong"),
                tags=(tag,),
            )
            icon = self.icons.profession(class_id, 20)
            canvas.create_image(
                columns["name"] - 24,
                top + (row_height - 20) // 2,
                image=icon,
                anchor="nw",
                tags=(tag,),
            )
            canvas.create_text(
                columns["name"],
                top + 17,
                text=self._fit_main_actor_name(
                    self._history_actor_name(participant, index, self.hide_names),
                    max(40, columns["name_limit"] - columns["name"] - 8),
                ),
                fill=TEXT,
                anchor="w",
                font=self._ui_font("strong"),
                tags=(tag,),
            )
            try:
                damage = max(
                    0.0, float(participant.get("damage", 0.0) or 0.0)
                )
            except (TypeError, ValueError, OverflowError):
                damage = 0.0
            try:
                raw_dps = participant.get("dps")
                dps = (
                    max(0.0, float(raw_dps))
                    if raw_dps is not None
                    else (damage / duration if duration else 0.0)
                )
            except (TypeError, ValueError, OverflowError):
                dps = damage / duration if duration else 0.0
            critical_rate = participant.get("critical_rate")
            try:
                critical_text = (
                    f"{float(critical_rate) * 100:.1f}%"
                    if critical_rate is not None
                    else "--"
                )
            except (TypeError, ValueError, OverflowError):
                critical_text = "--"
            values = {
                "damage": format_number(damage),
                "dps": format_number(dps),
                "share": f"{share * 100:.1f}%",
                "critical": critical_text,
            }
            for key in ("damage", "dps", "share", "critical"):
                if key not in columns:
                    continue
                canvas.create_text(
                    columns[key],
                    top + 17,
                    text=values[key],
                    fill=TEXT,
                    anchor="e",
                    font=self._ui_font(
                        "number_strong" if key == "damage" else "number"
                    ),
                    tags=(tag,),
                )
            canvas.tag_bind(
                tag,
                "<Button-1>",
                lambda _event, selected_actor=actor_id: self._select_history_actor(
                    selected_actor
                ),
            )
            canvas.tag_bind(tag, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind(tag, "<Leave>", lambda _event: canvas.configure(cursor=""))
        canvas.configure(
            scrollregion=(0, 0, width, max(height, len(participants) * row_height))
        )

    def _select_history_actor(self, actor_id: int) -> None:
        self.history_selected_actor = int(actor_id)
        self._render_history_selection()

    def _draw_history_skills(self) -> None:
        canvas = self.history_skill_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        record = self._selected_history_record()
        participants = record.get("participants", []) if record else []
        participants = participants if isinstance(participants, list) else []
        participant = next(
            (
                item
                for item in participants
                if int(item.get("actor_id", 0) or 0)
                == self.history_selected_actor
            ),
            None,
        )
        if not isinstance(participant, dict):
            canvas.create_text(
                width // 2,
                height // 2,
                text="暂无个人详情",
                fill=MUTED,
                font=self._ui_font("strong"),
            )
            canvas.configure(scrollregion=(0, 0, width, height))
            return

        row_height = 38
        header_height = 30
        top = header_height
        if self.history_detail_mode == "targets":
            columns = {
                "type": int(width * 0.60),
                "damage": int(width * 0.80),
                "share": width - 14,
            }
            canvas.create_rectangle(
                0, 0, width, header_height, fill=SURFACE, outline=""
            )
            for text, x, anchor in (
                ("伤害目标", 12, "w"),
                ("类型", columns["type"], "e"),
                ("伤害", columns["damage"], "e"),
                ("占个人总伤", columns["share"], "e"),
            ):
                canvas.create_text(
                    x,
                    header_height // 2,
                    text=text,
                    fill=MUTED,
                    anchor=anchor,
                    font=self._ui_font("small"),
                )
            rows = participant.get("targets", [])
            rows = rows if isinstance(rows, list) else []
            if not rows:
                canvas.create_text(
                    width // 2,
                    top + row_height // 2,
                    text="暂无目标伤害",
                    fill=MUTED,
                    font=self._ui_font("body"),
                )
                top += row_height
            for index, target in enumerate(rows):
                bottom = top + row_height
                base = PANEL if index % 2 == 0 else SURFACE
                kind = str(target.get("kind", "小怪"))
                color = WARN if kind == "Boss" else ACCENT
                name = str(target.get("name", "")).strip() or "未命名目标"
                entity_ids = target.get("entity_ids", [])
                entity_count = len(entity_ids) if isinstance(entity_ids, list) else 0
                if entity_count > 1:
                    name = f"{name} ×{entity_count}"
                share = float(target.get("share", 0.0) or 0.0)
                canvas.create_rectangle(
                    0, top, width, bottom - 1, fill=base, outline=""
                )
                canvas.create_text(
                    12,
                    top + row_height // 2,
                    text=name,
                    fill=TEXT,
                    anchor="w",
                    font=self._ui_font("strong"),
                )
                canvas.create_text(
                    columns["type"],
                    top + row_height // 2,
                    text=kind,
                    fill=color,
                    anchor="e",
                    font=self._ui_font("small"),
                )
                canvas.create_text(
                    columns["damage"],
                    top + row_height // 2,
                    text=format_number(target.get("damage", 0)),
                    fill=TEXT,
                    anchor="e",
                    font=self._ui_font("number_strong"),
                )
                canvas.create_text(
                    columns["share"],
                    top + row_height // 2,
                    text=f"{share * 100:.1f}%",
                    fill=TEXT,
                    anchor="e",
                    font=self._ui_font("number"),
                )
                top = bottom
        else:
            columns = {
                "damage": int(width * 0.57),
                "share": int(width * 0.71),
                "hits": int(width * 0.83),
                "max": width - 14,
            }
            canvas.create_rectangle(
                0, 0, width, header_height, fill=SURFACE, outline=""
            )
            for text, x, anchor in (
                ("技能", 42, "w"),
                ("伤害", columns["damage"], "e"),
                ("占比", columns["share"], "e"),
                ("次数", columns["hits"], "e"),
                ("最大伤害", columns["max"], "e"),
            ):
                canvas.create_text(
                    x,
                    header_height // 2,
                    text=text,
                    fill=MUTED,
                    anchor=anchor,
                    font=self._ui_font("small"),
                )
            rows = participant.get("skills", [])
            rows = rows if isinstance(rows, list) else []
            if not rows:
                aggregate_damage = max(0, int(participant.get("damage", 0) or 0))
                rows = (
                    [
                        {
                            "skill_id": 0,
                            "name": "未归类伤害",
                            "damage": aggregate_damage,
                            "share": 1.0,
                            "hits": None,
                            "max_hit": None,
                        }
                    ]
                    if aggregate_damage
                    else []
                )
            if not rows:
                canvas.create_text(
                    width // 2,
                    top + row_height // 2,
                    text="暂无技能详情",
                    fill=MUTED,
                    font=self._ui_font("body"),
                )
                top += row_height
            class_id = int(participant.get("profession_id", 0) or 0)
            for index, skill in enumerate(rows):
                bottom = top + row_height
                base = PANEL if index % 2 == 0 else SURFACE
                try:
                    skill_id = int(skill.get("skill_id", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    skill_id = 0
                name = str(skill.get("name", "")).strip() or "未命名技能"
                icon = self.icons.skill(skill_id, name, class_id, 24)
                hits_value = skill.get("hits")
                max_hit_value = skill.get("max_hit")
                hits_text = "--" if hits_value is None else str(int(hits_value or 0))
                max_text = (
                    "--" if max_hit_value is None else format_number(max_hit_value)
                )
                canvas.create_rectangle(
                    0, top, width, bottom - 1, fill=base, outline=""
                )
                canvas.create_image(
                    10,
                    top + (row_height - 24) // 2,
                    image=icon,
                    anchor="nw",
                )
                canvas.create_text(
                    42,
                    top + row_height // 2,
                    text=name,
                    fill=TEXT,
                    anchor="w",
                    font=self._ui_font("strong"),
                )
                canvas.create_text(
                    columns["damage"],
                    top + row_height // 2,
                    text=format_number(skill.get("damage", 0)),
                    fill=TEXT,
                    anchor="e",
                    font=self._ui_font("number_strong"),
                )
                canvas.create_text(
                    columns["share"],
                    top + row_height // 2,
                    text=f"{float(skill.get('share', 0.0) or 0.0) * 100:.1f}%",
                    fill=TEXT,
                    anchor="e",
                    font=self._ui_font("number"),
                )
                canvas.create_text(
                    columns["hits"],
                    top + row_height // 2,
                    text=hits_text,
                    fill=TEXT,
                    anchor="e",
                    font=self._ui_font("number"),
                )
                canvas.create_text(
                    columns["max"],
                    top + row_height // 2,
                    text=max_text,
                    fill=TEXT,
                    anchor="e",
                    font=self._ui_font("number"),
                )
                top = bottom
        canvas.configure(scrollregion=(0, 0, width, max(height, top)))

    def _draw_history_skills_legacy(self) -> None:
        canvas = self.history_skill_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        record = self._selected_history_record()
        participants = record.get("participants", []) if record else []
        participants = participants if isinstance(participants, list) else []
        participant = next(
            (
                item
                for item in participants
                if int(item.get("actor_id", 0) or 0) == self.history_selected_actor
            ),
            None,
        )
        if not isinstance(participant, dict):
            canvas.create_text(
                width // 2,
                height // 2,
                text="暂无个人详情",
                fill=MUTED,
                font=("Microsoft YaHei UI", 9, "bold"),
            )
            canvas.configure(scrollregion=(0, 0, width, height))
            return
        targets = participant.get("targets", [])
        targets = targets if isinstance(targets, list) else []
        skills = participant.get("skills", []) if isinstance(participant, dict) else []
        skills = skills if isinstance(skills, list) else []
        if not skills and isinstance(participant, dict):
            try:
                aggregate_damage = max(0, int(participant.get("damage", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                aggregate_damage = 0
            if aggregate_damage:
                skills = [
                    {
                        "name": "未归类伤害",
                        "damage": aggregate_damage,
                        "share": 1.0,
                        "hits": None,
                        "max_hit": None,
                    }
                ]
        damage_x, share_x, hits_x, max_x = (
            int(width * 0.58),
            int(width * 0.73),
            int(width * 0.83),
            width - 12,
        )
        row_height = 34
        header_height = 28
        top = 0

        canvas.create_rectangle(0, top, width, top + header_height, fill=SURFACE, outline="")
        canvas.create_text(9, top + 14, text="伤害目标", fill=ACCENT, anchor="w", font=("Microsoft YaHei UI", 8, "bold"))
        canvas.create_text(int(width * 0.55), top + 14, text="类型", fill=MUTED, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
        canvas.create_text(int(width * 0.77), top + 14, text="伤害", fill=MUTED, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
        canvas.create_text(width - 12, top + 14, text="占个人总伤", fill=MUTED, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
        top += header_height
        if targets:
            for index, target in enumerate(targets):
                bottom = top + row_height
                base = PANEL if index % 2 == 0 else SURFACE
                kind = str(target.get("kind", "小怪"))
                color = WARN if kind == "Boss" else ACCENT
                name = str(target.get("name", "")).strip() or "未命名目标"
                entity_count = len(target.get("entity_ids", []))
                if entity_count > 1:
                    name = f"{name} ×{entity_count}"
                canvas.create_rectangle(0, top, width, bottom - 1, fill=base, outline="")
                canvas.create_text(9, top + 17, text=name, fill=TEXT, anchor="w", font=("Microsoft YaHei UI", 8, "bold"))
                canvas.create_text(int(width * 0.55), top + 17, text=kind, fill=color, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
                canvas.create_text(int(width * 0.77), top + 17, text=format_number(target.get("damage", 0)), fill=TEXT, anchor="e", font=("Segoe UI", 8, "bold"))
                canvas.create_text(width - 12, top + 17, text=f"{float(target.get('share', 0.0) or 0.0) * 100:.1f}%", fill=TEXT, anchor="e", font=("Segoe UI", 8))
                top = bottom
        else:
            canvas.create_rectangle(0, top, width, top + row_height - 1, fill=PANEL, outline="")
            canvas.create_text(9, top + 17, text="暂无目标明细", fill=MUTED, anchor="w", font=("Microsoft YaHei UI", 8))
            top += row_height

        canvas.create_rectangle(0, top, width, top + header_height, fill=SURFACE, outline="")
        canvas.create_text(9, top + 14, text="技能明细", fill=ACCENT, anchor="w", font=("Microsoft YaHei UI", 8, "bold"))
        canvas.create_text(damage_x, top + 14, text="伤害", fill=MUTED, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
        canvas.create_text(share_x, top + 14, text="占比", fill=MUTED, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
        canvas.create_text(hits_x, top + 14, text="次数", fill=MUTED, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
        canvas.create_text(max_x, top + 14, text="最大伤害", fill=MUTED, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
        top += header_height
        if not skills:
            canvas.create_rectangle(0, top, width, top + row_height - 1, fill=PANEL, outline="")
            canvas.create_text(9, top + 17, text="暂无技能明细", fill=MUTED, anchor="w", font=("Microsoft YaHei UI", 8))
            top += row_height
        for index, skill in enumerate(skills):
            bottom = top + row_height
            base = PANEL if index % 2 == 0 else SURFACE
            hits_value = skill.get("hits")
            max_hit_value = skill.get("max_hit")
            hits_text = "--" if hits_value is None else f"{int(hits_value or 0)} 次"
            max_hit_text = (
                "--" if max_hit_value is None else format_number(max_hit_value)
            )
            canvas.create_rectangle(0, top, width, bottom - 1, fill=base, outline="")
            canvas.create_text(
                9,
                top + 17,
                text=str(skill.get("name", "")).strip() or "未命名技能",
                fill=TEXT,
                anchor="w",
                font=("Microsoft YaHei UI", 8, "bold"),
            )
            canvas.create_text(
                damage_x,
                top + 17,
                text=format_number(skill.get("damage", 0)),
                fill=TEXT,
                anchor="e",
                font=("Segoe UI", 8, "bold"),
            )
            canvas.create_text(
                share_x,
                top + 17,
                text=f"{float(skill.get('share', 0.0) or 0.0) * 100:.1f}%",
                fill=TEXT,
                anchor="e",
                font=("Segoe UI", 8),
            )
            canvas.create_text(
                hits_x,
                top + 17,
                text=hits_text,
                fill=TEXT,
                anchor="e",
                font=("Microsoft YaHei UI", 8),
            )
            canvas.create_text(
                max_x,
                top + 17,
                text=max_hit_text,
                fill=TEXT,
                anchor="e",
                font=("Segoe UI", 8),
            )
            top = bottom
        canvas.configure(scrollregion=(0, 0, width, max(height, top)))

    def _toggle_history_favorite(self) -> None:
        record = self._selected_history_record()
        if record is None:
            return
        updated = self.history_store.set_favorite(
            record.get("encounter_id"), not bool(record.get("favorite"))
        )
        if updated is not None:
            self._refresh_history_records()

    def _delete_history(self) -> None:
        record = self._selected_history_record()
        if record is None:
            return
        encounter_id = record.get("encounter_id")

        def remove() -> None:
            self.history_store.delete(encounter_id)
            self.history_selected_id = ""
            self.history_selected_actor = 0
            self._refresh_history_records()

        self._show_notice(
            "删除战斗记录",
            "确定删除选中的战斗记录？此操作无法撤销。",
            parent=self.history_window,
            confirm_text="删除",
            cancel_text="取消",
            on_confirm=remove,
        )

    def _clear_unfavorited_history(self) -> None:
        self._flush_combat_history()
        unfavorited_count = self.history_store.unfavorited_count()
        if unfavorited_count <= 0:
            self._show_notice(
                "一键清理",
                "当前没有可清理的未收藏记录。",
                parent=self.history_window,
            )
            return

        def clear_unfavorited() -> None:
            self.history_store.clear_unfavorited()
            self.history_selected_id = ""
            self.history_selected_actor = 0
            self._refresh_history_records()

        self._show_notice(
            "一键清理",
            f"确定删除 {unfavorited_count} 条未收藏记录？已收藏记录会保留。",
            parent=self.history_window,
            confirm_text="一键清理",
            cancel_text="取消",
            on_confirm=clear_unfavorited,
        )

    def _share_history(self) -> None:
        record = self._selected_history_record()
        if record is None:
            return
        text = self._history_share_text(record, self.hide_names)
        self._copy_share_text(text, self.history_window)

    def _share_current(self) -> None:
        record = self.model.build_combat_record("share")
        text = self._history_share_text(record or {}, self.hide_names)
        self._copy_share_text(text, self.root)

    def _copy_share_text(self, text: str, parent: tk.Misc | None) -> None:
        if not text:
            self._show_notice("分享 DPS", "当前没有可分享的 DPS。", parent=parent)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self._show_notice("分享 DPS", "已复制到剪贴板。", parent=parent)

    @staticmethod
    def _notice_dimensions(message: object) -> tuple[int, int]:
        text = str(message or "")
        visual_lines = 0
        widest = 0
        for line in text.splitlines() or [""]:
            units = sum(2 if ord(char) > 0x7F else 1 for char in line)
            widest = max(widest, units)
            visual_lines += max(1, (units + 41) // 42)
        width = 350 if widest <= 34 else 390
        height = min(286, 132 + max(0, visual_lines - 1) * 18)
        return width, height

    def _show_notice(
        self,
        title_text: str,
        message: str,
        *,
        parent: tk.Misc | None = None,
        confirm_text: str = "确定",
        cancel_text: str = "",
        on_confirm=None,
    ) -> None:
        self._close_notice_window()
        if self.compact_mode:
            return
        anchor = parent if parent is not None and parent.winfo_exists() else self.root
        bar = tk.Frame(
            anchor,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=ACCENT if on_confirm is not None else BORDER,
        )
        self.notice_window = bar
        bar.place(
            relx=0.5,
            rely=1.0,
            y=-14,
            anchor="s",
            relwidth=0.9,
        )
        text_group = tk.Frame(bar, bg=SURFACE)
        text_group.pack(side="left", fill="both", expand=True, padx=14, pady=10)
        tk.Label(
            text_group,
            text=str(title_text),
            bg=SURFACE,
            fg=ACCENT,
            anchor="w",
            font=self._ui_font("strong"),
        ).pack(fill="x")
        tk.Label(
            text_group,
            text=str(message),
            bg=SURFACE,
            fg=TEXT,
            justify="left",
            anchor="w",
            wraplength=max(260, anchor.winfo_width() - 300),
            font=self._ui_font("small"),
        ).pack(fill="x", pady=(2, 0))
        buttons = tk.Frame(bar, bg=SURFACE)
        buttons.pack(side="right", padx=12, pady=10)

        def confirm() -> None:
            self._close_notice_window()
            if on_confirm is not None:
                on_confirm()

        if cancel_text:
            cancel = self._action_button(buttons, cancel_text, self._close_notice_window)
            cancel.configure(padx=14, pady=5, font=self._ui_font("small"))
            cancel.pack(side="right", padx=(8, 0))
        confirm_button = self._action_button(buttons, confirm_text, confirm)
        confirm_button.configure(
            bg=ACCENT,
            fg="#07110e",
            padx=14,
            pady=5,
            font=self._ui_font("strong"),
        )
        confirm_button.pack(side="right")
        bar.lift()
        if on_confirm is None and not cancel_text:
            bar.after(2800, self._close_notice_window)

    def _close_notice_window(self) -> None:
        if self.notice_window is not None and self.notice_window.winfo_exists():
            self.notice_window.destroy()
        self.notice_window = None

    def _minimize_history(self) -> None:
        if self.history_window is None or not self.history_window.winfo_exists():
            return
        self.config["history_geometry"] = self.history_window.geometry()
        self.history_window.withdraw()
        self.tray_history_hidden = True
        save_config(self.config)

    def _close_history_window(self) -> None:
        if self.history_window is not None and self.history_window.winfo_exists():
            self.config["history_geometry"] = self.restore_geometry.get(
                id(self.history_window), self.history_window.geometry()
            )
            self.restore_geometry.pop(id(self.history_window), None)
            self.history_window.destroy()
        self.history_window = None
        self.history_list_canvas = None
        self.history_list_scrollbar = None
        self.history_query_var = None
        self.history_participant_panel = None
        self.history_participant_header_canvas = None
        self.history_participant_canvas = None
        self.history_participant_scrollbar = None
        self.history_skill_canvas = None
        self.history_skill_scrollbar = None
        self.history_count_label = None
        self.history_target_label = None
        self.history_time_label = None
        self.history_metrics_label = None
        self.history_total_value = None
        self.history_dps_value = None
        self.history_team_value = None
        self.history_detail_label = None
        self.history_detail_buttons = {}
        self.history_favorite_button = None
        self.history_max_button = None
        self.backend_pages = {}
        self.backend_nav_buttons = {}
        self.backend_settings_buttons = {}
        self.backend_settings_frames = {}
        self.membership_label = None
        self.expiry_label = None
        self.update_button = None
        self.update_summary_label = None
        self.update_status_label = None
        self.update_action_button = None
        self.opacity_scale = None
        self.opacity_value_label = None
        self.settings_font_size_var = None
        self.settings_opacity_var = None
        self.settings_show_names_var = None
        self.settings_show_damage_var = None
        self.settings_show_dps_var = None
        self.settings_show_share_var = None
        self.settings_show_critical_var = None
        self.settings_font_value_label = None
        self.tray_history_hidden = False
        save_config(self.config)
        if (
            not self.closing
            and not self.authorization_resetting
            and self.root.winfo_exists()
            and not self.compact_mode
        ):
            self.root.deiconify()
            self.root.lift()
            if not self.window_locked:
                self.root.focus_force()
            self._apply_windows_style(self.root)
            self._apply_main_transparency()
            self.root.after(20, self._apply_window_lock_state)

    def _sync_action_buttons(self) -> None:
        if (
            getattr(self, "lock_button", None) is not None
            and self.lock_button.winfo_exists()
        ):
            self.lock_button._icon_name = "lock" if self.window_locked else "unlock"
            self.lock_button._description = "解锁" if self.window_locked else "锁定"
            self.lock_button._normal_color = ACCENT if self.window_locked else TEXT
            self._sync_main_icon_button_visual(self.lock_button)
        if (
            getattr(self, "privacy_button", None) is not None
            and self.privacy_button.winfo_exists()
        ):
            self.privacy_button._icon_name = "eye_off" if self.hide_names else "eye"
            self.privacy_button._description = (
                "显示名称" if self.hide_names else "隐藏名称"
            )
            self.privacy_button._normal_color = ACCENT if self.hide_names else TEXT
            self._sync_main_icon_button_visual(self.privacy_button)
        if (
            getattr(self, "pin_button", None) is not None
            and self.pin_button.winfo_exists()
        ):
            self.pin_button.configure(
                fg=ACCENT if self._preferred_main_topmost() else MUTED
            )
        if (
            getattr(self, "compact_button", None) is not None
            and self.compact_button.winfo_exists()
        ):
            self.compact_button._description = "迷你模式"
            self.compact_button._normal_color = TEXT
            self._sync_main_icon_button_visual(self.compact_button)
        reset_disabled = self.model.combat_in_progress()
        if (
            getattr(self, "reset_button", None) is not None
            and self.reset_button.winfo_exists()
        ):
            self.reset_button._disabled = reset_disabled
            self.reset_button._normal_color = SUBTLE if reset_disabled else TEXT
            self.reset_button.configure(
                cursor="arrow" if reset_disabled else "hand2"
            )
            self._sync_main_icon_button_visual(self.reset_button)

    def _sync_expiry_label(self) -> None:
        if (
            self.membership_label is None
            or not self.membership_label.winfo_exists()
            or self.expiry_label is None
            or not self.expiry_label.winfo_exists()
        ):
            return
        session = self.licensing.session
        tier = str(getattr(session, "card_tier", "normal") or "normal").casefold()
        membership = membership_label_for_card_tier(tier)
        if self.membership_label.cget("text") != membership:
            self.membership_label.configure(text=membership)
        expires_at = getattr(session, "expires_at", None)
        if tier == "partner":
            text = "有效时间：永久"
        elif expires_at is not None:
            text = f"有效至：{expires_at.strftime('%Y-%m-%d')}"
        else:
            text = "有效时间：当前会话"
        if self.expiry_label.cget("text") != text:
            self.expiry_label.configure(text=text)

    def _draw_monster_hp(self) -> None:
        canvas = self.monster_hp_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        monster = self.model.current_monster()
        if monster is None:
            canvas.create_rectangle(
                1,
                3,
                width - 1,
                height - 3,
                fill="#15171a",
                outline="#34383d",
            )
            canvas.create_text(
                width // 2,
                height // 2,
                text="Lv.--   暂无目标   -- / --   --",
                fill=MUTED,
                font=self._ui_font("strong"),
            )
            return
        display_name = monster.name.strip()
        if boss_name_is_placeholder(display_name):
            display_name = "Boss"
        if not display_name:
            display_name = (
                "Boss" if self.model._monster_rank(monster) > 0 else "怪物"
            )
        level_text = f"Lv.{monster.level}" if monster.level else "Lv.--"
        current_hp = monster.current_hp
        max_hp = monster.max_hp
        hp_ceiling = (
            max_hp
            if max_hp is not None and max_hp > 0
            else monster.observed_max_hp
        )
        pending_hp = current_hp is None
        if current_hp is None:
            hp_text = f"-- / {format_number(max_hp)}" if max_hp is not None else "-- / --"
            ratio = 1.0
        elif hp_ceiling is not None and hp_ceiling > 0:
            hp_text = f"{format_number(current_hp)} / {format_number(hp_ceiling)}"
            ratio = min(1.0, max(0.0, current_hp / hp_ceiling))
        else:
            hp_text = f"{format_number(current_hp)} / --"
            ratio = 0.0
        canvas.create_rectangle(
            1,
            3,
            width - 1,
            height - 3,
            fill="#351719",
            outline="#8f332d",
        )
        if ratio > 0:
            canvas.create_rectangle(
                2,
                4,
                max(3, int((width - 3) * ratio)),
                height - 4,
                fill="#5b2b2c" if pending_hp else "#8d302b",
                outline="",
            )
        percentage_text = "--"
        if current_hp is not None and hp_ceiling is not None and hp_ceiling > 0:
            percentage = ratio * 100.0
            percentage_text = (
                f"{percentage:.0f}%"
                if percentage in (0.0, 100.0)
                else f"{percentage:.1f}%"
            )
        name_width = max(80, width - 270)
        shown_name = self._fit_main_actor_name(display_name, name_width)
        status_text = "   ".join(
            (level_text, shown_name, hp_text, percentage_text)
        )
        canvas.create_text(
            width // 2,
            height // 2,
            text=status_text,
            fill="#fff7f5",
            font=self._ui_font("strong"),
        )

    def _draw_main_header(self) -> None:
        canvas = self.header_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        columns = self._main_columns(width)
        compact = self.compact_mode
        header_font = self._ui_font("small" if compact else "strong")
        captions = (
            {
                "damage": "伤害",
                "dps": "DPS",
                "share": "占比",
                "critical": "暴击",
            }
            if compact
            else {
                "damage": "总伤害",
                "dps": "秒伤",
                "share": "占比",
                "critical": "暴击率",
            }
        )
        canvas.create_text(
            34 if compact else 42,
            14,
            text="玩家" if compact else "玩家名称",
            fill=TEXT,
            anchor="w",
            font=header_font,
        )
        for key in ("damage", "dps", "share", "critical"):
            if key not in columns:
                continue
            canvas.create_text(
                columns[key],
                14,
                text=captions[key],
                fill=TEXT,
                anchor="e",
                font=header_font,
            )
        canvas.create_line(0, 27, width, 27, fill=BORDER)
        if compact:
            canvas.create_rectangle(
                width - 64,
                2,
                width - 34,
                26,
                fill=SURFACE,
                outline="",
                tags=("compact_lock", "compact_lock_bg"),
            )
            lock_icon = self.icons.toolbar(
                "lock" if self.window_locked else "unlock",
                14,
                ACCENT if self.window_locked else TEXT,
            )
            canvas.create_image(
                width - 49,
                14,
                image=lock_icon,
                tags=("compact_lock",),
            )
            canvas.tag_bind(
                "compact_lock", "<Button-1>", self._toggle_lock_from_header
            )
            canvas.tag_bind(
                "compact_lock", "<Enter>", self._compact_lock_enter
            )
            canvas.tag_bind(
                "compact_lock", "<Leave>", self._compact_lock_leave
            )
            canvas.create_rectangle(
                width - 32,
                2,
                width - 2,
                26,
                fill=SURFACE,
                outline="",
                tags=("compact_restore", "compact_restore_bg"),
            )
            restore_icon = self.icons.toolbar("expand", 15, TEXT)
            canvas.create_image(
                width - 17,
                14,
                image=restore_icon,
                tags=("compact_restore",),
            )
            canvas.tag_bind(
                "compact_restore",
                "<Button-1>",
                self._restore_compact_from_header,
            )
            canvas.tag_bind(
                "compact_restore", "<Enter>", self._compact_restore_enter
            )
            canvas.tag_bind(
                "compact_restore", "<Leave>", self._compact_restore_leave
            )

    def _toggle_lock_from_header(self, _event=None) -> str:
        self.toggle_window_lock()
        return "break"

    def _compact_lock_enter(self, _event=None) -> None:
        self.header_canvas.configure(cursor="hand2")
        self.header_canvas.itemconfigure("compact_lock_bg", fill=PANEL_2)

    def _compact_lock_leave(self, _event=None) -> None:
        self.header_canvas.configure(cursor="")
        self.header_canvas.itemconfigure("compact_lock_bg", fill=SURFACE)

    def _restore_compact_from_header(self, _event=None) -> str:
        if self.compact_mode and not self.window_locked:
            self.toggle_compact_mode()
        return "break"

    def _compact_restore_enter(self, _event=None) -> None:
        self.header_canvas.configure(cursor="hand2")
        self.header_canvas.itemconfigure("compact_restore_bg", fill=PANEL_2)

    def _compact_restore_leave(self, _event=None) -> None:
        self.header_canvas.configure(cursor="")
        self.header_canvas.itemconfigure("compact_restore_bg", fill=SURFACE)

    def _main_columns(self, width: int) -> dict[str, int]:
        compact = bool(getattr(self, "compact_mode", False))
        enabled = list(self._enabled_main_metrics())
        content_width = max(
            120, width - (MINI_ACTION_AREA_WIDTH if compact else 0)
        )
        right_edge = max(100, content_width - (8 if compact else 18))
        if compact:
            if len(enabled) <= 1:
                name_ratio = 0.55
            elif len(enabled) == 2:
                name_ratio = 0.45
            else:
                name_ratio = 0.37
        else:
            name_ratio = 0.52 if len(enabled) <= 2 else 0.42
        name_limit = int(content_width * name_ratio)
        columns = {"name_limit": name_limit}
        if not enabled:
            return columns
        step = max(1, (right_edge - name_limit) / len(enabled))
        for index, key in enumerate(enabled, start=1):
            columns[key] = int(name_limit + step * index)
        return columns

    def _main_row_height(self) -> int:
        if bool(getattr(self, "compact_mode", False)):
            return max(28, self.ui_font_size + 12)
        return max(32, self.ui_font_size + 18)

    def _scroll_main(self, event) -> str:
        if bool(getattr(self, "window_locked", False)):
            return "break"
        try:
            delta = int(getattr(event, "delta", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            delta = 0
        try:
            button = int(getattr(event, "num", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            # Tk reports "??" here for native Windows <MouseWheel> events.
            button = 0
        if button == 4:
            direction, steps = -1, 1
        elif button == 5:
            direction, steps = 1, 1
        elif delta:
            direction = -1 if delta > 0 else 1
            steps = max(1, abs(delta) // 120)
        else:
            return "break"
        canvas = self.rows_canvas
        viewport_height = max(1, canvas.winfo_height())
        content_height = max(
            viewport_height,
            int(getattr(self, "main_scroll_content_height", viewport_height)),
        )
        maximum_offset = max(0, content_height - viewport_height)
        current_offset = min(
            maximum_offset,
            max(0, int(getattr(self, "main_scroll_offset", 0))),
        )
        self.main_scroll_offset = min(
            maximum_offset,
            max(
                0,
                current_offset + direction * steps * self._main_row_height(),
            ),
        )
        canvas.yview_moveto(self.main_scroll_offset / max(1, content_height))
        self._draw_main_scroll_indicator(
            canvas.winfo_width(), viewport_height, content_height
        )
        return "break"

    def _draw_main_scroll_indicator(
        self, width: int, viewport_height: int, content_height: int
    ) -> None:
        canvas = self.rows_canvas
        canvas.delete("main_scroll_indicator")
        if content_height <= viewport_height:
            return
        maximum_offset = max(1, content_height - viewport_height)
        thumb_height = max(
            14, int(viewport_height * viewport_height / content_height)
        )
        travel = max(1, viewport_height - thumb_height - 4)
        thumb_top = 2 + int(
            travel * min(maximum_offset, self.main_scroll_offset) / maximum_offset
        )
        visible_top = float(canvas.canvasy(0))
        canvas.create_rectangle(
            max(0, width - 4),
            visible_top + thumb_top,
            max(1, width - 1),
            visible_top + min(viewport_height - 2, thumb_top + thumb_height),
            fill=MUTED,
            outline="",
            tags=("main_scroll_indicator",),
        )

    def _shown_actor_name(self, actor_id: int) -> str:
        if not self.hide_names:
            display_name = self.model.display_name(actor_id)
            if display_name:
                return display_name
        ordered = list(self.model.friend_order)
        if actor_id not in ordered:
            ordered.extend(value for value in self.model.stats if value not in ordered)
        try:
            index = ordered.index(actor_id)
        except ValueError:
            index = len(ordered)
        return f"玩家{index + 1}"

    def _fit_main_actor_name(self, value: object, maximum_width: int) -> str:
        text = str(value or "").strip()
        if not text or maximum_width <= 0:
            return text
        font = self._ui_font("strong")
        if font.measure(text) <= maximum_width:
            return text
        suffix = "..."
        while text and font.measure(text + suffix) > maximum_width:
            text = text[:-1]
        return text + suffix if text else suffix

    @staticmethod
    def _actor_critical_text(actor: ActorStats | None) -> str:
        if (
            actor is None
            or not actor.damage_hits
            or actor.critical_hits is None
        ):
            return "--"
        return f"{actor.critical_hits / actor.damage_hits * 100:.1f}%"

    def _profession_info(self, class_id: int | None) -> tuple[str, str]:
        value = self.professions.get(str(class_id or 0), {})
        name = value.get("name", "") if isinstance(value, dict) else ""
        color = PROFESSION_COLORS.get(int(class_id or 0), SUBTLE)
        return (str(name).strip() or "职业识别中", color)

    def _draw_main_rows(self) -> None:
        self._draw_main_rows_on_canvas(
            self.rows_canvas, update_scroll_state=True
        )
        overlay = self.main_content_overlay_rows
        if (
            overlay is not None
            and overlay.winfo_exists()
            and overlay.winfo_ismapped()
        ):
            self._draw_main_rows_on_canvas(
                overlay,
                update_scroll_state=False,
                draw_empty_state=False,
            )

    def _draw_main_rows_on_canvas(
        self,
        canvas: tk.Canvas,
        *,
        update_scroll_state: bool,
        draw_empty_state: bool = True,
    ) -> None:
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        rows = sorted(self.model.current_stats(), key=lambda item: item.damage, reverse=True)
        total = sum(row.damage for row in rows)
        duration = self.model.duration()
        highest_damage = max((row.damage for row in rows), default=0)
        columns = self._main_columns(width)
        compact = bool(getattr(self, "compact_mode", False))
        row_height = self._main_row_height()
        icon_size = 20 if compact else 24
        name_x = 31 if compact else 38
        name_font = self._ui_font("small" if compact else "strong")
        number_font = self._ui_font("number_small" if compact else "number_strong")
        secondary_number_font = self._ui_font(
            "number_small" if compact else "number"
        )
        if not rows:
            if update_scroll_state:
                self.main_scroll_offset = 0
                self.main_scroll_content_height = height
            if draw_empty_state:
                canvas.create_text(
                    width // 2,
                    height // 2,
                    text="暂无伤害记录",
                    fill=MUTED,
                    font=self._ui_font("strong"),
                )
            canvas.configure(scrollregion=(0, 0, width, height))
            canvas.yview_moveto(0.0)
            return
        for index, row in enumerate(rows):
            top = index * row_height
            bottom = top + row_height
            share = row.damage / total if total else 0.0
            bar_ratio = relative_damage_bar_ratio(row.damage, highest_damage)
            class_id = self.model.actor_profession_id(row.actor_id)
            _profession, color = self._profession_info(class_id)
            bar = blend_color(BG, color, 0.22)
            tag = f"actor:{row.actor_id}"
            canvas.create_rectangle(
                0,
                top,
                max(5, int((width - 12) * bar_ratio)),
                bottom,
                fill=bar,
                outline="",
                tags=(tag,),
            )
            icon = self.icons.profession(class_id, icon_size)
            canvas.create_image(
                5 if compact else 6,
                top + (row_height - icon_size) // 2,
                image=icon,
                anchor="nw",
                tags=(tag,),
            )
            critical_text = self._actor_critical_text(row)
            actor_name = self._fit_main_actor_name(
                self._shown_actor_name(row.actor_id),
                max(24, columns["name_limit"] - name_x - 6),
            )
            canvas.create_text(
                name_x,
                top + row_height // 2,
                text=actor_name,
                fill=color,
                anchor="w",
                font=name_font,
                tags=(tag,),
            )
            if "damage" in columns:
                canvas.create_text(
                    columns["damage"],
                    top + row_height // 2,
                    text=format_number(row.damage),
                    fill=TEXT,
                    anchor="e",
                    font=number_font,
                    tags=(tag,),
                )
            if "dps" in columns:
                canvas.create_text(
                    columns["dps"],
                    top + row_height // 2,
                    text=format_number(row.damage / duration if duration else 0),
                    fill=TEXT,
                    anchor="e",
                    font=number_font,
                    tags=(tag,),
                )
            if "share" in columns:
                canvas.create_text(
                    columns["share"],
                    top + row_height // 2,
                    text=f"{share * 100:.1f}%",
                    fill=TEXT,
                    anchor="e",
                    font=secondary_number_font,
                    tags=(tag,),
                )
            if "critical" in columns:
                canvas.create_text(
                    columns["critical"],
                    top + row_height // 2,
                    text=critical_text,
                    fill=TEXT,
                    anchor="e",
                    font=secondary_number_font,
                    tags=(tag,),
                )
        content_height = max(height, len(rows) * row_height)
        scroll_offset = min(
            max(0, content_height - height),
            max(0, int(getattr(self, "main_scroll_offset", 0))),
        )
        if update_scroll_state:
            self.main_scroll_content_height = content_height
            self.main_scroll_offset = scroll_offset
        canvas.configure(
            yscrollincrement=1,
            scrollregion=(0, 0, width, content_height),
        )
        canvas.yview_moveto(scroll_offset / max(1, content_height))
        if update_scroll_state:
            self._draw_main_scroll_indicator(width, height, content_height)

    def _drag_start(self, event, window: tk.Misc) -> None:
        if window is self.root and self.window_locked:
            return
        if id(window) in self.restore_geometry:
            return
        self.drag_state[id(window)] = (
            event.x_root - window.winfo_x(),
            event.y_root - window.winfo_y(),
        )

    def _drag_move(self, event, window: tk.Misc) -> None:
        if window is self.root and self.window_locked:
            return
        offset = self.drag_state.get(id(window))
        if not offset or id(window) in self.restore_geometry:
            return
        window.geometry(f"+{event.x_root - offset[0]}+{event.y_root - offset[1]}")

    def _resize_start(self, event, window: tk.Misc) -> None:
        if window is self.root and self.window_locked:
            return
        self.resize_state[id(window)] = (
            event.x_root,
            event.y_root,
            window.winfo_width(),
            window.winfo_height(),
        )

    def _resize_move(self, event, window: tk.Misc, minimum_width: int, minimum_height: int) -> None:
        if window is self.root and self.window_locked:
            return
        state = self.resize_state.get(id(window))
        if not state or id(window) in self.restore_geometry:
            return
        x, y, width, height = state
        window.geometry(
            f"{max(minimum_width, width + event.x_root - x)}x"
            f"{max(minimum_height, height + event.y_root - y)}"
        )

    def _work_area(self, window: tk.Misc) -> tuple[int, int, int, int]:
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class MonitorInfo(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                    ]

                info = MonitorInfo()
                info.cbSize = ctypes.sizeof(info)
                monitor = ctypes.windll.user32.MonitorFromWindow(window.winfo_id(), 2)
                if monitor and ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    work = info.rcWork
                    return work.left, work.top, work.right - work.left, work.bottom - work.top
            except (AttributeError, OSError):
                pass
        return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()

    def _toggle_maximize(self, window: tk.Misc) -> None:
        if window is self.root and self.window_locked:
            return
        key = id(window)
        if window is self.root:
            button = self.max_button
        elif window is self.history_window:
            button = self.history_max_button
        else:
            button = getattr(self, "skill_max_button", None)
        if key in self.restore_geometry:
            window.geometry(self.restore_geometry.pop(key))
            if button is not None:
                button.configure(text="□")
            return
        self.restore_geometry[key] = window.geometry()
        x, y, width, height = self._work_area(window)
        window.geometry(f"{width}x{height}+{x}+{y}")
        if button is not None:
            button.configure(text="❐")

    def minimize(self) -> None:
        if self.closing:
            return
        self._remember_root_geometry()
        self.tray_skill_hidden = bool(
            self.skill_window is not None
            and self.skill_window.winfo_exists()
            and self.skill_window.state() == "normal"
        )
        self.tray_history_hidden = bool(
            self.history_window is not None
            and self.history_window.winfo_exists()
            and self.history_window.state() == "normal"
        )
        self.tray_feedback_hidden = bool(
            self.feedback_window is not None
            and self.feedback_window.winfo_exists()
            and self.feedback_window.state() == "normal"
        )
        if self.tray_skill_hidden:
            self.skill_window.withdraw()
        if self.tray_history_hidden:
            self.history_window.withdraw()
        if self.tray_feedback_hidden:
            self.feedback_window.withdraw()
        self._destroy_unlock_window()
        if (
            self.main_content_overlay_window is not None
            and self.main_content_overlay_window.winfo_exists()
        ):
            self.main_content_overlay_window.withdraw()
        self.root.withdraw()
        save_config(self.config)

    def show_from_tray(self) -> None:
        if self.closing:
            return
        if self.login_window is not None and self.login_window.winfo_exists():
            login_height = 354 if not self.process_is_elevated else 286
            self.login_window.geometry(
                self._visible_geometry(
                    self.login_window.geometry(), 420, login_height
                )
            )
            self.login_window.deiconify()
            self.login_window.lift()
            self.login_window.focus_force()
            return
        self.root.geometry(
            self._visible_geometry(
                self.root.geometry(),
                MINI_DEFAULT_WIDTH if self.compact_mode else 590,
                MINI_DEFAULT_HEIGHT if self.compact_mode else 400,
            )
        )
        self.root.overrideredirect(True)
        self.root.deiconify()
        self.root.lift()
        if not self.window_locked:
            self.root.focus_force()
        self._apply_windows_style(self.root)
        self._apply_main_transparency()
        self.root.after(20, self._apply_window_lock_state)
        if (
            self.tray_skill_hidden
            and self.skill_window is not None
            and self.skill_window.winfo_exists()
        ):
            self.skill_window.geometry(
                self._visible_geometry(self.skill_window.geometry(), 560, 360)
            )
            self.skill_window.deiconify()
            self.skill_window.lift()
        if (
            self.tray_history_hidden
            and self.history_window is not None
            and self.history_window.winfo_exists()
        ):
            self.history_window.geometry(
                self._visible_geometry(
                    self.history_window.geometry(), BACKEND_WIDTH, BACKEND_HEIGHT
                )
            )
            self.history_window.deiconify()
            self.history_window.lift()
        if (
            self.tray_feedback_hidden
            and self.feedback_window is not None
            and self.feedback_window.winfo_exists()
        ):
            self.feedback_window.geometry(
                self._visible_geometry(self.feedback_window.geometry(), 500, 418)
            )
            self.feedback_window.deiconify()
            self.feedback_window.lift()
        self.tray_skill_hidden = False
        self.tray_history_hidden = False
        self.tray_feedback_hidden = False

    def _restore_borderless(self) -> None:
        if self.closing:
            return
        if self.root.state() == "normal":
            self.root.overrideredirect(True)
            self._apply_windows_style(self.root)
            self._apply_main_transparency()
            self.root.lift()
        else:
            self.root.after(120, self._restore_borderless)

    @staticmethod
    def _apply_windows_style(window: tk.Misc) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes

            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
            dark = ctypes.c_int(1)
            corner = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        except (AttributeError, OSError):
            pass

    def _apply_main_transparency(self) -> None:
        if self.closing or not self.root.winfo_exists():
            return
        try:
            self.root.update_idletasks()
            self.root.configure(bg=BG)
            self.root.attributes("-alpha", self.window_alpha)
            if bool(getattr(self, "window_locked", False)):
                self._set_window_click_through(self.root, True)
        except tk.TclError:
            pass
        self._schedule_main_content_overlay_sync()

    def _dispatch_message(self, kind: str, payload: object) -> None:
        if kind == "event":
            self._ingest_combat_event(payload)
        elif kind == "identity":
            self.model.ingest_identity(payload)
        elif kind == "actor_merge":
            self.model.merge_actor(payload)
        elif kind == "party":
            self.model.ingest_party(payload)
        elif kind == "profile":
            self.model.ingest_profile(payload)
        elif kind == "monster":
            self.model.ingest_monster(payload)
        elif kind == "team_stat":
            self.model.ingest_team_stat(payload)
        elif kind == "stage_summary":
            self._ingest_stage_summary(payload)
        elif kind == "combat_state":
            self.model.ingest_combat_state(payload)
        elif kind == "life":
            self.model.ingest_life(payload)
        elif kind == "scene":
            self.model.ingest_scene(payload)
        elif kind == "tray_restore":
            self.show_from_tray()
        elif kind == "tray_exit":
            self.close()
        elif kind == "feedback_result":
            self._handle_feedback_result(payload)
        elif kind == "update_check_result":
            self._handle_update_check_result(payload)
        elif kind == "update_available" and isinstance(payload, UpdateInfo):
            self._show_update_window(payload)
        elif kind == "update_download_progress":
            self._handle_update_download_progress(payload)
        elif kind == "update_downloaded":
            self._handle_update_downloaded(payload)
        elif kind == "update_download_failed":
            self._handle_update_download_failed(payload)
        elif kind == "name":
            self.model.ingest_name(payload)
        elif kind == "skill_name":
            if self.model.ingest_skill_name(payload):
                self._save_preferences()
        elif kind == "connected":
            self.connected = True
            self.game_pid = int(payload.get("pid", 0) or 0)
            if self.heartbeat_worker is not None:
                self.heartbeat_worker.update_state(
                    using=True,
                    character_name=self.model.display_name(
                        int(self.model.self_id or 0)
                    ),
                    game_pid=self.game_pid,
                )
            self.dot.configure(fg=ACCENT)
            self.status_label.configure(text="运行中", fg=MUTED)
        elif kind == "waiting":
            self.connected = False
            self.game_pid = 0
            if self.heartbeat_worker is not None:
                self.heartbeat_worker.update_state(using=False)
            self.dot.configure(fg=WARN)
            self.status_label.configure(text="待机", fg=MUTED)
        elif kind in ("error", "fatal"):
            self.connected = False
            self.game_pid = 0
            if self.heartbeat_worker is not None:
                self.heartbeat_worker.update_state(using=False)
            self.dot.configure(fg=ERROR)
            self.status_label.configure(
                text=chinese_error_message(payload), fg=ERROR
            )
        elif kind == "license_required":
            self._return_to_login(str(payload))

    def _drain_messages(self) -> None:
        if self.closing:
            return

        control_queue = getattr(self, "control_messages", None)
        control_drained = control_queue is None
        if control_queue is not None:
            try:
                for _index in range(CONTROL_MESSAGE_DRAIN_BATCH_SIZE):
                    kind, payload = control_queue.get_nowait()
                    self._dispatch_message(kind, payload)
                    if self.closing:
                        return
            except queue.Empty:
                control_drained = True

        queue_drained = False
        deadline = time.perf_counter() + MESSAGE_DRAIN_TIME_BUDGET_SECONDS
        try:
            for _index in range(MESSAGE_DRAIN_BATCH_SIZE):
                kind, payload = self.messages.get_nowait()
                self._dispatch_message(kind, payload)
                if self.closing or time.perf_counter() >= deadline:
                    break
        except queue.Empty:
            queue_drained = True
        if not self.closing:
            # Continuous combat can keep the capture queue non-empty forever.
            # Control results are handled first, then combat is bounded so Tk
            # still receives paint and input events between batches.
            delay = 50 if queue_drained and control_drained else 1
            self.root.after(delay, self._drain_messages)

    def _ingest_combat_event(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self.model.ingest(payload)

    def _render(self) -> None:
        if self.closing:
            return
        now = time.time()
        self.model.finalize_if_idle(now)
        self._flush_combat_history()
        duration = self.model.duration(now)
        total = sum(row.damage for row in self.model.current_stats())
        total_dps = total / duration if duration else 0.0
        self.total_value.configure(text=format_number(total))
        dps_text = format_number(total_dps)
        self.dps_value.configure(text=dps_text)
        self._draw_main_content_overlay_dps()
        state = "战斗中" if self.model.active(now) else ("已结束" if total else "待机")
        self.time_value.configure(
            text=format_duration(duration),
            fg=ACCENT if state == "战斗中" else TEXT,
        )
        if self.heartbeat_worker is not None:
            self.heartbeat_worker.update_state(
                using=self.connected,
                character_name=(
                    self.model.display_name(int(self.model.self_id or 0))
                    if self.model.self_id is not None
                    else ""
                ),
                game_pid=self.game_pid,
            )
        if not self.compact_mode:
            self._draw_monster_hp()
        self._draw_main_rows()
        self._render_skill_details()
        self._sync_action_buttons()
        self._sync_expiry_label()
        self._sync_unlock_window_position()
        self._schedule_main_content_overlay_sync()
        self.root.after(180, self._render)

    def toggle_names(self) -> None:
        self.hide_names = not self.hide_names
        if self.settings_show_names_var is not None:
            self.settings_show_names_var.set(not self.hide_names)
        self._sync_action_buttons()
        self._save_preferences()
        self._draw_main_rows()
        self._render_skill_details()
        self._draw_history_participants()

    def toggle_topmost(self) -> None:
        if getattr(self, "window_locked", False):
            return
        value = not self._preferred_main_topmost()
        self._set_main_topmost(value)
        if self.skill_window is not None and self.skill_window.winfo_exists():
            self.skill_window.attributes("-topmost", value)
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.attributes("-topmost", value)
        if self.feedback_window is not None and self.feedback_window.winfo_exists():
            self.feedback_window.attributes("-topmost", value)
        self._sync_action_buttons()
        self._save_preferences()

    def _set_window_alpha(self, value, *, persist: bool = True) -> None:
        try:
            percent = float(value)
        except (TypeError, ValueError, OverflowError):
            return
        if 0.0 <= percent <= 1.0:
            percent *= 100.0
        percent = min(100, max(MIN_WINDOW_ALPHA_PERCENT, round(percent)))
        self.window_alpha = percent / 100.0
        self._apply_main_transparency()
        if (
            self.opacity_value_label is not None
            and self.opacity_value_label.winfo_exists()
        ):
            self.opacity_value_label.configure(text=f"{percent}%")
        if self.settings_opacity_var is not None:
            try:
                if int(self.settings_opacity_var.get()) != percent:
                    self.settings_opacity_var.set(percent)
            except tk.TclError:
                pass
        self.config["alpha"] = self.window_alpha
        if persist:
            save_config(self.config)

    def reset(self) -> None:
        if self.model.combat_in_progress():
            return
        self.model.reset(
            keep_identity=True,
            keep_monsters=True,
            preserve_active_target=True,
            archive_reason="manual_reset",
        )
        self._flush_combat_history()
        self._draw_main_rows()
        self._render_skill_details()

    def show_skill_details(self, actor_id: int) -> None:
        if self.compact_mode:
            return
        self.skill_actor_id = int(actor_id)
        if self.skill_window is None or not self.skill_window.winfo_exists():
            self._build_skill_window()
        else:
            self.skill_window.deiconify()
            self.skill_window.lift()
            self.skill_window.focus_force()
        self._render_skill_details()

    def _initial_skill_geometry(self, x: int, y: int) -> str:
        saved = str(self.config.get("skill_geometry", ""))
        match = re.fullmatch(r"\d+x\d+([+-]\d+[+-]\d+)", saved)
        suffix = match.group(1) if match else f"+{x}+{y}"
        if int(self.config.get("skill_layout_version", 0)) < 3:
            return self._visible_geometry(f"580x390{suffix}", 580, 390)
        return self._visible_geometry(saved if match else f"580x390{suffix}", 580, 390)

    def _build_skill_window(self) -> None:
        x = max(0, self.root.winfo_rootx() + 36)
        y = max(0, self.root.winfo_rooty() + 36)
        window = tk.Toplevel(self.root)
        self.skill_window = window
        window.title(f"{APP_TITLE} · 个人详情")
        window.configure(bg=BORDER)
        window.geometry(self._initial_skill_geometry(x, y))
        window.minsize(520, 340)
        window.attributes("-topmost", bool(self.root.attributes("-topmost")))
        window.attributes("-alpha", 1.0)
        window.overrideredirect(True)
        window.protocol("WM_DELETE_WINDOW", self._close_skill_window)
        window.bind("<Escape>", lambda _event: self._close_skill_window())

        shell = tk.Frame(window, bg=BORDER)
        shell.pack(fill="both", expand=True)
        body = tk.Frame(shell, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        titlebar = tk.Frame(body, bg=SURFACE, height=34)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        self._bind_drag(titlebar, window)
        logo_image = self.icons.app_logo(18)
        logo = tk.Label(titlebar, image=logo_image, bg=SURFACE, bd=0)
        logo.image = logo_image
        logo.pack(side="left", padx=(8, 2), pady=8)
        self._bind_drag(logo, window)
        skill_title = tk.Label(
            titlebar,
            text="个人伤害详情",
            bg=SURFACE,
            fg=TEXT,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        skill_title.pack(side="left", padx=(2, 8))
        self._bind_drag(skill_title, window)
        close_button = self._label_button(
            titlebar,
            "×",
            self._close_skill_window,
            width=34,
            hover="#7f2d35",
            fg="#c5ccd3",
            font=("Segoe UI", 13),
        )
        close_button.pack(side="right", fill="y")
        self.skill_max_button = self._label_button(
            titlebar,
            "□",
            lambda: self._toggle_maximize(window),
            width=34,
            font=("Segoe UI", 10),
        )
        self.skill_max_button.pack(side="right", fill="y")

        hero = tk.Frame(body, bg=BG, height=66)
        hero.pack(fill="x", padx=8, pady=(7, 5))
        hero.pack_propagate(False)
        self.skill_profession_icon = tk.Label(hero, bg=BG, bd=0)
        self.skill_profession_icon.pack(side="left", padx=(2, 7), pady=12)
        identity = tk.Frame(hero, bg=BG)
        identity.pack(side="left", fill="both", expand=True)
        self.skill_title_label = tk.Label(
            identity,
            text="技能伤害详情",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.skill_title_label.pack(fill="x", pady=(8, 0))
        self.skill_combat_metrics_label = tk.Label(
            identity,
            text="暴击率  --     死亡  0 次",
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 8),
        )
        self.skill_combat_metrics_label.pack(fill="x", pady=(2, 7))

        totals = tk.Frame(hero, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        totals.pack(side="right", fill="y", pady=5)
        self.skill_total_label = tk.Label(
            totals,
            text="总伤害  0",
            bg=SURFACE,
            fg=TEXT,
            padx=10,
            anchor="e",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.skill_total_label.pack(fill="both", expand=True)
        self.skill_dps_label = tk.Label(
            totals,
            text="DPS  0",
            bg=SURFACE,
            fg=MUTED,
            padx=10,
            anchor="e",
            font=("Microsoft YaHei UI", 8),
        )
        self.skill_dps_label.pack(fill="both", expand=True)

        tabs = tk.Frame(body, bg=BG, height=31)
        tabs.pack(fill="x", padx=8, pady=(0, 4))
        tabs.pack_propagate(False)
        self.skill_mode_buttons = {}
        for mode, text in (("skills", "技能明细"), ("targets", "伤害目标")):
            tab = tk.Label(
                tabs,
                text=text,
                bg=BG,
                fg=MUTED,
                padx=13,
                pady=5,
                cursor="hand2",
                font=("Microsoft YaHei UI", 8, "bold"),
            )
            tab.pack(side="left", fill="y")
            tab.bind(
                "<Button-1>",
                lambda _event, selected_mode=mode: self._set_skill_detail_mode(
                    selected_mode
                ),
            )
            tab.bind("<Enter>", lambda _event, widget=tab: widget.configure(fg=TEXT))
            tab.bind("<Leave>", lambda _event: self._sync_skill_detail_tabs())
            self.skill_mode_buttons[mode] = tab
        self._sync_skill_detail_tabs()

        panel = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(fill="both", expand=True, padx=8, pady=(0, 5))
        self.skill_header_canvas = tk.Canvas(panel, height=30, bg=SURFACE, bd=0, highlightthickness=0)
        self.skill_header_canvas.pack(fill="x")
        self.skill_header_canvas.bind("<Configure>", lambda _event: self._draw_skill_header())
        self.skill_rows_canvas = tk.Canvas(
            panel,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            yscrollincrement=38,
        )
        self.skill_rows_canvas.pack(fill="both", expand=True)
        self.skill_rows_canvas.bind(
            "<MouseWheel>",
            lambda event: self.skill_rows_canvas.yview_scroll(
                -1 if event.delta > 0 else 1, "units"
            ),
        )
        self.skill_rows_canvas.bind("<Configure>", lambda _event: self._draw_skill_rows())

        grip = tk.Label(body, text="◢", bg=BG, fg=SUBTLE, cursor="size_nw_se", font=("Segoe UI", 9))
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", lambda event: self._resize_start(event, window))
        grip.bind("<B1-Motion>", lambda event: self._resize_move(event, window, 520, 340))
        window.after(20, lambda: self._apply_windows_style(window))

    def _set_skill_detail_mode(self, mode: str) -> None:
        if mode not in {"skills", "targets"}:
            return
        self.skill_detail_mode = mode
        self._sync_skill_detail_tabs()
        self._draw_skill_header()
        self._draw_skill_rows()

    def _sync_skill_detail_tabs(self) -> None:
        for mode, button in self.skill_mode_buttons.items():
            selected = mode == self.skill_detail_mode
            button.configure(
                bg=PANEL_2 if selected else BG,
                fg=ACCENT if selected else MUTED,
            )

    @staticmethod
    def _skill_columns(width: int) -> tuple[int, int, int, int]:
        return int(width * 0.57), int(width * 0.73), int(width * 0.84), width - 17

    def _draw_skill_header(self) -> None:
        if self.skill_header_canvas is None:
            return
        canvas = self.skill_header_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        font = ("Microsoft YaHei UI", 8, "bold")
        if self.skill_detail_mode == "targets":
            kind_x, damage_x, share_x = int(width * 0.58), int(width * 0.79), width - 17
            canvas.create_text(12, 15, text="目标", fill=MUTED, anchor="w", font=font)
            canvas.create_text(kind_x, 15, text="类型", fill=MUTED, anchor="e", font=font)
            canvas.create_text(damage_x, 15, text="伤害", fill=MUTED, anchor="e", font=font)
            canvas.create_text(share_x, 15, text="占比", fill=MUTED, anchor="e", font=font)
        else:
            damage_x, share_x, hits_x, max_x = self._skill_columns(width)
            canvas.create_text(36, 15, text="技能", fill=MUTED, anchor="w", font=font)
            canvas.create_text(damage_x, 15, text="伤害", fill=MUTED, anchor="e", font=font)
            canvas.create_text(share_x, 15, text="占比", fill=MUTED, anchor="e", font=font)
            canvas.create_text(hits_x, 15, text="次数", fill=MUTED, anchor="e", font=font)
            canvas.create_text(max_x, 15, text="最大伤害", fill=MUTED, anchor="e", font=font)
        canvas.create_line(0, 29, width, 29, fill=BORDER)

    def _draw_skill_rows(self) -> None:
        if self.skill_rows_canvas is None or self.skill_actor_id is None:
            return
        if self.skill_detail_mode == "targets":
            self._draw_skill_target_rows()
            return
        canvas = self.skill_rows_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        actor = self.model.stats.get(self.skill_actor_id)
        stage_skill_snapshot = self.model.active_stage_skill_snapshot(
            self.skill_actor_id, actor
        )
        skills = sorted(actor.skills.values(), key=lambda item: item.damage, reverse=True) if actor else []
        total = actor.damage if actor else 0
        skill_rows = [
            (
                skill.skill_id,
                self.model.display_skill_name(self.skill_actor_id, skill.skill_id),
                skill.damage,
                (
                    None
                    if stage_skill_snapshot is not None
                    and skill.skill_id == 0
                    else skill.hits
                ),
                None if stage_skill_snapshot is not None else skill.max_hit,
            )
            for skill in skills
        ]
        if not skill_rows and total > 0:
            skill_rows = [(0, "未归类伤害", total, None, None)]
        class_id = self.model.actor_profession_id(self.skill_actor_id)
        _profession, color = self._profession_info(class_id)
        damage_x, share_x, hits_x, max_x = self._skill_columns(width)
        row_height = 38
        if not skill_rows:
            canvas.create_text(width // 2, height // 2, text="暂无技能伤害", fill=MUTED, font=("Microsoft YaHei UI", 10, "bold"))
            canvas.configure(scrollregion=(0, 0, width, height))
            return
        for index, (skill_id, name, damage, hits, max_hit) in enumerate(skill_rows):
            top = index * row_height
            bottom = top + row_height
            share = damage / total if total else 0.0
            base = PANEL if index % 2 == 0 else blend_color(PANEL, SURFACE, 0.28)
            bar = blend_color(base, color, 0.43)
            canvas.create_rectangle(0, top, width, bottom - 1, fill=base, outline="")
            canvas.create_rectangle(0, top, max(3, int(width * share)), bottom - 1, fill=bar, outline="")
            canvas.create_rectangle(0, top, 3, bottom - 1, fill=color, outline="")
            icon = self.icons.skill(skill_id, name, class_id, 22)
            canvas.create_image(8, top + 8, image=icon, anchor="nw")
            canvas.create_text(38, top + 19, text=name, fill=TEXT, anchor="w", font=("Microsoft YaHei UI", 9, "bold"))
            canvas.create_text(damage_x, top + 19, text=format_number(damage), fill=TEXT, anchor="e", font=("Segoe UI", 9, "bold"))
            canvas.create_text(share_x, top + 19, text=f"{share * 100:.1f}%", fill=TEXT, anchor="e", font=("Segoe UI", 9, "bold"))
            canvas.create_text(hits_x, top + 19, text="--" if hits is None else str(hits), fill=TEXT, anchor="e", font=("Segoe UI", 9, "bold"))
            canvas.create_text(max_x, top + 19, text="--" if max_hit is None else format_number(max_hit), fill=TEXT, anchor="e", font=("Segoe UI", 9, "bold"))
            canvas.create_line(0, bottom - 1, width, bottom - 1, fill=blend_color(BORDER, base, 0.45))
        canvas.configure(scrollregion=(0, 0, width, max(height, len(skill_rows) * row_height)))

    def _draw_skill_target_rows(self) -> None:
        if self.skill_rows_canvas is None or self.skill_actor_id is None:
            return
        canvas = self.skill_rows_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        rows = self.model.actor_target_rows(self.skill_actor_id)
        if not rows:
            canvas.create_text(
                width // 2,
                height // 2,
                text="暂无目标伤害",
                fill=MUTED,
                font=("Microsoft YaHei UI", 10, "bold"),
            )
            canvas.configure(scrollregion=(0, 0, width, height))
            return
        kind_x, damage_x, share_x = int(width * 0.58), int(width * 0.79), width - 17
        row_height = 38
        for index, row in enumerate(rows):
            top = index * row_height
            bottom = top + row_height
            share = max(0.0, min(1.0, float(row.get("share", 0.0) or 0.0)))
            kind = str(row.get("kind", "小怪"))
            color = WARN if kind == "Boss" else ACCENT
            base = PANEL if index % 2 == 0 else blend_color(PANEL, SURFACE, 0.28)
            name = str(row.get("name", "")).strip() or "未命名目标"
            entity_count = len(row.get("entity_ids", []))
            if entity_count > 1:
                name = f"{name} ×{entity_count}"
            canvas.create_rectangle(0, top, width, bottom - 1, fill=base, outline="")
            canvas.create_rectangle(
                0,
                top,
                max(3, int(width * share)),
                bottom - 1,
                fill=blend_color(base, color, 0.38),
                outline="",
            )
            canvas.create_rectangle(0, top, 3, bottom - 1, fill=color, outline="")
            canvas.create_text(12, top + 19, text=name, fill=TEXT, anchor="w", font=("Microsoft YaHei UI", 9, "bold"))
            canvas.create_text(kind_x, top + 19, text=kind, fill=color, anchor="e", font=("Microsoft YaHei UI", 8, "bold"))
            canvas.create_text(damage_x, top + 19, text=format_number(row.get("damage", 0)), fill=TEXT, anchor="e", font=("Segoe UI", 9, "bold"))
            canvas.create_text(share_x, top + 19, text=f"{share * 100:.1f}%", fill=TEXT, anchor="e", font=("Segoe UI", 9, "bold"))
            canvas.create_line(0, bottom - 1, width, bottom - 1, fill=blend_color(BORDER, base, 0.45))
        canvas.configure(scrollregion=(0, 0, width, max(height, len(rows) * row_height)))

    def _render_skill_details(self) -> None:
        if (
            self.skill_window is None
            or not self.skill_window.winfo_exists()
            or self.skill_actor_id is None
        ):
            return
        actor = self.model.stats.get(self.skill_actor_id)
        class_id = self.model.actor_profession_id(self.skill_actor_id)
        actor_name = self._shown_actor_name(self.skill_actor_id)
        icon = self.icons.profession(class_id, 30)
        self.skill_profession_icon.configure(image=icon)
        self.skill_profession_icon.image = icon
        self.skill_title_label.configure(text=actor_name)
        total = actor.damage if actor else 0
        duration = self.model.duration()
        actor_dps = total / duration if duration else 0.0
        self.skill_total_label.configure(text=f"总伤害  {format_number(total)}")
        self.skill_dps_label.configure(text=f"DPS  {format_number(actor_dps)}")
        critical_text = self._actor_critical_text(actor)
        deaths = self.model.member_death_counts.get(self.skill_actor_id, 0)
        if self.skill_combat_metrics_label is not None:
            self.skill_combat_metrics_label.configure(
                text=f"暴击率  {critical_text}     死亡  {deaths} 次"
            )
        self._draw_skill_header()
        self._draw_skill_rows()

    def _close_skill_window(self) -> None:
        if self.skill_window is not None and self.skill_window.winfo_exists():
            self.config["skill_geometry"] = self.skill_window.geometry()
            self.config["skill_layout_version"] = 3
            self.restore_geometry.pop(id(self.skill_window), None)
            self.skill_window.destroy()
        self.skill_window = None
        self.skill_actor_id = None
        self.skill_mode_buttons = {}
        self.skill_combat_metrics_label = None

    def _save_preferences(self) -> None:
        recent_skill_names = list(self.model.runtime_skill_names.items())[-2048:]
        self.config["runtime_skill_names"] = {
            str(skill_id): name for skill_id, name in recent_skill_names
        }
        self.config["hide_names"] = self.hide_names
        self.config["show_total_damage"] = self.show_total_damage
        self.config["show_dps"] = self.show_dps
        self.config["show_damage_share"] = self.show_damage_share
        self.config["show_critical_rate"] = self.show_critical_rate
        self.config["font_size"] = self.ui_font_size
        self.config["window_locked"] = self.window_locked
        self.config["layout_version"] = 15
        self.config["compact_layout_version"] = 3
        self.config["topmost"] = self._preferred_main_topmost()
        self.config["alpha"] = self.window_alpha
        self._remember_root_geometry()
        if self.history_window is not None and self.history_window.winfo_exists():
            self.config["history_geometry"] = self.restore_geometry.get(
                id(self.history_window), self.history_window.geometry()
            )
        for legacy_key in (
            "aliases",
            "skill_aliases",
            "skill_names",
            "local_player_name",
            "entity_names",
        ):
            self.config.pop(legacy_key, None)
        save_config(self.config)

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.close_started_at = time.monotonic()
        self._hide_main_tooltip()
        self._set_window_click_through(self.root, False)
        self._destroy_unlock_window()
        self._destroy_main_content_overlay()
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self._close_notice_window()
        self._close_skill_window()
        self._close_history_window()
        self._close_feedback_window()
        self._close_update_window(force=True)
        self.status_label.configure(
            text=self.close_status_text or "正在安全停止并退出…", fg=WARN
        )
        self._save_preferences()
        self.heartbeat_stop_event.set()
        self.stop_event.set()
        self.root.after(50, self._finish_close)

    def _ingest_pending_capture_messages(self) -> bool:
        handlers = {
            "event": self._ingest_combat_event,
            "identity": self.model.ingest_identity,
            "actor_merge": self.model.merge_actor,
            "party": self.model.ingest_party,
            "profile": self.model.ingest_profile,
            "monster": self.model.ingest_monster,
            "team_stat": self.model.ingest_team_stat,
            "stage_summary": self._ingest_stage_summary,
            "combat_state": self.model.ingest_combat_state,
            "life": self.model.ingest_life,
            "scene": self.model.ingest_scene,
            "name": self.model.ingest_name,
            "skill_name": self.model.ingest_skill_name,
        }
        deadline = time.perf_counter() + CLOSE_DRAIN_TIME_BUDGET_SECONDS
        for _index in range(MESSAGE_DRAIN_BATCH_SIZE):
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                return True
            handler = handlers.get(kind)
            if handler is not None:
                handler(payload)
            if time.perf_counter() >= deadline:
                return False
        return self.messages.empty()

    def _finish_close(self) -> None:
        if self.close_finalized:
            return
        if self.worker.is_alive():
            self.root.after(50, self._finish_close)
            return
        heartbeat_alive = bool(
            self.heartbeat_worker is not None and self.heartbeat_worker.is_alive()
        )
        heartbeat_grace_active = bool(
            heartbeat_alive
            and time.monotonic() - self.close_started_at
            < CLOSE_HEARTBEAT_GRACE_SECONDS
        )
        if heartbeat_grace_active:
            self.root.after(50, self._finish_close)
            return
        if not self._ingest_pending_capture_messages():
            self.root.after(1, self._finish_close)
            return
        self.close_finalized = True
        self.model.archive_current("exit")
        self._flush_combat_history()
        self._save_preferences()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    instance_guard = SingleInstanceGuard()
    try:
        instance_guard.acquire()
        if instance_guard.already_running:
            activate_existing_instance(attempts=20)
            return
        DpsWindow().run()
    except Exception:
        details = traceback.format_exc()
        try:
            (APP_DIR / "dps_error.log").write_text(details, encoding="utf-8")
        except Exception:
            pass
    finally:
        instance_guard.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
