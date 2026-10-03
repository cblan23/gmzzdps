import unittest
from dataclasses import replace
from encounter_tracker import EncounterTracker
from settlement_presenter import (
    completed_statistics_scope,
    encounter_view,
    skill_distribution,
)
from test_encounter_settlement import begin,wipe,normalized


class PresenterTests(unittest.TestCase):
    def test_live_teammates_blank_even_when_old_values_exist(self):
        t=EncounterTracker();e=begin(t)
        e.participants[1]['damage']=999
        view=encounter_view(e,live_self={'verified':True,'local_encounter_id':e.local_encounter_id,'damage':88})
        rows={r['id']:r for r in view['members']}
        self.assertEqual(rows['self']['damage_text'],'88')
        self.assertEqual(rows['peer']['damage_text'],'--')
        self.assertEqual(rows['peer']['status_text'],'战后结算')

    def test_pending_is_not_zero(self):
        t=EncounterTracker();e=wipe(t);view=encounter_view(e)
        self.assertEqual(view['status_text'],'等待结算')
        self.assertTrue(all(m['damage'] is None for m in view['members']))

    def test_settled_total_with_server_member_clock_has_dps(self):
        t=EncounterTracker();e=wipe(t);t.accept(normalized())
        view=encounter_view(e)
        self.assertEqual(view['status_text'],'延迟补齐')
        self.assertEqual(view['members'][0]['damage_text'],'100')
        self.assertEqual(view['members'][0]['dps_text'],'10')

    def test_final_victory_can_render_exact_all_scope(self):
        tracker=EncounterTracker();record=begin(tracker)
        tracker.end('VICTORY',110_000_000_000)
        stage=normalized(success=True)
        tracker.accept(stage,preferred_encounter_id=record.local_encounter_id)
        aggregate_members=tuple(
            replace(member,damage=300,member_battle_length=30)
            for member in stage.members
        )+(
            replace(
                stage.members[0],id='departed',iid=300,name='Departed',
                damage=450,member_battle_length=30,
            ),
        )
        aggregate=replace(
            stage,scope='ALL',battle_id=None,
            associated_battle_id=stage.battle_id,members=aggregate_members,
        )
        tracker.accept(aggregate)

        stage_view=encounter_view(record)
        final_view=encounter_view(record,statistics_scope='ALL')

        self.assertEqual(completed_statistics_scope(record),'ALL')
        self.assertEqual(stage_view['statistics_scope'],'STAGE')
        self.assertEqual(sum(row['damage'] for row in stage_view['members']),200)
        self.assertEqual(final_view['statistics_scope'],'ALL')
        self.assertEqual(final_view['duration_seconds'],30)
        self.assertEqual(sum(row['damage'] for row in final_view['members']),1050)
        self.assertEqual({row['id'] for row in final_view['members']},
                         {'self','peer','departed'})
        self.assertEqual(
            next(row for row in final_view['members'] if row['id']=='departed')['dps'],
            15,
        )

    def test_skills_keep_remainder_and_no_invented_timeline(self):
        t=EncounterTracker();e=wipe(t);t.accept(normalized())
        view=skill_distribution(e,'peer')
        self.assertEqual(view['unclassified_damage'],1)
        self.assertEqual(view['rows'][0]['damage'],99)
        self.assertIsNone(view['rows'][0]['crit_rate'])
        self.assertFalse(view['timeline_available'])

if __name__=='__main__':unittest.main()
