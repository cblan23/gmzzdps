"""Shared native battle-detail chrome used by both PVE and PvP history.

The data renderers stay mode-specific.  This module owns only the established
PVE detail shell so a second history page cannot silently drift in spacing,
sizes, borders, or scrolling behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from typing import Callable


TOOLBAR_HEIGHT = 44
HERO_HEIGHT = 190
METRIC_HEIGHT = 76
TAB_HEIGHT = 48
WORKSPACE_HEIGHT = 570
LEFT_PANEL_WIDTH = 380
RIGHT_PANEL_WIDTH = 760


@dataclass(frozen=True)
class NativeDetailWorkspace:
    root: tk.Misc
    left: tk.Frame
    right: tk.Frame


def _mark(widget: tk.Misc, part: str) -> tk.Misc:
    widget._native_history_detail_part = part
    return widget


def build_native_detail_toolbar(
    parent: tk.Misc,
    *,
    background: str,
    sticky_parent: tk.Misc | None = None,
) -> tuple[tk.Frame | None, tk.Frame, tk.Frame]:
    """Create the exact 44px PVE detail toolbar shell.

    PVE supplies ``sticky_parent`` and gets a placeholder plus an overlay.
    Standalone pages use the same shell in normal document flow.
    """

    placeholder = None
    if sticky_parent is not None:
        placeholder = tk.Frame(parent, bg=background, height=TOOLBAR_HEIGHT)
        placeholder.pack(fill="x")
        placeholder.pack_propagate(False)
        shell_parent = sticky_parent
    else:
        shell_parent = parent

    shell = tk.Frame(shell_parent, bg=background, height=TOOLBAR_HEIGHT)
    shell.pack_propagate(False)
    if sticky_parent is None:
        shell.pack(fill="x")
    toolbar = tk.Frame(shell, bg=background, height=TOOLBAR_HEIGHT)
    toolbar.pack(fill="both", expand=True, padx=18)
    toolbar.pack_propagate(False)
    _mark(shell, "toolbar")
    return placeholder, shell, toolbar


def build_native_detail_hero(
    parent: tk.Misc,
    *,
    background: str = "#0a0e0f",
    border: str = "#34403e",
) -> tk.Canvas:
    canvas = tk.Canvas(
        parent,
        bg=background,
        height=HERO_HEIGHT,
        bd=0,
        highlightthickness=1,
        highlightbackground=border,
    )
    canvas.pack(fill="x", padx=18)
    return _mark(canvas, "hero")


def build_native_detail_metrics(
    parent: tk.Misc,
    *,
    border: str,
) -> tk.Frame:
    metrics = tk.Frame(parent, bg=border, height=METRIC_HEIGHT)
    metrics.pack(fill="x", padx=18, pady=(8, 0))
    metrics.pack_propagate(False)
    metrics.grid_propagate(False)
    metrics.grid_rowconfigure(0, weight=1)
    return _mark(metrics, "metrics")


def build_native_detail_metric_card(
    parent: tk.Misc,
    *,
    caption: str,
    value: str,
    column: int,
    accent: str,
    subtitle: str,
    surface: str,
    muted: str,
    subtle: str,
    font: Callable[[str], object],
) -> tk.Label:
    parent.grid_columnconfigure(column, weight=1, uniform="history_detail_metrics")
    card = tk.Frame(parent, bg=surface)
    card.grid(
        row=0,
        column=column,
        sticky="nsew",
        padx=(0 if column == 0 else 1, 0),
    )
    tk.Frame(card, bg=accent, width=3).pack(side="left", fill="y")
    body = tk.Frame(card, bg=surface)
    body.pack(side="left", fill="both", expand=True)
    caption_label = tk.Label(
        body,
        text=caption,
        bg=surface,
        fg=muted,
        anchor="w",
        font=font("micro"),
    )
    caption_label.pack(fill="x", padx=11, pady=(8, 0))
    value_label = tk.Label(
        body,
        text=value,
        bg=surface,
        fg=accent,
        anchor="w",
        font=font("number_large"),
    )
    value_label.pack(fill="x", padx=11, pady=(3, 0))
    subtitle_label = tk.Label(
        body,
        text=subtitle,
        bg=surface,
        fg=subtle,
        anchor="w",
        font=font("micro"),
    )
    # The native PVE card keeps this as metadata so the 76px band remains
    # compact while live mode switching can still update its copy.
    value_label._caption_label = caption_label
    value_label._subtitle_label = subtitle_label
    value_label._accent_color = accent
    value_label._subtitle = subtitle
    return value_label


def build_native_detail_tabs(
    parent: tk.Misc,
    *,
    surface: str,
    border: str,
    text: str,
    font: Callable[[str], object],
) -> tk.Frame:
    bar = tk.Frame(
        parent,
        bg=surface,
        height=TAB_HEIGHT,
        highlightthickness=1,
        highlightbackground=border,
    )
    bar.pack(fill="x", padx=18, pady=(8, 0))
    bar.pack_propagate(False)
    tk.Label(
        bar,
        text="战斗分析",
        bg=surface,
        fg=text,
        padx=16,
        font=font("strong"),
    ).pack(side="left", fill="y")
    tk.Frame(bar, bg=border, width=1).pack(side="left", fill="y")
    return _mark(bar, "tabs")


def build_native_detail_workspace(
    parent: tk.Misc,
    *,
    split_pane_class,
    background: str,
    panel: str,
    border: str,
    bottom_padding: int = 0,
) -> NativeDetailWorkspace:
    if split_pane_class is not None:
        workspace = split_pane_class(parent, background=background)
    else:
        workspace = tk.Frame(parent, bg=background)
    workspace.configure(height=WORKSPACE_HEIGHT)
    workspace.pack_propagate(False)
    workspace.pack(fill="x", padx=18, pady=(8, bottom_padding))
    _mark(workspace, "workspace")

    left = tk.Frame(
        workspace,
        bg=panel,
        highlightthickness=1,
        highlightbackground=border,
    )
    right = tk.Frame(
        workspace,
        bg=panel,
        highlightthickness=1,
        highlightbackground=border,
    )
    if split_pane_class is not None:
        workspace.add(left, minsize=330, width=LEFT_PANEL_WIDTH, stretch="never")
        workspace.add(right, minsize=500, width=RIGHT_PANEL_WIDTH, stretch="always")
    else:
        workspace.grid_propagate(False)
        workspace.grid_columnconfigure(0, weight=0, minsize=LEFT_PANEL_WIDTH)
        workspace.grid_columnconfigure(1, weight=1, minsize=RIGHT_PANEL_WIDTH)
        workspace.grid_rowconfigure(0, weight=1)
        left.grid(row=0, column=0, sticky="nsew")
        right.grid(row=0, column=1, sticky="nsew")
    return NativeDetailWorkspace(workspace, left, right)
