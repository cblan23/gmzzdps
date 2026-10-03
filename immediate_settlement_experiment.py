#!/usr/bin/env python3
"""Local-only diagnostics for the one-shot wipe settlement experiment.

The production Npcap backend remains passive.  This module is inert unless
``immediate_settlement_experiment`` is enabled in the local configuration.
The legacy request implementation is imported lazily, only while arming one
explicit, zero-argument request after an independently observed wipe.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from combat_statistics import STATISTICS_MESSAGES, normalize_statistics
from network_state import (
    TEAM_STATISTICS_METHODS,
    direct_numeric_map,
    map_pairs,
)


EXPERIMENT_CONFIG_KEY = "immediate_settlement_experiment"
EXPERIMENT_MODE_CONFIG_KEY = "immediate_settlement_experiment_mode"
EXPERIMENT_MODE_OBSERVE = "observe"
EXPERIMENT_MODE_QUERY = "query"
EXPERIMENT_MODES = frozenset(
    {EXPERIMENT_MODE_OBSERVE, EXPERIMENT_MODE_QUERY}
)
IMMEDIATE_QUERY_DELAY_MS = 1_000
OBSERVE_ONLY_WINDOW_SECONDS = 10.0
QUERY_TRIGGER_TIMEOUT_SECONDS = 5.0
QUERY_RESPONSE_TIMEOUT_SECONDS = 8.0
ONE_SHOT_REQUEST_INTERVAL_SECONDS = 300.0
EXPERIMENT_RESPONSE_METHODS = frozenset(
    set(TEAM_STATISTICS_METHODS) | set(STATISTICS_MESSAGES)
)
OUTBOUND_SETTLEMENT_KEYWORDS = (
    "battle",
    "combat",
    "encounter",
    "stage",
    "dungeon",
    "fight",
    "reset",
    "finalize",
    "flush",
    "close",
    "create",
    "relive",
    "revive",
)
CAUSAL_OUTBOUND_CAPTURE_METHODS = (
    "ReqReportLockTarget",
    "ReqCastSkillNew",
)


def experiment_settings(
    config: object,
    *,
    frozen: bool,
) -> tuple[bool, str, bool, str]:
    """Return enabled, mode, query-enabled and an optional disabled reason."""

    value = config if isinstance(config, dict) else {}
    raw_enabled = value.get(EXPERIMENT_CONFIG_KEY, False)
    embedded_mode = ""
    if isinstance(raw_enabled, str):
        text = raw_enabled.strip().casefold()
        if text in EXPERIMENT_MODES:
            embedded_mode = text
            enabled = True
        else:
            enabled = text in {"1", "true", "yes", "on", "enabled"}
    else:
        enabled = bool(raw_enabled)
    raw_mode = value.get(
        EXPERIMENT_MODE_CONFIG_KEY,
        embedded_mode or EXPERIMENT_MODE_QUERY,
    )
    mode = str(raw_mode or EXPERIMENT_MODE_QUERY).strip().casefold()
    if mode not in EXPERIMENT_MODES:
        mode = EXPERIMENT_MODE_QUERY
    if not enabled:
        return False, mode, False, ""
    if frozen and mode == EXPERIMENT_MODE_QUERY:
        return True, mode, False, "FROZEN_BUILD_QUERY_DISABLED"
    return True, mode, mode == EXPERIMENT_MODE_QUERY, ""


def _iso_from_ns(value: object) -> str:
    try:
        timestamp_ns = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return ""
    if timestamp_ns <= 0:
        return ""
    try:
        return dt.datetime.fromtimestamp(
            timestamp_ns / 1_000_000_000,
            tz=dt.timezone.utc,
        ).astimezone().isoformat(timespec="milliseconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _receipt_ns(record: Mapping[str, object]) -> int:
    try:
        timestamp_ns = int(record.get("capture_timestamp_ns", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        timestamp_ns = 0
    if timestamp_ns > 0:
        return timestamp_ns
    try:
        filetime = int(record.get("filetime_100ns", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, (filetime - 116_444_736_000_000_000) * 100)


def _bounded_text(value: object, limit: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().replace("\x00", "")[:limit]


def _non_negative_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def is_settlement_outbound_candidate(record: object) -> bool:
    """Return whether an observed RPC can explain a settlement transition."""

    if not isinstance(record, dict):
        return False
    method = str(record.get("method", "") or "").strip().casefold()
    return bool(method) and (
        method in {value.casefold() for value in CAUSAL_OUTBOUND_CAPTURE_METHODS}
        or any(keyword in method for keyword in OUTBOUND_SETTLEMENT_KEYWORDS)
    )


def _hex_evidence(value: object, *, prefix_bytes: int = 32) -> dict[str, object]:
    """Return bounded comparison evidence for a captured hexadecimal blob."""

    if not isinstance(value, str):
        return {}
    compact = value.strip().casefold()
    if not compact or len(compact) % 2:
        return {}
    try:
        raw = bytes.fromhex(compact)
    except ValueError:
        return {}
    return {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "hex_prefix": raw[: max(0, int(prefix_bytes))].hex(),
    }


def _argument_descriptor_summary(record: Mapping[str, object]) -> list[dict[str, object]]:
    vector = record.get("argument_descriptor_vector")
    if not isinstance(vector, dict):
        return []
    items = vector.get("items")
    if not isinstance(items, list):
        return []
    result: list[dict[str, object]] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            continue
        item: dict[str, object] = {
            "index": index,
            "address": _non_negative_integer(raw_item.get("address")),
            "type_name": _bounded_text(raw_item.get("type_name"), 48),
            "lua_type": _non_negative_integer(raw_item.get("lua_type")),
        }
        evidence = _hex_evidence(raw_item.get("snapshot"))
        if evidence:
            item["snapshot"] = evidence
        result.append(item)
    return result


def summarize_outbound_rpc(record: object) -> dict[str, object]:
    """Keep correlation fields while the full raw record stays in network log."""

    if not isinstance(record, dict):
        return {}
    try:
        filetime_100ns = max(
            0, int(record.get("filetime_100ns", 0) or 0)
        )
    except (TypeError, ValueError, OverflowError):
        filetime_100ns = 0
    event_time_ns = max(
        0, (filetime_100ns - 116_444_736_000_000_000) * 100
    )
    argument_count = _non_negative_integer(
        record.get("variadic_argument_count")
    )
    summary = {
        "event_time_ns": event_time_ns,
        "event_time": (
            str(record.get("event_time", "") or "")
            or _iso_from_ns(event_time_ns)
        ),
        "direction": "C->S",
        "method": _bounded_text(record.get("method"), 128),
        "sequence": _non_negative_integer(record.get("sequence")),
        "script_entity": _non_negative_integer(record.get("script_entity")),
        "argument_count": argument_count,
        "argument_numbers": list(record.get("lua_argument_numbers", []) or []),
        "argument_indices": list(record.get("lua_argument_indices", []) or []),
        "argument_capture": _bounded_text(
            record.get("lua_argument_capture"), 64
        ),
        "argument_cells": list(record.get("lua_argument_cells", []) or []),
        "argument_snapshot": _hex_evidence(
            record.get("lua_argument_snapshot")
        ),
        "argument_descriptors": _argument_descriptor_summary(record),
        "argument_sync_state": _non_negative_integer(
            record.get("argument_sync_state")
        ),
        "return_address": _bounded_text(record.get("return_address"), 32),
        "variadic_qwords": list(record.get("variadic_qwords", []) or []),
        "entry_stack_cells": list(record.get("entry_stack_cells", []) or []),
        "context_snapshot": _hex_evidence(record.get("context_snapshot")),
        "variadic_snapshot": _hex_evidence(record.get("variadic_snapshot")),
        "variadic_storage_snapshot": _hex_evidence(
            record.get("variadic_storage_snapshot")
        ),
    }
    return summary


def summarize_statistics_record(
    record: object,
    *,
    context: object = None,
) -> list[dict[str, object]]:
    """Summarize an inbound statistics record without changing parser state."""

    if not isinstance(record, dict):
        return []
    method = str(record.get("method", ""))
    if method not in EXPERIMENT_RESPONSE_METHODS:
        return []
    received_at_ns = _receipt_ns(record)
    base: dict[str, object] = {
        "method": method,
        "direction": "S->C",
        "received_at_ns": received_at_ns,
        "received_at": _iso_from_ns(received_at_ns),
        "packet_size": int(record.get("npcap_message_bytes", 0) or 0),
    }
    capture_context = context if isinstance(context, dict) else {}
    if method in STATISTICS_MESSAGES:
        try:
            snapshots = normalize_statistics(
                record,
                instance_id=(
                    str(capture_context.get("instance_id") or "") or None
                ),
                dungeon_id=_non_negative_integer(
                    capture_context.get("dungeon_id")
                ),
                map_id=_non_negative_integer(capture_context.get("map_id")),
            )
        except (KeyError, TypeError, ValueError) as error:
            return [
                {
                    **base,
                    "valid": False,
                    "failure": (
                        "EMPTY_MEMBERS"
                        if "Empty or oversized server roster" in str(error)
                        else "INVALID_STATISTICS"
                    ),
                    "error_type": type(error).__name__,
                }
            ]
        return [
            {
                **base,
                "valid": True,
                "matcher_compatible": snapshot.scope == "STAGE",
                "scope": snapshot.scope,
                "battle_id": snapshot.battle_id,
                "associated_battle_id": snapshot.associated_battle_id,
                "stage_id": snapshot.stage_id,
                "stage_index": snapshot.stage_index,
                "is_stage_success": snapshot.is_stage_success,
                "member_count": len(snapshot.members),
                "members": [
                    {
                        "id": member.id,
                        "iid": member.iid,
                        "name": member.name,
                        "damage": member.damage,
                    }
                    for member in snapshot.members
                ],
            }
            for snapshot in snapshots
        ]

    args = record.get("decoded_arguments")
    if not isinstance(args, list) or not args:
        return [
            {
                **base,
                "valid": False,
                "failure": "EMPTY_MEMBERS",
                "matcher_compatible": False,
                "member_count": 0,
                "members": [],
            }
        ]
    entries = map_pairs(args[0])
    members: list[dict[str, object]] = []
    for raw_token, raw_fields in entries:
        fields = direct_numeric_map(raw_fields)
        token = _bounded_text(raw_token)
        if not token:
            continue
        # Common team snapshots use field 5 for cumulative damage.  An omitted
        # value in a full snapshot means an explicit zero in the existing
        # parser contract; Dirty snapshots retain it as unknown.
        damage = _non_negative_integer(fields.get(5))
        if damage is None and method == "RetCommonCombatStatisticsByTeam":
            damage = 0
        members.append(
            {
                "id": token,
                "iid": _non_negative_integer(fields.get(0)),
                "name": _bounded_text(fields.get(4), 48) or None,
                "damage": damage,
            }
        )
    return [
        {
            **base,
            "valid": bool(members),
            "failure": "" if members else "EMPTY_MEMBERS",
            "matcher_compatible": False,
            "scope": "TEAM_COUNTER",
            "battle_id": None,
            "associated_battle_id": None,
            "stage_id": None,
            "stage_index": None,
            "is_stage_success": None,
            "member_count": len(members),
            "members": members,
        }
    ]


class ExperimentLog:
    """Small append-only human-readable log with JSON fields per line."""

    def __init__(self, directory: Path, *, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.path: Path | None = None
        self._lock = threading.Lock()
        if not self.enabled:
            return
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / time.strftime(
            "immediate_settlement_%Y%m%d_%H%M%S.log"
        )

    def write(self, channel: str, message: str, **fields: object) -> None:
        if not self.enabled or self.path is None:
            return
        now_ns = time.time_ns()
        payload = {
            "logged_at_ns": now_ns,
            "logged_at": _iso_from_ns(now_ns),
            **fields,
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        line = f"{payload['logged_at']} [{channel}] {message} {serialized}\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)


@dataclass
class SettlementAttempt:
    encounter_id: str
    result: str
    ended_at_ns: int
    expected_battle_id: str | None
    expected_tokens: tuple[str, ...]
    query_planned: bool
    created_monotonic: float = field(default_factory=time.monotonic)
    requested_at_ns: int = 0
    triggered_at_ns: int = 0
    response_deadline: float = 0.0
    response_methods: list[str] = field(default_factory=list)
    failures: set[str] = field(default_factory=set)
    matched: bool = False
    complete: bool = False
    final: bool = False


class SettlementExperimentCoordinator:
    """Correlate one query with the unchanged SettlementMatcher results."""

    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool,
        mode: str,
        query_enabled: bool,
        disabled_reason: str = "",
    ) -> None:
        self.enabled = bool(enabled)
        self.mode = mode if mode in EXPERIMENT_MODES else EXPERIMENT_MODE_QUERY
        self.query_enabled = bool(query_enabled)
        self.disabled_reason = str(disabled_reason or "")
        self.log = ExperimentLog(directory, enabled=self.enabled)
        self.attempts: dict[str, SettlementAttempt] = {}
        self.seen_encounters: set[str] = set()
        if self.enabled:
            self.log.write(
                "Experiment",
                "started",
                mode=self.mode,
                query_enabled=self.query_enabled,
                disabled_reason=self.disabled_reason,
            )

    @property
    def log_path(self) -> Path | None:
        return self.log.path

    def lifecycle(self, message: str, **fields: object) -> None:
        """Record capture/application state without changing experiment state."""

        if not self.enabled:
            return
        self.log.write("Lifecycle", str(message or "state"), **fields)

    @staticmethod
    def _encounter_fields(encounter: object) -> dict[str, object]:
        roster = list(getattr(encounter, "participants_snapshot", ()) or ())
        return {
            "local_encounter_id": str(
                getattr(encounter, "local_encounter_id", "") or ""
            ),
            "instance_id": str(getattr(encounter, "instance_id", "") or ""),
            "encounter_started_at_ns": int(
                getattr(encounter, "started_at_ns", 0) or 0
            ),
            "encounter_started_at": _iso_from_ns(
                getattr(encounter, "started_at_ns", 0)
            ),
            "encounter_ended_at_ns": int(
                getattr(encounter, "ended_at_ns", 0) or 0
            ),
            "encounter_ended_at": _iso_from_ns(
                getattr(encounter, "ended_at_ns", 0)
            ),
            "result": str(getattr(encounter, "result", "") or ""),
            "settlement_status": str(
                getattr(encounter, "settlement_status", "") or ""
            ),
            "known_battle_id": (
                str(getattr(encounter, "server_battle_id", "") or "") or None
            ),
            "stage_id": getattr(encounter, "stage_id", None),
            "stage_index": getattr(encounter, "stage_index", None),
            "members": roster,
            "member_count": len(roster),
        }

    def encounter_started(self, encounter: object) -> None:
        if not self.enabled:
            return
        encounter_id = str(
            getattr(encounter, "local_encounter_id", "") or ""
        )
        if not encounter_id or encounter_id in self.seen_encounters:
            return
        self.seen_encounters.add(encounter_id)
        self.log.write(
            "Encounter",
            "START",
            **self._encounter_fields(encounter),
        )

    def encounter_ended(self, encounter: object) -> bool:
        """Record an end and return whether a one-second query is scheduled."""

        if not self.enabled:
            return False
        fields = self._encounter_fields(encounter)
        encounter_id = str(fields["local_encounter_id"])
        self.log.write("Encounter", "END", **fields)
        if fields["result"] != "WIPE" or not encounter_id:
            return False
        if encounter_id in self.attempts:
            return False
        expected_tokens = tuple(
            sorted(
                str(row.get("id") or "")
                for row in fields["members"]
                if isinstance(row, dict) and row.get("id")
            )
        )
        attempt = SettlementAttempt(
            encounter_id=encounter_id,
            result="WIPE",
            ended_at_ns=int(fields["encounter_ended_at_ns"] or 0),
            expected_battle_id=fields["known_battle_id"],
            expected_tokens=expected_tokens,
            query_planned=self.query_enabled,
        )
        if self.query_enabled:
            self.log.write(
                "ImmediateSettlement",
                f"waiting {IMMEDIATE_QUERY_DELAY_MS}ms",
                local_encounter_id=encounter_id,
            )
        else:
            attempt.response_deadline = (
                time.monotonic() + OBSERVE_ONLY_WINDOW_SECONDS
            )
            self.log.write(
                "ImmediateSettlement",
                "observe-only; no query will be sent",
                local_encounter_id=encounter_id,
                observe_seconds=OBSERVE_ONLY_WINDOW_SECONDS,
                disabled_reason=self.disabled_reason,
            )
        self.attempts[encounter_id] = attempt
        return self.query_enabled

    def query_submitted(self, encounter_id: str) -> bool:
        attempt = self.attempts.get(str(encounter_id))
        if attempt is None or attempt.final or not attempt.query_planned:
            return False
        if attempt.requested_at_ns:
            return False
        attempt.requested_at_ns = time.time_ns()
        self.log.write(
            "ImmediateSettlement",
            "query submitted to one-shot request chain",
            local_encounter_id=attempt.encounter_id,
            request_submit_time_ns=attempt.requested_at_ns,
            request_submit_time=_iso_from_ns(attempt.requested_at_ns),
        )
        return True

    def skip_query(self, encounter_id: str, reason: str) -> None:
        attempt = self.attempts.get(str(encounter_id))
        if attempt is None or attempt.final:
            return
        attempt.final = True
        attempt.failures.add(str(reason))
        self.log.write(
            "ImmediateSettlement",
            f"query skipped: {reason}",
            local_encounter_id=attempt.encounter_id,
            outcome=str(reason),
        )

    def query_event(self, payload: object) -> None:
        if not self.enabled or not isinstance(payload, dict):
            return
        encounter_id = str(payload.get("local_encounter_id", "") or "")
        attempt = self.attempts.get(encounter_id)
        if attempt is None:
            self.log.write(
                "ImmediateSettlement",
                "orphan request event",
                **payload,
            )
            return
        event = str(payload.get("event", "") or "")
        if event == "triggered":
            attempt.triggered_at_ns = int(
                payload.get("request_trigger_time_ns", 0) or time.time_ns()
            )
            attempt.response_deadline = (
                time.monotonic() + QUERY_RESPONSE_TIMEOUT_SECONDS
            )
            self.log.write(
                "ImmediateSettlement",
                "query triggered",
                **payload,
            )
            return
        if event in {"installing", "armed"}:
            self.log.write(
                "ImmediateSettlement",
                f"query {event}",
                **payload,
            )
            return
        failure = str(payload.get("failure", "") or event or "QUERY_ERROR")
        attempt.failures.add(failure)
        attempt.final = True
        self.log.write(
            "ImmediateSettlement",
            f"query failed: {failure}",
            **payload,
        )
        self.log.write(
            "Result",
            failure,
            local_encounter_id=encounter_id,
            outcome=failure,
            pending_preserved=True,
        )

    def statistics_received(self, summaries: object) -> None:
        if not self.enabled:
            return
        rows = summaries if isinstance(summaries, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            self.log.write("Statistics", "received", **row)
            received_at_ns = int(row.get("received_at_ns", 0) or 0)
            for attempt in self.attempts.values():
                if attempt.final or received_at_ns < attempt.ended_at_ns:
                    continue
                if (
                    attempt.query_planned
                    and attempt.triggered_at_ns
                    and received_at_ns < attempt.triggered_at_ns
                ):
                    continue
                method = str(row.get("method", "") or "")
                if method:
                    attempt.response_methods.append(method)
                member_count = int(row.get("member_count", 0) or 0)
                if not row.get("valid") or member_count == 0:
                    attempt.failures.add("EMPTY_MEMBERS")
                elif len(attempt.expected_tokens) > 1 and member_count == 1:
                    attempt.failures.add("SELF_ONLY")
                battle_id = str(row.get("battle_id", "") or "")
                if (
                    attempt.expected_battle_id
                    and battle_id
                    and battle_id != attempt.expected_battle_id
                ):
                    attempt.failures.add("WRONG_BATTLE_ID")
                if not row.get("matcher_compatible"):
                    attempt.failures.add("NO_CONFIDENT_MATCH")

    def matcher_events(
        self,
        events: object,
        *,
        encounters: Mapping[str, object],
    ) -> None:
        if not self.enabled:
            return
        for event in events if isinstance(events, list) else ():
            if not isinstance(event, dict):
                continue
            self.log.write(
                "Matcher",
                (
                    f"matched encounter={event.get('encounter_id')} "
                    f"confidence={str(event.get('confidence', '')).upper()}"
                    if event.get("encounter_id")
                    else f"unmatched reason={event.get('reason')}"
                ),
                **event,
            )
            encounter_id = str(event.get("encounter_id", "") or "")
            received_at_ns = int(event.get("received_at_ns", 0) or 0)
            candidates = (
                [self.attempts[encounter_id]]
                if encounter_id in self.attempts
                else [
                    attempt
                    for attempt in self.attempts.values()
                    if not attempt.final
                    and received_at_ns >= attempt.ended_at_ns
                ]
            )
            for attempt in candidates:
                if attempt.final:
                    continue
                if str(event.get("confidence", "")).casefold() != "high":
                    attempt.failures.add("NO_CONFIDENT_MATCH")
                    continue
                if encounter_id != attempt.encounter_id:
                    attempt.failures.add("WRONG_BATTLE_ID")
                    continue
                encounter = encounters.get(encounter_id)
                after = str(event.get("settlement_status_after", "") or "")
                complete = bool(event.get("complete", False))
                if encounter is not None:
                    after = str(
                        getattr(encounter, "settlement_status", after) or after
                    )
                attempt.matched = True
                attempt.complete = complete and after == "SETTLED"
                if not attempt.complete:
                    attempt.failures.add("INCOMPLETE_DAMAGE")
                    continue
                attempt.final = True
                self.log.write(
                    "Settlement",
                    "PENDING -> SETTLED",
                    local_encounter_id=encounter_id,
                    confidence="HIGH",
                    match_reason=event.get("reason"),
                    member_count=event.get("member_count"),
                    members=event.get("members"),
                    request_return_time_ns=received_at_ns,
                    request_return_time=_iso_from_ns(received_at_ns),
                )

    def poll(self, encounters: Mapping[str, object]) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        for attempt in self.attempts.values():
            if attempt.final or not attempt.response_deadline:
                continue
            encounter = encounters.get(attempt.encounter_id)
            if (
                encounter is not None
                and str(getattr(encounter, "settlement_status", ""))
                == "SETTLED"
            ):
                # The exact Matcher transition should normally have finalized
                # the attempt.  Preserve the success but make a missing
                # diagnostic event explicit instead of issuing another query.
                attempt.final = True
                attempt.matched = True
                attempt.complete = True
                self.log.write(
                    "Settlement",
                    "PENDING -> SETTLED",
                    local_encounter_id=attempt.encounter_id,
                    confidence=str(
                        getattr(encounter, "match_confidence", "") or ""
                    ).upper(),
                    match_reason="tracker_state_observed",
                )
                continue
            if now < attempt.response_deadline:
                continue
            if not attempt.response_methods:
                outcome = "NO_RESPONSE"
            elif "EMPTY_MEMBERS" in attempt.failures:
                outcome = "EMPTY_MEMBERS"
            elif "SELF_ONLY" in attempt.failures:
                outcome = "SELF_ONLY"
            elif "WRONG_BATTLE_ID" in attempt.failures:
                outcome = "WRONG_BATTLE_ID"
            elif "INCOMPLETE_DAMAGE" in attempt.failures:
                outcome = "INCOMPLETE_DAMAGE"
            else:
                outcome = "NO_CONFIDENT_MATCH"
            attempt.final = True
            self.log.write(
                "Result",
                outcome,
                local_encounter_id=attempt.encounter_id,
                query_planned=attempt.query_planned,
                query_triggered=bool(attempt.triggered_at_ns),
                response_methods=attempt.response_methods,
                failures=sorted(attempt.failures),
                pending_preserved=True,
            )


class PassiveOutboundRpcObserver:
    """Observe natural ``call_server`` traffic without issuing any request."""

    RETRY_SECONDS = 2.0
    ATTACHMENT_CHECK_SECONDS = 1.0

    def __init__(
        self,
        profile: Mapping[str, object],
        emit: Callable[[dict[str, object]], None],
    ) -> None:
        self.profile = profile
        self.emit = emit
        self.hook = None
        self.game_pid = 0
        self.next_install_at = 0.0
        self.next_attachment_check_at = 0.0

    def _event(self, event: str, **fields: object) -> None:
        self.emit(
            {
                "event": event,
                "event_time_ns": time.time_ns(),
                **fields,
            }
        )

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        text = str(error).strip().splitlines()[0] if str(error).strip() else ""
        return f"{type(error).__name__}: {text[:240]}"

    def _close_hook(self, *, reason: str) -> None:
        hook, self.hook = self.hook, None
        previous_pid, self.game_pid = self.game_pid, 0
        self.next_attachment_check_at = 0.0
        if hook is None:
            return
        try:
            hook.set_enabled(False)
            hook.close()
            self._event(
                "outbound_observer_closed",
                game_pid=previous_pid,
                reason=reason,
            )
        except BaseException as error:
            self._event(
                "outbound_observer_cleanup_error",
                game_pid=previous_pid,
                reason=reason,
                details=self._safe_error(error),
            )

    def sync(self, *, game_pid: int) -> bool:
        """Attach once to the current game and keep the injection path disabled."""

        requested_pid = int(game_pid or 0)
        if requested_pid <= 0:
            if self.hook is not None:
                self._close_hook(reason="game_disconnected")
            return False
        if self.hook is not None and requested_pid != self.game_pid:
            self._close_hook(reason="game_pid_changed")
        now = time.monotonic()
        if self.hook is not None:
            if now < self.next_attachment_check_at:
                return True
            self.next_attachment_check_at = now + self.ATTACHMENT_CHECK_SECONDS
            try:
                attached = bool(self.hook.alive and self.hook.is_attached())
            except BaseException:
                attached = False
            if attached:
                return True
            self._close_hook(reason="entry_detached")
            self.next_install_at = now + self.RETRY_SECONDS
            return False
        if now < self.next_install_at:
            return False
        self.next_install_at = now + self.RETRY_SECONDS
        self._event("outbound_observer_installing", game_pid=requested_pid)
        hook = None
        try:
            from team_stats_request_hook import TeamStatsRequestHook

            hook = TeamStatsRequestHook(
                profile=self.profile,
                pid=requested_pid,
                interval=ONE_SHOT_REQUEST_INTERVAL_SECONDS,
                enabled=False,
                synchronized_methods=CAUSAL_OUTBOUND_CAPTURE_METHODS,
                takeover_existing=False,
                stable_primary_only=False,
                allow_existing_adoption=False,
            ).install()
            status = hook.status()
            if bool(status.get("enabled", False)):
                hook.set_enabled(False)
                raise RuntimeError("observer request branch did not stay disabled")
            self.hook = hook
            self.game_pid = requested_pid
            self.next_attachment_check_at = (
                now + self.ATTACHMENT_CHECK_SECONDS
            )
            self._event(
                "outbound_observer_installed",
                game_pid=requested_pid,
                request_injection_enabled=False,
            )
            return True
        except BaseException as error:
            try:
                if hook is not None:
                    hook.close()
            except BaseException:
                pass
            self._event(
                "outbound_observer_install_error",
                game_pid=requested_pid,
                details=self._safe_error(error),
            )
            return False

    def poll(self) -> list[dict[str, object]]:
        if self.hook is None:
            return []
        try:
            records = self.hook.poll_requests()
        except BaseException as error:
            self._event(
                "outbound_observer_poll_error",
                game_pid=self.game_pid,
                details=self._safe_error(error),
            )
            self._close_hook(reason="poll_error")
            self.next_install_at = time.monotonic() + self.RETRY_SECONDS
            return []
        for record in records:
            if is_settlement_outbound_candidate(record):
                self._event(
                    "outbound_rpc",
                    **summarize_outbound_rpc(record),
                )
        return records

    def close(self) -> None:
        self._close_hook(reason="observer_shutdown")


class OneShotTeamStatsHookDriver:
    """Arm exactly one existing zero-argument team-stat request opportunity."""

    def __init__(
        self,
        profile: Mapping[str, object],
        emit: Callable[[dict[str, object]], None],
    ) -> None:
        self.profile = profile
        self.emit = emit
        self.hook = None
        self.active: dict[str, object] | None = None
        self.baseline_request_count = 0
        self.deadline = 0.0
        self.attempted_encounters: set[str] = set()

    def _event(self, event: str, **fields: object) -> None:
        payload = {
            "event": event,
            "event_time_ns": time.time_ns(),
            **fields,
        }
        self.emit(payload)

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        text = str(error).strip().splitlines()[0] if str(error).strip() else ""
        return f"{type(error).__name__}: {text[:240]}"

    def arm(self, command: object, *, game_pid: int) -> bool:
        if not isinstance(command, dict):
            return False
        encounter_id = str(command.get("local_encounter_id", "") or "")
        if not encounter_id or encounter_id in self.attempted_encounters:
            self._event(
                "rejected",
                local_encounter_id=encounter_id,
                failure="DUPLICATE_ATTEMPT",
            )
            return False
        self.attempted_encounters.add(encounter_id)
        if self.active is not None:
            self._event(
                "rejected",
                local_encounter_id=encounter_id,
                failure="QUERY_BUSY",
            )
            return False
        if not game_pid:
            self._event(
                "rejected",
                local_encounter_id=encounter_id,
                failure="GAME_NOT_CONNECTED",
            )
            return False
        self._event(
            "installing",
            local_encounter_id=encounter_id,
            game_pid=int(game_pid),
        )
        try:
            # Deliberately lazy: importing this module is itself proof that the
            # explicit local experiment was enabled.  Normal v0.3.0 startup
            # never imports or installs the legacy request hook.
            from team_stats_request_hook import TeamStatsRequestHook

            hook = TeamStatsRequestHook(
                profile=self.profile,
                pid=int(game_pid),
                interval=ONE_SHOT_REQUEST_INTERVAL_SECONDS,
                enabled=False,
                takeover_existing=False,
                stable_primary_only=True,
                allow_existing_adoption=False,
            ).install()
            self.hook = hook
            self.baseline_request_count = hook.arm_one_shot()
            self.active = dict(command)
            self.deadline = time.monotonic() + QUERY_TRIGGER_TIMEOUT_SECONDS
            self._event(
                "armed",
                local_encounter_id=encounter_id,
                baseline_request_count=self.baseline_request_count,
                trigger_timeout_seconds=QUERY_TRIGGER_TIMEOUT_SECONDS,
            )
            return True
        except BaseException as error:
            self._close_hook(encounter_id=encounter_id)
            self._event(
                "error",
                local_encounter_id=encounter_id,
                failure="QUERY_HOOK_UNAVAILABLE",
                details=self._safe_error(error),
            )
            return False

    def _close_hook(self, *, encounter_id: str) -> None:
        hook, self.hook = self.hook, None
        if hook is None:
            return
        try:
            hook.set_enabled(False)
            hook.close()
        except BaseException as error:
            self._event(
                "cleanup_error",
                local_encounter_id=encounter_id,
                failure="QUERY_HOOK_CLEANUP_FAILED",
                details=self._safe_error(error),
            )

    def poll(self) -> None:
        if self.active is None or self.hook is None:
            return
        encounter_id = str(self.active.get("local_encounter_id", "") or "")
        try:
            status = self.hook.status()
            request_count = int(status.get("request_count", 0) or 0)
        except BaseException as error:
            self._close_hook(encounter_id=encounter_id)
            self.active = None
            self.deadline = 0.0
            self._event(
                "error",
                local_encounter_id=encounter_id,
                failure="QUERY_STATUS_UNAVAILABLE",
                details=self._safe_error(error),
            )
            return
        if request_count > self.baseline_request_count:
            return_observed_ns = time.time_ns()
            payload = {
                "local_encounter_id": encounter_id,
                "request_trigger_time_ns": return_observed_ns,
                "request_trigger_time": _iso_from_ns(return_observed_ns),
                "request_return_observed_time_ns": return_observed_ns,
                "request_return_observed_time": _iso_from_ns(
                    return_observed_ns
                ),
                "request_count_before": self.baseline_request_count,
                "request_count_after": request_count,
                "request_filetime_100ns": int(
                    status.get("last_request_filetime", 0) or 0
                ),
                "request_call_result": int(
                    status.get("last_result", 0) or 0
                ),
                "request_method": "ReqCommonCombatStatisticsByTeam",
            }
            self._close_hook(encounter_id=encounter_id)
            self.active = None
            self.deadline = 0.0
            self._event("triggered", **payload)
            return
        if time.monotonic() < self.deadline:
            return
        self._close_hook(encounter_id=encounter_id)
        self.active = None
        self.deadline = 0.0
        self._event(
            "timeout",
            local_encounter_id=encounter_id,
            failure="REQUEST_NOT_TRIGGERED",
        )

    def close(self) -> None:
        encounter_id = str(
            (self.active or {}).get("local_encounter_id", "") or ""
        )
        self._close_hook(encounter_id=encounter_id)
        self.active = None
        self.deadline = 0.0
