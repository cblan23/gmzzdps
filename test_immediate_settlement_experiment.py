import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from immediate_settlement_experiment import (
    EXPERIMENT_MODE_OBSERVE,
    EXPERIMENT_MODE_QUERY,
    OneShotTeamStatsHookDriver,
    PassiveOutboundRpcObserver,
    SettlementExperimentCoordinator,
    experiment_settings,
    is_settlement_outbound_candidate,
    summarize_statistics_record,
    summarize_outbound_rpc,
)


def encounter(*, status="PENDING", result="WIPE"):
    return SimpleNamespace(
        local_encounter_id="encounter-1",
        instance_id="instance-1",
        started_at_ns=1_000_000_000,
        ended_at_ns=2_000_000_000,
        result=result,
        settlement_status=status,
        server_battle_id=None,
        stage_id=55,
        stage_index=1,
        participants_snapshot=[
            {"id": "self", "iid": 100, "name": "Self"},
            {"id": "peer", "iid": 200, "name": "Peer"},
        ],
        match_confidence="unmatched",
    )


class ExperimentSettingsTests(unittest.TestCase):
    def test_default_is_fully_inert(self):
        self.assertEqual(
            experiment_settings({}, frozen=False),
            (False, EXPERIMENT_MODE_QUERY, False, ""),
        )

    def test_observe_never_enables_query(self):
        self.assertEqual(
            experiment_settings(
                {
                    "immediate_settlement_experiment": True,
                    "immediate_settlement_experiment_mode": "observe",
                },
                frozen=False,
            ),
            (True, EXPERIMENT_MODE_OBSERVE, False, ""),
        )

    def test_frozen_build_cannot_enable_experimental_query(self):
        enabled, mode, query_enabled, reason = experiment_settings(
            {"immediate_settlement_experiment": True}, frozen=True
        )
        self.assertTrue(enabled)
        self.assertEqual(mode, EXPERIMENT_MODE_QUERY)
        self.assertFalse(query_enabled)
        self.assertEqual(reason, "FROZEN_BUILD_QUERY_DISABLED")


class StatisticsSummaryTests(unittest.TestCase):
    def test_team_counter_is_logged_but_never_declared_matcher_compatible(self):
        rows = summarize_statistics_record(
            {
                "method": "RetCommonCombatStatisticsByTeam",
                "capture_timestamp_ns": 3_000_000_000,
                "decoded_arguments": [
                    {
                        "$map": [
                            ["self", {"$map": [[0, 100], [4, "Self"], [5, 50]]}],
                            ["peer", {"$map": [[0, 200], [4, "Peer"], [5, 80]]}],
                        ]
                    }
                ],
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["member_count"], 2)
        self.assertFalse(rows[0]["matcher_compatible"])
        self.assertEqual(
            [member["damage"] for member in rows[0]["members"]],
            [50, 80],
        )

    def test_settlement_outbound_candidate_and_summary(self):
        record = {
            "method": "ReqCreateEncounter",
            "sequence": 9,
            "filetime_100ns": 116_444_736_010_000_000,
            "script_entity": 123,
            "variadic_argument_count": 2,
            "variadic_qwords": [1, 2, 3],
            "entry_stack_cells": [4, 5],
            "return_address": "0x1234",
            "lua_argument_cells": [6, 7],
            "lua_argument_snapshot": "01020304",
            "argument_sync_state": 1,
            "argument_descriptor_vector": {
                "items": [
                    {
                        "address": 8,
                        "type_name": "integer",
                        "lua_type": 2,
                        "snapshot": "aabb",
                    }
                ]
            },
        }
        self.assertTrue(is_settlement_outbound_candidate(record))
        self.assertFalse(
            is_settlement_outbound_candidate({"method": "ReqChatMessage"})
        )
        summary = summarize_outbound_rpc(record)
        self.assertEqual(summary["method"], "ReqCreateEncounter")
        self.assertEqual(summary["event_time_ns"], 1_000_000_000)
        self.assertEqual(summary["argument_count"], 2)
        self.assertEqual(summary["direction"], "C->S")
        self.assertEqual(summary["argument_cells"], [6, 7])
        self.assertEqual(summary["argument_snapshot"]["bytes"], 4)
        self.assertEqual(summary["argument_descriptors"][0]["type_name"], "integer")

    def test_causal_combat_requests_are_always_candidates(self):
        self.assertTrue(
            is_settlement_outbound_candidate({"method": "ReqReportLockTarget"})
        )
        self.assertTrue(
            is_settlement_outbound_candidate({"method": "ReqCastSkillNew"})
        )


class CoordinatorTests(unittest.TestCase):
    def test_lifecycle_event_is_written_without_mutating_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = SettlementExperimentCoordinator(
                Path(directory),
                enabled=True,
                mode=EXPERIMENT_MODE_QUERY,
                query_enabled=True,
            )
            coordinator.lifecycle("capture_start", reason="test")

            self.assertEqual(coordinator.attempts, {})
            contents = coordinator.log_path.read_text(encoding="utf-8")
            self.assertIn("[Lifecycle] capture_start", contents)
            self.assertIn('"reason":"test"', contents)

    def test_wipe_is_scheduled_and_submitted_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = SettlementExperimentCoordinator(
                Path(directory),
                enabled=True,
                mode=EXPERIMENT_MODE_QUERY,
                query_enabled=True,
            )
            target = encounter()
            self.assertTrue(coordinator.encounter_ended(target))
            self.assertFalse(coordinator.encounter_ended(target))
            self.assertTrue(coordinator.query_submitted(target.local_encounter_id))
            self.assertFalse(coordinator.query_submitted(target.local_encounter_id))

    def test_failed_response_preserves_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = SettlementExperimentCoordinator(
                Path(directory),
                enabled=True,
                mode=EXPERIMENT_MODE_QUERY,
                query_enabled=True,
            )
            target = encounter()
            coordinator.encounter_ended(target)
            coordinator.query_submitted(target.local_encounter_id)
            coordinator.query_event(
                {
                    "event": "triggered",
                    "local_encounter_id": target.local_encounter_id,
                    "request_trigger_time_ns": 2_500_000_000,
                }
            )
            coordinator.statistics_received(
                [
                    {
                        "method": "RetCommonCombatStatisticsByTeam",
                        "received_at_ns": 3_000_000_000,
                        "valid": False,
                        "member_count": 0,
                    }
                ]
            )
            coordinator.attempts[target.local_encounter_id].response_deadline = (
                time.monotonic() - 1
            )
            coordinator.poll({target.local_encounter_id: target})
            attempt = coordinator.attempts[target.local_encounter_id]
            self.assertTrue(attempt.final)
            self.assertIn("EMPTY_MEMBERS", attempt.failures)
            self.assertEqual(target.settlement_status, "PENDING")

    def test_only_high_complete_match_records_success(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = SettlementExperimentCoordinator(
                Path(directory),
                enabled=True,
                mode=EXPERIMENT_MODE_QUERY,
                query_enabled=True,
            )
            target = encounter(status="SETTLED")
            coordinator.encounter_ended(target)
            coordinator.matcher_events(
                [
                    {
                        "encounter_id": target.local_encounter_id,
                        "confidence": "high",
                        "reason": "bound_battle_id_exact_roster",
                        "received_at_ns": 3_000_000_000,
                        "settlement_status_before": "PENDING",
                        "settlement_status_after": "SETTLED",
                        "complete": True,
                        "member_count": 2,
                        "members": [
                            {"id": "self", "damage": 50},
                            {"id": "peer", "damage": 80},
                        ],
                    }
                ],
                encounters={target.local_encounter_id: target},
            )
            attempt = coordinator.attempts[target.local_encounter_id]
            self.assertTrue(attempt.final)
            self.assertTrue(attempt.matched)
            self.assertTrue(attempt.complete)


class OneShotDriverTests(unittest.TestCase):
    def test_driver_disables_and_closes_after_first_counter_increment(self):
        events = []

        class FakeHook:
            instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False
                self.enabled = False
                self.polls = 0
                FakeHook.instance = self

            def install(self):
                return self

            def arm_one_shot(self):
                return 9

            def status(self):
                self.polls += 1
                return {
                    "request_count": 10,
                    "last_result": 1,
                    "last_request_filetime": 123,
                }

            def set_enabled(self, enabled):
                self.enabled = bool(enabled)

            def close(self):
                self.closed = True

        driver = OneShotTeamStatsHookDriver({}, events.append)
        with patch(
            "team_stats_request_hook.TeamStatsRequestHook", FakeHook
        ):
            self.assertTrue(
                driver.arm({"local_encounter_id": "encounter-1"}, game_pid=42)
            )
            driver.poll()

        self.assertIsNone(driver.active)
        self.assertTrue(FakeHook.instance.closed)
        self.assertFalse(FakeHook.instance.enabled)
        self.assertEqual(
            FakeHook.instance.kwargs["allow_existing_adoption"], False
        )
        self.assertEqual(
            [event["event"] for event in events],
            ["installing", "armed", "triggered"],
        )


class PassiveOutboundObserverTests(unittest.TestCase):
    def test_observer_records_natural_calls_with_injection_disabled(self):
        events = []

        class FakeHook:
            instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.alive = True
                self.closed = False
                FakeHook.instance = self

            def install(self):
                return self

            def status(self):
                return {"enabled": False}

            def is_attached(self):
                return True

            def poll_requests(self):
                return [
                    {
                        "method": "ReqCreateEncounter",
                        "sequence": 1,
                        "filetime_100ns": 116_444_736_010_000_000,
                    },
                    {
                        "method": "ReqChatMessage",
                        "sequence": 2,
                        "filetime_100ns": 116_444_736_020_000_000,
                    },
                ]

            def set_enabled(self, _enabled):
                pass

            def close(self):
                self.closed = True

        observer = PassiveOutboundRpcObserver({}, events.append)
        with patch("team_stats_request_hook.TeamStatsRequestHook", FakeHook):
            self.assertTrue(observer.sync(game_pid=42))
            records = observer.poll()
            observer.close()

        self.assertEqual(len(records), 2)
        self.assertFalse(FakeHook.instance.kwargs["enabled"])
        self.assertFalse(FakeHook.instance.kwargs["stable_primary_only"])
        self.assertFalse(FakeHook.instance.kwargs["allow_existing_adoption"])
        self.assertEqual(
            FakeHook.instance.kwargs["synchronized_methods"],
            ("ReqReportLockTarget", "ReqCastSkillNew"),
        )
        self.assertTrue(FakeHook.instance.closed)
        self.assertEqual(
            [event["event"] for event in events],
            [
                "outbound_observer_installing",
                "outbound_observer_installed",
                "outbound_rpc",
                "outbound_observer_closed",
            ],
        )
        self.assertEqual(events[2]["method"], "ReqCreateEncounter")


if __name__ == "__main__":
    unittest.main()
