"""Offline v0.3.0 contracts. No game process or socket is opened."""
import base64
import json
from copy import deepcopy
from dataclasses import replace
import unittest

from combat_statistics import normalize_statistics, NormalizedCombatStatistics
from encounter_tracker import (
    EncounterDurationResolver,
    EncounterTracker,
    NEXT_CHALLENGE_BOUNDARY_WINDOW_NS,
)

NS = 1_000_000_000
ROSTER = [{'id':'self','iid':100,'name':'Self'}, {'id':'peer','iid':200,'name':'Peer'}]


def role_token(prefix, index):
    return base64.urlsafe_b64encode(
        bytes([prefix, index]) + bytes(10)
    ).decode('ascii')


def projection_token(index):
    return role_token(0x6A, index + 1)


def projection_members(boss_token='boss'):
    rows = {
        row['id']: {
            0: row['id'], 1: row['iid'], 5: row['name'], 6: 100,
            19: 10, 33: {123: 1}, 34: {123: 99},
            36: {boss_token: 20}, 37: {boss_token: 1},
        }
        for row in ROSTER
    }
    for index in range(4):
        token = projection_token(index)
        rows[token] = {
            0: token, 1: 300 + index, 4: 1_200_001 + index,
            5: f'投影{index + 1}·投影', 6: 50 + index, 19: 10,
            33: {123: 1}, 34: {123: 49 + index},
            36: {boss_token: 10}, 37: {boss_token: 1},
        }
    return rows


def packet(battle='battle-1', received=200, success=None, damage=100, seconds=10, members=None):
    data = members if members is not None else {
        r['id']:{0:r['id'],1:r['iid'],5:r['name'],6:damage,19:seconds,
                 33:{123:1},34:{123:damage-1},36:{'boss':20},37:{'boss':1}} for r in ROSTER}
    stage = {0:55,1:1,2:battle,5:data}
    if success is not None:
        stage[3] = success
    return {'method':'OnMsgUpdateStageCombatStatistics','capture_timestamp_ns':received*NS,
            'decoded_arguments':[stage]}


def normalized(**kwargs):
    return normalize_statistics(packet(**kwargs),instance_id='instance',dungeon_id=10,map_id=20)[0]


def begin(tracker, start=100, roster=None):
    return tracker.begin(instance_id='instance',started_at_ns=start*NS,
        participants_snapshot=roster or ROSTER,stage_id=55,stage_index=1,boss_token='boss',
        dungeon_id=10,map_id=20,self_token='self')


def wipe(tracker, start=100, duration=10, roster=None):
    e=begin(tracker,start,roster)
    for r in e.participants_snapshot:tracker.life(r['id'],(start+duration)*NS,True)
    tracker.end('WIPE',(start+duration)*NS)
    return e


class NormalizedStatisticsTests(unittest.TestCase):
    def test_absent_is_not_zero(self):
        raw=packet();raw['decoded_arguments'][0][5]['peer'].pop(6)
        raw['decoded_arguments'][0][5]['self'][6]=0
        r=normalize_statistics(raw)[0];by={m.id:m for m in r.members}
        self.assertIsNone(by['peer'].damage)
        self.assertEqual(by['self'].damage,0)
        self.assertIsNone(by['self'].heal)

    def test_healer_without_damage_and_empty_skill_table_is_zero(self):
        raw=packet()
        raw['decoded_arguments'][0][5]['peer'].pop(6)
        raw['decoded_arguments'][0][5]['peer'][34]={}
        result=normalize_statistics(raw)[0]
        by_id={member.id:member for member in result.members}
        self.assertEqual(by_id['peer'].damage,0)

        legacy=result.to_dict()
        peer=next(member for member in legacy['members'] if member['id']=='peer')
        peer['damage']=None
        restored=NormalizedCombatStatistics.from_dict(legacy)
        restored_by_id={member.id:member for member in restored.members}
        self.assertEqual(restored_by_id['peer'].damage,0)

    def test_no_nan_boolean_negative_damage(self):
        for invalid in (True,-1,1.5,float('nan'),float('inf'),'100'):
            raw = packet()
            raw['decoded_arguments'][0][5]['peer'][6] = invalid
            with self.subTest(value=invalid),self.assertRaises(ValueError):
                normalize_statistics(raw)

    def test_id_mismatch_not_name_matching(self):
        raw=packet();raw['decoded_arguments'][0][5]['peer'][0]='self'
        with self.assertRaises(ValueError):normalize_statistics(raw)

    def test_stage_all_separate_and_no_scaling(self):
        stage=packet()['decoded_arguments'][0]
        aggregate={k:dict(v, **{'6':v[6]*3}) for k,v in stage[5].items()}
        for row in aggregate.values():del row[6]
        raw={'method':'OnMsgSettlementCombatStatistics','capture_timestamp_ns':200*NS,
             'decoded_arguments':[{0:aggregate,55:stage[5]},stage]}
        result=normalize_statistics(raw,instance_id='instance')
        self.assertEqual([s.scope for s in result],['STAGE','ALL'])
        self.assertEqual(result[0].members[0].damage,100)
        self.assertEqual(result[1].members[0].damage,300)
        self.assertIsNone(result[1].battle_id)

    def test_settlement_skips_empty_auxiliary_stage(self):
        stage=packet(battle='current-battle',success=True)['decoded_arguments'][0]
        stage[0]=5_150_005
        raw={'method':'OnMsgSettlementCombatStatistics','capture_timestamp_ns':200*NS,
             'decoded_arguments':[{0:stage[5],5_150_005:stage[5],5_150_002:{}},stage]}

        result=normalize_statistics(raw,instance_id='instance')

        self.assertEqual([s.scope for s in result],['STAGE','ALL'])
        self.assertEqual(result[0].battle_id,'current-battle')
        self.assertEqual(result[0].stage_id,5_150_005)
        self.assertEqual({m.id:m.damage for m in result[0].members},{'peer':100,'self':100})
        self.assertEqual({m.id:m.damage for m in result[1].members},{'peer':100,'self':100})

    def test_settlement_still_rejects_empty_current_stage(self):
        stage=packet(success=True)['decoded_arguments'][0]
        stage[5]={}
        raw={'method':'OnMsgSettlementCombatStatistics','capture_timestamp_ns':200*NS,
             'decoded_arguments':[{5_150_002:{}},stage]}

        with self.assertRaisesRegex(ValueError,'Empty or oversized server roster'):
            normalize_statistics(raw)

    def test_damage_gap_not_proportionally_allocated(self):
        m=normalized().members[0]
        self.assertEqual(m.unclassified_damage,1)
        self.assertEqual(m.skill_damage,{'123':99})
        self.assertEqual(m.skill_count,{'123':1})
        self.assertNotIn('timeline',m.__dict__)

    def test_twelve_members_and_json_roundtrip(self):
        members={str(i):{0:str(i),1:100+i,6:i} for i in range(12)}
        s=normalized(members=members)
        self.assertEqual(len(s.members),12)
        self.assertEqual(NormalizedCombatStatistics.from_dict(json.loads(json.dumps(s.to_dict()))),s)

    def test_server_profession_id_is_preserved(self):
        raw = packet()
        raw['decoded_arguments'][0][5]['self'][4] = 1_200_002
        member = {row.id: row for row in normalize_statistics(raw)[0].members}['self']
        self.assertEqual(member.profession_id, 1_200_002)


class EncounterSettlementTests(unittest.TestCase):
    def test_delayed_victory_excludes_member_joined_after_boss_died(self):
        tracker = EncounterTracker()
        encounter = begin(tracker)
        tracker.end('VICTORY', 110 * NS)
        members = packet(success=True)['decoded_arguments'][0][5]
        members['late-combatant'] = {
            0: 'late-combatant', 1: 300, 5: 'Combatant·投影',
            6: 50, 19: 10, 36: {'boss': 5},
        }
        members['late-joiner'] = {
            0: 'late-joiner', 1: 400, 5: 'Henry', 6: 0,
        }
        stat = normalized(received=130, success=True, members=members)

        match = tracker.accept(
            stat, preferred_encounter_id=encounter.local_encounter_id
        )

        self.assertEqual(match.reason, 'current_party_server_statistics')
        self.assertEqual(len(encounter.stage_statistics['members']), 4)
        self.assertEqual(set(encounter.roster), {'self', 'peer', 'late-combatant'})
        self.assertFalse(encounter.roster['late-combatant']['is_ai'])
        self.assertEqual({row['id'] for row in encounter.participants},
                         {'self', 'peer', 'late-combatant'})

    def test_wipe_pending_and_new_live_coexist(self):
        t=EncounterTracker();old=wipe(t);new=begin(t,200)
        self.assertEqual(old.settlement_status,'PENDING')
        self.assertIsNone(old.participants[1]['damage'])
        result=t.accept(normalized(received=200))
        self.assertEqual(result.encounter_id,old.local_encounter_id)
        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual(new.settlement_status,'LIVE')
        self.assertIsNone(new.participants[1]['damage'])

    def test_wipe_two_real_players_ignores_unbound_projection_slots(self):
        tracker = EncounterTracker()
        roster = ROSTER + [
            {'id': projection_token(index), 'iid': None, 'name': None,
             'is_ai': True}
            for index in range(4)
        ]
        previous = tracker.begin(
            instance_id='instance', started_at_ns=100 * NS,
            participants_snapshot=roster, stage_id=None,
            boss_template_id=7100401, boss_token='entity:first-boss',
            self_token='self',
        )
        tracker.end('WIPE', 190 * NS)
        successor = tracker.begin(
            instance_id='instance', started_at_ns=200 * NS,
            participants_snapshot=[ROSTER[0], *roster[2:]],
            stage_id=None, boss_template_id=7100401,
            boss_token='entity:next-pull', self_token='self',
        )
        tracker.end('RESET', 210 * NS)

        result = tracker.accept(normalized(received=200, seconds=90))

        self.assertEqual(result.encounter_id, previous.local_encounter_id)
        self.assertEqual(result.reason, 'unique_wipe_unbound_projection_next_pull')
        self.assertEqual(previous.settlement_status, 'SETTLED')
        self.assertEqual(len(previous.participants), 2)
        self.assertEqual(len(previous.participants_snapshot), 6)
        self.assertEqual(successor.settlement_status, 'ABANDONED')

    def test_two_failed_records_separate_battle_ids(self):
        t=EncounterTracker();a=wipe(t);b=wipe(t,200,13)
        t.accept(normalized(battle='one',received=200,seconds=10))
        t.accept(normalized(battle='two',received=300,seconds=13,damage=150))
        self.assertEqual(a.server_battle_id,'one');self.assertEqual(b.server_battle_id,'two')
        self.assertEqual(b.participants[0]['damage'],150)

    def test_phase_two_wipe_matches_before_phase_one_repull(self):
        tracker=EncounterTracker()
        old=tracker.begin(
            instance_id='instance',started_at_ns=100*NS,
            participants_snapshot=ROSTER,dungeon_id=10,map_id=20,
            stage_id=55,stage_index=1,boss_template_id=7102991,
            boss_token='phase-two-token',self_token='self',
        )
        old.capture_complete=False
        tracker.end('WIPE',110*NS)
        snapshot=normalized(battle='phase-wipe',received=200)
        self.assertIsNone(tracker.accept(snapshot).encounter_id)

        tracker.begin(
            instance_id='instance',started_at_ns=200*NS,
            participants_snapshot=ROSTER,dungeon_id=10,map_id=20,
            stage_id=55,stage_index=1,boss_template_id=7102990,
            boss_token='phase-one-token',self_token='self',
        )

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual(old.server_battle_id,'phase-wipe')
        self.assertFalse(tracker.orphans)

    def test_duplicate_is_not_added(self):
        t=EncounterTracker();e=wipe(t);s=normalized()
        t.accept(s);revision=e.revision
        self.assertEqual(t.accept(replace(s,received_at_ns=201*NS)).reason,'duplicate_ignored')
        self.assertEqual(e.revision,revision)
        self.assertEqual(e.participants[0]['damage'],100)

    def test_changed_snapshot_without_revision_is_not_summed(self):
        t=EncounterTracker();e=wipe(t);t.accept(normalized())
        result=t.accept(normalized(received=201,damage=110))
        self.assertEqual(result.reason,'conflicting_snapshot_without_revision')
        self.assertEqual(e.participants[0]['damage'],100)

    def test_partial_snapshot_upsert_fills_null_without_replacing_known(self):
        t=EncounterTracker();e=wipe(t);raw=packet()
        raw['decoded_arguments'][0][5]['peer'].pop(6)
        s=normalize_statistics(raw,instance_id='instance')[0]
        t.accept(s)
        self.assertEqual(e.settlement_status,'PENDING')
        t.accept(normalized(received=201))
        self.assertEqual(e.settlement_status,'SETTLED')
        self.assertEqual([p['damage'] for p in e.participants],[100,100])

    def test_roster_departure_does_not_erase_old_members(self):
        t=EncounterTracker();e=wipe(t)
        changed=[ROSTER[0],{'id':'replacement','iid':300}]
        new=begin(t,200,changed)
        t.accept(normalized())
        self.assertEqual({r['id'] for r in e.participants},{'self','peer'})
        self.assertEqual(set(new.roster),{'self','replacement'})

    def test_six_player_result_can_settle_after_next_solo_boss_starts(self):
        roster = [ROSTER[0], *(
            {'id': f'peer-{index}', 'iid': 200 + index,
             'name': f'Peer {index}'}
            for index in range(1, 6)
        )]
        tracker = EncounterTracker()
        old = begin(tracker, 100, roster)
        tracker.end('VICTORY', 110 * NS)
        next_boss = begin(tracker, 200, [roster[0]])
        members = {
            member['id']: {
                0: member['id'], 1: member['iid'], 5: member['name'],
                6: 100 * index, 19: 10,
                33: {123: 1}, 34: {123: 100 * index - 1},
                36: {'boss': 20}, 37: {'boss': 1},
            }
            for index, member in enumerate(roster, 1)
        }
        result = tracker.accept(normalized(received=201, members=members))

        self.assertEqual(result.encounter_id, old.local_encounter_id)
        self.assertEqual(old.settlement_status, 'SETTLED')
        self.assertEqual(len(old.participants), 6)
        self.assertEqual(tracker.current_id, next_boss.local_encounter_id)

    def test_death_revive_death_stays_one_pull(self):
        t=EncounterTracker();e=begin(t)
        t.life('self',105*NS,True);t.life('self',108*NS,False);t.life('self',115*NS,True)
        t.life('peer',115*NS,True);t.end('WIPE',115*NS)
        self.assertEqual(e.death_count('self'),2)
        self.assertEqual(e.observed_alive_seconds('self'),12)
        raw=packet(seconds=15);raw['decoded_arguments'][0][5]['self'][19]=12
        raw['decoded_arguments'][0][5]['self'][37]['boss']=2
        s=normalize_statistics(raw,instance_id='instance')[0]
        self.assertEqual(t.accept(s).encounter_id,e.local_encounter_id)
        self.assertEqual(len(t.encounters),1)

    def test_ambiguous_same_boss_same_duration_does_not_guess(self):
        t=EncounterTracker();a=wipe(t);b=wipe(t,120)
        self.assertEqual(t.accept(normalized()).reason,'ambiguous_candidates')
        self.assertEqual(a.settlement_status,'PENDING');self.assertEqual(b.settlement_status,'PENDING')
        self.assertEqual(len(t.orphans),1)

    def test_wrong_instance_rejected_even_when_battle_id_matches(self):
        t=EncounterTracker();e=wipe(t);t.accept(normalized())
        self.assertIsNone(t.accept(replace(normalized(),instance_id='other')).encounter_id)
        self.assertEqual(e.participants[0]['damage'],100)

    def test_exit_keeps_finished_pull_pending_for_delayed_settlement(self):
        t=EncounterTracker();e=wipe(t);t.leave_instance('instance',180*NS)
        self.assertEqual(e.settlement_status,'PENDING')
        self.assertTrue(all(r['damage'] is None and r['dps'] is None for r in e.participants))
        self.assertEqual(t.accept(normalized()).encounter_id,e.local_encounter_id)

    def test_load_restores_legacy_abandoned_finished_pull_to_pending(self):
        t=EncounterTracker();e=wipe(t)
        e.settlement_status='ABANDONED'
        for row in e.participants:row['settlement_hint']='未取得结算'

        restored=EncounterTracker.from_dict(json.loads(json.dumps(t.to_dict())))
        migrated=restored.encounters[e.local_encounter_id]

        self.assertEqual(migrated.result,'WIPE')
        self.assertEqual(migrated.settlement_status,'PENDING')
        self.assertTrue(all(row['settlement_hint']=='等待结算' for row in migrated.participants))

    def test_capture_gap_prevents_first_automatic_binding(self):
        t=EncounterTracker();e=wipe(t);e.capture_complete=False;e.automatic_match_blocked=True
        self.assertIsNone(t.accept(normalized()).encounter_id)

    def test_exact_next_pull_boundary_settles_incomplete_previous_wipe(self):
        t=EncounterTracker();old=wipe(t);old.capture_complete=False
        snapshot=normalized(received=200)

        self.assertIsNone(t.accept(snapshot).encounter_id)
        self.assertEqual(len(t.orphans),1)
        new=begin(t,200)

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual(old.server_battle_id,'battle-1')
        self.assertEqual([row['damage'] for row in old.participants],[100,100])
        self.assertFalse(t.orphans)
        self.assertEqual(t.current_id,new.local_encounter_id)
        self.assertEqual(new.settlement_status,'LIVE')

    def test_next_pull_boundary_allows_revive_iid_rebinding_for_previous_wipe(self):
        tracker=EncounterTracker();old=wipe(tracker);old.capture_complete=False
        rebound_roster=[
            {'id':'self','iid':101,'name':'Self'},
            {'id':'peer','iid':201,'name':'Peer'},
        ]
        rebound_members={
            row['id']:{
                0:row['id'],1:row['iid'],5:row['name'],6:321,19:10,
                33:{123:1},34:{123:320},36:{'boss':20},37:{'boss':1},
            }
            for row in rebound_roster
        }
        snapshot=normalized(received=200,members=rebound_members)

        self.assertIsNone(tracker.accept(snapshot).encounter_id)
        new=begin(tracker,200,rebound_roster)

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual(old.server_battle_id,'battle-1')
        self.assertEqual([row['damage'] for row in old.participants],[321,321])
        self.assertFalse(tracker.orphans)
        self.assertEqual(tracker.current_id,new.local_encounter_id)
        self.assertEqual(new.settlement_status,'LIVE')

    def test_next_pull_boundary_allows_successor_roster_replacement(self):
        tracker=EncounterTracker();old=wipe(tracker);old.capture_complete=False
        snapshot=normalized(received=200)
        changed_roster=[
            {'id':'self','iid':101,'name':'Self'},
            {'id':'replacement','iid':301,'name':'Replacement'},
        ]

        self.assertIsNone(tracker.accept(snapshot).encounter_id)
        new=begin(tracker,200,changed_roster)

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual({row['id'] for row in old.participants},{'self','peer'})
        self.assertEqual(set(new.roster),{'self','replacement'})
        self.assertFalse(tracker.orphans)

    def test_next_pull_boundary_allows_successor_roster_addition(self):
        tracker=EncounterTracker();old=wipe(tracker);old.capture_complete=False
        snapshot=normalized(received=200)
        expanded_roster=[
            *ROSTER,
            {'id':'new-member','iid':301,'name':'New Member'},
        ]

        self.assertIsNone(tracker.accept(snapshot).encounter_id)
        new=begin(tracker,200,expanded_roster)

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual({row['id'] for row in old.participants},{'self','peer'})
        self.assertEqual(set(new.roster),{'self','peer','new-member'})
        self.assertFalse(tracker.orphans)

    def test_next_pull_boundary_allows_successor_roster_reduction(self):
        tracker=EncounterTracker();old=wipe(tracker);old.capture_complete=False
        snapshot=normalized(received=200)
        reduced_roster=[ROSTER[0]]

        self.assertIsNone(tracker.accept(snapshot).encounter_id)
        new=begin(tracker,200,reduced_roster)

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual({row['id'] for row in old.participants},{'self','peer'})
        self.assertEqual(set(new.roster),{'self'})
        self.assertFalse(tracker.orphans)

    def test_next_pull_boundary_with_roster_change_stays_in_same_instance(self):
        tracker=EncounterTracker();old=wipe(tracker);old.capture_complete=False
        snapshot=normalized(received=200)
        self.assertIsNone(tracker.accept(snapshot).encounter_id)

        tracker.begin(
            instance_id='other-instance',started_at_ns=200*NS,
            participants_snapshot=[ROSTER[0]],stage_id=55,stage_index=1,
            boss_token='boss',dungeon_id=10,map_id=20,self_token='self',
        )

        self.assertEqual(old.settlement_status,'PENDING')
        self.assertEqual(len(tracker.orphans),1)

    def test_next_pull_boundary_ignores_roster_and_boss_changes(self):
        tracker=EncounterTracker();old=wipe(tracker);old.capture_complete=False
        old.boss_template_id=7100471
        old.boss_token='entity:old-boss'
        changed_roster=[
            {'id':'self','iid':101,'name':'Self'},
            {'id':'replacement','iid':301,'name':'Replacement'},
        ]
        changed_members={
            row['id']:{
                0:row['id'],1:row['iid'],5:row['name'],6:321,19:10,
                33:{123:1},34:{123:320},36:{'boss':20},37:{'boss':1},
            }
            for row in changed_roster
        }
        snapshot=normalized(received=200,members=changed_members)

        self.assertIsNone(tracker.accept(snapshot).encounter_id)
        successor=tracker.begin(
            instance_id='instance',started_at_ns=200*NS,
            participants_snapshot=[
                {'id':'self','iid':102,'name':'Self'},
                {'id':'next-peer','iid':401,'name':'Next Peer'},
            ],stage_id=None,stage_index=None,boss_template_id=7100401,
            boss_token='entity:new-boss',dungeon_id=10,map_id=20,
            self_token='self',
        )

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual(old.server_battle_id,'battle-1')
        self.assertEqual(
            {row['id'] for row in old.participants},
            {'self','replacement'},
        )
        self.assertEqual([row['damage'] for row in old.participants],[321,321])
        self.assertEqual(tracker.current_id,successor.local_encounter_id)
        self.assertFalse(tracker.orphans)

    def test_multiple_wipes_settle_in_server_order_before_new_encounter(self):
        tracker=EncounterTracker()
        first=wipe(tracker,start=100,duration=10)
        second=wipe(tracker,start=120,duration=10)
        snapshot=normalized(
            battle='second-battle', received=200, seconds=99, damage=222
        )

        # With two otherwise identical pending pulls, receipt order alone is
        # intentionally ambiguous until the following Challenge boundary.
        self.assertIsNone(tracker.accept(snapshot).encounter_id)
        self.assertEqual(first.settlement_status,'PENDING')
        self.assertEqual(second.settlement_status,'PENDING')

        new_start=200*NS+29_600
        third=tracker.begin(
            instance_id='instance',started_at_ns=new_start,
            participants_snapshot=ROSTER,stage_id=55,stage_index=1,
            boss_token='boss',dungeon_id=10,map_id=20,self_token='self',
        )

        self.assertEqual(first.settlement_status,'PENDING')
        self.assertEqual(second.settlement_status,'SETTLED')
        self.assertEqual(second.server_battle_id,'second-battle')
        self.assertEqual([row['damage'] for row in second.participants],[222,222])
        self.assertEqual(third.settlement_status,'LIVE')
        self.assertEqual(tracker.current_id,third.local_encounter_id)
        self.assertFalse(tracker.orphans)

        revision=second.revision
        duplicate=replace(snapshot,received_at_ns=201*NS)
        self.assertEqual(tracker.accept(duplicate).reason,'duplicate_ignored')
        self.assertEqual(second.revision,revision)
        self.assertTrue(all(row['damage'] is None for row in third.participants))

    def test_next_challenge_boundary_window_is_strictly_bounded(self):
        tracker=EncounterTracker();old=wipe(tracker)
        old.capture_complete=False
        snapshot=normalized(received=200,seconds=99)
        self.assertIsNone(tracker.accept(snapshot).encounter_id)

        successor=tracker.begin(
            instance_id='instance',
            started_at_ns=200*NS+NEXT_CHALLENGE_BOUNDARY_WINDOW_NS+1,
            participants_snapshot=ROSTER,stage_id=55,stage_index=1,
            boss_token='boss',dungeon_id=10,map_id=20,self_token='self',
        )

        self.assertEqual(old.settlement_status,'PENDING')
        self.assertEqual(successor.settlement_status,'LIVE')
        self.assertEqual(len(tracker.orphans),1)

    def test_unscoped_table_at_exact_next_pull_boundary_is_strictly_recovered(self):
        t=EncounterTracker();old=wipe(t)
        snapshot=replace(normalized(received=200),instance_id=None)

        self.assertIsNone(t.accept(snapshot).encounter_id)
        new=begin(t,200)

        self.assertEqual(old.settlement_status,'SETTLED')
        self.assertEqual(old.server_battle_id,'battle-1')
        self.assertEqual(old.stage_statistics['instance_id'],'instance')
        self.assertEqual([row['damage'] for row in old.participants],[100,100])
        self.assertFalse(t.orphans)
        self.assertEqual(t.current_id,new.local_encounter_id)

    def test_unscoped_table_requires_exact_next_pull_timestamp(self):
        t=EncounterTracker();old=wipe(t)
        snapshot=replace(normalized(received=201),instance_id=None)
        self.assertIsNone(t.accept(snapshot).encounter_id)

        begin(t,200)

        self.assertEqual(old.settlement_status,'PENDING')
        self.assertEqual(len(t.orphans),1)

    def test_unscoped_table_requires_exact_member_iids(self):
        t=EncounterTracker();old=wipe(t)
        snapshot=replace(normalized(received=200),instance_id=None)
        mismatched=replace(snapshot.members[1],iid=999)
        snapshot=replace(snapshot,members=(snapshot.members[0],mismatched))
        self.assertIsNone(t.accept(snapshot).encounter_id)

        begin(t,200)

        self.assertEqual(old.settlement_status,'PENDING')
        self.assertEqual(len(t.orphans),1)

    def test_unscoped_table_rejects_multiple_exact_successors(self):
        t=EncounterTracker();old=wipe(t)
        first=begin(t,200)
        t.end('RESET',200*NS)
        second=begin(t,200)
        snapshot=replace(normalized(received=200),instance_id=None)

        match=t.accept(snapshot)

        self.assertIsNone(match.encounter_id)
        self.assertEqual(old.settlement_status,'PENDING')
        self.assertEqual(len(t.orphans),1)
        self.assertNotEqual(first.local_encounter_id,second.local_encounter_id)

    def test_next_pull_boundary_never_overrides_explicit_capture_gap(self):
        t=EncounterTracker();old=wipe(t);old.capture_complete=False
        old.automatic_match_blocked=True
        self.assertIsNone(t.accept(normalized(received=200)).encounter_id)

        begin(t,200)

        self.assertEqual(old.settlement_status,'PENDING')
        self.assertEqual(len(t.orphans),1)

    def test_exact_boss_token_settles_victory_with_incomplete_start_binding(self):
        t=EncounterTracker();e=begin(t);t.end('VICTORY',137*NS)
        e.capture_complete=False
        result=t.accept(normalized(success=True))
        self.assertEqual(result.encounter_id,e.local_encounter_id)
        self.assertEqual(result.reason,'unique_victory_roster_boss_token')
        self.assertEqual(e.settlement_status,'SETTLED')

    def test_victory_adds_server_proven_projection_roster_members(self):
        tracker=EncounterTracker();encounter=begin(tracker)
        encounter.capture_complete=False
        tracker.end('VICTORY',137*NS)
        snapshot=normalized(
            success=True, received=200, members=projection_members('boss')
        )

        match=tracker.accept(snapshot)

        self.assertEqual(match.encounter_id,encounter.local_encounter_id)
        self.assertEqual(
            match.reason,'unique_victory_projection_roster_boss_token'
        )
        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(len(encounter.participants),6)
        projections=[
            row for row in encounter.participants_snapshot
            if row['id'] in {projection_token(index) for index in range(4)}
        ]
        self.assertEqual(len(projections),4)
        self.assertTrue(all(row['is_ai'] is True for row in projections))

    def test_victory_matches_previous_boss_when_next_party_is_completely_new(self):
        tracker = EncounterTracker()
        previous = begin(tracker, start=100, roster=ROSTER)
        previous.boss_token = 'entity:previous-boss'
        tracker.end('VICTORY', 137 * NS)
        next_roster = [
            {'id': 'new-self', 'iid': 501, 'name': 'New Self'},
            {'id': 'new-peer', 'iid': 502, 'name': 'New Peer'},
        ]
        next_boss = begin(tracker, start=200, roster=next_roster)
        tracker.end('RESET', 210 * NS)
        members = projection_members('entity:previous-boss')
        for row in members.values():
            row[19] = 37

        match = tracker.accept(normalized(
            battle='previous-battle', received=200, success=True,
            members=members,
        ))

        self.assertEqual(match.encounter_id, previous.local_encounter_id)
        self.assertEqual(match.reason, 'unique_victory_next_boss_boundary')
        self.assertEqual(previous.settlement_status, 'SETTLED')
        self.assertEqual(len(previous.participants), 6)
        self.assertEqual(next_boss.result, 'RESET')

    def test_next_boss_boundary_cannot_use_another_dungeon(self):
        tracker = EncounterTracker()
        previous = begin(tracker, start=100, roster=ROSTER)
        previous.boss_token = 'entity:previous-boss'
        tracker.end('VICTORY', 137 * NS)
        next_boss = begin(tracker, start=200, roster=ROSTER)
        next_boss.instance_id = 'another-dungeon'
        tracker.end('RESET', 210 * NS)
        members = projection_members('wire-boss-token')
        for row in members.values():
            row[19] = 37

        match = tracker.accept(normalized(
            battle='previous-battle', received=200, success=True,
            members=members,
        ))

        self.assertIsNone(match.encounter_id)
        self.assertEqual(previous.settlement_status, 'PENDING')

    def test_final_victory_entity_boss_token_uses_exact_same_dungeon_stage(self):
        tracker = EncounterTracker()
        roster = [
            *ROSTER,
            *({'id': projection_token(index), 'iid': None, 'name': None,
               'is_ai': True} for index in range(4)),
        ]
        encounter = begin(tracker, start=100, roster=roster)
        encounter.boss_token = 'entity:local-boss'
        tracker.end('VICTORY', 200 * NS)
        snapshot = replace(
            normalized(
                battle='final-battle', received=200, success=True,
                members=projection_members('wire-boss-token'),
            ),
            source='OnMsgSettlementCombatStatistics',
        )

        wrong_dungeon = tracker.accept(replace(snapshot, instance_id='another-dungeon'))
        self.assertIsNone(wrong_dungeon.encounter_id)
        self.assertEqual(encounter.settlement_status, 'PENDING')

        match = tracker.accept(snapshot)

        self.assertEqual(match.encounter_id, encounter.local_encounter_id)
        self.assertEqual(match.reason, 'unique_final_victory_stage_roster_boundary')
        self.assertEqual(encounter.settlement_status, 'SETTLED')
        self.assertEqual(encounter.boss_token, 'wire-boss-token')
        self.assertEqual(len(encounter.participants), 6)

    def test_final_victory_with_add_references_still_settles_current_boss(self):
        tracker = EncounterTracker()
        roster = [
            *ROSTER,
            *({'id': projection_token(index), 'iid': None, 'name': None,
               'is_ai': True} for index in range(4)),
        ]
        encounter = begin(tracker, start=100, roster=roster)
        encounter.boss_token = 'entity:local-boss'
        tracker.end('VICTORY', 200 * NS)
        members = projection_members('wire-boss-token')
        members['self'][36] = {'wire-boss-token': 10, 'boss-add-one': 5,
                               'boss-add-two': 5}
        snapshot = replace(
            normalized(
                battle='final-battle', received=200, success=True,
                members=members,
            ),
            source='OnMsgSettlementCombatStatistics',
        )

        match = tracker.accept(snapshot)

        self.assertEqual(match.encounter_id, encounter.local_encounter_id)
        self.assertEqual(match.reason, 'unique_final_victory_stage_roster_boundary')
        self.assertEqual(encounter.settlement_status, 'SETTLED')
        self.assertEqual(encounter.boss_token, 'entity:local-boss')
        self.assertEqual(len(encounter.participants), 6)

    def test_victory_projection_extension_rejects_missing_human_member(self):
        tracker=EncounterTracker();encounter=begin(tracker)
        encounter.capture_complete=False
        tracker.end('VICTORY',137*NS)
        members=projection_members('boss')
        human_token = role_token(0x01, 99)
        human = members.pop(projection_token(3))
        human[0] = human_token
        human[5] = 'Late Human'
        members[human_token] = human

        match=tracker.accept(normalized(
            success=True, received=200, members=members
        ))

        self.assertIsNone(match.encounter_id)
        self.assertEqual(encounter.settlement_status,'PENDING')
        self.assertEqual(len(encounter.participants_snapshot),2)

    def test_legacy_entity_boss_victory_is_recovered_at_adjacent_boundary(self):
        tracker=EncounterTracker();encounter=begin(tracker)
        encounter.boss_token='entity:246559577302881'
        encounter.capture_complete=False
        tracker.end('VICTORY',137*NS)
        raw=packet(
            received=138, success=True,
            members=projection_members('stable-boss-token'),
        )
        raw['decoded_arguments'][0][0]=56
        raw['decoded_arguments'][0][1]=2
        snapshot=normalize_statistics(raw,instance_id='instance')[0]

        match=tracker.accept(snapshot)

        self.assertEqual(match.encounter_id,encounter.local_encounter_id)
        self.assertEqual(
            match.reason,
            'unique_victory_projection_roster_entity_boss_boundary',
        )
        self.assertEqual(encounter.boss_token,'stable-boss-token')
        self.assertEqual(encounter.stage_id,56)
        self.assertEqual(encounter.stage_index,2)
        self.assertEqual(len(encounter.participants),6)

    def test_legacy_entity_boss_migration_requires_adjacent_boundary(self):
        tracker=EncounterTracker();encounter=begin(tracker)
        encounter.boss_token='entity:246559577302881'
        encounter.capture_complete=False
        tracker.end('VICTORY',137*NS)

        match=tracker.accept(normalized(
            received=140, success=True,
            members=projection_members('stable-boss-token'),
        ))

        self.assertIsNone(match.encounter_id)
        self.assertEqual(encounter.boss_token,'entity:246559577302881')
        self.assertEqual(encounter.settlement_status,'PENDING')

    def test_capture_gap_blocks_exact_boss_token_victory_match(self):
        t=EncounterTracker();e=begin(t);t.end('VICTORY',137*NS)
        e.capture_complete=False;e.automatic_match_blocked=True
        self.assertIsNone(t.accept(normalized(success=True)).encounter_id)

    def test_retry_orphans_rekeys_legacy_healer_fingerprint(self):
        t=EncounterTracker()
        stat=normalized(success=True)
        legacy=stat.to_dict()
        peer=next(member for member in legacy['members'] if member['id']=='peer')
        peer['damage']=None
        peer['skill_damage']={}
        t.orphans['legacy-fingerprint']={
            'statistics':legacy,'reason':'no_proven_candidate'
        }

        t.retry_orphans()

        self.assertNotIn('legacy-fingerprint',t.orphans)
        self.assertEqual(len(t.orphans),1)
        canonical=NormalizedCombatStatistics.from_dict(legacy).fingerprint
        self.assertIn(canonical,t.orphans)

    def test_mid_instance_entity_token_is_recovered_from_complete_wipe_table(self):
        t=EncounterTracker();e=wipe(t);e.boss_token='entity:57246008840302'
        result=t.accept(normalized())
        self.assertEqual(result.encounter_id,e.local_encounter_id)
        self.assertEqual(e.settlement_status,'SETTLED')

    def test_server_member_times_create_verified_dps_denominator(self):
        t=EncounterTracker();e=wipe(t);t.accept(normalized())
        self.assertEqual(e.encounter_duration_seconds,10)
        self.assertEqual(e.duration_source,'server_encounter_clock')
        self.assertEqual([r['dps'] for r in e.participants],[10,10])

    def test_divergent_member_segments_do_not_shorten_a_long_local_boss_pull(self):
        tracker = EncounterTracker()
        encounter = begin(tracker, start=100)
        tracker.end('VICTORY', 450 * NS)
        members = {
            'self': {
                0: 'self', 1: 100, 5: 'Self', 6: 100,
                19: 37, 33: {123: 1}, 34: {123: 99},
                36: {}, 37: {'boss': 1},
            },
            'peer': {
                0: 'peer', 1: 200, 5: 'Peer', 6: 200,
                19: 5, 33: {123: 1}, 34: {123: 199},
                36: {}, 37: {'boss': 1},
            },
        }
        stat = normalized(received=500, success=True, members=members)

        self.assertEqual(tracker.accept(stat).encounter_id, encounter.local_encounter_id)
        self.assertEqual(encounter.duration_source, 'local_encounter_endpoints')
        self.assertEqual(encounter.encounter_duration_seconds, 350.0)
        self.assertEqual(
            {row['id']: row['dps'] for row in encounter.participants},
            {'self': 100 / 350, 'peer': 200 / 350},
        )

    def test_stale_stage_cannot_assign_next_boss_table_to_previous_victory(self):
        tracker = EncounterTracker()
        previous = begin(tracker, start=100)
        previous.boss_token = 'previous-boss'
        tracker.end('VICTORY', 450 * NS)
        members = {
            row['id']: {
                0: row['id'], 1: row['iid'], 5: row['name'], 6: 100,
                19: 37, 33: {123: 1}, 34: {123: 99},
                36: {'next-boss': 10}, 37: {'next-boss': 1},
            }
            for row in ROSTER
        }
        stat = normalized(received=500, members=members)

        match = tracker.accept(stat)

        self.assertIsNone(match.encounter_id)
        self.assertEqual(previous.settlement_status, 'PENDING')
        self.assertIsNone(previous.server_battle_id)

    def test_departed_participants_match_only_their_boss_and_clock(self):
        tracker = EncounterTracker()
        roster = ROSTER + [
            {'id': 'departed-one', 'iid': 300, 'name': 'Departed One'},
            {'id': 'departed-two', 'iid': 400, 'name': 'Departed Two'},
        ]
        previous = begin(tracker, start=100, roster=ROSTER)
        previous.boss_token = 'first-boss'
        tracker.end('VICTORY', 450 * NS)
        members = {
            row['id']: {
                0: row['id'], 1: row['iid'], 5: row['name'],
                6: 100 + index, 19: 350,
                36: {'first-boss': 10}, 37: {},
            }
            for index, row in enumerate(roster)
        }
        first = normalized(
            battle='first-battle', received=500, success=True,
            members=members,
        )
        self.assertEqual(
            tracker.accept(first).reason,
            'unique_boss_token_partial_roster_clock',
        )
        self.assertEqual(previous.server_battle_id, 'first-battle')
        self.assertEqual(len(previous.participants), 4)
        self.assertEqual(previous.encounter_duration_seconds, 350)

        next_boss = begin(tracker, start=510, roster=ROSTER)
        next_boss.boss_token = 'next-boss'
        tracker.end('WIPE', 547 * NS)
        next_members = deepcopy(members)
        for row in next_members.values():
            row[19] = 37
            row[36] = {'next-boss': 10}
        delayed = normalized(
            battle='next-battle', received=570, members=next_members,
        )
        self.assertEqual(
            tracker.accept(delayed).encounter_id,
            next_boss.local_encounter_id,
        )
        self.assertEqual(next_boss.server_battle_id, 'next-battle')
        self.assertEqual(previous.server_battle_id, 'first-battle')

    def test_partial_roster_rejects_unrelated_boss_or_short_clock(self):
        tracker = EncounterTracker()
        encounter = begin(tracker, start=100)
        encounter.boss_token = 'first-boss'
        tracker.end('VICTORY', 450 * NS)
        members = {
            row['id']: {
                0: row['id'], 1: row['iid'], 5: row['name'],
                6: 100, 19: 37, 36: {'next-boss': 10}, 37: {},
            }
            for row in ROSTER
        }
        members['departed'] = {
            0: 'departed', 1: 300, 5: 'Departed',
            6: 100, 19: 37, 36: {'next-boss': 10}, 37: {},
        }
        self.assertIsNone(tracker.accept(normalized(
            battle='next-battle', received=500, members=members,
        )).encounter_id)
        for row in members.values():
            row[36] = {'first-boss': 10}
        self.assertIsNone(tracker.accept(normalized(
            battle='short-clock', received=501, members=members,
        )).encounter_id)
        self.assertEqual(encounter.settlement_status, 'PENDING')

    def test_settled_deaths_prefer_server_killer_map_over_missing_local_events(self):
        tracker=EncounterTracker();encounter=wipe(tracker)
        encounter.life_events={}

        tracker.accept(normalized())

        self.assertEqual(encounter.settlement_status,'SETTLED')
        self.assertEqual(encounter.death_count('self'),1)
        self.assertEqual(encounter.death_count('peer'),1)
        self.assertEqual(
            {row['id']:row['killer_map'] for row in encounter.participants},
            {'self':{'boss':1},'peer':{'boss':1}},
        )

    def test_verified_common_clock_applies_to_all_members(self):
        t=EncounterTracker();e=wipe(t)
        e.duration_evidence={'source':'server_encounter_clock','seconds':10,'verified':True,'local_encounter_id':e.local_encounter_id}
        t.accept(normalized())
        self.assertEqual([p['dps'] for p in e.participants],[10,10])

    def test_failed_then_victory_no_cross_write(self):
        t=EncounterTracker();old=wipe(t);new=begin(t,200)
        t.accept(normalized(received=200))
        t.end('VICTORY',220*NS)
        t.accept(normalized(battle='victory',success=True,received=221,damage=999))
        self.assertEqual(old.participants[0]['damage'],100)
        self.assertEqual(new.participants[0]['damage'],999)
        self.assertEqual(new.result,'VICTORY')

    def test_restart_retains_pending_and_binding(self):
        t=EncounterTracker();e=wipe(t);begin(t,200)
        restored=EncounterTracker.from_dict(json.loads(json.dumps(t.to_dict())))
        self.assertEqual(restored.accept(normalized()).encounter_id,e.local_encounter_id)
        self.assertIsNone(restored.encounters[restored.current_id].participants[1]['damage'])

    def test_success_table_before_death_waits_as_orphan(self):
        t=EncounterTracker();e=begin(t)
        self.assertIsNone(t.accept(normalized(success=True)).encounter_id)
        t.end('VICTORY',199*NS)
        self.assertEqual(e.settlement_status,'SETTLED')

    def test_server_snapshot_fills_missing_start_identity(self):
        roster = [
            {'id': 'self', 'iid': None, 'name': '', 'profession_id': None},
            {'id': 'peer', 'iid': None, 'name': None},
        ]
        t = EncounterTracker()
        e = wipe(t, roster=roster)
        raw = packet()
        raw['decoded_arguments'][0][5]['self'][4] = 1_200_002
        raw['decoded_arguments'][0][5]['peer'][4] = 1_200_003

        self.assertEqual(
            t.accept(normalize_statistics(raw, instance_id='instance')[0]).encounter_id,
            e.local_encounter_id,
        )
        identities = {row['id']: row for row in e.participants_snapshot}
        self.assertEqual(
            (identities['self']['iid'], identities['self']['name'], identities['self']['profession_id']),
            (100, 'Self', 1_200_002),
        )
        self.assertEqual(
            (identities['peer']['iid'], identities['peer']['name'], identities['peer']['profession_id']),
            (200, 'Peer', 1_200_003),
        )

    def test_exact_boss_token_corrects_one_stage_late_start_context(self):
        tracker = EncounterTracker()
        encounter = begin(tracker)
        tracker.end('VICTORY', 137 * NS)
        raw = packet(received=200, success=True, seconds=37)
        raw['decoded_arguments'][0][0] = 56
        raw['decoded_arguments'][0][1] = 2
        snapshot = normalize_statistics(
            raw, instance_id='instance', dungeon_id=10, map_id=21
        )[0]

        match = tracker.accept(snapshot)

        self.assertEqual(match.encounter_id, encounter.local_encounter_id)
        self.assertEqual(match.reason, 'unique_victory_roster_boss_token')
        self.assertEqual(encounter.stage_id, 56)
        self.assertEqual(encounter.stage_index, 2)
        self.assertEqual(encounter.settlement_status, 'SETTLED')
        self.assertEqual([row['damage'] for row in encounter.participants], [100, 100])

    def test_unique_wipe_corrects_adjacent_delayed_stage_context(self):
        tracker = EncounterTracker()
        encounter = begin(tracker)
        tracker.end('WIPE', 137 * NS)
        raw = packet(received=200, seconds=37)
        raw['decoded_arguments'][0][0] = 56
        raw['decoded_arguments'][0][1] = 2
        # One player can die to a mechanic/self token, so the Boss token need
        # not be present in every killer map even though the roster is exact.
        raw['decoded_arguments'][0][5]['peer'][37] = {'peer': 1}
        snapshot = normalize_statistics(
            raw, instance_id='instance', dungeon_id=10, map_id=21
        )[0]

        match = tracker.accept(snapshot)

        self.assertEqual(match.encounter_id, encounter.local_encounter_id)
        self.assertEqual(match.reason, 'unique_wipe_adjacent_stage_roster')
        self.assertEqual(encounter.stage_id, 56)
        self.assertEqual(encounter.stage_index, 2)
        self.assertEqual(encounter.settlement_status, 'SETTLED')

    def test_grouped_final_settlement_restores_only_an_older_pending_stage(self):
        tracker = EncounterTracker()
        old = begin(tracker)
        old.boss_token = 'old-boss'
        tracker.end('VICTORY', 137 * NS)
        current_encounter = tracker.begin(
            instance_id='instance', started_at_ns=150 * NS,
            participants_snapshot=ROSTER, stage_id=56, stage_index=2,
            boss_token='current-boss', dungeon_id=10, map_id=20,
            self_token='self',
        )
        tracker.end('VICTORY', 190 * NS)
        previous_members = packet()['decoded_arguments'][0][5]
        for member in previous_members.values():
            member[36] = {'old-boss': 20}
            member[37] = {'old-boss': 1}
        current = packet(battle='current', success=True)['decoded_arguments'][0]
        current[0] = 56
        current[1] = 2
        for member in current[5].values():
            member[36] = {'current-boss': 20}
            member[37] = {'current-boss': 1}
        raw = {
            'method': 'OnMsgSettlementCombatStatistics',
            'capture_timestamp_ns': 200 * NS,
            'decoded_arguments': [{55: previous_members}, current],
        }
        current_stage, grouped = normalize_statistics(raw, instance_id='instance')

        self.assertIsNone(grouped.battle_id)
        self.assertEqual(
            tracker.accept(current_stage).encounter_id,
            current_encounter.local_encounter_id,
        )
        self.assertEqual(
            tracker.accept(grouped).encounter_id,
            old.local_encounter_id,
        )
        self.assertEqual(old.settlement_status, 'SETTLED')
        self.assertEqual(current_encounter.settlement_status, 'SETTLED')

    def test_final_bundle_never_mixes_previous_total_with_current_duration(self):
        tracker = EncounterTracker()
        roster = [dict(row) for row in ROSTER]

        previous = tracker.begin(
            instance_id='instance', started_at_ns=100 * NS,
            participants_snapshot=roster, stage_id=5_150_059,
            stage_index=2, boss_token='previous-boss', self_token='self',
        )
        tracker.end('VICTORY', 255 * NS)
        previous.duration_evidence = {
            'source': 'verified_shared_encounter_clock',
            'seconds': 155.0,
            'verified': True,
            'local_encounter_id': previous.local_encounter_id,
        }
        previous_members = {
            'self': {
                0: 'self', 1: 100, 5: 'Self', 6: 18_000_000,
                36: {'previous-boss': 1}, 37: {},
            },
            'peer': {
                0: 'peer', 1: 200, 5: 'Peer', 6: 18_339_593,
                36: {'previous-boss': 1}, 37: {},
            },
        }
        previous_raw = {
            'method': 'OnMsgUpdateStageCombatStatistics',
            'capture_timestamp_ns': 260 * NS,
            'decoded_arguments': [{
                0: 5_150_059, 1: 2, 2: 'previous-battle', 3: True,
                5: previous_members,
            }],
        }
        tracker.accept(normalize_statistics(
            previous_raw, instance_id='instance'
        )[0])

        current_encounter = tracker.begin(
            instance_id='instance', started_at_ns=300 * NS,
            participants_snapshot=roster,
            # Reproduce the real same-batch previous Statistics -> Challenge
            # leak: the third pull temporarily inherited the second stage.
            stage_id=5_150_059, stage_index=2,
            boss_token='entity:219945812459667', self_token='self',
        )
        tracker.end('VICTORY', 431_727_025_200)
        current_encounter.duration_evidence = {
            'source': 'verified_shared_encounter_clock',
            'seconds': 131.7270252,
            'verified': True,
            'local_encounter_id': current_encounter.local_encounter_id,
        }
        current_members = {
            'self': {
                0: 'self', 1: 100, 5: 'Self', 6: 17_000_000,
                36: {'current-boss-a': 1}, 37: {},
            },
            'peer': {
                0: 'peer', 1: 200, 5: 'Peer', 6: 17_185_348,
                36: {'current-boss-b': 1}, 37: {},
            },
        }
        aggregate = deepcopy(current_members)
        for values in aggregate.values():
            values[6] *= 3
        raw = {
            'method': 'OnMsgSettlementCombatStatistics',
            'capture_timestamp_ns': 447 * NS,
            'decoded_arguments': [{
                0: aggregate,
                5_150_058: previous_members,
                5_150_059: previous_members,
                5_150_060: current_members,
            }, {
                0: 5_150_060, 1: 3, 2: 'current-battle', 3: True,
                5: current_members,
            }],
        }

        matches = [
            tracker.accept(snapshot)
            for snapshot in normalize_statistics(raw, instance_id='instance')
        ]

        self.assertEqual(
            matches[0].encounter_id, current_encounter.local_encounter_id
        )
        self.assertEqual(
            matches[0].reason, 'unique_victory_adjacent_stage_roster'
        )
        self.assertEqual(current_encounter.stage_id, 5_150_060)
        self.assertEqual(current_encounter.stage_index, 3)
        self.assertEqual(current_encounter.server_battle_id, 'current-battle')
        self.assertEqual(
            sum(row['damage'] for row in current_encounter.participants),
            34_185_348,
        )
        self.assertEqual(
            sum(row['damage'] for row in previous.participants),
            36_339_593,
        )
        for row in current_encounter.participants:
            self.assertAlmostEqual(
                row['dps'], row['damage'] / 131.7270252
            )
        self.assertFalse(any(
            match.encounter_id == current_encounter.local_encounter_id
            for match in matches[2:]
        ))

    def test_historical_group_waits_until_current_battle_is_bound(self):
        tracker = EncounterTracker()
        encounter = begin(tracker)
        tracker.end('VICTORY', 137 * NS)
        previous_members = packet()['decoded_arguments'][0][5]
        current = packet(battle='current', success=True)['decoded_arguments'][0]
        current[0] = 56
        raw = {
            'method': 'OnMsgSettlementCombatStatistics',
            'capture_timestamp_ns': 200 * NS,
            'decoded_arguments': [{55: previous_members}, current],
        }
        grouped = normalize_statistics(raw, instance_id='instance')[1]

        match = tracker.accept(grouped)

        self.assertIsNone(match.encounter_id)
        self.assertEqual(match.reason, 'unbound_associated_current_stage')
        self.assertEqual(encounter.settlement_status, 'PENDING')

    def test_authoritative_current_stage_repairs_persisted_historical_misbind(self):
        tracker = EncounterTracker()
        encounter = tracker.begin(
            instance_id='instance', started_at_ns=300 * NS,
            participants_snapshot=ROSTER, stage_id=55, stage_index=2,
            boss_token='entity:300', self_token='self',
        )
        tracker.end('VICTORY', 431 * NS)
        current_stage = packet(
            battle='current-battle', success=True, damage=333
        )['decoded_arguments'][0]
        current_stage[0] = 56
        current_stage[1] = 3
        historical_raw = {
            'method': 'OnMsgSettlementCombatStatistics',
            'capture_timestamp_ns': 447 * NS,
            'decoded_arguments': [
                {55: packet(damage=222)['decoded_arguments'][0][5]},
                current_stage,
            ],
        }
        # Build the legacy bad state directly: a historical group from this
        # bundle was stored on the current encounter before its authoritative
        # second argument could match.
        current, historical = normalize_statistics(
            historical_raw, instance_id='instance'
        )
        encounter.stage_statistics = historical.to_dict()
        encounter.snapshot_fingerprints['STAGE'] = historical.fingerprint
        encounter.participants = [
            dict(row, damage=222, dps=222 / 131)
            for row in ROSTER
        ]
        encounter.settlement_status = 'SETTLED'

        match = tracker.accept(current)

        self.assertEqual(match.encounter_id, encounter.local_encounter_id)
        self.assertEqual(encounter.server_battle_id, 'current-battle')
        self.assertEqual(encounter.stage_id, 56)
        self.assertEqual(encounter.stage_index, 3)
        self.assertEqual(
            [row['damage'] for row in encounter.participants], [333, 333]
        )

    def test_previous_table_at_next_start_cannot_settle_the_new_pull(self):
        tracker = EncounterTracker()
        encounter = begin(tracker, start=200)
        snapshot = normalized(received=200)

        self.assertIsNone(tracker.accept(snapshot).encounter_id)
        tracker.end('VICTORY', 237 * NS)

        self.assertEqual(encounter.settlement_status, 'PENDING')
        self.assertIsNone(encounter.stage_statistics)

if __name__=='__main__':unittest.main()
