import unittest
import time

from hybrid_team_stats import (
    HybridTeamStatsHookDriver,
    HybridTeamStatsReceiveDriver,
    hybrid_team_stats_settings,
)


class FakeHook:
    instances = []

    def __init__(self, **options):
        self.options = dict(options)
        self.enabled = bool(options.get("enabled"))
        self.alive = True
        self.adopted = False
        self.request_count = 0
        self.last_result = 0
        self.closed = False
        self.attached = True
        self.arm_count = 0
        self.script_entity_override = 0
        self.__class__.instances.append(self)

    def install(self):
        return self

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)

    def arm_one_shot(self):
        self.arm_count += 1
        self.enabled = True
        return self.request_count

    def set_script_entity_override(self, value):
        self.script_entity_override = int(value or 0)

    def status(self):
        return {
            "enabled": self.enabled,
            "request_count": self.request_count,
            "last_result": self.last_result,
            "last_request_filetime": 123,
            "script_entity_override": self.script_entity_override,
            "last_script_entity": 444,
        }

    def is_attached(self):
        return self.attached

    def close(self):
        self.closed = True
        self.alive = False


class FakeReceiveHook:
    instances = []

    def __init__(self, **options):
        self.options = dict(options)
        self.alive = True
        self.adopted = False
        self.closed = False
        self.pending = []
        self.__class__.instances.append(self)

    def install(self):
        return self

    def poll(self, **options):
        records, self.pending = self.pending, []
        for record in records:
            method = str(record.get("method", ""))
            if options["decode_method_filter"](method):
                record.setdefault("decoded_arguments", [{}])
        return records

    def close(self):
        self.closed = True
        self.alive = False


class HybridTeamStatsTests(unittest.TestCase):
    def setUp(self):
        FakeHook.instances.clear()
        FakeReceiveHook.instances.clear()
        self.events = []

    def driver(self):
        return HybridTeamStatsHookDriver(
            {"profile": "test"},
            self.events.append,
            interval=1.0,
            hook_factory=FakeHook,
        )

    def test_settings_are_explicit_and_bounded(self):
        self.assertEqual(hybrid_team_stats_settings({}), (True, 1.0))
        self.assertEqual(
            hybrid_team_stats_settings(
                {"team_stats_hybrid_hook_enabled": "false"}
            ),
            (False, 1.0),
        )
        self.assertEqual(
            hybrid_team_stats_settings(
                {
                    "team_stats_hybrid_hook_enabled": "true",
                    "team_stats_hybrid_interval_seconds": 0.1,
                }
            ),
            (True, 0.75),
        )

    def test_hook_is_installed_only_for_team_mode(self):
        driver = self.driver()
        driver.sync(game_pid=44, mode="unknown")
        self.assertEqual(FakeHook.instances, [])

        driver.sync(
            game_pid=44,
            mode="team",
            script_entity_override=4044,
        )
        hook = FakeHook.instances[-1]
        self.assertTrue(hook.enabled)
        self.assertTrue(hook.options["stable_primary_only"])
        self.assertEqual(hook.options["interval"], 1.0)

        driver.sync(game_pid=44, mode="dummy")
        self.assertFalse(hook.enabled)
        driver.poll(force=True)
        self.assertFalse(driver.status_snapshot()["enabled"])

        driver.sync(
            game_pid=44,
            mode="team",
            script_entity_override=4044,
        )
        self.assertTrue(hook.enabled)
        self.assertEqual(hook.arm_count, 1)
        driver.close()
        self.assertTrue(hook.closed)

    def test_persistent_hook_proves_one_shot_trigger_without_detaching(self):
        driver = self.driver()
        driver.sync(
            game_pid=55,
            mode="team",
            script_entity_override=5055,
        )
        hook = FakeHook.instances[-1]
        self.assertTrue(
            driver.arm({"local_encounter_id": "wipe-1"}, game_pid=55)
        )
        hook.request_count += 1
        hook.last_result = 7
        driver.poll(force=True)

        triggered = [
            event for event in self.events if event.get("event") == "triggered"
        ]
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0]["local_encounter_id"], "wipe-1")
        self.assertTrue(triggered[0]["reused_persistent_hook"])
        self.assertIs(driver.hook, hook)
        self.assertFalse(hook.closed)

    def test_process_change_closes_previous_hook(self):
        driver = self.driver()
        driver.sync(
            game_pid=66,
            mode="team",
            script_entity_override=6066,
        )
        first = FakeHook.instances[-1]
        driver.sync(
            game_pid=77,
            mode="team",
            script_entity_override=7077,
        )
        self.assertTrue(first.closed)
        self.assertEqual(len(FakeHook.instances), 2)
        self.assertEqual(driver.game_pid, 77)

    def test_equipment_request_temporarily_releases_team_hook(self):
        driver = self.driver()
        driver.sync(
            game_pid=66,
            mode="team",
            script_entity_override=6066,
        )
        first = FakeHook.instances[-1]

        self.assertTrue(driver.pause_for_external_request())
        self.assertTrue(first.closed)
        self.assertIsNone(driver.hook)
        driver.resume_after_external_request()
        driver.sync(
            game_pid=66,
            mode="team",
            script_entity_override=6066,
        )

        self.assertEqual(len(FakeHook.instances), 2)
        self.assertTrue(FakeHook.instances[-1].enabled)

    def test_equipment_request_waits_when_team_hook_restoration_fails(self):
        driver = self.driver()
        driver.sync(
            game_pid=66,
            mode="team",
            script_entity_override=6066,
        )
        hook = FakeHook.instances[-1]
        original_close = hook.close

        def fail_close():
            raise RuntimeError("entry restoration failed")

        hook.close = fail_close
        self.assertFalse(driver.pause_for_external_request())
        self.assertIs(driver.hook, hook)
        self.assertFalse(hook.enabled)
        self.assertEqual(
            self.events[-1]["failure"],
            "TEAM_HOOK_CLEANUP_FAILED",
        )

        hook.close = original_close
        self.assertTrue(driver.pause_for_external_request())
        self.assertIsNone(driver.hook)

    def test_receive_driver_retains_only_team_statistics(self):
        driver = HybridTeamStatsReceiveDriver(
            {"profile": "test"},
            self.events.append,
            hook_factory=FakeReceiveHook,
        )
        driver.sync(game_pid=88)
        hook = FakeReceiveHook.instances[-1]
        hook.pending.extend(
            [
                {"sequence": 0, "method": "OnMsgSyncCurrentHp"},
                {
                    "sequence": 1,
                    "method": "RetCommonCombatStatisticsByTeam",
                    "filetime_100ns": 123,
                    "script_entity": 880088,
                },
            ]
        )
        deadline = time.monotonic() + 1.0
        records = []
        while not records and time.monotonic() < deadline:
            time.sleep(0.01)
            records = driver.poll()
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["method"], "RetCommonCombatStatisticsByTeam"
        )
        self.assertEqual(records[0]["capture_source"], "memory_receive_hook")
        status = driver.status_snapshot()
        self.assertEqual(status["response_count"], 1)
        self.assertEqual(status["decoded_response_count"], 1)
        self.assertEqual(status["local_role_script_entity"], 880088)
        driver.close()
        self.assertTrue(hook.closed)

    def test_receive_driver_reinstalls_for_new_game_process(self):
        driver = HybridTeamStatsReceiveDriver(
            {"profile": "test"},
            self.events.append,
            hook_factory=FakeReceiveHook,
        )
        driver.sync(game_pid=91)
        first = FakeReceiveHook.instances[-1]
        driver.sync(game_pid=92)
        self.assertTrue(first.closed)
        self.assertEqual(len(FakeReceiveHook.instances), 2)
        driver.close()


if __name__ == "__main__":
    unittest.main()
