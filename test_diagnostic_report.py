#!/usr/bin/env python3

from __future__ import annotations

import unittest

from diagnostic_report import DiagnosticAnalyzer, diagnostic_environment


TARGET_ID = 57_456_999_419_492


def damage_record(sequence: int = 1) -> dict:
    return {
        "sequence": sequence,
        "method": "OnMsgDamageSyncV2",
        "filetime_100ns": 134_321_845_000_000_000 + sequence,
        "script_entity": 0x1234,
        "decode_delay_ms": 0.2,
        "decoded_arguments": [
            57_277_687_900_295,
            TARGET_ID,
            860_200_100,
            2,
            2,
            1_234,
            0,
            1_234,
            False,
        ],
    }


def native_boss_record(
    template_id: int, boss_type: int, sequence: int = 1
) -> dict:
    return {
        "sequence": sequence,
        "function": "CommonComponent_TemplateBossType",
        "filetime_100ns": 134_321_845_000_000_000 + sequence,
        "entity_id": TARGET_ID,
        "template_id": template_id,
        "boss_type": boss_type,
    }


def native_damage_record(sequence: int = 1) -> dict:
    return {
        "sequence": sequence,
        "function": "DamageHook",
        "filetime_100ns": 134_321_845_000_000_000 + sequence,
        "attacker_id": 57_277_687_900_295,
        "target_id": TARGET_ID,
        "arg4_u64": 860_200_100,
        "arg5_i32": 2,
        "arg6_i32": 2,
        "raw_damage": 1_234,
        "arg8_i32": 0,
        "damage": 1_234,
        "arg10_bool": False,
    }


class DiagnosticAnalyzerTests(unittest.TestCase):
    def test_environment_collection_supports_build_python(self):
        environment = diagnostic_environment(elevated=True, packaged=True)

        self.assertTrue(environment["locale_encoding"])
        self.assertTrue(environment["windows_version"])
        self.assertTrue(environment["elevated"])
        self.assertTrue(environment["packaged"])

    def test_detects_silent_native_hook_blocking_network_fallback(self):
        analyzer = DiagnosticAnalyzer({})
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": True,
                "team_stats_hook_installed": True,
                "damage_source": "native",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [],
                "native_boss_records": [],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": True,
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(
            report["assessment"]["code"],
            "native_hook_silent_network_fallback_blocked",
        )
        capture = report["capture"]
        self.assertEqual(capture["network_damage_positive"], 1)
        self.assertEqual(capture["native_positive_damage_records"], 0)
        self.assertEqual(capture["network_damage_suppressed_by_native"], 1)
        self.assertEqual(capture["forwarded_damage_events"], 0)

    def test_confirmed_boss_network_fallback_is_displayable(self):
        analyzer = DiagnosticAnalyzer(
            {"7100001": {"boss_type": 3, "name": "测试 Boss"}}
        )
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": False,
                "team_stats_hook_installed": False,
                "damage_source": "script",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [],
                "native_boss_records": [native_boss_record(7_100_001, 3)],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": False,
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(report["assessment"]["code"], "capture_pipeline_ok")
        self.assertEqual(report["capture"]["parsed_damage_events"], 1)
        self.assertEqual(report["capture"]["forwarded_damage_events"], 1)
        self.assertEqual(report["capture"]["boss_confirmed_damage_events"], 1)
        target = report["capture"]["damage_targets"][0]
        self.assertEqual(target["template_id"], 7_100_001)
        self.assertTrue(target["catalog_match"])
        self.assertTrue(target["boss_mode_displayable"])
        links = report["capture"]["target_identity_links"]
        self.assertEqual(links["damage_targets"], 1)
        self.assertEqual(links["native_entity_exact_matches"], 1)
        self.assertEqual(links["native_template_exact_matches"], 1)
        self.assertEqual(links["confirmed_boss_exact_matches"], 1)

    def test_non_type3_damage_dummy_validates_common_target_pipeline(self):
        dummy_template_id = 7_107_304
        analyzer = DiagnosticAnalyzer(
            {
                "7100001": {"boss_type": 3, "name": "其他 Boss"},
                str(dummy_template_id): {
                    "boss_type": 0,
                    "name": "伤害木桩",
                },
            }
        )
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": False,
                "damage_source": "script",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [],
                "native_boss_records": [
                    native_boss_record(dummy_template_id, 0)
                ],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": False,
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(
            report["assessment"]["code"], "damage_dummy_pipeline_ok"
        )
        capture = report["capture"]
        self.assertEqual(capture["confirmed_boss_damage_events"], 0)
        self.assertEqual(capture["damage_dummy_damage_events"], 1)
        target = capture["damage_targets"][0]
        self.assertTrue(target["damage_dummy"])
        self.assertTrue(target["target_mode_displayable"])
        links = capture["target_identity_links"]
        self.assertEqual(links["native_entity_exact_matches"], 1)
        self.assertEqual(links["native_template_exact_matches"], 1)
        self.assertEqual(links["damage_dummy_exact_matches"], 1)

    def test_unknown_target_is_not_reported_as_displayable(self):
        analyzer = DiagnosticAnalyzer(
            {"7100001": {"boss_type": 3, "name": "其他 Boss"}}
        )
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": False,
                "damage_source": "script",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [],
                "native_boss_records": [],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": False,
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(
            report["assessment"]["code"], "damage_target_not_confirmed"
        )
        self.assertEqual(report["capture"]["forwarded_damage_events"], 1)
        self.assertEqual(report["capture"]["boss_confirmed_damage_events"], 0)
        self.assertFalse(
            report["capture"]["damage_targets"][0]["boss_mode_displayable"]
        )

    def test_native_damage_without_boss_confirmation_matches_user_report(self):
        analyzer = DiagnosticAnalyzer(
            {"7100001": {"boss_type": 3, "name": "其他 Boss"}}
        )
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": True,
                "native_boss_type_hook_installed": True,
                "team_stats_hook_installed": True,
                "damage_source": "native",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [native_damage_record()],
                "native_boss_records": [],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": True,
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(
            report["assessment"]["code"], "damage_target_not_confirmed"
        )
        capture = report["capture"]
        self.assertEqual(capture["network_damage_suppressed_by_native"], 1)
        self.assertEqual(capture["native_positive_damage_records"], 1)
        self.assertEqual(capture["parsed_damage_events"], 1)
        self.assertEqual(capture["forwarded_damage_events"], 1)
        self.assertEqual(capture["boss_confirmed_damage_events"], 0)

    def test_empty_catalog_is_reported_separately_from_capture_failure(self):
        analyzer = DiagnosticAnalyzer(None)
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": False,
                "damage_source": "script",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [],
                "native_boss_records": [],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": False,
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(report["assessment"]["code"], "boss_catalog_unavailable")
        self.assertEqual(report["capture"]["boss_catalog_size"], 0)

    def test_non_boss_target_reports_anonymous_template_evidence(self):
        analyzer = DiagnosticAnalyzer(
            {
                "7100001": {"boss_type": 3, "name": "其他 Boss"},
                "7100042": {"boss_type": 0, "name": "普通目标"},
            }
        )
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": False,
                "damage_source": "script",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [],
                "native_boss_records": [native_boss_record(7_100_042, 0)],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": False,
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(
            report["assessment"]["code"], "damage_target_not_confirmed"
        )
        target = report["capture"]["damage_targets"][0]
        self.assertEqual(target["template_id"], 7_100_042)
        self.assertEqual(target["runtime_boss_type"], 0)
        self.assertTrue(target["catalog_known"])
        self.assertFalse(target["catalog_match"])
        self.assertFalse(target["confirmed_boss"])
        self.assertNotIn("target_id", target)

    def test_report_does_not_retain_raw_identity_fields(self):
        analyzer = DiagnosticAnalyzer(None)
        analyzer.handle(
            "connected",
            {
                "game_pid": 1,
                "native_damage_hook_installed": False,
                "damage_source": "script",
            },
        )
        record = damage_record()
        record["decoded_arguments"].append("private-player-token")
        record["character_name"] = "private-player-name"
        analyzer.handle(
            "batch",
            {
                "records": [record],
                "native_records": [],
                "native_boss_records": [],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": False,
            },
        )

        report_text = str(analyzer.finish({"elevated": True}))

        self.assertNotIn("private-player-token", report_text)
        self.assertNotIn("private-player-name", report_text)
        self.assertNotIn(str(TARGET_ID), report_text)

    def test_lookup_report_distinguishes_unreadable_object_table(self):
        analyzer = DiagnosticAnalyzer(
            {"7100001": {"boss_type": 3, "name": "其他 Boss"}}
        )
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": True,
                "damage_source": "native",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [native_damage_record()],
                "native_boss_records": [],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": True,
                "native_diagnostic": {
                    "target_boss_lookup_enabled": True,
                    "target_lookup_candidates": 1,
                    "target_lookup_object_scans": 1,
                    "target_lookup_trusted_classes": 1,
                    "target_lookup_object_table_reads": 1,
                    "target_lookup_object_table_failures": 1,
                    "target_lookup_object_slots_scanned": 0,
                },
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(
            report["assessment"]["code"],
            "target_lookup_object_table_unreadable",
        )

    def test_lookup_report_distinguishes_exact_target_with_zero_template(self):
        analyzer = DiagnosticAnalyzer(
            {"7100001": {"boss_type": 3, "name": "其他 Boss"}}
        )
        analyzer.handle(
            "connected",
            {
                "game_pid": 9784,
                "native_damage_hook_installed": True,
                "damage_source": "native",
            },
        )
        analyzer.handle(
            "batch",
            {
                "records": [damage_record()],
                "native_records": [native_damage_record()],
                "native_boss_records": [],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": True,
                "native_diagnostic": {
                    "target_boss_lookup_enabled": True,
                    "target_lookup_candidates": 1,
                    "target_lookup_object_scans": 1,
                    "target_lookup_trusted_classes": 1,
                    "target_lookup_object_slots_scanned": 100,
                    "target_lookup_object_class_candidates": 5,
                    "target_lookup_object_exact_entity_matches": 1,
                    "target_lookup_object_exact_template_zero": 1,
                    "target_lookup_timeouts": 1,
                },
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(
            report["assessment"]["code"],
            "target_lookup_exact_component_template_zero",
        )


if __name__ == "__main__":
    unittest.main()
