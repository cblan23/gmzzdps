#!/usr/bin/env python3
"""Privacy-bounded capture analysis and anonymous diagnostic upload."""

from __future__ import annotations

import json
import locale
import os
import platform
import socket
import ssl
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from network_state import DAMAGE_TARGET_TEMPLATE_IDS, NetworkPacketParser


TOOL_NAME = "叨叨诡秘问题检测工具"
TOOL_VERSION = "1.1.0+20260904.1"
DIAGNOSTIC_SCHEMA_VERSION = 4
MAX_REPORTED_METHODS = 80
MAX_REPORTED_ERRORS = 20
MAX_REPORTED_BOSSES = 24
MAX_REPORTED_DAMAGE_TARGETS = 16
MAX_REPORTED_NATIVE_TEMPLATES = 32
MAX_TRACKED_DAMAGE_TARGETS = 64
MAX_TRACKED_NATIVE_ENTITIES = 4096
MAX_UPLOAD_BYTES = 224 * 1024

ASSESSMENT_RECOMMENDATIONS = {
    "capture_pipeline_ok": "采集链路正常；若主程序仍无显示，继续核对主程序版本、Boss 模式与界面筛选状态。",
    "damage_dummy_pipeline_ok": "公共采集链路正常；再对实际出问题的 Boss 运行一次检测，以验证该 Boss 模板。",
    "game_not_connected": "确认游戏已启动且检测工具以管理员身份运行；仍失败时核对游戏进程名。",
    "network_hook_failed": "核对当前游戏版本与运行配置中的网络 Hook 签名，并排查其他同类工具占用 Hook。",
    "network_hook_silent": "网络 Hook 已安装但没有回调；优先核对入口签名、Hook 所有权与游戏版本。",
    "no_damage_observed": "网络消息正常，本次未观察到伤害；让用户持续攻击同一个 Boss 或伤害木桩后重测。",
    "native_hook_silent_network_fallback_blocked": "修复伤害源选择：原生 Hook 静默时必须继续采用网络伤害。",
    "damage_decode_or_identity_failed": "核对伤害参数布局、实体 ID 解析与跨来源去重条件。",
    "boss_catalog_unavailable": "把当前 Boss 模板目录正确打入检测工具并验证加载路径。",
    "damage_target_not_confirmed": "伤害已解析但目标身份不足；核对模板 Hook、目标反查与 Boss 目录门槛。",
    "target_lookup_not_triggered": "查看目标范围和本机角色冲突计数，修正候选目标进入反查队列的条件。",
    "target_lookup_target_matches_local_player": "本机角色 ID 与受击目标发生冲突；修正本机实体读取偏移，或不要用该冲突作为 Boss 反查的唯一排除条件。",
    "target_lookup_target_id_range_unsupported": "伤害目标落在当前实体 ID 范围之外；根据匿名范围计数调整实体 ID 校验规则。",
    "target_lookup_no_common_component_class": "模板 Hook 没有提供可信 CommonComponent 类型；核对模板入口是否安装并产生记录。",
    "target_lookup_object_table_unreadable": "更新当前游戏版本对应的 UObject 表地址或对象表布局。",
    "target_lookup_common_component_not_indexed": "对象表可读但类型过滤未命中；核对 UClass 偏移与 CommonComponent 类型来源。",
    "target_lookup_exact_component_template_zero": "目标组件已精确匹配但模板仍为 0；核对模板字段偏移和模板初始化时机。",
    "target_lookup_exact_component_not_found": "对象表可读但实体无法连接；核对 CommonComponent 的实体 ID 字段偏移。",
    "target_lookup_exact_template_not_emitted": "模板已读到但没有进入解析器；修复子进程批次传递或元数据消费顺序。",
    "target_lookup_resolved_but_boss_rejected": "目标模板已识别；确认它确为 Boss 后补充 Boss 目录，或修正目录门槛。",
}

# Keep this list in sync with capture_process.py.  The analyzer is also used
# directly by tests and by older diagnostic payloads, so it must enforce the
# privacy boundary even when the child-process sanitizer was bypassed.
_NATIVE_DIAGNOSTIC_KEYS = frozenset(
    {
        "damage_ring_header_reads",
        "damage_ring_header_failures",
        "damage_ring_records_polled",
        "damage_ring_parse_failures",
        "damage_ring_overruns",
        "damage_target_records",
        "damage_target_id_zero",
        "damage_target_id_below_supported",
        "damage_target_id_low_supported",
        "damage_target_id_mid_supported",
        "damage_target_id_gap",
        "damage_target_id_high_supported",
        "damage_target_id_above_supported",
        "local_player_id_available_records",
        "damage_attacker_matches_local_player",
        "damage_target_matches_local_player",
        "boss_ring_header_reads",
        "boss_ring_header_failures",
        "boss_ring_records_polled",
        "boss_ring_parse_failures",
        "boss_ring_overruns",
        "target_lookup_candidates",
        "target_lookup_disabled_candidates",
        "target_lookup_skipped_unplausible",
        "target_lookup_skipped_local_player",
        "target_lookup_skipped_already_attempted",
        "target_lookup_skipped_already_emitted",
        "target_lookup_pending_reobserved",
        "target_lookup_evictions",
        "target_lookup_resolved",
        "target_lookup_timeouts",
        "target_lookup_component_observations",
        "target_lookup_class_reads",
        "target_lookup_class_matches",
        "target_lookup_component_reads",
        "target_lookup_component_matches",
        "target_lookup_component_read_failures",
        "target_lookup_component_class_rejections",
        "target_lookup_component_entity_mismatches",
        "target_lookup_component_template_zero",
        "target_lookup_observed_target_matches",
        "target_lookup_observed_target_template_zero",
        "target_lookup_observed_target_template_nonzero",
        "target_lookup_object_scans",
        "target_lookup_object_candidates",
        "target_lookup_object_table_reads",
        "target_lookup_object_table_failures",
        "target_lookup_object_slots_scanned",
        "target_lookup_object_pointers",
        "target_lookup_object_class_reads",
        "target_lookup_object_class_read_failures",
        "target_lookup_object_class_candidates",
        "target_lookup_object_component_read_failures",
        "target_lookup_object_entity_candidates",
        "target_lookup_object_exact_entity_matches",
        "target_lookup_object_exact_template_zero",
        "target_lookup_object_exact_template_nonzero",
        "existing_boss_scan_attempts",
        "existing_boss_scan_matches",
        "late_boss_component_resolved",
        "damage_hook_installed",
        "damage_hook_adopted",
        "name_hook_installed",
        "boss_type_hook_installed",
        "boss_init_hook_installed",
        "template_id_hook_installed",
        "template_bulk_hook_installed",
        "target_boss_lookup_enabled",
        "target_lookup_pending",
        "target_lookup_attempted",
        "target_lookup_emitted",
        "target_lookup_observed_components",
        "target_lookup_trusted_classes",
        "target_lookup_object_index_complete",
        "target_lookup_object_scan_count",
        "existing_boss_scan_attempted",
        "existing_boss_full_scan_complete",
    }
)


def _sorted_counter(counter: Counter[str]) -> dict[str, int]:
    """Serialize anonymous counters in a stable order for reports/tests."""
    result: dict[str, int] = {}
    for key, value in sorted(counter.items(), key=lambda item: str(item[0])):
        try:
            numeric = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if numeric > 0:
            result[str(key)] = numeric
    return result


class DiagnosticUploadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiagnosticSubmission:
    diagnostic_id: str
    message: str


def _last_error_line(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("details", "")
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:900]


def _safe_int(value: object, default: int = 0) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_bool(value: object) -> bool:
    return bool(value)


class DiagnosticAnalyzer:
    """Aggregate capture behavior without retaining raw RPC payloads."""

    def __init__(
        self,
        boss_template_catalog: dict[str, dict] | None = None,
        boss_name_allowlist: tuple[str, ...] = (),
    ):
        self.catalog = boss_template_catalog or {}
        self.parser = NetworkPacketParser(
            boss_template_catalog=self.catalog,
            boss_name_allowlist=boss_name_allowlist,
        )
        self.started_at = time.time()
        self.finished_at = 0.0
        self.stages: list[str] = []
        self.errors: list[dict[str, str]] = []
        self.connected_payload: dict[str, object] = {}
        self.process_payload: dict[str, object] = {}
        self.method_counts: Counter[str] = Counter()
        self.parser_update_counts: Counter[str] = Counter()
        self.network_records = 0
        self.decoded_network_records = 0
        self.network_damage_messages = 0
        self.network_damage_positive = 0
        self.network_damage_suppressed_by_native = 0
        self.native_damage_records = 0
        self.native_positive_damage_records = 0
        self.native_boss_records = 0
        self.native_name_records = 0
        self.native_skill_name_records = 0
        self.capture_batches = 0
        self.sequence_gaps = 0
        self.parsed_damage_events = 0
        self.forwarded_damage_events = 0
        self.filtered_damage_events = 0
        self.team_stat_updates = 0
        self.team_status: dict[str, int] = {}
        self.bosses: dict[tuple[int, str], dict[str, object]] = {}
        self.native_entity_templates: dict[int, dict[str, object]] = {}
        self.native_template_counts: Counter[tuple[int, int]] = Counter()
        self.native_template_stats: Counter[str] = Counter()
        self.damage_targets: dict[int, dict[str, object]] = {}
        self.native_diagnostic: dict[str, object] = {}
        self.native_diagnostic_snapshots = 0
        self.native_diagnostic_resets = 0
        # These counters describe where a captured damage candidate stopped.
        # They are deliberately kept separate from the production parser and
        # contain no entity IDs, names, packets, or memory addresses.
        self.damage_stage_counts: Counter[str] = Counter()
        self.damage_gate_reasons: Counter[str] = Counter()

    def _stage(self, value: object) -> None:
        stage = str(value or "").strip()[:64]
        if stage and (not self.stages or self.stages[-1] != stage):
            self.stages.append(stage)
            del self.stages[:-32]

    def _error(self, component: object, details: object) -> None:
        detail = _last_error_line(details)
        if not detail:
            return
        self.errors.append(
            {
                "component": str(component or "capture")[:64],
                "detail": detail,
            }
        )
        del self.errors[:-MAX_REPORTED_ERRORS]

    def _remember_boss(self, payload: dict) -> None:
        entity_type = str(payload.get("entity_type", "")).casefold()
        boss_type = _safe_int(payload.get("boss_type"), -1)
        if entity_type != "boss" and boss_type != 3:
            return
        template_id = _safe_int(payload.get("template_id"))
        name = str(payload.get("name", "")).strip()[:64]
        key = (template_id, name)
        self.bosses[key] = {
            "template_id": template_id,
            "name": name,
            "boss_type": boss_type,
            "source": str(payload.get("boss_source", ""))[:64],
        }
        while len(self.bosses) > MAX_REPORTED_BOSSES:
            self.bosses.pop(next(iter(self.bosses)))

    def _catalog_metadata(self, template_id: int) -> dict:
        metadata = self.catalog.get(str(template_id), {})
        return metadata if isinstance(metadata, dict) else {}

    def _merge_native_diagnostic(self, value: object) -> None:
        """Merge cumulative hook counters without trusting unbounded input."""

        if not isinstance(value, dict):
            return
        self.native_diagnostic_snapshots += 1
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()[:80]
            if not key or key not in _NATIVE_DIAGNOSTIC_KEYS:
                continue
            if isinstance(raw_value, bool):
                self.native_diagnostic[key] = bool(raw_value)
                continue
            if not isinstance(raw_value, (int, float)):
                continue
            try:
                numeric = float(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if numeric != numeric or abs(numeric) == float("inf"):
                continue
            numeric = max(0.0, min(2_000_000_000.0, numeric))
            previous = self.native_diagnostic.get(key)
            if isinstance(previous, (int, float)) and not isinstance(previous, bool):
                if numeric < float(previous):
                    # A child hook can be reinstalled after a recoverable
                    # capture error. Keep the latest value while recording the
                    # reset so an analyst does not mistake it for zero events.
                    self.native_diagnostic_resets += 1
                numeric = max(float(previous), numeric)
            self.native_diagnostic[key] = int(numeric)

    @staticmethod
    def _network_damage_shape(record: dict) -> tuple[str, int, int, int]:
        """Return (shape, attacker, target, damage) for a network candidate."""

        args = NetworkPacketParser._args(record)
        if len(args) < 9:
            return "decode_failed", 0, 0, 0
        try:
            attacker = int(args[0])
            target = int(args[1])
            damage = int(args[7])
        except (TypeError, ValueError, OverflowError):
            return "decode_failed", 0, 0, 0
        if attacker <= 0 or target <= 0:
            return "identity_incomplete", max(0, attacker), max(0, target), max(0, damage)
        if damage <= 0:
            return "non_positive", attacker, target, damage
        return "valid", attacker, target, damage

    @staticmethod
    def _native_damage_shape(record: dict) -> tuple[str, int, int, int]:
        """Return (shape, attacker, target, damage) for a native candidate."""

        try:
            attacker = int(record["attacker_id"])
            target = int(record["target_id"])
            damage = int(record.get("damage", record.get("arg9_i32", 0)))
        except (KeyError, TypeError, ValueError, OverflowError):
            return "decode_failed", 0, 0, 0
        if attacker <= 0 or target <= 0:
            return "identity_incomplete", max(0, attacker), max(0, target), max(0, damage)
        if damage <= 0:
            return "non_positive", attacker, target, damage
        return "valid", attacker, target, damage

    def _record_target_gate_reason(self, target_id: int, *, forwarded: bool | None) -> None:
        """Classify one valid target after the normal parser has seen it."""

        target_id = int(target_id or 0)
        if target_id <= 0:
            reason = "target_id_missing"
        elif (
            target_id in self.parser.confirmed_boss_entities
            or target_id == self.parser.active_boss_entity_id
            or target_id in self.parser.encounter_auxiliary_entities
        ):
            reason = "boss_confirmed"
        elif (
            target_id in self.parser.training_dummy_entities
            and _safe_int(self.parser.entity_template_ids.get(target_id, 0))
            in DAMAGE_TARGET_TEMPLATE_IDS
        ):
            reason = "damage_dummy_confirmed"
        elif target_id == self.parser.self_id or target_id in self.parser.party_ids:
            reason = "target_is_party_or_self"
        else:
            template_id = _safe_int(
                self.parser.entity_template_ids.get(target_id, 0)
            )
            boss_type = _safe_int(
                self.parser.entity_boss_types.get(target_id, -1), -1
            )
            profile = self.parser.entity_profiles.get(target_id, {})
            if not isinstance(profile, dict):
                profile = {}
            entity_type = str(profile.get("entity_type", "")).strip()
            if not template_id and boss_type < 0 and not entity_type:
                reason = "target_identity_unknown"
            elif not template_id:
                reason = "target_template_missing"
            elif str(template_id) not in self.parser.boss_template_catalog:
                reason = "target_template_not_in_boss_catalog"
            elif boss_type == 3:
                # A catalog match is strong evidence, but if the normal parser
                # did not activate it we need to preserve that distinction.
                reason = "target_catalog_match_not_activated"
            else:
                reason = "target_runtime_type_not_boss"
        self.damage_gate_reasons[reason] += 1
        if forwarded is False:
            self.damage_gate_reasons["filtered_by_known_target"] += 1

    def _record_damage_shape(
        self,
        source: str,
        shape: str,
        target_id: int = 0,
        *,
        forwarded: bool | None = None,
        event_emitted: bool = False,
        suppressed_by_native: bool = False,
    ) -> None:
        prefix = "network" if source == "network" else "native"
        self.damage_stage_counts[f"{prefix}_damage_captured"] += 1
        if shape == "decode_failed":
            self.damage_stage_counts[f"{prefix}_damage_decode_failed"] += 1
            return
        self.damage_stage_counts[f"{prefix}_damage_decoded"] += 1
        if shape == "identity_incomplete":
            self.damage_stage_counts[f"{prefix}_damage_identity_incomplete"] += 1
            return
        if shape == "non_positive":
            self.damage_stage_counts[f"{prefix}_damage_non_positive"] += 1
            return
        self.damage_stage_counts[f"{prefix}_damage_positive"] += 1
        if suppressed_by_native:
            self.damage_stage_counts[
                f"{prefix}_damage_suppressed_by_native"
            ] += 1
            self._record_target_gate_reason(target_id, forwarded=None)
            return
        if event_emitted:
            self.damage_stage_counts[f"{prefix}_damage_event_emitted"] += 1
        else:
            self.damage_stage_counts[f"{prefix}_damage_event_rejected"] += 1
        self._record_target_gate_reason(target_id, forwarded=forwarded)

    def _remember_native_template(self, record: dict) -> None:
        entity_id = _safe_int(record.get("entity_id"))
        template_id = _safe_int(record.get("template_id"))
        boss_type = _safe_int(record.get("boss_type", -1), -1)
        catalog_match = bool(
            template_id and str(template_id) in self.parser.boss_template_catalog
        )
        self.native_template_counts[(template_id, boss_type)] += 1
        self.native_template_stats["records"] += 1
        if entity_id:
            self.native_template_stats["records_with_entity_id"] += 1
        if template_id:
            self.native_template_stats["records_with_template_id"] += 1
        if boss_type == 3:
            self.native_template_stats["runtime_type3_records"] += 1
        if catalog_match:
            self.native_template_stats["catalog_match_records"] += 1
        if not entity_id:
            return
        observation = {
            "template_id": template_id,
            "runtime_boss_type": boss_type,
            "catalog_match": catalog_match,
        }
        self.native_entity_templates.pop(entity_id, None)
        self.native_entity_templates[entity_id] = observation
        while len(self.native_entity_templates) > MAX_TRACKED_NATIVE_ENTITIES:
            self.native_entity_templates.pop(next(iter(self.native_entity_templates)))
        target = self.damage_targets.get(entity_id)
        if target is not None:
            target.update(observation)

    def _remember_damage_event(self, payload: dict, *, forwarded: bool) -> None:
        target_id = _safe_int(payload.get("target_id"))
        damage = max(0, _safe_int(payload.get("damage")))
        if not target_id or damage <= 0:
            return
        active_boss = target_id == int(self.parser.active_boss_entity_id or 0)
        confirmed_boss = target_id in self.parser.confirmed_boss_entities
        encounter_auxiliary = target_id in self.parser.encounter_auxiliary_entities
        damage_dummy = bool(
            target_id in self.parser.training_dummy_entities
            and _safe_int(self.parser.entity_template_ids.get(target_id, 0))
            in DAMAGE_TARGET_TEMPLATE_IDS
        )
        boss_mode_confirmed = bool(
            confirmed_boss
            or damage_dummy
            or (encounter_auxiliary and self.parser.active_boss_entity_id is not None)
        )
        existing = self.damage_targets.pop(target_id, None) or {
            "records": 0,
            "damage": 0,
            "forwarded_records": 0,
            "filtered_records": 0,
            "boss_mode_confirmed_records": 0,
            "active_boss_seen": False,
            "template_id": 0,
            "runtime_boss_type": -1,
            "catalog_match": False,
        }
        existing["records"] = int(existing.get("records", 0) or 0) + 1
        existing["damage"] = int(existing.get("damage", 0) or 0) + damage
        count_key = "forwarded_records" if forwarded else "filtered_records"
        existing[count_key] = int(existing.get(count_key, 0) or 0) + 1
        if boss_mode_confirmed:
            existing["boss_mode_confirmed_records"] = int(
                existing.get("boss_mode_confirmed_records", 0) or 0
            ) + 1
        existing["active_boss_seen"] = bool(
            existing.get("active_boss_seen") or active_boss
        )
        existing["last_filetime_100ns"] = _safe_int(
            payload.get("filetime_100ns")
        )
        observation = self.native_entity_templates.get(target_id)
        if observation is not None:
            existing.update(observation)
        self.damage_targets[target_id] = existing
        while len(self.damage_targets) > MAX_TRACKED_DAMAGE_TARGETS:
            self.damage_targets.pop(next(iter(self.damage_targets)))

    def _consume_updates(self, updates: list[tuple[str, dict]]) -> None:
        for kind, payload in updates:
            self.parser_update_counts[str(kind)] += 1
            if not isinstance(payload, dict):
                continue
            if kind == "event":
                self.parsed_damage_events += 1
                forwarded = self.parser.should_forward_damage_event(payload)
                if forwarded:
                    self.forwarded_damage_events += 1
                else:
                    self.filtered_damage_events += 1
                self._remember_damage_event(payload, forwarded=forwarded)
            elif kind == "team_stat":
                self.team_stat_updates += 1
            elif kind == "profile":
                self._remember_boss(payload)

    @staticmethod
    def _positive_network_damage(record: dict) -> bool:
        if str(record.get("method", "")) != "OnMsgDamageSyncV2":
            return False
        args = NetworkPacketParser._args(record)
        if len(args) < 9:
            return False
        try:
            return bool(int(args[0]) and int(args[1]) and int(args[7]) > 0)
        except (TypeError, ValueError, OverflowError):
            return False

    def _enrich_native_boss(self, record: dict) -> dict:
        enriched = dict(record)
        template_id = _safe_int(enriched.get("template_id"))
        metadata = self.catalog.get(str(template_id), {})
        if not isinstance(metadata, dict):
            return enriched
        name = str(metadata.get("name", "")).strip()
        if name:
            enriched["name"] = name
        if metadata.get("level") not in (None, ""):
            enriched["level"] = metadata["level"]
        return enriched

    def _handle_batch(self, payload: dict) -> None:
        self.capture_batches += 1
        self._merge_native_diagnostic(payload.get("native_diagnostic"))
        records = [item for item in payload.get("records", []) if isinstance(item, dict)]
        native_records = [
            item for item in payload.get("native_records", []) if isinstance(item, dict)
        ]
        native_boss_records = [
            item
            for item in payload.get("native_boss_records", [])
            if isinstance(item, dict)
        ]
        native_name_records = [
            item
            for item in payload.get("native_name_records", [])
            if isinstance(item, dict)
        ]
        native_skill_name_records = [
            item
            for item in payload.get("native_skill_name_records", [])
            if isinstance(item, dict)
        ]
        native_active = bool(payload.get("native_damage_hook_installed"))
        self.network_records += len(records)
        self.native_damage_records += len(native_records)
        self.native_boss_records += len(native_boss_records)
        self.native_name_records += len(native_name_records)
        self.native_skill_name_records += len(native_skill_name_records)
        self.sequence_gaps += len(payload.get("sequence_gaps", []) or [])

        for record in native_boss_records:
            try:
                self._remember_native_template(record)
                enriched = self._enrich_native_boss(record)
                entity_id = _safe_int(enriched.get("entity_id"))
                template_id = _safe_int(enriched.get("template_id"))
                boss_type = _safe_int(enriched.get("boss_type"), -1)
                self.damage_stage_counts["native_boss_records_captured"] += 1
                if entity_id:
                    self.damage_stage_counts[
                        "native_boss_records_with_entity_id"
                    ] += 1
                if template_id:
                    self.damage_stage_counts[
                        "native_boss_records_with_template_id"
                    ] += 1
                if template_id and str(template_id) in self.parser.boss_template_catalog:
                    self.damage_stage_counts["native_boss_catalog_matches"] += 1
                if boss_type == 3:
                    self.damage_stage_counts["native_boss_runtime_type3"] += 1
                updates = self.parser.process_native_boss_type(enriched)
                self._consume_updates(updates)
            except Exception as exc:
                self._error("native_boss_parser", exc)
        for record in native_name_records:
            try:
                update = self.parser.apply_runtime_boss_name(record)
                if update:
                    self._consume_updates([update])
            except Exception as exc:
                self._error("native_name_parser", exc)
        for record in native_records:
            shape, _attacker, target_id, _damage = self._native_damage_shape(record)
            if shape == "valid":
                self.native_positive_damage_records += 1
            try:
                updates = self.parser.process_native_damage(record)
                event_emitted = any(kind == "event" for kind, _item in updates)
                event_payload = next(
                    (item for kind, item in updates if kind == "event"),
                    None,
                )
                self._record_damage_shape(
                    "native",
                    shape,
                    target_id,
                    forwarded=(
                        self.parser.should_forward_damage_event(
                            event_payload
                        )
                        if isinstance(event_payload, dict)
                        else None
                    ),
                    event_emitted=isinstance(event_payload, dict),
                )
                self._consume_updates(updates)
            except Exception as exc:
                if shape == "valid":
                    self.damage_stage_counts["native_damage_parser_errors"] += 1
                self._error("native_damage_parser", exc)

        for record in records:
            method = str(record.get("method", "")).strip()[:160] or "<empty>"
            self.method_counts[method] += 1
            if isinstance(record.get("decoded_arguments"), list):
                self.decoded_network_records += 1
            positive_damage = self._positive_network_damage(record)
            if method == "OnMsgDamageSyncV2":
                self.network_damage_messages += 1
            if positive_damage:
                self.network_damage_positive += 1
            try:
                if method == "OnMsgDamageSyncV2":
                    shape, _attacker, target_id, _damage = self._network_damage_shape(
                        record
                    )
                else:
                    shape, target_id = "non_damage", 0
                updates = self.parser.process(
                    record, include_damage=True
                )
                if method == "OnMsgDamageSyncV2":
                    event_payload = next(
                        (item for kind, item in updates if kind == "event"),
                        None,
                    )
                    suppressed_by_native = bool(
                        native_active
                        and shape == "valid"
                        and not isinstance(event_payload, dict)
                    )
                    if suppressed_by_native:
                        self.network_damage_suppressed_by_native += 1
                    self._record_damage_shape(
                        "network",
                        shape,
                        target_id,
                        forwarded=(
                            self.parser.should_forward_damage_event(event_payload)
                            if isinstance(event_payload, dict)
                            else None
                        ),
                        event_emitted=isinstance(event_payload, dict),
                        suppressed_by_native=suppressed_by_native,
                    )
                self._consume_updates(updates)
            except Exception as exc:
                if method == "OnMsgDamageSyncV2" and shape == "valid":
                    self.damage_stage_counts["network_damage_parser_errors"] += 1
                self._error("network_parser", exc)

        team_status = payload.get("team_status")
        if isinstance(team_status, dict):
            self.team_status = {
                "request_count": _safe_int(team_status.get("request_count")),
                "last_result": _safe_int(team_status.get("last_result")),
                "last_request_filetime": _safe_int(
                    team_status.get("last_request_filetime")
                ),
            }

    def _damage_target_summaries(self) -> list[dict[str, object]]:
        selected = list(self.damage_targets.items())[-MAX_REPORTED_DAMAGE_TARGETS:]
        summaries: list[dict[str, object]] = []
        for index, (target_id, observed) in enumerate(selected, 1):
            native = self.native_entity_templates.get(target_id, {})
            template_id = _safe_int(
                native.get(
                    "template_id",
                    observed.get(
                        "template_id",
                        self.parser.entity_template_ids.get(target_id, 0),
                    ),
                )
            )
            boss_type = _safe_int(
                native.get(
                    "runtime_boss_type",
                    observed.get(
                        "runtime_boss_type",
                        self.parser.entity_boss_types.get(target_id, -1),
                    ),
                ),
                -1,
            )
            metadata = self._catalog_metadata(template_id)
            catalog_match = bool(
                template_id and str(template_id) in self.parser.boss_template_catalog
            )
            confirmed_boss = target_id in self.parser.confirmed_boss_entities
            encounter_auxiliary = target_id in self.parser.encounter_auxiliary_entities
            damage_dummy = bool(template_id in DAMAGE_TARGET_TEMPLATE_IDS)
            records = max(0, _safe_int(observed.get("records")))
            boss_mode_events = max(
                0, _safe_int(observed.get("boss_mode_confirmed_records"))
            )
            if confirmed_boss or damage_dummy:
                boss_mode_events = records
            summaries.append(
                {
                    "target": f"target_{index}",
                    "records": records,
                    "damage": max(0, _safe_int(observed.get("damage"))),
                    "forwarded_records": max(
                        0, _safe_int(observed.get("forwarded_records"))
                    ),
                    "filtered_records": max(
                        0, _safe_int(observed.get("filtered_records"))
                    ),
                    "template_id": template_id,
                    "runtime_boss_type": boss_type,
                    "catalog_known": bool(template_id and metadata),
                    "catalog_match": catalog_match,
                    "catalog_name": str(metadata.get("name", "")).strip()[:64],
                    "native_template_observed": bool(native),
                    "identity_observed": bool(
                        native
                        or template_id
                        or boss_type >= 0
                        or target_id in self.parser.entity_profiles
                    ),
                    "confirmed_boss": confirmed_boss,
                    "damage_dummy": damage_dummy,
                    "encounter_auxiliary": encounter_auxiliary,
                    "active_boss_seen": bool(observed.get("active_boss_seen")),
                    "boss_mode_events": boss_mode_events,
                    "boss_mode_displayable": boss_mode_events > 0,
                    "target_mode_displayable": boss_mode_events > 0,
                }
            )
        return summaries

    def _native_template_summaries(self) -> list[dict[str, object]]:
        def sort_key(item: tuple[tuple[int, int], int]) -> tuple[int, int, int, int]:
            (template_id, boss_type), count = item
            catalog_match = str(template_id) in self.parser.boss_template_catalog
            interesting = bool(catalog_match or boss_type == 3)
            return (-int(interesting), -count, template_id, boss_type)

        summaries: list[dict[str, object]] = []
        for (template_id, boss_type), count in sorted(
            self.native_template_counts.items(), key=sort_key
        )[:MAX_REPORTED_NATIVE_TEMPLATES]:
            metadata = self._catalog_metadata(template_id)
            summaries.append(
                {
                    "template_id": template_id,
                    "runtime_boss_type": boss_type,
                    "records": count,
                    "catalog_known": bool(template_id and metadata),
                    "catalog_match": bool(
                        template_id
                        and str(template_id) in self.parser.boss_template_catalog
                    ),
                    "catalog_name": str(metadata.get("name", "")).strip()[:64],
                }
            )
        return summaries

    def _boss_confirmed_damage_events(self) -> int:
        return sum(
            max(0, _safe_int(target.get("boss_mode_events")))
            for target in self._damage_target_summaries()
        )

    def _damage_dummy_events(self) -> int:
        return sum(
            max(0, _safe_int(target.get("records")))
            for target in self._damage_target_summaries()
            if bool(target.get("damage_dummy"))
        )

    def _confirmed_boss_events(self) -> int:
        return sum(
            max(0, _safe_int(target.get("records")))
            for target in self._damage_target_summaries()
            if bool(target.get("confirmed_boss"))
        )

    def _target_identity_links(self) -> dict[str, int]:
        """Summarize exact target/identity joins without exporting either ID."""

        target_ids = set(self.damage_targets)
        native_matches = target_ids.intersection(self.native_entity_templates)
        return {
            "damage_targets": len(target_ids),
            "native_entity_exact_matches": len(native_matches),
            "native_template_exact_matches": sum(
                bool(
                    _safe_int(
                        self.native_entity_templates[target_id].get(
                            "template_id"
                        )
                    )
                )
                for target_id in native_matches
            ),
            "native_runtime_boss_exact_matches": sum(
                _safe_int(
                    self.native_entity_templates[target_id].get(
                        "runtime_boss_type", -1
                    ),
                    -1,
                )
                == 3
                for target_id in native_matches
            ),
            "native_catalog_exact_matches": sum(
                bool(
                    self.native_entity_templates[target_id].get(
                        "catalog_match"
                    )
                )
                for target_id in native_matches
            ),
            "parser_profile_exact_matches": sum(
                target_id in self.parser.entity_profiles
                for target_id in target_ids
            ),
            "confirmed_boss_exact_matches": sum(
                target_id in self.parser.confirmed_boss_entities
                for target_id in target_ids
            ),
            "damage_dummy_exact_matches": sum(
                _safe_int(self.parser.entity_template_ids.get(target_id, 0))
                in DAMAGE_TARGET_TEMPLATE_IDS
                for target_id in target_ids
            ),
        }

    def _native_diagnostic_int(self, key: str) -> int:
        return max(0, _safe_int(self.native_diagnostic.get(key)))

    def _target_lookup_failure_assessment(self) -> dict[str, str] | None:
        """Explain the exact target lookup stage when its switch was active."""

        if not bool(self.native_diagnostic.get("target_boss_lookup_enabled")):
            return None
        candidates = self._native_diagnostic_int("target_lookup_candidates")
        scans = self._native_diagnostic_int("target_lookup_object_scans")
        resolved = self._native_diagnostic_int("target_lookup_resolved")
        timeouts = self._native_diagnostic_int("target_lookup_timeouts")
        trusted_classes = self._native_diagnostic_int(
            "target_lookup_trusted_classes"
        )
        table_failures = self._native_diagnostic_int(
            "target_lookup_object_table_failures"
        )
        slots_scanned = self._native_diagnostic_int(
            "target_lookup_object_slots_scanned"
        )
        class_candidates = self._native_diagnostic_int(
            "target_lookup_object_class_candidates"
        )
        exact_entities = self._native_diagnostic_int(
            "target_lookup_object_exact_entity_matches"
        )
        exact_template_zero = self._native_diagnostic_int(
            "target_lookup_object_exact_template_zero"
        ) + self._native_diagnostic_int(
            "target_lookup_observed_target_template_zero"
        )
        exact_template_nonzero = self._native_diagnostic_int(
            "target_lookup_object_exact_template_nonzero"
        ) + self._native_diagnostic_int(
            "target_lookup_observed_target_template_nonzero"
        )

        if candidates <= 0:
            skipped_local = self._native_diagnostic_int(
                "target_lookup_skipped_local_player"
            )
            skipped_unplausible = self._native_diagnostic_int(
                "target_lookup_skipped_unplausible"
            )
            if skipped_local > 0:
                return {
                    "code": "target_lookup_target_matches_local_player",
                    "confidence": "high",
                    "stage": "target_lookup_candidate",
                    "summary": "伤害目标被原生层判定为本机角色，因此没有进入 Boss 反查队列。",
                }
            if skipped_unplausible > 0:
                return {
                    "code": "target_lookup_target_id_range_unsupported",
                    "confidence": "high",
                    "stage": "target_lookup_candidate",
                    "summary": "伤害目标不在当前支持的实体 ID 范围内，因此没有进入 Boss 反查队列。",
                }
            return {
                "code": "target_lookup_not_triggered",
                "confidence": "high",
                "stage": "target_lookup_candidate",
                "summary": "目标反查已开启，但伤害目标没有进入精确反查候选队列。",
            }
        if scans <= 0 and trusted_classes <= 0:
            return {
                "code": "target_lookup_no_common_component_class",
                "confidence": "high",
                "stage": "target_lookup_component_class",
                "summary": "目标反查已触发，但没有取得可验证的 CommonComponent 类型。",
            }
        if table_failures > 0 and slots_scanned <= 0:
            return {
                "code": "target_lookup_object_table_unreadable",
                "confidence": "high",
                "stage": "target_lookup_object_table",
                "summary": "目标反查已触发，但该客户端的游戏对象表无法按当前布局读取。",
            }
        if scans > 0 and slots_scanned > 0 and class_candidates <= 0:
            return {
                "code": "target_lookup_common_component_not_indexed",
                "confidence": "high",
                "stage": "target_lookup_class_filter",
                "summary": "游戏对象表可以读取，但其中没有匹配已验证类型的 CommonComponent。",
            }
        if exact_template_zero > 0 and exact_template_nonzero <= 0:
            return {
                "code": "target_lookup_exact_component_template_zero",
                "confidence": "high",
                "stage": "target_lookup_template",
                "summary": "已精确找到伤害目标对应组件，但该组件中的模板编号仍为 0。",
            }
        if exact_entities <= 0 and timeouts > 0:
            return {
                "code": "target_lookup_exact_component_not_found",
                "confidence": "high",
                "stage": "target_lookup_entity_join",
                "summary": "对象表和组件类型均可读取，但没有组件实体 ID 与伤害 target_id 精确相等。",
            }
        if exact_template_nonzero > 0 and resolved <= 0:
            return {
                "code": "target_lookup_exact_template_not_emitted",
                "confidence": "high",
                "stage": "target_lookup_emit",
                "summary": "已精确读到非零模板编号，但反查结果没有进入解析器。",
            }
        if resolved > 0:
            return {
                "code": "target_lookup_resolved_but_boss_rejected",
                "confidence": "high",
                "stage": "boss_catalog_gate",
                "summary": "目标反查已成功读取模板，但该模板仍未通过主程序 Boss 目录门槛。",
            }
        return {
            "code": "target_lookup_unresolved",
            "confidence": "medium",
            "stage": "target_lookup",
            "summary": "目标反查已执行，但本次报告尚未形成可用的精确目标模板。",
        }

    def _pipeline_checks(self) -> dict[str, dict[str, object]]:
        """Build an anonymous, stage-by-stage view of the capture path."""

        connected = bool(self.connected_payload)
        native_installed = bool(
            self.connected_payload.get("native_damage_hook_installed")
        )
        template_hooks = sum(
            bool(self.connected_payload.get(key))
            for key in (
                "native_boss_type_hook_installed",
                "native_boss_init_hook_installed",
                "native_template_id_hook_installed",
                "native_template_bulk_hook_installed",
            )
        )
        positive_damage = max(
            self.network_damage_positive,
            self.native_positive_damage_records,
        )
        displayable_damage = (
            self._confirmed_boss_events() + self._damage_dummy_events()
        )
        identity_links = self._target_identity_links()
        exact_identity_matches = max(
            identity_links.get("native_template_exact_matches", 0),
            identity_links.get("parser_profile_exact_matches", 0),
            identity_links.get("confirmed_boss_exact_matches", 0),
            identity_links.get("damage_dummy_exact_matches", 0),
        )
        lookup_enabled = bool(
            self.native_diagnostic.get("target_boss_lookup_enabled")
        )
        lookup_candidates = self._native_diagnostic_int(
            "target_lookup_candidates"
        )
        lookup_resolved = self._native_diagnostic_int("target_lookup_resolved")
        lookup_timeouts = self._native_diagnostic_int("target_lookup_timeouts")

        if (
            self.network_damage_positive > 0
            and self.native_positive_damage_records == 0
        ):
            fallback_status = (
                "passed"
                if self.parsed_damage_events > 0
                and self.network_damage_suppressed_by_native == 0
                else "blocked"
            )
        elif self.network_damage_suppressed_by_native > 0:
            fallback_status = "deduplicated"
        else:
            fallback_status = "not_needed"

        return {
            "connection": {
                "status": "passed" if connected else "failed",
            },
            "network_capture": {
                "status": "passed" if self.network_records > 0 else "silent",
                "records": self.network_records,
            },
            "native_damage_capture": {
                "status": (
                    "passed"
                    if self.native_positive_damage_records > 0
                    else "silent" if native_installed else "unavailable"
                ),
                "records": self.native_damage_records,
            },
            "network_fallback": {
                "status": fallback_status,
                "positive_records": self.network_damage_positive,
            },
            "damage_decode": {
                "status": (
                    "passed"
                    if self.parsed_damage_events > 0
                    else "failed" if positive_damage > 0 else "not_observed"
                ),
                "events": self.parsed_damage_events,
            },
            "template_hooks": {
                "status": "available" if template_hooks > 0 else "unavailable",
                "installed": template_hooks,
                "records": self.native_boss_records,
                "records_with_template_id": int(
                    self.native_template_stats.get("records_with_template_id", 0)
                ),
            },
            "target_lookup": {
                "status": (
                    "disabled"
                    if not lookup_enabled
                    else "passed"
                    if lookup_resolved > 0
                    else "timed_out"
                    if lookup_timeouts > 0
                    else "triggered"
                    if lookup_candidates > 0
                    else "not_triggered"
                ),
                "candidates": lookup_candidates,
                "resolved": lookup_resolved,
            },
            "target_identity": {
                "status": (
                    "passed"
                    if exact_identity_matches > 0
                    else "failed"
                    if self.parsed_damage_events > 0
                    else "not_observed"
                ),
                "exact_matches": exact_identity_matches,
            },
            "boss_display_gate": {
                "status": (
                    "passed"
                    if displayable_damage > 0
                    else "failed"
                    if self.parsed_damage_events > 0
                    else "not_observed"
                ),
                "events": displayable_damage,
            },
        }

    def handle(self, kind: str, payload: object = None) -> None:
        kind = str(kind or "")
        if kind == "process_started" and isinstance(payload, dict):
            self.process_payload = {
                "capture_process_pid": _safe_int(payload.get("pid")),
                "runtime_profile_id": str(
                    payload.get("runtime_profile_id", "")
                )[:64],
                "priority_class": str(payload.get("priority_class", ""))[:32],
                "priority_applied": _safe_bool(payload.get("priority_applied")),
                "main_thread_priority_applied": _safe_bool(
                    payload.get("main_thread_priority_applied")
                ),
            }
        elif kind == "state" and isinstance(payload, dict):
            self._stage(payload.get("stage"))
        elif kind == "connected" and isinstance(payload, dict):
            self._stage("capturing")
            keys = (
                "game_pid",
                "network_hook_adopted",
                "native_damage_hook_installed",
                "native_damage_hook_adopted",
                "native_name_hook_installed",
                "native_boss_type_hook_installed",
                "native_boss_init_hook_installed",
                "native_template_id_hook_installed",
                "native_template_bulk_hook_installed",
                "team_stats_hook_installed",
                "team_stats_mode",
                "damage_source",
            )
            self.connected_payload = {key: payload.get(key) for key in keys}
        elif kind == "batch" and isinstance(payload, dict):
            self._handle_batch(payload)
        elif kind in {"diagnostic", "capture_error", "cleanup_error", "fatal"}:
            component = kind
            if isinstance(payload, dict):
                component = payload.get("component") or payload.get("stage") or kind
            self._error(component, payload)
            if kind in {"capture_error", "fatal"}:
                self._stage(
                    payload.get("stage", kind) if isinstance(payload, dict) else kind
                )
        elif kind == "session_closed" and isinstance(payload, dict):
            self._stage(payload.get("reason", "session_closed"))
        elif kind == "stopped":
            self._stage("stopped")

    def assessment(self) -> dict[str, str]:
        connected = bool(self.connected_payload)
        native_installed = bool(
            self.connected_payload.get("native_damage_hook_installed")
        )
        if not connected:
            code = (
                "network_hook_failed"
                if any("network" in item["component"] for item in self.errors)
                else "game_not_connected"
            )
            return {
                "code": code,
                "confidence": "high" if self.errors else "medium",
                "stage": "connection",
                "summary": "未建立游戏采集连接，请查看钩子错误。",
            }
        if (
            native_installed
            and self.network_damage_positive > 0
            and self.native_positive_damage_records == 0
            and self.network_damage_suppressed_by_native > 0
        ):
            return {
                "code": "native_hook_silent_network_fallback_blocked",
                "confidence": "high",
                "stage": "native_damage_capture",
                "summary": "原生伤害入口已安装但没有产出，网络伤害回退同时被抑制。",
            }
        damage_dummy_events = self._damage_dummy_events()
        confirmed_boss_events = self._confirmed_boss_events()
        if damage_dummy_events > 0:
            return {
                "code": "damage_dummy_pipeline_ok",
                "confidence": "high",
                "stage": "damage_dummy_gate_passed",
                "summary": "伤害木桩的采集、目标关联与显示门槛均正常；本次结果仅验证公共读取链路，不等同于已验证具体 Boss 模板。",
            }
        if confirmed_boss_events > 0:
            return {
                "code": "capture_pipeline_ok",
                "confidence": "high",
                "stage": "target_gate_passed",
                "summary": "采集、解析与主程序 Boss 门槛均产生了可显示伤害。",
            }
        if self.parsed_damage_events > 0 and not self.parser.boss_template_catalog:
            return {
                "code": "boss_catalog_unavailable",
                "confidence": "high",
                "stage": "boss_catalog",
                "summary": "已解析到伤害，但检测工具未加载到 Boss 模板目录。",
            }
        if self.parsed_damage_events > 0:
            target_lookup_assessment = self._target_lookup_failure_assessment()
            if target_lookup_assessment is not None:
                return target_lookup_assessment
            return {
                "code": "damage_target_not_confirmed",
                "confidence": "high",
                "stage": "boss_gate",
                "summary": "已解析到伤害，但没有受击目标通过主程序 Boss 门槛。",
            }
        if self.network_damage_messages > 0 or self.native_damage_records > 0:
            return {
                "code": "damage_decode_or_identity_failed",
                "confidence": "medium",
                "stage": "damage_decode_or_identity",
                "summary": "捕获到伤害入口，但没有形成可显示的伤害事件。",
            }
        if self.network_records > 0:
            return {
                "code": "no_damage_observed",
                "confidence": "medium",
                "stage": "damage_message",
                "summary": "网络采集正常，但检测期间没有观察到伤害消息。",
            }
        return {
            "code": "network_hook_silent",
            "confidence": "high",
            "stage": "network_capture",
            "summary": "采集连接已建立，但没有收到任何可用网络消息。",
        }

    def finish(self, environment: dict[str, object]) -> dict[str, object]:
        self.finished_at = time.time()
        damage_targets = self._damage_target_summaries()
        assessment = self.assessment()
        assessment["recommended_fix"] = ASSESSMENT_RECOMMENDATIONS.get(
            assessment.get("code", ""),
            "根据 pipeline_checks 中第一个非通过阶段继续定位，不要仅依据管理员状态判断采集正常。",
        )
        methods = sorted(
            self.method_counts.items(), key=lambda item: (-item[1], item[0])
        )[:MAX_REPORTED_METHODS]
        connection = {
            key: (
                _safe_int(value)
                if key == "game_pid"
                else _safe_bool(value)
                if key.endswith("installed") or key.endswith("adopted")
                else str(value or "")[:32]
            )
            for key, value in self.connected_payload.items()
        }
        return {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "environment": dict(environment),
            "assessment": assessment,
            "capture": {
                "duration_seconds": round(
                    max(0.0, self.finished_at - self.started_at), 3
                ),
                "stages": list(self.stages),
                "process": dict(self.process_payload),
                "connection": connection,
                "capture_batches": self.capture_batches,
                "network_records": self.network_records,
                "decoded_network_records": self.decoded_network_records,
                "network_methods": [
                    {"method": method, "count": count}
                    for method, count in methods
                ],
                "network_damage_messages": self.network_damage_messages,
                "network_damage_positive": self.network_damage_positive,
                "network_damage_suppressed_by_native": (
                    self.network_damage_suppressed_by_native
                ),
                "native_damage_records": self.native_damage_records,
                "native_positive_damage_records": (
                    self.native_positive_damage_records
                ),
                "native_boss_records": self.native_boss_records,
                "native_name_records": self.native_name_records,
                "native_skill_name_records": self.native_skill_name_records,
                "monster_catalog_size": len(self.catalog),
                "boss_catalog_size": len(self.parser.boss_template_catalog),
                "native_template_stats": {
                    key: int(value)
                    for key, value in sorted(self.native_template_stats.items())
                },
                "native_template_candidates": self._native_template_summaries(),
                "native_diagnostic": dict(self.native_diagnostic),
                "native_diagnostic_snapshots": self.native_diagnostic_snapshots,
                "native_diagnostic_resets": self.native_diagnostic_resets,
                "target_identity_links": self._target_identity_links(),
                "pipeline_checks": self._pipeline_checks(),
                "damage_stage_counts": _sorted_counter(self.damage_stage_counts),
                "damage_gate_reasons": _sorted_counter(self.damage_gate_reasons),
                "sequence_gaps": self.sequence_gaps,
                "parser_updates": dict(self.parser_update_counts),
                "parsed_damage_events": self.parsed_damage_events,
                "forwarded_damage_events": self.forwarded_damage_events,
                "filtered_damage_events": self.filtered_damage_events,
                "boss_confirmed_damage_events": sum(
                    max(0, _safe_int(target.get("boss_mode_events")))
                    for target in damage_targets
                ),
                "confirmed_boss_damage_events": sum(
                    max(0, _safe_int(target.get("records")))
                    for target in damage_targets
                    if bool(target.get("confirmed_boss"))
                ),
                "damage_dummy_damage_events": sum(
                    max(0, _safe_int(target.get("records")))
                    for target in damage_targets
                    if bool(target.get("damage_dummy"))
                ),
                "damage_targets": damage_targets,
                "team_stat_updates": self.team_stat_updates,
                "team_status": dict(self.team_status),
                "bosses": list(self.bosses.values()),
                "errors": list(self.errors),
            },
            "privacy": {
                "raw_packets_uploaded": False,
                "card_number_uploaded": False,
                "player_names_uploaded": False,
                "local_paths_uploaded": False,
                "combat_entity_ids_uploaded": False,
                "monster_template_ids_uploaded": True,
            },
        }


def diagnostic_environment(*, elevated: bool, packaged: bool) -> dict[str, object]:
    windows_release, windows_version, _csd, _ptype = platform.win32_ver()
    get_encoding = getattr(locale, "getencoding", None)
    locale_encoding = (
        get_encoding()
        if callable(get_encoding)
        else locale.getpreferredencoding(False)
    )
    return {
        "platform": sys.platform,
        "windows_release": windows_release,
        "windows_version": windows_version or platform.version(),
        "machine": platform.machine(),
        "python_bits": platform.architecture()[0],
        "locale_encoding": locale_encoding,
        "cpu_count": int(os.cpu_count() or 0),
        "elevated": bool(elevated),
        "packaged": bool(packaged),
    }


def submit_diagnostic_report(
    server_url: str,
    client_id: str,
    diagnostics: dict[str, object],
    *,
    ca_bundle_path: Path | None = None,
    timeout: float = 12.0,
) -> DiagnosticSubmission:
    server_url = str(server_url).strip().rstrip("/")
    parsed = urlsplit(server_url)
    loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "::1",
        "localhost",
    }
    if parsed.scheme != "https" and not loopback_http:
        raise DiagnosticUploadError("检测服务器地址无效。")
    payload = json.dumps(
        {
            "tool_name": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "client_id": str(client_id).strip().lower()[:80],
            "diagnostics": diagnostics,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise DiagnosticUploadError("检测报告过大，无法上传。")
    request = Request(
        server_url + "/api/v1/dps/diagnostic",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"GMZZ-Diagnostic/{TOOL_VERSION}",
        },
        method="POST",
    )
    context = None
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        if ca_bundle_path is not None and ca_bundle_path.is_file():
            context.load_verify_locations(cafile=str(ca_bundle_path))
    raw = b""
    for attempt in range(2):
        try:
            options: dict[str, object] = {"timeout": max(2.0, float(timeout))}
            if context is not None:
                options["context"] = context
            with urlopen(request, **options) as response:
                raw = response.read(64 * 1024)
            break
        except HTTPError as exc:
            try:
                value = json.loads(exc.read(64 * 1024).decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError):
                value = {}
            finally:
                exc.close()
            message = str(value.get("message", "")).strip()
            if not message:
                message = (
                    "上传过于频繁，请稍后重试。"
                    if exc.code == 429
                    else f"服务器返回 HTTP {exc.code}。"
                )
            raise DiagnosticUploadError(message) from exc
        except (URLError, OSError, TimeoutError, socket.timeout) as exc:
            if attempt == 0:
                time.sleep(0.25)
                continue
            raise DiagnosticUploadError("无法上传检测报告，请检查网络后重试。") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise DiagnosticUploadError("检测服务器响应无效。") from exc
    diagnostic_id = str(value.get("diagnostic_id", "")).strip()
    if not value.get("ok") or not diagnostic_id.startswith("DG"):
        raise DiagnosticUploadError(
            str(value.get("message", "")).strip() or "检测报告上传失败。"
        )
    return DiagnosticSubmission(
        diagnostic_id=diagnostic_id[:32],
        message=str(value.get("message", "")).strip() or "检测报告已上传。",
    )
