#!/usr/bin/env python3

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import team_stats_request_hook as hook_module
from team_stats_request_hook import (
    LUA_GC64_STRING_TAG,
    LUA_GLOBAL_STRING_TABLE_OFFSET,
    LUA_STRING_TABLE_SEED_OFFSET,
    REQUEST_ARGUMENT_ACK_OFFSET,
    REQUEST_ARGUMENT_RAW_LUA_VALUE,
    REQUEST_ARGUMENT_SYNC_STATE_OFFSET,
    REQUEST_COMMIT_OFFSET,
    REQUEST_CONTEXT_SNAPSHOT_OFFSET,
    REQUEST_CONTEXT_SNAPSHOT_SIZE,
    REQUEST_ENTRY_STACK_CAPTURE_OFFSET,
    REQUEST_RECORD_COUNT,
    REQUEST_RECORD_SIZE,
    REQUEST_RECORDS_OFFSET,
    REQUEST_RING_MAGIC,
    REQUEST_RING_OFFSET,
    REQUEST_STACK_CELLS_OFFSET,
    REQUEST_VARIADIC_SNAPSHOT_OFFSET,
    REQUEST_VARIADIC_SNAPSHOT_SIZE,
    REQUEST_VARIADIC_STORAGE_SNAPSHOT_SIZE,
    SAVED_R8_STACK_OFFSET,
    SAVED_R9_STACK_OFFSET,
    SAVED_RCX_STACK_OFFSET,
    SAVED_RDX_STACK_OFFSET,
    SAVED_RETURN_ADDRESS_STACK_OFFSET,
    STATE_MAGIC,
    STATE_LAST_REQUEST_OFFSET,
    STATE_METHOD_COUNT_OFFSET,
    STATE_METHOD_ARGUMENT_KIND_OFFSET,
    STATE_METHOD_ARGUMENT_VALUE_OFFSET,
    STATE_METHOD_OFFSET,
    STATE_METHOD_STRIDE,
    STATE_METHOD_TEXT_OFFSET,
    STATE_NUMERIC_ORIGINAL_CELL_OFFSET,
    STATE_NUMERIC_SLOT_OFFSET,
    STATE_NUMERIC_VARIADIC_OFFSET,
    STATE_STRING_DESCRIPTOR_OFFSET,
    STATE_STRING_STORAGE_OFFSET,
    STATE_STRING_VECTOR_OFFSET,
    TRAMPOLINE_OFFSET,
    TeamStatsRequestHook,
    build_primary_team_request_stub,
    build_request_stub,
    build_trampoline,
    capture_lua_argument_cells,
    find_lua_string_argument_descriptor,
    lua_sparse_string_hash,
    parse_numeric_request,
    parse_lua_string_request,
    parse_request_record,
    resolve_lua_string_tvalue,
)
from inline_capture import build_absolute_patch
from runtime_capability import load_runtime_profile_file, runtime_profile_hook


RUNTIME_PROFILE = load_runtime_profile_file(
    Path(__file__).resolve().with_name("runtime-profile.dev.json")
)
CALL_SERVER_PROLOGUE = bytes(
    runtime_profile_hook(RUNTIME_PROFILE, "team_stats")["prologue"]
)


def request_record(sequence: int, method: bytes = b"ReqTargetDetail") -> bytes:
    raw = bytearray(REQUEST_RECORD_SIZE)
    struct.pack_into(
        "<8Q",
        raw,
        0,
        sequence,
        134_321_845_000_000_000,
        0x1111,
        0x2222,
        0x3333,
        0x4444,
        0x5555,
        len(method),
    )
    raw[0x40 : 0x40 + len(method)] = method
    raw[
        REQUEST_CONTEXT_SNAPSHOT_OFFSET : REQUEST_CONTEXT_SNAPSHOT_OFFSET
        + REQUEST_CONTEXT_SNAPSHOT_SIZE
    ] = bytes(range(REQUEST_CONTEXT_SNAPSHOT_SIZE))
    raw[
        REQUEST_VARIADIC_SNAPSHOT_OFFSET : REQUEST_VARIADIC_SNAPSHOT_OFFSET
        + REQUEST_VARIADIC_SNAPSHOT_SIZE
    ] = bytes(range(0x80, 0x80 + REQUEST_VARIADIC_SNAPSHOT_SIZE))
    struct.pack_into(
        "<4Q",
        raw,
        REQUEST_STACK_CELLS_OFFSET,
        0xAAAA,
        0xBBBB,
        0xCCCC,
        0xDDDD,
    )
    struct.pack_into("<Q", raw, REQUEST_COMMIT_OFFSET, sequence + 1)
    return bytes(raw)


class TeamStatsRequestHookTests(unittest.TestCase):
    def test_stable_primary_mode_excludes_recorder_and_argument_requests(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            interval=1.0,
            stable_primary_only=True,
        )

        self.assertEqual(
            hook.request_methods,
            (b"ReqCommonCombatStatisticsByTeam",),
        )
        self.assertEqual(hook.request_argument_kinds, (0,))
        self.assertEqual(hook.synchronized_methods, ())
        self.assertEqual(hook.interval, 1.0)

        stub = build_primary_team_request_stub(
            0x1000_0000,
            0x2000_0800,
            0x3000_0000,
            10_000_000,
            prologue=CALL_SERVER_PROLOGUE,
        )
        instructions = list(
            Cs(CS_ARCH_X86, CS_MODE_64).disasm(stub, 0)
        )
        instruction_pairs = {
            (instruction.mnemonic, instruction.op_str)
            for instruction in instructions
        }
        self.assertLess(len(stub), TRAMPOLINE_OFFSET)
        self.assertIn(("mov", "rbx, rcx"), instruction_pairs)
        self.assertNotIn(
            ("mov", "rbx, qword ptr [rsp + 0x48]"),
            instruction_pairs,
        )
        self.assertFalse(
            any("xadd" in instruction.mnemonic for instruction in instructions)
        )
        self.assertFalse(
            any(instruction.mnemonic == "pause" for instruction in instructions)
        )
        self.assertIn(CALL_SERVER_PROLOGUE, stub)

        with self.assertRaises(ValueError):
            TeamStatsRequestHook(
                profile=RUNTIME_PROFILE,
                stable_primary_only=True,
                additional_request_methods=("ReqCommonCombatStatistics",),
            )
        with self.assertRaises(ValueError):
            TeamStatsRequestHook(
                profile=RUNTIME_PROFILE,
                stable_primary_only=True,
                synchronized_methods=("ReqNTP",),
            )

    def test_stable_primary_status_never_reads_outbound_request_ring(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            stable_primary_only=True,
        )
        hook.process = 99
        hook.state = 0x7FF6_0010_0000
        hook.installed = True
        state = bytearray(0x40)
        struct.pack_into("<8sQQQQ", state, 0, STATE_MAGIC, 1, 123, 45, 1)

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(
                hook_module, "read_region", return_value=bytes(state)
            ) as read,
        ):
            status = hook.status()
            requests = hook.poll_requests()

        self.assertEqual(requests, [])
        self.assertEqual(status["request_count"], 45)
        self.assertEqual(
            status["request_methods"],
            ["ReqCommonCombatStatisticsByTeam"],
        )
        self.assertEqual(status["captured_request_count"], 0)
        read.assert_called_once_with(hook.process, hook.state, 0x40)

    def test_attachment_check_reads_only_the_installed_entry_patch(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            stable_primary_only=True,
        )
        hook.process = 99
        hook.target = 0x14012_3000
        hook.code = 0x7FF6_0000_1000
        hook.installed = True
        expected = build_absolute_patch(hook.code, len(hook.prologue))

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(
                hook_module, "read_region", return_value=expected
            ) as read,
        ):
            self.assertTrue(hook.is_attached())

        read.assert_called_once_with(hook.process, hook.target, len(expected))

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(
                hook_module, "read_region", return_value=hook.prologue
            ),
        ):
            self.assertFalse(hook.is_attached())

    def test_rearm_shifts_cadence_without_issuing_a_request(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            stable_primary_only=True,
        )
        hook.process = 99
        hook.state = 0x7FF6_0010_0000
        hook.installed = True
        timestamp = 134_321_845_000_000_000

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module.time, "sleep") as sleep,
            patch.object(hook_module, "write_memory") as write,
        ):
            self.assertTrue(
                hook.rearm_request_schedule(filetime_100ns=timestamp)
            )

        self.assertEqual(
            [call.args for call in write.call_args_list],
            [
                (hook.process, hook.state + 0x08, struct.pack("<Q", 0)),
                (
                    hook.process,
                    hook.state + STATE_LAST_REQUEST_OFFSET,
                    struct.pack("<Q", timestamp),
                ),
                (hook.process, hook.state + 0x08, struct.pack("<Q", 1)),
            ],
        )
        sleep.assert_called_once_with(0.35)

    def test_rearm_reenables_hook_when_clock_write_fails(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            stable_primary_only=True,
        )
        hook.process = 99
        hook.state = 0x7FF6_0010_0000
        hook.installed = True
        writes: list[tuple[int, bytes]] = []

        def fake_write(_process: int, address: int, value: bytes) -> None:
            writes.append((address, value))
            if address == hook.state + STATE_LAST_REQUEST_OFFSET:
                raise RuntimeError("clock write failed")

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module.time, "sleep"),
            patch.object(hook_module, "write_memory", side_effect=fake_write),
        ):
            with self.assertRaisesRegex(RuntimeError, "clock write failed"):
                hook.rearm_request_schedule()

        self.assertEqual(writes[0], (hook.state + 0x08, struct.pack("<Q", 0)))
        self.assertEqual(writes[-1], (hook.state + 0x08, struct.pack("<Q", 1)))

    def test_string_descriptor_discovery_requires_name_and_lua_type(self):
        integer_descriptor = 0x3200_1000
        string_descriptor = integer_descriptor - 0x100
        valid = bytearray(0x80)
        valid[0x10:0x16] = b"string"
        valid[0x50] = 3

        def fake_read(_process: int, address: int, size: int) -> bytes | None:
            self.assertEqual(size, 0x80)
            return bytes(valid) if address == string_descriptor else None

        with patch.object(hook_module, "read_region", side_effect=fake_read):
            self.assertEqual(
                find_lua_string_argument_descriptor(123, (integer_descriptor,)),
                string_descriptor,
            )

        invalid = bytearray(valid)
        invalid[0x50] = 1
        with patch.object(hook_module, "read_region", return_value=bytes(invalid)):
            self.assertEqual(
                find_lua_string_argument_descriptor(123, (integer_descriptor,)),
                0,
            )

    def test_adopts_exact_stale_hook_and_reenables_team_requests(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            enabled=True,
            takeover_existing=True,
        )
        hook.process = 99
        hook.target = 0x14012_3000
        code = 0x7FF6_0000_1000
        state = 0x7FF6_0010_0000
        ring = state + REQUEST_RING_OFFSET
        write_index = 321
        patch_bytes = build_absolute_patch(code, len(CALL_SERVER_PROLOGUE))
        stub = build_request_stub(
            state,
            code + TRAMPOLINE_OFFSET,
            hook.target,
            10_000_000,
            prologue=CALL_SERVER_PROLOGUE,
        )
        trampoline = build_trampoline(
            hook.target, prologue=CALL_SERVER_PROLOGUE
        )
        state_head = struct.pack("<8sQQQQ", STATE_MAGIC, 0, 1, 10, 1)
        ring_head = struct.pack(
            "<8sQQQQ",
            REQUEST_RING_MAGIC,
            write_index,
            REQUEST_RECORD_COUNT,
            REQUEST_RECORD_SIZE,
            hook.target,
        )
        method_head = struct.pack(
            "<QQQQQQ",
            state + STATE_METHOD_TEXT_OFFSET,
            0,
            len(hook.request_method),
            0x7F,
            0,
            0,
        )

        def fake_read(_process: int, address: int, size: int) -> bytes | None:
            if address == code:
                return stub[:size]
            if address == code + TRAMPOLINE_OFFSET:
                return trampoline[:size]
            if address == state:
                return state_head[:size]
            if address == ring:
                return ring_head[:size]
            if address == state + STATE_METHOD_OFFSET:
                return method_head[:size]
            if address == state + STATE_METHOD_COUNT_OFFSET:
                return struct.pack("<Q", 1)[:size]
            if address == state + STATE_METHOD_TEXT_OFFSET:
                return hook.request_method[:size]
            return None

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module, "read_region", side_effect=fake_read),
            patch.object(hook_module, "write_memory") as write,
        ):
            self.assertTrue(hook._adopt_existing(patch_bytes))

        self.assertTrue(hook.installed)
        self.assertTrue(hook.adopted)
        self.assertEqual(hook.code, code)
        self.assertEqual(hook.state, state)
        self.assertEqual(hook.request_next_sequence, write_index)
        write.assert_called_once_with(
            hook.process,
            state + 0x08,
            struct.pack("<Q", 1),
        )

    def test_adopts_exact_stable_primary_hook_without_request_ring(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            enabled=True,
            takeover_existing=True,
            stable_primary_only=True,
        )
        hook.process = 99
        hook.target = 0x14012_3000
        code = 0x7FF6_0000_1000
        state = 0x7FF6_0010_0000
        patch_bytes = build_absolute_patch(code, len(CALL_SERVER_PROLOGUE))
        stub = build_primary_team_request_stub(
            state,
            code + TRAMPOLINE_OFFSET,
            hook.target,
            10_000_000,
            prologue=CALL_SERVER_PROLOGUE,
        )
        trampoline = build_trampoline(
            hook.target, prologue=CALL_SERVER_PROLOGUE
        )
        state_head = bytearray(0x40)
        struct.pack_into(
            "<8sQQQQ",
            state_head,
            0,
            STATE_MAGIC,
            0,
            1,
            10,
            1,
        )
        struct.pack_into("<Q", state_head, STATE_METHOD_COUNT_OFFSET, 1)
        method_head = struct.pack(
            "<QQQQQQ",
            state + STATE_METHOD_TEXT_OFFSET,
            0,
            len(hook.request_method),
            0x2F,
            0,
            0,
        )
        read_addresses: list[int] = []

        def fake_read(_process: int, address: int, size: int) -> bytes | None:
            read_addresses.append(address)
            if address == code:
                return stub[:size]
            if address == code + TRAMPOLINE_OFFSET:
                return trampoline[:size]
            if address == state:
                return bytes(state_head[:size])
            if address == state + STATE_METHOD_OFFSET:
                return method_head[:size]
            if address == state + STATE_METHOD_TEXT_OFFSET:
                return hook.request_method[:size]
            return None

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module, "read_region", side_effect=fake_read),
            patch.object(hook_module, "write_memory") as write,
        ):
            self.assertTrue(hook._adopt_existing(patch_bytes))

        self.assertTrue(hook.installed)
        self.assertTrue(hook.adopted)
        self.assertEqual(hook.code, code)
        self.assertEqual(hook.state, state)
        self.assertNotIn(state + REQUEST_RING_OFFSET, read_addresses)
        write.assert_called_once_with(
            hook.process,
            state + 0x08,
            struct.pack("<Q", 1),
        )

    def test_multiple_requests_are_bounded_deduplicated_and_share_cadence(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            interval=1.0,
            additional_request_methods=(
                "ReqDungeonBattleStatistics",
                "ReqDungeonBattleStatistics",
            ),
        )

        self.assertEqual(
            hook.request_methods,
            (
                b"ReqCommonCombatStatisticsByTeam",
                b"ReqDungeonBattleStatistics",
            ),
        )
        self.assertEqual(hook.request_interval, 1.0)
        self.assertEqual(hook.interval, 0.5)

        stub = build_request_stub(
            0x1000_0000,
            0x2000_0800,
            0x3000_0000,
            5_000_000,
            prologue=CALL_SERVER_PROLOGUE,
            request_method_count=2,
        )
        instructions = {
            (instruction.mnemonic, instruction.op_str)
            for instruction in Cs(CS_ARCH_X86, CS_MODE_64).disasm(stub, 0)
        }
        self.assertIn(
            (
                "imul",
                f"rax, rax, 0x{STATE_METHOD_STRIDE:x}",
            ),
            instructions,
        )
        self.assertIn(
            (
                "mov",
                "rax, qword ptr [r13 + 0x30]",
            ),
            instructions,
        )

    def test_numeric_request_argument_is_an_exact_lua_number(self):
        player_id = 57_443_044_343_701
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            additional_request_methods=("ReqCommonCombatStatistics",),
            numeric_request_arguments={
                "ReqCommonCombatStatistics": player_id,
            },
        )

        self.assertEqual(
            hook.request_methods,
            (
                b"ReqCommonCombatStatisticsByTeam",
                b"ReqCommonCombatStatistics",
            ),
        )
        self.assertIsNone(hook.request_argument_bits[0])
        self.assertEqual(
            hook.request_argument_bits[1],
            struct.unpack("<Q", struct.pack("<d", float(player_id)))[0],
        )
        self.assertEqual(
            parse_numeric_request("ReqCommonCombatStatistics=57443044343701"),
            ("ReqCommonCombatStatistics", player_id),
        )

        with self.assertRaises(ValueError):
            TeamStatsRequestHook(
                profile=RUNTIME_PROFILE,
                numeric_request_arguments={"ReqMissing": player_id},
            )
        with self.assertRaises(ValueError):
            TeamStatsRequestHook._numeric_argument_bits(True)
        with self.assertRaises(ValueError):
            TeamStatsRequestHook._numeric_argument_bits(2**53 + 1)

    def test_raw_lua_string_request_keeps_the_exact_tagged_cell(self):
        raw_value = LUA_GC64_STRING_TAG | 0x13C4_FA3F8
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            additional_request_methods=("ReqCommonCombatStatistics",),
            raw_lua_request_arguments={
                "ReqCommonCombatStatistics": raw_value,
            },
        )

        self.assertEqual(
            hook.request_argument_kinds,
            (0, REQUEST_ARGUMENT_RAW_LUA_VALUE),
        )
        self.assertEqual(hook.request_argument_bits, (None, raw_value))
        self.assertEqual(
            parse_lua_string_request("ReqCommonCombatStatistics=AQAAAOwNKLYHAAAA"),
            ("ReqCommonCombatStatistics", "AQAAAOwNKLYHAAAA"),
        )
        with self.assertRaises(ValueError):
            TeamStatsRequestHook._raw_lua_argument_bits(0x13C4_FA3F8)

        stub = build_request_stub(
            0x1000_0000,
            0x2000_0800,
            0x3000_0000,
            5_000_000,
            prologue=CALL_SERVER_PROLOGUE,
            request_method_count=2,
        )
        instructions = {
            (instruction.mnemonic, instruction.op_str)
            for instruction in Cs(CS_ARCH_X86, CS_MODE_64).disasm(stub, 0)
        }
        self.assertIn(
            (
                "cmp",
                f"qword ptr [r13 + 0x{STATE_STRING_DESCRIPTOR_OFFSET:x}], 0",
            ),
            instructions,
        )
        self.assertIn(
            ("movabs", f"rdi, 0x{0x1000_0000 + STATE_STRING_STORAGE_OFFSET:x}"),
            instructions,
        )
        self.assertIn(
            ("movabs", f"rax, 0x{0x1000_0000 + STATE_STRING_VECTOR_OFFSET:x}"),
            instructions,
        )

    def test_resolves_an_interned_lua_string_from_its_primary_hash_bucket(self):
        token = b"AQAAAOwNKLYHAAAA"
        seed = 0xEFBEB877_4E61C911
        expected_hash = 0xADB03168
        self.assertEqual(lua_sparse_string_hash(seed, token), expected_hash)

        lua_state = 0xF5EA_0380
        global_state = 0xF5EA_03F8
        buckets = 0x7FF4_DCB6_0010
        mask = 0x7FFFF
        string_address = 0x13C4_FA3F8
        state = bytearray(0x18)
        state[9] = 6
        struct.pack_into("<Q", state, 0x10, global_state)
        string_state = bytearray(LUA_STRING_TABLE_SEED_OFFSET + 8)
        struct.pack_into("<QII", string_state, 0, buckets, mask, 123)
        struct.pack_into("<Q", string_state, LUA_STRING_TABLE_SEED_OFFSET, seed)
        header = bytearray(0x18)
        header[9] = 4
        struct.pack_into("<II", header, 0x10, expected_hash, len(token))
        bucket_address = buckets + (expected_hash & mask) * 8

        def fake_read(_process: int, address: int, size: int) -> bytes | None:
            values = {
                lua_state: bytes(state),
                global_state + LUA_GLOBAL_STRING_TABLE_OFFSET: bytes(string_state),
                bucket_address: struct.pack("<Q", string_address),
                string_address: bytes(header),
                string_address + 0x18: token,
            }
            value = values.get(address)
            self.assertIsNotNone(value, f"unexpected read at 0x{address:x}")
            assert value is not None
            self.assertEqual(len(value), size)
            return value

        with patch.object(hook_module, "read_region", side_effect=fake_read):
            raw_value = resolve_lua_string_tvalue(123, lua_state, token.decode("ascii"))

        self.assertEqual(raw_value, LUA_GC64_STRING_TAG | string_address)

    def test_takeover_close_restores_an_adopted_team_hook(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            takeover_existing=True,
        )
        hook.pid = 1234
        hook.process = 99
        hook.target = 0x14012_3000
        hook.state = 0x7FF6_0010_0000
        hook.code = 0x7FF6_0000_1000
        hook.installed = True
        hook.adopted = True
        expected_patch = build_absolute_patch(
            hook.code, len(CALL_SERVER_PROLOGUE)
        )

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module, "suspend_game_threads", return_value=[7]),
            patch.object(hook_module, "resume_threads") as resume,
            patch.object(hook_module, "write_code") as write,
            patch.object(
                hook_module,
                "read_region",
                side_effect=(expected_patch, CALL_SERVER_PROLOGUE),
            ),
            patch.object(hook_module.kernel32, "CloseHandle"),
        ):
            hook.close()

        write.assert_called_once_with(
            99, hook.target, CALL_SERVER_PROLOGUE
        )
        resume.assert_called_once_with([7])

    def test_close_does_not_rewrite_an_already_detached_entry(self):
        hook = TeamStatsRequestHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            takeover_existing=True,
        )
        hook.pid = 1234
        hook.process = 99
        hook.target = 0x14012_3000
        hook.state = 0x7FF6_0010_0000
        hook.code = 0x7FF6_0000_1000
        hook.installed = True

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(
                hook_module,
                "read_region",
                return_value=CALL_SERVER_PROLOGUE,
            ),
            patch.object(hook_module, "suspend_game_threads") as suspend,
            patch.object(hook_module, "write_code") as write,
            patch.object(hook_module.kernel32, "CloseHandle"),
        ):
            hook.close()

        suspend.assert_not_called()
        write.assert_not_called()
        self.assertFalse(hook.installed)

    def test_raw_record_parser_preserves_every_captured_field(self):
        record = parse_request_record(request_record(7), 7)

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["sequence"], 7)
        self.assertEqual(record["direction"], "outbound")
        self.assertEqual(record["capture_source"], "raw_outbound_rpc")
        self.assertEqual(record["script_entity"], 0x1111)
        self.assertEqual(record["context"], 0x2222)
        self.assertEqual(record["method_object"], 0x3333)
        self.assertEqual(record["variadic_arguments"], 0x4444)
        self.assertEqual(record["return_address"], "0x0000000000005555")
        self.assertEqual(record["method"], "ReqTargetDetail")
        self.assertEqual(record["method_length"], len(b"ReqTargetDetail"))
        self.assertEqual(
            record["context_snapshot"],
            bytes(range(REQUEST_CONTEXT_SNAPSHOT_SIZE)).hex(),
        )
        self.assertEqual(
            record["variadic_snapshot"],
            bytes(range(0x80, 0x80 + REQUEST_VARIADIC_SNAPSHOT_SIZE)).hex(),
        )
        self.assertEqual(
            record["entry_stack_offsets"], ["0x20", "0x28", "0x30", "0x38"]
        )
        self.assertEqual(
            record["entry_stack_cells"], [0xAAAA, 0xBBBB, 0xCCCC, 0xDDDD]
        )

    def test_record_parser_rejects_uncommitted_or_wrong_sequence_slots(self):
        raw = bytearray(request_record(4))
        struct.pack_into("<Q", raw, REQUEST_COMMIT_OFFSET, 0)
        self.assertIsNone(parse_request_record(bytes(raw), 4))
        self.assertIsNone(parse_request_record(request_record(4), 5))

    def test_record_parser_exposes_variadic_shape_and_argument_count(self):
        raw = bytearray(request_record(8))
        storage_address = 0x5A58_C65A0
        struct.pack_into(
            "<3Q",
            raw,
            REQUEST_VARIADIC_SNAPSHOT_OFFSET,
            0xF3C5_0380,
            storage_address,
            (10 << 32) | 3,
        )

        record = parse_request_record(bytes(raw), 8)

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["variadic_storage_address"], storage_address)
        self.assertEqual(record["variadic_argument_start"], 3)
        self.assertEqual(record["variadic_argument_end"], 10)
        self.assertEqual(record["variadic_argument_count"], 8)
        self.assertEqual(record["variadic_qwords"][2], (10 << 32) | 3)

        struct.pack_into(
            "<Q",
            raw,
            REQUEST_VARIADIC_SNAPSHOT_OFFSET + 16,
            0xFFFF_FFFF_FFFF_FFFF,
        )
        no_arguments = parse_request_record(bytes(raw), 8)
        self.assertIsNotNone(no_arguments)
        assert no_arguments is not None
        self.assertEqual(no_arguments["variadic_argument_start"], -1)
        self.assertEqual(no_arguments["variadic_argument_end"], -1)
        self.assertEqual(no_arguments["variadic_argument_count"], 0)

    def test_synchronized_capture_reads_exact_lua_argument_range(self):
        lua_state = 0x4200_0000
        stack_base = 0x4300_0000
        record = {
            "lua_state_address": lua_state,
            "variadic_argument_start": 3,
            "variadic_argument_count": 2,
        }
        state = bytearray(0x30)
        struct.pack_into("<2Q", state, 0x20, stack_base, stack_base + 5 * 8)
        cells = struct.pack("<2d", 57_415_122_813_416.0, 12.5)

        def fake_read_region(_process: int, address: int, size: int) -> bytes:
            if address == lua_state:
                self.assertEqual(size, 0x30)
                return bytes(state)
            self.assertEqual(address, stack_base + 2 * 8)
            self.assertEqual(size, len(cells))
            return cells

        with patch.object(hook_module, "read_region", side_effect=fake_read_region):
            capture_lua_argument_cells(123, record)

        self.assertEqual(record["lua_argument_indices"], [3, 4])
        self.assertEqual(record["lua_argument_numbers"], [57_415_122_813_416, 12.5])
        self.assertEqual(record["lua_argument_capture"], "hook_entry_synchronized")

    def test_stub_disassembly_proves_original_argument_and_stack_offsets(self):
        stub = build_request_stub(
            0x1000_0000,
            0x2000_0800,
            0x3000_0000,
            10_000_000,
            prologue=CALL_SERVER_PROLOGUE,
        )

        self.assertLess(len(stub), TRAMPOLINE_OFFSET)
        instructions = {
            (instruction.mnemonic, instruction.op_str)
            for instruction in Cs(CS_ARCH_X86, CS_MODE_64).disasm(stub, 0)
        }
        for offset in (
            SAVED_RCX_STACK_OFFSET,
            SAVED_RDX_STACK_OFFSET,
            SAVED_R8_STACK_OFFSET,
            SAVED_R9_STACK_OFFSET,
            SAVED_RETURN_ADDRESS_STACK_OFFSET,
        ):
            self.assertIn(("mov", f"rax, qword ptr [rsp + 0x{offset:x}]"), instructions)
        self.assertIn(
            (
                "lea",
                f"rsi, [rsp + 0x{REQUEST_ENTRY_STACK_CAPTURE_OFFSET:x}]",
            ),
            instructions,
        )
        self.assertIn(("mov", "rbx, qword ptr [rsp + 0x48]"), instructions)
        self.assertIn(("mov", "rsi, rdx"), instructions)
        self.assertIn(("rep stosq", "qword ptr [rdi], rax"), instructions)
        self.assertIn(("mov", "rax, qword ptr [rsp + 0x60]"), instructions)
        for source_offset, target_offset in zip(
            (0xD0, 0xD8, 0xE0, 0xE8),
            (0x20, 0x28, 0x30, 0x38),
        ):
            self.assertIn(
                (
                    "mov",
                    f"rax, qword ptr [rsp + 0x{source_offset:x}]",
                ),
                instructions,
            )
            self.assertIn(
                (
                    "mov",
                    f"qword ptr [rsp + 0x{target_offset:x}], rax",
                ),
                instructions,
            )
        self.assertNotIn(("mov", "rax, qword ptr [rsp + 0xc8]"), instructions)
        self.assertIn(("lea", "rdx, [rsi + r9*8 - 8]"), instructions)
        self.assertIn(
            (
                "mov",
                f"qword ptr [r13 + 0x{STATE_NUMERIC_SLOT_OFFSET:x}], rdx",
            ),
            instructions,
        )
        self.assertIn(
            (
                "mov",
                "rax, qword ptr [r8 + " f"0x{STATE_METHOD_ARGUMENT_VALUE_OFFSET:x}]",
            ),
            instructions,
        )
        self.assertIn(
            (
                "movabs",
                f"r9, 0x{0x1000_0000 + STATE_NUMERIC_VARIADIC_OFFSET:x}",
            ),
            instructions,
        )
        self.assertIn(
            (
                "mov",
                "rax, qword ptr [r13 + "
                f"0x{STATE_NUMERIC_ORIGINAL_CELL_OFFSET:x}]",
            ),
            instructions,
        )
        self.assertIn(CALL_SERVER_PROLOGUE, stub)

    def test_stub_can_synchronize_one_named_request_without_changing_calls(self):
        stub = build_request_stub(
            0x1000_0000,
            0x2000_0800,
            0x3000_0000,
            10_000_000,
            prologue=CALL_SERVER_PROLOGUE,
            synchronized_methods=(b"ReqNTP",),
        )

        self.assertLess(len(stub), TRAMPOLINE_OFFSET)
        # The exact name is compared through fixed 4-byte and 2-byte
        # immediates, so it is intentionally not stored as one C string.
        self.assertIn(b"ReqN", stub)
        self.assertIn(b"TP", stub)
        instructions = {
            (instruction.mnemonic, instruction.op_str)
            for instruction in Cs(CS_ARCH_X86, CS_MODE_64).disasm(stub, 0)
        }
        self.assertIn(
            (
                "cmp",
                "qword ptr [r13 + "
                f"0x{REQUEST_ARGUMENT_SYNC_STATE_OFFSET:x}], 1",
            ),
            instructions,
        )
        self.assertIn(("pause", ""), instructions)
        self.assertIn(CALL_SERVER_PROLOGUE, stub)

    def test_polling_is_ordered_and_accounts_for_ring_overflow(self):
        hook = object.__new__(TeamStatsRequestHook)
        hook.installed = True
        hook.process = 123
        hook.state = 0x1000_0000
        hook.target = 0x2000_0000
        hook.request_next_sequence = 10
        hook.request_dropped_count = 0
        write_index = 10 + REQUEST_RECORD_COUNT + 3
        ring = hook.state + REQUEST_RING_OFFSET
        header = struct.pack(
            "<8sQQQQ",
            REQUEST_RING_MAGIC,
            write_index,
            REQUEST_RECORD_COUNT,
            REQUEST_RECORD_SIZE,
            hook.target,
        )

        def fake_read_region(_process: int, address: int, size: int) -> bytes:
            if address == ring:
                return header
            self.assertEqual(size, REQUEST_RECORD_SIZE)
            relative = address - ring - REQUEST_RECORDS_OFFSET
            self.assertGreaterEqual(relative, 0)
            return bytes(REQUEST_RECORD_SIZE)

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module, "read_region", side_effect=fake_read_region),
            patch.object(
                hook_module,
                "parse_request_record",
                side_effect=lambda _raw, expected: {"sequence": expected},
            ),
        ):
            records = hook.poll_requests()

        self.assertEqual(hook.request_dropped_count, 3)
        self.assertEqual(len(records), REQUEST_RECORD_COUNT)
        self.assertEqual(records[0]["sequence"], 13)
        self.assertEqual(records[-1]["sequence"], write_index - 1)
        self.assertEqual(hook.request_next_sequence, write_index)

    def test_polling_reads_variadic_storage_outside_game_code(self):
        hook = object.__new__(TeamStatsRequestHook)
        hook.installed = True
        hook.process = 123
        hook.state = 0x1000_0000
        hook.target = 0x2000_0000
        hook.request_next_sequence = 0
        hook.request_dropped_count = 0
        ring = hook.state + REQUEST_RING_OFFSET
        header = struct.pack(
            "<8sQQQQ",
            REQUEST_RING_MAGIC,
            1,
            REQUEST_RECORD_COUNT,
            REQUEST_RECORD_SIZE,
            hook.target,
        )
        storage_address = 0x5A58_C65A0
        raw_record = bytearray(request_record(0))
        struct.pack_into(
            "<3Q",
            raw_record,
            REQUEST_VARIADIC_SNAPSHOT_OFFSET,
            0xF3C5_0380,
            storage_address,
            (3 << 32) | 3,
        )
        storage = bytes(index & 0xFF for index in range(0x200))

        def fake_read_region(_process: int, address: int, size: int) -> bytes:
            if address == ring:
                return header
            if address == storage_address:
                self.assertEqual(size, REQUEST_VARIADIC_STORAGE_SNAPSHOT_SIZE)
                return storage
            self.assertEqual(
                address,
                ring + REQUEST_RECORDS_OFFSET,
            )
            self.assertEqual(size, REQUEST_RECORD_SIZE)
            return bytes(raw_record)

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module, "read_region", side_effect=fake_read_region),
        ):
            records = hook.poll_requests()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["variadic_argument_count"], 1)
        self.assertEqual(records[0]["variadic_storage_snapshot"], storage.hex())
        self.assertEqual(
            records[0]["variadic_storage_capture"],
            "post_call_readprocessmemory",
        )

    def test_polling_reads_live_variadic_storage_before_acknowledgement(self):
        hook = object.__new__(TeamStatsRequestHook)
        hook.installed = True
        hook.process = 123
        hook.state = 0x1000_0000
        hook.target = 0x2000_0000
        hook.request_next_sequence = 0
        hook.request_dropped_count = 0
        ring = hook.state + REQUEST_RING_OFFSET
        header = struct.pack(
            "<8sQQQQ",
            REQUEST_RING_MAGIC,
            1,
            REQUEST_RECORD_COUNT,
            REQUEST_RECORD_SIZE,
            hook.target,
        )
        storage_address = 0x5A58_C65A0
        raw_record = bytearray(request_record(0, b"ReqNTP"))
        struct.pack_into(
            "<3Q",
            raw_record,
            REQUEST_VARIADIC_SNAPSHOT_OFFSET,
            0xF3C5_0380,
            storage_address,
            (3 << 32) | 3,
        )
        struct.pack_into(
            "<Q", raw_record, REQUEST_ARGUMENT_SYNC_STATE_OFFSET, 1
        )
        storage = bytes(index & 0xFF for index in range(0x200))

        def fake_read_region(_process: int, address: int, size: int) -> bytes:
            if address == ring:
                return header
            if address == storage_address:
                return storage
            if address == 0xF3C5_0380:
                self.assertEqual(size, 0x30)
                return None
            self.assertEqual(address, ring + REQUEST_RECORDS_OFFSET)
            return bytes(raw_record)

        with (
            patch.object(hook_module, "process_alive", return_value=True),
            patch.object(hook_module, "read_region", side_effect=fake_read_region),
            patch.object(hook_module, "write_memory") as write,
        ):
            records = hook.poll_requests()

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["variadic_storage_capture"],
            "hook_entry_synchronized",
        )
        write.assert_called_once_with(
            hook.process,
            ring
            + REQUEST_RECORDS_OFFSET
            + REQUEST_ARGUMENT_ACK_OFFSET,
            struct.pack("<Q", 1),
        )


if __name__ == "__main__":
    unittest.main()
