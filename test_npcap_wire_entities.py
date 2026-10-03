import struct
import unittest
import msgpack
from npcap_protocol import NpcapProtocolDecoder
from npcap_parser_adapter import NpcapParserAdapter
from npcap_wire_entities import decode_creation
from test_encounter_settlement import projection_token


ENTITY = 57236882400409


def create(entity=ENTITY, cls=b'NpcActor', token=b'boss', props=None):
    return [{},[token,entity,cls,msgpack.packb([props or {0:7102990,1:50,3:3},{}],use_bin_type=True)]]


def envelope(kind,value):
    payload=msgpack.packb(value,use_bin_type=True)
    return struct.pack('<IH',len(payload)+2,kind)+payload


class WireEntityTests(unittest.TestCase):
    def test_validated_future_dummy_is_promoted_on_creation(self):
        parser = NpcapParserAdapter(boss_template_catalog={})
        entity = ENTITY + 90
        updates = parser.process(
            {
                "method": "NpcapEntityCreated",
                "capture_source": "npcap",
                "arguments_synchronized": True,
                "decoded_arguments": [
                    {
                        "entity_id": entity,
                        "entity_token": "future-dummy",
                        "entity_class": "NpcActor",
                        "properties": {
                            "TemplateID": 7_100_635,
                            "Level": 90,
                            "BossType": 3,
                        },
                        "damage_target_validated": True,
                        "localization_id": 422_213_270_373_888,
                        "name": "伤害木桩",
                    }
                ],
                "network_entity_id": entity,
                "script_entity": entity,
                "filetime_100ns": 134_353_748_206_558_295,
            }
        )

        profile = next(value for kind, value in updates if kind == "profile")
        self.assertEqual(profile["name"], "伤害木桩")
        self.assertIn(entity, parser.training_dummy_entities)
        self.assertEqual(parser.active_boss_entity_id, entity)

    def test_new_projection_space_clears_old_projections_when_leave_packet_missing(self):
        parser = NpcapParserAdapter()
        self_id = 57_266_949_828_970
        old_id = self_id + 1
        self_token = "AQAAAOwNKLYHAAAA"
        old_token = projection_token(0)
        parser.self_id = self_id
        parser.self_token = self_token
        parser.self_confirmed = True
        parser.token_actors[self_token] = self_id
        parser.actor_tokens[self_id] = self_token
        parser.token_actors[old_token] = old_id
        parser.actor_tokens[old_id] = old_token
        parser.party_ids.add(old_id)
        parser.party_tokens.add(old_token)
        parser.other_party_tokens.add(old_token)
        parser.authoritative_party_tokens.update({self_token, old_token})
        parser.party_member_count = 2
        parser._begin_party_session({"filetime_100ns": 100}, explicit=True)

        base = {
            "capture_source": "npcap",
            "arguments_synchronized": True,
            "network_entity_id": self_id,
            "script_entity": self_id,
        }
        display = parser.process({
            **base, "method": "OnMsgDungeonBotDisplay",
            "decoded_arguments": [[
                {0: 1_000_001, 1: 70, 4: 1_200_006, 5: "科林·投影"}
            ]],
            "filetime_100ns": 200,
        })
        bot_id = next(value["entity_id"] for kind, value in display if kind == "profile")
        updates = parser.process({
            **base, "method": "NpcapEntityCreated",
            "decoded_arguments": [{
                "entity_id": self_id + 50,
                "entity_class": "DungeonSpace",
                "entity_token": "new-space",
                "properties": {"TemplateID": 5_200_002},
            }],
            "filetime_100ns": 300,
        })

        roster = next(value for kind, value in updates if kind == "party")
        self.assertTrue(roster["roster_replace"])
        self.assertEqual(roster["entity_ids"], [bot_id])
        self.assertNotIn(old_token, parser.party_tokens)

    def test_projection_creation_replaces_display_slot_before_roster_paint(self):
        parser = NpcapParserAdapter()
        self_id = 57_266_949_828_970
        bot_id = self_id + 50
        self_token = "AQAAAOwNKLYHAAAA"
        bot_token = "aqFyZFqM2Vv1Z3Uy"
        parser.self_id = self_id
        parser.self_token = self_token
        parser.self_confirmed = True
        parser.token_actors[self_token] = self_id
        parser.actor_tokens[self_id] = self_token

        def record(method, args, sequence):
            return {
                "method": method,
                "decoded_arguments": args,
                "capture_source": "npcap",
                "arguments_synchronized": True,
                "network_entity_id": self_id,
                "script_entity": self_id,
                "filetime_100ns": 134_321_845_085_764_054 + sequence,
                "sequence": sequence,
            }

        display = parser.process(record(
            "OnMsgDungeonBotDisplay",
            [[{0: 1_000_001, 1: 70, 4: 1_200_006, 5: "科林·投影"}]],
            1,
        ))
        temporary_actor = next(
            value["entity_id"] for kind, value in display if kind == "profile"
        )
        parser.process(record(
            "OnMsgSyncTeamGroupMemberFreqProp",
            [bot_token, 3, [123, {"x": 1}]],
            2,
        ))
        join = record(
            "OnMsgOtherJoinTeamGroup",
            [0, bot_id, {"$map": [
                [2, bot_token], [5, "科林·投影"], [6, 123456],
                [8, 1_200_006], [9, 70],
            ]}],
            3,
        )
        join["npcap_method_scope"] = "local_role"
        parser.process(join)
        self.assertIn(temporary_actor, parser.party_ids)
        created = record("NpcapEntityCreated", [{
            "entity_id": bot_id,
            "entity_class": "AvatarActor",
            "entity_token": bot_token,
            "properties": {"Name": "科林·投影", "Profession": 1_200_006},
        }], 4)
        created["network_entity_id"] = bot_id
        created["script_entity"] = bot_id
        updates = parser.process(created)

        self.assertEqual(parser.party_ids, {bot_id})
        self.assertNotIn(temporary_actor, parser.entity_profiles)
        self.assertFalse(parser.dungeon_bot_display_profiles)
        self.assertTrue(all(
            value["member_count"] <= 2
            for kind, value in updates if kind == "party"
        ))

    @staticmethod
    def hp_record(entity, hp, stamp):
        return {
            'method':'OnMsgSyncCurrentHp','capture_source':'npcap',
            'arguments_synchronized':True,'decoded_arguments':[float(hp)],
            'network_entity_id':entity,'script_entity':entity,
            'filetime_100ns':stamp // 100 + 116_444_736_000_000_000,
            'capture_timestamp_ns':stamp,
        }

    def test_template_comes_from_packet_not_memory(self):
        d=NpcapProtocolDecoder();data=envelope(11,create())
        d._append_application(data,1,1789310000)
        records=d._rpc_records()
        self.assertEqual(records[0]['method'],'NpcapEntityCreated')
        p=NpcapParserAdapter({}, {'7102990':{'name':'Boss','boss_type':3}})
        updates=p.process(records[0])
        self.assertEqual(p.entity_template_ids[ENTITY],7102990)
        self.assertEqual(p.wire_entity_tokens[ENTITY],'boss')
        self.assertEqual(p.wire_instance_id, f'npcap-bootstrap:{ENTITY}')
        self.assertTrue(any(k=='profile' for k,v in updates))

    def test_mid_instance_native_boss_creates_stable_fallback_context(self):
        p=NpcapParserAdapter({}, {'7102990':{'name':'Boss','boss_type':3}})
        p.process_native_boss_type({
            'entity_id':ENTITY,'template_id':7102990,'boss_type':3,
            'filetime_100ns':134338000000000000,
        })
        self.assertEqual(p.wire_instance_id,f'npcap-bootstrap:{ENTITY}')
        self.assertEqual(p.wire_entity_tokens[ENTITY],f'entity:{ENTITY}')

        p.process_native_boss_type({
            'entity_id':ENTITY+1,'template_id':7102990,'boss_type':3,
            'filetime_100ns':134338000010000000,
        })
        self.assertEqual(p.wire_instance_id,f'npcap-bootstrap:{ENTITY}')

    def test_preconfirmation_hp_decline_emits_one_original_time_boss_start(self):
        p=NpcapParserAdapter({}, {'7102990':{'name':'Boss','boss_type':3}})
        first=1_789_453_268_314_732_400
        p.process(self.hp_record(ENTITY,1_179_338,first))
        p.process(self.hp_record(ENTITY,690_899,first+540_056_000))

        updates=p.process_native_boss_type({
            'function':'CommonComponent_TargetIdLookup',
            'entity_id':ENTITY,'template_id':7102990,'boss_type':3,
            # The read-only metadata sample can be timestamped fractionally
            # before the already delivered HP observation.
            'filetime_100ns':first // 100 + 116_444_736_000_000_000 - 2_000_000,
        })

        combat=[value for kind,value in updates if kind=='combat_state']
        self.assertEqual(len(combat),1)
        self.assertTrue(combat[0]['in_combat'])
        self.assertTrue(combat[0]['passive_hp_activity'])
        self.assertEqual(
            combat[0]['filetime_100ns'],
            first // 100 + 116_444_736_000_000_000,
        )
        observations=p.take_settlement_observations()
        self.assertEqual(
            [row['record']['method'] for row in observations],
            ['NpcapBossHpActivity','NpcapBossConfirmed'],
        )
        self.assertEqual(observations[0]['timestamp_ns'],first)
        self.assertEqual(
            observations[0]['updates'][0][1]['source_method'],
            'NpcapBossHpActivity',
        )

        # Repeated metadata for the same entity/scene cannot create another
        # start edge or another Encounter.
        repeated=p.process_native_boss_type({
            'function':'CommonComponent_TargetIdLookup',
            'entity_id':ENTITY,'template_id':7102990,'boss_type':3,
            'filetime_100ns':first // 100 + 116_444_736_000_000_000,
        })
        self.assertFalse(any(kind=='combat_state' for kind,_ in repeated))
        self.assertEqual(p.take_settlement_observations(),[])

    def test_flat_preconfirmation_hp_does_not_fabricate_combat(self):
        p=NpcapParserAdapter({}, {'7102990':{'name':'Boss','boss_type':3}})
        first=1_789_453_268_314_732_400
        p.process(self.hp_record(ENTITY,1_179_338,first))
        p.process(self.hp_record(ENTITY,1_179_338,first+100_000_000))

        updates=p.process_native_boss_type({
            'entity_id':ENTITY,'template_id':7102990,'boss_type':3,
            'filetime_100ns':first // 100 + 116_444_736_000_000_000,
        })

        self.assertFalse(any(kind=='combat_state' for kind,_ in updates))
        self.assertEqual(
            [row['record']['method'] for row in p.take_settlement_observations()],
            ['NpcapBossConfirmed'],
        )

    def test_nonfinite_preconfirmation_hp_is_ignored(self):
        p=NpcapParserAdapter({}, {'7102990':{'name':'Boss','boss_type':3}})
        first=1_789_453_268_314_732_400

        for offset, hp in enumerate((float('nan'), float('inf'), -float('inf'))):
            p.process(self.hp_record(ENTITY, hp, first + offset * 100_000_000))

        self.assertNotIn(ENTITY, p.unconfirmed_hp_activity)

    def test_forwarded_creation_and_rpc_preserve_order(self):
        body=envelope(11,create())+envelope(22,[{},[123,19,[2]]])
        metadata=msgpack.packb({})
        data=struct.pack('<IH',2+len(metadata)+len(body),3+(len(metadata)<<8))+metadata+body
        d=NpcapProtocolDecoder();d._append_application(data,1,1789310000)
        records=d._rpc_records()
        self.assertEqual([r['method'] for r in records],['NpcapEntityCreated','OnMsgSyncFightMode'])

    def test_confirmed_fight_start_replaces_exited_boss_with_stale_hp(self):
        old_id = ENTITY
        ancestor_id = ENTITY + 1
        baldwin_id = ENTITY + 2
        base = 1_789_000_000_000_000_000
        parser = NpcapParserAdapter(
            {},
            {
                '7109821': {'name': '旧首领', 'boss_type': 3},
                '7103402': {'name': '先祖铠甲', 'boss_type': 3},
                '7103401': {'name': '伯德温·威瑟尔', 'boss_type': 3},
            },
            boss_name_allowlist=('旧首领', '先祖铠甲', '伯德温·威瑟尔'),
        )

        def created(entity, template, name, offset):
            return {
                'method': 'NpcapEntityCreated',
                'capture_source': 'npcap',
                'arguments_synchronized': True,
                'decoded_arguments': [{
                    'entity_id': entity,
                    'entity_token': name,
                    'entity_class': 'NpcActor',
                    'properties': {
                        'TemplateID': template,
                        'Level': 78,
                        'BossType': 3,
                    },
                }],
                'network_entity_id': entity,
                'script_entity': entity,
                'capture_timestamp_ns': base + offset,
                'filetime_100ns': (
                    (base + offset) // 100 + 116_444_736_000_000_000
                ),
            }

        def rpc(entity, method, args, offset):
            return {
                **created(entity, 0, '', offset),
                'method': method,
                'decoded_arguments': args,
            }

        parser.process(created(old_id, 7_109_821, 'old', 0))
        parser.process(rpc(old_id, 'OnMsgSyncFightMode', [2], 1_000))
        parser.process(rpc(
            old_id, 'OnMsgSyncDirtyFightAttributes', [{'21': 82_856_846.0}],
            2_000,
        ))
        parser.process(rpc(old_id, 'OnMsgSyncCurrentHp', [12_345_678.0], 3_000))
        parser.process(rpc(old_id, 'OnMsgSyncFightMode', [0], 4_000))

        # Catalog metadata alone must not replace a still-alive cached target.
        parser.process(created(ancestor_id, 7_103_402, 'ancestor', 5_000))
        self.assertEqual(parser.active_boss_entity_id, old_id)

        # The new entity's own formal fight start is the missing boundary.
        parser.process(rpc(ancestor_id, 'OnMsgSyncFightMode', [2], 6_000))
        self.assertEqual(parser.active_boss_entity_id, ancestor_id)
        self.assertEqual(parser.active_boss_pointer, ancestor_id)
        self.assertEqual(parser.pointer_entities[ancestor_id], ancestor_id)

        parser.process(rpc(
            ancestor_id,
            'OnMsgSyncDirtyFightAttributes',
            [{'21': 71_003_402.0}],
            7_000,
        ))
        hp_updates = parser.process(rpc(
            ancestor_id, 'OnMsgSyncCurrentHp', [70_000_000.0], 8_000,
        ))
        self.assertTrue(any(
            kind == 'monster'
            and value.get('entity_id') == ancestor_id
            and value.get('current_hp') == 70_000_000.0
            for kind, value in hp_updates
        ))

        # The verified second phase remains one encounter and switches at its
        # creation edge even while the first phase still has cached HP.
        parser.process(created(baldwin_id, 7_103_401, 'baldwin', 9_000))
        self.assertEqual(parser.active_boss_entity_id, baldwin_id)
        parser.process(rpc(baldwin_id, 'OnMsgSyncFightMode', [2], 10_000))
        parser.process(rpc(
            baldwin_id,
            'OnMsgSyncDirtyFightAttributes',
            [{'21': 63_018_319.0}],
            11_000,
        ))
        baldwin_hp = parser.process(rpc(
            baldwin_id, 'OnMsgSyncCurrentHp', [37_810_991.4], 12_000,
        ))
        self.assertEqual(
            parser.current_active_boss_state().get('name'),
            '伯德温·威瑟尔',
        )
        self.assertEqual(
            parser.current_active_boss_state().get('max_hp'),
            63_018_319.0,
        )
        self.assertTrue(any(
            kind == 'monster'
            and value.get('entity_id') == baldwin_id
            and value.get('current_hp') == 37_810_991.4
            for kind, value in baldwin_hp
        ))

    def test_live_second_boss_does_not_steal_active_lock(self):
        first_id = ENTITY + 10
        second_id = ENTITY + 11
        base = 1_789_000_100_000_000_000
        parser = NpcapParserAdapter(
            {},
            {
                '7199001': {'name': '首领甲', 'boss_type': 3},
                '7199002': {'name': '首领乙', 'boss_type': 3},
            },
            boss_name_allowlist=('首领甲', '首领乙'),
        )

        def record(entity, method, args, template, offset):
            common = {
                'capture_source': 'npcap',
                'arguments_synchronized': True,
                'network_entity_id': entity,
                'script_entity': entity,
                'capture_timestamp_ns': base + offset,
                'filetime_100ns': (
                    (base + offset) // 100 + 116_444_736_000_000_000
                ),
            }
            if method == 'NpcapEntityCreated':
                return {
                    **common,
                    'method': method,
                    'decoded_arguments': [{
                        'entity_id': entity,
                        'entity_token': f'boss-{entity}',
                        'entity_class': 'NpcActor',
                        'properties': {
                            'TemplateID': template,
                            'Level': 78,
                            'BossType': 3,
                        },
                    }],
                }
            return {**common, 'method': method, 'decoded_arguments': args}

        parser.process(record(first_id, 'NpcapEntityCreated', [], 7_199_001, 0))
        parser.process(record(first_id, 'OnMsgSyncFightMode', [2], 0, 1_000))
        parser.process(record(second_id, 'NpcapEntityCreated', [], 7_199_002, 2_000))
        parser.process(record(second_id, 'OnMsgSyncFightMode', [2], 0, 3_000))

        self.assertEqual(parser.active_boss_entity_id, first_id)
        self.assertEqual(parser.active_boss_pointer, first_id)

    def test_large_streamed_forwarding_wrapper_does_not_block_inner_rpc(self):
        rpc=envelope(22,[{},[123,90,[123,456,789]]])
        metadata=msgpack.packb({b'route':b'world'},use_bin_type=True)
        declared_size=13_108_609
        wrapper=struct.pack('<IH',declared_size,3+(len(metadata)<<8))+metadata
        d=NpcapProtocolDecoder();d._append_application(wrapper+rpc,1,1789310000)
        records=d._rpc_records()
        self.assertEqual([r['method'] for r in records],['OnMsgDamageSyncV2'])
        self.assertLess(len(d.application),declared_size)

    def test_fragmented_forwarding_metadata_waits_without_discarding_bytes(self):
        rpc=envelope(22,[{},[123,90,[123,456,789]]])
        metadata=msgpack.packb({b'route':b'world'},use_bin_type=True)
        wrapper=struct.pack('<IH',13_108_609,3+(len(metadata)<<8))+metadata
        split=8
        d=NpcapProtocolDecoder();d._append_application(wrapper[:split],1,1789310000)
        self.assertEqual(d._rpc_records(),[])
        self.assertEqual(bytes(d.application),wrapper[:split])
        d._append_application(wrapper[split:]+rpc,2,1789310001)
        self.assertEqual([r['method'] for r in d._rpc_records()],['OnMsgDamageSyncV2'])

    def test_unknown_frame_between_rpcs_is_skipped_as_one_complete_frame(self):
        first=envelope(22,[{},[123,19,[2]]])
        unknown=envelope(99,[b'payload',b'contains',b'framing-like-bytes'])
        second=envelope(22,[{},[123,90,[123,456,789]]])
        d=NpcapProtocolDecoder();d._append_application(first+unknown+second,1,1789310000)
        records=d._rpc_records()
        self.assertEqual([r['method'] for r in records],['OnMsgSyncFightMode','OnMsgDamageSyncV2'])
        self.assertEqual(d.diagnostics.unsupported_application_messages,1)

    def test_fragmented_unknown_frame_waits_at_validated_boundary(self):
        first=envelope(22,[{},[123,19,[2]]])
        unknown=envelope(99,[b'x'*128])
        second=envelope(22,[{},[123,90,[123,456,789]]])
        d=NpcapProtocolDecoder();d._append_application(first+unknown[:20],1,1789310000)
        self.assertEqual([r['method'] for r in d._rpc_records()],['OnMsgSyncFightMode'])
        self.assertEqual(bytes(d.application),unknown[:20])
        d._append_application(unknown[20:]+second,2,1789310001)
        self.assertEqual([r['method'] for r in d._rpc_records()],['OnMsgDamageSyncV2'])

    def test_false_large_creation_header_without_msgpack_array_cannot_block_rpc(self):
        false_creation=struct.pack('<IH',8_911_871,11)+b'not-a-creation'
        rpc=envelope(22,[{},[123,90,[123,456,789]]])
        d=NpcapProtocolDecoder();d._append_application(false_creation+rpc,1,1789310000)
        self.assertEqual([r['method'] for r in d._rpc_records()],['OnMsgDamageSyncV2'])

    def test_fragmented_creation_waits_for_full_payload(self):
        d=NpcapProtocolDecoder();data=envelope(11,create())
        d._append_application(data[:15],1,1789310000)
        self.assertEqual(d._rpc_records(),[])
        d._append_application(data[15:],2,1789310001)
        self.assertEqual(len(d._rpc_records()),1)

    def test_unknown_class_not_guessed_as_boss(self):
        obj=decode_creation(11,create(cls=b'Unknown',props={0:7102990}))
        self.assertEqual(obj['properties'],{})
        self.assertFalse(obj['known_property_schema'])

    def test_invalid_numeric_value_rejected(self):
        with self.assertRaises(ValueError):decode_creation(11,create(props={0:True}))

    def test_space_instance_mapping(self):
        d=NpcapProtocolDecoder()
        d._append_application(envelope(11,create(cls=b'DungeonSpace',token=b'space',props={17:b'instance',20:5200275,23:5100069})),1,1789310000)
        p=NpcapParserAdapter();updates=p.process(d._rpc_records()[0])
        self.assertEqual(p.wire_instance_id,'instance')
        self.assertEqual(p.dungeon_id,5100069)
        self.assertTrue(any(k=='instance_context' for k,v in updates))

    def test_last_hunt_uses_its_verified_shifted_space_properties(self):
        decoder = NpcapProtocolDecoder()
        decoder._append_application(envelope(11, create(
            entity=57363585205503, cls=b'PVPLastHuntSpace', token=b'aq0ZR5dVgmeSfQSK',
            props={4:1, 5:0, 8:0, 11:1200001,
                   17:16709800008, 18:b'aq0ZR5dVgmeSfQSK',
                   21:5200167, 24:5200167, 44:16709800008},
        )), 1, 1789732324.883635)
        record = decoder._rpc_records()[0]
        value = record['decoded_arguments'][0]
        self.assertTrue(value['known_property_schema'])
        self.assertEqual(value['properties']['TemplateID'], 5200167)
        parser = NpcapParserAdapter()
        context = next(v for k, v in parser.process(record) if k == 'instance_context')
        self.assertEqual(context['map_id'], 5200167)
        self.assertEqual(context['instance_id'], 'aq0ZR5dVgmeSfQSK')
        observation = parser.take_pvp_observations()[0]
        self.assertEqual(observation['context']['map_id'], 5200167)
        self.assertEqual(observation['context']['instance_id'], 'aq0ZR5dVgmeSfQSK')

    def test_space_creation_clears_stale_stage_but_duplicate_retains_it(self):
        p = NpcapParserAdapter()
        p.wire_instance_id = 'previous-instance'
        p.wire_map_id = 5_200_074
        p.dungeon_stage_id = 5_150_111
        p.dungeon_stage_phase = 1

        def space_record(instance, map_id):
            d = NpcapProtocolDecoder()
            d._append_application(envelope(11, create(
                cls=b'DungeonSpace', token=b'space',
                props={17: instance.encode(), 20: map_id, 23: 5_100_052},
            )), 1, 1789310000)
            return d._rpc_records()[0]

        record = space_record('current-instance', 5_200_052)
        p.process(record)
        self.assertEqual(p.dungeon_stage_id, 0)
        self.assertEqual(p.dungeon_stage_phase, 0)

        p.dungeon_stage_id = 5_150_073
        p.dungeon_stage_phase = 1
        p.process(record)
        self.assertEqual(p.dungeon_stage_id, 5_150_073)
        self.assertEqual(p.dungeon_stage_phase, 1)

        p.process(space_record('another-instance', 5_200_052))
        self.assertEqual(p.dungeon_stage_id, 0)
        self.assertEqual(p.dungeon_stage_phase, 0)

    def test_leave_quest_control_clears_boss_state_and_emits_boundary(self):
        local_actor=ENTITY+100
        p=NpcapParserAdapter()
        p.self_id=local_actor
        p.native_self_id=local_actor
        p.self_confirmed=True
        p.scene_id=5200275
        p.wire_instance_id='instance'
        p.active_boss_entity_id=ENTITY
        p.active_boss_pointer=ENTITY
        p.confirmed_boss_entities.add(ENTITY)
        p.entity_template_ids[ENTITY]=7102990
        p.wire_entity_tokens[ENTITY]='boss'
        p.wire_classes[ENTITY]='NpcActor'
        p.pointer_entities[ENTITY]=ENTITY
        p.combat_mode_pointers.add(ENTITY)
        p.settlement_boss_observation_keys.add(('instance',ENTITY,'boss'))
        p.unconfirmed_hp_activity[ENTITY+1]={'declined':True}
        p.boss_hp_activity_observation_keys.add(('instance',ENTITY,'boss'))

        updates=p.process({
            'method':'OnMsgLeaveQuestControl','capture_source':'npcap',
            'npcap_method_scope':'local_role','arguments_synchronized':True,
            'decoded_arguments':[],'network_entity_id':local_actor,
            'script_entity':local_actor,'filetime_100ns':134338000000000000,
            'capture_timestamp_ns':1789300000000000000,
        })

        scene=next(value for kind,value in updates if kind=='scene')
        self.assertTrue(scene['transition'])
        self.assertEqual(scene['source_method'],'OnMsgLeaveQuestControl')
        self.assertIsNone(p.active_boss_entity_id)
        self.assertFalse(p.confirmed_boss_entities)
        self.assertFalse(p.combat_mode_pointers)
        self.assertIsNone(p.wire_instance_id)
        self.assertIsNone(p.wire_map_id)
        self.assertFalse(p.wire_entity_tokens)
        self.assertFalse(p.wire_classes)
        self.assertFalse(p.settlement_boss_observation_keys)
        self.assertFalse(p.unconfirmed_hp_activity)
        self.assertFalse(p.boss_hp_activity_observation_keys)
        observation=p.completed_settlement_observations.pop()
        self.assertEqual(observation['record']['method'],'OnMsgLeaveQuestControl')

    def test_avatar_creation_rebinds_scene_actor_and_preserves_live_rating(self):
        token='AQAAAOwNsceneAAAA'
        old_actor=57236882400410
        new_actor=57236882400411
        p=NpcapParserAdapter()
        p.party_tokens.add(token)
        p.authoritative_party_tokens.add(token)
        p.party_ids.add(old_actor)
        p.token_actors[token]=old_actor
        p.actor_tokens[old_actor]=token
        p.live_team_property_ratings[token]=88893
        p.team_profile_cache[token]={
            'name':'墨爵','profession_id':1200003,
            'extraordinary_rating':88893,
        }
        p.entity_profiles[old_actor]={
            'name':'墨爵','profession_id':1200003,
            'extraordinary_rating':88893,
        }
        transition={
            'method':'OnMsgBeforeEnterNewSpace','capture_source':'npcap',
            'arguments_synchronized':True,'decoded_arguments':[],
            'network_entity_id':old_actor,'script_entity':old_actor,
            'filetime_100ns':134338000000000000,
            'capture_timestamp_ns':1789300000000000000,
        }
        p.process(transition)
        # A stage table can arrive before the entity creation stream finishes.
        p.stage_bound_tokens.add(token)
        created={
            **transition,
            'method':'NpcapEntityCreated',
            'network_entity_id':new_actor,'script_entity':new_actor,
            'decoded_arguments':[{
                'entity_id':new_actor,'entity_class':'AvatarActor',
                'entity_token':token,
                'properties':{'Name':'墨爵','Profession':1200003},
            }],
        }

        updates=p.process(created)

        merge=next(value for kind,value in updates if kind=='actor_merge')
        self.assertEqual(merge['from_actor_id'],old_actor)
        self.assertEqual(merge['to_actor_id'],new_actor)
        self.assertEqual(p.token_actors[token],new_actor)
        self.assertEqual(p.entity_profiles[new_actor]['extraordinary_rating'],88893)
        self.assertIn(new_actor,p.party_ids)
        self.assertNotIn(old_actor,p.party_ids)

    def test_same_self_token_scene_actor_change_is_not_character_switch(self):
        token='AQAAAOwNselfSceneAA'
        old_actor=57236882400420
        new_actor=57236882400421
        p=NpcapParserAdapter()
        p.self_token=token
        p.self_id=old_actor
        p.native_self_id=old_actor
        p.self_confirmed=True
        p.token_actors[token]=old_actor
        p.actor_tokens[old_actor]=token
        p.live_team_property_ratings[token]=82791
        p.team_profile_cache[token]={
            'name':'莫雪','profession_id':1200002,
            'extraordinary_rating':82791,
        }
        p.entity_profiles[old_actor]={
            'name':'莫雪','profession_id':1200002,
            'extraordinary_rating':82791,
        }
        transition={
            'method':'OnMsgBeforeEnterNewSpace','capture_source':'npcap',
            'arguments_synchronized':True,'decoded_arguments':[],
            'network_entity_id':old_actor,'script_entity':old_actor,
            'filetime_100ns':134338000000000000,
            'capture_timestamp_ns':1789300000000000000,
        }
        p.process(transition)
        p.stage_bound_tokens.add(token)
        created={
            **transition,
            'method':'NpcapEntityCreated',
            'network_entity_id':new_actor,'script_entity':new_actor,
            'decoded_arguments':[{
                'entity_id':new_actor,'entity_class':'AvatarActor',
                'entity_token':token,
                'properties':{'Name':'莫雪','Profession':1200002},
            }],
        }

        updates=p.process(created)

        self.assertFalse(any(kind=='identity_session_reset' for kind,_ in updates))
        self.assertTrue(any(kind=='actor_merge' for kind,_ in updates))
        self.assertTrue(any(kind=='identity' for kind,_ in updates))
        self.assertEqual(p.self_token,token)
        self.assertEqual(p.self_id,new_actor)
        self.assertEqual(p.entity_profiles[new_actor]['extraordinary_rating'],82791)

if __name__=='__main__':unittest.main()
