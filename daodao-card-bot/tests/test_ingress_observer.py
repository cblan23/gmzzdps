import unittest
from types import SimpleNamespace
from app.ingress_observer import metadata


class ObserverTest(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(bot_qq='3035610294', groups={'1094925831'})

    def test_never_stores_content_or_untrusted_fields(self):
        row = {'post_type':'message','message_type':'group','group_id':1094925831,
               'user_id':1806525,'message_id':123,'time':12345,'token':'secret',
               'message':[{'type':'at','data':{'qq':'3035610294'}},
                          {'type':'text','data':{'text':'private card secret'}}]}
        result = metadata(row, self.settings)
        self.assertEqual(result, {'kind':'group','group':'1094925831','mentioned':True,
                                 'user_id':1806525,'message_id':123,'time':12345})
        row['user_id'] = 'secret'
        self.assertNotIn('user_id', metadata(row, self.settings))

    def test_ignores_other_groups_and_private_messages(self):
        self.assertEqual(metadata({'post_type':'message','message_type':'private',
                                   'message':'secret'}, self.settings), {'kind':'other'})
        self.assertEqual(metadata({'post_type':'message','message_type':'group',
                                   'group_id':12345}, self.settings), {'kind':'other_group'})

    def test_heartbeat_requires_boolean(self):
        for value in (True, False, 'secret'):
            result = metadata({'post_type':'meta_event','meta_event_type':'heartbeat',
                               'status':{'online':value}}, self.settings)
            self.assertEqual(result['online'], value if isinstance(value, bool) else None)
