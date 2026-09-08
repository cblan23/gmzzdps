#!/usr/bin/env python3

from __future__ import annotations

import unittest

from boss_damage import BossDamageTracker


BASE_FILETIME = 134_321_845_000_000_000


def hit(
    sequence: int,
    damage: int,
    *,
    source_id: int = 101,
    target_id: int = 201,
    skill_id: int = 88_008_120,
) -> dict:
    return {
        "filetime_100ns": BASE_FILETIME + sequence * 10_000_000,
        "source_id": source_id,
        "target_id": target_id,
        "skill_id": skill_id,
        "damage": damage,
        "source_template_id": 7_102_403,
        "source_name": "星象仪者",
        "source_kind": "boss",
        "target_name": "队员",
    }


class BossDamageTrackerTests(unittest.TestCase):
    def test_summary_preserves_exact_amounts_and_groups_sources(self):
        tracker = BossDamageTracker()
        self.assertTrue(tracker.ingest(hit(1, 600)))
        self.assertTrue(tracker.ingest(hit(2, 400)))

        summary = tracker.summary(
            team_taken=1_000,
            skill_names={88_008_120: "知识禁制"},
            started_at_epoch=1_000.0,
            duration_seconds=30.0,
        )

        self.assertEqual(summary["coverage"], "complete")
        self.assertEqual(summary["observed_damage"], 1_000)
        self.assertEqual(summary["unassigned_taken"], 0)
        self.assertEqual(summary["hits"], 2)
        self.assertEqual(summary["max_hit"], 600)
        self.assertEqual(summary["skills"][0]["name"], "知识禁制")
        self.assertEqual(summary["skills"][0]["damage"], 1_000)
        self.assertEqual(summary["sources"][0]["damage"], 1_000)
        self.assertEqual(len(summary["event_log"]["rows"]), 2)

    def test_partial_and_conflict_are_never_scaled(self):
        tracker = BossDamageTracker()
        tracker.ingest(hit(1, 600))
        partial = tracker.summary(team_taken=1_000)
        conflict = tracker.summary(team_taken=500)

        self.assertEqual(partial["coverage"], "observed_partial")
        self.assertEqual(partial["unassigned_taken"], 400)
        self.assertEqual(partial["observed_damage"], 600)
        self.assertEqual(conflict["coverage"], "conflict")
        self.assertEqual(conflict["observed_damage"], 600)
        self.assertEqual(conflict["unassigned_taken"], 0)

    def test_duplicate_and_capacity_limit_are_explicit(self):
        tracker = BossDamageTracker(maximum_events=1)
        first = hit(1, 100)
        self.assertTrue(tracker.ingest(first))
        self.assertFalse(tracker.ingest(dict(first)))
        self.assertFalse(tracker.ingest(hit(2, 200)))

        summary = tracker.summary(team_taken=100)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["coverage"], "observed_partial")
        self.assertEqual(summary["observed_damage"], 100)
        self.assertEqual(summary["rejected_after_limit"], 1)

    def test_rebind_changes_only_target_identity(self):
        tracker = BossDamageTracker()
        tracker.ingest(hit(1, 100, target_id=201))
        self.assertTrue(tracker.rebind_target(201, 202))
        summary = tracker.summary(team_taken=100)
        self.assertEqual(summary["targets"][0]["actor_id"], 202)
        self.assertEqual(summary["observed_damage"], 100)


if __name__ == "__main__":
    unittest.main()
