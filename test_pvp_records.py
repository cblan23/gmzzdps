"""Automatic PVP map-boundary recording regressions."""

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from copy import deepcopy
import queue
import json
import sqlite3
import time
from unittest.mock import Mock

from pvp_records import (
    build_equipment_snapshot_upload,
    PvpHistoryRepository,
    PvpRecordingController,
    RECORDABLE_MAPS,
    CUMULATIVE_PVP_MAPS,
    PvpUploadWorker,
    TEAM_EQUIPMENT_UPLOAD_GRACE_SECONDS,
    project_duel_result_counters,
)
from pvp_tracker import _team_pvp_settlement
from npcap_parser_adapter import NpcapParserAdapter
from test_pvp_tracker import TEAM_SETTLEMENT_ARGS


BASE_NS = 1_789_700_000_000_000_000
SELF_ID = 57_407_071_770_816
SELF_TOKEN = "AQAAAOwNkGB8AAAA"


class PvpRecordingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = PvpHistoryRepository(
            Path(self.temporary.name) / "pvp.sqlite3"
        )
        self.controller = PvpRecordingController(self.repository)
        self.controller.bind_account("account-a")
        self.controller.ingest_update(
            "identity",
            {
                "self_id": SELF_ID,
                "user_token": SELF_TOKEN,
                "self_confirmed": True,
            },
        )
        self.controller.ingest_update(
            "profile",
            {
                "entity_id": SELF_ID,
                "user_token": SELF_TOKEN,
                "name": "本人",
                "profession_id": 1_200_001,
                "entity_type": "Player",
            },
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def scene(scene_id, seconds=0, **extra):
        return {
            "scene_id": scene_id,
            "capture_timestamp_ns": BASE_NS + seconds * 1_000_000_000,
            **extra,
        }

    def test_arena_entry_does_not_seed_pre_entry_party_into_live_roster(self):
        old_token = "AQAAAOldDungeonPeer"
        self.controller.ingest_update("party", {
            "in_team": True, "party_session_id": 6,
            "user_tokens": [SELF_TOKEN, old_token],
        })
        self.controller.ingest_scene(self.scene(5_203_003, 1))

        self.assertTrue(self.controller.active)
        self.assertNotIn(old_token, self.controller.display_tracker.pvp_allies)
        self.assertIn(old_token, self.controller.recording["ally_tokens"])

    def test_each_exported_formal_pvp_map_starts_automatically(self):
        self.assertEqual(len(RECORDABLE_MAPS), 19)
        self.assertNotIn(5200024, RECORDABLE_MAPS)
        for index, map_id in enumerate(RECORDABLE_MAPS):
            with self.subTest(map_id=map_id):
                controller = PvpRecordingController(self.repository)
                controller.bind_account(f"account-{index}")
                controller.ingest_update(
                    "identity",
                    {
                        "self_id": SELF_ID,
                        "user_token": f"{SELF_TOKEN}-{index}",
                        "self_confirmed": True,
                    },
                )
                controller.ingest_scene(self.scene(map_id, index + 1))
                self.assertTrue(controller.active)
                self.assertEqual(controller.recording["map_id"], map_id)
                self.assertEqual(
                    controller.recording["started_at_ns"],
                    BASE_NS + (index + 1) * 1_000_000_000,
                )

    def test_non_pvp_map_never_creates_a_match(self):
        self.controller.ingest_scene(self.scene(5_200_002, 1))
        self.assertFalse(self.controller.active)
        self.assertEqual(self.repository.list("account-a"), [])

    def test_enter_last_hunt_clears_retained_duel_even_before_role_token_is_known(self):
        self.begin_duel(1)
        self.controller.observe(self.observation(3, damage=300, map_id=5_200_002, instance_id='outdoor'))
        self.duel_rpc(4, 'OnMsgIndividualPVPResult', [1])
        self.controller.poll(BASE_NS + 15_000_000_000)
        self.controller.current.self_token = ''
        self.controller.ingest_scene(self.scene(5200167, 20))
        view = self.controller.snapshot(BASE_NS + 20_000_000_000)
        self.assertEqual(view['damage'], 0)
        self.assertEqual(view['outgoing'], [])
        self.assertEqual(view['map_name'], '终末猎杀')
        self.assertFalse(self.controller.active)

    def test_lost_duel_without_entity_dead_saves_fixed_death_counter_and_timeline(self):
        self.begin_duel(1)
        self.duel_rpc(4, 'OnMsgBeatenSyncV2', [SELF_ID + 10, SELF_ID, 860211000, 120, 120])
        self.duel_rpc(5, 'OnMsgIndividualPVPResult', [2])
        payload = self.controller.poll(BASE_NS + 16_000_000_000)
        self.assertEqual((payload['result'], payload['deaths'], payload['taken']), ('失败', 1, 120))
        self.assertEqual(payload['opponents'][0]['deaths_to'], 1)
        self.assertEqual(payload['opponents'][0]['timeline'][0]['type'], 'death')
        self.assertEqual(
            self.controller.snapshot(BASE_NS + 16_000_000_000)['incoming'][0]['defeats'],
            1,
        )

    def test_legacy_duel_result_projects_fixed_counters_for_ui_and_upload(self):
        legacy = {
            'schema_version': 1,
            'match_id': 'pvp_legacy_duel',
            'battle_id': 'pvp_legacy_duel',
            'match_kind': 'duel',
            'team_size': 1,
            'map_id': 0,
            'mode_name': '双人切磋',
            'result': '失败',
            'started_at_ns': BASE_NS,
            'ended_at_ns': BASE_NS + 30_000_000_000,
            'player': {'character_id': SELF_TOKEN, 'name': '本人'},
            'kills': 0,
            'assists': 0,
            'deaths': 0,
            'allies': [{
                'character_id': SELF_TOKEN,
                'name': '本人',
                'is_self': True,
                'kills': 0,
                'assists': 0,
                'deaths': 0,
            }],
            'enemies': [{
                'character_id': 'enemy-token',
                'name': '对手',
                'kills': 0,
                'assists': 0,
                'deaths': 0,
            }],
            'opponents': [{
                'opponent_id': 'enemy-token',
                'character_id': 'enemy-token',
                'name': '对手',
                'kills': 0,
                'assists': 0,
                'defeats': 0,
                'deaths': 0,
                'kills_on': 0,
                'deaths_to': 0,
            }],
        }
        untouched = deepcopy(legacy)
        projected = project_duel_result_counters(legacy)
        self.assertEqual(legacy, untouched)
        self.assertEqual(
            (projected['kills'], projected['assists'], projected['deaths']),
            (0, 0, 1),
        )
        self.assertEqual(
            (
                projected['allies'][0]['kills'],
                projected['allies'][0]['deaths'],
                projected['enemies'][0]['kills'],
                projected['enemies'][0]['deaths'],
            ),
            (0, 1, 1, 0),
        )
        self.assertEqual(
            (
                projected['opponents'][0]['kills_on'],
                projected['opponents'][0]['deaths_to'],
            ),
            (0, 1),
        )

        self.repository.save('account-a', legacy)
        self.assertEqual(self.repository.list('account-a')[0]['deaths'], 1)
        with self.repository.session() as connection:
            raw = json.loads(connection.execute(
                'SELECT payload_json FROM pvp_local_records WHERE match_id=?',
                (legacy['match_id'],),
            ).fetchone()[0])
        self.assertEqual(raw['deaths'], 0)

        uploaded = []
        def request(_action, body):
            uploaded.append(body['record'])
            return {'ok': True, 'match_id': legacy['match_id']}
        worker = PvpUploadWorker(
            self.repository,
            request,
            lambda: 'account-a',
        )
        worker.bind_request('account-a', request)
        self.assertTrue(worker.attempt_one(0))
        self.assertEqual(uploaded[0]['deaths'], 1)
        self.assertEqual(uploaded[0]['enemies'][0]['kills'], 1)

    def test_recorded_last_hunt_damage_and_life_events_reach_formal_hud(self):
        from tools.replay_pvp_capture import replay
        path = Path('logs/network_20260918_194650.jsonl')
        if not path.is_file():
            self.skipTest('local last-hunt capture evidence is unavailable')
        # This exact C7.log line proves the omitted historical map. The
        # capture itself used the old decoder, which dropped this class's
        # numeric properties; production now decodes indices 17/18/21/24/44.
        space_log = Path(self.temporary.name) / 'recorded-space-evidence.log'
        space_log.write_text(
            '[2026.09.18-19.52.04:915][527]LuaLog: ReleaseLog: '
            '[Entity(PVPLastHuntSpace, aq0ZR5dVgmeSfQSK)] [Space-LifeTimeStage] '
            'Space ctor entityId : aq0ZR5dVgmeSfQSK, TemplateID:5200167\n',
            encoding='utf-8',
        )
        report = replay(path, SELF_TOKEN, '2026-09-18T19:56:00',
                        window_totals=True, space_log=space_log)
        self.assertEqual(report['parser_errors'], 0)
        self.assertEqual(report['historical_spaces_recovered'], 1)
        view = report['pvp']
        self.assertEqual(view['map_name'], '终末猎杀')
        self.assertEqual((view['damage'], view['kills'], view['deaths']), (39_212, 1, 1))
        self.assertEqual({row['actor_id'] for row in view['outgoing']},
                         {57461295768177, 57394723753427, 57428546624383})
        self.assertEqual(view['incoming'][0]['actor_id'], 57461295768177)
        self.assertEqual(view['incoming'][0]['defeats'], 1)
        # The preceding duel has incoming amount 701, but no incoming amount
        # was sent for this map: never carry that 701 into the new match.
        self.assertEqual(view['total_taken'], '--')
        self.assertEqual(report['single_matches'][0]['damage_taken'], 701)

    def test_leaving_pvp_map_finalizes_at_exit_boundary(self):
        map_id = next(iter(RECORDABLE_MAPS))
        self.controller.ingest_scene(self.scene(map_id, 1))
        finalized = self.controller.ingest_scene(
            self.scene(0, 16, transition=True)
        )
        self.assertFalse(self.controller.active)
        self.assertEqual(finalized["map_id"], map_id)
        self.assertEqual(finalized["end_reason"], "left_map")
        self.assertEqual(finalized["duration_seconds"], 15)
        self.assertEqual(finalized["result"], "未知")
        self.assertEqual(
            self.repository.list("account-a")[0]["match_id"],
            finalized["match_id"],
        )

    def test_hunter_city_exit_immediately_queues_upload_and_reentry_is_new_match(self):
        for map_id in (5200131, 5200167):
            with self.subTest(map_id=map_id):
                account = f"hunter-{map_id}"
                controller = PvpRecordingController(self.repository)
                controller.bind_account(account)
                controller.ingest_update("identity", {
                    "self_id": SELF_ID, "user_token": SELF_TOKEN,
                    "self_confirmed": True,
                })
                controller.ingest_scene(self.scene(map_id, 1))
                first_id = controller.recording["match_id"]
                controller.observe(self.observation(2, damage=100, map_id=map_id))
                finalized = controller.ingest_scene(self.scene(0, 5, transition=True))
                self.assertEqual(finalized["match_id"], first_id)
                self.assertEqual(finalized["end_reason"], "left_map")
                self.assertEqual(finalized["damage"], 100)
                self.assertIsNone(controller.suspended_recording)
                self.assertEqual(
                    self.repository.due(
                        account, time.time() + TEAM_EQUIPMENT_UPLOAD_GRACE_SECONDS + 1
                    )["match_id"], first_id,
                )
                controller.ingest_scene(self.scene(map_id, 8))
                self.assertNotEqual(controller.recording["match_id"], first_id)

    def test_hunter_same_map_space_recreation_keeps_live_totals(self):
        self.controller.observe(self.observation(
            1, damage=1200, map_id=5200167, instance_id="space-a"))
        first_id = self.controller.recording["match_id"]
        self.controller.observe(self.observation(
            3, damage=800, map_id=5200167, instance_id="space-b"))

        self.assertEqual(self.controller.recording["match_id"], first_id)
        self.assertEqual(self.controller.snapshot(BASE_NS + 4_000_000_000)["damage"], 2000)
        self.assertEqual(self.repository.list("account-a"), [])
        finalized = self.controller.ingest_scene(self.scene(0, 6, transition=True))
        self.assertEqual((finalized["match_id"], finalized["damage"]),
                         (first_id, 2000))

    def test_hud_snapshot_reuses_unchanged_second_without_skipping_generation(self):
        self.controller.ingest_scene(self.scene(5200167, 1))
        tracker = self.controller.display_tracker
        original = tracker.session_snapshot
        tracker.session_snapshot = Mock(wraps=original)

        self.controller.snapshot(BASE_NS + 2_000_000_000)
        self.controller.snapshot(BASE_NS + 2_400_000_000)
        self.assertEqual(tracker.session_snapshot.call_count, 1)

        self.controller.observe(self.observation(3, damage=25, map_id=5200167))
        self.controller.snapshot(BASE_NS + 2_500_000_000)
        self.assertEqual(tracker.session_snapshot.call_count, 2)

    def test_direct_pvp_to_pvp_switch_splits_two_matches(self):
        first, second = 5200020, 5208002
        self.controller.ingest_scene(self.scene(first, 1))
        first_id = self.controller.recording["match_id"]
        finalized = self.controller.ingest_scene(self.scene(second, 11))
        self.assertEqual(finalized["match_id"], first_id)
        self.assertEqual(finalized["duration_seconds"], 10)
        self.assertTrue(self.controller.active)
        self.assertEqual(self.controller.recording["map_id"], second)
        self.assertEqual(
            self.controller.recording["started_at_ns"], BASE_NS + 11_000_000_000
        )
        self.controller.ingest_scene(self.scene(0, 21, transition=True))
        records = self.repository.list("account-a")
        self.assertEqual(len(records), 2)
        self.assertEqual({row["map_id"] for row in records}, {first, second})

    def test_duplicate_scene_refresh_does_not_split_same_map(self):
        map_id = next(iter(RECORDABLE_MAPS))
        self.controller.ingest_scene(self.scene(map_id, 1))
        match_id = self.controller.recording["match_id"]
        self.assertIsNone(
            self.controller.ingest_scene(
                self.scene(map_id, 3, force_reset=True, previous_scene_id=map_id)
            )
        )
        self.assertEqual(self.controller.recording["match_id"], match_id)
        self.assertEqual(self.repository.list("account-a"), [])

    def test_identity_arriving_after_entry_uses_entry_as_start(self):
        controller = PvpRecordingController(self.repository)
        controller.bind_account("late-identity")
        map_id = next(iter(RECORDABLE_MAPS))
        controller.ingest_scene(self.scene(map_id, 4))
        self.assertFalse(controller.active)
        controller.ingest_update(
            "identity",
            {
                "self_id": SELF_ID,
                "user_token": SELF_TOKEN,
                "self_confirmed": True,
            },
        )
        self.assertTrue(controller.active)
        self.assertEqual(
            controller.recording["started_at_ns"], BASE_NS + 4_000_000_000
        )

    def test_character_switch_and_application_exit_save_unknown_result(self):
        map_id = next(iter(RECORDABLE_MAPS))
        self.controller.ingest_scene(self.scene(map_id, 1))
        self.controller.ingest_update(
            "identity",
            {
                "self_id": SELF_ID + 1,
                "user_token": "new-character-token",
                "self_confirmed": True,
            },
        )
        record = self.repository.list("account-a")[0]
        self.assertEqual(record["end_reason"], "character_changed")
        self.assertEqual(record["result"], "未知")
        self.assertFalse(self.controller.active)

        self.controller.ingest_scene(self.scene(map_id, 20))
        self.controller.reset_context(
            BASE_NS + 30_000_000_000,
            reason="application_exit",
        )
        records = self.repository.list("account-a")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["end_reason"], "application_exit")

    def test_delayed_old_scene_boundary_cannot_close_new_map(self):
        first, second = list(RECORDABLE_MAPS)[:2]
        self.controller.ingest_scene(self.scene(first, 1))
        self.controller.ingest_scene(self.scene(second, 10))
        second_id = self.controller.recording["match_id"]
        finalized = self.controller.ingest_scene(
            self.scene(0, 9, transition=True)
        )
        self.assertIsNone(finalized)
        self.assertTrue(self.controller.active)
        self.assertEqual(self.controller.recording["match_id"], second_id)

    def test_new_instance_of_same_map_gets_a_fresh_start_time(self):
        map_id = next(iter(RECORDABLE_MAPS))
        self.controller.observe(
            {
                "timestamp_ns": BASE_NS + 1_000_000_000,
                "record": {"method": "NpcapEntityCreated"},
                "context": {
                    "self_id": SELF_ID,
                    "self_token": SELF_TOKEN,
                    "map_id": map_id,
                    "instance_id": "instance-a",
                },
            }
        )
        first_id = self.controller.recording["match_id"]
        finalized = self.controller.observe(
            {
                "timestamp_ns": BASE_NS + 12_000_000_000,
                "record": {"method": "NpcapEntityCreated"},
                "context": {
                    "self_id": SELF_ID,
                    "self_token": SELF_TOKEN,
                    "map_id": map_id,
                    "instance_id": "instance-b",
                },
            }
        )
        self.assertEqual(finalized["match_id"], first_id)
        self.assertEqual(
            self.controller.recording["started_at_ns"],
            BASE_NS + 12_000_000_000,
        )

    def test_scene_started_match_adopts_first_instance_without_false_split(self):
        map_id = next(iter(RECORDABLE_MAPS))
        self.controller.ingest_scene(self.scene(map_id, 1))
        match_id = self.controller.recording["match_id"]
        finalized = self.controller.observe({
            "timestamp_ns": BASE_NS + 2_000_000_000,
            "record": {"method": "NpcapEntityCreated"},
            "context": {"self_id": SELF_ID, "self_token": SELF_TOKEN,
                        "map_id": map_id, "instance_id": "first-confirmed-instance"},
        })
        self.assertIsNone(finalized)
        self.assertEqual(self.controller.recording["match_id"], match_id)
        self.assertEqual(self.controller.recording["instance_id"], "first-confirmed-instance")
        self.assertEqual(self.repository.list("account-a"), [])

    def test_desktop_scene_dispatch_drives_the_same_automatic_boundary(self):
        from test_combat_model import DpsWindow

        window = object.__new__(DpsWindow)
        window.pvp_recording = self.controller
        window.pvp_tracker = self.controller.current
        window.main_combat_mode = "pve"
        window.model = SimpleNamespace(
            encounter_id="",
            long_gap_phase_suspended=True,
            ingest_scene=lambda _value: False,
        )
        map_id = next(iter(RECORDABLE_MAPS))

        window._dispatch_message("scene", self.scene(map_id, 2))
        self.assertTrue(self.controller.active)
        match_id = self.controller.recording["match_id"]

        window._dispatch_message(
            "scene", self.scene(0, 9, transition=True)
        )
        self.assertFalse(self.controller.active)
        self.assertEqual(
            self.repository.list("account-a")[0]["match_id"], match_id
        )
        shown = []
        window._set_main_combat_mode = lambda mode: shown.append(mode)
        window._dispatch_message("scene", self.scene(5200167, 12))
        self.assertEqual(shown, ["pvp"])
        window._dispatch_message("scene", self.scene(5200167, 13))
        self.assertEqual(shown, ["pvp"])

    def observation(self, seconds, *, method="OnMsgDamageSyncV2", damage=100, event_id=None, **context):
        enemy = SELF_ID + 10
        return {
            "timestamp_ns": BASE_NS + seconds * 1_000_000_000,
            "record": {"method": method, "network_entity_id": SELF_ID,
                       "capture_event_id": event_id or f"obs-{seconds}-{method}"},
            "context": {"self_id": SELF_ID, "self_token": SELF_TOKEN,
                        "map_id": next(iter(RECORDABLE_MAPS)),
                        "profiles": [{"entity_id": enemy, "user_token": "enemy-token",
                                      "name": "对手", "entity_type": "Player"}], **context},
            "events": [{"attacker_id": SELF_ID, "target_id": enemy, "skill_id": 42,
                        "damage": damage, "damage_source": "network_exact"}] if damage else [],
        }

    def test_first_damage_observation_that_opens_a_match_is_not_lost(self):
        self.controller.observe(self.observation(1, damage=234))
        self.assertEqual(self.controller.snapshot(BASE_NS + 1_000_000_000)["damage"], 234)

    def test_both_opponent_directions_save_immutable_equipment_snapshots(self):
        map_id = next(iter(RECORDABLE_MAPS))
        self.controller.ingest_scene(self.scene(map_id, 1))
        profiles = [
            {"entity_id": SELF_ID + offset, "user_token": token,
             "name": token, "entity_type": "Player",
             "equipment_snapshot": {
                 "captured_at_ns": BASE_NS + 2_000_000_000,
                 "extraordinary_rating": rating, "partial": True,
                 "equipment": [{"slot": 1, "item_id": item_id}],
             }}
            for offset, token, item_id, rating in (
                (10, "enemy-token", 100, 88_893),
                (20, "incoming-token", 200, 90_001),
            )
        ]
        observation = self.observation(2, profiles=profiles)
        observation["events"].append({
            "attacker_id": SELF_ID + 20, "target_id": SELF_ID,
            "skill_id": 43, "damage": 50, "damage_source": "network_exact",
        })
        self.controller.observe(observation)
        finalized = self.controller.stop(BASE_NS + 3_000_000_000)
        frozen = deepcopy(finalized)
        opponents = {row["character_id"]: row for row in finalized["opponents"]}
        self.assertEqual(set(opponents), {"enemy-token", "incoming-token"})
        self.assertEqual(opponents["enemy-token"]["equipment_snapshot"]["equipment"][0]["item_id"], 100)
        self.assertEqual(opponents["incoming-token"]["equipment_snapshot"]["equipment"][0]["item_id"], 200)
        self.assertEqual(opponents["incoming-token"]["damage_from"], 50)
        profiles[0]["equipment_snapshot"]["equipment"][0]["item_id"] = 999
        self.controller.ingest_update("profile", profiles[0])
        self.assertEqual(finalized, frozen)
        self.assertEqual(self.repository.list("account-a"), [frozen])

    def test_live_checkpoint_omits_bulky_equipment_but_final_record_keeps_it(self):
        self.controller.observe(self.observation(1, map_id=5200167))
        snapshot = {
            "captured_at_ns": BASE_NS + 1_000_000_000,
            "extraordinary_rating": 90_001,
            "equipment": [{"slot": 1, "item_id": 12345}],
        }
        self.controller.ingest_update("equipment_profile", {
            "user_token": "enemy-token", "actor_id": SELF_ID + 10,
            "extraordinary_rating": 90_001,
            "equipment_snapshot": snapshot,
        })
        self.controller.checkpoint(BASE_NS + 2_000_000_000)
        with self.repository.session() as connection:
            raw = connection.execute(
                "SELECT payload_json FROM pvp_active_checkpoint "
                "WHERE account_key='account-a'"
            ).fetchone()[0]
        checkpoint = json.loads(raw)
        self.assertIsNone(checkpoint["opponents"][0]["equipment_snapshot"])
        self.assertEqual(len(checkpoint["allies"]), 1)
        self.assertIsNone(checkpoint["allies"][0]["equipment_snapshot"])
        self.assertEqual(checkpoint["enemies"][0]["character_id"], "enemy-token")
        finalized = self.controller.stop(BASE_NS + 3_000_000_000)
        self.assertEqual(
            finalized["opponents"][0]["equipment_snapshot"], snapshot
        )

    def test_hunter_queries_confirmed_opponent_without_arena_roster(self):
        from test_combat_model import DpsWindow

        enemy_token = "AQAAANANkGB8AAAA"
        self.controller.observe(self.observation(
            1, map_id=5200167,
            profiles=[{"entity_id": SELF_ID + 10, "user_token": enemy_token,
                       "name": "对手", "entity_type": "Player"}],
        ))
        self.assertFalse(self.controller.display_tracker.pvp_allies)
        window = object.__new__(DpsWindow)
        window.pvp_recording = self.controller

        members = window._pvp_equipment_members()
        self.assertEqual(
            {row["user_token"] for row in members},
            {SELF_TOKEN, enemy_token},
        )
        submit = Mock(return_value=True)
        window.worker = SimpleNamespace(
            equipment_session=lambda: (111, 222),
            schedule_equipment_profiles=submit,
        )
        window.model = SimpleNamespace(
            self_character_id=SELF_TOKEN,
            actor_character_ids={SELF_ID: SELF_TOKEN, SELF_ID + 10: enemy_token},
            party_member_count=1,
            display_name=lambda actor: "对手" if actor == SELF_ID + 10 else "本人",
            actor_is_ai=lambda _actor: False,
        )
        window._main_extraordinary_rating = lambda _actor: None
        window._invalidate_team_rating_preview_rows = Mock()
        window.team_equipment_profiles = {}
        self.assertTrue(window._schedule_team_equipment_profiles())
        queried = {
            member["user_token"]
            for call in submit.call_args_list
            for member in call.args[0]["members"]
        }
        self.assertIn(enemy_token, queried)

    def test_hunter_late_opponent_profile_fills_rating_and_equipment_before_upload(self):
        self.controller.observe(self.observation(1, map_id=5200167))
        finalized = self.controller.stop(BASE_NS + 2_000_000_000)
        self.assertIsNone(self.repository.due("account-a", time.time()))

        snapshot = {
            "captured_at_ns": BASE_NS + 1_500_000_000,
            "extraordinary_rating": 90_001,
            "equipment": [{"slot": 1, "item_id": 12345}],
        }
        self.assertTrue(self.repository.attach_equipment_snapshot(
            "account-a", finalized["match_id"], "enemy-token", snapshot
        ))
        saved = self.repository.list("account-a")[0]
        opponent = saved["opponents"][0]
        self.assertEqual(opponent["extraordinary_rating"], 90_001)
        self.assertEqual(opponent["equipment_snapshot"], snapshot)
        self.assertEqual(saved["enemies"][0]["extraordinary_rating"], 90_001)
        self.assertEqual(self.repository.due("account-a", time.time())["match_id"],
                         finalized["match_id"])

    def test_new_map_record_does_not_borrow_old_opponent_equipment(self):
        map_id, next_map_id = list(RECORDABLE_MAPS)[:2]
        self.controller.ingest_scene(self.scene(map_id, 1))
        profile = {"entity_id": SELF_ID + 10, "user_token": "enemy-token",
                   "name": "对手", "entity_type": "Player", "equipment_snapshot": {
                       "captured_at_ns": BASE_NS + 2_000_000_000,
                       "equipment": [{"slot": 1, "item_id": 100}], "partial": True,
                   }}
        self.controller.observe(self.observation(2, profiles=[profile]))
        self.controller.stop(BASE_NS + 3_000_000_000)
        self.controller.ingest_scene(self.scene(next_map_id, 4))
        self.controller.observe(self.observation(5, map_id=next_map_id))
        finalized = self.controller.stop(BASE_NS + 6_000_000_000)
        self.assertEqual(len(finalized["opponents"]), 1)
        self.assertIsNone(finalized["opponents"][0]["equipment_snapshot"])

    def duel_rpc(self, seconds, method, args):
        observation = self.observation(seconds, method=method, damage=0, map_id=5_200_002,
                                       instance_id='outdoor')
        observation['record']['decoded_arguments'] = args
        if method == 'OnMsgBeatenSyncV2' and len(args) >= 5:
            observation['events'] = [{
                'attacker_id': args[0], 'target_id': args[1],
                'skill_id': args[2], 'raw_damage': args[3], 'damage': args[4],
                'damage_source': 'network_exact', 'source_method': method,
                'beaten_exact': True,
            }]
        return self.controller.observe(observation)

    def begin_duel(self, seconds):
        self.duel_rpc(seconds, 'OnMsgIndividualPVPResponse', [True, 'enemy-token', '对手'])
        self.duel_rpc(seconds, 'OnMsgIndividualPVPState', [2, seconds * 1000])
        self.duel_rpc(seconds + 1, 'OnMsgIndividualPVPState', [3, (seconds + 1) * 1000])

    def test_consecutive_duels_reset_on_start_retain_on_end_and_save_separately(self):
        self.begin_duel(1)
        self.controller.observe(self.observation(3, damage=300, map_id=5_200_002, instance_id='outdoor'))
        self.assertIsNone(self.duel_rpc(4, 'OnMsgIndividualPVPResult', [1]))
        self.assertEqual(self.controller.snapshot(BASE_NS + 4_000_000_000)['damage'], 300)
        self.begin_duel(5)
        first = self.repository.list('account-a')[0]
        self.assertEqual(self.controller.snapshot(BASE_NS + 6_000_000_000)['damage'], 0)
        self.duel_rpc(8, 'OnMsgIndividualPVPState', [4, 8000])
        self.duel_rpc(8, 'OnMsgIndividualPVPState', [5, 8000])
        self.assertIsNone(self.duel_rpc(8, 'OnMsgIndividualPVPResult', [2]))
        self.duel_rpc(9, 'OnMsgBeatenSyncV2', [SELF_ID + 10, SELF_ID, 860211000, 509, 509])
        self.duel_rpc(9, 'OnMsgEntityDead', [0, 'enemy-token'])
        second = self.controller.poll(BASE_NS + 19_000_000_000)
        self.assertNotEqual(first['match_id'], second['match_id'])
        self.assertEqual((first['damage'], first['kills'], first['deaths'], first['assists']), (300, 1, 0, 0))
        self.assertEqual((second['damage'], second['kills'], second['deaths'], second['assists']), (0, 0, 1, 0))
        self.assertEqual((first['result'], second['result']), ('胜利', '失败'))
        self.assertEqual(second['opponents'][0]['name'], '对手')
        state = self.controller.snapshot(BASE_NS + 20_000_000_000)
        self.assertEqual((state['damage'], state['kills'], state['deaths'], state['assists']), (0, 0, 1, 0))
        self.assertEqual(state['incoming'][0]['defeats'], 1)
        self.assertEqual(state['total_taken'], '509')
        # Replayed result, other metadata and late unverified BeatenSync must
        # neither erase the win nor create a third history/outbox record.
        self.duel_rpc(20, 'OnMsgIndividualPVPResult', [2])
        self.duel_rpc(21, 'OnMsgSyncFightMode', [0])
        self.assertEqual(len(self.repository.list('account-a')), 2)
        self.assertIsNotNone(self.repository.due('account-a'))
        self.assertEqual(self.controller.snapshot(BASE_NS + 20_000_000_000)['deaths'], 1)

    def test_duel_stores_hp_balance_as_estimate_not_exact_healing(self):
        self.begin_duel(1)
        self.controller.ingest_update('actor_health', {
            'entity_id': SELF_ID, 'health_source': 'bound_realtime_hp',
            'current_hp': 1000, 'max_hp': 1000,
            'capture_timestamp_ns': BASE_NS + 2_000_000_000,
        })
        self.duel_rpc(3, 'OnMsgBeatenSyncV2', [SELF_ID + 10, SELF_ID, 860211000, 800, 800])
        self.controller.ingest_update('actor_health', {
            'entity_id': SELF_ID, 'health_source': 'bound_realtime_hp',
            'current_hp': 500, 'max_hp': 1000,
            'capture_timestamp_ns': BASE_NS + 3_500_000_000,
        })
        self.duel_rpc(4, 'OnMsgIndividualPVPResult', [2])
        record = self.controller.poll(BASE_NS + 15_000_000_000)
        self.assertEqual((record['healing'], record['healing_source']),
                         (300, 'hp_balance_estimate'))
        self.assertFalse(record['healing_source_available'])
        self.assertEqual((record['allies'][0]['healing'], record['allies'][0]['healing_source']),
                         (300, 'hp_balance_estimate'))

    def test_exact_duel_heal_overrides_hp_balance_estimate(self):
        self.begin_duel(1)
        self.controller.ingest_update('actor_health', {
            'entity_id': SELF_ID, 'health_source': 'bound_realtime_hp',
            'current_hp': 1000, 'max_hp': 1000,
            'capture_timestamp_ns': BASE_NS + 2_000_000_000,
        })
        self.duel_rpc(3, 'OnMsgBeatenSyncV2', [SELF_ID + 10, SELF_ID, 860211000, 800, 800])
        self.controller.ingest_update('actor_health', {
            'entity_id': SELF_ID, 'health_source': 'bound_realtime_hp',
            'current_hp': 500, 'max_hp': 1000,
            'capture_timestamp_ns': BASE_NS + 3_500_000_000,
        })
        self.controller.ingest_update('heal', {
            'healer_id': SELF_ID, 'target_id': SELF_ID,
            'effective_healing': 111, 'total_healing': 120,
            'healing_source': 'network_exact',
        })
        self.duel_rpc(4, 'OnMsgIndividualPVPResult', [2])
        record = self.controller.poll(BASE_NS + 15_000_000_000)
        self.assertEqual((record['healing'], record['healing_source']),
                         (111, 'network_exact'))

    def test_each_3v3_variant_is_one_independent_match_and_resets_on_next_entry(self):
        maps = sorted(map_id for map_id, metadata in RECORDABLE_MAPS.items()
                      if metadata['mode_id'] == 5500002)
        self.assertEqual(maps, [5200020, 5208002, 5208003, 5208012, 5208017, 5208018])
        for index, map_id in enumerate(maps):
            seconds = index * 10 + 1
            self.controller.ingest_scene(self.scene(map_id, seconds))
            self.assertEqual(self.controller.snapshot(BASE_NS + seconds * 1_000_000_000)['damage'], 0)
            self.controller.observe(self.observation(seconds + 1, damage=100, map_id=map_id))
            self.controller.ingest_scene(self.scene(0, seconds + 3, transition=True))
            self.assertEqual(self.controller.snapshot(BASE_NS + (seconds + 4) * 1_000_000_000)['damage'], 100)
        records = self.repository.list('account-a')
        self.assertEqual(len(records), len(maps))
        self.assertEqual({record['damage'] for record in records}, {100})

    def test_confirmed_party_roster_is_saved_as_optional_allies_without_fabricated_totals(self):
        ally_token = "AQAAABBBBBBBBBBB"
        self.controller.ingest_update(
            "party",
            {
                "in_team": True,
                "party_session_id": 7,
                "user_tokens": [SELF_TOKEN, ally_token],
            },
        )
        self.controller.ingest_update(
            "profile",
            {
                "entity_id": SELF_ID + 20,
                "user_token": ally_token,
                "name": "已确认队友",
                "profession_id": 1_200_006,
                "extraordinary_rating": 91_248,
                "entity_type": "Player",
            },
        )
        self.controller.ingest_scene(self.scene(5_208_004, 1))
        payload = self.controller.ingest_scene(self.scene(0, 5, transition=True))
        self.assertEqual(payload["team_size"], 6)
        allies = {row["character_id"]: row for row in payload["allies"]}
        self.assertEqual(allies[ally_token]["name"], "已确认队友")
        self.assertEqual(allies[ally_token]["extraordinary_rating"], 91_248)
        self.assertIsNone(allies[ally_token]["damage"])
        self.assertIsNone(allies[ally_token]["kills"])
        self.assertTrue(allies[SELF_TOKEN]["is_self"])

    def test_verified_pvp_roster_overrides_party_and_keeps_teammate_totals_unknown(self):
        stale_party_token = "AQAAASTALEPARTY1"
        ally_tokens = ("AQAAAPVPALLY001", "AQAAAPVPALLY002")
        self.controller.ingest_update(
            "party",
            {
                "in_team": True,
                "party_session_id": 8,
                "user_tokens": [SELF_TOKEN, stale_party_token],
            },
        )
        self.controller.ingest_scene(self.scene(5_208_004, 1))
        observation = self.observation(
            2, method="OnMsgSyncTeamPVPInfo", damage=0, map_id=5_208_004
        )
        observation["record"]["decoded_arguments"] = [
            2,
            {0: ally_tokens[0], 2: 1_200_003, 3: 70,
             4: "PVP队友甲", 6: 1_400_007},
            {0: ally_tokens[1], 2: 1_200_007, 3: 70,
             4: "PVP队友乙", 6: 1_400_008},
        ]
        self.controller.observe(observation)
        payload = self.controller.ingest_scene(self.scene(0, 5, transition=True))
        allies = {row["character_id"]: row for row in payload["allies"]}
        self.assertEqual(set(allies), {SELF_TOKEN, *ally_tokens})
        self.assertNotIn(stale_party_token, allies)
        for token in ally_tokens:
            self.assertIsNone(allies[token]["damage"])
            self.assertIsNone(allies[token]["kills"])
            self.assertIsNone(allies[token]["healing"])
            self.assertIsNone(allies[token]["taken"])

    def test_start_copies_complete_arena_roster_into_the_new_match_tracker(self):
        ally_tokens = ("AQAAAPVPALLY001", "AQAAAPVPALLY002")
        enemy_tokens = (
            "AQAAAPVPENEMY01",
            "AQAAAPVPENEMY02",
            "AQAAAPVPENEMY03",
        )
        arguments = [
            {
                11: {
                    0: [
                        {0: SELF_TOKEN, 1: "本人", 3: 1_200_001},
                        {0: ally_tokens[0], 1: "队友甲", 3: 1_200_002},
                        {0: ally_tokens[1], 1: "队友乙", 3: 1_200_003},
                    ]
                },
                22: {
                    0: [
                        {0: enemy_tokens[0], 1: "敌方甲", 3: 1_200_004},
                        {0: enemy_tokens[1], 1: "敌方乙", 3: 1_200_005},
                        {
                            0: enemy_tokens[2],
                            1: "竞技人机",
                            2: 3_000_001,
                            3: 1_200_006,
                        },
                    ]
                },
            }
        ]
        self.assertTrue(
            self.controller.current._replace_pvp_full_roster(arguments)
        )
        self.controller.map_id = 5_208_003
        self.controller.start(BASE_NS + 1_000_000_000)

        tracker = self.controller.recording["tracker"]
        self.assertEqual(
            {row["user_token"] for row in tracker.pvp_team_members(include_self=True)},
            {SELF_TOKEN, *ally_tokens},
        )
        enemies = tracker.pvp_team_members(side="enemy")
        self.assertEqual(
            {row["user_token"] for row in enemies}, set(enemy_tokens)
        )
        self.assertTrue(
            next(
                row for row in enemies if row["user_token"] == enemy_tokens[2]
            )["is_ai"]
        )
        self.controller.current.pvp_full_roster_teams.clear()
        self.assertEqual(len(tracker.pvp_full_roster_teams), 2)

    def test_verified_enemy_side_removes_stale_party_copy(self):
        enemy_token = "AQAAAPVPENEMY01"
        ally_token = "AQAAAPVPALLY001"
        self.controller.ingest_update(
            "party",
            {
                "in_team": True,
                "party_session_id": 9,
                "user_tokens": [SELF_TOKEN, enemy_token],
            },
        )
        self.controller.ingest_scene(self.scene(5_208_017, 1))
        observation = self.observation(
            2, method="OnMsgSyncTeamPVPInfo", damage=0, map_id=5_208_017
        )
        observation["record"]["decoded_arguments"] = [
            2,
            {0: ally_token, 2: 1_200_003, 3: 70,
             4: "真实队友", 6: 1_400_007},
            {0: enemy_token, 2: 1_200_007, 3: 70,
             4: "真实敌方", 5: 1, 6: 1_400_008},
        ]
        self.controller.observe(observation)
        payload = self.controller.ingest_scene(self.scene(0, 5, transition=True))

        ally_ids = {row["character_id"] for row in payload["allies"]}
        enemy_ids = {row["character_id"] for row in payload["enemies"]}
        self.assertEqual(ally_ids, {SELF_TOKEN, ally_token})
        self.assertIn(enemy_token, enemy_ids)
        self.assertTrue(ally_ids.isdisjoint(enemy_ids))

    def test_mastery_3v3_third_team_death_does_not_guess_result_or_end_match(self):
        map_id = 5_208_017
        ally_tokens = ("AQAAAPVPALLY001", "AQAAAPVPALLY002")
        enemy_tokens = (
            "AQAAAPVPENEMY01",
            "AQAAAPVPENEMY02",
            "AQAAAPVPENEMY03",
        )
        actor_by_token = {
            SELF_TOKEN: SELF_ID,
            ally_tokens[0]: SELF_ID + 20,
            ally_tokens[1]: SELF_ID + 30,
            enemy_tokens[0]: SELF_ID + 40,
            enemy_tokens[1]: SELF_ID + 50,
            enemy_tokens[2]: SELF_ID + 60,
        }
        self.controller.ingest_scene(self.scene(map_id, 1))
        roster = self.observation(
            2, method="OnMsgSyncTeamPVPInfo", damage=0, map_id=map_id
        )
        roster["record"]["decoded_arguments"] = [
            5,
            *[
                {0: token, 2: 1_200_001 + index, 3: 70,
                 4: f"队友{index + 1}", 6: 1_400_001 + index}
                for index, token in enumerate(ally_tokens)
            ],
            *[
                {0: token, 2: 1_200_004 + index, 3: 70,
                 4: f"敌方{index + 1}", 5: 1, 6: 1_400_004 + index}
                for index, token in enumerate(enemy_tokens)
            ],
        ]
        self.controller.observe(roster)
        profiles = [
            {
                "entity_id": actor,
                "user_token": token,
                "name": token,
                "entity_type": "Player",
            }
            for token, actor in actor_by_token.items()
        ]
        finalized = None
        for seconds, victim_token in enumerate((SELF_TOKEN, *ally_tokens), 3):
            death = self.observation(
                seconds,
                method="OnMsgEntityDead",
                damage=0,
                map_id=map_id,
                profiles=profiles,
            )
            death["record"]["network_entity_id"] = actor_by_token[victim_token]
            death["record"]["decoded_arguments"] = [0, enemy_tokens[0]]
            finalized = self.controller.observe(death)

        self.assertIsNone(finalized)
        self.assertTrue(self.controller.active)
        self.assertNotEqual(
            self.controller.snapshot(BASE_NS + 5_000_000_000)["result"],
            "失败",
        )
        self.assertEqual(len(self.repository.list("account-a")), 0)

        finalized = self.controller.ingest_scene(
            self.scene(0, 6, transition=True)
        )
        self.assertIsNotNone(finalized)
        self.assertEqual(finalized["result"], "未知")
        self.assertEqual(finalized["end_reason"], "left_map")
        self.assertEqual(len(self.repository.list("account-a")), 1)

    def test_verified_team_settlement_overlays_6v6_rows_and_keeps_missing_values_unknown(self):
        enemy_token = "AQAAAENEMY000001"
        self.controller.ingest_scene(self.scene(5_208_004, 1))
        payload = self.controller.ingest_update(
            "pvp_match_context",
            {
                "capture_timestamp_ns": BASE_NS + 8_000_000_000,
                "result": "胜利",
                "finished": True,
                "teams": {
                    "allies": [
                        {
                            "character_id": SELF_TOKEN,
                            "name": "本人",
                            "damage_done": 12_345,
                            "healing_done": 678,
                            "kills_on": 2,
                        }
                    ],
                    "enemies": [
                        {
                            "character_id": enemy_token,
                            "name": "敌方一号",
                            "extraordinary_rating": 91_248,
                            "damage_taken": 9_876,
                        }
                    ],
                },
            },
        )
        self.assertEqual(payload["result"], "胜利")
        self.assertEqual(payload["team_size"], 6)
        self.assertEqual(payload["allies"][0]["damage"], 12_345)
        self.assertEqual(payload["allies"][0]["healing"], 678)
        self.assertTrue(payload["allies"][0]["is_self"])
        enemy = payload["enemies"][0]
        self.assertEqual(enemy["character_id"], enemy_token)
        self.assertEqual(enemy["taken"], 9_876)
        self.assertIsNone(enemy.get("damage"))

    def test_verified_3v3_result_saves_on_reveal_once_with_six_equipment_snapshots(self):
        map_id = 5_208_003
        self.controller.ingest_scene(self.scene(map_id, 1))
        settlement = _team_pvp_settlement(
            deepcopy(TEAM_SETTLEMENT_ARGS), SELF_TOKEN
        )
        self.assertIsNotNone(settlement)
        all_rows = [*settlement['allies'], *settlement['enemies']]
        for index, row in enumerate(all_rows, 1):
            self.controller.ingest_update('equipment_profile', {
                'user_token': row['character_id'],
                'name': row['name'],
                'profession_id': row['profession_id'],
                'extraordinary_rating': row.get('extraordinary_rating'),
                'equipment_snapshot': {
                    'captured_at_ns': BASE_NS + 5_000_000_000 + index,
                    'equipment': [{'slot': 1, 'item_id': 10_000 + index}],
                    'partial': True,
                },
            })
        observation = self.observation(
            10,
            method='OnMsgTeamPVPSettlement',
            damage=0,
            map_id=map_id,
            event_id='team-result-798',
        )
        observation['record']['decoded_arguments'] = deepcopy(
            TEAM_SETTLEMENT_ARGS
        )
        observation['team_pvp_settlement'] = settlement

        finalized = self.controller.observe(observation)

        self.assertIsNotNone(finalized)
        self.assertFalse(self.controller.active)
        self.assertEqual(
            (finalized['result'], finalized['end_reason']),
            ('胜利', 'match_result'),
        )
        self.assertEqual((len(finalized['allies']), len(finalized['enemies'])), (3, 3))
        self.assertEqual(
            sum(
                isinstance(row.get('equipment_snapshot'), dict)
                for row in (*finalized['allies'], *finalized['enemies'])
            ),
            6,
        )
        self_row = next(row for row in finalized['allies'] if row['is_self'])
        self.assertEqual(
            (finalized['damage'], finalized['healing'], finalized['taken']),
            (0, 18_580, 7_594),
        )
        self.assertEqual(self_row['healing'], 18_580)
        self.assertEqual(
            (len(finalized['teams']['allies']), len(finalized['teams']['enemies'])),
            (3, 3),
        )
        self.assertEqual(
            finalized['teams']['allies'][0]['statistics_authoritative'],
            True,
        )
        self.assertEqual(len(self.repository.list('account-a')), 1)
        self.assertIsNotNone(self.repository.due('account-a', 0))

        self.assertIsNone(self.controller.observe(deepcopy(observation)))
        self.controller.ingest_scene(self.scene(0, 12, transition=True))
        self.assertEqual(len(self.repository.list('account-a')), 1)

    def test_real_method_798_capture_routes_to_immediate_six_player_record(self):
        path = Path('logs/network_20260921_172105.jsonl')
        if not path.is_file():
            self.skipTest('verified 2026-09-21 3V3 capture is unavailable')
        with path.open(encoding='utf-8-sig') as stream:
            raw = next(
                json.loads(line)
                for line in stream
                if '"method_id": 798' in line
            )
        raw_teams = raw['arguments'][0]
        capture_self_token = next(
            str(member.get('0') or '')
            for team in raw_teams.values()
            for member in team.get('4', ())
            if member.get('4') == '莫雪'
        )
        capture_self_id = int(raw['entity_id'])
        parser = NpcapParserAdapter(
            remembered_self_token=capture_self_token
        )
        parser.self_id = capture_self_id
        parser.self_token = capture_self_token
        parser.self_confirmed = True
        parser.token_actors[capture_self_token] = capture_self_id
        parser.actor_tokens[capture_self_id] = capture_self_token
        controller = PvpRecordingController(self.repository)
        controller.bind_account('account-a')
        controller.ingest_update('identity', {
            'self_id': capture_self_id,
            'user_token': capture_self_token,
            'self_confirmed': True,
        })
        record = {
            'method': 'OnMsgTeamPVPSettlement',
            'decoded_arguments': raw['arguments'],
            'network_entity_id': raw['entity_id'],
            'script_entity': raw['entity_id'],
            'capture_source': 'npcap',
            'arguments_synchronized': True,
            'capture_timestamp_ns': raw['capture_timestamp_ns'],
            'capture_event_id': raw['capture_event_id'],
            'npcap_method_scope': 'team_pvp_settlement_shape_compat',
            'npcap_recipient': capture_self_token,
        }
        controller.ingest_scene(self.scene(5_208_003, 1))
        for kind, value in parser.process(record):
            controller.ingest_update(kind, value)
        finalized = None
        for observation in parser.take_pvp_observations():
            finalized = controller.observe(observation) or finalized

        self.assertIsNotNone(finalized)
        self.assertEqual(finalized['end_reason'], 'match_result')
        self.assertEqual(
            (len(finalized['allies']), len(finalized['enemies'])),
            (3, 3),
        )
        self.assertEqual(
            sum(
                bool(row.get('is_ai'))
                for row in (*finalized['allies'], *finalized['enemies'])
            ),
            0,
        )
        self.assertEqual(len(self.repository.list('account-a')), 1)

    def test_late_same_match_equipment_reply_completes_team_history(self):
        map_id = 5_208_003
        self.controller.ingest_scene(self.scene(map_id, 1))
        settlement = _team_pvp_settlement(
            deepcopy(TEAM_SETTLEMENT_ARGS), SELF_TOKEN
        )
        observation = self.observation(
            10,
            method='OnMsgTeamPVPSettlement',
            damage=0,
            map_id=map_id,
        )
        observation['record']['decoded_arguments'] = deepcopy(
            TEAM_SETTLEMENT_ARGS
        )
        observation['team_pvp_settlement'] = settlement
        finalized = self.controller.observe(observation)
        target = finalized['enemies'][0]['character_id']

        changed = self.controller.ingest_update('equipment_profile', {
            'user_token': target,
            'name': finalized['enemies'][0]['name'],
            'equipment_snapshot': {
                'captured_at_ns': BASE_NS + 11_000_000_000,
                'equipment': [{'slot': 1, 'item_id': 77_777}],
                'partial': True,
            },
        })

        self.assertIsNone(changed)
        stored = self.repository.list('account-a')[0]
        enemy = next(
            row for row in stored['enemies']
            if row['character_id'] == target
        )
        self.assertEqual(
            enemy['equipment_snapshot']['equipment'][0]['item_id'], 77_777
        )

    def test_finished_3v3_equipment_query_uses_only_its_short_reply_grace(self):
        self.controller.ingest_scene(self.scene(5_208_003, 1))
        settlement = _team_pvp_settlement(
            deepcopy(TEAM_SETTLEMENT_ARGS), SELF_TOKEN
        )
        observation = self.observation(
            10, method='OnMsgTeamPVPSettlement', damage=0,
            map_id=5_208_003,
        )
        observation['record']['decoded_arguments'] = deepcopy(
            TEAM_SETTLEMENT_ARGS
        )
        observation['team_pvp_settlement'] = settlement
        finalized = self.controller.observe(observation)

        self.assertTrue(self.controller.equipment_query_active(
            finalized['ended_at_ns'] + 1_000_000_000
        ))
        self.assertFalse(self.controller.equipment_query_active(
            finalized['ended_at_ns'] + 21_000_000_000
        ))
        self.assertEqual(len(self.controller.finalized_equipment_tokens), 6)

    def test_finished_duel_late_equipment_reply_attaches_to_that_duel(self):
        self.begin_duel(1)
        self.duel_rpc(4, 'OnMsgIndividualPVPResult', [1])
        finalized = self.controller.poll(BASE_NS + 16_000_000_000)
        enemy_token = finalized['enemies'][0]['character_id']
        self.assertTrue(self.controller.equipment_query_active(
            finalized['ended_at_ns'] + 1_000_000_000
        ))

        self.controller.ingest_update('equipment_profile', {
            'user_token': enemy_token,
            'equipment_snapshot': {
                'captured_at_ns': finalized['ended_at_ns'] + 1,
                'equipment': [{'slot': 1, 'item_id': 33_333}],
            },
        })
        stored = self.repository.list('account-a')[0]
        self.assertEqual(
            stored['enemies'][0]['equipment_snapshot']['equipment'][0]['item_id'],
            33_333,
        )

    def test_team_upload_waits_until_all_six_human_snapshots_are_attached(self):
        map_id = 5_208_003
        self.controller.ingest_scene(self.scene(map_id, 1))
        settlement = _team_pvp_settlement(
            deepcopy(TEAM_SETTLEMENT_ARGS), SELF_TOKEN
        )
        observation = self.observation(
            10,
            method='OnMsgTeamPVPSettlement',
            damage=0,
            map_id=map_id,
        )
        observation['record']['decoded_arguments'] = deepcopy(
            TEAM_SETTLEMENT_ARGS
        )
        observation['team_pvp_settlement'] = settlement
        finalized = self.controller.observe(observation)
        members = [*finalized['allies'], *finalized['enemies']]

        self.assertEqual(len(members), 6)
        self.assertIsNone(self.repository.due('account-a', 0))
        for index, member in enumerate(members, 1):
            self.controller.ingest_update('equipment_profile', {
                'user_token': member['character_id'],
                'name': member['name'],
                'equipment_snapshot': {
                    'captured_at_ns': BASE_NS + 11_000_000_000 + index,
                    'equipment': [{'slot': 1, 'item_id': 80_000 + index}],
                    'partial': True,
                },
            })
            if index < len(members):
                self.assertIsNone(self.repository.due('account-a', 0))

        due = self.repository.due('account-a', 0)
        self.assertIsNotNone(due)
        stored = self.repository.list('account-a')[0]
        self.assertEqual(
            sum(
                isinstance(row.get('equipment_snapshot'), dict)
                for row in (*stored['allies'], *stored['enemies'])
            ),
            6,
        )

    def test_result_schedules_team_equipment_before_waking_record_upload(self):
        from test_combat_model import DpsWindow

        order = []
        tracker = SimpleNamespace(generation=8)
        recording = SimpleNamespace(
            display_tracker=tracker,
            current=tracker,
            observe=Mock(return_value={'match_id': 'pvp-team-result'}),
        )
        window = object.__new__(DpsWindow)
        window.pvp_recording = recording
        window.pvp_tracker = tracker
        window.pvp_upload_worker = SimpleNamespace(
            wake=lambda: order.append('wake-upload')
        )
        window.main_combat_mode = 'pve'
        window._schedule_team_equipment_profiles = (
            lambda: order.append('schedule-equipment')
        )
        window._dispatch_message = lambda *_args: order.append('history-refresh')

        window._handle_pvp_observation({'record': {}})

        self.assertEqual(
            order,
            ['schedule-equipment', 'wake-upload', 'history-refresh'],
        )

    def test_damage_observations_do_not_rescan_every_equipment_member(self):
        from test_combat_model import DpsWindow

        tracker = SimpleNamespace(generation=0)
        def observe(_payload):
            tracker.generation += 1
            return None

        window = object.__new__(DpsWindow)
        window.pvp_recording = SimpleNamespace(
            display_tracker=tracker, current=tracker, observe=observe
        )
        window.main_combat_mode = 'pve'
        schedules = []
        window._schedule_team_equipment_profiles = lambda: schedules.append(1)

        for _ in range(100):
            window._handle_pvp_observation({'damage': 1})

        self.assertEqual(schedules, [])

    def test_complete_entry_roster_queries_enemy_rating_immediately(self):
        from test_combat_model import DpsWindow

        tracker = SimpleNamespace(generation=0)
        def observe(_payload):
            tracker.generation += 1
            return None

        window = object.__new__(DpsWindow)
        window.pvp_recording = SimpleNamespace(
            display_tracker=tracker, current=tracker, observe=observe
        )
        window.main_combat_mode = 'pve'
        schedules = []
        window._schedule_team_equipment_profiles = lambda: schedules.append(True)

        window._handle_pvp_observation({
            'record': {'method': 'RetGetTeamArenaBattleInfo'}
        })

        self.assertEqual(schedules, [True])

    def test_formal_match_start_replaces_retained_duel_without_history_leak(self):
        self.begin_duel(1)
        self.controller.observe(self.observation(3, damage=300, map_id=5_200_002, instance_id='outdoor'))
        self.duel_rpc(4, 'OnMsgIndividualPVPResult', [1])
        self.controller.ingest_scene(self.scene(5200020, 10))
        self.controller.observe(self.observation(11, damage=200, map_id=5200020))
        record = self.controller.ingest_scene(self.scene(0, 20, transition=True))
        self.assertEqual((record['damage'], record['kills'], record['deaths']), (200, 0, 0))
        self.assertEqual((self.controller.snapshot(BASE_NS + 21_000_000_000)['damage'],
                          self.controller.snapshot(BASE_NS + 21_000_000_000)['kills']), (200, 0))
        self.controller.ingest_scene(self.scene(5200223, 30))
        state = self.controller.snapshot(BASE_NS + 30_000_000_000)
        self.assertEqual((state['damage'], state['taken'], state['kills'], state['deaths']), (0, 0, 0, 0))
        self.assertEqual((state['outgoing'], state['incoming']), ([], []))

    def test_every_formal_pvp_entry_clears_hud_only_once_without_erasing_history(self):
        self.controller.ingest_scene(self.scene(5200020, 1))
        self.controller.observe(self.observation(2, damage=300, map_id=5200020))
        self.controller.ingest_scene(self.scene(0, 5, transition=True))
        for index, map_id in enumerate(sorted(RECORDABLE_MAPS)):
            seconds = index * 10 + 10
            self.controller.ingest_scene(self.scene(map_id, seconds))
            self.assertEqual(self.controller.snapshot(BASE_NS + seconds * 1_000_000_000)['damage'], 0)
            self.controller.observe(self.observation(seconds + 1, damage=100, map_id=map_id))
            self.controller.ingest_scene(self.scene(map_id, seconds + 2))
            self.assertEqual(self.controller.snapshot(BASE_NS + (seconds + 3) * 1_000_000_000)['damage'], 100)
        self.controller.ingest_scene(self.scene(0, 200, transition=True))
        self.controller.stop(BASE_NS + 201_000_000_000)
        records = self.repository.list('account-a')
        self.assertEqual(len(records), 1 + len(RECORDABLE_MAPS))
        self.assertEqual(records[-1]['damage'], 300)
        self.assertEqual(self.controller.snapshot(BASE_NS + 201_000_000_000)['damage'], 100)

    def test_same_map_new_instance_resets_every_mode(self):
        for index, map_id in enumerate((5200021, 5200020)):
            seconds = index * 20 + 1
            self.controller.observe(self.observation(seconds, damage=100, map_id=map_id, instance_id='a'))
            self.controller.observe(self.observation(seconds + 2, damage=200, map_id=map_id, instance_id='b'))
            self.assertEqual(self.controller.snapshot(BASE_NS + (seconds + 3) * 1_000_000_000)['damage'], 200)

    def test_large_battle_short_leave_same_instance_resumes_one_match(self):
        map_id = 5200223
        self.controller.observe(self.observation(1, damage=100, map_id=map_id, instance_id='activity-a'))
        match_id = self.controller.recording['match_id']
        self.controller.ingest_scene(self.scene(0, 5, transition=True))
        self.assertFalse(self.controller.active)
        self.assertEqual(self.repository.list('account-a'), [])
        self.controller.ingest_scene(self.scene(map_id, 8))
        self.assertTrue(self.controller.active)
        self.controller.observe(self.observation(9, damage=200, map_id=map_id, instance_id='activity-a'))
        self.assertEqual(self.controller.recording['match_id'], match_id)
        self.assertEqual(self.controller.snapshot(BASE_NS + 10_000_000_000)['damage'], 300)
        self.controller.ingest_scene(self.scene(0, 12, transition=True))
        record = self.controller.stop(BASE_NS + 13_000_000_000)
        self.assertEqual((record['match_id'], record['damage']), (match_id, 300))
        self.assertEqual(len(self.repository.list('account-a')), 1)

    def test_large_battle_reentry_with_new_instance_starts_new_match(self):
        map_id = 5200223
        self.controller.observe(self.observation(1, damage=100, map_id=map_id, instance_id='activity-a'))
        first_id = self.controller.recording['match_id']
        self.controller.ingest_scene(self.scene(0, 5, transition=True))
        self.controller.ingest_scene(self.scene(map_id, 8))
        finalized = self.controller.observe(
            self.observation(9, damage=200, map_id=map_id, instance_id='activity-b')
        )
        self.assertEqual(finalized['match_id'], first_id)
        self.assertEqual(finalized['damage'], 100)
        self.assertEqual(finalized['ended_at_ns'], BASE_NS + 5_000_000_000)
        self.assertNotEqual(self.controller.recording['match_id'], first_id)
        self.assertEqual(self.controller.snapshot(BASE_NS + 10_000_000_000)['damage'], 200)

    def test_club_event_id_resumes_same_event_beyond_fallback_grace(self):
        map_id = 5200021
        self.controller.ingest_scene(self.scene(map_id, 1, event_id='event-a'))
        match_id = self.controller.recording['match_id']
        self.controller.observe(self.observation(2, damage=100, map_id=map_id, event_id='event-a'))
        self.controller.ingest_scene(self.scene(0, 5, transition=True))
        self.assertIsNone(self.controller.poll(BASE_NS + 700_000_000_000))
        self.controller.ingest_scene(self.scene(map_id, 701, event_id='event-a'))
        self.assertEqual(self.controller.recording['match_id'], match_id)
        self.controller.ingest_update('pvp_match_context', {
            'capture_timestamp_ns': BASE_NS + 702_000_000_000,
            'event_id': 'event-b',
        })
        self.assertNotEqual(self.controller.recording['match_id'], match_id)
        first = next(row for row in self.repository.list('account-a') if row['match_id'] == match_id)
        self.assertEqual((first['event_id'], first['damage']), ('event-a', 100))

    def test_club_timer_starts_with_verified_league_entry(self):
        map_id = 5_200_021
        self.controller.ingest_scene(self.scene(map_id, 1))
        self.assertEqual(self.controller.snapshot(BASE_NS + 35_000_000_000)['time'], '00:00')
        observation = self.observation(40, method='OnMsgSyncLeagueInfo',
                                       damage=0, map_id=map_id)
        observation['record']['decoded_arguments'] = [{0: {
            101: {6: 1, 17: {11: {0: 1, 1: [
                {2: SELF_TOKEN, 5: 'Self', 8: 1_200_001, 27: 95_000},
            ]}}},
        }}]

        self.controller.observe(observation)

        self.assertEqual(self.controller.snapshot(BASE_NS + 45_000_000_000)['time'], '00:05')
        self.assertEqual(
            self.controller.recording['battle_started_at_ns'],
            BASE_NS + 40_000_000_000,
        )

    def test_champion_round_id_splits_rounds_and_preserves_series_id(self):
        map_id = 5200110
        self.controller.ingest_scene(self.scene(map_id, 1, series_id='series-a',
                                                round_id='round-1', mode_id=5500006))
        self.controller.observe(self.observation(2, damage=321, map_id=map_id,
                                                 series_id='series-a', round_id='round-1'))
        first_id = self.controller.recording['match_id']
        finalized = self.controller.ingest_update('pvp_match_context', {
            'capture_timestamp_ns': BASE_NS + 5_000_000_000,
            'series_id': 'series-a', 'round_id': 'round-2',
        })
        self.assertEqual((finalized['match_id'], finalized['damage']), (first_id, 321))
        self.assertEqual((finalized['series_id'], finalized['round_id'], finalized['mode_id']),
                         ('series-a', 'round-1', 5500006))
        self.assertEqual(self.controller.snapshot(BASE_NS + 6_000_000_000)['damage'], 0)

    def test_explicit_match_result_retains_hud_until_new_match_identity(self):
        map_id = 5200020
        self.controller.ingest_scene(self.scene(map_id, 1, source_battle_id='battle-a'))
        self.controller.observe(self.observation(2, damage=456, map_id=map_id,
                                                 source_battle_id='battle-a'))
        finalized = self.controller.ingest_update('pvp_match_context', {
            'capture_timestamp_ns': BASE_NS + 5_000_000_000,
            'source_battle_id': 'battle-a', 'result': '胜利', 'finished': True,
        })
        self.assertEqual((finalized['result'], finalized['damage']), ('胜利', 456))
        self.controller.ingest_update('profile', {
            'entity_id': SELF_ID, 'user_token': SELF_TOKEN, 'name': '本人',
        })
        self.assertFalse(self.controller.active)
        self.assertEqual(self.controller.snapshot(BASE_NS + 6_000_000_000)['damage'], 456)
        self.controller.ingest_update('pvp_match_context', {
            'capture_timestamp_ns': BASE_NS + 7_000_000_000,
            'source_battle_id': 'battle-b',
        })
        self.assertTrue(self.controller.active)
        self.assertEqual(self.controller.snapshot(BASE_NS + 7_000_000_000)['damage'], 0)

    def test_record_payload_contains_canonical_and_requested_alias_fields(self):
        map_id = 5200020
        self.controller.ingest_scene(self.scene(map_id, 1))
        self.controller.observe(self.observation(2, damage=123, map_id=map_id))
        payload = self.controller.stop(BASE_NS + 3_000_000_000)
        self.assertEqual(payload['battle_id'], payload['match_id'])
        self.assertEqual((payload['damage'], payload['damage_done']), (123, 123))
        self.assertEqual(payload['taken'], payload['damage_taken'])
        self.assertEqual(payload['start_time'], payload['started_at'])
        opponent = payload['opponents'][0]
        self.assertEqual(opponent['damage_to'], opponent['damage'])
        self.assertEqual(opponent['damage_from'], opponent['taken'])
        self.assertEqual(opponent['kills_on'], opponent['kills'])
        self.assertEqual(opponent['deaths_to'], opponent['defeats'])
        self.assertEqual(opponent['interaction_scope'], 'pair_exact')
        self.assertEqual(
            payload['data_scope'],
            {
                'roster': 'partial',
                'self_metrics': 'self_exact',
                'team_metrics': 'observed_partial',
                'opponent_interactions': 'pair_exact',
            },
        )
        local = next(row for row in payload['allies'] if row['is_self'])
        self.assertEqual(local['metrics_scope'], 'self_exact')
        self.assertEqual(local['skills_scope'], 'self_exact')

    def test_local_pvp_favorite_is_persisted_without_mutating_match_payload(self):
        map_id = 5200020
        self.controller.ingest_scene(self.scene(map_id, 1))
        payload = self.controller.stop(BASE_NS + 3_000_000_000)
        match_id = payload['match_id']

        updated = self.repository.set_favorite('account-a', match_id, True)

        self.assertIsNotNone(updated)
        self.assertTrue(updated['favorite'])
        self.assertTrue(self.repository.list('account-a')[0]['favorite'])
        due = self.repository.due('account-a')
        self.assertNotIn('favorite', json.loads(due['payload_json']))

    def test_capture_context_reset_saves_duel_once_and_clears_old_character_hud(self):
        self.begin_duel(1)
        self.controller.observe(self.observation(3, damage=300, map_id=5_200_002, instance_id='outdoor'))
        self.duel_rpc(4, 'OnMsgIndividualPVPResult', [1])
        self.controller.reset_context(BASE_NS + 5_000_000_000)
        self.controller.reset_context(BASE_NS + 6_000_000_000)
        state = self.controller.snapshot(BASE_NS + 10_000_000_000)
        self.assertEqual((state['damage'], state['kills']), (0, '--'))
        self.assertEqual(len(self.repository.list('account-a')), 1)

    def test_interrupted_duel_checkpoint_is_recovered_for_backend_upload(self):
        self.begin_duel(1)
        self.controller.observe(self.observation(3, damage=300, map_id=5_200_002, instance_id='outdoor'))
        self.controller.checkpoint(BASE_NS + 4_000_000_000)
        recovered = PvpRecordingController(self.repository)
        recovered.bind_account('account-a')
        record = self.repository.list('account-a')[0]
        self.assertEqual((record['damage'], record['result'], record['end_reason']), (300, '未知', 'interrupted'))
        self.assertIsNotNone(self.repository.due('account-a'))

    def test_crash_during_duel_tail_keeps_authoritative_result_and_marks_capture_incomplete(self):
        self.begin_duel(1)
        self.controller.observe(self.observation(3, damage=300, map_id=5_200_002,
                                                 instance_id='outdoor'))
        self.duel_rpc(4, 'OnMsgIndividualPVPResult', [1])
        recovered = PvpRecordingController(self.repository)
        recovered.bind_account('account-a')
        record = self.repository.list('account-a')[0]
        self.assertEqual((record['result'], record['end_reason']), ('胜利', 'duel_result'))
        self.assertFalse(record['capture_complete'])
        self.assertEqual(recovered.snapshot(BASE_NS + 5_000_000_000)['damage'], 0)

    def test_recorded_consecutive_win_loss_capture_keeps_hud_and_single_records_separate(self):
        from npcap_parser_adapter import NpcapParserAdapter
        from pvp_tracker import timestamp_ns
        path = Path('logs/network_20260918_171310.jsonl')
        if not path.is_file():
            self.skipTest('local consecutive duel capture evidence is unavailable')
        records = [json.loads(line) for line in path.open(encoding='utf-8-sig')]
        # Same capture's live local profile, not the first damage attacker.
        profile = next(record for record in records if record.get('method') == 'NpcapLiveTeamProfile'
                       and record.get('user_token') == SELF_TOKEN)
        parser = NpcapParserAdapter({SELF_TOKEN: profile}, remembered_self_token=SELF_TOKEN)
        controller = PvpRecordingController(self.repository)
        controller.bind_account('real-replay')
        result_states = []
        for record in records:
            if record.get('diagnostic_type'):
                continue
            stamp = timestamp_ns(record)
            updates = (parser.apply_read_only_team_profile(record) if record.get('method') == 'NpcapLiveTeamProfile'
                       else parser.process(record))
            for kind, value in updates:
                if kind == 'scene':
                    controller.ingest_scene(value)
                else:
                    controller.ingest_update(kind, value)
            for observation in parser.take_pvp_observations():
                controller.observe(observation)
            if record.get('method') == 'OnMsgIndividualPVPResult':
                result_states.append(controller.snapshot(stamp))
        self.assertEqual(parser.pvp_observation_errors, 0)
        self.assertEqual(len(result_states), 2)
        self.assertEqual((result_states[0]['damage'], result_states[0]['kills'], result_states[0]['deaths']), (17_012, 1, 0))
        self.assertEqual((result_states[1]['damage'], result_states[1]['kills'], result_states[1]['deaths']), (0, 0, 1))
        self.assertEqual(result_states[1]['result'], '失败')
        self.assertEqual(result_states[1]['assists'], 0)
        self.assertEqual(result_states[1]['incoming'][0]['defeats'], 1)
        controller.poll(max(timestamp_ns(record) for record in records) + 11_000_000_000)
        stored = self.repository.list('real-replay')
        self.assertEqual(len(stored), 2)
        self.assertEqual([(row['damage'], row['result']) for row in stored], [(0, '失败'), (17_012, '胜利')])
        self.assertEqual((stored[0]['taken'], stored[0]['damage_taken']), (1_782, 1_782))
        self.assertEqual(stored[0]['opponents'][0]['damage_from'], 1_782)
        self.assertIsNotNone(self.repository.due('real-replay'))

    def test_desktop_teammate_header_only_uses_live_roster_and_clears_on_leave(self):
        from test_combat_model import DpsWindow
        window = object.__new__(DpsWindow)
        window.pvp_recording = self.controller
        window.config = {}
        first, second, npc, old = SELF_ID + 1, SELF_ID + 2, SELF_ID + 3, SELF_ID + 99
        window.model = SimpleNamespace(self_id=SELF_ID, party_active=True,
            party_ids={SELF_ID, first, second, npc}, provisional_party_ids=set(),
            friend_order=[second, first, old], non_player_actor_ids={npc},
            entity_names={SELF_ID: "本人", first: "墨爵", second: "碎星", old: "旧队友"},
            entity_professions={first: 1_200_002, second: 1_200_003}, actor_character_ids={})
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        self.controller.ingest_update('party', {
            'in_team': True, 'party_session_id': 1,
            'user_tokens': [SELF_TOKEN, 'fresh-teammate-a', 'fresh-teammate-b'],
        })
        state = window._pvp_layered_main_snapshot()
        self.assertTrue(state["pvp_in_map"])
        self.assertEqual([row["name"] for row in state["pvp_teammates"]], ["碎星", "墨爵"])
        window.hide_names = True
        self.assertEqual([row["name"] for row in window._pvp_current_teammates()], ["队友1", "队友2"])
        window.model.party_active = False
        self.assertEqual(window._pvp_current_teammates(), [])
        self.controller.ingest_scene(self.scene(0, 20, transition=True))
        self.assertFalse(window._pvp_layered_main_snapshot()["pvp_in_map"])

    def test_teammate_header_offset_resets_on_a_new_map_session(self):
        from test_combat_model import DpsWindow
        window = object.__new__(DpsWindow)
        window.pvp_recording = self.controller
        window.config = {}
        window.hide_names = False
        window.pvp_visible_session_id = None
        window.pvp_outgoing_offset = window.pvp_incoming_offset = 3
        window.pvp_teammate_offset = 7
        window.model = SimpleNamespace(self_id=SELF_ID, party_active=False,
            party_ids=set(), provisional_party_ids=set(), friend_order=[],
            non_player_actor_ids=set(), entity_names={}, entity_professions={},
            actor_character_ids={}, entity_extraordinary_ratings={},
            local_player_name="", actor_profession_id=lambda _actor: 0)
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        window._pvp_layered_main_snapshot()
        self.assertEqual(window.pvp_teammate_offset, 0)
        self.assertEqual(window.pvp_outgoing_offset, 0)
        self.assertEqual(window.pvp_incoming_offset, 0)

    def test_late_identity_replays_first_hit_exactly_once(self):
        controller = PvpRecordingController(self.repository)
        controller.bind_account("late-account")
        controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        observation = self.observation(2, damage=345, self_id=0, self_token=None)
        observation["context"]["profiles"].append({"entity_id": SELF_ID, "entity_type": "Player", "name": "本人"})
        controller.observe(observation)
        self.assertFalse(controller.active)
        controller.ingest_update("identity", {"self_id": SELF_ID, "user_token": SELF_TOKEN})
        controller.observe(deepcopy(observation))
        self.assertEqual(controller.snapshot(BASE_NS + 3_000_000_000)["damage"], 345)

    def test_multiple_duels_death_and_idle_never_clear_or_end_map_match(self):
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        match_id = self.controller.recording["match_id"]
        self.controller.observe(self.observation(2, damage=200))
        for seconds, method, args in ((3, "OnMsgIndividualPVPResult", [1]), (4, "OnMsgIndividualPVPState", [3]),
                (6, "OnMsgEntityDead", [0, "enemy-token"]), (8, "OnMsgEntityRelive", []),
                (9, "OnMsgIndividualPVPState", [3])):
            observation = self.observation(seconds, method=method, damage=0)
            observation["record"]["decoded_arguments"] = args
            self.controller.observe(observation)
        self.controller.observe(self.observation(10, damage=300))
        state = self.controller.snapshot(BASE_NS + 100_000_000_000)
        self.assertTrue(state["active"])
        self.assertEqual(state["session_id"], match_id)
        self.assertEqual(state["time"], "01:39")
        self.assertEqual(state["damage"], 500)
        self.assertEqual(state["deaths"], 1)
        self.assertEqual(self.repository.list("account-a"), [])

    def test_old_observation_after_switch_or_exit_cannot_reopen_or_contaminate(self):
        first, second = 5200020, 5208002
        self.controller.ingest_scene(self.scene(first, 1))
        old = self.observation(5, damage=999)
        self.controller.ingest_scene(self.scene(second, 10))
        self.controller.observe(old)
        self.assertEqual(self.controller.snapshot(BASE_NS + 11_000_000_000)["damage"], 0)
        self.controller.ingest_scene(self.scene(0, 20, transition=True))
        self.controller.observe(old)
        self.assertFalse(self.controller.active)
        self.assertEqual(len(self.repository.list("account-a")), 2)

    def test_instance_arriving_after_scene_is_saved_in_match(self):
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        self.controller.observe(self.observation(2, instance_id="instance-a"))
        payload = self.controller.stop(BASE_NS + 3_000_000_000)
        self.assertEqual(payload["instance_id"], "instance-a")

    def test_scene_refresh_before_space_creation_never_attaches_old_wire_instance(self):
        from pvp_tracker import parser_observation
        from test_pvp_tracker import Pipeline, packet
        parser = Pipeline().parser
        parser.wire_map_id = 5_200_002
        parser.wire_instance_id = "old-space"
        map_id = next(iter(RECORDABLE_MAPS))
        # Use this test's chronology rather than the older pipeline fixture.
        record = packet("OnMsgRefreshSceneObjects", args=[map_id, {}, {}], sequence=55)
        record["capture_timestamp_ns"] = BASE_NS + 1_000_000_000
        observation = parser_observation(parser, record, [("scene", {"scene_id": map_id})])
        self.assertIsNone(observation["context"]["instance_id"])
        self.controller.ingest_scene(self.scene(map_id, 1))
        first_id = self.controller.recording["match_id"]
        self.controller.observe(observation)
        parser.wire_map_id = map_id
        parser.wire_instance_id = "new-space"
        record = packet("NpcapEntityCreated", sequence=56)
        record["capture_timestamp_ns"] = BASE_NS + 2_000_000_000
        observation = parser_observation(parser, record, [("instance_context", dict(map_id=map_id, instance_id="new-space"))])
        self.controller.observe(observation)
        self.assertEqual(self.controller.recording["match_id"], first_id)
        self.assertEqual(self.controller.recording["instance_id"], "new-space")
        self.assertEqual(self.repository.list("account-a"), [])

    def test_rebinding_same_account_cannot_finalize_a_live_checkpoint(self):
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        match_id = self.controller.recording["match_id"]
        self.controller.bind_account("account-a")
        self.assertTrue(self.controller.active)
        self.assertEqual(self.controller.recording["match_id"], match_id)
        self.assertEqual(self.repository.list("account-a"), [])

    def test_finalized_match_cannot_change_local_owner_or_content(self):
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        payload = self.controller.stop(BASE_NS + 3_000_000_000)
        with self.assertRaisesRegex(ValueError, "owner"):
            self.repository.save("account-b", payload)
        changed = deepcopy(payload)
        changed["damage"] = 999
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.repository.save("account-a", changed)

    def test_crash_recovers_last_checkpoint_as_unknown_without_live_equipment_overwrite(self):
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        self.controller.observe(self.observation(4, damage=456))
        self.controller.checkpoint(BASE_NS + 4_000_000_000)
        recovered = PvpRecordingController(self.repository)
        recovered.bind_account("account-a")
        records = self.repository.list("account-a")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["damage"], 456)
        self.assertEqual(records[0]["end_reason"], "interrupted")
        self.assertEqual(records[0]["result"], "未知")
        self.assertFalse(records[0]["capture_complete"])

    def test_five_lost_upload_acknowledgements_retry_without_duplicate_server_records(self):
        from pvp_backend import PvpBackendStore, initialize_pvp_schema
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        payload = self.controller.stop(BASE_NS + 3_000_000_000)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        initialize_pvp_schema(connection)
        store = PvpBackendStore(b"x" * 32)
        attempts = []
        def request(action, body):
            attempts.append(body["record"]["match_id"])
            result = store.upload(connection, "server-card", body["record"])
            if len(attempts) <= 5:
                raise TimeoutError("ACK lost")
            return dict(result, ok=True)
        worker = PvpUploadWorker(self.repository, request, lambda: "account-a")
        worker.bind_request("account-a", request)
        try:
            for now in (0, 3, 8, 17, 34, 67):
                self.assertTrue(worker.attempt_one(now))
            self.assertEqual(attempts, [payload["match_id"]] * 6)
            self.assertEqual(len(store.records(connection, "server-card")), 1)
            self.assertIsNone(self.repository.due("account-a", 1000))
            self.assertEqual(len(self.repository.list("account-a")), 1)
        finally:
            connection.close()

    def test_account_bound_upload_never_uses_new_accounts_session(self):
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        payload = self.controller.stop(BASE_NS + 3_000_000_000)
        principal = ["account-a"]
        request = Mock(return_value={"ok": True, "match_id": payload["match_id"]})
        worker = PvpUploadWorker(self.repository, Mock(side_effect=AssertionError("unbound session")), lambda: principal[0])
        def old_session(action, body):
            principal[0] = "account-b"
            return request(action, body)
        worker.bind_request("account-a", old_session)
        self.assertTrue(worker.attempt_one(0))
        self.assertEqual(request.call_count, 1)
        self.assertIsNotNone(self.repository.due("account-a", 3))
        self.assertFalse(worker.attempt_one(3))

    def test_unbound_uploader_never_falls_back_to_mutable_live_session(self):
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        self.controller.stop(BASE_NS + 3_000_000_000)
        request = Mock(side_effect=AssertionError("must not use mutable live request"))
        worker = PvpUploadWorker(self.repository, request, lambda: "account-a")
        worker.bind_request("account-a", Mock())
        worker.unbind_request()
        self.assertFalse(worker.attempt_one(0))
        self.assertFalse(request.called)

    def test_shutdown_queue_drains_final_hit_before_record_is_finalized(self):
        from test_combat_model import DpsWindow
        window = object.__new__(DpsWindow)
        window.pvp_recording = self.controller
        window.pvp_tracker = self.controller.current
        window.main_combat_mode = "pve"
        window.model = Mock()
        window.messages = queue.Queue()
        window.settlement_ui = Mock()
        window.settlement_experiment = Mock()
        window.pvp_close_timestamp_ns = BASE_NS + 10_000_000_000
        self.controller.ingest_scene(self.scene(next(iter(RECORDABLE_MAPS)), 1))
        window.messages.put(("pvp_observation", self.observation(9, damage=789)))
        window.messages.put(("pvp_observation", self.observation(11, damage=999)))
        drained = False
        for _attempt in range(10):
            if window._ingest_pending_capture_messages():
                drained = True
                break
        self.assertTrue(drained)
        payload = self.controller.stop(window.pvp_close_timestamp_ns, reason="application_exit")
        self.assertEqual(payload["damage"], 789)
        self.assertEqual(payload["duration_seconds"], 9)


class PvpEquipmentSnapshotOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = PvpHistoryRepository(
            Path(self.temporary.name) / "pvp.sqlite3"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_crash_recovery_reattaches_same_match_equipment(self):
        profile = self.profile()
        snapshot = profile["equipment_snapshot"]
        self.repository.save_equipment_snapshot(
            "account-a", build_equipment_snapshot_upload(profile)
        )
        target = profile["user_token"]
        self.repository.checkpoint("account-a", {
            "match_id": "pvp_" + "a" * 32,
            "map_id": 5200167,
            "started_at_ns": BASE_NS - 1,
            "ended_at_ns": BASE_NS + 1,
            "player": {"character_id": SELF_TOKEN},
            "opponents": [{"character_id": target, "equipment_snapshot": None}],
            "allies": [{"character_id": SELF_TOKEN}],
            "enemies": [{"character_id": target}],
        })

        recovered = self.repository.recover("account-a")

        self.assertEqual(recovered["opponents"][0]["equipment_snapshot"], snapshot)
        self.assertEqual(recovered["enemies"][0]["equipment_snapshot"], snapshot)

    @staticmethod
    def profile():
        return {
            "game_pid": 111,
            "capture_session_id": 222,
            "local_user_token": SELF_TOKEN,
            "party_session_id": 333,
            "member_count": 2,
            "user_token": "AQAAANANkGB8AAAA",
            "name": "对手",
            "profession_id": 1_200_003,
            "extraordinary_rating": 90_001,
            "source_method": "RetOtherRoleShapeData",
            "equipment_count": 1,
            "pvp_equipment_count": 1,
            "active_word_count": 1,
            "total_word_count": 1,
            "equipment_snapshot": {
                "captured_at_ns": BASE_NS,
                "captured_at": "2026-09-18T12:00:00+00:00",
                "extraordinary_rating": 90_001,
                "equipment_score": 98,
                "equipment": [{
                    "slot": 1,
                    "slot_name": "武器",
                    "item_id": 12345,
                    "item_score": 98,
                    "equipment_mode": "pvp",
                    "is_pvp": True,
                }],
                "source": "RetOtherRoleShapeData",
                "partial": False,
            },
        }

    def test_successful_profile_is_durable_and_uploaded_independently(self):
        payload = build_equipment_snapshot_upload(self.profile())
        snapshot_id = self.repository.save_equipment_snapshot(
            "account-a", payload
        )
        self.repository.save_equipment_snapshot("account-a", payload)
        calls = []

        def request(action, body):
            calls.append((action, body))
            return {"ok": True, "snapshot_id": snapshot_id}

        worker = PvpUploadWorker(
            self.repository, request, lambda: "account-a"
        )
        worker.bind_request("account-a", request)
        self.assertTrue(worker.attempt_one(0))
        self.assertEqual(calls[0][0], "equipment/snapshots/upload")
        self.assertEqual(
            calls[0][1]["snapshot"]["equipment_snapshot"]["equipment"][0]["slot_name"],
            "武器",
        )
        self.assertIsNone(self.repository.due_equipment_snapshot("account-a", 1))
        with self.repository.session() as connection:
            row = connection.execute(
                "SELECT upload_state,server_snapshot_id FROM "
                "pvp_equipment_snapshot_outbox WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        self.assertEqual(tuple(row), ("uploaded", snapshot_id))

    def test_exact_prior_equipment_snapshot_can_restore_pve_history(self):
        older = build_equipment_snapshot_upload(self.profile())
        self.repository.save_equipment_snapshot("account-a", older)
        changed_profile = deepcopy(self.profile())
        changed_profile["equipment_snapshot"]["captured_at_ns"] = BASE_NS + 20
        changed_profile["equipment_snapshot"]["captured_at"] = (
            "2026-09-18T12:00:00.000000020+00:00"
        )
        changed_profile["extraordinary_rating"] = 90_002
        changed_profile["equipment_snapshot"]["extraordinary_rating"] = 90_002
        changed_profile["equipment_snapshot"]["equipment"][0]["item_id"] = 54321
        newer = build_equipment_snapshot_upload(changed_profile)
        self.repository.save_equipment_snapshot("account-a", newer)

        snapshot = self.repository.equipment_snapshot_at_or_before(
            "account-a",
            self.profile()["user_token"],
            BASE_NS + 30,
            extraordinary_rating=90_001,
        )

        self.assertEqual(snapshot["equipment"][0]["item_id"], 12345)
        self.assertIsNone(
            self.repository.equipment_snapshot_at_or_before(
                "account-b",
                self.profile()["user_token"],
                BASE_NS + 30,
                extraordinary_rating=90_001,
            )
        )

    def test_equipment_history_lookup_uses_character_time_index(self):
        with self.repository.session() as connection:
            plan = connection.execute(
                """EXPLAIN QUERY PLAN SELECT payload_json
                   FROM pvp_equipment_snapshot_outbox
                   WHERE account_key=? AND target_character_id=?
                   AND captured_at_ns<=?
                   ORDER BY captured_at_ns DESC LIMIT 64""",
                ("account-a", SELF_TOKEN, BASE_NS),
            ).fetchall()
        self.assertIn(
            "idx_pvp_equipment_snapshot_lookup",
            " ".join(str(row[3]) for row in plan),
        )

    def test_busy_equipment_history_lookup_does_not_block_tk_for_seconds(self):
        connection = sqlite3.connect(self.repository.path, timeout=0)
        try:
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("BEGIN EXCLUSIVE")
            started = time.perf_counter()
            for index in range(50):
                self.assertIsNone(
                    self.repository.equipment_snapshot_at_or_before(
                        "account-a", f"{SELF_TOKEN}-{index}", BASE_NS,
                    )
                )
            self.assertLess(time.perf_counter() - started, 0.3)
        finally:
            connection.rollback()
            connection.close()

    def test_equipment_upload_never_crosses_accounts(self):
        payload = build_equipment_snapshot_upload(self.profile())
        self.repository.save_equipment_snapshot("account-a", payload)
        principal = ["account-a"]

        def old_session(_action, _body):
            principal[0] = "account-b"
            return {"ok": True, "snapshot_id": payload["snapshot_id"]}

        worker = PvpUploadWorker(
            self.repository, Mock(), lambda: principal[0]
        )
        worker.bind_request("account-a", old_session)
        self.assertTrue(worker.attempt_one(0))
        self.assertIsNotNone(
            self.repository.due_equipment_snapshot("account-a", 3)
        )
        self.assertFalse(worker.attempt_one(3))

    def test_ingest_immediately_queues_correlated_active_response(self):
        from test_combat_model import DpsWindow

        window = object.__new__(DpsWindow)
        window.worker = SimpleNamespace(equipment_session=lambda: (111, 222))
        window.model = SimpleNamespace(
            actor_character_ids={7: "AQAAANANkGB8AAAA"},
            self_id=1,
        )
        window._team_equipment_local_token = lambda: SELF_TOKEN
        window._team_equipment_party_session_id = lambda: 333
        window._current_team_equipment_tokens = lambda: {
            SELF_TOKEN,
            "AQAAANANkGB8AAAA",
        }
        window._main_extraordinary_rating = lambda _actor: 90_001
        window._invalidate_team_rating_preview_rows = Mock()
        window.team_equipment_profiles = {}
        window.team_equipment_requested_ratings = {
            "AQAAANANkGB8AAAA": None
        }
        window.team_equipment_attempt_ratings = {
            "AQAAANANkGB8AAAA": None
        }
        window.team_equipment_profile_ratings = {}
        window.team_equipment_requested_at = {
            "AQAAANANkGB8AAAA": 1.0
        }
        window.pvp_history_repository = self.repository
        window.pvp_recording = SimpleNamespace(account_key="account-a")
        window.pvp_upload_worker = PvpUploadWorker(
            self.repository, Mock(), lambda: 'account-a'
        )
        window.settlement_experiment = Mock()

        self.assertTrue(window._ingest_team_equipment_profile(self.profile()))
        self.assertTrue(window.pvp_upload_worker.finish_equipment_snapshots())
        self.assertEqual(
            window.team_equipment_profile_ratings["AQAAANANkGB8AAAA"],
            90_001,
        )
        self.assertEqual(
            window.team_equipment_attempt_ratings["AQAAANANkGB8AAAA"],
            90_001,
        )
        queued = self.repository.due_equipment_snapshot("account-a", 0)
        self.assertIsNotNone(queued)

    def test_many_equipment_snapshots_persist_off_the_ui_thread(self):
        from copy import deepcopy
        import threading

        write_threads = []
        save = self.repository.save_equipment_snapshots
        def observed_save(entries):
            write_threads.append(threading.get_ident())
            return save(entries)

        self.repository.save_equipment_snapshots = observed_save
        worker = PvpUploadWorker(
            self.repository, Mock(), lambda: 'account-b'
        )
        ui_thread = threading.get_ident()
        for index in range(100):
            profile = deepcopy(self.profile())
            profile['user_token'] = f'actor-{index:03d}'
            profile['equipment_snapshot']['captured_at_ns'] += index
            self.assertTrue(worker.enqueue_equipment_snapshot('account-a', profile))

        self.assertTrue(worker.finish_equipment_snapshots())
        self.assertTrue(write_threads)
        self.assertTrue(all(thread != ui_thread for thread in write_threads))
        with self.repository.session() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM pvp_equipment_snapshot_outbox "
                "WHERE account_key='account-a'"
            ).fetchone()[0]
        self.assertEqual(count, 100)
        self.assertFalse(worker.enqueue_equipment_snapshot('account-a', self.profile()))

    def test_finalized_equipment_attachments_are_batched_off_the_ui_thread(self):
        from copy import deepcopy
        import threading

        members = [
            {'character_id': f'actor-{index:03d}', 'equipment_snapshot': None}
            for index in range(100)
        ]
        self.repository.save('account-a', {
            'match_id': 'many-players',
            'started_at_ns': BASE_NS,
            'end_reason': 'match_result',
            'player': {'character_id': SELF_TOKEN},
            'allies': members,
            'enemies': [],
            'teams': {'allies': deepcopy(members), 'enemies': []},
        })
        write_threads = []
        attach = self.repository.attach_equipment_snapshots
        def observed_attach(*args):
            write_threads.append(threading.get_ident())
            return attach(*args)

        self.repository.attach_equipment_snapshots = observed_attach
        worker = PvpUploadWorker(
            self.repository, Mock(), lambda: 'account-a'
        )
        ui_thread = threading.get_ident()
        for index in range(100):
            self.assertTrue(worker.enqueue_equipment_attachment(
                'account-a', 'many-players', f'actor-{index:03d}',
                {'captured_at_ns': BASE_NS + index, 'equipment': [{'slot': 1}]},
            ))

        self.assertTrue(worker.finish_equipment_snapshots())
        self.assertTrue(write_threads)
        self.assertTrue(all(thread != ui_thread for thread in write_threads))
        saved = self.repository.list('account-a')[0]
        self.assertEqual(
            sum(bool(row['equipment_snapshot']) for row in saved['allies']),
            100,
        )
        self.assertEqual(
            sum(bool(row['equipment_snapshot']) for row in saved['teams']['allies']),
            100,
        )

    def test_finalized_controller_uses_background_attachment_when_bound(self):
        controller = PvpRecordingController(self.repository)
        controller.account_key = 'account-a'
        controller.finalized_equipment_match_id = 'many-players'
        controller.finalized_equipment_tokens = {'actor-001'}
        controller.equipment_attach = Mock(return_value=True)
        snapshot = {'captured_at_ns': BASE_NS, 'equipment': []}

        self.assertTrue(controller._attach_finalized_equipment_profile({
            'user_token': 'actor-001', 'equipment_snapshot': snapshot,
        }))
        controller.equipment_attach.assert_called_once_with(
            'account-a', 'many-players', 'actor-001', snapshot,
        )


if __name__ == "__main__":
    unittest.main()
