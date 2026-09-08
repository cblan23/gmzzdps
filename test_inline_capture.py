import struct
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from damage_hook import DamageHook
from inline_capture import (
    BOSS_TEMPLATE_ID_OFFSET,
    BOSS_TYPE_ENTITY_ID_OFFSET,
    BOSS_TYPE_FIELD_OFFSET,
    BOSS_TYPE_RECORD_SIZE,
    build_boss_init_stub,
    build_boss_type_stub,
    build_template_bulk_stub,
    build_template_id_stub,
    parse_boss_type_record,
)
from network_capture import (
    MESSAGE_ARGUMENT_ACK_OFFSET,
    MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET,
    MESSAGE_ARGUMENT_SYNC_STATE_OFFSET,
    MESSAGE_COMMIT_OFFSET,
    MESSAGE_MAGIC,
    MESSAGE_RECORD_COUNT,
    MESSAGE_RECORDS_OFFSET,
    MESSAGE_RECORD_SIZE,
    NetworkMessageHook,
    RemoteMsgpackReader,
    build_absolute_patch,
    build_message_stub,
    parse_message_record,
)
from network_state import NetworkPacketParser
from runtime_capability import load_runtime_profile_file, runtime_profile_hook


RUNTIME_PROFILE = load_runtime_profile_file(
    Path(__file__).resolve().with_name("runtime-profile.dev.json")
)
NETWORK_HOOK = runtime_profile_hook(RUNTIME_PROFILE, "network_message")
BOSS_TYPE_HOOK = runtime_profile_hook(RUNTIME_PROFILE, "boss_type")
BOSS_INIT_HOOK = runtime_profile_hook(RUNTIME_PROFILE, "boss_init")
TEMPLATE_ID_HOOK = runtime_profile_hook(RUNTIME_PROFILE, "template_id")
TEMPLATE_BULK_HOOK = runtime_profile_hook(RUNTIME_PROFILE, "template_bulk")
MESSAGE_PROLOGUE = bytes(NETWORK_HOOK["prologue"])
BOSS_TYPE_PROLOGUE = bytes(BOSS_TYPE_HOOK["prologue"])
BOSS_INIT_PROLOGUE = bytes(BOSS_INIT_HOOK["prologue"])
TEMPLATE_ID_PROLOGUE = bytes(TEMPLATE_ID_HOOK["prologue"])
TEMPLATE_BULK_PROLOGUE = bytes(TEMPLATE_BULK_HOOK["prologue"])
MESSAGE_ARGUMENT_SYNC_METHOD_SEQUENCE = tuple(
    RUNTIME_PROFILE["protocol"]["synchronized_methods"]
)
MESSAGE_ARGUMENT_SYNC_METHOD_NAMES = frozenset(
    MESSAGE_ARGUMENT_SYNC_METHOD_SEQUENCE
)
MESSAGE_ARGUMENT_SYNC_METHODS = tuple(
    method.encode("ascii") for method in MESSAGE_ARGUMENT_SYNC_METHOD_SEQUENCE
)


def message_stub(ring: int, resume: int, **options) -> bytes:
    return build_message_stub(
        ring,
        resume,
        prologue=options.pop("prologue", MESSAGE_PROLOGUE),
        synchronized_methods=MESSAGE_ARGUMENT_SYNC_METHODS,
        **options,
    )


class BossTypeCaptureTests(unittest.TestCase):
    def test_all_synchronous_methods_fit_in_the_injected_stub(self):
        stub = message_stub(0x1234_0000, 0x1400_1000)

        self.assertLess(len(stub), 0x1000)

    def test_entity_death_is_pointer_only_and_never_blocks_game_thread(self):
        self.assertNotIn(b"OnMsgEntityDead", MESSAGE_ARGUMENT_SYNC_METHODS)
        self.assertNotIn("OnMsgEntityDead", MESSAGE_ARGUMENT_SYNC_METHOD_NAMES)
        self.assertEqual(
            MESSAGE_ARGUMENT_SYNC_METHOD_NAMES,
            frozenset(method.decode("ascii") for method in MESSAGE_ARGUMENT_SYNC_METHODS),
        )

    def test_message_stub_can_chain_after_the_displaced_rbp_push(self):
        secondary_prologue = bytes.fromhex(
            "57 41 54 41 56 41 57 48 8b ec 48 81 ec 80 00 00 00"
        )
        resume = 0x1400_2000
        stub = message_stub(
            0x1234_0000,
            resume,
            prologue=secondary_prologue,
            return_address_stack_offset=0x50,
        )

        self.assertIn(b"\x48\x8b\x44\x24\x50\x49\x89\x43\x30", stub)
        self.assertTrue(
            stub.endswith(
                secondary_prologue
                + b"\xff\x25\x00\x00\x00\x00"
                + struct.pack("<Q", resume)
            )
        )

    def test_only_low_frequency_encounter_edges_join_team_stat_sync(self):
        self.assertTrue(
            {
                "OnMsgSyncFightMode",
                "OnMsgSyncCurrentMaxHp",
            }.issubset(MESSAGE_ARGUMENT_SYNC_METHOD_NAMES)
        )
        self.assertTrue(
            {
                "OnMsgBeatenSyncV2",
                "OnMsgSyncCurrentHp",
                "OnMsgEntityDead",
            }.isdisjoint(MESSAGE_ARGUMENT_SYNC_METHOD_NAMES)
        )

    def test_network_record_exposes_argument_sync_state(self):
        sequence = 9
        raw = bytearray(MESSAGE_RECORD_SIZE)
        struct.pack_into(
            "<8Q",
            raw,
            0,
            sequence,
            134_321_845_085_764_054,
            1,
            2,
            3,
            4,
            5,
            len("RetCommonCombatStatisticsByTeam"),
        )
        raw[0x40 : 0x40 + 31] = b"RetCommonCombatStatisticsByTeam"
        struct.pack_into("<Q", raw, MESSAGE_ARGUMENT_SYNC_STATE_OFFSET, 1)
        struct.pack_into("<Q", raw, MESSAGE_COMMIT_OFFSET, sequence + 1)

        record = parse_message_record(bytes(raw), sequence)

        self.assertEqual(record["argument_sync_state"], 1)

    def test_authoritative_team_record_is_acknowledged_after_decode(self):
        hook = NetworkMessageHook(profile=RUNTIME_PROFILE, pid=1234)
        hook.process = 99
        hook.ring = 0x500000
        hook.installed = True
        hook.next_sequence = 0
        method = MESSAGE_ARGUMENT_SYNC_METHODS[0]
        record_address = hook.ring + MESSAGE_RECORDS_OFFSET
        raw = bytearray(MESSAGE_RECORD_SIZE)
        struct.pack_into(
            "<8Q",
            raw,
            0,
            0,
            134_321_845_085_764_054,
            1,
            2,
            3,
            0x600000,
            5,
            len(method),
        )
        raw[0x40 : 0x40 + len(method)] = method
        struct.pack_into("<Q", raw, MESSAGE_ARGUMENT_SYNC_STATE_OFFSET, 1)
        struct.pack_into("<Q", raw, MESSAGE_COMMIT_OFFSET, 1)
        writes = []

        def fake_read(_process, address, size):
            if address == hook.ring and size == 0x30:
                return struct.pack("<8sQQQQQ", MESSAGE_MAGIC, 1, 0, 0, 0, 1)
            if address == record_address:
                return bytes(raw)
            return None

        with (
            patch("network_capture.process_alive", return_value=True),
            patch("network_capture.read_region", side_effect=fake_read),
            patch("network_capture.RemoteMsgpackReader.decode", return_value=[]),
            patch(
                "network_capture.write_memory",
                side_effect=lambda process, address, data: writes.append(
                    (process, address, data)
                ),
            ),
        ):
            records = hook.poll(decode_arguments=True)

        self.assertTrue(records[0]["arguments_synchronized"])
        self.assertEqual(
            writes,
            [
                (
                    99,
                    record_address + MESSAGE_ARGUMENT_ACK_OFFSET,
                    struct.pack("<Q", 1),
                )
            ],
        )

    def test_timed_out_team_record_is_never_decoded(self):
        hook = NetworkMessageHook(profile=RUNTIME_PROFILE, pid=1234)
        hook.process = 99
        hook.ring = 0x700000
        hook.installed = True
        hook.next_sequence = 0
        method = MESSAGE_ARGUMENT_SYNC_METHODS[1]
        record_address = hook.ring + MESSAGE_RECORDS_OFFSET
        raw = bytearray(MESSAGE_RECORD_SIZE)
        struct.pack_into(
            "<8Q",
            raw,
            0,
            0,
            134_321_845_085_764_054,
            1,
            2,
            3,
            0x800000,
            5,
            len(method),
        )
        raw[0x40 : 0x40 + len(method)] = method
        struct.pack_into("<Q", raw, MESSAGE_ARGUMENT_SYNC_STATE_OFFSET, 3)
        struct.pack_into("<Q", raw, MESSAGE_COMMIT_OFFSET, 1)

        def fake_read(_process, address, size):
            if address == hook.ring and size == 0x30:
                return struct.pack("<8sQQQQQ", MESSAGE_MAGIC, 1, 0, 0, 0, 1)
            if address == record_address:
                return bytes(raw)
            return None

        with (
            patch("network_capture.process_alive", return_value=True),
            patch("network_capture.read_region", side_effect=fake_read),
            patch("network_capture.RemoteMsgpackReader.decode") as decode,
            patch("network_capture.write_memory") as write,
        ):
            records = hook.poll(decode_arguments=True)

        decode.assert_not_called()
        write.assert_not_called()
        self.assertIn("timed out", records[0]["decode_error"])

    def test_msgpack_array_children_are_read_in_one_bulk_operation(self):
        root_address = 0x10000
        children_address = 0x20000
        values = [20_875_602, 2_011, 20_868_585]
        root = bytearray(RemoteMsgpackReader.OBJECT_SIZE)
        struct.pack_into("<I", root, 0, 7)
        struct.pack_into("<Q", root, 8, len(values))
        struct.pack_into("<Q", root, 16, children_address)
        children = bytearray(RemoteMsgpackReader.OBJECT_SIZE * len(values))
        for index, value in enumerate(values):
            offset = index * RemoteMsgpackReader.OBJECT_SIZE
            struct.pack_into("<I", children, offset, 2)
            struct.pack_into("<Q", children, offset + 8, value)

        reads = []

        def fake_read(_process, address, size):
            reads.append((address, size))
            if address == root_address:
                return bytes(root)
            if address == children_address:
                return bytes(children)
            return None

        with patch("network_capture.read_region", side_effect=fake_read):
            decoded = RemoteMsgpackReader(99).decode(root_address)

        self.assertEqual(decoded, values)
        self.assertEqual(
            reads,
            [
                (root_address, RemoteMsgpackReader.OBJECT_SIZE),
                (
                    children_address,
                    RemoteMsgpackReader.OBJECT_SIZE * len(values),
                ),
            ],
        )

    def test_network_hook_adopts_valid_existing_capture(self):
        hook = NetworkMessageHook(profile=RUNTIME_PROFILE, pid=1234)
        hook.process = 99
        hook.target = 0x140123000
        stub = 0x7FF600001000
        ring = 0x7FF600010000
        jump = build_absolute_patch(stub, len(MESSAGE_PROLOGUE))
        stub_code = message_stub(
            ring, hook.target + len(MESSAGE_PROLOGUE)
        )
        header = struct.pack(
            "<8sQQQQQ",
            MESSAGE_MAGIC,
            321,
            MESSAGE_RECORD_COUNT,
            MESSAGE_RECORD_SIZE,
            hook.target,
            1,
        )

        def fake_read(_process, address, size):
            if address == stub:
                return stub_code[:size]
            if address == ring:
                return header[:size]
            return None

        with patch("network_capture.read_region", side_effect=fake_read):
            self.assertTrue(hook._adopt_existing(jump))
        self.assertTrue(hook.installed)
        self.assertTrue(hook.adopted)
        self.assertEqual(hook.stub, stub)
        self.assertEqual(hook.ring, ring)
        self.assertEqual(hook.next_sequence, 321)

    def test_network_takeover_restores_adopted_entry_on_close(self):
        hook = NetworkMessageHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            takeover_existing=True,
        )
        hook.pid = 1234
        hook.process = 99
        hook.target = 0x140123000
        hook.stub = 0x7FF600001000
        hook.ring = 0x7FF600010000
        hook.installed = True
        hook.adopted = True

        with (
            patch("network_capture.process_alive", return_value=True),
            patch("network_capture.suspend_game_threads", return_value=[7]),
            patch("network_capture.resume_threads") as resume,
            patch("network_capture.write_code") as write,
            patch("network_capture.read_region", return_value=MESSAGE_PROLOGUE),
            patch("network_capture.kernel32.CloseHandle"),
        ):
            hook.close()

        write.assert_called_once_with(99, hook.target, MESSAGE_PROLOGUE)
        resume.assert_called_once_with([7])

    def test_record_keeps_component_entity_and_enum_type(self):
        sequence = 17
        filetime = 134_321_845_085_764_054
        component = 0x0000_000F_5B75_6420
        entity_id = 237_663_626_070_592
        raw = struct.pack(
            "<8Q",
            sequence,
            filetime,
            component,
            entity_id,
            3,
            7_110_581,
            0x0000_0001_4696_3325,
            sequence + 1,
        )
        self.assertEqual(len(raw), BOSS_TYPE_RECORD_SIZE)
        record = parse_boss_type_record(raw, sequence)
        self.assertIsNotNone(record)
        self.assertEqual(record["component"], component)
        self.assertEqual(record["entity_id"], entity_id)
        self.assertEqual(record["boss_type"], 3)
        self.assertEqual(record["template_id"], 7_110_581)

    def test_stubs_capture_monster_template_id_from_common_component(self):
        boss_type_stub = build_boss_type_stub(
            0x100000,
            0x200000,
            0x300000,
            prologue=BOSS_TYPE_PROLOGUE,
        )
        boss_init_stub = build_boss_init_stub(
            0x100000,
            0x200000,
            prologue=BOSS_INIT_PROLOGUE,
        )
        self.assertIn(b"\x8b\x81\x98\x01\x00\x00", boss_type_stub)
        self.assertIn(b"\x8b\x87\x98\x01\x00\x00", boss_init_stub)

    def test_stub_replays_prologue_with_absolute_rip_load(self):
        global_address = 0x0000_0001_4E78_AC18
        stub = build_boss_type_stub(
            0x0000_1234_5678_0000,
            0x0000_0001_468D_6561,
            global_address,
            prologue=BOSS_TYPE_PROLOGUE,
        )
        displaced_prefix = BOSS_TYPE_PROLOGUE[:10]
        replay = displaced_prefix + b"\x48\xa1" + struct.pack(
            "<Q", global_address
        )
        self.assertIn(replay, stub)
        self.assertTrue(
            stub.endswith(
                b"\xff\x25\x00\x00\x00\x00"
                + struct.pack("<Q", 0x0000_0001_468D_6561)
            )
        )

    def test_template_init_stub_captures_saved_al_and_replays_write(self):
        resume = 0x0000_0001_468D_5A15
        stub = build_boss_init_stub(
            0x0000_1234_5678_0000,
            resume,
            prologue=BOSS_INIT_PROLOGUE,
        )
        self.assertIn(
            b"\x48\x8b\x44\x24\x20\x0f\xb6\xc0",
            stub,
        )
        self.assertIn(BOSS_INIT_PROLOGUE, stub)
        self.assertTrue(
            stub.endswith(
                b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
            )
        )

    def test_template_id_setter_captures_new_edx_value_and_current_type(self):
        global_address = 0x0000_0001_4E78_AC18
        resume = 0x0000_0001_468D_7901
        stub = build_template_id_stub(
            0x0000_1234_5678_0000,
            resume,
            global_address,
            prologue=TEMPLATE_ID_PROLOGUE,
        )

        # Current BossType comes from the same CommonComponent.  The incoming
        # TemplateId must come from EDX, not the still-old field at +0x198.
        self.assertIn(b"\x0f\xb6\x81\x37\x01\x00\x00", stub)
        self.assertIn(b"\x8b\xc2\x49\x89\x43\x28", stub)
        replay = (
            TEMPLATE_ID_PROLOGUE[:10]
            + b"\x48\xa1"
            + struct.pack("<Q", global_address)
        )
        self.assertIn(replay, stub)
        self.assertTrue(
            stub.endswith(
                b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", resume)
            )
        )

    def test_template_bulk_captures_r8b_and_ninth_stack_argument(self):
        resume = 0x0000_0001_468D_70B1
        stub = build_template_bulk_stub(
            0x0000_1234_5678_0000,
            resume,
            prologue=TEMPLATE_BULK_PROLOGUE,
        )

        # pushfq plus five saved registers shifts entry [rsp+0x48] to +0x78.
        self.assertIn(b"\x41\x0f\xb6\xc0\x49\x89\x43\x20", stub)
        self.assertIn(b"\x8b\x44\x24\x78\x49\x89\x43\x28", stub)
        self.assertTrue(
            stub.endswith(
                TEMPLATE_BULK_PROLOGUE
                + b"\xff\x25\x00\x00\x00\x00"
                + struct.pack("<Q", resume)
            )
        )

    def test_poll_boss_types_reads_all_four_identity_sources(self):
        hook = DamageHook(profile=RUNTIME_PROFILE, pid=1234)
        hook.process = 99
        hook.boss_type_installed = True
        hook.boss_init_installed = True
        hook.template_id_installed = True
        hook.template_bulk_installed = True
        hook.boss_type_ring = 0x1000
        hook.boss_init_ring = 0x2000
        hook.template_id_ring = 0x3000
        hook.template_bulk_ring = 0x4000
        observed = []

        def fake_poll(ring, next_sequence, magic, function):
            observed.append((ring, magic, function))
            return ([{"function": function, "component": 0}], next_sequence + 1)

        with (
            patch("damage_hook.process_alive", return_value=True),
            patch.object(hook, "_poll_boss_ring", side_effect=fake_poll),
        ):
            records = hook.poll_boss_types()

        self.assertEqual(len(records), 4)
        self.assertEqual(
            [function for _ring, _magic, function in observed],
            [
                "KAPI_Common_SetBossType",
                "CommonComponent_TemplateBossType",
                "CommonComponent_SetTemplateId",
                "CommonComponent_BulkTemplate",
            ],
        )
        self.assertEqual(hook.template_id_next_sequence, 1)
        self.assertEqual(hook.template_bulk_next_sequence, 1)

    def test_target_lookup_diagnostic_distinguishes_local_player_conflict(self):
        hook = DamageHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            target_boss_lookup_enabled=True,
        )
        hook.local_player_id = 57_277_687_900_295

        should_queue = hook._track_target_lookup_candidate(
            57_288_000_000_001,
            hook.local_player_id,
        )

        self.assertFalse(should_queue)
        diagnostics = hook.diagnostic_snapshot()
        self.assertEqual(diagnostics["damage_target_records"], 1)
        self.assertEqual(diagnostics["damage_target_matches_local_player"], 1)
        self.assertEqual(diagnostics["target_lookup_skipped_local_player"], 1)

    def test_target_lookup_diagnostic_distinguishes_entity_id_ranges(self):
        hook = DamageHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            target_boss_lookup_enabled=True,
        )

        self.assertTrue(
            hook._track_target_lookup_candidate(1, 8_500_000_000_000)
        )
        self.assertTrue(
            hook._track_target_lookup_candidate(1, 57_288_000_000_001)
        )

        diagnostics = hook.diagnostic_snapshot()
        self.assertEqual(diagnostics["damage_target_id_mid_supported"], 1)
        self.assertEqual(diagnostics["damage_target_id_gap"], 0)
        self.assertEqual(diagnostics["damage_target_id_high_supported"], 1)
        self.assertEqual(diagnostics["target_lookup_skipped_unplausible"], 0)

    def test_close_restores_both_template_entry_points(self):
        hook = DamageHook(profile=RUNTIME_PROFILE, pid=1234)
        hook.pid = 1234
        hook.process = 99
        hook.template_id_target = 0x140001000
        hook.template_bulk_target = 0x140002000
        hook.template_id_installed = True
        hook.template_bulk_installed = True

        with (
            patch("damage_hook.process_alive", return_value=True),
            patch("damage_hook.suspend_process", return_value=[7]),
            patch("damage_hook.resume_threads") as resume,
            patch("damage_hook.write_code") as write,
            patch(
                "damage_hook.read_region",
                side_effect=lambda _process, address, _size: (
                    TEMPLATE_BULK_PROLOGUE
                    if address == hook.template_bulk_target
                    else TEMPLATE_ID_PROLOGUE
                ),
            ),
            patch("damage_hook.kernel32.CloseHandle"),
        ):
            hook.close()

        self.assertEqual(
            write.call_args_list,
            [
                unittest.mock.call(
                    99, hook.template_bulk_target, TEMPLATE_BULK_PROLOGUE
                ),
                unittest.mock.call(
                    99, hook.template_id_target, TEMPLATE_ID_PROLOGUE
                ),
            ],
        )
        resume.assert_called_once_with([7])

    def test_damage_takeover_restores_adopted_entry_on_close(self):
        hook = DamageHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            takeover_existing=True,
        )
        hook.pid = 1234
        hook.process = 99
        hook.target = 0x140003000
        hook.installed = True
        hook.adopted = True

        with (
            patch("damage_hook.process_alive", return_value=True),
            patch("damage_hook.suspend_process", return_value=[7]),
            patch("damage_hook.resume_threads") as resume,
            patch("damage_hook.write_code") as write,
            patch(
                "damage_hook.read_region",
                return_value=hook.damage_prologue,
            ),
            patch("damage_hook.kernel32.CloseHandle"),
        ):
            hook.close()

        write.assert_called_once_with(99, hook.target, hook.damage_prologue)
        resume.assert_called_once_with([7])

    def test_template_record_keeps_its_capture_source(self):
        raw = struct.pack("<8Q", 1, 134_321_845_085_764_054, 2, 3, 1, 0, 4, 2)
        record = parse_boss_type_record(
            raw, 1, "CommonComponent_TemplateBossType"
        )
        self.assertEqual(record["function"], "CommonComponent_TemplateBossType")

    def test_existing_component_scan_recovers_mid_map_boss(self):
        component = 0x0000_0003_C369_5890
        entity_id = 4_642_860_057_279
        template_id = 7_102_403
        raw = bytearray(BOSS_TEMPLATE_ID_OFFSET + 4)
        struct.pack_into("<Q", raw, BOSS_TYPE_ENTITY_ID_OFFSET, entity_id)
        raw[BOSS_TYPE_FIELD_OFFSET] = 3
        struct.pack_into("<I", raw, BOSS_TEMPLATE_ID_OFFSET, template_id)
        hook = DamageHook(profile=RUNTIME_PROFILE, pid=1234)
        hook.process = 99
        hook._iter_live_object_pointers = lambda: iter((component,))

        with patch("damage_hook.read_region", return_value=bytes(raw)):
            updates = hook._scan_existing_boss_components({entity_id})

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["entity_id"], entity_id)
        self.assertEqual(updates[0]["template_id"], template_id)
        self.assertEqual(updates[0]["boss_type"], 3)
        self.assertTrue(updates[0]["existing_object_scan"])

    def test_existing_component_fallback_walks_global_objects_only_once(self):
        hook = DamageHook(profile=RUNTIME_PROFILE, pid=1234)
        first_target = 4_642_860_057_279
        second_target = first_target + 1
        recovered_boss = second_target

        def scan(target_ids):
            hook.existing_boss_component_cache[recovered_boss] = {
                "entity_id": recovered_boss,
                "boss_type": 3,
                "template_id": 7_102_403,
            }
            return [
                dict(hook.existing_boss_component_cache[entity_id])
                for entity_id in target_ids
                if entity_id in hook.existing_boss_component_cache
            ]

        with patch.object(
            hook, "_scan_existing_boss_components", side_effect=scan
        ) as full_scan:
            self.assertEqual(
                hook._recover_existing_boss_components_once({first_target}), []
            )
            updates = hook._recover_existing_boss_components_once(
                {second_target}
            )

        full_scan.assert_called_once_with({first_target})
        self.assertEqual(updates[0]["entity_id"], recovered_boss)

    def test_target_boss_lookup_is_disabled_by_default(self):
        hook = DamageHook(profile=RUNTIME_PROFILE, pid=1234)
        hook.pending_target_boss_lookups[4_642_860_057_279] = (
            time.monotonic()
        )

        with patch("damage_hook.read_region") as read:
            self.assertEqual(hook._resolve_target_boss_lookups(), [])

        self.assertFalse(hook.target_boss_lookup_enabled)
        read.assert_not_called()

    def test_target_lookup_delays_zero_template_then_recovers_first_karl(self):
        component = 0x0000_0003_C369_5890
        target_id = 4_642_860_057_279
        raw = bytearray(BOSS_TEMPLATE_ID_OFFSET + 4)
        struct.pack_into("<Q", raw, BOSS_TYPE_ENTITY_ID_OFFSET, target_id)
        raw[BOSS_TYPE_FIELD_OFFSET] = 0
        hook = DamageHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            target_boss_lookup_enabled=True,
        )
        hook.process = 99
        hook._remember_common_component_record(
            {
                "component": component,
                "entity_id": target_id,
                "boss_type": 0,
                "template_id": 0,
            }
        )
        hook.pending_target_boss_lookups[target_id] = time.monotonic()

        with patch("damage_hook.read_region", return_value=bytes(raw)):
            self.assertEqual(hook._resolve_target_boss_lookups(), [])
        self.assertIn(target_id, hook.pending_target_boss_lookups)
        self.assertEqual(
            hook.diagnostic_snapshot()["target_lookup_component_template_zero"],
            1,
        )

        struct.pack_into("<I", raw, BOSS_TEMPLATE_ID_OFFSET, 7_107_030)
        hook._last_target_boss_lookup_poll = 0.0
        with patch("damage_hook.read_region", return_value=bytes(raw)):
            updates = hook._resolve_target_boss_lookups()

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["entity_id"], target_id)
        self.assertEqual(updates[0]["template_id"], 7_107_030)
        self.assertEqual(updates[0]["boss_type"], 0)
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7107030": {
                    "boss_type": 0,
                    "name": "卡尔·埃德加",
                    "level": 60,
                }
            }
        )
        parsed = parser.process_native_boss_type(updates[0])
        profile = next(value for kind, value in parsed if kind == "profile")
        self.assertEqual(profile["entity_type"], "Boss")
        self.assertEqual(profile["template_id"], 7_107_030)

    def test_target_lookup_small_monster_template_fails_closed(self):
        target_id = 4_642_860_057_280
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7107030": {"boss_type": 0, "name": "卡尔·埃德加"}
            }
        )
        updates = parser.process_native_boss_type(
            {
                "function": "CommonComponent_TargetIdLookup",
                "entity_id": target_id,
                "template_id": 7_000_001,
                "boss_type": 3,
                "target_id_lookup": True,
            }
        )

        self.assertEqual(updates, [])
        self.assertNotIn(target_id, parser.confirmed_boss_entities)

    def test_target_lookup_object_index_runs_once_for_multiple_targets(self):
        observed_component = 0x0000_0003_C369_5000
        indexed_component = observed_component + 0x1000
        common_class = 0x0000_0001_5000_1000
        observed_entity = 4_642_860_050_000
        first_target = observed_entity + 1
        second_target = observed_entity + 2
        third_target = observed_entity + 3
        raw = bytearray(BOSS_TEMPLATE_ID_OFFSET + 4)
        struct.pack_into("<Q", raw, BOSS_TYPE_ENTITY_ID_OFFSET, first_target)
        raw[BOSS_TYPE_FIELD_OFFSET] = 0
        struct.pack_into("<I", raw, BOSS_TEMPLATE_ID_OFFSET, 7_107_030)
        hook = DamageHook(
            profile=RUNTIME_PROFILE,
            pid=1234,
            target_boss_lookup_enabled=True,
        )
        hook.process = 99
        hook._remember_common_component_record(
            {
                "component": observed_component,
                "entity_id": observed_entity,
                "boss_type": 0,
                "template_id": 0,
            }
        )
        now = time.monotonic()
        hook.pending_target_boss_lookups = {
            first_target: now,
            second_target: now,
        }
        object_walks = 0

        def iter_objects(*, track_target_lookup=False):
            nonlocal object_walks
            self.assertTrue(track_target_lookup)
            object_walks += 1
            return iter((indexed_component,))

        def fake_read(_process, address, size):
            if size == 8 and address in {
                observed_component + 0x10,
                indexed_component + 0x10,
            }:
                return struct.pack("<Q", common_class)
            if address == indexed_component and size == len(raw):
                return bytes(raw)
            return None

        hook._iter_live_object_pointers = iter_objects
        with patch("damage_hook.read_region", side_effect=fake_read):
            updates = hook._resolve_target_boss_lookups()
            hook.pending_target_boss_lookups[third_target] = time.monotonic()
            hook._last_target_boss_lookup_poll = 0.0
            hook._resolve_target_boss_lookups()

        self.assertEqual(updates[0]["entity_id"], first_target)
        self.assertEqual(object_walks, 1)
        self.assertEqual(hook.target_boss_object_scan_count, 1)
        diagnostics = hook.diagnostic_snapshot()
        self.assertEqual(
            diagnostics["target_lookup_object_exact_entity_matches"], 1
        )
        self.assertEqual(
            diagnostics["target_lookup_object_exact_template_nonzero"], 1
        )

    def test_local_controlled_entity_refreshes_after_transformation(self):
        hook = DamageHook(profile=RUNTIME_PROFILE, pid=1234)
        original_id = 57_266_949_828_970
        transformed_id = original_id + 50_000
        resolved = iter((original_id, transformed_id))
        hook.resolve_local_player_id = lambda _manager: next(resolved)

        self.assertEqual(
            hook._refresh_local_player_id(1, now=1.0), original_id
        )
        # The cheap guard avoids reading game memory on every damage record.
        self.assertEqual(
            hook._refresh_local_player_id(1, now=1.05), original_id
        )
        self.assertEqual(
            hook._refresh_local_player_id(1, now=1.11), transformed_id
        )


if __name__ == "__main__":
    unittest.main()
