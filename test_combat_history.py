#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from combat_history import CombatHistoryStore, HISTORY_SCHEMA_VERSION


def record(encounter_id: str, ended_at: float = 100.0) -> dict:
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "encounter_id": encounter_id,
        "ended_at_epoch": ended_at,
        "total_damage": 123_456,
        "participants": [{"actor_id": 1, "name": "莫雪", "skills": []}],
    }


class CombatHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = CombatHistoryStore(self.directory)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_save_load_favorite_and_delete(self):
        path = self.store.save(record("encounter-1"))
        self.assertTrue(path.is_file())
        self.assertFalse(self.store.load_recent()[0]["favorite"])

        updated = self.store.set_favorite("encounter-1", True)
        self.assertIsNotNone(updated)
        self.assertTrue(self.store.load_recent()[0]["favorite"])

        self.assertTrue(self.store.delete("encounter-1"))
        self.assertFalse(self.store.delete("encounter-1"))
        self.assertEqual(self.store.load_recent(), [])

    def test_updating_encounter_preserves_favorite(self):
        self.store.save(record("encounter-2", 100.0))
        self.store.set_favorite("encounter-2", True)
        replacement = record("encounter-2", 120.0)
        replacement["total_damage"] = 999_999
        self.store.save(replacement)

        loaded = self.store.load_recent()[0]
        self.assertTrue(loaded["favorite"])
        self.assertEqual(loaded["total_damage"], 999_999)

    def test_clear_unfavorited_preserves_favorites_and_unrelated_files(self):
        self.store.save(record("encounter-1", 100.0))
        self.store.save(record("encounter-2", 200.0))
        self.store.save(record("favorite", 300.0))
        self.store.set_favorite("favorite", True)
        unrelated = self.directory / "readme.txt"
        unrelated.write_text("保留", encoding="utf-8")

        self.assertEqual(self.store.unfavorited_count(), 2)
        self.assertEqual(self.store.clear_unfavorited(), 2)
        loaded = self.store.load_recent()
        self.assertEqual([item["encounter_id"] for item in loaded], ["favorite"])
        self.assertTrue(loaded[0]["favorite"])
        self.assertTrue(unrelated.is_file())
        self.assertEqual(self.store.unfavorited_count(), 0)
        self.assertEqual(self.store.clear_unfavorited(), 0)

    def test_invalid_and_corrupt_records_are_ignored(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "broken.json").write_text("{", encoding="utf-8")
        (self.directory / "wrong-schema.json").write_text(
            json.dumps(
                {
                    "schema_version": 99,
                    "encounter_id": "old",
                    "total_damage": 10,
                    "participants": [],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.store.load_recent(), [])

    def test_unsafe_encounter_id_cannot_escape_directory(self):
        path = self.store.save(record("../../outside"))
        self.assertEqual(path.parent, self.directory)
        self.assertTrue(path.is_file())
        self.assertFalse((self.directory.parent / "outside.json").exists())

    def test_late_completion_attaches_validation_without_rewriting_damage(self):
        finished = record("late-table", 1_788_058_851.0124204)
        finished["total_damage"] = 3_000
        finished["participants"] = [
            {"actor_id": 11, "name": "A", "damage": 1_250, "skills": []},
            {"actor_id": 22, "name": "B", "damage": 1_750, "skills": []},
        ]
        finished["damage_accounting"] = {
            "stage_summary_validations": [],
            "seen_stage_summary_ids": [],
        }
        self.store.save(finished)
        summary_epoch = 1_788_058_928.970
        summary = {
            "summary_id": "settlement|5150058|1|sample",
            "filetime_100ns": int(
                summary_epoch * 10_000_000 + 116_444_736_000_000_000
            ),
            "authoritative": True,
            "completion_confirmed": True,
            "member_count": 2,
            "actors": [
                {
                    "actor_id": 11,
                    "name": "A",
                    "damage": 1_250,
                    "skills": [
                        {"skill_id": 101, "damage": 1_000, "hits": 4},
                        {"skill_id": 102, "damage": 200, "hits": 1},
                    ],
                },
                {
                    "actor_id": 22,
                    "name": "B",
                    "damage": 1_750,
                    "skills": [
                        {"skill_id": 201, "damage": 1_750, "hits": 7}
                    ],
                },
            ],
        }

        attached = self.store.attach_stage_summary_validation(summary)

        self.assertIsNotNone(attached)
        loaded = self.store.load_recent()[0]
        self.assertEqual(loaded["total_damage"], 3_000)
        self.assertEqual(
            [row["damage"] for row in loaded["participants"]],
            [1_250, 1_750],
        )
        validations = loaded["damage_accounting"]["stage_summary_validations"]
        self.assertEqual(len(validations), 1)
        validation = validations[0]["validation"]
        self.assertTrue(validation["attached_to_archived_record"])
        self.assertTrue(validation["per_actor_exact_match"])
        self.assertEqual(validation["difference"], 0)
        self.assertFalse(validation["damage_correction_applied"])
        first_skills = loaded["participants"][0]["skills"]
        self.assertEqual(
            [(row["skill_id"], row["damage"]) for row in first_skills],
            [(101, 1_000), (102, 200), (0, 50)],
        )
        self.assertEqual(
            loaded["participants"][0]["skill_source"],
            "server_stage_summary",
        )
        self.assertEqual(
            loaded["damage_accounting"]["stage_skill_summary_applications"][0][
                "actor_ids"
            ],
            [11, 22],
        )
        self.assertEqual(
            loaded["damage_accounting"]["skill_reconciliation"],
            [
                {
                    "actor_id": 11,
                    "name": "A",
                    "damage": 1_250,
                    "classified_skill_damage": 1_200,
                    "unclassified_damage": 50,
                    "accounted_skill_damage": 1_250,
                    "difference": 0,
                },
                {
                    "actor_id": 22,
                    "name": "B",
                    "damage": 1_750,
                    "classified_skill_damage": 1_750,
                    "unclassified_damage": 0,
                    "accounted_skill_damage": 1_750,
                    "difference": 0,
                },
            ],
        )

        duplicate = self.store.attach_stage_summary_validation(summary)
        self.assertIsNotNone(duplicate)
        self.assertEqual(
            len(
                self.store.load_recent()[0]["damage_accounting"]
                ["stage_summary_validations"]
            ),
            1,
        )

    def test_existing_validation_upgrades_missing_stage_skills_safely(self):
        summary_id = "settlement|5150058|1|legacy"
        finished = record("legacy-validation", 1_788_058_851.0124204)
        finished["total_damage"] = 1_250
        finished["participants"] = [
            {"actor_id": 11, "name": "A", "damage": 1_250, "skills": []}
        ]
        finished["damage_accounting"] = {
            "stage_summary_validations": [{"summary_id": summary_id}],
            "seen_stage_summary_ids": [summary_id],
            # A pre-fix record may contain unusable metadata but no valid
            # application. It must not crash or prevent the skill upgrade.
            "stage_skill_summary_applications": [
                {"summary_id": summary_id, "actor_ids": [None, "broken"]}
            ],
        }
        self.store.save(finished)
        summary_epoch = 1_788_058_928.970
        summary = {
            "summary_id": summary_id,
            "filetime_100ns": int(
                summary_epoch * 10_000_000 + 116_444_736_000_000_000
            ),
            "authoritative": True,
            "completion_confirmed": True,
            "member_count": 1,
            "actors": [
                {
                    "actor_id": 11,
                    "name": "A",
                    "damage": 1_250,
                    "skills": [
                        {"skill_id": 101, "damage": 1_200, "hits": 5}
                    ],
                }
            ],
        }

        upgraded = self.store.attach_stage_summary_validation(summary)

        self.assertIsNotNone(upgraded)
        loaded = self.store.load_recent()[0]
        self.assertEqual(loaded["total_damage"], 1_250)
        self.assertEqual(
            [skill["damage"] for skill in loaded["participants"][0]["skills"]],
            [1_200, 50],
        )
        self.assertEqual(
            len(loaded["damage_accounting"]["stage_summary_validations"]),
            1,
        )
        self.assertEqual(
            loaded["damage_accounting"]["stage_skill_summary_applications"][-1][
                "actor_ids"
            ],
            [11],
        )


if __name__ == "__main__":
    unittest.main()
