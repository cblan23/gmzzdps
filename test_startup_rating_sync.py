import unittest
from unittest.mock import Mock, patch
from network_state import NetworkPacketParser
from test_combat_model import CombatModel, DpsWindow
from test_network_state import SELF_TOKEN, TEAMMATE_TOKEN, PLAYER_ID, packet


class StartupRatingTests(unittest.TestCase):
    def test_feedback_fb8d_late_start_clear_keeps_all_named_rating_rows(self):
        tokens = [SELF_TOKEN] + [
            f"fb8d-existing-member-{index:02d}" for index in range(10)
        ]
        late_token = "fb8d-late-member"
        ratings = {
            token: 80_000 + index * 100
            for index, token in enumerate([*tokens, late_token])
        }
        cache = {
            token: {
                "name": f"已确认队员{index + 1}",
                "profession_id": 1_200_001 + index % 7,
                "extraordinary_rating": ratings[token],
            }
            for index, token in enumerate(tokens)
        }
        parser = NetworkPacketParser(
            cache, remembered_self_token=SELF_TOKEN
        )

        updates = parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    PLAYER_ID + 900,
                    {
                        "$map": [
                            [2, late_token],
                            [5, "临时加入队员"],
                            [6, 912],
                            [8, 1_200_005],
                            [9, 69],
                            [27, ratings[late_token]],
                        ]
                    },
                ],
                sequence=1,
            )
        )
        common_rows = {
            token: {
                "$map": [
                    [3, 1_200_001 + index % 7],
                    [4, f"已确认队员{index + 1}"],
                    [5, 1_000 + index],
                ]
            }
            for index, token in enumerate(tokens)
        }
        common_rows[late_token] = {
            "$map": [[3, 1_200_005], [4, "临时加入队员"], [5, 2_000]]
        }
        updates.extend(
            parser.process(
                packet(
                    "RetCommonCombatStatisticsByTeam",
                    [common_rows],
                    sequence=2,
                )
            )
        )

        model = CombatModel(run_id="feedback-fb8d-rating-roster")
        handlers = {
            "identity": model.ingest_identity,
            "party": model.ingest_party,
            "profile": model.ingest_profile,
            "team_stat": model.ingest_team_stat,
        }
        for kind, payload in updates:
            handler = handlers.get(kind)
            if handler is not None:
                handler(payload)

        window = object.__new__(DpsWindow)
        window.model = model
        window.team_rating_preview_enabled = True
        window.team_rating_preview_rows_dirty = True
        window.team_rating_preview_rows_cache = []
        window.team_rating_preview_profile_cache = {}
        window.team_rating_preview_profile_signatures = {}
        window.team_rating_preview_roster_signature = None
        # Clicking clear should return to the same complete rating roster; it
        # must not synthesize anonymous 玩家8/玩家9 rows.
        window._main_display_is_cleared = lambda: True

        self.assertTrue(window._team_rating_preview_active())
        rows = window._team_rating_preview_rows()
        self.assertEqual(len(rows), 12)
        self.assertEqual(
            {row["display_name"] for row in rows},
            {
                *(f"已确认队员{index + 1}" for index in range(11)),
                "临时加入队员",
            },
        )
        self.assertEqual(
            {row["extraordinary_rating"] for row in rows},
            {None, ratings[late_token]},
        )
        self.assertFalse(
            any(
                str(row["display_name"]).startswith("玩家")
                for row in rows
            )
        )

    def test_late_start_bootstraps_preexisting_members_for_six_and_twelve_player_teams(self):
        for member_count in (6, 12):
            with self.subTest(member_count=member_count):
                self_name = "\u672c\u673a\u73a9\u5bb6"
                late_name = "\u65b0\u5165\u961f\u5458"
                old_tokens = [
                    f"startup-old-member-{index:02d}"
                    for index in range(member_count - 2)
                ]
                late_token = f"startup-late-member-{member_count:02d}"
                cache = {
                    SELF_TOKEN: {
                        "name": self_name,
                        "profession_id": 1_200_002,
                        "extraordinary_rating": 80_000,
                    },
                    **{
                        token: {
                            "name": f"\u539f\u961f\u5458{index + 1}",
                            "profession_id": 1_200_003 + index % 5,
                            "extraordinary_rating": 81_000 + index,
                        }
                        for index, token in enumerate(old_tokens)
                    },
                }
                parser = NetworkPacketParser(
                    cache, remembered_self_token=SELF_TOKEN
                )
                parser.process(
                    packet(
                        "OnMsgOtherJoinTeamGroup",
                        [
                            0,
                            PLAYER_ID + 500,
                            {
                                "$map": [
                                    [2, late_token],
                                    [5, late_name],
                                    [6, 900 + member_count],
                                    [8, 1_200_007],
                                    [9, 69],
                                    [27, 90_000],
                                ]
                            },
                        ],
                        sequence=1,
                    )
                )

                rows = {
                    SELF_TOKEN: {
                        "$map": [[3, 1_200_002], [4, self_name], [5, 100]]
                    },
                    late_token: {
                        "$map": [[3, 1_200_007], [4, late_name], [5, 200]]
                    },
                }
                rows.update(
                    {
                        token: {
                            "$map": [
                                [3, 1_200_003 + index % 5],
                                [4, f"\u539f\u961f\u5458{index + 1}"],
                                [5, 300 + index],
                            ]
                        }
                        for index, token in enumerate(old_tokens)
                    }
                )
                parser.process(
                    packet(
                        "RetCommonCombatStatisticsByTeam",
                        [rows],
                        sequence=2,
                    )
                )

                expected_tokens = {SELF_TOKEN, late_token, *old_tokens}
                self.assertEqual(
                    parser.authoritative_party_tokens, expected_tokens
                )
                self.assertEqual(
                    parser.party_tokens, expected_tokens - {SELF_TOKEN}
                )
                self.assertEqual(parser.party_member_count, member_count)
                self.assertEqual(
                    parser.entity_profiles[
                        parser.token_actors[late_token]
                    ]["extraordinary_rating"],
                    90_000,
                )
                for index, token in enumerate(old_tokens):
                    self.assertNotIn(
                        "extraordinary_rating",
                        parser.entity_profiles[parser.token_actors[token]],
                    )

    def test_pending_rating_is_attached_when_snapshot_discovers_old_member(self):
        self_name = "\u672c\u673a\u73a9\u5bb6"
        old_name = "\u539f\u961f\u5458"
        new_name = "\u65b0\u5165\u961f\u5458"
        old_token = "startup-uncached-member"
        late_token = "startup-new-member-token"
        parser = NetworkPacketParser(
            {
                SELF_TOKEN: {
                    "name": self_name,
                    "profession_id": 1_200_002,
                }
            },
            remembered_self_token=SELF_TOKEN,
        )
        parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    PLAYER_ID + 501,
                    {
                        "$map": [
                            [2, late_token],
                            [5, new_name],
                            [6, 901],
                            [8, 1_200_004],
                            [27, 91_000],
                        ]
                    },
                ],
                sequence=1,
            )
        )
        parser.process(
            packet(
                "OnUpdateTeamGroupMemberProps",
                [old_token, {"$map": [[11, 88_888]]}],
                sequence=2,
            )
        )

        parser.process(
            packet(
                "RetCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {
                            "$map": [[3, 1_200_002], [4, self_name]]
                        },
                        old_token: {
                            "$map": [[3, 1_200_003], [4, old_name]]
                        },
                        late_token: {
                            "$map": [[3, 1_200_004], [4, new_name]]
                        },
                    }
                ],
                sequence=3,
            )
        )

        old_actor = parser.token_actors[old_token]
        self.assertEqual(
            parser.entity_profiles[old_actor]["extraordinary_rating"], 88_888
        )
        self.assertNotIn(old_token, parser.pending_team_ratings)

    def test_incomplete_bootstrap_does_not_resurrect_explicit_departure(self):
        self_name = "\u672c\u673a\u73a9\u5bb6"
        departed_token = "startup-departed-member"
        old_token = "startup-still-present-member"
        parser = NetworkPacketParser(
            {
                SELF_TOKEN: {
                    "name": self_name,
                    "profession_id": 1_200_002,
                }
            },
            remembered_self_token=SELF_TOKEN,
        )
        parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    PLAYER_ID + 502,
                    {
                        "$map": [
                            [2, departed_token],
                            [5, "departed-player"],
                            [6, 902],
                            [8, 1_200_003],
                        ]
                    },
                ],
                sequence=1,
            )
        )
        parser.process(
            packet(
                "OnMsgOtherLeaveTeamGroup", [departed_token], sequence=2
            )
        )
        updates = parser.process(
            packet(
                "RetCommonCombatStatisticsByTeam",
                [
                    {
                        SELF_TOKEN: {"$map": [[4, self_name]]},
                        departed_token: {
                            "$map": [[4, "departed-player"], [5, 500]]
                        },
                        old_token: {
                            "$map": [[4, "still-present-player"], [5, 600]]
                        },
                    }
                ],
                sequence=3,
            )
        )

        self.assertNotIn(departed_token, parser.party_tokens)
        self.assertNotIn(departed_token, parser.token_actors)
        self.assertIn(old_token, parser.party_tokens)
        self.assertFalse(
            any(
                kind == "team_stat"
                and value.get("user_token") == departed_token
                for kind, value in updates
            )
        )

    def test_stage_snapshot_can_complete_roster_after_late_start_increment(self):
        self_name = "\u672c\u673a\u73a9\u5bb6"
        old_name = "\u539f\u961f\u5458"
        new_name = "\u65b0\u5165\u961f\u5458"
        old_token = "startup-stage-old-member"
        late_token = "startup-stage-new-member"
        parser = NetworkPacketParser(
            {
                SELF_TOKEN: {
                    "name": self_name,
                    "profession_id": 1_200_002,
                    "extraordinary_rating": 80_000,
                },
                old_token: {
                    "name": old_name,
                    "profession_id": 1_200_003,
                    "extraordinary_rating": 81_000,
                },
            }
        )
        parser.self_id = PLAYER_ID
        parser.self_confirmed = True
        parser._confirm_self_token(SELF_TOKEN, packet("", [], sequence=1))
        parser.process(
            packet(
                "OnMsgOtherJoinTeamGroup",
                [
                    0,
                    PLAYER_ID + 503,
                    {
                        "$map": [
                            [2, late_token],
                            [5, new_name],
                            [6, 903],
                            [8, 1_200_004],
                            [27, 82_000],
                        ]
                    },
                ],
                sequence=2,
            )
        )
        members = {
            SELF_TOKEN: {
                "$map": [
                    [0, SELF_TOKEN],
                    [1, PLAYER_ID],
                    [2, 69],
                    [4, 1_200_002],
                    [5, self_name],
                    [6, 100],
                ]
            },
            old_token: {
                "$map": [
                    [0, old_token],
                    [1, PLAYER_ID + 1],
                    [2, 69],
                    [4, 1_200_003],
                    [5, old_name],
                    [6, 200],
                ]
            },
            late_token: {
                "$map": [
                    [0, late_token],
                    [1, PLAYER_ID + 2],
                    [2, 69],
                    [4, 1_200_004],
                    [5, new_name],
                    [6, 300],
                ]
            },
        }

        parser.process(
            packet(
                "OnMsgUpdateStageCombatStatistics",
                [
                    {
                        "$map": [
                            [0, 5_150_058],
                            [1, 1],
                            [2, "startup-stage-instance"],
                            [5, members],
                        ]
                    }
                ],
                sequence=3,
            )
        )

        self.assertEqual(
            parser.authoritative_party_tokens,
            {SELF_TOKEN, old_token, late_token},
        )
        self.assertEqual(parser.party_tokens, {old_token, late_token})
        self.assertEqual(parser.party_member_count, 3)
        self.assertNotIn(
            "extraordinary_rating",
            parser.entity_profiles[PLAYER_ID + 1],
        )

    def test_sparse_rejoin_restores_only_exact_token_rating(self):
        parser = self.parser()
        row = packet('', [], sequence=1)
        self.assertIsNone(parser._confirmed_team_rating(TEAMMATE_TOKEN, None, row))
        self.assertIsNone(parser._confirmed_team_rating('unknown-token', None, row))
        self.assertEqual(parser._confirmed_team_rating(TEAMMATE_TOKEN, 91234, row), 91234)

    def test_fresh_peer_rating_is_preserved_in_token_cache(self):
        parser = self.parser()
        parser._bind_team_token(TEAMMATE_TOKEN, PLAYER_ID+1)
        parser.party_tokens.add(TEAMMATE_TOKEN)
        parser.process(packet('OnUpdateTeamGroupMemberProps', [TEAMMATE_TOKEN, {11: 92345}], sequence=1))
        self.assertEqual(parser.team_profile_cache[TEAMMATE_TOKEN]['extraordinary_rating'], 92345)
        parser.live_team_property_ratings.clear()
        self.assertIsNone(parser._confirmed_team_rating(TEAMMATE_TOKEN, None, packet('', [], sequence=2)))

    def test_disk_rating_is_hidden_until_current_session_confirms_it(self):
        parser = NetworkPacketParser({
            TEAMMATE_TOKEN: {
                'name': '墨爵',
                'profession_id': 1200002,
                'extraordinary_rating': 83231,
            }
        })
        actor_id = parser._bind_team_token(TEAMMATE_TOKEN, PLAYER_ID + 1)
        parser.party_tokens.add(TEAMMATE_TOKEN)
        parser.party_ids.add(actor_id)
        initial = parser._profile_update(
            actor_id, packet('', [], sequence=1),
            user_token=TEAMMATE_TOKEN, entity_type='Player',
        )
        self.assertIsNotNone(initial)
        self.assertNotIn('extraordinary_rating', parser.entity_profiles[actor_id])

        updates = parser.process(packet(
            'OnUpdateTeamGroupMemberProps',
            [TEAMMATE_TOKEN, {11: 88893}], sequence=2,
        ))
        self.assertEqual(parser.live_team_property_ratings[TEAMMATE_TOKEN], 88893)
        self.assertEqual(parser.entity_profiles[actor_id]['extraordinary_rating'], 88893)
        self.assertTrue(any(
            kind == 'profile' and value.get('extraordinary_rating') == 88893
            for kind, value in updates
        ))

        parser.process(packet(
            'OnUpdateTeamGroupMemberProps',
            [TEAMMATE_TOKEN, {5: 16720, 6: 16720}], sequence=3,
        ))
        self.assertEqual(parser.entity_profiles[actor_id]['extraordinary_rating'], 88893)

    def test_confirmed_self_does_not_show_disk_rating_until_live_refresh(self):
        parser = NetworkPacketParser(
            {
                SELF_TOKEN: {
                    'name': '莫雪',
                    'profession_id': 1200002,
                    'extraordinary_rating': 82929,
                },
                TEAMMATE_TOKEN: {
                    'name': '墨爵',
                    'profession_id': 1200003,
                    'extraordinary_rating': 91444,
                },
            },
            remembered_self_token=SELF_TOKEN,
        )
        parser.self_token = SELF_TOKEN
        parser.self_id = PLAYER_ID
        parser.self_confirmed = True

        self.assertNotIn('extraordinary_rating', parser._current_session_team_profile(SELF_TOKEN))
        self.assertNotIn(
            'extraordinary_rating',
            parser._current_session_team_profile(TEAMMATE_TOKEN),
        )

        parser.live_team_property_ratings[SELF_TOKEN] = 83555
        self.assertEqual(
            parser._current_session_team_profile(SELF_TOKEN)[
                'extraordinary_rating'
            ],
            83555,
        )

    def test_late_start_hp_heartbeat_bootstraps_teammate_without_stale_rating(self):
        parser = NetworkPacketParser(
            {
                SELF_TOKEN: {
                    'name': '\u672c\u673a\u73a9\u5bb6',
                    'profession_id': 1200006,
                    'extraordinary_rating': 82791,
                },
                TEAMMATE_TOKEN: {
                    'name': '\u665a\u542f\u52a8\u961f\u53cb',
                    'profession_id': 1200003,
                    'extraordinary_rating': 78056,
                },
            },
            remembered_self_token=SELF_TOKEN,
        )
        parser._confirm_local_actor(
            PLAYER_ID, packet('', [], sequence=1), native=True
        )
        parser._confirm_self_token(
            SELF_TOKEN, packet('', [], sequence=2)
        )

        updates = parser.process(
            packet(
                'OnUpdateTeamGroupMemberProps',
                [TEAMMATE_TOKEN, {4: False, 5: 16583, 6: 16583}],
                sequence=3,
            )
        )

        actor_id = parser.token_actors[TEAMMATE_TOKEN]
        self.assertIn(TEAMMATE_TOKEN, parser.party_tokens)
        self.assertIn(TEAMMATE_TOKEN, parser.other_party_tokens)
        self.assertIn(actor_id, parser.party_ids)
        self.assertEqual(parser.party_member_count, 2)
        self.assertEqual(
            parser.entity_profiles[actor_id]['name'], '\u665a\u542f\u52a8\u961f\u53cb'
        )
        self.assertEqual(
            parser.entity_profiles[actor_id]['profession_id'], 1200003
        )
        self.assertNotIn(
            'extraordinary_rating', parser.entity_profiles[actor_id]
        )
        self.assertTrue(
            any(
                kind == 'party'
                and TEAMMATE_TOKEN in value.get('user_tokens', [])
                and value.get('member_count') == 2
                for kind, value in updates
            )
        )
        self.assertTrue(
            any(
                kind == 'life'
                and value.get('actor_id') == actor_id
                and value.get('current_hp') == 16583.0
                and value.get('max_hp') == 16583.0
                for kind, value in updates
            )
        )

        rating_updates = parser.process(
            packet(
                'OnUpdateTeamGroupMemberProps',
                [TEAMMATE_TOKEN, {11: 88893}],
                sequence=4,
            )
        )
        self.assertEqual(
            parser.entity_profiles[actor_id]['extraordinary_rating'], 88893
        )
        self.assertTrue(
            any(
                kind == 'profile'
                and value.get('extraordinary_rating') == 88893
                for kind, value in rating_updates
            )
        )

    def test_late_start_live_power_fills_heartbeat_teammate_rating(self):
        parser = NetworkPacketParser(
            {
                SELF_TOKEN: {
                    'name': '\u672c\u673a\u73a9\u5bb6',
                    'profession_id': 1_200_002,
                    'extraordinary_rating': 82_928,
                },
                TEAMMATE_TOKEN: {
                    'name': '\u58a8\u7235',
                    'profession_id': 1_200_003,
                    # A persisted value must not masquerade as the current one.
                    'extraordinary_rating': 83_428,
                },
            },
            remembered_self_token=SELF_TOKEN,
        )
        parser._confirm_local_actor(
            PLAYER_ID, packet('', [], sequence=1), native=True
        )
        parser._confirm_self_token(
            SELF_TOKEN, packet('', [], sequence=2)
        )
        parser.process(
            packet(
                'OnUpdateTeamGroupMemberProps',
                [TEAMMATE_TOKEN, {4: False, 5: 16_720, 6: 16_720}],
                sequence=3,
            )
        )

        actor_id = parser.token_actors[TEAMMATE_TOKEN]
        self.assertNotIn(
            'extraordinary_rating', parser.entity_profiles[actor_id]
        )
        updates = parser.apply_read_only_team_profile(
            {
                'method': 'NpcapLiveTeamProfile',
                'capture_source': 'npcap_read_only_team_profile',
                'profile_source': 'live_lua_power',
                'filetime_100ns': packet('', [], sequence=4)[
                    'filetime_100ns'
                ],
                'user_token': TEAMMATE_TOKEN,
                'name': '\u58a8\u7235',
                'profession_id': 1_200_003,
                'level': 70,
                'extraordinary_rating': 91_444,
            }
        )

        self.assertEqual(
            parser.entity_profiles[actor_id]['extraordinary_rating'], 91_444
        )
        self.assertEqual(
            parser.live_team_property_ratings[TEAMMATE_TOKEN], 91_444
        )
        self.assertEqual(
            parser.team_profile_cache[TEAMMATE_TOKEN][
                'extraordinary_rating'
            ],
            91_444,
        )
        self.assertTrue(
            any(
                kind == 'profile'
                and value.get('entity_id') == actor_id
                and value.get('extraordinary_rating') == 91_444
                for kind, value in updates
            )
        )

    def test_live_power_rejects_unproven_and_departed_tokens(self):
        stranger_token = 'AQAAAOwNstranger'
        record = {
            'method': 'NpcapLiveTeamProfile',
            'capture_source': 'npcap_read_only_team_profile',
            'profile_source': 'live_lua_power',
            'filetime_100ns': packet('', [], sequence=1)[
                'filetime_100ns'
            ],
            'user_token': stranger_token,
            'name': 'not-a-member',
            'extraordinary_rating': 99_999,
        }
        parser = NetworkPacketParser()
        self.assertEqual(parser.apply_read_only_team_profile(record), [])
        self.assertNotIn(stranger_token, parser.token_actors)

        parser.process(
            packet(
                'OnUpdateTeamGroupMemberProps',
                [TEAMMATE_TOKEN, {4: False, 5: 16_720, 6: 16_720}],
                sequence=2,
            )
        )
        parser.process(
            packet(
                'OnMsgOtherLeaveTeamGroup',
                [TEAMMATE_TOKEN],
                sequence=3,
            )
        )
        departed_record = {
            **record,
            'user_token': TEAMMATE_TOKEN,
            'name': '\u5df2\u9000\u961f\u961f\u53cb',
        }
        self.assertEqual(
            parser.apply_read_only_team_profile(departed_record), []
        )
        self.assertNotIn(
            TEAMMATE_TOKEN, parser.live_team_property_ratings
        )

    def test_server_self_rating_cannot_be_replaced_by_read_only_supplement(self):
        parser = NetworkPacketParser({}, remembered_self_token=SELF_TOKEN)
        parser.self_token, parser.self_id, parser.self_confirmed = SELF_TOKEN, PLAYER_ID, True
        parser._remember_confirmed_self_rating(SELF_TOKEN, 88893)
        updates = parser.apply_read_only_team_profile({
            'method': 'NpcapLiveTeamProfile', 'capture_source': 'npcap_read_only_team_profile',
            'user_token': SELF_TOKEN, 'name': '本人', 'extraordinary_rating': 78750,
            'client_rating_key': 'power',
        })
        self.assertEqual(parser.live_team_property_ratings[SELF_TOKEN], 88893)
        self.assertNotIn(SELF_TOKEN, parser.read_only_rating_tokens)
        self.assertTrue(any(value.get('extraordinary_rating') == 88893 for kind, value in updates))

    def test_server_packet_upgrades_read_only_rating(self):
        parser = NetworkPacketParser({})
        parser._remember_confirmed_self_rating(SELF_TOKEN, 78750, read_only=True)
        self.assertEqual(parser._confirmed_team_rating(SELF_TOKEN, 88893, packet('', [], sequence=1)), 88893)
        self.assertNotIn(SELF_TOKEN, parser.read_only_rating_tokens)

    def test_legacy_alias_cannot_be_promoted_by_a_read_only_record(self):
        parser = NetworkPacketParser({})
        parser.self_token, parser.self_id, parser.self_confirmed = SELF_TOKEN, PLAYER_ID, True
        self.assertEqual(parser.apply_read_only_team_profile({
            'capture_source': 'npcap_read_only_team_profile', 'user_token': SELF_TOKEN,
            'client_rating_key': 'ZhanLi', 'extraordinary_rating': 78750,
        }), [])
        self.assertNotIn(SELF_TOKEN, parser.live_team_property_ratings)

    def test_token_bound_actor_and_party_scores_restore_late_start_display(self):
        for key, binding in (('CEScore', 'actor_CEScore'), ('ceScore', 'team_member_ceScore')):
            with self.subTest(key=key):
                parser = NetworkPacketParser({})
                parser.self_token, parser.self_id, parser.self_confirmed = SELF_TOKEN, PLAYER_ID, True
                updates = parser.apply_read_only_team_profile({
                    'capture_source': 'npcap_read_only_team_profile', 'user_token': SELF_TOKEN,
                    'client_rating_key': key, 'client_rating_binding': binding,
                    'extraordinary_rating': 80975, 'name': '本人',
                })
                self.assertEqual(parser.entity_profiles[PLAYER_ID]['extraordinary_rating'], 80975)
                self.assertTrue(any(kind == 'profile' and value.get('extraordinary_rating') == 80975
                                    for kind, value in updates))
                parser.process(packet('OnUpdateTeamGroupSelfProps', [{11: 81100}], sequence=1))
                parser.apply_read_only_team_profile({
                    'capture_source': 'npcap_read_only_team_profile', 'user_token': SELF_TOKEN,
                    'client_rating_key': key, 'client_rating_binding': binding,
                    'extraordinary_rating': 80975,
                })
                self.assertEqual(parser.entity_profiles[PLAYER_ID]['extraordinary_rating'], 81100)

    def test_unique_local_ce_score_pair_is_self_only(self):
        parser = NetworkPacketParser({})
        parser.self_token, parser.self_id, parser.self_confirmed = SELF_TOKEN, PLAYER_ID, True
        record = {
            'capture_source': 'npcap_read_only_team_profile',
            'user_token': SELF_TOKEN,
            'client_rating_key': 'CEScore',
            'client_rating_binding': 'local_actor_CEScore_pair',
            'extraordinary_rating': 81227,
        }

        updates = parser.apply_read_only_team_profile(record)

        self.assertEqual(parser.entity_profiles[PLAYER_ID]['extraordinary_rating'], 81227)
        parser.party_tokens.add(TEAMMATE_TOKEN)
        parser._bind_team_token(TEAMMATE_TOKEN, PLAYER_ID + 1)
        self.assertEqual(
            parser.apply_read_only_team_profile(
                {**record, 'user_token': TEAMMATE_TOKEN}
            ),
            [],
        )

    def test_fresh_exact_lua_table_updates_after_equipment_switch_without_a_repeated_rpc(self):
        parser = NetworkPacketParser({})
        parser.self_token, parser.self_id, parser.self_confirmed = SELF_TOKEN, PLAYER_ID, True
        server = packet('OnUpdateTeamGroupSelfProps', [{11: 80702}], sequence=1)
        server['filetime_100ns'] = 10000
        parser.process(server)
        record = {
            'capture_source': 'npcap_read_only_team_profile', 'user_token': SELF_TOKEN,
            'client_rating_key': 'CEScore', 'client_rating_binding': 'actor_CEScore',
            'filetime_100ns': 11000, 'extraordinary_rating': 80975,
        }
        parser.apply_read_only_team_profile(record)
        self.assertEqual(parser.entity_profiles[PLAYER_ID]['extraordinary_rating'], 80975)
        self.assertEqual(parser.live_team_rating_observed_100ns[SELF_TOKEN], 11000)
        self.assertEqual(parser.apply_read_only_team_profile({**record, 'filetime_100ns': 9000,
                                                            'extraordinary_rating': 78750}), [])
        self.assertEqual(parser.live_team_rating_observed_100ns[SELF_TOKEN], 11000)
        self.assertEqual(parser.entity_profiles[PLAYER_ID]['extraordinary_rating'], 80975)
        server['filetime_100ns'] = 12000
        server['decoded_arguments'] = [{11: 81100}]
        parser.process(server)
        self.assertEqual(parser.entity_profiles[PLAYER_ID]['extraordinary_rating'], 81100)
        self.assertEqual(parser.apply_read_only_team_profile(record), [])

    def test_score_alias_requires_the_verified_layout_not_just_the_key(self):
        parser = NetworkPacketParser({})
        parser.self_token, parser.self_id, parser.self_confirmed = SELF_TOKEN, PLAYER_ID, True
        for key, binding in (('CEScore', None), ('ceScore', None), ('ceScore', 'actor_CEScore'),
                             ('ZhanLi', 'actor_CEScore')):
            self.assertEqual(parser.apply_read_only_team_profile({
                'capture_source': 'npcap_read_only_team_profile', 'user_token': SELF_TOKEN,
                'client_rating_key': key, 'client_rating_binding': binding,
                'extraordinary_rating': 78750,
            }), [])

    def parser(self):
        return NetworkPacketParser({
            SELF_TOKEN: {'name': '本人', 'profession_id': 1200002, 'extraordinary_rating': 82885},
            TEAMMATE_TOKEN: {'name': '队友', 'profession_id': 1200002, 'extraordinary_rating': 85558},
        })

    def test_fresh_self_rating_survives_later_identity_cache_restore(self):
        parser = self.parser()
        parser.process(packet('OnUpdateTeamGroupSelfProps', [{11: 85558}], sequence=1))
        parser._confirm_local_actor(PLAYER_ID, packet('', [], sequence=2), native=True)
        parser._confirm_self_token(SELF_TOKEN, packet('', [], sequence=3))
        self.assertEqual(parser.entity_profiles[PLAYER_ID]['extraordinary_rating'], 85558)
        self.assertEqual(parser.current_self_identity()['extraordinary_rating'], 85558)

    def test_matching_peer_rating_cannot_replace_confirmed_self(self):
        parser = self.parser()
        parser._confirm_local_actor(PLAYER_ID, packet('', [], sequence=1), native=True)
        parser._confirm_self_token(SELF_TOKEN, packet('', [], sequence=2))
        parser._bind_team_token(TEAMMATE_TOKEN, PLAYER_ID+1)
        parser.party_tokens.add(TEAMMATE_TOKEN)
        parser.other_party_tokens.add(TEAMMATE_TOKEN)
        parser.team_profile_markers[TEAMMATE_TOKEN] = 85558
        parser.process(packet('OnUpdateTeamGroupSelfProps', [{11: 85558}], sequence=3))
        self.assertEqual(parser.self_token, SELF_TOKEN)
        self.assertEqual(parser.entity_profiles[PLAYER_ID]['extraordinary_rating'], 85558)
        self.assertEqual(parser.current_self_identity()['name'], '本人')


class CaptureRecoveryTests(unittest.TestCase):
    @staticmethod
    def window():
        window = object.__new__(DpsWindow)
        window.closing = False
        window.authorization_resetting = False
        window.license_network_paused = False
        window.capture_started = True
        window.capture_restart_after_id = None
        window.capture_restart_attempts = 0
        window.connected = True
        window.startup_capture_pending = False
        window.startup_wait_reason = ""
        window.startup_wait_started_at = 0.0
        window.root = Mock()
        window.root.after.return_value = "after#capture-restart"
        window.dot = Mock()
        window.status_label = Mock()
        window._schedule_layered_main_render = Mock()
        return window

    def test_unexpected_capture_stop_schedules_bounded_restart(self):
        window = self.window()
        globals_dict = DpsWindow._handle_capture_worker_stopped.__globals__
        with patch.dict(
            globals_dict, {"write_capture_lifecycle_event": Mock()}
        ):
            window._handle_capture_worker_stopped(
                {"reason": "worker_exception", "requested": False}
            )

        self.assertFalse(window.capture_started)
        self.assertFalse(window.connected)
        self.assertTrue(window.startup_capture_pending)
        self.assertEqual(
            window.capture_restart_after_id, "after#capture-restart"
        )
        window.root.after.assert_called_once()

    def test_authorization_pause_never_restarts_capture(self):
        window = self.window()
        window.license_network_paused = True
        globals_dict = DpsWindow._handle_capture_worker_stopped.__globals__
        with patch.dict(
            globals_dict, {"write_capture_lifecycle_event": Mock()}
        ):
            window._handle_capture_worker_stopped(
                {"reason": "parent_stop_event", "requested": True}
            )

        self.assertFalse(window.capture_started)
        window.root.after.assert_not_called()


if __name__ == '__main__':
    unittest.main()
