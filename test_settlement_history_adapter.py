"""Passive settlements projected into the established combat-history store."""
import tempfile
import unittest
from pathlib import Path

from combat_history import CombatHistoryStore
from combat_statistics import normalize_statistics
from encounter_repository import EncounterRepository
from encounter_tracker import EncounterTracker
from network_state import is_ai_team_token
from settlement_history_adapter import SettlementHistoryAdapter, encounter_history_record
from settlement_ui_controller import SettlementUIController
from test_encounter_settlement import (
    NS,
    ROSTER,
    normalized,
    packet,
    projection_members,
    wipe,
)

START_EPOCH = 1_800_000_000


class SettlementHistoryAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = CombatHistoryStore(self.directory)
        self.adapter = SettlementHistoryAdapter(self.store)

    @staticmethod
    def wipe(tracker):
        return wipe(tracker, start=START_EPOCH)

    @staticmethod
    def settlement(**kwargs):
        return normalized(received=START_EPOCH + 100, **kwargs)

    def test_pending_record_keeps_unknown_metrics_null(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)

        self.assertEqual(self.adapter.sync(tracker), {encounter.local_encounter_id})
        record = self.store.load(encounter.local_encounter_id)

        self.assertEqual(record["settlement_status"], "PENDING")
        self.assertIsNone(record["total_damage"])
        self.assertIsNone(record["team_dps"])
        self.assertIsNone(record["duration_seconds"])
        self.assertTrue(
            all(
                row["damage"] is None
                and row["dps"] is None
                and row["share"] is None
                for row in record["participants"]
            )
        )
        self.assertEqual(len(record["healers"]), len(ROSTER))
        self.assertEqual(len(record["damage_taken"]), len(ROSTER))
        self.assertTrue(
            all(row["effective_healing"] is None for row in record["healers"])
        )
        self.assertTrue(
            all(row["taken"] is None for row in record["damage_taken"])
        )
        self.assertIsNone(record["team_effective_healing"])
        self.assertIsNone(record["team_taken"])

    def test_unbound_projection_slots_do_not_count_as_participants(self):
        tracker = EncounterTracker()
        roster = [
            {'id': 'self', 'iid': 100, 'name': 'Self', 'is_ai': False},
            {'id': 'peer', 'iid': 200, 'name': 'Peer', 'is_ai': False},
            *(
                {'id': f'projection-{index}', 'iid': None, 'name': None,
                 'is_ai': True}
                for index in range(4)
            ),
        ]
        first = tracker.begin(
            instance_id='instance', started_at_ns=(START_EPOCH + 100) * NS,
            participants_snapshot=roster, boss_template_id=7100401,
            boss_token='entity:first', self_token='self',
        )
        tracker.end('WIPE', (START_EPOCH + 190) * NS)
        pending = encounter_history_record(first)
        self.assertEqual(pending['team_size'], 2)
        self.assertEqual(len(pending['participants']), 2)
        self.assertEqual(len(pending['participants_snapshot']), 2)
        self.assertEqual(len(first.participants_snapshot), 6)

        second = tracker.begin(
            instance_id='instance', started_at_ns=(START_EPOCH + 200) * NS,
            participants_snapshot=[roster[0], *roster[2:]],
            boss_template_id=7100401, boss_token='entity:second',
            self_token='self',
        )
        tracker.end('RESET', (START_EPOCH + 210) * NS)
        abandoned = encounter_history_record(second)
        self.assertEqual(abandoned['team_size'], 1)
        self.assertEqual(len(abandoned['participants']), 1)
        self.assertEqual(len(abandoned['participants_snapshot']), 1)
        self.assertEqual(len(second.participants_snapshot), 5)

        stale = dict(abandoned)
        stale['participants'] = [
            *abandoned['participants'],
            *({'user_token': row['id'], 'actor_id': 0, 'is_ai': True,
               'damage': None} for row in roster[2:]),
        ]
        stale['team_size'] = 5
        self.store.save(stale)
        self.assertEqual(self.adapter.sync(tracker), {
            first.local_encounter_id, second.local_encounter_id,
        })
        repaired = self.store.load(second.local_encounter_id)
        self.assertEqual(repaired['team_size'], 1)
        self.assertEqual(len(repaired['participants']), 1)
        self.assertEqual(self.adapter.sync(tracker), set())

    def test_live_hud_samples_are_saved_with_passive_only_encounter(self):
        tracker = EncounterTracker()
        encounter = tracker.begin(
            instance_id="sampled-boss",
            started_at_ns=START_EPOCH * 1_000_000_000,
            participants_snapshot=ROSTER,
            boss_template_id=7_109_821,
            self_token="self",
        )
        self.assertTrue(self.adapter.sample_live_team_dps(
            tracker, START_EPOCH + 1, 100, boss_template_id=7_109_821
        ))
        self.assertTrue(self.adapter.sample_live_team_dps(
            tracker, START_EPOCH + 2, 250, boss_template_id=7_109_821
        ))
        self.assertTrue(self.adapter.sample_live_team_dps(
            tracker, START_EPOCH + 3, 175, boss_template_id=7_109_821
        ))
        tracker.end("VICTORY", (START_EPOCH + 4) * 1_000_000_000)
        self.assertEqual(self.adapter.sync(tracker), {encounter.local_encounter_id})
        pending = self.store.load(encounter.local_encounter_id)
        self.assertEqual(len(pending["display_team_dps_samples"]["rows"]), 3)
        self.assertNotIn("team_dps_timeline", pending)

        encounter.encounter_duration_seconds = 4.0
        encounter.revision += 1

        self.assertEqual(self.adapter.sync(tracker), {encounter.local_encounter_id})
        record = self.store.load(encounter.local_encounter_id)
        self.assertEqual(record["display_team_dps_samples"]["rows"], [
            [1, 100.0], [2, 250.0], [3, 175.0],
        ])
        self.assertEqual(
            [row["team_dps"] for row in record["team_dps_timeline"]],
            [100.0, 250.0, 175.0],
        )

    def test_pending_then_settled_overwrites_the_same_history_file(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        first_path = next(iter(self.adapter.sync(tracker)))

        tracker.accept(self.settlement())
        self.assertEqual(self.adapter.sync(tracker), {encounter.local_encounter_id})
        record = self.store.load(encounter.local_encounter_id)

        self.assertEqual(first_path, encounter.local_encounter_id)
        self.assertEqual(record["settlement_status"], "SETTLED")
        self.assertEqual(record["total_damage"], 200)
        self.assertEqual(record["server_battle_id"], "battle-1")
        self.assertTrue(
            all(row["settlement_hint"] == "延迟补齐" for row in record["participants"])
        )
        self.assertEqual(record["team_dps"], 20.0)
        self.assertTrue(all(row["dps"] == 10.0 for row in record["participants"]))
        self.assertEqual(
            [path.name for path in self.directory.glob("*.json")],
            [f"{encounter.local_encounter_id}.json"],
        )

    def test_late_settlement_keeps_the_original_teammate_equipment_snapshot(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        self.adapter.sync(tracker)
        record = self.store.load(encounter.local_encounter_id)
        teammate = next(row for row in record["participants"] if row["user_token"] == "peer")
        teammate["equipment_snapshot"] = {
            "captured_at_ns": START_EPOCH * NS,
            "equipment": [{"slot": 1, "item_id": 456}],
        }
        self.store.save(record)

        tracker.accept(self.settlement())
        self.adapter.sync(tracker)
        settled = self.store.load(encounter.local_encounter_id)
        teammate = next(row for row in settled["participants"] if row["user_token"] == "peer")
        self.assertEqual(teammate["equipment_snapshot"], {
            "captured_at_ns": START_EPOCH * NS,
            "equipment": [{"slot": 1, "item_id": 456}],
        })

    def test_settlement_keeps_only_exact_local_target_distributions(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        tracker.accept(self.settlement())
        base = {
            "participants": [
                {
                    "actor_id": 100,
                    "is_self": True,
                    "damage": 100,
                    "targets": [{"entity_id": 900, "name": "Boss", "damage": 100}],
                },
                {
                    "actor_id": 200,
                    "damage": 100,
                    "targets": [{"entity_id": 900, "name": "Boss", "damage": 100}],
                },
            ],
        }

        exact = encounter_history_record(encounter, base)
        rows = {row["actor_id"]: row for row in exact["participants"]}
        self.assertEqual(rows[100]["targets"][0]["damage"], 100)
        self.assertEqual(rows[200]["targets"][0]["damage"], 100)
        self.assertEqual(exact["map_id"], encounter.map_id)

        base["participants"][1]["targets"][0]["damage"] = 99
        mismatched = encounter_history_record(encounter, base)
        rows = {row["actor_id"]: row for row in mismatched["participants"]}
        self.assertEqual(rows[100]["targets"][0]["damage"], 100)
        self.assertEqual(rows[200]["targets"], [])

    def test_late_settlement_keeps_healer_only_equipment_snapshot(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        self.adapter.sync(tracker)
        record = self.store.load(encounter.local_encounter_id)
        record["healers"] = [{
            "actor_id": 22,
            "user_token": "peer",
            "equipment_snapshot": {
                "captured_at_ns": START_EPOCH * NS,
                "equipment": [{"slot": 2, "item_id": 789}],
            },
        }]
        for participant in record["participants"]:
            participant.pop("equipment_snapshot", None)
        self.store.save(record)

        tracker.accept(self.settlement())
        self.adapter.sync(tracker)
        settled = self.store.load(encounter.local_encounter_id)
        teammate = next(
            row for row in settled["participants"] if row["user_token"] == "peer"
        )
        self.assertEqual(
            teammate["equipment_snapshot"]["equipment"][0]["item_id"], 789
        )

    def test_projection_skill_details_are_saved_from_server_statistics(self):
        members = projection_members()
        roster = [
            {
                "id": token,
                "iid": values[1],
                "name": values[5],
                "profession_id": values.get(4),
                "is_ai": is_ai_team_token(token),
            }
            for token, values in members.items()
        ]
        tracker = EncounterTracker()
        encounter = tracker.begin(
            instance_id="instance",
            started_at_ns=START_EPOCH * NS,
            participants_snapshot=roster,
            stage_id=55,
            stage_index=1,
            boss_token="boss",
            dungeon_id=10,
            map_id=20,
            self_token="self",
        )
        tracker.end("VICTORY", (START_EPOCH + 10) * NS)
        raw = packet(success=True, members=members)
        raw["capture_timestamp_ns"] = (START_EPOCH + 100) * NS

        tracker.accept(normalize_statistics(
            raw, instance_id="instance", dungeon_id=10, map_id=20
        )[0])
        self.adapter.sync(tracker)
        record = self.store.load(encounter.local_encounter_id)
        projections = [
            row for row in record["participants"] if row.get("is_ai") is True
        ]

        self.assertEqual(len(projections), 4)
        self.assertTrue(all(row["skills"] for row in projections))
        self.assertTrue(all(
            sum(skill["damage"] or 0 for skill in row["skills"])
            == row["damage"]
            for row in projections
        ))

    def test_server_count_without_skill_damage_does_not_invent_zero(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        raw = packet()
        raw["capture_timestamp_ns"] = (START_EPOCH + 100) * NS
        raw["decoded_arguments"][0][5]["self"].pop(34)
        snapshot = normalize_statistics(
            raw, instance_id="instance", dungeon_id=10, map_id=20
        )[0]

        tracker.accept(snapshot)
        self.adapter.sync(tracker)
        record = self.store.load(encounter.local_encounter_id)
        member = next(row for row in record["participants"] if row["user_token"] == "self")
        skill = member["skills"][0]

        self.assertIsNone(member["classified_skill_damage"])
        self.assertIsNone(skill["damage"])
        self.assertIsNone(skill["share"])
        self.assertEqual(skill["server_skill_count"], 1)
        self.assertEqual(skill["count_semantics"], "server_skill_count")

    def test_direct_settlement_preserves_critical_and_penetration_counters(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        raw = packet()
        raw["capture_timestamp_ns"] = (START_EPOCH + 100) * NS
        self_member = raw["decoded_arguments"][0][5]["self"]
        self_member[14] = 2
        self_member[25] = 3
        self_member[27] = 5

        tracker.accept(normalize_statistics(
            raw, instance_id="instance", dungeon_id=10, map_id=20
        )[0])
        self.adapter.sync(tracker)
        record = self.store.load(encounter.local_encounter_id)
        member = next(
            row for row in record["participants"]
            if row["user_token"] == "self"
        )

        self.assertEqual(member["damage_hits"], 5)
        self.assertEqual(member["critical_hits"], 3)
        self.assertEqual(member["critical_rate"], 0.6)
        self.assertEqual(member["penetration_hits"], 3)
        self.assertEqual(member["penetration_rate"], 0.6)

        peer = next(
            row for row in record["participants"]
            if row["user_token"] == "peer"
        )
        self.assertIsNone(peer["damage_hits"])
        self.assertIsNone(peer["critical_rate"])
        self.assertIsNone(peer["penetration_rate"])

    def test_missing_unpenetrated_counter_means_all_hits_penetrated(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        raw = packet()
        raw["capture_timestamp_ns"] = (START_EPOCH + 100) * NS
        self_member = raw["decoded_arguments"][0][5]["self"]
        self_member.pop(14, None)
        self_member[27] = 5

        tracker.accept(normalize_statistics(
            raw, instance_id="instance", dungeon_id=10, map_id=20
        )[0])
        self.adapter.sync(tracker)
        record = self.store.load(encounter.local_encounter_id)
        member = next(
            row for row in record["participants"]
            if row["user_token"] == "self"
        )

        self.assertEqual(member["damage_hits"], 5)
        self.assertEqual(member["penetration_hits"], 5)
        self.assertEqual(member["penetration_rate"], 1.0)

    def test_partial_healing_and_taken_do_not_become_team_totals(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        raw = packet()
        raw["capture_timestamp_ns"] = (START_EPOCH + 100) * NS
        raw["decoded_arguments"][0][5]["self"][7] = 40
        raw["decoded_arguments"][0][5]["self"][17] = 30
        raw["decoded_arguments"][0][5]["peer"].pop(7, None)
        raw["decoded_arguments"][0][5]["peer"].pop(17, None)

        tracker.accept(normalize_statistics(
            raw, instance_id="instance", dungeon_id=10, map_id=20
        )[0])
        self.adapter.sync(tracker)
        record = self.store.load(encounter.local_encounter_id)

        self.assertIsNone(record["team_taken"])
        self.assertIsNone(record["team_effective_healing"])
        self.assertEqual(len(record["damage_taken"]), len(ROSTER))
        self.assertTrue(any(row["taken"] is None for row in record["damage_taken"]))
        self.assertTrue(
            any(row["effective_healing"] is None for row in record["healers"])
        )

    def test_explicit_all_zero_healing_and_taken_remain_zero(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        raw = packet()
        raw["capture_timestamp_ns"] = (START_EPOCH + 100) * NS
        for member in raw["decoded_arguments"][0][5].values():
            member[7] = 0
            member[17] = 0

        tracker.accept(normalize_statistics(
            raw, instance_id="instance", dungeon_id=10, map_id=20
        )[0])
        self.adapter.sync(tracker)
        record = self.store.load(encounter.local_encounter_id)

        self.assertEqual(record["team_taken"], 0)
        self.assertEqual(record["team_effective_healing"], 0)
        self.assertEqual(record["team_hps"], 0.0)
        self.assertEqual(record["healers"], [])
        self.assertTrue(all(row["taken"] == 0 for row in record["damage_taken"]))

    def test_deleted_projection_stays_deleted_after_restart(self):
        repository = EncounterRepository(self.directory)
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        repository.save(tracker)
        self.adapter.sync(tracker)

        controller = SettlementUIController(repository)
        self.assertEqual(
            controller.hide_history_records({encounter.local_encounter_id}),
            {encounter.local_encounter_id},
        )
        self.assertTrue(self.store.delete(encounter.local_encounter_id))

        restored = SettlementUIController(repository).tracker
        self.assertEqual(self.adapter.sync(restored), set())
        self.assertIsNone(self.store.load(encounter.local_encounter_id))

    def test_unique_local_archive_merges_into_projection_without_duplicate(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        self.adapter.sync(tracker)
        local = {
            "encounter_id": "legacy-local-id",
            "started_at_epoch": float(START_EPOCH),
            "ended_at_epoch": float(START_EPOCH + 10),
            "result": "wipe",
            "archive_reason": "party_wipe",
            "target_filter": "boss",
            "monster": {"template_id": 7102990, "name": "Boss"},
            "total_damage": 55,
            "participants": [
                {
                    "actor_id": 100,
                    "name": "Self",
                    "is_self": True,
                    "damage": 55,
                    "dps": 5.5,
                    "share": 1.0,
                    "skills": [],
                }
            ],
        }

        projected = self.adapter.project_local_record(tracker, local)
        self.assertIsNotNone(projected)
        self.store.save(projected)

        self.assertEqual(projected["encounter_id"], encounter.local_encounter_id)
        self.assertEqual(projected["local_observation_id"], "legacy-local-id")
        rows = {row["user_token"]: row for row in projected["participants"]}
        self.assertEqual(rows["self"]["damage"], 55)
        self.assertEqual(rows["self"]["dps"], 5.5)
        self.assertIsNone(rows["peer"]["damage"])
        self.assertIsNone(rows["peer"]["dps"])
        self.assertEqual(
            [path.name for path in self.directory.glob("*.json")],
            [f"{encounter.local_encounter_id}.json"],
        )

        encounter.revision += 1
        self.adapter.sync(tracker)
        saved = self.store.load(encounter.local_encounter_id)
        self.assertEqual(saved["participants"][0]["dps"], 5.5)
        self.assertIsNone(saved["duration_seconds"])
        self.assertIsNone(saved["team_dps"])

        tracker.accept(self.settlement())
        self.adapter.sync(tracker)
        saved = self.store.load(encounter.local_encounter_id)
        self.assertEqual(saved["settlement_status"], "SETTLED")
        self.assertEqual(saved["participants"][0]["damage"], 100)
        self.assertEqual(saved["participants"][0]["dps"], 10.0)

    def test_unsettled_self_dps_uses_local_clock_without_fabricating_team_data(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        base = {
            "dps_duration_seconds": 10,
            "participants": [{"actor_id": 100, "is_self": True, "damage": 55}],
        }
        for status in ("PENDING", "ABANDONED"):
            with self.subTest(status=status):
                encounter.settlement_status = status
                record = encounter_history_record(encounter, base)
                rows = {row["user_token"]: row for row in record["participants"]}
                self.assertEqual(rows["self"]["dps"], 5.5)
                self.assertIsNone(rows["peer"]["dps"])
                self.assertIsNone(record["team_dps"])
                self.assertIsNone(record["duration_seconds"])

        record = encounter_history_record(encounter, {**base, "dps_duration_seconds": None})
        self.assertIsNone(record["participants"][0]["dps"])

    def test_victory_projects_zero_boss_health_when_last_sample_was_positive(self):
        tracker = EncounterTracker()
        encounter = tracker.begin(
            instance_id="instance",
            started_at_ns=START_EPOCH * NS,
            participants_snapshot=ROSTER,
            boss_template_id=7_109_821,
            boss_token="entity:first",
            self_token="self",
        )
        tracker.end("VICTORY", (START_EPOCH + 10) * NS)
        encounter.boss_current_hp = 750
        encounter.boss_max_hp = 1_000

        record = encounter_history_record(encounter)

        self.assertEqual(record["monster"]["current_hp"], 0)
        self.assertEqual(record["monster"]["max_hp"], 1_000)

    def test_explicit_live_binding_forces_one_settings_record_id(self):
        tracker = EncounterTracker()
        encounter = self.wipe(tracker)
        self.adapter.sync(tracker)
        self.assertTrue(
            self.adapter.bind("live-model-000001", encounter.local_encounter_id)
        )
        self.assertFalse(
            self.adapter.bind("live-model-000001", "another-encounter")
        )
        local = {
            "encounter_id": "live-model-000001",
            "started_at_epoch": START_EPOCH + 500,
            "ended_at_epoch": START_EPOCH + 510,
            "result": "wipe",
            "target_filter": "boss",
            "participants": [],
        }

        projected = self.adapter.project_local_record(tracker, local)
        self.assertIsNotNone(projected)
        self.store.save(projected)
        self.assertEqual(projected["encounter_id"], encounter.local_encounter_id)
        self.assertEqual(
            [path.name for path in self.directory.glob("*.json")],
            [f"{encounter.local_encounter_id}.json"],
        )

    def test_mismatched_bound_boss_does_not_inherit_entity_health_or_targets(self):
        """FB5B416D94E34350F9 must not mix Boss 4 and Boss 6 metadata."""

        tracker = EncounterTracker()
        encounter = tracker.begin(
            instance_id="instance",
            started_at_ns=START_EPOCH * NS,
            participants_snapshot=ROSTER,
            dungeon_id=10,
            map_id=20,
            stage_id=54,
            stage_index=0,
            boss_template_id=7100215,
            boss_token="descendant-token",
            self_token="self",
        )
        encounter.boss_name = "Descendant Guardian"
        tracker.end("WIPE", (START_EPOCH + 10) * NS)
        local = {
            "encounter_id": "wrong-local-record",
            "started_at_epoch": START_EPOCH,
            "ended_at_epoch": START_EPOCH + 10,
            "monster": {
                "template_id": 7102991,
                "name": "Viscountess myth form",
                "entity_id": 999,
                "current_hp": 1,
                "max_hp": 100,
            },
            "targets": [{"template_id": 7102991, "entity_id": 999}],
            "participants": [{
                "actor_id": 100,
                "name": "Self",
                "is_self": True,
                "damage": 999,
                "targets": [{"entity_id": 999, "damage": 999}],
            }],
        }
        self.assertTrue(
            self.adapter.bind(local["encounter_id"], encounter.local_encounter_id)
        )

        projected = self.adapter.project_local_record(tracker, local)

        self.assertEqual(projected["monster"]["template_id"], 7100215)
        self.assertEqual(projected["monster"]["name"], "Descendant Guardian")
        self.assertNotIn("entity_id", projected["monster"])
        self.assertNotIn("current_hp", projected["monster"])
        self.assertNotIn("max_hp", projected["monster"])
        self.assertNotIn("targets", projected)
        self.assertTrue(
            all(row["damage"] is None for row in projected["participants"])
        )


if __name__ == "__main__":
    unittest.main()
