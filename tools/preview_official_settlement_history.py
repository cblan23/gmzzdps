"""Open recorded passive settlements in the complete established backend UI.

The preview uses a temporary history directory and never starts capture, login,
upload, or game-process access.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import runpy
import sys
import tempfile
import tkinter as tk
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from combat_history import CombatHistoryStore
from combat_statistics import normalize_statistics
from encounter_tracker import EncounterTracker
from settlement_history_adapter import SettlementHistoryAdapter


DEFAULT_EVIDENCE = (
    Path(
        r"D:\vscode\GMZZDPS-NPCAP\experiments\boss-end-passive-20260914"
        r"\may-manor-wipes-43000\second-start-first-wipe-evidence.json"
    ),
    Path(
        r"D:\vscode\GMZZDPS-NPCAP\experiments\boss-end-passive-20260914"
        r"\may-manor-wipes-43000\third-start-second-wipe-skills.json"
    ),
)
NS = 1_000_000_000


def protocol_field(row: dict, field: int, default=None):
    return row.get(str(field), row.get(field, default))


def _normalized_evidence(evidence: Path):
    source = json.loads(evidence.read_text(encoding="utf-8"))
    received_text = source.get("received_at") or source.get("stat_arrival")
    statistics = source.get("statistics")
    statistics = statistics if isinstance(statistics, list) else []
    if not received_text and statistics:
        received_text = statistics[0].get("event_time")
    if not received_text:
        raise RuntimeError(f"Evidence has no receive timestamp: {evidence}")
    received = dt.datetime.fromisoformat(str(received_text)).timestamp()
    raw_members = source.get("members")
    if not isinstance(raw_members, dict):
        try:
            raw_stage = statistics[0]["decoded_arguments"][0]
        except (IndexError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Evidence has no raw stage table: {evidence}") from exc
        raw_members = protocol_field(raw_stage, 5, {})
    else:
        raw_stage = {
            0: source["stageID"],
            1: 1,
            2: source["battleID"],
            5: raw_members,
        }
    normalized = normalize_statistics(
        {
            "method": "OnMsgUpdateStageCombatStatistics",
            "capture_timestamp_ns": int(received * NS),
            "decoded_arguments": [raw_stage],
        },
        instance_id="official-ui-preview",
        dungeon_id=515,
    )[0]
    return received, raw_members, normalized


def build_preview_history(
    evidence_files: list[Path] | tuple[Path, ...], store: CombatHistoryStore
) -> tuple[str, int]:
    tracker = EncounterTracker()
    settled_rows = []
    latest_context = None
    for evidence in evidence_files:
        received, raw_members, normalized = _normalized_evidence(evidence)
        roster = []
        for index, member in enumerate(normalized.members):
            raw_member = raw_members.get(member.id, {})
            roster.append(
                {
                    "id": member.id,
                    "iid": member.iid,
                    "name": member.name,
                    "profession_id": protocol_field(raw_member, 4, 0),
                    "extraordinary_rating": 18_600 - index * 430,
                }
            )
        self_member = next(
            (member for member in normalized.members if member.name == "莫雪"),
            normalized.members[0],
        )
        killer_sets = [set(member.killer_map or {}) for member in normalized.members]
        common_killers = set.intersection(*killer_sets) if killer_sets else set()
        if not common_killers:
            raise RuntimeError(f"Evidence has no common wipe source: {evidence}")
        boss_token = max(
            common_killers,
            key=lambda token: sum(
                (member.killer_map or {}).get(token, 0)
                for member in normalized.members
            ),
        )
        encounter_length = max(
            (member.member_battle_length or 1)
            + max(0, int((member.killer_map or {}).get(boss_token, 0)) - 1)
            for member in normalized.members
        )
        settled_start = int(received - encounter_length - 45)
        settled = tracker.begin(
            instance_id="official-ui-preview",
            started_at_ns=settled_start * NS,
            participants_snapshot=roster,
            dungeon_id=515,
            stage_id=normalized.stage_id,
            stage_index=1,
            boss_template_id=7114101,
            boss_token=boss_token,
            self_token=self_member.id,
        )
        settled.boss_name = "异化猎犬"
        for member in normalized.members:
            death_count = int((member.killer_map or {}).get(boss_token, 0))
            alive_seconds = float(member.member_battle_length or 1)
            cursor = float(settled_start)
            for death_index in range(death_count):
                cursor += alive_seconds / death_count
                tracker.life(member.id, int(cursor * NS), True)
                if death_index + 1 < death_count:
                    cursor += 1.0
                    tracker.life(member.id, int(cursor * NS), False)
        tracker.end("WIPE", int((settled_start + encounter_length) * NS))
        match = tracker.accept(normalized)
        if match.encounter_id != settled.local_encounter_id:
            raise RuntimeError(
                f"Evidence did not bind to preview encounter: {match.reason}"
            )
        settled_rows.append((settled, normalized))
        latest_context = (
            received,
            roster,
            normalized,
            self_member,
            boss_token,
        )

    if latest_context is None:
        raise RuntimeError("No settlement evidence was supplied")
    received, roster, normalized, self_member, boss_token = latest_context

    pending_start = int(received - 25)
    pending = tracker.begin(
        instance_id="official-ui-preview",
        started_at_ns=pending_start * NS,
        participants_snapshot=roster,
        dungeon_id=515,
        stage_id=normalized.stage_id,
        stage_index=1,
        boss_template_id=7114101,
        boss_token=boss_token,
        self_token=self_member.id,
    )
    pending.boss_name = "异化猎犬"
    tracker.end("WIPE", (pending_start + 10) * NS)

    abandoned_start = int(received - 8)
    abandoned = tracker.begin(
        instance_id="official-ui-preview",
        started_at_ns=abandoned_start * NS,
        participants_snapshot=roster,
        dungeon_id=515,
        stage_id=normalized.stage_id,
        stage_index=1,
        boss_template_id=7114101,
        boss_token=boss_token,
        self_token=self_member.id,
    )
    abandoned.boss_name = "异化猎犬"
    tracker.end("RESET", (abandoned_start + 4) * NS)

    SettlementHistoryAdapter(store).sync(tracker)
    selected_member = next(
        (member for member in normalized.members if member.name == "墨爵"),
        self_member,
    )
    return settled_rows[-1][0].local_encounter_id, int(selected_member.iid or 0)


def initialize_official_history_window(
    app: dict,
    root: tk.Tk,
    store: CombatHistoryStore,
    *,
    enabled_pages: tuple[str, ...] = ("history",),
):
    window_type = app["DpsWindow"]
    window = object.__new__(window_type)
    window.root = root
    window.history_window = None
    window.backend_page_host = None
    window.history_store = store
    window.model = SimpleNamespace(
        boss_only=True,
        runtime_skill_names={},
        target_catalog={},
        completed_combats=[],
        pop_completed_combats=lambda: [],
    )
    window.worker = None
    window.combat_clock_worker = None
    window.pending_clock_records = {}
    window.metadata = app["load_skill_metadata"]()
    window.professions = (
        window.metadata.get("professions", {})
        if isinstance(window.metadata.get("professions"), dict)
        else {}
    )
    window.config = {
        "font_size": 14,
        "history_page_size": 10,
        "history_geometry": "1400x900+32+28",
        "backend_sidebar_width": 270,
    }
    window.main_ui_scale = 1.0
    window.window_dpi = 96
    window.dpi_scale = 1.0
    window.closing = False
    window.compact_mode = False
    window.connected = False
    window.capture_started = False
    window.hide_names = False
    window.history_hide_names = False
    window.backend_topmost = True
    window.backend_current_page = "history"
    window.backend_pages = {}
    window.backend_nav_buttons = {}
    window.backend_sidebar = None
    window.backend_sidebar_art_canvas = None
    window.backend_sidebar_art_after_id = None
    window.sidebar_avatar_label = None
    window.profile_edit_button = None
    window.backend_connection_dot = None
    window.backend_connection_label = None
    window.backend_collapse_button = None
    window.membership_label = None
    window.membership_badge_label = None
    window.expiry_label = None
    window.upload_profile = None
    window.licensing = SimpleNamespace(
        session=SimpleNamespace(card_tier="normal", expires_at=None)
    )
    window.drag_state = {}
    window.resize_state = {}
    window.pending_window_geometry = {}
    window.window_geometry_after_ids = {}
    window.restore_geometry = {}
    window.combat_upload_states = {}
    window.history_upload_in_progress = set()
    window.upload_public_mode = "anonymous"

    for name in (
        "show_total_damage show_dps show_damage_share show_critical_rate "
        "show_penetration_rate show_deaths show_revives show_death_duration "
        "show_effective_healing show_hps show_overheal_rate show_taken "
        "show_taken_share show_boss_damage show_boss_share show_boss_hits "
        "show_boss_max_hit"
    ).split():
        setattr(window, name, True)

    window.history_records = []
    window.history_loaded_record = None
    window.history_selected_id = ""
    window.history_selected_ids = set()
    window.history_selected_actor = 0
    window.history_page_number = 1
    window.history_page_size = 10
    window.history_effective_page_size = 10
    window.history_page_count = 1
    window.history_total_records = 0
    window.history_page_mode = "browser"
    window.history_layout_version = 2
    window.history_data_domain = "pve"
    window.history_visible_fields = set(
        app["normalize_history_visible_fields"](None)
    )
    window.history_meter_mode = "dps"
    window.history_detail_mode = "skills"
    window.history_skill_share_mode = "skills"
    window.history_local_detail_modules_visible = True
    window.history_trend_hover_time = None
    window.history_trend_selected_time = None
    window.history_trend_plot_bounds = None
    window.history_trend_record_id = ""
    window.history_trend_points = []
    window.history_trend_source = "settlement_average"
    window.history_participant_trend_record_id = ""
    window.history_participant_trend_points = {}
    window.history_participant_trend_scope = "team_only"
    window.history_detail_trend_plot_bounds = None
    window.history_detail_trend_hover_time = None
    window.history_detail_trend_selected_time = None
    window.history_skill_share_segments = []
    window.history_skill_share_render = None
    window.history_pvp_snapshot_offset = 0
    window.history_pvp_snapshot_record_id = ""
    window.history_pvp_matchup_cache_key = None
    window.history_pvp_matchup_cache = {}
    window.history_equipment_visible = False
    window.history_critical_luck_plot = None
    window.history_opener_panel_height = app["HISTORY_OPENER_PANEL_MIN_HEIGHT"]
    window.history_opener_regions = []
    window.history_skill_timeline_mode = "all"
    window.history_skill_timeline_page = 0
    window.history_skill_timeline_page_count = 1
    window.history_skill_timeline_hidden_ids = set()
    window.history_skill_timeline_range = (0.0, 1.0)
    window.history_skill_timeline_range_drag = None
    window.history_skill_timeline_range_bounds = None
    window.history_skill_timeline_view_start = 0.0
    window.history_skill_timeline_hits = {}
    window.history_skill_timeline_selected_hit = None
    window.history_skill_timeline_hover_hit = None
    window.history_skill_timeline_focus_time = None
    window.history_skill_timeline_plot_bounds = None
    window.history_skill_timeline_dragging = False

    for name in (
        "history_field_vars history_filter_entries history_filter_dropdowns "
        "history_overview_labels history_overview_cards history_overview_data "
        "history_snapshot_labels "
        "history_skill_share_buttons history_browser_meter_buttons "
        "history_meter_buttons history_disabled_meter_buttons "
        "history_detail_buttons history_skill_timeline_buttons"
    ).split():
        setattr(window, name, {})
    window.history_privacy_buttons = []

    for name in (
        "history_feedback_button history_browser_frame history_battle_detail_frame "
        "history_detail_sticky_toolbar history_page_root history_page_canvas "
        "history_page_scrollbar history_page_content_window history_filter_after_id "
        "history_filter_time_var history_filter_dungeon_var history_filter_boss_var "
        "history_filter_result_var history_sort_var history_page_size_var "
        "history_overview_frame history_browser_meter_group history_sort_dropdown "
        "history_cleanup_button history_refresh_button history_snapshot_heading_label "
        "history_snapshot_button_bar history_pagination_frame history_pagination_control "
        "history_page_range_label history_snapshot_result_canvas "
        "history_snapshot_trend_canvas history_snapshot_trend_title_label "
        "history_snapshot_status_label history_snapshot_icon_label "
        "history_snapshot_identity_text history_detail_hero_canvas "
        "history_detail_icon_label history_detail_dungeon_label "
        "history_detail_occurred_label history_detail_result_canvas "
        "history_detail_status_label history_detail_accent_frame "
        "history_detail_trend_canvas history_detail_trend_title_label "
        "history_detail_trend_peak_label history_detail_trend_hint_label "
        "history_player_icon_label history_player_name_label history_player_rank_label "
        "history_player_rating_label history_player_primary_label "
        "history_player_primary_caption history_player_share_canvas "
        "history_skill_share_canvas history_skill_share_panel "
        "history_equipment_panel history_equipment_canvas "
        "history_equipment_scrollbar history_skill_quality_canvas "
        "history_skill_quality_panel history_critical_luck_title_label "
        "history_critical_luck_caption_label history_critical_luck_help_badge "
        "history_critical_luck_canvas history_critical_luck_panel "
        "history_opener_panel history_opener_canvas history_opener_title_label "
        "history_opener_caption_label history_opener_help_badge "
        "history_skill_timeline_canvas history_skill_timeline_panel "
        "history_skill_timeline_toolbar_canvas history_skill_timeline_range_canvas "
        "history_skill_hit_detail_canvas history_result_value history_modal_overlay "
        "history_list_header_canvas history_list_canvas history_list_scrollbar "
        "history_list_horizontal_scrollbar history_query_var "
        "history_participant_panel history_participant_header_canvas "
        "history_participant_canvas history_participant_scrollbar "
        "history_detail_header history_team_heading_label "
        "history_detail_privacy_control history_team_subtitle_label "
        "history_skill_panel history_skill_canvas history_skill_scrollbar "
        "history_count_label history_target_label history_time_label "
        "history_metrics_label history_total_value history_dps_value "
        "history_team_value history_detail_label history_favorite_button "
        "history_max_button history_pin_button"
    ).split():
        setattr(window, name, None)
    window.history_player_stat_labels = {}
    window.history_privacy_var = None

    window._configure_tk_dpi_scaling()
    window._initialize_ui_fonts()
    window.icons = app["IconFactory"](root)
    window.icons.set_dpi(window.window_dpi)
    original_nav_button = window._backend_nav_button

    def preview_nav_button(
        parent, key, icon_name, caption, *, enabled, module_tag=""
    ):
        return original_nav_button(
            parent,
            key,
            icon_name,
            caption,
            enabled=enabled and key in enabled_pages,
            module_tag=module_tag,
        )

    window._backend_nav_button = preview_nav_button
    window._build_backend_updates_page = lambda parent: tk.Frame(
        parent, bg=app["BG"]
    )
    window._build_backend_settings_page = lambda parent: tk.Frame(
        parent, bg=app["BG"]
    )
    window._remember_backend_sidebar_width = lambda: None
    window._close_history_window = root.destroy
    return window


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence", type=Path, nargs="+", default=list(DEFAULT_EVIDENCE)
    )
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--close-after", type=float, default=0.0)
    args = parser.parse_args()
    for evidence in args.evidence:
        if not evidence.is_file():
            raise FileNotFoundError(evidence)

    app = runpy.run_path(
        str(ROOT / "dps_meter.pyw"), run_name="official_settlement_preview"
    )
    app["enable_windows_dpi_awareness"]()
    temporary = tempfile.TemporaryDirectory(prefix="gmzz-official-history-preview-")
    store = CombatHistoryStore(
        temporary.name,
        catalog_path=app["ASSET_DIR"] / "bosses" / "boss_icon_sources.json",
        profession_path=app["SKILL_METADATA_PATH"],
    )
    settled_id, selected_actor = build_preview_history(args.evidence, store)
    store.refresh_index()

    root = tk.Tk()
    root.title("被动结算隔离预览")
    root.configure(bg=app["BG"])
    root.geometry("1x1+8+8")
    root.overrideredirect(True)
    root.update_idletasks()
    window = initialize_official_history_window(app, root, store)
    window._build_history_window()
    preview = window.history_window
    if preview is None:
        raise RuntimeError("Official backend window was not created")
    window._update_history_filter_options()
    window._query_history_records()
    window.history_selected_id = settled_id
    window.history_selected_actor = selected_actor
    window.history_loaded_record = None
    window._open_selected_history_detail()
    preview.deiconify()
    preview.update_idletasks()
    preview.lift()
    if not preview.winfo_viewable():
        raise tk.TclError("Official backend preview did not become visible")
    root.withdraw()
    preview.deiconify()
    preview.lift()

    def ready() -> None:
        preview.update_idletasks()
        preview.lift()
        preview.focus_force()
        if args.screenshot:
            from PIL import ImageGrab

            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(
                bbox=(
                    preview.winfo_rootx(),
                    preview.winfo_rooty(),
                    preview.winfo_rootx() + preview.winfo_width(),
                    preview.winfo_rooty() + preview.winfo_height(),
                ),
                all_screens=True,
            ).save(args.screenshot)
        try:
            print("OFFICIAL_SETTLEMENT_PREVIEW_READY", flush=True)
        except (AttributeError, OSError):
            pass

    preview.after(1200, ready)
    if args.close_after > 0:
        preview.after(int(args.close_after * 1000), root.destroy)
    try:
        root.mainloop()
    finally:
        temporary.cleanup()


if __name__ == "__main__":
    main()
