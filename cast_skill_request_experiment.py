#!/usr/bin/env python3
"""Opt-in, local-only preparation for one natural cast-request replay chain.

This module is deliberately disconnected from the v0.3.0 settlement path.
It observes the existing ``ScriptEntity::call_server`` entry, accepts only the
confirmed primitive-only eight-argument ``ReqCastSkillNew`` schema, and can
arm one captured two-step cast chain when the operator supplies the explicit
``ACTIVE-ONCE`` confirmation. It never constructs a damage message; damage
acceptance and all resulting callbacks remain server decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from runtime_capability import load_runtime_profile_file
from team_stats_request_hook import (
    PRIMITIVE_REPLAY_METHOD,
    PRIMITIVE_REPLAY_SCHEMA,
    TeamStatsRequestHook,
)


ACTIVE_EXPERIMENT_CONFIRMATION = "ACTIVE-ONCE"
ACTIVE_EXPERIMENT_CONFIG_KEY = "cast_skill_request_experiment"
CAST_ARGUMENT_NAMES = (
    "skill_id",
    "target_entity_id",
    "target_x",
    "target_y",
    "target_z",
    "cast_parameter_6",
    "cast_parameter_7",
    "cast_instance_id",
)
CAST_ARGUMENT_TYPES = tuple(name for name, _lua_type in PRIMITIVE_REPLAY_SCHEMA)
CAPTURE_METHODS = ("ReqReportLockTarget", PRIMITIVE_REPLAY_METHOD)
DEFAULT_CHAIN_SKILLS = (86_020_010, 86_020_020)
MAX_NATURAL_CHAIN_GAP_SECONDS = 2.0
MIN_NATURAL_CHAIN_GAP_SECONDS = 0.02
REPLAY_EDGE_TIMEOUT_SECONDS = 5.0


def active_experiment_enabled(config: object, *, frozen: bool = False) -> bool:
    """Return false unless a local development config explicitly opts in."""

    if frozen or not isinstance(config, Mapping):
        return False
    raw = config.get(ACTIVE_EXPERIMENT_CONFIG_KEY, False)
    if isinstance(raw, str):
        return raw.strip().casefold() in {"1", "true", "yes", "on", "enabled"}
    return bool(raw)


def _event_time_ns(record: Mapping[str, object]) -> int:
    try:
        filetime = int(record.get("filetime_100ns", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, (filetime - 116_444_736_000_000_000) * 100)


def _hex_sha256(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return ""
    return hashlib.sha256(raw).hexdigest()


def _primitive_value(value: object, descriptor_type: str) -> int | float | None:
    if value is None:
        return None
    if descriptor_type == "int":
        if isinstance(value, bool):
            raise ValueError("boolean is not a ReqCastSkillNew integer")
        parsed = int(value)  # type: ignore[arg-type]
        if parsed != value or not -(2**53) <= parsed <= 2**53:
            raise ValueError("ReqCastSkillNew integer is not exactly representable")
        return parsed
    if descriptor_type == "float":
        if isinstance(value, bool):
            raise ValueError("boolean is not a ReqCastSkillNew float")
        parsed = float(value)  # type: ignore[arg-type]
        if not math.isfinite(parsed):
            raise ValueError("ReqCastSkillNew float must be finite")
        return parsed
    raise ValueError(f"unsupported ReqCastSkillNew descriptor {descriptor_type!r}")


@dataclass(frozen=True)
class CastSkillSnapshot:
    source_sequence: int
    captured_at_ns: int
    script_entity: int
    lua_state: int
    values: tuple[int | float | None, ...]
    descriptor_types: tuple[str, ...]
    descriptor_lua_types: tuple[int, ...]
    argument_sha256: str
    storage_sha256: str
    raw_record: Mapping[str, object] = field(repr=False, compare=False)

    @property
    def skill_id(self) -> int:
        return int(self.values[0] or 0)

    @property
    def target_entity_id(self) -> int:
        return int(self.values[1] or 0)

    @property
    def cast_instance_id(self) -> int:
        return int(self.values[7] or 0)

    def with_dynamic_values(
        self,
        *,
        target_entity_id: int,
        cast_instance_id: int,
    ) -> tuple[int | float | None, ...]:
        values = list(self.values)
        values[1] = int(target_entity_id)
        values[7] = int(cast_instance_id)
        return tuple(values)

    def summary(self) -> dict[str, object]:
        return {
            "source_sequence": self.source_sequence,
            "captured_at_ns": self.captured_at_ns,
            "script_entity": self.script_entity,
            "lua_state": self.lua_state,
            "arguments": [
                {
                    "position": index + 1,
                    "name": CAST_ARGUMENT_NAMES[index],
                    "descriptor": self.descriptor_types[index],
                    "lua_type": self.descriptor_lua_types[index],
                    "value": value,
                }
                for index, value in enumerate(self.values)
            ],
            "argument_sha256": self.argument_sha256,
            "storage_sha256": self.storage_sha256,
        }


def cast_skill_snapshot(record: object) -> CastSkillSnapshot:
    """Validate and normalize one synchronized natural cast call."""

    if not isinstance(record, Mapping):
        raise ValueError("cast snapshot source must be an RPC record")
    if str(record.get("method", "")) != PRIMITIVE_REPLAY_METHOD:
        raise ValueError("cast snapshot source is not ReqCastSkillNew")
    if (
        int(record.get("argument_sync_state", 0) or 0) != 1
        or str(record.get("lua_argument_capture", ""))
        != "hook_entry_synchronized"
        or str(record.get("variadic_storage_capture", ""))
        != "hook_entry_synchronized"
    ):
        raise ValueError("cast snapshot was not synchronized at call_server entry")
    numbers = record.get("lua_argument_numbers")
    cells = record.get("lua_argument_cells")
    vector = record.get("argument_descriptor_vector")
    items = vector.get("items") if isinstance(vector, Mapping) else None
    if (
        not isinstance(numbers, list)
        or not isinstance(cells, list)
        or not isinstance(items, list)
        or len(numbers) != 8
        or len(cells) != 8
        or len(items) != 8
    ):
        raise ValueError("cast snapshot does not contain eight complete arguments")

    descriptor_types: list[str] = []
    descriptor_lua_types: list[int] = []
    values: list[int | float | None] = []
    for index, ((expected_name, expected_lua_type), item, number, raw_cell) in enumerate(
        zip(PRIMITIVE_REPLAY_SCHEMA, items, numbers, cells, strict=True)
    ):
        if not isinstance(item, Mapping):
            raise ValueError(f"cast descriptor {index + 1} is invalid")
        name = str(item.get("type_name", ""))
        lua_type = int(item.get("lua_type", -1) or 0)
        if (name, lua_type) != (expected_name, expected_lua_type):
            raise ValueError(
                f"cast descriptor {index + 1} changed: {(name, lua_type)!r}"
            )
        cell = int(raw_cell)
        signed_tag = int.from_bytes(
            cell.to_bytes(8, "little", signed=False), "little", signed=True
        ) >> 47
        if cell != 0xFFFF_FFFF_FFFF_FFFF and -14 <= signed_tag <= -1:
            raise ValueError(
                f"cast argument {index + 1} contains a forbidden Lua reference"
            )
        if number is None and cell != 0xFFFF_FFFF_FFFF_FFFF:
            raise ValueError(
                f"cast argument {index + 1} is not a primitive nil/number"
            )
        descriptor_types.append(name)
        descriptor_lua_types.append(lua_type)
        values.append(_primitive_value(number, name))

    skill_id = int(values[0] or 0)
    cast_instance_id = int(values[7] or 0)
    if skill_id <= 0 or cast_instance_id <= 0:
        raise ValueError("cast skill and instance identifiers must be positive")
    return CastSkillSnapshot(
        source_sequence=int(record.get("sequence", 0) or 0),
        captured_at_ns=_event_time_ns(record),
        script_entity=int(record.get("script_entity", 0) or 0),
        lua_state=int(record.get("lua_state_address", 0) or 0),
        values=tuple(values),
        descriptor_types=tuple(descriptor_types),
        descriptor_lua_types=tuple(descriptor_lua_types),
        argument_sha256=_hex_sha256(record.get("lua_argument_snapshot")),
        storage_sha256=_hex_sha256(record.get("variadic_storage_snapshot")),
        raw_record=dict(record),
    )


@dataclass(frozen=True)
class CastReplayStep:
    snapshot: CastSkillSnapshot
    values: tuple[int | float | None, ...]
    delay_after_previous_seconds: float


@dataclass(frozen=True)
class CastReplayPlan:
    target_entity_id: int
    steps: tuple[CastReplayStep, ...]
    source: str

    def summary(self) -> dict[str, object]:
        return {
            "target_entity_id": self.target_entity_id,
            "source": self.source,
            "steps": [
                {
                    "skill_id": int(step.values[0] or 0),
                    "cast_instance_id": int(step.values[7] or 0),
                    "delay_after_previous_ms": round(
                        step.delay_after_previous_seconds * 1000, 3
                    ),
                    "arguments": list(step.values),
                    "source_sequence": step.snapshot.source_sequence,
                }
                for step in self.steps
            ],
        }


class CastSkillSessionState:
    """Track only the live values needed to reconstruct one natural chain."""

    def __init__(self, chain_skills: Sequence[int] = DEFAULT_CHAIN_SKILLS) -> None:
        self.chain_skills = tuple(int(value) for value in chain_skills)
        if len(self.chain_skills) != 2 or any(value <= 0 for value in self.chain_skills):
            raise ValueError("the first-hit experiment requires two skill IDs")
        self.lock_target_id = 0
        self.lock_script_entity = 0
        self.lock_lua_state = 0
        self.latest_cast_instance_id = 0
        self.latest_live_cast_instance_id = 0
        self.snapshots: list[CastSkillSnapshot] = []
        self.seeded_snapshot_sequences: set[int] = set()
        self.latest_seeded_cast_instance_id = 0
        self.capture_gap_detected = False

    def seed_templates(self, records: Sequence[object]) -> tuple[CastSkillSnapshot, ...]:
        """Load primitive field templates without treating IDs as current.

        Seed records may come from an earlier observation in the same game
        session.  They are useful only for the confirmed eight-argument shape;
        a newly observed live cast is still required before ``build_plan`` can
        derive the next monotonically increasing cast-instance identifiers.
        """

        snapshots = tuple(cast_skill_snapshot(record) for record in records)
        if len(snapshots) != 2:
            raise ValueError("exactly two seeded cast templates are required")
        first, second = snapshots
        gap = (second.captured_at_ns - first.captured_at_ns) / 1_000_000_000
        if (
            (first.skill_id, second.skill_id) != self.chain_skills
            or not MIN_NATURAL_CHAIN_GAP_SECONDS <= gap <= MAX_NATURAL_CHAIN_GAP_SECONDS
            or second.cast_instance_id != first.cast_instance_id + 1
            or first.lua_state != second.lua_state
            or first.script_entity != second.script_entity
        ):
            raise ValueError("seeded records are not one complete natural cast pair")
        self.snapshots.extend(snapshots)
        self.seeded_snapshot_sequences.update(
            snapshot.source_sequence for snapshot in snapshots
        )
        self.latest_seeded_cast_instance_id = max(
            snapshot.cast_instance_id for snapshot in snapshots
        )
        self.latest_cast_instance_id = max(
            self.latest_cast_instance_id,
            self.latest_seeded_cast_instance_id,
        )
        return snapshots

    def observe(self, record: object) -> CastSkillSnapshot | None:
        if not isinstance(record, Mapping):
            return None
        method = str(record.get("method", ""))
        if method == "ReqReportLockTarget":
            values = record.get("lua_argument_numbers")
            if (
                int(record.get("argument_sync_state", 0) or 0) == 1
                and isinstance(values, list)
                and len(values) == 1
            ):
                self.lock_target_id = int(values[0] or 0)
                self.lock_script_entity = int(record.get("script_entity", 0) or 0)
                self.lock_lua_state = int(record.get("lua_state_address", 0) or 0)
            return None
        if method != PRIMITIVE_REPLAY_METHOD:
            return None
        snapshot = cast_skill_snapshot(record)
        self.latest_cast_instance_id = max(
            self.latest_cast_instance_id, snapshot.cast_instance_id
        )
        self.latest_live_cast_instance_id = max(
            self.latest_live_cast_instance_id, snapshot.cast_instance_id
        )
        self.snapshots.append(snapshot)
        if len(self.snapshots) > 128:
            del self.snapshots[:-128]
        return snapshot

    def mark_capture_gap(self) -> None:
        self.capture_gap_detected = True

    def build_plan(self) -> CastReplayPlan:
        if self.capture_gap_detected:
            raise RuntimeError("capture gap prevents safe cast-instance reconstruction")
        if self.lock_target_id <= 0:
            raise RuntimeError("no currently locked target has been observed")
        candidates = [
            snapshot
            for snapshot in self.snapshots
            if snapshot.target_entity_id == self.lock_target_id
        ]
        selected: tuple[CastSkillSnapshot, CastSkillSnapshot] | None = None
        for first, second in zip(candidates, candidates[1:]):
            gap = (second.captured_at_ns - first.captured_at_ns) / 1_000_000_000
            if (
                (first.skill_id, second.skill_id) == self.chain_skills
                and MIN_NATURAL_CHAIN_GAP_SECONDS <= gap <= MAX_NATURAL_CHAIN_GAP_SECONDS
                and second.cast_instance_id == first.cast_instance_id + 1
                and first.lua_state == second.lua_state
                and first.script_entity == second.script_entity
                and first.lua_state == self.lock_lua_state
                and first.script_entity == self.lock_script_entity
            ):
                selected = (first, second)
        if selected is None:
            raise RuntimeError(
                "no complete natural first-hit pair exists for the current locked target"
            )
        if self.seeded_snapshot_sequences:
            if (
                self.latest_live_cast_instance_id
                <= self.latest_seeded_cast_instance_id
            ):
                raise RuntimeError(
                    "no fresh live cast exists after the seeded template pair"
                )
            current_instance = self.latest_live_cast_instance_id
        else:
            if self.latest_cast_instance_id <= 0:
                raise RuntimeError("current-session cast instance counter is unknown")
            current_instance = self.latest_cast_instance_id
        first, second = selected
        delay = (second.captured_at_ns - first.captured_at_ns) / 1_000_000_000
        next_instance = current_instance + 1
        return CastReplayPlan(
            target_entity_id=self.lock_target_id,
            source="current_session_synchronized_natural_pair",
            steps=(
                CastReplayStep(
                    snapshot=first,
                    values=first.with_dynamic_values(
                        target_entity_id=self.lock_target_id,
                        cast_instance_id=next_instance,
                    ),
                    delay_after_previous_seconds=0.0,
                ),
                CastReplayStep(
                    snapshot=second,
                    values=second.with_dynamic_values(
                        target_entity_id=self.lock_target_id,
                        cast_instance_id=next_instance + 1,
                    ),
                    delay_after_previous_seconds=delay,
                ),
            ),
        )


class OneShotCastSkillChainDriver:
    """Own one isolated capture/replay hook and allow one explicit chain."""

    def __init__(
        self,
        profile: Mapping[str, object],
        *,
        pid: int | None = None,
        chain_skills: Sequence[int] = DEFAULT_CHAIN_SKILLS,
        emit: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.profile = profile
        self.pid = pid
        self.state = CastSkillSessionState(chain_skills)
        self.emit = emit or (lambda _payload: None)
        self.hook: TeamStatsRequestHook | None = None
        self.attempted = False
        self.plan: CastReplayPlan | None = None
        self.step_index = -1
        self.baseline_request_count = 0
        self.step_deadline = 0.0
        self.next_step_at = 0.0

    def _event(self, event: str, **fields: object) -> None:
        self.emit({"event": event, "event_time_ns": time.time_ns(), **fields})

    def install(self) -> "OneShotCastSkillChainDriver":
        if self.hook is not None:
            return self
        self.hook = TeamStatsRequestHook(
            profile=self.profile,
            pid=self.pid,
            interval=300.0,
            enabled=False,
            additional_request_methods=(PRIMITIVE_REPLAY_METHOD,),
            synchronized_methods=CAPTURE_METHODS,
            takeover_existing=False,
            stable_primary_only=False,
            allow_existing_adoption=False,
            primitive_replay_enabled=True,
        ).install()
        self._event(
            "installed",
            game_pid=self.hook.pid,
            scheduled_request_branch=False,
            damage_message_generation=False,
        )
        return self

    def seed_templates(self, records: Sequence[object]) -> tuple[CastSkillSnapshot, ...]:
        if self.hook is not None:
            raise RuntimeError("cast templates must be seeded before hook installation")
        snapshots = self.state.seed_templates(records)
        self._event(
            "templates_seeded",
            source_sequences=[snapshot.source_sequence for snapshot in snapshots],
            skill_ids=[snapshot.skill_id for snapshot in snapshots],
            target_entity_id=snapshots[0].target_entity_id,
            latest_seeded_instance_id=max(
                snapshot.cast_instance_id for snapshot in snapshots
            ),
            requires_fresh_live_cast=True,
        )
        return snapshots

    def poll(self) -> list[dict[str, object]]:
        if self.hook is None:
            return []
        before_dropped = self.hook.request_dropped_count
        records = self.hook.poll_requests()
        if self.hook.request_dropped_count != before_dropped:
            self.state.mark_capture_gap()
            self._event("capture_gap", dropped=self.hook.request_dropped_count)
        for record in records:
            try:
                snapshot = self.state.observe(record)
            except ValueError as error:
                self._event(
                    "snapshot_rejected",
                    method=str(record.get("method", "")),
                    reason=str(error),
                )
                continue
            if snapshot is not None:
                self._event("cast_snapshot", **snapshot.summary())
            elif str(record.get("method", "")) == "ReqReportLockTarget":
                self._event("lock_target", target_entity_id=self.state.lock_target_id)
        self._advance()
        return records

    def arm(self, confirmation: str) -> CastReplayPlan:
        if confirmation != ACTIVE_EXPERIMENT_CONFIRMATION:
            raise ValueError("active cast experiment confirmation is missing")
        if self.attempted:
            raise RuntimeError("this driver has already attempted one cast chain")
        if self.hook is None:
            raise RuntimeError("cast experiment hook is not installed")
        plan = self.state.build_plan()
        self.attempted = True
        self.plan = plan
        self.step_index = 0
        self._event("chain_armed", **plan.summary())
        self._arm_current_step()
        return plan

    def _arm_current_step(self) -> None:
        if self.hook is None or self.plan is None or self.step_index < 0:
            raise RuntimeError("cast replay plan is not active")
        step = self.plan.steps[self.step_index]
        prepared = self.hook.prepare_primitive_replay(
            step.snapshot.raw_record, step.values
        )
        self.baseline_request_count = self.hook.arm_primitive_replay()
        self.step_deadline = time.monotonic() + REPLAY_EDGE_TIMEOUT_SECONDS
        self.next_step_at = 0.0
        self._event(
            "step_armed",
            step=self.step_index + 1,
            skill_id=int(step.values[0] or 0),
            target_entity_id=int(step.values[1] or 0),
            cast_instance_id=int(step.values[7] or 0),
            source_sequence=prepared["source_sequence"],
            timeout_seconds=REPLAY_EDGE_TIMEOUT_SECONDS,
        )

    def _advance(self) -> None:
        if self.hook is None or self.plan is None or self.step_index < 0:
            return
        if self.next_step_at:
            if time.monotonic() >= self.next_step_at:
                self.step_index += 1
                self._arm_current_step()
            return
        status = self.hook.primitive_replay_status()
        request_count = int(status.get("request_count", 0) or 0)
        if request_count > self.baseline_request_count:
            step = self.plan.steps[self.step_index]
            self._event(
                "step_sent",
                step=self.step_index + 1,
                skill_id=int(step.values[0] or 0),
                cast_instance_id=int(step.values[7] or 0),
                call_result=int(status.get("last_result", 0) or 0),
            )
            if self.step_index + 1 >= len(self.plan.steps):
                self._event("chain_complete", request_count=request_count)
                self.plan = None
                self.step_index = -1
                return
            delay = self.plan.steps[self.step_index + 1].delay_after_previous_seconds
            self.next_step_at = time.monotonic() + delay
            self._event("inter_step_wait", delay_ms=round(delay * 1000, 3))
            return
        if self.step_deadline and time.monotonic() >= self.step_deadline:
            try:
                self.hook.cancel_primitive_replay()
            finally:
                self._event("step_timeout", step=self.step_index + 1)
                self.plan = None
                self.step_index = -1

    def status(self) -> dict[str, object]:
        return {
            "installed": self.hook is not None,
            "attempted": self.attempted,
            "locked_target": self.state.lock_target_id,
            "latest_cast_instance_id": self.state.latest_cast_instance_id,
            "latest_live_cast_instance_id": self.state.latest_live_cast_instance_id,
            "seeded_snapshot_count": len(self.state.seeded_snapshot_sequences),
            "natural_snapshot_count": len(self.state.snapshots),
            "capture_gap_detected": self.state.capture_gap_detected,
            "active_step": self.step_index + 1 if self.step_index >= 0 else 0,
            "native": self.hook.primitive_replay_status() if self.hook else {},
        }

    def close(self) -> None:
        hook, self.hook = self.hook, None
        if hook is None:
            return
        try:
            hook.cancel_primitive_replay()
        finally:
            hook.close()
        self.plan = None
        self.step_index = -1
        self._event("closed")

    def __enter__(self) -> "OneShotCastSkillChainDriver":
        return self.install()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class JsonLineLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, payload: dict[str, object]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        print(line, flush=True)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line + "\n")


def load_seed_templates(
    path: Path,
    chain_skills: Sequence[int] = DEFAULT_CHAIN_SKILLS,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Select the latest complete synchronized natural pair from JSONL."""

    expected = tuple(int(value) for value in chain_skills)
    candidates: list[tuple[CastSkillSnapshot, Mapping[str, object]]] = []
    with path.open("r", encoding="utf-8-sig") as source:
        for line in source:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, Mapping):
                continue
            if str(record.get("method", "")) != PRIMITIVE_REPLAY_METHOD:
                continue
            try:
                snapshot = cast_skill_snapshot(record)
            except (TypeError, ValueError):
                continue
            candidates.append((snapshot, record))

    selected: tuple[Mapping[str, object], Mapping[str, object]] | None = None
    for (first, first_record), (second, second_record) in zip(
        candidates, candidates[1:]
    ):
        gap = (second.captured_at_ns - first.captured_at_ns) / 1_000_000_000
        if (
            (first.skill_id, second.skill_id) == expected
            and MIN_NATURAL_CHAIN_GAP_SECONDS <= gap <= MAX_NATURAL_CHAIN_GAP_SECONDS
            and second.cast_instance_id == first.cast_instance_id + 1
            and first.target_entity_id == second.target_entity_id
            and first.lua_state == second.lua_state
            and first.script_entity == second.script_entity
        ):
            selected = (first_record, second_record)
    if selected is None:
        raise RuntimeError("seed log contains no complete natural first-hit pair")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(__file__).resolve().with_name("runtime-profile.dev.json"),
    )
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--first-skill", type=int, default=DEFAULT_CHAIN_SKILLS[0])
    parser.add_argument("--second-skill", type=int, default=DEFAULT_CHAIN_SKILLS[1])
    parser.add_argument("--arm-on-ready", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--seed-log", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.arm_on_ready and args.confirm != ACTIVE_EXPERIMENT_CONFIRMATION:
        parser.error(
            f"--arm-on-ready requires --confirm {ACTIVE_EXPERIMENT_CONFIRMATION}"
        )

    log = JsonLineLog(args.output)
    profile = load_runtime_profile_file(args.profile)
    deadline = time.monotonic() + max(1.0, float(args.seconds))
    armed = False
    driver = OneShotCastSkillChainDriver(
        profile,
        pid=args.pid,
        chain_skills=(args.first_skill, args.second_skill),
        emit=log,
    )
    if args.seed_log is not None:
        driver.seed_templates(
            load_seed_templates(
                args.seed_log,
                (args.first_skill, args.second_skill),
            )
        )
    with driver:
        while time.monotonic() < deadline:
            driver.poll()
            if args.arm_on_ready and not armed:
                try:
                    driver.arm(args.confirm)
                except RuntimeError:
                    pass
                else:
                    armed = True
            time.sleep(0.01)
        log({"event": "final_status", **driver.status()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
