#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from replay_combat_log import anonymous_replay_evidence


class AnonymousReplayEvidenceTests(unittest.TestCase):
    def test_evidence_fingerprint_excludes_player_identity(self):
        actor_id = 57_266_949_828_970
        teammate_id = actor_id + 1
        encounter_id = "private-encounter-id"
        record = {
            "encounter_id": encounter_id,
            "archive_reason": "target_defeated",
            "duration_seconds": 20.5,
            "total_damage": 300,
            "monster": {"template_id": 7_107_030},
            "participants": [
                {
                    "actor_id": actor_id,
                    "name": "隐私角色名",
                    "damage": 200,
                    "targets": [{"entity_id": 99, "damage": 200}],
                    "skill_source": "exact_callbacks_with_explicit_unclassified",
                },
                {
                    "actor_id": teammate_id,
                    "name": "队友隐私名",
                    "damage": 100,
                    "targets": [{"entity_id": 0, "damage": 100}],
                    "skill_source": "server_stage_summary",
                },
            ],
            "damage_accounting": {"packet_event_total": 200},
        }
        summary = {
            "actors": [
                {"actor_id": actor_id, "damage": 200},
                {"actor_id": teammate_id, "damage": 100},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            evidence = anonymous_replay_evidence(
                source_path=source,
                line_count=1,
                records=[record],
                summaries=[summary],
                update_counts=Counter({"event": 1}),
            )

        serialized = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn(str(actor_id), serialized)
        self.assertNotIn(str(teammate_id), serialized)
        self.assertNotIn(encounter_id, serialized)
        self.assertNotIn("隐私角色名", serialized)
        self.assertNotIn("队友隐私名", serialized)
        self.assertEqual(evidence["combats"][0]["assigned_target_damage"], 200)
        self.assertEqual(evidence["combats"][0]["unassigned_target_damage"], 100)
        self.assertEqual(evidence["settlement_validations"][0]["difference"], 0)
        self.assertEqual(len(evidence["damage_result_fingerprint"]), 64)


if __name__ == "__main__":
    unittest.main()
