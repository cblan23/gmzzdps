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
    should_decode_network_arguments,
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
    def test_capture_only_decodes_messages_used_by_combat_or_identity(self):
        self.assertTrue(should_decode_network_arguments("OnMsgSyncCurrentHp"))
        self.assertTrue(should_decode_network_arguments("OnMsgHitFeedback"))
        self.assertTrue(should_decode_network_arguments("OnMsgHitFeedbackByID"))
        self.assertTrue(
            should_decode_network_arguments("OnMsgTakeLastAttackDamage")
        )
        self.assertTrue(
            should_decode_network_arguments("OnUpdateTeamGroupMemberProps")
        )
        self.assertTrue(should_decode_network_arguments("RetOtherRoleShapeData"))
        self.assertTrue(should_decode_network_arguments("OnMsgReconnectOrEnter"))
        self.assertTrue(
            should_decode_network_arguments("OnMsgDungeonStageSettlement")
        )
        self.assertFalse(should_decode_network_arguments("OnMsgStopSkill"))
        self.assertFalse(should_decode_network_arguments("OnMsgSyncFinalSpeed"))

    def test_stale_decoded_arguments_are_never_used(self):
        record = packet("OnMsgSyncCurrentHp", [123.0])
        record["decode_delay_ms"] = 251.0
        self.assertEqual(NetworkPacketParser._args(record), [])
        record["decode_delay_ms"] = 249.0
        self.assertEqual(NetworkPacketParser._args(record), [123.0])

    def test_dungeon_and_stage_ids_are_retained_from_explicit_protocol_fields(self):
        parser = NetworkPacketParser()
        parser.process(
            packet(
                "OnMsgDungeonReadinessCheck",
                [0, 5_100_054, "", 1_787_851_808, 12, 1],
                sequence=1,
            )
        )
        parser.process(
            packet(
                "OnMsgUpdateStageCombatStatistics",
                [
                    {
                        "$map": [
                            [0, 5_150_059],
                            [1, 2],
                            [5, {SELF_TOKEN: {"$map": [[1, PLAYER_ID]]}}],
                        ]
                    }
                ],
                sequence=2,
            )
        )
        parser.process(
            packet(
                "OnMsgReconnectOrEnter",
                [{"$map": [[3, 5_100_054], [8, 5_150_060], [9, 86_021_070]]}],
                sequence=3,
            )
        )

        context = parser.current_dungeon_context()
        self.assertEqual(context["dungeon_id"], 5_100_054)
        self.assertEqual(context["dungeon_stage_id"], 5_150_059)
        self.assertEqual(context["dungeon_stage_phase"], 2)
        self.assertEqual(
            context["reconnect_dungeon_candidates"],
            [5_100_054, 5_150_060],
        )
        self.assertEqual(
            context["dungeon_context_filetime"],
            packet("", [], sequence=3)["filetime_100ns"],
        )

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

    def test_first_karl_edgar_template_promotes_all_four_runtime_entities(self):
        entity_ids = (
            246_581_588_843_300,
            246_581_588_843_301,
            246_581_588_843_302,
            246_581_588_843_303,
        )
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7107030": {
                    "boss_type": 0,
                    "name": "Karl Edgar",
                }
            }
        )

        for sequence, entity_id in enumerate(entity_ids, start=1):
            updates = parser.process_native_boss_type(
                {
                    "entity_id": entity_id,
                    "template_id": 7_107_030,
                    "boss_type": 0,
                    "filetime_100ns": packet("", [], sequence=sequence)[
                        "filetime_100ns"
                    ],
                }
            )
            profile = next(
                value for kind, value in updates if kind == "profile"
            )
            self.assertEqual(profile["entity_type"], "Boss")
            self.assertEqual(profile["boss_type"], 3)
            self.assertEqual(profile["template_id"], 7_107_030)

        self.assertTrue(set(entity_ids).issubset(parser.confirmed_boss_entities))

    def test_server_level_arriving_after_boss_backfills_missing_level(self):
        boss_id = MONSTER_ID + 4_000
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7107030": {
                    "boss_type": 0,
                    "name": "Karl Edgar",
                }
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_107_030,
                "boss_type": 0,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        self.assertNotIn("level", parser.entity_profiles[boss_id])

        updates = parser.process(
            packet("RetGetServerLevelInfo", [68], sequence=2)
        )
        boss_profile = next(
            value
            for kind, value in updates
            if kind == "profile" and value.get("entity_id") == boss_id
        )
        self.assertEqual(boss_profile["level"], 68)
        self.assertEqual(parser.entity_profiles[boss_id]["level"], 68)

    def test_star_guard_is_linked_without_fabricating_realtime_damage(self):
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
        self.assertFalse(any(kind == "event" for kind, _value in hp_updates))
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

    def test_missing_astrologer_imprisonments_are_inferred_from_damage_targets(self):
        """FB7408AA158422D3F4: unnamed targets 1/3/5 are imprisonments."""
        boss_id = MONSTER_ID + 3_010
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {
                    "boss_type": 3,
                    "name": "星象仪者",
                    "level": 62,
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

        imprisonment_ids = [MONSTER_ID + 3_011 + index for index in range(3)]
        for sequence, imprisonment_id in enumerate(imprisonment_ids, start=2):
            updates = parser.process_native_damage(
                {
                    "attacker_id": PLAYER_ID,
                    "target_id": imprisonment_id,
                    "arg4_u64": 860_510_100,
                    "raw_damage": 10_000 + sequence,
                    "damage": 10_000 + sequence,
                    "local_player_id": PLAYER_ID,
                    "filetime_100ns": packet("", [], sequence=sequence)[
                        "filetime_100ns"
                    ],
                }
            )
            profile = next(value for kind, value in updates if kind == "profile")
            self.assertEqual(profile["name"], "禁锢")
            self.assertEqual(profile["template_id"], 7_102_404)
            self.assertTrue(profile["auxiliary_inferred"])
            self.assertTrue(
                parser._is_active_encounter_auxiliary(imprisonment_id)
            )
            self.assertIn(imprisonment_id, parser.pending_exact_damage)

        self.assertEqual(
            {
                parser.entity_template_ids[imprisonment_id]
                for imprisonment_id in imprisonment_ids
            },
            {7_102_404},
        )

    def test_inferred_imprisonment_hp_drop_does_not_guess_teammate_damage(self):
        boss_id = MONSTER_ID + 3_015
        imprisonment_id = MONSTER_ID + 3_016
        imprisonment_pointer = MONSTER_POINTER + 3_016
        teammate_id = PLAYER_ID + 3_016
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {
                    "boss_type": 3,
                    "name": "星象仪者",
                    "level": 62,
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
        parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": imprisonment_id,
                "arg4_u64": 860_510_100,
                "raw_damage": 10_000,
                "damage": 10_000,
                "local_player_id": PLAYER_ID,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        parser.party_ids.add(teammate_id)
        parser.pointer_entities[imprisonment_pointer] = imprisonment_id
        parser.process(
            packet(
                "OnMsgSyncCurrentMaxHp",
                [462_521.0, 462_521.0],
                pointer=imprisonment_pointer,
                sequence=3,
            )
        )
        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [
                    {
                        "$map": [
                            [0, teammate_id],
                            [2, 86_051_010],
                            [17, imprisonment_id],
                        ]
                    }
                ],
                pointer=90_016,
                sequence=4,
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [teammate_id],
                pointer=imprisonment_pointer,
                sequence=5,
            )
        )
        hp_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [362_521.0],
                pointer=imprisonment_pointer,
                sequence=6,
            )
        )

        self.assertFalse(any(kind == "event" for kind, _value in hp_updates))
        self.assertEqual(
            parser.entity_current_hp[imprisonment_id], 362_521.0
        )

    def test_explicit_star_guard_template_overrides_inferred_imprisonment(self):
        boss_id = MONSTER_ID + 3_017
        guard_id = MONSTER_ID + 3_018
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {
                    "boss_type": 3,
                    "name": "星象仪者",
                    "level": 62,
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
        inferred_updates = parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": guard_id,
                "arg4_u64": 860_510_100,
                "raw_damage": 10_000,
                "damage": 10_000,
                "local_player_id": PLAYER_ID,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        inferred_profile = next(
            value for kind, value in inferred_updates if kind == "profile"
        )
        self.assertEqual(inferred_profile["name"], "禁锢")

        explicit_updates = parser.process_native_boss_type(
            {
                "entity_id": guard_id,
                "template_id": 7_102_405,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=3)["filetime_100ns"],
            }
        )
        explicit_profile = next(
            value for kind, value in explicit_updates if kind == "profile"
        )
        self.assertEqual(explicit_profile["name"], "星光守卫")
        self.assertEqual(explicit_profile["template_id"], 7_102_405)
        self.assertFalse(explicit_profile["auxiliary_inferred"])
        self.assertEqual(parser.entity_template_ids[guard_id], 7_102_405)

    def test_missing_auxiliary_inference_rejects_players_and_other_templates(self):
        boss_id = MONSTER_ID + 3_030
        party_target_id = PLAYER_ID + 3_030
        other_monster_id = MONSTER_ID + 3_031
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {
                    "boss_type": 3,
                    "name": "星象仪者",
                    "level": 62,
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
        parser.party_ids.add(party_target_id)
        parser.entity_profiles[party_target_id] = {
            "entity_id": party_target_id,
            "entity_type": "Player",
            "name": "队友",
        }
        parser.entity_template_ids[other_monster_id] = 7_999_999

        for sequence, target_id in enumerate(
            (party_target_id, other_monster_id), start=2
        ):
            updates = parser.process_native_damage(
                {
                    "attacker_id": PLAYER_ID,
                    "target_id": target_id,
                    "arg4_u64": 860_510_100,
                    "raw_damage": 10_000,
                    "damage": 10_000,
                    "local_player_id": PLAYER_ID,
                    "filetime_100ns": packet("", [], sequence=sequence)[
                        "filetime_100ns"
                    ],
                }
            )
            self.assertFalse(
                any(
                    kind == "profile"
                    and value.get("entity_id") == target_id
                    and value.get("auxiliary_inferred")
                    for kind, value in updates
                )
            )
            self.assertFalse(parser._is_active_encounter_auxiliary(target_id))

        self.assertNotIn(party_target_id, parser.entity_template_ids)
        self.assertEqual(parser.entity_template_ids[other_monster_id], 7_999_999)

    def test_late_star_guard_template_revokes_player_and_party_classification(self):
        boss_id = MONSTER_ID + 3_020
        guard_id = MONSTER_ID + 3_021
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {
                    "boss_type": 3,
                    "name": "星象仪者",
                    "level": 62,
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

        early_updates = parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
                "attacker_id": guard_id,
                "target_id": boss_id,
                "arg4_u64": 8_605_101_000_396,
                "raw_damage": 9_148,
                "damage": 9_148,
            }
        )
        early_event = next(
            value for kind, value in early_updates if kind == "event"
        )
        self.assertTrue(early_event["player_attacker"])
        self.assertIn(guard_id, parser.player_attackers)
        self.assertIn(guard_id, parser.combat_source_actors)

        parser.party_tokens.add(TEAMMATE_TOKEN)
        parser.party_ids.add(guard_id)
        parser.party_member_count = 2
        parser.token_actors[TEAMMATE_TOKEN] = guard_id
        parser.actor_tokens[guard_id] = TEAMMATE_TOKEN
        late_updates = parser.process_native_boss_type(
            {
                "entity_id": guard_id,
                "template_id": 7_102_405,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=3)["filetime_100ns"],
            }
        )

        self.assertNotIn(guard_id, parser.player_attackers)
        self.assertNotIn(guard_id, parser.combat_source_actors)
        self.assertNotIn(guard_id, parser.party_ids)
        self.assertNotIn(guard_id, parser.actor_tokens)
        profile = next(value for kind, value in late_updates if kind == "profile")
        self.assertEqual(profile["name"], "星光守卫")
        party = next(value for kind, value in late_updates if kind == "party")
        self.assertNotIn(guard_id, party["entity_ids"])

        future_updates = parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=4)["filetime_100ns"],
                "attacker_id": guard_id,
                "target_id": boss_id,
                "arg4_u64": 8_605_101_000_396,
                "raw_damage": 8_804,
                "damage": 8_804,
            }
        )
        future_event = next(
            value for kind, value in future_updates if kind == "event"
        )
        self.assertFalse(future_event["player_attacker"])
        self.assertFalse(future_event["party_attacker"])

    def test_star_guard_damage_and_boss_hp_are_independent(self):
        boss_id = MONSTER_ID + 3_040
        guard_id = MONSTER_ID + 3_041
        boss_pointer = MONSTER_POINTER + 3_040
        teammate_id = PLAYER_ID + 3_040
        max_hp = 33_270_350
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {
                    "boss_type": 3,
                    "name": "星象仪者",
                    "level": 62,
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
        parser.process_native_boss_type(
            {
                "entity_id": guard_id,
                "template_id": 7_102_405,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.entity_max_hp[boss_id] = float(max_hp)
        parser.entity_current_hp[boss_id] = float(max_hp)

        guard_damage = parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=3)["filetime_100ns"],
                "attacker_id": teammate_id,
                "target_id": guard_id,
                "arg4_u64": 8_605_101_000_396,
                "raw_damage": 5_000,
                "damage": 5_000,
            }
        )
        guard_event = next(
            value for kind, value in guard_damage if kind == "event"
        )
        self.assertEqual(guard_event["target_id"], guard_id)
        self.assertEqual(guard_event["damage"], 5_000)

        parser.process(
            packet(
                "OnMsgAddBuffNew",
                [84_080_722, guard_id, 0],
                pointer=99_999,
                sequence=4,
            )
        )
        parser.process(
            packet(
                "OnMsgEntityDead",
                [84_080_713, guard_id, 1],
                pointer=99_999,
                sequence=5,
            )
        )
        parser.process(
            packet(
                "OnMsgEntityDead",
                [84_080_713, guard_id, 1],
                pointer=99_999,
                sequence=6,
            )
        )
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
                    pointer=90_000 + sequence,
                    sequence=sequence,
                )
            )

        teammate_cast(7)
        ordinary = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [max_hp - 10_000.0],
                pointer=boss_pointer,
                sequence=8,
            )
        )
        self.assertFalse(any(kind == "event" for kind, _value in ordinary))
        ordinary_hp = next(value for kind, value in ordinary if kind == "monster")
        self.assertEqual(ordinary_hp["current_hp"], max_hp - 10_000.0)

        teammate_cast(9)
        later_drop = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [max_hp - 1_500_000],
                pointer=boss_pointer,
                sequence=10,
            )
        )
        self.assertFalse(any(kind == "event" for kind, _value in later_drop))

    def test_star_guard_death_and_parent_hp_drop_emit_no_guessed_damage(self):
        """FB8E738F35DCC29DFE/FB1FB02AA330C2BBB4: HP is not per-player damage."""
        boss_id = MONSTER_ID + 3_060
        guard_id = MONSTER_ID + 3_061
        boss_pointer = MONSTER_POINTER + 3_060
        guard_pointer = MONSTER_POINTER + 3_061
        attacker_a = PLAYER_ID + 3_060
        attacker_b = PLAYER_ID + 3_061
        boss_hp = 17_663_571
        guard_max_hp = 1_325_792
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {"boss_type": 3, "name": "星象仪者", "level": 62}
            }
        )
        parser.self_id = PLAYER_ID
        parser.self_confirmed = True
        parser.party_ids.update({attacker_a, attacker_b})
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_102_403,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": guard_id,
                "template_id": 7_102_405,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.pointer_entities[guard_pointer] = guard_id
        parser.active_boss_pointer = boss_pointer
        parser.entity_max_hp[boss_id] = 33_270_350.0
        parser.entity_current_hp[boss_id] = float(boss_hp)
        parser.entity_current_hp_time[boss_id] = packet("", [], sequence=2)[
            "filetime_100ns"
        ]
        parser.entity_max_hp[guard_id] = float(guard_max_hp)
        parser.entity_current_hp[guard_id] = 2_831.0

        guard_death = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [0.0],
                pointer=guard_pointer,
                sequence=3,
            )
        )
        self.assertTrue(
            any(
                kind == "monster"
                and value.get("entity_id") == guard_id
                and value.get("current_hp") == 0
                for kind, value in guard_death
            )
        )
        def observed_hit(actor_id: int, sequence: int) -> None:
            parser.process(
                packet(
                    "OnMsgEndureExitHit",
                    [actor_id],
                    pointer=boss_pointer,
                    sequence=sequence,
                )
            )

        observed_hit(attacker_a, 4)
        early = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [boss_hp - 3_464.0],
                pointer=boss_pointer,
                sequence=5,
            )
        )
        self.assertFalse(any(kind == "event" for kind, _value in early))

        observed_hit(attacker_a, 6)
        observed_hit(attacker_b, 7)
        next_hp = boss_hp - 3_464 - guard_max_hp - 31_847
        mechanism_drop = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [float(next_hp)],
                pointer=boss_pointer,
                sequence=8,
            )
        )
        self.assertFalse(
            any(kind == "event" for kind, _value in mechanism_drop)
        )
        self.assertEqual(parser.entity_current_hp[boss_id], float(next_hp))

    def test_star_guard_real_damage_is_not_removed_with_parent_mechanic(self):
        """FB0FF4CCB0AF8424F7: real hits on the add still count in real time."""
        boss_id = MONSTER_ID + 3_070
        guard_id = MONSTER_ID + 3_071
        attacker_id = PLAYER_ID + 3_070
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {"boss_type": 3, "name": "星象仪者", "level": 62}
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
        parser.process_native_boss_type(
            {
                "entity_id": guard_id,
                "template_id": 7_102_405,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        real_damage = parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=3)["filetime_100ns"],
                "attacker_id": attacker_id,
                "target_id": guard_id,
                "arg4_u64": 8_605_101_000_396,
                "raw_damage": 48_219,
                "damage": 48_219,
            }
        )
        event = next(value for kind, value in real_damage if kind == "event")
        self.assertEqual(event["target_id"], guard_id)
        self.assertEqual(event["damage"], 48_219)
        self.assertFalse(event["provisional_damage"])

    def test_revived_star_guard_deaths_never_create_player_damage(self):
        """FB3958DB48DB1D4CE8: repeated guard deaths cannot inflate a player."""
        boss_id = MONSTER_ID + 3_080
        guard_id = MONSTER_ID + 3_081
        boss_pointer = MONSTER_POINTER + 3_080
        guard_pointer = MONSTER_POINTER + 3_081
        tank_id = PLAYER_ID + 3_080
        boss_hp = 20_888_137
        guard_max_hp = 1_325_792
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102403": {"boss_type": 3, "name": "星象仪者", "level": 62}
            }
        )
        parser.self_id = PLAYER_ID
        parser.self_confirmed = True
        parser.party_ids.add(tank_id)
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_102_403,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": guard_id,
                "template_id": 7_102_405,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.pointer_entities[guard_pointer] = guard_id
        parser.active_boss_pointer = boss_pointer
        parser.entity_max_hp[boss_id] = 33_270_350.0
        parser.entity_current_hp[boss_id] = float(boss_hp)
        parser.entity_current_hp_time[boss_id] = packet("", [], sequence=2)[
            "filetime_100ns"
        ]
        parser.entity_max_hp[guard_id] = float(guard_max_hp)
        parser.entity_current_hp[guard_id] = 100.0

        def guard_hp(value: float, sequence: int):
            return parser.process(
                packet(
                    "OnMsgSyncCurrentHp",
                    [value],
                    pointer=guard_pointer,
                    sequence=sequence,
                )
            )

        def boss_hit_then_drop(sequence: int, real_damage: int):
            parser.process(
                packet(
                    "OnMsgEndureExitHit",
                    [tank_id],
                    pointer=boss_pointer,
                    sequence=sequence,
                )
            )
            previous = int(parser.entity_current_hp[boss_id])
            return parser.process(
                packet(
                    "OnMsgSyncCurrentHp",
                    [float(previous - guard_max_hp - real_damage)],
                    pointer=boss_pointer,
                    sequence=sequence + 1,
                )
            )

        guard_hp(0.0, 3)
        first = boss_hit_then_drop(4, 10_000)
        self.assertFalse(any(kind == "event" for kind, _value in first))

        # The same entity returns to positive HP and starts a new life cycle.
        guard_hp(float(guard_max_hp), 6)
        guard_hp(float(guard_max_hp), 7)
        guard_hp(0.0, 8)
        guard_hp(0.0, 9)

        second = boss_hit_then_drop(10, 20_000)
        self.assertFalse(any(kind == "event" for kind, _value in second))

    def test_ice_prison_is_linked_to_langbo_without_making_langbo_multiphase(self):
        boss_id = MONSTER_ID + 3_010
        ice_prison_id = MONSTER_ID + 3_011
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7107105": {
                    "boss_type": 3,
                    "name": "朗伯·绞索",
                    "level": 63,
                }
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_107_105,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        updates = parser.process_native_boss_type(
            {
                "entity_id": ice_prison_id,
                "template_id": 7_107_121,
                "boss_type": 1,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )

        profile = next(value for kind, value in updates if kind == "profile")
        self.assertEqual(profile["name"], "冰牢")
        self.assertTrue(profile["encounter_auxiliary"])
        self.assertEqual(parser.active_boss_entity_id, boss_id)
        self.assertTrue(parser._is_active_encounter_auxiliary(ice_prison_id))

        parser._release_active_boss(boss_id)
        self.assertIsNone(parser.active_boss_entity_id)

    def test_multiphase_boss_hp_changes_never_create_team_damage(self):
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
        self.assertEqual(first_damage, [])

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
        self.assertEqual(second_damage, [])

    def test_boss_max_hp_epoch_does_not_credit_phase_drop_to_old_hits(self):
        """FB01EF06DCAF1BA6D3: 阶段换血条不能归到最后命中的玩家。"""
        boss_id = MONSTER_ID + 3_200
        boss_pointer = MONSTER_POINTER + 3_200
        teammate_id = PLAYER_ID + 3_200
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7103401": {
                    "boss_type": 3,
                    "name": "伯德温·威瑟尔",
                    "level": 62,
                }
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_103_401,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.entity_current_hp[boss_id] = 20_000_000.0
        parser.entity_max_hp[boss_id] = 36_000_000.0

        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [{"$map": [[0, teammate_id], [2, 86_021_100], [17, boss_id]]}],
                sequence=2,
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [teammate_id],
                pointer=boss_pointer,
                sequence=3,
            )
        )
        phase_change = parser.process(
            packet(
                "OnMsgSyncCurrentMaxHp",
                [18_000_000.0, 40_000_000.0],
                pointer=boss_pointer,
                sequence=4,
            )
        )
        self.assertFalse(
            any(
                kind == "event" and value["function"].endswith("/team-hit")
                for kind, value in phase_change
            )
        )

        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [{"$map": [[0, teammate_id], [2, 86_021_100], [17, boss_id]]}],
                sequence=5,
            )
        )
        next_drop = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [17_500_000.0],
                pointer=boss_pointer,
                sequence=6,
            )
        )
        inferred = [
            value
            for kind, value in next_drop
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        self.assertEqual(inferred, [])

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

    def test_shared_party_skill_cannot_rebind_confirmed_local_identity(self):
        teammate_id = PLAYER_ID + 40_000
        skill_id = 86_020_050
        parser = NetworkPacketParser()
        parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": MONSTER_ID,
                "arg4_u64": 860_200_400,
                "raw_damage": 1_000,
                "damage": 1_000,
                "local_player_id": PLAYER_ID,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.process(
            packet("RetCastSkillSuccessNew", [skill_id], sequence=2)
        )
        updates = parser.process(
            packet(
                "OnMsgHealSyncV2",
                [teammate_id, PLAYER_ID, skill_id],
                sequence=3,
            )
        )

        self.assertEqual(parser.self_id, PLAYER_ID)
        self.assertEqual(parser.native_self_id, PLAYER_ID)
        self.assertFalse(
            any(kind in {"identity", "actor_merge"} for kind, _value in updates)
        )

    def test_heuristic_identity_correction_does_not_merge_positive_actors(self):
        parser = NetworkPacketParser()
        tentative_id = PLAYER_ID + 40_001
        parser.self_id = tentative_id
        parser.self_confirmed = False

        updates = parser._confirm_local_actor(
            PLAYER_ID,
            packet("RetCastSkillSuccessNew", [86_020_050], sequence=1),
        )

        identity = next(value for kind, value in updates if kind == "identity")
        self.assertEqual(identity["entity_id"], PLAYER_ID)
        self.assertFalse(any(kind == "actor_merge" for kind, _value in updates))
        self.assertEqual(parser.self_id, PLAYER_ID)

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

    def test_placeholder_profile_name_cannot_replace_catalog_boss_name(self):
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7109821": {
                    "boss_type": 3,
                    "name": "异化猎犬",
                    "level": 62,
                }
            }
        )
        parser.process_native_boss_type(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_109_821,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )

        parser._profile_update(
            MONSTER_ID,
            packet("", [], sequence=2),
            name="首领",
            entity_type="Boss",
            boss_type=3,
            boss_rank=3,
            boss_source="hud_signal_team_target",
        )

        self.assertEqual(
            parser.entity_profiles[MONSTER_ID]["name"], "异化猎犬"
        )
        self.assertNotEqual(
            parser.runtime_entity_names.get(MONSTER_ID), "首领"
        )

    def test_placeholder_profile_recovers_when_template_arrives_later(self):
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7109821": {
                    "boss_type": 3,
                    "name": "异化猎犬",
                    "level": 62,
                }
            }
        )
        provisional = parser._profile_update(
            MONSTER_ID,
            packet("", [], sequence=1),
            name="未命名Boss",
            entity_type="Boss",
            boss_type=3,
            boss_rank=3,
        )
        self.assertNotIn("name", provisional[1])

        updates = parser.process_native_boss_type(
            {
                "entity_id": MONSTER_ID,
                "template_id": 7_109_821,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )
        profile = next(value for kind, value in updates if kind == "profile")

        self.assertEqual(profile["name"], "异化猎犬")
        self.assertEqual(
            parser.entity_profiles[MONSTER_ID]["name"], "异化猎犬"
        )

    def test_all_clown_templates_activate_and_forward_first_damage(self):
        templates = (7_102_873, 7_103_012, 7_103_309)
        catalog = {
            str(template_id): {"boss_type": 3, "name": "小丑"}
            for template_id in templates
        }
        for index, template_id in enumerate(templates, start=1):
            with self.subTest(template_id=template_id):
                boss_id = MONSTER_ID + index
                parser = NetworkPacketParser(boss_template_catalog=catalog)
                profiles = parser.process_native_boss_type(
                    {
                        "entity_id": boss_id,
                        "template_id": template_id,
                        "boss_type": 3,
                        "filetime_100ns": packet("", [], sequence=1)[
                            "filetime_100ns"
                        ],
                    }
                )
                profile = next(
                    value for kind, value in profiles if kind == "profile"
                )
                damage_updates = parser.process_native_damage(
                    {
                        "entity_id": PLAYER_ID,
                        "attacker_id": PLAYER_ID,
                        "target_id": boss_id,
                        "arg4_u64": 860_210_700,
                        "raw_damage": 12_345,
                        "damage": 12_345,
                        "local_player_id": PLAYER_ID,
                        "filetime_100ns": packet("", [], sequence=2)[
                            "filetime_100ns"
                        ],
                    }
                )
                event = next(
                    value for kind, value in damage_updates if kind == "event"
                )

                self.assertEqual(profile["name"], "小丑")
                self.assertEqual(parser.active_boss_entity_id, boss_id)
                self.assertEqual(event["target_id"], boss_id)
                self.assertEqual(event["damage"], 12_345)

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

    def test_same_process_boss_restore_keeps_metadata_but_not_current_hp(self):
        boss_id = MONSTER_ID + 1_150
        template_id = 7_102_403
        boss_name = "\u661f\u8c61\u4eea\u8005"
        catalog = {
            str(template_id): {
                "boss_type": 3,
                "name": boss_name,
                "level": 61,
            }
        }
        parser = NetworkPacketParser(boss_template_catalog=catalog)
        timestamp = packet("", [], sequence=1)["filetime_100ns"]
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": template_id,
                "boss_type": 3,
                "filetime_100ns": timestamp,
            }
        )
        parser.entity_max_hp[boss_id] = 9_876_543.0
        parser.entity_current_hp[boss_id] = 4_321_000.0

        state = parser.current_active_boss_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["name"], boss_name)
        self.assertEqual(state["max_hp"], 9_876_543.0)
        self.assertNotIn("current_hp", state)

        restored = NetworkPacketParser(boss_template_catalog=catalog)
        updates = restored.restore_active_boss_state(state)
        profile = next(value for kind, value in updates if kind == "profile")
        monster = next(value for kind, value in updates if kind == "monster")
        self.assertEqual(profile["name"], boss_name)
        self.assertEqual(monster["max_hp"], 9_876_543.0)
        self.assertNotIn("current_hp", monster)
        self.assertNotIn(boss_id, restored.entity_current_hp)
        self.assertEqual(restored.current_active_boss_state()["entity_id"], boss_id)

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

    def test_space_transition_clears_stale_dummy_without_full_refresh(self):
        dummy_id = MONSTER_ID
        boss_id = MONSTER_ID + 1
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7114223": {"boss_type": 3, "name": "伤害木桩"},
                "7102873": {"boss_type": 3, "name": "小丑"},
            }
        )
        parser.scene_id = 5_200_002
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)[
                    "filetime_100ns"
                ],
                "entity_id": dummy_id,
                "template_id": 7_114_223,
                "boss_type": 3,
            }
        )
        self.assertEqual(parser.active_boss_entity_id, dummy_id)

        updates = parser.process(
            packet("OnMsgBeforeEnterNewSpace", [], sequence=2)
        )
        scene = next(value for kind, value in updates if kind == "scene")
        self.assertTrue(scene["transition"])
        self.assertTrue(scene["force_reset"])
        self.assertEqual(scene["previous_scene_id"], 5_200_002)
        self.assertEqual(scene["scene_id"], 0)
        self.assertIsNone(parser.scene_id)
        self.assertIsNone(parser.active_boss_entity_id)
        self.assertEqual(parser.entity_template_ids, {})

        stale_updates = parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=3)[
                    "filetime_100ns"
                ],
                "entity_id": dummy_id,
                "template_id": 7_114_223,
                "boss_type": 3,
            }
        )
        self.assertEqual(stale_updates, [])
        self.assertIsNone(parser.active_boss_entity_id)

        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=4)[
                    "filetime_100ns"
                ],
                "entity_id": boss_id,
                "template_id": 7_102_873,
                "boss_type": 3,
            }
        )
        self.assertEqual(parser.active_boss_entity_id, boss_id)

    def test_numeric_refresh_overload_does_not_clear_live_scene(self):
        parser = NetworkPacketParser()
        parser.process(packet("OnMsgRefreshSceneObjects", [5_200_142, {}, {}]))
        parser.active_boss_entity_id = MONSTER_ID
        parser.active_boss_pointer = MONSTER_POINTER
        parser.pointer_entities[MONSTER_POINTER] = MONSTER_ID

        updates = parser.process(
            packet(
                "OnMsgRefreshSceneObjects",
                [809_022_291, 3_278_132, 0],
                sequence=2,
            )
        )

        self.assertFalse(any(kind == "scene" for kind, _value in updates))
        self.assertEqual(parser.scene_id, 5_200_142)
        self.assertEqual(parser.active_boss_entity_id, MONSTER_ID)
        self.assertEqual(parser.active_boss_pointer, MONSTER_POINTER)
        self.assertEqual(parser.pointer_entities[MONSTER_POINTER], MONSTER_ID)

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

    def test_stale_same_template_drill_respawn_replaces_wiped_entity(self):
        first_id = MONSTER_ID + 1_220
        respawn_id = MONSTER_ID + 1_221
        template_id = 7_115_080
        parser = NetworkPacketParser(
            boss_template_catalog={
                str(template_id): {"boss_type": 3, "name": "“钻头”"},
            },
            boss_name_allowlist=("钻头",),
        )
        first_time = packet("", [], sequence=1)["filetime_100ns"]
        parser.process_native_boss_type(
            {
                "entity_id": first_id,
                "template_id": template_id,
                "boss_type": 3,
                "filetime_100ns": first_time,
            }
        )
        parser.entity_current_hp[first_id] = 5_127_785.0
        parser.entity_current_hp_time[first_id] = first_time
        parser.active_boss_time_100ns = first_time
        parser.active_boss_damage_epoch = first_time

        # A duplicate observed during the live HP stream cannot steal the lock.
        parser.process_native_boss_type(
            {
                "entity_id": respawn_id,
                "template_id": template_id,
                "boss_type": 3,
                "filetime_100ns": first_time + 4 * 10_000_000,
            }
        )
        self.assertEqual(parser.active_boss_entity_id, first_id)

        # Local replay: after a wipe the old entity remains alive in cache, but
        # the next pull gets a fresh entity before the local player attacks it.
        parser.process_native_boss_type(
            {
                "entity_id": respawn_id,
                "template_id": template_id,
                "boss_type": 3,
                "filetime_100ns": first_time + 6 * 10_000_000,
            }
        )
        self.assertEqual(parser.active_boss_entity_id, respawn_id)
        self.assertIsNone(parser.active_boss_pointer)
        self.assertEqual(parser.pending_target_hits, {})

    def test_lost_control_lokin_replaces_previous_boss_but_never_merges_back(self):
        lokin_id = MONSTER_ID + 1_213
        lost_control_id = MONSTER_ID + 1_214
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7110642": {"boss_type": 3, "name": "洛克·金"},
                "7110641": {"boss_type": 3, "name": "洛克·金·失控"},
            },
            boss_name_allowlist=("洛克·金", "洛克·金·失控"),
        )
        parser.process_native_boss_type(
            {
                "entity_id": lokin_id,
                "template_id": 7_110_642,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.entity_current_hp[lokin_id] = 600_551.0
        parser.process_native_boss_type(
            {
                "entity_id": lost_control_id,
                "template_id": 7_110_641,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )

        self.assertEqual(parser.active_boss_entity_id, lost_control_id)
        parser.entity_current_hp[lost_control_id] = 2_161_985.0
        parser.process_native_damage(
            {
                "filetime_100ns": packet("", [], sequence=3)["filetime_100ns"],
                "attacker_id": PLAYER_ID,
                "target_id": lokin_id,
                "arg4_u64": 860100100,
                "damage": 500,
            }
        )
        self.assertEqual(parser.active_boss_entity_id, lost_control_id)

    def test_same_name_120110_hp_lokin_template_is_never_a_boss(self):
        scripted_id = MONSTER_ID + 1_215
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7110551": {"boss_type": 3, "name": "洛克·金"},
            },
            boss_name_allowlist=("洛克·金",),
        )
        updates = parser.process_native_boss_type(
            {
                "entity_id": scripted_id,
                "template_id": 7_110_551,
                "boss_type": 0,
                "name": "洛克·金",
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
            }
        )
        parser.recent_target_id = scripted_id
        name_update = parser.apply_runtime_boss_name(
            {
                "entity_id": scripted_id,
                "name": "洛克·金",
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
            }
        )

        self.assertEqual(updates, [])
        self.assertIsNone(name_update)
        self.assertNotIn(scripted_id, parser.confirmed_boss_entities)
        self.assertIsNone(parser.active_boss_entity_id)

    def test_same_name_type_zero_lokin_cannot_be_promoted_by_late_damage_name(self):
        scripted_id = MONSTER_ID + 1_216
        parser = NetworkPacketParser(
            boss_template_catalog={},
            boss_name_allowlist=("洛克·金",),
        )
        parser.process_native_boss_type(
            {
                "entity_id": scripted_id,
                "template_id": 7_110_510,
                "boss_type": 0,
                "name": "洛克·金",
                "filetime_100ns": packet("", [], sequence=1)[
                    "filetime_100ns"
                ],
            }
        )
        parser.recent_target_id = scripted_id
        parser.runtime_entity_names[scripted_id] = "洛克·金"

        updates = parser.process_native_damage(
            {
                "attacker_id": PLAYER_ID,
                "target_id": scripted_id,
                "arg4_u64": 860_600_400,
                "raw_damage": 1_000,
                "damage": 1_000,
                "filetime_100ns": packet("", [], sequence=2)[
                    "filetime_100ns"
                ],
            }
        )

        self.assertFalse(any(kind == "profile" for kind, _value in updates))
        self.assertNotIn(scripted_id, parser.confirmed_boss_entities)
        self.assertIsNone(parser.active_boss_entity_id)

    def test_directed_skill_hit_refreshes_confirmed_boss_without_making_damage(self):
        boss_id = MONSTER_ID + 1_217
        parser = NetworkPacketParser(
            boss_template_catalog={
                "7102834": {
                    "boss_type": 3,
                    "name": "瑞尔·比伯",
                    "level": 52,
                }
            }
        )
        parser.party_ids.add(PLAYER_ID)
        parser.process_native_boss_type(
            {
                "entity_id": boss_id,
                "template_id": 7_102_834,
                "boss_type": 3,
                "filetime_100ns": packet("", [], sequence=1)[
                    "filetime_100ns"
                ],
            }
        )

        updates = parser.process(
            packet(
                "OnMsgCreateBullet",
                [86_060_040, 0, PLAYER_ID, 0, 0, 0, 0, 0, 0, 0, boss_id, 0, 0],
                sequence=2,
            )
        )

        activity = next(value for kind, value in updates if kind == "monster")
        self.assertEqual(activity["entity_id"], boss_id)
        self.assertFalse(any(kind == "event" for kind, _value in updates))

    def test_invalid_current_max_hp_pair_waits_for_next_valid_hp_sample(self):
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
        self.assertEqual(inferred, [])
        self.assertEqual(parser.entity_max_hp[boss_id], 36_409_062.0)
        self.assertFalse(any(kind == "monster" for kind, _value in updates))

        valid_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [5_000_000.0],
                pointer=boss_pointer,
                sequence=21,
            )
        )
        inferred = [
            value
            for kind, value in valid_updates
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        self.assertEqual(inferred, [])
        monster = next(value for kind, value in valid_updates if kind == "monster")
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

    def test_boss_music_and_two_explicit_attackers_recover_late_native_boss(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 2_200
        boss_pointer = MONSTER_POINTER + 2_200
        stale_actor = PLAYER_ID + 2_200
        attackers = [PLAYER_ID + 2_201, PLAYER_ID + 2_202]
        parser.party_seen = True
        parser.party_member_count = 3
        parser.party_ids.update({stale_actor, *attackers})

        base_time = packet("", [], sequence=1)["filetime_100ns"]

        def timed_packet(
            method: str,
            arguments: list,
            *,
            pointer: int,
            milliseconds: int,
        ) -> dict:
            value = packet(method, arguments, pointer=pointer)
            value["filetime_100ns"] = base_time + milliseconds * 10_000
            return value

        parser.process(
            timed_packet(
                "OnMsgSyncFightMode",
                [2],
                pointer=boss_pointer,
                milliseconds=0,
            )
        )
        parser.process(
            timed_packet(
                "OnMsgPostAkEvent",
                [["Set_MUS_B_WYZY_Boss_XianZuQiShi_Stage1"]],
                pointer=1,
                milliseconds=80,
            )
        )
        parser.process(
            timed_packet(
                "OnMsgActorBuffStateSync",
                [5, 1, stale_actor],
                pointer=boss_pointer,
                milliseconds=400,
            )
        )
        parser.process(
            timed_packet(
                "OnMsgSyncCurrentHp",
                [34_898_577.0],
                pointer=boss_pointer,
                milliseconds=470,
            )
        )
        self.assertEqual(parser.pointer_entities[boss_pointer], stale_actor)
        self.assertEqual(parser.entity_current_hp[stale_actor], 34_898_577.0)

        first_updates = parser.process(
            timed_packet(
                "OnMsgCreateLUnitSpellAgent",
                [
                    {
                        "$map": [
                            [0, attackers[0]],
                            [2, 86_051_010],
                            [17, boss_id],
                        ]
                    }
                ],
                pointer=boss_pointer + 1,
                milliseconds=1_200,
            )
        )
        self.assertIsNone(parser.active_boss_entity_id)
        self.assertFalse(any(kind == "profile" for kind, _value in first_updates))

        second_updates = parser.process(
            timed_packet(
                "OnMsgCreateLUnitSpellAgent",
                [
                    {
                        "$map": [
                            [0, attackers[1]],
                            [2, 86_071_010],
                            [17, boss_id],
                        ]
                    }
                ],
                pointer=boss_pointer + 2,
                milliseconds=1_300,
            )
        )
        profile = next(value for kind, value in second_updates if kind == "profile")
        self.assertEqual(parser.active_boss_entity_id, boss_id)
        self.assertEqual(profile["entity_id"], boss_id)
        self.assertEqual(profile["boss_source"], "hud_signal_team_target")
        self.assertNotEqual(profile.get("name"), "首领")

        for index, attacker in enumerate(attackers, start=1):
            parser.process(
                timed_packet(
                    "OnMsgEndureExitHit",
                    [attacker],
                    pointer=boss_pointer,
                    milliseconds=1_300 + index * 10,
                )
            )
        bound = parser.process(
            timed_packet(
                "OnMsgSyncCurrentHp",
                [34_890_535.0],
                pointer=boss_pointer,
                milliseconds=1_400,
            )
        )
        self.assertEqual(parser.pointer_entities[boss_pointer], boss_id)
        self.assertEqual(parser.active_boss_pointer, boss_pointer)
        self.assertNotIn(stale_actor, parser.entity_current_hp)
        self.assertTrue(
            any(
                kind == "monster"
                and value.get("entity_id") == boss_id
                and value.get("current_hp") == 34_890_535.0
                for kind, value in bound
            )
        )

    def test_low_hp_or_one_actor_cannot_steal_player_pointer(self):
        for index, (attackers, current_hp) in enumerate(
            (([PLAYER_ID + 2_301], 25_000_000.0), ([PLAYER_ID + 2_302, PLAYER_ID + 2_303], 25_000.0))
        ):
            with self.subTest(attackers=len(attackers), current_hp=current_hp):
                parser = NetworkPacketParser()
                boss_id = MONSTER_ID + 2_300 + index
                boss_pointer = MONSTER_POINTER + 2_300 + index
                stale_actor = PLAYER_ID + 2_300 + index
                parser.party_ids.update({stale_actor, *attackers})
                parser.pointer_entities[boss_pointer] = stale_actor
                parser.process_native_boss_type(
                    {
                        "filetime_100ns": packet("", [], sequence=1)[
                            "filetime_100ns"
                        ],
                        "entity_id": boss_id,
                        "boss_type": 3,
                    }
                )
                for offset, attacker in enumerate(attackers, start=2):
                    parser._record_entity_hit(
                        boss_id,
                        attacker,
                        86_051_010 + offset,
                        packet("", [], sequence=offset),
                    )
                    parser._record_target_hit(
                        boss_pointer,
                        attacker,
                        packet("", [], sequence=offset),
                    )
                parser.process(
                    packet(
                        "OnMsgSyncCurrentHp",
                        [current_hp],
                        pointer=boss_pointer,
                        sequence=10,
                    )
                )
                self.assertEqual(parser.pointer_entities[boss_pointer], stale_actor)

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

    def test_confirmed_small_monster_damage_is_filtered_before_ui_queue(self):
        parser = NetworkPacketParser()
        small_monster_id = MONSTER_ID + 20
        parser.process_native_boss_type(
            {
                "filetime_100ns": 134_321_845_085_764_050,
                "entity_id": small_monster_id,
                "template_id": 7_000_001,
                "boss_type": 1,
            }
        )
        updates = parser.process_native_damage(
            {
                "filetime_100ns": 134_321_845_085_764_054,
                "attacker_id": PLAYER_ID,
                "target_id": small_monster_id,
                "arg4_u64": 8_602_107_000_396,
                "raw_damage": 1_000,
                "damage": 1_000,
            }
        )
        event = next(value for kind, value in updates if kind == "event")

        self.assertFalse(parser.should_forward_damage_event(event))
        self.assertTrue(
            parser.should_forward_damage_event(
                {**event, "target_id": small_monster_id + 1}
            )
        )

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
        self.assertTrue(updates[0][1]["critical"])

        script_updates = parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [PLAYER_ID, MONSTER_ID, 8_602_107_000_396, 2, 2, 1702, 0, 1688, False],
            ),
            include_damage=False,
        )
        self.assertEqual([kind for kind, _value in script_updates], ["identity"])

    def test_native_damage_classifies_unbound_actor_by_complete_party_profession(self):
        parser = NetworkPacketParser(
            team_profile_cache={
                SELF_TOKEN: {"name": "本地", "profession_id": 1_200_002},
                TEAMMATE_TOKEN: {"name": "队友甲", "profession_id": 1_200_002},
                "third-token": {"name": "队友乙", "profession_id": 1_200_002},
            }
        )
        stale_ids = [PLAYER_ID + 100, PLAYER_ID + 101]
        parser.self_id = PLAYER_ID
        parser.self_token = SELF_TOKEN
        parser.self_confirmed = True
        parser.party_seen = True
        parser.party_member_count = 3
        parser.authoritative_party_tokens = {
            SELF_TOKEN,
            TEAMMATE_TOKEN,
            "third-token",
        }
        parser.party_tokens = {TEAMMATE_TOKEN, "third-token"}
        parser.party_ids = set(stale_ids)
        parser._bind_team_token(SELF_TOKEN, PLAYER_ID)
        parser._bind_team_token(TEAMMATE_TOKEN, stale_ids[0])
        parser._bind_team_token("third-token", stale_ids[1])
        # Keep the synthetic namespaces deliberately unresolved so this test
        # exercises profession classification instead of the normal rebind path.
        parser._team_hp_bindings = lambda _record: []

        updates = parser.process_native_damage(
            {
                "filetime_100ns": 134_321_845_085_764_054,
                "attacker_id": PLAYER_ID + 200,
                "target_id": MONSTER_ID,
                "arg4_u64": 8_602_107_000_396,
                "raw_damage": 1_000,
                "damage": 1_000,
            }
        )
        event = next(value for kind, value in updates if kind == "event")
        self.assertTrue(event["player_attacker"])
        self.assertTrue(event["party_attacker"])
        self.assertNotIn(event["attacker_id"], parser.party_ids)

        outsider = parser.process_native_damage(
            {
                "filetime_100ns": 134_321_845_095_764_054,
                "attacker_id": PLAYER_ID + 300,
                "target_id": MONSTER_ID,
                "arg4_u64": 8_607_103_000_396,
                "raw_damage": 1_000,
                "damage": 1_000,
            }
        )
        outsider_event = next(value for kind, value in outsider if kind == "event")
        self.assertTrue(outsider_event["player_attacker"])
        self.assertFalse(outsider_event["party_attacker"])

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
        self.assertTrue(teammate_life["death_confirmed"])

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
        self.assertTrue(revived["explicit_transition"])

        npc_death_with_killer_token = parser.process(
            packet(
                "OnMsgEntityDead",
                [84_080_713, MONSTER_ID, TEAMMATE_TOKEN],
                sequence=23,
            )
        )
        self.assertFalse(
            any(kind == "life" for kind, _value in npc_death_with_killer_token)
        )

    def test_knowledge_prohibition_proxy_death_never_counts_as_player_death(self):
        """FBD54AD8CFD46B5926: 知识禁制的代理实体死亡不是玩家死亡。"""
        parser = NetworkPacketParser()
        teammate_actor = PLAYER_ID + 680
        parser.self_id = PLAYER_ID
        parser.self_token = SELF_TOKEN
        parser.self_confirmed = True
        parser.token_actors[TEAMMATE_TOKEN] = teammate_actor
        parser.actor_tokens[teammate_actor] = TEAMMATE_TOKEN
        parser.party_tokens = {TEAMMATE_TOKEN}
        parser.party_ids = {teammate_actor}

        parser.process(
            packet(
                "OnMsgCastSkillNew",
                [88_008_031, 0, 1, 20_000_026],
                sequence=24,
            )
        )
        proxy_death = parser.process(
            packet(
                "OnMsgEntityDead",
                [0, TEAMMATE_TOKEN],
                pointer=MONSTER_POINTER + 680,
                sequence=25,
            )
        )
        self.assertFalse(any(kind == "life" for kind, _value in proxy_death))

        nonzero_dead = next(
            value
            for kind, value in parser.process(
                packet(
                    "OnUpdateTeamGroupMemberProps",
                    [
                        TEAMMATE_TOKEN,
                        {"$map": [[4, True], [5, 4_218.0], [6, 12_000.0]]},
                    ],
                    sequence=26,
                )
            )
            if kind == "life"
        )
        self.assertTrue(nonzero_dead["dead"])
        self.assertFalse(nonzero_dead["death_confirmed"])

        real_death = next(
            value
            for kind, value in parser.process(
                packet(
                    "OnUpdateTeamGroupMemberProps",
                    [
                        TEAMMATE_TOKEN,
                        {"$map": [[4, True], [5, 0.0], [6, 12_000.0]]},
                    ],
                    sequence=27,
                )
            )
            if kind == "life"
        )
        self.assertTrue(real_death["death_confirmed"])

    def test_unknown_death_token_is_not_guessed_from_recent_damage(self):
        parser = NetworkPacketParser()
        teammate_actor = PLAYER_ID + 700
        parser.self_id = PLAYER_ID
        parser.self_token = SELF_TOKEN
        parser.self_confirmed = True
        parser.token_actors[SELF_TOKEN] = PLAYER_ID
        parser.actor_tokens[PLAYER_ID] = SELF_TOKEN
        parser.token_actors[TEAMMATE_TOKEN] = teammate_actor
        parser.actor_tokens[teammate_actor] = TEAMMATE_TOKEN
        parser.party_tokens = {TEAMMATE_TOKEN}
        parser.party_ids = {teammate_actor}

        parser.process(
            packet(
                "OnMsgDamageSyncV2",
                [
                    MONSTER_ID,
                    teammate_actor,
                    88_007_443,
                    0,
                    1,
                    12_000,
                    0,
                    12_000,
                    True,
                ],
                sequence=30,
            )
        )
        death_updates = parser.process(
            packet(
                "OnMsgEntityDead",
                [0, "unmapped-scene-death-token"],
                sequence=31,
            )
        )
        self.assertFalse(any(kind == "life" for kind, _value in death_updates))

        confirmed = next(
            value
            for kind, value in parser.process(
                packet(
                    "OnUpdateTeamGroupMemberProps",
                    [TEAMMATE_TOKEN, {"$map": [[4, True], [5, 0.0]]}],
                    sequence=32,
                )
            )
            if kind == "life"
        )
        self.assertEqual(confirmed["actor_id"], teammate_actor)
        self.assertTrue(confirmed["dead"])

        revived = next(
            value
            for kind, value in parser.process(
                packet("OnMsgEntityRelive", [101, {}, {}, ""], sequence=33)
            )
            if kind == "life"
        )
        self.assertEqual(revived["actor_id"], PLAYER_ID)
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
                    [18, index % 3],
                    [25, index + 1],
                    [27, (index + 1) * 2],
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
        self.assertFalse(summary["authoritative"])
        self.assertFalse(summary["completion_confirmed"])
        self.assertEqual(
            sum(row["damage"] > 0 for row in summary["actors"]),
            10,
        )
        self.assertEqual(summary["actors"][5]["name"], "队员6")
        self.assertEqual(summary["actors"][5]["profession_id"], 1_200_006)
        self.assertEqual(summary["actors"][5]["critical_hits"], 6)
        self.assertEqual(summary["actors"][5]["damage_hits"], 12)
        self.assertEqual(summary["actors"][5]["deaths"], 2)
        self.assertEqual(
            parser.team_profile_cache[tokens[11]]["name"],
            "队员12",
        )

    def test_stage_bound_actor_cast_does_not_guess_realtime_damage(self):
        parser = NetworkPacketParser()
        gap_actor = 9_040_909_995_988
        boss_id = MONSTER_ID + 404
        boss_pointer = MONSTER_POINTER + 404
        self.assertIsNone(parse_combat_entity_id(gap_actor))

        parser.self_id = PLAYER_ID
        parser.self_confirmed = True
        profiles = {
            "gap-self-token": {
                "$map": [
                    [0, "gap-self-token"],
                    [1, PLAYER_ID],
                    [2, 64],
                    [4, 1_200_002],
                    [5, "莫雪"],
                ]
            },
            "gap-teammate-token": {
                "$map": [
                    [0, "gap-teammate-token"],
                    [1, gap_actor],
                    [2, 64],
                    [4, 1_200_006],
                    [5, "木鱼丶"],
                ]
            },
        }
        parser.process(
            packet(
                "OnMsgUpdateStageCombatStatistics",
                [
                    {
                        "$map": [
                            [0, 5_150_038],
                            [1, 1],
                            [2, "gap-actor-stage"],
                            [5, profiles],
                        ]
                    }
                ],
                sequence=1,
            )
        )
        self.assertIn(gap_actor, parser.party_ids)

        parser.confirmed_boss_entities.add(boss_id)
        parser.active_boss_entity_id = boss_id
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.entity_current_hp[boss_id] = 1_000.0
        parser.entity_max_hp[boss_id] = 1_000.0

        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [
                    {
                        "$map": [
                            [0, gap_actor],
                            [2, 86_061_110],
                            [17, boss_id],
                        ]
                    }
                ],
                pointer=MONSTER_POINTER + 405,
                sequence=2,
            )
        )
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [gap_actor],
                pointer=boss_pointer,
                sequence=3,
            )
        )
        updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [700.0],
                pointer=boss_pointer,
                sequence=4,
            )
        )
        self.assertFalse(any(kind == "event" for kind, _value in updates))
        self.assertEqual(parser.entity_current_hp[boss_id], 700.0)

        unknown_skill_instance = 8_605_101_000_396
        parser.process(
            packet(
                "OnMsgEndureExitHit",
                [unknown_skill_instance],
                pointer=boss_pointer,
                sequence=5,
            )
        )
        self.assertNotIn(
            unknown_skill_instance,
            {
                actor_id
                for actor_id, _skill_id, _timestamp in parser.pending_target_hits.get(
                    boss_pointer, []
                )
            },
        )

    def test_stage_actor_binding_cannot_be_overwritten_by_hp_heuristics(self):
        parser = NetworkPacketParser()
        parser.self_id = PLAYER_ID
        parser.self_confirmed = True
        self_token = "stage-lock-self-token"
        teammate_token = "stage-lock-teammate-token"
        teammate_actor = PLAYER_ID + 100
        profiles = {
            self_token: {
                "$map": [
                    [0, self_token],
                    [1, PLAYER_ID],
                    [2, 63],
                    [4, 1_200_002],
                    [5, "莫雪"],
                ]
            },
            teammate_token: {
                "$map": [
                    [0, teammate_token],
                    [1, teammate_actor],
                    [2, 63],
                    [4, 1_200_006],
                    [5, "队友"],
                ]
            },
        }
        stage_packet = packet(
            "OnMsgUpdateStageCombatStatistics",
            [
                {
                    "$map": [
                        [0, 5_150_058],
                        [1, 1],
                        [2, "stage-lock-instance"],
                        [5, profiles],
                    ]
                }
            ],
            sequence=30,
        )

        parser.process(stage_packet)
        replacement_actor = PLAYER_ID + 200
        parser.combat_source_actors.add(replacement_actor)
        parser.actor_profession_hints[replacement_actor] = 1_200_006
        parser.entity_max_hp[replacement_actor] = 9_876.0
        parser.token_max_hp[teammate_token] = 9_876.0

        heuristic_updates = parser._team_hp_bindings(
            packet("OnMsgSyncCurrentMaxHp", [9_876.0], sequence=31)
        )

        self.assertEqual(parser.token_actors[teammate_token], teammate_actor)
        self.assertFalse(
            any(kind == "actor_merge" for kind, _value in heuristic_updates)
        )

        repeated_updates = parser.process(
            {
                **stage_packet,
                "sequence": 32,
                "filetime_100ns": int(stage_packet["filetime_100ns"]) + 2,
            }
        )
        repeated_profiles = {
            value["entity_id"]: value.get("name")
            for kind, value in repeated_updates
            if kind == "profile"
        }
        self.assertEqual(
            repeated_profiles,
            {PLAYER_ID: "莫雪", teammate_actor: "队友"},
        )

        parser.stage_bound_tokens.clear()
        parser._team_hp_bindings(
            packet("OnMsgSyncCurrentMaxHp", [9_876.0], sequence=33)
        )
        self.assertEqual(parser.token_actors[teammate_token], teammate_actor)

        parser._reset_scene_combat_bindings()
        parser.combat_source_actors.add(replacement_actor)
        parser.actor_profession_hints[replacement_actor] = 1_200_006
        parser.entity_max_hp[replacement_actor] = 9_876.0
        parser._team_hp_bindings(
            packet("OnMsgSyncCurrentMaxHp", [9_876.0], sequence=34)
        )
        self.assertEqual(parser.token_actors[teammate_token], replacement_actor)

    def test_settlement_statistics_emit_authoritative_exact_result(self):
        parser = NetworkPacketParser()
        first_actor = PLAYER_ID + 100
        second_actor = PLAYER_ID + 101
        profiles = {
            "settlement-token-1": {
                "$map": [
                    [0, "settlement-token-1"],
                    [1, first_actor],
                    [2, 63],
                    [4, 1_200_007],
                    [5, "知幻"],
                    [6, 763_225],
                    [18, 2],
                    [25, 85],
                    [27, 179],
                    [33, {86_073_010: 4}],
                    [34, {86_073_010: 191_791}],
                ]
            },
            "settlement-token-2": {
                "$map": [
                    [0, "settlement-token-2"],
                    [1, second_actor],
                    [2, 63],
                    [4, 1_200_005],
                    [5, "綠鱼"],
                    [6, 660_893],
                    [25, 150],
                    [27, 214],
                    [33, {86_053_010: 4}],
                    [34, {86_053_010: 211_718}],
                ]
            },
        }
        updates = parser.process(
            packet(
                "OnMsgSettlementCombatStatistics",
                [
                    {"$map": [[0, {}]]},
                    {
                        "$map": [
                            [0, 5_150_002],
                            [1, 1],
                            [2, "settlement-instance"],
                            [5, profiles],
                        ]
                    },
                ],
                sequence=31,
            )
        )

        summary = next(value for kind, value in updates if kind == "stage_summary")
        self.assertTrue(summary["authoritative"])
        self.assertTrue(summary["summary_id"].startswith("settlement|"))
        self.assertEqual(summary["member_count"], 2)
        self.assertEqual(
            [row["damage"] for row in summary["actors"]],
            [763_225, 660_893],
        )
        self.assertEqual(
            [row["critical_hits"] for row in summary["actors"]],
            [85, 150],
        )
        self.assertEqual(
            [row["damage_hits"] for row in summary["actors"]],
            [179, 214],
        )
        self.assertEqual(
            [row["deaths"] for row in summary["actors"]],
            [2, 0],
        )
        self.assertEqual(
            summary["actors"][0]["skills"],
            [{"skill_id": 86_073_010, "damage": 191_791, "hits": 4}],
        )
        self.assertEqual(
            summary["actors"][1]["skills"],
            [{"skill_id": 86_053_010, "damage": 211_718, "hits": 4}],
        )
        names = {
            value["entity_id"]: value["name"]
            for kind, value in updates
            if kind == "profile"
        }
        self.assertEqual(names[first_actor], "知幻")
        self.assertEqual(names[second_actor], "綠鱼")

    def test_completed_stage_update_emits_authoritative_exact_result(self):
        """Field 3=True is the game's final table even on the stage method."""
        parser = NetworkPacketParser()
        actor_id = PLAYER_ID + 109
        token = "completed-stage-token"
        updates = parser.process(
            packet(
                "OnMsgUpdateStageCombatStatistics",
                [
                    {
                        "$map": [
                            [0, 5_150_059],
                            [1, 2],
                            [2, "completed-stage-instance"],
                            [3, True],
                            [
                                5,
                                {
                                    token: {
                                        "$map": [
                                            [0, token],
                                            [1, actor_id],
                                            [4, 1_200_002],
                                            [5, "completed-stage-player"],
                                            [6, 5_832],
                                        ]
                                    }
                                },
                            ],
                        ]
                    }
                ],
                sequence=31,
            )
        )

        summary = next(value for kind, value in updates if kind == "stage_summary")
        self.assertTrue(summary["authoritative"])
        self.assertTrue(summary["completion_confirmed"])
        self.assertTrue(summary["summary_id"].startswith("settlement|"))
        self.assertEqual(summary["actors"][0]["damage"], 5_832)

    def test_settlement_uses_confirmed_token_actor_when_row_omits_actor_id(self):
        parser = NetworkPacketParser()
        first_actor = PLAYER_ID + 110
        second_actor = PLAYER_ID + 111
        parser.token_actors.update(
            {
                "settlement-token-1": first_actor,
                "settlement-token-2": second_actor,
            }
        )
        parser.actor_tokens.update(
            {
                first_actor: "settlement-token-1",
                second_actor: "settlement-token-2",
            }
        )
        profiles = {
            "settlement-token-1": {
                "$map": [
                    [0, "settlement-token-1"],
                    [4, 1_200_007],
                    [5, "知幻"],
                    [6, 763_225],
                ]
            },
            "settlement-token-2": {
                "$map": [
                    [0, "settlement-token-2"],
                    [4, 1_200_005],
                    [5, "綠鱼"],
                    [6, 660_893],
                ]
            },
        }

        updates = parser.process(
            packet(
                "OnMsgSettlementCombatStatistics",
                [
                    None,
                    {
                        "$map": [
                            [0, 5_150_002],
                            [1, 1],
                            [2, "settlement-missing-actor-ids"],
                            [5, profiles],
                        ]
                    },
                ],
                sequence=32,
            )
        )

        summary = next(value for kind, value in updates if kind == "stage_summary")
        self.assertEqual(
            [row["actor_id"] for row in summary["actors"]],
            [first_actor, second_actor],
        )
        self.assertEqual(
            [row["damage"] for row in summary["actors"]],
            [763_225, 660_893],
        )

    def test_stage_keeps_provisional_token_actors_when_rows_omit_actor_ids(self):
        parser = NetworkPacketParser()
        tokens = ["provisional-stage-token-1", "provisional-stage-token-2"]
        actors = [stable_team_actor_id(token) for token in tokens]
        parser.token_actors.update(dict(zip(tokens, actors)))
        parser.actor_tokens.update(dict(zip(actors, tokens)))
        profiles = {
            token: {
                "$map": [
                    [0, token],
                    [4, 1_200_001 + index],
                    [5, f"队员{index + 1}"],
                    [6, (index + 1) * 100_000],
                ]
            }
            for index, token in enumerate(tokens)
        }

        updates = parser.process(
            packet(
                "OnMsgUpdateStageCombatStatistics",
                [
                    {
                        "$map": [
                            [0, 5_150_058],
                            [1, 2],
                            [2, "stage-provisional-actors"],
                            [5, profiles],
                        ]
                    }
                ],
                sequence=33,
            )
        )

        party = next(value for kind, value in updates if kind == "party")
        summary = next(value for kind, value in updates if kind == "stage_summary")
        self.assertEqual(party["member_count"], 2)
        self.assertEqual(set(party["entity_ids"]), set(actors))
        self.assertEqual(
            [row["actor_id"] for row in summary["actors"]], actors
        )
        self.assertEqual(
            [row["damage"] for row in summary["actors"]],
            [100_000, 200_000],
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

    def test_common_team_statistics_emit_omitted_zero_baselines(self):
        parser = NetworkPacketParser()
        updates = parser.process(
            packet(
                "RetCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {"$map": [[4, "self"]]},
                        TEAMMATE_TOKEN: {
                            "$map": [[4, "teammate"], [5, 250_000]]
                        },
                    }
                ],
            )
        )
        team_stats = {
            value["user_token"]: value
            for kind, value in updates
            if kind == "team_stat"
        }
        self.assertEqual(team_stats[SELF_TOKEN]["absolute_damage"], 0)
        self.assertTrue(team_stats[SELF_TOKEN]["omitted_zero"])
        self.assertTrue(team_stats[SELF_TOKEN]["full_snapshot"])
        self.assertEqual(
            team_stats[TEAMMATE_TOKEN]["absolute_damage"], 250_000
        )
        self.assertFalse(team_stats[TEAMMATE_TOKEN]["omitted_zero"])
        self.assertTrue(team_stats[TEAMMATE_TOKEN]["full_snapshot"])

    def test_common_and_dirty_team_statistics_preserve_server_instance(self):
        parser = NetworkPacketParser()
        instance = 1_788_020_043
        for sequence, method in enumerate(
            (
                "RetCommonCombatStatisticsByTeam",
                "RetDirtyCommonCombatStatisticsByTeam",
            ),
            start=1,
        ):
            updates = parser.process(
                packet(
                    method,
                    [
                        {
                            SELF_TOKEN: {
                                "$map": [
                                    [4, "self"],
                                    [5, 33_786_580 + sequence],
                                    [10, instance],
                                ]
                            }
                        }
                    ],
                    sequence=sequence,
                )
            )
            team_update = next(
                value for kind, value in updates if kind == "team_stat"
            )
            self.assertEqual(
                team_update["full_snapshot"],
                method == "RetCommonCombatStatisticsByTeam",
            )
            self.assertEqual(team_update["server_time"], instance)
            self.assertEqual(
                team_update["absolute_damage"], 33_786_580 + sequence
            )

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
        self.assertTrue(party["left_team"])

    def test_self_leave_team_is_emitted_when_roster_is_already_empty(self):
        parser = NetworkPacketParser()
        parser.party_seen = True

        updates = parser.process(packet("OnMsgQuitTeamGroup", [], sequence=2))

        party = next(value for kind, value in updates if kind == "party")
        self.assertEqual(party["entity_ids"], [])
        self.assertTrue(party["authoritative"])
        self.assertTrue(party["left_team"])

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

    def test_live_projection_profiles_are_not_replaced_by_cached_roster(self):
        def scene_token(counter: int, middle: bytes, index: int) -> str:
            raw = (
                counter.to_bytes(4, "big")
                + middle
                + index.to_bytes(3, "big")
            )
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        professions = [1_200_005, 1_200_005, 1_200_006, 1_200_006]
        old_tokens = [
            scene_token(0x6A910210, b"old-a", index) for index in range(4)
        ]
        current_tokens = [
            scene_token(0x6A910220, b"new-b", index) for index in range(4)
        ]
        current_names = [
            "穆恩伊尔·投影",
            "旌旗·投影",
            "碎星碎星·投影",
            "疯狂疯狂星期四·投影",
        ]
        parser = NetworkPacketParser(
            team_profile_cache={
                token: {
                    "name": f"旧缓存{index + 1}·投影",
                    "profession_id": profession,
                    "level": 62,
                }
                for index, (token, profession) in enumerate(
                    zip(old_tokens, professions)
                )
            },
            allow_cached_projection_roster=True,
        )

        for index, (token, name, profession) in enumerate(
            zip(current_tokens, current_names, professions)
        ):
            parser.process(
                packet(
                    "OnSyncTeamGroupPropsForceRefresh",
                    [token, 10_717, 10_717, None, None, None, None],
                    sequence=100 + index * 2,
                )
            )
            parser.process(
                packet(
                    "OnMsgOtherJoinTeamGroup",
                    [
                        1,
                        2,
                        {
                            "$map": [
                                [2, token],
                                [5, name],
                                [8, profession],
                                [9, 63],
                            ]
                        },
                    ],
                    sequence=101 + index * 2,
                )
            )

        self.assertEqual(
            [parser.team_profile_cache[token]["name"] for token in current_tokens],
            current_names,
        )
        self.assertTrue(set(current_tokens).issubset(parser.live_team_profile_tokens))

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

    def test_boss_hp_drop_never_allocates_damage_to_hit_markers(self):
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
        self.assertFalse(any(kind == "event" for kind, _value in updates))
        self.assertEqual(parser.entity_current_hp[boss_id], 600.0)
        self.assertNotIn(healer_without_hits, parser.combat_source_actors)

    def test_same_hp_drop_emits_no_damage_from_either_players_view(self):
        boss_id = MONSTER_ID + 501
        boss_pointer = MONSTER_POINTER + 501
        actor_a = PLAYER_ID + 101
        actor_b = PLAYER_ID + 102
        skill_a = 86_051_010
        skill_b = 86_061_020

        def client_view(
            self_actor: int,
            remote_actor: int,
            self_skill: int,
            remote_skill: int,
            exact_damage: int,
        ) -> list[tuple[int, int]]:
            parser = NetworkPacketParser()
            parser.self_id = self_actor
            parser.self_confirmed = True
            parser.party_seen = True
            parser.party_member_count = 2
            parser.party_ids.add(remote_actor)
            parser.process_native_boss_type(
                {
                    "filetime_100ns": packet("", [], sequence=1)[
                        "filetime_100ns"
                    ],
                    "entity_id": boss_id,
                    "boss_type": 3,
                    "template_id": 7_102_834,
                }
            )
            parser.pointer_entities[boss_pointer] = boss_id
            parser.active_boss_pointer = boss_pointer
            parser.entity_current_hp[boss_id] = 10_000.0
            parser.entity_current_hp_time[boss_id] = packet(
                "", [], sequence=1
            )["filetime_100ns"]
            for sequence, (actor_id, skill_id) in enumerate(
                ((self_actor, self_skill), (remote_actor, remote_skill)),
                start=2,
            ):
                parser.process(
                    packet(
                        "OnMsgCreateLUnitSpellAgent",
                        [
                            {
                                "$map": [
                                    [0, actor_id],
                                    [2, skill_id],
                                    [17, boss_id],
                                ]
                            }
                        ],
                        pointer=90_100 + sequence,
                        sequence=sequence,
                    )
                )
            parser.process(
                packet(
                    "OnMsgEndureExitHit",
                    [remote_actor],
                    pointer=boss_pointer,
                    sequence=4,
                )
            )
            parser.process_native_damage(
                {
                    "attacker_id": self_actor,
                    "target_id": boss_id,
                    "arg4_u64": self_skill * 10,
                    "raw_damage": exact_damage,
                    "damage": exact_damage,
                    "local_player_id": self_actor,
                    "filetime_100ns": packet("", [], sequence=5)[
                        "filetime_100ns"
                    ],
                }
            )
            updates = parser.process(
                packet(
                    "OnMsgSyncCurrentHp",
                    [9_000.0],
                    pointer=boss_pointer,
                    sequence=6,
                )
            )
            return sorted(
                (
                    int(event["attacker_id"]),
                    int(event["damage"]),
                )
                for kind, event in updates
                if kind == "event"
            )

        from_a = client_view(actor_a, actor_b, skill_a, skill_b, 900)
        from_b = client_view(actor_b, actor_a, skill_b, skill_a, 100)
        self.assertEqual(from_a, from_b)
        self.assertEqual(from_a, [])

    def test_twelve_player_hp_drop_never_creates_observer_dependent_damage(self):
        """The numbered v0.0.10 feedbacks must not produce guessed totals."""
        boss_id = MONSTER_ID + 502
        boss_pointer = MONSTER_POINTER + 502
        actors = [PLAYER_ID + 200 + index for index in range(12)]
        skills = [86_010_010 + index * 10_000 for index in range(12)]

        def client_view(self_index: int) -> list[tuple[int, int]]:
            parser = NetworkPacketParser()
            self_actor = actors[self_index]
            parser.self_id = self_actor
            parser.self_confirmed = True
            parser.party_seen = True
            parser.party_member_count = 12
            parser.party_ids.update(actor for actor in actors if actor != self_actor)
            parser.process_native_boss_type(
                {
                    "filetime_100ns": packet("", [], sequence=1)[
                        "filetime_100ns"
                    ],
                    "entity_id": boss_id,
                    "boss_type": 3,
                    "template_id": 7_103_401,
                }
            )
            parser.pointer_entities[boss_pointer] = boss_id
            parser.active_boss_pointer = boss_pointer
            parser.entity_current_hp[boss_id] = 180_000.0
            parser.entity_current_hp_time[boss_id] = packet(
                "", [], sequence=1
            )["filetime_100ns"]

            callback_order = actors[self_index:] + actors[:self_index]
            for sequence, actor_id in enumerate(callback_order, start=2):
                actor_index = actors.index(actor_id)
                parser.process(
                    packet(
                        "OnMsgCreateLUnitSpellAgent",
                        [
                            {
                                "$map": [
                                    [0, actor_id],
                                    [2, skills[actor_index]],
                                    [17, boss_id],
                                ]
                            }
                        ],
                        pointer=91_000 + sequence,
                        sequence=sequence,
                    )
                )

            sequence = 20
            for actor_id in reversed(callback_order):
                if actor_id == self_actor:
                    continue
                parser.process(
                    packet(
                        "OnMsgEndureExitHit",
                        [actor_id],
                        pointer=boss_pointer,
                        sequence=sequence,
                    )
                )
                sequence += 1

            parser.process_native_damage(
                {
                    "attacker_id": self_actor,
                    "target_id": boss_id,
                    "arg4_u64": skills[self_index] * 10,
                    # Each observer reports a deliberately different exact
                    # local value, matching the shape of the submitted records.
                    "raw_damage": 1_000 + self_index * 7_777,
                    "damage": 1_000 + self_index * 7_777,
                    "local_player_id": self_actor,
                    "filetime_100ns": packet("", [], sequence=40)[
                        "filetime_100ns"
                    ],
                }
            )
            updates = parser.process(
                packet(
                    "OnMsgSyncCurrentHp",
                    [60_000.0],
                    pointer=boss_pointer,
                    sequence=41,
                )
            )
            return sorted(
                (int(event["attacker_id"]), int(event["damage"]))
                for kind, event in updates
                if kind == "event"
            )

        for self_index in range(12):
            with self.subTest(self_actor=actors[self_index]):
                self.assertEqual(client_view(self_index), [])

    def test_first_boss_hp_drop_does_not_backfill_damage_from_full_hp(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 505
        boss_pointer = MONSTER_POINTER + 505
        teammate_id = PLAYER_ID + 505
        skill_id = 87_001_110
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
                "template_id": 7_102_403,
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        max_time = packet("", [], sequence=2)["filetime_100ns"]
        parser.process(
            packet(
                "OnMsgSyncDirtyFightAttributes",
                [{"$map": [[21, 33_270_350.0]]}],
                pointer=boss_pointer,
                sequence=2,
            )
        )
        self.assertEqual(parser.entity_max_hp_time[boss_id], max_time)
        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [{"$map": [[0, teammate_id], [2, skill_id], [17, boss_id]]}],
                pointer=99_505,
                sequence=3,
            )
        )
        hit = packet(
            "OnMsgEndureExitHit",
            [teammate_id],
            pointer=boss_pointer,
            sequence=4,
        )
        hit["filetime_100ns"] = max_time + 10_000_000
        parser.process(
            {
                **hit,
                "method": "OnMsgSyncFightMode",
                "decoded_arguments": [2],
            }
        )
        parser.process(hit)
        hp = packet(
            "OnMsgSyncCurrentHp",
            [33_240_277.0],
            pointer=boss_pointer,
            sequence=5,
        )
        hp["filetime_100ns"] = hit["filetime_100ns"]

        updates = parser.process(hp)

        self.assertFalse(any(kind == "event" for kind, _value in updates))
        self.assertEqual(parser.entity_current_hp[boss_id], 33_240_277.0)

    def test_first_hp_does_not_backfill_recent_or_midfight_max_hp(self):
        cases = (
            (1_000_000, 33_240_277.0),
            (10_000_000, 16_000_000.0),
        )
        for index, (max_age, current_hp) in enumerate(cases):
            with self.subTest(max_age=max_age, current_hp=current_hp):
                parser = NetworkPacketParser()
                boss_id = MONSTER_ID + 506 + index
                boss_pointer = MONSTER_POINTER + 506 + index
                teammate_id = PLAYER_ID + 506 + index
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
                hp_time = packet("", [], sequence=20)["filetime_100ns"]
                parser.entity_max_hp[boss_id] = 33_270_350.0
                parser.entity_max_hp_time[boss_id] = hp_time - max_age
                parser.process(
                    packet(
                        "OnMsgCreateLUnitSpellAgent",
                        [
                            {
                                "$map": [
                                    [0, teammate_id],
                                    [2, 87_001_110],
                                    [17, boss_id],
                                ]
                            }
                        ],
                        pointer=99_506 + index,
                        sequence=2,
                    )
                )
                hit = packet(
                    "OnMsgEndureExitHit",
                    [teammate_id],
                    pointer=boss_pointer,
                    sequence=3,
                )
                hit["filetime_100ns"] = hp_time
                parser.process(
                    {
                        **hit,
                        "method": "OnMsgSyncFightMode",
                        "decoded_arguments": [2],
                    }
                )
                parser.process(hit)
                hp = packet(
                    "OnMsgSyncCurrentHp",
                    [current_hp],
                    pointer=boss_pointer,
                    sequence=4,
                )
                hp["filetime_100ns"] = hp_time

                updates = parser.process(hp)

                self.assertFalse(any(kind == "event" for kind, _value in updates))
                self.assertEqual(parser.entity_current_hp[boss_id], current_hp)

    def test_transient_tiny_boss_hp_sample_does_not_create_fake_team_damage(self):
        """A corrupt HP decode must not turn one teammate hit into 20M damage."""
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 510
        boss_pointer = MONSTER_POINTER + 510
        teammate_id = PLAYER_ID + 510

        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
                "template_id": 7_102_834,
            }
        )
        parser.process(
            packet(
                "OnMsgCreateLUnitSpellAgent",
                [{"$map": [[0, teammate_id], [2, 86_020_050], [17, boss_id]]}],
                pointer=90_510,
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
                [20_875_602.0],
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

        corrupt_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [2_011.0],
                pointer=boss_pointer,
                sequence=7,
            )
        )
        self.assertFalse(
            any(
                kind == "event" and value["function"].endswith("/team-hit")
                for kind, value in corrupt_updates
            )
        )
        self.assertFalse(
            any(
                kind == "monster" and value.get("current_hp") == 2_011.0
                for kind, value in corrupt_updates
            )
        )
        self.assertEqual(parser.entity_current_hp[boss_id], 20_875_602.0)

        recovered_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [20_868_585.0],
                pointer=boss_pointer,
                sequence=8,
            )
        )
        inferred = [
            value
            for kind, value in recovered_updates
            if kind == "event" and value["function"].endswith("/team-hit")
        ]
        self.assertEqual(inferred, [])
        self.assertEqual(parser.entity_current_hp[boss_id], 20_868_585.0)

    def test_malformed_hp_shape_and_transient_rise_do_not_change_boss_baseline(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 511
        boss_pointer = MONSTER_POINTER + 511
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=1)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )
        parser.pointer_entities[boss_pointer] = boss_id
        parser.active_boss_pointer = boss_pointer
        parser.entity_current_hp[boss_id] = 600_000.0
        parser.entity_max_hp[boss_id] = 20_000_000.0

        malformed_current = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [3_671_652.0, 0],
                pointer=boss_pointer,
                sequence=2,
            )
        )
        malformed_pair = parser.process(
            packet(
                "OnMsgSyncCurrentMaxHp",
                [3_671_652.0, 0],
                pointer=boss_pointer,
                sequence=3,
            )
        )
        transient_rise = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [3_671_652.0],
                pointer=boss_pointer,
                sequence=4,
            )
        )
        self.assertEqual(malformed_current, [])
        self.assertEqual(malformed_pair, [])
        self.assertFalse(any(kind == "monster" for kind, _value in transient_rise))
        self.assertEqual(parser.entity_current_hp[boss_id], 600_000.0)

        full_reset = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [20_000_000.0],
                pointer=boss_pointer,
                sequence=5,
            )
        )
        reset_candidate = next(
            value
            for kind, value in full_reset
            if kind == "monster" and "reset_candidate_hp" in value
        )
        self.assertEqual(reset_candidate["reset_candidate_hp"], 20_000_000.0)
        self.assertNotIn("current_hp", reset_candidate)
        self.assertEqual(parser.entity_current_hp[boss_id], 600_000.0)

        normal = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [550_000.0],
                pointer=boss_pointer,
                sequence=6,
            )
        )
        monster = next(value for kind, value in normal if kind == "monster")
        self.assertEqual(monster["current_hp"], 550_000.0)
        self.assertEqual(parser.entity_current_hp[boss_id], 550_000.0)

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

    def test_directed_boss_casts_do_not_fabricate_missing_damage(self):
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
        self.assertEqual(inferred, [])

    def test_only_exact_native_damage_survives_partial_twelve_player_callbacks(self):
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
            {value["attacker_id"] for value in damage_events}, set(attackers[:6])
        )
        self.assertEqual(sum(value["damage"] for value in damage_events), 60)

    def test_runtime_name_recovers_boss_when_capture_starts_mid_map(self):
        parser = NetworkPacketParser(
            boss_template_catalog={},
            boss_name_allowlist=("星象仪者", "伤害木桩"),
        )
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=0)["filetime_100ns"],
                "entity_id": MONSTER_ID,
                "boss_type": 3,
            }
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
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=0)["filetime_100ns"],
                "entity_id": target_id,
                "boss_type": 3,
            }
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

    def test_hit_callback_and_hp_drop_do_not_create_teammate_damage(self):
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
        self.assertFalse(any(kind == "event" for kind, _value in updates))
        self.assertEqual(parser.entity_current_hp[boss_id], 900.0)

    def test_boss_hp_stream_replaces_stale_player_pointer_binding(self):
        parser = NetworkPacketParser()
        boss_id = MONSTER_ID + 750
        boss_pointer = MONSTER_POINTER + 750
        stale_actor = PLAYER_ID + 750
        attackers = [PLAYER_ID + 751, PLAYER_ID + 752]
        stale_token = "stale-pointer-player-token"
        parser.party_seen = True
        parser.party_member_count = 3
        parser.party_ids.update({stale_actor, *attackers})
        parser.token_actors[stale_token] = stale_actor
        parser.actor_tokens[stale_actor] = stale_token
        parser.token_max_hp[stale_token] = 13_511.0

        parser.process(
            packet(
                "OnMsgActorBuffStateSync",
                [5, 1, stale_actor],
                pointer=boss_pointer,
                sequence=1,
            )
        )
        self.assertEqual(parser.pointer_entities[boss_pointer], stale_actor)
        parser.process_native_boss_type(
            {
                "filetime_100ns": packet("", [], sequence=2)["filetime_100ns"],
                "entity_id": boss_id,
                "boss_type": 3,
            }
        )

        for index, attacker in enumerate(attackers, start=3):
            parser.process(
                packet(
                    "OnMsgCreateLUnitSpellAgent",
                    [
                        {
                            "$map": [
                                [0, attacker],
                                [2, 86_051_010 + index],
                                [17, boss_id],
                            ]
                        }
                    ],
                    pointer=boss_pointer + index,
                    sequence=index,
                )
            )
            parser.process(
                packet(
                    "OnMsgEndureExitHit",
                    [attacker],
                    pointer=boss_pointer,
                    sequence=index + 2,
                )
            )

        bound = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [25_946_241.0],
                pointer=boss_pointer,
                sequence=8,
            )
        )
        self.assertEqual(parser.pointer_entities[boss_pointer], boss_id)
        self.assertEqual(parser.active_boss_pointer, boss_pointer)
        self.assertNotIn(stale_actor, parser.entity_current_hp)
        self.assertTrue(
            any(
                kind == "monster"
                and value.get("entity_id") == boss_id
                and value.get("current_hp") == 25_946_241.0
                for kind, value in bound
            )
        )

        for index, attacker in enumerate(attackers, start=9):
            parser.process(
                packet(
                    "OnMsgCreateLUnitSpellAgent",
                    [
                        {
                            "$map": [
                                [0, attacker],
                                [2, 86_051_020 + index],
                                [17, boss_id],
                            ]
                        }
                    ],
                    pointer=boss_pointer + index,
                    sequence=index,
                )
            )
            parser.process(
                packet(
                    "OnMsgEndureExitHit",
                    [attacker],
                    pointer=boss_pointer,
                    sequence=index + 2,
                )
            )
        damage_updates = parser.process(
            packet(
                "OnMsgSyncCurrentHp",
                [24_946_241.0],
                pointer=boss_pointer,
                sequence=14,
            )
        )
        events = [value for kind, value in damage_updates if kind == "event"]
        self.assertEqual(events, [])
        self.assertEqual(parser.entity_current_hp[boss_id], 24_946_241.0)

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
