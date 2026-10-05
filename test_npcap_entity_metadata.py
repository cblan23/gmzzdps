import struct
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

from npcap_entity_metadata import (
    PassiveEntityMetadataReader,
    PassiveTeamProfilePoller,
    decode_component,
)
from monster_metadata import MonsterSource, validated_reserved_damage_targets
from runtime_metadata import (
    LiveTeamProfileReader,
    RuntimeMetadataReader,
    latest_main_player_role,
)


class RuntimeRoleLogTests(unittest.TestCase):
    def test_role_is_found_when_creation_line_is_older_than_tail_window(self):
        token = "AQAAAOwNKLYHAAAA"
        actor_id = 57191790922782
        creation = (
            "[2026.09.16-00.36.27:636][526]LuaLog: ReleaseLog: "
            "create_entity Entity:"
            f"{token}, Uid:{actor_id} cname:MainPlayer isPlayer: true\n"
        ).encode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "C7.log"
            log_path.write_bytes(creation + b"chat output\n" * 1000)

            self.assertEqual(
                latest_main_player_role(log_path, tail_bytes=4096),
                (token, actor_id),
            )

    def test_backwards_search_returns_newest_main_player(self):
        old_token = "AQAAAOwNKlKdAAAA"
        new_token = "AQAAAOwNKLYHAAAA"
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "C7.log"
            log_path.write_bytes(
                (
                    f"create_entity Entity:{old_token}, Uid:101 "
                    "cname:MainPlayer isPlayer: true\n"
                ).encode("ascii")
                + b"x" * 5000
                + (
                    f"create_entity Entity:{new_token}, Uid:202 "
                    "cname:MainPlayer isPlayer: true\n"
                ).encode("ascii")
                + b"y" * 5000
            )

            self.assertEqual(
                latest_main_player_role(log_path, tail_bytes=4096),
                (new_token, 202),
            )


class EntityMetadataTests(unittest.TestCase):
    def test_future_damage_targets_require_matching_type_and_localization(self):
        validated = validated_reserved_damage_targets(
            [
                MonsterSource(7_100_635, 3, 90, 422_213_270_373_888, "", ()),
                MonsterSource(7_100_638, 1, None, None, "", ()),
                MonsterSource(7_100_639, 3, 90, 422_213_270_374_144, "", ()),
            ]
        )

        self.assertEqual(set(validated), {"7100635"})
        self.assertEqual(validated["7100635"]["name"], "伤害木桩")
        self.assertTrue(validated["7100635"]["damage_target_validated"])

    def test_validated_damage_target_evidence_reaches_parser_record(self):
        reader = object.__new__(PassiveEntityMetadataReader)
        reader.templates = {
            "7100635": {
                "boss_type": 3,
                "level": 90,
                "localization_id": 422_213_270_373_888,
                "name": "伤害木桩",
                "damage_target_validated": True,
            }
        }

        record = reader._record(
            57_228_828_730_502,
            0x1234,
            (57_228_828_730_502, 7_100_635, 3, 0x2000, 0x1100),
            134_353_748_206_558_295,
        )

        self.assertTrue(record["damage_target_validated"])
        self.assertEqual(record["localization_id"], 422_213_270_373_888)
        self.assertEqual(record["level"], 90)

    def test_newest_object_slots_are_checked_first(self):
        reader = object.__new__(PassiveEntityMetadataReader)
        reader.base = 0x1000
        reader.table = {'count_rva': 0x20, 'chunks_rva': 0x30}
        chunks = 0x2000
        first_chunk = 0x3000
        last_chunk = 0x4000
        reads = {
            0x1020: struct.pack('<I', 65538),
            0x1030: struct.pack('<Q', chunks),
            chunks: struct.pack('<Q', first_chunk),
            chunks + 8: struct.pack('<Q', last_chunk),
            last_chunk: struct.pack('<QQQ', 0, 22, 0) + struct.pack('<QQQ', 0, 33, 0),
        }
        reader._read = lambda address, length: reads.get(address)

        self.assertEqual(list(reader._objects())[:2], [33, 22])

    def raw(self):
        raw=bytearray(0x19c)
        struct.pack_into('<Q',raw,0,0x1100)
        struct.pack_into('<Q',raw,0x10,0x3000)
        struct.pack_into('<Q',raw,0x58,12345)
        struct.pack_into('<I',raw,0x198,7114223)
        raw[0x137]=3
        return raw

    def test_exact_known_template(self):
        self.assertEqual(decode_component(self.raw(),12345,0x1000,0x1000,{'7114223':{}})[:3],(12345,7114223,3))

    def test_unrelated_object_or_unknown_template_is_rejected(self):
        self.assertIsNone(decode_component(self.raw(),54321,0x1000,0x1000,{'7114223':{}}))
        self.assertIsNone(decode_component(self.raw(),12345,0x1000,0x1000,{}))
        self.assertIsNone(decode_component(self.raw(),12345,0x5000,0x1000,{'7114223':{}}))

    def test_truncated_read_is_rejected(self):
        self.assertIsNone(decode_component(self.raw()[:-1],12345,0x1000,0x1000,{'7114223':{}}))

    def test_stage_template_change_is_emitted_for_same_entity(self):
        reader = object.__new__(PassiveEntityMetadataReader)
        reader.base, reader.size = 0x1000, 0x1000
        reader.templates = {'7114223': {}, '7114225': {}}
        first = decode_component(self.raw(), 12345, reader.base, reader.size, reader.templates)
        reader.resolved = {12345}
        reader.components = {12345: 0x4000}
        reader.signatures = {12345: first}
        reader.checked_at = {}
        reader.ready = []
        reader.pending = {}
        reader.retry_at = 0
        reader.counters = Counter()
        changed = self.raw()
        struct.pack_into('<I', changed, 0x198, 7114225)
        reader._read = lambda *_: changed
        reader.request(12345, 123456)
        updates = reader.poll()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['entity_id'], 12345)
        self.assertEqual(updates[0]['template_id'], 7114225)
        self.assertEqual(reader.poll(), [])


class TeamProfilePollerTests(unittest.TestCase):
    def test_default_interval_refreshes_within_one_second(self):
        poller = PassiveTeamProfilePoller(1234)

        self.assertEqual(poller.interval_seconds, 1.0)

    def test_replace_drops_members_from_the_previous_roster(self):
        poller = PassiveTeamProfilePoller(1234)
        old_token = "startup-old-member"
        current_token = "startup-current-member"
        poller.request((old_token, current_token))

        poller.replace((current_token,))

        self.assertEqual(poller._active_tokens(), {current_token})

    def test_new_token_forces_one_immediate_full_roster_poll(self):
        first_token = "AQAAAOwNuopBAAAA"
        second_token = "AQAAAOwN0ga-AAAA"

        class FakeReader:
            calls = []

            def __init__(self, *_args, **_kwargs):
                pass

            def snapshot(self, tokens):
                self.__class__.calls.append(frozenset(tokens))
                return []

            def close(self):
                pass

        with patch("npcap_entity_metadata.LiveTeamProfileReader", FakeReader):
            poller = PassiveTeamProfilePoller(1234, interval_seconds=1.0)
            poller.request([first_token])
            poller.start()
            deadline = time.monotonic() + 0.5
            try:
                while len(FakeReader.calls) < 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(FakeReader.calls, [frozenset({first_token})])

                poller.request([first_token])
                poller.replace([first_token])
                time.sleep(0.1)
                self.assertEqual(len(FakeReader.calls), 1)

                poller.request([second_token])
                deadline = time.monotonic() + 0.5
                while len(FakeReader.calls) < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(
                    FakeReader.calls,
                    [
                        frozenset({first_token}),
                        frozenset({first_token, second_token}),
                    ],
                )

                poller.request([second_token])
                poller.replace([first_token, second_token])
                time.sleep(0.1)
                self.assertEqual(len(FakeReader.calls), 2)
            finally:
                poller.close()

    def test_periodic_poll_republishes_and_observes_changed_power(self):
        token = "AQAAAOwNuopBAAAA"

        class FakeReader:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.calls = 0
                self.closed = False
                self.__class__.instances.append(self)

            def snapshot(self, tokens):
                self.calls += 1
                return [
                    {
                        "user_token": next(iter(tokens)),
                        "name": "team-member",
                        "profession_id": 1_200_003,
                        "level": 70,
                        "extraordinary_rating": 91_443 + self.calls,
                    }
                ]

            def close(self):
                self.closed = True

        with patch(
            "npcap_entity_metadata.LiveTeamProfileReader", FakeReader
        ):
            poller = PassiveTeamProfilePoller(
                1234,
                interval_seconds=1.0,
            )
            # Keep production's two-second default intact while making this
            # background scheduling test finish promptly.
            poller.interval_seconds = 0.02
            poller.request([token])
            poller.start()
            records = []
            deadline = time.monotonic() + 1.0
            try:
                while len(records) < 2 and time.monotonic() < deadline:
                    records.extend(poller.poll())
                    time.sleep(0.01)
            finally:
                poller.close()
                records.extend(poller.poll())

        self.assertGreaterEqual(len(records), 2)
        self.assertEqual(records[0]["user_token"], token)
        self.assertEqual(records[0]["extraordinary_rating"], 91_444)
        self.assertGreater(
            records[-1]["extraordinary_rating"],
            records[0]["extraordinary_rating"],
        )
        self.assertTrue(
            all(
                record["method"] == "NpcapLiveTeamProfile"
                and record["capture_source"]
                == "npcap_read_only_team_profile"
                and record["profile_source"] == "live_lua_power"
                for record in records
            )
        )
        self.assertTrue(FakeReader.instances[0].closed)

    def test_unchanged_periodic_profile_is_not_requeued(self):
        token = "AQAAAOwNuopBAAAA"

        class FakeReader:
            def __init__(self, *_args, **_kwargs):
                self.calls = 0

            def snapshot(self, _tokens):
                self.calls += 1
                return [{
                    "user_token": token,
                    "name": "team-member",
                    "profession_id": 1_200_003,
                    "level": 70,
                    "extraordinary_rating": 91_444,
                }]

            def close(self):
                pass

        with patch("npcap_entity_metadata.LiveTeamProfileReader", FakeReader):
            poller = PassiveTeamProfilePoller(1234, interval_seconds=1.0)
            poller.interval_seconds = 0.01
            poller.request([token])
            poller.start()
            try:
                time.sleep(0.08)
                records = poller.poll()
            finally:
                poller.close()

        self.assertEqual(len(records), 1)

    def test_same_process_character_switch_publishes_new_local_role(self):
        old_role = ("AQAAAOwNKLYHAAAA", 57_463_447_253_497)
        new_role = ("AQAAAOwNkGB8AAAA", 57_233_666_752_426)

        class FakeReader:
            role = old_role

            def __init__(self, *_args, **_kwargs):
                self.closed = False

            def current_local_role(self):
                return self.__class__.role

            def snapshot(self, tokens):
                profiles = []
                for token in tokens:
                    profiles.append(
                        {
                            "user_token": token,
                            "name": (
                                "old-role" if token == old_role[0] else "new-role"
                            ),
                            "profession_id": 1_200_002,
                            "level": 70,
                            "extraordinary_rating": (
                                80_975 if token == old_role[0] else 89_361
                            ),
                            "client_rating_key": "CEScore",
                            "client_rating_binding": "actor_CEScore",
                        }
                    )
                return profiles

            def close(self):
                self.closed = True

        callbacks = []
        with patch(
            "npcap_entity_metadata.LiveTeamProfileReader", FakeReader
        ):
            poller = PassiveTeamProfilePoller(
                1234,
                interval_seconds=1.0,
                startup_profile_callback=callbacks.append,
            )
            poller.interval_seconds = 0.02
            poller.start()
            deadline = time.monotonic() + 1.0
            try:
                while len(callbacks) < 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(callbacks[0]["user_token"], old_role[0])
                self.assertEqual(callbacks[0]["entity_id"], old_role[1])

                FakeReader.role = new_role
                poller._wake.set()
                while len(callbacks) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                poller.close()

        self.assertEqual(len(callbacks), 2)
        self.assertEqual(callbacks[1]["user_token"], new_role[0])
        self.assertEqual(callbacks[1]["entity_id"], new_role[1])
        self.assertGreater(
            callbacks[1]["local_role_generation"],
            callbacks[0]["local_role_generation"],
        )
        self.assertEqual(poller._startup_token, new_role[0])
        self.assertEqual(poller._startup_actor_id, new_role[1])

    def test_encounter_context_is_published_without_a_group_roster(self):
        context = {
            "dungeon_id": 5_100_064,
            "map_id": 5_200_224,
            "in_dungeon": True,
            "brass_tome_status": "disabled",
            "brass_tome_enabled": False,
            "brass_tome_challenge_ids": [],
            "context_source": "live_lua_dungeon_and_brass_tome",
        }

        class FakeReader:
            def __init__(self, *_args, **_kwargs):
                pass

            def current_local_role(self):
                return None

            def current_dungeon_roster(self):
                return None

            def current_encounter_context(self):
                return dict(context)

            def close(self):
                pass

        with patch("npcap_entity_metadata.LiveTeamProfileReader", FakeReader):
            poller = PassiveTeamProfilePoller(1234, interval_seconds=1.0)
            poller.interval_seconds = 0.01
            poller.start()
            records = []
            deadline = time.monotonic() + 1.0
            try:
                while not records and time.monotonic() < deadline:
                    records.extend(poller.poll())
                    time.sleep(0.01)
            finally:
                poller.close()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["method"], "ReadOnlyCurrentEncounterContext")
        self.assertEqual(records[0]["map_id"], 5_200_224)
        self.assertEqual(records[0]["brass_tome_status"], "disabled")


class LiveTeamProfileReaderTests(unittest.TestCase):
    def test_current_encounter_context_reads_dungeon_map_and_brass_tome_state(self):
        state = bytearray(0x50)
        struct.pack_into("<Q", state, 0x48, 0x200)

        for raw_challenge_list, expected_status, expected_ids in (
            (106, "enabled", [10_101]),
            (203, "disabled", []),
            (None, "unknown", []),
        ):
            with self.subTest(status=expected_status):
                reader = object.__new__(LiveTeamProfileReader)
                reader.process = object()
                reader._stopped = lambda: False
                reader._discover_profile_lua_state = lambda: 0x100
                table_values = {
                    "Game": 101,
                    "DungeonSystem": 102,
                    "currentDungeonContext": 103,
                    "BrassTomeSystem": 104,
                    "model": 105,
                }
                reader._lua_table_value = (
                    lambda _table, key: table_values.get(key, 0)
                )
                reader._lua_value_tag = lambda value: (
                    -12 if value in {101, 102, 103, 104, 105, 106}
                    else -3 if value == 201
                    else -1 if value == 203
                    else None
                )
                reader._lua_table_nodes = lambda value: value

                def fields(table, _strings):
                    if table == 103:
                        return {
                            "InDungeon": (0, 201),
                            "DungeonTemplateID": (0, 5_100_064),
                            "LevelMapID": (0, 5_200_224),
                        }
                    if table == 105 and raw_challenge_list is not None:
                        return {"InGameChallengeItemList": (0, raw_challenge_list)}
                    if table == 106:
                        return {"ChallengeID": (0, 10_101)}
                    return {}

                reader._lua_table_fields = fields
                reader._lua_numeric_table_items = lambda _table: []
                reader._lua_nonnegative_integer = (
                    lambda value, maximum=None: int(value)
                )

                with patch("runtime_metadata.read_region", return_value=bytes(state)):
                    context = reader.current_encounter_context()

                self.assertEqual(context["dungeon_id"], 5_100_064)
                self.assertEqual(context["map_id"], 5_200_224)
                self.assertIs(context["in_dungeon"], True)
                self.assertEqual(context["brass_tome_status"], expected_status)
                self.assertEqual(context["brass_tome_challenge_ids"], expected_ids)
    def test_current_group_roster_is_valid_when_local_player_is_outside_dungeon(self):
        self_token = "AQAAAOwNKLYHAAAA"
        peer_token = "AQAAAOwN0ga-AAAA"
        reader = object.__new__(LiveTeamProfileReader)
        reader.process = object()
        reader._stopped = lambda: False
        reader.current_local_role = lambda: (self_token, 12345)
        reader._discover_profile_lua_state = lambda: 0x100
        table_values = {
            "Game": 1,
            "GroupSystem": 2,
            "DungeonSystem": 3,
            "model": 4,
            "currentDungeonContext": 0,
            "MemberIndexMap": 6,
            "GroupAllInfo": 7,
            "groupInfoMap": 8,
            "teamInfoMap": 9,
            "members": 10,
        }
        reader._lua_table_value = (
            lambda _table, key: table_values.get(key, 0)
        )
        reader._lua_table_nodes = lambda value: value
        reader._lua_table_fields = lambda table, _seen: (
            {self_token: (0, 0), peer_token: (0, 0)}
            if table == 6
            else {
                "id": (0, 30 + table),
                "name": (0, 40 + table),
            }
            if table in {21, 22}
            else {}
        )
        reader._lua_value_tag = lambda value: -12 if value in {21, 22} else 0
        reader._lua_numeric_table_items = lambda table: (
            [(7, 20)]
            if table == 8
            else [(1, 19)]
            if table == 9
            else [(1, 21), (2, 22)]
            if table == 10
            else []
        )
        reader._read_lua_string = lambda value, _maximum: {
            51: self_token,
            52: peer_token,
            61: "本人",
            62: "副本中的队友",
        }.get(value, "")
        reader._clean_name = lambda value: value
        reader._lua_nonnegative_integer = lambda _value, maximum=None: None
        state = bytearray(0x50)
        struct.pack_into("<Q", state, 0x48, 0x200)

        with patch("runtime_metadata.read_region", return_value=bytes(state)):
            roster = reader.current_dungeon_roster()

        self.assertIsNotNone(roster)
        self.assertEqual(roster["dungeon_id"], 0)
        self.assertEqual(roster["group_id"], 7)
        self.assertEqual(
            [member["user_token"] for member in roster["members"]],
            [self_token, peer_token],
        )

    def test_profile_lua_state_checks_observed_32bit_band_first(self):
        reader = object.__new__(LiveTeamProfileReader)
        reader.profile_lua_state = 0
        reader.role_key_object = 0x5B33E2F18
        reader._discover_role_key = Mock(return_value=True)
        reader._lua_global = Mock(return_value=0xFE4103F8)
        calls = []

        def scan(start, end, _signature):
            calls.append((start, end))
            return iter((0xFE410389,))

        reader._scan_range = Mock(side_effect=scan)

        self.assertEqual(reader._discover_profile_lua_state(), 0xFE410380)
        self.assertEqual(calls[0], (0xF0000000, 0x100000000))

    def test_bound_team_scan_precedes_slow_local_score_fallback(self):
        tokens = {"AQAAAOwNKLYHAAAA", "AQAAAOwN0ga-AAAA"}
        reader = object.__new__(LiveTeamProfileReader)
        reader._scan_bound_lua_profiles = Mock(return_value=set(tokens))
        reader._scan_local_actor_score = Mock()
        reader._discover_role_key = Mock()

        reader._scan_live_profiles(tokens)

        reader._scan_bound_lua_profiles.assert_called_once_with(tokens)
        reader._scan_local_actor_score.assert_not_called()
        reader._discover_role_key.assert_not_called()

    def test_lua_role_names_support_legacy_gbk_and_utf8_storage(self):
        self.assertEqual(
            RuntimeMetadataReader._decode_lua_text("莫雪".encode("gb18030")),
            "莫雪",
        )
        self.assertEqual(
            RuntimeMetadataReader._decode_lua_text("莫雪".encode("utf-8")),
            "莫雪",
        )

    def test_role_key_discovery_checks_current_lua_heap_band_first(self):
        reader = object.__new__(RuntimeMetadataReader)
        reader.role_key_object = 0
        calls = []

        def scan(start, end, needle):
            calls.append((start, end, needle))
            return iter([0x35BA0ED8]) if start == 0x30000000 else iter(())

        reader._scan_range = Mock(side_effect=scan)
        reader._valid_gc_string_object = Mock(return_value=0x35BA0EC0)

        self.assertTrue(reader._discover_role_key())
        self.assertEqual(calls[0][:2], (0x30000000, 0x40000000))
        self.assertEqual(reader.role_key_object, 0x35BA0EC0)

    def test_unique_local_ce_score_pair_tracks_current_actor_only(self):
        token = 'AQAAAOwNKLYHAAAA'
        rating_key_at = 0x13EA2B888
        maximum_key_at = 0x13EA2AE98
        reader = object.__new__(LiveTeamProfileReader)
        reader.profile_locators = {
            token: {
                'kind': 'local_actor_CEScore_pair',
                'user_token': token,
                'actor_id': 57_223_461_897_621,
                'rating_key_at': rating_key_at,
                'maximum_key_at': maximum_key_at,
            }
        }
        reader.current_local_role = Mock(
            return_value=(token, 57_223_461_897_621)
        )
        values = {
            rating_key_at: 101,
            maximum_key_at: 102,
            rating_key_at - 8: struct.unpack('<Q', struct.pack('<d', 81_227.0))[0],
            maximum_key_at - 8: struct.unpack('<Q', struct.pack('<d', 84_456.0))[0],
        }
        reader._read_qword = Mock(side_effect=lambda address: values.get(address, 0))
        reader._read_lua_string = Mock(
            side_effect=lambda raw, _limit=128: {101: 'CEScore', 102: 'MaxCEScore'}.get(raw, '')
        )

        profile = reader._cached_profile(token)

        self.assertEqual(profile['extraordinary_rating'], 81_227)
        self.assertEqual(
            profile['client_rating_binding'], 'local_actor_CEScore_pair'
        )
        reader.current_local_role.return_value = (token, 57_223_461_897_622)
        self.assertIsNone(reader._cached_profile(token))

    @staticmethod
    def bound_reader(layout, *, token='AQAAAOwNKLYHAAAA', rating=80975):
        reader = object.__new__(LiveTeamProfileReader)
        strings = {}
        tables = {}
        address = 0x100000
        token_value = 0xfffd800000800008

        def table(at, values, *, metatable=0, mask=31):
            nodes = at + 0x1000
            head = bytearray(0x40)
            head[9] = 11
            struct.pack_into('<Q', head, 0x20, metatable)
            struct.pack_into('<Q', head, 0x28, nodes)
            struct.pack_into('<I', head, 0x34, mask)
            data = bytearray((mask + 1) * 24)
            for index, (key, value) in enumerate(values.items()):
                key_raw = 0xfffd800000900000 + len(strings) * 0x100
                strings[key_raw] = key
                if isinstance(value, str):
                    value_raw = token_value if value == token else key_raw + 0x80
                    strings[value_raw] = value
                elif isinstance(value, tuple):
                    value_raw = value[0]
                else:
                    value_raw = struct.unpack('<Q', struct.pack('<d', value))[0]
                struct.pack_into('<QQ', data, index * 24, value_raw, key_raw)
            result = (bytes(head), nodes, mask, bytes(data))
            tables[at] = result
            return result

        if layout == 'actor':
            table(0x300000, {'__cname': 'MainPlayer'})
            table(0x200000, {'__index': (0xfffa000000300000,)})
            fields = {'Name': '本人', 'eid': token, 'CEScore': rating,
                      'MaxCEScore': 90000, 'ZhanLi': 78750,
                      'Profession': 1200002, 'Level': 70, 'shortUid': 33120849388}
            table(address, fields, metatable=0x200000)
        else:
            fields = {'name': '队员', 'id': token, 'ceScore': rating,
                      'profession': 1200003, 'level': 70, 'shortUid': 33120849388}
            if layout == 'team':
                fields.update(hp=16759, maxHp=16759, memberIndex=1)
            table(address, fields)
        reader._lua_table_nodes = Mock(side_effect=lambda at: tables.get(at))
        reader._read_lua_string = Mock(side_effect=lambda value, _limit=128: strings.get(value, ''))
        reader.profile_locators = {}
        return reader, address, tables, strings, table

    def test_exact_main_player_table_reads_current_not_maximum_or_legacy_score(self):
        reader, address, tables, _strings, _table = self.bound_reader('actor')
        profile, locator = reader._bound_profile_candidate(address, tables[address], {'AQAAAOwNKLYHAAAA'}, {})
        self.assertEqual(profile['extraordinary_rating'], 80975)
        self.assertEqual(profile['client_rating_key'], 'CEScore')
        self.assertEqual(profile['client_rating_binding'], 'actor_CEScore')
        self.assertEqual(profile['role_number'], 33120849388)
        self.assertEqual(locator['id_key'], 'eid')

    def test_exact_party_model_reads_ce_score_without_rolename(self):
        reader, address, tables, _strings, _table = self.bound_reader('team', rating=95064)
        profile, locator = reader._bound_profile_candidate(address, tables[address], {'AQAAAOwNKLYHAAAA'}, {})
        self.assertEqual(profile['extraordinary_rating'], 95064)
        self.assertEqual(profile['client_rating_binding'], 'team_member_ceScore')
        self.assertEqual(locator['id_key'], 'id')

    def test_stranger_or_generic_profile_cannot_supply_party_score(self):
        reader, address, tables, _strings, _table = self.bound_reader('team')
        self.assertIsNone(reader._bound_profile_candidate(address, tables[address], {'other-player-token'}, {}))
        reader, address, tables, _strings, _table = self.bound_reader('friend')
        self.assertIsNone(reader._bound_profile_candidate(address, tables[address], {'AQAAAOwNKLYHAAAA'}, {}))

    def test_cached_bound_score_updates_and_invalidates_on_resize_or_token_change(self):
        token = 'AQAAAOwNKLYHAAAA'
        reader, address, tables, strings, table = self.bound_reader('team', rating=80702)
        _profile, locator = reader._bound_profile_candidate(address, tables[address], {token}, {})
        reader.profile_locators[token] = locator
        # Same node allocation, updated live score. No GC scan is required.
        data = bytearray(tables[address][3])
        struct.pack_into('<d', data, locator['rating_key_at'] - tables[address][1] - 8, 80975)
        tables[address] = (*tables[address][:3], bytes(data))
        self.assertEqual(reader._cached_profile(token)['extraordinary_rating'], 80975)
        strings[0xfffd800000800008] = 'different-role-token'
        self.assertIsNone(reader._cached_profile(token))
        strings[0xfffd800000800008] = token
        tables[address] = (tables[address][0], tables[address][1] + 24, tables[address][2], tables[address][3])
        self.assertIsNone(reader._cached_profile(token))

    def test_bound_profile_record_keeps_layout_evidence(self):
        reader, address, tables, _strings, _table = self.bound_reader('actor')
        profile, _locator = reader._bound_profile_candidate(address, tables[address], {'AQAAAOwNKLYHAAAA'}, {})
        record = PassiveTeamProfilePoller._record(profile)
        self.assertEqual(record['profile_source'], 'live_lua_bound_rating')
        self.assertEqual(record['client_rating_binding'], 'actor_CEScore')

    def test_legacy_zhanli_is_diagnostic_only_not_the_extraordinary_rating(self):
        token = "AQAAAOwNKLYHAAAA"
        reference = 0x100000
        reader = object.__new__(LiveTeamProfileReader)
        reader._extract_profile = Mock(return_value=(token, "莫雪"))

        key_values = {
            reference: 101,
            reference + 3 * 24: 102,
            reference + 4 * 24: 103,
            reference + 5 * 24: 104,
        }
        row_values = {
            reference - 8: 201,
            reference + 3 * 24 - 8: 202,
            reference + 4 * 24 - 8: 203,
            reference + 5 * 24 - 8: 204,
        }
        strings = {
            101: "rolename",
            102: "id",
            103: "ZhanLi",
            104: "lv",
            201: "莫雪",
            202: token,
        }
        numbers = {203: 80_616.0, 204: 70.0}
        memory = {**key_values, **row_values}
        reader._read_qword = Mock(side_effect=lambda address: memory.get(address, 0))
        reader._read_lua_string = Mock(
            side_effect=lambda raw, _limit=128: strings.get(raw, "")
        )
        reader._lua_number = Mock(side_effect=lambda raw: numbers.get(raw))

        profile, locator = reader._profile_candidate(reference, token)

        self.assertNotIn('extraordinary_rating', profile)
        self.assertEqual(profile['client_rating_candidate'], 80_616)
        self.assertEqual(profile["level"], 70)
        self.assertEqual(locator["rating_key"], "ZhanLi")
        self.assertEqual(locator["rating_key_at"], reference + 4 * 24)

    def test_only_confirmed_power_key_can_supply_a_rating(self):
        for key in ('ZhanLi', 'ceScore', 'CEScore'):
            self.assertNotIn('extraordinary_rating', LiveTeamProfileReader._rating_fields(key, 78750))
        self.assertEqual(LiveTeamProfileReader._rating_fields('power', 88893)['extraordinary_rating'], 88893)

    def test_live_profile_scan_covers_separated_current_heap_band(self):
        self.assertGreaterEqual(
            LiveTeamProfileReader.PROFILE_SCAN_RADIUS, 0x20000000
        )

    def test_known_missing_token_does_not_rescan_the_heap_every_poll(self):
        token = "startup-missing-member"
        reader = object.__new__(LiveTeamProfileReader)
        reader.process = 1
        reader.stop_event = None
        reader.profile_locators = {}
        reader.last_live_profile_scan = 100.0
        reader.live_profile_scan_tokens = {token}
        reader._scan_live_profiles = Mock()
        reader._cached_profile = Mock(return_value=None)

        with patch("runtime_metadata.time.monotonic", return_value=102.0):
            self.assertEqual(reader.snapshot((token,)), [])

        reader._scan_live_profiles.assert_not_called()


if __name__=='__main__':unittest.main()
