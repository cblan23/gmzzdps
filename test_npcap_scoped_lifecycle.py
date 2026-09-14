import struct
import unittest
import msgpack
from npcap_protocol import NpcapProtocolDecoder
from npcap_parser_adapter import NpcapParserAdapter

SELF = 57266949828970
BOSS = 57236882400409
BOT_TOKEN = 'aqFyZFqM2Vv1Z3Uy'


class ScopedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.decoder = NpcapProtocolDecoder()
        self.parser = NpcapParserAdapter({}, {'7102990': {'name': '子爵夫人', 'boss_type': 3}})
        self.sequence = 0

    def record(self, method_id, args, entity=SELF):
        self.sequence += 1
        packed = msgpack.packb([{}, [entity, method_id, args]], use_bin_type=True)
        self.decoder._append_application(struct.pack('<IH', len(packed)+2, 22)+packed,
                                         self.sequence, 1789112000+self.sequence)
        return self.decoder._rpc_records()[0]

    def identify(self):
        self.parser.process(self.record(2253, [1789112000000, 1789112000005]))
        self.assertEqual(self.parser.native_self_id, SELF)

    def add_member(self):
        self.parser.process(self.record(214, [1, 2, {2: BOT_TOKEN, 5: '人机·投影', 8: 1200002, 9: 70}]))
        self.assertIn(BOT_TOKEN, self.parser.party_tokens)

    def test_verified_clock_reply_identifies_without_waiting_for_cast(self):
        self.identify()

    def test_world_return_info_does_not_clear_scene(self):
        self.identify()
        self.parser.scene_id = 5100001
        updates = self.parser.process(self.record(217, [1, 2, 3]))
        self.assertEqual(self.parser.scene_id, 5100001)
        self.assertFalse(any(kind == 'scene' for kind, _ in updates))
        updates = self.parser.process(self.record(381, []))
        self.assertTrue(any(kind == 'scene' for kind, _ in updates))

    def test_peer_quit_removes_only_peer_then_self_quit_clears_roster(self):
        self.identify()
        self.add_member()
        self.parser.process(self.record(209, [BOT_TOKEN]))
        self.assertNotIn(BOT_TOKEN, self.parser.party_tokens)
        self.add_member()
        updates = self.parser.process(self.record(208, []))
        self.assertFalse(self.parser.party_ids)
        self.assertTrue(any(kind == 'party' and value.get('left_team') for kind, value in updates))

    def test_actual_player_death_emits_confirmed_life_event(self):
        self.identify()
        updates = self.parser.process(self.record(2244, [0, '']))
        self.assertTrue(any(kind == 'life' and value['actor_id'] == SELF and value.get('death_confirmed')
                            for kind, value in updates))

    def test_npc_death_waits_for_verified_npc_identity(self):
        self.identify()
        record = self.record(253, [2, 'npc-token'], BOSS)
        self.assertEqual(self.parser.process(record), [])
        updates = self.parser.process_native_boss_type({
            'function': 'CommonComponent_TargetIdLookup', 'entity_id': BOSS,
            'template_id': 7102990, 'boss_type': 3, 'filetime_100ns': record['filetime_100ns']-100})
        self.assertTrue(any(kind == 'monster' and value.get('entity_id') == BOSS and value.get('death_confirmed')
                            for kind, value in updates))

    def test_player_scoped_quit_on_other_entity_never_clears_local_party(self):
        self.identify()
        self.add_member()
        self.parser.process(self.record(208, [], BOSS))
        self.assertIn(BOT_TOKEN, self.parser.party_tokens)


if __name__ == '__main__':
    unittest.main()
