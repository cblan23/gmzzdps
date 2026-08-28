#!/usr/bin/env python3

from __future__ import annotations

import base64
import unittest

from network_state import (
    NetworkPacketParser,
    parse_combat_entity_id,
    parse_entity_id,
    normalize_network_skill_id,
    profession_from_skill,
    stable_team_actor_id,
)


PLAYER_ID = 57_266_949_828_970
MONSTER_ID = 57_236_882_400_409
MONSTER_POINTER = 65_551_962_096
SELF_TOKEN = "AQAAAOwNKLYHAAAA"
TEAMMATE_TOKEN = "AQAAAOwN0ga-AAAA"
TEAMMATE_ROLE_NUMBER = 816_158_215_660


def packet(method: str, arguments: list, *, pointer: int = 1, sequence: int = 1) -> dict:
    return {
        "method": method,
        "decoded_arguments": arguments,
        "script_entity": pointer,
        "sequence": sequence,
        "filetime_100ns": 134_321_845_085_764_054 + sequence,
        "event_time": "2026-08-26T10:21:48+08:00",
    }


class NetworkPacketParserTests(unittest.TestCase):
    def test_low_combat_entity_ids_are_valid_only_in_explicit_id_fields(self):
        low_boss_id = 4_530_117_319_991
        skill_instance_id = 8_605_101_000_396
        self.assertIsNone(parse_entity_id(low_boss_id))
        self.assertEqual(parse_combat_entity_id(low_boss_id), low_boss_id)
        self.assertIsNone(parse_combat_entity_id(skill_instance_id))

        parser = NetworkPacketParser(
            boss_template_catalog={
                "7115020": {
                    "boss_type": 3,
                    "name": '"剥面人" 强尼',
                    "level": 61,
                }
            }
        )
        profile_updates = parser.process_native_boss_type(
            {
                "filetime_100ns": 134_323_457_734_801_117,
                "entity_id": low_boss_id,
                "template_id": 7_115_020,
                "boss_type": 3,
            }
        )
        profile = next(
            value for kind, value in profile_updates if kind == "profile"
        )
        self.assertEqual(profile["entity_id"], low_boss_id)
        self.assertEqual(profile["name"], '"剥面人" 强尼')
        self.assertEqual(parser.active_boss_entity_id, low_boss_id)

        low_player_id = 4_700_000_000_001
        damage_updates = parser.process_native_damage(
            {
                "filetime_100ns": 134_323_457_924_432_854,
                "attacker_id": low_player_id,
                "target_id": low_boss_id,
                "arg4_u64": skill_instance_id,
                "raw_damage": 8_337,
                "damage": 8_337,
            }
        )
        event = next(value for kind, value in damage_updates if kind == "event")
        self.assertEqual(event["attacker_id"], low_player_id)
        self.assertEqual(event["target_id"], low_boss_id)
        self.assertTrue(event["player_attacker"])
        self.assertIn(low_player_id, parser.combat_source_actors)

    def test_low_id_pool_promotes_every_feedback_boss_template(self):
        samples = (
            (4_530_117_319_991, 7_115_020, '"剥面人" 强尼'),
            (4_689_031_074_259, 7_103_402, "先祖铠甲"),
            (4_642_860_057_279, 7_102_403, "星象仪者"),
        )
        catalog = {
            str(template_id): {"boss_type": 3, "name": name, "level": 62}
            for _entity_id, template_id, name in samples
        }
        for sequence, (entity_id, template_id, name) in enumerate(samples, start=1):
            with self.subTest(name=name):
                parser = NetworkPacketParser(boss_template_catalog=catalog)
                updates = parser.process_native_boss_type(
                    {
                        "filetime_100ns": packet("", [], sequence=sequence)[
                            "filetime_100ns"
                        ],
                        "entity_id": entity_id,
                        "template_id": template_id,
                        "boss_type": 3,
                    }
                )
                profile = next(
                    value for kind, value in updates if kind == "profile"
                )
                self.assertEqual(profile["name"], name)
                self.assertEqual(parser.active_boss_entity_id, entity_id)

    def test_star_guard_is_linked_without_replacing_astrologer(self):
        boss_id = MONSTER_ID + 3_000
        guard_id = MONSTER_ID + 3_001
        guard_pointer = MONSTER_POINTER + 3_001
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {
                    "boss_type": 3,
                    "name": "星象仪者",
                    "level": 65,
                }
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_102_403,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        auxiliary_updates = parser.process_native_boss_type(
            {
                "entity_id": guard_id,
                "template_id": 7_102_405,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        auxiliary = next(
            value for kind, value in auxiliary_updates if kind == "profile"
        )
        self.assertEqual(auxiliary["name"], "星光守卫")
        self.assertTrue(auxiliary["encounter_auxiliary"])
        self.assertEqual(parser.active_boss_entity_id, boss_id)
        self.assertTrue(parser._is_active_encounter_auxiliary(guard_id))

        parser.pointer_entities[guard_pointer] = guard_id
        parser.entity_current_hp[guard_id] = 1_000.0
        parser.token_max_hp[TEAMMATE_TOKEN] = 10_220.0
        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [
                    {
                        "$map": [
                            [0, PLAYER_ID],
                            [2, 86_051_010],
                            [17, guard_id],
                        ]
                    }
                ],
                pointer=90_003,
                sequence=3,
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [PLAYER_ID],
                pointer=guard_pointer,
                sequence=4,
            )
        )
        hp_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [800.0],
                pointer=guard_pointer,
                sequence=5,
            )
        )
        inferred = [
            value
            for kind, value in hp_updates
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        self.assertEqual(len(inferred), 1)
        self.assertEqual(inferred[0]["damage"], 200)
        guard_hp = next(
            value for kind, value in hp_updates if kind == "monster"
        )
        self.assertEqual(guard_hp["entity_id"], guard_id)
        self.assertEqual(guard_hp["current_hp"], 800.0)

        collision = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [10_220.0],
                pointer=guard_pointer,
                sequence=6,
            )
        )
        self.assertFalse(any(kind in {"event", "monster"} for kind, _ in collision))
        self.assertEqual(parser.entity_current_hp[guard_id], 800.0)

    def test_multiphase_boss_hp_drop_restores_team_damage_but_hp_reset_does_not(self):
        """Phase healing is ignored; later real HP loss still carries teammates."""
        boss_id = MONSTER_ID + 3_100
        boss_pointer = MONSTER_POINTER + 3_100
        teammate_id = PLAYER_ID + 3_100
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {
                    "boss_type": 3,
                    "name": "星象仪者",
                    "level": 63,
                }
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_102_403,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)[
                    "filetime_100ns"
                ],
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.entity_current_hp[boss_id] = 33_270_350.0
        parser.entity_max_hp[boss_id] = 33_270_350.0

        def teammate_cast(sequence: int) -> None:
            parser.process(
                packet(
                    "OnMsgCreateLUnitSpellAgent",
                    [
                        {
                            "$map": [
                                [0, teammate_id],
                                [2, 86_051_010],
                                [17, boss_id],
                            ]
                        }
                    ],
                    pointer=99_000 + sequence,
                    sequence=sequence,
                )
            )

        teammate_cast(2)
        first_drop = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [33_000_000.0],
                pointer=boss_pointer,
                sequence=3,
            )
        )
        first_damage = [
            value
            for kind, value in first_drop
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        self.assertEqual(len(first_damage), 1)
        self.assertEqual(first_damage[0]["attacker_id"], teammate_id)
        self.assertEqual(first_damage[0]["damage"], 270_350)

        # A phase reset moves HP upward and must consume pending evidence
        # without inventing negative or carry-over damage.
        teammate_cast(4)
        reset_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [33_270_350.0],
                pointer=boss_pointer,
                sequence=5,
            )
        )
        self.assertFalse(
            any(
                kind == "event" and value["function"].endswith("/team-hit")
                for kind, value in reset_updates
            )
        )

        teammate_cast(6)
        second_drop = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [33_100_000.0],
                pointer=boss_pointer,
                sequence=7,
            )
        )
        second_damage = [
            value
            for kind, value in second_drop
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        self.assertEqual(len(second_damage), 1)
        self.assertEqual(second_damage[0]["damage"], 170_350)

    def test_spawn_owner_binding_keeps_guard_hp_off_astrologer(self):
        boss_id = MONSTER_ID + 3_100
        guard_id = MONSTER_ID + 3_101
        boss_pointer = MONSTER_POINTER + 3_100
        guard_pointer = MONSTER_POINTER + 3_101
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {"boss_type": 3, "name": "星象仪者", "level": 62}
            }
        )

        parser.process(
            packet(
                "OnMsgAddBuffNew",
                [85_000_111, boss_id, boss_id],
                pointer=boss_pointer,
                sequence=1,
            )
        )
        parser.process(
            packet(
                "OnMsgSyncDirtyFightAttributes",
                [{"$map": [[21, 33_270_350.0]]}],
                pointer=boss_pointer,
                sequence=2,
            )
        )
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_102_403,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=3)["filetime_100ns"],
            }
        )
        self.assertEqual(parser.pointer_entities[boss_pointer], boss_id)
        self.assertEqual(parser.entity_max_hp[boss_id], 33_270_350.0)

        parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": boss_id,
                "arg4_u64": 860_600_400,
                "raw_damage": 244,
                "damage": 244,
                "local_player_id": PLAYER_ID,
                "filetime_100ns": packet("", [], sequence=4)["filetime_100ns"],
            }
        )
        parser.process(
            packet(
                "OnMsgSyncFightMode", [2], pointer=boss_pointer, sequence=5
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [PLAYER_ID],
                pointer=boss_pointer,
                sequence=6,
            )
        )
        parser.process(
            packet(
                "OnMsgSyncCurrentMaxHp",
                [33_268_979.0, 33_270_350.0],
                pointer=boss_pointer,
                sequence=7,
            )
        )
        self.assertEqual(parser.active_boss_pointer, boss_pointer)
        self.assertEqual(parser.entity_current_hp[boss_id], 33_268_979.0)

        parser.process(
            packet(
                "OnMsgAddBuffNew",
                [85_000_111, guard_id, guard_id],
                pointer=guard_pointer,
                sequence=8,
            )
        )
        parser.process_native_boss_type(
            {
                "entity_id": guard_id,
                "template_id": 7_102_405,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=9)["filetime_100ns"],
            }
        )
        self.assertEqual(parser.pointer_entities[guard_pointer], guard_id)
        parser.process(
            packet(
                "OnMsgSyncCurrentMaxHp",
                [1_325_792.0, 1_325_792.0],
                pointer=guard_pointer,
                sequence=10,
            )
        )
        parser.process(
            packet("OnMsgEntityDead", [], pointer=guard_pointer, sequence=11)
        )

        self.assertEqual(parser.active_boss_entity_id, boss_id)
        self.assertEqual(parser.entity_current_hp[boss_id], 33_268_979.0)
        self.assertEqual(parser.entity_max_hp[boss_id], 33_270_350.0)
        self.assertEqual(parser.entity_current_hp[guard_id], 0.0)

    def test_native_player_identity_survives_temporary_skill_bar(self):
        parser = NetworkPacketParser()
        transformed_skill = 84_080_713
        normal = parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 860_600_400,
                "raw_damage": 1_000,
                "damage": 1_000,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        normal_event = next(value for kind, value in normal if kind == "event")
        self.assertTrue(normal_event["player_attacker"])

        transformed = parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": transformed_skill,
                "raw_damage": 2_000,
                "damage": 2_000,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        transformed_event = next(
            value for kind, value in transformed if kind == "event"
        )
        self.assertTrue(transformed_event["player_attacker"])

    def test_changed_native_local_entity_merges_temporary_form(self):
        transformed_id = PLAYER_ID + 50_000
        parser = NetworkPacketParser()
        parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 860_600_400,
                "raw_damage": 1_000,
                "damage": 1_000,
                "local_player_id": PLAYER_ID,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        changed = parser.process_native_damage(
            {
                "attacker_id": transformed_id,
                "target_id": MONSTER_ID,
                "arg4_u64": 84_080_713,
                "raw_damage": 2_000,
                "damage": 2_000,
                "local_player_id": transformed_id,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        merge = next(value for kind, value in changed if kind == "actor_merge")
        event = next(value for kind, value in changed if kind == "event")
        self.assertEqual(merge["from_actor_id"], PLAYER_ID)
        self.assertEqual(merge["to_actor_id"], transformed_id)
        self.assertTrue(event["player_attacker"])

    def test_alive_boss_lock_rejects_spawned_add_and_its_hp_pointer(self):
        boss_id = MONSTER_ID + 1_000
        add_id = MONSTER_ID + 1_001
        boss_pointer = MONSTER_POINTER + 1_000
        add_pointer = MONSTER_POINTER + 1_001
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102873": {"boss_type": 3, "name": "小丑", "level": 52},
                "7102892": {
                    "boss_type": 3,
                    "name": "纸人替身-保护",
                    "level": 99,
                },
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_102_873,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.entity_current_hp[boss_id] = 20_000_000.0
        parser.active_boss_pointer = boss_pointer
        parser.pointer_entities[boss_pointer] = boss_id

        parser.process_native_boss_type(
            {
                "entity_id": add_id,
                "template_id": 7_102_892,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, add_id, 86_021_070, 2, 2, 10, 0, 10, False],
                sequence=3,
            )
        )
        parser.pointer_state[add_pointer] = {
            "current_hp": 47_345.0,
            "max_hp": 55_419.0,
        }
        self.assertEqual(parser._bind_pointer(add_pointer, boss_id, packet("", [])), [])
        self.assertEqual(parser.active_boss_entity_id, boss_id)
        self.assertEqual(parser.active_boss_pointer, boss_pointer)
        self.assertNotIn(add_pointer, parser.pointer_entities)
        self.assertEqual(parser.entity_current_hp[boss_id], 20_000_000.0)

    def test_player_buff_source_cannot_capture_evidenced_boss_hp_pointer(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 1_050
        boss_pointer = MONSTER_POINTER + 1_050
        teammate_id = PLAYER_ID + 1_050
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.player_attackers.add(teammate_id)
        parser.process(
            packet("OnMsgSyncFightMode", [2], pointer=boss_pointer, sequence=2)
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [PLAYER_ID],
                pointer=boss_pointer,
                sequence=3,
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [teammate_id],
                pointer=boss_pointer,
                sequence=4,
            )
        )
        parser.process(
            packet(
                "OnMsgSyncDirtyFightAttributes",
                [{"$map": [[21, 5_403_352.0]]}],
                pointer=boss_pointer,
                sequence=5,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgActorBuffStateSync",
                [5, 1, teammate_id],
                pointer=boss_pointer,
                sequence=6,
            )
        )
        self.assertEqual(parser.pointer_entities[boss_pointer], boss_id)
        self.assertEqual(parser.active_boss_pointer, boss_pointer)
        monster = next(value for kind, value in updates if kind == "monster")
        self.assertEqual(monster["entity_id"], boss_id)
        self.assertEqual(monster["max_hp"], 5_403_352.0)

    def test_cached_party_profile_rebinds_unique_combat_actor_name(self):
        token = "AQAAAAEwmC8LAAAA"
        actor_id = 35_257_392_859_799
        parser = NetworkPacketParser(
            {
                token: {
                    "name": "芭芭拉·韦伯",
                    "profession_id": 1_200_006,
                    "level": 62,
                }
            }
        )
        old_actor = parser._bind_team_token(token, stable_team_actor_id(token))
        parser.party_tokens.add(token)
        parser.party_ids.add(old_actor)
        parser.token_max_hp[token] = 10_047.0
        parser.combat_source_actors.add(actor_id)
        parser.actor_profession_hints[actor_id] = 1_200_006
        parser.entity_current_hp[actor_id] = 10_047.0

        updates = parser._team_hp_bindings(packet("", [], sequence=10))
        merge = next(value for kind, value in updates if kind == "actor_merge")
        profile = next(
            value
            for kind, value in updates
            if kind == "profile" and value.get("entity_id") == actor_id
        )
        self.assertEqual(merge["from_actor_id"], old_actor)
        self.assertEqual(merge["to_actor_id"], actor_id)
        self.assertEqual(profile["name"], "芭芭拉·韦伯")
        self.assertEqual(profile["profession_id"], 1_200_006)
        self.assertEqual(parser.token_actors[token], actor_id)
        self.assertEqual(parser.actor_tokens[actor_id], token)

    def test_explicit_boss_death_releases_lock_and_pointer(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 1_100
        boss_pointer = MONSTER_POINTER + 1_100
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.active_boss_pointer = boss_pointer
        parser.pointer_entities[boss_pointer] = boss_id
        parser.entity_current_hp[boss_id] = 1_000.0

        updates = parser.process(
            packet("OnMsgEntityDead", [], pointer=boss_pointer, sequence=2)
        )
        death = next(
            value
            for kind, value in updates
            if kind == "monster" and value.get("entity_id") == boss_id
        )
        self.assertTrue(death["death_confirmed"])
        self.assertEqual(death["current_hp"], 0.0)
        self.assertIsNone(parser.active_boss_entity_id)
        self.assertIsNone(parser.active_boss_pointer)
        self.assertNotIn(boss_pointer, parser.pointer_entities)

    def test_same_scene_full_refresh_clears_active_boss_bindings(self):
        parser = NetworkPacketParser()
        parser.process(packet("OnMsgRefreshSceneObjects", [5_200_002, {}, {}]))
        parser.active_boss_entity_id = MONSTER_ID
        parser.active_boss_pointer = MONSTER_POINTER
        parser.pointer_entities[MONSTER_POINTER] = MONSTER_ID

        updates = parser.process(
            packet("OnMsgRefreshSceneObjects", [5_200_002, {}, {}], sequence=2)
        )
        scene = next(value for kind, value in updates if kind == "scene")
        self.assertTrue(scene["force_reset"])
        self.assertEqual(scene["scene_id"], 5_200_002)
        self.assertEqual(scene["previous_scene_id"], 5_200_002)
        self.assertIsNone(parser.active_boss_entity_id)
        self.assertEqual(parser.pointer_entities, {})

    def test_ancestor_armor_to_baldwin_is_one_phase_locked_boss(self):
        first_id = MONSTER_ID + 1_200
        second_id = MONSTER_ID + 1_201
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7103210": {"boss_type": 3, "name": "先祖铠甲"},
                "7103206": {"boss_type": 3, "name": "伯德温·威瑟尔"},
            }
        )
        for sequence, entity_id, template_id in (
            (1, first_id, 7_103_210),
            (2, second_id, 7_103_206),
        ):
            parser.process_native_boss_type(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "boss_type": 3,
                    "filetime_100ns": packet("", [], sequence=sequence)[
                        "filetime_100ns"
                    ],
                }
            )
        self.assertEqual(parser.active_boss_entity_id, second_id)
        parser.entity_current_hp[first_id] = 5_000_000.0

        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, second_id, 86_021_070, 2, 2, 100, 0, 100, False],
                sequence=3,
            )
        )
        self.assertEqual(parser.active_boss_entity_id, second_id)

    def test_baldwin_to_new_ancestor_requires_real_damage_to_switch(self):
        ancestor_id = MONSTER_ID + 1_210
        baldwin_id = MONSTER_ID + 1_211
        repull_id = MONSTER_ID + 1_212
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7103402": {"boss_type": 3, "name": "先祖铠甲"},
                "7103401": {"boss_type": 3, "name": "伯德温·威瑟尔"},
            },
            boss_name_allowlist=("先祖铠甲", "伯德温·威瑟尔"),
        )
        for sequence, entity_id, template_id in (
            (1, ancestor_id, 7_103_402),
            (2, baldwin_id, 7_103_401),
        ):
            parser.process_native_boss_type(
                {
                    "entity_id": entity_id,
                    "template_id": template_id,
                    "boss_type": 3,
                    "filetime_100ns": packet("", [], sequence=sequence)[
                        "filetime_100ns"
                    ],
                }
            )
        self.assertEqual(parser.active_boss_entity_id, baldwin_id)
        parser.entity_current_hp[baldwin_id] = 36_000_000.0

        parser.process_native_boss_type(
            {
                "entity_id": repull_id,
                "template_id": 7_103_402,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=3)[
                    "filetime_100ns"
                ],
            }
        )
        self.assertEqual(parser.active_boss_entity_id, baldwin_id)
        parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=4)[
                    "filetime_100ns"
                ],
                "attacker_id": PLAYER_ID,
                "target_id": repull_id,
                "arg4_u64": 860100100,
                "damage": 500,
            }
        )
        self.assertEqual(parser.active_boss_entity_id, repull_id)

    def test_entity_id_max_hp_is_rejected_without_losing_team_damage(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 1_220
        boss_pointer = MONSTER_POINTER + 1_220
        attackers = [PLAYER_ID + offset for offset in range(20, 31)]
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)[
                    "filetime_100ns"
                ],
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.entity_current_hp[boss_id] = 6_000_000.0
        parser.entity_max_hp[boss_id] = 36_409_062.0
        for sequence, attacker in enumerate(attackers, start=2):
            parser.process(
                packet(
                    "OnMsgCreateLUnitSpellAgent",
                    [
                        {
                            "$map": [
                                [0, attacker],
                                [2, 86_051_010],
                                [17, boss_id],
                            ]
                        }
                    ],
                    pointer=95_000 + sequence,
                    sequence=sequence,
                )
            )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentMaxHp",
                [5_000_000.0, PLAYER_ID],
                pointer=boss_pointer,
                sequence=20,
            )
        )

        inferred = [
            value
            for kind, value in updates
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        self.assertEqual({item["attacker_id"] for item in inferred}, set(attackers))
        self.assertEqual(sum(item["damage"] for item in inferred), 1_000_000)
        self.assertEqual(parser.entity_max_hp[boss_id], 36_409_062.0)
        monster = next(value for kind, value in updates if kind == "monster")
        self.assertEqual(monster["current_hp"], 5_000_000.0)
        self.assertNotIn("max_hp", monster)

    def test_join_success_and_local_cast_restore_network_self_identity(self):
        parser = NetworkPacketParser()
        teammate_token = "AQAAAOwNTEAMAAAA"
        teammate_profile = {
            "$map": [
                [2, teammate_token],
                [5, "队友"],
                [6, 123_456_789],
                [8, 1_200_003],
                [9, 61],
                [12, 9_000.0],
                [13, 9_000.0],
                [27, 40_000],
            ]
        }
        self_profile = {
            "$map": [
                [2, SELF_TOKEN],
                [5, "莫雪"],
                [6, 33_120_849_388],
                [8, 1_200_002],
                [9, 61],
                [12, 8_536.0],
                [13, 8_536.0],
                [27, 34_356],
            ]
        }
        parser.process(
            packet(
                "OnJoinGroupSuccess",
                [
                    {
                        "$map": [
                            [
                                17,
                                {
                                    "$map": [
                                        [
                                            57_244_934_763_060,
                                            {"$map": [[1, [teammate_profile, self_profile]]]},
                                        ]
                                    ]
                                },
                            ]
                        ]
                    }
                ],
                sequence=1,
            )
        )
        identity_updates = parser.process(
            packet(
                "OnUpdateTeamGroupSelfProps",
                [{"$map": [[11, 34_356], [5, 8_536.0], [6, 8_536.0]]}],
                sequence=2,
            )
        )
        provisional = stable_team_actor_id(SELF_TOKEN, 33_120_849_388)
        identity = next(
            value for kind, value in identity_updates if kind == "identity"
        )
        self.assertEqual(identity["entity_id"], provisional)
        self.assertEqual(parser.self_token, SELF_TOKEN)

        parser.process(
            packet("RetCastSkillSuccessNew", [86_021_030, 10_002_430, 1], sequence=3)
        )
        updates = parser.process(
            packet(
                "OnMsgHealSyncV2",
                [PLAYER_ID, PLAYER_ID + 1, 860_210_300, 215, 0],
                sequence=4,
            )
        )
        merge = next(value for kind, value in updates if kind == "actor_merge")
        self.assertEqual(merge["from_actor_id"], provisional)
        self.assertEqual(merge["to_actor_id"], PLAYER_ID)
        self.assertEqual(parser.self_id, PLAYER_ID)
        self.assertTrue(parser.self_confirmed)
        self.assertEqual(parser.entity_profiles[PLAYER_ID]["name"], "莫雪")

    def test_damage_packet_is_network_combat_event(self):
        parser = NetworkPacketParser()
        updates = parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [
                    PLAYER_ID,
                    MONSTER_ID,
                    8_602_107_000_396,
                    2,
                    2,
                    1702,
                    0,
                    1688,
                    False,
                ],
            )
        )
        self.assertEqual([kind for kind, _value in updates], ["identity", "event"])
        event = updates[-1][1]
        self.assertEqual(event["attacker_id"], PLAYER_ID)
        self.assertEqual(event["target_id"], MONSTER_ID)
        self.assertEqual(event["skill_id"], 86_021_070)
        self.assertEqual(event["damage"], 1688)
        self.assertNotIn("local_player_id", event)

    def test_battle_button_packet_cannot_replace_network_identity(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 100, 0, 100, False],
            )
        )
        updates = parser.process(
            packet(
                "OnMsgBattleButtonInfo",
                [233_341_278_340_510, 1, 1_787_714_905_662],
            )
        )
        self.assertEqual(parser.self_id, PLAYER_ID)
        self.assertFalse(any(kind == "identity" for kind, _value in updates))

    def test_hp_is_bound_to_beaten_packet_target(self):
        parser = NetworkPacketParser()
        self.assertEqual(
            parser.process(
                packet(
                    "OnMsgBeatenSyncV2",
                    [PLAYER_ID, MONSTER_ID, 8_112_107_012],
                    pointer=MONSTER_POINTER,
                )
            ),
            [],
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [563_096.0],
                pointer=MONSTER_POINTER,
                sequence=2,
            )
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], "monster")
        self.assertEqual(updates[0][1]["entity_id"], MONSTER_ID)
        self.assertEqual(updates[0][1]["current_hp"], 563_096.0)

    def test_attribute_21_is_max_hp_for_bound_target(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgBeatenSyncV2",
                [PLAYER_ID, MONSTER_ID, 0],
                pointer=MONSTER_POINTER,
            )
        )
        parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [101_000.0],
                pointer=MONSTER_POINTER,
                sequence=2,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncDirtyFightAttributes",
                [{"$map": [[21, 101_283.0], [161, 2500.0]]}],
                pointer=MONSTER_POINTER,
                sequence=3,
            )
        )
        self.assertEqual(updates[0][1]["max_hp"], 101_283.0)

    def test_damage_root_pointer_is_never_used_for_monster_hp(self):
        parser = NetworkPacketParser()
        root_pointer = 658_001_440
        parser.process(
            packet(
                "OnMsgBeatenSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_112_107_012],
                pointer=root_pointer,
            )
        )
        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 100, 0, 100, False],
                pointer=root_pointer,
                sequence=2,
            )
        )
        parser.process(
            packet(
                "OnMsgActorBuffStateSync",
                [0, 0, PLAYER_ID],
                pointer=root_pointer,
                sequence=3,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [8536.0],
                pointer=root_pointer,
                sequence=4,
            )
        )
        self.assertEqual(updates, [])
        self.assertNotIn(root_pointer, parser.pointer_entities)
        self.assertNotIn(root_pointer, parser.pointer_state)

    def test_current_max_hp_packet_is_retained_until_target_binding(self):
        parser = NetworkPacketParser()
        self.assertEqual(
            parser.process(
                packet(
                    "OnMsgSyncCurrentMaxHp",
                    [500_000.0, 563_096.0],
                    pointer=MONSTER_POINTER,
                    sequence=2,
                )
            ),
            [],
        )
        parser.process(
            packet(
                "OnMsgBeatenSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_112_107_012],
                pointer=MONSTER_POINTER,
                sequence=3,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [478_353.0],
                pointer=MONSTER_POINTER,
                sequence=4,
            )
        )
        merged = {}
        for kind, value in updates:
            if kind == "monster":
                merged.update(value)
        self.assertEqual(merged["entity_id"], MONSTER_ID)
        self.assertEqual(merged["current_hp"], 478_353.0)
        self.assertEqual(merged["max_hp"], 563_096.0)

    def test_guild_hit_dummy_gets_network_scene_profile(self):
        parser = NetworkPacketParser()
        parser.process(packet("OnMsgRefreshSceneObjects", [5_200_021, {}, {}]))
        updates = parser.process(
            packet(
                "OnMsgCreateLUnitSpellField",
                [
                    {
                        "$map": [
                            [9, {"$map": [[0, 2626.044386], [1, 13530.408447], [2, 192.0]]}],
                            [17, MONSTER_ID],
                        ]
                    }
                ],
                sequence=2,
            )
        )
        profile = next(value for kind, value in updates if kind == "profile")
        self.assertEqual(profile["entity_id"], MONSTER_ID)
        self.assertEqual(profile["name"], "公会伤害木桩")
        self.assertEqual(profile["level"], 62)
        self.assertEqual(profile["template_id"], 7_114_225)
        self.assertEqual(profile["boss_type"], 3)
        self.assertEqual(profile["boss_rank"], 3)

    def test_guild_hit_dummy_is_identified_when_capture_starts_mid_scene(self):
        parser = NetworkPacketParser()
        updates = parser.process(
            packet(
                "OnMsgCreateBullet",
                [
                    801_200_011,
                    1_795_692,
                    PLAYER_ID,
                    PLAYER_ID,
                    3600.0,
                    13210.0,
                    337.0,
                    -1.14,
                    161.8,
                    0.51,
                    MONSTER_ID,
                    2626.0,
                    13530.0,
                ],
                sequence=2,
            )
        )
        profile = next(value for kind, value in updates if kind == "profile")
        self.assertEqual(profile["entity_id"], MONSTER_ID)
        self.assertEqual(profile["name"], "公会伤害木桩")
        self.assertEqual(profile["boss_rank"], 3)

    def test_hud_and_music_do_not_promote_a_monster_without_native_type(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgActorBuffStateSync",
                [6, 1, MONSTER_ID],
                pointer=MONSTER_POINTER,
            )
        )
        self.assertEqual(
            parser.process(
                packet(
                    "OnMsgPostAkEvent",
                    [["Set_MUS_Dungeon_Stage_Boss_Example"]],
                    sequence=2,
                )
            ),
            [],
        )
        updates = parser.process(
            packet(
                "OnMsgSetHUDShow",
                [
                    [0, 4, 7],
                    True,
                    1,
                    "AI.BlackThornCase.Dungeon5100006.LevelMap5200206.FatMan_Boss",
                ],
                pointer=MONSTER_POINTER,
                sequence=3,
            )
        )
        self.assertFalse(
            any(
                kind in {"profile", "monster"}
                and value.get("boss_rank") == 3
                for kind, value in updates
            )
        )

    def test_native_boss_type_covers_all_required_dungeons(self):
        dungeons = (
            "黑荆棘",
            "安提哥努斯笔记",
            "记忆的传承",
            "世界灾厄",
            "五月庄园",
        )
        for index, dungeon in enumerate(dungeons):
            with self.subTest(dungeon=dungeon):
                parser = NetworkPacketParser()
                boss_id = MONSTER_ID + index
                updates = parser.process_native_boss_type(
                    {
                        "filetime_100ns": 134_321_845_085_764_054 + index,
                        "event_time": "2026-08-26T10:21:48+08:00",
                        "function": "KAPI_Common_SetBossType",
                        "component": 0xF000_0000 + index * 0x200,
                        "entity_id": boss_id,
                        "boss_type": 3,
                    }
                )
                profile = next(
                    value for kind, value in updates if kind == "profile"
                )
                self.assertEqual(profile["entity_id"], boss_id)
                self.assertEqual(profile["entity_type"], "Boss")
                self.assertEqual(profile["boss_type"], 3)
                self.assertEqual(profile["boss_rank"], 3)
                self.assertEqual(
                    profile["boss_source"], "runtime_common_component"
                )

    def test_native_boss_type_ignores_false_and_same_screen_small_monsters(self):
        parser = NetworkPacketParser()
        small_monster_id = MONSTER_ID + 10
        parser.process(
            packet(
                "OnMsgActorBuffStateSync",
                [6, 1, small_monster_id],
                pointer=MONSTER_POINTER + 10,
            )
        )
        self.assertEqual(
            parser.process_native_boss_type(
                {
                    "entity_id": small_monster_id,
                    "boss_type": 255,
                }
            ),
            [],
        )
        boss_id = MONSTER_ID + 11
        updates = parser.process_native_boss_type(
            {
                "filetime_100ns": 134_321_845_085_764_060,
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )
        self.assertEqual(parser.active_boss_entity_id, boss_id)
        self.assertTrue(
            any(
                kind == "profile" and value.get("entity_id") == boss_id
                for kind, value in updates
            )
        )
        self.assertNotEqual(parser.active_boss_entity_id, small_monster_id)

    def test_exported_boss_template_is_authoritative_over_runtime_type(self):
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7103401": {
                    "boss_type": 3,
                    "name": "伯德温·威瑟尔",
                    "level": 62,
                },
                "7103402": {
                    "boss_type": 0,
                    "name": "普通怪物",
                    "level": 62,
                },
            }
        )
        updates = parser.process_native_boss_type(
            {
                "filetime_100ns": 134_321_845_085_764_060,
                "entity_id": MONSTER_ID,
                "boss_type": 255,
                "template_id": 7_103_401,
            }
        )
        profile = next(value for kind, value in updates if kind == "profile")
        self.assertEqual(profile["name"], "伯德温·威瑟尔")
        self.assertEqual(profile["level"], 62)
        self.assertEqual(profile["boss_type"], 3)
        self.assertEqual(profile["boss_source"], "monster_template_catalog")

        self.assertEqual(
            parser.process_native_boss_type(
                {
                    "entity_id": MONSTER_ID + 1,
                    "boss_type": 3,
                    "template_id": 7_103_402,
                }
            ),
            [],
        )

    def test_production_catalog_mode_fails_closed_when_catalog_is_empty(self):
        parser = NetworkPacketParser(boss_template_catalog={})
        self.assertEqual(
            parser.process_native_boss_type(
                {
                    "entity_id": MONSTER_ID,
                    "boss_type": 3,
                    "template_id": 7_103_401,
                }
            ),
            [],
        )
        self.assertEqual(
            parser.process(
                packet(
                    "OnMsgCreateBullet",
                    [
                        801_200_011,
                        1_795_692,
                        PLAYER_ID,
                        PLAYER_ID,
                        3600.0,
                        13210.0,
                        337.0,
                        -1.14,
                        161.8,
                        0.51,
                        MONSTER_ID,
                        2626.0,
                        13530.0,
                    ],
                )
            ),
            [],
        )

    def test_runtime_name_only_applies_to_network_confirmed_boss(self):
        parser = NetworkPacketParser()
        self.assertIsNone(
            parser.apply_runtime_boss_name(
                {"entity_id": MONSTER_ID, "name": "洛克·金"}
            )
        )
        promotion = parser.process_native_boss_type(
            {
                "filetime_100ns": 134_321_845_085_764_055,
                "entity_id": MONSTER_ID,
                "boss_type": 3,
            }
        )
        profile = next(value for kind, value in promotion if kind == "profile")
        self.assertEqual(profile["name"], "洛克·金")
        update = parser.apply_runtime_boss_name(
            {
                "filetime_100ns": 134_321_845_085_764_056,
                "event_time": "2026-08-26T10:21:48+08:00",
                "function": "KAPI_DataCache_CacheEntityName/cache",
                "entity_id": MONSTER_ID,
                "name": "洛克·金·失控",
            }
        )
        self.assertIsNotNone(update)
        self.assertEqual(update[1]["name"], "洛克·金·失控")
        self.assertIsNone(
            parser.apply_runtime_boss_name(
                {"entity_id": PLAYER_ID, "name": "莫雪"}
            )
        )

    def test_native_damage_is_emitted_without_inferring_local_identity(self):
        parser = NetworkPacketParser()
        updates = parser.process_native_damage(
            {
                "filetime_100ns": 134_321_845_085_764_054,
                "event_time": "2026-08-26T10:21:48+08:00",
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 8_602_107_000_396,
                "arg5_i32": 2,
                "arg6_i32": 2,
                "raw_damage": 1702,
                "arg8_i32": 0,
                "damage": 1688,
                "arg10_bool": False,
            }
        )
        self.assertEqual([kind for kind, _value in updates], ["event"])
        self.assertIsNone(parser.self_id)
        self.assertEqual(updates[0][1]["skill_id"], 86_021_070)
        self.assertEqual(updates[0][1]["damage"], 1688)

        script_updates = parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 1702, 0, 1688, False],
            ),
            include_damage=False,
        )
        self.assertEqual([kind for kind, _value in script_updates], ["identity"])

    def test_native_local_player_id_confirms_identity(self):
        parser = NetworkPacketParser()
        updates = parser.process_native_damage(
            {
                "filetime_100ns": 134_321_845_085_764_054,
                "attacker_id": PLAYER_ID + 1,
                "target_id": MONSTER_ID,
                "arg4_u64": 8_605_101_000_396,
                "raw_damage": 100,
                "damage": 100,
                "local_player_id": PLAYER_ID,
            }
        )
        identity = next(value for kind, value in updates if kind == "identity")
        self.assertEqual(identity["entity_id"], PLAYER_ID)
        self.assertEqual(parser.self_id, PLAYER_ID)
        self.assertTrue(parser.self_confirmed)

    def test_remembered_network_identity_binds_on_first_local_damage(self):
        parser = NetworkPacketParser(
            team_profile_cache={
                SELF_TOKEN: {
                    "name": "莫雪",
                    "profession_id": 1_200_002,
                    "level": 62,
                }
            },
            remembered_self_token=SELF_TOKEN,
        )
        updates = parser.process_native_damage(
            {
                "filetime_100ns": 134_321_845_085_764_054,
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 8_602_107_000_396,
                "raw_damage": 100,
                "damage": 100,
                "local_player_id": PLAYER_ID,
            }
        )

        self.assertEqual(parser.self_token, SELF_TOKEN)
        self.assertEqual(parser.actor_tokens[PLAYER_ID], SELF_TOKEN)
        profile = next(
            value
            for kind, value in updates
            if kind == "profile" and value["entity_id"] == PLAYER_ID
        )
        self.assertEqual(profile["name"], "莫雪")
        self.assertEqual(
            parser.current_self_identity(),
            {
                "user_token": SELF_TOKEN,
                "name": "莫雪",
                "profession_id": 1_200_002,
                "level": 62,
            },
        )

    def test_remembered_network_identity_requires_matching_profession(self):
        parser = NetworkPacketParser(
            team_profile_cache={
                SELF_TOKEN: {
                    "name": "莫雪",
                    "profession_id": 1_200_003,
                }
            },
            remembered_self_token=SELF_TOKEN,
        )
        updates = parser.process_native_damage(
            {
                "filetime_100ns": 134_321_845_085_764_054,
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 8_602_107_000_396,
                "raw_damage": 100,
                "damage": 100,
                "local_player_id": PLAYER_ID,
            }
        )

        self.assertIsNone(parser.self_token)
        self.assertFalse(any(kind == "profile" for kind, _value in updates))

    def test_self_identity_survives_leave_and_accepts_new_network_token(self):
        replacement_token = "AQAAAOwNnewtoken"
        parser = NetworkPacketParser(
            team_profile_cache={
                SELF_TOKEN: {"name": "莫雪", "profession_id": 1_200_002},
                replacement_token: {
                    "name": "新角色",
                    "profession_id": 1_200_002,
                },
            },
            remembered_self_token=SELF_TOKEN,
        )
        parser.process_native_damage(
            {
                "filetime_100ns": 134_321_845_085_764_054,
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 8_602_107_000_396,
                "raw_damage": 100,
                "damage": 100,
                "local_player_id": PLAYER_ID,
            }
        )
        parser.process(packet("OnMsgSelfLeaveTeamGroup", [], sequence=2))

        self.assertEqual(parser.self_token, SELF_TOKEN)
        self.assertEqual(parser.actor_tokens[PLAYER_ID], SELF_TOKEN)

        parser.process(
            packet(
                "OnMsgDungeonReadinessCheck",
                [0, 5_100_054, "", 1_787_851_808, 1, 1],
                sequence=3,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgDungeonReadinessCheck",
                [4, 5_100_054, replacement_token, 0, 1, 1],
                sequence=4,
            )
        )

        self.assertEqual(parser.self_token, replacement_token)
        self.assertNotIn(SELF_TOKEN, parser.token_actors)
        profile = next(
            value
            for kind, value in updates
            if kind == "profile" and value["entity_id"] == PLAYER_ID
        )
        self.assertEqual(profile["name"], "新角色")

    def test_team_life_packets_emit_bound_actor_states(self):
        parser = NetworkPacketParser()
        teammate_actor = stable_team_actor_id(TEAMMATE_TOKEN)
        parser.self_id = PLAYER_ID
        parser.self_token = SELF_TOKEN
        parser.self_confirmed = True
        parser.token_actors = {
            SELF_TOKEN: PLAYER_ID,
            TEAMMATE_TOKEN: teammate_actor,
        }
        parser.actor_tokens = {
            PLAYER_ID: SELF_TOKEN,
            teammate_actor: TEAMMATE_TOKEN,
        }
        parser.party_tokens = {TEAMMATE_TOKEN}
        parser.party_ids = {teammate_actor}

        self_life = next(
            value
            for kind, value in parser.process(
                packet(
                    "OnUpdateTeamGroupSelfProps",
                    [{"$map": [[4, False], [5, 10_000.0], [6, 10_000.0]]}],
                    sequence=20,
                )
            )
            if kind == "life"
        )
        self.assertEqual(self_life["actor_id"], PLAYER_ID)
        self.assertFalse(self_life["dead"])

        teammate_life = next(
            value
            for kind, value in parser.process(
                packet(
                    "OnUpdateTeamGroupMemberProps",
                    [
                        TEAMMATE_TOKEN,
                        {"$map": [[4, True], [5, 0.0], [6, 12_000.0]]},
                    ],
                    sequence=21,
                )
            )
            if kind == "life"
        )
        self.assertEqual(teammate_life["actor_id"], teammate_actor)
        self.assertTrue(teammate_life["dead"])

        revived = next(
            value
            for kind, value in parser.process(
                packet(
                    "OnMsgEntityRelive",
                    [103, {}, {}, TEAMMATE_TOKEN],
                    sequence=22,
                )
            )
            if kind == "life"
        )
        self.assertFalse(revived["dead"])

    def test_non_token_ui_strings_are_not_team_members(self):
        self.assertEqual(NetworkPacketParser._team_token("onChat"), "")

    def test_unbound_hp_never_falls_back_to_a_recent_target(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 100, 0, 100, False],
                pointer=1234,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [478_353.0],
                pointer=MONSTER_POINTER,
                sequence=2,
            )
        )
        self.assertEqual(updates, [])
        self.assertNotIn(MONSTER_POINTER, parser.pointer_entities)

    def test_single_team_statistics_profile_binds_to_self(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 100, 0, 100, False],
            )
        )
        updates = parser.process(
            packet(
                "RetDirtyCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {
                            "$map": [
                                [0, SELF_TOKEN],
                                [1, 61],
                                [3, 1_200_002],
                                [4, "莫雪"],
                                [5, 353_750],
                            ]
                        }
                    }
                ],
                sequence=2,
            )
        )
        profile = next(value for kind, value in updates if kind == "profile")
        self.assertEqual(profile["entity_id"], PLAYER_ID)
        self.assertEqual(profile["name"], "莫雪")
        self.assertEqual(profile["level"], 61)
        self.assertEqual(profile["profession_id"], 1_200_002)

    def test_profile_name_and_current_party_come_from_packet_content(self):
        parser = NetworkPacketParser()
        profile_updates = parser.process(
            packet(
                "RetGetRoleProfile",
                [{"$map": [[0, PLAYER_ID], [1, "莫雪"], [2, 61]]}],
            )
        )
        self.assertTrue(
            any(
                kind == "profile"
                and value["entity_id"] == PLAYER_ID
                and value["name"] == "莫雪"
                and value["level"] == 61
                for kind, value in profile_updates
            )
        )
        self.assertEqual(
            parser.process(
                packet(
                    "RetSearchTeamList",
                    [[57_266_949_828_971, 57_266_949_828_972]],
                )
            ),
            [],
        )
        party_updates = parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    57_423_175_876_976,
                    {
                        "$map": [
                            [2, TEAMMATE_TOKEN],
                            [5, "华木尤"],
                            [6, TEAMMATE_ROLE_NUMBER],
                            [8, 1_200_006],
                            [9, 61],
                        ]
                    },
                ],
            )
        )
        teammate_id = stable_team_actor_id(
            TEAMMATE_TOKEN, TEAMMATE_ROLE_NUMBER
        )
        profile = next(value for kind, value in party_updates if kind == "profile")
        self.assertEqual(profile["entity_id"], teammate_id)
        self.assertEqual(profile["name"], "华木尤")
        self.assertEqual(profile["profession_id"], 1_200_006)
        party = next(value for kind, value in party_updates if kind == "party")
        self.assertEqual(
            party["entity_ids"],
            [teammate_id],
        )

    def test_multi_member_team_statistics_emit_absolute_damage(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 100, 0, 100, False],
            )
        )
        parser.process(
            packet(
                "RetCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {
                            "$map": [[1, 61], [3, 1_200_002], [4, "莫雪"]]
                        }
                    }
                ],
                sequence=2,
            )
        )
        parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    57_423_175_876_976,
                    {
                        "$map": [
                            [2, TEAMMATE_TOKEN],
                            [5, "华木尤"],
                            [6, TEAMMATE_ROLE_NUMBER],
                            [8, 1_200_006],
                            [9, 61],
                        ]
                    },
                ],
                sequence=3,
            )
        )
        updates = parser.process(
            packet(
                "RetDirtyCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {
                            "$map": [[1, 61], [3, 1_200_002], [4, "莫雪"], [5, 370_902]]
                        },
                        TEAMMATE_TOKEN: {
                            "$map": [[1, 61], [3, 1_200_006], [4, "华木尤"], [5, 1_162_830]]
                        },
                    }
                ],
                sequence=4,
            )
        )
        team_stats = {
            value["actor_id"]: value["absolute_damage"]
            for kind, value in updates
            if kind == "team_stat"
        }
        self.assertEqual(team_stats[PLAYER_ID], 370_902)
        self.assertEqual(
            team_stats[-TEAMMATE_ROLE_NUMBER],
            1_162_830,
        )

    def test_stage_statistics_bind_twelve_damage_actors_to_packet_names(self):
        parser = NetworkPacketParser()
        parser.self_id = PLAYER_ID
        parser.self_confirmed = True
        tokens = [f"stage-token-{index}" for index in range(12)]
        actors = [PLAYER_ID + index for index in range(12)]
        actors[5] = 8_957_694_780_510
        profiles = {}
        for index, (token, actor_id) in enumerate(zip(tokens, actors)):
            damage = 0 if index in {1, 9} else (index + 1) * 10_000
            profiles[token] = {
                "$map": [
                    [0, token],
                    [1, actor_id],
                    [2, 62],
                    [4, 1_200_001 + index % 7],
                    [5, f"队员{index + 1}"],
                    [6, damage],
                    [33, {86_010_010: 2}],
                    [34, {86_010_010: damage}],
                ]
            }
        updates = parser.process(
            packet(
                "OnMsgUpdateStageCombatStatistics",
                [
                    {
                        "$map": [
                            [0, 5_150_058],
                            [1, 1],
                            [2, "stage-instance"],
                            [5, profiles],
                        ]
                    }
                ],
                sequence=30,
            )
        )

        party = next(value for kind, value in updates if kind == "party")
        self.assertEqual(party["member_count"], 12)
        self.assertEqual(len(party["entity_ids"]), 11)
        self.assertEqual(parser.self_token, tokens[0])
        self.assertEqual(parser.token_actors[tokens[5]], actors[5])
        low_actor_profile = next(
            value
            for kind, value in updates
            if kind == "profile" and value["entity_id"] == actors[5]
        )
        self.assertEqual(low_actor_profile["name"], "队员6")
        self.assertEqual(low_actor_profile["profession_id"], 1_200_006)
        summary = next(value for kind, value in updates if kind == "stage_summary")
        self.assertEqual(summary["member_count"], 12)
        self.assertEqual(
            sum(row["damage"] > 0 for row in summary["actors"]),
            10,
        )
        self.assertEqual(
            parser.team_profile_cache[tokens[11]]["name"],
            "队员12",
        )

    def test_unique_profession_rebinds_self_token_without_local_name(self):
        parser = NetworkPacketParser()
        parser.self_id = PLAYER_ID
        parser.self_confirmed = True
        parser.actor_profession_hints[PLAYER_ID] = 1_200_002
        updates = parser.process(
            packet(
                "RetCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {
                            "$map": [[3, 1_200_002], [4, "莫雪"], [5, 100]]
                        },
                        TEAMMATE_TOKEN: {
                            "$map": [[3, 1_200_006], [4, "队友"], [5, 200]]
                        },
                    }
                ],
            )
        )
        self.assertEqual(parser.self_token, SELF_TOKEN)
        self.assertEqual(parser.token_actors[SELF_TOKEN], PLAYER_ID)
        party = [value for kind, value in updates if kind == "party"][-1]
        self.assertNotIn(PLAYER_ID, party["entity_ids"])
        self.assertEqual(party["member_count"], 2)

    def test_sparse_team_statistics_keep_omitted_member_damage(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "RetCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {"$map": [[4, "莫雪"], [5, 100_000]]},
                        TEAMMATE_TOKEN: {"$map": [[4, "队友"], [5, 200_000]]},
                    }
                ],
            )
        )
        updates = parser.process(
            packet(
                "RetDirtyCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {"$map": [[4, "莫雪"]]},
                        TEAMMATE_TOKEN: {"$map": [[4, "队友"], [5, 250_000]]},
                    }
                ],
                sequence=2,
            )
        )
        team_stats = [
            value for kind, value in updates if kind == "team_stat"
        ]
        self.assertEqual(len(team_stats), 1)
        self.assertEqual(team_stats[0]["user_token"], TEAMMATE_TOKEN)
        self.assertEqual(team_stats[0]["absolute_damage"], 250_000)

        explicit_zero = parser.process(
            packet(
                "RetDirtyCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {"$map": [[4, "莫雪"], [5, 0]]},
                        TEAMMATE_TOKEN: {"$map": [[4, "队友"]]},
                    }
                ],
                sequence=3,
            )
        )
        zero_stats = [
            value for kind, value in explicit_zero if kind == "team_stat"
        ]
        self.assertEqual(len(zero_stats), 1)
        self.assertEqual(zero_stats[0]["user_token"], SELF_TOKEN)
        self.assertEqual(zero_stats[0]["absolute_damage"], 0)

    def test_self_leave_team_clears_current_member_list(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    57_423_175_876_976,
                    {
                        "$map": [
                            [2, TEAMMATE_TOKEN],
                            [5, "华木尤"],
                            [6, TEAMMATE_ROLE_NUMBER],
                        ]
                    },
                ],
            )
        )
        updates = parser.process(
            packet("OnMsgSelfLeaveTeamGroup", [], sequence=2)
        )
        party = next(value for kind, value in updates if kind == "party")
        self.assertEqual(party["entity_ids"], [])
        self.assertTrue(party["authoritative"])

    def test_rejoin_uses_known_self_name_without_duplicate_party_member(self):
        parser = NetworkPacketParser()
        parser.self_id = PLAYER_ID
        parser.entity_profiles[PLAYER_ID] = {
            "name": "莫雪",
            "entity_type": "Player",
        }
        self_profile = {
            "$map": [
                [2, SELF_TOKEN],
                [5, "莫雪"],
                [6, 33_120_849_388],
                [8, 1_200_002],
                [9, 61],
            ]
        }
        teammate_profile = {
            "$map": [
                [2, TEAMMATE_TOKEN],
                [5, "新队友"],
                [6, TEAMMATE_ROLE_NUMBER],
                [8, 1_200_003],
                [9, 61],
            ]
        }

        updates = parser.process(
            packet(
                "OnJoinGroupSuccess",
                [[self_profile, teammate_profile]],
                sequence=2,
            )
        )

        party = [value for kind, value in updates if kind == "party"][-1]
        self.assertEqual(parser.self_token, SELF_TOKEN)
        self.assertEqual(party["member_count"], 2)
        self.assertEqual(len(party["entity_ids"]), 1)
        self.assertNotIn(PLAYER_ID, party["entity_ids"])
        self.assertNotIn(stable_team_actor_id(SELF_TOKEN), party["entity_ids"])

    def test_unrelated_idle_packets_do_not_clear_party(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    57_423_175_876_976,
                    {
                        "$map": [
                            [2, TEAMMATE_TOKEN],
                            [5, "华木尤"],
                            [6, TEAMMATE_ROLE_NUMBER],
                        ]
                    },
                ],
            )
        )
        later = packet("RetNTP", [1, 2], sequence=2)
        updates = parser.process(later)
        self.assertFalse(any(kind == "party" for kind, _value in updates))
        self.assertEqual(len(parser.party_ids), 1)

    def test_twelve_member_team_join_notifications_have_no_cap(self):
        parser = NetworkPacketParser()
        latest_party = None
        for index in range(11):
            updates = parser.process(
                packet(
                    "OnMsgOtherJoinTeamGroup",
                    [
                        0,
                        57_423_175_876_976,
                        {
                            "$map": [
                                [2, f"team-token-{index}"],
                                [5, f"队员{index + 2}"],
                                [6, 900_000_000_000 + index],
                                [8, 1_200_001 + index % 7],
                                [9, 61],
                            ]
                        },
                    ],
                    sequence=index + 1,
                )
            )
            latest_party = next(
                value for kind, value in updates if kind == "party"
            )
        self.assertIsNotNone(latest_party)
        self.assertEqual(len(latest_party["entity_ids"]), 11)
        self.assertEqual(latest_party["member_count"], 12)

    def test_batch_team_join_preserves_member_profile(self):
        parser = NetworkPacketParser()
        token = "batch-team-token"
        role_number = 29_928_934_579
        updates = parser.process(
            packet(
                "OnMsgMembersJoinTeam",
                [
                    48_634_599_062_654,
                    [
                        {
                            "$map": [
                                [2, token],
                                [5, "\u514b\u55b5\u83b1\u6069"],
                                [6, role_number],
                                [8, 1_200_003],
                                [9, 61],
                                [12, 8_098.0],
                                [13, 8_098.0],
                            ]
                        }
                    ],
                ],
            )
        )
        actor_id = -role_number
        profile = next(
            value for kind, value in updates if kind == "profile"
        )
        self.assertEqual(profile["entity_id"], actor_id)
        self.assertEqual(profile["name"], "\u514b\u55b5\u83b1\u6069")
        self.assertEqual(profile["profession_id"], 1_200_003)
        self.assertEqual(parser.token_max_hp[token], 8_098.0)
        party = next(value for kind, value in updates if kind == "party")
        self.assertIn(actor_id, party["entity_ids"])
        self.assertEqual(party["member_count"], 2)

    def test_projection_roster_rebinds_new_scene_entities_by_embedded_order(self):
        tokens = [
            "apBrk8QJI8woZSsS",
            "apBrk8QJI8woZStw",
            "apBrk8QJI8woZSvL",
            "apBrk8QJI8woZSwr",
        ]
        names = ["花瓣·投影", "旧缘·投影", "流火·投影", "飘飘·投影"]
        professions = [1_200_005, 1_200_005, 1_200_006, 1_200_001]
        cache = {
            token: {
                "name": name,
                "profession_id": profession,
                "level": 62,
            }
            for token, name, profession in zip(tokens, names, professions)
        }
        parser = NetworkPacketParser(team_profile_cache=cache)
        parser.party_tokens = set(tokens)
        parser.party_ids = {
            parser._bind_team_token(token, stable_team_actor_id(token))
            for token in tokens
        }
        actors = [57_238_493_118_587, 57_238_493_118_591,
                  57_238_493_118_592, 57_238_493_118_593]
        parser.combat_source_actors.update(actors)
        parser.actor_profession_hints.update(dict(zip(actors, professions)))

        updates = parser._team_hp_bindings(packet("", [], sequence=20))

        self.assertEqual(
            [parser.token_actors[token] for token in tokens],
            actors,
        )
        profiles = {
            value["entity_id"]: value["name"]
            for kind, value in updates
            if kind == "profile" and "name" in value
        }
        self.assertEqual(profiles, dict(zip(actors, names)))

    def test_full_projection_roster_rebinds_duplicate_professions(self):
        tokens = [
            base64.urlsafe_b64encode(b"projection-roster-" + bytes([index]))
            .rstrip(b"=")
            .decode("ascii")
            for index in range(11)
        ]
        names = [f"投影成员{index + 1}·投影" for index in range(11)]
        professions = [
            1_200_005,
            1_200_005,
            1_200_006,
            1_200_006,
            1_200_001,
            1_200_001,
            1_200_003,
            1_200_003,
            1_200_007,
            1_200_002,
            1_200_002,
        ]
        parser = NetworkPacketParser(
            team_profile_cache={
                token: {
                    "name": name,
                    "profession_id": profession,
                    "level": 62,
                }
                for token, name, profession in zip(
                    tokens, names, professions
                )
            }
        )
        parser.party_tokens = set(tokens)
        old_actors = {
            parser._bind_team_token(token, stable_team_actor_id(token))
            for token in tokens
        }
        parser.party_ids = set(old_actors)
        actors = [57_431_859_001_100 + index for index in range(11)]
        parser.combat_source_actors.update(actors)
        parser.actor_profession_hints.update(
            dict(zip(actors, professions))
        )

        updates = parser._team_hp_bindings(packet("", [], sequence=22))

        self.assertEqual(
            [parser.token_actors[token] for token in tokens], actors
        )
        profiles = {
            value["entity_id"]: value["name"]
            for kind, value in updates
            if kind == "profile" and "name" in value
        }
        self.assertEqual(profiles, dict(zip(actors, names)))
        self.assertEqual(parser.party_ids, set(actors))
        self.assertTrue(old_actors.isdisjoint(parser.party_ids))

    def test_projection_names_are_repaired_by_unique_skill_professions(self):
        tokens = [f"projection-token-{index}" for index in range(6)]
        names = [f"投影成员{index + 1}·投影" for index in range(6)]
        professions = [1_200_001 + index for index in range(6)]
        actors = [57_178_363_545_084 + index for index in range(6)]
        actor_professions = [
            1_200_005,
            1_200_001,
            1_200_006,
            1_200_003,
            1_200_002,
            1_200_004,
        ]
        parser = NetworkPacketParser(
            team_profile_cache={
                token: {
                    "name": name,
                    "profession_id": profession,
                    "level": 63,
                }
                for token, name, profession in zip(tokens, names, professions)
            }
        )
        parser.party_tokens = set(tokens)
        parser.party_ids = set(actors)
        parser.combat_source_actors.update(actors)
        parser.actor_profession_hints.update(
            dict(zip(actors, actor_professions))
        )
        # Reproduce the feedback state: an early order guess attached each
        # token/profile to the wrong positive actor.
        for token, actor_id in zip(tokens, actors):
            parser.token_actors[token] = actor_id
            parser.actor_tokens[actor_id] = token
            parser.entity_profiles[actor_id] = {
                "name": parser.team_profile_cache[token]["name"],
                "profession_id": parser.team_profile_cache[token]["profession_id"],
            }

        updates = parser._team_hp_bindings(packet("", [], sequence=24))

        expected = {
            token: next(
                actor
                for actor, actor_profession in zip(actors, actor_professions)
                if actor_profession == profession
            )
            for token, profession in zip(tokens, professions)
        }
        self.assertEqual(
            {token: parser.token_actors[token] for token in tokens}, expected
        )
        profiles = {
            value["entity_id"]: value
            for kind, value in updates
            if kind == "profile" and "name" in value
        }
        for token, actor_id in expected.items():
            self.assertEqual(
                profiles[actor_id]["name"],
                parser.team_profile_cache[token]["name"],
            )
            self.assertEqual(
                profiles[actor_id]["profession_id"],
                parser.actor_profession_hints[actor_id],
            )

    def test_drill_feedback_projection_names_follow_their_actual_skill_families(self):
        """FB3F3C449BEB21689D: names must not follow entity allocation order."""
        tokens = [f"drill-projection-token-{index}" for index in range(6)]
        names = [
            "飘飘来了·投影",
            "莫雪·投影",
            "墨爵·投影",
            "旧缘·投影",
            "碎星碎星·投影",
            "荡漾丶·投影",
        ]
        professions = [
            1_200_001,
            1_200_002,
            1_200_003,
            1_200_005,
            1_200_006,
            1_200_007,
        ]
        actors = [
            57_409_759_241_038,
            57_178_363_545_090,
            57_178_363_545_088,
            57_178_363_545_084,
            57_178_363_545_087,
            57_178_363_545_089,
        ]
        representative_skills = [
            86_011_071,
            86_021_100,
            86_033_030,
            86_051_080,
            86_061_020,
            86_071_030,
        ]
        parser = NetworkPacketParser(
            team_profile_cache={
                token: {
                    "name": name,
                    "profession_id": profession,
                    "level": 63,
                }
                for token, name, profession in zip(
                    tokens, names, professions
                )
            }
        )
        parser.self_token = tokens[0]
        parser.self_id = stable_team_actor_id(tokens[0])
        parser.self_confirmed = True
        parser.party_tokens = set(tokens[1:])
        parser.other_party_tokens = set(tokens[1:])
        parser.party_member_count = 6

        # This is the circularly shifted binding reported by the client: the
        # damage actors are right, while their cached names/classes are not.
        wrong_actor_order = [actors[1], actors[2], actors[3], actors[4], actors[5], actors[0]]
        for token, actor_id in zip(tokens, wrong_actor_order):
            parser.token_actors[token] = actor_id
            parser.actor_tokens[actor_id] = token
            parser.party_ids.add(actor_id)
            parser.entity_profiles[actor_id] = {
                "name": parser.team_profile_cache[token]["name"],
                "profession_id": parser.team_profile_cache[token]["profession_id"],
            }
        parser.combat_source_actors.update(actors)
        for actor_id, skill_id in zip(actors, representative_skills):
            parser.actor_profession_hints[actor_id] = profession_from_skill(skill_id)

        updates = parser._team_hp_bindings(packet("", [], sequence=25))

        self.assertEqual(
            {token: parser.token_actors[token] for token in tokens},
            dict(zip(tokens, actors)),
        )
        self.assertFalse(
            any(kind == "actor_merge" for kind, _value in updates),
            "two real projection players must never have their damage merged",
        )
        profiles = {
            value["entity_id"]: value
            for kind, value in updates
            if kind == "profile" and "name" in value
        }
        for actor_id, name, profession, skill_id in zip(
            actors, names, professions, representative_skills
        ):
            self.assertEqual(profiles[actor_id]["name"], name)
            self.assertEqual(profiles[actor_id]["profession_id"], profession)
            self.assertEqual(profession_from_skill(skill_id), profession)
        self.assertEqual(parser.self_id, actors[0])
        self.assertNotIn(actors[0], parser.party_ids)
        party = next(value for kind, value in updates if kind == "party")
        self.assertEqual(party["member_count"], 6)
        self.assertNotIn(actors[0], party["entity_ids"])

    def test_mid_scene_start_uses_same_process_packet_roster_for_projections(self):
        def scene_token(counter: int, middle: bytes, index: int) -> str:
            raw = (
                counter.to_bytes(4, "big")
                + middle
                + index.to_bytes(3, "big")
            )
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        professions = [
            1_200_005,
            1_200_005,
            1_200_006,
            1_200_006,
            1_200_001,
            1_200_001,
            1_200_003,
            1_200_003,
            1_200_007,
            1_200_007,
            1_200_002,
        ]
        names = [f"投影成员{index + 1}·投影" for index in range(11)]
        old_tokens = [
            scene_token(0x6A910210, b"old-a", index)
            for index in range(11)
        ]
        current_tokens = [
            scene_token(0x6A910220, b"new-b", index)
            for index in range(11)
        ]
        parser = NetworkPacketParser(
            team_profile_cache={
                token: {
                    "name": name,
                    "profession_id": profession,
                    "level": 63,
                }
                for token, name, profession in zip(
                    old_tokens, names, professions
                )
            },
            allow_cached_projection_roster=True,
        )
        parser.party_tokens = set(current_tokens)
        old_actors = {
            parser._bind_team_token(token, stable_team_actor_id(token))
            for token in current_tokens
        }
        parser.party_ids = set(old_actors)
        actors = [57_391_501_253_254 + index for index in range(11)]
        parser.combat_source_actors.update(actors)
        parser.actor_profession_hints.update(
            dict(zip(actors, professions))
        )

        updates = parser._team_hp_bindings(packet("", [], sequence=23))

        profiles = {
            value["entity_id"]: value["name"]
            for kind, value in updates
            if kind == "profile" and "name" in value
        }
        self.assertEqual(profiles, dict(zip(actors, names)))
        self.assertEqual(parser.party_ids, set(actors))
        self.assertTrue(old_actors.isdisjoint(parser.party_ids))
        self.assertTrue(parser.team_profile_cache_dirty)

    def test_ordered_binding_is_not_used_for_ordinary_team(self):
        tokens = ["AQAAAOwN8l5GAAAA", "AQAAAOwNKLYHAAAA"]
        parser = NetworkPacketParser(
            team_profile_cache={
                tokens[0]: {"name": "队员甲", "profession_id": 1_200_003},
                tokens[1]: {"name": "队员乙", "profession_id": 1_200_003},
            }
        )
        parser.party_tokens = set(tokens)
        old_actors = [stable_team_actor_id(token) for token in tokens]
        for token, actor_id in zip(tokens, old_actors):
            parser._bind_team_token(token, actor_id)
        parser.combat_source_actors.update([PLAYER_ID + 30, PLAYER_ID + 31])
        parser.actor_profession_hints.update(
            {PLAYER_ID + 30: 1_200_003, PLAYER_ID + 31: 1_200_003}
        )

        updates = parser._team_hp_bindings(packet("", [], sequence=21))

        self.assertFalse(any(kind == "actor_merge" for kind, _value in updates))
        self.assertEqual([parser.token_actors[token] for token in tokens], old_actors)

    def test_single_real_player_readiness_binds_cached_local_name(self):
        parser = NetworkPacketParser(
            team_profile_cache={
                SELF_TOKEN: {
                    "name": "碎星碎星",
                    "profession_id": 1_200_006,
                    "level": 62,
                }
            }
        )
        parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 860_600_400,
                "damage": 10,
                "raw_damage": 10,
                "local_player_id": PLAYER_ID,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.process(
            packet(
                "OnMsgDungeonReadinessCheck",
                [0, 5_100_054, "", 1_787_851_808, 1, 1],
                sequence=2,
            )
        )

        updates = parser.process(
            packet(
                "OnMsgDungeonReadinessCheck",
                [4, 5_100_054, SELF_TOKEN, 0, 1, 1],
                sequence=3,
            )
        )

        self.assertEqual(parser.self_token, SELF_TOKEN)
        self.assertEqual(parser.token_actors[SELF_TOKEN], PLAYER_ID)
        profile = next(value for kind, value in updates if kind == "profile")
        self.assertEqual(profile["entity_id"], PLAYER_ID)
        self.assertEqual(profile["name"], "碎星碎星")

    def test_multi_player_readiness_does_not_guess_local_token(self):
        parser = NetworkPacketParser()
        parser.self_id = PLAYER_ID
        parser.process(
            packet(
                "OnMsgDungeonReadinessCheck",
                [0, 5_100_003, "", 1_787_851_808, 6, 1],
                sequence=1,
            )
        )

        updates = parser.process(
            packet(
                "OnMsgDungeonReadinessCheck",
                [4, 5_100_003, TEAMMATE_TOKEN, 0, 6, 1],
                sequence=2,
            )
        )

        self.assertIsNone(parser.self_token)
        self.assertFalse(any(kind == "identity" for kind, _value in updates))

    def test_authoritative_six_member_roster_merges_late_self_entity(self):
        parser = NetworkPacketParser()
        teammate_tokens = [f"team-token-{index}" for index in range(5)]
        entries = {
            SELF_TOKEN: {
                "$map": [[1, 61], [3, 1_200_002], [4, "莫雪"], [5, 100]]
            }
        }
        for index, token in enumerate(teammate_tokens):
            entries[token] = {
                "$map": [
                    [1, 61],
                    [3, 1_200_001 + index],
                    [4, f"队员{index + 1}"],
                    [5, 0],
                ]
            }
        initial = parser.process(
            packet("RetCommonCombatStatisticsByTeam", [entries], sequence=1)
        )
        initial_party = next(value for kind, value in initial if kind == "party")
        self.assertEqual(initial_party["member_count"], 6)
        self.assertEqual(len(initial_party["entity_ids"]), 6)

        for sequence, token in enumerate(teammate_tokens, start=2):
            parser.process(
                packet(
                    "OnSyncTeamGroupPropsForceRefresh",
                    [token, None, None, 0, 0, 0, False],
                    sequence=sequence,
                )
            )

        updates = parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 100, 0, 100, False],
                sequence=10,
            )
        )
        merge = next(value for kind, value in updates if kind == "actor_merge")
        old_actor = stable_team_actor_id(SELF_TOKEN)
        self.assertEqual(merge["from_actor_id"], old_actor)
        self.assertEqual(merge["to_actor_id"], PLAYER_ID)
        self.assertEqual(parser.self_token, SELF_TOKEN)
        self.assertTrue(parser.self_confirmed)

        self_profile = next(
            value
            for kind, value in updates
            if kind == "profile" and value["entity_id"] == PLAYER_ID
        )
        self.assertEqual(self_profile["name"], "莫雪")
        self.assertEqual(self_profile["profession_id"], 1_200_002)
        party = next(value for kind, value in updates if kind == "party")
        self.assertEqual(party["member_count"], 6)
        self.assertEqual(len(party["entity_ids"]), 5)
        self.assertNotIn(old_actor, party["entity_ids"])

    def test_scene_change_rebinds_team_token_to_new_combat_entity(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 100, 0, 100, False],
            )
        )
        parser.process(
            packet(
                "RetCommonCombatStatisticsByTeam",
                [{SELF_TOKEN: {"$map": [[3, 1_200_002], [5, 100]]}}],
                sequence=2,
            )
        )
        parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    57_423_175_876_976,
                    {
                        "$map": [
                            [2, TEAMMATE_TOKEN],
                            [6, TEAMMATE_ROLE_NUMBER],
                            [8, 1_200_003],
                            [9, 61],
                        ]
                    },
                ],
                sequence=3,
            )
        )

        old_actor = PLAYER_ID + 101
        new_actor = PLAYER_ID + 202
        parser.process(
            packet("OnMsgRefreshSceneObjects", [5_200_001, {}, {}], sequence=4)
        )
        parser.process(
            packet(
                "OnMsgCreateLUnitSpellField",
                [{"$map": [[0, old_actor], [2, 86_030_010]]}],
                pointer=10_001,
                sequence=5,
            )
        )
        self.assertEqual(parser.token_actors[TEAMMATE_TOKEN], old_actor)

        scene_updates = parser.process(
            packet("OnMsgRefreshSceneObjects", [5_200_002, {}, {}], sequence=6)
        )
        self.assertTrue(any(kind == "scene" for kind, _value in scene_updates))
        updates = parser.process(
            packet(
                "OnMsgCreateLUnitSpellField",
                [{"$map": [[0, new_actor], [2, 86_030_010]]}],
                pointer=10_002,
                sequence=7,
            )
        )
        merge = next(value for kind, value in updates if kind == "actor_merge")
        self.assertEqual(merge["from_actor_id"], old_actor)
        self.assertEqual(merge["to_actor_id"], new_actor)
        self.assertEqual(parser.token_actors[TEAMMATE_TOKEN], new_actor)
        self.assertIn(new_actor, parser.party_ids)
        self.assertNotIn(old_actor, parser.party_ids)

    def test_boss_hp_drop_is_shared_only_between_observed_hitters(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 500
        boss_pointer = MONSTER_POINTER + 500
        attacker_a = PLAYER_ID + 10
        attacker_b = PLAYER_ID + 20
        healer_without_hits = PLAYER_ID + 30
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
                "template_id": 7_102_834,
            }
        )
        for sequence, attacker in enumerate((attacker_a, attacker_b), start=2):
            parser.process(
                packet(
                    "OnMsgCreateLUnitSpellAgent",
                    [{"$map": [[0, attacker], [2, 86_051_010], [17, boss_id]]}],
                    pointer=90_000 + sequence,
                    sequence=sequence,
                )
            )
        parser.process(
            packet("OnMsgSyncFightMode", [2], pointer=boss_pointer, sequence=4)
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [attacker_a],
                pointer=boss_pointer,
                sequence=5,
            )
        )
        parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [1_000.0],
                pointer=boss_pointer,
                sequence=6,
            )
        )
        for sequence, attacker in enumerate(
            (attacker_a, attacker_a, attacker_b), start=7
        ):
            parser.process(
                packet(
                    "OnMsgEndureExitHit",
                    [attacker],
                    pointer=boss_pointer,
                    sequence=sequence,
                )
            )
        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, boss_id, 8_602_107_000_396, 2, 2, 100, 0, 100, False],
                sequence=10,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [600.0],
                pointer=boss_pointer,
                sequence=11,
            )
        )
        inferred = [
            value
            for kind, value in updates
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        by_actor = {}
        for event in inferred:
            by_actor[event["attacker_id"]] = (
                by_actor.get(event["attacker_id"], 0) + event["damage"]
            )
        self.assertEqual(by_actor, {attacker_a: 200, attacker_b: 100})
        self.assertNotIn(healer_without_hits, by_actor)

    def test_duel_npc_hit_cannot_receive_inferred_boss_damage(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 525
        boss_pointer = MONSTER_POINTER + 525
        npc_id = MONSTER_ID + 526
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)[
                    "filetime_100ns"
                ],
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [1_000.0],
                pointer=boss_pointer,
                sequence=2,
            )
        )
        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [{"$map": [[0, npc_id], [2, 88_007_440]]}],
                pointer=90_525,
                sequence=3,
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [npc_id],
                pointer=boss_pointer,
                sequence=4,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [900.0],
                pointer=boss_pointer,
                sequence=5,
            )
        )

        self.assertFalse(
            any(
                kind == "event"
                and value.get("attacker_id") == npc_id
                for kind, value in updates
            )
        )

    def test_directed_boss_casts_restore_hitters_missing_from_hit_callback(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 550
        boss_pointer = MONSTER_POINTER + 550
        attackers = [PLAYER_ID + offset for offset in (10, 20, 30)]
        skill_id = 86_051_010
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [1_000.0],
                pointer=boss_pointer,
                sequence=2,
            )
        )
        for sequence, attacker in enumerate(attackers, start=3):
            parser.process(
                packet(
                    "OnMsgCreateLUnitSpellAgent",
                    [
                        {
                            "$map": [
                                [0, attacker],
                                [2, skill_id],
                                [17, boss_id],
                            ]
                        }
                    ],
                    pointer=91_000 + sequence,
                    sequence=sequence,
                )
            )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [400.0],
                pointer=boss_pointer,
                sequence=8,
            )
        )
        inferred = [
            value
            for kind, value in updates
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        self.assertEqual(
            {value["attacker_id"] for value in inferred}, set(attackers)
        )
        self.assertEqual(sum(value["damage"] for value in inferred), 600)

    def test_twelve_directed_boss_attackers_survive_partial_native_callbacks(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 551
        boss_pointer = MONSTER_POINTER + 551
        attackers = [PLAYER_ID + offset for offset in range(101, 113)]
        skill_id = 86_051_010
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [2_000.0],
                pointer=boss_pointer,
                sequence=2,
            )
        )
        all_updates = []
        for sequence, attacker in enumerate(attackers, start=3):
            all_updates.extend(
                parser.process(
                    packet(
                        "OnMsgCreateLUnitSpellAgent",
                        [
                            {
                                "$map": [
                                    [0, attacker],
                                    [2, skill_id],
                                    [17, boss_id],
                                ]
                            }
                        ],
                        pointer=92_000 + sequence,
                        sequence=sequence,
                    )
                )
            )
        for sequence, attacker in enumerate(attackers[:6], start=15):
            all_updates.extend(
                parser.process_native_damage(
                    {
                        "filetime_100ns": packet("", [], sequence=sequence)[
                            "filetime_100ns"
                        ],
                        "attacker_id": attacker,
                        "target_id": boss_id,
                        "arg4_u64": 8_605_101_000_396,
                        "raw_damage": 10,
                        "damage": 10,
                    }
                )
            )
        all_updates.extend(
            parser.process(
                packet(
                    "OnMsgSyncCurrentHp",
                    [800.0],
                    pointer=boss_pointer,
                    sequence=22,
                )
            )
        )
        damage_events = [
            value for kind, value in all_updates if kind == "event"
        ]
        self.assertEqual(
            {value["attacker_id"] for value in damage_events}, set(attackers)
        )
        self.assertEqual(sum(value["damage"] for value in damage_events), 1_200)

    def test_runtime_name_recovers_boss_when_capture_starts_mid_map(self):
        parser = NetworkPacketParser(
            boss_template_catalog={},
            boss_name_allowlist=("星象仪者", "伤害木桩"),
        )
        parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 860100100,
                "damage": 1_000,
            }
        )
        update = parser.apply_runtime_boss_name(
            {
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
                "entity_id": MONSTER_ID,
                "name": "星象仪者",
            }
        )
        self.assertIsNotNone(update)
        self.assertEqual(parser.active_boss_entity_id, MONSTER_ID)
        self.assertEqual(update[1]["boss_rank"], 3)

    def test_cached_runtime_name_activates_on_first_mid_map_damage(self):
        target_id = 4_642_860_057_279
        parser = NetworkPacketParser(
            boss_template_catalog={},
            boss_name_allowlist=("星象仪者", "伤害木桩"),
        )
        self.assertIsNone(
            parser.apply_runtime_boss_name(
                {
                    "filetime_100ns": packet("", [], sequence=1)[
                        "filetime_100ns"
                    ],
                    "entity_id": target_id,
                    "name": "星象仪者",
                }
            )
        )
        updates = parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=2)[
                    "filetime_100ns"
                ],
                "attacker_id": PLAYER_ID,
                "target_id": target_id,
                "arg4_u64": 860100100,
                "damage": 1_000,
            }
        )
        profile = next(value for kind, value in updates if kind == "profile")
        event = next(value for kind, value in updates if kind == "event")
        self.assertEqual(parser.active_boss_entity_id, target_id)
        self.assertEqual(profile["name"], "星象仪者")
        self.assertEqual(event["damage"], 1_000)

    def test_direct_damage_switches_dummy_lock_to_catalog_boss(self):
        dummy_id = MONSTER_ID
        boss_id = MONSTER_ID + 1
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7114225": {
                    "boss_type": 3,
                    "name": "伤害木桩",
                    "level": 62,
                },
                "7103402": {
                    "boss_type": 3,
                    "name": "先祖铠甲",
                    "level": 62,
                },
            },
            boss_name_allowlist=("伤害木桩", "先祖铠甲"),
        )
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)[
                    "filetime_100ns"
                ],
                "entity_id": dummy_id,
                "template_id": 7_114_225,
                "boss_type": 3,
            }
        )
        parser.entity_current_hp[dummy_id] = 125_548_490.0
        parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=2)[
                    "filetime_100ns"
                ],
                "attacker_id": PLAYER_ID,
                "target_id": dummy_id,
                "arg4_u64": 860100100,
                "damage": 500,
            }
        )
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=3)[
                    "filetime_100ns"
                ],
                "entity_id": boss_id,
                "template_id": 7_103_402,
                "boss_type": 3,
            }
        )
        self.assertEqual(parser.active_boss_entity_id, dummy_id)

        updates = parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=4)[
                    "filetime_100ns"
                ],
                "attacker_id": PLAYER_ID,
                "target_id": boss_id,
                "arg4_u64": 860100100,
                "damage": 468,
            }
        )

        self.assertEqual(parser.active_boss_entity_id, boss_id)
        self.assertEqual(
            next(value for kind, value in updates if kind == "event")[
                "target_id"
            ],
            boss_id,
        )

    def test_oversized_stale_stage_table_cannot_replace_party(self):
        parser = NetworkPacketParser()
        profiles = {
            f"stale-stage-token-{index}": {
                "$map": [
                    [0, f"stale-stage-token-{index}"],
                    [1, PLAYER_ID + index],
                    [5, f"旧队员{index + 1}"],
                    [6, 10_000],
                ]
            }
            for index in range(13)
        }
        updates = parser.process(
            packet(
                "OnMsgUpdateStageCombatStatistics",
                [{"$map": [[0, 1], [1, 1], [2, "stale"], [5, profiles]]}],
            )
        )
        self.assertFalse(any(kind == "party" for kind, _value in updates))
        self.assertFalse(
            any(kind == "stage_summary" for kind, _value in updates)
        )
        self.assertEqual(parser.party_member_count, 0)

    def test_inferred_teammate_hit_keeps_recent_network_skill_id(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 700
        boss_pointer = MONSTER_POINTER + 700
        teammate_id = PLAYER_ID + 70
        skill_id = 86_051_010
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )
        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [{"$map": [[0, teammate_id], [2, skill_id], [17, boss_id]]}],
                pointer=99_001,
                sequence=2,
            )
        )
        parser.process(
            packet("OnMsgSyncFightMode", [2], pointer=boss_pointer, sequence=3)
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [teammate_id],
                pointer=boss_pointer,
                sequence=4,
            )
        )
        parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [1_000.0],
                pointer=boss_pointer,
                sequence=5,
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [teammate_id],
                pointer=boss_pointer,
                sequence=6,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [900.0],
                pointer=boss_pointer,
                sequence=7,
            )
        )
        hit = next(
            value
            for kind, value in updates
            if kind == "event" and value["function"].endswith("/team-hit")
        )
        self.assertEqual(hit["attacker_id"], teammate_id)
        self.assertEqual(hit["skill_id"], skill_id)
        self.assertEqual(hit["damage"], 100)

    def test_player_hp_and_skill_values_cannot_replace_locked_boss_hp(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 800
        boss_pointer = MONSTER_POINTER + 800
        player_pointer = MONSTER_POINTER + 801
        teammate_id = PLAYER_ID + 80
        skill_id = 86_051_010
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )
        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [{"$map": [[0, teammate_id], [2, skill_id], [17, boss_id]]}],
                pointer=player_pointer,
                sequence=2,
            )
        )
        parser.process(
            packet("OnMsgSyncFightMode", [2], pointer=boss_pointer, sequence=3)
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [teammate_id],
                pointer=boss_pointer,
                sequence=4,
            )
        )
        parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [3_742_490.0],
                pointer=boss_pointer,
                sequence=5,
            )
        )
        self.assertEqual(parser.entity_current_hp[boss_id], 3_742_490.0)

        # This buff was applied by the Boss to a player. The third argument is
        # the source and must not rebind the player's ScriptEntity to the Boss.
        parser.process(
            packet(
                "OnMsgActorBuffStateSync",
                [6, 1, boss_id],
                pointer=player_pointer,
                sequence=6,
            )
        )
        player_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [8_274.0],
                pointer=player_pointer,
                sequence=7,
            )
        )
        self.assertEqual(parser.pointer_entities[player_pointer], teammate_id)
        self.assertFalse(
            any(
                kind == "monster"
                and value.get("entity_id") == boss_id
                and value.get("current_hp") == 8_274.0
                for kind, value in player_updates
            )
        )
        self.assertEqual(parser.entity_current_hp[boss_id], 3_742_490.0)

        skill_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [float(skill_id)],
                pointer=boss_pointer,
                sequence=8,
            )
        )
        self.assertFalse(
            any(
                kind == "monster" and value.get("current_hp") == float(skill_id)
                for kind, value in skill_updates
            )
        )
        self.assertEqual(parser.entity_current_hp[boss_id], 3_742_490.0)

    def test_wrong_pointer_death_cannot_zero_locked_boss(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 900
        boss_pointer = MONSTER_POINTER + 900
        wrong_pointer = MONSTER_POINTER + 901
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )
        parser.active_boss_pointer = boss_pointer
        parser.pointer_entities[boss_pointer] = boss_id
        parser.pointer_entities[wrong_pointer] = boss_id

        wrong_updates = parser.process(
            packet("OnMsgEntityDead", [], pointer=wrong_pointer, sequence=2)
        )
        self.assertFalse(
            any(
                kind == "monster"
                and value.get("entity_id") == boss_id
                and value.get("current_hp") == 0
                for kind, value in wrong_updates
            )
        )

        boss_updates = parser.process(
            packet("OnMsgEntityDead", [], pointer=boss_pointer, sequence=3)
        )
        self.assertTrue(
            any(
                kind == "monster"
                and value.get("entity_id") == boss_id
                and value.get("current_hp") == 0
                for kind, value in boss_updates
            )
        )

    def test_non_boss_hp_drop_never_generates_team_damage(self):
        parser = NetworkPacketParser()
        target_id = MONSTER_ID + 900
        target_pointer = MONSTER_POINTER + 900
        teammate_id = PLAYER_ID + 90
        parser.process(
            packet(
                "OnMsgBeatenSyncV2",
                [teammate_id, target_id, 0],
                pointer=target_pointer,
                sequence=1,
            )
        )
        parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [1_000.0],
                pointer=target_pointer,
                sequence=2,
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [teammate_id],
                pointer=target_pointer,
                sequence=3,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [500.0],
                pointer=target_pointer,
                sequence=4,
            )
        )
        self.assertFalse(
            any(
                kind == "event" and value["function"].endswith("/team-hit")
                for kind, value in updates
            )
        )

    def test_tarot_member_list_is_not_treated_as_dungeon_party_roster(self):
        parser = NetworkPacketParser(
            {
                TEAMMATE_TOKEN: {
                    "name": "旧队友",
                    "profession_id": 1_200_003,
                    "level": 62,
                },
                "new-token": {
                    "name": "新队友",
                    "profession_id": 1_200_005,
                    "level": 63,
                },
            }
        )
        parser.self_id = PLAYER_ID
        parser.self_token = SELF_TOKEN
        parser._bind_team_token(SELF_TOKEN, PLAYER_ID)
        old_actor = parser._bind_team_token(
            TEAMMATE_TOKEN, stable_team_actor_id(TEAMMATE_TOKEN)
        )
        parser.party_ids = {old_actor}
        parser.party_tokens = {TEAMMATE_TOKEN}

        updates = parser.process(
            packet(
                "OnMsgTarotTeamUpdateMemberList",
                [[SELF_TOKEN, "new-token"]],
                sequence=20,
            )
        )

        self.assertEqual(parser.party_tokens, {TEAMMATE_TOKEN})
        self.assertEqual(parser.party_ids, {old_actor})
        self.assertFalse(any(kind == "party" for kind, _value in updates))

    def test_tarot_member_list_cannot_assign_local_identity(self):
        parser = NetworkPacketParser()
        parser.self_id = PLAYER_ID
        parser.self_confirmed = True
        parser.process(
            packet(
                "OnUpdateTeamGroupSelfProps",
                [{"$map": [[11, 34_356]]}],
                sequence=21,
            )
        )
        parser.process(
            packet(
                "s2cSendOneFriendClubInfo",
                [
                    {
                        "$map": [
                            [0, SELF_TOKEN],
                            [1, 33_120_849_388],
                            [7, "莫雪"],
                            [10, 61],
                            [11, 1_200_002],
                            [17, 34_356],
                        ]
                    },
                    {
                        "$map": [
                            [0, TEAMMATE_TOKEN],
                            [1, TEAMMATE_ROLE_NUMBER],
                            [7, "华木尤"],
                            [10, 61],
                            [11, 1_200_003],
                            [17, 47_745],
                        ]
                    },
                ],
                sequence=22,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgTarotTeamUpdateMemberList",
                [[SELF_TOKEN, TEAMMATE_TOKEN]],
                sequence=23,
            )
        )
        self.assertIsNone(parser.self_token)
        self.assertFalse(any(kind == "party" for kind, _value in updates))

    def test_network_team_profile_is_exported_for_future_sessions(self):
        parser = NetworkPacketParser()
        actor_id = stable_team_actor_id(TEAMMATE_TOKEN)
        update = parser._profile_update(
            actor_id,
            packet("OnMsgMembersJoinTeam", [], sequence=30),
            user_token=TEAMMATE_TOKEN,
            name="网络队友",
            profession_id=1_200_006,
            level=61,
        )
        self.assertIsNotNone(update)
        cache = parser.take_team_profile_cache()
        self.assertEqual(cache[TEAMMATE_TOKEN]["name"], "网络队友")
        self.assertEqual(cache[TEAMMATE_TOKEN]["profession_id"], 1_200_006)
        self.assertIsNone(parser.take_team_profile_cache())

    def test_skill_normalization(self):
        self.assertEqual(normalize_network_skill_id(8_602_107_000_396), 86_021_070)
        self.assertEqual(normalize_network_skill_id(860_210_700), 86_021_070)
        self.assertEqual(normalize_network_skill_id(89_002_557), 89_002_557)
        self.assertEqual(profession_from_skill(86_071_030), 1_200_007)
        self.assertEqual(profession_from_skill(89_007_001), 0)


if __name__ == "__main__":
    unittest.main()
