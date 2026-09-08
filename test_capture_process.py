#!/usr/bin/env python3

from __future__ import annotations

import io
import multiprocessing
import queue
import runpy
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import capture_process as capture_module
from capture_process import (
    CaptureProcessClient,
    NetworkPoller,
    TEAM_STATS_MODE_DUMMY,
    TEAM_STATS_MODE_TEAM,
    TEAM_STATS_MODE_UNKNOWN,
    TeamStatsResponseHealth,
    authorized_team_detail_request_methods,
    collect_native_records,
    normalize_team_stats_mode,
    passive_team_detail_request_methods,
)
from network_state import (
    TRAINING_DUMMY_TEMPLATE_IDS,
    is_training_dummy_template_id,
)
from runtime_capability import create_development_capability


def development_capability():
    return create_development_capability(
        Path(__file__).resolve().with_name("runtime-profile.dev.json"),
        session_id="1" * 32,
        client_id="2" * 32,
        build_id="source-development",
        client_build="0.1.2+test",
    )


def _ipc_pressure_producer(output_queue, total: int, batch_size: int) -> None:
    sequence = 0
    batch_id = 0
    while sequence < total:
        stop = min(total, sequence + batch_size)
        output_queue.put(
            (
                "batch",
                {
                    "batch_id": batch_id,
                    "records": [
                        {
                            "sequence": item,
                            "method": "OnMsgSyncFightMode",
                            "filetime_100ns": 134_321_845_000_000_000 + item,
                        }
                        for item in range(sequence, stop)
                    ],
                },
            )
        )
        sequence = stop
        batch_id += 1
    output_queue.put(("stopped", {"batches": batch_id}))


class _AliveWatchdog:
    def is_alive(self) -> bool:
        return True


class _FiniteNetworkHook:
    def __init__(self, batches: list[list[dict]]):
        self.batches = list(batches)
        self._alive = True

    @property
    def alive(self) -> bool:
        return self._alive

    def poll(self, **_kwargs) -> list[dict]:
        if self.batches:
            return self.batches.pop(0)
        self._alive = False
        return []


class _PersistentNetworkHook:
    alive = True

    def poll(self, **_kwargs) -> list[dict]:
        return []


class _NativeHook:
    def __init__(self, calls: list[str], fail_at: str = ""):
        self.calls = calls
        self.fail_at = fail_at

    def _result(self, name: str) -> list[dict]:
        self.calls.append(name)
        if self.fail_at == name:
            raise RuntimeError(name)
        return [{"function": name}]

    def poll(self) -> list[dict]:
        return self._result("damage")

    def poll_boss_types(self) -> list[dict]:
        return self._result("boss")

    def poll_names(self) -> list[dict]:
        return self._result("name")

    def poll_skill_names(self) -> list[dict]:
        return self._result("skill")


class CaptureProcessTests(unittest.TestCase):
    def test_team_response_health_recovers_request_growth_without_replies(self):
        health = TeamStatsResponseHealth(
            timeout_seconds=20.0,
            minimum_requests=8,
            maximum_reinstalls=1,
        )
        health.set_mode(TEAM_STATS_MODE_TEAM, 100.0)
        health.mark_hook_installed(100.0)
        health.observe_records(
            [{"method": "OnMsgDamageSyncV2"}], 120.0
        )

        self.assertIsNone(
            health.assess(121.0, {"enabled": True, "request_count": 7})
        )
        status = {"enabled": True, "request_count": 8}
        self.assertEqual(health.assess(121.0, status), "rearm")
        health.mark_rearmed(121.0, status)

        self.assertIsNone(
            health.assess(140.9, {"enabled": True, "request_count": 40})
        )
        health.observe_native_damage([{"damage": 1}], 140.9)
        self.assertEqual(
            health.assess(141.0, {"enabled": True, "request_count": 41}),
            "reinstall",
        )
        health.mark_reinstalled(141.0)
        health.mark_hook_installed(141.0, preserve_recovery=True)
        health.observe_records(
            [{"method": "OnMsgSyncCurrentHp"}], 161.9
        )

        self.assertIsNone(
            health.assess(162.0, {"enabled": True, "request_count": 8})
        )
        self.assertEqual(health.state, "unhealthy")
        self.assertEqual(health.rearm_count, 1)
        self.assertEqual(health.reinstall_count, 1)

    def test_real_team_response_resets_the_recovery_epoch(self):
        health = TeamStatsResponseHealth(
            timeout_seconds=20.0,
            minimum_requests=8,
        )
        health.set_mode(TEAM_STATS_MODE_TEAM, 100.0)
        health.mark_hook_installed(100.0)

        self.assertEqual(
            health.observe_records(
                [
                    {"method": "OnMsgDamageSyncV2", "filetime_100ns": 111},
                    {
                        "method": "RetCommonCombatStatisticsByTeam",
                        "filetime_100ns": 222,
                    },
                ],
                112.0,
            ),
            1,
        )
        self.assertIsNone(
            health.assess(125.0, {"enabled": True, "request_count": 20})
        )
        snapshot = health.snapshot(
            125.0, {"enabled": True, "request_count": 20}
        )
        self.assertEqual(snapshot["response_health"], "healthy")
        self.assertEqual(snapshot["response_count"], 1)
        self.assertEqual(snapshot["last_response_filetime"], 222)
        self.assertEqual(snapshot["last_response_age_seconds"], 13.0)

    def test_team_response_health_does_not_recover_outside_combat(self):
        health = TeamStatsResponseHealth(
            timeout_seconds=20.0,
            minimum_requests=8,
        )
        health.set_mode(TEAM_STATS_MODE_TEAM, 100.0)
        health.mark_hook_installed(100.0)

        self.assertIsNone(
            health.assess(180.0, {"enabled": True, "request_count": 80})
        )
        self.assertEqual(health.rearm_count, 0)
        self.assertEqual(health.reinstall_count, 0)

    def test_parameterized_detail_requests_are_never_selected_for_active_calls(self):
        profile = development_capability().profile

        self.assertEqual(authorized_team_detail_request_methods(profile), ())
        altered = dict(profile)
        altered["protocol"] = dict(profile["protocol"])
        altered["protocol"]["synchronized_methods"] = (
            *profile["protocol"]["synchronized_methods"],
            "ReqUnknown",
        )
        self.assertEqual(authorized_team_detail_request_methods(altered), ())

    def test_signed_parameterized_detail_requests_are_selected_for_passive_capture(self):
        profile = development_capability().profile

        self.assertEqual(
            passive_team_detail_request_methods(profile),
            (
                "ReqCommonCombatStatistics",
                "ReqDungeonBattleStatistics",
                "ReqMonsterBattleStatistics",
                "ReqNpcCombatStatisticsByTeam",
                "ReqDirtyNpcCombatStatisticsByTeam",
            ),
        )
        altered = dict(profile)
        altered["protocol"] = dict(profile["protocol"])
        altered["protocol"]["synchronized_methods"] = (
            "ReqUnknown",
            "ReqNpcCombatStatisticsByTeam",
            "ReqNpcCombatStatisticsByTeam",
        )
        self.assertEqual(
            passive_team_detail_request_methods(altered),
            ("ReqNpcCombatStatisticsByTeam",),
        )

    def test_native_diagnostic_keeps_both_template_hook_states(self):
        sanitized = capture_module._sanitize_native_diagnostic(
            {
                "template_id_hook_installed": True,
                "template_bulk_hook_installed": True,
                "private_address": "0x12345678",
            }
        )

        self.assertEqual(
            sanitized,
            {
                "template_id_hook_installed": True,
                "template_bulk_hook_installed": True,
            },
        )

    def _run_fake_capture_lifecycle(
        self,
        initial_mode: int,
        transitions: tuple[int, ...] = (),
    ) -> tuple[list[object], list[tuple[str, object]]]:
        """Exercise the child controller without opening a game process."""
        calls: list[object] = []
        output_queue: queue.Queue = queue.Queue()
        stop_event = threading.Event()
        target_lookup_event = threading.Event()

        class SharedMode:
            value = initial_mode

        class NetworkHook:
            pid = 4321
            adopted = False

            def __init__(self, **_kwargs):
                self.alive = True

            def install(self):
                calls.append("network_install")
                return self

            def poll(self, **_kwargs):
                return []

            def close(self):
                calls.append("network_close")
                self.alive = False

        class NativeHook:
            adopted = False
            name_installed = True
            boss_type_installed = True
            boss_init_installed = True

            def __init__(self, **_kwargs):
                pass

            def install(self):
                calls.append("native_install")
                return self

            def set_target_boss_lookup_enabled(self, _enabled):
                pass

            def poll(self):
                return []

            def poll_boss_types(self):
                return []

            def poll_names(self):
                return []

            def poll_skill_names(self):
                return []

            def close(self):
                calls.append("native_close")

        class TeamHook:
            def __init__(self, **_kwargs):
                calls.append("team_construct")
                calls.append(("team_options", dict(_kwargs)))
                self.alive = True
                self.enabled = True

            def install(self):
                calls.append("team_install")
                return self

            def set_enabled(self, enabled):
                self.enabled = bool(enabled)
                calls.append(("team_enabled", self.enabled))

            def status(self):
                return {
                    "enabled": self.enabled,
                    "request_count": 0,
                    "last_result": 0,
                    "last_request_filetime": 0,
                }

            def poll_requests(self):
                return []

            def close(self):
                calls.append("team_close")
                self.alive = False

        def wait_for(predicate, description: str) -> None:
            deadline = __import__("time").monotonic() + 2.0
            while __import__("time").monotonic() < deadline:
                if predicate():
                    return
                threading.Event().wait(0.005)
            self.fail(f"timed out waiting for {description}: {calls!r}")

        with (
            patch.object(capture_module, "NetworkMessageHook", NetworkHook),
            patch.object(capture_module, "DamageHook", NativeHook),
            patch.object(capture_module, "TeamStatsRequestHook", TeamHook),
        ):
            capability = development_capability()

            class SharedExpiry:
                value = capability.expires_at

            controller = threading.Thread(
                target=capture_module._capture_forever,
                args=(
                    stop_event,
                    output_queue,
                    _AliveWatchdog(),
                    target_lookup_event,
                    capability.profile,
                    SharedExpiry,
                    SharedMode,
                ),
            )
            controller.start()
            wait_for(
                lambda: any(
                    kind == "connected" for kind, _payload in list(output_queue.queue)
                ),
                "fake capture connection",
            )
            for next_mode in transitions:
                installs_before = calls.count("team_install")
                enables_before = calls.count(("team_enabled", True))
                disables_before = calls.count(("team_enabled", False))
                SharedMode.value = next_mode
                if next_mode == TEAM_STATS_MODE_TEAM:
                    wait_for(
                        lambda: (
                            calls.count("team_install") > installs_before
                            or calls.count(("team_enabled", True))
                            > enables_before
                        ),
                        "team hook install or re-enable",
                    )
                else:
                    wait_for(
                        lambda: calls.count(("team_enabled", False))
                        > disables_before,
                        "team hook disable",
                    )
            stop_event.set()
            controller.join(2.0)
            self.assertFalse(controller.is_alive())

        messages: list[tuple[str, object]] = []
        while True:
            try:
                messages.append(output_queue.get_nowait())
            except queue.Empty:
                break
        return calls, messages

    def test_target_boss_lookup_shared_event_defaults_off_and_updates(self):
        client = CaptureProcessClient(
            runtime_capability=development_capability(),
            allow_development=True,
            parent_pid=1234,
        )
        try:
            self.assertFalse(client.target_boss_lookup_event.is_set())
            client.set_target_boss_lookup_enabled(True)
            self.assertTrue(client.target_boss_lookup_event.is_set())
            client.set_target_boss_lookup_enabled(False)
            self.assertFalse(client.target_boss_lookup_event.is_set())
        finally:
            client.output_queue.close()
            client.process.close()

    def test_team_stats_mode_is_shared_and_legacy_default_stays_team(self):
        client = CaptureProcessClient(
            runtime_capability=development_capability(),
            allow_development=True,
            parent_pid=1234,
        )
        try:
            self.assertEqual(client.get_team_stats_mode(), "team")
            self.assertEqual(client.set_team_stats_mode("unknown"), "unknown")
            self.assertEqual(client.get_team_stats_mode(), "unknown")
            self.assertEqual(client.set_team_stats_mode("dummy"), "dummy")
            self.assertEqual(client.get_team_stats_mode(), "dummy")
            self.assertEqual(client.set_team_stats_mode(TEAM_STATS_MODE_TEAM), "team")
        finally:
            client.output_queue.close()
            client.process.close()

    def test_team_stats_mode_normalization_fails_closed_for_parent_updates(self):
        self.assertEqual(
            normalize_team_stats_mode("dummy", default=TEAM_STATS_MODE_UNKNOWN),
            TEAM_STATS_MODE_DUMMY,
        )
        self.assertEqual(
            normalize_team_stats_mode("not-a-mode", default=TEAM_STATS_MODE_UNKNOWN),
            TEAM_STATS_MODE_UNKNOWN,
        )
        self.assertEqual(
            normalize_team_stats_mode(None, default=TEAM_STATS_MODE_UNKNOWN),
            TEAM_STATS_MODE_UNKNOWN,
        )

    def test_training_dummy_catalog_is_exact_template_scoped(self):
        for template_id in TRAINING_DUMMY_TEMPLATE_IDS:
            self.assertTrue(is_training_dummy_template_id(template_id))
        self.assertFalse(is_training_dummy_template_id(1))
        self.assertFalse(is_training_dummy_template_id("木桩"))

    def test_unknown_and_dummy_modes_never_install_team_request_hook(self):
        for mode in (TEAM_STATS_MODE_UNKNOWN, TEAM_STATS_MODE_DUMMY):
            with self.subTest(mode=mode):
                calls, messages = self._run_fake_capture_lifecycle(mode)
                self.assertNotIn("team_construct", calls)
                connected = next(
                    payload for kind, payload in messages if kind == "connected"
                )
                self.assertFalse(connected["team_stats_hook_installed"])

    def test_team_mode_installs_only_stable_primary_request_hook(self):
        calls, messages = self._run_fake_capture_lifecycle(TEAM_STATS_MODE_TEAM)

        self.assertIn("team_construct", calls)
        self.assertIn("team_install", calls)
        connected = next(
            payload for kind, payload in messages if kind == "connected"
        )
        self.assertTrue(connected["team_stats_hook_installed"])
        self.assertEqual(connected["team_stats_mode"], "team")
        options = next(
            item[1]
            for item in calls
            if isinstance(item, tuple) and item[0] == "team_options"
        )
        self.assertTrue(options["stable_primary_only"])
        self.assertTrue(options["takeover_existing"])
        self.assertNotIn("additional_request_methods", options)
        self.assertNotIn("raw_lua_request_arguments", options)
        self.assertNotIn("synchronized_methods", options)

    def test_scene_transition_disables_then_reenables_same_team_hook(self):
        calls, _messages = self._run_fake_capture_lifecycle(
            TEAM_STATS_MODE_TEAM,
            (TEAM_STATS_MODE_UNKNOWN, TEAM_STATS_MODE_TEAM),
        )

        self.assertEqual(calls.count("team_install"), 1)
        self.assertIn(("team_enabled", False), calls)
        self.assertIn(("team_enabled", True), calls)

    def test_reader_can_close_without_waiting_for_queue_feeder(self):
        events: list[str] = []

        class OutputQueue:
            def cancel_join_thread(self) -> None:
                events.append("cancel_join")

            def close(self) -> None:
                events.append("queue_close")

            def join_thread(self) -> None:
                raise AssertionError("non-blocking close must not join the feeder")

        class Process:
            @staticmethod
            def is_alive() -> bool:
                return False

            @staticmethod
            def close() -> None:
                events.append("process_close")

        client = object.__new__(CaptureProcessClient)
        client.output_queue = OutputQueue()
        client.process = Process()

        client.close(wait_for_queue=False)

        self.assertEqual(events, ["cancel_join", "queue_close", "process_close"])

    def test_unstarted_reader_can_close_without_joining_process(self):
        events: list[str] = []

        class OutputQueue:
            def cancel_join_thread(self) -> None:
                events.append("cancel_join")

            def close(self) -> None:
                events.append("queue_close")

        class Process:
            @staticmethod
            def close() -> None:
                events.append("process_close")

        client = object.__new__(CaptureProcessClient)
        client.output_queue = OutputQueue()
        client.process = Process()

        client.close(wait_for_queue=False)

        self.assertEqual(events, ["cancel_join", "queue_close", "process_close"])

    def test_native_rings_keep_existing_poll_order(self):
        calls: list[str] = []
        records, error = collect_native_records(_NativeHook(calls))

        self.assertEqual(calls, ["damage", "boss", "name", "skill"])
        self.assertFalse(error)
        self.assertEqual(records["native_records"][0]["function"], "damage")
        self.assertEqual(records["native_boss_records"][0]["function"], "boss")
        self.assertEqual(records["native_name_records"][0]["function"], "name")
        self.assertEqual(
            records["native_skill_name_records"][0]["function"], "skill"
        )

    def test_native_poll_failure_preserves_records_already_copied(self):
        calls: list[str] = []
        records, error = collect_native_records(
            _NativeHook(calls, fail_at="name")
        )

        self.assertEqual(calls, ["damage", "boss", "name"])
        self.assertIn("RuntimeError: name", error)
        self.assertEqual(len(records["native_records"]), 1)
        self.assertEqual(len(records["native_boss_records"]), 1)
        self.assertEqual(records["native_name_records"], [])
        self.assertEqual(records["native_skill_name_records"], [])

    def test_network_poller_keeps_sequences_continuous(self):
        hook = _FiniteNetworkHook(
            [
                [{"sequence": 100}, {"sequence": 101}],
                [{"sequence": 102}],
                [{"sequence": 103}, {"sequence": 104}],
            ]
        )
        poller = NetworkPoller(hook, threading.Event(), _AliveWatchdog())
        poller.start()
        poller.join(2.0)

        self.assertFalse(poller.is_alive())
        self.assertEqual(
            [record["sequence"] for record in poller.drain_records()],
            [100, 101, 102, 103, 104],
        )
        self.assertEqual(poller.drain_sequence_gaps(), [])
        self.assertEqual(poller.take_error(), "")

    def test_network_poller_keeps_acknowledging_during_shared_shutdown(self):
        stop_event = threading.Event()
        poller = NetworkPoller(
            _PersistentNetworkHook(), stop_event, _AliveWatchdog()
        )
        poller.start()
        stop_event.set()
        threading.Event().wait(0.03)

        self.assertTrue(poller.is_alive())
        self.assertTrue(poller.stop_and_join(1.0))
        self.assertFalse(poller.is_alive())

    def test_network_poller_drops_rpc_methods_without_dps_consumers(self):
        hook = _FiniteNetworkHook(
            [
                [
                    {"sequence": 200, "method": "OnMsgDestroyBulletV2"},
                    {"sequence": 201, "method": "OnMsgSyncCurrentHp"},
                    {"sequence": 202, "method": "OnMsgBeforeEnterNewSpace"},
                    {"sequence": 203, "method": "OnUpdateTeamExample"},
                ]
            ]
        )
        poller = NetworkPoller(hook, threading.Event(), _AliveWatchdog())
        poller.start()
        poller.join(2.0)

        self.assertFalse(poller.is_alive())
        records = poller.drain_records()
        self.assertEqual(
            [record["sequence"] for record in records], [201, 202, 203]
        )
        self.assertEqual(poller.drain_sequence_gaps(), [])

    def test_unbounded_spawn_queue_preserves_five_times_mob_segment(self):
        # The observed mob segment contained 8,940 messages. Exercise five
        # times that volume through the exact spawn/FIFO queue configuration.
        total = 8_940 * 5
        context = multiprocessing.get_context("spawn")
        output_queue = context.Queue(maxsize=0)
        process = context.Process(
            target=_ipc_pressure_producer,
            args=(output_queue, total, 64),
        )
        process.start()
        expected_sequence = 0
        expected_batch = 0
        try:
            while True:
                kind, payload = output_queue.get(timeout=15.0)
                if kind == "stopped":
                    self.assertEqual(payload["batches"], expected_batch)
                    break
                self.assertEqual(kind, "batch")
                self.assertEqual(payload["batch_id"], expected_batch)
                expected_batch += 1
                for record in payload["records"]:
                    self.assertEqual(record["sequence"], expected_sequence)
                    expected_sequence += 1
        finally:
            process.join(5.0)
            if process.is_alive():
                process.terminate()
                process.join(2.0)
            output_queue.close()
            output_queue.join_thread()

        self.assertEqual(process.exitcode, 0)
        self.assertEqual(expected_sequence, total)


class HookWorkerBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = runpy.run_path(
            str(Path(__file__).with_name("dps_meter.pyw"))
        )

    def test_parent_keeps_native_then_network_processing_contract(self):
        worker = self.module["HookWorker"](
            self.module["queue"].Queue(), self.module["threading"].Event()
        )
        calls: list[tuple] = []

        class Parser:
            def process_native_boss_type(self, record):
                calls.append(("boss", record["function"]))
                return []

            def apply_runtime_boss_name(self, record):
                calls.append(("name", record["function"]))
                return None

            def process_native_damage(self, record):
                calls.append(("damage", record["function"]))
                return []

            def process(self, record, *, include_damage):
                calls.append(("network", record["function"], include_damage))
                return []

            def current_dungeon_context(self):
                return {}

            def take_team_profile_cache(self):
                return None

            def current_self_identity(self):
                return None

            def current_active_boss_state(self):
                return None

        batch = {
            "request_records": [
                {
                    "function": "outbound",
                    "method": "ReqTargetDetail",
                    "capture_source": "raw_outbound_rpc",
                }
            ],
            "native_boss_records": [
                {
                    "function": "boss",
                    "entity_id": 57_236_882_400_409,
                    "template_id": 0,
                    "boss_type": 3,
                }
            ],
            "native_name_records": [
                {"function": "name", "entity_id": 1, "name": "Player"}
            ],
            "native_skill_name_records": [
                {"function": "skill", "skill_id": 1, "name": "Skill"}
            ],
            "native_records": [
                {
                    "function": "damage",
                    "target_id": 57_236_882_400_409,
                    "damage": 1,
                }
            ],
            "records": [{"function": "network"}],
            "native_damage_hook_installed": True,
        }
        log_handle = io.StringIO()

        dirty = worker._process_capture_batch(Parser(), batch, log_handle, 1234)

        self.assertFalse(dirty)
        self.assertEqual(
            calls,
            [
                ("boss", "boss"),
                ("name", "name"),
                ("damage", "damage"),
                ("network", "network", True),
            ],
        )
        self.assertEqual(
            [line["function"] for line in map(__import__("json").loads, log_handle.getvalue().splitlines())],
            ["outbound", "boss", "name", "skill", "damage", "network"],
        )

    def test_target_boss_lookup_config_missing_defaults_off(self):
        enabled_from_config = self.module[
            "target_boss_lookup_enabled_from_config"
        ]

        self.assertFalse(enabled_from_config({}))
        self.assertFalse(enabled_from_config(None))
        self.assertTrue(
            enabled_from_config({"target_boss_lookup_enabled": True})
        )

    def test_hook_worker_accepts_runtime_target_lookup_toggle(self):
        worker = self.module["HookWorker"](
            self.module["queue"].Queue(),
            self.module["threading"].Event(),
        )

        self.assertFalse(worker.target_boss_lookup_event.is_set())
        worker.set_target_boss_lookup_enabled(True)
        self.assertTrue(worker.target_boss_lookup_event.is_set())
        self.assertTrue(
            worker.diagnostic_snapshot()["target_boss_lookup_enabled"]
        )

    def test_team_stats_classifier_keeps_dummy_out_of_active_rpc_path(self):
        classify = self.module["classify_team_stats_mode"]
        dummy_id = 7_114_223
        mode, reason = classify(
            object(),
            {
                "native_boss_records": [
                    {"entity_id": 99, "template_id": dummy_id, "boss_type": 3}
                ],
                "records": [],
            },
            "unknown",
        )
        self.assertEqual(mode, "dummy")
        self.assertEqual(reason, "exact_dummy_template")

    def test_team_stats_classifier_requires_explicit_dungeon_evidence(self):
        classify = self.module["classify_team_stats_mode"]
        mode, _reason = classify(
            object(),
            {
                "records": [
                    {
                        "method": "OnMsgDamageSyncV2",
                        "attacker_id": 1,
                        "target_id": 2,
                    }
                ]
            },
            "unknown",
        )
        self.assertEqual(mode, "unknown")

        mode, reason = classify(
            object(),
            {"records": [{"method": "OnMsgDungeonReadinessCheck"}]},
            "unknown",
        )
        self.assertEqual(mode, "team")
        self.assertEqual(reason, "dungeon_protocol")

    def test_team_stats_classifier_resets_on_scene_transition(self):
        classify = self.module["classify_team_stats_mode"]
        mode, reason = classify(
            object(),
            {"records": [{"method": "OnMsgBeforeEnterNewSpace"}]},
            "team",
        )
        self.assertEqual(mode, "unknown")
        self.assertEqual(reason, "scene_transition")

    def test_team_stats_classifier_accepts_explicit_multiplayer_party(self):
        classify = self.module["classify_team_stats_mode"]

        class Parser:
            @staticmethod
            def current_capture_context():
                return {
                    "party_member_count": 6,
                    "party_seen": True,
                }

        mode, reason = classify(Parser(), {"records": []}, "unknown")
        self.assertEqual(mode, "team")
        self.assertEqual(reason, "multiplayer_party")

    def test_team_stats_classifier_exact_dummy_wins_over_party_roster(self):
        classify = self.module["classify_team_stats_mode"]
        dummy_id = 7_114_223

        class Parser:
            @staticmethod
            def current_capture_context():
                return {
                    "active_target_id": 99,
                    "active_target_is_training_dummy": True,
                    "target_is_training_dummy": True,
                    "party_member_count": 2,
                    "party_seen": True,
                }

        mode, reason = classify(
            Parser(),
            {
                "native_boss_records": [
                    {"entity_id": 99, "template_id": dummy_id, "boss_type": 3}
                ],
                "records": [],
            },
            "unknown",
        )
        self.assertEqual(mode, "dummy")
        self.assertEqual(reason, "exact_dummy_template")

    def test_team_stats_classifier_recovers_after_readiness_scene_transition(self):
        classify = self.module["classify_team_stats_mode"]

        class Parser:
            party_member_count = 0

            @classmethod
            def current_capture_context(cls):
                return {
                    "party_member_count": cls.party_member_count,
                    "party_seen": cls.party_member_count > 0,
                }

        mode, reason = classify(
            Parser(),
            {"records": [{"method": "OnMsgDungeonReadinessCheck"}]},
            "unknown",
        )
        self.assertEqual((mode, reason), ("team", "dungeon_protocol"))

        mode, reason = classify(
            Parser(),
            {"records": [{"method": "OnMsgBeforeEnterNewSpace"}]},
            mode,
        )
        self.assertEqual((mode, reason), ("unknown", "scene_transition"))

        Parser.party_member_count = 6
        mode, reason = classify(Parser(), {"records": []}, mode)
        self.assertEqual((mode, reason), ("team", "multiplayer_party"))

    def test_team_stats_classifier_accepts_parser_confirmed_non_dummy_boss(self):
        classify = self.module["classify_team_stats_mode"]

        class Parser:
            @staticmethod
            def current_capture_context():
                return {
                    "active_target_id": 123,
                    "active_target_is_training_dummy": False,
                    "target_is_training_dummy": False,
                    "has_non_dummy_boss": True,
                    "party_seen": True,
                }

        mode, reason = classify(Parser(), {"records": []}, "unknown")
        self.assertEqual(mode, "team")
        self.assertEqual(reason, "confirmed_boss")


if __name__ == "__main__":
    unittest.main()
