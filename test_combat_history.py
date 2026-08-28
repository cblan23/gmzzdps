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


if __name__ == "__main__":
    unittest.main()
