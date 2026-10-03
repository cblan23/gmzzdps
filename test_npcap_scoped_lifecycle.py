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
        self.parser.process(self.record(2350, [1789112000000, 1789112000005]))
        self.assertEqual(self.parser.native_self_id, SELF)

    def add_member(self):
        self.parser.process(self.record(221, [1, 2, {2: BOT_TOKEN, 5: '人机·投影', 8: 1200002, 9: 70}]))
        self.assertIn(BOT_TOKEN, self.parser.party_tokens)

    def test_verified_clock_reply_identifies_without_waiting_for_cast(self):
        self.identify()

    def test_scoped_observation_is_frozen_only_when_record_is_drained(self):
        pending = self.record(2342, [0, ''])

        self.assertEqual(self.parser.process(pending), [])
        self.assertEqual(self.parser.take_settlement_observations(), [])

        self.identify()
        observations = self.parser.take_settlement_observations()
        self.assertEqual(len(observations), 1)
        self.assertEqual(
            observations[0]["record"]["capture_event_id"],
            pending["capture_event_id"],
        )
        self.assertEqual(
            observations[0]["record"]["method"], "OnMsgEntityRelive"
        )
        self.assertEqual(self.parser.take_settlement_observations(), [])

    def test_world_return_info_does_not_clear_scene(self):
        self.identify()
        self.parser.scene_id = 5100001
        updates = self.parser.process(self.record(224, [1, 2, 3]))
        self.assertEqual(self.parser.scene_id, 5100001)
        self.assertFalse(any(kind == 'scene' for kind, _ in updates))
        updates = self.parser.process(self.record(389, []))
        self.assertTrue(any(kind == 'scene' for kind, _ in updates))

    def test_second_scene_boundary_cannot_replay_after_new_boss_creation(self):
        self.identify()

        before_enter = self.parser.process(self.record(389, []))
        self.assertTrue(any(kind == 'scene' for kind, _ in before_enter))
        self.assertIsNone(self.parser.native_self_id)

        # Both boundaries are routed to the local role, but the first one has
        # already cleared that role binding.  The second boundary still belongs
        # to the old scene and must be consumed now instead of being queued.
        leave = self.parser.process(self.record(1441, []))
        self.assertTrue(any(kind == 'scene' for kind, _ in leave))
        self.assertFalse(self.parser.pending_scoped_records)

        self.parser.process_native_boss_type({
            'function': 'CommonComponent_TargetIdLookup',
            'entity_id': BOSS,
            'template_id': 7102990,
            'boss_type': 3,
            'filetime_100ns': 134338000000000000,
        })
        self.assertEqual(self.parser.active_boss_entity_id, BOSS)

        # The first new-space local reply must only restore local identity.  It
        # must not drain an old transition and erase the new Boss metadata.
        self.identify()
        self.assertEqual(self.parser.active_boss_entity_id, BOSS)
        self.assertIn(BOSS, self.parser.confirmed_boss_entities)

    def test_deferred_old_stage_cannot_replay_after_scene_boundary(self):
        old_stage = self.record(
            348,
            [{0: 5_150_111, 1: 1, 2: 'old', 5: {}}],
        )
        self.assertEqual(self.parser.process(old_stage), [])
        self.assertEqual(len(self.parser.pending_scoped_records), 1)

        self.parser.process(self.record(389, []))
        self.assertFalse(self.parser.pending_scoped_records)
        self.assertEqual(self.parser.dungeon_stage_id, 0)
        self.assertEqual(self.parser.dungeon_stage_phase, 0)

        self.identify()
        self.assertEqual(self.parser.dungeon_stage_id, 0)
        self.assertEqual(self.parser.dungeon_stage_phase, 0)

    def test_peer_quit_removes_only_peer_then_self_quit_clears_roster(self):
        self.identify()
        self.add_member()
        self.parser.process(self.record(216, [BOT_TOKEN]))
        self.assertNotIn(BOT_TOKEN, self.parser.party_tokens)
        self.add_member()
        updates = self.parser.process(self.record(215, []))
        self.assertFalse(self.parser.party_ids)
        self.assertTrue(any(kind == 'party' and value.get('left_team') for kind, value in updates))

    def test_actual_player_death_emits_confirmed_life_event(self):
        self.identify()
        updates = self.parser.process(self.record(2341, [0, '']))
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
        observations = self.parser.take_settlement_observations()
        self.assertEqual(
            [row['record']['method'] for row in observations],
            ['NpcapBossConfirmed', 'OnMsgEntityDead'],
        )

    def test_player_scoped_quit_on_other_entity_never_clears_local_party(self):
        self.identify()
        self.add_member()
        self.parser.process(self.record(215, [], BOSS))
        self.assertIn(BOT_TOKEN, self.parser.party_tokens)


if __name__ == '__main__':
    unittest.main()
