import base64
import copy
import json
import sqlite3
import unittest

from profile_upload import (
    ProfileUploadError,
    ProfileUploadStore,
    build_upload_encounter,
    canonical_character_identity,
    encounter_upload_rejection_code,
    initialize_profile_schema,
    normalize_nickname,
    recorded_self_character_id,
    _timeline_rows_by_actor,
)


def character_token(role_number: int, prefix: int = 1) -> str:
    value = bytes((prefix, 0, 0, 0)) + int(role_number).to_bytes(8, "little")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class ProfileUploadTests(unittest.TestCase):
    def test_deaths_are_public_and_incomplete_team_totals_remain_unknown(self):
        payload = self.encounter(self.first)
        payload['participants'][0]['deaths'] = 2
        receipt = self.store.upload_encounter(self.connection, self.first, payload, public_mode='character', app_version='0.3.5')
        detail = self.store.public_encounter(self.connection, receipt['encounter_id'])
        history = self.store.public_history(self.connection)['records'][0]
        for data in (detail, history):
            self.assertIsNone(data['team_deaths'])
            self.assertEqual(data['recorded_deaths'], 2)
            self.assertEqual(data['death_recorded_members'], 1)
            self.assertEqual(data['death_total_members'], 2)
        self.assertEqual(detail['participants'][0]['deaths'], 2)
        self.assertIsNone(detail['participants'][1]['deaths'])
        payload['participants'][1]['deaths'] = 0
        self.store.upload_encounter(self.connection, self.first, payload, public_mode='character', app_version='0.3.5')
        self.assertEqual(self.store.public_encounter(self.connection, receipt['encounter_id'])['team_deaths'], 2)
        self.assertEqual(self.store.public_history(self.connection)['records'][0]['team_deaths'], 2)

    def test_missing_names_are_marked_unrecorded(self):
        payload = self.encounter(self.first)
        for member in payload['participants']:
            member['game_character_name'] = ''
        receipt = self.store.upload_encounter(self.connection, self.first, payload, public_mode='anonymous', app_version='0.3.5')
        detail = self.store.public_encounter(self.connection, receipt['encounter_id'])
        self.assertEqual(detail['participants'][0]['public_mode'], 'unrecorded')
        self.assertEqual(detail['participants'][0]['display_name'], '未记录姓名 · 成员01')

    def test_death_total_is_incomplete_when_roster_members_are_missing(self):
        payload = self.encounter(self.first)
        payload['team_size'] = 3
        for member in payload['participants']:
            member['deaths'] = 0
        receipt = self.store.upload_encounter(self.connection, self.first, payload, public_mode='character', app_version='0.3.5')
        detail = self.store.public_encounter(self.connection, receipt['encounter_id'])
        self.assertIsNone(detail['team_deaths'])
        self.assertEqual(detail['recorded_deaths'], 0)
        self.assertEqual(detail['death_recorded_members'], 2)
        self.assertEqual(detail['death_total_members'], 3)

    def test_equipment_upload_preserves_display_fields_without_raw_identifiers(self):
        payload = self.encounter(self.first)
        payload['participants'][0]['equipment_snapshot'] = {
            'equipment_score': 12345, 'equipment_score_complete': True,
            'attributes': {'pCrit': 400, 'mAtkMin': 500},
            'equipment': [{'slot': 1, 'item_id': 3060643, 'item_name': '裂金之战刃', 'enhance_level': 4,
                'metadata': {'icon': '3060643', 'item_level': 60, 'owner': self.first},
                'details': {'uid': self.second},
                'affixes': [{'name': '暴击', 'properties': [{'key': 'Crit_N', 'name': '暴击', 'value': 123}]}],
                'special_affix': {'name': '特殊效果', 'passive_skill_ids': [86010010]}}],
        }
        payload['participants'][0]['skills'][0].update(critical_hits=2, penetration_hits=1, critical_rate=.5, count_semantics='server_skill_count')
        receipt = self.store.upload_encounter(self.connection, self.first, payload, public_mode='character', app_version='0.3.5')
        detail = self.store.public_encounter(self.connection, receipt['encounter_id'])
        stats = detail['participants'][0]['stats']
        self.assertEqual(stats['equipment_rating'], 12345)
        item = stats['equipment_snapshot']['equipment'][0]
        self.assertEqual(item['enhance_level'], 4)
        self.assertEqual(item['affixes'][0]['properties'][0]['value'], 123)
        self.assertEqual(item['special_affix']['passive_skill_ids'], [86010010])
        self.assertEqual(stats['skills'][0]['critical_hits'], 2)
        self.assertEqual(stats['skills'][0]['count_semantics'], 'server_skill_count')
        text = json.dumps(detail, ensure_ascii=False)
        for hidden in (self.first, self.second, 'owner', 'uid', 'character_hash'):
            self.assertNotIn(hidden, text)
        self.store.upload_encounter(self.connection, self.first, self.encounter(self.first), public_mode='character', app_version='0.3.5')
        repeated = self.store.public_encounter(self.connection, receipt['encounter_id'])
        self.assertEqual(repeated['participants'][0]['stats']['equipment_snapshot']['equipment'][0]['item_id'], 3060643)

    def test_event_payload_preserves_unknown_flags_and_known_false(self):
        record = {'event_log': {'columns': ['time_ms', 'actor_id', 'skill_id', 'damage', 'critical'],
            'rows': [[10, 2, 101, 200, None], [20, 2, 101, 300, 0], [30, 2, 101, 600, 1]]}}
        events = _timeline_rows_by_actor(record)[2]
        self.assertEqual([row['critical'] for row in events], [None, False, True])
        self.assertTrue(all(row['penetrating'] is None for row in events))
        clean = self.store._clean_participant_stats({'skill_timeline': events})
        self.assertEqual(clean['skill_timeline'], events)

    def test_critical_luck_requires_known_events_and_rewards_high_damage_crits(self):
        events = [{'skill_id': 101, 'damage': 100 * (i + 1) * (2 if i >= 5 else 1), 'critical': i >= 5} for i in range(10)]
        damage = sum(row['damage'] for row in events)
        self.assertIsNone(self.store.critical_luck_model({'skill_timeline': events[:7]}, damage))
        self.assertIsNone(self.store.critical_luck_model({'skill_timeline': [{**row, 'critical': None} for row in events]}, damage))
        model = self.store.critical_luck_model({'skill_timeline': events}, damage * 2)
        self.assertEqual(model['critical_hits'], 5)
        self.assertEqual(model['known_hits'], 10)
        self.assertEqual(model['coverage'], .5)
        self.assertAlmostEqual(model['observed'], damage * 2)
        self.assertGreater(model['extra'], 0)
        self.assertGreater(model['percentile'], .5)

    def test_captured_victory_can_upload_without_settlement_detail_packet(self):
        local = {'archive_reason': 'target_defeated', 'result': 'defeated', 'completion_confirmed': False,
                 'monster': {'name': '异化猎犬', 'template_id': 7109821}}
        self.assertEqual(encounter_upload_rejection_code(local), '')
        parsed = {'result': 'defeated', 'completion_confirmed': False, 'payload': local}
        self.assertEqual(encounter_upload_rejection_code(parsed), '')
        local['result'] = 'failed'
        self.assertEqual(encounter_upload_rejection_code(local), 'UPLOAD_VICTORY_REQUIRED')

    def test_victory_upload_does_not_automatically_qualify_without_settlement(self):
        self.create_profile(self.first, '通关记录')
        receipt = self.store.upload_encounter(
            self.connection, self.first,
            self.encounter(self.first, completion_confirmed=False),
            public_mode='nickname', app_version='0.2.3',
        )
        self.assertTrue(receipt.get('encounter_id'))
        self.assertIn('ENCOUNTER_NOT_COMPLETED', receipt['validation_reasons'])

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        initialize_profile_schema(self.connection)
        self.timestamp = 1_800_000_000.0
        self.store = ProfileUploadStore(
            b"profile-test-hmac-key-32-bytes-minimum!!",
            now=lambda: self.timestamp,
            sensitive_words=("违禁词",),
        )
        self.first = character_token(1001)
        self.second = character_token(1002)

    def tearDown(self) -> None:
        self.connection.close()

    def create_profile(self, token: str, nickname: str, name: str = "角色甲") -> dict:
        return self.store.create_profile(
            self.connection,
            token,
            nickname,
            character_name=name,
            profession_id=1200001,
        )["profile"]

    def encounter(
        self,
        uploader: str,
        *,
        result: str = "defeated",
        completion_confirmed: bool = True,
    ) -> dict:
        return {
            "client_encounter_id": "local-battle-001",
            "started_at_epoch": 1_799_999_880.0,
            "ended_at_epoch": 1_800_000_000.0,
            "duration_seconds": 120.0,
            "dungeon_id": 515,
            "stage_id": 9001,
            "difficulty": "normal",
            "boss_name": "测试首领",
            "boss_template_ids": [7100208],
            "archive_reason": "target_defeated",
            "result": result,
            "completion_confirmed": completion_confirmed,
            "data_completeness": "complete",
            "team_size": 2,
            "team_total_damage": 3_000_000,
            "game_version": "2026.09",
            "participants": [
                {
                    "character_id": self.first,
                    "is_uploader": uploader == self.first,
                    "game_character_name": "夜行者",
                    "profession_id": 1200001,
                    "damage": 2_000_000,
                    "dps": 16666.67,
                    "skills": [{"skill_id": 101, "name": "斩击", "damage": 2_000_000}],
                },
                {
                    "character_id": self.second,
                    "is_uploader": uploader == self.second,
                    "game_character_name": "审判者",
                    "profession_id": 1200002,
                    "damage": 1_000_000,
                    "dps": 8333.33,
                    "skills": [{"skill_id": 202, "name": "审判", "damage": 1_000_000}],
                },
            ],
        }

    def test_character_identity_is_canonical_and_rejects_ai_owner(self) -> None:
        identity = canonical_character_identity(self.first + "==")
        self.assertEqual(identity.canonical, self.first)
        self.assertEqual(identity.role_number, 1001)
        self.assertEqual(identity.kind, "human")
        with self.assertRaisesRegex(ProfileUploadError, "人机"):
            canonical_character_identity(character_token(99, 0x6A))

    def test_nickname_normalization_and_availability_rules(self) -> None:
        self.assertEqual(normalize_nickname("  Ａb１２  "), ("Ab12", "ab12"))
        self.assertEqual(
            self.store.nickname_availability(self.connection, "Dps-Logs")["status"],
            "RESERVED",
        )
        self.assertEqual(
            self.store.nickname_availability(self.connection, "叨叨诡秘助手")["status"],
            "RESERVED",
        )
        self.assertEqual(
            self.store.nickname_availability(self.connection, "叨叨")["status"],
            "RESERVED",
        )
        self.assertEqual(
            self.store.nickname_availability(self.connection, "含违禁词昵称")["status"],
            "SENSITIVE",
        )
        for nickname in ("A", "abcdefghijklM", "有 空格", "名字!", "名字\u200b", "😀😀"):
            self.assertEqual(
                self.store.nickname_availability(self.connection, nickname)["status"],
                "INVALID",
            )

    def test_case_and_full_width_variants_cannot_claim_twice(self) -> None:
        profile = self.create_profile(self.first, "Player12")
        result = self.store.nickname_availability(self.connection, "ＰＬＡＹＥＲ１２")
        self.assertEqual(result["status"], "CLAIMED")
        resolved = self.store.resolve_profile(self.connection, self.first)
        self.assertEqual(resolved["profile"], profile)

    def test_profile_lookup_without_metadata_does_not_erase_character_details(self) -> None:
        self.create_profile(self.first, "角色资料", "夜行者")
        hashed = self.store._hash_character(self.first)
        self.connection.execute(
            "UPDATE profile_characters SET profession_id=? WHERE character_hash=?",
            (1_200_001, hashed),
        )

        resolved = self.store.resolve_profile(self.connection, self.first)

        self.assertEqual(resolved["status"], "LINKED")
        row = self.connection.execute(
            "SELECT character_name, profession_id FROM profile_characters "
            "WHERE character_hash=?",
            (hashed,),
        ).fetchone()
        self.assertEqual(row["character_name"], "夜行者")
        self.assertEqual(row["profession_id"], 1_200_001)

    def test_rename_keeps_profile_id_and_reserves_old_name(self) -> None:
        original = self.create_profile(self.first, "旧昵称")
        renamed = self.store.rename_profile(self.connection, self.first, "新昵称")
        self.assertEqual(renamed["profile"]["profile_id"], original["profile_id"])
        self.assertEqual(renamed["profile"]["nickname"], "新昵称")
        self.assertEqual(
            self.store.nickname_availability(self.connection, "旧昵称")["status"],
            "RESERVED",
        )
        reclaimed = self.store.nickname_availability(
            self.connection, "旧昵称", current_profile_id=original["profile_id"]
        )
        self.assertEqual(reclaimed["status"], "AVAILABLE")

    def test_link_code_is_short_lived_one_time_and_links_multiple_characters(self) -> None:
        profile = self.create_profile(self.first, "共同身份")
        link = self.store.create_link_code(self.connection, self.first)
        linked = self.store.redeem_link_code(
            self.connection,
            self.second,
            link["code"],
            character_name="审判者",
            profession_id=1200002,
        )
        self.assertEqual(linked["profile"]["profile_id"], profile["profile_id"])
        third = character_token(1003)
        with self.assertRaisesRegex(ProfileUploadError, "已经使用"):
            self.store.redeem_link_code(self.connection, third, link["code"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM profile_characters WHERE profile_id=?",
                (profile["profile_id"],),
            ).fetchone()[0],
            2,
        )

    def test_expired_link_code_is_rejected(self) -> None:
        self.create_profile(self.first, "时限身份")
        link = self.store.create_link_code(self.connection, self.first)
        self.timestamp = float(link["expires_at"]) + 1
        with self.assertRaisesRegex(ProfileUploadError, "已过期"):
            self.store.redeem_link_code(self.connection, self.second, link["code"])

    def test_participant_uploads_merge_and_preserve_captured_names(self) -> None:
        profile_a = self.create_profile(self.first, "上传者甲", "夜行者")
        profile_b = self.create_profile(self.second, "上传者乙", "审判者")
        first_receipt = self.store.upload_encounter(
            self.connection,
            self.first,
            self.encounter(self.first),
            public_mode="nickname",
            character_name="夜行者",
            app_version="0.2.3",
        )
        public = self.store.public_encounter(
            self.connection, first_receipt["encounter_id"]
        )
        self.assertEqual(public["participants"][0]["display_name"], "上传者甲")
        self.assertEqual(public["participants"][1]["display_name"], "审判者")
        self.assertNotIn(self.second, json.dumps(public, ensure_ascii=False))

        second_receipt = self.store.upload_encounter(
            self.connection,
            self.second,
            self.encounter(self.second),
            public_mode="character",
            character_name="审判者",
            app_version="0.2.3",
        )
        self.assertEqual(second_receipt["encounter_id"], first_receipt["encounter_id"])
        self.assertFalse(second_receipt["created_encounter"])
        self.assertEqual(second_receipt["upload_source_count"], 2)
        public = self.store.public_encounter(
            self.connection, first_receipt["encounter_id"]
        )
        self.assertEqual(
            [item["display_name"] for item in public["participants"]],
            ["上传者甲", "审判者"],
        )
        self.assertEqual(public["participants"][0]["profile_id"], profile_a["profile_id"])
        self.assertEqual(public["participants"][1]["profile_id"], profile_b["profile_id"])

        repeated = self.store.upload_encounter(
            self.connection,
            self.second,
            self.encounter(self.second),
            public_mode="nickname",
            character_name="审判者",
            app_version="0.2.3",
        )
        self.assertTrue(repeated["duplicate_upload"])
        self.assertEqual(repeated["upload_source_count"], 2)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM encounters").fetchone()[0], 1
        )

    def test_legacy_anonymous_mode_publishes_available_character_name(self) -> None:
        first_receipt = self.store.upload_encounter(
            self.connection,
            self.first,
            self.encounter(self.first),
            public_mode="anonymous",
            character_name="夜行者",
            app_version="0.2.3",
        )
        upload = self.connection.execute(
            "SELECT profile_id, public_mode, public_character_name FROM uploads "
            "WHERE upload_id=?",
            (first_receipt["upload_id"],),
        ).fetchone()
        self.assertIsNone(upload["profile_id"])
        self.assertEqual(upload["public_mode"], "character")
        self.assertEqual(upload["public_character_name"], "夜行者")
        public = self.store.public_encounter(
            self.connection, first_receipt["encounter_id"]
        )
        self.assertEqual(public["participants"][0]["display_name"], "夜行者")
        self.assertEqual(public["participants"][0]["public_mode"], "character")
        self.assertEqual(public["participants"][0]["profile_id"], "")

        second_receipt = self.store.upload_encounter(
            self.connection,
            self.first,
            self.encounter(self.first),
            public_mode="character",
            character_name="夜行者",
            app_version="0.2.3",
        )
        self.assertTrue(second_receipt["duplicate_upload"])
        self.assertEqual(second_receipt["encounter_id"], first_receipt["encounter_id"])
        public = self.store.public_encounter(
            self.connection, first_receipt["encounter_id"]
        )
        self.assertEqual(public["participants"][0]["display_name"], "夜行者")
        self.assertEqual(public["participants"][0]["public_mode"], "character")
        self.assertEqual(public["participants"][0]["profile_id"], "")

    def test_profile_schema_migration_keeps_legacy_upload_receipts(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(
                """
                CREATE TABLE uploads (
                    upload_id TEXT PRIMARY KEY,
                    encounter_id TEXT NOT NULL,
                    uploader_character_hash TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    public_mode TEXT NOT NULL,
                    public_character_name TEXT NOT NULL DEFAULT '',
                    uploaded_at REAL NOT NULL,
                    last_uploaded_at REAL NOT NULL,
                    app_version TEXT NOT NULL DEFAULT '',
                    game_version TEXT NOT NULL DEFAULT '',
                    payload_hash TEXT NOT NULL,
                    data_completeness TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL DEFAULT 'accepted',
                    upload_status TEXT NOT NULL DEFAULT 'uploaded',
                    statistics_status TEXT NOT NULL DEFAULT 'not_eligible',
                    ranking_status TEXT NOT NULL DEFAULT 'not_eligible',
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(encounter_id, uploader_character_hash)
                );
                INSERT INTO uploads(
                    upload_id, encounter_id, uploader_character_hash, profile_id,
                    public_mode, uploaded_at, last_uploaded_at, payload_hash
                ) VALUES (
                    'upl_legacy', 'enc_legacy', 'hash_legacy', 'prf_legacy',
                    'nickname', 10, 11, 'payload_legacy'
                );
                """
            )

            initialize_profile_schema(connection)

            columns = {
                str(row["name"]): row
                for row in connection.execute("PRAGMA table_info(uploads)")
            }
            self.assertEqual(int(columns["profile_id"]["notnull"]), 0)
            receipt = connection.execute(
                "SELECT * FROM uploads WHERE upload_id='upl_legacy'"
            ).fetchone()
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["profile_id"], "prf_legacy")
            self.assertEqual(receipt["payload_hash"], "payload_legacy")
        finally:
            connection.close()

    def test_same_template_encounter_merges_despite_different_boss_labels(self) -> None:
        self.create_profile(self.first, "模板上传甲")
        self.create_profile(self.second, "模板上传乙")
        first_payload = self.encounter(self.first)
        first_payload["boss_name"] = "名称已识别"
        first_receipt = self.store.upload_encounter(
            self.connection,
            self.first,
            first_payload,
            public_mode="nickname",
            app_version="0.2.3",
        )
        second_payload = self.encounter(self.second)
        second_payload["boss_name"] = "未知首领"
        second_receipt = self.store.upload_encounter(
            self.connection,
            self.second,
            second_payload,
            public_mode="nickname",
            app_version="0.2.3",
        )

        self.assertEqual(
            second_receipt["encounter_id"], first_receipt["encounter_id"]
        )
        self.assertEqual(second_receipt["upload_source_count"], 2)

    def test_partial_identity_uploads_merge_by_strong_team_signature(self) -> None:
        self.create_profile(self.first, "不完整甲")
        self.create_profile(self.second, "不完整乙")
        first_payload = self.encounter(self.first)
        first_payload["participants"][1]["character_id"] = ""
        first_payload["participants"][1]["game_character_name"] = ""
        first_receipt = self.store.upload_encounter(
            self.connection,
            self.first,
            first_payload,
            public_mode="nickname",
            app_version="0.2.2",
        )

        second_payload = self.encounter(self.second)
        second_payload["client_encounter_id"] = "other-client-battle-001"
        second_payload["participants"][0]["character_id"] = ""
        second_payload["participants"][0]["game_character_name"] = ""
        second_receipt = self.store.upload_encounter(
            self.connection,
            self.second,
            second_payload,
            public_mode="nickname",
            app_version="0.2.2",
        )

        self.assertEqual(
            second_receipt["encounter_id"], first_receipt["encounter_id"]
        )
        self.assertEqual(second_receipt["upload_source_count"], 2)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM encounters").fetchone()[0],
            1,
        )
        participants = self.connection.execute(
            "SELECT COUNT(*), SUM(identity_resolved) FROM encounter_participants"
        ).fetchone()
        self.assertEqual(tuple(participants), (2, 2))
        public = self.store.public_encounter(
            self.connection, first_receipt["encounter_id"]
        )
        self.assertEqual(
            [item["display_name"] for item in public["participants"]],
            ["不完整甲", "不完整乙"],
        )

    def test_raw_character_ids_are_never_persisted_or_returned(self) -> None:
        self.create_profile(self.first, "隐私身份")
        receipt = self.store.upload_encounter(
            self.connection,
            self.first,
            self.encounter(self.first),
            public_mode="nickname",
            app_version="0.2.3",
        )
        dump = "\n".join(self.connection.iterdump())
        self.assertNotIn(self.first, dump)
        self.assertNotIn(self.second, dump)
        public_json = json.dumps(
            self.store.public_encounter(self.connection, receipt["encounter_id"]),
            ensure_ascii=False,
        )
        self.assertNotIn(self.first, public_json)
        self.assertNotIn(self.second, public_json)
        self.assertNotIn("character_hash", public_json)

    def test_rename_updates_display_without_changing_uploaded_ownership(self) -> None:
        profile = self.create_profile(self.first, "改名前")
        receipt = self.store.upload_encounter(
            self.connection,
            self.first,
            self.encounter(self.first),
            public_mode="nickname",
            app_version="0.2.3",
        )
        self.store.rename_profile(self.connection, self.first, "改名后")
        public = self.store.public_encounter(self.connection, receipt["encounter_id"])
        self.assertEqual(public["participants"][0]["display_name"], "改名后")
        upload_profile = self.connection.execute(
            "SELECT profile_id FROM uploads WHERE upload_id=?", (receipt["upload_id"],)
        ).fetchone()[0]
        self.assertEqual(upload_profile, profile["profile_id"])

    def test_failed_battle_is_rejected_without_creating_upload_rows(self) -> None:
        self.create_profile(self.first, "失败记录")
        with self.assertRaises(ProfileUploadError) as raised:
            self.store.upload_encounter(
                self.connection,
                self.first,
                self.encounter(
                    self.first, result="failed", completion_confirmed=False
                ),
                public_mode="nickname",
                app_version="0.2.3",
            )
        self.assertEqual(raised.exception.code, "UPLOAD_VICTORY_REQUIRED")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0],
            0,
        )

    def test_interrupted_battle_is_rejected_without_creating_upload_rows(self) -> None:
        self.create_profile(self.first, "退出记录")
        with self.assertRaises(ProfileUploadError) as raised:
            self.store.upload_encounter(
                self.connection,
                self.first,
                self.encounter(
                    self.first,
                    result="interrupted",
                    completion_confirmed=False,
                ),
                public_mode="nickname",
                app_version="0.2.3",
            )
        self.assertEqual(raised.exception.code, "UPLOAD_VICTORY_REQUIRED")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0],
            0,
        )

    def test_damage_and_healing_dummy_victories_are_never_uploadable(self) -> None:
        self.create_profile(self.first, "木桩拦截")
        damage_dummy = self.encounter(self.first)
        damage_dummy["boss_name"] = "伤害木桩"
        damage_dummy["boss_template_ids"] = [7_114_223]
        healing_dummy = self.encounter(self.first)
        healing_dummy["target_filter"] = "healing_dummy"
        healing_dummy["boss_template_ids"] = [7_114_224]

        for encounter in (damage_dummy, healing_dummy):
            self.assertEqual(
                encounter_upload_rejection_code(encounter),
                "UPLOAD_TRAINING_DUMMY_NOT_ALLOWED",
            )
            with self.assertRaises(ProfileUploadError) as raised:
                self.store.upload_encounter(
                    self.connection,
                    self.first,
                    encounter,
                    public_mode="nickname",
                    app_version="0.2.3",
                )
            self.assertEqual(
                raised.exception.code, "UPLOAD_TRAINING_DUMMY_NOT_ALLOWED"
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0],
            0,
        )

    def test_mid_encounter_capture_uploads_but_is_not_counted(self) -> None:
        self.create_profile(self.first, "中途采集")
        payload = self.encounter(self.first)
        payload["capture_started_mid_encounter"] = True

        receipt = self.store.upload_encounter(
            self.connection,
            self.first,
            payload,
            public_mode="nickname",
            app_version="0.2.2",
        )

        self.assertEqual(receipt["upload_status"], "uploaded")
        self.assertEqual(receipt["statistics_status"], "not_eligible")
        self.assertEqual(receipt["ranking_status"], "not_eligible")
        self.assertIn("CAPTURE_INCOMPLETE", receipt["validation_reasons"])

    def test_configured_game_version_gate_keeps_upload_but_blocks_statistics(self) -> None:
        self.create_profile(self.first, "版本校验")
        gated_store = ProfileUploadStore(
            b"profile-test-hmac-key-32-bytes-minimum!!",
            now=lambda: self.timestamp,
            supported_game_versions=("2026.10",),
        )
        receipt = gated_store.upload_encounter(
            self.connection,
            self.first,
            self.encounter(self.first),
            public_mode="nickname",
            app_version="0.2.3",
        )
        self.assertEqual(receipt["upload_status"], "uploaded")
        self.assertEqual(receipt["statistics_status"], "not_eligible")
        self.assertEqual(receipt["ranking_status"], "not_eligible")
        self.assertIn("GAME_VERSION_UNSUPPORTED", receipt["validation_reasons"])

    def test_one_profile_keeps_separate_scores_for_linked_characters(self) -> None:
        profile = self.create_profile(self.first, "共同榜单身份")
        link = self.store.create_link_code(self.connection, self.first)
        linked = self.store.redeem_link_code(
            self.connection,
            self.second,
            link["code"],
            character_name="审判者",
            profession_id=1200002,
        )
        self.assertEqual(linked["profile"]["profile_id"], profile["profile_id"])
        self.store.upload_encounter(
            self.connection,
            self.first,
            self.encounter(self.first),
            public_mode="nickname",
            app_version="0.2.3",
        )
        self.store.upload_encounter(
            self.connection,
            self.second,
            self.encounter(self.second),
            public_mode="nickname",
            app_version="0.2.3",
        )

        leaderboard = self.store.public_leaderboards(self.connection)
        self.assertEqual(len(leaderboard), 2)
        self.assertEqual(
            {row["profile_id"] for row in leaderboard}, {profile["profile_id"]}
        )
        self.assertEqual(
            {row["profession_id"] for row in leaderboard}, {1200001, 1200002}
        )
        self.assertEqual(
            {row["display_name"] for row in leaderboard}, {"共同榜单身份"}
        )

    def test_public_history_browses_nonranking_records_and_searches_captured_names(self) -> None:
        profile = self.create_profile(self.first, "历史查询")
        first = self.encounter(self.first)
        receipt = self.store.upload_encounter(self.connection, self.first, first, public_mode="nickname", app_version="0.3.5")
        second = self.encounter(self.first)
        second['client_encounter_id'] = 'older-partial'
        second['started_at_epoch'] -= 3600
        second['ended_at_epoch'] -= 3600
        second['team_total_damage'] += 100_000
        second['participants'][0]['game_character_name'] = '历史角色名'
        partial = self.store.upload_encounter(self.connection, self.first, second, public_mode="anonymous", app_version="0.3.5")
        self.assertEqual(partial['statistics_status'], 'not_eligible')
        history = self.store.public_history(self.connection, limit=1)
        self.assertEqual(history['total'], 2)
        self.assertEqual(history['records'][0]['encounter_id'], receipt['encounter_id'])
        page_two = self.store.public_history(self.connection, limit=1, offset=1)
        self.assertEqual(page_two['records'][0]['public_mode'], 'character')
        self.assertEqual(page_two['records'][0]['display_name'], '历史角色名')
        self.assertEqual(page_two['records'][0]['profile_id'], profile['profile_id'])
        self.assertIn('TEAM_TOTAL_MISMATCH', page_two['records'][0]['qualification_reasons'])
        for hidden in (self.first, self.second, 'character_hash'):
            self.assertNotIn(hidden, json.dumps(page_two, ensure_ascii=False))
        self.assertEqual(self.store.public_history(self.connection, query='历史角色名')['total'], 1)
        self.assertEqual(self.store.public_history(self.connection, query='审判者')['total'], 2)
        self.assertEqual(self.store.public_history(self.connection, query='历史查询')['total'], 2)
        self.store.rename_profile(self.connection, self.first, '改名历史')
        self.assertEqual(self.store.public_history(self.connection, query='历史查询')['total'], 0)
        self.assertEqual(self.store.public_history(self.connection, query='改名历史')['total'], 2)
        self.assertEqual(self.store.public_history(self.connection, eligibility='not_eligible')['total'], 1)
        self.assertEqual(self.store.public_history(self.connection, eligibility='included')['total'], 1)
        statistics = self.store.public_statistics(self.connection)
        self.assertEqual(statistics['history_encounters'], 2)
        self.assertEqual(statistics['encounters'], 1)
        catalog = self.store.public_catalog(self.connection)
        self.assertEqual(catalog['bosses'][0]['records'], 2)
        self.assertEqual(catalog['bosses'][0]['included'], 1)

    def test_public_history_deduplicates_shared_uploads_and_applies_filters(self) -> None:
        self.create_profile(self.first, '上传甲')
        self.create_profile(self.second, '上传乙')
        receipt = self.store.upload_encounter(self.connection, self.first, self.encounter(self.first), public_mode='character', app_version='0.3.5')
        self.store.upload_encounter(self.connection, self.second, self.encounter(self.second), public_mode='nickname', app_version='0.3.5')
        history = self.store.public_history(self.connection, query='上传乙')
        self.assertEqual(history['total'], 1)
        self.assertEqual(history['records'][0]['display_name'], '上传乙')
        self.assertEqual(history['records'][0]['encounter_id'], receipt['encounter_id'])
        self.assertEqual(self.store.public_history(self.connection, query='夜行者')['total'], 1)
        self.assertEqual(self.store.public_history(self.connection, query='上传甲')['total'], 1)
        self.assertEqual(self.store.public_history(self.connection, boss='测试首领', profession=1200002, difficulty='normal', started_after=1_799_999_999, ended_before=1_800_000_001)['total'], 1)
        for filters in ({'boss': '其他首领'}, {'profession': 1200007}, {'difficulty': 'hard'}, {'started_after': 1_800_000_001}, {'ended_before': 1_800_000_000}, {'query': "' OR 1=1 --"}):
            self.assertEqual(self.store.public_history(self.connection, **filters)['total'], 0)
        page = self.store.public_history(self.connection, limit='invalid', offset='-3')
        self.assertEqual(page['limit'], 1)
        self.assertEqual(page['offset'], 0)
        self.assertEqual(self.store.public_history(self.connection, limit=999)['limit'], 50)

    def test_public_performance_accepts_actual_boss_names_without_preview_samples(self) -> None:
        self.create_profile(self.first, '真实样本')
        record = self.encounter(self.first)
        for participant in record['participants']:
            participant['extraordinary_rating'] = 30_000
        self.store.upload_encounter(self.connection, self.first, record, public_mode='nickname', app_version='0.3.5')
        performance = self.store.public_performance(self.connection, boss='name:测试首领')
        self.assertEqual(performance['selection']['boss_name'], '测试首领')
        self.assertEqual(performance['source'], 'real_uploads')
        self.assertGreater(performance['total_samples'], 0)
        self.assertEqual(self.store.public_performance(self.connection, boss='name:不存在的首领')['total_samples'], 0)

    def test_public_performance_builds_real_profession_percentiles_and_filters(self) -> None:
        profile = self.create_profile(self.first, "洞察测试")
        for index in range(5):
            encounter = copy.deepcopy(self.encounter(self.first))
            encounter["client_encounter_id"] = f"drill-insight-{index}"
            encounter["started_at_epoch"] = 1_799_990_000.0 + index * 300
            encounter["ended_at_epoch"] = encounter["started_at_epoch"] + 120.0
            encounter["boss_name"] = "“钻头”"
            encounter["boss_template_ids"] = [7_115_090]
            encounter["dungeon_id"] = 5_100_069
            encounter["stage_id"] = 5_150_106
            encounter["difficulty"] = "normal"
            encounter["game_version"] = "2026.09"
            first_dps = 10_000 + index * 4_000
            second_dps = 5_000 + index * 2_000
            encounter["participants"][0]["dps"] = first_dps
            encounter["participants"][0]["damage"] = first_dps * 120
            encounter["participants"][0]["extraordinary_rating"] = 70_000 + index * 5_000
            encounter["participants"][1]["dps"] = second_dps
            encounter["participants"][1]["damage"] = second_dps * 120
            encounter["participants"][1]["extraordinary_rating"] = 68_000 + index * 5_000
            encounter["team_total_damage"] = (first_dps + second_dps) * 120
            self.store.upload_encounter(
                self.connection,
                self.first,
                encounter,
                public_mode="nickname",
                app_version="0.2.3",
            )

        result = self.store.public_performance(
            self.connection,
            boss="钻头",
            difficulty="normal",
            metric="dps",
            rating_basis="extraordinary",
            min_rating=0,
            max_rating=200_000,
            game_version="2026.09",
            sort_by="p50",
            profile_id=profile["profile_id"],
        )
        self.assertEqual(result["selection"]["boss"], "drill")
        self.assertEqual(result["total_samples"], 10)
        self.assertEqual(result["total_encounters"], 5)
        self.assertEqual(result["availability"]["rating_min"], 68_000)
        self.assertEqual(result["availability"]["rating_max"], 90_000)
        groups = {row["profession_id"]: row for row in result["groups"]}
        self.assertEqual(groups[1_200_001]["sample_count"], 5)
        self.assertEqual(groups[1_200_001]["p50"], 18_000)
        self.assertEqual(groups[1_200_002]["p50"], 9_000)
        self.assertIsNotNone(groups[1_200_001]["mine"])
        self.assertIsNone(groups[1_200_002]["mine"])
        self.assertNotIn("character_hash", json.dumps(result, ensure_ascii=False))

        narrowed = self.store.public_performance(
            self.connection,
            boss="drill",
            min_rating=80_000,
            max_rating=90_000,
        )
        self.assertEqual(narrowed["total_samples"], 5)

        calibrated = self.store.public_performance(
            self.connection,
            boss="drill",
            calibrated=True,
        )
        self.assertEqual(calibrated["calibration"]["applied_groups"], 2)

        unavailable = self.store.public_performance(
            self.connection,
            boss="drill",
            rating_basis="equipment",
        )
        self.assertFalse(unavailable["availability"]["equipment_rating"])
        self.assertEqual(unavailable["total_samples"], 0)

    def test_client_payload_recovers_stage_tokens_and_marks_only_local_player(self) -> None:
        record = {
            "encounter_id": "source-001",
            "started_at_epoch": 100.0,
            "ended_at_epoch": 130.0,
            "duration_seconds": 30.0,
            "dungeon_id": 1,
            "dungeon_stage_id": 2,
            "total_damage": 300,
            "team_size": 2,
            "archive_reason": "target_defeated",
            "monster": {"name": "首领", "template_id": 7100208},
            "targets": [{"template_id": 7100208, "boss_type": 3}],
            "team_dps_timeline": [
                {"time_seconds": 1.25, "team_dps": 100, "source": "capture"},
                {"time": 2.5, "time_seconds": 99, "dps": 200},
                {"elapsed_seconds": 3.75, "dps": 250},
                {"second": 5, "dps": 300},
                {"dps": 350},
            ],
            "participants": [
                {"actor_id": 11, "name": "本人", "is_self": True, "damage": 200},
                {"actor_id": 12, "name": "队友", "is_self": False, "damage": 100},
            ],
            "damage_accounting": {
                "stage_summary_validations": [
                    {
                        "completion_confirmed": True,
                        "actors": [
                            {"actor_id": 11, "user_token": self.first},
                            {"actor_id": 12, "user_token": self.second},
                        ],
                    }
                ]
            },
        }
        payload = build_upload_encounter(
            record, self.first, current_character_name="本人"
        )
        self.assertEqual(recorded_self_character_id(record), self.first)
        self.assertEqual(
            [item["character_id"] for item in payload["participants"]],
            [self.first, self.second],
        )
        self.assertEqual(
            [item["is_uploader"] for item in payload["participants"]],
            [True, False],
        )
        self.assertEqual(payload["participants"][1]["game_character_name"], "队友")
        self.assertEqual(
            [point["time"] for point in payload["team_dps_timeline"]],
            [1.25, 2.5, 3.75, 5, 0],
        )
        self.assertEqual(payload["team_dps_timeline"][0]["source"], "capture")
        self.assertEqual(payload["team_dps_timeline"][0]["dps"], 100)
        self.assertEqual(payload["team_dps_timeline"][1]["team_dps"], 200)
        with self.assertRaisesRegex(ProfileUploadError, "本机角色"):
            build_upload_encounter(record, self.second)


if __name__ == "__main__":
    unittest.main()
