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
    def test_read_only_group_roster_keeps_cross_scene_party_and_clears_to_solo(self):
        self_token = "AQAAAOwNKLYHAAAA"
        peer_token = "AQAAAOwNuopBAAAA"
        parser = NpcapParserAdapter()
        parser.self_id = SELF
        parser.native_self_id = SELF
        parser.self_token = self_token
        parser.self_confirmed = True
        parser._bind_team_token(self_token, SELF)
        shared = {
            "method": "ReadOnlyCurrentDungeonRoster",
            "local_user_token": self_token,
            "group_id": 57422102180769,
            "filetime_100ns": TIME,
        }

        outside = dict(
            shared,
            dungeon_id=0,
            members=[
                {"user_token": self_token, "name": "本人", "is_ai": False},
                {"user_token": peer_token, "name": "副本中的队友", "is_ai": False},
            ],
        )
        updates = parser.apply_read_only_group_roster(outside)
        party = [value for kind, value in updates if kind == "party"][-1]
        self.assertTrue(party["roster_replace"])
        self.assertEqual(set(party["user_tokens"]), {self_token, peer_token})

        solo = dict(
            shared,
            dungeon_id=5100055,
            filetime_100ns=TIME + 10_000,
            members=[
                {"user_token": self_token, "name": "本人", "is_ai": False},
            ],
        )
        next_updates = parser.apply_read_only_group_roster(solo)
        next_party = [
            value for kind, value in next_updates if kind == "party"
        ][-1]
        self.assertTrue(next_party["roster_replace"])
        self.assertEqual(next_party["user_tokens"], [self_token])
        self.assertNotIn(peer_token, parser.party_tokens)

    def test_read_only_dungeon_roster_restores_live_projection_names(self):
        self_token = "AQAAAOwNKLYHAAAA"
        peer_token = "AQAAAOwNuopBAAAA"
        bot_token = "arydEbbSuqkfSnhn"
        replacement_bot = "arydEbbSuqkfSnZE"
        parser = NpcapParserAdapter()
        parser.self_id = SELF
        parser.native_self_id = SELF
        parser.self_token = self_token
        parser.self_confirmed = True
        parser._bind_team_token(self_token, SELF)
        roster = {
            "method": "ReadOnlyCurrentDungeonRoster",
            "local_user_token": self_token,
            "dungeon_id": 5100055,
            "group_id": 57422102180769,
            "filetime_100ns": TIME,
            "members": [
                {"user_token": self_token, "name": "本人", "profession_id": 1200002,
                 "extraordinary_rating": 105511, "is_ai": False},
                {"user_token": peer_token, "name": "队友", "profession_id": 1200003,
                 "extraordinary_rating": 117350, "is_ai": False},
                {"user_token": bot_token, "name": "投影甲", "profession_id": 1200005,
                 "is_ai": True},
            ],
        }

        updates = parser.apply_read_only_group_roster(roster)

        parties = [value for kind, value in updates if kind == "party"]
        self.assertEqual(len(parties), 1)
        self.assertTrue(parties[0]["roster_replace"])
        self.assertEqual(set(parties[0]["user_tokens"]),
                         {self_token, peer_token, bot_token})
        profiles = [value for kind, value in updates if kind == "profile"]
        self.assertTrue(any(value.get("name") == "投影甲" for value in profiles))
        self.assertTrue(any(value.get("extraordinary_rating") == 117350
                            for value in profiles))

        changed = dict(roster, members=[
            roster["members"][0], roster["members"][1],
            {"user_token": replacement_bot, "name": "投影乙",
             "profession_id": 1200006, "is_ai": True},
        ])
        next_updates = parser.apply_read_only_group_roster(changed)
        next_party = [value for kind, value in next_updates if kind == "party"][-1]
        self.assertEqual(set(next_party["user_tokens"]),
                         {self_token, peer_token, replacement_bot})
        self.assertNotIn(bot_token, parser.party_tokens)

        invalid = dict(roster, local_user_token="AQAAANewRoleAAAA")
        self.assertEqual(parser.apply_read_only_group_roster(invalid), [])

    @staticmethod
    def avatar_created(entity, token, name, profession_id, sequence):
        return packet(
            "NpcapEntityCreated",
            entity,
            [
                {
                    "entity_id": entity,
                    "entity_token": token,
                    "entity_class": "AvatarActor",
                    "properties": {
                        "Name": name,
                        "Level": 70,
                        "Profession": profession_id,
                    },
                }
            ],
            sequence,
        )

    def test_avatar_token_and_local_clock_identify_role_without_rating_poll(self):
        token = "AQAAAOwNkGB8AAAA"
        actor = 57_407_071_770_816
        parser = NpcapParserAdapter()

        parser.process(
            self.avatar_created(
                actor, token, "\u5f53\u524d\u89d2\u8272", 1_200_001, 1
            )
        )
        updates = parser.process(
            {
                **packet(
                    "RetNTP",
                    actor,
                    [1_789_381_234_500, 1_789_381_234_530],
                    2,
                ),
                "npcap_method_scope": "local_role",
            }
        )

        self.assertEqual(parser.self_id, actor)
        self.assertEqual(parser.native_self_id, actor)
        self.assertEqual(parser.self_token, token)
        self.assertNotIn(actor, parser.party_ids)
        self.assertNotIn(token, parser.party_tokens)
        identity = parser.current_self_identity()
        self.assertEqual(identity["name"], "\u5f53\u524d\u89d2\u8272")
        self.assertNotIn("extraordinary_rating", identity)
        self.assertTrue(
            any(kind == "identity" for kind, _value in updates)
        )

    def test_scene_switch_uses_wire_token_before_delayed_rating_poll(self):
        old_token = "AQAAAOwNKLYHAAAA"
        new_token = "AQAAAOwNkGB8AAAA"
        old_actor = 57_463_447_253_497
        new_actor = 57_407_071_770_816
        parser = NpcapParserAdapter()
        parser.apply_authoritative_local_profile(
            {
                **packet("NpcapLiveTeamProfile", old_actor, [], 1),
                "entity_id": old_actor,
                "user_token": old_token,
                "name": "\u65e7\u89d2\u8272",
                "profession_id": 1_200_002,
                "capture_source": "npcap_read_only_team_profile",
                "local_role_confirmed": True,
            }
        )
        # This is the observed wire order: the next local AvatarActor is
        # created just before the scene-boundary RPC, whose generic cleanup
        # clears the normal entity map.
        parser.process(
            self.avatar_created(
                new_actor, new_token, "\u65b0\u89d2\u8272", 1_200_001, 2
            )
        )
        parser.process(
            packet("OnMsgBeforeEnterNewSpace", new_actor, [], 3)
        )
        self.assertIsNone(parser.native_self_id)
        self.assertEqual(parser.self_token, old_token)

        updates = parser.process(
            {
                **packet(
                    "RetNTP",
                    new_actor,
                    [1_789_381_234_500, 1_789_381_234_530],
                    4,
                ),
                "npcap_method_scope": "local_role",
            }
        )

        resets = [
            value
            for kind, value in updates
            if kind == "identity_session_reset"
        ]
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]["reason"], "wire_local_token_changed")
        self.assertEqual(resets[0]["previous_user_token"], old_token)
        self.assertEqual(parser.self_id, new_actor)
        self.assertEqual(parser.native_self_id, new_actor)
        self.assertEqual(parser.self_token, new_token)
        self.assertNotIn(old_token, parser.token_actors)
        self.assertNotIn(new_actor, parser.party_ids)
        self.assertNotIn(new_token, parser.party_tokens)
        self.assertEqual(
            parser.current_self_identity()["name"], "\u65b0\u89d2\u8272"
        )

    def test_same_role_scene_rebind_does_not_reset_identity(self):
        token = "AQAAAOwNKLYHAAAA"
        old_actor = 57_463_447_253_497
        new_actor = 57_407_071_770_816
        parser = NpcapParserAdapter()
        parser.apply_authoritative_local_profile(
            {
                **packet("NpcapLiveTeamProfile", old_actor, [], 1),
                "entity_id": old_actor,
                "user_token": token,
                "name": "\u540c\u4e00\u89d2\u8272",
                "profession_id": 1_200_002,
                "capture_source": "npcap_read_only_team_profile",
                "local_role_confirmed": True,
            }
        )
        parser.process(
            self.avatar_created(
                new_actor, token, "\u540c\u4e00\u89d2\u8272", 1_200_002, 2
            )
        )
        parser.process(
            packet("OnMsgBeforeEnterNewSpace", new_actor, [], 3)
        )

        updates = parser.process(
            {
                **packet(
                    "RetNTP",
                    new_actor,
                    [1_789_381_234_500, 1_789_381_234_530],
                    4,
                ),
                "npcap_method_scope": "local_role",
            }
        )

        self.assertFalse(
            any(
                kind == "identity_session_reset"
                for kind, _value in updates
            )
        )
        self.assertEqual(parser.self_id, new_actor)
        self.assertEqual(parser.native_self_id, new_actor)
        self.assertEqual(parser.self_token, token)

    def test_log_proven_profile_replaces_character_in_same_process(self):
        old_token = "AQAAAOwNKLYHAAAA"
        new_token = "AQAAAOwNkGB8AAAA"
        old_actor = 57_463_447_253_497
        new_actor = 57_233_666_752_426
        parser = NpcapParserAdapter(
            {
                old_token: {
                    "name": "旧角色",
                    "profession_id": 1_200_002,
                    "extraordinary_rating": 80_975,
                }
            }
        )
        old_record = {
            **packet("NpcapLiveTeamProfile", old_actor, [], 1),
            "entity_id": old_actor,
            "user_token": old_token,
            "name": "旧角色",
            "profession_id": 1_200_002,
            "extraordinary_rating": 80_975,
            "client_rating_key": "CEScore",
            "client_rating_binding": "actor_CEScore",
            "capture_source": "npcap_read_only_team_profile",
            "local_role_confirmed": True,
        }
        parser.apply_authoritative_local_profile(old_record)

        switched = {
            **old_record,
            "entity_id": new_actor,
            "user_token": new_token,
            "name": "新角色",
            "extraordinary_rating": 89_361,
            "filetime_100ns": TIME + 20_000,
        }
        updates = parser.apply_authoritative_local_profile(switched)

        self.assertTrue(
            any(kind == "identity_session_reset" for kind, _value in updates)
        )
        self.assertEqual(parser.self_token, new_token)
        self.assertEqual(parser.self_id, new_actor)
        self.assertEqual(parser.native_self_id, new_actor)
        self.assertNotIn(old_token, parser.token_actors)
        identity = parser.current_self_identity()
        self.assertEqual(identity["name"], "新角色")
        self.assertEqual(identity["extraordinary_rating"], 89_361)

        # Reconnect/periodic refresh of the same new role cannot restore the
        # previous token or create a second parser epoch.
        repeated = parser.apply_authoritative_local_profile(
            {**switched, "filetime_100ns": TIME + 30_000}
        )
        self.assertFalse(
            any(kind == "identity_session_reset" for kind, _value in repeated)
        )
        self.assertEqual(parser.self_token, new_token)

    def test_log_proven_same_token_rebinds_actor_without_session_reset(self):
        token = "AQAAAOwNKLYHAAAA"
        old_actor = 57_463_447_253_497
        new_actor = 57_463_447_253_498
        parser = NpcapParserAdapter()
        first = {
            **packet("NpcapLiveTeamProfile", old_actor, [], 1),
            "entity_id": old_actor,
            "user_token": token,
            "name": "同一角色",
            "profession_id": 1_200_002,
            "extraordinary_rating": 80_975,
            "client_rating_key": "CEScore",
            "client_rating_binding": "actor_CEScore",
            "capture_source": "npcap_read_only_team_profile",
            "local_role_confirmed": True,
        }
        parser.apply_authoritative_local_profile(first)

        updates = parser.apply_authoritative_local_profile(
            {
                **first,
                "entity_id": new_actor,
                "filetime_100ns": TIME + 20_000,
            }
        )

        self.assertFalse(
            any(kind == "identity_session_reset" for kind, _value in updates)
        )
        self.assertTrue(any(kind == "actor_merge" for kind, _value in updates))
        self.assertEqual(parser.self_token, token)
        self.assertEqual(parser.self_id, new_actor)
        self.assertEqual(parser.native_self_id, new_actor)

    def test_boss_promotion_emits_one_synthetic_settlement_boundary(self):
        boss_entity = SELF + 100
        parser = NpcapParserAdapter(
            {},
            {
                '7102990': {
                    'name': '测试首领',
                    'boss_type': 3,
                    'level': 60,
                }
            },
        )
        record = {
            **packet('NpcapNativeBossType', boss_entity, [], 1),
            'entity_id': boss_entity,
            'entity_token': 'boss-token',
            'template_id': 7102990,
            'boss_type': 3,
            'capture_timestamp_ns': 1_000_000_000,
        }

        parser.process_native_boss_type(record)
        parser.process_native_boss_type({**record, 'sequence': 2})
        observations = parser.take_settlement_observations()
        confirmations = [
            value for value in observations
            if value['record']['method'] == 'NpcapBossConfirmed'
        ]

        self.assertEqual(len(confirmations), 1)
        self.assertEqual(
            confirmations[0]['record']['network_entity_id'], boss_entity
        )
        self.assertEqual(
            confirmations[0]['context']['instance_id'],
            f'npcap-bootstrap:{boss_entity}',
        )

    def test_named_recipient_restores_exact_self_identity_without_stale_rating(self):
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
        self.assertNotIn('extraordinary_rating', identity)
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

    def test_local_clock_reply_restores_same_process_remembered_identity(self):
        token = 'AQAAAOwN8l5GAAAA'
        parser = NpcapParserAdapter(
            {
                token: {
                    'name': '\u672c\u673a\u73a9\u5bb6',
                    'profession_id': 1200006,
                    'extraordinary_rating': 82791,
                }
            },
            remembered_self_token=token,
        )

        updates = parser.process(
            packet('RetNTP', SELF, [1_789_381_234_500, 1_789_381_234_530])
        )

        self.assertEqual(parser.self_id, SELF)
        self.assertEqual(parser.self_token, token)
        self.assertEqual(parser.current_self_identity()['name'], '\u672c\u673a\u73a9\u5bb6')
        self.assertNotIn('extraordinary_rating', parser.current_self_identity())
        self.assertTrue(
            any(
                kind == 'profile'
                and value.get('entity_id') == SELF
                and value.get('name') == '\u672c\u673a\u73a9\u5bb6'
                for kind, value in updates
            )
        )

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
