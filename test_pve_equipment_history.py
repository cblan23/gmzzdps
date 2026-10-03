"""PVE history gear is an immutable, UID-matched, captured snapshot."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest import mock

from test_combat_model import DpsWindow


class LabelProbe:
    def __init__(self):
        self.values = {}

    def configure(self, **values):
        self.values.update(values)


class PveEquipmentHistoryTests(unittest.TestCase):
    def window(self):
        window = object.__new__(DpsWindow)
        window.model = SimpleNamespace(actor_character_ids={1: "self-uid", 2: "teammate-uid"})
        window.team_equipment_profiles = {
            "self-uid": {"equipment_snapshot": {
                "captured_at_ns": 90_000_000_000,
                "equipment": [{"slot": 1, "item_id": 123}],
            }},
            "teammate-uid": {"equipment_snapshot": {
                "captured_at_ns": 95_000_000_000,
                "equipment": [{"slot": 1, "item_id": 456}],
            }},
            "late-uid": {"equipment_snapshot": {
                "captured_at_ns": 101_000_000_000,
                "equipment": [{"slot": 1, "item_id": 789}],
            }},
        }
        return window

    def test_saves_exact_uid_snapshots_without_retroactive_or_live_overwrite(self):
        window = self.window()
        record = {
            "ended_at_epoch": 100,
            "participants": [
                {"actor_id": 1, "is_self": True},
                {"actor_id": 2, "user_token": "teammate-uid"},
                {"actor_id": 3, "user_token": "late-uid"},
                {"actor_id": 4, "user_token": "other-uid"},
            ],
            "healers": [{"actor_id": 2, "user_token": "teammate-uid"}],
        }
        original = deepcopy(record)

        captured = window._attach_pve_equipment_snapshots(record)

        self.assertEqual(record, original)
        self.assertEqual(captured["participants"][0]["equipment_snapshot"]["equipment"][0]["item_id"], 123)
        self.assertEqual(captured["participants"][1]["equipment_snapshot"]["equipment"][0]["item_id"], 456)
        self.assertEqual(captured["healers"][0]["equipment_snapshot"]["equipment"][0]["item_id"], 456)
        self.assertEqual(captured["participants"][0]["user_token"], "self-uid")
        self.assertNotIn("equipment_snapshot", captured["participants"][2])
        self.assertNotIn("equipment_snapshot", captured["participants"][3])
        window.team_equipment_profiles["teammate-uid"]["equipment_snapshot"]["equipment"][0]["item_id"] = 999
        self.assertEqual(captured["participants"][1]["equipment_snapshot"]["equipment"][0]["item_id"], 456)
        self.assertIs(window._attach_pve_equipment_snapshots(captured), captured)

    def test_missing_end_time_never_uses_current_equipment_as_history(self):
        window = self.window()
        record = {"participants": [{"actor_id": 1}]}
        self.assertIs(window._attach_pve_equipment_snapshots(record), record)

    def test_delayed_settlement_uses_the_encounter_identity_snapshot(self):
        window = self.window()
        window.model.actor_character_ids = {}
        record = {
            "ended_at_epoch": 100,
            "participant_identities": [
                {"actor_id": 2, "character_id": "teammate-uid"}
            ],
            "participants": [{"actor_id": 2}],
        }

        captured = window._attach_pve_equipment_snapshots(record)

        self.assertEqual(
            captured["participants"][0]["equipment_snapshot"]["equipment"][0]["item_id"],
            456,
        )
        self.assertEqual(captured["participants"][0]["user_token"], "teammate-uid")

    def test_history_load_recovers_an_exact_archived_snapshot(self):
        window = self.window()
        window.team_equipment_profiles = {}
        archived = {
            "captured_at_ns": 95_000_000_000,
            "extraordinary_rating": 90_001,
            "equipment": [{"slot": 1, "item_id": 654}],
        }
        repository = mock.Mock()
        repository.equipment_snapshot_at_or_before.return_value = archived
        window.pvp_history_repository = repository
        window.pvp_recording = SimpleNamespace(account_key="account-a")
        record = {
            "encounter_id": "encounter-1",
            "ended_at_epoch": 100,
            "participant_identities": [
                {"actor_id": 2, "character_id": "teammate-uid"}
            ],
            "participants": [
                {
                    "actor_id": 2,
                    "extraordinary_rating": 90_001,
                }
            ],
        }
        window.history_selected_id = "encounter-1"
        window.history_loaded_record = None
        window.history_records = []
        window.history_store = mock.Mock()
        window.history_store.load.return_value = record
        window._restore_history_record = lambda value: value

        loaded = window._selected_history_record()

        self.assertEqual(
            loaded["participants"][0]["equipment_snapshot"]["equipment"][0]["item_id"],
            654,
        )
        repository.equipment_snapshot_at_or_before.assert_called_once_with(
            "account-a",
            "teammate-uid",
            100_000_000_000,
            extraordinary_rating=90_001,
        )
        window.history_store.save.assert_called_once_with(loaded)

    def test_delayed_settlement_accepts_same_rating_snapshot_before_statistics(self):
        window = self.window()
        window.team_equipment_profiles = {
            "self-uid": {
                "equipment_snapshot": {
                    "captured_at_ns": 112_000_000_000,
                    "extraordinary_rating": 90_001,
                    "equipment": [{"slot": 1, "item_id": 777}],
                }
            }
        }
        record = {
            "ended_at_epoch": 100,
            "settlement_received_at": 120,
            "self_character_id": "self-uid",
            "participants": [
                {
                    "actor_id": 1,
                    "is_self": True,
                    "extraordinary_rating": 90_001,
                }
            ],
        }

        captured = window._attach_pve_equipment_snapshots(record)

        self.assertEqual(
            captured["participants"][0]["equipment_snapshot"]["equipment"][0]["item_id"],
            777,
        )

    def test_delayed_settlement_rejects_post_battle_rating_mismatch(self):
        window = self.window()
        window.team_equipment_profiles = {
            "self-uid": {
                "equipment_snapshot": {
                    "captured_at_ns": 112_000_000_000,
                    "extraordinary_rating": 90_002,
                    "equipment": [{"slot": 1, "item_id": 888}],
                }
            }
        }
        record = {
            "ended_at_epoch": 100,
            "settlement_received_at": 120,
            "self_character_id": "self-uid",
            "participants": [
                {
                    "actor_id": 1,
                    "is_self": True,
                    "extraordinary_rating": 90_001,
                }
            ],
        }

        captured = window._attach_pve_equipment_snapshots(record)

        self.assertIs(captured, record)
        self.assertNotIn("equipment_snapshot", captured["participants"][0])

    def test_archive_lookup_uses_delayed_settlement_cutoff(self):
        window = self.window()
        window.team_equipment_profiles = {}
        archived = {
            "captured_at_ns": 112_000_000_000,
            "extraordinary_rating": 90_001,
            "equipment": [{"slot": 1, "item_id": 999}],
        }
        repository = mock.Mock()
        repository.equipment_snapshot_at_or_before.return_value = archived
        window.pvp_history_repository = repository
        window.pvp_recording = SimpleNamespace(account_key="account-a")
        record = {
            "ended_at_epoch": 100,
            "settlement_received_at": 120,
            "self_character_id": "self-uid",
            "participants": [
                {
                    "actor_id": 1,
                    "is_self": True,
                    "extraordinary_rating": 90_001,
                }
            ],
        }

        captured = window._attach_pve_equipment_snapshots(record)

        self.assertEqual(
            captured["participants"][0]["equipment_snapshot"]["equipment"][0]["item_id"],
            999,
        )
        repository.equipment_snapshot_at_or_before.assert_called_once_with(
            "account-a",
            "self-uid",
            120_000_000_000,
            extraordinary_rating=90_001,
        )

    def test_local_history_save_persists_capture_before_upload(self):
        window = self.window()
        window.worker = SimpleNamespace(diagnostic_snapshot=lambda: {})
        window.history_store = mock.Mock()
        window.settlement_history_adapter = None
        window.settlement_ui = None
        window._queue_automatic_victory_upload = mock.Mock()
        record = {"ended_at_epoch": 100, "participants": [{"actor_id": 1}]}

        self.assertTrue(window._save_combat_history_record(record))

        saved = window.history_store.save.call_args.args[0]
        self.assertEqual(saved["participants"][0]["equipment_snapshot"]["equipment"][0]["item_id"], 123)
        self.assertNotIn("equipment_snapshot", record["participants"][0])
        window._queue_automatic_victory_upload.assert_called_once_with(saved)

    def test_official_history_save_uses_the_same_capture_rule(self):
        window = self.window()
        record = {"ended_at_epoch": 100, "participants": [{"actor_id": 2, "user_token": "teammate-uid"}]}
        window.history_store = mock.Mock()
        window.history_store.load.return_value = record
        window._history_record_for_upload = mock.Mock(return_value=record)
        window._queue_automatic_victory_upload = mock.Mock()
        adapter = SimpleNamespace(sync=mock.Mock(return_value={"encounter-1"}))

        self.assertEqual(window._sync_settlement_history_for_upload(adapter, object()), {"encounter-1"})

        saved = window.history_store.save.call_args.args[0]
        self.assertEqual(saved["participants"][0]["equipment_snapshot"]["equipment"][0]["item_id"], 456)

    def test_detail_gear_tab_reuses_pvp_equipment_cards_without_changing_meter(self):
        window = self.window()
        window.history_meter_mode = "dps"
        window.history_page_mode = "detail"
        window.history_layout_version = 2
        window.history_selected_actor = 1
        window.history_detail_mode = "skills"
        window.history_detail_buttons = {
            name: LabelProbe() for name in ("skills", "targets", "equipment")
        }
        window.history_skill_section_title_label = LabelProbe()
        window.history_detail_hint_label = LabelProbe()
        window.history_skill_canvas = object()
        window._draw_history_skills = mock.Mock()
        window._selected_history_record = lambda: {"combat_mode": "pve", "participants": []}
        window._selected_history_damage_participant = lambda: ({}, None)

        window._set_history_detail_mode("equipment")

        self.assertEqual(window.history_detail_mode, "equipment")
        self.assertEqual(window.history_meter_mode, "dps")
        self.assertEqual(window.history_skill_section_title_label.values["text"], "装备快照")
        window._draw_history_skills.assert_called_once()
        window._set_history_detail_mode("skills")
        self.assertEqual(window.history_skill_section_title_label.values["text"], "技能伤害构成")

    def test_pve_equipment_view_selects_the_shared_pvp_card_renderer(self):
        window = self.window()
        window.history_selected_actor = 1
        window.history_detail_mode = "equipment"
        window.history_page_mode = "detail"
        window.history_skill_canvas = object()
        window._selected_history_record = lambda: {"combat_mode": "pve", "participants": []}
        window._draw_pve_history_equipment = mock.Mock()

        window._draw_history_skills()

        window._draw_pve_history_equipment.assert_called_once_with(
            window.history_skill_canvas, window._selected_history_record()
        )

    def test_equipment_selection_includes_healing_only_participant(self):
        window = self.window()
        window.history_selected_actor = 2
        record = {
            "participants": [],
            "healers": [
                {
                    "actor_id": 2,
                    "equipment_snapshot": {"equipment": [{"slot": 1}]},
                }
            ],
            "damage_taken": [],
        }

        selected = window._selected_history_equipment_participant(record)

        self.assertEqual(selected["actor_id"], 2)
        self.assertIn("equipment_snapshot", selected)


if __name__ == "__main__":
    unittest.main()
