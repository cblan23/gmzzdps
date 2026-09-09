from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path

import npcap_shadow_runner as runner


class SequenceCoverageTests(unittest.TestCase):
    def test_reports_missing_and_duplicate_pushes(self) -> None:
        coverage = runner.SequenceCoverage()
        self.assertFalse(coverage.add(100, 10))
        self.assertFalse(coverage.add(102, 30))
        self.assertTrue(coverage.add(100, 10))

        self.assertEqual(
            coverage.summary(),
            {
                "unique_pushes": 2,
                "duplicates": 1,
                "payload_bytes": 40,
                "first_sequence": 100,
                "last_sequence": 102,
                "missing_inside_span": 1,
                "complete_inside_span": False,
            },
        )

    def test_empty_coverage_is_complete(self) -> None:
        self.assertTrue(runner.SequenceCoverage().summary()["complete_inside_span"])


class ShadowHelpersTests(unittest.TestCase):
    def test_preferred_remote_uses_configured_value(self) -> None:
        counts = Counter({"in|10.0.0.2:30000": 10})
        self.assertEqual(
            runner.preferred_remote(counts, "10.0.0.9:40000"),
            "10.0.0.9:40000",
        )

    def test_preferred_remote_uses_most_common_inbound(self) -> None:
        counts = Counter(
            {
                "out|10.0.0.8:1000": 99,
                "in|10.0.0.2:30000": 10,
                "in|10.0.0.3:30000": 20,
            }
        )
        self.assertEqual(runner.preferred_remote(counts, ""), "10.0.0.3:30000")

    def test_snapshot_candidates_are_newest_first_and_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "old_rc4.json"
            newer = root / "new_rc4.json"
            older.write_text('{"pid":7,"cryptor":4096}', encoding="utf-8")
            newer.write_text('{"pid":7,"cryptor":8192}', encoding="utf-8")
            older.touch()
            newer.touch()
            candidates = runner.snapshot_candidates([root], 7)
            self.assertEqual(set(candidates), {4096, 8192})

    def test_remote_validation(self) -> None:
        self.assertEqual(runner.split_remote("127.0.0.1:30000"), ("127.0.0.1", 30000))
        with self.assertRaises(ValueError):
            runner.split_remote("127.0.0.1")


if __name__ == "__main__":
    unittest.main()
