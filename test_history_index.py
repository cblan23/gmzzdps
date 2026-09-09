#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from combat_history import CombatHistoryStore, HISTORY_SCHEMA_VERSION
from history_index import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_PARTIAL,
    DungeonCatalog,
    HistoryIndex,
    RESULT_DEFEATED,
    RESULT_FAILED,
    RESULT_INTERRUPTED,
    RESULT_UNDETERMINED,
    build_history_summary,
    rebuild_dps_timeline,
    rebuild_participant_dps_timelines,
    rebuild_team_dps_timeline,
)


class HistoryIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.history = self.root / "history"
        self.catalog_path = self.root / "bosses.json"
        self.profession_path = self.root / "professions.json"
        self.catalog_path.write_text(
            json.dumps(
                {
                    "dungeons": {
                        "5100001": {
                            "name": "测试回廊",
                            "stage_ids": [5150001],
                        },
                        "5100002": {
                            "name": "另一回廊",
                            "stage_ids": [5150002],
                        },
                    },
                    "bosses": {
                        "7100001": {
                            "name": "测试首领",
                            "icon": "boss.png",
                            "dungeon_ids": [5100001],
                            "stage_ids": [5150001],
                        }
                    },
                    "client_stage_templates": {
                        "7100002": {
                            "name": "未来首领",
                            "icon": "future-boss.png",
                            "stage_ids": [5150002],
                        }
                    },
                    "stages": {
                        "5150001": {
                            "name": "测试首领",
                            "icon": "boss.png",
                            "dungeon_ids": [5100001],
                        },
                        "5150002": {
                            "name": "阿蒙",
                            "icon": "stage-boss.png",
                            "dungeon_ids": [5100002],
                        }
                    },
                    "stage_name_index": {
                        "阿蒙": [
                            {"icon": "first.png", "stage_ids": [5150001]},
                            {"icon": "stage-boss.png", "stage_ids": [5150002]},
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.profession_path.write_text(
            json.dumps(
                {
                    "professions": {
                        "1200003": {"name": "愚者途径"},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.catalog = DungeonCatalog(self.catalog_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def record(battle_id: str, ended_at: float = 100.0) -> dict:
        return {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "encounter_id": battle_id,
            "archive_reason": "target_defeated",
            "started_at_epoch": ended_at - 20,
            "ended_at_epoch": ended_at,
            "duration_seconds": 20,
            "dungeon_id": 5100001,
            "dungeon_stage_id": 5150001,
            "team_size": 2,
            "total_damage": 3000,
            "team_dps": 150,
            "target_filter": "boss",
            "monster": {
                "entity_id": 99,
                "template_id": 7100001,
                "name": "测试首领",
                "boss_type": 3,
            },
            "targets": [
                {
                    "entity_id": 99,
                    "template_id": 7100001,
                    "name": "测试首领",
                }
            ],
            "participants": [
                {
                    "actor_id": 1,
                    "name": "本人",
                    "is_self": True,
                    "profession_id": 1200003,
                    "damage": 2000,
                    "dps": 100,
                    "share": 2 / 3,
                    "skills": [{"skill_id": 10, "damage": 2000}],
                },
                {
                    "actor_id": 2,
                    "name": "队友",
                    "is_self": False,
                    "damage": 1000,
                    "skills": [],
                },
            ],
        }

    def test_summary_prefers_runtime_dungeon_and_preserves_self_values(self):
        record = self.record("battle-a")
        record["capture_pipeline_at_archive"] = {
            "dungeon_id": 5100002,
            "dungeon_stage_id": 5150002,
        }
        summary = build_history_summary(
            record, self.catalog, {1200003: "愚者途径"}
        )

        self.assertEqual(summary["dungeon_id"], 5100001)
        self.assertEqual(summary["dungeon_name"], "测试回廊")
        self.assertEqual(summary["dungeon_source"], "runtime_dungeon_id")
        self.assertEqual(summary["boss_name"], "测试首领")
        self.assertEqual(summary["result"], RESULT_DEFEATED)
        self.assertEqual(summary["my_damage"], 2000)
        self.assertEqual(summary["my_dps"], 100)
        self.assertEqual(summary["profession_name"], "愚者途径")
        self.assertEqual(summary["completeness"], COMPLETENESS_COMPLETE)

    def test_team_stats_response_failure_marks_history_partial(self):
        record = self.record("team-response-failure")
        record["capture_pipeline_at_archive"] = {
            "team_stats_mode": "unknown",
            "team_stats_response_health": "inactive",
            "team_stats_data_incomplete": True,
        }

        summary = build_history_summary(record, self.catalog)

        self.assertIn("team_stats_stream", summary["missing_fields"])
        self.assertEqual(summary["completeness"], COMPLETENESS_PARTIAL)

    def test_summary_preserves_explicit_extraordinary_rating_without_inference(self):
        record = self.record("battle-rating")
        record["participants"][0]["extraordinary_rating"] = 12_345

        summary = build_history_summary(record, self.catalog)

        self.assertEqual(summary["extraordinary_rating"], 12_345)
        record["participants"][0].pop("extraordinary_rating")
        record["participants"][0]["level"] = 69
        record["participants"][0]["dps"] = 98_765
        summary_without_rating = build_history_summary(record, self.catalog)
        self.assertIsNone(summary_without_rating["extraordinary_rating"])

    def test_required_boss_names_infer_the_configured_dungeon(self):
        cases = (
            ("卡尔·埃德加", "黑荆棘事件簿"),
            ("瑞尔·比伯", "安提哥努斯笔记"),
            ("异化猎犬", "五月庄园·花园"),
            ("子嗣守护", "五月庄园·城堡"),
            ('"剥面人" 强尼', "记忆的传承"),
            ("“钻头”", "记忆的传承"),
            ("邦尼", "记忆的传承"),
            ("战争巨龙", "记忆的传承"),
            ("伤害木桩", "木桩"),
        )
        for index, (boss_name, dungeon_name) in enumerate(cases):
            with self.subTest(boss_name=boss_name):
                record = self.record(f"battle-dungeon-{index}")
                record.pop("dungeon_id")
                record.pop("dungeon_stage_id")
                record["monster"]["template_id"] = 0
                record["monster"]["name"] = boss_name
                record["targets"] = [dict(record["monster"])]

                summary = build_history_summary(record, self.catalog)

                self.assertEqual(summary["dungeon_name"], dungeon_name)
                self.assertEqual(summary["dungeon_source"], "boss_mapping")
                self.assertEqual(summary["stage_name"], summary["boss_name"])

        self.assertEqual(
            build_history_summary(
                {
                    **self.record("battle-ray-biber"),
                    "dungeon_id": 0,
                    "dungeon_stage_id": 0,
                    "monster": {
                        "entity_id": 99,
                        "name": "瑞尔·比伯",
                        "boss_type": 3,
                    },
                    "targets": [
                        {
                            "entity_id": 99,
                            "name": "瑞尔·比伯",
                            "boss_type": 3,
                        }
                    ],
                },
                self.catalog,
            )["boss_name"],
            "瑞尔比伯",
        )

    def test_old_incorrect_castle_name_is_normalized_for_existing_history(self):
        record = self.record("battle-old-castle-name")
        record["dungeon_name"] = "记忆的传承·城堡"

        summary = build_history_summary(record, self.catalog)

        self.assertEqual(summary["dungeon_name"], "五月庄园·城堡")

    def test_generic_memory_label_uses_boss_identity_to_separate_old_castle(self):
        old_castle = self.record("battle-generic-old-castle")
        old_castle.pop("dungeon_id")
        old_castle.pop("dungeon_stage_id")
        old_castle["dungeon_name"] = "记忆的传承"
        old_castle["monster"] = {
            "entity_id": 99,
            "template_id": 0,
            "name": "一号信徒",
            "boss_type": 3,
        }
        old_castle["targets"] = [dict(old_castle["monster"])]

        memory = self.record("battle-current-memory")
        memory.pop("dungeon_id")
        memory.pop("dungeon_stage_id")
        memory["dungeon_name"] = "记忆的传承"
        memory["monster"] = {
            "entity_id": 100,
            "template_id": 0,
            "name": '"剥面人" 强尼',
            "boss_type": 3,
        }
        memory["targets"] = [dict(memory["monster"])]

        old_castle_summary = build_history_summary(old_castle, self.catalog)
        memory_summary = build_history_summary(memory, self.catalog)

        self.assertEqual(
            old_castle_summary["dungeon_name"], "五月庄园·城堡"
        )
        self.assertEqual(memory_summary["dungeon_name"], "记忆的传承")
        self.assertEqual(memory_summary["boss_name"], "强尼")

    def test_newer_boss_validated_stage_replaces_stale_encounter_stage(self):
        record = self.record("battle-stale-stage")
        record["dungeon_id"] = 0
        record["dungeon_stage_id"] = 5_150_002
        record["capture_pipeline_at_archive"] = {
            "dungeon_id": 0,
            "dungeon_stage_id": 5_150_001,
        }

        summary = build_history_summary(record, self.catalog)

        self.assertEqual(summary["stage_id"], 5_150_001)
        self.assertEqual(summary["stage_name"], "测试首领")
        self.assertEqual(summary["dungeon_id"], 5_100_001)

    def test_history_search_text_only_contains_dungeon_and_boss_identity(self):
        record = self.record("secret-battle-id")
        record["note"] = "不要进入搜索"
        summary = build_history_summary(
            record, self.catalog, {1200003: "愚者途径"}
        )

        search_text = summary["search_text"]
        self.assertIn("测试回廊", search_text)
        self.assertIn("测试首领", search_text)
        self.assertNotIn("secret-battle-id", search_text)
        self.assertNotIn("本人", search_text)
        self.assertNotIn("愚者途径", search_text)
        self.assertNotIn("不要进入搜索", search_text)

    def test_legacy_stage_id_resolves_dungeon_without_guessing_self(self):
        record = self.record("battle-b")
        record.pop("dungeon_id")
        record.pop("dungeon_stage_id")
        for participant in record["participants"]:
            participant.pop("is_self", None)
        record["damage_accounting"] = {
            "stage_summary_validations": [
                {"summary_id": "settlement|5150001|1|token"}
            ]
        }

        summary = build_history_summary(
            record, self.catalog, {1200003: "愚者途径"}
        )

        self.assertEqual(summary["dungeon_id"], 5100001)
        self.assertEqual(summary["dungeon_source"], "stage_and_boss_mapping")
        self.assertIsNone(summary["my_damage"])
        self.assertIsNone(summary["my_dps"])
        self.assertIn("self_identity", summary["missing_fields"])
        self.assertEqual(summary["completeness"], COMPLETENESS_PARTIAL)

    def test_authoritative_completion_wins_over_exit_reason(self):
        record = self.record("battle-c")
        record["archive_reason"] = "party_exit"
        record["damage_accounting"] = {
            "stage_summary_validations": [
                {
                    "summary_id": "settlement|5150001|1|token",
                    "authoritative": True,
                    "completion_confirmed": True,
                }
            ]
        }
        self.assertEqual(
            build_history_summary(record, self.catalog)["result"],
            RESULT_DEFEATED,
        )

    def test_history_external_template_uses_client_stage_icon(self):
        record = self.record("battle-future-template")
        record["monster"]["template_id"] = 7100002
        record["monster"]["name"] = "未来首领"
        record["targets"][0]["template_id"] = 7100002
        record["targets"][0]["name"] = "未来首领"

        summary = build_history_summary(record, self.catalog)

        self.assertEqual(summary["boss_template_id"], 7100002)
        self.assertEqual(summary["boss_icon"], "future-boss.png")

    def test_unknown_template_uses_explicit_stage_icon(self):
        record = self.record("battle-stage-icon")
        record["dungeon_id"] = 5100002
        record["dungeon_stage_id"] = 5150002
        record["monster"]["template_id"] = 7100999
        record["monster"]["name"] = "阿蒙"
        record["targets"][0]["template_id"] = 7100999
        record["targets"][0]["name"] = "阿蒙"

        summary = build_history_summary(record, self.catalog)

        self.assertEqual(summary["boss_icon"], "stage-boss.png")

    def test_boss_summary_excludes_additional_normal_monsters(self):
        record = self.record("battle-with-add")
        record["targets"].append(
            {
                "entity_id": 101,
                "name": "小怪 1",
                "entity_type": "Monster",
                "boss_rank": 0,
            }
        )

        summary = build_history_summary(record, self.catalog)

        self.assertEqual(summary["boss_count"], 1)
        self.assertEqual(summary["boss_names"], ["测试首领"])

    def test_boss_summary_keeps_confirmed_second_boss(self):
        record = self.record("battle-double-boss")
        record["targets"].append(
            {
                "entity_id": 102,
                "template_id": 7100002,
                "name": "未来首领",
                "entity_type": "Boss",
                "boss_rank": 3,
            }
        )

        summary = build_history_summary(record, self.catalog)

        self.assertEqual(summary["boss_count"], 2)
        self.assertEqual(summary["boss_names"], ["测试首领", "未来首领"])

    def test_first_believer_phase_entities_collapse_to_canonical_boss(self):
        record = self.record("battle-first-believer")
        record["monster"].update(
            {
                "template_id": 7_100_208,
                "name": "安西娅",
                "boss_rank": 3,
            }
        )
        record["targets"] = [
            dict(record["monster"]),
            {
                "entity_id": 100,
                "template_id": 7_100_209,
                "name": "巴尼先生",
                "boss_rank": 3,
            },
            {
                "entity_id": 101,
                "template_id": 7_100_210,
                "name": "安西娅",
                "boss_rank": 3,
            },
        ]

        summary = build_history_summary(record, self.catalog)

        self.assertEqual(summary["boss_count"], 1)
        self.assertEqual(summary["boss_names"], ["一号信徒"])
        self.assertEqual(summary["boss_template_id"], 7_102_980)

    def test_index_filters_sorts_and_paginates_summaries(self):
        store = CombatHistoryStore(
            self.history,
            catalog_path=self.catalog_path,
            profession_path=self.profession_path,
        )
        for index in range(25):
            record = self.record(f"battle-{index:02d}", ended_at=1000 + index)
            record["participants"][0]["damage"] = 2000 + index
            record["participants"][0]["dps"] = 100 + index
            if index % 2:
                record["archive_reason"] = "party_wipe"
            store.save(record)
        result = store.query_summaries(
            {"boss_only": True, "result": RESULT_FAILED},
            page=2,
            page_size=10,
            sort="dps_desc",
            refresh=True,
        )

        self.assertEqual(result["total"], 12)
        self.assertEqual(result["page"], 2)
        self.assertEqual(len(result["records"]), 2)
        self.assertGreater(
            result["records"][0]["my_dps"],
            result["records"][1]["my_dps"],
        )
        overview = store.history_overview({"boss_only": True})
        self.assertEqual(overview["battle_count"], 25)
        self.assertEqual(overview["defeated_count"], 13)
        self.assertEqual(overview["failed_count"], 12)
        self.assertEqual(overview["interrupted_count"], 0)
        self.assertEqual(overview["judged_count"], 25)
        self.assertEqual(overview["average_duration"], 20)
        self.assertEqual(overview["timed_count"], 25)

    def test_index_preserves_and_aggregates_boss_damage_summaries(self):
        store = CombatHistoryStore(
            self.history,
            catalog_path=self.catalog_path,
            profession_path=self.profession_path,
        )
        low = self.record("boss-damage-low", ended_at=2000)
        low["boss_damage"] = {
            "coverage": "observed_partial",
            "observed_damage": 400,
            "team_taken": 1000,
            "classification_ratio": 0.4,
            "max_hit": 250,
            "sources": [{"entity_id": 99, "damage": 400}],
        }
        high = self.record("boss-damage-high", ended_at=2001)
        high["boss_damage"] = {
            "coverage": "complete",
            "observed_damage": 900,
            "team_taken": 900,
            "classification_ratio": 1.0,
            "max_hit": 500,
            "sources": [{"entity_id": 99, "damage": 900}],
        }
        unavailable = self.record("boss-damage-unavailable", ended_at=2002)
        unavailable["boss_damage"] = {
            "coverage": "unavailable",
            "unavailable_reason": "training_dummy",
            "observed_damage": 0,
            "team_taken": 0,
            "classification_ratio": 0.0,
            "max_hit": 0,
            "sources": [],
        }
        for record in (low, high, unavailable):
            store.save(record)

        result = store.query_summaries(
            {"boss_only": True},
            sort="boss_damage_desc",
            refresh=True,
        )
        overview = store.history_overview({"boss_only": True})

        self.assertEqual(
            [row["battle_id"] for row in result["records"]],
            [
                "boss-damage-high",
                "boss-damage-low",
                "boss-damage-unavailable",
            ],
        )
        self.assertEqual(result["records"][0]["boss_observed_damage"], 900)
        self.assertEqual(
            result["records"][2]["boss_damage_unavailable_reason"],
            "training_dummy",
        )
        self.assertAlmostEqual(
            overview["average_boss_classification_ratio"], 0.7
        )
        self.assertEqual(overview["highest_boss_observed_damage"], 900)
        self.assertEqual(overview["highest_boss_battle_id"], "boss-damage-high")

    def test_boss_filter_accepts_canonical_name_aliases(self):
        store = CombatHistoryStore(
            self.history,
            catalog_path=self.catalog_path,
            profession_path=self.profession_path,
        )
        record = self.record("battle-ray-biber")
        record["monster"]["template_id"] = 0
        record["monster"]["name"] = "瑞尔·比伯"
        record["targets"] = [dict(record["monster"])]
        store.save(record)

        result = store.query_summaries(
            {
                "boss": "瑞尔比伯",
                "boss_aliases": ["瑞尔比伯", "瑞尔·比伯"],
            },
            refresh=True,
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["records"][0]["boss_name"], "瑞尔比伯")

    def test_index_can_filter_interrupted_and_undetermined_as_one_group(self):
        store = CombatHistoryStore(
            self.history,
            catalog_path=self.catalog_path,
            profession_path=self.profession_path,
        )
        interrupted = self.record("battle-interrupted", ended_at=2000)
        interrupted["archive_reason"] = "party_exit"
        undetermined = self.record("battle-undetermined", ended_at=2001)
        undetermined.pop("archive_reason")
        store.save(self.record("battle-victory", ended_at=1999))
        store.save(interrupted)
        store.save(undetermined)

        result = store.query_summaries(
            {"results": [RESULT_INTERRUPTED, RESULT_UNDETERMINED]},
            page=1,
            page_size=10,
            refresh=True,
        )

        overview = store.history_overview({"boss_only": True})
        self.assertEqual(overview["defeated_count"], 1)
        self.assertEqual(overview["failed_count"], 0)
        self.assertEqual(overview["interrupted_count"], 2)
        self.assertEqual(overview["judged_count"], 3)
        self.assertAlmostEqual(overview["defeat_rate"], 1 / 3)

        self.assertEqual(result["total"], 2)
        self.assertEqual(
            {row["battle_id"] for row in result["records"]},
            {"battle-interrupted", "battle-undetermined"},
        )

    def test_index_sync_removes_deleted_source(self):
        store = CombatHistoryStore(
            self.history,
            catalog_path=self.catalog_path,
            profession_path=self.profession_path,
        )
        store.save(self.record("battle-delete"))
        store.refresh_index()
        (self.history / "battle-delete.json").unlink()

        result = store.refresh_index()

        self.assertEqual(result["indexed"], 0)
        self.assertEqual(result["removed"], 1)

    def test_catalog_change_rebuilds_cached_names_and_icons(self):
        store = CombatHistoryStore(
            self.history,
            catalog_path=self.catalog_path,
            profession_path=self.profession_path,
        )
        store.save(self.record("battle-catalog-refresh"))
        store.refresh_index()
        payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        payload["dungeons"]["5100001"]["name"] = "重制回廊"
        payload["bosses"]["7100001"]["icon"] = "boss-v2.png"
        self.catalog_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        store.refresh_index()
        result = store.query_summaries(
            {"boss_only": True}, page=1, page_size=10
        )

        self.assertEqual(result["records"][0]["dungeon_name"], "重制回廊")
        self.assertEqual(result["records"][0]["boss_icon"], "boss-v2.png")

    def test_rebuild_timeline_uses_only_confirmed_self_events(self):
        record = self.record("battle-timeline")
        record["event_log"] = {
            "version": 1,
            "rows": [
                [0, 1, 99, 10, 100, None, None],
                [1000, 2, 99, 20, 900, None, None],
                [10_000, 1, 99, 10, 200, None, None],
            ],
        }

        timeline = rebuild_dps_timeline(record)

        self.assertEqual(timeline[0]["dps"], 100)
        self.assertEqual(timeline[1]["dps"], 50)
        self.assertEqual(timeline[10]["dps"], 20)

    def test_rebuild_team_timeline_combines_every_observed_player(self):
        record = self.record("battle-team-timeline")
        record["total_damage"] = 1200
        record["event_log"] = {
            "version": 1,
            "rows": [
                [0, 1, 99, 10, 100, None, None],
                [1000, 2, 99, 20, 900, None, None],
                [10_000, 1, 99, 10, 200, None, None],
            ],
        }

        timeline = rebuild_team_dps_timeline(record)

        self.assertEqual(timeline[0]["team_dps"], 100)
        self.assertEqual(timeline[1]["team_dps"], 500)
        self.assertEqual(timeline[10]["team_dps"], 110)

    def test_incomplete_event_log_does_not_create_team_timeline(self):
        record = self.record("battle-incomplete-team-timeline")
        record["event_log"] = {
            "version": 1,
            "rows": [[0, 1, 99, 10, 100, None, None]],
        }

        self.assertEqual(rebuild_team_dps_timeline(record), [])

    def test_team_cumulative_samples_create_validated_timeline(self):
        record = self.record("battle-cumulative-team-timeline")
        record["duration_seconds"] = 3
        record["dps_duration_seconds"] = 3
        record["total_damage"] = 1200
        record["team_damage_samples"] = {
            "version": 1,
            "coverage": "live_team_cumulative",
            "rows": [[0, 100], [1, 500], [2, 900], [3, 1200]],
        }

        timeline = rebuild_team_dps_timeline(record)

        self.assertEqual(timeline[-1]["source"], "live_team_cumulative")
        self.assertEqual(timeline[-1]["team_dps"], 300)

    def test_team_cumulative_samples_must_match_settlement_total(self):
        record = self.record("battle-invalid-cumulative-team-timeline")
        record["duration_seconds"] = 3
        record["dps_duration_seconds"] = 3
        record["team_damage_samples"] = {
            "version": 1,
            "coverage": "live_team_cumulative",
            "rows": [[0, 100], [1, 500], [2, 900], [3, 1200]],
        }

        self.assertEqual(rebuild_team_dps_timeline(record), [])

    def test_participant_cumulative_samples_rebuild_every_player_timeline(self):
        record = self.record("battle-participant-timelines")
        record["duration_seconds"] = 3
        record["dps_duration_seconds"] = 3
        record["participant_damage_samples"] = {
            "version": 1,
            "coverage": "live_participant_cumulative",
            "rows": [
                [0, 1, 200],
                [0, 2, 100],
                [1, 1, 600],
                [1, 2, 300],
                [2, 1, 1_200],
                [2, 2, 600],
                [3, 1, 2_000],
                [3, 2, 1_000],
            ],
        }

        timelines = rebuild_participant_dps_timelines(record)

        self.assertEqual(set(timelines), {1, 2})
        self.assertEqual(timelines[1][-1]["dps"], 500)
        self.assertEqual(timelines[2][-1]["dps"], 250)
        self.assertEqual(
            timelines[1][-1]["source"], "live_participant_cumulative"
        )

    def test_participant_timelines_reject_incomplete_or_mismatched_samples(self):
        record = self.record("battle-invalid-participant-timelines")
        record["participant_damage_samples"] = {
            "version": 1,
            "coverage": "live_participant_cumulative",
            "rows": [[0, 1, 100], [20, 1, 1_999], [20, 2, 1_000]],
        }

        self.assertEqual(rebuild_participant_dps_timelines(record), {})

        old_record = self.record("battle-old-without-participant-samples")
        self.assertEqual(rebuild_participant_dps_timelines(old_record), {})

    def test_recalculate_removes_stale_incomplete_team_timeline(self):
        store = CombatHistoryStore(
            self.history,
            catalog_path=self.catalog_path,
            profession_path=self.profession_path,
        )
        record = self.record("battle-stale-team-timeline")
        record["event_log"] = {
            "version": 1,
            "rows": [[0, 1, 99, 10, 100, None, None]],
        }
        record["team_dps_timeline"] = [
            {"time_seconds": 0, "team_dps": 100}
        ]
        store.save(record)

        updated, _status = store.recalculate(record["encounter_id"])

        self.assertIsNotNone(updated)
        self.assertNotIn("team_dps_timeline", updated)
        self.assertNotIn(
            "team_dps_timeline", store.load(record["encounter_id"])
        )

    def test_json_and_csv_exports_keep_unknown_values_empty(self):
        store = CombatHistoryStore(
            self.history,
            catalog_path=self.catalog_path,
            profession_path=self.profession_path,
        )
        record = self.record("battle-export")
        for participant in record["participants"]:
            participant.pop("is_self", None)
        store.save(record)
        json_path = self.root / "export.json"
        csv_path = self.root / "export.csv"

        self.assertEqual(
            store.export_records(["battle-export"], json_path, "json"), 1
        )
        self.assertEqual(
            store.export_records(["battle-export"], csv_path, "csv"), 1
        )
        exported = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(exported["battle_count"], 1)
        csv_text = csv_path.read_text(encoding="utf-8-sig")
        self.assertIn("battle-export", csv_text)
        self.assertNotIn(",0.0,", csv_text)


class BossAssetCatalogTests(unittest.TestCase):
    def test_every_mapped_boss_icon_exists(self):
        root = Path(__file__).resolve().parent
        boss_root = root / "assets" / "bosses"
        payload = json.loads(
            (boss_root / "boss_icon_sources.json").read_text(encoding="utf-8")
        )

        mapped = 0
        missing: list[str] = []
        for section_name in ("bosses", "client_stage_templates", "stages"):
            for item_id, metadata in payload.get(section_name, {}).items():
                icon = str(metadata.get("icon") or "").strip()
                if not icon:
                    continue
                mapped += 1
                if not (boss_root / icon).is_file():
                    missing.append(f"{section_name}:{item_id}:{icon}")

        self.assertGreaterEqual(mapped, 121)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
