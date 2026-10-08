"""Automatic decided-battle uploads, using mocks only (no server or game access)."""
import queue
import unittest
from types import SimpleNamespace
from unittest import mock

from test_combat_model import DpsWindow, EncounterUploadResult, ProfileUploadError
from test_settlement_official_ui import CanvasProbe, FontProbe


class AutomaticVictoryUploadTests(unittest.TestCase):
    def setUp(self):
        self.window = object.__new__(DpsWindow)
        self.window.root = SimpleNamespace(after=mock.Mock())
        self.window.closing = False
        self.window.combat_upload_states = {}
        self.window.history_upload_in_progress = set()
        self.window.automatic_upload_queued = set()
        self.window._start_history_upload = mock.Mock()
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
        self.window._start_history_upload.assert_called_once_with(
            "victory-1", "character", silent=True
        )

    def test_disabled_setting_does_not_queue_automatic_upload(self):
        self.window.auto_upload_combat_records_enabled = False

        self.assertFalse(self.window._queue_automatic_victory_upload(self.record))

        self.window.root.after.assert_not_called()

    def test_queued_callback_honors_setting_disabled_before_it_runs(self):
        self.assertTrue(self.window._queue_automatic_victory_upload(self.record))
        callback = self.window.root.after.call_args.args[1]
        self.window.auto_upload_combat_records_enabled = False

        callback()

        self.window._start_history_upload.assert_not_called()

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

        self.window._start_history_upload.assert_not_called()
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

        self.window._start_history_upload.assert_called_once_with(
            "victory-1", "character", silent=True
        )

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

        self.window._start_history_upload.assert_called_once_with(
            "victory-1", "character", silent=True
        )

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

    def test_upload_states_prevent_duplicate_or_looping_requests(self):
        for state in ("uploading", "uploaded", "included", "ranked", "failed"):
            self.window.combat_upload_states = {"victory-1": {"state": state}}
            self.assertFalse(self.window._queue_automatic_victory_upload(self.record))
        self.window.root.after.assert_not_called()

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
        del self.window._start_history_upload
        self.window._build_history_upload_payload = mock.Mock(return_value=(
            self.record,
            {"participants": [{"character_id": "AQAAAOwNKLYHAAAA", "is_uploader": True}]},
            "本人",
        ))
        self.window._set_combat_upload_state = mock.Mock()
        self.window._show_notice = mock.Mock()
        self.window._close_history_modal = mock.Mock()
        self.window.history_window = None
        self.window.upload_public_mode = "character"
        self.window.config = {"upload_public_mode": "character"}
        self.window.licensing = SimpleNamespace(upload_encounter=mock.Mock(
            return_value=EncounterUploadResult(True)
        ))
        self.window.control_messages = queue.Queue()

    def test_automatic_sender_runs_in_background_without_touching_dialog_or_privacy(self):
        self.prepare_sender()
        globals_ = self.window._start_history_upload.__globals__
        thread_factory = mock.Mock()
        with mock.patch.object(globals_["threading"], "Thread", thread_factory):
            self.window._start_history_upload("victory-1", "character", silent=True)
        thread_factory.return_value.start.assert_called_once()
        self.assertTrue(thread_factory.call_args.kwargs["daemon"])
        self.window.licensing.upload_encounter.assert_not_called()
        thread_factory.call_args.kwargs["target"]()
        self.window.licensing.upload_encounter.assert_called_once()
        kind, payload = self.window.control_messages.get_nowait()
        self.assertEqual(kind, "encounter_upload_result")
        self.assertTrue(payload["silent"])
        self.window._handle_encounter_upload_result(payload)
        self.window._set_combat_upload_state.assert_called_with(
            "victory-1", "uploaded", rank=None, encounter_id="", upload_id="",
            message="", upload_status="", statistics_status="", ranking_status="",
            validation_reasons=(), public_mode="character",
            detail_signature='',
        )
        self.window._close_history_modal.assert_not_called()
        self.window._show_notice.assert_not_called()
        self.assertEqual(self.window.upload_public_mode, "character")
        self.assertEqual(self.window.config, {"upload_public_mode": "character"})

    def test_disabled_setting_blocks_silent_sender_before_payload_build(self):
        self.prepare_sender()
        self.window.auto_upload_combat_records_enabled = False

        self.window._start_history_upload("victory-1", "character", silent=True)

        self.window._build_history_upload_payload.assert_not_called()
        self.window.licensing.upload_encounter.assert_not_called()

    def test_silent_validation_failure_does_not_open_dialog(self):
        self.prepare_sender()
        self.window._build_history_upload_payload.side_effect = ProfileUploadError("BAD_ENCOUNTER")
        self.window._start_history_upload("victory-1", "character", silent=True)
        self.assertEqual(self.window._set_combat_upload_state.call_args.args,
                         ("victory-1", "failed"))
        self.window._close_history_modal.assert_not_called()
        self.window._show_notice.assert_not_called()
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
            "battle_id": "victory-1", "silent": True, "detail_signature": initial_signature,
            "result": EncounterUploadResult(True),
        })
        self.window.root.after.assert_called_once()
        self.window._handle_encounter_upload_result({
            "battle_id": "victory-1", "silent": True,
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

    def test_silent_network_failure_is_recorded_without_popup(self):
        self.prepare_sender()
        self.window._handle_encounter_upload_result({
            "battle_id": "victory-1", "silent": True,
            "result": EncounterUploadResult(False, error="connection", message="离线"),
        })
        self.assertEqual(self.window._set_combat_upload_state.call_args.args,
                         ("victory-1", "failed"))
        self.window._show_notice.assert_not_called()

    def test_history_renders_no_upload_button(self):
        self.window.icons = SimpleNamespace(toolbar=lambda *_: object())
        self.window._ui_font = lambda *_: FontProbe()
        tags = self.window._draw_history_inline_actions(
            CanvasProbe(), 0, 20, 0, False, "victory-1", self.record
        )
        self.assertEqual(set(tags), {"report", "favorite", "delete"})


if __name__ == "__main__":
    unittest.main()
