"""Automatic decided-battle uploads, using mocks only (no server or game access)."""
import queue
import unittest
from types import SimpleNamespace
from unittest import mock

from test_combat_model import DpsWindow, EncounterUploadResult, ProfileUploadError


class AutomaticVictoryUploadTests(unittest.TestCase):
    def setUp(self):
        self.window = object.__new__(DpsWindow)
        self.window.root = SimpleNamespace(after=mock.Mock())
        self.window.closing = False
        self.window.combat_upload_states = {}
        self.window.history_upload_in_progress = set()
        self.window.automatic_upload_queued = set()
        self.window._start_automatic_history_upload = mock.Mock()
        self.record = {
            "encounter_id": "victory-1",
            "result": "defeated",
            "archive_reason": "target_defeated",
            "monster": {"template_id": 7109821, "name": "Boss"},
        }

    def test_victory_queues_one_named_upload(self):
        self.assertTrue(self.window._queue_automatic_victory_upload(self.record))
        self.assertFalse(self.window._queue_automatic_victory_upload(self.record))
        self.window.root.after.assert_called_once()
        delay, callback = self.window.root.after.call_args.args
        self.assertEqual(delay, 0)
        callback()
        self.window._start_automatic_history_upload.assert_called_once_with("victory-1")

    def test_disabled_setting_does_not_queue_automatic_upload(self):
        self.window.auto_upload_combat_records_enabled = False

        self.assertFalse(self.window._queue_automatic_victory_upload(self.record))

        self.window.root.after.assert_not_called()

    def test_queued_callback_honors_setting_disabled_before_it_runs(self):
        self.assertTrue(self.window._queue_automatic_victory_upload(self.record))
        callback = self.window.root.after.call_args.args[1]
        self.window.auto_upload_combat_records_enabled = False

        callback()

        self.window._start_automatic_history_upload.assert_not_called()

    def test_pending_equipment_response_is_attached_before_upload(self):
        waiting = {
            **self.record,
            "ended_at_epoch": 100,
            "participants": [
                {
                    "actor_id": 2,
                    "user_token": "teammate-uid",
                    "name": "Teammate",
                }
            ],
        }
        self.window.team_equipment_requested_at = {"teammate-uid": 1.0}
        self.window.team_equipment_profiles = {}
        self.window._history_record_for_upload = mock.Mock(return_value=waiting)
        self.window._schedule_team_equipment_profiles = mock.Mock(return_value=False)
        self.window._attach_pve_equipment_snapshots = mock.Mock(
            side_effect=lambda record, **_options: record
        )

        self.assertTrue(self.window._queue_automatic_victory_upload(waiting))
        first_callback = self.window.root.after.call_args.args[1]
        first_callback()

        self.window._start_automatic_history_upload.assert_not_called()
        self.window._schedule_team_equipment_profiles.assert_called_once_with(
            extra_attempt_tokens={"teammate-uid"}
        )
        self.assertEqual(self.window.root.after.call_count, 2)
        waiting["participants"][0]["equipment_snapshot"] = {
            "equipment": [{"slot": 1, "item_id": 123}]
        }
        self.window.team_equipment_requested_at.clear()
        retry_callback = self.window.root.after.call_args.args[1]
        retry_callback()

        self.window._start_automatic_history_upload.assert_called_once_with("victory-1")

    def test_projection_member_does_not_delay_upload(self):
        projection = {
            **self.record,
            "participants": [
                {
                    "actor_id": 2,
                    "user_token": "projection-uid",
                    "name": "Projection·投影",
                    "is_ai": False,
                }
            ],
        }
        self.window.team_equipment_requested_at = {"projection-uid": 1.0}

        self.assertTrue(self.window._queue_automatic_victory_upload(projection))
        self.window.root.after.call_args.args[1]()

        self.window._start_automatic_history_upload.assert_called_once_with("victory-1")

    def test_ai_member_does_not_delay_upload(self):
        ai_team = {
            **self.record,
            "participants": [
                {
                    "actor_id": 2,
                    "user_token": "ai-teammate",
                    "name": "人机队友",
                    "is_ai": True,
                }
            ],
        }
        self.window.team_equipment_requested_at = {"ai-teammate": 1.0}

        self.assertTrue(self.window._queue_automatic_victory_upload(ai_team))
        self.window.root.after.call_args.args[1]()

        self.window._start_automatic_history_upload.assert_called_once_with("victory-1")

    def test_wipe_uploads_but_training_dummies_never_upload(self):
        wipe = {
            **self.record,
            "encounter_id": "wipe-1",
            "result": "failed",
            "archive_reason": "party_wipe",
        }
        self.assertTrue(self.window._queue_automatic_victory_upload(wipe))
        for changes in (
            {"target_filter": "dummy"},
            {"monster": {"template_id": 7114223, "name": "伤害木桩"}},
        ):
            self.assertFalse(self.window._queue_automatic_victory_upload(
                {**self.record, **changes}
            ))
        self.window.root.after.assert_called_once()

    def test_pending_waits_for_official_settlement(self):
        pending = {**self.record, "settlement_status": "PENDING"}
        self.assertFalse(self.window._queue_automatic_victory_upload(pending))
        self.assertTrue(self.window._queue_automatic_victory_upload(
            {**pending, "settlement_status": "SETTLED"}
        ))

    def test_pending_wipe_uploads_without_official_settlement(self):
        wipe = {
            **self.record,
            "encounter_id": "wipe-pending-1",
            "result": "failed",
            "archive_reason": "party_wipe",
            "settlement_status": "PENDING",
        }

        self.assertTrue(self.window._queue_automatic_victory_upload(wipe))

    def test_party_wipe_reason_uploads_before_result_projection(self):
        wipe = {
            key: value for key, value in self.record.items() if key != "result"
        }
        wipe.update(
            encounter_id="wipe-local-1",
            archive_reason="party_wipe",
        )

        self.assertTrue(self.window._queue_automatic_victory_upload(wipe))

    def test_upload_states_prevent_duplicate_or_looping_requests(self):
        signature = self.window._history_detail_signature(self.record)
        for state in ("uploading", "uploaded", "included", "ranked", "failed"):
            self.window.combat_upload_states = {
                "victory-1": {
                    "state": state,
                    "detail_signature": signature,
                }
            }
            self.assertFalse(self.window._queue_automatic_victory_upload(self.record))
        self.window.root.after.assert_not_called()

    def test_uploaded_record_requeues_when_outcome_evidence_changes(self):
        self.window.combat_upload_states = {
            "victory-1": {
                "state": "uploaded",
                "detail_signature": self.window._history_detail_signature(self.record),
            }
        }
        corrected = {
            **self.record,
            "result": "failed",
            "archive_reason": "party_wipe",
        }

        self.assertTrue(self.window._queue_automatic_victory_upload(corrected))

    def test_recovered_failed_archive_is_saved_reset_and_queued(self):
        recovered = {
            **self.record,
            "encounter_id": "legacy-polluted",
            "self_character_id": "role-a",
            "result": "success",
            "completion_confirmed": True,
            "dungeon_stage_id": 5_150_095,
            "monster": {
                "template_id": 7_110_208,
                "name": "罗塞尔的污染意志",
                "current_hp": 0,
                "max_hp": 137_524_743,
            },
            "victory_recovery": {
                "source": "legacy_polluted_will_low_hp_space_transition"
            },
        }
        self.window.current_character_id = "role-a"
        self.window.combat_history_recovery_in_progress = True
        self.window.combat_history_recovery_character_id = ""
        self.window.history_store = mock.Mock()
        self.window._set_combat_upload_state = mock.Mock()
        self.window._queue_automatic_victory_upload = mock.Mock(return_value=True)
        self.window.combat_upload_states = {
            "legacy-polluted": {"state": "failed"}
        }

        self.window._handle_combat_history_recovery_result(
            {"character_id": "role-a", "records": [recovered], "error": ""}
        )

        self.window.history_store.save.assert_called_once_with(recovered)
        self.assertEqual(
            self.window._set_combat_upload_state.call_args.args,
            ("legacy-polluted", "pending"),
        )
        self.window._queue_automatic_victory_upload.assert_called_once_with(recovered)

    def test_recovery_result_from_previous_role_is_not_saved(self):
        self.window.current_character_id = "role-b"
        self.window.combat_history_recovery_in_progress = True
        self.window.combat_history_recovery_character_id = ""
        self.window.history_store = mock.Mock()
        self.window._schedule_legacy_combat_history_recovery = mock.Mock()

        self.window._handle_combat_history_recovery_result(
            {"character_id": "role-a", "records": [self.record], "error": ""}
        )

        self.window.history_store.save.assert_not_called()
        self.window._schedule_legacy_combat_history_recovery.assert_called_once_with()

    def test_legacy_recovery_scan_waits_for_auth_and_runs_off_ui_thread(self):
        self.window.current_character_id = "role-a"
        self.window.combat_history_recovery_in_progress = False
        self.window.combat_history_recovery_character_id = ""
        self.window.profile_resolved_character_id = ""
        self.window.licensing = SimpleNamespace(session=SimpleNamespace(active=False))
        self.window.history_store = mock.Mock()
        self.window.control_messages = queue.Queue()

        self.assertFalse(self.window._schedule_legacy_combat_history_recovery())
        self.window.licensing.session.active = True
        self.assertFalse(self.window._schedule_legacy_combat_history_recovery())
        self.window.profile_resolved_character_id = "role-a"
        globals_ = self.window._schedule_legacy_combat_history_recovery.__globals__
        thread_factory = mock.Mock()
        with mock.patch.object(globals_["threading"], "Thread", thread_factory):
            self.assertTrue(self.window._schedule_legacy_combat_history_recovery())

        thread_factory.return_value.start.assert_called_once()
        self.window.history_store.find_legacy_polluted_will_victories.assert_not_called()
        thread_factory.call_args.kwargs["target"]()
        self.window.history_store.find_legacy_polluted_will_victories.assert_called_once()
        self.window.history_store.find_recent_character_records.assert_called_once()
        kind, payload = self.window.control_messages.get_nowait()
        self.assertEqual(kind, "combat_history_recovery_result")
        self.assertEqual(payload["character_id"], "role-a")

    def test_recovery_result_queues_only_pending_recent_records_for_current_role(self):
        pending = {
            **self.record,
            "encounter_id": "pending-recent",
            "self_character_id": "role-a",
        }
        already_failed = {
            **pending,
            "encounter_id": "failed-recent",
        }
        wrong_role = {
            **pending,
            "encounter_id": "other-role",
            "self_character_id": "role-b",
        }
        self.window.current_character_id = "role-a"
        self.window.combat_history_recovery_in_progress = True
        self.window.combat_history_recovery_character_id = ""
        self.window.combat_upload_states = {
            "failed-recent": {"state": "failed"}
        }
        self.window._queue_automatic_victory_upload = mock.Mock(return_value=True)

        self.window._handle_combat_history_recovery_result(
            {
                "character_id": "role-a",
                "records": [],
                "upload_candidates": [pending, already_failed, wrong_role],
                "error": "",
            }
        )

        self.window._queue_automatic_victory_upload.assert_called_once_with(pending)

    def test_failed_local_save_never_uploads(self):
        self.window.worker = SimpleNamespace(diagnostic_snapshot=lambda: {})
        self.window.history_store = mock.Mock()
        self.window.history_store.save.side_effect = OSError("disk unavailable")
        self.assertFalse(self.window._save_combat_history_record(self.record))
        self.window.root.after.assert_not_called()

    def test_successful_save_schedules_upload_after_write(self):
        saved = []
        self.window.worker = SimpleNamespace(diagnostic_snapshot=lambda: {})
        self.window.history_store = SimpleNamespace(save=lambda record: saved.append(record))
        self.window.root.after.side_effect = lambda *_: self.assertEqual(len(saved), 1)
        self.assertTrue(self.window._save_combat_history_record(self.record))
        self.window.root.after.assert_called_once()

    def test_disabled_upload_still_saves_record_locally(self):
        saved = []
        self.window.worker = SimpleNamespace(diagnostic_snapshot=lambda: {})
        self.window.history_store = SimpleNamespace(save=lambda record: saved.append(record))
        self.window.auto_upload_combat_records_enabled = False

        self.assertTrue(self.window._save_combat_history_record(self.record))

        self.assertEqual(len(saved), 1)
        self.window.root.after.assert_not_called()

    def test_settlement_adapter_saved_records_are_observed(self):
        adapter = SimpleNamespace(sync=mock.Mock(return_value={"victory-1"}))
        self.window._history_record_for_upload = mock.Mock(return_value=self.record)
        tracker = object()
        self.assertEqual(self.window._sync_settlement_history_for_upload(adapter, tracker),
                         {"victory-1"})
        adapter.sync.assert_called_once_with(tracker)
        self.window.root.after.assert_called_once()

    def prepare_sender(self):
        del self.window._start_automatic_history_upload
        self.window._build_history_upload_payload = mock.Mock(return_value=(
            self.record,
            {"participants": [{"character_id": "AQAAAOwNKLYHAAAA", "is_uploader": True}]},
            "本人",
        ))
        self.window._set_combat_upload_state = mock.Mock()
        self.window.auto_upload_combat_records_enabled = True
        self.window.licensing = SimpleNamespace(upload_encounter=mock.Mock(
            return_value=EncounterUploadResult(True)
        ))
        self.window.control_messages = queue.Queue()

    def test_automatic_sender_runs_in_background_without_touching_dialog_or_privacy(self):
        self.prepare_sender()
        globals_ = self.window._start_automatic_history_upload.__globals__
        thread_factory = mock.Mock()
        with mock.patch.object(globals_["threading"], "Thread", thread_factory):
            self.window._start_automatic_history_upload("victory-1")
        thread_factory.return_value.start.assert_called_once()
        self.assertTrue(thread_factory.call_args.kwargs["daemon"])
        self.window.licensing.upload_encounter.assert_not_called()
        thread_factory.call_args.kwargs["target"]()
        self.window.licensing.upload_encounter.assert_called_once()
        kind, payload = self.window.control_messages.get_nowait()
        self.assertEqual(kind, "encounter_upload_result")
        self.window._handle_encounter_upload_result(payload)
        signature = self.window._history_detail_signature(self.record)
        self.window._set_combat_upload_state.assert_called_with(
            "victory-1", "uploaded", rank=None, encounter_id="", upload_id="",
            message="", upload_status="", statistics_status="", ranking_status="",
            validation_reasons=(), public_mode="character",
            detail_signature=signature,
        )

    def test_disabled_setting_blocks_sender_before_payload_build(self):
        self.prepare_sender()
        self.window.auto_upload_combat_records_enabled = False

        self.window._start_automatic_history_upload("victory-1")

        self.window._build_history_upload_payload.assert_not_called()
        self.window.licensing.upload_encounter.assert_not_called()

    def test_validation_failure_is_recorded_without_a_dialog(self):
        self.prepare_sender()
        self.window._build_history_upload_payload.side_effect = ProfileUploadError("BAD_ENCOUNTER")
        self.window._start_automatic_history_upload("victory-1")
        self.assertEqual(self.window._set_combat_upload_state.call_args.args,
                         ("victory-1", "failed"))
        self.window.licensing.upload_encounter.assert_not_called()

    def test_detail_arriving_during_upload_is_sent_once_after_receipt(self):
        self.prepare_sender()
        initial_signature = self.window._history_detail_signature(self.record)
        latest = {**self.record, "skill_cast_log": {"rows": [[2000, 2, 86021030, 1]]}}
        self.window._history_record_for_upload = mock.Mock(return_value=latest)
        def save_state(battle_id, state, **metadata):
            self.window.combat_upload_states[battle_id] = {"state": state, **metadata}
        self.window._set_combat_upload_state.side_effect = save_state
        self.window.history_upload_in_progress.add("victory-1")
        self.window.combat_upload_states["victory-1"] = {"state": "uploading"}
        self.assertFalse(self.window._queue_automatic_victory_upload(latest))
        self.window._handle_encounter_upload_result({
            "battle_id": "victory-1", "detail_signature": initial_signature,
            "result": EncounterUploadResult(True),
        })
        self.window.root.after.assert_called_once()
        self.window._handle_encounter_upload_result({
            "battle_id": "victory-1",
            "detail_signature": self.window._history_detail_signature(latest),
            "result": EncounterUploadResult(True),
        })
        self.window.root.after.assert_called_once()

    def test_rating_or_equipment_arriving_after_upload_is_sent_once(self):
        for collection, detail in (
            ("participants", {"extraordinary_rating": 123866}),
            ("healers", {"extraordinary_rating": 123866}),
            ("damage_taken", {"equipment_snapshot": {"captured_at_ns": 100, "equipment": [{"item_id": 456}]}}),
        ):
            with self.subTest(collection=collection, detail=detail):
                self.window.root.after.reset_mock()
                self.window.automatic_upload_queued.clear()
                initial = {**self.record, collection: [{"actor_id": 2}]}
                latest = {**initial, collection: [{"actor_id": 2, **detail}]}
                self.window.combat_upload_states["victory-1"] = {
                    "state": "uploaded",
                    "detail_signature": self.window._history_detail_signature(initial),
                }

                self.assertTrue(self.window._queue_automatic_victory_upload(latest))
                self.window.root.after.assert_called_once()
                self.window.combat_upload_states["victory-1"]["detail_signature"] = self.window._history_detail_signature(latest)
                self.assertFalse(self.window._automatic_upload_needs_update("victory-1", latest))

    def test_network_failure_is_recorded_without_popup(self):
        self.prepare_sender()
        self.window._handle_encounter_upload_result({
            "battle_id": "victory-1",
            "result": EncounterUploadResult(False, error="connection", message="离线"),
        })
        self.assertEqual(self.window._set_combat_upload_state.call_args.args,
                         ("victory-1", "failed"))



if __name__ == "__main__":
    unittest.main()
