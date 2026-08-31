#!/usr/bin/env python3

from __future__ import annotations

import unittest

from diagnostic_report import DiagnosticAnalyzer, diagnostic_environment


def damage_record(sequence: int = 1) -> dict:
    return {
        "sequence": sequence,
        "method": "OnMsgDamageSyncV2",
        "filetime_100ns": 134_321_845_000_000_000 + sequence,
        "script_entity": 0x1234,
        "decode_delay_ms": 0.2,
        "decoded_arguments": [
            57_277_687_900_295,
            57_456_999_419_492,
            860_200_100,
            2,
            2,
            1_234,
            0,
            1_234,
            False,
        ],
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

    def test_network_fallback_produces_displayable_damage(self):
        analyzer = DiagnosticAnalyzer(None)
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
                "native_boss_records": [],
                "native_name_records": [],
                "native_skill_name_records": [],
                "native_damage_hook_installed": False,
            },
        )

        report = analyzer.finish({"elevated": True})

        self.assertEqual(report["assessment"]["code"], "capture_pipeline_ok")
        self.assertEqual(report["capture"]["parsed_damage_events"], 1)
        self.assertEqual(report["capture"]["forwarded_damage_events"], 1)

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


if __name__ == "__main__":
    unittest.main()
