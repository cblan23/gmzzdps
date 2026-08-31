#!/usr/bin/env python3

from __future__ import annotations

import json
import unittest

from research_v0015_protocol import anonymous_summary


class AnonymousProtocolEvidenceTests(unittest.TestCase):
    def test_anonymous_summary_keeps_exact_fields_without_identity(self):
        actor_id = 57_266_949_828_970
        token = "private-user-token"
        stage = {
            "$map": [
                [0, 5_150_040],
                [
                    5,
                    {
                        token: {
                            "$map": [
                                [1, actor_id],
                                [5, "隐私角色名"],
                                [6, 1_000],
                                [7, 90],
                                [17, 300],
                                [33, {"$map": [[86_021_030, 4]]}],
                                [34, {"$map": [[86_021_030, 900]]}],
                                [35, {"$map": [[86_021_100, 300]]}],
                                [36, {token: 90}],
                                [37, {token: 1}],
                            ]
                        }
                    },
                ],
            ]
        }
        report = {
            "source_files": ["capture.jsonl"],
            "method_counts": {"OnMsgSettlementCombatStatistics": 1},
            "damage": [
                {
                    "actor_id": actor_id,
                    "target_id": actor_id + 1,
                    "events": 2,
                    "damage": 1_000,
                }
            ],
            "healing": [
                {
                    "actor_id": actor_id,
                    "target_id": actor_id + 2,
                    "events": 2,
                    "attempted": 500,
                    "effective": 300,
                    "overheal": 200,
                }
            ],
            "invalid_heal_count": 0,
            "statistics_records": [
                {
                    "method": "OnMsgSettlementCombatStatistics",
                    "arguments": [{}, stage],
                }
            ],
        }

        evidence = anonymous_summary(report)
        serialized = json.dumps(evidence, ensure_ascii=False)

        self.assertNotIn(str(actor_id), serialized)
        self.assertNotIn(token, serialized)
        self.assertNotIn("隐私角色名", serialized)
        self.assertEqual(evidence["healing"]["attempted"], 500)
        self.assertEqual(evidence["healing"]["effective"], 300)
        row = evidence["settlement_rows"][0]
        self.assertEqual(row["skill_damage_total_field_34"], 900)
        self.assertEqual(row["skill_healing_total_field_35"], 300)
        self.assertTrue(
            evidence["verified_invariants"]["field_17_equals_field_35"]
        )
        self.assertTrue(
            evidence["verified_invariants"][
                "field_36_equals_field_7_for_every_row"
            ]
        )


if __name__ == "__main__":
    unittest.main()
