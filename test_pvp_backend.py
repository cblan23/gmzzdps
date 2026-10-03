"""Server-side PVP idempotency, privacy and alliance regressions."""

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from pvp_backend import (
    PvpBackendError,
    PvpBackendStore,
    initialize_pvp_schema,
)
from profile_upload import character_hash


def character_token(number):
    raw = b"\x01\x00\x00\x00" + int(number).to_bytes(8, "little")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def record(match_id="pvp_" + "1" * 32, character_id=None, damage=1200):
    character_id = character_id or character_token(1001)
    start = 1_789_700_000_000_000_000
    end = start + 90_000_000_000
    return {
        "schema_version": 1,
        "match_id": match_id,
        "map_id": 5_200_223,
        "mode_id": 5_500_009,
        "mode_name": "四方联赛",
        "map_name": "四方联赛",
        "instance_id": "instance-a",
        "started_at_ns": start,
        "ended_at_ns": end,
        "started_at": "2026-09-18T12:00:00+00:00",
        "ended_at": "2026-09-18T12:01:30+00:00",
        "duration_seconds": 90,
        "result": "未知",
        "end_reason": "left_map",
        "player": {
            "character_id": character_id,
            "name": "本人",
            "profession_id": 1_200_001,
            "level": 80,
            "extraordinary_rating": 88_893,
            "avatar_id": 4_270_019,
            "avatar_frame_id": 4_271_009,
            "equipment_snapshot": None,
        },
        "kills": 1,
        "assists": None,
        "deaths": 2,
        "damage": damage,
        "taken": None,
        "capture_complete": True,
        "assist_source_available": False,
        "incoming_source_available": False,
        "opponents": [],
    }


def equipment_snapshot(snapshot_id="pvp_eq_" + "1" * 32):
    captured_at_ns = 1_789_700_000_000_000_000
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "owner_character_id": character_token(1001),
        "target_character_id": character_token(2001),
        "target_name": "对手",
        "profession_id": 1_200_003,
        "extraordinary_rating": 90_001,
        "captured_at_ns": captured_at_ns,
        "captured_at": "2026-09-18T12:00:00+00:00",
        "source": "RetOtherRoleShapeData",
        "equipment_snapshot": {
            "captured_at_ns": captured_at_ns,
            "captured_at": "2026-09-18T12:00:00+00:00",
            "extraordinary_rating": 90_001,
            "equipment_score": 98,
            "equipment_score_source": "wire_item_score_sum",
            "equipment": [{
                "slot": 1,
                "slot_name": "武器",
                "item_id": 12345,
                "word_ids": [501],
                "auxiliary_id": 77001,
                "word_score": 88,
                "item_score": 98,
                "base_score": 10,
                "random_score": 8,
                "enhance_score": 80,
                "enhance_level": 1,
                "enhance_completed_level": 1,
                "enhance_stage_count": 8,
                "enhance_stages": [
                    {"stage": 1, "level": 10, "percent": 100}
                ],
                "grow_body_id": 101,
                "enhance_schedule": [
                    {"level": 1, "score": 80, "cumulative_score": 80},
                    {"level": 2, "score": 80, "cumulative_score": 160},
                ],
                "enhance_completed_score": 80,
                "enhance_level_score": 80,
                "enhance_level_remaining": 0,
                "enhance_level_progress_percent": 100,
                "enhance_overall_percent": 12,
                "enhance_progress_percent": 12,
                "next_enhance_level": 2,
                "next_enhance_score": 160,
                "next_enhance_increment": 80,
                "next_enhance_remaining": 80,
                "known_score": 18,
                "total_score": 98,
                "score_complete": True,
                "score_source": "local_equipment_model",
                "word_scores": [88],
                "affixes": [{
                    "word_id": 501,
                    "name": "攻击力",
                    "property_key": "Atk_N",
                    "property_value": 312,
                    "score": 88,
                }],
                "special_affix": {
                    "auxiliary_id": 109,
                    "name": "特殊攻击词条",
                    "score": 1_200,
                    "passive_skill_ids": [80_003_002],
                },
                "score_breakdown_total": 1_288,
                "score_breakdown_complete": True,
                "score_breakdown_matches": True,
                "details": {"quality": 5, "raw": [1, 2, 3]},
                "equipment_mode": "pvp",
                "is_pvp": True,
                "word_class_types": [1],
                "active_word_count": 1,
                "total_word_count": 1,
                "metadata": {"mode": "pvp", "quality": 5},
            }],
            "gems": None,
            "attributes": {"health": 1234},
            "sets": None,
            "affixes": None,
            "pvp_equipment_count": 1,
            "active_word_count": 1,
            "total_word_count": 1,
            "source": "RetOtherRoleShapeData",
            "partial": False,
        },
    }


class PvpBackendTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        initialize_pvp_schema(self.connection)
        self.store = PvpBackendStore(b"h" * 32, now=lambda: 1_800_000_000.0)

    def tearDown(self):
        self.connection.close()

    def test_hunter_large_observed_roster_and_legacy_outdoor_duel_upload(self):
        hunter = record(match_id="pvp_" + "a" * 32)
        hunter.update(
            map_id=5200167, mode_id=5500012, mode_name="终末猎杀",
            map_name="猎龙之城",
            allies=[{"character_id": hunter["player"]["character_id"], "is_self": True}],
            enemies=[{"character_id": character_token(3000 + index)}
                     for index in range(58)],
        )
        self.store.upload(self.connection, "card-a", hunter)
        self.assertEqual(len(self.store.records(self.connection, "card-a")[0]["enemies"]), 58)

        duel = record(match_id="pvp_" + "b" * 32)
        duel.update(map_id=5200002, match_kind="duel", mode_name="双人切磋")
        self.store.upload(self.connection, "card-a", duel)

        unrelated = record(match_id="pvp_" + "c" * 32)
        unrelated["map_id"] = 5200002
        with self.assertRaises(PvpBackendError):
            self.store.upload(self.connection, "card-a", unrelated)

    def test_four_faction_roster_accepts_sixty_allies_and_three_enemy_factions(self):
        payload = record(match_id="pvp_" + "f" * 32)
        payload["team_size"] = 60
        payload["allies"] = [
            {"character_id": character_token(10_000 + index),
             "is_self": index == 0}
            for index in range(60)
        ]
        payload["allies"][0]["character_id"] = payload["player"]["character_id"]
        payload["enemies"] = [
            {"character_id": character_token(20_000 + index)}
            for index in range(180)
        ]
        self.store.upload(self.connection, "card-a", payload)
        saved = self.store.records(self.connection, "card-a")[0]
        self.assertEqual(saved["team_size"], 60)
        self.assertEqual(len(saved["allies"]), 60)
        self.assertEqual(len(saved["enemies"]), 180)

    def test_hunter_dragon_damage_stays_separate_from_player_damage(self):
        payload = record(match_id="pvp_" + "d" * 32)
        payload.update(
            map_id=5200167, mode_name="终末猎杀", map_name="猎龙之城",
            damage=1200, damage_done=1200,
            monsters=[{
                "template_id": 7100625, "name": "战争巨龙",
                "damage_to": 4500, "damage_from": 300,
                "defeated": True,
                "skills_outgoing": [{"skill_id": 42, "damage": 4500, "hits": 3}],
                "skills_incoming": [{"skill_id": 43, "damage": 300, "hits": 1}],
            }],
        )

        self.store.upload(self.connection, "card-a", payload)
        saved = self.store.records(self.connection, "card-a")[0]

        self.assertEqual(saved["damage"], 1200)
        self.assertEqual(saved["monster_damage"], 4500)
        self.assertEqual(saved["monster_taken"], 300)
        self.assertTrue(saved["monsters"][0]["defeated"])
        self.assertEqual(saved["monsters"][0]["skills_outgoing"][0]["hits"], 3)

    def test_upload_is_idempotent_and_conflicting_content_is_rejected(self):
        payload = record()
        first = self.store.upload(self.connection, "card-a", payload)
        second = self.store.upload(self.connection, "card-a", payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["upload_count"], 2)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM pvp_matches").fetchone()[0],
            1,
        )
        saved_player = self.store.records(self.connection, "card-a")[0]["player"]
        self.assertEqual(
            (saved_player["level"], saved_player["avatar_id"], saved_player["avatar_frame_id"]),
            (80, 4_270_019, 4_271_009),
        )

        changed = json.loads(json.dumps(payload))
        changed["damage"] += 1
        with self.assertRaisesRegex(PvpBackendError, "绑定其他内容"):
            self.store.upload(self.connection, "card-a", changed)

    def test_estimated_healing_keeps_source_in_server_record(self):
        payload = record()
        payload["healing"] = 946
        payload["healing_source"] = "hp_balance_estimate"
        payload["allies"] = [{
            "character_id": payload["player"]["character_id"],
            "name": "本人", "is_self": True,
            "healing": 946, "healing_source": "hp_balance_estimate",
        }]
        self.store.upload(self.connection, "card-a", payload)
        saved = self.store.records(self.connection, "card-a")[0]
        self.assertEqual((saved["healing"], saved["healing_source"]),
                         (946, "hp_balance_estimate"))
        self.assertEqual(saved["allies"][0]["healing_source"], "hp_balance_estimate")

    def test_equipment_snapshot_upload_is_idempotent_and_keeps_full_details(self):
        payload = equipment_snapshot()
        payload["equipment_snapshot"]["equipment"][0]["item_name"] = "裂金之战刃"
        first = self.store.upload_equipment_snapshot(
            self.connection, "card-a", payload
        )
        second = self.store.upload_equipment_snapshot(
            self.connection, "card-a", payload
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["upload_count"], 2)
        row = self.connection.execute(
            "SELECT * FROM pvp_equipment_snapshots WHERE snapshot_id=?",
            (payload["snapshot_id"],),
        ).fetchone()
        self.assertEqual(row["target_name"], "对手")
        self.assertEqual(row["extraordinary_rating"], 90_001)
        self.assertNotEqual(row["target_character_hash"], character_token(2001))
        saved = json.loads(row["payload_json"])
        item = saved["equipment_snapshot"]["equipment"][0]
        self.assertEqual(item["slot_name"], "武器")
        self.assertEqual(item["item_name"], "裂金之战刃")
        self.assertEqual(item["item_score"], 98)
        self.assertEqual(item["enhance_level"], 1)
        self.assertEqual(item["enhance_completed_level"], 1)
        self.assertEqual(item["enhance_score"], 80)
        self.assertEqual(item["enhance_overall_percent"], 12)
        self.assertEqual(item["next_enhance_level"], 2)
        self.assertEqual(item["next_enhance_score"], 160)
        self.assertEqual(item["affixes"][0]["score"], 88)
        self.assertEqual(item["affixes"][0]["name"], "攻击力")
        self.assertEqual(item["special_affix"]["score"], 1_200)
        self.assertEqual(item["score_breakdown_total"], 1_288)
        self.assertTrue(item["score_breakdown_matches"])
        self.assertEqual(item["details"]["raw"], [1, 2, 3])

        changed = json.loads(json.dumps(payload))
        changed["extraordinary_rating"] = 90_002
        with self.assertRaises(PvpBackendError):
            self.store.upload_equipment_snapshot(
                self.connection, "card-a", changed
            )

    def test_equipment_snapshot_upload_keeps_latest_two_per_target(self):
        base_stamp = 1_789_700_000_000_000_000
        snapshots = []
        for index in range(3):
            payload = equipment_snapshot(
                "pvp_eq_" + str(index + 1) * 32
            )
            captured_at_ns = base_stamp + index
            payload["captured_at_ns"] = captured_at_ns
            payload["equipment_snapshot"]["captured_at_ns"] = captured_at_ns
            self.store.upload_equipment_snapshot(
                self.connection, "card-a", payload
            )
            snapshots.append(payload["snapshot_id"])

        saved = self.connection.execute(
            """SELECT snapshot_id FROM pvp_equipment_snapshots
               ORDER BY captured_at_ns DESC"""
        ).fetchall()
        self.assertEqual(
            [row["snapshot_id"] for row in saved],
            [snapshots[2], snapshots[1]],
        )

    def test_matchups_aggregate_by_uid_and_ignore_unknown_in_win_rate(self):
        first = record(match_id="pvp_" + "2" * 32)
        first.update(
            team_size=3,
            result="胜利",
            opponents=[{
                "opponent_id": "enemy-a",
                "character_id": character_token(2001),
                "name": "敌方",
                "damage": 0,
                "taken": 0,
                "kills": 0,
                "defeats": 0,
                "assists": 0,
            }],
        )
        second = record(match_id="pvp_" + "3" * 32)
        second.update(
            team_size=3,
            result="失败",
            opponents=[{
                "opponent_id": "enemy-a",
                "character_id": character_token(2001),
                "name": "敌方",
                "damage": 0,
                "taken": 0,
                "kills": 0,
                "defeats": 0,
                "assists": 0,
            }],
        )
        third = record(match_id="pvp_" + "4" * 32)
        third.update(
            team_size=3,
            result="未知",
            opponents=[{
                "opponent_id": "enemy-a",
                "character_id": character_token(2001),
                "name": "敌方",
                "damage": 0,
                "taken": 0,
                "kills": 0,
                "defeats": 0,
                "assists": 0,
            }],
        )
        for payload in (first, second, third):
            self.store.upload(self.connection, "card-a", payload)
        result = self.store.matchups(
            self.connection,
            "card-a",
            [character_token(2001)],
            3,
        )
        item = result[character_token(2001)]
        self.assertEqual(
            (item["total"], item["wins"], item["losses"], item["unknown"]),
            (3, 1, 1, 1),
        )
        self.assertEqual(item["win_rate"], 0.5)

    def test_matchups_excludes_current_match(self):
        payload = record()
        payload.update(
            team_size=1,
            match_kind="duel",
            map_id=0,
            result="胜利",
            opponents=[{
                "opponent_id": "enemy-a",
                "character_id": character_token(2001),
                "name": "敌方",
                "damage": 0,
                "taken": 0,
                "kills": 0,
                "defeats": 0,
                "assists": 0,
            }],
        )
        self.store.upload(self.connection, "card-a", payload)
        result = self.store.matchups(
            self.connection,
            "card-a",
            [character_token(2001)],
            1,
            exclude_match_id=payload["match_id"],
        )
        self.assertEqual(result[character_token(2001)]["total"], 0)

    def test_full_team_snapshots_round_trip_only_confirmed_optional_values(self):
        payload = record()
        payload.update(
            team_size=6,
            data_scope={
                "roster": "verified",
                "self_metrics": "self_exact",
                "team_metrics": "authoritative",
                "opponent_interactions": "pair_exact",
            },
            allies=[
                dict(character_id=payload["player"]["character_id"], name="本人",
                     profession_id=1_200_001, extraordinary_rating=88_893,
                     is_self=True, is_ai=False, kills=1, damage=1_200,
                     healing=None, taken=None),
                dict(character_id=character_token(1002), name="队友",
                     profession_id=1_200_002, extraordinary_rating=None,
                     is_self=False, is_ai=False, kills=None, damage=None,
                     healing=None, taken=None),
            ],
            enemies=[
                dict(character_id=character_token(2001), name="敌方",
                     profession_id=1_200_003, extraordinary_rating=91_248,
                     is_self=False, is_ai=False, kills=None, damage=None,
                     healing=None, taken=None),
            ],
        )
        payload["teams"] = {
            "allies": [
                dict(character_id=payload["player"]["character_id"], name="本人",
                     damage_done=1_200, healing_done=300),
            ],
            "enemies": [
                dict(character_id=character_token(2001), name="敌方",
                     damage_taken=1_200),
            ],
        }
        self.store.upload(self.connection, "card-a", payload)
        saved = self.store.records(self.connection, "card-a")[0]
        self.assertEqual(saved["team_size"], 6)
        self.assertEqual(saved["allies"][1]["name"], "队友")
        self.assertIsNone(saved["allies"][1]["damage"])
        self.assertEqual(saved["enemies"][0]["extraordinary_rating"], 91_248)
        self.assertEqual(saved["teams"]["allies"][0]["damage"], 1_200)
        self.assertEqual(saved["teams"]["allies"][0]["healing"], 300)
        self.assertEqual(saved["teams"]["enemies"][0]["taken"], 1_200)

    def test_skill_times_team_metrics_and_equipment_words_survive_upload(self):
        payload = record()
        stamp = payload["started_at_ns"] + 1_000_000_000
        snapshot = {
            "captured_at_ns": stamp,
            "extraordinary_rating": 91_248,
            "equipment_score": 728,
            "pvp_equipment_count": 8,
            "active_word_count": 4,
            "total_word_count": 10,
            "equipment": [
                {
                    "slot": 1,
                    "item_id": 12_345,
                    "word_ids": [501, 502],
                    "auxiliary_id": 77,
                    "word_score": 93,
                    "equipment_mode": "pvp",
                    "is_pvp": True,
                    "word_class_types": [1, 2],
                    "active_word_count": 1,
                    "total_word_count": 2,
                    "metadata": {"quality": 5, "icon": "weapon/icon"},
                }
            ],
            "affixes": [{"name": "竞技增伤", "score": 93}],
            "source": "RetOtherRoleShapeData",
            "partial": False,
        }
        skill = {
            "skill_id": 9001,
            "name": "测试技能",
            "hits": 2,
            "casts": 1,
            "critical_hits": 1,
            "damage": 1_200,
            "max_hit": 700,
            "share": 1.0,
            "hit_timestamps_ns": [stamp, stamp + 2_000_000_000],
            "cast_timestamps_ns": [stamp - 500_000_000],
        }
        payload.update(
            team_size=6,
            data_scope={
                "roster": "verified",
                "self_metrics": "self_exact",
                "team_metrics": "authoritative",
                "opponent_interactions": "pair_exact",
            },
            allies=[
                {
                    "character_id": payload["player"]["character_id"],
                    "name": "本人",
                    "profession_id": 1_200_001,
                    "extraordinary_rating": 88_893,
                    "is_self": True,
                    "kills": 1,
                    "assists": 3,
                    "deaths": 2,
                    "damage": 1_200,
                    "healing": 300,
                    "taken": 900,
                    "current_dead": True,
                    "metrics_scope": "self_exact",
                    "skills_scope": "self_exact",
                    "skills": [skill],
                    "equipment_snapshot": snapshot,
                }
            ],
            enemies=[
                {
                    "character_id": character_token(2001),
                    "name": "敌方",
                    "profession_id": 1_200_003,
                    "extraordinary_rating": 91_248,
                    "metrics_scope": "authoritative",
                    "skills_scope": "authoritative",
                    "statistics_authoritative": True,
                    "skills": [skill],
                    "equipment_snapshot": snapshot,
                }
            ],
            opponents=[
                {
                    "opponent_id": "enemy-a",
                    "character_id": character_token(2001),
                    "name": "敌方",
                    "kills": 1,
                    "defeats": 2,
                    "damage": 1_200,
                    "taken": 900,
                    "interaction_scope": "pair_exact",
                    "skills_outgoing": [skill],
                    "skills_incoming": [skill],
                    "timeline": [{"type": "kill", "timestamp_ns": stamp}],
                    "equipment_snapshot": snapshot,
                }
            ],
        )
        payload["player"]["equipment_snapshot"] = snapshot

        self.store.upload(self.connection, "card-a", payload)
        saved = self.store.records(self.connection, "card-a")[0]

        saved_skill = saved["allies"][0]["skills"][0]
        self.assertEqual(saved_skill["casts"], 1)
        self.assertEqual(saved_skill["critical_hits"], 1)
        self.assertEqual(saved_skill["hit_timestamps_ns"], skill["hit_timestamps_ns"])
        self.assertEqual(saved_skill["cast_timestamps_ns"], skill["cast_timestamps_ns"])
        self.assertEqual(saved["allies"][0]["assists"], 3)
        self.assertEqual(saved["allies"][0]["deaths"], 2)
        self.assertTrue(saved["allies"][0]["current_dead"])
        self.assertEqual(saved["allies"][0]["metrics_scope"], "self_exact")
        self.assertEqual(saved["allies"][0]["skills_scope"], "self_exact")
        self.assertTrue(saved["enemies"][0]["statistics_authoritative"])
        self.assertEqual(saved["opponents"][0]["interaction_scope"], "pair_exact")
        self.assertEqual(saved["data_scope"]["team_metrics"], "authoritative")
        item = saved["enemies"][0]["equipment_snapshot"]["equipment"][0]
        self.assertEqual(item["word_ids"], [501, 502])
        self.assertEqual(item["word_class_types"], [1, 2])
        self.assertEqual(item["word_score"], 93)
        self.assertEqual(item["equipment_mode"], "pvp")
        self.assertEqual(
            saved["opponents"][0]["equipment_snapshot"]["affixes"][0]["score"],
            93,
        )

    def test_personal_history_is_private_to_uploading_card(self):
        self.store.upload(self.connection, "card-a", record())
        self.assertEqual(len(self.store.records(self.connection, "card-a")), 1)
        self.assertEqual(self.store.records(self.connection, "card-b"), [])

    def test_requested_match_and_opponent_aliases_are_preserved_and_queryable(self):
        payload = record()
        payload.update({
            'battle_id': payload['match_id'],
            'start_time': payload['started_at'],
            'end_time': payload['ended_at'],
            'damage_done': payload['damage'],
            'damage_taken': payload['taken'],
            'series_id': 'series-a',
            'event_id': 'event-a',
            'war_id': 'war-a',
        })
        payload['opponents'] = [{
            'opponent_id': 'enemy-a', 'name': '对手', 'kills': 1,
            'assists': 0, 'defeats': 2, 'damage': 1200, 'taken': 900,
            'damage_to': 1200, 'damage_from': 900, 'kills_on': 1,
            'deaths_to': 2, 'damage_share': 1.0,
        }]
        self.store.upload(self.connection, 'card-a', payload)
        saved = self.store.records(self.connection, 'card-a')[0]
        self.assertEqual(saved['battle_id'], saved['match_id'])
        self.assertEqual((saved['damage_done'], saved['damage_taken']), (1200, None))
        opponent = saved['opponents'][0]
        self.assertEqual(
            (opponent['damage_to'], opponent['damage_from'], opponent['kills_on'], opponent['deaths_to']),
            (1200, 900, 1, 2),
        )
        row = self.connection.execute(
            'SELECT mode_id,damage_done,damage_taken,series_id,event_id,war_id FROM pvp_matches'
        ).fetchone()
        self.assertEqual(tuple(row), (5_500_009, 1200, None, 'series-a', 'event-a', 'war-a'))
        opponent_row = self.connection.execute(
            'SELECT opponent_id,damage_to,damage_from,kills_on,deaths_to FROM pvp_opponents'
        ).fetchone()
        self.assertEqual(tuple(opponent_row), ('enemy-a', 1200, 900, 1, 2))

    def test_invalid_non_finite_and_fractional_integer_fields_are_rejected(self):
        for field, value in (("duration_seconds", float("nan")), ("kills", 1.5),
                             ("damage", float("inf"))):
            payload = record(match_id="pvp_" + (field[0] * 32))
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(PvpBackendError):
                self.store.upload(self.connection, "card-a", payload)

    def test_unrecognized_data_source_markers_are_rejected(self):
        payload = record(match_id="pvp_" + "e" * 32)
        payload["data_scope"] = {"team_metrics": "client_says_so"}
        with self.assertRaises(PvpBackendError):
            self.store.upload(self.connection, "card-a", payload)

        payload = record(match_id="pvp_" + "f" * 32)
        payload["allies"] = [{
            "character_id": payload["player"]["character_id"],
            "metrics_scope": "guessed",
        }]
        with self.assertRaises(PvpBackendError):
            self.store.upload(self.connection, "card-a", payload)

    def test_verified_outdoor_duel_is_uploadable_but_arbitrary_zero_map_is_not(self):
        payload = record()
        payload.update({
            'map_id': 0, 'mode_id': 0, 'mode_name': '双人切磋',
            'map_name': '双人切磋', 'match_kind': 'duel',
        })
        self.store.upload(self.connection, 'card-a', payload)
        invalid = record(match_id='pvp_' + 'd' * 32)
        invalid['map_id'] = 0
        with self.assertRaisesRegex(PvpBackendError, '自动上传范围'):
            self.store.upload(self.connection, 'card-a', invalid)

    def test_preview_database_schema_is_upgraded_in_place(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('''CREATE TABLE pvp_matches (
                match_id TEXT PRIMARY KEY, uploader_card_hash TEXT NOT NULL,
                player_hash TEXT NOT NULL, player_name TEXT NOT NULL,
                profession_id INTEGER NOT NULL DEFAULT 0,
                map_id INTEGER NOT NULL, mode_name TEXT NOT NULL, map_name TEXT NOT NULL,
                started_at REAL NOT NULL, ended_at REAL NOT NULL,
                result TEXT NOT NULL, kills INTEGER NOT NULL, assists INTEGER,
                deaths INTEGER NOT NULL, extraordinary_rating INTEGER,
                payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
                created_at REAL NOT NULL, last_uploaded_at REAL NOT NULL,
                upload_count INTEGER NOT NULL DEFAULT 1
            )''')
            initialize_pvp_schema(connection)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(pvp_matches)')}
            self.assertTrue({'mode_id', 'damage_done', 'damage_taken', 'series_id', 'event_id', 'war_id'} <= columns)
            self.store.upload(connection, 'card-a', record())
            row = connection.execute('SELECT mode_id,damage_done FROM pvp_matches').fetchone()
            self.assertEqual(tuple(row), (5_500_009, 1200))
        finally:
            connection.close()

    def test_uncertified_user_cannot_manage_members(self):
        self.assertEqual(
            self.store.alliance(self.connection, "ordinary-card"),
            {"authorized": False},
        )
        with self.assertRaisesRegex(PvpBackendError, "管理权限"):
            self.store.member_mutation(
                self.connection,
                "ordinary-card",
                "upsert",
                {
                    "character_id": character_token(1001),
                    "character_name": "本人",
                },
            )

    def test_admin_can_add_edit_remove_and_aggregate_member_records(self):
        character_id = character_token(1001)
        self.store.certify(
            self.connection,
            "club_001",
            "星空远征团",
            "测试服",
            "admin-card",
        )
        self.store.member_mutation(
            self.connection,
            "admin-card",
            "upsert",
            {
                "character_id": character_id,
                "character_name": "本人",
                "profession_id": 1_200_001,
                "club_role": "核心成员",
            },
        )
        self.store.upload(
            self.connection,
            "member-card",
            record(character_id=character_id),
        )
        status = self.store.alliance(self.connection, "admin-card")
        self.assertTrue(status["authorized"])
        self.assertEqual(len(status["members"]), 1)
        self.assertEqual(status["members"][0]["matches"], 1)
        self.assertEqual(status["members"][0]["kills"], 1)
        self.assertEqual(status["members"][0]["deaths"], 2)
        self.assertIsNone(status["members"][0]["assists"])

        self.store.member_mutation(
            self.connection,
            "admin-card",
            "upsert",
            {
                "character_id": character_id,
                "character_name": "本人新名",
                "profession_id": 1_200_002,
                "club_role": "副盟主",
            },
        )
        updated = self.store.alliance(self.connection, "admin-card")["members"][0]
        self.assertEqual(updated["character_name"], "本人新名")
        self.assertEqual(updated["club_role"], "副盟主")

        removed = self.store.member_mutation(
            self.connection,
            "admin-card",
            "remove",
            {"character_id": character_id},
        )
        self.assertEqual(removed["changed"], 1)
        self.assertEqual(self.store.alliance(self.connection, "admin-card")["members"], [])

    def test_member_history_requires_same_authorized_alliance(self):
        character_id = character_token(2001)
        self.store.certify(self.connection, "club_a", "甲盟", "测试服", "admin-a")
        self.store.certify(self.connection, "club_b", "乙盟", "测试服", "admin-b")
        self.store.member_mutation(self.connection, "admin-a", "upsert", {
            "character_id": character_id, "character_name": "甲成员", "profession_id": 1_200_001})
        member_key = self.store.alliance(self.connection, "admin-a")["members"][0]["member_key"]
        self.store.upload(self.connection, "member-card", record(
            match_id="pvp_" + "2" * 32, character_id=character_id))
        self.assertEqual(len(self.store.member_records(self.connection, "admin-a", member_key)), 1)
        with self.assertRaisesRegex(PvpBackendError, "不属于"):
            self.store.member_records(self.connection, "admin-b", member_key)

    def test_hunter_city_analysis_uses_only_authorized_members_and_exact_skill_rows(self):
        start = record()["started_at_ns"] / 1e9
        self.store.now = lambda: start + 2 * 86400
        self.store.certify(self.connection, "club_a", "甲盟", "测试服", "admin-a")
        self.store.certify(self.connection, "club_b", "乙盟", "测试服", "admin-b")
        for admin, uid, name in (
            ("admin-a", character_token(1001), "甲一"),
            ("admin-a", character_token(1002), "甲二"),
            ("admin-b", character_token(1003), "乙一"),
        ):
            self.store.member_mutation(self.connection, admin, "upsert", {
                "character_id": uid, "character_name": name,
            })
        first = record(character_id=character_token(1001))
        first.update(map_id=5200131, mode_id=5500008, mode_name="猎城战",
                     map_name="猎龙之城", taken=300)
        first["opponents"] = [{
            "opponent_id": "enemy-a", "damage": 800, "taken": 300,
            "kills": 1, "defeats": 2,
            "skills_outgoing": [{"skill_id": 42, "name": "技能甲", "hits": 2,
                                 "damage": 800, "max_hit": 500}],
            "skills_incoming": [{"skill_id": 43, "name": "技能乙", "hits": 1,
                                 "damage": 300, "max_hit": 300},
                                {"skill_id": 99, "name": "未定向施法", "casts": 2}],
        }]
        second = record(match_id="pvp_" + "2" * 32,
                        character_id=character_token(1002), damage=450)
        second.update(map_id=5200167, mode_id=5500012, mode_name="终末猎杀",
                      map_name="猎龙之城", kills=2, deaths=0,
                      capture_complete=False)
        other_map = record(match_id="pvp_" + "3" * 32,
                           character_id=character_token(1001))
        other_club = record(match_id="pvp_" + "4" * 32,
                            character_id=character_token(1003))
        other_club.update(map_id=5200131, mode_id=5500008,
                          mode_name="猎城战", map_name="猎龙之城")
        for payload in (first, second, other_map, other_club):
            self.store.upload(self.connection, "member-card", payload)

        analysis = self.store.hunter_city_analysis(self.connection, "admin-a", 7)
        self.assertEqual(analysis["summary"], {
            "matches": 2, "members": 2, "kills": 3, "deaths": 2,
            "damage": 1650, "taken": 300, "taken_matches": 1,
            "complete_matches": 1,
        })
        self.assertEqual({row["map_id"] for row in analysis["records"]},
                         {5200131, 5200167})
        self.assertEqual((analysis["skills_outgoing"][0]["skill_id"],
                          analysis["skills_outgoing"][0]["damage"]), (42, 800))
        self.assertEqual((analysis["skills_incoming"][0]["skill_id"],
                          analysis["skills_incoming"][0]["damage"]), (43, 300))
        self.assertEqual(len(analysis["skills_incoming"]), 1)
        member_key = next(row["member_key"] for row in analysis["members"]
                          if row["character_name"] == "甲一")
        selected = self.store.hunter_city_analysis(
            self.connection, "admin-a", 7, member_key)
        self.assertEqual(selected["summary"]["matches"], 1)
        self.assertNotIn("opponents", selected["records"][0])
        detail = self.store.hunter_city_record(
            self.connection, "admin-a", first["match_id"])
        self.assertEqual(detail["opponents"][0]["skills_incoming"][0]["skill_id"], 43)
        with self.assertRaisesRegex(PvpBackendError, "管理权限"):
            self.store.hunter_city_analysis(self.connection, "member-card")
        with self.assertRaisesRegex(PvpBackendError, "不属于"):
            self.store.hunter_city_analysis(self.connection, "admin-b", 7, member_key)
        with self.assertRaisesRegex(PvpBackendError, "无权查看"):
            self.store.hunter_city_record(
                self.connection, "admin-b", first["match_id"])

    def test_alliance_search_subscriptions_paging_and_report_use_uploaded_players(self):
        start = record()["started_at_ns"] / 1e9
        self.store.now = lambda: start + 2 * 86400
        self.store.certify(self.connection, "club_a", "甲盟", "测试服", "admin-a")
        self.store.certify(self.connection, "club_b", "乙盟", "测试服", "admin-b")
        first_uid = character_token(1001)
        second_uid = character_token(1002)
        first = record(character_id=first_uid)
        first["player"]["name"] = "星河甲"
        first.update(map_id=5200131, mode_id=5500008,
                     mode_name="猎城战", map_name="猎龙之城", taken=200)
        other = record(match_id="pvp_" + "f" * 32, character_id=second_uid)
        other["player"]["name"] = "星河乙"
        self.store.upload(self.connection, "card-1", first)
        self.store.upload(self.connection, "card-2", other)
        self.assertEqual(len(self.store.search_alliance_players(
            self.connection, "ordinary-card", "星河")), 2)
        matches = self.store.search_alliance_players(self.connection, "admin-a", "星河")
        self.assertEqual({row["character_name"] for row in matches}, {"星河甲", "星河乙"})
        first_key = next(row["member_key"] for row in matches
                         if row["character_name"] == "星河甲")
        self.assertEqual(self.store.search_alliance_players(
            self.connection, "admin-b", "星河甲")[0]["member_key"], first_key)
        self.assertEqual(self.store.subscription_action(
            self.connection, "ordinary-card", "subscribe", first_key)[0]["new_matches"], 0)
        self.assertEqual(self.store.subscribed_report(
            self.connection, "ordinary-card", 30)["summary"]["matches"], 1)
        self.assertEqual(self.store.subscription_action(
            self.connection, "admin-a", "subscribe", first_key)[0]["new_matches"], 0)
        self.assertEqual(self.store.alliance(self.connection, "admin-b")["subscriptions"], [])
        for index in range(1, 52):
            item = record(match_id=f"pvp_{index:032x}", character_id=first_uid)
            item["player"]["name"] = "星河甲"
            item.update(map_id=5200167, mode_id=5500012,
                        mode_name="终末猎杀", map_name="猎龙之城")
            self.store.upload(self.connection, "card-1", item)
        subscriptions = self.store.alliance(self.connection, "admin-a")["subscriptions"]
        self.assertEqual((subscriptions[0]["matches"], subscriptions[0]["new_matches"]), (52, 51))
        page1 = self.store.alliance_player_records(self.connection, "admin-a", first_key)
        page2 = self.store.alliance_player_records(self.connection, "admin-a", first_key, offset=50)
        self.assertEqual((page1["total"], len(page1["records"]), len(page2["records"])),
                         (52, 50, 2))
        filtered = self.store.alliance_player_records(
            self.connection, "admin-a", first_key, 7, "猎城战")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["records"][0]["match_id"], first["match_id"])
        self.assertEqual(self.store.alliance_player_records(
            self.connection, "admin-a", first_key, 7, "猎城战", 0,
            "胜利")["total"], 0)
        detail = self.store.alliance_player_record(
            self.connection, "admin-a", first_key, first["match_id"])
        self.assertEqual(detail["player"]["name"], "星河甲")
        with self.assertRaisesRegex(PvpBackendError, "无权查看"):
            self.store.alliance_player_record(
                self.connection, "admin-a", first_key, other["match_id"])
        report = self.store.subscribed_report(self.connection, "admin-a", 30)
        self.assertEqual((report["subscribed_count"], report["summary"]["matches"]), (1, 52))
        self.assertEqual({row["mode_name"] for row in report["modes"]},
                         {"猎城战", "终末猎杀"})
        self.assertEqual(self.store.subscribed_report(
            self.connection, "admin-a", 30, "猎龙之城")["summary"]["matches"], 52)
        with self.assertRaises(PvpBackendError):
            self.store.subscribed_report(self.connection, "admin-a", 30,
                                         "战略服俱乐部宣战")
        self.assertEqual(self.store.subscribed_report(
            self.connection, "admin-b", 30)["summary"]["matches"], 0)
        self.assertEqual(self.store.subscription_action(
            self.connection, "admin-a", "seen", first_key)[0]["new_matches"], 0)
        self.assertEqual(self.store.subscription_action(
            self.connection, "admin-a", "unsubscribe", first_key), [])

    def test_subscription_survives_database_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pvp.sqlite3"
            first = sqlite3.connect(path)
            first.row_factory = sqlite3.Row
            first.execute("PRAGMA foreign_keys=ON")
            initialize_pvp_schema(first)
            store = PvpBackendStore(b"h" * 32, now=lambda: 1_800_000_000.0)
            store.certify(first, "club_a", "甲盟", "测试服", "admin-a")
            store.upload(first, "member-card", record())
            key = store.search_alliance_players(first, "admin-a", "本人")[0]["member_key"]
            store.subscription_action(first, "admin-a", "subscribe", key)
            first.commit()
            first.close()

            reopened = sqlite3.connect(path)
            reopened.row_factory = sqlite3.Row
            reopened.execute("PRAGMA foreign_keys=ON")
            initialize_pvp_schema(reopened)
            try:
                subscriptions = store.alliance(
                    reopened, "admin-a", include_members=False)["subscriptions"]
                self.assertEqual(len(subscriptions), 1)
                self.assertEqual(subscriptions[0]["member_key"], key)
                self.assertEqual(subscriptions[0]["new_matches"], 0)
            finally:
                reopened.close()

    def test_subscribed_report_custom_dates_cover_multiple_players_exactly(self):
        china = timezone(timedelta(hours=8))
        self.store.now = lambda: datetime(2026, 9, 27, tzinfo=china).timestamp()
        first_uid, second_uid = character_token(1001), character_token(1002)
        for index, (uid, day, mode_name, map_id) in enumerate((
            (first_uid, 20, "猎城战", 5200131),
            (second_uid, 24, "战略服俱乐部宣战", 5200229),
            (first_uid, 25, "猎城战", 5200131),
            (second_uid, 26, "战略服俱乐部宣战", 5200229),
        ), 1):
            payload = record(match_id=f"pvp_{index:032x}", character_id=uid)
            started = datetime(2026, 9, day, 12, tzinfo=china).timestamp()
            payload.update(started_at_ns=int(started * 1e9),
                           ended_at_ns=int((started + 90) * 1e9),
                           map_id=map_id, mode_name=mode_name,
                           mode_id=5500008 if map_id == 5200131 else 5500014,
                           map_name="猎龙之城" if map_id == 5200131 else "征服宣令")
            if day in (24, 25):
                payload["opponents"] = [dict(
                    opponent_id="enemy-a", character_id=character_token(2001),
                    name="对手甲", damage=300 if day == 24 else 500,
                    taken=200 if day == 24 else None,
                    kills=1 if day == 24 else 0,
                    defeats=1 if day == 25 else 0,
                )]
            if day == 25:
                # A Hunter City upload with a misleading result must not
                # create a win in the subscriber report.
                payload["result"] = "胜利"
            self.store.upload(self.connection, "member-card", payload)
        for uid in (first_uid, second_uid):
            self.store.subscription_action(
                self.connection, "viewer-card", "subscribe",
                character_hash(uid, self.store.hmac_key))

        report = self.store.subscribed_report(
            self.connection, "viewer-card", 0, "", "2026-09-24", "2026-09-25")
        self.assertEqual(report["subscribed_count"], 2)
        self.assertEqual(report["summary"]["matches"], 1)
        self.assertEqual(sum(row["matches"] for row in report["trend"]), 1)
        self.assertEqual(sum(row["damage"] for row in report["trend"]), 1200)
        self.assertEqual(report["summary"]["wins"], 0)
        self.assertEqual(len(report["trend"]), 12)
        self.assertEqual(report["range_start"],
                         datetime(2026, 9, 24, tzinfo=china).timestamp())
        self.assertEqual(report["range_end"],
                         datetime(2026, 9, 26, tzinfo=china).timestamp())
        self.assertEqual({row["mode_name"] for row in report["modes"]},
                         {"猎城战"})
        self.assertEqual(len(report["opponents"]), 1)
        self.assertEqual((report["opponents"][0]["damage"],
                          report["opponents"][0]["taken"],
                          report["opponents"][0]["taken_matches"],
                          report["opponents"][0]["deaths"]), (500, 0, 0, 1))
        first_key = character_hash(first_uid, self.store.hmac_key)
        selected = self.store.subscribed_report(
            self.connection, "viewer-card", 0, "", "2026-09-24",
            "2026-09-25", [first_key])
        self.assertEqual(selected["summary"]["matches"], 1)
        self.assertEqual(selected["selected_member_keys"], [first_key])
        self.assertEqual(selected["opponents"][0]["damage"], 500)
        hunter_records = self.store.alliance_player_records(
            self.connection, "viewer-card", first_key, 0, "猎城战", 0,
            "无胜负")
        self.assertEqual(hunter_records["total"], 2)
        self.assertEqual({row["result"] for row in hunter_records["records"]},
                         {"无胜负"})
        self.assertEqual(self.store.alliance_player_records(
            self.connection, "viewer-card", first_key, 0, "猎城战", 0,
            "胜利")["total"], 0)
        with self.assertRaises(PvpBackendError):
            self.store.subscribed_report(
                self.connection, "viewer-card", 0, "", "2026-09-24",
                "2026-09-25", ["f" * 64])
        filtered = self.store.subscribed_report(
            self.connection, "viewer-card", 0, "猎龙之城",
            "2026-09-24", "2026-09-25")
        self.assertEqual(filtered["summary"]["matches"], 1)
        self.assertEqual(sum(row["matches"] for row in filtered["trend"]), 1)
        self.assertEqual(sum(row["matches"] for row in self.store.subscribed_report(
            self.connection, "viewer-card", 0)["trend"]), 2)
        for start, end in (("2026-09-25", "2026-09-24"),
                           ("2026-09-24", ""), ("2026/09/24", "2026-09-25")):
            with self.subTest(start=start, end=end):
                with self.assertRaises(PvpBackendError):
                    self.store.subscribed_report(
                        self.connection, "viewer-card", 0, "", start, end)

    def test_legacy_club_subscription_migrates_once_to_open_menu(self):
        self.store.certify(self.connection, "club_a", "甲盟", "测试服", "admin-a")
        self.store.upload(self.connection, "member-card", record())
        key = self.store.search_alliance_players(
            self.connection, "admin-a", "本人")[0]["member_key"]
        self.connection.execute(
            """INSERT INTO pvp_alliance_subscriptions
               (club_id,card_hash,player_hash,player_name,seen_count,created_at)
               VALUES (?,?,?,?,?,?)""",
            ("club_a", "admin-a", key, "本人", 1, 1_800_000_000.0),
        )
        initialize_pvp_schema(self.connection)
        self.assertEqual(self.store.subscriptions(
            self.connection, "admin-a")[0]["member_key"], key)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM pvp_alliance_subscriptions").fetchone()[0], 0)
        self.store.subscription_action(self.connection, "admin-a", "unsubscribe", key)
        initialize_pvp_schema(self.connection)
        self.assertEqual(self.store.subscriptions(self.connection, "admin-a"), [])


if __name__ == "__main__":
    unittest.main()
