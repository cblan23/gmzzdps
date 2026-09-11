"""Explicit entity ownership must survive address reuse across Boss attempts."""
import unittest
from network_state import NetworkPacketParser
from test_network_state import packet, MONSTER_ID, MONSTER_POINTER, PLAYER_ID
from test_combat_model import CombatModel


class BossOwnerTests(unittest.TestCase):
    def test_three_attempts_reusing_one_address_do_not_take_the_guard_health(self):
        parser = NetworkPacketParser(boss_template_catalog={
            '7102990': dict(name='子爵夫人', boss_type=3, level=72),
            '7103232': dict(name='管家', boss_type=3, level=39),
        })
        seq = 0
        for index in range(3):
            boss_id = MONSTER_ID + index * 10
            guard_id = boss_id + 1
            seq += 10
            parser.active_boss_entity_id = None
            parser.active_boss_pointer = None
            parser.process(packet('OnMsgAddBuffNew', [85000111, boss_id, boss_id], pointer=MONSTER_POINTER, sequence=seq))
            parser.process_native_boss_type(dict(entity_id=boss_id, template_id=7102990, boss_type=3,
                filetime_100ns=packet('', [], sequence=seq+1)['filetime_100ns']))
            parser.process(packet('OnMsgSyncCurrentMaxHp', [60000000, 66735474], pointer=MONSTER_POINTER, sequence=seq+2))
            guard_pointer = MONSTER_POINTER + index + 1
            parser.process(packet('OnMsgAddBuffNew', [85000111, guard_id, guard_id], pointer=guard_pointer, sequence=seq+3))
            parser.process(packet('OnMsgSyncDirtyFightAttributes', [{'$map': [[21, 918830]]}], pointer=guard_pointer, sequence=seq+4))
            parser.player_attackers.add(PLAYER_ID)
            parser.process(packet('OnMsgSyncFightMode', [2], pointer=guard_pointer, sequence=seq+5))
            parser.process(packet('OnMsgEndureExitHit', [PLAYER_ID], pointer=guard_pointer, sequence=seq+6))
            updates = parser.process(packet('OnMsgSyncCurrentHp', [0], pointer=guard_pointer, sequence=seq+7))
            self.assertEqual(parser.pointer_entities[MONSTER_POINTER], boss_id)
            self.assertEqual(parser.active_boss_pointer, MONSTER_POINTER)
            self.assertEqual(parser.entity_max_hp[boss_id], 66735474)
            self.assertEqual(parser.entity_current_hp[boss_id], 60000000)
            self.assertFalse(any(kind == 'monster' and value.get('entity_id') == boss_id and 'current_hp' in value for kind, value in updates))
            self.assertEqual(parser._bind_pointer(guard_pointer, boss_id, packet('', [])), [])

    def test_late_exact_owner_replaces_incorrect_guess_and_clears_wrong_maximum(self):
        parser = NetworkPacketParser()
        parser.active_boss_entity_id = MONSTER_ID
        parser.confirmed_boss_entities.add(MONSTER_ID)
        wrong_pointer = MONSTER_POINTER + 1
        parser.pointer_entities[wrong_pointer] = MONSTER_ID
        parser.active_boss_pointer = wrong_pointer
        parser.entity_max_hp[MONSTER_ID] = 918830
        parser.entity_current_hp[MONSTER_ID] = 0
        model = CombatModel()
        model.ingest_monster(dict(entity_id=MONSTER_ID, max_hp=918830, current_hp=0))
        parser.process(packet('OnMsgAddBuffNew', [85000111, MONSTER_ID, MONSTER_ID], pointer=MONSTER_POINTER))
        corrected = parser.process_native_boss_type(dict(entity_id=MONSTER_ID, boss_type=3,
            filetime_100ns=packet('', [], sequence=1)['filetime_100ns']))
        for kind, update in corrected:
            if kind == 'monster': model.ingest_monster(update)
        self.assertEqual(parser.active_boss_pointer, MONSTER_POINTER)
        self.assertIsNone(model.monsters[MONSTER_ID].max_hp)
        actual = parser.process(packet('OnMsgSyncCurrentMaxHp', [50000000, 66735474], pointer=MONSTER_POINTER, sequence=2))
        for kind, update in actual:
            if kind == 'monster': model.ingest_monster(update)
        self.assertEqual(model.monsters[MONSTER_ID].max_hp, 66735474)
        self.assertEqual(model.monsters[MONSTER_ID].current_hp, 50000000)

    def test_scene_change_clears_pointer_ownership_for_new_units(self):
        parser = NetworkPacketParser()
        parser.process(packet('OnMsgAddBuffNew', [85000111, MONSTER_ID, MONSTER_ID], pointer=MONSTER_POINTER))
        parser._reset_scene_combat_bindings()
        self.assertEqual(parser.confirmed_pointer_owners, {})

    def test_hp_before_native_metadata_does_not_discard_new_owner_evidence(self):
        parser = NetworkPacketParser()
        old_id, new_id = MONSTER_ID, MONSTER_ID + 1
        parser.pointer_entities[MONSTER_POINTER] = old_id
        parser.confirmed_pointer_owners[MONSTER_POINTER] = old_id
        parser.process(packet('OnMsgAddBuffNew', [85000111, new_id, new_id], pointer=MONSTER_POINTER, sequence=1))
        parser.process(packet('OnMsgSyncCurrentHp', [60000000], pointer=MONSTER_POINTER, sequence=2))
        self.assertEqual(parser.pointer_candidates[MONSTER_POINTER][0], new_id)
        parser.process_native_boss_type(dict(entity_id=new_id, boss_type=3,
            filetime_100ns=packet('', [], sequence=3)['filetime_100ns']))
        parser.process(packet('OnMsgSyncCurrentMaxHp', [59000000, 66735474], pointer=MONSTER_POINTER, sequence=4))
        self.assertEqual(parser.pointer_entities[MONSTER_POINTER], new_id)
        self.assertEqual(parser.entity_current_hp[new_id], 59000000)


if __name__ == '__main__':
    unittest.main()
