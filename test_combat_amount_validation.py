"""Wrapped counters must not poison a pull or fabricate a reset to zero."""
import copy
import unittest

from network_state import NetworkPacketParser, parse_combat_amount, MAX_COMBAT_AMOUNT
from test_network_state import packet, SELF_TOKEN, TEAMMATE_TOKEN
from test_combat_model import CombatModel, SELF_ID, MONSTER_ID, damage, team_stat


class CombatAmountValidationTests(unittest.TestCase):
    def test_unsigned_wrap_values_are_rejected_but_large_valid_totals_are_retained(self):
        for value in (-1, -0.5, (1 << 64) - 1, float(1 << 64), '18446744073709551615', None, True, float('inf'), float('nan')):
            self.assertIsNone(parse_combat_amount(value), repr(value))
        for value in (0, 12345, (1 << 32) + 500, 10**12, MAX_COMBAT_AMOUNT):
            self.assertEqual(parse_combat_amount(value), value)

    def test_parser_drops_only_invalid_metric_and_keeps_other_player_values(self):
        parser = NetworkPacketParser()
        updates = parser.process(packet('RetCommonCombatStatisticsByTeam', [{
            SELF_TOKEN: {'$map': [[4, 'self'], [5, (1 << 64) - 1], [6, 250], [7, 100]]},
            TEAMMATE_TOKEN: {'$map': [[4, 'mate'], [5, 2000]]},
        }]))
        rows = {value['user_token']: value for kind, value in updates if kind == 'team_stat'}
        self.assertNotIn('absolute_damage', rows[SELF_TOKEN])
        self.assertEqual(rows[SELF_TOKEN]['absolute_taken'], 250)
        self.assertEqual(rows[SELF_TOKEN]['absolute_effective_healing'], 100)
        self.assertEqual(rows[TEAMMATE_TOKEN]['absolute_damage'], 2000)
        self.assertEqual(parser.invalid_combat_values, 1)

    def model(self, dummy=False):
        model = CombatModel(run_id='wrapped-counter')
        model.ingest_identity(dict(entity_id=SELF_ID))
        model.ingest_profile(dict(entity_id=SELF_ID, profession_id=1200002, entity_type='Player'))
        model.ingest_profile(dict(entity_id=MONSTER_ID, template_id=7114225 if dummy else 7102990,
                                  entity_type='TrainingDummy' if dummy else 'Boss', boss_rank=3))
        model.ingest(damage(1, SELF_ID, MONSTER_ID, 100))
        model.ingest_team_stat(dict(team_stat(2, SELF_ID, 100), absolute_taken=10, absolute_effective_healing=50))
        return model

    def test_bad_counter_does_not_change_totals_epoch_or_good_followup(self):
        model = self.model()
        encounter = model.encounter_id
        previous = copy.deepcopy((model.team_damage_states, model.team_taken_states, model.team_healing_states))
        invalid = dict(team_stat(3, SELF_ID, (1 << 64) - 1), absolute_taken=(1 << 64) - 2,
                       absolute_effective_healing=(1 << 64) - 3)
        original = copy.deepcopy(invalid)
        model.ingest_team_stat(invalid)
        self.assertEqual(invalid, original)
        self.assertEqual((model.team_damage_states, model.team_taken_states, model.team_healing_states), previous)
        self.assertEqual(model.encounter_id, encounter)
        self.assertEqual(model.stats[SELF_ID].damage, 100)
        model.ingest_team_stat(team_stat(4, SELF_ID, 600))
        self.assertEqual(model.stats[SELF_ID].damage, 600)
        self.assertEqual(model.encounter_id, encounter)

    def test_direct_wrapped_damage_event_cannot_enter_actor_totals(self):
        model = self.model(dummy=True)
        model.ingest(damage(3, SELF_ID, MONSTER_ID, (1 << 64) - 1))
        self.assertEqual(model.stats[SELF_ID].damage, 100)

    def test_wrapped_rpc_damage_on_dummy_is_rejected_before_aggregation(self):
        parser = NetworkPacketParser()
        parser.training_dummy_entities.add(MONSTER_ID)
        updates = parser.process(packet('OnMsgDamageSyncV2',
            [SELF_ID, MONSTER_ID, 86021070, 0, 0, 100, 0, (1 << 64) - 10, False]), include_damage=True)
        self.assertFalse(any(kind == 'event' for kind, _value in updates))
        self.assertEqual(parser.invalid_combat_values, 1)
