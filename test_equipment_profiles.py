import base64
import json
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from equipment_profiles import (
    ARM_CLAIMED,
    ARM_DONE,
    ARM_IDLE,
    ARM_READY,
    EquipmentMetadataCatalog,
    EquipmentProfileCoordinator,
    EquipmentQueryArchive,
    EquipmentQuerySession,
    EXPECTED_ARGUMENT_DESCRIPTORS,
    REQUEST_STUB_STATE_PREFIX,
    STATE_ARM,
    STATE_MAGIC,
    TRAMPOLINE_OFFSET,
    _absolute_patch,
    _owned_idle_request_hook,
    _restore_owned_idle_request_hook,
    _suspend_threads,
    equipment_item_name,
    equipment_slot_name,
    load_equipment_item_name_cache,
    locate_registered_method,
    parse_shape_response,
    profile_matches_session,
    scan_equipment_item_names,
    write_equipment_item_name_cache,
)
from network_state import PROJECTION_NAME_SUFFIX


def _token(prefix: int, fill: int) -> str:
    return base64.urlsafe_b64encode(bytes([prefix]) + bytes([fill]) * 11).decode(
        "ascii"
    ).rstrip("=")


class EquipmentProfileTests(unittest.TestCase):
    def test_confirmed_item_names_never_guess_unknown_templates(self):
        self.assertEqual(equipment_item_name(3_060_643), "裂金之战刃")
        self.assertEqual(equipment_item_name(3_210_641), "镜像之自我")
        self.assertEqual(
            equipment_item_name(3_060_643, 440_011_883_341_056),
            "裂金之战刃",
        )
        self.assertEqual(equipment_item_name(3_060_643, 1), "")
        self.assertEqual(equipment_item_name(1), "")

    def test_item_name_cache_rejects_localization_id_drift(self):
        metadata = {
            3_250_456: {"item_name_id": 440_011_883_401_216},
            3_270_456: {"item_name_id": 440_011_883_401_472},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "equipment_item_names.json"
            write_equipment_item_name_cache(
                path,
                metadata,
                {3_250_456: "新装备一", 3_270_456: "新装备二"},
            )
            current = dict(metadata)
            current[3_270_456] = {"item_name_id": 440_011_883_999_999}

            self.assertEqual(
                load_equipment_item_name_cache(path, current),
                {3_250_456: "新装备一"},
            )

    def test_live_item_name_scan_keeps_unique_matches_only(self):
        class Candidate:
            def __init__(self, text):
                self.text = text

        metadata = {
            3_250_456: {"item_name_id": 440_011_883_401_216},
            3_270_456: {"item_name_id": 440_011_883_401_472},
            3_280_456: {"item_name_id": 440_011_883_401_728},
        }
        scanner = MagicMock()
        scanner.__enter__.return_value.scan.return_value = {
            440_011_883_401_472: [Candidate("新装备二")],
            440_011_883_401_728: [Candidate("候选甲"), Candidate("候选乙")],
        }
        with patch("ksbc2_skill_names.LiveLocalizationScanner", return_value=scanner):
            names = scan_equipment_item_names(
                1234,
                metadata,
                {3_250_456: "缓存装备一"},
            )

        self.assertEqual(
            names,
            {3_250_456: "缓存装备一", 3_270_456: "新装备二"},
        )
        scanner.__enter__.return_value.scan.assert_called_once_with(
            {440_011_883_401_472, 440_011_883_401_728}
        )

    def test_sparse_wire_slots_match_the_live_equipment_panel(self):
        self.assertEqual(
            [equipment_slot_name(slot) for slot in (1, 2, 5, 6, 7, 9, 10, 12)],
            ["武器", "胸针", "指环", "护符", "护甲", "鞋靴", "帽子", "披风"],
        )

    def test_full_wire_profile_is_parsed_and_enriched(self):
        player = _token(1, 7)
        raw_item = {
            "$map": [
                [2, 9_100_001],
                [
                    12,
                    {
                        "$map": [
                            [
                                1,
                                {
                                    "$map": [
                                        [0, [501, 502, 503]],
                                        [1, 77_001],
                                        [2, 98],
                                        [99, "unknown-random-field"],
                                    ]
                                },
                            ]
                        ]
                    },
                ],
                [88, {"future": [1, 2, 3]}],
            ]
        }
        raw_profile = {
            "$map": [
                [0, "Mo Xue"],
                [1, 10086],
                [2, 1_200_001],
                [4, 80],
                [11, {"$map": [[1, raw_item]]}],
                [19, 80_702],
                [25, {"$map": [[0, 4_270_019], [1, 4_271_009]]}],
                [97, "unknown-profile-field"],
            ]
        }
        parsed = parse_shape_response(
            {
                "decoded_arguments": [
                    {"$map": [[player, raw_profile]]},
                    123456,
                ]
            }
        )

        self.assertEqual(parsed["correlation"], 123456)
        profile = parsed["profiles"][0]
        self.assertEqual(profile["extraordinary_rating"], 80_702)
        self.assertEqual(profile["level"], 80)
        self.assertEqual(profile["avatar_id"], 4_270_019)
        self.assertEqual(profile["avatar_frame_id"], 4_271_009)
        self.assertEqual(profile["raw_profile"][97], "unknown-profile-field")
        self.assertEqual(
            profile["equipment"][0]["raw_item"][88],
            {"future": [1, 2, 3]},
        )
        self.assertEqual(profile["equipment"][0]["auxiliary_id"], 77_001)

        catalog = EquipmentMetadataCatalog(
            source_path="fixture.ksbc2",
            source_sha256="abc",
            item_metadata={
                9_100_001: {
                    "tag": 2,
                    "mode": "pvp",
                    "icon": "fixture",
                }
            },
            word_class_types={501: 1, 502: 2, 503: 1},
            item_names={9_100_001: "已解析装备"},
        )
        enriched = catalog.enrich(profile)
        self.assertEqual(enriched["pvp_equipment_count"], 1)
        self.assertEqual(enriched["active_word_count"], 2)
        self.assertEqual(enriched["total_word_count"], 3)
        self.assertEqual(
            enriched["equipment"][0]["word_class_types"], [1, 2, 1]
        )
        self.assertEqual(enriched["equipment"][0]["slot_name"], "武器")
        self.assertEqual(enriched["equipment"][0]["item_name"], "已解析装备")
        self.assertEqual(enriched["equipment"][0]["item_score"], 98)
        self.assertIsNone(enriched["equipment_score"])
        self.assertEqual(enriched["equipment_known_score"], 98)
        self.assertFalse(enriched["equipment_score_complete"])
        self.assertEqual(enriched["equipment"][0]["affixes"][0]["word_id"], 501)

    def test_local_final_score_adds_verified_body_enhancement(self):
        catalog = EquipmentMetadataCatalog(
            source_path="fixture.ksbc2",
            source_sha256="abc",
            item_metadata={
                3_060_643: {
                    "tag": 1,
                    "mode": "adventure",
                    "base_score": 2_910,
                    "quality": 6,
                    "season_id": 101,
                }
            },
            word_class_types={},
            body_grow_ids={(101, 1): 101},
            enhance_schedules={
                101: tuple(
                    {
                        "level": level,
                        "score": 80,
                        "cumulative_score": level * 80,
                    }
                    for level in range(1, 9)
                )
            },
        )
        profile = {
            "equipment": [
                {
                    "slot": 1,
                    "item_id": 3_060_643,
                    "base_score": 2_910,
                    "word_score": 3_615,
                }
            ]
        }
        enriched = catalog.enrich(
            profile,
            exact_scores={
                1: {
                    "item_id": 3_060_643,
                    "enhance_score": 320,
                    "enhance_level": 4,
                    "enhance_stage_count": 8,
                    "enhance_stages": [
                        {
                            "stage": level,
                            "level": 10 if level <= 4 else 0,
                            "percent": 100 if level <= 4 else 0,
                        }
                        for level in range(1, 9)
                    ],
                    "total_score": 6_845,
                    "quality": 6,
                    "score_source": "local_equipment_model",
                }
            },
        )

        item = enriched["equipment"][0]
        self.assertEqual(item["base_score"], 2_910)
        self.assertEqual(item["random_score"], 3_615)
        self.assertEqual(item["enhance_score"], 320)
        self.assertEqual(item["enhance_level"], 4)
        self.assertEqual(item["enhance_completed_level"], 4)
        self.assertEqual(item["enhance_completed_score"], 320)
        self.assertEqual(item["enhance_level_score"], 320)
        self.assertEqual(item["enhance_level_progress_percent"], 100)
        self.assertEqual(item["enhance_overall_percent"], 50)
        self.assertEqual(item["next_enhance_level"], 5)
        self.assertEqual(item["next_enhance_score"], 400)
        self.assertEqual(item["next_enhance_increment"], 80)
        self.assertEqual(item["next_enhance_remaining"], 80)
        self.assertEqual(item["known_score"], 6_525)
        self.assertEqual(item["total_score"], 6_845)
        self.assertEqual(item["item_score"], 6_845)
        self.assertTrue(item["score_complete"])
        self.assertEqual(item["quality_name"], "黄色品质")
        self.assertEqual(enriched["equipment_score"], 6_845)
        self.assertTrue(enriched["equipment_score_complete"])

        partial = catalog.enrich(
            profile,
            exact_scores={
                1: {
                    "item_id": 3_060_643,
                    "base_score": 2_910,
                    "random_score": 3_615,
                    "enhance_score": 384,
                    "enhance_stage_count": 8,
                    "enhance_stages": [
                        {
                            "stage": level,
                            "level": (
                                10 if level <= 4 else 8 if level == 5 else 0
                            ),
                            "percent": (
                                100 if level <= 4 else 80 if level == 5 else 0
                            ),
                        }
                        for level in range(1, 9)
                    ],
                    "total_score": 6_909,
                    "quality": 6,
                    "score_source": "local_equipment_model",
                }
            },
        )["equipment"][0]
        self.assertEqual(partial["enhance_level"], 5)
        self.assertEqual(partial["enhance_completed_level"], 4)
        self.assertEqual(partial["enhance_score"], 384)
        self.assertEqual(partial["enhance_completed_score"], 320)
        self.assertEqual(partial["enhance_level_score"], 400)
        self.assertEqual(partial["enhance_level_remaining"], 16)
        self.assertEqual(partial["enhance_level_progress_percent"], 80)
        self.assertEqual(partial["enhance_overall_percent"], 60)

    def test_remote_score_does_not_invent_missing_body_enhancement(self):
        catalog = EquipmentMetadataCatalog(
            source_path="fixture.ksbc2",
            source_sha256="abc",
            item_metadata={
                3_210_641: {
                    "tag": 1,
                    "mode": "adventure",
                    "base_score": 3_642,
                    "quality": 7,
                }
            },
            word_class_types={},
        )
        enriched = catalog.enrich(
            {
                "equipment": [
                    {
                        "slot": 2,
                        "item_id": 3_210_641,
                        "base_score": 3_642,
                        "word_score": 953,
                    }
                ]
            }
        )

        item = enriched["equipment"][0]
        self.assertEqual(item["known_score"], 4_595)
        self.assertEqual(item["item_score"], 4_595)
        self.assertIsNone(item["enhance_score"])
        self.assertIsNone(item["total_score"])
        self.assertFalse(item["score_complete"])
        self.assertEqual(item["quality_name"], "红色品质")
        self.assertIsNone(enriched["equipment_score"])
        self.assertEqual(enriched["equipment_known_score"], 4_595)

    def test_remote_word_and_special_scores_have_verifiable_breakdown(self):
        catalog = EquipmentMetadataCatalog(
            source_path="fixture.ksbc2",
            source_sha256="abc",
            item_metadata={
                3_060_643: {
                    "tag": 1,
                    "mode": "adventure",
                    "base_score": 2_910,
                    "quality": 6,
                    "icon": "3060643",
                }
            },
            word_class_types={
                3_930_158: 1,
                3_930_155: 1,
                3_930_321: 2,
            },
            word_metadata={
                3_930_158: {
                    "effect_type": "FightProp",
                    "score": 870,
                    "properties": [
                        {"key": "Atk_N", "name": "攻击力", "value": 332}
                    ],
                },
                3_930_155: {
                    "effect_type": "FightProp",
                    "score": 675,
                    "properties": [
                        {"key": "Atk_N", "name": "攻击力", "value": 258}
                    ],
                },
                3_930_321: {
                    "effect_type": "FightProp",
                    "score": 301,
                    "properties": [
                        {"key": "Crit_N", "name": "暴击", "value": 79}
                    ],
                },
            },
            convergence_metadata={
                109: {
                    "auxiliary_id": 109,
                    "name": "特殊攻击词条",
                    "score": 1_200,
                    "icon": "/Game/Special.Special",
                    "passive_skill_ids": [80_003_002],
                    "properties": [
                        {"key": "Atk_N", "name": "攻击力", "value": 300}
                    ],
                }
            },
        )
        enriched = catalog.enrich(
            {
                "equipment": [
                    {
                        "slot": 1,
                        "item_id": 3_060_643,
                        "base_score": 2_910,
                        "word_ids": [
                            3_930_158,
                            3_930_158,
                            3_930_155,
                            3_930_321,
                        ],
                        "auxiliary_id": 109,
                        "word_score": 3_916,
                    }
                ]
            }
        )

        item = enriched["equipment"][0]
        self.assertEqual(item["word_scores"], [870, 870, 675, 301])
        self.assertEqual(
            [row["name"] for row in item["affixes"]],
            ["攻击力", "攻击力", "攻击力", "暴击"],
        )
        self.assertEqual(item["affixes"][0]["property_value"], 332)
        self.assertEqual(item["special_affix"]["score"], 1_200)
        self.assertEqual(item["special_affix"]["passive_skill_ids"], [80_003_002])
        self.assertEqual(item["score_breakdown_total"], 3_916)
        self.assertTrue(item["score_breakdown_complete"])
        self.assertTrue(item["score_breakdown_matches"])
        self.assertEqual(item["known_score"], 6_826)
        self.assertFalse(item["score_complete"])

    def test_archive_is_append_only_and_preserves_binary_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = EquipmentQueryArchive(Path(temporary))
            first_path = archive.append(
                "request_started",
                logical_arguments=[["token-a"], 123],
            )
            second_path = archive.append(
                "response_received",
                complete_payload=b"\x00\x01\xfe\xff",
                decoded={"unknown": [1, {"nested": True}]},
            )

            self.assertEqual(first_path, second_path)
            records = [
                json.loads(line)
                for line in first_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [record["event"] for record in records],
                ["request_started", "response_received"],
            )
            self.assertEqual(
                records[1]["complete_payload"]["$bytes_base64"],
                base64.b64encode(b"\x00\x01\xfe\xff").decode("ascii"),
            )
            self.assertEqual(records[1]["decoded"]["unknown"][1]["nested"], True)

    def test_session_queries_only_real_members_and_never_reuses_other_context(self):
        local_player = _token(1, 1)
        teammate = _token(1, 2)
        ai_member = _token(0x6A, 3)
        projection = _token(1, 4)
        session = EquipmentQuerySession.from_value(
            {
                "game_pid": 111,
                "capture_session_id": 222,
                "local_user_token": local_player,
                "party_session_id": 333,
                "member_count": 4,
                "members": [
                    {"user_token": local_player, "actor_id": 1, "name": "Self"},
                    {"user_token": teammate, "actor_id": 2, "name": "Mo Xue"},
                    {"user_token": ai_member, "actor_id": 3, "name": "AI", "is_ai": True},
                    {
                        "user_token": projection,
                        "actor_id": 4,
                        "name": "Projection" + PROJECTION_NAME_SUFFIX,
                        "is_ai": True,
                    },
                ],
            }
        )

        self.assertEqual(set(session.tokens), {local_player, teammate})

        human_with_same_prefix = EquipmentQuerySession.from_value({
            "game_pid": 111,
            "capture_session_id": 222,
            "local_user_token": local_player,
            "party_session_id": 333,
            "member_count": 2,
            "members": [
                {"user_token": local_player, "actor_id": 1, "name": "Self"},
                {
                    "user_token": ai_member,
                    "actor_id": 3,
                    "name": "Real opponent" + PROJECTION_NAME_SUFFIX,
                    "is_ai": False,
                },
            ],
        })
        self.assertIn(ai_member, human_with_same_prefix.tokens)
        payload = {
            **session.archive_fields(),
            "user_token": teammate,
        }
        self.assertTrue(
            profile_matches_session(
                payload,
                game_pid=111,
                capture_session_id=222,
                local_user_token=local_player,
                party_session_id=333,
                current_tokens=[local_player, teammate],
            )
        )
        self.assertTrue(
            profile_matches_session(
                payload,
                game_pid=111,
                capture_session_id=223,
                local_user_token=local_player,
                party_session_id=333,
                current_tokens=[local_player, teammate],
            )
        )
        next_transport = EquipmentQuerySession.from_value(
            {
                "game_pid": 111,
                "capture_session_id": 223,
                "local_user_token": local_player,
                "party_session_id": 333,
                "member_count": 2,
                "members": [
                    {"user_token": local_player, "actor_id": 1, "name": "Self"},
                    {"user_token": teammate, "actor_id": 2, "name": "Mo Xue"},
                ],
            }
        )
        self.assertEqual(session.key, next_transport.key)
        self.assertFalse(
            profile_matches_session(
                payload,
                game_pid=112,
                capture_session_id=223,
                local_user_token=local_player,
                party_session_id=333,
                current_tokens=[local_player, teammate],
            )
        )
        self.assertFalse(
            profile_matches_session(
                payload,
                game_pid=111,
                capture_session_id=223,
                local_user_token=local_player,
                party_session_id=334,
                current_tokens=[local_player, teammate],
            )
        )


class EquipmentQueryLatencyTests(unittest.TestCase):
    def coordinator(self, emit=lambda *_args: None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return EquipmentProfileCoordinator(
            data_dir=Path(directory.name), emit=emit,
            stop_event=threading.Event(), runtime_profile_provider=lambda: None,
        )

    def session_value(self, fill, party=7, pid=111):
        return {
            'game_pid': pid, 'capture_session_id': 1,
            'local_user_token': _token(1, 1), 'party_session_id': party,
            'member_count': 6,
            'members': [{'user_token': _token(1, fill), 'actor_id': fill, 'name': 'Player'}],
        }

    def test_verified_descriptor_location_does_not_rescan_process(self):
        with patch('equipment_profiles._descriptor_schema', return_value=EXPECTED_ARGUMENT_DESCRIPTORS) as schema:
            with patch('equipment_profiles._scan_descriptor_range') as scan:
                self.assertEqual(locate_registered_method(123, 456), (456, (456,)))
                schema.assert_called_once_with(123, 456)
                scan.assert_not_called()

    def test_invalid_descriptor_hint_is_not_used(self):
        with patch('equipment_profiles._descriptor_schema', return_value=()):
            with patch('equipment_profiles._scan_descriptor_range', return_value=[789]) as scan:
                self.assertEqual(locate_registered_method(123, 456), (789, (789,)))
                scan.assert_called_once()

    def test_unsent_newcomers_are_combined_without_crossing_teams(self):
        coordinator = self.coordinator()
        self.assertTrue(coordinator.schedule(self.session_value(2)))
        self.assertTrue(coordinator.schedule(self.session_value(3)))
        self.assertTrue(coordinator.schedule(self.session_value(4, party=8)))
        first = coordinator.query_commands.get_nowait()
        merged = coordinator._coalesce_queries(first)
        self.assertEqual(set(merged.tokens), {_token(1, 2), _token(1, 3)})
        self.assertEqual(merged.party_session_id, 7)
        self.assertEqual(merged.member_count, 6)
        separate = coordinator.query_commands.get_nowait()
        self.assertEqual(separate.party_session_id, 8)
        self.assertEqual(separate.tokens, (_token(1, 4),))
        self.assertTrue(coordinator.query_commands.empty())

    def test_query_worker_coalesces_member_arriving_during_short_wait(self):
        coordinator = self.coordinator()
        coordinator.schedule(self.session_value(2))
        executed = []

        def wait_for_roster(_seconds):
            coordinator.schedule(self.session_value(3))
            return False

        def execute(session):
            executed.append(session)
            coordinator.stop_event.set()

        with patch.object(coordinator.closing, 'wait', side_effect=wait_for_roster):
            with patch.object(coordinator, '_execute_query', side_effect=execute):
                coordinator._run_queries()

        self.assertEqual(len(executed), 1)
        self.assertEqual(set(executed[0].tokens), {_token(1, 2), _token(1, 3)})

    def test_new_opponent_query_precedes_queued_full_team_batch(self):
        coordinator = self.coordinator()
        allies = self.session_value(2)
        allies['members'] = [
            {'user_token': _token(1, index), 'actor_id': index, 'name': 'Ally'}
            for index in range(2, 14)
        ]
        opponent = self.session_value(14)
        opponent['priority'] = True
        coordinator.schedule(allies)
        coordinator.schedule(opponent)
        executed = []

        def execute(session):
            executed.append(session)
            coordinator.stop_event.set()

        with patch.object(coordinator.closing, 'wait', return_value=False), patch.object(
            coordinator, '_execute_query', side_effect=execute
        ):
            coordinator._run_queries()

        self.assertEqual(executed[0].tokens, (_token(1, 14),))
        self.assertTrue(executed[0].priority)
        self.assertEqual(len(coordinator.query_commands.get_nowait().tokens), 12)

    def test_enemy_priority_change_bypasses_deduplication(self):
        coordinator = self.coordinator()
        regular = self.session_value(2)
        urgent = dict(regular, priority=True)
        self.assertTrue(coordinator.schedule(regular))
        self.assertTrue(coordinator.schedule(urgent))
        self.assertTrue(coordinator.priority_queries.get_nowait().priority)
        self.assertTrue(coordinator.query_commands.get_nowait().priority is False)

    def test_response_is_published_while_another_query_is_waiting(self):
        entered = threading.Event()
        release = threading.Event()
        published = threading.Event()
        payloads = []

        def emit(kind, payload):
            payloads.append((kind, payload))
            published.set()

        coordinator = self.coordinator(emit)
        coordinator.catalog = EquipmentMetadataCatalog('fixture', 'sha', {}, {})

        def wait_query(session):
            with coordinator.lock:
                coordinator.requests[123456] = {'session': session, 'context': {}}
            entered.set()
            release.wait(4)

        with patch.object(coordinator, '_execute_query', side_effect=wait_query):
            coordinator.start()
            try:
                coordinator.schedule(self.session_value(2))
                self.assertTrue(entered.wait(2))
                coordinator.handle_response({'decoded_arguments': [
                    {_token(1, 2): {0: 'Player', 11: {}}}, 123456,
                ]})
                self.assertTrue(published.wait(2), 'response was blocked by request preparation')
                self.assertFalse(release.is_set())
                self.assertEqual(payloads[0][0], 'equipment_profile')
                self.assertEqual(payloads[0][1]['user_token'], _token(1, 2))
            finally:
                release.set()
                coordinator.close()
        self.assertFalse(coordinator.thread.is_alive())
        self.assertFalse(coordinator.query_thread.is_alive())


class EquipmentRequestHookRecoveryTests(unittest.TestCase):
    def test_thread_enumerator_keeps_private_signature_after_capture_import(self):
        import equipment_profiles
        import inline_capture

        self.assertIs(
            equipment_profiles._thread32_first.argtypes[1]._type_,
            equipment_profiles.THREADENTRY32,
        )
        self.assertIs(
            inline_capture.kernel32.Thread32First.argtypes[1]._type_,
            inline_capture.THREADENTRY32,
        )
        self.assertTrue(equipment_profiles._thread_ids(os.getpid()))

    def test_thread_that_exits_after_enumeration_is_ignored(self):
        class FakeKernel32:
            def __init__(self):
                self.opened = []
                self.resumed = []
                self.closed = []

            def OpenThread(self, _access, _inherit, thread_id):
                self.opened.append(thread_id)
                return 0 if thread_id == 10 else 200

            def SuspendThread(self, _thread):
                return 0

            def ResumeThread(self, thread):
                self.resumed.append(thread)
                return 0

            def CloseHandle(self, thread):
                self.closed.append(thread)
                return True

        fake = FakeKernel32()
        with patch("equipment_profiles._thread_ids", return_value=[10, 20]), patch(
            "equipment_profiles.kernel32", fake
        ), patch("equipment_profiles.ctypes.get_last_error", return_value=87):
            handles = _suspend_threads(123)

        self.assertEqual(handles, [200])
        self.assertEqual(fake.opened, [10, 20])

    @staticmethod
    def owned_hook_memory(*, arm_state=ARM_IDLE):
        target = 0x100000
        code = 0x200000
        state = 0x300000
        prologue = bytes.fromhex("40534883ec40488b81e8000000488bd9")
        memory = {
            target: _absolute_patch(code, len(prologue)),
            code: REQUEST_STUB_STATE_PREFIX + struct.pack("<Q", state),
            state: STATE_MAGIC
            + b"\0" * (STATE_ARM - len(STATE_MAGIC))
            + struct.pack("<Q", arm_state),
            code + TRAMPOLINE_OFFSET: prologue
            + b"\xff\x25\x00\x00\x00\x00"
            + struct.pack("<Q", target + len(prologue)),
        }

        def read(_process, address, length):
            value = memory.get(address)
            return value[:length] if value is not None and len(value) >= length else None

        return target, code, state, prologue, memory, read

    def test_valid_inactive_abandoned_hook_is_restored(self):
        for arm_state in (ARM_IDLE, ARM_READY, ARM_DONE):
            with self.subTest(arm_state=arm_state):
                target, code, state, prologue, memory, read = self.owned_hook_memory(
                    arm_state=arm_state
                )

                def write(_process, address, value):
                    memory[address] = bytes(value)

                with patch("equipment_profiles.read_region", side_effect=read), patch(
                    "equipment_profiles._write_code", side_effect=write
                ) as write_code, patch(
                    "equipment_profiles._suspend_threads", return_value=[88]
                ), patch("equipment_profiles._resume_threads") as resume:
                    self.assertEqual(
                        _owned_idle_request_hook(1, target, prologue),
                        (code, state),
                    )
                    self.assertTrue(
                        _restore_owned_idle_request_hook(1, 123, target, prologue)
                    )

                self.assertEqual(memory[target], prologue)
                write_code.assert_called_once_with(1, target, prologue)
                resume.assert_called_once_with([88])

    def test_unknown_or_active_hook_is_never_overwritten(self):
        target, _code, _state, prologue, memory, read = self.owned_hook_memory(
            arm_state=ARM_CLAIMED
        )
        with patch("equipment_profiles.read_region", side_effect=read), patch(
            "equipment_profiles._write_code"
        ) as write_code, patch("equipment_profiles._suspend_threads") as suspend:
            self.assertFalse(
                _restore_owned_idle_request_hook(1, 123, target, prologue)
            )
        write_code.assert_not_called()
        suspend.assert_not_called()

        memory[target] = b"\xcc" * len(prologue)
        with patch("equipment_profiles.read_region", side_effect=read), patch(
            "equipment_profiles._write_code"
        ) as write_code:
            self.assertFalse(
                _restore_owned_idle_request_hook(1, 123, target, prologue)
            )
        write_code.assert_not_called()


if __name__ == "__main__":
    unittest.main()
