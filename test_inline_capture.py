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
    MESSAGE_MAGIC,
    MESSAGE_PROLOGUE,
    MESSAGE_RECORD_COUNT,
    MESSAGE_RECORD_SIZE,
    NetworkMessageHook,
    RemoteMsgpackReader,
    build_absolute_patch,
    build_message_stub,
)


class BossTypeCaptureTests(unittest.TestCase):
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
            "<8sQQQQ",
            MESSAGE_MAGIC,
            321,
            MESSAGE_RECORD_COUNT,
            MESSAGE_RECORD_SIZE,
            hook.target,
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
