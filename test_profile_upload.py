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
)


def character_token(role_number: int, prefix: int = 1) -> str:
    value = bytes((prefix, 0, 0, 0)) + int(role_number).to_bytes(8, "little")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class ProfileUploadTests(unittest.TestCase):
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
                    "game_character_name": "夜行者" if uploader == self.first else "泄露甲",
                    "profession_id": 1200001,
                    "damage": 2_000_000,
                    "dps": 16666.67,
                    "skills": [{"skill_id": 101, "name": "斩击", "damage": 2_000_000}],
                },
                {
                    "character_id": self.second,
                    "is_uploader": uploader == self.second,
                    "game_character_name": "审判者" if uploader == self.second else "泄露乙",
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

    def test_participant_uploads_merge_and_only_self_becomes_public(self) -> None:
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
        self.assertEqual(public["participants"][1]["display_name"], "匿名玩家02")
        self.assertNotIn("泄露乙", json.dumps(public, ensure_ascii=False))

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

    def test_unlinked_character_can_upload_anonymously_or_show_own_character_name(self) -> None:
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
        self.assertEqual(upload["public_mode"], "anonymous")
        self.assertEqual(upload["public_character_name"], "")
        public = self.store.public_encounter(
            self.connection, first_receipt["encounter_id"]
        )
        self.assertEqual(public["participants"][0]["display_name"], "匿名玩家01")
        self.assertEqual(public["participants"][0]["public_mode"], "anonymous")
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
        self.assertEqual(payload["participants"][1]["game_character_name"], "")
        with self.assertRaisesRegex(ProfileUploadError, "本机角色"):
            build_upload_encounter(record, self.second)


if __name__ == "__main__":
    unittest.main()
