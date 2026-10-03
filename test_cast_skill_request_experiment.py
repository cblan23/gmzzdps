#!/usr/bin/env python3

from __future__ import annotations

import struct
import json
import tempfile
import unittest
from pathlib import Path

from cast_skill_request_experiment import (
    ACTIVE_EXPERIMENT_CONFIG_KEY,
    CAST_ARGUMENT_NAMES,
    CastSkillSessionState,
    active_experiment_enabled,
    cast_skill_snapshot,
    load_seed_templates,
)


INT_DESCRIPTOR = 0x6C16_29D0
FLOAT_DESCRIPTOR = 0x6C16_8770
SCRIPT_ENTITY = 0x127_E5D_0000
LUA_STATE = 0xF6AB_0380


def _cell(value):
    if value is None:
        return 0xFFFF_FFFF_FFFF_FFFF
    return struct.unpack("<Q", struct.pack("<d", float(value)))[0]


def cast_record(
    *,
    sequence: int,
    filetime: int,
    skill_id: int,
    target_id: int | None,
    instance_id: int,
    parameter_6: float | None = None,
) -> dict:
    values = [
        skill_id,
        target_id,
        -93.50488134361017 if target_id else None,
        -14.0 if target_id else None,
        -1026.4601440429688 if target_id else None,
        parameter_6,
        None,
        instance_id,
    ]
    types = (("int", 1), ("int", 1), *(("float", 2),) * 5, ("int", 1))
    storage = bytearray(0x200)
    storage[0x10 : 0x10 + len(b"ReqCastSkillNew")] = b"ReqCastSkillNew"
    raw_cells = [_cell(value) for value in values]
    return {
        "method": "ReqCastSkillNew",
        "sequence": sequence,
        "filetime_100ns": filetime,
        "script_entity": SCRIPT_ENTITY,
        "lua_state_address": LUA_STATE,
        "variadic_argument_count": 8,
        "argument_sync_state": 1,
        "lua_argument_capture": "hook_entry_synchronized",
        "variadic_storage_capture": "hook_entry_synchronized",
        "lua_argument_numbers": values,
        "lua_argument_cells": raw_cells,
        "lua_argument_snapshot": struct.pack("<8Q", *raw_cells).hex(),
        "variadic_storage_snapshot": bytes(storage).hex(),
        "argument_descriptor_vector": {
            "items": [
                {
                    "address": INT_DESCRIPTOR if name == "int" else FLOAT_DESCRIPTOR,
                    "type_name": name,
                    "lua_type": lua_type,
                }
                for name, lua_type in types
            ]
        },
    }


def lock_record(target_id: int) -> dict:
    return {
        "method": "ReqReportLockTarget",
        "argument_sync_state": 1,
        "script_entity": SCRIPT_ENTITY,
        "lua_state_address": LUA_STATE,
        "lua_argument_numbers": [target_id],
    }


class CastSkillSnapshotTests(unittest.TestCase):
    def test_normalizes_the_confirmed_eight_primitive_arguments(self):
        record = cast_record(
            sequence=140,
            filetime=134_339_599_859_460_080,
            skill_id=86_020_010,
            target_id=57_243_861_502_373,
            instance_id=10_001_616,
            parameter_6=179.33333276957273,
        )

        snapshot = cast_skill_snapshot(record)

        self.assertEqual(snapshot.skill_id, 86_020_010)
        self.assertEqual(snapshot.target_entity_id, 57_243_861_502_373)
        self.assertEqual(snapshot.cast_instance_id, 10_001_616)
        self.assertEqual(
            tuple(item["name"] for item in snapshot.summary()["arguments"]),
            CAST_ARGUMENT_NAMES,
        )
        self.assertEqual(
            snapshot.descriptor_types,
            ("int", "int", "float", "float", "float", "float", "float", "int"),
        )

    def test_rejects_a_tagged_reference_even_when_the_decoder_has_no_value(self):
        record = cast_record(
            sequence=1,
            filetime=134_339_599_000_000_000,
            skill_id=86_020_010,
            target_id=57_243_861_502_373,
            instance_id=10_001_616,
        )
        record["lua_argument_cells"][6] = 0xFFFD_8001_2345_6789

        with self.assertRaisesRegex(ValueError, "forbidden Lua reference"):
            cast_skill_snapshot(record)

    def test_requires_live_synchronized_storage(self):
        record = cast_record(
            sequence=1,
            filetime=134_339_599_000_000_000,
            skill_id=86_020_010,
            target_id=57_243_861_502_373,
            instance_id=10_001_616,
        )
        record["variadic_storage_capture"] = "post_call_readprocessmemory"

        with self.assertRaisesRegex(ValueError, "not synchronized"):
            cast_skill_snapshot(record)


class CastSkillSessionStateTests(unittest.TestCase):
    def test_rebuilds_target_and_monotonic_instances_from_current_session(self):
        target = 57_243_861_502_373
        state = CastSkillSessionState()
        state.observe(lock_record(target))
        first = cast_record(
            sequence=140,
            filetime=134_339_599_859_460_080,
            skill_id=86_020_010,
            target_id=target,
            instance_id=10_001_616,
            parameter_6=179.33333276957273,
        )
        second = cast_record(
            sequence=142,
            filetime=134_339_599_864_754_818,
            skill_id=86_020_020,
            target_id=target,
            instance_id=10_001_617,
        )
        state.observe(first)
        state.observe(second)

        plan = state.build_plan()

        self.assertEqual(plan.target_entity_id, target)
        self.assertEqual(plan.steps[0].values[1], target)
        self.assertEqual(plan.steps[1].values[1], target)
        self.assertEqual(plan.steps[0].values[7], 10_001_618)
        self.assertEqual(plan.steps[1].values[7], 10_001_619)
        self.assertAlmostEqual(
            plan.steps[1].delay_after_previous_seconds,
            0.5294738,
            places=6,
        )

    def test_refuses_to_guess_across_a_capture_gap(self):
        state = CastSkillSessionState()
        state.mark_capture_gap()
        with self.assertRaisesRegex(RuntimeError, "capture gap"):
            state.build_plan()

    def test_seeded_pair_requires_a_newer_live_cast_instance(self):
        target = 57_243_861_502_373
        state = CastSkillSessionState()
        first = cast_record(
            sequence=140,
            filetime=134_339_599_859_460_080,
            skill_id=86_020_010,
            target_id=target,
            instance_id=10_001_616,
            parameter_6=179.33333276957273,
        )
        second = cast_record(
            sequence=142,
            filetime=134_339_599_864_754_818,
            skill_id=86_020_020,
            target_id=target,
            instance_id=10_001_617,
        )
        state.seed_templates((first, second))
        state.observe(lock_record(target))

        with self.assertRaisesRegex(RuntimeError, "fresh live cast"):
            state.build_plan()

        state.observe(
            cast_record(
                sequence=200,
                filetime=134_339_600_000_000_000,
                skill_id=87_912_035,
                target_id=None,
                instance_id=10_001_618,
            )
        )
        plan = state.build_plan()
        self.assertEqual(plan.steps[0].values[7], 10_001_619)
        self.assertEqual(plan.steps[1].values[7], 10_001_620)

    def test_requires_a_pair_for_the_current_locked_target(self):
        state = CastSkillSessionState()
        state.observe(lock_record(123))
        state.observe(
            cast_record(
                sequence=1,
                filetime=134_339_599_000_000_000,
                skill_id=86_020_010,
                target_id=456,
                instance_id=10_000_001,
            )
        )
        with self.assertRaisesRegex(RuntimeError, "current locked target"):
            state.build_plan()


class ActiveExperimentBoundaryTests(unittest.TestCase):
    def test_active_experiment_is_default_off_and_never_enabled_when_frozen(self):
        self.assertFalse(active_experiment_enabled({}))
        self.assertTrue(active_experiment_enabled({ACTIVE_EXPERIMENT_CONFIG_KEY: True}))
        self.assertFalse(
            active_experiment_enabled(
                {ACTIVE_EXPERIMENT_CONFIG_KEY: True}, frozen=True
            )
        )

    def test_seed_loader_selects_only_a_complete_natural_pair(self):
        target = 57_243_861_502_373
        rows = [
            cast_record(
                sequence=140,
                filetime=134_339_599_859_460_080,
                skill_id=86_020_010,
                target_id=target,
                instance_id=10_001_616,
                parameter_6=179.33333276957273,
            ),
            cast_record(
                sequence=142,
                filetime=134_339_599_864_754_818,
                skill_id=86_020_020,
                target_id=target,
                instance_id=10_001_617,
            ),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "seed.jsonl"
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            selected = load_seed_templates(path)
        self.assertEqual([row["sequence"] for row in selected], [140, 142])

if __name__ == "__main__":
    unittest.main()
