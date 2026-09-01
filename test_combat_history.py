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

    def test_pure_healing_record_round_trips_and_invalid_shapes_are_safe(self):
        pure_healing = {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "encounter_id": "healing-dummy-history",
            "target_filter": "healing_dummy",
            "total_damage": 0,
            "team_effective_healing": 900,
            "hps_duration_seconds": 12.0,
            "participants": [],
            "healers": [
                {
                    "actor_id": 11,
                    "effective_healing": 900,
                    "hps": 75.0,
                    "skills": [],
                }
            ],
        }
        path = self.store.save(pure_healing)
        self.assertTrue(path.is_file())
        loaded = self.store.load_recent()[0]
        self.assertEqual(loaded["target_filter"], "healing_dummy")
        self.assertEqual(loaded["total_damage"], 0)
        self.assertEqual(loaded["team_effective_healing"], 900)

        malformed = dict(pure_healing)
        malformed["encounter_id"] = "healing-malformed"
        malformed["healers"] = None
        self.assertFalse(self.store._valid_record(malformed))

        # A legacy damage record may carry a malformed optional healing field;
        # preserve its existing DPS history instead of rejecting the record.
        legacy_damage = dict(pure_healing)
        legacy_damage["encounter_id"] = "legacy-damage-with-null-healers"
        legacy_damage["total_damage"] = 1
        legacy_damage["target_filter"] = "boss"
        legacy_damage["healers"] = None
        self.assertTrue(self.store._valid_record(legacy_damage))

        # Settlement attachment must also tolerate a legacy record whose
        # optional healing collections were cleared or encoded as null.
        legacy = dict(pure_healing)
        legacy["healers"] = None
        summary = {
            "summary_id": "healing-legacy-summary",
            "authoritative": True,
            "completion_confirmed": True,
            "actors": [
                {
                    "actor_id": 11,
                    "damage": 0,
                    "effective_healing": 1_000,
                    "healing_skills": [],
                }
            ],
        }
        updated, actor_ids = self.store._apply_exact_stage_healing(
            legacy, summary, "healing-legacy-summary"
        )
        self.assertIsInstance(updated, dict)
        self.assertEqual(actor_ids, [11])
        self.assertEqual(updated["healers"][0]["effective_healing"], 1_000)

    def test_unsafe_encounter_id_cannot_escape_directory(self):
        path = self.store.save(record("../../outside"))
        self.assertEqual(path.parent, self.directory)
        self.assertTrue(path.is_file())
        self.assertFalse((self.directory.parent / "outside.json").exists())

    def test_healing_display_filters_known_non_healers_and_removes_average(self):
        source = record("healing-display-normalization")
        source["duration_seconds"] = 10.0
        source["participants"] = [
            {"actor_id": 11, "profession_id": 1_200_002, "damage": 100},
            {"actor_id": 22, "profession_id": 1_200_003, "damage": 200},
        ]
        source["healers"] = [
            {
                "actor_id": 11,
                "profession_id": 1_200_002,
                "effective_healing": 800,
                "total_healing": 1_000,
                "response": {
                    "average_ms": 600,
                    "fastest_ms": 400,
                    "slowest_ms": 800,
                    "samples": 2,
                },
            },
            {
                "actor_id": 22,
                "profession_id": 1_200_003,
                "effective_healing": 300,
                "total_healing": 400,
            },
        ]
        source["team_effective_healing"] = 1_100
        source["team_total_healing"] = 1_400

        normalized = self.store.normalize_healing_for_display(source)

        self.assertEqual([row["actor_id"] for row in normalized["healers"]], [11])
        self.assertNotIn("average_ms", normalized["healers"][0]["response"])
        self.assertEqual(normalized["team_effective_healing"], 800)
        self.assertEqual(normalized["team_total_healing"], 1_000)
        self.assertEqual(normalized["team_hps"], 80.0)

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

    def test_late_game_settlement_replaces_wrong_shared_divisor_only(self):
        ended_at = 1_788_270_266.376329
        finished = record("late-team-clock", ended_at)
        finished.update(
            {
                "started_at_epoch": 1_788_270_036.0627675,
                "duration_seconds": 90.84104871749878,
                "dps_duration_seconds": 90.0,
                "hps_duration_seconds": 90.0,
                "duration_source": "server_shared_clock",
                "total_damage": 9_551_438,
                "team_dps": 9_551_438 / 90.0,
                "team_effective_healing": 199_227,
                "team_hps": 199_227 / 90.0,
                "participants": [
                    {
                        "actor_id": 11,
                        "damage": 5_000_000,
                        "dps": 5_000_000 / 90.0,
                        "skills": [],
                    },
                    {
                        "actor_id": 22,
                        "profession_id": 1_200_002,
                        "damage": 4_551_438,
                        "dps": 4_551_438 / 90.0,
                        "skills": [],
                    },
                ],
                "healers": [
                    {
                        "actor_id": 22,
                        "profession_id": 1_200_002,
                        "effective_healing": 199_227,
                        "hps": 199_227 / 90.0,
                    }
                ],
                "shared_clock": {"clock_id": "stale", "accepted": True},
                "damage_accounting": {
                    "stage_summary_validations": [],
                    "seen_stage_summary_ids": [],
                },
            }
        )
        self.store.save(finished)
        summary = {
            "summary_id": "settlement|5150109|1|team-clock",
            "filetime_100ns": int(
                (ended_at + 0.03) * 10_000_000
                + 116_444_736_000_000_000
            ),
            "authoritative": True,
            "completion_confirmed": True,
            "member_count": 2,
            "actors": [
                {
                    "actor_id": 11,
                    "damage": 5_000_000,
                    "combat_seconds_total": 231,
                },
                {
                    "actor_id": 22,
                    "profession_id": 1_200_002,
                    "damage": 4_551_438,
                    "effective_healing": 199_227,
                    "healing_skills": [],
                    "combat_seconds_total": 229,
                },
            ],
        }

        attached = self.store.attach_stage_summary_validation(summary)

        self.assertIsNotNone(attached)
        loaded = self.store.load_recent()[0]
        self.assertEqual(loaded["total_damage"], 9_551_438)
        self.assertEqual(
            [row["damage"] for row in loaded["participants"]],
            [5_000_000, 4_551_438],
        )
        self.assertEqual(loaded["duration_source"], "game_server_team_clock")
        self.assertEqual(loaded["dps_duration_seconds"], 231.0)
        self.assertEqual(loaded["team_dps"], 9_551_438 / 231.0)
        self.assertEqual(loaded["team_hps"], 199_227 / 231.0)
        self.assertFalse(loaded["shared_clock"]["accepted"])
        self.assertEqual(
            loaded["shared_clock"]["superseded_by"],
            "game_server_team_clock",
        )

    def test_embedded_final_settlement_repairs_old_history_display_copy(self):
        source = record("display-team-clock", 1_788_270_266.376329)
        source.update(
            {
                "started_at_epoch": 1_788_270_036.0627675,
                "duration_seconds": 90.84,
                "dps_duration_seconds": 90.0,
                "duration_source": "server_shared_clock",
                "total_damage": 3_000,
                "team_dps": 3_000 / 90.0,
                "participants": [
                    {"actor_id": 11, "damage": 1_250, "dps": 1_250 / 90.0},
                    {"actor_id": 22, "damage": 1_750, "dps": 1_750 / 90.0},
                ],
                "damage_accounting": {
                    "stage_summary_validations": [
                        {
                            "summary_id": "settlement|display-clock",
                            "authoritative": True,
                            "completion_confirmed": True,
                            "actors": [
                                {
                                    "actor_id": 11,
                                    "damage": 1_250,
                                    "combat_seconds_total": 231,
                                },
                                {
                                    "actor_id": 22,
                                    "damage": 1_750,
                                    "combat_seconds_total": 229,
                                },
                            ],
                        }
                    ]
                },
            }
        )

        restored = self.store.restore_game_server_team_clock_for_display(source)

        self.assertEqual(source["dps_duration_seconds"], 90.0)
        self.assertEqual(restored["dps_duration_seconds"], 231.0)
        self.assertEqual(restored["team_dps"], 3_000 / 231.0)

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

    def test_display_restore_uses_embedded_exact_stage_skills_without_resave(self):
        finished = record("display-restore")
        finished["total_damage"] = 1_250
        finished["participants"] = [
            {
                "actor_id": 11,
                "name": "队友",
                "damage": 1_250,
                "is_self": False,
                "skills": [],
            }
        ]
        finished["damage_accounting"] = {
            "stage_summary_validations": [
                {
                    "summary_id": "settlement|display|exact",
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": 11,
                            "damage": 1_250,
                            "skills": [
                                {"skill_id": 101, "damage": 1_000, "hits": 4},
                                {"skill_id": 102, "damage": 200, "hits": 1},
                            ],
                        }
                    ],
                }
            ]
        }

        restored = self.store.restore_exact_stage_skills_for_display(finished)

        self.assertEqual(finished["participants"][0]["skills"], [])
        self.assertEqual(restored["total_damage"], 1_250)
        self.assertEqual(restored["participants"][0]["damage"], 1_250)
        self.assertEqual(
            [
                (skill["skill_id"], skill["damage"])
                for skill in restored["participants"][0]["skills"]
            ],
            [(101, 1_000), (102, 200), (0, 50)],
        )
        self.assertEqual(
            restored["participants"][0]["skill_source"],
            "server_stage_summary",
        )
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_display_restore_rejects_mismatch_oversum_and_untrusted_tables(self):
        finished = record("display-restore-rejected")
        finished["total_damage"] = 1_550
        finished["participants"] = [
            {
                "actor_id": 11,
                "damage": 1_250,
                "is_self": False,
                "skills": [],
            },
            {
                "actor_id": 22,
                "damage": 100,
                "is_self": False,
                "skills": [],
            },
            {
                "actor_id": 33,
                "damage": 100,
                "is_self": False,
                "skills": [],
            },
            {
                "actor_id": 44,
                "damage": 100,
                "is_self": False,
                "skills": [
                    {
                        "skill_id": 401,
                        "name": "已有精确技能",
                        "damage": 100,
                        "max_hit": 100,
                    }
                ],
            },
        ]
        finished["damage_accounting"] = {
            "stage_summary_validations": [
                {
                    "summary_id": "settlement|display|rejected",
                    "authoritative": True,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": 11,
                            "damage": 1_300,
                            "skills": [{"skill_id": 101, "damage": 1_300}],
                        },
                        {
                            "actor_id": 22,
                            "damage": 100,
                            "skills": [{"skill_id": 201, "damage": 101}],
                        },
                        {
                            "actor_id": 44,
                            "damage": 100,
                            "skills": [{"skill_id": 402, "damage": 100}],
                        },
                    ],
                },
                {
                    "summary_id": "settlement|display|untrusted",
                    "authoritative": False,
                    "completion_confirmed": True,
                    "actors": [
                        {
                            "actor_id": 33,
                            "damage": 100,
                            "skills": [{"skill_id": 301, "damage": 100}],
                        }
                    ],
                },
            ]
        }

        restored = self.store.restore_exact_stage_skills_for_display(finished)

        self.assertIs(restored, finished)
        self.assertEqual(
            [participant["skills"] for participant in restored["participants"]],
            [
                [],
                [],
                [],
                [
                    {
                        "skill_id": 401,
                        "name": "已有精确技能",
                        "damage": 100,
                        "max_hit": 100,
                    }
                ],
            ],
        )

    def test_late_healing_settlement_keeps_partial_callback_detail_unscaled(self):
        finished = record("late-healing", 1_788_058_851.0)
        finished["duration_seconds"] = 10.0
        finished["total_damage"] = 1_250
        finished["participants"] = [
            {"actor_id": 11, "name": "输出", "damage": 1_250, "skills": []}
        ]
        finished["healers"] = [
            {
                "actor_id": 22,
                "name": "治疗",
                "effective_healing": 71,
                "observed_total_healing": 100,
                "observed_effective_healing": 71,
                "total_healing": 100,
                "overhealing": 29,
                "peak_hps": 14.2,
                "skills": [
                    {
                        "skill_id": 301,
                        "name": "治疗术",
                        "total_healing": 100,
                        "effective_healing": 71,
                        "overhealing": 29,
                    }
                ],
                "targets": [
                    {
                        "target_id": 11,
                        "effective_healing": 71,
                        "coverage": "exact_observed_partial",
                    }
                ],
            }
        ]
        finished["damage_accounting"] = {
            "stage_summary_validations": [],
            "seen_stage_summary_ids": [],
        }
        self.store.save(finished)
        summary_epoch = 1_788_058_852.0
        summary = {
            "summary_id": "settlement|healing|partial",
            "filetime_100ns": int(
                summary_epoch * 10_000_000 + 116_444_736_000_000_000
            ),
            "authoritative": True,
            "completion_confirmed": True,
            "member_count": 2,
            "actors": [
                {"actor_id": 11, "name": "输出", "damage": 1_250},
                {
                    "actor_id": 22,
                    "name": "治疗",
                    "damage": 0,
                    "effective_healing": 1_928,
                    "healing_skills": [
                        {"skill_id": 301, "effective_healing": 1_928}
                    ],
                },
            ],
        }

        attached = self.store.attach_stage_summary_validation(summary)

        self.assertIsNotNone(attached)
        loaded = self.store.load_recent()[0]
        healer = loaded["healers"][0]
        self.assertEqual(
            healer["coverage"], "server_effective_with_partial_callbacks"
        )
        self.assertEqual(healer["effective_healing"], 1_928)
        self.assertEqual(healer["hps"], 192.8)
        self.assertIsNone(healer["total_healing"])
        self.assertIsNone(healer["overhealing"])
        self.assertEqual(healer["overheal_rate"], 0.29)
        self.assertTrue(healer["overheal_rate_partial"])
        self.assertIsNone(healer["peak_hps"])
        self.assertEqual(healer["observed_effective_healing"], 71)
        self.assertEqual(healer["targets"][0]["effective_healing"], 71)
        self.assertEqual(healer["skills"][0]["effective_healing"], 1_928)
        self.assertIsNone(loaded["team_total_healing"])
        self.assertEqual(loaded["team_effective_healing"], 1_928)
        self.assertEqual(
            loaded["healing_accounting"][
                "stage_healing_summary_applications"
            ][0]["actor_ids"],
            [22],
        )

    def test_late_verified_healing_keeps_exact_skill_gross_and_overheal(self):
        finished = record("late-healing-verified", 1_788_058_851.0)
        finished["duration_seconds"] = 10.0
        finished["total_damage"] = 1_250
        finished["participants"] = [
            {"actor_id": 11, "name": "输出", "damage": 1_250, "skills": []}
        ]
        finished["healers"] = [
            {
                "actor_id": 22,
                "name": "治疗",
                "effective_healing": 800,
                "observed_total_healing": 1_200,
                "observed_effective_healing": 800,
                "total_healing": 1_200,
                "overhealing": 400,
                "peak_hps": 160.0,
                "skills": [
                    {
                        "skill_id": 301,
                        "name": "治疗术",
                        "total_healing": 1_000,
                        "effective_healing": 800,
                        "overhealing": 200,
                        "events": 2,
                    },
                    {
                        "skill_id": 302,
                        "name": "护盾溢出",
                        "total_healing": 200,
                        "effective_healing": 0,
                        "overhealing": 200,
                        "events": 1,
                    },
                ],
                "targets": [{"target_id": 11, "effective_healing": 800}],
            }
        ]
        finished["damage_accounting"] = {
            "stage_summary_validations": [],
            "seen_stage_summary_ids": [],
        }
        self.store.save(finished)
        summary = {
            "summary_id": "settlement|healing|verified",
            "filetime_100ns": int(
                1_788_058_852.0 * 10_000_000 + 116_444_736_000_000_000
            ),
            "authoritative": True,
            "completion_confirmed": True,
            "member_count": 2,
            "actors": [
                {"actor_id": 11, "name": "输出", "damage": 1_250},
                {
                    "actor_id": 22,
                    "name": "治疗",
                    "damage": 0,
                    "effective_healing": 800,
                    "healing_skills": [
                        {"skill_id": 301, "effective_healing": 800}
                    ],
                },
            ],
        }

        attached = self.store.attach_stage_summary_validation(summary)

        self.assertIsNotNone(attached)
        loaded = self.store.load_recent()[0]
        healer = loaded["healers"][0]
        self.assertEqual(healer["coverage"], "server_verified_callbacks")
        self.assertEqual(healer["total_healing"], 1_200)
        self.assertEqual(healer["overhealing"], 400)
        self.assertEqual(healer["peak_hps"], 160.0)
        self.assertEqual(healer["skills"][0]["total_healing"], 1_000)
        self.assertEqual(healer["skills"][0]["overhealing"], 200)
        self.assertEqual(healer["skills"][0]["events"], 2)
        self.assertEqual(healer["skills"][1]["total_healing"], 200)
        self.assertEqual(healer["skills"][1]["effective_healing"], 0)
        self.assertEqual(healer["skills"][1]["overhealing"], 200)
        self.assertEqual(healer["targets"][0]["effective_healing"], 800)
        self.assertEqual(
            loaded["healing_accounting"][
                "stage_healing_summary_applications"
            ][0]["actor_ids"],
            [22],
        )


if __name__ == "__main__":
    unittest.main()
