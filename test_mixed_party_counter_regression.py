"""Mixed human/projection counters must not turn a live pull into one-second totals."""
import unittest
from test_combat_model import CombatModel, BASE_FILETIME, SELF_ID, MONSTER_ID, damage

class MixedPartyCounterTests(unittest.TestCase):
    def model(self):
        model=CombatModel(run_id='mixed-party-regression')
        actors=[SELF_ID+i for i in range(12)]
        model.ingest_identity({'entity_id':SELF_ID})
        model.ingest_party({'entity_ids':actors,'member_count':12,'authoritative':True})
        for index,actor in enumerate(actors):
            model.ingest_profile({'entity_id':actor,'entity_type':'Player','name':f'player{index}' if index<3 else f'projection{index}', 'profession_id':1200005, 'is_ai':index>=3})
        model.ingest_profile({'entity_id':MONSTER_ID,'entity_type':'Boss','boss_rank':3,'boss_type':3,'template_id':7102990})
        model.ingest_monster({'entity_id':MONSTER_ID,'current_hp':66735474,'max_hp':66735474,'filetime_100ns':BASE_FILETIME})
        model.ingest_combat_state({'entity_id':MONSTER_ID,'in_combat':True,'filetime_100ns':BASE_FILETIME})
        model.ingest(damage(1000,SELF_ID,MONSTER_ID,100))
        for index,actor in enumerate(actors):self.snapshot(model,actor,100000+index*1000,60,100 if index>=3 else 0)
        model.ingest_monster({'entity_id':MONSTER_ID,'current_hp':34750529,'max_hp':66735474,'filetime_100ns':BASE_FILETIME+60*10_000_000})
        return model,actors
    def snapshot(self,model,actor,amount,seconds,epoch=0,omitted=False):
        return model.ingest_team_stat({'actor_id':actor,'absolute_damage':amount,'filetime_100ns':BASE_FILETIME+seconds*10_000_000,'server_time':epoch,'full_snapshot':True,'omitted_zero':omitted})
    def test_one_member_lower_snapshot_keeps_entire_pull_and_roster(self):
        model,actors=self.model();encounter=model.encounter_id;start=model.first_damage_time
        expected={actor:model.stats[actor].damage for actor in actors}
        self.snapshot(model,actors[0],0,61)
        for index,actor in enumerate(actors[1:],1):self.snapshot(model,actor,100000+index*1000,61,100 if index>=3 else 0)
        self.assertEqual(model.encounter_id,encounter)
        self.assertEqual(model.first_damage_time,start)
        self.assertEqual(set(model.stats),set(actors))
        self.assertEqual({actor:model.stats[actor].damage for actor in actors},expected)
        self.assertEqual(model.completed_combats,[])
    def test_omitted_counter_and_new_member_epoch_does_not_reset_whole_team(self):
        model,actors=self.model();encounter=model.encounter_id
        total=sum(row.damage for row in model.stats.values())
        self.snapshot(model,actors[5],0,62,101,omitted=True)
        self.assertEqual(model.encounter_id,encounter)
        self.assertEqual(sum(row.damage for row in model.stats.values()),total)
    def test_full_count_recovery_does_not_add_same_damage_twice(self):
        model,actors=self.model();expected=model.stats[actors[4]].damage
        self.snapshot(model,actors[4],100,61,100)
        self.snapshot(model,actors[4],105000,62,100)
        self.assertEqual(model.stats[actors[4]].damage,expected+1000)
    def test_confirmed_wipe_still_starts_fresh(self):
        model,actors=self.model();encounter=model.encounter_id
        for actor in actors:model.ingest_life({'actor_id':actor,'dead':True,'death_confirmed':True,'filetime_100ns':BASE_FILETIME+61*10_000_000})
        self.assertEqual(model.combat_end_reason,'party_wipe')
        self.snapshot(model,actors[4],0,62,200)
        self.assertNotEqual(model.encounter_id,encounter)
    def test_wounded_boss_without_fight_state_also_keeps_clock(self):
        model,actors=self.model();encounter=model.encounter_id;start=model.first_damage_time
        model.entity_combat_states.clear()
        self.snapshot(model,actors[2],0,62)
        self.assertEqual(model.encounter_id,encounter)
        self.assertEqual(model.first_damage_time,start)
    def test_each_member_position_cannot_split_large_team_totals(self):
        for position in range(12):
            with self.subTest(position=position):
                model,actors=self.model()
                for index,actor in enumerate(actors):self.snapshot(model,actor,2000000+index*100000,90,100 if index>=3 else 0)
                encounter=model.encounter_id
                expected=sum(row.damage for row in model.stats.values())
                for index,actor in enumerate(actors):
                    self.snapshot(model,actor,0 if index==position else 2000000+index*100000,91,100 if index>=3 else 0)
                record=model.build_combat_record('test_only')
                self.assertEqual(model.encounter_id,encounter)
                self.assertEqual(record['total_damage'],expected)
                self.assertEqual(len(record['participants']),12)
                self.assertGreaterEqual(record['duration_seconds'],89)
                self.assertLess(record['team_dps'],expected/80)
                self.assertEqual(len(model.team_counter_regressions),1)

if __name__=='__main__':unittest.main()
