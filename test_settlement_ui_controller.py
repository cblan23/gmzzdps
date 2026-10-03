import os
import tempfile
import time
import unittest
from copy import deepcopy
from types import SimpleNamespace
from combat_statistics import normalize_statistics
from encounter_repository import EncounterRepository
from encounter_tracker import EncounterTracker
from network_state import NetworkPacketParser
from settlement_presenter import encounter_view
from settlement_ui_controller import SettlementUIController, parser_observation
from test_encounter_settlement import packet,projection_members,ROSTER,NS


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controller=SettlementUIController(EncounterRepository(self.directory.name))
        self.context={'instance_id':'instance','dungeon_id':10,'map_id':20,'stage_id':55,'stage_index':1,
            'self_token':'self','roster':ROSTER,'roster_complete':True,
            'party_session_id':7,'in_team':True,
            'bosses':{'300':{'token':'boss','template_id':7102990,'name':'Boss'}}}

    def send(self,method,at,entity,args):
        record={'method':method,'capture_timestamp_ns':at*NS,'network_entity_id':entity,'decoded_arguments':args}
        self.controller.process({'record':record,'context':self.context,'timestamp_ns':at*NS,'updates':[]})

    def wipe(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        self.send('OnMsgEntityDead',110,100,[0,'boss'])
        self.send('OnMsgSyncFightMode',110,300,[0])
        self.send('OnMsgEntityDead',110,200,[0,'boss'])

    def test_unrelated_entity_does_not_copy_entire_history(self):
        self.send('OnMsgSyncFightMode', 100, 300, [2])
        self.controller.tracker.to_dict = lambda: self.fail(
            'An unrelated entity event copied the whole encounter journal'
        )

        self.send('NpcapEntityCreated', 101, 999, [])

        self.assertEqual(self.controller.tracker.current_id is not None, True)

    def test_delayed_old_table_updates_history_not_live_window(self):
        self.wipe();old=list(self.controller.tracker.encounters.values())[0]
        self.assertEqual(old.party_session_id,7)
        self.assertEqual(old.encounter_duration_seconds, 10)
        self.assertEqual(old.duration_source, 'verified_shared_encounter_clock')
        self.send('OnMsgSyncFightMode',200,300,[2])
        self.send('OnMsgUpdateStageCombatStatistics',200,100,packet()['decoded_arguments'])
        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual(self.controller.visible()['settlement_status'],'LIVE')
        self.assertTrue(all(r['damage'] is None for r in self.controller.visible()['members']))

    def test_same_party_session_allows_only_self_statistics_after_teammates_leave(self):
        self.wipe()
        old = next(iter(self.controller.tracker.encounters.values()))
        self.send('OnMsgSyncFightMode', 200, 300, [2])
        self.context['roster'] = [ROSTER[0]]
        self.context['in_team'] = True
        self.context['party_session_id'] = 7
        raw = packet(received=200, damage=654)
        raw['decoded_arguments'][0][5] = {
            'self': raw['decoded_arguments'][0][5]['self']
        }
        snapshot = normalize_statistics(
            raw, instance_id='instance', dungeon_id=10, map_id=20
        )[0]

        target = self.controller._direct_server_statistics_target(
            snapshot, self.context
        )

        self.assertIs(target, old)
        self.controller.tracker.accept(
            snapshot, preferred_encounter_id=target.local_encounter_id
        )
        self.assertEqual(old.settlement_status, 'SETTLED')
        self.assertEqual(old.participants[0]['damage'], 654)

    def test_delayed_old_table_uses_server_result_after_runtime_iids_rebind(self):
        self.wipe();old=next(iter(self.controller.tracker.encounters.values()))
        rebound=[
            {'id':'self','iid':101,'name':'Self'},
            {'id':'peer','iid':201,'name':'Peer'},
        ]
        self.context['roster']=rebound
        self.send('OnMsgSyncFightMode',200,300,[2])
        raw=packet(received=200,damage=654)
        raw['decoded_arguments'][0][5]['self'][1]=101
        raw['decoded_arguments'][0][5]['peer'][1]=201

        self.send(
            'OnMsgUpdateStageCombatStatistics',200,101,
            raw['decoded_arguments'],
        )

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual([row['damage'] for row in old.participants],[654,654])
        self.assertEqual(
            {row['id']:row['iid'] for row in old.participants},
            {'self':101,'peer':201},
        )
        self.assertFalse(self.controller.tracker.orphans)

    def test_new_scene_does_not_reject_delayed_wipe_with_previous_dungeon_stage(self):
        parser = NetworkPacketParser()
        parser.dungeon_stage_id = 5_150_111
        parser.dungeon_stage_phase = 1
        parser.process({'method': 'OnMsgBeforeEnterNewSpace', 'decoded_arguments': []})
        parser.self_id = 100
        parser.self_token = 'self'
        parser.party_tokens = {'peer'}
        parser.token_actors = {'self': 100, 'peer': 200}
        parser.confirmed_boss_entities = {300}
        parser.wire_entity_tokens = {300: 'boss'}
        parser.entity_template_ids = {300: 7100401}
        parser.wire_instance_id = 'instance'
        parser.wire_map_id = None
        observation = parser_observation(parser, {
            'method': 'OnMsgSyncFightMode', 'network_entity_id': 300,
            'capture_timestamp_ns': 100 * NS, 'decoded_arguments': [2],
        }, [])
        self.context = observation['context']
        self.assertIsNone(self.context['stage_id'])
        self.assertIsNone(self.context['stage_index'])
        self.wipe()
        old = self.controller.tracker.encounters[self.controller.last_visible_id]
        raw = packet(received=200, seconds=99)
        raw['decoded_arguments'][0][0] = 5_150_073
        self.send('OnMsgUpdateStageCombatStatistics', 200, 100, raw['decoded_arguments'])
        self.assertEqual(old.settlement_status, 'PENDING')
        self.send('OnMsgSyncFightMode', 200, 300, [2])
        self.assertEqual(old.settlement_status, 'SETTLED')
        self.assertEqual(old.stage_id, 5_150_073)
        self.assertEqual(old.server_battle_id, 'battle-1')
        self.assertEqual(old.match_confidence, 'high')
        self.assertFalse(self.controller.tracker.orphans)

    def test_live_heartbeat_can_shrink_stale_complete_roster_during_pull(self):
        stale_roster=ROSTER+[
            {'id':f'old-bot-{index}','iid':300+index,'name':f'Bot {index}','is_ai':True}
            for index in range(4)
        ]
        self.context['roster']=stale_roster
        self.send('OnMsgSyncFightMode',100,300,[2])
        current=self.controller.tracker.encounters[self.controller.tracker.current_id]
        self.assertTrue(current.capture_complete)
        self.assertEqual(len(current.participants_snapshot),6)

        replacement=deepcopy(self.context)
        replacement['roster']=deepcopy(ROSTER)
        replacement['roster_replace']=True
        observation={
            'record':{
                'method':'LivePartyRosterSnapshot',
                'capture_timestamp_ns':101*NS,
                'decoded_arguments':[],
            },
            'context':replacement,
            'timestamp_ns':101*NS,
            'updates':[],
        }
        self.assertTrue(self.controller.process(observation))
        self.assertEqual(
            {row['id'] for row in current.participants_snapshot},
            {'self','peer'},
        )
        self.assertEqual({row['id'] for row in current.participants},{'self','peer'})

    def test_next_scene_roster_replacement_cannot_rewrite_previous_wipe(self):
        self.wipe()
        previous=next(iter(self.controller.tracker.encounters.values()))
        before=deepcopy(previous.participants_snapshot)
        replacement=deepcopy(self.context)
        replacement['roster']=[deepcopy(ROSTER[0])]
        replacement['roster_replace']=True
        observation={
            'record':{
                'method':'LivePartyRosterSnapshot',
                'capture_timestamp_ns':150*NS,
                'decoded_arguments':[],
            },
            'context':replacement,
            'timestamp_ns':150*NS,
            'updates':[],
        }

        self.assertFalse(self.controller.process(observation))
        self.assertEqual(previous.participants_snapshot,before)

        self.context['roster']=[deepcopy(ROSTER[0])]
        self.send(
            'OnMsgUpdateStageCombatStatistics',200,100,
            packet(received=200)['decoded_arguments'],
        )
        self.send('OnMsgSyncFightMode',200,300,[2])

        self.assertEqual(previous.settlement_status,'SETTLED')
        self.assertEqual(
            {row['id'] for row in previous.participants},{'self','peer'}
        )
        current=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.assertEqual(set(current.roster),{'self'})

    def test_server_table_before_new_start_does_not_hide_pending_old(self):
        self.wipe()
        self.send('OnMsgUpdateStageCombatStatistics',200,100,packet()['decoded_arguments'])
        self.assertEqual(self.controller.visible()['status_text'],'延迟补齐')
        self.send('OnMsgSyncFightMode',200,300,[2])
        self.assertEqual(self.controller.visible()['status_text'],'战后结算')
        current=self.controller.tracker.encounters[self.controller.tracker.current_id]
        self.assertIsNone(current.stage_id)
        self.assertIsNone(current.stage_index)

    def test_consecutive_wipes_use_statistics_then_challenge_order(self):
        self.wipe()
        first=next(iter(self.controller.tracker.encounters.values()))
        self.send('OnMsgSyncFightMode',120,300,[2])
        self.send('OnMsgEntityDead',130,100,[0,'boss'])
        self.send('OnMsgSyncFightMode',130,300,[0])
        self.send('OnMsgEntityDead',130,200,[0,'boss'])
        second=max(
            self.controller.tracker.encounters.values(),
            key=lambda encounter:encounter.started_at_ns,
        )
        raw=packet(battle='second-battle',received=200,seconds=99,damage=222)
        self.send(
            'OnMsgUpdateStageCombatStatistics',200,100,
            raw['decoded_arguments'],
        )
        self.assertEqual(first.settlement_status,'PENDING')
        self.assertEqual(second.settlement_status,'PENDING')

        start_ns=200*NS+29_600
        self.controller.process({
            'record':{
                'method':'OnMsgSyncFightMode',
                'capture_timestamp_ns':start_ns,
                'network_entity_id':300,
                'decoded_arguments':[2],
            },
            'context':self.context,
            'timestamp_ns':start_ns,
            'updates':[],
        })

        third=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.assertEqual(first.settlement_status,'PENDING')
        self.assertEqual(second.settlement_status,'SETTLED')
        self.assertEqual(second.server_battle_id,'second-battle')
        self.assertEqual([row['damage'] for row in second.participants],[222,222])
        self.assertEqual(third.settlement_status,'LIVE')
        self.assertTrue(all(row['damage'] is None for row in third.participants))
        self.assertFalse(self.controller.tracker.orphans)

    def test_one_hundred_consecutive_wipes_remain_settled_and_displayable(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        settled=[]
        for attempt in range(100):
            start=100+attempt*20
            ended=self.controller.tracker.encounters[
                self.controller.tracker.current_id
            ]
            self.send('OnMsgEntityDead',start+10,100,[0,'boss'])
            self.send('OnMsgSyncFightMode',start+10,300,[0])
            self.send('OnMsgEntityDead',start+10,200,[0,'boss'])
            self.assertEqual(ended.result,'WIPE',attempt)

            next_start=start+20
            expected_damage=10_000+attempt
            result=packet(
                battle=f'battle-{attempt+1}',received=next_start,
                seconds=10,damage=expected_damage,
            )
            self.send(
                'OnMsgUpdateStageCombatStatistics',next_start,100,
                result['decoded_arguments'],
            )
            start_ns=next_start*NS+29_600
            self.controller.process({
                'record':{
                    'method':'OnMsgSyncFightMode',
                    'capture_timestamp_ns':start_ns,
                    'network_entity_id':300,
                    'decoded_arguments':[2],
                },
                'context':self.context,
                'timestamp_ns':start_ns,
                'updates':[],
            })

            self.assertEqual(ended.settlement_status,'SETTLED',attempt)
            self.assertEqual(
                [row['damage'] for row in ended.participants],
                [expected_damage,expected_damage],
                attempt,
            )
            view=encounter_view(ended)
            self.assertTrue(
                all(row['dps'] is not None for row in view['members']),
                attempt,
            )
            self.assertFalse(self.controller.tracker.orphans,attempt)
            settled.append(ended)

        current=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.assertEqual(current.settlement_status,'LIVE')
        self.assertEqual(len(settled),100)
        self.assertTrue(all(row.settlement_status=='SETTLED' for row in settled))
        latest_view=encounter_view(settled[-1])
        self.assertEqual(
            [row['damage'] for row in latest_view['members']],
            [10_099,10_099],
        )
        self.assertEqual(
            [row['dps'] for row in latest_view['members']],
            [1_009.9,1_009.9],
        )

    def test_live_third_pull_does_not_force_second_wipe_table_onto_latest_pending(self):
        self.wipe()
        self.send('OnMsgSyncFightMode',120,300,[2])
        self.send('OnMsgEntityDead',130,100,[0,'boss'])
        self.send('OnMsgSyncFightMode',130,300,[0])
        self.send('OnMsgEntityDead',130,200,[0,'boss'])
        pending=sorted(
            self.controller.tracker.encounters.values(),
            key=lambda row:row.started_at_ns,
        )
        self.assertEqual(len(pending),2)
        self.send('OnMsgSyncFightMode',150,300,[2])
        snapshot=normalize_statistics(
            packet(battle='unknown-earlier',received=200,damage=321),
            instance_id='instance',dungeon_id=10,map_id=20,
        )[0]
        self.assertIsNone(
            self.controller._direct_server_statistics_target(snapshot,self.context)
        )
        self.send('OnMsgUpdateStageCombatStatistics',200,100,
                  packet(battle='unknown-earlier',received=200,damage=321)['decoded_arguments'])
        self.assertTrue(all(row.settlement_status=='PENDING' for row in pending))
        self.assertEqual(len(self.controller.tracker.orphans),1)

    def test_new_boss_challenge_preserves_previous_wipe_before_delayed_table(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        first=self.controller.tracker.encounters[self.controller.tracker.current_id]
        self.context['bosses']['301']={
            'token':'next-boss-token','template_id':7100215,'name':'Next Boss',
        }
        old_table=packet(received=200,success=False,damage=222)
        self.send('OnMsgUpdateStageCombatStatistics',200,100,old_table['decoded_arguments'])
        self.assertEqual(first.result,'IN_PROGRESS')
        self.assertEqual(len(self.controller.tracker.orphans),1)

        self.controller.process({
            'record':{
                'method':'OnMsgSyncFightMode',
                'capture_timestamp_ns':200*NS+29_600,
                'network_entity_id':301,
                'decoded_arguments':[2],
            },
            'context':self.context,
            'timestamp_ns':200*NS+29_600,
            'updates':[],
        })

        current=self.controller.tracker.encounters[self.controller.tracker.current_id]
        self.assertEqual(first.result,'WIPE')
        self.assertEqual(first.settlement_status,'SETTLED')
        self.assertEqual(first.server_battle_id,'battle-1')
        self.assertEqual([row['damage'] for row in first.participants],[222,222])
        self.assertEqual(current.result,'IN_PROGRESS')
        self.assertEqual(current.settlement_status,'LIVE')
        self.assertIsNone(current.stage_statistics)
        self.assertFalse(self.controller.tracker.orphans)

    def test_new_boss_boundary_settles_previous_with_changed_server_roster(self):
        self.context.update(
            roster=[deepcopy(ROSTER[0])],
            roster_complete=False,
            in_team=False,
            party_session_id=0,
        )
        self.send('OnMsgSyncFightMode',100,300,[2])
        previous=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.send('OnMsgEntityDead',110,100,[0,'boss'])
        self.send('OnMsgSyncFightMode',110,300,[0])
        self.assertEqual(previous.result,'WIPE')

        server_roster=[
            {'id':'self','iid':100,'name':'Self','damage':379_763,'seconds':14},
            {'id':'old-peer','iid':201,'name':'Old Peer','damage':0,'seconds':4},
            {'id':'projection-a','iid':301,'name':'A\u00b7\u6295\u5f71','damage':11_541_027,'seconds':379},
            {'id':'projection-b','iid':302,'name':'B\u00b7\u6295\u5f71','damage':11_996_564,'seconds':379},
        ]
        members={
            row['id']:{
                0:row['id'],1:row['iid'],5:row['name'],6:row['damage'],
                19:row['seconds'],33:{123:1},34:{123:row['damage']},
                36:{'wire-old-boss':20},37:{'wire-old-boss':1},
            }
            for row in server_roster
        }
        raw=packet(
            battle='previous-battle',received=200,members=members
        )
        raw['decoded_arguments'][0][0]=5_150_075
        raw['decoded_arguments'][0][1]=3
        self.send(
            'OnMsgUpdateStageCombatStatistics',200,100,
            raw['decoded_arguments'],
        )
        self.assertEqual(previous.settlement_status,'PENDING')

        self.context['roster']=[
            {'id':'self','iid':102,'name':'Self'},
            {'id':'new-peer','iid':401,'name':'New Peer'},
        ]
        self.context['bosses']['301']={
            'token':'new-boss-token','template_id':7100401,'name':'New Boss',
        }
        self.send('OnMsgSyncFightMode',200,301,[2])

        self.assertEqual(previous.settlement_status,'SETTLED')
        self.assertEqual(previous.server_battle_id,'previous-battle')
        self.assertEqual(len(previous.participants),4)
        self.assertEqual(
            sum(row['damage'] for row in previous.participants),23_917_354
        )
        current=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.assertEqual(set(current.roster),{'self','new-peer'})
        self.assertIsNone(current.stage_statistics)
        self.assertFalse(self.controller.tracker.orphans)

        persisted=EncounterRepository(self.directory.name).load().encounters[
            previous.local_encounter_id
        ]
        self.assertEqual(persisted.settlement_status,'SETTLED')
        self.assertEqual(len(persisted.participants),4)

    def test_new_boss_challenge_retains_victory_with_explicit_success(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        first=self.controller.tracker.encounters[self.controller.tracker.current_id]
        self.context['bosses']['301']={
            'token':'next-boss-token','template_id':7100215,'name':'Next Boss',
        }
        self.send('OnMsgUpdateStageCombatStatistics',200,100,
                  packet(received=200,success=True)['decoded_arguments'])
        self.controller.process({
            'record':{
                'method':'OnMsgSyncFightMode',
                'capture_timestamp_ns':200*NS+29_600,
                'network_entity_id':301,
                'decoded_arguments':[2],
            },
            'context':self.context,
            'timestamp_ns':200*NS+29_600,
            'updates':[],
        })
        self.assertEqual(first.result,'VICTORY')
        self.assertEqual(first.settlement_status,'SETTLED')
        self.assertIsNone(
            self.controller.tracker.encounters[self.controller.tracker.current_id].stage_statistics
        )

    def test_final_boss_settlement_is_immediate_and_all_stays_separate(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.controller.observe_boss_health({
            'entity_id':300,'current_hp':285_738,'max_hp':59_392_271,
            'capture_timestamp_ns':105*NS,
        })
        self.send(
            'OnMsgDungeonStageSettlement',110,100,
            [55,True,'self',10,{}],
        )
        self.assertEqual(encounter.boss_current_hp,0)
        self.assertEqual(encounter.boss_max_hp,59_392_271)
        self.assertEqual(encounter.boss_health_observed_at_ns,110*NS)
        stage=packet(
            battle='final-battle',received=111,success=True,damage=321
        )['decoded_arguments'][0]
        all_members=deepcopy(stage[5])
        for member in all_members.values():
            member[6]=member[6]*3
        self.send(
            'OnMsgSettlementCombatStatistics',111,100,
            [{0:all_members,55:stage[5]},stage],
        )

        self.assertIsNone(self.controller.tracker.current_id)
        self.assertEqual(encounter.result,'VICTORY')
        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(encounter.server_battle_id,'final-battle')
        self.assertEqual(encounter.stage_statistics['scope'],'STAGE')
        self.assertEqual(encounter.all_statistics['scope'],'ALL')
        self.assertEqual(
            [row['damage'] for row in encounter.participants],[321,321]
        )
        visible=self.controller.visible()
        self.assertEqual(visible['statistics_scope'],'ALL')
        self.assertEqual(
            [row['damage'] for row in visible['members']],[963,963]
        )
        revision=encounter.revision
        self.send(
            'OnMsgSettlementCombatStatistics',112,100,
            [{0:all_members,55:stage[5]},stage],
        )
        self.assertEqual(encounter.revision,revision)
        self.assertEqual(len(self.controller.tracker.encounters),1)

    def test_solo_final_boss_result_settles_immediately_after_boss_death(self):
        self.context.update(
            roster=[deepcopy(ROSTER[0])],
            in_team=False,
            party_session_id=0,
        )
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.send('OnMsgEntityDead',109,300,[0,'boss'])
        self.assertIsNone(self.controller.tracker.current_id)

        stage=packet(
            battle='solo-final',received=110,success=True,damage=654
        )['decoded_arguments'][0]
        stage[0]=5_150_075
        stage[1]=3
        stage[5]={'self':stage[5]['self']}
        self.send(
            'OnMsgSettlementCombatStatistics',110,100,
            [{5_150_075:stage[5]},stage],
        )

        self.assertEqual(encounter.result,'VICTORY')
        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(encounter.server_battle_id,'solo-final')
        self.assertEqual(encounter.stage_id,5_150_075)
        self.assertEqual([row['damage'] for row in encounter.participants],[654])
        self.assertFalse(self.controller.tracker.orphans)

    def test_final_statistics_closes_live_boss_when_stage_settlement_is_absent(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.controller.observe_boss_health({
            'entity_id':300,'current_hp':1_000,'max_hp':1_000,
            'capture_timestamp_ns':105*NS,
        })
        self.send('OnMsgSyncFightMode',110,300,[0])
        self.controller.observe_boss_health({
            'entity_id':300,'current_hp':0,'death_confirmed':True,
            'capture_timestamp_ns':110*NS+69_000_000,
        })
        stage=packet(
            battle='final-without-stage-rpc',received=140,
            success=True,damage=432,
        )['decoded_arguments'][0]
        all_members=deepcopy(stage[5])
        for member in all_members.values():
            member[6]*=3

        self.send(
            'OnMsgSettlementCombatStatistics',140,100,
            [{0:all_members,55:stage[5]},stage],
        )

        self.assertIsNone(self.controller.tracker.current_id)
        self.assertEqual(encounter.result,'VICTORY')
        self.assertEqual(encounter.ended_at_ns,110*NS)
        self.assertEqual(encounter.encounter_duration_seconds,10)
        self.assertEqual(encounter.boss_current_hp,0)
        self.assertEqual(encounter.boss_max_hp,1_000)
        self.assertEqual(
            encounter.boss_health_observed_at_ns,110*NS+69_000_000
        )
        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(encounter.server_battle_id,'final-without-stage-rpc')
        self.assertEqual(encounter.stage_statistics['scope'],'STAGE')
        self.assertEqual(encounter.all_statistics['scope'],'ALL')
        self.assertEqual(
            [row['damage'] for row in encounter.participants],[432,432]
        )
        self.assertFalse(self.controller.tracker.orphans)

    def test_successful_final_statistics_sets_zero_without_zero_hp_tail(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.controller.observe_boss_health({
            'entity_id':300,'current_hp':36132,'max_hp':7_578_453,
            'capture_timestamp_ns':105*NS,
        })
        self.send('OnMsgSyncFightMode',110,300,[0])
        stage=packet(
            battle='final-without-zero-tail',received=140,
            success=True,damage=7_578_453,
        )['decoded_arguments'][0]

        self.send(
            'OnMsgSettlementCombatStatistics',140,100,
            [{0:deepcopy(stage[5]),55:stage[5]},stage],
        )

        self.assertEqual(encounter.result,'VICTORY')
        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(encounter.boss_current_hp,0)
        self.assertEqual(encounter.boss_max_hp,7_578_453)
        self.assertEqual(encounter.boss_health_observed_at_ns,140*NS)
        self.assertEqual(encounter.boss_health_source,'server_statistics')

    def test_current_final_table_adds_missed_projections_with_multiple_target_references(self):
        self.context['bosses'] = {
            '300': {
                'token': 'entity:300',
                'template_id': 7_115_080,
                'name': 'Memory Boss',
            }
        }
        self.send('OnMsgSyncFightMode', 100, 300, [2])
        encounter = self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.send('OnMsgSyncFightMode', 110, 300, [0])

        members = projection_members('server-boss')
        for values in members.values():
            values[36]['encounter-add'] = 1
        stage = packet(
            battle='single-boss-final',
            received=111,
            success=True,
            members=members,
        )['decoded_arguments'][0]
        self.send(
            'OnMsgSettlementCombatStatistics',
            111,
            100,
            [{0: deepcopy(members), 55: deepcopy(members)}, stage],
        )

        self.assertIsNone(self.controller.tracker.current_id)
        self.assertEqual(encounter.settlement_status, 'SETTLED')
        self.assertEqual(len(encounter.participants), 6)
        self.assertEqual(len(encounter.participants_snapshot), 6)
        self.assertFalse(self.controller.tracker.orphans)

    def test_final_server_statistics_are_direct_even_when_runtime_iids_changed(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        stage=packet(
            battle='final-rebound',received=111,success=True,damage=777
        )['decoded_arguments'][0]
        stage[0]=9_999_999
        stage[5]['self'][1]=101
        stage[5]['peer'][1]=201
        all_members=deepcopy(stage[5])

        self.send(
            'OnMsgSettlementCombatStatistics',111,101,
            [{0:all_members,9_999_999:deepcopy(all_members)},stage],
        )

        self.assertIsNone(self.controller.tracker.current_id)
        self.assertEqual(encounter.result,'VICTORY')
        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(encounter.server_battle_id,'final-rebound')
        self.assertEqual([row['damage'] for row in encounter.participants],[777,777])
        self.assertEqual(
            {row['id']:row['iid'] for row in encounter.participants},
            {'self':101,'peer':201},
        )
        self.assertFalse(self.controller.tracker.orphans)

    def test_late_hp_start_and_real_six_member_final_share_one_clock(self):
        damages=[610_770,0,1_085_365,1_204_643,229_962,1_190_827]
        tokens=['mojue','self','ai-1','ai-2','ai-3','ai-4']
        names=['墨爵','莫雪','月眠川·投影','凝光炼彩·投影','步摇·投影','嘉德丽雅·投影']
        actors=[101,102,103,104,105,106]
        self.context.update({
            'stage_id':5_150_104,
            'stage_index':1,
            'self_token':'self',
            'roster':[
                {
                    'id':token,'iid':actor,'name':name,
                    'profession_id':1_200_001+(index % 7),
                    'is_ai':index >= 2,
                }
                for index,(token,name,actor) in enumerate(
                    zip(tokens,names,actors)
                )
            ],
            'roster_complete':True,
            'bosses':{
                '300':{
                    'token':'dragon-token','template_id':7_115_041,
                    'name':'战争巨龙',
                },
            },
        })
        self.send('NpcapBossHpActivity',100,300,[])
        encounter=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.assertEqual(encounter.started_at_ns,100*NS)

        self.send(
            'OnMsgDungeonStageSettlement',140,102,
            [5_150_104,True,'self',4_250_302,{}],
        )
        member_table={}
        for token,name,actor,damage in zip(tokens,names,actors,damages):
            values={
                0:token,1:actor,4:1_200_001,5:name,
                7:0,17:0,19:58,33:{},34:{},35:{},36:{},37:{},
            }
            if token!='self':
                values[6]=damage
                values[34]={8_600_001:damage}
            member_table[token]=values
        stage={
            0:5_150_104,1:1,2:'real-final-battle',3:True,
            5:member_table,
        }
        self.send(
            'OnMsgSettlementCombatStatistics',141,102,
            [{0:deepcopy(member_table),5_150_104:deepcopy(member_table)},stage],
        )

        self.assertIsNone(self.controller.tracker.current_id)
        self.assertEqual(encounter.result,'VICTORY')
        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(encounter.duration_source,'server_encounter_clock')
        self.assertEqual(encounter.encounter_duration_seconds,58.0)
        self.assertEqual(len(encounter.participants),6)
        self.assertEqual(
            sum(row['damage'] for row in encounter.participants),
            4_321_567,
        )
        by_token={row['id']:row for row in encounter.participants}
        self.assertEqual(
            {token:by_token[token]['dps'] for token in tokens},
            {token:damage/58.0 for token,damage in zip(tokens,damages)},
        )
        self.assertEqual(
            self.controller.visible()['local_encounter_id'],
            encounter.local_encounter_id,
        )
        persisted=EncounterRepository(self.directory.name).load().encounters[
            encounter.local_encounter_id
        ]
        self.assertEqual(persisted.encounter_duration_seconds,58.0)
        self.assertEqual(
            sum(row['damage'] for row in persisted.participants),
            4_321_567,
        )

        revision=encounter.revision
        self.send(
            'OnMsgSettlementCombatStatistics',142,102,
            [{0:deepcopy(member_table),5_150_104:deepcopy(member_table)},stage],
        )
        self.assertEqual(encounter.revision,revision)
        self.assertEqual(len(self.controller.tracker.encounters),1)

    def test_incomplete_roster_cannot_settle_automatically(self):
        self.context['roster_complete']=False
        self.wipe()
        self.send('OnMsgUpdateStageCombatStatistics',200,100,packet()['decoded_arguments'])
        self.assertEqual(self.controller.visible()['status_text'],'等待结算')
        self.assertEqual(len(self.controller.tracker.orphans),1)

    def test_capture_gap_prevents_first_match(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        self.controller.mark_gap()
        self.send('OnMsgEntityDead',110,100,[0,'boss'])
        self.send('OnMsgSyncFightMode',110,300,[0])
        self.send('OnMsgEntityDead',110,200,[0,'boss'])
        self.send('OnMsgUpdateStageCombatStatistics',200,100,packet()['decoded_arguments'])
        self.assertEqual(self.controller.visible()['status_text'],'等待结算')
        encounter=next(iter(self.controller.tracker.encounters.values()))
        self.assertTrue(encounter.automatic_match_blocked)

    def test_explicit_success_boundary_ends_matching_stage_as_victory(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter = self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]

        self.send(
            'OnMsgDungeonStageSettlement', 110, 100,
            [55, True, 'self', 10, {}],
        )

        self.assertIsNone(self.controller.tracker.current_id)
        self.assertEqual(encounter.result, 'VICTORY')
        self.assertEqual(encounter.ended_at_ns, 110 * NS)
        self.send(
            'OnMsgUpdateStageCombatStatistics', 111, 100,
            packet(success=True)['decoded_arguments'],
        )
        self.assertEqual(encounter.settlement_status, 'SETTLED')

    def test_leave_quest_control_ends_live_instance_once_and_clears_bosses(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter_id=self.controller.tracker.current_id
        self.controller.pending_boss_starts['999']={
            'timestamp_ns':100*NS,
            'context':deepcopy(self.context),
        }

        self.send('OnMsgLeaveQuestControl',110,100,[])

        encounter=self.controller.tracker.encounters[encounter_id]
        self.assertEqual(encounter.result,'RESET')
        self.assertEqual(encounter.settlement_status,'ABANDONED')
        self.assertEqual(encounter.ended_at_ns,110*NS)
        self.assertIsNone(self.controller.tracker.current_id)
        self.assertIsNone(self.controller.instance_id)
        self.assertFalse(self.controller.boss_entities)
        self.assertFalse(self.controller.boss_in_combat)
        self.assertFalse(self.controller.pending_boss_starts)

        self.send('OnMsgLeaveQuestControl',111,100,[])
        self.assertEqual(len(self.controller.tracker.encounters),1)
        self.assertFalse(self.controller.boss_entities)

    def test_delayed_old_leave_does_not_reset_a_newer_live_pull(self):
        self.send('OnMsgSyncFightMode', 200, 300, [2])
        encounter_id = self.controller.tracker.current_id
        before = deepcopy(self.controller.tracker.to_dict())

        changed = self.controller.process({
            'record': {
                'method': 'OnMsgLeaveQuestControl',
                'capture_timestamp_ns': 150 * NS,
                'network_entity_id': 100,
                'decoded_arguments': [],
            },
            'context': self.context,
            'timestamp_ns': 150 * NS,
            'updates': [],
        })

        self.assertFalse(changed)
        self.assertEqual(self.controller.tracker.current_id, encounter_id)
        self.assertEqual(self.controller.tracker.to_dict(), before)
        self.assertEqual(self.controller.instance_id, 'instance')

    def test_adjacent_victory_repairs_entity_token_and_projection_roster(self):
        self.context['stage_id']=54
        self.context['stage_index']=0
        self.context['bosses']['300']['token']='entity:300'
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        encounter.capture_complete=False

        self.send('OnMsgEntityDead',110,300,[0,'entity:300'])
        self.assertEqual(encounter.result,'VICTORY')
        self.assertIsNone(self.controller.tracker.current_id)
        self.send(
            'OnMsgDungeonStageSettlement',111,100,
            [55,True,'self',10,{36:{'stable-boss-token':123}}],
        )

        self.assertEqual(encounter.stage_id,55)
        self.assertEqual(encounter.boss_token,'stable-boss-token')
        raw=packet(
            received=112,success=True,
            members=projection_members('stable-boss-token'),
        )
        self.send(
            'OnMsgSettlementCombatStatistics',112,100,
            [{55:raw['decoded_arguments'][0][5]},raw['decoded_arguments'][0]],
        )
        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(len(encounter.participants),6)
        self.assertFalse(self.controller.tracker.orphans)

    def test_settled_boss_roster_cannot_shrink_after_a_member_is_kicked(self):
        self.send('OnMsgSyncFightMode', 100, 300, [2])
        encounter = self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.send('OnMsgEntityDead', 110, 300, [0, 'boss'])
        encounter.capture_complete = False
        encounter.settlement_status = 'SETTLED'
        before = deepcopy(encounter.participants_snapshot)

        changed = self.controller._refresh_incomplete_encounter_roster(
            {**self.context, 'roster': [ROSTER[0]],
             'roster_replace': True}, 111 * NS,
        )

        self.assertFalse(changed)
        self.assertEqual(encounter.participants_snapshot, before)

    def test_known_boss_phase_transition_keeps_settlement_matching_enabled(self):
        self.send('OnMsgSyncFightMode',100,300,[2])
        encounter = self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.context['bosses']['301'] = {
            'token': 'boss-phase-two',
            'template_id': 7102991,
            'name': 'Boss phase two',
        }

        self.send('OnMsgSyncFightMode',105,301,[2])

        self.assertEqual(self.controller.tracker.current_id, encounter.local_encounter_id)
        self.assertTrue(encounter.capture_complete)
        self.assertFalse(encounter.automatic_match_blocked)
        self.send(
            'OnMsgDungeonStageSettlement', 110, 100,
            [55, True, 'self', 10, {}],
        )
        self.send(
            'OnMsgUpdateStageCombatStatistics', 111, 100,
            packet(success=True)['decoded_arguments'],
        )
        self.assertEqual(encounter.result, 'VICTORY')
        self.assertEqual(encounter.settlement_status, 'SETTLED')

    def test_three_consecutive_bosses_keep_independent_settlements(self):
        """FB5B416D94E34350F9: Bosses 4/5/6 must never merge."""

        def statistics(stage_id, stage_index, battle, damage, seconds, token):
            raw = packet(
                battle=battle,
                received=200,
                success=True,
                damage=damage,
                seconds=seconds,
            )
            stage = raw['decoded_arguments'][0]
            stage[0] = stage_id
            stage[1] = stage_index
            for member in stage[5].values():
                member[36] = {token: 20}
                member[37] = {token: 1}
            return raw['decoded_arguments']

        self.context.update(stage_id=54, stage_index=0)
        self.context['bosses'] = {
            '300': {
                'token': 'descendant-token',
                'template_id': 7100215,
                'name': 'Descendant Guardian',
            },
        }
        self.send('OnMsgSyncFightMode', 100, 300, [2])
        first_id = self.controller.tracker.current_id

        # The previous STAGE table is delivered immediately before the next
        # Challenge. It must settle Boss 4 before Boss 5 is created.
        self.send(
            'OnMsgUpdateStageCombatStatistics', 200, 100,
            statistics(54, 0, 'boss-4-battle', 111, 31, 'descendant-token'),
        )
        self.context['bosses']['301'] = {
            'token': 'anxia-token',
            'template_id': 7100208,
            'name': 'Anxia',
        }
        self.send('OnMsgSyncFightMode', 200, 301, [2])
        second_id = self.controller.tracker.current_id

        # These are the verified simultaneous pair and its successor phase.
        # All three identities belong to one Boss 5 Encounter.
        self.context.update(stage_id=55, stage_index=1)
        self.context['bosses']['302'] = {
            'token': 'barney-token',
            'template_id': 7100209,
            'name': 'Mr Barney',
        }
        self.send('OnMsgSyncFightMode', 205, 302, [2])
        self.context['bosses']['303'] = {
            'token': 'anxia-final-token',
            'template_id': 7100210,
            'name': 'Anxia final',
        }
        self.send('OnMsgSyncFightMode', 220, 303, [2])
        self.assertEqual(self.controller.tracker.current_id, second_id)

        self.send(
            'OnMsgUpdateStageCombatStatistics', 300, 100,
            statistics(55, 1, 'boss-5-battle', 222, 42, 'anxia-final-token'),
        )
        self.context['bosses']['304'] = {
            'token': 'viscountess-token',
            'template_id': 7102990,
            'name': 'Viscountess',
        }
        self.send('OnMsgSyncFightMode', 300, 304, [2])
        third_id = self.controller.tracker.current_id

        # The Viscountess form change stays in Boss 6 and the final settlement
        # is allowed to reference the latest phase token.
        self.context.update(stage_id=56, stage_index=2)
        self.context['bosses']['305'] = {
            'token': 'viscountess-myth-token',
            'template_id': 7102991,
            'name': 'Viscountess myth form',
        }
        self.send('OnMsgSyncFightMode', 320, 305, [2])
        self.assertEqual(self.controller.tracker.current_id, third_id)
        self.send(
            'OnMsgDungeonStageSettlement', 350, 100,
            [56, True, 'self', 10, {36: {'viscountess-myth-token': 1}}],
        )
        self.send(
            'OnMsgUpdateStageCombatStatistics', 351, 100,
            statistics(
                56, 2, 'boss-6-battle', 333, 53,
                'viscountess-myth-token',
            ),
        )

        self.assertIsNone(self.controller.tracker.current_id)
        encounters = [
            self.controller.tracker.encounters[encounter_id]
            for encounter_id in (first_id, second_id, third_id)
        ]
        self.assertEqual(len(self.controller.tracker.encounters), 3)
        self.assertEqual(
            [encounter.server_battle_id for encounter in encounters],
            ['boss-4-battle', 'boss-5-battle', 'boss-6-battle'],
        )
        self.assertEqual(
            [encounter.boss_template_id for encounter in encounters],
            [7100215, 7100210, 7102991],
        )
        self.assertEqual(
            [[row['damage'] for row in encounter.participants]
             for encounter in encounters],
            [[111, 111], [222, 222], [333, 333]],
        )
        # The first two server member segments are much shorter than their
        # observed Boss intervals, so they cannot replace those local clocks.
        self.assertEqual(
            [encounter.encounter_duration_seconds for encounter in encounters],
            [100.0, 100.0, 53.0],
        )
        self.assertTrue(
            all(encounter.settlement_status == 'SETTLED'
                for encounter in encounters)
        )
        self.assertFalse(self.controller.tracker.orphans)

    def test_different_boss_start_waits_for_late_roster_after_split(self):
        self.send('OnMsgSyncFightMode', 100, 300, [2])
        first_id=self.controller.tracker.current_id
        self.context['bosses']['301']={
            'token':'next-boss-token',
            'template_id':7100215,
            'name':'Next Boss',
        }
        unavailable=deepcopy(self.context)
        unavailable['roster']=[]
        unavailable['self_token']=None
        self.controller.process({
            'record':{
                'method':'OnMsgSyncFightMode',
                'capture_timestamp_ns':200*NS,
                'network_entity_id':301,
                'decoded_arguments':[2],
            },
            'context':unavailable,
            'timestamp_ns':200*NS,
            'updates':[],
        })

        self.assertIsNone(self.controller.tracker.current_id)
        self.assertEqual(
            self.controller.tracker.encounters[first_id].result,'VICTORY'
        )
        self.assertIn('301',self.controller.pending_boss_starts)

        self.send('NpcapBossConfirmed',205,301,[])

        current=self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.assertEqual(current.started_at_ns,200*NS)
        self.assertEqual(current.boss_template_id,7100215)
        self.assertNotEqual(current.local_encounter_id,first_id)

    def test_simultaneous_boss_death_waits_for_verified_successor(self):
        self.context['bosses'] = {
            '300': {
                'token': 'anxia-token',
                'template_id': 7100208,
                'name': 'Anxia',
            },
            '301': {
                'token': 'barney-token',
                'template_id': 7100209,
                'name': 'Mr Barney',
            },
        }
        self.send('OnMsgSyncFightMode', 100, 300, [2])
        encounter_id = self.controller.tracker.current_id
        self.send('OnMsgSyncFightMode', 101, 301, [2])

        self.send('OnMsgEntityDead', 110, 300, [0, 'anxia-token'])
        self.send('OnMsgEntityDead', 111, 301, [0, 'barney-token'])
        self.assertEqual(self.controller.tracker.current_id, encounter_id)

        self.context['bosses']['302'] = {
            'token': 'anxia-final-token',
            'template_id': 7100210,
            'name': 'Anxia final',
        }
        self.send('OnMsgSyncFightMode', 115, 302, [2])
        self.assertEqual(self.controller.tracker.current_id, encounter_id)

        self.send('OnMsgEntityDead', 120, 302, [0, 'anxia-final-token'])
        encounter = self.controller.tracker.encounters[encounter_id]
        self.assertIsNone(self.controller.tracker.current_id)
        self.assertEqual(encounter.result, 'VICTORY')
        self.assertEqual(encounter.boss_template_id, 7100210)
        self.assertEqual(encounter.boss_token, 'anxia-final-token')

    def test_explicit_success_boundary_does_not_end_another_stage(self):
        self.send('OnMsgSyncFightMode',100,300,[2])

        self.send(
            'OnMsgDungeonStageSettlement', 110, 100,
            [999, True, 'self', 10, {}],
        )

        self.assertIsNotNone(self.controller.tracker.current_id)

    def test_restart_abandons_unbounded_live_and_allows_new_pull(self):
        repository = EncounterRepository(self.directory.name)
        tracker = repository.load()
        stale = tracker.begin(
            instance_id='instance', started_at_ns=90 * NS,
            participants_snapshot=ROSTER, dungeon_id=10, map_id=20,
            stage_id=55, stage_index=1, boss_template_id=7102990,
            boss_token='boss', self_token='self',
        )
        repository.save(tracker)

        controller = SettlementUIController(repository)

        self.assertIsNone(controller.tracker.current_id)
        restored = controller.tracker.encounters[stale.local_encounter_id]
        self.assertEqual(restored.result, 'RESET')
        self.assertEqual(restored.settlement_status, 'ABANDONED')
        self.assertFalse(restored.capture_complete)
        self.assertIsNone(restored.ended_at_ns)
        controller.process({
            'record': {
                'method': 'OnMsgSyncFightMode',
                'capture_timestamp_ns': 100 * NS,
                'network_entity_id': 300,
                'decoded_arguments': [2],
            },
            'context': self.context,
            'timestamp_ns': 100 * NS,
            'updates': [],
        })
        self.assertIsNotNone(controller.tracker.current_id)
        self.assertNotEqual(controller.tracker.current_id, stale.local_encounter_id)

    def test_exact_stage_roster_is_complete_after_mid_instance_start(self):
        parser = SimpleNamespace(
            self_id=100,
            self_token='self',
            party_tokens={'peer'},
            authoritative_party_tokens={'self', 'peer'},
            explicit_party_roster_seen=False,
            token_actors={'self': 100, 'peer': 200},
            entity_profiles={
                100: {'name': 'Self'},
                200: {'name': 'Peer'},
                300: {'name': 'Boss'},
            },
            confirmed_boss_entities={300},
            wire_entity_tokens={300: 'entity:300'},
            entity_template_ids={300: 7102990},
            wire_instance_id='npcap-bootstrap:300',
            wire_map_id=None,
            dungeon_id=None,
            dungeon_stage_id=None,
            dungeon_stage_phase=None,
        )
        record = {
            'method': 'OnMsgSyncFightMode',
            'capture_timestamp_ns': 100 * NS,
            'network_entity_id': 300,
            'decoded_arguments': [2],
        }
        observation = parser_observation(parser, record, [])
        self.assertTrue(observation['context']['roster_complete'])

    def test_training_dummy_is_not_exposed_as_a_settlement_boss(self):
        parser = SimpleNamespace(
            self_id=100,
            self_token='self',
            party_tokens=set(),
            authoritative_party_tokens={'self'},
            explicit_party_roster_seen=True,
            token_actors={'self': 100},
            entity_profiles={
                100: {'name': 'Self'},
                300: {'name': '伤害木桩'},
            },
            confirmed_boss_entities={300},
            training_dummy_entities={300},
            wire_entity_tokens={300: 'dummy-token'},
            entity_template_ids={300: 7114223},
            wire_instance_id='npcap-bootstrap:300',
            wire_map_id=None,
            dungeon_id=None,
            dungeon_stage_id=None,
            dungeon_stage_phase=None,
        )
        record = {
            'method': 'OnMsgSyncFightMode',
            'capture_timestamp_ns': 100 * NS,
            'network_entity_id': 300,
            'decoded_arguments': [2],
        }

        observation = parser_observation(parser, record, [])

        self.assertEqual(observation['context']['bosses'], {})

    def test_start_before_boss_confirmation_is_replayed_at_original_time(self):
        unknown_context = deepcopy(self.context)
        unknown_context['instance_id'] = None
        unknown_context['bosses'] = {}
        start = {
            'record': {
                'method': 'OnMsgSyncFightMode',
                'capture_timestamp_ns': 100 * NS,
                'network_entity_id': 300,
                'decoded_arguments': [2],
            },
            'context': unknown_context,
            'timestamp_ns': 100 * NS,
            'updates': [],
        }

        self.controller.process(start)
        self.assertIsNone(self.controller.tracker.current_id)
        self.assertIn('300', self.controller.pending_boss_starts)

        confirmed_context = deepcopy(self.context)
        confirmed_context['instance_id'] = 'npcap-bootstrap:300'
        self.controller.process({
            'record': {
                'method': 'NpcapBossConfirmed',
                'capture_timestamp_ns': 101 * NS,
                'network_entity_id': 300,
                'decoded_arguments': [],
                'npcap_synthetic': True,
            },
            'context': confirmed_context,
            'timestamp_ns': 101 * NS,
            'updates': [],
        })

        current = self.controller.tracker.encounters[
            self.controller.tracker.current_id
        ]
        self.assertEqual(current.started_at_ns, 100 * NS)
        self.assertEqual(current.instance_id, 'npcap-bootstrap:300')
        self.assertEqual(current.boss_token, 'boss')
        self.assertNotIn('300', self.controller.pending_boss_starts)

    def test_pending_start_waits_for_late_identity_then_settles_on_next_pull(self):
        unknown_context = deepcopy(self.context)
        unknown_context.update({
            'instance_id': None,
            'self_token': None,
            'roster': [],
            'roster_complete': False,
            'bosses': {},
        })
        self.controller.process({
            'record': {
                'method': 'OnMsgSyncFightMode',
                'capture_timestamp_ns': 100 * NS,
                'network_entity_id': 300,
                'decoded_arguments': [2],
            },
            'context': unknown_context,
            'timestamp_ns': 100 * NS,
            'updates': [],
        })

        confirmed_without_identity = deepcopy(unknown_context)
        confirmed_without_identity.update({
            'instance_id': 'instance',
            'bosses': deepcopy(self.context['bosses']),
        })
        self.controller.process({
            'record': {
                'method': 'NpcapBossConfirmed',
                'capture_timestamp_ns': 101 * NS,
                'network_entity_id': 300,
                'decoded_arguments': [],
            },
            'context': confirmed_without_identity,
            'timestamp_ns': 101 * NS,
            'updates': [],
        })
        self.assertIsNone(self.controller.tracker.current_id)
        self.assertIn('300', self.controller.pending_boss_starts)

        # The first pull ended while the app was still learning its own token.
        self.controller.process({
            'record': {
                'method': 'OnMsgSyncFightMode',
                'capture_timestamp_ns': 110 * NS,
                'network_entity_id': 300,
                'decoded_arguments': [0],
            },
            'context': confirmed_without_identity,
            'timestamp_ns': 110 * NS,
            'updates': [],
        })

        # At the next pull the local-role statistics packet supplies a complete
        # context before the Boss [2] edge. It must recreate and settle pull 1.
        self.send(
            'OnMsgUpdateStageCombatStatistics', 200, 100,
            packet()['decoded_arguments'],
        )
        old_id = self.controller.tracker.current_id
        old = self.controller.tracker.encounters[old_id]
        self.assertEqual(old.started_at_ns, 100 * NS)
        self.assertEqual(self.controller.boss_in_combat['300'], False)

        self.send('OnMsgSyncFightMode', 200, 300, [2])

        self.assertEqual(old.result, 'WIPE')
        self.assertEqual(old.settlement_status, 'SETTLED')
        self.assertFalse(self.controller.tracker.orphans)
        self.assertNotEqual(self.controller.tracker.current_id, old_id)

    def test_replayed_start_allows_next_pull_table_to_settle_previous_wipe(self):
        unknown_context = deepcopy(self.context)
        unknown_context['bosses'] = {}
        self.controller.process({
            'record': {
                'method': 'OnMsgSyncFightMode',
                'capture_timestamp_ns': 100 * NS,
                'network_entity_id': 300,
                'decoded_arguments': [2],
            },
            'context': unknown_context,
            'timestamp_ns': 100 * NS,
            'updates': [],
        })
        self.send('NpcapBossConfirmed', 101, 300, [])
        old_id = self.controller.tracker.current_id
        self.send('OnMsgEntityDead', 110, 100, [0, 'boss'])
        self.send('OnMsgSyncFightMode', 110, 300, [0])
        self.send('OnMsgEntityDead', 110, 200, [0, 'boss'])
        self.assertIsNone(self.controller.tracker.current_id)

        self.send('OnMsgSyncFightMode', 200, 300, [2])
        self.send(
            'OnMsgUpdateStageCombatStatistics', 200, 100,
            packet()['decoded_arguments'],
        )

        old = self.controller.tracker.encounters[old_id]
        self.assertEqual(old.settlement_status, 'SETTLED')
        self.assertFalse(self.controller.tracker.orphans)

    def test_unscoped_table_immediately_before_same_batch_start_settles_wipe(self):
        self.wipe()
        old = next(iter(self.controller.tracker.encounters.values()))
        unscoped = deepcopy(self.context)
        unscoped['instance_id'] = None

        record = {
            'method': 'OnMsgUpdateStageCombatStatistics',
            'capture_timestamp_ns': 200 * NS,
            'network_entity_id': 100,
            'decoded_arguments': packet()['decoded_arguments'],
        }
        self.controller.process({
            'record': record,
            'context': unscoped,
            'timestamp_ns': 200 * NS,
            'updates': [],
        })
        self.assertEqual(len(self.controller.tracker.orphans), 1)

        self.send('OnMsgSyncFightMode', 200, 300, [2])

        self.assertEqual(old.settlement_status, 'SETTLED')
        self.assertEqual(old.server_battle_id, 'battle-1')
        self.assertEqual([row['damage'] for row in old.participants], [100, 100])
        self.assertFalse(self.controller.tracker.orphans)

    def test_restart_recovers_legacy_abandoned_wipe_and_unscoped_orphan(self):
        repository = EncounterRepository(self.directory.name)
        tracker = repository.load()
        old = tracker.begin(
            instance_id='instance', started_at_ns=100 * NS,
            participants_snapshot=ROSTER, dungeon_id=10, map_id=20,
            stage_id=55, stage_index=1, boss_template_id=7102990,
            boss_token='boss', self_token='self',
        )
        for row in ROSTER:
            tracker.life(row['id'], 110 * NS, True)
        tracker.end('WIPE', 110 * NS)
        old.settlement_status = 'ABANDONED'
        successor = tracker.begin(
            instance_id='instance', started_at_ns=200 * NS,
            participants_snapshot=ROSTER, dungeon_id=10, map_id=20,
            stage_id=55, stage_index=1, boss_template_id=7102990,
            boss_token='boss', self_token='self',
        )
        tracker.end('RESET', 210 * NS)
        snapshot = normalize_statistics(packet(received=200))[0]
        tracker.orphans[snapshot.fingerprint] = {
            'statistics': snapshot.to_dict(),
            'reason': 'missing_scoped_battle_identity',
        }
        repository.save(tracker)

        restored = SettlementUIController(repository)
        result = restored.tracker.encounters[old.local_encounter_id]

        self.assertEqual(result.settlement_status, 'SETTLED')
        self.assertEqual(result.server_battle_id, 'battle-1')
        self.assertEqual([row['damage'] for row in result.participants], [100, 100])
        self.assertFalse(restored.tracker.orphans)
        self.assertEqual(
            restored.tracker.encounters[successor.local_encounter_id].result,
            'RESET',
        )

    def test_capture_session_reset_abandons_live_and_clears_pending_state(self):
        self.send('OnMsgSyncFightMode', 100, 300, [2])
        current_id = self.controller.tracker.current_id
        self.controller.pending_boss_starts['999'] = {
            'timestamp_ns': 100 * NS,
            'context': deepcopy(self.context),
        }

        self.assertTrue(self.controller.reset_capture_session(105 * NS))

        encounter = self.controller.tracker.encounters[current_id]
        self.assertEqual(encounter.result, 'RESET')
        self.assertEqual(encounter.settlement_status, 'ABANDONED')
        self.assertFalse(encounter.capture_complete)
        self.assertIsNone(self.controller.tracker.current_id)
        self.assertFalse(self.controller.pending_boss_starts)
        self.assertFalse(self.controller.boss_entities)

    def test_restart_retries_persisted_server_table(self):
        repository = EncounterRepository(self.directory.name)
        tracker = repository.load()
        encounter = tracker.begin(
            instance_id='instance', started_at_ns=100 * NS,
            participants_snapshot=ROSTER, dungeon_id=10, map_id=20,
            stage_id=54, stage_index=0, boss_template_id=7102990,
            boss_token='boss', self_token='self',
        )
        tracker.end('VICTORY', 137 * NS)
        raw = packet(received=200, success=True, seconds=37)
        snapshot = normalize_statistics(
            raw, instance_id='instance', dungeon_id=10, map_id=21
        )[0]
        tracker.orphans[snapshot.fingerprint] = {
            'statistics': snapshot.to_dict(),
            'reason': 'previous_matcher_rejected',
        }
        repository.save(tracker)

        restored = SettlementUIController(repository)
        restored_encounter = restored.tracker.encounters[
            encounter.local_encounter_id
        ]

        self.assertEqual(restored_encounter.settlement_status, 'SETTLED')
        self.assertEqual(restored_encounter.stage_id, 55)
        self.assertFalse(restored.tracker.orphans)

    def test_restart_reapplies_healer_zero_from_persisted_pending_table(self):
        repository = EncounterRepository(self.directory.name)
        tracker = repository.load()
        encounter = tracker.begin(
            instance_id='instance', started_at_ns=100 * NS,
            participants_snapshot=ROSTER, dungeon_id=10, map_id=20,
            stage_id=55, stage_index=1, boss_template_id=7102990,
            boss_token='boss', self_token='self',
        )
        tracker.end('VICTORY', 137 * NS)
        snapshot = normalize_statistics(
            packet(received=200, success=True), instance_id='instance'
        )[0]
        persisted = snapshot.to_dict()
        healer = next(
            member for member in persisted['members'] if member['id'] == 'peer'
        )
        healer['damage'] = None
        healer['skill_damage'] = {}
        encounter.server_battle_id = snapshot.battle_id
        encounter.stage_statistics = persisted
        encounter.snapshot_fingerprints['STAGE'] = 'legacy-pre-healer-zero'
        encounter.settlement_status = 'PENDING'
        encounter.participants[1]['damage'] = None
        repository.save(tracker)

        restored = SettlementUIController(repository)
        restored_encounter = restored.tracker.encounters[
            encounter.local_encounter_id
        ]
        by_id = {row['id']: row for row in restored_encounter.participants}
        self.assertEqual(restored_encounter.settlement_status, 'SETTLED')
        self.assertEqual(by_id['peer']['damage'], 0)

    def test_morning_three_boss_sequence_stays_stable_under_repetition(self):
        class MemoryRepository:
            def __init__(self):
                self.tracker = EncounterTracker()

            def load(self):
                return self.tracker

            def save(self, tracker):
                self.tracker = tracker

        tokens = [f'morning-{index}' for index in range(15)]
        boss_specs = {
            1: {
                'stage_id': 5_150_061,
                'stage_index': 1,
                'total_damage': 48_411_085,
                'seconds': 218,
                'roster_tokens': tokens[:12],
                'stat_tokens': tokens[:12],
                'entities': (
                    (101, 'arsTccI5xUX_M1Yr', 7_100_215, 'Morning Boss 1'),
                ),
            },
            2: {
                'stage_id': 5_150_062,
                'stage_index': 2,
                'total_damage': 107_780_218,
                'seconds': 448,
                'roster_tokens': [tokens[0], *tokens[2:12]],
                'stat_tokens': [tokens[0], *tokens[2:13]],
                'entities': (
                    (201, 'arseXMI5xUX_M2RT', 7_100_208, 'Morning Boss 2A'),
                    (202, 'arseXMI5xUX_M2RV', 7_100_209, 'Morning Boss 2B'),
                    (203, 'arsfXcI5xUX_M2XP', 7_100_210,
                     'Morning Boss 2 Final'),
                ),
            },
            3: {
                'stage_id': 5_150_063,
                'stage_index': 3,
                'total_damage': 13_678_312,
                'seconds': 93,
                'roster_tokens': [tokens[0], *tokens[3:12]],
                'stat_tokens': [tokens[0], *tokens[3:13]],
                'entities': (
                    (301, 'arshuHOIayPdFx6K', 7_102_990, 'Morning Boss 3'),
                ),
                'extra_target_tokens': ('arsiLXOIayPdFx9N',),
            },
        }
        orders = (
            (1, 2, 3),
            (1, 3, 2),
            (2, 1, 3),
            (2, 3, 1),
            (3, 1, 2),
            (3, 2, 1),
        )
        expected_templates = {
            1: 7_100_215,
            2: 7_100_210,
            3: 7_102_990,
        }

        def identity(token):
            index = int(token.rsplit('-', 1)[1])
            return {
                'id': token,
                'iid': 10_000 + index,
                'name': f'Morning Player {index + 1}',
                'profession_id': 1_200_001 + index % 7,
            }

        def member_table(spec):
            stat_tokens = spec['stat_tokens']
            base, remainder = divmod(
                spec['total_damage'], len(stat_tokens)
            )
            target_tokens = (
                tuple(entity[1] for entity in spec['entities'])
                + tuple(spec.get('extra_target_tokens', ()))
            )
            rows = {}
            for position, token in enumerate(stat_tokens):
                player = identity(token)
                damage = base + (1 if position < remainder else 0)
                rows[token] = {
                    0: token,
                    1: player['iid'],
                    4: player['profession_id'],
                    5: player['name'],
                    6: damage,
                    19: spec['seconds'] - position % 3,
                    33: {86_000_010: 1},
                    34: {86_000_010: damage},
                    36: {target: damage for target in target_tokens},
                    37: {target: 1 for target in target_tokens},
                }
            return rows

        iterations = int(os.environ.get(
            'GMZZ_MORNING_STRESS_ITERATIONS', '6'
        ))
        self.assertGreater(iterations, 0)
        started = time.perf_counter()
        order_counts = {order: 0 for order in orders}
        max_encounters = 0
        max_orphans_seen = 0

        for iteration in range(iterations):
            order = orders[iteration % len(orders)]
            order_counts[order] += 1
            repository = MemoryRepository()
            controller = SettlementUIController(repository)
            context = {
                'instance_id': 'morning-three-boss-instance',
                'dungeon_id': 5_150_060,
                'map_id': 5_200_060,
                'stage_id': None,
                'stage_index': None,
                'self_token': tokens[0],
                'roster': [],
                'roster_complete': True,
                'party_session_id': 29,
                'in_team': True,
                'bosses': {
                    str(entity_id): {
                        'token': boss_token,
                        'template_id': template_id,
                        'name': name,
                    }
                    for spec in boss_specs.values()
                    for entity_id, boss_token, template_id, name
                    in spec['entities']
                },
            }

            def send(method, stamp_ns, entity, args):
                controller.process({
                    'record': {
                        'method': method,
                        'capture_timestamp_ns': stamp_ns,
                        'network_entity_id': entity,
                        'decoded_arguments': args,
                    },
                    'context': context,
                    'timestamp_ns': stamp_ns,
                    'updates': [],
                })

            def select_boss(boss_number):
                spec = boss_specs[boss_number]
                context.update({
                    'stage_id': spec['stage_id'],
                    'stage_index': spec['stage_index'],
                    'roster': [
                        identity(token) for token in spec['roster_tokens']
                    ],
                })
                return spec

            def start_boss(boss_number, stamp_ns):
                spec = select_boss(boss_number)
                for offset, entity in enumerate(spec['entities']):
                    send(
                        'OnMsgSyncFightMode',
                        stamp_ns + offset * 10_000,
                        entity[0],
                        [2],
                    )
                return spec['entities'][-1][0]

            def stage_payload(boss_number, battle_id, success):
                spec = boss_specs[boss_number]
                stage = {
                    0: spec['stage_id'],
                    1: spec['stage_index'],
                    2: battle_id,
                    5: member_table(spec),
                }
                if success is not None:
                    stage[3] = success
                return stage

            first_boss = order[0]
            first_last_entity = start_boss(first_boss, 100 * NS)
            wipe_at = 150 * NS
            send(
                'OnMsgUpdateStageCombatStatistics',
                wipe_at,
                10_000,
                [stage_payload(
                    first_boss,
                    f'morning-{iteration}-{first_boss}-wipe',
                    None,
                )],
            )
            max_orphans_seen = max(
                max_orphans_seen, len(controller.tracker.orphans)
            )
            send(
                'OnMsgSyncFightMode',
                wipe_at + 1,
                first_last_entity,
                [0],
            )
            current_start = wipe_at + 29_600
            start_boss(first_boss, current_start)

            for position, boss_number in enumerate(order):
                if position:
                    start_boss(boss_number, current_start)
                result_at = current_start + 50 * NS
                stage = stage_payload(
                    boss_number,
                    f'morning-{iteration}-{boss_number}-victory',
                    True,
                )
                if position == len(order) - 1:
                    spec = boss_specs[boss_number]
                    final_entity = spec['entities'][-1]
                    controller.observe_boss_health({
                        'entity_id': final_entity[0],
                        'boss_token': final_entity[1],
                        'template_id': final_entity[2],
                        'current_hp': 285_738,
                        'max_hp': 59_392_271,
                        'capture_timestamp_ns': result_at - 1,
                    })
                    send(
                        'OnMsgSettlementCombatStatistics',
                        result_at,
                        10_000,
                        [{
                            0: deepcopy(stage[5]),
                            spec['stage_id']: deepcopy(stage[5]),
                        }, stage],
                    )
                else:
                    send(
                        'OnMsgUpdateStageCombatStatistics',
                        result_at,
                        10_000,
                        [stage],
                    )
                    max_orphans_seen = max(
                        max_orphans_seen,
                        len(controller.tracker.orphans),
                    )
                    current_start = result_at + 29_600

            encounters = sorted(
                controller.tracker.encounters.values(),
                key=lambda encounter: encounter.started_at_ns,
            )
            expected_bosses = [order[0], *order]
            max_encounters = max(max_encounters, len(encounters))
            failure_context = (iteration, order)
            self.assertEqual(len(encounters), 4, failure_context)
            self.assertEqual(
                [encounter.stage_id for encounter in encounters],
                [
                    boss_specs[boss]['stage_id']
                    for boss in expected_bosses
                ],
                failure_context,
            )
            self.assertEqual(
                [encounter.boss_template_id for encounter in encounters],
                [expected_templates[boss] for boss in expected_bosses],
                failure_context,
            )
            self.assertEqual(
                [encounter.result for encounter in encounters],
                ['WIPE', 'VICTORY', 'VICTORY', 'VICTORY'],
                failure_context,
            )
            self.assertTrue(
                all(
                    encounter.settlement_status == 'SETTLED'
                    for encounter in encounters
                ),
                failure_context,
            )
            self.assertEqual(
                [len(encounter.participants) for encounter in encounters],
                [
                    len(boss_specs[boss]['stat_tokens'])
                    for boss in expected_bosses
                ],
                failure_context,
            )
            self.assertEqual(
                [
                    sum(row['damage'] for row in encounter.participants)
                    for encounter in encounters
                ],
                [
                    boss_specs[boss]['total_damage']
                    for boss in expected_bosses
                ],
                failure_context,
            )
            self.assertTrue(
                all(
                    row['dps'] is not None
                    for encounter in encounters
                    for row in encounter_view(encounter)['members']
                ),
                failure_context,
            )
            self.assertIsNone(controller.tracker.current_id, failure_context)
            self.assertFalse(controller.tracker.orphans, failure_context)
            self.assertEqual(
                encounters[-1].boss_current_hp, 0, failure_context
            )
            visible = controller.visible()
            self.assertIsNotNone(visible, failure_context)
            self.assertEqual(
                visible['local_encounter_id'],
                encounters[-1].local_encounter_id,
                failure_context,
            )

        if iterations > len(orders):
            distribution = ','.join(
                f'{"".join(map(str, order))}:{order_counts[order]}'
                for order in orders
            )
            print(
                'morning-three-boss-stress '
                f'iterations={iterations} failures=0 '
                f'orders={distribution} '
                f'max_encounters={max_encounters} '
                f'max_orphans_seen={max_orphans_seen} '
                f'elapsed_seconds={time.perf_counter() - started:.3f}'
            )
if __name__=='__main__':unittest.main()
