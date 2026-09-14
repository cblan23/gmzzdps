import copy
import unittest

from compare_capture_history import compare_encounters


class ComparisonTests(unittest.TestCase):
    def sample(self):
        return {'started_at_epoch': 100, 'ended_at_epoch': 110, 'total_damage': 100,
                'participants': [{'actor_id': 1, 'damage': 60, 'skill_count': 2},
                                 {'actor_id': 2, 'damage': 40, 'skill_count': 1}]}

    def test_missing_data_does_not_pass(self):
        self.assertFalse(compare_encounters({}, {})['matches'])
        second = self.sample()
        second['participants'][0].pop('skill_count')
        self.assertFalse(compare_encounters(self.sample(), second)['matches'])

    def test_equal_total_does_not_hide_player_error(self):
        second = self.sample()
        second['participants'][0]['damage'] += 1
        second['participants'][1]['damage'] -= 1
        self.assertEqual(compare_encounters(self.sample(), second)['difference_count'], 2)

    def test_participant_order_and_timestamp_tolerance(self):
        first = self.sample()
        second = copy.deepcopy(first)
        second['participants'].reverse()
        second['started_at_epoch'] += 0.01
        self.assertTrue(compare_encounters(first, second)['matches'])

    def test_duplicate_skill_events_not_collapsed(self):
        first = self.sample()
        first['event_log'] = {'rows': [[1, 20], [1, 20]]}
        second = copy.deepcopy(first)
        second['event_log']['rows'].pop()
        self.assertFalse(compare_encounters(first, second)['matches'])


if __name__ == '__main__':
    unittest.main()
