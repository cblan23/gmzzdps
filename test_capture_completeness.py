"""Capture-to-history regressions for the current client RPC descriptors."""
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import msgpack

from combat_history import CombatHistoryStore
from encounter_repository import EncounterRepository
from npcap_parser_adapter import NpcapParserAdapter
from npcap_protocol import NpcapProtocolDecoder
from profile_upload import _opening_rows_by_actor, build_upload_encounter
from settlement_history_adapter import SettlementHistoryAdapter
from settlement_ui_controller import SettlementUIController
import test_combat_model as combat_test
from test_combat_model import DpsWindow, SELF_ID, BASE_FILETIME
from test_profile_upload import character_token

SELF = 57_266_949_828_970
PEER = SELF + 1
BOSS = 57_236_882_400_409
START = 1_800_000_000
FILETIME = 116_444_736_000_000_000 + START * 10_000_000


class CaptureCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = CombatHistoryStore(self.directory)
        self.controller = SettlementUIController(EncounterRepository(self.directory))
        self.adapter = SettlementHistoryAdapter(self.store, controller=self.controller)
        self.parser = NpcapParserAdapter()
        self.parser.self_id = self.parser.native_self_id = SELF
        self.parser.self_confirmed = True
        self.parser.party_ids = {SELF, PEER}
        self.decoder = NpcapProtocolDecoder(capture_unknown=True)

    def rpc(self, method, args, actor=SELF, second=1):
        packed = msgpack.packb([{}, [actor, method, args]], use_bin_type=True)
        self.decoder._append_application(
            struct.pack('<IH', len(packed) + 2, 22) + packed, second, START + second,
        )
        return self.decoder._rpc_records()[0]

    def begin(self, template=7102990):
        encounter = self.controller.tracker.begin(
            instance_id='instance', started_at_ns=START * 1_000_000_000,
            participants_snapshot=[
                {'id': 'self', 'iid': SELF}, {'id': 'peer', 'iid': PEER},
            ], boss_template_id=template, boss_token=f'entity:{BOSS}', self_token='self',
        )
        self.controller.boss_entities[str(BOSS)] = {
            'token': f'entity:{BOSS}', 'template_id': template,
        }
        return encounter

    def test_current_teammate_cast_is_retained_without_changing_self_identity(self):
        record = self.rpc(1170, [86021030, BOSS, None, None, None, None, None, 2, 100001], PEER)
        updates = self.parser.process(record)
        casts = [value for kind, value in updates if kind == 'skill_cast']
        self.assertEqual(len(casts), 1)
        self.assertEqual(casts[0]['actor_id'], PEER)
        self.assertEqual(casts[0]['cast_source'], 'network_cast_broadcast')
        self.assertEqual(self.parser.self_id, SELF)
        self.assertFalse(self.decoder.unknown_records)

    def test_current_success_reply_is_distinct_from_buff_lifetime_update(self):
        success = self.rpc(1172, [86021030, 100001, 1])
        updates = self.parser.process(success)
        self.assertEqual(sum(kind == 'skill_cast' for kind, _ in updates), 1)
        buff = self.rpc(1121, [83310307, SELF, 3.0, 3.0, 1791212632737], second=2)
        self.assertEqual(buff['method'], 'OnMsgBuffTotalLifeChangeNew')
        self.assertFalse(any(kind == 'skill_cast' for kind, _ in self.parser.process(buff)))
        removed = self.rpc(1119, [81110008, PEER, 1], second=3)
        self.assertEqual(removed['method'], 'OnMsgRemoveBuffNew')
        self.assertFalse(any(kind == 'skill_cast' for kind, _ in self.parser.process(removed)))
        removed = self.rpc(1119, [86021030, PEER, 1], second=4)
        self.assertEqual(removed['method'], 'OnMsgRemoveBuffNew')
        self.assertFalse(any(kind == 'skill_cast' for kind, _ in self.parser.process(removed)))

    def test_heal_rpc_is_not_misclassified_as_a_hit(self):
        record = self.rpc(94, [PEER, SELF, 860230200, 1010, 300])
        self.assertEqual(record['method'], 'OnMsgHealSyncV2')
        heals = [value for kind, value in self.parser.process(record) if kind == 'heal']
        self.assertEqual(len(heals), 1)
        self.assertEqual(heals[0]['effective_healing'], 300)

    def test_passive_history_keeps_opening_when_local_damage_starts_late(self):
        encounter = self.begin()
        cast = {'filetime_100ns': FILETIME + 2 * 10_000_000, 'actor_id': PEER,
                'skill_id': 86021030, 'sequence': 10, 'cast_source': 'network_cast_broadcast'}
        self.adapter.observe_detail('skill_cast', cast)
        self.controller.tracker.end('VICTORY', (START + 20) * 1_000_000_000)
        encounter.encounter_duration_seconds = 20.0
        self.adapter.sync(self.controller.tracker)
        record = self.store.load(encounter.local_encounter_id)
        self.assertEqual(record['skill_cast_log']['rows'], [[2000, PEER, 86021030, 10]])
        self.assertEqual(_opening_rows_by_actor(record, {})[PEER][0]['source'], 'cast_broadcast')

    def test_hp_history_keeps_both_phases_without_local_damage_samples(self):
        encounter = self.begin()
        self.controller.boss_entities[str(BOSS + 1)] = {
            'token': f'entity:{BOSS+1}', 'template_id': 7102991,
        }
        for second, entity, hp in ((0, BOSS, 1000), (1, BOSS, 700), (2, BOSS, 510),
                                   (4, BOSS+1, 500), (5, BOSS+1, 200), (6, BOSS+1, 0)):
            self.controller.observe_boss_health({
                'entity_id': entity, 'current_hp': hp,
                'filetime_100ns': FILETIME + second * 10_000_000,
            })
        self.controller.tracker.end('VICTORY', (START + 6) * 1_000_000_000)
        encounter.encounter_duration_seconds = 6.0
        self.adapter.sync(self.controller.tracker)
        record = self.store.load(encounter.local_encounter_id)
        self.assertEqual(record['boss_hp_damage_samples']['rows'][-1][1], 990)
        self.assertEqual(record['boss_hp_damage_samples']['rows'][-1][2], 0)
        self.assertTrue(record['team_dps_timeline'])
        self.assertNotIn('display_team_dps_samples', record)

    def test_teammate_rpc_to_official_history_to_full_cast_upload(self):
        encounter = self.begin()
        first, second = character_token(1001), character_token(1002)
        encounter.self_token = first
        encounter.participants_snapshot = [
            {'id': first, 'iid': SELF, 'name': 'Self'},
            {'id': second, 'iid': PEER, 'name': 'Peer'},
        ]
        for elapsed in (2, 41):
            raw = self.rpc(1170, [86021030, BOSS, None, None, None, None, None, 2, 100001],
                           actor=PEER, second=elapsed)
            for kind, value in self.parser.process(raw):
                if kind == 'skill_cast':
                    self.adapter.observe_detail(kind, value)
        self.controller.tracker.end('VICTORY', (START + 60) * 1_000_000_000)
        encounter.settlement_status = 'SETTLED'
        encounter.encounter_duration_seconds = 60.0
        encounter.participants = [
            {'id': first, 'iid': SELF, 'damage': 2000},
            {'id': second, 'iid': PEER, 'damage': 1000},
        ]
        self.adapter.sync(self.controller.tracker)
        record = self.store.load(encounter.local_encounter_id)
        payload = build_upload_encounter(record, first)
        peer = next(row for row in payload['participants'] if row['character_id'] == second)
        self.assertEqual([row['time_ms'] for row in peer['opening_sequence']], [2000])
        self.assertEqual([row['time_ms'] for row in peer['cast_timeline']], [2000, 41000])
        self.assertTrue(all(row['source'] == 'cast_broadcast' for row in peer['cast_timeline']))

    def test_local_projection_preserves_saved_detail_after_scene_hp_is_cleared(self):
        encounter = self.begin()
        for elapsed, hp in ((0, 1000), (1, 700), (6, 0)):
            self.controller.observe_boss_health({
                'entity_id': BOSS, 'current_hp': hp, 'max_hp': 1000,
                'filetime_100ns': FILETIME + elapsed * 10_000_000,
            })
        self.controller.tracker.end('VICTORY', (START + 6) * 1_000_000_000)
        encounter.encounter_duration_seconds = 6.0
        self.adapter.sync(self.controller.tracker)
        stored = self.store.load(encounter.local_encounter_id)
        self.controller.boss_health_samples.clear()
        local = {**stored, 'encounter_id': 'local-meter', 'local_encounter_id': encounter.local_encounter_id}
        local['boss_hp_damage_samples'] = {
            'columns': ['time_seconds', 'observed_boss_hp_loss'],
            'rows': [[1, 300], [6, 1000]],
        }
        projected = self.adapter.project_local_record(self.controller.tracker, local)
        self.assertEqual(projected['boss_hp_damage_samples'], stored['boss_hp_damage_samples'])

    def test_late_detail_refreshes_an_already_uploaded_record_once(self):
        window = object.__new__(DpsWindow)
        window.root = SimpleNamespace(after=mock.Mock())
        window.closing = False
        window.automatic_upload_queued = set()
        window.history_upload_in_progress = set()
        record = {'encounter_id': 'victory', 'result': 'defeated',
                  'archive_reason': 'target_defeated', 'monster': {'template_id': 7102990}}
        window.combat_upload_states = {'victory': {'state': 'uploaded', 'detail_signature': ''}}
        detail = {**record, 'skill_cast_log': {'columns': ['time_ms','actor_id','skill_id','sequence'],
                                             'rows': [[2000, PEER, 86021030, 10]]}}
        self.assertTrue(window._queue_automatic_victory_upload(detail))
        self.assertFalse(window._queue_automatic_victory_upload(dict(detail)))
        window.combat_upload_states['victory']['detail_signature'] = window._history_detail_signature(detail)
        self.assertFalse(window._queue_automatic_victory_upload(dict(detail)))
        window.root.after.assert_called_once()

    def test_upload_detail_signature_survives_loading_saved_state(self):
        record = {'skill_cast_log': {'rows': [[2000, PEER, 86021030, 10]]}}
        signature = DpsWindow._history_detail_signature(record)
        states = combat_test.normalize_combat_upload_states({
            'records': {'victory': {'state': 'uploaded', 'detail_signature': signature}},
        })
        window = object.__new__(DpsWindow)
        window.combat_upload_states = states
        self.assertEqual(states['victory']['detail_signature'], signature)
        self.assertFalse(window._automatic_upload_needs_update('victory', record))

    def test_confirmed_death_tail_finishes_hp_curve_after_official_end(self):
        encounter = self.begin()
        for second, hp in ((0, 1000), (1, 700), (5, 100)):
            self.controller.observe_boss_health({
                'entity_id': BOSS, 'current_hp': hp, 'max_hp': 1000,
                'filetime_100ns': FILETIME + second * 10_000_000,
            })
        self.controller.tracker.end('VICTORY', (START + 6) * 1_000_000_000)
        encounter.encounter_duration_seconds = 6.0
        encounter.boss_current_hp = 0
        encounter.boss_health_source = 'server_statistics'
        self.adapter.sync(self.controller.tracker)
        self.assertTrue(self.controller.observe_boss_health({
            'entity_id': BOSS, 'current_hp': 0, 'death_confirmed': True,
            'filetime_100ns': FILETIME + 68_000_000,
        }))
        self.adapter.sync(self.controller.tracker)
        rows = self.store.load(encounter.local_encounter_id)['boss_hp_damage_samples']['rows']
        self.assertEqual(rows[-1][:4], [6, 1000, 0, 1000])
        self.assertEqual(encounter.boss_health_source, 'server_statistics')
        self.controller.observe_boss_health({
            'entity_id': BOSS, 'current_hp': 900, 'max_hp': 1000,
            'filetime_100ns': FILETIME + 70_000_000,
        })
        self.assertEqual(self.controller.boss_health_history(encounter)['rows'], rows)

    def test_wire_zero_is_history_only_until_victory_confirms_the_boundary(self):
        encounter = self.begin()
        self.parser.confirmed_boss_entities.add(BOSS)
        zero = {'method': 'OnMsgSyncCurrentHp', 'decoded_arguments': [0.0],
                'network_entity_id': BOSS, 'script_entity': BOSS, 'capture_source': 'npcap',
                'filetime_100ns': FILETIME + 68_000_000, 'arguments_synchronized': True}
        candidates = [value for kind, value in self.parser.process(zero)
                      if kind == 'boss_health_observation']
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]['end_zero_candidate'])
        self.assertNotIn('death_confirmed', candidates[0])
        for second, hp in ((0, 1000), (1, 700), (5, 100)):
            self.controller.observe_boss_health({
                'entity_id': BOSS, 'current_hp': hp,
                'filetime_100ns': FILETIME + second * 10_000_000,
            })
        self.controller.observe_boss_health(candidates[0])
        self.controller.tracker.end('WIPE', (START + 6) * 1_000_000_000)
        rows = self.controller.boss_health_history(encounter)['rows']
        self.assertEqual(rows[-1][2], 100)
        encounter.result = 'VICTORY'
        rows = self.controller.boss_health_history(encounter)['rows']
        self.assertEqual(rows[-1][:3], [6, 1000, 0])

    def test_model_archive_updates_when_only_cast_detail_arrives_late(self):
        model = combat_test.CombatModelTests._clock_ready_model('late-cast')
        model.archive_current()
        self.assertNotIn('skill_cast_log', model.completed_combats[-1])
        model.ingest_skill_cast({'filetime_100ns': BASE_FILETIME + 5 * 10_000_000,
            'actor_id': SELF_ID, 'skill_id': 86021030, 'sequence': 10,
            'cast_source': 'local_success_response', 'source_method': 'RetCastSkillSuccessNew'})
        self.assertTrue(model.archive_current())
        self.assertEqual(len(model.completed_combats), 1)
        self.assertEqual(len(model.completed_combats[-1]['skill_cast_log']['rows']), 1)

    def test_cast_after_frozen_battle_does_not_rewrite_old_archive(self):
        model = combat_test.CombatModelTests._clock_ready_model('post-battle-cast')
        end_ns = (BASE_FILETIME + 20 * 10_000_000 - 116_444_736_000_000_000) * 100
        self.assertTrue(model.freeze_verified_encounter(end_ns, 'VICTORY'))
        self.assertEqual(len(model.completed_combats), 1)
        model.ingest_skill_cast({
            'filetime_100ns': BASE_FILETIME + 50 * 10_000_000,
            'actor_id': SELF_ID, 'skill_id': 86021030, 'sequence': 20,
            'cast_source': 'local_success_response', 'source_method': 'RetCastSkillSuccessNew',
        })
        self.assertFalse(model.archive_current())
        self.assertNotIn('skill_cast_log', model.completed_combats[-1])


if __name__ == '__main__':
    unittest.main()
