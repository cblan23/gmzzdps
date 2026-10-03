"""No-game-access regressions for midstream PvP scene/player recovery."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from npcap_parser_adapter import NpcapParserAdapter
from pvp_client_bootstrap import SOURCE, pvp_lifecycle_records
from pvp_records import PvpHistoryRepository, PvpRecordingController
from test_pvp_tracker import SELF, SELF_TOKEN, ENEMY, ENEMY_TOKEN, OTHER, OTHER_TOKEN, packet


SPACE_TOKEN = 'aq0-XMPGvkVOaw--'
SPACE_ENTITY = 228711842375564
BASE_NS = int(datetime(2026, 9, 18, 21, 36, 7).astimezone().timestamp() * 1e9)


def line(milliseconds, text):
    return f'[2026.09.18-21.36.07:{milliseconds:03d}][618]LuaLog: ReleaseLog: {text}\n'


def space(token=SPACE_TOKEN, map_id=5208003, entity=SPACE_ENTITY, cls='TeamPvpSpace'):
    return (line(547, f'[Entity({cls}, {token})] [Space-LifeTimeStage] Space ctor entityId : {token}, TemplateID:{map_id}')
            + line(547, f'create_entity Entity:{token}, Uid:{entity} cname:{cls} isPlayer: false, is_brief:false'))


def avatar(entity=ENEMY, token=ENEMY_TOKEN, *, brief=False):
    return line(626, f'create_entity Entity:{token}, Uid:{entity} cname:AvatarActor isPlayer: false, is_brief:{str(brief).lower()}')


class PvpClientBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'C7.log'
        self.now_ns = BASE_NS + 30_000_000_000

    def tearDown(self):
        self.temporary.cleanup()

    def records(self, text, **options):
        self.path.write_text(text, encoding='utf-8')
        return pvp_lifecycle_records(self.path, until_ns=self.now_ns, **options)

    def test_exact_team_pvp_scene_and_real_full_avatar_identity(self):
        records = self.records(space() + avatar())
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['capture_source'], SOURCE)
        creation = records[0]['decoded_arguments'][0]
        self.assertEqual(creation['entity_class'], 'TeamPvpSpace')
        self.assertEqual(creation['properties']['TemplateID'], 5208003)
        self.assertFalse(creation['known_property_schema'])
        self.assertNotIn('damage', records[1])
        parser = NpcapParserAdapter()
        for record in records:
            parser.process(record)
        self.assertEqual(parser.wire_map_id, 5208003)
        self.assertEqual(parser.wire_classes[ENEMY], 'AvatarActor')
        self.assertEqual(parser.token_actors[ENEMY_TOKEN], ENEMY)

    def test_closed_arena_or_new_city_never_restores_old_arena(self):
        destroyed = line(999, f'[Entity(TeamPvpSpace, {SPACE_TOKEN})] [Space-LifeTimeStage] [Space][dtor] entityId:{SPACE_TOKEN}')
        self.assertEqual(self.records(space() + avatar() + destroyed), [])
        city = space('aqsvOlADjnghYMxf', 5200002, SPACE_ENTITY + 1, 'MainCitySpace')
        self.assertEqual(self.records(space() + avatar() + city), [])
        records = self.records(space() + avatar() + city, include_non_pvp=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['decoded_arguments'][0]['properties']['TemplateID'], 5200002)

    def test_mismatched_constructor_missing_creation_or_auto_chess_is_rejected(self):
        self.assertEqual(self.records(space().splitlines()[0] + '\n'), [])
        self.assertEqual(self.records(space(map_id=5200024) + avatar()), [])
        self.assertEqual(self.records(space().replace('entityId : ' + SPACE_TOKEN, 'entityId : anotherToken0000')), [])

    def test_deleted_brief_and_previous_scene_avatars_do_not_become_players(self):
        text = avatar(OTHER, OTHER_TOKEN) + space() + avatar() + avatar(OTHER, OTHER_TOKEN, brief=True)
        text += line(999, f'destroy_entity Entity:{ENEMY_TOKEN}, Uid:{ENEMY}')
        records = self.records(text)
        self.assertEqual(len(records), 1)

    def test_new_scene_never_reuses_previous_scene_avatars(self):
        records = self.records(space() + avatar() + space('aq0-NewInstance--', 5208002, SPACE_ENTITY + 1))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['decoded_arguments'][0]['properties']['TemplateID'], 5208002)

    def test_stopped_or_future_client_log_is_not_used_by_live_bootstrap(self):
        self.path.write_text(space() + avatar(), encoding='utf-8')
        with patch('pvp_client_bootstrap.time.time_ns', return_value=BASE_NS + 90_000_000_000):
            self.assertEqual(pvp_lifecycle_records(self.path), [])
        with patch('pvp_client_bootstrap.time.time_ns', return_value=BASE_NS + 30_000_000_000):
            self.assertEqual(len(pvp_lifecycle_records(self.path)), 2)

    def test_worker_restore_emits_real_parser_context_without_queries(self):
        import io
        from test_combat_model import MODULE
        self.path.write_text(space() + avatar(), encoding='utf-8')
        game_path = Path(self.temporary.name) / 'Game' / 'C7' / 'Binaries' / 'Win64' / 'C7-Win64-Shipping.exe'
        client_log = game_path.parents[2] / 'Saved' / 'Logs' / 'C7.log'
        client_log.parent.mkdir(parents=True)
        client_log.write_text(space() + avatar(), encoding='utf-8')
        worker = object.__new__(MODULE['HookWorker'])
        worker._emit_parser_update = Mock()
        worker._emit_completed_pvp_observations = Mock()
        worker._log_experiment_lifecycle = Mock()
        parser = NpcapParserAdapter()
        with patch('pvp_client_bootstrap.time.time_ns', return_value=self.now_ns):
            count = worker._restore_pvp_lifecycle_context(parser, {'game_path': str(game_path)}, io.StringIO())
        self.assertEqual(count, 2)
        self.assertEqual(parser.wire_map_id, 5208003)
        self.assertEqual(parser.wire_classes[ENEMY], 'AvatarActor')
        self.assertEqual(worker._emit_completed_pvp_observations.call_count, 2)

    def test_three_v_three_without_damage_sync_still_records_both_damage_directions(self):
        records = self.records(space() + avatar())
        parser = NpcapParserAdapter()
        repository = PvpHistoryRepository(Path(self.temporary.name) / 'pvp.sqlite3')
        controller = PvpRecordingController(repository)
        controller.bind_account('test-account')
        parser.native_self_id = SELF
        parser.self_id = SELF
        parser.self_token = SELF_TOKEN
        controller.ingest_update('identity', {'self_id': SELF, 'user_token': SELF_TOKEN, 'self_confirmed': True})

        def feed(record):
            for kind, update in parser.process(record):
                controller.ingest_update(kind, update)
            for observation in parser.take_pvp_observations():
                controller.observe(observation)

        for record in records:
            feed(record)
        self.assertTrue(controller.active)
        for seconds, owner, args in (
            (1, ENEMY, [SELF, ENEMY, 860200400, 120, 100]),
            (2, SELF, [ENEMY, SELF, 860200500, 90, 70]),
        ):
            stamp = BASE_NS + seconds * 1_000_000_000
            feed(packet('OnMsgBeatenSyncV2', actor=owner, args=args, sequence=seconds + 10,
                        capture_timestamp_ns=stamp,
                        filetime_100ns=stamp // 100 + 116_444_736_000_000_000))
        # No Avatar creation on the wire or outgoing DamageSync was required.
        state = controller.snapshot()
        self.assertEqual(state['damage'], 100)
        self.assertEqual(state['taken'], 70)
        self.assertEqual(state['outgoing'][0]['opponent_id'], ENEMY_TOKEN)
        saved = controller.stop()
        self.assertEqual(saved['map_id'], 5208003)
        self.assertEqual(saved['damage_done'], 100)
        self.assertEqual(saved['damage_taken'], 70)


if __name__ == '__main__':
    unittest.main()
