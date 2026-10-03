"""Native Tk PvP backend pages. Workers only post detached queue messages.

History and alliance views share immutable match payloads. Nothing in this
module requests game data or changes the PVE encounter pipeline.
"""
from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime, timedelta
import io
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
import uuid

from PIL import Image, ImageDraw, ImageTk

from native_history_detail import (
    build_native_detail_hero,
    build_native_detail_metric_card,
    build_native_detail_metrics,
    build_native_detail_tabs,
    build_native_detail_toolbar,
    build_native_detail_workspace,
)
from pvp_records import HUNTER_CITY_MAPS, RECORDABLE_MAPS, project_duel_result_counters
from pvp_tracker import pvp_bot_evidence


# Keep the PvP backend on the same neutral surface hierarchy as the formal
# PVE history pages.  Colour is reserved for meaning, not for page chrome.
BG = "#08090b"
SURFACE = "#0f1114"
PANEL = "#141619"
PANEL_2 = "#1a1d21"
EDGE = "#2b2f34"
TEXT = "#f4f6f8"
MUTED = "#8d99a8"
SUBTLE = "#556170"
TEAL = "#6fe3bd"
CYAN = "#70b9e6"
BLUE = "#70b9e6"
GOLD = "#f0bc72"
RED = "#ef6b73"
CARD = SURFACE
REPORT_METRICS = (
    ("伤害", "damage", "#86def0"),
    ("承伤", "taken", "#8db9ca"),
    ("击杀", "kills", "#d9cfb1"),
    ("阵亡", "deaths", "#f17b88"),
)
REPORT_SURFACE = SURFACE
REPORT_DEEP = PANEL
REPORT_BORDER = EDGE
REPORT_SOFT = MUTED
HUNTER_REPORT_BANNER = Path(
    getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
) / "assets" / "hunter_city_report_banner.png"
ROW_HEADER = PANEL_2
ROW_ODD = SURFACE
ROW_EVEN = PANEL
ROW_SELECTED = "#17342c"
DETAIL_HEADER = "#111317"
ROLES = ("盟主", "副盟主", "精英成员", "核心成员", "普通成员", "预备成员")
PAIR_TABS = ("交手总览", "技能分布", "战斗记录", "装备快照")
HUNTER_PAIR_TABS = ("交手总览", "技能分布", "装备快照", "历史交手")
HISTORY_COLUMNS = ("场次编号", "模式", "地图", "开始时间", "结束时间", "时长", "战绩", "结果", "查看")
MEMBER_COLUMNS = ("角色名称", "职业", "非凡评分", "装备评分", "战盟职务", "最近活跃", "PvP 场次", "击杀", "助攻", "阵亡", "操作")

# These four layouts are confirmed by the exported client map catalogue.  The
# page may also consume an explicit ``team_size`` written by a newer record,
# but it never guesses a team size from the number of observed damage targets.
PVP_TEAM_SIZE_BY_MAP = {
    5_200_110: 6,
    5_200_111: 6,
    5_200_280: 1,
    5_200_020: 3,
    5_208_002: 3,
    5_208_003: 3,
    5_208_012: 3,
    5_208_017: 3,
    5_208_018: 3,
    5_208_004: 6,
    5_203_003: 12,
}
PVP_TEAM_SIZE_BY_MODE = {
    5_500_004: 6,
    5_500_005: 6,
    5_500_017: 1,
    5_500_002: 3,
    5_500_010: 6,
    5_500_003: 12,
}


def number(value):
    return "--" if value is None or value == "--" else f"{int(value):,}"


def optional_integer(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def compact_number(value):
    parsed = optional_integer(value)
    if parsed is None:
        return "--"
    if parsed >= 100_000_000:
        return f"{parsed / 100_000_000:.1f}亿"
    if parsed >= 10_000:
        return f"{parsed / 10_000:.1f}万"
    return f"{parsed:,}"


def blend_color(left, right, amount):
    """The exact colour blend used by the native PVE history components."""

    amount = min(1.0, max(0.0, float(amount)))
    lhs = tuple(int(left[index:index + 2], 16) for index in (1, 3, 5))
    rhs = tuple(int(right[index:index + 2], 16) for index in (1, 3, 5))
    mixed = tuple(round(a + (b - a) * amount) for a, b in zip(lhs, rhs))
    return "#" + "".join(f"{value:02x}" for value in mixed)


def participant_uid(value):
    """Return only a stable character UID; names are never identity keys."""

    if not isinstance(value, dict):
        return ""
    for field in ("character_id", "character_uid", "role_uid", "uid"):
        token = str(value.get(field) or "").strip()
        if token:
            return token
    token = str(value.get("opponent_id") or "").strip()
    if token and not token.casefold().startswith(("entity:", "unknown:", "duel:")):
        return token
    return ""


def pvp_team_size(record):
    if not isinstance(record, dict):
        return 0
    if optional_integer(record.get("map_id")) in HUNTER_CITY_MAPS:
        return 0
    if str(record.get("match_kind") or "").casefold() == "duel":
        return 1
    explicit = optional_integer(record.get("team_size"))
    if explicit in {1, 3, 6, 12, 60}:
        return explicit
    try:
        map_id = int(record.get("map_id") or 0)
    except (TypeError, ValueError, OverflowError):
        map_id = 0
    try:
        mode_id = int(record.get("mode_id") or 0)
    except (TypeError, ValueError, OverflowError):
        mode_id = 0
    return PVP_TEAM_SIZE_BY_MAP.get(map_id, PVP_TEAM_SIZE_BY_MODE.get(mode_id, 0))


def _participant_row(value, *, is_self=False):
    source = value if isinstance(value, dict) else {}

    def first(*fields):
        for field in fields:
            if source.get(field) is not None:
                return source.get(field)
        return None

    return {
        "character_id": participant_uid(source),
        "name": str(source.get("name") or source.get("character_name") or "未知玩家"),
        "profession_id": optional_integer(source.get("profession_id")) or 0,
        "extraordinary_rating": optional_integer(first("extraordinary_rating", "rating")),
        "kills": optional_integer(first("kills", "kills_on")),
        "damage": optional_integer(first("damage", "damage_done")),
        "healing": optional_integer(first("healing", "healing_done", "effective_healing", "heal")),
        "taken": optional_integer(first("taken", "damage_taken", "damage_from")),
        "is_ai": pvp_bot_evidence(source),
        "is_self": bool(is_self or source.get("is_self")),
        "source": source,
    }


def pvp_snapshot_rosters(record):
    """Return confirmed friendly/enemy rows without manufacturing team stats.

    Newer records may contain full ``teams``/``allies``/``enemies`` arrays.
    Old records contain only the local player and direct opponents.  For team
    modes those opponent counters describe interaction with the local player,
    not the opponent's whole-match totals, so only identity/rating is reused.
    """

    record = record if isinstance(record, dict) else {}
    player = record.get("player") if isinstance(record.get("player"), dict) else {}
    local_source = {
        **player,
        "kills": record.get("kills"),
        "damage": record.get("damage", record.get("damage_done")),
        "healing": record.get("healing", record.get("healing_done")),
        "taken": record.get("taken", record.get("damage_taken")),
    }
    local = _participant_row(local_source, is_self=True)
    teams = record.get("teams") if isinstance(record.get("teams"), dict) else {}

    def team_list(*keys):
        for key in keys:
            value = record.get(key)
            if isinstance(value, list):
                return value
            value = teams.get(key)
            if isinstance(value, list):
                return value
        return []

    # Accept both the canonical allies/enemies shape and the names emitted by
    # newer protocol adapters.  This is a read-only projection: absent fields
    # remain None and are rendered as ``--`` by the snapshot.
    raw_allies = team_list("allies", "friendly", "our_team", "team_allies")
    raw_enemies = team_list("enemies", "opposing", "enemy_team", "team_enemies")
    if not raw_allies and not raw_enemies:
        participants = record.get("team_participants")
        if not isinstance(participants, list):
            participants = record.get("participants")
        if isinstance(participants, list):
            for value in participants:
                if not isinstance(value, dict):
                    continue
                side = str(value.get("side") or value.get("team") or "").casefold()
                if side in {"ally", "allies", "friendly", "ours", "我方", "1"}:
                    raw_allies.append(value)
                elif side in {"enemy", "enemies", "opponent", "opposing", "敌方", "2"}:
                    raw_enemies.append(value)

    local_uid = participant_uid(player)
    allies = []
    local_replaced = False
    for value in raw_allies:
        if not isinstance(value, dict):
            continue
        is_local = bool(local_uid and participant_uid(value) == local_uid) or bool(value.get("is_self"))
        if is_local:
            # Keep explicit whole-match values from a team settlement, while
            # falling back to the local top-level counters when the packet only
            # carries identity/rating for this member.
            explicit = _participant_row(value, is_self=True)
            merged = dict(local)
            for field, item in explicit.items():
                if field in {"source", "is_self", "character_id"}:
                    continue
                if item is not None and item != "未知玩家":
                    merged[field] = item
            merged["character_id"] = explicit.get("character_id") or local.get("character_id")
            merged["is_self"] = True
            merged["source"] = explicit.get("source", value)
            allies.append(merged)
            local_replaced = True
        else:
            allies.append(_participant_row(value))
    if not local_replaced:
        allies.append(local)

    size = pvp_team_size(record)
    if raw_enemies:
        enemies = [_participant_row(value) for value in raw_enemies if isinstance(value, dict)]
    else:
        opponents = [value for value in record.get("opponents", ()) if isinstance(value, dict)]
        if size == 1:
            enemies = []
            for opponent in opponents[:1]:
                # In a confirmed 1V1, the two directional counters are each
                # player's complete direct damage for the match.
                enemy = _participant_row({
                    **opponent,
                    "kills": opponent.get("defeats", opponent.get("deaths_to")),
                    "damage": opponent.get("taken", opponent.get("damage_from")),
                    "taken": opponent.get("damage", opponent.get("damage_to")),
                })
                enemies.append(enemy)
        else:
            enemies = [
                _participant_row({
                    "character_id": participant_uid(opponent),
                    "opponent_id": opponent.get("opponent_id"),
                    "name": opponent.get("name"),
                    "profession_id": opponent.get("profession_id"),
                    "extraordinary_rating": opponent.get("extraordinary_rating"),
                    "is_ai": opponent.get("is_ai"),
                })
                for opponent in opponents
            ]
    if size == 1:
        # A duel snapshot is strictly one local player versus one confirmed
        # peer, even if an old payload retained extra interaction rows.
        allies = [next((row for row in allies if row.get("is_self")), local)]
        enemies = enemies[:1]
    return allies, enemies


def pvp_opponent_history(records, uid):
    """Aggregate only finalized saved matches by stable character UID."""

    uid = str(uid or "").strip()
    result = {"total": 0, "wins": 0, "losses": 0, "unknown": 0, "recent": []}
    if not uid:
        return result
    ordered = sorted(
        (row for row in records if isinstance(row, dict)),
        key=lambda row: int(row.get("ended_at_ns") or row.get("started_at_ns") or 0),
        reverse=True,
    )
    for record in ordered:
        if not (
            record.get("ended_at_ns")
            or record.get("ended_at")
            or record.get("end_time")
        ):
            continue
        candidates = [row for row in record.get("opponents", ()) if isinstance(row, dict)]
        _allies, enemies = pvp_snapshot_rosters(record)
        candidates.extend(row.get("source", {}) for row in enemies)
        if not any(participant_uid(row) == uid for row in candidates):
            continue
        outcome = str(record.get("result") or "未知")
        if outcome not in {"胜利", "失败", "未知"}:
            outcome = "未知"
        result["total"] += 1
        result["wins" if outcome == "胜利" else "losses" if outcome == "失败" else "unknown"] += 1
        if len(result["recent"]) < 5:
            result["recent"].append(outcome)
    return result


def pvp_hunter_opponent_history(records, uid):
    """Summarize direct encounters with one stable character in Hunter City."""

    uid = str(uid or "").strip()
    result = {"total": 0, "damage_to": 0, "damage_from": 0,
              "known_incoming": 0, "kills": 0, "deaths": 0, "matches": []}
    if not uid:
        return result
    ordered = sorted(
        (row for row in records if isinstance(row, dict)
         and optional_integer(row.get("map_id")) in HUNTER_CITY_MAPS),
        key=lambda row: int(row.get("ended_at_ns") or row.get("started_at_ns") or 0),
        reverse=True,
    )
    for record in ordered:
        if not record.get("ended_at_ns"):
            continue
        opponent = next(
            (row for row in record.get("opponents", ())
             if isinstance(row, dict) and participant_uid(row) == uid),
            None,
        )
        if opponent is None:
            continue
        damage_to = optional_integer(opponent.get("damage")) or 0
        damage_from = optional_integer(opponent.get("taken"))
        kills = optional_integer(opponent.get("kills")) or 0
        deaths = optional_integer(opponent.get("defeats")) or 0
        result["total"] += 1
        result["damage_to"] += damage_to
        result["kills"] += kills
        result["deaths"] += deaths
        if damage_from is not None:
            result["damage_from"] += damage_from
            result["known_incoming"] += 1
        result["matches"].append({
            "started_at_ns": record.get("started_at_ns"),
            "mode_name": record.get("mode_name") or "猎龙之城",
            "damage_to": damage_to,
            "damage_from": damage_from,
            "kills": kills,
            "deaths": deaths,
        })
    return result


def hunter_detail_opponents(record):
    """Enrich direct opponents only with same-record identity evidence."""

    record = record if isinstance(record, dict) else {}
    known = {
        participant_uid(row): row
        for row in record.get("enemies", ())
        if isinstance(row, dict) and participant_uid(row)
    }
    result = []
    for raw in record.get("opponents", ()):
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        profile = known.get(participant_uid(row), {})
        for field in (
            "name", "profession_id", "club_name", "extraordinary_rating",
            "avatar_id", "avatar_frame_id", "equipment_snapshot",
        ):
            if row.get(field) in (None, "", {}, []) and profile.get(field) not in (None, "", {}, []):
                row[field] = deepcopy(profile[field])
        snapshot = row.get("equipment_snapshot")
        if row.get("extraordinary_rating") is None and isinstance(snapshot, dict):
            row["extraordinary_rating"] = snapshot.get("extraordinary_rating")
        result.append(row)
    return result


def percent(value):
    return "--" if value is None else f"{value * 100:.1f}%"


def stamp(value, *, short=False):
    if not value:
        return "--"
    try:
        date = (datetime.fromtimestamp(value / 1e9) if isinstance(value, (int, float))
                else datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone())
        return date.strftime("%H:%M:%S" if short else "%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return "--"


def duration(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def compact_duration(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return duration(seconds)


def full_stamp(value, *, seconds=False):
    if not value:
        return "--"
    try:
        date = (datetime.fromtimestamp(value / 1e9) if isinstance(value, (int, float))
                else datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone())
        return date.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "--"


def match_caption(value):
    value = str(value or "").removeprefix("pvp_").strip()
    return "#" + (value[-8:].upper() if value else "--")


def sum_known(rows, field):
    return None if any(row.get(field) is None for row in rows) else sum(row[field] for row in rows)


def merge_records(local, remote):
    # Local finalized snapshots are never overwritten by later profile data.
    indexed = {
        row["match_id"]: project_duel_result_counters(row)
        for row in remote
        if isinstance(row, dict) and row.get("match_id")
    }
    indexed.update({
        row["match_id"]: project_duel_result_counters(row)
        for row in local
        if isinstance(row, dict) and row.get("match_id")
    })
    return sorted(indexed.values(), key=lambda row: row.get("started_at_ns", 0), reverse=True)


def filter_records(records, mode="全部模式", period="全部时间", query="", now=None):
    days = {"今天": 1, "近7天": 7, "近30天": 30}.get(period)
    now = time.time() if now is None else now
    cutoff = (datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
              if period == "今天" else now - days * 86400 if days else 0)
    query = query.strip().casefold()
    result = []
    for row in records:
        if mode != "全部模式" and row.get("mode_name") != mode:
            continue
        if cutoff and row.get("started_at_ns", 0) / 1e9 < cutoff:
            continue
        haystack = " ".join(str(value or "") for value in (
            row.get("match_id"), row.get("mode_name"), row.get("map_name"),
            row.get("player", {}).get("name"),
            *(opponent.get("name") for opponent in row.get("opponents", []))))
        if not query or query in haystack.casefold():
            result.append(row)
    return result


def parse_members(text):
    """Explicit name, stable role ID, profession template ID, role; no guessing."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = next(csv.reader(io.StringIO(line), delimiter="\t" if "\t" in line else ","))
        cells = [cell.strip() for cell in cells]
        if len(cells) != 4 or not cells[0] or not cells[1] or cells[3] not in ROLES:
            raise ValueError("每行填写：角色名称,角色ID,职业ID,职务。")
        try:
            profession = int(cells[2])
        except ValueError:
            raise ValueError("职业ID必须是整数。") from None
        if not 0 <= profession <= 9_999_999:
            raise ValueError("职业ID超出范围。")
        rows.append(dict(character_name=cells[0], character_id=cells[1],
                         profession_id=profession, club_role=cells[3]))
    if not 1 <= len(rows) <= 500:
        raise ValueError("每次可导入 1–500 名成员。")
    return rows


class PvpPage(tk.Frame):
    def __init__(self, parent, host, page_key, dropdown, scrollbar, split_pane=None):
        super().__init__(parent, bg=BG)
        self.host, self.page_key, self.dropdown = host, page_key, dropdown
        self.scrollbar_class = scrollbar
        self.split_pane_class = split_pane
        self.instance_key = uuid.uuid4().hex
        self.account_key = self.principal()
        self.pending = {}
        self.images = []
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical = scrollbar(self, command=self.canvas.yview, background=BG)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = scrollbar(self, orient="horizontal", command=self.canvas.xview, background=BG)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.content = tk.Frame(self.canvas, bg=BG)
        self.content_window = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.content.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.content_window, width=max(1100, e.width)))
        self.canvas.bind("<MouseWheel>", self._wheel, add="+")
        self.content.bind("<MouseWheel>", self._wheel, add="+")

    def principal(self):
        return str(getattr(getattr(self.host, "pvp_recording", None), "account_key", "") or "")

    def clear(self):
        for widget in self.content.winfo_children():
            widget.destroy()
        self.images.clear()

    def label(self, parent, text, *, role="body", color=TEXT, bg=CARD, **options):
        return tk.Label(parent, text=text, bg=bg, fg=color, font=self.host._ui_font(role), **options)

    def icon_label(self, parent, name, *, size=24, color=CYAN, bg=CARD, **options):
        image = self.host.icons.toolbar(name, size, color)
        self.images.append(image)
        return tk.Label(parent, image=image, bg=bg, bd=0, **options)

    @staticmethod
    def bind_click(widget, command, *, cursor="hand2"):
        widget.configure(cursor=cursor)
        widget.bind("<Button-1>", lambda _event: command())

    def toggle(self, parent, variable):
        canvas = tk.Canvas(
            parent,
            width=48,
            height=26,
            bg=parent.cget("bg"),
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        enabled = bool(variable.get())
        track = TEAL if enabled else "#343b41"
        canvas.create_oval(2, 3, 22, 23, fill=track, outline=track)
        canvas.create_rectangle(12, 3, 36, 23, fill=track, outline=track)
        canvas.create_oval(26, 3, 46, 23, fill=track, outline=track)
        center = 36 if enabled else 12
        canvas.create_oval(
            center - 9,
            4,
            center + 9,
            22,
            fill=TEXT,
            outline=TEXT,
        )
        canvas.bind("<Button-1>", lambda _event: variable.set(not variable.get()))
        return canvas

    def card(self, parent, title="", *, pack=True):
        box = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=EDGE)
        if pack:
            box.pack(fill="x", padx=20, pady=(0, 14))
        if title:
            self.label(box, "▎ " + title, role="strong", color=CYAN, anchor="w").pack(fill="x", padx=14, pady=(12, 10))
        return box

    def button(self, parent, text, command, *, primary=False):
        button = self.host._backend_action_button(parent, text, command)
        if primary:
            button.configure(fg=CYAN, highlightbackground=EDGE)
            button._normal_fg = CYAN
            button._hover_fg = TEXT
        return button

    def title(self, caption, subtitle, back=None):
        header = tk.Frame(self.content, bg=BG)
        header.pack(fill="x", padx=22, pady=(22, 18))
        if back:
            self.button(header, "← 返回战斗记录", back).pack(side="right", pady=5)
        self.label(header, caption, role="settings_title", bg=BG, anchor="w").pack(fill="x")
        self.label(header, subtitle, role="small", color=MUTED, bg=BG, anchor="w").pack(fill="x", pady=(7, 0))

    def icon(self, parent, profession, *, size=32, bg=CARD):
        image = self._history_profession_icon(profession, size)
        self.images.append(image)
        widget = tk.Label(parent, image=image, bg=bg)
        widget.pack(side="left", padx=(0, 9))
        return widget

    def _history_profession_icon(self, profession, size=32):
        """Use the coloured crest used by the live DPS window.

        Some detached preview hosts expose only the legacy ``profession``
        factory, so retain that fallback while the normal application uses
        the same coloured ``main_profession`` renderer as the DPS window.
        """

        class_id = int(profession or 0)
        main_profession = getattr(self.host.icons, "main_profession", None)
        if callable(main_profession):
            try:
                return main_profession(class_id, size=size)
            except TypeError:
                return main_profession(class_id, size)
        return self.host.icons.profession(class_id, size=size)

    def profession(self, profession):
        return self.host._profession_info(int(profession or 0))[0] if profession else "--"

    def summary(self, parent, entries):
        line = tk.Frame(parent, bg=BG)
        line.pack(fill="x", padx=20, pady=(0, 16))
        for column, (caption, value, color) in enumerate(entries):
            line.grid_columnconfigure(column, weight=1, uniform="metrics")
            card = self.card(line, pack=False)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 12 if column < len(entries) - 1 else 0))
            self.label(card, caption, color=MUTED, role="small").pack(anchor="w", padx=16, pady=(13, 7))
            self.label(card, value, color=color, role="title").pack(anchor="w", padx=16, pady=(0, 14))

    def table(self, parent, headings, rows, *, widths=None, commands=None):
        table = tk.Frame(parent, bg=CARD)
        table.pack(fill="x", padx=12, pady=(0, 12))
        widths = widths or [80] * len(headings)
        for col, (heading, width) in enumerate(zip(headings, widths)):
            table.grid_columnconfigure(col, weight=1, minsize=width)
            self.label(table, heading, color=MUTED, role="small", bg=PANEL_2, anchor="w", padx=7, pady=9).grid(row=0, column=col, sticky="ew")
        if not rows:
            self.label(table, "暂无数据", color=MUTED, pady=30).grid(row=1, column=0, columnspan=len(headings), sticky="ew")
        for index, values in enumerate(rows):
            background = PANEL if index % 2 == 0 else CARD
            for col, value in enumerate(values):
                widget = self.label(table, value, bg=background, role="small", anchor="w", padx=7, pady=10)
                widget.grid(row=index + 1, column=col, sticky="nsew")
                if commands and commands[index]:
                    widget.configure(cursor="hand2", fg=CYAN if col in (0, len(values) - 1) else TEXT)
                    widget.bind("<Button-1>", lambda _e, command=commands[index]: command())
        table.bind("<MouseWheel>", self._wheel, add="+")
        return table

    def _wheel(self, event):
        try:
            delta = int(getattr(event, "delta", 0) or 0)
            button = int(getattr(event, "num", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            delta = button = 0
        amount = -1 if button == 4 or delta > 0 else 1 if button == 5 or delta < 0 else 0
        if amount:
            self.canvas.yview_scroll(amount, "units")
            return "break"
        return None

    def request(self, action, payload, callback, *, channel="default"):
        account_key = self.principal()
        if not account_key:
            callback({"ok": False, "error": "invalid_session", "message": "请先登录。"})
            return
        request_id = uuid.uuid4().hex
        # A newer request on this channel supersedes the old one.
        self.pending[channel] = (request_id, callback)
        session = deepcopy(self.host.licensing.session)
        gateway = self.host.licensing.gateway
        queue = self.host.control_messages
        envelope = dict(page=self.page_key, instance=self.instance_key, account_key=account_key,
                        request_id=request_id, channel=channel)

        def run():
            try:
                response = gateway.pvp_request(session, action, deepcopy(payload))
            except Exception:
                response = {"ok": False, "error": "offline", "message": "服务暂不可用，已保存的记录不受影响。"}
            queue.put(("pvp_backend_response", dict(envelope, response=response)))

        threading.Thread(target=run, name="pvp-backend-query", daemon=True).start()

    def receive(self, envelope):
        if (envelope.get("instance") != self.instance_key or envelope.get("account_key") != self.principal()
                or envelope.get("account_key") != self.account_key or not self.winfo_exists()):
            return
        channel = envelope.get("channel")
        pending = self.pending.get(channel)
        if pending and pending[0] == envelope.get("request_id"):
            self.pending.pop(channel)
            pending[1](envelope.get("response", {}))

    def dispose(self):
        self.pending.clear()
        self.instance_key = ""


class PvpHistoryPage(PvpPage):
    """PvP records rendered with the native PVE battle-record shell.

    ``kings`` remains an internal route alias so older saved window state and
    alliance links continue to work.  It is intentionally not a separate
    visual system anymore.
    """
    PAGE_SIZE = 10

    def __init__(self, parent, host, dropdown, scrollbar, *, page_key="kings", fixed_records=None, heading="竞技战绩", back=None, split_pane=None):
        super().__init__(parent, host, page_key, dropdown, scrollbar, split_pane)
        self.fixed_records = (
            [project_duel_result_counters(row) for row in fixed_records]
            if fixed_records is not None
            else None
        )
        self.heading, self.external_back = heading, back
        self.remote = []
        self.records = list(self.fixed_records or [])
        self.selected_record = None
        self.selected_opponent = None
        self.overview_selected_match_id = ""
        self.pair_tab = PAIR_TABS[0]
        self.hunter_history_offset = 0
        self._hunter_detail_banner_source = None
        self._hunter_detail_banner_cache = {}
        self._hunter_equipment_lookup_cache = {}
        self.hunter_equipment_selected_index = 0
        self.page_index = 0
        self.mode = tk.StringVar(self, "全部模式")
        self.period = tk.StringVar(self, "全部时间")
        self.result_filter = tk.StringVar(self, "全部结果")
        self.query = tk.StringVar(self, "")
        self.show_names = tk.BooleanVar(self, not bool(getattr(host, "hide_names", False)))
        self.message = "进入 PvP 地图自动开始，离开地图自动保存。"
        for variable in (self.mode, self.period, self.result_filter):
            variable.trace_add("write", lambda *_args: self.apply_filters())
        self.show_names.trace_add("write", lambda *_args: self.render())
        self.render()

    def name(self, value, index=0, *, local=False):
        return (str(value or "未知玩家") if self.show_names.get() else "本人" if local else f"玩家 {index + 1}")

    def on_show(self):
        account_key = self.principal()
        if account_key != self.account_key:
            self.account_key = account_key
            self.pending.clear()
            self.remote = []
            self.selected_record = self.selected_opponent = None
            self.page_index = 0
        self.reload_local()
        if self.fixed_records is None:
            self.request("records/list", {"limit": 500}, self.remote_received, channel="records")

    def reload_local(self):
        if self.fixed_records is None:
            local = self.host.pvp_history_repository.list(self.principal()) if self.principal() else []
            self.records = merge_records(local, self.remote)
        self.render()

    def remote_received(self, response):
        if response.get("ok"):
            self.remote = response.get("records", [])
            self.message = "进入 PvP 地图自动开始，离开地图自动保存。"
        else:
            self.message = str(response.get("message") or "线上历史暂不可用，当前展示已保存记录。")
        self.reload_local()

    def apply_filters(self):
        self.page_index = 0
        self.selected_record = self.selected_opponent = None
        self.render()
        self.canvas.yview_moveto(0)

    def overview(self):
        self.selected_record = self.selected_opponent = None
        self.render()
        self.canvas.yview_moveto(0)

    def open_record(self, record):
        self.selected_record = record
        self.selected_opponent = next(iter(record.get("opponents", [])), None)
        self.pair_tab = PAIR_TABS[0]
        self.hunter_history_offset = 0
        self.render()
        self.canvas.yview_moveto(0)

    def render(self):
        self.clear()
        if self.selected_record:
            self.render_detail()
        else:
            self.render_overview()

    def render_overview(self):
        """Render the PvP browser inside the established PVE shell.

        The record table and snapshot renderers remain PvP-specific, while the
        surrounding bands intentionally mirror the native PVE history page:
        compact heading, two-row filters, overview cards, and a fixed
        list/snapshot split pane.
        """
        # A finalized map can arrive while this page is open. Read SQLite at
        # render time so switching back from a detail never shows a stale copy.
        if self.fixed_records is None:
            local = self.host.pvp_history_repository.list(self.principal()) if self.principal() else []
            self.records = merge_records(local, self.remote)

        header = tk.Frame(self.content, bg=BG, height=64)
        header.pack(fill="x", padx=18)
        header.pack_propagate(False)
        heading_text = tk.Frame(header, bg=BG)
        heading_text.pack(side="left", fill="y")
        self.label(
            heading_text,
            self.heading,
            role="title",
            bg=BG,
            anchor="w",
        ).pack(fill="x", pady=(8, 0))
        self.label(
            heading_text,
            "记录每一场竞技对决与大型团战",
            role="micro",
            color=MUTED,
            bg=BG,
            anchor="w",
        ).pack(fill="x", pady=(0, 5))
        actions = tk.Frame(header, bg=BG)
        actions.pack(side="right", fill="y")
        self.button(actions, "↻ 刷新", self.on_show).pack(side="right", pady=14)
        if self.external_back:
            self.button(actions, "← 返回", self.external_back).pack(side="right", padx=(0, 6), pady=14)

        filters = tk.Frame(
            self.content,
            bg=PANEL,
            height=88,
            highlightthickness=1,
            highlightbackground=EDGE,
        )
        filters.pack(fill="x", padx=18, pady=(8, 0))
        filters.pack_propagate(False)
        first_row = tk.Frame(filters, bg=PANEL, height=42)
        first_row.pack(fill="x", padx=10, pady=(8, 0))
        first_row.pack_propagate(False)
        search_shell = tk.Frame(first_row, bg=SURFACE, height=32, width=250,
                                highlightthickness=1, highlightbackground=EDGE)
        search_shell.pack(side="left")
        search_shell.pack_propagate(False)
        self.icon_label(search_shell, "search", size=16, color=TEAL, bg=SURFACE).pack(side="left", padx=(8, 2))
        search = tk.Entry(
            search_shell,
            textvariable=self.query,
            bg=SURFACE,
            fg=TEXT,
            insertbackground=CYAN,
            relief="flat",
            bd=0,
            font=self.host._ui_font("small"),
        )
        search.pack(side="left", fill="both", expand=True, padx=(3, 7), pady=2)
        search.insert(0, "")
        search.bind("<Return>", lambda _event: self.apply_filters())
        for caption, variable, choices, width in (
            ("战斗模式", self.mode, ("全部模式", *sorted({m["mode_name"] for m in RECORDABLE_MAPS.values()} | {row.get("mode_name") for row in self.records if row.get("mode_name")})), 155),
            ("战斗结果", self.result_filter, ("全部结果", "胜利", "失败", "未知", "无胜负"), 120),
        ):
            group = tk.Frame(first_row, bg=PANEL)
            group.pack(side="left", padx=(8, 0))
            self.label(group, caption, color=MUTED, role="micro", bg=PANEL).pack(side="left", padx=(0, 6))
            dropdown = self.dropdown(group, variable, choices, width=width,
                                     font=self.host._ui_font("small"), background=SURFACE)
            dropdown.pack(side="left")
        second_row = tk.Frame(filters, bg=PANEL, height=37)
        second_row.pack(fill="x", padx=10)
        second_row.pack_propagate(False)
        self.label(second_row, "时间", color=MUTED, role="micro", bg=PANEL).pack(side="left", padx=(0, 8), pady=5)
        for caption, value in (("今天", "今天"), ("近7天", "近7天"), ("近30天", "近30天"), ("全部", "全部时间")):
            chip = tk.Label(second_row, text=caption,
                            bg=TEAL if self.period.get() == value else SURFACE,
                            fg="#07110e" if self.period.get() == value else MUTED,
                            padx=10, pady=5, cursor="hand2",
                            font=self.host._ui_font("micro"))
            chip.pack(side="left", padx=(0, 2), pady=1)
            chip.bind("<Button-1>", lambda _event, selected=value: self.period.set(selected))
        privacy = tk.Frame(second_row, bg=PANEL)
        privacy.pack(side="right", pady=1)
        self.label(privacy, "显示角色名称", color=MUTED, role="micro", bg=PANEL).pack(side="left", padx=(0, 6))
        self.toggle(privacy, self.show_names).pack(side="left")

        filtered = filter_records(self.records, self.mode.get(), self.period.get(), self.query.get())
        if self.result_filter.get() != "全部结果":
            filtered = [row for row in filtered
                        if self.overview_outcome(row)[0] == self.result_filter.get()]

        overview = tk.Frame(self.content, bg=BG, height=104)
        overview.pack(fill="x", padx=18, pady=(8, 8))
        overview.pack_propagate(False)
        known_damage = [optional_integer(row.get("damage")) for row in filtered]
        known_damage = [value for value in known_damage if value is not None]
        stats = (
            ("战斗场次", number(len(filtered)), TEAL),
            ("今日新增", number(sum(1 for row in filtered if self.overview_started_at(row.get("started_at_ns"))[:5] == datetime.now().strftime("%m-%d"))), GOLD),
            ("平均伤害", number(round(sum_known(filtered, "damage") / len(filtered))) if filtered and sum_known(filtered, "damage") is not None else "--", CYAN),
            ("最高伤害", number(max(known_damage)) if known_damage else "--", RED),
            ("平均时长", compact_duration(round(sum(float(row.get("duration_seconds") or 0) for row in filtered) / len(filtered))) if filtered else "--", "#91d27f"),
        )
        for column, (caption, value, accent) in enumerate(stats):
            overview.grid_columnconfigure(column, weight=1, uniform="pvp-overview")
            card = tk.Frame(overview, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 0))
            tk.Frame(card, bg=accent, height=2).place(x=0, y=0, relwidth=1)
            self.label(card, caption, color=MUTED, role="micro", bg=PANEL, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
            self.label(card, value, color=accent, role="title", bg=PANEL, anchor="w").pack(fill="x", padx=10, pady=(0, 0))
            self.label(card, "竞技战绩汇总", color=SUBTLE, role="micro", bg=PANEL, anchor="w").pack(fill="x", padx=10, pady=(0, 5))

        pages = max(1, (len(filtered) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_index = min(self.page_index, pages - 1)
        visible = filtered[self.page_index * self.PAGE_SIZE:(self.page_index + 1) * self.PAGE_SIZE]
        if visible and self.overview_selected_match_id not in {str(row.get("match_id") or "") for row in visible}:
            self.overview_selected_match_id = str(visible[0].get("match_id") or "")
        selected_record = next((row for row in visible if str(row.get("match_id") or "") == self.overview_selected_match_id), visible[0] if visible else None)
        workspace = self.split_pane_class(self.content, background=BG) if self.split_pane_class is not None else tk.Frame(self.content, bg=BG)
        workspace.configure(height=570)
        workspace.pack_propagate(False)
        workspace.pack(fill="x", padx=18, pady=(0, 14))
        left = tk.Frame(workspace, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
        right = tk.Frame(workspace, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
        if self.split_pane_class is not None:
            workspace.add(left, minsize=410, width=700, stretch="always")
            workspace.add(right, minsize=270, width=330, stretch="never")
        else:
            workspace.grid_columnconfigure(0, weight=1)
            workspace.grid_columnconfigure(1, weight=0, minsize=330)
            left.grid(row=0, column=0, sticky="nsew")
            right.grid(row=0, column=1, sticky="nsew")
        self.render_overview_table(left, visible, total=len(filtered), pages=pages)
        self.render_overview_snapshot(right, selected_record)
        if self.message and self.message != "进入 PvP 地图自动开始，离开地图自动保存。":
            self.label(self.content, self.message, bg=BG, color=MUTED, role="micro", anchor="w").pack(fill="x", padx=22, pady=(0, 18))

    @staticmethod
    def overview_outcome(record):
        try:
            map_id = int(record.get("map_id") or 0)
        except (TypeError, ValueError, OverflowError):
            map_id = 0
        if map_id in HUNTER_CITY_MAPS:
            return "无胜负", CYAN, "analytics"
        outcome = str(record.get("result") or "未知")
        if outcome not in {"胜利", "失败", "未知"}:
            outcome = "未知"
        return (
            outcome,
            TEAL if outcome == "胜利" else RED if outcome == "失败" else GOLD,
            "crown" if outcome == "胜利" else "analytics" if outcome == "失败" else "warning",
        )

    @staticmethod
    def overview_started_at(value):
        if not value:
            return "--"
        try:
            date = datetime.fromtimestamp(float(value) / 1e9)
            return date.strftime("%m-%d · %H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            return "--"

    def bind_overview_cell(self, widget, record, *, details=False):
        command = (
            (lambda record=record: self.open_record(record))
            if details
            else (lambda record=record: self.select_overview_record(record))
        )
        self.bind_click(widget, command)
        for child in widget.winfo_children():
            self.bind_click(child, command)
            for nested in child.winfo_children():
                self.bind_click(nested, command)

    def overview_text_cell(
        self,
        table,
        record,
        row,
        column,
        primary,
        secondary="",
        *,
        background,
        primary_color=TEXT,
        anchor="center",
    ):
        cell = tk.Frame(table, bg=background)
        cell.grid(row=row, column=column, sticky="nsew", padx=(0, 1), pady=(1, 0))
        block = tk.Frame(cell, bg=background)
        block.pack(expand=True, fill="x", padx=7, pady=6)
        self.label(
            block,
            primary,
            color=primary_color,
            role="strong",
            bg=background,
            anchor=anchor,
        ).pack(fill="x")
        if secondary:
            self.label(
                block,
                secondary,
                color=SUBTLE,
                role="small",
                bg=background,
                anchor=anchor,
            ).pack(fill="x", pady=(1, 0))
        self.bind_overview_cell(cell, record)
        return cell

    def render_overview_table(self, parent, visible, *, total, pages):
        try:
            parent.grid_rowconfigure(0, weight=1)
            parent.grid_columnconfigure(0, weight=1)
        except (AttributeError, tk.TclError):
            pass
        card = tk.Frame(
            parent,
            bg=CARD,
            highlightthickness=1,
            highlightbackground=EDGE,
        )
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        field_bar = tk.Frame(card, bg=SURFACE, height=38)
        field_bar.pack(fill="x")
        field_bar.pack_propagate(False)
        self.label(
            field_bar,
            "显示字段",
            color=MUTED,
            role="micro",
            bg=SURFACE,
            anchor="w",
        ).pack(side="left", padx=(10, 8), fill="y")
        tk.Frame(field_bar, bg=EDGE, width=1, height=18).pack(side="left", padx=(0, 8), pady=10)
        for caption in ("角色名称", "非凡评分", "我的表现", "战斗结果"):
            self.label(
                field_bar,
                "✓ " + caption,
                color=TEAL if caption in {"角色名称", "非凡评分", "战斗结果"} else MUTED,
                role="micro",
                bg=SURFACE,
                anchor="w",
            ).pack(side="left", padx=(0, 10), pady=8)
        header = tk.Frame(card, bg=CARD)
        header.pack(fill="x", padx=14, pady=(12, 10))
        self.label(header, "战斗记录", role="strong", bg=CARD).pack(side="left")
        self.label(
            header,
            "点击记录可在右侧查看战斗快照",
            color=MUTED,
            role="small",
            bg=CARD,
        ).pack(side="left", padx=(14, 0))

        table = tk.Frame(card, bg=EDGE, highlightthickness=1, highlightbackground=EDGE)
        table.pack(fill="both", expand=True, padx=9)
        headings = (
            "战斗时长",
            "模式 / 地图",
            "角色名称",
            "非凡评分",
            "战绩",
            "造成伤害",
            "结果",
            "查看",
        )
        minimums = (88, 108, 104, 76, 112, 88, 70, 46)
        weights = (1, 2, 2, 1, 2, 1, 1, 0)
        for column, (caption, minimum, weight) in enumerate(zip(headings, minimums, weights)):
            table.grid_columnconfigure(column, weight=weight, minsize=minimum)
            self.label(
                table,
                caption,
                color=MUTED,
                role="small",
                bg=ROW_HEADER,
                anchor="center",
                pady=9,
            ).grid(row=0, column=column, sticky="nsew", padx=(0, 1))
        if not visible:
            self.label(
                table,
                "暂无符合条件的 PvP 战斗记录",
                color=MUTED,
                bg=ROW_ODD,
                pady=36,
            ).grid(row=1, column=0, columnspan=len(headings), sticky="ew")

        for index, record in enumerate(visible):
            row = index + 1
            selected = str(record.get("match_id") or "") == self.overview_selected_match_id
            background = ROW_SELECTED if selected else ROW_EVEN if index % 2 == 0 else ROW_ODD
            player = record.get("player", {}) if isinstance(record.get("player"), dict) else {}
            duration_cell = self.overview_text_cell(
                table,
                record,
                row,
                0,
                compact_duration(record.get("duration_seconds")),
                self.overview_started_at(record.get("started_at_ns")),
                background=background,
                primary_color=TEAL if selected else TEXT,
                anchor="w",
            )
            if selected:
                tk.Frame(duration_cell, bg=TEAL, width=3).place(x=0, y=0, relheight=1.0)
            self.overview_text_cell(
                table,
                record,
                row,
                1,
                str(record.get("mode_name") or "--"),
                str(record.get("map_name") or "--"),
                background=background,
                anchor="w",
            )

            identity = tk.Frame(table, bg=background)
            identity.grid(row=row, column=2, sticky="nsew", padx=(0, 1), pady=(1, 0))
            identity_body = tk.Frame(identity, bg=background)
            identity_body.pack(expand=True, fill="x", padx=7, pady=6)
            self.icon(
                identity_body,
                player.get("profession_id"),
                size=27,
                bg=background,
            )
            identity_text = tk.Frame(identity_body, bg=background)
            identity_text.pack(side="left", fill="x", expand=True)
            self.label(
                identity_text,
                self.name(player.get("name"), local=True),
                role="strong",
                bg=background,
                anchor="w",
            ).pack(fill="x")
            self.label(
                identity_text,
                self.profession(player.get("profession_id")),
                role="small",
                color=SUBTLE,
                bg=background,
                anchor="w",
            ).pack(fill="x", pady=(1, 0))
            self.bind_overview_cell(identity, record)

            self.overview_text_cell(
                table,
                record,
                row,
                3,
                number(player.get("extraordinary_rating")),
                background=background,
            )
            self.overview_text_cell(
                table,
                record,
                row,
                4,
                " / ".join(number(record.get(field)) for field in ("kills", "assists", "deaths")),
                "击杀 / 助攻 / 阵亡",
                background=background,
                primary_color=TEAL,
            )
            self.overview_text_cell(
                table,
                record,
                row,
                5,
                number(record.get("damage")),
                "伤害",
                background=background,
                primary_color=CYAN,
            )

            outcome, outcome_color, result_icon = self.overview_outcome(record)
            result_cell = tk.Frame(table, bg=background)
            result_cell.grid(row=row, column=6, sticky="nsew", padx=(0, 1), pady=(1, 0))
            result_body = tk.Frame(result_cell, bg=background)
            result_body.pack(expand=True)
            self.icon_label(
                result_body,
                result_icon,
                size=16,
                color=outcome_color,
                bg=background,
            ).pack(side="left", padx=(0, 4))
            self.label(
                result_body,
                outcome,
                color=outcome_color,
                role="small",
                bg=background,
            ).pack(side="left")
            self.bind_overview_cell(result_cell, record)

            view = tk.Frame(table, bg=background)
            view.grid(row=row, column=7, sticky="nsew", pady=(1, 0))
            eye = self.icon_label(view, "eye", size=19, color=TEAL, bg=background)
            eye.pack(expand=True)
            self.bind_overview_cell(view, record, details=True)

        pager = tk.Frame(card, bg=CARD)
        pager.pack(fill="x", padx=13, pady=10)
        self.label(
            pager,
            f"共 {total} 场",
            color=TEAL,
            role="small",
            bg=CARD,
        ).pack(side="left")
        self.label(
            pager,
            f"当前 {self.page_index * self.PAGE_SIZE + 1 if total else 0}–{min(total, (self.page_index + 1) * self.PAGE_SIZE)} 条",
            color=MUTED,
            role="small",
            bg=CARD,
        ).pack(side="left", padx=(12, 0))

        def turn(change):
            next_page = max(0, min(pages - 1, self.page_index + change))
            if next_page != self.page_index:
                self.page_index = next_page
                self.render()
                self.canvas.yview_moveto(0)

        for icon_name, change, enabled in (
            ("chevron_right", 1, self.page_index < pages - 1),
            ("chevron_left", -1, self.page_index > 0),
        ):
            control = tk.Frame(pager, bg=PANEL_2, width=32, height=30)
            control.pack(side="right", padx=(6, 0))
            control.pack_propagate(False)
            icon = self.icon_label(
                control,
                icon_name,
                size=16,
                color=TEXT if enabled else SUBTLE,
                bg=PANEL_2,
            )
            icon.pack(fill="both", expand=True)
            if enabled:
                self.bind_click(control, lambda change=change: turn(change))
                self.bind_click(icon, lambda change=change: turn(change))
        self.label(
            pager,
            f"{self.page_index + 1} / {pages}",
            color=TEXT,
            role="small",
            bg=CARD,
        ).pack(side="right", padx=8)
        self.label(
            pager,
            f"{self.PAGE_SIZE} 条 / 页",
            color=MUTED,
            role="small",
            bg=CARD,
            padx=10,
            pady=6,
        ).pack(side="right", padx=(0, 9))

    def render_overview_snapshot(self, parent, record):
        try:
            parent.grid_rowconfigure(0, weight=1)
            parent.grid_columnconfigure(0, weight=1)
        except (AttributeError, tk.TclError):
            pass
        card = tk.Frame(
            parent,
            bg=CARD,
            highlightthickness=1,
            highlightbackground=EDGE,
        )
        card.grid(row=0, column=1, sticky="nsew")
        header = tk.Frame(card, bg=CARD)
        header.pack(fill="x", padx=13, pady=(12, 10))
        self.label(header, "战斗快照", role="strong", bg=CARD).pack(side="left")
        if not record:
            self.label(
                card,
                "请选择一条战斗记录",
                color=MUTED,
                bg=CARD,
                pady=80,
            ).pack(fill="x")
            return

        team_size = pvp_team_size(record)
        hunter = optional_integer(record.get("map_id")) in HUNTER_CITY_MAPS
        template = "终末猎杀" if hunter else f"{team_size}V{team_size}" if team_size else "PVP"
        self.label(header, template, color=GOLD, role="small", bg=CARD).pack(side="right")

        viewport = tk.Frame(card, bg=CARD)
        viewport.pack(fill="both", expand=True, padx=(12, 5), pady=(0, 7))
        canvas = tk.Canvas(viewport, bg=CARD, bd=0, highlightthickness=0, yscrollincrement=32)
        scroll = self.scrollbar_class(
            viewport,
            orient="vertical",
            command=canvas.yview,
            background=CARD,
            width=8,
        )
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=CARD)
        body_window = canvas.create_window(0, 0, anchor="nw", window=body)
        body.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(body_window, width=max(1, event.width)),
        )
        self.render_snapshot_meta(body, record)
        if hunter:
            self.render_hunter_snapshot(body, record)
        elif team_size == 1:
            self.render_snapshot_duel(body, record)
        else:
            self.render_snapshot_teams(body, record, team_size)
        self._bind_snapshot_scroll(body, canvas)

        action = self.button(
            card,
            "查看战斗详情  →",
            lambda record=record: self.open_record(record),
            primary=True,
        )
        action.pack(side="bottom", fill="x", padx=12, pady=(4, 12), ipady=4)

    def _bind_snapshot_scroll(self, widget, canvas):
        def wheel(event):
            delta = int(getattr(event, "delta", 0) or 0)
            if delta:
                canvas.yview_scroll(-1 if delta > 0 else 1, "units")
            return "break"

        widget.bind("<MouseWheel>", wheel, add="+")
        widget.bind("<Button-4>", lambda _event: (canvas.yview_scroll(-1, "units"), "break")[1], add="+")
        widget.bind("<Button-5>", lambda _event: (canvas.yview_scroll(1, "units"), "break")[1], add="+")
        for child in widget.winfo_children():
            self._bind_snapshot_scroll(child, canvas)

    def render_snapshot_meta(self, parent, record):
        outcome, outcome_color, result_icon = self.overview_outcome(record)
        if optional_integer(record.get("map_id")) in HUNTER_CITY_MAPS:
            outcome, outcome_color, result_icon = "该类型无结果", MUTED, None
        panel = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
        panel.pack(fill="x")
        title = tk.Frame(panel, bg=PANEL)
        title.pack(fill="x", padx=10, pady=(9, 5))
        self.label(
            title,
            str(record.get("mode_name") or "未知玩法"),
            role="strong",
            bg=PANEL,
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        badge = tk.Frame(title, bg=PANEL)
        badge.pack(side="right")
        if result_icon:
            self.icon_label(badge, result_icon, size=15, color=outcome_color, bg=PANEL).pack(side="left")
        self.label(badge, outcome, color=outcome_color, role="small", bg=PANEL).pack(side="left", padx=(4, 0))
        for caption, value in (
            ("地图", record.get("map_name") or "--"),
            ("开始", full_stamp(record.get("started_at_ns"))),
            ("时长", compact_duration(record.get("duration_seconds"))),
        ):
            row = tk.Frame(panel, bg=PANEL)
            row.pack(fill="x", padx=10, pady=(0, 4))
            self.label(row, caption, color=SUBTLE, role="micro", bg=PANEL).pack(side="left")
            self.label(row, str(value), color=MUTED, role="micro", bg=PANEL, anchor="e").pack(side="right")
        tk.Frame(panel, bg=PANEL, height=4).pack()

    def render_snapshot_duel(self, parent, record):
        allies, enemies = pvp_snapshot_rosters(record)
        local = allies[0] if allies else _participant_row({}, is_self=True)
        enemy = enemies[0] if enemies else _participant_row({})
        versus = tk.Frame(parent, bg=CARD)
        versus.pack(fill="x", pady=(9, 0))
        versus.grid_columnconfigure(0, weight=1, uniform="duel-side")
        versus.grid_columnconfigure(1, weight=0)
        versus.grid_columnconfigure(2, weight=1, uniform="duel-side")
        self.render_snapshot_duel_side(versus, local, 0, local=True)
        self.label(versus, "VS", color=RED, role="title", bg=CARD).grid(row=0, column=1, padx=7, sticky="n")
        self.render_snapshot_duel_side(versus, enemy, 2, local=False)
        history = pvp_opponent_history(self.records, enemy.get("character_id"))
        self.render_snapshot_history(parent, enemy, history, include_recent=True)

    def render_snapshot_duel_side(self, parent, row, column, *, local):
        background = blend_color(PANEL, TEAL if local else RED, 0.08)
        card = tk.Frame(
            parent,
            bg=background,
            width=122,
            height=218,
            highlightthickness=1,
            highlightbackground=EDGE,
        )
        card.grid(row=0, column=column, sticky="nsew")
        card.grid_propagate(False)
        card.pack_propagate(False)
        self.label(
            card,
            "我" if local else "对手",
            color=TEAL if local else RED,
            role="micro",
            bg=background,
        ).pack(fill="x", padx=6, pady=(6, 0))
        portrait = self._history_profession_icon(row.get("profession_id"), 30)
        self.images.append(portrait)
        tk.Label(card, image=portrait, bg=background, bd=0).pack(pady=(3, 0))
        shown_name = self.name(row.get("name"), local=local)
        self.label(
            card,
            shown_name,
            role="strong",
            bg=background,
            wraplength=108,
        ).pack(fill="x", padx=6, pady=(1, 0))
        self.label(
            card,
            f"非凡评分 {number(row.get('extraordinary_rating'))}",
            color=TEAL if row.get("extraordinary_rating") is not None else SUBTLE,
            role="micro",
            bg=background,
        ).pack(fill="x", padx=6, pady=(2, 7))
        for caption, field, color in (
            ("击败", "kills", TEAL),
            ("伤害", "damage", CYAN),
            ("治疗", "healing", GOLD),
            ("承伤", "taken", RED),
        ):
            line = tk.Frame(card, bg=background)
            line.pack(fill="x", padx=7, pady=2)
            self.label(line, caption, color=MUTED, role="micro", bg=background).pack(side="left")
            self.label(line, compact_number(row.get(field)), color=color, role="micro", bg=background).pack(side="right")
        tk.Frame(card, bg=background, height=6).pack()

    def render_hunter_snapshot(self, parent, record):
        allies, _enemies = pvp_snapshot_rosters(record)
        allies = [row for row in allies if not row.get("is_ai")]
        ratings = [
            row["extraordinary_rating"]
            for row in allies
            if not row.get("is_ai") and row.get("extraordinary_rating") is not None
        ]
        score = round(sum(ratings) / len(ratings)) if ratings else None
        overview = tk.Frame(parent, bg="#142328", highlightthickness=1,
                            highlightbackground=REPORT_BORDER)
        overview.pack(fill="x", pady=(10, 0))
        tk.Frame(overview, bg=TEAL, width=3).pack(side="left", fill="y")
        metrics = tk.Frame(overview, bg="#142328")
        metrics.pack(fill="x", padx=12, pady=11)
        self.label(metrics, "我方出战", color=REPORT_SOFT, role="micro",
                   bg="#142328").grid(row=0, column=0, sticky="w")
        self.label(metrics, "平均非凡评分", color=REPORT_SOFT, role="micro",
                   bg="#142328").grid(row=0, column=1, sticky="e")
        self.label(metrics, f"{len(allies)} 人", color=TEAL, role="title",
                   bg="#142328").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.label(metrics, number(score), color=GOLD, role="title",
                   bg="#142328").grid(row=1, column=1, sticky="e", pady=(4, 0))
        metrics.grid_columnconfigure(0, weight=1)
        metrics.grid_columnconfigure(1, weight=1)

        heading = tk.Frame(parent, bg=CARD)
        heading.pack(fill="x", pady=(15, 6))
        self.label(heading, "我方阵容", color=TEXT, role="strong",
                   bg=CARD).pack(side="left")
        self.label(heading, f"{len(ratings)} 人有评分", color=MUTED,
                   role="micro", bg=CARD).pack(side="right")
        roster = tk.Frame(parent, bg=EDGE, highlightthickness=1,
                          highlightbackground=EDGE)
        roster.pack(fill="x")
        for index, row in enumerate(allies):
            background = ROW_SELECTED if row.get("is_self") else ROW_EVEN if index % 2 else ROW_ODD
            line = tk.Frame(roster, bg=background, height=39)
            line.pack(fill="x", pady=(0, 1))
            line.pack_propagate(False)
            if row.get("profession_id"):
                self.icon(line, row["profession_id"], size=24,
                          bg=background).pack_configure(padx=(9, 5))
            else:
                tk.Frame(line, bg=background, width=38).pack(side="left")
            self.label(line, self.name(row.get("name"), index,
                                       local=bool(row.get("is_self"))),
                       bg=background, color=TEAL if row.get("is_self") else TEXT,
                       role="small", anchor="w").pack(side="left", fill="x",
                                                       expand=True)
            self.label(line, number(row.get("extraordinary_rating")),
                       bg=background, color=GOLD if row.get("extraordinary_rating") is not None else SUBTLE,
                       role="small", anchor="e").pack(side="right", padx=10)

    def render_snapshot_teams(self, parent, record, team_size):
        allies, enemies = pvp_snapshot_rosters(record)
        team_caption = (
            f"我方{team_size}人  VS  敌方{team_size}人"
            if team_size in {3, 6, 12}
            else "我方  VS  敌方"
        )
        self.label(
            parent,
            team_caption,
            color=TEXT,
            role="strong",
            bg=CARD,
            anchor="w",
        ).pack(fill="x", pady=(9, 0))
        if team_size in {6, 12, 60}:
            averages = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=EDGE)
            averages.pack(fill="x", pady=(9, 0))

            def average(rows):
                ratings = [
                    row.get("extraordinary_rating")
                    for row in rows
                    if not row.get("is_ai") and row.get("extraordinary_rating") is not None
                ]
                return round(sum(ratings) / len(ratings)) if ratings else None

            self.label(
                averages,
                f"我方平均 {number(average(allies))}",
                color=GOLD,
                role="micro",
                bg=SURFACE,
                anchor="w",
            ).pack(fill="x", padx=8, pady=(5, 0))
            self.label(
                averages,
                f"敌方平均 {number(average(enemies))}",
                color=GOLD,
                role="micro",
                bg=SURFACE,
                anchor="w",
            ).pack(fill="x", padx=8, pady=(0, 5))

        self.render_snapshot_team_table(parent, "我方", allies, team_size, friendly=True)
        self.render_snapshot_team_table(parent, "敌方", enemies, team_size, friendly=False)
        histories = []
        for enemy in enemies:
            uid = enemy.get("character_id")
            if uid:
                histories.append((enemy, pvp_opponent_history(self.records, uid)))
        if team_size == 12:
            histories.sort(key=lambda item: (item[1]["total"], item[1]["recent"] != []), reverse=True)
            histories = histories[:5]
        self.render_snapshot_team_history(parent, histories)

    def render_snapshot_team_table(self, parent, caption, rows, team_size, *, friendly):
        section = tk.Frame(parent, bg=CARD)
        section.pack(fill="x", pady=(9, 0))
        self.label(
            section,
            f"{caption} {team_size or len(rows)}人",
            color=TEAL if friendly else RED,
            role="strong",
            bg=CARD,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        include_rating = team_size in {6, 12, 60}
        table = tk.Frame(section, bg=EDGE, highlightthickness=1, highlightbackground=EDGE)
        table.pack(fill="x")
        ranked = sorted(
            rows,
            key=lambda row: (
                row.get("damage") is not None,
                row.get("damage") or 0,
                row.get("is_self") is True,
            ),
            reverse=True,
        )
        if team_size:
            ranked = ranked[:team_size]
            ranked += [_participant_row({"name": "--"}) for _ in range(max(0, team_size - len(ranked)))]
        header = tk.Frame(table, bg=ROW_HEADER)
        header.pack(fill="x")
        self.label(
            header,
            "玩家 / " + ("超凡评分 / " if include_rating else "") + "击败 · 伤害 · 治疗 · 承伤",
            color=MUTED,
            role="micro",
            bg=ROW_HEADER,
            anchor="w",
            padx=7,
            pady=5,
        ).pack(fill="x")
        # A two-line row keeps all six fields readable in the fixed native
        # snapshot column.  The outer snapshot canvas supplies the only
        # scrolling needed for 6V6/12V12.
        for index, row in enumerate(ranked, 1):
            background = ROW_SELECTED if row.get("is_self") else ROW_EVEN if index % 2 else ROW_ODD
            line = tk.Frame(table, bg=background, height=48)
            line.pack(fill="x", pady=(1, 0))
            line.pack_propagate(False)
            try:
                profession_id = int(row.get("profession_id") or 0)
            except (TypeError, ValueError, OverflowError):
                profession_id = 0
            if profession_id:
                self.icon(
                    line,
                    profession_id,
                    size=30,
                    bg=background,
                ).pack_configure(padx=(7, 2))
            else:
                tk.Frame(line, bg=background, width=39).pack(
                    side="left",
                    fill="y",
                )
            identity = tk.Frame(line, bg=background)
            identity.pack(side="left", fill="both", expand=True, padx=(2, 3))
            name = self.name(row.get("name"), index - 1, local=bool(row.get("is_self")))
            self.label(
                identity,
                name + (f"  ·  {number(row.get('extraordinary_rating'))}" if include_rating else ""),
                color=TEAL if row.get("is_self") else TEXT,
                role="micro",
                bg=background,
                anchor="w",
            ).pack(fill="x", pady=(3, 0))
            metrics_values = [
                compact_number(row.get(field))
                for field in ("kills", "damage", "healing", "taken")
            ]
            metrics = f"击败 {metrics_values[0]}  伤害 {metrics_values[1]}"
            metrics_extra = f"治疗 {metrics_values[2]}  承伤 {metrics_values[3]}"
            self.label(
                identity,
                metrics,
                color=MUTED,
                role="micro",
                bg=background,
                anchor="w",
            ).pack(fill="x", pady=(0, 2))
            self.label(
                identity,
                metrics_extra,
                color=MUTED,
                role="micro",
                bg=background,
                anchor="w",
            ).pack(fill="x")

    def render_snapshot_history(self, parent, opponent, history, *, include_recent):
        box = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=EDGE)
        box.pack(fill="x", pady=(9, 0))
        self.label(box, "历史交手", role="strong", bg=SURFACE, anchor="w").pack(fill="x", padx=9, pady=(8, 4))
        values = (
            f"总交手 {history['total']}   胜 {history['wins']}   负 {history['losses']}   未知 {history['unknown']}"
            if opponent.get("character_id")
            else "缺少角色唯一 UID，暂不计入历史交手"
        )
        self.label(box, values, color=MUTED, role="micro", bg=SURFACE, anchor="w").pack(fill="x", padx=9)
        if include_recent:
            recent = " / ".join({"胜利": "胜", "失败": "负"}.get(item, "未知") for item in history["recent"])
            self.label(
                box,
                "最近5场  " + (recent or "--"),
                color=SUBTLE,
                role="micro",
                bg=SURFACE,
                anchor="w",
            ).pack(fill="x", padx=9, pady=(4, 8))
        else:
            tk.Frame(box, bg=SURFACE, height=7).pack()

    def render_snapshot_team_history(self, parent, histories):
        box = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=EDGE)
        box.pack(fill="x", pady=(9, 0))
        self.label(box, "历史交手", role="strong", bg=SURFACE, anchor="w").pack(fill="x", padx=9, pady=(8, 5))
        if not histories:
            self.label(
                box,
                "暂无带角色唯一 UID 的敌方记录",
                color=SUBTLE,
                role="micro",
                bg=SURFACE,
                anchor="w",
            ).pack(fill="x", padx=9, pady=(0, 8))
            return
        for index, (enemy, history) in enumerate(histories):
            row = tk.Frame(box, bg=SURFACE)
            row.pack(fill="x", padx=9, pady=(0, 6))
            self.label(
                row,
                self.name(enemy.get("name"), index),
                color=TEXT,
                role="micro",
                bg=SURFACE,
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            self.label(
                row,
                f"{history['total']}场  {history['wins']}胜  {history['losses']}负  {history['unknown']}未知",
                color=MUTED,
                role="micro",
                bg=SURFACE,
                anchor="e",
            ).pack(side="right")

    def select_overview_record(self, record):
        self.overview_selected_match_id = str(record.get("match_id") or "")
        position = self.canvas.yview()[0] if self.canvas.winfo_exists() else 0.0
        self.render()
        self.canvas.yview_moveto(position)

    def render_detail(self):
        record = self.selected_record or {}
        player = record.get("player", {}) if isinstance(record.get("player"), dict) else {}
        hunter_city = int(record.get("map_id") or 0) in HUNTER_CITY_MAPS
        opponents = (
            hunter_detail_opponents(record)
            if hunter_city else
            [row for row in record.get("opponents", ()) if isinstance(row, dict)]
        )
        selected_uid = participant_uid(self.selected_opponent)
        self.selected_opponent = next(
            (row for row in opponents if participant_uid(row) == selected_uid),
            None,
        ) if selected_uid else None
        if self.selected_opponent not in opponents:
            self.selected_opponent = opponents[0] if opponents else None
        if hunter_city and isinstance(self.selected_opponent, dict):
            opponent = self.selected_opponent
            if not isinstance(opponent.get("equipment_snapshot"), dict):
                uid = participant_uid(opponent)
                key = (str(record.get("match_id") or ""), uid)
                if uid and key not in self._hunter_equipment_lookup_cache:
                    reader = getattr(
                        getattr(self.host, "pvp_history_repository", None),
                        "equipment_snapshot_at_or_before", None,
                    )
                    snapshot = None
                    if callable(reader) and self.principal():
                        snapshot = reader(
                            self.principal(), uid,
                            record.get("ended_at_ns"),
                            extraordinary_rating=opponent.get("extraordinary_rating"),
                        )
                    started = int(record.get("started_at_ns") or 0)
                    captured = int(snapshot.get("captured_at_ns") or 0) if isinstance(snapshot, dict) else 0
                    self._hunter_equipment_lookup_cache[key] = (
                        snapshot if captured >= started else None
                    )
                snapshot = self._hunter_equipment_lookup_cache.get(key)
                if isinstance(snapshot, dict):
                    opponent["equipment_snapshot"] = snapshot
                    if opponent.get("extraordinary_rating") is None:
                        opponent["extraordinary_rating"] = snapshot.get("extraordinary_rating")

        if hunter_city:
            self.render_detail_hero(record, player, len(opponents))
            self.render_detail_toolbar(record)
        else:
            self.render_detail_toolbar(record)
            self.render_detail_hero(record, player, len(opponents))
        self.render_detail_metrics(record)
        self.render_detail_tabs()

        workspace = build_native_detail_workspace(
            self.content,
            split_pane_class=self.split_pane_class,
            background=BG,
            panel=PANEL,
            border=EDGE,
            bottom_padding=22,
        )
        self.render_detail_opponents(
            workspace.left,
            opponents,
            placed=False,
            use_parent=True,
        )
        self.render_detail_player(
            workspace.right,
            opponents,
            placed=False,
            use_parent=True,
        )

    def render_detail_toolbar(self, record):
        _placeholder, _shell, toolbar = build_native_detail_toolbar(
            self.content,
            background=BG,
        )
        back = self.label(
            toolbar,
            "←  返回战盟中枢" if self.external_back and self.fixed_records is not None
            and self.page_key == "alliance" else "←  返回战斗记录",
            role="strong",
            color=TEAL,
            bg=BG,
            cursor="hand2",
        )
        back.pack(side="left", fill="y")
        back.bind("<Button-1>", lambda _event: (
            self.external_back() if self.external_back and self.fixed_records is not None
            else self.overview()
        ))
        self.label(
            toolbar,
            "/  战斗详情",
            color=MUTED,
            role="small",
            bg=BG,
        ).pack(side="left", fill="y", padx=(9, 0))
        self.label(
            toolbar,
            match_caption(record.get("match_id")),
            color=SUBTLE,
            role="small",
            bg=BG,
        ).pack(side="right", fill="y")

    def render_detail_hero(self, record, player, opponent_count):
        hero = build_native_detail_hero(
            self.content,
        )
        if int(record.get("map_id") or 0) in HUNTER_CITY_MAPS:
            hero.configure(height=220)
        # The native PVE banner keeps hidden state labels beside its Canvas so
        # accessibility and UI automation can read what the Canvas paints.
        hero._archive_state_label = tk.Label(hero, text="竞技战绩")
        hero.bind(
            "<Configure>",
            lambda event, canvas=hero, record=record, player=player, opponent_count=opponent_count: (
                self.draw_detail_hero(canvas, record, player, opponent_count)
            ),
        )

    def draw_detail_hero(self, canvas, record, player, opponent_count):
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        map_name = str(record.get("map_name") or "未知 PvP 战场")
        mode_name = str(record.get("mode_name") or "模式未知")
        hunter_city = int(record.get("map_id") or 0) in HUNTER_CITY_MAPS
        if hunter_city and HUNTER_REPORT_BANNER.is_file():
            if self._hunter_detail_banner_source is None:
                with Image.open(HUNTER_REPORT_BANNER) as source:
                    self._hunter_detail_banner_source = source.convert("RGBA")
            key = (width, height)
            backdrop = self._hunter_detail_banner_cache.get(key)
            if backdrop is None:
                source = self._hunter_detail_banner_source
                scale = max(width / source.width, height / source.height)
                resized = source.resize(
                    (max(width, round(source.width * scale)),
                     max(height, round(source.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                left = max(0, (resized.width - width) // 2)
                top = max(0, round((resized.height - height) * 0.2))
                frame = resized.crop((left, top, left + width, top + height))
                shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                shade_draw = ImageDraw.Draw(shade)
                for x in range(width):
                    alpha = round(190 * (1 - x / max(1, width - 1)) ** 1.6)
                    shade_draw.line((x, 0, x, height), fill=(3, 10, 14, alpha))
                backdrop = ImageTk.PhotoImage(
                    Image.alpha_composite(frame, shade), master=canvas
                )
                self._hunter_detail_banner_cache = {key: backdrop}
        else:
            backdrop = self.host.icons.history_hero(map_name, width, height)
        canvas.create_image(0, 0, image=backdrop, anchor="nw")
        canvas._pvp_history_hero = backdrop
        canvas.create_rectangle(0, 0, 4, height, fill=TEAL, outline="")
        canvas.create_line(4, 0, 4, height, fill="#b7f5df", width=1)

        tag_y = 28
        tag_width = min(230, max(86, self.host._ui_font("micro").measure(mode_name) + 22))
        canvas.create_rectangle(28, tag_y, 28 + tag_width, tag_y + 24, fill=SURFACE, outline=EDGE)
        canvas.create_text(39, tag_y + 12, text=mode_name, fill=TEXT, anchor="w", font=self.host._ui_font("micro"))
        type_text = f"{pvp_team_size(record)}V{pvp_team_size(record)}" if pvp_team_size(record) else "PVP"
        type_width = max(72, self.host._ui_font("micro").measure(type_text) + 22)
        type_x = 36 + tag_width
        canvas.create_rectangle(type_x, tag_y, type_x + type_width, tag_y + 24, fill=PANEL, outline=EDGE)
        canvas.create_text(type_x + type_width / 2, tag_y + 12, text=type_text, fill=MUTED, font=self.host._ui_font("micro"))
        canvas.create_text(28, 75, text=map_name, fill=TEXT, anchor="w", font=self.host._ui_font("hero_title"))
        canvas.create_text(30, 113, text="PVP 战斗详情", fill=MUTED, anchor="w", font=self.host._ui_font("micro"))
        canvas.create_text(
            29,
            height - 30,
            text=(
                f"{full_stamp(record.get('started_at_ns'))}    ◆    "
                f"战斗时长 {compact_duration(record.get('duration_seconds'))}    ◆    "
                f"{opponent_count} 名已确认交手玩家"
            ),
            fill="#b7c4c0",
            anchor="w",
            font=self.host._ui_font("micro"),
        )

        if hunter_city:
            return
        portrait_size = min(156, max(96, height - 30))
        portrait_x = width - 102
        portrait_y = height // 2 - 2
        for inset, color, dash in ((0, "#6e3946", ()), (12, "#925162", (3, 4)), (25, "#47262f", ())):
            radius = portrait_size // 2 - inset
            if radius > 0:
                canvas.create_oval(
                    portrait_x - radius,
                    portrait_y - radius,
                    portrait_x + radius,
                    portrait_y + radius,
                    outline=color,
                    width=1,
                    dash=dash,
                )
        portrait = self._history_profession_icon(player.get("profession_id"), 112)
        canvas.create_image(portrait_x, portrait_y, image=portrait)
        canvas._pvp_player_portrait = portrait
        outcome, outcome_color, _outcome_icon = self.overview_outcome(record)
        result_width = 82
        result_left = width - result_width - 16
        result_top = height - 40
        canvas.create_polygon(
            result_left + 6,
            result_top,
            result_left + result_width,
            result_top,
            result_left + result_width,
            result_top + 30,
            result_left + result_width - 6,
            result_top + 36,
            result_left,
            result_top + 36,
            result_left,
            result_top + 6,
            fill=blend_color("#101615", outcome_color, 0.18),
            outline=outcome_color,
        )
        canvas.create_text(
            result_left + result_width / 2,
            result_top + 18,
            text=("✓  " if outcome == "胜利" else "◆  ") + outcome,
            fill=outcome_color,
            font=self.host._ui_font("strong"),
        )

    def render_detail_metrics(self, record):
        metrics = build_native_detail_metrics(self.content, border=EDGE)
        outcome, outcome_color, _outcome_icon = self.overview_outcome(record)
        hunter_city = int(record.get("map_id") or 0) in HUNTER_CITY_MAPS
        entries = (
            ("战斗时长", compact_duration(record.get("duration_seconds")), TEAL, "完整比赛用时"),
            ("造成伤害", number(record.get("damage", record.get("damage_done"))), CYAN, "Player → Player"),
            ("承受伤害", number(record.get("taken", record.get("damage_taken"))), RED, "Player ← Player"),
            ("本场战绩", " / ".join(number(record.get(field)) for field in (("kills", "deaths") if hunter_city else ("kills", "assists", "deaths"))), GOLD,
             "击杀 / 阵亡" if hunter_city else "击杀 / 助攻 / 阵亡"),
            ("玩法状态" if hunter_city else "战斗结果", outcome, outcome_color,
             "猎龙之城不判定胜负" if hunter_city else "整场比赛结果"),
        )
        for column, (caption, value, color, subtitle) in enumerate(entries):
            build_native_detail_metric_card(
                metrics,
                caption=caption,
                value=value,
                column=column,
                accent=color,
                subtitle=subtitle,
                surface=SURFACE,
                muted=MUTED,
                subtle=SUBTLE,
                font=self.host._ui_font,
            )

    def render_detail_tabs(self):
        bar = build_native_detail_tabs(
            self.content,
            surface=SURFACE,
            border=EDGE,
            text=TEXT,
            font=self.host._ui_font,
        )
        hunter_city = int((self.selected_record or {}).get("map_id") or 0) in HUNTER_CITY_MAPS
        tabs = (
            (*HUNTER_PAIR_TABS, "巨龙战果")
            if int((self.selected_record or {}).get("map_id") or 0) == 5200167
            else HUNTER_PAIR_TABS if hunter_city else PAIR_TABS
        )
        self.detail_tab_buttons = {}
        for caption in tabs:
            selected = caption == self.pair_tab
            tab = self.label(
                bar,
                caption,
                color=TEAL if selected else MUTED,
                role="strong" if selected else "small",
                bg=PANEL_2 if selected else SURFACE,
                padx=18,
                cursor="hand2",
                highlightthickness=1,
                highlightbackground=TEAL if selected else EDGE,
            )
            tab.pack(side="left", fill="y")
            self.detail_tab_buttons[caption] = tab
            tab.bind(
                "<Button-1>",
                lambda _event, caption=caption: self.choose_tab(caption),
            )
        self.label(
            bar,
            "点击左侧玩家，右侧查看双方直接交手数据",
            color=SUBTLE,
            role="small",
            bg=SURFACE,
        ).pack(side="right", fill="y", padx=13)

    def bind_opponent_row(self, widget, opponent):
        self.bind_click(widget, lambda opponent=opponent: self.choose_opponent(opponent))
        for child in widget.winfo_children():
            self.bind_click(child, lambda opponent=opponent: self.choose_opponent(opponent))
            for nested in child.winfo_children():
                self.bind_click(nested, lambda opponent=opponent: self.choose_opponent(opponent))

    def render_detail_opponents(
        self,
        parent,
        opponents,
        *,
        placed=True,
        use_parent=False,
    ):
        if use_parent:
            card = parent
        else:
            card = tk.Frame(
                parent,
                bg=PANEL,
                highlightthickness=1,
                highlightbackground=EDGE,
            )
            if placed:
                card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
            else:
                card.pack(fill="both", expand=True)
        header = tk.Frame(card, bg=SURFACE, height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.label(
            header,
            "本场交手玩家",
            role="title",
            bg=SURFACE,
        ).pack(side="left", fill="y", padx=13)
        self.label(
            header,
            f"{len(opponents)} 名玩家",
            color=MUTED,
            role="micro",
            bg=SURFACE,
        ).pack(side="right", fill="y", padx=13)
        roster = tk.Frame(card, bg=PANEL)
        roster.pack(fill="both", expand=True)
        canvas = tk.Canvas(
            roster,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            yscrollincrement=43,
        )
        scroll = self.scrollbar_class(
            roster,
            orient="vertical",
            command=canvas.yview,
            background=PANEL,
            width=8,
        )
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.bind(
            "<Configure>",
            lambda _event, canvas=canvas, opponents=opponents: (
                self.draw_detail_opponent_rows(canvas, opponents)
            ),
        )
        self._bind_snapshot_scroll(canvas, canvas)

    def draw_detail_opponent_rows(self, canvas, opponents):
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        if not opponents:
            canvas.create_text(
                width // 2,
                height // 2,
                text="本场尚未取得玩家交手数据",
                fill=MUTED,
                font=self.host._ui_font("strong"),
            )
            canvas.configure(scrollregion=(0, 0, width, height))
            return
        ranked = sorted(opponents, key=lambda row: int(row.get("damage") or 0), reverse=True)
        selected_id = str((self.selected_opponent or {}).get("opponent_id") or "")
        icons = []
        row_height = 43
        highest = max((int(row.get("damage") or 0) for row in ranked), default=0)
        for index, opponent in enumerate(ranked):
            top = index * row_height
            bottom = top + row_height
            center = top + row_height // 2
            opponent_id = str(opponent.get("opponent_id") or "")
            selected = opponent is self.selected_opponent or bool(selected_id and opponent_id == selected_id)
            base = blend_color(PANEL, TEAL, 0.10) if selected else PANEL if index % 2 == 0 else "#11161b"
            tag = f"pvp-opponent:{index}"
            canvas.create_rectangle(0, top, width, bottom - 1, fill=base, outline="", tags=(tag,))
            canvas.create_rectangle(
                0,
                top,
                3,
                bottom - 1,
                fill=TEAL if selected else blend_color(base, CYAN, 0.65),
                outline="",
                tags=(tag,),
            )
            damage = max(0, int(opponent.get("damage") or 0))
            if highest:
                canvas.create_rectangle(
                    3,
                    bottom - 3,
                    max(5, int(width * damage / highest)),
                    bottom - 1,
                    fill=blend_color(base, CYAN, 0.78),
                    outline="",
                    tags=(tag,),
                )
            canvas.create_text(
                21,
                center,
                text=str(index + 1),
                fill=TEAL if selected else GOLD if index < 3 else MUTED,
                font=self.host._ui_font("number_strong"),
                tags=(tag,),
            )
            icon = self._history_profession_icon(opponent.get("profession_id"), 21)
            icons.append(icon)
            canvas.create_image(38, top + 11, image=icon, anchor="nw", tags=(tag,))
            original_index = opponents.index(opponent)
            canvas.create_text(
                65,
                top + 13,
                text=self.name(opponent.get("name"), original_index),
                fill=TEXT,
                anchor="w",
                font=self.host._ui_font("strong"),
                tags=(tag,),
            )
            rating = optional_integer(opponent.get("extraordinary_rating"))
            if rating is None and isinstance(opponent.get("equipment_snapshot"), dict):
                rating = optional_integer(opponent["equipment_snapshot"].get("extraordinary_rating"))
            hunter_city = int((self.selected_record or {}).get("map_id") or 0) in HUNTER_CITY_MAPS
            rating_text = number(rating) if rating is not None else "未获取" if hunter_city else "--"
            canvas.create_text(
                65,
                top + 30,
                text=f"非凡评分 {rating_text}",
                fill=TEAL if rating is not None else SUBTLE,
                anchor="w",
                font=self.host._ui_font("micro"),
                tags=(tag,),
            )
            canvas.create_text(
                width - 13,
                top + 13,
                text=f"{number(opponent.get('damage'))}  伤害",
                fill=TEXT,
                anchor="e",
                font=self.host._ui_font("number_strong"),
                tags=(tag,),
            )
            canvas.create_text(
                width - 13,
                top + 30,
                text=f"击败 {number(opponent.get('kills'))}  ·  被击败 {number(opponent.get('defeats'))}",
                fill=MUTED,
                anchor="e",
                font=self.host._ui_font("micro"),
                tags=(tag,),
            )
            canvas.create_line(0, bottom - 1, width, bottom - 1, fill="#20262d")
            canvas.tag_bind(tag, "<Button-1>", lambda _event, row=opponent: self.choose_opponent(row))
            canvas.tag_bind(tag, "<Enter>", lambda _event, target=canvas: target.configure(cursor="hand2"))
            canvas.tag_bind(tag, "<Leave>", lambda _event, target=canvas: target.configure(cursor=""))
        canvas._pvp_opponent_icons = icons
        canvas.configure(scrollregion=(0, 0, width, max(height, len(ranked) * row_height)))

    def render_detail_player(
        self,
        parent,
        opponents,
        *,
        placed=True,
        use_parent=False,
    ):
        if use_parent:
            card = parent
        else:
            card = tk.Frame(
                parent,
                bg=PANEL,
                highlightthickness=1,
                highlightbackground=EDGE,
            )
            if placed:
                card.grid(row=0, column=1, sticky="nsew")
            else:
                card.pack(fill="both", expand=True)
        if not opponents or not isinstance(self.selected_opponent, dict):
            self.label(
                card,
                getattr(self, "detail_message", "请选择有交手数据的战斗记录"),
                color=MUTED,
                bg=PANEL,
                pady=72,
            ).pack(fill="x")
            return

        opponent = self.selected_opponent
        rating = optional_integer(opponent.get("extraordinary_rating"))
        if rating is None and isinstance(opponent.get("equipment_snapshot"), dict):
            rating = optional_integer(opponent["equipment_snapshot"].get("extraordinary_rating"))
        hunter_city = int((self.selected_record or {}).get("map_id") or 0) in HUNTER_CITY_MAPS
        rating_text = number(rating) if rating is not None else "未获取" if hunter_city else "--"
        selected_index = opponents.index(opponent)
        identity = tk.Frame(card, bg=SURFACE, height=92)
        identity.pack(fill="x")
        identity.pack_propagate(False)
        identity_copy = tk.Frame(identity, bg=SURFACE)
        identity_copy.pack(side="left", fill="both", expand=True, padx=(12, 5))
        shell = tk.Frame(
            identity_copy,
            bg=PANEL_2,
            width=50,
            height=50,
            highlightthickness=1,
            highlightbackground=EDGE,
        )
        shell.pack(side="left", pady=19)
        shell.pack_propagate(False)
        self.icon(shell, opponent.get("profession_id"), size=42, bg=PANEL_2).pack_configure(
            padx=4, pady=4
        )
        player_copy = tk.Frame(identity_copy, bg=SURFACE)
        player_copy.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.label(
            player_copy,
            self.name(opponent.get("name"), selected_index),
            role="title",
            bg=SURFACE,
            anchor="w",
        ).pack(fill="x", pady=(21, 0))
        self.label(
            player_copy,
            (
                (
                    (f"俱乐部 {opponent['club_name']}  ·  "
                     if opponent.get("club_name") else "")
                    + f"非凡评分 {rating_text}"
                ) if hunter_city else (
                    f"{self.profession(opponent.get('profession_id'))}  ·  "
                    f"俱乐部 {opponent.get('club_name') or '--'}  ·  "
                    f"非凡评分 {rating_text}"
                )
            ),
            color=MUTED,
            role="micro",
            bg=SURFACE,
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        stat_entries = (
            ("对其伤害", number(opponent.get("damage")), CYAN, 104),
            ("承受伤害", number(opponent.get("taken")), RED, 104),
            (
                "击败 / 被击败",
                f"{number(opponent.get('kills'))} / {number(opponent.get('defeats'))}",
                TEXT,
                130,
            ),
        )
        if not hunter_city:
            stat_entries += (("伤害占比", percent(opponent.get("damage_share")), TEAL, 104),)
        for caption, value, color, width in stat_entries:
            stat = tk.Frame(
                identity,
                bg=SURFACE,
                width=width,
                highlightthickness=1,
                highlightbackground=EDGE,
            )
            stat.pack(side="left", fill="y")
            stat.pack_propagate(False)
            self.label(
                stat,
                caption,
                color=MUTED,
                role="micro",
                bg=SURFACE,
                anchor="w",
            ).pack(fill="x", padx=11, pady=(21, 0))
            self.label(
                stat,
                value,
                color=color,
                role="number_strong",
                bg=SURFACE,
                anchor="w",
            ).pack(fill="x", padx=11, pady=(4, 0))

        if not hunter_city:
            try:
                share = min(1.0, max(0.0, float(opponent.get("damage_share") or 0.0)))
            except (TypeError, ValueError, OverflowError):
                share = 0.0
            share_bar = tk.Canvas(card, height=4, bg=SURFACE, bd=0, highlightthickness=0)
            share_bar.pack(fill="x")
            share_bar.bind(
                "<Configure>",
                lambda event, share=share, bar=share_bar: (
                    bar.delete("fill"),
                    bar.create_rectangle(
                        0,
                        0,
                        max(1, round(event.width * share)),
                        4,
                        fill=TEAL,
                        outline="",
                        tags="fill",
                    ),
                ),
            )

        descriptions = {
            "交手总览": "双方在本场战斗中的直接交手结果",
            "技能分布": "仅统计我与当前玩家之间的技能伤害",
            "战斗记录": "本场已识别的击杀与阵亡事件" if hunter_city else "本场已识别的击杀、助攻与阵亡事件",
            "装备快照": "仅展示本场获取的对手资料",
            "历史交手": "按角色身份汇总已保存的猎龙交手",
        }
        heading = tk.Frame(card, bg=DETAIL_HEADER, height=49)
        heading.pack(fill="x")
        heading.pack_propagate(False)
        self.detail_pair_heading_label = self.label(
            heading,
            self.pair_tab,
            role="title",
            bg=DETAIL_HEADER,
        )
        self.detail_pair_heading_label.pack(side="left", fill="y", padx=(12, 10))
        self.detail_pair_description_label = self.label(
            heading,
            descriptions[self.pair_tab],
            color=SUBTLE,
            role="micro",
            bg=DETAIL_HEADER,
        )
        self.detail_pair_description_label.pack(side="left", fill="y")

        body_shell = tk.Frame(card, bg=PANEL)
        body_shell.pack(fill="both", expand=True)
        body_canvas = tk.Canvas(
            body_shell,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            yscrollincrement=52,
        )
        body_scroll = self.scrollbar_class(
            body_shell,
            orient="vertical",
            command=body_canvas.yview,
            background=PANEL,
            width=8,
        )
        body_canvas.configure(yscrollcommand=body_scroll.set)
        body_scroll.pack(side="right", fill="y")
        body_canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(body_canvas, bg=PANEL)
        self.detail_pair_body = body
        self.detail_pair_canvas = body_canvas
        body_window = body_canvas.create_window(0, 0, anchor="nw", window=body)
        body.bind(
            "<Configure>",
            lambda _event: body_canvas.configure(scrollregion=body_canvas.bbox("all")),
        )
        body_canvas.bind(
            "<Configure>",
            lambda event: body_canvas.itemconfigure(body_window, width=max(1, event.width)),
        )
        self._render_detail_pair_body(body, body_canvas, opponent)

    def _render_detail_pair_body(self, body, body_canvas, opponent) -> None:
        """Swap only the tab body, keeping the detail shell and hero stable."""

        content = tk.Frame(body, bg=PANEL)
        content.pack(fill="both", expand=True)
        if self.pair_tab == "交手总览":
            self.render_pair_overview(content, opponent)
        elif self.pair_tab == "技能分布":
            self.render_skills(content, opponent)
        elif self.pair_tab == "战斗记录":
            self.render_pair_timeline(content, opponent)
        elif self.pair_tab == "装备快照":
            self.render_equipment(content, opponent.get("equipment_snapshot"))
        elif self.pair_tab == "巨龙战果":
            self.render_hunter_dragon(content)
        else:
            self.render_hunter_history(content, opponent)
        self._bind_snapshot_scroll(content, body_canvas)
        old = getattr(self, "detail_pair_content", None)
        self.detail_pair_content = content
        if old is not None and old is not content:
            old.destroy()

    def choose_opponent(self, opponent):
        self.selected_opponent = opponent
        self.hunter_history_offset = 0
        self.hunter_equipment_selected_index = 0
        self.render()

    def render_hunter_dragon(self, parent):
        header = tk.Frame(parent, bg=DETAIL_HEADER, height=54)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.label(header, "战争巨龙 · 独立统计", role="title", bg=DETAIL_HEADER,
                   anchor="w").pack(side="left", fill="y", padx=16)
        monsters = [
            row for row in (self.selected_record or {}).get("monsters", ())
            if isinstance(row, dict)
        ]
        if not monsters:
            self.label(parent, "本场没有确认的巨龙伤害记录。",
                       bg=PANEL, color=MUTED, role="small",
                       anchor="w").pack(fill="x", padx=18, pady=20)
            return
        for monster in monsters:
            box = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                           highlightbackground=EDGE)
            box.pack(fill="x", padx=12, pady=(12, 0))
            self.label(box, str(monster.get("name") or "战争巨龙"),
                       role="strong", bg=SURFACE, anchor="w").pack(
                           fill="x", padx=15, pady=(12, 5))
            self.label(box,
                       f"对龙伤害  {number(monster.get('damage_to'))}     "
                       f"承受龙伤害  {number(monster.get('damage_from'))}     "
                       + ("已击败" if monster.get("defeated") else "击败状态未确认"),
                       role="small", color=CYAN, bg=SURFACE,
                       anchor="w").pack(fill="x", padx=15, pady=(0, 12))
            for caption, key, color in (
                ("对龙技能", "skills_outgoing", CYAN),
                ("龙的技能", "skills_incoming", RED),
            ):
                skills = [
                    skill for skill in monster.get(key, ())
                    if isinstance(skill, dict) and (
                        int(skill.get("damage") or 0) > 0
                        or int(skill.get("hits") or 0) > 0
                    )
                ]
                if not skills:
                    continue
                self.label(box, caption, role="small", color=color,
                           bg=SURFACE, anchor="w").pack(fill="x", padx=15)
                for skill in sorted(skills, key=lambda row: -int(row.get("damage") or 0)):
                    skill_id = int(skill.get("skill_id") or 0)
                    name = (str(skill.get("name") or "").strip()
                            or getattr(self.host.model, "skill_names", {}).get(skill_id)
                            or f"技能 {skill_id}")
                    line = tk.Frame(box, bg=PANEL)
                    line.pack(fill="x", padx=15, pady=2)
                    icon = self.host.icons.skill(skill_id, name, 0, 26)
                    self.images.append(icon)
                    tk.Label(line, image=icon, bg=PANEL, bd=0).pack(
                        side="left", padx=(8, 8), pady=4)
                    self.label(line, name, bg=PANEL, role="small",
                               anchor="w").pack(side="left", fill="x", expand=True)
                    self.label(line,
                               f"{number(skill.get('damage'))} · {number(skill.get('hits'))} 次",
                               bg=PANEL, role="small", color=color).pack(
                                   side="right", padx=8)
                tk.Frame(box, bg=SURFACE, height=9).pack()

    def choose_tab(self, caption):
        self.pair_tab = caption
        for title, tab in getattr(self, "detail_tab_buttons", {}).items():
            selected = title == caption
            tab.configure(
                bg=PANEL_2 if selected else SURFACE,
                fg=TEAL if selected else MUTED,
                font=self.host._ui_font("strong" if selected else "small"),
                highlightbackground=TEAL if selected else EDGE,
            )
        body = getattr(self, "detail_pair_body", None)
        canvas = getattr(self, "detail_pair_canvas", None)
        opponent = self.selected_opponent
        if (
            body is None or canvas is None or opponent is None
            or not body.winfo_exists() or not canvas.winfo_exists()
        ):
            self.render()
            return
        descriptions = {
            "交手总览": "双方在本场战斗中的直接交手结果",
            "技能分布": "仅统计我与当前玩家之间的技能伤害",
            "战斗记录": "本场已识别的击杀与阵亡事件",
            "装备快照": "仅展示本场获取的对手资料",
            "历史交手": "按角色身份汇总已保存的猎龙交手",
            "巨龙战果": "本场确认的战争巨龙伤害与技能",
        }
        self.detail_pair_heading_label.configure(text=self.pair_tab)
        self.detail_pair_description_label.configure(
            text=descriptions.get(self.pair_tab, "")
        )
        self._render_detail_pair_body(body, canvas, opponent)

    def render_pair_overview(self, parent, opponent):
        taken_total = self.selected_record.get("taken")
        incoming_share = (opponent.get("taken", 0) / taken_total
                          if taken_total and opponent.get("taken") is not None else None)
        hunter_city = int(self.selected_record.get("map_id") or 0) in HUNTER_CITY_MAPS
        if hunter_city:
            for caption, value, count, color, reverse in (
                ("本人造成伤害", opponent.get("damage"), opponent.get("kills"), CYAN, False),
                ("本人承伤记录", opponent.get("taken"), opponent.get("defeats"), RED, True),
            ):
                card = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                                highlightbackground=EDGE)
                card.pack(fill="x", padx=12, pady=(8, 10))
                line = tk.Canvas(card, bg=SURFACE, height=45, highlightthickness=0)
                line.pack(fill="x")
                line.bind("<Configure>", lambda event, line=line, color=color,
                          reverse=reverse: self.draw_hunter_direction(line, event.width, color, reverse))
                labels = tk.Frame(card, bg=SURFACE)
                labels.pack(fill="x", padx=14, pady=(0, 12))
                self.label(labels, caption, bg=SURFACE, color=MUTED,
                           role="small").pack(side="left")
                self.label(labels, number(value), bg=SURFACE, color=color,
                           role="strong").pack(side="left", padx=(12, 0))
                self.label(labels,
                           f"{'阵亡' if reverse else '击杀'} {number(count)}",
                           bg=SURFACE, color=TEXT, role="small").pack(side="right")
        else:
            self._render_pair_overview_with_assists(parent, opponent, incoming_share)

        if hunter_city:
            return
        footer = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=EDGE)
        footer.pack(fill="x", padx=12, pady=(0, 10))
        self.label(
            footer,
            "最近一次交手",
            color=MUTED,
            role="small",
            bg=SURFACE,
        ).pack(side="left", padx=12, pady=10)
        self.label(
            footer,
            (
                f"{stamp(opponent.get('last_interaction_ns'))}   ·   "
                f"阵亡承伤占比 {percent(opponent.get('death_taken_share'))}"
            ),
            color=TEXT,
            role="strong",
            bg=SURFACE,
        ).pack(side="right", padx=12, pady=10)

    def draw_hunter_direction(self, canvas, width, color, reverse):
        canvas.delete("all")
        width = max(320, int(width or 0))
        left, right = 44, width - 44
        canvas.create_oval(left - 16, 8, left + 16, 40,
                           fill=TEAL, outline="")
        canvas.create_oval(right - 16, 8, right + 16, 40,
                           fill=RED, outline="")
        canvas.create_text(left, 24, text="我", fill="#081113",
                           font=self.host._ui_font("strong"))
        canvas.create_text(right, 24, text="对手", fill="#160b0c",
                           font=self.host._ui_font("small"))
        start, end = (right - 24, left + 25) if reverse else (left + 24, right - 25)
        canvas.create_line(start, 24, end, 24, fill=color, width=3,
                           arrow="last", arrowshape=(12, 14, 6))

    def _render_pair_overview_with_assists(self, parent, opponent, incoming_share):
        rows = (
            (
                "我 → 他",
                number(opponent.get("damage")),
                number(opponent.get("kills")),
                number(opponent.get("assists")),
                percent(opponent.get("damage_share")),
            ),
            (
                "他 → 我",
                number(opponent.get("taken")),
                number(opponent.get("defeats")),
                "--",
                percent(incoming_share),
            ),
        )
        self.table(
            parent,
            ("交手方向", "伤害", "击败 / 被击败", "助攻", "伤害占比"),
            rows,
            widths=(120, 105, 130, 80, 100),
        )

    def render_pair_timeline(self, parent, opponent):
        captions = {
            "kill": ("我击败目标", TEAL),
            "death": ("目标击败我", RED),
            "assist": ("我助攻击败目标", BLUE),
        }
        rows = []
        hunter_city = int(self.selected_record.get("map_id") or 0) in HUNTER_CITY_MAPS
        timeline = [event for event in opponent.get("timeline", ())
                    if isinstance(event, dict)
                    and (not hunter_city or event.get("type") != "assist")]
        for event in sorted(timeline, key=lambda row: int(row.get("timestamp_ns") or 0)):
            caption, _color = captions.get(event.get("type"), ("未知事件", MUTED))
            rows.append((stamp(event.get("timestamp_ns")), caption))
        self.table(parent, ("交手时间", "事件"), rows, widths=(180, 360))

    def render_hunter_history(self, parent, opponent):
        opponent_uid = participant_uid(opponent)
        history = pvp_hunter_opponent_history(
            self.records, opponent_uid
        )
        known = history["known_incoming"]
        incoming_text = (
            number(history["damage_from"]) if known else "未记录"
        )
        self.label(
            parent,
            (f"历史交手 {history['total']} 场  ·  我对他 {number(history['damage_to'])} 伤害"
             f"  ·  他对我 {incoming_text} 伤害  ·  击杀 {history['kills']} / 阵亡 {history['deaths']}"),
            color=CYAN, role="strong", anchor="w",
        ).pack(fill="x", padx=18, pady=(12, 14))
        self.label(
            parent,
            f"承伤记录  {known} / {history['total']} 场",
            color=MUTED, role="micro", anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 8))
        matches = history["matches"]
        page_size = 20
        offset = min(self.hunter_history_offset, max(0, len(matches) - 1) // page_size * page_size)
        rows = [
            (stamp(row["started_at_ns"]), str(row["mode_name"]),
             number(row["damage_to"]), number(row["damage_from"]),
             number(row["kills"]), number(row["deaths"]))
            for row in matches[offset:offset + page_size]
        ]
        self.table(
            parent,
            ("时间", "模式", "我对他伤害", "他对我伤害", "击杀", "阵亡"),
            rows, widths=(120, 100, 100, 100, 60, 60),
        )
        if len(matches) > page_size:
            pager = tk.Frame(parent, bg=PANEL)
            pager.pack(fill="x", padx=18, pady=(0, 12))
            self.label(
                pager, f"第 {offset // page_size + 1} / {(len(matches) - 1) // page_size + 1} 页",
                color=MUTED, bg=PANEL,
            ).pack(side="left")
            if offset > 0:
                self.button(pager, "上一页", lambda: self.set_hunter_history_offset(offset - page_size)).pack(side="right")
            if offset + page_size < len(matches):
                self.button(pager, "下一页", lambda: self.set_hunter_history_offset(offset + page_size)).pack(side="right", padx=(0, 8))

    def set_hunter_history_offset(self, offset):
        self.hunter_history_offset = max(0, int(offset))
        self.render()

    def render_skills(self, parent, opponent):
        hunter_city = int((self.selected_record or {}).get("map_id") or 0) in HUNTER_CITY_MAPS
        sides = tk.Frame(parent, bg=CARD)
        sides.pack(fill="x", padx=12, pady=(0, 12))
        directions = (
            (("本人使用的技能", "skills_outgoing"), ("对手使用的技能", "skills_incoming"))
            if hunter_city else
            (("我 → 他", "skills_outgoing"), ("他 → 我", "skills_incoming"))
        )
        for column, (caption, field) in enumerate(directions):
            sides.grid_columnconfigure(column, weight=1, uniform="skills")
            box = self.card(sides, caption, pack=False)
            box.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if not column else 0)
            if hunter_city:
                direction = tk.Canvas(box, bg=CARD, height=45, highlightthickness=0)
                direction.pack(fill="x", padx=12)
                direction.bind("<Configure>", lambda event, direction=direction,
                               column=column: self.draw_hunter_direction(
                                   direction, event.width,
                                   RED if column else CYAN, bool(column)))
                skills = [row for row in opponent.get(field, ()) if isinstance(row, dict)]
                total = sum(max(0, int(row.get("damage") or 0)) for row in skills)
                catalog = getattr(self.host.model, "skill_names", {})
                runtime = getattr(self.host.model, "runtime_skill_names", {})
                for raw in sorted(skills, key=lambda row: -(row.get("damage") or 0)):
                    amount = max(0, int(raw.get("damage") or 0))
                    hits = max(0, int(raw.get("hits") or 0))
                    if not amount and not hits:
                        continue
                    skill_id = int(raw.get("skill_id") or 0)
                    name = str(raw.get("name") or catalog.get(skill_id)
                               or runtime.get(skill_id) or f"技能 {skill_id}")
                    line = tk.Frame(box, bg=PANEL, height=52)
                    line.pack(fill="x", padx=12, pady=(0, 5))
                    line.pack_propagate(False)
                    icon = self.host.icons.skill(
                        skill_id, name,
                        (self.selected_record.get("player") or {}).get("profession_id")
                        if column == 0 else opponent.get("profession_id"),
                        30,
                    )
                    self.images.append(icon)
                    tk.Label(line, image=icon, bg=PANEL, bd=0).pack(
                        side="left", padx=(8, 9), pady=10)
                    self.label(line, name, bg=PANEL, role="small",
                               anchor="w").pack(side="left", fill="x", expand=True)
                    shown_share = f"{amount / total * 100:.1f}%" if total else "--"
                    cast_count = max(0, int(raw.get("casts") or 0))
                    count_text = f"释放 {cast_count} 次" if cast_count else f"命中 {hits} 次"
                    metric_text = (
                        f"{number(amount)}  ·  {count_text}  ·  {shown_share}"
                        if amount else f"{count_text}"
                    )
                    self.label(line, metric_text,
                               bg=PANEL, color=CYAN if column == 0 else RED,
                               role="small").pack(side="right", padx=9)
                continue
            rows = []
            catalog = getattr(self.host.model, "skill_names", {})
            runtime = getattr(self.host.model, "runtime_skill_names", {})
            for skill in opponent.get(field, []):
                if not any(
                    int(skill.get(key, 0) or 0) > 0
                    for key in ("damage", "hits", "casts")
                ):
                    continue
                skill_id = int(skill.get("skill_id") or 0)
                name = str(skill.get("name") or catalog.get(skill_id) or runtime.get(skill_id) or f"技能 {skill_id}")
                casts = max(0, int(skill.get("casts") or 0))
                hits = max(0, int(skill.get("hits") or 0))
                damage = skill.get("damage")
                rows.append((
                    name,
                    f"释放 {casts} 次" if casts else f"命中 {hits} 次",
                    number(damage) if damage else "--",
                    percent(skill.get("share")) if damage else "--",
                    number(skill.get("max_hit")) if damage else "--",
                ))
            self.table(box, ("技能", "释放 / 命中", "总伤害", "占比", "最高一击"), rows, widths=(110, 90, 70, 55, 70))

    def render_equipment(self, parent, snapshot):
        if not snapshot:
            self.label(parent, "装备快照 · 未获取\n本场未获取对手装备资料，不使用当前装备补写历史。", color=MUTED, pady=28).pack(fill="x")
            return
        captured = full_stamp(snapshot.get("captured_at_ns") or snapshot.get("captured_at"), seconds=True)
        hunter_city = int((self.selected_record or {}).get("map_id") or 0) in HUNTER_CITY_MAPS
        if hunter_city:
            self.label(parent, "本场装备快照 · " + captured, color=CYAN,
                       role="strong", anchor="w").pack(fill="x", padx=18, pady=(2, 5))
            self.label(parent, "仅使用本场采集资料；空白项不从当前角色资料补写。",
                       color=MUTED, role="small", anchor="w").pack(fill="x", padx=18, pady=(0, 12))
            canvas = tk.Canvas(parent, bg=PANEL, height=590, bd=0, highlightthickness=0)
            canvas.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            player = dict(self.selected_opponent or {}, equipment_snapshot=snapshot)

            def draw(event=None):
                if not canvas.winfo_exists():
                    return
                canvas.delete("all")
                self.host._draw_pvp_duel_equipment_analysis(
                    canvas,
                    max(680, int(getattr(event, "width", 0) or canvas.winfo_width())),
                    max(580, canvas.winfo_height()),
                    player,
                    selected_index_attr="hunter_equipment_selected_index",
                )

            def select_item(event):
                x = canvas.canvasx(event.x)
                y = canvas.canvasy(event.y)
                for left, top, right, bottom, index in getattr(
                    canvas, "_pvp_equipment_hit_rows", ()
                ):
                    if left <= x <= right and top <= y <= bottom:
                        self.hunter_equipment_selected_index = index
                        draw()
                        return

            canvas.bind("<Configure>", draw)
            canvas.bind("<Button-1>", select_item)
            self._bind_snapshot_scroll(canvas, canvas)
            draw()
            return
        self.label(parent, "装备快照 · " + captured, color=CYAN, role="strong", anchor="w").pack(fill="x", padx=18, pady=(2, 12))
        self.label(parent, "采集时的装备资料，不代表对方当前装备；仅展示实际收到的字段。",
                   color=MUTED, role="small", anchor="w").pack(fill="x", padx=18, pady=(0, 12))
        if snapshot.get("equipment") and not snapshot.get("equipment_score_complete"):
            self.label(parent, "非本人装备分数暂不包含强化分。",
                       color=MUTED, role="small", anchor="w").pack(fill="x", padx=18, pady=(0, 12))
        metrics = [("非凡评分", number(snapshot.get("extraordinary_rating")))]
        if snapshot.get("equipment_score") is not None:
            metrics.append(("装备评分", number(snapshot["equipment_score"])))
        self.table(parent, ("指标", "快照数值"), metrics)
        rows = []
        for row in snapshot.get("equipment", []):
            if not isinstance(row, dict):
                continue
            slot = row.get("slot_name") or f"槽位 {row.get('slot', '--')}"
            mode = "竞技" if row.get("is_pvp") or str(row.get("equipment_mode") or "").casefold() == "pvp" else "冒险"
            score = row.get("item_score", row.get("word_score"))
            rows.append(
                (
                    str(slot),
                    f"{mode}  ·  物品 {row.get('item_id', '--')}",
                    number(score),
                    ", ".join(str(value) for value in row.get("word_ids", []) or []) or "--",
                )
            )
        self.table(
            parent,
            ("装备槽位", "类型 / 物品", "装备分数", "词条"),
            rows,
            widths=(120, 190, 95, 160),
        )
        details = []
        for index, row in enumerate(snapshot.get("equipment", []), start=1):
            if not isinstance(row, dict):
                continue
            raw_details = row.get("details")
            if raw_details not in (None, {}, [], ""):
                details.append(
                    (
                        str(row.get("slot_name") or f"槽位 {row.get('slot', index)}"),
                        str(raw_details),
                    )
                )
        if details:
            self.table(parent, ("装备详情", "主动请求返回的原始字段"), details, widths=(150, 415))
        details = [(caption, str(snapshot[field])) for caption, field in
                   (("宝石", "gems"), ("主要属性", "attributes"), ("套装信息", "sets"))
                   if snapshot.get(field)]
        if details:
            self.table(parent, ("其他快照字段", "内容"), details)
        if snapshot.get("partial", True):
            self.label(parent, "部分装备快照：未返回的装备属性、宝石或套装信息不会推测补齐。",
                       color=MUTED, role="small", anchor="w").pack(fill="x", padx=18, pady=(0, 16))


class PvpAlliancePage(PvpPage):
    def __init__(self, parent, host, dropdown, scrollbar, *, split_pane=None):
        super().__init__(parent, host, "alliance", dropdown, scrollbar, split_pane)
        self.status = None
        self.query = tk.StringVar(self, "")
        self.form = None
        self.form_member = None
        self.message = ""
        self.child_history = None
        self.form_error = None
        self.mutation_pending = False
        self.analysis = None
        self.analysis_error = ""
        self.hunter_open = False
        self.analysis_days = tk.StringVar(self, "近30天")
        self.analysis_member = tk.StringVar(self, "全部成员")
        self.subscriptions = []
        self.search_results = []
        self.search_message = ""
        self.selected_player = None
        self.records_page = None
        self.report = None
        self.screen = "search"
        self.period = tk.StringVar(self, "全部时间")
        self.mode = tk.StringVar(self, "全部模式")
        self.result_filter = tk.StringVar(self, "全部结果")
        self.report_period = tk.StringVar(self, "近30天")
        self.report_metric = tk.StringVar(self, "伤害")
        self.report_selected = {}
        self.report_request_members = ()
        today = datetime.now().date()
        self.report_start_date = tk.StringVar(self, (today - timedelta(days=29)).isoformat())
        self.report_end_date = tk.StringVar(self, today.isoformat())
        self.render_admin()

    @property
    def authorized(self):
        return isinstance(self.status, dict) and self.status.get("authorized") is True

    def on_show(self):
        self.account_key = self.principal()
        self.pending.clear()
        self.status = None
        self.form = None
        self.analysis = None
        self.hunter_open = False
        self.subscriptions = []
        self.search_results = []
        self.selected_player = None
        self.records_page = None
        self.report = None
        self.report_selected = {}
        self.report_request_members = ()
        self.screen = "search"
        self.clear_child()
        self.render_admin()
        self.request("alliance/subscriptions/status", {},
                     self.subscription_status_received, channel="status")
        self.request("alliance/status", {"compact": True},
                     self.status_received, channel="alliance_status")

    def subscription_status_received(self, response):
        if response.get("ok"):
            self.subscriptions = list(response.get("subscriptions") or [])
        else:
            self.search_message = str(response.get("message") or "订阅列表暂不可用。")
        if self.screen == "search":
            self.render_admin()

    def status_received(self, response):
        if self.screen != "search":
            return
        self.status = response if response.get("ok") and response.get("authorized") is True else None
        if response.get("ok") and "subscriptions" in response:
            self.subscriptions = list(response.get("subscriptions") or [])
        self.render_admin()

    def render_locked(self, *, loading=False, error=""):
        self.search_message = error or ""
        self.render_admin()

    def _hub_panel(self, parent, title, subtitle, *, accent=EDGE):
        shell = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                         highlightbackground=EDGE)
        tk.Frame(shell, bg=accent, height=2).pack(fill="x")
        heading = tk.Frame(shell, bg=SURFACE)
        heading.pack(fill="x", padx=18, pady=(15, 12))
        self.label(heading, title, bg=SURFACE, role="strong", color=TEXT,
                   anchor="w").pack(fill="x")
        self.label(heading, subtitle, bg=SURFACE, role="micro", color=MUTED,
                   anchor="w").pack(fill="x", pady=(5, 0))
        body = tk.Frame(shell, bg=SURFACE)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        return body, shell

    def _hub_button(self, parent, caption, command, *, primary=False, accent=TEAL):
        normal_bg = accent if primary else PANEL_2
        normal_fg = BG if primary else TEXT
        button = tk.Label(
            parent, text=caption, bg=normal_bg, fg=normal_fg,
            font=self.host._ui_font("small"), padx=14, pady=8,
            highlightthickness=1, highlightbackground=accent if primary else EDGE,
            cursor="hand2",
        )
        button.bind("<Button-1>", lambda _event: command())
        button.bind("<Enter>", lambda _event: button.configure(
            bg=blend_color(normal_bg, TEXT, 0.13), fg=normal_fg))
        button.bind("<Leave>", lambda _event: button.configure(
            bg=normal_bg, fg=normal_fg))
        return button

    def render_admin(self):
        self.hunter_open = False
        self.screen = "search"
        self.clear()
        subscriptions = [row for row in self.subscriptions if isinstance(row, dict)]
        new_matches = sum(max(0, int(row.get("new_matches") or 0))
                          for row in subscriptions)
        alliance = self.status.get("alliance", {}) if self.authorized else {}
        alliance = alliance if isinstance(alliance, dict) else {}

        hero = tk.Frame(self.content, bg=SURFACE, highlightthickness=1,
                        highlightbackground=EDGE)
        hero.pack(fill="x", padx=20, pady=(18, 14))
        tk.Frame(hero, bg=TEAL, width=4).pack(side="left", fill="y")
        identity = tk.Frame(hero, bg=SURFACE)
        identity.pack(side="left", fill="both", expand=True, padx=22, pady=19)
        self.label(identity, "PVP  /  战盟数据", role="micro", bg=SURFACE,
                   color=TEAL, anchor="w").pack(fill="x")
        self.label(identity, "战盟中枢", role="settings_title", bg=SURFACE,
                   color=TEXT, anchor="w").pack(fill="x", pady=(4, 2))
        identity_line = (
            f"{alliance.get('club_name') or '已授权战盟'} · {alliance.get('admin_role') or '管理视图'}"
            if self.authorized else "查找角色、订阅战报，查看真实的 PvP 战斗详情"
        )
        self.label(identity, identity_line, role="small", bg=SURFACE,
                   color=MUTED, anchor="w").pack(fill="x")
        metrics = tk.Frame(hero, bg=SURFACE)
        metrics.pack(side="right", padx=(0, 20), pady=21)
        for caption, value, color in (
            ("已订阅角色", str(len(subscriptions)), TEXT),
            ("新增战斗", str(new_matches), TEAL if new_matches else MUTED),
        ):
            metric = tk.Frame(metrics, bg=SURFACE)
            metric.pack(side="left", padx=(23, 0))
            self.label(metric, value, role="title", bg=SURFACE, color=color,
                       anchor="e").pack(fill="x")
            self.label(metric, caption, role="micro", bg=SURFACE,
                       color=MUTED, anchor="e").pack(fill="x", pady=(2, 0))

        search_box, search_shell = self._hub_panel(
            self.content, "查找角色", "按角色名称检索已上传的 PvP 战绩", accent=CYAN)
        search_shell.pack(fill="x", padx=20, pady=(0, 14))
        controls = tk.Frame(search_box, bg=SURFACE)
        controls.pack(fill="x")
        entry_shell = tk.Frame(controls, bg=PANEL, highlightthickness=1,
                               highlightbackground=EDGE)
        entry_shell.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.icon_label(entry_shell, "search", size=17, color=CYAN,
                        bg=PANEL).pack(side="left", padx=(12, 8))
        search = tk.Entry(entry_shell, textvariable=self.query, bg=PANEL,
                          fg=TEXT, insertbackground=TEAL, relief="flat", bd=0,
                          font=self.host._ui_font("body"))
        search.pack(side="left", fill="x", expand=True, pady=11, padx=(0, 10))
        search.bind("<Return>", lambda _event: self.search_player())
        self._hub_button(controls, "搜索角色  →", self.search_player,
                         primary=True, accent=CYAN).pack(side="left")
        if self.search_message:
            self.label(search_box, self.search_message, bg=SURFACE, color=GOLD,
                       role="small", anchor="w").pack(fill="x", pady=(10, 0))

        columns = tk.Frame(self.content, bg=BG)
        columns.pack(fill="x", padx=20, pady=(0, 14))
        columns.grid_columnconfigure(0, weight=7, uniform="hub")
        columns.grid_columnconfigure(1, weight=5, uniform="hub")
        left = tk.Frame(columns, bg=BG)
        right = tk.Frame(columns, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        results, result_shell = self._hub_panel(
            left, "检索结果", "选择角色进入其完整战斗履历", accent=CYAN)
        result_shell.pack(fill="x")
        if not self.search_results:
            self.label(results, "输入角色名称后，相关战绩会出现在这里。",
                       bg=SURFACE, color=MUTED, role="small",
                       anchor="w").pack(fill="x", pady=(9, 12))
        for index, row in enumerate(self.search_results, 1):
            if not isinstance(row, dict):
                continue
            command = lambda player=row: self.select_player(player)
            tile = tk.Frame(results, bg=PANEL, highlightthickness=1,
                            highlightbackground=EDGE)
            tile.pack(fill="x", pady=(0, 7))
            self.bind_click(tile, command)
            self.label(tile, f"{index:02d}", bg=PANEL, color=CYAN,
                       role="small").pack(side="left", padx=(12, 12))
            identity = tk.Frame(tile, bg=PANEL)
            identity.pack(side="left", fill="x", expand=True, pady=10)
            name = self.label(identity, row.get("character_name") or "未知角色",
                              bg=PANEL, color=TEXT, role="strong", anchor="w")
            name.pack(fill="x")
            detail = self.label(
                identity,
                f"{self.profession(row.get('profession_id'))}  ·  {number(row.get('matches'))} 场",
                bg=PANEL, color=MUTED, role="micro", anchor="w")
            detail.pack(fill="x", pady=(3, 0))
            for widget in (name, detail):
                self.bind_click(widget, command)
            self._hub_button(tile, "查看战绩  →", command,
                             accent=CYAN).pack(side="right", padx=11)

        if self.authorized:
            admin, admin_shell = self._hub_panel(
                right, "猎龙之城战报", "战盟成员的已上传战绩与交手分析",
                accent=GOLD)
            admin_shell.pack(fill="x", pady=(0, 12))
            self.label(admin, alliance.get("club_name") or "已授权战盟",
                       bg=SURFACE, color=TEXT, role="strong",
                       anchor="w").pack(fill="x")
            self.label(admin,
                       f"{alliance.get('admin_role') or '管理员'} · 管理员战报",
                       bg=SURFACE, color=MUTED, role="micro",
                       anchor="w").pack(fill="x", pady=(4, 13))
            self._hub_button(admin, "打开战报与分析  →", self.open_hunter,
                             primary=True, accent=GOLD).pack(fill="x")

        updates, updates_shell = self._hub_panel(
            right, "订阅动态", f"已订阅 {len(subscriptions)} 位角色 · 新增 {new_matches} 场",
            accent=TEAL)
        updates_shell.pack(fill="x", pady=(0, 12))
        self._hub_button(updates, "生成订阅战报  →", self.open_report,
                         primary=True).pack(fill="x", pady=(0, 12))
        if not subscriptions:
            self.label(updates, "搜索并订阅角色后，新战斗会在这里汇总。",
                       bg=SURFACE, color=MUTED, role="small",
                       anchor="w").pack(fill="x", pady=(0, 7))
        for row in subscriptions:
            command = lambda player=row: self.select_player(player, subscribed=True)
            tile = tk.Frame(updates, bg=PANEL, highlightthickness=1,
                            highlightbackground=EDGE)
            tile.pack(fill="x", pady=(0, 7))
            self.bind_click(tile, command)
            identity = tk.Frame(tile, bg=PANEL)
            identity.pack(side="left", fill="x", expand=True, padx=11, pady=10)
            name = self.label(identity, row.get("character_name") or "未知角色",
                              bg=PANEL, color=TEXT, role="strong", anchor="w")
            name.pack(fill="x")
            detail = self.label(identity,
                                f"累计 {number(row.get('matches'))} 场",
                                bg=PANEL, color=MUTED, role="micro", anchor="w")
            detail.pack(fill="x", pady=(3, 0))
            for widget in (name, detail):
                self.bind_click(widget, command)
            fresh = max(0, int(row.get("new_matches") or 0))
            self.label(tile, f"+{fresh} 新场次" if fresh else "已查看",
                       bg=PANEL, color=TEAL if fresh else MUTED,
                       role="micro").pack(side="right", padx=(0, 12))

    @staticmethod
    def selected_days(value):
        return {"全部时间": 0, "近1天": 1, "近7天": 7,
                "近30天": 30, "近90天": 90}[value]

    @staticmethod
    def mode_choices():
        return ("全部模式", *sorted({row["mode_name"] for row in RECORDABLE_MAPS.values()}))

    def is_subscribed(self, member_key):
        return any(row["member_key"] == member_key for row in self.subscriptions)

    def search_player(self):
        query = self.query.get().strip()
        if not query:
            self.search_message = "请输入角色名称。"
            self.render_admin()
            return
        self.search_message = "正在查询角色…"
        self.search_results = []
        self.render_admin()
        self.request("alliance/players/search", {"query": query},
                     self.search_received, channel="player_search")

    def search_received(self, response):
        if self.screen != "search":
            return
        if response.get("ok"):
            self.search_results = list(response.get("players") or [])
            self.search_message = (
                "最多显示 30 位，请输入更完整的名称。" if len(self.search_results) >= 30
                else "" if self.search_results else "没有找到已上传记录的角色。"
            )
        else:
            self.search_message = str(response.get("message") or "角色查询失败，请重试。")
        self.render_admin()

    def select_player(self, player, *, subscribed=False):
        self.selected_player = dict(player)
        self.records_page = None
        self.record_offset = 0
        self.load_player_records()
        if subscribed:
            self.request("alliance/subscriptions/update",
                         {"action": "seen", "member_key": player["member_key"]},
                         self.subscription_received, channel="subscription")

    def load_player_records(self, offset=0):
        if not self.selected_player:
            return
        self.record_offset = max(0, int(offset))
        self.records_page = None
        self.search_message = "正在读取战斗记录…"
        self.render_player()
        self.request("alliance/players/records", {
            "member_key": self.selected_player["member_key"],
            "days": self.selected_days(self.period.get()),
            "mode_name": "" if self.mode.get() == "全部模式" else self.mode.get(),
            "result": "" if self.result_filter.get() == "全部结果" else self.result_filter.get(),
            "offset": self.record_offset,
        }, self.player_records_received, channel="player_records")

    def player_records_received(self, response):
        if self.screen != "player":
            return
        if response.get("ok"):
            self.records_page = response
            self.search_message = ""
        else:
            self.search_message = str(response.get("message") or "战斗记录读取失败，请重试。")
        self.render_player()

    def render_player(self):
        if not self.selected_player:
            self.render_admin()
            return
        self.screen = "player"
        self.clear()
        player = self.selected_player
        self.title(player.get("character_name") or "角色战报", "全部已上传 PvP 记录 · 每页 50 场")
        controls = self.card(self.content, "筛选记录")
        row = tk.Frame(controls, bg=CARD)
        row.pack(fill="x", padx=14, pady=(0, 14))
        self.button(row, "← 返回搜索", self.render_admin).pack(side="left", padx=(0, 12))
        self.dropdown(row, self.period, ("全部时间", "近1天", "近7天", "近30天", "近90天"),
                      width=130, font=self.host._ui_font("body"), background=CARD).pack(side="left", padx=(0, 10))
        self.dropdown(row, self.mode, self.mode_choices(),
                      width=190, font=self.host._ui_font("body"), background=CARD).pack(side="left", padx=(0, 10))
        self.dropdown(row, self.result_filter, ("全部结果", "胜利", "失败", "未知", "无胜负"),
                      width=120, font=self.host._ui_font("body"), background=CARD).pack(side="left", padx=(0, 10))
        self.button(row, "查询", lambda: self.load_player_records(), primary=True).pack(side="left")
        action = "取消订阅" if self.is_subscribed(player["member_key"]) else "订阅角色"
        self.button(row, action, self.toggle_subscription, primary=True).pack(side="right")
        if self.search_message:
            self.label(controls, self.search_message, color=MUTED, role="small",
                       anchor="w").pack(fill="x", padx=14, pady=(0, 12))
        if self.records_page is None:
            return
        data = self.records_page
        records = data.get("records", [])
        total = int(data.get("total") or 0)
        box = self.card(self.content, f"战斗记录 · 共 {number(total)} 场")
        self.table(box, ("时间", "模式", "地图", "结果", "击杀 / 阵亡", "伤害", "查看"),
                   [(stamp(row.get("started_at_ns")), row.get("mode_name", "--"),
                     row.get("map_name", "--"), row.get("result", "未知"),
                     f"{number(row.get('kills'))} / {number(row.get('deaths'))}",
                     number(row.get("damage")), "查看详情") for row in records],
                   widths=(140, 135, 120, 65, 100, 95, 85),
                   commands=[lambda record=record: self.open_player_record(record)
                             for record in records])
        paging = tk.Frame(box, bg=CARD)
        paging.pack(fill="x", padx=14, pady=(0, 14))
        self.label(paging, f"第 {self.record_offset // 50 + 1} 页", color=MUTED,
                   role="small").pack(side="left", padx=(0, 12))
        if self.record_offset:
            self.button(paging, "上一页", lambda: self.load_player_records(self.record_offset - 50)).pack(side="left", padx=(0, 10))
        if self.record_offset + len(records) < total:
            self.button(paging, "下一页", lambda: self.load_player_records(self.record_offset + 50)).pack(side="left")

    def toggle_subscription(self):
        if not self.selected_player:
            return
        action = "unsubscribe" if self.is_subscribed(self.selected_player["member_key"]) else "subscribe"
        self.request("alliance/subscriptions/update",
                     {"action": action, "member_key": self.selected_player["member_key"]},
                     self.subscription_received, channel="subscription")

    def subscription_received(self, response):
        if response.get("ok"):
            self.subscriptions = list(response.get("subscriptions") or [])
            self.search_message = ""
        else:
            self.search_message = str(response.get("message") or "订阅操作失败，请重试。")
        if self.screen == "player":
            self.render_player()
        elif self.screen == "search":
            self.render_admin()

    def open_player_record(self, record):
        self.search_message = "正在读取单场详情…"
        self._show_player_record_page(record, loading=True)
        self.request("alliance/players/record", {
            "member_key": self.selected_player["member_key"],
            "match_id": record["match_id"],
        }, self.player_record_received, channel="player_record")

    def _show_player_record_page(self, record, *, loading=False):
        if self.child_history is None:
            self.grid_remove()
            self.child_history = PvpHistoryPage(
                self.master, self.host, self.dropdown, self.scrollbar_class,
                page_key="alliance", fixed_records=[record],
                heading=self.selected_player["character_name"] + " · 单场详情",
                back=self.return_from_player_record, split_pane=self.split_pane_class,
            )
            self.child_history.grid(row=0, column=0, sticky="nsew")
        else:
            self.child_history.fixed_records = [project_duel_result_counters(record)]
            self.child_history.records = list(self.child_history.fixed_records)
        self.child_history.detail_message = (
            "正在读取服务器详情…" if loading else "请选择有交手数据的战斗记录"
        )
        self.child_history.open_record(self.child_history.records[0])

    def player_record_received(self, response):
        if self.screen != "player":
            return
        record = response.get("record") if response.get("ok") else None
        if not isinstance(record, dict):
            self.search_message = str(response.get("message") or "单场详情读取失败，请重试。")
            self.clear_child()
            self.grid(row=0, column=0, sticky="nsew")
            self.render_player()
            return
        self.search_message = ""
        self._show_player_record_page(record, loading=False)

    def return_from_player_record(self):
        self.clear_child()
        self.grid(row=0, column=0, sticky="nsew")
        self.render_player()

    def open_report(self):
        self.report = None
        self.load_report()

    def report_selection_var(self, member_key):
        variable = self.report_selected.get(member_key)
        if variable is None:
            variable = tk.BooleanVar(self, True)
            self.report_selected[member_key] = variable
        return variable

    def load_report(self):
        selected = [row["member_key"] for row in self.subscriptions
                    if self.report_selection_var(row["member_key"]).get()]
        if not selected:
            self.report = None
            self.search_message = "请先订阅并选择至少一名角色。"
            self.render_report()
            return
        period = self.report_period.get()
        custom = period == "自定义日期"
        start_date = self.report_start_date.get().strip() if custom else ""
        end_date = self.report_end_date.get().strip() if custom else ""
        if custom:
            try:
                first = datetime.strptime(start_date, "%Y-%m-%d")
                last = datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                first = last = None
            if (first is None or last is None or first > last
                    or first.strftime("%Y-%m-%d") != start_date
                    or last.strftime("%Y-%m-%d") != end_date):
                self.report = None
                self.search_message = "请填写有效的起止日期（YYYY-MM-DD）。"
                self.render_report()
                return
        self.report = None
        self.report_request_members = tuple(selected)
        self.search_message = "正在生成订阅战报…"
        self.render_report()
        self.request("alliance/subscriptions/report", {
            "days": 0 if custom else self.selected_days(period),
            "start_date": start_date, "end_date": end_date,
            "member_keys": selected,
        }, self.report_received, channel="subscription_report")

    def report_received(self, response):
        if (self.screen != "report" or self.report_request_members != tuple(
                row["member_key"] for row in self.subscriptions
                if self.report_selected.get(row["member_key"])
                and self.report_selected[row["member_key"]].get())):
            return
        if response.get("ok"):
            self.report = response.get("report")
            self.search_message = ""
        else:
            self.search_message = str(response.get("message") or "战报生成失败，请重试。")
        self.render_report()

    def report_panel(self, parent, title, subtitle="", *, accent=CYAN, pack=True):
        content, shell = self._hub_panel(parent, title, subtitle, accent=accent)
        if pack:
            shell.pack(fill="x", padx=20, pady=(0, 12))
        return content, shell

    def report_button(self, parent, caption, command, *, selected=False,
                      accent=CYAN):
        button = tk.Label(
            parent, text=caption, cursor="hand2", bd=0,
            bg=accent if selected else REPORT_DEEP,
            fg=REPORT_DEEP if selected else REPORT_SOFT,
            font=self.host._ui_font("small"), padx=15, pady=8,
            highlightthickness=1,
            highlightbackground=accent if selected else REPORT_BORDER,
        )
        button.bind("<Button-1>", lambda _event: command())
        button.bind("<Enter>", lambda _event: button.configure(
            bg=accent if selected else blend_color(REPORT_DEEP, accent, 0.18),
            fg=REPORT_DEEP if selected else TEXT))
        button.bind("<Leave>", lambda _event: button.configure(
            bg=accent if selected else REPORT_DEEP,
            fg=REPORT_DEEP if selected else REPORT_SOFT))
        return button

    def report_kpi(self, parent, column, caption, value, detail, accent):
        shell = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                         highlightbackground=EDGE)
        shell.grid(row=0, column=column, sticky="nsew",
                   padx=(0, 10 if column < 3 else 0))
        tk.Frame(shell, bg=accent, height=2).pack(fill="x")
        box = tk.Frame(shell, bg=SURFACE)
        box.pack(fill="both", expand=True)
        content = tk.Frame(box, bg=SURFACE)
        content.pack(side="left", fill="both", expand=True,
                    padx=16, pady=(10, 10))
        self.label(content, caption, bg=SURFACE, color=MUTED,
                   role="small", anchor="w").pack(fill="x")
        self.label(content, value, bg=SURFACE,
                   color=accent if caption in {"造成伤害", "阵亡"} else TEXT,
                   role="title", anchor="w").pack(fill="x", pady=(6, 4))
        self.label(content, detail, bg=SURFACE, color=accent,
                   role="micro", anchor="w").pack(fill="x")

    def draw_report_hero(self, canvas, width, height, *, home=False):
        canvas.delete("all")
        width, height = max(1, int(width)), max(1, int(height))
        if not hasattr(self, "_report_hero_source"):
            try:
                self._report_hero_source = Image.open(HUNTER_REPORT_BANNER).convert("RGBA")
            except OSError:
                self._report_hero_source = None
            self._report_hero_cache = {}
        source = self._report_hero_source
        if source is not None:
            key = (width, height)
            photo = self._report_hero_cache.get(key)
            if photo is None:
                scale = max(width / source.width, height / source.height)
                resized = source.resize(
                    (max(width, round(source.width * scale)),
                     max(height, round(source.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                x = max(0, (resized.width - width) // 2)
                y = max(0, round((resized.height - height) * 0.12))
                frame = resized.crop((x, y, x + width, y + height))
                overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                draw = ImageDraw.Draw(overlay)
                for column in range(width):
                    ratio = column / max(1, width - 1)
                    alpha = round(224 * (1 - ratio) ** 1.6 + 15)
                    draw.line((column, 0, column, height),
                              fill=(4, 10, 15, min(240, alpha)))
                for row in range(height):
                    alpha = round(80 * row / max(1, height - 1))
                    draw.line((0, row, width, row), fill=(4, 9, 13, alpha))
                photo = ImageTk.PhotoImage(Image.alpha_composite(frame, overlay),
                                           master=canvas)
                self._report_hero_cache = {key: photo}
            canvas._report_photo = photo
            canvas.create_image(0, 0, image=photo, anchor="nw")
        else:
            canvas.create_rectangle(0, 0, width, height, fill=REPORT_DEEP,
                                    outline="")
        canvas.create_text(39, 39, text=(
            "ALLIANCE INTELLIGENCE  /  HUNTER CITY" if home
            else "TACTICAL REPORT  /  HUNTER CITY"),
                            anchor="w", fill=CYAN,
                            font=self.host._ui_font("small"))
        canvas.create_text(36, 92, text="战盟中枢" if home else "猎龙之城",
                           anchor="w", fill=TEXT,
                           font=self.host._ui_font("hero_title"))
        canvas.create_text(39, 133, text="猎 龙 战 场 情 报" if home else "战 场 洞 察", anchor="w",
                           fill="#d7e5e8", font=self.host._ui_font("strong"))
        canvas.create_text(39, 173,
                           text=("检索已上传角色，订阅追踪，并按时间汇总多人交手。"
                                 if home else "从每一次交手中，看见真实的伤害、击杀与阵亡。"),
                           anchor="w", fill=REPORT_SOFT,
                           font=self.host._ui_font("small"))
        canvas.create_line(38, height - 3, 282, height - 3, fill=CYAN,
                           width=2)
        if home:
            return
        canvas.create_rectangle(width - 136, 25, width - 22, 59,
                                fill="#14252d", outline=REPORT_BORDER,
                                tags=("report-back",))
        canvas.create_text(width - 79, 42, text="← 返回搜索", fill=TEXT,
                           font=self.host._ui_font("small"),
                           tags=("report-back",))
        canvas.tag_bind("report-back", "<Button-1>",
                        lambda _event: self.render_admin())
        canvas.tag_bind("report-back", "<Enter>",
                        lambda _event: canvas.configure(cursor="hand2"))
        canvas.tag_bind("report-back", "<Leave>",
                        lambda _event: canvas.configure(cursor=""))

    def render_report(self):
        self.screen = "report"
        self.clear()
        header = tk.Frame(self.content, bg=SURFACE, highlightthickness=1,
                          highlightbackground=EDGE)
        header.pack(fill="x", padx=20, pady=(18, 14))
        tk.Frame(header, bg=TEAL, width=4).pack(side="left", fill="y")
        heading = tk.Frame(header, bg=SURFACE)
        heading.pack(side="left", fill="both", expand=True, padx=22, pady=18)
        self.label(heading, "PVP  /  战盟数据  /  订阅战报", role="micro",
                   bg=SURFACE, color=TEAL, anchor="w").pack(fill="x")
        self.label(heading, "猎龙之城战报", role="settings_title", bg=SURFACE,
                   color=TEXT, anchor="w").pack(fill="x", pady=(4, 2))
        self.label(heading, "已订阅角色的真实交手记录与统计",
                   role="small", bg=SURFACE, color=MUTED,
                   anchor="w").pack(fill="x")
        self._hub_button(header, "← 返回中枢", self.render_admin).pack(
            side="right", padx=20)

        filters, _shell = self.report_panel(
            self.content, "统计范围", "选择时间段后重新生成报告；自定义结束日期包含当天。")
        row = tk.Frame(filters, bg=REPORT_SURFACE)
        row.pack(fill="x", padx=18, pady=(0, 12))
        self.label(row, "时间", color=REPORT_SOFT, role="small",
                   bg=REPORT_SURFACE).pack(side="left", padx=(0, 8))
        self.dropdown(row, self.report_period, ("全部时间", "近1天", "近7天", "近30天", "近90天", "自定义日期"),
                      width=135, font=self.host._ui_font("body"), background=REPORT_SURFACE).pack(side="left", padx=(0, 16))
        self.label(row, "从", color=REPORT_SOFT, role="small",
                   bg=REPORT_SURFACE).pack(side="left", padx=(0, 6))
        tk.Entry(row, textvariable=self.report_start_date, bg=REPORT_DEEP, fg=TEXT,
                 insertbackground=CYAN, width=11, relief="flat",
                 font=self.host._ui_font("small")).pack(side="left", ipady=8, padx=(0, 14))
        self.label(row, "至", color=REPORT_SOFT, role="small",
                   bg=REPORT_SURFACE).pack(side="left", padx=(0, 6))
        tk.Entry(row, textvariable=self.report_end_date, bg=REPORT_DEEP, fg=TEXT,
                 insertbackground=CYAN, width=11, relief="flat",
                 font=self.host._ui_font("small")).pack(side="left", ipady=8, padx=(0, 16))
        self.report_button(row, "生成报告  →", self.load_report,
                           selected=True).pack(side="left")
        if self.report:
            self.report_button(row, "复制报告", self.copy_report).pack(side="right")
        if self.search_message:
            self.label(filters, self.search_message, color=GOLD, role="small",
                       bg=REPORT_SURFACE, anchor="w").pack(fill="x", padx=18, pady=(0, 10))
        selected_box = tk.Frame(self.content, bg=BG)
        selected_box.pack(fill="x", padx=34, pady=(3, 13))
        for row in self.subscriptions:
            self.report_selection_var(row["member_key"])
        selected_controls = tk.Frame(selected_box, bg=BG)
        selected_controls.pack(fill="x", pady=(0, 7))
        count = sum(bool(self.report_selected.get(row["member_key"])
                         and self.report_selected[row["member_key"]].get())
                    for row in self.subscriptions)
        self.label(selected_controls,
                   f"参与统计  /  已选 {count}/{len(self.subscriptions)} 位角色",
                   color=CYAN, role="small", bg=BG,
                   anchor="w").pack(side="left")
        self.report_button(selected_controls, "清空", lambda: self.set_report_selection(False)).pack(side="right")
        self.report_button(selected_controls, "全选", lambda: self.set_report_selection(True)).pack(side="right", padx=(0, 8))
        choices = tk.Frame(selected_box, bg=BG)
        choices.pack(fill="x")
        for column in range(4):
            choices.grid_columnconfigure(column, weight=1, uniform="report-people")
        for index, player in enumerate(self.subscriptions):
            key = player["member_key"]
            variable = self.report_selection_var(key)
            selected = variable.get()
            choice = tk.Label(
                choices,
                text=f"{'●' if selected else '○'}  {player['character_name']}  {'✓' if selected else ''}",
                bg="#1d343c" if selected else REPORT_DEEP,
                fg=TEXT if selected else REPORT_SOFT,
                highlightthickness=1,
                highlightbackground=REPORT_BORDER,
                font=self.host._ui_font("small"),
                padx=12, pady=7, cursor="hand2", anchor="w",
            )
            choice.bind("<Button-1>", lambda _event, item=variable: (
                item.set(not item.get()), self.report_selection_changed()))
            choice.grid(row=index // 4, column=index % 4, sticky="ew",
                        padx=(0, 9), pady=3)
        if not self.report:
            return
        report = self.report
        summary = report.get("summary", {})
        range_start = report.get("range_start")
        range_end = report.get("range_end")
        if range_start is not None and range_end is not None:
            range_text = (f"实际统计：{datetime.fromtimestamp(range_start):%Y-%m-%d %H:%M} "
                          f"至 {datetime.fromtimestamp(range_end):%Y-%m-%d %H:%M}"
                          + ("（结束日期含当天）" if report.get("start_date") else ""))
            ribbon = tk.Frame(self.content, bg=BG)
            ribbon.pack(fill="x", padx=34, pady=(5, 15))
            self.label(ribbon, range_text, color=CYAN, role="micro",
                       bg=BG, anchor="e").pack(side="right", padx=(12, 0))
            tk.Frame(ribbon, bg=REPORT_BORDER, height=1).pack(
                side="left", fill="x", expand=True, pady=8)
        kpis = tk.Frame(self.content, bg=BG)
        kpis.pack(fill="x", padx=20, pady=(0, 12))
        for column in range(4):
            kpis.grid_columnconfigure(column, weight=1, uniform="report-kpis")
        self.report_kpi(kpis, 0, "订阅角色", number(report.get("subscribed_count")),
                        "本次参与统计", CYAN)
        self.report_kpi(kpis, 1, "参战人次", number(summary.get("matches")),
                        "选定时间范围", "#9b9cff")
        self.report_kpi(kpis, 2, "造成伤害", compact_number(summary.get("damage")),
                        number(summary.get("damage")), TEAL)
        self.report_kpi(kpis, 3, "击杀 / 阵亡",
                        f"{number(summary.get('kills'))} / {number(summary.get('deaths'))}",
                        "无胜负判定", GOLD)
        metric = self.report_metric.get()
        analysis = tk.Frame(self.content, bg=BG)
        analysis.pack(fill="x", padx=34, pady=(22, 12))
        titles = tk.Frame(analysis, bg=BG)
        titles.pack(side="left", fill="x", expand=True)
        self.label(titles, "02  /  PERFORMANCE OVERVIEW", color=CYAN,
                   role="micro", bg=BG, anchor="w").pack(fill="x")
        self.label(titles, "战斗表现", color=TEXT, role="title",
                   bg=BG, anchor="w").pack(fill="x", pady=(2, 0))
        switches = tk.Frame(analysis, bg=BG)
        switches.pack(side="right", pady=4)
        for caption, _field, color in REPORT_METRICS:
            self.report_button(
                switches, caption,
                lambda selected=caption: self.set_report_metric(selected),
                selected=caption == metric, accent=color,
            ).pack(side="left", padx=(0, 3))

        dashboard = tk.Frame(self.content, bg=BG)
        dashboard.pack(fill="x", padx=20, pady=(0, 14))
        dashboard.grid_columnconfigure(0, weight=7, uniform="report-dashboard")
        dashboard.grid_columnconfigure(1, weight=4, uniform="report-dashboard")
        trend_box, trend_shell = self.report_panel(
            dashboard, f"时间走势  /  {metric}",
            "将统计时间均分为 12 段，悬停节点查看精确值。", pack=False)
        trend_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        trend_canvas = tk.Canvas(trend_box, bg=REPORT_SURFACE,
                                 height=255, highlightthickness=0)
        trend_canvas.pack(fill="x", padx=17, pady=(2, 17))
        trend_canvas.bind("<Configure>", lambda event: self.draw_report_trend(
            trend_canvas, report.get("trend", ()),
            float(report.get("range_end") or 0) - float(report.get("range_start") or 0),
            event.width, metric))
        coverage, coverage_shell = self.report_panel(
            dashboard, "击杀与阵亡", "猎龙之城只记录战果，不判定胜负。",
            accent=TEAL, pack=False)
        coverage_shell.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        result_canvas = tk.Canvas(coverage, bg=REPORT_SURFACE,
                                  height=255, highlightthickness=0)
        result_canvas.pack(fill="x", padx=16, pady=(2, 17))
        result_canvas.bind("<Configure>", lambda event: self.draw_report_results(
            result_canvas, summary, event.width))
        comparison = tk.Frame(self.content, bg=BG)
        comparison.pack(fill="x", padx=20, pady=(0, 14))
        comparison.grid_columnconfigure(0, weight=6, uniform="report-comparison")
        comparison.grid_columnconfigure(1, weight=5, uniform="report-comparison")
        box, ranking_shell = self.report_panel(
            comparison, f"订阅角色 · {metric}对比",
            "贡献排行 · 最多显示前 10 位", accent=TEAL, pack=False)
        ranking_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        metric_field = next(field for label, field, _color in REPORT_METRICS if label == metric)
        ranked_members = sorted(report.get("members", ()),
                                key=lambda item: -(item.get(metric_field) or 0))[:10]
        member_canvas = tk.Canvas(box, bg=REPORT_SURFACE,
                                  height=max(250, len(ranked_members) * 54 + 30),
                                  highlightthickness=0)
        member_canvas.pack(fill="x", padx=17, pady=(2, 15))
        member_canvas.bind("<Configure>", lambda event: self.draw_report_members(
            member_canvas, ranked_members, event.width, metric))
        pie_box, pie_shell = self.report_panel(
            comparison, f"{metric}贡献占比", "选中角色之间的比例", accent=CYAN,
            pack=False)
        pie_shell.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        pie_canvas = tk.Canvas(pie_box, bg=REPORT_SURFACE,
                               height=max(250, len(ranked_members) * 54 + 30),
                               highlightthickness=0)
        pie_canvas.pack(fill="x", padx=17, pady=(2, 15))
        pie_canvas.bind("<Configure>", lambda event: self.draw_report_pie(
            pie_canvas, report.get("members", ()), event.width, metric))
        table_box, _shell = self.report_panel(
            self.content, "角色数据明细",
            "承伤只累计已收到精确记录的场次。", accent=GOLD)
        self.table(table_box, ("角色", "参战人次", "击杀", "阵亡", "伤害", "承伤覆盖"),
                   [(f"{row['character_name']} · {row['member_key'][:8]}", number(row["matches"]),
                     number(row["kills"]), number(row["deaths"]), number(row["damage"]),
                     f"{number(row['taken'])} ({number(row['taken_matches'])}/{number(row['matches'])} 场)")
                    for row in report.get("members", ())],
                   widths=(170, 85, 65, 65, 100, 170))
        opponent_title = {"伤害": "我对对手造成的伤害",
                          "承伤": "对手对我造成的已记录伤害",
                          "击杀": "我击败的对手",
                          "阵亡": "击败我的对手"}[metric]
        opponent_box, _shell = self.report_panel(
            self.content, f"对手交手 · {opponent_title}",
            "汇总选中角色与对手的交手；未确认身份的对手按单场分开。",
            accent=GOLD)
        ranked_opponents = sorted(report.get("opponents", ()),
                                  key=lambda item: -(item.get(metric_field) or 0))[:12]
        opponent_canvas = tk.Canvas(opponent_box, bg=REPORT_SURFACE,
                                    height=max(110, len(ranked_opponents) * 52 + 26),
                                    highlightthickness=0)
        opponent_canvas.pack(fill="x", padx=17, pady=(2, 16))
        opponent_canvas.bind("<Configure>", lambda event: self.draw_report_members(
            opponent_canvas, ranked_opponents, event.width, metric, opponent=True))

    @staticmethod
    def draw_report_bar(canvas, left, top, right, bottom, color, tag):
        if right <= left or bottom <= top:
            return
        canvas.create_rectangle(left - 2, top - 2, right + 2, bottom + 2,
                                fill=blend_color(color, REPORT_SURFACE, 0.76), outline="",
                                tags=(tag,))
        for step in range(12):
            y0 = top + (bottom - top) * step / 12
            y1 = top + (bottom - top) * (step + 1) / 12
            canvas.create_rectangle(left, y0, right, y1,
                                    fill=blend_color(color, REPORT_SURFACE, 0.2 + step * 0.035),
                                    outline="", tags=(tag,))
        canvas.create_line(left, top, right, top,
                           fill=blend_color(color, TEXT, 0.35), tags=(tag,))

    def set_report_metric(self, metric):
        if metric == self.report_metric.get():
            return
        position = self.canvas.yview()[0]
        self.report_metric.set(metric)
        self.render_report()
        self.after_idle(lambda: self.canvas.yview_moveto(position))

    def draw_report_trend(self, canvas, trend, span_seconds, width, metric="伤害"):
        canvas.delete("all")
        width = max(480, int(width))
        field, color = next(((key, accent) for label, key, accent in REPORT_METRICS
                             if label == metric), ("damage", CYAN))
        rows = [row for row in trend if isinstance(row, dict)]
        values = [None if field == "taken" and row.get("matches")
                  and not row.get("taken_matches")
                  else max(0, int(row.get(field) or 0)) for row in rows]
        if not rows or not any(value is not None and value > 0 for value in values):
            empty = "承伤尚无精确记录" if field == "taken" and any(
                row.get("matches") for row in rows) else "所选范围暂无此项记录"
            canvas.create_text(width / 2, 110, text=empty,
                               fill=MUTED, font=self.host._ui_font("small"))
            return
        left, right, top, bottom = 70, width - 22, 45, 185
        highest = max(value or 0 for value in values)
        formatter = compact_number if field in {"damage", "taken"} else number
        info = canvas.create_text(left, 16, text=f"{metric}峰值 {formatter(highest)} · 悬停查看精确值",
                                  anchor="w", fill=color,
                                  font=self.host._ui_font("small"))
        for fraction in (0, 0.5, 1):
            y = bottom - fraction * (bottom - top)
            canvas.create_line(left, y, right, y, fill=EDGE)
            canvas.create_text(left - 8, y, text=formatter(round(highest * fraction)),
                               anchor="e", fill=MUTED,
                               font=self.host._ui_font("micro"))
        step = (right - left) / len(values)
        points = []
        for index, value in enumerate(values):
            center = left + (index + 0.5) * step
            if value is None:
                canvas.create_text(center, bottom - 9, text="--",
                                   fill=MUTED, font=self.host._ui_font("micro"))
                points.append(None)
            else:
                y = bottom - value / highest * (bottom - top)
                points.append((center, y, value, index))
            if index in {0, len(values) // 3, 2 * len(values) // 3, len(values) - 1}:
                started = float(rows[index].get("started_at") or 0)
                pattern = ("%H:%M" if span_seconds <= 86400
                           else "%y-%m" if span_seconds > 366 * 86400
                           else "%m-%d")
                canvas.create_text(center, bottom + 24,
                                   text=datetime.fromtimestamp(started).strftime(pattern),
                                   fill=MUTED, font=self.host._ui_font("micro"))
        run = []
        for point in (*points, None):
            if point is not None:
                run.append(point)
                continue
            if len(run) > 1:
                polygon = [run[0][0], bottom]
                for item in run:
                    polygon.extend((item[0], item[1]))
                polygon.extend((run[-1][0], bottom))
                canvas.create_polygon(*polygon,
                                      fill=blend_color(color, REPORT_SURFACE, 0.83),
                                      outline="")
            run = []
        for fraction in (0, 0.5, 1):
            y = bottom - fraction * (bottom - top)
            canvas.create_line(left, y, right, y, fill=REPORT_BORDER)
        for earlier, later in zip(points, points[1:]):
            if earlier is None or later is None:
                continue
            canvas.create_line(earlier[0], earlier[1], later[0], later[1],
                               fill=blend_color(color, REPORT_SURFACE, 0.7), width=8,
                               smooth=True)
            canvas.create_line(earlier[0], earlier[1], later[0], later[1],
                               fill=color, width=3, smooth=True)
        for point in points:
            if point is None:
                continue
            center, y, value, index = point
            tag = f"trend:{index}"
            canvas.create_oval(center - 7, y - 7, center + 7, y + 7,
                               fill=blend_color(color, REPORT_SURFACE, 0.55),
                               outline=color, width=2, tags=(tag,))
            canvas.create_oval(center - 3, y - 3, center + 3, y + 3,
                               fill=TEXT, outline="", tags=(tag,))
            canvas.create_text(center, max(35, y - 15), text=formatter(value),
                               fill=TEXT, font=self.host._ui_font("micro"))
            exact = f"{datetime.fromtimestamp(rows[index]['started_at']):%m-%d %H:%M} · {metric} {number(value)} · {number(rows[index].get('matches'))} 人次"
            canvas.tag_bind(tag, "<Enter>",
                            lambda _event, detail=exact: canvas.itemconfigure(info, text=detail))
            canvas.tag_bind(tag, "<Leave>",
                            lambda _event: canvas.itemconfigure(
                                info, text=f"{metric}峰值 {formatter(highest)} · 悬停查看精确值"))

    def set_report_selection(self, selected):
        for row in self.subscriptions:
            self.report_selected[row["member_key"]].set(bool(selected))
        self.report_selection_changed()

    def report_selection_changed(self):
        self.report = None
        self.search_message = "统计角色已更改，请点击“生成报告”。"
        self.render_report()

    def draw_report_results(self, canvas, summary, width):
        canvas.delete("all")
        width = max(480, int(width))
        kills = max(0, int(summary.get("kills") or 0))
        deaths = max(0, int(summary.get("deaths") or 0))
        total = kills + deaths
        if not total:
            canvas.create_text(width / 2, 120, text="所选范围暂无击杀或阵亡记录",
                               fill=MUTED, font=self.host._ui_font("small"))
            return
        cx, cy, radius = 115, 125, 78
        canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                           outline=REPORT_BORDER, width=19)
        if kills:
            canvas.create_arc(cx - radius, cy - radius,
                              cx + radius, cy + radius, start=90,
                              extent=-359.9 * kills / total,
                              style=tk.ARC, outline=CYAN, width=19)
        if deaths:
            canvas.create_arc(cx - radius, cy - radius,
                              cx + radius, cy + radius,
                              start=90 - 359.9 * kills / total,
                              extent=-359.9 * deaths / total,
                              style=tk.ARC, outline=RED, width=19)
        canvas.create_text(cx, cy - 12, text="K / D", fill=REPORT_SOFT,
                           font=self.host._ui_font("small"))
        canvas.create_text(cx, cy + 16,
                           text=f"{number(kills)} / {number(deaths)}", fill=TEXT,
                           font=self.host._ui_font("strong"))
        label_x = 235
        canvas.create_oval(label_x, 72, label_x + 10, 82,
                           fill=CYAN, outline="")
        canvas.create_text(label_x + 18, 77, text=f"击杀  {number(kills)}",
                           anchor="w", fill=TEXT, font=self.host._ui_font("strong"))
        canvas.create_text(label_x + 18, 104, text=f"{kills / total:.1%}  占比",
                           anchor="w", fill=REPORT_SOFT,
                           font=self.host._ui_font("micro"))
        canvas.create_oval(label_x, 141, label_x + 10, 151,
                           fill=RED, outline="")
        canvas.create_text(label_x + 18, 146, text=f"阵亡  {number(deaths)}",
                           anchor="w", fill=TEXT, font=self.host._ui_font("strong"))
        canvas.create_text(label_x + 18, 173, text=f"{deaths / total:.1%}  占比",
                           anchor="w", fill=REPORT_SOFT,
                           font=self.host._ui_font("micro"))
        canvas.create_line(28, 226, width - 28, 226, fill=REPORT_BORDER)
        canvas.create_text(width / 2, 240,
                           text="数量比例用于比较；不代表胜负。",
                           fill=REPORT_SOFT, font=self.host._ui_font("micro"))

    def draw_report_pie(self, canvas, members, width, metric="伤害"):
        canvas.delete("all")
        width = max(480, int(width))
        field, _accent = next(((key, accent) for label, key, accent in REPORT_METRICS
                               if label == metric), ("damage", CYAN))
        values = sorted(((row, max(0, int(row.get(field) or 0)))
                         for row in members if isinstance(row, dict)),
                        key=lambda item: -item[1])
        total = sum(value for _row, value in values)
        if not total:
            canvas.create_text(width / 2, 130, text=f"本次没有可计算的{metric}贡献",
                               fill=MUTED, font=self.host._ui_font("small"))
            return
        colors = {
            "伤害": (CYAN, "#527f92", "#b45b6d", GOLD, REPORT_SOFT),
            "承伤": ("#8db9ca", "#557485", "#b88596", CYAN, RED),
            "击杀": (GOLD, CYAN, "#557485", TEAL, RED),
            "阵亡": (RED, "#d98aa5", GOLD, "#9b9fff", CYAN),
        }[metric]
        center_x, center_y, radius = 122, 126, 78
        start = 90.0
        for index, (row, value) in enumerate(values):
            if not value:
                continue
            extent = -360.0 * value / total
            color = colors[index % len(colors)]
            canvas.create_arc(center_x - radius, center_y - radius,
                              center_x + radius, center_y + radius,
                              start=start, extent=extent, fill=color,
                              outline=REPORT_SURFACE, width=2)
            start += extent
        canvas.create_oval(center_x - 40, center_y - 40,
                           center_x + 40, center_y + 40,
                           fill=REPORT_SURFACE, outline=REPORT_SURFACE)
        canvas.create_text(center_x, center_y - 9, text=metric,
                           fill=MUTED, font=self.host._ui_font("small"))
        canvas.create_text(center_x, center_y + 15,
                           text=compact_number(total) if field in {"damage", "taken"}
                           else number(total), fill=TEXT,
                           font=self.host._ui_font("strong"))
        legend_rows = sorted(enumerate(values), key=lambda item: -item[1][1])
        for rank, (index, (row, value)) in enumerate(legend_rows):
            if rank >= 5:
                break
            x, y = 238, 36 + rank * 39
            canvas.create_oval(x, y - 5, x + 12, y + 7,
                               fill=colors[index % len(colors)], outline="")
            name = str(row.get("character_name") or "未知角色")
            canvas.create_text(x + 21, y, text=f"{name}  {value / total:.1%}",
                               anchor="w", fill=TEXT,
                               font=self.host._ui_font("small"))
            canvas.create_text(x + 21, y + 17,
                               text=compact_number(value) if field in {"damage", "taken"}
                               else number(value), anchor="w", fill=REPORT_SOFT,
                               font=self.host._ui_font("micro"))
        if len(legend_rows) > 5:
            canvas.create_text(width - 18, 237,
                               text=f"另有 {len(legend_rows) - 5} 人计入圆环",
                               anchor="e", fill=MUTED,
                               font=self.host._ui_font("micro"))

    def draw_report_members(self, canvas, members, width, metric="伤害", *, opponent=False):
        canvas.delete("all")
        width = max(480, int(width))
        if not members:
            canvas.create_text(width / 2, 55,
                               text="暂无对手交手记录" if opponent else "本次未选中订阅角色",
                               fill=MUTED, font=self.host._ui_font("small"))
            return
        field, color = next(((key, accent) for label, key, accent in REPORT_METRICS
                             if label == metric), ("damage", CYAN))
        highest = max(1, *(int(row.get(field) or 0) for row in members))
        formatter = compact_number if field in {"damage", "taken"} else number
        info = canvas.create_text(14, 13, text=f"{metric}排行 · 悬停查看精确值",
                                  anchor="w", fill=color,
                                  font=self.host._ui_font("small"))
        bar_left = 195 if width < 680 else 265
        bar_width = max(80, width - bar_left - 105)
        for index, row in enumerate(members):
            y = 34 + index * 52
            key = str((row.get("opponent_key") if opponent else row.get("member_key")) or "")
            name = str((row.get("name") if opponent else row.get("character_name")) or "未知角色")
            suffix = "单场" if opponent and key.startswith("pvp_") else key[:8]
            label = f"{name} · {suffix}"
            interactions = int(row.get("interactions" if opponent else "matches") or 0)
            known = int(row.get("taken_matches") or 0)
            value = None if field == "taken" and interactions and not known else max(
                0, int(row.get(field) or 0))
            canvas.create_text(14, y + 8, text=label, anchor="w",
                               fill=TEXT, font=self.host._ui_font("small"))
            detail = (f"交手 {number(interactions)} 次" if opponent
                      else f"参战 {number(interactions)} 人次")
            if field == "taken":
                detail += f" · 承伤记录 {number(known)}/{number(interactions)}"
            canvas.create_text(14, y + 28, text=detail, anchor="w",
                               fill=MUTED, font=self.host._ui_font("micro"))
            canvas.create_rectangle(bar_left, y + 8, bar_left + bar_width, y + 28,
                                    fill=PANEL_2, outline="")
            if value:
                tag = f"compare:{index}"
                self.draw_report_bar(canvas, bar_left, y + 8,
                                     bar_left + bar_width * value / highest,
                                     y + 28, color, tag)
                exact = f"{name} · {metric} {number(value)} · {detail}"
                canvas.tag_bind(tag, "<Enter>",
                                lambda _event, text=exact: canvas.itemconfigure(info, text=text))
                canvas.tag_bind(tag, "<Leave>",
                                lambda _event: canvas.itemconfigure(
                                    info, text=f"{metric}排行 · 悬停查看精确值"))
            canvas.create_text(width - 12, y + 18,
                               text=formatter(value) if value is not None else "未记录",
                               anchor="e", fill=TEXT,
                               font=self.host._ui_font("small"))

    def report_text(self):
        report = self.report or {}
        summary = report.get("summary", {})
        period = self.report_period.get()
        range_label = (f"{report['start_date']} 至 {report['end_date']}"
                       if report.get("start_date") and report.get("end_date")
                       else period)
        lines = [f"猎龙之城订阅战斗报告 · {range_label}",
                 f"生成时间 {stamp((report.get('generated_at') or 0) * 1e9)}",
                 f"订阅角色 {number(report.get('subscribed_count'))} · 参战人次 {number(summary.get('matches'))}",
                 f"击杀 {number(summary.get('kills'))} · 阵亡 {number(summary.get('deaths'))} · 伤害 {number(summary.get('damage'))}",
                 f"已记录承伤 {number(summary.get('taken'))}（{number(summary.get('taken_matches'))}/{number(summary.get('matches'))} 场）"]
        for row in report.get("members", ()):
            lines.append(f"{row['character_name']} · {row['member_key'][:8]}：{number(row['matches'])} 场，击杀 {number(row['kills'])}，阵亡 {number(row['deaths'])}，伤害 {number(row['damage'])}")
        opponents = sorted(report.get("opponents", ()),
                           key=lambda item: -(item.get("damage") or 0))[:5]
        if opponents:
            lines.append("主要交手对手：")
            for row in opponents:
                lines.append(f"{row.get('name') or '未知玩家'}：交手 {number(row.get('interactions'))} 次，"
                             f"造成伤害 {number(row.get('damage'))}，"
                             f"击杀 {number(row.get('kills'))}，阵亡 {number(row.get('deaths'))}")
        return "\n".join(lines)

    def copy_report(self):
        self.clipboard_clear()
        self.clipboard_append(self.report_text())
        self.search_message = "报告已复制。"
        self.render_report()

    def open_hunter(self):
        if not self.authorized:
            return
        self.analysis = None
        self.analysis_error = "正在读取战盟成员…"
        self.render_hunter()
        self.request("alliance/status", {}, self.hunter_status_received,
                     channel="hunter_status")

    def hunter_status_received(self, response):
        if not self.hunter_open:
            return
        if not response.get("ok") or response.get("authorized") is not True:
            self.status = None
            self.render_locked(error=str(response.get("message") or "管理员权限已失效。"))
            return
        self.status = response
        self.load_hunter()

    def load_hunter(self):
        if not self.authorized:
            return
        days = {"近7天": 7, "近30天": 30, "近90天": 90}[self.analysis_days.get()]
        member_key = self.hunter_member_labels.get(self.analysis_member.get(), "")
        self.analysis = None
        self.analysis_error = "正在查询猎龙之城战报…"
        self.render_hunter()
        self.request("alliance/hunter/analysis", {"days": days, "member_key": member_key},
                     self.hunter_received, channel="hunter_analysis")

    def hunter_received(self, response):
        if not self.hunter_open:
            return
        if response.get("ok"):
            self.analysis = response
            self.analysis_error = ""
        elif response.get("error") in {"PVP_ALLIANCE_FORBIDDEN", "invalid_session", "card_expired", "card_revoked"}:
            self.status = None
            self.render_locked(error=str(response.get("message") or "管理员权限已失效。"))
            return
        else:
            self.analysis_error = str(response.get("message") or "查询失败，请重试。")
        self.render_hunter()

    def render_hunter(self):
        if not self.authorized:
            self.render_locked()
            return
        self.hunter_open = True
        self.clear()
        heading = tk.Frame(self.content, bg=SURFACE, highlightthickness=1,
                           highlightbackground=EDGE)
        heading.pack(fill="x", padx=20, pady=(18, 14))
        tk.Frame(heading, bg=GOLD, width=4).pack(side="left", fill="y")
        heading_text = tk.Frame(heading, bg=SURFACE)
        heading_text.pack(side="left", fill="both", expand=True,
                          padx=22, pady=18)
        self.label(heading_text, "PVP  /  战盟数据  /  猎龙之城", role="micro",
                   bg=SURFACE, color=GOLD, anchor="w").pack(fill="x")
        self.label(heading_text, "猎龙之城战报", role="settings_title",
                   bg=SURFACE, color=TEXT, anchor="w").pack(fill="x", pady=(4, 2))
        self.label(heading_text,
                   "猎城战与终末猎杀 · 已上传的战盟成员个人记录",
                   role="small", bg=SURFACE, color=MUTED,
                   anchor="w").pack(fill="x")
        self._hub_button(heading, "← 返回中枢", self.render_admin).pack(
            side="right", padx=20)
        filters = self.card(self.content, "查询与分析")
        controls = tk.Frame(filters, bg=CARD)
        controls.pack(fill="x", padx=14, pady=(0, 14))
        self.dropdown(controls, self.analysis_days, ("近7天", "近30天", "近90天"),
                      width=130, font=self.host._ui_font("body"), background=CARD).pack(side="left", padx=(0, 10))
        members = self.status.get("members", [])
        self.hunter_member_labels = {
            f"{member['character_name']} · {member['member_key'][:8]}": member["member_key"]
            for member in members
        }
        choices = ("全部成员", *self.hunter_member_labels)
        if self.analysis_member.get() not in choices:
            self.analysis_member.set("全部成员")
        self.dropdown(controls, self.analysis_member, choices,
                      width=220, font=self.host._ui_font("body"), background=CARD).pack(side="left", padx=(0, 10))
        self.button(controls, "查询战报", self.load_hunter, primary=True).pack(side="left")
        if self.analysis_error:
            self.label(filters, self.analysis_error, color=MUTED, role="small", anchor="w").pack(fill="x", padx=14, pady=(0, 12))
        if not self.analysis:
            return
        data = self.analysis
        summary = data.get("summary", {})
        matches = int(summary.get("matches") or 0)
        self.summary(self.content, (
            ("战斗场次", number(matches), CYAN),
            ("参战成员", number(summary.get("members")), CYAN),
            ("击杀 / 阵亡", f"{number(summary.get('kills'))} / {number(summary.get('deaths'))}", GOLD),
            ("本人总伤害", number(summary.get("damage")), TEAL),
        ))
        coverage = self.card(self.content, "数据范围")
        self.label(coverage,
                   f"承伤已记录 {number(summary.get('taken_matches'))}/{number(matches)} 场，共 {number(summary.get('taken'))}；"
                   f"采集完整 {number(summary.get('complete_matches'))}/{number(matches)} 场。"
                   "技能表只汇总实际收到的双方命中事件。",
                   color=MUTED, role="small", anchor="w", justify="left").pack(fill="x", padx=14, pady=(0, 14))
        member_box = self.card(self.content, "成员表现 · 点击成员可查询其战报")
        member_rows = data.get("members", [])
        self.table(member_box, ("角色", "场次", "击杀", "阵亡", "本人伤害", "已记录承伤"),
                   [(row.get("character_name", "--"), number(row.get("matches")), number(row.get("kills")),
                     number(row.get("deaths")), number(row.get("damage")),
                     f"{number(row.get('taken'))} ({number(row.get('taken_matches'))}/{number(row.get('matches'))} 场)")
                    for row in member_rows],
                   widths=(140, 65, 65, 65, 105, 145),
                   commands=[lambda row=row: self.select_hunter_member(row) for row in member_rows])
        skills = tk.Frame(self.content, bg=BG)
        skills.pack(fill="x", padx=20, pady=(0, 14))
        for column, (caption, field) in enumerate((("我命中敌人的技能", "skills_outgoing"),
                                                   ("敌人命中我的技能", "skills_incoming"))):
            skills.grid_columnconfigure(column, weight=1, uniform="skills")
            box = self.card(skills, caption, pack=False)
            box.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if column == 0 else 0)
            catalog = getattr(self.host.model, "skill_names", {})
            rows = []
            for skill in data.get(field, ()):
                skill_id = int(skill.get("skill_id") or 0)
                name = skill.get("name") or catalog.get(skill_id) or f"技能 {skill_id}"
                rows.append((name, number(skill.get("hits")), number(skill.get("damage")), number(skill.get("max_hit"))))
            self.table(box, ("技能", "命中", "总伤害", "最高一击"), rows,
                       widths=(140, 65, 100, 90))
        records = data.get("records", [])
        box = self.card(self.content, "最近战报 · 最多显示 50 场")
        self.table(box, ("开始时间", "玩法", "角色", "击杀 / 阵亡", "本人伤害", "查看"),
                   [(stamp(row.get("started_at_ns")), row.get("mode_name", "--"),
                     row.get("player", {}).get("name", "--"),
                     f"{number(row.get('kills'))} / {number(row.get('deaths'))}",
                     number(row.get("damage")), "查看详情") for row in records],
                   widths=(145, 100, 100, 100, 105, 90),
                   commands=[lambda row=row: self.view_hunter_record(row) for row in records])

    def select_hunter_member(self, member):
        self.analysis_member.set(next(
            (label for label, key in self.hunter_member_labels.items() if key == member["member_key"]),
            "全部成员",
        ))
        self.load_hunter()

    def view_hunter_record(self, record):
        self.analysis_error = "正在读取单场战报…"
        self._show_hunter_record_page(record, loading=True)
        self.request("alliance/hunter/record", {"match_id": record["match_id"]},
                     self.hunter_record_received, channel="hunter_record")

    def _show_hunter_record_page(self, record, *, loading=False):
        if self.child_history is None:
            self.grid_remove()
            self.child_history = PvpHistoryPage(
                self.master, self.host, self.dropdown, self.scrollbar_class,
                page_key="alliance", fixed_records=[record],
                heading="猎龙之城 · 战斗详情", back=self.return_from_hunter,
                split_pane=self.split_pane_class,
            )
            self.child_history.grid(row=0, column=0, sticky="nsew")
        else:
            self.child_history.fixed_records = [project_duel_result_counters(record)]
            self.child_history.records = list(self.child_history.fixed_records)
        self.child_history.detail_message = (
            "正在读取服务器详情…" if loading else "请选择有交手数据的战斗记录"
        )
        self.child_history.open_record(self.child_history.records[0])

    def hunter_record_received(self, response):
        if not self.hunter_open:
            return
        if not response.get("ok"):
            if response.get("error") in {"PVP_ALLIANCE_FORBIDDEN", "invalid_session", "card_expired", "card_revoked"}:
                self.status = None
                self.render_locked(error=str(response.get("message") or "管理员权限已失效。"))
                return
            self.analysis_error = str(response.get("message") or "战报读取失败，请重试。")
            self.clear_child()
            self.grid(row=0, column=0, sticky="nsew")
            self.render_hunter()
            return
        record = response.get("record")
        if not isinstance(record, dict):
            self.analysis_error = "战报内容无效。"
            self.clear_child()
            self.grid(row=0, column=0, sticky="nsew")
            self.render_hunter()
            return
        self.analysis_error = ""
        self._show_hunter_record_page(record)

    def return_from_hunter(self):
        self.clear_child()
        self.grid(row=0, column=0, sticky="nsew")
        self.render_hunter()

    def open_form(self, mode, member=None):
        if not self.authorized:
            return
        self.form, self.form_member = mode, member
        self.message = ""
        self.render_admin()

    def render_form(self):
        mode, member = self.form, self.form_member or {}
        box = self.card(self.content, "批量导入" if mode == "batch" else "成员资料 / 调整职务" if mode == "edit" else "添加成员")
        error = self.label(box, "", color=RED, role="small", anchor="w")
        self.form_error = error
        if mode == "batch":
            self.label(box, "每行：角色名称,角色ID,职业ID,职务  （也支持制表符分隔）", role="small", color=MUTED, anchor="w").pack(fill="x", padx=14)
            editor = tk.Text(box, bg=BG, fg=TEXT, insertbackground=CYAN, height=6, relief="flat", font=self.host._ui_font("body"))
            editor.pack(fill="x", padx=14, pady=12)
            def payload():
                return {"members": parse_members(editor.get("1.0", "end"))}
            action = "alliance/members/batch"
        else:
            fields = tk.Frame(box, bg=CARD)
            fields.pack(fill="x", padx=14)
            variables = {}
            for col, (caption, key, default) in enumerate((("角色名称", "character_name", ""),
                    ("角色ID（绑定标识）", "character_id", ""), ("职业ID", "profession_id", "0"))):
                if mode == "edit" and key == "character_id":
                    continue
                group = tk.Frame(fields, bg=CARD)
                group.pack(side="left", padx=(0, 14))
                self.label(group, caption, color=MUTED, role="small").pack(anchor="w", pady=(0, 7))
                variables[key] = tk.StringVar(group, str(member.get(key, default)))
                tk.Entry(group, textvariable=variables[key], bg=BG, fg=TEXT, insertbackground=CYAN,
                         width=24 if key == "character_id" else 16, relief="flat", font=self.host._ui_font("body")).pack(ipady=7)
            role = tk.StringVar(fields, member.get("club_role", "普通成员"))
            group = tk.Frame(fields, bg=CARD)
            group.pack(side="left")
            self.label(group, "战盟职务", color=MUTED, role="small").pack(anchor="w", pady=(0, 7))
            self.dropdown(group, role, ROLES, width=140, font=self.host._ui_font("body"), background=CARD).pack()
            def payload():
                values = {key: variable.get().strip() for key, variable in variables.items()}
                if not values.get("character_name") or (mode != "edit" and not values.get("character_id")):
                    raise ValueError("请填写角色名称与角色ID。")
                try:
                    values["profession_id"] = int(values["profession_id"])
                except ValueError:
                    raise ValueError("职业ID必须是整数。") from None
                values["club_role"] = role.get()
                if mode == "edit":
                    values["member_key"] = member["member_key"]
                return values
            action = "alliance/members/upsert"
        error.pack(fill="x", padx=14, pady=(8, 0))
        buttons = tk.Frame(box, bg=CARD)
        buttons.pack(fill="x", padx=14, pady=14)
        def submit():
            if self.mutation_pending:
                return
            try:
                values = payload()
            except ValueError as exc:
                error.configure(text=str(exc))
                return
            error.configure(text="正在保存…")
            self.mutation_pending = True
            self.request(action, values, self.mutation_received, channel="mutation")
        self.button(buttons, "保存成员" if mode != "batch" else "确认导入", submit, primary=True).pack(side="left", padx=(0, 10))
        def cancel():
            self.form = None
            self.render_admin()
        self.button(buttons, "取消", cancel).pack(side="left")
        if mode == "edit":
            self.button(buttons, "查看战斗记录", lambda: self.view_member(member), primary=True).pack(side="right")
            self.button(buttons, "移除成员", lambda: self.confirm_remove(member)).pack(side="right", padx=12)

    def mutation_received(self, response):
        self.mutation_pending = False
        if response.get("ok"):
            self.message = "成员资料已更新。"
            self.on_show()
        elif response.get("error") in {"PVP_ALLIANCE_FORBIDDEN", "invalid_session", "card_expired", "card_revoked"}:
            self.status = None
            self.form = None
            self.render_locked(error=str(response.get("message") or "管理员权限已失效。"))
        else:
            self.message = str(response.get("message") or "保存失败，请重试。")
            # Keep the user's input intact on a transient server failure.
            if self.form_error is not None and self.form_error.winfo_exists():
                self.form_error.configure(text=self.message)
            else:
                self.render_admin()

    def confirm_remove(self, member):
        self.form = None
        self.render_admin()
        box = self.card(self.content, "确认移除成员")
        self.label(box, f"将 {member.get('character_name', '--')} 移出战盟？个人战绩仍然保留。", color=RED, anchor="w").pack(fill="x", padx=14, pady=12)
        actions = tk.Frame(box, bg=CARD)
        actions.pack(fill="x", padx=14, pady=(0, 14))
        self.button(actions, "确认移除", lambda: self.request("alliance/members/remove", {"member_key": member["member_key"]},
                    self.mutation_received, channel="mutation")).pack(side="left", padx=(0, 12))
        self.button(actions, "取消", self.render_admin).pack(side="left")
        self.canvas.yview_moveto(1)

    def view_member(self, member):
        self.message = "正在获取成员 PvP 战斗记录…"
        self.render_admin()
        def received(response):
            if response.get("ok"):
                if getattr(self.host, "backend_current_page", "alliance") != "alliance":
                    return
                self.clear_child()
                self.grid_remove()
                self.child_history = PvpHistoryPage(self.master, self.host, self.dropdown, self.scrollbar_class,
                    page_key="alliance", fixed_records=response.get("records", []),
                    heading=member.get("character_name", "成员") + " · 战斗记录", back=self.return_from_member,
                    split_pane=self.split_pane_class)
                self.child_history.grid(row=0, column=0, sticky="nsew")
            else:
                self.mutation_received(response)
        self.request("alliance/members/records", {"member_key": member["member_key"], "limit": 500}, received, channel="member_records")

    def return_from_member(self):
        self.clear_child()
        self.grid(row=0, column=0, sticky="nsew")
        self.on_show()

    def clear_child(self):
        if self.child_history is not None:
            self.child_history.dispose()
            self.child_history.destroy()
            self.child_history = None

    def on_hide(self):
        if self.child_history is not None:
            self.child_history.grid_remove()

    def dispose(self):
        self.clear_child()
        super().dispose()
