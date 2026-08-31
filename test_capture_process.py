#!/usr/bin/env python3

from __future__ import annotations

import io
import multiprocessing
import queue
import runpy
import threading
import unittest
from pathlib import Path

from capture_process import CaptureProcessClient, NetworkPoller, collect_native_records


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
    def test_target_boss_lookup_shared_event_defaults_off_and_updates(self):
        client = CaptureProcessClient(parent_pid=1234)
        try:
            self.assertFalse(client.target_boss_lookup_event.is_set())
            client.set_target_boss_lookup_enabled(True)
            self.assertTrue(client.target_boss_lookup_event.is_set())
            client.set_target_boss_lookup_enabled(False)
            self.assertFalse(client.target_boss_lookup_event.is_set())
        finally:
            client.output_queue.close()
            client.process.close()

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
                ("network", "network", False),
            ],
        )
        self.assertEqual(
            [line["function"] for line in map(__import__("json").loads, log_handle.getvalue().splitlines())],
            ["boss", "name", "skill", "damage", "network"],
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


if __name__ == "__main__":
    unittest.main()
