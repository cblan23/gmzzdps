#!/usr/bin/env python3

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import team_stats_request_hook as hook_module
from runtime_capability import load_runtime_profile_file, runtime_profile_hook
from team_stats_request_hook import (
    ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE,
    PRIMITIVE_REPLAY_METHOD,
    PRIMITIVE_REPLAY_SCHEMA,
    REPLAY_ARMED,
    STATE_REPLAY_ARM_OFFSET,
    STATE_REPLAY_CELLS_OFFSET,
    STATE_REPLAY_CONTROL_OFFSET,
    STATE_REPLAY_DESCRIPTOR_VECTOR_OFFSET,
    STATE_REPLAY_STORAGE_OFFSET,
    TRAMPOLINE_OFFSET,
    TeamStatsRequestHook,
    build_request_stub,
)


RUNTIME_PROFILE = load_runtime_profile_file(
    Path(__file__).resolve().with_name("runtime-profile.dev.json")
)
CALL_SERVER_PROLOGUE = bytes(
    runtime_profile_hook(RUNTIME_PROFILE, "team_stats")["prologue"]
)


def descriptor(name: str, lua_type: int) -> bytes:
    raw = bytearray(ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE)
    encoded = name.encode("ascii")
    raw[0x10 : 0x10 + len(encoded)] = encoded
    raw[0x50] = lua_type
    return bytes(raw)


def replay_record() -> tuple[dict, list[object], int, int, int, int]:
    script_entity = 0x12_3456_7000
    lua_state = 0xF6AB_0380
    int_descriptor = 0x6C16_29D0
    float_descriptor = 0x6C16_8770
    values: list[object] = [
        86_020_010,
        57_243_861_502_373,
        -93.5,
        -14.0,
        -1026.46,
        179.333,
        None,
        10_001_618,
    ]
    cells = [
        0xFFFF_FFFF_FFFF_FFFF
        if value is None
        else struct.unpack("<Q", struct.pack("<d", float(value)))[0]
        for value in values
    ]
    storage = bytearray(0x200)
    storage[0x10 : 0x10 + len(b"ReqCastSkillNew")] = b"ReqCastSkillNew"
    items = []
    for name, lua_type in PRIMITIVE_REPLAY_SCHEMA:
        items.append(
            {
                "address": int_descriptor if name == "int" else float_descriptor,
                "type_name": name,
                "lua_type": lua_type,
            }
        )
    return (
        {
            "method": PRIMITIVE_REPLAY_METHOD,
            "sequence": 140,
            "filetime_100ns": 134_339_599_859_460_080,
            "script_entity": script_entity,
            "lua_state_address": lua_state,
            "variadic_argument_count": 8,
            "argument_sync_state": 1,
            "lua_argument_capture": "hook_entry_synchronized",
            "variadic_storage_capture": "hook_entry_synchronized",
            "lua_argument_cells": cells,
            "argument_descriptor_vector": {"items": items},
            "variadic_storage_snapshot": bytes(storage).hex(),
        },
        values,
        script_entity,
        lua_state,
        int_descriptor,
        float_descriptor,
    )


class PrimitiveReplayStubTests(unittest.TestCase):
    def test_opt_in_stub_is_fully_decodable_and_restores_lua_top(self):
        stub = build_request_stub(
            0x1000_0000,
            0x2000_0800,
            0x3000_0000,
            3_000_000_000,
            prologue=CALL_SERVER_PROLOGUE,
            request_method_count=2,
            synchronized_methods=(b"ReqReportLockTarget", b"ReqCastSkillNew"),
            primitive_replay_enabled=True,
        )
        instructions = list(Cs(CS_ARCH_X86, CS_MODE_64).disasm(stub, 0))
        rendered = {(item.mnemonic, item.op_str) for item in instructions}

        self.assertLess(len(stub), TRAMPOLINE_OFFSET)
        self.assertEqual(sum(item.size for item in instructions), len(stub))
        self.assertIn(("mov", "rdx, qword ptr [rcx + 0x30]"), rendered)
        self.assertIn(
            (
                "lock cmpxchg",
                f"qword ptr [r13 + 0x{STATE_REPLAY_ARM_OFFSET:x}], rdx",
            ),
            rendered,
        )
        self.assertIn(("mov", "qword ptr [rax + 0x28], rdi"), rendered)
        self.assertIn(("mov", "qword ptr [r10 + 0x28], rax"), rendered)

    def test_default_stub_does_not_contain_the_replay_control_address(self):
        state = 0x1000_0000
        stub = build_request_stub(
            state,
            0x2000_0800,
            0x3000_0000,
            10_000_000,
            prologue=CALL_SERVER_PROLOGUE,
        )
        self.assertNotIn(
            struct.pack("<Q", state + STATE_REPLAY_CONTROL_OFFSET), stub
        )


class PrimitiveReplayControllerTests(unittest.TestCase):
    def new_hook(self) -> TeamStatsRequestHook:
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            enabled=False,
            additional_request_methods=(PRIMITIVE_REPLAY_METHOD,),
            synchronized_methods=("ReqCastSkillNew",),
            allow_existing_adoption=False,
            primitive_replay_enabled=True,
        )
        hook.process = 99
        hook.state = 0x1000_0000
        hook.installed = True
        return hook

    def test_active_scheduler_and_replay_cannot_be_enabled_together(self):
        with self.assertRaisesRegex(ValueError, "scheduled request branch disabled"):
            TeamStatsRequestHook(
                profile=RUNTIME_PROFILE,
                enabled=True,
                additional_request_methods=(PRIMITIVE_REPLAY_METHOD,),
                primitive_replay_enabled=True,
            )

    def test_prepare_reencodes_primitives_and_revalidates_live_descriptors(self):
        hook = self.new_hook()
        record, values, script_entity, lua_state, int_address, float_address = (
            replay_record()
        )
        int_snapshot = descriptor("int", 1)
        float_snapshot = descriptor("float", 2)
        lua_snapshot = bytearray(0x38)
        struct.pack_into(
            "<3Q", lua_snapshot, 0x20, 0x5000_0000, 0x5000_0100, 0x5000_1000
        )
        control = bytes(0x50)

        def read(_process: int, address: int, size: int):
            if address == script_entity and size == 8:
                return bytes(8)
            if address == lua_state and size == 0x38:
                return bytes(lua_snapshot)
            if address == int_address and size == ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE:
                return int_snapshot
            if address == float_address and size == ARGUMENT_DESCRIPTOR_SNAPSHOT_SIZE:
                return float_snapshot
            if address == hook.state + STATE_REPLAY_CONTROL_OFFSET and size == 0x50:
                return control
            raise AssertionError(f"unexpected read 0x{address:x} size={size}")

        writes = []
        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module, "read_region", side_effect=read),
            patch.object(
                hook_module,
                "write_memory",
                side_effect=lambda process, address, data: writes.append(
                    (process, address, data)
                ),
            ),
        ):
            prepared = hook.prepare_primitive_replay(record, values)

        by_address = {address: data for _process, address, data in writes}
        encoded = struct.unpack(
            "<8Q", by_address[hook.state + STATE_REPLAY_CELLS_OFFSET]
        )
        self.assertEqual(encoded[6], 0xFFFF_FFFF_FFFF_FFFF)
        self.assertEqual(
            struct.unpack("<d", struct.pack("<Q", encoded[0]))[0],
            86_020_010.0,
        )
        self.assertEqual(prepared["values"], tuple(values))
        self.assertIn(hook.state + STATE_REPLAY_STORAGE_OFFSET, by_address)
        self.assertIn(
            hook.state + STATE_REPLAY_DESCRIPTOR_VECTOR_OFFSET, by_address
        )

    def test_prepare_rejects_a_captured_gc_reference(self):
        hook = self.new_hook()
        record, values, *_rest = replay_record()
        record["lua_argument_cells"][3] = 0xFFFD_8001_2345_6789
        with (
            patch.object(hook_module, "process_alive", return_value=True),
            self.assertRaisesRegex(ValueError, "GC/reference TValue"),
        ):
            hook.prepare_primitive_replay(record, values)

    def test_arm_publishes_only_the_one_shot_flag_after_preparation(self):
        hook = self.new_hook()
        hook.prepared_primitive_replay = {"method": PRIMITIVE_REPLAY_METHOD}
        control = struct.pack("<10Q", 0, 7, 0, 1, 2, 8, 3, 0, 0, 0)
        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module, "read_region", return_value=control),
            patch.object(hook_module, "write_memory") as write,
        ):
            baseline = hook.arm_primitive_replay()

        self.assertEqual(baseline, 7)
        write.assert_called_once_with(
            hook.process,
            hook.state + STATE_REPLAY_ARM_OFFSET,
            struct.pack("<Q", REPLAY_ARMED),
        )


if __name__ == "__main__":
    unittest.main()
