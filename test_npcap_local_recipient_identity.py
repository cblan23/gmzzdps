import unittest
from npcap_parser_adapter import NpcapParserAdapter, normalize_npcap_record

SELF = 57266949828970
BOT = 57266949828971
TIME = 134321845000000000


def packet(method, entity, arguments, sequence=1):
    return {'method': method, 'network_entity_id': entity, 'script_entity': entity,
            'capture_source': 'npcap', 'arguments_synchronized': True,
            'decoded_arguments': arguments, 'sequence': sequence,
            'filetime_100ns': TIME + sequence*10000}


class LocalRecipientTests(unittest.TestCase):
    def test_named_recipient_restores_exact_local_name_and_rating(self):
        token = 'AQAAAOwNKLYHAAAA'
        bot_token = 'aqFyZFqM2Vv1Z3Uy'
        parser = NpcapParserAdapter({token: {'name': '测试本人', 'profession_id': 1200002,
                                            'extraordinary_rating': 78192}}, remembered_self_token=token)
        incoming = packet('OnSyncTeamGroupPropsForceRefresh', 0, [bot_token, 14769, 14769])
        incoming['npcap_recipient'] = token
        parser.process(incoming)
        self.assertEqual(parser.self_token, token)
        identity = parser.current_self_identity()
        self.assertEqual(identity['name'], '测试本人')
        self.assertEqual(identity['extraordinary_rating'], 78192)
        parser.process(packet('OnUpdateTeamGroupSelfProps', SELF, [{4: False, 5: 100, 6: 100}], 2))
        self.assertEqual(parser.self_id, SELF)
        self.assertEqual(parser.current_self_identity()['name'], '测试本人')
        self.assertNotEqual(parser.token_actors.get(bot_token), SELF)

    def test_projection_recipient_cannot_claim_local_identity(self):
        parser = NpcapParserAdapter()
        incoming = packet('OnSyncTeamGroupPropsForceRefresh', 0, ['aqFyZFqM2Vv1Z3Uy', 100, 100])
        incoming['npcap_recipient'] = 'aqFyZFqM2Vv1Z3Uy'
        parser.process(incoming)
        self.assertIsNone(parser.self_token)

    def test_same_skill_projection_never_becomes_local_player(self):
        parser = NpcapParserAdapter()
        parser.process(packet('OnMsgCastSkillNew', BOT, [86021010], 1))
        replies = parser.process(packet('RetCastSkillSuccessNew', SELF, [86021010, 10002184, 1], 2))
        parser.process(packet('OnMsgCastSkillNew', BOT, [86021010], 3))
        self.assertEqual(parser.self_id, SELF)
        self.assertTrue(parser.self_confirmed)
        self.assertTrue(any(kind == 'identity' and value['entity_id'] == SELF for kind, value in replies))
        casts = [value for kind, value in replies if kind == 'skill_cast']
        self.assertEqual(casts[0]['actor_id'], SELF)
        self.assertFalse(parser.entity_profiles[SELF]['is_ai'])

    def test_local_properties_identify_self_before_damage(self):
        parser = NpcapParserAdapter()
        parser.process(packet('OnUpdateTeamGroupSelfProps', SELF, [{4: False, 5: 100, 6: 100}]))
        self.assertEqual(parser.self_id, SELF)
        updates = parser.process(packet('OnMsgEntityDead', SELF, [], 2))
        deaths = [value for kind, value in updates if kind == 'life']
        self.assertTrue(any(value['actor_id'] == SELF and value['death_confirmed'] for value in deaths))

    def test_capture_latency_does_not_change_legacy_argument_age(self):
        record = packet('OnUpdateTeamGroupSelfProps', SELF, [{4: False}])
        record['decode_delay_ms'] = 900
        adapted = normalize_npcap_record(record)
        self.assertEqual(record['decode_delay_ms'], 900)
        self.assertEqual(adapted['decode_delay_ms'], 0)
        self.assertEqual(adapted['capture_decode_latency_ms'], 900)
        record['capture_source'] = 'legacy'
        self.assertIs(normalize_npcap_record(record), record)


if __name__ == '__main__':
    unittest.main()
