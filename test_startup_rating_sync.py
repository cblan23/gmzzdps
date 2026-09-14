import unittest
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
            set(ratings.values()),
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
                    self.assertEqual(
                        parser.entity_profiles[
                            parser.token_actors[token]
                        ]["extraordinary_rating"],
                        81_000 + index,
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
        self.assertEqual(
            parser.entity_profiles[PLAYER_ID + 1]["extraordinary_rating"],
            81_000,
        )

    def test_sparse_rejoin_restores_only_exact_token_rating(self):
        parser = self.parser()
        row = packet('', [], sequence=1)
        self.assertEqual(parser._confirmed_team_rating(TEAMMATE_TOKEN, None, row), 85558)
        self.assertIsNone(parser._confirmed_team_rating('unknown-token', None, row))
        self.assertEqual(parser._confirmed_team_rating(TEAMMATE_TOKEN, 91234, row), 91234)

    def test_fresh_peer_rating_is_preserved_in_token_cache(self):
        parser = self.parser()
        parser._bind_team_token(TEAMMATE_TOKEN, PLAYER_ID+1)
        parser.party_tokens.add(TEAMMATE_TOKEN)
        parser.process(packet('OnUpdateTeamGroupMemberProps', [TEAMMATE_TOKEN, {11: 92345}], sequence=1))
        self.assertEqual(parser.team_profile_cache[TEAMMATE_TOKEN]['extraordinary_rating'], 92345)
        parser.live_team_property_ratings.clear()
        self.assertEqual(parser._confirmed_team_rating(TEAMMATE_TOKEN, None, packet('', [], sequence=2)), 92345)

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


if __name__ == '__main__':
    unittest.main()
