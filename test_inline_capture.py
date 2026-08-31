import struct
import unittest
from unittest.mock import patch

from damage_hook import DamageHook
from inline_capture import (
    BOSS_INIT_PROLOGUE,
    BOSS_TEMPLATE_ID_OFFSET,
    BOSS_TYPE_ENTITY_ID_OFFSET,
    BOSS_TYPE_FIELD_OFFSET,
    BOSS_TYPE_PROLOGUE,
    BOSS_TYPE_RECORD_SIZE,
    build_boss_init_stub,
    build_boss_type_stub,
    parse_boss_type_record,
)
from network_capture import (
    MESSAGE_ARGUMENT_ACK_OFFSET,
    MESSAGE_ARGUMENT_SYNC_ENABLED_OFFSET,
    MESSAGE_ARGUMENT_SYNC_METHODS,
    MESSAGE_ARGUMENT_SYNC_METHOD_NAMES,
    MESSAGE_ARGUMENT_SYNC_STATE_OFFSET,
    MESSAGE_COMMIT_OFFSET,
    MESSAGE_MAGIC,
    MESSAGE_PROLOGUE,
    MESSAGE_RECORD_COUNT,
    MESSAGE_RECORDS_OFFSET,
    MESSAGE_RECORD_SIZE,
    NetworkMessageHook,
    RemoteMsgpackReader,
    build_absolute_patch,
    build_message_stub,
    parse_message_record,
)


class BossTypeCaptureTests(unittest.TestCase):
    def test_all_synchronous_methods_fit_in_the_injected_stub(self):
        stub = build_message_stub(0x1234_0000, 0x1400_1000)

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
        stub = build_message_stub(
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
        hook = NetworkMessageHook(pid=1234)
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
        hook = NetworkMessageHook(pid=1234)
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
        hook = NetworkMessageHook(pid=1234)
        hook.process = 99
        hook.target = 0x140123000
        stub = 0x7FF600001000
        ring = 0x7FF600010000
        jump = build_absolute_patch(stub, len(MESSAGE_PROLOGUE))
        stub_code = build_message_stub(
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
        boss_type_stub = build_boss_type_stub(0x100000, 0x200000, 0x300000)
        boss_init_stub = build_boss_init_stub(0x100000, 0x200000)
        self.assertIn(b"\x8b\x81\x98\x01\x00\x00", boss_type_stub)
        self.assertIn(b"\x8b\x87\x98\x01\x00\x00", boss_init_stub)

    def test_stub_replays_prologue_with_absolute_rip_load(self):
        global_address = 0x0000_0001_4E78_AC18
        stub = build_boss_type_stub(
            0x0000_1234_5678_0000,
            0x0000_0001_468D_6561,
            global_address,
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
        stub = build_boss_init_stub(0x0000_1234_5678_0000, resume)
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
        hook = DamageHook(pid=1234)
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
        hook = DamageHook(pid=1234)
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

    def test_local_controlled_entity_refreshes_after_transformation(self):
        hook = DamageHook(pid=1234)
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
