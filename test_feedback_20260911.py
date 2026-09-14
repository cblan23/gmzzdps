import unittest
import struct
import msgpack

from test_combat_model import CombatModel, DpsWindow, SELF_ID, TEAMMATE_ID, MONSTER_ID, SECOND_MONSTER_ID, BASE_FILETIME, damage
from profile_upload import encounter_upload_rejection_code
from history_index import build_history_summary
from network_state import NetworkPacketParser
from npcap_protocol import NpcapProtocolDecoder
from npcap_parser_adapter import normalize_npcap_record


class FeedbackRegressionTests(unittest.TestCase):
    def model(self):
        model = CombatModel(run_id='feedback-regression')
        model.ingest_identity({'entity_id': SELF_ID})
        model.ingest_party({'entity_ids': [SELF_ID, TEAMMATE_ID], 'member_count': 2, 'authoritative': True})
        model.ingest_profile({'entity_id': MONSTER_ID, 'entity_type': 'Boss', 'boss_type': 3,
                              'boss_rank': 3, 'template_id': 7102990, 'name': '子爵夫人'})
        return model

    def state(self, model, entity, time, active):
        return model.ingest_combat_state({'entity_id': entity, 'filetime_100ns': BASE_FILETIME + time*10_000_000,
                                          'in_combat': active})

    def life(self, model, actor, time, dead):
        return model.ingest_life({'actor_id': actor, 'filetime_100ns': BASE_FILETIME + time*10_000_000,
                                 'dead': dead, 'death_confirmed': True, 'explicit_transition': True})

    def wiped_model(self):
        model = self.model()
        self.state(model, MONSTER_ID, 0, True)
        model.ingest(damage(1000, SELF_ID, MONSTER_ID, 100))
        self.life(model, SELF_ID, 2, True)
        self.life(model, TEAMMATE_ID, 3, True)
        self.assertEqual(model.combat_end_reason, 'party_wipe')
        self.state(model, MONSTER_ID, 4, False)
        self.life(model, SELF_ID, 5, False)
        self.life(model, TEAMMATE_ID, 5, False)
        return model

    def test_fbff61146_wipe_repull_clears_deaths_before_first_hit(self):
        model = self.wiped_model()
        previous = model.encounter_id
        self.assertEqual(model.member_death_counts.get(SELF_ID), 1)
        self.state(model, MONSTER_ID, 8, True)
        self.assertNotEqual(model.encounter_id, previous)
        self.assertEqual(model.member_death_counts, {})
        self.assertFalse(model.combat_end_time)
        self.assertTrue(model.encounter_start_signal_100ns)
        archived = next(row for row in model.completed_combats if row['encounter_id'] == previous)
        self.assertEqual(next(row for row in archived['participants'] if row['actor_id'] == SELF_ID)['deaths'], 1)

    def test_wipe_repull_clears_revives_and_preserves_previous_record(self):
        model = self.model()
        self.state(model, MONSTER_ID, 0, True)
        model.ingest(damage(1000, SELF_ID, MONSTER_ID, 100))
        self.life(model, SELF_ID, 2, True)
        self.life(model, SELF_ID, 3, False)
        self.assertEqual(model.member_revive_counts.get(SELF_ID), 1)
        self.life(model, SELF_ID, 4, True)
        self.life(model, TEAMMATE_ID, 5, True)
        self.assertEqual(model.combat_end_reason, 'party_wipe')
        previous = model.encounter_id
        self.state(model, MONSTER_ID, 6, False)
        self.life(model, SELF_ID, 7, False)
        self.life(model, TEAMMATE_ID, 7, False)
        self.state(model, MONSTER_ID, 8, True)
        self.assertNotEqual(model.encounter_id, previous)
        self.assertEqual(model.member_revive_counts, {})
        self.assertEqual(model.member_death_counts, {})
        archived = next(row for row in model.completed_combats if row['encounter_id'] == previous)
        participant = next(row for row in archived['participants'] if row['actor_id'] == SELF_ID)
        self.assertEqual(participant['revives'], 1)
        self.assertEqual(participant['deaths'], 2)

    def test_wipe_stale_or_unrelated_combat_state_does_not_reset(self):
        model = self.wiped_model()
        previous = model.encounter_id
        self.state(model, MONSTER_ID, 2, True)
        self.state(model, SECOND_MONSTER_ID, 8, True)
        self.assertEqual(model.encounter_id, previous)
        self.assertEqual(model.member_death_counts.get(SELF_ID), 1)

    def test_viscountess_final_death_uploads_without_settlement_snapshot(self):
        model = self.model()
        model.ingest(damage(1000, SELF_ID, MONSTER_ID, 100))
        model.ingest_profile({'entity_id': SECOND_MONSTER_ID, 'entity_type': 'Boss', 'boss_type': 3,
                              'boss_rank': 3, 'template_id': 7102991, 'name': '子爵夫人-神话姿态'})
        model.ingest(damage(2000, SELF_ID, SECOND_MONSTER_ID, 200))
        for entity in (SELF_ID, TEAMMATE_ID, SECOND_MONSTER_ID):
            self.state(model, entity, 3, False)
        model.ingest_monster({'entity_id': SECOND_MONSTER_ID, 'current_hp': 0,
                              'death_confirmed': True, 'filetime_100ns': BASE_FILETIME + 4*10_000_000})
        record = model.build_combat_record(model.combat_end_reason or 'idle')
        self.assertEqual(encounter_upload_rejection_code(record), '')
        self.assertEqual(encounter_upload_rejection_code(build_history_summary(record)), '')
        self.assertEqual(model.combat_end_reason, 'target_defeated')
        for trigger in ('party_exit', 'scene_change', 'shutdown'):
            with self.subTest(trigger=trigger):
                archived = model.build_combat_record(trigger)
                self.assertEqual(encounter_upload_rejection_code(archived), '')
                self.assertEqual(encounter_upload_rejection_code(build_history_summary(archived)), '')

    def test_self_live_counter_visible_without_native_damage_events(self):
        model = self.model()
        model.ingest_team_stat({'actor_id': SELF_ID, 'absolute_damage': 0, 'full_snapshot': True,
                                'filetime_100ns': BASE_FILETIME})
        self.state(model, MONSTER_ID, 1, True)
        model.ingest_team_stat({'actor_id': SELF_ID, 'absolute_damage': 125, 'full_snapshot': True,
                                'filetime_100ns': BASE_FILETIME + 2*10_000_000})
        self.assertFalse(model.events)
        window = object.__new__(DpsWindow)
        window.model = model
        window.latest_healing_summary = {}
        window._shown_actor_name = lambda actor: str(actor)
        window._profession_display_metric = lambda _profession: 'dps'
        window._main_visible_roster_members = lambda: {SELF_ID, TEAMMATE_ID}
        rows = window._main_combat_display_rows(model.last_damage_time)
        own = next(row for row in rows if row['is_self'])
        self.assertEqual(own['total_value'], 125)
        self.assertGreater(own['stat_value'], 0)

    def test_fb6550_self_rpc_damage_updates_live_without_native_hook(self):
        model = self.model()
        parser = NetworkPacketParser()
        parser.self_id = SELF_ID
        parser.self_confirmed = True
        decoder = NpcapProtocolDecoder(stream_id='self-damage-regression')
        arguments = [SELF_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 1702, 0, 1688, False]
        payload = msgpack.packb([{}, [MONSTER_ID, 90, arguments]], use_bin_type=True)
        rpc = struct.pack('<IH', len(payload)+2, 18) + payload
        timestamp = model._event_seconds({'filetime_100ns': BASE_FILETIME})
        # Two identical, real RPCs are two hits. No native callback is involved.
        decoder._append_application(rpc+rpc, 100, timestamp)
        records = decoder._rpc_records()
        self.assertEqual(len(records), 2)
        for record in records:
            for kind, update in parser.process(record):
                if kind == 'event':
                    model.ingest(update)
        self.assertEqual(len(model.events), 2)
        own = next(row for row in model.current_stats() if row.actor_id == SELF_ID)
        self.assertEqual(own.damage, 3376)
        self.assertFalse(model.combat_end_time)

    def test_delayed_legacy_memory_arguments_remain_rejected(self):
        record = {'decoded_arguments': [1, 2], 'decode_delay_ms': 1000,
                  'arguments_synchronized': True}
        self.assertEqual(NetworkPacketParser._args(record), [])
        record['capture_source'] = 'npcap'
        self.assertEqual(NetworkPacketParser._args(record), [])
        self.assertEqual(NetworkPacketParser._args(normalize_npcap_record(record)), [1, 2])
        record['arguments_synchronized'] = False
        self.assertEqual(NetworkPacketParser._args(record), [])


if __name__ == '__main__':
    unittest.main()
