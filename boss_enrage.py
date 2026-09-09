#!/usr/bin/env python3
"""Read-only Boss enrage pacing from encounter time and current HP.

The predictor deliberately consumes already-resolved Boss identity/HP data.  It
does not participate in packet capture, encounter ownership, DPS totals, or
archive boundaries.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


BOSS_ENRAGE_SCHEMA_VERSION = 1


def normalize_boss_key(value: object) -> str:
    return "".join(
        character.casefold()
        for character in str(value or "").strip()
        if character.isalnum()
    )


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _positive_float(value: object, default: float) -> float:
    parsed = _finite_float(value, default)
    return parsed if parsed > 0.0 else float(default)


def _integer_set(value: object) -> frozenset[int]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    result: set[int] = set()
    for item in value:
        try:
            parsed = int(item or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed > 0:
            result.add(parsed)
    return frozenset(result)


@dataclass(frozen=True)
class BossEnrageHpSlot:
    slot_id: str
    template_ids: frozenset[int]
    max_hp: float
    complete_when_templates: frozenset[int] = frozenset()


@dataclass(frozen=True)
class BossEnrageRule:
    rule_id: str
    enrage_seconds: float
    template_ids: frozenset[int] = frozenset()
    boss_names: frozenset[str] = frozenset()
    dungeon_ids: frozenset[int] = frozenset()
    state_hold_seconds: float = 3.0
    safe_margin_seconds: float = 30.0
    critical_margin_seconds: float = 10.0
    countdown_start_signal: str = ""
    first_sample_is_baseline: bool = False
    hp_slots: tuple[BossEnrageHpSlot, ...] = ()

    def matches(
        self,
        bosses: Sequence[object],
        *,
        dungeon_id: int = 0,
    ) -> int:
        if self.dungeon_ids and int(dungeon_id or 0) not in self.dungeon_ids:
            return 0
        templates = {
            _boss_int_value(boss, "template_id")
            for boss in bosses
            if _boss_int_value(boss, "template_id") > 0
        }
        names = {
            normalize_boss_key(_boss_value(boss, "name", ""))
            for boss in bosses
            if normalize_boss_key(_boss_value(boss, "name", ""))
        }
        if self.template_ids and templates & self.template_ids:
            return 4 + int(bool(self.dungeon_ids))
        if self.boss_names and names & self.boss_names:
            return 2 + int(bool(self.dungeon_ids))
        return 0


@dataclass(frozen=True)
class BossEnrageCatalog:
    rules: tuple[BossEnrageRule, ...] = ()

    def match(
        self,
        bosses: Sequence[object],
        *,
        dungeon_id: int = 0,
    ) -> BossEnrageRule | None:
        best_rule: BossEnrageRule | None = None
        best_score = 0
        for rule in self.rules:
            score = rule.matches(bosses, dungeon_id=dungeon_id)
            if score > best_score:
                best_rule = rule
                best_score = score
        return best_rule


def _rule_from_mapping(
    value: Mapping[str, object], defaults: Mapping[str, object]
) -> BossEnrageRule | None:
    if value.get("enabled", True) is False:
        return None
    rule_id = str(value.get("id", "") or "").strip()
    enrage_seconds = _finite_float(value.get("enrage_seconds"), 0.0)
    if not rule_id or enrage_seconds <= 0.0:
        return None
    raw_names = value.get("boss_names", ())
    if not isinstance(raw_names, (list, tuple, set, frozenset)):
        raw_names = ()
    boss_names = frozenset(
        normalized
        for item in raw_names
        if (normalized := normalize_boss_key(item))
    )
    template_ids = _integer_set(value.get("template_ids"))
    if not template_ids and not boss_names:
        return None

    def setting(name: str, fallback: object) -> object:
        return value.get(name, defaults.get(name, fallback))

    hp_slots: list[BossEnrageHpSlot] = []
    raw_hp_slots = value.get("hp_slots", ())
    if isinstance(raw_hp_slots, list):
        for index, raw_slot in enumerate(raw_hp_slots):
            if not isinstance(raw_slot, Mapping):
                continue
            slot_templates = _integer_set(raw_slot.get("template_ids"))
            slot_max_hp = _finite_float(raw_slot.get("max_hp"), 0.0)
            if not slot_templates or slot_max_hp <= 0.0:
                continue
            slot_id = str(raw_slot.get("id", "") or "").strip()
            hp_slots.append(
                BossEnrageHpSlot(
                    slot_id=slot_id or f"slot_{index + 1}",
                    template_ids=slot_templates,
                    max_hp=slot_max_hp,
                    complete_when_templates=_integer_set(
                        raw_slot.get("complete_when_templates")
                    ),
                )
            )
    return BossEnrageRule(
        rule_id=rule_id,
        enrage_seconds=enrage_seconds,
        template_ids=template_ids,
        boss_names=boss_names,
        dungeon_ids=_integer_set(value.get("dungeon_ids")),
        state_hold_seconds=_positive_float(
            setting("state_hold_seconds", 3.0), 3.0
        ),
        safe_margin_seconds=_positive_float(
            setting("safe_margin_seconds", 30.0), 30.0
        ),
        critical_margin_seconds=_positive_float(
            setting("critical_margin_seconds", 10.0), 10.0
        ),
        countdown_start_signal=str(
            value.get("countdown_start_signal", "") or ""
        ).strip(),
        first_sample_is_baseline=bool(
            value.get("first_sample_is_baseline", False)
        ),
        hp_slots=tuple(hp_slots),
    )


def load_boss_enrage_catalog(*paths: Path) -> BossEnrageCatalog:
    """Load and merge forecast rules; later files replace rules with the same ID."""

    defaults: dict[str, object] = {}
    rules_by_id: dict[str, BossEnrageRule] = {}
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_defaults = payload.get("defaults", {})
        if isinstance(raw_defaults, dict):
            defaults.update(raw_defaults)
        raw_rules = payload.get("encounters", ())
        if not isinstance(raw_rules, list):
            continue
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                continue
            rule = _rule_from_mapping(raw_rule, defaults)
            rule_id = str(raw_rule.get("id", "") or "").strip()
            if rule is None:
                if rule_id and raw_rule.get("enabled", True) is False:
                    rules_by_id.pop(rule_id, None)
                continue
            rules_by_id[rule.rule_id] = rule
    return BossEnrageCatalog(tuple(rules_by_id.values()))


def _boss_value(boss: object, key: str, default: object = None) -> object:
    if isinstance(boss, Mapping):
        return boss.get(key, default)
    return getattr(boss, key, default)


def _boss_int_value(boss: object, key: str) -> int:
    try:
        return int(_boss_value(boss, key, 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class EnragePrediction:
    rule_id: str
    state: str
    desired_state: str
    message: str
    calculating: bool
    boss_current_hp: float
    boss_max_hp: float
    boss_hp_percent: float
    enrage_seconds: float
    elapsed_seconds: float
    time_to_enrage_seconds: float
    estimated_kill_seconds: float | None
    safety_margin_seconds: float | None
    expected_remaining_hp: float
    expected_remaining_hp_percent: float
    required_output_increase_percent: float | None
    recent_hp_per_second: float
    recent_percent_per_second: float
    lifetime_hp_per_second: float
    blended_hp_per_second: float
    recent_sample_seconds: float
    excluded_mechanic_seconds: float
    countdown_paused_seconds: float
    mechanic_active: bool
    schedule_start_hp_percent: float = 100.0
    theoretical_remaining_hp: float = 0.0
    theoretical_remaining_hp_percent: float = 0.0
    theoretical_stage_remaining_percent: float = 0.0
    actual_stage_remaining_percent: float = 0.0
    progress_delta_percent: float = 0.0


def _prediction_message(
    state: str,
    *,
    margin_seconds: float | None,
    expected_remaining_percent: float,
    required_increase_percent: float | None,
) -> str:
    del expected_remaining_percent, required_increase_percent
    labels = {
        "ample": "充裕",
        "normal": "正常",
        "critical": "临界",
        "danger": "危险",
    }
    label = labels.get(state, "节奏预测")
    if margin_seconds is None or not math.isfinite(margin_seconds):
        return label
    rounded = int(math.floor(abs(margin_seconds) + 0.5))
    minutes, seconds = divmod(rounded, 60)
    sign = "+" if margin_seconds >= 0.0 else "-"
    return f"{label} · {sign}{minutes}:{seconds:02d}"


class BossEnragePredictor:
    """Compare live HP progress with the Boss enrage-time schedule."""

    def __init__(self, catalog: BossEnrageCatalog):
        self.catalog = catalog
        self._encounter_key = ""
        self._rule_id = ""
        self._stable_state = ""
        self._stable_message = ""
        self._candidate_state = ""
        self._candidate_since = 0.0
        self._slot_hp: dict[str, float] = {}
        self._schedule_start_hp_ratio = 1.0
        self._schedule_baseline_ready = False
        self._last_elapsed_seconds: float | None = None

    def reset(self) -> None:
        self._encounter_key = ""
        self._rule_id = ""
        self._stable_state = ""
        self._stable_message = ""
        self._candidate_state = ""
        self._candidate_since = 0.0
        self._slot_hp.clear()
        self._schedule_start_hp_ratio = 1.0
        self._schedule_baseline_ready = False
        self._last_elapsed_seconds = None

    def _aggregate_hp(
        self,
        rule: BossEnrageRule,
        bosses: Sequence[object],
    ) -> tuple[float, float, tuple[int, ...]] | None:
        if rule.hp_slots:
            return self._aggregate_slotted_hp(rule, bosses)
        current_total = 0.0
        maximum_total = 0.0
        composition: list[int] = []
        valid_count = 0
        for index, boss in enumerate(bosses):
            current = _finite_float(_boss_value(boss, "current_hp"), -1.0)
            maximum = _finite_float(_boss_value(boss, "max_hp"), 0.0)
            if maximum <= 0.0:
                maximum = _finite_float(
                    _boss_value(boss, "observed_max_hp"), 0.0
                )
            if current < 0.0 or maximum <= 0.0:
                continue
            current_total += min(maximum, max(0.0, current))
            maximum_total += maximum
            entity_id = _boss_int_value(boss, "entity_id")
            template_id = _boss_int_value(boss, "template_id")
            composition.append(entity_id or -(template_id or index + 1))
            valid_count += 1
        if not valid_count or maximum_total <= 0.0:
            return None
        return current_total, maximum_total, tuple(sorted(composition))

    def _aggregate_slotted_hp(
        self,
        rule: BossEnrageRule,
        bosses: Sequence[object],
    ) -> tuple[float, float, tuple[int, ...]] | None:
        """Keep configured sequential Boss HP in a stable encounter total."""

        visible_by_template: dict[int, list[tuple[float, float]]] = {}
        visible_templates: set[int] = set()
        for boss in bosses:
            template_id = _boss_int_value(boss, "template_id")
            current = _finite_float(_boss_value(boss, "current_hp"), -1.0)
            maximum = _finite_float(_boss_value(boss, "max_hp"), 0.0)
            if maximum <= 0.0:
                maximum = _finite_float(
                    _boss_value(boss, "observed_max_hp"), 0.0
                )
            if template_id <= 0 or current < 0.0:
                continue
            visible_templates.add(template_id)
            visible_by_template.setdefault(template_id, []).append(
                (current, maximum)
            )

        current_total = 0.0
        maximum_total = 0.0
        composition: list[int] = []
        matched_any = False
        for index, slot in enumerate(rule.hp_slots):
            maximum = float(slot.max_hp)
            composition.append(-(index + 1))
            completed = bool(
                slot.complete_when_templates & visible_templates
            )
            candidates = [
                values
                for template_id in slot.template_ids
                for values in visible_by_template.get(template_id, ())
            ]
            if completed:
                # A verified successor is stronger than a stale previous-phase
                # HP row that the client has not removed from the visible set.
                self._slot_hp[slot.slot_id] = 0.0
            elif candidates:
                matched_any = True
                current, observed_maximum = max(candidates, key=lambda item: item[0])
                if observed_maximum > 0.0:
                    maximum = max(maximum, observed_maximum)
                slot_current = min(maximum, max(0.0, current))
                self._slot_hp[slot.slot_id] = slot_current
            elif slot.slot_id not in self._slot_hp:
                # A configured but not-yet-published Boss remains part of the
                # work required to finish the encounter.
                self._slot_hp[slot.slot_id] = maximum
            maximum_total += maximum
            current_total += min(maximum, max(0.0, self._slot_hp[slot.slot_id]))

        if not matched_any or maximum_total <= 0.0:
            return None
        return current_total, maximum_total, tuple(composition)

    def _start_encounter(
        self,
        encounter_key: str,
        rule: BossEnrageRule,
    ) -> None:
        self.reset()
        self._encounter_key = encounter_key
        self._rule_id = rule.rule_id

    @staticmethod
    def _classify(
        rule: BossEnrageRule,
        *,
        margin_seconds: float | None,
        required_increase_percent: float | None,
    ) -> str:
        del required_increase_percent
        if margin_seconds is None or not math.isfinite(margin_seconds):
            return "danger"
        if margin_seconds >= rule.safe_margin_seconds:
            return "ample"
        if margin_seconds >= rule.critical_margin_seconds:
            return "normal"
        if margin_seconds >= -rule.critical_margin_seconds:
            return "critical"
        return "danger"

    def _stable_display(
        self,
        desired_state: str,
        desired_message: str,
        *,
        monotonic_seconds: float,
        hold_seconds: float,
    ) -> tuple[str, str]:
        if not self._stable_state:
            self._stable_state = desired_state
            self._stable_message = desired_message
            return desired_state, desired_message
        if desired_state == self._stable_state:
            self._candidate_state = ""
            self._candidate_since = 0.0
            self._stable_message = desired_message
            return self._stable_state, self._stable_message
        if desired_state != self._candidate_state:
            self._candidate_state = desired_state
            self._candidate_since = monotonic_seconds
        elif monotonic_seconds - self._candidate_since >= hold_seconds:
            self._stable_state = desired_state
            self._stable_message = desired_message
            self._candidate_state = ""
            self._candidate_since = 0.0
        return self._stable_state, self._stable_message

    def update(
        self,
        *,
        encounter_key: object,
        elapsed_seconds: object,
        bosses: Iterable[object],
        dungeon_id: object = 0,
        encounter_running: bool = True,
        forced_invulnerability: bool = False,
        monotonic_seconds: object = 0.0,
        countdown_elapsed_seconds: object = None,
    ) -> EnragePrediction | None:
        # Kept in the call signature for compatibility with the live UI. The
        # pacing model intentionally uses no DPS or damage-window sampling.
        del forced_invulnerability
        boss_list = list(bosses)
        if not encounter_running or not boss_list:
            self.reset()
            return None
        try:
            parsed_dungeon_id = int(dungeon_id or 0)
        except (TypeError, ValueError, OverflowError):
            parsed_dungeon_id = 0
        rule = self.catalog.match(boss_list, dungeon_id=parsed_dungeon_id)
        if rule is None:
            self.reset()
            return None
        key = str(encounter_key or "").strip() or "current"
        encounter_elapsed = max(0.0, _finite_float(elapsed_seconds, 0.0))
        monotonic_now = max(
            0.0, _finite_float(monotonic_seconds, encounter_elapsed)
        )
        if key != self._encounter_key or rule.rule_id != self._rule_id:
            self._start_encounter(key, rule)
        try:
            explicit_countdown_elapsed = (
                None
                if countdown_elapsed_seconds is None
                else max(0.0, float(countdown_elapsed_seconds))
            )
        except (TypeError, ValueError, OverflowError):
            explicit_countdown_elapsed = None
        elapsed = (
            explicit_countdown_elapsed
            if rule.countdown_start_signal and explicit_countdown_elapsed is not None
            else encounter_elapsed
        )
        if self._last_elapsed_seconds is not None:
            # The UI can switch from its local pull edge to a corrected combat
            # interval while the same encounter is running. Never let that
            # source correction move the schedule marker backwards or reset
            # phase/dual-Boss state; encounter-key changes still reset above.
            elapsed = max(self._last_elapsed_seconds, elapsed)

        aggregate = self._aggregate_hp(rule, boss_list)
        if aggregate is None:
            self.reset()
            return None
        current_hp, max_hp, _composition = aggregate
        if current_hp <= 0.0:
            self.reset()
            return None
        boss_percent = current_hp / max_hp * 100.0

        # Some encounters explicitly start their enrage clock at a later
        # phase signal. Phase one must not produce a marker. The first sample
        # after that signal becomes the schedule baseline (for example,
        # 星象仪者 starts its five-minute clock below 100% HP).
        if rule.countdown_start_signal and explicit_countdown_elapsed is None:
            self._stable_state = ""
            self._stable_message = ""
            self._candidate_state = ""
            self._candidate_since = 0.0
            self._schedule_start_hp_ratio = 1.0
            self._schedule_baseline_ready = False
            self._last_elapsed_seconds = None
            return None

        if not self._schedule_baseline_ready and rule.first_sample_is_baseline:
            self._schedule_start_hp_ratio = min(
                1.0, max(0.0, current_hp / max_hp)
            )
        self._schedule_baseline_ready = True
        self._last_elapsed_seconds = elapsed

        time_to_enrage = max(0.0, rule.enrage_seconds - elapsed)
        theoretical_stage_ratio = min(
            1.0, max(0.0, time_to_enrage / rule.enrage_seconds)
        )
        schedule_start_hp = max_hp * self._schedule_start_hp_ratio
        actual_stage_ratio = min(
            1.0,
            max(0.0, current_hp / schedule_start_hp),
        )
        # Positive means actual HP is below the time schedule (ahead); negative
        # means more HP remains than the schedule allows (behind).
        progress_delta_ratio = theoretical_stage_ratio - actual_stage_ratio
        margin = progress_delta_ratio * rule.enrage_seconds
        theoretical_hp = schedule_start_hp * theoretical_stage_ratio
        theoretical_hp_percent = theoretical_hp / max_hp * 100.0
        progress_delta_percent = progress_delta_ratio * 100.0
        required_increase = None
        expected_remaining = max(0.0, current_hp - theoretical_hp)
        expected_remaining_percent = expected_remaining / max_hp * 100.0
        desired_state = self._classify(
            rule,
            margin_seconds=margin,
            required_increase_percent=required_increase,
        )
        desired_message = _prediction_message(
            desired_state,
            margin_seconds=margin,
            expected_remaining_percent=expected_remaining_percent,
            required_increase_percent=required_increase,
        )
        state, _message = self._stable_display(
            desired_state,
            desired_message,
            monotonic_seconds=monotonic_now,
            hold_seconds=rule.state_hold_seconds,
        )
        # State/color transitions are held for stability, while the numeric
        # margin remains live instead of freezing for the whole hold period.
        message = _prediction_message(
            state,
            margin_seconds=margin,
            expected_remaining_percent=expected_remaining_percent,
            required_increase_percent=required_increase,
        )
        self._stable_message = message
        return EnragePrediction(
            rule_id=rule.rule_id,
            state=state,
            desired_state=desired_state,
            message=message,
            calculating=False,
            boss_current_hp=current_hp,
            boss_max_hp=max_hp,
            boss_hp_percent=boss_percent,
            enrage_seconds=rule.enrage_seconds,
            elapsed_seconds=elapsed,
            time_to_enrage_seconds=time_to_enrage,
            estimated_kill_seconds=None,
            safety_margin_seconds=margin,
            expected_remaining_hp=expected_remaining,
            expected_remaining_hp_percent=expected_remaining_percent,
            required_output_increase_percent=required_increase,
            recent_hp_per_second=0.0,
            recent_percent_per_second=0.0,
            lifetime_hp_per_second=0.0,
            blended_hp_per_second=0.0,
            recent_sample_seconds=0.0,
            excluded_mechanic_seconds=0.0,
            countdown_paused_seconds=0.0,
            mechanic_active=False,
            schedule_start_hp_percent=self._schedule_start_hp_ratio * 100.0,
            theoretical_remaining_hp=theoretical_hp,
            theoretical_remaining_hp_percent=theoretical_hp_percent,
            theoretical_stage_remaining_percent=theoretical_stage_ratio * 100.0,
            actual_stage_remaining_percent=actual_stage_ratio * 100.0,
            progress_delta_percent=progress_delta_percent,
        )
